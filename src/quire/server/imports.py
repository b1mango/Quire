"""接收本地文件、整理并登记书库。"""

from __future__ import annotations

import hashlib
import logging
import secrets
import tempfile
import threading
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

from PIL import Image

from ..importer import files
from ..reader import comic, epub, pdfpages
from ..reader.archive import read_entry
from ..store import library
from ..store.models import JsonValue
from . import settings

if TYPE_CHECKING:
    from .app import _Handler

_LOG = logging.getLogger(__name__)
_MAX_UPLOAD = 4 * 1024**3
_CHUNK = 1024 * 1024
_IMPORT_LOCK = threading.Lock()


def _duplicate(data_root: Path, hashes: set[str]) -> library.Book | None:
    return next(
        (
            book
            for book in library.list_books(data_root)
            if any(item.sha256 in hashes for item in book.files)
        ),
        None,
    )


def _cover(data_root: Path, book: library.Book, prepared: files.PreparedBook) -> None:
    fmt, path = prepared.files[0]
    try:
        if fmt in {"cbz", "zip"}:
            data, _ = comic.read_page(path, 0)
        elif fmt == "pdf" and prepared.kind == "manga":
            with path.open("rb") as stream:
                data, _ = pdfpages.read_page(stream, 0)
        elif fmt == "epub" and prepared.cover_path:
            with epub.Epub(path)._archive() as archive:
                data = read_entry(archive, prepared.cover_path)
        else:
            return
        with Image.open(BytesIO(data)) as opened:
            image = opened.convert("RGB")
            image = image.resize(
                (320, max(1, round(image.height * 320 / image.width))), Image.Resampling.LANCZOS
            )
            covers = data_root / "covers"
            covers.mkdir(parents=True, exist_ok=True)
            image.save(covers / f"{book.id}.jpg", "JPEG")
        library.set_cover(data_root, book.id, f"covers/{book.id}.jpg")
    except Exception:
        _LOG.warning("导入封面生成失败", exc_info=True)


def _receive(handler: _Handler, length: int, path: Path) -> str | None:
    digest = hashlib.sha256()
    with path.open("wb") as output:
        while length:
            chunk = handler.rfile.read(min(length, _CHUNK))
            if not chunk:
                handler.close_connection = True
                return None
            output.write(chunk)
            digest.update(chunk)
            length -= len(chunk)
    return digest.hexdigest()


def route(handler: _Handler, method: str, parts: list[str]) -> bool:
    if method != "POST" or parts != ["api", "import"]:
        return False
    query = parse_qs(urlsplit(handler.path).query)
    name = query.get("name", [""])[0].strip()
    length_text = handler.headers.get("Content-Length")
    if not name or length_text is None or not length_text.isascii() or not length_text.isdecimal():
        handler.close_connection = True
        handler._reply_json(400, {"error": "请提供文件名和有效的文件长度"})
        return True
    length = int(length_text)
    if length > _MAX_UPLOAD:
        handler.close_connection = True
        handler._reply_json(413, {"error": "文件超过 4 GiB 上限"})
        return True
    fmt = Path(name.replace("\\", "/")).suffix.lower().removeprefix(".")
    if fmt not in files.FORMATS:
        handler.close_connection = True
        handler._reply_json(415, {"error": "不支持这种文件格式"})
        return True
    root = handler.server.data_root
    staging = root / "imports"
    staging.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=staging, suffix=".part") as temporary:
        path = Path(temporary.name)
        digest = _receive(handler, length, path)
        if digest is None:
            handler._reply_json(400, {"error": "上传的文件不完整"})
            return True
        with _IMPORT_LOCK:
            duplicate = _duplicate(root, {digest})
            if duplicate:
                handler._reply_json(409, {"error": "书库里已有同一文件", "book_id": duplicate.id})
                return True
            current = settings.load(handler.server.settings_path, root)
            style = query.get("style", [current.epub_style])[0]
            prepared = files.prepare(path, name, current.output_path, style, current.epub_font)
            try:
                hashes = {library._hash(target)[1] for _, target in prepared.files}
                duplicate = _duplicate(root, hashes)
                if duplicate:
                    for _, target in prepared.files:
                        target.unlink(missing_ok=True)
                    handler._reply_json(
                        409, {"error": "书库里已有同一文件", "book_id": duplicate.id}
                    )
                    return True
                book = library.add_book(
                    root,
                    secrets.token_hex(8),
                    title=prepared.title,
                    author=prepared.author,
                    kind=prepared.kind,
                    source_url="",
                    files=prepared.files,
                )
            except BaseException:
                for _, target in prepared.files:
                    target.unlink(missing_ok=True)
                raise
            _cover(root, book, prepared)
    notes: list[JsonValue] = list(prepared.notes)
    handler._reply_json(
        201,
        {
            "book_id": book.id,
            "title": book.title,
            "author": book.author,
            "kind": book.kind,
            "formats": [fmt for fmt, _ in prepared.files],
            "chapters": prepared.chapters,
            "pages": prepared.pages,
            "notes": notes,
        },
    )
    return True
