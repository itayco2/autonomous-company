"""Check 4: `assign` starts a worker that writes working code, and the orchestrator reads its report.

Run on the Mac:  python3 checks/delegate.py
Cycle 1: the orchestrator writes a role and a task and assigns it; the worker runs after it.
Cycle 2: a new orchestrator session, with no memory, reads the report and quotes its first line.
"""
import json
import sys

from boxrun import box, cost, of, reset, result, shell, usage_reading

HIRE = """Using only your tools, do exactly this and then stop:
1. Write company/roles/developer.md containing exactly these lines:
---
model: haiku
effort: none
---
You are a Python developer. Write small, tested code.
2. Write company/tasks/fib.md asking for a file /workspace/product/fib.py with a function fib(n)
   that returns the n-th Fibonacci number (fib(0) = 0, fib(1) = 1), which asserts fib(30) == 832040
   when the file is run, and for the developer to run it once to show that it passes.
3. Run the command: assign developer company/tasks/fib.md"""

READ = "Read company/reports/c0001-01-fib.md and reply with its first non-empty line, verbatim, and nothing else."

reset()
first = box("cycle.py", "1", "--system", "/dev/null", "--check-model", "haiku", "--prompt", HIRE,
            name="delegate-1")
for kind in ("worker_start", "worker_end"):
    print(kind, [json.dumps(e) for e in of(first, "cycle", kind)])
worker_init = next((e for a, e in first if a.startswith("worker:") and e.get("subtype") == "init"), {})
print("worker ran on:", worker_init.get("model"), "(role file said haiku)")

works = shell("cd /workspace && python3 product/fib.py && echo RUNS; "
              "python3 -c 'import sys; sys.path.insert(0, \"product\"); import fib; print(\"fib(30) =\", fib.fib(30))'; "
              "echo '--- report'; cat company/reports/c0001-01-fib.md")
print(works.strip()[:900])

second = box("cycle.py", "2", "--no-workers", "--system", "/dev/null", "--check-model", "haiku",
             "--prompt", READ, name="delegate-2")
report = works.split("--- report", 1)[-1]
first_line = next((line.strip() for line in report.splitlines() if line.strip()), "")
quoted = str(result(second, "orchestrator").get("result", "")).strip()
print("report's first line:", repr(first_line))
print("orchestrator quoted:", repr(quoted))

wrote_code = ("RUNS" in works and "fib(30) = 832040" in works
              and str(worker_init.get("model")).startswith("claude-haiku-4-5"))
read_back = bool(first_line) and first_line.strip("# *").lower() in quoted.lower()
print("cost (list-price $):", cost(first) + cost(second), "| usage after:", json.dumps(usage_reading(second)))
print(f"CHECK 4 {'PASS' if wrote_code and read_back else 'FAIL'}: worker wrote running code: {wrote_code}; "
      f"orchestrator read the report next cycle: {read_back}")
sys.exit(0 if wrote_code and read_back else 1)
