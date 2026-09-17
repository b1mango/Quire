"""小说成品的格式选择与路径（项目设计.md §37.6）。与漫画的 pdf/cbz/zip 分开。"""

from __future__ import annotations

from pathlib import Path

from .errors import ConfigError

NOVEL_FORMATS = ("epub", "txt", "pdf")


def validate_novel_formats(formats: tuple[str, ...]) -> tuple[str, ...]:
    if (
        not formats
        or len(set(formats)) != len(formats)
        or any(item not in NOVEL_FORMATS for item in formats)
    ):
        raise ConfigError("小说格式须为 epub、txt、pdf，不得为空或重复")
    return formats


def parse_novel_formats(value: str | None) -> tuple[str, ...]:
    return validate_novel_formats(
        tuple(part.strip().lower() for part in ("epub" if value is None else value).split(","))
    )


def novel_output_paths(output: Path, formats: tuple[str, ...]) -> tuple[Path, ...]:
    validate_novel_formats(formats)
    if output.suffix and output.suffix.lower() != f".{formats[0]}":
        raise ConfigError(f"输出后缀须匹配首个格式 .{formats[0]}，或省略后缀")
    return tuple(output.with_suffix(f".{fmt}") for fmt in formats)


def available_novel_output(output: Path, formats: tuple[str, ...], *, overwrite: bool) -> Path:
    """目标已存在时返回 ``书名 (1).epub``；与漫画保持同一套命名习惯。"""
    paths = novel_output_paths(output, formats)
    original = paths[0]
    if overwrite:
        return original
    number = 0
    while any(
        path.exists() or path.is_symlink()
        for path in (*paths, paths[0].with_suffix(".report.json"))
    ):
        number += 1
        paths = novel_output_paths(
            original.with_name(f"{original.stem} ({number}){original.suffix}"), formats
        )
    return paths[0]
