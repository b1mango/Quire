"""Manga argument declarations and core compression option validation."""

from __future__ import annotations

import argparse

from .errors import ConfigError
from .image.options import PRESETS, CompressionOptions, parse_size


def add_manga_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("url")
    parser.add_argument("--core", action="store_true", help="使用 core 连接池、压缩和本地账本")
    parser.add_argument("--resume", action="store_true", help="校验并复用 core 任务已完成图片")
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
