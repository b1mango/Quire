from __future__ import annotations

import json

import pytest

from scripts import acceptance_manga as manga
from scripts.acceptance_capture import write_json
from scripts.acceptance_report import assess


def measurement():
    return {
        "capture_ok": True,
        "result": {
            "missing": 0,
            "pages": 1,
            "source_resources": 1,
            "resources_reused": 0,
            "artifacts_reused": False,
            "compression": {"target_bytes": 50_000_000, "target_met": True},
            "artifacts": [
                {"format": kind, "bytes": 1000, "target_met": True}
                for kind in ("pdf", "cbz", "zip")
            ],
        },
        "verification": {
            "source_count": 1,
            "page_count": 1,
            "structure_passed": True,
            "quality_passed": True,
            "artifact_bytes": dict.fromkeys(("pdf", "cbz", "zip"), 1000),
        },
        "source_files": ["one.bin"],
        "calls": {"encoding_trials": 1},
        "requests": [],
        "disk": {"errors": [], "observed_peak": {"logical_bytes": 1000}},
        "peak_rss_bytes": 1000,
        "elapsed_s": 1,
    }


CONFIG = {"mode": "replay", "url": "https://xkcd.com/1/"}


def test_assess_uses_independent_source_count_and_verified_page_count():
    data = measurement()
    assert assess(data, CONFIG)["passed"]
    books = {**CONFIG, "url": "https://books.toscrape.com/"}
    assert not assess(data, books)["checks"]["source_count"]
    data["result"]["pages"] = 2
    assert not assess(data, CONFIG)["checks"]["page_count"]
    data["result"]["missing"] = 1
    assert not assess(data, CONFIG)["checks"]["expected_missing"]


@pytest.mark.parametrize("mode", ["resource-reuse", "artifact-reuse"])
def test_reuse_rejects_new_image_requests(mode):
    data = measurement()
    data["result"]["resources_reused"] = 1
    data["result"]["artifacts_reused"] = mode == "artifact-reuse"
    data["calls"]["encoding_trials"] = 0 if mode == "artifact-reuse" else 1
    data["requests"] = [{"url": CONFIG["url"]}, {"url": "https://xkcd.com/robots.txt"}]
    config = {**CONFIG, "mode": mode}
    assert assess(data, config)["passed"]
    data["requests"].append({"url": "https://imgs.xkcd.com/comics/one.png"})
    result = assess(data, config)
    assert result["checks"]["reuse"]
    assert not result["checks"]["no_image_requests"] and not result["behavior_passed"]


def test_quality_and_budget_failure_remain_distinct_from_correct_behavior():
    data = measurement()
    data["verification"]["quality_passed"] = False
    data["disk"]["observed_peak"]["logical_bytes"] = 250_000_001
    result = assess(data, CONFIG)
    assert not result["passed"] and result["behavior_passed"]
    assert not result["checks"]["quality"] and not result["budgets"]["disk_250mb"]


@pytest.mark.parametrize("target", [1, 500, 2000, None])
def test_target_status_must_match_artifact_size(target):
    data = measurement()
    expected = None if target is None else target >= 1000
    data["result"]["compression"] = {"target_bytes": target, "target_met": expected}
    for item in data["result"]["artifacts"]:
        item["target_met"] = expected
    config = {**CONFIG, "compression": {"target_bytes": target}}
    assert assess(data, config)["checks"]["target"]
    data["result"]["compression"]["target_met"] = not expected
    assert not assess(data, config)["checks"]["target"]


def test_target_rejects_incorrect_sizes_and_individual_status():
    data = measurement()
    data["result"]["artifacts"][0]["bytes"] = 999
    assert not assess(data, CONFIG)["checks"]["target"]
    data["result"]["artifacts"][0]["bytes"] = 1000
    data["result"]["artifacts"][0]["target_met"] = False
    assert not assess(data, CONFIG)["checks"]["target"]


def test_failed_sampler_cannot_pass_without_result():
    data = measurement()
    del data["result"]
    data.update(capture_ok=False, source_files=[])
    data["disk"] = {"errors": ["PermissionError"], "observed_peak": None}
    result = assess(data, CONFIG)
    assert not result["passed"] and not result["behavior_passed"]
    assert not result["checks"]["disk_sampling"]


def test_continue_reuses_measurement_and_checks_config(tmp_path, monkeypatch):
    controls = tmp_path / "controls"
    controls.mkdir()
    write_json(controls / "case.json", CONFIG)
    write_json(controls / "case.metrics.json", measurement())

    def forbidden(*args, **kwargs):
        raise AssertionError("Must not spawn another capture worker")

    monkeypatch.setattr(manga.subprocess, "run", forbidden)
    result = manga.run_case(tmp_path, "case", CONFIG, verify=False, reuse_measurement=True)
    assert result == measurement()
    with pytest.raises(FileExistsError):
        manga.run_case(tmp_path, "case", CONFIG, verify=False)
    with pytest.raises(FileExistsError):
        manga.run_case(tmp_path, "case", {**CONFIG, "resume": True}, reuse_measurement=True)


def test_existing_workload_measurement_rejected_before_touching_corpus(tmp_path, monkeypatch):
    controls = tmp_path / "controls"
    controls.mkdir()
    write_json(controls / "repeated-default.metrics.json", measurement())

    def forbidden(*args, **kwargs):
        raise AssertionError("Must reject before any corpus generation")

    monkeypatch.setattr(manga, "repeated_corpus", forbidden)
    with pytest.raises(FileExistsError, match="measurements exist"):
        manga.run(tmp_path, "workload")


def test_existing_corpus_is_validated_and_never_rewritten(tmp_path):
    source = tmp_path / "corpus" / "xkcd-657"
    source.mkdir(parents=True)
    write_json(source / "sources.json", ["image.bin"])
    (source / "image.bin").write_bytes(b"first-image")
    root = manga.repeated_corpus(tmp_path, 3, fullsize=True)
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.iterdir()}
    assert manga.repeated_corpus(tmp_path, 3, fullsize=True) == root
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.iterdir()} == before
    (source / "image.bin").write_bytes(b"changed-image")
    with pytest.raises(ValueError, match="corpus differs"):
        manga.repeated_corpus(tmp_path, 3, fullsize=True)
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.iterdir()} == before
    (source / "image.bin").write_bytes(b"first-image")
    body = next(p for p in root.iterdir() if p.suffix == ".bin")
    body.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="corpus differs"):
        manga.repeated_corpus(tmp_path, 3, fullsize=True)


def test_main_writes_failures_and_returns_nonzero(tmp_path, monkeypatch):
    data = measurement()
    data["verification"]["quality_passed"] = False
    data["acceptance"] = assess(data, CONFIG)
    monkeypatch.setattr(manga, "run", lambda *args, **kwargs: {"cases": {"bad-quality": data}})
    monkeypatch.setattr(
        manga.sys, "argv", ["acceptance", "--output", str(tmp_path), "--phase", "verify"]
    )
    with pytest.raises(SystemExit) as exc:
        manga.main()
    assert exc.value.code == 1
    report = json.loads((tmp_path / "report.json").read_text())
    assert not report["passed"]
    assert report["summary"]["quality_failed"] == ["bad-quality"]
    assert not report["summary"]["behavior_failed"]
