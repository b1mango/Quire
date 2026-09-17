"""OCR 预处理：纯 Pillow 流水线测试（项目设计.md §6.7，不引 opencv）。"""

from __future__ import annotations

from PIL import Image, ImageDraw

from quire.ocr.preprocess import (
    MIN_SHORT_EDGE,
    denoise,
    deskew,
    detect_skew,
    enhance_contrast,
    grayscale,
    prepare,
    split_lines,
    upscale,
)


def text_image(
    size: tuple[int, int] = (400, 200), *, lines: int = 4, angle: float = 0.0
) -> Image.Image:
    """合成一张有若干条水平「文字行」的灰度图（黑带模拟墨点）。"""
    image = Image.new("L", size, 255)
    draw = ImageDraw.Draw(image)
    top, step = 20, (size[1] - 40) // max(lines, 1)
    for index in range(lines):
        y = top + index * step
        draw.rectangle((20, y, size[0] - 20, y + step // 3), fill=0)
    if angle:
        image = image.rotate(angle, resample=Image.Resampling.BILINEAR, fillcolor=255)
    return image


def test_grayscale_converts_to_l() -> None:
    assert grayscale(Image.new("RGB", (10, 10), (128, 64, 200))).mode == "L"


def test_enhance_contrast_widens_histogram() -> None:
    """窄范围渐变的图分块均衡后，像素取值范围应变宽。"""
    image = Image.new("L", (256, 64))
    for x in range(256):
        for y in range(64):
            image.putpixel((x, y), 110 + round(x / 256 * 20))
    out = enhance_contrast(image)
    assert out.mode == "L"
    lo, hi = out.getextrema()
    assert hi - lo > 20


def test_enhance_contrast_keeps_uniform_tiles() -> None:
    """纯色块没有可均衡的分布，必须保持原灰度而不是被拉成全黑。"""
    image = Image.new("L", (200, 200), 120)
    out = enhance_contrast(image)
    assert out.getextrema() == (120, 120)


def test_enhance_contrast_keeps_tiny_images() -> None:
    """小于分块尺寸的图不做分块，只灰度化。"""
    image = Image.new("RGB", (8, 8), (10, 20, 30))
    out = enhance_contrast(image)
    assert out.mode == "L" and out.size == (8, 8)


def test_denoise_removes_isolated_pixels() -> None:
    image = Image.new("L", (30, 30), 255)
    image.putpixel((15, 15), 0)
    assert denoise(image).getpixel((15, 15)) == 255


def test_upscale_short_edge_to_minimum() -> None:
    out = upscale(Image.new("L", (400, 200)))
    assert min(out.size) == MIN_SHORT_EDGE
    assert out.size[0] == 2000  # 等比例
    original = Image.new("L", (1200, 1500))
    assert upscale(original) is original


def test_detect_skew_finds_angle() -> None:
    assert detect_skew(text_image(angle=0.0)) == 0.0
    detected = detect_skew(text_image(angle=-2.0))
    assert abs(detected - 2.0) <= 0.5


def test_deskew_rotates_back() -> None:
    skewed = text_image(angle=-2.0)
    assert detect_skew(deskew(skewed)) == 0.0
    flat = text_image(angle=0.0)
    assert deskew(flat) is flat  # 角度过小不重采样


def test_split_lines_finds_bands() -> None:
    boxes = split_lines(text_image(lines=3))
    assert len(boxes) == 3
    assert [box[1] for box in boxes] == sorted(box[1] for box in boxes)  # 自上而下
    blank = split_lines(Image.new("L", (100, 100), 255))
    assert blank == []


def test_prepare_pipeline() -> None:
    out = prepare(text_image(size=(300, 150), angle=-1.5).convert("RGB"))
    assert out.mode == "L"
    assert min(out.size) >= MIN_SHORT_EDGE
