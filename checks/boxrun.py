"""Shared by the checks that call the model: run things in a fresh box and read the event stream.

Every check runs on the scratch volume autonomous-company_checks, never on the company's workspace, and
on an empty book and vault of its own in log/checks/, never on the owner's desk/book and desk/vault.
Streams are saved raw to log/checks/<name>.jsonl, so every number in a result can be re-read.
"""
import json
import os
import pathlib
import shutil
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOG = ROOT / "log" / "checks"
os.environ.setdefault("WORKSPACE_VOLUME", "autonomous-company_checks")


def scratch_desk(folder, environ):
    """Point DESK_BOOK and DESK_VAULT (compose.yaml) at empty folders under `folder`, unless the
    caller chose them already. A broken read-only mount then writes into scratch, never into the
    owner's book, and no check ever sees a value the owner provided. The vault is 0700, like the owner's."""
    for variable, name, mode in (("DESK_BOOK", "desk-book", 0o755), ("DESK_VAULT", "desk-vault", 0o700)):
        if variable in environ:
            continue
        path = pathlib.Path(folder) / name
        shutil.rmtree(path, ignore_errors=True)
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(mode)
        environ[variable] = str(path)


scratch_desk(LOG, os.environ)


def box(*command, name, timeout=900):
    """Run `command` in a fresh box, save its stdout to log/checks/<name>.jsonl, return the events."""
    LOG.mkdir(parents=True, exist_ok=True)
    out = LOG / f"{name}.jsonl"
    with out.open("w") as stdout, (LOG / f"{name}.err").open("w") as stderr:
        subprocess.run(["docker", "compose", "run", "--rm", "-T", "box", *command], cwd=ROOT,
                       stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, timeout=timeout)
    return read(out)


def shell(script):
    """Run a bash snippet in a fresh box and return its stdout. The token is blanked: setup and
    inspection never need the model, so a mistake here can never spend anything."""
    done = subprocess.run(["docker", "compose", "run", "--rm", "-T", "-e", "CLAUDE_CODE_OAUTH_TOKEN=", "box",
                           "bash", "-c", script], cwd=ROOT,
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300)
    return done.stdout


def desk(code, **env):
    """Run a Python snippet in a fresh front desk container and return what it printed. desk/server.py
    imports as `server`, and the snippet writes the scratch book and vault exactly as the real desk
    does: as root, from outside the box. The desk's port is not published, so the real one is untouched."""
    flags = [part for name, value in env.items() for part in ("-e", f"{name}={value}")]
    done = subprocess.run(["docker", "compose", "run", "--rm", "--no-deps", "-T", *flags, "desk", "python3", "-c",
                           "import sys\nsys.path.insert(0, '/desk')\n" + code], cwd=ROOT,
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300)
    return done.stdout + (done.stderr if done.returncode else "")


def reset():
    """Empty the scratch workspace."""
    shell("find /workspace -mindepth 1 -delete")


def read(path):
    """Events from a saved stream. cycle.py wraps each one as {actor, event}; bare claude does not."""
    events = []
    for line in pathlib.Path(path).read_text().splitlines():
        if not line.startswith("{"):
            continue
        row = json.loads(line)
        events.append((row["actor"], row["event"]) if "actor" in row else ("claude", row))
    return events


def of(events, actor=None, kind=None):
    return [e for a, e in events if (actor is None or a == actor) and (kind is None or e.get("type") == kind)]


def result(events, actor=None):
    results = of(events, actor, "result")
    return results[-1] if results else {}


def tool_calls(events, actor=None):
    """(tool name, input, is_error, output) for every tool call, in order."""
    outcomes = {}
    for message in of(events, actor, "user"):
        for block in message.get("message", {}).get("content", []):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                content = block.get("content")
                if isinstance(content, list):
                    content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
                outcomes[block["tool_use_id"]] = (bool(block.get("is_error")), str(content)[:240])
    calls = []
    for message in of(events, actor, "assistant"):
        for block in message.get("message", {}).get("content", []):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                is_error, output = outcomes.get(block["id"], (None, "no result"))
                calls.append((block["name"], block["input"], is_error, output))
    return calls


def usage_reading(events):
    """The last rate-limit reading in a stream: what the heartbeat will see after a cycle."""
    readings = [e.get("rate_limit_info", {}) for e in of(events, kind="rate_limit_event")]
    return readings[-1] if readings else None


def cost(events):
    """API-list-price dollars across every session in the stream (orchestrator and workers)."""
    return round(sum(r.get("total_cost_usd") or 0 for r in of(events, kind="result")), 4)
