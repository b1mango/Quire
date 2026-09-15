"""Pure format selection and paths for a group of book outputs."""

from __future__ import annotations

from pathlib import Path

from .errors import ConfigError

FORMATS = ("pdf", "cbz", "zip")


def validate_formats(formats: tuple[str, ...]) -> tuple[str, ...]:
    if not formats or len(set(formats)) != len(formats) or any(f not in FORMATS for f in formats):
        raise ConfigError("格式须为 pdf、cbz、zip，不得为空或重复")
    return formats


def parse_formats(value: str | None) -> tuple[str, ...]:
    return validate_formats(
        tuple(part.strip().lower() for part in ("pdf" if value is None else value).split(","))
    )


def output_paths(output: Path, formats: tuple[str, ...]) -> tuple[Path, ...]:
    validate_formats(formats)
    if output.suffix and output.suffix.lower() != f".{formats[0]}":
        raise ConfigError(f"输出后缀须匹配首个格式 .{formats[0]}，或省略后缀")
    return tuple(output.with_suffix(f".{format}") for format in formats)


def available_output(output: Path, formats: tuple[str, ...], *, overwrite: bool) -> Path:
    paths = output_paths(output, formats)
    original = paths[0]
    if overwrite:
        return original
    number = 0
    while any(
        path.exists() or path.is_symlink()
        for path in (*paths, paths[0].with_suffix(".report.json"))
    ):
        number += 1
        paths = output_paths(
            original.with_name(f"{original.stem} ({number}){original.suffix}"), formats
        )
    return paths[0]
