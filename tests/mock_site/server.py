"""Loopback fixture: ordered pages, hotlink checks and injected failures."""

from __future__ import annotations

import gzip
import io
import threading
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from PIL import Image, ImageDraw


def page_image(index: int) -> bytes:
    image = Image.new("RGB", (400 + index, 600), (50 + index * 30, 90, 150))
    draw = ImageDraw.Draw(image)
    draw.rectangle((25, 25, 375, 270), fill=(230, 235, 240), outline=(10, 10, 10), width=3)
    draw.rectangle((25, 290, 375, 560), fill=(210, 215, 220), outline=(10, 10, 10), width=3)
    draw.text((40, 40), f"QUIRE TEST PAGE {index}", fill=(10, 10, 10), font_size=24)
    output = io.BytesIO()
    image.save(output, "JPEG")
    return output.getvalue()


class MockSite(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.counts: Counter[str] = Counter()
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.redirect_robots = False
        self.images = {f"/images/{n}.jpg": page_image(n) for n in range(1, 4)}

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"


class Handler(BaseHTTPRequestHandler):
    server: MockSite

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        with self.server.lock:
            self.server.counts[path] += 1
            count = self.server.counts[path]
        if path == "/robots.txt":
            if self.server.redirect_robots:
                return self.respond(302, b"", extra={"Location": "/policy"})
            return self.respond(200, b"User-agent: *\nDisallow: /forbidden\n")
        if path == "/policy":
            return self.respond(200, b"User-agent: *\nDisallow: /forbidden\n")
        if path == "/benchmark":
            pages = "".join(f'<img src="/images/{i % 3 + 1}.jpg?page={i}">' for i in range(200))
            return self.respond(200, f"<main>{pages}</main>".encode(), "text/html")
        if path in {"/comic", "/partial", "/fallback"}:
            middle = "/missing.jpg" if path == "/partial" else "/images/2.jpg"
            first = 'data-src="/missing.jpg"' if path == "/fallback" else ""
            html = (
                f'<html><title>Quire Fixture</title><h1>Quire Fixture</h1><main class="reader">'
                f'<img {first} src="/images/1.jpg"><img src="{middle}">'
                '<img data-src="/images/3.jpg" src="/placeholder.gif"></main>'
                '<aside style="background:url(/ads.jpg)"></aside></html>'
            )
            return self.respond(200, html.encode(), "text/html; charset=utf-8")
        if path == "/retry" and count == 1:
            return self.respond(429, b"retry", extra={"Retry-After": "0.02"})
        if path == "/retry":
            return self.respond(200, b"ok")
        if path == "/blocked":
            return self.respond(403, b"blocked")
        if path == "/redirect":
            return self.respond(302, b"", extra={"Location": "/comic"})
        if path == "/gbk":
            return self.respond(
                200, '<meta charset="gbk"><h1>卷帙测试</h1>'.encode("gbk"), "text/html"
            )
        if path == "/gzip":
            return self.respond(
                200, gzip.compress(b"<h1>gzip</h1>"), extra={"Content-Encoding": "gzip"}
            )
        if path == "/bomb":
            return self.respond(
                200, gzip.compress(b"x" * 100_000), extra={"Content-Encoding": "gzip"}
            )
        if path in self.server.images:
            if not self.headers.get("Referer", "").startswith(self.server.url):
                return self.respond(403, b"referer required")
            with self.server.lock:
                self.server.active += 1
                self.server.peak = max(self.server.peak, self.server.active)
            try:
                time.sleep(0.08 if path.endswith("1.jpg") else 0.01)
                self.respond(200, self.server.images[path], "image/jpeg")
            finally:
                with self.server.lock:
                    self.server.active -= 1
            return
        self.respond(404, b"not found")

    def respond(
        self,
        status: int,
        data: bytes,
        content_type: str = "text/plain",
        extra: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Type", content_type)
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            return


@contextmanager
def serve() -> Iterator[MockSite]:
    server = MockSite()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
