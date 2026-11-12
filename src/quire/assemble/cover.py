"""为 EPUB 绘制与书库一致的线装题签封面。"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from fontTools.ttLib import TTFont, TTLibError
from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = (
    Path("/System/Library/Fonts/Supplemental/Songti.ttc"),
    Path("/System/Library/Fonts/STHeiti Medium.ttc"),
    Path("/System/Library/Fonts/PingFang.ttc"),
)
COLORS = (
    "#2E4A62",
    "#3D5A4C",
    "#7A2E2A",
    "#2B3A55",
    "#8A5A2B",
    "#4A3B52",
    "#5B6B3A",
    "#6E2F3F",
    "#3A4A4A",
    "#7B6A4A",
)


def cloth_color(title: str) -> str:
    """布色：按码点做 FNV-1a，与前端 covers.js 的 clothColor 同算法，书库与 EPUB 封面同色。"""
    value = 2166136261
    for char in title or "书":
        value = ((value ^ ord(char)) * 16777619) & 0xFFFFFFFF
    return COLORS[value % len(COLORS)]


def create_cover(title: str) -> bytes | None:
    shown = title if len(title) <= 9 else title[:8] + "…"
    for path in FONT_CANDIDATES:
        try:
            with TTFont(path, fontNumber=0, lazy=True) as candidate:
                cmap = candidate.getBestCmap() or {}
                required = "中文" + shown
                if any(ord(char) not in cmap for char in required if not char.isspace()):
                    continue
            font = ImageFont.truetype(str(path), 120, index=0)
        except (OSError, ValueError, KeyError, TTLibError):
            continue
        break
    else:
        return None

    image = Image.new("RGB", (1200, 1600), cloth_color(title))
    draw = ImageDraw.Draw(image, "RGBA")
    for column in range(216):
        draw.line((column, 0, column, 1599), fill=(255, 255, 255, int(26 * (1 - column / 216))))
    for y in range(1600):
        if y < 800:
            shade = (255, 255, 255, int(15 * (1 - y / 800)))
        else:
            shade = (0, 0, 0, int(25 * (y - 800) / 800))
        draw.line((0, y, 1199, y), fill=shade)
    for offset in range(-1600, 1200, 4):
        draw.line((offset, 0, offset + 1600, 1600), fill=(255, 255, 255, 5), width=2)
    line_x = 1128
    draw.line((line_x, 0, line_x, 1600), fill=(243, 238, 223, 58), width=2)
    for y in (320, 640, 960, 1280):
        draw.line((line_x - 30, y, line_x + 30, y), fill=(243, 238, 223, 140), width=8)

    if title.isascii():
        font = font.font_variant(size=110)
        left, top, right = 108, 96, 972
        while draw.textbbox((0, 0), shown, font=font)[2] > right - left - 100:
            font = font.font_variant(size=font.size - 1)
        box = draw.textbbox((0, 0), shown, font=font)
        bottom = top + box[3] - box[1] + 120
        draw.rectangle((left, top, right, bottom), fill="#F3EEDF", outline="#2A2520", width=1)
        draw.text((left + 50 - box[0], top + 60 - box[1]), shown, font=font, fill="#2A2520")
    else:
        left, top, right = 108, 96, 360
        size = min(140, int(840 / max(len(shown), 1) / 1.12))
        font = font.font_variant(size=size)
        step = int(size * 1.12)
        bottom = top + 120 + step * len(shown)
        draw.rectangle((left, top, right, bottom), fill="#F3EEDF", outline="#2A2520", width=1)
        for index, char in enumerate(shown):
            box = draw.textbbox((0, 0), char, font=font)
            x = (left + right - (box[2] - box[0])) / 2 - box[0]
            text_y = top + 60 + index * step + (step - (box[3] - box[1])) / 2 - box[1]
            draw.text((x, text_y), char, font=font, fill="#2A2520")
    stream = BytesIO()
    image.save(stream, format="PNG", optimize=False, compress_level=6)
    return stream.getvalue()
