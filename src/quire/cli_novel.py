"""``quire novel``：小说命令的参数组装与结果打印（项目设计.md §37）。"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .cli_console import (
    EXIT_FETCH,
    EXIT_OK,
    EXIT_PARTIAL,
    data_home,
    fail,
    human_size,
    info,
    module_available,
    warn,
)
from .cli_options import render_options
from .cli_progress import Progress
from .errors import UnsupportedError
from .models import NovelResult
from .utils.urls import redact


def cmd_novel(args: argparse.Namespace) -> int:
    """小说：目录/单章 → 正文 → EPUB / TXT / PDF。全部走 core 路径。"""
    from .models import NovelOptions
    from .novel_options import available_novel_output, parse_novel_formats

    formats = parse_novel_formats(args.format)
    render = render_options(args, chrome_for_pdf="pdf" in formats)
    if not all(module_available(name) for name in ("httpx", "websockets", "quire.core_novel")):
        raise UnsupportedError(
            "小说功能需要 core 下载能力", hint="安装 quire-local[core]；micro 只做静态漫画"
        )
    out = Path(args.output) if args.output else Path.cwd() / f"book.{formats[0]}"
    out = available_novel_output(out, formats, overwrite=args.overwrite)

    options = NovelOptions(
        content_selector=args.content_selector,
        chapter_selector=args.chapter_selector,
        next_selector=args.next_selector,
        max_chapters=args.max_chapters,
        max_pages=args.max_pages,
        concurrency=args.concurrency,
        rate=args.rate,
        retries=args.retries,
        timeout=args.timeout,
        keep_html=args.keep_html,
        overwrite=args.overwrite,
        referer=args.referer,
        max_bytes=args.max_bytes,
        ocr_mode=args.ocr,
        ocr_engine=args.ocr_engine,
        offline=args.offline,
        model_dir=(Path(args.data_dir) / "models" if args.data_dir else data_home() / "models"),
    )
    info(f"→ {redact(args.url)}")
    progress = Progress(enabled=not args.quiet)
    try:
        from .core_novel import run_core_novel

        result = asyncio.run(
            run_core_novel(
                args.url,
                out,
                options=options,
                workdir=args.workdir,
                resume=args.resume,
                formats=formats,
                render=render,
                pdf_chrome=args.chrome,
                progress=progress,
            )
        )
    except KeyboardInterrupt:
        fail("已取消")
        return EXIT_FETCH

    _report_novel(result, args)
    return EXIT_PARTIAL if result.partial else EXIT_OK


def _report_novel(result: NovelResult, args: argparse.Namespace) -> None:
    for warning in result.warnings:
        warn(warning)
    info("")
    info(f"✓ {result.output}")
    info(
        f"  {result.chapters_written + result.chapters_failed} 章 · "
        f"{result.characters:,} 字 · {result.elapsed_s:.1f} 秒"
    )
    if result.failures:
        info("")
        info(f"  {len(result.failures)} 章缺失，已在成品中标注原因：")
        for url, reason in result.failures[:10]:
            info(f"    · {reason}\n        {url[:110]}")
        if len(result.failures) > 10:
            info(f"    …… 另有 {len(result.failures) - 10} 章，见报告")
        info("    修正后可用 --resume 只重取失败的章节。")
    if result.html_dir:
        info(f"  原始 HTML：{result.html_dir}")
    if result.ocr_chapters:
        info(f"  OCR：{result.ocr_chapters} 章正文来自图片识别")
    if result.review:
        info(f"  低置信复核：{result.review}")
    if result.report:
        info(f"  报告：{result.report}")
    if result.task_id:
        info(f"  任务：{result.task_id[:12]} · 复用 {result.resources_reused} 章")
    for artifact in result.artifacts:
        info(f"  {artifact.format.upper()}：{artifact.path} · {human_size(artifact.bytes)}")
