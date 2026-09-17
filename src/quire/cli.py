"""CLI 命令、能力诊断与用户可见错误映射（项目设计.md §20）。"""

from __future__ import annotations

import argparse
import asyncio
import platform
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

from . import __version__
from .cli_console import (
    EXIT_CONFIG,
    EXIT_FETCH,
    EXIT_OK,
    EXIT_PARSE,
    EXIT_PARTIAL,
    ArgumentParser,
    data_home,
    fail,
    find_chrome,
    human_size,
    info,
    warn,
)
from .cli_console import (
    module_available as _module_available,
)
from .cli_options import add_manga_options, add_novel_options, compression_options, render_options
from .cli_progress import Progress
from .errors import ConfigError, QuireError, UnsupportedError
from .export_options import available_output, parse_formats
from .manga import MangaOptions, MangaResult, run_local, run_manga
from .parse.images import FilterPolicy
from .parse.minidom import SelectorError
from .utils.naming import unique_path
from .utils.urls import redact

_cmd_novel: Callable[[argparse.Namespace], int] | None
try:  # micro 分发不包含小说命令，导入失败即视为没有该子命令
    from .cli_novel import cmd_novel as _cmd_novel
except ImportError:  # pragma: no cover - 只在缺少 core 模块的分发里发生
    _cmd_novel = None


def cmd_manga(args: argparse.Namespace) -> int:
    compression = compression_options(args)
    render = render_options(args)
    formats = parse_formats(args.format)
    if args.format is not None and not args.core:
        raise ConfigError("--format 需要 --core")
    if args.resume and not args.core:
        raise ConfigError("--resume 需要 --core")
    if args.core and not all(
        _module_available(name)
        for name in ("httpx", "PIL", "pypdf", "websockets", "quire.core_manga")
    ):
        raise UnsupportedError(
            "缺少 core 下载能力", hint="安装 quire-local[core]；micro 不包含断点恢复"
        )
    out = Path(args.output) if args.output else Path.cwd() / f"comic.{formats[0]}"
    if args.core:
        out = available_output(out, formats, overwrite=args.overwrite or args.resume)
    elif out.exists():
        if not args.overwrite:
            out = unique_path(out)
            warn(f"目标已存在，改写到 {out.name}")
    out.parent.mkdir(parents=True, exist_ok=True)

    options = MangaOptions(
        selector=args.selector,
        order=args.order,
        concurrency=args.concurrency,
        rate=args.rate,
        retries=args.retries,
        timeout=args.timeout,
        dpi=args.dpi,
        paper=args.paper,
        keep_images=args.keep_images,
        overwrite=args.overwrite,
        max_bytes=args.max_bytes,
        referer=args.referer,
        first=args.start,
        last=args.end,
        policy=FilterPolicy(
            min_width=args.min_width,
            min_height=args.min_height,
            min_bytes=int(args.min_size * 1024),
        ),
    )

    info(f"→ {redact(args.url)}")
    progress = Progress(enabled=not args.quiet)
    try:
        if args.core:
            from .core_manga import run_core_manga

            result = asyncio.run(
                run_core_manga(
                    args.url,
                    out,
                    options=options,
                    workdir=args.workdir,
                    resume=args.resume,
                    progress=progress,
                    compression=compression,
                    formats=formats,
                    render=render,
                )
            )
        else:
            result = run_manga(
                args.url,
                out,
                options=options,
                workdir=args.workdir,
                progress=progress,
            )
    except KeyboardInterrupt:
        fail("已取消")
        return EXIT_FETCH

    _report(result, args)
    return EXIT_PARTIAL if result.partial else EXIT_OK


