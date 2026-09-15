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
    policy: FilterPolicy = field(default_factory=FilterPolicy)

    def __post_init__(self) -> None:
        if not 1 <= self.concurrency <= 16 or not 0 <= self.retries <= 10:
            raise ConfigError("并发须为 1-16，重试须为 0-10")
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

    @property
    def partial(self) -> bool:
        return bool(self.failures)


class ProgressSink(Protocol):
    def update(self, done: int, total: int, result: MangaResult) -> None: ...
