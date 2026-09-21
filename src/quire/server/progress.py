"""任务进度采集：账本轮询、漫画缩略图与导出阶段回调（项目设计.md §3.3）。

账本用只读连接打开，不影响任务线程的单写者；当前任务取"启动后
被更新过的最新任务"——恢复旧任务时 ``updated_at`` 同样前进。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..models import MangaResult, NovelResult

if TYPE_CHECKING:
    from .job_state import Job

_LOG = logging.getLogger(__name__)

_THUMB_NAME = re.compile(r"^\d{5}-(\d{6})\.[a-z0-9]+$")


def task_counts(
    connection: sqlite3.Connection, started: str, current: str
) -> tuple[tuple[int, int, int] | None, str]:
    row = connection.execute(
        "SELECT id FROM tasks WHERE updated_at >= ? ORDER BY updated_at DESC LIMIT 1",
        (started,),
    ).fetchone()
    task_id = row[0] if row else current
    if not task_id:
        return None, ""
    counts = connection.execute(
        "SELECT status, COUNT(*) FROM resources WHERE task_id = ? GROUP BY status", (task_id,)
    ).fetchall()
    by_status = dict(counts)
    total = sum(by_status.values())
    return (by_status.get("done", 0), by_status.get("failed", 0), total), task_id


def make_thumbs(cache: Path, dest: Path, thumbed: set[int], namespace: str = "") -> list[int]:
    """为缓存里新完成的漫画页生成缩略图；损坏文件跳过，不影响任务。"""
    if not cache.is_dir():
        return []
    made: list[int] = []
    try:
        names = sorted(entry.name for entry in cache.iterdir())
    except OSError:
        return []
    for name in names:
        match = _THUMB_NAME.match(name)
        if match is None:
            continue
        page = int(match[1])
        identity = thumb_number(namespace, page) if namespace else page
        if identity in thumbed:
            continue
        thumbed.add(identity)
        try:
            from PIL import Image

            dest.mkdir(parents=True, exist_ok=True)
            with Image.open(cache / name) as image:
                image.thumbnail((160, 224))
                image.convert("RGB").save(dest / f"p{identity}.jpg", "JPEG", quality=68)
            made.append(page)
        except Exception:
            _LOG.debug("thumbnail failed for %s", name, exc_info=True)
    return made


def thumb_number(task_id: str, page: int) -> int:
    return int(hashlib.sha256(task_id.encode()).hexdigest()[:16], 16) * 1_000_000 + page


def thumb_event(cache: Path, job_id: str, task_id: str, page: int) -> dict[str, Any]:
    """Attach the cache filename's chapter to each thumbnail, independent of event order."""
    name = next(cache.glob(f"*-{page:06d}.*"), None)
    chapter = int(name.name.split("-", 1)[0]) if name else 1
    return {
        "page": page,
        "url": f"/api/jobs/{job_id}/thumbs/{thumb_number(task_id, page)}",
        "chapter_id": f"{task_id}:{chapter}",
    }


class ExportSink:
    """漫画导出阶段进度（下载阶段由账本轮询覆盖）。

    导出按页回调时缓存原图尚未清理，是补齐缩略图的最后时机——
    小书下载太快时账本轮询可能错过（项目设计.md §3.3 缩略图流）。
    """

    def __init__(self, job: Job, on_page: Callable[[], None] | None = None) -> None:
        self.job = job
        self.on_page = on_page
        self.announced = False

    def update(self, done: int, total: int, result: MangaResult | None = None) -> None:
        if not self.announced:
            self.announced = True
            self.job.emit("phase", {"phase": "encoding"})
        self.job.emit("progress", {"done": done, "failed": 0, "total": total})
        if self.on_page is not None:
            self.on_page()


class ChapterSink:
    """小说章节级进度。"""

    def __init__(self, job: Job) -> None:
        self.job = job

    def update(self, done: int, total: int, result: NovelResult | None = None) -> None:
        with self.job.condition:
            self.job.done, self.job.total = done, total
        self.job.emit("progress", {"done": done, "failed": 0, "total": total})


def _read_counts(path: Path, started: str, current: str) -> tuple[tuple[int, int, int] | None, str]:
    """Create, query and close the connection on the same worker thread."""
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=0.1)
    try:
        return task_counts(connection, started, current)
    finally:
        connection.close()


async def poll_job(job: Job, workdir: Path, thumbs: dict[str, Any], thumbs_root: Path) -> None:
    """轮询任务账本（只读）推送进度，漫画每完成一页补一张缩略图。"""
    started = datetime.now(UTC).isoformat()
    db_path = workdir / "ledger.db"
    while True:
        await asyncio.sleep(0.4)
        try:
            counts, thumbs["task_id"] = await asyncio.to_thread(
                _read_counts, db_path, started, thumbs["task_id"]
            )
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
                thumbs_root / job.id,
                thumbs["done"],
                task_id,
            )
            for page in pages:
                job.emit("thumb", thumb_event(workdir / "cache" / task_id, job.id, task_id, page))
