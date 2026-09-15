"""Core PDF fidelity, document structure, cleanup and bounded-workload RSS."""

from __future__ import annotations

import gc
import io
import json
import random
import subprocess
import sys
import weakref
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO, cast

import pytest
from PIL import Image, ImageDraw
from pypdf import PageObject, PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    Destination,
    DictionaryObject,
    IndirectObject,
    NameObject,
    NumberObject,
    StreamObject,
)

from quire.assemble.models import ExportPage
from quire.assemble.pdf import CorePdfWriter
from quire.errors import FetchError


def make_page(
    mode: str = "RGB", format: str = "PNG", size: tuple[int, int] = (37, 53)
) -> ExportPage:
    with Image.new(mode, size) as image, io.BytesIO() as buffer:
        draw = ImageDraw.Draw(image)
        colors: dict[str, int | tuple[int, ...]] = {
            "RGB": (231, 52, 13),
            "L": 93,
            "1": 1,
            "CMYK": (0, 255, 255, 0),
        }
        color = colors[mode]
        draw.rectangle((0, 0, size[0] // 2, size[1] // 2), fill=color)
        image.save(buffer, format=format)
        return ExportPage(buffer.getvalue(), *size, 1, "https://example.test/1")


def write_pdf(path: Path, *pages: ExportPage) -> PdfReader:
    with CorePdfWriter(path, title="test", source_url="https://example.test") as writer:
        for page in pages:
            writer.add_page(page)
    return PdfReader(path, strict=True)


def image_objects(page: PageObject) -> DictionaryObject:
    return cast(DictionaryObject, cast(DictionaryObject, page["/Resources"])["/XObject"])


@pytest.mark.parametrize("mode", ["RGB", "L", "CMYK"])
def test_jpeg_direct_embedding(tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    page = make_page(mode, "JPEG")

    def no_decode(*args: object, **kwargs: object) -> None:
        raise AssertionError("JPEG must not be decoded or re-encoded")

    with monkeypatch.context() as patch:
        patch.setattr(Image.Image, "tobytes", no_decode)
        patch.setattr(Image.Image, "save", no_decode)
        reader = write_pdf(tmp_path / "jpeg.pdf", page)
    obj = cast(StreamObject, image_objects(reader.pages[0])["/Im0"])
    assert obj["/Filter"] == NameObject("/DCTDecode")
    assert obj.get_data() == page.data
    assert obj["/BitsPerComponent"] == NumberObject(8)
    if mode == "CMYK":
        assert obj["/ColorSpace"] == NameObject("/DeviceCMYK")
        assert obj["/Decode"] == ArrayObject([NumberObject(v) for v in [1, 0] * 4])
    # page.images re-encodes JPEGs; the raw XObject stream is the fidelity oracle.
    with Image.open(io.BytesIO(page.data)) as source, Image.open(io.BytesIO(obj.get_data())) as got:
        assert got.tobytes() == source.tobytes()


@pytest.mark.parametrize("mode", ["RGB", "L", "1"])
@pytest.mark.parametrize("size", [(1, 3), (37, 53), (40, 55)])
def test_png_pixels_and_smoke_contract(tmp_path: Path, mode: str, size: tuple[int, int]) -> None:
    page = make_page(mode, size=size)
    reader = write_pdf(tmp_path / "png.pdf", page)
    pdf_page = reader.pages[0]
    objects = image_objects(pdf_page)
    assert list(objects) == ["/Im0"]
    assert isinstance(objects.raw_get("/Im0"), IndirectObject)
    obj = cast(StreamObject, objects["/Im0"])
    assert obj["/Filter"] == NameObject("/FlateDecode")
    assert "/DecodeParms" not in obj
    assert obj["/BitsPerComponent"] == NumberObject(1 if mode == "1" else 8)
    assert obj["/ColorSpace"] == NameObject("/DeviceRGB" if mode == "RGB" else "/DeviceGray")
    with Image.open(io.BytesIO(page.data)) as source:
        assert obj.get_data() == source.tobytes()
        assert cast(Image.Image, pdf_page.images[0].image).tobytes() == source.tobytes()
    content = pdf_page.get_contents()
    assert content is not None
    assert [op for _, op in content.operations] == [b"q", b"cm", b"Do", b"Q"]
    assert [float(v) for v in content.operations[1][0]] == pytest.approx(
        [size[0] * 0.48, 0, 0, size[1] * 0.48, 0, 0]
    )
    assert (float(pdf_page.mediabox.width), float(pdf_page.mediabox.height)) == pytest.approx(
        [size[0] * 0.48, size[1] * 0.48]
    )


@pytest.mark.parametrize(
    "paper,dimensions",
    [
        ("a4", (595.28, 841.89)),
        ("a5", (419.53, 595.28)),
        ("b5", (498.90, 708.66)),
        ("letter", (612.0, 792.0)),
        ("original", (37.0, 53.0)),
    ],
)
@pytest.mark.parametrize("size", [(37, 53), (53, 37)])
def test_paper_contain(
    tmp_path: Path, paper: str, dimensions: tuple[float, float], size: tuple[int, int]
) -> None:
    path = tmp_path / "layout.pdf"
    with CorePdfWriter(path, title="layout", source_url="", paper=paper, dpi=72) as writer:
        writer.add_page(make_page(size=size))
    page = PdfReader(path).pages[0]
    width, height = size if paper == "original" else dimensions
    assert (float(page.mediabox.width), float(page.mediabox.height)) == (width, height)
    scale = min(width / size[0], height / size[1])
    content = page.get_contents()
    assert content is not None
    assert [float(v) for v in content.operations[1][0]] == pytest.approx(
        [
            size[0] * scale,
            0,
            0,
            size[1] * scale,
            (width - size[0] * scale) / 2,
            (height - size[1] * scale) / 2,
        ]
    )


def test_metadata_outlines_order_and_missing_page(tmp_path: Path) -> None:
    path = tmp_path / "book.tmp"
    title = "\u5377\u5e19 (test) \\ \u7b2c\u4e00\u5377"
    source = "https://user:password@example.test/book?token=secret#private"
    first = make_page()
    missing = replace(make_page("1"), source_index=2, missing_reason="HTTP 404")
    last = replace(make_page("L"), source_index=3, part=2)
    with CorePdfWriter(path, title=title + "\0\x1f\x7f\ud800", source_url=source) as writer:
        for page in [first, missing, last, first]:
            writer.add_page(page)
        assert not path.exists()
    assert list(tmp_path.iterdir()) == [path]
    reader = PdfReader(path, strict=True)
    assert reader.metadata is not None
    assert reader.metadata.title == title
    assert reader.metadata.subject == "https://example.test/book"
    assert reader.metadata.producer == "Quire"
    assert len(reader.pages) == 4
    assert len({cast(IndirectObject, page.indirect_reference).idnum for page in reader.pages}) == 4
    assert "MISSING PAGE" in reader.pages[1].extract_text()
    resources = cast(DictionaryObject, reader.pages[1]["/Resources"])
    font = cast(DictionaryObject, resources["/Font"])
    assert isinstance(font.raw_get("/F0"), IndirectObject)
    assert cast(DictionaryObject, font["/F0"])["/BaseFont"] == NameObject("/Helvetica")
    assert all(not reader.pages[i].extract_text() for i in [0, 2, 3])
    outlines = reader.outline
    root, children = cast(Destination, outlines[0]), cast(list[Destination], outlines[1])
    assert root.title == title
    assert reader.get_destination_page_number(root) == 0
    assert [o.title for o in children] == [
        "Page 1 / part 1",
        "Page 2 / part 1",
        "Page 3 / part 2",
        "Page 1 / part 1",
    ]
    assert [reader.get_destination_page_number(o) for o in children] == list(range(4))
    for index, original in enumerate([first, missing, last, first]):
        with Image.open(io.BytesIO(original.data)) as image:
            assert (
                cast(Image.Image, reader.pages[index].images[0].image).tobytes() == image.tobytes()
            )
    assert all(value not in path.read_bytes() for value in [b"password", b"secret", b"private"])


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://[broken?secret", "[invalid URL]"),
        ("https://example.test/\x00book?q=secret", "https://example.test/book"),
    ],
)
def test_empty_document_and_source_cleaning(tmp_path: Path, url: str, expected: str) -> None:
    path = tmp_path / "empty.pdf"
    with CorePdfWriter(path, title="", source_url=url):
        pass
    reader = PdfReader(path, strict=True)
    assert len(reader.pages) == 0
    assert reader.outline == []
    assert reader.metadata is not None and reader.metadata.subject == expected


def test_empty_title_bookmark(tmp_path: Path) -> None:
    path = tmp_path / "untitled.pdf"
    with CorePdfWriter(path, title="\0", source_url="") as writer:
        writer.add_page(make_page())
    assert cast(Destination, PdfReader(path).outline[0]).title == "Untitled"


@pytest.mark.parametrize("dpi,paper", [(0, "original"), (-1, "a4"), (150, "unknown")])
def test_invalid_options(tmp_path: Path, dpi: int, paper: str) -> None:
    with pytest.raises(ValueError):
        CorePdfWriter(tmp_path / "bad.pdf", title="", source_url="", dpi=dpi, paper=paper)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "problem", ["width", "height", "source_index", "part", "size", "data", "format", "alpha"]
)
def test_invalid_page_propagates(tmp_path: Path, problem: str) -> None:
    page = make_page()
    if problem in {"width", "height", "source_index", "part"}:
        page = replace(
            page,
            width=0 if problem == "width" else page.width,
            height=0 if problem == "height" else page.height,
            source_index=0 if problem == "source_index" else page.source_index,
            part=0 if problem == "part" else page.part,
        )
    elif problem == "size":
        page = replace(page, width=page.width + 1)
    elif problem == "data":
        page = replace(page, data=b"broken image")
    elif problem == "format":
        page = make_page(format="BMP")
    else:
        with Image.new("RGB", (37, 53)) as image, io.BytesIO() as buffer:
            image.save(buffer, format="PNG", transparency=(0, 0, 0))
            page = replace(page, data=buffer.getvalue())
    path = tmp_path / "bad.pdf"
    with pytest.raises(FetchError), CorePdfWriter(path, title="", source_url="") as writer:
        writer.add_page(page)
    assert not path.exists()


