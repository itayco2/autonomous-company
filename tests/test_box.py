"""The box side of the front desk: requests, the Head's hook, what the Head hears, the press budget.
Pure functions only, standard library only.

Run:  python3 -m unittest discover -s tests
"""
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "box" / "bin"), str(ROOT / "door")]

import company  # noqa: E402
from company import fingerprint, parse_ask, space_used  # noqa: E402
from proxy import Door, listed  # noqa: E402
import ask  # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


desk = load("desk_server", "desk/server.py")
office = load("office_server", "office/server.py")
hook = load("only_assign", "box/bin/only-assign.py")
cycle = load("cycle", "box/bin/cycle.py")

ASK = "---\ntitle: A Gumroad store\nneeds: STORE_URL, GUMROAD_ACCESS_TOKEN\n---\nA store in your name, and a key that can create products.\n"


class Requests(unittest.TestCase):
    def test_a_request_names_what_it_needs_back(self):
        self.assertEqual(parse_ask(ASK), ("A Gumroad store", ["GUMROAD_ACCESS_TOKEN", "STORE_URL"],
                                          "A store in your name, and a key that can create products."))

    def test_a_request_may_need_only_an_answer(self):
        self.assertEqual(parse_ask(ASK.replace("needs: STORE_URL, GUMROAD_ACCESS_TOKEN\n", ""))[1], [])

    def test_needs_are_vault_names(self):
        # A name becomes a file in the vault: nothing that could leave it, nothing ambiguous.
        for needs in ("../ESCAPE", "gumroad_token", "A", "STORE URL", "TOKEN/X", "_X"):
            with self.assertRaises(ValueError, msg=needs):
                parse_ask(ASK.replace("STORE_URL, GUMROAD_ACCESS_TOKEN", needs))

    def test_a_request_says_what_and_why(self):
        for broken in ("No header.\n", ASK.replace("title: A Gumroad store\n", ""),
                       ASK.replace("A store in your name, and a key that can create products.\n", "")):
            with self.assertRaises(ValueError):
                parse_ask(broken)

    def test_at_most_ten_needs(self):
        with self.assertRaises(ValueError):
            parse_ask(ASK.replace("STORE_URL, GUMROAD_ACCESS_TOKEN", ", ".join(f"KEY_{i}" for i in range(11))))

    def test_the_walls_give_the_head_the_rules_ask_holds(self):
        # The Head learns the request format from walls.md alone, before ask ever refuses it.
        walls = " ".join((ROOT / "walls.md").read_text().split())
        self.assertEqual(company.MAX_NEEDS, 10)
        self.assertIn("at most ten, in capitals, digits and underscores", walls)
        self.assertIn("leave it out when you need an answer, not a thing", walls)
        self.assertIn("The person can provide only the names it lists", walls)
        for folder in company.PRESS_SPACE:
            self.assertIn(f"{folder.relative_to(company.WORKSPACE)}/", walls)


class TheHeadsHook(unittest.TestCase):
    def verdict(self, command):
        return hook.decide({"tool_name": "Bash", "tool_input": {"command": command}})

    def test_ask_is_the_heads_second_command(self):
        self.assertIsNone(self.verdict("ask company/asks/store.md"))
        self.assertIsNone(self.verdict("ask /workspace/company/asks/store.md"))

    def test_nothing_rides_along_with_ask(self):
        for command in ("ask company/a.md && rm -rf company", "ask ../etc/passwd", "ask company/../../etc/passwd",
                        "ask /etc/passwd", "ask company/a.md; ls", "ask", "ask /opt/company/vault/X"):
            self.assertIsNotNone(self.verdict(command), command)


