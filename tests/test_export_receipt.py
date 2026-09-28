from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Literal, cast

import pytest

from quire.errors import LedgerError
from quire.export_options import output_paths
from quire.export_receipt import (
    ExportReceipt,
    PublishedFile,
    export_key,
    read_receipt,
    write_receipt,
)
from quire.image.options import CompressionOptions
from quire.models import ArtifactResult, MangaOptions, MangaResult
from quire.parse.images import Rejection
from quire.store.cache import FileStamp
from quire.store.export_files import Stamp, load_record, make_workspace, save_record

FORMATS = ("pdf", "cbz", "zip")
TASK = "b" * 64
SOURCES = tuple(FileStamp(f"cache/{TASK}/{i:04d}.jpg", "f" * 64, i) for i in range(1, 5))
BAD_CANDIDATES = (
    "../book.pdf /pass-1/book.pdf pass-0/book.pdf pass-4/book.pdf pass-1/../book.pdf "
    "pass-1//book.pdf pass-1/./book.pdf pass-1/book.zip pass-1/book.pdf\0 pass-1\\book.pdf"
).split()
BAD_SOURCES = ["/cache/{task}/a.jpg", "cache/other/a.jpg"] + [
    f"cache/{{task}}/{name}"
    for name in "|.|..|../a.jpg|dir/a.jpg|./a.jpg|/a.jpg|A.jpg|a.JPG|a.jpg\0|a\\b.jpg|a%2fb.jpg".split(
        "|"
    )
]


def prepared(root: Path, formats: tuple[str, ...] = FORMATS) -> ExportReceipt:
    output = root / f"book.{formats[0]}"
    key = export_key(TASK, "Book", output, formats, MangaOptions(), CompressionOptions())
    workspace, identity = make_workspace(root, key)
    target = 50 if formats == ("cbz", "zip") else 1000
    artifacts = tuple(
        ArtifactResult(fmt, path, 100 + i, 100 + i <= target)
        for i, (fmt, path) in enumerate(zip(formats, output_paths(output, formats), strict=True))
    )
    old = Stamp(0, "d" * 64)
    files = tuple(
        PublishedFile(a.format, f"pass-2/book.{a.format}", a.path, Stamp(a.bytes, "c" * 64), old)
        for a in artifacts
    )
    report = output.with_suffix(".report.json")
    result = MangaResult(
        output=output,
        pages_written=3,
        pages_failed=1,
        pages_rejected=1,
        bytes_out=artifacts[0].bytes,
        elapsed_s=1.25,
        title="Book",
        warnings=("partial export",),
        rejections=tuple(
            Rejection("https://example.test/a", "filtered", stage)
            for stage in ("size", "url", "duplicate")
        ),
        failures=(("https://example.test/b", "missing"),),
        images_dir=root / "cache" / TASK,
        report=report,
        task_id=TASK,
        resources_reused=2,
        source_resources=4,
        compression="balanced",
        target_bytes=target,
        target_met=all(a.target_met for a in artifacts),
        encoding_rounds=3,
        quality=80,
        max_edge=2000,
        artifacts=artifacts,
    )
    files += (PublishedFile("report", "book.report.json", report, Stamp(500, "e" * 64), None),)
    return ExportReceipt(key, "prepared", workspace, identity, formats, files, result, SOURCES)


def persisted(root: Path) -> tuple[ExportReceipt, dict[str, object]]:
    receipt = prepared(root)
    write_receipt(root, receipt)
    payload = load_record(root, receipt.key)
    assert payload is not None
    return receipt, payload


def object_at(payload: dict[str, object], path: str) -> dict[str, object]:
    value: object = payload
    for name in path.split(".") if path else ():
        value = (
            cast(list[object], value)[int(name)]
            if name.isdigit()
            else cast(dict[str, object], value)[name]
        )
    return cast(dict[str, object], value)


def assert_corrupt(root: Path, receipt: ExportReceipt, payload: dict[str, object]) -> None:
    save_record(root, receipt.key, payload)
    original = (root / "exports" / f"{receipt.key}.json").read_bytes()
    with pytest.raises(LedgerError, match="export receipt"):
        read_receipt(root, receipt.key, root / "book.pdf", FORMATS)
    assert (root / "exports" / f"{receipt.key}.json").read_bytes() == original


