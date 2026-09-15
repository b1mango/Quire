"""Core static-page capture with explicit verified resource recovery."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import cast

from .core_export import export_pdf
from .errors import ConfigError
from .fetch.session import AsyncFetcher
from .image.downloader import download_images
from .image.options import CompressionOptions
from .manga import _save_report, discover_page
from .models import MangaOptions, MangaResult, ProgressSink
from .parse.images import Candidate
from .parse.minidom import parse as parse_html
from .store.cache import remove_cached
from .store.ledger import Ledger
from .store.models import JsonValue, ResourceSpec, task_identity


def _identity_options(opts: MangaOptions, candidates: list[Candidate]) -> dict[str, JsonValue]:
    identity = cast(dict[str, JsonValue], json.loads(json.dumps(asdict(opts))))
    for key in ("concurrency", "rate", "timeout", "retries", "overwrite", "keep_images"):
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
    compression: CompressionOptions | None = None,
) -> MangaResult:
    opts = options or MangaOptions()
    encoding = compression or CompressionOptions()
    output = Path(out)
    if output.exists() and not opts.overwrite:
        raise ConfigError(f"目标已存在：{output}")
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
    )
    async with client:
        page = await client.get(url, referer=opts.referer)
        candidates, result = discover_page(url, opts, page)
        specs = [ResourceSpec(1, i + 1, c.url, c.referer) for i, c in enumerate(candidates)]
        identity = _identity_options(opts, candidates)
        task_id, _ = task_identity(url, identity, specs)
        with Ledger(root) as ledger:
            if ledger.contains(task_id) and not resume:
                raise ConfigError("已有相同采集任务，请使用 --resume 继续或选择其他 --workdir")
            ledger.create_task(url, identity, specs)
            if resume:
                ledger.recover(task_id)
            downloads = await download_images(
                client,
                ledger,
                task_id,
                candidates,
                concurrency=opts.concurrency,
                max_bytes=opts.max_bytes,
            )
            cache = ledger.root / "cache" / task_id
            result = replace(
                result,
                output=output,
                task_id=task_id,
                resources_reused=downloads.reused,
                images_dir=cache if opts.keep_images else None,
            )
            result = await export_pdf(
                ledger.root, downloads.snapshot, candidates, opts, encoding, result, progress
            )
            if result.partial:
                result = replace(
                    result, warnings=result.warnings + ("恢复缓存已保留，可用 --resume 重试",)
                )
            result = _save_report(
                replace(
                    result, bytes_out=output.stat().st_size, elapsed_s=time.monotonic() - started
                ),
                opts.overwrite,
            )
            if result.partial or result.target_met is False:
                return result
            if not opts.keep_images and result.report is not None:
                for record in downloads.snapshot.resources:
                    if record.local_path is not None:
                        remove_cached(ledger.root, task_id, record.local_path)
            return result
