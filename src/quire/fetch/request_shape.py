"""请求成形：URL 校验、公开请求头构造、重定向拼接（供 session 使用）。"""

from __future__ import annotations

from collections.abc import Mapping

import httpx

from ..errors import ConfigError, NetworkError
from ..utils.urls import is_usable_url
from .simple import DEFAULT_ACCEPT, DEFAULT_LANGUAGE, DEFAULT_UA

_PUBLIC_HEADERS = {"accept", "accept-language", "referer"}


def valid_url(value: str) -> httpx.URL:
    if not is_usable_url(value) or any(ord(c) < 32 for c in value):
        raise ConfigError("URL must be an absolute HTTP(S) address without credentials")
    try:
        url = httpx.URL(value)
        if url.scheme not in {"http", "https"} or not url.host or url.userinfo:
            raise ValueError
        return url.copy_with(fragment=None)
    except (httpx.InvalidURL, ValueError):
        raise ConfigError("URL must be an absolute HTTP(S) address without credentials") from None


def public_headers(referer: str | None, custom: Mapping[str, str] | None) -> dict[str, str]:
    result = {
        "user-agent": DEFAULT_UA,
        "accept": DEFAULT_ACCEPT,
        "accept-language": DEFAULT_LANGUAGE,
        "accept-encoding": "gzip, deflate",
    }
    for name, value in (custom or {}).items():
        key = name.lower()
        if key not in _PUBLIC_HEADERS or any(ord(c) < 32 or ord(c) > 126 for c in value):
            raise ConfigError("Only public ASCII collection headers are supported")
        result[key] = value
    if referer is not None:
        result["referer"] = str(valid_url(referer))
    if result.get("referer"):
        result["referer"] = str(valid_url(result["referer"]))
    return result


def redirect_target(url: httpx.URL, location: str) -> httpx.URL:
    try:
        return valid_url(str(url.join(location)))
    except httpx.InvalidURL:
        raise NetworkError("Invalid redirect Location") from None
