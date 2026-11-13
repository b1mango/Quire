"""Explicit cross-site reader links allowed in known public catalogs,
plus shared "this URL is not a chapter" predicates (namespace/category).
"""

import re
from typing import Protocol
from urllib.parse import urlsplit


class _Link(Protocol):
    @property
    def title(self) -> str: ...
    @property
    def url(self) -> str: ...


#: 分类/导航路径：指向它们的链接不是章节（实测 quanben 等站的 /c/*.html
#: 分类链接会混进章节下拉）。必须是子路径或 .html 页面，裸的 "/c" 不算。
_CATEGORY_PATH = re.compile(
    r"^/(?:c|cat|cats|category|categories|sort|sorts|fenlei|type|types|tag|tags|"
    r"rank|paihang|search|author|zuozhe)(?:/|\.html?$)",
    re.I,
)


def is_category_link(url: str) -> bool:
    """分类/导航路径(/c/…、/category/…、/tag/… 等)不是章节。"""
    return _CATEGORY_PATH.match(urlsplit(url).path) is not None


def is_namespace_link(url: str) -> bool:
    """MediaWiki 风格的命名空间链接（``/wiki/Special:AllPages``）不是章节。"""
    path = urlsplit(url).path.rstrip("/")
    tail = path.rsplit("/", 1)[-1]
    return ":" in tail


def is_linked_reader(base_url: str, url: str) -> bool:
    target = urlsplit(url)
    return (
        urlsplit(base_url).hostname in {"www.gugu5.cc", "gugu5.cc"}
        and target.hostname == "ac.qq.com"
        and bool(re.fullmatch(r"/ComicView/index/id/\d+/cid/\d+", target.path))
    )


_DIGIT_RUN = re.compile(r"\d+")


def pick_source[T: _Link](links: list[T]) -> list[T]:
    """多线路目录只留一条线路。

    苹果 CMS（MacCMS / stui 模板）等站把同一部作品放在多个「线路」标签里，章节地址形如
    ``/chapter/<作品>-<线路>-<话>.html``：签名相同，通用分组会把几条线路混成一本、章节重复。
    找出一个把链接分成 2–8 组的数字位，若组间标题大量重合（≥ 一半）就是并行线路，
    取章节最多的一条（并列取先出现的）。分卷目录各卷标题不同，不受影响。
    """
    if len(links) < 6:
        return links
    runs = [_DIGIT_RUN.findall(urlsplit(link.url).path) for link in links]
    width = len(runs[0])
    if width < 2 or any(len(r) != width for r in runs):
        return links
    for index in range(width):
        groups: dict[str, list[T]] = {}
        for link, run in zip(links, runs, strict=True):
            groups.setdefault(run[index], []).append(link)
        if not 2 <= len(groups) <= 8 or min(map(len, groups.values())) < 3:
            continue
        ranked = sorted(groups.values(), key=len, reverse=True)
        best = {link.title for link in ranked[0]}
        if all(len({x.title for x in g} & best) * 2 >= len(g) for g in ranked[1:]):
            return ranked[0]
    return links