class TheHeadHears(unittest.TestCase):
    def test_each_answer_in_one_line(self):
        self.assertEqual(cycle.told({"id": "a1", "title": "A store", "decision": "provided", "names": ["STORE_URL"], "note": "Done"}),
                         "- a1 (A store): provided. In the vault: STORE_URL. Their note: Done")
        self.assertEqual(cycle.told({"id": "a2", "title": "Ads", "decision": "no"}), "- a2 (Ads): no.")
        self.assertEqual(cycle.told({"id": "vault", "decision": "withdrawn", "names": ["STORE_URL"]}),
                         "- STORE_URL: taken back out of the vault.")

    def test_the_press_budget_in_one_sentence(self):
        under = cycle.press_line(420_000_000)
        self.assertIn("holds 420 MB of the 1 GB", under)
        self.assertNotIn("over", under)
        self.assertIn("holds 1.2 GB of the 1 GB", cycle.press_line(1_200_000_000))
        self.assertIn("over that now", cycle.press_line(1_200_000_000))

    def test_space_is_measured_once_per_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b = pathlib.Path(tmp) / "a", pathlib.Path(tmp) / "b"
            a.mkdir(), b.mkdir()
            (a / "f").write_bytes(b"x" * 100_000)
            os.link(a / "f", b / "f")
            self.assertEqual(space_used([a, b]), space_used([a]))
            self.assertGreaterEqual(space_used([a]), 100_000)

    def test_junk_in_the_book_is_never_fatal(self):
        # The book is a file on the Mac; a hand edit or a torn write must not stop a cycle at its start.
        for junk in ({"id": "a1", "decision": "provided", "names": "STORE_URL"}, {"id": "a1", "names": 7},
                     {"id": "a1", "decision": "provided", "names": [1, None, "STORE_URL"], "title": {"x": 1}},
                     {"id": "a1", "decision": "no", "note": ["a"], "title": None}, {"decision": "withdrawn", "names": [[]]},
                     {"id": ["a1"], "decision": ["no"]}):
            self.assertIsInstance(cycle.told(junk), str, junk)
        self.assertEqual(cycle.told({"id": "a1", "decision": "provided", "names": [1, "STORE_URL"], "title": 5}),
                         "- a1 (a request): provided. In the vault: STORE_URL.")
        with tempfile.TemporaryDirectory() as tmp:
            book, heard = pathlib.Path(tmp) / "decisions.jsonl", pathlib.Path(tmp) / "heard.json"
            book.write_text("\n".join([
                '{"id": "a1", "decision": "provided", "names": ["X_KEY"], "ts": 1}', '{"id": 5, "decision": "no"}',
                '{"id": "a2", "decision": {"no": 1}, "ts": [1]}', '{"id": "a3", "decision": "maybe"}', "[1, 2]",
                '{"id": "a4", "decision": "no", "ts": {"x": [1]}}', "[" * 100_000, '{"id": "a5", "deci']))
            heard.write_text('[["unhashable"]]')
            with mock.patch.multiple(cycle, DECISIONS=book, HEARD=heard):
                fresh, was_heard = cycle.desk_answers()
            self.assertEqual([a["id"] for a in fresh], ["a1", "a4"])  # only what the desk writes is an answer
            self.assertEqual(was_heard, set())
            self.assertEqual(len([cycle.told(a) for a in fresh]), 2)


def fake_claude(code, prompts):
    """A stand-in for one headless session: it keeps the prompt and exits with `code`."""
    def run(actor, prompt, *rest, **kwargs):
        prompts.append(prompt)
        return code
    return run


