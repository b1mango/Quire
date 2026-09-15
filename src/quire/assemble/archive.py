"""Sequential CBZ/ZIP output to a caller-owned temporary path."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import TracebackType
from typing import TypedDict
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

from ..image.probe import sniff_format
from ..utils.urls import redact
from .models import ExportPage


class _Page(TypedDict):
    file: str
    width: int
    height: int
    source_index: int
    part: int
    source_url: str
    missing_reason: str | None
    sha256: str


def _text(value: str) -> str:
    """Reject invalid XML 1.0 characters and non-whitespace control characters."""
    for char in value:
        code = ord(char)
        if (
            (code < 0x20 and char not in "\t\n\r")
            or 0x7F <= code <= 0x9F
            or 0xD800 <= code <= 0xDFFF
            or code in {0xFFFE, 0xFFFF}
        ):
            raise ValueError("Archive metadata contains an invalid text character")
    return value


def _source(url: str) -> str:
    # Validate before urlsplit can silently strip control characters.
    return _text(redact(_text(url)))


def _entry(name: str, compression: int) -> ZipInfo:
    info = ZipInfo(name)  # Fixed 1980 timestamp, independent of the local clock.
    info.compress_type = compression
    info.create_system = 3
    info.external_attr = 0o100600 << 16
    return info


class ArchiveWriter:
    """Write already-encoded JPEG/PNG pages without retaining their payloads.

    Source indices are positive and increasing; gaps support selected ranges.
    Each source starts at part 1 and continues without gaps. A missing source
    occupies one supplied placeholder page. Pixel validation belongs upstream.
    This single-use context neither publishes nor removes the temporary file.
    """

    def __init__(self, path: Path, *, format: str, title: str, source_url: str) -> None:
        if format not in {"cbz", "zip"}:
            raise ValueError("Archive format must be cbz or zip")
        self._path = path
        self._format = format
        self._title = _text(title)
        self._source_url = _source(source_url)
        self._pages: list[_Page] = []
        self._archive: ZipFile | None = None
        self._used = False
        self._failed = False

    def __enter__(self) -> ArchiveWriter:
        if self._used:
            raise RuntimeError("ArchiveWriter contexts cannot be reused")
        self._used = True
        self._archive = ZipFile(self._path, "w", compression=ZIP_STORED)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        archive = self._archive
        self._archive = None
        if archive is None:
            return
        try:
            if exc_type is None:
                if self._failed:
                    raise RuntimeError("Cannot finalize an archive after a failed write")
                name, data = self._metadata()
                archive.writestr(_entry(name, ZIP_DEFLATED), data, compresslevel=6)
        finally:
            archive.close()

    def add_page(self, page: ExportPage) -> None:
        archive = self._archive
        if archive is None or self._failed:
            raise RuntimeError("ArchiveWriter is not open for writing")
        for value in (page.width, page.height, page.source_index, page.part):
            if type(value) is not int or value <= 0:
                raise ValueError("Page dimensions, source_index and part must be positive integers")
        if len(self._pages) >= 999999:
            raise ValueError("Archive exceeds the six-digit page limit")
        self._check_order(page)
        extension = {"jpeg": "jpg", "png": "png"}.get(sniff_format(page.data))
        if extension is None:
            raise ValueError("Archive pages must contain JPEG or PNG bytes")
        metadata: _Page = {
            "file": f"{len(self._pages) + 1:06d}.{extension}",
            "width": page.width,
            "height": page.height,
            "source_index": page.source_index,
            "part": page.part,
            "source_url": _source(page.source_url),
            "missing_reason": (
                _text(page.missing_reason) if page.missing_reason is not None else None
            ),
            "sha256": hashlib.sha256(page.data).hexdigest(),
        }
        try:
            archive.writestr(_entry(metadata["file"], ZIP_STORED), page.data)
            self._pages.append(metadata)
        except BaseException:
            self._failed = True
            raise

    def _check_order(self, page: ExportPage) -> None:
        previous = self._pages[-1] if self._pages else None
        if previous is not None and page.source_index < previous["source_index"]:
            raise ValueError("Source pages must be in increasing order")
        if previous is not None and page.source_index == previous["source_index"]:
            if previous["missing_reason"] is not None or page.missing_reason is not None:
                raise ValueError("A missing source must occupy a single placeholder page")
            expected_part = previous["part"] + 1
        else:
            expected_part = 1
        if page.part != expected_part:
            raise ValueError("Source parts must start at 1 and be consecutive")

    def _metadata(self) -> tuple[str, bytes]:
        if self._format == "zip":
            manifest = {
                "schema": 1,
                "title": self._title,
                "source": self._source_url,
                "pages": self._pages,
            }
            return "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode(
                "utf-8"
            )
        root = ET.Element("ComicInfo")
        ET.SubElement(root, "Title").text = self._title
        ET.SubElement(root, "PageCount").text = str(len(self._pages))
        ET.SubElement(root, "Web").text = self._source_url
        pages = ET.SubElement(root, "Pages")
        for index, page in enumerate(self._pages):
            bookmark = f"Source {page['source_index']} / part {page['part']}"
            if page["missing_reason"] is not None:
                bookmark += f" / MISSING: {page['missing_reason']}"
            ET.SubElement(
                pages,
                "Page",
                {
                    "Image": str(index),
                    "ImageWidth": str(page["width"]),
                    "ImageHeight": str(page["height"]),
                    "Bookmark": bookmark,
                },
            )
        return "ComicInfo.xml", ET.tostring(root, encoding="utf-8", xml_declaration=True)
