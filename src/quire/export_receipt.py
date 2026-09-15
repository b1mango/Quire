"""Strict export receipts; filesystem ownership checks belong to export_files."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Literal, NoReturn, cast

from .errors import ConfigError, LedgerError
from .export_options import output_paths
from .image.options import PRESETS, CompressionOptions
from .models import ArtifactResult, MangaOptions, MangaResult
from .parse.images import Rejection
from .store.cache import FileStamp
from .store.export_files import Stamp, load_record, save_record


@dataclass(frozen=True, slots=True)
class PublishedFile:
    kind: str
    candidate: str
    destination: Path
    stamp: Stamp
    before: Stamp | None


@dataclass(frozen=True, slots=True)
class ExportReceipt:
    key: str
    state: Literal["building", "prepared", "complete"]
    workspace: Path
    workspace_identity: tuple[int, int]
    formats: tuple[str, ...]
    files: tuple[PublishedFile, ...] = ()
    result: MangaResult | None = None
    sources: tuple[FileStamp, ...] = ()
    cleaned: bool = False
    previous: tuple[Stamp | None, ...] = ()


def _bad(field: str) -> NoReturn:
    raise LedgerError(f"Invalid export receipt: {field}")


def _require(condition: bool, field: str) -> None:
    if not condition:
        _bad(field)


def _object(value: object, names: Iterable[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != set(names):
        _bad("object fields")
    return cast(dict[str, object], value)


def _list(value: object) -> list[object]:
    if not isinstance(value, list):
        _bad("array")
    return cast(list[object], value)


def _text(value: object) -> str:
    if not isinstance(value, str):
        _bad("string")
    return value


def _integer(value: object, minimum: int = 0, maximum: int | None = None) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        _bad("integer range")
    return value


def _optional_integer(value: object, maximum: int | None = None) -> int | None:
    return None if value is None else _integer(value, 1, maximum)


def _optional_bool(value: object) -> bool | None:
    if value is not None and not isinstance(value, bool):
        _bad("boolean")
    return value


def _path(value: object) -> Path:
    text = _text(value)
    path = Path(text)
    _require(
        path.is_absolute() and ".." not in path.parts and "\0" not in text and str(path) == text,
        "absolute canonical path",
    )
    return path


def _digest(value: object) -> str:
    text = _text(value)
    _require(re.fullmatch(r"[0-9a-f]{64}", text) is not None, "SHA-256")
    return text


def _stamp(value: object) -> Stamp:
    data = _object(value, ("size", "sha256"))
    return Stamp(_integer(data["size"]), _digest(data["sha256"]))


def _sources(value: object, result: MangaResult) -> tuple[FileStamp, ...]:
    entries = _list(value)
    _require(len(entries) <= result.source_resources, "source count")
    sources = []
    seen: set[str] = set()
    for item in entries:
        entry = _object(item, ("path", "sha256", "size"))
        path = _text(entry["path"])
        _require(
            re.fullmatch(rf"cache/{result.task_id}/[a-z0-9._-]+", path) is not None
            and path.rsplit("/", 1)[-1] not in {".", ".."},
            "source path",
        )
        _require(path not in seen, "duplicate source path")
        seen.add(path)
        sources.append(FileStamp(path, _digest(entry["sha256"]), _integer(entry["size"], 1)))
    return tuple(sources)


def _json(value: object) -> object:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {_text(key): _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    _bad("JSON value")


def _destinations(output: Path, formats: tuple[str, ...]) -> tuple[Path, ...]:
    _require(isinstance(formats, tuple) and all(isinstance(f, str) for f in formats), "formats")
    try:
        return output_paths(_path(str(output.absolute())), formats)
    except ConfigError as exc:
        raise LedgerError("Invalid export receipt: output formats") from exc


def export_key(
    task_id: str,
    title: str,
    output: Path,
    formats: tuple[str, ...],
    opts: MangaOptions,
    compression: CompressionOptions,
) -> str:
    """Identify export content and location, independently of overwrite authorization."""
    _destinations(output, formats)
    payload = {
        "task_id": _text(task_id),
        "title": _text(title),
        "output": str(output.absolute()),
        "formats": formats,
        "compression": asdict(compression),
        "dpi": opts.dpi,
        "paper": opts.paper,
        "keep_images": opts.keep_images,
    }
    try:
        data = json.dumps(payload, sort_keys=True, ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise LedgerError("Invalid export identity") from exc
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _result(
    value: object, paths: tuple[Path, ...], formats: tuple[str, ...], root: Path
) -> MangaResult:
    data = _object(value, (field.name for field in fields(MangaResult)))
    output, report = _path(data["output"]), _path(data["report"])
    _require(output == paths[0] and report == paths[0].with_suffix(".report.json"), "result paths")
    task_id = _digest(data["task_id"])
    images = None if data["images_dir"] is None else _path(data["images_dir"])
    if images is not None:
        _require(images == root.absolute() / "cache" / task_id, "images path")
    elapsed = data["elapsed_s"]
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)):
        _bad("elapsed_s")
    try:
        seconds = float(elapsed)
    except OverflowError:
        _bad("elapsed_s")
    _require(math.isfinite(seconds) and seconds >= 0, "elapsed_s")
    compression = None if data["compression"] is None else _text(data["compression"])
    _require(compression is None or compression in PRESETS, "compression")
    target = _optional_integer(data["target_bytes"], 10**12)
    rejections = []
    for item in _list(data["rejections"]):
        entry = _object(item, ("url", "reason", "stage"))
        _require(
            _text(entry["stage"]) in {"url", "duplicate", "size", "format", "decode"},
            "rejection stage",
        )
        rejections.append(Rejection(*(_text(entry[name]) for name in ("url", "reason", "stage"))))
    failures = []
    for item in _list(data["failures"]):
        pair = _list(item)
        _require(len(pair) == 2, "failure pair")
        failures.append((_text(pair[0]), _text(pair[1])))
    artifacts = []
    entries = _list(data["artifacts"])
    _require(len(entries) == len(formats), "artifact group")
    for item, fmt, path in zip(entries, formats, paths, strict=True):
        entry = _object(item, ("format", "path", "bytes", "target_met"))
        _require(_text(entry["format"]) == fmt and _path(entry["path"]) == path, "artifact path")
        size, met = _integer(entry["bytes"]), _optional_bool(entry["target_met"])
        _require(met is (None if target is None else size <= target), "artifact target_met")
        artifacts.append(ArtifactResult(fmt, path, size, met))
    met = _optional_bool(data["target_met"])
    _require(
        met is (None if target is None else all(a.target_met for a in artifacts)), "target_met"
    )
    size = _integer(data["bytes_out"])
    _require(size == artifacts[0].bytes, "bytes_out")
    _require(
        data["artifacts_reused"] is False and data["export_recovered"] is False, "return flags"
    )
    failed, rejected = _integer(data["pages_failed"]), _integer(data["pages_rejected"])
    sources, reused = _integer(data["source_resources"]), _integer(data["resources_reused"])
    written = _integer(data["pages_written"])
    _require(failed == len(failures) and failed <= sources, "failure count")
    # Discovery rejections do not represent failed source pages in the selected export.
    filtered = sum(r.stage in {"size", "format", "decode"} for r in rejections)
    _require(rejected == filtered and rejected <= failed, "rejection count")
    _require(reused <= sources, "reused resource count")
    _require(written >= sources - failed, "written page count")
    return MangaResult(
        output=output,
        pages_written=written,
        pages_failed=failed,
        pages_rejected=rejected,
        bytes_out=size,
        elapsed_s=seconds,
        title=_text(data["title"]),
        warnings=tuple(_text(item) for item in _list(data["warnings"])),
        rejections=tuple(rejections),
        failures=tuple(failures),
        images_dir=images,
        report=report,
        task_id=task_id,
        resources_reused=reused,
        source_resources=sources,
        compression=compression,
        target_bytes=target,
        target_met=met,
        encoding_rounds=_integer(data["encoding_rounds"], 0, 3),
        quality=_optional_integer(data["quality"], 100),
        max_edge=_optional_integer(data["max_edge"]),
        artifacts=tuple(artifacts),
    )


def _decode(
    payload: object, key: str, output: Path, formats: tuple[str, ...], root: Path
) -> ExportReceipt:
    data = _object(payload, ("schema", *(field.name for field in fields(ExportReceipt))))
    _require(type(data["schema"]) is int and data["schema"] == 1, "schema")
    _require(_digest(data["key"]) == _digest(key), "key")
    state = _text(data["state"])
    _require(state in {"building", "prepared", "complete"}, "state")
    cleaned = data["cleaned"]
    _require(isinstance(cleaned, bool), "cleaned boolean")
    _require(state == "complete" or cleaned is False, "cleanup state")
    paths = _destinations(output, formats)
    _require(tuple(_text(f) for f in _list(data["formats"])) == formats, "formats")
    prior = _list(data["previous"])
    _require(not prior or len(prior) == len(formats) + 1, "previous file group")
    previous = tuple(None if item is None else _stamp(item) for item in prior)
    workspace = _path(data["workspace"])
    _require(
        workspace.parent == paths[0].parent
        and re.fullmatch(rf"\.quire-export-{key[:12]}-[A-Za-z0-9_-]+", workspace.name) is not None,
        "workspace location",
    )
    identity = _list(data["workspace_identity"])
    _require(len(identity) == 2, "workspace identity")
    inode = (_integer(identity[0]), _integer(identity[1]))
    entries = _list(data["files"])
    published: list[PublishedFile] = []
    sources: tuple[FileStamp, ...] = ()
    result = None
    if state == "building":
        _require(
            not entries and data["result"] is None and not _list(data["sources"]), "building state"
        )
    else:
        kinds, destinations = (*formats, "report"), (*paths, paths[0].with_suffix(".report.json"))
        _require(len(entries) == len(kinds), "file group")
        for item, kind, path in zip(entries, kinds, destinations, strict=True):
            entry = _object(item, (field.name for field in fields(PublishedFile)))
            _require(
                _text(entry["kind"]) == kind and _path(entry["destination"]) == path, "file path"
            )
            candidate = _text(entry["candidate"])
            _require(
                candidate == "book.report.json"
                if kind == "report"
                else re.fullmatch(rf"pass-[1-3]/book\.{kind}", candidate) is not None,
                "candidate path",
            )
            before = None if entry["before"] is None else _stamp(entry["before"])
            published.append(PublishedFile(kind, candidate, path, _stamp(entry["stamp"]), before))
        _require(len({Path(f.candidate).parent for f in published[:-1]}) == 1, "candidate pass")
        result = _result(data["result"], paths, formats, root)
        sources = _sources(data["sources"], result)
        _require(
            all(
                f.stamp.size == a.bytes
                for f, a in zip(published[:-1], result.artifacts, strict=True)
            ),
            "artifact stamp size",
        )
    return ExportReceipt(
        key,
        cast(Literal["building", "prepared", "complete"], state),
        workspace,
        inode,
        formats,
        tuple(published),
        result,
        sources,
        cast(bool, cleaned),
        previous,
    )


def write_receipt(root: Path, receipt: ExportReceipt) -> None:
    """Validate before replacing the persisted JSON record under the caller's Ledger lock."""
    try:
        _require(
            isinstance(receipt.previous, tuple)
            and all(item is None or isinstance(item, Stamp) for item in receipt.previous),
            "previous tuple of stamps",
        )
        payload = cast(dict[str, object], _json({"schema": 1, **asdict(receipt)}))
        output = (
            receipt.result.output
            if receipt.result is not None
            else receipt.workspace.parent / "book"
        )
        _decode(payload, receipt.key, output, receipt.formats, root)
    except (TypeError, ValueError, RecursionError, AttributeError) as exc:
        raise LedgerError("Invalid export receipt") from exc
    save_record(root, receipt.key, payload)


def read_receipt(
    root: Path, key: str, output: Path, formats: tuple[str, ...]
) -> ExportReceipt | None:
    """Return absent records as None; reject malformed or mismatched receipts explicitly."""
    _digest(key)
    _destinations(output, formats)
    payload = load_record(root, key)
    return None if payload is None else _decode(payload, key, output, formats, root)
