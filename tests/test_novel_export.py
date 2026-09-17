from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from quire import export_commit, novel_export
from quire.assemble.epub import EpubWriter
from quire.assemble.models import NovelChapter
from quire.assemble.txt import write_txt
from quire.errors import FetchError, LedgerError
from quire.models import NovelResult
from quire.novel_export import export_novel, preflight

CHAPTERS = (NovelChapter(1, "第一章", ("一段完整的正文。",)),)


def export(tmp_path: Path, **kwargs):
    output = tmp_path / "book.txt"
    result = NovelResult(output, title="书名", task_id="a" * 64, chapters_written=1)
    destinations = (output, output.with_suffix(".epub"))
    before = preflight((*destinations, output.with_suffix(".report.json")), overwrite=False)
    return export_novel(
        result,
        CHAPTERS,
        destinations,
        ("txt", "epub"),
        before,
        source_url="https://example.test/book",
        **kwargs,
    )


@pytest.mark.parametrize("kind", ["txt", "epub"])
def test_writers_refuse_existing_files_and_symlinks(kind, tmp_path):
    original = tmp_path / "original"
    original.write_bytes(b"user data")
    link = tmp_path / "alias"
    link.symlink_to(original)
    for path in (original, link):
        with pytest.raises(FileExistsError):
            if kind == "txt":
                write_txt(path, title="title", chapters=CHAPTERS)
            else:
                with EpubWriter(path, title="title"):
                    pass
    assert original.read_bytes() == b"user data" and link.is_symlink()


def test_partial_publication_lists_committed_file_and_can_rebuild(tmp_path, monkeypatch):
    original = export_commit.os.link

    def fail_second(source, destination, **kwargs):
        if Path(destination).suffix == ".epub":
            raise OSError("injected disk failure")
        return original(source, destination, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(export_commit.os, "link", fail_second)
        with pytest.raises(FetchError, match="已发布：book.txt"):
            asyncio.run(export(tmp_path))
    assert (tmp_path / "book.txt").is_file()
    assert not (tmp_path / "book.epub").exists()
    assert not list(tmp_path.glob(".quire-export-*"))


def test_destination_changed_during_build_is_preserved(tmp_path, monkeypatch):
    original = novel_export.write_txt

    def write_and_race(path, **kwargs):
        size = original(path, **kwargs)
        (tmp_path / "book.txt").write_bytes(b"concurrent edit")
        return size

    monkeypatch.setattr(novel_export, "write_txt", write_and_race)
    with pytest.raises(FetchError, match="已发布：无"):
        asyncio.run(export(tmp_path))
    assert (tmp_path / "book.txt").read_bytes() == b"concurrent edit"
    assert not (tmp_path / "book.epub").exists()


def test_cancel_removes_candidates_without_publishing(tmp_path, monkeypatch):
    monkeypatch.setattr(novel_export, "publish", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(export(tmp_path))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("during_failure", [False, True])
def test_cleanup_failure_reports_published_outputs_or_preserves_original_error(
    tmp_path,
    monkeypatch,
    during_failure,
):
    def fail_cleanup(*args):
        raise LedgerError("injected unsafe directory")

    monkeypatch.setattr(novel_export, "clean_workspace", fail_cleanup)
    if during_failure:
        monkeypatch.setattr(novel_export, "publish", AsyncMock(side_effect=asyncio.CancelledError))
        with pytest.raises(asyncio.CancelledError) as caught:
            asyncio.run(export(tmp_path))
        assert "未能安全清理" in caught.value.__notes__[0]
    else:
        with pytest.raises(FetchError, match="已发布：book.txt, book.epub, book.report.json"):
            asyncio.run(export(tmp_path))
    assert len(list(tmp_path.glob(".quire-export-*"))) == 1


def test_unreadable_destination_after_failure_is_reported_unknown(tmp_path, monkeypatch):
    async def fail_publication(receipt):
        receipt.files[0].destination.symlink_to(tmp_path / "missing")
        raise LedgerError("injected output replacement")

    monkeypatch.setattr(novel_export, "publish", fail_publication)
    with pytest.raises(FetchError, match="book.txt（状态无法确认）"):
        asyncio.run(export(tmp_path))
    assert (tmp_path / "book.txt").is_symlink()