class CycleStart(unittest.TestCase):
    """main() in a temporary workspace, with the head's session faked: no model, no Docker."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name).resolve()
        self.workspace, self.desk = root / "workspace", root / "desk"
        self.company, self.box = self.workspace / "company", self.workspace / ".box"
        for folder in (self.company / "asks", self.box, self.desk, root / "notes", root / "vault"):
            folder.mkdir(parents=True)
        (root / "walls.md").write_text("walls")
        self.walls = root / "walls.md"
        (root / "notes" / "hello.md").write_text("Make something.")
        self.book = self.desk / "decisions.jsonl"
        self.book.write_text(json.dumps({"id": "a1", "decision": "no", "title": "A store", "ts": 1}) + "\n")
        self.patches = mock.patch.multiple(
            cycle, WORKSPACE=self.workspace, COMPANY=self.company, BOX=self.box, QUEUE=self.box / "queue.jsonl",
            DELIVERED=self.box / "notes-delivered.json", HEARD=self.box / "answers-heard.json",
            NOTES=root / "notes", DECISIONS=self.book, ASKS=self.box / "asks.jsonl", VAULT=root / "vault",
            PRESS_SPACE=(self.workspace / "press", self.workspace / "view" / "press"), VAULT_SEEN={})
        self.patches.start()

    def tearDown(self):
        self.patches.stop()
        self.tmp.cleanup()

    def cycle(self, code):
        """Run one cycle start with the head's session exiting `code`; (its prompt, the events)."""
        prompts, out = [], io.StringIO()
        with mock.patch.object(cycle, "claude", fake_claude(code, prompts)), \
                mock.patch.object(sys, "argv", ["cycle.py", "7", "--no-workers", "--system", str(self.walls)]), \
                contextlib.redirect_stdout(out):
            cycle.main()
        return prompts[0], [json.loads(line)["event"] for line in out.getvalue().splitlines()]

    def test_a_failed_session_hears_its_notes_and_answers_again(self):
        prompt, _ = self.cycle(1)
        self.assertIn("Make something.", prompt)
        self.assertIn("- a1 (A store): no.", prompt)
        self.assertFalse((self.box / "notes-delivered.json").exists())
        self.assertFalse((self.box / "answers-heard.json").exists())
        prompt, _ = self.cycle(0)  # told again, and this time it ended well
        self.assertIn("Make something.", prompt)
        self.assertIn("- a1 (A store): no.", prompt)
        prompt, _ = self.cycle(0)
        self.assertNotIn("Make something.", prompt)
        self.assertNotIn("a1", prompt)

    def test_the_head_hears_which_requests_can_no_longer_be_answered(self):
        request = self.company / "asks" / "github.md"
        request.write_text(ASK)
        asked = [{"id": "a2", "file": "company/asks/github.md", "sha256": fingerprint(ASK)},
                 {"id": "a3", "file": "company/asks/gone.md", "sha256": fingerprint(ASK)}]
        (self.box / "asks.jsonl").write_text("".join(json.dumps(a) + "\n" for a in asked))
        prompt, _ = self.cycle(0)
        self.assertNotIn("a2 can no longer", prompt)
        self.assertIn("- a3 can no longer be answered: company/asks/gone.md is gone; ask again", prompt)
        request.write_text(ASK + "One more line.\n")
        prompt, _ = self.cycle(0)
        self.assertIn("- a2 can no longer be answered: company/asks/github.md changed since it was asked", prompt)

    def test_the_press_is_measured_again_after_the_workers(self):
        _, events = self.cycle(0)
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds[-2:], ["press_space", "cycle_end"])
        self.assertLess(kinds.index("press_space"), kinds.index("orchestrator_end"))

    def test_a_vault_value_never_reaches_the_outside_log(self):
        (self.workspace.parent / "vault" / "GITHUB_TOKEN").write_text("ghp_secret_value_123\n")
        self.book.write_text(json.dumps({"id": "a1", "decision": "no", "note": "it is ghp_secret_value_123"}) + "\n")
        out = io.StringIO()
        with mock.patch.object(cycle, "claude", fake_claude(0, [])), \
                mock.patch.object(sys, "argv", ["cycle.py", "7", "--no-workers", "--system", str(self.walls)]), \
                contextlib.redirect_stdout(out):
            cycle.main()
        self.assertNotIn("ghp_secret_value_123", out.getvalue())
        self.assertIn("[vault:GITHUB_TOKEN]", out.getvalue())


