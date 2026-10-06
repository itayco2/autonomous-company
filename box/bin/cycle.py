#!/usr/bin/env python3
"""cycle.py <n>: one turn of the company, inside a fresh box.

First the orchestrator: Opus 5.5 at max effort, with a narrow tool list (read anything, write
under company/, run `assign`). Then the workers it assigned this cycle, one at a time, with the
full tool set, each on the model and effort its role file names. Every line on stdout is one event
for the outside log, which the heartbeat writes on the Mac. Nothing here writes a log inside the
workspace, where the agent could edit it. What the person watching provided never reaches that
log: each vault value in a line becomes [vault:NAME] before the line is printed.

The person watching speaks only at a cycle's start: notes they left, and their answers at the front
desk to what the head asked for. Each goes into the outside log, and counts as heard only once the
head's session has ended well, so a cycle that is killed or fails tells it again. The head also
hears which of its requests can no longer be answered, because the file changed after `ask`.

The press space is measured here too, at the start, every minute while the workers run, and at
the end, against the budget they set. While it is over, a chronicler's task opens with bringing it
back under, and a chronicler still running once it is a quarter over is stopped.

A power cut can land anywhere, so a queue lives for one cycle only. Whatever is left in it when the
next cycle starts is dropped, not run, and each report says so; the orchestrator decides again.
That also drops anything a worker slipped into the queue, so only the orchestrator assigns work.
"""
import argparse
import datetime
import json
import os
import pathlib
import signal
import subprocess
import threading
import time

from company import (ASK_ID, ASKS, BOX, COMPANY, DECISIONS, DESK, ITEM_NAME, MAX_TOGETHER, MCP_CONFIG,
                     ORCHESTRATOR_EFFORT, ORCHESTRATOR_MODEL, PRESS_CAP, PRESS_SPACE, QUEUE, ROLE_NAME, VAULT,
                     WORKSPACE, answers, fingerprint, parse_role, read_jsonl, read_queue, ready_to_start,
                     replaced_ids, report_path, space_used, write_atomic)

WALLS = pathlib.Path("/opt/company/walls.md")
NOTES = pathlib.Path("/opt/company/notes")
DELIVERED = BOX / "notes-delivered.json"
HEARD = BOX / "answers-heard.json"
# What the desk writes in the book. Any other line there is not an answer, whatever it holds.
DECIDED = ("provided", "no", "withdrawn")
# A running chronicler is stopped a quarter past the budget: room to finish a file, not a second budget.
PRESS_STOP = PRESS_CAP * 5 // 4
STOP_AT = f"{PRESS_STOP / 1e9:g} GB"
# A shorter vault value is too likely to be an ordinary word in the log, so it is left as it is.
SECRET_MIN = 8


def owner_notes():
    """Notes from the person watching that the head has not had yet. They go with the first message
    of a cycle and into the outside log, so every human input is on record."""
    try:
        seen = set(json.loads(DELIVERED.read_text()))
    except (OSError, ValueError, TypeError):
        seen = set()
    fresh = [p for p in sorted(NOTES.glob("*.md")) if p.is_file() and p.name not in seen] if NOTES.is_dir() else []
    return fresh, seen


def desk_answers():
    """Answers from the front desk that the head has not heard yet. A withdrawn yes is an answer too."""
    try:
        heard = set(json.loads(HEARD.read_text()))
    except (OSError, ValueError, TypeError):
        heard = set()
    fresh = [d for d in read_jsonl(DECISIONS) if isinstance(d.get("id"), str) and d.get("decision") in DECIDED
             and f"{d['id']}@{d.get('ts')}" not in heard]
    return fresh, heard


def mark_heard(notes, seen, fresh, heard):
    """Record that the head heard these notes and answers. Called only once its session has ended
    well: being told twice is harmless, losing an answer is not."""
    if notes:
        write_atomic(DELIVERED, json.dumps(sorted(seen | {note.name for note in notes})))
    if fresh:
        write_atomic(HEARD, json.dumps(sorted(heard | {f"{a['id']}@{a.get('ts')}" for a in fresh})))


