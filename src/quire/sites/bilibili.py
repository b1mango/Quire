"""B 站漫画(manga.bilibili.com)阅读页适配:被动旁观式捕获正文图地址。

为什么不是通用的代理渲染:

* 阅读页是 canvas 渲染的 Vue SPA,DOM 里没有任何正文 ``<img>``;
* 章节接口 ``GetImageIndex``/``ImageToken`` 必须带页面 JS 现算的
  ``ultra_sign``/``m2`` 签名,且响应正文是 ``bytesData`` 加密字段——
  用我们的 HTTP 客户端原样重放页面请求,实测也只回 ``code 99``。
  签名与浏览器自身请求上下文绑定,逆向混淆 JS 不在项目策略内。

可行路径(实测验证):页面解密后由浏览器自己请求最终图
(``*.hdslb.com/bfs/manga/...?token=...``),这些地址在 token 有效期内
可直接下载。于是渲染通道对 B 站改为**旁观**:浏览器直连本站与图床
(ChromeProcess 的 proxy_bypass 例外),Fetch 拦截层只记录图片请求
地址并立即放行,不代理任何流量;用 ArrowLeft(日漫从右往左,左=下一页)
逐页翻完整章,按首次出现顺序重建一份带 ``<img>`` 清单的页面,
交回通用发现/下载管线。

限制(如实记录):

* 翻页逐张加载,长章耗时随页数线性增长,受渲染超时约束;
* token 有时效,捕获完成后须在在同一任务内立刻下载;
* 少数图以加密 blob(mangaup ``?cpx=``)下发,无法离线还原,
  未收齐阅读器声明页数时整章失败，不静默导出残缺章节;
* 图片为图床按视口宽度派生的 AVIF(如 ``@935w.avif``),与原始分辨率
  有差距;修改后缀会使签名失效(实测 403),只能按页面给出的规格取图。
"""

from __future__ import annotations

import asyncio
import json
import re
from html import escape
from urllib.parse import parse_qs, urlsplit

from ..errors import FetchError, QuireError, UnsupportedError
from ..fetch.browser import RenderOptions
from ..fetch.browser_cdp import Cdp
from ..fetch.browser_process import ChromeProcess, find_chrome
from ..fetch.simple import Response

_HOST = "manga.bilibili.com"
#: 浏览器直连例外:本站(页面与签名接口)与图床(正文图)。其余域名仍进黑洞代理。
_PROXY_BYPASS = "*.bilibili.com;*.hdslb.com"
#: 正文图请求特征:图床 manga 路径,含签名 token 的才是页面解密后的最终图。
_IMAGE_PATTERN = "*://*.hdslb.com/bfs/manga/*"
_READER_PATH = re.compile(r"^/mc\d+/\d+/?$")
#: 阅读器页脚的总页数指示(如 "54P"),仅作识别阶段的粗估。
_TOTAL_PAGES = re.compile(r"(\d{1,4})\s*P\b")
_STATE = (
    "JSON.stringify({ready: !!document.querySelector('.current-page'),"
    " page: (document.querySelector('.current-page')||{}).textContent || '',"
    " title: document.title || '',"
    " href: location.href,"
    " text: (document.body ? document.body.innerText : '').slice(0, 500)})"
)
_KEY = {
    "key": "ArrowLeft",
    "code": "ArrowLeft",
    "windowsVirtualKeyCode": 37,
    "nativeVirtualKeyCode": 37,
}


def is_reader_page(url: str) -> bool:
    parts = urlsplit(url)
    return (
        parts.scheme == "https"
        and parts.username is None
        and parts.password is None
        and (parts.hostname or "").lower() == _HOST
        and _READER_PATH.fullmatch(parts.path) is not None
    )


