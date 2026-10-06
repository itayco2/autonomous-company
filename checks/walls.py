"""Check 2: from inside the box, the only way out is the door, DNS included, and the door keeps the
owner's network closed.

Run inside the box:  docker compose run --rm -T box python3 - open < checks/walls.py
The argument is the door's mode (open or allowlist): in open mode the public internet must be
reachable through the door, in allowlist mode it must not. In both, direct routes, DNS and every
private address must fail. A probe that names a door reason passes only on a 403 carrying that
reason in X-Door-Reason, since a 502, a redirect or a timeout would look closed without proving the
rule works. Standard library only. Exit code 0 means every probe came out as it must.
"""
import json
import os
import socket
import ssl
import struct
import sys

PROXY = ("door", 3128)
TIMEOUT = 4


def dns_query(name):
    header = struct.pack(">HHHHHH", 0x5151, 0x0100, 1, 0, 0, 0)
    question = b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0"
    return header + question + struct.pack(">HH", 1, 1)


def dns_udp(server, name):
    """Answer count from a raw A query, or the error. Bypasses the system resolver entirely."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(TIMEOUT)
        s.sendto(dns_query(name), (server, 53))
        reply = s.recv(512)
    rcode = reply[3] & 0x0F
    answers = struct.unpack(">H", reply[6:8])[0]
    return f"rcode={rcode} answers={answers}", answers > 0


def resolve(name):
    addresses = sorted({info[4][0] for info in socket.getaddrinfo(name, 443)})
    return ",".join(addresses), True


def tcp(host, port, family=socket.AF_INET):
    with socket.socket(family, socket.SOCK_STREAM) as s:
        s.settimeout(TIMEOUT)
        s.connect((host, port))
    return "connected", True


def via_door(request_line, tls_host=None):
    """What the door answers: True for a 200 (for which a tls_host also needs a verified TLS
    handshake), the X-Door-Reason of a 403, and False for anything else."""
    s = socket.create_connection(PROXY, timeout=TIMEOUT)
    target = request_line.split()[1]
    host = target.split("//", 1)[-1].split("/", 1)[0] if "://" in target else target
    try:
        s.sendall(request_line.encode() + f"\r\nHost: {host}\r\n\r\n".encode())
        head = b""
        while b"\r\n\r\n" not in head and (chunk := s.recv(4096)):
            head += chunk
        status, *lines = head.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")
        reason = next((v.strip() for k, _, v in (line.partition(":") for line in lines)
                       if k.strip().lower() == "x-door-reason"), None)
        if " 403 " in status and reason:
            return f"{status}; X-Door-Reason: {reason}", reason
        if " 200 " not in status or not tls_host:
            return status, " 200 " in status
        with ssl.create_default_context().wrap_socket(s, server_hostname=tls_host) as tls:
            return f"{status}; TLS {tls.version()} to {tls.getpeercert()['subject'][-1][0][1]}", True
    finally:
        s.close()


OPEN = (sys.argv[1:] or ["allowlist"])[0] == "open"
# In open mode a shut site must be refused for being shut. A 502, a redirect, a timeout or a site that
# no longer resolves would also read as closed while saying nothing about the list.
SHUT = "no-new-identities" if OPEN else False
# In open mode a bare address is refused for being one, public or not.
LITERAL = "address-literal" if OPEN else False

# (name, function, args, expected): True must succeed, False must fail in any way, and a door reason
# must be a 403 that gives exactly that reason.
PROBES = [
    ("system resolver: example.com", resolve, ("example.com",), False),
    ("system resolver: api.anthropic.com (allowlisted names too)", resolve, ("api.anthropic.com",), False),
    ("Docker's resolver 127.0.0.11, raw query: example.com", dns_udp, ("127.0.0.11", "example.com"), False),
    ("public resolver 1.1.1.1, raw UDP query", dns_udp, ("1.1.1.1", "example.com"), False),
    ("public resolver 8.8.8.8, raw UDP query", dns_udp, ("8.8.8.8", "example.com"), False),
    ("public resolver 1.1.1.1, TCP 53", tcp, ("1.1.1.1", 53), False),
    ("direct HTTPS by IP, 1.1.1.1:443", tcp, ("1.1.1.1", 443), False),
    ("direct IPv6, [2606:4700:4700::1111]:443", tcp, ("2606:4700:4700::1111", 443, socket.AF_INET6), False),
    ("the Mac, host.docker.internal", resolve, ("host.docker.internal",), False),
    ("the Docker Desktop host, 192.168.65.254:443", tcp, ("192.168.65.254", 443), False),
    # The public internet: open in open mode, closed on the allowlist.
    ("door: CONNECT github.com:443 + TLS", via_door, ("CONNECT github.com:443 HTTP/1.1", "github.com"), OPEN),
    ("door: CONNECT example.com:443", via_door, ("CONNECT example.com:443 HTTP/1.1",), OPEN),
    ("door: plain GET http://example.com/", via_door, ("GET http://example.com/ HTTP/1.1",), OPEN),
    ("door: CONNECT evil.api.anthropic.com:443 (suffix trick)", via_door, ("CONNECT evil.api.anthropic.com:443 HTTP/1.1",), False),
    # Never: the owner's network, the Mac, Docker's own addresses, loopback, cloud metadata,
    # and public names that resolve to any of them.
    ("door: CONNECT host.docker.internal:443 (the Mac)", via_door, ("CONNECT host.docker.internal:443 HTTP/1.1",), False),
    ("door: CONNECT 192.168.1.1:80 (a home router)", via_door, ("CONNECT 192.168.1.1:80 HTTP/1.1",), False),
    ("door: CONNECT 192.168.65.254:443 (Docker's host)", via_door, ("CONNECT 192.168.65.254:443 HTTP/1.1",), False),
    ("door: CONNECT 10.0.0.1:443", via_door, ("CONNECT 10.0.0.1:443 HTTP/1.1",), False),
    ("door: CONNECT 169.254.169.254:80 (cloud metadata)", via_door, ("CONNECT 169.254.169.254:80 HTTP/1.1",), False),
    ("door: CONNECT 127.0.0.1:3128 (the door itself)", via_door, ("CONNECT 127.0.0.1:3128 HTTP/1.1",), False),
    ("door: CONNECT door:3128 (a name for a private address)", via_door, ("CONNECT door:3128 HTTP/1.1",), False),
    ("door: CONNECT localtest.me:80 (public name -> 127.0.0.1)", via_door, ("CONNECT localtest.me:80 HTTP/1.1",), False),
    ("door: plain GET http://192.168.1.1/", via_door, ("GET http://192.168.1.1/ HTTP/1.1",), False),
    # Names only: a client that connects by number can still name a shut site in its TLS handshake.
    ("door: CONNECT 1.1.1.1:443 (a public address, by number)", via_door, ("CONNECT 1.1.1.1:443 HTTP/1.1",), LITERAL),
    ("door: CONNECT 16843009:443 (the same address, as one number)", via_door, ("CONNECT 16843009:443 HTTP/1.1",), LITERAL),
    ("door: plain GET http://1.1.1.1/ (a public address, by number)", via_door, ("GET http://1.1.1.1/ HTTP/1.1",), LITERAL),
    # In open mode the public internet is reachable: stores and payments are open like the rest...
    ("door: CONNECT api.gumroad.com:443 + TLS (a store)", via_door, ("CONNECT api.gumroad.com:443 HTTP/1.1", "api.gumroad.com"), OPEN),
    ("door: CONNECT api.stripe.com:443 + TLS (payments)", via_door, ("CONNECT api.stripe.com:443 HTTP/1.1", "api.stripe.com"), OPEN),
    # ...but it never invents an identity, so the door shuts the sites for that by name (door/shut.txt):
    # inboxes, rented numbers, captcha solvers. A list of names cannot cover every such site, so it
    # is a speed bump behind the written rules; these probes show that the list itself holds.
    ("door: CONNECT api.mail.tm:443 (a temporary inbox)", via_door, ("CONNECT api.mail.tm:443 HTTP/1.1",), SHUT),
    ("door: CONNECT sms-activate.org:443 (a rented phone number)", via_door, ("CONNECT sms-activate.org:443 HTTP/1.1",), SHUT),
    ("door: CONNECT 2captcha.com:443 (a captcha solver)", via_door, ("CONNECT 2captcha.com:443 HTTP/1.1",), SHUT),
    ("door: plain GET http://www.yopmail.com/ (a temporary inbox)", via_door, ("GET http://www.yopmail.com/ HTTP/1.1",), SHUT),
    # The one thing besides the door on the box's own network: the read-only office.
    ("the office, office:8771 (read-only, on the box network)", tcp, ("office", 8771), True),
    # The front desk answers from the Mac only: not on the box network, not even by name.
    ("the front desk, desk:8772 (the owner's answers and the vault)", tcp, ("desk", 8772), False),
    # The model and the registries: open in both modes.
    ("door: CONNECT api.anthropic.com:443 + TLS", via_door, ("CONNECT api.anthropic.com:443 HTTP/1.1", "api.anthropic.com"), True),
    ("door: CONNECT registry.npmjs.org:443 + TLS", via_door, ("CONNECT registry.npmjs.org:443 HTTP/1.1", "registry.npmjs.org"), True),
]


def as_required(got, expected):
    """A door reason is met only by that reason; True only by a success; False by anything else,
    a refusal for any reason included."""
    if isinstance(expected, str):
        return got == expected
    return (got is True) == expected


def main():
    routes = [line.split()[:3] for line in open("/proc/net/route").read().splitlines()[1:]]
    default_route = any(dest == "00000000" for _, dest, _ in routes)
    print(f"user={os.getuid()} default_route={default_route} routes={[r[:2] for r in routes]}")
    failures = 0
    for name, fn, args, expected in PROBES:
        try:
            detail, got = fn(*args)
        except Exception as error:
            detail, got = f"{type(error).__name__}: {error}", False
        ok = as_required(got, expected)
        failures += not ok
        wanted = f"403 {expected}" if isinstance(expected, str) else "open" if expected else "closed"
        print(json.dumps({"ok": ok, "probe": name, "expected": wanted, "got": detail}))
    ok = failures == 0 and not default_route
    print(f"RESULT {'PASS' if ok else 'FAIL'}: {len(PROBES) - failures}/{len(PROBES)} probes as required")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
