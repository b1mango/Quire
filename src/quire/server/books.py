"""任务成品登记入书库：成品清单、报告与封面（项目设计.md §6.12）。"""

from __future__ import annotations

import secrets
from pathlib import Path

from ..models import MangaResult, NovelResult
from ..store import library
from .job_state import JobSpec


def register_book(
    data_root: Path,
    thumbs_root: Path,
    job_id: str,
    spec: JobSpec,
    result: MangaResult | NovelResult,
) -> library.Book:
    files: list[tuple[str, Path]] = [(a.format, a.path) for a in result.artifacts]
    if result.report is not None:
        files.append(("report", result.report))
    if isinstance(result, NovelResult) and result.review is not None:
        files.append(("review", result.review))
    book = library.add_book(
        data_root,
        secrets.token_hex(8),
        title=result.title or spec.title,
        kind=spec.kind,
        source_url=spec.url,
        files=files,
        compress=spec.compress if spec.kind == "manga" else "",
    )
    thumbs = sorted((thumbs_root / job_id).glob("p*.jpg"))
    if thumbs:
        covers = data_root / "covers"
        covers.mkdir(parents=True, exist_ok=True)
        target = covers / f"{book.id}.jpg"
        target.write_bytes(thumbs[0].read_bytes())
        library.set_cover(data_root, book.id, f"covers/{book.id}.jpg")
    return book
