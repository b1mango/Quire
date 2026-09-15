from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from pypdf import PdfReader

from quire.cli import main
from quire.core_manga import run_core_manga
from quire.errors import ConfigError, LedgerError
from quire.fetch.session import AsyncFetcher
from quire.models import MangaOptions
from quire.store.ledger import Ledger
from tests.mock_site.server import page_image, serve
from tests.test_async_fetch import answer
from tests.test_manga import widths
from tests.test_png_safety import _png


def options(**kwargs):
    return MangaOptions(rate=1000, retries=0, selector="main.reader", **kwargs)


@pytest.fixture
def site():
    with serve() as server:
        yield server


def test_parallel_order_report_cleanup_and_resume_after_cleanup(site, tmp_path):
    url = site.url + "/comic"
    result = asyncio.run(run_core_manga(url, tmp_path / "book.pdf", options=options()))
    assert widths(result.output) == [401, 402, 403]
    assert 2 <= site.peak <= 4
    assert not result.partial and result.resources_reused == 0
    root = tmp_path / ".quire-core"
    assert not list((root / "cache").rglob("*.jpg"))
    report = json.loads(result.report.read_text())
    assert report["task_id"] == result.task_id
    assert report["resources_reused"] == 0
    with pytest.raises(ConfigError, match="--resume"):
        asyncio.run(run_core_manga(url, tmp_path / "again.pdf", options=options()))
    resumed = asyncio.run(
        run_core_manga(url, tmp_path / "again.pdf", options=options(), resume=True)
    )
    assert resumed.resources_reused == 0 and widths(resumed.output) == [401, 402, 403]
    assert all(site.counts[f"/images/{n}.jpg"] == 2 for n in range(1, 4))


def test_recovery_reuses_cache_with_new_rate_and_downloads_only_corrupt_page(site, tmp_path):
    root = tmp_path / "work"
    opts = options(keep_images=True)
    result = asyncio.run(
        run_core_manga(site.url + "/comic", tmp_path / "book.pdf", options=opts, workdir=root)
    )
    files = sorted(result.images_dir.glob("*.jpg"))
    assert len(files) == 3
    files[1].write_bytes(b"damaged")
    resumed = asyncio.run(
        run_core_manga(
            site.url + "/comic",
            tmp_path / "fixed.pdf",
            options=replace(opts, rate=500),
            workdir=root,
            resume=True,
        )
    )
    assert resumed.task_id == result.task_id and resumed.resources_reused == 2
    assert [site.counts[f"/images/{n}.jpg"] for n in range(1, 4)] == [1, 2, 1]
    assert widths(resumed.output) == [401, 402, 403]
    cached = asyncio.run(
        run_core_manga(
            site.url + "/comic",
            tmp_path / "cached.pdf",
            options=opts,
            workdir=root,
            resume=True,
        )
    )
    assert cached.resources_reused == 3
    changed = asyncio.run(
        run_core_manga(
            site.url + "/comic",
            tmp_path / "range.pdf",
            options=replace(opts, last=1),
            workdir=root,
            resume=True,
        )
    )
    assert changed.task_id != result.task_id and changed.resources_reused == 0


def test_partial_keeps_recovery_cache_and_fallback_uses_one_page(site, tmp_path):
    result = asyncio.run(
        run_core_manga(
            site.url + "/partial",
            tmp_path / "partial.pdf",
            options=options(),
        )
    )
    assert result.pages_failed == 1 and result.pages_written == 2
    assert "MISSING PAGE" in PdfReader(result.output).pages[1].extract_text()
    assert len(list((tmp_path / ".quire-core" / "cache" / result.task_id).glob("*.jpg"))) == 2
    resumed = asyncio.run(
        run_core_manga(
            site.url + "/partial",
            tmp_path / "retry.pdf",
            options=options(),
            resume=True,
        )
    )
    assert resumed.resources_reused == 2 and resumed.pages_failed == 1
    assert site.counts["/missing.jpg"] == 2 and site.counts["/images/1.jpg"] == 1
    fallback = asyncio.run(
        run_core_manga(
            site.url + "/fallback",
            tmp_path / "fallback.pdf",
            options=options(),
        )
    )
    assert widths(fallback.output) == [401, 402, 403]


