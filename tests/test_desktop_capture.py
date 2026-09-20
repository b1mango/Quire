"""Desktop regressions: explicit modes, hash routes and dynamic capture end to end."""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock

import pytest

from quire.core_novel import plan_chapters
from quire.errors import ConfigError, NoChaptersError, ParseError
from quire.fetch.browser_process import find_chrome
from quire.fetch.simple import Response
from quire.models import NovelOptions
from quire.parse.chapters import discover_chapters, page_key
from quire.parse.minidom import parse
from quire.server import probe as probing
from quire.server.job_state import JobSpec
from quire.server.probe import _inspect, probe_url
from quire.utils.urls import join_document_url
from tests.mock_site.novel_server import _chapter_page
from tests.test_server_e2e import _request, _wait_job, ui  # noqa: F401

URL = "https://example.test/#/book/10/"
CATALOGUE = """<h1>测试目录</h1><span id="lastchapter"><a href="/#/book/10/3">第3章 尾声</a></span>
<a href="/#/book/10/1">开始阅读</a><div id="list">
<a href="#/book/10/1">第1章 起点</a><a href="/#/book/10/2">第2章 最后一页</a>
<a href="/#/book/10/3">第3章 尾声</a></div>"""


def response(html, url=URL):
    return Response(url, 200, {"content-type": "text/html; charset=utf-8"}, html.encode(), 0)


def test_hash_catalogue_keeps_routes_and_reading_order():
    links = discover_chapters(parse(CATALOGUE), URL)
    assert [x.title for x in links] == ["第1章 起点", "第2章 最后一页", "第3章 尾声"]
    assert [x.url for x in links] == [URL + str(n) for n in range(1, 4)]
    assert page_key(URL + "1") != page_key(URL + "2")
    assert page_key("https://example.test/a#one") == page_key("https://example.test/a#two")
    assert join_document_url(URL, "#!/read/2") == "https://example.test/#!/read/2"
    assert join_document_url(URL, "#heading") == ""
    assert join_document_url(URL, "javascript:alert(1)") == ""


def test_explicit_novel_single_never_follows_sidebar_catalogue():
    page = response(_chapter_page(1, 1) + CATALOGUE)
    plan = plan_chapters(page, NovelOptions(capture_mode="single"))
    assert [link.url for link in plan.links] == [URL]
    assert plan.preloaded[URL] == page
    with pytest.raises(NoChaptersError):
        plan_chapters(response("<h1>没有目录</h1>"), NovelOptions(capture_mode="catalogue"))
    assert (
        len(plan_chapters(response(CATALOGUE), NovelOptions(capture_mode="catalogue")).links) == 3
    )


@pytest.mark.parametrize("kind", ["novel", "manga"])
def test_probe_respects_explicit_kind_and_mode(kind):
    result = _inspect(response(CATALOGUE), kind, "catalogue", "none", None)
    assert result.kind == kind and result.count == 3 and result.series == (kind == "manga")
    with pytest.raises(ParseError, match="章节列表"):
        _inspect(response(_chapter_page(1, 1)), kind, "catalogue", "none", None)
    single = (
        _chapter_page(1, 1)
        if kind == "novel"
        else '<img src="/pages/001.jpg" width="800" height="1200">'
    )
    result = _inspect(response(single), kind, "single", "none", None)
    assert result.kind == kind and result.count == 1 and not result.series
    if kind == "novel":
        with pytest.raises(ParseError):
            _inspect(response(single), "manga", "single", "none", None)
    else:
        assert _inspect(response(single), "novel", "single", "none", None).kind == "novel"


@pytest.mark.parametrize(
    "fields",
    [
        {"capture_mode": "invalid"},
        {"capture_mode": []},
        {"render": "true"},
        {"capture_mode": "single", "series": True},
        {"kind": []},
    ],
)
def test_task_rejects_invalid_modes(fields):
    spec = {"kind": "manga", "url": URL, "title": "t", "formats": ("cbz",)} | fields
    with pytest.raises(ConfigError):
        JobSpec(**spec)


def test_invalid_core_mode():
    with pytest.raises(ConfigError):
        NovelOptions(capture_mode="invalid")


