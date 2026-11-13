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


#: 「点击完成验证」类 Cookie 门:页面用 JS 写一个固定 Cookie 后刷新(kalamanhua 等小站常见),
#: 形如 ``k="…"; document.cookie = "omo_token=" + k`` 或直接写字面量。
_COOKIE_SET = re.compile(
    r"""document\.cookie\s*=\s*["']([A-Za-z0-9_.-]{1,64})=(?:["']\s*\+\s*([A-Za-z_$][\w$]{0,31})"""
    r"""|([A-Za-z0-9+/=:_.-]{4,256})[;"'])"""
)
_RELOAD = re.compile(r"location\.(?:reload|href|replace|assign)|location\s*=", re.I)
_COOKIE_VALUE = re.compile(r"^[A-Za-z0-9+/=:_.-]{4,256}$")


def cookie_gate(text: str, content_length: int) -> tuple[str, str] | None:
    """若页面是「写 Cookie 再刷新」的验证门,返回 (Cookie 名, 值);否则 None。"""
    if content_length > MAX_CHALLENGE_BYTES or not _RELOAD.search(text):
        return None
    found = _COOKIE_SET.search(text)
    if found is None:
        return None
    name, var, literal = found.groups()
    if name.lower().startswith(("cf_", "__cf")):
        return None  # Cloudflare 凭据只能由其服务端签发,交给真实 Chrome 升级通道
    if literal:
        return name, literal
    assigned = re.search(rf"""(?<![\w$.]){re.escape(var)}\s*=\s*["']([^"'\\]{{4,256}})["']""", text)
    if assigned is None or not _COOKIE_VALUE.match(assigned[1]):
        return None
    return name, assigned[1]
