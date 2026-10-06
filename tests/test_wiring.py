"""The wiring: compose.yaml's mounts, how the checks set up their scratch desk, and what check 3b
counts as proof. Read as text or run with stand-ins; no Docker, no make and no model.

Run:  python3 -m unittest discover -s tests
"""
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "box" / "bin"), str(ROOT / "checks")]

from company import ASK_ID, ITEM_NAME, parse_ask  # noqa: E402

# boxrun points the checks at a scratch book and vault the moment it is imported; given folders of
# our own, it leaves the project's log/ alone, and patch.dict puts the environment back afterwards.
_IMPORT_DESK = tempfile.TemporaryDirectory()


def _no_docker(*args, **kwargs):
    raise AssertionError("a check started Docker when it was imported; its work belongs in main()")


with mock.patch.dict(os.environ, {"DESK_BOOK": _IMPORT_DESK.name, "DESK_VAULT": _IMPORT_DESK.name}):
    import boxrun  # noqa: E402
    # compose.yaml has a fixed project name, so a check that ran on import would act on the live company.
    with mock.patch.multiple(boxrun, shell=_no_docker, desk=_no_docker, reset=_no_docker, box=_no_docker):
        import roles  # noqa: E402

BOOK = "${DESK_BOOK:-./desk/book}"
VAULT = "${DESK_VAULT:-./desk/vault}"


def services():
    """{service: {"volumes": [...], "networks": "...", "lines": [...]}} from compose.yaml, read as text."""
    found, current, in_services, in_volumes = {}, None, False, False
    for line in (ROOT / "compose.yaml").read_text().splitlines():
        if re.match(r"^\S", line):
            current, in_services = None, line.rstrip() == "services:"
        head = re.match(r"^  ([a-z-]+):\s*$", line) if in_services else None
        if head:
            current, in_volumes = found.setdefault(head.group(1), {"volumes": [], "networks": "", "lines": []}), False
            continue
        if current is None:
            continue
        current["lines"].append(line)
        if re.match(r"^    volumes:\s*$", line):
            in_volumes = True
        elif re.match(r"^    \S", line):
            in_volumes = False
            if line.strip().startswith("networks:"):
                current["networks"] = line
        elif in_volumes and line.strip().startswith("- "):
            current["volumes"].append(line.strip()[2:])
    return found


def recipe(target):
    """The commands of a Makefile target, with continued lines joined."""
    lines = (ROOT / "Makefile").read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{target}:"))
    commands, current = [], ""
    for line in lines[start + 1:]:
        if not line.startswith("\t"):
            break
        current += line.strip().rstrip("\\") + " "
        if not line.endswith("\\"):
            commands.append(current.strip())
            current = ""
    return commands


class Compose(unittest.TestCase):
    def setUp(self):
        self.services = services()

    def test_the_box_reads_the_book_and_vault_read_only_and_the_checks_can_move_them(self):
        volumes = self.services["box"]["volumes"]
        self.assertIn(f"{BOOK}:/opt/company/desk:ro", volumes)
        self.assertIn(f"{VAULT}:/opt/company/vault:ro", volumes)

    def test_the_desk_writes_the_same_book_and_vault(self):
        volumes = self.services["desk"]["volumes"]
        self.assertIn(f"{BOOK}:/opt/company/desk", volumes)
        self.assertIn(f"{VAULT}:/opt/company/vault", volumes)

    def test_nothing_mounts_the_owners_book_or_vault_past_the_variables(self):
        # A bare ./desk/book anywhere would put the owner's real answers under a check.
        for name, service in self.services.items():
            for volume in service["volumes"]:
                self.assertFalse(volume.startswith(("./desk/book", "./desk/vault")), f"{name}: {volume}")

    def test_only_the_box_and_the_desk_hold_the_vault(self):
        # The office is on the box's network and the box can read its state, so the vault stays out of it.
        holders = {name for name, s in self.services.items() if any(v.startswith(VAULT) for v in s["volumes"])}
        self.assertEqual(holders, {"box", "desk"})

    def test_the_office_reads_the_book_and_imports_the_company_rules(self):
        volumes = self.services["office"]["volumes"]
        self.assertIn(f"{BOOK}:/desk:ro", volumes)
        self.assertIn("./box/bin:/opt/company/bin:ro", volumes)

    def test_the_desk_reads_the_outside_log_and_cannot_write_it(self):
        self.assertIn("./log:/log:ro", self.services["desk"]["volumes"])
        self.assertIn("./box/bin:/opt/company/bin:ro", self.services["desk"]["volumes"])

    def test_the_desk_stays_off_the_box_network(self):
        self.assertNotIn("box", re.findall(r"[a-z]+", self.services["desk"]["networks"].split(":", 1)[1]))

    def test_the_door_says_to_restart_after_its_list_is_edited(self):
        lines = self.services["door"]["lines"]
        mount = next(i for i, line in enumerate(lines) if "./door/shut.txt:/door/shut.txt" in line)
        comment = " ".join(line.strip() for line in lines[:mount] if line.strip().startswith("#"))
        self.assertIn("docker compose restart door", comment)


