"""Per-host pacing with an injectable monotonic clock."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from ..utils.urls import host_of


class HostRateLimiter:
    def __init__(
        self,
        rate: float = 4.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._interval = 1.0 / rate
        self._lock = threading.Lock()
        self._next_at: dict[str, float] = {}
        self._clock, self._sleep = clock, sleep

    def acquire(self, url: str) -> None:
        host = host_of(url)
        while True:
            with self._lock:
                now = self._clock()
                delay = self._next_at.get(host, 0.0) - now
                if delay <= 0:
                    self._next_at[host] = now + self._interval
                    return
            self._sleep(min(delay, 1.0))

    def defer(self, url: str, seconds: float) -> None:
        with self._lock:
            host = host_of(url)
            self._next_at[host] = max(self._next_at.get(host, 0), self._clock() + seconds)
