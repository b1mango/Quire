"""目录章节范围：1–20000 闭区间，last=0 表示到末尾。"""

from __future__ import annotations

import pytest

from quire.errors import ConfigError
from quire.parse.chapter_range import (
    parse_ranges,
    ranges_selected,
    select_range,
    select_ranges,
    validate_range,
)

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


# ------------------------------------------------------------ 多段范围表达式


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("1", ((1, 1),)),
        ("1-10", ((1, 10),)),
        ("1-10,15-20,103", ((1, 10), (15, 20), (103, 103))),
        ("200-", ((200, None),)),
        (
            "1-5,8-",
            (
                (1, 5),
                (8, None),
            ),
        ),
        (
            " 1-3 , 5 ",
            (
                (1, 3),
                (5, 5),
            ),
        ),
        ("20000", ((20000, 20000),)),
    ],
)
def test_parse_ranges_valid(expr, expected):
    assert parse_ranges(expr) == expected


@pytest.mark.parametrize(
    "expr",
    [
        "",
        "   ",
        "abc",
        "-",
        "1,,2",
        "1 - 3",  # 段内不允许空格
        "0",
        "0-5",
        "20001",
        "1-20001",
        "10-5",  # 倒置
        "1-5,3",  # 重复
        "1-5,5-8",  # 边界重叠
        "1-3,2-",
        "5-,6",  # 开放段不是最后一段
        "5-,10-",
        "1.5",
        "1-2-3",
        "第1章",
        "1;" + "1," * 100,  # 超长
    ],
)
def test_parse_ranges_invalid(expr):
    with pytest.raises(ConfigError):
        parse_ranges(expr)


def test_parse_ranges_type_check():
    with pytest.raises(ConfigError):
        parse_ranges(None)  # type: ignore[arg-type]


def test_select_ranges():
    assert select_ranges(ITEMS, parse_ranges("1-2,4")) == ("第1章", "第2章", "第4章")
    assert select_ranges(ITEMS, parse_ranges("3-")) == ITEMS[2:]
    assert select_ranges(ITEMS, parse_ranges("5")) == ("第5章",)
    assert select_ranges(ITEMS, parse_ranges("1,2,3,4,5")) == ITEMS


def test_select_ranges_beyond_catalogue():
    with pytest.raises(ConfigError, match="超出当前目录"):
        select_ranges(ITEMS, parse_ranges("4-6"))
    with pytest.raises(ConfigError, match="超出当前目录"):
        select_ranges((), parse_ranges("1"))
    # 开放尾不越界：按目录实际长度展开
    assert select_ranges(ITEMS, parse_ranges("4-")) == ITEMS[3:]


def test_ranges_selected_count():
    assert ranges_selected("1-10,15-20,103", total=200) == 17
    assert ranges_selected("3-", total=5) == 3
