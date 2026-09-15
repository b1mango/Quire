"""Conservative classification without sampling or thresholding."""

from __future__ import annotations

from contextlib import ExitStack, closing
from typing import Literal

from PIL import Image, ImageChops

from ..errors import FetchError

PageKind = Literal["color", "gray", "bitonal"]


def classify(image: Image.Image) -> PageKind:
    """Classify normalized pixels exactly, retaining even single-pixel color."""
    try:
        if image.mode == "1":
            return "bitonal"
        with ExitStack() as stack:
            gray = image
            if image.mode != "L":
                rgb = stack.enter_context(closing(image.convert("RGB")))
                red, green, blue = (stack.enter_context(closing(band)) for band in rgb.split())
                rg = stack.enter_context(closing(ImageChops.difference(red, green)))
                rb = stack.enter_context(closing(ImageChops.difference(red, blue)))
                if rg.getbbox() is not None or rb.getbbox() is not None:
                    return "color"
                gray = red
            return "gray" if any(gray.histogram()[1:255]) else "bitonal"
    except Exception:
        raise FetchError("Image classification failed") from None
