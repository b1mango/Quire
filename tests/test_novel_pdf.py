"""M4 real Chrome contract, typography, offline boundary and bounded cleanup."""

from __future__ import annotations

import asyncio
import os
import re
import unicodedata
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any, cast
from xml.etree import ElementTree as ET

import pytest
from pypdf import PdfReader
from pypdf.generic import ArrayObject, DictionaryObject

from quire.assemble import novel_html
from quire.assemble.models import NovelChapter
from quire.assemble.novel_html import render_html
from quire.errors import ConfigError, FetchError, UnsupportedError
from quire.fetch import browser_pdf
from quire.fetch.browser_cdp import Cdp
from quire.fetch.browser_pdf import print_pdf
from quire.fetch.browser_process import ChromeProcess, find_chrome

_INJECTION = '<script>alert("中文注入")</script> & <img src="https://example.test/a">'
_LONG = "他抬头看见远山如黛，风从林间穿过，带来潮湿的泥土气息。" * 90


def sample_chapters() -> tuple[NovelChapter, ...]:
    return (
        NovelChapter(1, "第一章 起点", ("中文正文，标点完整。", _INJECTION, _LONG)),
        NovelChapter(2, "第二章 缺失", (), missing_reason="HTTP 404：章节不可用"),
        NovelChapter(3, "第三章 尾声", ("最后一段：千里之行，始于足下。",)),
    )


@pytest.fixture
def chrome() -> str:
    executable = find_chrome()
    if executable is None:
        pytest.skip("System Chrome required for novel PDF tests")
    return executable


@pytest.fixture
def owned(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[ChromeProcess]]:
    instances: list[ChromeProcess] = []

    def factory(executable: str) -> ChromeProcess:
        process = ChromeProcess(executable)
        instances.append(process)
        return process

    monkeypatch.setattr(browser_pdf, "ChromeProcess", factory)
    yield instances
    for process in instances:
        assert process.profile is not None and not process.profile.exists()
        if process.pid is not None:
            with pytest.raises(ProcessLookupError):
                os.kill(process.pid, 0)


def test_html_cleans_controls_escapes_and_keeps_stable_anchors() -> None:
    dirty = '<>&"\x00\x01\x7f\x85\ud800\ufffe\uffff中文'
    chapters = (
        NovelChapter(7, dirty, (dirty,), truncated=True),
        NovelChapter(7, "\x00", (), missing_reason=dirty),
    )
    html = render_html(title=dirty, chapters=chapters)
    root = ET.fromstring(
        html.replace('<meta charset="utf-8">', '<meta charset="utf-8"/>').replace(
            "'none'\">", "'none'\"/>"
        )
    )
    assert root.findtext("head/title") == '<>&"中文'
    assert root.findtext("body/main/section/p") == '<>&"中文'
    assert root.findall(".//script") == []
    assert [a.attrib["href"] for a in root.findall(".//nav/ol/li/a")] == [
        "#chapter-1",
        "#chapter-2",
    ]
    assert "第 7 章" in html and "本章抓取失败" in html and "已截断" in html
    assert "size: A5; margin: 18mm" in html
    assert render_html(title=dirty, chapters=chapters) == html


def test_html_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigError, match="至少"):
        render_html(title="empty", chapters=[])
    monkeypatch.setattr(novel_html, "MAX_HTML_BYTES", 2000)
    with pytest.raises(ConfigError, match="32 MiB"):
        render_html(title="中文" * 2000, chapters=sample_chapters())
    with pytest.raises(ConfigError, match="32 MiB"):
        render_html(title="测试", chapters=sample_chapters())