def _report(result: MangaResult, args: argparse.Namespace) -> None:
    if result.artifacts_reused:
        info("成品校验通过，已复用" + ("并完成中断恢复" if result.export_recovered else ""))
    for warning in result.warnings:
        warn(warning)

    if args.explain and result.rejections:
        info(f"\n被过滤的候选（{len(result.rejections)} 个）：")
        for rej in result.rejections[: args.explain_limit]:
            info(f"  - [{rej.stage}] {rej.reason}\n      {redact(rej.url)[:110]}")

    info("")
    info(f"✓ {result.output}")
    info(
        f"  {result.pages_written + result.pages_failed} 页 · {human_size(result.bytes_out)} · {result.elapsed_s:.1f} 秒"
    )

    if result.failures:
        info("")
        info(f"  {len(result.failures)} 页缺失，已用占位页补齐：")
        for url, reason in result.failures[:10]:
            info(f"    · {reason}\n        {url[:110]}")
        if len(result.failures) > 10:
            info(f"    …… 另有 {len(result.failures) - 10} 页，见报告")
        info("    可修正链接或选择器后重试。")
    if result.images_dir:
        info(f"  原图：{result.images_dir}")
    if result.report:
        info(f"  报告：{result.report}")
    if result.task_id:
        info(f"  任务：{result.task_id[:12]} · 复用 {result.resources_reused} 张")
    if result.compression:
        target = (
            "无体积目标"
            if result.target_bytes is None
            else (
                f"目标 {human_size(result.target_bytes)} · {'已达成' if result.target_met else '未达成'}"
            )
        )
        info(f"  {result.compression} · {result.encoding_rounds} 轮 · {target}")
    if result.artifacts:
        for artifact in result.artifacts:
            state = " · 未达体积目标" if artifact.target_met is False else ""
            info(
                f"  {artifact.format.upper()}：{artifact.path} · {human_size(artifact.bytes)}{state}"
            )


def cmd_inspect(args: argparse.Namespace) -> int:
    """侦察：不下载，只报告页面结构。这是写站点规则的主力工具（§6.14）。"""
    from .fetch.simple import Fetcher
    from .parse.images import collect, prefilter
    from .parse.minidom import parse as parse_html

    client = Fetcher(timeout=args.timeout, retries=1, rate=args.rate)
    page = client.get(args.url, referer=args.referer)
    doc = parse_html(page.text, base_url=page.url)
    base = doc.effective_base() or page.url

    info(f"URL      {redact(page.url)}")
    info(f"状态     {page.status} · {human_size(len(page.content))} · {page.elapsed_ms} ms")
    info(f"标题     {doc.title or '(无)'}")
    info(f"base     {redact(base)}")
    if doc.find("base") is not None:
        warn("<base href> 生效，相对地址按上面的 base 解析")

    candidates = collect(doc, base, selector=args.selector)
    kept, rejected = prefilter(candidates)
    info("")
    info(f"候选图片 {len(candidates)} 个 → 粗筛后 {len(kept)} 个")

    sources: dict[str, int] = {}
    for c in candidates:
        sources[c.source] = sources.get(c.source, 0) + 1
    if sources:
        info("来源分布 " + " · ".join(f"{k}={v}" for k, v in sorted(sources.items())))

    if args.explain and rejected:
        info(f"\n被丢弃（{len(rejected)} 个）：")
        for rej in rejected[: args.explain_limit]:
            info(f"  - [{rej.stage}] {rej.reason}\n      {redact(rej.url)[:110]}")

    if kept:
        info("\n前 5 个候选：")
        for c in kept[:5]:
            info(f"  #{c.order:<4} [{c.source:6}] {redact(c.url)[:100]}")

    if args.dump_html:
        dump = Path(args.dump_html)
        if dump.is_dir():
            dump = dump / "page.html"
        from .workspace import write_bytes

        write_bytes(dump, page.text.encode("utf-8"))
        info(f"\nHTML 已存到 {dump}")

    if not kept:
        warn("一个候选都没有——这页很可能由 JS 渲染，或用的是非标准属性")
        return EXIT_PARSE
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    """能力自检：为什么能用、为什么不能用，一眼可见。"""
    info(f"quire {__version__}")
    info(f"Python  {platform.python_version()} ({sys.executable})")
    info(f"系统    {platform.system()} {platform.release()} / {platform.machine()}")
    info("")

    info("可选依赖")
    optional = [("PIL", "core 图片解码与压缩"), ("httpx", "core 连接池"), ("pypdf", "PDF 测试校验")]
    for module, purpose in optional:
        mark = "✓" if _module_available(module) else "·"
        info(f"  {mark} {module:<14} {purpose}")

    info("")
    info("渲染后端")
    chrome = find_chrome()
    if chrome:
        info(f"  ✓ {chrome}")
    else:
        warn("没找到 Chrome / Edge / Brave / Chromium")
        warn("  JS 渲染与「小说转 PDF」都需要它（项目设计.md 决策 N）")
    info("")
    info("OCR 引擎")
    if shutil.which("tesseract"):
        info("  ✓ 系统 tesseract（优先引擎，增量 0）")
    else:
        info("  · 无系统 tesseract")
    if _module_available("onnxruntime"):
        info("  ✓ onnxruntime（内置引擎，quire-local[ocr]）")
        from .ocr.models import check_models

        status = check_models(data_home() / "models")
        if status.ready:
            info(f"  ✓ PP-OCRv4 模型已就绪：{status.model_dir}")
        else:
            missing = "、".join((*status.missing, *status.corrupt))
            info(f"  · 模型未就绪（{missing}），首次 OCR 时按需下载到 {status.model_dir}")
    else:
        info("  · 无 onnxruntime（内置引擎需 quire-local[ocr]）")

    info("")
    info("能力档位")
    info("  micro：静态网页 / 本地 JPEG、PNG → 无损 PDF，零第三方运行时依赖")
    if all(
        _module_available(name)
        for name in ("httpx", "PIL", "pypdf", "websockets", "quire.core_manga")
    ):
        info("  core：漫画下载恢复、转码切页、PDF/CBZ/ZIP；--render使用系统Chrome加载动态页面")
    if all(_module_available(name) for name in ("httpx", "websockets", "quire.core_novel")):
        info("  core：小说目录、正文抽取、EPUB/TXT/PDF，章级断点恢复（quire novel）")
        info("        图片正文本地 OCR：tesseract 或内置 PP-OCRv4（--ocr，模型按需下载）")
    info("  应用界面尚未实现（M6）")

    info("")
    info("数据目录")
    info(f"  后续默认数据根：{data_home()}")
    info("  当前 micro 的输出和临时缓存位于所选输出路径旁")

    return EXIT_OK


