"""The live office's server: reads the outside log and the workspace, both read-only, and serves one
snapshot of who is doing what, right now, at http://127.0.0.1:8771/state.json, plus the office page.

Everything shown comes from what happened: the cycle's event stream (log/cycles/NNNN.jsonl, or the
founding cycle's log/checks/calibrate-N.jsonl), the reports and role files in the workspace, the
door's log and the heartbeat's log. Nothing here is written by the agent, and nothing is invented.
The agent's own words reach the page only as data; the page renders them as text, never as markup.

Some things here come from the company on purpose: its requests to the person watching (the front
desk, shown with the answers from desk/book outside the box), the building's look
(company/look.json) and the sales it reports (company/ledger.json). Each is cleaned to a fixed list
of things before it leaves this server: the page that shows them also carries the form where the
person provides keys, so nothing the company writes may ever become markup or code on it. The vault
itself is never read here; the office knows only the names of what was provided.

Every file the company can write is read as if it were hostile: each field is type-checked, and each
part of the snapshot is built on its own, so a broken file costs its own part of the page and never
the page itself. The front desk above all must stay up, since it is where keys are taken back.
"""
import calendar
import collections
import glob
import hashlib
import http.server
import json
import os
import pathlib
import re
import sys
import threading
import time

# The box's own rules, read-only here: which asks the Head really filed, and what a request says.
sys.path.insert(0, "/opt/company/bin")
from company import ASK_ID, FILED, filed_asks, fingerprint, parse_ask  # noqa: E402

LOG = pathlib.Path(os.environ.get("LOG", "/log"))
WORKSPACE = pathlib.Path(os.environ.get("WORKSPACE", "/w"))
PAGE = pathlib.Path(__file__).with_name("office.html")
DESK = pathlib.Path(os.environ.get("DESK", "/desk"))
PLACES = pathlib.Path(__file__).with_name("places.txt")
URL_HOST = re.compile(r"https?://([a-z0-9][a-z0-9.-]*\.[a-z]{2,})", re.I)
KIND = {"payments": "payments", "store": "a store", "social": "social", "publishing": "publishing", "blog": "a blog",
        "email": "email", "hosting": "hosting"}


def load_places():
    """host -> kind, from places.txt: where the company reaching a place is worth a line in the story."""
    places, kind = {}, None
    for line in read_text(PLACES, 50_000).splitlines():
        line = line.split("#")[0].strip()
        if line.startswith("[") and line.endswith("]"):
            kind = line[1:-1]
        elif line and kind:
            places[line.lower()] = kind
    return places


def place_of(host, places):
    """(the listed site, its kind) for a host or any parent of it, else (None, None)."""
    host = (host or "").lower().rstrip(".")
    parts = host.split(".")
    for i in range(len(parts) - 1):
        site = ".".join(parts[i:])
        if site in places:
            return site, places[site]
    return None, None


PORT = int(os.environ.get("PORT", "8771"))
# The names this office is reached by: from the Mac, and from inside the box. A page elsewhere that
# rebinds its own name to 127.0.0.1 still sends its own name, and gets nothing.
HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"office:{PORT}"}
CHUNK = 8_000_000  # a long stream is read this much at a time, never whole
DOOR_START = 32_000_000  # a fresh start reads the door's log from this far before its end
REACH_WINDOW = 2 * 3600
TURNED_WINDOW = 24 * 3600
FAILED = {}  # part of the snapshot -> the last error it logged, so a broken file logs once, not every second


def section(name, build, fallback):
    """One part of the snapshot. If a file the company wrote breaks it, that part falls back to empty
    and the error is logged once; the rest of the office, the front desk above all, still answers."""
    try:
        value = build()
    except Exception as error:  # RecursionError too: deeply nested JSON is one way to break a part
        said = f"{type(error).__name__}: {error}"[:300]
        if FAILED.get(name) != said:
            FAILED[name] = said
            print(json.dumps({"event": "part_failed", "part": name, "error": said}), flush=True)
        return fallback
    FAILED.pop(name, None)
    return value


def size(n):
    """420 MB, 1 GB, 1.2 GB: the way a person reads a disk, as cycle.py says it."""
    return f"{n / 1e9:.1f} GB".replace(".0 GB", " GB") if n >= 1e9 else f"{n / 1e6:,.0f} MB"


NAMES = """Ada Noa Kenji Maya Tom Iris Omar Lena Yuki Sam Leo Nia Theo Zara Ben Mira Ravi Ella Finn Aya
Dan Lior Ines Kai Rosa Jin Tali Hugo Anya Eli Sofia Arun Nora Luca Hana Idan Vera Milo Amara Yoni
Lea Cy Juno Rafa Esme Tomer Ivy Nico Priya Gil Asha Otto Keren Diego Mina Jonah Lila Kofi Dina Emil
Ruth Tariq Nell Ari Sana Bruno Chloe Uri Wren Malik Ofir Greta Jude Amir Lucy Sven Maia Raz Pia
Joel Nadia Oren Talia Hiro Bea Karim Liv Shai Mila Rhea Ezra Iman Adi Kira Luis Yael Marco Zoe""".split()
assert len(NAMES) == len(set(NAMES)), "every name in the office list is different"
REGULAR = {}  # role -> its regular's name, fixed once given
STAFF = {}  # (role, k) -> the role's k-th person after the regular, fixed once given


def pick_name(seed, taken):
    """A first name from the office's list, the same one every time for the same seed, never one in use."""
    start = int(hashlib.sha1(seed.encode()).hexdigest(), 16) % len(NAMES)
    for step in range(len(NAMES)):
        name = NAMES[(start + step) % len(NAMES)]
        if name not in taken:
            return name
    return NAMES[start]


def regular(role):
    """Each role has a regular: the same person whenever that role is in the lounge or alone at a desk."""
    if role not in REGULAR:
        REGULAR[role] = pick_name(role, set(REGULAR.values()) | set(STAFF.values()))
    return REGULAR[role]


def staff(role, k):
    """The role's k-th person: 0 is the regular. A team keeps its people from cycle to cycle, so the
    second builder at work is always the same second builder."""
    if k == 0:
        return regular(role)
    if (role, k) not in STAFF:
        STAFF[(role, k)] = pick_name(f"{role}/{k}", set(REGULAR.values()) | set(STAFF.values()))
    return STAFF[(role, k)]


def short_path(path):
    return str(path or "").replace("/workspace/", "", 1)


