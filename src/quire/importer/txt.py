"""流式识别小说编码、章节和站点签名。"""

from __future__ import annotations

import codecs
import re
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

from ..assemble.epub import EpubChapter, EpubWriter
from ..errors import ParseError
from ..reader.txt import _HEADING, _SPECIAL_HEADING
from ..text.clean import clean_paragraphs, strip_site_suffix
from ..text.noise import is_site_signature, repeated_site_signatures
from . import rewrap
from .naming import text_metadata

_ENGLISH = re.compile(r"^chapter\s+\d+(?:\s+[^。！？!?]*)?$", re.I)
_VOLUME = re.compile(r"^第[〇一二三四五六七八九十百千万零两\d]+卷")
_TARGET = 8000


@dataclass(frozen=True, slots=True)
class TextInfo:
    encoding: str
    title: str
    author: str
    has_headings: bool
    signatures: frozenset[str]
    wrap: int = 0  # 硬换行宽度（显示宽度），0 表示正常分段


def _encoding(path: Path) -> str:
    with path.open("rb") as stream:
        prefix = stream.read(4)
    choices: tuple[str, ...]
    if prefix.startswith(codecs.BOM_UTF8):
        choices = ("utf-8-sig",)
    elif prefix.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        choices = ("utf-16",)
    else:
        choices = ("utf-8", "gb18030")
    for encoding in choices:
        try:
            with path.open("r", encoding=encoding, newline=None) as stream:
                while chunk := stream.read(1024 * 1024):
                    if "\x00" in chunk:
                        raise ParseError("TXT 文件含有无法识别的内容")
        except UnicodeDecodeError:
            continue
        return encoding
    raise ParseError("TXT 文件无法解码", hint="请检查文件编码后重试")


def _heading(line: str) -> bool:
    if len(line) > 40 or any(mark in line for mark in '。！？!?"“”‘’「」『』'):
        return False
    return bool(_HEADING.match(line) or _SPECIAL_HEADING.match(line) or _ENGLISH.fullmatch(line))


def _chapters(
    path: Path, encoding: str, has_headings: bool, wrap: int = 0
) -> Iterator[tuple[str, list[str]]]:
    title = "前言" if has_headings else "第 1 段"
    paragraphs: list[str] = []
    length = 0
    number = 1
    last = ""
    joining = False  # 上一行是被硬折断的满行，本行要接上去
    with path.open("r", encoding=encoding, newline=None) as stream:
        for raw in stream:
            line = raw.strip()
            if not line:
                joining = False
                continue
            if has_headings and _heading(line):
                joining = False
                if line == last and len(paragraphs) <= 3:
                    continue  # 本章开头几段内又抄了一遍标题（网盘 TXT 常见），不算新章；分卷后重名的章照常切
                if paragraphs:
                    yield title, paragraphs
                next_title = strip_site_suffix(line)
                if not paragraphs and _VOLUME.match(title):
                    next_title = f"{title} · {next_title}"
                title, paragraphs, last = next_title, [], line
            else:
                if not has_headings and paragraphs and length >= _TARGET and not joining:
                    yield title, paragraphs
                    number += 1
                    title, paragraphs, length = f"第 {number} 段", [], 0
                if joining and paragraphs and not rewrap.indented(raw):
                    paragraphs[-1] = rewrap.join(paragraphs[-1], line)
                else:
                    paragraphs.append(line)
                length += len(line)
                joining = rewrap.continues(raw, wrap)
    if paragraphs:
        yield title, paragraphs


def analyze(path: Path, title: str, author: str) -> TextInfo:
    encoding = _encoding(path)
    with path.open("r", encoding=encoding, newline=None) as stream:
        title, author = text_metadata(islice(stream, 30), title, author)
    with path.open("r", encoding=encoding, newline=None) as stream:
        # 至少 3 个标题才按章切：正文里偶然一行像标题时，不把整本切成两大章
        has_headings = sum(1 for line in stream if _heading(line.strip())) >= 3
    with path.open("r", encoding=encoding, newline=None) as stream:
        wrap = rewrap.detect_width(stream)
    candidates: list[tuple[str, ...]] = []
    for _, paragraphs in _chapters(path, encoding, has_headings, wrap):
        candidates.append(tuple({line for line in paragraphs if is_site_signature(line)}))
    signatures = repeated_site_signatures(candidates, len(candidates))
    return TextInfo(encoding, title, author, has_headings, frozenset(signatures), wrap)


def write(
    path: Path, info: TextInfo, epub_path: Path, txt_path: Path, style: str, font: str
) -> tuple[int, list[str]]:
    count = 0
    removed = 0
    with (
        EpubWriter(epub_path, title=info.title, style=style, font=font, cover=False) as epub,
        txt_path.open("x", encoding="utf-8", newline="\n") as text,
    ):
        for title, paragraphs in _chapters(path, info.encoding, info.has_headings, info.wrap):
            filtered = [line for line in paragraphs if line not in info.signatures]
            cleaned = clean_paragraphs(filtered, title=title)
            removed += len(paragraphs) - len(cleaned.paragraphs)
            if not cleaned.paragraphs:
                continue
            count += 1
            epub.add_chapter(EpubChapter(count, title, cleaned.paragraphs))
            text.write(title + "\n\n")
            for paragraph in cleaned.paragraphs:
                text.write(paragraph + "\n\n")
        if not count:
            raise ParseError("TXT 文件没有可用的正文")
    notes = [
        "已整理为 UTF-8",
        f"识别 {count} 章" if info.has_headings else f"分为 {count} 段",
        f"删除广告/站点提示及重复内容 {removed} 行",
    ]
    if info.wrap:
        notes.append(f"检测到每行约 {info.wrap // 2} 字的硬换行，已合并成段落")
    if not info.has_headings:
        notes.append("未识别到章节标题，按约 8000 字在段落边界分段")
    return count, notes
