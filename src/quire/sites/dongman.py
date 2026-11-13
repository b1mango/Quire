"""dongmanmanhua.cn(咚漫)目录适配。

桌面目录页 ``/<分类>/<作品>/list?title_no=…&page=N`` 每页只列 10 话,翻页要走上百页;
同一作品的移动版目录页(m. 子域、移动端 UA)一次内嵌全部话数,每话为
``<li data-episode-no=N><a data-href="…/viewer?title_no=…&episode_no=N…">`` 加
``<span class="ellipsis">标题</span>``。这里用一次移动版请求取全量,按 episode_no
升序重建阅读页链接(统一路径,只留 title_no、episode_no 两个参数)替换页面正文,
再交给通用目录识别;请求或结构不符时退回桌面页本身(只有当页 10 话),不终止任务。
"""

from __future__ import annotations

import logging
import re
from html import escape
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

from ..errors import FetchError, ParseError
from ..fetch.simple import Response
from ..parse.minidom import parse as parse_html

_LOG = logging.getLogger(__name__)

_HOST = "dongmanmanhua.cn"
_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)
_VIEWER = re.compile(r"^(/[^?#]+/viewer)\?")


class _CatalogueClient(Protocol):
    async def get(
        self, url: str, *, referer: str | None = None, headers: dict[str, str] | None = None
    ) -> Response: ...


def _is_host(host: str) -> bool:
    host = host.lower()
    return host == _HOST or host.endswith("." + _HOST)


def is_list_page(url: str) -> bool:
    parts = urlsplit(url)
    return (
        _is_host(parts.hostname or "")
        and parts.path.endswith("/list")
        and bool(parse_qs(parts.query).get("title_no"))
    )


def is_viewer_page(url: str) -> bool:
    """单话阅读页：页内有前后话导航，不能当目录识别。"""
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    return (
        _is_host(parts.hostname or "")
        and parts.path.endswith("/viewer")
        and bool(query.get("title_no"))
        and bool(query.get("episode_no"))
    )


def episodes(html: str, title_no: str) -> dict[int, tuple[str, str]]:
    """移动版目录:episode_no → (阅读页路径, 标题);只收本作品的话。"""
    found: dict[int, tuple[str, str]] = {}
    for node in parse_html(html).iter_elements():
        if node.tag != "li" or not (node.get("data-episode-no") or "").isdigit():
            continue
        number = int(node.get("data-episode-no") or "0")
        link = next((n for n in node.iter_elements() if n.tag == "a" and n.get("data-href")), None)
        if link is None:
            continue
        href = link.get("data-href") or ""
        match = _VIEWER.match(href)
        query = parse_qs(urlsplit(href).query)
        if not match or query.get("title_no", [""])[0] != title_no:
            continue
        title = next(
            (
                n.text.strip()
                for n in node.iter_elements()
                if "ellipsis" in (n.get("class") or "").split()
            ),
            "",
        )
        # 通用目录识别会丢弃超过 60 字的标题（防导航误收），这里先截短
        title = title or f"第{number}话"
        found[number] = (match[1], title if len(title) <= 60 else title[:59] + "…")
    return found


async def expand_listing(client: _CatalogueClient, page: Response) -> Response:
    """咚漫目录页并入全部话数;非目标页或移动版失败时原样返回。"""
    if not is_list_page(page.url):
        return page
    parts = urlsplit(page.url)
    title_no = parse_qs(parts.query)["title_no"][0]
    mobile = f"https://m.{_HOST}{parts.path}?title_no={title_no}"
    try:
        response = await client.get(mobile, referer=page.url, headers={"user-agent": _MOBILE_UA})
        found = episodes(response.text, title_no)
    except (FetchError, ParseError) as exc:
        _LOG.warning("咚漫移动版目录读取失败,退回桌面页当页目录:%s", exc)
        return page
    if len(found) <= 10:
        return page
    # 阅读页路径里的标题段每话不同，通用识别按 URL 形态分组会被拆散；站点对任意标题段都会
    # 302 到规范地址，所以统一成 <作品>/episode/viewer，整本书只剩 episode_no 不同。
    # 链接与最终页同源（桌面页常 301 到不带 www 的裸域），否则通用识别按跨站过滤掉
    work = f"{parts.scheme}://{parts.netloc}{parts.path.removesuffix('/list')}"
    items = "".join(
        f'<li><a href="{escape(work)}/episode/viewer?title_no={escape(title_no)}'
        f'&amp;episode_no={number}"><span>{escape(title)}</span></a></li>'
        for number, (_, title) in sorted(found.items())
    )
    # 全量目录替换整页正文:桌面页还混着当页 10 话、「第一话」快捷入口和推荐作品,留着会干扰识别
    head = page.text[: page.text.find("<body")] if "<body" in page.text else ""
    h1 = parse_html(page.text).select_one("h1")  # 作品名（桌面页 h1.subj），书名用它而不是 <title>
    heading = f"<h1>{escape(h1.text.strip())}</h1>" if h1 is not None and h1.text.strip() else ""
    html = f'{head}<body>{heading}<ul id="quire-catalogue">{items}</ul></body></html>'
    headers = {**page.headers, "content-type": "text/html; charset=utf-8"}
    return Response(page.url, page.status, headers, html.encode("utf-8"), page.elapsed_ms)
