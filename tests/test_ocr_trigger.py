"""OCR 触发判定与正文图片收集：纯函数测试（项目设计.md §6.7）。"""

from __future__ import annotations

from quire.ocr.trigger import content_node, image_urls, should_ocr
from quire.parse.minidom import parse

BASE = "http://example.test/book/1.html"
LONG_TEXT = "他抬头看见远山如黛，风从林间穿过。" * 20  # 远超 200 字阈值


def doc(body: str) -> object:
    return parse(f"<html><body><div id='content'>{body}</div></body></html>", base_url=BASE)


def test_always_forces_and_never_disables() -> None:
    page = doc("<img src='1.jpg'>")
    assert should_ocr(page, mode="always") is True
    assert should_ocr(doc(f"<p>{LONG_TEXT}</p>"), mode="always") is True
    assert should_ocr(page, mode="never") is False


def test_long_text_never_triggers() -> None:
    page = doc(f"<p>{LONG_TEXT}</p><img src='1.jpg'>")
    assert should_ocr(page) is False


def test_undimensioned_content_image_triggers() -> None:
    """正文区本身是图片：文字不足阈值且图片连尺寸都不写（扫描站典型）。"""
    page = doc("<img src='1.jpg'>")
    assert should_ocr(page) is True


def test_declared_large_image_triggers_by_area() -> None:
    page = doc("<p>短短一句。</p><img src='1.jpg' width='800' height='1200'>")
    assert should_ocr(page) is True


def test_style_declared_size_counts() -> None:
    page = doc("<p>短。</p><img src='1.jpg' style='width: 800px; height: 1200px'>")
    assert should_ocr(page) is True


def test_icon_sized_images_do_not_trigger() -> None:
    """只有小尺寸装饰图、文字又不足阈值的页面不 OCR（避免对导航页空转）。"""
    page = doc("<p>只有几个字。</p><img src='icon.png' width='64' height='64'>")
    assert should_ocr(page) is False


def test_short_text_without_images_does_not_trigger() -> None:
    assert should_ocr(doc("<p>只有几个字。</p>")) is False


def test_explicit_selector_scopes_decision() -> None:
    page = parse(
        "<html><body><div id='side'><img src='ad.jpg'></div>"
        f"<div id='content'><p>{LONG_TEXT}</p></div></body></html>",
        base_url=BASE,
    )
    assert should_ocr(page, "#content") is False
    assert should_ocr(page, "#side") is True


def test_content_node_falls_back_to_body() -> None:
    page = parse("<html><body>正文</body></html>", base_url=BASE)
    assert content_node(page, None) is not None
    assert content_node(page, "#missing") is None


def test_image_urls_resolution_order_and_dedup() -> None:
    page = doc(
        "<img srcset='small.jpg 1x, big.jpg 2x'>"
        "<img data-src='lazy.jpg' src='placeholder.gif'>"
        "<img src='plain.jpg'>"
        "<img src='plain.jpg'>"
    )
    node = content_node(page, "#content")
    assert node is not None
    assert image_urls(node, BASE) == [
        "http://example.test/book/big.jpg",
        "http://example.test/book/lazy.jpg",
        "http://example.test/book/plain.jpg",
    ]
