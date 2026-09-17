"""本地书库索引：``library.db`` 记录已成书及其成品文件（项目设计.md §6.12）。

只创建当前 schema，未知版本/结构明确拒绝，不改原数据、不做历史迁移。
成品文件在用户的输出目录里，库内保存绝对路径与 SHA-256；删除前逐件
复核哈希，内容被改过的文件拒绝删除，避免误删用户后续修改。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..errors import LedgerError

_APPLICATION_ID = 0x51554952
_VERSION = 1

_SCHEMA = """
CREATE TABLE books (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    source_url TEXT NOT NULL,
    artifacts TEXT NOT NULL,
    cover TEXT,
    compress TEXT NOT NULL DEFAULT '',
    bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL
)
"""

BOOK_FORMATS = ("pdf", "cbz", "zip", "epub", "txt")


@dataclass(frozen=True, slots=True)
class BookArtifact:
    format: str
    path: Path
    bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class Book:
    id: str
    title: str
    author: str
    kind: str
    source_url: str
    artifacts: tuple[BookArtifact, ...]
    cover: str | None
    compress: str
    bytes: int
    created_at: str

    @property
    def files(self) -> tuple[BookArtifact, ...]:
        """真正的书（不含报告等附属文件）。"""
        return tuple(a for a in self.artifacts if a.format in BOOK_FORMATS)


def _hash(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise LedgerError("书库不存在")
    try:
        connection = sqlite3.connect(str(db_path), timeout=10)
        connection.row_factory = sqlite3.Row
        ok = (
            connection.execute("PRAGMA application_id").fetchone()[0] == _APPLICATION_ID
            and connection.execute("PRAGMA user_version").fetchone()[0] == _VERSION
        )
        if ok:
            row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'books'"
            ).fetchone()
            ok = row is not None and "artifacts" in row[0]
        if not ok:
            connection.close()
            raise LedgerError("书库版本或结构不兼容，未做修改")
        return connection
    except sqlite3.Error as exc:
        raise LedgerError("无法打开书库") from exc


def _create(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        connection = sqlite3.connect(str(db_path), timeout=10)
        connection.row_factory = sqlite3.Row
        connection.executescript(_SCHEMA)
        connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version = {_VERSION}")
        connection.commit()
        os.chmod(db_path, 0o600)
        return connection
    except sqlite3.Error as exc:
        raise LedgerError("无法创建书库") from exc


def _open(data_root: Path) -> sqlite3.Connection:
    db_path = data_root.absolute() / "library.db"
    if db_path.exists():
        return _connect(db_path)
    return _create(db_path)


def _artifact(payload: object) -> BookArtifact:
    if not isinstance(payload, dict):
        raise LedgerError("书库记录损坏")
    fmt, path, size, sha = (
        payload.get("format"),
        payload.get("path"),
        payload.get("bytes"),
        payload.get("sha256"),
    )
    if (
        not isinstance(fmt, str)
        or not isinstance(path, str)
        or type(size) is not int
        or size < 0
        or not isinstance(sha, str)
        or len(sha) != 64
    ):
        raise LedgerError("书库记录损坏")
    return BookArtifact(fmt, Path(path), size, sha)


def _book(row: sqlite3.Row) -> Book:
    try:
        artifacts = tuple(_artifact(item) for item in json.loads(row["artifacts"]))
    except (TypeError, json.JSONDecodeError) as exc:
        raise LedgerError("书库记录损坏") from exc
    return Book(
        id=row["id"],
        title=row["title"],
        author=row["author"],
        kind=row["kind"],
        source_url=row["source_url"],
        artifacts=artifacts,
        cover=row["cover"],
        compress=row["compress"],
        bytes=row["bytes"],
        created_at=row["created_at"],
    )


def _fingerprint(path: Path) -> BookArtifact:
    """登记一件成品：必须是已存在的普通文件，拒绝符号链接。"""
    absolute = path.absolute()
    if absolute.is_symlink() or not absolute.is_file():
        raise LedgerError(f"成品不是普通文件：{absolute}")
    size, sha = _hash(absolute)
    return BookArtifact("", absolute, size, sha)


def add_book(
    data_root: Path,
    book_id: str,
    *,
    title: str,
    kind: str,
    source_url: str,
    files: Sequence[tuple[str, Path]],
    compress: str = "",
    author: str = "",
) -> Book:
    """登记一本书。``files`` 为 ``(格式, 路径)``，登记时实测大小与哈希。"""
    if not book_id or not title or kind not in {"manga", "novel"}:
        raise LedgerError("书籍登记信息不完整")
    artifacts = tuple(
        BookArtifact(fmt, stamped.path, stamped.bytes, stamped.sha256)
        for fmt, path in files
        for stamped in (_fingerprint(path),)
    )
    if not any(a.format in BOOK_FORMATS for a in artifacts):
        raise LedgerError("书籍缺少成品文件")
    total = sum(a.bytes for a in artifacts)
    payload = json.dumps(
        [
            {"format": a.format, "path": str(a.path), "bytes": a.bytes, "sha256": a.sha256}
            for a in artifacts
        ],
        ensure_ascii=False,
    )
    created = datetime.now(UTC).isoformat()
    with _open(data_root) as connection:
        try:
            connection.execute(
                "INSERT INTO books VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
                (book_id, title, author, kind, source_url, payload, compress, total, created),
            )
        except sqlite3.IntegrityError as exc:
            raise LedgerError("书籍编号冲突") from exc
    return Book(book_id, title, author, kind, source_url, artifacts, None, compress, total, created)


def set_cover(data_root: Path, book_id: str, cover: str) -> None:
    with _open(data_root) as connection:
        changed = connection.execute(
            "UPDATE books SET cover = ? WHERE id = ?", (cover, book_id)
        ).rowcount
    if not changed:
        raise LedgerError("未知书籍")


def list_books(data_root: Path, search: str = "") -> tuple[Book, ...]:
    with _open(data_root) as connection:
        rows = connection.execute(
            "SELECT * FROM books WHERE ? = '' OR title LIKE ? OR author LIKE ? "
            "ORDER BY created_at DESC",
            (search, f"%{search}%", f"%{search}%"),
        ).fetchall()
    return tuple(_book(row) for row in rows)


def get_book(data_root: Path, book_id: str) -> Book:
    with _open(data_root) as connection:
        row = connection.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    if row is None:
        raise LedgerError("未找到这本书")
    return _book(row)


def delete_book(data_root: Path, book_id: str) -> int:
    """删除书籍及登记在案的成品文件，返回释放的字节数。

    逐件复核：路径仍是普通文件、不是符号链接、SHA-256 与登记一致才删除；
    内容被改过的文件整单拒绝，已不存在的文件视为已清理。附属文件（报告）
    随书一起删除。
    """
    root = data_root.absolute()
    book = get_book(root, book_id)
    changed = [
        str(a.path)
        for a in book.artifacts
        if a.path.exists() and (a.path.is_symlink() or _hash(a.path)[1] != a.sha256)
    ]
    if changed:
        raise LedgerError(f"成品内容已被修改，拒绝删除：{changed[0]}")
    freed = 0
    for artifact in book.artifacts:
        if artifact.path.exists():
            artifact.path.unlink()
            freed += artifact.bytes
    if book.cover:
        cover = root / book.cover
        if cover.is_file() and not cover.is_symlink() and cover.parent.parent == root:
            cover.unlink()
    with _open(root) as connection:
        connection.execute("DELETE FROM books WHERE id = ?", (book_id,))
    return freed
