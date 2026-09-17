"""Bounded, offline PDF printing using an owned Chrome process and existing CDP."""

from __future__ import annotations

import asyncio
import base64
import binascii
import math
import os
import sys
from pathlib import Path
from typing import Any, BinaryIO

from ..errors import ConfigError, FetchError, UnsupportedError
from .browser_cdp import Cdp
from .browser_process import ChromeProcess, find_chrome

MAX_HTML_BYTES = 32 * 1024 * 1024
MAX_PDF_BYTES = 128 * 1024 * 1024
_CHUNK_BYTES = 256 * 1024
_POLICY = (
    '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
    "style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">"
)
_FONTS_READY = """(async () => {
  document.documentElement.getBoundingClientRect();
  await document.fonts.ready;
  return document.fonts.status === 'loaded';
})()"""
_FOOTER = (
    "<div style=\"font-family: 'Songti SC', 'PingFang SC', serif; font-size: 9px; "
    'color: #333; width: 100%; text-align: center;">'
    '<span class="pageNumber"></span> / <span class="totalPages"></span></div>'
)


def _identifier(reply: dict[str, Any], key: str) -> str:
    value = reply.get(key)
    if not isinstance(value, str) or not value:
        raise FetchError("Chrome PDF 返回了无效标识")
    return value


async def _prepare(cdp: Cdp, html: str) -> str:
    await cdp.call("Browser.setDownloadBehavior", {"behavior": "deny"})
    target = await cdp.call("Target.createTarget", {"url": "about:blank"})
    attached = await cdp.call(
        "Target.attachToTarget", {"targetId": _identifier(target, "targetId"), "flatten": True}
    )
    session = _identifier(attached, "sessionId")
    await cdp.call("Page.enable", session_id=session)
    await cdp.call("Emulation.setScriptExecutionDisabled", {"value": True}, session_id=session)
    await cdp.call("Network.setBlockedURLs", {"urls": ["*"]}, session_id=session)
    await cdp.call(
        "Network.emulateNetworkConditions",
        {"offline": True, "latency": 0, "downloadThroughput": 0, "uploadThroughput": 0},
        session_id=session,
    )
    await cdp.call("Emulation.setEmulatedMedia", {"media": "print"}, session_id=session)
    tree = await cdp.call("Page.getFrameTree", session_id=session)
    frame = tree.get("frameTree", {}).get("frame", {})
    await cdp.call(
        "Page.setDocumentContent",
        {"frameId": _identifier(frame, "id"), "html": "<!DOCTYPE html>" + _POLICY + html},
        session_id=session,
    )
    fonts = await cdp.call(
        "Runtime.evaluate",
        {"expression": _FONTS_READY, "awaitPromise": True, "returnByValue": True},
        session_id=session,
    )
    if "exceptionDetails" in fonts or fonts.get("result", {}).get("value") is not True:
        raise FetchError("小说 PDF 字体加载失败")
    return session


async def _close_stream(cdp: Cdp, session: str, handle: str) -> None:
    async def close() -> None:
        try:
            async with asyncio.timeout(1):
                await cdp.call("IO.close", {"handle": handle}, session_id=session)
        except TimeoutError:
            raise FetchError("Chrome PDF 流关闭超时") from None

    # Repeated cancellation must not leave a cleanup task using a closed CDP connection.
    task = asyncio.create_task(close(), name="quire-pdf-stream-cleanup")
    cancelled = False
    try:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        task.result()
    finally:
        if cancelled:
            raise asyncio.CancelledError


