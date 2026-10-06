"""Check 3b: the orchestrator chooses each worker's model and effort, and only free models can run.

Run on the Mac:  python3 checks/roles.py        (no token: every step runs with it blanked)
1. `assign` accepts valid headers and refuses Fable, bad headers, an effort for Haiku, a smuggled
   flag and a duplicate; each accepted task gets a report saying Queued at once.
2. A role rewritten to Fable after it was queued is refused when the worker would start.
3. A queue left by a cut-off cycle is dropped at the next cycle, and its reports say so.
4. Below all of that, the CLI itself: with the managed policy in /etc, a claude process asked for
   Fable gets Opus 5.5 instead, and the agent user cannot edit the policy.
5. The front desk from inside: `ask` takes a well-formed request from the head only, and refuses a
   repeat and a need that is not a vault name.
6. The book of answers and the vault are mounted read-only in the box, and a value the front desk
   puts in the vault, as root and from its own container, can be read by the agent user.

A worker's `assign` and `ask` get an input that only the actor gate refuses, and the head then runs
the very same input, which must be accepted: a refusal for any other reason cannot pass for the gate.
Steps 5 and 6 run on an empty scratch book and vault (boxrun.scratch_desk), never the owner's.
"""
import secrets
import sys

from boxrun import desk, reset, shell

