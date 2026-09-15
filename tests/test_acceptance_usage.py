from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts import acceptance_usage as disk


def test_scan_regular_files_links_sparse_allocation_and_case_boundary(tmp_path):
    case, corpus = tmp_path / "case", tmp_path / "corpus"
    work, outputs = case / "work", case / "outputs"
    for directory in (work, outputs, corpus):
        directory.mkdir(parents=True)
    original, sparse = work / "original", outputs / "sparse"
    original.write_bytes(b"source" * 1000)
    with sparse.open("wb") as stream:
        stream.seek(1024 * 1024)
        stream.write(b"x")
    (outputs / "hardlink").hardlink_to(original)
    (corpus / "outside").write_bytes(b"not in the case" * 1000)
    (case / "file-link").symlink_to(corpus / "outside")
    (case / "directory-link").symlink_to(corpus, target_is_directory=True)
    (case / "loop").symlink_to(case, target_is_directory=True)
    (case / "dangling").symlink_to(tmp_path / "absent")
    os.mkfifo(case / "fifo")

    stats = [original.lstat(), sparse.lstat()]
    expected = disk.Usage(sum(s.st_size for s in stats), sum(s.st_blocks * 512 for s in stats), 2)
    assert disk.scan_usage(case) == expected
    assert disk.scan_usage(case / "directory-link") == disk.Usage()
    assert disk.scan_usage(case / "file-link") == disk.Usage()
    assert disk.scan_usage(original) == disk.Usage(stats[0].st_size, stats[0].st_blocks * 512, 1)
    assert disk.scan_usage(tmp_path / "absent" / "child") == disk.Usage()
    with pytest.raises(OSError):
        disk.scan_usage(case / "directory-link" / "outside")


@pytest.mark.parametrize("stage", ["lstat", "open", "scandir"])
def test_scan_tolerates_disappearing_entries(tmp_path, stage):
    gone, kept = tmp_path / "gone", tmp_path / "kept"
    kept.write_bytes(b"retained")
    if stage == "lstat":
        gone.write_bytes(b"gone")
    else:
        gone.mkdir()
    original = getattr(os, stage)

    def disappear(path, *args, **kwargs):
        target = path == "gone" if stage != "scandir" else os.fstat(path).st_ino == gone_ino
        if target:
            gone.unlink() if gone.is_file() else gone.rmdir()
            raise FileNotFoundError("removed concurrently")
        return original(path, *args, **kwargs)

    gone_ino = gone.lstat().st_ino
    with patch.object(disk.os, stage, disappear):
        result = disk.scan_usage(tmp_path)
    info = kept.lstat()
    assert result == disk.Usage(info.st_size, info.st_blocks * 512, 1)


def test_scan_never_follows_directory_replaced_by_symlink(tmp_path):
    root, outside = tmp_path / "case", tmp_path / "outside"
    child = root / "child"
    child.mkdir(parents=True)
    outside.mkdir()
    (outside / "secret").write_bytes(b"private")
    original = os.open

    def replace(path, flags, *args, **kwargs):
        if path == "child":
            child.rmdir()
            child.symlink_to(outside, target_is_directory=True)
        return original(path, flags, *args, **kwargs)

    with patch.object(disk.os, "open", replace), pytest.raises(OSError):
        disk.scan_usage(root)


@pytest.mark.parametrize("operation", ["lstat", "open", "scandir"])
def test_scan_reports_non_disappearance_errors(tmp_path, operation):
    with (
        patch.object(disk.os, operation, side_effect=PermissionError("private path")),
        pytest.raises(PermissionError),
    ):
        disk.scan_usage(tmp_path)


def test_scan_closes_descriptors_on_error(tmp_path):
    (tmp_path / "nested").mkdir()
    opened = []
    original = os.open

    def track(*args, **kwargs):
        descriptor = original(*args, **kwargs)
        opened.append(descriptor)
        return descriptor

    with (
        patch.object(disk.os, "open", track),
        patch.object(disk.os, "scandir", side_effect=OSError("I/O failure")),
        pytest.raises(OSError),
    ):
        disk.scan_usage(tmp_path)
    assert opened
    for descriptor in opened:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_monitor_checkpoints_capture_short_lived_peak_and_detach_report(tmp_path):
    work, output = tmp_path / "work", tmp_path / "outputs"
    work.mkdir()
    output.mkdir()
    (work / "source").write_bytes(b"source")
    initial = disk.scan_usage(tmp_path)
    monitor = disk.UsageMonitor(tmp_path, interval=3600)
    with monitor:
        thread = monitor._thread
        assert thread.daemon and thread.is_alive()
        candidate = work / "candidate"
        candidate.write_bytes(b"candidate" * 4096)
        staged = output / "staged"
        staged.hardlink_to(candidate)
        peak = monitor.sample("before-cleanup")
        candidate.unlink()
        staged.unlink()
        assert monitor.sample("after-cleanup") == initial
        snapshot = monitor.report()
        assert snapshot["final"] is None
        snapshot["checkpoints"].clear()
        snapshot["errors"].append("injected")
    report = monitor.report()
    assert not thread.is_alive()
    assert report["initial"] == report["final"] == report["last"] == asdict(initial)
    assert report["observed_peak"] == asdict(peak)
    assert report["delta_peak_logical"] == len(b"candidate" * 4096)
    assert report["samples"] == 4 and report["interval_s"] == 3600
    assert [c["label"] for c in report["checkpoints"]] == [
        "initial",
        "before-cleanup",
        "after-cleanup",
        "final",
    ]
    assert report["errors"] == []
    assert str(tmp_path) not in json.dumps(report)
    with pytest.raises(RuntimeError):
        monitor.sample("late")
    with pytest.raises(RuntimeError):
        with monitor:
            pass


