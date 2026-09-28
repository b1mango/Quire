"""Immutable capture inputs and results shared by the backends."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .errors import ConfigError
from .parse.images import FilterPolicy, Rejection


@dataclass(frozen=True, slots=True)
class MangaOptions:
    selector: str | None = None
    attrs: tuple[str, ...] | None = None
    order: str = "auto"
    remove: tuple[str, ...] = ()
    next_selector: str | None = None
    follow_pages: bool = False
    concurrency: int = 4
    rate: float = 4.0
    retries: int = 3
    timeout: float = 20.0
    dpi: int = 150
    paper: str = "original"
    keep_images: bool = False
    referer: str | None = None
    first: int = 1
    last: int = 0
    max_bytes: int = 32 * 1024 * 1024
    overwrite: bool = False
    obey_robots: bool = True
    policy: FilterPolicy = field(default_factory=FilterPolicy)

    def __post_init__(self) -> None:
        if type(self.obey_robots) is not bool:
            raise ConfigError("robots 开关须为布尔值")
        if not 1 <= self.concurrency <= 32 or not 0 <= self.retries <= 10:
            raise ConfigError("并发须为 1-32，重试须为 0-10")
        if any(not math.isfinite(v) or v <= 0 for v in (self.rate, self.timeout, self.dpi)):
            raise ConfigError("速率、超时和 DPI 须为有限正数")
        if self.first < 1 or self.last < 0 or (self.last and self.last < self.first):
            raise ConfigError("页范围无效，起始页从 1 开始")
        if self.order not in {"auto", "dom", "asc", "desc"}:
            raise ConfigError("不支持的排序方式")
        if self.paper not in {"original", "a4", "a5", "b5", "letter"}:
            raise ConfigError("不支持的纸张")
        if not 1 <= self.max_bytes <= 128 * 1024 * 1024:
            raise ConfigError("单个资源大小上限须在 1 byte 到 128 MiB 之间")
        if min(self.policy.min_width, self.policy.min_height, self.policy.min_bytes) < 0:
            raise ConfigError("图片过滤阈值不能为负数")


@dataclass(frozen=True, slots=True)
class ArtifactResult:
    format: str
    path: Path
    bytes: int
    target_met: bool | None


@dataclass(frozen=True, slots=True)
class MangaResult:
    output: Path
    pages_written: int = 0
    pages_failed: int = 0
    pages_rejected: int = 0
    bytes_out: int = 0
    elapsed_s: float = 0.0
    title: str = ""
    warnings: tuple[str, ...] = ()
    rejections: tuple[Rejection, ...] = ()
    failures: tuple[tuple[str, str], ...] = ()
    images_dir: Path | None = None
    report: Path | None = None
    task_id: str | None = None
    resources_reused: int = 0
    source_resources: int = 0
    compression: str | None = None
    target_bytes: int | None = None
    target_met: bool | None = None
    encoding_rounds: int = 0
    quality: int | None = None
    max_edge: int | None = None
    artifacts: tuple[ArtifactResult, ...] = ()
    artifacts_reused: bool = False
    export_recovered: bool = False

    @property
    def total_bytes(self) -> int:
        return (
            sum(artifact.bytes for artifact in self.artifacts) if self.artifacts else self.bytes_out
        )

    @property
    def partial(self) -> bool:
        return bool(self.failures)


class ProgressSink(Protocol):
    def update(self, done: int, total: int, result: MangaResult) -> None: ...


class ChapterProgress(Protocol):
    """小说章节级进度。``result`` 为 None 表示只报数量，不带快照。"""

    def update(self, done: int, total: int, result: NovelResult | None = None) -> None: ...


# ============================================================ 小说


@dataclass(frozen=True, slots=True)
class NovelOptions:
    """小说采集参数。``clean_version`` 参与任务身份，抽取算法变更即换任务。"""

    content_selector: str | None = None
    chapter_selector: str | None = None
    next_selector: str | None = None
    max_chapters: int = 2000
    max_pages: int = 50
    concurrency: int = 3
    rate: float = 4.0
    retries: int = 3
    timeout: float = 20.0
    keep_html: bool = False
    overwrite: bool = False
    referer: str | None = None
    max_bytes: int = 32 * 1024 * 1024
    clean_version: int = 4
    ocr_mode: str = "auto"  # auto|always|never（项目设计.md §6.7）
    ocr_engine: str = "auto"  # auto|tesseract|onnx
    offline: bool = False  # 离线：缺 OCR 模型直接报错（退出码 6），不下载
    model_dir: Path | None = None  # OCR 模型目录；None 用数据根下的 models/
    capture_mode: str = "auto"  # auto | catalogue | single
    chapter_first: int = 1
    chapter_last: int = 0
    chapter_ranges: str = ""  # 多段范围表达式（如 "1-10,15-20"）；非空时取代起止两章
    obey_robots: bool = True

    def __post_init__(self) -> None:
        from .parse.chapter_range import parse_ranges, validate_range

        if type(self.obey_robots) is not bool:
            raise ConfigError("robots 开关须为布尔值")

        validate_range(self.chapter_first, self.chapter_last)
        if self.chapter_ranges:
            parse_ranges(self.chapter_ranges)
            if self.chapter_first != 1 or self.chapter_last != 0:
                raise ConfigError("章节范围表达式与起止章节只能选一种")
        ranged = (
            bool(self.chapter_ranges)
            or self.chapter_first != 1
            or self.chapter_last
            not in (
                0,
                1,
            )
        )
        if self.capture_mode == "single" and ranged:
            raise ConfigError("单章模式不能选择目录范围")
        if self.capture_mode not in {"auto", "catalogue", "single"}:
            raise ConfigError("采集模式须为 auto、catalogue 或 single")
        if not 1 <= self.concurrency <= 32 or not 0 <= self.retries <= 10:
            raise ConfigError("并发须为 1-32，重试须为 0-10")
        if not 1 <= self.max_chapters <= 20000 or not 1 <= self.max_pages <= 200:
            raise ConfigError("章节数上限须为 1-20000，单章页数上限须为 1-200")
        if any(not math.isfinite(v) or v <= 0 for v in (self.rate, self.timeout)):
            raise ConfigError("速率和超时须为有限正数")
        if not 1 <= self.max_bytes <= 128 * 1024 * 1024:
            raise ConfigError("单个资源大小上限须在 1 byte 到 128 MiB 之间")
        if self.ocr_mode not in {"auto", "always", "never"}:
            raise ConfigError("OCR 模式须为 auto、always 或 never")
        if self.ocr_engine not in {"auto", "tesseract", "onnx"}:
            raise ConfigError("OCR 引擎须为 auto、tesseract 或 onnx")


@dataclass(frozen=True, slots=True)
class ChapterResult:
    """一章的结果：序号、标题、字符数，以及是否复用缓存/是否失败。"""

    index: int
    title: str
    url: str
    chars: int = 0
    paragraphs: int = 0
    pages: int = 1
    reused: bool = False
    truncated: bool = False
    missing_reason: str | None = None

    @property
    def failed(self) -> bool:
        return self.missing_reason is not None


@dataclass(frozen=True, slots=True)
class NovelResult:
    output: Path
    title: str = ""
    chapters_written: int = 0
    chapters_failed: int = 0
    characters: int = 0
    elapsed_s: float = 0.0
    warnings: tuple[str, ...] = ()
    failures: tuple[tuple[str, str], ...] = ()
    chapters: tuple[ChapterResult, ...] = ()
    artifacts: tuple[ArtifactResult, ...] = ()
    report: Path | None = None
    task_id: str | None = None
    resources_reused: int = 0
    source_resources: int = 0
    pages_fetched: int = 0
    html_dir: Path | None = None
    truncated: bool = False
    ocr_chapters: int = 0
    review: Path | None = None

    @property
    def total_bytes(self) -> int:
        return sum(artifact.bytes for artifact in self.artifacts)

    @property
    def partial(self) -> bool:
        return bool(self.failures) or self.truncated or any(c.truncated for c in self.chapters)
