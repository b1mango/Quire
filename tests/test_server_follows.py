"""追更：follows 表生命周期、跨任务缓存前缀拼接、JobSpec 校验。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from quire.errors import ConfigError, LedgerError
from quire.server.follows import cached_prefix
from quire.server.job_state import JobSpec
from quire.store import library
from quire.store.cache import publish_bytes
from quire.store.ledger import Ledger
from quire.store.models import ResourceSpec, task_identity


def _book(data_root: Path, book_id: str = "b1") -> library.Book:
    target = data_root / f"{book_id}.txt"
    target.write_text("正文", "utf-8")
    return library.add_book(
        data_root,
        book_id,
        title="书",
        kind="novel",
        source_url="http://x.test/book/",
        files=[("txt", target)],
    )


def test_follow_record_lifecycle(tmp_path: Path) -> None:
    book = _book(tmp_path)
    assert library.get_follow(tmp_path, book.id) is None
    library.upsert_follow(
        tmp_path, book.id, capture_mode="catalogue", chapters=3, last_title="第三章 岔路"
    )
    follow = library.get_follow(tmp_path, book.id)
    assert follow is not None and follow.chapters == 3 and follow.update == 0
    assert library.list_follows(tmp_path) == {book.id: follow}

    library.record_check(tmp_path, book.id, remote_count=5, match=True)
    follow = library.get_follow(tmp_path, book.id)
    assert follow is not None and follow.update == 2 and follow.checked_at

    library.record_check(tmp_path, book.id, remote_count=5, match=False)
    follow = library.get_follow(tmp_path, book.id)
    assert follow is not None and follow.update == 0  # 边界对不上时不给 +N

    with pytest.raises(LedgerError):
        library.record_check(tmp_path, "missing-book", remote_count=5, match=True)

    library.delete_book(tmp_path, book.id)
    assert library.get_follow(tmp_path, book.id) is None
    assert not (tmp_path / "b1.txt").exists()


def test_follow_survives_existing_library(tmp_path: Path) -> None:
    """旧版书库（无 follows 表）打开时补建，books 数据不动。"""
    book = _book(tmp_path)
    db = tmp_path / "library.db"
    connection = sqlite3.connect(str(db))
    connection.execute("DROP TABLE follows")
    connection.commit()
    connection.close()
    library.upsert_follow(tmp_path, book.id, capture_mode="auto", chapters=2, last_title="第二章")
    assert library.get_book(tmp_path, book.id).title == "书"
    follow = library.get_follow(tmp_path, book.id)
    assert follow is not None and follow.chapters == 2


def _settle_chapter(root: Path, url: str, chapter: int, title: str, body: str) -> None:
    payload = json.dumps(
        {
            "schema": 2,
            "title": title,
            "url": f"{url}{chapter}.html",
            "pages": 1,
            "truncated": False,
            "paragraphs": [body],
            "source": "html",
            "review": [],
        },
        ensure_ascii=False,
    ).encode("utf-8")
    spec = ResourceSpec(chapter, 1, f"{url}{chapter}.html")
    with Ledger(root) as ledger:
        task_id, _ = task_identity(url, {"book": url, "chapter": chapter}, [spec])
        ledger.create_task(url, {"book": url, "chapter": chapter}, [spec])
        ledger.start(task_id)
        ledger.claim(task_id, chapter, 1)
        relative = publish_bytes(ledger.root, task_id, f"{chapter:05d}.json", payload)
        ledger.complete(task_id, chapter, 1, relative)
        ledger.finish(task_id)


def test_cached_prefix_merges_tasks_and_stops_at_gaps(tmp_path: Path) -> None:
    root = tmp_path / "work"
    url = "http://x.test/book/"
    _settle_chapter(root, url, 1, "第一章", "正文一")
    _settle_chapter(root, url, 3, "第三章", "正文三")
    assert len(cached_prefix(root, url, 3)) == 1  # 第二章缺失，连续前缀只有第一章
    _settle_chapter(root, url, 2, "第二章", "正文二")
    prefix = cached_prefix(root, url, 3)
    assert [c.title for c in prefix] == ["第一章", "第二章", "第三章"]
    assert [c.paragraphs for c in prefix][1] == ("正文二",)
    assert cached_prefix(root, url, 0) == ()
    # 损坏的缓存章：前缀截断而不是带上坏数据
    broken = list((root / "cache").glob("*/00001.json"))
    assert broken
    broken[0].write_bytes(b"corrupted")
    assert len(cached_prefix(root, url, 3)) == 0


def test_jobspec_follow_prefix_validation() -> None:
    base = dict(kind="novel", url="http://x.test/book/", title="书", formats=("txt",))
    JobSpec(**base, chapter_first=4, chapter_last=0, follow_prefix=3)
    with pytest.raises(ConfigError):
        JobSpec(**base, follow_prefix=-1)
    with pytest.raises(ConfigError):
        JobSpec(**base, chapter_first=1, follow_prefix=3)  # 起点必须是已抓数+1
    with pytest.raises(ConfigError):
        JobSpec(**{**base, "kind": "manga", "formats": ("pdf",)}, chapter_first=4, follow_prefix=3)
    with pytest.raises(ConfigError):
        JobSpec(**base, follow_prefix=1.5)


def test_http_toggle_follow_and_static_icons(tmp_path: Path) -> None:
    import http.client
    import threading

    from quire.server.app import make_server

    book = _book(tmp_path)
    server = make_server("127.0.0.1", 0, data_root=tmp_path)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        host, port = server.server_address[:2]
        headers = {"X-Quire-Token": server.token}

        def req(method: str, path: str) -> tuple[int, bytes]:
            conn = http.client.HTTPConnection(str(host), int(port), timeout=5)
            conn.request(method, path, headers=headers)
            resp = conn.getresponse()
            body = resp.read()
            conn.close()
            return resp.status, body

        status, js_bytes = req("GET", "/static/icons.js")
        assert status == 200 and b"LUCIDE" in js_bytes

        # 尚无章节缓存时开启追更应明确报错 400
        status, err_bytes = req("PUT", f"/api/books/{book.id}/follow")
        assert status == 400 and "章节缓存" in json.loads(err_bytes)["error"]

        # 落定连续两章后可开启、查询、再关闭追更
        _settle_chapter(tmp_path, book.source_url, 1, "第一章", "正文一")
        _settle_chapter(tmp_path, book.source_url, 2, "第二章", "正文二")
        status, on_bytes = req("PUT", f"/api/books/{book.id}/follow")
        assert status == 200
        assert json.loads(on_bytes)["follow"] == {"chapters": 2, "update": 0, "changed": False}
        assert library.get_follow(tmp_path, book.id) is not None

        status, off_bytes = req("DELETE", f"/api/books/{book.id}/follow")
        assert status == 200 and json.loads(off_bytes) == {"follow": None}
        assert library.get_follow(tmp_path, book.id) is None

        # 未开启追更时检查更新或提交追更任务应返回 400
        assert req("POST", f"/api/books/{book.id}/check-update")[0] == 400
        assert req("POST", f"/api/books/{book.id}/follow")[0] == 400

        # 非目录小说（无来源地址或漫画）不支持开启追更
        local_file = tmp_path / "local.txt"
        local_file.write_text("正文", "utf-8")
        local_book = library.add_book(
            tmp_path,
            "local1",
            title="本地书",
            kind="novel",
            source_url="",
            files=[("txt", local_file)],
        )
        assert req("PUT", f"/api/books/{local_book.id}/follow")[0] == 400
    finally:
        server.shutdown()
        server.server_close()


def test_follow_edge_cases_and_stream_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from quire.assemble.models import NovelChapter
    from quire.models import ArtifactResult, NovelResult
    from quire.server import follows
    from quire.server.app import make_server

    server = make_server("127.0.0.1", 0, data_root=tmp_path)
    try:
        book = _book(tmp_path, "b2")
        spec = JobSpec(
            kind="novel",
            url=book.source_url,
            title="书",
            formats=("txt",),
            follow_prefix=2,
            chapter_first=3,
        )
        with pytest.raises(ConfigError, match="缓存不完整"):
            follows.resolve_prefix(spec, tmp_path)

        # 单章或非第一章起始的区间不登记追更锚点
        out = tmp_path / "out.txt"
        out.write_text("x", "utf-8")
        res = NovelResult(
            output=out,
            title="书",
            artifacts=(ArtifactResult("txt", out, 1, None),),
            report=out,
            chapters_written=1,
            chapters=(NovelChapter(2, "第二章", ("正文",)),),
        )
        follows.register_follow(
            tmp_path,
            book.id,
            JobSpec(
                kind="novel",
                url=book.source_url,
                title="书",
                formats=("txt",),
                capture_mode="single",
            ),
            res,
        )
        follows.register_follow(
            tmp_path,
            book.id,
            JobSpec(
                kind="novel", url=book.source_url, title="书", formats=("txt",), chapter_first=2
            ),
            res,
        )
        assert library.get_follow(tmp_path, book.id) is None

        # 流式检查在发出阶段帧后出错时，以错误帧收尾；全部检查单本出错不中断
        library.upsert_follow(
            tmp_path, book.id, capture_mode="catalogue", chapters=1, last_title="第一章"
        )

        async def failing_probe(*args: object, on_stage: object = None, **kwargs: object) -> object:
            if callable(on_stage):
                on_stage("fetch")
            raise ConfigError("站点不可达", hint="稍后重试")

        monkeypatch.setattr(follows, "probe_url", failing_probe)
        frames: list[dict[str, object]] = []
        follows.check_update_stream(server, book.id, frames.append)
        assert frames[0] == {"stage": "fetch"} and "error" in frames[-1]

        all_frames: list[dict[str, object]] = []
        follows.check_all_stream(server, all_frames.append)
        assert all_frames[0] == {"total": 1} and all_frames[1]["error"] == "站点不可达"
    finally:
        server.server_close()
