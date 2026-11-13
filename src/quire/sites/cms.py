"""常见漫画建站程序的章节接口（参照 Mihon multisrc：一个模板覆盖成百上千个站）。

* Madara（WordPress 漫画主题，海外漫画站最常见）：书页常不内嵌章节，靠
  ``POST <书页>/ajax/chapters/`` 取；旧版走 ``POST /wp-admin/admin-ajax.php``
  ``action=manga_get_chapters&manga=<id>``。章节行 ``li.wp-manga-chapter > a``，最新在前。
* 漫城 CMS（MCCMS，国内漫画站常用）：``GET /index.php/api/comic/chapter?mid=<作品号>``
  一次返回全部章节 JSON（去掉 index.php 的伪静态站走 ``/api/comic/chapter``）。

只在识别出对应程序、且接口给出的章节比页面多时替换；任何一步失败都按原页面识别。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Protocol
from urllib.parse import urljoin, urlsplit

from ..errors import FetchError
from ..fetch.simple import Response
from ..parse.chapters import discover_chapters
from ..parse.minidom import Document
from ..parse.minidom import parse as parse_html
from .catalogue_pages import rebuild

_LOG = logging.getLogger(__name__)
_MCCMS_ID = re.compile(r"get_(?:comment|Score|Collect)\((\d+)\)|isCollect\((\d+)\)|[?&]mid=(\d+)")


class _Client(Protocol):
    async def get(
        self, url: str, *, referer: str | None = None, headers: dict[str, str] | None = None
    ) -> Response: ...

    async def post(
        self, url: str, *, body: bytes, content_type: str, referer: str | None = None
    ) -> Response: ...


def _count(doc: Document, url: str) -> int:
    return len(discover_chapters(doc, url, limit=20001))


def madara_chapters(html: str, base: str) -> list[tuple[str, str]]:
    """Madara 章节片段 → 阅读顺序（接口与页面都是最新在前，这里倒过来）。"""
    found: list[tuple[str, str]] = []
    for row in parse_html(html).select("li.wp-manga-chapter"):
        link = row.select_one("a")
        href = (link.get("href") or "").strip() if link is not None else ""
        if link is not None and href:
            title = (
                re.sub(r"\s+", " ", link.text).strip()[:60] or href.rstrip("/").rsplit("/", 1)[-1]
            )
            found.append((urljoin(base, href), title))
    return list({url: (url, title) for url, title in reversed(found)}.values())


async def expand_madara(client: _Client, page: Response) -> Response:
    doc = parse_html(page.text, base_url=page.url)
    holder = doc.select_one("[id^=manga-chapters-holder]")
    if holder is None and "wp-manga" not in page.text:
        return page
    links = madara_chapters(page.text, page.url)
    if len(links) >= 2 and _count(doc, page.url) >= len(links):
        return page  # 章节已内嵌且通用识别能读全
    origin = f"{urlsplit(page.url).scheme}://{urlsplit(page.url).netloc}"
    manga_id = (holder.get("data-id") if holder is not None else "") or ""
    if not manga_id:
        found = re.search(r'"manga_id"\s*:\s*"?(\d+)', page.text)
        manga_id = found[1] if found else ""
    attempts = [(page.url.split("?")[0].rstrip("/") + "/ajax/chapters/?t=1", b"t=1")]
    if manga_id:
        attempts.append(
            (
                f"{origin}/wp-admin/admin-ajax.php",
                f"action=manga_get_chapters&manga={manga_id}".encode(),
            )
        )
    for url, body in attempts:
        try:
            response = await client.post(
                url, body=body, content_type="application/x-www-form-urlencoded", referer=page.url
            )
        except FetchError as exc:
            _LOG.info("Madara 章节接口不可用，换下一种：%s", exc)
            continue
        fetched = madara_chapters(response.text, page.url)
        if len(fetched) > max(len(links), 1):
            return rebuild(page, doc, fetched, trusted=True)
    return rebuild(page, doc, links, trusted=True) if len(links) >= 2 else page


async def expand_mccms(client: _Client, page: Response) -> Response:
    if "/packs/mccms/" not in page.text and "Mccms" not in page.text:
        return page
    found = _MCCMS_ID.search(page.text)
    if found is None:
        return page
    mid = next(g for g in found.groups() if g)
    origin = f"{urlsplit(page.url).scheme}://{urlsplit(page.url).netloc}"
    prefix = "/index.php" if "/index.php/" in page.url else ""
    doc = parse_html(page.text, base_url=page.url)
    for api in dict.fromkeys(
        (
            f"{origin}{prefix}/api/comic/chapter?mid={mid}",
            f"{origin}/index.php/api/comic/chapter?mid={mid}",
        )
    ):
        try:
            data = json.loads((await client.get(api, referer=page.url)).text)
        except (FetchError, ValueError) as exc:
            _LOG.info("漫城 CMS 章节接口不可用：%s", exc)
            continue
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            continue
        links = [
            (urljoin(page.url, str(row["link"])), str(row.get("name") or "")[:60] or f"第{n}话")
            for n, row in enumerate(rows, 1)
            if isinstance(row, dict) and row.get("link")
        ]
        if len(links) > _count(doc, page.url):
            return rebuild(page, doc, links, trusted=True)
        return page
    return page
