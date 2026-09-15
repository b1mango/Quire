from __future__ import annotations

import json
import struct
import subprocess
import sys
import traceback
import zlib
from contextlib import closing
from dataclasses import FrozenInstanceError
from io import BytesIO
from typing import Any

import pytest
from PIL import Image, ImageCms, ImageFile, ImageOps

from quire.errors import FetchError
from quire.image import codec
from quire.image.codec import ImageInfo, inspect_image, normalized_image

FORMATS = ("JPEG", "PNG", "WEBP", "GIF", "TIFF", "AVIF")


def pack(image: Image.Image, fmt: str = "PNG", **kwargs: Any) -> bytes:
    with BytesIO() as buffer:
        image.save(buffer, format=fmt, **kwargs)
        return buffer.getvalue()


@pytest.mark.parametrize("fmt", FORMATS)
def test_supported_formats_and_owned_image(fmt: str) -> None:
    with Image.new("RGB", (23, 17), (31, 110, 201)) as source:
        data = pack(source, fmt)
    assert inspect_image(data) == ImageInfo(fmt.lower(), 23, 17, 1)
    with normalized_image(data) as image:
        assert image.size == (23, 17)
        assert image.mode == "RGB"
        assert image.info == {}
        assert image.getbbox() is not None
    with pytest.raises(ValueError):
        image.getpixel((0, 0))


def test_image_info_is_frozen() -> None:
    info = ImageInfo("png", 2, 3, 1)
    with pytest.raises(FrozenInstanceError):
        info.width = 4  # type: ignore[misc]


@pytest.mark.parametrize("fmt", FORMATS)
def test_truncated_pixels_are_rejected(fmt: str) -> None:
    with Image.effect_noise((64, 64), 60).convert("RGB") as source:
        data = pack(source, fmt)
    with pytest.raises(FetchError):
        inspect_image(data[: len(data) // 2])
    with pytest.raises(FetchError), normalized_image(data[: len(data) // 2]):
        pytest.fail("Truncated image was accepted")


@pytest.mark.parametrize("data", [b"", b"not an image", b"\xff\xd8private-payload\xff\xd9"])
def test_unrecognized_data_is_redacted(data: bytes) -> None:
    with pytest.raises(FetchError) as caught:
        inspect_image(data)
    assert "private-payload" not in "".join(traceback.format_exception(caught.value))
    assert caught.value.__cause__ is None


def chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload))
    )


def broken_png(width: int, height: int, payload: bytes = b"invalid-deflate") -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", payload) + chunk(b"IEND", b"")
    )


def test_verify_alone_is_insufficient() -> None:
    data = broken_png(4, 4)
    with Image.open(BytesIO(data)) as image:
        image.verify()
    with pytest.raises(FetchError):
        inspect_image(data)


@pytest.mark.parametrize("size", [(4001, 4000), (100001, 1), (1, 100001), (20000, 20000)])
def test_bomb_and_dimension_limits_precede_pixel_load(size: tuple[int, int]) -> None:
    original_limits = (Image.MAX_IMAGE_PIXELS, ImageFile.LOAD_TRUNCATED_IMAGES)
    with pytest.raises(FetchError):
        inspect_image(broken_png(*size))
    assert (Image.MAX_IMAGE_PIXELS, ImageFile.LOAD_TRUNCATED_IMAGES) == original_limits
    assert codec.MAX_PIXELS == 16_000_000
    assert codec.MAX_DIM == 100_000


@pytest.mark.parametrize("size", [(100000, 1), (1, 100000), (4000, 4000)])
def test_exact_limits_are_accepted(size: tuple[int, int]) -> None:
    with Image.new("L", size, 128) as image:
        info = inspect_image(pack(image))
    assert (info.width, info.height) == size


