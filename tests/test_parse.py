from __future__ import annotations

import pytest
from lxml import html

from quire.image.probe import ImageProbe
from quire.parse.images import (
    Candidate,
    FilterPolicy,
    collect,
    order_candidates,
    parse_srcset,
    postfilter,
    prefilter,
)
from quire.parse.minidom import SelectorError, parse


@pytest.mark.parametrize(
    "selector,xpath",
    [
        ("main > img", "//main/img"),
        ("main img", "//main//img"),
        ("#reader > div img", "//*[@id='reader']/div//img"),
        ("img[data-key='ABC']", "//img[@data-key='ABC']"),
        ("img[data-key^='A']", "//img[starts-with(@data-key, 'A')]"),
        ("img, main > div", "//img | //main/div"),
        ("main > div img[data-key]", "//main/div//img[@data-key]"),
    ],
)
def test_selectors_match_lxml(selector, xpath):
    markup = (
        '<main id="reader"><img id="a" data-key="ABC"><div id="group">'
        '<div><img id="b" data-key="abc"></div></div></main><img id="outside">'
    )
    expected = [node.get("id") for node in html.fromstring(markup).xpath(xpath)]
    assert [node.get("id") for node in parse(markup).select(selector)] == expected


@pytest.mark.parametrize(
    "selector", ["", "img >", "> img", "main >> img", "img:first-child", "img + img"]
)
def test_invalid_selector_fails_even_empty_document(selector):
    with pytest.raises(SelectorError):
        parse("").select(selector)


def test_nested_ancestor_backtracking():
    doc = parse('<a><b id="yes"><b><c></c></b></b></a>')
    assert len(doc.select("a > b c")) == 1


def test_quoted_attribute_punctuation_and_invalid_groups():
    doc = parse('<img data-key="a],b" id="page">')
    assert doc.select_one('[data-key="a],b"]').get("id") == "page"
    for selector in ["img,", ",img", "div*", "[data-key=]", "img ]"]:
        with pytest.raises(SelectorError):
            doc.select(selector)


def test_ancestor_states_are_not_repeated():
    doc = parse("<div>" * 80 + "<img>" + "</div>" * 80)
    assert doc.select("main div div div div div div div img") == []


def test_base_text_and_implicit_closure():
    doc = parse(
        '<base href="../assets/"><title>卷帙</title><p><b>Hello</b> <i>world</i><p>next',
        base_url="https://example.org/ch/1",
    )
    assert doc.title == "卷帙"
    assert doc.effective_base() == "https://example.org/assets/"
    assert [n.text for n in doc.select("p")] == ["Hello world", "next"]
    assert doc.select_one("b").closest("p").text == "Hello world"


def test_scoped_candidates_order_fallback_and_referer():
    doc = parse(
        '<main><div style="background:url(1.jpg)"></div>'
        '<img data-src="2.jpg" src="thumb.jpg">'
        '<img data-srcset="3.jpg 1000w, small.jpg 300w"></main>'
        '<aside style="background:url(outside.jpg)"><a href="other.jpg"></a></aside>',
        base_url="https://example.org/read",
    )
    candidates = collect(doc, doc.effective_base(), selector="main")
    assert [c.url for c in candidates] == [f"https://example.org/{n}.jpg" for n in range(1, 4)]
    assert candidates[1].alternatives == ("https://example.org/thumb.jpg",)
    assert all(c.referer == "https://example.org/read" for c in candidates)


def test_filter_does_not_reject_long_vertical_comics_or_small_compressed_pages():
    candidate = Candidate("https://example.org/page.png?token=ad-secret", 0)
    probe = ImageProbe("png", 800, 16000, 1, 1, False, True)
    assert prefilter([candidate, candidate])[0] == [candidate]
    assert postfilter([(candidate, probe, 200)])[0] == [candidate]
    assert postfilter([(candidate, probe, 200)], FilterPolicy(min_bytes=300))[1]


def test_order_is_explicit_and_auto_preserves_dom():
    candidates = [Candidate(f"https://example.org/{n}.jpg", i) for i, n in enumerate([10, 2, 1])]
    assert order_candidates(candidates)[0] == candidates
    assert [c.url.rsplit("/", 1)[1] for c in order_candidates(candidates, "asc")[0]] == [
        "1.jpg",
        "2.jpg",
        "10.jpg",
    ]
    assert order_candidates(candidates, "desc")[0] == candidates
    assert parse_srcset("1.jpg 1x, 2.jpg 2x")[-1] == ("2.jpg", 2000)


@pytest.mark.parametrize(
    "selector,expected",
    [
        ("[class~=wide]", True),
        ("[class~=narrow]", False),
        ("[lang|=en]", True),
        ("[lang|=fr]", False),
        ("[data-x$=tail]", True),
        ("[data-x$=head]", False),
        ("[data-x*=middle]", True),
        ("[data-x*=absent]", False),
        ("[data-x^=head]", True),
        ("[data-x^=tail]", False),
        ("img.wide", True),
        ("img.narrow", False),
    ],
)
def test_attribute_selectors(selector, expected):
    doc = parse('<img class="wide tall" lang="en-US" data-x="head-middle-tail">')
    assert bool(doc.select(selector)) is expected


def test_collection_other_sources_and_filters():
    picture = parse(
        '<picture><source srcset="large.jpg 2x, small.jpg 1x"><img src="fallback.jpg"></picture>',
        base_url="https://x/",
    )
    assert collect(picture, picture.base_url)[0].url == "https://x/large.jpg"
    doc = parse('<a href="p.jpg">page</a><img src="data:bad">', base_url="https://x/")
    assert collect(doc, doc.base_url)[0].source == "anchor"
    css = parse('<style>.page{background:url("p.png")}</style>', base_url="https://x/")
    assert collect(css, css.base_url)[0].source == "style"
    c = Candidate("https://x/p.png", 1)
    assert not prefilter([Candidate("file:///p.png", 0)])[0]
    assert not prefilter([Candidate("https://x/logo.png", 0)])[0]
    assert not postfilter([(c, ImageProbe(), 0)])[0]
    assert not postfilter([(c, ImageProbe("png", 10, 10, complete=True), 200)])[0]
    assert not postfilter([(c, ImageProbe("png", 9000, 200, complete=True), 200)])[0]
    assert not postfilter([(c, ImageProbe("png", 400, 600, complete=False), 200)])[0]
    assert order_candidates([c], "dom")[0] == [c]
    assert order_candidates([c], "unknown")[1]
    assert order_candidates([c])[0] == [c]
    assert parse_srcset(" ,p.jpg badw") == [("p.jpg", 1)]
