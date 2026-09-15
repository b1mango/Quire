"""Produce a user-inspectable fixture PDF through the packaged CLI."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.mock_site.server import serve  # noqa: E402

with serve() as site:
    output = ROOT / "output" / "smoke"
    output.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(ROOT / "dist" / "quire.pyz"),
            "manga",
            site.url + "/partial",
            "-o",
            str(output / "quire-sample.pdf"),
            "--selector",
            "main",
            "--rate",
            "1000",
            "--retries",
            "0",
            "--overwrite",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 4:
        raise RuntimeError(result.stdout + result.stderr)
    report = json.loads((output / "quire-sample.report.json").read_text())
    assert report["pages"] == 3 and report["images"] == 2 and report["missing"] == 1
    sys.stdout.write(result.stdout)
