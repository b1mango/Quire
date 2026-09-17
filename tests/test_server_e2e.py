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
from tests.mock_site.novel_server import novel_site
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
    conn.close()
    return response.status, json.loads(data) if data else {}


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
                "target_mb": 50,
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

        output = server.data_root / "library" / f"{probe['title']}.pdf"
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
        epub = server.data_root / "library" / f"{probe['title']}.epub"
        txt = server.data_root / "library" / f"{probe['title']}.txt"
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

        txt = (server.data_root / "library" / f"{probe['title']}.txt").read_text("utf-8")
        assert "第2章第1页第1段" in txt
        assert "第1章第1页第1段" not in txt


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
        txt = (server.data_root / "library" / "测试之书（未完成）.txt").read_text("utf-8")
        assert "第1章第1页第1段" in txt  # 已抓章节在成品里
        assert "第2章第1页第1段" not in txt  # 未抓章节不伪造正文


def test_probe_rejects_page_without_content(ui):
    server, _ = ui
    with novel_site() as site:
        status, error = _request(server, "POST", "/api/probe", {"url": f"{site.url}/rank"})
        assert status == 422
        assert "章节列表" in error["error"] or "图片" in error["error"]
