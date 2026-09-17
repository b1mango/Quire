"""Manga argument declarations and core compression option validation."""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

from .errors import ConfigError
from .image.options import PRESETS, CompressionOptions, parse_size

if TYPE_CHECKING:
    from .fetch.browser import RenderOptions


def add_manga_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("url")
    parser.add_argument("--core", action="store_true", help="使用 core 连接池、压缩和本地账本")
    parser.add_argument("--resume", action="store_true", help="校验并复用 core 任务已完成图片")
    parser.add_argument(
        "--render", action="store_true", help="core使用系统Chrome加载动态页面并滚动"
    )
    parser.add_argument("--chrome", help="Chrome可执行文件路径，需要--render")
    parser.add_argument("--render-timeout", type=float, default=None, help="渲染总期限秒，默认30")
    parser.add_argument("--max-scrolls", type=int, default=None, help="滚动上限，默认100")
    parser.add_argument("--render-wait", type=float, default=None, help="到底后的稳定等待秒，默认1")
    parser.add_argument("-o", "--output", help="输出 PDF 路径")
    parser.add_argument("--selector", help="图片区域选择器，如 'div.reader img'")
    parser.add_argument("--order", default="auto", choices=["auto", "dom", "asc", "desc"])
    parser.add_argument("--referer", help="自定义 Referer")
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--end", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--rate", type=float, default=4.0, help="每站请求/秒")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--dpi", type=int, default=150)
    parser.add_argument(
        "--paper", default="original", choices=["original", "a4", "a5", "b5", "letter"]
    )
    parser.add_argument("--min-width", type=int, default=200)
    parser.add_argument("--min-height", type=int, default=200)
    parser.add_argument("--min-size", type=float, default=0, help="最小图片 KB")
    parser.add_argument("--max-bytes", type=int, default=32 * 1024 * 1024)
    parser.add_argument("--keep-images", action="store_true", help="保留下载原图")
    parser.add_argument("--workdir", help="缓存与账本目录，默认在输出旁")
    parser.add_argument("--explain", action="store_true")
    parser.add_argument("--explain-limit", type=int, default=30)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument("--compress", choices=tuple(PRESETS), help="core 默认 balanced")
    parser.add_argument("--target-size", help="core 成品目标，例如 50MB 或 50MiB")
    parser.add_argument("--format", help="core 输出格式，可组合 pdf,cbz,zip；默认 pdf")
    parser.add_argument("--no-bitonal", action="store_true", help="禁用纯黑白页 1-bit 编码")
    parser.add_argument("--no-split-tall", action="store_true", help="保留长条漫为一页")


def compression_options(args: argparse.Namespace) -> CompressionOptions | None:
    requested = args.compress or args.target_size or args.no_bitonal or args.no_split_tall
    if not args.core:
        if requested:
            raise ConfigError("压缩与切页参数需要 --core")
        return None
    preset = args.compress or "balanced"
    target = None if preset == "lossless" else 50_000_000
    if args.target_size is not None:
        target = parse_size(args.target_size)
    return CompressionOptions(preset, target, not args.no_bitonal, not args.no_split_tall)


def render_options(
    args: argparse.Namespace,
    *,
    chrome_for_pdf: bool = False,
) -> RenderOptions | None:
    requested = (
        (args.chrome is not None and not chrome_for_pdf)
        or args.render_timeout is not None
        or args.max_scrolls is not None
        or args.render_wait is not None
    )
    if not args.render:
        if requested:
            raise ConfigError("浏览器参数需要--render")
        return None
    if not args.core:
        raise ConfigError("--render需要--core")
    from .errors import UnsupportedError

    try:
        from .fetch.browser import RenderOptions
        from .fetch.browser_process import find_chrome
    except ImportError:
        raise UnsupportedError("缺少动态采集能力", hint="安装quire-local[core]") from None
    executable = find_chrome(args.chrome)
    if executable is None:
        raise UnsupportedError(
            "动态采集需要系统Chrome/Edge/Brave/Chromium", hint="安装Chrome或指定--chrome"
        )
    return RenderOptions(
        executable=executable,
        timeout=30 if args.render_timeout is None else args.render_timeout,
        max_scrolls=100 if args.max_scrolls is None else args.max_scrolls,
        settle=1 if args.render_wait is None else args.render_wait,
    )


def add_novel_options(parser: argparse.ArgumentParser) -> None:
    """小说参数。小说依赖 core，没有单独的 --core 开关。"""
    parser.add_argument("url", help="目录页或单章地址")
    parser.add_argument("-o", "--output", help="输出路径，默认当前目录的 book.epub")
    parser.add_argument("--format", help="epub,txt,pdf；默认 epub")
    parser.add_argument("--chapter-selector", help="目录页里的章节链接选择器")
    parser.add_argument("--content-selector", help="正文容器选择器")
    parser.add_argument("--next-selector", help="分页的下一页链接选择器")
    parser.add_argument("--max-chapters", type=int, default=2000)
    parser.add_argument("--max-pages", type=int, default=20, help="单章最多跟随的分页数")
    parser.add_argument("--referer")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--rate", type=float, default=4.0, help="每站请求/秒")
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--max-bytes", type=int, default=32 * 1024 * 1024)
    parser.add_argument("--keep-html", action="store_true", help="保留每章原始 HTML 以便调规则")
    parser.add_argument(
        "--resume", action="store_true", help="继续上次任务：复用已抓取章节（不覆盖同名成品）"
    )
    parser.add_argument("--workdir", help="缓存与账本目录，默认在输出旁")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--render", action="store_true", help="用系统 Chrome 渲染动态页面")
    parser.add_argument("--chrome", help="Chrome 可执行文件路径，用于 --render 或 PDF 导出")
    parser.add_argument("--render-timeout", type=float, default=None, help="渲染总期限秒，默认30")
    parser.add_argument("--max-scrolls", type=int, default=None, help="滚动上限，默认100")
    parser.add_argument("--render-wait", type=float, default=None, help="到底后的稳定等待秒，默认1")
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.set_defaults(core=True)
