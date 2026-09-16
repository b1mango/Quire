from __future__ import annotations

import asyncio
import json
import os
from contextlib import closing
from unittest.mock import AsyncMock
from zipfile import ZipFile

import pytest
from pypdf import PdfReader

from quire.cli import main
from quire.core_manga import run_core_manga
from quire.errors import ConfigError, FetchError, NoImagesError, UnsupportedError
from quire.fetch import browser
from quire.fetch.browser import RenderOptions, render_page
from quire.fetch.browser_process import ChromeProcess, find_chrome
from quire.fetch.session import AsyncFetcher
from quire.fetch.simple import Response
from quire.models import MangaOptions
from tests.mock_site.dynamic_server import serve


@pytest.fixture
def site():
    with serve() as server:
        yield server


@pytest.fixture
def chrome():
    executable = find_chrome()
    if executable is None:
        pytest.skip("System Chrome is required for the real render test")
    return executable


@pytest.fixture
def owned(monkeypatch):
    instances = []

    def factory(executable):
        process = ChromeProcess(executable)
        instances.append(process)
        return process

    monkeypatch.setattr(browser, "ChromeProcess", factory)
    yield instances
    for process in instances:
        assert process.profile is not None and not process.profile.exists()
        if process.pid is not None:
            with pytest.raises(ProcessLookupError):
                os.kill(process.pid, 0)


def options():
    return MangaOptions(selector="main.reader", order="dom", rate=100, retries=0)


def test_real_dynamic_three_formats_and_cli(site, chrome, owned, tmp_path):
    out = tmp_path / "dynamic.pdf"
    args = [
        "manga",
        site.url + "/redirect",
        "--core",
        "--render",
        "--chrome",
        chrome,
        "--format",
        "pdf,cbz,zip",
        "--selector",
        "main.reader",
        "--order",
        "dom",
        "--rate",
        "100",
        "--retries",
        "0",
        "-o",
        str(out),
        "-q",
    ]
    assert main(args) == 0
    with closing(PdfReader(out)) as pdf, ZipFile(out.with_suffix(".zip")) as archive:
        assert len(pdf.pages) == 3
        manifest = json.loads(archive.read("manifest.json"))
        assert [entry["width"] for entry in manifest["pages"]] == [401, 402, 403]
        assert all(entry["missing_reason"] is None for entry in manifest["pages"])
    with ZipFile(out.with_suffix(".cbz")) as cbz:
        assert cbz.testzip() is None
    assert site.counts["/reader.js"] == 1 and site.counts["/dynamic"] == 1
    assert len(owned) == 1


def test_static_dynamic_page_does_not_start_browser(site, owned, tmp_path):
    with pytest.raises(NoImagesError):
        asyncio.run(
            run_core_manga(site.url + "/dynamic", tmp_path / "static.pdf", options=options())
        )
    assert not owned and site.counts["/reader.js"] == 0


@pytest.mark.parametrize(
    "path,settings,match",
    [
        ("/endless", {"max_scrolls": 2}, "滚动上限"),
        ("/delayed", {"timeout": 3}, "超时"),
        ("/blocked-script", {"timeout": 3}, "robots"),
    ],
)
def test_real_render_limits_fail_without_publishing(
    site, chrome, owned, tmp_path, path, settings, match
):
    out = tmp_path / "failed.pdf"
    with pytest.raises(FetchError, match=match):
        asyncio.run(
            run_core_manga(
                site.url + path,
                out,
                options=options(),
                render=RenderOptions(executable=chrome, **settings),
            )
        )
    assert not out.exists()
    assert not site.counts["/forbidden/reader.js"]


def test_real_cancel_cleans_process_and_keeps_previous_output(site, chrome, owned, tmp_path):
    async def run():
        task = asyncio.create_task(
            run_core_manga(
                site.url + "/delayed",
                tmp_path / "cancel.pdf",
                options=options(),
                render=RenderOptions(executable=chrome),
            )
        )
        async with asyncio.timeout(10):
            while not site.counts["/reader.js"]:
                await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert not (tmp_path / "cancel.pdf").exists()


def test_real_browser_does_not_forward_cookies(site, chrome, owned):
    async def run():
        async with AsyncFetcher(rate=100, retries=0) as client:
            page = await client.get(site.url + "/cookie")
            rendered, _ = await render_page(page, client, RenderOptions(executable=chrome))
        assert 'data-cookie-received="false"' in rendered.text
        assert site.counts["/needs-cookie"] == 1

    asyncio.run(run())


