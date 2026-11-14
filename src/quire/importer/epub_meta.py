"""导入时把用户确认的书名/作者写回 EPUB 的 OPF 与 NCX。

只替换 ``dc:title`` / 第一个 ``dc:creator`` / NCX ``docTitle`` ``docAuthor`` 的文字，标签属性和
文件其余部分原样保留；原书没有作者元素时在书名后补一个，作者清空时删掉第一个作者元素。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

_TITLE = re.compile(r"(<dc:title\b[^>]*>).*?(</dc:title\s*>)", re.S)
_CREATOR = re.compile(r"(\s*<dc:creator\b[^>]*>).*?(</dc:creator\s*>)", re.S)
_NCX_TITLE = re.compile(r"(<docTitle\b[^>]*>\s*<text\b[^>]*>).*?(</text\s*>)", re.S)
_NCX_AUTHOR = re.compile(r"(<docAuthor\b[^>]*>\s*<text\b[^>]*>).*?(</text\s*>)", re.S)


@dataclass(frozen=True, slots=True)
class Retitle:
    """``None`` 表示该字段沿用原书。"""

    title: str | None = None
    author: str | None = None


def _put(pattern: re.Pattern[str], markup: str, value: str) -> str:
    text = html.escape(value, quote=False)
    return pattern.sub(lambda m: m[1] + text + m[2], markup, count=1)


def retitle_opf(markup: str, change: Retitle) -> str:
    if change.title is not None:
        markup = _put(_TITLE, markup, change.title)
    if change.author is None:
        return markup
    if not change.author:
        return _CREATOR.sub("", markup, count=1)
    if _CREATOR.search(markup):
        return _put(_CREATOR, markup, change.author)
    creator = f"<dc:creator>{html.escape(change.author, quote=False)}</dc:creator>"
    return _TITLE.sub(lambda m: m[0] + "\n    " + creator, markup, count=1)


def retitle_ncx(markup: str, change: Retitle) -> str:
    if change.title is not None:
        markup = _put(_NCX_TITLE, markup, change.title)
    if change.author:
        markup = _put(_NCX_AUTHOR, markup, change.author)
    return markup
