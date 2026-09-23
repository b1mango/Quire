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
    ("全本小说网", "quanben.io", "小说", "完结全本中文小说,库存大"),
    ("无限小说", "8book.com", "小说", "繁体中文全本小说,需浏览器渲染"),
    ("The Paper Books", "thepaperbooks.com", "小说", "英文小说"),
    ("咚漫", "dongmanmanhua.cn", "漫画", "国产条漫与韩漫中译,更新快"),
    ("B站漫画", "manga.bilibili.com", "漫画", "哔哩哔哩正版漫画,日漫国漫都有"),
    ("腾讯动漫", "ac.qq.com", "漫画", "国漫正版平台,库存全"),
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
