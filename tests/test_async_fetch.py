from __future__ import annotations

import asyncio
import gzip
import random
import time
from collections import Counter

import httpx
import pytest

from quire.errors import BlockedError, ConfigError, FetchError, HttpStatusError, NetworkError
from quire.fetch.session import AsyncFetcher


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks=(), *, waiting=None):
        self.chunks = chunks
        self.closed = False
        self.waiting = waiting

    async def __aiter__(self):
        if self.waiting is not None:
            self.waiting.set()
            await asyncio.Event().wait()
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


def answer(request, status=200, data=b"ok", headers=None):
    return httpx.Response(status, stream=Stream([data]), headers=headers, request=request)


def test_blocked_hints_and_browser_like_default_headers():
    async def run():
        seen = {}

        def handler(request):
            if request.url.path == "/robots.txt":
                return answer(request, 404)
            seen[request.url.path] = request.headers
            if request.url.path == "/cf":
                return answer(
                    request,
                    403,
                    data=b"<title>Attention Required! | Cloudflare</title>",
                    headers={"server": "cloudflare", "cf-ray": "abc-SJC"},
                )
            return answer(request, 403, data=b"blocked")

        async with AsyncFetcher(
            transport=httpx.MockTransport(handler), retries=0, rate=1e9
        ) as client:
            with pytest.raises(BlockedError) as plain:
                await client.get("https://example.test/plain")
            assert plain.value.hint and "Cloudflare" not in plain.value.hint
            with pytest.raises(BlockedError) as cloudflare:
                await client.get("https://example.test/cf")
            assert cloudflare.value.hint and "Cloudflare" in cloudflare.value.hint
            sent = seen["/plain"]
            assert "Chrome/" in sent["user-agent"]
            assert sent["accept-language"].startswith("zh-CN")

    asyncio.run(run())


def test_async_response_charset_and_robots_single_lookup():
    async def run():
        counts = Counter()

        async def handler(request):
            counts[request.url.path] += 1
            if request.url.path == "/robots.txt":
                await asyncio.sleep(0.005)
                return answer(request, data=b"User-agent: *\nDisallow: /private")
            return answer(
                request,
                data=gzip.compress("卷帙".encode("gb18030")),
                headers={"Content-Encoding": "gzip", "Content-Type": "text/html; charset=gb18030"},
            )

        async with AsyncFetcher(transport=httpx.MockTransport(handler), rate=10000) as client:
            replies = await asyncio.gather(
                *(client.get("https://example.test/page") for _ in range(8))
            )
            assert all(r.text == "卷帙" and r.content_type == "text/html" for r in replies)
            with pytest.raises(TypeError):
                replies[0].headers["mutate"] = "no"
            with pytest.raises(BlockedError):
                await client.get("https://example.test/private")
        assert counts["/robots.txt"] == 1 and counts["/private"] == 0

    asyncio.run(run())


@pytest.mark.parametrize("status", [404, 410, 403, 500])
def test_robots_absence_or_errors(status):
    async def run():
        seen = []

        def handler(request):
            seen.append(request.url.path)
            return answer(request, status=status if request.url.path == "/robots.txt" else 200)

        async with AsyncFetcher(
            transport=httpx.MockTransport(handler), retries=0, rate=10000
        ) as client:
            if status in (404, 410):
                assert (await client.get("https://example.test/page")).content == b"ok"
            else:
                with pytest.raises(BlockedError if status == 403 else HttpStatusError):
                    await client.get("https://example.test/page")
                assert seen == ["/robots.txt"]

    asyncio.run(run())


def test_redirected_robots_and_single_connection_do_not_deadlock():
    async def run():
        def handler(request):
            if request.url.path == "/robots.txt":
                return answer(request, 302, headers={"Location": "/policy"})
            if request.url.path == "/policy":
                return answer(request, data=b"User-agent: *\nDisallow: /blocked")
            return answer(request)

        async with AsyncFetcher(
            transport=httpx.MockTransport(handler), concurrency=1, rate=10000
        ) as client:
            replies = await asyncio.wait_for(
                asyncio.gather(*(client.get(f"https://example.test/page/{i}") for i in range(8))), 2
            )
            assert all(reply.status == 200 for reply in replies)

    asyncio.run(run())


