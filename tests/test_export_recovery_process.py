"""Recover durable exports after real process exits using only loopback HTTP."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from zipfile import ZipFile

from pypdf import PdfReader

from quire.export_receipt import ExportReceipt, read_receipt
from quire.store.ledger import Ledger
from tests.mock_site.server import MockSite, serve

ROOT = Path(__file__).resolve().parents[1]
FORMATS = ("pdf", "cbz", "zip")
IMAGE_PATHS = tuple(f"/images/{page}.jpg" for page in range(1, 4))
EXIT_CODE = 73
CHILD = r"""
import asyncio
import json
import os
import sys
from pathlib import Path

from quire import core_export, core_pages, core_publish, export_commit
from quire.core_manga import run_core_manga
from quire.manga import report_payload
from quire.models import MangaOptions
from quire.parse.images import FilterPolicy

url, directory, mode = sys.argv[1:]
root = Path(directory)
output = root / "book.pdf"
calls = {"trial_calls": 0, "encoding_calls": 0}
actual_trial = core_export._trial
actual_encode = core_pages.encode_pages

async def trial(*args, **kwargs):
    calls["trial_calls"] += 1
    if mode == "resume":
        raise AssertionError("Recovery must not build another encoding trial")
    return await actual_trial(*args, **kwargs)

def encode(*args, **kwargs):
    calls["encoding_calls"] += 1
    if mode == "resume":
        raise AssertionError("Recovery must not encode source images")
    return actual_encode(*args, **kwargs)

core_export._trial = trial
core_pages.encode_pages = encode

if mode == "after-pdf":
    actual_sync = export_commit.sync_directory

    def sync(path):
        actual_sync(path)
        if Path(path) == root and output.exists() and not output.with_suffix(".cbz").exists():
            os._exit(73)

    export_commit.sync_directory = sync
elif mode == "during-cleanup":
    actual_remove = core_publish.remove_cached

    def remove(cache_root, task_id, relative_path, **kwargs):
        actual_remove(cache_root, task_id, relative_path, **kwargs)
        assert not (cache_root / relative_path).exists()
        os._exit(73)

    core_publish.remove_cached = remove
elif mode == "linked-pdf":
    actual_link = export_commit.os.link

    def link(source, destination, **kwargs):
        actual_link(source, destination, **kwargs)
        if Path(source).name == "publish-pdf.tmp" and Path(destination) == output:
            os._exit(73)

    export_commit.os.link = link
else:
    assert mode == "resume", mode

