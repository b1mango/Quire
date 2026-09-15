from __future__ import annotations

import io
import struct
import zlib

import pytest
from PIL import Image
from pypdf import PdfReader

from quire.assemble.pdf_min import MiniPdfWriter
from quire.assemble.png import _flatten_alpha_png, _png_to_pdf_image, _unfilter
from quire.errors import UnsupportedError


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
