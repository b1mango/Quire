"""Cloudflare 盾自动升级：403 触发真实 Chrome 通道、cf_clearance 回注与诚实失败。

单元测试用 MockTransport 模拟 CF 403/放行，用 monkeypatch 替换 solve_clearance；
端到端测试用真实 Chrome 打本地 mock CF 站（无 cf_clearance 一律 403 质询页，
质询页脚本自置 Cookie 后重载），无 Chrome 时跳过。
"""

from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import httpx
import pytest

from quire.errors import BlockedError, FetchError
from quire.fetch import cloudflare
from quire.fetch.browser import _challenge_dom
from quire.fetch.browser_process import find_chrome
from quire.fetch.cloudflare import ClearanceEscalation
from quire.fetch.session import AsyncFetcher

CHALLENGE_HTML = b"""<html><head><title>Just a moment...</title></head>
<body><div id="challenge-running"></div><p>Checking your browser</p></body></html>"""

CF_HEADERS = {"cf-ray": "test-SJC", "server": "cloudflare", "content-type": "text/html"}


class _Body(httpx.AsyncByteStream):
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def __aiter__(self):
        yield self.data

    async def aclose(self) -> None:
        pass


def _reply(request: httpx.Request, status: int, body: bytes, headers: dict[str, str] | None = None):
    return httpx.Response(status, stream=_Body(body), headers=headers or {}, request=request)


def _cf_handler(seen: list[dict[str, str]], state: dict[str, str]):
    """无 cf_clearance 一律 403 质询页；带当前有效 Cookie 才放行正文。"""

    def handler(request: httpx.Request) -> httpx.Response:
        cookie = request.headers.get("cookie", "")
        seen.append(
            {
                "path": request.url.path,
                "cookie": cookie,
                "ua": request.headers.get("user-agent", ""),
            }
        )
        if f"cf_clearance={state['token']}" in cookie:
            return _reply(request, 200, "<html><body><p>真实正文</p></body></html>".encode())
        return _reply(request, 403, CHALLENGE_HTML, CF_HEADERS)

    return handler


