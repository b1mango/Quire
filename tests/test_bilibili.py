"""sites/bilibili.py 的纯函数部分:阅读页判定、总页数解析、清单页重建。"""

from __future__ import annotations

import pytest

from quire.manga import discover_page
from quire.models import MangaOptions
from quire.sites.bilibili import _listing_response, _page_number, _total_pages, is_reader_page


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://manga.bilibili.com/mc25969/290775", True),
        ("https://manga.bilibili.com/mc25969/290775?from=manga_detail", True),
        ("https://manga.bilibili.com/detail/mc25969", False),  # 详情页不是阅读页
        ("https://manga.bilibili.com/", False),
        ("https://www.bilibili.com/mc25969/290775", False),
        ("https://manga.bilibili.com.evil.test/mc1/2", False),
    ],
)
def test_is_reader_page(url, expected):
    assert is_reader_page(url) is expected


def test_total_pages_from_reader_chrome_text():
    state = {"text": "?\n1\n2\n54P\n第 001 话\n已有312条弹幕\n显示工具栏"}
    assert _total_pages(state) == 54
    assert _total_pages({"text": "没有页码"}) == 0
    assert _total_pages({}) == 0


def test_page_number_parsing():
    assert _page_number({"page": "12"}) == 12
    assert _page_number({"page": ""}) == 0
    assert _page_number({}) == 0


def test_listing_response_feeds_generic_discovery_in_order():
    urls = [
        f"https://manga.hdslb.com/bfs/manga/ab{i:03d}.jpg@935w.avif?token=t{i}" for i in range(3)
    ]
    page = _listing_response("https://manga.bilibili.com/mc25969/290775", "第 001 话 & 标题", urls)
    candidates, result = discover_page(page.url, MangaOptions(rate=1000), page)
    assert [c.url for c in candidates] == urls
    assert all(c.referer == page.url for c in candidates)
    assert result.title.startswith("第 001 话")


