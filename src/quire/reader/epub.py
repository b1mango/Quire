"""读取 EPUB 的线性章节、目录和受限图片资源。"""

from __future__ import annotations

import mimetypes
import posixpath
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote, unquote, urlsplit
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from .archive import read_entry, safe_path
from .sanitize import sanitize

_XHTML_LIMIT = 8 * 1024 * 1024


def _xml(data: bytes) -> ET.Element:
    upper = data.upper().replace(b"\x00", b"")
    if b"<!DOCTYPE" in upper and b"<!ENTITY" in upper:
        raise ValueError("不允许 XML 外部实体")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError("EPUB XML 格式无效") from exc


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _resolve(base: str, href: str) -> str:
    parts = urlsplit(href)
    path = unquote(parts.path)
    if parts.scheme or parts.netloc or path.startswith("/") or "\\" in path or "\x00" in path:
        raise ValueError("EPUB 路径无效")
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(base), path)) if path else base
    return safe_path(resolved)


class Epub:
    def __init__(self, path: Path | BinaryIO) -> None:
        self.path = path
        try:
            self._load()
        except (KeyError, StopIteration) as exc:
            raise ValueError("EPUB 结构无效") from exc

    def _archive(self) -> ZipFile:
        if not isinstance(self.path, Path):
            self.path.seek(0)
        return ZipFile(self.path)

    def _load(self) -> None:
        with self._archive() as archive:
            container = _xml(read_entry(archive, "META-INF/container.xml"))
            rootfile = next(e for e in container.iter() if _local(e.tag) == "rootfile")
            self.opf = safe_path(rootfile.attrib["full-path"])
            package = _xml(read_entry(archive, self.opf))
            self.root = posixpath.dirname(self.opf)
            manifest = next(e for e in package if _local(e.tag) == "manifest")
            items = {
                item.attrib["id"]: (
                    _resolve(self.opf, item.attrib["href"]),
                    item.attrib.get("media-type", ""),
                    item.attrib.get("properties", "").split(),
                )
                for item in manifest
                if _local(item.tag) == "item"
            }
            metadata = next((e for e in package if _local(e.tag) == "metadata"), None)
            dc = "{http://purl.org/dc/elements/1.1/}"
            self.title = ""
            self.author = ""
            cover_id = ""
            if metadata is not None:
                self.title = next(
                    ("".join(e.itertext()).strip() for e in metadata if e.tag == dc + "title"), ""
                )
                self.author = next(
                    ("".join(e.itertext()).strip() for e in metadata if e.tag == dc + "creator"), ""
                )
                cover_id = next(
                    (
                        e.attrib.get("content", "")
                        for e in metadata
                        if _local(e.tag) == "meta" and e.attrib.get("name") == "cover"
                    ),
                    "",
                )
            self.cover_path = next(
                (
                    path
                    for path, media, props in items.values()
                    if media.startswith("image/") and "cover-image" in props
                ),
                None,
            )
            if self.cover_path is None and cover_id in items:
                path, media, _ = items[cover_id]
                if media.startswith("image/"):
                    self.cover_path = path
            self.images = {path for path, media, _ in items.values() if media.startswith("image/")}
            spine = next(e for e in package if _local(e.tag) == "spine")
            self.paths = [
                items[item.attrib["idref"]][0]
                for item in spine
                if _local(item.tag) == "itemref" and item.attrib.get("linear") != "no"
            ]
            nav = next((path for path, _, props in items.values() if "nav" in props), None)
            ncx = next(
                (path for path, media, _ in items.values() if media == "application/x-dtbncx+xml"),
                None,
            )
            self.toc = self._toc(archive, nav, ncx)
        self.sections = len(self.paths)

    def _toc(
        self, archive: ZipFile, nav: str | None, ncx: str | None
    ) -> list[dict[str, str | int]]:
        for path, kind in ((nav, "nav"), (ncx, "ncx")):
            if path is None:
                continue
            document = _xml(read_entry(archive, path, _XHTML_LIMIT))
            entries: list[dict[str, str | int]] = []
            if kind == "nav":
                nodes = [
                    e
                    for e in document.iter()
                    if _local(e.tag) == "nav"
                    and any(_local(k) == "type" and "toc" in v.split() for k, v in e.attrib.items())
                ]
                links = (
                    (e.attrib.get("href", ""), "".join(e.itertext()).strip())
                    for node in nodes
                    for e in node.iter()
                    if _local(e.tag) == "a"
                )
            else:
                links = (
                    (
                        next(
                            (e.attrib.get("src", "") for e in node if _local(e.tag) == "content"),
                            "",
                        ),
                        next(
                            (
                                "".join(e.itertext()).strip()
                                for e in node
                                if _local(e.tag) == "navLabel"
                            ),
                            "",
                        ),
                    )
                    for node in document.iter()
                    if _local(node.tag) == "navPoint"
                )
            for href, title in links:
                try:
                    target = _resolve(path, href)
                    index = self.paths.index(target)
                except ValueError:
                    continue
                entries.append({"title": title, "section": index})
            if entries:
                return entries
        return [
            {"title": posixpath.basename(path), "section": index}
            for index, path in enumerate(self.paths)
        ]

    def section(self, index: int, book_id: str) -> dict[str, str | int]:
        if index < 0 or index >= len(self.paths):
            raise IndexError(index)
        path = self.paths[index]
        with self._archive() as archive:
            document = read_entry(archive, path, _XHTML_LIMIT).decode("utf-8-sig")

        def image_url(src: str) -> str | None:
            try:
                target = _resolve(path, src)
            except ValueError:
                return None
            if target not in self.images:
                return None
            relative = posixpath.relpath(target, self.root or ".")
            try:
                safe_path(relative)
            except ValueError:
                return None
            return f"/api/books/{quote(book_id, safe='')}/read/res/{quote(relative, safe='/')}?format=epub"

        title = next(
            (str(item["title"]) for item in self.toc if item["section"] == index),
            posixpath.basename(path),
        )
        return {"index": index, "title": title, "html": sanitize(document, image_url)}

    def resource(self, relative_path: str) -> tuple[bytes, str]:
        relative = safe_path(relative_path)
        path = safe_path(posixpath.join(self.root, relative))
        if path not in self.images:
            raise ValueError("资源不存在")
        content_type = mimetypes.guess_type(path)[0]
        if content_type is None or not content_type.startswith("image/"):
            raise ValueError("资源不是图片")
        with self._archive() as archive:
            return read_entry(archive, path), content_type
