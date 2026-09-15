"""Real HTTP and SIGKILL recovery through the public CLI in fresh processes."""

from __future__ import annotations

from pathlib import Path

from scripts.smoke_resume import IMAGE_PATHS, run_smoke


def test_killed_cli_resumes_then_repairs_only_corrupt_cache(tmp_path: Path) -> None:
    report = run_smoke(tmp_path)

    assert report["ok"] and report["server_closed"]
    assert report["kill"]["waited"]
    assert report["kill"]["resource_statuses"] == ["done", "downloading", "pending"]
    assert [report["kill"]["http_counts"][path] for path in IMAGE_PATHS] == [1, 1, 0]
    assert report["resume"]["task_id"] == report["repair"]["task_id"] == report["task_id"]
    assert report["resume"]["pdf_pages"] == report["repair"]["pdf_pages"] == 3
    assert report["resume"]["resources_reused"] == 1
    assert report["repair"]["resources_reused"] == 2
    assert [report["resume"]["http_counts"][path] for path in IMAGE_PATHS] == [1, 2, 1]
    assert [report["repair"]["http_delta"][path] for path in IMAGE_PATHS] == [1, 0, 0]
    assert report["resume"]["output"] != report["repair"]["output"]
    assert report["corruption"] == {"page": 1, "same_size": True, "sha256_changed": True}