def plain(value, limit):
    """A field from the book as one line of text, or '' when it is not text."""
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def told(answer):
    """One answer as one line for the head. A field of the wrong type is left out, never fatal."""
    names = answer.get("names")
    names = ", ".join(n for n in names if isinstance(n, str)) if isinstance(names, list) else ""
    ident, title = plain(answer.get("id"), 40), plain(answer.get("title"), 160) or "a request"
    if answer.get("decision") == "withdrawn":
        line = f"- {names}: taken back out of the vault."
    elif answer.get("decision") == "provided":
        line = f"- {ident} ({title}): provided." + (f" In the vault: {names}." if names else "")
    else:
        line = f"- {ident} ({title}): no."
    note = plain(answer.get("note"), 600)
    return line + (f" Their note: {note}" if note else "")


def request_text(name):
    """A request file's text, or None if it is gone or is not a file under company/."""
    try:
        path = (WORKSPACE / name).resolve()
        if COMPANY.resolve() not in path.parents or not path.is_file():
            return None
        return path.read_text(errors="replace")
    except (OSError, ValueError):
        return None


def void_asks(asked, answered, read):
    """A line for the head about each of its requests that the person can no longer answer: not
    answered, not replaced by a newer ask, and its file changed or gone since `ask`. The desk refuses
    an answer to an edited request, so without this the head would wait for ever. Only the first
    entry of an id counts, as at the desk."""
    first = {}
    for entry in asked:
        if isinstance(entry.get("id"), str) and ASK_ID.fullmatch(entry["id"]):
            first.setdefault(entry["id"], entry)
    replaced, lines = replaced_ids(asked), []
    for ident, entry in first.items():
        name, sha = entry.get("file"), entry.get("sha256")
        if ident in answered or ident in replaced or not isinstance(name, str) or not isinstance(sha, str):
            continue
        text = read(name)
        if text is None or fingerprint(text) != sha:
            what = "is gone" if text is None else "changed since it was asked"
            lines.append(f"- {ident} can no longer be answered: {name} {what}; ask again if it is still wanted.")
    return lines


def size(n):
    """420 MB, 1 GB, 1.2 GB: the way a person reads a disk."""
    return f"{n / 1e9:.1f} GB".replace(".0 GB", " GB") if n >= 1e9 else f"{n / 1e6:,.0f} MB"


def press_line(used):
    """The press budget in one sentence, for the head and for every worker."""
    folders = [f"/{p.relative_to(WORKSPACE.parent)}/".replace("//", "/") for p in PRESS_SPACE]
    where = ", ".join(folders[:-1]) + (" and " if len(folders) > 1 else "") + folders[-1]
    line = (f"The press space ({where}) holds {size(used)} of the {size(PRESS_CAP)} the person watching "
            "allows for it, and press material counts wherever it is kept, so moving it frees nothing: keep "
            "media small, and delete raw frames and drafts once they are used.")
    if used > PRESS_CAP:
        line += " It is over that now: anyone adding to it first brings it back under."
    return line


def press_first(role, used):
    """The line a chronicler's task opens with while the press space is over its budget, or ''.
    The task still runs: a chronicler is the one who can bring the space back under."""
    if not role.startswith("chronicler") or used <= PRESS_CAP:
        return ""
    return (f"Before anything else: the press space holds {size(used)}, over the {size(PRESS_CAP)} the person "
            "watching allows. Bring it back under that first, by deleting raw frames, drafts and copies; "
            f"moving files elsewhere does not count. A chronicler still running when it passes {STOP_AT} is "
            "stopped. Then do this task with what fits.\n\n")


def chroniclers_to_stop(sessions, used):
    """Which running sessions the press watch stops, from {session: (role, reading at its start)}:
    each chronicler's once the space is past PRESS_STOP and past where it stood when the session
    started. One that started over the line was told to bring the space down first, and may."""
    return sorted(s for s, (role, start) in sessions.items()
                  if str(role).startswith("chronicler") and used > max(PRESS_STOP, start))


