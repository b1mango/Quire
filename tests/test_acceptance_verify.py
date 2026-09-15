from __future__ import annotations

import hashlib
import io
import json
from contextlib import closing
from pathlib import Path
from zipfile import ZipFile

import pytest
from PIL import Image, ImageDraw
from pypdf import PdfReader, PdfWriter

from quire.assemble.archive import ArchiveWriter
from quire.assemble.models import ExportPage
from quire.assemble.pdf import CorePdfWriter
from quire.image.options import CompressionOptions
from scripts import acceptance_verify as verify
from tests import test_multiformat as fixture


def sources_for_capture(root: Path, monkeypatch, *, large=False) -> list[Path]:
    paths = []
    for label, size in (("page", (2100, 2400) if large else (400, 600)), ("strip", (400, 3600))):
        path = root / f"{label}.png"
        with Image.new("RGB", size, "white") as page:
            draw = ImageDraw.Draw(page)
            for y in range(0, size[1], 40):
                shade = (y * 7 // 40) % 190
                draw.rectangle((20, y, size[0] - 20, y + 25), fill=(shade, 100, 210 - shade))
                draw.text((30, y + 5), f"Line {y} / source detail", fill="black")
            page.save(path)
        paths.append(path)

    def payload(size=(400, 600), color="red"):
        return paths[size[1] == 3600].read_bytes()

    monkeypatch.setattr(fixture, "image", payload)
    return [paths[0], paths[0], paths[1]]


def rewrite_zip(path, change):
    with ZipFile(path) as archive:
        items = {name: archive.read(name) for name in archive.namelist()}
    change(items)
    with ZipFile(path, "w") as archive:
        for name, data in items.items():
            archive.writestr(name, data)


def change_manifest(items, mutate):
    manifest = json.loads(items["manifest.json"])
    mutate(manifest)
    items["manifest.json"] = json.dumps(manifest).encode()


def test_lossless_strips_mapping_and_limited_comparisons(tmp_path, monkeypatch):
    sources = sources_for_capture(tmp_path, monkeypatch)
    result = fixture.capture(tmp_path, compression=CompressionOptions("lossless", None))
    directory = tmp_path / "review"
    report = verify.verify_artifacts(
        result.output, sources, lossless=True, max_edge=0, comparison_dir=directory
    )
    assert (
        report["structure_passed"] and report["quality_passed"] and report["lossless_pixel_match"]
    )
    assert report["page_count"] == 7 and report["source_count"] == 3
    assert report["artifact_bytes"] == {
        kind: result.output.with_suffix(f".{kind}").stat().st_size for kind in ("pdf", "cbz", "zip")
    }
    assert [(p["source_index"], p["part"]) for p in report["pages"]] == [
        (1, 1),
        (2, 1),
        (3, 1),
        (3, 2),
        (3, 3),
        (3, 4),
        (3, 5),
    ]
    assert [p["crop"] for p in report["pages"][2:]] == [
        [0, 0, 400, 800],
        [0, 800, 400, 1600],
        [0, 1600, 400, 2400],
        [0, 2400, 400, 3200],
        [0, 3200, 400, 3600],
    ]
    assert all(
        p["exact"] and p["source_mae"] == 0 and p["native"]["exact"] for p in report["pages"]
    )
    assert all(p["geometry_ok"] and p["pdf_zip_pixel_match"] for p in report["pages"])
    assert report["comparisons"]["sample_pages"] == [1, 2, 3, 4, 6, 7]
    assert len(list(directory.glob("*-detail-*.png"))) == 12
    assert not (tmp_path / "comparisons").exists()
    with Image.open(report["comparisons"]["contact"]) as contact:
        assert contact.size == (720, 1704)
    json.dumps(report, allow_nan=False)


def test_default_compression_records_resize_and_native_error(tmp_path, monkeypatch):
    sources = sources_for_capture(tmp_path, monkeypatch, large=True)
    result = fixture.capture(tmp_path)
    report = verify.verify_artifacts(
        result.output, sources, lossless=False, max_edge=result.max_edge
    )
    assert report["structure_passed"] and report["quality_passed"]
    page = report["pages"][0]
    assert page["source_size"] == [2100, 2400] and page["output_size"] == [1750, 2000]
    assert not page["exact"] and page["psnr_db"] >= 28 and page["source_mae"] <= 5
    assert page["native"]["mae"] > page["source_mae"]
    assert page["lossless_pixel_match"] is None


def test_quality_failure_returns_report_instead_of_raising(tmp_path, monkeypatch):
    sources = sources_for_capture(tmp_path, monkeypatch)
    result = fixture.capture(tmp_path, compression=CompressionOptions("lossless", None))
    with Image.new("RGB", (400, 600), "black") as wrong:
        wrong.save(sources[0])
    for lossless in (False, True):
        report = verify.verify_artifacts(result.output, sources, lossless=lossless, max_edge=None)
        assert report["structure_passed"] and not report["quality_passed"]
        assert not report["pages"][0]["passed"] and report["pages"][0]["psnr_db"] < 28
        assert report["pages"][2]["passed"]


def test_expected_missing_is_explicit_even_when_reference_exists(tmp_path, monkeypatch):
    sources = sources_for_capture(tmp_path, monkeypatch)
    result = fixture.capture(tmp_path, missing=True)
    with pytest.raises(ValueError, match="missing count: expected 0, got 1"):
        verify.verify_artifacts(result.output, sources, lossless=False, max_edge=2000)
    report = verify.verify_artifacts(
        result.output, sources, lossless=False, max_edge=2000, expected_missing=1
    )
    page = report["pages"][1]
    assert report["structure_passed"] and report["missing_count"] == 1
    assert page["placeholder_text"] and page["placeholder_visible"]
    assert page["source_mae"] is None and page["lossless_pixel_match"] is None
    sources[1] = tmp_path / "missing-source"
    assert verify.verify_artifacts(
        result.output, sources, lossless=False, max_edge=2000, expected_missing=1
    )["quality_passed"]
    with pytest.raises(ValueError, match="missing count: expected 0, got 1"):
        verify.verify_artifacts(result.output, sources, lossless=False, max_edge=2000)


@pytest.mark.parametrize(
    "mutation,match",
    [
        ("crc", "CRC"),
        ("sha", "SHA256"),
        ("part", "source_index/part"),
        ("file", "file sequence"),
        ("dimensions", "dimensions mismatch"),
        ("cbz_bytes", "CBZ/ZIP image bytes"),
        ("pdf_pixels", "PDF/ZIP decoded pixels"),
        ("title", "PDF metadata"),
        ("bookmark", "PDF bookmark"),
        ("geometry", "geometry"),
    ],
)
def test_structural_damage_is_contextual_value_error(tmp_path, monkeypatch, mutation, match):
    sources = sources_for_capture(tmp_path, monkeypatch)
    result = fixture.capture(tmp_path)
    if mutation == "crc":
        path = result.output.with_suffix(".zip")
        with ZipFile(path) as archive:
            entry = archive.infolist()[0]
        data = bytearray(path.read_bytes())
        offset = entry.header_offset + 30 + len(entry.filename.encode()) + len(entry.extra)
        data[offset + entry.compress_size // 2] ^= 1
        path.write_bytes(data)
    elif mutation in {"sha", "part", "file", "dimensions"}:

        def mutate(manifest):
            entry = manifest["pages"][0]
            if mutation == "sha":
                entry["sha256"] = "0" * 64
            elif mutation == "part":
                manifest["pages"][3]["part"] = 3
            elif mutation == "file":
                entry["file"] = "000002.jpg"
            else:
                entry["width"] += 1

        rewrite_zip(result.output.with_suffix(".zip"), lambda items: change_manifest(items, mutate))
    elif mutation == "cbz_bytes":

        def adjust(items):
            name = next(iter(items))
            items[name] += b"trailing bytes"

        rewrite_zip(result.output.with_suffix(".cbz"), adjust)
    elif mutation == "pdf_pixels":
        with ZipFile(result.output.with_suffix(".zip")) as archive:
            name = json.loads(archive.read("manifest.json"))["pages"][0]["file"]
        with Image.new("RGB", (400, 600), "black") as image, io.BytesIO() as buffer:
            image.save(buffer, "JPEG")
            replacement = buffer.getvalue()

        def adjust(items):
            items[name] = replacement
            if "manifest.json" in items:
                change_manifest(
                    items,
                    lambda m: m["pages"][0].update(sha256=hashlib.sha256(replacement).hexdigest()),
                )

        for suffix in (".zip", ".cbz"):
            rewrite_zip(result.output.with_suffix(suffix), adjust)
    else:
        with PdfReader(result.output) as reader, closing(PdfWriter(clone_from=reader)) as writer:
            if mutation == "title":
                writer.add_metadata({"/Title": "Wrong"})
            elif mutation == "bookmark":
                del writer.root_object["/Outlines"]
            else:
                writer.pages[0].rotate(90)
            buffer = io.BytesIO()
            writer.write(buffer)
        result.output.write_bytes(buffer.getvalue())
    with pytest.raises(ValueError, match=match):
        verify.verify_artifacts(result.output, sources, lossless=False, max_edge=2000)


def test_missing_blank_image_cannot_pass_on_pdf_text_alone(tmp_path):
    source = tmp_path / "missing-source"
    output = tmp_path / "blank.pdf"
    with Image.new("RGB", (900, 1200), "white") as image, io.BytesIO() as buffer:
        image.save(buffer, "PNG")
        page = ExportPage(
            buffer.getvalue(), 900, 1200, 1, "https://example.test/1", missing_reason="network"
        )
    with CorePdfWriter(output, title="Missing", source_url="https://example.test") as writer:
        writer.add_page(page)
    for fmt in ("zip", "cbz"):
        with ArchiveWriter(
            output.with_suffix(f".{fmt}"),
            format=fmt,
            title="Missing",
            source_url="https://example.test",
        ) as writer:
            writer.add_page(page)
    with pytest.raises(ValueError, match="visible ZIP image"):
        verify.verify_artifacts(output, [source], lossless=False, max_edge=2000, expected_missing=1)


def test_many_repeated_paths_are_processed_one_at_a_time(tmp_path, monkeypatch):
    source = tmp_path / "source.png"
    with Image.new("RGB", (20, 24), "white") as image:
        ImageDraw.Draw(image).line((0, 0, 19, 23), fill="black")
        image.save(source)
    output, url = tmp_path / "many.pdf", "https://example.test"
    with (
        CorePdfWriter(output, title="Repeat", source_url=url) as pdf,
        ArchiveWriter(
            output.with_suffix(".zip"), format="zip", title="Repeat", source_url=url
        ) as archive,
        ArchiveWriter(
            output.with_suffix(".cbz"), format="cbz", title="Repeat", source_url=url
        ) as cbz,
    ):
        for index in range(1, 201):
            page = ExportPage(source.read_bytes(), 20, 24, index, url)
            for writer in (pdf, archive, cbz):
                writer.add_page(page)
    normalize, decode, active, calls = verify.normalized, verify.pdf_pixels, set(), []

    def track(image, key):
        assert key not in active, "Previous decoded image was not released"
        active.add(key)
        original_close = image.close

        def close():
            active.discard(key)
            original_close()

        image.close = close
        return image

    def counted(path):
        calls.append(path)
        return track(normalize(path), "source")

    monkeypatch.setattr(verify, "normalized", counted)
    monkeypatch.setattr(verify, "pdf_pixels", lambda obj: track(decode(obj), "pdf"))
    report = verify.verify_artifacts(output, [source] * 200, lossless=True, max_edge=None)
    assert report["passed"] and len(calls) == 200 and not active
    assert len(report["comparisons"]["sample_pages"]) == 6
    assert len(list((tmp_path / "comparisons").glob("*-detail-*.png"))) == 12
