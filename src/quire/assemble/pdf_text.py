"""PDF string serialization and bounded placeholder layout."""

from __future__ import annotations

import textwrap


def _escape_literal(raw: bytes) -> bytes:
    out = bytearray()
    for b in raw:
        if b in (0x28, 0x29, 0x5C):  # ( ) \
            out.append(0x5C)
            out.append(b)
        elif b < 0x20:
            out.extend(f"\\{b:03o}".encode())
        else:
            out.append(b)
    return bytes(out)


def pdf_text(text: str) -> bytes:
    """PDF 字符串。纯 ASCII 用字面量，含非 ASCII 就用 UTF-16BE 十六进制串。

    中文书名/章节名必须走这条路径，否则阅读器里会是一片乱码或直接报错。
    """
    try:
        ascii_bytes = text.encode("ascii")
    except UnicodeEncodeError:
        return b"<FEFF" + text.encode("utf-16-be").hex().upper().encode("ascii") + b">"
    return b"(" + _escape_literal(ascii_bytes) + b")"


def _pdf_date() -> str:
    import datetime as _dt

    now = _dt.datetime.now(_dt.UTC)
    return now.strftime("D:%Y%m%d%H%M%SZ")


def placeholder_content(lines: list[str], width: float, height: float) -> bytes:
    x = width * 0.12
    y = height * 0.62
    body_size = 10.0
    max_chars = max(20, int((width - 2 * x) / (body_size * 0.65)))
    commands = []
    for index, line in enumerate(lines):
        safe = line.encode("ascii", "replace").decode("ascii")
        wrapped = textwrap.wrap(safe, width=max_chars)[:4]
        for row in wrapped:
            if y < 48:
                break
            size = 18 if index == 0 else body_size
            literal = pdf_text(row).decode("ascii")
            commands.append(f"BT 0.2 g /F1 {size} Tf 1 0 0 1 {x:.2f} {y:.2f} Tm {literal} Tj ET")
            y -= size * 1.7
    return "\n".join(commands).encode("ascii")
