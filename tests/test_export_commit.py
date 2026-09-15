"""Publication faults must preserve changed files and durable recovery candidates."""

from __future__ import annotations

import asyncio
import errno
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest

from quire import export_commit as commit
from quire.errors import FetchError, LedgerError
from quire.export_receipt import ExportReceipt, PublishedFile
from quire.store.export_files import fingerprint, make_workspace


@pytest.fixture
def prepared(tmp_path):
    key = "a" * 64
    workspace, identity = make_workspace(tmp_path, key)
    (workspace / "pass-1").mkdir()
    files = []
    for kind in ("pdf", "cbz", "report"):
        candidate = f"pass-1/book.{kind}" if kind != "report" else "book.report.json"
        source = workspace / candidate
        source.write_bytes(f"candidate-{kind}".encode())
        destination = tmp_path / (f"book.{kind}" if kind != "report" else "book.report.json")
        stamp = fingerprint(source)
        assert stamp is not None
        files.append(PublishedFile(kind, candidate, destination, stamp, None))
    return ExportReceipt(key, "prepared", workspace, identity, ("pdf", "cbz"), tuple(files))


@pytest.fixture
def linked(prepared):
    item = prepared.files[0]
    staged = prepared.workspace / "publish-pdf.tmp"
    staged.write_bytes((prepared.workspace / item.candidate).read_bytes())
    os.link(staged, item.destination)
    return staged


def assert_candidates_preserved(receipt):
    for item in receipt.files:
        assert fingerprint(receipt.workspace / item.candidate) == item.stamp


@pytest.mark.parametrize("atime_changes", [False, True])
def test_finish_link_removes_only_staged_name(prepared, linked, monkeypatch, atime_changes):
    item = prepared.files[0]
    inode = linked.stat().st_ino
    original_fstat = os.fstat
    snapshots = []

    def fstat(fd):
        value = original_fstat(fd)
        if value.st_ino == inode:
            snapshots.append(value)
            if atime_changes and len(snapshots) == 2:
                # Model read-driven atime changes without utime also changing ctime.
                fields = {
                    name: getattr(value, name) for name in dir(value) if name.startswith("st_")
                }
                fields.update(
                    st_atime=value.st_atime + 60, st_atime_ns=value.st_atime_ns + 60_000_000_000
                )
                return SimpleNamespace(**fields)
        return value

    monkeypatch.setattr(commit.os, "fstat", fstat)
    commit._finish_link(prepared, item)

    assert len(snapshots) == 2
    assert not linked.exists()
    assert item.destination.stat().st_ino == inode
    assert item.destination.stat().st_nlink == 1
    assert fingerprint(item.destination) == item.stamp
    assert_candidates_preserved(prepared)


def test_finish_link_rejects_third_link_without_unlinking(prepared, linked, tmp_path):
    item = prepared.files[0]
    extra = tmp_path / "user-link.pdf"
    os.link(linked, extra)

    with pytest.raises(LedgerError, match="Unexpected links"):
        commit._finish_link(prepared, item)

    assert extra.samefile(linked) and linked.samefile(item.destination)
    assert linked.stat().st_nlink == 3
    assert extra.read_bytes() == (prepared.workspace / item.candidate).read_bytes()
    assert_candidates_preserved(prepared)


@pytest.mark.parametrize("damage", ["size", "hash"])
def test_finish_link_rejects_receipt_mismatch_without_unlinking(prepared, linked, damage):
    item = prepared.files[0]
    content = b"changed" if damage == "size" else b"X" * item.stamp.size
    linked.write_bytes(content)

    message = "publication changed" if damage == "size" else "differs from receipt"
    with pytest.raises(LedgerError, match=message):
        commit._finish_link(prepared, item)

    assert linked.samefile(item.destination) and linked.stat().st_nlink == 2
    assert item.destination.read_bytes() == content
    assert_candidates_preserved(prepared)


def test_finish_link_rejects_replacement_between_stat_and_open(prepared, linked, monkeypatch):
    item = prepared.files[0]
    original_open = os.open
    saved = linked.with_suffix(".saved")

    def open_file(path, flags, *args, **kwargs):
        if path == linked:
            linked.rename(saved)
            linked.write_bytes(b"X" * item.stamp.size)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(commit.os, "open", open_file)
    with pytest.raises(LedgerError, match="publication changed"):
        commit._finish_link(prepared, item)

    assert saved.samefile(item.destination)
    assert linked.read_bytes() == b"X" * item.stamp.size
    assert item.destination.read_bytes() == (prepared.workspace / item.candidate).read_bytes()


