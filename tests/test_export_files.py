from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from quire.errors import LedgerError
from quire.store import export_files as files

KEY = "a" * 64
FORMATS = ("pdf", "cbz", "zip")


def record_path(root):
    return root / "exports" / f"{KEY}.json"


def populated_workspace(parent):
    path, identity = files.make_workspace(parent, KEY)
    for index in (1, 2, 3):
        folder = path / f"pass-{index}"
        folder.mkdir()
        for fmt in FORMATS:
            (folder / f"book.{fmt}").write_bytes(b"candidate")
    for name in ("book.report.json", "publish-report.tmp", *(f"publish-{f}.tmp" for f in FORMATS)):
        (path / name).write_bytes(b"pending")
    return path, identity


@pytest.mark.parametrize("content", [b"", b"abc", b"abc" * 800_000])
def test_fingerprint_bounded_and_empty(tmp_path, monkeypatch, content):
    path = tmp_path / "book.pdf"
    path.write_bytes(content)
    real_read = os.read
    sizes = []

    def read(fd, size):
        sizes.append(size)
        return real_read(fd, size)

    monkeypatch.setattr(os, "read", read)
    stamp = files.fingerprint(path)
    assert stamp == files.Stamp(len(content), hashlib.sha256(content).hexdigest())
    assert all(size <= 1024 * 1024 for size in sizes)
    assert path.read_bytes() == content
    with pytest.raises(FrozenInstanceError):
        stamp.size = 1


def test_missing_files_and_workspace(tmp_path):
    assert files.fingerprint(tmp_path / "missing") is None
    assert files.fingerprint(tmp_path / "missing" / "book.pdf") is None
    assert files.load_record(tmp_path, KEY) is None
    (tmp_path / "exports").mkdir()
    assert files.load_record(tmp_path, KEY) is None
    files.clean_workspace(tmp_path / "missing", (0, 0), FORMATS)
    files.clean_workspace(tmp_path / "missing" / "workspace", (0, 0), FORMATS)
    with pytest.raises(LedgerError):
        files.check_workspace(tmp_path / "missing", (0, 0))


@pytest.mark.parametrize("kind", ["symlink", "dangling", "hardlink", "fifo", "directory"])
@pytest.mark.parametrize("operation", ["fingerprint", "load", "save"])
def test_unsafe_entries_preserve_original(tmp_path, kind, operation):
    original = tmp_path / "original"
    original.write_bytes(b'{"original":true}')
    (tmp_path / "exports").mkdir()
    path = record_path(tmp_path)
    if kind in {"symlink", "dangling"}:
        path.symlink_to(original if kind == "symlink" else tmp_path / "missing")
    elif kind == "hardlink":
        os.link(original, path)
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.mkdir()
    before = path.lstat()
    with pytest.raises(LedgerError):
        if operation == "fingerprint":
            files.fingerprint(path)
        elif operation == "load":
            files.load_record(tmp_path, KEY)
        else:
            files.save_record(tmp_path, KEY, {"new": True})
    assert original.read_bytes() == b'{"original":true}'
    assert path.lstat().st_ino == before.st_ino


@pytest.mark.parametrize("kind", ["exports", "ancestor"])
def test_directory_links_are_rejected(tmp_path, kind):
    original = tmp_path / "original"
    original.mkdir()
    if kind == "exports":
        root = tmp_path
        (root / "exports").symlink_to(original, target_is_directory=True)
    else:
        root = tmp_path / "link"
        root.symlink_to(original, target_is_directory=True)
    with pytest.raises(LedgerError):
        files.save_record(root, KEY, {})
    with pytest.raises(LedgerError):
        files.load_record(root, KEY)
    with pytest.raises(LedgerError):
        files.fingerprint(root / "exports" / "book")
    assert list(original.iterdir()) == []


