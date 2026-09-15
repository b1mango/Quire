from __future__ import annotations

import struct

import pytest

from quire.image.probe import probe_bytes, probe_file, sniff_format
from quire.manga import run_local
from quire.utils.naming import image_filename, natural_key, safe_filename, unique_path, volume_label
from quire.utils.urls import (
    guess_ext,
    host_of,
    is_usable_url,
    join_url,
    normalize_url,
    redact,
    same_host,
)
from tests.conftest import make_image_bytes


@pytest.mark.parametrize(
    "fmt,mode",
    [
        ("JPEG", "RGB"),
        ("PNG", "RGB"),
        ("PNG", "RGBA"),
        ("GIF", "P"),
        ("WEBP", "RGB"),
        ("BMP", "RGB"),
    ],
)
def test_probe_file_and_truncation(fmt, mode, tmp_path):
    data = make_image_bytes(fmt, mode, (256, 340))
    result = probe_bytes(data)
    assert result.ok and (result.width, result.height) == (256, 340)
    assert result.megapixels == 256 * 340 / 1_000_000
    path = tmp_path / "image"
    path.write_bytes(data)
    assert probe_file(path).ok
    assert not probe_bytes(data[:-20]).ok
    path.write_bytes(data[:-20])
    assert not probe_file(path).ok


def test_probe_empty_unknown_missing_and_bad_crc(tmp_path):
    path = tmp_path / "missing"
    assert not probe_file(path).ok
    path.write_bytes(b"tiny")
    assert not probe_file(path).ok
    path.write_bytes(b"not an image at all")
    assert not probe_file(path).ok
    assert not probe_file(tmp_path).ok
    assert not probe_bytes(b"<html>error</html>").ok
    data = bytearray(make_image_bytes("PNG"))
    data[-5] ^= 0xFF
    assert not probe_bytes(bytes(data)).ok
    data = bytearray(make_image_bytes("PNG"))
    data[33:37] = (len(data) + 100).to_bytes(4, "big")
    assert not probe_bytes(bytes(data)).ok
    assert not probe_bytes(b"\x89PNG\r\n\x1a\n").ok
    assert not probe_bytes(b"\xff\xd8\xff").ok
    assert not probe_bytes(b"GIF89a").ok
    assert not probe_bytes(b"BM").ok
    assert not probe_bytes(b"RIFF0000WEBP").ok


@pytest.mark.parametrize(
    "header,fmt",
    [
        (b"II*\x00", "tiff"),
        (b"MM\x00*", "tiff"),
        (b"\x00\x00\x00\x18ftypavif", "avif"),
        (b"\xff\x0a", "jxl"),
    ],
)
def test_unsupported_recognized_formats(header, fmt):
    assert sniff_format(header) == fmt
    result = probe_bytes(header)
    assert result.format == fmt and not result.ok


def test_webp_lossless_and_alpha():
    for kwargs in [{"lossless": True}, {"lossless": False}]:
        data = make_image_bytes("WEBP", "RGBA", (128, 256), **kwargs)
        result = probe_bytes(data)
        assert result.ok and result.has_alpha
        assert (result.width, result.height) == (128, 256)


def test_header_limits_and_special_bmp():
    data = bytearray(32)
    data[:2] = b"BM"
    struct.pack_into("<I", data, 2, 32)
    struct.pack_into("<IHH", data, 14, 12, 300, 400)
    assert probe_bytes(bytes(data)).ok
    struct.pack_into("<H", data, 18, 0)
    assert not probe_bytes(bytes(data)).ok
    png = bytearray(make_image_bytes("PNG"))
    png[12:16] = b"JUNK"
    assert not probe_bytes(bytes(png)).ok


def test_url_safety_and_signed_queries():
    assert (
        join_url("https://example.org/ch/1", "../img/a b.jpg?sig=A%2FB#p")
        == "https://example.org/img/a%20b.jpg?sig=A%2FB"
    )
    assert join_url("https://example.org/", "//cdn.example.org/a") == "https://cdn.example.org/a"
    assert join_url("https://example.org/", "data:image/png;base64,a") == ""
    assert join_url("https://example.org/", "") == ""
    assert normalize_url(" ") == ""
    for url in [None, "", "#local", "file:///a", "http://[broken", "javascript:x"]:
        assert not is_usable_url(url)
    assert host_of("http://[broken") == ""
    assert same_host("https://www.example.org/a", "http://example.org/b")
    assert not same_host("https://a.org", "https://b.org")
    assert not same_host("", "https://a.org")
    assert redact("https://u:p@example.org/a?token=secret#x") == "https://example.org/a?..."
    assert redact("http://[broken") == "[invalid URL]"
    assert guess_ext("https://x/a.jfif") == ".jpg"
    assert guess_ext("https://x/content", "image/webp") == ".webp"
    assert guess_ext("https://x/content", "text/html") == ".jpg"
    assert guess_ext("https://x/.hidden") == ".jpg"