def _patch_solve(monkeypatch, results):
    calls: list[str] = []

    async def fake_solve(client, url, *, cdp_endpoint, executable, timeout):
        calls.append(url)
        await asyncio.sleep(0.02)
        outcome = results[len(calls) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(cloudflare, "solve_clearance", fake_solve)
    return calls


def _client(handler) -> AsyncFetcher:
    return AsyncFetcher(
        transport=httpx.MockTransport(handler), retries=0, rate=1e9, respect_robots=False
    )


def test_cf_403_escalates_and_injects_clearance(monkeypatch) -> None:
    """CF 403 自动升级；重试带 cf_clearance 与浏览器一致的 UA，拿到真实正文。"""

    async def run() -> None:
        seen: list[dict[str, str]] = []
        state = {"token": "tok-value"}
        calls = _patch_solve(monkeypatch, [("tok-value", "MockChrome/1.0")])
        async with _client(_cf_handler(seen, state)) as client:
            client.escalation = ClearanceEscalation(client)
            page = await client.get("https://cf.test/book/1.html")
            assert "真实正文" in page.text
        assert calls == ["https://cf.test/book/1.html"]
        assert seen[0]["cookie"] == ""
        assert "cf_clearance=tok-value" in seen[1]["cookie"]
        assert seen[1]["ua"] == "MockChrome/1.0"

    asyncio.run(run())


def test_clearance_reused_without_second_escalation(monkeypatch) -> None:
    """回注后同站后续请求走快速 HTTP 通道，不再启动浏览器。"""

    async def run() -> None:
        seen: list[dict[str, str]] = []
        state = {"token": "tok-value"}
        calls = _patch_solve(monkeypatch, [("tok-value", "MockChrome/1.0")])
        async with _client(_cf_handler(seen, state)) as client:
            client.escalation = ClearanceEscalation(client)
            await client.get("https://cf.test/book/1.html")
            page = await client.get("https://cf.test/book/2.html")
            assert "真实正文" in page.text
        assert len(calls) == 1
        assert len(seen) == 3  # 403 + 重试 + 第二章各一次
        assert all("cf_clearance=tok-value" in r["cookie"] for r in seen[1:])

    asyncio.run(run())


def test_concurrent_403_solves_only_once(monkeypatch) -> None:
    """同站并发 403 只升级一次；等待中的请求直接用回注凭据重试。"""

    async def run() -> None:
        seen: list[dict[str, str]] = []
        state = {"token": "tok-value"}
        calls = _patch_solve(monkeypatch, [("tok-value", "MockChrome/1.0")])
        async with _client(_cf_handler(seen, state)) as client:
            client.escalation = ClearanceEscalation(client)
            first, second = await asyncio.gather(
                client.get("https://cf.test/book/1.html"),
                client.get("https://cf.test/book/2.html"),
            )
            assert "真实正文" in first.text and "真实正文" in second.text
        assert len(calls) == 1

    asyncio.run(run())


def test_escalation_failure_is_honest_and_cached(monkeypatch) -> None:
    """质询超时/需人工：如实 BlockedError，且同站不再重复启动浏览器。"""

    async def run() -> None:
        seen: list[dict[str, str]] = []
        state = {"token": "tok-value"}
        failure = BlockedError("Cloudflare 质询在 45 秒内未通过", hint="可能需要人工完成验证")
        calls = _patch_solve(monkeypatch, [failure])
        async with _client(_cf_handler(seen, state)) as client:
            client.escalation = ClearanceEscalation(client)
            with pytest.raises(BlockedError, match="质询"):
                await client.get("https://cf.test/book/1.html")
            with pytest.raises(BlockedError, match="质询"):
                await client.get("https://cf.test/book/2.html")
        assert len(calls) == 1  # 失败按站缓存，不逐章重试

    asyncio.run(run())


def test_rejected_clearance_is_reported_and_not_retried(monkeypatch) -> None:
    """回注后仍 403（凭据未被接受）：如实报错并标记该站升级无效。"""

    async def run() -> None:
        seen: list[dict[str, str]] = []
        state = {"token": "never-accepted"}
        calls = _patch_solve(monkeypatch, [("wrong-token", "MockChrome/1.0")])
        async with _client(_cf_handler(seen, state)) as client:
            client.escalation = ClearanceEscalation(client)
            with pytest.raises(BlockedError):
                await client.get("https://cf.test/book/1.html")
            with pytest.raises(BlockedError) as again:
                await client.get("https://cf.test/book/2.html")
            assert again.value.hint and "cf_clearance" in again.value.hint
        assert len(calls) == 1

    asyncio.run(run())


def test_expired_clearance_escalates_again(monkeypatch) -> None:
    """cf_clearance 过期（再次 403）：如实重新升级，换新凭据后继续。"""

    async def run() -> None:
        seen: list[dict[str, str]] = []
        state = {"token": "v1"}
        calls = _patch_solve(monkeypatch, [("v1", "UA/1"), ("v2", "UA/2")])
        async with _client(_cf_handler(seen, state)) as client:
            client.escalation = ClearanceEscalation(client)
            assert "真实正文" in (await client.get("https://cf.test/book/1.html")).text
            state["token"] = "v2"  # 站点吊销 v1
            assert "真实正文" in (await client.get("https://cf.test/book/2.html")).text
        assert len(calls) == 2
        assert "cf_clearance=v2" in seen[-1]["cookie"] and seen[-1]["ua"] == "UA/2"

    asyncio.run(run())


def test_plain_403_never_escalates(monkeypatch) -> None:
    """普通防盗链 403（非 CF）不触发升级，行为与之前一致。"""

    async def run() -> None:
        calls = _patch_solve(monkeypatch, [])

        def handler(request: httpx.Request) -> httpx.Response:
            return _reply(request, 403, b"<html><body>referer required</body></html>")

        async with _client(handler) as client:
            client.escalation = ClearanceEscalation(client)
            with pytest.raises(BlockedError) as exc:
                await client.get("https://plain.test/image.jpg")
            assert not exc.value.cloudflare
        assert calls == []

    asyncio.run(run())


def test_set_clearance_rejects_invalid_values() -> None:
    client = AsyncFetcher()
    with pytest.raises(FetchError):
        client.set_clearance("https://cf.test/", cookie="a;b", user_agent="UA")
    with pytest.raises(FetchError):
        client.set_clearance("https://cf.test/", cookie="tok", user_agent="UA\nX-Injected: 1")


def test_challenge_dom_detection() -> None:
    assert _challenge_dom(CHALLENGE_HTML.decode())
    assert _challenge_dom("<html><head><title>Attention Required!</title></head></html>")
    assert not _challenge_dom("<html><head><title>第一章</title></head><body>正文</body></html>")


# ---------------------------------------------------------------- 真实 Chrome 端到端

E2E_CHALLENGE = b"""<html><head><title>Just a moment...</title></head>
<body><div id="challenge-running"></div><p>Checking your browser</p>
<script>
setTimeout(() => {
  document.cookie = "cf_clearance=e2e-token; path=/";
  location.reload();
}, 200);
</script></body></html>"""

CATALOGUE = (
    "<html><head><title>测试之书_测试书城</title></head><body><h1>测试之书</h1>"
    '<ul id="chapter-list"><li><a href="/book/1.html">第一章 起点</a></li>'
    '<li><a href="/book/2.html">第二章 继续</a></li></ul>'
    "</body></html>"
)


def _chapter(number: int, title: str) -> bytes:
    paragraphs = "".join(
        f"<p>{title}第{index}段：他抬头看见远山如黛，风从林间穿过，"
        "带来潮湿的泥土气息。他知道这一趟必须走下去，因为答案就在前面等着他。</p>"
        for index in range(1, 7)
    )
    return (
        f"<html><head><title>{title}_测试书城</title></head><body>"
        f"<h1>{title}</h1>"
        f'<div id="content">{paragraphs}</div>'
        f'<div class="nav"><a href="/book/">目录</a></div>'
        "</body></html>"
    ).encode()


class _CfSite(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), _CfHandler)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"


