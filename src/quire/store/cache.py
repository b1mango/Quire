"""Bounded checks of task-owned cache files without following symlinks."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..errors import LedgerError


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


def fingerprint(
    root: Path, task_id: str, relative: str, *, sync: bool = False, expected_size: int | None = None
) -> FileStamp:
    path = PurePosixPath(relative)
    if (
        not re.fullmatch(r"[0-9a-f]{64}", task_id)
        or path.is_absolute()
        or ".." in path.parts
        or not re.fullmatch(r"[a-z0-9._/-]+", relative)
        or len(path.parts) < 3
        or path.parts[:2] != ("cache", task_id)
        or str(path) != relative
    ):
        raise LedgerError("Cache path must be relative to its task cache directory")
    try:
        with _open_entry(root, path) as (descriptor, directory, ancestors):
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_size < 1 or before.st_nlink != 1:
                raise LedgerError("Cache entry must be a nonempty private regular file")
            if expected_size is not None and before.st_size != expected_size:
                raise LedgerError("Cache file size does not match the ledger")
            digest = hashlib.sha256()
            remaining = before.st_size
            while remaining:
                block = os.read(descriptor, min(1024 * 1024, remaining))
                if not block:
                    raise LedgerError("Cache file changed while being verified")
                digest.update(block)
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
            return FileStamp(relative, digest.hexdigest(), before.st_size)
    except OSError as exc:
        raise LedgerError("Cache file is missing, unsafe or unreadable") from exc
