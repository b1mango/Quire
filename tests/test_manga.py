from __future__ import annotations

import json
from dataclasses import replace

import pytest
from pypdf import PdfReader

from quire.cli import main
from quire.errors import ConfigError
from quire.manga import MangaOptions, run_local, run_manga
from tests.mock_site.server import page_image, serve


@pytest.fixture
def site():
    with serve() as server:
        yield server


def options(**kwargs):
    return MangaOptions(rate=1000, retries=0, selector="main.reader", concurrency=3, **kwargs)


class Progress:
    def __init__(self):
        self.calls = []

    def update(self, done, total, result):
        self.calls.append((done, total))


def widths(path):
    return [int(p["/Resources"]["/XObject"]["/Im0"]["/Width"]) for p in PdfReader(path).pages]


def test_order_parallel_progress_cleanup_and_report(site, tmp_path):
    sink = Progress()
    result = run_manga(site.url + "/comic", tmp_path / "book.pdf", options=options(), progress=sink)
    assert widths(result.output) == [401, 402, 403]
    assert result.pages_written == 3 and not result.partial
    assert sink.calls == [(1, 3), (2, 3), (3, 3)]
    assert 2 <= site.peak <= 3
    assert not list((tmp_path / ".quire-work").iterdir())
    assert json.loads(result.report.read_text())["status"] == "done"
    assert not site.counts["/placeholder.gif"]


def test_partial_and_keep_images(site, tmp_path):
    result = run_manga(
        site.url + "/partial", tmp_path / "book.pdf", options=options(keep_images=True)
    )
    pages = PdfReader(result.output).pages
    assert len(pages) == 3 and result.pages_written == 2 and result.pages_failed == 1
    assert "MISSING PAGE" in pages[1].extract_text()
    assert len(list(result.images_dir.glob("*.jpg"))) == 2
    assert json.loads(result.report.read_text())["status"] == "partial"


def test_fallback_does_not_add_duplicate_pages(site, tmp_path):
    result = run_manga(site.url + "/fallback", tmp_path / "book.pdf", options=options())
    assert widths(result.output) == [401, 402, 403]
    assert not result.partial
    assert site.counts["/missing.jpg"] == 1


def test_failure_keeps_existing_output_and_unrelated_cache(site, tmp_path):
    out = tmp_path / "book.pdf"
    out.write_bytes(b"original")
    work = tmp_path / "work"
    work.mkdir()
    unrelated = work / "keep.txt"
    unrelated.write_text("original")

    class Interrupted:
        def update(self, *_):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_manga(
            site.url + "/comic",
            out,
            options=options(overwrite=True),
            workdir=work,
            progress=Interrupted(),
        )
    assert out.read_bytes() == b"original"
    assert list(work.iterdir()) == [unrelated]
    assert not list(tmp_path.glob(".book.pdf.*"))


def test_invalid_options_and_ranges_fail_before_output(site, tmp_path):
    for kwargs in [{"concurrency": 0}, {"rate": float("nan")}, {"first": -1}, {"last": -1}]:
        with pytest.raises(ConfigError):
            MangaOptions(**kwargs)
    with pytest.raises(ConfigError):
        run_manga(site.url + "/comic", tmp_path / "empty.pdf", options=options(first=99))
    assert not (tmp_path / "empty.pdf").exists()


def test_cli_exit_codes_and_existing_file(site, tmp_path, capsys):
    out = tmp_path / "out.pdf"
    args = [
        "manga",
        site.url + "/partial",
        "-o",
        str(out),
        "--rate",
        "1000",
        "--retries",
        "0",
        "--selector",
        "main",
    ]
    assert main(args) == 4
    assert main(args) == 4
    assert out.with_name("out (1).pdf").exists()
    assert main(["manga", site.url + "/comic", "--concurrency", "0"]) == 1
    assert main(["manga"]) == 1
    assert main(["manga", site.url + "/comic", "--selector", "img:first-child"]) == 1
    assert "Traceback" not in capsys.readouterr().err


def test_local_sort_missing_and_no_input_deletion(tmp_path):
    images = tmp_path / "images"
    images.mkdir()
    (images / "10.jpg").write_bytes(page_image(3))
    (images / "2.jpg").write_bytes(page_image(2))
    (images / "1.jpg").write_bytes(page_image(1))
    result = run_local(images, tmp_path / "local.pdf")
    assert widths(result.output) == [401, 402, 403]
    assert len(list(images.iterdir())) == 3
    with pytest.raises(FileExistsError):
        run_local(images, result.output)
    run_local(images, result.output, options=replace(MangaOptions(), overwrite=True))
