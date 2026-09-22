"""Native Chrome networking; credentials stay in Chrome and only owned tabs close."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

from ..errors import BlockedError, FetchError, HttpStatusError, QuireError
from ..utils.urls import is_usable_url
from .browser_cdp import Cdp
from .browser_network import _interception_gone
from .simple import Response


def allowed_hosts(url: str) -> frozenset[str]:
    host = urlsplit(url).hostname or ""
    hosts = {host, "challenges.cloudflare.com"}
    if host == "shuqi.com" or host.endswith(".shuqi.com"):
        hosts.update(
            {
                "shuqi.com",
                "www.shuqi.com",
                "m.shuqi.com",
                "t.shuqi.com",
                "ocean.shuqireader.com",
                "c.shuqireader.com",
            }
        )
    return frozenset(hosts)


async def _finish(task: asyncio.Task[Any]) -> Any:
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


@asynccontextmanager
async def owned_tab(cdp: Cdp) -> AsyncIterator[str]:
    target: str | None = None

    async def create_target() -> None:
        nonlocal target
        async with asyncio.timeout(10):
            reply = await cdp.call(
                "Target.createTarget", {"url": "about:blank", "background": True}
            )
            target = reply["targetId"]

    try:
        # Keep waiting for ownership information even during repeated cancellation.
        await _finish(asyncio.create_task(create_target()))
        attached = await cdp.call("Target.attachToTarget", {"targetId": target, "flatten": True})
        yield attached["sessionId"]
    finally:
        if target is not None:

            async def cleanup() -> None:
                try:
                    async with asyncio.timeout(3):
                        reply = await cdp.call("Target.closeTarget", {"targetId": target})
                        if reply.get("success") is False:
                            raise FetchError("Chrome refused target cleanup")
                except (QuireError, TimeoutError):
                    raise FetchError("CDP 标签页清理失败；请手动关闭 Quire 新建的标签页") from None

            await _finish(asyncio.create_task(cleanup()))


class NativeNetwork:
    def __init__(self, cdp: Cdp, session: str, frame: str, page: Response, max_bytes: int):
        self.cdp, self.session, self.frame = cdp, session, frame
        self.hosts = allowed_hosts(page.url)
        self.pending: set[str] = set()
        self.warnings: set[str] = set()
        self.catalogue_script: str | None = None
        self.last_activity = asyncio.get_running_loop().time()
        self.status = 0
        self.error: BaseException | None = None
        self.task: asyncio.Task[None] | None = None
        self.count = self.size = 0
        self.max_bytes = max_bytes

    async def __aenter__(self) -> NativeNetwork:
        await self.cdp.call(
            "Fetch.enable", {"patterns": [{"urlPattern": "*"}]}, session_id=self.session
        )
        self.task = asyncio.create_task(self._events())
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    def check(self) -> None:
        if self.error:
            raise self.error
        if self.task and self.task.done():
            self.task.result()

    async def _events(self) -> None:
        while True:
            event = await self.cdp.events.get()
            if event.get("sessionId") != self.session:
                continue
            params, method = event.get("params", {}), event.get("method")
            if method == "Fetch.requestPaused":
                url = params["request"]["url"]
                self.count += 1
                if self.count > 1024:
                    raise FetchError("真实浏览器资源数量超过限制")
                allowed = is_usable_url(url) and urlsplit(url).hostname in self.hosts
                if not allowed:
                    self.warnings.add("已阻止未授权站点的浏览器请求；部分资源可能缺失")
                    if (
                        params.get("resourceType") == "Document"
                        and params.get("frameId") == self.frame
                    ):
                        self.error = BlockedError("真实浏览器跨站导航已阻止")
                await self._continue(
                    "Fetch.continueRequest" if allowed else "Fetch.failRequest",
                    {
                        "requestId": params["requestId"],
                        **({} if allowed else {"errorReason": "BlockedByClient"}),
                    },
                    session_id=self.session,
                )
            elif method == "Network.requestWillBeSent":
                self.pending.add(params["requestId"])
                self.last_activity = asyncio.get_running_loop().time()
            elif method in {"Network.loadingFinished", "Network.loadingFailed"}:
                self.pending.discard(params["requestId"])
                self.last_activity = asyncio.get_running_loop().time()
            elif method == "Network.dataReceived":
                self.size += params.get("dataLength", 0)
                if self.size > self.max_bytes * 8:
                    raise FetchError("真实浏览器资源体积超过限制")
            elif method == "Network.responseReceived":
                if params.get("type") == "Document" and params.get("frameId") == self.frame:
                    self.status = int(params["response"]["status"])
            elif method == "Page.javascriptDialogOpening":
                await self.cdp.call(
                    "Page.handleJavaScriptDialog", {"accept": False}, session_id=self.session
                )

    async def _continue(self, method: str, params: dict[str, Any], *, session_id: str) -> None:
        try:
            async with asyncio.timeout(5):
                await self.cdp.call(method, params, session_id=session_id)
        except QuireError as exc:
            if not _interception_gone(exc):
                raise
        except TimeoutError:
            raise FetchError("Chrome 请求拦截应答超时") from None

    def validate_status(self, url: str) -> None:
        if self.status in {401, 403, 429}:
            raise BlockedError("浏览器仍被站点拒绝或需要登录/验证，未取得正文")
        if not 200 <= self.status < 300:
            raise HttpStatusError(url, self.status)
