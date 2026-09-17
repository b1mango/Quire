"""Regression cases from the M3/M4 independent review."""

from pathlib import Path

import pytest

from quire.core_novel import plan_chapters
from quire.fetch.simple import Response
from quire.models import NovelOptions
from quire.parse.chapters import ChapterLink, find_next_page
from quire.parse.minidom import parse
from tests.test_novel_capture import (
    PROSE,
    SOURCE,
    StubFetcher,
    _cached_chapter,
    chapter_html,
    run_capture,
)


def response(body: str, url: str = SOURCE + "2.html") -> Response:
    return Response(url=url, status=200, headers={}, content=body.encode(), elapsed_ms=1)


@pytest.mark.parametrize("selector", [None, "#content"])
def test_chapter_entry_with_numbered_neighbours_keeps_current_chapter(selector) -> None:
    page = response(
        chapter_html(
            "第二章 归来",
            tail='<a href="1.html">第一章 出发</a><a href="3.html">第三章 新途</a>',
        )
    )
    plan = plan_chapters(page, NovelOptions(content_selector=selector))
    assert [link.url for link in plan.links] == [page.url]
    assert plan.preloaded == {page.url: page}


@pytest.mark.parametrize("selector", [None, "#content"])
def test_long_introduction_does_not_hide_a_catalogue(selector) -> None:
    page = response(
        chapter_html(
            "远山故事",
            tail='<div class="chapters"><a href="1.html">第一章</a>'
            '<a href="2.html">第二章</a><a href="3.html">第三章</a></div>',
        ),
        SOURCE,
    )
    plan = plan_chapters(page, NovelOptions(content_selector=selector))
    assert [link.url for link in plan.links] == [SOURCE + f"{i}.html" for i in (1, 2, 3)]
    assert not plan.preloaded


def test_explicit_chapter_selector_still_wins_over_entry_body() -> None:
    page = response(
        chapter_html(
            "第二章 归来",
            tail='<a class="chapter" href="1.html">第一章</a>'
            '<a class="chapter" href="3.html">第三章</a>',
        )
    )
    plan = plan_chapters(page, NovelOptions(chapter_selector="a.chapter"))
    assert [link.url for link in plan.links] == [SOURCE + "1.html", SOURCE + "3.html"]


@pytest.mark.parametrize(
    "markup",
    [
        '<a href="{target}">Next</a>',
        '<link rel="next" href="{target}">',
        '<a rel="next" href="{target}">→</a>',
    ],
)
@pytest.mark.parametrize(
    "current,target,follow",
    [
        ("2.html", "2_2.html", True),
        ("2_2.html", "2_3.html", True),
        ("2.html", "3.html", False),
        ("2_2.html", "3.html", False),
    ],
)
def test_ambiguous_next_uses_chapter_url_evidence(markup, current, target, follow) -> None:
    url = SOURCE + current
    doc = parse(markup.format(target=target), base_url=url)
    assert find_next_page(doc, url, url) == (SOURCE + target if follow else None)


@pytest.mark.parametrize("alias", [SOURCE + "2.html", "http://example.test:80/book/2.html#top"])
@pytest.mark.parametrize("max_pages", [1, 10])
def test_page_label_cannot_merge_known_chapters(tmp_path: Path, alias, max_pages) -> None:
    links = tuple(ChapterLink(f"第{i}章", SOURCE + f"{i}.html") for i in (1, 2))
    pages = {
        links[0].url: chapter_html("第一章", tail=f'<a href="{alias}">下一页</a>'),
        links[1].url: chapter_html("第二章", "<p>第二章独有内容。</p>" + PROSE),
    }
    fetcher = StubFetcher(pages)
    result, snapshot = run_capture(
        tmp_path, links, fetcher, options=NovelOptions(max_pages=max_pages)
    )
    assert sorted(fetcher.requests) == sorted(pages)
    first = _cached_chapter(tmp_path / "work", snapshot)
    assert first.pages == 1 and not first.truncated
    assert "第二章独有内容" not in "".join(first.paragraphs)
    assert _cached_chapter(tmp_path / "work", snapshot, 1).pages == 1
    assert any("其他章节" in warning for warning in result.warnings)


@pytest.mark.parametrize("title", ["第一章", "无章号标题"])
def test_page_title_change_stops_unknown_chapter_boundary(tmp_path: Path, title) -> None:
    links = (ChapterLink(title, SOURCE + "1.html"),)
    fetcher = StubFetcher(
        {
            links[0].url: chapter_html("第一章", tail='<a href="1_2.html">下一页</a>'),
            SOURCE + "1_2.html": chapter_html("第二章", "<p>下一章独有内容。</p>" + PROSE),
        }
    )
    result, snapshot = run_capture(tmp_path, links, fetcher)
    first = _cached_chapter(tmp_path / "work", snapshot)
    assert first.pages == 1 and not first.truncated
    assert "下一章独有内容" not in "".join(first.paragraphs)
    assert any("章号" in warning for warning in result.warnings)


def test_redirected_continuation_cannot_merge_a_known_chapter(tmp_path: Path) -> None:
    links = tuple(ChapterLink(f"第{i}章", SOURCE + f"{i}.html") for i in (1, 2))
    fetcher = StubFetcher(
        {
            links[0].url: chapter_html("第一章", tail='<a href="1_2.html">下一页</a>'),
            SOURCE + "1_2.html": response(chapter_html("书名", "<p>下一章独有内容。</p>" + PROSE)),
            links[1].url: chapter_html("第二章"),
        }
    )
    result, snapshot = run_capture(tmp_path, links, fetcher)
    first = _cached_chapter(tmp_path / "work", snapshot)
    assert first.pages == 1 and "下一章独有内容" not in "".join(first.paragraphs)
    assert any("其他章节" in warning for warning in result.warnings)
