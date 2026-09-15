"""Restricted CSS selectors, compiled once per query."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .minidom import Node


class SelectorError(ValueError):
    """Unsupported or malformed CSS selector."""


_ATTR_RE = re.compile(
    r"""\[\s*(?P<name>[\w:-]+)\s*
        (?:(?P<op>[~^$*|]?=)\s*
           (?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'|(?P<bare>[^\]\s]*))
        )?\s*\]""",
    re.VERBOSE,
)
_TOKEN_RE = re.compile(
    r"(?P<tag>\*|[a-zA-Z][\w-]*)"
    r"|#(?P<id>[\w-]+)"
    r"|\.(?P<cls>[\w-]+)"
    r"|\[(?P<attr>[^\]]*)\]"
)


class _Compound:
    """一个复合选择器，如 ``div.reader[data-x="1"]``。"""

    __slots__ = ("tag", "id", "classes", "attrs")

    def __init__(self) -> None:
        self.tag: str | None = None
        self.id: str | None = None
        self.classes: list[str] = []
        self.attrs: list[tuple[str, str, str]] = []  # (name, op, value)；op 为 "" 表示只判存在

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Compound {self.tag} #{self.id} .{' .'.join(self.classes)} {self.attrs}>"


def _split_groups(selector: str) -> list[list[tuple[str, _Compound]]]:
    """把 ``a, b > c`` 拆成若干组，每组是 ``[(combinator, Compound), ...]``。"""
    groups: list[list[tuple[str, _Compound]]] = []
    for raw in _split_top_level_commas(selector):
        group = _parse_group(raw)
        if group:
            groups.append(group)
    if not groups:
        raise SelectorError(f"选择器为空或无法解析：{selector!r}")
    return groups


def _split_top_level_commas(selector: str) -> list[str]:
    """按逗号切分，但忽略 ``[...]`` 内部的逗号（``[content="a,b"]``）。"""
    out: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in selector:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    return [s for s in (p.strip() for p in out) if s]


def _parse_group(text: str) -> list[tuple[str, _Compound]]:
    text = text.strip()
    if not text:
        return []

    result: list[tuple[str, _Compound]] = []
    combinator = ""  # 第一个复合选择器没有前导组合器
    i, n = 0, len(text)

    while i < n:
        ch = text[i]
        if ch.isspace():
            # 空白是后代组合器，除非后面紧跟 '>'（那就交给 '>' 处理）
            j = i
            while j < n and text[j].isspace():
                j += 1
            if j < n and text[j] == ">":
                i = j
                continue
            combinator = combinator or (" " if result else "")
            i = j
            continue
        if ch == ">":
            if not result or combinator == ">":
                raise SelectorError(f"选择器不能以 '>' 开头：{text!r}")
            combinator = ">"
            i += 1
            continue

        m = _TOKEN_RE.match(text, i)
        if not m:
            raise SelectorError(f"不支持的选择器语法（位置 {i}）：{text!r}")

        compound = _Compound()
        # 连续 token 属于同一个复合选择器：div.a.b#c[x]
        while m:
            if m.group("tag"):
                if compound.tag is not None:
                    break
                compound.tag = m.group("tag").lower()
            elif m.group("id"):
                compound.id = m.group("id")
            elif m.group("cls"):
                compound.classes.append(m.group("cls"))
            elif m.group("attr") is not None:
                compound.attrs.append(_parse_attr(m.group("attr")))
            i = m.end()
            m = _TOKEN_RE.match(text, i)

        result.append((combinator, compound))
        combinator = ""

    if combinator == ">":
        raise SelectorError(f"Incomplete selector: {text!r}")
    return result


def _parse_attr(spec: str) -> tuple[str, str, str]:
    m = _ATTR_RE.match(f"[{spec}]")
    if not m:
        raise SelectorError(f"不支持的属性选择器：[{spec}]")
    name = m.group("name").lower()
    op = m.group("op") or ""
    value = m.group("dq") or m.group("sq") or m.group("bare") or ""
    return (name, op, value)


def _match_compound(node: Node, c: _Compound) -> bool:
    if node.is_text:
        return False
    if c.tag is not None and c.tag != "*" and node.tag != c.tag:
        return False
    if c.id is not None and node.attrs.get("id") != c.id:
        return False
    if c.classes:
        node_classes = node.classes
        for cls in c.classes:
            if cls not in node_classes:
                return False
    for name, op, value in c.attrs:
        actual = node.attrs.get(name)
        if actual is None:
            return False
        if not op:
            continue
        actual_l = actual
        value_l = value
        if op == "=":
            if actual_l != value_l:
                return False
        elif op == "~=":
            if value_l not in actual_l.split():
                return False
        elif op == "^=":
            if not value_l or not actual_l.startswith(value_l):
                return False
        elif op == "$=":
            if not value_l or not actual_l.endswith(value_l):
                return False
        elif op == "*=":
            if not value_l or value_l not in actual_l:
                return False
        elif op == "|=":
            if actual_l != value_l and not actual_l.startswith(f"{value_l}-"):
                return False
    return True


def _matches_selector(node: Node, group: list[tuple[str, _Compound]]) -> bool:
    """从右向左匹配——这是选择器引擎的标准做法，也让它天然是 O(depth)。"""
    combinator, compound = group[-1]
    if not _match_compound(node, compound):
        return False

    # Backtrack over ancestor alternatives for mixed child/descendant queries.
    pending = [(node, len(group) - 1)]
    while pending:
        current, index = pending.pop()
        if not _match_compound(current, group[index][1]):
            continue
        if index == 0:
            return True
        parent = current.parent
        while parent is not None:
            pending.append((parent, index - 1))
            if group[index][0] == ">":
                break
            parent = parent.parent
    return False
