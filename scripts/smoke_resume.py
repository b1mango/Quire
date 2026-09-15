"""Exercise real CLI process death and cache repair using only loopback HTTP."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.mock_site.server import Handler, MockSite  # noqa: E402

IMAGE_PATHS = tuple(f"/images/{page}.jpg" for page in range(1, 4))
PROCESS_TIMEOUT = 30


class ResumeHandler(Handler):
    server: ResumeSite

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == IMAGE_PATHS[1]:
            with self.server.lock:
                block = not self.server.blocked.is_set()
                if block:
                    self.server.counts[path] += 1
                    self.server.blocked.set()
            if block:
                # The parent kills the waiting client before releasing this handler.
                self.server.release.wait(PROCESS_TIMEOUT)
                return
        super().do_GET()


class ResumeSite(MockSite):
    daemon_threads = False

    def __init__(self) -> None:
        super().__init__()
        self.RequestHandlerClass = ResumeHandler
        self.blocked = threading.Event()
        self.release = threading.Event()

    def snapshot_counts(self) -> dict[str, int]:
        with self.lock:
            return {path: self.counts[path] for path in sorted(set(self.counts) | set(IMAGE_PATHS))}


def _snapshot(root: Path) -> list[sqlite3.Row]:
    uri = (root / "ledger.db").resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=2)) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            "SELECT task_id, page, status, attempts, local_path, size, sha256 "
            "FROM resources ORDER BY chapter, page"
        ).fetchall()


def _command(site: ResumeSite, root: Path, output: Path, *, resume: bool) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "quire",
        "manga",
        site.url + "/comic",
        "--core",
        "--workdir",
        str(root),
        "--keep-images",
        "--rate",
        "100",
        "--concurrency",
        "1",
        "--retries",
        "0",
        "--selector",
        "main.reader",
        "--output",
        str(output),
        "--quiet",
    ]
    if resume:
        command.append("--resume")
    return command


def _environment() -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.lower().endswith("_proxy")
    }
    environment.update(
        PYTHONPATH=str(ROOT / "src"), NO_PROXY="*", no_proxy="*", PYTHONUNBUFFERED="1"
    )
    return environment


def _interrupt(site: ResumeSite, root: Path, output: Path) -> dict[str, object]:
    with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as log:
        process = subprocess.Popen(
            _command(site, root, output, resume=False),
            cwd=ROOT,
            env=_environment(),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            assert site.blocked.wait(15), "CLI did not reach the blocked second image"
            assert process.poll() is None, "CLI exited before the parent could kill it"
            rows = _snapshot(root)
            assert [row["status"] for row in rows] == ["done", "downloading", "pending"]
            assert [row["attempts"] for row in rows] == [1, 1, 0]
            first = rows[0]
            cached = (root / first["local_path"]).read_bytes()
            assert cached == site.images[IMAGE_PATHS[0]]
            assert len(cached) == first["size"]
            assert hashlib.sha256(cached).hexdigest() == first["sha256"]
            counts = site.snapshot_counts()
            assert [counts[path] for path in IMAGE_PATHS] == [1, 1, 0], counts
            process.kill()
            returncode = process.wait(timeout=10)
            assert returncode == -signal.SIGKILL, returncode
            assert not output.exists(), "Interrupted CLI unexpectedly published a PDF"
            assert not output.with_suffix(".report.json").exists()
            assert [row["status"] for row in _snapshot(root)] == ["done", "downloading", "pending"]
            return {
                "returncode": returncode,
                "waited": process.poll() is not None,
                "task_id": first["task_id"],
                "resource_statuses": [row["status"] for row in rows],
                "http_counts": counts,
            }
        except Exception as exc:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            log.seek(0)
            raise AssertionError(f"Kill checkpoint failed: {exc}\n{log.read()}") from exc
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            site.release.set()


def _resume(
    site: ResumeSite, root: Path, output: Path, task_id: str, reused: int
) -> dict[str, object]:
    before = site.snapshot_counts()
    result = subprocess.run(
        _command(site, root, output, resume=True),
        cwd=ROOT,
        env=_environment(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=PROCESS_TIMEOUT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(output.with_suffix(".report.json").read_text(encoding="utf-8"))
    assert report["task_id"] == task_id
    assert report["resources_reused"] == reused, report
    assert report["status"] == "done" and report["missing"] == 0, report
    assert report["pages"] == report["images"] == 3, report
    with output.open("rb") as stream:
        pages = PdfReader(stream).pages
        page_count = len(pages)
        widths = [int(page["/Resources"]["/XObject"]["/Im0"]["/Width"]) for page in pages]
    assert page_count == 3 and widths == [401, 402, 403], widths
    rows = _snapshot(root)
    assert len(rows) == 3 and all(row["status"] == "done" for row in rows)
    for row, path in zip(rows, IMAGE_PATHS, strict=True):
        cached = (root / row["local_path"]).read_bytes()
        assert cached == site.images[path]
        assert len(cached) == row["size"]
        assert hashlib.sha256(cached).hexdigest() == row["sha256"]
    counts = site.snapshot_counts()
    return {
        "returncode": result.returncode,
        "task_id": report["task_id"],
        "output": output.name,
        "pdf_pages": page_count,
        "image_widths": widths,
        "resources_reused": report["resources_reused"],
        "http_counts": counts,
        "http_delta": {path: counts[path] - before.get(path, 0) for path in counts},
    }


def run_smoke(directory: Path) -> dict[str, object]:
    """Run the kill/resume/repair scenario in a fresh caller-owned directory."""
    root = directory / "work"
    output = directory / "resumed.pdf"
    repaired_output = directory / "repaired.pdf"
    site = ResumeSite()
    thread = threading.Thread(target=site.serve_forever, name="resume-smoke-http")
    thread.start()
    try:
        killed = _interrupt(site, root, output)
        task_id = str(killed["task_id"])
        resumed = _resume(site, root, output, task_id, reused=1)
        assert [resumed["http_delta"][path] for path in IMAGE_PATHS] == [0, 1, 1]
        assert [resumed["http_counts"][path] for path in IMAGE_PATHS] == [1, 2, 1]

        first = _snapshot(root)[0]
        cached_path = root / first["local_path"]
        original = cached_path.read_bytes()
        # Equal length forces recovery to check SHA-256, not just the file size.
        damaged = bytes([original[0] ^ 0xFF]) + original[1:]
        cached_path.write_bytes(damaged)
        assert cached_path.stat().st_size == first["size"]
        assert hashlib.sha256(damaged).hexdigest() != first["sha256"]
        original_pdf = output.read_bytes()
        repaired = _resume(site, root, repaired_output, task_id, reused=2)
        assert [repaired["http_delta"][path] for path in IMAGE_PATHS] == [1, 0, 0]
        assert [repaired["http_counts"][path] for path in IMAGE_PATHS] == [2, 2, 1]
        assert cached_path.read_bytes() == original
        assert output.read_bytes() == original_pdf
        report = {
            "ok": True,
            "task_id": task_id,
            "kill": killed,
            "resume": resumed,
            "repair": repaired,
            "corruption": {"page": 1, "same_size": True, "sha256_changed": True},
        }
    finally:
        site.release.set()
        site.shutdown()
        site.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive(), "HTTP server thread did not stop"
    report["server_closed"] = True
    return report


def main() -> int:
    output = ROOT / "output" / "resume-smoke"
    output.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="run-", dir=output) as directory:
            report = run_smoke(Path(directory))
    except Exception as exc:
        report = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    payload = json.dumps(report, indent=2, ensure_ascii=True) + "\n"
    (output / "report.json").write_text(payload, encoding="utf-8")
    sys.stdout.write(payload)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
