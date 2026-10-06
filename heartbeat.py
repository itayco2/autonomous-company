#!/usr/bin/env python3
"""The heartbeat: starts one cycle after another until it is told to stop. Runs on the Mac, never in
the box, and writes the outside log, which the agent cannot touch.

`make start` runs it under caffeinate, so the Mac stays awake while it works. `make stop` (a STOP
file) ends it within a few seconds, killing the cycle in progress; the orchestrator resumes from its
files at the next start. A cycle has no time limit: it lasts until the orchestrator and every worker
it assigned are done.

It reads the usage readings in each cycle's own event stream while the cycle runs:
- the company's share of the week is capped (--share, percentage points of the weekly window); at
  the cap the cycle is ended and the heartbeat sleeps until the weekly reset. The share is saved at
  every reading, so a stop or a power cut never forgets what was spent;
- a rejected 5-hour window ends the cycle and the heartbeat sleeps until that window resets;
- if paid overage is ever available or in use, it stops for good: this run must cost nothing;
- if the token stops working, it stops for good, because only a person can mint a new one.
The readings are account-wide, so anything else using the account during a cycle counts too.

Never two cycles at once: it will not start while any box container of this project is running,
the founding cycle included. Cycle numbers continue from the highest one found in the workspace.

Log: log/heartbeat.jsonl (decisions and one line per cycle), log/cycles/NNNN.jsonl (each cycle's
full event stream), log/heartbeat-state.json (next cycle number and the week's share so far).
"""
import argparse
import json
import os
import pathlib
import queue
import re
import signal
import subprocess
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parent
LOG = ROOT / "log"
CYCLES = LOG / "cycles"
EVENTS = LOG / "heartbeat.jsonl"
STATE = LOG / "heartbeat-state.json"
PIDFILE = LOG / "heartbeat.pid"
STOP = ROOT / "STOP"
VOLUME = os.environ.get("WORKSPACE_VOLUME", "autonomous-company_workspace")
BOXES = ["--filter", "label=com.docker.compose.project=autonomous-company", "--filter", "label=com.docker.compose.service=box"]
AUTH_FAILURE = ("401", "not logged in", "oauth access token", "failed to authenticate")


def log(event, **fields):
    line = json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **fields})
    with EVENTS.open("a") as f:  # the file first: the terminal may already be gone
        f.write(line + "\n")
    try:
        print(line, flush=True)
    except OSError:
        pass


class StateUnreadable(Exception):
    pass


def load_state():
    """The saved state, a fresh one if there is none, and never a silent reset of a damaged one."""
    if not STATE.exists():
        return {"next_cycle": 1, "week": None, "week_used_pp": 0.0}
    try:
        return json.loads(STATE.read_text())
    except ValueError as error:
        raise StateUnreadable(f"{STATE} cannot be read ({error}); fix or remove it by hand") from error


def save_state(state):
    temporary = STATE.with_name(STATE.name + ".tmp")
    temporary.write_text(json.dumps(state))
    os.replace(temporary, STATE)


def highest_cycle(names):
    """The highest cycle number mentioned in the workspace's report and queue file names."""
    numbers = [int(n) for name in names for n in re.findall(r"(?:^c|running-|before-cycle-)(\d+)", name)]
    return max(numbers, default=0)


def workspace_names():
    script = ("import os\nfor d in ('/w/company/reports', '/w/.box', '/w/.box/done', '/w/.box/dropped'):\n"
              "    print('\\n'.join(os.listdir(d)) if os.path.isdir(d) else '')")
    done = subprocess.run(["docker", "run", "--rm", "--network", "none", "-v", f"{VOLUME}:/w:ro",
                           "autonomous-company-box", "python3", "-c", script], capture_output=True, text=True, timeout=120)
    return done.stdout.split()


def quick_failure(outcome):
    """A cycle that ended within a minute and never really ran: an error, a non-zero exit, or no session at all."""
    return outcome["seconds"] < 60 and (outcome["errors"] > 0 or outcome["exit"] not in (0, None) or outcome["results"] == 0)


