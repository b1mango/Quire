"""Bounded DOM rendering in an isolated system Chrome, with explicit scrolling."""

from __future__ import annotations

import asyncio
import math
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from ..errors import BlockedError, ConfigError, FetchError, QuireError, UnsupportedError
from ..utils.urls import is_usable_url
from .browser_catalogue import ready_script
from .browser_cdp import Cdp
from .browser_endpoint import resolve_endpoint, validate_endpoint
from .browser_native import NativeNetwork, owned_tab
from .browser_network import BrowserNetwork
from .browser_process import ChromeProcess, find_chrome
from .session import AsyncFetcher
from .simple import DEFAULT_UA, Response

_STATE = """(() => {
 const root = document.scrollingElement;
 return {url: location.href, ready: document.readyState, y: scrollY,
 challenge: !!document.querySelector("#challenge-running,#cf-challenge-running") ||
 /^(Just a moment|Attention Required)/i.test(document.title),
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
    cdp_endpoint: str = ""
    native: bool = False
    timeout: float = 30
    max_scrolls: int = 100
    settle: float = 1.0
    referer: str = ""

    def __post_init__(self) -> None:
        if type(self.native) is not bool or not isinstance(self.cdp_endpoint, str):
            raise ConfigError("浏览器模式参数类型错误")
        if self.cdp_endpoint:
            validate_endpoint(self.cdp_endpoint)
        if self.referer:
            parts = urlsplit(self.referer)
            if (
                parts.scheme not in {"http", "https"}
                or not parts.hostname
                or not is_usable_url(self.referer)
            ):
                raise ConfigError("渲染 Referer 须为无凭据 HTTP(S) 地址")
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


def _navigate_params(url: str, options: RenderOptions) -> dict[str, str]:
    # Referer 门控的站点（如正文域校验来源页）需要把 --referer 带进浏览器导航。
    params = {"url": url}
    if options.referer:
        params["referrer"] = options.referer
    return params


def _challenge_dom(html: str) -> bool:
    """渲染结果仍是 Cloudflare 质询页（不能当正文交付）。"""
    lowered = html.lower()
    return any(
        marker in lowered
        for marker in (
            'id="challenge-running"',
            'id="cf-challenge-running"',
            "<title>just a moment",
            "<title>attention required",
        )
    )


async def _harvest_clearance(
    cdp: Cdp, session: str, url: str, client: AsyncFetcher, warnings: set[str]
) -> None:
    """原生渲染通过后回收 cf_clearance 与一致的 UA，同站后续请求走快速 HTTP 通道。

    回收失败不拖垮已成功的渲染，但会在报告警告里如实可见；Cookie 只进内存会话。
    """
    try:
        cookies = await cdp.call("Network.getCookies", {"urls": [url]}, session_id=session)
        clearance = next(
            (c["value"] for c in cookies.get("cookies", []) if c.get("name") == "cf_clearance"),
            None,
        )
        if not clearance:
            return
        agent = await _evaluate(cdp, session, "navigator.userAgent")
        if not isinstance(agent, str) or not agent:
            raise FetchError("无法读取真实浏览器的 User-Agent")
        client.set_clearance(url, cookie=clearance, user_agent=agent)
    except QuireError:
        warnings.add("cf_clearance 回收失败；同站后续请求仍走浏览器通道")


async def _scroll(
    cdp: Cdp,
    session: str,
    network: BrowserNetwork | NativeNetwork,
    opts: RenderOptions,
    content: Literal["images", "text"] = "images",
) -> None:
    previous: tuple[object, ...] | None = None
    stable = asyncio.get_running_loop().time()
    started = stable
    scrolls = 0
    while True:
        network.check()
        script = network.ready_script
        if isinstance(script, str) and script and await _evaluate(cdp, session, script):
            return
        state = await _evaluate(cdp, session, _STATE)
        if not isinstance(state, dict) or not is_usable_url(state.get("url", "")):
            raise FetchError("动态页面导航到了不支持的地址")
        if isinstance(network, NativeNetwork) and state.get("challenge"):
            await asyncio.sleep(0.2)
            continue
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
        elif (
            bottom
            and visible
            and now - max(stable, network.last_activity) >= opts.settle
            and (not isinstance(network, NativeNetwork) or now - started >= 2)
        ):
            return
        await asyncio.sleep(0.2)


async def render_page(
    page: Response,
    client: AsyncFetcher,
    options: RenderOptions,
    *,
    content: Literal["images", "text"] = "images",
) -> tuple[Response, tuple[str, ...]]:
    if options.native or options.cdp_endpoint:
        return await _render_native(page, client, options, content=content)
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
                    network.ready_script = ready_script(page.url)
                    snapshot: Any = None
                    for retried in (False, True):
                        navigation = await cdp.call(
                            "Page.navigate", _navigate_params(page.url, options), session_id=session
                        )
                        network.check()
                        if navigation.get("errorText"):
                            raise FetchError("动态页面导航失败")
                        await _scroll(cdp, session, network, options, content)
                        snapshot = await _evaluate(cdp, session, _DOM)
                        network.check()
                        if not isinstance(snapshot, dict) or not is_usable_url(
                            snapshot.get("url", "")
                        ):
                            raise FetchError("动态页面返回了无效DOM")
                        if not _challenge_dom(snapshot["html"]):
                            break
                        # Cloudflare 质询页：走真实 Chrome 升级通道过质询并回注
                        # cf_clearance，然后带凭据重渲染一次；没有升级通道或重试
                        # 仍不过就如实失败，绝不拿质询页冒充正文。
                        if retried or client.escalation is None:
                            raise BlockedError(
                                "Cloudflare 验证未通过；隔离 Chrome 无法完成质询",
                                hint="本机有可用 Chrome 时会自动升级；也可配置 --cdp-endpoint 后重试",
                            )
                        await client.escalation(page.url)
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


async def _render_native(
    page: Response,
    client: AsyncFetcher,
    options: RenderOptions,
    *,
    content: Literal["images", "text"],
) -> tuple[Response, tuple[str, ...]]:
    if not is_usable_url(page.url):
        raise ConfigError("真实浏览器仅接受无凭据 HTTP(S) 地址")
    try:
        async with asyncio.timeout(options.timeout), AsyncExitStack() as stack:
            if client.respect_robots:
                await client.robots.check(page.url)
            async with client.limiter.admit(page.url, asyncio.Semaphore(1), options.timeout):
                pass
            if options.cdp_endpoint:
                endpoint = await resolve_endpoint(options.cdp_endpoint)
            else:
                executable = find_chrome(options.executable)
                if executable is None:
                    raise UnsupportedError("真实浏览器渲染需要系统 Chrome")
                chrome = await stack.enter_async_context(
                    ChromeProcess(
                        executable,
                        extra_args=("--no-proxy-server",),
                    )
                )
                endpoint = chrome.endpoint
            cdp = await stack.enter_async_context(Cdp(endpoint))
            session = await stack.enter_async_context(owned_tab(cdp))
            await cdp.call("Page.enable", session_id=session)
            await cdp.call("Network.enable", session_id=session)
            await cdp.call("Network.setBypassServiceWorker", {"bypass": True}, session_id=session)
            tree = await cdp.call("Page.getFrameTree", session_id=session)
            async with NativeNetwork(
                cdp, session, tree["frameTree"]["frame"]["id"], page, client.max_bytes
            ) as network:
                network.ready_script = ready_script(page.url)
                navigation = await cdp.call(
                    "Page.navigate", _navigate_params(page.url, options), session_id=session
                )
                if navigation.get("errorText"):
                    raise FetchError("真实浏览器导航失败")
                await _scroll(cdp, session, network, options, content)
                snapshot = await _evaluate(cdp, session, _DOM)
                network.check()
                if not isinstance(snapshot, dict) or not is_usable_url(snapshot.get("url", "")):
                    raise FetchError("真实浏览器返回无效 DOM")

                if urlsplit(snapshot["url"]).hostname not in network.hosts:
                    raise FetchError("真实浏览器导航超出站点范围")
                network.validate_status(snapshot["url"])
                html = snapshot["html"]
                if _challenge_dom(html):
                    raise BlockedError("Cloudflare 验证尚未完成；请在 Chrome 完成验证后重试")
                await _harvest_clearance(cdp, session, snapshot["url"], client, network.warnings)
                encoded = html.encode("utf-8")
                if len(encoded) > client.max_bytes:
                    raise FetchError("动态页面DOM超过大小限制")
                return Response(
                    snapshot["url"],
                    network.status,
                    {"content-type": "text/html; charset=utf-8"},
                    encoded,
                    0,
                ), tuple(sorted(network.warnings))
    except TimeoutError:
        raise FetchError("真实浏览器渲染超时；登录或人工验证可能尚未完成") from None


async def fetch_render_input(
    client: AsyncFetcher,
    url: str,
    options: RenderOptions | None,
    *,
    referer: str | None = None,
) -> Response:
    """Native mode must reach Chrome even when an HTTP client would get a 403."""
    if options and (options.native or options.cdp_endpoint):
        if not is_usable_url(url):
            raise ConfigError("浏览器仅接受无凭据 HTTP(S) 地址")
        return Response(url, 200, {}, b"", 0)
    return await client.get(url, referer=referer)