class UnanswerableRequests(unittest.TestCase):
    """Which of the head's requests the person can no longer answer."""

    def test_only_unanswered_unreplaced_changed_requests(self):
        files = {"company/asks/a.md": "A", "company/asks/b.md": "B2", "company/asks/c.md": "C2", "company/asks/d.md": "D2"}
        asked = [{"id": "a1", "file": "company/asks/a.md", "sha256": fingerprint("A")},       # unchanged
                 {"id": "a2", "file": "company/asks/b.md", "sha256": fingerprint("B")},       # changed
                 {"id": "a3", "file": "company/asks/c.md", "sha256": fingerprint("C")},       # changed, answered
                 {"id": "a4", "file": "company/asks/d.md", "sha256": fingerprint("D")},       # changed, replaced
                 {"id": "a5", "file": "company/asks/d.md", "sha256": fingerprint("D2"), "replaces": ["a4"]},
                 {"id": "a2", "file": "company/asks/a.md", "sha256": fingerprint("A")},       # a later forgery of a2
                 {"id": "a6", "file": ["x"], "sha256": 5}, {"id": 7}, {"replaces": "a1"}, {"replaces": [["a1"]]}]
        lines = cycle.void_asks(asked, {"a3": {}}, files.get)
        self.assertEqual(lines, ["- a2 can no longer be answered: company/asks/b.md changed since it was asked; "
                                 "ask again if it is still wanted."])

    def test_a_request_outside_company_is_not_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = pathlib.Path(tmp) / "workspace"
            (workspace / "company").mkdir(parents=True)
            (workspace / "secret.md").write_text("x")
            (workspace / "company" / "r.md").write_text("ask")
            with mock.patch.multiple(cycle, WORKSPACE=workspace, COMPANY=workspace / "company"):
                self.assertEqual(cycle.request_text("company/r.md"), "ask")
                for name in ("secret.md", "company/../secret.md", "/etc/passwd", "company/none.md", "company/\0x"):
                    self.assertIsNone(cycle.request_text(name), name)


