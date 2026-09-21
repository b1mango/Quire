"""In-memory Pillow validation and first-frame color normalization."""

from __future__ import annotations

import warnings
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageCms, ImageOps

from ..errors import FetchError

MAX_PIXELS = 16_000_000
MAX_DIM = 100_000


@dataclass(frozen=True, slots=True)
class ImageInfo:
    format: str
    width: int
    height: int
    frames: int


def check_image_size(size: tuple[int, int]) -> None:
    """Reject dimensions before decoding or allocating transformed pixels."""
    width, height = size
    if not (0 < width <= MAX_DIM and 0 < height <= MAX_DIM and width * height <= MAX_PIXELS):
        raise FetchError("Image exceeds the allowed dimensions or pixel count")


@contextmanager
def decoded_image(data: bytes) -> Iterator[tuple[Image.Image, ImageInfo]]:
    """Yield a validated first frame without changing orientation or coordinates."""
    with ExitStack() as stack:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with BytesIO(data) as buffer, closing(Image.open(buffer)) as probe:
                    check_image_size(probe.size)
                    info = ImageInfo(
                        (probe.format or "").lower(),
                        probe.width,
                        probe.height,
                        int(getattr(probe, "n_frames", 1)),
                    )
                    probe.verify()
                # verify invalidates some decoders; load from a fresh, owned stream.
                buffer = stack.enter_context(BytesIO(data))
                image = stack.enter_context(closing(Image.open(buffer)))
                check_image_size(image.size)
                image.load()
                check_image_size(image.size)
        except FetchError:
            raise
        except Exception:
            raise FetchError("Image validation or first-frame decoding failed") from None
        yield image, info


def inspect_image(data: bytes) -> ImageInfo:
    """Validate decoding and normalization; report the oriented first-frame size."""
    with decoded_image(data) as (source, info), closing(_normalize_checked(source)) as image:
        return ImageInfo(info.format, image.width, image.height, info.frames)


def _srgb(image: Image.Image) -> Image.Image:
    profile = image.info["icc_profile"]
    if not isinstance(profile, bytes) or not profile:
        raise FetchError("Invalid or unsupported embedded ICC profile")
    try:
        source = ImageCms.ImageCmsProfile(BytesIO(profile))
        target = ImageCms.createProfile("sRGB")
        result = ImageCms.profileToProfile(image, source, target, outputMode="RGB")
        if result is None:
            raise ValueError("Missing color transform output")
        return result
    except Exception:
        raise FetchError("Invalid or unsupported embedded ICC profile") from None


def _convert_owned(image: Image.Image, mode: str) -> Image.Image:
    """Transfer ownership, releasing the old pixels only when conversion is needed."""
    if image.mode == mode:
        return image
    try:
        return image.convert(mode)
    finally:
        image.close()


def _normalize(image: Image.Image) -> Image.Image:
    """Consume the owned first frame and return its normalized replacement."""
    alpha = None
    try:
        ImageOps.exif_transpose(image, in_place=True)
        if image.mode in {"RGBA", "LA", "PA"}:
            alpha = image.getchannel("A")
        elif "transparency" in image.info:
            with closing(image.convert("RGBA")) as rgba:
                alpha = rgba.getchannel("A")
        mode = image.mode
        if mode in {"LA", "1"}:
            mode = "L"
        elif mode in {"P", "PA", "RGBA", "RGBX"}:
            mode = "RGB"
        # Avoid silently clipping high-depth samples to 8 bits, including in lossless.
        if mode not in {"L", "RGB", "CMYK", "LAB"}:
            raise FetchError("Unsupported image sample depth or color mode")
        image = _convert_owned(image, mode)
        if "icc_profile" in image.info:
            converted = _srgb(image)
            image.close()
            image = converted
        elif image.mode not in {"RGB", "L"}:
            image = _convert_owned(image, "RGB")
        if alpha is not None:
            image = _convert_owned(image, "RGB")
            # Paste the white background in place, after converting colors to sRGB.
            with closing(ImageOps.invert(alpha)) as inverse:
                image.paste("white", mask=inverse)
        return image
    except BaseException:
        image.close()
        raise
    finally:
        if alpha is not None:
            alpha.close()


@contextmanager
def normalized_image(data: bytes) -> Iterator[Image.Image]:
    """Yield an owned, oriented, opaque first frame; close it on context exit."""
    with decoded_image(data) as (source, _):
        with closing(_normalize_checked(source)) as image:
            yield image


def _normalize_checked(source: Image.Image) -> Image.Image:
    try:
        image = _normalize(source)
        image.info.clear()
        return image
    except FetchError:
        raise
    except Exception:
        raise FetchError("Image orientation or color normalization failed") from None
