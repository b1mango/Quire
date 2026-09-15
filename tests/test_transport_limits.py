from __future__ import annotations

import gzip
import http.client
import io
import threading
import urllib.request
import zlib

import pytest

from quire.errors import FetchError
from quire.fetch.simple import Fetcher, _HttpOnlyRedirect


class SocketStream:
    def __init__(self, stream):
        self.stream = stream

    def makefile(self, *args):
        return self.stream


def test_redirect_does_not_drain_intermediate_body():
    class TrackedStream(io.BytesIO):
        consumed = 0

        def read(self, size=-1):
            data = super().read(size)
            self.consumed += len(data)
            return data

    stream = TrackedStream(
        b"HTTP/1.1 302 Found\r\nLocation: /final\r\nContent-Length: 1000000\r\n\r\n"
        + b"x" * 1_000_000
    )
    response = http.client.HTTPResponse(SocketStream(stream))
    response.begin()
    targets = []
    handler = _HttpOnlyRedirect(targets.append)

    class Opener:
        def open(self, req, timeout):
            return req.full_url

    handler.add_parent(Opener())
    request = urllib.request.Request("https://example.test/start")
    request.timeout = 1
    result = handler.http_error_302(request, response, 302, "Found", response.headers)
    assert result == "https://example.test/final"
    assert targets == [result]
    assert stream.closed
    assert stream.consumed == 0


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("framing", ["length", "chunk-header", "chunk-trailer"])
def test_trickling_body_checks_deadline_and_cancel_between_reads(monkeypatch, cancelled, framing):
    now = [0.0]
    cancel = threading.Event()

    class Trickle(io.RawIOBase):
        def __init__(self):
            if framing == "length":
                wire = b"Content-Length: 96\r\n\r\n" + b"x" * 96
            elif framing == "chunk-header":
                wire = (
                    b"Transfer-Encoding: chunked\r\n\r\n1;extension="
                    + b"a" * 1000
                    + b"\r\nx\r\n0\r\n\r\n"
                )
            else:
                wire = (
                    b"Transfer-Encoding: chunked\r\n\r\n0\r\nX-Trailer: "
                    + b"a" * 1000
                    + b"\r\n\r\n"
                )
            self.data = bytearray(b"HTTP/1.1 200 OK\r\n" + wire)
            self.reads = 0
            self.reading_body = False

        def readable(self):
            return True

        def readinto(self, buf):
            if not self.data:
                return 0
            buf[0] = self.data.pop(0)
            if self.reading_body:
                self.reads += 1
                now[0] += 0.01
                if cancelled and self.reads == 3:
                    cancel.set()
            return 1

    raw = Trickle()
    response = http.client.HTTPResponse(SocketStream(io.BufferedReader(raw)))
    response.begin()
    raw.reading_body = True
    monkeypatch.setattr("quire.fetch.simple.time.monotonic", lambda: now[0])
    client = Fetcher(timeout=0.05, cancel=cancel)
    with pytest.raises(FetchError if cancelled else TimeoutError):
        client._read_body(response)
    assert raw.reads <= 6
    response.close()


def test_concatenated_gzip_preserves_every_member_and_limits_total():
    body = gzip.compress(b"first") + gzip.compress(b"") + gzip.compress(b"second")
    assert Fetcher(max_bytes=11)._decode(body, "gzip") == b"firstsecond"
    with pytest.raises(FetchError, match="size limit"):
        Fetcher(max_bytes=10)._decode(body, "gzip")
    with pytest.raises(zlib.error):
        Fetcher()._decode(body[:-2], "gzip")
    with pytest.raises(zlib.error):
        Fetcher()._decode(body + b"trailing junk", "gzip")
    with pytest.raises(zlib.error):
        Fetcher()._decode(zlib.compress(b"first") + zlib.compress(b"second"), "deflate")


@pytest.mark.parametrize("budget", [11, 10])
def test_chunked_payload_and_trailers_with_response_limit(budget):
    stream = io.BytesIO(
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        b"5;name=value\r\nfirst\r\n6\r\nsecond\r\n0\r\nX-Trailer: yes\r\n\r\n"
    )
    response = http.client.HTTPResponse(SocketStream(stream))
    response.begin()
    try:
        if budget == 11:
            assert Fetcher(max_bytes=budget)._read_body(response) == b"firstsecond"
        else:
            with pytest.raises(FetchError, match="size limit"):
                Fetcher(max_bytes=budget)._read_body(response)
    finally:
        response.close()
    assert stream.closed