@pytest.mark.parametrize(
    ("state", "cleaned"),
    [("building", False), ("prepared", False), ("complete", False), ("complete", True)],
)
@pytest.mark.parametrize("formats", [("pdf",), ("cbz", "zip"), FORMATS])
def test_json_roundtrip(
    tmp_path: Path,
    state: Literal["building", "prepared", "complete"],
    cleaned: bool,
    formats: tuple[str, ...],
) -> None:
    receipt = replace(prepared(tmp_path, formats), state=state, cleaned=cleaned)
    if formats != ("pdf",):
        receipt = replace(receipt, previous=tuple(item.before for item in receipt.files))
    if formats == ("pdf",):
        receipt = replace(receipt, sources=())
    if state == "building":
        receipt = replace(receipt, files=(), result=None, sources=())
    write_receipt(tmp_path, receipt)
    restored = read_receipt(tmp_path, receipt.key, tmp_path / "book", formats)
    assert restored == receipt
    payload = load_record(tmp_path, receipt.key)
    assert payload is not None and payload["schema"] == 1
    if receipt.result is not None:
        assert payload["result"] == json.loads(json.dumps(asdict(receipt.result), default=str))


@pytest.mark.parametrize(
    "field",
    "task title output formats dpi paper keep_images preset target_bytes bitonal split_tall".split(),
)
def test_identity_includes_export_choices(
    tmp_path: Path, field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    opts, compression = MangaOptions(), CompressionOptions()
    baseline = export_key(TASK, "Book", tmp_path / "book.pdf", FORMATS, opts, compression)
    changed = replace(opts, overwrite=True, concurrency=8)
    assert baseline == export_key(TASK, "Book", Path("book.pdf"), FORMATS, changed, compression)
    assert read_receipt(tmp_path, baseline, Path("book.pdf"), FORMATS) is None
    opts = replace(
        opts,
        dpi=300 if field == "dpi" else opts.dpi,
        paper="a4" if field == "paper" else opts.paper,
        keep_images=field == "keep_images",
    )
    compression = replace(
        compression,
        preset="small" if field == "preset" else compression.preset,
        target_bytes=None if field == "target_bytes" else compression.target_bytes,
        bitonal=field != "bitonal",
        split_tall=field != "split_tall",
    )
    key = export_key(
        "other" if field == "task" else TASK,
        "Other" if field == "title" else "Book",
        tmp_path / ("other.pdf" if field == "output" else "book.pdf"),
        ("pdf", "zip", "cbz") if field == "formats" else FORMATS,
        opts,
        compression,
    )
    assert key != baseline


@pytest.mark.parametrize(
    ("field", "value"),
    [
        *[("schema", v) for v in (2, True, 1.0)],
        *[("state", v) for v in ("unknown", cast(object, []))],
        *[("key", v) for v in ("a" * 64, "A" * 64)],
        ("formats", ["pdf", "pdf", "zip"]),
        ("formats", ["zip", "cbz", "pdf"]),
        ("formats", "pdf"),
        ("formats", [False]),
        *[("workspace_identity", [1, v]) for v in (True, -1, 1.0, "1")],
        *[("workspace_identity", v) for v in ([1], "12")],
        *[("files", v) for v in cast(tuple[object, ...], ([], {}))],
        ("result", None),
        *[("cleaned", v) for v in (None, 0, 1, "false", True)],
        *[
            ("sources", v)
            for v in cast(tuple[object, ...], (None, {}, [None], [asdict(SOURCES[0])] * 2))
        ],
        ("sources", [asdict(replace(SOURCES[0], path=f"cache/{TASK}/{i}.png")) for i in range(5)]),
        *[("previous", v) for v in (None, "", [None], [None] * 5)],
        ("previous", {}),
    ],
)
def test_invalid_receipt_fields(tmp_path: Path, field: str, value: object) -> None:
    receipt, payload = persisted(tmp_path)
    payload[field] = value
    assert_corrupt(tmp_path, receipt, payload)


@pytest.mark.parametrize(
    "location",
    "|result|files.0|files.0.stamp|result.rejections.0|result.artifacts.0|sources.0".split("|"),
)
@pytest.mark.parametrize("operation", ["missing", "extra"])
def test_exact_object_fields(tmp_path: Path, location: str, operation: str) -> None:
    receipt, payload = persisted(tmp_path)
    target = object_at(payload, location)
    if operation == "missing":
        target.pop(next(iter(target)))
    else:
        target["unknown"] = None
    assert_corrupt(tmp_path, receipt, payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        *[("pages_written", v) for v in (True, -1, 0)],
        *[("pages_failed", v) for v in (0, 1.5)],
        *[("pages_rejected", v) for v in (0, "1")],
        ("bytes_out", 101),
        *[("elapsed_s", v) for v in (True, -0.1, "1", 10**400)],
        ("title", 1),
        *[("warnings", v) for v in ("warning", [1])],
        ("rejections", [[]]),
        ("rejections", []),
        ("rejections", [{"url": 1, "reason": "bad", "stage": "size"}]),
        ("rejections", [{"url": "a", "reason": "bad", "stage": "unknown"}]),
        *[("failures", v) for v in ([["url"]], [], [["url", "why", "extra"]], [["url", False]])],
        ("images_dir", "cache/task"),
        *[("task_id", v) for v in (1, None, "a" * 63, "A" * 64)],
        ("task_id", "../escape"),
        *[("resources_reused", v) for v in (False, 5)],
        *[("source_resources", v) for v in (-1, 0)],
        ("compression", "unknown"),
        *[("target_bytes", v) for v in (True, 0, 10**12 + 1)],
        *[("target_met", v) for v in (1, False)],
        ("encoding_rounds", 4),
        *[("quality", v) for v in (101, 0)],
        ("max_edge", -1),
        ("artifacts", []),
        ("artifacts_reused", True),
        ("export_recovered", 0),
        ("artifacts.0.format", "html"),
        *[("artifacts.0.bytes", v) for v in (True, -1, 999)],
        ("artifacts.0.target_met", 1),
    ],
)
def test_invalid_result_fields(tmp_path: Path, field: str, value: object) -> None:
    receipt, payload = persisted(tmp_path)
    parent, _, name = f"result.{field}".rpartition(".")
    object_at(payload, parent)[name] = value
    assert_corrupt(tmp_path, receipt, payload)


