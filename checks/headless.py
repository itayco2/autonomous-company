"""Checks 1 and 7: headless calls work in the box, and the stream carries a usage reading.

Run on the Mac:  python3 checks/headless.py
Two calls: a cheap one on Haiku, then the orchestrator's own setting, Opus 5.5 at max effort,
which must report that exact model back.
"""
import json
import sys

from boxrun import box, result, usage_reading

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "box" / "bin"))
from company import ORCHESTRATOR_EFFORT, ORCHESTRATOR_MODEL  # noqa: E402

CALL = ["claude", "-p", "Reply with the single word: ready", "--setting-sources", "",
        "--output-format", "stream-json", "--verbose"]
runs = {
    "haiku": box(*CALL, "--model", "haiku", name="headless"),
    "orchestrator": box(*CALL, "--model", ORCHESTRATOR_MODEL, "--effort", ORCHESTRATOR_EFFORT, name="headless-opus"),
}
passed = {}
for label, events in runs.items():
    init = next((e for _, e in events if e.get("subtype") == "init"), {})
    final = result(events)
    print(f"{label} init:", {k: init.get(k) for k in ("claude_code_version", "model", "apiKeySource")})
    print(f"{label} result:", {k: final.get(k) for k in ("is_error", "result", "duration_ms", "total_cost_usd", "terminal_reason")})
    passed[label] = not final.get("is_error") and "ready" in str(final.get("result", "")).lower()
    if label == "orchestrator":
        # modelUsage is keyed by the model that was actually billed, not the one that was asked for.
        served = sorted(final.get("modelUsage") or {})
        print("orchestrator served by:", served)
        passed[label] = passed[label] and init.get("model") == ORCHESTRATOR_MODEL and ORCHESTRATOR_MODEL in served

reading = usage_reading(runs["orchestrator"])
print("usage reading:", json.dumps(reading))
one = all(passed.values())
windows = (reading or {}).get("unifiedWindows", {})
seven = bool(reading) and all("utilization" in windows.get(w, {}) for w in ("five_hour", "seven_day"))
print(f"CHECK 1 {'PASS' if one else 'FAIL'}: headless in the box: {passed} (orchestrator asked for and "
      f"served by {ORCHESTRATOR_MODEL}; effort {ORCHESTRATOR_EFFORT} was accepted, the stream does not echo it)")
print(f"CHECK 7 {'PASS' if seven else 'FAIL'}: 5-hour and weekly utilisation in the stream")
sys.exit(0 if one and seven else 1)
