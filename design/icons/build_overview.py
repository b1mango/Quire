#!/usr/bin/env python3
"""拼接 6 款图标方案的大小尺寸对比图(轻预览,不入库)。"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ICONS = [
    ("A-gathering", "A · 装订叠页"),
    ("B-quire-mark", "B · 卷 Q 字标"),
    ("C-the-pull", "C · 书库抽本"),
    ("D-scroll", "D · 卷轴"),
    ("E-folio-dark", "E · 暗房书页"),
    ("F-capture", "F · 采集成书"),
]
BASE = str(Path(__file__).resolve().parent)

cell_big = 300
small_sizes = [64, 32, 16]
pad = 40
label_h = 46
cols = len(ICONS)
rows_bg = ["#F5F5F3", "#1A1D20"]

W = cols * (cell_big + pad) + pad
H = 2 * (cell_big + label_h + pad) + pad + 2 * (sum(small_sizes) + pad * 4)

sheet = Image.new("RGB", (W, H), "#FFFFFF")
draw = ImageDraw.Draw(sheet)
font = font_sm = None
for fp in [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
]:
    try:
        font = ImageFont.truetype(fp, 26)
        font_sm = ImageFont.truetype(fp, 18)
        break
    except Exception:
        continue
if font is None:
    font = font_sm = ImageFont.load_default()


def render(svg_name, size):
    import os
    import subprocess
    import tempfile

    tmp = tempfile.mktemp(suffix=".png")
    subprocess.run(
        [
            "sips",
            "-s",
            "format",
            "png",
            "-z",
            str(size),
            str(size),
            f"{BASE}/{svg_name}.svg",
            "--out",
            tmp,
        ],
        check=True,
        capture_output=True,
    )
    im = Image.open(tmp).convert("RGBA")
    os.unlink(tmp)
    return im


x = pad
for name, label in ICONS:
    big = render(name, cell_big)
    # 上行:浅色底;下行:深色底
    for r, bg in enumerate(rows_bg):
        y0 = pad + r * (cell_big + label_h + pad)
        cell = Image.new("RGB", (cell_big, cell_big), bg)
        cell.paste(big, (0, 0), big)
        sheet.paste(cell, (x, y0))
        if draw:
            draw.text((x + 4, y0 + cell_big + 6), label, fill="#333333", font=font)
    # 小尺寸列(浅色底)
    y = 2 * (cell_big + label_h + pad) + pad
    for s in small_sizes:
        im = render(name, s)
        cell = Image.new("RGB", (s, s), "#F5F5F3")
        cell.paste(im, (0, 0), im)
        sheet.paste(cell, (x, y))
        cell_d = Image.new("RGB", (s, s), "#1A1D20")
        cell_d.paste(im, (0, 0), im)
        sheet.paste(cell_d, (x + s + 8, y))
        if draw:
            draw.text((x + 2 * s + 20, y), f"{s}px", fill="#555555", font=font_sm)
        y += s + pad
    x += cell_big + pad

out = f"{BASE}/overview-grid.png"
sheet.save(out)
sys.stdout.write(out + "\n")
