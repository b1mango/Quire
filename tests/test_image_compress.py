from __future__ import annotations

import traceback
from collections.abc import Generator
from dataclasses import FrozenInstanceError
from io import BytesIO
from typing import Any

import pytest
from PIL import Image, ImageCms
from PIL.JpegImagePlugin import JpegImageFile

from quire.errors import FetchError
from quire.image import compress
from quire.image.analyze import classify
from quire.image.codec import normalized_image
from quire.image.compress import EncodedPage, encode_pages
from quire.image.options import CompressionOptions, Encoding

LOSSLESS = Encoding(0, 0, True)
DEFAULT = Encoding(80, 2000)
OPTIONS = CompressionOptions(target_bytes=None)


def pack(image: Image.Image, fmt: str = "PNG", **kwargs: Any) -> bytes:
    with BytesIO() as buffer:
        image.save(buffer, format=fmt, **kwargs)
        return buffer.getvalue()


@pytest.mark.parametrize(
    ("mode", "pixels", "expected"),
    [
        ("RGB", [(0, 0, 0), (255, 255, 255)], "bitonal"),
        ("RGB", [(20, 20, 20), (128, 128, 128)], "gray"),
        ("RGB", [(254, 255, 255), (128, 128, 128)], "color"),
        ("L", [0, 255], "bitonal"),
        ("L", [0, 254], "gray"),
        ("L", [1, 255], "gray"),
        ("1", [0, 255], "bitonal"),
    ],
)
def test_exact_classification(mode: str, pixels: list[Any], expected: str) -> None:
    with Image.new(mode, (2, 1)) as image:
        image.putdata(pixels)
        assert classify(image) == expected


def test_single_pale_color_pixel_never_sampled_away() -> None:
    with Image.new("RGB", (2000, 1000), "white") as image:
        image.putpixel((1999, 999), (255, 254, 255))
        assert classify(image) == "color"


def test_classification_failure_is_redacted() -> None:
    image = Image.new("RGB", (2, 2))
    image.close()
    with pytest.raises(FetchError, match="classification"):
        classify(image)


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP", "GIF", "TIFF", "AVIF"])
def test_all_formats_encode_to_readable_pages(fmt: str) -> None:
    with Image.new("RGB", (32, 24), (70, 120, 160)) as image:
        pages = list(encode_pages(pack(image, fmt), DEFAULT, OPTIONS))
    assert len(pages) == 1
    assert (pages[0].width, pages[0].height, pages[0].kind) == (32, 24, "color")
    with Image.open(BytesIO(pages[0].data)) as output:
        output.load()
        assert output.format == "JPEG"
        assert output.mode == "RGB"


def test_encoded_page_is_frozen() -> None:
    page = EncodedPage(b"png", 1, 1, "gray")
    with pytest.raises(FrozenInstanceError):
        page.width = 2  # type: ignore[misc]


@pytest.mark.parametrize("bitonal", [False, True])
@pytest.mark.parametrize("lossless", [False, True])
@pytest.mark.parametrize("mode", ["1", "L", "RGB"])
def test_binary_output_switch(mode: str, lossless: bool, bitonal: bool) -> None:
    with Image.new(mode, (16, 16)) as source:
        source.paste("white", (8, 0, 16, 16))
        pages = list(
            encode_pages(
                pack(source),
                LOSSLESS if lossless else DEFAULT,
                CompressionOptions(target_bytes=None, bitonal=bitonal),
            )
        )
    with Image.open(BytesIO(pages[0].data)) as actual:
        assert pages[0].kind == "bitonal"
        assert actual.mode == ("1" if bitonal else "L")
        assert actual.format == ("PNG" if lossless or bitonal else "JPEG")
        assert actual.getpixel((0, 0)) == 0
        assert actual.getpixel((15, 0)) == 255