@pytest.mark.parametrize("status", [403, 500])
def test_concurrent_robots_failures_are_shared_and_later_call_can_retry(status):
    async def run():
        seen = []
        current_status = status

        async def handler(request):
            seen.append(request.url.path)
            if request.url.path == "/robots.txt":
                await asyncio.sleep(0.01)
                return answer(request, current_status)
            return answer(request)

        async with AsyncFetcher(
            transport=httpx.MockTransport(handler), rate=1e9, retries=0
        ) as client:
            replies = await asyncio.gather(
                *(client.get("https://example.test/page") for _ in range(5)),
                return_exceptions=True,
            )
            error = BlockedError if status == 403 else HttpStatusError
            assert all(isinstance(reply, error) for reply in replies)
            assert seen == ["/robots.txt"]
            current_status = 404
            assert (await client.get("https://example.test/page")).status == 200
            assert seen == ["/robots.txt", "/robots.txt", "/page"]

    asyncio.run(run())


def test_queue_and_transfer_share_one_timeout_budget():
    async def run():
        async def handler(request):
            if request.url.path == "/robots.txt":
                return answer(request, 404)
            await asyncio.sleep(0.14)
            return answer(request)

        async with AsyncFetcher(
            transport=httpx.MockTransport(handler),
            concurrency=1,
            rate=1e9,
            timeout=0.2,
            retries=0,
        ) as client:
            await client.robots.check("https://example.test/page")
            await client._slots.acquire()
            task = asyncio.create_task(client.get("https://example.test/page"))
            await asyncio.sleep(0.14)
            client._slots.release()
            with pytest.raises(NetworkError, match="TimeoutError"):
                await asyncio.wait_for(task, 1)
            assert (await client.get("https://example.test/page")).status == 200

    asyncio.run(run())


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
def test_retryable_status_and_stream_closed(status, monkeypatch):
    async def run():
        calls = 0
        streams = []
        delays = []
        real_sleep = asyncio.sleep

        async def sleep(delay):
            delays.append(delay)
            await real_sleep(0)

        monkeypatch.setattr("quire.fetch.session.asyncio.sleep", sleep)

        def handler(request):
            nonlocal calls
            if request.url.path == "/robots.txt":
                return answer(request, 404)
            calls += 1
            stream = Stream([b"ok"])
            streams.append(stream)
            return httpx.Response(
                status if calls == 1 else 200,
                stream=stream,
                headers={"Retry-After": "0"},
                request=request,
            )

        async with AsyncFetcher(
            transport=httpx.MockTransport(handler), retries=1, rate=1e9, rng=random.Random(1)
        ) as client:
            assert (await client.get("https://example.test/page")).content == b"ok"
        assert calls == 2 and all(s.closed for s in streams)
        assert any(0.36 <= d <= 0.84 for d in delays)

    asyncio.run(run())


@pytest.mark.parametrize("status", [403, 404, 429])
def test_nonretry_or_excessive_retry_after(status):
    async def run():
        calls = []

        def handler(request):
            calls.append(request.url.path)
            if request.url.path == "/robots.txt":
                return answer(request, 404)
            return answer(request, status, headers={"Retry-After": "121"})

        async with AsyncFetcher(
            transport=httpx.MockTransport(handler), retries=3, rate=1e9
        ) as client:
            with pytest.raises(BlockedError if status == 403 else HttpStatusError) as exc:
                await client.get("https://example.test/page?token=private")
            assert "private" not in str(exc.value)
            if status == 429:
                assert client.limiter._next["example.test"] > time.monotonic() + 119
        assert calls.count("/page") == 1

    asyncio.run(run())


def test_connection_error_retries_then_reports_clean_error(monkeypatch):
    async def run():
        calls = 0
        real_sleep = asyncio.sleep

        async def sleep(delay):
            await real_sleep(0)

        monkeypatch.setattr("quire.fetch.session.asyncio.sleep", sleep)

        def handler(request):
            nonlocal calls
            if request.url.path == "/robots.txt":
                return answer(request, 404)
            calls += 1
            raise httpx.ConnectError("private internal error", request=request)

        async with AsyncFetcher(
            transport=httpx.MockTransport(handler), retries=1, rate=1e9
        ) as client:
            with pytest.raises(NetworkError) as exc:
                await client.get("https://example.test/page?token=private")
            assert "private" not in str(exc.value)
        assert calls == 2

    asyncio.run(run())


