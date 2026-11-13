"""验证并整理各格式的本地书籍。"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from zipfile import BadZipFile

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from ..errors import ParseError
from ..reader import comic, epub, pdfpages
from ..reader.archive import read_entry
from ..utils.naming import available_book_dir, safe_filename
from . import txt
from .epub_clean import clean_epub
from .naming import filename_metadata

FORMATS = frozenset({"txt", "epub", "pdf", "cbz", "zip"})


@dataclass(frozen=True, slots=True)
class PreparedBook:
    title: str
    author: str
    kind: str
    files: tuple[tuple[str, Path], ...]
    chapters: int | None
    pages: int | None
    notes: list[str]
    cover_path: str | None = None


def _inspect(
    path: Path, fmt: str, title: str, author: str
) -> tuple[str, str, str, int | None, int | None, str | None]:
    try:
        if fmt == "epub":
            book = epub.Epub(path)
            if not book.sections:
                raise ValueError("EPUB 没有章节")
            with book._archive() as archive:
                for section in book.paths:
                    read_entry(archive, section, 8 * 1024 * 1024).decode("utf-8-sig")
            return (
                book.title or title,
                book.author or author,
                "novel",
                book.sections,
                None,
                book.cover_path,
            )
        if fmt == "pdf":
            with path.open("rb") as stream:
                pages = len(PdfReader(stream).pages)
                if not pages:
                    raise ValueError("PDF 没有页面")
                stream.seek(0)
                image_pages, _ = pdfpages.inspect(stream)
            return title, author, "manga" if image_pages else "novel", None, pages, None
        pages = len(comic.pages(path))
        if not pages:
            raise ValueError("压缩包没有图片")
        return title, author, "manga", None, pages, None
    except (
        ValueError,
        BadZipFile,
        PyPdfError,
        EOFError,
        UnicodeError,
        KeyError,
        IndexError,
        TypeError,
        AttributeError,
        NotImplementedError,
    ) as exc:
        raise ParseError("文件内容无效或无法读取", hint="请检查文件是否完整") from exc


def describe(path: Path, name: str) -> dict[str, str | int | None]:
    """导入前预览：识别出的书名、作者、类型与章/页数，不写任何文件。"""
    fmt = Path(name).suffix.lower().removeprefix(".")
    title, author = filename_metadata(name)
    if fmt == "txt":
        info = txt.analyze(path, title, author)
        return {
            "title": info.title,
            "author": info.author,
            "kind": "novel",
            "format": fmt,
            "chapters": None,
            "pages": None,
        }
    title, author, kind, chapters, pages, _ = _inspect(path, fmt, title, author)
    return {
        "title": title,
        "author": author,
        "kind": kind,
        "format": fmt,
        "chapters": chapters,
        "pages": pages,
    }


def prepare(
    path: Path,
    name: str,
    output: Path,
    style: str,
    font: str,
    *,
    title: str = "",
    author: str | None = None,
) -> PreparedBook:
    """整理并写出成品。``title`` / ``author`` 为用户在导入前确认过的值（空书名表示沿用识别结果）。"""
    fmt = Path(name).suffix.lower().removeprefix(".")
    chosen_title, chosen_author = title.strip(), author
    title, author = filename_metadata(name)
    info = txt.analyze(path, title, author) if fmt == "txt" else None
    if info is not None:
        title, author = info.title, info.author
        kind, chapters, pages, cover = "novel", None, None, None
    else:
        title, author, kind, chapters, pages, cover = _inspect(path, fmt, title, author)
    if chosen_title:
        title = chosen_title
    if chosen_author is not None:
        author = chosen_author.strip()
    if info is not None:
        info = replace(info, title=title, author=author)
    directory = available_book_dir(output, title)
    directory.mkdir(parents=True, exist_ok=True)
    filename = safe_filename(title, default="book")
    paths = (
        [("epub", directory / f"{filename}.epub"), ("txt", directory / f"{filename}.txt")]
        if info is not None
        else [(fmt, directory / f"{filename}.{fmt}")]
    )
    try:
        if info is not None:
            chapters, notes = txt.write(path, info, paths[0][1], paths[1][1], style, font)
        else:
            notes = []
            if fmt == "epub":
                removed = clean_epub(path, paths[0][1])
                notes.append(f"删除广告/站点提示 {removed} 段" if removed else "未发现广告段落")
            else:
                with path.open("rb") as source, paths[0][1].open("xb") as copy:
                    shutil.copyfileobj(source, copy, length=1024 * 1024)
        return PreparedBook(title, author, kind, tuple(paths), chapters, pages, notes, cover)
    except BaseException:
        for _, target in paths:
            target.unlink(missing_ok=True)
        raise
