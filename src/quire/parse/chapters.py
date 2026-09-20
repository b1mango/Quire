"""章节发现与分页跟随：纯函数，无 I/O（项目设计.md §6.6、§37.4）。

两件事决定小说能不能用：

* **目录识别**：目录页里有大量同源链接（首页、分类、排行、上一章），
  全都当章节就会抓出一堆垃圾。这里用"链接文本像不像章节 + URL 模式
  是否成组"两道筛子，成组才认为找到了目录，否则明确报错让用户写规则。
* **分页 vs 分章**："下一页"要继续拼正文，"下一章"必须停。混淆的后果
  是把整本书塞进第一章，或者每章只抓到第一页——所以这里先判"下一章"，
  再判"下一页"，并且只跟随同源链接，用 visited 集防环。
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from ..text.clean import chapter_number, has_chapter_mark
from ..utils.urls import is_usable_url, route_fragment
from ..utils.urls import join_document_url as join_url
from .catalog_links import is_linked_reader
from .minidom import Document, Node

#: 明确不是章节的导航链接文本。
NAV_WORDS = frozenset(
    {
        "首页",
        "主页",
        "目录",
        "章节目录",
        "返回目录",
        "上一章",
        "下一章",
        "上一页",
        "下一页",
        "尾页",
        "末页",
        "返回",
        "书架",
        "登录",
        "注册",
        "排行",
        "排行榜",
        "分类",
        "搜索",
        "作家",
        "作者",
        "投推荐票",
        "加入书签",
        "手机版",
        "电脑版",
        "顶部",
        "底部",
        "close",
        "more",
    }
)

#: 继续拼正文的提示词（先判"下一章"，再判这些）。
NEXT_PAGE_WORDS = ("下一页", "下一頁", "下页", "次页", "后一页", "nextpage")
#: 结束本章的提示词。
NEXT_CHAPTER_WORDS = (
    "下一章",
    "下一節",
    "下一节",
    "下一回",
    "下一话",
    "下一話",
    "下章",
    "nextchapter",
)

#: 目录链接可能出现的容器类名/标签（提高召回，不单独作为判据）。
_LIST_HINTS = re.compile(
    r"(?:^|[\s_-])(chapter|chapters|list|listmain|catalog|mulu|zhangjie|volume|dir)\b", re.I
)

_DIGITS = re.compile(r"\d+")
_WS = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class ChapterLink:
    """目录里的一个章节。``number`` 为 None 表示标题里没有章节号。"""

    title: str
    url: str
    number: int | None = None


def chapter_signature(url: str) -> str:
    """URL 的数字段换成 ``#``：同一本书的章节链接会得到同一签名。"""
    return _DIGITS.sub("#", url)


def classify_next(text: str) -> str:
    """链接文本表示什么：``page``（继续本章）/ ``chapter``（下一章）/ ``none``。"""
    value = _WS.sub("", text).lower()  # 去掉所有空白后再匹配，兼容中英混排
    if not value:
        return "none"
    if any(word in value for word in NEXT_CHAPTER_WORDS):
        return "chapter"
    if any(word in value for word in NEXT_PAGE_WORDS):
        return "page"
    return "none"


