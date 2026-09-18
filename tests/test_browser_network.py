from __future__ import annotations

import asyncio
import base64
from unittest.mock import AsyncMock

import pytest

from quire.errors import BlockedError, FetchError, HttpStatusError
from quire.fetch.browser_network import BrowserNetwork
from quire.fetch.simple import Response

URL = "https://example.test/page"


def response(url=URL, body=b"page"):
    return Response(url, 200, {"content-type": "text/html", "set-cookie": "secret=yes"}, body, 0)


def network():
    cdp = AsyncMock()
    cdp.events = asyncio.Queue()
    client = AsyncMock()
    client.get.return_value = response(URL + "/image")
    return BrowserNetwork(cdp, "session", "frame", client, response())


def request(url=URL, kind="Document", **fields):
    return {
        "requestId": "r1",
        "resourceType": kind,
        "frameId": "frame",
        "request": {"url": url, "method": "GET", **fields},
    }


def test_initial_response_is_reused_and_transport_cookie_headers_excluded():
    async def run():
        net = network()
        await net._serve(request())
        net.client.get.assert_not_called()
        method, payload = net.cdp.call.call_args.args
        assert method == "Fetch.fulfillRequest"
        assert base64.b64decode(payload["body"]) == b"page"
        assert payload["responseHeaders"] == [{"name": "content-type", "value": "text/html"}]
        assert not net.seed

    asyncio.run(run())


def test_resource_requests_use_public_fetcher_and_redirect_seed():
    async def run():
        net = network()
        await net._serve(
            request(URL + "/old", "Image", headers={"Referer": URL, "Cookie": "secret"})
        )
        net.client.get.assert_awaited_once_with(URL + "/old", referer=URL)
        assert net.cdp.call.call_args.args[1]["responseCode"] == 302
        await net._serve(request(URL + "/image", "Image"))
        assert net.client.get.await_count == 1
        assert net.cdp.call.call_args.args[1]["responseCode"] == 200

    asyncio.run(run())


@pytest.mark.parametrize(
    "kind,url,method,frame",
    [
        ("Document", URL, "POST", "frame"),
        ("Document", "file:///secret", "GET", "frame"),
        ("Document", URL, "GET", "subframe"),
        ("EventSource", URL, "GET", "frame"),
        ("WebSocket", URL, "GET", "frame"),
        ("XHR", URL, "POST", "frame"),  # 页面自带的 POST API:拒掉继续,不致命
    ],
)
def test_unsupported_requests_are_aborted(kind, url, method, frame):
    async def run():
        net = network()
        await net._serve({**request(url, kind, method=method), "frameId": frame})
        net.client.get.assert_not_called()
        assert net.cdp.call.call_args.args[0] == "Fetch.failRequest"
        if kind == "Document" and frame == "frame":
            with pytest.raises(FetchError):
                net.check()
        else:
            net.check()  # 非主文档的不支持请求只是警告
            assert net.warnings

    asyncio.run(run())


def test_policy_failure_is_fatal_for_main_and_reported_for_subresource():
    async def run():
        net = network()
        net.client.get.side_effect = BlockedError("robots denied")
        await net._serve(request(URL + "/image", "Image"))
        net.check()
        assert net.warnings == {"动态页面部分资源被 robots.txt 拦截,已跳过：example.test"}
        await net._serve(request(URL + "/blocked"))
        with pytest.raises(BlockedError):
            net.check()

    asyncio.run(run())


def test_robots_blocked_page_api_calls_do_not_stop_capture():
    # bilibili 漫画页自发调用 api.bilibili.com(Disallow: /):拒掉继续渲染。
    async def run():
        net = network()
        net.client.get.side_effect = BlockedError("robots denied")
        await net._serve(request("https://api.bilibili.com/x/web-interface/nav", "XHR"))
        net.check()
        assert net.cdp.call.call_args.args[0] == "Fetch.failRequest"
        assert net.warnings == {"动态页面部分资源被 robots.txt 拦截,已跳过：api.bilibili.com"}

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["Script", "Fetch", "XHR", "Stylesheet"])
def test_failed_page_dependencies_stop_capture(kind):
    async def run():
        net = network()
        before = net.last_activity
        await asyncio.sleep(0)
        net.client.get.side_effect = FetchError("next page failed")
        await net._serve(request(URL + "/next", kind))
        assert net.last_activity > before
        with pytest.raises(FetchError, match="next page"):
            net.check()

    asyncio.run(run())


