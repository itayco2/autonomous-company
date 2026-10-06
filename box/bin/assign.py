#!/usr/bin/env python3
"""assign <role> <task file> [after <n>[,<n>...]]: hand one task to one worker.

The only command the orchestrator may run. It checks the role and the task, queues them for this
cycle, and writes the task's report file at once with the word Queued, so the orchestrator can see
every assignment's state from the reports alone. The cycle runs the queue after the orchestrator's
session ends, up to sixteen workers at a time, in the order assigned. `after 2,3` makes a task wait
until tasks #2 and #3 of this cycle have finished.
"""
import json
import os
import pathlib
import re
import sys
import time

from company import COMPANY, QUEUE, ROLE_NAME, WORKSPACE, parse_role, read_queue, write_atomic


def fail(message):
    print(f"assign: {message}", file=sys.stderr)
    sys.exit(2)


def main(argv):
    after = []
    if len(argv) == 4 and argv[2] == "after":
        if not re.fullmatch(r"\d+(,\d+)*", argv[3]):
            fail("after takes task numbers separated by commas, like: after 2,3")
        after = sorted({int(n) for n in argv[3].split(",")})
        argv = argv[:2]
    if len(argv) != 2:
        fail("usage: assign <role> <task file> [after <n>[,<n>...]]")
    role, task = argv
    if os.environ.get("COMPANY_ACTOR") != "orchestrator":
        fail("only the head of the company assigns work")
    if not ROLE_NAME.fullmatch(role):
        fail("a role name is lowercase letters, digits and dashes")
    role_file = COMPANY / "roles" / f"{role}.md"
    if not role_file.is_file():
        fail(f"there is no role file company/roles/{role}.md; write it first")
    try:
        model, effort, _ = parse_role(role_file.read_text(errors="replace"))
    except ValueError as error:
        fail(f"company/roles/{role}.md: {error}")
    task_path = (pathlib.Path.cwd() / task).resolve()
    if COMPANY.resolve() not in task_path.parents or not task_path.is_file():
        fail("the task file must be an existing file under company/")
    task_name = str(task_path.relative_to(WORKSPACE))

    cycle = int(os.environ.get("COMPANY_CYCLE", "0"))
    queued, _ = read_queue(QUEUE)
    for entry in queued:
        if entry["role"] == role and entry["task"] == task_name:
            fail(f"{role} <- {task_name} is already queued this cycle; its report is {entry['report']}")
    number = len(queued) + 1
    known = {entry.get("id") for entry in queued}
    missing = [n for n in after if n not in known]
    if missing:
        fail(f"after {','.join(map(str, missing))}: no such task queued this cycle; numbers go up to {number - 1}")
    runs_on = model + (f", effort {effort}" if effort else "")
    entry = {
        "id": number, "cycle": cycle, "role": role, "model": model, "effort": effort, "task": task_name, "after": after,
        # Cycle and number in the name: re-assigning a task never overwrites an earlier report.
        "report": f"company/reports/c{cycle:04d}-{number:02d}-{task_path.stem}.md",
        "queued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    QUEUE.parent.mkdir(exist_ok=True)
    torn = QUEUE.exists() and not QUEUE.read_bytes().endswith(b"\n") and QUEUE.stat().st_size > 0
    with QUEUE.open("a") as f:
        f.write(("\n" if torn else "") + json.dumps(entry) + "\n")
    waits = f", after #{', #'.join(map(str, after))}" if after else ""
    write_atomic(WORKSPACE / entry["report"],
                 f"Queued in cycle {cycle} for {role} ({runs_on}{waits}). It has not started yet.\n")
    print(f"queued #{number}: {role} ({runs_on}) <- {task_name}{waits}. It starts after this session ends; "
          f"the report will be at {entry['report']}.")


if __name__ == "__main__":
    main(sys.argv[1:])
