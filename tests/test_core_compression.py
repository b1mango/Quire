from __future__ import annotations

import asyncio
import io
import json

import httpx
import pytest
from PIL import Image
from pypdf import PdfReader

from quire.cli import main
from quire.core_manga import run_core_manga
from quire.errors import ConfigError, FetchError
from quire.fetch.session import AsyncFetcher
from quire.image.options import CompressionOptions, parse_size
from quire.models import MangaOptions
from tests.test_async_fetch import answer


def image_bytes(mode="RGB", size=(500, 800), color="red", fmt="PNG"):
    data = io.BytesIO()
    Image.new(mode, size, color).save(data, fmt)
    return data.getvalue()


def capture(
    tmp_path, data, *, compression=None, options=None, resume=False, name="book", progress=None
):
    def handler(request):
        if request.url.path == "/robots.txt":
            return answer(request, 404)
        if request.url.path == "/chapter":
            return answer(request, data=b'<h1>Compression</h1><img src="/image">')
        return answer(request, data=data)

    return asyncio.run(
        run_core_manga(
            "https://example.test/chapter",
            tmp_path / f"{name}.pdf",
            options=options,
            compression=compression,
            resume=resume,
            progress=progress,
            fetcher=AsyncFetcher(transport=httpx.MockTransport(handler), rate=1e9),
        )
    )


@pytest.mark.parametrize(
    "text,expected",
    [
        ("50MB", 50_000_000),
        ("1MiB", 1048576),
        ("1.5KB", 1500),
        ("100", 100),
        ("2gb", 2_000_000_000),
        ("1KiB", 1024),
        ("1GiB", 1073741824),
    ],
)
def test_parse_target_units(text, expected):
    assert parse_size(text) == expected


@pytest.mark.parametrize("text", ["0", "-1MB", "nan", "1.1B", "1TB", "9999999999999B", "5junk"])
def test_bad_target(text):
    with pytest.raises(ConfigError):
        parse_size(text)


@pytest.mark.parametrize(
    "kwargs",
    [{"preset": "wrong"}, {"target_bytes": 0}, {"target_bytes": True}, {"preset": "lossless"}],
)
def test_bad_settings(kwargs):
    with pytest.raises(ConfigError):
        CompressionOptions(**kwargs)


def test_default_compression_report_and_cleanup(tmp_path):
    result = capture(tmp_path, image_bytes(size=(2200, 3000)))
    assert result.target_met is True and result.target_bytes == 50_000_000
    assert result.compression == "balanced" and result.encoding_rounds == 1
    obj = PdfReader(result.output).pages[0]["/Resources"]["/XObject"]["/Im0"]
    assert max(obj["/Width"], obj["/Height"]) == 2000
    report = json.loads(result.report.read_text())
    assert report["source_resources"] == 1 and report["compression"]["target_met"] is True
    assert not list((tmp_path / ".quire-core" / "cache").rglob("*.png"))


def test_target_unreachable_keeps_cache_and_reencode_without_redownload(tmp_path):
    result = capture(tmp_path, image_bytes(), compression=CompressionOptions(target_bytes=1))
    assert result.target_met is False and result.encoding_rounds == 3
    assert result.warnings and not result.partial
    assert list((tmp_path / ".quire-core" / "cache").rglob("*.png"))
    resumed = capture(
        tmp_path,
        b"must not download",
        resume=True,
        name="again",
        compression=CompressionOptions("lossless", None),
    )
    assert resumed.resources_reused == 1 and resumed.target_met is None
    assert resumed.quality is None and resumed.encoding_rounds == 1
    assert not list((tmp_path / ".quire-core" / "cache").rglob("*.png"))


def test_reduced_encoding_reaches_a_measured_target(tmp_path):
    import random

    image = Image.frombytes("RGB", (600, 900), random.Random(12).randbytes(600 * 900 * 3))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    data = buffer.getvalue()
    initial = capture(tmp_path, data, options=MangaOptions(keep_images=True))
    minimum = capture(
        tmp_path,
        b"not downloaded",
        resume=True,
        name="minimum",
        options=MangaOptions(keep_images=True),
        compression=CompressionOptions("tiny", None),
    )
    target = (initial.bytes_out + minimum.bytes_out) // 2
    assert minimum.bytes_out < target < initial.bytes_out
    result = capture(
        tmp_path,
        b"not downloaded",
        resume=True,
        name="target",
        compression=CompressionOptions(target_bytes=target),
    )
    assert result.target_met and 2 <= result.encoding_rounds <= 3
    assert result.bytes_out <= target and result.resources_reused == 1


