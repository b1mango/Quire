"""A single-reader CDP transport with bounded events and deterministic shutdown."""

from __future__ import annotations

import asyncio
import json
from typing import Any, NoReturn

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

from ..errors import ConfigError, FetchError, NetworkError


class _NoRedirectConnect(connect):
    def process_redirect(self, exc: Exception) -> Exception:
        return exc


def _invalid_constant(value: str) -> NoReturn:
    raise ValueError("Invalid JSON constant")


def _message(raw: str | bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw, parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise FetchError("Invalid CDP JSON message") from None
    if not isinstance(value, dict):
        raise FetchError("Invalid CDP message structure")
    if "sessionId" in value and (not isinstance(value["sessionId"], str) or not value["sessionId"]):
        raise FetchError("Invalid CDP session identifier")
    if "id" in value:
        if (
            type(value["id"]) is not int
            or value["id"] < 1
            or "method" in value
            or ("result" in value) == ("error" in value)
        ):
            raise FetchError("Invalid CDP response structure")
        if "result" in value and not isinstance(value["result"], dict):
            raise FetchError("Invalid CDP response result")
        if "error" in value:
            error = value["error"]
            if (
                not isinstance(error, dict)
                or type(error.get("code")) is not int
                or not isinstance(error.get("message"), str)
            ):
                raise FetchError("Invalid CDP response error")
    elif (
        not isinstance(value.get("method"), str)
        or not value["method"]
        or not isinstance(value.get("params", {}), dict)
        or "result" in value
        or "error" in value
    ):
        raise FetchError("Invalid CDP event structure")
    return value


class Cdp:
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        self._connection: ClientConnection | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._failure: FetchError | None = None
        self._next_id = 0
        self._entered = False

    async def __aenter__(self) -> Cdp:
        if self._entered or self._close_task is not None:
            raise ConfigError("Cdp cannot be entered more than once")
        self._entered = True
        try:
            self._connection = await _NoRedirectConnect(
                self.endpoint,
                proxy=None,
                max_size=40 * 1024 * 1024,
                open_timeout=10,
                close_timeout=1,
            )
        except (OSError, TimeoutError, ValueError, WebSocketException):
            raise NetworkError("Unable to connect to Chrome debugging endpoint") from None
        self._reader = asyncio.create_task(self._read(), name="quire-cdp-reader")
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if self._failure is not None:
            raise type(self._failure)(self._failure.message) from None
        if self._connection is None:
            raise NetworkError("CDP connection is not open")
        if (
            not isinstance(method, str)
            or not method
            or (params is not None and not isinstance(params, dict))
            or (session_id is not None and (not isinstance(session_id, str) or not session_id))
        ):
            raise FetchError("Invalid CDP command arguments")
        self._next_id += 1
        message: dict[str, Any] = {"id": self._next_id, "method": method}
        if params is not None:
            message["params"] = params
        if session_id is not None:
            message["sessionId"] = session_id
        try:
            wire = json.dumps(message, allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            raise FetchError("CDP command must contain valid JSON data") from None
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[message["id"]] = future
        try:
            await self._connection.send(wire)
            return await future
        except (OSError, TimeoutError, WebSocketException):
            self._fail(NetworkError("CDP connection failed while sending a command"))
            await self.close()
            assert self._failure is not None
            raise type(self._failure)(self._failure.message) from None
        finally:
            self._pending.pop(message["id"], None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    def _fail(self, error: FetchError) -> None:
        if self._failure is None:
            self._failure = error
        for future in self._pending.values():
            if not future.done():
                future.set_exception(type(self._failure)(self._failure.message))

    async def _read(self) -> None:
        assert self._connection is not None
        try:
            async for raw in self._connection:
                message = _message(raw)
                if "id" in message:
                    future = self._pending.get(message["id"])
                    if future is None or future.done():
                        continue
                    if "error" in message:
                        code = message["error"]["code"]
                        # Chrome 的 message 可能回显请求参数,只放行已知的安全短语。
                        detail = message["error"]["message"]
                        suffix = (
                            ": Invalid InterceptionId" if "Invalid InterceptionId" in detail else ""
                        )
                        future.set_exception(
                            FetchError(f"CDP command failed (code {code}){suffix}")
                        )
                    else:
                        future.set_result(message["result"])
                else:
                    try:
                        self.events.put_nowait(message)
                    except asyncio.QueueFull:
                        raise FetchError("CDP event queue exceeded its limit of 256") from None
        except FetchError as exc:
            self._fail(exc)
        except (OSError, TimeoutError, WebSocketException):
            self._fail(NetworkError("CDP connection was lost"))
        finally:
            self._fail(NetworkError("CDP connection closed"))
            self._begin_close()

    def _begin_close(self) -> asyncio.Task[None]:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._shutdown(), name="quire-cdp-cleanup")
        return self._close_task

    async def close(self) -> None:
        self._fail(NetworkError("CDP connection closed"))
        task = self._begin_close()
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _shutdown(self) -> None:
        try:
            if self._connection is not None:
                await self._connection.close()
        except (OSError, TimeoutError, WebSocketException):
            self._fail(NetworkError("CDP connection failed during shutdown"))
        finally:
            if self._reader is not None:
                self._reader.cancel()
                await asyncio.gather(self._reader, return_exceptions=True)
