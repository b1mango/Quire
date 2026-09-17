"""取消任务后的半成品导出：账本已落定内容按所选格式出书，不再是纯诊断 JSON。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from quire.server.job_state import JobSpec
from quire.server.salvage import salvage_job
from quire.server.settings import UiSettings
from quire.store.cache import publish_bytes
from quire.store.ledger import Ledger
from quire.store.models import ResourceSpec
from tests.mock_site.server import page_image


def _settings(tmp_path: Path) -> UiSettings:
    return UiSettings(str(tmp_path / "out"))


def _novel_chapter_payload(index: int) -> bytes:
    return json.dumps(
        {
            "schema": 2,
            "title": f"第{index}章 测试",
            "url": f"http://e.c/book/{index}.html",
            "pages": 1,
            "truncated": False,
            "paragraphs": [f"第{index}章正文第一段。", f"第{index}章正文第二段。"],
            "source": "html",
            "review": [],
        },
        ensure_ascii=False,
    ).encode()


def _novel_ledger(workdir: Path, done: int, total: int) -> str:
    specs = [ResourceSpec(i + 1, 1, f"http://e.c/book/{i + 1}.html") for i in range(total)]
    with Ledger(workdir) as ledger:
        task_id = ledger.create_task("http://e.c/book/", {"clean_version": 4}, specs)
        ledger.start(task_id)
        for i in range(done):
            ledger.claim(task_id, i + 1, 1)
            relative = publish_bytes(
                ledger.root, task_id, f"{i + 1:05d}.json", _novel_chapter_payload(i + 1)
            )
            ledger.complete(task_id, i + 1, 1, relative)
    return task_id


def test_salvage_novel_exports_partial_in_chosen_formats(tmp_path):
    workdir = tmp_path / "out" / ".quire-core"
    task_id = _novel_ledger(workdir, done=2, total=4)
    spec = JobSpec(
        kind="novel", url="http://e.c/book/", title="测试书", formats=("txt", "epub"), ocr="never"
    )
    result = asyncio.run(salvage_job(spec, task_id, _settings(tmp_path), tmp_path))
    assert result is not None and result.partial
    assert result.title.endswith("（未完成）")
    assert {a.format for a in result.artifacts} == {"txt", "epub"}
    txt = (tmp_path / "out" / "测试书（未完成）.txt").read_text("utf-8")
    assert "第1章正文第一段" in txt
    assert "第2章正文第二段" in txt
    assert "第3章" not in txt or "缺失" in txt or "未完成" in txt  # 未抓章节只有占位说明
    assert (tmp_path / "out" / "测试书（未完成）.epub").read_bytes()[:2] == b"PK"


def test_salvage_manga_exports_partial_pdf(tmp_path):
    workdir = tmp_path / "out" / ".quire-core"
    specs = [
        ResourceSpec(1, i + 1, f"http://e.c/comic/{i + 1}.jpg", "http://e.c/comic")
        for i in range(3)
    ]
    with Ledger(workdir) as ledger:
        task_id = ledger.create_task("http://e.c/comic", {"selector": None}, specs)
        ledger.start(task_id)
        for i in range(2):
            ledger.claim(task_id, 1, i + 1)
            relative = publish_bytes(
                ledger.root, task_id, f"00001-{i + 1:06d}.jpg", page_image(i + 1)
            )
            ledger.complete(task_id, 1, i + 1, relative)
    spec = JobSpec(kind="manga", url="http://e.c/comic", title="测试漫", formats=("pdf",))
    result = asyncio.run(salvage_job(spec, task_id, _settings(tmp_path), tmp_path))
    assert result is not None and result.partial
    assert result.pages_written == 2 and result.pages_failed == 1
    pdf = tmp_path / "out" / "测试漫（未完成）.pdf"
    assert pdf.read_bytes().startswith(b"%PDF")


def test_salvage_without_settled_content_returns_none(tmp_path):
    workdir = tmp_path / "out" / ".quire-core"
    task_id = _novel_ledger(workdir, done=0, total=2)
    spec = JobSpec(kind="novel", url="http://e.c/book/", title="空书", formats=("txt",))
    assert asyncio.run(salvage_job(spec, task_id, _settings(tmp_path), tmp_path)) is None
    assert not list((tmp_path / "out").glob("*.txt"))  # 没有产出任何成品
