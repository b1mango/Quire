"""阅读进度：每本书保存最近一次的格式和位置。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..errors import LedgerError
from . import library

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reading_progress (
    book_id TEXT PRIMARY KEY,
    format TEXT NOT NULL,
    position TEXT NOT NULL,
    percent REAL NOT NULL,
    updated_at TEXT NOT NULL
)
"""


@dataclass(frozen=True, slots=True)
class Progress:
    book_id: str
    format: str
    position: str
    percent: float
    updated_at: str


def _open(data_root: Path) -> sqlite3.Connection:
    connection = library._open(data_root)
    try:
        connection.execute(_SCHEMA)
        connection.commit()
    except sqlite3.Error as exc:
        connection.close()
        raise LedgerError("无法初始化阅读进度表") from exc
    return connection


def get(data_root: Path, book_id: str) -> Progress | None:
    with _open(data_root) as connection:
        row = connection.execute(
            "SELECT * FROM reading_progress WHERE book_id = ?", (book_id,)
        ).fetchone()
    if row is None:
        return None
    return Progress(
        row["book_id"], row["format"], row["position"], row["percent"], row["updated_at"]
    )


def save(data_root: Path, book_id: str, *, format: str, position: str, percent: float) -> None:
    with _open(data_root) as connection:
        changed = connection.execute(
            "INSERT INTO reading_progress "
            "SELECT id, ?, ?, ?, ? FROM books WHERE id = ? "
            "ON CONFLICT(book_id) DO UPDATE SET format = excluded.format, "
            "position = excluded.position, percent = excluded.percent, "
            "updated_at = excluded.updated_at",
            (format, position, percent, datetime.now(UTC).isoformat(), book_id),
        ).rowcount
        if not changed:
            raise LedgerError("未找到这本书")
