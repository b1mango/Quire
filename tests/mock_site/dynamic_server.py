"""Local M2 pages. Use ``with serve() as server`` and inspect URL/path counts.

GET counts include failures and are keyed by path, without the query string.
``/needs-cookie`` returns {"cookie_received": bool}, never cookie contents.
"""

from __future__ import annotations

import json
import threading
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .server import page_image

READER_JS = """\
const reader = document.querySelector('main.reader');
const mode = reader.dataset.mode;
let pages = 0;
let pending = false;

function appendPage() {
    pages += 1;
    const section = document.createElement('section');
    section.className = 'page';
    const img = document.createElement('img');
    const index = (pages - 1) % 3 + 1;
    img.src = '/images/' + index + '.jpg' +
        (mode === 'endless' ? '?page=' + pages : '');
    img.alt = 'Page ' + pages;
    section.appendChild(img);
    reader.appendChild(section);
    reader.dataset.pages = String(pages);
    const status = document.getElementById('status');
    if (status) status.remove();
}

function onScroll() {
    if (window.scrollY <= 0) return;
    if (mode === 'endless') {
        appendPage();
        return;
    }
    if (pending || pages >= 3) return;
    pending = true;
    setTimeout(() => {
        appendPage();
        pending = false;
        if (pages === 3) window.removeEventListener('scroll', onScroll);
    }, 200);
}

if (mode === 'delayed') {
    setTimeout(appendPage, 10000);
} else {
    appendPage();
    window.addEventListener('scroll', onScroll, {passive: true});
}
"""

COOKIE_JS = """\
fetch('/needs-cookie')
    .then(response => response.json())
    .then(result => {
        const status = document.getElementById('cookie-status');
        status.dataset.cookieReceived = String(result.cookie_received);
        status.textContent = result.cookie_received ? 'cookie: present' : 'cookie: absent';
    });
"""


def reader_page(mode: str, script: str = "/reader.js") -> bytes:
    # Each appended page extends the scroll range, including on tall viewports.
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        "<title>Dynamic Fixture</title><style>"
        "main.reader {min-height: 200vh; overflow-anchor: none;}"
        ".page {min-height: max(120vh, 660px);}"
        "img {display: block; max-width: 100%; height: auto;}"
        "</style></head><body><h1>Dynamic Fixture</h1>"
        f'<main class="reader" data-mode="{mode}">'
        '<p id="status">No images: waiting for reader script.</p></main>'
        f'<script src="{script}" defer></script></body></html>'
    ).encode()


class DynamicSite(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.lock = threading.Lock()
        self.images = {f"/images/{n}.jpg": page_image(n) for n in range(1, 4)}
        super().__init__(("127.0.0.1", 0), Handler)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"


class Handler(BaseHTTPRequestHandler):
    server: DynamicSite

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        with self.server.lock:
            self.server.counts[path] += 1
        if path == "/robots.txt":
            return self.respond(200, b"User-agent: *\nDisallow: /forbidden\n")
        if path == "/redirect":
            return self.respond(302, b"", extra={"Location": "/dynamic"})
        if path in {"/dynamic", "/endless", "/delayed", "/blocked-script"}:
            script = "/forbidden/reader.js" if path == "/blocked-script" else "/reader.js"
            return self.respond(200, reader_page(path[1:], script), "text/html; charset=utf-8")
        if path in {"/reader.js", "/forbidden/reader.js"}:
            # The forbidden script is functional so a robots bypass is observable.
            return self.respond(200, READER_JS.encode(), "text/javascript; charset=utf-8")
        if path == "/cookie":
            html = (
                "<!doctype html><html><head><title>Cookie Fixture</title></head>"
                '<body><h1>Cookie Fixture</h1><main class="reader">'
                '<img src="/images/1.jpg" alt="Page 1">'
                '<p id="cookie-status">cookie: pending</p></main>'
                f"<script>{COOKIE_JS}</script></body></html>"
            )
            return self.respond(
                200,
                html.encode(),
                "text/html; charset=utf-8",
                {"Set-Cookie": "fixture_cookie=1; Path=/; HttpOnly; SameSite=Lax"},
            )
        if path == "/needs-cookie":
            result = {"cookie_received": bool(self.headers.get("Cookie"))}
            return self.respond(200, json.dumps(result).encode(), "application/json")
        if path in self.server.images:
            referer = urlsplit(self.headers.get("Referer", ""))
            origin = urlsplit(self.server.url)
            if (referer.scheme, referer.netloc) != (origin.scheme, origin.netloc):
                return self.respond(403, b"referer required")
            return self.respond(200, self.server.images[path], "image/jpeg")
        self.respond(404, b"not found")

    def respond(
        self,
        status: int,
        data: bytes,
        content_type: str = "text/plain; charset=utf-8",
        extra: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            return


@contextmanager
def serve() -> Iterator[DynamicSite]:
    """Start an isolated loopback server on an OS-assigned port; always close it."""
    server = DynamicSite()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
