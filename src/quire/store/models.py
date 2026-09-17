"""Immutable ledger inputs, canonical task identity and resource snapshots."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Literal
from urllib.parse import urlsplit

from ..errors import LedgerError
from ..utils.urls import is_usable_url

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
type TaskStatus = Literal["pending", "running", "done", "partial", "failed"]
type ResourceStatus = Literal["pending", "downloading", "done", "failed"]
type FailureCode = Literal["network", "invalid_image", "invalid_text", "blocked", "cancelled"]


def _absolute_url(url: str) -> bool:
    return (
        is_usable_url(url)
        and url == url.strip()
        and urlsplit(url).scheme in {"http", "https"}
        and not any(ord(c) < 32 for c in url)
    )


@dataclass(frozen=True, slots=True)
class ResourceSpec:
    chapter: int
    page: int
    url: str
    referer: str = ""

    def __post_init__(self) -> None:
        if type(self.chapter) is not int or type(self.page) is not int:
            raise LedgerError("Chapter and page must be integers")
        if not 1 <= self.chapter < 2**63 or not 1 <= self.page < 2**63:
            raise LedgerError("Chapter and page must be positive SQLite integers")
        if not _absolute_url(self.url) or (self.referer and not _absolute_url(self.referer)):
            raise LedgerError("Resource URL and referer must be absolute HTTP(S) addresses")


@dataclass(frozen=True, slots=True)
class ResourceRecord:
    spec: ResourceSpec
    status: ResourceStatus
    attempts: int
    local_path: str | None
    sha256: str | None
    size: int | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class TaskSnapshot:
    task_id: str
    status: TaskStatus
    resources: tuple[ResourceRecord, ...]


def _json(value: object) -> str:
    def validate(item: object) -> None:
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise LedgerError("Option keys must be strings")
            for child in item.values():
                validate(child)
        elif isinstance(item, list):
            for child in item:
                validate(child)
        elif item is not None and type(item) not in (str, int, bool, float):
            raise LedgerError("Options must contain JSON values")

    try:
        validate(value)
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise LedgerError("Options must be finite, acyclic JSON values") from exc


def task_identity(
    url: str, options: Mapping[str, JsonValue], resources: Sequence[ResourceSpec]
) -> tuple[str, str]:
    if not _absolute_url(url):
        raise LedgerError("Source URL must be an absolute HTTP(S) address")
    if not resources:
        raise LedgerError("A task must have at least one resource")
    keys = [(resource.chapter, resource.page) for resource in resources]
    if keys != sorted(set(keys)):
        raise LedgerError("Resources must have unique chapter/page keys in reading order")
    options_json = _json(dict(options))
    canonical = _json(
        {
            "source": url,
            "options": json.loads(options_json),
            "resources": [asdict(r) for r in resources],
        }
    )
    return hashlib.sha256(canonical.encode()).hexdigest(), options_json
