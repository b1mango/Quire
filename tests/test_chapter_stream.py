"""ChapterTracker：从账本只读轮询已落定章节，经 Job.record_chapter 发 SSE。"""

from __future__ import annotations

import asyncio
import time

from quire.server.chapter_stream import ChapterTracker
from quire.server.job_state import Job, JobSpec
from quire.store.ledger import Ledger
from quire.store.models import ResourceSpec
from quire.workspace import write_bytes

URL = "https://example.test/book"
TITLES = ("第一章 起点", "第二章 分页", "第三章 岔路")
PAGES = (2, 1, 2)  # 各章页数


def _job() -> Job:
    spec = JobSpec(kind="manga", url=URL, title="测试书", formats=("pdf",))
    return Job("job-1", spec)


def _specs(pages: tuple[int, ...] = PAGES) -> tuple[ResourceSpec, ...]:
    return tuple(
        ResourceSpec(chapter, page, f"{URL}/{chapter}/{page}.jpg", URL)
        for chapter, count in enumerate(pages, 1)
        for page in range(1, count + 1)
    )


def _make_task(root, options: dict | None = None) -> str:
    with Ledger(root) as ledger:
        task_id = ledger.create_task(URL, options or {"chapter_titles": list(TITLES)}, _specs())
        ledger.start(task_id)
    return task_id


def _settle(root, task_id: str, chapter: int, pages=(1, 2), fail: bool = False) -> None:
    with Ledger(root) as ledger:
        for page in pages:
            ledger.claim(task_id, chapter, page)
            if fail:
                ledger.fail(task_id, chapter, page, "network")
            else:
                relative = f"cache/{task_id}/{chapter}-{page}.jpg"
                write_bytes(root / relative, b"image")
                ledger.complete(task_id, chapter, page, relative)


def test_register_reports_settled_chapters_as_reused(tmp_path):
    task_id = _make_task(tmp_path)
    _settle(tmp_path, task_id, 1, pages=(1, 2))
    job = _job()
    tracker = ChapterTracker(job, tmp_path)
    tracker.register(task_id)
    chapters = job.snapshot()["chapters"]
    assert len(chapters) == 1
    chapter = chapters[0]
    assert chapter["id"] == f"{task_id}:1"
    assert chapter["title"] == "第一章 起点"
    assert chapter["status"] == "done"
    assert (chapter["pages"], chapter["failed_pages"]) == (2, 0)
    assert chapter["reused"] is True
    # 重复注册不重复上报
    tracker.register(task_id)
    assert len(job.snapshot()["chapters"]) == 1


def test_sweep_reports_chapters_settled_after_register(tmp_path):
    task_id = _make_task(tmp_path)
    job = _job()
    tracker = ChapterTracker(job, tmp_path)
    tracker.register(task_id)
    assert job.snapshot()["chapters"] == []
    _settle(tmp_path, task_id, 2, pages=(1,))
    tracker.sweep()
    chapters = job.snapshot()["chapters"]
    assert [c["title"] for c in chapters] == ["第二章 分页"]
    assert chapters[0]["reused"] is False
    # 重复 sweep 不重复上报
    tracker.sweep()
    assert len(job.snapshot()["chapters"]) == 1


def test_partial_chapter_waits_for_all_pages(tmp_path):
    task_id = _make_task(tmp_path)
    job = _job()
    tracker = ChapterTracker(job, tmp_path)
    tracker.register(task_id)
    _settle(tmp_path, task_id, 1, pages=(1,))  # 第一章共 2 页，只落定 1 页
    tracker.sweep()
    assert job.snapshot()["chapters"] == []
    _settle(tmp_path, task_id, 1, pages=(2,))
    tracker.sweep()
    assert [c["title"] for c in job.snapshot()["chapters"]] == ["第一章 起点"]


def test_failed_chapter_reported_with_page_counts(tmp_path):
    task_id = _make_task(tmp_path)
    _settle(tmp_path, task_id, 3, pages=(1, 2), fail=True)
    job = _job()
    tracker = ChapterTracker(job, tmp_path)
    tracker.register(task_id)
    chapter = job.snapshot()["chapters"][0]
    assert chapter["status"] == "failed"
    assert (chapter["pages"], chapter["failed_pages"]) == (2, 2)


def test_novel_style_options_and_title_fallback(tmp_path):
    # 小说账本把标题放在 chapters[0]；标题不够时回退到任务书名
    task_id = _make_task(tmp_path, options={"chapters": [["唯一章", f"{URL}/1"]]})
    _settle(tmp_path, task_id, 2, pages=(1,))
    job = _job()
    tracker = ChapterTracker(job, tmp_path)
    tracker.register(task_id)
    titles = [c["title"] for c in job.snapshot()["chapters"]]
    assert titles == ["测试书"]


def test_watch_picks_up_settled_chapters(tmp_path):
    task_id = _make_task(tmp_path, options=None)
    job = _job()
    tracker = ChapterTracker(job, tmp_path)
    tracker.register(task_id)

    async def scenario() -> None:
        watcher = asyncio.create_task(tracker.watch())
        try:
            await asyncio.sleep(0.05)
            _settle(tmp_path, task_id, 1, pages=(1, 2))
            deadline = time.monotonic() + 5
            while not job.snapshot()["chapters"] and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)

    asyncio.run(scenario())
    assert [c["title"] for c in job.snapshot()["chapters"]] == ["第一章 起点"]
    assert tracker.tasks[task_id].complete is False  # 仍有未落定章节
    _settle(tmp_path, task_id, 2, pages=(1,))
    _settle(tmp_path, task_id, 3, pages=(1, 2))
    tracker.sweep()
    assert tracker.tasks[task_id].complete is True
    assert len(job.snapshot()["chapters"]) == 3
