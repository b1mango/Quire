"""小说纯函数层：文本清洗、正文抽取、章节发现（无网络、无磁盘）。"""

from __future__ import annotations

import pytest

from quire.errors import ParseError
from quire.parse.article import (
    Article,
    best_container,
    extract_article,
    extract_paragraphs,
    link_density,
    page_title,
    validate_article,
)
from quire.parse.chapters import (
    ChapterLink,
    chapter_signature,
    classify_next,
    discover_chapters,
    find_next_page,
    looks_like_catalogue,
)
from quire.parse.minidom import parse as parse_html
from quire.text.clean import (
    chapter_number,
    cjk_ratio,
    clean_paragraphs,
    cn_number,
    has_chapter_mark,
    is_noise_line,
    normalize_line,
    punctuation_density,
    repeated_line_ratio,
    strip_site_suffix,
)

# ============================================================ 清洗


@pytest.mark.parametrize(
    "line",
    [
        "请记住本站域名 www.example.com",
        "http://ad.example.com/track?id=9",
        "求推荐票，求收藏！",
        "本章未完，请点击下一页",
        "……",
        "----",
        "12345",
        "   ",
    ],
)
def test_noise_lines_are_dropped(line: str) -> None:
    assert is_noise_line(line)


@pytest.mark.parametrize(
    "line",
    ["他抬头看见远山如黛。", "“你来了。”他说。", "第 3 章 起点"],
)
def test_real_prose_is_not_noise(line: str) -> None:
    assert not is_noise_line(line)


def test_normalize_line_fixes_chinese_punctuation_and_spacing() -> None:
    assert normalize_line("他 说,你好. 真的吗?") == "他说，你好。真的吗？"
    assert normalize_line("等等...") == "等等……"
    assert normalize_line("(测试)") == "（测试）"
    assert normalize_line("(hello)") == "(hello)"
    assert normalize_line("他说（测试）") == "他说（测试）"


def test_normalize_line_keeps_english_and_numbers() -> None:
    assert normalize_line("version 1.5, build 7") == "version 1.5, build 7"


def test_normalize_line_strips_inline_site_notice() -> None:
    assert normalize_line("他走了。请记住本站域名") == "他走了。"


def test_clean_paragraphs_removes_noise_duplicates_and_spam() -> None:
    paragraphs = (
        "",
        "第一段正文，写了一点事情。",
        "第一段正文，写了一点事情。",
        "请记住本站域名 www.example.com",
        "第二段正文，又写了一点事情。",
        "第二段正文，又写了一点事情。",
        "刷屏行",
        "刷屏行",
        "刷屏行",
        "刷屏行",
        "刷屏行",
        "刷屏行",
        "刷屏行",
        "刷屏行",
        "收尾段落，故事到此结束。",
    )
    cleaned = clean_paragraphs(paragraphs)
    assert cleaned == (
        "第一段正文，写了一点事情。",
        "第二段正文，又写了一点事情。",
        "收尾段落，故事到此结束。",
    )


def test_clean_paragraphs_drops_line_equal_to_title() -> None:
    assert clean_paragraphs(("第一章 起点", "正文内容。"), title="第一章 起点") == ("正文内容。",)


def test_text_ratios() -> None:
    assert cjk_ratio("你好世界") == 1.0
    assert cjk_ratio("hello") == 0.0
    assert punctuation_density("你好，世界。") == pytest.approx(2 / 6)
    assert repeated_line_ratio(["a", "a", "b", "b"]) == pytest.approx(0.5)
    assert repeated_line_ratio([]) == 0.0


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("第一章 起点", 1),
        ("第12章 转折", 12),
        ("第十二章 转折", 12),
        ("第二十三章 归来", 23),
        ("第 105 节", 105),
        ("Chapter 7", 7),
        ("7. 起点", 7),
        ("序章", None),
    ],
)
def test_chapter_number(title: str, expected: int | None) -> None:
    assert chapter_number(title) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("十", 10),
        ("十二", 12),
        ("二十", 20),
        ("二十三", 23),
        ("一百零五", 105),
        ("两千零一十", 2010),
    ],
)
def test_cn_number(text: str, expected: int) -> None:
    assert cn_number(text) == expected


def test_cn_number_rejects_non_numbers() -> None:
    assert cn_number("序") is None
    assert cn_number("") is None


