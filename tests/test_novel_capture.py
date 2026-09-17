"""章级抓取单元测试：用桩 fetcher 覆盖失败、渲染、取消与缓存校验路径。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from quire.core_chapters import capture_chapters, decode_chapter
from quire.errors import BlockedError, FetchError, LedgerError
from quire.fetch.simple import Response
from quire.models import NovelOptions
from quire.parse.chapters import ChapterLink
from quire.store.ledger import Ledger
from quire.store.models import ResourceSpec, task_identity

SOURCE = "http://example.test/book/"

PROSE = "".join(
    f"<p>他第{index}次抬头看见远山如黛，风从林间穿过，心里忽然安静下来，"
    f"于是继续往前走去，直到天色将晚才停下脚步。</p>"
    for index in range(1, 7)
)


def chapter_html(title: str, body: str = PROSE, *, tail: str = "") -> str:
    return f"<html><body><h1>{title}</h1><div id='content'>{body}</div>{tail}</body></html>"


class StubFetcher:
    """按地址返回预设页面；值是异常时直接抛出。"""

    def __init__(self, pages: dict[str, str | Exception | Response]) -> None:
        self.pages = pages
        self.requests: list[str] = []

    async def get(self, url: str, *, referer: str | None = None) -> Response:
        self.requests.append(url)
        value = self.pages[url]
        if isinstance(value, Exception):
            raise value
        if isinstance(value, Response):
            return value
        return Response(url=url, status=200, headers={}, content=value.encode(), elapsed_ms=1)


@contextmanager
def ledger_task(tmp_path: Path, links: tuple[ChapterLink, ...]) -> Iterator[tuple[Ledger, str]]:
    specs = [ResourceSpec(index + 1, 1, link.url) for index, link in enumerate(links)]
    identity = {"chapters": [[link.title, link.url] for link in links]}
    task_id, _ = task_identity(SOURCE, identity, specs)
    with Ledger(tmp_path / "work") as ledger:
        ledger.create_task(SOURCE, identity, specs)
        yield ledger, task_id


def run_capture(
    tmp_path: Path,
    links: tuple[ChapterLink, ...],
    fetcher: StubFetcher,
    **kwargs: object,
):
    options = kwargs.pop("options", None) or NovelOptions(rate=1000.0, retries=0, concurrency=2)
    with ledger_task(tmp_path, links) as (ledger, task_id):
        result = asyncio.run(
            capture_chapters(fetcher, ledger, task_id, links, options=options, **kwargs)  # type: ignore[arg-type]
        )
        return result, ledger.snapshot(task_id)


def test_blocked_and_failed_chapters_are_recorded_per_chapter(tmp_path: Path) -> None:
    links = (
        ChapterLink("第一章 正常", SOURCE + "1.html"),
        ChapterLink("第二章 拒绝", SOURCE + "2.html"),
        ChapterLink("第三章 网络", SOURCE + "3.html"),
    )
    fetcher = StubFetcher(
        {
            SOURCE + "1.html": chapter_html("第一章 正常"),
            SOURCE + "2.html": BlockedError("robots"),
            SOURCE + "3.html": FetchError("boom"),
        }
    )
    captured, snapshot = run_capture(tmp_path, links, fetcher)
    statuses = {record.spec.chapter: record.status for record in snapshot.resources}
    assert statuses == {1: "done", 2: "failed", 3: "failed"}
    codes = {record.spec.chapter: record.error_code for record in snapshot.resources}
    assert codes[2] == "blocked" and codes[3] == "network"
    assert captured.pages_fetched == 3


def test_oversized_page_is_rejected_before_parsing(tmp_path: Path) -> None:
    links = (ChapterLink("第一章", SOURCE + "1.html"),)
    fetcher = StubFetcher({SOURCE + "1.html": chapter_html("第一章")})
    _, snapshot = run_capture(
        tmp_path, links, fetcher, options=NovelOptions(retries=0, max_bytes=64)
    )
    assert snapshot.resources[0].error_code == "invalid_text"


def test_render_hook_is_applied_and_warnings_kept(tmp_path: Path) -> None:
    links = (ChapterLink("第一章", SOURCE + "1.html"),)
    fetcher = StubFetcher({SOURCE + "1.html": "<html><div id='content'>正在加载</div></html>"})
    calls: list[str] = []

    async def renderer(page: Response) -> tuple[Response, tuple[str, ...]]:
        calls.append(page.url)
        return (
            Response(
                url=page.url,
                status=200,
                headers={},
                content=chapter_html("第一章 渲染后").encode(),
                elapsed_ms=1,
            ),
            ("渲染时跳过了 1 张图",),
        )

    captured, snapshot = run_capture(tmp_path, links, fetcher, render=renderer)
    assert calls == [SOURCE + "1.html"]
    assert snapshot.resources[0].status == "done"
    assert captured.warnings == ("渲染时跳过了 1 张图",)


@pytest.mark.parametrize(
    ("error", "code"),
    [(FetchError("renderer failed"), "network"), (BlockedError("robots"), "blocked")],
)
def test_renderer_failure_is_recorded_and_later_chapters_continue(
    tmp_path: Path,
    error: Exception,
    code: str,
) -> None:
    links = tuple(ChapterLink(f"第{index}章", SOURCE + f"{index}.html") for index in (1, 2))
    fetcher = StubFetcher({link.url: chapter_html(link.title) for link in links})
    rendered: list[str] = []

    async def renderer(page: Response) -> tuple[Response, tuple[str, ...]]:
        rendered.append(page.url)
        if page.url == links[0].url:
            raise error
        return page, ()

    captured, snapshot = run_capture(
        tmp_path,
        links,
        fetcher,
        render=renderer,
        options=NovelOptions(rate=1000.0, retries=0, concurrency=1),
    )
    assert rendered == fetcher.requests == [link.url for link in links]
    assert [record.status for record in snapshot.resources] == ["failed", "done"]
    assert snapshot.resources[0].error_code == code
    assert snapshot.resources[0].local_path is None
    assert captured.pages_fetched == 2 and snapshot.status == "partial"
    assert _cached_chapter(tmp_path / "work", snapshot, 1).paragraphs


def test_preloaded_entry_page_is_not_requested_twice(tmp_path: Path) -> None:
    links = (ChapterLink("第一章", SOURCE + "1.html"),)
    fetcher = StubFetcher({SOURCE + "1.html": chapter_html("第一章")})
    preloaded = {
        SOURCE + "1.html": Response(
            url=SOURCE + "1.html",
            status=200,
            headers={},
            content=chapter_html("第一章 入口页").encode(),
            elapsed_ms=1,
        )
    }
    captured, snapshot = run_capture(tmp_path, links, fetcher, preloaded=preloaded)
    assert fetcher.requests == []
    assert captured.pages_fetched == 0
    assert snapshot.resources[0].status == "done"


def test_keep_html_writes_raw_pages(tmp_path: Path) -> None:
    links = (
        ChapterLink("第二章", SOURCE + "2.html"),
        ChapterLink("第二章 下一页", SOURCE + "2_2.html"),
    )
    fetcher = StubFetcher(
        {
            SOURCE + "2.html": chapter_html(
                "第二章", PROSE, tail=f"<a href='{SOURCE}2_2.html'>下一页</a>"
            ),
            SOURCE + "2_2.html": chapter_html("第二章 续", PROSE),
        }
    )
    html_dir = tmp_path / "html"
    html_dir.mkdir()
    with ledger_task(tmp_path, links[:1]) as (ledger, task_id):
        asyncio.run(
            capture_chapters(
                fetcher,
                ledger,
                task_id,
                links[:1],
                options=NovelOptions(rate=1000.0, retries=0),
                html_dir=html_dir,
            )
        )
    assert [path.name for path in sorted(html_dir.iterdir())] == [
        "00001-001.html",
        "00001-002.html",
    ]


def _cached_chapter(root: Path, snapshot, index: int = 0) -> object:
    from quire.store.cache import read_cached

    record = snapshot.resources[index]
    data = read_cached(
        root,
        snapshot.task_id,
        record.local_path or "",
        sha256=record.sha256 or "",
        size=record.size or 0,
    )
    return decode_chapter(data)


def test_catalogue_title_wins_over_page_heading(tmp_path: Path) -> None:
    links = (ChapterLink("第一章 美好标题", SOURCE + "1.html"),)
    fetcher = StubFetcher({SOURCE + "1.html": chapter_html("书名/第001章")})
    _, snapshot = run_capture(tmp_path, links, fetcher)
    cached = _cached_chapter(tmp_path / "work", snapshot)
    assert cached.title == "第一章 美好标题"
    assert cached.pages == 1 and cached.paragraphs and not cached.truncated


def test_page_heading_is_used_when_catalogue_title_has_no_mark(tmp_path: Path) -> None:
    links = (ChapterLink("无标记的标题", SOURCE + "2.html"),)
    fetcher = StubFetcher({SOURCE + "2.html": chapter_html("第一章 页面标题")})
    _, snapshot = run_capture(tmp_path, links, fetcher)
    assert _cached_chapter(tmp_path / "work", snapshot).title == "第一章 页面标题"


def test_validation_failure_marks_chapter_invalid(tmp_path: Path) -> None:
    links = (ChapterLink("第一章", SOURCE + "1.html"),)
    fetcher = StubFetcher({SOURCE + "1.html": "<html><h1>空</h1><div>太短了。</div></html>"})
    _, snapshot = run_capture(tmp_path, links, fetcher)
    assert snapshot.resources[0].error_code == "invalid_text"


def test_empty_after_cleaning_is_invalid(tmp_path: Path) -> None:
    links = (ChapterLink("第一章", SOURCE + "1.html"),)
    body = "".join("<p>请记住本站域名 www.example.com</p>" for _ in range(40))
    fetcher = StubFetcher({SOURCE + "1.html": chapter_html("第一章", body)})
    _, snapshot = run_capture(tmp_path, links, fetcher)
    assert snapshot.resources[0].error_code == "invalid_text"
    assert snapshot.resources[0].status == "failed"


def test_cancellation_marks_the_claimed_chapter_cancelled(tmp_path: Path) -> None:
    links = (ChapterLink("第一章", SOURCE + "1.html"),)

    class Hanging(StubFetcher):
        async def get(self, url: str, *, referer: str | None = None) -> Response:
            await asyncio.sleep(30)
            raise AssertionError("unreachable")

    with ledger_task(tmp_path, links) as (ledger, task_id):

        async def scenario() -> None:
            task = asyncio.create_task(
                capture_chapters(
                    Hanging({}),
                    ledger,
                    task_id,
                    links,
                    options=NovelOptions(rate=1000.0, retries=0),
                )
            )
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(scenario())
        snapshot = ledger.snapshot(task_id)
    assert snapshot.resources[0].status == "failed"
    assert snapshot.resources[0].error_code == "cancelled"


def test_progress_callback_receives_completed_counts(tmp_path: Path) -> None:
    links = (
        ChapterLink("第一章", SOURCE + "1.html"),
        ChapterLink("第二章", SOURCE + "2.html"),
    )
    fetcher = StubFetcher(
        {SOURCE + "1.html": chapter_html("第一章"), SOURCE + "2.html": chapter_html("第二章")}
    )
    seen: list[tuple[int, int]] = []
    run_capture(
        tmp_path, links, fetcher, on_progress=lambda done, total: seen.append((done, total))
    )
    assert sorted(seen) == [(1, 2), (2, 2)]


def test_decode_chapter_rejects_bad_payloads() -> None:
    with pytest.raises(LedgerError):
        decode_chapter(b"not json")
    with pytest.raises(LedgerError):
        decode_chapter(b'{"schema": 99}')
    with pytest.raises(LedgerError):
        decode_chapter(b'{"schema": 2, "title": 1, "paragraphs": [], "pages": 1}')
    cached = decode_chapter(b'{"schema": 2, "title": "t", "paragraphs": ["a"], "pages": 2}')
    assert (cached.title, cached.paragraphs, cached.pages) == ("t", ("a",), 2)
    assert cached.truncated is False
    assert cached.source == "html"
    assert cached.review == ()
    with pytest.raises(LedgerError):
        decode_chapter(b'{"schema": 2, "title": "t", "paragraphs": [], "pages": 1, "truncated": 3}')


def test_max_pages_truncation_is_reported_and_marked(tmp_path: Path) -> None:
    """分页超限不能静默：要有警告、缓存标记，成品里也要写明。"""
    links = (ChapterLink("第一章 长章", SOURCE + "1.html"),)
    pages = {}
    for number in range(1, 5):
        following = f"<a href='{SOURCE}1_{number + 1}.html'>下一页</a>" if number < 4 else ""
        pages[SOURCE + ("1.html" if number == 1 else f"1_{number}.html")] = chapter_html(
            "第一章 长章", PROSE, tail=following
        )
    fetcher = StubFetcher(pages)
    captured, snapshot = run_capture(
        tmp_path, links, fetcher, options=NovelOptions(rate=1000.0, max_pages=2)
    )
    cached = _cached_chapter(tmp_path / "work", snapshot)
    assert cached.pages == 2
    assert cached.truncated is True
    assert captured.pages_fetched == 2
    assert any("--max-pages 2" in warning for warning in captured.warnings)
    assert fetcher.requests == [SOURCE + "1.html", SOURCE + "1_2.html"]


def test_default_cap_merges_thirty_page_chapter(tmp_path: Path) -> None:
    """默认上限 50：超过旧默认 20 页的长章不再被截断，完整合并。"""
    links = (ChapterLink("第一章 长章", SOURCE + "1.html"),)
    pages = {}
    for number in range(1, 31):
        following = f"<a href='{SOURCE}1_{number + 1}.html'>下一页</a>" if number < 30 else ""
        pages[SOURCE + ("1.html" if number == 1 else f"1_{number}.html")] = chapter_html(
            "第一章 长章", PROSE + f"<p>这是第{number}页独有的句子。</p>", tail=following
        )
    fetcher = StubFetcher(pages)
    captured, snapshot = run_capture(tmp_path, links, fetcher)
    assert snapshot.resources[0].status == "done"
    cached = _cached_chapter(tmp_path / "work", snapshot)
    assert cached.pages == 30 and cached.truncated is False
    assert "这是第30页独有的句子。" in "".join(cached.paragraphs)
    assert not any("--max-pages" in warning for warning in captured.warnings)
    assert len(fetcher.requests) == 30


@pytest.mark.parametrize(
    ("stage", "target"),
    [
        ("fetch", "http://other.test/book/1.html"),
        ("fetch", "https://example.test/book/1.html"),
        ("continuation", "http://example.test:8080/book/1_2.html"),
        ("render", "http://other.test/book/1.html"),
    ],
)
def test_cross_origin_redirect_is_not_cached_or_followed(
    tmp_path: Path,
    stage: str,
    target: str,
) -> None:
    links = (ChapterLink("第一章", SOURCE + "1.html"),)
    redirected = Response(
        url=target,
        status=200,
        headers={},
        elapsed_ms=1,
        content=chapter_html("第一章", tail="<a href='1_3.html'>下一页</a>").encode(),
    )
    fetcher = StubFetcher({links[0].url: redirected})
    expected_requests = [links[0].url]
    if stage == "continuation":
        fetcher.pages[links[0].url] = chapter_html("第一章", tail="<a href='1_2.html'>下一页</a>")
        fetcher.pages[SOURCE + "1_2.html"] = redirected
        expected_requests.append(SOURCE + "1_2.html")
    elif stage == "render":
        fetcher.pages[links[0].url] = chapter_html("第一章")

    async def renderer(page: Response) -> tuple[Response, tuple[str, ...]]:
        return redirected, ()

    captured, snapshot = run_capture(
        tmp_path,
        links,
        fetcher,
        render=renderer if stage == "render" else None,
    )
    assert fetcher.requests == expected_requests
    assert snapshot.resources[0].status == "failed"
    assert snapshot.resources[0].error_code == "invalid_text"
    assert snapshot.resources[0].local_path is None
    assert any("跳转到其他站点" in warning for warning in captured.warnings)


def test_next_chapter_rel_link_does_not_duplicate_the_book(tmp_path: Path) -> None:
    """实测回归：每章末尾带 rel=next 的"下一章"链接，不能把全书塞进第一章。"""
    links = tuple(ChapterLink(f"第{index}章", SOURCE + f"{index}.html") for index in range(1, 4))
    pages = {}
    for index in range(1, 4):
        body = f"<p>第{index}章独有的正文标记，用来确认没有串章。</p>" + PROSE
        tail = f"<a rel='next' href='{SOURCE}{index + 1}.html'>Next</a>" if index < 3 else ""
        pages[SOURCE + f"{index}.html"] = chapter_html(f"第{index}章", body, tail=tail)
    _, snapshot = run_capture(tmp_path, links, StubFetcher(pages))
    assert [record.status for record in snapshot.resources] == ["done"] * 3
    for index in range(1, 4):
        cached = _cached_chapter(tmp_path / "work", snapshot, index - 1)
        assert cached.pages == 1, f"第{index}章被当成分页抓了 {cached.pages} 页"
        text = "".join(cached.paragraphs)
        assert f"第{index}章独有的正文标记" in text
        for other in range(1, 4):
            if other != index:
                assert f"第{other}章独有的正文标记" not in text
