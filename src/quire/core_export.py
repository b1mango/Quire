"""Build shared encoding trials and publish a selected set of book formats."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack, closing
from dataclasses import replace
from pathlib import Path

from .assemble.archive import ArchiveWriter
from .assemble.models import clean_metadata_text
from .assemble.pdf import CorePdfWriter
from .core_pages import source_pages
from .image.options import CompressionOptions, Encoding
from .models import ArtifactResult, MangaOptions, MangaResult, ProgressSink
from .parse.images import Candidate
from .store.export_files import check_workspace, clean_workspace
from .store.models import TaskSnapshot

_MEASURE_THRESHOLD = 50_000_000


async def _trial(
    directory: Path,
    root: Path,
    snapshot: TaskSnapshot,
    candidates: list[Candidate],
    opts: MangaOptions,
    compression: CompressionOptions,
    encoding: Encoding,
    result: MangaResult,
    progress: ProgressSink | None,
    formats: tuple[str, ...],
    source_url: str,
    chapter_titles: tuple[str, ...] = (),
) -> MangaResult:
    directory.mkdir(exist_ok=True)
    with ExitStack() as stack:
        writers: list[CorePdfWriter | ArchiveWriter] = []
        for format in formats:
            path = directory / f"book.{format}"
            if format == "pdf":
                writers.append(
                    stack.enter_context(
                        CorePdfWriter(
                            path,
                            title=result.title,
                            source_url=source_url,
                            dpi=opts.dpi,
                            paper=opts.paper,
                        )
                    )
                )
            else:
                writers.append(
                    stack.enter_context(
                        ArchiveWriter(
                            path, format=format, title=result.title, source_url=source_url
                        )
                    )
                )
        for index, (record, candidate) in enumerate(
            zip(snapshot.resources, candidates, strict=True)
        ):
            with closing(
                source_pages(
                    root, snapshot, record, candidate, index, opts, compression, encoding, result
                )
            ) as pages:
                for page, updated in pages:
                    for writer in writers:
                        if isinstance(writer, CorePdfWriter) and chapter_titles:
                            writer.chapter = chapter_titles[record.spec.chapter - 1]
                        writer.add_page(page)
                    result = updated
                    await asyncio.sleep(0)
            if progress:
                progress.update(index + 1, len(candidates), result)
            await asyncio.sleep(0)
    artifacts = tuple(
        ArtifactResult(
            format,
            path,
            path.stat().st_size,
            None
            if compression.target_bytes is None
            else path.stat().st_size <= compression.target_bytes,
        )
        for format in formats
        for path in (directory / f"book.{format}",)
    )
    return replace(result, bytes_out=artifacts[0].bytes, artifacts=artifacts)


async def export_books(
    root: Path,
    snapshot: TaskSnapshot,
    candidates: list[Candidate],
    opts: MangaOptions,
    compression: CompressionOptions,
    result: MangaResult,
    progress: ProgressSink | None,
    *,
    workspace: Path,
    formats: tuple[str, ...],
    source_url: str,
    chapter_titles: tuple[str, ...] = (),
) -> MangaResult:
    best: MangaResult | None = None
    selected: Encoding | None = None
    best_round = 0
    inodes: dict[int, tuple[int, int]] = {}
    # Large sources: measure formats sequentially, retaining sizes instead of all
    # losing candidates. Rebuild only the selected encoding after the search.
    measure = len(formats) > 1 and sum(r.size or 0 for r in snapshot.resources) > _MEASURE_THRESHOLD

    async def run(round_no: int, encoding: Encoding, chosen: tuple[str, ...]) -> MangaResult:
        directory = workspace / f"pass-{round_no}"
        directory.mkdir()
        stat = directory.lstat()
        inodes[round_no] = (stat.st_dev, stat.st_ino)
        trial = await _trial(
            directory,
            root,
            snapshot,
            candidates,
            opts,
            compression,
            encoding,
            result,
            progress,
            chosen,
            clean_metadata_text(source_url),
            chapter_titles,
        )
        check_workspace(directory, inodes[round_no])
        return trial

    def discard(round_no: int, chosen: tuple[str, ...]) -> None:
        clean_workspace(workspace / f"pass-{round_no}", inodes[round_no], chosen)

    for rounds, encoding in enumerate(compression.passes(), 1):
        if measure:
            measured: list[ArtifactResult] = []
            trial = None
            for fmt in formats:
                part = await run(rounds, encoding, (fmt,))
                trial = trial or part
                measured.extend(part.artifacts)
                discard(rounds, (fmt,))
            assert trial is not None
            trial = replace(trial, artifacts=tuple(measured))
        else:
            trial = await run(rounds, encoding, formats)
        better = best is None or max(a.bytes for a in trial.artifacts) < max(
            a.bytes for a in best.artifacts
        )
        if better:
            if best is not None and not measure:
                discard(best_round, formats)
            best, selected, best_round = trial, encoding, rounds
        elif not measure:
            discard(rounds, formats)
        assert best is not None
        if all(a.target_met is not False for a in best.artifacts):
            break
    assert best is not None and selected is not None
    if measure:
        best = await run(best_round, selected, formats)
    met = None if compression.target_bytes is None else all(a.target_met for a in best.artifacts)
    if met is False:
        best = replace(
            best,
            warnings=best.warnings
            + (
                f"未达目标 {compression.target_bytes} bytes；已测最小组合最大成品 {max(a.bytes for a in best.artifacts)} bytes，已保留恢复缓存",
            ),
        )
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