@pytest.mark.parametrize("change", ["truncate", "append", "file", "parent", "ancestor", "unlink"])
def test_changes_during_read_are_errors(tmp_path, monkeypatch, change):
    folder = tmp_path / "ancestor" / "parent"
    folder.mkdir(parents=True)
    path = folder / "book"
    path.write_bytes(b"original")
    real_read = os.read
    changed = False

    def read(fd, size):
        nonlocal changed
        if not changed:
            changed = True
            if change == "truncate":
                path.write_bytes(b"")
            elif change == "append":
                with path.open("ab") as handle:
                    handle.write(b"extra")
            elif change == "file":
                path.rename(folder / "old")
                path.write_bytes(b"new")
            elif change == "unlink":
                path.unlink()
            else:
                target = folder if change == "parent" else folder.parent
                target.rename(tmp_path / "old")
                target.mkdir()
        return real_read(fd, size)

    monkeypatch.setattr(os, "read", read)
    with pytest.raises(LedgerError):
        files.fingerprint(path)


@pytest.mark.parametrize("target", ["file", "directory"])
def test_replacement_between_stat_and_open(tmp_path, monkeypatch, target):
    folder = tmp_path / "folder"
    folder.mkdir()
    path = folder / "book"
    path.write_bytes(b"original")
    real_open = os.open
    changed = False

    def open_file(name, flags, *args, **kwargs):
        nonlocal changed
        if name == ("book" if target == "file" else "folder") and not changed:
            changed = True
            if target == "file":
                path.rename(folder / "old")
                path.write_bytes(b"replacement")
            else:
                folder.rename(tmp_path / "old")
                folder.mkdir()
                (folder / "book").write_bytes(b"replacement")
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", open_file)
    with pytest.raises(LedgerError):
        files.fingerprint(path)
    assert path.read_bytes() == b"replacement"


@pytest.mark.parametrize(
    "key", ["", "A" * 64, "a" * 63, "g" * 64, "../" + "a" * 61, "a" * 64 + "\n"]
)
def test_invalid_key_is_rejected_before_writes(tmp_path, key):
    for operation in (
        lambda: files.save_record(tmp_path, key, {}),
        lambda: files.load_record(tmp_path, key),
        lambda: files.make_workspace(tmp_path, key),
    ):
        with pytest.raises(LedgerError):
            operation()
    assert list(tmp_path.iterdir()) == []