def ending(text, started, code, why):
    """What a worker's report says once its session is over, or None to leave it as the agent wrote it.
    A session the harness stopped says why, in the report the agent wrote or in place of a missing one."""
    if text == started:
        return (f"Ended {now():%Y-%m-%d %H:%M} (exit code {code}) without writing a report.\n"
                + (f"It was stopped: {why}\n" if why else ""))
    if why:
        return text.rstrip("\n") + f"\n\nStopped {now():%Y-%m-%d %H:%M} before it finished: {why}\n"
    return None


# Each worker's session while it runs, by its name in the log: ((task number, role, press reading at
# its start), process). The press watch reads it; STOPPED holds why it stopped one, for its report.
WORKING, STOPPED = {}, {}
WORKING_LOCK = threading.Lock()


def signal_session(process, sig):
    """A signal to the session and everything it started: each session is its own process group."""
    try:
        os.killpg(process.pid, sig)
    except OSError:  # already gone
        pass


def stop_chroniclers(used):
    """Stop the running chroniclers' sessions that chroniclers_to_stop names. One that is still there
    at the next reading is killed outright. Returns the task numbers stopped now."""
    why = (f"the press space reached {size(used)}, past the {STOP_AT} at which a chronicler is stopped "
           f"(its budget is {size(PRESS_CAP)}). Bring it back under before adding to it.")
    stopped = []
    with WORKING_LOCK:
        for actor, (_, process) in WORKING.items():
            if actor in STOPPED and process.poll() is None:
                signal_session(process, signal.SIGKILL)
        running = {actor: (worker[1], worker[2]) for actor, (worker, process) in WORKING.items()
                   if actor not in STOPPED and process.poll() is None}
        for actor in chroniclers_to_stop(running, used):
            worker, process = WORKING[actor]
            signal_session(process, signal.SIGTERM)
            STOPPED[actor] = why
            stopped.append(worker[0])
    return stopped


def watch_press(stop):
    """Measure the press space every minute while the workers run, into the outside log. A session
    hears the reading only at its start, so the harness is what stops a chronicler far over it."""
    while not stop.wait(60):
        used = space_used(PRESS_SPACE)
        emit("cycle", {"type": "press_space", "used": used, "cap": PRESS_CAP})
        stopped = stop_chroniclers(used)
        if stopped:
            emit("cycle", {"type": "press_over", "used": used, "cap": PRESS_CAP, "stopped": stopped})


ORCHESTRATOR_TOOLS = [
    "--tools", "Read,Glob,Grep,Write,Edit,Bash",
    "--allowedTools", "Read,Glob,Grep,Edit(company/**),Write(company/**),Bash(assign *),Bash(ask *)",
    "--permission-mode", "dontAsk",
    # No settings files: a worker can write /workspace/.claude/settings.json, and the orchestrator
    # must not inherit permissions or hooks from it. Managed settings in /etc still apply.
    "--setting-sources", "",
    # Read-only commands like `ls` skip permission rules, so a hook sees every Bash call first.
    "--settings", json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": "/opt/company/bin/only-assign.py"}]}]}}),
]
WORKER_TOOLS = ["--permission-mode", "bypassPermissions", "--setting-sources", ""]
STREAM = ["--output-format", "stream-json", "--verbose"]

WORKER_PREAMBLE = """You are an agent in a sealed computer, given one task by the head of this workspace.
Nobody will answer questions, so decide for yourself and finish. The workspace is /workspace.
You can reach the public internet and install libraries with npm and pip. You are not an
administrator, so system packages cannot be installed;
Chromium is at /usr/bin/chromium.
Act in the world (publish, sell, post, sign up, write to people) only when your task says the person
watching has said yes to it at the front desk. When you do, these rules have no exceptions. Sell and
publish only the company's own work, described truthfully. No spam, no fake reviews, no pretending to
be a person or another company, and each site's own rules apply. Never claim or suggest that people
made the work (no invented founders, team members, faces or testimonials), and if anyone asks, say
plainly that the company is run by AI agents. Use only the accounts and keys the person
watching provided: they are files in /opt/company/vault/, one per item. Never create an account,
never spend money, and never send a vault value anywhere except the service it belongs to, nor
write it into any file, page or report. Use a vault value inside the command that sends it, as
$(cat /opt/company/vault/NAME), and never print it.
The folder /workspace/view/ is served as static files to a person watching in a browser.
Your role:
"""


