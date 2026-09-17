"""Site adaptation, series, offline rebuild and portable settings commands."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .cli_console import data_home, info
from .cli_options import add_manga_options, compression_options, render_options
from .cli_progress import Progress
from .errors import ConfigError
from .export_options import parse_formats
from .fetch.simple import Fetcher
from .models import MangaOptions
from .parse.images import FilterPolicy
from .parse.minidom import parse
from .sites.diagnostics import diagnose
from .sites.rules import new_rule, resolve_rule, rule_path


def _root(args: argparse.Namespace) -> Path:
    return Path(args.data_dir).expanduser() if args.data_dir else data_home()


def cmd_series(args: argparse.Namespace) -> int:
    from .core_series import run_series

    rule = resolve_rule(_root(args), args.url, args.site)
    opts = MangaOptions(
        selector=args.selector,
        order=args.order,
        referer=args.referer,
        max_bytes=args.max_bytes,
        concurrency=args.concurrency,
        rate=args.rate,
        retries=args.retries,
        timeout=args.timeout,
        dpi=args.dpi,
        paper=args.paper,
        keep_images=args.keep_images,
        overwrite=args.overwrite,
        policy=FilterPolicy(args.min_width, args.min_height, int(args.min_size * 1024)),
    )
    result = asyncio.run(
        run_series(
            args.url,
            args.output or "series",
            split_by=" ".join(args.split_by),
            first=args.start,
            last=args.end,
            rule=rule,
            options=opts,
            compression=compression_options(args),
            formats=parse_formats(args.format),
            workdir=args.workdir,
            render=render_options(args),
            progress=Progress(enabled=not args.quiet),
            on_volume=lambda r: info(f"已交付：{r.output}"),
        )
    )
    for warning in result.warnings:
        info(warning)
    return 4 if result.partial else 0


def cmd_reassemble(args: argparse.Namespace) -> int:
    from .core_reassemble import run_reassemble

    result = asyncio.run(
        run_reassemble(
            args.task_id,
            args.output,
            workdir=args.workdir,
            formats=parse_formats(args.format),
            overwrite=args.overwrite,
            compression=compression_options(args),
        )
    )
    info(f"已离线重组：{result.output}")
    return 4 if result.partial else 0


def cmd_sites(args: argparse.Namespace) -> int:
    root = _root(args)
    if args.action == "list":
        for path in sorted((root / "sites").glob("*.toml")):
            info(path.stem)
        return 0
    if args.action == "new":
        info(str(new_rule(root, args.name)))
        return 0
    if args.action == "set":
        return _set_rule(root, args.name, args.key, args.value)
    rule = resolve_rule(root, args.url, args.name)
    page = Fetcher(timeout=20, retries=1, rate=4).get(args.url)
    result = diagnose(parse(page.text, base_url=page.url), page.url, rule)
    info(json.dumps(result, ensure_ascii=False, indent=2))
    return 3 if result["warnings"] else 0


def _set_rule(root: Path, name: str, key: str, value: str) -> int:
    import os
    import re

    from .sites.rules import load_rule

    keys = {
        "catalogue.chapter_links",
        "catalogue.volumes",
        "chapter.image_selector",
        "novel.content",
        "novel.next",
    }
    if key not in keys:
        raise ConfigError("只支持修改选择器字段")
    parse("").select(value)
    path = rule_path(root, name)
    load_rule(root, name)
    section, field = key.split(".")
    lines = path.read_text("utf-8").splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == f"[{section}]"), None)
    if start is None:
        lines.extend([f"[{section}]", f"{field} = {json.dumps(value, ensure_ascii=False)}"])
    else:
        end = next(
            (i for i in range(start + 1, len(lines)) if lines[i].strip().startswith("[")),
            len(lines),
        )
        index = next(
            (i for i in range(start + 1, end) if re.match(rf"\s*{field}\s*=", lines[i])), end
        )
        replacement = f"{field} = {json.dumps(value, ensure_ascii=False)}"
        if index == end:
            lines.insert(index, replacement)
        else:
            lines[index] = replacement
    import tomllib

    rewritten = "\n".join(lines) + "\n"
    try:
        check = tomllib.loads(rewritten)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError("此规则使用多行或非标准表写法，请直接编辑 TOML；原文件已保留") from exc
    expected = tomllib.loads(path.read_text("utf-8"))
    expected.setdefault(section, {})[field] = value
    if check != expected:
        raise ConfigError("选择器修改会影响其他字段，请直接编辑 TOML；原文件已保留")
    # Validate the rewritten document before atomically replacing this user-named rule.
    temporary = path.with_suffix(".tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    os.replace(temporary, path)
    info(str(path))
    return 0


def cmd_profile(args: argparse.Namespace) -> int:
    from .server import settings

    root = _root(args)
    settings_path = root / "settings.json"
    if args.action == "export":
        current = asdict(settings.load(settings_path, root))
        current.pop("output_dir")  # No machine-local path or credentials in portable profiles.
        with args.file.open("x", encoding="utf-8") as handle:
            json.dump({"version": 1, "settings": current}, handle, ensure_ascii=False, indent=2)
    else:
        if args.file.stat().st_size > 64 * 1024:
            raise ConfigError("profile 超过 64 KiB")
        payload = json.loads(args.file.read_text("utf-8"))
        if (
            not isinstance(payload, dict)
            or set(payload) != {"version", "settings"}
            or type(payload["version"]) is not int
            or payload["version"] != 1
        ):
            raise ConfigError("profile 格式或版本不支持")
        values = payload["settings"]
        if not isinstance(values, dict) or "output_dir" in values:
            raise ConfigError("profile 不能修改本机输出目录")
        merged = {**asdict(settings.load(settings_path, root)), **values}
        settings.save(settings_path, settings.parse(merged))
    info(f"profile {args.action} 完成")
    return 0


def inspect_rules(args: argparse.Namespace) -> int:
    from .workspace import write_bytes

    rule = resolve_rule(_root(args), args.url, args.site)
    page = Fetcher(timeout=args.timeout, retries=1, rate=args.rate).get(
        args.url, referer=args.referer
    )
    result = diagnose(parse(page.text, base_url=page.url), page.url, rule)
    if not args.explain:
        result.pop("rejections")
    else:
        result["rejections"] = result["rejections"][: args.explain_limit]
    info(json.dumps(result, ensure_ascii=False, indent=2))
    if args.dump_html:
        dump = Path(args.dump_html)
        if str(dump) == "debug":
            dump.mkdir(exist_ok=True)
        if dump.is_dir():
            dump = dump / "page.html"
        write_bytes(dump, page.text.encode("utf-8"))
        info(f"HTML 已存到 {dump}")
    return 0 if result["images"] or result["chapters"] or result["text_valid"] else 3


def add_commands(sub: Any) -> None:
    series = sub.add_parser("series", help="漫画目录按卷、章节数或实测体积分卷")
    add_manga_options(series)
    series.add_argument("--split-by", nargs="+", default=["none"])
    series.add_argument("--from", dest="start", type=int, default=1)
    series.add_argument("--to", dest="end", type=int, default=0)
    series.set_defaults(core=True, func=cmd_series)
    reassemble = sub.add_parser("reassemble", help="从保留的原图离线重新导出")
    reassemble.add_argument("task_id")
    reassemble.add_argument("-o", "--output", type=Path, required=True)
    reassemble.add_argument("--workdir", type=Path, required=True)
    reassemble.add_argument("--format", default="pdf")
    reassemble.add_argument("--compress", default="balanced")
    reassemble.add_argument("--target-size")
    reassemble.add_argument("--overwrite", action="store_true")
    reassemble.set_defaults(core=True, no_bitonal=False, no_split_tall=False, func=cmd_reassemble)
    sites = sub.add_parser("sites", help="本地 TOML 站点规则")
    sites.add_argument("--data-dir")
    actions = sites.add_subparsers(dest="action", required=True)
    actions.add_parser("list")
    new = actions.add_parser("new")
    new.add_argument("name")
    test = actions.add_parser("test")
    test.add_argument("name")
    test.add_argument("url")
    setter = actions.add_parser("set")
    setter.add_argument("name")
    setter.add_argument("key")
    setter.add_argument("value")
    sites.set_defaults(func=cmd_sites)
    profile = sub.add_parser("profile", help="导入导出不含本机路径的设置")
    profile.add_argument("--data-dir")
    profile.add_argument("action", choices=("import", "export"))
    profile.add_argument("file", type=Path)
    profile.set_defaults(func=cmd_profile)