def assert_sample_pdf(path: Path) -> None:
    def normalized(text: str) -> str:
        # Songti's ToUnicode table uses compatibility radicals for some shared glyphs.
        return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))

    with closing(PdfReader(path)) as pdf:
        texts = [page.extract_text() for page in pdf.pages]
        body = [re.sub(r"\n\d+\s*/\s*\d+\s*$", "", text) for text in texts]
        combined = normalized("".join(body))
        assert "卷帙中文排版" in combined
        assert normalized(_INJECTION) in combined
        assert normalized(_LONG) in combined
        assert normalized("本章抓取失败：HTTP404：章节不可用") in combined
        assert normalized("最后一段：千里之行，始于足下。") in combined
        assert len(pdf.pages) >= 6  # TOC, long chapter, missing chapter and final chapter.
        for index, page in enumerate(pdf.pages, 1):
            assert float(page.mediabox.width) == pytest.approx(148 / 25.4 * 72, abs=1)
            assert float(page.mediabox.height) == pytest.approx(210 / 25.4 * 72, abs=1)
            assert f"{index}/{len(pdf.pages)}" in re.sub(r"\s+", "", texts[index - 1])
        links = [
            cast(DictionaryObject, ref.get_object())
            for ref in cast(ArrayObject, pdf.pages[0]["/Annots"])
        ]
        destinations = [
            pdf.named_destinations[str(link["/Dest"])]
            for link in links
            if str(link["/Subtype"]) == "/Link"
        ]
        assert len(destinations) == 3
        page_indices = [pdf.get_destination_page_number(dest) for dest in destinations]
        assert len(set(page_indices)) == 3
        for page_index, chapter in zip(page_indices, sample_chapters(), strict=True):
            assert page_index is not None
            target = pdf.pages[page_index]
            assert normalized(target.extract_text()).startswith(normalized(chapter.title))
        assert all("/A" not in link for link in links)


def test_real_chinese_pdf(chrome: str, owned: list[ChromeProcess], tmp_path: Path) -> None:
    path = tmp_path / "novel.pdf"
    count = asyncio.run(
        print_pdf(
            render_html(title="卷帙中文排版", chapters=sample_chapters()), path, executable=chrome
        )
    )
    assert count == path.stat().st_size
    assert_sample_pdf(path)
    assert len(owned) == 1


