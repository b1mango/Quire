from __future__ import annotations

import asyncio
import http.client
import json
import time
from pathlib import Path
from typing import Any

import pytest

from quire.errors import ConfigError, ParseError
from quire.models import ArtifactResult, MangaResult
from quire.server.app import make_server
from quire.server.jobs import JobManager
from quire.store import library


async def _fake_manga(url: str, out: Path, **kwargs: Any) -> MangaResult:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"%PDF-fake")
    report = out.with_suffix(".report.json")
    report.write_text("{}")
    progress = kwargs.get("progress")
    if progress:
        progress.update(1, 1)
    return MangaResult(
        output=out,
        title="假漫画",
        artifacts=(ArtifactResult("pdf", out, 9, None),),
        report=report,
        pages_written=3,
    )


async def _slow_manga(url: str, out: Path, **kwargs: Any) -> MangaResult:
    await asyncio.sleep(60)
    return _fake_manga(url, out, **kwargs)


async def _failing_manga(url: str, out: Path, **kwargs: Any) -> MangaResult:
    raise ParseError("没找到图片", hint="换一个链接")


def _make(tmp_path: Path, **kwargs: Any):
    opened: list[Path] = []
    revealed: list[Path] = []
    server = make_server(
        "127.0.0.1",
        0,
        data_root=tmp_path,
        opener=opened.append,
        revealer=revealed.append,
        manager=JobManager(
            tmp_path,
            run_manga=kwargs.get("run_manga", _fake_manga),
            run_novel=kwargs.get("run_novel"),
        ),
    )
    import threading

    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, opened, revealed


@pytest.fixture()
def server(tmp_path):
    srv, opened, _ = _make(tmp_path)
    yield srv, opened
    srv.shutdown()
    srv.server_close()


def _conn(server) -> http.client.HTTPConnection:
    host, port = server.server_address[:2]
    return http.client.HTTPConnection(host, port, timeout=10)


def _request(server, method: str, path: str, body: Any = None, headers: dict | None = None):
    conn = _conn(server)
    payload = json.dumps(body) if body is not None else None
    all_headers = {"X-Quire-Token": server.token, **(headers or {})}
    if payload is not None:
        all_headers["Content-Type"] = "application/json"
    conn.request(method, path, payload, all_headers)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, json.loads(data) if data else {}


def _wait_job(server, job_id: str, timeout: float = 10) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _, job = _request(server, "GET", f"/api/jobs/{job_id}")
        if job["status"] not in {"pending", "running"}:
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_non_loopback_refused(tmp_path):
    with pytest.raises(ConfigError):
        make_server("0.0.0.0", 0, data_root=tmp_path)


def test_token_required(server):
    srv, _ = server
    conn = _conn(srv)
    conn.request("GET", "/api/settings")
    response = conn.getresponse()
    assert response.status == 401
    conn.close()
    status, _ = _request(srv, "GET", "/api/settings", headers={"X-Quire-Token": "wrong"})
    assert status == 401


def test_query_token_and_cookie(server):
    srv, _ = server
    conn = _conn(srv)
    conn.request("GET", f"/?token={srv.token}")
    response = conn.getresponse()
    assert response.status == 200
    cookie = response.getheader("Set-Cookie")
    assert "SameSite=Strict" in cookie
    body = response.read()
    assert "卷帙" in body.decode()
    conn.close()
    conn = _conn(srv)
    conn.request("GET", "/api/settings", headers={"Cookie": cookie.split(";")[0]})
    assert conn.getresponse().status == 200
    conn.close()


def test_origin_checked(server):
    srv, _ = server
    status, _ = _request(
        srv, "POST", "/api/probe", {"url": "http://x"}, headers={"Origin": "https://evil.example"}
    )
    assert status == 403
    status, _ = _request(srv, "GET", "/api/settings", headers={"Origin": srv.origin})
    assert status == 200


def test_static_assets(server):
    srv, _ = server
    for name, marker in (("app.css", "--accent"), ("app.js", "quire")):
        conn = _conn(srv)
        conn.request("GET", f"/static/{name}", headers={"X-Quire-Token": srv.token})
        response = conn.getresponse()
        assert response.status == 200
        assert marker in response.read().decode()
        conn.close()
    status, _ = _request(srv, "GET", "/static/../jobs.py")
    assert status in {404, 400}
    status, _ = _request(srv, "GET", "/static/index.html")
    assert status == 404


