"""Web UI 任务执行：单任务队列、取消、进度轮询与 SSE 事件。

任务在独立线程里跑 ``asyncio`` 事件循环，取消经 ``loop.call_soon_threadsafe``
转成 core 管线原生的 task cancellation。进度来自两处：导出阶段的
ProgressSink 回调，以及对账本（只读连接）与任务缓存目录的轮询——
漫画每完成一页就生成缩略图并经 SSE 推给前端（项目设计.md §3.3）。
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..errors import QuireError
from ..export_options import available_output, validate_formats
from ..image.options import PRESETS, CompressionOptions
from ..models import MangaOptions, MangaResult, NovelOptions, NovelResult
from ..novel_options import available_novel_output, validate_novel_formats
from ..store import library
from ..store.models import JsonValue
from ..utils.naming import safe_filename
from .progress import ChapterSink, ExportSink, make_thumbs, task_counts
from .settings import UiSettings

_LOG = logging.getLogger(__name__)

type MangaRunner = Callable[..., Coroutine[Any, Any, MangaResult]]
type NovelRunner = Callable[..., Coroutine[Any, Any, NovelResult]]


class JobConflictError(Exception):
    """已有任务进行中：UI 一次只跑一个任务（账本单写者边界）。"""


@dataclass(frozen=True, slots=True)
class JobSpec:
    kind: str  # "manga" | "novel"
    url: str
    title: str
    formats: tuple[str, ...]
    compress: str = "balanced"
    target_bytes: int | None = 50_000_000
    ocr: str = "auto"

    def __post_init__(self) -> None:
        from ..errors import ConfigError
        from .probe import validate_task_url

        validate_task_url(self.url)
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
        self.condition = threading.Condition()
        self._events: list[JobEvent] = []
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
            }


class JobManager:
    def __init__(
        self,
        data_root: Path,
        *,
        run_manga: MangaRunner | None = None,
        run_novel: NovelRunner | None = None,
    ) -> None:
        self.data_root = data_root.absolute()
        self.thumbs_root = self.data_root / "thumbs"
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        if run_manga is None or run_novel is None:
            from ..core_manga import run_core_manga
            from ..core_novel import run_core_novel

            run_manga = run_manga or run_core_manga
            run_novel = run_novel or run_core_novel
        self._run_manga = run_manga
        self._run_novel = run_novel

    def submit(self, spec: JobSpec, settings: UiSettings) -> Job:
        with self._lock:
            if any(j.status in {"pending", "running"} for j in self._jobs.values()):
                raise JobConflictError("已有任务进行中")
            job = Job(secrets.token_hex(8), spec)
            self._jobs[job.id] = job
        threading.Thread(
            target=self._thread_main, args=(job, settings), daemon=True, name=f"quire-{job.id}"
        ).start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def jobs(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def cancel(self, job_id: str) -> Job | None:
        job = self.get(job_id)
        if job is not None:
            job.cancel()
        return job

    # ---------------------------------------------------------- 执行线程

    def _thread_main(self, job: Job, settings: UiSettings) -> None:
        with job.condition:
            job.status = "running"
        job.emit("phase", {"phase": "fetching"})
        try:
            result = asyncio.run(self._capture(job, settings))
        except asyncio.CancelledError:
            self._settle(job, "cancelled", "已取消")
            return
        except QuireError as exc:
            self._settle(job, "failed", exc.message, exc.hint)
            return
        except Exception as exc:  # 不向浏览器暴露堆栈（安全清单 §6）
            _LOG.exception("job %s failed", job.id)
            self._settle(job, "failed", f"任务失败：{type(exc).__name__}")
            return
        try:
            book = self._register(job, result)
        except QuireError as exc:
            self._settle(job, "failed", exc.message, exc.hint)
            return
        with job.condition:
            job.book_id = book.id
            if isinstance(result, MangaResult):
                job.done = result.pages_written
                job.failed_pages = result.pages_failed
                job.total = result.pages_written + result.pages_failed
            else:
                job.done = result.chapters_written
                job.failed_pages = result.chapters_failed
                job.total = result.chapters_written + result.chapters_failed
        self._settle(
            job,
            "partial" if result.partial else "done",
            "",
            done_payload={
                "book_id": book.id,
                "title": result.title,
                "partial": result.partial,
                "failures": len(result.failures),
                "warnings": list(result.warnings),
                "bytes": result.total_bytes,
                "elapsed_s": round(result.elapsed_s, 2),
            },
        )

    def _settle(
        self,
        job: Job,
        status: str,
        message: str,
        hint: str | None = None,
        done_payload: dict[str, JsonValue] | None = None,
    ) -> None:
        with job.condition:
            job.status = status
            job.error = message
            job.hint = hint
        if done_payload is not None:
            job.emit("done", done_payload)
        elif status == "cancelled":
            job.emit("cancelled", {"message": message})
        else:
            job.emit("failed", {"message": message, "hint": hint})
        with job.condition:
            job.condition.notify_all()

    async def _capture(self, job: Job, settings: UiSettings) -> MangaResult | NovelResult:
        spec = job.spec
        job.attach(asyncio.get_running_loop(), asyncio.current_task())  # type: ignore[arg-type]
        out_dir = settings.output_path
        out_dir.mkdir(parents=True, exist_ok=True)
        workdir = out_dir / ".quire-core"
        base = out_dir / safe_filename(spec.title, default="book")
        thumbs: dict[str, Any] = {"task_id": "", "done": set()}

        def sweep_thumbs() -> None:
            task_id = thumbs["task_id"]
            if spec.kind != "manga" or not task_id:
                return
            made = make_thumbs(
                workdir / "cache" / task_id, self.thumbs_root / job.id, thumbs["done"]
            )
            for page in made:
                job.emit("thumb", {"page": page, "url": f"/api/jobs/{job.id}/thumbs/{page}"})

        poll = asyncio.create_task(self._poll(job, workdir, thumbs))
        try:
            if spec.kind == "manga":
                out = available_output(
                    base.with_suffix(f".{spec.formats[0]}"), spec.formats, overwrite=False
                )
                return await self._run_manga(
                    spec.url,
                    out,
                    options=MangaOptions(concurrency=settings.concurrency, rate=settings.rate),
                    workdir=workdir,
                    resume=True,
                    progress=ExportSink(job, on_page=sweep_thumbs),
                    compression=CompressionOptions(spec.compress, spec.target_bytes),
                    formats=spec.formats,
                )
            out = available_novel_output(
                base.with_suffix(f".{spec.formats[0]}"), spec.formats, overwrite=False
            )
            return await self._run_novel(
                spec.url,
                out,
                options=NovelOptions(
                    concurrency=settings.concurrency,
                    rate=settings.rate,
                    ocr_mode=spec.ocr,
                    model_dir=self.data_root / "models",
                ),
                workdir=workdir,
                formats=spec.formats,
                progress=ChapterSink(job),
            )
        finally:
            poll.cancel()
            await asyncio.gather(poll, return_exceptions=True)
            sweep_thumbs()  # 部分成功/保留原图时补最后一轮；缓存已清理则空转

    def _register(self, job: Job, result: MangaResult | NovelResult) -> library.Book:
        spec = job.spec
        files: list[tuple[str, Path]] = [(a.format, a.path) for a in result.artifacts]
        if result.report is not None:
            files.append(("report", result.report))
        if isinstance(result, NovelResult) and result.review is not None:
            files.append(("review", result.review))
        book = library.add_book(
            self.data_root,
            secrets.token_hex(8),
            title=result.title or spec.title,
            kind=spec.kind,
            source_url=spec.url,
            files=files,
            compress=spec.compress if spec.kind == "manga" else "",
        )
        thumbs = sorted((self.thumbs_root / job.id).glob("p*.jpg"))
        if thumbs:
            covers = self.data_root / "covers"
            covers.mkdir(parents=True, exist_ok=True)
            target = covers / f"{book.id}.jpg"
            target.write_bytes(thumbs[0].read_bytes())
            library.set_cover(self.data_root, book.id, f"covers/{book.id}.jpg")
        return book

    # ---------------------------------------------------------- 进度轮询

    async def _poll(self, job: Job, workdir: Path, thumbs: dict[str, Any]) -> None:
        started = datetime.now(UTC).isoformat()
        db_path = workdir / "ledger.db"
        connection: sqlite3.Connection | None = None
        try:
            while True:
                await asyncio.sleep(0.4)
                if connection is None:
                    if not db_path.exists():
                        continue
                    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
                try:
                    counts, thumbs["task_id"] = task_counts(connection, started, thumbs["task_id"])
                except sqlite3.Error:
                    continue
                if counts is not None:
                    done, failed, total = counts
                    if (done, failed, total) != (job.done, job.failed_pages, job.total):
                        with job.condition:
                            job.done, job.failed_pages, job.total = done, failed, total
                        job.emit("progress", {"done": done, "failed": failed, "total": total})
                task_id = thumbs["task_id"]
                if job.spec.kind == "manga" and task_id:
                    pages = await asyncio.to_thread(
                        make_thumbs,
                        workdir / "cache" / task_id,
                        self.thumbs_root / job.id,
                        thumbs["done"],
                    )
                    for page in pages:
                        job.emit(
                            "thumb", {"page": page, "url": f"/api/jobs/{job.id}/thumbs/{page}"}
                        )
        finally:
            if connection is not None:
                connection.close()
