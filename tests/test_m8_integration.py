"""Real local HTTP UI/CLI flow and focused M8 failure regressions."""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from pypdf import PdfReader

from quire.cli import main
from quire.core_discovery import discover_manga
from quire.core_series import run_series
from quire.errors import ConfigError
from quire.fetch.session import AsyncFetcher
from quire.models import MangaOptions
from quire.parse.minidom import parse
from quire.server.job_state import JobSpec
from quire.sites.diagnostics import diagnose
from quire.sites.rules import SiteRule, new_rule
from tests.mock_site.server import page_image
from tests.test_async_fetch import answer
from tests.test_m8 import CATALOGUE, URL, client, rule
from tests.test_server_e2e import _request, _wait_job
from tests.test_server_e2e import ui as ui


@contextmanager
def site():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/robots.txt":
                body = b"User-agent: *\nAllow: /"
            elif self.path == "/book":
                body = CATALOGUE.encode()
            elif self.path.startswith("/chapter/"):
                n = self.path.rsplit("/", 1)[-1]
                body = f'<main class="reader"><img src="/pages/{n}.jpg"></main>'.encode()
            else:
                body = page_image(int(self.path.rsplit("/", 1)[-1].split(".")[0]))
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def test_rule_series_ui_and_cli(ui, tmp_path, capsys):
    server, _ = ui
    path = new_rule(server.data_root, "127.0.0.1")
    path.write_text(path.read_text().replace('# volumes = ".volume"', 'volumes = ".volume"'))
    with site() as base:
        status, probe = _request(server, "POST", "/api/probe", {"url": base + "/book"})
        assert status == 200 and probe["series"] and len(probe["volumes"]) == 2
        status, job = _request(
            server,
            "POST",
            "/api/jobs",
            {
                "url": base + "/book",
                "kind": "manga",
                "title": "山海集",
                "formats": ["pdf"],
                "series": True,
                "split_by": "volume",
                "volumes": [2],
            },
        )
        assert status == 201
        finished = _wait_job(server, job["id"])
        assert finished["status"] == "done", finished
        _, books = _request(server, "GET", "/api/books")
        assert len(books["books"]) == 1 and "第二卷" in books["books"][0]["title"]
        assert any(e.kind == "volume" for e in server.manager.get(job["id"]).events_after(0, 0))
        assert (
            main(
                ["sites", "--data-dir", str(server.data_root), "test", "127.0.0.1", base + "/book"]
            )
            == 0
        )
        assert (
            main(
                [
                    "inspect",
                    base + "/book",
                    "--data-dir",
                    str(server.data_root),
                    "--dump-html",
                    str(tmp_path / "debug.html"),
                    "--explain",
                ]
            )
            == 0
        )
        assert (tmp_path / "debug.html").exists()
        assert '"granularity": "series"' in capsys.readouterr().out
        assert (
            main(
                [
                    "series",
                    base + "/book",
                    "--data-dir",
                    str(server.data_root),
                    "--split-by",
                    "chapters",
                    "2",
                    "--from",
                    "1",
                    "--to",
                    "2",
                    "--keep-images",
                    "--rate",
                    "1000",
                    "-q",
                    "-o",
                    str(tmp_path / "cli"),
                ]
            )
            == 0
        )
        report = json.loads((tmp_path / "cli/第1卷.report.json").read_text())
        assert (
            main(
                [
                    "reassemble",
                    report["task_id"],
                    "--workdir",
                    str(tmp_path / "cli/.quire-core"),
                    "-o",
                    str(tmp_path / "offline.pdf"),
                ]
            )
            == 0
        )
    assert len(PdfReader(tmp_path / "offline.pdf").pages) == 2


@pytest.mark.parametrize(
    "payload",
    [
        {"series": 1},
        {"split_by": "chapters 0"},
        {"volumes": (0,)},
        {"series": True, "kind": "novel", "formats": ("txt",)},
        {"series": True, "split_by": "size 50MB", "volumes": (1,)},
    ],
)
def test_series_job_validation(payload):
    with pytest.raises(ConfigError):
        JobSpec(**{"url": URL, "kind": "manga", "title": "x", "formats": ("pdf",), **payload})


