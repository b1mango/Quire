"""只读取安全的 ZIP 条目，不向磁盘解压。"""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
from zipfile import ZipFile

_MAX_ENTRY = 64 * 1024 * 1024


def safe_path(path: str) -> str:
    """校验已解码的包内路径，拒绝绝对路径和任何上级目录。"""
    decoded = path
    if (
        not decoded
        or "\x00" in decoded
        or "\\" in decoded
        or PurePosixPath(decoded).is_absolute()
        or PureWindowsPath(decoded).is_absolute()
        or PureWindowsPath(decoded).drive
        or ".." in decoded.split("/")
    ):
        raise ValueError("压缩包路径无效")
    normalized = str(PurePosixPath(decoded))
    if normalized == ".":
        raise ValueError("压缩包路径无效")
    return normalized


def read_entry(archive: ZipFile, name: str, limit: int = _MAX_ENTRY) -> bytes:
    """读取包内已登记的条目，限制解压大小及压缩比。"""
    if safe_path(name) != name or name not in archive.namelist():
        raise ValueError("压缩包条目不存在或路径无效")
    info = archive.getinfo(name)
    if info.is_dir() or info.file_size > min(limit, _MAX_ENTRY):
        raise ValueError("压缩包条目过大或无效")
    if info.file_size > info.compress_size * 100:
        raise ValueError("压缩包条目压缩比过高")
    return archive.read(info)
