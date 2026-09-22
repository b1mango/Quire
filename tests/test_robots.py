from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from quire.errors import BlockedError, HttpStatusError, NetworkError
from quire.fetch.robots import RobotsPolicy
from quire.fetch.simple import Fetcher, Response


def assert_access(body: str, path: str, allowed: bool) -> None:
    policy = RobotsPolicy(lambda _: body)
    url = "https://example.test" + path
    if allowed:
        policy.check(url)
    else:
        with pytest.raises(BlockedError, match="robots.txt") as error:
            policy.check(url)
        assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    "rules,path,allowed",
    [
        ("Allow: /\nDisallow: /private/", "/private/page", False),
        ("Disallow: /private/\nAllow: /", "/private/page", False),
        ("Disallow: /\nAllow: /public/", "/public/page", True),
        ("Disallow: /same\nAllow: /same", "/same/page", True),
        ("Allow: /same\nDisallow: /same", "/same/page", True),
        ("Disallow: /private", "/Private/page", True),
        ("Disallow: /private", "/other", True),
        ("Disallow: /", "", False),
        ("Disallow: /?token=", "?token=secret", False),
        ("Disallow: /private$", "/private#ignored", False),
        ("Disallow: /private$", "/private?token=secret", True),
        ("Disallow: /private$", "/private/page", True),
        ("Disallow: /*.jpg$", "/private.jpg", False),
        ("Disallow: /*.jpg$", "/private.jpg?token=secret", True),
        ("Disallow: /*.jpg", "/private.jpg?token=secret", False),
        ("Disallow: /*.jpg", "/private.png", True),
        ("Disallow: /a*b*c$", "/abc", False),
        ("Disallow: /a*b*c$", "/axbyc", False),
        ("Disallow: /a*b*c$", "/axyc", True),
        ("Disallow: /a*a$", "/a", True),
        ("Disallow: /a*a$", "/aa", False),
        ("Disallow: /a**b*$", "/abtail", False),
        ("Disallow: /foo$bar", "/foo$bar/more", False),
        ("Disallow: /a?b[0].+", "/axb0zz", True),
        ("Disallow: /a?b[0].+", "/a?b[0].+", False),
        ("Allow: /a*\nDisallow: /ab", "/ab", False),
        ("Allow: /ab\nDisallow: /a*b", "/ab", True),
        ("Allow: /foo\nDisallow: /foo$", "/foo", True),
        ("Disallow:\nAllow:\nDisallow: relative", "/anything", True),
    ],
)
def test_path_rules(rules: str, path: str, allowed: bool) -> None:
    assert_access("User-agent: *\n" + rules, path, allowed)


@pytest.mark.parametrize(
    "rules,path,allowed",
    [
        ("Disallow: /%70rivate", "/private", False),
        ("Disallow: /private", "/%70rivate", False),
        ("Disallow: /~name", "/%7ename", False),
        ("Disallow: /a%2fb", "/a%2Fb", False),
        ("Disallow: /a%2Fb", "/a/b", True),
        ("Disallow: /a/b", "/a%2Fb", True),
        ("Disallow: /caf\u00e9", "/caf%C3%A9", False),
        ("Disallow: /caf%c3%a9", "/caf\u00e9", False),
        ("Disallow: /%E5%8D%B7", "/\u5377", False),
        ("Disallow: /a%2Ab$", "/a*b", True),
        ("Disallow: /a%2Ab$", "/a%2ab", False),
        ("Disallow: /a%24", "/a%24tail", False),
        ("Disallow: /a%3Fb", "/a?b", True),
        ("Disallow: /a%23b", "/a%23b", False),
        ("Disallow: /a%23b", "/a#b", True),
        ("Disallow: /a%252Fb", "/a%2Fb", True),
        ("Disallow: /bad%ZZ", "/bad%ZZ", False),
        ("Disallow: /a b", "/a%20b", False),
        ("Disallow: /search?q=%7e", "/search?q=~&token=secret", False),
        ("Disallow: /%66oo\nAllow: /foo", "/foo", True),
        ("Disallow: /%41*\nAllow: /AB", "/AB", True),
    ],
)
def test_percent_encoding(rules: str, path: str, allowed: bool) -> None:
    assert_access("User-agent: *\n" + rules, path, allowed)