class Meter:
    """Turns a cycle's rate-limit readings into the week's running share and a reason to stop."""

    def __init__(self, state, share_pp):
        self.state, self.share_pp, self.last = state, share_pp, None
        self.five_hour_reset = None

    def reached(self):
        return self.state["week_used_pp"] >= self.share_pp

    def read(self, info):
        """Account one rate_limit_info. Returns a reason to end the cycle, or None."""
        if info.get("isUsingOverage") or info.get("overageStatus") not in (None, "rejected"):
            return "overage"
        windows = info.get("unifiedWindows") or {}
        week = windows.get("seven_day") or {}
        used, resets = week.get("utilization"), week.get("resetsAt")
        if used is not None and resets is not None:
            if self.state.get("week") != resets:
                # A new weekly window (or the first reading ever): the share starts again from zero.
                self.state["week"], self.state["week_used_pp"], self.last = resets, 0.0, None
            if self.last is not None:
                self.state["week_used_pp"] = round(self.state["week_used_pp"] + max(0.0, 100 * (used - self.last)), 2)
            self.last = used
        self.five_hour_reset = (windows.get("five_hour") or {}).get("resetsAt") or self.five_hour_reset
        if info.get("status") == "rejected":
            self.five_hour_reset = info.get("resetsAt") or self.five_hour_reset
            return "rate_limited"
        return "weekly_share" if self.reached() else None


def classify_result(event):
    """A reason to stop from one session's result event, or None."""
    if not event.get("is_error"):
        return None
    text = str(event.get("result") or " ".join(event.get("errors") or [])).lower()
    if any(marker in text for marker in AUTH_FAILURE):
        return "auth"
    if event.get("api_error_status") == 429:
        return "rate_limited"
    return None


def stop_requested():
    return STOP.exists()


def docker_up():
    try:
        return subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def running_boxes():
    try:
        return subprocess.run(["docker", "ps", "-q", *BOXES], capture_output=True, text=True, timeout=20).stdout.split()
    except (OSError, subprocess.TimeoutExpired):
        return []


def container_running(name):
    try:
        out = subprocess.run(["docker", "ps", "-q", "--filter", f"name=^{name}$"], capture_output=True, text=True, timeout=20)
        return bool(out.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return True


def kill_cycle(name, patience=30):
    """Kill a cycle's container and keep at it until it is really gone."""
    deadline = time.monotonic() + patience
    while True:
        try:
            subprocess.run(["docker", "kill", name], capture_output=True, timeout=20, start_new_session=True)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if not container_running(name) or time.monotonic() > deadline:
            return not container_running(name)
        time.sleep(1)


def run_cycle(n, meter):
    """One cycle in its own container, its stream copied to log/cycles/, ended early only for a reason."""
    name = f"ac-cycle-{n}"
    CYCLES.mkdir(parents=True, exist_ok=True)
    # Its own session: closing the terminal reaches the heartbeat, which ends the cycle properly.
    process = subprocess.Popen(["docker", "compose", "run", "--rm", "-T", "--name", name, "box", "cycle.py", str(n)],
                               cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True, start_new_session=True)
    lines = queue.Queue()
    threading.Thread(target=lambda: [lines.put(l) for l in process.stdout] + [lines.put(None)], daemon=True).start()
    started, ended_by, cost, workers, said, errors, results = time.monotonic(), None, 0.0, 0, "", 0, 0
    last_kill = 0.0
    with (CYCLES / f"{n:04d}.jsonl").open("a") as out:
        while True:
            try:
                line = lines.get(timeout=1)
            except queue.Empty:
                line = ""
            if line is None:
                break
            if line:
                out.write(line)
                out.flush()
                try:
                    row = json.loads(line)
                except ValueError:
                    row = {}
                actor, event = row.get("actor"), row.get("event") or {}
                reason = None
                if event.get("type") == "rate_limit_event":
                    before = (meter.state.get("week"), meter.state["week_used_pp"])
                    reason = meter.read(event.get("rate_limit_info") or {})
                    if (meter.state.get("week"), meter.state["week_used_pp"]) != before:
                        save_state(meter.state)  # a power cut loses at most one reading
                elif event.get("type") == "result":
                    results += 1
                    cost += event.get("total_cost_usd") or 0
                    errors += bool(event.get("is_error"))
                    reason = classify_result(event)
                    if actor == "orchestrator":
                        said = str(event.get("result") or "")[:400]
                elif event.get("type") == "worker_start":
                    workers += 1
                if reason and not ended_by:
                    ended_by = reason
            if stop_requested() and ended_by is None:
                ended_by = "stop"
            # Once there is a reason, keep killing until the container is gone.
            if ended_by and time.monotonic() - last_kill > 5:
                last_kill = time.monotonic()
                threading.Thread(target=kill_cycle, args=(name, 10), daemon=True).start()
    process.wait()
    return {"n": n, "seconds": round(time.monotonic() - started), "ended_by": ended_by or "finished",
            "exit": process.returncode, "cost_usd": round(cost, 4), "workers": workers, "errors": errors,
            "results": results, "week_used_pp": meter.state["week_used_pp"], "orchestrator_said": said}


def sleep_until(epoch, why, quiet=False):
    """Sleep until a Unix time, waking early for a STOP. False if stopped."""
    if not quiet:
        log("sleeping", why=why, until=time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch)))
    while time.time() < epoch:
        if stop_requested():
            return False
        time.sleep(min(5, max(0.1, epoch - time.time())))
    return True


