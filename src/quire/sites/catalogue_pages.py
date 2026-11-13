"""通用目录分页合并。

移动版书站常把目录切成每页 20 章：页内有 ``<select>`` 列出全部分页（so-novel 的经验：
大多数分页目录都能从 ``select > option`` 一次拿全），否则只有「下一页」。这里把各分页的
章节合并成一页交给通用识别。阅读页（有「下一章」）不处理；任何一页读取失败都退回原页面，
宁可只给当前页，也不交出中间缺章的目录。
"""

from __future__ import annotations

import asyncio
import logging
import re
from html import escape
from typing import Protocol
from urllib.parse import urlsplit

from ..errors import FetchError
from ..fetch.simple import Response
from ..parse.catalog_links import CATALOGUE_TOKEN, chapter_signature
from ..parse.chapters import ChapterLink, classify_next, discover_chapters
from ..parse.minidom import Document
from ..parse.minidom import parse as parse_html
from ..utils.urls import is_usable_url
from ..utils.urls import join_document_url as join_url

_LOG = logging.getLogger(__name__)
MAX_PAGES = 300
_NEXT = re.compile(r"(?:下一?[页頁]|后一?页|次页|next(?:page)?)[>»›]*", re.I)


class _Client(Protocol):
    async def get(
        self, url: str, *, referer: str | None = None, headers: dict[str, str] | None = None
    ) -> Response: ...


def _same_site(url: str | None, page_url: str) -> bool:
    return (
        bool(url) and is_usable_url(url or "") and urlsplit(url).netloc == urlsplit(page_url).netloc
    )


def option_pages(doc: Document, page_url: str) -> list[str]:
    """下拉框里的目录分页地址（同站、同一地址形态、选项文字带数字）；没有就返回空。"""
    for select in doc.select("select"):
        urls: list[str] = []
        for option in select.select("option"):
            url = join_url(page_url, option.get("value") or "")
            if _same_site(url, page_url) and any(c.isdigit() for c in option.text) and url:
                if url not in urls:
                    urls.append(url)
        if len(urls) >= 2 and len({chapter_signature(u) for u in urls}) == 1:
            return urls[:MAX_PAGES]
    return []


def next_page(doc: Document, page_url: str) -> str | None:
    for node in doc.iter_elements():
        # 只认整段就是「下一页」的按钮：章节名里的「最后一页」也会被宽松匹配命中
        if node.tag == "a" and _NEXT.fullmatch(re.sub(r"\s+", "", node.text)):
            url = join_url(page_url, node.get("href") or "")
            if _same_site(url, page_url) and url != page_url:
                return url
    return None


def _is_reader(doc: Document) -> bool:
    return any(n.tag == "a" and classify_next(n.text) == "chapter" for n in doc.iter_elements())


def _chapters(page: Response) -> tuple[Document, tuple[ChapterLink, ...]]:
    doc = parse_html(page.text, base_url=page.url)
    return doc, discover_chapters(doc, page.url, limit=20001)


async def expand_pages(client: _Client, page: Response) -> Response:
    doc, first = _chapters(page)
    if len(first) < 2 or _is_reader(doc):
        return page
    pages: list[tuple[ChapterLink, ...]] = []
    try:
        urls = option_pages(doc, page.url)
        if urls:
            others = [u for u in urls if u != page.url]
            fetched = await asyncio.gather(*(client.get(u, referer=page.url) for u in others))
            by_url = dict(zip(others, (_chapters(r)[1] for r in fetched), strict=True))
            # 书页本身不在分页里时不并入它：书页常带「最新章节」块，会打乱顺序
            pages = [by_url.get(u, first) for u in urls]
        else:
            pages, current, seen = [first], doc, {page.url}
            while (target := next_page(current, page.url)) and len(pages) < MAX_PAGES:
                if target in seen:
                    break
                seen.add(target)
                current, links = _chapters(await client.get(target, referer=page.url))
                if not links:
                    break
                pages.append(links)
    except FetchError as exc:
        _LOG.warning("目录分页读取失败，按当前页识别：%s", exc)
        return page
    merged = list({link.url: link for links in pages for link in links}.values())
    if len(merged) <= len(first):
        return page
    return rebuild(page, doc, [(link.url, link.title) for link in merged])


def rebuild(
    page: Response, doc: Document, links: list[tuple[str, str]], *, trusted: bool = False
) -> Response:
    """用整理好的章节（阅读顺序）重建目录页，书名与 <title> 沿用原页。

    ``trusted``：章节来自站点接口（Madara、漫城 CMS），带令牌整份照收，不再二次筛选。
    """
    h1 = doc.select_one("h1")
    heading = f"<h1>{escape(h1.text.strip())}</h1>" if h1 is not None and h1.text.strip() else ""
    items = "".join(f'<li><a href="{escape(url)}">{escape(title)}</a></li>' for url, title in links)
    box = (
        f'<ul id="quire-catalogue" data-quire="{CATALOGUE_TOKEN}">{items}</ul>'
        if trusted
        else f'<div class="listmain"><ul>{items}</ul></div>'
    )
    html = f"<html><head><title>{escape(doc.title.strip())}</title></head><body>{heading}{box}</body></html>"
    headers = {**page.headers, "content-type": "text/html; charset=utf-8"}
    return Response(page.url, page.status, headers, html.encode("utf-8"), page.elapsed_ms)
