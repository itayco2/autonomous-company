"""The door: the box's only way out.

Two modes, set by DOOR_MODE:
- allowlist: CONNECT only, to a host on the allowlist, on port 443. Everything else gets a 403.
- open: any public address on the internet, by CONNECT or plain
  HTTP. What stays closed is everything that is not the public internet: the owner's home network,
  the Mac, Docker's own addresses, cloud metadata, loopback. The check is made on the address a
  name resolves to, and the door connects to that very address, so a name pointed at a private
  address cannot sneak through.
In open mode two more rules. The door takes names, never a bare address: an address skips every rule
made on names, and npm, pip, curl and browsers behind a proxy always send names. And the company
never invents an identity, since its accounts and keys come from the person watching, so the sites
for doing that (temporary inboxes, rented phone numbers, captcha solvers; door/shut.txt) stay shut,
with all their subdomains. That list blocks by name and cannot name every inbox on the internet, so
it is a speed bump behind the written rules (walls.md), not a wall.
The list fails closed: if it goes missing or cannot be read, the last list read stays in force, and
in open mode the door will not start without one. Restart the door after editing the list
(docker compose restart door): compose mounts the single file, which pins the file the door started
with, so an edit saved by writing a new file and renaming it over the old one (sed -i, and many
editors) never reaches a running door.
Every refusal is a 403 that says why in an X-Door-Reason header.
Every decision is one JSON line, on stdout and in DOOR_LOG, which lives outside the box. The door
never looks inside a TLS tunnel, so it records who was reached and how many bytes moved, not what
was said.
"""
import asyncio
import ipaddress
import json
import os
import socket
import time
from urllib.parse import urlsplit

ALLOWLIST_PATH = os.environ.get("DOOR_ALLOWLIST", "/door/allowlist.txt")
SHUT_PATH = os.environ.get("DOOR_SHUT", "/door/shut.txt")
LOG_PATH = os.environ.get("DOOR_LOG", "/log/door.jsonl")
PORT = int(os.environ.get("DOOR_PORT", "3128"))
MODE = os.environ.get("DOOR_MODE", "allowlist")


def load_allowlist(path):
    with open(path) as f:
        names = (line.split("#")[0].strip().lower() for line in f)
        return frozenset(name for name in names if name)


def listed(host, names):
    """True if the host is one of the names or a subdomain of one: app.gumroad.com is gumroad.com."""
    return any(host == name or host.endswith("." + name) for name in names)


class Watched:
    """A file read again whenever it changes, so an edit to the list counts from the next request.
    If the file goes missing or cannot be read (a directory, say, which compose makes when a mount's
    source is missing), the last value read stays: a list of shut sites must fail closed."""

    def __init__(self, path, parse, empty):
        self.path, self.parse, self.stamp, self.value = path, parse, None, empty

    def get(self):
        try:
            info = os.stat(self.path)
            stamp = (info.st_mtime_ns, info.st_size)
        except OSError:
            return self.value
        if stamp != self.stamp:
            try:
                with open(self.path, errors="replace") as f:
                    self.value = self.parse(f.read())
            except OSError:
                return self.value
            self.stamp = stamp
        return self.value


def parse_names(text):
    return frozenset(n for n in (line.split("#")[0].strip().lower() for line in text.splitlines()) if n)


def address_literal(host):
    """True if the host is an address rather than a name, in any spelling the resolver accepts:
    1.1.1.1, but also 16843009 and 0x1.0x1.0x1.0x1, which getaddrinfo reads as that same address."""
    try:
        socket.inet_aton(host)
        return True
    except (OSError, ValueError):
        pass
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def decide(request_line, allow, mode="allowlist"):
    """(allowed, host, port, reason) for the first line of a proxy request. In open mode this is
    only the first half: the address the host resolves to is checked by `public` before connecting.
    Open mode refuses a bare address, public or not: the shut list is made of names, and a client
    that connects by number can still name a shut site inside its own TLS handshake."""
    parts = request_line.split()
    if len(parts) != 3:
        return False, "", 0, "malformed"
    method, target, _ = parts
    if method != "CONNECT":
        try:
            url = urlsplit(target)
            host, port = (url.hostname or "").lower().rstrip("."), url.port or 80
        except ValueError:
            return False, "", 0, "malformed"
        if mode == "open" and url.scheme == "http" and host:
            if address_literal(host):
                return False, host, port, "address-literal"
            return True, host, port, "open"
        return False, host, port, "not-connect"
    host, sep, port = target.rpartition(":")
    if not sep or not port.isdigit() or not 0 < int(port) < 65536:
        return False, target.lower(), 0, "malformed"
    host = host.strip("[]").lower().rstrip(".")
    if mode == "open":
        if not host:
            return False, host, int(port), "malformed"
        if address_literal(host):
            return False, host, int(port), "address-literal"
        return True, host, int(port), "open"
    if int(port) != 443:
        return False, host, int(port), "port"
    if host not in allow:
        return False, host, 443, "host"
    return True, host, 443, "allowlisted"