def discover_chapters(
    doc: Document, base_url: str, *, selector: str | None = None, limit: int = 2000
) -> tuple[ChapterLink, ...]:
    """从目录页发现章节，返回阅读顺序。

    分三档，越往下越保守：

    * 用户给了 ``--chapter-selector``：保持文档顺序，仍检查来源和导航；
    * 标题带章节号/章节标记的强候选：≥2 个就采用；
    * 只是"位于章节目录容器里"的弱候选：必须 URL 模式成组才采用。

    后两条分界来自实测：维基文库的章节页侧栏有几十个站点导航链接，全都
    "在 <ul> 里"，不分组就会把整站导航当成目录。宁可不认，也不能抓错。
    """
    base = join_url(base_url, doc.effective_base()) or base_url
    materialized = doc.select_one("#quire-catalogue")
    if materialized is not None and urlsplit(base_url).hostname == "t.shuqi.com":
        selector = "#quire-catalogue a"
    anchors = _anchors(doc, selector)
    found: list[ChapterLink] = []
    strong: list[ChapterLink] = []
    weak: list[ChapterLink] = []
    for anchor in anchors:
        url = join_url(base, anchor.get("href") or "")
        linked_reader = is_linked_reader(base_url, url or "")
        if not url or not is_usable_url(url) or not (_same_origin(base_url, url) or linked_reader):
            continue
        if _namespace_link(url):
            continue
        title = _WS.sub(" ", anchor.text).strip()
        if not title or len(title) > 60 or title in NAV_WORDS:
            continue
        number = chapter_number(title)
        if number is None and classify_next(title) != "none":
            continue
        link = ChapterLink(title=title, url=url, number=number)
        found.append(link)
        if number is not None or has_chapter_mark(title):
            strong.append(link)
        elif selector is not None or _in_list(anchor):
            weak.append(link)

    # 截断放在排序之后：扫描阶段的"预算"会被侧栏链接吃掉，
    # 实测中它会让真正的章节列表还没扫到就停止。
    if selector is not None:
        return tuple({link.url: link for link in found}.values())[:limit]
    chosen = strong if len(strong) >= 2 else weak
    # 最新章节通常在完整目录前重复出现；以后面的阅读清单位置为准。
    chosen = list({link.url: link for link in reversed(chosen)}.values())[::-1]
    grouped = _modal_group(chosen)
    if grouped and all(is_linked_reader(base_url, link.url) for link in grouped):
        grouped.sort(key=lambda link: int(urlsplit(link.url).path.rsplit("/", 1)[-1]))
        return tuple(grouped[:limit])
    return tuple(_sort_chapters(grouped)[:limit])


def find_next_page(
    doc: Document, base_url: str, current_url: str, *, selector: str | None = None
) -> str | None:
    """本章的下一页地址；没有足够证据时返回 None。

    证据分三档（设计 §37.4）：

    1. 文本明确写"下一页/Next Page" → 跟随；
    2. 文本明确写"下一章/Next Chapter" → 绝不跟随；
    3. 只有 ``rel=next``、文本是箭头或 "Next" 这类含糊写法 → **只在地址看起来
       像同一章的续页时**才跟随。

    第 3 条是实测逼出来的：很多站点在 ``<head>`` 里放
    ``<link rel="next" href="下一章">``（WordPress 类），或者把"下一章"的
    链接写成 ``<a rel="next">Next</a>``。只看 ``rel`` 就会把整本书当成
    第一章的分页，重复抓进同一章且没有任何报错。
    """
    base = join_url(base_url, doc.effective_base()) or base_url
    if selector:
        # 显式选择器允许"继续阅读"等文本，仍受同源与下一章边界约束。
        node = doc.select_one(selector)
        anchor = _anchor(node)
        return _next_url(anchor, base, current_url) if anchor is not None else None
    for anchor in _next_candidates(doc):
        url = _next_url(anchor, base, current_url)
        if url is None:
            continue
        if classify_next(anchor.text) == "page":
            return url
        if _looks_like_continuation(current_url, url):
            return url
    return None


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


def _same_origin(left: str, right: str) -> bool:
    try:
        a, b = urlsplit(left), urlsplit(right)
        ports = {"http": 80, "https": 443}
        return (
            a.scheme in ports
            and a.hostname is not None
            and (a.scheme, a.hostname, a.port if a.port is not None else ports.get(a.scheme))
            == (b.scheme, b.hostname, b.port if b.port is not None else ports.get(b.scheme))
        )
    except ValueError:
        return False


PageKey = tuple[str, str | None, int, str, str, str]


def page_key(url: str) -> PageKey:
    """文档身份保留 hash 路由、忽略普通锚点，统一默认端口。"""
    parts = urlsplit(url)
    return (
        parts.scheme,
        parts.hostname,
        parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80),
        parts.path or "/",
        parts.query,
        route_fragment(url),
    )


