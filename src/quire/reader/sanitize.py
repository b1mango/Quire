"""只输出阅读器允许的 HTML 标签和属性。"""

from __future__ import annotations

from collections.abc import Callable
from html import escape
from html.parser import HTMLParser
from urllib.parse import urlsplit

_ALLOWED = {
    "h1",
    "h2",
    "h3",
    "h4",
    "p",
    "br",
    "hr",
    "em",
    "strong",
    "b",
    "i",
    "blockquote",
    "ul",
    "ol",
    "li",
    "img",
    "figure",
    "figcaption",
    "sup",
    "sub",
    "ruby",
    "rt",
    "span",
    "div",
    "a",
}
_VOID = {"br", "hr", "img"}
_DROP = {"script", "style", "iframe", "object", "form"}


class _Sanitizer(HTMLParser):
    def __init__(self, image_url: Callable[[str], str | None]) -> None:
        super().__init__(convert_charrefs=True)
        self.image_url = image_url
        self.output: list[str] = []
        self.skipped: list[str] = []
        self.in_body = False
        self.has_body = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "body":
            self.in_body = True
            self.has_body = True
        if self.skipped:
            if tag in _DROP:
                self.skipped.append(tag)
            return
        if tag in _DROP:
            self.skipped.append(tag)
            return
        if tag not in _ALLOWED or not self.in_body:
            return
        values = dict(attrs)
        attributes = ""
        if tag == "img":
            src = self.image_url(values.get("src") or "")
            if src is None:
                return
            attributes = f' src="{escape(src, quote=True)}"'
            if values.get("alt") is not None:
                attributes += f' alt="{escape(values["alt"] or "", quote=True)}"'
        elif tag == "a":
            href = values.get("href") or ""
            parsed = urlsplit(href)
            if href.startswith("#") and parsed.fragment and not parsed.path:
                attributes = f' href="{escape(href, quote=True)}"'
        self.output.append(f"<{tag}{attributes}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self.skipped:
            if tag == self.skipped[-1]:
                self.skipped.pop()
            return
        if tag == "body":
            self.in_body = False
        if tag in _ALLOWED and tag not in _VOID and self.in_body:
            self.output.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self.skipped and self.in_body:
            self.output.append(escape(data))


def sanitize(document: str, image_url: Callable[[str], str | None]) -> str:
    parser = _Sanitizer(image_url)
    parser.feed(document)
    parser.close()
    return "".join(parser.output)
