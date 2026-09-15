from __future__ import annotations

import asyncio
import io
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZipFile

import httpx
import pytest
from PIL import Image
from pypdf import PdfReader

from quire.cli import main
from quire.core_manga import run_core_manga
from quire.errors import ConfigError, FetchError
from quire.export_options import available_output, output_paths, parse_formats
from quire.fetch.session import AsyncFetcher
from quire.image.options import CompressionOptions
from quire.models import MangaOptions, MangaResult
from tests.test_async_fetch import answer


def image(size=(400, 600), color="red"):
    with Image.new("RGB", size, color) as source, io.BytesIO() as data:
        source.save(data, "PNG")
        return data.getvalue()


def capture(
    root,
    *,
    formats=("pdf", "cbz", "zip"),
    missing=False,
    requests=None,
    title="卷帙 &amp; 海",
    **kwargs,
):
    def handle(request):
        if requests is not None:
            requests.append(request.url.path)
        if request.url.path == "/robots.txt":
            return answer(request, 404)
        if request.url.path == "/chapter":
            return answer(
                request,
                data=f'<h1>{title}</h1><img src="/one?token=secret"><img src="/two"><img src="/three">'.encode(),
            )
        if request.url.path == "/two" and missing:
            return answer(request, 404)
        return answer(
            request, data=image((400, 3600) if request.url.path == "/three" else (400, 600))
        )

    return asyncio.run(
        run_core_manga(
            "https://example.test/chapter?token=private",
            root / f"book.{formats[0]}",
            formats=formats,
            fetcher=AsyncFetcher(transport=httpx.MockTransport(handle), rate=1e9, retries=0),
            **kwargs,
        )
    )


@pytest.mark.parametrize(
    "formats", [("pdf",), ("cbz",), ("zip",), ("pdf", "cbz", "zip"), ("zip", "cbz")]
)
def test_formats_metadata_pages_and_cleanup(tmp_path, formats):
    result = capture(tmp_path, formats=formats)
    assert result.pages_written == 7 and result.source_resources == 3 and not result.partial
    assert result.total_bytes == sum(a.path.stat().st_size for a in result.artifacts)
    assert result.bytes_out == result.artifacts[0].bytes
    report = json.loads(result.report.read_text())
    assert [a["format"] for a in report["artifacts"]] == list(formats)
    assert all(a["target_met"] for a in report["artifacts"])
    assert report["total_bytes"] == result.total_bytes
    assert not list((tmp_path / ".quire-core/cache").rglob("*.png"))
    for artifact in result.artifacts:
        if artifact.format == "pdf":
            pdf = PdfReader(artifact.path)
            assert len(pdf.pages) == 7 and pdf.metadata.title == "卷帙 & 海"
            assert pdf.outline and "private" not in str(pdf.metadata)
        else:
            with ZipFile(artifact.path) as archive:
                assert archive.testzip() is None
                pages = [name for name in archive.namelist() if name.endswith((".jpg", ".png"))]
                assert len(pages) == 7 and pages == sorted(pages)
                assert all(name.startswith(f"{i:06d}.") for i, name in enumerate(pages, 1))
                if artifact.format == "cbz":
                    meta = archive.read("ComicInfo.xml")
                    assert ET.fromstring(meta).findtext("PageCount") == "7"
                else:
                    meta = archive.read("manifest.json")
                    assert len(json.loads(meta)["pages"]) == 7
                assert b"private" not in meta and b"secret" not in meta


def test_missing_page_same_position_all_formats(tmp_path):
    result = capture(tmp_path, missing=True)
    assert result.partial and result.pages_failed == 1 and result.pages_written == 6
    assert "MISSING PAGE" in PdfReader(tmp_path / "book.pdf").pages[1].extract_text()
    with ZipFile(tmp_path / "book.zip") as archive:
        entries = json.loads(archive.read("manifest.json"))["pages"]
        assert entries[1]["missing_reason"] == "network"
        assert entries[1]["source_index"] == 2
        assert entries[2]["source_index"] == 3 and entries[-1]["part"] == 5
        assert entries[1]["file"].endswith(".png")
    with ZipFile(tmp_path / "book.cbz") as archive:
        assert "000002.png" in archive.namelist()
    assert len(list((tmp_path / ".quire-core/cache").rglob("*.png"))) == 2