def _decode_chunk(reply: dict[str, Any]) -> tuple[bytes, bool]:
    data, encoded, eof = reply.get("data"), reply.get("base64Encoded", False), reply.get("eof")
    if not isinstance(data, str) or type(encoded) is not bool or type(eof) is not bool:
        raise FetchError("Chrome PDF 流格式无效")
    if len(data) > 4 * ((_CHUNK_BYTES + 2) // 3):
        raise FetchError("Chrome PDF 流块超过大小限制")
    try:
        chunk = base64.b64decode(data, validate=True) if encoded else data.encode("utf-8")
    except (ValueError, UnicodeError, binascii.Error):
        raise FetchError("Chrome PDF 流编码无效") from None
    if len(chunk) > _CHUNK_BYTES or (not chunk and not eof):
        raise FetchError("Chrome PDF 流块无效")
    return chunk, eof


async def _print(cdp: Cdp, session: str, output: BinaryIO) -> int:
    reply = await cdp.call(
        "Page.printToPDF",
        {
            "transferMode": "ReturnAsStream",
            "preferCSSPageSize": True,
            "paperWidth": 148 / 25.4,
            "paperHeight": 210 / 25.4,
            "marginTop": 18 / 25.4,
            "marginBottom": 18 / 25.4,
            "marginLeft": 18 / 25.4,
            "marginRight": 18 / 25.4,
            "displayHeaderFooter": True,
            "headerTemplate": "<span></span>",
            "footerTemplate": _FOOTER,
            "printBackground": False,
        },
        session_id=session,
    )
    handle = _identifier(reply, "stream")
    size, prefix, tail = 0, b"", b""
    try:
        while True:
            reply = await cdp.call(
                "IO.read", {"handle": handle, "size": _CHUNK_BYTES}, session_id=session
            )
            chunk, eof = _decode_chunk(reply)
            size += len(chunk)
            if size > MAX_PDF_BYTES:
                raise FetchError("小说 PDF 超过 128 MiB 限制")
            prefix = (prefix + chunk)[:5]
            tail = (tail + chunk)[-1024:]
            output.write(chunk)
            if eof:
                break
        if prefix != b"%PDF-" or not tail.rstrip().endswith(b"%%EOF"):
            raise FetchError("Chrome 返回了不完整的 PDF")
        return size
    finally:
        failure = sys.exception()
        try:
            await _close_stream(cdp, session, handle)
        except FetchError:
            # Preserve cancellation/timeout if Chrome also disconnects during cleanup.
            if failure is not None:
                raise failure from None
            raise


async def print_pdf(html: str, destination: Path, *, executable: str, timeout: float = 60) -> int:
    """Return bytes written; never overwrite, and remove our candidate on any failure.

    The deadline covers startup, font readiness, printing and streaming. Owned Chrome/CDP
    cleanup is cancellation-resistant and may briefly extend past the work deadline.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    if not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise ConfigError("小说 PDF 超时须大于 0 且不超过 60 秒")
    if len(html) > MAX_HTML_BYTES:
        raise ConfigError("小说 HTML 超过 32 MiB 限制")
    try:
        size = len(html.encode("utf-8"))
    except UnicodeError:
        raise ConfigError("小说 HTML 必须是有效的 UTF-8 文本") from None
    if size > MAX_HTML_BYTES:
        raise ConfigError("小说 HTML 超过 32 MiB 限制")
    try:
        resolved = find_chrome(executable)
    except ConfigError:
        resolved = None
    if resolved is None:
        raise UnsupportedError("小说转 PDF 需要 Chrome", hint="安装 Chrome 或指定有效的 --chrome")
    owned: os.stat_result | None = None
    try:
        with destination.open("xb") as output:
            owned = os.fstat(output.fileno())
            async with asyncio.timeout_at(deadline), ChromeProcess(resolved) as chrome:
                async with Cdp(chrome.endpoint) as cdp:
                    session = await _prepare(cdp, html)
                    size = await _print(cdp, session, output)
        return size
    except BaseException as exc:
        if owned is not None:
            try:
                current = destination.lstat()
            except FileNotFoundError:
                current = None
            if current is not None and (current.st_dev, current.st_ino) == (
                owned.st_dev,
                owned.st_ino,
            ):
                destination.unlink()
        if isinstance(exc, TimeoutError):
            raise FetchError("小说 PDF 打印超时，候选文件已清理") from None
        raise
