"""按用户选择探查目录或单章；动态页面复用隔离浏览器，结果贯通任务执行。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..assemble.models import clean_metadata_text
from ..errors import ConfigError, ParseError, QuireError
from ..fetch.browser import RenderOptions, render_page
from ..fetch.session import AsyncFetcher
from ..fetch.simple import Response
from ..parse.article import extract_article, validate_article
from ..parse.chapters import discover_chapters, looks_like_catalogue
from ..parse.images import collect, prefilter
from ..parse.minidom import parse as parse_html
from ..parse.packed import extract_script_images
from ..parse.series import Volume, plan_volumes
from ..sites import bilibili
from ..sites.cleanup import clean_document
from ..sites.expand import expand_catalogue
from ..sites.kind_guard import check_placement
from ..sites.registry import cookie_hosts_for
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
    chapters: tuple[str, ...] = ()
    estimate_bytes: int | None = None
    estimates: dict[str, int] = field(default_factory=dict)
    sample_urls: tuple[str, ...] = ()  # 内部估算用,不暴露给前端


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
    obey_robots: bool = True,
    on_stage: Callable[[str], None] | None = None,
) -> ProbeResult:
    url = validate_task_url(url)
    validate_capture_mode(kind, capture_mode)
    check_placement(url, kind)
    rule = resolve_rule(data_root, url) if data_root else None
    chosen_kind = kind or (rule.kind if rule else None)

    def stage(name: str) -> None:
        if on_stage is not None:
            on_stage(name)

    def estimate_stage(result: ProbeResult) -> None:
        if result.kind == "manga":
            stage("estimate")

    async with AsyncFetcher(
        timeout=timeout,
        retries=1,
        concurrency=2,
        rate=rate,
        respect_robots=obey_robots,
        cookie_hosts=cookie_hosts_for(url),
    ) as client:
        stage("fetch")
        page = await client.get(url)
        page = await expand_catalogue(client, page)
        dynamic = bool(route_fragment(url)) or bilibili.is_reader_page(page.url)
        if not dynamic:
            stage("parse")
            try:
                result = _inspect(page, chosen_kind, capture_mode, split_by, rule)
            except ParseError:
                if not parse_html(page.text).select("script[src],script[type=module]"):
                    raise
            else:
                estimate_stage(result)
                estimate = await _estimate_bytes(client, result)
                return _confirm_placement(replace(result, estimate_bytes=estimate), kind)
        stage("render")
        if bilibili.is_reader_page(page.url):
            # B站阅读页:旁观式捕获首屏图片作样本,总页数读阅读器 UI。
            page, total = await bilibili.capture_reader_page(
                page.url, RenderOptions(timeout=60), full=False
            )
            stage("parse")
            result = _inspect(page, chosen_kind, capture_mode, split_by, rule)
            if total > result.count:
                result = replace(result, count=total)
        else:
            page, _ = await render_page(
                page,
                client,
                RenderOptions(timeout=60, max_scrolls=1000),
                content="images" if chosen_kind == "manga" and capture_mode == "single" else "text",
            )
            stage("parse")
            result = _inspect(page, chosen_kind, capture_mode, split_by, rule)
        estimate_stage(result)
        estimate = await _estimate_bytes(client, result)
        return _confirm_placement(replace(result, render=True, estimate_bytes=estimate), kind)


def _confirm_placement(result: ProbeResult, kind: str | None) -> ProbeResult:
    """实测内容形态与用户所选模块冲突时提示切换,而不是静默纠正(误放检测)。"""
    if kind is None or result.kind == kind:
        return result
    if result.kind == "manga":
        raise ConfigError(
            "这个链接识别出的是漫画内容,小说模块解析不出正文",
            hint="请切换到「漫画」标签,用目录或单章模式提交。",
        )
    raise ConfigError(
        "这个链接识别出的是小说内容,漫画模块解析不出图片",
        hint="请切换到「小说」标签,用目录或单章模式提交。",
    )


async def _sample_page_bytes(
    client: AsyncFetcher, urls: list[str], referer: str, estimates: dict[str, int]
) -> int | None:
    """实测前几张图片的平均字节数;样本最多 3 张,失败跳过,全失败返回 None。"""
    sizes: list[int] = []
    encoded: dict[str, list[int]] = {key: [] for key in ("archive", "balanced", "small")}
    for image_url in urls[:3]:
        try:
            response = await client.get(image_url, referer=referer, robots=False)
        except QuireError:
            continue
        sizes.append(len(response.content))
        from ..image.compress import encode_pages
        from ..image.options import CompressionOptions

        for key in encoded:
            options = CompressionOptions(preset=key, target_bytes=None)
            try:
                size = sum(
                    len(page.data)
                    for page in encode_pages(response.content, options.passes()[0], options)
                )
                encoded[key].append(size)
            except QuireError:
                continue
    estimates.update({key: sum(values) // len(values) for key, values in encoded.items() if values})
    return sum(sizes) // len(sizes) if sizes else None


async def _estimate_bytes(client: AsyncFetcher, result: ProbeResult) -> int | None:
    """体积预估:样本平均 × 页数;系列任务按首章页数 × 章数粗估。失败返回 None。"""
    if result.kind != "manga" or not result.sample_urls:
        return None
    try:
        if result.series:
            chapter = await client.get(result.sample_urls[0])
            urls = extract_script_images(chapter.text)
            if not urls:
                doc = parse_html(chapter.text, base_url=chapter.url)
                kept, _ = prefilter(collect(doc, doc.effective_base() or chapter.url))
                urls = [candidate.url for candidate in kept]
            if not urls:
                return None
            average = await _sample_page_bytes(client, urls, chapter.url, result.estimates)
            result.estimates.update(
                {k: v * len(urls) * result.count for k, v in result.estimates.items()}
            )
            return average * len(urls) * result.count if average else None
        average = await _sample_page_bytes(
            client, list(result.sample_urls), result.url, result.estimates
        )
        result.estimates.update({k: v * result.count for k, v in result.estimates.items()})
        return average * result.count if average else None
    except QuireError:
        return None


def _inspect(
    page: Response, kind: str | None, mode: str, split_by: str, rule: SiteRule | None
) -> ProbeResult:
    doc = parse_html(page.text, base_url=page.url)
    clean_document(doc, page.url)
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
            first_chapter = volumes[0].chapters[0].url if volumes and volumes[0].chapters else ""
            return ProbeResult(
                "manga",
                title,
                len(links),
                page.url,
                volumes,
                True,
                chapters=tuple(c.title for v in volumes for c in v.chapters),
                sample_urls=(first_chapter,) if first_chapter else (),
            )
        if len(links) > 20000:
            raise ParseError("目录超过 20000 章上限", hint="请拆分目录后再抓取。")
        return ProbeResult(
            "novel", title, len(links), page.url, chapters=tuple(c.title for c in links)
        )
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
    script_urls = extract_script_images(page.text) if kind != "novel" else []
    if "ac.qq.com/ComicView/" in page.url and not script_urls:
        raise ParseError("腾讯漫画正文清单未能解析，不能将装饰图作为正文")
    if kept or script_urls:
        # 与 discover_page 同一规则:脚本内嵌清单更长时以它为准(SPA 只渲染当前页)。
        script_urls = extract_script_images(page.text)
        urls = (
            script_urls
            if script_urls and (len(script_urls) > len(kept) or "ac.qq.com/ComicView/" in page.url)
            else [c.url for c in kept]
        )
        return ProbeResult("manga", title, len(urls), page.url, sample_urls=tuple(urls[:3]))
    raise ParseError(
        "这个链接没找到可用的正文或图片",
        hint="请确认选择的小说/漫画类型与链接一致，并使用对应的目录页或单章阅读页。",
    )
