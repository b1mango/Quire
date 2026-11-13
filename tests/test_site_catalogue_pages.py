"""通用目录补全：「查看全部章节」跟随与分页目录合并。"""

from __future__ import annotations

import asyncio

from quire.errors import NetworkError
from quire.fetch.simple import Response
from quire.parse.chapters import discover_chapters
from quire.parse.minidom import parse as parse_html
from quire.sites import catalogue_pages, full_catalogue

BASE = "https://n.test/book/9/"


def _resp(url: str, html: str) -> Response:
    return Response(url, 200, {"content-type": "text/html"}, html.encode(), 1)


def _list(numbers: range, extra: str = "") -> str:
    items = "".join(f'<dd><a href="/book/9/{n}.html">第{n}章 事{n}</a></dd>' for n in numbers)
    return f'<html><head><title>某书_站名</title></head><body><h1>某书</h1>{extra}<div class="listmain"><dl>{items}</dl></div></body></html>'


class _Client:
    def __init__(self, pages: dict[str, str], fail: str = "") -> None:
        self.pages, self.fail, self.calls = pages, fail, []

    async def get(self, url, *, referer=None, headers=None):
        self.calls.append(url)
        if url == self.fail:
            raise NetworkError("boom")
        return _resp(url, self.pages[url])


def _count(page: Response) -> int:
    return len(discover_chapters(parse_html(page.text, base_url=page.url), page.url, limit=20001))


def test_full_catalogue_link_is_followed_when_it_has_more_chapters():
    book = _resp(BASE, _list(range(1, 3), '<a href="/book/9/all.html">查看全部章节 &gt;&gt;</a>'))
    client = _Client({BASE + "all.html": _list(range(1, 51))})
    page = asyncio.run(full_catalogue.expand_listing(client, book))
    assert page.url.endswith("all.html") and _count(page) == 50


def test_full_catalogue_ignores_reader_pages():
    reader = _resp(
        BASE + "5.html", '<a href="/book/9/">目录</a><a href="/book/9/6.html">下一章</a>'
    )
    assert asyncio.run(full_catalogue.expand_listing(_Client({}), reader)) is reader


def test_select_paged_catalogue_is_merged_in_option_order():
    select = (
        '<select><option value="/book/9/index_1.html">第1-20章</option>'
        '<option value="/book/9/index_2.html">第21-40章</option></select>'
    )
    first = _resp(BASE + "index_1.html", _list(range(1, 21), select))
    client = _Client({BASE + "index_2.html": _list(range(21, 41), select)})
    page = asyncio.run(catalogue_pages.expand_pages(client, first))
    links = discover_chapters(parse_html(page.text, base_url=page.url), page.url, limit=20001)
    assert len(links) == 40 and links[0].title == "第1章 事1" and links[-1].title == "第40章 事40"
    assert parse_html(page.text).select_one("h1").text == "某书"


def test_next_page_catalogue_is_followed_until_it_ends():
    nxt = lambda n: f'<a href="/book/9/index_{n}.html">下一页</a>'  # noqa: E731
    first = _resp(BASE, _list(range(1, 21), nxt(2)))
    client = _Client(
        {
            BASE + "index_2.html": _list(range(21, 41), nxt(3)),
            BASE + "index_3.html": _list(range(41, 45)),
        }
    )
    assert _count(asyncio.run(catalogue_pages.expand_pages(client, first))) == 44


def test_failed_catalogue_page_keeps_original_instead_of_gaps():
    select = (
        '<select><option value="/book/9/index_1.html">1</option>'
        '<option value="/book/9/index_2.html">2</option><option value="/book/9/index_3.html">3</option></select>'
    )
    first = _resp(BASE + "index_1.html", _list(range(1, 21), select))
    client = _Client({BASE + "index_3.html": _list(range(41, 61))}, fail=BASE + "index_2.html")
    assert asyncio.run(catalogue_pages.expand_pages(client, first)) is first


def test_explicit_chapter_list_link_is_followed_from_a_sparse_book_page():
    book = _resp(
        BASE,
        '<h1>某书</h1><a href="/book/9/all/">章节列表</a><a href="/book/9/99.html">第99章 新</a>',
    )
    client = _Client({BASE + "all/": _list(range(1, 31))})
    assert _count(asyncio.run(full_catalogue.expand_listing(client, book))) == 30
    chapter = _resp(BASE + "2.html", '<p>正文</p><a href="/book/9/">目录</a>')
    assert asyncio.run(full_catalogue.expand_listing(_Client({}), chapter)) is chapter
