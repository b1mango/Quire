"""双槽并行调度：并行完成、同站速率共享、渲染互斥、取消排队任务。"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

from quire.fetch.async_policy import AsyncRateLimiter, HostPace
from quire.models import ArtifactResult, MangaResult
from quire.server.job_state import Job, JobSpec
from quire.server.jobs import MAX_PARALLEL, JobManager
from quire.server.settings import UiSettings


def _spec(title: str, *, render: bool = False) -> JobSpec:
    return JobSpec(
        kind="manga", url=f"http://x.c/{title}", title=title, formats=("pdf",), render=render
    )


def _settings(tmp_path: Path) -> UiSettings:
    return UiSettings(str(tmp_path / "out"))


def _result(out: Path) -> MangaResult:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"%PDF-fake")
    report = out.with_suffix(".report.json")
    report.write_text("{}")
    return MangaResult(
        output=out,
        title="假漫画",
        artifacts=(ArtifactResult("pdf", out, 9, None),),
        report=report,
        pages_written=3,
    )


def _wait(job: Job, timeout: float = 10) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if job.status not in {"pending", "running"}:
            return job.status
        time.sleep(0.02)
    raise AssertionError(f"job {job.id} did not finish")


def _wait_started(count: int, probe: list[str], timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and len(probe) < count:
        time.sleep(0.02)
    assert len(probe) == count


def test_two_jobs_parallel_third_queued(tmp_path):
    release = threading.Event()
    started: list[str] = []
    peak = 0
    running = 0
    lock = threading.Lock()

    async def runner(url: str, out: Path, **kwargs: Any) -> MangaResult:
        nonlocal peak, running
        with lock:
            started.append(out.stem)
            running += 1
            peak = max(peak, running)
        try:
            while not release.is_set():
                await asyncio.sleep(0.01)
            return _result(out)
        finally:
            with lock:
                running -= 1

    manager = JobManager(tmp_path, run_manga=runner, run_novel=runner)
    jobs = [manager.submit(_spec(f"t{i}"), _settings(tmp_path)) for i in range(3)]
    _wait_started(2, started)
    assert jobs[2].status == "pending"  # 两槽占满，第三个排队
    release.set()
    assert [_wait(job) for job in jobs] == ["done"] * 3
    assert peak == MAX_PARALLEL
    assert len(started) == 3


def test_render_jobs_serialize(tmp_path):
    intervals: list[tuple[float, float]] = []
    lock = threading.Lock()

    async def runner(url: str, out: Path, **kwargs: Any) -> MangaResult:
        start = time.monotonic()
        await asyncio.sleep(0.15)
        with lock:
            intervals.append((start, time.monotonic()))
        return _result(out)

    manager = JobManager(tmp_path, run_manga=runner, run_novel=runner)
    jobs = [
        manager.submit(_spec("a", render=True), _settings(tmp_path)),
        manager.submit(_spec("b", render=True), _settings(tmp_path)),
    ]
    assert [_wait(job) for job in jobs] == ["done", "done"]
    (first_start, first_end), (second_start, _) = sorted(intervals)
    assert first_end <= second_start  # 渲染互斥：执行区间不重叠


def test_cancel_queued_job_never_starts(tmp_path):
    release = threading.Event()
    started: list[str] = []

    async def runner(url: str, out: Path, **kwargs: Any) -> MangaResult:
        started.append(out.stem)
        while not release.is_set():
            await asyncio.sleep(0.01)
        return _result(out)

    manager = JobManager(tmp_path, run_manga=runner, run_novel=runner)
    first = manager.submit(_spec("t1"), _settings(tmp_path))
    second = manager.submit(_spec("t2"), _settings(tmp_path))
    third = manager.submit(_spec("t3"), _settings(tmp_path))
    _wait_started(2, started)
    manager.cancel(third.id)
    assert third.status == "cancelled"
    release.set()
    assert _wait(first) == "done" and _wait(second) == "done"
    assert sorted(started) == ["t1", "t2"]  # 排队任务被取消后没有启动


def test_same_source_jobs_serialize_on_shared_ledger(tmp_path):
    """同一来源 URL 共用一本账本（单写者）：后提交的同源任务排队等它完成。"""
    order: list[str] = []
    lock = threading.Lock()

    async def runner(url: str, out: Path, **kwargs: Any) -> MangaResult:
        with lock:
            order.append(f"start:{out.stem}")
        await asyncio.sleep(0.1)
        with lock:
            order.append(f"end:{out.stem}")
        return _result(out)

    manager = JobManager(tmp_path, run_manga=runner, run_novel=runner)
    settings = _settings(tmp_path)
    first = manager.submit(
        JobSpec(kind="manga", url="http://x.c/book", title="a", formats=("pdf",)), settings
    )
    second = manager.submit(
        JobSpec(kind="manga", url="http://x.c/book", title="b", formats=("pdf",)), settings
    )
    assert _wait(first) == "done" and _wait(second) == "done"
    assert order == ["start:a", "end:a", "start:b", "end:b"]


def test_shared_pace_caps_combined_rate():
    pace = HostPace()

    async def run() -> float:
        limiter_a = AsyncRateLimiter(10, pace)
        limiter_b = AsyncRateLimiter(10, pace)
        slots = asyncio.Semaphore(8)

        async def hit(limiter: AsyncRateLimiter) -> None:
            async with limiter.admit("https://shared.test/page", slots, 10):
                pass

        start = time.monotonic()
        await asyncio.gather(*[hit(limiter_a if i % 2 else limiter_b) for i in range(6)])
        return time.monotonic() - start

    # 两个限速器共享一个 pace：6 次许可共占 6 个 0.1s 窗口，合计 ≤ 10 次/秒
    assert asyncio.run(run()) >= 0.45


def test_private_pace_is_per_limiter():
    async def run() -> float:
        limiter_a = AsyncRateLimiter(10)
        limiter_b = AsyncRateLimiter(10)
        slots = asyncio.Semaphore(8)

        async def hit(limiter: AsyncRateLimiter) -> None:
            async with limiter.admit("https://shared.test/page", slots, 10):
                pass

        start = time.monotonic()
        await asyncio.gather(*[hit(limiter_a if i % 2 else limiter_b) for i in range(6)])
        return time.monotonic() - start

    # 各自独立的 pace：每个限速器只排 3 个窗口
    assert asyncio.run(run()) < 0.45