PRINTING = threading.Lock()
VAULT_SEEN = {}  # the vault as last read, and how its folder looked then


def vault_values(folder, cache):
    """{name: value} for each value in the vault long enough to look for, read again only when the
    folder or a file in it changed. A vault this box cannot read is empty here, as it is to agents."""
    try:
        with os.scandir(folder) as found:
            files = sorted((e.name, e.inode(), e.stat().st_mtime_ns, e.stat().st_size)
                           for e in found if ITEM_NAME.fullmatch(e.name) and e.is_file())
        key = (os.stat(folder).st_mtime_ns, files)
    except OSError:
        return cache.get("values", {})
    if cache.get("key") != key:
        values = {}
        for name, *_ in files:
            try:
                value = (pathlib.Path(folder) / name).read_text(errors="replace").strip()
            except OSError:
                continue
            if len(value) >= SECRET_MIN:
                values[name] = value
        cache.update(key=key, values=values)
    return cache["values"]


def scrub(value, forms):
    """A decoded line with the vault values taken out of every text in it. A number stays the number
    it was, value or not: the heartbeat adds up the costs in this log, and a text there would stop it."""
    if isinstance(value, dict):
        return {scrub(k, forms): scrub(v, forms) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, forms) for v in value]
    if isinstance(value, str):
        for form, name in forms:
            if form in value:
                value = value.replace(form, f"[vault:{name}]")
    return value


def redact(line, values):
    """The line with each vault value in it, as written or as JSON writes it, replaced by
    [vault:NAME]. Longest first, so a value that holds another, or its own escaped form, goes whole.
    The line stays one JSON object: a value can also match across JSON's own syntax (the digits of a
    number, the letter after a backslash), and a line that no longer reads is redacted text by text."""
    forms = sorted(((form, name) for name, value in values.items() for form in {value, json.dumps(value)[1:-1]}),
                   key=lambda pair: -len(pair[0]))
    clean = line
    for form, name in forms:
        if form in clean:
            clean = clean.replace(form, f"[vault:{name}]")
    if clean == line:
        return line
    try:
        json.loads(clean)
    except (ValueError, RecursionError):
        try:
            return json.dumps(scrub(json.loads(line), forms))
        except (ValueError, RecursionError):  # not a JSON line to begin with
            return clean
    return clean


def emit(actor, event):
    line = json.dumps({"actor": actor, "ts": round(datetime.datetime.now().timestamp(), 2), "event": event})
    with PRINTING:  # workers run side by side; one whole line at a time
        # An agent that reads or prints a vault value puts it in its stream; the outside log gets the name.
        print(redact(line, vault_values(VAULT, VAULT_SEEN)), flush=True)


def now():
    return datetime.datetime.now().astimezone()


def claude(actor, prompt, model, effort, flags, system, env_extra, worker=None):
    """One headless session. The prompt goes in on stdin and the system text as `--flag=value`, so
    text the agent wrote can never be read as a command-line option. A worker's session is listed
    in WORKING while it runs, as (task number, role, press reading at its start)."""
    command = ["claude", "-p", "--model", model, *(["--effort", effort] if effort else []),
               *flags, *STREAM, f"--append-system-prompt={system}"]
    env = {**os.environ, **env_extra}
    # Its own process group, so stopping a session stops what it started (a capture, a render) too.
    with subprocess.Popen(command, cwd=WORKSPACE, env=env, text=True, start_new_session=True,
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE) as process:
        if worker:
            with WORKING_LOCK:
                WORKING[actor] = (worker, process)
        try:
            process.stdin.write(prompt)
            process.stdin.close()
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except (ValueError, RecursionError):  # a tool input nested past Python's limit reads as neither
                    event = {"type": "raw", "text": line.rstrip()}
                emit(actor, event)
        finally:
            if worker:
                with WORKING_LOCK:
                    WORKING.pop(actor, None)
    return process.returncode


