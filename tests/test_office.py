"""The office's view of the desk, the look, sales, places and the door: pure functions and temp dirs
only, standard library only. Nothing here opens a socket or starts a container.

Run:  python3 -m unittest discover -s tests
"""
import contextlib
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "box" / "bin")]

from company import fingerprint  # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


office = load("office_server", "office/server.py")

ASK = "---\ntitle: A Gumroad store\nneeds: STORE_URL, GUMROAD_ACCESS_TOKEN\n---\nA store in your name, and a key that can create products.\n"


def iso(when):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when))


def line(actor, event, ts=None):
    return json.dumps({"actor": actor, "ts": ts or time.time(), "event": event})


def tool_use(ident, command):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "id": ident, "input": {"command": command}}]}}


def tool_result(ident, text, error=False):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": ident, "content": text, "is_error": error}]}}


def head_asked(log, ident, file, answered=True):
    """What the outside log holds when the head runs `ask`: its command, and (once it came back) the reply."""
    (log / "cycles").mkdir(parents=True, exist_ok=True)
    rows = [line("orchestrator", tool_use(f"t{ident}", f"ask {file}"))]
    if answered:
        rows.append(line("orchestrator", tool_result(f"t{ident}", f"asked {ident}: A Gumroad store. It is on the person's desk.")))
    with (log / "cycles" / "0001.jsonl").open("a") as f:
        f.write("\n".join(rows) + "\n")


