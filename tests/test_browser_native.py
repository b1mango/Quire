from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from quire.errors import BlockedError, ConfigError, FetchError
from quire.fetch.browser import RenderOptions, fetch_render_input, render_page
from quire.fetch.browser_cdp import Cdp
from quire.fetch.browser_endpoint import validate_endpoint
from quire.fetch.browser_native import NativeNetwork, allowed_hosts, owned_tab
from quire.fetch.browser_process import ChromeProcess, find_chrome
from quire.fetch.session import AsyncFetcher
from quire.fetch.simple import Response
from quire.server.settings import UiSettings, load, save
from tests.mock_site.dynamic_server import serve


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.com:9222",
        "http://127.0.0.1:9222@evil.test:9222",
        "http://127.0.0.1:9222/path",
        "http://127.0.0.1:9222?secret=x",
        "ws://127.0.0.1:9222/devtools/page/x",
        "http://0.0.0.0:9222",
        "http://127.0.0.1",
        "http://127.0.0.1:9222#x",
        42,
    ],
)
def test_endpoint_rejects_nonlocal_and_credential_addresses(endpoint):
    with pytest.raises(ConfigError):
        validate_endpoint(endpoint)


def test_settings_roundtrip_and_ipv6(tmp_path):
    assert validate_endpoint("http://localhost:9222") == "http://127.0.0.1:9222"
    assert validate_endpoint("http://[::1]:9222") == "http://[::1]:9222"
    path = tmp_path / "settings.json"
    settings = UiSettings(str(tmp_path), browser_native=True, cdp_endpoint="http://localhost:9222")
    save(path, settings)
    assert load(path, tmp_path) == settings


def test_native_input_skips_http_even_on_403():
    async def run():
        client = AsyncMock()
        client.get.side_effect = BlockedError("403")
        page = await fetch_render_input(client, "https://www.shuqi.com", RenderOptions(native=True))
        assert page.content == b""
        client.get.assert_not_called()

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["attach", "body", "cancel_create", "cancel_body"])
def test_only_owned_target_closed_on_failure_and_cancel(phase):
    async def run():
        created = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def call(method, params=None, **kwargs):
            calls.append((method, params))
            if method == "Target.createTarget":
                created.set()
                if phase == "cancel_create":
                    await release.wait()
                return {"targetId": "owned"}
            if method == "Target.attachToTarget":
                if phase == "attach":
                    raise FetchError("attach failure")
                return {"sessionId": "session"}
            return {}

        cdp = AsyncMock()
        cdp.call.side_effect = call

        async def work():
            async with owned_tab(cdp):
                if phase == "cancel_body":
                    created.clear()
                    created.set()
                    await release.wait()
                raise FetchError("body failure")

        task = asyncio.create_task(work())
        await created.wait()
        await asyncio.sleep(0)
        if phase.startswith("cancel"):
            task.cancel()
            release.set()
        with pytest.raises((FetchError, asyncio.CancelledError)):
            await task
        assert [p for m, p in calls if m == "Target.closeTarget"] == [{"targetId": "owned"}]
        assert not any(m in {"Browser.close", "Browser.setDownloadBehavior"} for m, _ in calls)

    asyncio.run(run())


def test_native_network_blocks_foreign_credentials_and_reports_403():
    async def run():
        cdp = AsyncMock()
        cdp.events = asyncio.Queue()
        page = Response("https://www.shuqi.com", 200, {}, b"", 0)
        async with NativeNetwork(cdp, "s", "f", page, 1000) as network:
            await cdp.events.put(
                {
                    "sessionId": "s",
                    "method": "Fetch.requestPaused",
                    "params": {
                        "requestId": "r",
                        "request": {"url": "https://unrelated.example/path"},
                        "resourceType": "Document",
                        "frameId": "f",
                    },
                }
            )
            await asyncio.sleep(0)
            with pytest.raises(BlockedError):
                network.check()
            assert cdp.call.call_args.args[0] == "Fetch.failRequest"
            network.status = 403
            with pytest.raises(BlockedError):
                network.validate_status(page.url)
        assert "evil.shuqi.com" not in allowed_hosts(page.url)

    asyncio.run(run())


