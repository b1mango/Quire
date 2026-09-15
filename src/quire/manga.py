"""Ordered, bounded manga capture and atomic PDF publication."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from .assemble.pdf_min import MiniPdfWriter
from .errors import ConfigError, FetchError, NoImagesError
from .fetch.simple import Fetcher, FetchPort, Response
from .image.probe import probe_bytes
from .models import MangaOptions, MangaResult, ProgressSink
from .parse.images import Candidate, collect, order_candidates, postfilter, prefilter
from .parse.minidom import parse as parse_html
from .utils.naming import image_filename, natural_key, safe_filename
from .utils.urls import is_image_url, redact
from .workspace import task_cache, write_bytes

__all__ = ["MangaOptions", "MangaResult", "run_manga", "run_local"]


def _discover(
    url: str, opts: MangaOptions, client: FetchPort
) -> tuple[list[Candidate], MangaResult]:
    if opts.selector:
        parse_html("").select(opts.selector)
    page = client.get(url, referer=opts.referer)
    return discover_page(url, opts, page)


def discover_page(
    url: str, opts: MangaOptions, page: Response
) -> tuple[list[Candidate], MangaResult]:
    doc = parse_html(page.text, base_url=page.url)
    title_node = doc.select_one("h1") or doc.select_one("title")
    title = safe_filename(title_node.text if title_node else "comic", max_len=80)
    candidates = collect(
        doc, doc.effective_base() or page.url, selector=opts.selector, attrs=opts.attrs
    )
    kept, rejected = prefilter(candidates)
    if not kept:
        raise NoImagesError(url, hint="尝试指定 --selector；动态网页支持将在 M2 加入。")
    ordered, warning = order_candidates(kept, opts.order)
    selected = ordered[opts.first - 1 : opts.last or None]
    if not selected:
        raise ConfigError("页范围内没有图片")
    if opts.referer:
        selected = [replace(c, referer=opts.referer) for c in selected]
    return selected, MangaResult(
        Path(), title=title, rejections=tuple(rejected), warnings=(warning,) if warning else ()
    )


def run_manga(
    url: str,
    out: Path | str,
    *,
    options: MangaOptions | None = None,
    workdir: Path | str | None = None,
    fetcher: FetchPort | None = None,
    progress: ProgressSink | None = None,
) -> MangaResult:
    opts = options or MangaOptions()
    started = time.monotonic()
    output = Path(out)
    if output.exists() and not opts.overwrite:
        raise ConfigError(f"目标已存在：{output}")
    cancel = threading.Event()
    client = fetcher or Fetcher(
        timeout=opts.timeout,
        retries=opts.retries,
        concurrency=opts.concurrency,
        rate=opts.rate,
        max_bytes=opts.max_bytes,
        cancel=cancel,
    )
    candidates, result = _discover(url, opts, client)
    root = Path(workdir) if workdir else output.parent / ".quire-work"
    with task_cache(root, keep=opts.keep_images) as cache:
        result = replace(result, output=output, images_dir=cache if opts.keep_images else None)
        result = _assemble(candidates, cache, opts, client, result, progress, cancel)
    result = replace(result, elapsed_s=time.monotonic() - started, bytes_out=output.stat().st_size)
    return _save_report(result, opts.overwrite)


def _assemble(
    candidates: list[Candidate],
    cache: Path,
    opts: MangaOptions,
    client: FetchPort,
    result: MangaResult,
    progress: ProgressSink | None,
    cancel: threading.Event,
) -> MangaResult:
    total = len(candidates)
    pool = ThreadPoolExecutor(max_workers=opts.concurrency)
    pending: dict[int, Future[Path]] = {}
    next_submit = 0
    try:
        with MiniPdfWriter(
            result.output,
            title=result.title,
            dpi=opts.dpi,
            paper=opts.paper,
            overwrite=opts.overwrite,
        ) as pdf:
            for index, candidate in enumerate(candidates):
                while next_submit < total and len(pending) < opts.concurrency:
                    pending[next_submit] = pool.submit(
                        _download_one, client, candidates[next_submit], cache, next_submit, opts
                    )
                    next_submit += 1
                try:
                    path = pending.pop(index).result()
                except FetchError as exc:
                    result = _missing(pdf, result, index, total, candidate, str(exc))
                else:
                    result = _embed(pdf, path, result, index, total, candidate, opts)
                    if not opts.keep_images:
                        path.unlink(missing_ok=True)
                if progress:
                    progress.update(index + 1, total, result)
    finally:
        cancel.set()
        pool.shutdown(wait=True, cancel_futures=True)
    return result


def _download_one(
    client: FetchPort, candidate: Candidate, cache: Path, index: int, opts: MangaOptions
) -> Path:
    last: FetchError = FetchError("No usable image address")
    for url in (candidate.url, *candidate.alternatives):
        try:
            response = client.get(url, referer=candidate.referer or None)
            probe = probe_bytes(response.content)
            if not probe.ok:
                last = FetchError(probe.error or "Image is incomplete or unsupported")
                continue
        except FetchError as exc:
            last = exc
            continue
        extension = "." + ("jpg" if probe.format == "jpeg" else probe.format)
        path = cache / image_filename(index + 1, extension)
        if len(response.content) > opts.max_bytes:
            raise FetchError("Image exceeds configured size limit")
        write_bytes(path, response.content)
        return path
    raise last


def _embed(
    pdf: MiniPdfWriter,
    path: Path,
    result: MangaResult,
    index: int,
    total: int,
    candidate: Candidate,
    opts: MangaOptions,
) -> MangaResult:
    data = path.read_bytes()
    return embed_bytes(pdf, data, result, index, total, candidate, opts)


def embed_bytes(
    pdf: MiniPdfWriter,
    data: bytes,
    result: MangaResult,
    index: int,
    total: int,
    candidate: Candidate,
    opts: MangaOptions,
) -> MangaResult:
    kept, rejected = postfilter([(candidate, probe_bytes(data), len(data))], opts.policy)
    if kept and pdf.add_image_bytes(data):
        return replace(result, pages_written=result.pages_written + 1)
    reason = rejected[0].reason if rejected else "Format requires the core image backend"
    result = replace(
        result,
        pages_rejected=result.pages_rejected + 1,
        rejections=result.rejections + tuple(rejected),
    )
    return _missing(pdf, result, index, total, candidate, reason)


def _missing(
    pdf: MiniPdfWriter,
    result: MangaResult,
    index: int,
    total: int,
    candidate: Candidate,
    reason: str,
) -> MangaResult:
    url = redact(candidate.url)
    pdf.add_placeholder(
        ["MISSING PAGE", f"page {index + 1} / {total}", f"source: {url}", f"reason: {reason}"]
    )
    return replace(
        result, pages_failed=result.pages_failed + 1, failures=result.failures + ((url, reason),)
    )


def _save_report(result: MangaResult, overwrite: bool) -> MangaResult:
    report = result.output.with_suffix(".report.json")
    payload = {
        "schema": 1,
        "title": result.title,
        "output": result.output.name,
        "status": "partial" if result.partial else "done",
        "pages": result.pages_written + result.pages_failed,
        "images": result.pages_written,
        "missing": result.pages_failed,
        "bytes": result.bytes_out,
        "elapsed_seconds": round(result.elapsed_s, 3),
        "warnings": list(result.warnings),
        "failures": list(result.failures),
        "rejections": [
            {"url": redact(r.url), "stage": r.stage, "reason": r.reason} for r in result.rejections
        ],
        "images_dir": str(result.images_dir) if result.images_dir else None,
    }
    if result.task_id is not None:
        payload.update(task_id=result.task_id, resources_reused=result.resources_reused)
    try:
        write_bytes(
            report,
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
            overwrite=overwrite,
        )
    except OSError as exc:
        return replace(result, warnings=result.warnings + (f"报告写入失败：{exc}",))
    return replace(result, report=report)


def run_local(directory: Path, out: Path, *, options: MangaOptions | None = None) -> MangaResult:
    opts = options or MangaOptions()
    if not directory.is_dir():
        raise ConfigError(f"图片目录不存在：{directory}")
    images = sorted(
        (p for p in directory.iterdir() if p.is_file() and is_image_url(p.name)),
        key=lambda p: natural_key(p.name),
    )
    images = images[opts.first - 1 : opts.last or None]
    if not images:
        raise ConfigError("目录或页范围内没有图片")
    started = time.monotonic()
    result = MangaResult(out, title=directory.name)
    with MiniPdfWriter(
        out, title=result.title, dpi=opts.dpi, paper=opts.paper, overwrite=opts.overwrite
    ) as pdf:
        for index, path in enumerate(images):
            candidate = Candidate(path.name, index)
            if path.stat().st_size > opts.max_bytes:
                result = _missing(pdf, result, index, len(images), candidate, "Image too large")
            else:
                result = _embed(pdf, path, result, index, len(images), candidate, opts)
    return _save_report(
        replace(result, elapsed_s=time.monotonic() - started, bytes_out=out.stat().st_size),
        opts.overwrite,
    )
