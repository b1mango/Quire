import base64
import json

from quire.parse.chapters import discover_chapters
from quire.parse.minidom import parse
from quire.parse.tencent import extract_tencent_images


def test_reader_uses_last_nonce_without_executing_javascript():
    payload = base64.b64encode(
        json.dumps({"picture": [{"url": "https://images.test/page.jpg"}]}).encode()
    ).decode()
    encoded = payload[:2] + "ab" + payload[2:]
    html = """<script>window["no" + "nce"] = '' + '99wrong';
    window["non"+"ce"] = "" + (+eval("1+1")).toString() + "ab";</script>"""
    html += f"<script>var DATA = '{encoded}';</script>"
    assert extract_tencent_images(html) == ["https://images.test/page.jpg"]
    assert extract_tencent_images(html.replace("1+1", "__import__(1)")) == []


def test_gugu_reader_links_are_explicit_and_in_reading_order():
    html = '<a href="http://ac.qq.com/ComicView/index/id/1/cid/5">第一回 下</a><a href="http://ac.qq.com/ComicView/index/id/1/cid/2">第一回 上</a>'
    links = discover_chapters(parse(html), "http://www.gugu5.cc/o/book/")
    assert [link.title for link in links] == ["第一回 上", "第一回 下"]
    assert not discover_chapters(parse(html), "http://unrelated.test/")


def test_comicinfo_catalogue_yields_comicview_chapters():
    """腾讯 ComicInfo 目录页:静态 HTML 直出 ComicView 章节链接,无需渲染。"""
    from quire.parse.chapters import looks_like_catalogue

    base = "https://ac.qq.com/Comic/ComicInfo/id/505430"
    items = "".join(
        f'<li><a href="/ComicView/index/id/505430/cid/{n}">第{n}话 标题{n}</a></li>'
        for n in (1, 2, 3)
    )
    html = f"<html><body><ul class='chapter-page-list'>{items}</ul></body></html>"
    links = discover_chapters(parse(html), base)
    assert looks_like_catalogue(links)
    assert [link.url for link in links] == [
        f"https://ac.qq.com/ComicView/index/id/505430/cid/{n}" for n in (1, 2, 3)
    ]


def test_unknown_reader_envelope_does_not_export_decorations():
    import pytest

    from quire.errors import NoImagesError
    from quire.fetch.simple import Response
    from quire.manga import discover_page
    from quire.models import MangaOptions

    url = "https://ac.qq.com/ComicView/index/id/1/cid/2"
    html = b'<h1>reader</h1><img src="https://images.test/logo.jpg">'
    with pytest.raises(NoImagesError):
        discover_page(url, MangaOptions(), Response(url, 200, {}, html, 0))