@pytest.mark.parametrize("stamp_field", ["stamp", "before", "source", "previous"])
@pytest.mark.parametrize(
    "value",
    [
        [],
        *[{"size": v, "sha256": "a" * 64} for v in (-1, True, 1.0)],
        {"size": 0, "sha256": "A" * 64},
        {"size": 0, "sha256": "a" * 63},
        {"size": 0, "sha256": 0},
    ],
)
def test_invalid_stamps(tmp_path: Path, stamp_field: str, value: object) -> None:
    receipt, payload = persisted(tmp_path)
    if stamp_field == "previous":
        payload["previous"] = [value, None, None, None]
    elif stamp_field == "source":
        item = {"path": SOURCES[0].path, **value} if isinstance(value, dict) else value
        payload["sources"] = [item]
    else:
        object_at(payload, "files.0")[stamp_field] = value
    assert_corrupt(tmp_path, receipt, payload)


@pytest.mark.parametrize(
    ("location", "path"),
    [("files.0.candidate", value) for value in BAD_CANDIDATES]
    + [("sources.0.path", value) for value in BAD_SOURCES],
)
def test_relative_paths(tmp_path: Path, location: str, path: str) -> None:
    receipt, payload = persisted(tmp_path)
    parent, _, field = location.rpartition(".")
    object_at(payload, parent)[field] = path.format(task=TASK)
    assert_corrupt(tmp_path, receipt, payload)


