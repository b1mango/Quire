"""Real loopback HTTP/1.1 session smoke; no external network access."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

from quire.fetch.session import AsyncFetcher
from quire.fetch.simple import Response

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.mock_site.async_server import BODY, AsyncMockSite  # noqa: E402


async def _get(client: AsyncFetcher, site: AsyncMockSite, path: str) -> Response:
    return await asyncio.wait_for(client.get(site.url + path), 5)


async def _exercise(site: AsyncMockSite, report: dict[str, object]) -> None:
    responses: list[Response] = []
    async with AsyncFetcher(timeout=20, retries=3, concurrency=4, rate=4, max_bytes=1024) as client:
        responses.append(await _get(client, site, "/body"))
        responses.append(await _get(client, site, "/body"))
        records = site.records("/body")
        report["reused_connection"] = records[0].connection_id == records[1].connection_id
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(_get(client, site, f"/slow/{i}")) for i in range(8)]
            tasks.append(group.create_task(_get(client, site, "/chunked")))
        responses.extend(task.result() for task in tasks)
        report["readsuccess"] = sum(r.status == 200 and r.content == BODY for r in responses)
        report["read_bytes"] = sum(len(r.content) for r in responses)
        assert report["reused_connection"], site.snapshot()
        assert report["readsuccess"] == len(responses) == 11
        assert 1 < site.peak <= 4, site.snapshot()
        waiting = asyncio.create_task(_get(client, site, "/wait-headers"))
        await site.wait_for(lambda: bool(site.records("/wait-headers")))
        assert site.records("/wait-headers")[0].headers_sent_at is None
    results = await asyncio.wait_for(asyncio.gather(waiting, return_exceptions=True), 1)
    report["cancelled_on_close"] = isinstance(results[0], asyncio.CancelledError)
    assert report["cancelled_on_close"], results
    await site.wait_idle()
    report["client_closed_connections"] = site.open_connections == 0
    timestamps = [request.timestamp for request in site.requests]
    gaps = [b - a for a, b in zip(timestamps, timestamps[1:], strict=False)]
    report["min_request_gap_seconds"] = min(gaps)
    assert min(gaps) >= 0.20, gaps
    assert site.counts["/robots.txt"] == 1
    assert all(request.http_version == "1.1" for request in site.requests)


async def run_smoke() -> dict[str, object]:
    baseline = asyncio.all_tasks()
    started = time.monotonic()
    report: dict[str, object] = {"ok": False, "readsuccess": 0}
    site = AsyncMockSite()
    try:
        async with asyncio.timeout(15), site:
            await _exercise(site, report)
        report["ok"] = True
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        await asyncio.sleep(0)
        remaining = asyncio.all_tasks() - baseline
        report["background_tasks_after_close"] = len(remaining)
        for task in remaining:
            task.cancel()
        if remaining:
            await asyncio.wait_for(asyncio.gather(*remaining, return_exceptions=True), 2)
            report["ok"] = False
        report.update(site.snapshot())
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return report


def main() -> int:
    report = asyncio.run(run_smoke())
    output = ROOT / "output" / "session-smoke"
    output.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, indent=2, ensure_ascii=True) + "\n"
    (output / "report.json").write_text(payload, encoding="utf-8")
    sys.stdout.write(payload)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