SCRIPT = r"""
cd /workspace && mkdir -p company/roles company/tasks && echo "Do it." > company/tasks/t.md
role() { printf -- "$2" > company/roles/$1.md; }
role good         '---\nmodel: sonnet\neffort: high\n---\nYou build.\n'
role full-id      '---\nmodel: claude-opus-5-5\neffort: max\n---\nYou think.\n'
role quick        '---\nmodel: haiku\neffort: none\n---\nYou are quick.\n'
role fable        '---\nmodel: fable\neffort: high\n---\nYou cost money.\n'
role fable-id     '---\nmodel: claude-fable-5-1\neffort: high\n---\nYou cost money.\n'
role no-header    'You have no header.\n'
role bad-effort   '---\nmodel: sonnet\neffort: ultra\n---\nYou try too hard.\n'
role haiku-effort '---\nmodel: haiku\neffort: max\n---\nYou pretend.\n'
role flag         '---\nmodel: sonnet --dangerously-skip-permissions\neffort: high\n---\nYou inject.\n'
export COMPANY_ACTOR=orchestrator COMPANY_CYCLE=1
for r in good full-id quick fable fable-id no-header bad-effort haiku-effort flag good; do
  if out=$(assign $r company/tasks/t.md 2>&1); then echo "ACCEPT $r | $out"; else echo "REFUSE $r | $(echo "$out" | head -1)"; fi
done
echo "STUB $(head -1 company/reports/c0001-01-t.md)"
echo "Wait." > company/tasks/w.md; echo "Wait more." > company/tasks/w2.md
assign quick company/tasks/w.md after 1,2 >/dev/null 2>&1 && echo "AFTER-OK accepted" || echo "AFTER-OK refused"
assign quick company/tasks/w2.md after 9 >/dev/null 2>&1 && echo "AFTER-UNKNOWN accepted" || echo "AFTER-UNKNOWN refused"
echo "AFTER-STORED $(grep '"task": "company/tasks/w.md"' .box/queue.jsonl | python3 -c 'import json,sys; print(json.load(sys.stdin)["after"])')"
echo "Fresh." > company/tasks/w3.md
if out=$(COMPANY_ACTOR=worker assign quick company/tasks/w3.md 2>&1); then echo "WORKER-ASSIGN accepted"; else echo "WORKER-ASSIGN refused | $out"; fi
assign quick company/tasks/w3.md >/dev/null 2>&1 && echo "WORKER-ASSIGN-CONTROL accepted" || echo "WORKER-ASSIGN-CONTROL refused"
printf -- '---\nmodel: fable\neffort: high\n---\nRewritten by a worker.\n' > company/roles/good.md
cd /opt/company/bin && python3 - <<'EOF'
import json, cycle
from company import QUEUE, read_queue
entries, _ = read_queue(QUEUE)
good = next(e for e in entries if e["role"] == "good")
print("RUNTIME good ->", cycle.run_worker(good, 1))
cycle.drop_leftovers(2)
print("DROPPED queue exists after drop:", QUEUE.exists())
EOF
cd /workspace
echo "REPORT-RUNTIME $(head -1 company/reports/c0001-01-t.md)"
echo "REPORT-DROPPED $(head -1 company/reports/c0001-02-t.md)"
model=$(claude -p x --model claude-fable-5-1 --output-format stream-json --verbose </dev/null 2>/dev/null \
  | python3 -c 'import sys,json; print(next((json.loads(l).get("model") for l in sys.stdin if "\"init\"" in l), None))')
echo "POLICY fable request runs as: $model"
mkdir -p company/asks
printf -- '---\ntitle: A store\nneeds: STORE_URL\n---\nA store in your name.\n' > company/asks/store.md
printf -- '---\ntitle: Everything\nneeds: ../../etc/x\n---\nAll of it.\n' > company/asks/bad.md
ask company/asks/store.md >/dev/null 2>&1 && echo "ASK-OK accepted" || echo "ASK-OK refused"
ask company/asks/store.md >/dev/null 2>&1 && echo "ASK-REPEAT accepted" || echo "ASK-REPEAT refused"
ask company/asks/bad.md >/dev/null 2>&1 && echo "ASK-BADNAME accepted" || echo "ASK-BADNAME refused"
printf -- '---\ntitle: A domain\nneeds: DOMAIN_NAME\n---\nA domain in your name.\n' > company/asks/other.md
if out=$(COMPANY_ACTOR=worker ask company/asks/other.md 2>&1); then echo "WORKER-ASK accepted"; else echo "WORKER-ASK refused | $out"; fi
ask company/asks/other.md >/dev/null 2>&1 && echo "WORKER-ASK-CONTROL accepted" || echo "WORKER-ASK-CONTROL refused"
echo "ASK-STORED $(python3 -c 'import json; print([(e["id"], e["needs"]) for e in map(json.loads, open(".box/asks.jsonl"))])')"
echo "MOUNTS $(awk '$2 == "/opt/company/desk" || $2 == "/opt/company/vault" {split($4, o, ","); print $2 "=" o[1]}' /proc/mounts | sort | tr '\n' ' ')"
echo "VAULT-READ as $(id -un): $(cat /opt/company/vault/CHECK_ITEM 2>&1 | head -c 200)"
( echo '{}' >> /opt/company/desk/decisions.jsonl ) 2>/dev/null && echo "BOOK writable" || echo "BOOK read-only"
( echo x > /opt/company/vault/FORGED ) 2>/dev/null && echo "VAULT writable" || echo "VAULT read-only"
echo "POLICY owner: $(stat -c %U /etc/claude-code/managed-settings.json)"
( echo '{}' > /etc/claude-code/managed-settings.json ) 2>/dev/null && echo "POLICY writable" || echo "POLICY read-only"
"""

EXPECT = {"good": "ACCEPT", "full-id": "ACCEPT", "quick": "ACCEPT", "fable": "REFUSE", "fable-id": "REFUSE",
          "no-header": "REFUSE", "bad-effort": "REFUSE", "haiku-effort": "REFUSE", "flag": "REFUSE"}
