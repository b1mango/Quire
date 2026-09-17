"""Size-bounded volumes: measure actual artifacts, split only at chapter boundaries."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from .capture_plan import MangaPlan
from .core_export import export_books
from .core_manga import _identity_options
from .core_reassemble import export_cached
from .errors import ConfigError
from .fetch.session import AsyncFetcher
from .image.downloader import download_images
from .image.options import CompressionOptions
from .models import MangaOptions, MangaResult, ProgressSink
from .store.export_files import clean_workspace, make_workspace
from .store.ledger import Ledger
from .store.models import TaskSnapshot


async def export_by_size(
    url: str,
    output: Path,
    root: Path,
    plan: MangaPlan,
    opts: MangaOptions,
    compression: CompressionOptions,
    formats: tuple[str, ...],
    limit: int,
    client: AsyncFetcher,
    progress: ProgressSink | None,
    on_volume: Callable[[MangaResult], None] | None,
    on_task: Callable[[str], None] | None = None,
) -> tuple[MangaResult, ...]:
    candidates = list(plan.candidates)
    identity = _identity_options(opts, candidates)
    identity["title"] = plan.result.title
    identity["chapter_titles"] = list(plan.chapter_titles)
    encoding = (
        compression
        if compression.preset == "lossless"
        else replace(compression, target_bytes=min(compression.target_bytes or limit, limit))
    )
    with Ledger(root) as ledger:
        task_id = ledger.create_task(url, identity, plan.specs)
        ledger.recover(task_id)
        if on_task:
            on_task(task_id)
        downloaded = await download_images(
            client,
            ledger,
            task_id,
            candidates,
            concurrency=opts.concurrency,
            max_bytes=opts.max_bytes,
        )
        snapshot = downloaded.snapshot
        chapters = sorted({r.spec.chapter for r in snapshot.resources})

        def subset(first: int, last: int) -> tuple[TaskSnapshot, list[int]]:
            indices = [
                i for i, r in enumerate(snapshot.resources) if first <= r.spec.chapter <= last
            ]
            return replace(
                snapshot, resources=tuple(snapshot.resources[i] for i in indices)
            ), indices

        async def measure(first: int, last: int) -> int:
            part, indices = subset(first, last)
            workspace, inode = make_workspace(output, task_id)
            try:
                measured = await export_books(
                    root,
                    part,
                    [candidates[i] for i in indices],
                    opts,
                    encoding,
                    replace(plan.result, title=f"{plan.result.title} · 第{len(results) + 1}卷"),
                    None,
                    workspace=workspace,
                    formats=formats,
                    source_url=url,
                    chapter_titles=plan.chapter_titles,
                )
                return max(a.bytes for a in measured.artifacts)
            finally:
                clean_workspace(workspace, inode, formats)

        # Greedy longest fitting prefix found by bounded binary search. Monotonicity
        # isn't assumed for correctness: every selected volume is measured itself.
        results: list[MangaResult] = []
        # The final count is unknown during streaming. Pad to the chapter-count
        # upper bound so already delivered filenames never have to be renamed.
        width = len(str(len(chapters)))
        start = 0
        while start < len(chapters):
            low, high, best = start, len(chapters) - 1, start - 1
            while low <= high:
                middle = (low + high) // 2
                if await measure(chapters[start], chapters[middle]) <= limit:
                    best, low = middle, middle + 1
                else:
                    high = middle - 1
            if best < start:
                raise ConfigError(
                    f"第 {chapters[start]} 章单章成品超过 {limit} bytes，无法在章边界切分；"
                    f"已交付 {len(results)} 卷。请增大上限或降低画质，原图已保留"
                )
            first, last = chapters[start], chapters[best]
            index = len(results) + 1
            part, indices = subset(first, last)
            result = await export_cached(
                root,
                part,
                [candidates[i] for i in indices],
                output / f"第{index:0{width}d}卷.{formats[0]}",
                opts,
                encoding,
                formats,
                replace(plan.result, title=f"{plan.result.title} · 第{index}卷"),
                url,
                chapter_titles=plan.chapter_titles,
                progress=progress,
            )
            if any(a.bytes > limit for a in result.artifacts):
                raise ConfigError("分卷实测体积与预检不符，请查看报告；已生成的卷保留")
            results.append(result)
            if on_volume:
                on_volume(result)
            start = best + 1
        return tuple(results)
