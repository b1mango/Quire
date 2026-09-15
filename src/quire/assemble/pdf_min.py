"""Streaming micro PDF writer: JPEG/PNG embedding, outlines and missing pages.

JPEG and opaque PNG samples are embedded without transcoding. Alpha PNG
samples are flattened to white by the bounded decoder in png.py.
"""

from __future__ import annotations

import sys
import zlib
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from ..errors import UnsupportedError
from ..image.probe import probe_bytes
from ..workspace import atomic_output
from .pdf_text import _pdf_date, pdf_text, placeholder_content
from .png import _png_to_pdf_image

__all__ = ["MiniPdfWriter", "PaperSize", "PAGE_SIZES"]

#: 常见纸张（宽, 高），单位 pt。
PaperSize = tuple[float, float]
PAGE_SIZES: dict[str, PaperSize] = {
    "a5": (419.53, 595.28),
    "a4": (595.28, 841.89),
    "b5": (498.90, 708.66),
    "letter": (612.0, 792.0),
}

_PDF_HEADER = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"


# ============================================================ 写入器


@dataclass
class _PendingPage:
    number: int
    content_number: int
    image_number: int | None
    width_pt: float
    height_pt: float
    title: str = ""


class MiniPdfWriter:
    """顺序写 PDF，边写边落盘，不把整本书攒在内存里（项目设计.md §8）。

    用法::

        with MiniPdfWriter("out.pdf", title="海贼王") as pdf:
            pdf.add_image_file(Path("001.jpg"))
            pdf.add_placeholder(["MISSING PAGE 2"])
    """

    def __init__(
        self,
        path: Path | str,
        *,
        title: str = "",
        author: str = "",
        producer: str = "Quire",
        dpi: int = 150,
        paper: str | PaperSize = "original",
        fit: str = "contain",
        margin_mm: float = 0.0,
        overwrite: bool = False,
    ) -> None:
        self.path = Path(path)
        self.title = title
        self.author = author
        self.producer = producer
        self.dpi = dpi
        self.paper = paper
        self.fit = fit
        self.margin_pt = margin_mm * 72.0 / 25.4

        self._fh: IO[bytes] | None = None
        self._transaction: AbstractContextManager[IO[bytes]] = atomic_output(
            self.path, overwrite=overwrite
        )
        self._offsets: dict[int, int] = {}
        self._next_number = 4  # 1=Catalog 2=Pages 3=Info
        self._pages: list[_PendingPage] = []
        self._outline: list[tuple[str, int]] = []
        self._bytes_written = 0
        self._closed = False

    # -------------------------------------------------- 上下文管理
    def __enter__(self) -> MiniPdfWriter:
        self._fh = self._transaction.__enter__()
        try:
            self._raw(_PDF_HEADER)
            fields = (
                (b"Title", self.title),
                (b"Author", self.author),
                (b"Producer", self.producer),
            )
            info = b" ".join(b"/" + key + b" " + pdf_text(value) for key, value in fields)
            self._write_object(
                3, b"<< " + info + b" /CreationDate (" + _pdf_date().encode() + b") >>"
            )
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            if exc[0] is None:
                self.close()
            else:
                self._transaction.__exit__(*exc)  # type: ignore[arg-type]
        except BaseException:
            self._transaction.__exit__(*sys.exc_info())
            raise
        finally:
            self._fh = None

    # -------------------------------------------------- 底层写入
    def _raw(self, data: bytes) -> int:
        assert self._fh is not None
        offset = self._bytes_written
        self._fh.write(data)
        self._bytes_written += len(data)
        return offset

    def _write_object(self, number: int, body: bytes) -> int:
        offset = self._raw(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
        self._offsets[number] = offset
        return offset

    def _write_stream_object(self, number: int, header: bytes, payload: bytes) -> None:
        """``header`` 是字典内容（可带可不带收尾的 ``>>``），本函数负责补 ``/Length``。

        收尾的 ``>>`` 统一由这里补——曾经因为调用方多写了一个 ``>>``，
        产生了 ``... /Filter /DCTDecode >> /Length 269 >>`` 这种把 /Length
        甩到字典外的畸形对象，阅读器能打开但取不到图。
        """
        body = header.rstrip()
        if body.endswith(b">>"):
            body = body[:-2].rstrip()

        offset = self._raw(
            f"{number} 0 obj\n".encode()
            + body
            + b" /Length "
            + str(len(payload)).encode()
            + b" >>\nstream\n"
        )
        self._raw(payload)
        self._raw(b"\nendstream\nendobj\n")
        self._offsets[number] = offset

    def _alloc(self) -> int:
        n = self._next_number
        self._next_number += 1
        return n

    # -------------------------------------------------- 版面计算
    def _paper_size(self) -> PaperSize:
        if isinstance(self.paper, str):
            return PAGE_SIZES.get(self.paper, PAGE_SIZES["a4"])
        return self.paper

    def _layout(self, iw: int, ih: int) -> tuple[float, float, float, float]:
        """返回 ``(页宽, 页高, 图宽, 图高)``，单位 pt。

        ``original`` 时页面严丝合缝地等于图像——漫画阅读体验最好的默认值，
        也没有任何重采样。
        """
        if iw <= 0 or ih <= 0:
            iw = ih = 1

        if self.paper == "original":
            scale = 72.0 / self.dpi
            return iw * scale, ih * scale, iw * scale, ih * scale

        page_w, page_h = self._paper_size()
        avail_w = max(page_w - 2 * self.margin_pt, 1.0)
        avail_h = max(page_h - 2 * self.margin_pt, 1.0)

        if self.fit == "width":
            s = avail_w / iw
        elif self.fit == "height":
            s = avail_h / ih
        else:  # contain
            s = min(avail_w / iw, avail_h / ih)
        return page_w, page_h, iw * s, ih * s

    def _draw_image(self, page_w: float, page_h: float, draw_w: float, draw_h: float) -> bytes:
        x = (page_w - draw_w) / 2
        y = (page_h - draw_h) / 2
        return f"q\n{draw_w:.2f} 0 0 {draw_h:.2f} {x:.2f} {y:.2f} cm\n/Im0 Do\nQ\n".encode()

    def _add_page(
        self,
        width_pt: float,
        height_pt: float,
        draw: bytes,
        image: tuple[int, bytes, bytes] | None,
        title: str = "",
    ) -> None:
        """``image`` 为 ``(对象号, header, payload)``；``draw`` 是内容流。"""
        page_no = self._alloc()
        content_no = self._alloc()
        image_no = None

        if image is not None:
            image_no, header, payload = image
            self._write_stream_object(image_no, header, payload)
            resources = f"<< /XObject << /Im0 {image_no} 0 R >> >>".encode()
        else:
            resources = (
                b"<< /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >>"
            )

        self._write_stream_object(content_no, b"<<", draw)
        self._write_object(
            page_no,
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width_pt:.2f} {height_pt:.2f}] "
                f"/Resources {resources.decode()} /Contents {content_no} 0 R >>"
            ).encode(),
        )
        self._pages.append(_PendingPage(page_no, content_no, image_no, width_pt, height_pt, title))
        if title:
            self._outline.append((title, page_no))

    # -------------------------------------------------- 公开 API
    def add_image_file(self, path: Path | str, *, title: str = "") -> bool:
        """加一页图片。返回 False 表示该图不受支持（调用方应改用占位页）。"""
        data = Path(path).read_bytes()
        return self.add_image_bytes(data, title=title)

    def add_image_bytes(self, data: bytes, *, title: str = "") -> bool:
        probe = probe_bytes(data)
        if not probe.ok:
            return False

        if probe.format == "jpeg":
            channels = probe.channels or 3
            cs = {1: b"/DeviceGray", 3: b"/DeviceRGB", 4: b"/DeviceCMYK"}.get(
                channels, b"/DeviceRGB"
            )
            header = (
                b"<< /Type /XObject /Subtype /Image"
                + f" /Width {probe.width} /Height {probe.height}".encode()
                + b" /ColorSpace "
                + cs
                + f" /BitsPerComponent {probe.bits or 8}".encode()
                + b" /Filter /DCTDecode >>"
            )
            if channels == 4 and probe.adobe_transform in (0, 2):
                header = header[:-2] + b" /Decode [1 0 1 0 1 0 1 0] >>"
            payload = data
        elif probe.format == "png":
            try:
                png = _png_to_pdf_image(data)
            except (UnsupportedError, zlib.error, IndexError, ValueError):
                return False
            header = (
                b"<< /Type /XObject /Subtype /Image"
                + f" /Width {png.width} /Height {png.height}".encode()
                + b" /ColorSpace "
                + png.color_space
                + f" /BitsPerComponent {png.bits}".encode()
                + b" /Filter /FlateDecode"
            )
            if png.pre_compressed:
                # IDAT 原样透传：数据里带着 PNG 的逐行滤波字节，
                # 正好对应 PDF 的 Predictor 15（= PNG optimum predictor）。
                header += (
                    f" /DecodeParms << /Predictor 15 /Colors {png.colors} "
                    f"/BitsPerComponent {png.bits} /Columns {png.width} >>"
                ).encode()
            # 反滤波后重新压缩的数据（alpha 合成路径）没有逐行滤波字节，
            # 绝不能声明 Predictor——声明了会让解码器把首字节当成滤波类型。
            header += b" >>"
            payload = png.data
        else:
            return False

        page_w, page_h, draw_w, draw_h = self._layout(probe.width, probe.height)
        draw = self._draw_image(page_w, page_h, draw_w, draw_h)
        image_no = self._alloc()
        self._add_page(page_w, page_h, draw, (image_no, header, payload), title)
        return True

    def add_placeholder(self, lines: list[str]) -> None:
        """缺页占位页（决策 H）。

        正文走 base-14 的 Helvetica，所以内容必须能转成 ASCII——
        中文走不了这条路径，因此 ``lines`` 由调用方组装成英文/URL 形式。
        """
        if self.paper == "original":
            page_w, page_h = PAGE_SIZES["a4"]
        else:
            page_w, page_h = self._paper_size()
        draw = placeholder_content(lines, page_w, page_h)
        self._add_page(page_w, page_h, draw, None)

    # -------------------------------------------------- 收尾
    def close(self) -> None:
        if self._closed or self._fh is None:
            return
        self._closed = True

        # 顺序：先写大纲（要分配对象号），再写引用它的 Catalog，最后写 Pages。
        # 对象在文件里的物理顺序不影响正确性——xref 记录的是真实偏移。
        outline_root = self._write_outline() if self._outline else None

        catalog = b"<< /Type /Catalog /Pages 2 0 R"
        if outline_root is not None:
            catalog += f" /Outlines {outline_root} 0 R".encode()
        catalog += b" >>"
        self._write_object(1, catalog)

        kids = " ".join(f"{p.number} 0 R" for p in self._pages)
        self._write_object(
            2, f"<< /Type /Pages /Kids [{kids}] /Count {len(self._pages)} >>".encode()
        )

        # 交叉引用表：必须按对象号升序，且每个对象都已写过
        max_no = max(self._offsets) if self._offsets else 3
        xref_offset = self._bytes_written
        rows = [b"xref\n", f"0 {max_no + 1}\n".encode(), b"0000000000 65535 f \n"]
        for i in range(1, max_no + 1):
            off = self._offsets.get(i, 0)
            rows.append(f"{off:010d} 00000 n \n".encode())
        self._raw(b"".join(rows))

        trailer = (
            f"trailer\n<< /Size {max_no + 1} /Root 1 0 R /Info 3 0 R >>\n".encode()
            + b"startxref\n"
            + str(xref_offset).encode()
            + b"\n%%EOF\n"
        )
        self._raw(trailer)
        self._transaction.__exit__(None, None, None)
        self._fh = None

    def _write_outline(self) -> int:
        """扁平的章节目录。层级留到 core 档再做。"""
        root_no = self._alloc()
        item_numbers = [self._alloc() for _ in self._outline]
        parent = f"{root_no} 0 R"

        for idx, ((title, page_no), item_no) in enumerate(
            zip(self._outline, item_numbers, strict=True)
        ):
            body = (
                b"<< /Title "
                + pdf_text(title)
                + b" /Parent "
                + parent.encode()
                + f" /Dest [{page_no} 0 R /Fit]".encode()
            )
            if idx > 0:
                body += f" /Prev {item_numbers[idx - 1]} 0 R".encode()
            if idx < len(item_numbers) - 1:
                body += f" /Next {item_numbers[idx + 1]} 0 R".encode()
            self._write_object(item_no, body + b" >>")

        self._write_object(
            root_no,
            f"<< /Type /Outlines /First {item_numbers[0]} 0 R "
            f"/Last {item_numbers[-1]} 0 R /Count {len(item_numbers)} >>".encode(),
        )
        return root_no

    # -------------------------------------------------- 统计
    @property
    def page_count(self) -> int:
        return len(self._pages)

    @property
    def size_bytes(self) -> int:
        return self._bytes_written


def write_images_to_pdf(
    images: list[Path],
    out: Path | str,
    *,
    title: str = "",
    dpi: int = 150,
    paper: str = "original",
) -> int:
    """便捷入口：一串图片 → 一个 PDF。返回实际写入的页数。"""
    written = 0
    with MiniPdfWriter(out, title=title, dpi=dpi, paper=paper) as pdf:
        for img in images:
            if pdf.add_image_file(img):
                written += 1
    return written
