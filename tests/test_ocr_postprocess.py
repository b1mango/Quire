"""OCR 后处理：行合并、标点归一化、误识修正、跨页去重、复核清单。"""

from __future__ import annotations

from quire.ocr.base import OcrLine, ReviewEntry
from quire.ocr.postprocess import (
    collect_review,
    drop_repeated_short_lines,
    fix_misread,
    merge_lines,
    normalize_paragraphs,
)


def line(text: str, x0: int = 0, x1: int = 400, confidence: float = 0.9) -> OcrLine:
    return OcrLine(text, confidence, x0, 0, x1, 10)


def test_merge_continues_full_width_lines() -> None:
    """上一行未以终止标点结尾且本行满宽：同一段续行，拼接。"""
    merged = merge_lines([line("他抬头看见远山如黛"), line("风从林间穿过，")])
    assert merged == ("他抬头看见远山如黛风从林间穿过，",)


def test_merge_breaks_on_terminal_punctuation() -> None:
    merged = merge_lines([line("第一段说完了。"), line("第二段开始，")])
    assert merged == ("第一段说完了。", "第二段开始，")


def test_merge_breaks_on_indent() -> None:
    """首行缩进约 2 字符（相对中位字宽）即另起一段。"""
    lines = [line("满宽的一行没有说完", x0=0, x1=400), line("缩进的新段，", x0=80, x1=380)]
    assert merge_lines(lines) == ("满宽的一行没有说完", "缩进的新段，")


def test_merge_short_line_after_unfinished_starts_new_paragraph() -> None:
    """下行不满宽又不缩进：无法判断归属时宁多分段不错拼。"""
    lines = [line("满宽的一行没有说完，", x0=0, x1=400), line("短行乙，", x0=0, x1=200)]
    assert merge_lines(lines) == ("满宽的一行没有说完，", "短行乙，")


def test_merge_skips_blank_lines() -> None:
    assert merge_lines([line(""), line("  "), line("正文。")]) == ("正文。",)
    assert merge_lines([]) == ()


def test_merge_without_coordinates_still_works() -> None:
    """没有坐标信息（x1<=x0）时按文本规则合并，不崩溃。"""
    merged = merge_lines([OcrLine("没有坐标的一行，", 0.9), OcrLine("下一句。", 0.9)])
    assert merged == ("没有坐标的一行，下一句。",)


def test_fix_misread_is_conservative() -> None:
    assert fix_misread("他说｡走吧､") == "他说。走吧、"
    assert fix_misread("正常的中文。") == "正常的中文。"


def test_normalize_paragraphs_applies_text_rules() -> None:
    """复用正文清洗：半角转全角、省略号、去多余空格，空段丢弃。"""
    normalized = normalize_paragraphs(["他说:走吧...", "   ", "中英 之间"])
    assert normalized == ("他说：走吧……", "中英之间")


def test_drop_repeated_short_lines_removes_headers() -> None:
    """整书超过 60% 的章都出现的短行是页眉页脚，删掉。"""
    chapters = [
        ("页眉书名", "第一章正文。"),
        ("页眉书名", "第二章正文。"),
        ("页眉书名", "第三章正文。"),
        ("独有短行", "第四章正文。"),
    ]
    result = drop_repeated_short_lines(chapters)
    assert result[0] == ("第一章正文。",)
    assert result[3] == ("独有短行", "第四章正文。")


def test_drop_repeated_short_lines_keeps_small_books_and_long_lines() -> None:
    two = [("页眉", "正文。"), ("页眉", "正文。")]
    assert drop_repeated_short_lines(two) == two
    long_line = "重复出现但长度明显超过三十个字符的行不会被当成页眉页脚处理掉。"
    chapters = [(long_line,), (long_line,), (long_line,)]
    assert drop_repeated_short_lines(chapters) == chapters


def test_collect_review_low_confidence_only() -> None:
    lines = [line("清楚的一行。", confidence=0.95), line("模糊的一行。", confidence=0.42)]
    entries = collect_review(lines, chapter=3)
    assert entries == [ReviewEntry(3, "模糊的一行。", 0.42)]
    assert collect_review([line("", confidence=0.1)], chapter=1) == []