def test_total_size_and_failed_abort_are_fatal():
    async def run():
        net = network()
        net.bytes = 128 * 1024 * 1024
        await net._serve(request())
        with pytest.raises(FetchError, match="128"):
            net.check()
        net = network()
        net.cdp.call.side_effect = FetchError("disconnected")
        await net._serve(request())
        with pytest.raises(FetchError, match="disconnected"):
            net.check()
        net = network()
        net.client.get.side_effect = ValueError("invalid")
        await net._serve(request(URL + "/script", "Script"))
        with pytest.raises(FetchError, match="处理失败"):
            net.check()

    asyncio.run(run())


def test_event_dispatch_dialogs_limits_and_cancellation():
    async def run():
        net = network()
        started = asyncio.Event()

        async def pending(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        net.client.get.side_effect = pending
        async with net:
            for session, method in (
                ("other", "Fetch.requestPaused"),
                ("session", "Page.javascriptDialogOpening"),
            ):
                await net.cdp.events.put({"sessionId": session, "method": method})
            await net.cdp.events.put(
                {
                    "sessionId": "session",
                    "method": "Fetch.requestPaused",
                    "params": request(URL + "/image", "Image"),
                }
            )
            await asyncio.wait_for(started.wait(), 1)
            pending_tasks = list(net.pending)
            net.count = 512
            await net.cdp.events.put(
                {"sessionId": "session", "method": "Fetch.requestPaused", "params": request()}
            )
            await asyncio.sleep(0)
            with pytest.raises(FetchError, match="数量"):
                net.check()
        assert all(task.done() for task in pending_tasks)
        assert net._pump.done()
        assert any(
            call.args[0] == "Page.handleJavaScriptDialog" for call in net.cdp.call.call_args_list
        )

    asyncio.run(run())


def test_post_xhr_with_body_is_proxied():
    # bilibili 阅读器用 twirp POST 拉图片清单:带 postData 的 XHR POST 转发给公共 fetcher。
    async def run():
        net = network()
        net.client.post.return_value = response("https://api.example.test/index", b'{"ok":1}')
        await net._serve(
            request(
                "https://api.example.test/index",
                "XHR",
                method="POST",
                postData='{"ep_id":1}',
                headers={"Content-Type": "application/json", "Referer": URL},
            )
        )
        net.client.post.assert_awaited_once_with(
            "https://api.example.test/index",
            body=b'{"ep_id":1}',
            content_type="application/json",
            referer=URL,
        )
        method, payload = net.cdp.call.call_args.args
        assert method == "Fetch.fulfillRequest"
        assert base64.b64decode(payload["body"]) == b'{"ok":1}'
        net.check()

    asyncio.run(run())


def test_post_with_oversized_body_falls_back_to_warning():
    async def run():
        net = network()
        await net._serve(
            request(URL + "/api", "XHR", method="POST", postData="x" * (2 * 1024 * 1024 + 1))
        )
        net.client.post.assert_not_called()
        net.check()
        assert net.warnings
        assert net.cdp.call.call_args.args[0] == "Fetch.failRequest"

    asyncio.run(run())


def test_rejected_page_api_calls_do_not_stop_capture():
    # bilibili 未登录时 GetInitInfo 回 401:拒掉继续渲染,不致命。
    async def run():
        net = network()
        net.client.post.side_effect = HttpStatusError("https://x.test/twirp/Init", 401)
        await net._serve(request("https://x.test/twirp/Init", "XHR", method="POST", postData="{}"))
        net.check()
        assert net.cdp.call.call_args.args[0] == "Fetch.failRequest"
        assert net.warnings == {"动态页面接口请求被拒绝了(HTTP 401),已跳过"}

    asyncio.run(run())


def test_cancelled_interception_is_a_warning_not_fatal():
    # 页面在应答前取消请求(广告/统计常见):Invalid InterceptionId 不再致命。
    gone = FetchError("CDP command failed (code -32602): Invalid InterceptionId.")

    async def run():
        net = network()
        net.cdp.call.side_effect = gone
        await net._serve(request(URL + "/ad", "Image"))
        net.check()
        assert net.warnings == {"动态页面在应答前取消了部分请求,已跳过"}

    asyncio.run(run())

    async def run_main():
        net = network()
        net.cdp.call.side_effect = gone
        await net._serve(request())
        with pytest.raises(FetchError, match="Invalid InterceptionId"):
            net.check()

    asyncio.run(run_main())

    async def run_fail():
        net = network()
        net.client.get.side_effect = BlockedError("robots denied")
        net.cdp.call.side_effect = gone
        await net._serve(request(URL + "/img", "Image"))
        net.check()  # failRequest 落在已失效的拦截点上也不致命
        assert net.warnings

    asyncio.run(run_fail())