class _ImageTap:
    """Fetch 事件的消费者:按首次出现顺序记录正文图地址,其余事件读掉防队列溢出。"""

    def __init__(self, cdp: Cdp, session: str) -> None:
        self.cdp, self.session = cdp, session
        self.urls: list[str] = []
        self._known: set[str] = set()

    async def pump(self) -> None:
        while True:
            event = await self.cdp.events.get()
            if event.get("sessionId") != self.session:
                continue
            if event.get("method") != "Fetch.requestPaused":
                continue
            params = event["params"]
            url = params["request"]["url"]
            # 加密 blob(?cpx=)离线无法还原,不计入正文清单。
            parts = urlsplit(url)
            host = parts.hostname or ""
            query = parse_qs(parts.query)
            identity = parts._replace(query="", fragment="").geturl()
            if (
                parts.scheme == "https"
                and host.endswith(".hdslb.com")
                and parts.path.startswith("/bfs/manga/")
                and query.get("token")
                and "cpx" not in query
                and identity not in self._known
            ):
                self._known.add(identity)
                self.urls.append(url)
            try:
                await self.cdp.call(
                    "Fetch.continueRequest",
                    {"requestId": params["requestId"]},
                    session_id=self.session,
                )
            except QuireError:
                pass  # 页面取消了请求(预取竞态),无需放行


async def _press_left(cdp: Cdp, session: str) -> None:
    for kind in ("rawKeyDown", "keyUp"):
        await cdp.call("Input.dispatchKeyEvent", {"type": kind, **_KEY}, session_id=session)


async def _state(cdp: Cdp, session: str) -> dict[str, object]:
    reply = await cdp.call(
        "Runtime.evaluate", {"expression": _STATE, "returnByValue": True}, session_id=session
    )
    value = reply.get("result", {}).get("value")
    try:
        state = json.loads(value) if isinstance(value, str) else None
    except ValueError:
        state = None
    return state if isinstance(state, dict) else {}


def _page_number(state: dict[str, object]) -> int:
    try:
        return int(str(state.get("page") or ""))
    except ValueError:
        return 0


def _total_pages(state: dict[str, object]) -> int:
    found = _TOTAL_PAGES.findall(str(state.get("text") or ""))
    return max((int(item) for item in found), default=0)


async def capture_reader_page(
    url: str, render: RenderOptions, *, full: bool = True
) -> tuple[Response, int]:
    """旁观式捕获一章的正文图,重建为带 ``<img>`` 清单的页面;同时返回 UI 总页数。

    ``full=False``(识别阶段)只等首屏图片就绪并读取总页数,不逐页翻完。
    """
    if not is_reader_page(url):
        raise UnsupportedError("请使用 B站漫画 HTTPS 单话阅读页")
    executable = find_chrome(render.executable)
    if executable is None:
        raise UnsupportedError(
            "动态采集需要系统Chrome/Edge/Brave/Chromium", hint="安装Chrome或指定--chrome"
        )
    try:
        async with asyncio.timeout(render.timeout):
            async with ChromeProcess(
                executable,
                extra_args=("--window-size=1600,2400",),
                proxy_bypass=_PROXY_BYPASS,
            ) as chrome:
                async with Cdp(chrome.endpoint) as cdp:
                    await cdp.call("Browser.setDownloadBehavior", {"behavior": "deny"})
                    target = await cdp.call("Target.createTarget", {"url": "about:blank"})
                    attached = await cdp.call(
                        "Target.attachToTarget",
                        {"targetId": target["targetId"], "flatten": True},
                    )
                    session = attached["sessionId"]
                    await cdp.call("Page.enable", session_id=session)
                    await cdp.call(
                        "Fetch.enable",
                        {"patterns": [{"urlPattern": _IMAGE_PATTERN}]},
                        session_id=session,
                    )
                    tap = _ImageTap(cdp, session)
                    pump = asyncio.create_task(tap.pump())
                    try:
                        navigation = await cdp.call(
                            "Page.navigate", {"url": url}, session_id=session
                        )
                        if navigation.get("errorText"):
                            raise FetchError("B站漫画阅读页导航失败")
                        state = await _await_ready(cdp, session)
                        await _await_first_images(tap)
                        if pump.done():
                            pump.result()
                        if full:
                            await _paginate(cdp, session, tap, render, url, _total_pages(state))
                            state = await _state(cdp, session)
                        if pump.done():
                            pump.result()
                    finally:
                        pump.cancel()
                        await asyncio.gather(pump, return_exceptions=True)
    except TimeoutError:
        raise FetchError(
            "B站漫画页面翻页超时，未确认内容完整", hint="可增大 --render-timeout 后重试。"
        ) from None
    if not tap.urls:
        raise FetchError(
            "B站漫画正文图未能捕获",
            hint="该话可能需要登录/付费，或站点页面结构已改版；免费话请确认链接是阅读页。",
        )
    title = str(state.get("title") or "").strip() or "bilibili-manga"
    return _listing_response(url, title, tap.urls), _total_pages(state)


