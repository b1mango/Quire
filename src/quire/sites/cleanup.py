"""已知站点的页面清理:抽取正文前移除模板噪声容器。

通用正文评分按"文本量 × 链接密度"选容器;有的站(thepaperbooks 等)
把相关文章链接列表塞进正文栏,链接占比越线导致整页被拒。这里按站点
删掉已确认的噪声块,让通用逻辑看到干净的页面。只收实测确认的站点与
选择器,删错了会让正文缺失——所以每处登记都要有真实页面依据。
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlsplit

from ..parse.minidom import Document
from . import eightbook

#: 域名后缀 → 要整棵移除的噪声容器选择器。
_JUNK: dict[str, tuple[str, ...]] = {
    # thepaperbooks.com:正文栏混入"大家也想知道/口碑排行榜"链接列表与标签云,
    # 链接密度 0.29 超过正文阈值 0.25,实测移除后正文正常抽取(2026-09-22)。
    "thepaperbooks.com": (
        ".entry-main-content",
        ".post-block-list",
        ".tags",
        ".entry-meta",
        "#div-onead-draft",
    ),
}


#: 域名后缀 → 需要文本判定的站点级清理函数(选择器删不掉的噪声,如
#: 与正文共用类名的同形异符水印)。
_JUNK_FN: dict[str, Callable[[Document], None]] = {
    # 8book.com 及其正文伪装域:阅读页导航块 + "8book.com" 同形异符水印
    # (实测 2026-09-22:水印每次字符组合不同,只能按文本形态识别)。
    "8book.com": eightbook.clean_document,
    "binaccount.com": eightbook.clean_document,
}


def clean_document(doc: Document, url: str) -> None:
    """移除命中站点的已知噪声容器;未登记的站点不动。"""
    host = (urlsplit(url).hostname or "").lower()
    selectors = next(
        (s for suffix, s in _JUNK.items() if host == suffix or host.endswith("." + suffix)),
        (),
    )
    for selector in selectors:
        for node in doc.select(selector):
            if node.parent is not None:
                node.parent.children.remove(node)
    clean_fn = next(
        (fn for suffix, fn in _JUNK_FN.items() if host == suffix or host.endswith("." + suffix)),
        None,
    )
    if clean_fn is not None:
        clean_fn(doc)
