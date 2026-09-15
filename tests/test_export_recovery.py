from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from quire.errors import ConfigError, FetchError, LedgerError
from quire.image.options import CompressionOptions
from quire.models import MangaOptions
from quire.store.export_files import fingerprint
from tests.test_multiformat import capture


def receipt(root):
    path = next((root / ".quire-core/exports").glob("*.json"))
    return path, json.loads(path.read_text())


def no_encoding(*_):
    raise AssertionError("Verified exports must not be encoded again")


def interrupt(root, monkeypatch):
    original = os.link

    def fail(source, target, **kwargs):
        if target.suffix == ".cbz":
            raise OSError("injected publication failure")
        return original(source, target, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr("quire.export_commit.os.link", fail)
        with pytest.raises(FetchError, match="已发布：book.pdf"):
            capture(root)
    return receipt(root)


def test_complete_group_reuse_after_source_cleanup(tmp_path, monkeypatch):
    first = capture(tmp_path)
    original = {a.path: fingerprint(a.path) for a in first.artifacts}
    assert not list(tmp_path.glob(".quire-export-*"))
    monkeypatch.setattr("quire.core_pages.encode_pages", no_encoding)
    requests = []
    second = capture(tmp_path, resume=True, requests=requests)
    assert second.artifacts_reused and not second.export_recovered and second.resources_reused == 0
    assert requests == ["/robots.txt", "/chapter"]
    assert all(fingerprint(path) == stamp for path, stamp in original.items())


def test_prepared_group_resumes_without_download_or_encoding(tmp_path, monkeypatch):
    _, record = interrupt(tmp_path, monkeypatch)
    assert record["state"] == "prepared"
    monkeypatch.setattr("quire.core_pages.encode_pages", no_encoding)
    requests = []
    result = capture(tmp_path, resume=True, requests=requests)
    assert result.artifacts_reused and result.export_recovered
    assert requests == ["/robots.txt", "/chapter"]
    assert receipt(tmp_path)[1]["state"] == "complete"
    assert not Path(record["workspace"]).exists()


@pytest.mark.parametrize("kind", ["modified", "missing"])
def test_completed_damage_requires_explicit_rebuild(tmp_path, kind):
    first = capture(tmp_path)
    target = first.artifacts[-1].path
    target.write_bytes(b"user modified") if kind == "modified" else target.unlink()
    with pytest.raises(ConfigError, match="--overwrite"):
        capture(tmp_path, resume=True)
    result = capture(tmp_path, resume=True, options=MangaOptions(overwrite=True))
    assert not result.artifacts_reused and not result.partial
    assert target.read_bytes().startswith(b"PK")


def test_prepared_user_edit_never_overwritten_even_with_flag(tmp_path, monkeypatch):
    interrupt(tmp_path, monkeypatch)
    target = tmp_path / "book.pdf"
    target.write_bytes(b"user edit")
    with pytest.raises(LedgerError, match="Output changed"):
        capture(tmp_path, resume=True, options=MangaOptions(overwrite=True))
    assert target.read_bytes() == b"user edit"
    assert not (tmp_path / "book.cbz").exists()


@pytest.mark.parametrize("kind", ["missing", "corrupt", "link"])
def test_prepared_candidate_integrity_before_any_additional_publish(tmp_path, monkeypatch, kind):
    _, record = interrupt(tmp_path, monkeypatch)
    path = Path(record["workspace"]) / record["files"][1]["candidate"]
    path.unlink()
    if kind == "corrupt":
        path.write_bytes(b"broken")
    if kind == "link":
        path.symlink_to(tmp_path / "book.pdf")
    with pytest.raises(LedgerError):
        capture(tmp_path, resume=True)
    assert not (tmp_path / "book.cbz").exists()
    assert len(list((tmp_path / ".quire-core/cache").rglob("*.png"))) == 3


def test_partial_retries_missing_sources_at_same_output(tmp_path):
    first = capture(tmp_path, missing=True)
    requests = []
    second = capture(tmp_path, resume=True, requests=requests)
    assert first.partial and not second.partial and not second.artifacts_reused
    assert second.resources_reused == 2
    assert requests == ["/robots.txt", "/chapter", "/two"]
    assert not list((tmp_path / ".quire-core/cache").rglob("*.png"))


def test_unmet_target_reuses_outputs_and_retains_sources(tmp_path, monkeypatch):
    compression = CompressionOptions(target_bytes=1)
    capture(tmp_path, compression=compression)
    monkeypatch.setattr("quire.core_pages.encode_pages", no_encoding)
    result = capture(tmp_path, resume=True, compression=compression)
    assert result.artifacts_reused and result.target_met is False
    assert len(list((tmp_path / ".quire-core/cache").rglob("*.png"))) == 3


def test_keep_images_changed_export_needs_sources(tmp_path):
    capture(tmp_path)
    requests = []
    result = capture(
        tmp_path,
        resume=True,
        requests=requests,
        options=MangaOptions(keep_images=True, overwrite=True),
    )
    assert not result.artifacts_reused and result.resources_reused == 0
    assert len(list(result.images_dir.glob("*.png"))) == 3
    assert len(requests) == 5


@pytest.mark.parametrize("kind", ["missing", "corrupt"])
def test_keep_images_checks_sources_before_reusing(tmp_path, kind):
    opts = MangaOptions(keep_images=True)
    first = capture(tmp_path, options=opts)
    source = next(first.images_dir.glob("*.png"))
    source.unlink() if kind == "missing" else source.write_bytes(b"damaged")
    requests = []
    result = capture(tmp_path, resume=True, requests=requests, options=opts)
    assert not result.artifacts_reused and result.resources_reused == 2
    assert len(requests) == 3 and len(list(result.images_dir.glob("*.png"))) == 3


def test_cleanup_interruption_preserves_changed_source(tmp_path, monkeypatch):
    from quire.core_publish import remove_cached

    changed = []

    def modify(root, task_id, relative, **kwargs):
        path = root / relative
        path.write_bytes(b"user content")
        changed.append(path)
        return remove_cached(root, task_id, relative, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr("quire.core_publish.remove_cached", modify)
        with pytest.raises(LedgerError):
            capture(tmp_path)
    assert receipt(tmp_path)[1]["state"] == "complete"
    monkeypatch.setattr("quire.core_pages.encode_pages", no_encoding)
    with pytest.raises(LedgerError):
        capture(tmp_path, resume=True)
    assert changed[0].read_bytes() == b"user content"


def test_building_failure_recovers_known_workspace(tmp_path, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr("quire.core_pages.encode_pages", no_encoding)
        with pytest.raises(AssertionError):
            capture(tmp_path)
    _, record = receipt(tmp_path)
    assert record["state"] == "building"
    result = capture(tmp_path, resume=True)
    assert result.resources_reused == 3 and not result.artifacts_reused
    assert not Path(record["workspace"]).exists()


@pytest.mark.parametrize("kind", ["unknown", "replaced", "symlink"])
def test_recovery_refuses_unowned_workspace(tmp_path, monkeypatch, kind):
    _, record = interrupt(tmp_path, monkeypatch)
    workspace = Path(record["workspace"])
    if kind == "unknown":
        extra = workspace / "personal.txt"
        extra.write_text("personal")
    else:
        saved = workspace.with_name(workspace.name + "-saved")
        workspace.rename(saved)
        workspace.mkdir() if kind == "replaced" else workspace.symlink_to(
            saved, target_is_directory=True
        )
        extra = saved / "personal.txt"
        extra.write_text("personal")
    with pytest.raises(LedgerError):
        capture(tmp_path, resume=True)
    assert extra.read_text() == "personal"


def test_cancel_staging_resumes_from_prepared(tmp_path, monkeypatch):
    from quire.export_commit import _stage

    async def cancel(record, item):
        await _stage(record, item)
        raise asyncio.CancelledError

    with monkeypatch.context() as patch:
        patch.setattr("quire.export_commit._stage", cancel)
        with pytest.raises(asyncio.CancelledError):
            capture(tmp_path)
    assert not (tmp_path / "book.pdf").exists()
    assert receipt(tmp_path)[1]["state"] == "prepared"
    monkeypatch.setattr("quire.core_pages.encode_pages", no_encoding)
    assert capture(tmp_path, resume=True).export_recovered


def test_new_output_parent_and_old_unregistered_temporary_survive(tmp_path):
    old = tmp_path / ".quire-encode-old"
    old.mkdir()
    (old / "personal.txt").write_text("keep")
    result = capture(tmp_path / "new")
    assert result.report.is_file() and (old / "personal.txt").read_text() == "keep"


def test_old_cleaned_export_does_not_delete_later_retained_sources(tmp_path, monkeypatch):
    root = tmp_path / "work"
    capture(tmp_path / "first", formats=("pdf",), workdir=root)
    retained = capture(
        tmp_path / "second",
        formats=("cbz",),
        resume=True,
        workdir=root,
        options=MangaOptions(keep_images=True),
    )
    sources = {path: path.read_bytes() for path in retained.images_dir.glob("*.png")}
    monkeypatch.setattr("quire.core_pages.encode_pages", no_encoding)
    result = capture(tmp_path / "first", formats=("pdf",), resume=True, workdir=root)
    assert result.artifacts_reused
    assert sources and all(path.read_bytes() == data for path, data in sources.items())


def test_same_size_changed_cache_survives_pending_cleanup(tmp_path, monkeypatch):
    from quire.core_publish import remove_cached

    changed = []

    def modify(root, task_id, relative, **kwargs):
        path = root / relative
        path.write_bytes(b"x" * path.stat().st_size)
        changed.append(path)
        return remove_cached(root, task_id, relative, **kwargs)

    monkeypatch.setattr("quire.core_publish.remove_cached", modify)
    with pytest.raises(LedgerError, match="preserved"):
        capture(tmp_path)
    assert changed[0].read_bytes().startswith(b"xxx")


@pytest.mark.parametrize(
    "payload", ['{"schema":1,"schema":1}', '{"result":{"missing":1,"missing":0}}']
)
def test_duplicate_receipt_keys_rejected_without_touching_outputs(tmp_path, payload):
    first = capture(tmp_path, missing=True)
    path, _ = receipt(tmp_path)
    before = first.output.read_bytes()
    path.write_text(payload)
    with pytest.raises(LedgerError, match="JSON"):
        capture(tmp_path, resume=True)
    assert first.output.read_bytes() == before


def test_partial_rebuild_interruption_keeps_authorized_outputs(tmp_path, monkeypatch):
    capture(tmp_path, missing=True)
    with monkeypatch.context() as patch:
        patch.setattr("quire.core_pages.encode_pages", no_encoding)
        with pytest.raises(AssertionError):
            capture(tmp_path, resume=True)
    assert receipt(tmp_path)[1]["state"] == "building"
    result = capture(tmp_path, resume=True)
    assert not result.partial and result.resources_reused == 3


@pytest.mark.parametrize("damage", ["delete", "modify"])
def test_output_changes_during_cleanup_stop_and_preserve_candidates(tmp_path, monkeypatch, damage):
    from quire.core_publish import remove_cached

    def alter(root, task_id, relative, **kwargs):
        remove_cached(root, task_id, relative, **kwargs)
        target = tmp_path / "book.pdf"
        target.unlink() if damage == "delete" else target.write_bytes(b"user change")

    monkeypatch.setattr("quire.core_publish.remove_cached", alter)
    with pytest.raises(LedgerError, match="during cleanup"):
        capture(tmp_path)
    _, record = receipt(tmp_path)
    assert not record["cleaned"] and record["state"] == "complete"
    assert len(list((tmp_path / ".quire-core/cache").rglob("*.png"))) == 2
    assert (Path(record["workspace"]) / record["files"][0]["candidate"]).is_file()
