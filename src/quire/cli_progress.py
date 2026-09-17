"""Small terminal progress sink shared by the capture backends."""

from __future__ import annotations

import sys
import time

from .models import MangaResult, NovelResult


class Progress:
    def __init__(self, *, enabled: bool = True, width: int = 24) -> None:
        self.enabled = enabled and sys.stderr.isatty()
        self.width = width
        self._last = 0.0

    def update(
        self, done: int, total: int, result: MangaResult | NovelResult | None = None
    ) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        if done < total and now - self._last < 0.1:
            return
        self._last = now
        ratio = done / total if total else 0
        filled = int(self.width * ratio)
        bar = "█" * filled + "·" * (self.width - filled)
        failed = _failures(result)
        tail = f"失败 {failed}" if failed else ""
        sys.stderr.write(f"\r  {bar} {done:>4}/{total}  {tail}   ")
        sys.stderr.flush()
        if done >= total:
            sys.stderr.write("\n")


def _failures(result: MangaResult | NovelResult | None) -> int:
    if isinstance(result, MangaResult):
        return result.pages_failed
    if isinstance(result, NovelResult):
        return result.chapters_failed
    return 0
