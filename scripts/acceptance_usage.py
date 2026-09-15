"""Standalone disk sampling for one case root (work and outputs, excluding corpus).

Only the standard library is imported. Peaks are observations, not filesystem
snapshots or OS high-water marks. Reports must also live outside the case root.
"""

from __future__ import annotations

import math
import os
import stat
import sys
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from types import TracebackType


@dataclass(frozen=True)
class Usage:
    logical_bytes: int = 0
    allocated_bytes: int = 0
    files: int = 0


def scan_usage(root: Path) -> Usage:
    """Count unique regular-file inodes using lstat and st_blocks * 512.

    Missing entries are normal during cleanup; all other errors propagate.
    Directory descriptors prevent symlink replacement races during traversal.
    Symlink entries (including root) are skipped; symlink ancestors are rejected.
    """
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    seen: set[tuple[int, int]] = set()
    logical = allocated = files = 0

    def visit(parent: int, name: str) -> None:
        nonlocal logical, allocated, files
        try:
            info = os.lstat(name, dir_fd=parent)
        except FileNotFoundError:
            return
        if stat.S_ISREG(info.st_mode):
            identity = (info.st_dev, info.st_ino)
            if identity not in seen:
                seen.add(identity)
                logical += info.st_size
                allocated += info.st_blocks * 512
                files += 1
        elif stat.S_ISDIR(info.st_mode):
            try:
                directory = os.open(name, flags, dir_fd=parent)
            except FileNotFoundError:
                return
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        visit(directory, entry.name)
            except FileNotFoundError:
                pass
            finally:
                os.close(directory)

    parent = os.open(root.anchor or ".", flags)
    try:
        # Open each ancestor separately so even intermediate symlinks cannot escape.
        parts = root.parts[1:] if root.anchor else root.parts
        for part in parts[:-1]:
            child = os.open(part, flags, dir_fd=parent)
            os.close(parent)
            parent = child
        visit(parent, parts[-1] if parts else ".")
    except FileNotFoundError:
        pass
    finally:
        os.close(parent)
    return Usage(logical, allocated, files)


class UsageError(RuntimeError):
    """Sampling failed; the message contains error categories only."""


class UsageMonitor:
    """Single-use context manager with serialized polling and phase samples.

    sample(label) returns Usage or raises UsageError, without paths/messages from
    the underlying failure. Polling failures are recorded and raised at exit.
    Exit always joins the daemon and attempts a final sample; an existing body
    exception takes precedence. Keep the monitor to retrieve report() on failure.
    """

    def __init__(self, root: Path, interval: float = 0.02) -> None:
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("interval must be finite and positive")
        self.root = root
        self.interval = interval
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = "new"
        self._initial: Usage | None = None
        self._final: Usage | None = None
        self._last: Usage | None = None
        self._peak: Usage | None = None
        self._samples = 0
        self._errors: list[str] = []
        self._checkpoints: list[tuple[str, Usage | None]] = []

    def _sample_locked(self, label: str | None) -> Usage:
        self._samples += 1
        try:
            usage = scan_usage(self.root)
        except Exception as exc:
            category = type(exc).__name__
            if category not in self._errors:
                self._errors.append(category)
            if label is not None:
                self._checkpoints.append((label, None))
            raise UsageError(category) from None
        self._last = usage
        peak = self._peak or Usage()
        self._peak = Usage(
            max(peak.logical_bytes, usage.logical_bytes),
            max(peak.allocated_bytes, usage.allocated_bytes),
            max(peak.files, usage.files),
        )
        if label is not None:
            self._checkpoints.append((label, usage))
        return usage

    def sample(self, label: str) -> Usage:
        """Synchronously sample a phase; repeated labels retain every checkpoint."""
        with self._lock:
            if self._state != "running" or self._stop.is_set():
                raise RuntimeError("usage monitor is not running")
            return self._sample_locked(label)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            with self._lock:
                if self._stop.is_set():
                    return
                try:
                    self._sample_locked(None)
                except UsageError:
                    pass  # Exit propagates recorded failures to the acceptance caller.

    def __enter__(self) -> UsageMonitor:
        with self._lock:
            if self._state != "new":
                raise RuntimeError("usage monitor cannot be reused")
            self._state = "running"
            try:
                self._initial = self._sample_locked("initial")
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()
            except BaseException:
                self._state = "closed"
                self._stop.set()
                raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        with self._lock:
            try:
                self._final = self._sample_locked("final")
            except UsageError:
                pass
            finally:
                self._state = "closed"
            if self._errors and exc_type is None:
                raise UsageError(", ".join(self._errors)) from None

    def report(self) -> dict[str, object]:
        """Return a detached JSON-ready snapshot.

        samples counts attempts, including failures; errors lists unique classes.
        Each checkpoint has label/usage (None on failure). Initial/final/last and
        observed_peak contain Usage fields, or None if unavailable. The peak is
        per metric; delta_peak_logical is peak minus initial, in bytes. During a
        run final is None. Any errors invalidate acceptance, even after recovery.
        """
        with self._lock:
            return {
                "initial": asdict(self._initial) if self._initial is not None else None,
                "final": asdict(self._final) if self._final is not None else None,
                "last": asdict(self._last) if self._last is not None else None,
                "observed_peak": asdict(self._peak) if self._peak is not None else None,
                "delta_peak_logical": (
                    self._peak.logical_bytes - self._initial.logical_bytes
                    if self._peak is not None and self._initial is not None
                    else None
                ),
                "interval_s": self.interval,
                "samples": self._samples,
                "checkpoints": [
                    {"label": label, "usage": asdict(usage) if usage is not None else None}
                    for label, usage in self._checkpoints
                ],
                "errors": list(self._errors),
            }


def rss_bytes(raw: int, platform: str = sys.platform) -> int:
    """Normalize resource.getrusage(...).ru_maxrss without image dependencies."""
    return raw if platform == "darwin" else raw * 1024
