"""The window: the workspace's view/ folder, served read-only to the Mac at http://127.0.0.1:8770.

It runs outside the box from a read-only mount, so the person sees exactly what the company put in
view/, and nothing can be written from here. Until view/ exists, a placeholder page checks again
every ten seconds. A page with no tab icon of its own gets a plain one, so the browser never keeps
showing an icon cached from a company that is gone.
"""
import functools
import http.server
import io
import os
import pathlib
import struct
import zlib

VIEW = pathlib.Path(os.environ.get("VIEW", "/w/view"))
PORT = int(os.environ.get("PORT", "8770"))
EMPTY = (b"<!doctype html><meta charset=utf-8><title>Nothing yet</title><meta http-equiv=refresh content=10>"
         b"<link rel=icon href=/favicon.ico>"
         b"<p style='font:16px system-ui;margin:40px'>Nothing in view/ yet. This page checks again every "
         b"10 seconds.</p>")


def plain_icon(size=32):
    """A 32x32 PNG: a grey window with a lit pane, built from the standard library alone."""
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            edge = x in (0, 1, size - 2, size - 1) or y in (0, 1, size - 2, size - 1) or x in (15, 16) or y in (15, 16)
            lit = 3 <= x <= 13 and 3 <= y <= 13
            row += bytes((42, 36, 48, 255) if edge else (245, 166, 35, 255) if lit else (90, 82, 102, 255))
        rows.append(bytes(row))
    chunk = lambda kind, data: (struct.pack(">I", len(data)) + kind + data  # noqa: E731
                                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))


ICON = plain_icon()


class Window(http.server.SimpleHTTPRequestHandler):
    def send_head(self):
        if self.path.split("?")[0] == "/favicon.ico" and not (VIEW / "favicon.ico").is_file():
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(ICON)))
            self.end_headers()
            return io.BytesIO(ICON)
        if not VIEW.is_dir():
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(EMPTY)))
            self.end_headers()
            return io.BytesIO(EMPTY)
        return super().send_head()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")  # always the latest the company wrote
        super().end_headers()

    def log_message(self, *args):
        pass


handler = functools.partial(Window, directory=str(VIEW))
http.server.ThreadingHTTPServer(("0.0.0.0", PORT), handler).serve_forever()
