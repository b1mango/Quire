from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from quire.errors import LedgerError
from quire.store.cache import fingerprint, publish_bytes, read_cached, remove_cached

TASK = "a" * 64
RELATIVE = f"cache/{TASK}/page.jpg"
DATA = b"verified image bytes" * 100_000


def existing(root, data=b"old image"):
    path = root / RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.mark.parametrize("task", ["", "a" * 63, "A" * 64, "g" * 64, "../escape"])
def test_invalid_task_rejected_before_writes(tmp_path, task):
    with pytest.raises(LedgerError):
        publish_bytes(tmp_path, task, "page.jpg", b"image")
    with pytest.raises(LedgerError):
        remove_cached(tmp_path, task, f"cache/{task}/page.jpg")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "filename",
    [
        "",
        ".",
        "..",
        "Page.jpg",
        "caf\u00e9.jpg",
        "/page.jpg",
        "a/b",
        "a\\b",
        "a\x00",
        "page.jpg/",
        "./page.jpg",
        "../page.jpg",
        "a b",
        "page.jpg\n",
    ],
)
def test_invalid_single_filename_rejected(tmp_path, filename):
    with pytest.raises(LedgerError):
        publish_bytes(tmp_path, TASK, filename, b"image")
    with pytest.raises(LedgerError):
        remove_cached(tmp_path, TASK, f"cache/{TASK}/{filename}")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "relative", ["/tmp/page.jpg", "../page.jpg", "ledger.db", f"cache/{'b' * 64}/page.jpg"]
)
def test_remove_rejects_unowned_paths(tmp_path, relative):
    with pytest.raises(LedgerError):
        remove_cached(tmp_path, TASK, relative)


def test_empty_data_rejected_before_creating_directories(tmp_path):
    with pytest.raises(LedgerError, match="empty"):
        publish_bytes(tmp_path, TASK, "page.jpg", b"")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("preexisting", [False, True])
def test_publish_private_permissions_and_fingerprint(tmp_path, preexisting):
    if preexisting:
        path = existing(tmp_path)
        path.parent.chmod(0o777)
        path.parent.parent.chmod(0o777)
        path.chmod(0o666)
    previous_umask = os.umask(0o077)
    try:
        assert publish_bytes(tmp_path, TASK, "page.jpg", DATA) == RELATIVE
    finally:
        os.umask(previous_umask)
    assert (tmp_path / RELATIVE).read_bytes() == DATA
    for relative, mode in [("cache", 0o700), (f"cache/{TASK}", 0o700), (RELATIVE, 0o600)]:
        assert stat.S_IMODE((tmp_path / relative).stat().st_mode) == mode
    assert (tmp_path / RELATIVE).stat().st_nlink == 1
    assert {p.name for p in (tmp_path / RELATIVE).parent.iterdir()} == {"page.jpg"}
    assert fingerprint(tmp_path, TASK, RELATIVE).sha256 == hashlib.sha256(DATA).hexdigest()


def test_replace_is_atomic_and_syncs_file_before_directories(tmp_path, monkeypatch):
    path = existing(tmp_path)
    original_inode = path.stat().st_ino
    real_replace, real_sync, real_write = os.replace, os.fsync, os.write
    events = []

    def write(fd, data):
        assert path.read_bytes() == b"old image"
        return real_write(fd, data[:7])

    def sync(fd):
        events.append("file" if stat.S_ISREG(os.fstat(fd).st_mode) else "directory")
        return real_sync(fd)

    def replace(src, dst, *, src_dir_fd, dst_dir_fd):
        assert src_dir_fd == dst_dir_fd
        assert path.read_bytes() == b"old image"
        assert events == ["file", "file"]
        real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
        assert path.read_bytes() == b"complete new image"
        events.append("replace")

    monkeypatch.setattr("quire.store.cache.os.write", write)
    monkeypatch.setattr("quire.store.cache.os.fsync", sync)
    monkeypatch.setattr("quire.store.cache.os.replace", replace)
    publish_bytes(tmp_path, TASK, "page.jpg", b"complete new image")
    assert events == ["file", "file", "replace", "directory", "directory", "directory"]
    assert path.stat().st_ino != original_inode


