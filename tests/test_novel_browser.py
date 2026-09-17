from __future__ import annotations

import asyncio
import json
from contextlib import closing
from dataclasses import replace
from unittest.mock import AsyncMock
from zipfile import ZipFile

import pytest
from pypdf import PdfReader

from quire.cli import main
from quire.core_novel import run_core_novel
from quire.errors import UnsupportedError
from quire.fetch.browser import RenderOptions
from quire.fetch.browser_process import find_chrome
from quire.models import NovelOptions
from tests.mock_site.dynamic_server import serve
from tests.mock_site.novel_server import novel_site


@pytest.fixture
def chrome():
    executable = find_chrome()
    if executable is None:
        pytest.skip("System Chrome is required")
    return executable


def test_dynamic_text_without_images_is_collected(chrome, tmp_path):
    out = tmp_path / "dynamic.txt"
    with serve() as site:
        result = asyncio.run(
            run_core_novel(
                site.url + "/novel",
                out,
                formats=("txt",),
                options=NovelOptions(rate=100, retries=0),
                render=RenderOptions(executable=chrome, timeout=5, settle=0.4),
            )
        )
        assert site.counts["/novel"] == 1
    assert not result.partial and result.chapters_written == 1
    assert "第6段" in out.read_text()


def test_three_formats_share_chapters_and_pdf_can_reuse_cache(chrome, tmp_path):
    out = tmp_path / "novel.pdf"
    with novel_site() as site:
        args = [
            "novel",
            site.url,
            "-o",
            str(out),
            "--format",
            "pdf,epub,txt",
            "--chrome",
            chrome,
            "--rate",
            "100",
            "--retries",
            "0",
            "-q",
        ]
        assert main(args) == 4
        before = site.hits("/book/1.html")
        second = asyncio.run(
            run_core_novel(
                site.url,
                tmp_path / "again.pdf",
                formats=("pdf",),
                pdf_chrome=chrome,
                options=NovelOptions(rate=100, retries=0),
                resume=True,
            )
        )
        assert second.resources_reused == 4
        assert site.hits("/book/1.html") == before
    with closing(PdfReader(out)) as pdf:
        assert len(pdf.pages) >= 6
        assert pdf.pages[0].get("/Annots")
    with ZipFile(out.with_suffix(".epub")) as epub:
        assert epub.testzip() is None
    assert "第2章第3页第6段" in out.with_suffix(".txt").read_text()
    report = json.loads(out.with_suffix(".report.json").read_text())
    assert [item["format"] for item in report["artifacts"]] == ["pdf", "epub", "txt"]


def test_missing_pdf_browser_fails_before_fetch_or_output(monkeypatch, tmp_path):
    monkeypatch.setattr("quire.core_novel.find_chrome", lambda _: None)
    fetcher = AsyncMock()
    out = tmp_path / "new" / "book.pdf"
    with pytest.raises(UnsupportedError):
        asyncio.run(run_core_novel("https://unused.test", out, formats=("pdf",), fetcher=fetcher))
    assert main(["novel", "https://unused.test", "-o", str(out), "--format", "pdf"]) == 6
    assert not out.parent.exists()
    fetcher.get.assert_not_called()


def test_incomplete_core_install_reports_missing_dependency(monkeypatch, tmp_path):
    monkeypatch.setattr("quire.cli_novel.module_available", lambda name: name != "websockets")
    assert main(["novel", "https://unused.test", "-o", str(tmp_path / "book.epub")]) == 6
    assert list(tmp_path.iterdir()) == []


def test_pdf_failure_preserves_old_outputs_and_chapter_cache(chrome, monkeypatch, tmp_path):
    from quire.errors import FetchError

    out = tmp_path / "book.epub"
    opts = NovelOptions(rate=100, retries=0)
    with novel_site() as site:
        asyncio.run(run_core_novel(site.url, out, options=opts))
        before, hits = out.read_bytes(), site.hits("/book/1.html")
        failure = AsyncMock(side_effect=FetchError("injected print failure"))
        monkeypatch.setattr("quire.novel_export.print_pdf", failure)
        with pytest.raises(FetchError, match="已发布：无"):
            asyncio.run(
                run_core_novel(
                    site.url,
                    out,
                    options=replace(opts, overwrite=True),
                    formats=("epub", "pdf"),
                    pdf_chrome=chrome,
                )
            )
        assert site.hits("/book/1.html") == hits
    assert out.read_bytes() == before
    assert not out.with_suffix(".pdf").exists()
    assert not list(tmp_path.glob(".quire-export-*"))