def status():
    running = False
    try:
        os.kill(int(PIDFILE.read_text()), 0)
        running = True
    except (OSError, ValueError):
        pass
    try:
        state = load_state()
    except StateUnreadable as error:
        print(error)
        return
    boxes = running_boxes()
    print(f"heartbeat: {'running' if running else 'not running'} | cycles running now: {len(boxes)} | "
          f"next cycle {state['next_cycle']} | company's share of this week so far: {state['week_used_pp']} points")
    lines = EVENTS.read_text().splitlines() if EVENTS.exists() else []
    for line in lines[-8:]:
        row = json.loads(line)
        detail = {k: row[k] for k in ("n", "ended_by", "seconds", "workers", "cost_usd", "why", "until") if k in row}
        print(f"  {row['ts'][:16]}  {row['event']:<14} {detail}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--share", type=float, default=20, help="weekly share cap, in percentage points")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    LOG.mkdir(exist_ok=True)
    if args.status:
        return status()
    try:
        os.kill(int(PIDFILE.read_text()), 0)
        print("a heartbeat is already running; `make stop` first")
        return
    except (OSError, ValueError):
        pass
    try:
        state = load_state()
    except StateUnreadable as error:
        print(error)
        return
    PIDFILE.write_text(str(os.getpid()))
    STOP.unlink(missing_ok=True)
    current = {"name": None}

    def end(signum, frame):
        # Ignore any further signal (closing a terminal sends two), so the cleanup below always finishes.
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(sig, signal.SIG_IGN)
        raise SystemExit(f"signal {signum}")

    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, end)

    meter = Meter(state, args.share)
    quick_failures, why_stopped, docker_wait, last_end = 0, "stop", 30, 0.0
    try:
        subprocess.run(["docker", "compose", "up", "-d", "door", "window", "office", "desk"], cwd=ROOT, capture_output=True)
        highest = highest_cycle(workspace_names())
        if highest >= state["next_cycle"]:
            state["next_cycle"] = highest + 1
            save_state(state)
        log("started", volume=VOLUME, share_pp=args.share, next_cycle=state["next_cycle"], week_used_pp=state["week_used_pp"])
        while not stop_requested():
            if not docker_up():
                log("docker_unreachable", retry_in_s=docker_wait)
                if not sleep_until(time.time() + docker_wait, "docker_unreachable", quiet=True):
                    break
                docker_wait = min(docker_wait * 2, 600)
                continue
            docker_wait = 30
            others = running_boxes()
            if others:
                log("waiting", why="another cycle is running (the founding cycle or a leftover)", containers=len(others))
                while running_boxes():
                    if not sleep_until(time.time() + 15, "other_cycle", quiet=True):
                        break
                continue
            if meter.reached() and state.get("week") and time.time() < state["week"]:
                if not sleep_until(state["week"], "weekly_share"):
                    break
                continue
            n = state["next_cycle"]
            state["next_cycle"] = n + 1  # saved before the cycle, so a crash never reuses a number
            save_state(state)
            current["name"] = f"ac-cycle-{n}"
            if time.time() - last_end > 120:
                meter.last = None  # after a pause, others may have used the account: start a new baseline
            log("cycle_start", n=n)
            outcome = run_cycle(n, meter)
            current["name"], last_end = None, time.time()
            save_state(state)
            log("cycle_end", **outcome)
            if outcome["ended_by"] in ("stop", "overage", "auth"):
                why_stopped = outcome["ended_by"]
                break
            if outcome["ended_by"] == "rate_limited":
                if not sleep_until(meter.five_hour_reset or time.time() + 1800, "rate_limited"):
                    break
                continue
            quick_failures = quick_failures + 1 if quick_failure(outcome) else 0
            if quick_failures >= 3:
                why_stopped = "repeated_quick_failures"
                break
            if not sleep_until(time.time() + (300 * quick_failures or 5), "between_cycles", quiet=not quick_failures):
                break
    except SystemExit as reason:
        why_stopped = str(reason)
    finally:
        if current["name"]:
            kill_cycle(current["name"])
        save_state(state)
        STOP.unlink(missing_ok=True)
        PIDFILE.unlink(missing_ok=True)
        log("stopped", why=why_stopped, next_cycle=state["next_cycle"], week_used_pp=state["week_used_pp"])


if __name__ == "__main__":
    main()
