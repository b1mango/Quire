"""Bounded contact sheets and native-size detail ROIs for artifact readback."""

from __future__ import annotations

from contextlib import ExitStack, closing

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageStat

from scripts.smoke_compression import pixel_metrics


def _detail_boxes(reference) -> list[tuple]:
    width, height = min(384, reference.width), min(192, reference.height)
    candidates = []
    for y in sorted({0, (reference.height - height) // 2, reference.height - height}):
        for x in sorted({0, (reference.width - width) // 2, reference.width - width}):
            box = (x, y, x + width, y + height)
            with reference.crop(box) as crop, crop.convert("L") as gray:
                candidates.append((ImageStat.Stat(gray).var[0], box))
    # Texture-rich native-size regions, spread apart; no fixture-specific markers.
    candidates.sort(reverse=True)
    first = candidates[0][1]
    others = [item for item in candidates if item[1] != first and item[0] >= candidates[0][0] / 4]
    second = (
        max(
            others,
            key=lambda item: (abs(item[1][0] - first[0]) + abs(item[1][1] - first[1]), item[0]),
        )[1]
        if others
        else first
    )
    return [first, second]


def comparisons(reference, actual, native, sheet, slot, directory, stem, entry) -> list[dict]:
    draw = ImageDraw.Draw(sheet)
    for column, (image, label) in enumerate(((reference, "SOURCE ROI"), (actual, "PDF READBACK"))):
        if entry["missing_reason"] is not None and column == 0:
            label = "MISSING SOURCE / PLACEHOLDER"
        draw.text(
            (column * 360 + 8, slot * 284 + 4), f"PAGE {entry['page']} / {label}", fill="black"
        )
        with image.copy() as thumb:
            thumb.thumbnail((344, 252), Image.Resampling.LANCZOS)
            sheet.paste(thumb, (column * 360 + 8, slot * 284 + 24))
    details = []
    for index, box in enumerate(_detail_boxes(reference), 1):
        with ExitStack() as stack:
            left = stack.enter_context(closing(reference.crop(box)))
            right = stack.enter_context(closing(native.crop(box)))
            diff = stack.enter_context(closing(ImageChops.difference(left, right)))
            error = stack.enter_context(closing(ImageEnhance.Brightness(diff).enhance(4)))
            cell = max(220, left.width)
            panel = stack.enter_context(
                closing(Image.new("RGB", (cell * 3, left.height + 24), "white"))
            )
            labels = ("SOURCE", "PDF AT SOURCE SIZE", "ABS ERROR x4")
            if entry["missing_reason"] is not None:
                labels = ("ZIP PLACEHOLDER", "PDF PLACEHOLDER", "ABS ERROR x4")
            for col, (image, label) in enumerate(zip((left, right, error), labels, strict=True)):
                panel.paste(image, (col * cell, 24))
                ImageDraw.Draw(panel).text((col * cell + 4, 4), label, fill="black")
            path = directory / f"{stem}-page-{entry['page']:06d}-detail-{index}.png"
            panel.save(path)
            origin = entry["crop"] or [0, 0]
            source_roi = [value + origin[i % 2] for i, value in enumerate(box)]
            details.append(
                {
                    "file": str(path),
                    "crop_roi": list(box),
                    "source_roi": source_roi,
                    **pixel_metrics(left, right),
                }
            )
    return details
