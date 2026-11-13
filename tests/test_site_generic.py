"""通用站点增强：Cookie 验证门、多线路目录去重、未知站判类、阅读框架跟随、系统代理。"""

from __future__ import annotations

import asyncio

import httpx

from quire.fetch import proxy
from quire.fetch.challenge import cookie_gate
from quire.fetch.simple import Response
from quire.parse.catalog_links import pick_source
from quire.parse.chapters import ChapterLink, discover_chapters
from quire.parse.minidom import parse as parse_html
from quire.sites import reader_frame
from quire.sites.kind_guard import guess_kind

GATE = (
    '<button id="vb">点击完成验证</button><script>var b=document.getElementById("vb"),'
    'k="c26451e0ee406f87";b.addEventListener("click",function(){document.cookie="omo_token="+k+'
    '"; max-age=259200; path=/";t.location.reload()})</script>'
)


def test_cookie_gate_reads_variable_and_literal_forms():
    assert cookie_gate(GATE, len(GATE)) == ("omo_token", "c26451e0ee406f87")
    literal = (
        '<script>document.cookie="pass=ok1234; path=/";location.replace(location.href)</script>'
    )
    assert cookie_gate(literal, len(literal)) == ("pass", "ok1234")


def test_cookie_gate_ignores_normal_pages_and_cloudflare():
    analytics = '<script>document.cookie="seen=1abc"</script>'  # 不刷新：普通统计脚本
    assert cookie_gate(analytics, len(analytics)) is None
    assert cookie_gate(GATE, 64 * 1024) is None  # 大页面不当验证门
    cf = '<script>document.cookie = "cf_clearance=e2e-token; path=/";location.reload();</script>'
    assert cookie_gate(cf, len(cf)) is None  # Cloudflare 凭据交给真实 Chrome


def test_gate_cookie_is_sent_on_retry():
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("cookie"))
        if request.headers.get("cookie") != "omo_token=c26451e0ee406f87":
            return httpx.Response(403, stream=httpx.ByteStream(GATE.encode()))
        return httpx.Response(
            200, stream=httpx.ByteStream("<html><body>正文</body></html>".encode())
        )

    async def run() -> Response:
        from quire.fetch.session import AsyncFetcher

        async with AsyncFetcher(
            transport=httpx.MockTransport(handler), respect_robots=False, retries=0
        ) as client:
            await client.get("https://gate.test/comic/1.html")
            return await client.get("https://gate.test/comic/2.html")

    page = asyncio.run(run())
    assert "正文" in page.text
    assert seen == [None, "omo_token=c26451e0ee406f87", "omo_token=c26451e0ee406f87"]


def _links(source: int, count: int, start: int = 1) -> list[ChapterLink]:
    return [
        ChapterLink(f"第{n}话", f"https://m.test/chapter/2797-{source}-{n + 1}.html", n)
        for n in range(start, start + count)
    ]


def test_pick_source_keeps_the_fullest_playlist():
    chosen = pick_source(_links(1, 6) + _links(2, 9))
    assert len(chosen) == 9 and all("-2-" in link.url for link in chosen)


def test_pick_source_leaves_volumes_and_plain_lists_alone():
    plain = _links(1, 12)
    assert pick_source(plain) == plain
    volumes = [
        ChapterLink(f"第{n}章 卷{v}事{n}", f"https://n.test/book/9/{v}/{n}.html", n)
        for v in (1, 2)
        for n in range(1, 6)
    ]
    assert pick_source(volumes) == volumes  # 分卷标题不重合，不是并行线路


def test_discover_merges_playlists_into_one_source():
    tabs = "".join(
        '<ul class="stui-content__playlist">'
        + "".join(
            f'<li><a href="/chapter/2797-{s}-{n + 1}.html">第{n}话</a></li>' for n in range(1, k)
        )
        + "</ul>"
        for s, k in ((1, 8), (2, 11))
    )
    url = "https://m.test/comic/2797.html"
    links = discover_chapters(parse_html(f"<html><body>{tabs}</body></html>"), url)
    assert len(links) == 10 and links[0].url.endswith("/chapter/2797-2-2.html")


def test_guess_kind_uses_url_title_and_chapter_names():
    assert (
        guess_kind("https://www.kalamanhua.com/comic/2797.html", "意中人漫画", ["第一话"])
        == "manga"
    )
    assert guess_kind("https://x.test/b/1/", "某某小说网", ["第一章 起"]) == "novel"
    assert guess_kind("https://x.test/b/1/", "", ["第1话 a", "第2话 b"]) == "manga"
    assert guess_kind("https://x.test/b/1/", "", []) == "novel"  # 没证据按小说


PLAYER = (
    '<script>var maccms={"path":"","mid":"1"};</script>'
    '<script>var player_aaaa={"encrypt":"3","url":"abc123","from":"wmmh","link":"/x"}</script>'
)
SCRIPT = (
    'MacPlayer.Html = \'<iframe border="0" src="https://img.test/play/min/\' + MacPlayer.PlayUrl'
    ' + \'" width="100%"></iframe>\';MacPlayer.Show();'
)
FRAME = '<html><body><img src="data:image/gif;base64,R0" data-src="/image/1.webp"></body></html>'


class _Client:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages, self.calls = pages, []

    async def get(self, url, *, referer=None, headers=None):
        self.calls.append(url)
        return Response(url, 200, {"content-type": "text/html"}, self.pages[url].encode(), 1)


