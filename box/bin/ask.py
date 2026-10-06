#!/usr/bin/env python3
"""ask <request file>: ask the person watching for something only they can provide.

The company acts in the world only with the person's yes. What it cannot make itself, it asks for here: an account
in the person's name, a key, a payout, a domain. Only the head of the company asks. A worker could
set the variable and ask too, but an ask only ever reaches the person, who reads it before acting,
so the worst a forged ask can do is take up their time.

The answer is written outside the box by the front desk: a line in /opt/company/desk/decisions.jsonl
and, for each thing provided, a file in /opt/company/vault/ named as the request's `needs` line
names it. Both are read-only in here. An answer is bound to the request's exact text: an edited
request is answered as nothing, so the head asks again, and the new ask replaces the old one.
"""
import json
import os
import pathlib
import sys
import time

from company import (ASK_ID, ASKS, COMPANY, DECISIONS, VAULT, WORKSPACE, answers, fingerprint, parse_ask,
                     read_jsonl, replaced_ids)


def fail(message):
    print(f"ask: {message}", file=sys.stderr)
    sys.exit(2)


def main(argv):
    if len(argv) != 1:
        fail("usage: ask <request file under company/>")
    if os.environ.get("COMPANY_ACTOR") != "orchestrator":
        fail("only the head of the company asks the person watching")
    path = (pathlib.Path.cwd() / argv[0]).resolve()
    if COMPANY.resolve() not in path.parents or not path.is_file():
        fail("the request must be an existing file under company/")
    name = str(path.relative_to(WORKSPACE))
    text = path.read_text(errors="replace")
    try:
        title, needs, _ = parse_ask(text)
    except ValueError as error:
        fail(f"{name}: {error}")

    asked, answered = read_jsonl(ASKS), answers(read_jsonl(DECISIONS))
    # An ask that a later one replaced can never be answered, so the same text asked again is a new
    # ask, not a repeat: otherwise a request edited and then put back would wait for ever.
    replaced = replaced_ids(asked)
    ids = [e["id"] for e in asked if isinstance(e.get("id"), str) and ASK_ID.fullmatch(e["id"])]
    sha = fingerprint(text)
    for entry in asked:
        if entry.get("sha256") == sha and entry.get("id") in ids and entry["id"] not in replaced:
            answer = answered.get(entry["id"])
            fail(f"this exact request is already {entry['id']}, "
                 + (f"answered {answer.get('decision')}" if answer else "still waiting for an answer"))
    entry = {
        "id": f"a{max((int(i[1:]) for i in ids), default=0) + 1}", "file": name, "title": title, "needs": needs,
        "sha256": sha,
        # An earlier ask of the same file that is still unanswered is replaced by this one.
        "replaces": [e["id"] for e in asked if e.get("file") == name and e.get("id") in ids and e["id"] not in answered],
        "cycle": int(os.environ.get("COMPANY_CYCLE", "0")),
        "asked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    ASKS.parent.mkdir(exist_ok=True)
    torn = ASKS.exists() and ASKS.stat().st_size > 0 and not ASKS.read_bytes().endswith(b"\n")
    with ASKS.open("a") as f:
        f.write(("\n" if torn else "") + json.dumps(entry) + "\n")
    wants = f" What they provide lands in {VAULT}/ as {', '.join(needs)}." if needs else ""
    replaced = f" It replaces {', '.join(entry['replaces'])}." if entry["replaces"] else ""
    print(f"asked {entry['id']}: {title}. It is on the person's desk.{wants}{replaced} Their answer arrives with "
          f"the first message of a later cycle and stays in {DECISIONS}; the vault fills as soon as they "
          f"provide. Leave {name} as it is: an edited request can no longer be answered, so change it only to "
          "ask again.")


if __name__ == "__main__":
    main(sys.argv[1:])