def test_shuqi_whitelist_includes_app_cdn_hosts():
    """书旗 SPA 的应用资源在阿里/书旗 CDN 上,缺了目录页停在「加载中」。"""
    hosts = allowed_hosts("https://t.shuqi.com/catalog/9031364/")
    assert "g.alicdn.com" in hosts
    assert "render-resource.11222.cn" in hosts
    assert "render.shuqireader.com" in hosts
    assert "px.effirst.com" not in hosts  # 统计域仍按未授权阻止


def test_real_cdp_reuses_login_and_keeps_existing_tab(tmp_path):
    chrome = find_chrome()
    if not chrome:
        pytest.skip("Chrome unavailable")

    async def run(url):
        async with ChromeProcess(chrome, extra_args=("--no-proxy-server",)) as process:
            async with Cdp(process.endpoint) as cdp:
                original = await cdp.call("Target.createTarget", {"url": "about:blank"})
                attached = await cdp.call(
                    "Target.attachToTarget", {"targetId": original["targetId"], "flatten": True}
                )
                await cdp.call(
                    "Network.setCookie",
                    {"name": "session", "value": "fixture-login", "url": url},
                    session_id=attached["sessionId"],
                )
                before = await cdp.call("Target.getTargets")
                async with AsyncFetcher(respect_robots=False) as client:
                    response, _ = await render_page(
                        Response(url + "/novel", 200, {}, b"", 0),
                        client,
                        RenderOptions(cdp_endpoint=process.endpoint, timeout=10, settle=0.4),
                        content="text",
                    )
                    assert "第6段" in response.text
                    assert not client._client.cookies
                after = await cdp.call("Target.getTargets")
                assert {t["targetId"] for t in before["targetInfos"]} == {
                    t["targetId"] for t in after["targetInfos"]
                }
                cookies = await cdp.call(
                    "Network.getCookies", {"urls": [url]}, session_id=attached["sessionId"]
                )
                assert any(c["value"] == "fixture-login" for c in cookies["cookies"])
                # Full novel pipeline must not need the anonymous HTTP page fetch.
                from quire.core_novel import run_core_novel
                from quire.models import NovelOptions

                out = tmp_path / "native.txt"
                result = await run_core_novel(
                    url + "/novel",
                    out,
                    formats=("txt",),
                    options=NovelOptions(obey_robots=False, rate=100, retries=0),
                    render=RenderOptions(cdp_endpoint=process.endpoint, timeout=10, settle=0.4),
                )
                assert result.chapters_written == 1 and not result.partial
                assert "第6段" in out.read_text()

    with serve() as site:
        asyncio.run(run(site.url))


def test_discovery_rejects_redirect_and_remote_websocket(monkeypatch):
    import httpx

    from quire.errors import NetworkError
    from quire.fetch.browser_endpoint import resolve_endpoint

    factory = httpx.AsyncClient
    for status, body, headers in [
        (302, {}, {"location": "http://evil.test/json/version"}),
        (200, {"webSocketDebuggerUrl": "ws://evil.test:9222/devtools/browser/x"}, {}),
        (200, {"webSocketDebuggerUrl": "ws://127.0.0.1:9999/devtools/browser/x"}, {}),
        (200, {"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/x"}, {}),
    ]:
        hits = []

        def handler(request, hits=hits, status=status, body=body, headers=headers):
            hits.append(str(request.url))
            return httpx.Response(status, json=body, headers=headers)

        monkeypatch.setattr(
            httpx, "AsyncClient", lambda **kw: factory(transport=httpx.MockTransport(handler), **kw)
        )
        if (
            status == 200
            and body["webSocketDebuggerUrl"] == "ws://127.0.0.1:9222/devtools/browser/x"
        ):
            assert (
                asyncio.run(resolve_endpoint("http://localhost:9222"))
                == body["webSocketDebuggerUrl"]
            )
        else:
            with pytest.raises(NetworkError):
                asyncio.run(resolve_endpoint("http://localhost:9222"))
        assert hits == ["http://127.0.0.1:9222/json/version"]


