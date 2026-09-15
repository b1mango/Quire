"""Opt-in public manga acceptance; response replay never accesses the network."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


from scripts.acceptance_capture import write_json  # noqa: E402
from scripts.acceptance_report import assess  # noqa: E402

SITES = {
    "xkcd-1": {"url": "https://xkcd.com/1/", "selector": "#comic"},
    "xkcd-657": {"url": "https://xkcd.com/657/", "selector": "#comic"},
    "xkcd-1732": {"url": "https://xkcd.com/1732/", "selector": "#comic"},
    "books-six": {"url": "https://books.toscrape.com/", "selector": ".product_pod img", "last": 6},
}


def run_case(
    root: Path, label: str, config: dict, *, verify: bool = True, reuse_measurement: bool = False
) -> dict:
    controls = root / "controls"
    controls.mkdir(parents=True, exist_ok=True)
    request, metrics = controls / f"{label}.json", controls / f"{label}.metrics.json"
    if metrics.exists():
        if not reuse_measurement or json.loads(request.read_text()) != config:
            raise FileExistsError(
                f"Metrics already exist for {label}; use --continue or a new run root"
            )
    else:
        write_json(request, config)
        process = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/acceptance_capture.py"),
                str(request),
                str(metrics),
            ],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if process.returncode != 0:
            raise RuntimeError(f"Worker {label} failed: {process.stderr[-2000:]}")
    data = json.loads(metrics.read_text())
    if data["capture_ok"] and verify:
        from scripts.acceptance_verify import verify_artifacts

        source_paths = [
            Path(config["corpus"]) / (name or "missing-source") for name in data["source_files"]
        ]
        try:
            data["verification"] = verify_artifacts(
                Path(data["output"]),
                source_paths,
                lossless=config.get("compression", {}).get("preset") == "lossless",
                max_edge=data["result"]["compression"]["max_edge"],
                expected_missing=config.get("expected_missing", 0),
                comparison_dir=root / "comparisons" / label,
            )
        except (OSError, ValueError, AssertionError) as exc:
            data["verification"] = {"structure_passed": False, "error": str(exc)}
    write_json(metrics, data)
    return data


def repeated_corpus(
    root: Path, count: int = 200, *, missing: bool = False, fullsize: bool = False
) -> Path:
    corpus = root / "corpus" / ("missing" if missing else "fullsize" if fullsize else "repeated")
    inputs = []
    for site in ("xkcd-657",) if fullsize else ("xkcd-1", "xkcd-657", "books-six"):
        source_root = root / "corpus" / site
        for name in json.loads((source_root / "sources.json").read_text()):
            if name:
                inputs.append(source_root / name)
    if not inputs:
        raise ValueError("No captured sources available for repeated workload")
    records, sources, bodies = {}, [], {}

    def add(url, body, status=200):
        digest = hashlib.sha256(body).hexdigest()
        name = f"{digest}.bin"
        bodies[name] = body
        records[url] = {"status": status, "headers": {}, "body": name, "sha256": digest}
        return name

    add("https://acceptance.test/robots.txt", b"User-agent: *\nAllow: /\n")
    html = ["<h1>Quire repeated public samples / local workload</h1>"]
    for index in range(count):
        url = f"https://acceptance.test/{index + 1}.png"
        sources.append(add(url, inputs[index % len(inputs)].read_bytes()))
        html.append(f'<img src="{url}">')
    add("https://acceptance.test/chapter", "".join(html).encode())
    if missing:
        add("https://acceptance.test/2.png", b"", 404)
    if corpus.exists():
        if (
            json.loads((corpus / "responses.json").read_text()) != records
            or json.loads((corpus / "sources.json").read_text()) != sources
            or any((corpus / name).read_bytes() != body for name, body in bodies.items())
        ):
            raise ValueError("Existing workload corpus differs; use a new output root")
        return corpus
    corpus.mkdir(parents=True)
    for name, body in bodies.items():
        (corpus / name).write_bytes(body)
    write_json(corpus / "responses.json", records)
    write_json(corpus / "sources.json", sources)
    return corpus


def run(root: Path, phase: str, *, reuse_measurement: bool = False) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    report_path = root / "report.json"
    report = (
        json.loads(report_path.read_text())
        if report_path.exists()
        else {
            "created_utc": datetime.now(UTC).isoformat(),
            "cases": {},
            "sources": {
                "xkcd": "Randall Munroe, https://xkcd.com/license.html, CC BY-NC 2.5",
                "books": "https://toscrape.com/ scraping sandbox",
            },
            "limits": [
                "Small public sample, not a general website success-rate estimate.",
                "Repeated workload is not 200 distinct manga pages.",
                "Disk values are observed peaks from polling and phase checkpoints.",
                "RSS is worker lifetime high-water mark; verification runs separately.",
                "Original response bodies and books remain local and are not redistributed.",
            ],
        }
    )

    def record(label, config):
        report["cases"][label] = run_case(
            root, label, config, verify=phase != "live", reuse_measurement=reuse_measurement
        )
        write_json(report_path, report)
        result = report["cases"][label]
        if "verification" in result:
            result["acceptance"] = assess(result, config)
            write_json(report_path, report)
        sys.stdout.write(
            json.dumps(
                {
                    "case": label,
                    "capture_ok": result["capture_ok"],
                    "elapsed_s": result["elapsed_s"],
                    "peak_rss_bytes": result["peak_rss_bytes"],
                }
            )
            + "\n"
        )
        sys.stdout.flush()

    if phase == "verify":
        for path in sorted((root / "controls").glob("*.metrics.json")):
            label = path.name.removesuffix(".metrics.json")
            config = json.loads(path.with_name(f"{label}.json").read_text())
            report["cases"][label] = run_case(root, label, config, reuse_measurement=True)
            report["cases"][label]["acceptance"] = assess(report["cases"][label], config)
        write_json(report_path, report)
    elif phase == "live":
        for site, spec in SITES.items():
            record(
                site,
                {
                    **spec,
                    "mode": "live",
                    "directory": str(root / "runs" / site),
                    "corpus": str(root / "corpus" / site),
                    "keep_images": True,
                },
            )
    elif phase == "replay":
        for site, spec in SITES.items():
            corpus = root / "corpus" / site
            if not (corpus / "sources.json").exists():
                continue
            for mode, compression in (
                ("default", {}),
                ("lossless", {"preset": "lossless", "target_bytes": None}),
                ("unreachable", {"target_bytes": 1}),
            ):
                config = {
                    **spec,
                    "mode": "replay",
                    "directory": str(root / "runs" / f"{site}-{mode}"),
                    "corpus": str(corpus),
                    "compression": compression,
                }
                record(f"{site}-{mode}", config)
                if mode == "default":
                    record(
                        f"{site}-artifact-reuse",
                        {**config, "resume": True, "mode": "artifact-reuse"},
                    )
            config = {
                **spec,
                "mode": "resource-reuse",
                "directory": str(root / "runs" / site),
                "corpus": str(corpus),
                "name": "lossless-from-cache",
                "resume": True,
                "keep_images": True,
                "compression": {"preset": "lossless", "target_bytes": None},
            }
            record(f"{site}-resource-reuse", config)
    elif phase == "workload":
        if not reuse_measurement and any(
            (root / "controls" / f"{label}.metrics.json").exists()
            for label in (
                "repeated-default",
                "repeated-unreachable",
                "missing-page",
                "fullsize-default",
            )
        ):
            raise FileExistsError("Workload measurements exist; use --continue or a new run root")
        corpus = repeated_corpus(root)
        for mode, compression in (("default", {}), ("unreachable", {"target_bytes": 1})):
            config = {
                "url": "https://acceptance.test/chapter",
                "mode": "repeated-workload",
                "directory": str(root / "runs" / f"repeated-{mode}"),
                "corpus": str(corpus),
                "compression": compression,
            }
            record(f"repeated-{mode}", config)
        corpus = repeated_corpus(root, 3, missing=True)
        record(
            "missing-page",
            {
                "url": "https://acceptance.test/chapter",
                "mode": "fault-replay",
                "directory": str(root / "runs/missing-page"),
                "corpus": str(corpus),
                "expected_missing": 1,
            },
        )
        corpus = repeated_corpus(root, fullsize=True)
        record(
            "fullsize-default",
            {
                "url": "https://acceptance.test/chapter",
                "mode": "repeated-workload",
                "directory": str(root / "runs/fullsize-default"),
                "corpus": str(corpus),
            },
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--phase", choices=("live", "replay", "workload", "verify"), required=True)
    parser.add_argument(
        "--continue",
        dest="reuse_measurement",
        action="store_true",
        help="Reuse recorded measurements after interruption; never repeat a completed capture",
    )
    args = parser.parse_args()
    report = run(args.output.resolve(), args.phase, reuse_measurement=args.reuse_measurement)
    cases = report["cases"]
    summary = {
        "capture_failed": [name for name, data in cases.items() if not data["capture_ok"]],
        "pending_verification": [name for name, data in cases.items() if "acceptance" not in data],
        "behavior_failed": [
            name
            for name, data in cases.items()
            if "acceptance" in data and not data["acceptance"]["behavior_passed"]
        ],
        "quality_failed": [
            name
            for name, data in cases.items()
            if "acceptance" in data and not data["acceptance"]["checks"]["quality"]
        ],
        "budget_failed": [
            name
            for name, data in cases.items()
            if "acceptance" in data and not all(data["acceptance"]["budgets"].values())
        ],
    }
    report["summary"] = summary
    report["passed"] = not any(summary.values())
    write_json(args.output.resolve() / "report.json", report)
    sys.stdout.write(json.dumps({"passed": report["passed"], **summary}) + "\n")
    if (
        summary["capture_failed"]
        or summary["behavior_failed"]
        or summary["quality_failed"]
        or summary["budget_failed"]
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
