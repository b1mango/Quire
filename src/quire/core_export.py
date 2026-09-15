"""Build bounded compression trials and publish the selected complete PDF."""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import replace
from pathlib import Path

from .assemble.pdf_min import MiniPdfWriter
from .errors import FetchError
from .image.codec import inspect_image
from .image.compress import encode_pages
from .image.options import CompressionOptions, Encoding
from .image.probe import ImageProbe
from .manga import _missing
from .models import MangaOptions, MangaResult, ProgressSink
from .parse.images import Candidate, postfilter
from .store.cache import read_cached
from .store.models import ResourceRecord, TaskSnapshot
from .workspace import atomic_output


def _source_bytes(root: Path, snapshot: TaskSnapshot, record: ResourceRecord) -> bytes:
    assert record.local_path is not None and record.sha256 is not None and record.size is not None
    return read_cached(
        root, snapshot.task_id, record.local_path, sha256=record.sha256, size=record.size
    )


def _add_source(
    pdf: MiniPdfWriter,
    data: bytes,
    candidate: Candidate,
    index: int,
    total: int,
    opts: MangaOptions,
    compression: CompressionOptions,
    encoding: Encoding,
    result: MangaResult,
) -> MangaResult:
    info = inspect_image(data)
    probe = ImageProbe(format=info.format, width=info.width, height=info.height, complete=True)
    _, rejected = postfilter([(candidate, probe, len(data))], opts.policy)
    if rejected:
        result = replace(
            result,
            pages_rejected=result.pages_rejected + 1,
            rejections=result.rejections + tuple(rejected),
        )
        return _missing(pdf, result, index, total, candidate, rejected[0].reason)
    if info.frames > 1:
        result = replace(
            result, warnings=result.warnings + (f"来源图片 {index + 1} 为多帧，仅采第一帧",)
        )
    for page in encode_pages(data, encoding, compression):
        if not pdf.add_image_bytes(page.data):
            raise FetchError("Encoded image is not supported by the PDF writer")
        result = replace(result, pages_written=result.pages_written + 1)
    return result


async def _trial(
    output: Path,
    root: Path,
    snapshot: TaskSnapshot,
    candidates: list[Candidate],
    opts: MangaOptions,
    compression: CompressionOptions,
    encoding: Encoding,
    result: MangaResult,
    progress: ProgressSink | None,
) -> MangaResult:
    total = len(candidates)
    with MiniPdfWriter(output, title=result.title, dpi=opts.dpi, paper=opts.paper) as pdf:
        for index, (record, candidate) in enumerate(
            zip(snapshot.resources, candidates, strict=True)
        ):
            if record.local_path is None:
                result = _missing(
                    pdf, result, index, total, candidate, record.error_code or "network"
                )
            else:
                data = _source_bytes(root, snapshot, record)
                result = _add_source(
                    pdf, data, candidate, index, total, opts, compression, encoding, result
                )
            if progress:
                progress.update(index + 1, total, result)
            await asyncio.sleep(0)
    return replace(result, bytes_out=output.stat().st_size)


async def export_pdf(
    root: Path,
    snapshot: TaskSnapshot,
    candidates: list[Candidate],
    opts: MangaOptions,
    compression: CompressionOptions,
    result: MangaResult,
    progress: ProgressSink | None,
) -> MangaResult:
    result.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".quire-encode-", dir=result.output.parent
    ) as directory:
        best: MangaResult | None = None
        selected: Encoding | None = None
        selected_path: Path | None = None
        rounds = 0
        for rounds, encoding in enumerate(compression.passes(), 1):
            trial_path = Path(directory) / f"pass-{rounds}.pdf"
            trial = await _trial(
                trial_path,
                root,
                snapshot,
                candidates,
                opts,
                compression,
                encoding,
                result,
                progress,
            )
            if best is None or trial.bytes_out < best.bytes_out:
                if selected_path is not None:
                    selected_path.unlink()
                best, selected, selected_path = trial, encoding, trial_path
            else:
                trial_path.unlink()
            if compression.target_bytes is None or best.bytes_out <= compression.target_bytes:
                break
        assert best is not None and selected is not None and selected_path is not None
        met = (
            None if compression.target_bytes is None else best.bytes_out <= compression.target_bytes
        )
        if met is False:
            best = replace(
                best,
                warnings=best.warnings
                + (
                    f"未达目标 {compression.target_bytes} bytes；已测最小 {best.bytes_out} bytes，已保留恢复缓存",
                ),
            )
        await _publish(selected_path, result.output, overwrite=opts.overwrite)
        return replace(
            best,
            source_resources=len(candidates),
            compression=compression.preset,
            target_bytes=compression.target_bytes,
            target_met=met,
            encoding_rounds=rounds,
            quality=selected.quality or None,
            max_edge=selected.max_edge or None,
        )


async def _publish(source_path: Path, destination: Path, *, overwrite: bool) -> None:
    with (
        source_path.open("rb") as source,
        atomic_output(destination, overwrite=overwrite) as target,
    ):
        while block := source.read(1024 * 1024):
            target.write(block)
            await asyncio.sleep(0)
        # The final checkpoint is before atomic_output commits the destination.
        await asyncio.sleep(0)