def test_preflight_output_and_selector_protect_files(site, tmp_path):
    out = tmp_path / "book.pdf"
    out.write_bytes(b"original")
    with pytest.raises(ConfigError, match="目标已存在"):
        asyncio.run(run_core_manga(site.url + "/comic", out))
    with pytest.raises(ValueError):
        asyncio.run(
            run_core_manga(
                site.url + "/comic",
                tmp_path / "bad.pdf",
                options=MangaOptions(selector="img:first-child"),
            )
        )
    assert not site.counts and out.read_bytes() == b"original"


def test_export_failure_keeps_committed_cache_and_original_output(site, tmp_path):
    class Interrupted:
        def update(self, *_):
            raise OSError("injected export failure")

    out = tmp_path / "book.pdf"
    out.write_bytes(b"original")
    with pytest.raises(OSError, match="injected"):
        asyncio.run(
            run_core_manga(
                site.url + "/comic",
                out,
                options=options(overwrite=True),
                progress=Interrupted(),
            )
        )
    assert out.read_bytes() == b"original"
    assert len(list((tmp_path / ".quire-core" / "cache").rglob("*.jpg"))) == 3
    result = asyncio.run(
        run_core_manga(
            site.url + "/comic",
            out,
            options=options(overwrite=True),
            resume=True,
        )
    )
    assert result.resources_reused == 3 and widths(out) == [401, 402, 403]


def test_report_failure_preserves_cache(site, tmp_path):
    report = tmp_path / "book.report.json"
    report.write_text("existing")
    result = asyncio.run(
        run_core_manga(
            site.url + "/comic",
            tmp_path / "book.pdf",
            options=options(),
        )
    )
    assert result.report is None and result.warnings
    assert report.read_text() == "existing"
    assert len(list((tmp_path / ".quire-core" / "cache").rglob("*.jpg"))) == 3


def test_cli_core_partial_recovery_and_missing_capability(site, tmp_path, monkeypatch, capsys):
    args = [
        "manga",
        site.url + "/partial",
        "--core",
        "--rate",
        "1000",
        "--retries",
        "0",
        "--selector",
        "main.reader",
        "-o",
        str(tmp_path / "cli.pdf"),
    ]
    assert main(args) == 4
    assert main([*args, "--resume"]) == 4
    assert (tmp_path / "cli (1).pdf").exists()
    assert main(["manga", site.url + "/comic", "--resume"]) == 1
    monkeypatch.setattr("quire.cli._module_available", lambda _: False)
    assert main(args) == 6
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize("bad", [b"not an image", b"x" * 10001])
def test_invalid_images_are_recorded_as_placeholders(tmp_path, bad):
    def handler(request):
        if request.url.path == "/robots.txt":
            return answer(request, 404)
        if request.url.path == "/page":
            return answer(request, data=b'<img src="/bad.jpg">')
        return answer(request, data=bad)

    client = AsyncFetcher(transport=httpx.MockTransport(handler), rate=1e9)
    result = asyncio.run(
        run_core_manga(
            "https://example.test/page",
            tmp_path / "bad.pdf",
            fetcher=client,
            options=MangaOptions(max_bytes=10000),
        )
    )
    assert result.pages_failed == 1
    with Ledger(tmp_path / ".quire-core") as ledger:
        assert ledger.snapshot(result.task_id).resources[0].error_code == "invalid_image"


