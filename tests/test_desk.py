"""The front desk server and the vault: pure functions only, standard library only.

Run:  python3 -m unittest discover -s tests
"""
import contextlib
import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "box" / "bin")]

from company import fingerprint  # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


desk = load("desk_server", "desk/server.py")

ASK = "---\ntitle: A Gumroad store\nneeds: STORE_URL, GUMROAD_ACCESS_TOKEN\n---\nA store in your name, and a key that can create products.\n"
FILED = {"a1": "company/asks/store.md"}


def head_asked(log, ident, file, cycle=1):
    """The two lines the outside log holds when the head runs `ask`: its command and the reply."""
    calls = log / "cycles"
    calls.mkdir(parents=True, exist_ok=True)
    rows = [{"actor": "orchestrator", "event": {"message": {"content": [
                {"type": "tool_use", "name": "Bash", "id": f"t{ident}", "input": {"command": f"ask {file}"}}]}}},
            {"actor": "orchestrator", "event": {"message": {"content": [
                {"type": "tool_result", "tool_use_id": f"t{ident}", "content": f"asked {ident}: A Gumroad store. It is on the person's desk."}]}}}]
    with (calls / f"{cycle:04d}.jsonl").open("a") as f:
        f.write("".join(json.dumps(r) + "\n" for r in rows))