def test_safe_names_and_existing_files(tmp_path):
    assert safe_filename("") == "untitled"
    assert safe_filename("CON") == "_CON"
    assert safe_filename("...") == "untitled"
    assert "/" not in safe_filename("1/2:3\n")
    assert len(safe_filename("卷" * 200).encode()) <= 220
    assert sorted(["10", "2", ""], key=natural_key) == ["", "2", "10"]
    assert volume_label(1, 100) == "第001卷"
    assert image_filename(2, "jpg", total=1000) == "0002.jpg"
    path = tmp_path / "file.pdf"
    assert unique_path(path) == path
    path.touch()
    assert unique_path(path).name == "file (1).pdf"


@pytest.mark.parametrize("progressive", [False, True])
def test_jpeg_structure_accepts_baseline_and_progressive(tmp_path, progressive):
    data = make_image_bytes("JPEG", size=(256, 340), progressive=progressive)
    path = tmp_path / "image.jpg"
    path.write_bytes(data + b"trailing metadata")
    assert probe_bytes(data).ok and probe_file(path).ok


@pytest.mark.parametrize("damage", ["no_sos", "short_sof", "short_sos", "no_scan", "bad_sos_id"])
def test_jpeg_structure_rejects_missing_or_invalid_scan(tmp_path, damage):
    data = make_image_bytes("JPEG", size=(256, 340))
    sof = data.index(b"\xff\xc0")
    sos = data.index(b"\xff\xda")
    sof_end = sof + 2 + int.from_bytes(data[sof + 2 : sof + 4], "big")
    sos_end = sos + 2 + int.from_bytes(data[sos + 2 : sos + 4], "big")
    if damage == "no_sos":
        data = data[:sof_end] + b"\xff\xd9"
    elif damage == "short_sof":
        data = data[: sof + 12] + b"\xff\xd9"
    elif damage == "short_sos":
        data = data[: sos + 6] + b"\xff\xd9"
    elif damage == "no_scan":
        data = data[:sos_end] + b"\xff\xd9"
    elif damage == "bad_sos_id":
        data = data[: sos + 5] + b"\xff" + data[sos + 6 :]
    images = tmp_path / "images"
    images.mkdir()
    path = images / "1.jpg"
    path.write_bytes(data)
    assert not probe_bytes(data).ok and not probe_file(path).ok
    result = run_local(images, tmp_path / "out.pdf")
    assert result.pages_written == 0 and result.pages_failed == 1


def test_jpeg_eoi_inside_metadata_does_not_replace_real_eoi():
    data = make_image_bytes("JPEG", size=(256, 340))
    app = b"\xff\xe1\x00\x06xx\xff\xd9"
    assert not probe_bytes(data[:2] + app + data[2:-2]).ok


def test_large_jpeg_eoi_at_tail_window_start(tmp_path):
    data = make_image_bytes("JPEG", size=(1024, 1024)) + b"x" * 4094
    assert len(data) > 64 * 1024 and data[-4096:-4094] == b"\xff\xd9"
    path = tmp_path / "large.jpg"
    path.write_bytes(data)
    assert probe_bytes(data).ok and probe_file(path).ok


@pytest.mark.parametrize("extra", [b"", b"extension metadata" * 3])
def test_jpeg_adobe_header_accepts_extension_metadata(tmp_path, extra):
    data = make_image_bytes("JPEG", size=(256, 340), progressive=True)
    payload = b"Adobe\x00\x64\x00\x00\x00\x00\x01" + extra
    marker = b"\xff\xee" + struct.pack(">H", len(payload) + 2) + payload
    data = data[:2] + marker + data[2:]
    path = tmp_path / "adobe.jpg"
    path.write_bytes(data)
    assert probe_bytes(data).ok and probe_file(path).ok
    assert probe_bytes(data).adobe_transform == 1


def test_jpeg_adobe_header_rejects_missing_transform():
    data = make_image_bytes("JPEG")
    payload = b"Adobe\x00\x64\x00\x00\x00\x00"
    marker = b"\xff\xee" + struct.pack(">H", len(payload) + 2) + payload
    assert not probe_bytes(data[:2] + marker + data[2:]).ok