# ============================================================ 参数


def build_parser() -> argparse.ArgumentParser:
    parser = ArgumentParser(
        prog="quire",
        description="卷帙 Quire —— 把漫画与小说抓成一本本本地的书。",
    )
    parser.add_argument("--version", action="version", version=f"quire {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    m = sub.add_parser("manga", help="抓一页漫画并合成 PDF")
    add_manga_options(m)
    m.set_defaults(func=cmd_manga)

    if _cmd_novel is not None:
        n = sub.add_parser("novel", help="抓一本小说并导出 EPUB / TXT / PDF")
        add_novel_options(n)
        n.set_defaults(func=_cmd_novel)

    i = sub.add_parser("inspect", help="侦察页面结构（写站点规则用）")
    i.add_argument("url")
    i.add_argument("--selector", help="测试某个选择器")
    i.add_argument("--referer")
    i.add_argument("--rate", type=float, default=4.0)
    i.add_argument("--timeout", type=float, default=20.0)
    i.add_argument("--explain", action="store_true", help="打印每个候选被丢弃的原因")
    i.add_argument("--explain-limit", type=int, default=30)
    i.add_argument("--dump-html", nargs="?", const="debug", help="把 HTML 存下来")
    i.set_defaults(func=cmd_inspect)

    d = sub.add_parser("doctor", help="能力与依赖自检")
    d.set_defaults(func=cmd_doctor)
    local = sub.add_parser("local", help="本地图片目录生成 PDF，不删除输入图片")
    local.add_argument("directory", type=Path)
    local.add_argument("-o", "--output", type=Path, required=True)
    local.add_argument("--overwrite", action="store_true")
    local.set_defaults(func=cmd_local)
    return parser


def cmd_local(args: argparse.Namespace) -> int:
    output = args.output if args.overwrite else unique_path(args.output)
    result = run_local(
        args.directory,
        output,
        options=MangaOptions(
            overwrite=args.overwrite, policy=FilterPolicy(min_width=0, min_height=0)
        ),
    )
    info(f"{result.output} | {result.pages_written} 页 | {human_size(result.bytes_out)}")
    if result.failures:
        warn(f"{result.pages_failed} 页缺失，见 {result.report}")
    return EXIT_PARTIAL if result.partial else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        return int(args.func(args))
    except QuireError as exc:
        fail(exc.message, exc.hint)
        return exc.exit_code
    except (SelectorError, ValueError, OverflowError) as exc:
        fail(str(exc))
        return EXIT_CONFIG
    except OSError as exc:
        fail(f"文件操作失败：{exc}")
        return EXIT_FETCH
    except KeyboardInterrupt:
        fail("已取消")
        return EXIT_FETCH


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
