"""TXT 输出：最朴素的成品，任何阅读器都能打开（项目设计.md §37.6）。

不做花活：不塞首行缩进的空格（缩进该由阅读器决定）、不写页码、不加分隔线。
章节之间空三行、段落之间空一行，是绝大多数阅读器与转换工具都认的格式。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from .models import NovelChapter

__all__ = ["render_txt", "write_txt"]


def render_txt(title: str, chapters: Sequence[NovelChapter]) -> str:
    """拼出完整文本。章节失败时写明原因，不假装这一章不存在。"""
    blocks: list[str] = [title.strip() or "未命名"]
    for chapter in chapters:
        lines = [chapter.heading]
        if chapter.missing_reason is not None:
            lines.append(f"［本章抓取失败：{chapter.missing_reason}］")
            lines.append(f"［地址：{chapter.source_url}］")
        else:
            lines.extend(chapter.paragraphs)
        blocks.append("\n\n".join(lines))
    return "\n\n\n\n".join(blocks) + "\n"


def write_txt(path: Path, *, title: str, chapters: Sequence[NovelChapter]) -> int:
    """写入候选文件（不负责原子发布），返回字节数。"""
    data = render_txt(title, chapters).encode("utf-8")
    with path.open("xb") as stream:
        stream.write(data)
    return len(data)
