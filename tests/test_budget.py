from __future__ import annotations

import hashlib
import subprocess
import sys
import zipfile
from pathlib import Path

from scripts.build_micro_zipapp import build
from tests.mock_site.server import page_image

ROOT = Path(__file__).resolve().parents[1]


def test_micro_budget_reproducibility_and_isolated_execution(tmp_path):
    archive = tmp_path / "quire.pyz"
    assert build(archive) < 400_000
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    build(archive)
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == digest
    with zipfile.ZipFile(archive) as zipped:
        assert all(name.endswith(".py") for name in zipped.namelist())
    images = tmp_path / "images"
    images.mkdir()
    (images / "1.jpg").write_bytes(page_image(1))
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(archive),
            "local",
            str(images),
            "-o",
            str(tmp_path / "isolated.pdf"),
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "isolated.pdf").read_bytes().startswith(b"%PDF")


def test_runtime_and_frontend_budget():
    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert project["project"]["dependencies"] == []
    assert sum(p.stat().st_size for p in (ROOT / "ui-preview").rglob("*") if p.is_file()) < 200_000
