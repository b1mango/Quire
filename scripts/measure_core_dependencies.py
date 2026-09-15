"""Measure the installed core dependency closure from distribution metadata."""

from __future__ import annotations

import json
import sys
import tomllib
from importlib.metadata import distribution
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def measure() -> dict[str, object]:
    project = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    pending = [
        Requirement(item).name for item in project["project"]["optional-dependencies"]["core"]
    ]
    packages = {}
    while pending:
        name = canonicalize_name(pending.pop())
        if name in packages:
            continue
        installed = distribution(name)
        files = [
            installed.locate_file(file)
            for file in installed.files or ()
            if file.suffix != ".pyc" and "__pycache__" not in file.parts
        ]
        packages[name] = {
            "version": installed.version,
            "bytes": sum(path.stat().st_size for path in files if path.is_file()),
        }
        for text in installed.requires or ():
            requirement = Requirement(text)
            if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
                pending.append(requirement.name)
    return {
        "packages": dict(sorted(packages.items())),
        "total_installed_bytes_excluding_pyc": sum(item["bytes"] for item in packages.values()),
        "includes_python_runtime": False,
    }


if __name__ == "__main__":
    result = measure()
    output = Path("output/audit/core-dependencies.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(result, indent=2) + "\n"
    output.write_text(payload)
    sys.stdout.write(payload)
