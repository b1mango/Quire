"""Enforce the design's per-core-module coverage floor."""

from __future__ import annotations

import json
import sys
from pathlib import Path

report = json.loads(Path(sys.argv[1]).read_text())
prefixes = (
    "src/quire/parse/",
    "src/quire/image/",
    "src/quire/utils/",
    "src/quire/assemble/",
    "src/quire/store/",
    "src/quire/fetch/session.py",
    "src/quire/fetch/async_policy.py",
    "src/quire/fetch/decoding.py",
    "src/quire/core_manga.py",
    "src/quire/core_export.py",
)
failed = []
for path, details in report["files"].items():
    if path.startswith(prefixes) and details["summary"]["percent_covered"] < 90:
        failed.append(f"{path}: {details['summary']['percent_covered']:.1f}%")
if failed:
    raise SystemExit("Core coverage below 90%:\n" + "\n".join(failed))
sys.stdout.write("All core modules have at least 90% statement coverage.\n")
