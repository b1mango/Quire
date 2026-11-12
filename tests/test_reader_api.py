from __future__ import annotations

import http.client
import json
import threading
from zipfile import ZipFile

import pytest

from quire.assemble.epub import EpubChapter, EpubWriter
from quire.server.app import make_server
from quire.store import library


@pytest.fixture
def server(tmp_path):
    srv = make_server("127.0.0.1", 0, data_root=tmp_path, opener=lambda p: None)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def request(server, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection(*server.server_address[:2], timeout=10)
    payload = json.dumps(body) if body is not None else None
    conn.request(method, path, payload, {"X-Quire-Token": server.token, **(headers or {})})
    response = conn.getresponse()
    data = response.read()
    result = response.status, dict(response.getheaders()), data
    conn.close()
    return result


def add(server, book_id, files):
    library.add_book(
        server.data_root,
        book_id,
        title="测试书",
        author="作者",
        kind="novel",
        source_url="https://example.com",
        files=files,
    )


def test_modes_default_and_progress(server):
    root = server.data_root
    epub = root / "book.epub"
    with EpubWriter(epub, title="测试", cover=False) as writer:
        writer.add_chapter(EpubChapter(1, "第一章", ("正文",)))
    cbz = root / "book.cbz"
    with ZipFile(cbz, "w") as archive:
        archive.writestr("p2.jpg", b"image")
    pdf = root / "book.pdf"
    pdf.write_bytes(b"%PDF-123456789")
    text = root / "book.txt"
    text.write_text("第一章 开始\n正文")
    add(server, "b1", [("epub", epub), ("cbz", cbz), ("pdf", pdf), ("txt", text)])
    for query, mode, fmt in (
        ("", "text", "epub"),
        ("?format=txt", "text", "txt"),
        ("?format=cbz", "pages", "cbz"),
        ("?format=pdf", "pdf", "pdf"),
    ):
        status, headers, raw = request(server, "GET", "/api/books/b1/read" + query)
        data = json.loads(raw)
        assert status == 200 and headers["Cache-Control"] == "no-store"
        assert (data["mode"], data["format"], data["progress"]) == (mode, fmt, None)
        if fmt == "pdf":
            assert data["pages"] == data["sections"] == 0 and data["toc"] == []
    status, _, _ = request(
        server,
        "PUT",
        "/api/books/b1/progress",
        {"format": "epub", "position": "3:0.42", "percent": 0.27},
    )
    assert status == 200
    _, _, raw = request(server, "GET", "/api/books/b1/read")
    assert json.loads(raw)["progress"]["position"] == "3:0.42"
    assert (
        request(
            server,
            "PUT",
            "/api/books/b1/progress",
            {"format": "epub", "position": "x", "percent": 2},
        )[0]
        == 400
    )
    status, headers, raw = request(server, "GET", "/api/books/b1/read/page/0?format=cbz")
    assert status == 200 and raw == b"image" and headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Cache-Control"] == "private, max-age=3600"
    status, _, raw = request(server, "GET", "/api/books/b1/read/section/0?format=txt")
    assert status == 200 and "<h1>第一章 开始</h1>" in json.loads(raw)["html"]


def test_pdf_range_and_replaced_file(server):
    pdf = server.data_root / "book.pdf"
    pdf.write_bytes(b"%PDF-123456789")
    add(server, "b1", [("pdf", pdf)])
    status, headers, data = request(
        server, "GET", "/api/books/b1/read/pdf", headers={"Range": "bytes=2-6"}
    )
    assert status == 206 and data == b"DF-12"
    assert headers["Content-Range"] == "bytes 2-6/14"
    assert headers["Content-Type"] == "application/pdf"
    assert headers["Cache-Control"] == "private, max-age=3600"
    pdf.write_bytes(b"changed")
    status, _, data = request(server, "GET", "/api/books/b1/read")
    assert status == 404 and "文件已移动或被修改" in json.loads(data)["error"]


def test_replaced_with_symlink_is_rejected(server):
    pdf = server.data_root / "book.pdf"
    target = server.data_root / "other.pdf"
    pdf.write_bytes(b"original")
    target.write_bytes(b"otherpdf")
    add(server, "b1", [("pdf", pdf)])
    pdf.unlink()
    pdf.symlink_to(target)
    status, _, data = request(server, "GET", "/api/books/b1/read/pdf")
    assert status == 404 and "文件已移动或被修改" in json.loads(data)["error"]


def test_missing_or_invalid_formats_and_resource_traversal(server):
    epub = server.data_root / "book.epub"
    with EpubWriter(epub, title="测试", cover=False) as writer:
        writer.add_chapter(EpubChapter(1, "第一章", ("正文",)))
    add(server, "b1", [("epub", epub)])
    assert request(server, "GET", "/api/books/missing/read")[0] == 404
    assert request(server, "GET", "/api/books/b1/read?format=pdf")[0] == 404
    assert request(server, "GET", "/api/books/b1/read/res/%2e%2e/cover.png?format=epub")[0] == 404
