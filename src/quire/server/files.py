"""文件类响应：任务缩略图与书库封面（自 app.py 拆出，守住模块行数门禁）。

安全契约不变：缩略图按任务目录名与纯数字页码拼路径，封面要求仍位于
数据目录的二级子目录内，符号链接一律 404。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..errors import LedgerError
from ..store import library

if TYPE_CHECKING:
    from .app import _Handler


def send(handler: _Handler, path: Path, content_type: str) -> None:
    body = path.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def thumb(handler: _Handler, job_id: str, page: str) -> None:
    job = handler.server.manager.get(job_id)
    if job is None or not page.isdigit():
        return handler._reply_json(404, {"error": "没有这张缩略图"})
    path = handler.server.manager.thumbs_root / job.id / f"p{int(page)}.jpg"
    if not path.is_file() or path.is_symlink():
        return handler._reply_json(404, {"error": "没有这张缩略图"})
    send(handler, path, "image/jpeg")


def cover(handler: _Handler, book_id: str) -> None:
    try:
        book = library.get_book(handler.server.data_root, book_id)
    except LedgerError:
        return handler._reply_json(404, {"error": "没有这本书"})
    if not book.cover:
        return handler._reply_json(404, {"error": "这本书没有封面"})
    path = handler.server.data_root / book.cover
    if not path.is_file() or path.is_symlink() or path.parent.parent != handler.server.data_root:
        return handler._reply_json(404, {"error": "这本书没有封面"})
    send(handler, path, "image/jpeg")