@pytest.mark.parametrize("case", ["stalled", "unknown_total", "next_chapter", "extra_images"])
def test_incomplete_capture_is_never_success(case, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from quire.errors import FetchError
    from quire.fetch.browser import RenderOptions
    from quire.sites import bilibili

    url = "https://manga.bilibili.com/mc1/2"
    tap = SimpleNamespace(urls=["one"] if case != "extra_images" else ["one", "two", "three"])
    state = {"page": "1", "href": url if case != "next_chapter" else url + "3"}
    monkeypatch.setattr(bilibili, "_press_left", AsyncMock())
    monkeypatch.setattr(bilibili, "_state", AsyncMock(return_value=state))
    monkeypatch.setattr(bilibili.asyncio, "sleep", AsyncMock())
    with pytest.raises(FetchError):
        asyncio.run(
            bilibili._paginate(
                None, "session", tap, RenderOptions(), url, 0 if case == "unknown_total" else 2
            )
        )


def test_complete_capture_stops_before_next_chapter(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from quire.fetch.browser import RenderOptions
    from quire.sites import bilibili

    press = AsyncMock()
    monkeypatch.setattr(bilibili, "_press_left", press)
    asyncio.run(
        bilibili._paginate(
            None,
            "session",
            SimpleNamespace(urls=["one", "two"]),
            RenderOptions(),
            "https://manga.bilibili.com/mc1/2",
            2,
        )
    )
    press.assert_not_awaited()


def test_tap_deduplicates_tokens_and_excludes_encrypted_or_foreign_images():
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from quire.sites.bilibili import _ImageTap

    async def run():
        urls = [
            "https://a.hdslb.com/bfs/manga/a.jpg?token=first",
            "https://a.hdslb.com/bfs/manga/a.jpg?token=renewed",
            "https://a.hdslb.com/bfs/manga/b.jpg?token=t&cpx=1",
            "https://evil.test/bfs/manga/a.jpg?token=t",
            "https://a.hdslb.com/cover/a.jpg?token=t",
        ]
        queue = asyncio.Queue()
        cdp = SimpleNamespace(events=queue, call=AsyncMock())
        tap = _ImageTap(cdp, "session")
        for i, url in enumerate(urls):
            queue.put_nowait(
                {
                    "sessionId": "session",
                    "method": "Fetch.requestPaused",
                    "params": {"requestId": str(i), "request": {"url": url}},
                }
            )
        queue.put_nowait({"sessionId": "other", "method": "Fetch.requestPaused", "params": {}})
        queue.put_nowait({"sessionId": "session", "method": "Page.loadEventFired", "params": {}})
        from quire.errors import FetchError

        cdp.call.side_effect = [
            *([{}] * (len(urls) - 1)),
            FetchError("request cancelled"),
        ]
        pump = asyncio.create_task(tap.pump())
        await asyncio.sleep(0)
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
        assert tap.urls == [urls[0]]
        assert cdp.call.await_count == len(urls)

    asyncio.run(run())


def test_capture_reader_page_local_chrome_and_error_paths(monkeypatch):
    import asyncio
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from quire.errors import FetchError, UnsupportedError
    from quire.fetch.browser import RenderOptions
    from quire.sites import bilibili

    url = "https://manga.bilibili.com/mc1/2"
    with pytest.raises(UnsupportedError):
        asyncio.run(
            bilibili.capture_reader_page("https://manga.bilibili.com/detail/mc1", RenderOptions())
        )

    monkeypatch.setattr(bilibili, "find_chrome", lambda *args: None)
    with pytest.raises(UnsupportedError):
        asyncio.run(bilibili.capture_reader_page(url, RenderOptions()))

    process = AsyncMock()
    process.endpoint = "ws://127.0.0.1:9222/devtools/browser/x"
    process.__aenter__.return_value = process
    monkeypatch.setattr(bilibili, "find_chrome", lambda *args: "/fake/chrome")
    monkeypatch.setattr(bilibili, "ChromeProcess", lambda *args, **kwargs: process)

    states = [
        "{bad json",
        json.dumps({"ready": True, "page": "1", "title": "测试话", "href": url, "text": "2P"}),
        json.dumps({"ready": True, "page": "2", "title": "测试话", "href": url, "text": "2P"}),
        json.dumps({"ready": True, "page": "2", "title": "测试话", "href": url, "text": "2P"}),
    ]
    queue: asyncio.Queue = asyncio.Queue()

    async def call(method, params=None, **kwargs):
        if method == "Target.createTarget":
            return {"targetId": "owned"}
        if method == "Target.attachToTarget":
            return {"sessionId": "session"}
        if method == "Target.closeTarget":
            return {"success": True}
        if method == "Page.navigate":
            queue.put_nowait(
                {
                    "sessionId": "session",
                    "method": "Fetch.requestPaused",
                    "params": {
                        "requestId": "1",
                        "request": {"url": "https://a.hdslb.com/bfs/manga/1.jpg?token=t1"},
                    },
                }
            )
            return {}
        if method == "Input.dispatchKeyEvent" and params.get("type") == "keyUp":
            queue.put_nowait(
                {
                    "sessionId": "session",
                    "method": "Fetch.requestPaused",
                    "params": {
                        "requestId": "2",
                        "request": {"url": "https://a.hdslb.com/bfs/manga/2.jpg?token=t2"},
                    },
                }
            )
            return {}
        if method == "Runtime.evaluate":
            val = states.pop(0) if len(states) > 1 else states[0]
            return {"result": {"value": val}}
        return {}

    cdp = AsyncMock()
    cdp.events = queue
    cdp.call.side_effect = call
    cdp.__aenter__.return_value = cdp
    monkeypatch.setattr(bilibili, "Cdp", lambda _: cdp)

    async def fast_first_images(tap):
        while not tap.urls:
            await asyncio.sleep(0.01)

    monkeypatch.setattr(bilibili, "_await_first_images", fast_first_images)
    assert asyncio.run(bilibili._state(cdp, "session")) == {}

    page, total = asyncio.run(bilibili.capture_reader_page(url, RenderOptions(max_scrolls=1)))
    assert total == 2 and "1.jpg" in page.text and "2.jpg" in page.text

    # 空图片清单与导航错误
    async def no_images(tap):
        pass

    monkeypatch.setattr(bilibili, "_await_first_images", no_images)
    with pytest.raises(FetchError, match="未能捕获"):
        asyncio.run(bilibili.capture_reader_page(url, RenderOptions(), full=False))

    async def fail_nav(method, params=None, **kwargs):
        if method == "Target.createTarget":
            return {"targetId": "owned"}
        if method == "Target.attachToTarget":
            return {"sessionId": "session"}
        if method == "Target.closeTarget":
            return {"success": True}
        if method == "Page.navigate":
            return {"errorText": "net::ERR_FAILED"}
        return {}

    cdp.call.side_effect = fail_nav
    with pytest.raises(FetchError, match="导航失败"):
        asyncio.run(bilibili.capture_reader_page(url, RenderOptions(), full=False))

    # 翻页达到 max_scrolls 但未收齐页数
    tap = SimpleNamespace(urls=["one"])
    monkeypatch.setattr(
        bilibili,
        "_state",
        AsyncMock(
            side_effect=[
                {"page": "2", "href": url},
                {"page": "3", "href": url},
            ]
        ),
    )
    with pytest.raises(FetchError, match="上限"):
        asyncio.run(bilibili._paginate(cdp, "session", tap, RenderOptions(max_scrolls=1), url, 3))