def test_dot_pattern_preserved_without_thresholding() -> None:
    with Image.new("L", (32, 32)) as source:
        source.putdata([0 if (x + y) % 2 else 255 for y in range(32) for x in range(32)])
        binary = list(encode_pages(pack(source), DEFAULT, OPTIONS))[0]
        with Image.open(BytesIO(binary.data)) as output, output.convert("L") as gray:
            assert gray.tobytes() == source.tobytes()
        source.putpixel((15, 15), 254)
        page = list(encode_pages(pack(source), LOSSLESS, OPTIONS))[0]
        assert page.kind == "gray"
        with Image.open(BytesIO(page.data)) as output:
            assert output.mode == "L"
            assert output.tobytes() == source.tobytes()


def test_grayscale_jpeg_and_pale_color_jpeg() -> None:
    cases: list[tuple[int | tuple[int, int, int], str, str]] = [
        (128, "L", "gray"),
        ((254, 255, 255), "RGB", "color"),
    ]
    for pixel, mode, kind in cases:
        with Image.new(mode, (20, 20), pixel) as source:
            page = list(encode_pages(pack(source), DEFAULT, OPTIONS))[0]
        with Image.open(BytesIO(page.data)) as output:
            assert output.format == "JPEG"
            assert output.mode == mode
            assert page.kind == kind


