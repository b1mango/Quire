"""同一章续页的地址判定（x.html → x_2.html、?page=2 等），供章节分页跟随使用。"""

from __future__ import annotations

import posixpath
import re
from urllib.parse import parse_qs, urlsplit

from ..utils.urls import route_fragment

#: 正文里的页码标记：「第(1/3)页」「(1/3)」「1/3页」。
PAGE_MARK = re.compile(
    r"第?\s*[（(]\s*\d{1,3}\s*/\s*\d{1,3}\s*[）)]\s*[页頁]?|\d{1,3}\s*/\s*\d{1,3}\s*[页頁]"
)

_PAGE_PARAMS = frozenset({"page", "p", "pg", "pageno", "page_num", "pageindex"})
_PAGE_SUFFIX = re.compile(r"^(.*?)([_-])([0-9]+)$")


def _looks_like_continuation(current_url: str, candidate: str) -> bool:
    """候选地址是否像"当前页的下一页"，而不是"下一个文档"。

    路径分页保持目录、章节根名与扩展名，页号递增一。已带后缀时要求
    章节根名以数字结尾，避免 chapter_2 → chapter_3 串章。query 分页只允许
    一个明确分页参数递增，其余参数（包括重复值和空值）必须保持。
    """
    current, target = urlsplit(current_url), urlsplit(candidate)
    first_route, next_route = route_fragment(current_url), route_fragment(candidate)
    if first_route != next_route:
        if (
            not first_route
            or not next_route
            or (current.path, current.query) != (target.path, target.query)
        ):
            return False
        current, target = (
            urlsplit(first_route.removeprefix("!")),
            urlsplit(next_route.removeprefix("!")),
        )
    before = parse_qs(current.query, keep_blank_values=True)
    after = parse_qs(target.query, keep_blank_values=True)
    if current.path == target.path:
        keys = {key for key in before.keys() | after.keys() if key.lower() in _PAGE_PARAMS}
        # 裸 query 后缀分页:?2702787 → ?2702787_2(8book 实测),同根且页号递增一。
        old_q = _PAGE_SUFFIX.fullmatch(current.query)
        new_q = _PAGE_SUFFIX.fullmatch(target.query)
        if not keys and new_q is not None:
            if old_q is None:
                return bool(current.query) and current.query == new_q[1] and int(new_q[3]) == 2
            return old_q[1] == new_q[1] and int(old_q[3]) + 1 == int(new_q[3])
        if len(keys) != 1:
            return False
        key = keys.pop()
        old, new = before.pop(key, ["1"]), after.pop(key, [])
        return (
            before == after
            and len(old) == len(new) == 1
            and re.fullmatch(r"[0-9]+", old[0]) is not None
            and re.fullmatch(r"[0-9]+", new[0]) is not None
            and 0 < int(old[0]) < int(new[0]) == int(old[0]) + 1
        )
    stem, ext = posixpath.splitext(current.path)
    ahead, ahead_ext = posixpath.splitext(target.path)
    old_page, new_page = _PAGE_SUFFIX.fullmatch(stem), _PAGE_SUFFIX.fullmatch(ahead)
    if before != after or ext != ahead_ext or new_page is None:
        return False
    if old_page is None:
        return new_page[1] == stem and int(new_page[3]) == 2
    return (
        re.search(r"[0-9]$", old_page[1]) is not None
        and old_page.group(1, 2) == new_page.group(1, 2)
        and 0 < int(old_page[3]) < int(new_page[3]) == int(old_page[3]) + 1
    )