def test_one_encoding_per_source_per_round(tmp_path, monkeypatch):
    from quire.core_pages import encode_pages

    calls = []

    def counted(*args):
        calls.append(1)
        return encode_pages(*args)

    monkeypatch.setattr("quire.core_pages.encode_pages", counted)
    result = capture(tmp_path)
    assert result.encoding_rounds == 1 and len(calls) == 3


def test_switch_format_reuses_sources(tmp_path):
    first = capture(tmp_path, formats=("pdf",), options=MangaOptions(keep_images=True))
    requests = []
    result = capture(
        tmp_path,
        formats=("cbz", "zip"),
        resume=True,
        requests=requests,
        options=MangaOptions(overwrite=True),
    )
    assert result.task_id == first.task_id and result.resources_reused == 3
    assert requests == ["/robots.txt", "/chapter"]


def test_target_applies_to_every_file(tmp_path):
    result = capture(tmp_path, compression=CompressionOptions(target_bytes=1))
    assert result.target_met is False and result.encoding_rounds == 3
    assert all(a.target_met is False for a in result.artifacts)
    assert len(list((tmp_path / ".quire-core/cache").rglob("*.png"))) == 3


def test_secondary_output_preflight_before_network(tmp_path):
    requests = []
    (tmp_path / "book.zip").write_bytes(b"old")
    with pytest.raises(ConfigError, match="目标已存在"):
        capture(tmp_path, requests=requests)
    assert not requests and (tmp_path / "book.zip").read_bytes() == b"old"


@pytest.mark.parametrize("kind", ["directory", "symlink"])
def test_reject_nonregular_destination(tmp_path, kind):
    path = tmp_path / "book.cbz"
    path.mkdir() if kind == "directory" else path.symlink_to("absent")
    with pytest.raises(ConfigError, match="普通文件"):
        capture(tmp_path, options=MangaOptions(overwrite=True))


def test_writer_failure_preserves_all_old_outputs(tmp_path, monkeypatch):
    for fmt in ("pdf", "cbz", "zip"):
        (tmp_path / f"book.{fmt}").write_bytes(b"old")

    def fail(*_):
        raise OSError("injected")

    monkeypatch.setattr("quire.core_export.ArchiveWriter.add_page", fail)
    with pytest.raises(OSError, match="injected"):
        capture(tmp_path, options=MangaOptions(overwrite=True))
    assert all((tmp_path / f"book.{fmt}").read_bytes() == b"old" for fmt in ("pdf", "cbz", "zip"))
    assert len(list((tmp_path / ".quire-core/cache").rglob("*.png"))) == 3
    assert not list(tmp_path.glob(".quire-encode-*"))


def test_partial_publish_failure_reports_committed_and_keeps_cache(tmp_path, monkeypatch):
    import os

    original = os.link

    def fault(source, destination, **kwargs):
        if destination.suffix == ".cbz":
            raise OSError("injected")
        return original(source, destination, **kwargs)

    monkeypatch.setattr("quire.export_commit.os.link", fault)
    with pytest.raises(FetchError, match="已发布：book.pdf"):
        capture(tmp_path)
    assert (tmp_path / "book.pdf").exists()
    assert not (tmp_path / "book.zip").exists() and not (tmp_path / "book.cbz").exists()
    assert len(list((tmp_path / ".quire-core/cache").rglob("*.png"))) == 3


def test_multi_cancel_during_copy_preserves_all_old_files(tmp_path, monkeypatch):
    from quire.export_commit import _stage

    async def cancel(receipt, item):
        await _stage(receipt, item)
        if item.kind == "zip":
            asyncio.current_task().cancel()

    monkeypatch.setattr("quire.export_commit._stage", cancel)
    for fmt in ("pdf", "cbz", "zip"):
        (tmp_path / f"book.{fmt}").write_bytes(b"old")
    with pytest.raises(asyncio.CancelledError):
        capture(tmp_path, options=MangaOptions(overwrite=True))
    assert all((tmp_path / f"book.{fmt}").read_bytes() == b"old" for fmt in ("pdf", "cbz", "zip"))
    assert not (tmp_path / "book.report.json").exists()


