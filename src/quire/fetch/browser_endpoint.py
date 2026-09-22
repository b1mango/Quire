"""Validate local browser debugging addresses without DNS or proxy indirection."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

import httpx

from ..errors import ConfigError, NetworkError


def validate_endpoint(value: str) -> str:
    try:
        if not isinstance(value, str) or any(c.isspace() for c in value):
            raise ValueError
        parts = urlsplit(value)
        host = parts.hostname or ""
        if host != "localhost" and not ipaddress.ip_address(host).is_loopback:
            raise ValueError
        if (
            parts.scheme not in {"http", "ws"}
            or not parts.port
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            raise ValueError
        if parts.scheme == "http" and parts.path not in {"", "/"}:
            raise ValueError
        if parts.scheme == "ws" and not re.fullmatch(
            r"/devtools/browser/[A-Za-z0-9_-]+", parts.path
        ):
            raise ValueError
        host = "127.0.0.1" if host == "localhost" else host
        authority = f"[{host}]:{parts.port}" if ":" in host else f"{host}:{parts.port}"
        return urlunsplit((parts.scheme, authority, parts.path, "", ""))
    except (ValueError, TypeError):
        raise ConfigError("CDP 仅支持回环 HTTP 端口或浏览器级 WebSocket 地址（无凭据）") from None


async def resolve_endpoint(value: str) -> str:
    endpoint = validate_endpoint(value)
    if endpoint.startswith("ws:"):
        return endpoint
    try:
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=5) as client:
            async with client.stream("GET", endpoint.rstrip("/") + "/json/version") as response:
                if response.status_code != 200:
                    raise ValueError
                body = bytearray()
                async for block in response.aiter_bytes():
                    body.extend(block)
                    if len(body) > 65536:
                        raise ValueError
                import json

                data = json.loads(body)
                resolved = validate_endpoint(data["webSocketDebuggerUrl"])
                if (
                    not resolved.startswith("ws:")
                    or urlsplit(resolved).netloc != urlsplit(endpoint).netloc
                ):
                    raise ValueError
                return resolved
    except (httpx.HTTPError, ValueError, KeyError, TypeError, ConfigError):
        raise NetworkError("无法发现本机 Chrome CDP；请启用远程调试并检查端口") from None
