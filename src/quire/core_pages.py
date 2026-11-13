"""Convert verified sources into shared export pages and capture statistics."""

from __future__ import annotations

from collections.abc import Generator
from dataclasses import replace
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .assemble.models import ExportPage, clean_metadata_text
from .errors import QuireError
from .image.codec import inspect_image
from .image.compress import encode_pages
from .image.options import CompressionOptions, Encoding
from .image.probe import ImageProbe
from .models import MangaOptions, MangaResult
from .parse.images import Candidate, postfilter
from .store.cache import read_cached
from .store.models import ResourceRecord, TaskSnapshot
from .utils.urls import redact


def _missing(index: int, url: str, reason: str) -> ExportPage:
    with Image.new("RGB", (900, 1200), "white") as image, BytesIO() as buffer:
        draw = ImageDraw.Draw(image)
        draw.text((60, 100), "MISSING PAGE", fill="black", font=ImageFont.load_default(size=38))
        draw.text(
            (60, 170),
            f"Source page {index + 1}",
            fill="black",
            font=ImageFont.load_default(size=24),
        )
        draw.text(
            (60, 230),
            "See the capture report for details.",
            fill="black",
            font=ImageFont.load_default(size=20),
        )
        image.save(buffer, "PNG")
        return ExportPage(buffer.getvalue(), 900, 1200, index + 1, url, missing_reason=reason)


def source_pages(
    root: Path,
    snapshot: TaskSnapshot,
    record: ResourceRecord,
    candidate: Candidate,
    index: int,
    opts: MangaOptions,
    compression: CompressionOptions,
    encoding: Encoding,
    result: MangaResult,
) -> Generator[tuple[ExportPage, MangaResult], None, None]:
    index += opts.first - 1
    url = clean_metadata_text(redact(candidate.url))
    reason = record.error_code or "network"
    if record.local_path is not None:
        assert record.sha256 is not None and record.size is not None
        data = read_cached(
            root, snapshot.task_id, record.local_path, sha256=record.sha256, size=record.size
        )
        info = inspect_image(data)
        probe = ImageProbe(format=info.format, width=info.width, height=info.height, complete=True)
        _, rejected = postfilter([(candidate, probe, len(data))], opts.policy)
        if not rejected:
            if info.frames > 1:
                result = replace(
                    result, warnings=result.warnings + (f"来源图片 {index + 1} 为多帧，仅采第一帧",)
                )
            for part, page in enumerate(encode_pages(data, encoding, compression), 1):
                result = replace(result, pages_written=result.pages_written + 1)
                yield ExportPage(page.data, page.width, page.height, index + 1, url, part), result
            return
        reason = rejected[0].reason
        result = replace(
            result,
            pages_rejected=result.pages_rejected + 1,
            rejections=result.rejections + tuple(rejected),
        )
    result = replace(
        result, pages_failed=result.pages_failed + 1, failures=result.failures + ((url, reason),)
    )
    yield _missing(index, url, reason), result


def strip_width(root: Path, snapshot: TaskSnapshot, sample: int = 40) -> int:
    """整卷是条漫切片时返回切片宽度，否则 0。

    抽样已缓存的前 40 张：同一宽度占 ≥60%，且这些图的高宽比中位数 > 1.8（普通漫画单页约 1.4）。
    读不出的图跳过，判不出来就按普通漫画处理（窄图照常过滤）。
    """
    from collections import Counter
    from statistics import median

    sizes: list[tuple[int, int]] = []
    for record in snapshot.resources:
        if len(sizes) >= sample:
            break
        if record.local_path is None or record.sha256 is None or record.size is None:
            continue
        try:
            data = read_cached(
                root, snapshot.task_id, record.local_path, sha256=record.sha256, size=record.size
            )
            info = inspect_image(data)
        except (QuireError, ValueError, OSError):
            continue
        if info.width and info.height:
            sizes.append((info.width, info.height))
    if len(sizes) < 3:
        return 0
    width, count = Counter(w for w, _ in sizes).most_common(1)[0]
    ratios = [h / w for w, h in sizes if w == width]
    return width if count >= len(sizes) * 0.6 and median(ratios) > 1.8 else 0
