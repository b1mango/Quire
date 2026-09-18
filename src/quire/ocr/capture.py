"""OCR 页面识别编排：引擎选择、取图与识别（项目设计.md §6.7）。

引擎选择顺序（``--ocr-engine auto``）：系统 tesseract 探测到就用
（增量 0），否则 onnxruntime + PP-OCRv4 mobile 模型（按需下载）。
引擎**惰性创建**：文本抽取成功的章节不会触发模型下载——文本优先，
抽不到才 OCR。识别是 CPU 密集的同步调用，丢进线程池，
与 §17 的「同步函数 + 线程/进程池」一致。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..errors import UnsupportedError
from ..parse.minidom import Document
from .base import OcrEngine, OcrLine
from .models import ensure_models
from .onnx_engine import OnnxEngine, onnx_available
from .postprocess import merge_lines, normalize_paragraphs
from .tesseract import TesseractEngine, find_tesseract
from .trigger import content_node, image_urls


class ImageFetch(Protocol):
    """取图端口：由编排层注入（复用 AsyncFetcher，继承限速/重试/大小上限）。"""

    async def get(
        self, url: str, *, referer: str | None = None, robots: bool = True
    ) -> ImageResponse: ...


class ImageResponse(Protocol):
    @property
    def content(self) -> bytes: ...


@dataclass(frozen=True, slots=True)
class OcrPageText:
    """一页（可能多张正文图）的 OCR 结果。"""

    paragraphs: tuple[str, ...]
    lines: tuple[OcrLine, ...]
    engine: str
    images: int
    warnings: tuple[str, ...] = ()


class OcrRunner:
    """一次任务共享的 OCR 执行器：持有一个引擎实例，逐页识别正文图。"""

    def __init__(
        self,
        *,
        engine: str = "auto",
        model_dir: Path,
        offline: bool = False,
        content_selector: str | None = None,
        max_bytes: int = 32 * 1024 * 1024,
    ) -> None:
        self.preference = engine
        self.model_dir = model_dir
        self.offline = offline
        self.content_selector = content_selector
        self.max_bytes = max_bytes
        self._engine: OcrEngine | None = None
        self._lock = asyncio.Lock()

    # ---------------------------------------------------------- 引擎
    def _create_engine(self) -> OcrEngine:
        """同步创建引擎（可能伴随模型下载），由 ``to_thread`` 调用。"""
        if self.preference == "tesseract":
            if find_tesseract() is None:
                raise UnsupportedError(
                    "指定的 tesseract 引擎未安装",
                    hint="安装 tesseract（含 chi_sim 语言包），或改用 --ocr-engine onnx",
                )
            return TesseractEngine()
        if self.preference == "onnx":
            return self._create_onnx()
        if find_tesseract() is not None:
            return TesseractEngine()
        if onnx_available():
            return self._create_onnx()
        raise UnsupportedError(
            "没有可用的 OCR 引擎",
            hint="安装系统 tesseract（增量 0），或安装 quire-local[ocr] 使用内置引擎",
        )

    def _create_onnx(self) -> OcrEngine:
        if not onnx_available():
            raise UnsupportedError(
                "指定的 onnx 引擎缺少 onnxruntime",
                hint="安装 quire-local[ocr]，或改用 --ocr-engine tesseract",
            )
        ensure_models(self.model_dir, offline=self.offline)
        return OnnxEngine(self.model_dir)

    async def _get_engine(self) -> OcrEngine:
        async with self._lock:
            if self._engine is None:
                self._engine = await asyncio.to_thread(self._create_engine)
            return self._engine

    # ---------------------------------------------------------- 识别
    async def recognize_page(
        self,
        doc: Document,
        page_url: str,
        fetch: ImageFetch,
        *,
        referer: str | None = None,
    ) -> OcrPageText:
        """识别一页的正文图：无正文图返回空结果，由上层按文本失败处理。"""
        node = content_node(doc, self.content_selector)
        urls = image_urls(node, page_url) if node is not None else []
        if not urls:
            return OcrPageText((), (), "", 0)
        engine = await self._get_engine()
        paragraphs: list[str] = []
        lines: list[OcrLine] = []
        warnings: list[str] = []
        used = 0
        for url in urls:
            response = await fetch.get(url, referer=referer, robots=False)
            if len(response.content) > self.max_bytes:
                warnings.append(f"正文图超过大小上限，已跳过：{url[:100]}")
                continue
            page = await asyncio.to_thread(engine.recognize, response.content)
            merged = merge_lines(page.lines)
            paragraphs.extend(normalize_paragraphs(merged))
            lines.extend(page.lines)
            used += 1
        return OcrPageText(tuple(paragraphs), tuple(lines), engine.name, used, tuple(warnings))
