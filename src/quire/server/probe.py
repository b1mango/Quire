"""新建任务前的 URL 探查：判定漫画/小说、估计页数或章数（项目设计.md §6.9）。

探查只读取入口页一次，不下载图片或章节正文；结果用于预填标题、
切换格式选项，并在选 PDF 但缺 Chrome 时于"开始"之前提示（决策 N）。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..assemble.models import clean_metadata_text
from ..errors import ParseError
from ..fetch.session import AsyncFetcher
from ..parse.chapters import discover_chapters, looks_like_catalogue
from ..parse.images import collect, prefilter
from ..parse.minidom import parse as parse_html
from ..utils.urls import is_usable_url


@dataclass(frozen=True, slots=True)
class ProbeResult:
    kind: str  # "manga" | "novel"
    title: str
    count: int  # 漫画为图片页数，小说为章节数
    url: str  # 重定向后的最终地址


def validate_task_url(url: str) -> str:
    """任务地址的后端复核：仅 http(s)、不含凭据与控制字符。"""
    from urllib.parse import urlsplit

    candidate = url.strip()
    parts = urlsplit(candidate)
    if (
        not is_usable_url(candidate)
        or parts.scheme not in {"http", "https"}
        or parts.username is not None
        or parts.password is not None
        or any(ord(c) < 32 for c in candidate)
    ):
        raise ParseError("这个地址不是有效的 http(s) 链接", hint="粘贴漫画或小说的目录页地址。")
    return candidate


async def probe_url(url: str, *, rate: float = 4.0, timeout: float = 20.0) -> ProbeResult:
    url = validate_task_url(url)
    async with AsyncFetcher(timeout=timeout, retries=1, concurrency=2, rate=rate) as client:
        page = await client.get(url)
    doc = parse_html(page.text, base_url=page.url)
    title_node = doc.select_one("h1") or doc.select_one("title")
    title = clean_metadata_text(title_node.text if title_node else "") or "未命名"
    links = discover_chapters(doc, page.url, limit=2001)
    if looks_like_catalogue(links):
        return ProbeResult("novel", title, len(links), page.url)
    kept, _ = prefilter(collect(doc, doc.effective_base() or page.url))
    if kept:
        return ProbeResult("manga", title, len(kept), page.url)
    raise ParseError(
        "这个链接没找到章节列表，也没有可用的图片",
        hint="要不要试试章节页或漫画阅读页的地址？动态页面暂请用 CLI 的 --render。",
    )