def test_record_roundtrip_modes_and_sync(tmp_path, monkeypatch):
    synced = []
    real_sync = os.fsync

    def sync(fd):
        synced.append(stat.S_IFMT(os.fstat(fd).st_mode))
        real_sync(fd)

    monkeypatch.setattr(os, "fsync", sync)
    payload = {"state": "building", "nested": [1, True, None, {"title": "\u4e66"}]}
    files.save_record(tmp_path, KEY, payload)
    path = record_path(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert files.load_record(tmp_path, KEY) == payload
    assert stat.S_IFREG in synced and stat.S_IFDIR in synced
    original_inode = path.stat().st_ino
    files.save_record(tmp_path, KEY, {"state": "complete"})
    assert files.load_record(tmp_path, KEY) == {"state": "complete"}
    assert path.stat().st_ino != original_inode
    assert [entry.name for entry in path.parent.iterdir()] == [path.name]


@pytest.mark.parametrize(
    "data", [b"", b"{", b"[]", b"null", b"true", b"\xff", b'{"x":NaN}', b"{" * 2000]
)
def test_corrupt_json_is_preserved(tmp_path, data):
    (tmp_path / "exports").mkdir()
    path = record_path(tmp_path)
    path.write_bytes(data)
    for operation in (
        lambda: files.load_record(tmp_path, KEY),
        lambda: files.save_record(tmp_path, KEY, {}),
    ):
        with pytest.raises(LedgerError):
            operation()
        assert path.read_bytes() == data


@pytest.mark.parametrize("payload", [{"x": object()}, {"x": float("nan")}, []])
def test_invalid_payload_does_not_touch_existing_record(tmp_path, payload):
    files.save_record(tmp_path, KEY, {"original": True})
    with pytest.raises(LedgerError):
        files.save_record(tmp_path, KEY, payload)
    assert files.load_record(tmp_path, KEY) == {"original": True}


def test_record_size_limit(tmp_path):
    payload = {"x": "x" * (8 * 1024 * 1024 - len(json.dumps({"x": ""})))}
    files.save_record(tmp_path, KEY, payload)
    assert record_path(tmp_path).stat().st_size == 8 * 1024 * 1024
    assert files.load_record(tmp_path, KEY) == payload
    with pytest.raises(LedgerError):
        files.save_record(tmp_path, KEY, {"x": payload["x"] + "x"})
    path = record_path(tmp_path)
    with path.open("ab") as handle:
        handle.write(b" ")
    before = path.read_bytes()
    with pytest.raises(LedgerError):
        files.load_record(tmp_path, KEY)
    with pytest.raises(LedgerError):
        files.save_record(tmp_path, KEY, {})
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "failure", ["write", "zero-write", "file-sync", "replace", "directory-sync"]
)
@pytest.mark.parametrize("existing", [False, True])
def test_save_failure_preserves_old_record(tmp_path, monkeypatch, failure, existing):
    (tmp_path / "exports").mkdir()
    if existing:
        files.save_record(tmp_path, KEY, {"original": True})
    real_write, real_sync, real_replace = os.write, os.fsync, os.replace
    published = failed = False

    def write(fd, data):
        if failure == "write":
            raise PermissionError("denied")
        if failure == "zero-write":
            return 0
        return real_write(fd, data)

    def sync(fd):
        nonlocal failed
        is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if not failed and (
            failure == "file-sync" and not is_directory or failure == "directory-sync" and published
        ):
            failed = True
            raise OSError("sync failed")
        real_sync(fd)

    def replace(src, dst, **kwargs):
        nonlocal published
        if failure == "replace":
            raise PermissionError("denied")
        result = real_replace(src, dst, **kwargs)
        published = True
        return result

    monkeypatch.setattr(os, "write", write)
    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(LedgerError):
        files.save_record(tmp_path, KEY, {"new": True})
    assert files.load_record(tmp_path, KEY) == ({"original": True} if existing else None)
    assert set(p.name for p in (tmp_path / "exports").iterdir()) == (
        {f"{KEY}.json"} if existing else set()
    )


@pytest.mark.parametrize("target", ["record", "exports", "root"])
def test_save_replacement_preserves_unknown_content(tmp_path, monkeypatch, target):
    root = tmp_path / "root"
    root.mkdir()
    files.save_record(root, KEY, {"original": True})
    real_write = os.write
    changed = False

    def write(fd, data):
        nonlocal changed
        if not changed:
            changed = True
            if target == "record":
                path = record_path(root)
                path.rename(root / "old.json")
                path.write_bytes(b"unknown")
            else:
                directory = root / "exports" if target == "exports" else root
                directory.rename(tmp_path / "old")
                directory.mkdir()
                if target == "root":
                    (root / "exports").mkdir()
                record_path(root).write_bytes(b"unknown")
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", write)
    with pytest.raises(LedgerError):
        files.save_record(root, KEY, {"new": True})
    assert record_path(root).read_bytes() == b"unknown"


def test_workspace_private_random_and_idempotent_cleanup(tmp_path):
    first, identity = populated_workspace(tmp_path)
    second, other = files.make_workspace(tmp_path, KEY)
    assert first != second
    assert first.name.startswith(f".quire-export-{KEY[:12]}-")
    assert identity == (first.stat().st_dev, first.stat().st_ino)
    assert stat.S_IMODE(first.stat().st_mode) == 0o700
    files.check_workspace(first, identity)
    original = tmp_path / "book.pdf"
    original.write_bytes(b"published original")
    files.clean_workspace(first, identity, FORMATS)
    files.clean_workspace(first, identity, FORMATS)
    assert not first.exists() and second.exists()
    assert original.read_bytes() == b"published original"
    files.clean_workspace(second, other, ())


