"""Series detection and deterministic chapter/volume selection, without I/O."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..errors import ConfigError, NoChaptersError
from ..image.options import parse_size
from ..utils.urls import join_url
from .chapters import ChapterLink, discover_chapters
from .minidom import Document

_VOLUME = re.compile(
    r"(?:第\s*[0-9零〇一二三四五六七八九十百千]+\s*卷|vol\.?\s*\d+|卷之\s*\S+)", re.I
)


@dataclass(frozen=True, slots=True)
class Volume:
    index: int
    title: str
    chapters: tuple[ChapterLink, ...]


def split_spec(value: str) -> tuple[str, int]:
    parts = value.split()
    if parts in (["none"], ["volume"]):
        return parts[0], 0
    if len(parts) == 2:
        if parts[0] == "chapters" and parts[1].isdigit() and 1 <= int(parts[1]) <= 20000:
            return "chapters", int(parts[1])
        if parts[0] == "size":
            return "size", parse_size(parts[1])
    raise ConfigError("分卷须为 none、volume、chapters N 或 size 50MB")


def plan_volumes(
    doc: Document,
    url: str,
    *,
    split_by: str = "none",
    chapter_selector: str | None = None,
    volume_selector: str | None = None,
    first: int = 1,
    last: int = 0,
    fallback_chapters: int = 20,
    order: str = "auto",
) -> tuple[tuple[Volume, ...], tuple[str, ...]]:
    mode, amount = split_spec(split_by)
    if (
        type(first) is not int
        or type(last) is not int
        or first < 1
        or last < 0
        or (last and last < first)
    ):
        raise ConfigError("章节范围无效")
    if not 1 <= fallback_chapters <= 20000:
        raise ConfigError("回退分卷章数须为 1-20000")
    links = discover_chapters(doc, url, selector=chapter_selector, limit=20001)
    if len(links) > 20000:
        raise ConfigError("系列超过 20000 章，请缩小目录范围")
    if order == "desc":
        links = tuple(reversed(links))
    elif order == "dom":
        positions = {
            join_url(doc.effective_base() or url, a.get("href") or ""): i
            for i, a in enumerate(doc.select(chapter_selector or "a[href]"))
        }
        links = tuple(sorted(links, key=lambda link: positions.get(link.url, 0)))
    elif order not in {"auto", "asc"}:
        raise ConfigError("目录排序参数无效")
    links = links[first - 1 : last or None]
    if not links:
        raise NoChaptersError(url)
    groups: list[tuple[str, list[ChapterLink]]] = []
    warnings: tuple[str, ...] = ()
    if mode == "volume":
        labels: dict[str, str] = {}
        if volume_selector:
            for container in doc.select(volume_selector):
                heading = container.select_one("h1,h2,h3,h4")
                title = container.get("data-title") or (heading.text if heading else "")
                if title.strip():
                    for anchor in container.select("a[href]"):
                        labels[join_url(doc.effective_base() or url, anchor.get("href") or "")] = (
                            title.strip()
                        )
        for link in links:
            matched = _VOLUME.search(link.title)
            label = labels.get(link.url) or (matched[0] if matched else "")
            if not label:
                groups = []
                warnings = (
                    f"卷边界不完整，已按每 {fallback_chapters} 章分卷；可配置 catalogue.volumes",
                )
                break
            if not groups or groups[-1][0] != label:
                groups.append((label, []))
            groups[-1][1].append(link)
        if not groups:
            mode, amount = "chapters", fallback_chapters
    if mode in {"none", "size"}:
        groups = [("全书", list(links))]
    elif mode == "chapters":
        groups = [("", list(links[i : i + amount])) for i in range(0, len(links), amount)]
    width = len(str(len(groups)))
    return tuple(
        Volume(i, title or f"第{i:0{width}d}卷", tuple(chapters))
        for i, (title, chapters) in enumerate(groups, 1)
    ), warnings