def test_script_shell_falls_back_to_renderer(monkeypatch):
    client = AsyncMock()
    client.get.return_value = response('<script src="/app.js"></script>', "https://example.test/")
    client.__aenter__.return_value = client
    monkeypatch.setattr(probing, "AsyncFetcher", lambda **kw: client)
    renderer = AsyncMock(return_value=(response(CATALOGUE), ()))
    monkeypatch.setattr(probing, "render_page", renderer)
    result = asyncio.run(probe_url("https://example.test/", kind="novel", capture_mode="catalogue"))
    assert result.render and result.count == 3
    renderer.assert_awaited_once()
    with pytest.raises(ConfigError):
        asyncio.run(probe_url(URL, kind="wrong"))
    with pytest.raises(ConfigError):
        asyncio.run(probe_url(URL, capture_mode=None))


def test_catalogue_limit_is_explicit():
    links = "".join(f'<a href="/{n}">第{n}章</a>' for n in range(20001))
    with pytest.raises(ParseError, match="20000"):
        _inspect(response(links), "novel", "catalogue", "none", None)


def test_dynamic_hash_catalogue_to_finished_book(ui):  # noqa: F811
    if not find_chrome():
        pytest.skip("system Chrome needed for real dynamic capture")
    catalogue = CATALOGUE.replace("/#/book/10/", "#/book/10/")
    chapters = {
        str(n): _chapter_page(n, 1).replace('href="/book/', 'href="#/book/10/') for n in (1, 3)
    }
    chapters["2"] = _chapter_page(1, 1).replace("第一章 起点", "第二章 分页")
    script = (
        f'const chapters={json.dumps(chapters)}; const route=location.hash.split("/").pop();'
        f"document.body.innerHTML=chapters[route] || {json.dumps(catalogue)};"
    ).encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):  # noqa: N802
            if self.path == "/robots.txt":
                body, content_type = b"User-agent: *\nAllow: /", "text/plain"
            elif self.path == "/app.js":
                body, content_type = script, "text/javascript"
            else:
                body, content_type = b'<script src="/app.js" defer></script>', "text/html"
            self.send_response(200)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    site = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=site.serve_forever, daemon=True).start()
    server, _ = ui
    try:
        url = f"http://127.0.0.1:{site.server_port}/#/book/10/"
        status, found = _request(
            server,
            "POST",
            "/api/probe",
            {
                "url": url,
                "kind": "novel",
                "capture_mode": "catalogue",
            },
        )
        assert status == 200 and found["count"] == 3 and found["render"]
        for mode, entry, expected in (("catalogue", url, 3), ("single", url + "1", 1)):
            status, job = _request(
                server,
                "POST",
                "/api/jobs",
                {
                    "url": entry,
                    "kind": "novel",
                    "title": mode,
                    "formats": ["txt"],
                    "capture_mode": mode,
                    "render": True,
                    "ocr": "never",
                },
            )
            assert status == 201
            done = _wait_job(server, job["id"])
            assert done["status"] == "done", done
            assert done["done"] == expected
    finally:
        site.shutdown()
        site.server_close()


def test_latest_list_cannot_hide_full_catalogue():
    html = '<div class="latest-list"><a href="/3">第3章</a><a href="/4">第4章</a></div>'
    html += "".join(f'<a href="/{n}">第{n}章</a>' for n in range(1, 5))
    assert [link.number for link in discover_chapters(parse(html), URL)] == [1, 2, 3, 4]


@pytest.mark.parametrize(
    "first,next_url,allowed",
    [
        ("?page=1#/book/1", "?page=2#/book/2", False),
        ("?page=1#/book/1", "?page=2#/book/1", True),
        ("#/book/1?page=1", "#/book/1?page=2", True),
        ("#/book/1?page=1", "#/book/2?page=2", False),
        ("#/book/1", "#/book/1_2", True),
        ("#/book/1", "#/book/2", False),
    ],
)
def test_hash_pagination_identity(first, next_url, allowed):
    from quire.parse.chapters import find_next_page

    base = "https://example.test/"
    doc = parse(f'<a rel="next" href="{base + next_url}">Next</a>')
    assert find_next_page(doc, base + first, base + first) == (base + next_url if allowed else None)
    doc = parse(f'<a href="{base + next_url}">下一页</a>')
    assert find_next_page(doc, base + first, base + first) == (base + next_url if allowed else None)


