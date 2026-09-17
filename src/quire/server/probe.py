"""按用户选择探查目录或单章；动态页面复用隔离浏览器，结果贯通任务执行。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from ..assemble.models import clean_metadata_text
from ..errors import ConfigError, ParseError
from ..fetch.browser import RenderOptions, render_page
from ..fetch.session import AsyncFetcher
from ..fetch.simple import Response
from ..parse.article import extract_article, validate_article
from ..parse.chapters import discover_chapters, looks_like_catalogue
from ..parse.images import collect, prefilter
from ..parse.minidom import parse as parse_html
from ..parse.series import Volume, plan_volumes
from ..sites.rules import SiteRule, resolve_rule
from ..utils.urls import is_usable_url, route_fragment


@dataclass(frozen=True, slots=True)
class ProbeResult:
    kind: str
    title: str
    count: int
    url: str
    volumes: tuple[Volume, ...] = ()
    series: bool = False
    render: bool = False


def validate_task_url(url: str) -> str:
    """任务地址的后端复核：仅 http(s)、不含凭据与控制字符。"""
    from urllib.parse import urlsplit

    candidate = url.strip()
    parts = urlsplit(candidate)
    if (
        not is_usable_url(candidate)
        or parts.scheme not in {"http", "https"}
        or parts.username is not None
        or parts.password is not None
        or any(ord(c) < 32 for c in candidate)
    ):
        raise ParseError("这个地址不是有效的 http(s) 链接", hint="粘贴漫画或小说的目录页地址。")
    return candidate


def validate_capture_mode(kind: str | None, mode: str) -> None:
    if kind is not None and kind not in ("manga", "novel"):
        raise ConfigError("任务类型须为 manga 或 novel")
    if mode not in ("auto", "catalogue", "single"):
        raise ConfigError("采集模式须为 auto、catalogue 或 single")


async def probe_url(
    url: str,
    *,
    rate: float = 4.0,
    timeout: float = 20.0,
    data_root: Path | None = None,
    kind: str | None = None,
    split_by: str = "volume",
    capture_mode: str = "auto",
) -> ProbeResult:
    url = validate_task_url(url)
    validate_capture_mode(kind, capture_mode)
    rule = resolve_rule(data_root, url) if data_root else None
    chosen_kind = kind or (rule.kind if rule else None)
    async with AsyncFetcher(timeout=timeout, retries=1, concurrency=2, rate=rate) as client:
        page = await client.get(url)
        dynamic = bool(route_fragment(url))
        if not dynamic:
            try:
                return _inspect(page, chosen_kind, capture_mode, split_by, rule)
            except ParseError:
                if not parse_html(page.text).select("script[src],script[type=module]"):
                    raise
        page, _ = await render_page(
            page,
            client,
            RenderOptions(timeout=60, max_scrolls=1000),
            content="images" if chosen_kind == "manga" and capture_mode == "single" else "text",
        )
        return replace(_inspect(page, chosen_kind, capture_mode, split_by, rule), render=True)


def _inspect(
    page: Response, kind: str | None, mode: str, split_by: str, rule: SiteRule | None
) -> ProbeResult:
    doc = parse_html(page.text, base_url=page.url)
    title_node = doc.select_one("h1") or doc.select_one("title")
    title = clean_metadata_text(title_node.text if title_node else "") or "未命名"
    links = discover_chapters(
        doc, page.url, selector=rule.chapter_links if rule else None, limit=20001
    )
    catalogue = bool(links) if mode == "catalogue" else looks_like_catalogue(links)
    if mode != "single" and catalogue:
        if kind == "manga":
            volumes, _ = plan_volumes(
                doc,
                page.url,
                split_by=split_by,
                chapter_selector=rule.chapter_links if rule else None,
                volume_selector=rule.volume_selector if rule else None,
                order=rule.chapter_order if rule else "auto",
            )
            return ProbeResult("manga", title, len(links), page.url, volumes, True)
        if len(links) > 20000:
            raise ParseError("目录超过 20000 章上限", hint="请拆分目录后再抓取。")
        return ProbeResult("novel", title, len(links), page.url)
    if mode == "catalogue":
        raise ParseError("这个链接没找到章节列表", hint="请检查目录地址，或切换到单章抓取。")
    if rule:
        for selector in rule.remove:
            for node in doc.select(selector):
                if node.parent is not None:
                    node.parent.children.remove(node)
    if kind != "manga":
        article = extract_article(
            doc, selector=rule.content_selector if rule else None, title=title
        )
        if validate_article(article)[0]:
            return ProbeResult("novel", title, 1, page.url)
    kept, _ = prefilter(
        collect(
            doc,
            doc.effective_base() or page.url,
            selector=rule.image_selector if rule else None,
            attrs=rule.image_attrs if rule else None,
        )
    )
    if kept and kind == "novel":
        return ProbeResult("novel", title, 1, page.url)
    if kept:
        return ProbeResult("manga", title, len(kept), page.url)
    raise ParseError(
        "这个链接没找到可用的正文或图片",
        hint="请确认选择的小说/漫画类型与链接一致，并使用对应的目录页或单章阅读页。",
    )
