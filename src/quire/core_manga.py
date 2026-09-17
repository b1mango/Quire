"""Core page capture with explicit verified resource recovery."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import asdict, replace
from pathlib import Path
from typing import cast

from .assemble.models import clean_metadata_text
from .capture_plan import MangaPlan
from .core_export import export_books
from .core_publish import begin, complete, preflight, prepare, recover_export
from .errors import ConfigError, LedgerError, PausedError
from .export_options import output_paths
from .export_receipt import export_key, read_receipt
from .fetch.async_policy import HostPace
from .fetch.browser import RenderOptions
from .fetch.session import AsyncFetcher
from .image.downloader import download_images
from .image.options import CompressionOptions
from .models import MangaOptions, MangaResult, ProgressSink
from .parse.images import Candidate
from .parse.minidom import parse as parse_html
from .store.ledger import Ledger
from .store.models import JsonValue, ResourceSpec, task_identity


def _identity_options(opts: MangaOptions, candidates: list[Candidate]) -> dict[str, JsonValue]:
    identity = cast(dict[str, JsonValue], json.loads(json.dumps(asdict(opts))))
    for key in ("concurrency", "rate", "timeout", "retries", "overwrite", "keep_images"):
        identity.pop(key)
    for key in ("remove", "next_selector", "follow_pages"):
        if not identity[key]:
            identity.pop(key)
    identity["alternatives"] = [list(c.alternatives) for c in candidates]
    return identity


async def run_core_manga(
    url: str,
    out: Path | str,
    *,
    options: MangaOptions | None = None,
    workdir: Path | str | None = None,
    resume: bool = False,
    fetcher: AsyncFetcher | None = None,
    progress: ProgressSink | None = None,
    on_task: Callable[[str], None] | None = None,
    compression: CompressionOptions | None = None,
    formats: tuple[str, ...] = ("pdf",),
    render: RenderOptions | None = None,
    plan: MangaPlan | None = None,
    shared_fetcher: bool = False,
    stop: Callable[[], bool] | None = None,
    pace: HostPace | None = None,
) -> MangaResult:
    opts = options or MangaOptions()
    encoding = compression or CompressionOptions()
    destinations = output_paths(Path(out).absolute(), formats)
    output = destinations[0]
    for destination in destinations:
        if destination.is_symlink() or (destination.exists() and not destination.is_file()):
            raise ConfigError(f"目标不是普通文件：{destination}")
        if destination.exists() and not opts.overwrite and not resume:
            raise ConfigError(f"目标已存在：{destination}")
    if not resume:
        preflight(output, formats, overwrite=opts.overwrite)
    if opts.selector:
        parse_html("").select(opts.selector)
    started = time.monotonic()
    root = Path(workdir) if workdir else output.parent / ".quire-core"
    client = fetcher or AsyncFetcher(
        timeout=opts.timeout,
        retries=opts.retries,
        concurrency=opts.concurrency,
        rate=opts.rate,
        max_bytes=opts.max_bytes,
        pace=pace,
    )
    async with nullcontext(client) if shared_fetcher else client:
        if plan is None:
            from .core_discovery import discover_manga

            candidates, result = await discover_manga(client, url, opts, render)
            specs = [ResourceSpec(1, i + 1, c.url, c.referer) for i, c in enumerate(candidates)]
        else:
            candidates, result, specs = list(plan.candidates), plan.result, list(plan.specs)
        result = replace(result, title=clean_metadata_text(result.title))
        identity = _identity_options(opts, candidates)
        if plan is not None:
            identity["title"] = result.title
            identity["chapter_titles"] = list(plan.chapter_titles)
        task_id, _ = task_identity(url, identity, specs)
        with Ledger(root) as ledger:
            if ledger.contains(task_id) and not resume:
                raise ConfigError("已有相同采集任务，请使用 --resume 继续或选择其他 --workdir")
            ledger.create_task(url, identity, specs)
            key = export_key(task_id, result.title, output, formats, opts, encoding)
            receipt = read_receipt(ledger.root, key, output, formats) if resume else None
            if receipt is not None:
                if receipt.result is not None and (
                    receipt.result.task_id != task_id or receipt.result.title != result.title
                ):
                    raise LedgerError("Export receipt does not match the current task")
                reused = await recover_export(ledger.root, receipt, ledger.snapshot(task_id), opts)
                if reused is not None:
                    if on_task:
                        on_task(task_id)
                    return replace(reused, elapsed_s=time.monotonic() - started)
            before = preflight(
                output,
                formats,
                overwrite=opts.overwrite,
                owned=receipt,
            )
            if resume:
                ledger.recover(task_id)
            if on_task:
                on_task(task_id)
            downloads = await download_images(
                client,
                ledger,
                task_id,
                candidates,
                concurrency=opts.concurrency,
                max_bytes=opts.max_bytes,
                stop=stop,
            )
            if stop is not None and stop():
                raise PausedError("任务已暂停，已下载的部分保留在缓存里")
            cache = ledger.root / "cache" / task_id
            result = replace(
                result,
                output=output,
                task_id=task_id,
                resources_reused=downloads.reused,
                images_dir=cache if opts.keep_images else None,
            )
            output.parent.mkdir(parents=True, exist_ok=True)
            receipt = begin(ledger.root, key, output, formats, before)
            result = await export_books(
                ledger.root,
                downloads.snapshot,
                candidates,
                opts,
                encoding,
                result,
                progress,
                workspace=receipt.workspace,
                formats=formats,
                source_url=url,
                chapter_titles=plan.chapter_titles if plan else (),
            )
            if result.partial:
                result = replace(
                    result, warnings=result.warnings + ("恢复缓存已保留，可用 --resume 重试",)
                )
            receipt = prepare(
                ledger.root,
                receipt,
                replace(result, elapsed_s=time.monotonic() - started),
                before,
                downloads.snapshot,
            )
            return await complete(ledger.root, receipt, opts)
