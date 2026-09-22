"""8book 跨域宿主适配:目录物化与浏览器主机白名单。"""

from quire.fetch.browser_catalogue import ready_script
from quire.fetch.browser_native import allowed_hosts
from quire.parse.chapters import discover_chapters
from quire.parse.minidom import parse
from quire.sites import eightbook


def test_ready_script_dispatch_by_page_kind():
    assert "quire-catalogue" in ready_script("https://8book.com/novelbooks/12345/")
    assert "#text" in ready_script("https://8book.com/read/12345/?2702643")
    assert ready_script("https://8book.com/read/12345/") is None  # 无章节 id 不干预
    assert ready_script("https://8book.com/novelbooks/12345/1/") is None
    assert ready_script("https://evil8book.com/novelbooks/12345/") is None
    assert ready_script("https://finance.binaccount.com/read/12345/?2702643") is None


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
    assert "8book.com" in hosts
    assert "evil.binaccount.com" not in hosts
    chapter = allowed_hosts("https://8book.com/read/12345/?2702643")
    assert "finance.binaccount.com" in chapter
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


def test_no_plain_http_rewrite():
    """章节正文经站点内嵌算法派生的 /txt/ 接口提供(2026-09-22 实测);

    本适配只物化目录 + 白名单,不做纯 HTTP 改写——阅读页骨架不含正文,
    直接抓章节地址只会拿到空壳。
    """
    assert not hasattr(eightbook, "rewrite_chapter")
