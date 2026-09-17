"""Reproducible stdlib-only archive; no environment or cache files included."""

from __future__ import annotations

import argparse
import hashlib
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build(destination: Path) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = ROOT / "src"
    entries = {
        p.relative_to(source).as_posix(): p.read_bytes()
        for p in sorted((source / "quire").rglob("*.py"))
        if "store" not in p.relative_to(source / "quire").parts
        and p.relative_to(source).as_posix()
        not in {
            "quire/fetch/session.py",
            "quire/fetch/async_policy.py",
            "quire/fetch/decoding.py",
            "quire/fetch/browser.py",
            "quire/fetch/browser_pdf.py",
            "quire/fetch/browser_process.py",
            "quire/fetch/browser_cdp.py",
            "quire/fetch/browser_network.py",
            "quire/core_manga.py",
            "quire/core_export.py",
            "quire/core_publish.py",
            "quire/core_novel.py",
            "quire/core_chapters.py",
            "quire/novel_options.py",
            "quire/novel_export.py",
            "quire/cli_novel.py",
            "quire/text/clean.py",
            "quire/parse/article.py",
            "quire/parse/chapters.py",
            "quire/assemble/txt.py",
            "quire/assemble/epub.py",
            "quire/assemble/novel_html.py",
            "quire/export_commit.py",
            "quire/export_receipt.py",
            "quire/core_pages.py",
            "quire/assemble/models.py",
            "quire/assemble/archive.py",
            "quire/assemble/pdf.py",
            "quire/image/downloader.py",
            "quire/image/codec.py",
            "quire/image/analyze.py",
            "quire/image/compress.py",
        }
    }
    entries["__main__.py"] = b"from quire.cli import main\nraise SystemExit(main())\n"
    staged = destination.with_suffix(".pyz.tmp")
    with zipfile.ZipFile(staged, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(entries.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, content)
    size = staged.stat().st_size
    if size > 400_000:
        staged.unlink()
        raise RuntimeError(f"Micro exceeds 400 KB: {size}")
    staged.replace(destination)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    sys.stdout.write(f"{destination}: {size} bytes | sha256 {digest}\n")
    return size


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "quire.pyz")
    build(parser.parse_args().output)