def test_existing_file_and_symlink_are_preserved(
    chrome: str, owned: list[ChromeProcess], tmp_path: Path
) -> None:
    path, alias = tmp_path / "existing.pdf", tmp_path / "alias.pdf"
    path.write_bytes(b"original")
    alias.symlink_to(path)
    for destination in (path, alias):
        with pytest.raises(FileExistsError):
            asyncio.run(print_pdf("<p>test</p>", destination, executable=chrome))
    assert path.read_bytes() == b"original" and alias.is_symlink() and not owned


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), 61])
def test_invalid_deadline(timeout: float, tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        asyncio.run(print_pdf("", tmp_path / "bad.pdf", executable="", timeout=timeout))
    assert list(tmp_path.iterdir()) == []


def test_missing_chrome_and_html_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "bad.pdf"
    with pytest.raises(UnsupportedError, match="小说转 PDF 需要 Chrome"):
        asyncio.run(print_pdf("test", path, executable=str(tmp_path / "missing-chrome")))
    monkeypatch.setattr(browser_pdf, "MAX_HTML_BYTES", 10)
    for html in ("x" * 11, "汉" * 4, "\ud800"):
        with pytest.raises(ConfigError):
            asyncio.run(print_pdf(html, path, executable=""))
    assert not path.exists()


def test_real_scripts_and_network_disabled(
    chrome: str, owned: list[ChromeProcess], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Cdp.call

    async def call(
        self: Cdp,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if method == "Page.printToPDF":
            state = await original(
                self,
                "Runtime.evaluate",
                {
                    "expression": "window.userScriptRan === true",
                    "returnByValue": True,
                },
                session_id=session_id,
            )
            assert state["result"]["value"] is False
            assert (await original(self, "Storage.getCookies"))["cookies"] == []
        return await original(self, method, params, session_id=session_id)

    monkeypatch.setattr(Cdp, "call", call)

    async def run() -> None:
        hits: list[bytes] = []

        async def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            hits.append(await reader.read(4096))
            writer.close()
            await writer.wait_closed()

        async with await asyncio.start_server(accept, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            url = f"http://127.0.0.1:{port}/forbidden"
            html = (
                f'<link rel="stylesheet" href="{url}"><style>@import url("{url}");'
                f'@font-face{{font-family:remote;src:url("{url}")}}p{{font-family:remote}}'
                "</style><p>安全正文</p><script>window.userScriptRan=true;"
                'document.body.innerHTML="脚本执行";</script>'
                f'<script src="{url}"></script><img src="{url}">'
                f'<iframe src="{url}"></iframe><img src=x onerror="window.userScriptRan=true">'
            )
            await print_pdf(html, tmp_path / "offline.pdf", executable=chrome)
            assert not hits

    asyncio.run(run())
    with closing(PdfReader(tmp_path / "offline.pdf")) as pdf:
        text = unicodedata.normalize("NFKC", "".join(page.extract_text() for page in pdf.pages))
        assert "安全正文" in text and "脚本执行" not in text


@pytest.mark.parametrize("failure", ["read", "oversize", "invalid", "fonts", "print"])
def test_real_failure_cleans_candidate(
    failure: str,
    chrome: str,
    owned: list[ChromeProcess],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original, closed = Cdp.call, []
    if failure == "oversize":
        monkeypatch.setattr(browser_pdf, "MAX_PDF_BYTES", 20)

    async def call(
        self: Cdp,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if (failure, method) in {("read", "IO.read"), ("print", "Page.printToPDF")}:
            raise FetchError("injected failure")
        if failure == "fonts" and method == "Runtime.evaluate":
            return {"result": {"value": False}}
        if failure == "invalid" and method == "IO.read":
            return {"data": "%PDF-incomplete", "eof": True}
        if method == "IO.close":
            closed.append(method)
        return await original(self, method, params, session_id=session_id)

    monkeypatch.setattr(Cdp, "call", call)
    with pytest.raises(FetchError):
        asyncio.run(print_pdf("<p>故障测试</p>", tmp_path / "failure.pdf", executable=chrome))
    assert not (tmp_path / "failure.pdf").exists()
    assert len(closed) == (0 if failure in {"fonts", "print"} else 1)


@pytest.mark.parametrize("mode", ["timeout", "cancel", "cancel-close-failure"])
def test_real_deadline_and_repeated_cancel_reclaim_stream(
    mode: str,
    chrome: str,
    owned: list[ChromeProcess],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original, closed = Cdp.call, []

    async def run() -> None:
        reading, closing_stream = asyncio.Event(), asyncio.Event()

        async def call(
            self: Cdp,
            method: str,
            params: dict[str, Any] | None = None,
            *,
            session_id: str | None = None,
        ) -> dict[str, Any]:
            if method == "IO.read":
                reading.set()
                await asyncio.Future[None]()
            if method == "IO.close":
                closing_stream.set()
                await asyncio.sleep(0.05)
                closed.append(method)
                if mode == "cancel-close-failure":
                    raise FetchError("injected disconnect during cleanup")
            return await original(self, method, params, session_id=session_id)

        monkeypatch.setattr(Cdp, "call", call)
        task = asyncio.create_task(
            print_pdf(
                "<p>取消测试</p>",
                tmp_path / "cancel.pdf",
                executable=chrome,
                timeout=3 if mode == "timeout" else 60,
            )
        )
        async with asyncio.timeout(10):
            await reading.wait()
            if mode.startswith("cancel"):
                task.cancel()
                await closing_stream.wait()
                if mode == "cancel":
                    task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(FetchError, match="超时"):
                    await task
        assert not [task for task in asyncio.all_tasks() if task.get_name().startswith("quire-")]

    asyncio.run(run())
    assert closed == ["IO.close"] and not (tmp_path / "cancel.pdf").exists()


def test_real_disk_failure_removes_candidate(
    chrome: str, owned: list[ChromeProcess], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = browser_pdf._print

    async def fail_write(cdp: Cdp, session: str, output: Any) -> int:
        def fail(data: bytes) -> int:
            raise OSError("injected disk full")

        monkeypatch.setattr(output, "write", fail)
        return await original(cdp, session, output)

    monkeypatch.setattr(browser_pdf, "_print", fail_write)
    with pytest.raises(OSError, match="disk full"):
        asyncio.run(print_pdf("<p>磁盘测试</p>", tmp_path / "disk.pdf", executable=chrome))
    assert not (tmp_path / "disk.pdf").exists()


def test_deadline_covers_chrome_startup(
    chrome: str, owned: list[ChromeProcess], tmp_path: Path
) -> None:
    with pytest.raises(FetchError, match="超时"):
        asyncio.run(
            print_pdf("<p>启动测试</p>", tmp_path / "startup.pdf", executable=chrome, timeout=0.001)
        )
    assert not (tmp_path / "startup.pdf").exists() and owned
