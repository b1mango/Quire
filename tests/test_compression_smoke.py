from __future__ import annotations

import importlib.util
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/smoke_compression.py"
spec = importlib.util.spec_from_file_location("compression_smoke", SCRIPT)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


def test_metrics_detect_error_without_numpy():
    source = Image.new("RGB", (8, 8), (100, 100, 100))
    actual = Image.new("RGB", source.size, (110, 110, 110))
    same = smoke.pixel_metrics(source, source)
    assert same["exact"] and same["psnr_db"] is None
    metrics = smoke.pixel_metrics(source, actual)
    assert metrics["mae"] == 10 and metrics["rmse"] == 10 and metrics["max_error"] == 10
    assert metrics["psnr_db"] == pytest.approx(20 * math.log10(255 / 10))
    with pytest.raises(ValueError, match="equal-size"):
        smoke.pixel_metrics(source, Image.new("RGB", (4, 4)))


@pytest.mark.parametrize("platform,multiplier", [("darwin", 1), ("linux", 1024)])
def test_peak_rss_units(platform, multiplier):
    assert smoke.rss_bytes(1234, platform) == 1234 * multiplier


def test_smoke_rerun_readback_and_cache(tmp_path):
    for attempt in range(2):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--output", str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=180,
        )
        report = json.loads((tmp_path / "report.json").read_text())
        assert result.returncode == 0, report
        assert report["passed"] and report["synthetic_only"] and report["source_count"] == 7
        for case in report["cases"].values():
            assert len(case["pages"]) == 11 and case["cached_sources_verified"]
            assert case["elapsed_s"] > 0 and case["peak_rss_bytes"] > 0
            assert case["resources_reused"] == 0
            assert case["artifacts_reused"] is bool(attempt)
            assert sum(p not in ("/chapter", "/robots.txt") for p in case["requests"]) == (
                0 if attempt else 7
            )
        assert report["cases"]["unreachable"]["target_met"] is False
        assert all(p["exact"] for p in report["cases"]["lossless"]["pages"])
        assert report["cases"]["lossless"]["pages"][1]["jpeg_bytes_preserved"]
        assert len(list((tmp_path / "comparisons").rglob("*-detail.png"))) == 33
        manifest = json.loads((tmp_path / "manifest.json").read_text())
        assert manifest[5]["stored_size"] == [1000, 800]
        assert manifest[5]["exif_orientation"] == 6