def public(address):
    """True only for an address on the public internet: not private, loopback, link-local (cloud
    metadata), shared, reserved or multicast. The owner's network and the Mac are all private."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.is_global and not ip.is_multicast


def origin_form(request_head):
    """A plain-HTTP proxy request rewritten for the server itself: `GET http://h/p HTTP/1.1` becomes
    `GET /p HTTP/1.1`, and the connection closes after one answer, so a second request on it can
    never reach this server by mistake."""
    first, _, rest = request_head.partition(b"\r\n")
    method, target, version = first.decode("latin-1").split()
    url = urlsplit(target)
    path = (url.path or "/") + (f"?{url.query}" if url.query else "")
    headers = [h for h in rest.split(b"\r\n") if h and not h.lower().startswith((b"connection:", b"proxy-connection:"))]
    return b"\r\n".join([f"{method} {path} {version}".encode("latin-1"), *headers, b"Connection: close", b"", b""])


class Door:
    def __init__(self, allow, log_path, mode="allowlist", shut_path=SHUT_PATH):
        self.allow, self.mode = allow, mode
        self.log_file = open(log_path, "a", buffering=1)
        self.shut = Watched(shut_path, parse_names, frozenset())

    def start_problem(self):
        """Why this door must not start, or None. In open mode the shut list is what keeps the
        identity sites closed, so a list that is missing, unreadable or empty keeps the door down:
        a box that cannot get out beats one that reaches every inbox with only shut=0 to show for it."""
        if self.mode == "open" and not self.shut.get():
            return "no-shut-list"
        return None

    def log(self, **fields):
        line = json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **fields})
        print(line, flush=True)
        self.log_file.write(line + "\n")

    async def handle(self, reader, writer):
        client = writer.get_extra_info("peername")[0]
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ConnectionError):
            writer.close()
            return
        request_line = head.split(b"\r\n", 1)[0].decode("latin-1")
        allowed, host, port, reason = decide(request_line, self.allow, self.mode)
        target = host
        if allowed and self.mode == "open" and listed(host, self.shut.get()):
            allowed, reason = False, "no-new-identities"
        if allowed and self.mode == "open":
            # Resolve here, refuse if any answer is not public, and connect to the address checked.
            try:
                infos = await asyncio.get_running_loop().getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_STREAM)
                addresses = [info[4][0] for info in infos]
            except OSError:
                addresses = []
            if not addresses:
                allowed, reason = False, "unresolvable"
            elif not all(public(a) for a in addresses):
                allowed, reason = False, "private-address"
            else:
                target = addresses[0]
        if not allowed:
            self.log(event="refuse", client=client, host=host, port=port, reason=reason)
            # The reason travels with the refusal, so a check can tell a site shut on purpose from
            # one refused for something else, or from a door that merely failed to get through.
            await self.reply(writer, f"HTTP/1.1 403 Forbidden\r\nX-Door-Reason: {reason}\r\nConnection: close\r\n\r\n".encode())
            return
        try:
            # IPv4 only: the door's own network has no IPv6 route, so a v6 attempt can only fail.
            opening = asyncio.open_connection(target, port, family=socket.AF_INET)
            up_reader, up_writer = await asyncio.wait_for(opening, timeout=15)
        except (OSError, asyncio.TimeoutError) as error:
            self.log(event="unreachable", client=client, host=host, port=port,
                     reason=type(error).__name__, detail=str(error)[:200])
            await self.reply(writer, b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            return
        self.log(event="allow", client=client, host=host, port=port)
        if request_line.startswith("CONNECT "):
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        else:
            up_writer.write(origin_form(head))
        started = time.monotonic()
        moved = {"sent": 0, "received": 0}
        pumps = [
            asyncio.create_task(self.pump(reader, up_writer, moved, "sent")),
            asyncio.create_task(self.pump(up_reader, writer, moved, "received")),
        ]
        _, pending = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for side in (writer, up_writer):
            side.close()
        self.log(event="close", client=client, host=host, seconds=round(time.monotonic() - started, 1), **moved)

    @staticmethod
    async def pump(reader, writer, moved, key):
        try:
            while data := await reader.read(65536):
                moved[key] += len(data)
                writer.write(data)
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass

    @staticmethod
    async def reply(writer, response):
        try:
            writer.write(response)
            await writer.drain()
        except ConnectionError:
            pass
        writer.close()


async def main():
    door = Door(load_allowlist(ALLOWLIST_PATH), LOG_PATH, MODE)
    problem = door.start_problem()
    if problem:
        door.log(event="stop", mode=MODE, reason=problem, shut=door.shut.path)
        raise SystemExit(1)
    server = await asyncio.start_server(door.handle, "0.0.0.0", PORT)
    door.log(event="open", port=PORT, mode=MODE, allowlist=sorted(door.allow) if MODE != "open" else None,
             shut=len(door.shut.get()) if MODE == "open" else None)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
