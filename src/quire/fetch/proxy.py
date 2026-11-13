"""跟随 macOS 系统代理（Clash、Surge 等「设置为系统代理」时写入的 HTTP/HTTPS 代理）。

本机 DNS 被污染或站点需要代理才能连通时，浏览器能打开而直连会失败；这里与浏览器一致，
读系统设置里的 HTTP(S) 代理交给 httpx。回环地址始终直连。环境变量
``QUIRE_SYSTEM_PROXY=0`` 可关闭。只认 http:// 代理：SOCKS 需额外依赖，不在此支持。
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
        "all://": httpx.AsyncHTTPTransport(proxy=proxy, limits=limits)
    }
    mounts.update(dict.fromkeys(_DIRECT))
    return mounts