def test_policy_denial_records_blocked_image(tmp_path):
    def handler(request):
        if request.url.path == "/robots.txt":
            return answer(request, data=b"User-agent: *\nDisallow: /bad.jpg")
        return answer(request, data=b'<img src="/bad.jpg">')

    result = asyncio.run(
        run_core_manga(
            "https://example.test/page",
            tmp_path / "bad.pdf",
            fetcher=AsyncFetcher(transport=httpx.MockTransport(handler), rate=1e9),
        )
    )
    with Ledger(tmp_path / ".quire-core") as ledger:
        assert ledger.snapshot(result.task_id).resources[0].error_code == "blocked"


def test_cancelled_worker_settles_and_later_resume_reuses_completed_page(tmp_path):
    async def run():
        ready = asyncio.Event()

        async def handler(request):
            path = request.url.path
            if path == "/robots.txt":
                return answer(request, 404)
            if path == "/page":
                return answer(request, data=b'<img src="/1.jpg"><img src="/2.jpg">')
            if path == "/2.jpg":
                ready.set()
                await asyncio.Event().wait()
            return answer(request, data=page_image(1))

        task = asyncio.create_task(
            run_core_manga(
                "https://example.test/page",
                tmp_path / "book.pdf",
                options=MangaOptions(concurrency=1),
                fetcher=AsyncFetcher(transport=httpx.MockTransport(handler), rate=1e9),
            )
        )
        await asyncio.wait_for(ready.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert not (tmp_path / "book.pdf").exists()
    root = tmp_path / ".quire-core"
    task_id = next((root / "cache").iterdir()).name
    with Ledger(root) as ledger:
        records = ledger.snapshot(task_id).resources
        assert [r.status for r in records] == ["done", "failed"]
        assert records[1].error_code == "cancelled"


def test_disk_commit_failure_propagates_and_workers_close(site, tmp_path, monkeypatch):
    def fail(*_):
        raise LedgerError("injected disk failure")

    monkeypatch.setattr("quire.image.downloader.publish_bytes", fail)
    with pytest.raises(LedgerError, match="injected disk"):
        asyncio.run(run_core_manga(site.url + "/comic", tmp_path / "book.pdf", options=options()))
    assert not (tmp_path / "book.pdf").exists()


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("format", ["png", "webp"])
def test_unusable_image_is_not_committed_and_can_retry_or_use_alternative(
    tmp_path, fallback, format
):
    import io
    import zlib

    from PIL import Image

    counts = {"/bad.png": 0, "/good.jpg": 0}
    damaged = True

    def handler(request):
        if request.url.path == "/robots.txt":
            return answer(request, 404)
        if request.url.path == "/page":
            image = (
                b'<img data-src="/bad.png" src="/good.jpg">'
                if fallback
                else b'<img src="/bad.png">'
            )
            return answer(request, data=image)
        counts[request.url.path] += 1
        if request.url.path == "/good.jpg":
            data = page_image(1)
        elif format == "webp" and damaged:
            buffer = io.BytesIO()
            Image.new("RGB", (256, 256), "red").save(buffer, "WEBP")
            data = buffer.getvalue()
        else:
            data = _png(b"not zlib" if damaged else zlib.compress(bytes((256 * 3 + 1) * 256)))
        return answer(request, data=data)

    def run(name, resume=False):
        return asyncio.run(
            run_core_manga(
                "https://example.test/page",
                tmp_path / name,
                resume=resume,
                fetcher=AsyncFetcher(transport=httpx.MockTransport(handler), rate=1e9),
            )
        )

    result = run("first.pdf")
    if fallback:
        assert result.pages_written == 1 and not result.partial
        assert counts == {"/bad.png": 1, "/good.jpg": 1}
    else:
        assert result.pages_failed == 1
        with Ledger(tmp_path / ".quire-core") as ledger:
            assert ledger.snapshot(result.task_id).resources[0].status == "failed"
        damaged = False
        result = run("retry.pdf", resume=True)
        assert result.pages_written == 1 and result.resources_reused == 0
        assert counts["/bad.png"] == 2
