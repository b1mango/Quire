"""TXT 章节切分与安全 HTML 片段生成。"""

from __future__ import annotations

import html
import os
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

_HEADING = re.compile(r"^第[〇一二三四五六七八九十百千万零两\d]+[章回节卷](?:\s|$|[^\s].*)")
_SPECIAL_HEADING = re.compile(r"^(?:序章|序言|前言|楔子|尾声|后记|番外)(?:\s.*)?$")
_TARGET_SIZE = 8000


@dataclass(frozen=True, slots=True)
class Section:
    title: str
    html: str


def _is_heading(line: str) -> bool:
    return len(line) <= 40 and bool(_HEADING.match(line) or _SPECIAL_HEADING.match(line))


def _section(lines: list[str], heading: bool, index: int) -> Section:
    title = lines[0].strip() if heading else f"第 {index + 1} 段"
    result = [f"<h1>{html.escape(title)}</h1>"] if heading else []
    result.extend(
        f"<p>{html.escape(line.strip())}</p>"
        for line in lines[1 if heading else 0 :]
        if line.strip()
    )
    return Section(title, "".join(result))


def _split(text: str) -> tuple[Section, ...]:
    lines = text.splitlines()
    headings = [i for i, line in enumerate(lines) if _is_heading(line.strip())]
    result: list[Section] = []
    if headings:
        if headings[0] > 0 and any(line.strip() for line in lines[: headings[0]]):
            result.append(_section(lines[: headings[0]], False, len(result)))
        for index, start in enumerate(headings):
            end = headings[index + 1] if index + 1 < len(headings) else len(lines)
            result.append(_section(lines[start:end], True, len(result)))
    else:
        for start in range(0, len(text), _TARGET_SIZE):
            result.append(
                _section(text[start : start + _TARGET_SIZE].splitlines(), False, len(result))
            )
    if not result:
        result.append(_section([], False, 0))
    return tuple(result)


_cache: OrderedDict[tuple[Path, int, int], tuple[Section, ...]] = OrderedDict()
_cache_lock = threading.Lock()


def sections(path: Path, stream: BinaryIO | None = None) -> tuple[Section, ...]:
    """按文件信息缓存切分结果；传入流时使用已经校验的打开文件。"""
    if stream is None:
        with path.open("rb") as opened:
            return sections(path, opened)
    stat = os.fstat(stream.fileno())
    key = (path, stat.st_mtime_ns, stat.st_size)
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    stream.seek(0)
    raw = stream.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("gb18030")
    result = _split(text)
    with _cache_lock:
        _cache[key] = result
        _cache.move_to_end(key)
        if len(_cache) > 8:
            _cache.popitem(last=False)
    return result
