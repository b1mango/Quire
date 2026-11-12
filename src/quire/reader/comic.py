"""漫画压缩包的页面枚举和读取。"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import BinaryIO
from zipfile import ZipFile

from .archive import read_entry, safe_path

_IMAGE_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".avif": "image/avif",
}


def _sort_key(name: str) -> list[tuple[int, int | str]]:
    return [
        (1, int(part)) if part.isdigit() else (0, part.casefold())
        for part in re.split(r"(\d+)", name)
    ]


def pages(path: Path | BinaryIO) -> tuple[str, ...]:
    """仅枚举图片页面，忽略隐藏文件和 macOS 元数据。"""
    with ZipFile(path) as archive:
        names = []
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = info.filename
            if PurePosixPath(name).suffix.lower() not in _IMAGE_TYPES:
                continue
            if safe_path(name) != name:
                raise ValueError("压缩包路径无效")
            if any(part.startswith(".") or part == "__MACOSX" for part in name.split("/")):
                continue
            names.append(name)
    return tuple(sorted(names, key=_sort_key))


def read_page(path: Path | BinaryIO, index: int) -> tuple[bytes, str]:
    names = pages(path)
    if index < 0 or index >= len(names):
        raise IndexError("没有这一页")
    name = names[index]
    with ZipFile(path) as archive:
        return read_entry(archive, name), _IMAGE_TYPES[PurePosixPath(name).suffix.lower()]
