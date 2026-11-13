"""跟随 macOS 系统代理（Clash、Surge 等「设置为系统代理」时写入的 HTTP/HTTPS 代理）。

本机 DNS 被污染或站点需要代理才能连通时，浏览器能打开而直连会失败；这里与浏览器一致，
读系统设置里的 HTTP(S) 代理交给 httpx。回环地址始终直连。环境变量
``QUIRE_SYSTEM_PROXY=0`` 可关闭。只认 http:// 代理：SOCKS 需额外依赖，不在此支持。

代理规则并不总对：有的站经代理被重置（dquanben 实测），有的站只有经代理才通（本机 DNS
把 kalamanhua 解析成 0.0.0.0）。所以按主机记路线：先走代理，连不上就改直连并记住。
"""

from __future__ import annotations

import os
import sys
import urllib.request

import httpx

_DIRECT = ("all://127.0.0.1", "all://localhost", "all://[::1]")


def system_proxy() -> str | None:
    if os.environ.get("QUIRE_SYSTEM_PROXY", "1") == "0" or sys.platform != "darwin":
        return None
    reader = getattr(urllib.request, "getproxies_macosx_sysconf", None)
    if reader is None:
        return None
    conf = reader()
    url = conf.get("https") or conf.get("http") or ""
    return url if url.startswith("http://") else None


def proxy_mounts(
    proxy: str | None, limits: httpx.Limits
) -> dict[str, httpx.AsyncBaseTransport | None] | None:
    """httpx mounts：除回环外全部经代理；没有系统代理时返回 None（直连）。"""
    if not proxy:
        return None
    mounts: dict[str, httpx.AsyncBaseTransport | None] = {
        "all://": FallbackTransport(
            httpx.AsyncHTTPTransport(proxy=proxy, limits=limits),
            httpx.AsyncHTTPTransport(limits=limits),
        )
    }
    mounts.update(dict.fromkeys(_DIRECT))
    return mounts


#: 代理这一跳失败（尚未拿到任何响应）的错误：改走直连重试是安全的。
_ROUTE_ERRORS = (
    httpx.ProxyError,
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.RemoteProtocolError,
    httpx.ReadError,
)


_GEO_BLOCKED = frozenset({403, 405, 451})


class FallbackTransport(httpx.AsyncBaseTransport):
    """先经代理；某主机经代理连不上或被拒（疑似封境外 IP）而直连可用时，记住该主机走直连。"""

    def __init__(self, proxied: httpx.AsyncBaseTransport, direct: httpx.AsyncBaseTransport):
        self.proxied, self.direct = proxied, direct
        self.direct_hosts: set[str] = set()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host.lower()
        if host in self.direct_hosts:
            return await self.direct.handle_async_request(request)
        try:
            response = await self.proxied.handle_async_request(request)
        except _ROUTE_ERRORS:
            response = await self.direct.handle_async_request(request)
            self.direct_hosts.add(host)
            return response
        if response.status_code not in _GEO_BLOCKED:
            return response
        # 国内站常封境外出口 IP（经代理 403/405，直连 200，实测 xiaoshuopu、69shu）：
        # 直连试一次，成功才改走直连；直连也不行就如实返回代理的结果
        try:
            direct = await self.direct.handle_async_request(request)
        except _ROUTE_ERRORS:
            return response
        if direct.status_code < 400:
            await response.aclose()
            self.direct_hosts.add(host)
            return direct
        await direct.aclose()
        return response

    async def aclose(self) -> None:
        await self.proxied.aclose()
        await self.direct.aclose()