def describe(tool, arguments):
    """(kind, text) for one tool call, in a few words."""
    a = arguments or {}
    if tool in ("Read", "Write", "Edit", "NotebookEdit"):
        return tool.lower(), short_path(a.get("file_path") or a.get("notebook_path"))
    if tool in ("Glob", "Grep"):
        return "look", f"{tool.lower()} {a.get('pattern', '')}"
    if tool == "Bash":
        command = str(a.get("command", "")).strip().splitlines()
        return "run", "$ " + (command[0] if command else "")[:140]
    if tool == "WebSearch":
        return "search", str(a.get("query", ""))
    if tool == "WebFetch":
        return "fetch", str(a.get("url", ""))
    if tool in ("Task", "Agent"):
        return "helper", "sub-agent: " + str(a.get("description", ""))
    if tool == "TodoWrite":
        return "plan", f"a to-do list of {len(a.get('todos') or [])}"
    return "tool", tool


def friendly(kind, text):
    """What an agent is doing, in everyday words, from one action. The page never shows raw commands."""
    t = (text or "").lower()
    if kind == "think":
        return "thinking"
    if kind == "refused":
        return "hit a wall, trying another way"
    if kind in ("write", "edit"):
        if t.startswith("view/"):
            return "building the page you see"
        if "company/roles/" in t:
            return "hiring a " + pathlib.PurePath(text).stem.replace("-", " ")
        if "company/tasks/" in t:
            return "writing a task"
        if "company/reports/" in t:
            return "writing its report"
        if "readme" in t:
            return "writing the company charter"
        if any(n in t for n in ("state", "log", "backlog", "notes")):
            return "updating its notes"
        return "writing " + pathlib.PurePath(text).name
    if kind in ("read", "look"):
        if "readme" in t:
            return "reading the charter"
        if "company/tasks/" in t:
            return "reading its task"
        if "company/reports/" in t:
            return "reading a report"
        if t.startswith("view/"):
            return "looking over the page"
        return "looking through files"
    if kind == "run":
        if t.startswith("$ ask "):
            return "asking you for something"
        if "assign " in t:
            return "handing out a task"
        if any(w in t for w in ("pip install", "npm install", "npm i ", "npm pack", "apt-get")):
            return "installing tools"
        if "test" in t:
            return "testing its work"
        return "working in the terminal"
    if kind == "search":
        return "searching the web"
    if kind == "fetch":
        host = URL_HOST.search(text or "")
        return "visiting " + host.group(1).lower() if host else "trying to open a web page"
    if kind == "helper":
        return "asking a helper for a hand"
    if kind == "plan":
        return "making a to-do list"
    if kind == "say":
        return "talking it through"
    if kind == "start":
        return "picking up a new task"
    if kind == "done":
        return "done for this cycle"
    return "working"


def task_title(task_file):
    heading = next((l for l in read_text(WORKSPACE / task_file, 3000).splitlines() if l.startswith("#")), "")
    title = heading.lstrip("#").strip() or pathlib.PurePath(task_file).stem
    # "Task c01-02 · builder · Page: pricing" -> "Page: pricing"; the office shows the role itself.
    return re.sub(r"^Task\s+\S+\s+·\s+[a-z0-9-]+\s+·\s+", "", title)


# A running cycle writes to its stream at least every minute (the press watch alone does, and a session
# streams its thinking); fifteen silent minutes without a cycle_end mean it was cut off: the heartbeat
# killed with the terminal or the Mac switched off, with nobody left to write the ending.
QUIET = 900


def cut_off(agents, beat, last_seen, now):
    """The office as it really is after a cycle died mid-way: nobody at a desk, and the company closed.
    Sessions the stream left open are shown as cut off; the stream itself is left as it is. Times are
    said as "how long ago", since this container's clock and the owner's are in different zones."""
    minutes = max(1, round((now - last_seen) / 60))
    people = [{**a, "session": "ended", "state": "idle", "activity": "cut off when the company stopped"}
              if a.get("session") == "running" else a for a in agents]
    if beat.get("status") == "running":
        beat = {**beat, "status": "stopped",
                "detail": f"nothing has run for {minutes} minutes; the heartbeat ended without a word"}
    return people, beat


