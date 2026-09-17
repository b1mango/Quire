"""小说端到端：本地目录页 → 章节 → EPUB/TXT，含恢复与失败定位。"""

from __future__ import annotations

import asyncio
import json
import zipfile
from dataclasses import replace
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from quire.core_novel import run_core_novel
from quire.errors import ConfigError, NoChaptersError, NoTextError
from quire.models import NovelOptions
from tests.mock_site.novel_server import CHAPTER_TITLES, novel_site

CATALOGUE = "/"
OPTIONS = NovelOptions(rate=100.0, retries=0, timeout=10.0, concurrency=3)


def run(site, out: Path, workdir: Path, **kwargs):
    params = {"options": kwargs.pop("options", OPTIONS), "workdir": workdir}
    params.update(kwargs)
    return asyncio.run(run_core_novel(site.url + CATALOGUE, out, **params))


@pytest.fixture
def site():
    with novel_site() as running:
        yield running


def _epub_text(path: Path) -> str:
    with zipfile.ZipFile(path) as archive:
        parts = [name for name in archive.namelist() if name.startswith("OEBPS/text/")]
        return "\n".join(archive.read(name).decode("utf-8") for name in parts)


def test_catalogue_exports_epub_and_txt_with_pagination(site, tmp_path: Path) -> None:
    out = tmp_path / "book.epub"
    result = run(site, out, tmp_path / "work", formats=("epub", "txt"))

    assert result.title == "测试之书"
    assert result.chapters_written == 4
    assert result.chapters_failed == 1  # 第四章在站点上返回 404
    assert result.source_resources == 5
    assert [artifact.format for artifact in result.artifacts] == ["epub", "txt"]
    assert out.exists() and out.with_suffix(".txt").exists()
    assert result.report is not None and result.report.exists()

    # 分页拼接：第二章由 3 页组成，第 3 页的内容必须在内。
    pages = {chapter.index: chapter.pages for chapter in result.chapters}
    assert pages[2] == 3
    assert pages[3] == 1  # 第三章的"下一章"不能被当成"下一页"

    body = _epub_text(out)
    assert "第2章第3页第6段" in body
    assert "第4章" not in body.split("本章抓取失败")[0]
    assert "请记住本站" not in body
    assert "http://ad.example.com" not in body
    assert "求推荐票" not in body

    with zipfile.ZipFile(out) as archive:
        assert archive.namelist()[0] == "mimetype"
        assert archive.read("mimetype") == b"application/epub+zip"
        nav = archive.read("OEBPS/nav.xhtml").decode("utf-8")
    for title in CHAPTER_TITLES.values():
        assert title in nav

    text = out.with_suffix(".txt").read_text("utf-8")
    assert "第三章 岔路" in text
    assert "请记住本站" not in text

    report = json.loads(result.report.read_text("utf-8"))
    assert report["status"] == "partial"
    assert report["written"] == 4 and report["missing"] == 1
    assert report["failures"][0][0].endswith("/book/4.html")


def test_missing_chapter_is_marked_in_artifacts(site, tmp_path: Path) -> None:
    out = tmp_path / "book.epub"
    result = run(site, out, tmp_path / "work", formats=("epub", "txt"))
    assert result.partial
    body = _epub_text(out)
    assert "本章抓取失败" in body
    assert "第4章" not in body.replace("第四章 缺失", "")
    text = out.with_suffix(".txt").read_text("utf-8")
    assert "［本章抓取失败：" in text
    assert "第四章 缺失" in text  # 目录里的标题仍然保留


def test_resume_reuses_cached_chapters_and_retries_only_failures(site, tmp_path: Path) -> None:
    out = tmp_path / "book.epub"
    work = tmp_path / "work"
    run(site, out, work)
    done = [f"/book/{n}.html" for n in (1, 2, 3, 5)]
    done_pages = ["/book/2_2.html", "/book/2_3.html"]
    before = {path: site.hits(path) for path in (*done, *done_pages)}
    before_missing = site.hits("/book/4.html")

    second = run(site, out, work, resume=True, options=replace(OPTIONS, overwrite=True))
    assert second.resources_reused == 4
    assert second.pages_fetched == 1  # 只有失败的第四章重试
    for path, count in before.items():
        assert site.hits(path) == count, path
    assert site.hits("/book/4.html") == before_missing + 1
    assert second.chapters_written == 4


