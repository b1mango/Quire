"""设置页 OCR 模块下载（§6.7 延伸）：流式进度、引擎缺失拒绝、任务内不静默下载。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from quire.errors import ConfigError, UnsupportedError
from quire.ocr import capture, models
from quire.ocr.capture import OcrRunner
from quire.server import ocr_dl

PAYLOADS = {"a.onnx": b"fake-det", "b.onnx": b"fake-rec"}
TEST_FILES = tuple(
    (name, hashlib.sha256(data).hexdigest(), f"remote/{name}") for name, data in PAYLOADS.items()
)


def downloader(url: str) -> bytes:
    return PAYLOADS[url.rsplit("/", 1)[-1]]


def test_ensure_models_reports_each_installed_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(models, "MODEL_FILES", TEST_FILES)
    seen: list[str] = []
    status = models.ensure_models(tmp_path, downloader=downloader, on_file=seen.append)
    assert status.ready
    assert seen == list(PAYLOADS)


def _ctx(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(data_root=tmp_path)


def test_download_streams_progress_frames(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ocr_dl, "module_available", lambda name: True)
    monkeypatch.setattr(
        ocr_dl, "check_models", lambda d: SimpleNamespace(missing=("a", "b"), corrupt=())
    )

    def fake_ensure(model_dir, **kwargs):
        assert model_dir == tmp_path / "models"
        on_file = kwargs["on_file"]
        for name in ("a", "b"):
            on_file(name)
        return SimpleNamespace(ready=True)

    monkeypatch.setattr(ocr_dl, "ensure_models", fake_ensure)
    frames: list[dict] = []
    ocr_dl.download_ocr_models(_ctx(tmp_path), frames.append)
    assert frames == [
        {"total": 2},
        {"done": 1, "total": 2, "file": "a"},
        {"done": 2, "total": 2, "file": "b"},
        {"result": {"ready": True, "downloaded": ["a", "b"]}},
    ]


def test_download_ready_short_circuits_without_network(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ocr_dl, "module_available", lambda name: True)
    monkeypatch.setattr(ocr_dl, "check_models", lambda d: SimpleNamespace(missing=(), corrupt=()))
    frames: list[dict] = []
    ocr_dl.download_ocr_models(_ctx(tmp_path), frames.append)
    assert frames == [{"total": 0}, {"result": {"ready": True, "downloaded": []}}]


def test_download_failure_ends_with_error_frame(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ocr_dl, "module_available", lambda name: True)
    monkeypatch.setattr(
        ocr_dl, "check_models", lambda d: SimpleNamespace(missing=("a",), corrupt=())
    )

    def fail(model_dir, **kwargs):
        raise UnsupportedError("OCR 模型下载失败：a（无可用镜像）", hint="手动放置")

    monkeypatch.setattr(ocr_dl, "ensure_models", fail)
    frames: list[dict] = []
    ocr_dl.download_ocr_models(_ctx(tmp_path), frames.append)
    assert frames[-1] == {"error": "OCR 模型下载失败：a（无可用镜像）", "hint": "手动放置"}


def test_download_rejected_without_onnxruntime(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ocr_dl, "module_available", lambda name: False)
    with pytest.raises(ConfigError):
        ocr_dl.download_ocr_models(_ctx(tmp_path), lambda frame: None)


def test_runner_without_download_permission_points_to_settings(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(capture, "onnx_available", lambda: True)
    monkeypatch.setattr(
        capture, "check_models", lambda d: SimpleNamespace(ready=False, missing=("a",), corrupt=())
    )
    runner = OcrRunner(engine="onnx", model_dir=tmp_path, allow_download=False)
    with pytest.raises(UnsupportedError) as caught:
        runner._create_engine()
    assert caught.value.hint is not None and "设置页" in caught.value.hint


def test_runner_with_ready_models_does_not_call_download(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(capture, "onnx_available", lambda: True)
    monkeypatch.setattr(capture, "check_models", lambda d: SimpleNamespace(ready=True))
    monkeypatch.setattr(capture, "OnnxEngine", lambda model_dir: object())
    runner = OcrRunner(engine="onnx", model_dir=tmp_path, allow_download=False)
    assert runner._create_engine() is not None
