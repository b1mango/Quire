"""模块误放检测:已知站点 URL 形态先验,probe/提交阶段就提示放错模块。

只收录内容形态明确单一的站点;未命中的 URL 交给通用识别,不干预。
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from ..errors import ConfigError

#: 已知漫画站:(host 后缀, 需匹配的路径形态;None 表示整站都是漫画)。
_MANGA_HOSTS: tuple[tuple[str, re.Pattern[str] | None], ...] = (
    ("ac.qq.com", re.compile(r"^/Comic(View)?/")),  # 腾讯动漫的目录与阅读页
    ("manhuagui.com", None),  # 看漫画
    ("mycomic.com", None),
    ("dongmanmanhua.cn", None),
    ("manga.bilibili.com", None),
)

#: 已知小说站:整站都是小说。
_NOVEL_HOSTS = ("quanben.io", "hetushu.com", "t.shuqi.com", "qidian.com", "fanqienovel.com")


def expected_kind(url: str) -> str | None:
    """已知站点的内容形态;不认识或形态不限的站点返回 None。"""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    for suffix, path_pattern in _MANGA_HOSTS:
        if host == suffix or host.endswith("." + suffix):
            if path_pattern is None or path_pattern.match(parts.path):
                return "manga"
    for suffix in _NOVEL_HOSTS:
        if host == suffix or host.endswith("." + suffix):
            return "novel"
    return None


def check_placement(url: str, kind: str | None) -> None:
    """URL 形态与所选模块明显冲突时明确报错;kind 为 None(自动识别)时不干预。"""
    expected = expected_kind(url)
    if expected is None or kind is None or expected == kind:
        return
    if expected == "manga":
        raise ConfigError(
            "这是漫画链接,小说模块解析不出正文",
            hint="请切换到「漫画」标签,用目录或单章模式提交。",
        )
    raise ConfigError(
        "这是小说链接,漫画模块解析不出图片",
        hint="请切换到「小说」标签,用目录或单章模式提交。",
    )
