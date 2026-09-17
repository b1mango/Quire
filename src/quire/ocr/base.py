"""OCR 数据类型与引擎协议：纯声明，无 I/O（项目设计.md §6.7、§17.3）。

识别结果统一成行（``OcrLine``），后处理只依赖文本、置信度与水平位置：
``x0`` 用来判断首行缩进，``width`` 用来判断这一行是否接近满宽。
引擎是实现 ``OcrEngine`` 协议的可插拔后端——系统 tesseract（探测到就用，
增量 0）或 onnxruntime + PP-OCRv4 mobile 模型（按需下载）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

#: 低置信行阈值：低于它的行进 review 清单（项目设计.md §6.7）。
REVIEW_CONFIDENCE = 0.6


@dataclass(frozen=True, slots=True)
class OcrLine:
    """一行识别结果。坐标是整页像素坐标，用于行合并的缩进/满宽判定。"""

    text: str
    confidence: float
    x0: int = 0
    y0: int = 0
    x1: int = 0
    y1: int = 0

    @property
    def needs_review(self) -> bool:
        return self.confidence < REVIEW_CONFIDENCE


@dataclass(frozen=True, slots=True)
class OcrPage:
    """一页的识别结果：按阅读顺序排列的行与该页平均置信度。"""

    lines: tuple[OcrLine, ...]
    engine: str = ""

    @property
    def confidence(self) -> float:
        if not self.lines:
            return 0.0
        return sum(line.confidence for line in self.lines) / len(self.lines)


@runtime_checkable
class OcrEngine(Protocol):
    """引擎协议（项目设计.md §17.3）：只声明「能做什么」。"""

    @property
    def name(self) -> str: ...

    def recognize(self, image: bytes) -> OcrPage:
        """识别一张图片（JPEG/PNG 字节），返回按阅读顺序排列的行。"""
        ...


@dataclass(frozen=True, slots=True)
class ReviewEntry:
    """一条低置信记录：哪章哪行、置信度多少。"""

    chapter: int
    text: str
    confidence: float