def test_settings_roundtrip_and_validation(server):
    srv, _ = server
    status, settings = _request(srv, "GET", "/api/settings")
    assert status == 200
    assert settings["compress"] == "balanced"
    new_dir = str(srv.data_root / "books")
    status, updated = _request(
        srv,
        "PUT",
        "/api/settings",
        {"output_dir": new_dir, "concurrency": 2, "theme": "swiss:light"},
    )
    assert status == 200
    assert updated["output_dir"] == new_dir
    assert updated["compress"] == "balanced"  # 未提到的项保持
    assert (srv.data_root / "books").is_dir()
    for bad in (
        {"concurrency": 99},
        {"theme": "dark:dark"},
        {"output_dir": "relative/path"},
        {"unknown_key": 1},
    ):
        status, error = _request(srv, "PUT", "/api/settings", bad)
        assert status == 400, bad
        assert error["error"]
    # 损坏的设置文件：清晰报错，不被覆盖
    srv.settings_path.write_text("{oops")
    status, error = _request(srv, "GET", "/api/settings")
    assert status == 400
    assert srv.settings_path.read_text() == "{oops"


def test_capabilities(server):
    srv, _ = server
    status, caps = _request(srv, "GET", "/api/capabilities")
    assert status == 200
    assert caps["version"]
    assert "chrome" in caps
    assert "tesseract" in caps["ocr"]


def test_job_validation(server):
    srv, _ = server
    for body, expect in (
        ({"kind": "manga", "url": "ftp://x", "title": "t", "formats": ["pdf"]}, 422),
        ({"kind": "alien", "url": "http://x.c", "title": "t", "formats": ["pdf"]}, 400),
        ({"kind": "manga", "url": "http://x.c", "title": "t", "formats": ["exe"]}, 400),
        ({"kind": "manga", "url": "http://x.c", "title": "t", "formats": []}, 400),
        ({"kind": "novel", "url": "http://x.c", "title": "t", "formats": ["cbz"]}, 400),
        ({"kind": "manga", "url": "http://x.c", "title": " ", "formats": ["pdf"]}, 400),
        ({"kind": "manga", "url": "http://u:p@x.c", "title": "t", "formats": ["pdf"]}, 422),
        (
            {
                "kind": "manga",
                "url": "http://x.c",
                "title": "t",
                "formats": ["pdf"],
                "compress": "huge",
            },
            400,
        ),
    ):
        status, error = _request(srv, "POST", "/api/jobs", body)
        assert status == expect, (body, error)


def test_job_conflict_and_unknown(server):
    srv, _ = server
    srv.shutdown()
    srv.server_close()
    srv, _, _ = _make(srv.data_root, run_manga=_slow_manga)
    spec = {"kind": "manga", "url": "http://x.c/1", "title": "t", "formats": ["pdf"]}
    status, job = _request(srv, "POST", "/api/jobs", spec)
    assert status == 201
    status, error = _request(srv, "POST", "/api/jobs", spec)
    assert status == 409
    srv.manager.cancel(job["id"])
    _wait_job(srv, job["id"])
    status, error = _request(srv, "GET", "/api/jobs/ghost")
    assert status == 404
    status, error = _request(srv, "POST", "/api/jobs/ghost/cancel")
    assert status == 404
    srv.shutdown()


def test_job_failure_surfaces_message(server):
    srv, _ = server
    srv.shutdown()
    srv.server_close()
    srv, _, _ = _make(srv.data_root, run_manga=_failing_manga)
    status, job = _request(
        srv,
        "POST",
        "/api/jobs",
        {"kind": "manga", "url": "http://x.c/1", "title": "t", "formats": ["pdf"]},
    )
    assert status == 201
    done = _wait_job(srv, job["id"])
    assert done["status"] == "failed"
    assert done["error"] == "没找到图片"
    assert done["hint"]
    srv.shutdown()


def test_cancel_running_job(server):
    srv, _ = server
    srv.shutdown()
    srv.server_close()
    srv, _, _ = _make(srv.data_root, run_manga=_slow_manga)
    _, job = _request(
        srv,
        "POST",
        "/api/jobs",
        {"kind": "manga", "url": "http://x.c/1", "title": "t", "formats": ["pdf"]},
    )
    time.sleep(0.3)
    status, _ = _request(srv, "POST", f"/api/jobs/{job['id']}/cancel")
    assert status == 200
    done = _wait_job(srv, job["id"])
    assert done["status"] == "cancelled"
    srv.shutdown()


