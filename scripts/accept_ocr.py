"""M5 验收：真实 PP-OCRv4 模型在固定中文/数字/标点样本上的准确率核对（项目设计.md §6.7）。

用法：``.venv/bin/python scripts/accept_ocr.py``（需先安装 ``quire-local[ocr]``）。

流程：
1. 用 Pillow + 系统宋体渲染固定样本（中文散文 / 数字混排 / 标点密集），
   含轻度噪声、倾斜、低对比三类扫描常见退化；
2. 真实 ``OnnxEngine``（按需下载的 PP-OCRv4 mobile 模型）逐张识别；
3. 以 Levenshtein 编辑距离核对字符准确率，逐样本与汇总输出，
   报告写入 ``output/m5-acceptance/report.json``。

本脚本只做验收测量，不进入单元测试（真实模型下载不进 CI）。
"""

from __future__ import annotations

import json
import sys
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from quire.ocr.models import ensure_models
from quire.ocr.onnx_engine import OnnxEngine
from quire.ocr.postprocess import merge_lines, normalize_paragraphs

MODEL_DIR = Path("output/m5-models")
REPORT = Path("output/m5-acceptance/report.json")
FONT_PATH = "/System/Library/Fonts/Supplemental/Songti.ttc"

#: 固定样本：中文散文、数字混排、标点密集，各配一种退化变体。
SAMPLES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "chinese_prose",
        (
            "他抬头看见远山如黛，风从林间穿过，带来潮湿的泥土气息。",
            "这一趟必须走下去，因为答案就在前面的村落里等着他。",
        ),
        "clean",
    ),
    (
        "chinese_noisy",
        (
            "她翻开那本旧书，纸页已经发黄，字迹却依然清晰可辨。",
            "窗外的雨下了一整夜，到天亮时才渐渐停歇下来。",
        ),
        "noise",
    ),
    (
        "digits_mixed",
        (
            "全书共238章，定价49.80元，1997年5月第1次印刷。",
            "联系电话：010-68502114，邮政编码100045。",
        ),
        "clean",
    ),
    (
        "punctuation_dense",
        (
            "“你当真要走？”她问。“是的。”他答道，“天一亮就走……”",
            "他说：世上的事，十有八九不如意；可那又如何呢？！",
        ),
        "clean",
    ),
    (
        "skewed_page",
        (
            "这一页略微倾斜，用来检验预处理流水线的纠偏能力。",
            "纠偏之后，识别结果应当与端正的页面保持一致。",
        ),
        "skew",
    ),
    (
        "low_contrast",
        (
            "低对比度的扫描页面常见于老旧的复印件与褪色纸张。",
            "对比度增强之后，文字笔画应当重新变得清晰可辨。",
        ),
        "faded",
    ),
)


@dataclass(frozen=True)
class SampleResult:
    name: str
    expected: str
    recognized: str
    accuracy: float
    confidence: float


def render(lines: tuple[str, ...], degrade: str) -> Image.Image:
    """把样本文字渲染成模拟扫描页（宋体 32px，黑字米白底）。"""
    font = ImageFont.truetype(FONT_PATH, 32)
    width, height = 1100, 60 + len(lines) * 64
    image = Image.new("L", (width, height), 250)
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(lines):
        draw.text((80, 30 + index * 64), line, font=font, fill=20)
    if degrade == "noise":
        import random

        rng = random.Random(42)
        for _ in range(1500):
            x, y = rng.randrange(width), rng.randrange(height)
            image.putpixel((x, y), rng.randrange(256))
    elif degrade == "skew":
        image = image.rotate(-1.8, resample=Image.Resampling.BILINEAR, fillcolor=250)
    elif degrade == "faded":
        image = image.point(lambda v: 250 - (250 - v) // 4)
    return image.filter(ImageFilter.GaussianBlur(0.4))


def levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    row = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, cb in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (ca != cb))
    return row[-1]


def normalize_for_compare(text: str) -> str:
    """核对前去掉空白与换行（只比字符，不比排版）。"""
    return "".join(unicodedata.normalize("NFKC", text).split())


def main() -> int:
    started = time.monotonic()
    status = ensure_models(MODEL_DIR)
    assert status.ready, status
    engine = OnnxEngine(MODEL_DIR)
    results: list[SampleResult] = []
    for name, lines, degrade in SAMPLES:
        image = render(lines, degrade)
        from io import BytesIO

        buffer = BytesIO()
        image.save(buffer, format="PNG")
        page = engine.recognize(buffer.getvalue())
        recognized = "".join(normalize_paragraphs(merge_lines(page.lines)))
        expected = "".join(lines)
        distance = levenshtein(normalize_for_compare(expected), normalize_for_compare(recognized))
        accuracy = 1 - distance / max(len(normalize_for_compare(expected)), 1)
        results.append(
            SampleResult(name, expected, recognized, round(accuracy, 4), round(page.confidence, 4))
        )
        sys.stdout.write(f"[{name}] 准确率 {accuracy:.2%}，置信度 {page.confidence:.3f}\n")
        sys.stdout.write(f"  期望：{expected}\n")
        sys.stdout.write(f"  识别：{recognized}\n")
    total_expected = sum(len(normalize_for_compare(item.expected)) for item in results)
    total_distance = sum(
        round((1 - item.accuracy) * len(normalize_for_compare(item.expected))) for item in results
    )
    overall = 1 - total_distance / total_expected
    passed = overall >= 0.95
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(
        json.dumps(
            {
                "passed": passed,
                "overall_accuracy": round(overall, 4),
                "elapsed_s": round(time.monotonic() - started, 3),
                "samples": [item.__dict__ for item in results],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    sys.stdout.write(f"\n总体字符准确率 {overall:.2%}（目标 ≥95%），报告 {REPORT}\n")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
