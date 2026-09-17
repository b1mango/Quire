"""A single encoded page shared by the core output writers."""

from __future__ import annotations

from dataclasses import dataclass


def clean_metadata_text(text: str) -> str:
    """Keep XML-compatible text shared by the PDF, archive and report metadata."""
    return "".join(
        char
        for char in text
        if (ord(char) >= 0x20 or char in "\t\n\r")
        and not 0x7F <= ord(char) <= 0x9F
        and not 0xD800 <= ord(char) <= 0xDFFF
        and ord(char) not in {0xFFFE, 0xFFFF}
    )


@dataclass(frozen=True, slots=True)
class ExportPage:
    data: bytes
    width: int
    height: int
    source_index: int
    source_url: str
    part: int = 1
    missing_reason: str | None = None


@dataclass(frozen=True, slots=True)
class NovelChapter:
    """一章已清洗的正文。``missing_reason`` 非空表示这一章抓取失败。

    ``source`` 记录正文来源（``html`` 或 ``ocr``，项目设计.md §5 Page.source）；
    ``review`` 是 OCR 低置信行 ``(文本, 置信度)`` 清单，供导出期汇总 review.txt。
    """

    index: int
    title: str
    paragraphs: tuple[str, ...]
    source_url: str = ""
    missing_reason: str | None = None
    pages: int = 1
    truncated: bool = False
    source: str = "html"
    review: tuple[tuple[str, float], ...] = ()

    @property
    def chars(self) -> int:
        return sum(len(paragraph) for paragraph in self.paragraphs)

    @property
    def heading(self) -> str:
        return self.title.strip() or f"第 {self.index} 章"
