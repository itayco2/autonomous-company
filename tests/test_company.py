"""The fast tier: pure functions only, standard library only, no Docker and no model.

Run:  python3 -m unittest discover -s tests
"""
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "box" / "bin"), str(ROOT / "door")]

import company  # noqa: E402
from company import (MAX_TOGETHER, MODELS, ORCHESTRATOR_EFFORT, ORCHESTRATOR_MODEL, parse_role,  # noqa: E402
                     read_queue, write_atomic)
from proxy import decide, origin_form, public  # noqa: E402

ALLOW = frozenset({"api.anthropic.com", "registry.npmjs.org"})


def role(model, effort, body="You work."):
    return f"---\nmodel: {model}\neffort: {effort}\n---\n{body}\n"


class RoleFiles(unittest.TestCase):
    def test_the_orchestrator_is_opus_5_5_at_max_effort(self):
        self.assertEqual((ORCHESTRATOR_MODEL, ORCHESTRATOR_EFFORT), ("claude-opus-5-5", "max"))

    def test_short_names_become_exact_model_ids(self):
        self.assertEqual(parse_role(role("sonnet", "high")), ("claude-sonnet-5-5", "high", "You work."))
        self.assertEqual(parse_role(role("Opus", "MAX"))[:2], ("claude-opus-5-5", "max"))

    def test_full_ids_are_accepted(self):
        for model_id in ("claude-opus-5-5", "claude-sonnet-5-5"):
            self.assertEqual(parse_role(role(model_id, "low"))[0], model_id)
        self.assertEqual(parse_role(role("claude-haiku-4-5", "none"))[0], "claude-haiku-4-5")

    def test_haiku_has_no_effort_setting(self):
        # The CLI drops an effort for Haiku 4.5 silently, so a role may not claim one.
        self.assertEqual(parse_role(role("haiku", "none"))[:2], ("claude-haiku-4-5", None))
        self.assertEqual(parse_role("---\nmodel: haiku\n---\nx")[:2], ("claude-haiku-4-5", None))
        for level in ("low", "max"):
            with self.assertRaisesRegex(ValueError, "not available for claude-haiku-4-5"):
                parse_role(role("haiku", level))

    def test_fable_is_refused_by_name_and_by_id(self):
        # Fable is billed to usage credits in headless mode, which would end the zero-spend rule.
        for model in ("fable", "claude-fable-5-1", "claude-fable-5"):
            with self.assertRaisesRegex(ValueError, "not available"):
                parse_role(role(model, "high"))

    def test_the_allowlist_matches_the_managed_settings(self):
        import json
        managed = json.loads((ROOT / "box" / "managed-settings.json").read_text())
        self.assertEqual(sorted(managed["availableModels"]), sorted(MODELS.values()))
        self.assertTrue(managed["enforceAvailableModels"])

    def test_a_flag_smuggled_into_the_model_is_refused(self):
        with self.assertRaises(ValueError):
            parse_role(role("sonnet --dangerously-skip-permissions", "high"))

    def test_unknown_effort_and_missing_fields_are_refused(self):
        for text in (role("sonnet", "ultra"), "---\nmodel: sonnet\n---\nx", "---\neffort: low\n---\nx"):
            with self.assertRaises(ValueError):
                parse_role(text)

    def test_a_role_without_a_header_is_refused_with_the_format(self):
        for text in ("You have no header.", "", "---\nmodel: haiku\neffort: none\nno closing line"):
            with self.assertRaisesRegex(ValueError, "model: sonnet"):
                parse_role(text)

    def test_an_oversized_or_binary_role_is_refused_before_it_reaches_the_command_line(self):
        for text in (role("sonnet", "high", "x" * 40_000), role("sonnet", "high", "a\0b")):
            with self.assertRaisesRegex(ValueError, "plain text"):
                parse_role(text)

    def test_only_the_header_is_stripped(self):
        body = "Line one.\n---\nA rule inside the instructions stays."
        self.assertEqual(parse_role(role("sonnet", "low", body))[2], body)


