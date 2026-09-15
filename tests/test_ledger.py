from __future__ import annotations

import hashlib
import math
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError

import pytest

from quire.errors import LedgerError
from quire.store.ledger import Ledger
from quire.store.models import ResourceSpec, task_identity
from quire.workspace import write_bytes

URL = "https://example.test/book?sig=private"
RESOURCES = tuple(ResourceSpec(1, i, f"https://example.test/{i}.jpg", URL) for i in range(1, 4))


def save(root, task_id, page, content=b"image"):
    relative = f"cache/{task_id}/{page}.jpg"
    write_bytes(root / relative, content)
    return relative


def test_roundtrip_and_idempotent_manifest(tmp_path):
    with Ledger(tmp_path) as ledger:
        task_id = ledger.create_task(
            URL, {"selector": "main", "options": {"b": 2, "a": 1}}, RESOURCES
        )
        assert task_id == ledger.create_task(
            URL, {"options": {"a": 1, "b": 2}, "selector": "main"}, RESOURCES
        )
        initial = ledger.snapshot(task_id)
        assert initial.status == "pending"
        assert [r.spec.page for r in initial.resources] == [1, 2, 3]
        with pytest.raises(FrozenInstanceError):
            initial.status = "done"
        ledger.start(task_id)
        claimed = ledger.claim(task_id, 1, 1)
        assert claimed.status == "downloading" and claimed.attempts == 1
        relative = save(tmp_path, task_id, 1)
        ledger.complete(task_id, 1, 1, relative)
        ledger.claim(task_id, 1, 2)
        ledger.fail(task_id, 1, 2, "network")
        ledger.claim(task_id, 1, 3)
        ledger.fail(task_id, 1, 3, "invalid_image")
        assert ledger.finish(task_id).status == "partial"
        assert task_id == ledger.create_task(
            URL, {"selector": "main", "options": {"a": 1, "b": 2}}, RESOURCES
        )
        assert ledger.snapshot(task_id).status == "partial"
    with Ledger(tmp_path) as ledger:
        snapshot = ledger.snapshot(task_id)
        first = snapshot.resources[0]
        assert (first.local_path, first.size, first.sha256) == (
            relative,
            5,
            hashlib.sha256(b"image").hexdigest(),
        )
        assert [r.error_code for r in snapshot.resources] == [None, "network", "invalid_image"]
        restored = ledger.recover(task_id)
        assert restored.status == "pending"
        assert [r.status for r in restored.resources] == ["done", "pending", "pending"]
        ledger.start(task_id)
        for page in (2, 3):
            assert ledger.claim(task_id, 1, page).attempts == 2
            ledger.complete(task_id, 1, page, save(tmp_path, task_id, page))
        assert ledger.finish(task_id).status == "done"
        assert ledger.recover(task_id).status == "done"


@pytest.mark.parametrize("change", ["missing", "same-size", "larger", "symlink", "directory"])
def test_recovery_revalidates_cache_and_preserves_other_files(tmp_path, change):
    with Ledger(tmp_path) as ledger:
        task_id = ledger.create_task(URL, {}, RESOURCES[:1])
        ledger.start(task_id)
        ledger.claim(task_id, 1, 1)
        relative = save(tmp_path, task_id, 1)
        ledger.complete(task_id, 1, 1, relative)
        ledger.finish(task_id)
        path = tmp_path / relative
        path.unlink()
        if change in {"same-size", "larger"}:
            path.write_bytes(b"other" if change == "same-size" else b"longer image")
        elif change == "symlink":
            other = tmp_path / "original.jpg"
            other.write_bytes(b"image")
            path.symlink_to(other)
        elif change == "directory":
            path.mkdir()
        orphan = path.parent / "uncommitted.tmp"
        orphan.write_bytes(b"do not adopt or delete")
        state = ledger.recover(task_id)
        record = state.resources[0]
        assert state.status == "pending" and record.status == "pending"
        assert (record.local_path, record.sha256, record.size) == (None, None, None)
        assert record.attempts == 1
        assert orphan.read_bytes() == b"do not adopt or delete"
        if change == "symlink":
            assert other.read_bytes() == b"image"


def test_relocated_data_root_reuses_valid_resources(tmp_path):
    root = tmp_path / "before"
    with Ledger(root) as ledger:
        task_id = ledger.create_task(URL, {}, RESOURCES[:1])
        ledger.start(task_id)
        ledger.claim(task_id, 1, 1)
        ledger.complete(task_id, 1, 1, save(root, task_id, 1))
    shutil.move(root, tmp_path / "after")
    with Ledger(tmp_path / "after") as ledger:
        assert ledger.recover(task_id).status == "done"