class Makefile(unittest.TestCase):
    def setUp(self):
        self.text = (ROOT / "Makefile").read_text()
        self.checks = recipe("checks")

    def test_the_scratch_desk_is_under_log_checks(self):
        check_desk = re.search(r"^CHECK_DESK := (.*)$", self.text, re.M).group(1)
        self.assertEqual(check_desk, "DESK_BOOK=./log/checks/desk-book DESK_VAULT=./log/checks/desk-vault")

    def test_the_scratch_desk_starts_empty_and_the_vault_closed_like_the_owners(self):
        first = self.checks[0]
        self.assertIn("rm -rf log/checks/desk-book log/checks/desk-vault", first)
        self.assertIn("mkdir -p -m 700 log/checks/desk-vault", first)

    def test_office_and_desk_start_on_the_owners_book_before_the_walls(self):
        walls = next(i for i, c in enumerate(self.checks) if "checks/walls.py" in c)
        command = self.checks[walls]
        up = command.index("docker compose up -d --no-deps office desk")
        run = command.index("docker compose run")
        self.assertLess(up, run)
        # The real office and desk stay up after the checks, so they must never get the scratch desk.
        self.assertNotIn("DESK_", command[:up])
        self.assertNotIn("CHECK_DESK", command[:command.index("if (")])

    def test_the_walls_run_only_after_the_desk_answers_on_the_mac(self):
        command = next(c for c in self.checks if "checks/walls.py" in c)
        self.assertLess(command.index("$(DESK_ANSWERS)"), command.index("docker compose run"))
        answers = re.search(r"^DESK_ANSWERS := (.*)$", self.text, re.M).group(1)
        self.assertIn("http://127.0.0.1:8772/health", answers)
        self.assertIn("Origin: http://127.0.0.1:8771", answers)
        self.assertIn("RESULT FAIL", command.split("else", 1)[1])

    def test_every_check_runs_on_the_scratch_desk(self):
        for script in ("walls", "roles", "halt"):
            command = next(c for c in self.checks if f"checks/{script}.py" in c)
            self.assertIn("$(CHECK_DESK)", command, script)


@unittest.skipUnless(shutil.which("make"), "make is not installed")
class MakefilePlatform(unittest.TestCase):
    """What `make -n` would run, with uname replaced by a stand-in that names the system."""

    def plan(self, system, *targets):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        uname = pathlib.Path(folder.name) / "uname"
        uname.write_text(f"#!/bin/sh\necho {system}\n")
        uname.chmod(0o755)
        env = {k: v for k, v in os.environ.items() if k != "MAKEFLAGS"}
        env["PATH"] = f"{folder.name}:{env.get('PATH', '/usr/bin:/bin')}"
        done = subprocess.run(["make", "-n", "-C", str(ROOT), *targets], env=env, capture_output=True,
                              text=True, timeout=30)
        return done.stdout

    def test_macos_keeps_the_machine_awake_and_opens_with_open(self):
        out = self.plan("Darwin", "start", "view")
        self.assertIn("caffeinate -is python3 heartbeat.py --share 20", out)
        self.assertIn("open http://127.0.0.1:8770", out)

    def test_linux_runs_the_heartbeat_directly_and_opens_with_xdg_open(self):
        out = self.plan("Linux", "start", "view", "office")
        self.assertNotIn("caffeinate", out)
        self.assertIn("python3 heartbeat.py --share 20", out)
        self.assertIn("xdg-open http://127.0.0.1:8770", out)
        self.assertIn("xdg-open http://127.0.0.1:8771", out)


