from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from quire import novel_export
from quire.assemble.models import NovelChapter
from quire.core_chapter_cache import decode_chapter, removed_entries
from quire.core_novel import export_chapters
from quire.errors import FetchError, LedgerError
from quire.export_commit import publish
from quire.models import NovelResult
from quire.novel_export import export_novel, preflight, report_payload, review_text
from quire.ocr.base import ReviewEntry
from quire.text.clean import clean_paragraphs, normalize_line
from quire.text.noise import URL_REVIEW_RULE


@pytest.mark.parametrize(
    "line",
    [
        "天才一秒记住本站地址：www.biquge-fake.cc",
        "最新章节请到ｗｗｗ．ｂｉｑｕｇｅ．ｃｏｍ阅读",
        "看最新章节请到【笔趣阁】",
        "手机用户请访问 m.biquge-fake.cc 阅读，更优质的阅读体验",
        "手机用户请浏览 m.example.com 阅读",
        "更多精彩小说，尽在某某小说网 www.example.com",
        "本书由【某某整理组】整理，版权归作者所有，请勿用作商业用途",
        "如果觉得本书不错，请收藏本站，加入书签，方便下次阅读",
        "百度搜索 青崖剑录 八一中文网 即可找到本书最新章节",
        "→→→点击下载APP，免费看全本←←←",
        "【八一中文网首发】",
        "笔趣阁",
        "顶点小说无弹窗",
        "求月票！求推荐票！求收藏！",
        "投推荐票，加入书签",
        "（本章完）",
        "本章未完，请翻页",
        "本章未完，点击下一页继续阅读 >>> www.example-novel.com",
        "QQ群：123456",
        "加入书友群",
        "关注公众号：reader",
        "微信：reader",
        "ｗｗｗ．ｅｘａｍｐｌｅ．ｃｏｍ",
        "example.com",
        "https://example.com/a?b=1",
        "-------------",
    ],
)
def test_whole_line_templates_are_recorded(line: str) -> None:
    cleaned = clean_paragraphs((line,))
    assert cleaned.paragraphs == ()
    assert len(cleaned.removed) == 1
    assert cleaned.removed[0][1] == line


def test_extended_ads_are_limited_to_forty_nonspace_characters() -> None:
    line = "本章未完，点击下一页继续阅读 >>> www.example-very-long-novel.com"
    assert len("".join(line.split())) > 40
    # Existing page templates remain supported; longer new templates stay in the book.
    assert clean_paragraphs((line,)).paragraphs


@pytest.mark.parametrize(
    "line",
    [
        "他低声说：“记住本站地址的人都死了。”",
        "师父让他去城里找一家叫“笔趣阁”的书铺。",
        "那天他在微信上收到一条消息，内容只有三个字。",
        "求月票这件事，他想了一夜。",
        "本章完了，可故事还没完。",
        "最新章节请到我的梦里阅读。",
        "手机用户请浏览他的回忆录阅读。",
        "‘求月票！’",
        "「本章完」",
        "『加入书签』",
    ],
)
def test_prose_and_quotes_are_not_removed(line: str) -> None:
    cleaned = clean_paragraphs((line,))
    assert cleaned.paragraphs == (line,)
    assert cleaned.removed == ()


@pytest.mark.parametrize("domain", ["www.example.com", "ｗｗｗ．ｅｘａｍｐｌｅ．ｃｏｍ"])
def test_embedded_url_records_fragment_without_deleting_prose(domain: str) -> None:
    raw = f"网址是 {domain}，他抄在掌心里。"
    cleaned = clean_paragraphs((raw,))
    assert cleaned.paragraphs == ("网址是 ，他抄在掌心里。",)
    assert cleaned.removed == (("网址片段", raw),)


def test_quoted_url_is_preserved_and_recorded_for_review() -> None:
    line = "他说：“网址是 www.example.com。”"
    cleaned = clean_paragraphs((line,))
    assert cleaned.paragraphs == (line,)
    assert cleaned.removed == (("网址片段待复核", line),)


def test_zero_width_and_original_lines_in_legacy_records() -> None:
    assert normalize_line("天\u200b色\u200c已\u200d经\u2060暗\ufeff了。") == "天色已经暗了。"
    raw = (" 第一章 ", "　求月票！　", "正文。", "正文。", "正文结尾。")
    cleaned = clean_paragraphs(raw, title="第一章")
    assert cleaned.paragraphs == ("正文。", "正文结尾。")
    assert cleaned.removed == (
        ("章节标题", raw[0]),
        ("求票推广", raw[1]),
        ("相邻重复", raw[3]),
    )


def test_noise_with_interspersed_whitespace_is_recorded_as_original() -> None:
    raw = " 求　月　票！ "
    assert clean_paragraphs((raw,)).removed == (("求票推广", raw),)


def test_repeat_threshold_counts_chapters_not_occurrences_and_preserves_ocr_prose() -> None:
    chapters = tuple(
        NovelChapter(
            index,
            f"第{index}章",
            ("共同正文。", "【青崖书库】", "【青崖书库】"),
            source="ocr" if index == 10 else "html",
        )
        for index in range(1, 11)
    )
    result = export_chapters(chapters)
    for chapter in result[:9]:
        assert chapter.paragraphs == ("共同正文。",)
    assert result[9].paragraphs == chapters[9].paragraphs
    entries = removed_entries(result)
    assert len(entries) == 18 and all(rule == "跨章站点签名" for _, rule, _ in entries)

    below_minimum = chapters[:4]
    assert tuple(chapter.paragraphs for chapter in export_chapters(below_minimum)) == tuple(
        chapter.paragraphs for chapter in below_minimum
    )
    below_ratio = tuple(
        NovelChapter(i, str(i), ("【青崖书库】",) if i <= 5 else ("其他正文。",))
        for i in range(1, 12)
    )
    assert not removed_entries(export_chapters(below_ratio))
    exactly_half = below_ratio[:10]
    assert len(removed_entries(export_chapters(exactly_half))) == 5


