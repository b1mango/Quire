from __future__ import annotations

import http.client
import json
import threading
from io import BytesIO
from pathlib import Path
from urllib.parse import quote
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from PIL import Image
from pypdf import PdfWriter

from quire.assemble.epub import EpubChapter, EpubWriter
from quire.server import imports
from quire.server.app import make_server
from quire.store import library


@pytest.fixture()
def server(tmp_path):
    server = make_server("127.0.0.1", 0, data_root=tmp_path, opener=lambda _: None)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


def request(server, name, data, *, style=""):
    host, port = server.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=10)
    path = "/api/import?name=" + quote(name)
    if style:
        path += "&style=" + quote(style)
    conn.request(
        "POST",
        path,
        data,
        {"X-Quire-Token": server.token, "Content-Type": "application/octet-stream"},
    )
    response = conn.getresponse()
    result = response.status, json.loads(response.read())
    conn.close()
    return result


def image_bytes():
    out = BytesIO()
    Image.new("RGB", (12, 20), "red").save(out, "PNG")
    return out.getvalue()


def comic_bytes():
    out = BytesIO()
    with ZipFile(out, "w", ZIP_DEFLATED) as archive:
        archive.writestr("1.png", image_bytes())
    return out.getvalue()


def epub_bytes(path: Path):
    with EpubWriter(path, title="OPF书名", cover=False) as writer:
        writer.add_chapter(EpubChapter(1, "第一章", ("一段正文。",)))
    return path.read_bytes()


def pdf_bytes():
    out = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=200)
    writer.write(out)
    return out.getvalue()


@pytest.mark.parametrize("fmt", ["cbz", "zip"])
def test_comic_import_and_duplicate(server, fmt):
    data = comic_bytes()
    status, result = request(server, f"漫画_阿青.{fmt.upper()}", data)
    assert status == 201
    assert (result["title"], result["author"], result["kind"], result["pages"]) == (
        "漫画",
        "阿青",
        "manga",
        1,
    )
    assert result["chapters"] is None and result["formats"] == [fmt]
    book = library.get_book(server.data_root, result["book_id"])
    assert book.cover and (server.data_root / book.cover).is_file()
    with Image.open(server.data_root / book.cover) as cover:
        assert cover.width == 320 and cover.format == "JPEG"
    status, duplicate = request(server, f"漫画.{fmt}", data)
    assert status == 409 and duplicate["book_id"] == book.id
    assert not list((server.data_root / "imports").glob("*.part"))


def test_txt_and_epub_import(server, tmp_path):
    data = "第一章 开端\n第一段正文。\n".encode("gb18030")
    status, result = request(server, "小说.txt", data, style="classic")
    assert status == 201
    assert result["formats"] == ["epub", "txt"] and result["chapters"] == 1
    assert result["pages"] is None and result["kind"] == "novel"
    status, duplicate = request(server, "小说.txt", data, style="classic")
    assert status == 409 and duplicate["book_id"] == result["book_id"]
    status, epub_result = request(server, "外部.epub", epub_bytes(tmp_path / "input.epub"))
    assert status == 201 and epub_result["title"] == "OPF书名"
    assert epub_result["chapters"] == 1 and epub_result["formats"] == ["epub"]


def test_pdf_import(server):
    status, result = request(server, "文档.pdf", pdf_bytes())
    assert status == 201
    assert result["kind"] == "novel" and result["pages"] == 1 and result["chapters"] is None


def test_bad_requests(server, monkeypatch):
    assert request(server, "书.exe", b"x")[0] == 415
    assert request(server, "书.epub", b"not epub")[0] == 422
    assert request(server, "", b"x")[0] == 400
    host, port = server.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=10)
    conn.putrequest("POST", "/api/import?name=book.txt")
    conn.putheader("X-Quire-Token", server.token)
    conn.endheaders()
    response = conn.getresponse()
    assert response.status == 400
    response.read()
    conn.close()
    monkeypatch.setattr(imports, "_MAX_UPLOAD", 2)
    assert request(server, "书.txt", b"abc")[0] == 413
    assert not list((server.data_root / "imports").glob("*.part"))
