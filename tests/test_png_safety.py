from __future__ import annotations

import io
import struct
import tracemalloc
import zlib

import pytest
from PIL import Image
from pypdf import PdfReader

from quire.assemble.pdf_min import MiniPdfWriter
from quire.assemble.png import _flatten_alpha_png, _png_to_pdf_image, _unfilter
from quire.errors import UnsupportedError
from quire.manga import run_local


def test_transparent_color_png_yields_placeholder_capability(tmp_path):
    image = Image.new("RGB", (20, 30), "red")
    data = io.BytesIO()
    image.save(data, "PNG", transparency=(255, 0, 0))
    with MiniPdfWriter(tmp_path / "out.pdf") as pdf:
        assert not pdf.add_image_bytes(data.getvalue())


def test_alpha_gray_white_composite(tmp_path):
    image = Image.new("LA", (20, 30), (0, 128))
    data = io.BytesIO()
    image.save(data, "PNG")
    with MiniPdfWriter(tmp_path / "out.pdf") as pdf:
        assert pdf.add_image_bytes(data.getvalue())
    xobject = PdfReader(tmp_path / "out.pdf").pages[0]["/Resources"]["/XObject"]["/Im0"]
    assert xobject.get_data() == bytes([127]) * 600


def test_bounded_alpha_decompression_and_bad_scanlines():
    with pytest.raises(UnsupportedError):
        _flatten_alpha_png(100_000, 100_000, 6, b"")
    with pytest.raises(UnsupportedError):
        _flatten_alpha_png(1, 1, 6, zlib.compress(b"too long"))
    with pytest.raises(UnsupportedError):
        _unfilter(b"bad", 1, 1, 4)
    with pytest.raises(UnsupportedError):
        _unfilter(bytes([5, 1, 2, 3, 4]), 1, 1, 4)
    assert _unfilter(bytes([3, 10, 3, 10]), 1, 2, 1) == bytes([10, 15])


@pytest.mark.parametrize("bits,color,interlace", [(8, 2, 1), (8, 9, 0), (16, 6, 0), (8, 3, 0)])
def test_unsupported_png_variants(bits, color, interlace):
    ihdr = struct.pack(">IIBBBBB", 1, 1, bits, color, 0, 0, interlace)
    data = b"\x89PNG\r\n\x1a\n" + len(ihdr).to_bytes(4, "big") + b"IHDR" + ihdr + b"0000"
    with pytest.raises(UnsupportedError):
        _png_to_pdf_image(data)


def _png(idat, *, width=256, height=256, bits=8, color=2, compression=0):
    def chunk(kind, payload):
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload))
        )

    ihdr = struct.pack(">IIBBBBB", width, height, bits, color, compression, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


@pytest.mark.parametrize(
    "damage", ["zlib", "short", "long", "filter", "checksum", "truncated", "extra"]
)
def test_corrupt_opaque_png_keeps_page_position(tmp_path, damage):
    raw = bytes((256 * 3 + 1) * 256)
    idat = zlib.compress(raw)
    if damage == "zlib":
        idat = b"not zlib"
    elif damage == "short":
        idat = zlib.compress(raw[:-1])
    elif damage == "long":
        idat = zlib.compress(raw + b"\0")
    elif damage == "filter":
        offset = (256 * 3 + 1) * 100
        idat = zlib.compress(raw[:offset] + b"\5" + raw[offset + 1 :])
    elif damage == "checksum":
        idat = idat[:-1] + bytes([idat[-1] ^ 1])
    elif damage == "truncated":
        idat = idat[:-2]
    elif damage == "extra":
        idat += zlib.compress(b"extra")
    images = tmp_path / "images"
    images.mkdir()
    good = io.BytesIO()
    Image.new("RGB", (256, 256), "red").save(good, "PNG")
    (images / "1.png").write_bytes(good.getvalue())
    (images / "2.png").write_bytes(_png(idat))
    (images / "3.png").write_bytes(good.getvalue())
    result = run_local(images, tmp_path / "book.pdf")
    assert (result.pages_written, result.pages_failed, result.partial) == (2, 1, True)
    pages = PdfReader(result.output).pages
    assert len(pages) == 3 and "MISSING PAGE" in pages[1].extract_text()
    assert all("/XObject" in pages[i]["/Resources"] for i in (0, 2))


def test_opaque_png_validation_uses_bounded_memory():
    idat = zlib.compress(bytes((1024 * 3 + 1) * 2048))
    data = _png(idat, width=1024, height=2048)
    tracemalloc.start()
    try:
        image = _png_to_pdf_image(data)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert image.data == idat and image.pre_compressed
    assert peak < 512 * 1024, f"Validation retained inflated image data: {peak} bytes"


def test_opaque_png_budget_and_decompression_bomb():
    with pytest.raises(UnsupportedError, match="budget"):
        _png_to_pdf_image(_png(b"", width=100_000, height=100_000))
    bomb = _png(zlib.compress(bytes(2 * 1024 * 1024)), width=1, height=1)
    tracemalloc.start()
    try:
        with pytest.raises(UnsupportedError):
            _png_to_pdf_image(bomb)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 512 * 1024


@pytest.mark.parametrize("kwargs", [{"bits": 3}, {"compression": 1}, {"width": 0}])
def test_invalid_opaque_png_header_is_rejected(kwargs):
    with pytest.raises(UnsupportedError):
        _png_to_pdf_image(_png(zlib.compress(b"\0"), **kwargs))


@pytest.mark.parametrize("width,height", [(30_000, 3), (1, 20_000)])
def test_scanlines_cross_decompression_blocks(width, height):
    idat = zlib.compress(bytes((width * 3 + 1) * height))
    image = _png_to_pdf_image(_png(idat, width=width, height=height))
    assert image.data == idat