@pytest.mark.parametrize("preexisting", [False, True])
@pytest.mark.parametrize(
    "failure", ["write", "zero", "file-sync", "replace", "task-sync", "cache-sync", "root-sync"]
)
def test_write_failure_rolls_back_and_removes_temporary(
    tmp_path, monkeypatch, preexisting, failure
):
    path = existing(tmp_path) if preexisting else tmp_path / RELATIVE
    real_write, real_sync, real_replace = os.write, os.fsync, os.replace
    calls = 0
    failed = False

    def write(fd, data):
        nonlocal failed
        if failure == "zero":
            return 0
        if failure == "write":
            if failed:
                raise OSError("disk full after partial write")
            failed = True
            return real_write(fd, data[:1])
        return real_write(fd, data)

    def sync(fd):
        nonlocal calls
        calls += 1
        sync_at = {
            "file-sync": 1,
            "task-sync": 2 + preexisting,
            "cache-sync": 3 + preexisting,
            "root-sync": 4 + preexisting,
        }.get(failure)
        if calls == sync_at:
            raise OSError("sync failure")
        return real_sync(fd)

    def replace(src, dst, **kwargs):
        if failure == "replace":
            raise OSError("replace failure")
        return real_replace(src, dst, **kwargs)

    monkeypatch.setattr("quire.store.cache.os.write", write)
    monkeypatch.setattr("quire.store.cache.os.fsync", sync)
    monkeypatch.setattr("quire.store.cache.os.replace", replace)
    with pytest.raises(LedgerError):
        publish_bytes(tmp_path, TASK, "page.jpg", b"new image")
    assert sorted(p.name for p in path.parent.iterdir()) == (["page.jpg"] if preexisting else [])
    if preexisting:
        assert path.read_bytes() == b"old image"
        assert path.stat().st_nlink == 1