def test_ocr_repeated_prose_is_kept_and_site_header_is_recorded() -> None:
    chapters = tuple(
        NovelChapter(i, str(i), ("影印书库", "他站在城门口。"), source="ocr") for i in range(1, 4)
    )
    result = export_chapters(chapters)
    assert all(chapter.paragraphs == ("他站在城门口。",) for chapter in result)
    assert removed_entries(result) == tuple((i, "OCR页眉页脚", "影印书库") for i in range(1, 4))


def test_old_and_new_cache_fields_and_invalid_removed() -> None:
    base = {"schema": 2, "title": "第一章", "paragraphs": ["正文。"]}
    old = decode_chapter(json.dumps(base).encode())
    assert old.removed == ()
    base["removed"] = [["求票推广", "求月票！"]]
    cached = decode_chapter(json.dumps(base, ensure_ascii=False).encode())
    assert cached.removed == (("求票推广", "求月票！"),)
    assert removed_entries((cached.to_chapter(4, ""),)) == ((4, "求票推广", "求月票！"),)
    base["removed"] = [["求票推广", 123]]
    with pytest.raises(LedgerError, match="字段不合法"):
        decode_chapter(json.dumps(base).encode())


def test_review_keeps_ocr_format_and_adds_removed_section() -> None:
    ocr = (ReviewEntry(2, "模糊行", 0.41),)
    assert (
        review_text("书名", ocr)
        == "# 书名 OCR 低置信行复核清单（置信度 < 0.6）\n\n第2章 [0.410] 模糊行\n"
    )
    assert review_text("书名", ocr, ((3, "求票推广", "求月票！"),)).endswith(
        "\n\n清洗删除（1 条）\n第3章 [求票推广] 求月票！\n"
    )
    pending = ((4, "网址片段待复核", "他说：“www.example.com。”"),)
    review = review_text("书名", ocr, pending)
    assert "清洗删除" not in review
    assert "网址待复核（1 条）" in review
    report = report_payload(NovelResult(Path("book.txt")), pending)
    assert report["removed_lines"] == 0 and report["removed_rules"] == {}


def test_cached_removals_flow_to_review_and_report(tmp_path: Path) -> None:
    output = tmp_path / "book.txt"
    cached = decode_chapter(
        json.dumps(
            {
                "schema": 2,
                "title": "第一章",
                "paragraphs": ["正文。"],
                "removed": [["求票推广", "求月票！"]],
            },
            ensure_ascii=False,
        ).encode()
    )
    chapters = export_chapters((cached.to_chapter(1, ""),))
    report = output.with_suffix(".report.json")
    review = output.with_suffix(".review.txt")
    stamps = preflight((output, report, review), overwrite=False)
    result = NovelResult(output, title="书名", task_id="a" * 64, chapters_written=1)
    final = asyncio.run(
        export_novel(
            result,
            chapters,
            (output,),
            ("txt",),
            stamps[:2],
            source_url="https://example.test",
            review_destination=review,
            review_stamp=stamps[2],
        )
    )
    assert final.review == review
    assert "第1章 [求票推广] 求月票！" in review.read_text("utf-8")
    payload = json.loads(report.read_text("utf-8"))
    assert payload["removed_lines"] == 1
    assert payload["removed_rules"] == {"求票推广": 1}
    assert "求月票" not in output.read_text("utf-8")


def test_removed_review_preserves_a_concurrently_created_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "book.txt"
    review = output.with_suffix(".review.txt")
    stamps = preflight((output, output.with_suffix(".report.json"), review), overwrite=False)
    cached = decode_chapter(
        json.dumps(
            {
                "schema": 2,
                "title": "第一章",
                "paragraphs": ["正文。"],
                "removed": [["求票推广", "求月票！"]],
            },
            ensure_ascii=False,
        ).encode()
    )

    async def race(receipt):
        review.write_text("用户文件", encoding="utf-8")
        await publish(receipt)

    monkeypatch.setattr(novel_export, "publish", race)
    result = NovelResult(output, title="书名", task_id="a" * 64, chapters_written=1)
    with pytest.raises(FetchError, match="小说导出失败"):
        asyncio.run(
            export_novel(
                result,
                export_chapters((cached.to_chapter(1, ""),)),
                (output,),
                ("txt",),
                stamps[:2],
                source_url="https://example.test",
                review_destination=review,
                review_stamp=stamps[2],
            )
        )
    assert review.read_text("utf-8") == "用户文件"


def test_prose_with_dotted_names_is_untouched() -> None:
    """正文里的 Mr.Li、J.K.Rowling、文件名不是网址，不能被删掉。"""
    for line in (
        "J.K.Rowling 的书他读过三遍。",
        "Mr.Li 推门进来。",
        "他把文件存成 notes.txt 发了过去。",
    ):
        result = clean_paragraphs([line])
        assert result.paragraphs == (line,)
        assert result.removed == ()


def test_bare_domain_in_prose_is_kept_for_review() -> None:
    result = clean_paragraphs(["他打开 google.com 查了查。"])
    assert result.paragraphs == ("他打开 google.com 查了查。",)
    assert result.removed == ((URL_REVIEW_RULE, "他打开 google.com 查了查。"),)


def test_spaced_latest_chapter_notice_is_whole_line() -> None:
    result = clean_paragraphs(["最新章节请到 www.xbiquge.la 阅读"])
    assert result.paragraphs == ()
