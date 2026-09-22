"""Cloudflare 盾自动升级：真实 Chrome 完成 JS 质询，回收 cf_clearance 到 HTTP 会话。

触发点：``AsyncFetcher.get`` 在 403 且 ``simple.cloudflare_block`` 命中时调用
``Escalation``（见 fetch/session.py）。本模块用真实 Chrome（用户配置的
cdp_endpoint，或本机隔离 Chrome 直连）执行质询页脚本，通过后取回
cf_clearance 与一致的 User-Agent 回注会话，同站后续请求走快速 HTTP 通道。

边界（与方案B一致）：CDP 仅回环；只清理自建标签页；cf_clearance 只进内存
Cookie 罐，不写日志/报告/仓库。质询需人工（验证码）、超时未通过、或 IP 级
硬封（如 "Sorry, you have been blocked"）时如实 BlockedError，不伪成功。
"""

from __future__ import annotations

import asyncio
import logging
import math
from contextlib import AsyncExitStack
from urllib.parse import urlsplit

from ..errors import BlockedError, ConfigError, FetchError
from ..utils.urls import is_usable_url, redact
from .browser import RenderOptions, _evaluate
from .browser_cdp import Cdp
from .browser_endpoint import resolve_endpoint
from .browser_native import NativeNetwork, owned_tab
from .browser_process import ChromeProcess, find_chrome
from .session import AsyncFetcher
from .simple import Response

_LOG = logging.getLogger(__name__)

#: 质询页状态：challenge 为真表示仍在验证；hardBlock 是 IP 级封禁文案。
_STATE = """(() => {
 const text = document.body ? document.body.innerText.slice(0, 4000) : "";
 return {url: location.href, ready: document.readyState,
 challenge: !!document.querySelector("#challenge-running,#cf-challenge-running") ||
 /^(Just a moment|Attention Required)/i.test(document.title),
 hardBlock: /sorry, you have been blocked|access denied|error 1020/i.test(text)};
})()"""


async def solve_clearance(
    client: AsyncFetcher,
    url: str,
    *,
    cdp_endpoint: str = "",
    executable: str | None = None,
    timeout: float = 45.0,
) -> tuple[str, str]:
    """用真实 Chrome 完成 Cloudflare JS 质询，返回 (cf_clearance, User-Agent)。"""
    if not is_usable_url(url):
        raise ConfigError("浏览器仅接受无凭据 HTTP(S) 地址")
    if not math.isfinite(timeout) or not 5 <= timeout <= 300:
        raise ConfigError("Cloudflare 升级超时须为 5-300 秒")
    try:
        async with asyncio.timeout(timeout), AsyncExitStack() as stack:
            if cdp_endpoint:
                endpoint = await resolve_endpoint(cdp_endpoint)
            else:
                found = find_chrome(executable)
                if found is None:
                    raise BlockedError(
                        f"HTTP 403: {redact(url)}",
                        hint="站点启用了 Cloudflare 防护；本机未找到可用 Chrome，"
                        "无法自动完成验证，可安装 Chrome 或更换网络环境后重试。",
                    )
                chrome = await stack.enter_async_context(
                    ChromeProcess(found, extra_args=("--no-proxy-server",))
                )
                endpoint = chrome.endpoint
            cdp = await stack.enter_async_context(Cdp(endpoint))
            session = await stack.enter_async_context(owned_tab(cdp))
            await cdp.call("Page.enable", session_id=session)
            await cdp.call("Network.enable", session_id=session)
            tree = await cdp.call("Page.getFrameTree", session_id=session)
            page = Response(url, 200, {}, b"", 0)
            host = urlsplit(url).hostname
            async with NativeNetwork(
                cdp, session, tree["frameTree"]["frame"]["id"], page, client.max_bytes
            ) as network:
                navigation = await cdp.call("Page.navigate", {"url": url}, session_id=session)
                if navigation.get("errorText"):
                    raise FetchError("真实浏览器导航失败")
                while True:
                    network.check()
                    try:
                        state = await _evaluate(cdp, session, _STATE)
                    except FetchError:
                        # 导航/重载途中执行上下文短暂不可用，继续轮询直到超时。
                        await asyncio.sleep(0.25)
                        continue
                    if isinstance(state, dict) and state.get("hardBlock"):
                        raise BlockedError(
                            f"站点对当前网络环境硬封禁（IP 级）：{redact(url)}",
                            hint="浏览器访问同样被拒，只能更换网络环境后重试。",
                        )
                    if (
                        isinstance(state, dict)
                        and not state.get("challenge")
                        and state.get("ready") == "complete"
                        and urlsplit(str(state.get("url", ""))).hostname == host
                    ):
                        break
                    await asyncio.sleep(0.25)
                network.validate_status(url)
                cookies = await cdp.call("Network.getCookies", {"urls": [url]}, session_id=session)
                clearance = next(
                    (
                        c["value"]
                        for c in cookies.get("cookies", [])
                        if c.get("name") == "cf_clearance" and c.get("value")
                    ),
                    None,
                )
                if not isinstance(clearance, str):
                    raise BlockedError(
                        f"Cloudflare 验证已通过但未发放 cf_clearance：{redact(url)}",
                        hint="无法复用到 HTTP 通道；可用 --render --browser-native 全程走浏览器。",
                    )
                agent = await _evaluate(cdp, session, "navigator.userAgent")
                if not isinstance(agent, str) or not agent:
                    raise FetchError("无法读取真实浏览器的 User-Agent")
                return clearance, agent
    except TimeoutError:
        raise BlockedError(
            f"Cloudflare 质询在 {timeout:.0f} 秒内未通过：{redact(url)}",
            hint="可能需要人工完成验证（验证码），或站点拒绝了自动浏览器；可稍后再试。",
        ) from None


