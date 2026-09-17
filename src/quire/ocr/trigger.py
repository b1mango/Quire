"""OCR 触发判定与正文图片收集：纯函数，无 I/O（项目设计.md §6.7）。

``auto`` 模式的两条触发规则：

1. **文字量 < 阈值 且 图片面积占比 > 40%**——无渲染拿不到真实排版面积，
   用「声明了宽高的图片像素面积 ÷（图片面积 + 文字估算面积）」近似，
   文字按 16px 字号折成 ``CHAR_AREA`` 平方像素/字；
2. **正文区本身是图片**——容器文字不足阈值、且含有未声明尺寸的内容图
   （扫描站最常见的样子：``<div id="content"><img src="1.jpg"></div>``）。

``--ocr always`` 强制、``never`` 关闭，判定本身不联网。
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from ..parse.article import best_container
from ..parse.images import LAZY_ATTRS, parse_srcset
from ..parse.minidom import Document, Node
from ..utils.urls import join_url

#: 文字量阈值：与正文质量校验的短文判据一致（项目设计.md §6.6）。
TEXT_THRESHOLD = 200

#: 图片面积占比阈值（项目设计.md §6.7）。
IMAGE_AREA_RATIO = 0.4

#: 单个字符的排版面积估算（16px 字号 × 16px 字宽）。
CHAR_AREA = 16 * 16

#: 声明尺寸小于该面积的图片视为图标/装饰，不参与触发判定。
MIN_CONTENT_AREA = 200 * 200

_SIZE_STYLE = re.compile(r"(?:^|;)\s*(width|height)\s*:\s*(\d+(?:\.\d+)?)px", re.IGNORECASE)


def _iter_images(node: Node) -> Iterator[Node]:
    for child in node.iter_elements():
        if child.tag == "img":
            yield child


def _declared_area(img: Node) -> int | None:
    """``width``/``height`` 属性或内联 style 声明的面积；缺任一维返回 None。"""
    width, height = img.get("width"), img.get("height")
    if not (width and height):
        style = img.get("style") or ""
        found = dict(_SIZE_STYLE.findall(style))
        width = width or found.get("width")
        height = height or found.get("height")
    try:
        area = int(float(width or "")) * int(float(height or ""))
    except ValueError:
        return None
    return area or None


def _resolve_image(img: Node, base_url: str) -> str | None:
    """按「srcset → 懒加载属性 → src」的可信顺序取图片地址。"""
    srcset = img.get("data-srcset") or img.get("srcset") or ""
    if srcset:
        entries = parse_srcset(srcset)
        if entries:
            resolved = join_url(base_url, max(entries, key=lambda e: e[1])[0])
            if resolved:
                return resolved
    for attr in LAZY_ATTRS:
        raw = img.get(attr)
        if raw:
            resolved = join_url(base_url, raw)
            if resolved:
                return resolved
    return None


def content_node(doc: Document, selector: str | None) -> Node | None:
    """定位正文区：显式选择器优先，其次正文评分，最后语义容器兜底。"""
    if selector:
        return doc.select_one(selector)
    node = best_container(doc, min_chars=1)
    if node is not None:
        return node
    return doc.select_one("main") or doc.select_one("article") or doc.select_one("body")


def image_urls(node: Node, base_url: str) -> list[str]:
    """按 DOM 序收集正文区内的图片地址（去重，保持首次出现顺序）。"""
    out: list[str] = []
    seen: set[str] = set()
    for img in _iter_images(node):
        url = _resolve_image(img, base_url)
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def should_ocr(doc: Document, selector: str | None = None, *, mode: str = "auto") -> bool:
    """这一页是否需要 OCR。``mode`` 为 ``auto|always|never``（项目设计.md §6.7）。"""
    if mode == "always":
        return True
    if mode == "never":
        return False
    node = content_node(doc, selector)
    if node is None:
        return False
    chars = len(re.sub(r"\s+", "", node.text))
    if chars >= TEXT_THRESHOLD:
        return False
    area = 0
    undimensioned = False
    for img in _iter_images(node):
        declared = _declared_area(img)
        if declared is None:
            undimensioned = True
        elif declared >= MIN_CONTENT_AREA:
            area += declared
    if undimensioned and image_urls(node, doc.base_url or ""):
        # 正文区本身是图片：文字不足阈值，图片连尺寸都不写——典型的扫描正文。
        return True
    return area > 0 and area / (area + max(chars, 1) * CHAR_AREA) > IMAGE_AREA_RATIO
