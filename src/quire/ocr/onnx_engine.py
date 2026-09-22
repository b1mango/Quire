"""onnxruntime + PP-OCRv4 mobile 模型引擎（项目设计.md §6.7 A1 落地）。

只依赖 ``onnxruntime``（及其传递依赖 numpy），不引 opencv/rapidocr：
det 的 DB 后处理用纯 Python 并查集找连通块，再按 DB unclip 公式外扩矩形；
rec 用 CTC 贪心解码，字典 ``ppocr_keys_v1.txt`` 随模型一起下载校验。
det 零检出时退回预处理的投影切行（§6.7 的兜底路径）。

模型目录必须先通过 ``ocr.models.ensure_models`` 校验；
本模块加载前会再验一次 SHA-256，损坏的模型不会被加载。
"""

from __future__ import annotations

import importlib.util
import logging
from contextlib import closing
from pathlib import Path
from types import ModuleType
from typing import Any

from PIL import Image

from ..errors import FetchError, UnsupportedError
from ..image.codec import check_image_size, decoded_image
from .base import OcrLine, OcrPage
from .models import check_models, manual_hint
from .preprocess import prepare, split_lines
from .tesseract import OcrEngineError

_LOG = logging.getLogger(__name__)

#: det 输入的最长边与对齐；二值化阈值与 unclip 比例（DB 后处理惯例值）。
DET_LIMIT = 1600
DET_ALIGN = 32
DET_THRESHOLD = 0.3
DET_UNCLIP = 1.6

#: rec/cls 输入高度；cls 宽度与旋转判定置信度。
REC_HEIGHT = 48
CLS_WIDTH = 192
CLS_ROTATE_SCORE = 0.9

#: 过小的检出框视为噪点。
MIN_BOX_HEIGHT = 10

DET_MODEL = "ch_PP-OCRv4_det_mobile.onnx"
REC_MODEL = "ch_PP-OCRv4_rec_mobile.onnx"
CLS_MODEL = "ch_ppocr_mobile_v2.0_cls_mobile.onnx"
REC_KEYS = "ppocr_keys_v1.txt"


def onnx_available() -> bool:
    return importlib.util.find_spec("onnxruntime") is not None


def _runtime() -> tuple[ModuleType, ModuleType]:
    if not onnx_available():
        raise UnsupportedError(
            "缺少 onnxruntime，无法使用内置 OCR 引擎",
            hint="安装 quire-local[ocr]，或安装系统 tesseract 作为替代引擎",
        )
    import numpy
    import onnxruntime

    return numpy, onnxruntime


def _session(ort: ModuleType, path: Path) -> Any:
    options = ort.SessionOptions()
    options.log_severity_level = 3
    try:
        return ort.InferenceSession(
            str(path), sess_options=options, providers=["CPUExecutionProvider"]
        )
    except Exception as exc:  # onnxruntime 抛自家异常层次，统一转为领域错误
        raise OcrEngineError(f"OCR 模型加载失败：{path.name}（{type(exc).__name__}）") from exc


def _load_keys(path: Path) -> list[str]:
    keys = path.read_text(encoding="utf-8").splitlines()
    if not keys:
        raise OcrEngineError("OCR 字典为空")
    return keys


def _resize_det(image: Image.Image) -> tuple[Image.Image, float]:
    """最长边限制 + 32 对齐，返回缩放后的图与还原系数。"""
    width, height = image.size
    factor = min(DET_LIMIT / max(width, height), 1.0)
    width = max(DET_ALIGN, round(width * factor / DET_ALIGN) * DET_ALIGN)
    height = max(DET_ALIGN, round(height * factor / DET_ALIGN) * DET_ALIGN)
    resized = image.resize((width, height), Image.Resampling.BILINEAR)
    return resized, image.width / width


def _to_tensor(np: ModuleType, image: Image.Image, size: tuple[int, int] | None = None) -> Any:
    """RGB 图转 ``1×3×H×W`` 的 (x-0.5)/0.5 归一化张量。"""
    if size is not None:
        check_image_size(size)
        image = image.resize(size, Image.Resampling.BILINEAR)
    array = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0
    array = (array - 0.5) / 0.5
    return array.transpose(2, 0, 1)[None, :, :, :]


def _components(bitmap: Any) -> list[tuple[int, int, int, int]]:
    """纯 Python 并查集找二值图连通块，返回轴对齐外接框。"""
    height, width = bitmap.shape
    parent = list(range(width * height))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    ones = zip(*bitmap.nonzero(), strict=True)
    for row, col in ones:
        index = row * width + col
        if col and bitmap[row, col - 1]:
            parent[root(index)] = root(index - 1)
        if row and bitmap[row - 1, col]:
            parent[root(index)] = root(index - width)
    boxes: dict[int, list[int]] = {}
    for row, col in zip(*bitmap.nonzero(), strict=True):
        owner = root(row * width + col)
        box = boxes.setdefault(owner, [col, row, col, row])
        box[0], box[1] = min(box[0], col), min(box[1], row)
        box[2], box[3] = max(box[2], col), max(box[3], row)
    return [(b[0], b[1], b[2] + 1, b[3] + 1) for b in boxes.values()]


