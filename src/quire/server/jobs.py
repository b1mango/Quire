"""Web UI 任务执行：单任务队列、取消、进度轮询与 SSE 事件。

任务在独立线程里跑 ``asyncio`` 事件循环，取消经 ``loop.call_soon_threadsafe``
转成 core 管线原生的 task cancellation。进度来自两处：导出阶段的
ProgressSink 回调，以及对账本（只读连接）与任务缓存目录的轮询——
漫画每完成一页就生成缩略图并经 SSE 推给前端（项目设计.md §3.3）。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import sqlite3
import threading
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..core_series import SeriesResult
from ..errors import QuireError
from ..export_options import available_output
from ..fetch.browser import RenderOptions
from ..image.options import CompressionOptions
from ..models import MangaOptions, MangaResult, NovelOptions, NovelResult
from ..novel_options import available_novel_output
from ..sites.rules import resolve_rule
from ..store import library
from ..store.models import JsonValue
from ..utils.naming import safe_filename
from .job_state import Job, JobSpec
from .progress import ChapterSink, ExportSink, make_thumbs, task_counts
from .settings import UiSettings

_LOG = logging.getLogger(__name__)

type MangaRunner = Callable[..., Coroutine[Any, Any, MangaResult]]
type NovelRunner = Callable[..., Coroutine[Any, Any, NovelResult]]


class JobConflictError(Exception):
    """已有任务进行中：UI 一次只跑一个任务（账本单写者边界）。"""


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
            if isinstance(result, SeriesResult):
                self._settle(
                    job,
                    "partial" if result.partial else "done",
                    "",
                    done_payload={
                        "book_id": job.book_id,
                        "title": result.title,
                        "partial": result.partial,
                        "warnings": list(result.warnings),
                        "volumes": len(result.volumes),
                        "bytes": sum(v.total_bytes for v in result.volumes),
                    },
                )
                return
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

    async def _capture(
        self, job: Job, settings: UiSettings
    ) -> MangaResult | NovelResult | SeriesResult:
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

        rule = resolve_rule(self.data_root, spec.url)
        manga_options = MangaOptions(
            concurrency=settings.concurrency, rate=settings.rate, follow_pages=True
        )
        novel_options = NovelOptions(
            concurrency=settings.concurrency,
            rate=settings.rate,
            ocr_mode=spec.ocr,
            model_dir=self.data_root / "models",
            capture_mode=spec.capture_mode,
            max_chapters=20000,
        )
        render = RenderOptions(timeout=60, max_scrolls=1000) if spec.render else None
        if rule:
            manga_options, novel_options = rule.manga(manga_options), rule.novel(novel_options)
        if spec.series or (spec.kind == "manga" and spec.capture_mode == "catalogue"):
            from ..core_series import run_series

            def delivered(result: MangaResult) -> None:
                book = self._register(job, result)
                with job.condition:
                    job.book_id = book.id
                    job.done += 1
                job.emit("volume", {"book_id": book.id, "title": result.title, "done": job.done})

            return await run_series(
                spec.url,
                base,
                split_by=spec.split_by,
                selected=spec.volumes,
                rule=rule,
                options=manga_options,
                workdir=workdir,
                compression=CompressionOptions(spec.compress, spec.target_bytes),
                formats=spec.formats,
                on_volume=delivered,
                render=render,
            )
        poll = asyncio.create_task(self._poll(job, workdir, thumbs))
        try:
            if spec.kind == "manga":
                out = available_output(
                    base.with_suffix(f".{spec.formats[0]}"), spec.formats, overwrite=False
                )
                return await self._run_manga(
                    spec.url,
                    out,
                    options=manga_options,
                    workdir=workdir,
                    resume=True,
                    progress=ExportSink(job, on_page=sweep_thumbs),
                    compression=CompressionOptions(spec.compress, spec.target_bytes),
                    formats=spec.formats,
                    render=render,
                )
            out = available_novel_output(
                base.with_suffix(f".{spec.formats[0]}"), spec.formats, overwrite=False
            )
            return await self._run_novel(
                spec.url,
                out,
                options=novel_options,
                workdir=workdir,
                formats=spec.formats,
                progress=ChapterSink(job),
                render=render,
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
