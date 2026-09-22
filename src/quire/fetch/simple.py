"""Bounded stdlib HTTP transport for the dependency-free micro build."""

from __future__ import annotations

import http.client
import io
import math
import random
import threading
import time
import urllib.error
import urllib.request
import zlib
from collections.abc import Buffer, Callable, Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Protocol
from urllib.parse import urlsplit

from ..errors import BlockedError, ConfigError, FetchError, HttpStatusError, NetworkError
from ..utils.urls import is_usable_url, redact
from .ratelimit import HostRateLimiter
from .robots import RobotsPolicy
from .text import decode_html

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)
DEFAULT_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,"
    "image/avif,image/webp,image/png,image/svg+xml,*/*;q=0.8"
)
DEFAULT_LANGUAGE = "zh-CN,zh;q=0.9,en;q=0.8"
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

BLOCKED_HINT = "站点拒绝了访问；该站可能限制当前网络环境，可在设置请求头后重试。"
CLOUDFLARE_HINT = (
    "站点启用了 Cloudflare 等安全防护，当前网络环境被拦截；"
    "可在本机 Chrome 完成验证或更换网络环境后重试。"
)
CONNECTION_HINT = (
    "连接未能建立或被中途重置；站点可能对当前网络环境不可达"
    "（防火墙/运营商拦截或站点故障），可更换网络环境后重试。"
)


def cloudflare_block(headers: Mapping[str, str], body: bytes) -> bool:
    """403 响应是否来自 Cloudflare 等防护拦截（区别于普通防盗链）。"""
    lowered = {key.lower(): value for key, value in headers.items()}
    if "cf-ray" in lowered or "cloudflare" in lowered.get("server", "").lower():
        return True
    return b"cloudflare" in body[:8192].lower()


def blocked_error(url: str, headers: Mapping[str, str], body: bytes) -> BlockedError:
    cloudflare = cloudflare_block(headers, body)
    error = BlockedError(
        f"HTTP 403: {redact(url)}", hint=CLOUDFLARE_HINT if cloudflare else BLOCKED_HINT
    )
    error.cloudflare = cloudflare
    return error


@dataclass(frozen=True, slots=True)
class Response:
    url: str
    status: int
    headers: Mapping[str, str]
    content: bytes
    elapsed_ms: int

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()

    @property
    def text(self) -> str:
        return decode_html(self.content, self.headers.get("content-type", ""))


class FetchPort(Protocol):
    def get(
        self,
        url: str,
        *,
        referer: str | None = None,
        headers: Mapping[str, str] | None = None,
        robots: bool = True,
    ) -> Response: ...


class _CheckedReader(io.RawIOBase):
    """Check between socket reads, including HTTP chunk metadata read by urllib."""

    def __init__(self, source: io.BufferedReader, check: Callable[[], None]) -> None:
        self.source, self.check = source, check

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Buffer) -> int:
        self.check()
        view = memoryview(buffer).cast("B")
        data = self.source.read1(len(view))
        self.check()
        view[: len(data)] = data
        return len(data)

    def close(self) -> None:
        try:
            self.source.close()
        finally:
            super().close()


def retry_after(value: str, now: float) -> float:
    try:
        seconds = float(value)
        return max(0.0, seconds) if math.isfinite(seconds) else 0.0
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - now)
        except (ValueError, TypeError, OverflowError):
            return 0.0


