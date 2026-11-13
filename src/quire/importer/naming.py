"""从用户文件名与小说开头提取书名和作者。"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import PurePosixPath

_DECORATION = re.compile(r"【[^】]*】|\[[^\]]*\]|[（(](?:精校版|全本|完结版|完整版)[）)]")
_METADATA = re.compile(r"^\s*(书名|作者)\s*[:：]\s*(.+?)\s*$")


def filename_metadata(name: str) -> tuple[str, str]:
    stem = PurePosixPath(name.replace("\\", "/")).stem.strip()
    cleaned = _DECORATION.sub("", stem).strip()
    match = re.fullmatch(r"《([^》]+)》\s*(?:作者\s*[:：]\s*(.+)|[（(]([^）)]+)[）)])?", cleaned)
    if match:
        return match.group(1).strip(), (match.group(2) or match.group(3) or "").strip()
    match = re.fullmatch(r"(.+?)\s+作者\s*[:：]\s*(.+)", cleaned)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    for separator in ("_", "-"):
        if separator not in cleaned:
            continue
        title, author = (part.strip() for part in cleaned.rsplit(separator, 1))
        if title and author and len(author) <= 12 and not any(c.isdigit() for c in author):
            return title, author
    return cleaned or stem or "未命名", ""


def text_metadata(lines: Iterable[str], title: str, author: str) -> tuple[str, str]:
    for line in lines:
        match = _METADATA.fullmatch(line)
        if match:
            value = match.group(2).strip().strip("《》")
            if match.group(1) == "书名":
                title = value
            else:
                author = value
    return title, author
