"""书库阅读接口与文件响应。"""

from __future__ import annotations

import os
import re
import stat
from typing import TYPE_CHECKING, BinaryIO
from urllib.parse import parse_qs, unquote, urlsplit
from zipfile import BadZipFile

from ..errors import LedgerError
from ..reader import comic, epub, txt
from ..store import library, progress
from ..store.models import JsonValue

if TYPE_CHECKING:
    from .app import _Handler

_FORMATS = ("epub", "cbz", "zip", "pdf", "txt")
_MOVED = "文件已移动或被修改，请重新导出"


def _artifact(book: library.Book, fmt: str) -> library.BookArtifact | None:
    return next((item for item in book.files if item.format == fmt), None)


def _bytes(handler: _Handler, data: bytes, content_type: str) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "private, max-age=3600")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Content-Security-Policy", "sandbox; default-src 'none'")
    handler.end_headers()
    handler.wfile.write(data)


def _pdf(handler: _Handler, stream: BinaryIO, size: int) -> None:
    start, end = 0, size - 1
    requested = handler.headers.get("Range")
    if requested:
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
        if not match or not any(match.groups()):
            return _range_error(handler, size)
        first, last = match.groups()
        if first:
            start = int(first)
            end = min(int(last), end) if last else end
        else:
            length = int(last)
            start = max(size - length, 0)
        if start > end or start >= size:
            return _range_error(handler, size)
    stream.seek(start)
    handler.send_response(206 if requested else 200)
    handler.send_header("Content-Type", "application/pdf")
    handler.send_header("Content-Length", str(end - start + 1))
    handler.send_header("Accept-Ranges", "bytes")
    if requested:
        handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    handler.send_header("Cache-Control", "private, max-age=3600")
    handler.end_headers()
    remaining = end - start + 1
    while remaining:
        chunk = stream.read(min(65536, remaining))
        if not chunk:
            break
        handler.wfile.write(chunk)
        remaining -= len(chunk)


def _range_error(handler: _Handler, size: int) -> None:
    handler.send_response(416)
    handler.send_header("Content-Range", f"bytes */{size}")
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def _save(handler: _Handler, book: library.Book) -> None:
    body = handler._body()
    if not isinstance(body, dict):
        return handler._reply_json(400, {"error": "阅读进度无效"})
    fmt, position, percent = body.get("format"), body.get("position"), body.get("percent")
    if not isinstance(fmt, str) or fmt not in _FORMATS or _artifact(book, fmt) is None:
        return handler._reply_json(404, {"error": "这本书没有该格式"})
    if (
        not isinstance(position, str)
        or len(position) > 64
        or isinstance(percent, bool)
        or not isinstance(percent, (int, float))
        or not 0 <= percent <= 1
    ):
        return handler._reply_json(400, {"error": "阅读进度无效"})
    try:
        progress.save(
            handler.server.data_root, book.id, format=fmt, position=position, percent=percent
        )
    except LedgerError:
        return handler._reply_json(404, {"error": "没有这本书"})
    handler._reply_json(200, {"saved": True})


def _read(
    handler: _Handler,
    book: library.Book,
    item: library.BookArtifact,
    parts: list[str],
    stream: BinaryIO,
) -> None:
    fmt, path = item.format, item.path
    if len(parts) == 4:
        toc: list[JsonValue] = []
        sections, pages = 0, 0
        if fmt == "epub":
            document = epub.Epub(stream)
            toc, sections = [dict(entry) for entry in document.toc], document.sections
        elif fmt == "txt":
            texts = txt.sections(path, stream)
            sections = len(texts)
            toc = [{"title": section.title, "section": i} for i, section in enumerate(texts)]
        elif fmt in ("cbz", "zip"):
            pages = len(comic.pages(stream))
        mode = "text" if fmt in ("epub", "txt") else "pages" if fmt in ("cbz", "zip") else "pdf"
        saved = progress.get(handler.server.data_root, book.id)
        state: JsonValue = (
            {"position": saved.position, "percent": saved.percent, "updated_at": saved.updated_at}
            if saved
            else None
        )
        return handler._reply_json(
            200,
            {
                "book_id": book.id,
                "title": book.title,
                "author": book.author,
                "format": fmt,
                "mode": mode,
                "toc": toc,
                "sections": sections,
                "pages": pages,
                "progress": state,
            },
        )
    if len(parts) == 6 and parts[4] == "section" and fmt in ("epub", "txt"):
        index = int(parts[5])
        if index < 0:
            raise IndexError
        if fmt == "epub":
            result = epub.Epub(stream).section(index, book.id)
        else:
            section = txt.sections(path, stream)[index]
            result = {"index": index, "title": section.title, "html": section.html}
        return handler._reply_json(200, dict(result))
    if len(parts) >= 6 and parts[4] == "res" and fmt == "epub":
        data, content_type = epub.Epub(stream).resource("/".join(parts[5:]))
        return _bytes(handler, data, content_type)
    if len(parts) == 6 and parts[4] == "page" and fmt in ("cbz", "zip"):
        data, content_type = comic.read_page(stream, int(parts[5]))
        return _bytes(handler, data, content_type)
    if len(parts) == 5 and parts[4] == "pdf" and fmt == "pdf":
        return _pdf(handler, stream, item.bytes)
    handler._reply_json(404, {"error": "没有这个阅读内容"})


def route(handler: _Handler, method: str, parts: list[str]) -> bool:
    if len(parts) < 4 or parts[:2] != ["api", "books"] or parts[3] not in ("read", "progress"):
        return False
    if (parts[3] == "read" and method != "GET") or (
        parts[3] == "progress" and (method != "PUT" or len(parts) != 4)
    ):
        return False
    try:
        book = library.get_book(handler.server.data_root, unquote(parts[2]))
    except LedgerError:
        handler._reply_json(404, {"error": "没有这本书"})
        return True
    if parts[3] == "progress":
        _save(handler, book)
        return True
    query = parse_qs(urlsplit(handler.path).query, keep_blank_values=True)
    default = next((fmt for fmt in _FORMATS if _artifact(book, fmt)), "")
    fmt = query.get("format", [default])[0]
    item = _artifact(book, fmt) if fmt in _FORMATS else None
    if item is None:
        handler._reply_json(404, {"error": "这本书没有该格式"})
    else:
        try:
            fd = os.open(item.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            stream = os.fdopen(fd, "rb")
        except OSError:
            handler._reply_json(404, {"error": _MOVED})
            return True
        with stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != item.bytes:
                handler._reply_json(404, {"error": _MOVED})
            else:
                try:
                    _read(handler, book, item, [unquote(part) for part in parts], stream)
                except (ValueError, IndexError, KeyError, BadZipFile, UnicodeError, OSError):
                    handler._reply_json(404, {"error": "无法读取这本书的内容"})
    return True
