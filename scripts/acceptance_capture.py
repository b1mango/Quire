"""Subprocess capture and explicit public-response recording/replay for S1.7."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import resource
import sys
import time
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from quire import core_export, core_manga, core_publish, export_commit  # noqa: E402
from quire.errors import QuireError  # noqa: E402
from quire.fetch.session import AsyncFetcher  # noqa: E402
from quire.image.options import CompressionOptions  # noqa: E402
from quire.manga import report_payload  # noqa: E402
from quire.models import MangaOptions  # noqa: E402
from quire.parse.images import FilterPolicy  # noqa: E402
from quire.store.ledger import Ledger  # noqa: E402
from scripts.acceptance_usage import UsageError, UsageMonitor  # noqa: E402


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


class Body(httpx.AsyncByteStream):
    def __init__(self, data: bytes):
        self.data = data

    async def __aiter__(self):
        yield self.data


class ObservedFetcher(AsyncFetcher):
    def __init__(self, corpus: Path, *, live: bool):
        self.corpus, self.live = corpus, live
        self.records = {} if live else json.loads((corpus / "responses.json").read_text())
        self.requests = []
        super().__init__(
            rate=1 if live else 1e6,
            concurrency=2,
            retries=0,
            timeout=25,
            transport=None if live else httpx.MockTransport(self.replay),
        )

    def replay(self, request: httpx.Request) -> httpx.Response:
        key = str(request.url)
        if key not in self.records:
            raise RuntimeError("Replay requested an unrecorded URL")
        record = self.records[key]
        if "error" in record:
            self.requests.append({"url": key, "error": record["error"]})
            raise RuntimeError(
                "Recording is incomplete: recorded request failure cannot be replayed"
            )
        data = (self.corpus / record["body"]).read_bytes() if record["body"] else b""
        if hashlib.sha256(data).hexdigest() != record["sha256"]:
            raise RuntimeError("Replay body differs from its recorded digest")
        return httpx.Response(
            record["status"], headers=record["headers"], stream=Body(data), request=request
        )

    async def _request(self, url, headers, *, method="GET", content=None):
        started = time.perf_counter()
        try:
            response, location, delay = await super()._request(
                url, headers, method=method, content=content
            )
        except (httpx.HTTPError, QuireError) as exc:
            error = type(exc).__name__
            self.requests.append({"url": str(url), "error": error})
            if self.live:
                self.records[str(url)] = {"error": error}
            raise
        self.requests.append(
            {
                "url": str(url),
                "status": response.status,
                "bytes": len(response.content),
                "elapsed_s": time.perf_counter() - started,
            }
        )
        if self.live:
            digest = hashlib.sha256(response.content).hexdigest()
            body = f"{digest}.bin" if response.content else None
            if body and not (self.corpus / body).exists():
                (self.corpus / body).write_bytes(response.content)
            # Bodies have already been decoded; never replay compressed framing headers.
            public = {
                key: response.headers[key]
                for key in ("content-type", "location", "retry-after")
                if key in response.headers
            }
            self.records[str(url)] = {
                "status": response.status,
                "headers": public,
                "body": body,
                "sha256": digest,
            }
        return response, location, delay


def worker(config: dict) -> dict:
    usage = UsageMonitor(Path(config["directory"]).resolve(), interval=0.02)
    started = time.perf_counter()
    try:
        return _worker(config, usage)
    except UsageError as exc:
        return {
            "mode": config["mode"],
            "capture_ok": False,
            "error": {"type": "UsageError", "message": str(exc)},
            "disk": usage.report(),
            "elapsed_s": time.perf_counter() - started,
            "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            * (1 if sys.platform == "darwin" else 1024),
            "requests": [],
            "calls": {"encoding_trials": None},
        }


def _worker(config: dict, usage: UsageMonitor) -> dict:
    case = Path(config["directory"]).resolve()
    corpus = Path(config["corpus"]).resolve()
    case.mkdir(parents=True, exist_ok=True)
    corpus.mkdir(parents=True, exist_ok=True)
    live = config["mode"] == "live"
    if live and (corpus / "responses.json").exists():
        raise FileExistsError("Public recordings already exist; use a new output root")
    client = ObservedFetcher(corpus, live=live)
    compression = CompressionOptions(**config.get("compression", {}))
    opts = MangaOptions(
        selector=config.get("selector"),
        first=1,
        last=config.get("last", 0),
        order="dom",
        concurrency=2,
        rate=1 if live else 1e6,
        retries=0,
        timeout=25,
        keep_images=config.get("keep_images", False),
        policy=FilterPolicy(min_width=0, min_height=0, min_bytes=0),
    )
    metrics = {
        "mode": config["mode"],
        "compression_options": asdict(compression),
        "calls": {"encoding_trials": 0},
    }
    output = case / f"{config.get('name', 'book')}.pdf"
    task_root = case / "work"
    actual_trial, actual_stage = core_export._trial, export_commit._stage
    actual_prepare, actual_cleanup = core_manga.prepare, core_publish.cleanup
    with usage:

        async def trial(*args, **kwargs):
            metrics["calls"]["encoding_trials"] += 1
            value = await actual_trial(*args, **kwargs)
            usage.sample("trial-complete")
            return value

        async def stage(*args, **kwargs):
            value = await actual_stage(*args, **kwargs)
            usage.sample("staged-file")
            return value

        def prepare(*args, **kwargs):
            value = actual_prepare(*args, **kwargs)
            usage.sample("prepared")
            return value

        def cleanup(*args, **kwargs):
            usage.sample("before-cleanup")
            value = actual_cleanup(*args, **kwargs)
            usage.sample("after-cleanup")
            return value

        started = time.perf_counter()
        try:
            with (
                patch.object(core_export, "_trial", trial),
                patch.object(export_commit, "_stage", stage),
                patch.object(core_manga, "prepare", prepare),
                patch.object(core_publish, "cleanup", cleanup),
            ):
                result = asyncio.run(
                    core_manga.run_core_manga(
                        config["url"],
                        output,
                        options=opts,
                        workdir=task_root,
                        resume=config.get("resume", False),
                        compression=compression,
                        formats=("pdf", "cbz", "zip"),
                        fetcher=client,
                    )
                )
        except (QuireError, OSError, RuntimeError) as exc:
            metrics.update(
                error={"type": type(exc).__name__, "message": str(exc)}, capture_ok=False
            )
        else:
            metrics.update(result=report_payload(result), output=str(output), capture_ok=True)
        metrics["elapsed_s"] = time.perf_counter() - started
        metrics["peak_rss_bytes"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (
            1 if sys.platform == "darwin" else 1024
        )
    metrics.update(disk=usage.report(), requests=client.requests)
    if live:
        write_json(corpus / "responses.json", client.records)
    if metrics["capture_ok"]:
        # Keep verifier input outside measured task directories, even when core cleans its cache.
        if live:
            sources = []
            with Ledger(task_root) as ledger:
                snapshot = ledger.snapshot(result.task_id)
            for record in snapshot.resources:
                if record.local_path is None:
                    sources.append(None)
                    continue
                data = (task_root / record.local_path).read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                name = f"{digest}.bin"
                if not (corpus / name).exists():
                    (corpus / name).write_bytes(data)
                sources.append(name)
            write_json(corpus / "sources.json", sources)
        metrics["source_files"] = json.loads((corpus / "sources.json").read_text())
    return metrics


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("metrics", type=Path)
    args = parser.parse_args()
    write_json(args.metrics, worker(json.loads(args.config.read_text())))


if __name__ == "__main__":
    main()