@unittest.skipUnless(shutil.which("make"), "make is not installed")
class MakefileDoorMode(unittest.TestCase):
    """The Makefile's DOOR_MODE line, evaluated by make in a scratch folder with its own .env."""

    def mode(self, env_file=None, door_mode=None):
        line = next(l for l in (ROOT / "Makefile").read_text().splitlines() if l.startswith("DOOR_MODE ?="))
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        tree = pathlib.Path(folder.name)
        (tree / "Makefile").write_text(line + "\nshow:\n\t@echo $(DOOR_MODE)\n")
        if env_file is not None:
            (tree / ".env").write_text(env_file)
        env = {k: v for k, v in os.environ.items() if k not in ("DOOR_MODE", "MAKEFLAGS")}
        if door_mode is not None:
            env["DOOR_MODE"] = door_mode
        done = subprocess.run(["make", "-s", "show"], cwd=tree, env=env, capture_output=True, text=True, timeout=30)
        return done.stdout.strip()

    def test_open_by_default_never_the_compose_text(self):
        self.assertEqual(self.mode(), "open")

    def test_env_sets_it_with_quotes_and_comments_stripped(self):
        self.assertEqual(self.mode("DOOR_MODE=allowlist\n"), "allowlist")
        self.assertEqual(self.mode("DOOR_MODE='allowlist' # tight\n"), "allowlist")

    def test_the_shell_wins_over_env(self):
        self.assertEqual(self.mode("DOOR_MODE=open\n", door_mode="allowlist"), "allowlist")

    def test_the_walls_check_is_told_that_mode(self):
        walls = next(c for c in recipe("checks") if "checks/walls.py" in c)
        self.assertIn('python3 - "$(DOOR_MODE)" < checks/walls.py', walls)