@pytest.mark.parametrize("height", [160, 161, 169, 180])
@pytest.mark.parametrize("split", [False, True])
def test_tall_crops_contiguous_without_omissions(height: int, split: bool) -> None:
    with Image.new("RGB", (20, height)) as source:
        source.putdata([(x, y, (x + y) % 256) for y in range(height) for x in range(20)])
        pages = list(
            encode_pages(
                pack(source), LOSSLESS, CompressionOptions(target_bytes=None, split_tall=split)
            )
        )
        expected_heights = (
            [40] * (height // 40) + ([height % 40] if height % 40 else [])
            if split and height > 160
            else [height]
        )
        assert [page.height for page in pages] == expected_heights
        with Image.new("RGB", source.size) as joined:
            top = 0
            for page in pages:
                with Image.open(BytesIO(page.data)) as output:
                    joined.paste(output, (0, top))
                top += page.height
            assert joined.tobytes() == source.tobytes()


def test_split_before_resizing_and_do_not_enlarge_last_crop() -> None:
    with Image.new("RGB", (100, 850), (10, 30, 60)) as source:
        pages = list(encode_pages(pack(source), Encoding(88, 100), OPTIONS))
    assert [(p.width, p.height) for p in pages] == [(50, 100)] * 4 + [(100, 50)]


@pytest.mark.parametrize(
    "size,limit,expected",
    [
        ((160, 80), 100, (100, 50)),
        ((80, 160), 100, (50, 100)),
        ((20, 10), 100, (20, 10)),
        ((160, 80), 0, (160, 80)),
    ],
)
def test_dimension_limits_without_upscaling(
    size: tuple[int, int],
    limit: int,
    expected: tuple[int, int],
) -> None:
    with Image.new("RGB", size, (10, 30, 60)) as image:
        page = list(encode_pages(pack(image), Encoding(88, limit), OPTIONS))[0]
    assert (page.width, page.height) == expected


def test_lossless_ignores_resize_limit() -> None:
    with Image.new("RGB", (160, 80), (10, 30, 60)) as source:
        page = list(encode_pages(pack(source), Encoding(0, 30, True), OPTIONS))[0]
        assert (page.width, page.height) == source.size
        with Image.open(BytesIO(page.data)) as output:
            assert output.tobytes() == source.tobytes()


@pytest.mark.parametrize("orientation", [None, 1])
@pytest.mark.parametrize("mode", ["RGB", "L"])
def test_lossless_jpeg_keeps_original_bytes(orientation: int | None, mode: str) -> None:
    with Image.new(mode, (30, 20)) as source:
        exif = Image.Exif()
        if orientation is not None:
            exif[274] = orientation
        data = pack(source, "JPEG", exif=exif)
    assert list(encode_pages(data, LOSSLESS, OPTIONS))[0].data is data


@pytest.mark.parametrize("reason", ["orientation", "icc", "cmyk", "split"])
def test_lossless_jpeg_normalizes_when_required(reason: str) -> None:
    kwargs: dict[str, Any] = {}
    if reason == "orientation":
        exif = Image.Exif()
        exif[274] = 6
        kwargs["exif"] = exif
    if reason == "icc":
        kwargs["icc_profile"] = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    mode = "CMYK" if reason == "cmyk" else "RGB"
    size = (20, 170) if reason == "split" else (30, 20)
    with Image.new(mode, size) as source:
        data = pack(source, "JPEG", **kwargs)
    pages = list(encode_pages(data, LOSSLESS, OPTIONS))
    with normalized_image(data) as expected, Image.new("RGB", expected.size) as joined:
        top = 0
        for page in pages:
            with Image.open(BytesIO(page.data)) as output:
                assert output.format == "PNG"
                assert not output.info
                joined.paste(output, (0, top))
                top += page.height
        with expected.convert("RGB") as rgb:
            assert joined.tobytes() == rgb.tobytes()


def test_no_split_lossless_tall_jpeg_keeps_bytes() -> None:
    with Image.new("RGB", (20, 170)) as image:
        data = pack(image, "JPEG")
    options = CompressionOptions(target_bytes=None, split_tall=False)
    assert list(encode_pages(data, LOSSLESS, options))[0].data is data


@pytest.mark.parametrize("quality", [60, 70, 80, 88, 95])
def test_requested_quality_reaches_jpeg_quantization(quality: int) -> None:
    with Image.new("RGB", (32, 24), (50, 100, 150)) as image:
        page = list(encode_pages(pack(image), Encoding(quality, 0), OPTIONS))[0]
        reference = pack(image, "JPEG", quality=quality, optimize=False, subsampling=0)
    with Image.open(BytesIO(page.data)) as actual, Image.open(BytesIO(reference)) as expected:
        assert isinstance(actual, JpegImageFile) and isinstance(expected, JpegImageFile)
        assert actual.quantization == expected.quantization


def test_metadata_stripped_after_reencoding() -> None:
    exif = Image.Exif()
    exif[315] = "private-artist"
    with Image.new("RGB", (30, 20), (20, 40, 60)) as source:
        data = pack(source, "JPEG", exif=exif, comment=b"private-comment")
    page = list(encode_pages(data, DEFAULT, OPTIONS))[0]
    assert b"private-artist" not in page.data and b"private-comment" not in page.data
    with Image.open(BytesIO(page.data)) as output:
        assert "exif" not in output.info
        assert "comment" not in output.info
        assert "icc_profile" not in output.info


def test_generator_encodes_only_requested_crop(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    original = compress._encode

    def record(image: Image.Image, encoding: Encoding, options: CompressionOptions) -> EncodedPage:
        nonlocal calls
        calls += 1
        return original(image, encoding, options)

    monkeypatch.setattr(compress, "_encode", record)
    with Image.new("RGB", (20, 170)) as source:
        pages = encode_pages(pack(source), LOSSLESS, OPTIONS)
    assert calls == 0
    assert next(pages).height == 40
    assert calls == 1
    assert isinstance(pages, Generator)
    pages.close()
    assert calls == 1


def test_encoder_errors_are_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    with Image.new("RGB", (20, 20)) as source:
        data = pack(source)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError("private-payload")

    monkeypatch.setattr(Image.Image, "save", fail)
    with pytest.raises(FetchError, match="encoding") as caught:
        list(encode_pages(data, DEFAULT, OPTIONS))
    assert "private-payload" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("encoding", [Encoding(0, 100), Encoding(101, 100), Encoding(80, -1)])
def test_invalid_encoding_is_domain_error(encoding: Encoding) -> None:
    with pytest.raises(FetchError, match="parameters"):
        list(encode_pages(b"", encoding, OPTIONS))


def test_invalid_image_cannot_bypass_lossless_validation() -> None:
    with pytest.raises(FetchError):
        list(encode_pages(b"\xff\xd8private-payload\xff\xd9", LOSSLESS, OPTIONS))