@pytest.mark.parametrize("orientation", range(1, 9))
def test_all_exif_orientations(orientation: int) -> None:
    methods = {
        2: Image.Transpose.FLIP_LEFT_RIGHT,
        3: Image.Transpose.ROTATE_180,
        4: Image.Transpose.FLIP_TOP_BOTTOM,
        5: Image.Transpose.TRANSPOSE,
        6: Image.Transpose.ROTATE_270,
        7: Image.Transpose.TRANSVERSE,
        8: Image.Transpose.ROTATE_90,
    }
    with Image.new("RGB", (3, 2)) as source:
        source.putdata(
            [
                (10, 20, 30),
                (40, 50, 60),
                (70, 80, 90),
                (110, 120, 130),
                (140, 150, 160),
                (170, 180, 190),
            ]
        )
        exif = Image.Exif()
        exif[274] = orientation
        data = pack(source, exif=exif)
        expected = source.transpose(methods[orientation]) if orientation != 1 else source.copy()
        with expected, normalized_image(data) as actual:
            assert actual.size == expected.size
            assert actual.tobytes() == expected.tobytes()
            info = inspect_image(data)
            assert (info.width, info.height) == expected.size


def test_tiff_orientation_applied_only_once() -> None:
    with Image.new("RGB", (3, 2)) as source:
        source.putdata([(i, i + 1, i + 2) for i in range(0, 60, 10)])
        exif = Image.Exif()
        exif[274] = 6
        data = pack(source, "TIFF", exif=exif)
        with source.transpose(Image.Transpose.ROTATE_270) as expected:
            with normalized_image(data) as actual:
                assert actual.size == expected.size
                assert actual.tobytes() == expected.tobytes()
                info = inspect_image(data)
                assert (info.width, info.height) == expected.size


@pytest.mark.parametrize(
    ("mode", "pixel", "expected"),
    [
        ("RGBA", (0, 60, 120, 0), (255, 255, 255)),
        ("RGBA", (0, 60, 120, 128), (127, 157, 187)),
        ("LA", (0, 128), (127, 127, 127)),
    ],
)
def test_alpha_white_composite(
    mode: str, pixel: tuple[int, ...], expected: tuple[int, ...]
) -> None:
    with Image.new(mode, (2, 2), pixel) as source:
        with normalized_image(pack(source)) as actual:
            assert actual.mode == "RGB"
            assert actual.getpixel((0, 0)) == expected


@pytest.mark.parametrize("transparent", [False, True])
def test_palette_to_rgb(transparent: bool) -> None:
    with Image.new("P", (2, 1)) as source:
        source.putpalette([7, 7, 7, 20, 40, 60] + [0] * 762)
        source.putdata([0, 1])
        data = pack(source, transparency=0) if transparent else pack(source)
    with normalized_image(data) as actual:
        assert actual.getpixel((0, 0)) == ((255, 255, 255) if transparent else (7, 7, 7))
        assert actual.getpixel((1, 0)) == (20, 40, 60)


def test_rgb_color_key_transparency() -> None:
    with Image.new("RGB", (2, 1), (20, 40, 60)) as source:
        data = pack(source, transparency=(20, 40, 60))
    with normalized_image(data) as actual:
        assert actual.getpixel((0, 0)) == (255, 255, 255)


def test_cmyk_jpeg_conversion() -> None:
    with Image.new("CMYK", (16, 12), (50, 100, 150, 10)) as source:
        data = pack(source, "JPEG")
    with Image.open(BytesIO(data)) as decoded, decoded.convert("RGB") as expected:
        with normalized_image(data) as actual:
            assert actual.mode == "RGB"
            assert actual.tobytes() == expected.tobytes()


@pytest.mark.parametrize("alpha", [False, True])
def test_valid_srgb_profile(alpha: bool) -> None:
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    mode, pixel = ("RGBA", (20, 60, 100, 128)) if alpha else ("RGB", (20, 60, 100))
    with Image.new(mode, (3, 2), pixel) as source:
        data = pack(source, icc_profile=profile)
    with normalized_image(data) as actual:
        assert actual.getpixel((0, 0)) == ((137, 157, 177) if alpha else pixel)
        assert "icc_profile" not in actual.info


def test_lab_profile_converted_by_littlecms() -> None:
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("LAB"))
    with Image.new("LAB", (3, 2), (128, 150, 170)) as source:
        data = pack(source, "TIFF", icc_profile=profile.tobytes())
        expected = ImageCms.profileToProfile(
            source, profile, ImageCms.createProfile("sRGB"), outputMode="RGB"
        )
        assert expected is not None
        with expected, normalized_image(data) as actual:
            assert actual.tobytes() == expected.tobytes()