def test_corrupted_chapter_cache_is_refetched_alone(site, tmp_path: Path) -> None:
    out = tmp_path / "book.epub"
    work = tmp_path / "work"
    first = run(site, out, work)
    task_id = first.task_id
    assert task_id is not None
    cached = sorted((work / "cache" / task_id).glob("*.json"))
    assert len(cached) == 4
    cached[1].write_bytes(b"{not json")

    hits_before = {n: site.hits(f"/book/{n}.html") for n in (1, 2, 3, 5)}
    second = run(site, out, work, resume=True, options=replace(OPTIONS, overwrite=True))
    assert second.resources_reused == 3
    assert second.chapters_written == 4
    # 只有被破坏的那一章重新请求过。
    refreshed = [n for n, count in hits_before.items() if site.hits(f"/book/{n}.html") > count]
    assert len(refreshed) == 1


def test_single_chapter_url_is_one_chapter_and_reuses_the_fetch(site, tmp_path: Path) -> None:
    out = tmp_path / "book.txt"
    result = asyncio.run(
        run_core_novel(
            site.url + "/book/3.html",
            out,
            options=OPTIONS,
            workdir=tmp_path / "work",
            formats=("txt",),
        )
    )
    assert result.chapters_written == 1
    assert result.source_resources == 1
    assert site.hits("/book/3.html") == 1  # 入口页不会请求两次
    text = out.read_text("utf-8")
    assert "第三章 岔路" in text


def test_max_chapters_truncates_with_warning(site, tmp_path: Path) -> None:
    out = tmp_path / "book.epub"
    result = run(
        site,
        out,
        tmp_path / "work",
        options=replace(OPTIONS, max_chapters=2),
    )
    assert result.source_resources == 2
    assert result.chapters_written == 2 and result.chapters_failed == 0
    assert result.partial and result.truncated and not result.failures
    assert any("上限" in warning for warning in result.warnings)
    assert all(site.hits(f"/book/{index}.html") == 0 for index in (3, 4, 5))
    assert result.report is not None
    report = json.loads(result.report.read_text("utf-8"))
    assert report["status"] == "partial" and report["truncated"] is True
    assert report["written"] == 2 and report["missing"] == 0
    assert report["warnings"] and len(report["chapter_list"]) == 2


def test_explicit_selector_with_one_link_selects_that_chapter(site, tmp_path: Path) -> None:
    out = tmp_path / "selected.txt"
    result = run(
        site,
        out,
        tmp_path / "work",
        formats=("txt",),
        options=replace(OPTIONS, chapter_selector="#chapter-list a[href='/book/3.html']"),
    )
    assert not result.partial
    assert result.source_resources == result.chapters_written == 1
    assert result.chapters[0].url == site.url + "/book/3.html"
    assert "第三章 岔路" in out.read_text("utf-8")
    assert site.hits("/") == site.hits("/book/3.html") == 1
    assert all(site.hits(f"/book/{index}.html") == 0 for index in (1, 2, 4, 5))


def test_explicit_selector_without_matches_does_not_fall_back_to_single_chapter(
    site,
    tmp_path: Path,
) -> None:
    out = tmp_path / "absent.epub"
    work = tmp_path / "work"
    with pytest.raises(NoChaptersError):
        run(site, out, work, options=replace(OPTIONS, chapter_selector="#absent a"))
    assert site.hits("/") == 1
    assert all(site.hits(f"/book/{index}.html") == 0 for index in range(1, 6))
    assert not out.exists() and not out.with_suffix(".report.json").exists()
    assert not work.exists()


def test_second_run_into_new_output_reuses_the_same_cache(site, tmp_path: Path) -> None:
    work = tmp_path / "work"
    run(site, tmp_path / "book.epub", work)
    before = site.hits("/book/1.html")
    # 同一个目录页、新的输出名：章节缓存继续复用，不重新联网抓章节。
    second = run(site, tmp_path / "copy.epub", work)
    assert second.resources_reused == 4
    assert second.chapters_written == 4
    assert site.hits("/book/1.html") == before


def test_existing_output_is_refused_unless_overwrite(site, tmp_path: Path) -> None:
    out = tmp_path / "book.epub"
    out.write_bytes(b"old")
    with pytest.raises(ConfigError, match="目标已存在"):
        run(site, out, tmp_path / "work")
    result = run(site, out, tmp_path / "work2", options=NovelOptions(rate=100.0, overwrite=True))
    assert result.chapters_written == 4
    assert out.read_bytes() != b"old"


def test_wrong_content_selector_reports_no_text(site, tmp_path: Path) -> None:
    with pytest.raises(NoTextError):
        run(
            site,
            tmp_path / "book.epub",
            tmp_path / "work",
            options=NovelOptions(rate=100.0, retries=0, content_selector="div.absent"),
        )


def test_report_lists_chapter_details_for_small_books(site, tmp_path: Path) -> None:
    out = tmp_path / "book.epub"
    run(site, out, tmp_path / "work")
    report = json.loads((out.with_suffix(".report.json")).read_text("utf-8"))
    assert len(report["chapter_list"]) == 5
    missing = [item for item in report["chapter_list"] if item["missing"]]
    assert len(missing) == 1 and missing[0]["index"] == 4
    assert report["chapter_list"][1]["chars"] > 200