def test_manga_single_collects_continuation_but_stops_at_next_chapter():
    from quire.core_discovery import discover_manga
    from quire.models import MangaOptions

    pages = {
        "https://example.test/1": response(
            '<img src="/1.jpg"><a href="/1_2">下一页</a>', "https://example.test/1"
        ),
        "https://example.test/1_2": response(
            '<img src="/2.jpg"><a href="/2">下一章</a>', "https://example.test/1_2"
        ),
    }
    client = AsyncMock()
    client.get.side_effect = lambda url, **kw: pages[url]
    found, _ = asyncio.run(
        discover_manga(client, "https://example.test/1", MangaOptions(follow_pages=True))
    )
    assert [p.url for p in found] == ["https://example.test/1.jpg", "https://example.test/2.jpg"]
    assert client.get.await_count == 2


def _mock_client(pages: dict[str, Response]):
    client = AsyncMock()

    async def get(url, **kwargs):
        return pages[url]

    client.get.side_effect = get
    client.__aenter__.return_value = client
    return client


def test_probe_estimates_size_from_image_samples(monkeypatch):
    page_url = "https://example.test/ch"
    images = {f"https://example.test/p{n}.jpg": 1000 + n for n in range(1, 5)}
    html = "<h1>单章</h1>" + "".join(
        f'<img src="/p{n}.jpg" width="800" height="1200">' for n in range(1, 5)
    )
    pages = {page_url: response(html, page_url)}
    for url, size in images.items():
        pages[url] = Response(url, 200, {"content-type": "image/jpeg"}, b"x" * size, 0)
    client = _mock_client(pages)
    monkeypatch.setattr(probing, "AsyncFetcher", lambda **kw: client)

    result = asyncio.run(probe_url(page_url, kind="manga", capture_mode="single"))
    expected = (1001 + 1002 + 1003) // 3 * 4
    assert result.estimate_bytes == expected
    calls = [c for c in client.get.call_args_list if c.args[0] in images]
    assert len(calls) == 3 and all(c.kwargs.get("robots") is False for c in calls)


def test_probe_estimates_series_from_first_chapter(monkeypatch):
    catalogue_url = "https://example.test/book"
    chapter_url = "https://example.test/ch1"
    catalogue = "<h1>书</h1>" + "".join(f'<a href="/ch{n}">第{n}章</a>' for n in (1, 2))
    chapter = "<h1>第1章</h1>" + "".join(
        f'<img src="/c1-{n}.jpg" width="800" height="1200">' for n in (1, 2)
    )
    pages = {
        catalogue_url: response(catalogue, catalogue_url),
        chapter_url: response(chapter, chapter_url),
        "https://example.test/c1-1.jpg": Response(
            "https://example.test/c1-1.jpg", 200, {}, b"x" * 500, 0
        ),
        "https://example.test/c1-2.jpg": Response(
            "https://example.test/c1-2.jpg", 200, {}, b"x" * 700, 0
        ),
    }
    monkeypatch.setattr(probing, "AsyncFetcher", lambda **kw: _mock_client(pages))

    result = asyncio.run(probe_url(catalogue_url, kind="manga", capture_mode="catalogue"))
    assert result.series and result.count == 2
    # 2 张样本均值 600 × 首章 2 页 × 2 章
    assert result.estimate_bytes == 600 * 2 * 2


def test_probe_estimates_each_preset_with_real_image(monkeypatch):
    from io import BytesIO

    from PIL import Image

    image = Image.effect_noise((400, 600), 80).convert("RGB")
    buffer = BytesIO()
    image.save(buffer, "PNG")
    image.close()
    url = "https://example.test/ch"
    pages = {
        url: response('<h1>Chapter</h1><img src="/p.jpg">', url),
        "https://example.test/p.jpg": Response(url, 200, {}, buffer.getvalue(), 0),
    }
    monkeypatch.setattr(probing, "AsyncFetcher", lambda **kw: _mock_client(pages))
    result = asyncio.run(probe_url(url, kind="manga", capture_mode="single"))
    assert (
        result.estimates["archive"] > result.estimates["balanced"] > result.estimates["small"] > 0
    )
