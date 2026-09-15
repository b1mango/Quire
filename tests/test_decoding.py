from __future__ import annotations

import gzip
import random
import zlib

import pytest

from quire.errors import FetchError
from quire.fetch.decoding import decode_body


def _encode(body: bytes, kind: str) -> bytes:
    if kind == "gzip":
        return gzip.compress(body, mtime=0)
    compressor = zlib.compressobj(wbits=-15 if kind == "raw" else 15)
    return compressor.compress(body) + compressor.flush()


def _encoding(kind: str) -> str:
    return "deflate" if kind == "raw" else kind


@pytest.fixture
def bounded_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int, int]]:
    """Exercise real zlib while guarding against unbounded calls and flushing."""
    factory = zlib.decompressobj
    calls: list[tuple[int, int, int]] = []

    class GuardedDecoder:
        def __init__(self, wbits: int) -> None:
            self.decoder = factory(wbits)

        def decompress(self, data: bytes | memoryview, max_length: int = 0) -> bytes:
            assert 0 < max_length <= 65536
            assert len(data) <= 65536
            result = self.decoder.decompress(data, max_length)
            calls.append((len(data), max_length, len(result)))
            return result

        @property
        def eof(self) -> bool:
            return self.decoder.eof

        @property
        def unused_data(self) -> bytes:
            return self.decoder.unused_data

        @property
        def unconsumed_tail(self) -> bytes:
            return self.decoder.unconsumed_tail

    monkeypatch.setattr("quire.fetch.decoding.zlib.decompressobj", GuardedDecoder)
    return calls


@pytest.mark.parametrize("encoding", ["", "identity", " IDENTITY\t", " \t"])
def test_identity_and_zero_budget(encoding: str) -> None:
    body = b"unaltered\x00\xff"
    assert decode_body(body, encoding, len(body)) is body
    assert decode_body(b"", encoding, 0) == b""
    with pytest.raises(FetchError, match="size limit"):
        decode_body(body, encoding, len(body) - 1)


@pytest.mark.parametrize("encoding", ["identity", "gzip", "deflate"])
def test_negative_budget(encoding: str) -> None:
    with pytest.raises(FetchError, match="size limit"):
        decode_body(b"", encoding, -1)


@pytest.mark.parametrize("encoding", ["br", "zstd", "gzip, deflate", "x-gzip", "gzip; q=1"])
def test_unsupported_encoding(encoding: str) -> None:
    with pytest.raises(FetchError, match="Unsupported content encoding"):
        decode_body(b"", encoding, 1024)


@pytest.mark.parametrize("kind", ["gzip", "deflate", "raw"])
@pytest.mark.parametrize("body", [b"", b"hello\x00\xff", b"a" * 65536, b"b" * 131073])
def test_round_trip(kind: str, body: bytes, bounded_calls: list[tuple[int, int, int]]) -> None:
    raw = _encode(body, kind)
    assert decode_body(raw, f" {_encoding(kind).upper()} ", max(len(raw), len(body))) == body
    assert bounded_calls


@pytest.mark.parametrize("kind", ["gzip", "deflate", "raw"])
def test_input_budget_before_inflate(kind: str, bounded_calls: list[tuple[int, int, int]]) -> None:
    raw = _encode(b"", kind)
    with pytest.raises(FetchError, match="size limit"):
        decode_body(raw, _encoding(kind), len(raw) - 1)
    assert not bounded_calls


@pytest.mark.parametrize("kind", ["gzip", "deflate", "raw"])
def test_bomb_stops_at_budget_plus_one(
    kind: str, bounded_calls: list[tuple[int, int, int]]
) -> None:
    raw = _encode(b"x" * (8 * 1024 * 1024), kind)
    budget = 100_000
    assert len(raw) < budget
    with pytest.raises(FetchError, match="size limit"):
        decode_body(raw, _encoding(kind), budget)
    assert sum(length for _, _, length in bounded_calls) == budget + 1


@pytest.mark.parametrize("budget", [65536, 65535])
def test_gzip_members_share_budget(budget: int, bounded_calls: list[tuple[int, int, int]]) -> None:
    first, second = b"a" * 32768, b"b" * 32768
    raw = _encode(first, "gzip") + _encode(b"", "gzip") + _encode(second, "gzip")
    assert len(raw) < budget
    if budget == 65536:
        assert decode_body(raw, "gzip", budget) == first + second
    else:
        with pytest.raises(FetchError, match="size limit"):
            decode_body(raw, "gzip", budget)
    assert sum(length for _, _, length in bounded_calls) == 65536


def test_empty_gzip_members_after_exact_output_budget() -> None:
    body = b"a" * 4096
    raw = _encode(body, "gzip") + _encode(b"", "gzip") * 10
    assert decode_body(raw, "gzip", len(body)) == body
    with pytest.raises(FetchError, match="size limit"):
        decode_body(raw + _encode(b"b", "gzip"), "gzip", len(body))


