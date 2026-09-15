"""A single encoded page shared by the core output writers."""

from __future__ import annotations

from dataclasses import dataclass


def clean_metadata_text(text: str) -> str:
    """Keep XML-compatible text shared by the PDF, archive and report metadata."""
    return "".join(
        char
        for char in text
        if (ord(char) >= 0x20 or char in "\t\n\r")
        and not 0x7F <= ord(char) <= 0x9F
        and not 0xD800 <= ord(char) <= 0xDFFF
        and ord(char) not in {0xFFFE, 0xFFFF}
    )


@dataclass(frozen=True, slots=True)
class ExportPage:
    data: bytes
    width: int
    height: int
    source_index: int
    source_url: str
    part: int = 1
    missing_reason: str | None = None
