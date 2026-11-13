"""站点目录扩展的统一入口:依次应用各站点适配器,新增站点在这里登记。"""

from __future__ import annotations

from typing import Protocol

from ..fetch.simple import Response
from . import catalogue_pages, cms, dongman, fanqie, full_catalogue, mobile_site, quanben


class _Client(Protocol):
    async def get(
        self, url: str, *, referer: str | None = None, headers: dict[str, str] | None = None
    ) -> Response: ...

    async def post(
        self, url: str, *, body: bytes, content_type: str, referer: str | None = None
    ) -> Response: ...


async def expand_catalogue(client: _Client, page: Response, *, catalogue: bool = True) -> Response:
    """目录页经各站点适配器补全(不命中的适配器原样放行)。

    ``catalogue=False``(用户指定单章)时跳过通用补全,只留站点适配器。
    """
    page = await quanben.expand_listing(client, page)
    page = await dongman.expand_listing(client, page)
    page = await fanqie.expand_listing(client, page)
    if not catalogue:
        return page
    # 建站程序接口 → 「查看全部章节」→ 桌面/移动版互换 → 分页合并
    page = await cms.expand_madara(client, page)
    page = await cms.expand_mccms(client, page)
    page = await full_catalogue.expand_listing(client, page)
    page = await mobile_site.expand_alternate(client, page)
    return await catalogue_pages.expand_pages(client, page)