@pytest.mark.parametrize("location", ["root", "pass"])
@pytest.mark.parametrize(
    "kind",
    ["unknown-file", "unknown-directory", "symlink", "hardlink", "fifo", "directory", "unselected"],
)
def test_workspace_rejects_all_entries_before_deleting(tmp_path, kind, location):
    path, identity = populated_workspace(tmp_path)
    folder = path if location == "root" else path / "pass-3"
    original = tmp_path / "original"
    original.write_bytes(b"original")
    selected = FORMATS
    if kind.startswith("unknown"):
        entry = folder / "unknown"
        entry.mkdir() if kind == "unknown-directory" else entry.write_bytes(b"unknown")
    elif kind == "unselected":
        selected = ("cbz", "zip")
    else:
        entry = folder / ("book.report.json" if location == "root" else "book.pdf")
        entry.unlink()
        if kind == "symlink":
            entry.symlink_to(original)
        elif kind == "hardlink":
            os.link(original, entry)
        elif kind == "fifo":
            os.mkfifo(entry)
        else:
            entry.mkdir()
    before = {str(p.relative_to(path)): p.lstat().st_ino for p in path.rglob("*")}
    with pytest.raises(LedgerError):
        files.clean_workspace(path, identity, selected)
    assert {str(p.relative_to(path)): p.lstat().st_ino for p in path.rglob("*")} == before
    assert original.read_bytes() == b"original"


@pytest.mark.parametrize("replacement", ["directory", "symlink"])
def test_workspace_identity_replacement(tmp_path, replacement):
    path, identity = populated_workspace(tmp_path)
    old = tmp_path / "old"
    path.rename(old)
    if replacement == "directory":
        path.mkdir()
        (path / "unknown").write_bytes(b"unknown")
    else:
        path.symlink_to(old, target_is_directory=True)
    for operation in (
        lambda: files.check_workspace(path, identity),
        lambda: files.clean_workspace(path, identity, FORMATS),
    ):
        with pytest.raises(LedgerError):
            operation()
    assert (old / "pass-1" / "book.pdf").read_bytes() == b"candidate"


@pytest.mark.parametrize("change", ["pass", "root", "unknown", "file"])
def test_cleanup_concurrent_replacement_before_deletion(tmp_path, monkeypatch, change):
    path, identity = populated_workspace(tmp_path)
    real_listdir = os.listdir
    count = 0

    def listdir(fd):
        nonlocal count
        result = real_listdir(fd)
        count += 1
        if count == 5:
            if change in {"pass", "root"}:
                target = path / "pass-3" if change == "pass" else path
                target.rename(tmp_path / "old")
                target.mkdir()
                (target / "unknown").write_bytes(b"unknown")
            elif change == "unknown":
                (path / "unknown").write_bytes(b"unknown")
            else:
                entry = path / "pass-3" / "book.pdf"
                entry.rename(tmp_path / "old.pdf")
                entry.write_bytes(b"changed")
        return result

    monkeypatch.setattr(os, "listdir", listdir)
    with pytest.raises(LedgerError):
        files.clean_workspace(path, identity, FORMATS)
    original = tmp_path / "old" if change == "root" else path
    assert (original / "pass-1" / "book.pdf").read_bytes() == b"candidate"
    if change == "file":
        assert (path / "pass-3" / "book.pdf").read_bytes() == b"changed"


@pytest.mark.parametrize("operation", ["fingerprint", "load", "save", "make", "check", "clean"])
def test_permission_errors_are_ledger_errors(tmp_path, monkeypatch, operation):
    files.save_record(tmp_path, KEY, {})
    path, identity = populated_workspace(tmp_path)
    original_open = os.open

    def denied(name, flags, *args, **kwargs):
        if name in {tmp_path.name, "exports", f"{KEY}.json", path.name}:
            raise PermissionError("denied")
        return original_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", denied)
    operations = {
        "fingerprint": lambda: files.fingerprint(record_path(tmp_path)),
        "load": lambda: files.load_record(tmp_path, KEY),
        "save": lambda: files.save_record(tmp_path, KEY, {"new": True}),
        "make": lambda: files.make_workspace(tmp_path, KEY),
        "check": lambda: files.check_workspace(path, identity),
        "clean": lambda: files.clean_workspace(path, identity, FORMATS),
    }
    with pytest.raises(LedgerError):
        operations[operation]()