class SideBySide(unittest.TestCase):
    from company import ready_to_start
    start = staticmethod(ready_to_start)

    def entries(self, *waits):
        return [{"id": i + 1, "after": list(w)} for i, w in enumerate(waits)]

    def ids(self, entries):
        return [e["id"] for e in entries]

    def test_independent_tasks_start_together_in_the_order_assigned(self):
        self.assertEqual(self.ids(self.start(self.entries((), (), ()), set(), {})), [1, 2, 3])

    def test_a_task_waits_for_what_it_names(self):
        pending = self.entries((), (), (1, 2))
        self.assertEqual(self.ids(self.start(pending, set(), {})), [1, 2])
        self.assertEqual(self.ids(self.start(pending[2:], {1}, {2: "running"})), [])
        self.assertEqual(self.ids(self.start(pending[2:], {1, 2}, {})), [3])

    def test_a_later_independent_task_is_not_held_up_by_a_waiting_one(self):
        pending = self.entries((), (1,), ())
        self.assertEqual(self.ids(self.start(pending, set(), {})), [1, 3])

    def test_the_machine_limit_holds(self):
        pending = self.entries(*[()] * (MAX_TOGETHER + 4))
        self.assertEqual(len(self.start(pending, set(), {})), MAX_TOGETHER)
        self.assertEqual(len(self.start(pending, set(), {n: 1 for n in range(100, 100 + MAX_TOGETHER - 1)})), 1)

    def test_a_wait_on_a_number_not_in_this_cycle_does_not_block_forever(self):
        self.assertEqual(self.ids(self.start(self.entries((), (9,)), set(), {})), [1, 2])


class Queue(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())

    def test_a_torn_line_is_skipped_not_fatal(self):
        path = self.dir / "q.jsonl"
        good = '{"role": "dev", "task": "company/t.md", "report": "company/reports/r.md"}'
        path.write_text(good + "\n" + '{"role": "dev", "ta' + "\n" + "[1, 2]\n" + good + "\n")
        entries, unreadable = read_queue(path)
        self.assertEqual((len(entries), unreadable), (2, 2))

    def test_a_missing_queue_is_empty(self):
        self.assertEqual(read_queue(self.dir / "none.jsonl"), ([], 0))

    def test_a_line_nested_too_deep_is_skipped_not_fatal(self):
        # json.loads raises RecursionError, not ValueError, on a line nested past Python's limit.
        path = self.dir / "deep.jsonl"
        good = '{"role": "dev", "task": "company/t.md", "report": "company/reports/r.md"}'
        path.write_text("[" * 100_000 + "\n" + good + "\n")
        self.assertEqual(read_queue(path)[1], 1)
        self.assertEqual(len(company.read_jsonl(path)), 1)

    def test_replaced_asks_are_read_from_any_well_formed_entry(self):
        asked = [{"id": "a2", "replaces": ["a1"]}, {"id": "a4", "replaces": ["a2", "a3"]},
                 {"replaces": "a9"}, {"replaces": [["a8"], 7, None]}, {"replaces": None}, {}]
        self.assertEqual(company.replaced_ids(asked), {"a1", "a2", "a3"})

    def test_the_press_space_holds_the_press_room_too(self):
        # The chronicler copies every press file into view/press/; the budget counts that copy.
        self.assertEqual(company.PRESS_SPACE, (company.WORKSPACE / "press", company.WORKSPACE / "view" / "press",
                                               company.COMPANY / "work" / "chronicler"))

    def test_atomic_write_leaves_no_temporary_file(self):
        path = self.dir / "sub" / "report.md"
        write_atomic(path, "one")
        write_atomic(path, "two")
        self.assertEqual(path.read_text(), "two")
        self.assertEqual([p.name for p in path.parent.iterdir()], ["report.md"])

    def test_report_paths_outside_reports_are_refused(self):
        original = company.WORKSPACE, company.REPORTS
        company.WORKSPACE, company.REPORTS = self.dir, self.dir / "company" / "reports"
        try:
            self.assertIsNotNone(company.report_path({"report": "company/reports/c0001-01-t.md"}))
            for bad in ("company/roles/x.md", "company/reports/../../../etc/passwd", "/etc/passwd", ""):
                self.assertIsNone(company.report_path({"report": bad}), bad)
        finally:
            company.WORKSPACE, company.REPORTS = original


