"""PNG samples for the micro PDF writer."""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

from ..errors import UnsupportedError

_DECODE_BUDGET = 64 * 1024 * 1024
_DECODE_BLOCK = 64 * 1024


@dataclass(frozen=True, slots=True)
class _PngImage:
    """已经整理成 PDF 可直接使用的图像参数。"""

    width: int
    height: int
    bits: int
    colors: int  # PDF /Colors：1=灰度/索引，3=RGB
    color_space: bytes  # 如 b"/DeviceRGB" 或 b"[/Indexed /DeviceRGB 255 <...>]"
    data: bytes  # 压缩后的样本数据
    pre_compressed: bool  # True 表示 data 已是 zlib 流（可透传）


def _read_chunks(data: bytes) -> dict[bytes, list[bytes]]:
    """把 PNG 拆成 {chunk_type: [payload, ...]}。只解析头部区域，容忍截断。"""
    chunks: dict[bytes, list[bytes]] = {}
    i = 8  # 跳过签名
    n = len(data)
    while i + 8 <= n:
        (length,) = struct.unpack_from(">I", data, i)
        ctype = data[i + 4 : i + 8]
        payload = data[i + 8 : i + 8 + length]
        chunks.setdefault(ctype, []).append(payload)
        if ctype == b"IEND":
            break
        i += 12 + length
    return chunks


def _png_to_pdf_image(data: bytes) -> _PngImage:
    """验证 PNG 样本后透传 IDAT；带 alpha 的样本合成到白底。"""
    chunks = _read_chunks(data)
    ihdr = chunks.get(b"IHDR", [b""])[0]
    if len(ihdr) != 13:
        raise UnsupportedError("PNG 缺少 IHDR")

    width, height = struct.unpack_from(">II", ihdr, 0)
    bits = ihdr[8]
    color_type = ihdr[9]
    interlace = ihdr[12]

    if interlace:
        raise UnsupportedError("暂不支持 Adam7 隔行 PNG")
    allowed_bits = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8,), 6: (8,)}
    if (
        not 0 < width < 2**31
        or not 0 < height < 2**31
        or bits not in allowed_bits.get(color_type, ())
        or ihdr[10:12] != b"\x00\x00"
    ):
        raise UnsupportedError("Invalid or unsupported PNG header")

    idat = b"".join(chunks.get(b"IDAT", []))
    if b"tRNS" in chunks:
        raise UnsupportedError("PNG transparent color requires the core image backend")

    # 非透明样本分块验证，不保留整张解压图；PDF 仍透传原始 IDAT。
    if color_type in (0, 2, 3):
        colors = 3 if color_type == 2 else 1
        _validate_opaque(idat, (width * colors * bits + 7) // 8 + 1, height)
    if color_type == 0:  # 灰度
        return _PngImage(width, height, bits, 1, b"/DeviceGray", idat, True)

    if color_type == 2:  # RGB
        return _PngImage(width, height, bits, 3, b"/DeviceRGB", idat, True)

    if color_type == 3:  # 调色板
        plte = chunks.get(b"PLTE", [b""])[0]
        if not plte or len(plte) % 3 or len(plte) > 3 * (1 << bits):
            raise UnsupportedError("Invalid PNG palette")
        hival = len(plte) // 3 - 1
        cs = b"[/Indexed /DeviceRGB %d <%s>]" % (hival, plte.hex().upper().encode())
        return _PngImage(width, height, bits, 1, cs, idat, True)

    if color_type in (4, 6):  # 灰度+alpha / RGBA
        return _flatten_alpha_png(width, height, color_type, idat)

    raise UnsupportedError(f"不支持的 PNG 颜色类型：{color_type}")


def _validate_opaque(idat: bytes, stride: int, height: int) -> None:
    """Bound both compressed input slices and inflated blocks, including invalid streams."""
    expected = stride * height
    if expected > _DECODE_BUDGET:
        raise UnsupportedError("PNG exceeds the micro decode budget")
    decoder = zlib.decompressobj()
    position = total = 0
    pending = b""
    while position < len(idat) or pending:
        if not pending:
            pending = idat[position : position + _DECODE_BLOCK]
            position += len(pending)
        raw = decoder.decompress(pending, min(_DECODE_BLOCK, expected - total + 1))
        pending = decoder.unconsumed_tail
        if total + len(raw) > expected or decoder.unused_data:
            raise UnsupportedError("Invalid PNG compressed data length")
        if any(raw[i] > 4 for i in range((-total) % stride, len(raw), stride)):
            raise UnsupportedError("Invalid PNG scanline filter")
        total += len(raw)
        if decoder.eof:
            if position != len(idat) or pending:
                raise UnsupportedError("Trailing PNG compressed data")
            break
    if not decoder.eof or total != expected:
        raise UnsupportedError("Incomplete PNG compressed data")


def _unfilter(raw: bytes, width: int, height: int, bpp: int) -> bytearray:
    """PNG 反滤波。只在 alpha 合成这条慢路径上用到。"""
    stride = width * bpp
    if len(raw) != (stride + 1) * height:
        raise UnsupportedError("Invalid PNG scanline length")
    out = bytearray(stride * height)
    prev = bytearray(stride)
    pos = 0
    row = bytearray(stride)

    for y in range(height):
        ftype = raw[pos]
        pos += 1
        row[:] = raw[pos : pos + stride]
        pos += stride

        if ftype == 0:
            pass
        elif ftype == 1:
            for i in range(bpp, stride):
                row[i] = (row[i] + row[i - bpp]) & 0xFF
        elif ftype == 2:
            for i in range(stride):
                row[i] = (row[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(stride):
                a = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                a = row[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                row[i] = (row[i] + pr) & 0xFF
        else:
            raise UnsupportedError(f"未知的 PNG 滤波类型：{ftype}")

        out[y * stride : (y + 1) * stride] = row
        prev[:] = row

    return out


def _flatten_alpha_png(width: int, height: int, color_type: int, idat: bytes) -> _PngImage:
    """把 alpha 合成到白底，得到不透明的灰度或 RGB 样本。"""
    bpp = 2 if color_type == 4 else 4
    expected = (width * bpp + 1) * height
    if expected > _DECODE_BUDGET:
        raise UnsupportedError("Alpha PNG exceeds the micro decode budget")
    decoder = zlib.decompressobj()
    raw = decoder.decompress(idat, expected + 1)
    if not decoder.eof or decoder.unused_data or len(raw) != expected:
        raise UnsupportedError("Invalid PNG compressed data")
    samples = _unfilter(raw, width, height, bpp)

    if color_type == 4:
        out = bytearray(width * height)
        for i in range(width * height):
            v, a = samples[i * 2], samples[i * 2 + 1]
            out[i] = (v * a + 255 * (255 - a)) // 255
        colors, cs = 1, b"/DeviceGray"
    else:
        out = bytearray(width * height * 3)
        for i in range(width * height):
            r, g, b, a = samples[i * 4 : i * 4 + 4]
            inv = 255 - a
            out[i * 3] = (r * a + 255 * inv) // 255
            out[i * 3 + 1] = (g * a + 255 * inv) // 255
            out[i * 3 + 2] = (b * a + 255 * inv) // 255
        colors, cs = 3, b"/DeviceRGB"

    return _PngImage(width, height, 8, colors, cs, zlib.compress(bytes(out), 6), False)
