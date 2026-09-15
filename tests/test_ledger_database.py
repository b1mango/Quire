from __future__ import annotations

import os
import select
import sqlite3
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from quire.errors import LedgerError
from quire.store import database
from quire.store.database import open_database


def _task(connection: sqlite3.Connection, task_id: str = "task") -> None:
    connection.execute(
        "INSERT INTO tasks VALUES (?, ?, '{}', 'pending', 'now', 'now')",
        (task_id, "https://example.invalid/source?token=private"),
    )


def test_create_reopen_and_transaction_contract(tmp_path: Path) -> None:
    root = tmp_path / "root #? space"
    with open_database(root) as connection:
        assert connection.row_factory is sqlite3.Row
        assert connection.isolation_level is None
        assert not connection.in_transaction
        for pragma, expected in (
            ("application_id", 0x51554952),
            ("user_version", 1),
            ("foreign_keys", 1),
            ("journal_mode", "delete"),
            ("synchronous", 2),
        ):
            assert connection.execute(f"PRAGMA {pragma}").fetchone()[0] == expected
        assert {
            row["name"]
            for row in connection.execute("SELECT * FROM sqlite_schema WHERE type='table'")
        } == {"tasks", "resources"}
        connection.execute("BEGIN IMMEDIATE")
        _task(connection)
        connection.commit()
        assert connection.execute("SELECT id FROM tasks").fetchone()["id"] == "task"
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    for name in ("ledger.db", "ledger.lock"):
        assert stat.S_IMODE((root / name).stat().st_mode) == 0o600
    assert {path.name for path in root.iterdir()} == {"ledger.db", "ledger.lock"}
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")
    with open_database(root) as reopened:
        assert reopened.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1


@pytest.mark.parametrize("status", ["pending", "running", "done", "partial", "failed"])
def test_task_states(tmp_path: Path, status: str) -> None:
    with open_database(tmp_path) as connection:
        _task(connection)
        connection.execute("UPDATE tasks SET status = ?", (status,))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE tasks SET status = 'invalid'")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE tasks SET source_url = NULL")


@pytest.mark.parametrize(
    "column,value",
    [
        ("task_id", "missing"),
        ("task_id", None),
        ("chapter", 0),
        ("chapter", None),
        ("chapter", 1.5),
        ("page", 0),
        ("page", None),
        ("page", "bad"),
        ("attempts", -1),
        ("attempts", None),
        ("attempts", 0.5),
        ("status", "invalid"),
        ("status", None),
        ("url", None),
        ("referer", None),
        ("local_path", "cache/page"),
        ("sha256", "abc"),
        ("size", 1),
    ],
)
def test_resource_constraints(tmp_path: Path, column: str, value: object) -> None:
    with open_database(tmp_path) as connection:
        _task(connection)
        connection.execute(
            "INSERT INTO resources VALUES ('task', 1, 1, 'url', 'ref', 'pending', 0, "
            "NULL, NULL, NULL, NULL)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(f"UPDATE resources SET {column} = ?", (value,))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO resources SELECT * FROM resources")


@pytest.mark.parametrize(
    "path,sha256,size",
    [
        (None, "hash", 1),
        ("cache/1", None, 1),
        ("cache/1", "hash", None),
        ("cache/1", "hash", 0),
        ("cache/1", "hash", -1),
        ("cache/1", "hash", 1.5),
    ],
)
def test_done_requires_complete_metadata(
    tmp_path: Path,
    path: str | None,
    sha256: str | None,
    size: int | float | None,
) -> None:
    with open_database(tmp_path) as connection:
        _task(connection)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO resources VALUES ('task', 1, 1, 'url', 'ref', 'done', 1, ?, ?, ?, NULL)",
                (path, sha256, size),
            )


@pytest.mark.parametrize("status", ["pending", "downloading", "done", "failed"])
def test_valid_resources_and_attempts_default(tmp_path: Path, status: str) -> None:
    metadata = ("cache/1", "hash", 1) if status == "done" else (None, None, None)
    with open_database(tmp_path) as connection:
        _task(connection)
        connection.execute(
            "INSERT INTO resources (task_id, chapter, page, url, referer, status, "
            "local_path, sha256, size) VALUES ('task', 1, 1, 'url', 'ref', ?, ?, ?, ?)",
            (status, *metadata),
        )
        assert connection.execute("SELECT attempts FROM resources").fetchone()[0] == 0
        if status != "done":
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute("UPDATE resources SET size = 1")