@pytest.mark.parametrize("reverse", [False, True])
def test_specific_groups_merge_and_override_wildcard(reverse: bool) -> None:
    groups = [
        "User-agent: *\nDisallow: /\n",
        "User-agent: Quire\nDisallow: /first\n",
        "User-agent: Other\nUser-agent: qUiRe\nDisallow: /second\nAllow: /first/open\n",
    ]
    body = "\n".join(reversed(groups) if reverse else groups)
    for path, allowed in [
        ("/first", False),
        ("/second", False),
        ("/first/open", True),
        ("/unlisted", True),
    ]:
        assert_access(body, path, allowed)


def test_all_wildcard_groups_merge() -> None:
    body = "User-agent: *\nDisallow: /a\nUser-agent: *\nDisallow: /b\nAllow: /a/open"
    for path, allowed in [("/a", False), ("/b", False), ("/a/open", True), ("/c", True)]:
        assert_access(body, path, allowed)


@pytest.mark.parametrize(
    "body,allowed",
    [
        ("User-agent: *\nDisallow: /\nUser-agent: Quire\nDisallow:", True),
        ("User-agent: Quire\nAllow:\nUser-agent: *\nDisallow: /", True),
        ("User-agent: *\nDisallow: /\nUser-agent: Quire", True),
        ("User-agent: Other\nDisallow: /", True),
        ("User-agent: Qui\nDisallow: /", True),
        ("User-agent: QuireBot\nDisallow: /", True),
        ("User-agent: Quire\nUser-agent: *\nDisallow: /", False),
        ("Disallow: /\nUser-agent: Quire\nAllow: /", True),
        ("\ufeffUsEr-AgEnT: qUIRE # name\r\n\r\nDISALLOW: / # deny\r\n", False),
        (
            "User-agent: Other\nSitemap: https://example.test/map\nUser-agent: Quire\nDisallow: /",
            False,
        ),
        ("User-agent: Quire\nDisallow: /\nUnknown: ignored\nAllow: /public", False),
        ("# Comment\nmalformed line\n: value\nUser-agent: *\nDisallow", True),
    ],
)
def test_group_parsing(body: str, allowed: bool) -> None:
    assert_access(body, "/private", allowed)


def test_policy_cached_per_origin_and_robots_bypasses_lookup() -> None:
    requested: list[str] = []

    def fetch(url: str) -> str:
        requested.append(url)
        return "User-agent: *\nDisallow: /private"

    policy = RobotsPolicy(fetch)
    policy.check("https://example.test/robots.txt")
    assert not requested
    policy.check("https://example.test/open")
    with pytest.raises(BlockedError):
        policy.check("https://example.test/private?token=secret")
    policy.check("https://example.test/other")
    policy.check("http://example.test/open")
    policy.check("https://example.test:8443/open")
    assert requested == [
        "https://example.test/robots.txt",
        "http://example.test/robots.txt",
        "https://example.test:8443/robots.txt",
    ]


@pytest.mark.parametrize("status", [404, 410])
def test_missing_policy_cached_as_unrestricted(status: int) -> None:
    requested: list[str] = []

    def fetch(url: str) -> str:
        requested.append(url)
        raise HttpStatusError(url, status)

    policy = RobotsPolicy(fetch)
    policy.check("https://example.test/private")
    policy.check("https://example.test/other")
    assert requested == ["https://example.test/robots.txt"]


@pytest.mark.parametrize("status", [400, 401, 403, 429])
def test_client_error_policy_cached_as_unrestricted(status: int) -> None:
    """robots.txt 返回 4xx(含 CDN 反爬 403/429):按 REP 惯例视为没有限制。"""
    requested: list[str] = []

    def fetch(url: str) -> str:
        requested.append(url)
        raise HttpStatusError(url, status)

    policy = RobotsPolicy(fetch)
    policy.check("https://cdn.example.test/img/1.jpg")
    policy.check("https://cdn.example.test/img/2.jpg")
    assert requested == ["https://cdn.example.test/robots.txt"]


