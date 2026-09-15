"""迷你 DOM：stdlib ``html.parser`` 之上的容错树 + CSS 选择器子集。

为什么不用 lxml（项目设计.md §2.1）：lxml 要多背约 6 MB 且需要编译。
而我们要的选择器只是 ``div.reader img[data-src]`` 这一档，
自带一个 ~350 行的实现换掉 6 MB，是本项目"体积是产品特性"的具体兑现。

实现范围（站点覆盖率尚未测量）：
    标签 · ``*`` · ``.class`` · ``#id`` ·
    ``[attr]`` ``[attr=v]`` ``[attr~=v]`` ``[attr^=v]`` ``[attr$=v]`` ``[attr*=v]`` ·
    后代（空格）· 子代（``>``）· 并列（``,``）

刻意**不**实现：伪类、伪元素、兄弟组合器（``+`` ``~``）、``:nth-child``。
不支持的语法会明确报错。完整选择器后端尚未接入。

容错：真实网页的 ``<p>``/``<li>``/``<td>`` 几乎从不闭合。
本模块实现了一组隐式闭合规则，让 ``<p>a<p>b`` 得到两个兄弟而不是嵌套。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from html.parser import HTMLParser

from .selectors import SelectorError, _matches_selector, _split_groups

__all__ = ["Document", "Node", "parse", "SelectorError"]


#: HTML 空元素：没有闭合标签，也不会成为父节点。
VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)

#: 遇到这些开始标签时，栈顶若是集合内的标签就先隐式闭合。
_AUTO_CLOSE: dict[str, frozenset[str]] = {
    "p": frozenset({"p"}),
    "li": frozenset({"li"}),
    "dt": frozenset({"dt", "dd"}),
    "dd": frozenset({"dt", "dd"}),
    "td": frozenset({"td", "th"}),
    "th": frozenset({"td", "th"}),
    "tr": frozenset({"td", "th", "tr"}),
    "option": frozenset({"option"}),
    "thead": frozenset({"thead", "tbody", "tfoot"}),
    "tbody": frozenset({"thead", "tbody", "tfoot"}),
    "tfoot": frozenset({"thead", "tbody", "tfoot"}),
}

#: 块级元素会把尚未闭合的 ``<p>`` 顶掉（浏览器行为）。
_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "details",
        "div",
        "dl",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "ul",
    }
)

_WS_RE = re.compile(r"\s+")


# ============================================================ 节点


class Node:
    """一个元素节点。文本节点用 ``tag is None`` 表示。"""

    __slots__ = ("tag", "attrs", "children", "parent", "data")

    def __init__(
        self,
        tag: str | None,
        attrs: dict[str, str] | None = None,
        data: str = "",
    ) -> None:
        self.tag = tag
        self.attrs: dict[str, str] = attrs or {}
        self.children: list[Node] = []
        self.parent: Node | None = None
        self.data = data

    # -------------------------------------------------- 基础访问
    @property
    def is_text(self) -> bool:
        return self.tag is None

    def get(self, name: str, default: str | None = None) -> str | None:
        v = self.attrs.get(name.lower())
        return default if v is None else v

    def has(self, name: str) -> bool:
        return name.lower() in self.attrs

    @property
    def classes(self) -> frozenset[str]:
        return frozenset((self.attrs.get("class") or "").split())

    @property
    def own_text(self) -> str:
        """仅直接文本子节点，拼接后折叠空白。"""
        return _WS_RE.sub(" ", "".join(c.data for c in self.children if c.is_text)).strip()

    @property
    def text(self) -> str:
        """所有后代文本，折叠空白。

        故意不模仿浏览器的 ``innerText``（那要处理 CSS 可见性）——
        我们要的是"这段里有什么字"，不是"渲染出来什么样"。
        """
        buf: list[str] = []
        stack = [self]
        while stack:
            n = stack.pop()
            if n.is_text:
                buf.append(n.data)
                continue
            stack.extend(reversed(n.children))
        return _WS_RE.sub(" ", "".join(buf)).strip()

    def attr_list(self, name: str) -> list[str]:
        """按空白拆分的属性值，如 ``class``、``rel``。"""
        return (self.attrs.get(name.lower()) or "").split()

    # -------------------------------------------------- 遍历
    def iter(self) -> Iterator[Node]:
        """先序遍历（含自身）。用显式栈避免深页面爆栈。"""
        stack = [self]
        while stack:
            node = stack.pop()
            yield node
            if node.children:
                stack.extend(reversed(node.children))

    def iter_elements(self) -> Iterator[Node]:
        for n in self.iter():
            if not n.is_text:
                yield n

    def find_all(self, tag: str) -> list[Node]:
        tag = tag.lower()
        return [n for n in self.iter_elements() if n.tag == tag]

    def find(self, tag: str) -> Node | None:
        tag = tag.lower()
        for n in self.iter_elements():
            if n.tag == tag:
                return n
        return None

    # -------------------------------------------------- 选择器
    def select(self, selector: str) -> list[Node]:
        """返回所有匹配的**后代**节点（不含自身，与浏览器 querySelectorAll 一致）。"""
        out: list[Node] = []
        seen: set[int] = set()
        groups = _split_groups(selector)
        for group in groups:
            for node in self.iter_elements():
                if node is self:
                    continue
                if _matches_selector(node, group) and id(node) not in seen:
                    seen.add(id(node))
                    out.append(node)
        # 保持文档顺序（并列选择器会打乱），且不重复
        order = {id(n): i for i, n in enumerate(self.iter_elements())}
        out.sort(key=lambda n: order.get(id(n), 0))
        return out

    def select_one(self, selector: str) -> Node | None:
        found = self.select(selector)
        return found[0] if found else None

    def closest(self, selector: str) -> Node | None:
        """从自身向上找最近匹配的祖先（含自身）。"""
        groups = _split_groups(selector)
        node: Node | None = self
        while node is not None:
            if any(_matches_selector(node, g) for g in groups):
                return node
            node = node.parent
        return None

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        if self.is_text:
            return f"<#text {self.data[:20]!r}>"
        cls = f".{'.'.join(sorted(self.classes))}" if self.classes else ""
        return f"<{self.tag}{cls} {len(self.children)}子>"


class Document(Node):
    """整篇文档。``tag`` 恒为 ``#document``。"""

    __slots__ = ("base_url",)

    def __init__(self, base_url: str = "") -> None:
        super().__init__("#document")
        self.base_url = base_url

    @property
    def title(self) -> str:
        node = self.find("title")
        return node.text if node else ""

    def effective_base(self) -> str:
        """``<base href>`` 优先，其次构造时给的原地址。"""
        base = self.find("base")
        if base is not None:
            href = base.get("href")
            if href:
                from ..utils.urls import join_url

                joined = join_url(self.base_url, href)
                if joined:
                    return joined
        return self.base_url

    def iter_elements(self) -> Iterator[Node]:
        for n in self.iter():
            if not n.is_text and n.tag != "#document":
                yield n


