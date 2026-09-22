"""OCR decode/resize budgets are enforced before allocating untrusted pixels."""

from __future__ import annotations

import logging
import struct
import subprocess
import zlib
from io import BytesIO

import pytest
from PIL import Image, PngImagePlugin

from quire.errors import FetchError
from quire.image.codec import MAX_DIM, MAX_PIXELS
from quire.ocr import preprocess
from quire.ocr.onnx_engine import OnnxEngine, _to_tensor
from quire.ocr.tesseract import OcrEngineError, TesseractEngine
from tests.test_ocr_onnx import make_engine, png_bytes


def oversized_png(size: tuple[int, int]) -> bytes:
    with Image.new("L", (1, 1)) as image, BytesIO() as buffer:
        image.save(buffer, format="PNG")
        data = buffer.getvalue()
    header = struct.pack(">II", *size) + data[24:29]
    crc = zlib.crc32(b"IHDR" + header)
    return data[:16] + header + struct.pack(">I", crc) + data[33:]


@pytest.mark.parametrize("size", [(4001, 4000), (1, MAX_DIM + 1)])
@pytest.mark.parametrize("kind", ["onnx", "tesseract"])
def test_engines_reject_size_before_load(
    size: tuple[int, int], kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = oversized_png(size)

    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("over-budget image reached decoding or subprocess")

    monkeypatch.setattr(PngImagePlugin.PngImageFile, "load", unexpected)
    monkeypatch.setattr(subprocess, "run", unexpected)
    engine = object.__new__(OnnxEngine) if kind == "onnx" else TesseractEngine("tesseract")
    with pytest.raises(OcrEngineError, match="dimensions or pixel count"):
        engine.recognize(data)


@pytest.mark.parametrize("size", [(1, MAX_DIM), (20, 10000), (10000, 20)])
def test_upscale_caps_both_allocation_budgets(
    size: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    targets: list[tuple[int, int]] = []

    def resize(image: Image.Image, size: tuple[int, int], *args: object) -> Image.Image:
        targets.append(size)
        return image

    monkeypatch.setattr(Image.Image, "resize", resize)
    with Image.new("L", size) as image:
        preprocess.upscale(image)
    target = targets[0] if targets else size
    assert max(target) <= MAX_DIM
    assert target[0] * target[1] <= MAX_PIXELS
    assert target[0] >= size[0] and target[1] >= size[1]


def test_tesseract_receives_only_first_frame_without_orientation_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with (
        Image.new("RGB", (30, 20), "white") as first,
        Image.new("RGB", (30, 20), "black") as second,
        BytesIO() as buffer,
    ):
        first.save(buffer, format="TIFF", save_all=True, append_images=[second])
        data = buffer.getvalue()

    def run(command: object, *, input: bytes, **kwargs: object) -> subprocess.CompletedProcess:
        with Image.open(BytesIO(input)) as image:
            assert image.size == (30, 20)
            assert getattr(image, "n_frames", 1) == 1
            assert image.getpixel((0, 0)) == (255, 255, 255)
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", run)
    assert TesseractEngine("tesseract").recognize(data).lines == ()


def test_recognition_tensor_rejects_extreme_line_before_resize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import numpy as np

    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail("oversized recognition target reached resize")

    monkeypatch.setattr(Image.Image, "resize", unexpected)
    with Image.new("L", (100000, 10)) as image:
        with pytest.raises(FetchError, match="dimensions or pixel count"):
            _to_tensor(np, image, (480000, 48))


def test_extreme_line_box_is_skipped_while_other_lines_recognize(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """极端宽高比行框被跳过（记日志），其余行照常识别。"""
    engine = make_engine()
    boxes = [(0, 0, 100000, 10), (20, 20, 280, 44)]
    monkeypatch.setattr(engine, "_detect", lambda image: boxes)
    with caplog.at_level(logging.WARNING, logger="quire.ocr.onnx_engine"):
        page = engine.recognize(png_bytes())
    assert [(line.text, line.x0, line.y0, line.x1, line.y1) for line in page.lines] == [
        ("你好", 20, 20, 280, 44)
    ]
    assert "line box" in caplog.text


def test_all_line_boxes_over_budget_fail_honestly(monkeypatch: pytest.MonkeyPatch) -> None:
    """所有行框尺寸都非法时才整页如实失败。"""
    engine = make_engine()
    monkeypatch.setattr(engine, "_detect", lambda image: [(0, 0, 100000, 10)])
    with pytest.raises(OcrEngineError, match="行框尺寸"):
        engine.recognize(png_bytes())
