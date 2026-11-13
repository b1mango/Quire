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


def call(server, method, path, body=None):
    host, port = server.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=10)
    data = json.dumps(body).encode() if body is not None else None
    conn.request(
        method, path, data, {"X-Quire-Token": server.token, "Content-Type": "application/json"}
    )
    response = conn.getresponse()
    result = response.status, json.loads(response.read())
    conn.close()
    return result


def preview(server, name, data):
    host, port = server.server_address[:2]
    conn = http.client.HTTPConnection(host, port, timeout=10)
    conn.request(
        "POST",
        "/api/import?preview=1&name=" + quote(name),
        data,
        {"X-Quire-Token": server.token, "Content-Type": "application/octet-stream"},
    )
    response = conn.getresponse()
    result = response.status, json.loads(response.read())
    conn.close()
    return result


NOVEL = "".join(f"第{n}章 起{n}\n第{n}章正文第一句。\n第{n}章正文第二句。\n" for n in range(1, 5))


def test_preview_then_confirm_with_edited_title_and_author(server):
    status, info = preview(server, "《旧名》作者：旧人.txt", NOVEL.encode("gb18030"))
    assert status == 200 and (info["title"], info["author"], info["kind"]) == (
        "旧名",
        "旧人",
        "novel",
    )
    assert library.list_books(server.data_root) == ()  # 预览不入库
    status, book = call(
        server, "POST", f"/api/import/{info['token']}", {"title": "新名", "author": "新人"}
    )
    assert status == 201 and (book["title"], book["author"], book["chapters"]) == (
        "新名",
        "新人",
        4,
    )
    assert not list((server.data_root / "imports").glob("*"))  # 暂存已清理
    assert call(server, "POST", f"/api/import/{info['token']}", {})[0] == 404  # 令牌只能用一次


def test_preview_can_be_discarded(server):
    status, info = preview(server, "书.txt", NOVEL.encode())
    assert call(server, "DELETE", f"/api/import/{info['token']}")[0] == 200
    assert not list((server.data_root / "imports").glob("*"))
    assert library.list_books(server.data_root) == ()


def test_zip_with_txt_and_gbk_entry_name(server):
    # Windows 打包：中文名按 GBK 存且不带 UTF-8 标记（zipfile 写非 ASCII 名会自动标 UTF-8，故按字节替换）
    gbk = "《压缩书》作者：甲.txt".encode("gbk")
    placeholder = "A" * (len(gbk) - 4) + ".txt"
    out = BytesIO()
    with ZipFile(out, "w") as archive:
        archive.writestr(placeholder, NOVEL.encode("gb18030"))
        archive.writestr("readme.txt", "小文件".encode("gbk"))
    out = BytesIO(out.getvalue().replace(placeholder.encode(), gbk))
    status, info = preview(server, "网盘打包.zip", out.getvalue())
    assert status == 200 and (info["title"], info["author"], info["format"]) == (
        "压缩书",
        "甲",
        "txt",
    )
    assert "从压缩包取出" in info["notes"][0] and "另有 1 个文件" in info["notes"][1]
    status, book = call(server, "POST", f"/api/import/{info['token']}", {})
    assert status == 201 and book["formats"] == ["epub", "txt"] and book["chapters"] == 4


def test_epub_import_removes_ad_paragraphs(server, tmp_path):
    source = tmp_path / "ad.epub"
    ad = "更多精校小说尽在知轩藏书下载：https://zxcs.zip/"
    with EpubWriter(source, title="广告书") as writer:
        writer.add_chapter(EpubChapter(1, "前言", ("=" * 58, ad, "=" * 58, "真正的前言。")))
        writer.add_chapter(EpubChapter(2, "第一章", ("正文。", "更多的人请到后院去看看那座网球场")))
    status, book = request(server, "ad.epub", source.read_bytes())
    assert status == 201 and "删除广告/站点提示 3 段" in book["notes"]
    stored = next(f.path for f in library.get_book(server.data_root, book["book_id"]).files)
    text = "".join(
        ZipFile(stored).read(n).decode() for n in ZipFile(stored).namelist() if n.endswith("html")
    )
    assert "知轩藏书" not in text and "=" * 30 not in text
    assert "真正的前言" in text and "网球场" in text  # 正文不误删