def test_cross_origin_redirect_checks_policy_and_drops_referer_and_cookies():
    async def run():
        seen = []
        middle = Stream(waiting=asyncio.Event())

        def handler(request):
            seen.append(request)
            if request.url.path == "/robots.txt":
                return answer(request, 404)
            if request.url.host == "first.test":
                return httpx.Response(
                    302,
                    headers={
                        "Location": "https://second.test/page",
                        "Set-Cookie": "session=private",
                    },
                    stream=middle,
                    request=request,
                )
            return answer(request)

        async with AsyncFetcher(transport=httpx.MockTransport(handler), rate=1e9) as client:
            result = await client.get("https://first.test/page", referer="https://first.test/book")
            assert result.url == "https://second.test/page"
            assert client._client is not None and not client._client.cookies
        assert middle.closed and not middle.waiting.is_set()
        assert any(r.url.host == "second.test" and r.url.path == "/robots.txt" for r in seen)
        assert "referer" not in seen[-1].headers and "cookie" not in seen[-1].headers

    asyncio.run(run())


@pytest.mark.parametrize(
    "location",
    ["http://other.test/page", "file:///private", "https://u:p@other.test/", "/page", ""],
)
def test_redirect_invalid_downgrade_or_loop(location):
    async def run():
        def handler(request):
            return (
                answer(request, 404)
                if request.url.path == "/robots.txt"
                else answer(request, 302, headers={"Location": location})
            )

        async with AsyncFetcher(transport=httpx.MockTransport(handler), rate=1e9) as client:
            with pytest.raises((BlockedError, ConfigError, NetworkError)):
                await client.get("https://example.test/page")

    asyncio.run(run())


@pytest.mark.parametrize(
    "data,headers",
    [
        (b"x" * 81, {}),
        (b"x", {"Content-Length": "81"}),
        (b"x", {"Content-Length": "2"}),
        (b"x", {"Content-Length": "bad"}),
        (gzip.compress(b"x" * 1000), {"Content-Encoding": "gzip"}),
        (b"x", {"Content-Encoding": "br"}),
    ],
)
def test_body_budgets_and_closed_failed_responses(data, headers):
    async def run():
        stream = Stream([data])

        def handler(request):
            if request.url.path == "/robots.txt":
                return answer(request, 404)
            return httpx.Response(200, headers=headers, stream=stream, request=request)

        async with AsyncFetcher(
            transport=httpx.MockTransport(handler), rate=1e9, max_bytes=80
        ) as client:
            with pytest.raises(FetchError):
                await client.get("https://example.test/page")
        assert stream.closed

    asyncio.run(run())


@pytest.mark.parametrize("where", ["headers", "body", "policy", "rate", "backoff"])
def test_cancel_at_waiting_boundaries_releases_context(where):
    async def run():
        ready = asyncio.Event()
        stream = Stream(waiting=ready)

        async def handler(request):
            if request.url.path == "/robots.txt":
                if where == "policy":
                    ready.set()
                    await asyncio.Event().wait()
                return answer(request, 404)
            if where == "headers":
                ready.set()
                await asyncio.Event().wait()
            if where == "backoff":
                ready.set()
                return answer(request, 500)
            return httpx.Response(200, stream=stream, request=request)

        async with AsyncFetcher(transport=httpx.MockTransport(handler), rate=1e9) as client:
            if where == "rate":
                await client.robots.check("https://example.test/page")
                client.limiter.defer("https://example.test/page", 100)
            task = asyncio.create_task(client.get("https://example.test/page"))
            if where == "rate":
                await asyncio.sleep(0.01)
            else:
                await asyncio.wait_for(ready.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 0.5)
            assert not client._active
        if where == "body":
            assert stream.closed

    asyncio.run(run())


def test_deadline_and_context_closing_cancels_inflight():
    async def run():
        ready = asyncio.Event()

        async def handler(request):
            if request.url.path == "/robots.txt":
                return answer(request, 404)
            ready.set()
            await asyncio.Event().wait()

        transport = httpx.MockTransport(handler)
        async with AsyncFetcher(transport=transport, rate=1e9, timeout=0.02, retries=0) as client:
            with pytest.raises(NetworkError):
                await client.get("https://example.test/page")
        ready.clear()
        async with AsyncFetcher(transport=httpx.MockTransport(handler), rate=1e9) as client:
            task = asyncio.create_task(client.get("https://example.test/page"))
            await ready.wait()
        assert task.cancelled() and client._client is None
        with pytest.raises(ConfigError):
            await client.get("https://example.test/page")
        with pytest.raises(ConfigError):
            await client.__aenter__()

    asyncio.run(run())


