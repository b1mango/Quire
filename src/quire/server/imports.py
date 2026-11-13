"""接收本地文件、整理并登记书库。"""

from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlsplit

from PIL import Image

from ..errors import QuireError
from ..importer import files
from ..importer.unwrap import unwrap
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


@dataclass
class _Staged:
    path: Path
    name: str
    digest: str
    notes: list[str]
    extra: list[Path]
    created: float


_STAGED: dict[str, _Staged] = {}
_STAGE_TTL = 2 * 3600


def _discard(item: _Staged) -> None:
    for path in (item.path, *item.extra):
        path.unlink(missing_ok=True)


def _sweep() -> None:
    """待确认超过 2 小时（关了窗口没确认）的上传自动清掉。"""
    now = time.time()
    for token, item in list(_STAGED.items()):
        if now - item.created > _STAGE_TTL:
            _discard(_STAGED.pop(token))


def _accept(handler: _Handler, query: dict[str, list[str]]) -> _Staged | None:
    """校验并接收上传，返回暂存文件；出错时已回复。ZIP 里装书时换成解出的书文件。"""
    name = query.get("name", [""])[0].strip()
    length_text = handler.headers.get("Content-Length")
    if not name or length_text is None or not length_text.isascii() or not length_text.isdecimal():
        handler.close_connection = True
        handler._reply_json(400, {"error": "请提供文件名和有效的文件长度"})
        return None
    length = int(length_text)
    if length > _MAX_UPLOAD:
        handler.close_connection = True
        handler._reply_json(413, {"error": "文件超过 4 GiB 上限"})
        return None
    fmt = Path(name.replace("\\", "/")).suffix.lower().removeprefix(".")
    if fmt not in files.FORMATS:
        handler.close_connection = True
        handler._reply_json(415, {"error": "不支持这种文件格式"})
        return None
    staging = handler.server.data_root / "imports"
    staging.mkdir(parents=True, exist_ok=True)
    path = staging / f"{secrets.token_hex(12)}.part"
    digest = _receive(handler, length, path)
    if digest is None:
        path.unlink(missing_ok=True)
        handler._reply_json(400, {"error": "上传的文件不完整"})
        return None
    item = _Staged(path, name, digest, [], [], time.time())
    try:
        inner = unwrap(path, name, staging)
    except QuireError:
        _discard(item)
        raise
    if inner is not None:  # ZIP 里装的是书：之后按解出的书文件处理，原压缩包一并清理
        item.path, item.name, item.notes, item.extra = inner[0], inner[1], inner[2], [path]
    return item


def _commit(
    handler: _Handler, item: _Staged, style: str | None, title: str, author: str | None
) -> None:
    root = handler.server.data_root
    with _IMPORT_LOCK:
        duplicate = _duplicate(root, {item.digest})
        if duplicate:
            handler._reply_json(409, {"error": "书库里已有同一文件", "book_id": duplicate.id})
            return
        current = settings.load(handler.server.settings_path, root)
        prepared = files.prepare(
            item.path,
            item.name,
            current.output_path,
            style or current.epub_style,
            current.epub_font,
            title=title,
            author=author,
        )
        try:
            hashes = {library._hash(target)[1] for _, target in prepared.files}
            duplicate = _duplicate(root, hashes)
            if duplicate:
                for _, target in prepared.files:
                    target.unlink(missing_ok=True)
                handler._reply_json(409, {"error": "书库里已有同一文件", "book_id": duplicate.id})
                return
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
    notes: list[JsonValue] = [*item.notes, *prepared.notes]
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


def route(handler: _Handler, method: str, parts: list[str]) -> bool:
    """``POST /api/import?name=``：一步导入；加 ``&preview=1`` 只上传并返回识别结果与令牌，
    之后 ``POST /api/import/<令牌>``（JSON：title、author、style）确认导入，
    ``DELETE /api/import/<令牌>`` 放弃。"""
    if parts[:2] != ["api", "import"] or len(parts) > 3:
        return False
    query = parse_qs(urlsplit(handler.path).query)
    if len(parts) == 3:
        if method not in ("POST", "DELETE"):
            return False
        with _IMPORT_LOCK:
            item = _STAGED.pop(parts[2], None)
        if item is None:
            handler._reply_json(404, {"error": "这个导入已过期，请重新选择文件"})
            return True
        if method == "DELETE":
            _discard(item)
            handler._reply_json(200, {"discarded": True})
            return True
        body = handler._body()
        title = body.get("title") if isinstance(body, dict) else None
        author = body.get("author") if isinstance(body, dict) else None
        style = body.get("style") if isinstance(body, dict) else None
        try:
            _commit(
                handler,
                item,
                style if isinstance(style, str) and style else None,
                title if isinstance(title, str) else "",
                author if isinstance(author, str) else None,
            )
        finally:
            _discard(item)
        return True
    if method != "POST":
        return False
    with _IMPORT_LOCK:
        _sweep()
    item = _accept(handler, query)
    if item is None:
        return True
    if query.get("preview", [""])[0] != "1":
        try:
            _commit(handler, item, query.get("style", [""])[0] or None, "", None)
        finally:
            _discard(item)
        return True
    try:
        with _IMPORT_LOCK:
            duplicate = _duplicate(handler.server.data_root, {item.digest})
        if duplicate:
            _discard(item)
            handler._reply_json(409, {"error": "书库里已有同一文件", "book_id": duplicate.id})
            return True
        info = files.describe(item.path, item.name)
    except BaseException:
        _discard(item)
        raise
    token = secrets.token_hex(12)
    with _IMPORT_LOCK:
        _STAGED[token] = item
    preview: dict[str, JsonValue] = {"token": token, "name": item.name, "notes": list(item.notes)}
    preview.update(info)
    handler._reply_json(200, preview)
    return True
