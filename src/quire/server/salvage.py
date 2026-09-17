"""任务取消后的半成品导出：把账本里已落定的内容按用户所选格式出书。

只读账本与本地缓存，**不联网**；产物文件名与元数据标题带「未完成」，
缺失章节/缺页按既有 partial 语义（占位页 / 缺章说明）保留在成品里。
漫画系列分卷任务不在此列：每卷完成时已单独交付。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ..assemble.models import NovelChapter
from ..core_chapters import FAILURE_TEXT, decode_chapter
from ..core_novel import export_chapters
from ..core_reassemble import export_cached
from ..export_options import available_output, validate_formats
from ..image.options import CompressionOptions
from ..models import ChapterResult, MangaOptions, MangaResult, NovelResult
from ..novel_export import export_novel, preflight
from ..novel_options import available_novel_output, novel_output_paths, validate_novel_formats
from ..ocr.base import ReviewEntry
from ..parse.images import Candidate
from ..sites.rules import resolve_rule
from ..store.cache import read_cached
from ..store.ledger import Ledger
from ..store.models import TaskSnapshot
from ..utils.naming import safe_filename
from ..utils.urls import redact
from .job_state import JobSpec
from .settings import UiSettings

#: 半成品提示：书名、元数据与任务警告统一使用。
PARTIAL_NOTE = "（未完成）"


def _snapshot(workdir: Path, task_id: str) -> TaskSnapshot:
    with Ledger(workdir) as ledger:
        return ledger.snapshot(task_id)


async def salvage_job(
    spec: JobSpec, task_id: str, settings: UiSettings, data_root: Path
) -> MangaResult | NovelResult | None:
    """取消后导出半成品；账本里一个完整章节/图片都没有时返回 None。"""
    workdir = settings.output_path / ".quire-core"
    snapshot = _snapshot(workdir, task_id)
    if not any(record.status == "done" for record in snapshot.resources):
        return None
    title = f"{spec.title}{PARTIAL_NOTE}"
    base = settings.output_path / safe_filename(title, default="book")
    if spec.kind == "novel":
        return await _salvage_novel(workdir, snapshot, spec, title, base)
    return await _salvage_manga(workdir, snapshot, spec, settings, data_root, title, base)


async def _salvage_manga(
    workdir: Path,
    snapshot: TaskSnapshot,
    spec: JobSpec,
    settings: UiSettings,
    data_root: Path,
    title: str,
    base: Path,
) -> MangaResult:
    options = MangaOptions(concurrency=settings.concurrency, rate=settings.rate, follow_pages=True)
    rule = resolve_rule(data_root, spec.url)
    if rule:
        options = rule.manga(options)
    candidates = [
        Candidate(record.spec.url, index, referer=record.spec.referer)
        for index, record in enumerate(snapshot.resources)
    ]
    # 取消时仍 pending 的资源从未下载；把占位原因标成 cancelled，免得报告误写 network。
    snapshot = replace(
        snapshot,
        resources=tuple(
            replace(record, error_code="cancelled")
            if record.status != "done" and record.error_code is None
            else record
            for record in snapshot.resources
        ),
    )
    formats = validate_formats(spec.formats)
    out = available_output(base.with_suffix(f".{formats[0]}"), formats, overwrite=False)
    result = MangaResult(out, title=title)
    result = await export_cached(
        workdir,
        snapshot,
        candidates,
        out,
        options,
        CompressionOptions(spec.compress, spec.target_bytes),
        formats,
        result,
        spec.url,
    )
    return replace(result, warnings=result.warnings + ("任务已取消，导出为未完成版本",))


async def _salvage_novel(
    workdir: Path, snapshot: TaskSnapshot, spec: JobSpec, title: str, base: Path
) -> NovelResult | None:
    chapters: list[NovelChapter] = []
    for record in snapshot.resources:
        index = record.spec.chapter
        if record.status == "done" and record.local_path and record.sha256 and record.size:
            cached = decode_chapter(
                read_cached(
                    workdir,
                    snapshot.task_id,
                    record.local_path,
                    sha256=record.sha256,
                    size=record.size,
                )
            )
            chapters.append(
                NovelChapter(
                    index,
                    cached.title,
                    cached.paragraphs,
                    source_url=record.spec.url,
                    pages=cached.pages,
                    truncated=cached.truncated,
                    source=cached.source,
                    review=cached.review,
                )
            )
            continue
        reason = (
            FAILURE_TEXT.get(record.error_code or "", "未完成")
            if record.status == "failed"
            else "任务已取消"
        )
        chapters.append(
            NovelChapter(
                index, f"第 {index} 章", (), source_url=record.spec.url, missing_reason=reason
            )
        )
    written = [chapter for chapter in chapters if chapter.missing_reason is None]
    chrome = _pdf_chrome()
    formats = tuple(
        fmt for fmt in validate_novel_formats(spec.formats) if fmt != "pdf" or chrome is not None
    )
    if not formats:
        return None
    output = available_novel_output(base.with_suffix(f".{formats[0]}"), formats, overwrite=False)
    destinations = novel_output_paths(output, formats)
    report_path = output.with_suffix(".report.json")
    review_entries = tuple(
        ReviewEntry(chapter.index, text, confidence)
        for chapter in written
        for text, confidence in chapter.review
    )
    review_path = output.with_suffix(".review.txt") if review_entries else None
    extra = (review_path,) if review_path is not None else ()
    stamps = preflight((*destinations, report_path, *extra), overwrite=False)
    before, report_stamp = stamps[: len(destinations)], stamps[len(destinations)]
    review_stamp = stamps[-1] if review_path is not None else None
    result = NovelResult(
        output=output,
        title=title,
        chapters_written=len(written),
        chapters_failed=len(chapters) - len(written),
        characters=sum(chapter.chars for chapter in written),
        warnings=("任务已取消，导出为未完成版本",),
        failures=tuple(
            (redact(chapter.source_url), chapter.missing_reason or "")
            for chapter in chapters
            if chapter.missing_reason is not None
        ),
        chapters=tuple(
            ChapterResult(
                index=chapter.index,
                title=chapter.title,
                url=chapter.source_url,
                chars=chapter.chars,
                paragraphs=len(chapter.paragraphs),
                pages=chapter.pages,
                truncated=chapter.truncated,
                missing_reason=chapter.missing_reason,
            )
            for chapter in chapters
        ),
        task_id=snapshot.task_id,
        source_resources=len(chapters),
    )
    return await export_novel(
        result,
        export_chapters(tuple(chapters)),
        destinations,
        formats,
        (*before, report_stamp),
        source_url=spec.url,
        pdf_chrome=chrome,
        review=review_entries,
        review_destination=review_path,
        review_stamp=review_stamp,
    )


def _pdf_chrome() -> str | None:
    from ..cli_console import find_chrome

    return find_chrome()