@pytest.mark.parametrize(
    "change",
    [
        "PRAGMA user_version = 999",
        "PRAGMA application_id = 0",
        "ALTER TABLE resources ADD COLUMN extra TEXT",
        "DROP TABLE resources",
        "CREATE TRIGGER unexpected AFTER INSERT ON tasks BEGIN DELETE FROM tasks; END",
        "CREATE INDEX unexpected ON tasks(status)",
    ],
)
def test_incompatible_database_is_not_changed(tmp_path: Path, change: str) -> None:
    with open_database(tmp_path) as connection:
        connection.execute(change)
    path = tmp_path / "ledger.db"
    path.chmod(0o640)
    original = path.read_bytes()
    with pytest.raises(LedgerError) as error, open_database(tmp_path):
        pytest.fail("incompatible database accepted")
    assert path.read_bytes() == original
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert str(tmp_path) not in str(error.value)
    assert change not in str(error.value)


def test_matching_version_without_constraints_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "ledger.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE tasks AS SELECT '' AS id")
        connection.execute("PRAGMA user_version = 1")
        connection.execute("PRAGMA application_id = 1364543826")
    finally:
        connection.close()
    original = path.read_bytes()
    with pytest.raises(LedgerError), open_database(tmp_path):
        pytest.fail("weak schema accepted")
    assert path.read_bytes() == original


@pytest.mark.parametrize("contents", [b"", b"not a SQLite database\x00private"])
def test_empty_and_corrupt_old_files_are_not_reinitialized(tmp_path: Path, contents: bytes) -> None:
    path = tmp_path / "ledger.db"
    path.write_bytes(contents)
    with pytest.raises(LedgerError) as error, open_database(tmp_path):
        pytest.fail("corrupt database accepted")
    assert path.read_bytes() == contents
    assert "private" not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
@pytest.mark.parametrize("dangling", [False, True])
def test_wal_sidecars_are_rejected_without_modification(
    tmp_path: Path,
    suffix: str,
    dangling: bool,
) -> None:
    with open_database(tmp_path):
        pass
    path = tmp_path / "ledger.db"
    original = path.read_bytes()
    sidecar = tmp_path / f"ledger.db{suffix}"
    target = tmp_path / "absent"
    if dangling:
        sidecar.symlink_to(target)
    else:
        sidecar.write_bytes(b"foreign journal")
    with pytest.raises(LedgerError, match="journal mode"), open_database(tmp_path):
        pytest.fail("unsupported sidecar accepted")
    assert path.read_bytes() == original
    if dangling:
        assert sidecar.is_symlink() and sidecar.readlink() == target
        assert not target.exists()
    else:
        assert sidecar.read_bytes() == b"foreign journal"


def test_root_symlink_is_rejected_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o755)
    root = tmp_path / "root"
    root.symlink_to(target, target_is_directory=True)
    with pytest.raises(LedgerError, match="directory"), open_database(root):
        pytest.fail("root symlink accepted")
    assert list(target.iterdir()) == []
    assert stat.S_IMODE(target.stat().st_mode) == 0o755


def test_foreign_key_damage_is_rejected_without_modification(tmp_path: Path) -> None:
    with open_database(tmp_path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "INSERT INTO resources VALUES ('missing', 1, 1, 'url', 'ref', 'pending', 0, "
            "NULL, NULL, NULL, NULL)"
        )
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    path = tmp_path / "ledger.db"
    original = path.read_bytes()
    with pytest.raises(LedgerError, match="references"), open_database(tmp_path):
        pytest.fail("orphaned resource accepted")
    assert path.read_bytes() == original


