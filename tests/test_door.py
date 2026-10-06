"""The door: its shut list, its decisions and its refusals, and how the walls check reads them.
Pure functions and in-memory streams only, standard library only: nothing here reaches a network.

Run:  python3 -m unittest discover -s tests
"""
import asyncio
import contextlib
import importlib.util
import io
import json
import os
import pathlib
import socket
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "box" / "bin"), str(ROOT / "door")]

import company  # noqa: E402
from company import fingerprint, parse_ask, space_used  # noqa: E402
from proxy import Door, address_literal, decide, listed  # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


desk = load("desk_server", "desk/server.py")
office = load("office_server", "office/server.py")
hook = load("only_assign", "box/bin/only-assign.py")
cycle = load("cycle", "box/bin/cycle.py")
walls = load("walls", "checks/walls.py")

ASK = "---\ntitle: A Gumroad store\nneeds: STORE_URL, GUMROAD_ACCESS_TOKEN\n---\nA store in your name, and a key that can create products.\n"


class Client:
    """The client's side of a connection to the door, in memory: it keeps everything the door answers."""

    def __init__(self):
        self.answer, self.closed = b"", False

    def get_extra_info(self, name):
        return ("172.30.0.5", 40000)

    def write(self, data):
        self.answer += data

    async def drain(self):
        pass

    def close(self):
        self.closed = True


def through_door(door, request_line, addresses=None):
    """One request through Door.handle on asyncio streams. The resolver answers only from `addresses`,
    and any attempt to connect anywhere fails the test, so a refusal can never be a network accident."""
    async def run():
        async def getaddrinfo(host, port, **kwargs):
            if host not in (addresses or {}):
                raise socket.gaierror(f"{host} is not in this test's resolver")
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addresses[host], port))]
        asyncio.get_running_loop().getaddrinfo = getaddrinfo
        reader = asyncio.StreamReader()
        reader.feed_data(f"{request_line}\r\nHost: x\r\n\r\n".encode())
        reader.feed_eof()
        client = Client()
        await door.handle(reader, client)
        return client
    with mock.patch("asyncio.open_connection", side_effect=AssertionError("the door tried to connect")), \
            contextlib.redirect_stdout(io.StringIO()):
        return asyncio.run(run())


def answered(client):
    """The status line and the headers of what the door answered."""
    status, *lines = client.answer.split(b"\r\n\r\n", 1)[0].decode().split("\r\n")
    return status, dict(line.split(": ", 1) for line in lines)


