"""书库分组：分组定义与书籍归属两张表（项目设计.md §6.12）。

沿用 follows 的先例：``CREATE TABLE IF NOT EXISTS`` 在旧书库打开时补建，
不改 books 结构、不动 user_version。一本书至多属于一个分组；删除分组
仅限空分组，组内还有书时明确拒绝，避免误清空用户的整理结果。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..errors import LedgerError
from . import library

_GROUP_SCHEMA = """
CREATE TABLE IF NOT EXISTS group_defs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS group_members (
    book_id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL
)
"""


@dataclass(frozen=True, slots=True)
class Group:
    id: str
    name: str
    created_at: str
    members: int


def _open(data_root: Path) -> sqlite3.Connection:
    connection = library._open(data_root)
    try:
        connection.executescript(_GROUP_SCHEMA)
        connection.commit()
    except sqlite3.Error as exc:
        connection.close()
        raise LedgerError("无法初始化分组表") from exc
    return connection


def _check_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned or len(cleaned) > 50:
        raise LedgerError("分组名须为 1–50 个字符")
    return cleaned


def create_group(data_root: Path, group_id: str, name: str) -> Group:
    cleaned = _check_name(name)
    created = datetime.now(UTC).isoformat()
    with _open(data_root) as connection:
        connection.execute("INSERT INTO group_defs VALUES (?, ?, ?)", (group_id, cleaned, created))
    return Group(group_id, cleaned, created, 0)


def _require_group(connection: sqlite3.Connection, group_id: str) -> None:
    row = connection.execute("SELECT 1 FROM group_defs WHERE id = ?", (group_id,)).fetchone()
    if row is None:
        raise LedgerError("没有这个分组")


def rename_group(data_root: Path, group_id: str, name: str) -> None:
    cleaned = _check_name(name)
    with _open(data_root) as connection:
        changed = connection.execute(
            "UPDATE group_defs SET name = ? WHERE id = ?", (cleaned, group_id)
        ).rowcount
    if not changed:
        raise LedgerError("没有这个分组")


def delete_group(data_root: Path, group_id: str) -> None:
    """删除空分组；组内还有书时拒绝（先移出再删）。

    成员数按现存书籍计（join books）——书删除后残留的归属行不算数，
    删除分组时一并清掉。
    """
    with _open(data_root) as connection:
        _require_group(connection, group_id)
        members = connection.execute(
            "SELECT COUNT(*) FROM group_members m JOIN books b ON b.id = m.book_id "
            "WHERE m.group_id = ?",
            (group_id,),
        ).fetchone()[0]
        if members:
            raise LedgerError("分组里还有书，请先把书移出分组")
        connection.execute("DELETE FROM group_members WHERE group_id = ?", (group_id,))
        connection.execute("DELETE FROM group_defs WHERE id = ?", (group_id,))


def assign(data_root: Path, book_ids: Sequence[str], group_id: str | None) -> int:
    """把书移动到分组（``group_id=None`` 表示移出分组）；返回实际归属的书数。

    未知书号整单拒绝，不写半截归属；移出分组不要求分组存在。
    """
    ids = list(dict.fromkeys(book_ids))
    if not ids:
        return 0
    with _open(data_root) as connection:
        if group_id is not None:
            _require_group(connection, group_id)
        known = {
            row[0]
            for row in connection.execute(
                f"SELECT id FROM books WHERE id IN ({','.join('?' * len(ids))})", ids
            )
        }
        missing = [book_id for book_id in ids if book_id not in known]
        if missing:
            raise LedgerError(f"书库里没有这本书：{missing[0]}")
        if group_id is None:
            connection.executemany(
                "DELETE FROM group_members WHERE book_id = ?", ((book_id,) for book_id in ids)
            )
        else:
            connection.executemany(
                "INSERT OR REPLACE INTO group_members VALUES (?, ?)",
                ((book_id, group_id) for book_id in ids),
            )
    return len(ids)


def drop_book(data_root: Path, book_id: str) -> None:
    """书被删除时顺带清掉归属记录（幂等， endpoints 删除路径调用）。"""
    with _open(data_root) as connection:
        connection.execute("DELETE FROM group_members WHERE book_id = ?", (book_id,))


def list_groups(data_root: Path) -> tuple[Group, ...]:
    with _open(data_root) as connection:
        rows = connection.execute(
            "SELECT g.id, g.name, g.created_at, "
            "(SELECT COUNT(*) FROM group_members m JOIN books b ON b.id = m.book_id "
            "WHERE m.group_id = g.id) "
            "FROM group_defs g ORDER BY g.created_at"
        ).fetchall()
    return tuple(Group(row["id"], row["name"], row["created_at"], row[3]) for row in rows)


def membership(data_root: Path) -> dict[str, str]:
    """现存书籍的 book_id → group_id 映射；书的归属行随书删除而失效。"""
    with _open(data_root) as connection:
        rows = connection.execute(
            "SELECT m.book_id, m.group_id FROM group_members m JOIN books b ON b.id = m.book_id"
        ).fetchall()
    return {row["book_id"]: row["group_id"] for row in rows}
