"""Regression tests for advisory progress and terminal job cleanup."""

from __future__ import annotations

import asyncio
import sqlite3
import threading

import pytest

from quire.errors import PausedError
from quire.server import jobs as jobs_mod
from quire.server import progress, salvage
from quire.server.chapter_stream import ChapterTracker
from quire.server.job_state import Job, job_workdir
from quire.server.jobs import JobManager
from tests.test_server_parallel import _result, _settings, _spec, _wait


@pytest.mark.parametrize("cancelled", [False, True])
def test_registration_failure_releases_same_source_queue(tmp_path, monkeypatch, cancelled):
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    async def runner(url, out, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            if cancelled:
                kwargs["on_task"]("task")
                raise asyncio.CancelledError
        return _result(out)

    manager = JobManager(tmp_path, run_manga=runner, run_novel=runner)
    original_register = manager._register

    def register(job, result):
        if calls == 1:
            raise OSError("private/path")
        return original_register(job, result)

    monkeypatch.setattr(manager, "_register", register)
    if cancelled:

        async def salvage_result(*args):
            return _result(tmp_path / "salvage.pdf")

        monkeypatch.setattr(ChapterTracker, "register", lambda *args: None)
        monkeypatch.setattr(salvage, "salvage_job", salvage_result)
    spec = _spec("same-source")
    first = manager.submit(spec, _settings(tmp_path))
    assert entered.wait(5)
    second = manager.submit(spec, _settings(tmp_path))
    assert second.status == "pending"
    release.set()
    assert _wait(first) == "failed"
    assert first.error == "任务失败：OSError"
    assert _wait(second) == "done"
    assert first.id not in manager._active
    assert [event.kind for event in first.events_after(0, 0)].count("failed") == 1


@pytest.mark.parametrize("failure", [None, asyncio.CancelledError, PausedError])
def test_final_sweeps_do_not_replace_capture_outcome(tmp_path, monkeypatch, failure):
    async def runner(url, out, **kwargs):
        if failure:
            raise failure()
        return _result(out)

    def broken_sweep(self):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ChapterTracker, "sweep", broken_sweep)
    manager = JobManager(tmp_path, run_manga=runner, run_novel=runner)
    job = Job("job", _spec("book"))
    if failure:
        with pytest.raises(failure):
            asyncio.run(manager._capture(job, _settings(tmp_path)))
    else:
        result = asyncio.run(manager._capture(job, _settings(tmp_path)))
        assert result.pages_written == 3


def test_final_thumbnail_sweep_does_not_replace_success(tmp_path, monkeypatch):
    async def runner(url, out, **kwargs):
        kwargs["on_task"]("task")
        return _result(out)

    monkeypatch.setattr(ChapterTracker, "register", lambda *args: None)

    def broken_thumbs(*args):
        raise OSError("cannot scan thumbnails")

    monkeypatch.setattr(jobs_mod, "make_thumbs", broken_thumbs)
    manager = JobManager(tmp_path, run_manga=runner, run_novel=runner)
    assert _wait(manager.submit(_spec("book"), _settings(tmp_path))) == "done"


def test_watcher_recovers_after_transient_database_failure(tmp_path, monkeypatch):
    from tests.test_chapter_stream import _job, _make_task, _settle

    task_id = _make_task(tmp_path)
    job = _job()
    tracker = ChapterTracker(job, tmp_path)
    tracker.register(task_id)
    _settle(tmp_path, task_id, 1)
    real_sweep = tracker.sweep
    calls = 0

    def flaky_sweep():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("database is locked")
        real_sweep()

    monkeypatch.setattr(tracker, "sweep", flaky_sweep)

    async def scenario():
        watcher = asyncio.create_task(tracker.watch())
        try:
            async with asyncio.timeout(3):
                while not job.snapshot()["chapters"]:
                    await asyncio.sleep(0.02)
            assert not watcher.done()
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    asyncio.run(scenario())
    assert calls >= 2
    assert len(job.snapshot()["chapters"]) == 1


def test_poll_database_connection_lives_on_worker_and_loop_remains_responsive(
    tmp_path, monkeypatch
):
    job = Job("job", _spec("book"))
    workdir = job_workdir(_settings(tmp_path).output_path, job.spec)
    workdir.mkdir(parents=True)
    db = sqlite3.connect(workdir / "ledger.db")
    db.executescript("""
        CREATE TABLE tasks (id TEXT, updated_at TEXT);
        CREATE TABLE resources (task_id TEXT, status TEXT);
        INSERT INTO tasks VALUES ('task', '2999');
        INSERT INTO resources VALUES ('task', 'done');
    """)
    db.close()
    entered = threading.Event()
    release = threading.Event()
    real_counts = progress.task_counts
    threads = []

    def blocked_counts(connection, started, current):
        threads.append(threading.get_ident())
        entered.set()
        assert release.wait(3)
        return real_counts(connection, started, current)

    monkeypatch.setattr(progress, "task_counts", blocked_counts)

    async def scenario():
        poll = asyncio.create_task(
            progress.poll_job(job, workdir, {"task_id": "", "done": set()}, tmp_path / "thumbs")
        )
        try:
            async with asyncio.timeout(3):
                while not entered.is_set():
                    await asyncio.sleep(0.01)
                # This coroutine must run while the worker is inside the DB query.
                assert threads == [threads[0]] and threads[0] != threading.get_ident()
                release.set()
                while job.done != 1:
                    await asyncio.sleep(0.01)
        finally:
            release.set()
            poll.cancel()
            await asyncio.gather(poll, return_exceptions=True)

    asyncio.run(scenario())
    assert job.done == job.total == 1


def test_thread_start_failure_releases_slot_and_fails_job(tmp_path, monkeypatch):
    """Thread.start 本身失败：槽位释放、任务如实失败，后续任务照常执行。"""

    async def runner(url, out, **kwargs):
        return _result(out)

    manager = JobManager(tmp_path, run_manga=runner, run_novel=runner)
    real_start = threading.Thread.start
    failed_once = False

    def flaky_start(thread):
        nonlocal failed_once
        if not failed_once and thread.name.startswith("quire-"):
            failed_once = True
            raise RuntimeError("can't start new thread")
        real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", flaky_start)
    first = manager.submit(_spec("book"), _settings(tmp_path))
    assert _wait(first) == "failed"
    assert first.error == "任务失败：RuntimeError"
    assert first.id not in manager._active
    assert [event.kind for event in first.events_after(0, 0)].count("failed") == 1
    # 槽位已释放：后续任务正常执行
    assert _wait(manager.submit(_spec("book2"), _settings(tmp_path))) == "done"


def test_chapter_registration_failure_does_not_fail_job(tmp_path, monkeypatch):
    """advisory 章节注册失败只记日志，任务照常完成。"""

    async def runner(url, out, **kwargs):
        kwargs["on_task"]("task")
        return _result(out)

    def broken_register(self, task_id):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ChapterTracker, "register", broken_register)
    manager = JobManager(tmp_path, run_manga=runner, run_novel=runner)
    assert _wait(manager.submit(_spec("book"), _settings(tmp_path))) == "done"
