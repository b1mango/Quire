"""正文容器边界与评论排除的聚焦回归。"""

from __future__ import annotations

import pytest

from quire.parse.article import (
    best_container,
    container_stats,
    extract_article,
    link_density,
)
from quire.parse.minidom import parse

PROSE = tuple(
    f"第{index}段，他沿着河岸慢慢走去，远处的灯火渐渐亮起，晚风吹散了白日的暑气。"
    for index in range(10)
)
COMMENTS = "".join(
    f"<p>读者{index}的评论，这一章写得很好，希望早点更新。</p>" for index in range(40)
)


def _paragraphs(paragraphs: tuple[str, ...]) -> str:
    return "".join(f"<p>{paragraph}</p>" for paragraph in paragraphs)


@pytest.mark.parametrize("selector", [None, "#content"])
@pytest.mark.parametrize(
    "attribute",
    [
        'class="comments"',
        'id="comments"',
        'class="widget comment-list"',
        'id="comment-42"',
        'id="comment_area"',
        'class="reader-comments"',
        'id="replies"',
        'class="REPLY"',
    ],
)
def test_nested_comments_are_excluded_from_text_and_metrics(
    attribute: str, selector: str | None
) -> None:
    doc = parse(
        f'<body><div id="content">{_paragraphs(PROSE)}'
        f'<div {attribute}>{COMMENTS}<a href="/user">读者主页</a></div>'
        "<p>故事的最后一句，灯火熄灭了。</p></div></body>"
    )
    article = extract_article(doc, selector=selector)
    expected = (*PROSE, "故事的最后一句，灯火熄灭了。")
    assert article.paragraphs == expected
    assert article.link_density == 0.0
    content = doc.select_one("#content")
    assert content is not None
    assert container_stats(doc, content).chars == sum(map(len, expected))
    assert link_density(content) == 0.0


@pytest.mark.parametrize(
    ("opening", "closing"),
    [
        ('<div id="content">', "</div>"),
        ('<div class="content">', "</div>"),
        ('<div class="chapter-content">', "</div>"),
        ("<article>", "</article>"),
        ("<main>", "</main>"),
    ],
)
def test_trusted_boundary_excludes_sibling_comments_and_outer_text(
    opening: str, closing: str
) -> None:
    doc = parse(
        f"<body><div>站点欢迎语。</div>{opening}{_paragraphs(PROSE)}{closing}"
        f'<div class="comments">{COMMENTS}</div><div>站点附言。</div></body>'
    )
    assert extract_article(doc).paragraphs == PROSE


def test_specific_content_boundary_wins_over_semantic_page_wrappers() -> None:
    doc = parse(
        '<body><main><article><p>读者讨论。</p><div id="content">'
        f'{_paragraphs(PROSE)}</div><div class="comments">{COMMENTS}</div>'
        "<p>模板附言。</p></article></main></body>"
    )
    assert best_container(doc) is doc.select_one("#content")
    assert extract_article(doc).paragraphs == PROSE


@pytest.mark.parametrize("first_size", [5, 9])
@pytest.mark.parametrize("attribute", ['id="content"', 'class="layout"'])
def test_all_body_siblings_and_short_tail_are_preserved(attribute: str, first_size: int) -> None:
    tail = "尾段，他终于推开了家门。"
    doc = parse(
        f"<body><div {attribute}><section>{_paragraphs(PROSE[:first_size])}</section>"
        f"<section>{_paragraphs(PROSE[first_size:])}</section><p>{tail}</p></div>"
        f'<div class="comments">{COMMENTS}</div></body>'
    )
    assert extract_article(doc).paragraphs == (*PROSE, tail)


def test_multiple_named_body_siblings_keep_the_common_container_and_tail() -> None:
    tail = "尾段，雨停了。"
    doc = parse(
        f'<body><div><section class="chapter-content">{_paragraphs(PROSE[:9])}</section>'
        f'<section class="chapter-content">{_paragraphs(PROSE[9:])}</section>'
        f'<p>{tail}</p></div><div id="comments">{COMMENTS}</div></body>'
    )
    assert extract_article(doc).paragraphs == (*PROSE, tail)


@pytest.mark.parametrize("attribute", ['class="commentary"', 'id="uncommented"'])
def test_similar_names_do_not_remove_prose(attribute: str) -> None:
    doc = parse(f"<body><div {attribute}>{_paragraphs(PROSE)}</div></body>")
    assert extract_article(doc).paragraphs == PROSE


def test_semantic_content_inside_comments_is_not_a_candidate() -> None:
    doc = parse(
        f'<body><div class="layout">{_paragraphs(PROSE)}</div>'
        f'<div id="comments"><article><div id="content">{COMMENTS}</div></article></div>'
        "</body>"
    )
    assert extract_article(doc).paragraphs == PROSE


def test_empty_semantic_container_does_not_disable_automatic_extraction() -> None:
    doc = parse(
        f'<body><div id="content"></div><div class="layout">{_paragraphs(PROSE)}</div></body>'
    )
    assert extract_article(doc).paragraphs == PROSE
