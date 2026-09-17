"""Private export files; receipt schemas and publication belong to callers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from ..errors import LedgerError
from .cache import _copy_backup, _discard, _entry, _identity, _inode, _unchanged, _write_all

_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_LIMIT = 8 * 1024 * 1024
_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class Stamp:
    size: int
    sha256: str


def _key(key: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        raise LedgerError("Export key must be 64 lowercase hexadecimal characters")


def _check_edges(edges: list[tuple[int, str, int]]) -> None:
    for parent, name, descriptor in edges:
        value = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISDIR(value.st_mode) or _inode(value) != _inode(os.fstat(descriptor)):
            raise LedgerError("Export directory identity changed")


def _open_directory(parent: int, name: str, stack: ExitStack) -> int:
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode):
        raise LedgerError("Export directory must not be a link or non-directory")
    try:
        descriptor = os.open(name, _DIRECTORY, dir_fd=parent)
        stack.callback(os.close, descriptor)
        if _inode(before) != _inode(os.fstat(descriptor)):
            raise LedgerError("Export directory identity changed")
        return descriptor
    except OSError as exc:
        raise LedgerError("Export directory changed or is inaccessible") from exc


@contextmanager
def _directory(path: Path) -> Iterator[tuple[int, Callable[[], None]]]:
    # Keep every ancestor pinned so a renamed ancestor cannot redirect an operation.
    with ExitStack() as stack:
        absolute = path.absolute()
        edges: list[tuple[int, str, int]] = []

        def check() -> None:
            try:
                _check_edges(edges)
            except OSError as exc:
                raise LedgerError("Export directory identity changed") from exc

        try:
            descriptor = os.open(absolute.anchor, _DIRECTORY)
            stack.callback(os.close, descriptor)
            for name in absolute.parts[1:]:
                if name == "..":
                    raise LedgerError("Export directory must not contain parent traversal")
                check()
                try:
                    child = _open_directory(descriptor, name, stack)
                except FileNotFoundError:
                    check()
                    raise
                edges.append((descriptor, name, child))
                descriptor = child
            check()
        except FileNotFoundError:
            raise
        except (OSError, ValueError) as exc:
            raise LedgerError("Export directory is unsafe or inaccessible") from exc
        try:
            yield descriptor, check
            check()
        except (OSError, ValueError) as exc:
            raise LedgerError("Export file operation failed") from exc


def _read_file(
    directory: int, name: str, check: Callable[[], None], *, collect: bool = False
) -> tuple[Stamp, bytes, os.stat_result] | None:
    before = _entry(directory, name)
    if before is None:
        check()
        _unchanged(directory, name, None)
        return None
    if collect and before.st_size > _LIMIT:
        raise LedgerError("Export record exceeds 8 MiB")
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        if _identity(before) != _identity(os.fstat(descriptor)):
            raise LedgerError("Export file changed before reading")
        digest = hashlib.sha256()
        blocks = []
        remaining = before.st_size
        while remaining:
            block = os.read(descriptor, min(_CHUNK, remaining))
            if not block:
                raise LedgerError("Export file changed while reading")
            digest.update(block)
            if collect:
                blocks.append(block)
            remaining -= len(block)
        if _identity(before) != _identity(os.fstat(descriptor)):
            raise LedgerError("Export file changed while reading")
        check()
        _unchanged(directory, name, before)
        return Stamp(before.st_size, digest.hexdigest()), b"".join(blocks), before
    finally:
        os.close(descriptor)


def fingerprint(path: Path) -> Stamp | None:
    """Hash a stable, single-linked regular file, including an empty one."""
    if not path.name:
        raise LedgerError("Export file must be a regular file")
    try:
        with _directory(path.parent) as (directory, check):
            result = _read_file(directory, path.name, check)
            return None if result is None else result[0]
    except FileNotFoundError:
        return None


def _reject_constant(value: str) -> object:
    raise ValueError("Non-finite JSON number")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _decode(data: bytes) -> dict[str, object]:
    try:
        payload: object = json.loads(
            data.decode("utf-8"), parse_constant=_reject_constant, object_pairs_hook=_unique_object
        )
        if not isinstance(payload, dict):
            raise ValueError("Expected an object")
        return cast(dict[str, object], payload)
    except (ValueError, RecursionError) as exc:
        raise LedgerError("Export record is not a valid JSON object") from exc


def load_record(root: Path, key: str) -> dict[str, object] | None:
    _key(key)
    try:
        with _directory(root / "exports") as (directory, check):
            result = _read_file(directory, f"{key}.json", check, collect=True)
            return None if result is None else _decode(result[1])
    except FileNotFoundError:
        return None


def save_record(root: Path, key: str, payload: dict[str, object]) -> None:
    """Atomically save a bounded JSON object while the caller holds the Ledger lock."""
    _key(key)
    try:
        if not isinstance(payload, dict):
            raise ValueError("Expected an object")
        data = json.dumps(payload, ensure_ascii=True, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise LedgerError("Export record must contain JSON values") from exc
    if len(data) > _LIMIT:
        raise LedgerError("Export record exceeds 8 MiB")
    try:
        with _directory(root) as (parent, check_root), ExitStack() as stack:
            try:
                os.mkdir("exports", 0o700, dir_fd=parent)
                os.fsync(parent)
            except FileExistsError:
                pass
            directory = _open_directory(parent, "exports", stack)

            def check() -> None:
                check_root()
                _check_edges([(parent, "exports", directory)])

            check()
            result = _read_file(directory, f"{key}.json", check, collect=True)
            before = None if result is None else result[2]
            if result is not None:
                _decode(result[1])
            _save(directory, f"{key}.json", data, before, check)
    except OSError as exc:
        raise LedgerError("Export record could not be saved") from exc


def _save(
    directory: int,
    name: str,
    data: bytes,
    before: os.stat_result | None,
    check: Callable[[], None],
) -> None:
    temporary, backup = (f".quire-{secrets.token_hex(16)}.{suffix}" for suffix in ("tmp", "bak"))
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory,
    )
    owned = os.fstat(descriptor)
    expected = Stamp(len(data), hashlib.sha256(data).hexdigest())
    saved = None
    published = preserve_backup = False
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, data)
        os.fsync(descriptor)
        check()
        _unchanged(directory, name, before)
        if before is not None:
            saved = _copy_backup(directory, name, backup, before)
        check()
        _unchanged(directory, name, before)
        _unchanged(directory, temporary, os.fstat(descriptor))
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        published = True
        check()
        os.fsync(directory)
        check()
        _published(directory, name, owned, expected)
    except BaseException:
        if published:
            preserve_backup = saved is not None
            _published(directory, name, owned, expected)
            if saved is None:
                os.unlink(name, dir_fd=directory)
            else:
                _unchanged(directory, backup, saved)
                os.replace(backup, name, src_dir_fd=directory, dst_dir_fd=directory)
                preserve_backup = False
            os.fsync(directory)
        raise
    finally:
        os.close(descriptor)
        _discard(directory, temporary, owned)
        if saved is not None and not preserve_backup:
            _discard(directory, backup, saved)
        os.fsync(directory)


def _published(directory: int, name: str, owned: os.stat_result, expected: Stamp) -> None:
    result = _read_file(directory, name, lambda: None, collect=True)
    if result is None or result[0] != expected or _inode(result[2]) != _inode(owned):
        raise LedgerError("Published export record changed")


def make_workspace(parent: Path, key: str) -> tuple[Path, tuple[int, int]]:
    _key(key)
    try:
        with _directory(parent) as (directory, check), ExitStack() as stack:
            for _ in range(10):
                name = f".quire-export-{key[:12]}-{secrets.token_hex(16)}"
                try:
                    os.mkdir(name, 0o700, dir_fd=directory)
                    break
                except FileExistsError:
                    continue
            else:
                raise LedgerError("Could not allocate a unique export workspace")
            descriptor = _open_directory(directory, name, stack)
            check()
            _check_edges([(directory, name, descriptor)])
            os.fchmod(descriptor, 0o700)
            os.fsync(descriptor)
            os.fsync(directory)
            check()
            _check_edges([(directory, name, descriptor)])
            return parent / name, _inode(os.fstat(descriptor))
    except OSError as exc:
        raise LedgerError("Export workspace could not be created") from exc


def check_workspace(path: Path, identity: tuple[int, int]) -> None:
    try:
        with _directory(path) as (directory, _):
            if _inode(os.fstat(directory)) != identity:
                raise LedgerError("Export workspace identity changed")
    except OSError as exc:
        raise LedgerError("Export workspace is missing or unsafe") from exc


def clean_workspace(path: Path, identity: tuple[int, int], formats: tuple[str, ...]) -> None:
    """Validate the entire registered layout before deleting any known entry."""
    if path.name in {"", ".."} or any(
        fmt not in {"pdf", "cbz", "zip", "epub", "txt"} for fmt in formats
    ):
        raise LedgerError("Invalid export workspace or formats")
    try:
        with _directory(path.parent) as (parent, check), ExitStack() as stack:
            try:
                directory = _open_directory(parent, path.name, stack)
            except FileNotFoundError:
                check()
                return
            if _inode(os.fstat(directory)) != identity:
                raise LedgerError("Export workspace identity changed")
            edges = [(parent, path.name, directory)]
            files: list[tuple[int, str, os.stat_result]] = []
            listings: dict[int, set[str]] = {}
            passes = {f"pass-{index}" for index in (1, 2, 3)}
            root_files = (
                {"book.report.json", "publish-report.tmp", "book.review.txt", "publish-review.tmp"}
                | {f"publish-{fmt}.tmp" for fmt in formats}
                | {f"book.{fmt}" for fmt in formats}
            )

            def scan(fd: int, allowed: set[str], directories: set[str]) -> None:
                before = os.fstat(fd)
                names = listings[fd] = set(os.listdir(fd))
                if names - allowed - directories:
                    raise LedgerError("Unknown export workspace entry")
                for name in sorted(names):
                    if name in directories:
                        child = _open_directory(fd, name, stack)
                        edges.append((fd, name, child))
                        scan(child, {f"book.{fmt}" for fmt in formats}, set())
                    else:
                        value = _entry(fd, name)
                        if value is None:
                            raise LedgerError("Export workspace entry disappeared")
                        files.append((fd, name, value))
                if _identity(os.fstat(fd)) != _identity(before):
                    raise LedgerError("Export workspace directory changed while scanning")

            def validate() -> None:
                check()
                _check_edges(edges)
                for fd, names in listings.items():
                    if set(os.listdir(fd)) != names:
                        raise LedgerError("Export workspace entries changed")
                for fd, name, value in files:
                    _unchanged(fd, name, value)
                check()
                _check_edges(edges)

            scan(directory, root_files, passes)
            validate()
            while files:
                validate()
                fd, name, _ = files[-1]
                os.unlink(name, dir_fd=fd)
                files.pop()
                listings[fd].remove(name)
                os.fsync(fd)
            while len(edges) > 1:
                validate()
                fd, name, child = edges[-1]
                os.rmdir(name, dir_fd=fd)
                edges.pop()
                del listings[child]
                listings[fd].remove(name)
                os.fsync(fd)
            validate()
            os.rmdir(path.name, dir_fd=parent)
            os.fsync(parent)
            check()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise LedgerError("Export workspace could not be cleaned safely") from exc
