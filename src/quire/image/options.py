"""Pure compression settings and bounded encoding passes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from ..errors import ConfigError

PRESETS = {
    "archive": (0, 0),
    "balanced": (80, 2000),
    "small": (70, 1600),
}


def parse_size(value: str) -> int:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*(B|KB|MB|GB|KiB|MiB|GiB)?", value.strip(), re.I)
    if match is None:
        raise ConfigError("体积格式应为 50MB、50MiB 或正整数字节")
    units = {
        "b": 1,
        "kb": 1000,
        "mb": 1000**2,
        "gb": 1000**3,
        "kib": 1024,
        "mib": 1024**2,
        "gib": 1024**3,
    }
    size = Decimal(match[1]) * units[(match[2] or "B").lower()]
    if size != int(size) or not 1 <= size <= 10**12:
        raise ConfigError("目标体积须为 1 byte 至 1 TB 的整数字节")
    return int(size)


@dataclass(frozen=True, slots=True)
class Encoding:
    quality: int
    max_edge: int
    lossless: bool = False


@dataclass(frozen=True, slots=True)
class CompressionOptions:
    preset: str = "balanced"
    target_bytes: int | None = 50_000_000
    bitonal: bool = True
    split_tall: bool = True

    def __post_init__(self) -> None:
        if self.preset not in PRESETS:
            raise ConfigError("未知压缩档位")
        if self.target_bytes is not None and (
            type(self.target_bytes) is not int or not 1 <= self.target_bytes <= 10**12
        ):
            raise ConfigError("目标体积须为 1 byte 至 1 TB 的整数字节")

    def passes(self) -> tuple[Encoding, ...]:
        quality, edge = PRESETS[self.preset]
        initial = Encoding(quality, edge, self.preset == "archive")
        if initial.lossless or self.target_bytes is None:
            return (initial,)
        middle = Encoding(max(60, (quality + 60) // 2), max(1200, (edge or 2800) - 400))
        return tuple(dict.fromkeys((initial, middle, Encoding(60, 1200))))