def test_pagination_removal_dedup_and_cycle():
    def handler(request):
        if request.url.path == "/robots.txt":
            return answer(request, data=b"User-agent: *\nAllow: /")
        number = int(request.url.params.get("page", "1"))
        next_link = '<a class="next" href="?page=2">下一页</a>' if number == 1 else ""
        body = (
            f'<title>A\x01B</title><main><img src="/{number}.jpg">'
            '<aside class="ad"><img src="/bad.jpg"></aside></main>' + next_link
        )
        return answer(request, data=body.encode())

    async def run(first=1):
        async with AsyncFetcher(rate=10000, transport=httpx.MockTransport(handler)) as fetcher:
            return await discover_manga(
                fetcher, URL, MangaOptions(next_selector="a.next", remove=(".ad",), first=first)
            )

    candidates, result = asyncio.run(run())
    assert [c.url for c in candidates] == ["https://series.test/1.jpg", "https://series.test/2.jpg"]
    assert "\x01" not in result.title
    with pytest.raises(ConfigError):
        asyncio.run(run(3))

    def loop_handler(request):
        if request.url.path == "/robots.txt":
            return answer(request, data=b"User-agent: *\nAllow: /")
        n = int(request.url.params.get("page", "1"))
        return answer(
            request,
            data=f'<img src="/1.jpg"><a class="next" href="?page={3 - n}">下一页</a>'.encode(),
        )

    async def loop():
        async with AsyncFetcher(rate=10000, transport=httpx.MockTransport(loop_handler)) as fetcher:
            return await discover_manga(
                fetcher, URL + "?page=1", MangaOptions(next_selector="a.next")
            )

    with pytest.raises(ConfigError, match="循环"):
        asyncio.run(loop())


def test_sequential_large_trial_keeps_same_books(tmp_path, monkeypatch):
    from quire import core_export

    monkeypatch.setattr(core_export, "_MEASURE_THRESHOLD", 1)
    result = asyncio.run(
        run_series(
            URL,
            tmp_path / "s",
            split_by="volume",
            rule=rule(tmp_path),
            formats=("pdf", "cbz", "zip"),
            fetcher=client(),
        )
    )
    assert len(result.volumes) == 2
    assert all(
        len(PdfReader(v.output).pages) == 2 and len(v.artifacts) == 3 for v in result.volumes
    )
    assert not list((tmp_path / "s").glob(".quire-export-*"))


def test_novel_and_selector_diagnostics():
    html = (
        '<div id="chapters"><a href="/ch1">第一章</a></div><article>'
        + "".join(
            f"<p>这是第{i}段小说中的正文，用于检验内容是否符合文本提取要求。</p>" for i in range(20)
        )
        + "</article>"
    )
    data = diagnose(
        parse(html), URL, SiteRule("novel", ("series.test",), "novel", content_selector="article")
    )
    assert data["text_valid"]
    broken = diagnose(
        parse(html), URL, SiteRule("novel", ("series.test",), chapter_links="#absent")
    )
    assert broken["suggestions"][0]["selector"] == "#chapters a"


def test_series_cli_passes_reading_and_network_options(tmp_path, monkeypatch):
    from quire import core_series

    seen = []

    async def run(url, output, **kwargs):
        seen.append(kwargs["options"])
        return core_series.SeriesResult("sample", ())

    monkeypatch.setattr(core_series, "run_series", run)
    assert (
        main(
            [
                "series",
                URL,
                "--data-dir",
                str(tmp_path),
                "--order",
                "desc",
                "--referer",
                "https://ref.test/",
                "--max-bytes",
                "12345",
            ]
        )
        == 0
    )
    assert (seen[0].order, seen[0].referer, seen[0].max_bytes) == (
        "desc",
        "https://ref.test/",
        12345,
    )


def test_probe_applies_custom_image_attributes_and_removal(tmp_path, monkeypatch):
    from quire.server import probe

    path = new_rule(tmp_path, "series.test")
    path.write_text(
        path.read_text().replace(
            '# image_attrs = ["data-src", "src"]',
            'image_attrs = ["data-page-custom"]\nremove = [".unwanted"]',
        )
    )

    def handler(request):
        body = (
            b"User-agent: *\nAllow: /"
            if request.url.path == "/robots.txt"
            else b'<main class="reader"><img data-page-custom="/1.jpg"><div class="unwanted"><img data-page-custom="/2.jpg"></div></main>'
        )
        return answer(request, data=body)

    monkeypatch.setattr(
        probe,
        "AsyncFetcher",
        lambda **kwargs: AsyncFetcher(rate=10000, transport=httpx.MockTransport(handler)),
    )
    result = asyncio.run(probe.probe_url(URL, data_root=tmp_path))
    assert result.kind == "manga" and result.count == 1
