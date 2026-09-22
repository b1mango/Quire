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
        pump = asyncio.create_task(tap.pump())
        await asyncio.sleep(0)
        pump.cancel()
        await asyncio.gather(pump, return_exceptions=True)
        assert tap.urls == [urls[0]]
        assert cdp.call.await_count == len(urls)

    asyncio.run(run())
