"""通用「查看全部章节」跟随。

许多小说/漫画站的书页只列最新十来章，完整目录在「查看全部章节」「完整目录」「章节目录」
链接后面（笔趣阁类镜像普遍如此）。书页上有这种链接时取回目标页，章节更多才替换；
阅读页（有「下一章」）不处理，取回失败或章节没变多都原样返回。
"""

from __future__ import annotations

import logging
import re
from typing import Protocol
from urllib.parse import urlsplit

from ..errors import FetchError
from ..fetch.simple import Response
from ..parse.chapters import classify_next, discover_chapters
from ..parse.minidom import parse as parse_html
from ..utils.urls import is_usable_url
from ..utils.urls import join_document_url as join_url

_LOG = logging.getLogger(__name__)

_FULL = re.compile(
    r"^(?:查看|显示|展开|进入|点击查看|阅读)?(?:全部|完整|所有|更多)?(?:章节|目录)(?:目录|列表)?"
    r"(?:\s*[>»›]+)?$|^(?:查看)?(?:全部|完整|更多)章节|^全文目录$|^章节列表$"
)


class _Client(Protocol):
    async def get(
        self, url: str, *, referer: str | None = None, headers: dict[str, str] | None = None
    ) -> Response: ...


#: 明确指向「整本目录」的说法；单写「目录」「章节目录」的在阅读页上也常见（回书页），算弱证据
_STRONG = re.compile(r"(?:查看|显示|展开)?(?:全部|完整|所有|更多)(?:章节|目录)|章节列表|全文目录")


def full_links(page: Response) -> list[str]:
    """书页上指向完整目录的同源链接（去掉指回本页的）；阅读页返回空。"""
    doc = parse_html(page.text, base_url=page.url)
    here = page.url.split("#", 1)[0]
    found: list[str] = []
    for node in doc.iter_elements():
        if node.tag != "a" or not node.get("href"):
            continue
        text = re.sub(r"\s+", "", node.text)
        if classify_next(text) == "chapter":
            return []  # 阅读页：「目录」链接是回书页，不跟
        if not _FULL.search(text):
            continue
        url = join_url(page.url, node.get("href") or "")
        if (
            url
            and is_usable_url(url)
            and urlsplit(url).netloc == urlsplit(page.url).netloc
            and url != here
        ):
            if url not in found:
                found.append(url)
    return found[:2]


async def expand_listing(client: _Client, page: Response) -> Response:
    targets = full_links(page)
    if not targets:
        return page
    best = page
    count = len(discover_chapters(parse_html(page.text, base_url=page.url), page.url, limit=20001))
    if count < 2 and not _strong(page):
        return page  # 本页不像书页（单章页的「目录」链接是回书页，不能把单章任务换成整本）
    for target in targets:
        try:
            other = await client.get(target, referer=page.url)
        except FetchError as exc:
            _LOG.warning("完整目录页读取失败，按当前页识别：%s", exc)
            continue
        links = discover_chapters(
            parse_html(other.text, base_url=other.url), other.url, limit=20001
        )
        if len(links) > count:
            best, count = other, len(links)
    return best


def _strong(page: Response) -> bool:
    doc = parse_html(page.text, base_url=page.url)
    return any(
        node.tag == "a" and _STRONG.search(re.sub(r"\s+", "", node.text))
        for node in doc.iter_elements()
    )
