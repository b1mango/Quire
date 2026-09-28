"""Core asyncio HTTP/1.1 session with bounded bodies, policies and pooling."""

from __future__ import annotations

import asyncio
import math
import random
import time
from collections.abc import Mapping
from types import MappingProxyType
from typing import Protocol

import httpx

from ..errors import BlockedError, ConfigError, FetchError, HttpStatusError, NetworkError
from ..utils.urls import redact, route_fragment
from .async_policy import AsyncRateLimiter, AsyncRobotsPolicy, HostPace
from .challenge import challenge_target
from .decoding import read_body, sniff_body
from .request_shape import public_headers, redirect_target, valid_url
from .simple import CONNECTION_HINT, RETRYABLE_STATUS, Response, blocked_error, retry_after

_REDIRECTS = {301, 302, 303, 307, 308}


class Escalation(Protocol):
    """Cloudflare 盾拦截时的升级通道（fetch/cloudflare.ClearanceEscalation 实现）。

    ``__call__`` 负责完成质询并把 cf_clearance 回注会话；无法自动通过时
    如实抛出 BlockedError，绝不伪成功。``reject`` 在回注后仍被 403 拒绝时
    调用，标记该站升级无效，避免逐章重复启动浏览器。
    """

    async def __call__(self, url: str) -> None: ...

    def reject(self, url: str) -> None: ...