@pytest.mark.parametrize("case", ["navigation", "snapshot", "size"])
def test_render_invalid_navigation_and_snapshot_leave_no_result(case, monkeypatch):
    process = AsyncMock()
    process.__aenter__.return_value = process
    cdp = AsyncMock()
    cdp.__aenter__.return_value = cdp

    async def call(method, *args, **kwargs):
        if method == "Target.createTarget":
            return {"targetId": "target"}
        if method == "Target.attachToTarget":
            return {"sessionId": "session"}
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "frame"}}}
        if method == "Page.navigate" and case == "navigation":
            return {"errorText": "failure"}
        if method == "Runtime.evaluate":
            value = (
                None if case == "snapshot" else {"url": "https://example.test/", "html": "x" * 20}
            )
            return {"result": {"value": value}}
        return {}

    cdp.call.side_effect = call
    monkeypatch.setattr(browser, "find_chrome", lambda _: "/fake")
    monkeypatch.setattr(browser, "ChromeProcess", lambda _: process)
    monkeypatch.setattr(browser, "Cdp", lambda _: cdp)
    monkeypatch.setattr(browser, "_scroll", AsyncMock())
    page = Response("https://example.test/", 200, {}, b"", 0)
    with pytest.raises(FetchError):
        asyncio.run(render_page(page, AsyncFetcher(max_bytes=10), RenderOptions()))
    assert process.__aexit__.await_count == 1


@pytest.mark.parametrize(
    "values",
    [
        {"timeout": 0},
        {"timeout": float("nan")},
        {"timeout": 301},
        {"max_scrolls": 0},
        {"max_scrolls": 1001},
        {"settle": 0},
        {"settle": 11},
    ],
)
def test_render_options_validate(values):
    with pytest.raises(ConfigError):
        RenderOptions(**values)


def test_browser_options_fail_before_network_or_output(tmp_path, monkeypatch, capsys):
    url = "https://unused.test/"
    out = tmp_path / "must-not-exist" / "book.pdf"
    for args in (
        ["--render"],
        ["--chrome", "/missing"],
        ["--render-timeout", "2"],
        ["--max-scrolls", "2"],
    ):
        assert main(["manga", url, "-o", str(out), *args]) == 1
    monkeypatch.setattr("quire.fetch.browser_process.find_chrome", lambda _: None)
    assert main(["manga", url, "--core", "--render", "-o", str(out)]) == 6
    assert not out.parent.exists()
    assert "Traceback" not in capsys.readouterr().err


def test_missing_browser_and_js_error_are_actionable(monkeypatch):
    monkeypatch.setattr(browser, "find_chrome", lambda _: None)
    page = Response("https://example.test/", 200, {}, b"", 0)
    with pytest.raises(UnsupportedError):
        asyncio.run(render_page(page, AsyncFetcher(), RenderOptions()))
    cdp = AsyncMock()
    cdp.call.return_value = {"exceptionDetails": {"text": "private page data"}}
    with pytest.raises(FetchError, match="脚本"):
        asyncio.run(browser._evaluate(cdp, "session", "document.title"))


def test_unusable_navigation_rejected():
    cdp = AsyncMock()
    cdp.call.return_value = {"result": {"value": {"url": "file:///private"}}}
    net = AsyncMock()
    net.check = lambda: None
    with pytest.raises(FetchError, match="导航"):
        asyncio.run(browser._scroll(cdp, "s", net, RenderOptions()))


def test_short_completed_request_restarts_stability_window(monkeypatch):
    async def run():
        now = asyncio.get_running_loop().time
        started = now()
        net = AsyncMock()
        net.check = lambda: None
        net.pending = set()
        net.last_activity = started
        cdp = AsyncMock()
        cdp.call.return_value = {
            "result": {
                "value": {
                    "url": "https://example.test/",
                    "ready": "complete",
                    "height": 600,
                    "viewport": 600,
                    "y": 0,
                    "images": "one.jpg",
                }
            }
        }

        async def activity():
            await asyncio.sleep(0.1)
            net.last_activity = now()

        activity_task = asyncio.create_task(activity())
        await browser._scroll(cdp, "s", net, RenderOptions(settle=0.2))
        await activity_task
        assert now() - net.last_activity >= 0.2

    asyncio.run(run())