class Rooms(unittest.TestCase):
    """A temporary workspace, desk book and outside log, put in place of the office's own."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        self.work, self.book, self.log = root / "w", root / "desk", root / "log"
        for folder in (self.work / ".box", self.work / "company" / "asks", self.work / "company" / "roles", self.book, self.log):
            folder.mkdir(parents=True)
        (self.work / "company" / "asks" / "store.md").write_text(ASK)
        old = {k: getattr(office, k) for k in ("WORKSPACE", "DESK", "LOG")}
        office.WORKSPACE, office.DESK, office.LOG = self.work, self.book, self.log
        self.addCleanup(lambda: [setattr(office, k, v) for k, v in old.items()])

    def asks(self, *rows):
        (self.work / ".box" / "asks.jsonl").write_text("".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in rows))

    def decisions(self, *rows):
        (self.book / "decisions.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def real(ident="a1", **more):
    return {"id": ident, "file": "company/asks/store.md", "title": "A Gumroad store", "needs": ["STORE_URL"],
            "sha256": fingerprint(ASK), "cycle": 6, **more}


class TheLook(unittest.TestCase):
    def test_only_the_listed_look_gets_through(self):
        look, problems = office.clean_look({
            "sign": "<img src=x onerror=alert(1)>", "script": "alert(1)",
            "colors": {"sky": "#123456", "tower": "red", "lobby": "url(javascript:x)"},
            "floors": {"builder": {"name": "The Workshop", "wall": "#EADBC2", "decor": ["plant", {"thing": "iframe"}, {"thing": "poster", "text": "Ship it", "color": "#ef476f"}]},
                       "ghost": {"name": "Nobody"}},
            "merch": [{"item": "cap", "color": "#2dd4bf", "for": "everyone"}, {"item": "cape", "color": "#ffffff"},
                      {"item": "mug", "color": "#ffffff", "for": "ghost"}],
        }, {"builder"})
        self.assertEqual(look["colors"], {"sky": "#123456"})
        self.assertEqual(look["floors"], {"builder": {"name": "The Workshop", "wall": "#eadbc2",
                                                      "decor": [{"thing": "plant"}, {"thing": "poster", "text": "Ship it", "color": "#ef476f"}]}})
        self.assertEqual(look["merch"], [{"item": "cap", "color": "#2dd4bf", "for": "everyone"}])
        self.assertEqual(len(look["sign"]), 24)  # kept as text, cut short; the page never renders it as markup
        self.assertNotIn("script", look)
        self.assertGreaterEqual(len(problems), 6)

    def test_a_list_where_a_name_belongs_is_a_problem_not_a_crash(self):
        # The natural slip: merch for two roles at once. It used to take the whole office down.
        look, problems = office.clean_look({
            "merch": [{"item": "cap", "color": "#ffffff", "for": ["builder", "writer"]}, {"item": ["cap"], "color": "#ffffff"},
                      {"item": "mug", "color": "#ffffff", "for": "builder"}],
            "lobby": {"decor": [{"thing": ["plant"]}, {"thing": {"a": 1}}, "lamp"]},
            "floors": {"builder": {"decor": [{"thing": ["x"]}]}},
        }, {"builder", "writer"})
        self.assertEqual(look["merch"], [{"item": "mug", "color": "#ffffff", "for": "builder"}])
        self.assertEqual(look["lobby"], {"decor": [{"thing": "lamp"}]})
        self.assertTrue(any("merch[0].for" in p for p in problems))

    def test_new_merch_makes_the_story(self):
        after = {"merch": [{"item": "cap", "color": "#2dd4bf", "for": "everyone"}]}
        self.assertEqual(office.merch_news({"merch": []}, after), "Merch day: new caps for everyone")
        self.assertIsNone(office.merch_news(after, after))


class Sales(unittest.TestCase):
    def test_reported_sales_are_numbers_and_short_texts(self):
        report = office.clean_ledger({"sales": [
            {"when": "2026-01-15", "what": "Starter pack", "where": "store", "amount": 9, "currency": "USD"},
            {"what": "x", "amount": True, "currency": "USD"}, {"what": "x", "amount": "nine", "currency": "USD"},
            {"what": "x", "amount": 3, "currency": "dollars"}, {"what": "x", "amount": 1e9, "currency": "USD"},
            {"what": "<b>" * 40, "amount": 2.5, "currency": "EUR"}]})
        self.assertEqual((report["count"], report["totals"]), (2, {"USD": 9.0, "EUR": 2.5}))
        self.assertEqual(len(report["last"][0]["what"]), 60)
        self.assertEqual(office.clean_ledger("nonsense"), {"count": 0, "refunds": 0, "totals": {}, "last": []})

    def test_store_answers_are_read_as_the_store_meant_them(self):
        # Stripe and Gumroad answer "usd"; a store's export has "9.00"; a refund is a negative amount.
        report = office.clean_ledger({"sales": [
            {"amount": 9, "currency": "usd"}, {"amount": "9.00", "currency": " Usd "}, {"amount": -9, "currency": "USD"},
            {"amount": "nan", "currency": "USD"}]})
        self.assertEqual((report["count"], report["refunds"], report["totals"]), (2, 1, {"USD": 9.0}))

    def test_every_row_left_out_is_counted_and_said(self):
        report = office.clean_ledger({"sales": [{"amount": 5, "currency": "USD"}, "a sale", {"amount": "$5", "currency": "USD"}]})
        self.assertEqual(report["count"], 1)
        self.assertEqual(report["problem"], "2 sales left out (amount or currency not readable), the first at sales[1]")
        self.assertNotIn("problem", office.clean_ledger({"sales": [{"amount": 5, "currency": "USD"}]}))

    def test_the_newest_sales_are_the_ones_kept(self):
        old = [{"amount": 1000, "currency": "EUR"}] * 3
        report = office.clean_ledger({"sales": old + [{"amount": 1, "currency": "USD"}] * 5000})
        self.assertEqual(report["totals"], {"USD": 5000.0})
        self.assertIn("only the newest 5000 of 5003", report["problem"])


class Places(unittest.TestCase):
    def test_places_are_named_by_their_site(self):
        places = {"gumroad.com": "store", "reddit.com": "social"}
        self.assertEqual(office.place_of("api.gumroad.com", places), ("gumroad.com", "store"))
        self.assertEqual(office.place_of("old.reddit.com.", places), ("reddit.com", "social"))
        self.assertEqual(office.place_of("notgumroad.com", places), (None, None))
        self.assertEqual(office.load_places()["gumroad.com"], "store")
        self.assertNotIn("mail.tm", office.load_places())


class TheFrontDesk(Rooms):
    def test_the_vault_as_the_office_sees_it_is_names_only(self):
        head_asked(self.log, "a1", "company/asks/store.md")
        self.asks(real())
        self.decisions({"id": "a1", "decision": "provided", "names": ["STORE_URL", "GUMROAD_ACCESS_TOKEN"], "title": "A Gumroad store", "at": "t1"},
                       {"id": "vault", "decision": "withdrawn", "names": ["STORE_URL"], "at": "t2"})
        self.assertEqual([v["name"] for v in office.vault_names()], ["GUMROAD_ACCESS_TOKEN"])
        asks = office.front_desk({})["asks"]
        self.assertEqual((asks[0]["status"], asks[0]["text"]), ("provided", ""))

    def test_only_asks_the_head_filed_are_shown(self):
        head_asked(self.log, "a1", "company/asks/store.md")
        (self.work / "company" / "asks" / "fake.md").write_text(ASK.replace("A Gumroad store", "A bank login"))
        self.asks(real(), {**real("a2"), "file": "company/asks/fake.md"},  # a worker's line: the log never saw a2
                  {**real("../../vault/TOKEN/x")}, {**real("a1"), "file": "company/asks/fake.md"}, "[1, 2]")
        desk = office.front_desk({})
        self.assertEqual([a["id"] for a in desk["asks"]], ["a1"])
        self.assertEqual(desk["ignored"], 3)

    def test_title_and_needs_come_from_the_file_the_head_filed(self):
        head_asked(self.log, "a1", "company/asks/store.md")
        # The line was edited to add a field for a key the request never asked for.
        self.asks({**real(), "title": "Just one small thing", "needs": ["STRIPE_SECRET_KEY"]})
        a = office.front_desk({})["asks"][0]
        self.assertEqual((a["status"], a["title"], a["needs"]), ("waiting", "A Gumroad store", ["GUMROAD_ACCESS_TOKEN", "STORE_URL"]))
        self.assertEqual(a["sha256"], fingerprint(ASK))

    def test_the_first_line_for_an_id_is_the_one_that_counts(self):
        head_asked(self.log, "a1", "company/asks/store.md")
        self.asks(real(), {**real(), "sha256": "0" * 64})
        desk = office.front_desk({})
        self.assertEqual(([a["status"] for a in desk["asks"]], desk["ignored"]), (["waiting"], 1))

    def test_a_line_ahead_of_the_heads_takes_its_place_as_at_the_desk(self):
        # The desk honours only the first line for an id, so a card shown here would be refused there.
        head_asked(self.log, "a1", "company/asks/store.md")
        (self.work / "company" / "asks" / "fake.md").write_text(ASK.replace("A Gumroad store", "A bank login"))
        self.asks({**real(), "file": "company/asks/fake.md"}, real())
        desk = office.front_desk({})
        self.assertEqual((desk["asks"], desk["ignored"]), ([], 2))

    def test_only_a_filed_ask_can_replace_another(self):
        head_asked(self.log, "a1", "company/asks/store.md")
        self.asks(real(), {**real("a9"), "replaces": ["a1"]})
        self.assertEqual(office.front_desk({})["asks"][0]["status"], "waiting")

    def test_an_ask_on_its_way_is_not_counted_as_ignored(self):
        # ask.py writes its line a moment before its reply reaches the outside log.
        head_asked(self.log, "a1", "company/asks/store.md", answered=False)
        self.asks(real())
        desk = office.front_desk({})
        self.assertEqual((desk["asks"], desk["ignored"]), ([], 0))
        head_asked(self.log, "a1", "company/asks/store.md")
        self.assertEqual(len(office.front_desk({})["asks"]), 1)

    def test_junk_in_asks_jsonl_never_breaks_the_desk(self):
        head_asked(self.log, "a1", "company/asks/store.md")
        self.asks({**real(), "replaces": 5, "needs": 3, "asked_at": 7, "cycle": [1]}, {"id": "a2", "replaces": [{"x": 1}], "file": ["x"]},
                  {"id": ["a3"]}, "[" * 100000)
        self.decisions({"id": "a7", "decision": "provided", "names": "STORE_URL"}, {"id": "a8", "decision": "provided", "names": [["X"]]})
        a = office.front_desk({})["asks"][0]
        self.assertEqual((a["status"], a["asked_at"], a["cycle"]), ("waiting", None, None))
        self.assertEqual(office.vault_names(), [])


class TheDoor(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = pathlib.Path(tmp.name) / "door.jsonl"
        self.now = time.time()

    def write(self, *rows, mode="a"):
        with self.path.open(mode) as f:
            f.write("".join(json.dumps(r) + "\n" for r in rows))

    def row(self, event, host, ago, **more):
        return {"ts": iso(self.now - ago), "event": event, "client": "box", "host": host, "port": 443, **more}

    def test_the_end_of_the_log_is_read_not_its_head(self):
        # A log bigger than a fresh start reads: only its end counts, and the line cut in two is skipped.
        old = office.DOOR_START
        office.DOOR_START = 2000
        self.addCleanup(setattr, office, "DOOR_START", old)
        self.write(*[self.row("allow", "api.gumroad.com", 60, filler="x" * 200) for _ in range(40)])
        self.write(self.row("allow", "reddit.com", 30))
        door = office.DoorLog(self.path)
        door.catch_up(self.now)
        self.assertLess(door.allowed, 40)
        self.assertEqual(door.reach(self.now)[0]["site"], "reddit.com")

    def test_the_light_moves_with_every_connection(self):
        self.write(*[self.row("allow", "pypi.org", 5)] * 400)
        door = office.DoorLog(self.path)
        door.catch_up(self.now)
        before = door.markers()
        # A window of the last lines would read 400 again; a running total moves.
        self.write(self.row("allow", "pypi.org", 1))
        door.catch_up(self.now)
        self.assertEqual(door.markers(), {"allowed": before["allowed"] + 1, "refused": before["refused"]})
        self.write(self.row("refuse", "accounts.google.com", 1, reason="no-new-identities"))
        door.catch_up(self.now)
        self.assertEqual(door.markers()["refused"], before["refused"] + 1)

    def test_refusals_to_make_identities_stay_for_a_day(self):
        self.write(self.row("refuse", "signup.old.example", 25 * 3600, reason="no-new-identities"),
                   self.row("refuse", "accounts.google.com", 20 * 3600, reason="no-new-identities"),
                   self.row("refuse", "accounts.google.com", 3600, reason="no-new-identities"),
                   self.row("refuse", "10.0.0.1", 60, reason="private-address"),
                   *[self.row("allow", "pypi.org", 30)] * 2000)
        door = office.DoorLog(self.path)
        door.catch_up(self.now)
        self.assertEqual([(t["host"], t["count"]) for t in door.turned_back()], [("accounts.google.com", 2)])

    def test_out_in_the_world_covers_two_hours_of_listed_places(self):
        self.write(self.row("allow", "api.gumroad.com", 3 * 3600), self.row("allow", "api.gumroad.com", 600),
                   self.row("allow", "api.gumroad.com", 60), self.row("allow", "pypi.org", 60))
        door = office.DoorLog(self.path)
        door.catch_up(self.now)
        reach = door.reach(self.now)
        self.assertEqual([(r["site"], r["kind"], r["count"]) for r in reach], [("gumroad.com", "a store", 2)])

    def test_a_line_still_being_written_waits_until_it_is_whole(self):
        door = office.DoorLog(self.path)
        whole = json.dumps(self.row("allow", "pypi.org", 1))
        self.path.write_text(whole[:20])
        door.catch_up(self.now)
        self.assertEqual(door.allowed, 0)
        with self.path.open("a") as f:
            f.write(whole[20:] + "\n")
        door.catch_up(self.now)
        self.assertEqual(door.allowed, 1)


class TheStory(Rooms):
    def feed(self, cycle, *rows):
        for r in rows:
            cycle.feed(r, time.time())

    def told(self, cycle):
        return [e["text"] for e in cycle.story]

    def test_an_ask_is_told_only_once_the_desk_took_it(self):
        c = office.Cycle("x")
        self.feed(c, line("orchestrator", tool_use("t1", "ask company/asks/store.md")),
                  line("orchestrator", tool_result("t1", "ask: this exact request is already a1", error=True)))
        self.assertFalse(any("asked you" in t for t in self.told(c)))
        self.feed(c, line("orchestrator", tool_use("t2", "ask company/asks/store.md")),
                  line("orchestrator", tool_result("t2", "asked a2: A Gumroad store. It is on the person's desk.")))
        self.assertEqual([t for t in self.told(c) if "asked you" in t],
                         ["The Head asked you for: A Gumroad store. It is waiting at the front desk."])

    def test_going_out_is_told_only_when_the_call_came_back(self):
        c = office.Cycle("x")
        self.feed(c, line("worker:builder#2", tool_use("t1", "curl https://api.gumroad.com/v2/products")),
                  line("worker:builder#2", tool_result("t1", "curl: (6) Could not resolve host", error=True)))
        self.assertFalse(any("went out" in t for t in self.told(c)))
        self.feed(c, line("worker:builder#2", tool_use("t2", "curl https://api.gumroad.com/v2/products")),
                  line("worker:builder#2", tool_result("t2", '{"success": true}')))
        self.assertEqual([t for t in self.told(c) if "went out" in t], ["Someone went out to gumroad.com (a store)"])

    def test_stopping_the_chroniclers_for_space_is_told(self):
        c = office.Cycle("x")
        self.feed(c, line("cycle", {"type": "worker_start", "role": "chronicler", "id": 7, "task": "company/tasks/film.md"}),
                  line("cycle", {"type": "press_over", "used": 1_600_000_000, "cap": 1_000_000_000, "stopped": [7]}))
        name = c.agents["chronicler#7"]["name"]
        self.assertIn(f"The press space went far over its budget (1.6 GB of 1 GB), so the cycle stopped {name}", self.told(c))
        self.assertEqual(c.press, {"used": 1_600_000_000, "cap": 1_000_000_000})

    def test_a_refusal_at_the_door_is_told_once_per_site_in_a_cycle(self):
        c = office.Cycle("x")
        self.feed(c, line("cycle", {"type": "cycle_start", "n": 4}, ts=1000))
        before = {"host": "accounts.google.com", "count": 1, "last": 900}
        now = {"host": "accounts.google.com", "count": 2, "last": 1100}
        c.door_news([before])
        c.door_news([now])
        c.door_news([now])
        self.assertEqual([t for t in self.told(c) if "door" in t],
                         ["The door turned back a try to make a new identity at accounts.google.com"])

    def test_a_long_stream_is_read_in_pieces_and_no_letter_is_cut(self):
        old = office.CHUNK
        office.CHUNK = 7
        self.addCleanup(setattr, office, "CHUNK", old)
        stream = self.log / "0001.jsonl"
        say = {"type": "assistant", "message": {"content": [{"type": "text", "text": "שלום, ready to ship ✓"}]}}
        stream.write_text(line("orchestrator", say) + "\nnot json at all\n" + line("orchestrator", {"type": "user", "message": "x"}) + "\n")
        c = office.Cycle(stream)
        with contextlib.redirect_stdout(io.StringIO()):  # the line it cannot follow is logged, and skipped
            c.catch_up()
        self.assertEqual(c.agents["head"]["doing"]["text"], "שלום, ready to ship ✓")


class AlwaysAnswering(Rooms):
    def test_a_part_that_breaks_falls_back_and_says_so_once(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            for _ in range(3):
                self.assertEqual(office.section("test part", lambda: json.loads("[" * 100000), "empty"), "empty")
        self.assertEqual(out.getvalue().count("part_failed"), 1)
        self.assertEqual(office.section("test part", lambda: "back", "empty"), "back")

    def test_broken_company_files_never_take_the_office_down(self):
        roles = self.work / "company" / "roles"
        (roles / "builder.md").write_text("---\nmodel: sonnet\neffort: high\n---\nYou build things.\n")
        (self.work / "company" / "look.json").write_text(json.dumps({"merch": [{"item": "cap", "color": "#ffffff", "for": ["builder"]}]}))
        (self.work / "company" / "ledger.json").write_text("[" * 100000)
        (self.work / ".box" / "queue.jsonl").write_text("\n".join([
            json.dumps({"role": ["builder"], "task": "t", "report": "r1"}), json.dumps({"role": "builder", "task": 5, "report": "r2"}),
            json.dumps({"role": "builder", "task": "t", "report": "company/reports/c0001-01-x.md", "cycle": [1]}), "{" * 5000]) + "\n")
        self.asks({**real(), "replaces": 5}, "[" * 100000)
        self.decisions({"id": "a1", "decision": "provided", "names": ["GUMROAD_ACCESS_TOKEN"], "title": "A Gumroad store", "at": "t1"})
        (self.log / "cycles").mkdir()
        (self.log / "cycles" / "0001.jsonl").write_text(line("cycle", {"type": "cycle_start", "n": 1}) + "\n" + "[" * 100000 + "\n")
        (self.log / "door.jsonl").write_text(json.dumps({"ts": iso(time.time()), "event": "allow", "host": "pypi.org"}) + "\n")
        with contextlib.redirect_stdout(io.StringIO()):
            state = office.Office().snapshot()
        json.dumps(state)
        self.assertEqual([v["name"] for v in state["desk"]["vault"]], ["GUMROAD_ACCESS_TOKEN"])
        self.assertTrue(state["look"]["problems"])
        self.assertEqual(state["sales"]["problem"], "ledger.json is not valid JSON")
        self.assertEqual(state["cycle"]["n"], 1)
        self.assertEqual(state["door_counts"], {"allowed": 1, "refused": 0})
        # What the page never reads is not sent every second.
        self.assertFalse({"events", "door"} & set(state))
        self.assertFalse(any("actions" in a for a in state["agents"]))


class FakeSocket:
    """Just enough of a socket for the request handler: the request in, the response kept."""

    def __init__(self, request):
        self.request, self.sent = request, b""

    def makefile(self, mode, *args):
        return io.BytesIO(self.request)

    def sendall(self, data):
        self.sent += data


def get(path, host):
    sock = FakeSocket(f"GET {path} HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
    office.Handler(sock, ("127.0.0.1", 50000), None)
    head, _, _ = sock.sent.partition(b"\r\n\r\n")
    return head.decode()


class TheServer(unittest.TestCase):
    def test_only_the_office_own_names_are_answered(self):
        self.assertTrue(get("/", f"127.0.0.1:{office.PORT}").split("\r\n")[0].endswith("200 OK"))
        self.assertTrue(get("/", f"office:{office.PORT}").split("\r\n")[0].endswith("200 OK"))
        # A page that rebound its own name to 127.0.0.1 still sends that name.
        self.assertIn(" 403 ", get("/state.json", f"attacker.example:{office.PORT}").split("\r\n")[0])
        self.assertIn(" 403 ", get("/", "127.0.0.1:9999").split("\r\n")[0])

    def test_the_page_never_sits_in_another_page_frame(self):
        head = get("/", f"localhost:{office.PORT}")
        self.assertIn("X-Frame-Options: DENY", head)
        self.assertIn("Content-Security-Policy: frame-ancestors 'none'", head)


class ADeadCycle(unittest.TestCase):
    """A cycle whose heartbeat was killed leaves its last workers open in the stream forever."""

    def test_a_silent_stream_closes_the_office(self):
        agents = [{"id": "head", "session": "ended"}, {"id": "inspector#6", "session": "running", "state": "working"}]
        people, beat = office.cut_off(agents, {"status": "running", "detail": ""}, 1_000_000.0, 1_000_000.0 + 27 * 60)
        self.assertEqual([a["session"] for a in people], ["ended", "ended"])
        self.assertEqual(people[1]["activity"], "cut off when the company stopped")
        self.assertEqual((beat["status"], beat["detail"][:30]), ("stopped", "nothing has run for 27 minutes"))

    def test_only_fifteen_quiet_minutes_count(self):
        c = office.Cycle(pathlib.Path("/nonexistent"))
        c.feed(json.dumps({"actor": "cycle", "ts": 1000.0, "event": {"type": "cycle_start", "n": 7}}), 1000.0)
        self.assertFalse(c.silent(1000.0 + office.QUIET - 1))
        self.assertTrue(c.silent(1000.0 + office.QUIET + 1))
        c.feed(json.dumps({"actor": "cycle", "ts": 2000.0, "event": {"type": "cycle_end", "n": 7}}), 2000.0)
        self.assertFalse(c.silent(10_000.0))


class TheWeeklyCap(unittest.TestCase):
    def test_the_office_shows_the_cap_the_heartbeat_runs_with(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = pathlib.Path(tmp)
            (log / "heartbeat.jsonl").write_text("\n".join(json.dumps(r) for r in [
                {"event": "started", "share_pp": 20.0}, {"event": "cycle_start", "n": 7},
                {"event": "started", "share_pp": 30.0}, {"event": "cycle_start", "n": 8}]) + "\n")
            (log / "heartbeat-state.json").write_text(json.dumps({"week_used_pp": 18.0}))
            old = office.LOG
            office.LOG = log
            self.addCleanup(setattr, office, "LOG", old)
            beat = office.heartbeat()
            self.assertEqual((beat["status"], beat["share_pp"], beat["week_used_pp"]), ("running", 30.0, 18.0))


class TaskStates(Rooms):
    """Agents write an early progress report ("Partial: in progress - ...") and keep updating it, so a
    report that exists is not yet a finished one. Found in testing: six working tasks counted as done."""

    def report(self, name, first):
        folder = self.work / "company" / "reports"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text(first + "\n\nmore\n")

    def status(self, current_cycle, running_roles):
        return {t["report"].split("/")[-1]: t["status"] for t in office.tasks(current_cycle, running_roles, {})}

    def test_an_in_progress_report_is_working_then_cut_off_when_its_cycle_is_over(self):
        self.report("c0003-01-b-api-tests.md", "Partial: in progress - cloud tests by builder (claude-sonnet-5-5)")
        self.report("c0003-02-b-site.md", "Partial: the site is live, the sitemap is not submitted yet")
        self.report("c0003-03-b-converter.md", "Done: the converter is built and tested")
        self.assertEqual(self.status(3, {"builder"}), {"c0003-01-b-api-tests.md": "started",
                                                        "c0003-02-b-site.md": "done", "c0003-03-b-converter.md": "done"})
        self.assertEqual(self.status(4, set())["c0003-01-b-api-tests.md"], "cut off")


if __name__ == "__main__":
    unittest.main()
