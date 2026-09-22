"""书库分组：store/groups.py 的 CRUD、归属与边界。"""

from __future__ import annotations

from pathlib import Path

import pytest

from quire.errors import LedgerError
from quire.store import groups, library


def _add(root: Path, book_id: str, title: str = "测试书") -> library.Book:
    (root / "out").mkdir(exist_ok=True)
    target = root / "out" / f"{title}{book_id}.pdf"
    target.write_bytes(b"book-bytes")
    return library.add_book(
        root,
        book_id,
        title=title,
        kind="manga",
        source_url="https://example.com/comic/1",
        files=[("pdf", target)],
    )


def test_group_crud_and_membership(tmp_path):
    _add(tmp_path, "b1")
    _add(tmp_path, "b2", "另一本")
    group = groups.create_group(tmp_path, "g1", " 科幻 ")
    assert group.name == "科幻" and group.members == 0
    assert groups.assign(tmp_path, ["b1", "b2"], "g1") == 2
    assert groups.membership(tmp_path) == {"b1": "g1", "b2": "g1"}
    (listed,) = groups.list_groups(tmp_path)
    assert listed.members == 2 and listed.name == "科幻"
    # 非空分组拒绝删除；移出后可删
    with pytest.raises(LedgerError, match="移出"):
        groups.delete_group(tmp_path, "g1")
    groups.assign(tmp_path, ["b1"], None)
    assert groups.membership(tmp_path) == {"b2": "g1"}
    groups.assign(tmp_path, ["b2"], None)
    groups.delete_group(tmp_path, "g1")
    assert groups.list_groups(tmp_path) == ()


def test_assign_rejects_unknown_book_and_group(tmp_path):
    _add(tmp_path, "b1")
    with pytest.raises(LedgerError, match="没有这本书"):
        groups.assign(tmp_path, ["b1", "ghost"], None)
    with pytest.raises(LedgerError, match="没有这个分组"):
        groups.assign(tmp_path, ["b1"], "ghost")
    assert groups.assign(tmp_path, [], None) == 0


def test_group_name_validation_and_rename(tmp_path):
    with pytest.raises(LedgerError, match="1–50"):
        groups.create_group(tmp_path, "g1", "  ")
    with pytest.raises(LedgerError, match="1–50"):
        groups.create_group(tmp_path, "g1", "x" * 51)
    groups.create_group(tmp_path, "g1", "旧名")
    groups.rename_group(tmp_path, "g1", "新名")
    assert groups.list_groups(tmp_path)[0].name == "新名"
    with pytest.raises(LedgerError, match="没有这个分组"):
        groups.rename_group(tmp_path, "ghost", "x")


def test_membership_follows_book_deletion(tmp_path):
    _add(tmp_path, "b1")
    groups.create_group(tmp_path, "g1", "科幻")
    groups.assign(tmp_path, ["b1"], "g1")
    groups.drop_book(tmp_path, "b1")
    assert groups.membership(tmp_path) == {}
    # 漏调 drop_book 时读取端也不返回幽灵归属
    _add(tmp_path, "b2")
    groups.assign(tmp_path, ["b2"], "g1")
    library.delete_book(tmp_path, "b2")
    assert groups.membership(tmp_path) == {}
    assert groups.list_groups(tmp_path)[0].members == 0
    # 残留归属行不算成员：分组仍可删除，删除时一并清掉
    groups.delete_group(tmp_path, "g1")
    assert groups.list_groups(tmp_path) == ()