class AskingAgain(unittest.TestCase):
    """ask.py's refusal of a repeat must match what the desk can still answer."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name).resolve()
        self.workspace = root / "workspace"
        (self.workspace / "company" / "asks").mkdir(parents=True)
        self.request = self.workspace / "company" / "asks" / "store.md"
        self.asks = self.workspace / ".box" / "asks.jsonl"
        self.patches = mock.patch.multiple(ask, ASKS=self.asks, DECISIONS=root / "decisions.jsonl",
                                           COMPANY=self.workspace / "company", WORKSPACE=self.workspace)
        self.patches.start()
        self.env = mock.patch.dict(os.environ, {"COMPANY_ACTOR": "orchestrator", "COMPANY_CYCLE": "3"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.patches.stop()
        self.tmp.cleanup()

    def ask(self, text):
        self.request.write_text(text)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                ask.main([str(self.request)])
            except SystemExit:
                return err.getvalue()
        return out.getvalue()

    def test_a_replaced_request_put_back_is_a_new_ask(self):
        edited = ASK + "Also a payout.\n"
        self.assertTrue(self.ask(ASK).startswith("asked a1:"))
        self.assertTrue(self.ask(edited).startswith("asked a2:"))
        # Back to the first text: a1 was replaced and the desk would refuse it, so this is a3, not "still waiting".
        self.assertTrue(self.ask(ASK).startswith("asked a3:"))
        last = company.read_jsonl(self.asks)[-1]
        self.assertEqual(last["replaces"], ["a1", "a2"])

    def test_the_same_waiting_request_is_still_refused(self):
        self.ask(ASK)
        self.assertIn("already a1, still waiting", self.ask(ASK))

    def test_junk_in_the_asks_file_is_not_fatal(self):
        self.asks.parent.mkdir(parents=True)
        self.asks.write_text('{"id": ["a9"], "sha256": "x", "file": "company/asks/store.md"}\n{"id": "a²"}\n'
                             '{"id": "a4", "replaces": "a1"}\n{"id": 5, "replaces": [[1]]}\n')
        self.assertTrue(self.ask(ASK).startswith("asked a5:"))


class VaultInTheLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_values_are_read_again_when_the_vault_changes(self):
        cache = {}
        (self.vault / "STORE_URL").write_text("https://shop.example\n")
        (self.vault / "PIN").write_text("1234")  # too short to look for
        (self.vault / ".GITHUB_TOKEN.tmp").write_text("half-written")  # the desk's temporary file
        self.assertEqual(cycle.vault_values(self.vault, cache), {"STORE_URL": "https://shop.example"})
        (self.vault / "GITHUB_TOKEN").write_text("ghp_abcdefgh")
        self.assertEqual(cycle.vault_values(self.vault, cache)["GITHUB_TOKEN"], "ghp_abcdefgh")
        (self.vault / "GITHUB_TOKEN").write_text("ghp_a_new_longer_one")
        self.assertEqual(cycle.vault_values(self.vault, cache)["GITHUB_TOKEN"], "ghp_a_new_longer_one")
        (self.vault / "GITHUB_TOKEN").unlink()
        self.assertNotIn("GITHUB_TOKEN", cycle.vault_values(self.vault, cache))

    def test_a_vault_that_cannot_be_read_hides_nothing_new_and_is_not_fatal(self):
        self.assertEqual(cycle.vault_values(self.vault / "missing", {}), {})

    def test_raw_and_json_forms_become_the_name(self):
        values = {"KEY": 'ab"cd\\efgh', "TOKEN": "secret-token-1", "LONGER": "secret-token-1-and-more"}
        line = json.dumps({"event": {"content": 'it is ab"cd\\efgh and secret-token-1-and-more', "x": "secret-token-1"}})
        out = cycle.redact(line, values)
        self.assertEqual(json.loads(out), {"event": {"content": "it is [vault:KEY] and [vault:LONGER]",
                                                     "x": "[vault:TOKEN]"}})
        self.assertEqual(cycle.redact("plain secret-token-1 text", values), "plain [vault:TOKEN] text")
        self.assertEqual(cycle.redact(json.dumps({"x": "déjà-vu-key"}), {"K": "déjà-vu-key"}),
                         '{"x": "[vault:K]"}')

    def test_a_redacted_line_is_still_one_json_object(self):
        # A numeric id inside a number, and a value whose first letter is the n of an escaped newline:
        # replaced in the line as it is written, either would leave a line nothing can read.
        values = {"STORE_ID": "123456789", "TOKEN": "nX7fQ2pL9w"}
        line = json.dumps({"event": {"input": {"store": 123456789, "url": "/stores/123456789"},
                                     "text": "first\nX7fQ2pL9w", "said": "use nX7fQ2pL9w"}, "cost": 0.123456789})
        out = cycle.redact(line, values)
        self.assertEqual(json.loads(out), {"event": {"input": {"store": 123456789, "url": "/stores/[vault:STORE_ID]"},
                                                     "text": "first\nX7fQ2pL9w", "said": "use [vault:TOKEN]"},
                                           "cost": 0.123456789})

    def test_every_printed_line_is_redacted(self):
        (self.vault / "SERVICE_APP_PASSWORD").write_text("abcd-efgh-ijkl-mnop")
        out = io.StringIO()
        with mock.patch.multiple(cycle, VAULT=self.vault, VAULT_SEEN={}), contextlib.redirect_stdout(out):
            cycle.emit("worker:poster#2", {"type": "user", "message": {"content": [
                {"type": "tool_result", "content": "abcd-efgh-ijkl-mnop\n"}]}})
        row = json.loads(out.getvalue())
        self.assertEqual(row["event"]["message"]["content"][0]["content"], "[vault:SERVICE_APP_PASSWORD]\n")

    def test_the_rule_is_told_to_every_agent(self):
        self.assertIn("$(cat /opt/company/vault/NAME), and never print it", cycle.WORKER_PREAMBLE)
        walls = (ROOT / "walls.md").read_text()
        self.assertIn("$(cat /opt/company/vault/NAME)", walls)
        self.assertIn("Leave a request file unchanged after `ask`", walls)


class PressCap(unittest.TestCase):
    def test_the_press_room_is_part_of_the_press_space(self):
        self.assertIn(company.WORKSPACE / "view" / "press", company.PRESS_SPACE)
        line = cycle.press_line(0)
        self.assertIn("/workspace/press/, /workspace/view/press/ and /workspace/company/work/chronicler/", line)
        self.assertIn("moving it frees nothing", line)
        walls = (ROOT / "walls.md").read_text()
        self.assertIn("view/press/", walls)
        self.assertIn("counts wherever it is kept", walls)

    def test_a_chronicler_over_the_cap_cleans_up_first(self):
        over, under = company.PRESS_CAP + 1, company.PRESS_CAP
        self.assertTrue(cycle.press_first("chronicler", over).startswith("Before anything else"))
        self.assertTrue(cycle.press_first("chronicler-video", over).endswith("\n\n"))
        self.assertEqual(cycle.press_first("chronicler", under), "")
        self.assertEqual(cycle.press_first("builder", over), "")

    def test_which_chroniclers_the_press_watch_stops(self):
        stop = cycle.PRESS_STOP
        self.assertEqual(stop, 1_250_000_000)
        sessions = {"worker:chronicler#1": ("chronicler", 900_000_000),
                    "worker:chronicler-video#2": ("chronicler-video", 1_200_000_000),
                    "worker:chronicler#3": ("chronicler", 1_400_000_000),  # started over the line, told to clean
                    "worker:builder#4": ("builder", 0)}
        self.assertEqual(cycle.chroniclers_to_stop(sessions, stop), [])
        self.assertEqual(cycle.chroniclers_to_stop(sessions, stop + 1),
                         ["worker:chronicler#1", "worker:chronicler-video#2"])
        # The one cleaning up is stopped only if the space grows past where it started.
        self.assertEqual(cycle.chroniclers_to_stop(sessions, 1_300_000_000),
                         ["worker:chronicler#1", "worker:chronicler-video#2"])
        self.assertEqual(cycle.chroniclers_to_stop(sessions, 1_400_000_001),
                         ["worker:chronicler#1", "worker:chronicler#3", "worker:chronicler-video#2"])

    def test_a_stopped_session_says_why_in_its_report(self):
        started = "Started 2026-10-01 10:00 in cycle 7 by chronicler (sonnet).\n"
        self.assertTrue(cycle.ending(started, started, -15, "the press space reached 1.3 GB.")
                        .endswith("without writing a report.\nIt was stopped: the press space reached 1.3 GB.\n"))
        text = cycle.ending("Filmed the lobby.\n", started, -15, "the press space reached 1.3 GB.")
        self.assertTrue(text.startswith("Filmed the lobby.\n\nStopped "))
        self.assertTrue(text.endswith("before it finished: the press space reached 1.3 GB.\n"))
        self.assertIsNone(cycle.ending("Filmed the lobby.\n", started, 0, None))
        self.assertTrue(cycle.ending(started, started, 1, None).endswith("(exit code 1) without writing a report.\n"))

    def test_the_watch_stops_only_chroniclers_and_everything_they_started(self):
        sessions = {}
        for actor, worker in (("worker:chronicler#5", (5, "chronicler", 0)), ("worker:builder#6", (6, "builder", 0))):
            sessions[actor] = (worker, subprocess.Popen(["sleep", "30"], start_new_session=True))
        try:
            with mock.patch.multiple(cycle, WORKING=dict(sessions), STOPPED={}):
                self.assertEqual(cycle.stop_chroniclers(cycle.PRESS_STOP), [])
                self.assertEqual(cycle.stop_chroniclers(cycle.PRESS_STOP + 1), [5])
                self.assertEqual(sessions["worker:chronicler#5"][1].wait(timeout=10), -signal.SIGTERM)
                self.assertIn("press space reached", cycle.STOPPED["worker:chronicler#5"])
                self.assertEqual(cycle.stop_chroniclers(cycle.PRESS_STOP + 1), [])  # once, not every minute
            self.assertIsNone(sessions["worker:builder#6"][1].poll())
        finally:
            for _, process in sessions.values():
                if process.poll() is None:
                    process.kill()
                process.wait()


if __name__ == "__main__":
    unittest.main()