class AsyncFetcher:
    def __init__(
        self,
        *,
        timeout: float = 20,
        retries: int = 3,
        concurrency: int = 4,
        rate: float = 4,
        max_bytes: int = 32 * 1024 * 1024,
        transport: httpx.AsyncBaseTransport | None = None,
        rng: random.Random | None = None,
        pace: HostPace | None = None,
        respect_robots: bool = True,
        cookie_hosts: frozenset[str] = frozenset(),
    ) -> None:
        if (
            not math.isfinite(timeout)
            or timeout <= 0
            or not math.isfinite(rate)
            or rate <= 0
            or type(retries) is not int
            or not 0 <= retries <= 10
            or type(concurrency) is not int
            or not 1 <= concurrency <= 32
            or type(max_bytes) is not int
            or not 1 <= max_bytes <= 128 * 1024 * 1024
        ):
            raise ConfigError("Invalid core HTTP timeout, rate, concurrency, retry or size limit")
        self.timeout, self.retries, self.max_bytes = timeout, retries, max_bytes
        self.respect_robots = respect_robots
        self.cookie_hosts = {h.lower() for h in cookie_hosts}
        self.escalation: Escalation | None = None
        self._host_agents: dict[str, str] = {}
        self.limiter = AsyncRateLimiter(rate, pace)
        self._slots = asyncio.Semaphore(concurrency)
        self._transport, self._concurrency = transport, concurrency
        self._rng = rng or random.Random()
        self._client: httpx.AsyncClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closing = False
        self._active: set[asyncio.Task[object]] = set()
        self.robots = AsyncRobotsPolicy(self._robots_text)

    async def __aenter__(self) -> AsyncFetcher:
        if self._loop is not None:
            raise ConfigError("AsyncFetcher cannot be entered more than once")
        self._loop = asyncio.get_running_loop()
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            limits=httpx.Limits(
                max_connections=self._concurrency, max_keepalive_connections=self._concurrency
            ),
            follow_redirects=False,
            trust_env=False,
            transport=self._transport,
        )
        await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._loop is not asyncio.get_running_loop():
            raise ConfigError("AsyncFetcher must close on its owning event loop")
        self._closing = True
        current = asyncio.current_task()
        tasks = [task for task in self._active if task is not current]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _uses_cookies(self, url: httpx.URL) -> bool:
        host = url.host.lower()
        return any(host == h or host.endswith("." + h) for h in self.cookie_hosts)

    def set_clearance(self, url: str, *, cookie: str, user_agent: str) -> None:
        """回注浏览器通道取得的 cf_clearance 与一致的 User-Agent（仅内存会话）。

        cf_clearance 与 UA、出口 IP 绑定：登记后同站请求带该 Cookie 并固定 UA，
        过期被拒时由升级通道重新处置。Cookie 只进内存 Cookie 罐，不写日志/报告。
        """
        host = valid_url(url).host.lower()
        if (
            not cookie
            or not cookie.isascii()
            or any(c in cookie for c in ";, \t\r\n")
            or not user_agent
            or any(ord(c) < 32 or ord(c) > 126 for c in user_agent)
        ):
            raise FetchError("浏览器通道返回了无效的 cf_clearance 或 User-Agent")
        self.cookie_hosts.add(host)
        self._host_agents[host] = user_agent
        if self._client is not None:
            self._client.cookies.set("cf_clearance", cookie, domain=host, path="/")

    async def get(
        self,
        url: str,
        *,
        referer: str | None = None,
        headers: Mapping[str, str] | None = None,
        robots: bool = True,
    ) -> Response:
        if self._client is None or self._closing or self._loop is not asyncio.get_running_loop():
            raise ConfigError("AsyncFetcher must be used inside its owning async context")
        request_url, request_headers = valid_url(url), public_headers(referer, headers)
        if agent := self._host_agents.get(request_url.host.lower()):
            request_headers["user-agent"] = agent
        task = asyncio.current_task()
        assert task is not None
        self._active.add(task)
        started = time.monotonic()
        try:
            try:
                response = await self._fetch(
                    request_url, request_headers, policy=False, robots=robots
                )
            except BlockedError as exc:
                # Cloudflare 盾：交给升级通道（真实 Chrome 过质询并回注 cf_clearance），
                # 然后带新凭据重试一次；升级失败或重试仍被拒就如实抛出，不伪成功。
                if self.escalation is None or not exc.cloudflare:
                    raise
                await self.escalation(str(request_url))
                if agent := self._host_agents.get(request_url.host.lower()):
                    request_headers["user-agent"] = agent
                try:
                    response = await self._fetch(
                        request_url, request_headers, policy=False, robots=robots
                    )
                except BlockedError as retried:
                    if retried.cloudflare:
                        self.escalation.reject(str(request_url))
                    raise
            if self._uses_cookies(request_url):
                # JS 令牌质询(ixdzs8 等):带会话 Cookie 重访一次;仍是质询就如实返回。
                target = challenge_target(response.text, response.url, len(response.content))
                if target is not None:
                    response = await self._fetch(
                        valid_url(target),
                        {**request_headers, "referer": response.url},
                        policy=False,
                        robots=robots,
                    )
            fragment = route_fragment(url)
            return Response(
                response.url + ("#" + fragment if fragment else ""),
                response.status,
                response.headers,
                response.content,
                int((time.monotonic() - started) * 1000),
            )
        finally:
            self._active.discard(task)

    async def shuqi_request(
        self, url: str, *, method: str, headers: Mapping[str, str], body: bytes | None
    ) -> Response:
        """Preserve the isolated page's anonymous API token only on Shuqi's exact API host."""
        if body is not None and len(body) > 2 * 1024 * 1024:
            raise ConfigError("POST 请求体超过 2 MiB 限制")
        target = valid_url(url)
        if (
            target.scheme != "https"
            or target.host != "ocean.shuqireader.com"
            or method not in {"GET", "POST"}
        ):
            raise ConfigError("Invalid Shuqi API target")
        public = {
            k.lower(): v
            for k, v in headers.items()
            if k.lower() in {"authorization", "origin", "content-type", "referer", "accept"}
        }
        if any(any(ord(c) < 32 or ord(c) > 126 for c in v) for v in public.values()):
            raise ConfigError("Invalid page API headers")
        return await self._fetch(
            target,
            {**public_headers(None, None), **public},
            policy=False,
            method=method,
            content=body,
        )

    async def preflight(self, url: str, headers: Mapping[str, str]) -> Response:
        """Forward public CORS metadata only; upstream decides access, never synthesize it."""
        allowed = {"origin", "access-control-request-method", "access-control-request-headers"}
        public = {k.lower(): v for k, v in headers.items() if k.lower() in allowed}
        if any(any(ord(c) < 32 or ord(c) > 126 for c in v) for v in public.values()):
            raise ConfigError("Invalid CORS preflight headers")
        return await self._fetch(
            valid_url(url), {**public_headers(None, None), **public}, policy=False, method="OPTIONS"
        )

    async def post(
        self,
        url: str,
        *,
        body: bytes,
        content_type: str,
        referer: str | None = None,
    ) -> Response:
        # 仅供动态渲染代理页面自发的 XHR POST(如 twirp API),同样过 robots 与限额。
        if len(body) > 2 * 1024 * 1024:
            raise ConfigError("POST 请求体超过 2 MiB 限制")
        if self._client is None or self._closing or self._loop is not asyncio.get_running_loop():
            raise ConfigError("AsyncFetcher must be used inside its owning async context")
        request_url = valid_url(url)
        request_headers = {**public_headers(referer, None), "content-type": content_type}
        task = asyncio.current_task()
        assert task is not None
        self._active.add(task)
        started = time.monotonic()
        try:
            response = await self._fetch(
                request_url, request_headers, policy=False, method="POST", content=body
            )
            return Response(
                response.url,
                response.status,
                response.headers,
                response.content,
                int((time.monotonic() - started) * 1000),
            )
        finally:
            self._active.discard(task)

    async def _robots_text(self, url: str) -> str:
        return (await self._fetch(valid_url(url), public_headers(None, None), policy=True)).text

    async def _fetch(
        self,
        url: httpx.URL,
        headers: dict[str, str],
        *,
        policy: bool,
        robots: bool = True,
        method: str = "GET",
        content: bytes | None = None,
    ) -> Response:
        redirects = 0
        attempt = 0
        seen = {str(url)}
        while True:
            if not policy and robots and self.respect_robots:
                await self.robots.check(str(url))
            try:
                async with self.limiter.admit(str(url), self._slots, self.timeout) as remaining:
                    deadline = time.monotonic() + remaining
                    async with asyncio.timeout(remaining):
                        response, location, delay = await self._request(
                            url, headers, method=method, content=content
                        )
                        if time.monotonic() > deadline:
                            raise TimeoutError("Response deadline exceeded")
            except (httpx.HTTPError, TimeoutError) as exc:
                if attempt >= self.retries:
                    raise NetworkError(
                        f"{type(exc).__name__}: {redact(str(url))}", hint=CONNECTION_HINT
                    ) from None
            else:
                if response.status in _REDIRECTS:
                    if method != "GET":
                        raise NetworkError("POST 请求不允许跟随重定向")
                    target = redirect_target(url, location)
                    if url.scheme == "https" and target.scheme != "https":
                        raise BlockedError("HTTPS redirect downgrade is not allowed")
                    if str(target) in seen or redirects >= 10:
                        raise NetworkError("Redirect loop or hop limit exceeded")
                    if (url.scheme, url.host, url.port) != (
                        target.scheme,
                        target.host,
                        target.port,
                    ):
                        headers = {
                            k: v
                            for k, v in headers.items()
                            if k not in {"referer", "authorization", "origin"}
                        }
                    url = target
                    seen.add(str(url))
                    redirects += 1
                    attempt = 0
                    continue
                if 200 <= response.status < 300:
                    return response
                if response.status == 403:
                    raise blocked_error(str(url), response.headers, response.content)
                if response.status not in RETRYABLE_STATUS:
                    raise HttpStatusError(str(url), response.status)
                self.limiter.defer(str(url), delay)
                if delay > 120 or attempt >= self.retries:
                    raise HttpStatusError(str(url), response.status)
            await asyncio.sleep(min(8, 0.6 * 2**attempt) * self._rng.uniform(0.6, 1.4))
            attempt += 1

    async def _request(
        self,
        url: httpx.URL,
        headers: dict[str, str],
        *,
        method: str = "GET",
        content: bytes | None = None,
    ) -> tuple[Response, str, float]:
        assert self._client is not None
        # 默认直连 Request:绕过 Cookie 罐与默认认证头;仅登记的站点保留会话 Cookie。
        if self._uses_cookies(url):
            request = self._client.build_request(method, url, headers=headers, content=content)
        else:
            request = httpx.Request(method, url, headers=headers, content=content)
        response = await self._client.send(request, stream=True)
        try:
            location = response.headers.get("location", "")
            if response.status_code in _REDIRECTS and not location:
                raise NetworkError("Redirect response is missing Location")
            body = b""
            if 200 <= response.status_code < 300:
                body = await read_body(response, self.max_bytes)
            elif response.status_code == 403:
                body = await sniff_body(response)
            elif response.status_code in _REDIRECTS:
                redirect_target(url, location)
            result = Response(
                str(response.url),
                response.status_code,
                MappingProxyType(dict(response.headers)),
                body,
                0,
            )
            delay = retry_after(response.headers.get("retry-after", ""), time.time())
            return result, location, delay
        finally:
            await response.aclose()
            if not self._uses_cookies(url):
                self._client.cookies.clear()
