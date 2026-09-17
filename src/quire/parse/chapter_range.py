"""Inclusive catalogue positions shared by novel capture and manga series."""

from __future__ import annotations

from ..errors import ConfigError


def validate_range(first: int, last: int) -> None:
    if (
        type(first) is not int
        or type(last) is not int
        or not 1 <= first <= 20000
        or not 0 <= last <= 20000
        or last != 0
        and last < first
    ):
        raise ConfigError("章节范围须为 1–20000 的整数，结束章节不能早于起始章节")


def select_range[T](items: tuple[T, ...], first: int, last: int) -> tuple[T, ...]:
    validate_range(first, last)
    if first > len(items) or last > len(items):
        raise ConfigError(f"章节范围超出当前目录（共 {len(items)} 章），请重新识别目录")
    return items[first - 1 : last or None]
