from __future__ import annotations

import hashlib
import os

import pytest

from quire.errors import LedgerError
from quire.store.cache import fingerprint

TASK = "a" * 64


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/image",
        "../image",
        f"cache/{TASK}/../image",
        f"cache/{TASK}//image",
        f"cache/{TASK}/./image",
        f"cache/{TASK}/image\x00",
        f"cache/{TASK}/folder\\image",
        "cache/other/image",
        "ledger.db",
        f"cache/{TASK}",
    ],
)
def test_cache_rejects_unowned_or_noncanonical_path(tmp_path, path):
    with pytest.raises(LedgerError):
        fingerprint(tmp_path, TASK, path)


def test_bad_task_id_cannot_select_cache(tmp_path):
    with pytest.raises(LedgerError):
        fingerprint(tmp_path, "../escape", "cache/../escape/page")


@pytest.mark.parametrize(
    "kind", ["parent-link", "file-link", "hardlink", "fifo", "directory", "empty"]
)
def test_nonregular_and_linked_files_are_rejected_without_blocking(tmp_path, kind):
    folder = tmp_path / "cache" / TASK
    folder.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    original = external / "image"
    original.write_bytes(b"original")
    path = folder / "image"
    if kind == "parent-link":
        folder.rmdir()
        folder.symlink_to(external, target_is_directory=True)
    elif kind == "file-link":
        path.symlink_to(original)
    elif kind == "hardlink":
        os.link(original, path)
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "directory":
        path.mkdir()
    else:
        path.touch()
    with pytest.raises(LedgerError):
        fingerprint(tmp_path, TASK, f"cache/{TASK}/image")
    assert original.read_bytes() == b"original"


def test_streaming_fingerprint_and_fsync(tmp_path):
    path = tmp_path / "cache" / TASK / "chapter" / "image"
    path.parent.mkdir(parents=True)
    content = b"abc" * 1_000_000
    path.write_bytes(content)
    relative = path.relative_to(tmp_path).as_posix()
    stamp = fingerprint(tmp_path, TASK, relative, sync=True)
    assert stamp.path == relative and stamp.size == len(content)
    assert stamp.sha256 == hashlib.sha256(content).hexdigest()


@pytest.mark.parametrize("change", ["truncate", "append"])
def test_concurrent_modification_rejected(tmp_path, monkeypatch, change):
    path = tmp_path / "cache" / TASK / "image"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"a" * 10)
    real_read = os.read

    def read(fd, size):
        if change == "truncate":
            path.write_bytes(b"")
        else:
            with path.open("ab") as handle:
                handle.write(b"b")
        return real_read(fd, size)

    monkeypatch.setattr("quire.store.cache.os.read", read)
    with pytest.raises(LedgerError, match="changed"):
        fingerprint(tmp_path, TASK, f"cache/{TASK}/image")


@pytest.mark.parametrize("replacement", ["directory", "symlink", "file"])
def test_path_replacement_during_hashing_is_rejected(tmp_path, monkeypatch, replacement):
    folder = tmp_path / "cache" / TASK
    folder.mkdir(parents=True)
    path = folder / "image"
    path.write_bytes(b"old image")
    real_read = os.read
    swapped = False

    def read(fd, size):
        nonlocal swapped
        if not swapped:
            swapped = True
            if replacement == "file":
                path.rename(folder / "old")
                path.write_bytes(b"new image")
            else:
                folder.rename(tmp_path / "old")
                if replacement == "directory":
                    folder.mkdir()
                    path.write_bytes(b"new image")
                else:
                    folder.symlink_to(tmp_path / "old", target_is_directory=True)
        return real_read(fd, size)

    monkeypatch.setattr("quire.store.cache.os.read", read)
    with pytest.raises(LedgerError):
        fingerprint(tmp_path, TASK, f"cache/{TASK}/image", sync=True)


@pytest.mark.parametrize("name", ["Page.jpg", "PAGE.JPG", "caf\u00e9.jpg"])
def test_cache_names_have_one_ascii_lowercase_spelling(tmp_path, name):
    with pytest.raises(LedgerError):
        fingerprint(tmp_path, TASK, f"cache/{TASK}/{name}")