@pytest.mark.parametrize("operation", ["publish", "remove", "read"])
@pytest.mark.parametrize(
    "kind", ["root", "cache", "task", "symlink", "dangling", "hardlink", "fifo", "directory"]
)
def test_link_and_nonregular_boundaries(tmp_path, operation, kind):
    root = tmp_path / "root"
    path = existing(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "page.jpg"
    target.write_bytes(b"outside")
    if kind in {"root", "cache", "task"}:
        folder = {"root": root, "cache": root / "cache", "task": path.parent}[kind]
        folder.rename(tmp_path / "old")
        folder.symlink_to(outside, target_is_directory=True)
    else:
        path.unlink()
        if kind in {"symlink", "dangling"}:
            path.symlink_to(target if kind == "symlink" else outside / "missing")
        elif kind == "hardlink":
            os.link(target, path)
        elif kind == "fifo":
            os.mkfifo(path)
        else:
            path.mkdir()
    with pytest.raises(LedgerError):
        if operation == "publish":
            publish_bytes(root, TASK, "page.jpg", b"new image")
        elif operation == "remove":
            remove_cached(root, TASK, RELATIVE)
        else:
            read_cached(root, TASK, RELATIVE, sha256=hashlib.sha256(b"outside").hexdigest(), size=7)
    assert target.read_bytes() == b"outside"
    assert {p.name for p in outside.iterdir()} == {"page.jpg"}


@pytest.mark.parametrize("missing", ["root", "cache", "task", "file"])
def test_remove_missing_does_not_create_directories(tmp_path, missing):
    root = tmp_path / "root"
    if missing != "root":
        root.mkdir()
    if missing in {"task", "file"}:
        (root / "cache").mkdir()
    if missing == "file":
        (root / "cache" / TASK).mkdir()
    before = sorted(tmp_path.rglob("*"))
    remove_cached(root, TASK, RELATIVE)
    assert sorted(tmp_path.rglob("*")) == before


def test_remove_only_known_file_including_empty_corrupt_file(tmp_path):
    path = existing(tmp_path, b"")
    unknown = path.parent / "unknown"
    unknown.mkdir()
    (unknown / "data").write_bytes(b"keep")
    (path.parent / "unknown-link").symlink_to(unknown)
    remove_cached(tmp_path, TASK, RELATIVE)
    remove_cached(tmp_path, TASK, RELATIVE)
    assert not path.exists()
    assert (unknown / "data").read_bytes() == b"keep"
    assert (path.parent / "unknown-link").is_symlink()


@pytest.mark.parametrize("operation", ["publish", "remove"])
@pytest.mark.parametrize("level", ["root", "cache", "task"])
@pytest.mark.parametrize("replacement", ["directory", "symlink"])
def test_parent_replacement_cannot_redirect_mutations(
    tmp_path, monkeypatch, operation, level, replacement
):
    root = tmp_path / "root"
    path = existing(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "page.jpg"
    target.write_bytes(b"outside")
    folder = {"root": root, "cache": root / "cache", "task": path.parent}[level]
    real_write, real_unlink = os.write, os.unlink
    switched = False

    def swap():
        nonlocal switched
        if switched:
            return
        switched = True
        folder.rename(tmp_path / "old")
        if replacement == "symlink":
            folder.symlink_to(outside, target_is_directory=True)
        else:
            folder.mkdir()
            (folder / "page.jpg").write_bytes(b"replacement")

    def write(fd, data):
        swap()
        return real_write(fd, data)

    def unlink(name, *, dir_fd):
        swap()
        return real_unlink(name, dir_fd=dir_fd)

    monkeypatch.setattr("quire.store.cache.os.write", write)
    if operation == "remove":
        monkeypatch.setattr("quire.store.cache.os.unlink", unlink)
    with pytest.raises(LedgerError):
        if operation == "publish":
            publish_bytes(root, TASK, "page.jpg", b"new image")
        else:
            remove_cached(root, TASK, RELATIVE)
    assert target.read_bytes() == b"outside"
    if replacement == "directory":
        assert (folder / "page.jpg").read_bytes() == b"replacement"
    assert not list((tmp_path / "old").rglob("*.tmp"))


def test_read_returns_exact_verified_bytes_with_bounded_reads(tmp_path, monkeypatch):
    existing(tmp_path, DATA)
    real_read = os.read
    sizes = []

    def read(fd, size):
        sizes.append(size)
        return real_read(fd, size)

    monkeypatch.setattr("quire.store.cache.os.read", read)
    assert (
        read_cached(
            tmp_path, TASK, RELATIVE, sha256=hashlib.sha256(DATA).hexdigest(), size=len(DATA)
        )
        == DATA
    )
    assert sum(sizes) == len(DATA)
    assert max(sizes) <= 1024 * 1024


@pytest.mark.parametrize("change", ["hash", "size", "zero", "bad-hash", "append", "symlink"])
def test_read_rejects_tampering_and_path_replacement(tmp_path, monkeypatch, change):
    path = existing(tmp_path, b"image")
    expected_hash = hashlib.sha256(b"image").hexdigest()
    expected_size = 5
    if change == "hash":
        path.write_bytes(b"other")
    elif change == "size":
        expected_size = 6
    elif change == "zero":
        expected_size = 0
    elif change == "bad-hash":
        expected_hash = "bad"
    else:
        real_read = os.read

        def read(fd, size):
            if change == "append":
                path.write_bytes(b"image-extra")
            else:
                path.rename(path.parent / "old")
                path.symlink_to(path.parent / "old")
            return real_read(fd, size)

        monkeypatch.setattr("quire.store.cache.os.read", read)
    with pytest.raises(LedgerError):
        read_cached(tmp_path, TASK, RELATIVE, sha256=expected_hash, size=expected_size)


@pytest.mark.parametrize("boundary", ["replace", "sync"])
def test_parent_swap_at_publish_boundary_rolls_back_pinned_directory(
    tmp_path, monkeypatch, boundary
):
    path = existing(tmp_path)
    old = tmp_path / "old"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "page.jpg").write_bytes(b"outside")
    real_replace, real_sync = os.replace, os.fsync
    swapped = False

    def swap():
        nonlocal swapped
        if not swapped:
            swapped = True
            path.parent.rename(old)
            path.parent.symlink_to(outside, target_is_directory=True)

    def replace(src, dst, **kwargs):
        if boundary == "replace":
            swap()
        return real_replace(src, dst, **kwargs)

    def sync(fd):
        if boundary == "sync" and stat.S_ISDIR(os.fstat(fd).st_mode):
            swap()
        return real_sync(fd)

    monkeypatch.setattr("quire.store.cache.os.replace", replace)
    monkeypatch.setattr("quire.store.cache.os.fsync", sync)
    with pytest.raises(LedgerError):
        publish_bytes(tmp_path, TASK, "page.jpg", b"new image")
    assert (outside / "page.jpg").read_bytes() == b"outside"
    assert (old / "page.jpg").read_bytes() == b"old image"
    assert {p.name for p in old.iterdir()} == {"page.jpg"}


def test_rollback_failure_retains_original_backup(tmp_path, monkeypatch):
    path = existing(tmp_path)
    real_replace, real_sync = os.replace, os.fsync

    def replace(src, dst, **kwargs):
        if src.endswith(".bak"):
            raise OSError("rollback failed")
        return real_replace(src, dst, **kwargs)

    def sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("directory sync failed")
        return real_sync(fd)

    monkeypatch.setattr("quire.store.cache.os.replace", replace)
    monkeypatch.setattr("quire.store.cache.os.fsync", sync)
    with pytest.raises(LedgerError):
        publish_bytes(tmp_path, TASK, "page.jpg", b"new image")
    backups = list(path.parent.glob("*.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == b"old image"
    assert not list(path.parent.glob("*.tmp"))


def test_exit_after_backup_before_replace_does_not_block_next_publish(tmp_path):
    path = existing(tmp_path, b"damaged cache")
    original_inode = path.stat().st_ino
    unknown = path.parent / "unknown"
    unknown.write_bytes(b"keep")
    script = """
import os
import sys
from pathlib import Path
from quire.store import cache

def interrupted_replace(*args, **kwargs):
    os._exit(73)

cache.os.replace = interrupted_replace
cache.publish_bytes(Path(sys.argv[1]), sys.argv[2], "page.jpg", b"new image")
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), TASK],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 73, result.stderr.decode()
    backups = list(path.parent.glob("*.bak"))
    assert len(backups) == 1 and backups[0].read_bytes() == b"damaged cache"
    assert backups[0].stat().st_ino != original_inode
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert backups[0].stat().st_nlink == path.stat().st_nlink == 1
    assert path.stat().st_ino == original_inode and path.read_bytes() == b"damaged cache"
    stale = {p.name: p.read_bytes() for p in path.parent.iterdir() if p != path}
    assert publish_bytes(tmp_path, TASK, "page.jpg", b"recovered") == RELATIVE
    assert path.read_bytes() == b"recovered" and path.stat().st_nlink == 1
    assert {p.name: p.read_bytes() for p in path.parent.iterdir() if p != path} == stale


@pytest.mark.parametrize("failure", ["truncate", "append", "symlink", "copy-sync"])
def test_backup_rejects_source_changes_and_copy_sync_failure(tmp_path, monkeypatch, failure):
    path = existing(tmp_path)
    real_read, real_sync = os.read, os.fsync
    syncs = 0

    def read(fd, size):
        if failure == "truncate":
            path.write_bytes(b"")
        elif failure == "append":
            path.write_bytes(b"old image changed")
        elif failure == "symlink":
            path.rename(path.parent / "unknown")
            path.symlink_to(path.parent / "unknown")
        return real_read(fd, size)

    def sync(fd):
        nonlocal syncs
        syncs += 1
        if failure == "copy-sync" and syncs == 2:
            raise OSError("backup sync failed")
        return real_sync(fd)

    monkeypatch.setattr("quire.store.cache.os.read", read)
    monkeypatch.setattr("quire.store.cache.os.fsync", sync)
    with pytest.raises(LedgerError):
        publish_bytes(tmp_path, TASK, "page.jpg", b"new image")
    assert not list(path.parent.glob("*.bak")) and not list(path.parent.glob("*.tmp"))
    if failure == "symlink":
        assert path.is_symlink() and (path.parent / "unknown").read_bytes() == b"old image"
    elif failure == "copy-sync":
        assert path.read_bytes() == b"old image" and path.stat().st_nlink == 1


def test_backup_copy_is_bounded_and_oversized_source_is_not_read(tmp_path, monkeypatch):
    path = existing(tmp_path, DATA)
    real_read = os.read
    sizes = []

    def read(fd, size):
        sizes.append(size)
        return real_read(fd, size)

    monkeypatch.setattr("quire.store.cache.os.read", read)
    publish_bytes(tmp_path, TASK, "page.jpg", b"new image")
    assert sum(sizes) == len(DATA) and max(sizes) <= 1024 * 1024
    with path.open("r+b") as handle:
        handle.truncate(128 * 1024 * 1024 + 1)
    sizes.clear()
    with pytest.raises(LedgerError, match="backup size limit"):
        publish_bytes(tmp_path, TASK, "page.jpg", b"new image")
    assert sizes == [] and path.stat().st_size == 128 * 1024 * 1024 + 1
    assert {p.name for p in path.parent.iterdir()} == {"page.jpg"}
