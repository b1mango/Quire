"""CLI commands, capability diagnostics and user-facing error mapping."""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import sys
import time
from pathlib import Path
from typing import Never

from . import __version__
from .errors import QuireError
from .manga import MangaOptions, MangaResult, run_local, run_manga
from .parse.images import FilterPolicy
from .parse.minidom import SelectorError
from .utils.naming import unique_path
from .utils.urls import redact

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_FETCH = 2
EXIT_PARSE = 3
EXIT_PARTIAL = 4
EXIT_BLOCKED = 5
EXIT_DEPENDENCY = 6

CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        from .errors import ConfigError

        raise ConfigError(message)


class Progress:
    """极简进度条。刻意不用 rich —— 省 6 MB（项目设计.md §2.2）。"""

    def __init__(self, *, enabled: bool = True, width: int = 24) -> None:
        self.enabled = enabled and sys.stderr.isatty()
        self.width = width
        self._last = 0.0

    def update(self, done: int, total: int, result: MangaResult) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        if done < total and now - self._last < 0.1:
            return
        self._last = now
        ratio = done / total if total else 0
        filled = int(self.width * ratio)
        bar = "█" * filled + "·" * (self.width - filled)
        tail = f"失败 {result.pages_failed}" if result.pages_failed else ""
        sys.stderr.write(f"\r  {bar} {done:>4}/{total}  {tail}   ")
        sys.stderr.flush()
        if done >= total:
            sys.stderr.write("\n")


# ============================================================ 输出


def info(message: str) -> None:
    sys.stdout.write(message + "\n")


def warn(message: str) -> None:
    sys.stderr.write(f"  ! {message}\n")


def fail(message: str, hint: str | None = None) -> None:
    sys.stderr.write(f"\n✗ {message}\n")
    if hint:
        sys.stderr.write(f"  → {hint}\n")


def human_size(num: int) -> str:
    value = float(num)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def cmd_manga(args: argparse.Namespace) -> int:
    out = Path(args.output) if args.output else Path.cwd() / "comic.pdf"
    if out.exists():
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
    optional = [("PIL", "开发测试图像库"), ("pypdf", "PDF 回读校验"), ("lxml", "选择器对拍测试")]
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
    if shutil.which("tesseract"):
        info("  ✓ 系统 tesseract 已安装，OCR 接入尚未实现")
    else:
        info("  · 无系统 tesseract，OCR 将在 M5 实现")

    info("")
    info("能力档位")
    info("  micro：静态网页 / 本地 JPEG、PNG → 无损 PDF，零第三方运行时依赖")
    info("  压缩、动态渲染、小说、OCR 和应用界面尚未实现")

    info("")
    info("数据目录")
    info(f"  后续默认数据根：{data_home()}")
    info("  当前 micro 的输出和临时缓存位于所选输出路径旁")

    return EXIT_OK


def _module_available(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def find_chrome() -> str | None:
    for path in CHROME_CANDIDATES:
        if os.path.exists(path):
            return path
    for name in ("google-chrome", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def data_home() -> Path:
    override = os.environ.get("QUIRE_HOME")
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "quire"
    return Path.home() / ".local" / "share" / "quire"


# ============================================================ 参数


def build_parser() -> argparse.ArgumentParser:
    parser = ArgumentParser(
        prog="quire",
        description="卷帙 Quire —— 把漫画与小说抓成一本本本地的书。",
    )
    parser.add_argument("--version", action="version", version=f"quire {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    m = sub.add_parser("manga", help="抓一页漫画并合成 PDF")
    m.add_argument("url")
    m.add_argument("-o", "--output", help="输出 PDF 路径")
    m.add_argument("--selector", help="限定图片所在区域的选择器，如 'div.reader img'")
    m.add_argument(
        "--order",
        default="auto",
        choices=["auto", "dom", "asc", "desc"],
        help="页序策略（默认 auto：DOM 序，与编号冲突时告警）",
    )
    m.add_argument("--referer", help="自定义 Referer，用于防盗链站点")
    m.add_argument("--start", type=int, default=1, help="起始页（1 起）")
    m.add_argument("--end", type=int, default=0, help="结束页（0 表示到最后一页）")
    m.add_argument("--concurrency", type=int, default=4, help="并发下载数（默认 4）")
    m.add_argument("--rate", type=float, default=4.0, help="每站限速 req/s（默认 4）")
    m.add_argument("--retries", type=int, default=3, help="失败重试次数（默认 3）")
    m.add_argument("--timeout", type=float, default=20.0, help="单请求超时秒数")
    m.add_argument("--dpi", type=int, default=150, help="像素→物理尺寸的换算基准")
    m.add_argument(
        "--paper",
        default="original",
        choices=["original", "a4", "a5", "b5", "letter"],
        help="纸张（默认 original：页面等于图片）",
    )
    m.add_argument("--min-width", type=int, default=200, help="小于此宽度判为噪音")
    m.add_argument("--min-height", type=int, default=200, help="小于此高度判为噪音")
    m.add_argument("--min-size", type=float, default=0, help="最小图片 KB（默认不限）")
    m.add_argument("--max-bytes", type=int, default=32 * 1024 * 1024, help="单资源最大字节数")
    m.add_argument("--keep-images", action="store_true", help="保留下载的原图")
    m.add_argument("--workdir", help="临时目录（默认在输出文件旁边）")
    m.add_argument("--explain", action="store_true", help="打印每张图被过滤的原因")
    m.add_argument("--explain-limit", type=int, default=30)
    m.add_argument("--overwrite", action="store_true", help="覆盖同名输出")
    m.add_argument("-q", "--quiet", action="store_true", help="不显示进度条")
    m.set_defaults(func=cmd_manga)

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
