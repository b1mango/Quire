"""Report settled chapters from the exact ledger tasks attached to a UI job.

Registration happens after cache verification. Polling observes committed resources only;
a final sweep covers captures that finish between ticks. No chapter body is exposed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .job_state import Job


_LOG = logging.getLogger(__name__)


@dataclass
class _Tracked:
    titles: tuple[str, ...]
    reused: frozenset[int]
    reported: set[int] = field(default_factory=set)
    complete: bool = False


class ChapterTracker:
    def __init__(self, job: Job, workdir: Path):
        self.job = job
        self.path = workdir / "ledger.db"
        self.tasks: dict[str, _Tracked] = {}

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(f"{self.path.as_uri()}?mode=ro", uri=True, timeout=0.1)

    @staticmethod
    def _counts(db: sqlite3.Connection, task_id: str) -> list[tuple[int, int, int, int]]:
        return db.execute(
            "SELECT chapter, COUNT(*), SUM(status='done'), SUM(status='failed') "
            "FROM resources WHERE task_id=? GROUP BY chapter ORDER BY chapter",
            (task_id,),
        ).fetchall()

    def register(self, task_id: str) -> None:
        if task_id in self.tasks:
            return
        db = self._connect()
        try:
            row = db.execute("SELECT options_json FROM tasks WHERE id=?", (task_id,)).fetchone()
            options = json.loads(row[0])
            titles = tuple(item[0] for item in options.get("chapters", [])) or tuple(
                options.get("chapter_titles", ())
            )
            counts = self._counts(db, task_id)
            self.tasks[task_id] = _Tracked(
                titles, frozenset(chapter for chapter, total, done, _ in counts if done == total)
            )
            self._report(task_id, counts)
        finally:
            db.close()

    def register_safely(self, task_id: str) -> None:
        try:
            self.register(task_id)
        except Exception:
            _LOG.exception("job %s chapter registration failed", self.job.id)

    def _report(self, task_id: str, counts: list[tuple[int, int, int, int]]) -> None:
        tracked = self.tasks[task_id]
        for chapter, total, done, failed in counts:
            if chapter in tracked.reported or done + failed != total:
                continue
            title = (
                tracked.titles[chapter - 1]
                if chapter <= len(tracked.titles)
                else self.job.spec.title
            )
            self.job.record_chapter(
                {
                    "id": f"{task_id}:{chapter}",
                    "title": title,
                    "status": "failed" if failed else "done",
                    "pages": total,
                    "failed_pages": failed,
                    "reused": chapter in tracked.reused,
                }
            )
            tracked.reported.add(chapter)
        tracked.complete = len(tracked.reported) == len(counts)

    def sweep(self) -> None:
        pending = [task_id for task_id, tracked in self.tasks.items() if not tracked.complete]
        if not pending:
            return
        db = self._connect()
        try:
            for task_id in pending:
                self._report(task_id, self._counts(db, task_id))
        finally:
            db.close()

    def sweep_safely(self) -> None:
        try:
            self.sweep()
        except Exception:
            _LOG.exception("job %s chapter sweep failed", self.job.id)

    async def watch(self) -> None:
        while True:
            await asyncio.sleep(0.3)
            self.sweep_safely()
