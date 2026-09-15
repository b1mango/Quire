"""Offline process-kill and cache recovery smoke test; no network or real images."""

from __future__ import annotations

import argparse
import json
import selectors
import subprocess
import sys
import tempfile
from pathlib import Path

from quire.store.ledger import Ledger
from quire.store.models import ResourceSpec
from quire.workspace import write_bytes

SOURCE = "https://example.test/ledger-smoke"
RESOURCES = tuple(ResourceSpec(1, n, f"https://example.test/{n}.jpg") for n in range(1, 4))


def _write(root: Path, task_id: str, page: int) -> str:
    relative = f"cache/{task_id}/{page}.bin"
    write_bytes(root / relative, f"resource-{page}".encode(), overwrite=True)
    return relative


def _worker(root: Path) -> None:
    with Ledger(root) as ledger:
        task_id = ledger.create_task(SOURCE, {}, RESOURCES)
        ledger.start(task_id)
        ledger.claim(task_id, 1, 1)
        ledger.complete(task_id, 1, 1, _write(root, task_id, 1))
        ledger.claim(task_id, 1, 2)
        _write(root, task_id, 2)
        sys.stdout.write(task_id + "\n")
        sys.stdout.flush()
        sys.stdin.read(1)


def run_smoke(root: Path) -> dict[str, object]:
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--worker", str(root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            if not selector.select(timeout=15):
                raise RuntimeError("Ledger worker did not become ready")
        task_id = process.stdout.readline().strip()
        if len(task_id) != 64:
            raise RuntimeError("Ledger worker failed before its commit")
        process.kill()
        process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
    with Ledger(root) as ledger:
        before = ledger.snapshot(task_id)
        assert [r.status for r in before.resources] == ["done", "downloading", "pending"]
        restored = ledger.recover(task_id)
        assert [r.status for r in restored.resources] == ["done", "pending", "pending"]
        ledger.start(task_id)
        reused = sum(r.status == "done" for r in restored.resources)
        for resource in restored.resources:
            if resource.status == "pending":
                page = resource.spec.page
                ledger.claim(task_id, 1, page)
                ledger.complete(task_id, 1, page, _write(root, task_id, page))
        assert ledger.finish(task_id).status == "done"
        (root / restored.resources[0].local_path).write_bytes(b"corrupted!")  # type: ignore[arg-type]
        repaired = ledger.recover(task_id)
        assert [r.status for r in repaired.resources] == ["pending", "done", "done"]
    return {
        "killed_exit_code": process.returncode,
        "reused_after_kill": reused,
        "retried_after_kill": 2,
        "invalidated_after_corruption": 1,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", type=Path)
    args = parser.parse_args()
    if args.worker:
        _worker(args.worker)
    else:
        output = Path(__file__).resolve().parents[1] / "output" / "ledger-smoke"
        output.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="run-", dir=output) as directory:
            report = run_smoke(Path(directory))
        payload = json.dumps(report, indent=2) + "\n"
        (output / "report.json").write_text(payload)
        sys.stdout.write(payload)
