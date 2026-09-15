from __future__ import annotations

import asyncio
import hashlib
import io
import json

import httpx
import pytest
from PIL import Image

from scripts import acceptance_capture as capture
from scripts.acceptance_capture import Body, ObservedFetcher, worker, write_json
from scripts.acceptance_usage import UsageError, UsageMonitor


def corpus(root):
    root.mkdir()
    with Image.new("RGB", (300, 400), "red") as image, io.BytesIO() as stream:
        image.save(stream, "PNG")
        data = stream.getvalue()
    records = {}
    for path, body, status in (
        ("/robots.txt", b"", 404),
        ("/chapter", b'<h1>Fixture</h1><img src="/1.png">', 200),
        ("/1.png", data, 200),
    ):
        digest = hashlib.sha256(body).hexdigest()
        name = f"{digest}.bin"
        (root / name).write_bytes(body)
        records[f"https://acceptance.test{path}"] = {
            "body": name,
            "sha256": digest,
            "status": status,
            "headers": {},
        }
    write_json(root / "responses.json", records)
    write_json(root / "sources.json", [records["https://acceptance.test/1.png"]["body"]])
    return records


def test_worker_separates_capture_cache_and_artifact_reuse(tmp_path):
    source = tmp_path / "corpus"
    corpus(source)
    config = {
        "mode": "replay",
        "directory": str(tmp_path / "case"),
        "corpus": str(source),
        "url": "https://acceptance.test/chapter",
        "keep_images": True,
    }
    first = worker(config)
    assert first["capture_ok"] and first["result"]["missing"] == 0
    assert first["calls"]["encoding_trials"] == 1 and len(first["requests"]) == 3
    assert first["disk"]["initial"]["logical_bytes"] == 0
    assert first["disk"]["observed_peak"]["logical_bytes"] > first["disk"]["final"]["logical_bytes"]
    assert not first["disk"]["errors"]
    resumed = worker({**config, "resume": True, "mode": "artifact-reuse"})
    assert resumed["result"]["artifacts_reused"] and resumed["calls"]["encoding_trials"] == 0
    assert len(resumed["requests"]) == 2
    rebuilt = worker(
        {
            **config,
            "resume": True,
            "name": "lossless",
            "mode": "resource-reuse",
            "compression": {"preset": "lossless", "target_bytes": None},
        }
    )
    assert rebuilt["result"]["resources_reused"] == 1 and not rebuilt["result"]["artifacts_reused"]
    assert rebuilt["calls"]["encoding_trials"] == 1 and len(rebuilt["requests"]) == 2


def test_worker_reports_unrecorded_url_and_never_uses_network(tmp_path):
    source = tmp_path / "corpus"
    corpus(source)
    config = {
        "mode": "replay",
        "directory": str(tmp_path / "case"),
        "corpus": str(source),
        "url": "https://acceptance.test/unknown",
    }
    result = worker(config)
    assert not result["capture_ok"] and "unrecorded" in result["error"]["message"]


def test_replay_rejects_changed_body(tmp_path):
    source = tmp_path / "corpus"
    records = corpus(source)
    record = records["https://acceptance.test/chapter"]
    (source / record["body"]).write_bytes(b"changed")
    fetcher = ObservedFetcher(source, live=False)
    with pytest.raises(RuntimeError, match="digest"):
        fetcher.replay(httpx.Request("GET", "https://acceptance.test/chapter"))


def test_recording_decoded_bodies_excludes_framing_and_private_headers(tmp_path):
    import gzip

    source = tmp_path / "corpus"
    source.mkdir()
    content = b"hello world" * 10
    data = gzip.compress(content)

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404, stream=Body(b""), request=request)
        return httpx.Response(
            200,
            headers={
                "content-encoding": "gzip",
                "content-length": str(len(data)),
                "set-cookie": "secret=yes",
            },
            stream=Body(data),
            request=request,
        )

    fetcher = ObservedFetcher(source, live=True)
    fetcher._transport = httpx.MockTransport(handler)

    async def record():
        async with fetcher:
            return await fetcher.get("https://acceptance.test/chapter")

    assert asyncio.run(record()).content == content
    write_json(source / "responses.json", fetcher.records)
    raw = (source / "responses.json").read_text()
    assert "secret" not in raw and "content-encoding" not in raw and "content-length" not in raw
    replay = ObservedFetcher(source, live=False)

    async def read():
        async with replay:
            return await replay.get("https://acceptance.test/chapter")

    assert asyncio.run(read()).content == content


