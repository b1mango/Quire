"""Durable export lifecycle, result reuse and verified source cleanup."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

from .errors import ConfigError, LedgerError
from .export_commit import matches, publish, sync_directory, sync_file
from .export_options import output_paths
from .export_receipt import ExportReceipt, PublishedFile, write_receipt
from .manga import report_payload
from .models import MangaOptions, MangaResult
from .store.cache import FileStamp, _identity, remove_cached
from .store.cache import fingerprint as cache_fingerprint
from .store.export_files import Stamp, check_workspace, clean_workspace, fingerprint, make_workspace
from .store.models import TaskSnapshot


def preflight(
    output: Path,
    formats: tuple[str, ...],
    *,
    overwrite: bool,
    owned: ExportReceipt | None = None,
) -> tuple[Stamp | None, ...]:
    paths = (*output_paths(output, formats), output.with_suffix(".report.json"))
    stamps = tuple(fingerprint(path) for path in paths)
    allowed = {item.destination: item.stamp for item in owned.files} if owned else {}
    if owned is not None and owned.state == "building" and owned.previous:
        allowed = {
            path: stamp
            for path, stamp in zip(paths, owned.previous, strict=True)
            if stamp is not None
        }
    for path, stamp in zip(paths, stamps, strict=True):
        if stamp is not None and not overwrite and allowed.get(path) != stamp:
            raise ConfigError(f"目标已存在：{path}；重建须明确指定 --overwrite")
    return stamps


def begin(
    root: Path, key: str, output: Path, formats: tuple[str, ...], before: tuple[Stamp | None, ...]
) -> ExportReceipt:
    workspace, identity = make_workspace(output.parent, key)
    receipt = ExportReceipt(key, "building", workspace, identity, formats, previous=before)
    write_receipt(root, receipt)
    return receipt


def prepare(
    root: Path,
    receipt: ExportReceipt,
    result: MangaResult,
    before: tuple[Stamp | None, ...],
    snapshot: TaskSnapshot,
) -> ExportReceipt:
    check_workspace(receipt.workspace, receipt.workspace_identity)
    destinations = output_paths(result.output, receipt.formats)
    report = result.output.with_suffix(".report.json")
    final = replace(
        result,
        report=report,
        artifacts=tuple(
            replace(a, path=p) for a, p in zip(result.artifacts, destinations, strict=True)
        ),
    )
    candidate_report = receipt.workspace / "book.report.json"
    descriptor = os.open(
        candidate_report, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(
            json.dumps(report_payload(final), ensure_ascii=False, indent=2).encode("utf-8")
        )
        handle.flush()
        os.fsync(handle.fileno())
    candidates = (*(a.path for a in result.artifacts), candidate_report)
    files = tuple(
        PublishedFile(
            kind,
            str(candidate.relative_to(receipt.workspace)),
            destination,
            sync_file(candidate),
            prior,
        )
        for kind, candidate, destination, prior in zip(
            (*receipt.formats, "report"), candidates, (*destinations, report), before, strict=True
        )
    )
    for directory in {path.parent for path in candidates}:
        sync_directory(directory)
    sources = tuple(
        FileStamp(resource.local_path, resource.sha256, resource.size)
        for resource in snapshot.resources
        if resource.local_path is not None
        and resource.sha256 is not None
        and resource.size is not None
    )
    prepared = replace(receipt, state="prepared", files=files, result=final, sources=sources)
    check_workspace(receipt.workspace, receipt.workspace_identity)
    write_receipt(root, prepared)
    return prepared


def cleanup(root: Path, receipt: ExportReceipt, opts: MangaOptions) -> ExportReceipt:
    result = receipt.result
    assert result is not None
    metadata = {item.destination: _identity(item.destination.lstat()) for item in receipt.files}
    if not matches(receipt):
        raise ConfigError("成品校验失败，原图已保留")
    if receipt.cleaned:
        return receipt
    if not opts.keep_images and not result.partial and result.target_met is not False:
        assert result.task_id is not None
        for source in receipt.sources:
            _check_outputs(metadata)
            remove_cached(root, result.task_id, source.path, expected=source)
    _check_outputs(metadata)
    clean_workspace(receipt.workspace, receipt.workspace_identity, receipt.formats)
    _check_outputs(metadata)
    cleaned = replace(receipt, cleaned=True)
    write_receipt(root, cleaned)
    return cleaned


def _check_outputs(metadata: dict[Path, tuple[int, ...]]) -> None:
    try:
        if any(_identity(path.lstat()) != value for path, value in metadata.items()):
            raise LedgerError("Output changed during cleanup; remaining sources preserved")
    except OSError as exc:
        raise LedgerError("Output disappeared during cleanup; remaining sources preserved") from exc


async def complete(root: Path, receipt: ExportReceipt, opts: MangaOptions) -> MangaResult:
    await publish(receipt)
    receipt = replace(receipt, state="complete")
    write_receipt(root, receipt)
    cleanup(root, receipt, opts)
    assert receipt.result is not None
    return receipt.result


async def recover_export(
    root: Path, receipt: ExportReceipt, snapshot: TaskSnapshot, opts: MangaOptions
) -> MangaResult | None:
    if receipt.state == "building":
        clean_workspace(receipt.workspace, receipt.workspace_identity, receipt.formats)
        return None
    interrupted = receipt.state == "prepared"
    if interrupted:
        result = await complete(root, receipt, opts)
    else:
        if not matches(receipt):
            if not opts.overwrite:
                raise ConfigError("已完成成品缺失或已修改；重建须明确指定 --overwrite")
            clean_workspace(receipt.workspace, receipt.workspace_identity, receipt.formats)
            return None
        cleanup(root, receipt, opts)
        assert receipt.result is not None
        result = receipt.result
    if result.partial:
        return None
    if opts.keep_images:
        for resource in snapshot.resources:
            if resource.local_path is None:
                return None
            try:
                stamp = cache_fingerprint(
                    root, snapshot.task_id, resource.local_path, expected_size=resource.size
                )
            except LedgerError:
                return None
            if stamp.sha256 != resource.sha256:
                return None
    return replace(result, resources_reused=0, artifacts_reused=True, export_recovered=interrupted)
