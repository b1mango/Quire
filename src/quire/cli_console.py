"""CLI 输出、退出码与能力探测：被各命令模块共用（项目设计.md §20.1、§20.2）。"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path
from typing import Never

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


def module_available(name: str) -> bool:
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