def test_chapter_mark_and_site_suffix() -> None:
    assert has_chapter_mark("第十二章 起点")
    assert not has_chapter_mark("作品相关")
    assert strip_site_suffix("第一章 起点_测试书城") == "第一章 起点"
    assert strip_site_suffix("测试之书 - 某作者") == "测试之书"
    assert strip_site_suffix("没有分隔符的书名") == "没有分隔符的书名"


# ============================================================ 正文抽取


def _article_html(body: str, *, nav: str = "") -> str:
    return f"<html><body>{nav}<div id='wrap'><div class='content'>{body}</div></div></body></html>"


def test_extract_paragraphs_splits_blocks_and_br() -> None:
    doc = parse_html(_article_html("<p>第一段</p><p>第二段<br>第三段</p>"))
    node = doc.select_one("div.content")
    assert node is not None
    assert extract_paragraphs(node) == ("第一段", "第二段", "第三段")


def test_extract_paragraphs_skips_scripts_and_nested_containers() -> None:
    doc = parse_html(_article_html("<div><p>外层</p></div><script>var x=1</script><p>尾段</p>"))
    node = doc.select_one("div.content")
    assert node is not None
    assert extract_paragraphs(node) == ("外层", "尾段")


def test_link_density_distinguishes_index_from_prose() -> None:
    prose = parse_html(_article_html("<p>正文内容正文内容</p>")).select_one("div.content")
    index = parse_html("<div><a href='/a'>章节一</a><a href='/b'>章节二</a></div>").select_one(
        "div"
    )
    assert prose is not None and index is not None
    assert link_density(prose) == 0.0
    assert link_density(index) == 1.0


def test_best_container_prefers_content_over_link_heavy_wrapper() -> None:
    body = "".join(
        f"<p>这是第{i}段正文，写了一些很长很长的内容用来超过阈值。</p>" for i in range(8)
    )
    html = (
        "<html><body><div id='page'>"
        "<div class='nav'>" + "<a href='/x'>导航链接占位</a>" * 20 + "</div>"
        f"<div class='content'>{body}</div></div></body></html>"
    )
    doc = parse_html(html)
    node = best_container(doc)
    assert node is not None and node.get("class") == "content"


def test_extract_article_uses_selector_and_reports_missing() -> None:
    html = _article_html("<p>" + "正文内容。" * 60 + "</p>")
    doc = parse_html(html, base_url="http://example.test/book/1.html")
    article = extract_article(doc, "http://example.test/book/1.html", selector="div.content")
    assert article.char_count > 200
    assert article.source_url.endswith("/book/1.html")
    with pytest.raises(ParseError):
        extract_article(doc, "", selector="div.nope")


def test_page_title_prefers_h1_and_strips_site_suffix() -> None:
    doc = parse_html("<title>第一章 起点_测试书城</title><h1>第一章 起点</h1>")
    assert page_title(doc) == "第一章 起点"
    assert page_title(parse_html("<title>只有标题_站点</title>"), fallback="兜底") == "只有标题"


def test_validate_article_accepts_prose_and_rejects_bad_pages() -> None:
    good = Article(
        "第一章",
        tuple(
            f"他第{index}次抬头看见远山如黛，风从林间穿过，心里忽然安静下来。"
            for index in range(20)
        ),
    )
    assert validate_article(good) == (True, "ok")
    assert validate_article(Article("短", ("太短了。",)))[1] == "too_short"
    english = tuple("hello there, this is plain english prose. " * 2 for _ in range(5))
    assert validate_article(Article("t", english))[1] == "not_cjk_prose"
    index = Article("目录", tuple("第一章 起点在这里" for _ in range(40)), link_density=0.9)
    assert validate_article(index)[1] == "looks_like_index_page"
    soup = Article("x", tuple("asdfghjklqwertyuiop" * 2 for _ in range(20)))
    assert validate_article(soup)[1] in {"looks_like_word_soup", "not_cjk_prose"}
    repeated = Article("y", tuple("同一行内容重复出现。" for _ in range(30)))
    assert validate_article(repeated)[1] == "template_repeat"


# ============================================================ 章节发现