@pytest.mark.parametrize("value", ["", "pdf,pdf", "pdf,,cbz", "epub", ",", "txt"])
def test_invalid_formats(value):
    with pytest.raises(ConfigError):
        parse_formats(value)


def test_format_paths_and_group_numbering(tmp_path):
    assert parse_formats(None) == ("pdf",)
    assert parse_formats(" ZIP, cbz ") == ("zip", "cbz")
    assert output_paths(tmp_path / "book", ("zip",)) == (tmp_path / "book.zip",)
    with pytest.raises(ConfigError, match="后缀"):
        output_paths(tmp_path / "book.pdf", ("zip",))
    (tmp_path / "book.zip").touch()
    (tmp_path / "book (1).report.json").touch()
    assert (
        available_output(tmp_path / "book.pdf", ("pdf", "zip"), overwrite=False).name
        == "book (2).pdf"
    )
    assert (
        available_output(tmp_path / "book.pdf", ("pdf", "zip"), overwrite=True).name == "book.pdf"
    )
    assert MangaResult(Path("book.pdf"), bytes_out=4).total_bytes == 4


def test_cli_formats_validation_and_selection(tmp_path, monkeypatch, capsys):
    assert main(["manga", "https://example.test", "--format", "cbz"]) == 1
    assert main(["manga", "https://example.test", "--core", "--format", "bad"]) == 1
    seen = []

    async def run(url, out, **kwargs):
        seen.append((out, kwargs["formats"]))
        return MangaResult(out)

    monkeypatch.setattr("quire.core_manga.run_core_manga", run)
    output = tmp_path / "book.zip"
    output.touch()
    assert (
        main(
            [
                "manga",
                "https://example.test",
                "--core",
                "--format",
                "zip,cbz",
                "-o",
                str(output),
                "-q",
            ]
        )
        == 0
    )
    assert seen == [(tmp_path / "book (1).zip", ("zip", "cbz"))]


@pytest.mark.parametrize("missing", [False, True])
def test_selected_range_preserves_source_index(tmp_path, missing):
    result = capture(tmp_path, missing=missing, options=MangaOptions(first=2, last=2))
    with ZipFile(tmp_path / "book.zip") as archive:
        page = json.loads(archive.read("manifest.json"))["pages"][0]
        assert page["source_index"] == 2 and page["file"].startswith("000001.")
    with ZipFile(tmp_path / "book.cbz") as archive:
        bookmark = ET.fromstring(archive.read("ComicInfo.xml")).find("Pages/Page").get("Bookmark")
        assert "Source 2" in bookmark
    pdf = PdfReader(result.output)
    assert "Page 2" in pdf.outline[1][0].title
    assert result.pages_failed == int(missing)


def test_control_characters_cleaned_in_shared_metadata(tmp_path):
    result = capture(tmp_path, title="Book\u0080\ufffeTitle")
    assert result.title == "BookTitle"
    assert PdfReader(result.output).metadata.title == "BookTitle"
    with ZipFile(tmp_path / "book.zip") as archive:
        assert json.loads(archive.read("manifest.json"))["title"] == "BookTitle"
    with ZipFile(tmp_path / "book.cbz") as archive:
        assert ET.fromstring(archive.read("ComicInfo.xml")).findtext("Title") == "BookTitle"
    assert json.loads(result.report.read_text())["title"] == "BookTitle"


@pytest.mark.parametrize("overwrite", [False, True])
def test_cleanup_error_after_commit_reports_published(tmp_path, monkeypatch, overwrite):
    original_unlink = Path.unlink

    def fail(path, *args, **kwargs):
        if path.name == "publish-pdf.tmp":
            raise OSError("cleanup EIO")
        return original_unlink(path, *args, **kwargs)

    if overwrite:
        (tmp_path / "book.pdf").write_bytes(b"old")
    monkeypatch.setattr(Path, "unlink", fail)
    if overwrite:
        # replace consumes the staging name, so no unlink follows this publication.
        result = capture(tmp_path, options=MangaOptions(overwrite=True))
        assert result.report is not None
        return
    with pytest.raises(FetchError, match="已发布：book.pdf"):
        capture(tmp_path)
    assert (tmp_path / "book.pdf").read_bytes().startswith(b"%PDF")
    assert not (tmp_path / "book.zip").exists()
    assert len(list((tmp_path / ".quire-core/cache").rglob("*.png"))) == 3
