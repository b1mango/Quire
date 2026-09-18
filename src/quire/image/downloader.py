"""Bounded async image workers with one event-loop-owned ledger writer."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from ..errors import BlockedError, FetchError
from ..parse.images import Candidate
from ..store.cache import publish_bytes
from ..store.ledger import Ledger
from ..store.models import FailureCode, ResourceRecord, TaskSnapshot
from .codec import inspect_image


class ImageResponse(Protocol):
    @property
    def content(self) -> bytes: ...


class ImageFetcher(Protocol):
    async def get(
        self, url: str, *, referer: str | None = None, robots: bool = True
    ) -> ImageResponse: ...


@dataclass(frozen=True, slots=True)
class DownloadResult:
    snapshot: TaskSnapshot
    reused: int


async def download_images(
    client: ImageFetcher,
    ledger: Ledger,
    task_id: str,
    candidates: list[Candidate],
    *,
    concurrency: int,
    max_bytes: int,
    stop: Callable[[], bool] | None = None,
) -> DownloadResult:
    snapshot = ledger.snapshot(task_id)
    reused = sum(item.status == "done" for item in snapshot.resources)
    if snapshot.status == "done":
        return DownloadResult(snapshot, reused)
    ledger.start(task_id)
    pending = iter(
        (record, candidate)
        for record, candidate in zip(snapshot.resources, candidates, strict=True)
        if record.status == "pending"
    )

    async def worker() -> None:
        for record, candidate in pending:
            if stop is not None and stop():
                break  # 暂停：不再派发新下载，进行中的请求已收尾
            spec = record.spec
            ledger.claim(task_id, spec.chapter, spec.page)
            try:
                await _download_one(client, ledger, task_id, record, candidate, max_bytes)
            except asyncio.CancelledError:
                ledger.fail(task_id, spec.chapter, spec.page, "cancelled")
                raise

    tasks = [
        asyncio.create_task(worker()) for _ in range(min(concurrency, len(snapshot.resources)))
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    settled = ledger.snapshot(task_id) if stop is not None and stop() else ledger.finish(task_id)
    return DownloadResult(settled, reused)


async def _download_one(
    client: ImageFetcher,
    ledger: Ledger,
    task_id: str,
    record: ResourceRecord,
    candidate: Candidate,
    max_bytes: int,
) -> None:
    failure: FailureCode = "network"
    spec = record.spec
    for url in (candidate.url, *candidate.alternatives):
        try:
            response = await client.get(url, referer=candidate.referer or None, robots=False)
        except BlockedError:
            failure = "blocked"
            continue
        except FetchError:
            failure = "network"
            continue
        if len(response.content) > max_bytes:
            failure = "invalid_image"
            continue
        try:
            info = inspect_image(response.content)
        except FetchError:
            failure = "invalid_image"
            continue
        extension = "jpg" if info.format.lower() == "jpeg" else info.format.lower()
        relative = publish_bytes(
            ledger.root,
            task_id,
            f"{spec.chapter:05d}-{spec.page:06d}.{extension}",
            response.content,
        )
        ledger.complete(task_id, spec.chapter, spec.page, relative)
        return
    ledger.fail(task_id, spec.chapter, spec.page, failure)
