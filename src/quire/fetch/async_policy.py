"""Async host pacing and coalesced REP lookups using the shared rule parser."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from urllib.parse import urlsplit, urlunsplit

from ..errors import BlockedError, FetchError, HttpStatusError
from .robots import _allowed, _parse, _Rule


def _failure_copy(error: FetchError) -> FetchError:
    if isinstance(error, HttpStatusError):
        return HttpStatusError(error.url, error.status, hint=error.hint)
    return type(error)(error.message, hint=error.hint)


class HostPace:
    """按 host 的节流窗口状态，可在线程间共享（每个任务线程各有事件循环）。

    Web UI 并行任务共用一个实例：两个任务打同一站点时合计速率不超过设定值。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next: dict[str, float] = {}

    def delay(self, host: str) -> float:
        with self._lock:
            return self._next.get(host, 0) - time.monotonic()

    def try_reserve(self, host: str, interval: float) -> bool:
        """原子占用当前窗口：成功则把该 host 的下次可用时间后推一个间隔。"""
        with self._lock:
            now = time.monotonic()
            if self._next.get(host, 0) > now:
                return False
            self._next[host] = now + interval
            return True

    def defer(self, host: str, seconds: float) -> None:
        with self._lock:
            self._next[host] = max(self._next.get(host, 0), time.monotonic() + seconds)


class AsyncRateLimiter:
    def __init__(self, rate: float, pace: HostPace | None = None) -> None:
        self.interval = 1 / rate
        self._pace = pace or HostPace()

    @property
    def _next(self) -> dict[str, float]:
        return self._pace._next

    @asynccontextmanager
    async def admit(
        self, url: str, slots: asyncio.Semaphore, timeout: float
    ) -> AsyncIterator[float]:
        host = urlsplit(url).hostname or ""
        remaining = timeout
        while True:
            delay = self._pace.delay(host)
            if delay > 0:
                await asyncio.sleep(delay)
                continue
            queued = time.monotonic()
            async with asyncio.timeout(remaining):
                await slots.acquire()
            now = time.monotonic()
            remaining -= now - queued
            if self._pace.try_reserve(host, self.interval):
                break
            slots.release()
        try:
            yield remaining
        finally:
            slots.release()

    def defer(self, url: str, seconds: float) -> None:
        self._pace.defer(urlsplit(url).hostname or "", seconds)


class AsyncRobotsPolicy:
    def __init__(self, fetch_text: Callable[[str], Awaitable[str]]) -> None:
        self._fetch_text = fetch_text
        self._locks: dict[str, asyncio.Lock] = {}
        self._rules: dict[str, tuple[_Rule, ...]] = {}
        self._failures: dict[str, FetchError] = {}

    async def check(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.path == "/robots.txt":
            return
        origin = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
        previous_failure = self._failures.get(origin)
        async with self._locks.setdefault(origin, asyncio.Lock()):
            if origin not in self._rules:
                failure = self._failures.get(origin)
                if failure is not previous_failure:
                    assert failure is not None
                    raise _failure_copy(failure) from None
                try:
                    try:
                        text = await self._fetch_text(origin + "/robots.txt")
                    except HttpStatusError as exc:
                        if exc.status >= 500:
                            raise
                        # 4xx(含 404/410/反爬 403):按 REP 惯例视为没有限制
                        text = ""
                except FetchError as exc:
                    # Queued callers share this failure; a later call can try again.
                    self._failures[origin] = _failure_copy(exc)
                    raise
                self._rules[origin] = _parse(text)
            if not _allowed(self._rules[origin], url):
                raise BlockedError("robots.txt does not allow Quire to fetch this resource")