@pytest.mark.parametrize(
    "location",
    "workspace files.0.destination result.output result.report result.artifacts.0.path result.images_dir".split(),
)
@pytest.mark.parametrize("escape", ["relative", "parent", "other", "dot", "double", "nul"])
def test_path_escape(tmp_path: Path, location: str, escape: str) -> None:
    receipt, payload = persisted(tmp_path)
    parent, _, field = location.rpartition(".")
    target = object_at(payload, parent)
    original = cast(str, target[field])
    values = {
        "relative": Path(original).name,
        "parent": str(tmp_path / ".." / Path(original).name),
        "other": str(tmp_path / "other" / Path(original).name),
        "dot": f"{tmp_path}/./{Path(original).name}",
        "double": f"{tmp_path}//{Path(original).name}",
        "nul": original + "\0",
    }
    target[field] = values[escape]
    assert_corrupt(tmp_path, receipt, payload)


@pytest.mark.parametrize(
    "name",
    (
        ".quire-export-other-random .quire-export-{key}- .quire-export-{key}-random/child "
        ".quire-export-{key}-random.json .quire-export-{key}-../escape"
    ).split(),
)
def test_workspace_name(tmp_path: Path, name: str) -> None:
    receipt, payload = persisted(tmp_path)
    payload["workspace"] = str(tmp_path / name.format(key=receipt.key[:12]))
    assert_corrupt(tmp_path, receipt, payload)


@pytest.mark.parametrize("damage", ["duplicate", "reorder", "extra", "report", "pass", "size"])
def test_file_group_contract(tmp_path: Path, damage: str) -> None:
    receipt, payload = persisted(tmp_path)
    files = cast(list[dict[str, object]], payload["files"])
    if damage == "duplicate":
        files[1] = files[0]
    elif damage == "reorder":
        files.reverse()
    elif damage == "extra":
        files.append(files[0])
    elif damage == "report":
        files[-1]["candidate"] = "pass-2/book.report.json"
    elif damage == "pass":
        files[0]["candidate"] = "pass-1/book.pdf"
    else:
        cast(dict[str, object], files[0]["stamp"])["size"] = 1
    assert_corrupt(tmp_path, receipt, payload)


@pytest.mark.parametrize("damage", ["files", "result", "sources", "cleaned"])
def test_building_must_be_empty(tmp_path: Path, damage: str) -> None:
    receipt, payload = persisted(tmp_path)
    bad = True if damage == "cleaned" else payload[damage]
    payload.update(state="building", files=[], result=None, sources=[], cleaned=False)
    payload[damage] = bad
    assert_corrupt(tmp_path, receipt, payload)


def test_read_must_match_request(tmp_path: Path) -> None:
    receipt, _ = persisted(tmp_path)
    for output, formats in [
        (tmp_path / "other.pdf", FORMATS),
        (tmp_path / "book.pdf", ("pdf",)),
        (tmp_path / "book.pdf", ("pdf", "pdf")),
    ]:
        with pytest.raises(LedgerError):
            read_receipt(tmp_path, receipt.key, output, formats)
    with pytest.raises(LedgerError):
        read_receipt(tmp_path, "bad", tmp_path / "book.pdf", FORMATS)


@pytest.mark.parametrize(
    "damage", ["result", "source", "previous-list", "previous-dict", "previous-size"]
)
def test_write_rejects_invalid_before_replacing(tmp_path: Path, damage: str) -> None:
    receipt, payload = persisted(tmp_path)
    assert receipt.result is not None
    invalid = replace(receipt, result=replace(receipt.result, elapsed_s=float("nan")))
    if damage == "source":
        invalid = replace(receipt, sources=(replace(SOURCES[0], size=0),))
    if damage.startswith("previous"):
        value: object = {
            "previous-list": [],
            "previous-dict": ({}, None, None, None),
            "previous-size": (Stamp(-1, "a" * 64), None, None, None),
        }[damage]
        invalid = replace(receipt, previous=cast(tuple[Stamp | None, ...], value))
    with pytest.raises(LedgerError):
        write_receipt(tmp_path, invalid)
    assert load_record(tmp_path, receipt.key) == payload


@pytest.mark.parametrize("value", [b"{", b"[]", b'{"schema":NaN}', b'{"schema":Infinity}', b"\xff"])
def test_malformed_json(tmp_path: Path, value: bytes) -> None:
    receipt, _ = persisted(tmp_path)
    (tmp_path / "exports" / f"{receipt.key}.json").write_bytes(value)
    with pytest.raises(LedgerError):
        read_receipt(tmp_path, receipt.key, tmp_path / "book.pdf", FORMATS)
