"""小说 API/CLI 的成品保护、恢复和截断状态回归。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from quire.cli import main
from quire.core_chapters import TRUNCATION_NOTE
from quire.core_novel import run_core_novel
from quire.errors import ConfigError
from tests.mock_site.novel_server import novel_site
from tests.test_novel import OPTIONS, _epub_text, run


@pytest.fixture
def site():
    with novel_site() as running:
        yield running


@pytest.mark.parametrize("conflict", ["own", "unrelated", "secondary"])
def test_api_resume_with_existing_task_still_requires_overwrite(
    site,
    tmp_path: Path,
    conflict: str,
) -> None:
    work = tmp_path / "work"
    first_out = tmp_path / "book.epub"
    run(site, first_out, work, formats=("epub", "txt"))
    protected = [first_out, first_out.with_suffix(".txt"), first_out.with_suffix(".report.json")]
    out = first_out
    if conflict != "own":
        out = tmp_path / "other.epub"
        existing = out if conflict == "unrelated" else out.with_suffix(".txt")
        existing.write_bytes(b"unrelated user file")
        protected.append(existing)
    before = {path: path.read_bytes() for path in protected}
    hits = site.total_hits()

    with pytest.raises(ConfigError, match="目标已存在"):
        run(site, out, work, resume=True, formats=("epub", "txt"))

    assert site.total_hits() == hits
    assert {path: path.read_bytes() for path in protected} == before
    if conflict != "own":
        assert not out.with_suffix(".report.json").exists()
    if conflict == "secondary":
        assert not out.exists()


@pytest.mark.parametrize("resume", [False, True])
def test_existing_report_is_refused_before_network_or_ledger_creation(
    site,
    tmp_path: Path,
    resume: bool,
) -> None:
    out = tmp_path / "book.epub"
    report = out.with_suffix(".report.json")
    report.write_bytes(b"user report, not a Quire task")
    work = tmp_path / "work"

    with pytest.raises(ConfigError, match="目标已存在"):
        run(site, out, work, resume=resume, formats=("epub", "txt"))

    assert site.total_hits() == 0
    assert report.read_bytes() == b"user report, not a Quire task"
    assert not out.exists() and not out.with_suffix(".txt").exists()
    assert not work.exists()


def test_cli_resume_numbers_all_conflicting_targets_and_reuses_cache(
    site,
    tmp_path: Path,
    capsys,
) -> None:
    out = tmp_path / "book.epub"
    args = [
        "novel",
        site.url + "/",
        "-o",
        str(out),
        "--format",
        "epub,txt",
        "--rate",
        "100",
        "--retries",
        "0",
        "--workdir",
        str(tmp_path / "work"),
    ]
    assert main(args) == 4
    capsys.readouterr()
    original_report = json.loads(out.with_suffix(".report.json").read_text("utf-8"))
    reserved = [tmp_path / "book (1).report.json", tmp_path / "book (2).txt"]
    for path in reserved:
        path.write_bytes(b"reserved user file")
    protected = [out, out.with_suffix(".txt"), out.with_suffix(".report.json"), *reserved]
    before = {path: path.read_bytes() for path in protected}
    chapter_paths = [f"/book/{index}.html" for index in (1, 2, 3, 5)]
    chapter_paths.extend(["/book/2_2.html", "/book/2_3.html"])
    hits = {path: site.hits(path) for path in chapter_paths}
    missing_hits = site.hits("/book/4.html")

    assert main([*args, "--resume"]) == 4

    numbered = tmp_path / "book (3).epub"
    assert numbered.exists() and numbered.with_suffix(".txt").exists()
    report = json.loads(numbered.with_suffix(".report.json").read_text("utf-8"))
    assert report["task_id"] == original_report["task_id"]
    assert report["output"] == numbered.name
    assert report["resources_reused"] == 4 and report["pages_fetched"] == 1
    assert {path: site.hits(path) for path in chapter_paths} == hits
    assert site.hits("/book/4.html") == missing_hits + 1
    assert {path: path.read_bytes() for path in protected} == before
    assert not (tmp_path / "book (1).epub").exists()
    assert not (tmp_path / "book (2).epub").exists()
    printed = capsys.readouterr().out
    assert str(numbered) in printed and "复用 4 章" in printed


def test_legacy_novel_candidates_cannot_follow_or_remove_symlinks(site, tmp_path: Path) -> None:
    work = tmp_path / "work"
    first = run(site, tmp_path / "first.epub", work)
    assert first.task_id is not None
    legacy = work / "novel" / first.task_id
    legacy.mkdir(parents=True)
    links = {}
    for suffix in ("epub", "txt", "report.json"):
        target = tmp_path / f"precious.{suffix}"
        target.write_bytes(f"user original {suffix}".encode())
        link = legacy / f"book.{suffix}"
        link.symlink_to(target)
        links[link] = (target, link.lstat().st_ino, target.read_bytes(), target.stat().st_mtime_ns)

    second = run(site, tmp_path / "copy.epub", work, resume=True, formats=("epub", "txt"))

    assert second.task_id == first.task_id and second.resources_reused == 4
    assert second.chapters_written == 4
    assert all(artifact.path.exists() for artifact in second.artifacts)
    assert set(legacy.iterdir()) == set(links)
    for link, (target, inode, content, modified) in links.items():
        assert link.is_symlink() and link.readlink() == target
        assert link.lstat().st_ino == inode
        assert target.read_bytes() == content and target.stat().st_mtime_ns == modified


def test_keep_html_refetch_uses_new_directory_and_preserves_previous_dump(
    site,
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    options = replace(OPTIONS, keep_html=True)
    first = run(site, tmp_path / "first.epub", work, options=options)
    assert first.html_dir is not None and first.task_id is not None
    old_files = {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in first.html_dir.iterdir()
    }
    assert len(old_files) == 6
    (work / "cache" / first.task_id / "00002.json").write_bytes(b"{broken cache")
    paths = [f"/book/{index}.html" for index in (1, 2, 3, 4, 5)]
    paths.extend(["/book/2_2.html", "/book/2_3.html"])
    before = {path: site.hits(path) for path in paths}

    second = run(site, tmp_path / "second.epub", work, options=options, resume=True)

    assert second.task_id == first.task_id
    assert second.chapters_written == 4 and second.resources_reused == 3
    assert second.pages_fetched == 4
    assert second.html_dir is not None and second.html_dir != first.html_dir
    assert {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns) for path in first.html_dir.iterdir()
    } == old_files
    new_files = {path.name: path.read_bytes() for path in second.html_dir.iterdir()}
    assert new_files == {
        f"00002-{page:03d}.html": old_files[f"00002-{page:03d}.html"][0] for page in range(1, 4)
    }
    refetched = {"/book/2.html", "/book/2_2.html", "/book/2_3.html", "/book/4.html"}
    assert {path: site.hits(path) - before[path] for path in paths} == {
        path: int(path in refetched) for path in paths
    }


def test_max_pages_partial_survives_cache_reuse_in_results_reports_and_artifacts(
    site,
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    options = replace(OPTIONS, max_pages=2)
    task_id = None
    for index, filename in enumerate(("first.epub", "cached.epub")):
        out = tmp_path / filename
        result = asyncio.run(
            run_core_novel(
                site.url + "/book/2.html",
                out,
                options=options,
                workdir=work,
                formats=("epub", "txt"),
                resume=bool(index),
            )
        )
        assert result.partial and not result.failures and result.chapters_failed == 0
        assert result.chapters_written == 1 and result.chapters[0].truncated
        assert result.chapters[0].pages == 2
        assert result.resources_reused == index
        assert result.pages_fetched == (1 if index == 0 else 0)
        if index:
            assert result.task_id == task_id
        task_id = result.task_id
        assert result.report is not None
        report = json.loads(result.report.read_text("utf-8"))
        assert report["status"] == "partial" and report["missing"] == 0
        assert report["chapter_list"][0]["truncated"] is True
        assert report["chapter_list"][0]["pages"] == 2
        for body in (_epub_text(out), out.with_suffix(".txt").read_text("utf-8")):
            assert TRUNCATION_NOTE in body and "第2章第2页第6段" in body
            assert "第2章第3页" not in body
    assert site.hits("/book/2_2.html") == 1
    assert site.hits("/book/2_3.html") == 0


@pytest.mark.parametrize(
    ("flag", "path", "written"),
    [("--max-chapters", "/", 2), ("--max-pages", "/book/2.html", 1)],
)
def test_cli_truncation_without_failed_chapters_exits_four(
    site,
    tmp_path: Path,
    capsys,
    flag: str,
    path: str,
    written: int,
) -> None:
    out = tmp_path / "limited.txt"
    code = main(
        [
            "novel",
            site.url + path,
            "-o",
            str(out),
            "--format",
            "txt",
            flag,
            "2",
            "--rate",
            "100",
            "--retries",
            "0",
            "--workdir",
            str(tmp_path / "work"),
        ]
    )
    assert code == 4 and out.exists()
    report = json.loads(out.with_suffix(".report.json").read_text("utf-8"))
    assert report["status"] == "partial" and report["written"] == written
    assert report["missing"] == 0 and report["failures"] == []
    assert report["warnings"] and "章缺失" not in capsys.readouterr().out
    if flag == "--max-chapters":
        assert report["truncated"] is True
        assert all(site.hits(f"/book/{index}.html") == 0 for index in (3, 4, 5))
    else:
        assert report["chapter_list"][0]["truncated"] is True
        assert TRUNCATION_NOTE in out.read_text("utf-8")
        assert site.hits("/book/2_3.html") == 0
