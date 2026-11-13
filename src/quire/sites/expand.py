"""站点目录扩展的统一入口:依次应用各站点适配器,新增站点在这里登记。"""

from __future__ import annotations

from typing import Protocol

from ..fetch.simple import Response
from . import catalogue_pages, dongman, fanqie, full_catalogue, quanben


class _Client(Protocol):
    async def get(
        self, url: str, *, referer: str | None = None, headers: dict[str, str] | None = None
    ) -> Response: ...


async def expand_catalogue(client: _Client, page: Response) -> Response:
    """目录页经各站点适配器补全(不命中的适配器原样放行)。"""
    page = await quanben.expand_listing(client, page)
    page = await dongman.expand_listing(client, page)
    page = await fanqie.expand_listing(client, page)
    # 通用兜底：书页只列最新几章时跟「查看全部章节」
    page = await full_catalogue.expand_listing(client, page)
    return await catalogue_pages.expand_pages(client, page)  # 分页目录合并
