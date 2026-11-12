"""小说正文的清洗、归一化与文本判据：纯字符串函数（项目设计.md §6.6、§37.3）。

**宁可留下可疑内容，也不误删正文**。所以噪声表只匹配完整的站点/推广
模板句，重复行删除只在"同一行占了大半章"时才动手，
半角转全角只在中文之间发生（不碰英文、数字和小数点）。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from .noise import (
    _PAGE_NOTICE,
    _SITE_NOTICE,
    URL_REVIEW_RULE,
    has_quotes,
    has_url,
    noise_rule,
    remove_url_fragments,
)

_WS = re.compile(r"\s+")
_ZERO_WIDTH = str.maketrans("", "", "\u200b\u200c\u200d\u2060\ufeff")

#: 内嵌提示必须自成完整句子；不删除对话或句中提及的模板词。
INLINE_NOISE = re.compile(
    rf"(?:^|(?<=[。！？]))\s*(?:{_SITE_NOTICE}|{_PAGE_NOTICE})(?:[。!！]|$)",
    re.IGNORECASE,
)

_CHAPTER_MARK = re.compile(
    r"第\s*[0-9零〇一二三四五六七八九十百千万两]{1,12}\s*[章节回话卷篇]"
    r"|[Cc]hapter\s*\d{1,6}"
)
_TITLE_SPLIT = re.compile(r"\s*[|｜\-–—·_]\s*")

_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000, "万": 10000}

_NUMBER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"第\s*([0-9]{1,6})\s*[章节回话卷篇]"),
    re.compile(r"第\s*([零〇一二三四五六七八九十百千万两]{1,12})\s*[章节回话卷篇]"),
    re.compile(r"[Cc]hapter\s*([0-9]{1,6})"),
    re.compile(r"^\s*([0-9]{1,4})\s*[、.．，:：]\s*\S"),
)

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_PUNCT = re.compile(r"[，。！？；：、“”‘’（）《》〈〉…—,.!?;:\"'()\[\]·]")
_SPACE_BETWEEN_CJK = re.compile(r"(?<=[\u4e00-\u9fff])[ \t]+(?=[\u4e00-\u9fff])")
_ELLIPSIS = re.compile(r"\.{3,}|。{3,}|…{2,}|·{3,}")
_FULLWIDTH = ((",", "，"), (".", "。"), ("!", "！"), ("?", "？"), (":", "："), (";", "；"))
_BRACKET_PAIR = re.compile(r"\((\s*[\u4e00-\u9fff][^()]*?)\)")


# ============================================================ 行级清洗


def is_noise_line(line: str) -> bool:
    """这一行是否属于站点模板/推广/提示，应当整行删除。"""
    text = line.strip()
    if not text:
        return True
    return noise_rule(text) is not None


def normalize_line(text: str) -> str:
    """折叠空白、去掉中文之间的空格、统一省略号、把中文里的半角标点转全角。"""
    value = _SPACE_BETWEEN_CJK.sub("", _WS.sub(" ", text.translate(_ZERO_WIDTH)).strip())
    value = _ELLIPSIS.sub("……", value)
    value = _strip_inline_noise(value)
    for half, full in _FULLWIDTH:
        value = re.sub(rf"(?<=[\u4e00-\u9fff])\s*{re.escape(half)}\s*(?!\d)", full, value)
    # 括号成对处理：只在括号里是中文时转换，避免把 "(hello)" 也改掉。
    value = _BRACKET_PAIR.sub(lambda match: f"（{match.group(1).strip()}）", value)
    return _WS.sub(" ", value).strip()


def _strip_inline_noise(value: str) -> str:
    """只删除完整模板句；含引号的段落保留，避免改写被引用的文字。"""
    if any(mark in value for mark in ('"', "'", "“", "”", "‘", "’", "「", "」", "『", "』")):
        return value
    if not INLINE_NOISE.search(value):
        return value
    cleaned = INLINE_NOISE.sub("", value)
    return _WS.sub(" ", cleaned).strip()


def repeated_line_ratio(lines: list[str] | tuple[str, ...]) -> float:
    """重复行占比：同一行出现多次时，多出来的部分占全部行的比例。"""
    if not lines:
        return 0.0
    counts = Counter(lines)
    repeated = sum(count - 1 for count in counts.values() if count > 1)
    return repeated / len(lines)


@dataclass(frozen=True, slots=True)
class CleanedParagraphs:
    paragraphs: tuple[str, ...]
    removed: tuple[tuple[str, str], ...]


def clean_paragraphs(
    paragraphs: tuple[str, ...] | list[str], *, title: str = ""
) -> CleanedParagraphs:
    """把原始段落清洗成可直接写入成品的正文段落。

    * 空行、噪声行、与章节标题重复的行直接删除；
    * 紧邻的完全重复行只保留一条；
    * 整章被同一行刷屏（占比 > 60%）时删除该行——这是模板页的典型特征。
    """
    chapter_title = normalize_line(strip_site_suffix(title)) if title else ""
    kept: list[tuple[str, str, int]] = []
    removed: list[tuple[int, str, str]] = []
    for index, raw in enumerate(paragraphs):
        source = _SPACE_BETWEEN_CJK.sub("", _WS.sub(" ", raw.translate(_ZERO_WIDTH)).strip())
        rule = noise_rule(source)
        if rule:
            removed.append((index, rule, raw))
            continue
        if has_quotes(raw) and has_url(raw):
            removed.append((index, URL_REVIEW_RULE, raw))
        before_inline = _ELLIPSIS.sub("……", source)
        if before_inline and not _strip_inline_noise(before_inline):
            removed.append((index, "站点提示", raw))
            continue
        line = normalize_line(raw)
        _, fragments = remove_url_fragments(raw)
        if fragments:
            removed.append((index, "网址片段", raw))
            line, _ = remove_url_fragments(line)
        elif not has_quotes(raw) and has_url(raw):
            removed.append((index, URL_REVIEW_RULE, raw))
        if not line:
            continue
        if chapter_title and line == chapter_title:
            removed.append((index, "章节标题", raw))
            continue
        kept.append((line, raw, index))

    spam = _spam_lines([line for line, _, _ in kept])
    deduped: list[str] = []
    for line, raw, index in kept:
        if line in spam:
            removed.append((index, "章节刷屏", raw))
            continue
        if deduped and deduped[-1] == line:
            removed.append((index, "相邻重复", raw))
            continue
        deduped.append(line)
    removed.sort(key=lambda item: item[0])
    return CleanedParagraphs(tuple(deduped), tuple((rule, raw) for _, rule, raw in removed))


def _spam_lines(lines: list[str]) -> set[str]:
    """占比超过 60% 且出现 3 次以上的行，判为模板刷屏。"""
    if len(lines) < 5:
        return set()
    counts = Counter(lines)
    return {line for line, count in counts.items() if count >= 3 and count / len(lines) > 0.6}


# ============================================================ 判据与标题


def cjk_ratio(text: str) -> float:
    """汉字占非空白字符的比例。英文站的正文这个值会很低。"""
    dense = _WS.sub("", text)
    if not dense:
        return 0.0
    return len(_CJK.findall(dense)) / len(dense)


def punctuation_density(text: str) -> float:
    """标点占非空白字符的比例。词库式乱码的标点密度接近 0。"""
    dense = _WS.sub("", text)
    if not dense:
        return 0.0
    return len(_PUNCT.findall(dense)) / len(dense)


def strip_site_suffix(title: str) -> str:
    """去掉标题里的站点名/作者后缀。

    优先取带章节标记的那一段（``第一章 标题_书名_站点``）；
    没有章节标记时取第一段（``书名 - 作者``）。
    """
    text = _WS.sub(" ", title).strip()
    parts = [part.strip() for part in _TITLE_SPLIT.split(text) if part.strip()]
    if len(parts) <= 1:
        return text
    marked = [part for part in parts if _CHAPTER_MARK.search(part)]
    return marked[0] if marked else parts[0]


def cn_number(text: str) -> int | None:
    """中文数字转整数：``十二`` → 12、``一百零五`` → 105。无法解析返回 None。"""
    if not text:
        return None
    if all(char in _CN_DIGITS for char in text):
        return int("".join(str(_CN_DIGITS[char]) for char in text)) or None
    total = section = number = 0
    for char in text:
        if char in _CN_DIGITS:
            number = _CN_DIGITS[char]
        elif char in _CN_UNITS:
            unit = _CN_UNITS[char]
            if unit == 10000:
                total += (section + number) * unit
                section = number = 0
            else:
                section += (number or 1) * unit
                number = 0
        else:
            return None
    return total + section + number or None


def chapter_number(text: str) -> int | None:
    """从标题里解析章节号：``第十二章`` → 12、``Chapter 3`` → 3、``7. 标题`` → 7。"""
    for pattern in _NUMBER_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        token = match.group(1)
        if token.isdigit():
            value = int(token)
            return value if value > 0 else None
        return cn_number(token)
    return None


def has_chapter_mark(text: str) -> bool:
    """标题里是否带章节标记（用于区分目录链接和普通导航链接）。"""
    return _CHAPTER_MARK.search(text) is not None
