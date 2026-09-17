from __future__ import annotations

import gzip
import threading
import zlib

import pytest

from quire.errors import BlockedError, ConfigError, FetchError, HttpStatusError
from quire.fetch.ratelimit import HostRateLimiter
from quire.fetch.simple import Fetcher, retry_after
from quire.fetch.text import decode_html
from tests.mock_site.server import serve


@pytest.fixture
def site():
    with serve() as server:
        yield server


def test_http_redirect_charset_and_gzip(site):
    client = Fetcher(rate=1000)
    assert client.get(site.url + "/redirect").url == site.url + "/comic"
    assert "卷帙测试" in client.get(site.url + "/gbk").text
    assert client.get(site.url + "/gzip").text == "<h1>gzip</h1>"


def test_retry_and_nonretry_statuses(site):
    client = Fetcher(rate=1000, retries=1)
    assert client.get(site.url + "/retry").content == b"ok"
    assert site.counts["/retry"] == 2
    with pytest.raises(HttpStatusError) as error:
        client.get(site.url + "/missing?token=secret")
    assert error.value.status == 404
    assert "secret" not in str(error.value)
    assert site.counts["/missing"] == 1
    with pytest.raises(BlockedError):
        client.get(site.url + "/blocked")
    assert site.counts["/blocked"] == 1


def test_blocked_hints_and_browser_like_default_headers(site):
    client = Fetcher(rate=1000)
    client.get(site.url + "/comic")
    sent = site.headers_log["/comic"]
    assert "Chrome/" in sent["User-Agent"]
    assert sent["Accept-Language"].startswith("zh-CN")
    with pytest.raises(BlockedError) as plain:
        client.get(site.url + "/blocked")
    assert plain.value.hint and "Cloudflare" not in plain.value.hint
    with pytest.raises(BlockedError) as cloudflare:
        client.get(site.url + "/cf-blocked")
    assert cloudflare.value.hint and "Cloudflare" in cloudflare.value.hint


def test_response_and_decode_budgets(site):
    with pytest.raises(FetchError, match="size limit"):
        Fetcher(max_bytes=80).get(site.url + "/comic")
    with pytest.raises(FetchError, match="size limit"):
        Fetcher(max_bytes=1000).get(site.url + "/bomb")


def test_robots_denial_and_single_fetch(site):
    client = Fetcher(rate=1000)
    client.get(site.url + "/comic")
    client.get(site.url + "/gzip")
    with pytest.raises(BlockedError, match="robots.txt"):
        client.get(site.url + "/forbidden")
    assert site.counts["/robots.txt"] == 1
    assert site.counts["/forbidden"] == 0


def test_redirected_robots_has_no_recursive_policy_lookup(site):
    site.redirect_robots = True
    client = Fetcher(rate=1000)
    assert client.get(site.url + "/comic").status == 200
    assert site.counts["/robots.txt"] == 1
    assert site.counts["/policy"] == 1


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "ftp://example.org/a", "http://", "https://u:p@example.org/"]
)
def test_http_only(url):
    with pytest.raises(ConfigError):
        Fetcher().get(url)


def test_cancel_stops_before_request():
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(FetchError, match="cancelled"):
        Fetcher(cancel=cancel).get("https://example.org/")


def test_rate_and_retry_after():
    now = [100.0]
    limiter = HostRateLimiter(
        2, clock=lambda: now[0], sleep=lambda delay: now.__setitem__(0, now[0] + delay)
    )
    limiter.acquire("https://a.example/1")
    limiter.acquire("https://b.example/1")
    assert now[0] == 100
    limiter.acquire("https://a.example/2")
    assert now[0] == 100.5
    limiter.defer("https://a.example/3", 3)
    limiter.acquire("https://a.example/3")
    assert now[0] == 103.5
    assert retry_after("Wed, 21 Oct 2015 07:28:00 GMT", 1445412470) == 10
    assert retry_after("bad", 0) == 0
    assert retry_after("-3", 0) == 0


def test_decoding_formats():
    assert decode_html("卷帙".encode("gb18030"), "") == "卷帙"
    assert decode_html("卷帙".encode("utf-16"), "") == "卷帙"
    assert decode_html("卷帙".encode("utf-8-sig"), "") == "卷帙"
    assert decode_html("繁體中文".encode("big5"), "text/html; charset=big5") == "繁體中文"
    assert decode_html(b"hello", "text/html; charset=unknown") == "hello"
    client = Fetcher()
    assert client._decode(zlib.compress(b"hello"), "deflate") == b"hello"
    assert client._decode(zlib.compress(b"hello")[2:-4], "deflate") == b"hello"
    assert client._decode(gzip.compress(b"hello"), "gzip") == b"hello"
    with pytest.raises(zlib.error):
        client._decode(b"bad", "deflate")
