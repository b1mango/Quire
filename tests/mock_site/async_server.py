"""Bounded loopback HTTP/1.1 fixture with observable keepalive connections."""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from urllib.parse import urlsplit

import h11

BODY = b"quire-loopback-body\n"
DRIP_BODY = b"x" * 80


@dataclass
class RequestRecord:
    connection_id: int
    path: str
    timestamp: float
    http_version: str
    headers: dict[str, str]
    headers_sent_at: float | None = None
    chunk_timestamps: list[float] = field(default_factory=list)
    bytes_sent: int = 0
    complete: bool = False
    ended_at: float | None = None
    method: str = "GET"
    body: bytes = b""


class AsyncMockSite:
    def __init__(self) -> None:
        self.requests: list[RequestRecord] = []
        self.counts: Counter[str] = Counter()
        self.connections: dict[int, dict[str, float | None]] = {}
        self.active = 0
        self.peak = 0
        self.errors: list[str] = []
        self._server: asyncio.Server | None = None
        self._writers: dict[int, asyncio.StreamWriter] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self.url = ""

    @property
    def pending_tasks(self) -> int:
        return sum(not task.done() for task in self._tasks)

    @property
    def open_connections(self) -> int:
        return len(self._writers)

    async def __aenter__(self) -> AsyncMockSite:
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self._server.sockets[0].getsockname()[1]}"
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()
        await asyncio.wait_for(self._server.wait_closed(), 2)
        for writer in tuple(self._writers.values()):
            writer.close()
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 2)

    async def wait_for(self, predicate: Callable[[], bool], timeout: float = 2) -> None:
        try:
            async with asyncio.timeout(timeout):
                while not predicate():
                    await asyncio.sleep(0.005)
        except TimeoutError:
            raise AssertionError(f"Fixture condition timed out: {self.snapshot()}") from None

    async def wait_closed(self, connection_id: int) -> None:
        await self.wait_for(lambda: self.connections[connection_id]["closed_at"] is not None)

    async def wait_idle(self) -> None:
        await self.wait_for(lambda: not self.open_connections and not self.pending_tasks)
        assert self.active == 0 and not self.errors, self.snapshot()

    def records(self, path: str) -> list[RequestRecord]:
        return [record for record in self.requests if record.path == path]

    def snapshot(self) -> dict[str, object]:
        return {
            "counts": dict(self.counts),
            "conns": len(self.connections),
            "connections": self.connections,
            "peak": self.peak,
            "active": self.active,
            "open_connections": self.open_connections,
            "server_tasks": self.pending_tasks,
            "requests": [asdict(record) for record in self.requests],
            "server_errors": list(self.errors),
        }

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connection_id = len(self.connections) + 1
        self.connections[connection_id] = {"accepted_at": time.monotonic(), "closed_at": None}
        self._writers[connection_id] = writer
        task = asyncio.create_task(
            self._serve(connection_id, reader, writer), name=f"mock-http-{connection_id}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _serve(
        self, connection_id: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        protocol = h11.Connection(h11.SERVER)
        try:
            while True:
                async with asyncio.timeout(8):
                    read = await self._read_request(protocol, reader)
                    if read is None:
                        break
                    request, request_body = read
                    await self._handle(
                        connection_id, request, request_body, protocol, reader, writer
                    )
                if protocol.our_state is not h11.DONE or protocol.their_state is not h11.DONE:
                    break
                protocol.start_next_cycle()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception as exc:
            self.errors.append(f"connection {connection_id}: {type(exc).__name__}: {exc}")
        finally:
            writer.close()
            with suppress(ConnectionError, TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), 1)
            self._writers.pop(connection_id, None)
            self.connections[connection_id]["closed_at"] = time.monotonic()

    async def _read_request(
        self, protocol: h11.Connection, reader: asyncio.StreamReader
    ) -> tuple[h11.Request, bytes] | None:
        request = None
        body = bytearray()
        while True:
            event = protocol.next_event()
            if event is h11.NEED_DATA:
                protocol.receive_data(await reader.read(8192))
            elif isinstance(event, h11.Request):
                request = event
            elif isinstance(event, h11.Data):
                body.extend(event.data)
            elif isinstance(event, h11.EndOfMessage):
                assert request is not None and request.method in {b"GET", b"POST"}
                return request, bytes(body)
            elif isinstance(event, h11.ConnectionClosed):
                return None
            else:
                raise AssertionError(f"Unexpected request event: {event!r}")

    async def _handle(
        self,
        connection_id: int,
        request: h11.Request,
        request_body: bytes,
        protocol: h11.Connection,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        record = RequestRecord(
            connection_id,
            urlsplit(request.target.decode("ascii")).path,
            time.monotonic(),
            request.http_version.decode("ascii"),
            {key.decode("ascii"): value.decode("ascii") for key, value in request.headers},
            method=request.method.decode("ascii"),
            body=request_body,
        )
        self.requests.append(record)
        self.counts[record.path] += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await self._respond(record, protocol, reader, writer)
        finally:
            record.ended_at = time.monotonic()
            self.active -= 1

    async def _pause(self, reader: asyncio.StreamReader, delay: float) -> None:
        deadline = time.monotonic() + delay
        while (remaining := deadline - time.monotonic()) > 0:
            if reader.at_eof():
                raise ConnectionAbortedError("Client disconnected")
            await asyncio.sleep(min(0.01, remaining))

    async def _respond(
        self,
        record: RequestRecord,
        protocol: h11.Connection,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        path = record.path
        if path == "/wait-headers":
            await self._pause(reader, 5)
        elif path.startswith("/slow/"):
            await self._pause(reader, 0.35)
        drip = path in {"/drip", "/drip-chunked"}
        body = DRIP_BODY if drip else (b"ok" if path == "/ok" else BODY)
        status = 404 if path == "/robots.txt" else 200
        if status == 404:
            body = b""
        framing = (
            (b"transfer-encoding", b"chunked")
            if path in {"/chunked", "/drip-chunked"}
            else (b"content-length", str(len(body)).encode("ascii"))
        )
        writer.write(
            protocol.send(
                h11.Response(
                    status_code=status,
                    headers=[framing, (b"content-type", b"text/plain; charset=utf-8")],
                )
            )
        )
        await writer.drain()
        record.headers_sent_at = time.monotonic()
        chunks = [body[i : i + 1] for i in range(len(body))] if drip else [body]
        for index, block in enumerate(chunks):
            if index:
                await self._pause(reader, 0.04)
            writer.write(protocol.send(h11.Data(data=block)))
            await writer.drain()
            record.chunk_timestamps.append(time.monotonic())
            record.bytes_sent += len(block)
        writer.write(protocol.send(h11.EndOfMessage()))
        await writer.drain()
        record.complete = True