class TheDoor(unittest.TestCase):
    def open_door(self, tmp, shut=None):
        door = Door(frozenset(), str(pathlib.Path(tmp) / "log"), "open", str(shut or ROOT / "door" / "shut.txt"))
        self.addCleanup(door.log_file.close)
        return door

    def logged(self, tmp):
        return [json.loads(line) for line in (pathlib.Path(tmp) / "log").read_text().splitlines()]

    def test_a_listed_site_covers_its_subdomains_only(self):
        self.assertTrue(listed("api.mail.tm", {"mail.tm"}))
        self.assertTrue(listed("mail.tm", {"mail.tm"}))
        self.assertFalse(listed("gmail.tm", {"mail.tm"}))
        self.assertFalse(listed("mail.tm.evil.net", {"mail.tm"}))

    def test_identity_sites_stay_shut_and_stores_are_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            door = Door(frozenset(), str(pathlib.Path(tmp) / "log"), "open", str(ROOT / "door" / "shut.txt"))
            self.addCleanup(door.log_file.close)
            for host in ("api.mail.tm", "www.guerrillamail.com", "api.2captcha.com", "sms-activate.org"):
                self.assertTrue(listed(host, door.shut.get()), host)
            for host in ("api.gumroad.com", "api.stripe.com", "www.reddit.com", "github.com", "api.anthropic.com"):
                self.assertFalse(listed(host, door.shut.get()), host)

    def test_the_door_rereads_its_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            shut = pathlib.Path(tmp) / "shut.txt"
            shut.write_text("# inboxes\nmail.tm\n")
            door = Door(frozenset(), str(pathlib.Path(tmp) / "log"), "open", str(shut))
            self.addCleanup(door.log_file.close)
            self.assertEqual(door.shut.get(), {"mail.tm"})
            shut.write_text("mail.tm\nyopmail.com\n")
            os.utime(shut, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
            self.assertEqual(door.shut.get(), {"mail.tm", "yopmail.com"})


    def test_a_list_that_cannot_be_read_keeps_the_last_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            shut = pathlib.Path(tmp) / "shut.txt"
            shut.write_text("mail.tm\n")
            door = self.open_door(tmp, shut)
            self.assertEqual(door.shut.get(), {"mail.tm"})
            shut.unlink()
            self.assertEqual(door.shut.get(), {"mail.tm"}, "a missing file must not open the list")
            # What compose leaves when a mount's source is missing: a directory where the file was.
            shut.mkdir()
            self.assertEqual(door.shut.get(), {"mail.tm"}, "a directory must not open the list")
            shut.rmdir()
            shut.write_text("mail.tm\nyopmail.com\n")
            self.assertEqual(door.shut.get(), {"mail.tm", "yopmail.com"}, "a list that comes back counts again")

    def test_open_mode_will_not_start_without_a_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = pathlib.Path(tmp) / "missing.txt"
            folder = pathlib.Path(tmp) / "folder"
            folder.mkdir()
            empty = pathlib.Path(tmp) / "empty.txt"
            empty.write_text("# nothing listed yet\n")
            for shut in (missing, folder, empty):
                self.assertEqual(self.open_door(tmp, shut).start_problem(), "no-shut-list", shut.name)
            self.assertIsNone(self.open_door(tmp).start_problem())
            on_the_allowlist = Door(frozenset(), str(pathlib.Path(tmp) / "log"), "allowlist", str(missing))
            self.addCleanup(on_the_allowlist.log_file.close)
            self.assertIsNone(on_the_allowlist.start_problem(), "the allowlist does not use the shut list")

    def test_open_mode_takes_names_never_addresses(self):
        for spelling in ("1.1.1.1", "16843009", "0x01010101", "1.1", "2606:4700:4700::1111"):
            self.assertTrue(address_literal(spelling), spelling)
        for name in ("example.com", "3com.com", "1e100.net", "1.1.1.1.nip.io", "localhost"):
            self.assertFalse(address_literal(name), name)
        for line in ("CONNECT 1.1.1.1:443 HTTP/1.1", "CONNECT 16843009:443 HTTP/1.1",
                     "CONNECT [2606:4700:4700::1111]:443 HTTP/1.1", "GET http://1.1.1.1/ HTTP/1.1",
                     "GET http://0x01010101:8080/x HTTP/1.1"):
            self.assertEqual(decide(line, frozenset(), "open")[3], "address-literal", line)
        self.assertEqual(decide("CONNECT github.com:443 HTTP/1.1", frozenset(), "open"), (True, "github.com", 443, "open"))
        self.assertEqual(decide("GET http://example.com/ HTTP/1.1", frozenset(), "open"), (True, "example.com", 80, "open"))
        # The allowlist refuses an address the way it refuses any name not on it.
        self.assertEqual(decide("CONNECT 1.1.1.1:443 HTTP/1.1", {"api.anthropic.com"}), (False, "1.1.1.1", 443, "host"))

    def test_a_shut_site_is_refused_and_says_why(self):
        with tempfile.TemporaryDirectory() as tmp:
            door = self.open_door(tmp)
            for line, host in (("GET http://www.yopmail.com/ HTTP/1.1", "www.yopmail.com"),
                               ("CONNECT api.mail.tm:443 HTTP/1.1", "api.mail.tm")):
                client = through_door(door, line)
                status, headers = answered(client)
                self.assertEqual(status, "HTTP/1.1 403 Forbidden", line)
                self.assertEqual(headers.get("X-Door-Reason"), "no-new-identities", line)
                self.assertTrue(client.closed, line)
                last = self.logged(tmp)[-1]
                self.assertEqual((last["event"], last["host"], last["reason"]), ("refuse", host, "no-new-identities"))

    def test_every_refusal_says_why(self):
        with tempfile.TemporaryDirectory() as tmp:
            door = self.open_door(tmp)
            for line, reason in (("CONNECT 1.1.1.1:443 HTTP/1.1", "address-literal"),
                                 ("GET http://192.168.1.1/ HTTP/1.1", "address-literal"),
                                 ("CONNECT router.example:443 HTTP/1.1", "private-address"),
                                 ("CONNECT gone.example:443 HTTP/1.1", "unresolvable"),
                                 ("GET /relative HTTP/1.1", "not-connect"),
                                 ("CONNECT nonsense HTTP/1.1", "malformed")):
                status, headers = answered(through_door(door, line, {"router.example": "192.168.1.1"}))
                self.assertEqual((status, headers.get("X-Door-Reason")), ("HTTP/1.1 403 Forbidden", reason), line)
                self.assertEqual(self.logged(tmp)[-1]["reason"], reason, line)

    def test_the_walls_check_counts_a_shut_site_closed_only_for_being_shut(self):
        with tempfile.TemporaryDirectory() as tmp:
            refusal = through_door(self.open_door(tmp), "CONNECT api.mail.tm:443 HTTP/1.1").answer

        def through_walls(answer):
            near, far = socket.socketpair()
            self.addCleanup(far.close)
            far.sendall(answer)
            with mock.patch.object(walls.socket, "create_connection", return_value=near):
                return walls.via_door("CONNECT api.mail.tm:443 HTTP/1.1")[1]

        self.assertEqual(through_walls(refusal), "no-new-identities")
        for other in (b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n",
                      b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n",
                      b"HTTP/1.1 301 Moved Permanently\r\nLocation: https://www.yopmail.com/\r\n\r\n"):
            got = through_walls(other)
            self.assertFalse(walls.as_required(got, "no-new-identities"), other)
            self.assertTrue(walls.as_required(got, False), other)
        # A door reason is met by that reason only; a plain "closed" by any refusal, but never a success.
        self.assertTrue(walls.as_required("no-new-identities", "no-new-identities"))
        self.assertFalse(walls.as_required("unresolvable", "no-new-identities"))
        self.assertFalse(walls.as_required(True, "address-literal"))
        self.assertTrue(walls.as_required("address-literal", False))
        self.assertFalse(walls.as_required(True, False))
        self.assertTrue(walls.as_required(True, True))
        self.assertFalse(walls.as_required("host", True))


if __name__ == "__main__":
    unittest.main()
