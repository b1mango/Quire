"""Acceptance decisions from independent fixture expectations and measurements."""

from __future__ import annotations

from urllib.parse import urlsplit


def assess(data: dict, config: dict) -> dict:
    result = data.get("result", {})
    verification = data.get("verification", {})
    compression = result.get("compression", {})
    mode = config["mode"]
    if mode == "repeated-workload":
        expected_sources = 200
    elif mode == "fault-replay":
        expected_sources = 3
    else:
        expected_sources = {
            "https://xkcd.com/1/": 1,
            "https://xkcd.com/657/": 1,
            "https://xkcd.com/1732/": 1,
            "https://books.toscrape.com/": 6,
        }[config["url"]]
    checks = {
        "capture": data["capture_ok"],
        "disk_sampling": not data["disk"]["errors"],
        "expected_missing": result.get("missing") == config.get("expected_missing", 0),
        "source_count": result.get("source_resources")
        == len(data.get("source_files", []))
        == verification.get("source_count")
        == expected_sources,
        "page_count": result.get("pages") == verification.get("page_count")
        and result.get("pages", 0) > 0,
        "structure": verification.get("structure_passed", False),
        "quality": verification.get("quality_passed", False),
    }
    if data["capture_ok"]:
        expected_target = config.get("compression", {}).get("target_bytes", 50_000_000)
        artifacts = result["artifacts"]
        measured = verification.get("artifact_bytes", {})
        target_met = (
            None
            if expected_target is None
            else all(size <= expected_target for size in measured.values())
        )
        checks["target"] = (
            set(measured) == {"pdf", "cbz", "zip"}
            and len(artifacts) == 3
            and {item["format"] for item in artifacts} == set(measured)
            and all(
                item["bytes"] == measured[item["format"]]
                and item["target_met"]
                is (None if expected_target is None else item["bytes"] <= expected_target)
                for item in artifacts
            )
            and compression["target_bytes"] == expected_target
            and compression["target_met"] is target_met
        )
        if mode in ("artifact-reuse", "resource-reuse"):
            checks["no_image_requests"] = all(
                request["url"] == config["url"] or urlsplit(request["url"]).path == "/robots.txt"
                for request in data["requests"]
            )
        if mode == "artifact-reuse":
            checks["reuse"] = result["artifacts_reused"] and data["calls"]["encoding_trials"] == 0
        elif mode == "resource-reuse":
            checks["reuse"] = (
                result["resources_reused"] == result["source_resources"]
                and not result["artifacts_reused"]
                and data["calls"]["encoding_trials"] >= 1
            )
        else:
            checks["fresh"] = (
                result["resources_reused"] == 0
                and not result["artifacts_reused"]
                and data["calls"]["encoding_trials"] >= 1
            )
    budgets = {
        "rss_500mb": data["peak_rss_bytes"] <= 500_000_000,
        "disk_250mb": data["disk"]["observed_peak"] is not None
        and data["disk"]["observed_peak"]["logical_bytes"] <= 250_000_000,
        "elapsed_300s": data["elapsed_s"] <= 300,
    }
    return {
        "checks": checks,
        "budgets": budgets,
        "passed": all(checks.values()) and all(budgets.values()),
        "behavior_passed": all(value for key, value in checks.items() if key != "quality"),
    }
