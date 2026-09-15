"""Stage and recover individually atomic publications from durable receipts."""

from __future__ import annotations

import asyncio
import hashlib
import os
import stat
from pathlib import Path

from .errors import FetchError, LedgerError
from .export_receipt import ExportReceipt, PublishedFile
from .store.cache import _identity
from .store.export_files import Stamp, _directory, check_workspace, fingerprint


def sync_file(path: Path) -> Stamp:
    value = fingerprint(path)
    if value is None:
        raise LedgerError("Export candidate is missing")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if fingerprint(path) != value:
        raise LedgerError("Export candidate changed while syncing")
    return value


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _temporary(receipt: ExportReceipt, item: PublishedFile) -> Path:
    return receipt.workspace / f"publish-{item.kind}.tmp"


def _finish_link(receipt: ExportReceipt, item: PublishedFile) -> None:
    # A killed no-clobber link publication can leave exactly these two owned names.
    temporary = _temporary(receipt, item)
    try:
        target = item.destination.lstat()
        staged = temporary.lstat()
    except FileNotFoundError:
        return
    if (target.st_dev, target.st_ino) != (staged.st_dev, staged.st_ino):
        return
    if not stat.S_ISREG(target.st_mode) or target.st_nlink != 2:
        raise LedgerError("Unexpected links in interrupted publication")
    check_workspace(receipt.workspace, receipt.workspace_identity)
    descriptor = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if _identity(before) != _identity(staged) or before.st_size != item.stamp.size:
            raise LedgerError("Interrupted publication changed")
        digest = hashlib.sha256()
        remaining = item.stamp.size
        while remaining:
            block = os.read(descriptor, min(remaining, 1024 * 1024))
            if not block:
                raise LedgerError("Interrupted publication was truncated")
            digest.update(block)
            remaining -= len(block)
        if digest.hexdigest() != item.stamp.sha256 or _identity(os.fstat(descriptor)) != _identity(
            before
        ):
            raise LedgerError("Interrupted publication differs from receipt")
        if _identity(temporary.lstat()) != _identity(before) or _identity(
            item.destination.lstat()
        ) != _identity(before):
            raise LedgerError("Interrupted publication paths changed")
        temporary.unlink()
        sync_directory(receipt.workspace)
        sync_directory(item.destination.parent)
    finally:
        os.close(descriptor)


def matches(receipt: ExportReceipt) -> bool:
    return all(fingerprint(item.destination) == item.stamp for item in receipt.files)


async def _stage(receipt: ExportReceipt, item: PublishedFile) -> None:
    with _directory(receipt.workspace) as (directory, check):
        await _stage_in_directory(receipt, item, directory)
        check()


async def _stage_in_directory(receipt: ExportReceipt, item: PublishedFile, directory: int) -> None:
    check_workspace(receipt.workspace, receipt.workspace_identity)
    source = receipt.workspace / item.candidate
    if fingerprint(source) != item.stamp:
        raise LedgerError("Export candidate differs from receipt; sources preserved")
    temporary = _temporary(receipt, item)
    old = fingerprint(temporary)
    if old is not None:
        check_workspace(receipt.workspace, receipt.workspace_identity)
        os.unlink(temporary.name, dir_fd=directory)
    source_fd = -1
    try:
        with _directory(source.parent) as (source_directory, _):
            check_workspace(receipt.workspace, receipt.workspace_identity)
            source_fd = os.open(
                source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source_directory
            )
        before = os.fstat(source_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size != item.stamp.size
        ):
            raise LedgerError("Export candidate changed before staging")
        check_workspace(receipt.workspace, receipt.workspace_identity)
        target_fd = os.open(
            temporary.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        with os.fdopen(target_fd, "wb") as target:
            remaining = item.stamp.size
            while remaining:
                block = os.read(source_fd, min(remaining, 1024 * 1024))
                if not block:
                    raise LedgerError("Export candidate was truncated while staging")
                target.write(block)
                remaining -= len(block)
                await asyncio.sleep(0)
            if _identity(os.fstat(source_fd)) != _identity(before):
                raise LedgerError("Export candidate changed while staging")
            target.flush()
            os.fsync(target.fileno())
    finally:
        if source_fd != -1:
            os.close(source_fd)
    if fingerprint(temporary) != item.stamp or fingerprint(source) != item.stamp:
        raise LedgerError("Export candidate changed while staging")
    check_workspace(receipt.workspace, receipt.workspace_identity)


async def publish(receipt: ExportReceipt) -> None:
    check_workspace(receipt.workspace, receipt.workspace_identity)
    committed: list[str] = []
    pending = []
    try:
        for item in receipt.files:
            _finish_link(receipt, item)
            current = fingerprint(item.destination)
            if current == item.stamp:
                committed.append(item.destination.name)
            elif current != item.before:
                raise LedgerError("Output changed since preparation; refusing to overwrite")
            else:
                pending.append(item)
        for item in pending:
            await _stage(receipt, item)
        sync_directory(receipt.workspace)
        await asyncio.sleep(0)
        # Each file is atomic; the durable receipt makes the group recoverable.
        for item in pending:
            check_workspace(receipt.workspace, receipt.workspace_identity)
            if fingerprint(item.destination) != item.before:
                raise LedgerError("Output changed before publication; refusing to overwrite")
            temporary = _temporary(receipt, item)
            if fingerprint(temporary) != item.stamp:
                raise LedgerError("Staged output changed before publication")
            check_workspace(receipt.workspace, receipt.workspace_identity)
            if fingerprint(item.destination) != item.before:
                raise LedgerError("Output changed before publication; refusing to overwrite")
            if item.before is None:
                os.link(temporary, item.destination, follow_symlinks=False)
                committed.append(item.destination.name)
                temporary.unlink()
            else:
                os.replace(temporary, item.destination)
                committed.append(item.destination.name)
            sync_directory(item.destination.parent)
            sync_directory(receipt.workspace)
            await asyncio.sleep(0)
        if not matches(receipt):
            raise LedgerError("Published outputs failed verification")
        for parent in {item.destination.parent for item in receipt.files}:
            sync_directory(parent)
    except OSError:
        names = ", ".join(committed) or "无"
        raise FetchError(f"成品发布失败；已发布：{names}；请用 --resume 恢复，原图已保留") from None