class Scratch:
    """Points the desk at a temporary book, vault, workspace and log, and puts them back after."""

    def scratch(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        tmp = pathlib.Path(folder.name)
        paths = {"VAULT": tmp / "vault", "DECISIONS": tmp / "book" / "decisions.jsonl", "WORKSPACE": tmp / "workspace",
                 "ASKS": tmp / "workspace" / ".box" / "asks.jsonl", "LOG": tmp / "log", "FILED_CACHE": {}}
        paths["DECISIONS"].parent.mkdir(parents=True)
        for name, value in paths.items():
            self.addCleanup(setattr, desk, name, getattr(desk, name))
            setattr(desk, name, value)
        return tmp


class TheDesk(Scratch, unittest.TestCase):
    def setUp(self):
        self.text = ASK
        self.filed = dict(FILED)
        self.reads = []
        self.asked = [{"id": "a1", "file": "company/asks/store.md", "title": "A Gumroad store",
                       "needs": ["GUMROAD_ACCESS_TOKEN", "STORE_URL"], "sha256": fingerprint(ASK), "replaces": []}]

    def read(self, name, text=None):
        self.reads.append(name)
        return self.text if text is None else text

    def answer(self, request, text=None):
        line, items, _, why = desk.answer(self.asked, self.filed, request, lambda name: self.read(name, text), 1000.0)
        return line, items, why

    def test_providing_names_the_items_and_never_holds_a_value(self):
        line, items, why = self.answer({"id": "a1", "decision": "provide", "sha256": fingerprint(ASK),
                                        "items": {"GUMROAD_ACCESS_TOKEN": "  tok-123\n", "STORE_URL": "https://x.gumroad.com"}})
        self.assertIsNone(why)
        self.assertEqual((line["decision"], line["names"]), ("provided", ["GUMROAD_ACCESS_TOKEN", "STORE_URL"]))
        self.assertEqual(items["GUMROAD_ACCESS_TOKEN"], "tok-123")
        self.assertNotIn("tok-123", json.dumps(line))

    def test_item_names_cannot_leave_the_vault(self):
        for name in ("../../etc/passwd", "lower", ".HIDDEN", "A", "X" * 60):
            _, _, why = self.answer({"id": "a1", "decision": "provide", "sha256": fingerprint(ASK), "items": {name: "v"}})
            self.assertIsNotNone(why, name)

    def test_empty_values_and_empty_answers_are_refused(self):
        self.assertIsNotNone(self.answer({"id": "a1", "decision": "provide", "sha256": fingerprint(ASK), "items": {"STORE_URL": "  "}})[2])
        self.assertIsNotNone(self.answer({"id": "a1", "decision": "provide", "sha256": fingerprint(ASK), "items": {}})[2])
        line, _, why = self.answer({"id": "a1", "decision": "provide", "sha256": fingerprint(ASK), "note": "I made the store; it is acme.gumroad.com"})
        self.assertIsNone(why)
        self.assertEqual(line["names"], [])

    def test_no_is_an_answer(self):
        line, items, why = self.answer({"id": "a1", "decision": "no", "sha256": fingerprint(ASK), "note": "not this one"})
        self.assertEqual((why, line["decision"], items, line["note"]), (None, "no", {}, "not this one"))

    def test_an_edited_request_cannot_be_answered(self):
        why = self.answer({"id": "a1", "decision": "provide", "sha256": fingerprint(ASK + "x"), "note": "x"}, text=ASK + "x")[2]
        self.assertIn("edited", why)

    def test_the_answer_is_bound_to_what_the_page_showed(self):
        why = self.answer({"id": "a1", "decision": "provide", "sha256": "0" * 64, "note": "x"})[2]
        self.assertIn("changed since this page showed it", why)

    def test_a_replaced_request_is_not_answered(self):
        self.asked.append({**self.asked[0], "id": "a2", "replaces": ["a1"]})
        self.filed["a2"] = "company/asks/store.md"
        self.assertIsNotNone(self.answer({"id": "a1", "decision": "provide", "sha256": fingerprint(ASK), "note": "x"})[2])

    def test_unknown_requests_and_answers_are_refused(self):
        self.assertIsNotNone(self.answer({"id": "a9", "decision": "provide", "sha256": fingerprint(ASK), "note": "x"})[2])
        self.assertIsNotNone(self.answer({"id": "a1", "decision": "yes", "sha256": fingerprint(ASK)})[2])

    def test_taking_back_needs_vault_names(self):
        line, _, why = self.answer({"decision": "withdraw", "names": ["STORE_URL"]})
        self.assertEqual((why, line["decision"], line["names"]), (None, "withdrawn", ["STORE_URL"]))
        self.assertIsNotNone(self.answer({"decision": "withdraw", "names": ["../x"]})[2])
        self.assertIsNotNone(self.answer({"decision": "withdraw"})[2])

    def test_the_vault_holds_the_values_and_the_book_only_the_names(self):
        self.scratch()
        vault, book = desk.VAULT, desk.DECISIONS
        line, items, _ = self.answer({"id": "a1", "decision": "provide", "sha256": fingerprint(ASK), "items": {"STORE_URL": "secret-url"}})
        desk.record(line, items, ASK)
        self.assertEqual((vault / "STORE_URL").read_text(), "secret-url")
        self.assertEqual((vault / "STORE_URL").stat().st_mode & 0o777, 0o600)
        self.assertNotIn("secret-url", book.read_text())
        self.assertEqual(sorted(p.name for p in vault.iterdir()), ["STORE_URL"])
        gone, _, _ = self.answer({"decision": "withdraw", "names": ["STORE_URL"]})
        desk.record(gone, {}, None)
        self.assertFalse((vault / "STORE_URL").exists())

    # Finding 0: a request id the box wrote is never a path, and no value is left without a book line.

    def test_an_id_that_is_not_an_ask_id_is_refused_before_anything_is_read(self):
        for ident in ("../../vault/TOKEN_Y/x", "a1\0", "a" + "1" * 300, "a1/../a2", "A1", "a1 ", 1, None, ["a1"], {"a": 1}):
            self.asked.append({**self.asked[0], "id": ident})
            self.filed[str(ident)] = "company/asks/store.md"
            line, items, why = self.answer({"id": ident, "decision": "provide", "sha256": fingerprint(ASK),
                                            "items": {"STORE_URL": "v"}})
            self.assertEqual((line, items, why), (None, None, "there is no such request"), repr(ident))
        self.assertEqual(self.reads, [])

    def test_the_copy_is_written_before_the_vault(self):
        tmp = self.scratch()
        (tmp / "book" / "asks").write_text("not a folder")
        line, items, _ = self.answer({"id": "a1", "decision": "provide", "sha256": fingerprint(ASK), "items": {"STORE_URL": "secret-url"}})
        with self.assertRaises(OSError):
            desk.record(line, items, ASK)
        self.assertFalse(desk.VAULT.exists() and any(desk.VAULT.iterdir()))
        self.assertFalse(desk.DECISIONS.exists())

    def test_a_book_that_cannot_be_written_leaves_no_value_and_keeps_the_old_one(self):
        self.scratch()
        vault = desk.VAULT
        line, items, _ = self.answer({"id": "a1", "decision": "provide", "sha256": fingerprint(ASK), "items": {"STORE_URL": "old-url"}})
        desk.record(line, items, ASK)
        desk.DECISIONS.rename(desk.DECISIONS.with_name("kept.jsonl"))
        desk.DECISIONS.mkdir()  # appending to a folder fails, after the copy and with values staged
        line, items, _ = self.answer({"id": "a1", "decision": "provide", "sha256": fingerprint(ASK),
                                      "items": {"STORE_URL": "new-url", "GUMROAD_ACCESS_TOKEN": "tok"}})
        with self.assertRaises(OSError):
            desk.record(line, items, ASK)
        self.assertEqual(sorted(p.name for p in vault.iterdir()), ["STORE_URL"])
        self.assertEqual((vault / "STORE_URL").read_text(), "old-url")

    def test_a_value_that_cannot_be_placed_leaves_nothing_staged(self):
        self.scratch()
        vault = desk.VAULT
        (vault / "STORE_URL").mkdir(parents=True)
        line, items, _ = self.answer({"id": "a1", "decision": "provide", "sha256": fingerprint(ASK),
                                      "items": {"GUMROAD_ACCESS_TOKEN": "tok", "STORE_URL": "secret-url"}})
        with self.assertRaises(OSError):
            desk.record(line, items, ASK)
        self.assertEqual(sorted(p.name for p in vault.iterdir()), ["GUMROAD_ACCESS_TOKEN", "STORE_URL"])
        self.assertTrue((vault / "STORE_URL").is_dir())
        self.assertIn('"GUMROAD_ACCESS_TOKEN"', desk.DECISIONS.read_text())

    # Findings 1 and 28: only what the head asked for, in the text the person read.

    def test_only_the_needs_of_the_text_read_can_be_provided(self):
        self.asked[0]["needs"].append("STRIPE_SECRET_KEY")  # a worker edits the needs in asks.jsonl
        self.asked[0]["title"] = "Something else"
        line, _, why = self.answer({"id": "a1", "decision": "provide", "sha256": fingerprint(ASK),
                                    "items": {"STORE_URL": "u", "STRIPE_SECRET_KEY": "sk"}})
        self.assertIsNone(line)
        self.assertIn("does not ask for STRIPE_SECRET_KEY", why)
        line, _, why = self.answer({"id": "a1", "decision": "provide", "sha256": fingerprint(ASK), "items": {"STORE_URL": "u"}})
        self.assertEqual((why, line["title"], line["names"]), (None, "A Gumroad store", ["STORE_URL"]))

    def test_a_request_that_needs_nothing_takes_a_note_and_no_values(self):
        text = "---\ntitle: Pick a name\n---\nWhich of these names do you like?\n"
        self.asked[0]["sha256"] = fingerprint(text)
        why = self.answer({"id": "a1", "decision": "provide", "sha256": fingerprint(text), "items": {"STORE_URL": "u"}}, text=text)[2]
        self.assertIn("does not ask for STORE_URL", why)
        line, _, why = self.answer({"id": "a1", "decision": "provide", "sha256": fingerprint(text), "note": "the second"}, text=text)
        self.assertEqual((why, line["names"]), (None, []))

    def test_an_ask_the_log_does_not_show_the_head_filing_is_refused(self):
        self.filed = {}
        why = self.answer({"id": "a1", "decision": "provide", "sha256": fingerprint(ASK), "items": {"STORE_URL": "u"}})[2]
        self.assertIn("log shows no ask from the head", why)
        self.filed = {"a1": "company/asks/other.md"}  # the head asked with another file
        why = self.answer({"id": "a1", "decision": "provide", "sha256": fingerprint(ASK), "items": {"STORE_URL": "u"}})[2]
        self.assertIn("log shows no ask from the head", why)
        self.assertEqual(self.reads, [])

    def test_the_first_line_for_an_id_is_the_one_that_counts(self):
        forged = "---\ntitle: Payouts\nneeds: STRIPE_SECRET_KEY\n---\nA key for payouts.\n"
        self.asked.append({"id": "a1", "file": "company/asks/payouts.md", "sha256": fingerprint(forged)})
        self.filed["a1"] = "company/asks/payouts.md"
        why = self.answer({"id": "a1", "decision": "provide", "sha256": fingerprint(forged), "items": {"STRIPE_SECRET_KEY": "sk"}}, text=forged)[2]
        self.assertIn("log shows no ask from the head", why)
        self.filed = dict(FILED)
        self.assertIsNone(self.answer({"id": "a1", "decision": "provide", "sha256": fingerprint(ASK), "items": {"STORE_URL": "u"}})[2])

    def test_only_a_filed_ask_can_replace_another(self):
        self.asked.append({"id": "a7", "file": "company/x.md", "replaces": ["a1"]})  # never filed
        self.assertIsNone(self.answer({"id": "a1", "decision": "no", "sha256": fingerprint(ASK)})[2])
        self.filed["a7"] = "company/x.md"
        self.assertIn("newer version", self.answer({"id": "a1", "decision": "no", "sha256": fingerprint(ASK)})[2])

    def test_junk_in_asks_jsonl_never_blocks_answering_the_rest(self):
        self.asked[0]["replaces"] = 7
        self.asked[0]["needs"] = "STORE_URL"
        self.asked[:0] = [{"id": ["a1"]}, {"id": {"x": 1}}, {"id": 3, "replaces": [["a1"]]}, {"id": None}]
        self.asked += [{"id": "a2", "file": "company/asks/store.md", "replaces": [["a1"], {"a": 1}, 4], "needs": 5},
                       {"id": "a3", "file": ["company/asks/store.md"], "replaces": "a1", "sha256": ["x"]},
                       {"id": "a4", "file": "company/asks/store.md", "replaces": None}]
        self.filed.update(a2="company/asks/store.md", a3="company/asks/store.md", a4="company/asks/store.md")
        line, items, why = self.answer({"id": "a1", "decision": "provide", "sha256": fingerprint(ASK), "items": {"STORE_URL": "u"}})
        self.assertEqual((why, items), (None, {"STORE_URL": "u"}))
        self.assertIn("edited", self.answer({"id": "a2", "decision": "no", "sha256": fingerprint(ASK)})[2])

    def test_the_desk_reads_the_head_asks_from_the_outside_log(self):
        self.scratch()
        self.assertEqual(desk.asks_filed(), {})
        head_asked(desk.LOG, "a1", "company/asks/store.md")
        self.assertEqual(desk.asks_filed(), {"a1": "company/asks/store.md"})
        head_asked(desk.LOG, "a2", "/workspace/company/asks/other.md", cycle=2)
        self.assertEqual(desk.asks_filed(), {"a1": "company/asks/store.md", "a2": "company/asks/other.md"})
        self.assertTrue(desk.FILED_CACHE["offsets"])

    # Finding 6: the copy in the book is the text that was verified, read once.

    def test_the_copy_is_the_text_that_was_verified(self):
        self.scratch()
        texts = iter([ASK, ASK.replace("A store", "Another store")])
        line, items, text, why = desk.answer(self.asked, self.filed, {"id": "a1", "decision": "provide", "sha256": fingerprint(ASK),
                                             "items": {"STORE_URL": "u"}}, lambda name: next(texts), 1000.0)
        self.assertEqual((why, text), (None, ASK))
        desk.record(line, items, text)
        copy = (desk.DECISIONS.parent / "asks" / "a1.md").read_text()
        self.assertEqual((copy, fingerprint(copy)), (ASK, line["sha256"]))
        self.assertEqual(next(texts), ASK.replace("A store", "Another store"))  # read exactly once


class TheDeskOverHttp(Scratch, unittest.TestCase):
    """do_POST end to end, without a socket: the request in, the answer and the vault out."""

    def setUp(self):
        tmp = self.scratch()
        request = tmp / "workspace" / "company" / "asks" / "store.md"
        request.parent.mkdir(parents=True)
        request.write_text(ASK)
        desk.ASKS.parent.mkdir(parents=True)
        desk.ASKS.write_text(json.dumps({"id": "a1", "file": "company/asks/store.md", "title": "A Gumroad store",
                                         "needs": ["GUMROAD_ACCESS_TOKEN", "STORE_URL"], "sha256": fingerprint(ASK),
                                         "replaces": []}) + "\n" + "{not json\n" + json.dumps({"id": 5, "replaces": 9}) + "\n")

    def post(self, body):
        data = json.dumps(body).encode()
        handler = desk.Handler.__new__(desk.Handler)
        handler.headers = {"Host": "127.0.0.1:8772", "Origin": "http://127.0.0.1:8771", "X-Desk": "owner",
                           "Content-Type": "application/json", "Content-Length": str(len(data))}
        handler.rfile, handler.wfile = io.BytesIO(data), io.BytesIO()
        handler.path, handler.command, handler.request_version = "/decide", "POST", "HTTP/1.1"
        handler.requestline, handler.client_address = "POST /decide HTTP/1.1", ("127.0.0.1", 0)
        with contextlib.redirect_stdout(io.StringIO()):
            handler.do_POST()
        head, _, reply = handler.wfile.getvalue().partition(b"\r\n\r\n")
        return int(head.split()[1]), json.loads(reply)

    def test_an_ask_only_asks_jsonl_shows_is_not_answered(self):
        code, reply = self.post({"id": "a1", "decision": "provide", "sha256": fingerprint(ASK), "items": {"STORE_URL": "secret-url"}})
        self.assertEqual(code, 409)
        self.assertIn("log shows no ask from the head", reply["error"])
        self.assertFalse(desk.VAULT.exists() and any(desk.VAULT.iterdir()))
        self.assertFalse(desk.DECISIONS.exists())

    def test_an_ask_the_head_filed_fills_the_vault_and_the_book(self):
        head_asked(desk.LOG, "a1", "company/asks/store.md")
        code, reply = self.post({"id": "a1", "decision": "provide", "sha256": fingerprint(ASK), "items": {"STORE_URL": "secret-url"}})
        self.assertEqual((code, reply["answer"]["names"]), (200, ["STORE_URL"]))
        self.assertEqual((desk.VAULT / "STORE_URL").read_text(), "secret-url")
        self.assertEqual(json.loads(desk.DECISIONS.read_text())["id"], "a1")
        self.assertEqual((desk.DECISIONS.parent / "asks" / "a1.md").read_text(), ASK)
        code, reply = self.post({"id": "a1", "decision": "provide", "sha256": fingerprint(ASK), "items": {"STRIPE_SECRET_KEY": "sk"}})
        self.assertEqual(code, 409)
        self.assertFalse((desk.VAULT / "STRIPE_SECRET_KEY").exists())


if __name__ == "__main__":
    unittest.main()