class WithToken(unittest.TestCase):
    """with-token.sh run for real, with docker, curl, python3 and sleep replaced by stand-ins that
    only write down how they were called. The real docker is not even on the PATH."""

    def run_script(self, desk_answers=True, env_file=None, door_mode=None):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        tree = pathlib.Path(folder.name)
        (tree / "checks").mkdir()
        (tree / "checks" / "with-token.sh").write_text((ROOT / "checks" / "with-token.sh").read_text())
        (tree / "checks" / "walls.py").write_text("")
        (tree / "compose.yaml").write_text("      DOOR_MODE: open\n")
        (tree / "log" / "checks" / "desk-vault").mkdir(parents=True)
        (tree / "log" / "checks" / "desk-vault" / "STALE").write_text("left over")
        stubs, calls = tree / "stubs", tree / "calls.txt"
        stubs.mkdir()
        record = f'echo "$(basename "$0") $* | DESK_BOOK=${{DESK_BOOK-unset}} DESK_VAULT=${{DESK_VAULT-unset}}" >> "{calls}"\n'
        stub = {"docker": record + 'case "$*" in *"python3 -"*) echo "RESULT PASS";; esac\n',
                "curl": record + ("exit 0\n" if desk_answers else "exit 7\n"),
                "python3": record, "sleep": record}
        for name, body in stub.items():
            (stubs / name).write_text("#!/bin/sh\n" + body)
            (stubs / name).chmod(0o755)
        env = {"PATH": f"{stubs}:/usr/bin:/bin", "HOME": str(tree)}
        if env_file is not None:
            (tree / ".env").write_text(env_file)
        if door_mode is not None:
            env["DOOR_MODE"] = door_mode
        done = subprocess.run(["/bin/sh", str(tree / "checks" / "with-token.sh")], cwd=tree, env=env,
                              capture_output=True, text=True, timeout=60)
        return tree, done.stdout, calls.read_text().splitlines()

    def walls_mode(self, **kwargs):
        """The mode the walls check was told, read off the stand-in docker's record of the call."""
        _, _, calls = self.run_script(**kwargs)
        walls = next(c for c in calls if c.startswith("docker compose run"))
        return re.search(r"python3 - (\S+)", walls).group(1)

    def test_the_walls_hear_open_when_nothing_sets_the_door_mode(self):
        self.assertEqual(self.walls_mode(), "open")

    def test_the_walls_hear_the_mode_written_in_env_quotes_and_comments_aside(self):
        self.assertEqual(self.walls_mode(env_file="DOOR_MODE=allowlist\n"), "allowlist")
        self.assertEqual(self.walls_mode(env_file='DOOR_MODE="allowlist"  # tight\n'), "allowlist")
        self.assertEqual(self.walls_mode(env_file="# DOOR_MODE=allowlist\n"), "open")

    def test_a_door_mode_in_the_shell_wins_over_env_as_it_does_for_compose(self):
        self.assertEqual(self.walls_mode(env_file="DOOR_MODE=open\n", door_mode="allowlist"), "allowlist")

    def test_office_and_desk_start_first_on_the_owners_book(self):
        _, out, calls = self.run_script()
        self.assertTrue(calls[0].startswith("docker compose up -d --no-deps office desk"), calls[0])
        self.assertIn("DESK_BOOK=unset DESK_VAULT=unset", calls[0])
        self.assertTrue(calls[1].startswith("curl "), calls[1])
        self.assertIn("http://127.0.0.1:8772/health", calls[1])
        self.assertIn("CHECK 2 PASS", out)

    def test_the_walls_and_halt_run_on_the_scratch_desk(self):
        _, _, calls = self.run_script()
        walls = next(c for c in calls if c.startswith("docker compose run"))
        self.assertIn("DESK_BOOK=./log/checks/desk-book DESK_VAULT=./log/checks/desk-vault", walls)
        halt = next(c for c in calls if "checks/halt.py" in c)
        self.assertIn("DESK_BOOK=./log/checks/desk-book DESK_VAULT=./log/checks/desk-vault", halt)

    def test_a_desk_that_does_not_answer_fails_the_walls_without_running_them(self):
        _, out, calls = self.run_script(desk_answers=False)
        self.assertIn("CHECK 2 FAIL: the front desk does not answer on the Mac", out)
        self.assertFalse([c for c in calls if c.startswith("docker compose run")])
        self.assertEqual(sum(c.startswith("curl ") for c in calls), 10)

    def test_the_scratch_desk_starts_empty_and_the_vault_closed(self):
        tree, _, _ = self.run_script()
        vault = tree / "log" / "checks" / "desk-vault"
        self.assertEqual(list(vault.iterdir()), [])
        self.assertEqual(stat.S_IMODE(vault.stat().st_mode), 0o700)
        self.assertTrue((tree / "log" / "checks" / "desk-book").is_dir())


