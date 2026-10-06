"""The front desk: where the person watching provides what the company asks for, or says no.

The company acts in the world only with the person's yes; it asks for what it cannot make itself, like an
account in the person's name or a key. What the person provides is written here, one file per item,
into desk/vault/ (read-only inside the box, and nowhere else), and the answer is a line in
desk/book/decisions.jsonl that names the items but never holds a value.

This server runs in its own container, on the window network only. The box is not on that network,
so no agent can reach it, and the book and the vault are read-only to the box: the company asks,
only the Mac answers. The office page at http://127.0.0.1:8771 sends the answers. A browser lets
another page post here too, so a request must come from the office's own address, as JSON, with the
X-Desk header; anything else is refused before it is read. The agents' own page lives on 8770, a
different address, so it cannot answer for them either.

An answer is bound to the exact text the person read: the page sends the text's fingerprint, and
the desk refuses if the file changed since it was asked or since the page showed it. What it may
provide is what that text's needs line names, and nothing else. Only an ask the head filed counts:
any worker can write asks.jsonl, so the proof is the head's own `ask` command and its reply in the
outside log, which the heartbeat writes on the Mac and no agent can touch.
"""
import datetime
import http.server
import json
import os
import pathlib
import sys
import threading
import time

sys.path.insert(0, "/opt/company/bin")
from company import (ASK_ID, ASKS, DECISIONS, ITEM_NAME, VAULT, WORKSPACE, filed_asks,  # noqa: E402
                     fingerprint, parse_ask, read_jsonl)

PORT = int(os.environ.get("PORT", "8772"))
ORIGINS = {"http://127.0.0.1:8771", "http://localhost:8771"}
HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
MAX_BODY = 64_000
MAX_VALUE = 16_000
WRITING = threading.Lock()
# The outside log, read-only here. filed_asks reads each cycle's stream on from where it stopped,
# kept in this one dict for the life of the server, so an answer costs only the new lines.
LOG = pathlib.Path(os.environ.get("LOG", "/log"))
FILED_CACHE = {}
READING = threading.Lock()


def read_request_file(name):
    """The text of a request file in the workspace, or None if it is not a file under company/."""
    path = (WORKSPACE / str(name or "")).resolve()
    if (WORKSPACE / "company").resolve() not in path.parents or not path.is_file():
        return None
    return path.read_text(errors="replace")