def _listing_response(url: str, title: str, images: list[str]) -> Response:
    """把捕获到的图片地址重建为带 ``<img>`` 清单的静态页,交回通用发现管线。"""
    items = "".join(f'<img src="{escape(image, quote=True)}">' for image in images)
    html = (
        f"<!DOCTYPE html><html><head><title>{escape(title)}</title></head>"
        f"<body>{items}</body></html>"
    )
    return Response(url, 200, {"content-type": "text/html; charset=utf-8"}, html.encode(), 0)


async def _await_ready(cdp: Cdp, session: str) -> dict[str, object]:
    deadline = asyncio.get_running_loop().time() + 20
    while True:
        state = await _state(cdp, session)
        if state.get("ready"):
            return state
        if asyncio.get_running_loop().time() > deadline:
            raise FetchError(
                "B站漫画阅读器未就绪",
                hint="页面可能改版或需要登录；请在浏览器里确认该话能正常阅读。",
            )
        await asyncio.sleep(0.5)


async def _await_first_images(tap: _ImageTap) -> None:
    """识别阶段:等首屏图片到达并短暂稳定(最多 10 秒),不逐页翻完。"""
    deadline = asyncio.get_running_loop().time() + 10
    quiet = asyncio.get_running_loop().time()
    count = -1
    while asyncio.get_running_loop().time() < deadline:
        if len(tap.urls) != count:
            count, quiet = len(tap.urls), asyncio.get_running_loop().time()
        elif count > 0 and asyncio.get_running_loop().time() - quiet >= 2:
            return
        await asyncio.sleep(0.3)


async def _paginate(
    cdp: Cdp,
    session: str,
    tap: _ImageTap,
    render: RenderOptions,
    url: str,
    total: int,
) -> None:
    """ArrowLeft 逐页翻到底。

    到底的三个判据(实测:阅读器翻到末尾会自动进入下一话继续出图):
    收齐阅读器 UI 报告的总页数;页面 URL 离开本话(翻进了下一话);
    图片数与页码同时停滞若干轮。
    """
    if not total:
        raise FetchError("B站漫画总页数未能确认，已停止以避免导出残缺章节")
    entry_path = urlsplit(url).path.rstrip("/")
    stalled = 0
    last = (len(tap.urls), 0)
    for _ in range(render.max_scrolls):
        # 不再翻过末页，防止下一话请求混入本话。
        if len(tap.urls) == total:
            return
        if len(tap.urls) > total:
            raise FetchError("B站漫画图片数与阅读器不一致，未确认章节边界")
        await _press_left(cdp, session)
        await asyncio.sleep(0.6)
        state = await _state(cdp, session)
        if urlsplit(str(state.get("href") or "")).path.rstrip("/") != entry_path:
            raise FetchError("B站漫画提前跳转至其他章节，未确认内容完整")
        current = (len(tap.urls), _page_number(state))
        if current == last:
            stalled += 1
            if stalled >= 5:
                raise FetchError("B站漫画翻页停滞，正文未收齐；请确认该话可以完整阅读")
        else:
            stalled, last = 0, current
    if len(tap.urls) == total:
        return
    raise FetchError("B站漫画翻页达到上限，未确认内容完整", hint="请增大 --max-scrolls 后重试。")
