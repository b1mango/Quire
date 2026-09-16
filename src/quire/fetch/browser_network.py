"""Route isolated Chrome requests through the public HTTP policy and limits."""

from __future__ import annotations

import asyncio
import base64
from typing import Any

from ..errors import FetchError, QuireError
from .browser_cdp import Cdp
from .session import AsyncFetcher
from .simple import Response


class BrowserNetwork:
    def __init__(self, cdp: Cdp, session: str, frame: str, client: AsyncFetcher, page: Response):
        self.cdp, self.session, self.frame, self.client = cdp, session, frame, client
        self.seed = {page.url: page}
        self.pending: set[asyncio.Task[None]] = set()
        self.error: BaseException | None = None
        self.warnings: set[str] = set()
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
        try:
            if (
                request["method"] != "GET"
                or not url.startswith(("http://", "https://"))
                or kind in {"WebSocket", "EventSource"}
                or kind == "Document"
                and not main
            ):
                raise FetchError("动态页面包含不支持的请求或内嵌文档")
            response = self.seed.pop(url, None)
            if response is None:
                headers = request.get("headers", {})
                referer = headers.get("Referer") or headers.get("referer")
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
            await self.cdp.call(
                "Fetch.fulfillRequest",
                {"requestId": params["requestId"], **payload},
                session_id=self.session,
            )
        except QuireError as exc:
            if (
                main
                or kind in {"Script", "Fetch", "XHR", "Stylesheet"}
                or self.bytes > 128 * 1024 * 1024
            ):
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
                self.error = failure
        except Exception:
            self.error = FetchError("动态页面资源处理失败")
        finally:
            self.last_activity = asyncio.get_running_loop().time()
