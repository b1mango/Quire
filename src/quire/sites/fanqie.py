"""fanqienovel.com(番茄小说网页版)目录适配。

书籍页 ``/page/<id>`` 的完整章节列表不在 HTML 锚点里,而是内嵌在
``window.__INITIAL_STATE__`` 的 ``chapterListWithVolume``(按卷分组的
两层数组,每项含 itemId/title/needPay/isChapterLock;静态 HTML 实测只带
前两条锚点)。这里把全部章节重建为 ``/reader/<itemId>`` 锚点列表并入
页面,交给通用目录识别;JSON 缺失或结构不符时原样返回,不因此终止任务。

付费/锁定章节的正文登录态以外拿不到,目录仍按页面实际列出的为准。
正文的 PUA 字体混淆由 parse/fontmap 在章节抓取阶段还原,与本模块无关。
"""

from __future__ import annotations

import json
import re
from html import escape
from urllib.parse import urlsplit

from ..fetch.simple import Response

_HOSTS = frozenset({"fanqienovel.com", "www.fanqienovel.com"})
_KEY = '"chapterListWithVolume":'
_STATE = "window.__INITIAL_STATE__"


def is_book_page(url: str) -> bool:
    parts = urlsplit(url)
    path = parts.path.strip("/")
    return (
        (parts.hostname or "").lower() in _HOSTS
        and path.startswith("page/")
        and path.removeprefix("page/").isdigit()
    )


def _catalogue_entries(html: str) -> list[tuple[str, str]] | None:
    """内嵌 JSON 里的 (itemId, 标题) 列表,按页面给出的卷内顺序。"""
    if _STATE not in html:
        return None
    index = html.find(_KEY)
    if index < 0:
        return None
    try:
        volumes, _ = json.JSONDecoder().raw_decode(html[index + len(_KEY) :])
    except ValueError:
        return None
    if not isinstance(volumes, list):
        return None
    entries: list[tuple[str, str]] = []
    for volume in volumes:
        if not isinstance(volume, list):
            return None
        for item in volume:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("itemId"), str)
                or not item["itemId"].isdigit()
                or not isinstance(item.get("title"), str)
            ):
                return None
            entries.append((item["itemId"], item["title"]))
    return entries or None


async def expand_listing(client: object, page: Response) -> Response:
    """番茄书籍页并入 __INITIAL_STATE__ 里的全量目录;非目标页原样返回。

    签名与 quanben.expand_listing 保持一致(编排层统一调用);
    目录数据已在页面内,不需要额外请求,client 仅为协议对称保留。
    """
    del client
    if not is_book_page(page.url):
        return page
    entries = _catalogue_entries(page.text)
    if not entries:
        return page
    items = "".join(
        f'<li><a href="/reader/{escape(item_id)}"><span>{escape(title)}</span></a></li>'
        for item_id, title in entries
    )
    block = f'<ul class="chapter-list">{items}</ul>'
    html = page.text
    # 页面自带的零星章节锚点标题带「最近更新：」等前缀,而目录识别按 URL 去重
    # 保留先出现者——所以把重建的完整列表插到 <body> 最前面,让干净标题胜出。
    body = re.search(r"<body[^>]*>", html)
    if body:
        merged = html[: body.end()] + block + html[body.end() :]
    else:
        merged = block + html
    headers = {**page.headers, "content-type": "text/html; charset=utf-8"}
    return Response(page.url, page.status, headers, merged.encode("utf-8"), page.elapsed_ms)
