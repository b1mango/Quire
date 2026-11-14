"""EPUB 导入清洗：删掉章节文件里整段都是广告/站点提示的段落，其余原样保留。

网盘 EPUB 多由 TXT 转制，章首章尾常夹「更多精校小说尽在…下载」、长分隔线等。这里只动
章节 XHTML 中**不含同名嵌套**的 ``p/div/h1–h6/li`` 块：块内纯文本整段命中
``text.noise.noise_rule`` 才删；样式、图片、目录、元数据与其他条目逐字节照抄（含 mimetype
排第一、不压缩的约定）。解不出 UTF-8 的章节文件原样保留。
"""

from __future__ import annotations

import html
import re
import shutil
from pathlib import Path
from zipfile import ZipFile, ZipInfo

from ..reader.archive import read_entry
from ..text.noise import noise_rule
from .epub_meta import Retitle, retitle_ncx, retitle_opf

_BLOCK = re.compile(r"<(p|div|h[1-6]|li)\b[^>]*>((?:(?!<\1\b).)*?)</\1\s*>\s*", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_PAGES = (".xhtml", ".html", ".htm")
_NAV = ("nav.xhtml", "toc.xhtml", "nav.html", "toc.html")


def _clean_markup(markup: str) -> tuple[str, int]:
    removed = 0

    def drop(match: re.Match[str]) -> str:
        nonlocal removed
        text = html.unescape(_TAG.sub("", match[2])).strip()
        if text and noise_rule(text):
            removed += 1
            return ""
        return match[0]

    return _BLOCK.sub(drop, markup), removed


def _retitle(name: str, data: bytes, change: Retitle) -> bytes:
    lower = name.lower()
    rewrite = retitle_opf if lower.endswith(".opf") else retitle_ncx
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    updated = rewrite(text, change)
    return updated.encode("utf-8") if updated != text else data


def clean_epub(source: Path, target: Path, change: Retitle | None = None) -> int:
    """把 ``source`` 清洗后写到 ``target``（须不存在），返回删除的段落数。

    ``change`` 给出时，把用户确认的书名/作者写进 OPF 与 NCX。
    """
    removed = 0
    with ZipFile(source) as archive, ZipFile(target, "x") as out:
        for info in archive.infolist():
            copy = ZipInfo(info.filename, info.date_time)
            copy.compress_type, copy.external_attr = info.compress_type, info.external_attr
            if info.filename.lower().endswith(_PAGES) and not info.is_dir():
                data = read_entry(archive, info.filename)
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    out.writestr(copy, data)
                    continue
                if 'epub:type="toc"' in text or info.filename.lower().endswith(_NAV):
                    out.writestr(copy, data)  # 导航文档里的「目录」标题不是广告
                    continue
                markup, count = _clean_markup(text)
                removed += count
                out.writestr(copy, markup.encode("utf-8") if count else data)
                continue
            if change and info.filename.lower().endswith((".opf", ".ncx")):
                out.writestr(
                    copy, _retitle(info.filename, read_entry(archive, info.filename), change)
                )
                continue
            with archive.open(info) as src, out.open(copy, "w") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
    return removed