@pytest.mark.parametrize("members", [1024, 1025, 100_000])
def test_empty_gzip_member_limit(members: int, bounded_calls: list[tuple[int, int, int]]) -> None:
    raw = _encode(b"", "gzip") * members
    if members == 1024:
        assert decode_body(raw, "gzip", len(raw)) == b""
    else:
        with pytest.raises(FetchError, match="member limit"):
            decode_body(raw, "gzip", len(raw))
    assert len(bounded_calls) == 1024


@pytest.mark.parametrize("kind", ["gzip", "deflate", "raw"])
def test_all_truncation_points(kind: str) -> None:
    raw = _encode(b"a complete stream" * 40, kind)
    for end in range(len(raw)):
        with pytest.raises(FetchError):
            decode_body(raw[:end], _encoding(kind), 4096)


def test_every_truncated_second_gzip_member() -> None:
    first = _encode(b"first", "gzip")
    second = _encode(b"second", "gzip")
    for end in range(1, len(second)):
        with pytest.raises(FetchError):
            decode_body(first + second[:end], "gzip", 4096)


@pytest.mark.parametrize("kind", ["gzip", "deflate", "raw"])
@pytest.mark.parametrize("trailer", [b"garbage", b"\x00", b"\x00" * 32, b"\x1f\x8b"])
def test_trailing_junk(kind: str, trailer: bytes) -> None:
    with pytest.raises(FetchError):
        decode_body(_encode(b"payload", kind) + trailer, _encoding(kind), 4096)


@pytest.mark.parametrize("kind", ["deflate", "raw"])
@pytest.mark.parametrize("following", ["gzip", "deflate", "raw"])
def test_deflate_rejects_concatenated_streams(kind: str, following: str) -> None:
    raw = _encode(b"first", kind) + _encode(b"second", following)
    with pytest.raises(FetchError):
        decode_body(raw, "deflate", 4096)


@pytest.mark.parametrize("kind", ["gzip", "deflate"])
@pytest.mark.parametrize("offset", [-1, -5])
def test_corrupt_checksum_or_size(kind: str, offset: int) -> None:
    raw = bytearray(_encode(b"payload" * 100, kind))
    raw[offset] ^= 0xFF
    with pytest.raises(FetchError):
        decode_body(bytes(raw), kind, 4096)


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
@pytest.mark.parametrize("raw", [b"not compressed", b"\x07"])
def test_invalid_stream_is_domain_error(encoding: str, raw: bytes) -> None:
    with pytest.raises(FetchError) as caught:
        decode_body(raw, encoding, 4096)
    assert isinstance(caught.value.__cause__, zlib.error)


@pytest.mark.parametrize("kind", ["gzip", "deflate", "raw"])
def test_large_incompressible_input_crosses_blocks(
    kind: str, bounded_calls: list[tuple[int, int, int]]
) -> None:
    body = random.Random(29).randbytes(200_000)
    raw = _encode(body, kind)
    assert decode_body(raw, _encoding(kind), max(len(raw), len(body))) == body
    assert len(bounded_calls) >= 4


@pytest.mark.parametrize("member_size", [65535, 65536, 65537])
def test_gzip_member_boundary_at_input_block(member_size: int) -> None:
    body = b"first member"
    member = _encode(body, "gzip")
    # RFC 1952 FEXTRA extends the fixed header without changing the compressed
    # payload or trailer. Size the extra field from the actual compressed data.
    extra_size = member_size - len(member) - 2
    assert 4 <= extra_size <= 65535
    extra = b"BC" + (extra_size - 4).to_bytes(2, "little") + bytes(extra_size - 4)
    first = (
        member[:3]
        + bytes([member[3] | 4])
        + member[4:10]
        + extra_size.to_bytes(2, "little")
        + extra
        + member[10:]
    )
    assert len(first) == member_size
    assert gzip.decompress(first) == body
    second = _encode(b"b" * 1000, "gzip")
    raw = first + second
    assert decode_body(raw, "gzip", max(len(raw), len(body) + 1000)) == body + b"b" * 1000


@pytest.mark.parametrize("level", [0, 1, 6, 9])
@pytest.mark.parametrize("wbits", [-15, 9, 15])
def test_deflate_levels_and_windows(level: int, wbits: int) -> None:
    body = b"payload\x00\xff" * 1000
    compressor = zlib.compressobj(level=level, wbits=wbits)
    raw = compressor.compress(body) + compressor.flush()
    assert decode_body(raw, "deflate", max(len(raw), len(body))) == body


def test_deflate_raw_with_zlib_looking_prefix() -> None:
    # RFC 1951 stored block with ignored alignment bits and LEN=156. Its first
    # two bytes also form a valid RFC 1950 header; header sniffing is ambiguous.
    body = b"a" * 156
    raw = b"\x78\x9c\x00\x63\xff" + body + b"\x03\x00"
    assert zlib.decompress(raw, -15) == body
    assert decode_body(raw, "deflate", len(raw)) == body


def test_deflate_preset_dictionary_is_rejected() -> None:
    compressor = zlib.compressobj(zdict=b"dictionary")
    raw = compressor.compress(b"dictionary payload") + compressor.flush()
    with pytest.raises(FetchError):
        decode_body(raw, "deflate", 4096)
