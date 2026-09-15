"""Fixed SQLite schema and process ownership for the local ledger."""

from __future__ import annotations

import fcntl
import os
import shutil
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from ..errors import LedgerError

_APPLICATION_ID = 0x51554952
_VERSION = 1
_TASKS_SQL = """CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    source_url TEXT NOT NULL,
    options_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'done', 'partial', 'failed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)"""
_RESOURCES_SQL = """CREATE TABLE resources (
    task_id TEXT NOT NULL REFERENCES tasks(id),
    chapter INTEGER NOT NULL CHECK (typeof(chapter) = 'integer' AND chapter >= 1),
    page INTEGER NOT NULL CHECK (typeof(page) = 'integer' AND page >= 1),
    url TEXT NOT NULL,
    referer TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'downloading', 'done', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (typeof(attempts) = 'integer' AND attempts >= 0),
    local_path TEXT,
    sha256 TEXT,
    size INTEGER,
    error_code TEXT,
    PRIMARY KEY (task_id, chapter, page),
    CHECK (
        (status = 'done' AND local_path IS NOT NULL AND sha256 IS NOT NULL
            AND size IS NOT NULL AND typeof(size) = 'integer' AND size > 0)
        OR (status != 'done' AND local_path IS NULL AND sha256 IS NULL AND size IS NULL)
    )
)"""
_SCHEMA = {
    ("table", "tasks", "tasks", _TASKS_SQL),
    ("table", "resources", "resources", _RESOURCES_SQL),
    ("index", "sqlite_autoindex_tasks_1", "tasks", None),
    ("index", "sqlite_autoindex_resources_1", "resources", None),
}


def _check_file(path: Path, fd: int) -> None:
    opened = os.fstat(fd)
    current = path.lstat()
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or not stat.S_ISREG(current.st_mode)
        or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
    ):
        raise LedgerError("Ledger files must be ordinary private files")


def _open_file(path: Path, *, create: bool = False) -> int:
    flags = os.O_NOFOLLOW | os.O_NONBLOCK
    flags |= os.O_RDWR | os.O_CREAT if create else os.O_RDONLY
    fd = os.open(path, flags, 0o600)
    try:
        _check_file(path, fd)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    # Immutable reads never replay journals or create WAL sidecars on the original.
    mode = "ro&immutable=1" if readonly else "rw"
    connection = sqlite3.connect(
        f"{path.as_uri()}?mode={mode}", uri=True, isolation_level=None, timeout=0
    )
    connection.row_factory = sqlite3.Row
    return connection


def _configure(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    if connection.execute("PRAGMA journal_mode = DELETE").fetchone()[0] != "delete":
        raise LedgerError("Unsupported ledger journal mode")
    connection.execute("PRAGMA synchronous = FULL")


def _validate(connection: sqlite3.Connection) -> None:
    if (
        connection.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION_ID
        or connection.execute("PRAGMA user_version").fetchone()[0] != _VERSION
    ):
        raise LedgerError("Unsupported ledger database version")
    schema = connection.execute("SELECT type, name, tbl_name, sql FROM sqlite_schema")
    if {tuple(row) for row in schema} != _SCHEMA:
        raise LedgerError("Unsupported ledger database structure")
    if [tuple(row) for row in connection.execute("PRAGMA quick_check")] != [("ok",)]:
        raise LedgerError("Damaged ledger database")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise LedgerError("Damaged ledger references")


def _copy_file(fd: int, target: Path) -> None:
    output_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(output_fd, "wb") as output, os.fdopen(fd, "rb", closefd=False) as source:
        os.fchmod(output.fileno(), 0o600)
        source.seek(0)
        shutil.copyfileobj(source, output)


def _reject_wal(path: Path) -> None:
    if any(os.path.lexists(path.with_name(path.name + suffix)) for suffix in ("-wal", "-shm")):
        raise LedgerError("Unsupported ledger journal mode")


def _validate_existing(path: Path, fd: int) -> None:
    _reject_wal(path)
    journal = path.with_name(path.name + "-journal")
    try:
        journal_fd = _open_file(journal)
    except FileNotFoundError:
        connection = _connect(path, readonly=True)
        try:
            _validate(connection)
        finally:
            connection.close()
    else:
        try:
            # A hot journal may restore schema pages too. Recover only a private copy
            # until the committed schema and data have passed all validation.
            with TemporaryDirectory(prefix="quire-ledger-check-") as temporary:
                copied = Path(temporary) / "ledger.db"
                _copy_file(fd, copied)
                _copy_file(journal_fd, copied.with_name("ledger.db-journal"))
                connection = _connect(copied)
                try:
                    _validate(connection)
                finally:
                    connection.close()
        finally:
            os.close(journal_fd)
    _check_file(path, fd)


def _unlink_owned(path: Path, fd: int) -> None:
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    opened = os.fstat(fd)
    if (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino):
        path.unlink()


def _initialize(path: Path) -> int:
    fd: int | None = None
    published = False
    try:
        with TemporaryDirectory(prefix=".ledger-new-", dir=path.parent) as temporary:
            staged = Path(temporary) / "ledger.db"
            fd = _open_file(staged, create=True)
            os.fchmod(fd, 0o600)
            connection = _connect(staged)
            try:
                _configure(connection)
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(_TASKS_SQL)
                connection.execute(_RESOURCES_SQL)
                connection.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version = {_VERSION}")
                connection.commit()
                _validate(connection)
            finally:
                connection.close()
            os.fsync(fd)
            # Linking publishes a complete database without overwriting a raced file.
            os.link(staged, path)
            published = True
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return fd
    except BaseException:
        if fd is not None:
            try:
                if published:
                    _unlink_owned(path, fd)
            finally:
                os.close(fd)
        raise


def _prepare(root: Path) -> sqlite3.Connection:
    path = root / "ledger.db"
    created = False
    try:
        fd = _open_file(path)
    except FileNotFoundError:
        if any(root.glob("ledger.db-*")):
            raise LedgerError("Ledger database has orphaned journal files") from None
        fd = _initialize(path)
        created = True
    connection: sqlite3.Connection | None = None
    try:
        if not created:
            _validate_existing(path, fd)
        os.fchmod(fd, 0o600)
        connection = _connect(path)
        _check_file(path, fd)
        _configure(connection)
        return connection
    except BaseException:
        try:
            if connection is not None:
                connection.close()
        finally:
            if created:
                _unlink_owned(path, fd)
        raise
    finally:
        os.close(fd)


@contextmanager
def open_database(root: Path) -> Iterator[sqlite3.Connection]:
    """Hold the ledger lock and a thread-bound, explicitly transacted connection."""
    lock_fd: int | None = None
    connection: sqlite3.Connection | None = None
    try:
        try:
            root = root.absolute()
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not stat.S_ISDIR(root.lstat().st_mode):
                raise LedgerError("Ledger root must be a directory")
            lock_fd = _open_file(root / "ledger.lock", create=True)
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LedgerError("Ledger is already in use") from None
            os.fchmod(lock_fd, 0o600)
            connection = _prepare(root)
        except (sqlite3.Error, OSError):
            raise LedgerError("Cannot open ledger database") from None
        except LedgerError as exc:
            raise exc from None
        yield connection
    finally:
        try:
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    raise LedgerError("Cannot close ledger database") from None
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