CATALOGUE = """
<html><body><h1>测试之书</h1>
<ul id="list">
  <li><a href="/book/1.html">第一章 起点</a></li>
  <li><a href="/book/2.html">第二章 分页</a></li>
  <li><a href="/book/3.html">第三章 岔路</a></li>
  <li><a href="/book/10.html">第十章 尾声</a></li>
  <li><a href="/book/10.html">最新章节</a></li>
  <li><a href="/rank">排行榜</a></li>
  <li><a href="https://other.test/book/1.html">外站章节</a></li>
</ul>
<nav><a href="/">首页</a><a href="/book/1.html">下一章</a></nav>
</body></html>
"""


def test_discover_chapters_filters_navigation_and_orders_by_number() -> None:
    doc = parse_html(CATALOGUE, base_url="http://example.test/book/")
    links = discover_chapters(doc, "http://example.test/book/")
    assert [link.title for link in links] == [
        "第一章 起点",
        "第二章 分页",
        "第三章 岔路",
        "第十章 尾声",
    ]
    assert [link.url for link in links] == [
        "http://example.test/book/1.html",
        "http://example.test/book/2.html",
        "http://example.test/book/3.html",
        "http://example.test/book/10.html",
    ]
    assert looks_like_catalogue(links)


def test_discover_chapters_uses_selector_and_keeps_document_order_without_numbers() -> None:
    doc = parse_html(
        "<ul id='list'><li><a href='/b'>乙章</a></li><li><a href='/a'>甲章</a></li>"
        "<li><a href='/c'>丙章</a></li></ul>",
        base_url="http://example.test/",
    )
    links = discover_chapters(doc, "http://example.test/", selector="#list a")
    assert [link.title for link in links] == ["乙章", "甲章", "丙章"]


def test_discover_chapters_keeps_modal_url_pattern() -> None:
    html = (
        "<div>"
        + "".join(f"<a href='/book/{n}.html'>第{n}章 正文</a>" for n in range(1, 6))
        + "<a href='/other/9.html'>第九章 别处</a>"
        + "</div>"
    )
    doc = parse_html(html, base_url="http://example.test/")
    links = discover_chapters(doc, "http://example.test/")
    assert len(links) == 5
    assert all("/book/" in link.url for link in links)


def test_chapter_signature_normalizes_digits() -> None:
    assert chapter_signature("http://a/book/12.html") == chapter_signature("http://a/book/3.html")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("下一页", "page"),
        ("下一頁", "page"),
        ("Next Page", "page"),
        ("下一章", "chapter"),
        ("下一节", "chapter"),
        ("Next Chapter", "chapter"),
        ("目录", "none"),
        ("", "none"),
    ],
)
def test_classify_next(text: str, expected: str) -> None:
    assert classify_next(text) == expected


def test_find_next_page_follows_pagination_only() -> None:
    base = "http://example.test/book/2.html"
    paginated = parse_html(
        "<div class='nav'><a href='/book/'>目录</a>"
        "<a href='/book/2_2.html'>下一页</a>"
        "<a href='/book/3.html'>下一章</a></div>",
        base_url=base,
    )
    assert find_next_page(paginated, base, base) == "http://example.test/book/2_2.html"

    last = parse_html(
        "<div><a href='/book/3.html'>下一章</a><a href='http://other.test/x'>下一页</a></div>",
        base_url=base,
    )
    assert find_next_page(last, base, base) is None


def test_find_next_page_honours_rel_next_and_selector() -> None:
    base = "http://example.test/book/1.html"
    doc = parse_html('<a rel="next" href="/book/1_2.html">继续</a>', base_url=base)
    assert find_next_page(doc, base, base) == "http://example.test/book/1_2.html"
    shaped = parse_html(
        '<div class="pager"><a href="/book/1_3.html">继续阅读</a></div>', base_url=base
    )
    assert (
        find_next_page(shaped, base, base, selector="div.pager a")
        == "http://example.test/book/1_3.html"
    )
    assert find_next_page(shaped, base, base, selector="div.absent a") is None


def test_find_next_page_ignores_current_url_and_document_links() -> None:
    base = "http://example.test/book/1.html"
    doc = parse_html(
        "<a href='/book/1.html#top'>下一页</a><a href='mailto:a@b.c'>下一页</a>",
        base_url=base,
    )
    assert find_next_page(doc, base, base) is None


