"""小说采集编排：目录 → 章节 → 导出 → 发布（项目设计.md §37）。

顺序有意与漫画一致：先确认网页身份与章节清单，再建账本、恢复、抓取，
最后一次性生成候选并逐文件原子发布。**导出阶段不联网**——正文全部来自
账本缓存，所以成品重建只是本地计算，不需要再碰站点。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

from .assemble.models import NovelChapter
from .core_chapter_cache import decode_chapter
from .core_chapters import (
    FAILURE_TEXT,
    TRUNCATION_NOTE,
    Renderer,
    capture_chapters,
)
from .errors import NoTextError, PausedError, UnsupportedError
from .fetch.async_policy import HostPace
from .fetch.browser import RenderOptions, fetch_render_input, render_page
from .fetch.browser_process import find_chrome
from .fetch.cloudflare import ClearanceEscalation
from .fetch.session import AsyncFetcher
from .fetch.simple import Response
from .models import (
    ChapterProgress,
    ChapterResult,
    NovelOptions,
    NovelResult,
)
from .novel_export import export_novel, preflight
from .novel_options import novel_output_paths, validate_novel_formats
from .novel_plan import plan_chapters
from .ocr.base import ReviewEntry
from .ocr.postprocess import drop_repeated_short_lines
from .parse.chapters import ChapterLink
from .parse.minidom import parse as parse_html
from .sites.expand import expand_catalogue
from .sites.registry import cookie_hosts_for
from .store.cache import read_cached
from .store.ledger import Ledger
from .store.models import JsonValue, ResourceSpec, task_identity
from .utils.urls import redact


def novel_identity(opts: NovelOptions, links: tuple[ChapterLink, ...]) -> dict[str, JsonValue]:
    """参与任务身份的只有内容参数与章节清单；网络调优参数不影响复用。"""
    return {
        "content_selector": opts.content_selector,
        "chapter_selector": opts.chapter_selector,
        "next_selector": opts.next_selector,
        "max_chapters": opts.max_chapters,
        "max_pages": opts.max_pages,
        "capture_mode": opts.capture_mode,
        "chapter_first": opts.chapter_first,
        "chapter_last": opts.chapter_last,
        "chapter_ranges": opts.chapter_ranges,
        "clean_version": opts.clean_version,
        "ocr_mode": opts.ocr_mode,
        "ocr_engine": opts.ocr_engine,
        "chapters": [[link.title, link.url] for link in links],
    }


def load_chapters(
    ledger: Ledger, task_id: str, links: tuple[ChapterLink, ...]
) -> tuple[NovelChapter, ...]:
    """把账本里的章节按阅读顺序还原成可导出的章节（失败章写明原因）。"""
    snapshot = ledger.snapshot(task_id)
    chapters: list[NovelChapter] = []
    for record, link in zip(snapshot.resources, links, strict=True):
        index = record.spec.chapter
        done = (
            record.status == "done"
            and record.local_path is not None
            and record.sha256 is not None
            and record.size is not None
        )
        if done:
            data = read_cached(
                ledger.root,
                task_id,
                record.local_path or "",
                sha256=record.sha256 or "",
                size=record.size or 0,
            )
            cached = decode_chapter(data)
            chapters.append(
                NovelChapter(
                    index,
                    cached.title,
                    cached.paragraphs,
                    source_url=link.url,
                    pages=cached.pages,
                    truncated=cached.truncated,
                    source=cached.source,
                    review=cached.review,
                )
            )
            continue
        reason = FAILURE_TEXT.get(record.error_code or "", "未完成")
        chapters.append(
            NovelChapter(index, link.title, (), source_url=link.url, missing_reason=reason)
        )
    return tuple(chapters)


def export_chapters(chapters: tuple[NovelChapter, ...]) -> tuple[NovelChapter, ...]:
    """导出用章节：被分页上限截断的章追加一行说明，读者不会以为书是完整的。

    OCR 章节另做跨页去重：整书超过 60% 的章都出现的短行是页眉页脚，
    删掉（§6.7 后处理）；HTML 章节已由正文清洗处理，不在此列。
    """
    noted = tuple(
        replace(chapter, paragraphs=(*chapter.paragraphs, TRUNCATION_NOTE))
        if chapter.truncated and chapter.missing_reason is None
        else chapter
        for chapter in chapters
    )
    ocr_paragraphs = drop_repeated_short_lines(
        [chapter.paragraphs for chapter in noted if chapter.source == "ocr"]
    )
    deduped = iter(ocr_paragraphs)
    return tuple(
        replace(chapter, paragraphs=next(deduped)) if chapter.source == "ocr" else chapter
        for chapter in noted
    )


def _chapter_result(chapter: NovelChapter, reused: frozenset[int]) -> ChapterResult:
    return ChapterResult(
        index=chapter.index,
        title=chapter.title,
        url=chapter.source_url,
        chars=chapter.chars,
        paragraphs=len(chapter.paragraphs),
        pages=chapter.pages,
        reused=chapter.index in reused,
        truncated=chapter.truncated,
        missing_reason=chapter.missing_reason,
    )


async def run_core_novel(
    url: str,
    out: Path | str,
    *,
    options: NovelOptions | None = None,
    workdir: Path | str | None = None,
    resume: bool = False,
    fetcher: AsyncFetcher | None = None,
    formats: tuple[str, ...] = ("epub",),
    render: RenderOptions | None = None,
    pdf_chrome: str | None = None,
    progress: ChapterProgress | None = None,
    on_task: Callable[[str], None] | None = None,
    stop: Callable[[], bool] | None = None,
    prepend: Sequence[NovelChapter] | None = None,
    pace: HostPace | None = None,
) -> NovelResult:
    opts = options or NovelOptions()
    chosen = validate_novel_formats(formats)
    executable = None
    if "pdf" in chosen:
        executable = find_chrome(pdf_chrome or (render.executable if render else None))
        if executable is None:
            raise UnsupportedError(
                "小说 PDF 需要系统 Chrome/Edge/Brave/Chromium", hint="安装 Chrome 或指定 --chrome"
            )
    ocr_runner = None
    if opts.ocr_mode != "never":
        from .ocr.capture import OcrRunner

        model_dir = opts.model_dir
        if model_dir is None:
            from .cli_console import data_home

            model_dir = data_home() / "models"
        ocr_runner = OcrRunner(
            engine=opts.ocr_engine,
            model_dir=model_dir,
            offline=opts.offline,
            allow_download=opts.ocr_download,
            content_selector=opts.content_selector,
            max_bytes=opts.max_bytes,
        )
    destinations = novel_output_paths(Path(out).absolute(), chosen)
    output = destinations[0]
    output.parent.mkdir(parents=True, exist_ok=True)
    report_path = output.with_suffix(".report.json")
    review_path = output.with_suffix(".review.txt")
    stamps = preflight((*destinations, report_path, review_path), overwrite=opts.overwrite)
    before, report_stamp, review_stamp = stamps[: len(destinations)], stamps[-2], stamps[-1]
    for selector in (opts.content_selector, opts.chapter_selector, opts.next_selector):
        if selector:
            parse_html("").select(selector)
    started = time.monotonic()
    root = Path(workdir) if workdir else output.parent / ".quire-core"
    client = fetcher or AsyncFetcher(
        timeout=opts.timeout,
        retries=opts.retries,
        concurrency=opts.concurrency,
        rate=opts.rate,
        max_bytes=opts.max_bytes,
        pace=pace,
        respect_robots=opts.obey_robots,
        cookie_hosts=cookie_hosts_for(url),
    )
    async with client:
        if fetcher is None:
            # Cloudflare 盾站点：HTTP 403 命中时自动升级真实 Chrome 过质询并回注
            # cf_clearance；外部传入的 fetcher 由调用方自行决定是否启用。
            client.escalation = ClearanceEscalation(client, render)
        page = await fetch_render_input(client, url, render, referer=opts.referer)
        if not (render and (render.native or render.cdp_endpoint)):
            page = await expand_catalogue(client, page)
        render_warnings: tuple[str, ...] = ()
        renderer: Renderer | None = None
        if render is not None:
            page, render_warnings = await render_page(page, client, render, content="text")
            renderer = _browser_renderer(client, render)
        plan = plan_chapters(page, opts, render_warnings)
        specs = [ResourceSpec(i + 1, 1, link.url) for i, link in enumerate(plan.links)]
        identity = novel_identity(opts, plan.links)
        identity["render"] = render is not None
        task_id, _ = task_identity(url, identity, specs)
        with Ledger(root) as ledger:
            ledger.create_task(url, identity, specs)
            # 每次运行都校验缓存：已提交且哈希正确的章节直接复用，失败或损坏的重取。
            ledger.recover(task_id)
            if on_task:
                on_task(task_id)
            # A fresh directory avoids trusting old diagnostic files or links.
            html_dir = None
            if opts.keep_html:
                from .store.export_files import make_workspace

                html_dir, _ = make_workspace(ledger.root, task_id)
            on_progress = _progress_hook(progress)
            captured = await capture_chapters(
                client,
                ledger,
                task_id,
                plan.links,
                options=opts,
                render=renderer,
                ocr=ocr_runner,
                preloaded=plan.preloaded,
                page_fetcher=_RenderInput(client, render),
                html_dir=html_dir,
                on_progress=on_progress,
                stop=stop,
            )
            if stop is not None and stop():
                raise PausedError("任务已暂停，已抓取的章节保留在缓存里")
            chapters = load_chapters(ledger, task_id, plan.links)
            prefix = tuple(prepend or ())
            if prefix:
                # 追更：旧章节缓存在前（章号已是目录绝对序号），新章顺延。
                offset = len(prefix)
                chapters = (
                    *prefix,
                    *(replace(chapter, index=chapter.index + offset) for chapter in chapters),
                )
            reused_ids = frozenset(
                {*range(1, len(prefix) + 1), *(i + len(prefix) for i in captured.reused_chapters)}
            )
            written = sum(1 for chapter in chapters if chapter.missing_reason is None)
            review_entries = tuple(
                ReviewEntry(chapter.index, text, confidence)
                for chapter in chapters
                for text, confidence in chapter.review
            )
            warnings = plan.warnings + captured.warnings
            if review_entries:
                warnings += (f"OCR 低置信行 {len(review_entries)} 条，详见 review.txt",)
            result = NovelResult(
                output=output,
                title=plan.title,
                chapters_written=written,
                chapters_failed=len(chapters) - written,
                characters=sum(chapter.chars for chapter in chapters),
                warnings=warnings,
                failures=tuple(
                    (redact(chapter.source_url), chapter.missing_reason or "")
                    for chapter in chapters
                    if chapter.missing_reason is not None
                ),
                chapters=tuple(_chapter_result(chapter, reused_ids) for chapter in chapters),
                task_id=task_id,
                resources_reused=captured.reused + len(prefix),
                source_resources=len(plan.links),
                pages_fetched=captured.pages_fetched,
                html_dir=html_dir,
                truncated=plan.truncated,
                ocr_chapters=sum(
                    1
                    for chapter in chapters
                    if chapter.source == "ocr" and chapter.missing_reason is None
                ),
            )
            if not written:
                # 一章都没成功：先落报告，再报错——否则用户看不到每章的具体原因。
                result = replace(result, elapsed_s=time.monotonic() - started)
                saved = await export_novel(result, (), (), (), (report_stamp,), source_url=url)
                raise NoTextError(
                    url,
                    hint="这页可能是目录或动态页面：用 --chapter-selector 指定章节链接，"
                    "或 --content-selector 指定正文容器；JS 页面加 --render。"
                    f"每章原因见 {saved.report or '报告'}。",
                )
            result = replace(result, elapsed_s=time.monotonic() - started)
            return await export_novel(
                result,
                export_chapters(chapters),
                destinations,
                chosen,
                (*before, report_stamp),
                source_url=url,
                pdf_chrome=executable,
                review=review_entries,
                review_destination=review_path,
                review_stamp=review_stamp,
            )


def _browser_renderer(client: AsyncFetcher, render: RenderOptions) -> Renderer:
    """把浏览器渲染包成 core_chapters 需要的回调，避免它依赖具体浏览器。"""

    import asyncio

    slots = asyncio.Semaphore(1)

    async def render_one(page: Response) -> tuple[Response, tuple[str, ...]]:
        async with slots:
            return await render_page(page, client, render, content="text")

    return render_one


def _progress_hook(progress: ChapterProgress | None) -> Callable[[int, int], None] | None:
    """CLI 的进度条只需要一个 (done, total) 回调。"""
    if progress is None:
        return None

    def report(done: int, total: int) -> None:
        progress.update(done, total, None)

    return report


class _RenderInput:
    def __init__(self, client: AsyncFetcher, render: RenderOptions | None):
        self.client, self.render = client, render

    async def get(self, url: str, *, referer: str | None = None, robots: bool = True) -> Response:
        return await fetch_render_input(self.client, url, self.render, referer=referer)