def test_failed_task_can_only_restart_after_recovery(tmp_path):
    with Ledger(tmp_path) as ledger:
        task_id = ledger.create_task(URL, {}, RESOURCES[:1])
        ledger.start(task_id)
        ledger.claim(task_id, 1, 1)
        ledger.fail(task_id, 1, 1, "cancelled")
        assert ledger.finish(task_id).status == "failed"
        with pytest.raises(LedgerError):
            ledger.start(task_id)
        ledger.recover(task_id)
        ledger.start(task_id)
        assert ledger.claim(task_id, 1, 1).attempts == 2


def test_illegal_transitions_and_unknown_task_leave_state_unchanged(tmp_path):
    with Ledger(tmp_path) as ledger:
        task_id = ledger.create_task(URL, {}, RESOURCES[:1])
        for action in (
            lambda: ledger.claim(task_id, 1, 1),
            lambda: ledger.complete(task_id, 1, 1, "anything"),
            lambda: ledger.fail(task_id, 1, 1, "network"),
            lambda: ledger.finish(task_id),
            lambda: ledger.snapshot("unknown"),
            lambda: ledger.recover("unknown"),
            lambda: ledger.start("unknown"),
        ):
            with pytest.raises(LedgerError):
                action()
        ledger.start(task_id)
        ledger.claim(task_id, 1, 1)
        for action in (
            lambda: ledger.start(task_id),
            lambda: ledger.claim(task_id, 1, 1),
            lambda: ledger.finish(task_id),
            lambda: ledger.fail(task_id, 1, 1, "https://example.test?secret"),
            lambda: ledger.complete(task_id, 1, 1, "cache/../ledger.db"),
        ):
            with pytest.raises(LedgerError):
                action()
        assert ledger.snapshot(task_id).resources[0].status == "downloading"


def test_context_and_thread_ownership(tmp_path):
    ledger = Ledger(tmp_path)
    with pytest.raises(LedgerError):
        ledger.snapshot("anything")
    with ledger:
        task_id = ledger.create_task(URL, {}, RESOURCES)
        with pytest.raises(LedgerError):
            ledger.__enter__()
        with ThreadPoolExecutor(1) as pool:
            for operation in (ledger.snapshot, ledger.start):
                with pytest.raises(LedgerError):
                    pool.submit(operation, task_id).result()
    with ledger:
        assert ledger.snapshot(task_id).status == "pending"


def test_insert_failure_rolls_back_whole_task(tmp_path):
    with Ledger(tmp_path) as ledger:
        ledger._db.execute(
            "CREATE TRIGGER reject_page BEFORE INSERT ON resources "
            "WHEN NEW.page = 2 BEGIN SELECT RAISE(ABORT, 'test'); END"
        )
        with pytest.raises(LedgerError):
            ledger.create_task(URL, {}, RESOURCES)
        assert ledger._db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
        assert ledger._db.execute("SELECT count(*) FROM resources").fetchone()[0] == 0
        ledger._db.execute("DROP TRIGGER reject_page")
        assert ledger.create_task(URL, {}, RESOURCES)


def test_manifest_and_options_identity():
    original, canonical = task_identity(URL, {"a": 1, "nested": {"x": [1, 2]}}, RESOURCES)
    assert canonical == '{"a":1,"nested":{"x":[1,2]}}'
    for url, options, resources in (
        (URL + "x", {"a": 1, "nested": {"x": [1, 2]}}, RESOURCES),
        (URL, {"a": 1, "nested": {"x": [2, 1]}}, RESOURCES),
        (URL, {"a": 2}, RESOURCES),
        (URL, {"a": 1}, RESOURCES[:2]),
        (URL, {"a": 1}, (ResourceSpec(1, 1, RESOURCES[0].url + "?v=2"),)),
    ):
        assert task_identity(url, options, resources)[0] != original


@pytest.mark.parametrize(
    "options", [{"x": math.nan}, {"x": math.inf}, {1: "bad"}, {"x": b"bad"}, {"x": (1, 2)}]
)
def test_invalid_json_options(options):
    with pytest.raises(LedgerError):
        task_identity(URL, options, RESOURCES)


