"""Route isolated Chrome requests through the public HTTP policy and limits."""

from __future__ import annotations

import asyncio
import base64
from typing import Any
from urllib.parse import urlsplit

from ..errors import BlockedError, FetchError, HttpStatusError, QuireError
from ..utils.urls import normalize_url
from .browser_cdp import Cdp
from .session import AsyncFetcher
from .simple import Response


def _interception_gone(exc: QuireError) -> bool:
    # 页面在我们应答前取消了请求(广告/统计脚本常见),拦截点已失效,无需再应答。
    return isinstance(exc, FetchError) and "Invalid InterceptionId" in exc.message


def _site(host: str | None) -> str:
    # 近似可注册域(末两段):判断子请求是否与主页面同站。
    parts = (host or "").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else (host or "")


class BrowserNetwork:
    def __init__(self, cdp: Cdp, session: str, frame: str, client: AsyncFetcher, page: Response):
        self.cdp, self.session, self.frame, self.client = cdp, session, frame, client
        from dataclasses import replace

        # HTTP 请求没有 fragment；浏览器导航仍保留原 page.url 的 hash 路由。
        self.seed = {normalize_url(page.url): replace(page, url=normalize_url(page.url))}
        self.site = _site(urlsplit(page.url).hostname)
        self.pending: set[asyncio.Task[None]] = set()
        self.error: BaseException | None = None
        self.warnings: set[str] = set()
        self.ready_script: str | None = None
        self.count = 0
        self.bytes = 0
        self.last_activity = asyncio.get_running_loop().time()
        self._pump: asyncio.Task[None] | None = None

    async def __aenter__(self) -> BrowserNetwork:
        await self.cdp.call(
            "Fetch.enable", {"patterns": [{"urlPattern": "*"}]}, session_id=self.session
        )
        self._pump = asyncio.create_task(self._events())
        return self

    async def __aexit__(self, *exc: object) -> None:
        tasks = [*self.pending]
        if self._pump is not None:
            tasks.append(self._pump)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def check(self) -> None:
        if self.error is not None:
            raise self.error
        if self._pump is not None and self._pump.done():
            self._pump.result()

    async def _events(self) -> None:
        while True:
            event = await self.cdp.events.get()
            if event.get("sessionId") != self.session:
                continue
            if event.get("method") == "Page.javascriptDialogOpening":
                await self.cdp.call(
                    "Page.handleJavaScriptDialog", {"accept": False}, session_id=self.session
                )
            if event.get("method") != "Fetch.requestPaused":
                continue
            self.count += 1
            self.last_activity = asyncio.get_running_loop().time()
            if self.count > 512 or len(self.pending) >= 64:
                self.error = FetchError("动态页面资源数量超过限制")
                return
            task = asyncio.create_task(self._serve(event["params"]))
            self.pending.add(task)
            task.add_done_callback(self.pending.discard)

    async def _serve(self, params: dict[str, Any]) -> None:
        request = params["request"]
        kind, url = params.get("resourceType"), request["url"]
        main = kind == "Document" and params.get("frameId") == self.frame
        # 页面自发的 XHR POST(如 bilibili 的 twirp API)带 postData 时可代理;
        # 缺 body 或超大时仍按不支持处理。
        post_data = request.get("postData")
        proxyable_post = (
            request["method"] == "POST"
            and kind in {"Fetch", "XHR"}
            and isinstance(post_data, str)
            and len(post_data) <= 2 * 1024 * 1024
        )
        preflight = request["method"] == "OPTIONS" and kind in {
            "Fetch",
            "XHR",
            "Other",
            "Preflight",
        }
        unsupported = (
            request["method"] != "GET"
            and not proxyable_post
            and not preflight
            or not url.startswith(("http://", "https://"))
            or kind in {"WebSocket", "EventSource"}
            or kind == "Document"
            and not main
        )
        if unsupported:
            # 页面自己发的 POST/WebSocket/子框架代理不了:拒掉这一条、继续渲染,
            # 只有主文档本身不支持才让整个渲染失败。
            if main:
                self.error = FetchError("动态页面包含不支持的请求或内嵌文档")
            else:
                self.warnings.add("动态页面发起了无法代理的请求(POST/WebSocket 等),已跳过")
            try:
                await self.cdp.call(
                    "Fetch.failRequest",
                    {"requestId": params["requestId"], "errorReason": "BlockedByClient"},
                    session_id=self.session,
                )
            except QuireError as failure:
                if not _interception_gone(failure):
                    self.error = failure
            finally:
                self.last_activity = asyncio.get_running_loop().time()
            return
        try:
            response = self.seed.pop(url, None) if request["method"] == "GET" else None
            if response is None:
                headers = request.get("headers", {})
                referer = headers.get("Referer") or headers.get("referer")
                if (
                    self.site == "shuqi.com"
                    and urlsplit(url).hostname == "ocean.shuqireader.com"
                    and request["method"] in {"GET", "POST"}
                ):
                    response = await self.client.shuqi_request(
                        url,
                        method=request["method"],
                        headers=headers,
                        body=post_data.encode("utf-8") if isinstance(post_data, str) else None,
                    )
                elif preflight:
                    response = await self.client.preflight(url, headers)
                elif proxyable_post:
                    assert isinstance(post_data, str)
                    content_type = (
                        headers.get("Content-Type")
                        or headers.get("content-type")
                        or "application/octet-stream"
                    )
                    response = await self.client.post(
                        url,
                        body=post_data.encode("utf-8"),
                        content_type=content_type,
                        referer=referer,
                    )
                else:
                    response = await self.client.get(url, referer=referer)
            self.bytes += len(response.content)
            if self.bytes > 128 * 1024 * 1024:
                raise FetchError("动态页面总资源超过128 MiB限制")
            if response.url != url:
                self.seed[response.url] = response
                payload = {
                    "responseCode": 302,
                    "responseHeaders": [{"name": "location", "value": response.url}],
                    "body": "",
                }
            else:
                # Bodies are decoded by AsyncFetcher; cookies and transport framing never pass.
                allowed = {
                    "content-type",
                    "content-security-policy",
                    "access-control-allow-origin",
                    "access-control-allow-headers",
                    "access-control-allow-methods",
                    "access-control-allow-credentials",
                    "access-control-max-age",
                    "x-content-type-options",
                }
                payload = {
                    "responseCode": response.status,
                    "responseHeaders": [
                        {"name": key, "value": value}
                        for key, value in response.headers.items()
                        if key.lower() in allowed
                    ],
                    "body": base64.b64encode(response.content).decode("ascii"),
                }
            try:
                await self.cdp.call(
                    "Fetch.fulfillRequest",
                    {"requestId": params["requestId"], **payload},
                    session_id=self.session,
                )
            except FetchError as exc:
                if _interception_gone(exc) and not main:
                    self.warnings.add("动态页面在应答前取消了部分请求,已跳过")
                    return
                raise
        except QuireError as exc:
            same_site = _site(urlsplit(url).hostname) == self.site
            dependency = kind in {"Script", "Fetch", "XHR", "Stylesheet"}
            if (
                isinstance(exc, BlockedError)
                and not main
                and not (same_site and kind in {"Script", "Stylesheet"})
            ):
                # 第三方/接口子请求被 robots 拦下:拒掉这一条、继续渲染,
                # 主文档与同站脚本样式被拦仍失败(正文可能不完整)。
                self.warnings.add(
                    f"动态页面部分资源被 robots.txt 拦截,已跳过：{urlsplit(url).netloc}"
                )
            elif isinstance(exc, HttpStatusError) and kind in {"Fetch", "XHR"}:
                # 页面自发的接口调用未登录/被拒(如 bilibili GetInitInfo 401):
                # 拒掉这一条继续渲染;正文接口若也失败,占位页会在下游被查出。
                self.warnings.add(f"动态页面接口请求被拒绝了(HTTP {exc.status}),已跳过")
            elif main or (dependency and same_site) or self.bytes > 128 * 1024 * 1024:
                self.error = exc
            else:
                self.warnings.add(f"动态页面部分资源未加载：{type(exc).__name__}")
            try:
                await self.cdp.call(
                    "Fetch.failRequest",
                    {"requestId": params["requestId"], "errorReason": "BlockedByClient"},
                    session_id=self.session,
                )
            except QuireError as failure:
                if not _interception_gone(failure):
                    self.error = failure
        except Exception:
            self.error = FetchError("动态页面资源处理失败")
        finally:
            self.last_activity = asyncio.get_running_loop().time()
