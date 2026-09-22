"""Explicit cross-site reader links allowed in known public catalogs,
plus shared "this URL is not a chapter" predicates (namespace/category).
"""

import re
from urllib.parse import urlsplit

#: 分类/导航路径：指向它们的链接不是章节（实测 quanben 等站的 /c/*.html
#: 分类链接会混进章节下拉）。章节 URL 几乎不会落在这些段下。
_CATEGORY_PATH = re.compile(
    r"^/(?:c|cat|cats|category|categories|sort|sorts|fenlei|type|types|tag|tags|"
    r"rank|paihang|search|author|zuozhe)(?:/|\.html?$|$)",
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
