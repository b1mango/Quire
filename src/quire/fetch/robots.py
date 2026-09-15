"""Per-origin robots policy, fetched once for each crawler session."""

from __future__ import annotations

import threading
from collections.abc import Callable
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from ..errors import BlockedError, HttpStatusError


class RobotsPolicy:
    def __init__(self, fetch_text: Callable[[str], str]) -> None:
        self._fetch_text = fetch_text
        self._lock = threading.RLock()
        self._policies: dict[str, RobotFileParser] = {}

    def check(self, url: str) -> None:
        parts = urlsplit(url)
        origin = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
        if parts.path == "/robots.txt":
            return
        with self._lock:
            if origin not in self._policies:
                parser = RobotFileParser(origin + "/robots.txt")
                try:
                    body = self._fetch_text(origin + "/robots.txt")
                except HttpStatusError as exc:
                    if exc.status not in {404, 410}:
                        raise
                    body = ""
                parser.parse(body.splitlines())
                self._policies[origin] = parser
            if not self._policies[origin].can_fetch("Quire", url):
                raise BlockedError("robots.txt does not allow Quire to fetch this resource")
