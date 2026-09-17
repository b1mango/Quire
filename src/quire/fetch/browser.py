"""Bounded DOM rendering in an isolated system Chrome, with explicit scrolling."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import Any, Literal

from ..errors import ConfigError, FetchError, UnsupportedError
from ..utils.urls import is_usable_url
from .browser_cdp import Cdp
from .browser_network import BrowserNetwork
from .browser_process import ChromeProcess, find_chrome
from .session import AsyncFetcher
from .simple import DEFAULT_UA, Response

_STATE = """(() => {
 const root = document.scrollingElement;
 return {url: location.href, ready: document.readyState, y: scrollY,
 height: root ? root.scrollHeight : 0, viewport: innerHeight,
 text: document.body ? document.body.innerText.trim() : '',
 images: [...document.images].map(i => i.currentSrc || i.src || i.dataset.src || '').join('|')};
})()"""
_DOM = """(() => {
 const copy = document.documentElement.cloneNode(true);
 const originals = [...document.images];
 copy.querySelectorAll('img').forEach((image, i) => {
   const url = originals[i].currentSrc || originals[i].src;
   if (/^https?:/.test(url)) image.setAttribute('src', url);
 });
 return {url: location.href, html: '<!DOCTYPE html>' + copy.outerHTML};
})()"""


@dataclass(frozen=True)
class RenderOptions:
    executable: str | None = None
    timeout: float = 30
    max_scrolls: int = 100
    settle: float = 1.0

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.timeout)
            or not 0 < self.timeout <= 300
            or not math.isfinite(self.settle)
            or not 0.2 <= self.settle <= 10
            or self.timeout <= self.settle
            or type(self.max_scrolls) is not int
            or not 1 <= self.max_scrolls <= 1000
        ):
            raise ConfigError("渲染超时须大于稳定等待且≤300秒，滚动次数须为1-1000")


async def _evaluate(cdp: Cdp, session: str, expression: str) -> Any:
    reply = await cdp.call(
        "Runtime.evaluate", {"expression": expression, "returnByValue": True}, session_id=session
    )
    if "exceptionDetails" in reply:
        raise FetchError("动态页面脚本执行失败")
    return reply.get("result", {}).get("value")


async def _scroll(
    cdp: Cdp,
    session: str,
    network: BrowserNetwork,
    opts: RenderOptions,
    content: Literal["images", "text"] = "images",
) -> None:
    previous: tuple[object, ...] | None = None
    stable = asyncio.get_running_loop().time()
    scrolls = 0
    while True:
        network.check()
        state = await _evaluate(cdp, session, _STATE)
        if not isinstance(state, dict) or not is_usable_url(state.get("url", "")):
            raise FetchError("动态页面导航到了不支持的地址")
        visible = state.get(content, "")
        marker = (state["url"], state["height"], state["y"], visible)
        now = asyncio.get_running_loop().time()
        if marker != previous or network.pending or state["ready"] != "complete":
            previous, stable = marker, now
        bottom = state["y"] + state["viewport"] >= state["height"] - 2
        if not bottom and state["ready"] != "loading":
            if scrolls >= opts.max_scrolls:
                raise FetchError("动态页面达到滚动上限，未确认内容完整；请增大--max-scrolls")
            scroll = (
                "window.scrollTo(0, document.scrollingElement.scrollHeight)"
                if content == "text"
                else "window.scrollBy(0, Math.max(1, innerHeight * 0.8))"
            )
            await _evaluate(cdp, session, scroll)
            scrolls += 1
            stable = now
        elif bottom and visible and now - max(stable, network.last_activity) >= opts.settle:
            return
        await asyncio.sleep(0.2)


async def render_page(
    page: Response,
    client: AsyncFetcher,
    options: RenderOptions,
    *,
    content: Literal["images", "text"] = "images",
) -> tuple[Response, tuple[str, ...]]:
    executable = find_chrome(options.executable)
    if executable is None:
        raise UnsupportedError(
            "动态采集需要系统Chrome/Edge/Brave/Chromium", hint="安装Chrome或指定--chrome"
        )
    try:
        async with asyncio.timeout(options.timeout), ChromeProcess(executable) as chrome:
            async with Cdp(chrome.endpoint) as cdp:
                await cdp.call("Browser.setDownloadBehavior", {"behavior": "deny"})
                target = await cdp.call("Target.createTarget", {"url": "about:blank"})
                attached = await cdp.call(
                    "Target.attachToTarget", {"targetId": target["targetId"], "flatten": True}
                )
                session = attached["sessionId"]
                await cdp.call("Page.enable", session_id=session)
                await cdp.call("Network.enable", session_id=session)
                await cdp.call(
                    "Network.setBypassServiceWorker", {"bypass": True}, session_id=session
                )
                await cdp.call(
                    "Network.setCacheDisabled", {"cacheDisabled": True}, session_id=session
                )
                await cdp.call(
                    "Network.setUserAgentOverride", {"userAgent": DEFAULT_UA}, session_id=session
                )
                tree = await cdp.call("Page.getFrameTree", session_id=session)
                async with BrowserNetwork(
                    cdp, session, tree["frameTree"]["frame"]["id"], client, page
                ) as network:
                    navigation = await cdp.call(
                        "Page.navigate", {"url": page.url}, session_id=session
                    )
                    network.check()
                    if navigation.get("errorText"):
                        raise FetchError("动态页面导航失败")
                    await _scroll(cdp, session, network, options, content)
                    snapshot = await _evaluate(cdp, session, _DOM)
                    network.check()
                    if not isinstance(snapshot, dict) or not is_usable_url(snapshot.get("url", "")):
                        raise FetchError("动态页面返回了无效DOM")
                    encoded = snapshot["html"].encode("utf-8")
                    if len(encoded) > client.max_bytes:
                        raise FetchError("动态页面DOM超过大小限制")
                    response = Response(
                        snapshot["url"],
                        200,
                        {"content-type": "text/html; charset=utf-8"},
                        encoded,
                        0,
                    )
                    return response, tuple(sorted(network.warnings))
    except TimeoutError:
        raise FetchError("动态渲染超时，未确认内容完整；可增大--render-timeout后重试") from None
