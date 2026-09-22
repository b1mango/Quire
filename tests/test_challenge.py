"""JS 令牌质询:识别、带 Cookie 重访、隐私边界(默认不存 Cookie)。"""

from __future__ import annotations

import asyncio

import httpx

from quire.fetch.challenge import challenge_target
from quire.fetch.session import AsyncFetcher

CHALLENGE = """
<html><head><title>正在验证浏览器</title></head><body><p>請稍等…</p>
<script>
let token = "MTc5MDA1MTE4MDozZDMzOWM2YjM3YWFiZmQz";
window.location.href = location.pathname + "?challenge=" + encodeURIComponent(token);
</script></body></html>
"""


def test_challenge_target_detects_token_page() -> None:
    url = "https://ixdzs8.com/read/1/p1.html"
    target = challenge_target(CHALLENGE, url, len(CHALLENGE.encode()))
    assert target is not None
    assert target.startswith(url + "?challenge=")
    assert "MTc5MDA1MTE4MD" in target


def test_challenge_target_ignores_normal_pages() -> None:
    assert challenge_target("<html><body>正常正文</body></html>", "https://a.test/", 30) is None
    big = "x" * 20000 + CHALLENGE
    assert challenge_target(big, "https://a.test/", len(big)) is None


def _challenge_server(seen: list[dict[str, str]]):
    class _Body(httpx.AsyncByteStream):
        def __init__(self, data: bytes) -> None:
            self.data = data

        async def __aiter__(self):
            yield self.data

        async def aclose(self) -> None:
            pass

    def reply(request: httpx.Request, text: str, headers: dict[str, str] | None = None):
        return httpx.Response(200, stream=_Body(text.encode()), headers=headers, request=request)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "path": request.url.path,
                "query": request.url.query.decode(),
                "cookie": request.headers.get("cookie", ""),
            }
        )
        if "challenge=" not in request.url.query.decode():
            return reply(request, CHALLENGE, {"set-cookie": "PHPSESSID=test-session; path=/"})
        assert "test-session" in request.headers.get("cookie", ""), "质询应答必须带会话 Cookie"
        return reply(request, "<html><body><p>真实正文</p></body></html>")

    return handler


def test_fetcher_solves_challenge_with_cookies() -> None:
    async def run() -> None:
        seen: list[dict[str, str]] = []
        async with AsyncFetcher(
            transport=httpx.MockTransport(_challenge_server(seen)),
            retries=0,
            rate=1e9,
            respect_robots=False,
            cookie_hosts=frozenset({"ixdzs8.com"}),
        ) as client:
            page = await client.get("https://ixdzs8.com/read/1/p1.html")
            assert "真实正文" in page.text
            assert len(seen) == 2
            # 第二次请求携带质询参数与会话 Cookie
            assert "challenge=" in seen[1]["query"]
            assert "test-session" in seen[1]["cookie"]

    asyncio.run(run())


def test_fetcher_without_cookie_hosts_does_not_solve_or_store() -> None:
    """默认(未登记站点):不识答质询、不保留 Cookie——隐私边界不变。"""

    async def run() -> None:
        seen: list[dict[str, str]] = []
        async with AsyncFetcher(
            transport=httpx.MockTransport(_challenge_server(seen)),
            retries=0,
            rate=1e9,
            respect_robots=False,
        ) as client:
            page = await client.get("https://ixdzs8.com/read/1/p1.html")
            assert "正在验证浏览器" in page.text  # 质询页原样返回
            assert len(seen) == 1
            assert seen[0]["cookie"] == ""

    asyncio.run(run())