def stamp(now):
    return {"ts": round(now, 3), "at": datetime.datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds")}


def clean_items(items):
    """{name: value} with every name a vault name and every value a non-empty text, or a reason."""
    if not isinstance(items, dict) or len(items) > 10:
        return None, "items is a list of up to ten names and values"
    clean = {}
    for name, value in items.items():
        if not isinstance(name, str) or not ITEM_NAME.fullmatch(name):
            return None, f"{str(name)[:40]!r} is not a name like GUMROAD_ACCESS_TOKEN"
        if not isinstance(value, str) or not value.strip() or "\0" in value or len(value) > MAX_VALUE:
            return None, f"{name} needs a value under {MAX_VALUE // 1000} KB"
        clean[name] = value.strip()
    return clean, None


def asks_filed():
    """{ask id: request file} for every ask the outside log shows the head filing. Answers arrive on
    several threads at once, and the cache is one dict, so they take turns reading it."""
    with READING:
        return dict(filed_asks(LOG, FILED_CACHE))


def honoured(asked, filed):
    """{id: asks.jsonl entry} for the asks the head filed: the first entry for each id, and only when
    it names the file the head asked with. A later line for the same id is ignored, and a line with
    junk for an id is skipped, so no line a worker writes can stand in for the head's or stop the
    desk answering the rest."""
    first = {}
    for entry in asked:
        ident = entry.get("id")
        if isinstance(ident, str) and ident not in first:
            first[ident] = entry
    return {ident: entry for ident, entry in first.items()
            if ASK_ID.fullmatch(ident) and ident in filed and entry.get("file") == filed[ident]}


def answer(asked, filed, request, read, now):
    """(the line to record, the items to put in the vault, the request text that was verified, None)
    or (None, None, None, why it is refused). The text comes back so that the copy kept in the book
    is the very text whose fingerprint was checked, not a second read of a file the box can change.
    The person may answer a request again at any time and the latest answer counts."""
    decision, note = request.get("decision"), " ".join(str(request.get("note") or "").split())[:600]
    if decision == "withdraw":
        names = request.get("names")
        if not isinstance(names, list) or not names or not all(isinstance(n, str) and ITEM_NAME.fullmatch(n) for n in names):
            return None, None, None, "withdraw names the vault items to take back"
        return {"id": "vault", "decision": "withdrawn", "names": sorted(set(names)), "note": note, **stamp(now)}, {}, None, None
    # The id names the copy kept in the book, so it is checked before it is used for anything.
    ident = request.get("id")
    if not isinstance(ident, str) or not ASK_ID.fullmatch(ident):
        return None, None, None, "there is no such request"
    proven = honoured(asked, filed)
    entry = proven.get(ident)
    if entry is None:
        if any(e.get("id") == ident for e in asked):
            return None, None, None, "the log shows no ask from the head for this request, so it cannot be answered"
        return None, None, None, "there is no such request"
    if decision not in ("provide", "no"):
        return None, None, None, "the answer is provide, no or withdraw"
    replaced = {r for e in proven.values() if isinstance(e.get("replaces"), list) for r in e["replaces"] if isinstance(r, str)}
    if ident in replaced:
        return None, None, None, "the head asked again with a newer version; answer that one"
    text = read(entry.get("file"))
    if text is None:
        return None, None, None, "the request file is gone"
    sha = fingerprint(text)
    if sha != entry.get("sha256"):
        return None, None, None, "the request was edited after it was asked, so it cannot be answered; the head can ask again"
    if request.get("sha256") != sha:
        return None, None, None, "the request changed since this page showed it; read it again"
    # The title and the needs come from the text the person read, never from asks.jsonl, whose
    # other fields any worker can rewrite.
    try:
        title, needs, _ = parse_ask(text)
    except ValueError as error:
        return None, None, None, str(error)
    items = {}
    if decision == "provide":
        items, why = clean_items(request.get("items") or {})
        if why:
            return None, None, None, why
        stray = sorted(set(items) - set(needs))
        if stray:
            return None, None, None, (f"the request does not ask for {', '.join(stray)}; it asks for "
                                      + (", ".join(needs) or "an answer, not a thing"))
        if not items and not note:
            return None, None, None, "provide something, or write a note saying what you did"
    line = {"id": ident, "decision": "provided" if decision == "provide" else "no", "names": sorted(items),
            "note": note, "title": title, "file": entry.get("file"), "sha256": sha, **stamp(now)}
    return line, items, text, None


def discard(paths):
    for path in paths:
        try:
            path.unlink()
        except OSError:
            pass


def stage(items):
    """{name: temporary file} with each value written beside its place in the vault, or nothing at
    all: the vault itself does not change until the book names what goes into it."""
    VAULT.mkdir(parents=True, exist_ok=True)
    staged = {}
    try:
        for name, value in items.items():
            temporary = VAULT / f".{name}.tmp"
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            staged[name] = temporary
            with os.fdopen(fd, "w") as f:
                f.write(value)
    except BaseException:
        discard(staged.values())
        raise
    return staged


def write_line(line):
    with DECISIONS.open("a") as f:
        f.write(json.dumps(line) + "\n")
        f.flush()
        os.fsync(f.fileno())


def record(line, items, text):
    """Keep a copy of exactly what was read, then fill or empty the vault and append the answer.
    The book names what is in the vault; no value is ever written to it. Whatever fails part way,
    no value is left in the vault without a line naming it, or the person could neither see it nor
    take it back: the copy is written first, a new value waits beside the vault until its line is
    in the book, and a value taken back is gone before the line saying so is written."""
    with WRITING:
        if text is not None:
            copy = DECISIONS.parent / "asks" / f"{line['id']}.md"
            copy.parent.mkdir(parents=True, exist_ok=True)
            copy.write_text(text)
        if line["decision"] == "withdrawn":
            for name in line["names"]:
                try:
                    (VAULT / name).unlink()
                except FileNotFoundError:
                    pass
            write_line(line)
            return
        staged = stage(items)
        try:
            write_line(line)
            for name in list(staged):
                os.replace(staged[name], VAULT / name)
                del staged[name]
        finally:
            discard(staged.values())


class Handler(http.server.BaseHTTPRequestHandler):
    def trusted(self):
        return self.headers.get("Host") in HOSTS and self.headers.get("Origin") in ORIGINS

    def reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        if self.trusted():
            self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
        self.send_header("Vary", "Origin")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        if not self.trusted():
            self.reply(403, {"error": "not from the office"})
            return
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", self.headers["Origin"])
        self.send_header("Access-Control-Allow-Methods", "GET, POST")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Desk")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Vary", "Origin")
        self.end_headers()

    def do_GET(self):
        if self.path == "/health" and self.trusted():
            self.reply(200, {"ok": True})
        else:
            self.reply(404 if self.trusted() else 403, {"error": "nothing here"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if (self.path != "/decide" or not self.trusted() or self.headers.get("X-Desk") != "owner"
                or not str(self.headers.get("Content-Type", "")).startswith("application/json") or not 0 < length <= MAX_BODY):
            self.reply(403, {"error": "answers come from the office page only"})
            return
        try:
            request = json.loads(self.rfile.read(length))
            assert isinstance(request, dict)
        except (ValueError, AssertionError):
            self.reply(400, {"error": "not a JSON object"})
            return
        line, items, text, why = answer(read_jsonl(ASKS), asks_filed(), request, read_request_file, time.time())
        if why:
            self.reply(409, {"error": why})
            return
        record(line, items, text)
        print(json.dumps({"event": "answer", **{k: line.get(k) for k in ("id", "decision", "names")}}), flush=True)
        self.reply(200, {"ok": True, "answer": line})

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    DECISIONS.parent.mkdir(parents=True, exist_ok=True)
    VAULT.mkdir(parents=True, exist_ok=True)
    # A power cut between staging a value and writing its line leaves the value staged. It was never
    # recorded as provided, so it goes.
    discard(VAULT.glob(".*.tmp"))
    print(json.dumps({"event": "open", "port": PORT, "book": str(DECISIONS), "vault": str(VAULT), "log": str(LOG)}), flush=True)
    http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
