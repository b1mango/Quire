"""Loopback novel fixture: catalogue, paginated chapters, one missing chapter."""

from __future__ import annotations

import threading
import time
from collections import Counter
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

CHAPTER_TITLES = {
    1: "第一章 起点",
    2: "第二章 分页",
    3: "第三章 岔路",
    4: "第四章 缺失",
    5: "第五章 尾声",
}

#: 每章正文页数；第三章的"下一章"链接不能被当成"下一页"。
CHAPTER_PAGES = {1: 1, 2: 3, 3: 1, 4: 1, 5: 1}

#: 目录里混进的导航链接，不应被当成章节。
NOISE_LINKS = (
    '<a href="/rank">排行榜</a>',
    '<a href="/book/5.html">最新章节</a>',
    '<a href="https://example.com/book/9.html">外站章节</a>',
)


def chapter_path(number: int, page: int = 1) -> str:
    return f"/book/{number}.html" if page == 1 else f"/book/{number}_{page}.html"


def _paragraphs(number: int, page: int) -> str:
    body = "".join(
        f"<p>第{number}章第{page}页第{index}段：他抬头看见远山如黛，风从林间穿过，"
        f"带来潮湿的泥土气息。他知道这一趟必须走下去，因为答案就在前面等着他。</p>"
        for index in range(1, 7)
    )
    noise = (
        "<p>请记住本站域名 www.example.com</p>"
        "<p>http://ad.example.com/track?id=9</p>"
        "<p>求推荐票，求收藏！</p>"
        "<p>本章未完，请点击下一页</p>"
    )
    return body + noise


def _chapter_page(number: int, page: int) -> str:
    total = CHAPTER_PAGES[number]
    navigation = ['<a href="/book/">目录</a>']
    if page < total:
        navigation.append(f'<a href="{chapter_path(number, page + 1)}">下一页</a>')
    if number < max(CHAPTER_TITLES) and page == total:
        navigation.append(f'<a href="{chapter_path(number + 1)}">下一章</a>')
    return (
        f"<html><head><title>{CHAPTER_TITLES[number]}_测试书城</title></head><body>"
        f"<h1>{CHAPTER_TITLES[number]}</h1>"
        f'<div id="content">{_paragraphs(number, page)}</div>'
        f'<div class="nav">{"".join(navigation)}</div>'
        "</body></html>"
    )


def _catalogue() -> str:
    items = "".join(
        f'<li><a href="{chapter_path(number)}">{title}</a></li>'
        for number, title in CHAPTER_TITLES.items()
    )
    return (
        "<html><head><title>测试之书_测试书城</title></head><body>"
        "<h1>测试之书</h1>"
        f'<ul id="chapter-list">{items}{"".join(NOISE_LINKS)}</ul>'
        '<nav><a href="/">首页</a><a href="/rank">排行榜</a></nav>'
        "</body></html>"
    )


class NovelSite(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.counts: Counter[str] = Counter()
        self.delays: dict[str, float] = {}
        self.lock = threading.Lock()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"

    def hits(self, path: str) -> int:
        with self.lock:
            return self.counts[path]

    def total_hits(self) -> int:
        with self.lock:
            return sum(self.counts.values())


class Handler(BaseHTTPRequestHandler):
    server: NovelSite

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - http.server 接口
        path = urlsplit(self.path).path
        with self.server.lock:
            self.server.counts[path] += 1
        delay = self.server.delays.get(path)
        if delay:
            time.sleep(delay)
        if path == "/robots.txt":
            return self.send(404, b"", "text/plain")
        if path in {"/", "/book/"}:
            return self.send(200, _catalogue().encode(), "text/html; charset=utf-8")
        if path == "/rank":
            return self.send(200, b"<html><h1>rank</h1></html>", "text/html; charset=utf-8")
        number, page = _parse_chapter(path)
        if number is None:
            return self.send(404, b"missing", "text/plain")
        if number == 4:
            return self.send(404, b"<html>gone</html>", "text/html; charset=utf-8")
        if page > CHAPTER_PAGES[number]:
            return self.send(404, b"<html>end</html>", "text/html; charset=utf-8")
        return self.send(200, _chapter_page(number, page).encode(), "text/html; charset=utf-8")

    def send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _parse_chapter(path: str) -> tuple[int | None, int]:
    if not path.startswith("/book/") or not path.endswith(".html"):
        return None, 1
    name = path[len("/book/") : -len(".html")]
    if "_" in name:
        head, _, tail = name.partition("_")
        if head.isdigit() and tail.isdigit():
            return int(head), int(tail)
        return None, 1
    return (int(name), 1) if name.isdigit() else (None, 1)


@contextmanager
def novel_site():
    """起一个本地小说站，退出时确保线程结束。"""
    site = NovelSite()
    thread = threading.Thread(target=site.serve_forever, daemon=True)
    thread.start()
    try:
        yield site
    finally:
        site.shutdown()
        site.server_close()
        thread.join(timeout=5)
