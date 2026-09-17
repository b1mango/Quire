"""Explicit discovered manga pages, shared by series capture and offline rebuild."""

from __future__ import annotations

from dataclasses import dataclass

from .models import MangaResult
from .parse.images import Candidate
from .store.models import ResourceSpec


@dataclass(frozen=True, slots=True)
class MangaPlan:
    candidates: tuple[Candidate, ...]
    specs: tuple[ResourceSpec, ...]
    result: MangaResult
    chapter_titles: tuple[str, ...] = ()
