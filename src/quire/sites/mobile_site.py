"""桌面站与移动站互相回退。

有的书站桌面版目录靠脚本加载或被截断，而移动版（m./wap. 子域、移动端 UA）是静态整页、
结构更统一；也有反过来的。当前页认不出目录（不足 2 章）且不是阅读页时，依次试：
页面自己声明的移动/桌面版地址（``<meta http-equiv="mobile-agent">``、
``<link rel="alternate" media="…screen…">``、``<link rel="canonical">``），再试 www ↔ m. 互换。
另一版章节更多才采用；取回失败按当前页识别。之后的分页合并照常作用于采用的那一版。
"""

from __future__ import annotations

import logging
import re
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

from ..errors import FetchError
from ..fetch.simple import Response
from ..parse.chapters import classify_next, discover_chapters
from ..parse.minidom import Document
from ..parse.minidom import parse as parse_html
from ..utils.urls import is_usable_url
from ..utils.urls import join_document_url as join_url

_LOG = logging.getLogger(__name__)
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)
_AGENT_URL = re.compile(r"url=(\S+)", re.I)


class _Client(Protocol):
    async def get(
        self, url: str, *, referer: str | None = None, headers: dict[str, str] | None = None
    ) -> Response: ...


def _site(host: str) -> str:
    return ".".join(host.lower().split(".")[-2:])


def alternates(doc: Document, page_url: str) -> list[str]:
    """另一版本的候选地址（同一站点，不同主机名），声明的优先。"""
    found: list[str] = []
    for node in doc.select("meta[http-equiv]"):
        if (node.get("http-equiv") or "").lower() == "mobile-agent":
            match = _AGENT_URL.search(node.get("content") or "")
            if match:
                found.append(join_url(page_url, match[1].strip("'\";")) or "")
    for node in doc.select("link[rel]"):
        rel = (node.get("rel") or "").lower()
        if rel == "alternate" and "screen" in (node.get("media") or "") or rel == "canonical":
            found.append(join_url(page_url, node.get("href") or "") or "")
    parts = urlsplit(page_url)
    host = parts.hostname or ""
    if host.startswith(("m.", "wap.")):
        swapped = "www." + host.split(".", 1)[1]
    else:
        swapped = "m." + (host[4:] if host.startswith("www.") else host)
    found.append(urlunsplit((parts.scheme, swapped, parts.path, parts.query, "")))
    here = urlsplit(page_url).netloc
    out: list[str] = []
    for url in found:
        target = urlsplit(url)
        if (
            url
            and is_usable_url(url)
            and target.netloc != here
            and _site(target.hostname or "") == _site(host)
            and url not in out
        ):
            out.append(url)
    return out[:3]


async def expand_alternate(client: _Client, page: Response) -> Response:
    doc = parse_html(page.text, base_url=page.url)
    count = len(discover_chapters(doc, page.url, limit=20001))
    if count >= 2 or any(
        n.tag == "a" and classify_next(n.text) == "chapter" for n in doc.iter_elements()
    ):
        return page
    for url in alternates(doc, page.url):
        mobile = (urlsplit(url).hostname or "").startswith(("m.", "wap."))
        try:
            other = await client.get(
                url, referer=page.url, headers={"user-agent": MOBILE_UA} if mobile else None
            )
        except FetchError as exc:
            _LOG.info("另一版本不可用：%s", exc)
            continue
        found = discover_chapters(
            parse_html(other.text, base_url=other.url), other.url, limit=20001
        )
        if len(found) > max(count, 1):
            return other
    return page
