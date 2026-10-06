"""The company's rules, shared by assign.py and cycle.py: what it may run on, how a role file says
it, and how the queue and the reports are written so that a power cut at any instant is harmless.

The orchestrator is fixed. Every worker runs on whatever its role file names, and the head of the
company writes that file, so choosing a worker's model and effort is part of hiring. The same
three models are the allowlist in /etc/claude-code/managed-settings.json, which every claude
process in the box obeys; this module is where the orchestrator hears about a bad choice.
Fable is left out on purpose, because in headless mode it is billed to usage credits without asking.

The front desk lives here too. The company acts in the world only with the person watching's yes; it asks them
for what only they can provide (an account in their name, a key, a payout), and
what they provide arrives in a vault outside the box, read-only in here. The press space has a
budget the person set.
"""

import hashlib
import json
import os
import pathlib
import posixpath
import re

WORKSPACE = pathlib.Path("/workspace")
COMPANY = WORKSPACE / "company"
REPORTS = COMPANY / "reports"
BOX = WORKSPACE / ".box"
QUEUE = BOX / "queue.jsonl"
ROLE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")
ASKS = BOX / "asks.jsonl"
# Written only by the front desk, outside the box; mounted read-only here, so no agent can answer itself.
DESK = pathlib.Path("/opt/company/desk")
DECISIONS = DESK / "decisions.jsonl"
# What the person provided, one file per item, named as asked. Read-only here; never leaves the box
# except to the service it belongs to.
VAULT = pathlib.Path("/opt/company/vault")
ITEM_NAME = re.compile(r"[A-Z][A-Z0-9_]{1,47}")
ASK_ID = re.compile(r"a[0-9]{1,6}")
# The company's budget: none unless the owner writes one into this file, in the book outside the box;
# purchases are "bought" lines in the book.
BUDGET = DESK / "budget.json"
BUDGET_DEFAULT = {"start": 0.0, "share_of_sales": 0.0, "monthly_cap": 0.0, "credits": []}
COST = re.compile(r"([0-9]{1,5}(?:\.[0-9]{1,2})?) ?USD")
FILED = re.compile(r"asked (a[0-9]{1,6}): ")
# The owner's budget for press material (pictures, clips and drafts about the company): the press
# folder, its copy in the person's view, and a working folder for it, together.
PRESS_SPACE = (WORKSPACE / "press", WORKSPACE / "view" / "press", COMPANY / "work" / "chronicler")
PRESS_CAP = 1_000_000_000

ORCHESTRATOR_MODEL = "claude-opus-5-5"
ORCHESTRATOR_EFFORT = "max"
# Configurable choice: the head runs as many workers side by side as it wants, up to
# sixteen at the same moment. A Claude session takes about 150 MB; a browser or a big build in it
# adds 300-500 MB, so sixteen needs the box's 10 GB and Docker Desktop given 12 GB or more.
MAX_TOGETHER = 16
MCP_CONFIG = COMPANY / "mcp.json"

MODELS = {
    "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-4-5",
}
LEVELS = ("low", "medium", "high", "xhigh", "max")
# Haiku 4.5 takes no effort setting: the CLI drops one silently, so a role must not pretend to set it.
EFFORTS = {"claude-opus-5-5": LEVELS, "claude-sonnet-5-5": LEVELS, "claude-haiku-4-5": ("none",)}
MAX_ROLE_BYTES = 32_000  # the role text travels as one command-line argument

HEADER_HELP = ("a role file starts with a header like this:\n---\nmodel: sonnet\neffort: high\n---\n"
               f"model is one of {', '.join(MODELS)}; effort is one of {', '.join(LEVELS)} "
               "for opus and sonnet, and none for haiku")


MAX_ASK_BYTES = 20_000
MAX_NEEDS = 10
ASK_HELP = ("a request starts with a header like this:\n---\ntitle: A store to sell our first product\n"
            "needs: STORE_ACCESS_TOKEN, STORE_URL\n---\nthen what you need, exactly how to make it "
            "(the site, the settings, the permissions a key needs), and what you will do with it. needs "
            "names each thing you want back, in capitals; leave it out when you need an answer, not a thing")


def split_header(text, what, help_text, limit):
    """(fields, body) from a file that opens with a --- header, or ValueError saying what is wrong."""
    if "\0" in text or len(text.encode()) > limit:
        raise ValueError(f"a {what} must be plain text under {limit // 1000} KB")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---" or "---" not in (l.strip() for l in lines[1:]):
        raise ValueError(f"the {what} has no header; {help_text}")
    end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    fields = {}
    for line in lines[1:end]:
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip().lower()] = value.strip()
    return fields, "\n".join(lines[end + 1:]).strip()


