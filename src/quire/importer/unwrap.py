"""网盘压缩包：ZIP 里装的是书（TXT/EPUB/PDF）而不是漫画图片时，取出书文件再导入。

只取一本：挑体积最大的书文件（网盘包常附带「说明.txt」「更多好书.url」之类的小文件），
其余写进整理说明。不带 UTF-8 标记的条目名按 GBK 还原（Windows 打的包中文名默认如此）。
解出的文件仍受单条目 64 MiB 之外的总体积约束：按流复制，不整份读进内存。
"""

from __future__ import annotations

import shutil
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile, ZipInfo

from ..errors import ParseError
from ..reader import comic

BOOK_TYPES = (".txt", ".epub", ".pdf")
_MAX_INNER = 2 * 1024**3


def entry_name(info: ZipInfo) -> str:
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("gbk")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


def unwrap(path: Path, name: str, workdir: Path) -> tuple[Path, str, list[str]] | None:
    """ZIP 里没有漫画图片但有书文件时，解出最大的一本，返回 (路径, 原文件名, 说明)；否则 None。"""
    if PurePosixPath(name).suffix.lower() != ".zip":
        return None
    try:
        if comic.pages(path):
            return None
        with ZipFile(path) as archive:
            books = [
                info
                for info in archive.infolist()
                if not info.is_dir()
                and PurePosixPath(entry_name(info)).suffix.lower() in BOOK_TYPES
                and not any(
                    part.startswith((".", "__MACOSX")) for part in entry_name(info).split("/")
                )
            ]
            if not books:
                return None
            chosen = max(books, key=lambda info: info.file_size)
            if chosen.file_size > _MAX_INNER:
                raise ParseError("压缩包里的书文件过大")
            inner = PurePosixPath(entry_name(chosen)).name
            target = workdir / f"{path.name}.inner{PurePosixPath(inner).suffix.lower()}"
            with archive.open(chosen) as source, target.open("wb") as out:
                shutil.copyfileobj(source, out, 1024 * 1024)
    except (BadZipFile, ValueError, OSError) as exc:
        raise ParseError("压缩包无法读取", hint="请检查文件是否完整") from exc
    notes = [f"从压缩包取出《{inner}》"]
    if len(books) > 1:
        notes.append(f"压缩包里另有 {len(books) - 1} 个文件未导入")
    return target, inner, notes
