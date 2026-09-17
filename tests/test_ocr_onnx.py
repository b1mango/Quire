"""onnxruntime 引擎：用假 InferenceSession 覆盖 det/cls/rec 逻辑，不依赖真实模型。

真实模型的下载与识别准确率属于里程碑验收（固定中文/数字/标点样本），
单元测试只锁死张量处理、DB 后处理与 CTC 解码逻辑。
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from quire.errors import UnsupportedError
from quire.ocr import onnx_engine
from quire.ocr.base import OcrLine, OcrPage
from quire.ocr.models import ModelStatus
from quire.ocr.onnx_engine import (
    OnnxEngine,
    _components,
    _load_keys,
    _resize_det,
    _to_tensor,
    _unclip,
    onnx_available,
)
from quire.ocr.tesseract import OcrEngineError


def png_bytes(size: tuple[int, int] = (300, 200)) -> bytes:
    image = Image.new("L", size, 255)
    draw = ImageDraw.Draw(image)
    for index in range(3):
        y = 20 + index * 60
        draw.rectangle((20, y, size[0] - 20, y + 20), fill=0)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class FakeSession:
    """按callable生成输出的假 InferenceSession。"""

    def __init__(self, produce) -> None:  # noqa: ANN001
        self.produce = produce

    def get_inputs(self):  # noqa: ANN201
        return [type("Input", (), {"name": "x"})()]

    def run(self, outputs: object, feed: dict[str, np.ndarray]) -> list[np.ndarray]:
        return [self.produce(feed["x"])]


def det_with_blob(tensor: np.ndarray) -> np.ndarray:
    """在 det 概率图中部放一块高概率区域。"""
    _, _, height, width = tensor.shape
    prob = np.zeros((1, 1, height, width), dtype=np.float32)
    prob[0, 0, height // 4 : height // 2, width // 4 : 3 * width // 4] = 0.9
    return prob


def rec_logits(text: str, keys: list[str], classes: int) -> np.ndarray:
    """生成让 CTC 贪心解码出 ``text`` 的 logits（含重复与 blank）。"""
    tokens: list[int] = []
    for char in text:
        tokens.extend((keys.index(char) + 1, keys.index(char) + 1, 0))
    logits = np.full((len(tokens), classes), -10.0, dtype=np.float32)
    for row, token in enumerate(tokens):
        if token:
            logits[row, token] = 10.0
        else:
            logits[row, 0] = 10.0
    return logits[None, :, :]


def make_engine(
    det=det_with_blob, rec_text: str = "你好", keys: list[str] | None = None, cls_label: int = 0
) -> OnnxEngine:
    engine = object.__new__(OnnxEngine)
    engine._np = np
    engine._keys = keys or ["你", "好", "世", "界"]
    classes = len(engine._keys) + 2
    engine._det = FakeSession(det)
    engine._rec = FakeSession(lambda tensor: rec_logits(rec_text, engine._keys, classes))
    scores = np.full((1, 2), 0.01, dtype=np.float32)
    scores[0, cls_label] = 0.99
    engine._cls = FakeSession(lambda tensor: scores)
    return engine


def test_onnxruntime_is_available_in_dev_env() -> None:
    assert onnx_available() is True


def test_components_finds_disjoint_blobs() -> None:
    bitmap = np.zeros((20, 20), dtype=bool)
    bitmap[2:5, 2:5] = True
    bitmap[10:15, 8:18] = True
    boxes = _components(bitmap)
    assert sorted(boxes) == [(2, 2, 5, 5), (8, 10, 18, 15)]
    assert _components(np.zeros((4, 4), dtype=bool)) == []


def test_unclip_expands_and_clamps() -> None:
    box = _unclip((10, 10, 30, 20), (40, 30))
    assert box[0] < 10 and box[1] < 10 and box[2] > 30 and box[3] > 20
    clamped = _unclip((0, 0, 40, 30), (40, 30))
    assert clamped == (0, 0, 40, 30)


def test_resize_det_limits_long_edge_and_aligns() -> None:
    resized, scale = _resize_det(Image.new("RGB", (4000, 1000)))
    assert max(resized.size) <= 1600
    assert resized.size[0] % 32 == 0 and resized.size[1] % 32 == 0
    assert scale == pytest.approx(4000 / resized.size[0])


def test_to_tensor_normalizes() -> None:
    tensor = _to_tensor(np, Image.new("RGB", (8, 8), (255, 255, 255)))
    assert tensor.shape == (1, 3, 8, 8)
    assert float(tensor.max()) == pytest.approx(1.0)
    assert float(tensor.min()) == pytest.approx(1.0)


def test_recognize_decodes_ctc_with_blank_and_repeats() -> None:
    engine = make_engine(rec_text="你好")
    page = engine.recognize(png_bytes())
    assert page.engine == "onnx"
    assert [line.text for line in page.lines] == ["你好"]
    assert page.lines[0].confidence > 0.9


def test_recognize_rejects_undecodable_bytes() -> None:
    engine = make_engine()
    with pytest.raises(OcrEngineError, match="不是可解码的图片"):
        engine.recognize(b"not an image")


def test_recognize_falls_back_to_projection_split() -> None:
    """det 零检出时退回预处理的投影切行。"""
    engine = make_engine(det=lambda tensor: np.zeros_like(tensor[:1, :1]))
    page = engine.recognize(png_bytes())
    assert len(page.lines) == 3  # 三条墨带切成三行
    assert all(line.text == "你好" for line in page.lines)


def test_recognize_rotates_when_cls_is_confident() -> None:
    upright = make_engine(cls_label=0)
    flipped = make_engine(cls_label=1)
    assert upright._orient(Image.new("L", (40, 20))).size == (40, 20)
    rotated = flipped._orient(Image.new("L", (40, 20)))
    assert rotated.size == (40, 20)  # 尺寸不变，像素转了 180°


def test_init_rejects_missing_models(tmp_path: Path) -> None:
    with pytest.raises(UnsupportedError) as caught:
        OnnxEngine(tmp_path)
    assert caught.value.exit_code == 6
    assert caught.value.hint is not None and str(tmp_path) in caught.value.hint


def test_load_keys_rejects_empty(tmp_path: Path) -> None:
    path = tmp_path / "keys.txt"
    path.write_text("", encoding="utf-8")
    with pytest.raises(OcrEngineError, match="字典为空"):
        _load_keys(path)
    path.write_text("你\n好\n", encoding="utf-8")
    assert _load_keys(path) == ["你", "好"]


def test_runtime_raises_without_onnxruntime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(onnx_engine, "onnx_available", lambda: False)
    with pytest.raises(UnsupportedError, match="onnxruntime"):
        onnx_engine._runtime()


def test_runtime_returns_real_modules() -> None:
    np_module, ort = onnx_engine._runtime()
    assert np_module.__name__ == "numpy" and ort.__name__ == "onnxruntime"


def test_base_dataclass_helpers() -> None:
    page = OcrPage((OcrLine("甲", 0.8), OcrLine("乙", 0.4)), "fake")
    assert OcrPage(()).confidence == 0.0
    assert page.confidence == pytest.approx(0.6)
    assert page.lines[0].needs_review is False
    assert page.lines[1].needs_review is True  # < 0.6 进复核清单


def test_init_with_fake_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """构造路径：模型校验通过 → 建三个会话 → 载字典（全部用假实现）。"""
    status = ModelStatus(tmp_path, True, (), ())
    monkeypatch.setattr(onnx_engine, "check_models", lambda model_dir: status)
    monkeypatch.setattr(onnx_engine, "_runtime", lambda: (np, None))
    sessions: list[str] = []
    monkeypatch.setattr(
        onnx_engine, "_session", lambda ort, path: sessions.append(path.name) or None
    )
    (tmp_path / onnx_engine.REC_KEYS).write_text("你\n好\n", encoding="utf-8")
    engine = OnnxEngine(tmp_path)
    assert sessions == [onnx_engine.DET_MODEL, onnx_engine.REC_MODEL, onnx_engine.CLS_MODEL]
    assert engine._keys == ["你", "好"]


def test_recognize_line_drops_blank_results() -> None:
    """rec 全 blank（空行框）→ 该行被丢弃。"""
    engine = make_engine(det=det_with_blob)
    classes = len(engine._keys) + 2
    blank = np.full((1, 5, classes), -10.0, dtype=np.float32)
    blank[0, :, 0] = 10.0
    engine._rec = FakeSession(lambda tensor: blank)
    page = engine.recognize(png_bytes())
    assert page.lines == ()


def test_read_accepts_probability_rows() -> None:
    """rec 输出已是归一化概率时直接取最大，不再套 softmax。"""
    engine = make_engine()
    classes = len(engine._keys) + 2
    rows = np.zeros((1, 4, classes), dtype=np.float32)
    for col, token in enumerate((1, 2, 2, 0)):
        rows[0, col, token] = 0.97
        rows[0, col, (token + 1) % classes] = 0.03  # 每行恰好归一化
    engine._rec = FakeSession(lambda tensor: rows)
    text, confidence = engine._read(Image.new("L", (80, 24), 255))
    assert text == "你好" and confidence == pytest.approx(0.97, abs=1e-3)