def parse_role(text):
    """(model id, effort or None, instructions) from a role file, or ValueError saying what is wrong."""
    fields, instructions = split_header(text, "role file", HEADER_HELP, MAX_ROLE_BYTES)
    model = fields.get("model", "").lower()
    model = MODELS.get(model, model)
    if model not in EFFORTS:
        raise ValueError(f"model {fields.get('model')!r} is not available; {HEADER_HELP}")
    effort = fields.get("effort", "none" if model == MODELS["haiku"] else "").lower()
    if effort not in EFFORTS[model]:
        raise ValueError(f"effort {fields.get('effort')!r} is not available for {model}; {HEADER_HELP}")
    return model, (None if effort == "none" else effort), instructions


def parse_ask(text):
    """(title, needs, body) from a request for the front desk, or ValueError saying what is wrong.
    Each need is the name the provided thing will have in the vault, like STORE_ACCESS_TOKEN."""
    fields, body = split_header(text, "request file", ASK_HELP, MAX_ASK_BYTES)
    title = fields.get("title", "")
    if not title or len(title) > 160:
        raise ValueError(f"the title is missing or longer than 160 characters; {ASK_HELP}")
    needs = sorted({n.strip() for n in fields.get("needs", "").split(",") if n.strip()})
    bad = [n for n in needs if not ITEM_NAME.fullmatch(n)]
    if bad:
        raise ValueError(f"needs takes names in capitals, digits and underscores, like STORE_ACCESS_TOKEN, "
                         f"not {', '.join(bad)}")
    if len(needs) > MAX_NEEDS:
        raise ValueError(f"needs names at most {MAX_NEEDS} things")
    if not body:
        raise ValueError("the request says nothing after its header: write what you need, how to make it, "
                         "and what you will do with it")
    return title, needs, body


def ask_cost(text):
    """The price a request asks the person to pay, in US dollars, or None for a request that buys
    nothing. A purchase says it in its header: `cost: 12 USD`."""
    fields, _ = split_header(text, "request file", ASK_HELP, MAX_ASK_BYTES)
    if "cost" not in fields:
        return None
    match = COST.fullmatch(fields["cost"].strip())
    if not match or float(match.group(1)) <= 0:
        raise ValueError("cost takes a price in US dollars, like `cost: 12 USD` or `cost: 9.99 USD`")
    return round(float(match.group(1)), 2)


def budget(config, decisions):
    """(left, spent, credited) in US dollars: the start, plus what sales have credited, minus every
    purchase the book records as bought. A malformed number counts as nothing, never as money."""
    def number(value):
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0 else 0.0
    config = config if isinstance(config, dict) else {}
    credits = config.get("credits") if isinstance(config.get("credits"), list) else []
    credited = sum(number(c.get("amount")) for c in credits if isinstance(c, dict))
    spent = sum(number(d.get("spent")) for d in decisions if isinstance(d, dict) and d.get("decision") == "bought")
    return round(number(config.get("start", BUDGET_DEFAULT["start"])) + credited - spent, 2), round(spent, 2), round(credited, 2)


def read_budget(path=None):
    """The owner's budget file, or the default when it is missing or unreadable."""
    try:
        config = json.loads((path or BUDGET).read_text())
    except (OSError, ValueError, RecursionError):
        return dict(BUDGET_DEFAULT)
    return config if isinstance(config, dict) else dict(BUDGET_DEFAULT)


def fingerprint(text):
    """What the person read and answered is bound to this: an edited request is a different request."""
    return hashlib.sha256(text.encode()).hexdigest()


def read_jsonl(path):
    """Every whole JSON object in a file of lines. A line torn by a power cut is skipped, never fatal."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    rows = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):  # nested past Python's limit is as unreadable as torn
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def answers(decisions):
    """The person's latest answer to each request, by request id: a later line overrides an earlier one."""
    return {d["id"]: d for d in decisions if isinstance(d.get("id"), str)}


def replaced_ids(asked):
    """Every ask id that a later ask of the same file replaced. The desk never answers one of these."""
    return {r for e in asked if isinstance(e.get("replaces"), list) for r in e["replaces"] if isinstance(r, str)}