def test_live_will_not_overwrite_prior_recordings(tmp_path):
    source = tmp_path / "corpus"
    corpus(source)
    before = (source / "responses.json").read_bytes()
    with pytest.raises(FileExistsError):
        worker({"mode": "live", "directory": str(tmp_path / "case"), "corpus": str(source)})
    assert (source / "responses.json").read_bytes() == before


@pytest.mark.parametrize("error", ["ReadTimeout", "FetchError"])
def test_live_records_request_failure_and_replay_rejects_it(tmp_path, monkeypatch, error):
    secret = "private-proxy-credential"
    source = tmp_path / "corpus"
    url = "https://acceptance.test/chapter"

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404, stream=Body(b""), request=request)
        if error == "ReadTimeout":
            raise httpx.ReadTimeout(secret, request=request)
        return httpx.Response(
            200,
            headers={"content-length": "1000"},
            stream=Body(secret.encode()),
            request=request,
        )

    def fetcher(root, *, live):
        client = ObservedFetcher(root, live=live)
        if live:
            client._transport = httpx.MockTransport(handler)
        return client

    async def forbidden_network(*args, **kwargs):
        raise AssertionError("Real network access is forbidden")

    monkeypatch.setattr(capture, "ObservedFetcher", fetcher)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden_network)
    config = {
        "mode": "live",
        "directory": str(tmp_path / "case"),
        "corpus": str(source),
        "url": url,
    }
    recorded = worker(config)
    assert recorded["capture_ok"] is False
    assert recorded["requests"][-1] == {"url": url, "error": error}
    raw = (source / "responses.json").read_text()
    assert json.loads(raw)[url] == {"error": error}
    assert secret not in raw + json.dumps(recorded)

    replayed = worker({**config, "mode": "replay"})
    assert replayed["capture_ok"] is False
    assert replayed["requests"][-1] == {"url": url, "error": error}
    assert replayed["error"] == {
        "type": "RuntimeError",
        "message": "Recording is incomplete: recorded request failure cannot be replayed",
    }
    assert secret not in json.dumps(replayed)
    assert (source / "responses.json").read_text() == raw


def test_worker_returns_invalid_metrics_when_usage_exit_fails(tmp_path, monkeypatch):
    source = tmp_path / "corpus"
    corpus(source)
    exited = []

    class FailedExitMonitor(UsageMonitor):
        def __exit__(self, *args):
            super().__exit__(*args)
            exited.append(self)
            raise UsageError("PermissionError")

        def report(self):
            report = super().report()
            if self in exited:
                report["errors"] = ["PermissionError"]
            return report

    monkeypatch.setattr(capture, "UsageMonitor", FailedExitMonitor)
    result = worker(
        {
            "mode": "replay",
            "directory": str(tmp_path / "case"),
            "corpus": str(source),
            "url": "https://acceptance.test/chapter",
        }
    )
    assert len(exited) == 1
    assert result["capture_ok"] is False
    assert result["error"] == {"type": "UsageError", "message": "PermissionError"}
    assert result["disk"]["errors"] == ["PermissionError"]
    assert result["disk"]["final"] is not None
    assert result["elapsed_s"] > 0 and result["peak_rss_bytes"] > 0
    assert (tmp_path / "case" / "book.pdf").is_file()
    metrics = tmp_path / "metrics.json"
    write_json(metrics, result)
    assert json.loads(metrics.read_text()) == result