MUST_CONTAIN = [
    ("REFUSE good | assign: good <- company/tasks/t.md is already queued", "a duplicate assignment was queued"),
    ("STUB Queued in cycle 1 for good", "an accepted task had no Queued report"),
    ("WORKER-ASSIGN refused", "assign queued work for a worker: the head's own tool must never assign on a worker's behalf"),
    ("WORKER-ASSIGN-CONTROL accepted", "the head could not assign what the worker tried, so the worker's refusal "
                                       "proves nothing about the actor gate"),
    ("AFTER-OK accepted", "a task could not wait for earlier tasks"),
    ("AFTER-UNKNOWN refused", "a task could wait for a task that does not exist"),
    ("AFTER-STORED [1, 2]", "the waits were not recorded in the queue"),
    ("RUNTIME good -> None", "the cycle ran a role that had been rewritten to Fable"),
    ("REPORT-RUNTIME Not run: model 'fable'", "no report told the orchestrator why the task did not run"),
    ("DROPPED queue exists after drop: False", "a cut-off cycle's queue survived into the next cycle"),
    ("REPORT-DROPPED Not run: this computer stopped", "a dropped task's report did not say so"),
    ("POLICY fable request runs as: claude-opus-5-5", "the CLI in the box would run Fable"),
    ("POLICY owner: root", "the model policy is not owned by root"),
    ("POLICY read-only", "the agent can rewrite the model policy"),
    ("ASK-OK accepted", "the head could not put a request on the desk"),
    ("ASK-REPEAT refused", "the same request could be asked twice"),
    ("ASK-BADNAME refused", "a request could name a need that is not a vault name"),
    ("WORKER-ASK refused", "ask filed a request for a worker: the head's own tool must never ask on a worker's behalf"),
    ("WORKER-ASK-CONTROL accepted", "the head could not ask what the worker tried, so the worker's refusal "
                                    "proves nothing about the actor gate"),
    ("ASK-STORED [('a1', ['STORE_URL']), ('a2', ['DOMAIN_NAME'])]",
     "the requests were not recorded as a1 and a2 with what they need"),
    ("MOUNTS /opt/company/desk=ro /opt/company/vault=ro", "the book or the vault is not mounted read-only in the box"),
    ("PLANTED by the desk", "the front desk could not put a value in the scratch vault"),
    ("BOOK read-only", "an agent could write its own answer into the book"),
    ("VAULT read-only", "an agent could plant or replace a key in the vault"),
]

# A value planted the way the front desk provides one: desk/server.py's own record, which stages the
# value, writes the book line and only then puts it in the vault, as root, in the desk's own container.
# The box reads the scratch book and vault, so the value is new every run; its line has an id no ask
# can have, so it never stands for an answer to one.
PLANT = """
import os
import time
import server
line = {"id": "check", "decision": "provided", "names": ["CHECK_ITEM"], "note": "check 3b", **server.stamp(time.time())}
server.record(line, {"CHECK_ITEM": os.environ["CHECK_VALUE"]}, None)
made = os.stat(server.VAULT / "CHECK_ITEM")
print(f"PLANTED by the desk as uid {os.getuid()}: owner {made.st_uid}, mode {made.st_mode & 0o777:o}")
"""
WITHDRAW = """
import time
import server
server.record({"id": "vault", "decision": "withdrawn", "names": ["CHECK_ITEM"], "note": "check 3b",
               **server.stamp(time.time())}, {}, None)
"""


def failures(out, value):
    """Why the check fails, from everything the desk and the box printed; empty when it passes."""
    verdicts = {}
    for line in out.splitlines():
        if line.startswith(("ACCEPT", "REFUSE")):
            verdicts.setdefault(line.split()[1], line.split()[0])  # first verdict per role; the duplicate is the 2nd "good"
    found = [f"{r}: {verdicts.get(r)} (wanted {want})" for r, want in EXPECT.items() if verdicts.get(r) != want]
    readable = (f"VAULT-READ as agent: {value}", "an agent cannot read a value the front desk put in the vault, "
                "so every key the person provides would be useless")
    found += [why for text, why in MUST_CONTAIN + [readable] if text not in out]
    return found


def main():
    value = f"check-{secrets.token_hex(8)}"
    reset()
    out = desk(PLANT, CHECK_VALUE=value)
    try:
        out += shell(SCRIPT)
    finally:
        desk(WITHDRAW)
    print(out.strip())
    found = failures(out, value)
    print(f"CHECK 3b {'PASS' if not found else 'FAIL'}: " + ("; ".join(found) or
          "the orchestrator's model choices hold at assign, at run time and in the CLI itself; only free models run; "
          "only the head asks or assigns, only the desk answers or fills the vault, and the agents can read "
          "what it provides"))
    sys.exit(0 if not found else 1)


if __name__ == "__main__":
    main()
