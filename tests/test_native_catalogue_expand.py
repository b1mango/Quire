"""真实浏览器模式下，下载任务与探查一样在渲染之后补全目录（咚漫全量等）。"""

from __future__ import annotations

import asyncio

import pytest

from quire import core_series
from quire.errors import ConfigError
from quire.fetch.browser import RenderOptions
from quire.fetch.simple import Response

URL = "https://dongman.test/list"


def _page(count: int) -> Response:
    items = "".join(f'<li><a href="/v/{n}.html">第{n}话</a></li>' for n in range(1, count + 1))
    html = (
        f'<html><body><h1>天堂岛</h1><div class="chapter-list"><ul>{items}</ul></div></body></html>'
    )
    return Response(URL, 200, {"content-type": "text/html"}, html.encode(), 1)


def test_series_expands_catalogue_after_native_render(monkeypatch, tmp_path):
    calls: list[str] = []

    async def fetch(client, url, render, referer=None):
        calls.append("fetch")
        return _page(3)  # 真实浏览器渲染出的桌面页只露出 3 话

    async def render_page(page, client, render, content="images"):
        calls.append("render")
        return page, ()

    async def expand(client, page, *, catalogue=True):
        calls.append("expand")
        return _page(49)

    async def stop_after_plan(*args, **kwargs):
        raise ConfigError("planned")

    monkeypatch.setattr(core_series, "fetch_render_input", fetch)
    monkeypatch.setattr(core_series, "render_page", render_page)
    monkeypatch.setattr(core_series, "expand_catalogue", expand)
    monkeypatch.setattr(core_series, "_pages", stop_after_plan)

    class Client:
        escalation = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

    with pytest.raises(ConfigError, match="planned"):  # 1–5 话不再报「超出当前目录（共 3 章）」
        asyncio.run(
            core_series.run_series(
                URL,
                tmp_path / "out",
                first=1,
                last=5,
                formats=("cbz",),
                fetcher=Client(),  # type: ignore[arg-type]
                render=RenderOptions(native=True, timeout=60),
            )
        )
    assert calls == ["fetch", "render", "expand"]