def drop_leftovers(n):
    """Queues left by a cycle that was cut off are not run. A report still saying Queued is told
    why; one still saying Started keeps its own note that the agent was cut off."""
    for path in [QUEUE, *sorted(BOX.glob("running-*.jsonl"))]:
        if not path.exists():
            continue
        entries, unreadable = read_queue(path)
        for entry in entries:
            report = report_path(entry)
            if report and report.is_file() and report.read_text(errors="replace").startswith("Queued"):
                write_atomic(report, f"Not run: this computer stopped before the task started (queued in "
                                     f"cycle {entry.get('cycle')}). Assign it again if it is still wanted.\n")
        (BOX / "dropped").mkdir(parents=True, exist_ok=True)
        os.replace(path, BOX / "dropped" / f"before-cycle-{n}-{path.name}")
        emit("cycle", {"type": "queue_dropped", "file": path.name, "entries": len(entries), "unreadable": unreadable})


def worker_tools():
    """Every worker's flags, plus the MCP servers the head listed in company/mcp.json, if it is valid.
    Only workers get them: the orchestrator keeps its narrow tools."""
    try:
        servers = json.loads(MCP_CONFIG.read_text()).get("mcpServers")
    except (OSError, ValueError, AttributeError):
        return WORKER_TOOLS
    return [*WORKER_TOOLS, "--mcp-config", str(MCP_CONFIG)] if isinstance(servers, dict) and servers else WORKER_TOOLS


def run_worker(entry, n):
    """Run one queued task on the model its role file names now. The file is checked again here,
    because a worker can rewrite role files after `assign` accepted them."""
    report = report_path(entry)
    if report is None or not ROLE_NAME.fullmatch(entry["role"]):
        emit("cycle", {"type": "worker_refused", "id": entry.get("id"), "reason": "malformed queue entry"})
        return None
    try:
        model, effort, instructions = parse_role((COMPANY / "roles" / f"{entry['role']}.md").read_text(errors="replace"))
        task_path = (WORKSPACE / entry["task"]).resolve()
        if COMPANY.resolve() not in task_path.parents:
            raise ValueError("the task file is not under company/")
        task = task_path.read_text(errors="replace")
    except (OSError, ValueError) as error:
        write_atomic(report, f"Not run: {error}\n")
        emit("cycle", {"type": "worker_refused", "id": entry.get("id"), "reason": str(error)[:300]})
        return None
    runs_on = model + (f", effort {effort}" if effort else "")
    started = (f"Started {now():%Y-%m-%d %H:%M} in cycle {n} by {entry['role']} ({runs_on}). If this line is "
               "still here in a later cycle, the agent was cut off before it finished.\n")
    write_atomic(report, started)
    emit("cycle", {"type": "worker_start", **entry, "model": model, "effort": effort})
    used = space_used(PRESS_SPACE)
    prompt = (f"{press_first(entry['role'], used)}{task}\n\nWhen you are done, write your report to {report}, "
              "replacing what is there: what you did, where the result is, and what is unfinished.")
    actor = f"worker:{entry['role']}#{entry.get('id')}"
    code = claude(actor, prompt, model, effort, worker_tools(),
                  WORKER_PREAMBLE.replace("Your role:\n", press_line(used) + "\nYour role:\n") + instructions,
                  {"COMPANY_ACTOR": "worker", "COMPANY_CYCLE": str(n)}, worker=(entry.get("id"), entry["role"], used))
    with WORKING_LOCK:
        why = STOPPED.pop(actor, None)
    text = ending(report.read_text(errors="replace"), started, code, why)
    if text is not None:
        write_atomic(report, text)
    return code


def work(entry, n):
    try:
        code = run_worker(entry, n)
    except Exception as error:  # one worker that cannot start must not stop the rest
        report = report_path(entry)
        if report:
            write_atomic(report, f"Not run: {type(error).__name__}: {error}\n")
        emit("cycle", {"type": "worker_failed", "id": entry.get("id"), "reason": str(error)[:300]})
        code = None
    emit("cycle", {"type": "worker_end", "id": entry.get("id"), "exit": code})


