"""站点目录扩展的统一入口:依次应用各站点适配器,新增站点在这里登记。"""

from __future__ import annotations

from typing import Protocol

from ..fetch.simple import Response
from . import fanqie, quanben


class _Client(Protocol):
    async def get(self, url: str, *, referer: str | None = None) -> Response: ...


async def expand_catalogue(client: _Client, page: Response) -> Response:
    """目录页经各站点适配器补全(不命中的适配器原样放行)。"""
    page = await quanben.expand_listing(client, page)
    return await fanqie.expand_listing(client, page)
