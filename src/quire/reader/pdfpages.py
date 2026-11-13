"""图片型 PDF 当漫画读：每页只有一张图、没有文字时，按页取出原图交给漫画阅读器。

参照 Mihon / Kavita / Panels 等漫画阅读器：漫画 PDF 本质是图片序列，交给系统 PDF 视图会按
「整页适配」缩成窄条，翻页、条漫连读和进度都做不好。卷帙导出的漫画 PDF 每页嵌一张 JPEG，
取出时不重新编码。带文字的 PDF 仍交给系统 PDF 视图。
"""

from __future__ import annotations

import statistics
from typing import BinaryIO

from pypdf import PdfReader
from pypdf.errors import PyPdfError
from pypdf.generic import DictionaryObject

_SAMPLE = 24
_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".jp2": "image/jp2"}


def _resources(page: DictionaryObject) -> DictionaryObject:
    res = page.get("/Resources")
    return res.get_object() if res is not None else DictionaryObject()


def _image_only(page: DictionaryObject) -> bool:
    res = _resources(page)
    if "/Font" in res:
        return False
    xobjects = res.get("/XObject")
    if xobjects is None:
        return False
    kinds = [x.get_object().get("/Subtype") for x in xobjects.get_object().values()]
    return kinds == ["/Image"]


def inspect(stream: BinaryIO) -> tuple[int, bool]:
    """(图片页数, 是否条漫)。抽样页里有任何一页不是单图页就返回 (0, False)：按普通 PDF 处理。"""
    try:
        return _inspect(stream)
    except (PyPdfError, ValueError, KeyError, AttributeError):
        return 0, False  # 解析不了的 PDF 交给系统 PDF 视图


def _inspect(stream: BinaryIO) -> tuple[int, bool]:
    reader = PdfReader(stream)
    total = len(reader.pages)
    if total == 0:
        return 0, False
    picks = sorted({*range(min(total, _SAMPLE)), total // 2, total - 1})
    ratios: list[float] = []
    for index in picks:
        page = reader.pages[index]
        if not _image_only(page):
            return 0, False
        box = page.mediabox
        if float(box.width) > 0:
            ratios.append(float(box.height) / float(box.width))
    # 高宽比中位数 > 1.8：条漫切片（普通漫画单页约 1.4），默认上下连读
    return total, bool(ratios) and statistics.median(ratios) > 1.8


def read_page(stream: BinaryIO, index: int) -> tuple[bytes, str]:
    try:
        return _read_page(stream, index)
    except PyPdfError as exc:
        raise ValueError("PDF 页面读取失败") from exc


def _read_page(stream: BinaryIO, index: int) -> tuple[bytes, str]:
    reader = PdfReader(stream)
    if index < 0 or index >= len(reader.pages):
        raise IndexError("没有这一页")
    images = reader.pages[index].images
    if len(images) != 1:
        raise ValueError("这一页不是单张图片")
    image = images[0]
    suffix = "." + image.name.rsplit(".", 1)[-1].lower() if "." in image.name else ".png"
    return image.data, _TYPES.get(suffix, "image/png")