class ClearanceEscalation:
    """按站串行的升级入口：同站并发 403 只启动一次 Chrome；失败按站缓存不重试。"""

    def __init__(
        self, client: AsyncFetcher, render: RenderOptions | None = None, *, timeout: float = 45.0
    ) -> None:
        self._client = client
        self._endpoint = render.cdp_endpoint if render else ""
        self._executable = render.executable if render else None
        self._timeout = render.timeout if render else timeout
        self._locks: dict[str, asyncio.Lock] = {}
        self._solves: dict[str, int] = {}
        self._failures: dict[str, BlockedError] = {}

    async def __call__(self, url: str) -> None:
        host = urlsplit(url).hostname or ""
        if host in self._failures:
            raise self._failures[host]
        before = self._solves.get(host, 0)
        async with self._locks.setdefault(host, asyncio.Lock()):
            if host in self._failures:
                raise self._failures[host]
            if self._solves.get(host, 0) > before:
                return  # 等待期间已有并发请求完成验证，直接用回注的凭据重试
            _LOG.info("Cloudflare 拦截，正在用真实 Chrome 完成验证：%s", host)
            try:
                cookie, agent = await solve_clearance(
                    self._client,
                    url,
                    cdp_endpoint=self._endpoint,
                    executable=self._executable,
                    timeout=self._timeout,
                )
            except BlockedError as exc:
                self._failures[host] = exc
                _LOG.warning("Cloudflare 自动验证失败：%s（%s）", host, exc.message)
                raise
            self._client.set_clearance(url, cookie=cookie, user_agent=agent)
            self._solves[host] = before + 1
            _LOG.info("Cloudflare 验证已通过，cf_clearance 已回注会话：%s", host)

    def reject(self, url: str) -> None:
        """回注后仍被 403：cf_clearance 未被接受，标记该站升级无效，不再重复启动。"""
        host = urlsplit(url).hostname or ""
        self._failures[host] = BlockedError(
            f"HTTP 403: {redact(url)}",
            hint="cf_clearance 未被站点接受（可能已过期或与网络环境绑定失败）；"
            "可更换网络环境，或改用 --render --browser-native 全程走浏览器。",
        )
