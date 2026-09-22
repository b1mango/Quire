"""书库分组与批量操作的 HTTP 端点：分组 CRUD、批量移动/删除、列表回带归属。"""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from quire.server.app import make_server
from quire.store import library


@pytest.fixture()
def server(tmp_path):
    srv = make_server("127.0.0.1", 0, data_root=tmp_path, opener=lambda p: None)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _request(server, method: str, path: str, body: Any = None):
    host, port = server.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=10)
    payload = json.dumps(body) if body is not None else None
    headers = {"X-Quire-Token": server.token}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    conn.request(method, path, payload, headers)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, json.loads(data) if data else {}


def _add_book(root: Path, book_id: str) -> None:
    target = root / "out" / f"{book_id}.pdf"
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(b"%PDF-fake")
    library.add_book(
        root,
        book_id,
        title=f"书{book_id}",
        kind="manga",
        source_url="https://example.com/c/1",
        files=[("pdf", target)],
    )


def test_groups_crud_flow(server):
    status, group = _request(server, "POST", "/api/groups", {"name": "科幻"})
    assert status == 201 and group["members"] == 0
    status, _ = _request(server, "PUT", f"/api/groups/{group['id']}", {"name": "SF"})
    assert status == 200
    status, data = _request(server, "GET", "/api/books")
    assert status == 200 and data["groups"][0]["name"] == "SF"
    status, _ = _request(server, "DELETE", f"/api/groups/{group['id']}")
    assert status == 200
    status, data = _request(server, "GET", "/api/books")
    assert data["groups"] == []


def test_group_endpoints_validate_input(server):
    status, error = _request(server, "POST", "/api/groups", {"name": "  "})
    assert status == 400 and "分组名" in error["error"]
    status, error = _request(server, "DELETE", "/api/groups/ghost")
    assert status == 400 and "没有这个分组" in error["error"]


def test_batch_move_and_books_group_field(server):
    _add_book(server.data_root, "b1")
    _add_book(server.data_root, "b2")
    _, group = _request(server, "POST", "/api/groups", {"name": "科幻"})
    status, result = _request(
        server,
        "POST",
        "/api/books/batch",
        {"action": "move", "ids": ["b1", "b2"], "group_id": group["id"]},
    )
    assert status == 200 and result["moved"] == 2
    status, data = _request(server, "GET", "/api/books")
    assert {b["id"]: b["group"] for b in data["books"]} == {"b1": group["id"], "b2": group["id"]}
    # 非空分组不可删；移出后可删
    status, error = _request(server, "DELETE", f"/api/groups/{group['id']}")
    assert status == 400 and "移出" in error["error"]
    status, result = _request(
        server,
        "POST",
        "/api/books/batch",
        {"action": "move", "ids": ["b1", "b2"], "group_id": None},
    )
    assert result["moved"] == 2
    _, data = _request(server, "GET", "/api/books")
    assert all(b["group"] is None for b in data["books"])


def test_batch_delete_reports_per_book(server):
    _add_book(server.data_root, "b1")
    _add_book(server.data_root, "b2")
    # b2 的成品被外部改动：删除被逐件复核拒绝，b1 照常删除
    (server.data_root / "out" / "b2.pdf").write_bytes(b"tampered")
    status, result = _request(
        server, "POST", "/api/books/batch", {"action": "delete", "ids": ["b1", "b2"]}
    )
    assert status == 200
    assert result["deleted"] == 1 and len(result["failures"]) == 1
    assert result["failures"][0]["id"] == "b2"
    _, data = _request(server, "GET", "/api/books")
    assert [b["id"] for b in data["books"]] == ["b2"]


def test_batch_rejects_bad_payload(server):
    status, error = _request(server, "POST", "/api/books/batch", {"action": "move", "ids": []})
    assert status == 400 and "没有选中" in error["error"]
    status, error = _request(server, "POST", "/api/books/batch", {"action": "burn", "ids": ["b1"]})
    assert status == 400 and "不支持" in error["error"]
    _add_book(server.data_root, "b1")
    status, error = _request(
        server,
        "POST",
        "/api/books/batch",
        {"action": "move", "ids": ["b1", "ghost"], "group_id": None},
    )
    assert status == 400 and "没有这本书" in error["error"]