@pytest.mark.parametrize("change", ["truncate", "append"])
def test_finish_link_bounds_read_and_rejects_change_even_when_digest_matches(
    prepared, linked, monkeypatch, change
):
    item = prepared.files[0]
    original_read = os.read
    reads = []

    def read(fd, size):
        block = original_read(fd, size)
        reads.append(size)
        if len(reads) == 1:
            with linked.open("wb" if change == "truncate" else "ab") as handle:
                handle.write(b"changed")
        return block

    monkeypatch.setattr(commit.os, "read", read)
    with pytest.raises(LedgerError, match="differs from receipt"):
        commit._finish_link(prepared, item)

    assert reads == [item.stamp.size] and linked.samefile(item.destination)
    assert item.destination.read_bytes().endswith(b"changed")
    assert linked.stat().st_nlink == 2
    assert_candidates_preserved(prepared)


@pytest.mark.parametrize("name", ["staged", "destination"])
def test_finish_link_rejects_path_replacement_after_hash(prepared, linked, monkeypatch, name):
    item = prepared.files[0]
    target = linked if name == "staged" else item.destination
    saved = target.with_suffix(".saved")
    inode = linked.stat().st_ino
    original_fstat = os.fstat
    calls = 0

    def fstat(fd):
        nonlocal calls
        value = original_fstat(fd)
        if value.st_ino == inode:
            calls += 1
            if calls == 2:
                target.rename(saved)
                target.write_bytes(b"user replacement")
        return value

    monkeypatch.setattr(commit.os, "fstat", fstat)
    with pytest.raises(LedgerError, match="paths changed"):
        commit._finish_link(prepared, item)

    assert calls == 2 and target.read_bytes() == b"user replacement"
    assert saved.read_bytes() == (prepared.workspace / item.candidate).read_bytes()
    assert linked.exists() and item.destination.exists()
    assert_candidates_preserved(prepared)


def test_finish_link_leaves_different_inodes_untouched(prepared, linked):
    item = prepared.files[0]
    item.destination.unlink()
    item.destination.write_bytes(b"user output")

    commit._finish_link(prepared, item)

    assert item.destination.read_bytes() == b"user output"
    assert fingerprint(linked) == item.stamp
    assert not linked.samefile(item.destination)


def test_sync_file_requires_existing_unchanged_candidate(prepared, tmp_path):
    item = prepared.files[0]
    assert commit.sync_file(prepared.workspace / item.candidate) == item.stamp
    with pytest.raises(LedgerError, match="candidate is missing"):
        commit.sync_file(tmp_path / "missing.pdf")


def test_sync_file_rejects_change_after_fsync(prepared, monkeypatch):
    source = prepared.workspace / prepared.files[0].candidate
    original_sync = os.fsync

    def sync(fd):
        original_sync(fd)
        source.write_bytes(b"user modified candidate")

    monkeypatch.setattr(commit.os, "fsync", sync)
    with pytest.raises(LedgerError, match="changed while syncing"):
        commit.sync_file(source)
    assert source.read_bytes() == b"user modified candidate"


@pytest.mark.parametrize("operation", ["finish-link", "stage"])
def test_bounded_read_rejects_early_eof_and_keeps_paths(prepared, monkeypatch, operation):
    item = prepared.files[0]
    source = prepared.workspace / item.candidate
    target = prepared.workspace / "publish-pdf.tmp" if operation == "finish-link" else source
    if operation == "finish-link":
        target.write_bytes(source.read_bytes())
        os.link(target, item.destination)
    original_read = os.read
    inode = target.stat().st_ino
    staging = operation == "finish-link"
    descriptors, reads = [], []

    def checked(path):
        nonlocal staging
        value = fingerprint(path)
        if path == prepared.workspace / "publish-pdf.tmp":
            staging = True
        return value

    def read(fd, size):
        if staging and os.fstat(fd).st_ino == inode:
            descriptors.append(fd)
            reads.append(size)
            target.write_bytes(b"")
        return original_read(fd, size)

    monkeypatch.setattr(commit, "fingerprint", checked)
    monkeypatch.setattr(commit.os, "read", read)
    with pytest.raises(LedgerError, match="was truncated"):
        if operation == "finish-link":
            commit._finish_link(prepared, item)
        else:
            asyncio.run(commit._stage(prepared, item))
    assert reads == [item.stamp.size] and target.read_bytes() == b""
    with pytest.raises(OSError) as closed:
        os.fstat(descriptors[0])
    assert closed.value.errno == errno.EBADF
    if operation == "finish-link":
        assert target.samefile(item.destination) and target.stat().st_nlink == 2
        assert_candidates_preserved(prepared)
    else:
        assert not item.destination.exists()


