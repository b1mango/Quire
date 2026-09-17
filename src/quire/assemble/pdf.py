"""Core image PDF output; compressed streams remain in pypdf until writing."""

from __future__ import annotations

import unicodedata
from contextlib import closing
from io import BytesIO
from pathlib import Path
from types import TracebackType
from urllib.parse import urlsplit, urlunsplit

from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    ContentStream,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    IndirectObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

from ..errors import FetchError
from ..utils.urls import redact
from .models import ExportPage

_PAPER_SIZES = {
    "a5": (419.53, 595.28),
    "a4": (595.28, 841.89),
    "b5": (498.90, 708.66),
    "letter": (612.0, 792.0),
}


def _clean_text(text: str) -> str:
    return "".join(char for char in text if unicodedata.category(char) not in {"Cc", "Cs"})


def _image_stream(page: ExportPage) -> DictionaryObject:
    with BytesIO(page.data) as buffer, closing(Image.open(buffer)) as image:
        if image.size != (page.width, page.height):
            raise ValueError("Export page dimensions do not match the image")
        jpeg = image.format == "JPEG"
        modes = {"RGB", "L", "CMYK"} if jpeg else {"RGB", "L", "1"}
        if image.format not in {"JPEG", "PNG"} or image.mode not in modes:
            raise ValueError("Expected a normalized RGB/L/1 PNG or RGB/L/CMYK JPEG")
        if not jpeg and "transparency" in image.info:
            raise ValueError("PNG transparency must be flattened before PDF export")
        colorspace = {"RGB": "/DeviceRGB", "CMYK": "/DeviceCMYK"}.get(image.mode, "/DeviceGray")
        stream = DecodedStreamObject()
        stream.update(
            {
                NameObject("/Type"): NameObject("/XObject"),
                NameObject("/Subtype"): NameObject("/Image"),
                NameObject("/Width"): NumberObject(page.width),
                NameObject("/Height"): NumberObject(page.height),
                NameObject("/ColorSpace"): NameObject(colorspace),
                NameObject("/BitsPerComponent"): NumberObject(1 if image.mode == "1" else 8),
            }
        )
        if jpeg:
            stream.set_data(page.data)
            stream[NameObject("/Filter")] = NameObject("/DCTDecode")
            if image.mode == "CMYK" and "adobe" in image.info:
                stream[NameObject("/Decode")] = ArrayObject(
                    [NumberObject(value) for value in (1, 0) * 4]
                )
            return stream
        # Packed 1-bit rows need no PNG predictor, including non-byte-aligned widths.
        stream.set_data(image.tobytes())
        return stream.flate_encode()


class CorePdfWriter:
    """Write to a caller-owned temporary path on successful context exit.

    Input pages have already been normalized by the image pipeline. Decoded
    pixels are local to add_page; encoded images accumulate for this document.
    Publication and removal of any partial temporary file belong to the caller.
    """

    def __init__(
        self,
        path: Path,
        *,
        title: str,
        source_url: str,
        dpi: int = 150,
        paper: str = "original",
    ) -> None:
        if dpi <= 0:
            raise ValueError("PDF dpi must be positive")
        if paper != "original" and paper not in _PAPER_SIZES:
            raise ValueError("Unknown PDF paper size")
        self.path = path
        self.title = _clean_text(title)
        self.source_url = urlunsplit(
            urlsplit(redact(_clean_text(source_url)))._replace(query="", fragment="")
        )
        self.dpi = dpi
        self.paper = paper
        self._writer: PdfWriter | None = None
        self._root: IndirectObject | None = None
        self._entered = False

    def __enter__(self) -> CorePdfWriter:
        if self._entered:
            raise RuntimeError("PDF writer contexts cannot be reused")
        self._entered = True
        writer = PdfWriter()
        try:
            writer.add_metadata(
                {"/Title": self.title, "/Subject": self.source_url, "/Producer": "Quire"}
            )
        except BaseException:
            writer.close()
            raise
        self._writer = writer
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        writer = self._writer
        if writer is None:
            return
        try:
            if exc_type is None:
                with self.path.open("wb") as output:
                    writer.write(output)
        finally:
            self._writer = None
            self._root = None
            writer.close()

    chapter: str = ""
    _chapter_name: str = ""
    _chapter_root: object = None

    def add_page(self, page: ExportPage) -> None:
        writer = self._writer
        if writer is None:
            raise RuntimeError("PDF writer must be inside its context")
        if min(page.width, page.height, page.source_index, page.part) <= 0:
            raise FetchError("Page dimensions, source index and part must be positive")
        try:
            stream = _image_stream(page)
        except Exception:
            raise FetchError("Invalid encoded page for PDF export") from None
        # pypdf registers a new clone only when this public attribute is present.
        stream.indirect_reference = None
        image = stream.clone(writer)
        assert image.indirect_reference is not None
        if self.paper == "original":
            width, height = page.width * 72 / self.dpi, page.height * 72 / self.dpi
            draw_width, draw_height = width, height
        else:
            width, height = _PAPER_SIZES[self.paper]
            scale = min(width / page.width, height / page.height)
            draw_width, draw_height = page.width * scale, page.height * scale
        pdf_page = writer.add_blank_page(width=width, height=height)
        resources = DictionaryObject(
            {
                NameObject("/XObject"): DictionaryObject(
                    {NameObject("/Im0"): image.indirect_reference}
                )
            }
        )
        content = ContentStream(None, writer)
        content.operations = [
            ([], b"q"),
            (
                [
                    FloatObject(value)
                    for value in (
                        draw_width,
                        0,
                        0,
                        draw_height,
                        (width - draw_width) / 2,
                        (height - draw_height) / 2,
                    )
                ],
                b"cm",
            ),
            ([NameObject("/Im0")], b"Do"),
            ([], b"Q"),
        ]
        if page.missing_reason is not None:
            font = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
            font.indirect_reference = None
            font = font.clone(writer)
            assert font.indirect_reference is not None
            resources[NameObject("/Font")] = DictionaryObject(
                {NameObject("/F0"): font.indirect_reference}
            )
            # The supplied PNG is visible; an invisible text layer makes it searchable.
            content.operations += [
                ([], b"q"),
                ([], b"BT"),
                ([NameObject("/F0"), NumberObject(12)], b"Tf"),
                ([NumberObject(3)], b"Tr"),
                ([TextStringObject("MISSING PAGE")], b"Tj"),
                ([], b"ET"),
                ([], b"Q"),
            ]
        pdf_page[NameObject("/Resources")] = resources
        pdf_page.replace_contents(content)
        if self._root is None:
            self._root = writer.add_outline_item(self.title or "Untitled", pdf_page)
        if self.chapter and self.chapter != self._chapter_name:
            self._chapter_root = writer.add_outline_item(self.chapter, pdf_page, parent=self._root)
            self._chapter_name = self.chapter
        writer.add_outline_item(
            f"Page {page.source_index} / part {page.part}",
            pdf_page,
            parent=self._chapter_root or self._root,  # type: ignore[arg-type]
        )
