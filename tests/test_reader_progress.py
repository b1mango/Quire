from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from quire.errors import LedgerError
from quire.store import library, progress


def test_progress_save_replace_and_delete_book(tmp_path: Path) -> None:
    target = tmp_path / "book.txt"
    target.write_text("正文", encoding="utf-8")
    library.add_book(
        tmp_path,
        "b1",
        title="书",
        kind="novel",
        source_url="https://example.test",
        files=[("txt", target)],
    )
    assert progress.get(tmp_path, "b1") is None
    progress.save(tmp_path, "b1", format="txt", position="3:0.42", percent=0.27)
    saved = progress.get(tmp_path, "b1")
    assert saved is not None
    assert (saved.format, saved.position, saved.percent) == ("txt", "3:0.42", 0.27)
    assert saved.updated_at
    progress.save(tmp_path, "b1", format="pdf", position="5", percent=0.5)
    saved = progress.get(tmp_path, "b1")
    assert saved is not None
    assert (saved.format, saved.position, saved.percent) == ("pdf", "5", 0.5)
    library.delete_book(tmp_path, "b1")
    assert progress.get(tmp_path, "b1") is None


def test_progress_initializes_old_library_without_changing_version(tmp_path: Path) -> None:
    target = tmp_path / "book.txt"
    target.write_text("正文", encoding="utf-8")
    library.add_book(
        tmp_path,
        "b1",
        title="书",
        kind="novel",
        source_url="https://example.test",
        files=[("txt", target)],
    )
    with sqlite3.connect(tmp_path / "library.db") as connection:
        before = connection.execute("PRAGMA user_version").fetchone()[0]
    progress.save(tmp_path, "b1", format="txt", position="0", percent=0.0)
    with sqlite3.connect(tmp_path / "library.db") as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == before
    assert library.get_book(tmp_path, "b1").title == "书"


def test_progress_save_after_book_deletion_does_not_create_orphan(tmp_path: Path) -> None:
    target = tmp_path / "book.txt"
    target.write_text("正文", encoding="utf-8")
    library.add_book(
        tmp_path,
        "b1",
        title="书",
        kind="novel",
        source_url="https://example.test",
        files=[("txt", target)],
    )
    book = library.get_book(tmp_path, "b1")
    library.delete_book(tmp_path, book.id)
    with pytest.raises(LedgerError, match="未找到这本书"):
        progress.save(tmp_path, book.id, format="txt", position="0", percent=0.0)
    assert progress.get(tmp_path, book.id) is None
