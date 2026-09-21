"""OCR 预处理：纯 Pillow 手写，不引 opencv（项目设计.md §6.7、§2.2）。

流水线：灰度 → 对比度增强（CLAHE 查找表近似）→ 中值去噪
→ 短边放大到 ≥1000px（小字准确率提升最明显的一步）→ 纠偏。
纠偏与切行共用投影分析：二值化后按行统计墨点，
纠偏取投影方差最大的角度（文字行水平时峰谷最分明），
切行取墨点连续的行带——只作为 det 模型零检出时的兜底路径。
"""

from __future__ import annotations

from math import sqrt
from typing import cast

from PIL import Image, ImageFilter

from ..image.codec import MAX_DIM, MAX_PIXELS, check_image_size

#: 短边放大的目标（项目设计.md §6.7）。
MIN_SHORT_EDGE = 1000

#: 纠偏搜索范围与步长（度）。
MAX_SKEW_DEG = 5.0
SKEW_STEP_DEG = 0.5

#: CLAHE 近似的网格数与对比度限制因子。
CLAHE_TILES = 8
CLAHE_CLIP = 2.0

#: 二值化兜底阈值：图像灰度范围过窄（近似纯色）时使用。
INK_THRESHOLD = 128

#: 切行时允许的行内断档（像素）与最小行高。
LINE_GAP = 3
MIN_LINE_HEIGHT = 8


def grayscale(image: Image.Image) -> Image.Image:
    return image.convert("L")


def _clipped_lut(hist: list[int], clip_limit: int) -> list[int]:
    """限制直方图峰值后做累积均衡，返回 256 档查找表（CLAHE 的单块近似）。"""
    if sum(1 for count in hist if count) <= 1:
        # 纯色块：没有可均衡的分布，保持原样（否则会被拉成全黑）。
        return list(range(256))
    excess = sum(max(0, count - clip_limit) for count in hist)
    clipped = [min(count, clip_limit) for count in hist]
    bonus, remainder = divmod(excess, 256)
    clipped = [count + bonus + (1 if i < remainder else 0) for i, count in enumerate(clipped)]
    total = sum(clipped)
    if not total:
        return list(range(256))
    cdf: list[int] = []
    running = 0
    for count in clipped:
        running += count
        cdf.append(running)
    floor = next((value for value in cdf if value), 0)
    span = max(total - floor, 1)
    return [min(255, max(0, round((value - floor) * 255 / span))) for value in cdf]


def enhance_contrast(image: Image.Image) -> Image.Image:
    """分块直方图均衡（CLAHE 查找表近似）：每块裁剪后套本块 LUT 再贴回。"""
    gray = grayscale(image)
    width, height = gray.size
    if min(width, height) < CLAHE_TILES * 2:
        return gray
    out = Image.new("L", gray.size)
    tile_w, tile_h = max(width // CLAHE_TILES, 1), max(height // CLAHE_TILES, 1)
    clip = max(1, int(tile_w * tile_h / 256 * CLAHE_CLIP))
    for row in range(CLAHE_TILES):
        for col in range(CLAHE_TILES):
            box = (
                col * tile_w,
                row * tile_h,
                width if col == CLAHE_TILES - 1 else (col + 1) * tile_w,
                height if row == CLAHE_TILES - 1 else (row + 1) * tile_h,
            )
            tile = gray.crop(box)
            out.paste(tile.point(_clipped_lut(tile.histogram(), clip)), box)
    return out


def denoise(image: Image.Image) -> Image.Image:
    return image.filter(ImageFilter.MedianFilter(3))


def upscale(image: Image.Image, min_short_edge: int = MIN_SHORT_EDGE) -> Image.Image:
    """按比例放大短边，同时限制目标像素与最长边；不缩小原图。"""
    check_image_size(image.size)
    width, height = image.size
    short = min(width, height)
    if short >= min_short_edge or short == 0:
        return image
    factor = min(
        min_short_edge / short,
        sqrt(MAX_PIXELS / (width * height)),
        MAX_DIM / max(width, height),
    )
    # Floor keeps rounding from crossing the allocation budget.
    size = (int(width * factor), int(height * factor))
    check_image_size(size)
    if size == image.size:
        return image
    return image.resize(size, Image.Resampling.LANCZOS)


def _ink_profile(image: Image.Image) -> list[int]:
    """每一行的墨点像素数（二值化后统计，字节操作在 C 层完成）。

    阈值按图像自身灰度范围取中点：褪色的低对比扫描页文字可能远在
    固定阈值之上，自适应阈值才能保证纠偏与切行仍有墨点可用。
    """
    gray = grayscale(image)
    lo, hi = cast("tuple[int, int]", gray.getextrema())
    threshold = (lo + hi) // 2 if hi - lo > 8 else INK_THRESHOLD
    binary = gray.point(lambda v: 0 if v < threshold else 255)
    width, height = binary.size
    data = binary.tobytes()
    return [width - data[row * width : (row + 1) * width].count(255) for row in range(height)]


def detect_skew(
    image: Image.Image,
    *,
    max_deg: float = MAX_SKEW_DEG,
    step: float = SKEW_STEP_DEG,
) -> float:
    """投影方差最大化估倾斜角：在 ``±max_deg`` 内按 ``step`` 搜索。"""
    probe = grayscale(image)
    if min(probe.size) > 600:
        factor = 600 / min(probe.size)
        probe = probe.resize(
            (round(probe.width * factor), round(probe.height * factor)),
            Image.Resampling.BILINEAR,
        )
    best_angle, best_variance = 0.0, -1.0
    steps = round(max_deg / step)
    for index in range(-steps, steps + 1):
        angle = index * step
        rotated = probe.rotate(angle, resample=Image.Resampling.BILINEAR, fillcolor=255)
        profile = _ink_profile(rotated)
        mean = sum(profile) / len(profile)
        variance = sum((value - mean) ** 2 for value in profile) / len(profile)
        if variance > best_variance:
            best_angle, best_variance = angle, variance
    if not best_variance or sum(_ink_profile(probe)) == 0:
        return 0.0  # 没有墨点（空白页/极浅扫描）：任何角度都不可靠，不纠偏
    return best_angle if abs(best_angle) >= step / 2 else 0.0


def deskew(image: Image.Image) -> Image.Image:
    """按检测到的角度旋转纠偏；角度过小不处理，避免无谓重采样。"""
    angle = detect_skew(image)
    if not angle:
        return image
    return image.rotate(angle, resample=Image.Resampling.BICUBIC, fillcolor=255)


def split_lines(image: Image.Image) -> list[tuple[int, int, int, int]]:
    """投影切行：墨点连续的行带切成行框 ``(x0, y0, x1, y1)``，允许小断档。"""
    profile = _ink_profile(image)
    width = image.width
    boxes: list[tuple[int, int, int, int]] = []
    start: int | None = None
    last_ink = 0
    for row, ink in enumerate(profile):
        if ink >= 2:
            if start is None:
                start = row
            last_ink = row
        elif start is not None and row - last_ink > LINE_GAP:
            if last_ink - start + 1 >= MIN_LINE_HEIGHT:
                boxes.append((0, start, width, last_ink + 1))
            start = None
    if start is not None and last_ink - start + 1 >= MIN_LINE_HEIGHT:
        boxes.append((0, start, width, last_ink + 1))
    return boxes


def prepare(image: Image.Image) -> Image.Image:
    """完整预处理流水线（项目设计.md §6.7 的顺序）。"""
    check_image_size(image.size)
    return deskew(upscale(denoise(enhance_contrast(image))))