@pytest.mark.parametrize("name", ["ledger.db", "ledger.lock"])
@pytest.mark.parametrize("kind", ["symlink", "dangling", "directory", "fifo", "hardlink"])
def test_unsafe_file_types_are_rejected(tmp_path: Path, name: str, kind: str) -> None:
    root = tmp_path / "root"
    root.mkdir()
    target = tmp_path / "target"
    target.write_bytes(b"unchanged")
    path = root / name
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "dangling":
        path.symlink_to(tmp_path / "absent")
    elif kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        os.link(target, path)
    with pytest.raises(LedgerError), open_database(root):
        pytest.fail("unsafe file accepted")
    assert target.read_bytes() == b"unchanged"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_existing_root_permissions_are_preserved(tmp_path: Path) -> None:
    tmp_path.chmod(0o755)
    with open_database(tmp_path):
        pass
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o755


def test_second_owner_rejected_and_exception_releases_lock(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="caller"), open_database(tmp_path) as connection:
        inode = (tmp_path / "ledger.lock").stat().st_ino
        with pytest.raises(LedgerError, match="in use"), open_database(tmp_path):
            pytest.fail("second owner accepted")
        connection.execute("BEGIN IMMEDIATE")
        _task(connection)
        raise RuntimeError("caller")
    with pytest.raises(sqlite3.ProgrammingError):
        connection.execute("SELECT 1")
    assert (tmp_path / "ledger.lock").stat().st_ino == inode
    with open_database(tmp_path) as connection:
        assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


def test_connection_keeps_sqlite_thread_check(tmp_path: Path) -> None:
    with open_database(tmp_path) as connection, ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(connection.execute, "SELECT 1")
        with pytest.raises(sqlite3.ProgrammingError):
            future.result()


def test_initialization_failure_cleans_only_own_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unrelated = tmp_path / "unrelated"
    unrelated.write_bytes(b"keep")
    with monkeypatch.context() as patch:
        patch.setattr(database, "_RESOURCES_SQL", "invalid SQL containing private path")
        with pytest.raises(LedgerError) as error, open_database(tmp_path):
            pytest.fail("failed initialization accepted")
        assert "private" not in str(error.value)
    assert {path.name for path in tmp_path.iterdir()} == {"ledger.lock", "unrelated"}
    assert unrelated.read_bytes() == b"keep"
    with open_database(tmp_path):
        pass


_CRASH_CHILD = """
import sqlite3, sys, time
from pathlib import Path
from quire.store.database import open_database
with open_database(Path(sys.argv[1])) as db:
    if sys.argv[2] == 'unknown':
        db.execute('PRAGMA user_version = 99')
    db.execute('PRAGMA cache_size = 1')
    db.execute('PRAGMA cache_spill = ON')
    db.execute('BEGIN IMMEDIATE')
    db.execute("UPDATE tasks SET options_json = ?", ('x' * 200000,))
    print('ready', flush=True)
    time.sleep(60)
"""


@pytest.mark.parametrize("unknown_version", [False, True])
def test_real_kill_hot_journal_recovery(tmp_path: Path, unknown_version: bool) -> None:
    with open_database(tmp_path) as connection:
        _task(connection)
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _CRASH_CHILD,
            str(tmp_path),
            "unknown" if unknown_version else "valid",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert select.select([child.stdout], [], [], 10)[0], "child did not reach spill"
        assert child.stdout.readline().strip() == "ready"
        with pytest.raises(LedgerError, match="in use"), open_database(tmp_path):
            pytest.fail("second process accepted")
        journal = tmp_path / "ledger.db-journal"
        assert journal.stat().st_size > 512
        assert journal.read_bytes()[:8] == bytes.fromhex("d9d505f920a163d7")
    finally:
        child.kill()
        child.communicate(timeout=10)
    path = tmp_path / "ledger.db"
    before = (path.read_bytes(), journal.read_bytes())
    if unknown_version:
        with pytest.raises(LedgerError, match="version"), open_database(tmp_path):
            pytest.fail("unknown version recovered in place")
        assert (path.read_bytes(), journal.read_bytes()) == before
    else:
        with open_database(tmp_path) as connection:
            assert connection.execute("SELECT options_json FROM tasks").fetchone()[0] == "{}"
            assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            assert not connection.in_transaction
        assert not journal.exists()
        with open_database(tmp_path) as connection:
            assert connection.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1