def _anchor(node: Node | None) -> Node | None:
    if node is None:
        return None
    if node.tag == "a":
        return node
    return node.select_one("a")


def _next_url(anchor: Node, base: str, current_url: str) -> str | None:
    if classify_next(anchor.text) == "chapter":
        return None
    url = join_url(base, anchor.get("href") or "")
    if (
        not url
        or url == current_url
        or not is_usable_url(url)
        or not _same_origin(current_url, url)
        or route_fragment(current_url) != route_fragment(url)
        and not _looks_like_continuation(current_url, url)
    ):
        return None
    before = parse_qs(urlsplit(current_url).query, keep_blank_values=True)
    after = parse_qs(urlsplit(url).query, keep_blank_values=True)
    if {k: v for k, v in before.items() if k.lower() not in _PAGE_PARAMS} != {
        k: v for k, v in after.items() if k.lower() not in _PAGE_PARAMS
    }:
        return None
    return url


def looks_like_catalogue(links: tuple[ChapterLink, ...]) -> bool:
    """至少两个章节才算目录页；单章页面按"只抓这一章"处理。"""
    return len(links) >= 2


def _anchors(doc: Document, selector: str | None) -> list[Node]:
    if selector:
        out: list[Node] = []
        for node in doc.select(selector):
            anchor = node if node.tag == "a" else node.select_one("a")
            if anchor is not None and anchor.get("href"):
                out.append(anchor)
        return out
    return [node for node in doc.iter_elements() if node.tag == "a" and node.get("href")]


def _next_candidates(doc: Document) -> list[Node]:
    out: list[Node] = []
    for node in doc.select("link[rel~=next]"):
        out.append(node)
    for node in doc.iter_elements():
        if node.tag != "a":
            continue
        rel = set(node.attr_list("rel"))
        if (
            rel & {"next"}
            or classify_next(node.text) != "none"
            or node.text.strip().lower() in {"next", "→", "›", "»", ">"}
        ):
            out.append(node)
    return out


def _in_list(anchor: Node) -> bool:
    """链接是否位于**像章节列表**的容器里。

    只认类名/标题里明确写着 chapter/list/目录 的容器，不认任意 ``<ul>``——
    站点侧栏、页脚、语言切换全都用 ``<ul>``，认了就会把整站导航当目录。
    """
    node: Node | None = anchor
    while node is not None:
        classes = " ".join(sorted(node.classes)) + " " + (node.get("id") or "")
        if _LIST_HINTS.search(classes):
            return True
        node = node.parent
    return False


def _namespace_link(url: str) -> bool:
    """MediaWiki 风格的命名空间链接（``/wiki/Special:AllPages``）不是章节。"""
    path = urlsplit(url).path.rstrip("/")
    tail = path.rsplit("/", 1)[-1]
    return ":" in tail


def _modal_group(links: list[ChapterLink]) -> list[ChapterLink]:
    """只保留 URL 模式最一致的那一组；没有成组证据时返回空（宁可不猜）。

    返回空会让上层退回"只抓这一页"，比把侧栏导航抓成一本书安全得多。
    """
    if len(links) < 3:
        return links
    groups: dict[str, list[ChapterLink]] = {}
    for link in links:
        groups.setdefault(chapter_signature(link.url), []).append(link)
    if len(groups) == 1:
        return links
    best = max(groups.values(), key=len)
    return best if len(best) >= 2 else []


def _sort_chapters(links: list[ChapterLink]) -> list[ChapterLink]:
    """全部带唯一章节号时按号排序，否则保持目录页的文档顺序。

    有些站点从最新一章往下排，纯文档顺序会倒着出书；但更多站点的目录
    本身就是阅读顺序。用"是否全部带唯一章节号"做判据，只在能证明顺序
    时才重排，其余情况不猜——猜错就是把整本书的顺序打乱。
    """
    numbers = [link.number for link in links]
    if links and all(number is not None for number in numbers) and len(set(numbers)) == len(links):
        return sorted(links, key=lambda link: link.number or 0)
    return links
