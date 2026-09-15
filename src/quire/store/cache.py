"""Bounded checks of task-owned cache files without following symlinks."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..errors import LedgerError

_BACKUP_LIMIT = 128 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class FileStamp:
    path: str
    sha256: str
    size: int


@contextmanager
def _open_entry(
    root: Path, path: PurePosixPath
) -> Iterator[tuple[int, int, tuple[tuple[int, int], ...]]]:
    directory = descriptor = -1
    ancestors = []
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        root_stat = os.fstat(directory)
        ancestors.append((root_stat.st_dev, root_stat.st_ino))
        for part in path.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
            current = os.fstat(directory)
            ancestors.append((current.st_dev, current.st_ino))
        descriptor = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
        yield descriptor, directory, tuple(ancestors)
    finally:
        if descriptor != -1:
            os.close(descriptor)
        if directory != -1:
            os.close(directory)


def _identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _cache_path(task_id: str, relative: str, *, single: bool = False) -> PurePosixPath:
    path = PurePosixPath(relative)
    if (
        not re.fullmatch(r"[0-9a-f]{64}", task_id)
        or path.is_absolute()
        or ".." in path.parts
        or not re.fullmatch(r"[a-z0-9._/-]+", relative)
        or len(path.parts) < 3
        or path.parts[:2] != ("cache", task_id)
        or str(path) != relative
        or (single and len(path.parts) != 3)
    ):
        raise LedgerError("Cache path must be relative to its task cache directory")
    return path


def _verified(
    root: Path,
    task_id: str,
    relative: str,
    *,
    sync: bool = False,
    expected_size: int | None = None,
    collect: bool = False,
) -> tuple[FileStamp, bytes]:
    path = _cache_path(task_id, relative)
    try:
        with _open_entry(root, path) as (descriptor, directory, ancestors):
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size < 1 or before.st_nlink != 1:
                raise LedgerError("Cache entry must be a nonempty private regular file")
            if expected_size is not None and before.st_size != expected_size:
                raise LedgerError("Cache file size does not match the ledger")
            digest = hashlib.sha256()
            blocks = []
            remaining = before.st_size
            while remaining:
                block = os.read(descriptor, min(1024 * 1024, remaining))
                if not block:
                    raise LedgerError("Cache file changed while being verified")
                digest.update(block)
                if collect:
                    blocks.append(block)
                remaining -= len(block)
            if _identity(before) != _identity(os.fstat(descriptor)):
                raise LedgerError("Cache file changed while being verified")
            if sync:
                os.fsync(descriptor)
                os.fsync(directory)
            with _open_entry(root, path) as (current, _, current_ancestors):
                if current_ancestors != ancestors or _identity(os.fstat(current)) != _identity(
                    before
                ):
                    raise LedgerError("Cache path changed while being verified")
            return FileStamp(relative, digest.hexdigest(), before.st_size), b"".join(blocks)
    except OSError as exc:
        raise LedgerError("Cache file is missing, unsafe or unreadable") from exc


def fingerprint(
    root: Path, task_id: str, relative: str, *, sync: bool = False, expected_size: int | None = None
) -> FileStamp:
    stamp, _ = _verified(root, task_id, relative, sync=sync, expected_size=expected_size)
    return stamp


def read_cached(root: Path, task_id: str, relative: str, *, sha256: str, size: int) -> bytes:
    """Return bounded, verified bytes so consumers never reopen an unchecked path."""
    if size < 1 or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise LedgerError("Cache size and SHA-256 must describe a nonempty file")
    stamp, data = _verified(root, task_id, relative, expected_size=size, collect=True)
    if stamp.sha256 != sha256:
        raise LedgerError("Cache file SHA-256 does not match the ledger")
    return data


def _inode(value: os.stat_result) -> tuple[int, int]:
    return value.st_dev, value.st_ino


def _check_directories(root: Path, task_id: str, directories: list[int]) -> None:
    current = root.lstat()
    for index, descriptor in enumerate(directories):
        if index:
            current = os.stat(
                ("cache", task_id)[index - 1],
                dir_fd=directories[index - 1],
                follow_symlinks=False,
            )
        if not stat.S_ISDIR(current.st_mode) or _inode(current) != _inode(os.fstat(descriptor)):
            raise LedgerError("Cache directory changed while being accessed")


@contextmanager
def _task_directory(root: Path, task_id: str, *, create: bool) -> Iterator[list[int]]:
    with ExitStack() as stack:
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, descriptor)
        directories = [descriptor]
        for part in ("cache", task_id):
            _check_directories(root, task_id, directories)
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=directories[-1])
                except FileExistsError:
                    pass
            descriptor = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directories[-1]
            )
            stack.callback(os.close, descriptor)
            directories.append(descriptor)
            _check_directories(root, task_id, directories)
            if create:
                os.fchmod(descriptor, 0o700)
        yield directories


def _entry(directory: int, name: str) -> os.stat_result | None:
    try:
        value = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        raise LedgerError("Cache entry must be a private regular file")
    return value


def _unchanged(directory: int, name: str, before: os.stat_result | None) -> None:
    current = _entry(directory, name)
    if (None if current is None else _identity(current)) != (
        None if before is None else _identity(before)
    ):
        raise LedgerError("Cache file changed while being accessed")


def _discard(directory: int, name: str, owned: os.stat_result) -> None:
    try:
        current = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return
    if _inode(current) == _inode(owned) and stat.S_ISREG(current.st_mode):
        os.unlink(name, dir_fd=directory)


def _write_all(descriptor: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise LedgerError("Cache write made no progress")
        remaining = remaining[written:]


def _copy_backup(
    directory: int, filename: str, backup: str, before: os.stat_result
) -> os.stat_result:
    # Bound corrupt cache copies by the core network layer's maximum response size.
    if before.st_size > _BACKUP_LIMIT:
        raise LedgerError("Cache file exceeds the backup size limit")
    with ExitStack() as stack:
        source = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        stack.callback(os.close, source)
        if _identity(os.fstat(source)) != _identity(before):
            raise LedgerError("Cache file changed before being copied")
        target = os.open(
            backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
        )
        stack.callback(os.close, target)
        owned = os.fstat(target)
        try:
            os.fchmod(target, 0o600)
            remaining = before.st_size
            while remaining:
                block = os.read(source, min(1024 * 1024, remaining))
                if not block:
                    raise LedgerError("Cache file changed while being copied")
                _write_all(target, block)
                remaining -= len(block)
            if _identity(os.fstat(source)) != _identity(before):
                raise LedgerError("Cache file changed while being copied")
            _unchanged(directory, filename, before)
            os.fsync(target)
            saved = os.fstat(target)
            _unchanged(directory, backup, saved)
            return saved
        except BaseException:
            _discard(directory, backup, owned)
            raise


def publish_bytes(root: Path, task_id: str, filename: str, data: bytes) -> str:
    """Durably publish one task file; the caller holds the Ledger writer lock."""
    path = _cache_path(task_id, f"cache/{task_id}/{filename}", single=True)
    if not data:
        raise LedgerError("Cache data must not be empty")
    try:
        with _task_directory(root, task_id, create=True) as directories:
            directory = directories[-1]
            before = _entry(directory, filename)
            temporary = f".quire-{secrets.token_hex(16)}.tmp"
            backup = f".quire-{secrets.token_hex(16)}.bak"
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            owned = os.fstat(descriptor)
            saved = None
            published = False
            preserve_backup = False
            try:
                os.fchmod(descriptor, 0o600)
                _write_all(descriptor, data)
                os.fsync(descriptor)
                _check_directories(root, task_id, directories)
                _unchanged(directory, filename, before)
                # An independent copy keeps the original single-linked even after a crash.
                if before is not None:
                    saved = _copy_backup(directory, filename, backup, before)
                _check_directories(root, task_id, directories)
                _unchanged(directory, filename, before)
                _unchanged(directory, temporary, os.fstat(descriptor))
                os.replace(temporary, filename, src_dir_fd=directory, dst_dir_fd=directory)
                published = True
                _check_directories(root, task_id, directories)
                _unchanged(directory, filename, os.fstat(descriptor))
                for parent in reversed(directories):
                    os.fsync(parent)
                _check_directories(root, task_id, directories)
                if saved is not None:
                    _discard(directory, backup, saved)
            except BaseException:
                if published:
                    # A failed rollback must leave the old contents available for recovery.
                    preserve_backup = saved is not None
                    _unchanged(directory, filename, os.fstat(descriptor))
                    if saved is None:
                        os.unlink(filename, dir_fd=directory)
                    else:
                        _unchanged(directory, backup, saved)
                        os.replace(backup, filename, src_dir_fd=directory, dst_dir_fd=directory)
                        preserve_backup = False
                    os.fsync(directory)
                raise
            finally:
                os.close(descriptor)
                _discard(directory, temporary, owned)
                if saved is not None and not preserve_backup:
                    _discard(directory, backup, saved)
            return str(path)
    except OSError as exc:
        raise LedgerError("Cache file could not be published safely") from exc


def remove_cached(
    root: Path, task_id: str, relative: str, *, expected: FileStamp | None = None
) -> None:
    """Remove only a named regular task file, leaving every other entry alone."""
    path = _cache_path(task_id, relative, single=True)
    try:
        with _task_directory(root, task_id, create=False) as directories:
            directory = directories[-1]
            before = _entry(directory, path.name)
            if before is None:
                return
            if expected is not None:
                actual = fingerprint(root, task_id, relative, expected_size=expected.size)
                if actual != expected:
                    raise LedgerError("Cache file changed; preserved instead of deleting")
            _check_directories(root, task_id, directories)
            _unchanged(directory, path.name, before)
            os.unlink(path.name, dir_fd=directory)
            os.fsync(directory)
            _check_directories(root, task_id, directories)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise LedgerError("Cache file could not be removed safely") from exc