def _unclip(box: tuple[int, int, int, int], size: tuple[int, int]) -> tuple[int, int, int, int]:
    """按 DB unclip 公式外扩矩形（offset = 面积×比例 ÷ 周长），夹在图内。"""
    x0, y0, x1, y1 = box
    width, height = x1 - x0, y1 - y0
    delta = width * height * DET_UNCLIP / (2 * (width + height))
    return (
        max(0, round(x0 - delta)),
        max(0, round(y0 - delta)),
        min(size[0], round(x1 + delta)),
        min(size[1], round(y1 + delta)),
    )


class OnnxEngine:
    """``OcrEngine`` 协议的 onnxruntime 实现（PP-OCRv4 det + cls + rec）。"""

    name = "onnx"

    def __init__(self, model_dir: Path):
        status = check_models(model_dir)
        if not status.ready:
            wanted = (*status.missing, *status.corrupt)
            raise UnsupportedError(
                "OCR 模型缺失或损坏：" + "、".join(wanted),
                hint=manual_hint(model_dir, wanted),
            )
        np, ort = _runtime()
        self._np = np
        self._det = _session(ort, model_dir / DET_MODEL)
        self._rec = _session(ort, model_dir / REC_MODEL)
        self._cls = _session(ort, model_dir / CLS_MODEL)
        self._keys = _load_keys(model_dir / REC_KEYS)

    def recognize(self, image_bytes: bytes) -> OcrPage:
        try:
            with decoded_image(image_bytes) as (image, _), closing(prepare(image)) as prepared:
                boxes = self._detect(prepared)
                if not boxes:
                    boxes = split_lines(prepared)
                lines: list[OcrLine] = []
                skipped = 0
                for box in boxes:
                    try:
                        line = self._recognize_line(prepared, box)
                    except FetchError as exc:
                        # 单个行框尺寸非法只跳过该行，不拖垮整页
                        skipped += 1
                        _LOG.warning("OCR skip over-budget line box %s: %s", box, exc)
                        continue
                    if line:
                        lines.append(line)
                if skipped and skipped == len(boxes):
                    raise OcrEngineError(f"整页 {skipped} 个行框尺寸均超出限制，无法识别")
                return OcrPage(tuple(lines), self.name)
        except FetchError as exc:
            raise OcrEngineError(f"OCR 输入不是可解码或尺寸合规的图片：{exc}") from exc

    # ---------------------------------------------------------- det
    def _detect(self, image: Image.Image) -> list[tuple[int, int, int, int]]:
        np = self._np
        resized, scale = _resize_det(image)
        tensor = _to_tensor(np, resized)
        name = self._det.get_inputs()[0].name
        prob = self._det.run(None, {name: tensor})[0][0, 0]
        bitmap = prob > DET_THRESHOLD
        if not bitmap.any():
            return []
        boxes: list[tuple[int, int, int, int]] = []
        for box in _components(bitmap):
            x0, y0, x1, y1 = _unclip(box, resized.size)
            scaled = (
                round(x0 * scale),
                round(y0 * scale),
                min(image.width, round(x1 * scale)),
                min(image.height, round(y1 * scale)),
            )
            if scaled[3] - scaled[1] >= MIN_BOX_HEIGHT and scaled[2] > scaled[0]:
                boxes.append(scaled)
        boxes.sort(key=lambda box: (box[1], box[0]))
        return boxes

    # ---------------------------------------------------- cls + rec
    def _recognize_line(self, image: Image.Image, box: tuple[int, int, int, int]) -> OcrLine | None:
        crop = image.crop(box)
        crop = self._orient(crop)
        text, confidence = self._read(crop)
        if not text:
            return None
        return OcrLine(text, confidence, box[0], box[1], box[2], box[3])

    def _orient(self, crop: Image.Image) -> Image.Image:
        """cls 模型判定 0°/180°；只有很有把握时才旋转。"""
        tensor = _to_tensor(self._np, crop, (CLS_WIDTH, REC_HEIGHT))
        scores = self._cls.run(None, {self._cls.get_inputs()[0].name: tensor})[0][0]
        index = int(scores.argmax())
        if index == 1 and float(scores[index]) > CLS_ROTATE_SCORE:
            return crop.transpose(Image.Transpose.ROTATE_180)
        return crop

    def _read(self, crop: Image.Image) -> tuple[str, float]:
        """rec 模型 + CTC 贪心解码；置信度取所选 token 的概率均值。

        PP-OCRv4 mobile 的 rec 输出已经是按行归一化的概率分布，
        直接取最大值；遇到未归一化的 logits 导出才补 softmax。
        """
        np = self._np
        width = max(REC_HEIGHT, round(crop.width * REC_HEIGHT / crop.height))
        tensor = _to_tensor(np, crop, (width, REC_HEIGHT))
        logits = self._rec.run(None, {self._rec.get_inputs()[0].name: tensor})[0][0]
        row_sums = logits.sum(axis=1)
        if bool((logits >= 0).all()) and bool(np.abs(row_sums - 1.0).max() < 1e-3):
            probs = logits
        else:
            probs = np.exp(logits - logits.max(axis=1, keepdims=True))
            probs /= probs.sum(axis=1, keepdims=True)
        chosen = probs.argmax(axis=1)
        text: list[str] = []
        confidence: list[float] = []
        previous = -1
        for token, score in zip(chosen, probs.max(axis=1), strict=True):
            index = int(token)
            if index and index != previous and index - 1 < len(self._keys):
                text.append(self._keys[index - 1])
                confidence.append(float(score))
            previous = index
        if not text:
            return "", 0.0
        return "".join(text), round(sum(confidence) / len(confidence), 4)
