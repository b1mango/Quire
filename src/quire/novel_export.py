"""Novel candidates and guarded publication, reusing the existing file primitives."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

from .assemble.epub import EpubChapter, EpubWriter
from .assemble.models import NovelChapter
from .assemble.novel_html import render_html
from .assemble.txt import write_txt
from .errors import ConfigError, FetchError, LedgerError
from .export_commit import publish, sync_file
from .export_receipt import ExportReceipt, PublishedFile
from .fetch.browser_pdf import print_pdf
from .models import ArtifactResult, NovelResult
from .ocr.base import ReviewEntry
from .store.export_files import (
    Stamp,
    check_workspace,
    clean_workspace,
    fingerprint,
    make_workspace,
)


def preflight(destinations: tuple[Path, ...], *, overwrite: bool) -> tuple[Stamp | None, ...]:
    stamps = tuple(fingerprint(path) for path in destinations)
    if not overwrite:
        for path, stamp in zip(destinations, stamps, strict=True):
            if stamp is not None:
                raise ConfigError(f"目标已存在：{path}；覆盖须明确指定 --overwrite")
    return stamps


def report_payload(result: NovelResult) -> dict[str, object]:
    return {
        "schema": 1,
        "title": result.title,
        "output": result.output.name,
        "status": "partial" if result.partial else "done",
        "chapters": result.chapters_written + result.chapters_failed,
        "written": result.chapters_written,
        "missing": result.chapters_failed,
        "truncated": result.truncated,
        "characters": result.characters,
        "elapsed_seconds": round(result.elapsed_s, 3),
        "warnings": list(result.warnings),
        "failures": list(result.failures),
        "artifacts": [
            {"format": item.format, "file": item.path.name, "bytes": item.bytes}
            for item in result.artifacts
        ],
        "task_id": result.task_id,
        "resources_reused": result.resources_reused,
        "source_resources": result.source_resources,
        "pages_fetched": result.pages_fetched,
        "html_dir": str(result.html_dir) if result.html_dir else None,
        "ocr_chapters": result.ocr_chapters,
        "review": str(result.review) if result.review else None,
        "chapter_list": [
            {
                "index": item.index,
                "title": item.title,
                "chars": item.chars,
                "paragraphs": item.paragraphs,
                "pages": item.pages,
                "reused": item.reused,
                "truncated": item.truncated,
                "missing": item.missing_reason,
            }
            for item in result.chapters
        ],
    }


def review_text(title: str, entries: tuple[ReviewEntry, ...]) -> str:
    """低置信行复核清单：供人工抽查，不改写正文。"""
    lines = [f"# {title} OCR 低置信行复核清单（置信度 < 0.6）", ""]
    lines.extend(f"第{entry.chapter}章 [{entry.confidence:.3f}] {entry.text}" for entry in entries)
    return "\n".join(lines) + "\n"


async def export_novel(
    result: NovelResult,
    chapters: tuple[NovelChapter, ...],
    destinations: tuple[Path, ...],
    formats: tuple[str, ...],
    before: tuple[Stamp | None, ...],
    *,
    source_url: str,
    pdf_chrome: str | None = None,
    review: tuple[ReviewEntry, ...] = (),
    review_destination: Path | None = None,
    review_stamp: Stamp | None = None,
) -> NovelResult:
    started = time.monotonic()
    assert result.task_id is not None
    workspace, identity = make_workspace(result.output.parent, result.task_id)
    receipt = ExportReceipt(result.task_id, "building", workspace, identity, formats)
    try:
        candidates: list[Path] = []
        for fmt in formats:
            await asyncio.sleep(0)
            check_workspace(workspace, identity)
            path = workspace / f"book.{fmt}"
            if fmt == "txt":
                write_txt(path, title=result.title, chapters=chapters)
            elif fmt == "pdf":
                assert pdf_chrome is not None
                await print_pdf(
                    render_html(title=result.title, chapters=chapters), path, executable=pdf_chrome
                )
            else:
                with EpubWriter(path, title=result.title, source_url=source_url) as writer:
                    for chapter in chapters:
                        writer.add_chapter(
                            EpubChapter(
                                chapter.index,
                                chapter.heading,
                                chapter.paragraphs,
                                chapter.source_url,
                                chapter.missing_reason,
                            )
                        )
                        await asyncio.sleep(0)
            candidates.append(path)
        report = result.output.with_suffix(".report.json")
        review_path = result.output.with_suffix(".review.txt") if review else None
        final = replace(
            result,
            report=report,
            review=review_path,
            elapsed_s=result.elapsed_s + time.monotonic() - started,
            artifacts=tuple(
                ArtifactResult(fmt, destination, sync_file(path).size, None)
                for fmt, path, destination in zip(formats, candidates, destinations, strict=True)
            ),
        )
        report_file = workspace / "book.report.json"
        check_workspace(workspace, identity)
        with report_file.open("xb") as stream:
            stream.write(json.dumps(report_payload(final), ensure_ascii=False, indent=2).encode())
        candidates.append(report_file)
        extras: list[tuple[str, Path, Path, Stamp | None]] = []
        if review and review_destination is not None:
            review_file = workspace / "book.review.txt"
            check_workspace(workspace, identity)
            with review_file.open("xb") as stream:
                stream.write(review_text(result.title, review).encode("utf-8"))
            extras.append(("review", review_file, review_destination, review_stamp))
        files = tuple(
            PublishedFile(kind, path.name, destination, sync_file(path), stamp)
            for kind, path, destination, stamp in zip(
                (*formats, "report", *(kind for kind, _, _, _ in extras)),
                (*candidates, *(path for _, path, _, _ in extras)),
                (*destinations, report, *(destination for _, _, destination, _ in extras)),
                (*before, *(stamp for _, _, _, stamp in extras)),
                strict=True,
            )
        )
        receipt = replace(receipt, state="prepared", files=files)
        await publish(receipt)
        return final
    except (OSError, FetchError, LedgerError) as exc:
        raise FetchError(
            f"小说导出失败；已发布：{_published_names(receipt)}；章节缓存已保留",
            hint="另存或明确覆盖后重试；系统未保证多文件同时提交。",
        ) from exc
    finally:
        failure = sys.exception()
        try:
            clean_workspace(workspace, identity, formats)
        except (LedgerError, OSError) as exc:
            note = f"临时目录未能安全清理：{workspace.name}"
            if failure is not None:
                failure.add_note(note)
            else:
                raise FetchError(
                    f"{note}；已发布：{_published_names(receipt)}；章节缓存已保留"
                ) from exc


def _published_names(receipt: ExportReceipt) -> str:
    names = []
    for item in receipt.files:
        try:
            if fingerprint(item.destination) == item.stamp:
                names.append(item.destination.name)
        except (LedgerError, OSError):
            names.append(f"{item.destination.name}（状态无法确认）")
    return ", ".join(names) or "无"
