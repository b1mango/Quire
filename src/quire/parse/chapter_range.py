"""Inclusive catalogue positions shared by novel capture and manga series."""

from __future__ import annotations

import re

from ..errors import ConfigError

MAX_CHAPTER = 20000


def validate_range(first: int, last: int) -> None:
    if (
        type(first) is not int
        or type(last) is not int
        or not 1 <= first <= MAX_CHAPTER
        or not 0 <= last <= MAX_CHAPTER
        or last != 0
        and last < first
    ):
        raise ConfigError("章节范围须为 1–20000 的整数，结束章节不能早于起始章节")


def select_range[T](items: tuple[T, ...], first: int, last: int) -> tuple[T, ...]:
    validate_range(first, last)
    if first > len(items) or last > len(items):
        raise ConfigError(f"章节范围超出当前目录（共 {len(items)} 章），请重新识别目录")
    return items[first - 1 : last or None]


# ------------------------------------------------------------ 多段范围表达式

_SEGMENT = re.compile(r"(\d+)(?:-(\d*))?")
#: (起, 止) 段；止为 None 表示开放到目录末尾（如 ``200-``）。
type RangeSegments = tuple[tuple[int, int | None], ...]


def parse_ranges(expr: str) -> RangeSegments:
    """解析 gallery-dl 风格范围表达式：``1-10,15-20,103``，尾段可开放 ``200-``。

    章节号 1–20000；区间不得倒置；章节不得在段间重复；开放段只能是最后一段。
    """
    if not isinstance(expr, str) or not expr.strip():
        raise ConfigError("章节范围表达式不能为空")
    if len(expr) > 200:
        raise ConfigError("章节范围表达式过长")
    segments: list[tuple[int, int | None]] = []
    covered: set[int] = set()
    for raw in expr.split(","):
        part = raw.strip()
        match = _SEGMENT.fullmatch(part)
        if match is None:
            raise ConfigError(f"章节范围「{part}」无法识别，示例：1-10,15-20,103")
        first = int(match[1])
        if not 1 <= first <= MAX_CHAPTER:
            raise ConfigError(f"章节号须在 1–{MAX_CHAPTER} 之间")
        if segments and segments[-1][1] is None:
            raise ConfigError("开放区间（如 200-）只能是最后一段")
        if match[2] is not None and not match[2]:
            # 开放尾：与已覆盖区间重叠即重复
            if covered and first <= max(covered):
                raise ConfigError(f"章节 {first} 在范围里重复出现")
            segments.append((first, None))
            continue
        last = int(match[2]) if match[2] is not None else first
        if last < first:
            raise ConfigError(f"章节范围「{part}」倒置，结束不能早于起始")
        if last > MAX_CHAPTER:
            raise ConfigError(f"章节号须在 1–{MAX_CHAPTER} 之间")
        overlap = covered.intersection(range(first, last + 1))
        if overlap:
            raise ConfigError(f"章节 {min(overlap)} 在范围里重复出现")
        covered.update(range(first, last + 1))
        segments.append((first, last))
    return tuple(segments)


def select_ranges[T](items: tuple[T, ...], segments: RangeSegments) -> tuple[T, ...]:
    """按 1-based 章号段挑选目录项；开放尾按目录实际长度展开。"""
    picked: list[T] = []
    for first, last in segments:
        end = len(items) if last is None else last
        if first > len(items) or end > len(items):
            raise ConfigError(f"章节范围超出当前目录（共 {len(items)} 章），请重新识别目录")
        picked.extend(items[first - 1 : end])
    return tuple(picked)


def ranges_selected(expr: str, total: int) -> int:
    """表达式在 ``total`` 章目录下选中的章数（供前端/摘要展示口径一致）。"""
    return len(select_ranges(tuple(range(total)), parse_ranges(expr)))
