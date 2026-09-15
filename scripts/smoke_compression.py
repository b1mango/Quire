"""Offline S1.4 fixtures and PDF readback: python scripts/smoke_compression.py.

Requires the existing core/dev extras. PDFKit rendering is a separate review step.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import resource
import subprocess
import sys
import time
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
CASES = {
    "sample": {},
    "unreachable": {"target_bytes": 1},
    "lossless": {"preset": "lossless", "target_bytes": None},
}
LIMITS = {
    "psnr_db_min": 28,
    "mae_max": 5,
    "detail_mae_max": 10,
    "marker_error_max": 12,
    "white_error_max": 2,
}


def save_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def normalized(path: Path):
    from PIL import Image, ImageOps

    with Image.open(path) as raw:
        oriented = ImageOps.exif_transpose(raw).convert("RGBA")
    white = Image.new("RGBA", oriented.size, "white")
    return Image.alpha_composite(white, oriented).convert("RGB")


def draw_page(size: tuple[int, int], number: int, *, gray: bool = False):
    from PIL import Image, ImageDraw, ImageFont

    width, height = size
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    font = lambda n: ImageFont.load_default(size=n)  # noqa: E731
    ink = (24, 24, 24)
    color = (40 + number * 17, 160 - number * 7, 60 + number * 13)
    draw.rectangle((16, 16, 48, 48), fill=color)
    draw.text((64, 16), f"NORTH PIER / {number:02d}", font=font(26), fill=ink)
    top, bottom = 76, height - 220
    middle = (top + bottom) // 2
    for index, (y0, y1) in enumerate(((top, middle - 12), (middle + 12, bottom))):
        draw.rectangle((24, y0, width - 24, y1), fill=(180, 215, 224), outline=ink, width=3)
        horizon = y0 + (y1 - y0) * 2 // 3
        draw.rectangle((28, horizon, width - 28, y1 - 4), fill=(62, 133, 157))
        for x in range(40, width - 50, 64):
            roof = horizon - 30 - (x * 7 % 90)
            draw.rectangle((x, roof, x + 40, horizon), fill=(105, 115, 125), outline=ink)
            draw.rectangle((x + 10, roof + 12, x + 18, roof + 24), fill=(255, 224, 90))
        cx, cy = width * (2 + index) // 5, horizon
        draw.ellipse((cx - 22, cy - 100, cx + 22, cy - 56), fill=(239, 202, 180), outline=ink)
        draw.polygon(
            ((cx - 22, cy - 52), (cx + 22, cy - 52), (cx + 40, cy + 28), (cx - 40, cy + 28)),
            fill=(186, 53, 67),
        )
        draw.line((cx - 10, cy + 28, cx - 18, cy + 62), fill=ink, width=4)
        draw.line((cx + 10, cy + 28, cx + 18, cy + 62), fill=ink, width=4)
        draw.rectangle((44, y0 + 18, min(width - 44, 460), y0 + 80), fill="white", outline=ink)
        words = ("The last ferry leaves at six.", "Then we still have time.")[index]
        draw.text((58, y0 + 36), words, font=font(20), fill=ink)
    y = height - 180
    draw.text((40, y), "KEEP THE LIGHT ON. We will return before dawn.", font=font(14), fill=ink)
    draw.text(
        (40, y + 24), "Small print: 0123456789 / Il1 / O0 / North pier.", font=font(12), fill=ink
    )
    for i in range(8):
        draw.line((40, y + 56 + i * 5, width // 2, y + 56 + i * 5), fill=ink, width=1)
    for yy in range(y + 52, y + 106, 6):
        for xx in range(width // 2 + 20, width - 40, 6):
            draw.ellipse((xx, yy, xx + 1, yy + 1), fill=ink)
    for i, shade in enumerate(((215, 45, 67), (30, 165, 120), (38, 89, 205), (128, 128, 128))):
        draw.rectangle((40 + i * 100, height - 54, 120 + i * 100, height - 20), fill=shade)
    return image.convert("L") if gray else image


def generate_sources(root: Path) -> list[dict]:
    from PIL import Image, ImageDraw

    directory = root / "sources"
    directory.mkdir(parents=True, exist_ok=True)
    specs = []
    definitions = [
        ("01-color.png", (1440, 2160)),
        ("02-color.jpg", (960, 1440)),
        ("03-mono.png", (900, 1200)),
        ("04-bitonal.png", (900, 1200)),
        ("05-alpha.png", (800, 1000)),
        ("06-exif.jpg", (800, 1000)),
    ]
    for number, (name, size) in enumerate(definitions, 1):
        page = draw_page(size, number, gray=number in (3, 4))
        kwargs = {"quality": 94, "subsampling": 0} if name.endswith("jpg") else {}
        if number == 4:
            page = page.convert("1", dither=Image.Dither.NONE)
        elif number == 5:
            page = page.convert("RGBA")
            draw = ImageDraw.Draw(page)
            draw.rectangle((0, 0, 12, 12), fill=(240, 0, 200, 0))
            draw.rectangle((600, 20, 700, 55), fill=(240, 40, 80, 128))
        elif number == 6:
            page = page.transpose(Image.Transpose.ROTATE_90)
            exif = Image.Exif()
            exif[274] = 6
            kwargs["exif"] = exif
        page.save(directory / name, **kwargs)
        page.close()
        specs.append({"file": name, "size": list(size), "boxes": [[0, 0, *size]]})
    strip = Image.new("RGB", (600, 5400), "white")
    boxes = []
    for i, top in enumerate(range(0, 5400, 1200)):
        height = min(1200, 5400 - top)
        with draw_page((600, height), 7 + i) as panel:
            strip.paste(panel, (0, top))
        boxes.append([0, top, 600, top + height])
    strip.save(directory / "07-strip.png")
    strip.close()
    specs.append({"file": "07-strip.png", "size": [600, 5400], "boxes": boxes})
    for spec in specs:
        path = directory / spec["file"]
        with Image.open(path) as raw:
            spec.update(
                stored_size=list(raw.size),
                mode=raw.mode,
                exif_orientation=raw.getexif().get(274, 1),
            )
        spec.update(sha256=hashlib.sha256(path.read_bytes()).hexdigest(), bytes=path.stat().st_size)
    save_json(root / "manifest.json", specs)
    return specs


def rss_bytes(raw: int, platform: str = sys.platform) -> int:
    return raw if platform == "darwin" else raw * 1024


def worker(root: Path, case: str) -> None:
    import httpx

    from quire.core_manga import run_core_manga
    from quire.fetch.session import AsyncFetcher
    from quire.image.options import CompressionOptions
    from quire.models import MangaOptions

    specs = json.loads((root / "manifest.json").read_text())
    fixture_id = hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest()
    requests = []

    class Stream(httpx.AsyncByteStream):
        def __init__(self, data):
            self.data = data

        async def __aiter__(self):
            yield self.data

    def handle(request):
        requests.append(request.url.path)
        if request.url.host != "compression.test":
            raise RuntimeError("Unexpected mock host")
        if request.url.path == "/robots.txt":
            data = b"User-agent: *\nAllow: /\n"
        elif request.url.path == "/chapter":
            data = (
                "<h1>North Pier</h1>" + "".join(f'<img src="/{s["file"]}">' for s in specs)
            ).encode()
        elif request.url.path[1:] in {s["file"] for s in specs}:
            data = (root / "sources" / request.url.path[1:]).read_bytes()
        else:
            raise RuntimeError("Unexpected mock path")
        return httpx.Response(200, stream=Stream(data), request=request)

    workdir = root / "cache-work" / case
    baseline = rss_bytes(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    started = time.perf_counter()
    result = asyncio.run(
        run_core_manga(
            f"https://compression.test/chapter?fixture={fixture_id}",
            root / f"{case}.pdf",
            options=MangaOptions(overwrite=True, keep_images=case != "unreachable", order="dom"),
            compression=CompressionOptions(**CASES[case]),
            workdir=workdir,
            resume=True,
            fetcher=AsyncFetcher(transport=httpx.MockTransport(handle), rate=1e9, retries=0),
        )
    )
    elapsed = time.perf_counter() - started
    peak = rss_bytes(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    cached = [p for p in (workdir / "cache" / result.task_id).rglob("*") if p.is_file()]
    hashes = sorted(hashlib.sha256(p.read_bytes()).hexdigest() for p in cached)
    save_json(
        root / f"{case}.metrics.json",
        {
            "elapsed_s": elapsed,
            "peak_rss_bytes": peak,
            "before_processing_peak_rss_bytes": baseline,
            "resources_reused": result.resources_reused,
            "requests": requests,
            "task_id": result.task_id,
            "fixture_sha256": fixture_id,
            "cached_sources_verified": hashes == sorted(s["sha256"] for s in specs),
            "bytes": result.bytes_out,
            "source_resources": result.source_resources,
            "pages_written": result.pages_written,
            "failed": result.pages_failed,
            "partial": result.partial,
            "target_bytes": result.target_bytes,
            "target_met": result.target_met,
            "rounds": result.encoding_rounds,
            "quality": result.quality,
            "max_edge": result.max_edge,
            "warnings": list(result.warnings),
            "report": result.report.name,
        },
    )


def pixel_metrics(reference, actual) -> dict:
    from PIL import ImageChops

    if reference.size != actual.size:
        raise ValueError("Metrics require aligned equal-size images")
    with ImageChops.difference(reference.convert("RGB"), actual.convert("RGB")) as diff:
        hist = diff.histogram()
    count = sum(hist)
    mse = sum((i % 256) ** 2 * n for i, n in enumerate(hist)) / count
    return {
        "psnr_db": 10 * math.log10(255**2 / mse) if mse else None,
        "exact": mse == 0,
        "mae": sum((i % 256) * n for i, n in enumerate(hist)) / count,
        "rmse": math.sqrt(mse),
        "max_error": max(i % 256 for i, n in enumerate(hist) if n),
        "fraction_over_16": sum(n for i, n in enumerate(hist) if i % 256 > 16) / count,
    }


def compare_page(reference, actual, directory: Path, stem: str) -> dict:
    from PIL import Image, ImageChops, ImageDraw, ImageEnhance

    aligned = reference.resize(actual.size, Image.Resampling.LANCZOS)
    native = actual.resize(reference.size, Image.Resampling.LANCZOS)
    metrics = pixel_metrics(aligned, actual)
    y = reference.height - 180
    detail_box = (40, y, reference.width - 40, y + 110)
    metrics["detail_native"] = pixel_metrics(reference.crop(detail_box), native.crop(detail_box))
    metrics["native"] = pixel_metrics(reference, native)
    samples = [(32, 32)] + [(80 + i * 100, reference.height - 36) for i in range(4)]
    metrics["colors"] = [
        {
            "xy": [x, y],
            "source": list(reference.getpixel((x, y))),
            "pdf": list(native.getpixel((x, y))),
        }
        for x, y in samples
    ]
    metrics["marker_error"] = max(
        abs(a - b)
        for a, b in zip(metrics["colors"][0]["source"], metrics["colors"][0]["pdf"], strict=True)
    )
    metrics["white_corner_error"] = max(255 - v for v in native.getpixel((4, 4)))
    metrics["passed"] = (metrics["exact"] or metrics["psnr_db"] >= LIMITS["psnr_db_min"]) and (
        metrics["mae"] <= LIMITS["mae_max"]
        and metrics["marker_error"] <= LIMITS["marker_error_max"]
        and metrics["detail_native"]["mae"] <= LIMITS["detail_mae_max"]
        and metrics["white_corner_error"] <= LIMITS["white_error_max"]
    )
    actual.save(directory / f"{stem}-pdf.png")
    crops = [reference.crop(detail_box), native.crop(detail_box)]
    crops.append(ImageEnhance.Brightness(ImageChops.difference(*crops)).enhance(4))
    sheet = Image.new("RGB", (crops[0].width, 3 * (crops[0].height + 24)), "white")
    draw = ImageDraw.Draw(sheet)
    labels = ("SOURCE", "PDF READBACK", "ABS ERROR x4")
    for i, (crop, label) in enumerate(zip(crops, labels, strict=True)):
        top = i * (crop.height + 24)
        draw.text((4, top + 4), label, fill="black")
        sheet.paste(crop, (0, top + 24))
    sheet.save(directory / f"{stem}-detail.png")
    for image in [aligned, native, sheet, *crops]:
        image.close()
    return metrics


def pdf_pixels(obj):
    from PIL import Image

    if obj["/Filter"] == "/DCTDecode":
        with Image.open(BytesIO(obj.get_data())) as decoded:
            return decoded.convert("RGB")
    mode = (
        "1"
        if obj["/BitsPerComponent"] == 1
        else ("L" if obj["/ColorSpace"] == "/DeviceGray" else "RGB")
    )
    return Image.frombytes(mode, (obj["/Width"], obj["/Height"]), obj.get_data()).convert("RGB")


def inspect_pdf(root: Path, case: str, specs: list[dict], run: dict) -> dict:
    from PIL import Image
    from pypdf import PdfReader

    directory = root / "comparisons" / case
    directory.mkdir(parents=True, exist_ok=True)
    reader = PdfReader(root / f"{case}.pdf")
    expected_count = sum(len(s["boxes"]) for s in specs)
    if len(reader.pages) != expected_count:
        raise ValueError(f"{case}: expected {expected_count} pages, got {len(reader.pages)}")
    pages = []
    for spec in specs:
        with normalized(root / "sources" / spec["file"]) as source:
            if list(source.size) != spec["size"]:
                raise ValueError("Source orientation differs from fixture contract")
            for box in spec["boxes"]:
                index = len(pages)
                pdf_page = reader.pages[index]
                objects = pdf_page["/Resources"]["/XObject"]
                if len(objects) != 1:
                    raise ValueError("Expected exactly one image per PDF page")
                obj = objects["/Im0"]
                actual = pdf_pixels(obj)
                with source.crop(box) as reference, actual:
                    expected = reference.copy()
                    if run["max_edge"]:
                        expected.thumbnail(
                            (run["max_edge"], run["max_edge"]), Image.Resampling.LANCZOS
                        )
                    geometry = actual.size == expected.size and all(
                        abs(a - b * 72 / 150) < 0.011
                        for a, b in zip(
                            (float(pdf_page.mediabox.width), float(pdf_page.mediabox.height)),
                            actual.size,
                            strict=True,
                        )
                    )
                    operations = pdf_page.get_contents().operations
                    geometry &= [op for _, op in operations] == [b"q", b"cm", b"Do", b"Q"]
                    if geometry:
                        matrix = [float(v) for v in operations[1][0]]
                        geometry &= all(
                            abs(a - b) < 0.011
                            for a, b in zip(
                                matrix,
                                (actual.width * 0.48, 0, 0, actual.height * 0.48, 0, 0),
                                strict=True,
                            )
                        )
                        geometry &= operations[2][0] == ["/Im0"]
                    expected.close()
                    entry = compare_page(reference, actual, directory, f"page-{index + 1:02d}")
                    entry.update(
                        page=index + 1,
                        source=spec["file"],
                        crop=box,
                        size=list(actual.size),
                        geometry_ok=geometry,
                    )
                    entry["passed"] &= geometry and (case != "lossless" or entry["exact"])
                    if case == "lossless" and spec["file"] == "02-color.jpg":
                        entry["jpeg_bytes_preserved"] = (
                            obj.get_data() == (root / "sources" / spec["file"]).read_bytes()
                        )
                        entry["passed"] &= entry["jpeg_bytes_preserved"]
                    pages.append(entry)
    return {
        "pages": pages,
        "quality_passed": all(p["passed"] for p in pages),
        "structure_passed": all(
            p["geometry_ok"] and p["marker_error"] <= 12 and p["white_corner_error"] <= 2
            for p in pages
        ),
    }


def run_smoke(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    specs = generate_sources(root)
    report = {
        "synthetic_only": True,
        "source_count": len(specs),
        "thresholds": LIMITS,
        "limits": [
            "Not a 200-page real manga test or a 50MB workload benchmark.",
            "English synthetic text only; no OCR or human readability certification.",
            "PSNR compares decoded source pixels after EXIF and white compositing.",
            "Aligned PSNR excludes resize loss; native/detail metrics include it.",
            "Null PSNR with exact=true means identical pixels (infinite PSNR).",
            "Thresholds are smoke alarms, not perceptual quality guarantees.",
            "Unreachable target quality is diagnostic; its detail failures remain "
            "visible and do not fail the target-behavior smoke check.",
            "Lossy PDFs can exceed lossless size for simple PNG art; no size guarantee.",
            "PDF streams decoded by pypdf/Pillow; PDFKit rendering pending separately.",
            "Core PNG pixels use FlateDecode without PNG Predictor, including packed 1-bit.",
            "No ICC, CMYK, animation, real scan noise or website coverage here.",
            "RSS is child lifetime high-water mark including imports and processing; "
            "excludes fixture generation, PDF readback and comparison generation.",
            "Elapsed measures core call including mock fetch/cache/PDF IO; "
            "reruns reuse verified cache. No CPU-only or network timing claim.",
        ],
        "render_commands": [],
        "cases": {},
    }
    for case in CASES:
        subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--output",
                str(root),
                "--worker",
                case,
            ],
            check=True,
            timeout=180,
        )
        run = json.loads((root / f"{case}.metrics.json").read_text())
        quality = inspect_pdf(root, case, specs, run)
        behavior = (
            run["cached_sources_verified"]
            and run["failed"] == 0
            and not run["partial"]
            and run["source_resources"] == len(specs)
            and run["pages_written"] == 11
        )
        if case == "sample":
            behavior &= run["target_bytes"] == 50_000_000 and run["target_met"] is True
        elif case == "unreachable":
            behavior &= (
                run["target_met"] is False
                and run["target_bytes"] == 1
                and 1 <= run["rounds"] <= 3
                and bool(run["warnings"])
            )
        else:
            behavior &= run["target_bytes"] is None and run["target_met"] is None
        passed = (
            behavior
            and quality["structure_passed"]
            and (case == "unreachable" or quality["quality_passed"])
        )
        report["cases"][case] = {**run, **quality, "behavior_ok": behavior, "passed": passed}
        report["render_commands"].append(
            [
                "swift",
                str(ROOT / "scripts/render_pdf.swift"),
                str(root / f"{case}.pdf"),
                str(root / f"{case}-render"),
            ]
        )
    report["passed"] = all(c["passed"] and c["behavior_ok"] for c in report["cases"].values())
    save_json(root / "report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "output/compression-smoke")
    parser.add_argument("--worker", choices=CASES, help=argparse.SUPPRESS)
    args = parser.parse_args()
    root = args.output.resolve()
    if args.worker:
        worker(root, args.worker)
        return 0
    report = run_smoke(root)
    sys.stdout.write(
        json.dumps({"passed": report["passed"], "report": str(root / "report.json")}) + "\n"
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