class FiledAsks(unittest.TestCase):
    """The proof the desk and the office both rely on: the head's own `ask` in the outside log."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.log = pathlib.Path(tmp.name)
        (self.log / "cycles").mkdir()

    def write(self, *rows):
        import json
        with (self.log / "cycles" / "0001.jsonl").open("a") as f:
            f.write("".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in rows))

    @staticmethod
    def asked(ident, command, reply, actor="orchestrator", error=False):
        use = {"type": "tool_use", "name": "Bash", "id": ident, "input": {"command": command}}
        result = {"type": "tool_result", "tool_use_id": ident, "content": reply, "is_error": error}
        return ({"actor": actor, "event": {"type": "assistant", "message": {"content": [use]}}},
                {"actor": actor, "event": {"type": "user", "message": {"content": [result]}}})

    def test_every_spelling_of_a_path_names_the_file_ask_recorded(self):
        # ask.py records the resolved path under /workspace, so the proof must name the same file.
        for n, typed in enumerate(("company/asks/x.md", "/workspace/company/asks/x.md", "company/./asks//x.md")):
            self.write(*self.asked(f"t{n}", f"ask {typed}", f"asked a{n + 1}: A store. It is on the person's desk."))
        self.assertEqual(company.filed_asks(self.log, {}), {f"a{n}": "company/asks/x.md" for n in (1, 2, 3)})
        self.assertEqual(company.filed_name("company/workspace/x.md"), "company/workspace/x.md")

    def test_only_the_heads_asks_that_came_back_count(self):
        self.write(*self.asked("w", "ask company/asks/x.md", "asked a1: A store.", actor="worker:poster#2"),
                   *self.asked("e", "ask company/asks/y.md", "asked a2: refused", error=True),
                   *self.asked("h", "ask company/asks/z.md", "asked a3: A domain."))
        self.assertEqual(company.filed_asks(self.log, {}), {"a3": "company/asks/z.md"})

    def test_a_line_nested_too_deep_does_not_hide_the_asks_after_it(self):
        cache = {}
        self.write('{"actor": "orchestrator", "x": "ask asked a", "y": ' + "[" * 100_000 + "\n")
        self.assertEqual(company.filed_asks(self.log, cache), {})
        self.write(*self.asked("h", "ask company/asks/z.md", "asked a1: A domain."))
        self.assertEqual(company.filed_asks(self.log, cache), {"a1": "company/asks/z.md"})


def load_hook():
    import importlib.util
    spec = importlib.util.spec_from_file_location("only_assign", ROOT / "box" / "bin" / "only-assign.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.decide


class OrchestratorHook(unittest.TestCase):
    decide = staticmethod(load_hook())

    def bash(self, command):
        return self.decide({"tool_name": "Bash", "tool_input": {"command": command}})

    def test_a_plain_assign_passes(self):
        for command in ("assign developer company/tasks/fib.md", "assign qa-2 /workspace/company/t/a_b.md",
                        "  assign dev company/tasks/x.md  ", "assign dev company/tasks/x.md after 2",
                        "assign inspector company/tasks/review.md after 2,3,10"):
            self.assertIsNone(self.bash(command), command)

    def test_every_other_shape_is_denied(self):
        for command in ("ls /workspace", "git -C /workspace status", "cat company/x.md",
                        "assign dev company/t.md; touch x", "assign dev company/t.md && ls",
                        "assign dev company/t.md > /workspace/x", "assign $(id) company/t.md",
                        "assign dev company/../outside.md", "assign dev outside.md", "assign Dev company/t.md",
                        "assign dev company/t.md\nls", "assign dev 'company/a b.md'", "",
                        "assign dev company/t.md after 2; ls", "assign dev company/t.md after $(id)",
                        "assign dev company/t.md after", "assign dev company/t.md before 2"):
            self.assertIsNotNone(self.bash(command), command)

    def test_other_tools_are_left_to_the_permission_rules(self):
        self.assertIsNone(self.decide({"tool_name": "Write", "tool_input": {"file_path": "/etc/x"}}))


class Door(unittest.TestCase):
    def test_allowlisted_hosts_pass_on_443_only(self):
        self.assertEqual(decide("CONNECT api.anthropic.com:443 HTTP/1.1", ALLOW)[0], True)
        self.assertEqual(decide("CONNECT api.anthropic.com:80 HTTP/1.1", ALLOW)[3], "port")

    def test_lookalike_hosts_are_refused(self):
        for host in ("evil.api.anthropic.com", "api.anthropic.com.evil.test", "anthropic.com", "160.79.104.10"):
            self.assertEqual(decide(f"CONNECT {host}:443 HTTP/1.1", ALLOW)[3], "host", host)

    def test_case_and_trailing_dot_do_not_change_the_answer(self):
        self.assertTrue(decide("CONNECT API.Anthropic.COM.:443 HTTP/1.1", ALLOW)[0])

    def test_open_mode_lets_any_host_through_to_the_address_check(self):
        for line in ("CONNECT github.com:443 HTTP/1.1", "CONNECT cdn.playwright.dev:443 HTTP/1.1",
                     "GET http://deb.debian.org/debian/dists HTTP/1.1", "CONNECT github.com:22 HTTP/1.1"):
            self.assertTrue(decide(line, ALLOW, "open")[0], line)
        self.assertFalse(decide("GET https://github.com/ HTTP/1.1", ALLOW, "open")[0])
        self.assertFalse(decide("CONNECT github.com:0 HTTP/1.1", ALLOW, "open")[0])
        # The shut list is made of names, so open mode wants a name: a bare address is refused.
        self.assertEqual(decide("CONNECT 140.82.112.3:22 HTTP/1.1", ALLOW, "open")[3], "address-literal")

    def test_only_public_addresses_count_as_the_internet(self):
        for address in ("8.8.8.8", "140.82.112.3", "151.101.0.223"):
            self.assertTrue(public(address), address)
        # The owner's home network, the Mac through Docker, the box's own network, cloud metadata,
        # loopback, carrier-grade NAT, multicast and junk.
        for address in ("192.168.1.1", "10.0.0.5", "172.20.0.3", "192.168.65.254", "169.254.169.254",
                        "127.0.0.1", "100.64.0.1", "0.0.0.0", "224.0.0.1", "::1", "not-an-ip"):
            self.assertFalse(public(address), address)

    def test_a_plain_http_request_is_rewritten_for_one_answer(self):
        head = b"GET http://deb.debian.org/debian/x?y=1 HTTP/1.1\r\nHost: deb.debian.org\r\nProxy-Connection: keep-alive\r\n\r\n"
        out = origin_form(head)
        self.assertTrue(out.startswith(b"GET /debian/x?y=1 HTTP/1.1\r\n"))
        self.assertIn(b"Host: deb.debian.org", out)
        self.assertNotIn(b"keep-alive", out)
        self.assertTrue(out.endswith(b"Connection: close\r\n\r\n"))

    def test_everything_but_connect_is_refused(self):
        self.assertEqual(decide("GET http://api.anthropic.com/ HTTP/1.1", ALLOW)[3], "not-connect")
        self.assertEqual(decide("garbage", ALLOW)[3], "malformed")
        self.assertEqual(decide("CONNECT api.anthropic.com HTTP/1.1", ALLOW)[3], "malformed")


if __name__ == "__main__":
    unittest.main()
