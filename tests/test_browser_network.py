from __future__ import annotations

import asyncio
import base64
from unittest.mock import AsyncMock

import pytest

from quire.errors import BlockedError, FetchError
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
            assert net.warnings

    asyncio.run(run())


def test_policy_failure_is_fatal_for_main_and_reported_for_subresource():
    async def run():
        net = network()
        net.client.get.side_effect = BlockedError("robots denied")
        await net._serve(request(URL + "/image", "Image"))
        net.check()
        assert net.warnings == {"动态页面部分资源未加载：BlockedError"}
        await net._serve(request(URL + "/blocked"))
        with pytest.raises(BlockedError):
            net.check()

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