@pytest.mark.parametrize("when", ["before", "open", "during", "synced-source", "synced-staged"])
def test_stage_rejects_changed_source_without_publishing(prepared, monkeypatch, when):
    item = prepared.files[0]
    source = prepared.workspace / item.candidate
    original_sleep, original_sync = asyncio.sleep, os.fsync
    changed = False

    async def sleep(delay):
        nonlocal changed
        if not changed:
            changed = True
            source.write_bytes(b"changed")
        await original_sleep(delay)

    def checked(path):
        value = fingerprint(path)
        if path == prepared.workspace / "publish-pdf.tmp":
            source.write_bytes(b"changed")
        return value

    def sync(fd):
        original_sync(fd)
        target = prepared.workspace / "publish-pdf.tmp" if when == "synced-staged" else source
        target.write_bytes(b"changed")

    if when == "before":
        source.write_bytes(b"changed")
    elif when == "open":
        monkeypatch.setattr(commit, "fingerprint", checked)
    elif when == "during":
        monkeypatch.setattr(commit.asyncio, "sleep", sleep)
    else:
        monkeypatch.setattr(commit.os, "fsync", sync)
    message = {"before": "differs from receipt", "open": "changed before staging"}.get(
        when, "changed while staging"
    )
    with pytest.raises(LedgerError, match=message):
        asyncio.run(commit._stage(prepared, item))

    assert source.read_bytes() == (b"candidate-pdf" if when == "synced-staged" else b"changed")
    assert all(not file.destination.exists() for file in prepared.files)
    if when == "during":
        assert changed
        assert fingerprint(prepared.workspace / "publish-pdf.tmp") == item.stamp


@pytest.mark.parametrize("when", ["prepared", "before-publication", "staged", "after-publication"])
def test_publish_rejects_changed_files_and_preserves_candidates(prepared, monkeypatch, when):
    item = prepared.files[0]
    target = prepared.workspace / "publish-pdf.tmp" if when == "staged" else item.destination
    original_sync = commit.sync_directory
    changed = False

    def sync(path):
        nonlocal changed
        original_sync(path)
        ready = (
            all(file.destination.exists() for file in prepared.files)
            if when == "after-publication"
            else path == prepared.workspace
        )
        if ready and not changed:
            changed = True
            target.write_bytes(b"user content")

    if when == "prepared":
        target.write_bytes(b"user content")
    else:
        monkeypatch.setattr(commit, "sync_directory", sync)
    messages = {
        "prepared": "Output changed since preparation",
        "before-publication": "Output changed before publication",
        "staged": "Staged output changed before publication",
        "after-publication": "Published outputs failed verification",
    }
    with pytest.raises(LedgerError, match=messages[when]):
        asyncio.run(commit.publish(prepared))

    assert target.read_bytes() == b"user content"
    assert_candidates_preserved(prepared)
    if when == "after-publication":
        assert all(fingerprint(file.destination) == file.stamp for file in prepared.files[1:])
    else:
        assert all(not file.destination.exists() for file in prepared.files[1:])
    if when != "prepared":
        assert changed


