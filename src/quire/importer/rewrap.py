"""TXT 硬换行修复：按固定宽度折行的文本（一句话被切成几行）合并回段落。

网盘 TXT 有不少是从网页/旧阅读器导出，按「每行 N 字」或「每行 N 字节」强制折行。识别方式：
按显示宽度（中日韩全角记 2、其余记 1，正好对上按 GBK 字节折行的文件）统计行宽，最常见的
「满行宽度」W ≥ 30 且满行占比 ≥ 25%、满行里 ≥ 60% 不以句末标点结尾，就判为硬换行。
合并规则：上一行是满行（宽度 ≥ W−2）且不以句末标点结尾、本行没有行首缩进 → 接到上一段；
遇到缩进、空行、标题或句末标点就分段。判不出时什么都不改。
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from collections.abc import Iterable

TERMINAL = tuple("。！？!?…”」』》）)～~")
_SAMPLE = 20000


def display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def detect_width(lines: Iterable[str]) -> int:
    """返回折行宽度（显示宽度）；不是硬换行文本时返回 0。"""
    widths: list[tuple[int, bool]] = []
    for raw in lines:
        line = raw.rstrip("\r\n")  # 行尾空格也占折行宽度，只去换行符
        if line.strip():
            widths.append((display_width(line), line.rstrip().endswith(TERMINAL)))
        if len(widths) >= _SAMPLE:
            break
    if len(widths) < 40:
        return 0
    common = Counter(w for w, _ in widths if w >= 30).most_common(1)
    if not common:
        return 0
    width = common[0][0]
    full = [ends for w, ends in widths if width - 2 <= w <= width]
    open_ended = sum(1 for ends in full if not ends)
    if len(full) >= 20 and len(full) >= len(widths) * 0.25 and open_ended >= len(full) * 0.6:
        return width
    return 0


def indented(raw: str) -> bool:
    return raw[:1] in (" ", "\t", "　")


def continues(previous_raw: str, width: int) -> bool:
    """上一行是否是被硬折断的满行（下一行应接上去）。"""
    line = previous_raw.rstrip("\r\n")
    return bool(width) and display_width(line) >= width - 2 and not line.rstrip().endswith(TERMINAL)


def join(head: str, tail: str) -> str:
    if head[-1:].isascii() and head[-1:].isalnum() and tail[:1].isascii() and tail[:1].isalnum():
        return f"{head} {tail}"
    return head + tail
