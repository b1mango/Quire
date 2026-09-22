"""小说采集的目录规划:从入口页决定抓哪些章节（项目设计.md §37）。

目录页按目录抓,单章页只抓这一章;"单章入口"需要章节标题、前后章导航
与有效正文共同证明,避免把简介页当正文。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .assemble.models import clean_metadata_text
from .errors import NoChaptersError
from .fetch.simple import Response
from .models import NovelOptions
from .parse.article import extract_article, page_title, validate_article
from .parse.chapter_range import parse_ranges, select_range, select_ranges
from .parse.chapters import ChapterLink, discover_chapters, looks_like_catalogue
from .parse.minidom import Document
from .parse.minidom import parse as parse_html
from .sites.cleanup import clean_document
from .text.clean import chapter_number


@dataclass(frozen=True, slots=True)
class Plan:
    title: str
    links: tuple[ChapterLink, ...]
    preloaded: dict[str, Response]
    warnings: tuple[str, ...]
    truncated: bool = False


def plan_chapters(page: Response, opts: NovelOptions, warnings: Sequence[str] = ()) -> Plan:
    """从入口页决定抓哪些章节：目录页按目录，单章页只抓这一章。"""
    doc = parse_html(page.text, base_url=page.url)
    clean_document(doc, page.url)
    title = clean_metadata_text(page_title(doc, fallback="未命名")) or "未命名"
    links = discover_chapters(
        doc, page.url, selector=opts.chapter_selector, limit=opts.max_chapters + 1
    )
    notes = list(warnings)
    if opts.capture_mode == "single":
        return Plan(title, (ChapterLink(title, page.url),), {page.url: page}, tuple(notes))
    if (opts.chapter_selector or opts.capture_mode == "catalogue") and not links:
        raise NoChaptersError(page.url)
    single_entry = (
        opts.capture_mode == "auto"
        and not opts.chapter_selector
        and (not looks_like_catalogue(links) or _chapter_entry(doc, title, links, opts))
    )
    if single_entry:
        single = ChapterLink(title=title, url=page.url, number=None)
        return Plan(title, (single,), {page.url: page}, tuple(notes))
    truncated = len(links) > opts.max_chapters
    if truncated:
        links = links[: opts.max_chapters]
        notes.append(f"章节数超过上限 {opts.max_chapters}，只抓前 {opts.max_chapters} 章")
    if opts.chapter_ranges:
        links = select_ranges(links, parse_ranges(opts.chapter_ranges))
    else:
        links = select_range(links, opts.chapter_first, opts.chapter_last)
    return Plan(title, links, {}, tuple(notes), truncated)


def _chapter_entry(
    doc: Document, title: str, links: tuple[ChapterLink, ...], opts: NovelOptions
) -> bool:
    """章节标题、前后章导航及有效正文共同证明入口是单章，避免把简介当正文。"""
    number = chapter_number(title)
    if number is None or not links or len(links) > 2:
        return False
    if any(link.number not in {number - 1, number + 1} for link in links):
        return False
    article = extract_article(doc, selector=opts.content_selector, title=title)
    return validate_article(article)[0]
