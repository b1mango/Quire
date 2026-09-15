"""共享测试夹具。

图片夹具用 Pillow 现生成（Pillow 只在开发依赖里，产品代码不碰它）。
图案刻意用**渐变 + 结构化噪点**而不是纯色——
纯色图会掩盖滤波/预测器 bug，这是踩过的坑。
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

FILL = {"RGB": (180, 180, 180), "RGBA": (200, 30, 30, 128), "L": 180, "1": 1, "P": 3}


def structured_image(width: int, height: int, mode: str = "RGB") -> Image.Image:
    """有结构的测试图：横向渐变 + 纵向渐变 + 异或噪点。"""
    im = Image.new(mode, (width, height))
    px = im.load()
    assert px is not None
    for y in range(height):
        for x in range(width):
            r = x * 255 // max(1, width - 1)
            g = y * 255 // max(1, height - 1)
            b = (x * 7 ^ y * 13) & 0xFF
            if mode == "RGB":
                px[x, y] = (r, g, b)
            elif mode == "RGBA":
                px[x, y] = (r, g, b, (x * 2) % 256)
            elif mode == "L":
                px[x, y] = (x * 3 + y * 5) & 0xFF
    return im


def make_image_bytes(
    fmt: str, mode: str = "RGB", size: tuple[int, int] = (120, 90), **kwargs: object
) -> bytes:
    im = structured_image(size[0], size[1], mode)
    if mode == "P":
        im = im.convert("P", palette=Image.ADAPTIVE, colors=kwargs.pop("colors", 16))
    elif mode == "1":
        im = im.convert("1")
    buf = io.BytesIO()
    im.save(buf, format=fmt, **kwargs)  # type: ignore[arg-type]
    return buf.getvalue()


@pytest.fixture(scope="session")
def image_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """一组覆盖各格式的图片文件。"""
    d = tmp_path_factory.mktemp("images")
    specs = [
        ("JPEG", "RGB", "jpg", {}),
        ("PNG", "RGB", "png", {}),
        ("PNG", "RGBA", "png", {}),
        ("PNG", "L", "png", {}),
        ("PNG", "P", "png", {}),
        ("PNG", "1", "png", {}),
        ("GIF", "P", "gif", {}),
        ("BMP", "RGB", "bmp", {}),
        ("WEBP", "RGB", "webp", {}),
    ]
    for fmt, mode, ext, kw in specs:
        data = make_image_bytes(fmt, mode, **kw)
        (d / f"{fmt}_{mode}.{ext}").write_bytes(data)
    return d
