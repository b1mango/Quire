"""quanben.io 目录适配:JSONP 参数重建、响应解析与页面并入。"""

from __future__ import annotations

import asyncio
import json
import random
import re

import pytest

from quire.errors import NetworkError, ParseError
from quire.fetch.simple import Response
from quire.parse.chapters import discover_chapters
from quire.parse.minidom import parse as parse_html
from quire.sites import quanben

LIST_URL = "https://quanben.io/n/testbook/list.html"

_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>测试书 - 全本小说网</title>
<script>
function load_more(book){}
var callback='a1b2';
function da1b2(data) {}
</script></head>
<body>
<h1>测试书</h1>
<div class="breadcrumb"><a href="/">首页</a> <a href="/c/junshi.html">军事小说</a></div>
<ul class="list3">
<li><a href="/n/testbook/1.html"><span>第1章 开始</span></a></li>
<li><a href="/n/testbook/2.html"><span>第2章 继续</span></a></li>
</ul>
<div class="content_more" id="detail"><a href="javascript:void(0)" onclick="load_more('174634')">[展开完整列表]</a></div>
<ul class="list3">
<li><a href="/n/testbook/9.html"><span>第9章 尾声上</span></a></li>
<li><a href="/n/testbook/10.html"><span>第10章 尾声下</span></a></li>
</ul>
</body></html>
"""


def _page() -> Response:
    return Response(LIST_URL, 200, {"content-type": "text/html; charset=utf-8"}, _PAGE.encode(), 0)


def _jsonp(callback: str, numbers: range) -> str:
    items = "".join(
        f'<li><a href="/n/testbook/{n}.html"><span>第{n}章 章节{n}</span></a></li>' for n in numbers
    )
    payload = json.dumps({"id": "174634", "content": f'<ul class="list3">{items}</ul>'})
    return f"d{callback}({payload});"


class _Client:
    """记录请求并按地址返回 JSONP 的假客户端。"""

    def __init__(self, body: str | None = None, error: Exception | None = None) -> None:
        self.requests: list[tuple[str, str | None]] = []
        self.body, self.error = body, error

    async def get(self, url: str, *, referer: str | None = None) -> Response:
        self.requests.append((url, referer))
        if self.error is not None:
            raise self.error
        assert self.body is not None
        return Response(
            url, 200, {"content-type": "text/html; charset=utf-8"}, self.body.encode(), 0
        )


def test_is_list_page() -> None:
    assert quanben.is_list_page(LIST_URL)
    assert quanben.is_list_page("https://www.quanben.io/n/testbook/list.html")
    assert not quanben.is_list_page("https://quanben.io/n/testbook/37.html")
    assert not quanben.is_list_page("https://other.test/n/testbook/list.html")


def test_obfuscate_roundtrip() -> None:
    token = quanben.obfuscate("1a9664", random.Random(7))
    assert len(token) == 6 * 3
    # 服务端只取每三字符的中间位反查:重建值必须能还原原 callback
    decoded = ""
    for index in range(0, len(token), 3):
        char = token[index + 1]
        position = quanben._STATIC_CHARS.find(char)
        decoded += quanben._STATIC_CHARS[(position - 3) % len(quanben._STATIC_CHARS)]
    assert decoded == "1a9664"


def test_expand_listing_fetches_jsonp_with_referer() -> None:
    client = _Client(_jsonp("a1b2", range(3, 9)))
    merged = asyncio.run(quanben.expand_listing(client, _page(), rng=random.Random(1)))
    assert len(client.requests) == 1
    url, referer = client.requests[0]
    assert referer == LIST_URL
    query = dict(part.split("=", 1) for part in url.split("?", 1)[1].split("&"))
    assert query["c"] == "book" and query["a"] == "list.jsonp"
    assert query["callback"] == "a1b2" and query["book_id"] == "174634"
    assert re.fullmatch(r"[A-Za-z0-9]{12}", query["b"])
    links = discover_chapters(parse_html(merged.text, base_url=merged.url), merged.url)
    assert [link.number for link in links] == list(range(1, 11))


def test_expand_listing_orders_by_url_despite_duplicate_titles() -> None:
    """站点存在标题重号的章节(实测第1156章出现两次):顺序以 URL 章号为准。"""
    client = _Client(_jsonp("a1b2", range(3, 9)))
    page = _page()
    html = page.content.decode().replace("第9章 尾声上", "第8章 重号")  # 页面尾段与接口段标题撞号
    page = Response(page.url, page.status, page.headers, html.encode(), page.elapsed_ms)
    merged = asyncio.run(quanben.expand_listing(client, page, rng=random.Random(1)))
    links = discover_chapters(parse_html(merged.text, base_url=merged.url), merged.url)
    paths = [link.url.rsplit("/", 1)[-1] for link in links]
    assert paths == [f"{n}.html" for n in range(1, 11)]


def test_expand_listing_passthrough() -> None:
    client = _Client("unused")
    other = Response(
        "https://example.test/list", 200, {"content-type": "text/html"}, b"<html></html>", 0
    )
    assert asyncio.run(quanben.expand_listing(client, other)) is other
    # quanben 地址但页面结构变了(没有内嵌参数):同样原样返回
    broken = Response(
        LIST_URL, 200, {"content-type": "text/html; charset=utf-8"}, b"<html></html>", 0
    )
    assert asyncio.run(quanben.expand_listing(client, broken)) is broken
    assert not client.requests


def test_expand_listing_falls_back_on_failure() -> None:
    client = _Client(error=NetworkError("connection reset"))
    page = _page()
    assert asyncio.run(quanben.expand_listing(client, page)) is page


def test_jsonp_content_rejects_bad_payload() -> None:
    with pytest.raises(ParseError):
        quanben.jsonp_content("参数错误", "a1b2")
    with pytest.raises(ParseError):
        quanben.jsonp_content('da1b2({"id": "1"});', "a1b2")
    with pytest.raises(ParseError):
        quanben.jsonp_content('da1b2({"content": "<p>没有链接</p>"});', "a1b2")
    assert "<a " in quanben.jsonp_content(_jsonp("a1b2", range(3, 4)), "a1b2")