@pytest.mark.parametrize(
    "url", ["relative", "file:///private", "https://u:p@example.test", "https://example.test/\n"]
)
def test_bad_urls_before_network(url):
    async def run():
        async with AsyncFetcher() as client:
            with pytest.raises(ConfigError):
                await client.get(url)

    asyncio.run(run())


@pytest.mark.parametrize(
    "headers",
    [{"Authorization": "secret"}, {"Cookie": "secret"}, {"Host": "other"}, {"Accept": "a\nb"}],
)
def test_bad_headers_before_network(headers):
    async def run():
        async with AsyncFetcher() as client:
            with pytest.raises(ConfigError):
                await client.get("https://example.test", headers=headers)

    asyncio.run(run())


@pytest.mark.parametrize(
    "options",
    [
        {"timeout": 0},
        {"timeout": float("nan")},
        {"rate": 0},
        {"retries": -1},
        {"retries": True},
        {"concurrency": 0},
        {"max_bytes": 0},
    ],
)
def test_invalid_settings(options):
    with pytest.raises(ConfigError):
        AsyncFetcher(**options)


def test_cooled_host_does_not_hold_slots_needed_by_other_hosts():
    async def run():
        def handler(request):
            return answer(request, 404 if request.url.path == "/robots.txt" else 200)

        async with AsyncFetcher(
            transport=httpx.MockTransport(handler), concurrency=1, rate=1e9
        ) as client:
            await client.robots.check("https://slow.test/page")
            client.limiter.defer("https://slow.test/page", 100)
            blocked = asyncio.create_task(client.get("https://slow.test/page"))
            await asyncio.sleep(0)
            assert (await asyncio.wait_for(client.get("https://fast.test/page"), 1)).status == 200
            blocked.cancel()
            with pytest.raises(asyncio.CancelledError):
                await blocked

    asyncio.run(run())


def test_waiting_for_request_slot_has_a_deadline():
    async def run():
        ready = asyncio.Event()

        def handler(request):
            return answer(request, 404 if request.url.path == "/robots.txt" else 200)

        async with AsyncFetcher(
            transport=httpx.MockTransport(handler), concurrency=1, rate=1e9, timeout=0.02, retries=0
        ) as client:
            await client.robots.check("https://example.test/page")
            await client._slots.acquire()
            ready.set()
            with pytest.raises(NetworkError):
                await asyncio.wait_for(client.get("https://example.test/page"), 1)
            client._slots.release()
            assert (await client.get("https://example.test/page")).status == 200

    asyncio.run(run())


def test_redirect_cooldown_is_charged_to_destination_host():
    async def run():
        def handler(request):
            if request.url.path == "/robots.txt":
                return answer(request, 404)
            if request.url.host == "first.test":
                return answer(request, 302, headers={"Location": "https://second.test/page"})
            return answer(request, 429, headers={"Retry-After": "121"})

        async with AsyncFetcher(transport=httpx.MockTransport(handler), rate=1e9) as client:
            with pytest.raises(HttpStatusError):
                await client.get("https://first.test/page")
            assert client.limiter._next["second.test"] > time.monotonic() + 120
            assert client.limiter._next["first.test"] <= time.monotonic()

    asyncio.run(run())


def test_redirect_hop_limit_and_target_policy_denial():
    async def run():
        paths = []

        def handler(request):
            paths.append(str(request.url))
            if request.url.path == "/robots.txt":
                return answer(request, data=b"User-agent: *\nDisallow: /denied")
            if request.url.path == "/blocked-redirect":
                return answer(request, 302, headers={"Location": "/denied"})
            return answer(request, 302, headers={"Location": f"/{int(request.url.path[1:]) + 1}"})

        async with AsyncFetcher(transport=httpx.MockTransport(handler), rate=1e9) as client:
            with pytest.raises(NetworkError, match="hop limit"):
                await client.get("https://example.test/0")
            with pytest.raises(BlockedError):
                await client.get("https://example.test/blocked-redirect")
        assert not any(p.endswith("/denied") for p in paths)
        assert sum(p.rsplit("/", 1)[-1].isdigit() for p in paths) == 11

    asyncio.run(run())