def test_workspace_creation_collision_preserves_existing(tmp_path, monkeypatch):
    monkeypatch.setattr(files.secrets, "token_hex", lambda size: "fixed")
    path, identity = files.make_workspace(tmp_path, KEY)
    (path / "unknown").write_bytes(b"keep")
    with pytest.raises(LedgerError):
        files.make_workspace(tmp_path, KEY)
    files.check_workspace(path, identity)
    assert (path / "unknown").read_bytes() == b"keep"


@pytest.mark.parametrize("formats", [("../pdf",), ("PDF",), ("html",)])
def test_invalid_cleanup_formats_do_not_touch_workspace(tmp_path, formats):
    path, identity = populated_workspace(tmp_path)
    with pytest.raises(LedgerError):
        files.clean_workspace(path, identity, formats)
    assert (path / "pass-1" / "book.pdf").read_bytes() == b"candidate"


def test_partial_cleanup_failure_can_resume(tmp_path, monkeypatch):
    path, identity = populated_workspace(tmp_path)
    real_unlink = os.unlink
    count = 0

    def unlink(name, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise PermissionError("denied")
        real_unlink(name, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", unlink)
        with pytest.raises(LedgerError):
            files.clean_workspace(path, identity, FORMATS)
    files.clean_workspace(path, identity, FORMATS)
    assert not path.exists()


@pytest.mark.parametrize("path", [Path("."), Path("/"), Path("bad\x00name")])
def test_invalid_fingerprint_paths_are_errors(path):
    with pytest.raises(LedgerError):
        files.fingerprint(path)


def test_parent_traversal_rejected(tmp_path):
    folder = tmp_path / "folder"
    folder.mkdir()
    with pytest.raises(LedgerError):
        files.fingerprint(folder / ".." / "book.pdf")


@pytest.mark.parametrize("operation", ["save", "make"])
def test_missing_write_parent_is_error(tmp_path, operation):
    with pytest.raises(LedgerError):
        if operation == "save":
            files.save_record(tmp_path / "missing", KEY, {})
        else:
            files.make_workspace(tmp_path / "missing", KEY)
    assert list(tmp_path.iterdir()) == []


def test_removed_ancestor_during_read_is_error(tmp_path, monkeypatch):
    folder = tmp_path / "folder"
    folder.mkdir()
    path = folder / "book"
    path.write_bytes(b"original")
    real_read = os.read

    def read(fd, size):
        folder.rename(tmp_path / "old")
        return real_read(fd, size)

    monkeypatch.setattr(os, "read", read)
    with pytest.raises(LedgerError):
        files.fingerprint(path)
    assert (tmp_path / "old" / "book").read_bytes() == b"original"


@pytest.mark.parametrize("change", ["file", "pass"])
def test_cleanup_changes_during_initial_scan_are_rejected(tmp_path, monkeypatch, change):
    path, identity = populated_workspace(tmp_path)
    real_listdir = os.listdir
    count = 0

    def listdir(fd):
        nonlocal count
        names = real_listdir(fd)
        count += 1
        if count == 1 and change == "pass":
            folder = path / "pass-1"
            folder.rename(tmp_path / "old")
            folder.mkdir()
        elif count == 2 and change == "file":
            (path / "pass-1" / "book.pdf").unlink()
        return names

    monkeypatch.setattr(os, "listdir", listdir)
    with pytest.raises(LedgerError):
        files.clean_workspace(path, identity, FORMATS)
    assert (path / "pass-2" / "book.pdf").read_bytes() == b"candidate"


def test_unknown_added_after_cleanup_started_is_preserved(tmp_path, monkeypatch):
    path, identity = populated_workspace(tmp_path)
    real_unlink = os.unlink
    deleted = []

    def unlink(name, **kwargs):
        real_unlink(name, **kwargs)
        deleted.append(name)
        (path / "unknown").write_bytes(b"keep")

    with monkeypatch.context() as patch:
        patch.setattr(os, "unlink", unlink)
        with pytest.raises(LedgerError):
            files.clean_workspace(path, identity, FORMATS)
    assert len(deleted) == 1
    assert (path / "unknown").read_bytes() == b"keep"
    assert (path / "pass-1" / "book.pdf").read_bytes() == b"candidate"


def test_cleanup_never_lists_parent(tmp_path, monkeypatch):
    path, identity = populated_workspace(tmp_path)
    real_listdir = os.listdir
    allowed = {
        (folder.stat().st_dev, folder.stat().st_ino)
        for folder in [path, *(path / f"pass-{index}" for index in (1, 2, 3))]
    }

    def listdir(fd):
        assert isinstance(fd, int)
        current = os.fstat(fd)
        assert (current.st_dev, current.st_ino) in allowed
        return real_listdir(fd)

    monkeypatch.setattr(os, "listdir", listdir)
    files.clean_workspace(path, identity, FORMATS)


@pytest.mark.parametrize("operation", ["read", "save", "clean"])
def test_actual_permission_denial_preserves_contents(tmp_path, operation):
    if os.geteuid() == 0:
        pytest.skip("Root bypasses POSIX file permissions")
    files.save_record(tmp_path, KEY, {"original": True})
    path, identity = populated_workspace(tmp_path)
    protected = (
        record_path(tmp_path)
        if operation == "read"
        else tmp_path / "exports"
        if operation == "save"
        else path / "pass-3"
    )
    mode = stat.S_IMODE(protected.stat().st_mode)
    protected.chmod(0 if operation == "read" else 0o500)
    try:
        with pytest.raises(LedgerError):
            if operation == "read":
                files.fingerprint(protected)
            elif operation == "save":
                files.save_record(tmp_path, KEY, {"new": True})
            else:
                files.clean_workspace(path, identity, FORMATS)
    finally:
        protected.chmod(mode)
    assert files.load_record(tmp_path, KEY) == {"original": True}
    assert (path / "pass-1" / "book.pdf").read_bytes() == b"candidate"


def test_unknown_replacement_after_record_publish_is_preserved(tmp_path, monkeypatch):
    files.save_record(tmp_path, KEY, {"original": True})
    real_replace = os.replace

    def replace(src, dst, **kwargs):
        real_replace(src, dst, **kwargs)
        record_path(tmp_path).write_bytes(b"unknown concurrent modification")

    monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(LedgerError):
        files.save_record(tmp_path, KEY, {"new": True})
    assert record_path(tmp_path).read_bytes() == b"unknown concurrent modification"
    backups = list((tmp_path / "exports").glob("*.bak"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_bytes()) == {"original": True}


def test_load_detects_record_replacement_during_read(tmp_path, monkeypatch):
    files.save_record(tmp_path, KEY, {"original": True})
    real_read = os.read

    def read(fd, size):
        record_path(tmp_path).rename(tmp_path / "old.json")
        record_path(tmp_path).write_bytes(b"unknown")
        return real_read(fd, size)

    monkeypatch.setattr(os, "read", read)
    with pytest.raises(LedgerError):
        files.load_record(tmp_path, KEY)
    assert record_path(tmp_path).read_bytes() == b"unknown"


def test_fingerprint_real_concurrent_parent_replacement(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    folder = tmp_path / "folder"
    folder.mkdir()
    path = folder / "book.pdf"
    path.write_bytes(b"original")
    reading, replaced = Event(), Event()
    real_read = os.read

    def read(fd, size):
        reading.set()
        assert replaced.wait(5)
        return real_read(fd, size)

    def replace():
        assert reading.wait(5)
        try:
            folder.rename(tmp_path / "old")
            folder.mkdir()
            path.write_bytes(b"replacement")
        finally:
            replaced.set()

    monkeypatch.setattr(os, "read", read)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(replace)
        with pytest.raises(LedgerError):
            files.fingerprint(path)
        future.result()
    assert path.read_bytes() == b"replacement"
    assert (tmp_path / "old" / "book.pdf").read_bytes() == b"original"
