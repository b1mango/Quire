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