def run_workers(n):
    """Run what the orchestrator queued this cycle, up to MAX_TOGETHER at a time, in the order it
    assigned them, each as soon as the tasks it waits for have finished. The queue is moved aside
    and read once, before any worker starts, so nothing a worker writes to it runs."""
    if not QUEUE.exists():
        return
    running_file = BOX / f"running-{n}.jsonl"
    os.replace(QUEUE, running_file)
    pending, unreadable = read_queue(running_file)
    if unreadable:
        emit("cycle", {"type": "queue_unreadable_lines", "count": unreadable})
    finished, running = set(), {}
    while pending or running:
        for entry in ready_to_start(pending, finished, running, MAX_TOGETHER):
            pending.remove(entry)
            thread = threading.Thread(target=work, args=(entry, n), daemon=True)
            running[entry.get("id")] = thread
            thread.start()
        time.sleep(0.5)
        for number, thread in list(running.items()):
            if not thread.is_alive():
                del running[number]
                finished.add(number)
    (BOX / "done").mkdir(parents=True, exist_ok=True)
    os.replace(running_file, BOX / "done" / running_file.name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("n", type=int)
    parser.add_argument("--prompt", help="replaces the date-and-cycle prompt (checks only)")
    parser.add_argument("--system", default=str(WALLS), help="file given as the orchestrator's system prompt")
    parser.add_argument("--check-model", help="checks only: run the orchestrator on this model, effort unset")
    parser.add_argument("--no-workers", action="store_true")
    args = parser.parse_args()

    COMPANY.mkdir(exist_ok=True)
    BOX.mkdir(exist_ok=True)
    started = now()
    model, effort = (args.check_model, None) if args.check_model else (ORCHESTRATOR_MODEL, ORCHESTRATOR_EFFORT)
    emit("cycle", {"type": "cycle_start", "n": args.n, "time": started.isoformat(timespec="seconds"),
                   "orchestrator": {"model": model, "effort": effort}})
    drop_leftovers(args.n)
    prompt = args.prompt or f"It is {started:%A %Y-%m-%d %H:%M %Z}. This is cycle {args.n}."
    notes, seen = owner_notes() if not args.prompt else ([], set())
    fresh, heard = desk_answers() if not args.prompt else ([], set())
    for note in notes:
        text = note.read_text(errors="replace")
        prompt += f"\n\n{text.strip()}"
        emit("cycle", {"type": "owner_note", "name": note.name, "text": text[:4000]})
    if fresh:
        prompt += (f"\n\nThe person watching answered at the front desk (the record is {DECISIONS}; what they "
                   f"provided is in {VAULT}/):\n" + "\n".join(told(a) for a in fresh))
        for answer in fresh:
            emit("cycle", {"type": "owner_answer", **{k: answer.get(k) for k in ("id", "decision", "title", "note", "names")}})
    void = void_asks(read_jsonl(ASKS), answers(read_jsonl(DECISIONS)), request_text) if not args.prompt else []
    if void:
        prompt += "\n\n" + "\n".join(void)
    used = space_used(PRESS_SPACE)
    emit("cycle", {"type": "press_space", "used": used, "cap": PRESS_CAP})
    if not args.prompt:
        prompt += "\n\n" + press_line(used)
    code = claude("orchestrator", prompt, model, effort, ORCHESTRATOR_TOOLS, pathlib.Path(args.system).read_text(),
                  {"COMPANY_ACTOR": "orchestrator", "COMPANY_CYCLE": str(args.n)})
    emit("cycle", {"type": "orchestrator_end", "exit": code})
    if code == 0:
        mark_heard(notes, seen, fresh, heard)
    if not args.no_workers:
        stop = threading.Event()
        threading.Thread(target=watch_press, args=(stop,), daemon=True).start()
        run_workers(args.n)
        stop.set()
    # The last reading comes after the last write: the office shows it until the next cycle starts.
    emit("cycle", {"type": "press_space", "used": space_used(PRESS_SPACE), "cap": PRESS_CAP})
    emit("cycle", {"type": "cycle_end", "n": args.n})


if __name__ == "__main__":
    main()
