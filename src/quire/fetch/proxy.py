"""跟随 macOS 系统代理（Clash、Surge 等「设置为系统代理」时写入的 HTTP/HTTPS 代理）。

本机 DNS 被污染或站点需要代理才能连通时，浏览器能打开而直连会失败；这里与浏览器一致，
读系统设置里的 HTTP(S) 代理交给 httpx。回环地址始终直连。环境变量
``QUIRE_SYSTEM_PROXY=0`` 可关闭。只认 http:// 代理：SOCKS 需额外依赖，不在此支持。

代理规则并不总对：有的站经代理被重置或封境外出口（dquanben、xiaoshuopu 实测），有的站
只有经代理才通（本机 DNS 把 kalamanhua 解析成 0.0.0.0）。所以「自动」线路按主机选路：
先走该主机记住的线路（默认代理），连不上或被拒而另一条可用时改走另一条并记住；记住的线路
存到数据目录、30 天过期，下次启动不必再试错。设置里可锁定「始终代理 / 始终直连」。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Literal

import httpx

_LOG = logging.getLogger(__name__)
_DIRECT = ("all://127.0.0.1", "all://localhost", "all://[::1]")
ROUTE_MODES = ("auto", "proxy", "direct")
Route = Literal["proxy", "direct"]
_TTL = 30 * 24 * 3600
_MAX_HOSTS = 2000


def system_proxy() -> str | None:
    if os.environ.get("QUIRE_SYSTEM_PROXY", "1") == "0" or sys.platform != "darwin":
        return None
    reader = getattr(urllib.request, "getproxies_macosx_sysconf", None)
    if reader is None:
        return None
    conf = reader()
    url = conf.get("https") or conf.get("http") or ""
    return url if url.startswith("http://") else None


class RouteBook:
    """线路模式与按主机记住的线路（进程内共享，探查学到的线路下载时直接用）。"""

    def __init__(self) -> None:
        self.mode = "auto"
        self.path: Path | None = None
        self.routes: dict[str, tuple[Route, float]] = {}
        self._lock = threading.Lock()

    def configure(self, mode: str, path: Path | None = None) -> None:
        with self._lock:
            self.mode = mode if mode in ROUTE_MODES else "auto"
            if path is not None and path != self.path:
                self.path, self.routes = path, self._load(path)

    @staticmethod
    def _load(path: Path) -> dict[str, tuple[Route, float]]:
        try:
            raw = json.loads(path.read_text("utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            _LOG.warning("线路记录无法读取，重新学习：%s", path)
            return {}
        now = time.time()
        routes: dict[str, tuple[Route, float]] = {}
        if isinstance(raw, dict):
            for host, entry in raw.items():
                if (
                    isinstance(entry, list)
                    and len(entry) == 2
                    and entry[0] in ("proxy", "direct")
                    and isinstance(entry[1], int | float)
                    and now - entry[1] < _TTL
                ):
                    routes[str(host)] = (entry[0], float(entry[1]))
        return routes

    def preferred(self, host: str) -> Route:
        if self.mode == "direct":
            return "direct"
        if self.mode == "proxy":
            return "proxy"
        entry = self.routes.get(host)
        return entry[0] if entry else "proxy"

    def learn(self, host: str, route: Route) -> None:
        with self._lock:
            if host in self.routes and self.routes[host][0] == route:
                return
            self.routes[host] = (route, time.time())
            if len(self.routes) > _MAX_HOSTS:
                oldest = sorted(self.routes, key=lambda h: self.routes[h][1])
                for stale in oldest[: len(self.routes) - _MAX_HOSTS]:
                    del self.routes[stale]
            if self.path is None:
                return
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temp = self.path.with_suffix(".tmp")
                temp.write_text(json.dumps({h: list(v) for h, v in self.routes.items()}), "utf-8")
                temp.replace(self.path)
            except OSError as exc:
                _LOG.warning("线路记录写入失败（只在本次运行生效）：%s", exc)


ROUTES = RouteBook()


def proxy_mounts(
    proxy: str | None, limits: httpx.Limits, book: RouteBook = ROUTES
) -> dict[str, httpx.AsyncBaseTransport | None] | None:
    """httpx mounts：回环直连，其余按线路选；没有系统代理或锁定直连时返回 None（直连）。"""
    if not proxy or book.mode == "direct":
        return None
    mounts: dict[str, httpx.AsyncBaseTransport | None] = {
        "all://": FallbackTransport(
            httpx.AsyncHTTPTransport(proxy=proxy, limits=limits),
            httpx.AsyncHTTPTransport(limits=limits),
            book,
        )
    }
    mounts.update(dict.fromkeys(_DIRECT))
    return mounts


#: 这一跳失败（尚未拿到任何响应）的错误：改走另一条线路重试是安全的。
_ROUTE_ERRORS = (
    httpx.ProxyError,
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.RemoteProtocolError,
    httpx.ReadError,
)
#: 国内站常封境外出口 IP（经代理 403/405，直连 200）；反过来也可能。
_GEO_BLOCKED = frozenset({403, 405, 451})


class FallbackTransport(httpx.AsyncBaseTransport):
    """按 RouteBook 选线路；自动模式下一条线路失败而另一条可用时改线并记住。"""

    def __init__(
        self,
        proxied: httpx.AsyncBaseTransport,
        direct: httpx.AsyncBaseTransport,
        book: RouteBook | None = None,
    ) -> None:
        self.proxied, self.direct = proxied, direct
        self.book = book if book is not None else RouteBook()

    def _via(self, route: Route) -> httpx.AsyncBaseTransport:
        return self.proxied if route == "proxy" else self.direct

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host.lower()
        first = self.book.preferred(host)
        if self.book.mode != "auto":
            return await self._via(first).handle_async_request(request)
        second: Route = "direct" if first == "proxy" else "proxy"
        try:
            response = await self._via(first).handle_async_request(request)
        except _ROUTE_ERRORS:
            response = await self._via(second).handle_async_request(request)
            self.book.learn(host, second)
            return response
        if response.status_code not in _GEO_BLOCKED:
            return response
        # 被拒：另一条线路试一次，成功才改线；也不行就如实返回原线路的结果
        try:
            other = await self._via(second).handle_async_request(request)
        except _ROUTE_ERRORS:
            return response
        if other.status_code < 400:
            await response.aclose()
            self.book.learn(host, second)
            return other
        await other.aclose()
        return response

    async def aclose(self) -> None:
        await self.proxied.aclose()
        await self.direct.aclose()
