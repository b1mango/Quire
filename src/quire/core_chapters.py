"""账本驱动的章节抓取：一章一个资源，缓存清洗后的正文（项目设计.md §37.5）。

缓存里存的是**清洗后的结构化正文**（JSON），不是原始 HTML：恢复时既不必
重新联网，也不必重新抽取。原始 HTML 只在 ``--keep-html`` 时另存，用于调规则。

一章只有在"分页链正常走到头"时才算完成：中途某页网络失败会把整章记为失败，
下次 ``--resume`` 重取这一章，而不是把半章当完整章节交付。

OCR（§6.7）：文本优先——``auto`` 模式只在正文校验失败且页面是图片正文时
才识别；``always`` 跳过文本抽取直接识别。识别结果是正文的一种来源，
缓存里记 ``source`` 与低置信行，导出期据此生成 review.txt 与跨页去重。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlsplit

from .core_chapter_cache import CACHE_SCHEMA
from .errors import BlockedError, FetchError, ParseError
from .fetch.simple import Response
from .models import NovelOptions
from .novel_fonts import FontCache, deobfuscate_page
from .ocr.postprocess import collect_review
from .ocr.tesseract import OcrEngineError
from .ocr.trigger import should_ocr
from .parse.article import (
    VALIDATION_MESSAGES,
    extract_article,
    page_title,
    validate_article,
)
from .parse.chapters import ChapterLink, PageKey, find_next_page, page_key
from .parse.minidom import parse as parse_html
from .sites.cleanup import clean_document
from .store.cache import publish_bytes
from .store.export_files import _directory
from .store.ledger import Ledger
from .store.models import FailureCode, ResourceRecord, TaskSnapshot
from .text.clean import chapter_number, clean_paragraphs, has_chapter_mark
from .workspace import write_bytes

if TYPE_CHECKING:
    from .ocr.capture import OcrRunner

#: 分页超限时写入成品的说明：宁可读者看到"可能不完整"，也不能静默截断。
TRUNCATION_NOTE = "［本章内容可能不完整：分页超过设定上限］"

#: 资源失败码 → 给用户看的说明。
FAILURE_TEXT = {
    "network": "网络失败或超时",
    "blocked": "站点拒绝访问（robots 或反爬）",
    "invalid_text": "正文校验未通过",
    "invalid_image": "资源格式不支持",
    "cancelled": "任务已取消",
}


class ChapterFetcher(Protocol):
    async def get(
        self, url: str, *, referer: str | None = None, robots: bool = True
    ) -> Response: ...


class Renderer(Protocol):
    """把一页交给浏览器渲染后再取回。由编排层注入，本模块不依赖具体浏览器。"""

    async def __call__(self, page: Response) -> tuple[Response, tuple[str, ...]]: ...


@dataclass(slots=True)
class _Stats:
    pages: int = 0
    completed: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CaptureResult:
    snapshot: TaskSnapshot
    reused: int
    pages_fetched: int
    warnings: tuple[str, ...]
    reused_chapters: frozenset[int] = frozenset()


async def capture_chapters(
    client: ChapterFetcher,
    ledger: Ledger,
    task_id: str,
    links: tuple[ChapterLink, ...],
    *,
    options: NovelOptions,
    render: Renderer | None = None,
    ocr: OcrRunner | None = None,
    preloaded: dict[str, Response] | None = None,
    page_fetcher: ChapterFetcher | None = None,
    html_dir: Path | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    stop: Callable[[], bool] | None = None,
) -> CaptureResult:
    """并发抓取未完成的章节。已在账本里标记完成的章节不重新请求。"""
    snapshot = ledger.snapshot(task_id)
    already = frozenset(item.spec.chapter for item in snapshot.resources if item.status == "done")
    reused = len(already)
    if snapshot.status == "done":
        return CaptureResult(snapshot, reused, 0, (), already)
    ledger.start(task_id)
    stats = _Stats()
    stats.completed = reused
    seed = preloaded or {}
    fonts = FontCache()  # 一本书共用一份混淆字体,映射只解析一次
    total = len(links)
    chapter_urls = frozenset(page_key(link.url) for link in links)
    pending = iter(
        (record, link)
        for record, link in zip(snapshot.resources, links, strict=True)
        if record.status == "pending"
    )

    async def worker() -> None:
        for record, link in pending:
            if stop is not None and stop():
                break  # 暂停：不再派发新章节，进行中的章节已完整落定
            spec = record.spec
            ledger.claim(task_id, spec.chapter, spec.page)
            try:
                await _capture_one(
                    client,
                    ledger,
                    task_id,
                    record,
                    link,
                    options,
                    render,
                    ocr,
                    seed,
                    stats,
                    html_dir,
                    chapter_urls,
                    fonts,
                    page_fetcher,
                )
            except asyncio.CancelledError:
                ledger.fail(task_id, spec.chapter, spec.page, "cancelled")
                raise
            stats.completed += 1
            if on_progress is not None:
                on_progress(stats.completed, total)

    tasks = [asyncio.create_task(worker()) for _ in range(min(options.concurrency, len(links)))]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return CaptureResult(
        ledger.snapshot(task_id) if stop is not None and stop() else ledger.finish(task_id),
        reused,
        stats.pages,
        tuple(dict.fromkeys(stats.warnings)),
        already,
    )


async def _capture_one(
    client: ChapterFetcher,
    ledger: Ledger,
    task_id: str,
    record: ResourceRecord,
    link: ChapterLink,
    opts: NovelOptions,
    render: Renderer | None,
    ocr: OcrRunner | None,
    preloaded: dict[str, Response],
    stats: _Stats,
    html_dir: Path | None,
    chapter_urls: frozenset[PageKey],
    fonts: FontCache,
    page_fetcher: ChapterFetcher | None = None,
) -> None:
    chapter = record.spec.chapter
    failure: FailureCode | None = None
    title = link.title
    number = link.number or chapter_number(title)
    first_key = page_key(link.url)
    paragraphs: list[str] = []
    review: list[tuple[str, float]] = []
    ocr_used = False
    visited = {link.url}
    url = link.url
    pages = 0
    following: str | None = None
    while url and pages < opts.max_pages:
        page = preloaded.get(url) if pages == 0 else None
        cached = page is not None
        if not cached:
            stats.pages += 1  # 统计"发出过的页面请求"，失败重试也算，便于核对恢复行为
        try:
            if page is None:
                page = await (page_fetcher or client).get(url, referer=opts.referer)
            if render is not None and not cached:
                page, warnings = await render(page)
                stats.warnings.extend(warnings)
        except BlockedError:
            failure = "blocked"
            break
        except FetchError:
            failure = "network"
            break
        if len(page.content) > opts.max_bytes:
            failure = "invalid_text"
            break
        source, target = urlsplit(link.url), urlsplit(page.url)
        if (
            source.scheme,
            source.hostname,
            source.port or (443 if source.scheme == "https" else 80),
        ) != (
            target.scheme,
            target.hostname,
            target.port or (443 if target.scheme == "https" else 80),
        ):
            failure = "invalid_text"
            stats.warnings.append(f"第{chapter}章跳转到其他站点，未采纳正文")
            break
        if pages and page_key(page.url) != first_key and page_key(page.url) in chapter_urls:
            stats.warnings.append(f"第{chapter}章分页跳转到其他章节，已停止拼接")
            following = None
            break
        try:
            page = await deobfuscate_page(client, page, fonts)
        except ParseError as exc:
            # 字体混淆还原失败：如实记失败，不把乱码缓存成正文。
            failure = "invalid_text"
            stats.warnings.append(f"第{chapter}章：{exc.message}")
            break
        if html_dir is not None:
            with _directory(html_dir):
                write_bytes(html_dir / f"{chapter:05d}-{pages + 1:03d}.html", page.content)
        doc = parse_html(page.text, base_url=page.url)
        clean_document(doc, page.url)
        page_number = chapter_number(page_title(doc))
        if pages and number is not None and page_number is not None and page_number != number:
            stats.warnings.append(f"第{chapter}章续页章号发生变化，已停止拼接")
            following = None
            break
        if pages == 0 and page_number is not None:
            number = page_number
        extracted: tuple[str, ...] | None = None
        if ocr is not None and opts.ocr_mode == "always":
            pass  # --ocr always：跳过文本抽取，强制 OCR（项目设计.md §6.7）
        else:
            try:
                article = extract_article(
                    doc, page.url, selector=opts.content_selector, continuation=pages > 0
                )
            except ParseError as exc:
                # 选择器在某一章没命中：记这一章失败并继续，最终 0 章成功时再报错。
                failure = "invalid_text"
                stats.warnings.append(f"第{chapter}章：{exc.message}")
                break
            usable, code = validate_article(article, continuation=pages > 0)
            if usable:
                extracted = article.paragraphs
                if pages == 0 and article.title and not has_chapter_mark(title):
                    # 目录里的标题通常比正文页 <h1> 更有信息量（后者常只是"书名/第N回"），
                    # 所以只在目录标题没有章节标记时才用页面标题兜底。
                    title = article.title
            else:
                reason = VALIDATION_MESSAGES.get(code, code)
                if ocr is None or not should_ocr(doc, opts.content_selector):
                    failure = "invalid_text"
                    stats.warnings.append(f"第{chapter}章第{pages + 1}页正文未通过校验：{reason}")
                    break
                stats.warnings.append(
                    f"第{chapter}章第{pages + 1}页文本抽取失败（{reason}），改用 OCR"
                )
        if extracted is None:
            # 文本优先，抽不到才 OCR；OCR 失败同样按整章失败处理，交给 --resume。
            assert ocr is not None
            try:
                ocr_result = await ocr.recognize_page(doc, page.url, client, referer=opts.referer)
            except BlockedError:
                failure = "blocked"
                break
            except FetchError:
                failure = "network"
                break
            except OcrEngineError as exc:
                failure = "invalid_text"
                stats.warnings.append(f"第{chapter}章 OCR 失败：{exc.message}")
                break
            stats.warnings.extend(ocr_result.warnings)
            if not ocr_result.paragraphs:
                failure = "invalid_text"
                stats.warnings.append(f"第{chapter}章第{pages + 1}页 OCR 未取得正文")
                break
            extracted = ocr_result.paragraphs
            ocr_used = True
            review.extend(
                (entry.text, entry.confidence)
                for entry in collect_review(ocr_result.lines, chapter)
            )
        paragraphs.extend(extracted)
        pages += 1
        following = find_next_page(doc, page.url, page.url, selector=opts.next_selector)
        if following and page_key(following) != first_key and page_key(following) in chapter_urls:
            stats.warnings.append(f"第{chapter}章分页指向其他章节，已停止拼接")
            following = None
        if following in visited:
            failure = "invalid_text"
            stats.warnings.append(f"第{chapter}章分页形成循环，需检查站点规则")
            break
        if not following:
            following = None
            break
        visited.add(following)
        url = following

    truncated = following is not None and pages >= opts.max_pages
    if truncated:
        stats.warnings.append(
            f"第{chapter}章超过 --max-pages {opts.max_pages}，只抓了前 {pages} 页"
        )

    if failure is not None:
        # 分页链中途失败也算整章失败：半章不能当完整章节交付，交给 --resume 重取。
        ledger.fail(task_id, chapter, 1, failure)
        return
    cleaned = clean_paragraphs(tuple(paragraphs), title=title)
    if not cleaned:
        ledger.fail(task_id, chapter, 1, "invalid_text")
        return
    payload = json.dumps(
        {
            "schema": CACHE_SCHEMA,
            "title": title,
            "url": link.url,
            "pages": max(1, pages),
            "truncated": truncated,
            "paragraphs": list(cleaned),
            "source": "ocr" if ocr_used else "html",
            "review": [[text, confidence] for text, confidence in review],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    relative = publish_bytes(ledger.root, task_id, f"{chapter:05d}.json", payload)
    ledger.complete(task_id, chapter, 1, relative)