class _CfHandler(BaseHTTPRequestHandler):
    server: _CfSite

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - http.server 接口
        path = urlsplit(self.path).path
        with self.server.lock:
            self.server.counts[path] = self.server.counts.get(path, 0) + 1
        authed = "cf_clearance=e2e-token" in (self.headers.get("Cookie") or "")
        if not authed:
            return self._respond(403, E2E_CHALLENGE, extra={"cf-ray": "e2e-SJC"})
        if path in {"/", "/book/"}:
            return self._respond(200, CATALOGUE.encode())
        if path == "/book/1.html":
            return self._respond(200, _chapter(1, "第一章 起点"))
        if path == "/book/2.html":
            return self._respond(200, _chapter(2, "第二章 继续"))
        return self._respond(404, b"not found")

    def _respond(self, status: int, body: bytes, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("server", "cloudflare")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return


def _serve_cf() -> tuple[_CfSite, threading.Thread]:
    site = _CfSite()
    thread = threading.Thread(target=site.serve_forever, daemon=True)
    thread.start()
    return site, thread


def test_real_chrome_solves_mock_cf_and_recycles_clearance(monkeypatch) -> None:
    """真实 Chrome 过 mock CF 质询：cf_clearance 回注后同站请求不再走渲染。"""
    if not find_chrome():
        pytest.skip("Chrome unavailable")
    site, thread = _serve_cf()
    calls: list[str] = []
    real_solve = cloudflare.solve_clearance

    async def counting_solve(client, url, **kwargs):
        calls.append(url)
        return await real_solve(client, url, **kwargs)

    monkeypatch.setattr(cloudflare, "solve_clearance", counting_solve)

    async def run() -> None:
        async with AsyncFetcher(respect_robots=False, rate=1e9, retries=0) as client:
            client.escalation = ClearanceEscalation(client, timeout=30)
            page = await client.get(site.url + "/book/1.html")
            assert "第一章 起点" in page.text
            again = await client.get(site.url + "/")
            assert "测试之书" in again.text

    try:
        asyncio.run(run())
    finally:
        site.shutdown()
        site.server_close()
        thread.join(timeout=5)
    assert len(calls) == 1  # 第二次请求直接带 cf_clearance 走 HTTP


def test_core_novel_auto_escalates_without_render(tmp_path, monkeypatch) -> None:
    """完整小说流水线：无 --render 时 CF 403 也自动升级真实 Chrome 取到正文。"""
    if not find_chrome():
        pytest.skip("Chrome unavailable")
    from quire.core_novel import run_core_novel
    from quire.models import NovelOptions

    site, thread = _serve_cf()
    calls: list[str] = []
    real_solve = cloudflare.solve_clearance

    async def counting_solve(client, url, **kwargs):
        calls.append(url)
        return await real_solve(client, url, **kwargs)

    monkeypatch.setattr(cloudflare, "solve_clearance", counting_solve)

    async def run() -> None:
        out = tmp_path / "book.txt"
        result = await run_core_novel(
            site.url + "/book/",
            out,
            formats=("txt",),
            options=NovelOptions(obey_robots=False, rate=100, retries=0),
        )
        assert result.chapters_written == 2 and not result.partial
        text = out.read_text()
        assert "第一章 起点" in text and "第二章 继续" in text

    try:
        asyncio.run(run())
    finally:
        site.shutdown()
        site.server_close()
        thread.join(timeout=5)
    assert len(calls) == 1  # 目录页升级一次，章节页走 HTTP 快速通道
