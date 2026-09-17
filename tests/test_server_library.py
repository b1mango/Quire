from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from quire.errors import LedgerError
from quire.store import library


def _artifact(path: Path, data: bytes = b"book-bytes") -> Path:
    path.write_bytes(data)
    return path


def _add(root: Path, book_id: str = "b1", title: str = "测试书") -> library.Book:
    (root / "out").mkdir(exist_ok=True)
    target = _artifact(root / "out" / f"{title}.pdf")
    return library.add_book(
        root,
        book_id,
        title=title,
        kind="manga",
        source_url="https://example.com/comic/1",
        files=[("pdf", target)],
        compress="balanced",
    )


def test_add_list_get_search_roundtrip(tmp_path):
    book = _add(tmp_path)
    assert book.bytes == len(b"book-bytes")
    assert book.artifacts[0].sha256
    again = _add(tmp_path, "b2", "另一本书")
    assert library.list_books(tmp_path)[0].id == again.id  # 新书在前
    assert [b.id for b in library.list_books(tmp_path, "测试")] == [book.id]
    assert library.list_books(tmp_path, "没有的书") == ()
    loaded = library.get_book(tmp_path, book.id)
    assert loaded.title == "测试书"
    assert loaded.files[0].format == "pdf"


def test_reopen_existing_database(tmp_path):
    _add(tmp_path)
    loaded = library.list_books(tmp_path)
    assert len(loaded) == 1
    # 重复打开不重建、不丢数据
    assert library.get_book(tmp_path, "b1").kind == "manga"


def test_unknown_database_version_rejected(tmp_path):
    _add(tmp_path)
    db = tmp_path / "library.db"
    connection = sqlite3.connect(db)
    connection.execute("PRAGMA user_version = 99")
    connection.close()
    with pytest.raises(LedgerError):
        library.list_books(tmp_path)
    # 原数据未被改写
    connection = sqlite3.connect(db)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 99
    connection.close()


def test_add_rejects_symlink_and_missing(tmp_path):
    real = _artifact(tmp_path / "real.pdf")
    link = tmp_path / "link.pdf"
    link.symlink_to(real)
    with pytest.raises(LedgerError):
        library.add_book(
            tmp_path, "x", title="t", kind="manga", source_url="https://e.c", files=[("pdf", link)]
        )
    with pytest.raises(LedgerError):
        library.add_book(
            tmp_path,
            "y",
            title="t",
            kind="manga",
            source_url="https://e.c",
            files=[("pdf", tmp_path / "none.pdf")],
        )


def test_add_requires_book_file(tmp_path):
    report = _artifact(tmp_path / "x.report.json", b"{}")
    with pytest.raises(LedgerError):
        library.add_book(
            tmp_path,
            "z",
            title="t",
            kind="manga",
            source_url="https://e.c",
            files=[("report", report)],
        )


def test_delete_removes_files_and_record(tmp_path):
    book = _add(tmp_path)
    report = _artifact(book.artifacts[0].path.with_suffix(".report.json"), b"{}")
    library.add_book(
        tmp_path,
        "b2",
        title="第二本",
        kind="novel",
        source_url="https://e.c/n",
        files=[("epub", _artifact(tmp_path / "out" / "第二本.epub")), ("report", report)],
    )
    cover_dir = tmp_path / "covers"
    cover_dir.mkdir()
    (cover_dir / "b2.jpg").write_bytes(b"cover")
    library.set_cover(tmp_path, "b2", "covers/b2.jpg")

    freed = library.delete_book(tmp_path, "b2")
    assert freed == len(b"book-bytes") + len(b"{}")
    assert not (tmp_path / "out" / "第二本.epub").exists()
    assert not report.exists()
    assert not (cover_dir / "b2.jpg").exists()
    assert [b.id for b in library.list_books(tmp_path)] == [book.id]
    with pytest.raises(LedgerError):
        library.delete_book(tmp_path, "b2")


def test_delete_refuses_modified_artifact(tmp_path):
    book = _add(tmp_path)
    book.artifacts[0].path.write_bytes(b"tampered content")
    with pytest.raises(LedgerError, match="已被修改"):
        library.delete_book(tmp_path, book.id)
    assert book.artifacts[0].path.exists()  # 被改过的文件保留
    assert library.get_book(tmp_path, book.id)  # 记录保留


def test_delete_tolerates_missing_artifact(tmp_path):
    book = _add(tmp_path)
    book.artifacts[0].path.unlink()
    library.delete_book(tmp_path, book.id)
    assert library.list_books(tmp_path) == ()


def test_set_cover_unknown_book(tmp_path):
    with pytest.raises(LedgerError):
        library.set_cover(tmp_path, "ghost", "covers/x.jpg")