class _HttpOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, check: Callable[[str], None]) -> None:
        self.check = check

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        # urllib otherwise drains the entire redirect body with an unbounded read.
        fp.close()
        if not is_usable_url(newurl) or urlsplit(newurl).scheme not in {"http", "https"}:
            raise NetworkError("Redirect target must be HTTP(S)")
        self.check(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Fetcher:
    def __init__(
        self,
        *,
        timeout: float = 20.0,
        retries: int = 3,
        concurrency: int = 8,
        rate: float = 4.0,
        user_agent: str = DEFAULT_UA,
        max_bytes: int = 32 * 1024 * 1024,
        cancel: threading.Event | None = None,
        rng: random.Random | None = None,
        respect_robots: bool = True,
    ) -> None:
        if (
            not math.isfinite(timeout)
            or not math.isfinite(rate)
            or timeout <= 0
            or rate <= 0
            or retries < 0
            or concurrency < 1
            or max_bytes < 1
        ):
            raise ConfigError("Invalid HTTP timeout, rate, retry or size limit")
        self.timeout, self.retries = timeout, retries
        self.concurrency, self.max_bytes = concurrency, max_bytes
        self.user_agent = user_agent
        self.cancel = cancel or threading.Event()
        self.limiter = HostRateLimiter(rate, sleep=self._sleep)
        self.rng = rng or random.Random()
        self._local = threading.local()
        self.respect_robots = respect_robots
        self.robots = RobotsPolicy(self._robots_text)

    def _robots_text(self, url: str) -> str:
        self._local.fetching_policy = True
        try:
            return self._get(url).text
        finally:
            self._local.fetching_policy = False

    def _redirect_check(self, url: str) -> None:
        skip = (
            not self.respect_robots
            or getattr(self._local, "fetching_policy", False)
            or getattr(self._local, "skip_robots", False)
        )
        if not skip:
            self.robots.check(url)
        self.limiter.acquire(url)

    def _sleep(self, delay: float) -> None:
        if self.cancel.wait(delay):
            raise FetchError("Task cancelled")

    def get(
        self,
        url: str,
        *,
        referer: str | None = None,
        headers: Mapping[str, str] | None = None,
        robots: bool = True,
    ) -> Response:
        if not is_usable_url(url) or urlsplit(url).scheme not in {"http", "https"}:
            raise ConfigError("URL must be an absolute HTTP(S) address without credentials")
        self._sleep(0)
        check = robots and self.respect_robots
        if check:
            self.robots.check(url)
        self._local.skip_robots = not check  # 图片等二进制请求的重定向也不再查 robots
        try:
            return self._get(url, referer=referer, headers=headers)
        finally:
            self._local.skip_robots = False

    def _get(
        self, url: str, *, referer: str | None = None, headers: Mapping[str, str] | None = None
    ) -> Response:
        if not is_usable_url(url) or urlsplit(url).scheme not in {"http", "https"}:
            raise ConfigError("URL must be an absolute HTTP(S) address without credentials")
        request_headers = {
            "User-Agent": self.user_agent,
            "Accept": DEFAULT_ACCEPT,
            "Accept-Language": DEFAULT_LANGUAGE,
            "Accept-Encoding": "gzip, deflate",
        }
        if referer:
            request_headers["Referer"] = referer
        request_headers.update(headers or {})
        last: FetchError = NetworkError(f"Request failed: {redact(url)}")
        for attempt in range(self.retries + 1):
            self._sleep(0)
            self.limiter.acquire(url)
            try:
                return self._request(url, request_headers)
            except urllib.error.HTTPError as exc:
                status = exc.code
                delay = retry_after(exc.headers.get("Retry-After", ""), time.time())
                body = b""
                if status == 403:
                    try:
                        body = exc.read(65536)
                    except (OSError, http.client.HTTPException):
                        body = b""  # sniffing is best-effort; the block stands either way
                exc.close()
                if status == 403:
                    raise blocked_error(url, dict(exc.headers), body) from None
                last = HttpStatusError(url, status)
                if status not in RETRYABLE_STATUS or delay > 120:
                    raise last from None
                self.limiter.defer(url, delay)
            except (OSError, urllib.error.URLError, http.client.HTTPException, zlib.error) as exc:
                last = NetworkError(f"{type(exc).__name__}: {redact(url)}", hint=CONNECTION_HINT)
            if attempt < self.retries:
                self._sleep(min(8.0, 0.6 * 2**attempt) * self.rng.uniform(0.6, 1.4))
        raise last

    def _request(self, url: str, headers: Mapping[str, str]) -> Response:
        if not hasattr(self._local, "opener"):
            self._local.opener = urllib.request.build_opener(
                _HttpOnlyRedirect(self._redirect_check)
            )
        started = time.monotonic()
        request = urllib.request.Request(url, headers=dict(headers))
        with self._local.opener.open(request, timeout=self.timeout) as response:
            body = self._read_body(response)
            return Response(
                response.geturl(),
                response.status,
                {k.lower(): v for k, v in response.headers.items()},
                body,
                int((time.monotonic() - started) * 1000),
            )

    def _read_body(self, response: http.client.HTTPResponse) -> bytes:
        declared = response.headers.get("Content-Length", "")
        if declared.isdigit() and int(declared) > self.max_bytes:
            raise FetchError("Response exceeds configured size limit")
        raw = bytearray()
        deadline = time.monotonic() + self.timeout

        def check() -> None:
            self._sleep(0)
            if time.monotonic() > deadline:
                raise TimeoutError("Response deadline exceeded")

        if response.fp is not None:
            response.fp = io.BufferedReader(_CheckedReader(response.fp, check))
        while True:
            check()
            chunk = response.read1(min(65536, self.max_bytes + 1 - len(raw)))
            check()
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > self.max_bytes:
                raise FetchError("Response exceeds configured size limit")
        if declared.isdigit() and len(raw) != int(declared):
            raise http.client.IncompleteRead(bytes(raw), int(declared))
        encoding = response.headers.get("Content-Encoding", "").strip().lower()
        if encoding in {"gzip", "deflate"}:
            return self._decode(bytes(raw), encoding)
        if encoding not in {"", "identity"}:
            raise FetchError(f"Unsupported content encoding: {encoding}")
        return bytes(raw)

    def _decode(self, raw: bytes, encoding: str) -> bytes:
        modes = [31] if encoding == "gzip" else [15, -15]
        for index, mode in enumerate(modes):
            try:
                decoded = bytearray()
                pending = raw
                while True:
                    self._sleep(0)
                    decoder = zlib.decompressobj(mode)
                    decoded.extend(decoder.decompress(pending, self.max_bytes + 1 - len(decoded)))
                    if len(decoded) > self.max_bytes:
                        raise FetchError("Decoded response exceeds configured size limit")
                    if not decoder.eof:
                        raise zlib.error("Incomplete compressed response")
                    pending = decoder.unused_data
                    if not pending:
                        return bytes(decoded)
                    if encoding != "gzip":
                        raise zlib.error("Trailing data after compressed response")
            except zlib.error:
                if index == len(modes) - 1:
                    raise
        raise zlib.error("Invalid compressed response")
