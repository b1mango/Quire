"""Offline multi-format capture with independently inspected archives and PDF."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZipFile

import httpx
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quire.core_manga import run_core_manga  # noqa: E402
from quire.fetch.session import AsyncFetcher  # noqa: E402
from quire.models import MangaOptions  # noqa: E402


class Body(httpx.AsyncByteStream):
    def __init__(self, data: bytes = b"") -> None:
        self.data = data

    async def __aiter__(self):
        yield self.data


def sample(number: int) -> bytes:
    with Image.new("RGB", (800, 1100), "white") as image, io.BytesIO() as buffer:
        draw = ImageDraw.Draw(image)
        draw.rectangle((40, 40, 760, 1060), outline="#222222", width=3)
        draw.rectangle((80, 180, 720, 880), fill=("#259788" if number == 1 else "#bc4161"))
        draw.text(
            (80, 90),
            f"Quire / Source {number}",
            fill="#222222",
            font=ImageFont.load_default(size=36),
        )
        draw.text(
            (80, 940),
            "0123456789 / fine detail preserved",
            fill="#222222",
            font=ImageFont.load_default(size=22),
        )
        image.save(buffer, "PNG")
        return buffer.getvalue()


def run(directory: Path) -> dict[str, object]:
    directory.mkdir(parents=True, exist_ok=True)
    images = {"/1.png": sample(1), "/3.png": sample(3)}
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(404, stream=Body(), request=request)
        if request.url.path == "/chapter":
            return httpx.Response(
                200,
                stream=Body(
                    '<h1>卷帙 / 测试</h1><img src="/1.png"><img src="/2.png"><img src="/3.png">'.encode()
                ),
                request=request,
            )
        return httpx.Response(
            200 if request.url.path in images else 404,
            stream=Body(images.get(request.url.path, b"")),
            request=request,
        )

    result = asyncio.run(
        run_core_manga(
            "https://formats.test/chapter?token=private",
            directory / "sample.pdf",
            formats=("pdf", "cbz", "zip"),
            resume=True,
            options=MangaOptions(overwrite=True),
            fetcher=AsyncFetcher(transport=httpx.MockTransport(handler), rate=1e9, retries=0),
        )
    )
    pdf = PdfReader(result.output)
    assert len(pdf.pages) == 3 and result.pages_failed == 1
    assert "MISSING PAGE" in pdf.pages[1].extract_text()
    assert pdf.metadata.title == result.title and "卷帙" in result.title and pdf.outline
    assert "private" not in str(pdf.metadata)
    with ZipFile(directory / "sample.zip") as archive, ZipFile(directory / "sample.cbz") as cbz:
        assert archive.testzip() is None and cbz.testzip() is None
        manifest = json.loads(archive.read("manifest.json"))
        assert len(manifest["pages"]) == 3
        assert manifest["pages"][1]["missing_reason"] is not None
        assert ET.fromstring(cbz.read("ComicInfo.xml")).findtext("PageCount") == "3"
        for index, page in enumerate(manifest["pages"]):
            data = archive.read(page["file"])
            assert cbz.read(page["file"]) == data
            assert hashlib.sha256(data).hexdigest() == page["sha256"]
            if index != 1:
                obj = pdf.pages[index]["/Resources"]["/XObject"]["/Im0"]
                assert obj["/Filter"] == "/DCTDecode" and obj.get_data() == data
        assert "private" not in json.dumps(manifest)
    summary = {
        "passed": True,
        "synthetic_only": True,
        "sources": 3,
        "pages": 3,
        "missing": 1,
        "elapsed_s": result.elapsed_s,
        "resources_reused": result.resources_reused,
        "total_bytes": result.total_bytes,
        "requests": requests,
        "artifacts": [
            {"format": a.format, "file": a.path.name, "bytes": a.bytes} for a in result.artifacts
        ],
        "render_command": "swift scripts/render_pdf.swift output/formats-smoke/sample.pdf output/formats-smoke/page",
    }
    (directory / "report.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


if __name__ == "__main__":
    sys.stdout.write(json.dumps(run(ROOT / "output/formats-smoke"), indent=2) + "\n")