def test_looks_like_catalogue_needs_two_chapters() -> None:
    single = (ChapterLink("第一章", "http://a/1"),)
    assert not looks_like_catalogue(single)
    assert looks_like_catalogue((*single, ChapterLink("第二章", "http://a/2")))


WIKI_PAGE = """
<html><body>
<div id="mw-panel"><ul>
  <li><a href="/wiki/Special:AllPages">Special:AllPages</a></li>
  <li><a href="/wiki/Help:%E7%9B%AE%E5%BD%95">Help:目录</a></li>
  <li><a href="/wiki/Wikisource:%E5%85%B3%E4%BA%8E">Wikisource:关于</a></li>
  <li><a href="/wiki/Category:%E5%B0%8F%E8%AF%B4">Category:小说</a></li>
</ul></div>
<div class="mw-parser-output"><h1>第一回 甄士隱夢幻識通靈</h1>
<p>此開卷第一回也。作者自云：因曾歷過一番夢幻之后，故將真事隱去。</p></div>
<div class="noprint"><a href="/wiki/%E7%B4%85%E6%A8%93%E5%A4%A2/%E7%AC%AC002%E5%9B%9E">下一回</a></div>
</body></html>
"""


def test_wiki_sidebar_is_not_mistaken_for_a_catalogue() -> None:
    """实测回归：维基文库章节页的侧栏导航不能被当成目录（见设计 §37.4）。"""
    doc = parse_html(WIKI_PAGE, base_url="https://zh.wikisource.org/wiki/book/1")
    links = discover_chapters(doc, "https://zh.wikisource.org/wiki/book/1")
    assert links == ()
    assert not looks_like_catalogue(links)


def test_unnumbered_links_need_a_hinted_container_or_a_pattern() -> None:
    plain = parse_html(
        "<ul><li><a href=/x>侧栏一</a></li><li><a href=/y>侧栏二</a></li>"
        "<li><a href=/z>侧栏三</a></li></ul>",
        base_url="http://example.test/",
    )
    assert discover_chapters(plain, "http://example.test/") == ()
    patterned = parse_html(
        "<ul id=chapter-list>"
        + "".join(f"<li><a href=/book/{i}.html>第{i}话 相遇</a></li>" for i in range(1, 5))
        + "</ul>",
        base_url="http://example.test/",
    )
    assert len(discover_chapters(patterned, "http://example.test/")) == 4


def test_sidebar_links_do_not_consume_the_chapter_budget() -> None:
    """实测回归：扫描预算被侧栏吃掉会让真正的目录扫不到（设计 §37.4）。"""
    sidebar = "".join(f"<li><a href=/site/page{i}>站点栏目{i}</a></li>" for i in range(1, 9))
    chapters = "".join(f"<li><a href=/book/{i}.html>第{i}回 正文</a></li>" for i in range(1, 121))
    doc = parse_html(
        f"<div class=sidebar><ul>{sidebar}</ul></div><ul id=chapter-list>{chapters}</ul>",
        base_url="http://example.test/",
    )
    links = discover_chapters(doc, "http://example.test/", limit=4)
    assert [link.number for link in links] == [1, 2, 3, 4]


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ('<a href="/book/2_2.html">下一页</a>', "http://example.test/book/2_2.html"),
        ('<a rel="next" href="/book/2_2.html">→</a>', "http://example.test/book/2_2.html"),
        ('<a rel="next" href="/book/3.html">Next</a>', None),
        ('<a rel="next" href="/book/10.html">Next</a>', None),
        ('<link rel="next" href="/book/2_2.html">', "http://example.test/book/2_2.html"),
        ('<a rel="next" href="/book/3.html">下一章</a>', None),
        ('<a rel="next" href="/book/2_2.html">下一章</a>', None),
    ],
)
def test_rel_next_only_follows_real_continuations(html: str, expected: str | None) -> None:
    """实测回归：rel=next 指向下一章时不能当成当前章的分页（设计 §37.4）。"""
    base = "http://example.test/book/2.html"
    doc = parse_html(html, base_url=base)
    assert find_next_page(doc, base, base) == expected


def test_rel_next_query_pagination_is_followed() -> None:
    base = "http://example.test/read?id=9"
    doc = parse_html('<a rel="next" href="/read?id=9&page=2">→</a>', base_url=base)
    assert find_next_page(doc, base, base) == "http://example.test/read?id=9&page=2"