def test_lifecycle_and_cancellation(tmp_path: Path) -> None:
    path = tmp_path / "existing.tmp"
    path.write_bytes(b"original")
    writer = CorePdfWriter(path, title="", source_url="")
    page = make_page()
    with pytest.raises(RuntimeError):
        writer.add_page(page)
    with pytest.raises(KeyboardInterrupt), writer:
        writer.add_page(page)
        with pytest.raises(RuntimeError):
            writer.__enter__()
        raise KeyboardInterrupt
    assert path.read_bytes() == b"original"
    with pytest.raises(RuntimeError):
        writer.add_page(page)
    with pytest.raises(RuntimeError):
        writer.__enter__()
    writer.__exit__(None, None, None)


@pytest.mark.parametrize("failure", ["metadata", "write", "open", "none"])
def test_resources_released(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    references: list[weakref.ReferenceType[PdfWriter]] = []
    streams: list[BinaryIO] = []
    original_write = PdfWriter.write

    def close(writer: PdfWriter) -> None:
        references.append(weakref.ref(writer))

    def write(writer: PdfWriter, output: BinaryIO) -> object:
        streams.append(output)
        if failure == "write":
            output.write(b"partial")
            raise OSError("write failed")
        return original_write(writer, output)

    def metadata(*args: object) -> None:
        raise OSError("metadata failed")

    monkeypatch.setattr(PdfWriter, "close", close)
    monkeypatch.setattr(PdfWriter, "write", write)
    if failure == "metadata":
        monkeypatch.setattr(PdfWriter, "add_metadata", metadata)
    path = tmp_path / "missing" / "book.pdf" if failure == "open" else tmp_path / "book.pdf"
    writer = CorePdfWriter(path, title="", source_url="")

    def run() -> None:
        with writer:
            writer.add_page(make_page())

    if failure == "none":
        run()
    else:
        with pytest.raises(OSError, match="failed|No such file"):
            run()
    gc.collect()
    assert len(references) == 1 and references[0]() is None
    assert all(stream.closed for stream in streams)


@pytest.mark.skipif(sys.platform not in {"darwin", "linux"}, reason="RSS units require macOS/Linux")
def test_200_page_rss(tmp_path: Path) -> None:
    """Distinct noisy and structured samples; generation/readback excluded from child RSS."""
    rng = random.Random(150)
    modes = [("RGB", "JPEG"), ("L", "JPEG"), ("RGB", "PNG"), ("L", "PNG"), ("1", "PNG")]
    for index in range(200):
        mode, format = modes[index % len(modes)]
        size = (801 + index % 7 * 23, 1103 + index % 11 * 17)
        row_bytes = (size[0] + 7) // 8 if mode == "1" else size[0] * (3 if mode == "RGB" else 1)
        with Image.frombytes(mode, size, rng.randbytes(row_bytes * size[1])) as image:
            if index % 2 == 0:
                ImageDraw.Draw(image).rectangle((0, 0, size[0] // 2, size[1]), fill=0)
            image.save(tmp_path / f"{index:03d}.img", format=format)
    code = """
import json, resource, sys, time
from pathlib import Path
from PIL import Image
from quire.assemble.models import ExportPage
from quire.assemble.pdf import CorePdfWriter
root = Path(sys.argv[1])
units = 1 if sys.platform == 'darwin' else 1024
rss = lambda: resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * units
start = time.perf_counter()
report = {'baseline_rss_bytes': rss(), 'input_bytes': 0, 'samples': []}
with CorePdfWriter(root / '200.pdf', title='RSS workload', source_url='') as writer:
    for index, path in enumerate(sorted(root.glob('*.img')), 1):
        with Image.open(path) as image:
            width, height = image.size
        data = path.read_bytes()
        writer.add_page(ExportPage(data, width, height, index, ''))
        report['input_bytes'] += len(data)
        if index % 50 == 0:
            report['samples'].append({'pages': index, 'rss_bytes': rss()})
report.update(pages=index, peak_rss_bytes=rss(), output_bytes=(root / '200.pdf').stat().st_size,
              seconds=time.perf_counter() - start)
print(json.dumps(report))
"""
    process = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    report = json.loads(process.stdout)
    sys.stdout.write("CorePdfWriter RSS: " + json.dumps(report) + "\n")
    assert report["pages"] == 200
    assert report["input_bytes"] > 50_000_000
    assert report["output_bytes"] > 50_000_000
    assert 0 < report["peak_rss_bytes"] < 500_000_000
    reader = PdfReader(tmp_path / "200.pdf", strict=True)
    assert (
        len(reader.pages)
        == len({cast(IndirectObject, p.indirect_reference).idnum for p in reader.pages})
        == 200
    )
    children = cast(list[Destination], reader.outline[1])
    assert [reader.get_destination_page_number(o) for o in children] == list(range(200))