def test_lossless_jpeg_payload_is_unchanged(tmp_path):
    data = image_bytes(fmt="JPEG")
    result = capture(tmp_path, data, compression=CompressionOptions("lossless", None))
    obj = PdfReader(result.output).pages[0]["/Resources"]["/XObject"]["/Im0"]
    assert obj.get_data() == data


def test_tall_strip_page_count_and_disable(tmp_path):
    data = image_bytes(size=(250, 2250))
    result = capture(tmp_path, data, options=MangaOptions(keep_images=True))
    pages = PdfReader(result.output).pages
    assert result.source_resources == 1 and len(pages) == 5
    assert [p["/Resources"]["/XObject"]["/Im0"]["/Height"] for p in pages] == [500] * 4 + [250]
    result = capture(
        tmp_path,
        data,
        resume=True,
        name="whole",
        compression=CompressionOptions("lossless", None, split_tall=False),
    )
    assert len(PdfReader(result.output).pages) == 1


def test_transparent_webp_and_first_gif_frame(tmp_path):
    result = capture(tmp_path, image_bytes("RGBA", color=(0, 0, 0, 0), fmt="WEBP"))
    assert result.pages_failed == 0
    buffer = io.BytesIO()
    first = Image.new("RGB", (400, 600), "red")
    first.save(
        buffer,
        "GIF",
        save_all=True,
        append_images=[Image.new("RGB", first.size, "blue")],
        duration=100,
        loop=0,
    )
    result = capture(tmp_path, buffer.getvalue(), name="gif", resume=True)
    assert result.pages_written == 1 and any("多帧" in w for w in result.warnings)


def test_postfilter_keeps_placeholder_and_cache(tmp_path):
    result = capture(tmp_path, image_bytes(size=(100, 100)))
    assert result.pages_failed == 1 and result.pages_rejected == 1
    assert "MISSING PAGE" in PdfReader(result.output).pages[0].extract_text()


def test_cancelled_encoding_preserves_old_output_and_cached_sources(tmp_path):
    class Cancel:
        def update(self, *_):
            raise asyncio.CancelledError

    output = tmp_path / "book.pdf"
    output.write_bytes(b"old")
    with pytest.raises(asyncio.CancelledError):
        capture(tmp_path, image_bytes(), options=MangaOptions(overwrite=True), progress=Cancel())
    assert output.read_bytes() == b"old"
    assert not list(tmp_path.glob(".quire-encode-*"))
    assert list((tmp_path / ".quire-core" / "cache").rglob("*.png"))


def test_cancel_during_final_copy_never_commits_or_cleans_cache(tmp_path, monkeypatch):
    from quire.export_commit import _stage

    async def cancel(receipt, item):
        await _stage(receipt, item)
        asyncio.current_task().cancel()

    monkeypatch.setattr("quire.export_commit._stage", cancel)
    output = tmp_path / "book.pdf"
    output.write_bytes(b"original")
    with pytest.raises(asyncio.CancelledError):
        capture(tmp_path, image_bytes(), options=MangaOptions(overwrite=True))
    assert output.read_bytes() == b"original"
    assert list((tmp_path / ".quire-core" / "cache").rglob("*.png"))
    assert not (tmp_path / "book.report.json").exists()
    assert not list(tmp_path.glob(".quire-encode-*"))
    assert not list(tmp_path.glob(".book.pdf.*"))


def test_reject_encoded_failure_without_publishing(tmp_path, monkeypatch):
    from quire.image.compress import EncodedPage

    monkeypatch.setattr(
        "quire.core_pages.encode_pages", lambda *_: iter([EncodedPage(b"bad", 1, 1, "color")])
    )
    with pytest.raises(FetchError):
        capture(tmp_path, image_bytes())
    assert not (tmp_path / "book.pdf").exists()


def test_cli_flags_are_validated_before_network(tmp_path, capsys):
    base = ["manga", "https://example.test/chapter", "-o", str(tmp_path / "book.pdf")]
    for flags in (
        ["--compress", "balanced"],
        ["--no-split-tall"],
        ["--core", "--target-size", "junk"],
        ["--core", "--compress", "lossless", "--target-size", "5MB"],
    ):
        assert main([*base, *flags]) == 1
    assert "Traceback" not in capsys.readouterr().err
