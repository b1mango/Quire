"""8book 跨域宿主适配:目录物化与浏览器主机白名单。"""

from quire.fetch.browser_catalogue import ready_script
from quire.fetch.browser_native import allowed_hosts
from quire.parse.chapters import discover_chapters
from quire.parse.minidom import parse
from quire.sites import eightbook
from quire.sites.cleanup import clean_document


def test_ready_script_dispatch_by_page_kind():
    assert "quire-catalogue" in ready_script("https://8book.com/novelbooks/12345/")
    assert "#text" in ready_script("https://8book.com/read/12345/?2702643")
    # 伪装域续页(分页渲染直接导航过去)也要就绪判定
    assert "#text" in ready_script("https://finance.binaccount.com/read/12345/?2702643_2")
    assert ready_script("https://8book.com/read/12345/") is None  # 无章节 id 不干预
    assert ready_script("https://8book.com/novelbooks/12345/1/") is None
    assert ready_script("https://evil8book.com/novelbooks/12345/") is None
    assert ready_script("https://finance.binaccount.com/books/12345/") is None


def test_materialized_catalogue_discovers_same_origin_chapters_in_order():
    html = (
        '<section id="quire-catalogue">'
        '<a href="/read/12345/?2702644">第二章 下山</a>'
        '<a href="/read/12345/?2702643">第一章 上山</a>'
        '<a href="/read/12345/?2702645">第三章 进城</a>'
        "</section>"
    )
    links = discover_chapters(parse(html), "https://8book.com/novelbooks/12345/")
    # discover_chapters 按章节标题里的序数重排,与 DOM 顺序无关。
    assert [link.url for link in links] == [
        f"https://8book.com/read/12345/?270264{n}" for n in (3, 4, 5)
    ]
    assert links[0].title == "第一章 上山"


def test_allowed_hosts_admits_content_domain_only_for_8book():
    hosts = allowed_hosts("https://8book.com/novelbooks/12345/")
    assert "finance.binaccount.com" in hosts
    assert "www.8book.com" in hosts
    assert "evil.binaccount.com" not in hosts
    chapter = allowed_hosts("https://8book.com/read/12345/?2702643")
    assert "finance.binaccount.com" in chapter
    # 伪装域续页:放行脚本来源域
    mirror = allowed_hosts("https://finance.binaccount.com/read/12345/?2702643_2")
    assert "8book.com" in mirror and "www.8book.com" in mirror
    other = allowed_hosts("https://example.test/")
    assert "finance.binaccount.com" not in other


def test_content_mirror_accepts_only_measured_redirect():
    source = "https://8book.com/read/98986/?2702643"
    assert eightbook.is_content_mirror(source, "https://finance.binaccount.com/read/98986/?2702643")
    # 分页续页:查询带 _N 后缀
    assert eightbook.is_content_mirror(
        source, "https://finance.binaccount.com/read/98986/?2702643_2"
    )
    # 其他章节/其他书/其他路径/其他域一律不认
    assert not eightbook.is_content_mirror(
        source, "https://finance.binaccount.com/read/98986/?2702644"
    )
    assert not eightbook.is_content_mirror(
        source, "https://finance.binaccount.com/read/11111/?2702643"
    )
    assert not eightbook.is_content_mirror(
        source, "https://finance.binaccount.com/other/98986/?2702643"
    )
    assert not eightbook.is_content_mirror(source, "https://evil.test/read/98986/?2702643")
    assert not eightbook.is_content_mirror(
        "https://example.test/read/98986/?2702643",
        "https://finance.binaccount.com/read/98986/?2702643",
    )


def test_clean_document_removes_nav_and_watermark_keeps_prose():
    html = (
        '<div id="content">'
        '<div id="subtitle">第一卷 第一章 時空機器</div>'
        '<span class="prev"><a href="?2702642">上一篇</a></span>'
        '<span class="chmenus"><a href="/novelbooks/98986">章節列表</a></span>'
        '<span class="next"><a href="?2702644">下一篇</a></span>'
        '<div id="text"><p>'
        '<span class="read_spans">項少龍抬頭看見遠山如黛,風從林間穿過,心裡忽然安靜下來.</span><br>'
        '<span class="read_spans">8ВｏΟk·СΟm</span><br>'
        '<span class="read_spans">他繼續往前走去,直到天色將晚才停下腳步.</span><br>'
        '<span class="read_spans">⒏ьoОｋ·Ｃом</span><br>'
        '<span class="read_spans">８ｂＯＯK。сοm</span><br>'
        "</p></div></div>"
    )
    for url in (
        "https://8book.com/read/98986/?2702643",
        "https://finance.binaccount.com/read/98986/?2702643",
    ):
        doc = parse(html)
        clean_document(doc, url)
        text = doc.text
        assert "章節列表" not in text and "上一篇" not in text and "下一篇" not in text
        assert (
            "8ВｏΟk·СΟm" not in text and "⒏ьoОｋ·Ｃом" not in text and "８ｂＯＯK。сοm" not in text
        )
        assert "項少龍抬頭看見遠山如黛" in text and "他繼續往前走去" in text


def test_clean_document_leaves_other_sites_untouched():
    html = '<div><span class="prev">上一篇</span><p>8ВｏΟk·СΟm</p></div>'
    doc = parse(html)
    clean_document(doc, "https://example.test/read/1/?2")
    assert "上一篇" in doc.text and "8ВｏΟk·СΟm" in doc.text


def test_navigation_referer_only_for_content_mirror_pages():
    assert (
        eightbook.navigation_referer("https://finance.binaccount.com/read/98986/?2702787_2")
        == "https://8book.com/"
    )
    assert eightbook.navigation_referer("https://8book.com/read/98986/?2702787") is None
    assert eightbook.navigation_referer("https://finance.binaccount.com/other/") is None
    assert eightbook.navigation_referer("https://example.test/read/1/?2") is None


def test_decoy_marker_catches_tax_blog_only_on_mirror_reads():
    url = "https://finance.binaccount.com/read/98986/?2702787_2"
    assert eightbook.decoy_marker(url, "<html><title>稅美人</title></html>") is not None
    assert eightbook.decoy_marker(url, '<div id="text" class="text"></div>') is None
    # 非阅读页地址不判定
    assert eightbook.decoy_marker("https://finance.binaccount.com/", "稅美人") is None
    assert eightbook.decoy_marker("https://example.test/read/1/?2", "稅美人") is None


def test_no_plain_http_rewrite():
    """章节正文经站点内嵌算法派生的 /txt/ 接口提供(2026-09-22 实测);

    本适配只物化目录 + 白名单,不做纯 HTTP 改写——阅读页骨架不含正文,
    直接抓章节地址只会拿到空壳。
    """
    assert not hasattr(eightbook, "rewrite_chapter")
