from __future__ import annotations

import sqlite3

import pytest

from quire.errors import ConfigError
from quire.server import settings as settings_mod
from quire.server.jobs import Job, JobSpec
from quire.server.progress import ChapterSink, ExportSink, make_thumbs, task_counts
from tests.mock_site.server import page_image


def test_defaults_and_target_bytes(tmp_path):
    settings = settings_mod.defaults(tmp_path)
    assert settings.output_path == tmp_path / "library"
    assert settings.target_bytes == 50_000_000
    lossless = settings_mod.UiSettings(str(tmp_path), compress="lossless")
    assert lossless.target_bytes is None


def test_load_missing_returns_defaults(tmp_path):
    assert settings_mod.load(tmp_path / "none.json", tmp_path).theme == "paper:light"


@pytest.mark.parametrize(
    "payload",
    [
        {"output_dir": "relative"},
        {"output_dir": "/tmp/x", "compress": "huge"},
        {"output_dir": "/tmp/x", "target_mb": 0},
        {"output_dir": "/tmp/x", "concurrency": 0},
        {"output_dir": "/tmp/x", "rate": float("nan")},
        {"output_dir": "/tmp/x", "rate": -1},
        {"output_dir": "/tmp/x", "ocr": "maybe"},
        {"output_dir": "/tmp/x", "theme": "paper:dark"},
        {"output_dir": "/tmp/x", "auto_check_updates": "yes"},
    ],
)
def test_invalid_settings_rejected(payload):
    with pytest.raises(ConfigError):
        settings_mod.parse(payload)


def test_parse_rejects_non_dict_and_empty_output(tmp_path):
    with pytest.raises(ConfigError):
        settings_mod.parse([1, 2])
    with pytest.raises(ConfigError):
        settings_mod.parse({"output_dir": "  "})
    file_path = tmp_path / "afile"
    file_path.write_text("x")
    with pytest.raises(ConfigError):
        settings_mod.parse({"output_dir": str(file_path)})


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "settings.json"
    out = tmp_path / "books"
    settings_mod.save(
        path,
        settings_mod.UiSettings(str(out), compress="small", rate=2.0, auto_check_updates=True),
    )
    loaded = settings_mod.load(path, tmp_path)
    assert loaded.compress == "small"
    assert loaded.rate == 2.0
    assert loaded.auto_check_updates is True
    assert out.is_dir()


def test_task_counts_empty_and_populated(tmp_path):
    db = tmp_path / "ledger.db"
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE tasks (id TEXT, url TEXT, options TEXT, status TEXT, created_at TEXT, updated_at TEXT)"
    )
    connection.execute(
        "CREATE TABLE resources (task_id TEXT, chapter INT, page INT, url TEXT, referer TEXT,"
        " status TEXT, attempts INT, local_path TEXT, sha256 TEXT, size INT, error_code TEXT)"
    )
    assert task_counts(connection, "2999", "") == (None, "")
    connection.execute("INSERT INTO tasks VALUES ('t1', '', '', 'running', '2999', '2999')")
    connection.executemany(
        "INSERT INTO resources VALUES ('t1', 1, ?, '', '', ?, 0, NULL, NULL, NULL, NULL)",
        [(1, "done"), (2, "done"), (3, "failed"), (4, "pending")],
    )
    counts, task_id = task_counts(connection, "2999", "")
    assert task_id == "t1"
    assert counts == (2, 1, 4)
    connection.close()


def test_make_thumbs(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "00001-000001.jpg").write_bytes(page_image(1))
    (cache / "00001-000002.jpg").write_bytes(page_image(2))
    (cache / "00001-000003.jpg").write_bytes(b"corrupt")
    (cache / "notes.json").write_text("{}")
    thumbed: set[int] = set()
    made = make_thumbs(cache, tmp_path / "thumbs", thumbed)
    assert made == [1, 2]
    assert (tmp_path / "thumbs" / "p1.jpg").stat().st_size > 100
    assert make_thumbs(cache, tmp_path / "thumbs", thumbed) == []  # 已做过的跳过
    assert make_thumbs(tmp_path / "missing", tmp_path / "thumbs", set()) == []


def _job() -> Job:
    return Job("j1", JobSpec(kind="manga", url="http://e.c/1", title="t", formats=("pdf",)))


def test_export_sink_announces_phase_once():
    job = _job()
    sink = ExportSink(job)
    sink.update(1, 3)
    sink.update(2, 3)
    events = job.events_after(0, 0)
    assert [e.kind for e in events].count("phase") == 1
    assert events[-1].data["done"] == 2


def test_chapter_sink_updates_job_counts():
    job = _job()
    ChapterSink(job).update(2, 5)
    assert job.done == 2 and job.total == 5
    assert job.events_after(0, 0)[-1].data == {"done": 2, "failed": 0, "total": 5}


def test_jobspec_validation():
    with pytest.raises(ConfigError):
        JobSpec(kind="manga", url="http://e.c", title="t", formats=("pdf",), ocr="bad")
    with pytest.raises(ConfigError):
        JobSpec(kind="novel", url="http://e.c", title="t", formats=("epub",), compress="bad")
    with pytest.raises(ConfigError):
        JobSpec(kind="manga", url="http://e.c", title="t", formats=("pdf",), target_bytes=0)
    spec = JobSpec(kind="manga", url="http://e.c", title="t", formats=("pdf",), target_bytes=None)
    assert spec.compress == "balanced"


def test_thumbnail_identity_is_scoped_to_ledger_task(tmp_path):
    from quire.server.progress import thumb_event

    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "00002-000001.jpg").write_bytes(page_image(1))
    seen = set()
    for task in ("volume-a", "volume-b"):
        assert make_thumbs(cache, tmp_path / "thumbs", seen, task) == [1]
        event = thumb_event(cache, "job", task, 1)
        assert event["chapter_id"] == f"{task}:2"
        identity = event["url"].rsplit("/", 1)[-1]
        assert (tmp_path / "thumbs" / f"p{identity}.jpg").exists()
    assert len(list((tmp_path / "thumbs").iterdir())) == 2
