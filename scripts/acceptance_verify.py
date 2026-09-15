"""Independent S1.7 readback for original-paper, 150-dpi, split-tall exports.

Structural/input/decode failures raise ValueError with artifact/page context.
Quality failures return quality_passed=False. Exact pixels have psnr_db=None
(infinite PSNR). References use first-frame EXIF/white normalization, without ICC.
Only one source and one output page are decoded at a time; paths may repeat.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import ExitStack, closing, contextmanager
from io import BytesIO
from itertools import groupby
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from PIL import Image
from pypdf import PdfReader

from scripts.acceptance_compare import comparisons
from scripts.smoke_compression import normalized, pdf_pixels, pixel_metrics

LIMITS = {"psnr_db_min": 28, "mae_max": 5}


def _require(condition, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _metadata(reader, archive, cbz, sources, expected_missing) -> tuple[dict, list]:
    for label, book in (("ZIP", archive), ("CBZ", cbz)):
        _require(book.testzip() is None, f"{label}: CRC failure")
    manifest = json.loads(archive.read("manifest.json"))
    _require(manifest["schema"] == 1, "manifest schema must be 1")
    entries = manifest["pages"]
    _require(isinstance(entries, list) and entries, "manifest must contain pages")
    _require(len(reader.pages) == len(entries), "PDF/manifest page count mismatch")
    previous = (0, 0)
    missing = 0
    for index, entry in enumerate(entries, 1):
        for key in ("source_index", "part", "width", "height"):
            _require(type(entry[key]) is int and entry[key] > 0, f"page {index}: invalid {key}")
        source, part = entry["source_index"], entry["part"]
        _require(source <= len(sources), f"page {index}: source_index out of range")
        expected = (source, previous[1] + 1) if source == previous[0] else (previous[0] + 1, 1)
        _require((source, part) == expected, f"page {index}: source_index/part order mismatch")
        previous = source, part
        _require(
            entry["file"] in (f"{index:06d}.png", f"{index:06d}.jpg"),
            f"page {index}: manifest file sequence mismatch",
        )
        reason = entry["missing_reason"]
        _require(
            reason is None or isinstance(reason, str) and bool(reason),
            f"page {index}: invalid missing_reason",
        )
        missing += reason is not None
    _require(previous[0] == len(sources), "manifest does not cover every source")
    _require(
        missing == expected_missing, f"missing count: expected {expected_missing}, got {missing}"
    )
    files = [entry["file"] for entry in entries]
    _require(archive.namelist() == [*files, "manifest.json"], "ZIP entries/order mismatch")
    _require(cbz.namelist() == [*files, "ComicInfo.xml"], "CBZ entries/order mismatch")
    comic = ET.fromstring(cbz.read("ComicInfo.xml"))
    _require(
        comic.tag == "ComicInfo" and comic.findtext("PageCount") == str(len(entries)),
        "CBZ metadata page count mismatch",
    )
    _require(
        comic.findtext("Title", "") == manifest["title"]
        and comic.findtext("Web", "") == manifest["source"],
        "CBZ metadata mismatch",
    )
    metadata = reader.metadata
    subject = urlunsplit(urlsplit(manifest["source"])._replace(query="", fragment=""))
    _require(
        metadata
        and metadata.title == manifest["title"]
        and metadata.subject == subject
        and metadata.producer == "Quire",
        "PDF metadata mismatch",
    )
    outline = reader.outline
    _require(len(outline) == 2 and isinstance(outline[1], list), "PDF bookmark tree mismatch")
    _require(
        outline[0].title == (manifest["title"] or "Untitled")
        and reader.get_destination_page_number(outline[0]) == 0,
        "PDF root bookmark mismatch",
    )
    _require(len(outline[1]) == len(entries), "PDF bookmark count mismatch")
    for index, (entry, bookmark) in enumerate(zip(entries, outline[1], strict=True)):
        _require(
            bookmark.title == f"Page {entry['source_index']} / part {entry['part']}"
            and reader.get_destination_page_number(bookmark) == index,
            f"page {index + 1}: PDF bookmark target/order mismatch",
        )
    return manifest, entries


def _geometry(page, size, missing: bool) -> dict:
    width, height = (n * 72 / 150 for n in size)
    box = [float(n) for n in page.mediabox]
    operations = page.get_contents().operations
    expected_ops = [b"q", b"cm", b"Do", b"Q"]
    if missing:
        expected_ops += [b"q", b"BT", b"Tf", b"Tr", b"Tj", b"ET", b"Q"]
    _require([op for _, op in operations] == expected_ops, "PDF drawing operations mismatch")
    matrix = [float(n) for n in operations[1][0]]
    expected = [width, 0, 0, height, 0, 0]
    ok = len(matrix) == 6 and all(abs(a - b) < 0.011 for a, b in zip(matrix, expected, strict=True))
    ok &= all(abs(a - b) < 0.011 for a, b in zip(box, [0, 0, width, height], strict=True))
    ok &= list(page.cropbox) == list(page.mediabox) and page.rotation == 0
    ok &= float(page.get("/UserUnit", 1)) == 1 and operations[2][0] == ["/Im0"]
    _require(ok, "PDF geometry/transform mismatch")
    return {"mediabox": box, "matrix": matrix, "dpi": 150, "ok": True}


def _compare(reference, actual, lossless, max_edge, stack) -> tuple[dict, Image.Image]:
    expected = stack.enter_context(closing(reference.copy()))
    if max_edge and not lossless:
        expected.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    _require(expected.size == actual.size, "output dimensions differ from source crop/resize")
    aligned = pixel_metrics(expected, actual)
    native = stack.enter_context(closing(actual.resize(reference.size, Image.Resampling.LANCZOS)))
    match = reference.size == actual.size and aligned["exact"]
    diagnostic = (aligned["exact"] or aligned["psnr_db"] >= LIMITS["psnr_db_min"]) and aligned[
        "mae"
    ] <= LIMITS["mae_max"]
    return {
        **aligned,
        "source_mae": aligned["mae"],
        "native": pixel_metrics(reference, native),
        "lossless_pixel_match": match if lossless else None,
        "passed": match if lossless else diagnostic,
    }, native


@contextmanager
def _page(reader, archive, cbz, index, entry, source, box, *, lossless, max_edge):
    with ExitStack() as stack:
        data = archive.read(entry["file"])
        _require(hashlib.sha256(data).hexdigest() == entry["sha256"], "manifest SHA256 mismatch")
        _require(cbz.read(entry["file"]) == data, "CBZ/ZIP image bytes differ")
        raw = stack.enter_context(Image.open(BytesIO(data)))
        _require(
            raw.format == ("PNG" if entry["file"].endswith("png") else "JPEG"),
            "manifest image extension mismatch",
        )
        actual_zip = stack.enter_context(closing(raw.convert("RGB")))
        _require(
            actual_zip.size == (entry["width"], entry["height"]), "manifest dimensions mismatch"
        )
        page = reader.pages[index]
        objects = page["/Resources"]["/XObject"]
        _require(set(objects) == {"/Im0"}, "PDF must contain exactly one page image")
        obj = objects["/Im0"]
        _require(
            obj["/Subtype"] == "/Image"
            and obj["/Filter"] in ("/DCTDecode", "/FlateDecode")
            and obj["/ColorSpace"] in ("/DeviceRGB", "/DeviceGray")
            and obj["/BitsPerComponent"] in (1, 8)
            and not any(key in obj for key in ("/Decode", "/DecodeParms", "/Mask", "/SMask")),
            "unsupported PDF image transform/encoding",
        )
        try:
            actual = stack.enter_context(closing(pdf_pixels(obj)))
        finally:
            # pypdf caches Flate decoded bytes; discard them before the next page.
            if hasattr(obj, "decoded_self"):
                obj.decoded_self = None
        _require(
            actual.size == actual_zip.size and pixel_metrics(actual_zip, actual)["exact"],
            "PDF/ZIP decoded pixels differ",
        )
        missing = entry["missing_reason"] is not None
        result = {
            "page": index + 1,
            **entry,
            "output_size": list(actual.size),
            "geometry": _geometry(page, actual.size, missing),
            "geometry_ok": True,
            "pdf_zip_pixel_match": True,
            "cbz_zip_bytes_match": True,
            "source_size": list(source.size) if source else None,
            "crop": list(box) if box else None,
        }
        if missing:
            text = "MISSING PAGE" in page.extract_text()
            with actual_zip.convert("L") as gray:
                hist = gray.histogram()
            visible = sum(hist[:64]) >= 100 and sum(hist[192:]) > actual.width * actual.height / 2
            _require(
                text and visible, "missing placeholder must have PDF text and a visible ZIP image"
            )
            result.update(
                placeholder_text=text,
                placeholder_visible=visible,
                exact=None,
                source_mae=None,
                psnr_db=None,
                native=None,
                lossless_pixel_match=None,
                passed=True,
            )
            reference = native = actual_zip
        else:
            reference = stack.enter_context(closing(source.crop(box)))
            metrics, native = _compare(reference, actual, lossless, max_edge, stack)
            result.update(metrics, crop_size=list(reference.size))
        yield result, reference, actual, native


def verify_artifacts(
    output: Path,
    sources: list[Path],
    *,
    lossless: bool,
    max_edge: int | None,
    expected_missing: int = 0,
    comparison_dir: Path | None = None,
) -> dict:
    """Validate all sources[index-1], including intentional missing-source positions.

    Each expected source must occur in manifest order; missing sources occupy one
    part. Missing references need not exist and are never used for quality scores.
    ValueError denotes structural/input/IO failure; quality alone never raises.
    comparison_dir overrides the default output.parent/'comparisons'. At most six
    pages (head/middle/tail) each get two detail ROIs plus a shared contact sheet.
    """
    context = "inputs"
    try:
        _require(output.suffix.lower() == ".pdf", "output must be a PDF path")
        _require(bool(sources), "sources cannot be empty")
        _require(
            type(expected_missing) is int and 0 <= expected_missing <= len(sources),
            "expected_missing must be an integer within source count",
        )
        _require(
            max_edge is None or type(max_edge) is int and max_edge >= 0,
            "max_edge must be None or a nonnegative integer",
        )
        directory = comparison_dir if comparison_dir is not None else output.parent / "comparisons"
        with ExitStack() as stack:
            context = "artifact metadata/CRC"
            reader = stack.enter_context(closing(PdfReader(output)))
            archive = stack.enter_context(ZipFile(output.with_suffix(".zip")))
            cbz = stack.enter_context(ZipFile(output.with_suffix(".cbz")))
            manifest, entries = _metadata(reader, archive, cbz, sources, expected_missing)
            count = len(entries)
            samples = (0, 1, count // 2 - 1, count // 2, count - 2, count - 1)
            selected = sorted({i for i in samples if 0 <= i < count})
            directory.mkdir(parents=True, exist_ok=True)
            sheet = stack.enter_context(
                closing(Image.new("RGB", (720, 284 * len(selected)), "white"))
            )
            pages, index = [], 0
            for source_index, members in groupby(entries, key=lambda entry: entry["source_index"]):
                path, group = sources[source_index - 1], list(members)
                context = f"source {source_index}"
                with ExitStack() as source_stack:
                    missing = any(e["missing_reason"] is not None for e in group)
                    source, stored = None, None
                    if missing:
                        _require(
                            len(group) == 1 and group[0]["part"] == 1,
                            "missing source must occupy exactly one part",
                        )
                        boxes = [None]
                    else:
                        with Image.open(path) as raw:
                            stored = list(raw.size)
                            _require(
                                "icc_profile" not in raw.info,
                                "ICC reference normalization unsupported",
                            )
                        source = source_stack.enter_context(closing(normalized(path)))
                        step = (
                            source.width * 2 if source.height > source.width * 8 else source.height
                        )
                        boxes = [
                            (0, top, source.width, min(top + step, source.height))
                            for top in range(0, source.height, step)
                        ]
                        _require(
                            len(boxes) == len(group), "manifest parts do not cover source height"
                        )
                    for entry, box in zip(group, boxes, strict=True):
                        context = f"page {index + 1} (source {source_index}, part {entry['part']})"
                        with _page(
                            reader,
                            archive,
                            cbz,
                            index,
                            entry,
                            source,
                            box,
                            lossless=lossless,
                            max_edge=max_edge,
                        ) as (result, reference, actual, native):
                            if index in selected:
                                result["details"] = comparisons(
                                    reference,
                                    actual,
                                    native,
                                    sheet,
                                    selected.index(index),
                                    directory,
                                    output.stem,
                                    result,
                                )
                            result.update(source=str(path), stored_source_size=stored)
                            pages.append(result)
                        index += 1
            contact = directory / f"{output.stem}-contact.png"
            sheet.save(contact)
        quality = all(page["passed"] for page in pages)
        return {
            "structure_passed": True,
            "quality_passed": quality,
            "passed": quality,
            "source_count": len(sources),
            "page_count": count,
            "artifact_bytes": {
                kind: output.with_suffix(f".{kind}").stat().st_size
                for kind in ("pdf", "cbz", "zip")
            },
            "missing_count": expected_missing,
            "expected_missing": expected_missing,
            "title": manifest["title"],
            "thresholds": dict(LIMITS),
            "pages": pages,
            "lossless_pixel_match": all(p["lossless_pixel_match"] is not False for p in pages)
            if lossless
            else None,
            "comparisons": {"contact": str(contact), "sample_pages": [i + 1 for i in selected]},
            "limits": [
                "Independent first-frame EXIF/white normalization; ICC unsupported.",
                "Aligned metrics exclude resize loss; native/details include it.",
                "Exact pixels use null PSNR for infinity; thresholds are diagnostics.",
                "PDF image-stream readback; renderer and small-text readability need review.",
            ],
        }
    except Exception as exc:
        raise ValueError(f"{output.name}: {context}: {type(exc).__name__}: {exc}") from exc