def test_epub_parts_are_well_formed_xml(site, tmp_path: Path) -> None:
    out = tmp_path / "book.epub"
    run(site, out, tmp_path / "work")
    with zipfile.ZipFile(out) as archive:
        assert archive.testzip() is None
        for name in archive.namelist():
            if name.endswith((".xhtml", ".opf", ".ncx", ".xml")):
                ET.fromstring(archive.read(name))


def test_cli_novel_reports_partial_and_writes_output(site, tmp_path: Path, capsys) -> None:
    from quire.cli import main

    out = tmp_path / "cli.epub"
    code = main(
        [
            "novel",
            site.url + "/",
            "-o",
            str(out),
            "--rate",
            "100",
            "--retries",
            "0",
            "--workdir",
            str(tmp_path / "work"),
            "--format",
            "epub,txt",
        ]
    )
    assert code == 4  # 有缺章：成功但带警告
    assert out.exists() and out.with_suffix(".txt").exists()
    printed = capsys.readouterr().out
    assert "1 章缺失" in printed
    assert "EPUB" in printed and "TXT" in printed


def test_cli_novel_single_chapter_exits_zero(site, tmp_path: Path, capsys) -> None:
    from quire.cli import main

    out = tmp_path / "one.epub"
    code = main(
        [
            "novel",
            site.url + "/book/1.html",
            "-o",
            str(out),
            "--rate",
            "100",
            "--retries",
            "0",
            "--workdir",
            str(tmp_path / "work"),
        ]
    )
    assert code == 0
    assert "1 章" in capsys.readouterr().out


def test_cli_novel_rejects_bad_format_and_extension(tmp_path: Path) -> None:
    from quire.cli import main

    assert main(["novel", "http://example.test/", "--format", "mobi"]) == 1
    assert main(["novel", "http://example.test/", "-o", str(tmp_path / "x.mobi")]) == 1
    assert main(["novel", "http://example.test/", "--content-selector", "div["]) == 1


def test_available_novel_output_numbers_existing_targets(tmp_path: Path) -> None:
    from quire.novel_options import available_novel_output, novel_output_paths

    (tmp_path / "book.epub").write_bytes(b"x")
    (tmp_path / "book.txt").write_bytes(b"x")
    formats = ("epub", "txt")
    assert (
        available_novel_output(tmp_path / "book.epub", formats, overwrite=False).name
        == "book (1).epub"
    )
    assert (
        available_novel_output(tmp_path / "book.epub", formats, overwrite=True).name == "book.epub"
    )
    with pytest.raises(ConfigError):
        novel_output_paths(tmp_path / "book.pdf", formats)


def test_fully_cached_single_chapter_never_touches_the_site_again(site, tmp_path: Path) -> None:
    work = tmp_path / "work"
    url = site.url + "/book/1.html"
    asyncio.run(
        run_core_novel(url, tmp_path / "one.txt", options=OPTIONS, workdir=work, formats=("txt",))
    )
    before = site.hits("/book/1.html")
    second = asyncio.run(
        run_core_novel(
            url,
            tmp_path / "two.txt",
            options=OPTIONS,
            workdir=work,
            formats=("txt",),
        )
    )
    assert second.resources_reused == 1
    assert second.pages_fetched == 0  # 章节抓取没有发出任何请求
    # 只多了一次"确认任务身份"的入口页请求，正文来自缓存。
    assert site.hits("/book/1.html") == before + 1


def test_resume_does_not_overwrite_an_unrelated_file(site, tmp_path: Path) -> None:
    """--resume 不是覆盖许可：没有可继续的任务时，用户同名文件必须原样保留。"""
    out = tmp_path / "mine.epub"
    out.write_bytes(b"MY PRECIOUS ORIGINAL FILE")
    with pytest.raises(ConfigError, match="目标已存在"):
        run(site, out, tmp_path / "fresh-work", resume=True)
    assert out.read_bytes() == b"MY PRECIOUS ORIGINAL FILE"
    assert site.total_hits() == 0


def test_resume_with_explicit_overwrite_rewrites_its_own_output(site, tmp_path: Path) -> None:
    work = tmp_path / "work"
    out = tmp_path / "book.epub"
    first = run(site, out, work)
    assert first.chapters_written == 4
    # 同一任务 + 同名输出仍须显式授权覆盖。
    second = run(site, out, work, resume=True, options=replace(OPTIONS, overwrite=True))
    assert second.resources_reused == 4
    assert second.task_id == first.task_id
    assert out.exists()
