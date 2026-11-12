"""咚漫适配：移动版全量目录、统一阅读页链接、单话页识别，以及自动判类走已知站点。"""

from __future__ import annotations

import asyncio

from quire.fetch.request_shape import public_headers
from quire.fetch.simple import Response
from quire.parse.chapters import discover_chapters
from quire.parse.minidom import parse as parse_html
from quire.sites import dongman
from quire.sites.kind_guard import expected_kind

LIST = "https://www.dongmanmanhua.cn/FANTASY/feirenzai/list?title_no=1804&page=1"
LONG = "第497话 头裹团花手帕，身穿纳锦云袍。腰间双束虎筋绦，微露绣裙偏绡。凤嘴弓鞋三寸，龙须膝裤金销。手提宝剑怒声高，凶比月婆容貌。"
TITLES = {n: f"第{n}话 标题{n}" for n in range(1, 15)} | {
    7: "番外 水调歌头1",
    8: "第【1003】话 鼻子",
    9: LONG,
}


def _item(number: int, title: str, title_no: str = "1804") -> str:
    return (
        f'<li id="episode_{number}" data-episode-no="{number}">'
        f'<a class="workEpisodeListItem" data-href="/FANTASY/feirenzai/slug{number}/viewer?title_no={title_no}'
        f'&episode_no={number}&source=mweb"><p class="sub_title"><span class="ellipsis">{title}</span></p></a></li>'
    )


def _mobile() -> str:
    items = "".join(_item(n, t) for n, t in sorted(TITLES.items(), reverse=True))
    return f"<html><body><ul>{items}{_item(99, '别的作品', title_no='42')}</ul></body></html>"


def _desktop() -> Response:
    html = '<html><head><title>非人哉_官方在线漫画阅读</title></head><body><h1 class="subj">非人哉</h1>'
    html += "".join(
        f'<a href="/x/viewer?title_no=1804&episode_no={n}">第{n}话</a>' for n in (14, 13)
    )
    html += "</body></html>"
    return Response(LIST, 200, {"content-type": "text/html"}, html.encode(), 1)


class _Client:
    def __init__(self, body: str) -> None:
        self.body, self.calls = body, []

    async def get(self, url, *, referer=None, headers=None):
        self.calls.append((url, headers))
        return Response(url, 200, {"content-type": "text/html"}, self.body.encode(), 1)


def test_page_shapes_and_known_kind():
    assert dongman.is_list_page(LIST)
    assert not dongman.is_list_page("https://www.dongmanmanhua.cn/FANTASY/feirenzai/list")
    viewer = "https://www.dongmanmanhua.cn/FANTASY/feirenzai/x/viewer?title_no=1804&episode_no=5"
    assert dongman.is_viewer_page(viewer) and not dongman.is_list_page(viewer)
    assert not dongman.is_viewer_page("https://evil.test/a/viewer?title_no=1&episode_no=2")
    assert expected_kind(LIST) == "manga"  # 自动识别时先信已知站点


def test_mobile_episodes_skip_other_titles_and_shorten_long_names():
    found = dongman.episodes(_mobile(), "1804")
    assert sorted(found) == list(range(1, 15))
    assert found[7][1] == "番外 水调歌头1"
    assert len(found[9][1]) <= 60 and found[9][1].endswith("…")


def test_expand_rebuilds_full_catalogue_in_reading_order():
    client = _Client(_mobile())
    page = asyncio.run(dongman.expand_listing(client, _desktop()))
    url, headers = client.calls[0]
    assert url.startswith("https://m.dongmanmanhua.cn/FANTASY/feirenzai/list?title_no=1804")
    assert "iPhone" in headers["user-agent"]  # 移动版目录要移动端 UA，否则 302 回桌面版
    doc = parse_html(page.text)
    assert doc.select_one("h1").text == "非人哉"
    links = discover_chapters(doc, LIST, limit=20001)
    assert [link.title for link in links][:2] == ["第1话 标题1", "第2话 标题2"]
    assert len(links) == 14  # 番外、无章号、超长标题都不再被通用规则丢掉
    assert {link.url.split("?")[0] for link in links} == {
        "https://www.dongmanmanhua.cn/FANTASY/feirenzai/episode/viewer"
    }
    assert links[-1].url.endswith("title_no=1804&episode_no=14")


def test_expand_falls_back_when_mobile_list_is_unusable():
    page = _desktop()
    assert asyncio.run(dongman.expand_listing(_Client("<html></html>"), page)) is page
    other = Response("https://quanben.io/n/a/list.html", 200, {}, b"<html></html>", 1)
    assert asyncio.run(dongman.expand_listing(_Client(_mobile()), other)) is other


def test_custom_user_agent_is_a_public_header():
    assert public_headers(None, {"user-agent": "UA/1"})["user-agent"] == "UA/1"
