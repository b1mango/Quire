"""Loopback scanned-novel fixture: chapter pages whose content is a single image."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO

from PIL import Image

CHAPTER_TITLES = {1: "第一章 残卷", 2: "第二章 拓片", 3: "第三章 影印"}


def chapter_path(number: int) -> str:
    return f"/book/{number}.html"


def image_bytes(number: int) -> bytes:
    """每章一张不同的占位图（fake 引擎按字节区分章节）。"""
    image = Image.new("RGB", (600, 800), (255, 255, 250 - number))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _chapter_page(number: int) -> str:
    return (
        f"<html><head><title>{CHAPTER_TITLES[number]}_影印书库</title></head><body>"
        f"<h1>{CHAPTER_TITLES[number]}</h1>"
        f'<div id="content"><img src="/img/{number}.png"></div>'
        f'<div class="nav"><a href="/book/">目录</a></div>'
        "</body></html>"
    )


def _catalogue() -> str:
    items = "".join(
        f'<li><a href="{chapter_path(number)}">{title}</a></li>'
        for number, title in CHAPTER_TITLES.items()
    )
    return (
        "<html><head><title>影印之书_影印书库</title></head><body>"
        f"<h1>影印之书</h1><ul id='chapter-list'>{items}</ul>"
        "</body></html>"
    )


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib 命名
        path = self.path.split("?", 1)[0]
        if path in ("/", "/book/"):
            self._send(_catalogue().encode(), "text/html")
            return
        for number in CHAPTER_TITLES:
            if path == chapter_path(number):
                self._send(_chapter_page(number).encode(), "text/html")
                return
            if path == f"/img/{number}.png":
                self._send(image_bytes(number), "image/png")
                return
        self.send_error(404)

    def _send(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


class ScanSite(ThreadingHTTPServer):
    daemon_threads = True

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"


@contextmanager
def scan_site():
    server = ScanSite(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
