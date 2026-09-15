"""Real HTTP/1.1 checks; every scenario and network operation has a deadline."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Coroutine
from typing import Any

import pytest

from quire.errors import ConfigError, FetchError, NetworkError
from quire.fetch.session import AsyncFetcher
from quire.fetch.simple import Response
from tests.mock_site.async_server import BODY, AsyncMockSite


def run_scenario(scenario: Coroutine[Any, Any, None]) -> None:
    async def bounded() -> None:
        baseline = asyncio.all_tasks()
        try:
            async with asyncio.timeout(12):
                await scenario
        finally:
            await asyncio.sleep(0)
            remaining = asyncio.all_tasks() - baseline
            for task in remaining:
                task.cancel()
            if remaining:
                await asyncio.wait_for(asyncio.gather(*remaining, return_exceptions=True), 2)
            assert not remaining, f"Background tasks survived scenario: {remaining}"

    asyncio.run(bounded())


async def get(client: AsyncFetcher, site: AsyncMockSite, path: str) -> Response:
    return await asyncio.wait_for(client.get(site.url + path), 3)


async def wait_stage(site: AsyncMockSite, path: str) -> None:
    await site.wait_for(lambda: bool(site.records(path)))
    record = site.records(path)[0]
    if path == "/wait-headers":
        assert record.headers_sent_at is None
    else:
        await site.wait_for(lambda: record.bytes_sent >= 2)
        assert record.headers_sent_at is not None and not record.complete


def test_http11_sequential_gets_reuse_connection() -> None:
    async def run() -> None:
        async with AsyncMockSite() as site:
            async with AsyncFetcher(timeout=1, retries=0, rate=10000) as client:
                first = await asyncio.wait_for(
                    client.get(
                        site.url + "/body",
                        referer=site.url + "/book",
                        headers={"Accept-Language": "en"},
                    ),
                    3,
                )
                second = await get(client, site, "/body")
                assert isinstance(first, Response) and isinstance(second, Response)
                assert first.content == second.content == BODY
                assert first.status == second.status == 200
                assert first.url == site.url + "/body"
                records = site.records("/body")
                assert len(records) == 2
                assert records[0].connection_id == records[1].connection_id, site.snapshot()
                assert records[0].headers["referer"] == site.url + "/book"
                assert records[0].headers["accept-language"] == "en"
                assert all(record.http_version == "1.1" for record in site.requests)
                assert site.counts["/robots.txt"] == 1
            await site.wait_idle()

    run_scenario(run())


def test_real_requests_obey_concurrency_limit() -> None:
    async def run() -> None:
        async with AsyncMockSite() as site:
            async with AsyncFetcher(timeout=2, retries=0, concurrency=2, rate=10000) as client:
                async with asyncio.TaskGroup() as group:
                    tasks = [group.create_task(get(client, site, f"/slow/{i}")) for i in range(6)]
                assert all(task.result().content == BODY for task in tasks)
                assert site.peak == 2, site.snapshot()
                ids = {r.connection_id for r in site.requests if r.path.startswith("/slow/")}
                assert len(ids) <= 2, site.snapshot()
                assert site.counts["/robots.txt"] == 1
            await site.wait_idle()

    run_scenario(run())


def test_request_timestamps_include_robots_and_queued_rate_limits() -> None:
    async def run() -> None:
        async with AsyncMockSite() as site:
            async with AsyncFetcher(timeout=2, retries=0, concurrency=2, rate=10) as client:
                async with asyncio.TaskGroup() as group:
                    tasks = [group.create_task(get(client, site, f"/slow/{i}")) for i in range(4)]
                assert all(task.result().content == BODY for task in tasks)
            times = [record.timestamp for record in site.requests]
            assert len(times) == 5 and site.requests[0].path == "/robots.txt"
            assert all(b - a >= 0.08 for a, b in zip(times, times[1:], strict=False)), times
            assert site.peak <= 2
            await site.wait_idle()

    run_scenario(run())


@pytest.mark.parametrize("path", ["/wait-headers", "/drip", "/drip-chunked"])
def test_cancel_closes_socket_and_releases_slot(path: str) -> None:
    async def run() -> None:
        async with AsyncMockSite() as site:
            async with AsyncFetcher(timeout=2, retries=0, concurrency=1, rate=10000) as client:
                task = asyncio.create_task(get(client, site, path))
                await wait_stage(site, path)
                record = site.records(path)[0]
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 1)
                await site.wait_closed(record.connection_id)
                assert not record.complete and site.counts[path] == 1
                assert (await get(client, site, "/ok")).content == b"ok"
                assert site.records("/ok")[0].connection_id != record.connection_id
            await site.wait_idle()

    run_scenario(run())


@pytest.mark.parametrize("path", ["/wait-headers", "/drip", "/drip-chunked"])
def test_complete_deadline_bounds_headers_and_continuous_drip(path: str) -> None:
    async def run() -> None:
        async with AsyncMockSite() as site:
            async with AsyncFetcher(timeout=0.4, retries=0, concurrency=1, rate=10000) as client:
                assert (await get(client, site, "/ok")).content == b"ok"
                started = time.monotonic()
                with pytest.raises(NetworkError):
                    await asyncio.wait_for(client.get(site.url + path), 1.5)
                elapsed = time.monotonic() - started
                assert 0.3 <= elapsed < 1.2, (elapsed, site.snapshot())
                record = site.records(path)[0]
                if path == "/wait-headers":
                    assert record.headers_sent_at is None
                else:
                    assert len(record.chunk_timestamps) >= 3, site.snapshot()
                    assert record.chunk_timestamps[-1] - record.chunk_timestamps[0] >= 0.08
                assert not record.complete and site.counts[path] == 1
                await site.wait_closed(record.connection_id)
                assert (await get(client, site, "/ok")).content == b"ok"
            await site.wait_idle()

    run_scenario(run())


@pytest.mark.parametrize("path", ["/wait-headers", "/drip", "/drip-chunked"])
def test_context_exit_cancels_active_and_queued_requests(path: str) -> None:
    async def run() -> None:
        async with AsyncMockSite() as site:
            async with AsyncFetcher(timeout=2, retries=0, concurrency=1, rate=10000) as client:
                assert (await get(client, site, "/ok")).content == b"ok"
                active = asyncio.create_task(get(client, site, path))
                await wait_stage(site, path)
                queued = asyncio.create_task(get(client, site, "/queued"))
                await asyncio.sleep(0.02)
                assert not queued.done()
            results = await asyncio.wait_for(
                asyncio.gather(active, queued, return_exceptions=True), 1
            )
            assert all(isinstance(result, asyncio.CancelledError) for result in results), results
            assert site.counts["/queued"] == 0
            await site.wait_idle()
            with pytest.raises(ConfigError):
                await get(client, site, "/ok")

    run_scenario(run())


def test_chunked_body_is_read_and_oversize_body_releases_connection() -> None:
    async def run() -> None:
        async with AsyncMockSite() as site:
            async with AsyncFetcher(timeout=1, retries=0, rate=10000) as client:
                response = await get(client, site, "/chunked")
                assert response.content == BODY
                assert response.headers["transfer-encoding"] == "chunked"
            await site.wait_idle()
            async with AsyncFetcher(
                timeout=1, retries=0, concurrency=1, rate=10000, max_bytes=8
            ) as client:
                with pytest.raises(FetchError):
                    await get(client, site, "/chunked")
                assert (await get(client, site, "/ok")).content == b"ok"
            await site.wait_idle()

    run_scenario(run())
