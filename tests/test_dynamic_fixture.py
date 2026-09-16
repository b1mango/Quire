from __future__ import annotations

import io
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from urllib.robotparser import RobotFileParser

import pytest
from PIL import Image

from tests.mock_site.dynamic_server import serve


def test_dynamic_http_contract():
    with serve() as server:
        assert isinstance(server, ThreadingHTTPServer)
        assert server.server_address == ("127.0.0.1", server.server_port)
        client = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        try:
            client.request("GET", "/robots.txt")
            response = client.getresponse()
            robots = RobotFileParser()
            robots.parse(response.read().decode().splitlines())
            assert robots.can_fetch("quire", server.url + "/dynamic")
            assert not robots.can_fetch("quire", server.url + "/forbidden/reader.js")

            client.request("GET", "/redirect")
            response = client.getresponse()
            assert response.status == 302
            assert response.getheader("Location") == "/dynamic"
            response.read()

            for path in ("/dynamic", "/endless", "/delayed", "/blocked-script"):
                client.request("GET", path)
                response = client.getresponse()
                html = response.read().decode()
                assert response.status == 200
                assert "<h1>" in html and '<main class="reader"' in html
                assert "<img" not in html
                script = "/forbidden/reader.js" if path == "/blocked-script" else "/reader.js"
                assert f'src="{script}" defer' in html

            for path in ("/reader.js", "/forbidden/reader.js"):
                client.request("GET", path)
                response = client.getresponse()
                assert response.status == 200
                assert response.getheader("Content-Type").startswith("text/javascript")
                assert response.read()

            client.request("GET", "/missing?probe=1")
            response = client.getresponse()
            assert response.status == 404
            response.read()
            assert server.counts == dict.fromkeys(
                (
                    "/robots.txt",
                    "/redirect",
                    "/dynamic",
                    "/endless",
                    "/delayed",
                    "/blocked-script",
                    "/reader.js",
                    "/forbidden/reader.js",
                    "/missing",
                ),
                1,
            )
        finally:
            client.close()


def test_images_check_referer_and_keep_natural_sizes():
    with serve() as server:
        client = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        try:
            for referer in (None, server.url + ".invalid/dynamic"):
                headers = {} if referer is None else {"Referer": referer}
                client.request("GET", "/images/1.jpg", headers=headers)
                response = client.getresponse()
                assert response.status == 403
                response.read()
            for index in range(1, 4):
                client.request(
                    "GET",
                    f"/images/{index}.jpg?page={index}",
                    headers={"Referer": server.url + "/dynamic"},
                )
                response = client.getresponse()
                assert response.status == 200
                assert response.getheader("Content-Type") == "image/jpeg"
                with Image.open(io.BytesIO(response.read())) as image:
                    assert image.size == (400 + index, 600)
            assert server.counts == {"/images/1.jpg": 3, "/images/2.jpg": 1, "/images/3.jpg": 1}
        finally:
            client.close()


def test_cookie_is_set_in_header_and_presence_is_reported():
    with serve() as server:
        client = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        try:
            client.request("GET", "/cookie")
            response = client.getresponse()
            cookie = response.getheader("Set-Cookie")
            html = response.read().decode()
            assert "HttpOnly" in cookie
            assert '<img src="/images/1.jpg"' in html
            assert "fetch('/needs-cookie')" in html
            assert "document.cookie" not in html and "fixture_cookie" not in html

            for value in (None, cookie.split(";", 1)[0], "imported_cookie=test"):
                client.request(
                    "GET", "/needs-cookie", headers={} if value is None else {"Cookie": value}
                )
                response = client.getresponse()
                assert response.status == 200
                expected = (
                    b'{"cookie_received": false}'
                    if value is None
                    else (b'{"cookie_received": true}')
                )
                assert response.read() == expected
            assert server.counts == {"/cookie": 1, "/needs-cookie": 3}
        finally:
            client.close()


@pytest.mark.parametrize("fail", [False, True])
def test_server_closes_on_normal_and_exception_exit(fail):
    try:
        with serve() as server:
            if fail:
                raise RuntimeError("fixture failure")
    except RuntimeError as exc:
        assert fail and str(exc) == "fixture failure"
    assert server.socket.fileno() == -1
