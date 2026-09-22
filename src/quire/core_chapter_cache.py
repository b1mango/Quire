"""章节缓存的读取侧:结构与版本校验(写入在 core_chapters)。

缓存里存的是**清洗后的结构化正文**(JSON),不是原始 HTML。结构不符时
明确报错,不把坏数据当正文。改结构必须同时改 ``NovelOptions.clean_version``。
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .errors import LedgerError

#: 章节缓存格式版本。改结构必须同时改 ``NovelOptions.clean_version``。
CACHE_SCHEMA = 2


@dataclass(frozen=True, slots=True)
class CachedChapter:
    """一章缓存的正文与元信息。``review`` 是 OCR 低置信行 ``(文本, 置信度)``。"""

    title: str
    paragraphs: tuple[str, ...]
    pages: int = 1
    truncated: bool = False
    source: str = "html"
    review: tuple[tuple[str, float], ...] = ()


def decode_chapter(data: bytes) -> CachedChapter:
    """读取章节缓存。结构不符时明确报错,不把坏数据当正文。"""
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise LedgerError("章节缓存不是合法 JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema") != CACHE_SCHEMA:
        raise LedgerError("章节缓存版本不匹配")
    title = payload.get("title")
    paragraphs = payload.get("paragraphs")
    pages = payload.get("pages", 1)
    truncated = payload.get("truncated", False)
    source = payload.get("source", "html")
    review = payload.get("review", [])
    if (
        not isinstance(title, str)
        or not isinstance(paragraphs, list)
        or not all(isinstance(item, str) for item in paragraphs)
        or not isinstance(pages, int)
        or pages < 1
        or not isinstance(truncated, bool)
        or source not in {"html", "ocr"}
        or not isinstance(review, list)
        or not all(
            isinstance(item, list)
            and len(item) == 2
            and isinstance(item[0], str)
            and isinstance(item[1], int | float)
            for item in review
        )
    ):
        raise LedgerError("章节缓存字段不合法")
    return CachedChapter(
        title,
        tuple(paragraphs),
        pages,
        truncated,
        source,
        tuple((item[0], float(item[1])) for item in review),
    )