def test_cyclic_options_and_bad_manifest():
    cyclic = {}
    cyclic["self"] = cyclic
    with pytest.raises(LedgerError):
        task_identity(URL, cyclic, RESOURCES)
    for resources in ((), RESOURCES[::-1], (RESOURCES[0], RESOURCES[0])):
        with pytest.raises(LedgerError):
            task_identity(URL, {}, resources)
    for url in (
        "file:///secret",
        "relative/page",
        "//example.test/book",
        "https://example.test/\n",
    ):
        with pytest.raises(LedgerError):
            task_identity(url, {}, RESOURCES)


@pytest.mark.parametrize(
    "chapter,page,url,referer",
    [
        (0, 1, URL, ""),
        (1, -1, URL, ""),
        (True, 1, URL, ""),
        (1, 2**64, URL, ""),
        (1, 1, "file:///secret", ""),
        (1, 1, URL, "javascript:alert(1)"),
    ],
)
def test_invalid_resource_spec(chapter, page, url, referer):
    with pytest.raises(LedgerError):
        ResourceSpec(chapter, page, url, referer)


def test_sql_values_are_parameters(tmp_path):
    url = "https://example.test/';DROP%20TABLE%20tasks;--?secret=value"
    with Ledger(tmp_path) as ledger:
        task_id = ledger.create_task(
            url, {"selector": "'); DELETE FROM tasks; --"}, (ResourceSpec(1, 1, url),)
        )
        assert ledger.snapshot(task_id).resources[0].spec.url == url
        assert ledger._db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1


def test_read_error_does_not_expose_sql_or_paths(tmp_path):
    with Ledger(tmp_path) as ledger:
        ledger._db.execute("DROP TABLE resources")
        with pytest.raises(LedgerError):
            ledger.create_task(URL, {}, RESOURCES)
        ledger._db.execute("DROP TABLE tasks")
        with pytest.raises(LedgerError, match="Cannot read ledger task"):
            ledger.snapshot("unknown")


def test_complete_rejects_file_already_assigned_to_other_page(tmp_path):
    with Ledger(tmp_path) as ledger:
        task_id = ledger.create_task(URL, {}, RESOURCES)
        ledger.start(task_id)
        ledger.claim(task_id, 1, 1)
        relative = save(tmp_path, task_id, 1)
        ledger.complete(task_id, 1, 1, relative)
        ledger.claim(task_id, 1, 2)
        with pytest.raises(LedgerError, match="already assigned"):
            ledger.complete(task_id, 1, 2, relative)
        assert ledger.snapshot(task_id).resources[1].status == "downloading"


def test_interrupted_recovery_preserves_previous_snapshot(tmp_path, monkeypatch):
    with Ledger(tmp_path) as ledger:
        task_id = ledger.create_task(URL, {}, RESOURCES)
        ledger.start(task_id)
        for resource in RESOURCES:
            page = resource.page
            ledger.claim(task_id, 1, page)
            ledger.complete(task_id, 1, page, save(tmp_path, task_id, page))
        before = ledger.finish(task_id)
        calls = []

        def interrupted(task, resource):
            calls.append(resource.spec.page)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return False

        monkeypatch.setattr(ledger, "_valid_cache", interrupted)
        with pytest.raises(KeyboardInterrupt):
            ledger.recover(task_id)
        assert ledger.snapshot(task_id) == before


def test_fsync_failure_never_marks_resource_done(tmp_path, monkeypatch):
    with Ledger(tmp_path) as ledger:
        task_id = ledger.create_task(URL, {}, RESOURCES[:1])
        ledger.start(task_id)
        ledger.claim(task_id, 1, 1)
        relative = save(tmp_path, task_id, 1)

        def full_disk(fd):
            raise OSError("simulated fsync failure")

        monkeypatch.setattr("quire.store.cache.os.fsync", full_disk)
        with pytest.raises(LedgerError):
            ledger.complete(task_id, 1, 1, relative)
        record = ledger.snapshot(task_id).resources[0]
        assert record.status == "downloading" and record.sha256 is None


def test_wrong_thread_exit_keeps_connection_and_lock_for_owner(tmp_path):
    ledger = Ledger(tmp_path)
    with ledger:
        task_id = ledger.create_task(URL, {}, RESOURCES[:1])
        with ThreadPoolExecutor(1) as pool:
            with pytest.raises(LedgerError, match="owning thread"):
                pool.submit(ledger.__exit__, None, None, None).result()
        assert ledger.snapshot(task_id).status == "pending"
        with pytest.raises(LedgerError, match="in use"):
            with Ledger(tmp_path):
                pytest.fail("A wrong-thread exit released the owner lock")
    with Ledger(tmp_path) as reopened:
        assert reopened.snapshot(task_id).status == "pending"
