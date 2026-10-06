"""Check 4b: workers run side by side, and a task that waits starts only when what it waits for is done.

Run on the Mac:  python3 checks/together.py
The orchestrator (on Haiku, for cost) queues two independent tasks and a third that waits for both.
The verdict comes from the cycle's own timestamps, not from what any agent says.
"""
import json
import sys

from boxrun import LOG, box, reset, shell

PLAN = """Using only your tools, do exactly this and then stop:
1. Write company/roles/helper.md containing exactly these lines:
---
model: haiku
effort: none
---
You do small jobs quickly.
2. Write company/tasks/one.md containing: Run the shell command `sleep 20`, then write the word one to /workspace/one.txt.
3. Write company/tasks/two.md containing: Run the shell command `sleep 20`, then write the word two to /workspace/two.txt.
4. Write company/tasks/three.md containing: Write the contents of /workspace/one.txt and /workspace/two.txt to /workspace/three.txt.
5. Run: assign helper company/tasks/one.md
6. Run: assign helper company/tasks/two.md
7. Run: assign helper company/tasks/three.md after 1,2"""

reset()
box("cycle.py", "1", "--system", "/dev/null", "--check-model", "haiku", "--prompt", PLAN, name="together")

# Each event's time is on its wrapping row, so read the raw log.
times = {}
for line in (LOG / "together.jsonl").read_text().splitlines():
    row = json.loads(line)
    event = row.get("event") or {}
    if row.get("actor") == "cycle" and event.get("type") in ("worker_start", "worker_end"):
        times[(event["type"], event["id"])] = row.get("ts")
started = sorted(n for kind, n in times if kind == "worker_start")
print("starts:", {n: times[("worker_start", n)] for n in started})
print("ends:  ", {n: times[("worker_end", n)] for kind, n in sorted(times) if kind == "worker_end"})
three = shell("cat /workspace/three.txt 2>/dev/null").strip()
print("three.txt:", repr(three))

failures = []
if started != [1, 2, 3]:
    failures.append(f"workers started: {started} instead of [1, 2, 3]")
else:
    s1, s2, s3 = (times[("worker_start", n)] for n in (1, 2, 3))
    e1, e2 = times.get(("worker_end", 1)), times.get(("worker_end", 2))
    if not (e1 and e2 and s2 < e1 and s1 < e2):
        failures.append("tasks 1 and 2 did not overlap")
    if not (e1 and e2 and s3 >= max(e1, e2)):
        failures.append("task 3 started before tasks 1 and 2 had finished")
if "one" not in three or "two" not in three:
    failures.append("task 3 did not see both results")
print(f"CHECK 4b {'PASS' if not failures else 'FAIL'}: " + ("; ".join(failures) or
      "tasks 1 and 2 ran side by side, and task 3 waited for both"))
sys.exit(0 if not failures else 1)
