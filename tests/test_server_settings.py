from __future__ import annotations

import sqlite3

import pytest

from quire.errors import ConfigError
from quire.server import settings as settings_mod
from quire.server.jobs import Job, JobSpec
from quire.server.progress import ChapterSink, ExportSink, make_thumbs, task_counts
from tests.mock_site.server import page_image


def test_defaults_and_task_sizes(tmp_path):
    settings = settings_mod.defaults(tmp_path)
    assert settings.output_path == tmp_path / "library"
    assert settings.task_novel_mb == 100
    assert settings.task_manga_mb == 500


def test_legacy_target_mb_key_is_dropped():
    assert settings_mod.parse({"output_dir": "/tmp/x", "target_mb": 1}) == settings_mod.UiSettings(
        "/tmp/x"
    )


@pytest.mark.parametrize(
    ("legacy", "preset"),
    [("lossless", "archive"), ("high", "balanced"), ("tiny", "small")],
)
def test_legacy_compress_presets_are_mapped(legacy, preset):
    assert settings_mod.parse({"output_dir": "/tmp/x", "compress": legacy}).compress == preset


def test_load_missing_returns_defaults(tmp_path):
    assert settings_mod.load(tmp_path / "none.json", tmp_path).theme == "paper:light"


@pytest.mark.parametrize(
    "payload",
    [
        {"output_dir": "relative"},
        {"output_dir": "/tmp/x", "compress": "huge"},
        {"output_dir": "/tmp/x", "task_novel_mb": 0},
        {"output_dir": "/tmp/x", "task_manga_mb": "500"},
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


def test_concurrent_settings_saves_use_distinct_staging_files(tmp_path, monkeypatch):
    import os
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from quire import workspace

    path = tmp_path / "settings.json"
    settings_mod.save(path, settings_mod.defaults(tmp_path))
    original_replace = os.replace
    barrier = threading.Barrier(2)
    staged = []

    def synchronized_replace(source, destination):
        staged.append(source)
        barrier.wait(timeout=5)
        original_replace(source, destination)

    monkeypatch.setattr(workspace.os, "replace", synchronized_replace)
    candidates = [
        settings_mod.UiSettings(str(tmp_path / "books"), theme=theme)
        for theme in ("darkroom:dark", "swiss:light")
    ]
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda settings: settings_mod.save(path, settings), candidates))
    assert len(set(staged)) == 2
    assert settings_mod.load(path, tmp_path) in candidates
    assert all(not stage.exists() for stage in staged)


def test_concurrent_put_settings_no_lost_update(tmp_path, monkeypatch):
    import http.client
    import json
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    from quire.server.app import make_server

    server = make_server("127.0.0.1", 0, data_root=tmp_path, opener=lambda p: None)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    original_load = settings_mod.load

    def slow_load(path, data_root):
        settings = original_load(path, data_root)
        time.sleep(0.2)  # 拉宽读改写窗口：无锁时两个 PUT 必读到同一基线
        return settings

    monkeypatch.setattr(settings_mod, "load", slow_load)

    def put(payload):
        host, port = server.server_address[:2]
        conn = http.client.HTTPConnection(host, port, timeout=10)
        conn.request(
            "PUT",
            "/api/settings",
            json.dumps(payload),
            {"X-Quire-Token": server.token, "Content-Type": "application/json"},
        )
        response = conn.getresponse()
        body = json.loads(response.read())
        conn.close()
        return response.status, body

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(put, ({"theme": "swiss:light"}, {"compress": "small"})))
    finally:
        server.shutdown()
        server.server_close()
    assert all(status == 200 for status, _ in results)
    assert results[0][1]["theme"] == "swiss:light"  # 各自响应都含自己刚写的值
    assert results[1][1]["compress"] == "small"
    final = settings_mod.load(server.settings_path, tmp_path)
    assert final.theme == "swiss:light"
    assert final.compress == "small"


def test_settings_failed_replace_preserves_original_and_cleans_staging(tmp_path, monkeypatch):
    from quire import workspace

    path = tmp_path / "settings.json"
    original = settings_mod.defaults(tmp_path)
    settings_mod.save(path, original)

    def fail_replace(*args):
        raise OSError("disk failure")

    monkeypatch.setattr(workspace.os, "replace", fail_replace)
    with pytest.raises(OSError):
        settings_mod.save(path, settings_mod.UiSettings(str(tmp_path), theme="swiss:light"))
    assert settings_mod.load(path, tmp_path) == original
    assert not list(tmp_path.glob(".settings.json.*"))