# ============================================================ 解析器


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.doc = Document()
        self._stack: list[Node] = [self.doc]

    # -------------------------------------------------- 工具
    @property
    def _top(self) -> Node:
        return self._stack[-1]

    def _append(self, node: Node) -> None:
        node.parent = self._top
        self._top.children.append(node)

    def _pop_until(self, tags: frozenset[str] | set[str]) -> None:
        while len(self._stack) > 1 and self._top.tag in tags:
            self._stack.pop()

    # -------------------------------------------------- HTMLParser 回调
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()

        # 1) 隐式闭合：<li> 关掉上一个 <li>
        if tag in _AUTO_CLOSE:
            self._pop_until(_AUTO_CLOSE[tag])
        # 2) 块级元素顶掉未闭合的 <p>
        if tag in _BLOCK_TAGS:
            self._pop_until({"p"})

        node = Node(tag, {k.lower(): (v or "") for k, v in attrs})

        # 用 html.parser 给的原始顺序保留属性（dict 在 3.7+ 有序，够用）
        self._append(node)
        if tag not in VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        node = Node(tag, {k.lower(): (v or "") for k, v in attrs})
        self._append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in VOID_TAGS:
            return
        # 找最近的同名祖先；找不到就忽略（容错）
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                return

    def handle_data(self, data: str) -> None:
        if not data:
            return
        self._append(Node(None, None, data))


def parse(html: str, *, base_url: str = "") -> Document:
    """把 HTML 解析成 :class:`Document`。

    使用 HTMLParser 的容错语法；不吞掉意外异常，不执行脚本。
    """
    builder = _TreeBuilder()
    builder.doc.base_url = base_url
    builder.feed(html)
    builder.close()
    return builder.doc


# ============================================================ 选择器引擎
