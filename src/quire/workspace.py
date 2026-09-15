"""Task-owned temporary files and atomic publication."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO


@contextmanager
def atomic_output(
    path: Path, *, overwrite: bool = False, on_commit: Callable[[], None] | None = None
) -> Iterator[IO[bytes]]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(prefix=f".{path.name}.", dir=path.parent, delete=False)
    staged = Path(handle.name)
    try:
        with handle:
            yield handle.file
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            os.replace(staged, path)
        else:
            # link publishes a complete file and fails atomically if the target exists.
            os.link(staged, path)
        if on_commit is not None:
            on_commit()
    finally:
        staged.unlink(missing_ok=True)


def write_bytes(path: Path, data: bytes, *, overwrite: bool = False) -> None:
    with atomic_output(path, overwrite=overwrite) as handle:
        handle.write(data)


@contextmanager
def task_cache(root: Path, *, keep: bool = False) -> Iterator[Path]:
    root.mkdir(parents=True, exist_ok=True)
    cache = Path(tempfile.mkdtemp(prefix="task-", dir=root))
    try:
        yield cache
    finally:
        if not keep:
            shutil.rmtree(cache)
