"""Rule-driven removal, dynamic capture and bounded manga chapter pagination."""

from __future__ import annotations

from dataclasses import replace

from .assemble.models import clean_metadata_text
from .errors import ConfigError
from .fetch.browser import RenderOptions, render_page
from .fetch.session import AsyncFetcher
from .manga import discover_page
from .models import MangaOptions, MangaResult
from .parse.chapters import find_next_page
from .parse.images import Candidate
from .parse.minidom import parse


async def discover_manga(
    client: AsyncFetcher, url: str, opts: MangaOptions, render: RenderOptions | None = None
) -> tuple[list[Candidate], MangaResult]:
    candidates: list[Candidate] = []
    visited: set[str] = set()
    seen: set[str] = set()
    current: str | None = url
    result: MangaResult | None = None
    while current:
        if current in visited or len(visited) >= 20:
            raise ConfigError("漫画章节分页循环或超过 20 页；请检查 chapter.next_page 规则")
        visited.add(current)
        page = await client.get(current, referer=opts.referer or (url if current != url else None))
        notes: tuple[str, ...] = ()
        if render:
            page, notes = await render_page(page, client, render)
        found, part = discover_page(
            current,
            replace(opts, first=1, last=0) if opts.next_selector or opts.follow_pages else opts,
            page,
        )
        if result is None:
            result = part
        else:
            result = replace(
                result,
                warnings=result.warnings + part.warnings,
                rejections=result.rejections + part.rejections,
            )
        result = replace(result, warnings=result.warnings + notes)
        for candidate in found:
            if candidate.url not in seen:
                seen.add(candidate.url)
                candidates.append(candidate)
        current = (
            find_next_page(
                parse(page.text, base_url=page.url), page.url, page.url, selector=opts.next_selector
            )
            if opts.next_selector or opts.follow_pages
            else None
        )
    assert result is not None
    if opts.next_selector or opts.follow_pages:
        candidates = candidates[opts.first - 1 : opts.last or None]
        if not candidates:
            raise ConfigError("页范围超出章节图片数")
    return candidates, replace(result, title=clean_metadata_text(result.title))
