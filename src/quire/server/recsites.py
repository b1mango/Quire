"""推荐站点:单一数据源(名称/地址/类型/简介) + 在系统浏览器打开站点。

站点清单以本项目真实抓取记录为准(项目进度.md 站点可达性),简介描述
资源特点,不写测试流水账。开 URL 走默认浏览器;host 必须在清单内,
本接口不是任意 URL 打开器。
"""

from __future__ import annotations

import subprocess

from ..errors import ConfigError
from ..store.models import JsonValue

#: (名称, 域名, 类型, 资源特点)
RECOMMENDED_SITES: tuple[tuple[str, str, str, str], ...] = (
    # 2026-11-12 实测：用 Quire 真实流程抓前 2 章并回读成品通过；无需浏览器渲染
    ("全本小说网", "quanben.io", "小说", "完结全本多，目录完整，抓取最稳"),
    ("读全本", "dquanben.com", "小说", "网文全本，章节齐全"),
    ("新笔趣阁", "hafuklxt.cc", "小说", "笔趣阁镜像，连载网文更新快；镜像域名可能变动"),
    ("无限小说", "8book.com", "小说", "繁体中文全本，需浏览器渲染"),
    ("咚漫", "dongmanmanhua.cn", "漫画", "国产条漫与韩漫中译，目录页与单话页都可抓"),
)

_SITE_URLS: dict[str, str] = {host: f"https://{host}/" for _, host, _, _ in RECOMMENDED_SITES}


def list_sites() -> dict[str, JsonValue]:
    return {
        "sites": [
            {"name": name, "host": host, "kind": kind, "note": note}
            for name, host, kind, note in RECOMMENDED_SITES
        ]
    }


def _open_url(url: str) -> None:
    subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def open_site(body: object) -> dict[str, JsonValue]:
    host = body.get("host") if isinstance(body, dict) else None
    if not isinstance(host, str):
        raise ConfigError("缺少站点域名")
    url = _SITE_URLS.get(host)
    if url is None:
        raise ConfigError("不在推荐站点清单内")
    _open_url(url)
    return {"opened": url}
