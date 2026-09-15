from __future__ import annotations

import hashlib
import json
import struct
import tracemalloc
import zlib
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

import pytest

from quire.assemble import archive as archive_module
from quire.assemble.archive import ArchiveWriter
from quire.assemble.models import ExportPage
from tests.conftest import make_image_bytes

TITLE = '\u5377\u5e19 <Title>&"</Title><Web>injected</Web> ../../outside'
URL = "https://private-user:private-pass@example.test/book?token=private-query#private-fragment"
SOURCE = "https://example.test/book?..."
REASON = '\u7f3a\u9875 <timeout>&"'


@pytest.fixture
def page() -> ExportPage:
    return ExportPage(make_image_bytes("JPEG", size=(32, 48)), 32, 48, 1, URL)


@pytest.fixture(params=["cbz", "zip"])
def fmt(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def writer(path: Path, fmt: str = "zip", **values: str) -> ArchiveWriter:
    return ArchiveWriter(
        path, format=fmt, title=values.get("title", TITLE), source_url=values.get("source_url", URL)
    )


def test_pages_roundtrip_and_metadata(tmp_path: Path, page: ExportPage, fmt: str) -> None:
    png = make_image_bytes("PNG", size=(20, 30))
    pages = [
        page,
        replace(page, part=2),
        ExportPage(png, 20, 30, 2, URL, missing_reason=REASON),
        replace(page, source_index=3),
    ]
    path = tmp_path / "caller-temporary"
    with writer(path, fmt) as output:
        for item in pages:
            assert output.add_page(item) is None
    metadata_name = "ComicInfo.xml" if fmt == "cbz" else "manifest.json"
    names = ["000001.jpg", "000002.jpg", "000003.png", "000004.jpg"]
    with ZipFile(path) as result:
        assert result.namelist() == [*names, metadata_name]
        assert result.testzip() is None
        for name, item in zip(names, pages, strict=True):
            info = result.getinfo(name)
            assert result.read(name) == item.data
            assert info.compress_type == ZIP_STORED
            assert info.CRC == zlib.crc32(item.data)
        assert result.getinfo(metadata_name).compress_type == ZIP_DEFLATED
        metadata = result.read(metadata_name)
    for secret in (b"private-user", b"private-pass", b"private-query", b"private-fragment"):
        assert secret not in metadata
    assert list(tmp_path.iterdir()) == [path]
    if fmt == "cbz":
        root = ET.fromstring(metadata)
        assert root.tag == "ComicInfo"
        assert [node.tag for node in root] == ["Title", "PageCount", "Web", "Pages"]
        assert root.findtext("Title") == TITLE
        assert root.findtext("Web") == SOURCE
        assert root.findtext("PageCount") == "4"
        entries = root.findall("Pages/Page")
        assert len(entries) == 4
        for index, (entry, item) in enumerate(zip(entries, pages, strict=True)):
            expected = f"Source {item.source_index} / part {item.part}"
            if item.missing_reason is not None:
                expected += f" / MISSING: {REASON}"
            assert entry.attrib == {
                "Image": str(index),
                "ImageWidth": str(item.width),
                "ImageHeight": str(item.height),
                "Bookmark": expected,
            }
    else:
        manifest = json.loads(metadata)
        assert list(manifest) == ["schema", "title", "source", "pages"]
        assert {key: manifest[key] for key in ("schema", "title", "source")} == {
            "schema": 1,
            "title": TITLE,
            "source": SOURCE,
        }
        assert manifest["pages"] == [
            {
                "file": name,
                "width": item.width,
                "height": item.height,
                "source_index": item.source_index,
                "part": item.part,
                "source_url": SOURCE,
                "missing_reason": item.missing_reason,
                "sha256": hashlib.sha256(item.data).hexdigest(),
            }
            for name, item in zip(names, pages, strict=True)
        ]
        assert list(manifest["pages"][0]) == [
            "file",
            "width",
            "height",
            "source_index",
            "part",
            "source_url",
            "missing_reason",
            "sha256",
        ]


def test_empty_and_repeatable_archives(tmp_path: Path, page: ExportPage, fmt: str) -> None:
    paths = [tmp_path / name for name in ("first", "second")]
    for path in paths:
        with writer(path, fmt):
            pass
        with ZipFile(path) as result:
            assert result.testzip() is None
            if fmt == "zip":
                assert json.loads(result.read("manifest.json"))["pages"] == []
            else:
                root = ET.fromstring(result.read("ComicInfo.xml"))
                assert root.findtext("PageCount") == "0"
                assert root.findall("Pages/Page") == []
    assert paths[0].read_bytes() == paths[1].read_bytes()
    for path in paths:
        with writer(path, fmt) as output:
            output.add_page(page)
    assert paths[0].read_bytes() == paths[1].read_bytes()


@pytest.mark.parametrize("invalid", ["", "pdf", "CBZ", "zip ", "../cbz"])
def test_invalid_format_does_not_touch_path(tmp_path: Path, invalid: str) -> None:
    path = tmp_path / "existing"
    path.write_bytes(b"original")
    with pytest.raises(ValueError, match="format"):
        writer(path, invalid)
    assert path.read_bytes() == b"original"


@pytest.mark.parametrize(
    "char",
    [
        "\x00",
        "\x01",
        "\x0b",
        "\x1f",
        "\x7f",
        "\x85",
        "\x9f",
        "\ud800",
        "\udfff",
        "\ufffe",
        "\uffff",
    ],
)
@pytest.mark.parametrize("field", ["title", "source_url", "page_url", "missing_reason"])
def test_invalid_metadata_is_rejected(
    tmp_path: Path, page: ExportPage, fmt: str, char: str, field: str
) -> None:
    path = tmp_path / "temporary"
    if field in {"title", "source_url"}:
        with pytest.raises(ValueError, match="invalid text"):
            writer(path, fmt, **{field: URL + char})
        assert not path.exists()
        return
    with writer(path, fmt) as output:
        changes = {"source_url" if field == "page_url" else field: URL + char}
        with pytest.raises(ValueError, match="invalid text"):
            output.add_page(replace(page, **changes))
        output.add_page(page)
    with ZipFile(path) as result:
        assert result.namelist()[0] == "000001.jpg"
        assert len(result.namelist()) == 2


def test_valid_unicode_whitespace_and_invalid_url(
    tmp_path: Path, page: ExportPage, fmt: str
) -> None:
    title = "\u4e2d\u6587\tline\nnext\r\U00020000"
    path = tmp_path / "temporary"
    with writer(path, fmt, title=title, source_url="https://[invalid") as output:
        output.add_page(replace(page, source_url="https://[invalid", missing_reason=""))
    with ZipFile(path) as result:
        if fmt == "zip":
            manifest = json.loads(result.read("manifest.json"))
            assert manifest["title"] == title
            assert manifest["source"] == manifest["pages"][0]["source_url"] == "[invalid URL]"
            assert manifest["pages"][0]["missing_reason"] == ""
        else:
            root = ET.fromstring(result.read("ComicInfo.xml"))
            assert root.findtext("Title") == title.replace("\r", "\n")
            assert root.findtext("Web") == "[invalid URL]"
            assert root.findall("Pages/Page")[0].get("Bookmark", "").endswith("MISSING: ")


@pytest.mark.parametrize(
    "data", [b"", b"<html>error</html>", b"\xff\xd8", b"\x89PNG", b"GIF89a", b"RIFF0000WEBP"]
)
def test_unknown_bytes_are_rejected_before_writing(
    tmp_path: Path, page: ExportPage, data: bytes
) -> None:
    path = tmp_path / "temporary"
    with writer(path) as output:
        with pytest.raises(ValueError, match="JPEG or PNG"):
            output.add_page(replace(page, data=data, source_url="https://example.test/pretend.jpg"))
        output.add_page(page)
    with ZipFile(path) as result:
        assert result.namelist() == ["000001.jpg", "manifest.json"]


@pytest.mark.parametrize("field", ["width", "height", "source_index", "part"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_invalid_page_numbers(tmp_path: Path, page: ExportPage, field: str, value: object) -> None:
    with writer(tmp_path / "temporary") as output:
        with pytest.raises(ValueError, match="positive integers"):
            output.add_page(replace(page, **{field: value}))


@pytest.mark.parametrize(
    "sequence",
    [
        [(1, 2, None)],
        [(2, 1, None), (1, 1, None)],
        [(1, 1, None), (1, 1, None)],
        [(1, 1, None), (1, 3, None)],
        [(1, 1, None), (2, 2, None)],
        [(1, 1, ""), (1, 2, None)],
        [(1, 1, None), (1, 2, "missing")],
    ],
)
def test_invalid_source_and_part_order(
    tmp_path: Path, page: ExportPage, sequence: list[tuple[int, int, str | None]]
) -> None:
    with writer(tmp_path / "temporary") as output:
        for source_index, part, reason in sequence[:-1]:
            output.add_page(
                replace(page, source_index=source_index, part=part, missing_reason=reason)
            )
        source_index, part, reason = sequence[-1]
        with pytest.raises(ValueError, match="order|parts|placeholder"):
            output.add_page(
                replace(page, source_index=source_index, part=part, missing_reason=reason)
            )


def test_selected_sources_preserve_indices(tmp_path: Path, page: ExportPage) -> None:
    path = tmp_path / "temporary"
    with writer(path) as output:
        output.add_page(replace(page, source_index=7))
        output.add_page(replace(page, source_index=10))
    with ZipFile(path) as result:
        assert [
            item["source_index"] for item in json.loads(result.read("manifest.json"))["pages"]
        ] == [7, 10]


def test_context_lifecycle(tmp_path: Path, page: ExportPage) -> None:
    path = tmp_path / "temporary"
    output = writer(path)
    assert not path.exists()
    with pytest.raises(RuntimeError, match="not open"):
        output.add_page(page)
    with output:
        with pytest.raises(RuntimeError, match="reused"):
            output.__enter__()
        output.add_page(page)
    with pytest.raises(RuntimeError, match="not open"):
        output.add_page(page)
    with pytest.raises(RuntimeError, match="reused"):
        output.__enter__()
    output.__exit__(None, None, None)


@pytest.mark.parametrize("exception", [RuntimeError, KeyboardInterrupt])
def test_body_exception_closes_without_final_metadata(
    tmp_path: Path, page: ExportPage, fmt: str, exception: type[BaseException]
) -> None:
    path = tmp_path / "temporary"
    with pytest.raises(exception, match="cancelled"):
        with writer(path, fmt) as output:
            handle = output._archive
            output.add_page(page)
            raise exception("cancelled")
    assert handle is not None and handle.fp is None
    with ZipFile(path) as result:
        assert result.namelist() == ["000001.jpg"]
        assert result.testzip() is None


@pytest.mark.parametrize("stage", ["image", "caught-image", "metadata"])
def test_write_exception_closes_handle(
    tmp_path: Path, page: ExportPage, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    original = ZipFile.writestr
    handles: list[BinaryIO] = []

    def fail(self: ZipFile, name: ZipInfo, data: bytes, *args: object, **kwargs: object) -> None:
        assert self.fp is not None
        handles.append(self.fp)
        original(self, name, data)
        if name.filename.endswith(".jpg") == (stage != "metadata"):
            raise OSError("disk failure")

    monkeypatch.setattr(ZipFile, "writestr", fail)
    output = writer(tmp_path / "temporary")
    error = RuntimeError if stage == "caught-image" else OSError
    with pytest.raises(error, match="failed write|disk failure"):
        with output:
            if stage == "caught-image":
                with pytest.raises(OSError, match="disk failure"):
                    output.add_page(page)
                with pytest.raises(RuntimeError, match="not open"):
                    output.add_page(page)
            else:
                output.add_page(page)
    assert handles and all(handle.closed for handle in handles)
    assert output._archive is None


def test_open_failure_does_not_create_parent(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        with writer(tmp_path / "missing" / "temporary"):
            pytest.fail("The context must not open")
    assert list(tmp_path.iterdir()) == []


def test_payload_corruption_is_detectable(tmp_path: Path, page: ExportPage) -> None:
    path = tmp_path / "temporary"
    with writer(path) as output:
        output.add_page(page)
    with ZipFile(path) as result:
        offset = result.getinfo("000001.jpg").header_offset
    with path.open("r+b") as file:
        file.seek(offset + 26)
        name_size, extra_size = struct.unpack("<HH", file.read(4))
        file.seek(offset + 30 + name_size + extra_size)
        file.write(b"!")
    with ZipFile(path) as result:
        assert result.testzip() == "000001.jpg"


def test_streaming_does_not_retain_book_payloads(tmp_path: Path, page: ExportPage) -> None:
    path = tmp_path / "temporary"
    tracemalloc.start()
    try:
        with writer(path) as output:
            for index in range(1, 201):
                output.add_page(replace(page, source_index=index, data=page.data + bytes(65536)))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 3_000_000
    assert path.stat().st_size > 13_000_000
    with ZipFile(path) as result:
        assert result.testzip() is None
        assert len(json.loads(result.read("manifest.json"))["pages"]) == 200


def test_page_name_limit(tmp_path: Path, page: ExportPage, monkeypatch: pytest.MonkeyPatch) -> None:
    class FullPages(list[archive_module._Page]):
        def __len__(self) -> int:
            return 999999

    with writer(tmp_path / "temporary") as output:
        with monkeypatch.context() as patch:
            patch.setattr(output, "_pages", FullPages())
            with pytest.raises(ValueError, match="six-digit"):
                output.add_page(page)