@pytest.mark.parametrize(
    "error",
    [
        HttpStatusError("https://example.test/robots.txt", 503),
        BlockedError("HTTP 403"),
        NetworkError("connection lost"),
    ],
)
def test_failed_lookup_is_unrestricted_and_cached(error: Exception) -> None:
    """robots.txt 获取失败(连接错误/5xx/反爬)一律视为不限制,且按 origin 缓存。"""
    requested: list[str] = []

    def fetch(url: str) -> str:
        requested.append(url)
        raise error

    policy = RobotsPolicy(fetch)
    policy.check("https://example.test/private")
    policy.check("https://example.test/other")
    assert requested == ["https://example.test/robots.txt"]


def test_concurrent_checks_fetch_policy_once() -> None:
    requested: list[str] = []

    def fetch(url: str) -> str:
        requested.append(url)
        return "User-agent: *\nDisallow: /private"

    policy = RobotsPolicy(fetch)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(policy.check, ["https://example.test/open"] * 16))
    assert requested == ["https://example.test/robots.txt"]


def test_fetcher_never_requests_denied_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[str] = []
    body = (
        "User-agent: *\nDisallow: /\n"
        "User-agent: Quire\nAllow: /\nDisallow: /private/\n"
        "User-agent: Quire\nDisallow: /*.jpg$\n"
    )

    def request(self: Fetcher, url: str, headers: object) -> Response:
        requested.append(url)
        content = body.encode() if url.endswith("/robots.txt") else b"ok"
        return Response(url, 200, {}, content, 0)

    monkeypatch.setattr(Fetcher, "_request", request)
    client = Fetcher(rate=1000000, retries=0)
    for path in ["/private/page", "/cover.jpg"]:
        with pytest.raises(BlockedError):
            client.get("https://example.test" + path)
    assert client.get("https://example.test/open").content == b"ok"
    assert requested == ["https://example.test/robots.txt", "https://example.test/open"]


def test_fetcher_respect_robots_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    """respect_robots=False 时完全不请求 robots.txt,Disallow 路径也直接抓取。"""
    requested: list[str] = []

    def request(self: Fetcher, url: str, headers: object) -> Response:
        requested.append(url)
        return Response(url, 200, {}, b"ok", 0)

    monkeypatch.setattr(Fetcher, "_request", request)
    client = Fetcher(rate=1000000, retries=0, respect_robots=False)
    assert client.get("https://example.test/private/page").content == b"ok"
    assert requested == ["https://example.test/private/page"]


def test_fetcher_image_requests_skip_robots(monkeypatch: pytest.MonkeyPatch) -> None:
    """图片二进制请求不查 robots(页面级策略只管 HTML),CDN 误伤不再发生。"""
    requested: list[str] = []

    def request(self: Fetcher, url: str, headers: object) -> Response:
        requested.append(url)
        content = b"User-agent: *\nDisallow: /" if url.endswith("/robots.txt") else b"ok"
        return Response(url, 200, {}, content, 0)

    monkeypatch.setattr(Fetcher, "_request", request)
    client = Fetcher(rate=1000000, retries=0)
    assert client.get("https://img.example.test/1.jpg", robots=False).content == b"ok"
    assert requested == ["https://img.example.test/1.jpg"]  # robots.txt 都没取
    with pytest.raises(BlockedError):  # 页面请求仍受 robots 约束
        client.get("https://img.example.test/1.jpg")
    assert requested[-1] == "https://img.example.test/robots.txt"


def test_micro_policy_imports_without_site_packages() -> None:
    source = str(Path(__file__).resolve().parents[1] / "src")
    code = (
        f"import sys; sys.path.insert(0, {source!r})\n"
        "from quire.fetch.robots import RobotsPolicy\n"
        "RobotsPolicy(lambda _: 'User-agent: *\\nAllow: /').check('https://example.test/')\n"
    )
    subprocess.run([sys.executable, "-I", "-S", "-B", "-c", code], timeout=5, check=True)


def test_wildcards_do_not_cause_regex_backtracking() -> None:
    code = (
        "from quire.fetch.robots import RobotsPolicy\n"
        "body = 'User-agent: *\\nDisallow: /' + '*a' * 1000 + 'b$'\n"
        "RobotsPolicy(lambda _: body).check('https://example.test/' + 'a' * 20000)\n"
    )
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    subprocess.run([sys.executable, "-B", "-c", code], env=environment, timeout=5, check=True)