@pytest.mark.parametrize("profile", [b"", b"private-invalid-profile"])
def test_invalid_icc_is_explicit_and_redacted(profile: bytes) -> None:
    with Image.new("RGB", (3, 2)) as source:
        data = pack(source)
    data = data[:33] + chunk(b"iCCP", b"test\0\0" + zlib.compress(profile)) + data[33:]
    with pytest.raises(FetchError, match="ICC"):
        inspect_image(data)
    with pytest.raises(FetchError, match="ICC") as caught, normalized_image(data):
        pytest.fail("Invalid ICC was accepted")
    assert "private-invalid-profile" not in "".join(traceback.format_exception(caught.value))


def test_incompatible_icc_is_rejected() -> None:
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("LAB")).tobytes()
    with Image.new("RGB", (3, 2)) as source:
        data = pack(source, icc_profile=profile)
    with pytest.raises(FetchError, match="ICC"):
        inspect_image(data)
    with pytest.raises(FetchError, match="ICC"), normalized_image(data):
        pytest.fail("Mismatched profile was accepted")


@pytest.mark.parametrize("fmt", ["GIF", "TIFF", "WEBP", "PNG", "AVIF"])
def test_multiframe_decodes_first_frame_only(fmt: str) -> None:
    with Image.new("RGB", (16, 12), "red") as first, Image.new("RGB", (16, 12), "blue") as last:
        data = pack(first, fmt, save_all=True, append_images=[last], duration=100, loop=0)
    assert inspect_image(data).frames == 2
    with Image.open(BytesIO(data)) as source, source.convert("RGB") as expected:
        with normalized_image(data) as actual:
            assert actual.tobytes() == expected.tobytes()


def test_consumer_error_not_mislabeled_as_decode_failure() -> None:
    with Image.new("L", (2, 2), 100) as source:
        data = pack(source)
    with pytest.raises(RuntimeError, match="consumer"), normalized_image(data) as image:
        raise RuntimeError("consumer")
    with pytest.raises(ValueError):
        image.getpixel((0, 0))


def test_normalization_error_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: Any, **kwargs: Any) -> Image.Image:
        raise ValueError("private-payload")

    with Image.new("L", (2, 2)) as source:
        data = pack(source)
    monkeypatch.setattr(ImageOps, "exif_transpose", fail)
    with pytest.raises(FetchError, match="normalization") as caught, normalized_image(data):
        pytest.fail("Fault was accepted")
    assert "private-payload" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("mode", ["I;16", "F"])
def test_unsupported_depth_fails_without_silent_clipping(mode: str) -> None:
    with Image.new(mode, (2, 2), 1000) as source:
        data = pack(source, "TIFF")
    with pytest.raises(FetchError, match="sample depth"):
        inspect_image(data)
    with pytest.raises(FetchError, match="sample depth"), normalized_image(data):
        pytest.fail("Unsupported depth was silently clipped")


@pytest.mark.skipif(sys.platform not in {"darwin", "linux"}, reason="RSS units require macOS/Linux")
@pytest.mark.parametrize("operation", ["inspect", "normalize"])
def test_rgba_icc_16mp_peak_rss(operation: str) -> None:
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    with closing(Image.new("RGBA", (4000, 4000), (20, 60, 100, 128))) as source:
        data = pack(source, icc_profile=profile)
    # exec a fresh interpreter: sample generation is excluded from the child's RSS.
    script = """
import json
import resource
import sys
from quire.image.codec import inspect_image, normalized_image

data = sys.stdin.buffer.read()
if sys.argv[1] == "inspect":
    info = inspect_image(data)
    assert (info.format, info.width, info.height, info.frames) == ("png", 4000, 4000, 1)
else:
    with normalized_image(data) as image:
        assert image.size == (4000, 4000) and image.mode == "RGB"
        assert image.getpixel((3999, 3999)) == (137, 157, 177)
rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
sys.stdout.write(json.dumps({"peak_rss_bytes": int(rss * (1 if sys.platform == "darwin" else 1024))}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, operation],
        input=data,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr.decode()
    peak = json.loads(result.stdout)["peak_rss_bytes"]
    sys.stdout.write(f"{operation}: peak RSS {peak:,} bytes\n")
    assert 0 < peak <= 500_000_000, f"{operation}: peak RSS {peak:,} bytes"
