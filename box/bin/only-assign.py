#!/usr/bin/env python3
"""The orchestrator's PreToolUse hook: its only commands are `assign <role> <task file>` and
`ask <request file>`, each in plain form.

Claude Code runs commands it judges read-only, such as `ls` or `git status`, without a permission
rule, even in dontAsk mode (check 3 caught `ls` on 2026-09-30). They cannot write by themselves,
but a worker can plant a git config that makes `git status` start a program. So every Bash call
passes through here first, and anything that is not a bare assign or ask is denied.
"""
import json
import re
import sys

PLAIN = re.compile(r"assign [a-z0-9][a-z0-9-]{0,40} (/workspace/)?company/[A-Za-z0-9._/-]+( after \d+(,\d+)*)?")
ASK = re.compile(r"ask (/workspace/)?company/[A-Za-z0-9._/-]+")


def decide(event):
    """None to let the call through, or the reason it is denied."""
    if event.get("tool_name") != "Bash":
        return None
    command = str(event.get("tool_input", {}).get("command", "")).strip()
    if (PLAIN.fullmatch(command) or ASK.fullmatch(command)) and ".." not in command:
        return None
    return ("The only commands here are `assign <role> <task file under company/> [after <n>,<n>]` and "
            "`ask <request file under company/>`, each on its own.")


if __name__ == "__main__":
    reason = decide(json.load(sys.stdin))
    if reason:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                 "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}}))