def test_cdp_does_not_follow_websocket_redirect():
    from websockets.datastructures import Headers
    from websockets.exceptions import InvalidStatus
    from websockets.http11 import Response as WsResponse

    from quire.fetch.browser_cdp import _NoRedirectConnect

    exc = InvalidStatus(WsResponse(302, "Found", Headers({"Location": "ws://evil.test:9222/"})))
    assert _NoRedirectConnect("ws://127.0.0.1:9222/devtools/browser/x").process_redirect(exc) is exc


def test_native_403_is_not_success_and_login_cookie_reaches_server():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    chrome = find_chrome()
    if not chrome:
        pytest.skip("Chrome unavailable")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            authenticated = self.headers.get("Cookie") == "session=fixture-login"
            body = (
                b"<html><body>Authorized chapter</body></html>"
                if authenticated
                else b"<html><body>Forbidden</body></html>"
            )
            self.send_response(200 if authenticated else 403)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def run():
        url = f"http://127.0.0.1:{server.server_port}/"
        async with ChromeProcess(chrome, extra_args=("--no-proxy-server",)) as process:
            options = RenderOptions(cdp_endpoint=process.endpoint, timeout=10, settle=0.4)
            async with AsyncFetcher(respect_robots=False) as client, Cdp(process.endpoint) as cdp:
                with pytest.raises(BlockedError):
                    await render_page(
                        Response(url, 200, {}, b"", 0), client, options, content="text"
                    )
                async with owned_tab(cdp) as session:
                    await cdp.call(
                        "Network.setCookie",
                        {"name": "session", "value": "fixture-login", "url": url},
                        session_id=session,
                    )
                page, _ = await render_page(
                    Response(url, 200, {}, b"", 0), client, options, content="text"
                )
                assert "Authorized chapter" in page.text and page.status == 200
                assert not client._client.cookies

    try:
        asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_repeated_cancel_waits_for_target_ownership_and_cleanup():
    async def run():
        created, release = asyncio.Event(), asyncio.Event()
        closed = []

        async def call(method, params=None, **kwargs):
            if method == "Target.createTarget":
                created.set()
                await release.wait()
                return {"targetId": "owned"}
            if method == "Target.closeTarget":
                closed.append(params["targetId"])
            return {}

        cdp = AsyncMock()
        cdp.call.side_effect = call

        async def work():
            async with owned_tab(cdp):
                pytest.fail("cancelled before attach")

        task = asyncio.create_task(work())
        await created.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed == ["owned"]

    asyncio.run(run())


def test_cli_endpoint_does_not_require_local_chrome(monkeypatch):
    import argparse

    from quire.cli_options import add_novel_options, render_options

    parser = argparse.ArgumentParser()
    add_novel_options(parser)
    monkeypatch.setattr(
        "quire.fetch.browser_process.find_chrome",
        lambda _: pytest.fail("must not discover local Chrome"),
    )
    args = parser.parse_args(
        ["https://t.shuqi.com", "--render", "--cdp-endpoint", "http://127.0.0.1:9222"]
    )
    assert render_options(args).cdp_endpoint == "http://127.0.0.1:9222"
    args.render = False
    with pytest.raises(ConfigError):
        render_options(args)


def test_closed_interception_does_not_kill_native_pump():
    async def run():
        cdp = AsyncMock()
        cdp.call.side_effect = FetchError("CDP command failed: Invalid InterceptionId")
        network = NativeNetwork(
            cdp, "s", "f", Response("https://t.shuqi.com", 200, {}, b"", 0), 1024
        )
        await network._continue("Fetch.continueRequest", {"requestId": "gone"}, session_id="s")
        cdp.call.side_effect = FetchError("disconnected")
        with pytest.raises(FetchError, match="disconnected"):
            await network._continue("Fetch.continueRequest", {"requestId": "live"}, session_id="s")

    asyncio.run(run())