def space_used(paths):
    """Bytes in files under the given folders, counting each file once and following no links."""
    total, seen = 0, set()
    for root in paths:
        for folder, _, names in os.walk(root):
            for name in names:
                try:
                    info = os.lstat(os.path.join(folder, name))
                except OSError:
                    continue
                if (info.st_dev, info.st_ino) not in seen:
                    seen.add((info.st_dev, info.st_ino))
                    total += info.st_blocks * 512 if hasattr(info, "st_blocks") else info.st_size
    return total


def filed_name(typed):
    """The request file as ask.py records it, from the path the head typed: `company/./asks/x.md`,
    `company//asks/x.md` and `/workspace/company/asks/x.md` all name company/asks/x.md. The head
    runs in /workspace, and its hook lets through no `..`."""
    name = posixpath.normpath(typed)
    return name[len("/workspace/"):] if name.startswith("/workspace/") else name


def filed_asks(log_dir, cache):
    """{ask id: request file} for every ask the head filed, read from the outside log.

    The proof is the head's own `ask` command and its "asked aN: ..." reply in log/cycles/*.jsonl,
    which the harness writes on the Mac, outside the box. asks.jsonl alone proves nothing: any
    worker can write it. `cache` is a dict the caller keeps between calls; each stream is read on
    from where the last call stopped, whole lines only, so a poll costs only the new lines."""
    found = cache.setdefault("found", {})
    offsets = cache.setdefault("offsets", {})
    pending = cache.setdefault("pending", {})  # the head's tool_use id -> the request file it asked
    for path in sorted(pathlib.Path(log_dir).glob("cycles/*.jsonl")):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        start = offsets.get(str(path), 0)
        if size < start:  # replaced, not appended to: read it again
            start = 0
        if size == start:
            continue
        with path.open("rb") as f:
            f.seek(start)
            chunk = f.read(size - start)
        whole = chunk.rfind(b"\n") + 1
        offsets[str(path)] = start + whole
        for raw in chunk[:whole].splitlines():
            if b'"orchestrator"' not in raw or (b'"ask ' not in raw and b"asked a" not in raw):
                continue
            try:
                row = json.loads(raw)
            except (ValueError, RecursionError):  # the offset has moved on, so one bad line must not stop the rest
                continue
            message = (row.get("event") or {}).get("message") if isinstance(row, dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            if row.get("actor") != "orchestrator" or not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and block.get("name") == "Bash":
                    command = str((block.get("input") or {}).get("command", "")).strip()
                    if command.startswith("ask "):
                        pending[block.get("id")] = filed_name(command.split(None, 1)[1])
                elif block.get("type") == "tool_result" and not block.get("is_error"):
                    text = block.get("content")
                    if isinstance(text, list):
                        text = " ".join(str(c.get("text", "")) for c in text if isinstance(c, dict))
                    match = FILED.match(str(text or "").strip())
                    if match and block.get("tool_use_id") in pending:
                        found[match.group(1)] = pending.pop(block["tool_use_id"])
    return found


def write_atomic(path, text):
    """Write through a temporary file and a rename, so a power cut leaves the old text or the new."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text)
    os.replace(temporary, path)


def read_queue(path):
    """(entries, unreadable line count). A line torn by a power cut is skipped, never fatal."""
    entries, unreadable = [], 0
    if path.exists():
        for line in path.read_text(errors="replace").splitlines():
            try:
                entry = json.loads(line)
            except (ValueError, RecursionError):
                entry = None
            if isinstance(entry, dict) and all(isinstance(entry.get(k), str) for k in ("role", "task", "report")):
                entries.append(entry)
            elif line.strip():
                unreadable += 1
    return entries, unreadable


def ready_to_start(pending, finished, running, cap=MAX_TOGETHER):
    """Which pending entries may start now: in the order assigned, each only once every task it waits
    for has finished (whatever the outcome), and never more than `cap` running at once. A wait on a
    number that is not in this cycle's queue is ignored rather than blocking forever."""
    queued = {e.get("id") for e in pending} | set(finished) | set(running)
    ready = []
    for entry in pending:
        if len(running) + len(ready) >= cap:
            break
        waits = [n for n in entry.get("after") or [] if n in queued]
        if all(n in finished for n in waits):
            ready.append(entry)
    return ready


def report_path(entry):
    """The entry's report file, only if it is inside company/reports/; None for anything else."""
    path = (WORKSPACE / entry.get("report", "")).resolve()
    return path if REPORTS.resolve() in path.parents else None
