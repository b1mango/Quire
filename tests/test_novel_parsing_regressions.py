"""Regressions for conservative novel parsing and continuation boundaries."""

from __future__ import annotations

import pytest

from quire.parse.article import Article, extract_article, extract_paragraphs, validate_article
from quire.parse.chapters import discover_chapters, find_next_page
from quire.parse.minidom import parse
from quire.text.clean import chapter_number, clean_paragraphs, is_noise_line, normalize_line


@pytest.mark.parametrize(
    "text",
    [
        "他扫码付了钱，又把推荐票放在桌上。",
        "她在微信公众号里看到广告合作，又提到了笔趣阁。",
        '他说："求月票，求订阅。"随后合上电脑。',
        "她指着屏幕说：请记住本站域名。",
        "故事未完待续，他翻开全文阅读，确认页面无弹窗。",
        "请记住本站域名这几个字，正是他留下的暗号。",
    ],
)
def test_generic_words_preserve_the_entire_paragraph(text: str) -> None:
    assert not is_noise_line(text)
    assert normalize_line(text) == text
    assert clean_paragraphs((text,)) == (text,)


def test_inline_notice_requires_a_complete_template_sentence() -> None:
    assert normalize_line("他走了。请记住本站域名。雨还在下。") == "他走了。雨还在下。"
    assert normalize_line("她读到本章未完，便放下了书。") == "她读到本章未完，便放下了书。"
    assert clean_paragraphs(("本章未完，请点击下一页", "求推荐票，求收藏！")) == ()


def test_repeated_prose_below_the_spam_threshold_is_preserved() -> None:
    paragraphs = ("他没有回答。", "风声依旧。", "他没有回答。", "雨又落下。", "他没有回答。")
    assert clean_paragraphs(paragraphs) == paragraphs


@pytest.mark.parametrize("title", ["第一〇一章", "第一零一章", "第〇一〇一章"])
def test_digitwise_chinese_chapter_numbers(title: str) -> None:
    assert chapter_number(title) == 101


def test_inline_elements_do_not_split_prose() -> None:
    doc = parse(
        "<article><p>他<span>低声<em>说</em></span>："
        "<a href='/word'>你好</a>，世界。<span class='ads'>扫码广告</span>然后离开。"
        "<br>第二行。</p><p>末段<strong>结束</strong>。</p></article>"
    )
    node = doc.select_one("article")
    assert node is not None
    assert extract_paragraphs(node) == (
        "他低声说：你好，世界。然后离开。",
        "第二行。",
        "末段结束。",
    )


@pytest.mark.parametrize(
    ("current", "target", "follow"),
    [
        ("/book/2.html", "/book/2_2.html", True),
        ("/book/2_2.html", "/book/2_3.html", True),
        ("/book/2_3.html", "/book/2_4.html", True),
        ("/book/2-2.html", "/book/2-3.html", True),
        ("/book/chapter_2.html", "/book/chapter_3.html", False),
        ("/book/2_2.html", "/book/3.html", False),
        ("/book/2.html", "/book/20.html", False),
        ("/book/2.html", "/book/2-other.html", False),
        ("/book/2.html", "/other/2_2.html", False),
        ("/book/2.html", "/book/2_2.txt", False),
        ("/book/2_3.html", "/book/2_2.html", False),
        ("/book/2_2.html?book=1", "/book/2_3.html?book=2", False),
        ("/read?id=9", "/read?id=9&page=2", True),
        ("/read?id=9&page=2", "/read?page=3&id=9", True),
        ("/read?id=9&page=2", "/read?id=10&page=3", False),
        ("/read?id=9&page=2&lang=zh", "/read?id=9&page=3", False),
        ("/read?id=9&page=2", "/read?id=9&page=3&lang=en", False),
        ("/read?id=9&page=2", "/read?id=9&page=1", False),
        ("/read?id=9&page=2", "/read?id=9&page=2x", False),
        ("/read?id=9&page=2", "/read?id=9&page=3&page=4", False),
        ("/read?id=9&id=8&page=2", "/read?id=9&page=3", False),
        ("/read?id=9&lang=&page=2", "/read?id=9&page=3", False),
        ("/read?id=9&id=8&page=2", "/read?id=8&id=9&page=3", False),
    ],
)
def test_rel_next_preserves_chapter_identity(current: str, target: str, follow: bool) -> None:
    origin = "https://example.test"
    url = origin + current
    doc = parse(f'<a rel="next" href="{target}">Next</a>', base_url=url)
    assert find_next_page(doc, url, url) == (origin + target if follow else None)


def test_category_and_nav_links_are_not_chapters() -> None:
    """分类/导航链接(指向 /c/*.html 等路径)不进入章节范围下拉。

    回归:quanben 等站的目录页把「现代言情/古代言情」等分类链接放在列表
    容器里,之前会被当成章节。
    """
    url = "https://example.test/n/book/list.html"
    doc = parse(
        '<ul class="list3">'
        '<li><a href="/n/book/1.html"><span>第1章 开始</span></a></li>'
        '<li><a href="/n/book/2.html"><span>第2章 继续</span></a></li>'
        '<li><a href="/n/book/3.html"><span>第3章 结束</span></a></li>'
        "</ul>"
        '<ul class="list3">'
        '<li><a href="/c/xiandai.html">现代言情</a></li>'
        '<li><a href="/c/gudai.html">古代言情</a></li>'
        '<li><a href="/category/junshi.html">军事</a></li>'
        '<li><a href="/rank/top.html">排行榜单</a></li>'
        '<li><a href="/tag/wanben.html">完本</a></li>'
        '<li><a href="/n/book/foreword.html">作品相关</a></li>'
        "</ul>",
        base_url=url,
    )
    links = discover_chapters(doc, url)
    assert [link.url for link in links] == [
        "https://example.test/n/book/1.html",
        "https://example.test/n/book/2.html",
        "https://example.test/n/book/3.html",
    ]


