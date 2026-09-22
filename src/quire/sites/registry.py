"""已知需要 Cookie 会话的站点(用于 JS 质询应答),只登记实测确认的。

默认请求不带 Cookie(隐私与可复现);列入这里的站点会在会话内保留
该站自己的 Cookie,用于通过站点的浏览器质询。Cookie 只来自目标站本身,
不读取浏览器、不跨站携带,进程结束即销毁。
"""

from __future__ import annotations

from urllib.parse import urlsplit

#: ixdzs8.com(爱下电子书):PHPSESSID + ?challenge= 令牌质询,无会话不过。
_COOKIE_HOSTS = frozenset({"ixdzs8.com"})


def cookie_hosts_for(url: str) -> frozenset[str]:
    """入口 URL 命中的需 Cookie 站点(按域名后缀匹配,含子域)。"""
    host = (urlsplit(url).hostname or "").lower()
    return frozenset(h for h in _COOKIE_HOSTS if host == h or host.endswith("." + h))