result = asyncio.run(run_core_manga(
    url,
    output,
    formats=("pdf", "cbz", "zip"),
    workdir=root / "work",
    resume=mode == "resume",
    options=MangaOptions(
        selector="main.reader", concurrency=1, rate=1000, retries=0, timeout=5,
        policy=FilterPolicy(min_width=0, min_height=0, min_bytes=0),
    ),
))
assert mode == "resume", "Process did not reach its requested interruption point"
sys.stdout.write(json.dumps({**report_payload(result), **calls}) + "\n")
"""


def _child(site: MockSite, root: Path, mode: str) -> subprocess.CompletedProcess[str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.lower().endswith("_proxy")
    }
    environment.update(
        PYTHONPATH=str(ROOT / "src"), NO_PROXY="*", no_proxy="*", PYTHONUNBUFFERED="1"
    )
    process = subprocess.run(
        [sys.executable, "-c", CHILD, site.url + "/comic", str(root), mode],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    expected = 0 if mode == "resume" else EXIT_CODE
    assert process.returncode == expected, (
        f"{mode}: exit {process.returncode}, expected {expected}\n"
        f"stdout:\n{process.stdout}\nstderr:\n{process.stderr}"
    )
    return process


def _receipt(root: Path) -> ExportReceipt:
    records = list((root / "work" / "exports").glob("*.json"))
    assert len(records) == 1
    receipt = read_receipt(root / "work", records[0].stem, root / "book.pdf", FORMATS)
    assert receipt is not None and receipt.result is not None
    assert tuple(item.kind for item in receipt.files) == (*FORMATS, "report")
    assert receipt.result.pages_written == 3 and not receipt.result.partial
    return receipt


def _sources(root: Path, receipt: ExportReceipt) -> list[Path]:
    assert receipt.result is not None and receipt.result.task_id is not None
    with Ledger(root / "work") as ledger:
        snapshot = ledger.snapshot(receipt.result.task_id)
    assert len(snapshot.resources) == 3
    paths = []
    for resource in snapshot.resources:
        assert resource.status == "done" and resource.local_path is not None
        paths.append(root / "work" / resource.local_path)
    return paths


def _image_counts(site: MockSite) -> tuple[int, ...]:
    with site.lock:
        return tuple(site.counts[path] for path in IMAGE_PATHS)


def _resume_and_check(site: MockSite, root: Path, receipt: ExportReceipt) -> None:
    counts = _image_counts(site)
    assert counts == (1, 1, 1)
    candidates = {}
    published = {}
    for item in receipt.files:
        data = (receipt.workspace / item.candidate).read_bytes()
        assert len(data) == item.stamp.size
        assert hashlib.sha256(data).hexdigest() == item.stamp.sha256
        candidates[item.destination] = data
        if item.destination.exists():
            info = item.destination.stat()
            published[item.destination] = (info.st_dev, info.st_ino, info.st_mtime_ns)
    sources = _sources(root, receipt)

    result = json.loads(_child(site, root, "resume").stdout)

    assert _image_counts(site) == counts
    assert result["trial_calls"] == result["encoding_calls"] == 0
    assert result["artifacts_reused"] is True
    assert result["export_recovered"] is (receipt.state == "prepared")
    assert result["resources_reused"] == 0
    assert receipt.result is not None
    assert result["task_id"] == receipt.result.task_id
    assert result["status"] == "done" and result["pages"] == 3 and result["missing"] == 0
    complete = _receipt(root)
    assert complete.state == "complete" and complete.key == receipt.key
    assert complete.files == receipt.files
    for path, data in candidates.items():
        assert path.read_bytes() == data
        info = path.stat()
        assert info.st_nlink == 1
        if path in published:
            assert (info.st_dev, info.st_ino, info.st_mtime_ns) == published[path]
    assert all(not path.exists() for path in sources)
    assert not receipt.workspace.exists()
    assert not list(root.glob(".quire-export-*"))
    assert len(PdfReader(root / "book.pdf").pages) == 3
    for kind in ("cbz", "zip"):
        with ZipFile(root / f"book.{kind}") as archive:
            assert archive.testzip() is None
            assert len([name for name in archive.namelist() if name.endswith(".jpg")]) == 3
    report = json.loads((root / "book.report.json").read_text(encoding="utf-8"))
    assert report["task_id"] == result["task_id"]
    assert report["status"] == "done" and report["pages"] == 3 and report["missing"] == 0
    assert tuple(item["format"] for item in report["artifacts"]) == FORMATS


def test_prepared_after_pdf_exit_resumes_candidates_without_download_or_encoding(
    tmp_path: Path,
) -> None:
    with serve() as site:
        _child(site, tmp_path, "after-pdf")
        receipt = _receipt(tmp_path)
        assert receipt.state == "prepared"
        assert (tmp_path / "book.pdf").is_file()
        assert (tmp_path / "book.pdf").stat().st_nlink == 1
        assert not (receipt.workspace / "publish-pdf.tmp").exists()
        for kind in ("cbz", "zip", "report"):
            assert (receipt.workspace / f"publish-{kind}.tmp").is_file()
        assert all(not item.destination.exists() for item in receipt.files[1:])
        for path, image in zip(_sources(tmp_path, receipt), IMAGE_PATHS, strict=True):
            assert path.read_bytes() == site.images[image]

        _resume_and_check(site, tmp_path, receipt)


def test_complete_cleanup_exit_resumes_after_first_source_deleted_without_rebuilding(
    tmp_path: Path,
) -> None:
    with serve() as site:
        _child(site, tmp_path, "during-cleanup")
        receipt = _receipt(tmp_path)
        assert receipt.state == "complete"
        assert all(item.destination.is_file() for item in receipt.files)
        sources = _sources(tmp_path, receipt)
        assert [path.exists() for path in sources] == [False, True, True]
        for path, image in zip(sources[1:], IMAGE_PATHS[1:], strict=True):
            assert path.read_bytes() == site.images[image]
        assert receipt.workspace.is_dir()

        _resume_and_check(site, tmp_path, receipt)


def test_linked_pdf_exit_finishes_owned_two_link_publication_without_rebuilding(
    tmp_path: Path,
) -> None:
    with serve() as site:
        _child(site, tmp_path, "linked-pdf")
        receipt = _receipt(tmp_path)
        assert receipt.state == "prepared"
        output = tmp_path / "book.pdf"
        staged = receipt.workspace / "publish-pdf.tmp"
        assert output.samefile(staged)
        assert output.stat().st_nlink == staged.stat().st_nlink == 2
        assert all(not item.destination.exists() for item in receipt.files[1:])
        for path, image in zip(_sources(tmp_path, receipt), IMAGE_PATHS, strict=True):
            assert path.read_bytes() == site.images[image]

        _resume_and_check(site, tmp_path, receipt)
