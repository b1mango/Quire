"""Virtual catalogue completeness and isolated API credential boundaries."""

import asyncio

import httpx
import pytest

from quire.errors import ConfigError
from quire.fetch.browser_catalogue import catalogue_script
from quire.fetch.session import AsyncFetcher
from quire.parse.chapters import discover_chapters
from quire.parse.minidom import parse


def test_materialized_catalogue_preserves_prologue_order_and_all_2202_entries():
    html = (
        '<section id="quire-catalogue">'
        + "".join(
            f'<a href="/reader/4994468?forceChapterIndex={i}&forceChapterId={i + 100}">'
            f"{'序章' if i == 0 else f'第{i}章 标题'}</a>"
            for i in range(2202)
        )
        + "</section>"
    )
    links = discover_chapters(parse(html), "https://t.shuqi.com/catalog/4994468/", limit=20001)
    from quire.fetch.simple import Response
    from quire.models import NovelOptions
    from quire.novel_plan import plan_chapters

    plan = plan_chapters(
        Response("https://t.shuqi.com/catalog/4994468/", 200, {}, html.encode(), 0),
        NovelOptions(capture_mode="catalogue", max_chapters=20000),
    )
    assert len(plan.links) == 2202 and plan.links[0].title == "序章"
    assert len(links) == 2202 and links[0].title == "序章"
    assert links[-1].title == "第2201章 标题"
    assert catalogue_script("https://t.shuqi.com/catalog/4994468/")
    assert catalogue_script("https://evil.test/catalog/4994468/") is None


def test_api_token_never_follows_cross_origin_redirect():
    requests = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"ok"

    def serve(request):
        requests.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.host == "ocean.shuqireader.com":
            return httpx.Response(302, headers={"location": "https://other.test/target"})
        return httpx.Response(200, stream=Body())

    async def run():
        async with AsyncFetcher(transport=httpx.MockTransport(serve), rate=1000) as client:
            await client.shuqi_request(
                "https://ocean.shuqireader.com/api",
                method="GET",
                headers={"Authorization": "test-token", "Cookie": "private"},
                body=None,
            )

    asyncio.run(run())
    own = next(r for r in requests if r.url.path == "/api")
    target = next(r for r in requests if r.url.path == "/target")
    assert own.headers["authorization"] == "test-token"
    assert "cookie" not in own.headers and "authorization" not in target.headers
    assert all("authorization" not in r.headers for r in requests if r.url.path == "/robots.txt")


def test_api_token_rejects_unrelated_host():
    client = AsyncFetcher()
    with pytest.raises(ConfigError):
        asyncio.run(
            client.shuqi_request("https://evil.test/api", method="GET", headers={}, body=None)
        )
