"""Web UI 设置：JSON 持久化、严格校验、原子写入（项目设计.md §20.3）。

设置文件损坏或字段非法时清晰报错且不改写原文件；未知字段拒绝，
避免把拼错的键默默吞掉。
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..errors import ConfigError
from ..fetch.browser_endpoint import validate_endpoint
from ..image.options import PRESETS
from ..workspace import atomic_output

THEMES = ("paper:light", "darkroom:light", "darkroom:dark", "swiss:light")
MAX_RATE = 5.0  # 应用默认策略上限，并非站点承载能力测量值
OCR_MODES = ("auto", "always", "never")


@dataclass(frozen=True, slots=True)
class UiSettings:
    output_dir: str
    compress: str = "balanced"
    task_novel_mb: int = 100
    task_manga_mb: int = 500
    concurrency: int = 12
    rate: float = 4.0
    ocr: str = "auto"
    theme: str = "paper:light"
    auto_check_updates: bool = False
    obey_robots: bool = False
    browser_native: bool = False
    cdp_endpoint: str = ""

    def __post_init__(self) -> None:
        if type(self.browser_native) is not bool or not isinstance(self.cdp_endpoint, str):
            raise ConfigError("浏览器设置类型错误")
        if self.cdp_endpoint:
            validate_endpoint(self.cdp_endpoint)
        output = Path(self.output_dir).expanduser()
        if not output.is_absolute():
            raise ConfigError("输出目录须为绝对路径")
        if output.exists() and not output.is_dir():
            raise ConfigError(f"输出目录不是文件夹：{output}")
        if self.compress not in PRESETS:
            raise ConfigError("未知压缩档位")
        if type(self.task_novel_mb) is not int or not 1 <= self.task_novel_mb <= 1_000_000:
            raise ConfigError("小说任务体积上限须为 1-1000000 MB")
        if type(self.task_manga_mb) is not int or not 1 <= self.task_manga_mb <= 1_000_000:
            raise ConfigError("漫画任务体积上限须为 1-1000000 MB")
        if type(self.concurrency) is not int or not 1 <= self.concurrency <= 16:
            raise ConfigError("并发须为 1-16")
        if (
            type(self.rate) not in {int, float}
            or not math.isfinite(self.rate)
            or not 0 < self.rate <= MAX_RATE
        ):
            raise ConfigError("每站限速须大于 0 且不超过 5 次/秒")
        if self.ocr not in OCR_MODES:
            raise ConfigError("OCR 模式须为 auto、always 或 never")
        if self.theme not in THEMES:
            raise ConfigError("未知主题")
        if type(self.auto_check_updates) is not bool:
            raise ConfigError("自动检查追更开关须为布尔值")
        if type(self.obey_robots) is not bool:
            raise ConfigError("robots.txt 开关须为布尔值")

    @property
    def output_path(self) -> Path:
        return Path(self.output_dir).expanduser()


def defaults(data_root: Path) -> UiSettings:
    return UiSettings(str(data_root / "library"))


def parse(payload: Any) -> UiSettings:
    if not isinstance(payload, dict):
        raise ConfigError("设置格式不正确")
    payload = {key: value for key, value in payload.items() if key != "target_mb"}
    legacy_presets = {"lossless": "archive", "high": "balanced", "tiny": "small"}
    if payload.get("compress") in legacy_presets:
        payload = {**payload, "compress": legacy_presets[payload["compress"]]}
    known = {
        "output_dir",
        "compress",
        "task_novel_mb",
        "task_manga_mb",
        "concurrency",
        "rate",
        "ocr",
        "theme",
        "auto_check_updates",
        "obey_robots",
        "browser_native",
        "cdp_endpoint",
    }
    unknown = set(payload) - known
    if unknown:
        raise ConfigError(f"未知设置项：{sorted(unknown)[0]}")
    if not isinstance(payload.get("output_dir"), str) or not payload["output_dir"].strip():
        raise ConfigError("输出目录不能为空")
    try:
        return UiSettings(**{**payload, "output_dir": payload["output_dir"].strip()})
    except TypeError:
        raise ConfigError("设置字段类型不正确") from None


def load(path: Path, data_root: Path) -> UiSettings:
    if not path.exists():
        return defaults(data_root)
    try:
        payload = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ConfigError(f"设置文件损坏：{path}") from None
    return parse(payload)


def save(path: Path, settings: UiSettings) -> None:
    settings.output_path.mkdir(parents=True, exist_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(settings), ensure_ascii=False, indent=2) + "\n"
    with atomic_output(path, overwrite=True) as handle:
        handle.write(payload.encode("utf-8"))
