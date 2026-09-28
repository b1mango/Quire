"""M6 端到端：真实 LocalServer + 真实 core 管线 + 本地 mock 站。

走通「贴 URL → 任务进度 → 成品 → 书库可见/可打开 → 删除」全流程，
以及漫画缺页的部分成功（退出码 4 语义：黄色提示，不是红色错误）。
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from quire.server.app import make_server
from tests.mock_site.novel_server import CHAPTER_PAGES, CHAPTER_TITLES, novel_site
from tests.mock_site.server import serve


@pytest.fixture()
def ui(tmp_path):
    opened: list[Path] = []
    server = make_server("127.0.0.1", 0, data_root=tmp_path, opener=opened.append)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server, opened
    server.shutdown()
    server.server_close()


def _request(server, method: str, path: str, body: Any = None):
    host, port = server.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=15)
    payload = json.dumps(body) if body is not None else None
    headers = {"X-Quire-Token": server.token}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    conn.request(method, path, payload, headers)
    response = conn.getresponse()
    data = response.read()
    content_type = response.getheader("Content-Type") or ""
    conn.close()
    if content_type.startswith("application/x-ndjson"):
        # 流式识别：阶段帧 + 收尾 result/error 帧，收敛为单次响应语义
        frames = [json.loads(line) for line in data.decode().splitlines() if line.strip()]
        result = next((frame["result"] for frame in frames if "result" in frame), None)
        failure = next((frame for frame in frames if "error" in frame), None)
        if result is not None:
            return response.status, result
        if failure is not None:
            return response.status, {"error": failure["error"], "hint": failure.get("hint")}
        return response.status, {}
    return response.status, json.loads(data) if data else {}


def _request_frames(server, method: str, path: str, body: Any = None) -> tuple[int, list[dict]]:
    """与 _request 同路，但返回全部 NDJSON 帧（断流式识别的阶段文案用）。"""
    host, port = server.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=15)
    payload = json.dumps(body) if body is not None else None
    headers = {"X-Quire-Token": server.token}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    conn.request(method, path, payload, headers)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    if not (response.getheader("Content-Type") or "").startswith("application/x-ndjson"):
        return response.status, [json.loads(data)] if data else []
    return response.status, [
        json.loads(line) for line in data.decode().splitlines() if line.strip()
    ]


def _wait_job(server, job_id: str, timeout: float = 60) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _, job = _request(server, "GET", f"/api/jobs/{job_id}")
        if job["status"] not in {"pending", "running"}:
            return job
        time.sleep(0.1)
    raise AssertionError("job did not finish in time")


def _sse_events(server, job_id: str) -> str:
    host, port = server.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=15)
    conn.request("GET", f"/api/jobs/{job_id}/events?after=0&token={server.token}")
    response = conn.getresponse()
    assert response.status == 200
    raw = response.read().decode()
    conn.close()
    return raw


def test_manga_full_flow(ui):
    server, opened = ui
    with serve() as site:
        status, probe = _request(server, "POST", "/api/probe", {"url": f"{site.url}/comic"})
        assert status == 200
        assert probe["kind"] == "manga"
        assert probe["count"] == 3
        assert probe["title"]

        status, job = _request(
            server,
            "POST",
            "/api/jobs",
            {
                "kind": "manga",
                "url": probe["url"],
                "title": probe["title"],
                "formats": ["pdf"],
                "compress": "balanced",
            },
        )
        assert status == 201
        done = _wait_job(server, job["id"])
        assert done["status"] == "done"

        raw = _sse_events(server, job["id"])
        assert "event: progress" in raw
        assert "event: thumb" in raw
        assert "event: done" in raw

        _, data = _request(server, "GET", "/api/books")
        assert len(data["books"]) == 1
        book = data["books"][0]
        assert book["formats"] == ["pdf"]
        assert book["cover"]

        output = server.data_root / "library" / probe["title"] / f"{probe['title']}.pdf"
        assert output.read_bytes().startswith(b"%PDF")

        # 封面（由下载页缩略图生成）与打开
        host, port = server.server_address[:2]
        conn = http.client.HTTPConnection(host, port, timeout=10)
        conn.request("GET", f"/api/books/{book['id']}/cover?token={server.token}")
        response = conn.getresponse()
        assert response.status == 200
        assert len(response.read()) > 100
        conn.close()
        status, _ = _request(server, "POST", f"/api/books/{book['id']}/open")
        assert status == 200
        assert opened == [output]

        status, result = _request(server, "DELETE", f"/api/books/{book['id']}")
        assert status == 200
        assert not output.exists()
        _, data = _request(server, "GET", "/api/books")
        assert data["books"] == []


def test_manga_partial_is_warning_not_error(ui):
    server, _ = ui
    with serve() as site:
        status, job = _request(
            server,
            "POST",
            "/api/jobs",
            {
                "kind": "manga",
                "url": f"{site.url}/partial",
                "title": "缺页书",
                "formats": ["pdf"],
            },
        )
        assert status == 201
        done = _wait_job(server, job["id"])
        # 部分成功：状态 partial（黄色提示），不是 failed；成品仍进书库
        assert done["status"] == "partial"
        assert done["failed_pages"] == 1
        raw = _sse_events(server, job["id"])
        assert '"partial": true' in raw
        _, data = _request(server, "GET", "/api/books")
        assert len(data["books"]) == 1


def test_novel_full_flow(ui):
    server, opened = ui
    with novel_site() as site:
        status, probe = _request(server, "POST", "/api/probe", {"url": f"{site.url}/book/"})
        assert status == 200
        assert probe["kind"] == "novel"
        assert probe["count"] == 5

        status, job = _request(
            server,
            "POST",
            "/api/jobs",
            {
                "kind": "novel",
                "url": probe["url"],
                "title": probe["title"],
                "formats": ["epub", "txt"],
                "ocr": "never",
            },
        )
        assert status == 201
        done = _wait_job(server, job["id"])
        # 第 4 章 404：部分成功，成品可用
        assert done["status"] == "partial"

        _, data = _request(server, "GET", "/api/books")
        assert len(data["books"]) == 1
        book = data["books"][0]
        assert book["formats"] == ["epub", "txt"]
        epub = server.data_root / "library" / probe["title"] / f"{probe['title']}.epub"
        txt = server.data_root / "library" / probe["title"] / f"{probe['title']}.txt"
        assert epub.read_bytes()[:2] == b"PK"
        content = txt.read_text("utf-8")
        assert "第一章" in content
        assert "抓取失败" in content or "缺失" in content

        status, _ = _request(server, "POST", f"/api/books/{book['id']}/open")
        assert status == 200
        assert opened == [epub]


def test_novel_chapter_range_flow(ui):
    """选目录范围的端到端：probe 返回章节标题，快照与 SSE 带已落定章节。"""
    server, _ = ui
    with novel_site() as site:
        status, probe = _request(server, "POST", "/api/probe", {"url": f"{site.url}/book/"})
        assert status == 200
        assert [c["index"] for c in probe["chapters"]] == [1, 2, 3, 4, 5]
        assert probe["chapters"][0]["title"] == "第一章 起点"

        status, job = _request(
            server,
            "POST",
            "/api/jobs",
            {
                "kind": "novel",
                "url": probe["url"],
                "title": probe["title"],
                "formats": ["txt"],
                "ocr": "never",
                "chapter_first": 2,
                "chapter_last": 3,
            },
        )
        assert status == 201
        done = _wait_job(server, job["id"])
        assert done["status"] == "done"  # 第 2、3 章都正常，不含 404 的第 4 章
        chapters = done["chapters"]
        assert sorted(c["title"] for c in chapters) == ["第三章 岔路", "第二章 分页"]
        assert all(c["status"] == "done" and not c["reused"] for c in chapters)

        raw = _sse_events(server, job["id"])
        assert "event: chapter" in raw

        txt = (server.data_root / "library" / probe["title"] / f"{probe['title']}.txt").read_text(
            "utf-8"
        )
        assert "第2章第1页第1段" in txt
        assert "第1章第1页第1段" not in txt


def test_novel_multi_range_flow(ui):
    """多段范围表达式端到端：1-2,5 只抓第 1、2、5 章，跳过 404 的第 4 章。"""
    server, _ = ui
    with novel_site() as site:
        status, probe = _request(server, "POST", "/api/probe", {"url": f"{site.url}/book/"})
        assert status == 200
        status, job = _request(
            server,
            "POST",
            "/api/jobs",
            {
                "kind": "novel",
                "url": probe["url"],
                "title": probe["title"],
                "formats": ["txt"],
                "ocr": "never",
                "chapter_ranges": "1-2,5",
            },
        )
        assert status == 201
        done = _wait_job(server, job["id"])
        assert done["status"] == "done"
        chapters = done["chapters"]
        assert sorted(c["title"] for c in chapters) == ["第一章 起点", "第二章 分页", "第五章 尾声"]
        assert site.hits("/book/3.html") == 0
        assert site.hits("/book/4.html") == 0
        txt = (server.data_root / "library" / probe["title"] / f"{probe['title']}.txt").read_text(
            "utf-8"
        )
        assert "第1章第1页第1段" in txt
        assert "第5章第1页第1段" in txt
        assert "第3章第1页第1段" not in txt


def test_cancel_exports_partial_book(ui):
    """取消抓取后：已落定章节按所选格式导出半成品，书库可见，状态 partial。"""
    server, _ = ui
    with novel_site() as site:
        for path in (
            "/book/2.html",
            "/book/2_2.html",
            "/book/2_3.html",
            "/book/3.html",
            "/book/5.html",
        ):
            site.delays[path] = 5
        status, job = _request(
            server,
            "POST",
            "/api/jobs",
            {
                "kind": "novel",
                "url": f"{site.url}/book/",
                "title": "测试之书",
                "formats": ["txt"],
                "ocr": "never",
            },
        )
        assert status == 201
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            _, snap = _request(server, "GET", f"/api/jobs/{job['id']}")
            if any(c["status"] == "done" for c in snap["chapters"]):
                break
            time.sleep(0.1)
        else:
            raise AssertionError("没有章节落定")
        status, _ = _request(server, "POST", f"/api/jobs/{job['id']}/cancel")
        assert status == 200
        done = _wait_job(server, job["id"])
        assert done["status"] == "partial"
        assert done["book_id"]

        _, data = _request(server, "GET", "/api/books")
        assert len(data["books"]) == 1
        assert "（未完成）" in data["books"][0]["title"]
        txt = (
            server.data_root / "library" / "测试之书（未完成）" / "测试之书（未完成）.txt"
        ).read_text("utf-8")
        assert "第1章第1页第1段" in txt  # 已抓章节在成品里
        assert "第2章第1页第1段" not in txt  # 未抓章节不伪造正文


def test_pause_and_resume(ui):
    """暂停优雅收尾 → paused；按快照 spec 继续 → 已落定章节不重复下载。"""
    server, _ = ui
    with novel_site() as site:
        for path in ("/book/3.html", "/book/5.html"):
            site.delays[path] = 5
        status, job = _request(
            server,
            "POST",
            "/api/jobs",
            {
                "kind": "novel",
                "url": f"{site.url}/book/",
                "title": "测试之书",
                "formats": ["txt"],
                "ocr": "never",
            },
        )
        assert status == 201
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            _, snap = _request(server, "GET", f"/api/jobs/{job['id']}")
            if any(c["status"] == "done" for c in snap["chapters"]):
                break
            time.sleep(0.1)
        else:
            raise AssertionError("没有章节落定")
        status, _ = _request(server, "POST", f"/api/jobs/{job['id']}/pause")
        assert status == 200
        done = _wait_job(server, job["id"])
        assert done["status"] == "paused"
        assert "event: paused" in _sse_events(server, job["id"])

        status, resumed = _request(server, "POST", "/api/jobs", done["spec"])
        assert status == 201
        final = _wait_job(server, resumed["id"])
        assert final["status"] == "partial"  # 第 4 章 404 仍然缺
        assert site.hits("/book/1.html") == 1  # 已落定章节不重复下载
        assert site.hits("/book/2.html") == 1
        txt = (server.data_root / "library" / "测试之书" / "测试之书.txt").read_text("utf-8")
        assert "第1章第1页第1段" in txt


def test_follow_update_flow(ui):
    """追更全链路：3 章成书 → 目录 +2 → 检查更新 → 追更 → 整本 5 章，旧章不重抓。"""
    server, _ = ui
    with novel_site() as site:
        site.titles = {number: CHAPTER_TITLES[number] for number in (1, 2, 3)}
        site.pages = {number: CHAPTER_PAGES[number] for number in (1, 2, 3)}
        site.missing = set()
        status, probe = _request(server, "POST", "/api/probe", {"url": f"{site.url}/book/"})
        assert status == 200 and probe["count"] == 3
        status, job = _request(
            server,
            "POST",
            "/api/jobs",
            {
                "kind": "novel",
                "url": probe["url"],
                "title": probe["title"],
                "formats": ["txt"],
                "ocr": "never",
            },
        )
        assert status == 201
        done = _wait_job(server, job["id"])
        assert done["status"] == "done"
        _, data = _request(server, "GET", "/api/books")
        book = data["books"][0]
        assert book["follow"] == {"chapters": 3, "update": 0, "changed": False}

        site.titles.update({4: "第四章 新生", 5: "第五章 新尾声"})
        site.pages.update({4: 1, 5: 1})
        status, check = _request(server, "POST", f"/api/books/{book['id']}/check-update")
        assert status == 200
        assert check["update"] == 2 and not check["changed"]
        _, data = _request(server, "GET", "/api/books")
        assert data["books"][0]["follow"]["update"] == 2  # 书库角标

        status, follow_job = _request(server, "POST", f"/api/books/{book['id']}/follow")
        assert status == 201
        done = _wait_job(server, follow_job["id"])
        assert done["status"] == "done"
        assert site.hits("/book/1.html") == 1  # 旧章节只抓过一次
        assert site.hits("/book/2_3.html") == 1
        assert site.hits("/book/4.html") == 1 and site.hits("/book/5.html") == 1

        _, data = _request(server, "GET", "/api/books")
        assert len(data["books"]) == 2
        newest = data["books"][0]  # created_at 倒序，追更产物在前
        assert newest["follow"]["chapters"] == 5
        txts = sorted((server.data_root / "library").rglob("*.txt"))
        merged = [p for p in txts if "第5章第1页第1段" in p.read_text("utf-8")]
        assert len(merged) == 1
        assert "第1章第1页第1段" in merged[0].read_text("utf-8")

        status, check = _request(server, "POST", f"/api/books/{newest['id']}/check-update")
        assert status == 200 and check["update"] == 0
        status, error = _request(server, "POST", f"/api/books/{newest['id']}/follow")
        assert status == 400 and "没有新章节" in error["error"]


def test_follow_rejected_when_catalogue_changed(ui):
    """末章标题指纹对不上：只报目录变动，不按序号续抓。"""
    server, _ = ui
    with novel_site() as site:
        site.titles = {number: CHAPTER_TITLES[number] for number in (1, 2, 3)}
        site.pages = {number: CHAPTER_PAGES[number] for number in (1, 2, 3)}
        site.missing = set()
        status, job = _request(
            server,
            "POST",
            "/api/jobs",
            {
                "kind": "novel",
                "url": f"{site.url}/book/",
                "title": "测试之书",
                "formats": ["txt"],
                "ocr": "never",
            },
        )
        assert status == 201
        assert _wait_job(server, job["id"])["status"] == "done"
        _, data = _request(server, "GET", "/api/books")
        book = data["books"][0]

        site.titles[3] = "第三章 改名换姓"
        site.titles[4] = "第四章 新生"
        site.pages[4] = 1
        status, check = _request(server, "POST", f"/api/books/{book['id']}/check-update")
        assert status == 200
        assert check["changed"] is True and check["update"] == 0
        status, error = _request(server, "POST", f"/api/books/{book['id']}/follow")
        assert status == 400 and "续抓" in error["error"]


def test_check_all_streams_progress_frames(ui):
    """全部检查：首帧总数、逐本进度帧、收尾完整结果。"""
    server, _ = ui
    with novel_site() as site:
        site.titles = {number: CHAPTER_TITLES[number] for number in (1, 2, 3)}
        site.pages = {number: CHAPTER_PAGES[number] for number in (1, 2, 3)}
        site.missing = set()
        status, job = _request(
            server,
            "POST",
            "/api/jobs",
            {
                "kind": "novel",
                "url": f"{site.url}/book/",
                "title": "测试之书",
                "formats": ["txt"],
                "ocr": "never",
            },
        )
        assert status == 201
        assert _wait_job(server, job["id"])["status"] == "done"
        status, frames = _request_frames(server, "POST", "/api/books/check-all-updates")
        assert status == 200
        assert frames[0] == {"total": 1}
        progress = [frame for frame in frames if frame.get("done")]
        assert progress == [
            {
                "done": 1,
                "total": 1,
                "title": "测试之书",
                "update": 0,
                "error": None,
            }
        ]
        assert frames[-1]["result"]["results"][0]["chapters"] == 3


def test_probe_reports_library_progress(ui):
    """识别命中书库来源的链接时返回库内进度（续抓标注已在库章节）；未命中为 None。"""
    server, _ = ui
    with novel_site() as site:
        site.titles = {number: CHAPTER_TITLES[number] for number in (1, 2, 3)}
        site.pages = {number: CHAPTER_PAGES[number] for number in (1, 2, 3)}
        site.missing = set()
        status, job = _request(
            server,
            "POST",
            "/api/jobs",
            {
                "kind": "novel",
                "url": f"{site.url}/book/",
                "title": "测试之书",
                "formats": ["txt"],
                "ocr": "never",
            },
        )
        assert status == 201
        assert _wait_job(server, job["id"])["status"] == "done"
        _, data = _request(server, "GET", "/api/books")
        book = data["books"][0]

        status, probe = _request(server, "POST", "/api/probe", {"url": f"{site.url}/book/"})
        assert status == 200
        assert probe["library"] == {
            "book_id": book["id"],
            "title": "测试之书",
            "chapters": 3,
        }

        with serve() as comic:
            status, probe = _request(server, "POST", "/api/probe", {"url": f"{comic.url}/comic"})
            assert status == 200
            assert probe["library"] is None


def test_probe_rejects_page_without_content(ui):
    server, _ = ui
    with novel_site() as site:
        status, error = _request(server, "POST", "/api/probe", {"url": f"{site.url}/rank"})
        # 识别中途的失败以 NDJSON 错误帧收尾，响应行已是 200（流式语义）
        assert status == 200
        assert "章节列表" in error["error"] or "图片" in error["error"]


def test_probe_streams_stage_frames(ui):
    server, _ = ui
    with novel_site() as site:
        status, frames = _request_frames(server, "POST", "/api/probe", {"url": f"{site.url}/book/"})
        assert status == 200
        stages = [frame["stage"] for frame in frames if "stage" in frame]
        assert stages[:2] == ["fetch", "parse"]
        assert "result" in frames[-1]
        assert frames[-1]["result"]["count"] > 0


def test_probe_pre_stream_error_keeps_status(ui):
    server, _ = ui
    status, error = _request(server, "POST", "/api/probe", {"url": "not-a-url"})
    assert status == 422
    assert "error" in error
