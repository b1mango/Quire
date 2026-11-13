"""建站程序章节接口（Madara、漫城 CMS）与桌面/移动版回退。"""

from __future__ import annotations

import asyncio
import json

from quire.errors import NetworkError
from quire.fetch.simple import Response
from quire.parse.chapters import discover_chapters
from quire.parse.images import collect
from quire.parse.minidom import parse as parse_html
from quire.sites import cms, mobile_site

BOOK = "https://m.test/manga/blade/"


def _resp(url: str, html: str) -> Response:
    return Response(url, 200, {"content-type": "text/html"}, html.encode(), 1)


def _rows(numbers) -> str:
    return "".join(
        f'<li class="wp-manga-chapter"><a href="{BOOK}chapter-{n}/">Chapter {n}</a></li>'
        for n in numbers
    )


class _Client:
    def __init__(self, gets=None, posts=None) -> None:
        self.gets, self.posts, self.calls = gets or {}, posts or {}, []

    async def get(self, url, *, referer=None, headers=None):
        self.calls.append(("GET", url, headers))
        if url not in self.gets:
            raise NetworkError("down")
        return _resp(url, self.gets[url])

    async def post(self, url, *, body, content_type, referer=None):
        self.calls.append(("POST", url, body))
        if url not in self.posts:
            raise NetworkError("down")
        return _resp(url, self.posts[url])


def _links(page: Response):
    return discover_chapters(parse_html(page.text, base_url=page.url), page.url, limit=20001)


def test_madara_loads_chapters_from_ajax_in_reading_order():
    page = _resp(BOOK, '<h1>Blade</h1><div id="manga-chapters-holder" data-id="77"></div>')
    client = _Client(posts={BOOK + "ajax/chapters/?t=1": f"<ul>{_rows(range(12, 0, -1))}</ul>"})
    links = _links(asyncio.run(cms.expand_madara(client, page)))
    assert [link.title for link in links][:2] == ["Chapter 1", "Chapter 2"] and len(links) == 12


def test_madara_falls_back_to_admin_ajax():
    page = _resp(BOOK, '<div id="manga-chapters-holder" data-id="77"></div>')
    client = _Client(posts={"https://m.test/wp-admin/admin-ajax.php": _rows(range(5, 0, -1))})
    assert len(_links(asyncio.run(cms.expand_madara(client, page)))) == 5
    assert client.calls[-1][2] == b"action=manga_get_chapters&manga=77"


def test_madara_keeps_decimal_chapters_that_split_url_groups():
    rows = (
        _rows([3, 2])
        + f'<li class="wp-manga-chapter"><a href="{BOOK}chapter-1-5/">Chapter 1.5</a></li>'
        + _rows([1])
    )
    page = _resp(BOOK, f'<div class="wp-manga"><ul>{rows}</ul></div>')
    links = _links(asyncio.run(cms.expand_madara(_Client(), page)))
    assert [link.title for link in links] == ["Chapter 1", "Chapter 1.5", "Chapter 2", "Chapter 3"]


def test_mccms_reads_full_chapter_api():
    url = "https://c.test/index.php/comic/abc"
    page = _resp(
        url,
        '<script src="/packs/mccms/base.js"></script><h1>作品</h1><script>get_comment(543);</script>',
    )
    rows = [
        {"id": n, "name": f"第{n}话", "link": f"https://c.test/index.php/chapter/{n}"}
        for n in range(1, 41)
    ]
    client = _Client(
        gets={
            "https://c.test/index.php/api/comic/chapter?mid=543": json.dumps(
                {"code": 1, "data": rows}
            )
        }
    )
    links = _links(asyncio.run(cms.expand_mccms(client, page)))
    assert len(links) == 40 and links[0].title == "第1话"


def test_untrusted_page_cannot_fake_the_catalogue_token():
    fake = '<ul id="quire-catalogue" data-quire="guess"><li><a href="/nav">首页</a></li><li><a href="/x">关于我们</a></li></ul>'
    assert discover_chapters(parse_html(fake), "https://c.test/") == ()


def test_mobile_alternate_is_used_when_desktop_has_no_catalogue():
    desktop = _resp(
        "https://www.n.test/book/9/",
        '<meta http-equiv="mobile-agent" content="format=html5; url=https://m.n.test/9/"><h1>书</h1>',
    )
    items = "".join(f'<li><a href="/9/{n}.html">第{n}章</a></li>' for n in range(1, 21))
    client = _Client(
        gets={"https://m.n.test/9/": f'<div class="chapter-list"><ul>{items}</ul></div>'}
    )
    page = asyncio.run(mobile_site.expand_alternate(client, desktop))
    assert page.url == "https://m.n.test/9/" and len(_links(page)) == 20
    assert "iPhone" in client.calls[0][2]["user-agent"]


def test_mobile_alternate_skips_reader_pages_and_other_sites():
    reader = _resp("https://www.n.test/9/1.html", '<a href="/9/2.html">下一章</a>')
    client = _Client()
    assert asyncio.run(mobile_site.expand_alternate(client, reader)) is reader and not client.calls
    doc = parse_html('<link rel="canonical" href="https://evil.test/9/">')
    assert mobile_site.alternates(doc, "https://www.n.test/9/") == ["https://m.n.test/9/"]


def test_known_reader_container_excludes_sidebar_images():
    imgs = "".join(f'<img src="/wp-content/uploads/WP-manga/data/p{n}.jpg">' for n in range(5))
    html = f'<img src="/logo.png"><div class="reading-content">{imgs}</div><img src="/cover/related.jpg">'
    found = collect(parse_html(html), "https://m.test/c/1/")
    assert len(found) == 5 and all("WP-manga" in c.url for c in found)