@pytest.mark.parametrize("failed_index", [0, 1])
def test_link_failure_reports_committed_files_and_leaves_retryable_candidates(
    prepared, monkeypatch, failed_index
):
    original_link = os.link

    def link(source, destination, **kwargs):
        if destination == prepared.files[failed_index].destination:
            raise OSError(errno.EIO, "injected publication failure")
        return original_link(source, destination, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(commit.os, "link", link)
        with pytest.raises(FetchError) as error:
            asyncio.run(commit.publish(prepared))
    assert ("book.pdf" in str(error.value)) is bool(failed_index)
    assert "--resume" in str(error.value)
    for index, item in enumerate(prepared.files):
        assert item.destination.exists() is (index < failed_index)
    assert_candidates_preserved(prepared)

    asyncio.run(commit.publish(prepared))
    assert commit.matches(prepared)
    assert not list(prepared.workspace.glob("publish-*.tmp"))
    assert_candidates_preserved(prepared)


def test_publish_replaces_recorded_previous_output_and_reuses_matching_file(prepared):
    first, second, report = prepared.files
    first.destination.write_bytes(b"previous authorized output")
    first = replace(first, before=fingerprint(first.destination))
    second.destination.write_bytes((prepared.workspace / second.candidate).read_bytes())
    reused_inode = second.destination.stat().st_ino
    receipt = replace(prepared, files=(first, second, report))

    asyncio.run(commit.publish(receipt))

    assert commit.matches(receipt)
    assert second.destination.stat().st_ino == reused_inode
    assert all(item.destination.stat().st_nlink == 1 for item in receipt.files)
    assert_candidates_preserved(receipt)


def test_stage_workspace_swap_after_staged_fingerprint_preserves_external_files(
    prepared, tmp_path, monkeypatch
):
    item = prepared.files[0]
    staged = prepared.workspace / "publish-pdf.tmp"
    staged.write_bytes(b"previous staging")
    external, saved = tmp_path / "external", tmp_path / "saved-workspace"
    (external / "pass-1").mkdir(parents=True)
    external_files = {
        external / staged.name: b"user staging",
        external / item.candidate: b"user source",
    }
    for path, content in external_files.items():
        path.write_bytes(content)
    inodes = {path: path.stat().st_ino for path in external_files}
    swapped = False

    def checked(path):
        nonlocal swapped
        value = fingerprint(path)
        if path == staged and not swapped:
            swapped = True
            prepared.workspace.rename(saved)
            prepared.workspace.symlink_to(external, target_is_directory=True)
        return value

    monkeypatch.setattr(commit, "fingerprint", checked)
    with pytest.raises(LedgerError):
        asyncio.run(commit._stage(prepared, item))

    assert swapped and prepared.workspace.is_symlink()
    for path, content in external_files.items():
        assert path.read_bytes() == content and path.stat().st_ino == inodes[path]
    assert (saved / staged.name).read_bytes() == b"previous staging"
    assert_candidates_preserved(replace(prepared, workspace=saved))
    assert not item.destination.exists()


def test_publish_rechecks_old_target_after_staged_fingerprint(prepared, monkeypatch):
    first = prepared.files[0]
    first.destination.write_bytes(b"previous authorized output")
    first = replace(first, before=fingerprint(first.destination))
    receipt = replace(prepared, files=(first, *prepared.files[1:]))
    staged = receipt.workspace / "publish-pdf.tmp"
    ready = changed = False
    original_sync = commit.sync_directory

    def sync(path):
        nonlocal ready
        original_sync(path)
        if path == receipt.workspace:
            ready = True

    def checked(path):
        nonlocal changed
        value = fingerprint(path)
        if ready and path == staged and not changed:
            changed = True
            first.destination.write_bytes(b"user modified output")
        return value

    monkeypatch.setattr(commit, "sync_directory", sync)
    monkeypatch.setattr(commit, "fingerprint", checked)
    with pytest.raises(LedgerError, match="Output changed before publication"):
        asyncio.run(commit.publish(receipt))

    assert changed and first.destination.read_bytes() == b"user modified output"
    assert fingerprint(staged) == first.stamp
    assert all(not item.destination.exists() for item in receipt.files[1:])
    assert_candidates_preserved(receipt)


@pytest.mark.parametrize("fail_sync", [False, True])
def test_publish_syncs_parent_even_when_all_destinations_match(prepared, monkeypatch, fail_sync):
    for item in prepared.files:
        item.destination.write_bytes((prepared.workspace / item.candidate).read_bytes())
    inodes = {item.destination: item.destination.stat().st_ino for item in prepared.files}
    parent = prepared.files[0].destination.parent
    identity = (parent.stat().st_dev, parent.stat().st_ino)
    original_sync = os.fsync
    synced = []

    def sync(fd):
        value = os.fstat(fd)
        if (value.st_dev, value.st_ino) == identity:
            synced.append(parent)
            if fail_sync:
                raise OSError(errno.EIO, "injected parent sync failure")
        original_sync(fd)

    monkeypatch.setattr(commit.os, "fsync", sync)
    if fail_sync:
        with pytest.raises(FetchError) as error:
            asyncio.run(commit.publish(prepared))
        assert "--resume" in str(error.value)
        assert all(item.destination.name in str(error.value) for item in prepared.files)
    else:
        asyncio.run(commit.publish(prepared))

    assert synced == [parent]
    assert commit.matches(prepared)
    assert all(path.stat().st_ino == inode for path, inode in inodes.items())
    assert not list(prepared.workspace.glob("publish-*.tmp"))
    assert_candidates_preserved(prepared)
