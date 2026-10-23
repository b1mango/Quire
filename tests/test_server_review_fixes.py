"""Regression coverage for input safety, configured probing and bounded queue state."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from quire.server import endpoints
from quire.server import probe as probing
from quire.server.execution import MAX_FINISHED_JOBS
from quire.server.job_state import Job, JobSpec
from quire.server.jobs import JobManager
from quire.server.settings import UiSettings, save
from tests.test_desktop_capture import CATALOGUE, response


@pytest.mark.parametrize(
    "fields", [{"browser_native": True}, {"cdp_endpoint": "http://127.0.0.1:9222"}]
)
def test_probe_uses_configured_browser_without_first_http_fetch(tmp_path, monkeypatch, fields):
    settings = UiSettings(str(tmp_path / "out"), **fields)
    ctx = SimpleNamespace(settings_path=tmp_path / "settings.json", data_root=tmp_path)
    save(ctx.settings_path, settings)
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get.side_effect = AssertionError("configured browser must bypass plain HTTP")
    monkeypatch.setattr(probing, "AsyncFetcher", lambda **kwargs: client)
    rendered = response(CATALOGUE)
    renderer = AsyncMock(return_value=(rendered, ()))
    expanded = AsyncMock(side_effect=lambda client, page: page)
    monkeypatch.setattr(probing, "render_page", renderer)
    monkeypatch.setattr(probing, "expand_catalogue", expanded)
    monkeypatch.setattr(endpoints, "find_chrome", lambda: None)
    monkeypatch.setattr(endpoints, "_library_hint", lambda *args: None)
    result = endpoints.probe(
        ctx, {"url": "https://example.test/book", "kind": "novel", "capture_mode": "catalogue"}
    )
    assert result["render"] and result["count"] == 3
    client.get.assert_not_awaited()
    options = renderer.await_args.args[2]
    assert options == settings.render_options()
    assert expanded.await_args.args[1] is rendered


def test_unconfigured_static_probe_stays_on_http(monkeypatch):
    client = AsyncMock()
    client.__aenter__.return_value = client
    static_catalogue = CATALOGUE.replace("/#/book/10/", "/chapters/").replace(
        "#/book/10/", "/chapters/"
    )
    client.get.return_value = response(static_catalogue, "https://example.test/book")
    monkeypatch.setattr(probing, "AsyncFetcher", lambda **kwargs: client)
    renderer = AsyncMock(side_effect=AssertionError("no browser needed"))
    monkeypatch.setattr(probing, "render_page", renderer)
    result = asyncio.run(
        probing.probe_url("https://example.test/book", kind="novel", capture_mode="catalogue")
    )
    assert result.count == 3 and not result.render
    client.get.assert_awaited_once()
    renderer.assert_not_awaited()


def make_job(index, status="done"):
    job = Job(str(index), JobSpec("novel", f"https://example.test/{index}", "合成小说", ("txt",)))
    job.created_at = f"{index:08}"
    job.status = status
    return job


def test_queue_summary_omits_chapters_but_detail_keeps_them(tmp_path):
    manager = JobManager(tmp_path)
    job = make_job(0)
    for number in range(2000):
        job.record_chapter({"id": str(number), "title": "合成章节", "status": "done"})
    manager._jobs[job.id] = job
    summary = endpoints.list_jobs(SimpleNamespace(manager=manager))
    assert "chapters" not in summary["jobs"][0]
    assert len(json.dumps(summary, ensure_ascii=False).encode()) < 1500
    assert len(manager.get(job.id).snapshot()["chapters"]) == 2000


def test_history_pruning_preserves_pending_running_and_owned_jobs(tmp_path):
    manager = JobManager(tmp_path)
    for index in range(MAX_FINISHED_JOBS + 5):
        job = make_job(index, "paused" if index % 2 else "done")
        manager._jobs[job.id] = job
    pending, running, finalizing = make_job(100, "pending"), make_job(101, "running"), make_job(-1)
    for job in (pending, running, finalizing):
        manager._jobs[job.id] = job
    manager._active[finalizing.id] = "owned-ledger"
    kept = manager.jobs()
    assert len(kept) == MAX_FINISHED_JOBS + 3
    assert all(manager.get(job.id) is job for job in (pending, running, finalizing))
    assert manager.get("0") is None
    assert manager.get(str(MAX_FINISHED_JOBS + 4)) is not None
    manager._release_slot(finalizing)
    assert manager.get(finalizing.id) is None
    assert len(manager.jobs()) == MAX_FINISHED_JOBS + 2
    assert list(tmp_path.iterdir()) == []  # No file/ledger/cache deletion or writes.


@pytest.mark.parametrize("native", [True, False])
def test_manga_capture_uses_browser_without_initial_http(monkeypatch, native):
    from quire import core_discovery
    from quire.fetch.browser import RenderOptions
    from quire.models import MangaOptions

    options = RenderOptions(native=native, cdp_endpoint="" if native else "http://127.0.0.1:9222")
    client = AsyncMock()
    client.get.side_effect = AssertionError("must not fetch plain HTTP before Chrome")
    renderer = AsyncMock(
        return_value=(
            response(
                '<h1>Comic</h1><img src="https://example.test/001.jpg" width="800" height="1200">',
                "https://example.test/chapter/1",
            ),
            (),
        )
    )
    monkeypatch.setattr(core_discovery, "render_page", renderer)
    found, _ = asyncio.run(
        core_discovery.discover_manga(
            client, "https://example.test/chapter/1", MangaOptions(), options
        )
    )
    assert len(found) == 1
    client.get.assert_not_awaited()
    assert renderer.await_args.args[2] is options


def test_static_manga_capture_keeps_referer(monkeypatch):
    from quire.core_discovery import discover_manga
    from quire.models import MangaOptions

    client = AsyncMock()
    client.get.return_value = response('<img src="/001.jpg" width="800" height="1200">')
    asyncio.run(
        discover_manga(
            client,
            "https://example.test/chapter/1",
            MangaOptions(referer="https://example.test/catalogue"),
        )
    )
    client.get.assert_awaited_once_with(
        "https://example.test/chapter/1", referer="https://example.test/catalogue"
    )


def test_series_catalogue_reaches_browser_before_http(tmp_path, monkeypatch):
    from quire import core_series
    from quire.errors import FetchError
    from quire.fetch.browser import RenderOptions

    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get.side_effect = AssertionError("must not fetch plain HTTP before Chrome")
    renderer = AsyncMock(return_value=(response(CATALOGUE), ()))
    monkeypatch.setattr(core_series, "render_page", renderer)
    monkeypatch.setattr(core_series, "plan_volumes", lambda *args, **kwargs: ((), ()))
    options = RenderOptions(native=True)
    with pytest.raises(FetchError, match="没有生成任何卷"):
        asyncio.run(
            core_series.run_series(
                "https://example.test/catalogue", tmp_path, fetcher=client, render=options
            )
        )
    client.get.assert_not_awaited()
    assert renderer.await_args.args[2] is options


def test_bilibili_cdp_uses_owned_tab_without_local_chrome(monkeypatch):
    from quire.fetch.browser import RenderOptions
    from quire.sites import bilibili

    calls = []
    client = AsyncMock()
    client.__aenter__.return_value = client

    async def call(method, params=None, **kwargs):
        calls.append(method)
        return {
            "Target.createTarget": {"targetId": "owned"},
            "Target.attachToTarget": {"sessionId": "session"},
            "Target.closeTarget": {"success": True},
        }.get(method, {})

    client.call.side_effect = call
    endpoint = AsyncMock(return_value="ws://127.0.0.1:9222/devtools/browser/test")
    monkeypatch.setattr(bilibili, "resolve_endpoint", endpoint)
    monkeypatch.setattr(bilibili, "Cdp", lambda url: client)
    monkeypatch.setattr(bilibili, "find_chrome", lambda *args: None)
    monkeypatch.setattr(
        bilibili, "_await_ready", AsyncMock(return_value={"title": "comic", "text": "1P"})
    )

    async def images(tap):
        tap.urls.append("https://a.hdslb.com/bfs/manga/a.jpg?token=t")

    monkeypatch.setattr(bilibili, "_await_first_images", images)
    page, total = asyncio.run(
        bilibili.capture_reader_page(
            "https://manga.bilibili.com/mc1/2",
            RenderOptions(cdp_endpoint="http://127.0.0.1:9222"),
            full=False,
        )
    )
    assert total == 1 and "a.jpg" in page.text
    endpoint.assert_awaited_once_with("http://127.0.0.1:9222")
    assert "Target.closeTarget" in calls
    assert "Browser.setDownloadBehavior" not in calls
