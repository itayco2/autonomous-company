"""Check 9: the usage cost of one cycle, from identical cycles, which sets the weekly cap.

Run on the Mac:  python3 checks/calibrate.py [--cycles 3] [--limit-min 20]
Each run starts from an empty workspace, so the cycles are identical: a founding cycle with the
real walls file and the real models. Usage is read by a one-word probe before and after each
cycle. Any other use of the same account during a run (including an interactive session) lands
in the delta, so run it while nothing else is using Claude. Results append to
log/checks/calibration.jsonl.
"""
import argparse
import json
import os
import subprocess
import time

os.environ["WORKSPACE_VOLUME"] = "autonomous-company_calibration"
from boxrun import LOG, ROOT, box, cost, of, read, reset, result, usage_reading  # noqa: E402

PROBE = ["claude", "-p", "Reply with the word: ok", "--model", "haiku", "--setting-sources", "",
         "--output-format", "stream-json", "--verbose"]


def utilisation(events):
    """Each window's utilisation (0 to 1) and when it resets. A delta across a reset means nothing."""
    windows = (usage_reading(events) or {}).get("unifiedWindows", {})
    return {w: {"utilization": windows.get(w, {}).get("utilization"), "resetsAt": windows.get(w, {}).get("resetsAt")}
            for w in ("five_hour", "seven_day")}


def delta_pct(before, after):
    """Percentage points used in each window, or None when unreadable or when the window reset in between."""
    deltas = {}
    for w in before:
        b, a = before[w], after[w]
        if None in (b["utilization"], a["utilization"]) or b["resetsAt"] != a["resetsAt"]:
            deltas[w] = None
        else:
            deltas[w] = round(100 * (a["utilization"] - b["utilization"]), 2)
    return deltas


def run_cycle(k, limit_seconds):
    name = f"ac-calibrate-{k}"
    out = LOG / f"calibrate-{k}.jsonl"
    started = time.monotonic()
    with out.open("w") as stdout:
        process = subprocess.Popen(["docker", "compose", "run", "--rm", "-T", "--name", name, "box", "cycle.py", "1"],
                                   cwd=ROOT, stdin=subprocess.DEVNULL, stdout=stdout, stderr=subprocess.DEVNULL)
        try:
            process.wait(timeout=limit_seconds)
            cut = False
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "kill", name], capture_output=True)
            process.wait()
            cut = True
    return read(out), round(time.monotonic() - started), cut


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--limit-min", type=float, default=20)
    args = parser.parse_args()
    before = utilisation(box(*PROBE, name="calibrate-0-probe"))
    for k in range(1, args.cycles + 1):
        reset()
        events, seconds, cut = run_cycle(k, args.limit_min * 60)
        after = utilisation(box(*PROBE, name=f"calibrate-{k}-probe"))
        models = sorted({e.get("model") for e in of(events, kind="system") if e.get("subtype") == "init"})
        row = {
            "cycle": k, "seconds": seconds, "cut_at_limit": cut, "models": models,
            "workers": len(of(events, "cycle", "worker_start")),
            "list_price_usd": cost(events),
            "errors": [str(r.get("result") or "; ".join(r.get("errors") or []))[:160]
                       for r in of(events, kind="result") if r.get("is_error")],
            "served": sorted({m for r in of(events, kind="result") for m in (r.get("modelUsage") or {})}),
            "before": before, "after": after,
            "delta_pct": delta_pct(before, after),
            "orchestrator_result": str(result(events, "orchestrator").get("result", ""))[:300],
        }
        print(json.dumps(row))
        with (LOG / "calibration.jsonl").open("a") as f:
            f.write(json.dumps(row) + "\n")
        before = after


if __name__ == "__main__":
    main()
