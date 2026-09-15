"""Bounded HTTP content decoding for core (design section 29).

Both wire and decoded bodies must fit max_bytes. A single extra output byte
detects overflow; no unbounded decompress/flush call is used. Gzip accepts at
most 1,024 members, including empty ones: a 32 MiB input could otherwise contain
over 1.6 million empty members without spending any decoded-byte budget.
"""

from __future__ import annotations

import zlib

from ..errors import FetchError

_CHUNK_SIZE = 64 * 1024
_MAX_GZIP_MEMBERS = 1024
_GZIP_WBITS = zlib.MAX_WBITS | 16


def decode_body(raw: bytes, encoding: str, max_bytes: int) -> bytes:
    """Decode a complete body, rejecting invalid, unsupported or oversized data.

    Encoding tokens are case-insensitive and may have surrounding whitespace.
    Stacked encodings and gzip padding are not accepted. A zero budget permits
    only an empty identity body; negative budgets raise FetchError.
    """
    if max_bytes < 0:
        raise FetchError("Response size limit must be non-negative")
    if len(raw) > max_bytes:
        raise FetchError("Response exceeds configured size limit")
    encoding = encoding.strip().lower()
    if encoding in {"", "identity"}:
        return raw
    if encoding not in {"gzip", "deflate"}:
        raise FetchError(f"Unsupported content encoding: {encoding}")

    modes = (_GZIP_WBITS,) if encoding == "gzip" else (zlib.MAX_WBITS, -zlib.MAX_WBITS)
    for index, mode in enumerate(modes):
        try:
            return _inflate(raw, mode, max_bytes)
        except zlib.error as exc:
            if index == len(modes) - 1:
                raise FetchError("Invalid or incomplete compressed response") from exc
    raise AssertionError("No compression mode attempted")


def _inflate(raw: bytes, wbits: int, max_bytes: int) -> bytes:
    decoded = bytearray()
    source = memoryview(raw)
    pending: bytes | memoryview = b""
    offset = 0
    members = 1
    decoder = zlib.decompressobj(wbits)
    while True:
        if not pending and offset < len(raw):
            end = min(offset + _CHUNK_SIZE, len(raw))
            pending = source[offset:end]
            offset = end

        remaining = max_bytes - len(decoded)
        chunk = decoder.decompress(pending, min(_CHUNK_SIZE, remaining + 1))
        if len(chunk) > remaining:
            raise FetchError("Decoded response exceeds configured size limit")
        decoded.extend(chunk)

        if decoder.eof:
            # Only a bounded input block is copied into unused_data, even when
            # many tiny members precede a large remainder of the wire body.
            pending = decoder.unused_data
            if not pending and offset == len(raw):
                return bytes(decoded)
            if wbits != _GZIP_WBITS:
                raise zlib.error("Trailing data after compressed response")
            if members >= _MAX_GZIP_MEMBERS:
                raise FetchError("Gzip response exceeds member limit (1024)")
            members += 1
            decoder = zlib.decompressobj(wbits)
        else:
            pending = decoder.unconsumed_tail
            # Empty input may still drain buffered output after max_length was
            # reached. Only stop once neither input nor output can progress.
            if not pending and offset == len(raw) and not chunk:
                raise zlib.error("Incomplete compressed response")
