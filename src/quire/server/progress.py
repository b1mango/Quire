"""任务进度采集：账本轮询、漫画缩略图与导出阶段回调（项目设计.md §3.3）。

账本用只读连接打开，不影响任务线程的单写者；当前任务取"启动后
被更新过的最新任务"——恢复旧任务时 ``updated_at`` 同样前进。
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

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


def make_thumbs(cache: Path, dest: Path, thumbed: set[int]) -> list[int]:
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
        if page in thumbed:
            continue
        thumbed.add(page)
        try:
            from PIL import Image

            dest.mkdir(parents=True, exist_ok=True)
            with Image.open(cache / name) as image:
                image.thumbnail((160, 224))
                image.convert("RGB").save(dest / f"p{page}.jpg", "JPEG", quality=68)
            made.append(page)
        except Exception:
            _LOG.debug("thumbnail failed for %s", name, exc_info=True)
    return made


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
