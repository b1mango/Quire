"""通用 JS 质询识别:页面让浏览器带令牌自跳转(ixdzs8 等 PHP 小站常见)。

形态:极小的页面,内嵌 ``token = "…"; window.location.href =
location.pathname + "?challenge=" + token``。浏览器执行后带 Cookie
重访即获真实内容。这里只做**识别与目标 URL 重建**,执行(带 Cookie
重访)在 fetch/session 里完成——识别不出就返回 None,按普通页面处理。
"""

from __future__ import annotations

import re
from urllib.parse import quote, urlsplit, urlunsplit

#: 质询页特征:自跳转回当前路径并带上 challenge 参数。
_REDIRECT = re.compile(r"location\.pathname\s*\+\s*[\"']\?challenge=", re.I)
_TOKEN = re.compile(r"""token\s*=\s*["']([A-Za-z0-9+/=:_-]{16,256})["']""")

#: 质询页都很小;超过这个体积不按质询处理,避免误判正常页面。
MAX_CHALLENGE_BYTES = 16 * 1024


def challenge_target(text: str, url: str, content_length: int) -> str | None:
    """若页面是 JS 令牌质询,返回应重访的 URL;否则 None。"""
    if content_length > MAX_CHALLENGE_BYTES or not _REDIRECT.search(text):
        return None
    token = _TOKEN.search(text)
    if token is None:
        return None
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, f"challenge={quote(token[1])}", ""))