class Cycle:
    """The state of one cycle, built line by line from its event stream."""

    def __init__(self, path):
        self.path, self.offset, self.buffer = path, 0, b""
        self.n, self.phase, self.started, self.press = None, "starting", None, None
        self.last_seen = None  # the newest event's own time
        self.agents, self.usage, self.pending = {}, {}, {}
        self.story, self.hired_seen, self.reached, self.turned_told = [], set(), set(), set()

    def silent(self, now):
        """True when the cycle has not ended and its stream has said nothing for QUIET seconds."""
        return self.phase != "ended" and self.last_seen is not None and now - self.last_seen > QUIET

    def agent(self, actor):
        """One agent per session: the head, or a worker named by role and task number ("builder#4"),
        so two workers of one role running side by side stay two agents."""
        key = "head" if actor == "orchestrator" else actor.split(":", 1)[-1]
        if key not in self.agents:
            self.agents[key] = {"id": key, "role": key.split("#")[0], "model": None, "effort": None, "state": "idle",
                                "doing": None, "session": None, "cost": None, "helpers": {},
                                "task": None, "task_title": None, "activity": None, "order": len(self.agents)}
        return self.agents[key]

    def tell(self, ts, text, who):
        # In time order: a line worked out late (a refusal at the door, say) still lands where it happened.
        story = self.story + [{"ts": ts, "text": text, "who": who}]
        self.story = sorted(story, key=lambda e: e["ts"] if isinstance(e["ts"], (int, float)) else 0)[-12:]

    def act(self, agent, kind, text, ts, error=False):
        action = {"kind": kind, "text": text[:220], "ts": ts, "error": error}
        agent["doing"] = action
        agent["activity"] = friendly(kind, text)
        if agent["id"] == "head" and kind == "write" and "company/roles/" in text:
            role = pathlib.PurePath(text).stem
            if role not in self.hired_seen:
                self.hired_seen.add(role)
                self.tell(ts, f"The Head hired {regular(role)} as the {role.replace('-', ' ').title()}", "head")
        if agent["id"] == "head" and kind == "run" and text.startswith("$ assign "):
            parts = text.split()
            if len(parts) >= 4:
                self.tell(ts, f"The Head gave the {parts[2].replace('-', ' ').title()} a task: "
                              f"{task_title(parts[3].replace('/workspace/', ''))}", "head")
        return action

    def promised(self, agent, kind, text):
        """What a call will be worth a story line for, if it comes back without an error: an ask the
        desk filed, and the first time this person reaches a store, a network, a blog... A refused
        ask, or a link that only sat in a command, is not something that happened."""
        after = {}
        if agent["id"] == "head" and kind == "run" and text.startswith("$ ask "):
            parts = text.split()
            if len(parts) >= 3:
                after["ask"] = header(read_text(WORKSPACE / parts[2].replace("/workspace/", ""), 3000)).get("title")
        if kind in ("fetch", "run"):
            places = {}
            for host in URL_HOST.findall(text or ""):
                site, where = place_of(host, PLACES_MAP)
                if site and (agent["id"], site) not in self.reached:
                    places.setdefault(site, where)
            after["places"] = places
        return after

    def came_back(self, agent, after, result, ts):
        """The story lines a call earned, now that it came back without an error."""
        if "ask" in after and FILED.match(result.strip()):
            self.tell(ts, f"The Head asked you for: {after['ask'] or 'something'}. It is waiting at the front desk.", "head")
        for site, where in after.get("places", {}).items():
            if (agent["id"], site) not in self.reached:
                self.reached.add((agent["id"], site))
                who = agent.get("name") or ("The Head" if agent["id"] == "head" else "Someone")
                self.tell(ts, f"{who} went out to {site} ({KIND.get(where, where)})", agent["id"])

    def press_over(self, event, ts):
        """The press space went far over the owner's budget, and the cycle stopped the chroniclers."""
        used, cap = event.get("used"), event.get("cap")
        measured = isinstance(used, (int, float)) and isinstance(cap, (int, float)) and cap > 0
        if measured:
            self.press = {"used": used, "cap": cap}
        stopped = event.get("stopped") if isinstance(event.get("stopped"), list) else []
        # Each stopped task by the person who was on it ("chronicler#7" is task 7), else by its number.
        names = [next((a.get("name") or a["id"] for a in self.agents.values() if a["id"].rpartition("#")[2] == str(i)),
                      f"task {i}") for i in stopped if isinstance(i, (int, str))]
        who = ", ".join(names[:6]) + (f" and {len(names) - 6} more" if len(names) > 6 else "")
        self.tell(ts, "The press space went far over its budget" + (f" ({size(used)} of {size(cap)})" if measured else "")
                  + f", so the cycle stopped {who or 'the press work'}", "cycle")

    def door_news(self, turned_back):
        """A refusal at the door is news once per site in a cycle: the company tried to make an identity."""
        for t in turned_back:
            if isinstance(self.started, (int, float)) and t["last"] >= self.started and t["host"] not in self.turned_told:
                self.turned_told.add(t["host"])
                self.tell(t["last"], f"The door turned back a try to make a new identity at {t['host']}", "cycle")

    def feed(self, line, now):
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            return
        if not isinstance(row, dict) or not isinstance(row.get("event") or {}, dict):
            return
        actor, event = str(row.get("actor", "")), row.get("event") or {}
        ts, kind, sub = row.get("ts") or now, event.get("type"), event.get("subtype")
        legacy = "ts" not in row  # streams from before side-by-side work: no timestamps, workers named by role
        if not legacy and isinstance(ts, (int, float)):
            self.last_seen = max(self.last_seen or 0, ts)
        if actor == "cycle":
            if kind == "cycle_start":
                self.n, self.phase, self.started = event.get("n"), "orchestrator", ts
                head = self.agent("orchestrator")
                head["model"] = (event.get("orchestrator") or {}).get("model") or head["model"]
                head["effort"] = (event.get("orchestrator") or {}).get("effort")
                head["task_title"] = f"Planning cycle {event.get('n')}"
                self.tell(ts, f"Cycle {event.get('n')} began. The Head is planning.", "head")
            elif kind == "orchestrator_end":
                self.phase = "workers"
                self.tell(ts, "The Head finished planning. The team takes over, one at a time.", "head")
            elif kind == "worker_start":
                name = str(event.get("role")) if legacy else f"{event.get('role')}#{event.get('id')}"
                worker = self.agent("worker:" + name)
                title = task_title(str(event.get("task")))
                # The first of the team who is not at a desk takes the task: the regular, if free.
                busy = {a.get("name") for a in self.agents.values() if a.get("session") == "running" and a is not worker}
                k = 0
                while staff(worker["role"], k) in busy:
                    k += 1
                person = staff(worker["role"], k)
                worker.update(model=event.get("model"), effort=event.get("effort"), state="working", session="running",
                              task={"file": short_path(event.get("task")), "report": event.get("report")},
                              task_title=title, name=person)
                self.act(worker, "start", "starts " + short_path(event.get("task")), ts)
                self.tell(ts, f"{person} the {worker['role'].replace('-', ' ').title()} started: {title}", worker["id"])
            elif kind == "owner_note":
                self.tell(ts, "You left the Head a note. It arrives with this cycle's first message.", "head")
            elif kind == "owner_answer":
                if event.get("decision") == "withdrawn":
                    self.tell(ts, "The Head heard you took back: " + ", ".join(event.get("names") or []), "head")
                else:
                    said = "what you provided" if event.get("decision") == "provided" else "your no"
                    self.tell(ts, f"The Head got {said} for: {event.get('title') or event.get('id')}", "head")
            elif kind == "press_space":
                self.press = {"used": event.get("used"), "cap": event.get("cap")}
            elif kind == "press_over":
                self.press_over(event, ts)
            elif kind in ("worker_refused", "worker_failed"):
                self.tell(ts, "A task could not start: " + str(event.get("reason"))[:90], "cycle")
            elif kind == "cycle_end":
                self.phase = "ended"
                self.tell(ts, f"Cycle {self.n} is over.", "cycle")
            return
        agent = self.agent(actor)
        if kind == "system" and sub == "init":
            agent.update(model=event.get("model") or agent["model"], session="running", state="thinking")
        elif kind == "system" and sub == "thinking_tokens":
            if agent["state"] != "working" or not agent["doing"] or agent["doing"]["kind"] != "think":
                agent["state"] = "thinking"
        elif kind == "assistant":
            for block in (event.get("message") or {}).get("content") or []:
                btype = block.get("type") if isinstance(block, dict) else None
                if btype == "thinking":
                    agent["state"] = "thinking"
                elif btype == "text" and str(block.get("text", "")).strip():
                    agent["state"] = "working"
                    self.act(agent, "say", str(block["text"]).strip().replace("\n", " "), ts)
                elif btype == "tool_use":
                    agent["state"] = "working"
                    k, text = describe(block.get("name"), block.get("input"))
                    self.pending[block.get("id")] = (agent, self.act(agent, k, text, ts), self.promised(agent, k, text))
        elif kind == "user":
            content = (event.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                caller, action, after = self.pending.pop(block.get("tool_use_id"), (None, None, None))
                if action is None:
                    continue
                result = block.get("content")
                if isinstance(result, list):
                    result = " ".join(str(c.get("text", "")) for c in result if isinstance(c, dict))
                if block.get("is_error"):
                    text = str(result)
                    action["error"] = True
                    action["kind"] = "refused" if ("hook" in text or "ermission" in text) else action["kind"]
                else:
                    self.came_back(caller, after, str(result or ""), ts)
        elif kind == "system" and sub in ("task_started", "task_progress", "task_notification"):
            helper = agent["helpers"].setdefault(event.get("task_id"), {"what": event.get("description"), "status": "running",
                                                                        "last": "", "type": event.get("task_type")})
            if sub == "task_progress":
                helper["last"] = str(event.get("description") or event.get("last_tool_name") or "")[:160]
            if sub == "task_notification":
                helper["status"] = event.get("status") or "completed"
        elif kind == "rate_limit_event":
            self.usage = event.get("rate_limit_info") or self.usage
        elif kind == "result":
            agent.update(state="done", session="ended", cost=event.get("total_cost_usd"))
            if agent["id"] != "head":
                agent["state"] = "idle"
                who = agent.get("name") or "The " + agent["role"].replace("-", " ").title()
                self.tell(ts, f"{who} finished: {agent.get('task_title') or 'the task'}", agent["id"])
            self.act(agent, "done", "session ended" + (" with an error" if event.get("is_error") else ""), ts,
                     error=bool(event.get("is_error")))

    def catch_up(self):
        """Read the stream on from where the last poll stopped. An office started mid-cycle meets a
        stream of a hundred MB or more, so it is read a few MB at a time, never whole, and split on
        bytes, so a letter cut in two by a chunk's edge is whole again before it is decoded."""
        now = time.time()
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            while chunk := f.read(CHUNK):
                self.offset += len(chunk)
                *lines, self.buffer = (self.buffer + chunk).split(b"\n")
                for line in lines:
                    if line.strip():
                        # One line the office cannot follow costs that line, never the rest of the stream.
                        section("stream line", lambda: self.feed(line.decode("utf-8", errors="replace"), now), None)


def newest_stream():
    paths = glob.glob(str(LOG / "cycles" / "*.jsonl")) + glob.glob(str(LOG / "checks" / "calibrate-[0-9]*.jsonl"))
    paths = [p for p in paths if not p.endswith("-probe.jsonl")]
    return max(paths, key=os.path.getmtime) if paths else None


def header(text):
    fields = {}
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for line in lines[1:12]:
            if line.strip() == "---":
                break
            key, _, value = line.partition(":")
            fields[key.strip().lower()] = value.strip()
    return fields


def read_text(path, limit=4000):
    try:
        with open(path, "r", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def blurb(text):
    """One line on what a role does, taken from its own file. Role files open with the same scene-
    setting sentence ("You are the X of ..."), so that one is skipped for the first sentence that
    says what the job is; failing that, what the role owns."""
    body = text.split("\n---", 2)[-1] if text.startswith("---") else text
    prose = " ".join(l.strip() for l in body.splitlines() if l.strip() and not l.lstrip().startswith(("#", "-", "|")))
    prose = re.sub(r"[*_`]", "", prose)
    sentences = re.split(r"(?<=[.!?])\s+", prose)
    for sentence in sentences:  # a role that says its job outright says it best
        if sentence.startswith(("Your job is", "Your job:", "Your role is")):
            return sentence[:160]
    for sentence in sentences:
        if sentence.startswith(("You may ", "You can ")):  # what it is allowed to do, not what it does
            continue
        negative = sentence.startswith(("You don't", "You do not", "You never", "You must not"))
        if not negative and ((sentence.startswith("You ") and not sentence.startswith("You are ")) or ", you " in sentence):
            return sentence[:160]
    owns = re.search(r"^##\s*You own\s*$(.*?)(?=^##|\Z)", body, re.M | re.S)
    if owns:
        items = [re.sub(r"[*_`]", "", m).strip().lower() for m in re.findall(r"^-\s*([^:\n]+):", owns.group(1), re.M)][:3]
        if items:
            return "Keeps " + (", ".join(items[:-1]) + " and " + items[-1] if len(items) > 1 else items[0]) + "."
    return ""


def crew():
    roles = {}
    for path in sorted(glob.glob(str(WORKSPACE / "company" / "roles" / "*.md"))):
        text = read_text(path, 3000)
        fields = header(text)
        roles[pathlib.Path(path).stem] = {"model": fields.get("model"), "effort": fields.get("effort"), "blurb": blurb(text)}
    return roles


def queue_entries():
    """Every queue line the box kept, by report. The queue files are in the workspace, where a worker
    can write anything, so a line counts only when its role, task and report are texts, as in the box."""
    entries = {}
    box = WORKSPACE / ".box"
    for path in [box / "queue.jsonl", *box.glob("running-*.jsonl"), *box.glob("done/*.jsonl"), *box.glob("dropped/*.jsonl")]:
        for line in read_text(path, 400_000).splitlines():
            try:
                entry = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if isinstance(entry, dict) and all(isinstance(entry.get(k), str) for k in ("role", "task", "report")):
                entries[entry["report"]] = entry
    return entries


def headcount(entries):
    """Each role's team: as many people as it ever had tasks in one cycle, at least one, at most sixteen."""
    most = {}
    cycles = ((e.get("cycle") if isinstance(e.get("cycle"), int) else None, e.get("role")) for e in entries.values())
    for (cycle, role), n in collections.Counter(cycles).items():
        if isinstance(role, str):
            most[role] = min(16, max(most.get(role, 1), n))
    return most


def tasks(current_cycle, running_roles, entries):
    found = []
    for path in sorted(glob.glob(str(WORKSPACE / "company" / "reports" / "c*.md"))):
        name = "company/reports/" + pathlib.Path(path).name
        first = next((l.strip() for l in read_text(path, 2000).splitlines() if l.strip()), "")
        entry = entries.get(name, {})
        role = entry.get("role") or (re.search(r"(?:for|by) ([a-z0-9-]+) \(", first) or [None, None])[1]
        match = re.match(r"c(\d+)-(\d+)-", pathlib.Path(path).name)
        cycle = int(match.group(1)) if match else None
        if first.startswith("Queued"):
            status = "queued"
        elif first.startswith("Started") or re.match(r"(?:partial:\s*)?in progress\b", first, re.I):
            # Agents now write "Partial: in progress - ..." early and keep updating it; only a final
            # report says Done/Partial/Blocked without "in progress". Left behind, it was cut off.
            status = "started" if cycle == current_cycle and role in running_roles else "cut off"
        elif first.startswith("Not run"):
            status = "not run"
        elif first.startswith("Ended") and "without writing a report" in first:
            status = "no report"
        else:
            status = "done"
        title = ""
        task_file = entry.get("task")
        if task_file:
            heading = next((l for l in read_text(WORKSPACE / task_file, 3000).splitlines() if l.startswith("#")), "")
            title = heading.lstrip("#").strip() or pathlib.Path(task_file).stem
            # "Task c01-02 · builder · Page: pricing" -> "Page: pricing"; the board shows the role itself.
            title = re.sub(r"^Task\s+\S+\s+·\s+[a-z0-9-]+\s+·\s+", "", title)
        found.append({"report": name, "cycle": cycle, "role": role, "status": status,
                      "title": title or pathlib.Path(path).stem, "line": first[:200]})
    return found


class DoorLog:
    """What the office needs from the door's log, read on from where the last poll stopped. Nothing
    rotates that log and it grows about a megabyte an hour, so its head is old news: a fresh start
    reads only its last 32 MB, more than the day the turned-back list covers, and every poll after
    that reads only the new lines.

    It keeps running totals of connections let through and refused (the door light blinks when one
    moves; a count over a window of lines would stand still while old lines leave it), each visit to
    a listed place in the last two hours, and each refusal to make a new identity in the last day."""

    def __init__(self, path):
        self.path = path
        self.start(0)
        self.offset = None  # nothing read yet: the first catch_up decides where to start

    def start(self, offset):
        self.offset, self.rest, self.cut = offset, b"", offset > 0
        self.allowed = self.refused = 0
        self.visits = collections.deque()  # (when, site, kind), oldest first
        self.turned = collections.deque(maxlen=1000)  # (when, host), oldest first

    def catch_up(self, now):
        try:
            end = os.path.getsize(self.path)
        except OSError:
            end = 0
        if self.offset is None or end < self.offset:  # the first read, or the log was replaced
            # One byte early, so the first piece is either the tail of a cut line or empty, and goes.
            self.start(max(0, end - DOOR_START - 1))
        if end > self.offset:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                while chunk := f.read(CHUNK):
                    self.offset += len(chunk)
                    lines = (self.rest + chunk).split(b"\n")
                    self.rest = lines.pop()
                    if self.cut and lines:  # a fresh start lands inside a line (or just after one)
                        lines, self.cut = lines[1:], False
                    for line in lines:
                        self.take(line, now)
        while self.visits and now - self.visits[0][0] >= REACH_WINDOW:
            self.visits.popleft()
        while self.turned and now - self.turned[0][0] >= TURNED_WINDOW:
            self.turned.popleft()

    def take(self, line, now):
        if b'"allow"' not in line and b'"refuse"' not in line:
            return
        try:
            row = json.loads(line)
            when = calendar.timegm(time.strptime(row["ts"], "%Y-%m-%dT%H:%M:%SZ"))
        except (ValueError, KeyError, TypeError, RecursionError):
            return
        host = str(row.get("host") or "")[:100]
        if row.get("event") == "allow":
            self.allowed += 1
            site, where = place_of(host, PLACES_MAP)
            if site and now - when < REACH_WINDOW:
                self.visits.append((when, site, KIND.get(where, where)))
        elif row.get("event") == "refuse":
            self.refused += 1
            if row.get("reason") == "no-new-identities" and now - when < TURNED_WINDOW:
                self.turned.append((when, host))

    def markers(self):
        return {"allowed": self.allowed, "refused": self.refused}

    def turned_back(self):
        """Each site the door refused because the company tried to make a new identity there, in the
        last day: how often, and when last. Newest first."""
        hosts = {}
        for when, host in self.turned:
            entry = hosts.setdefault(host, {"host": host, "count": 0, "last": 0})
            entry["count"] += 1
            entry["last"] = max(entry["last"], when)
        return sorted(hosts.values(), key=lambda e: -e["last"])[:12]

    def reach(self, now):
        """Where the company went out in the world in the last two hours: each named place, its kind,
        how many connections and when last. The door's log knows sites, not people."""
        seen = {}
        for when, site, kind in self.visits:
            if now - when < REACH_WINDOW:
                entry = seen.setdefault(site, {"site": site, "kind": kind, "count": 0, "last": 0})
                entry["count"] += 1
                entry["last"] = max(entry["last"], when)
        return sorted(seen.values(), key=lambda e: -e["last"])[:12]


def jsonl(path, limit=2_000_000):
    rows = []
    for line in read_text(path, limit).splitlines():
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def texts(value):
    """A list of texts from a field the company wrote, or nothing: anything else is not a list of names."""
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def front_desk(cache):
    """Every request the Head filed, with where it stands: waiting, provided, no, replaced by a newer
    ask, or edited after it was filed. A waiting request carries its full text and fingerprint, which
    the page sends back with the answer.

    asks.jsonl is in the workspace, where any worker can write, so a line in it proves nothing. An ask
    is shown only when the outside log holds the Head's own `ask` of it (company.filed_asks, kept up by
    `cache`) and the first line for its id names that same file. Its title and the names it needs are
    read from that file, the text the person reads and the desk checks, never from the line. Every
    other line is counted, and the page says how many were ignored."""
    filed = filed_asks(LOG, cache)
    # The Head's asks whose reply has not reached the log yet: ask.py writes its line a moment before
    # that, and a request on its way is early, not ignored.
    asking = set((cache.get("pending") or {}).values())
    # Only the first line for an id can be the Head's, as at the desk: a line slipped in ahead of it with
    # another file makes the ask unanswerable there, so here it is not shown as waiting either.
    proven, ignored, seen = {}, 0, set()
    for e in jsonl(WORKSPACE / ".box" / "asks.jsonl"):
        ident = e.get("id")
        first = isinstance(ident, str) and ident not in seen
        if first:
            seen.add(ident)
        if first and ASK_ID.fullmatch(ident) and filed.get(ident) == e.get("file"):
            proven[ident] = e
        else:
            early = isinstance(ident, str) and ident not in filed and isinstance(e.get("file"), str) and e["file"] in asking
            ignored += not early
    latest = {d["id"]: d for d in jsonl(DESK / "decisions.jsonl") if isinstance(d.get("id"), str)}
    replaced = {r for e in proven.values() for r in texts(e.get("replaces"))}
    out = []
    for ident, e in proven.items():
        try:
            path = (WORKSPACE / e["file"]).resolve()
            text = read_text(path, 20_000) if (WORKSPACE / "company").resolve() in path.parents else ""
        except (ValueError, OSError):  # a name no file can have, like one with a NUL in it
            text = ""
        sha = fingerprint(text) if text else None
        try:
            title, needs, _ = parse_ask(text)
        except ValueError:
            title, needs = None, None  # the Head's ask passed this check when it was filed; this text is not that one
        d = latest.get(ident)
        if d:
            status = {"provided": "provided", "no": "no"}.get(d.get("decision"), "no")
        elif ident in replaced:
            status = "replaced"
        elif sha != e.get("sha256") or needs is None:
            status = "edited"
        else:
            status = "waiting"
        answered = d or {}
        # An answered ask keeps the title the desk recorded when it was answered, from the same file.
        shown = answered.get("title") if isinstance(answered.get("title"), str) else title
        out.append({"id": ident, "title": str(shown or e["file"])[:160], "needs": needs or [], "file": e["file"],
                    "asked_at": e.get("asked_at") if isinstance(e.get("asked_at"), str) else None,
                    "cycle": e.get("cycle") if type(e.get("cycle")) is int else None, "status": status,
                    "names": texts(answered.get("names")), "note": str(answered.get("note") or "")[:600] or None,
                    "answered_at": answered.get("at") if isinstance(answered.get("at"), str) else None,
                    "text": text if status == "waiting" else "", "sha256": sha if status == "waiting" else None})
    waiting = [a for a in out if a["status"] == "waiting"]
    rest = sorted((a for a in out if a["status"] != "waiting"), key=lambda a: a["answered_at"] or a["asked_at"] or "", reverse=True)
    return {"asks": waiting + rest[:12], "ignored": ignored}


def vault_names():
    """The names now in the vault, worked out from the book alone: what was provided and not taken
    back. The book is the desk's, outside the box, so this list stands even when the asks do not. The
    vault's values never come here."""
    vault = {}
    for d in jsonl(DESK / "decisions.jsonl"):
        for name in texts(d.get("names")):
            if d.get("decision") == "provided":
                title = d.get("title")
                vault[name] = {"name": name, "at": d.get("at") if isinstance(d.get("at"), str) else None,
                               "for": title[:160] if isinstance(title, str) else None}
            elif d.get("decision") == "withdrawn":
                vault.pop(name, None)
    return sorted(vault.values(), key=lambda v: v["name"])


LEDGER_ROWS = 5000
NO_SALES = {"count": 0, "refunds": 0, "totals": {}, "last": []}


def clean_ledger(raw):
    """The sales the company reports, cleaned to numbers and short texts: count, totals per currency,
    and the last five. It is the company's own account; the store's dashboard is the record.

    Stores answer in their own way, so "usd" is read as USD and "9.00" as 9, and a refund is a negative
    amount. Only the newest rows are kept. A row whose amount or currency still cannot be read is left
    out and counted, and the page says so, so a sales meter at 0.00 is never a silent mistake."""
    rows = raw.get("sales") if isinstance(raw, dict) else None
    rows = rows if isinstance(rows, list) else []
    first = max(0, len(rows) - LEDGER_ROWS)
    sales, skipped = [], []
    for i, item in enumerate(rows[first:], start=first):
        amount = item.get("amount") if isinstance(item, dict) else None
        currency = item.get("currency") if isinstance(item, dict) else None
        if isinstance(amount, str):
            try:
                amount = float(amount)
            except ValueError:
                amount = None
        currency = currency.strip().upper() if isinstance(currency, str) else None
        # Not a bool, a finite number under ten million either way, and a three-letter currency code.
        if (isinstance(amount, bool) or not isinstance(amount, (int, float)) or not abs(amount) < 1e7
                or not currency or not re.fullmatch(r"[A-Z]{3}", currency)):
            skipped.append(i)
            continue
        short = lambda v, n: " ".join(v.split())[:n] if isinstance(v, str) else ""  # noqa: E731
        sales.append({"what": short(item.get("what"), 60), "where": short(item.get("where"), 40),
                      "when": short(item.get("when"), 25), "amount": round(float(amount), 2), "currency": currency})
    totals = {}
    for sale in sales:
        totals[sale["currency"]] = round(totals.get(sale["currency"], 0) + sale["amount"], 2)
    report = {"count": sum(s["amount"] >= 0 for s in sales), "refunds": sum(s["amount"] < 0 for s in sales),
              "totals": totals, "last": sales[-5:][::-1]}
    problems = []
    if skipped:
        problems.append(f"{len(skipped)} {'sale' if len(skipped) == 1 else 'sales'} left out (amount or currency "
                        f"not readable), the first at sales[{skipped[0]}]")
    if first:
        problems.append(f"only the newest {LEDGER_ROWS} of {len(rows)} sales are counted")
    if problems:
        report["problem"] = "; ".join(problems)
    return report


def ledger():
    path = WORKSPACE / "company" / "ledger.json"
    if not path.is_file():
        return dict(NO_SALES)
    if path.stat().st_size > 2_000_000:
        return {**NO_SALES, "problem": "ledger.json is over 2 MB, so it is not read"}
    try:
        return clean_ledger(json.loads(read_text(path, 2_000_000)))
    except (ValueError, RecursionError):
        return {**NO_SALES, "problem": "ledger.json is not valid JSON"}


HEX = re.compile(r"#[0-9a-fA-F]{6}")
DECOR = {"plant", "tall-plant", "lamp", "poster", "painting", "clock", "banner", "neon", "window", "whiteboard",
         "trophy", "bookshelf", "aquarium", "server-rack", "arcade", "flag", "beanbag"}
MERCH = {"cap", "shirt", "badge", "scarf", "headphones", "mug"}


def clean_look(raw, roles):
    """(look, problems): only the fixed list of things in office/look-format.md, every colour a #rrggbb
    and every text short, so the page can apply it as colours and plain text and nothing else."""
    problems, look = [], {}

    def text(value, limit, where):
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{where}: not a text")
            return None
        if len(value) > limit:
            problems.append(f"{where}: cut to {limit} characters")
        return " ".join(value.split())[:limit]

    def color(value, where):
        if isinstance(value, str) and HEX.fullmatch(value):
            return value.lower()
        problems.append(f"{where}: {str(value)[:20]!r} is not a #rrggbb colour")
        return None

    def decor(items, where):
        things = []
        for i, item in enumerate(items[:6] if isinstance(items, list) else []):
            item = {"thing": item} if isinstance(item, str) else item
            # Each test is on a text first: a list where a name belongs is a mistake, not a crash.
            if not isinstance(item, dict) or not isinstance(item.get("thing"), str) or item["thing"] not in DECOR:
                problems.append(f"{where}[{i}]: not one of the decor things")
                continue
            thing = {"thing": item["thing"]}
            if "text" in item and (t := text(item["text"], 20, f"{where}[{i}].text")):
                thing["text"] = t
            if "color" in item and (c := color(item["color"], f"{where}[{i}].color")):
                thing["color"] = c
            things.append(thing)
        if isinstance(items, list) and len(items) > 6:
            problems.append(f"{where}: only the first 6 are shown")
        return things

    if not isinstance(raw, dict):
        return {}, ["look.json is not a JSON object"]
    for key in raw:
        if key not in ("sign", "motto", "colors", "logo", "floors", "lobby", "head", "merch"):
            problems.append(f"{key}: not part of the look")
    if "sign" in raw and (t := text(raw["sign"], 24, "sign")):
        look["sign"] = t
    if "motto" in raw and (t := text(raw["motto"], 60, "motto")):
        look["motto"] = t
    if isinstance(raw.get("colors"), dict):
        look["colors"] = {k: c for k in ("sky", "tower", "lobby", "head") if k in raw["colors"]
                          and (c := color(raw["colors"][k], f"colors.{k}"))}
    logo = raw.get("logo")
    if isinstance(logo, dict) and isinstance(logo.get("pixels"), list) and isinstance(logo.get("colors"), dict):
        palette = {k: c for k, v in list(logo["colors"].items())[:8] if isinstance(k, str) and len(k) == 1
                   and (c := color(v, f"logo.colors.{k}"))}
        rows = [r[:16] for r in logo["pixels"][:16] if isinstance(r, str)]
        if palette and rows:
            look["logo"] = {"pixels": rows, "colors": palette}
    elif "logo" in raw:
        problems.append("logo: needs pixels (a list of rows) and colors (character -> #rrggbb)")
    if isinstance(raw.get("floors"), dict):
        look["floors"] = {}
        for role, floor in raw["floors"].items():
            if role not in roles or not isinstance(floor, dict):
                problems.append(f"floors.{str(role)[:30]}: no such role")
                continue
            clean = {}
            if "name" in floor and (t := text(floor["name"], 24, f"floors.{role}.name")):
                clean["name"] = t
            if "wall" in floor and (c := color(floor["wall"], f"floors.{role}.wall")):
                clean["wall"] = c
            if "decor" in floor:
                clean["decor"] = decor(floor["decor"], f"floors.{role}.decor")
            look["floors"][role] = clean
    for place in ("lobby", "head"):
        if isinstance(raw.get(place), dict) and "decor" in raw[place]:
            look[place] = {"decor": decor(raw[place]["decor"], f"{place}.decor")}
    merch = []
    for i, item in enumerate(raw.get("merch")[:8] if isinstance(raw.get("merch"), list) else []):
        if not isinstance(item, dict) or not isinstance(item.get("item"), str) or item["item"] not in MERCH:
            problems.append(f"merch[{i}]: not one of {', '.join(sorted(MERCH))}")
            continue
        who = item.get("for", "everyone")
        if not isinstance(who, str) or (who not in ("everyone", "head") and who not in roles):
            problems.append(f"merch[{i}].for: {str(who)[:30]!r} is not everyone, head or a role")
            continue
        if c := color(item.get("color"), f"merch[{i}].color"):
            merch.append({"item": item["item"], "color": c, "for": who})
    if merch:
        look["merch"] = merch
    return look, problems[:30]


def look(roles):
    path = WORKSPACE / "company" / "look.json"
    if not path.is_file():
        return {}, []
    if path.stat().st_size > 64_000:
        return {}, ["look.json is over 64 KB"]
    try:
        raw = json.loads(read_text(path, 64_000))
    except (ValueError, RecursionError) as error:
        return {}, [f"look.json is not valid JSON: {error}"[:200]]
    return clean_look(raw, roles)


def merch_news(before, after):
    """A line for the story when new merch goes out."""
    fresh = [m for m in after.get("merch", []) if m not in before.get("merch", [])]
    if not fresh:
        return None
    gifts = ", ".join(f"{m['item']}s for {'everyone' if m['for'] == 'everyone' else 'the ' + m['for'].replace('-', ' ')}"
                      for m in fresh[:3])
    return f"Merch day: new {gifts}"


def heartbeat():
    lines = read_text(LOG / "heartbeat.jsonl", 2_000_000).splitlines()
    last = json.loads(lines[-1]) if lines else None
    state = {}
    try:
        state = json.loads(read_text(LOG / "heartbeat-state.json"))
    except ValueError:
        pass
    if not last:
        return {"status": "not started", "detail": "the founding cycle was started by hand", **state}
    status = {"stopped": "stopped", "sleeping": "sleeping"}.get(last.get("event"), "running")
    detail = last.get("why") or ""
    if last.get("until"):
        detail += " until " + last["until"]
    # The weekly cap is whatever the running heartbeat was started with (`make start` passes it).
    cap = None
    for line in reversed(lines):
        if '"started"' in line:
            try:
                cap = json.loads(line).get("share_pp")
            except ValueError:
                continue
            break
    return {"status": status, "detail": detail.strip(), **state, "share_pp": cap if isinstance(cap, (int, float)) else 20}


def view():
    root = WORKSPACE / "view"
    files = []  # (path, when it last changed), each stated once: a build's temporary file can vanish in between
    try:
        for p in root.rglob("*") if root.is_dir() else []:
            try:
                if p.is_file():
                    files.append((p, p.stat().st_mtime))
            except OSError:
                continue
            if len(files) >= 5000:
                break
    except OSError:
        pass
    latest = max(files, key=lambda f: f[1])[0] if files else None
    title = re.search(r"<title>(.*?)</title>", read_text(root / "index.html", 20000), re.S | re.I)
    return {"files": len(files), "latest": str(latest.relative_to(WORKSPACE)) if latest else None,
            "title": title.group(1).strip()[:80] if title else None}


def company_name():
    """The name from the first heading of the company's charter, whatever the Head called that file:
    "# Acme - handbook" is Acme."""
    for name in ("README.md", "handbook.md", "HANDBOOK.md", "charter.md"):
        first = next((l for l in read_text(WORKSPACE / "company" / name, 2000).splitlines() if l.startswith("# ")), "")
        found = re.split(r"\s[\u2014\u2013-]\s", first[2:].strip())[0][:40]  # a dash of any length
        # "Acme handbook" names the file, not the company.
        found = re.sub(r"\s+(handbook|charter|readme)$", "", found, flags=re.I).strip()
        if found:
            return found
    return None


class Office:
    def __init__(self):
        self.lock, self.cycle, self.look = threading.Lock(), None, None
        self.filed = {}  # company.filed_asks keeps its place in the outside log here, between polls
        self.door = DoorLog(LOG / "door.jsonl")

    def people(self, c, roles, team):
        """(everyone in the building, the roles at work right now)."""
        sessions = list(c.agents.values()) if c else []
        blank = {"model": None, "effort": None, "state": "idle", "doing": None, "session": None, "cost": None,
                 "helpers": {}, "task": None, "task_title": None, "activity": None, "order": 999}
        head = next((a for a in sessions if a["id"] == "head"), None) or {**blank, "id": "head", "role": "head",
                                                                          "model": "claude-opus-5-5", "effort": "max"}
        head = {**head, "blurb": "Runs the company. Decides everything, writes the tasks and hands them out."}
        workers = [a for a in sessions if a["id"] != "head"]
        running = {a["role"] for a in workers if a.get("session") == "running"}
        out = [head]
        # Everyone on every team, every time: at a desk while working, otherwise in the lounge,
        # carrying what their last session this cycle did.
        for role in sorted(set(roles) | {a["role"] for a in workers}):
            fields = roles.get(role, {})
            extra = {"role": role, "blurb": fields.get("blurb"), "hired_model": fields.get("model"),
                     "hired_effort": fields.get("effort")}
            mine, placed = [a for a in workers if a["role"] == role], set()
            for k in range(team.get(role, 1)):
                name = staff(role, k)
                theirs = sorted((a for a in mine if a.get("name") == name), key=lambda a: (a.get("session") == "running", a["order"]))
                if theirs:
                    placed.add(theirs[-1]["id"])
                base = theirs[-1] if theirs else {**blank, "id": f"{role}~{k}"}
                out.append({**base, **extra, "name": name})
            # Anyone at work under a name from before teams kept their names.
            out += [{**a, **extra} for a in mine if a.get("session") == "running" and a["id"] not in placed]
        out = [{**a, "helpers": [h for h in (a.get("helpers") or {}).values() if h.get("type") == "local_agent"][-6:]}
               for a in out]
        return out, running

    def snapshot(self):
        """The whole office, each part built on its own: a part a broken file takes down comes back
        empty, and the rest, the front desk above all, still answers."""
        with self.lock:
            now = time.time()
            path = section("stream", newest_stream, None)
            roles = section("roles", crew, {})
            entries = section("queue", queue_entries, {})
            team = section("teams", lambda: headcount(entries), {})
            # Names in a fixed order before any event is read, so a restart gives everyone the same name.
            for role in sorted(set(roles) | set(team)):
                for k in range(team.get(role, 1)):
                    staff(role, k)
            if path and (self.cycle is None or self.cycle.path != path):
                self.cycle = Cycle(path)
            c = self.cycle
            if c:
                section("cycle", c.catch_up, None)
            section("door", lambda: self.door.catch_up(now), None)
            read = section("look", lambda: look(set(roles)), None)
            styled, problems = read or (self.look or {}, ["look.json could not be read"])
            if read and c and self.look is not None and styled != self.look:
                news = merch_news(self.look, styled)
                c.tell(now, news or "The building got a new look", "head")
            if read:
                self.look = styled
            turned_back = section("turned back", self.door.turned_back, [])
            if c:
                section("door news", lambda: c.door_news(turned_back), None)
            out, running = section("people", lambda: self.people(c, roles, team), ([], set()))
            beat = section("heartbeat", heartbeat, {"status": "unknown", "detail": ""})
            quiet = bool(c and c.silent(now))
            if quiet:
                (out, beat), running = cut_off(out, beat, c.last_seen, now), set()
            desk = section("front desk", lambda: front_desk(self.filed), {"asks": [], "ignored": 0})
            return {
                "now": now, "company": section("name", company_name, None),
                "source": os.path.basename(path) if path else None,
                "cycle": {"n": c.n if c else None, "phase": ("stopped" if quiet else c.phase) if c else "none",
                          "started": c.started if c else None},
                "agents": out, "tasks": section("tasks", lambda: tasks(c.n if c else None, running, entries), []),
                "usage": c.usage if c else {}, "door_counts": section("door light", self.door.markers, {}),
                "heartbeat": beat,
                "view": section("view", view, {"files": 0, "latest": None, "title": None}),
                "story": c.story if c else [],
                "desk": {**desk, "vault": section("vault", vault_names, []), "turned_back": turned_back},
                "reach": section("reach", lambda: self.door.reach(now), []),
                "sales": section("sales", ledger, {**NO_SALES, "problem": "ledger.json could not be read"}),
                "press": c.press if c else None,
                "look": {**styled, "problems": problems},
                "page_version": PAGE.stat().st_mtime,
            }


PLACES_MAP = load_places()
OFFICE = Office()


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.headers.get("Host") not in HOSTS:
            self.send_error(403)
            return
        if self.path in ("/", "/index.html"):
            body, kind = PAGE.read_bytes(), "text/html; charset=utf-8"
        elif self.path.startswith("/state.json"):
            body, kind = json.dumps(OFFICE.snapshot()).encode(), "application/json"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        # Never inside another page's frame: the company's own page on 8770 could lay this one under
        # its own and steer the owner's clicks at the desk, which then reach the desk as the owner's.
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        super().end_headers()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
