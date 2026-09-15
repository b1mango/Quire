"""纯 Python 图片探测：不依赖 Pillow，只读文件头尾。

存在的意义是**成功率**：漫画站最讨厌的失败不是 404，而是
"HTTP 200 + 一张下了一半的图"。那种图不报错，直接进 PDF，
等用户翻到那一页才发现花了半页。

本模块做三件事：
  1. 从头部解析出格式、宽高、通道数、是否有 alpha；
  2. 检查 JPEG 的 SOF/SOS 和尾部标记；结构通过不代表像素解码完整；
  3. 全部只用标准库，因此 ``micro`` 档也能用它守住质量。

效率：:func:`probe_file` 只 seek 读头 64 KB 与尾 4 KB，
不为校验一张 2 MB 的图把整个文件读进内存（项目设计.md §8 内存铁律）。
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

__all__ = ["ImageProbe", "probe_bytes", "probe_file", "sniff_format"]

#: 读头部这么多字节足够解析所有支持格式的头。
HEAD_BYTES = 64 * 1024
#: 尾部窗口：有些 JPEG 会带少量尾随数据，所以在窗口内搜索 EOI 而不是只看最后两字节。
TAIL_BYTES = 4096

JPEG_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
JPEG_STANDALONE = {0x01, *range(0xD0, 0xD8)}  # TEM + RST0..RST7，没有长度字段


@dataclass(frozen=True, slots=True)
class ImageProbe:
    """探测结果。``width``/``height`` 为 0 表示没解析出来。"""

    format: str = ""
    width: int = 0
    height: int = 0
    channels: int = 0
    bits: int = 0
    has_alpha: bool = False
    complete: bool = False
    error: str = ""
    adobe_transform: int | None = None

    @property
    def ok(self) -> bool:
        """尺寸及结构检查通过；不保证像素解码或 PDF 后端支持。"""
        return bool(self.format) and self.width > 0 and self.height > 0 and self.complete

    @property
    def megapixels(self) -> float:
        return self.width * self.height / 1_000_000

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 0.0


def sniff_format(data: bytes) -> str:
    """只认格式，不做完整解析。用于给下载内容挑扩展名。"""
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:2] == b"BM":
        return "bmp"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return "avif"
    if data[:2] == b"\xff\x0a":
        return "jxl"
    return ""


def probe_bytes(data: bytes) -> ImageProbe:
    """从完整字节流探测。"""
    fmt = sniff_format(data)
    if not fmt:
        return ImageProbe(error="无法识别的图片格式")

    return probe_bytes_from_parts(data, data[-TAIL_BYTES:], len(data))


def probe_file(path: Path | str) -> ImageProbe:
    """从磁盘探测，只读头尾（项目设计.md §8 内存铁律）。"""
    p = Path(path)
    try:
        size = p.stat().st_size
    except OSError as exc:
        return ImageProbe(error=f"读不到文件：{exc}")

    if size < 16:
        return ImageProbe(error=f"文件过小（{size} 字节），几乎肯定是坏的")

    try:
        with p.open("rb") as fh:
            head = fh.read(HEAD_BYTES)
            if size > TAIL_BYTES:
                fh.seek(-TAIL_BYTES, 2)
                tail = fh.read(TAIL_BYTES)
            else:
                tail = head
    except OSError as exc:
        return ImageProbe(error=f"读取失败：{exc}")

    probe = probe_bytes_from_parts(head, tail, size)
    return probe


def probe_bytes_from_parts(head: bytes, tail: bytes, size: int) -> ImageProbe:
    """头/尾分开的场景（下载时边写边校验）。"""
    fmt = sniff_format(head)
    if not fmt:
        return ImageProbe(error="无法识别的图片格式")
    if fmt == "jpeg":
        return _probe_jpeg(head, tail if len(head) < size else None)
    if fmt == "png":
        return _probe_png(head, tail if len(head) < size else None)
    if fmt == "gif":
        return _probe_gif(head, tail)
    if fmt == "webp":
        return _probe_webp(head, tail, size)
    if fmt == "bmp":
        return _probe_bmp(head, size)
    return ImageProbe(format=fmt, complete=True)


# ============================================================ 各格式


def _probe_jpeg(data: bytes, tail: bytes | None = None) -> ImageProbe:
    """Validate headers through the first SOS and an EOI, without decoding entropy data."""
    invalid = ImageProbe(format="jpeg", error="Invalid or unsupported JPEG structure")
    i = 2  # 跳过 SOI
    width = height = channels = bits = 0
    component_ids: set[int] = set()
    adobe_transform = None
    n = len(data)
    while i < n - 1:
        if data[i] != 0xFF:
            return invalid
        marker = data[i + 1]
        if marker == 0xFF:  # 填充字节
            i += 1
            continue
        if marker in {0x00, 0xD8, 0xD9, *JPEG_STANDALONE} or i + 4 > n:
            return invalid
        seg_len = struct.unpack_from(">H", data, i + 2)[0]
        end = i + 2 + seg_len
        if seg_len < 2 or end > n:
            return invalid
        if marker in JPEG_SOF:
            if marker not in (0xC0, 0xC1, 0xC2) or seg_len < 8 or component_ids:
                return invalid
            bits = data[i + 4]
            height = struct.unpack_from(">H", data, i + 5)[0]
            width = struct.unpack_from(">H", data, i + 7)[0]
            channels = data[i + 9]
            component_ids = set(data[i + 10 : end : 3])
            if (
                bits != 8
                or channels not in (1, 3, 4)
                or not width
                or not height
                or seg_len != 8 + 3 * channels
                or len(component_ids) != channels
            ):
                return invalid
        elif marker == 0xEE and data[i + 4 : i + 9] == b"Adobe":
            if seg_len < 14:
                return invalid
            adobe_transform = data[i + 15]
        elif marker == 0xDA:
            if seg_len < 6 or not component_ids:
                return invalid
            count = data[i + 4]
            scan_ids = set(data[i + 5 : end - 3 : 2])
            if (
                not 1 <= count <= channels
                or seg_len != 6 + 2 * count
                or len(scan_ids) != count
                or not scan_ids <= component_ids
            ):
                return invalid
            complete = _jpeg_has_eoi(data, end) if tail is None else _jpeg_has_eoi(tail)
            return ImageProbe(
                format="jpeg",
                width=width,
                height=height,
                channels=channels,
                bits=bits,
                complete=complete,
                adobe_transform=adobe_transform,
                error="" if complete else "Missing JPEG scan data or EOI",
            )
        i = end
    return invalid


def _jpeg_has_eoi(tail: bytes, scan_start: int = -1) -> bool:
    """在尾部窗口里搜 FFD9。

    不能只看最后两字节：不少站点会在 JPEG 后面追加元数据或换行。
    """
    idx = tail.rfind(b"\xff\xd9")
    return idx > scan_start


def _probe_png(data: bytes, tail: bytes | None = None) -> ImageProbe:
    if len(data) < 33:
        return ImageProbe(format="png", error="数据过短")
    if data[12:16] != b"IHDR":
        return ImageProbe(format="png", error="第一个 chunk 不是 IHDR")

    width, height = struct.unpack_from(">II", data, 16)
    bits = data[24]
    color_type = data[25]
    interlace = data[28] if len(data) > 28 else 0

    channels, has_alpha = {
        0: (1, False),  # 灰度
        2: (3, False),  # RGB
        3: (1, False),  # 调色板（alpha 在 tRNS 里，单独判）
        4: (2, True),  # 灰度 + alpha
        6: (4, True),  # RGBA
    }.get(color_type, (0, False))

    window = data if tail is None else tail
    complete = window.endswith(b"\x00\x00\x00\x00IEND\xaeB\x60\x82")
    if tail is None and complete:
        position = 8
        found_idat = False
        while position + 12 <= len(data):
            length = int.from_bytes(data[position : position + 4], "big")
            end = position + 12 + length
            if end > len(data):
                complete = False
                break
            chunk = data[position + 4 : end - 4]
            checksum = int.from_bytes(data[end - 4 : end], "big")
            if zlib.crc32(chunk) != checksum:
                complete = False
                break
            found_idat = found_idat or chunk[:4] == b"IDAT"
            position = end
        complete = complete and found_idat and position == len(data)
    complete = complete and width > 0 and height > 0 and channels > 0

    err = ""
    if interlace:
        err = "Adam7 隔行 PNG，micro 档不支持"
    elif not complete:
        err = "PNG 结构、CRC 或结束标记不完整"

    return ImageProbe(
        format="png",
        width=width,
        height=height,
        channels=channels,
        bits=bits,
        has_alpha=has_alpha,
        complete=complete,
        error=err,
    )


def _probe_gif(data: bytes, tail: bytes | None = None) -> ImageProbe:
    if len(data) < 13:
        return ImageProbe(format="gif", error="数据过短")
    width, height = struct.unpack_from("<HH", data, 6)
    window = data if tail is None else tail
    complete = window.rstrip(b"\r\n\x00").endswith(b"\x3b")
    return ImageProbe(
        format="gif",
        width=width,
        height=height,
        channels=3,
        bits=8,
        complete=complete,
        error="" if complete else "缺少 GIF 结束标记（文件被截断）",
    )


def _probe_webp(data: bytes, tail: bytes | None = None, size: int = 0) -> ImageProbe:
    if len(data) < 30:
        return ImageProbe(format="webp", error="数据过短")

    riff_size = struct.unpack_from("<I", data, 4)[0]
    if size:
        # RIFF 声明的长度应当等于文件长度 - 8
        complete = (riff_size + 8) <= size
    else:
        complete = (riff_size + 8) <= len(data)

    chunk = data[12:16]
    width = height = 0
    has_alpha = False

    if chunk == b"VP8 ":
        # 有损：frame tag(3) + sync code(3) + 尺寸
        if data[23:26] == b"\x9d\x01\x2a":
            width = struct.unpack_from("<H", data, 26)[0] & 0x3FFF
            height = struct.unpack_from("<H", data, 28)[0] & 0x3FFF
    elif chunk == b"VP8L":
        if len(data) > 25 and data[20] == 0x2F:
            bits = struct.unpack_from("<I", data, 21)[0]
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
            has_alpha = bool((bits >> 28) & 1)
    elif chunk == b"VP8X":
        flags = data[20]
        has_alpha = bool(flags & 0x10)
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1

    err = ""
    if width <= 0 or height <= 0:
        err = "无法解析 WebP 尺寸"
    elif not complete:
        err = "RIFF 长度与文件不符（文件被截断）"

    return ImageProbe(
        format="webp",
        width=width,
        height=height,
        channels=4 if has_alpha else 3,
        bits=8,
        has_alpha=has_alpha,
        complete=complete and width > 0,
        error=err,
    )


def _probe_bmp(data: bytes, size: int = 0) -> ImageProbe:
    if len(data) < 26:
        return ImageProbe(format="bmp", error="数据过短")
    header_size = struct.unpack_from("<I", data, 14)[0]
    if header_size == 12:  # BITMAPCOREHEADER
        width, height = struct.unpack_from("<HH", data, 18)
    else:
        width, height = struct.unpack_from("<ii", data, 18)
        height = abs(height)
    if width <= 0 or height <= 0:
        return ImageProbe(format="bmp", error="无法解析 BMP 尺寸")
    declared = struct.unpack_from("<I", data, 2)[0]
    complete = declared <= (size or len(data))
    return ImageProbe(
        format="bmp",
        width=width,
        height=height,
        channels=3,
        bits=8,
        complete=complete,
        error="" if complete else "文件头声明长度大于实际（文件被截断）",
    )
