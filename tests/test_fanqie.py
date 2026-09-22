"""fanqienovel.com 目录适配:__INITIAL_STATE__ 内嵌目录的重建。"""

from __future__ import annotations

import asyncio

from quire.fetch.simple import Response
from quire.parse.chapters import discover_chapters
from quire.parse.minidom import parse
from quire.sites import fanqie

BOOK_URL = "https://fanqienovel.com/page/7000000000000000001"


def book_page(entries: list[tuple[str, str]] | None, *, state: bool = True) -> Response:
    if entries is None:
        payload = ""
    else:
        items = ",".join(
            f'{{"itemId":"{item_id}","title":"{title}"}}' for item_id, title in entries
        )
        payload = f'"chapterListWithVolume":[[{items}]]'
    script = f"<script>window.__INITIAL_STATE__={{{payload}}}</script>" if state else ""
    html = f"<html><head><title>测试书</title></head><body>{script}</body></html>"
    return Response(BOOK_URL, 200, {"content-type": "text/html; charset=utf-8"}, html.encode(), 0)


def expand(page: Response) -> Response:
    return asyncio.run(fanqie.expand_listing(None, page))


def test_book_page_listing_expands_from_embedded_state() -> None:
    page = expand(
        book_page([("7000000000000000101", "第1章 开始"), ("7000000000000000102", "第2章 继续")])
    )
    doc = parse(page.text, base_url=page.url)
    links = discover_chapters(doc, page.url)
    assert [link.title for link in links] == ["第1章 开始", "第2章 继续"]
    assert links[0].url == "https://fanqienovel.com/reader/7000000000000000101"


def test_injected_listing_wins_title_over_static_anchors() -> None:
    """页面自带的「最近更新」锚点标题不干净,重建列表插在前头让其胜出。"""
    page = book_page([("7000000000000000102", "第2章 继续")])
    html = page.text.replace(
        "<body>",
        '<body><a href="/reader/7000000000000000102">最近更新：第2章 继续</a>',
    )
    page = expand(
        Response(BOOK_URL, 200, {"content-type": "text/html; charset=utf-8"}, html.encode(), 0)
    )
    links = discover_chapters(parse(page.text, base_url=page.url), page.url)
    assert [link.title for link in links] == ["第2章 继续"]


def test_non_book_page_passes_through() -> None:
    page = Response(
        "https://fanqienovel.com/reader/7000000000000000101", 200, {}, b"<html></html>", 0
    )
    assert expand(page) is page
    assert not fanqie.is_book_page("https://example.com/page/123")


def test_missing_or_malformed_state_passes_through() -> None:
    assert expand(book_page(None)) is not None  # 无 chapterListWithVolume:原样
    page = book_page(None, state=False)
    assert expand(page) is page
    broken = Response(
        BOOK_URL,
        200,
        {},
        b'<script>window.__INITIAL_STATE__={"chapterListWithVolume":[[{"itemId":1}]]}</script>',
        0,
    )
    assert expand(broken) is broken
