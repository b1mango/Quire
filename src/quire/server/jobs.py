"""Web UI 任务执行：双槽并行、排队、取消、进度轮询与 SSE 事件。

最多两个任务并行（各占一个线程跑 ``asyncio`` 事件循环），其余排队；
渲染任务串行互斥；同站请求经共享 HostPace 合计限速（项目设计.md §3.3）。
取消经 ``loop.call_soon_threadsafe`` 转成 core 管线原生的 task cancellation。
进度来自两处：导出阶段的 ProgressSink 回调，以及对账本（只读连接）与
任务缓存目录的轮询——漫画每完成一页就生成缩略图并经 SSE 推给前端。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import threading
from collections import deque
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from ..core_series import SeriesResult
from ..errors import PausedError, QuireError
from ..export_options import available_output
from ..fetch.async_policy import HostPace
from ..fetch.browser import RenderOptions
from ..image.options import CompressionOptions
from ..models import MangaOptions, MangaResult, NovelOptions, NovelResult
from ..novel_options import available_novel_output
from ..sites.rules import resolve_rule
from ..store import library
from ..store.models import JsonValue
from ..utils.naming import available_book_dir, safe_filename
from .books import register_book
from .chapter_stream import ChapterTracker
from .follows import resolve_prefix
from .job_state import Job, JobSpec, job_workdir
from .progress import ChapterSink, ExportSink, make_thumbs, poll_job, thumb_event
from .salvage import settle_cancelled
from .settings import UiSettings

_LOG = logging.getLogger(__name__)

MAX_PARALLEL = 2

type MangaRunner = Callable[..., Coroutine[Any, Any, MangaResult]]
type NovelRunner = Callable[..., Coroutine[Any, Any, NovelResult]]


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
        self._queue: deque[tuple[Job, UiSettings]] = deque()
        self._active: dict[str, str] = {}  # job_id → 账本工作目录键（单写者互斥）
        self._render_lock = threading.Lock()
        self._pace = HostPace()  # 服务级共享：并行任务打同一站点合计限速
        if run_manga is None or run_novel is None:
            from ..core_manga import run_core_manga
            from ..core_novel import run_core_novel

            run_manga = run_manga or run_core_manga
            run_novel = run_novel or run_core_novel
        self._run_manga = run_manga
        self._run_novel = run_novel

    def submit(self, spec: JobSpec, settings: UiSettings) -> Job:
        job = Job(secrets.token_hex(8), spec)
        with self._lock:
            self._jobs[job.id] = job
            self._queue.append((job, settings))
        self._pump()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def jobs(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def cancel(self, job_id: str) -> Job | None:
        job = self.get(job_id)
        if job is None:
            return None
        with self._lock:
            queued = job.status == "pending" and any(entry[0] is job for entry in self._queue)
            if queued:
                self._queue = deque(entry for entry in self._queue if entry[0] is not job)
        if queued:
            # 还在排队：直接落定，不占执行槽
            job.cancel()
            self._settle(job, "cancelled", "已取消")
        else:
            job.cancel()
        return job

    def pause(self, job_id: str) -> Job | None:
        job = self.get(job_id)
        if job is not None:
            job.pause()
        return job

    def _pump(self) -> None:
        """空出执行槽时按提交顺序启动排队任务；已被取消的直接落定。

        账本单写者：同一来源 URL 的任务共用一本账本，须等前一个完成。
        """
        start: list[tuple[Job, UiSettings]] = []
        settle: list[Job] = []
        with self._lock:
            busy = set(self._active.values())
            kept: deque[tuple[Job, UiSettings]] = deque()
            for job, settings in self._queue:
                key = str(job_workdir(settings.output_path, job.spec))
                if job.cancel_requested:
                    settle.append(job)
                elif len(self._active) >= MAX_PARALLEL or key in busy:
                    kept.append((job, settings))
                else:
                    busy.add(key)
                    self._active[job.id] = key
                    start.append((job, settings))
            self._queue = kept
        for job in settle:
            self._settle(job, "cancelled", "已取消")
        for job, settings in start:
            threading.Thread(
                target=self._thread_main, args=(job, settings), daemon=True, name=f"quire-{job.id}"
            ).start()

    # ---------------------------------------------------------- 执行线程

    def _thread_main(self, job: Job, settings: UiSettings) -> None:
        try:
            # 渲染任务串行：等待期间任务仍是 pending，不占浏览器
            if job.spec.render:
                with self._render_lock:
                    self._execute(job, settings)
            else:
                self._execute(job, settings)
        except Exception as exc:
            _LOG.exception("job %s finalization failed", job.id)
            self._settle(job, "failed", f"任务失败：{type(exc).__name__}")
        finally:
            with self._lock:
                self._active.pop(job.id, None)
            self._pump()

    def _execute(self, job: Job, settings: UiSettings) -> None:
        with job.condition:
            job.status = "running"
        job.emit("phase", {"phase": "fetching"})
        try:
            result = asyncio.run(self._capture(job, settings))
        except PausedError:
            self._settle(job, "paused", "已暂停，已抓取的部分保留在缓存里")
            return
        except asyncio.CancelledError:
            settle_cancelled(job, settings, self.data_root, self._register, self._settle)
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
        elif status == "paused":
            job.emit("paused", {"message": message})
        else:
            job.emit("failed", {"message": message, "hint": hint})
        with job.condition:
            job.condition.notify_all()

    async def _capture(
        self, job: Job, settings: UiSettings
    ) -> MangaResult | NovelResult | SeriesResult:
        chapters = ChapterTracker(job, job_workdir(settings.output_path, job.spec))
        watcher = asyncio.create_task(chapters.watch())
        try:
            return await self._run_capture(job, settings, chapters)
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            chapters.sweep_safely()

    async def _run_capture(
        self, job: Job, settings: UiSettings, chapters: ChapterTracker
    ) -> MangaResult | NovelResult | SeriesResult:
        spec = job.spec
        job.attach(asyncio.get_running_loop(), asyncio.current_task())  # type: ignore[arg-type]

        def should_stop() -> bool:
            return job.pause_requested

        out_dir = settings.output_path
        out_dir.mkdir(parents=True, exist_ok=True)
        workdir = job_workdir(out_dir, spec)
        book_dir = available_book_dir(out_dir, spec.title)  # 每作品独立成品文件夹
        book_dir.mkdir(parents=True, exist_ok=True)
        base = book_dir / safe_filename(spec.title, default="book")
        thumbs: dict[str, Any] = {"task_id": "", "done": set()}

        def sweep_thumbs() -> None:
            task_id = thumbs["task_id"]
            if spec.kind != "manga" or not task_id:
                return
            made = make_thumbs(
                workdir / "cache" / task_id, self.thumbs_root / job.id, thumbs["done"], task_id
            )
            for page in made:
                job.emit("thumb", thumb_event(workdir / "cache" / task_id, job.id, task_id, page))

        rule = resolve_rule(self.data_root, spec.url)

        def on_task(task_id: str) -> None:
            with job.condition:
                job.task_id = task_id
                thumbs["task_id"] = task_id
            chapters.register(task_id)

        manga_options = MangaOptions(
            concurrency=settings.concurrency,
            rate=settings.rate,
            follow_pages=True,
            obey_robots=settings.obey_robots,
        )
        novel_options = NovelOptions(
            concurrency=settings.concurrency,
            rate=settings.rate,
            ocr_mode=spec.ocr,
            model_dir=self.data_root / "models",
            capture_mode=spec.capture_mode,
            max_chapters=20000,
            chapter_first=spec.chapter_first,
            chapter_last=spec.chapter_last,
            chapter_ranges=spec.chapter_ranges,
            obey_robots=settings.obey_robots,
        )
        render = (
            RenderOptions(
                timeout=60,
                max_scrolls=1000,
                native=settings.browser_native,
                cdp_endpoint=settings.cdp_endpoint,
            )
            if spec.render or settings.browser_native or settings.cdp_endpoint
            else None
        )
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
                book_dir,
                split_by=spec.split_by,
                first=spec.chapter_first,
                last=spec.chapter_last,
                ranges=spec.chapter_ranges,
                selected=spec.volumes,
                rule=rule,
                options=manga_options,
                workdir=workdir,
                compression=CompressionOptions(spec.compress, spec.target_bytes),
                formats=spec.formats,
                on_volume=delivered,
                render=render,
                on_task=on_task,
                stop=should_stop,
                pace=self._pace,
            )
        poll = asyncio.create_task(poll_job(job, workdir, thumbs, self.thumbs_root))
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
                    on_task=on_task,
                    stop=should_stop,
                    pace=self._pace,
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
                on_task=on_task,
                stop=should_stop,
                prepend=resolve_prefix(spec, workdir) if spec.follow_prefix else None,
                pace=self._pace,
            )
        finally:
            poll.cancel()
            await asyncio.gather(poll, return_exceptions=True)
            try:
                sweep_thumbs()  # 部分成功/保留原图时补最后一轮
            except Exception:
                _LOG.exception("job %s final thumbnail sweep failed", job.id)

    def _register(self, job: Job, result: MangaResult | NovelResult) -> library.Book:
        return register_book(self.data_root, self.thumbs_root, job.id, job.spec, result)
