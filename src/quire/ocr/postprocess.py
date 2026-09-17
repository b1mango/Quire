"""OCR 后处理：行合并、标点归一化、保守误识修正、跨页去重、复核清单。

全部是纯函数（项目设计.md §6.7、§17.3）。行合并只依赖每行的文本、
置信度与水平位置：缩进判定用正文中位字宽，满宽判定用中位行宽，
这样不假设具体字号与页宽。误识修正表刻意保守——只收 OCR 把全角
标点降级成半角片假名式符号这类几乎不可能是有意书写的情况，
拿不准的一律留给 review 清单而不是擅自改写。
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence

from ..text.clean import normalize_line
from .base import REVIEW_CONFIDENCE, OcrLine, ReviewEntry

#: 一行结尾出现这些标点，下一段另起（句号类终止符）。
_TERMINAL = re.compile(r"[。！？；…：”’」』）】]$")

#: 保守误识修正表：OCR 常把全角标点识成半角兼容符号。
COMMON_FIXES: tuple[tuple[str, str], ...] = (
    ("｡", "。"),
    ("､", "、"),
    ("･", "·"),
    ("‧", "·"),
)

#: 跨页去重：短行出现在超过该比例的页（章）里就删掉（页眉页脚类）。
REPEAT_RATIO = 0.6
REPEAT_MAX_LEN = 30

#: 缩进与满宽判定系数（相对中位字宽/中位行宽）。
_INDENT_CHARS = 1.5
_FULL_WIDTH = 0.85


def _median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def fix_misread(text: str) -> str:
    """套用保守误识修正表。"""
    for wrong, right in COMMON_FIXES:
        text = text.replace(wrong, right)
    return text


def merge_lines(lines: Sequence[OcrLine]) -> tuple[str, ...]:
    """把识别行合并成段落（项目设计.md §6.7）。

    新段条件：本行首行缩进（约 2 字符），或上一行以终止标点结尾；
    否则拼接（上一行未结束且本行满宽，是同一段的续行）。
    """
    usable = [line for line in lines if line.text.strip()]
    if not usable:
        return ()
    left = min(line.x0 for line in usable)
    widths = [line.x1 - line.x0 for line in usable if line.x1 > line.x0]
    char_widths = [
        (line.x1 - line.x0) / len(line.text.strip())
        for line in usable
        if line.x1 > line.x0 and line.text.strip()
    ]
    body_width = _median(widths)
    char_width = _median(char_widths)
    paragraphs: list[str] = []
    for line in usable:
        text = fix_misread(line.text.strip())
        indented = bool(char_width) and line.x0 - left >= _INDENT_CHARS * char_width
        full_width = bool(body_width) and (line.x1 - line.x0) >= _FULL_WIDTH * body_width
        if not paragraphs or indented or _TERMINAL.search(paragraphs[-1]):
            paragraphs.append(text)
        elif full_width or not char_width:
            paragraphs[-1] += text
        else:
            # 短行后面又跟短行：无法判断归属时另起，宁多分段不错拼。
            paragraphs.append(text)
    return tuple(paragraphs)


def normalize_paragraphs(paragraphs: Sequence[str]) -> tuple[str, ...]:
    """标点归一化：复用正文清洗的半角→全角、省略号、中文间空格规则。"""
    return tuple(line for line in (normalize_line(item) for item in paragraphs) if line)


def drop_repeated_short_lines(
    chapters: Sequence[tuple[str, ...]],
    *,
    ratio: float = REPEAT_RATIO,
    max_len: int = REPEAT_MAX_LEN,
) -> list[tuple[str, ...]]:
    """跨页去重：出现在超过 ``ratio`` 比例章里的短行整书删除（页眉/页脚）。"""
    if len(chapters) < 3:
        return list(chapters)
    counts: Counter[str] = Counter()
    for paragraphs in chapters:
        for line in {item for item in paragraphs if len(item) <= max_len}:
            counts[line] += 1
    repeated = {
        line for line, count in counts.items() if count >= 3 and count / len(chapters) > ratio
    }
    if not repeated:
        return list(chapters)
    return [tuple(line for line in paragraphs if line not in repeated) for paragraphs in chapters]


def collect_review(lines: Sequence[OcrLine], chapter: int) -> list[ReviewEntry]:
    """低置信行（<0.6）汇总进复核清单。"""
    return [
        ReviewEntry(chapter, line.text.strip(), round(line.confidence, 3))
        for line in lines
        if line.text.strip() and line.confidence < REVIEW_CONFIDENCE
    ]
