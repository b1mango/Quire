"""Offline export from verified ledger sources; never fetch an entry page."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import replace
from pathlib import Path

from .core_export import export_books
from .core_publish import begin, complete, preflight, prepare, recover_export
from .errors import ConfigError, LedgerError
from .export_options import output_paths
from .export_receipt import export_key, read_receipt
from .image.options import CompressionOptions
from .models import MangaOptions, MangaResult, ProgressSink
from .parse.images import Candidate, FilterPolicy
from .store.cache import fingerprint
from .store.ledger import Ledger
from .store.models import TaskSnapshot


async def export_cached(
    root: Path,
    snapshot: TaskSnapshot,
    candidates: list[Candidate],
    output: Path,
    opts: MangaOptions,
    compression: CompressionOptions,
    formats: tuple[str, ...],
    result: MangaResult,
    source_url: str,
    *,
    chapter_titles: tuple[str, ...] = (),
    progress: ProgressSink | None = None,
) -> MangaResult:
    started = time.monotonic()
    output = output_paths(output.absolute(), formats)[0]
    output.parent.mkdir(parents=True, exist_ok=True)
    key = export_key(snapshot.task_id, result.title, output, formats, opts, compression)
    # A size-split series shares a source ledger across volumes. The selected
    # resources are part of the export identity, independently of the filename.
    selected = [
        [r.spec.chapter, r.spec.page, r.spec.url, r.sha256, r.size] for r in snapshot.resources
    ]
    key = hashlib.sha256((key + json.dumps(selected, ensure_ascii=True)).encode()).hexdigest()
    receipt = read_receipt(root, key, output, formats)
    if receipt:
        restored = await recover_export(root, receipt, snapshot, opts)
        if restored:
            return restored
    before = preflight(output, formats, overwrite=opts.overwrite, owned=receipt)
    receipt = begin(root, key, output, formats, before)
    result = replace(
        result,
        output=output,
        task_id=snapshot.task_id,
        resources_reused=len(snapshot.resources),
        images_dir=root / "cache" / snapshot.task_id if opts.keep_images else None,
    )
    result = await export_books(
        root,
        snapshot,
        candidates,
        opts,
        compression,
        result,
        progress,
        workspace=receipt.workspace,
        formats=formats,
        source_url=source_url,
        chapter_titles=chapter_titles,
    )
    prepared = prepare(
        root, receipt, replace(result, elapsed_s=time.monotonic() - started), before, snapshot
    )
    return await complete(root, prepared, opts)


async def run_reassemble(
    task_id: str,
    out: Path | str,
    *,
    workdir: Path | str,
    formats: tuple[str, ...] = ("pdf",),
    compression: CompressionOptions | None = None,
    overwrite: bool = False,
    progress: ProgressSink | None = None,
) -> MangaResult:
    if not re.fullmatch(r"[0-9a-f]{64}", task_id):
        raise ConfigError("请提供报告中的完整 64 位任务 ID")
    root = Path(workdir).absolute()
    if not (root / "ledger.db").is_file():
        raise ConfigError("此目录没有采集账本，请检查 --workdir")
    with Ledger(root) as ledger:
        source, identity = ledger.describe(task_id)
        if "clean_version" in identity:
            raise ConfigError("reassemble 用于漫画原图；小说可用 novel --resume 从正文缓存重新导出")
        snapshot = ledger.snapshot(task_id)
        for record in snapshot.resources:
            try:
                if record.status != "done" or record.local_path is None:
                    raise LedgerError("Missing source")
                stamp = fingerprint(root, task_id, record.local_path, expected_size=record.size)
                if stamp.sha256 != record.sha256:
                    raise LedgerError("Source differs")
            except LedgerError as exc:
                raise ConfigError(
                    "该任务未保留完整原图或原图已损坏，需重抓或下次加 --keep-images"
                ) from exc
        policy_data = identity.get("policy")
        if not isinstance(policy_data, dict):
            raise LedgerError("任务缺少图片过滤配置")
        options = {
            k: v
            for k, v in identity.items()
            if k
            in {
                "selector",
                "attrs",
                "order",
                "dpi",
                "paper",
                "referer",
                "first",
                "last",
                "max_bytes",
                "remove",
                "next_selector",
            }
        }
        opts = MangaOptions(
            **options,  # type: ignore[arg-type]
            policy=FilterPolicy(**policy_data),  # type: ignore[arg-type]
            keep_images=True,
            overwrite=overwrite,
        )
        titles = identity.get("chapter_titles", [])
        if not isinstance(titles, list) or any(not isinstance(t, str) for t in titles):
            raise LedgerError("章节标题记录损坏")
        candidates = [
            Candidate(r.spec.url, i, referer=r.spec.referer)
            for i, r in enumerate(snapshot.resources)
        ]
        result = MangaResult(Path(out), title=str(identity.get("title", Path(out).stem)))
        return await export_cached(
            root,
            snapshot,
            candidates,
            Path(out),
            opts,
            compression or CompressionOptions(),
            formats,
            result,
            source,
            chapter_titles=tuple(str(t) for t in titles),
            progress=progress,
        )
