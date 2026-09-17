"""Self-contained, escaped novel typography for Chrome's A5 PDF output."""

from __future__ import annotations

from collections.abc import Sequence
from html import escape

from ..errors import ConfigError
from .models import NovelChapter, clean_metadata_text

MAX_HTML_BYTES = 32 * 1024 * 1024

_STYLE = """
@page { size: A5; margin: 18mm; }
html { font-family: "Songti SC", "PingFang SC", serif; color: #111; background: #fff; }
body { margin: 0; font-size: 11pt; line-height: 1.8; letter-spacing: 0; }
h1 { font-size: 22pt; line-height: 1.4; margin: 0 0 12mm; }
h2 { font-size: 16pt; line-height: 1.5; margin: 0 0 8mm; break-after: avoid; }
h1, h2, a, p { overflow-wrap: anywhere; }
nav h2 { font-size: 14pt; margin-bottom: 5mm; }
nav ol { list-style: none; margin: 0; padding: 0; }
nav li { margin: 0 0 3mm; break-inside: avoid; }
a { color: inherit; text-decoration: none; }
section { break-before: page; }
p { margin: 0 0 0.7em; text-indent: 2em; white-space: pre-wrap; orphans: 2; widows: 2; }
p.missing, p.notice { text-indent: 0; color: #333; }
"""


def _text(value: str) -> str:
    if len(value) > MAX_HTML_BYTES:
        raise ConfigError("小说 HTML 超过 32 MiB 限制")
    return escape(clean_metadata_text(value), quote=True)


def render_html(*, title: str, chapters: Sequence[NovelChapter]) -> str:
    """Render chapters in supplied order; generated anchors never use external text."""
    if not chapters:
        raise ConfigError("小说 PDF 至少需要一章")
    parts: list[str] = []
    size = 0

    def append(part: str) -> None:
        nonlocal size
        size += len(part.encode("utf-8"))
        if size > MAX_HTML_BYTES:
            raise ConfigError("小说 HTML 超过 32 MiB 限制")
        parts.append(part)

    heading = _text(title) or "未命名作品"
    append(
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        "style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">"
        f"<title>{heading}</title><style>{_STYLE}</style></head><body>"
        f'<h1>{heading}</h1><nav aria-label="目录"><h2>目录</h2><ol>'
    )
    for position, chapter in enumerate(chapters, 1):
        name = _text(chapter.title).strip() or f"第 {chapter.index} 章"
        append(f'<li><a href="#chapter-{position}">{name}</a></li>')
    append("</ol></nav><main>")
    for position, chapter in enumerate(chapters, 1):
        name = _text(chapter.title).strip() or f"第 {chapter.index} 章"
        append(f'<section id="chapter-{position}"><h2>{name}</h2>')
        if chapter.missing_reason is not None:
            reason = _text(chapter.missing_reason) or "正文未能获取"
            append(f'<p class="missing">本章抓取失败：{reason}</p>')
        else:
            for paragraph in chapter.paragraphs:
                append(f"<p>{_text(paragraph)}</p>")
            if chapter.truncated:
                append('<p class="notice">本章正文已截断。</p>')
        append("</section>")
    append("</main></body></html>")
    return "".join(parts)