def test_maccms_player_frame_is_followed():
    page = Response("https://m.test/chapter/1-2-3.html", 200, {}, PLAYER.encode(), 1)
    client = _Client(
        {"https://m.test/static/player/wmmh.js": SCRIPT, "https://img.test/play/min/abc123": FRAME}
    )
    frame = asyncio.run(reader_frame.follow(client, page, manga=True))
    assert frame.url == "https://img.test/play/min/abc123"


def test_player_url_decodes_like_macplayer():
    assert reader_frame.player_url({"url": "a%2Fb", "encrypt": "1"}) == "a/b"
    assert reader_frame.player_url({"url": "YSUyRmI=", "encrypt": "2"}) == "a/b"
    assert reader_frame.player_url({"url": "raw", "encrypt": "3"}) == "raw"


def test_frame_follow_falls_back_to_page():
    lone = '<html><body><iframe src="/reader/1"></iframe></body></html>'
    page = Response("https://m.test/c/1.html", 200, {}, lone.encode(), 1)
    client = _Client({"https://m.test/reader/1": FRAME})
    assert asyncio.run(reader_frame.follow(client, page, manga=True)).url.endswith("/reader/1")
    assert asyncio.run(reader_frame.follow(client, page, manga=False)) is page  # 小说不跟 iframe
    empty = _Client({"https://m.test/reader/1": "<html></html>"})
    assert asyncio.run(reader_frame.follow(empty, page, manga=True)) is page  # 框架里也没图


def test_system_proxy_switch_and_loopback_bypass(monkeypatch):
    monkeypatch.setenv("QUIRE_SYSTEM_PROXY", "0")
    assert proxy.system_proxy() is None
    assert proxy.proxy_mounts(None, httpx.Limits()) is None
    mounts = proxy.proxy_mounts("http://127.0.0.1:7897", httpx.Limits())
    assert mounts is not None and mounts["all://127.0.0.1"] is None
    assert isinstance(mounts["all://"], proxy.FallbackTransport)


def test_fallback_transport_switches_host_to_direct_and_remembers():
    calls: list[str] = []

    def broken(request: httpx.Request) -> httpx.Response:
        calls.append("proxy")
        raise httpx.RemoteProtocolError("reset", request=request)

    def direct(request: httpx.Request) -> httpx.Response:
        calls.append("direct")
        return httpx.Response(200, text="ok")

    transport = proxy.FallbackTransport(httpx.MockTransport(broken), httpx.MockTransport(direct))

    async def run() -> None:
        async with httpx.AsyncClient(transport=transport) as client:
            assert (await client.get("https://cn.test/a")).text == "ok"
            assert (await client.get("https://cn.test/b")).text == "ok"

    asyncio.run(run())
    assert calls == ["proxy", "direct", "direct"]  # 第二次不再试代理
    assert transport.direct_hosts == {"cn.test"}


def test_fallback_transport_tries_direct_when_proxy_exit_is_blocked():
    def blocked(request: httpx.Request) -> httpx.Response:
        return httpx.Response(405, text="geo")

    def direct(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200 if request.url.host == "cn.test" else 403, text="d")

    transport = proxy.FallbackTransport(httpx.MockTransport(blocked), httpx.MockTransport(direct))

    async def run() -> tuple[int, int]:
        async with httpx.AsyncClient(transport=transport) as client:
            ok = await client.get("https://cn.test/")
            still = await client.get("https://cf.test/")  # 直连也被拒：如实返回代理的结果
            return ok.status_code, still.status_code

    assert asyncio.run(run()) == (200, 405)
    assert transport.direct_hosts == {"cn.test"}


def test_script_array_images_cover_common_templates():
    from quire.parse.packed import extract_script_images

    sinmh = (
        '<script>var chapterImages = ["001.jpg","002.jpg","003.jpg"];var chapterPath = '
        '"images/comic/12/345/";var pageImage = "https://res.test/images/default/cover.png";</script>'
    )
    assert extract_script_images(sinmh) == [
        f"https://res.test/images/comic/12/345/00{n}.jpg" for n in (1, 2, 3)
    ]
    absolute = '<script>var newImgs = ["https:\\/\\/img.test\\/a\\/1.webp","https:\\/\\/img.test\\/a\\/2.webp"]</script>'
    assert extract_script_images(absolute) == [
        "https://img.test/a/1.webp",
        "https://img.test/a/2.webp",
    ]
    import base64

    run = 'ts_reader.run({"sources":[{"images":["https://i.test/c/1.jpg","https://i.test/c/2.jpg"]}]})'
    encoded = base64.b64encode(run.encode()).decode()
    inline = f'<script src="data:text/javascript;base64,{encoded}"></script>'
    assert len(extract_script_images(inline)) == 2


def test_unnamed_arrays_need_one_folder_and_five_images():
    from quire.parse.script_images import extract_array_images

    covers = '<script>x=["https://c.test/a/1.jpg","https://c.test/b/2.jpg","https://c.test/c/3.jpg","https://c.test/d/4.jpg","https://c.test/e/5.jpg"]</script>'
    assert extract_array_images(covers) == []  # 推荐作品封面散在不同目录
    pages = (
        "<script>x=[" + ",".join(f'"https://c.test/ch/{n}.jpg"' for n in range(1, 7)) + "]</script>"
    )
    assert len(extract_array_images(pages)) == 6
    relative = '<script>var chapterImages = ["1.jpg","2.jpg"]</script>'
    assert extract_array_images(relative) == []  # 不知道图床：交给渲染