def test_monitor_peaks_are_independent_per_metric(tmp_path):
    values = [disk.Usage(10, 512, 1), disk.Usage(50, 1024, 2), disk.Usage(20, 4096, 4)]
    with patch.object(disk, "scan_usage", side_effect=values):
        with disk.UsageMonitor(tmp_path, interval=3600) as monitor:
            monitor.sample("candidate")
    assert monitor.report()["observed_peak"] == asdict(disk.Usage(50, 4096, 4))
    assert monitor.report()["delta_peak_logical"] == 40


def test_monitor_periodic_sampling_and_stop(tmp_path):
    sampled = threading.Event()
    calls = 0

    def scan(root):
        nonlocal calls
        calls += 1
        if calls >= 3:
            sampled.set()
        return disk.Usage(calls, calls * 512, calls)

    with patch.object(disk, "scan_usage", scan):
        with disk.UsageMonitor(tmp_path) as monitor:
            assert sampled.wait(2), "periodic sampling did not run"
        stopped_calls = calls
        report = monitor.report()
    assert not monitor._thread.is_alive()
    assert stopped_calls >= 4 and report["samples"] == stopped_calls
    assert report["final"]["logical_bytes"] == stopped_calls


def test_monitor_concurrent_samples_and_reports_are_serialized(tmp_path):
    gate, active = threading.Barrier(9), threading.Lock()
    calls = 0

    def scan(root):
        nonlocal calls
        assert active.acquire(blocking=False), "overlapping scans"
        try:
            calls += 1
            return disk.Usage(calls, calls * 512, calls)
        finally:
            active.release()

    def sample_many():
        gate.wait(timeout=3)
        for _ in range(8):
            monitor.sample("phase")
            report = monitor.report()
            assert report["last"]["files"] == report["samples"]

    with patch.object(disk, "scan_usage", scan):
        with disk.UsageMonitor(tmp_path, interval=0.001) as monitor:
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(sample_many) for _ in range(8)]
                gate.wait(timeout=3)
                for future in futures:
                    future.result(timeout=3)
    report = monitor.report()
    assert len(report["checkpoints"]) == 66
    assert report["samples"] == calls >= 66
    assert report["observed_peak"] == report["final"] == report["last"]
    assert not monitor._thread.is_alive()


@pytest.mark.parametrize("failure_at", ["initial", "phase", "poll", "final"])
def test_monitor_failures_are_sanitized_and_cannot_pass_acceptance(tmp_path, failure_at):
    failed = threading.Event()
    calls = 0
    monitor = disk.UsageMonitor(tmp_path, interval=0.001 if failure_at == "poll" else 3600)

    def scan(root):
        nonlocal calls
        calls += 1
        if calls == (1 if failure_at == "initial" else 2):
            failed.set()
            raise PermissionError("/private/customer-name/secret-token")
        return disk.Usage(10, 512, 1)

    with patch.object(disk, "scan_usage", scan):
        with pytest.raises(disk.UsageError, match="^PermissionError$") as failure:
            with monitor:
                if failure_at == "phase":
                    # Even a caller catching a failed phase must fail on context exit.
                    with pytest.raises(disk.UsageError):
                        monitor.sample("phase")
                elif failure_at == "poll":
                    assert failed.wait(2), "polling failure did not occur"
    report = monitor.report()
    assert report["errors"] == ["PermissionError"]
    assert "secret-token" not in json.dumps(report) + str(failure.value)
    assert monitor._thread is None or not monitor._thread.is_alive()
    if failure_at in {"initial", "final"}:
        assert report[failure_at] is None
    if failure_at == "phase":
        assert report["checkpoints"][1] == {"label": "phase", "usage": None}


def test_monitor_preserves_body_exception_and_reports_final_failure(tmp_path):
    original = ValueError("business failure")
    with patch.object(disk, "scan_usage", side_effect=[disk.Usage(), OSError("private")]):
        with pytest.raises(ValueError) as failure:
            with disk.UsageMonitor(tmp_path, interval=3600) as monitor:
                raise original
    assert failure.value is original
    assert monitor.report()["errors"] == ["OSError"]
    assert not monitor._thread.is_alive()


@pytest.mark.parametrize("interval", [0, -1, float("inf"), float("nan")])
def test_monitor_rejects_invalid_interval(tmp_path, interval):
    with pytest.raises(ValueError):
        disk.UsageMonitor(tmp_path, interval=interval)


def test_rss_units_and_standalone_import_without_site_packages():
    assert disk.rss_bytes(123, "darwin") == 123
    assert disk.rss_bytes(123, "linux") == 123 * 1024
    subprocess.run(
        [sys.executable, "-S", "-c", "from scripts.acceptance_usage import UsageMonitor"],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        timeout=10,
    )