@pytest.mark.parametrize("selector", [None, "a"])
@pytest.mark.parametrize(
    "foreign",
    [
        "https://other.test",
        "http://example.test",
        "https://example.test:444",
        "https://www.example.test",
    ],
)
def test_base_href_cannot_change_the_origin(foreign: str, selector: str | None) -> None:
    url = "https://example.test/book/2.html"
    doc = parse(f'<base href="{foreign}/book/"><a href="2_2.html">下一页</a>', base_url=url)
    assert find_next_page(doc, url, url, selector=selector) is None
    catalogue = parse(
        f'<base href="{foreign}/book/"><a href="1.html">第一章</a><a href="2.html">第二章</a>',
        base_url=url,
    )
    assert discover_chapters(catalogue, url, selector=selector) == ()


def test_relative_base_is_only_for_resolution_and_default_ports_are_same_origin() -> None:
    url = "https://example.test/book/2.html"
    doc = parse(
        '<base href="/pages/"><a href="https://example.test:443/pages/2_2.html">下一页</a>',
        base_url=url,
    )
    assert find_next_page(doc, url, url) == "https://example.test:443/pages/2_2.html"
    catalogue = parse(
        '<base href="/pages/"><a href="1.html">第一章</a><a href="2.html">第二章</a>',
        base_url=url,
    )
    assert [link.url for link in discover_chapters(catalogue, url)] == [
        "https://example.test/pages/1.html",
        "https://example.test/pages/2.html",
    ]


def test_selector_does_not_override_explicit_next_chapter() -> None:
    url = "https://example.test/book/2.html"
    doc = parse('<a href="2_2.html">下一章</a>', base_url=url)
    assert find_next_page(doc, url, url, selector="a") is None


@pytest.mark.parametrize("selector", [None, "a"])
def test_explicit_page_links_cannot_change_query_identity(selector: str | None) -> None:
    url = "https://example.test/read?id=2&page=1"
    doc = parse('<a href="?id=3&page=2">下一页</a>', base_url=url)
    assert find_next_page(doc, url, url, selector=selector) is None


@pytest.mark.parametrize("selector", [None, "#content"])
def test_short_trusted_continuation_survives_extraction_and_validation(
    selector: str | None,
) -> None:
    paragraph = "风停了。他终于回到家中，轻轻关上了门。"
    doc = parse(f"<h1>第二章 归来</h1><div id='content'><p>{paragraph}</p></div>")
    article = extract_article(doc, selector=selector, continuation=True)
    assert article.paragraphs == (paragraph,)
    assert validate_article(article, continuation=True) == (True, "ok")
    assert validate_article(article) == (False, "too_short")
    assert clean_paragraphs(article.paragraphs) == (paragraph,)


@pytest.mark.parametrize(
    ("title", "body"),
    [
        ("", "风停了。他终于回到家中，轻轻关上了门。"),
        ("页面不存在", "请求失败，请稍后重试。"),
        ("第二章", "页面不存在，请稍后重试。"),
        ("第二章", "正在加载，请稍候。"),
        ("第二章", "请记住本站域名 www.example.com"),
        ("第二章", "本章未完，请点击下一页"),
        ("第二章", "<a href='/3'>下一章，继续阅读。</a>"),
        ("第二章", "this is an error, please try again."),
    ],
)
def test_continuation_does_not_accept_unknown_or_error_pages(title: str, body: str) -> None:
    doc = parse(f"<title>{title}</title><div id='content'>{body}</div>")
    article = extract_article(doc, selector="#content", continuation=True)
    assert not validate_article(article, continuation=True)[0]


def test_whitespace_cannot_satisfy_the_length_threshold() -> None:
    assert (
        validate_article(Article("第二章", ("风停了。" + " " * 200 + "天亮了。",)))[1]
        == "too_short"
    )


def test_three_page_chain_keeps_the_short_ending_and_stops_before_the_next_chapter() -> None:
    origin = "https://example.test/book/"
    first = tuple(f"他第{i}次扫码，想到昨天获得的推荐票，又轻轻摇头。" for i in range(12))
    middle = ("天色暗了。<em>他</em>仍向前走。",)
    ending = ("他到家了。",)
    pages = {
        "2.html": (first, "2_2.html"),
        "2_2.html": (middle, "2_3.html"),
        "2_3.html": (ending, "3.html"),
    }
    url: str | None = origin + "2.html"
    gathered: list[str] = []
    visited: list[str] = []
    while url is not None:
        paragraphs, following = pages[url.removeprefix(origin)]
        body = "".join(f"<p>{paragraph}</p>" for paragraph in paragraphs)
        doc = parse(
            f"<title>第二章 归来</title><div id='content'>{body}</div>"
            f'<nav><a rel="next" href="{following}">Next</a></nav>',
            base_url=url,
        )
        continuation = bool(visited)
        article = extract_article(doc, url, continuation=continuation)
        assert validate_article(article, continuation=continuation) == (True, "ok")
        visited.append(url)
        gathered.extend(article.paragraphs)
        url = find_next_page(doc, url, url)
    assert visited == [origin + name for name in pages]
    assert clean_paragraphs(gathered) == (*first, "天色暗了。他仍向前走。", *ending)