def test_sse_stream_replays_events(server):
    srv, _ = server
    _, job = _request(
        srv,
        "POST",
        "/api/jobs",
        {"kind": "manga", "url": "http://x.c/1", "title": "t", "formats": ["pdf"]},
    )
    _wait_job(srv, job["id"])
    conn = _conn(srv)
    conn.request("GET", f"/api/jobs/{job['id']}/events?after=0&token={srv.token}")
    response = conn.getresponse()
    assert response.status == 200
    raw = response.read().decode()  # 任务已结束，流会关闭
    conn.close()
    assert "event: phase" in raw
    assert "event: done" in raw
    assert "id: 1" in raw


def test_thumb_endpoint(server):
    srv, _ = server
    _, job = _request(
        srv,
        "POST",
        "/api/jobs",
        {"kind": "manga", "url": "http://x.c/1", "title": "t", "formats": ["pdf"]},
    )
    _wait_job(srv, job["id"])
    thumb_dir = srv.manager.thumbs_root / job["id"]
    thumb_dir.mkdir(parents=True)
    (thumb_dir / "p1.jpg").write_bytes(b"jpeg")
    conn = _conn(srv)
    conn.request("GET", f"/api/jobs/{job['id']}/thumbs/1?token={srv.token}")
    response = conn.getresponse()
    assert response.status == 200
    assert response.read() == b"jpeg"
    conn.close()
    status, _ = _request(srv, "GET", f"/api/jobs/{job['id']}/thumbs/99")
    assert status == 404
    status, _ = _request(srv, "GET", f"/api/jobs/{job['id']}/thumbs/x")
    assert status == 404


def test_books_flow(server):
    srv, opened = server
    _, job = _request(
        srv,
        "POST",
        "/api/jobs",
        {"kind": "manga", "url": "http://x.c/1", "title": "t", "formats": ["pdf"]},
    )
    done = _wait_job(srv, job["id"])
    assert done["status"] == "done"
    status, data = _request(srv, "GET", "/api/books")
    assert len(data["books"]) == 1
    book = data["books"][0]
    assert book["title"] == "假漫画"
    assert book["formats"] == ["pdf"]
    status, data = _request(srv, "GET", "/api/books?search=%E6%89%BE%E4%B8%8D%E5%88%B0")
    assert data["books"] == []

    status, result = _request(srv, "POST", f"/api/books/{book['id']}/open")
    assert status == 200
    assert opened and opened[0].name.endswith(".pdf")

    # 成品被改过 → 删除拒绝；恢复后删除成功，文件与封面记录一起清掉
    target = opened[0]
    target.write_bytes(b"tampered")
    status, error = _request(srv, "DELETE", f"/api/books/{book['id']}")
    assert status == 400
    target.write_bytes(b"%PDF-fake")
    status, result = _request(srv, "DELETE", f"/api/books/{book['id']}")
    assert status == 200
    assert not target.exists()
    _, data = _request(srv, "GET", "/api/books")
    assert data["books"] == []


def test_cover_endpoint(server):
    srv, _ = server
    out = srv.data_root / "b.pdf"
    out.write_bytes(b"%PDF")
    book = library.add_book(
        srv.data_root,
        "coverb",
        title="c",
        kind="manga",
        source_url="http://e.c",
        files=[("pdf", out)],
    )
    conn = _conn(srv)
    conn.request("GET", f"/api/books/{book.id}/cover?token={srv.token}")
    assert conn.getresponse().status == 404
    conn.close()
    covers = srv.data_root / "covers"
    covers.mkdir()
    (covers / f"{book.id}.jpg").write_bytes(b"cover-bytes")
    library.set_cover(srv.data_root, book.id, f"covers/{book.id}.jpg")
    conn = _conn(srv)
    conn.request("GET", f"/api/books/{book.id}/cover?token={srv.token}")
    response = conn.getresponse()
    assert response.status == 200
    assert response.read() == b"cover-bytes"
    conn.close()


def test_open_missing_file(server):
    srv, _ = server
    out = srv.data_root / "gone.pdf"
    out.write_bytes(b"%PDF")
    book = library.add_book(
        srv.data_root,
        "gone",
        title="g",
        kind="manga",
        source_url="http://e.c",
        files=[("pdf", out)],
    )
    out.unlink()
    status, error = _request(srv, "POST", f"/api/books/{book.id}/open")
    assert status == 400
    assert error["hint"]


