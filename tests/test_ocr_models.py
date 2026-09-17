"""OCR 模型按需下载与强制校验：SHA-256、原子安装、离线行为（项目设计.md §6.7）。

不依赖真实模型下载：用 monkeypatch 把 ``MODEL_FILES`` 换成内容已知的测试项，
``_http_download`` 走回环 HTTP 服务验证流式读取与大小上限。
"""

from __future__ import annotations

import hashlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from quire.errors import UnsupportedError
from quire.ocr import models
from quire.ocr.models import (
    MODEL_FILES,
    check_models,
    ensure_models,
    manual_hint,
    sha256_file,
    write_checksums,
)

PAYLOADS = {
    "a.onnx": b"fake-det-model",
    "b.onnx": b"fake-rec-model",
    "keys.txt": "字典\n".encode(),
}
TEST_FILES = tuple(
    (name, hashlib.sha256(data).hexdigest(), f"remote/{name}") for name, data in PAYLOADS.items()
)


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(models, "MODEL_FILES", TEST_FILES)


def downloader(url: str) -> bytes:
    name = url.rsplit("/", 1)[-1]
    return PAYLOADS[name]


def test_check_models_reports_missing_corrupt_ready(tmp_path: Path, patched: None) -> None:
    status = check_models(tmp_path)
    assert not status.ready and set(status.missing) == set(PAYLOADS) and not status.corrupt
    for name, data in PAYLOADS.items():
        (tmp_path / name).write_bytes(data)
    assert check_models(tmp_path).ready
    (tmp_path / "a.onnx").write_bytes(b"tampered")
    status = check_models(tmp_path)
    assert status.corrupt == ("a.onnx",)
    assert not status.missing and not status.ready


def test_offline_missing_models_errors_with_manual_hint(tmp_path: Path, patched: None) -> None:
    with pytest.raises(UnsupportedError) as caught:
        ensure_models(tmp_path, offline=True)
    assert caught.value.exit_code == 6
    assert caught.value.hint is not None
    assert str(tmp_path) in caught.value.hint
    assert "https://" in caught.value.hint  # 手动下载地址
    assert not list(tmp_path.iterdir())  # 什么都没留下


def test_download_installs_and_writes_checksums(tmp_path: Path, patched: None) -> None:
    status = ensure_models(tmp_path, downloader=downloader)
    assert status.ready
    for name, data in PAYLOADS.items():
        assert (tmp_path / name).read_bytes() == data
    checksums = (tmp_path / "CHECKSUMS").read_text("utf-8").splitlines()
    assert len(checksums) == len(PAYLOADS)
    for name, expected, _ in TEST_FILES:
        assert f"{expected}  {name}" in checksums
    assert not list(tmp_path.glob("*.part"))  # 不留临时文件


def test_redownloads_only_corrupt_files(tmp_path: Path, patched: None) -> None:
    ensure_models(tmp_path, downloader=downloader)
    (tmp_path / "b.onnx").write_bytes(b"tampered")
    fetched: list[str] = []

    def selective(url: str) -> bytes:
        fetched.append(url)
        return downloader(url)

    assert ensure_models(tmp_path, downloader=selective).ready
    assert len(fetched) == 1 and fetched[0].endswith("b.onnx")


def test_hash_mismatch_aborts_without_partial_files(tmp_path: Path, patched: None) -> None:
    def bad(url: str) -> bytes:
        return b"corrupted-download"

    with pytest.raises(UnsupportedError, match="SHA-256"):
        ensure_models(tmp_path, downloader=bad)
    assert not list(tmp_path.iterdir())  # 校验不过什么都不留


def test_all_mirrors_failing_reports_manual_hint(tmp_path: Path, patched: None) -> None:
    def down(url: str) -> bytes:
        raise OSError("network down")

    with pytest.raises(UnsupportedError) as caught:
        ensure_models(tmp_path, downloader=down)
    assert "下载失败" in caught.value.message
    assert caught.value.hint is not None and str(tmp_path) in caught.value.hint
    assert not list(tmp_path.iterdir())


def test_manual_hint_lists_urls_and_dir(tmp_path: Path) -> None:
    hint = manual_hint(tmp_path, [MODEL_FILES[0][0]])
    assert str(tmp_path) in hint
    assert MODEL_FILES[0][2] in hint  # 固定 URL 的相对路径部分


def test_sha256_file(tmp_path: Path) -> None:
    path = tmp_path / "x.bin"
    path.write_bytes(b"data")
    assert sha256_file(path) == hashlib.sha256(b"data").hexdigest()


def test_write_checksums_format(tmp_path: Path, patched: None) -> None:
    path = write_checksums(tmp_path)
    assert path.name == "CHECKSUMS"
    lines = path.read_text("utf-8").splitlines()
    assert all(line.count("  ") == 1 for line in lines)


class _Handler(BaseHTTPRequestHandler):
    payload = b"loopback-model-bytes"

    def do_GET(self) -> None:  # noqa: N802 - stdlib 命名
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, *args: object) -> None:
        pass


def test_http_download_streams_and_enforces_size_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/m.onnx"
        assert models._http_download(url) == _Handler.payload
        monkeypatch.setattr(models, "MAX_MODEL_BYTES", 4)
        with pytest.raises(UnsupportedError, match="大小上限"):
            models._http_download(url)
    finally:
        server.shutdown()
        server.server_close()
