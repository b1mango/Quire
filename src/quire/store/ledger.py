"""Single-owner transactional task ledger and explicit cache recovery."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import get_ident
from typing import cast

from ..errors import LedgerError
from .cache import fingerprint
from .database import open_database
from .models import (
    FailureCode,
    JsonValue,
    ResourceRecord,
    ResourceSpec,
    ResourceStatus,
    TaskSnapshot,
    TaskStatus,
    task_identity,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _record(row: sqlite3.Row) -> ResourceRecord:
    return ResourceRecord(
        ResourceSpec(row["chapter"], row["page"], row["url"], row["referer"]),
        cast(ResourceStatus, row["status"]),
        row["attempts"],
        row["local_path"],
        row["sha256"],
        row["size"],
        row["error_code"],
    )


class Ledger:
    def __init__(self, data_root: Path) -> None:
        self.root = data_root.absolute()
        self._connection: sqlite3.Connection | None = None
        self._context: AbstractContextManager[sqlite3.Connection] | None = None
        self._owner: int | None = None

    def __enter__(self) -> Ledger:
        if self._context is not None:
            raise LedgerError("Ledger is already open")
        context = open_database(self.root)
        self._connection = context.__enter__()
        self._context = context
        self._owner = get_ident()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._context is not None and self._owner != get_ident():
            raise LedgerError("Ledger must be closed by its owning thread")
        context, self._context = self._context, None
        self._connection = None
        self._owner = None
        if context is not None:
            context.__exit__(*exc)  # type: ignore[arg-type]

    @property
    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            raise LedgerError("Ledger must be used within its context manager")
        return self._connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        db = self._db
        try:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
        except sqlite3.Error as exc:
            raise LedgerError("Ledger transaction failed") from exc

    def create_task(
        self, url: str, options: Mapping[str, JsonValue], resources: Sequence[ResourceSpec]
    ) -> str:
        manifest = tuple(resources)
        task_id, options_json = task_identity(url, options, manifest)
        now = _now()
        with self._transaction() as db:
            cursor = db.execute(
                "INSERT INTO tasks VALUES (?, ?, ?, 'pending', ?, ?) ON CONFLICT(id) DO NOTHING",
                (task_id, url, options_json, now, now),
            )
            if cursor.rowcount:
                db.executemany(
                    "INSERT INTO resources VALUES (?, ?, ?, ?, ?, 'pending', 0, NULL, NULL, NULL, NULL)",
                    [(task_id, r.chapter, r.page, r.url, r.referer) for r in manifest],
                )
        return task_id

    def snapshot(self, task_id: str) -> TaskSnapshot:
        try:
            row = self._db.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise LedgerError("Unknown ledger task")
            resources = self._db.execute(
                "SELECT * FROM resources WHERE task_id = ? ORDER BY chapter, page", (task_id,)
            ).fetchall()
            return TaskSnapshot(task_id, cast(TaskStatus, row[0]), tuple(map(_record, resources)))
        except sqlite3.Error as exc:
            raise LedgerError("Cannot read ledger task") from exc

    def contains(self, task_id: str) -> bool:
        try:
            return (
                self._db.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
                is not None
            )
        except sqlite3.Error as exc:
            raise LedgerError("Cannot look up ledger task") from exc

    def start(self, task_id: str) -> None:
        with self._transaction() as db:
            changed = db.execute(
                "UPDATE tasks SET status = 'running', updated_at = ? WHERE id = ? AND status = 'pending'",
                (_now(), task_id),
            ).rowcount
            if not changed:
                raise LedgerError("Only a pending task can start; recover it before restarting")

    def _require_downloading(self, db: sqlite3.Connection, key: tuple[str, int, int]) -> None:
        row = db.execute(
            "SELECT r.status, t.status FROM resources r JOIN tasks t ON t.id = r.task_id "
            "WHERE r.task_id = ? AND r.chapter = ? AND r.page = ?",
            key,
        ).fetchone()
        if row is None or tuple(row) != ("downloading", "running"):
            raise LedgerError("Resource must be downloading in a running task")

    def claim(self, task_id: str, chapter: int, page: int) -> ResourceRecord:
        key = (task_id, chapter, page)
        with self._transaction() as db:
            row = db.execute(
                "UPDATE resources SET status = 'downloading', attempts = attempts + 1 "
                "WHERE task_id = ? AND chapter = ? AND page = ? AND status = 'pending' "
                "AND EXISTS (SELECT 1 FROM tasks WHERE id = task_id AND status = 'running') "
                "RETURNING *",
                key,
            ).fetchone()
            if row is None:
                raise LedgerError("Only a pending resource in a running task can be claimed")
            db.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (_now(), task_id))
            return _record(row)

    def complete(self, task_id: str, chapter: int, page: int, relative_path: str) -> None:
        key = (task_id, chapter, page)
        with self._transaction() as db:
            self._require_downloading(db, key)
            if db.execute(
                "SELECT 1 FROM resources WHERE task_id = ? AND local_path = ?",
                (task_id, relative_path),
            ).fetchone():
                raise LedgerError("Cache path is already assigned to another resource")
            stamp = fingerprint(self.root, task_id, relative_path, sync=True)
            db.execute(
                "UPDATE resources SET status = 'done', local_path = ?, sha256 = ?, size = ? "
                "WHERE task_id = ? AND chapter = ? AND page = ?",
                (stamp.path, stamp.sha256, stamp.size, *key),
            )
            db.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (_now(), task_id))

    def fail(self, task_id: str, chapter: int, page: int, code: FailureCode) -> None:
        if code not in {"network", "invalid_image", "blocked", "cancelled"}:
            raise LedgerError("Unknown resource failure code")
        key = (task_id, chapter, page)
        with self._transaction() as db:
            self._require_downloading(db, key)
            db.execute(
                "UPDATE resources SET status = 'failed', error_code = ? "
                "WHERE task_id = ? AND chapter = ? AND page = ?",
                (code, *key),
            )
            db.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (_now(), task_id))

    def finish(self, task_id: str) -> TaskSnapshot:
        with self._transaction() as db:
            snapshot = self.snapshot(task_id)
            if snapshot.status != "running" or any(
                r.status not in {"done", "failed"} for r in snapshot.resources
            ):
                raise LedgerError("Task cannot finish before all resources are settled")
            completed = sum(r.status == "done" for r in snapshot.resources)
            status = (
                "done"
                if completed == len(snapshot.resources)
                else "partial"
                if completed
                else "failed"
            )
            db.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (status, _now(), task_id),
            )
        return self.snapshot(task_id)

    def recover(self, task_id: str) -> TaskSnapshot:
        with self._transaction() as db:
            snapshot = self.snapshot(task_id)
            invalid = []
            for resource in snapshot.resources:
                if self._valid_cache(task_id, resource):
                    continue
                invalid.append((task_id, resource.spec.chapter, resource.spec.page))
            db.executemany(
                "UPDATE resources SET status = 'pending', local_path = NULL, sha256 = NULL, "
                "size = NULL, error_code = NULL WHERE task_id = ? AND chapter = ? AND page = ?",
                invalid,
            )
            db.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                ("pending" if invalid else "done", _now(), task_id),
            )
        return self.snapshot(task_id)

    def _valid_cache(self, task_id: str, resource: ResourceRecord) -> bool:
        if resource.status != "done" or resource.local_path is None:
            return False
        try:
            stamp = fingerprint(
                self.root, task_id, resource.local_path, expected_size=resource.size
            )
        except LedgerError:
            return False
        return (stamp.size, stamp.sha256) == (resource.size, resource.sha256)
