"""Validated UI task input and synchronized event state."""

from __future__ import annotations

import asyncio
import itertools
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..errors import ConfigError
from ..export_options import validate_formats
from ..image.options import PRESETS
from ..novel_options import validate_novel_formats
from ..parse.chapter_range import validate_range
from ..parse.series import split_spec
from ..store.models import JsonValue
from ..utils.naming import safe_filename


@dataclass(frozen=True, slots=True)
class JobSpec:
    kind: str  # "manga" | "novel"
    url: str
    title: str
    formats: tuple[str, ...]
    compress: str = "balanced"
    target_bytes: int | None = 50_000_000
    ocr: str = "auto"

    series: bool = False
    split_by: str = "none"
    volumes: tuple[int, ...] = ()
    capture_mode: str = "auto"
    render: bool = False
    chapter_first: int = 1
    chapter_last: int = 0

    def __post_init__(self) -> None:
        from .probe import validate_capture_mode, validate_task_url

        validate_task_url(self.url)
        validate_capture_mode(self.kind, self.capture_mode)
        validate_range(self.chapter_first, self.chapter_last)
        if self.capture_mode == "single" and (
            self.chapter_first != 1 or self.chapter_last not in (0, 1)
        ):
            raise ConfigError("单章模式不能选择目录范围")
        if self.volumes and (self.chapter_first != 1 or self.chapter_last != 0):
            raise ConfigError("自定义章节范围按范围重新分卷，不能同时预选原目录卷号")
        if type(self.render) is not bool:
            raise ConfigError("动态页面设置须为布尔值")
        if self.series and self.capture_mode == "single":
            raise ConfigError("单章抓取不能同时启用系列分卷")
        if type(self.series) is not bool or not isinstance(self.split_by, str):
            raise ConfigError("系列设置格式无效")
        mode, _ = split_spec(self.split_by)
        if self.series and self.kind != "manga":
            raise ConfigError("系列分卷用于漫画")
        if (
            not isinstance(self.volumes, tuple)
            or len(set(self.volumes)) != len(self.volumes)
            or any(type(v) is not int or not 1 <= v <= 20000 for v in self.volumes)
        ):
            raise ConfigError("卷号列表无效")
        if mode == "size" and self.volumes:
            raise ConfigError("按体积分卷时卷数尚未确定，不能预选卷号")
        if self.kind == "manga":
            validate_formats(self.formats)
        elif self.kind == "novel":
            validate_novel_formats(self.formats)
        else:
            raise ConfigError("任务类型须为 manga 或 novel")
        if not safe_filename(self.title, default=""):
            raise ConfigError("书名不能为空")
        if self.compress not in PRESETS:
            raise ConfigError("未知压缩档位")
        if self.target_bytes is not None and (
            type(self.target_bytes) is not int or not 1 <= self.target_bytes <= 10**12
        ):
            raise ConfigError("目标体积须为 1 byte 至 1 TB 的整数字节")
        if self.ocr not in {"auto", "always", "never"}:
            raise ConfigError("OCR 模式须为 auto、always 或 never")


@dataclass(frozen=True, slots=True)
class JobEvent:
    seq: int
    kind: str
    data: dict[str, JsonValue]


class Job:
    """一个任务的共享状态；事件追加与状态迁移都在条件变量锁内完成。"""

    def __init__(self, job_id: str, spec: JobSpec) -> None:
        self.id = job_id
        self.spec = spec
        self.created_at = datetime.now(UTC).isoformat()
        self.status = "pending"
        self.phase = ""
        self.done = 0
        self.total = 0
        self.failed_pages = 0
        self.error = ""
        self.hint: str | None = None
        self.book_id: str | None = None
        self.task_id: str | None = None
        self.condition = threading.Condition()
        self._events: list[JobEvent] = []
        self._chapters: dict[str, dict[str, JsonValue]] = {}
        self._seq = itertools.count(1)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[Any] | None = None
        self._cancel_requested = False

    def emit(self, kind: str, data: dict[str, JsonValue]) -> None:
        with self.condition:
            event = JobEvent(next(self._seq), kind, data)
            self._events.append(event)
            if len(self._events) > 1000:
                del self._events[:200]
            self.condition.notify_all()

    def record_chapter(self, chapter: dict[str, JsonValue]) -> None:
        with self.condition:
            key = str(chapter["id"])
            if self._chapters.get(key) == chapter:
                return
            self._chapters[key] = dict(chapter)
            self.emit("chapter", dict(chapter))

    def attach(self, loop: asyncio.AbstractEventLoop, task: asyncio.Task[Any]) -> None:
        with self.condition:
            self._loop = loop
            self._task = task
            pending_cancel = self._cancel_requested
        if pending_cancel:
            loop.call_soon_threadsafe(task.cancel)

    def cancel(self) -> None:
        with self.condition:
            if self.status not in {"pending", "running"}:
                return
            self._cancel_requested = True
            loop, task = self._loop, self._task
        if loop is not None and task is not None:
            loop.call_soon_threadsafe(task.cancel)

    def events_after(self, seq: int, timeout: float) -> list[JobEvent]:
        """返回序号之后的事件；任务已结束时附带最后一条，便于客户端收尾。"""
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                events = [event for event in self._events if event.seq > seq]
                if events or self.status not in {"pending", "running"}:
                    return events or self._events[-1:]
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self.condition.wait(remaining)

    def snapshot(self) -> dict[str, JsonValue]:
        with self.condition:
            return {
                "id": self.id,
                "kind": self.spec.kind,
                "url": self.spec.url,
                "title": self.spec.title,
                "status": self.status,
                "phase": self.phase,
                "done": self.done,
                "total": self.total,
                "failed_pages": self.failed_pages,
                "error": self.error,
                "hint": self.hint,
                "book_id": self.book_id,
                "created_at": self.created_at,
                "chapters": [dict(chapter) for chapter in self._chapters.values()],
            }
