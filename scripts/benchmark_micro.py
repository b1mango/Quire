"""Measure the packaged process on a known 200-page loopback fixture."""

from __future__ import annotations

import json
import resource
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.mock_site.server import serve  # noqa: E402

with serve() as site:
    output = ROOT / "output" / "benchmark"
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(ROOT / "dist" / "quire.pyz"),
            "manga",
            site.url + "/benchmark",
            "-o",
            str(output / "200-pages.pdf"),
            "--selector",
            "main",
            "--order",
            "dom",
            "--overwrite",
            "--quiet",
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    elapsed = time.monotonic() - started
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    report = json.loads((output / "200-pages.report.json").read_text())
    assert report["images"] == 200 and report["missing"] == 0
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    measurements = {
        "environment": "loopback HTTP, synthetic JPEGs, no compression",
        "pages": 200,
        "rate_requests_per_second": 4,
        "workers": 4,
        "elapsed_seconds": round(elapsed, 3),
        "peak_rss_bytes": int(usage.ru_maxrss),
        "output_bytes": report["bytes"],
        "missing_pages": report["missing"],
        "http_requests": sum(site.counts.values()),
    }
    (output / "measurements.json").write_text(json.dumps(measurements, indent=2) + "\n")
    sys.stdout.write(json.dumps(measurements, indent=2) + "\n")
