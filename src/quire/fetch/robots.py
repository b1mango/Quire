"""Session-cached REP decisions for the fixed Quire product token.

micro cannot use RobotFileParser: it picks the first rule/group, lacks path
wildcards, and unquotes reserved octets. This small adapter reuses urllib.parse
for URL/UTF-8 handling and implements Allow/Disallow, longest literal-octet
match (Allow wins ties), '*' and terminal '$', and merged case-insensitive UA
groups. An explicit Quire group replaces the '*' fallback, even when empty.
Paths include queries, exclude fragments, and preserve encoded reserved octets.
Other records (including Crawl-delay and Sitemap) are ignored, not group breaks.

Wildcard matching searches literal segments in order, without backtracking or
generated regexes. The only regex has a fixed three-character percent token.
Transport size limits, retries and cancellation remain the fetcher's concern.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote, unquote_to_bytes, urlsplit, urlunsplit

from ..errors import BlockedError, HttpStatusError

_PERCENT = re.compile(r"%[0-9a-fA-F]{2}")
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def _percent_octet(match: re.Match[str]) -> str:
    octet = chr(int(match[0][1:], 16))
    return octet if octet in _UNRESERVED else match[0].upper()


def _normalize(path: str) -> str:
    return _PERCENT.sub(_percent_octet, quote(path, safe="/:@!$&'()*+,;=?%"))


@dataclass(frozen=True, slots=True)
class _Rule:
    segments: tuple[str, ...]
    anchored: bool
    specificity: int
    allow: bool

    def matches(self, path: str) -> bool:
        first, *rest = self.segments
        if not path.startswith(first):
            return False
        position = len(first)
        if not rest:
            return not self.anchored or position == len(path)
        for segment in rest[:-1]:
            found = path.find(segment, position)
            if found < 0:
                return False
            position = found + len(segment)
        last = rest[-1]
        if self.anchored:
            return path.endswith(last) and len(path) - len(last) >= position
        return path.find(last, position) >= 0


def _parse(body: str) -> tuple[_Rule, ...]:
    groups: list[tuple[list[str], list[_Rule]]] = []
    agents: list[str] = []
    rules: list[_Rule] = []
    in_rules = False
    for line in body.removeprefix("\ufeff").splitlines():
        field, separator, value = line.partition("#")[0].partition(":")
        if not separator:
            continue
        field, value = field.strip().lower(), value.strip()
        if field == "user-agent":
            if in_rules:
                groups.append((agents, rules))
                agents, rules = [], []
                in_rules = False
            agents.append(value.lower())
        elif field in {"allow", "disallow"} and agents:
            in_rules = True
            if value.startswith("/"):
                segments = tuple(_normalize(value.removesuffix("$")).split("*"))
                rules.append(
                    _Rule(
                        segments,
                        value.endswith("$"),
                        len(unquote_to_bytes("".join(segments))),
                        field == "allow",
                    )
                )
    groups.append((agents, rules))
    selected = "quire" if any("quire" in names for names, _ in groups) else "*"
    return tuple(rule for names, entries in groups if selected in names for rule in entries)


def _allowed(rules: tuple[_Rule, ...], url: str) -> bool:
    parts = urlsplit(url)
    path = _normalize((parts.path or "/") + ("?" + parts.query if parts.query else ""))
    return max(
        ((rule.specificity, rule.allow) for rule in rules if rule.matches(path)),
        default=(0, True),
    )[1]


class RobotsPolicy:
    def __init__(self, fetch_text: Callable[[str], str]) -> None:
        self._fetch_text = fetch_text
        self._lock = threading.RLock()
        self._policies: dict[str, tuple[_Rule, ...]] = {}

    def check(self, url: str) -> None:
        parts = urlsplit(url)
        origin = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
        if parts.path == "/robots.txt":
            return
        with self._lock:
            if origin not in self._policies:
                try:
                    body = self._fetch_text(origin + "/robots.txt")
                except HttpStatusError as exc:
                    if exc.status >= 500:
                        raise
                    # 4xx(含 404/410/反爬 403):按 REP 惯例视为没有限制
                    body = ""
                self._policies[origin] = _parse(body)
            rules = self._policies[origin]
        if not _allowed(rules, url):
            raise BlockedError("robots.txt does not allow Quire to fetch this resource")