def test_reveal_book_in_finder(tmp_path):
    srv, _, revealed = _make(tmp_path)
    try:
        output = srv.data_root / "library"
        output.mkdir()
        out = output / "书.pdf"
        out.write_bytes(b"%PDF")
        book = library.add_book(
            srv.data_root,
            "reveal-ok",
            title="书",
            kind="manga",
            source_url="http://e.c",
            files=[("pdf", out)],
        )
        status, result = _request(srv, "POST", f"/api/books/{book.id}/reveal")
        assert status == 200
        assert revealed == [out]
        assert result["revealed"] == str(out)

        # 输出目录之外的成品拒绝定位（路径穿越/目录外一律 400）
        outside = srv.data_root / "outside.pdf"
        outside.write_bytes(b"%PDF")
        other = library.add_book(
            srv.data_root,
            "reveal-out",
            title="外部",
            kind="manga",
            source_url="http://e.c",
            files=[("pdf", outside)],
        )
        status, error = _request(srv, "POST", f"/api/books/{other.id}/reveal")
        assert status == 400
        assert "输出目录" in error["error"]
        assert revealed == [out]  # 未调用系统打开

        status, error = _request(srv, "POST", "/api/books/ghost/reveal")
        assert status == 400
        out.unlink()
        status, error = _request(srv, "POST", f"/api/books/{book.id}/reveal")
        assert status == 400
        assert error["hint"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_body_limits(server):
    srv, _ = server
    conn = _conn(srv)
    conn.request(
        "POST",
        "/api/probe",
        "x" * 70_000,
        {"X-Quire-Token": srv.token, "Content-Type": "application/json"},
    )
    response = conn.getresponse()
    assert response.status == 400
    conn.close()
    status, error = _request(srv, "POST", "/api/probe", None)
    assert status == 400  # 缺少链接


def test_malformed_bodies_and_routes(server):
    srv, _ = server
    conn = _conn(srv)
    conn.request(
        "POST",
        "/api/jobs",
        "[1,2]",
        {"X-Quire-Token": srv.token, "Content-Type": "application/json"},
    )
    assert conn.getresponse().status == 400
    conn.close()
    conn = _conn(srv)
    conn.request(
        "PUT",
        "/api/settings",
        '"just-a-string"',
        {"X-Quire-Token": srv.token, "Content-Type": "application/json"},
    )
    assert conn.getresponse().status == 400
    conn.close()
    conn = _conn(srv)
    conn.request("POST", "/api/probe", "{oops", {"X-Quire-Token": srv.token})
    assert conn.getresponse().status == 400
    conn.close()
    status, error = _request(srv, "GET", "/api/nope")
    assert status == 404
    status, _ = _request(srv, "GET", "/api/jobs")
    assert status == 200


def test_job_chapter_range(server):
    srv, _ = server
    base = {"kind": "manga", "url": "http://x.c", "title": "t", "formats": ["pdf"]}
    # 自定义范围与预选卷号互斥（按范围重新分卷）
    status, error = _request(srv, "POST", "/api/jobs", {**base, "volumes": [1], "chapter_first": 2})
    assert status == 400 and "重新分卷" in error["error"]
    status, error = _request(srv, "POST", "/api/jobs", {**base, "volumes": [2], "chapter_last": 9})
    assert status == 400 and "重新分卷" in error["error"]
    # 单章模式不能选范围
    status, error = _request(
        srv, "POST", "/api/jobs", {**base, "capture_mode": "single", "chapter_last": 5}
    )
    assert status == 400 and "单章" in error["error"]
    # 非法范围
    for bad in (
        {"chapter_first": 0},
        {"chapter_last": 20001},
        {"chapter_first": 5, "chapter_last": 2},
    ):
        status, _ = _request(srv, "POST", "/api/jobs", {**base, **bad})
        assert status == 400, bad
    # 合法范围透传到 JobSpec；快照带 chapters 字段
    status, job = _request(
        srv, "POST", "/api/jobs", {**base, "chapter_first": 2, "chapter_last": 9}
    )
    assert status == 201
    spec = srv.manager.get(job["id"]).spec
    assert (spec.chapter_first, spec.chapter_last) == (2, 9)
    done = _wait_job(srv, job["id"])
    assert done["status"] == "done"
    assert done["chapters"] == []


def test_novel_pdf_requires_chrome(server, monkeypatch):
    srv, _ = server
    import quire.server.endpoints as endpoints

    monkeypatch.setattr(endpoints, "find_chrome", lambda: None)
    status, error = _request(
        srv,
        "POST",
        "/api/jobs",
        {"kind": "novel", "url": "http://x.c/1", "title": "t", "formats": ["pdf"]},
    )
    assert status == 400
    assert "Chrome" in error["error"]


def test_capabilities_with_onnx(server, monkeypatch):
    srv, _ = server
    import quire.server.endpoints as endpoints

    monkeypatch.setattr(endpoints, "module_available", lambda name: True)
    status, caps = _request(srv, "GET", "/api/capabilities")
    assert status == 200
    assert caps["ocr"]["onnxruntime"] is True
