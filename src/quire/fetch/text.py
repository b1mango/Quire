"""HTML decoding without optional detection libraries."""

from __future__ import annotations

from email.message import Message
from html.parser import HTMLParser


class _CharsetParser(HTMLParser):
    charset = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "meta" or self.charset:
            return
        values = dict(attrs)
        self.charset = values.get("charset") or ""
        if not self.charset:
            message = Message()
            message["content-type"] = values.get("content") or ""
            self.charset = message.get_content_charset() or ""


def decode_html(data: bytes, content_type: str) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig", errors="replace")
    message = Message()
    message["content-type"] = content_type
    meta = _CharsetParser()
    meta.feed(data[:8192].decode("ascii", errors="ignore"))
    for encoding in (message.get_content_charset(), meta.charset, "utf-8", "gb18030", "big5"):
        if encoding:
            try:
                return data.decode(encoding)
            except (LookupError, UnicodeError):
                continue
    return data.decode("utf-8", errors="replace")