class ScratchDesk(unittest.TestCase):
    def test_the_checks_get_an_empty_book_and_a_closed_vault(self):
        with tempfile.TemporaryDirectory() as folder:
            (pathlib.Path(folder) / "desk-vault").mkdir()
            (pathlib.Path(folder) / "desk-vault" / "OLD_KEY").write_text("from an earlier run")
            environ = {}
            boxrun.scratch_desk(folder, environ)
            self.assertEqual(environ, {"DESK_BOOK": str(pathlib.Path(folder) / "desk-book"),
                                       "DESK_VAULT": str(pathlib.Path(folder) / "desk-vault")})
            self.assertEqual(list(pathlib.Path(environ["DESK_VAULT"]).iterdir()), [])
            self.assertEqual(stat.S_IMODE(os.stat(environ["DESK_VAULT"]).st_mode), 0o700)
            self.assertTrue(pathlib.Path(environ["DESK_BOOK"]).is_dir())

    def test_paths_set_by_the_caller_are_kept_and_left_alone(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as mine:
            (pathlib.Path(mine) / "KEPT").write_text("x")
            environ = {"DESK_BOOK": mine, "DESK_VAULT": mine}
            boxrun.scratch_desk(folder, environ)
            self.assertEqual(environ, {"DESK_BOOK": mine, "DESK_VAULT": mine})
            self.assertTrue((pathlib.Path(mine) / "KEPT").exists())
            self.assertEqual(list(pathlib.Path(folder).iterdir()), [])

    def test_absolute_paths_so_compose_reads_them_as_folders(self):
        # A bare relative path like log/checks/desk-book would be taken for a named volume.
        with tempfile.TemporaryDirectory() as folder:
            environ = {}
            boxrun.scratch_desk(folder, environ)
            self.assertTrue(all(pathlib.Path(p).is_absolute() for p in environ.values()))


def printf_text(script, path):
    """The text a `printf -- '...' > path` line in the check's script writes."""
    match = re.search(r"printf -- '([^']*)' > " + re.escape(path), script)
    return match.group(1).replace("\\n", "\n")


class Roles(unittest.TestCase):
    VALUE = "check-0123456789abcdef"

    def passing_output(self):
        lines = [f"{verdict} {role} | ..." for role, verdict in roles.EXPECT.items()]
        lines += [text for text, _ in roles.MUST_CONTAIN]
        lines.append(f"VAULT-READ as agent: {self.VALUE}")
        return "\n".join(lines)

    def test_a_full_run_passes(self):
        self.assertEqual(roles.failures(self.passing_output(), self.VALUE), [])

    def test_the_worker_ask_is_a_request_the_head_could_file(self):
        # Refused only by the actor gate: well formed, every need a vault name, and not a repeat.
        text = printf_text(roles.SCRIPT, "company/asks/other.md")
        title, needs, _ = parse_ask(text)
        self.assertTrue(title and needs and all(ITEM_NAME.fullmatch(n) for n in needs))
        self.assertNotEqual(text, printf_text(roles.SCRIPT, "company/asks/store.md"))
        worker = roles.SCRIPT.index("COMPANY_ACTOR=worker ask company/asks/other.md")
        self.assertNotIn("company/asks/other.md", roles.SCRIPT[:worker].replace("> company/asks/other.md", ""))

    def test_the_worker_task_is_fresh(self):
        # Never assigned before the worker tries it, so a duplicate refusal cannot pass for the gate.
        worker = roles.SCRIPT.index("COMPANY_ACTOR=worker assign quick company/tasks/w3.md")
        before = roles.SCRIPT[:worker]
        self.assertIn("> company/tasks/w3.md", before)
        self.assertNotIn("assign quick company/tasks/w3.md", before)

    def test_the_head_then_runs_the_very_same_input(self):
        for worker, control in (("COMPANY_ACTOR=worker ask company/asks/other.md", "\nask company/asks/other.md"),
                                ("COMPANY_ACTOR=worker assign quick company/tasks/w3.md",
                                 "\nassign quick company/tasks/w3.md")):
            self.assertLess(roles.SCRIPT.index(worker), roles.SCRIPT.index(control))

    def test_a_worker_refused_for_another_reason_fails(self):
        for control in ("WORKER-ASK-CONTROL", "WORKER-ASSIGN-CONTROL"):
            out = self.passing_output().replace(f"{control} accepted", f"{control} refused")
            found = roles.failures(out, self.VALUE)
            self.assertEqual(len(found), 1, control)
            self.assertIn("proves nothing about the actor gate", found[0])

    def test_a_worker_accepted_fails(self):
        out = self.passing_output().replace("WORKER-ASK refused", "WORKER-ASK accepted")
        self.assertIn("ask filed a request for a worker", " ".join(roles.failures(out, self.VALUE)))

    def test_an_unreadable_vault_fails(self):
        out = self.passing_output().replace(f"VAULT-READ as agent: {self.VALUE}",
                                            "VAULT-READ as agent: cat: /opt/company/vault/CHECK_ITEM: Permission denied")
        self.assertIn("an agent cannot read a value the front desk put in the vault",
                      " ".join(roles.failures(out, self.VALUE)))

    def test_a_value_from_an_earlier_run_does_not_count(self):
        self.assertTrue(roles.failures(self.passing_output(), "check-ffffffffffffffff"))

    def test_a_writable_or_missing_mount_fails_even_when_the_write_probe_is_refused(self):
        for mounts in ("MOUNTS /opt/company/desk=rw /opt/company/vault=ro", "MOUNTS /opt/company/vault=ro"):
            out = self.passing_output().replace("MOUNTS /opt/company/desk=ro /opt/company/vault=ro", mounts)
            self.assertIn("not mounted read-only", " ".join(roles.failures(out, self.VALUE)), mounts)

    def test_the_value_is_planted_by_the_desks_own_code(self):
        # The snippets run here as they run in the desk's container, on today's desk/server.py: a name
        # the desk no longer has would fail check 3b only once Docker runs it.
        with tempfile.TemporaryDirectory() as folder:
            book, vault = pathlib.Path(folder) / "book", pathlib.Path(folder) / "vault"
            book.mkdir()
            server = importlib.util.module_from_spec(importlib.util.spec_from_file_location("server", ROOT / "desk" / "server.py"))
            server.__spec__.loader.exec_module(server)
            server.DECISIONS, server.VAULT = book / "decisions.jsonl", vault
            out = io.StringIO()
            with mock.patch.dict(sys.modules, {"server": server}), mock.patch.dict(os.environ, {"CHECK_VALUE": self.VALUE}), \
                    contextlib.redirect_stdout(out):
                exec(roles.PLANT, {})
                self.assertEqual((vault / "CHECK_ITEM").read_text(), self.VALUE)
                exec(roles.WITHDRAW, {})
            self.assertTrue(out.getvalue().startswith("PLANTED by the desk as uid "))
            self.assertFalse((vault / "CHECK_ITEM").exists())
            lines = [json.loads(line) for line in server.DECISIONS.read_text().splitlines()]
            self.assertEqual([(d["decision"], d["names"]) for d in lines], [("provided", ["CHECK_ITEM"]), ("withdrawn", ["CHECK_ITEM"])])
            self.assertNotIn(self.VALUE, server.DECISIONS.read_text())
            self.assertFalse(ASK_ID.fullmatch(lines[0]["id"]))
        out = self.passing_output().replace("PLANTED by the desk", "Traceback")
        self.assertIn("could not put a value", " ".join(roles.failures(out, self.VALUE)))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DeskAndOffice(unittest.TestCase):
    """The office shows a request as waiting exactly when the desk would take an answer to it. Each
    reads asks.jsonl and the outside log on its own, so a rule kept in one and not the other gives the
    owner a card the desk refuses, or hides one it would take."""

    ASK = "---\ntitle: A store\nneeds: STORE_URL\n---\nA store in your name.\n"

    def setUp(self):
        import company
        self.company = company
        self.desk, self.office = load("wiring_desk", "desk/server.py"), load("wiring_office", "office/server.py")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        self.work, self.log, book = root / "w", root / "log", root / "book"
        for folder in (self.work / ".box", self.work / "company" / "asks", self.log / "cycles", book):
            folder.mkdir(parents=True)
        for name in ("store", "fake"):
            (self.work / "company" / "asks" / f"{name}.md").write_text(self.ASK.replace("A store", f"A {name}"))
        self.office.WORKSPACE, self.office.DESK, self.office.LOG = self.work, book, self.log

    def head_asked(self, ident, typed):
        rows = [{"actor": "orchestrator", "event": {"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "Bash", "id": f"t{ident}", "input": {"command": f"ask {typed}"}}]}}},
                {"actor": "orchestrator", "event": {"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": f"t{ident}", "content": f"asked {ident}: A store."}]}}}]
        with (self.log / "cycles" / "0001.jsonl").open("a") as f:
            f.write("".join(json.dumps(r) + "\n" for r in rows))

    def line(self, ident, name="store", **more):
        text = (self.work / "company" / "asks" / f"{name}.md").read_text()
        return {"id": ident, "file": f"company/asks/{name}.md", "sha256": self.company.fingerprint(text), **more}

    def read(self, name):
        path = (self.work / str(name)).resolve()
        return path.read_text() if path.is_file() else None

    def agree(self, *rows):
        (self.work / ".box" / "asks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        cards = {a["id"]: a for a in self.office.front_desk({})["asks"] if a["status"] == "waiting"}
        asked, filed = self.company.read_jsonl(self.work / ".box" / "asks.jsonl"), self.company.filed_asks(self.log, {})
        taken = set()
        for row in rows:
            sha = cards[row["id"]]["sha256"] if row["id"] in cards else row.get("sha256")
            request = {"id": row["id"], "decision": "no", "sha256": sha, "note": "not now"}
            if self.desk.answer(asked, filed, request, self.read, 0)[3] is None:
                taken.add(row["id"])
        self.assertEqual(set(cards), taken)
        return taken

    def test_the_heads_ask(self):
        self.head_asked("a1", "company/asks/store.md")
        self.assertEqual(self.agree(self.line("a1")), {"a1"})

    def test_however_the_head_spelled_the_path(self):
        self.head_asked("a1", "/workspace/company/./asks//store.md")
        self.assertEqual(self.agree(self.line("a1")), {"a1"})

    def test_a_line_slipped_in_ahead_of_the_heads(self):
        self.head_asked("a1", "company/asks/store.md")
        self.assertEqual(self.agree(self.line("a1", "fake"), self.line("a1")), set())

    def test_a_line_after_the_heads(self):
        self.head_asked("a1", "company/asks/store.md")
        self.assertEqual(self.agree(self.line("a1"), self.line("a1", "fake")), {"a1"})

    def test_a_line_the_head_never_filed(self):
        self.head_asked("a1", "company/asks/store.md")
        self.assertEqual(self.agree(self.line("a1"), self.line("a2", "fake")), {"a1"})

    def test_replaced_only_by_a_filed_ask(self):
        self.head_asked("a1", "company/asks/store.md")
        self.assertEqual(self.agree(self.line("a1"), self.line("a2", "fake", replaces=["a1"])), {"a1"})
        self.head_asked("a2", "company/asks/fake.md")
        self.assertEqual(self.agree(self.line("a1"), self.line("a2", "fake", replaces=["a1"])), {"a2"})

    def test_an_edited_request(self):
        self.head_asked("a1", "company/asks/store.md")
        row = self.line("a1")
        (self.work / "company" / "asks" / "store.md").write_text(self.ASK + "One more thing.\n")
        self.assertEqual(self.agree(row), set())


class Documents(unittest.TestCase):
    def test_every_make_target_the_office_page_names_exists(self):
        page = (ROOT / "office" / "office.html").read_text()
        targets = set(re.findall(r"^([a-z-]+):", (ROOT / "Makefile").read_text(), re.M))
        named = set(re.findall(r"\bmake ([a-z][a-z-]*)", page))
        self.assertTrue(named)
        self.assertEqual(named - targets, set())

    def test_the_design_doc_names_only_files_that_exist(self):
        text = (ROOT / "docs" / "DESIGN.md").read_text()
        named = set(re.findall(r"`((?:box|checks|desk|door|office|window)/[\w./-]+\.(?:py|txt|md|html|yaml))`", text))
        self.assertIn("door/shut.txt", named)
        self.assertEqual([n for n in sorted(named) if not (ROOT / n).is_file()], [])

    def test_the_design_doc_says_the_company_asks_before_acting(self):
        text = " ".join((ROOT / "docs" / "DESIGN.md").read_text().split())
        self.assertIn("ask at the front desk before it publishes, sells, posts or signs up", text)
        self.assertNotIn("Nothing here is built", text)

    def test_preflight_rows_are_numbered_in_order_with_five_cells(self):
        rows = [line for line in (ROOT / "PREFLIGHT.md").read_text().splitlines() if re.match(r"^\| \d+ \|", line)]
        self.assertEqual([int(row.split("|")[1]) for row in rows], list(range(1, len(rows) + 1)))
        self.assertEqual({row.count("|") for row in rows}, {6})


if __name__ == "__main__":
    unittest.main()
