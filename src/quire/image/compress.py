"""Bounded, sequential page splitting, resizing and encoding."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from io import BytesIO

from PIL import Image

from ..errors import FetchError
from .analyze import classify
from .codec import normalized_image
from .options import CompressionOptions, Encoding


@dataclass(frozen=True, slots=True)
class EncodedPage:
    data: bytes
    width: int
    height: int
    kind: str


def _keep_jpeg(data: bytes) -> bool:
    # Called only after normalized_image has verified and fully decoded the input.
    with BytesIO(data) as buffer, closing(Image.open(buffer)) as image:
        return (
            image.format == "JPEG"
            and image.mode in {"RGB", "L"}
            and image.getexif().get(274, 1) == 1
            and "icc_profile" not in image.info
        )


def _encode(image: Image.Image, encoding: Encoding, options: CompressionOptions) -> EncodedPage:
    kind = classify(image)
    binary = kind == "bitonal" and options.bitonal
    mode = "1" if binary else "RGB" if kind == "color" else "L"
    with closing(image.convert(mode, dither=Image.Dither.NONE)) as output, BytesIO() as buffer:
        output.info.clear()
        if encoding.lossless or binary:
            output.save(buffer, format="PNG", optimize=True)
        else:
            # Pillow's optimize buffer estimate can underflow for high-entropy 4:4:4 images.
            output.save(
                buffer, format="JPEG", quality=encoding.quality, optimize=False, subsampling=0
            )
        return EncodedPage(buffer.getvalue(), output.width, output.height, kind)


def encode_pages(
    data: bytes,
    encoding: Encoding,
    options: CompressionOptions,
) -> Iterator[EncodedPage]:
    """Yield one encoded crop at a time; target-size budgeting belongs to the caller."""
    try:
        if encoding.max_edge < 0 or (not encoding.lossless and not 1 <= encoding.quality <= 100):
            raise FetchError("Invalid image encoding parameters")
        with normalized_image(data) as image:
            step = image.height
            if options.split_tall and image.height > image.width * 8:
                step = image.width * 2
            if encoding.lossless and step == image.height and _keep_jpeg(data):
                yield EncodedPage(data, image.width, image.height, classify(image))
                return
            for top in range(0, image.height, step):
                box = (0, top, image.width, min(top + step, image.height))
                with closing(image.crop(box)) as page:
                    if not encoding.lossless and encoding.max_edge:
                        page.thumbnail(
                            (encoding.max_edge, encoding.max_edge),
                            resample=Image.Resampling.LANCZOS,
                        )
                    yield _encode(page, encoding, options)
    except FetchError:
        raise
    except Exception:
        raise FetchError("Image page encoding failed") from None
