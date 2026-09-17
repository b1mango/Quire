"""目录章节范围：1–20000 闭区间，last=0 表示到末尾。"""

from __future__ import annotations

import pytest

from quire.errors import ConfigError
from quire.parse.chapter_range import select_range, validate_range

ITEMS = tuple(f"第{i}章" for i in range(1, 6))


@pytest.mark.parametrize(
    "first,last",
    [(1, 0), (1, 1), (1, 20000), (20000, 20000), (7, 0), (5, 5)],
)
def test_valid_ranges(first, last):
    validate_range(first, last)


@pytest.mark.parametrize(
    "first,last",
    [
        (0, 0),  # 起始从 1 开始
        (0, 5),
        (1, 20001),
        (20001, 0),
        (3, 2),  # 结束早于起始
        (1, -1),
        (True, 1),  # bool 不是合法章节号
        (1, True),
        ("1", 1),
        (1, 2.0),
    ],
)
def test_invalid_ranges(first, last):
    with pytest.raises(ConfigError):
        validate_range(first, last)


def test_select_full_and_open_end():
    assert select_range(ITEMS, 1, 0) == ITEMS
    assert select_range(ITEMS, 3, 0) == ITEMS[2:]
    assert select_range(ITEMS, 2, 4) == ITEMS[1:4]
    assert select_range(ITEMS, 5, 5) == ITEMS[4:]


def test_select_beyond_catalogue():
    with pytest.raises(ConfigError, match="超出当前目录"):
        select_range(ITEMS, 6, 0)
    with pytest.raises(ConfigError, match="超出当前目录"):
        select_range(ITEMS, 1, 6)
    with pytest.raises(ConfigError, match="超出当前目录"):
        select_range((), 1, 1)
