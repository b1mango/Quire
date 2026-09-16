from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import textwrap
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest
from websockets.asyncio.server import ServerConnection, serve

from quire.errors import ConfigError, FetchError, NetworkError
from quire.fetch import browser_process
from quire.fetch.browser_cdp import Cdp
from quire.fetch.browser_process import ChromeProcess, find_chrome


def _fake_chrome(
    tmp_path: Path,
    *,
    ready: bool,
    stubborn: bool = False,
    endpoint: str = "12345\n/devtools/browser/fake-browser\n",
    early_exit: bool = False,
) -> Path:
    executable = tmp_path / "fake-chrome"
    executable.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent(
            f"""\
            import json
            import signal
            import subprocess
            import sys
            import time
            from pathlib import Path

            directory = Path({str(tmp_path)!r})
            profile = Path(next(arg.split('=', 1)[1] for arg in sys.argv
                                if arg.startswith('--user-data-dir=')))
            if {early_exit!r}:
                sys.exit(7)
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])

            def stop(signum, frame):
                child.wait(timeout=2)
                (directory / 'child-reaped').write_text('yes')
                if not {stubborn!r}:
                    sys.exit(0)

            signal.signal(signal.SIGTERM, stop)
            (directory / 'argv.json').write_text(json.dumps(sys.argv))
            (directory / 'child.pid').write_text(str(child.pid))
            if {ready!r}:
                (profile / 'DevToolsActivePort').write_text({endpoint!r})
            while True:
                time.sleep(0.02)
            """
        ),
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable


async def _wait_until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.01)


def _assert_clean(process: ChromeProcess, tmp_path: Path) -> None:
    assert process.profile is not None and not process.profile.exists()
    assert process.pid is not None
    with pytest.raises(ProcessLookupError):
        os.kill(process.pid, 0)
    with pytest.raises(ChildProcessError):
        os.waitpid(process.pid, os.WNOHANG)
    child = int((tmp_path / "child.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)
    with pytest.raises(ProcessLookupError):
        os.killpg(process.pid, 0)
    assert (tmp_path / "child-reaped").read_text() == "yes"


def _assert_no_transport_tasks() -> None:
    assert not [task for task in asyncio.all_tasks() if task.get_name().startswith("quire-")]


def test_find_chrome_explicit_and_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    executable = _fake_chrome(tmp_path, ready=True)
    assert find_chrome(str(executable)) == str(executable)
    with pytest.raises(ConfigError):
        find_chrome(str(tmp_path / "missing"))
    monkeypatch.setattr(browser_process, "_CHROME_PATHS", (str(executable),))
    assert find_chrome() == str(executable)
    monkeypatch.setattr(browser_process, "_CHROME_NAMES", (executable.name,))
    monkeypatch.setattr(browser_process, "_CHROME_PATHS", ())
    monkeypatch.setenv("PATH", str(tmp_path))
    assert find_chrome() == str(executable)
    executable.chmod(0o600)
    with pytest.raises(ConfigError):
        find_chrome(str(executable))
    monkeypatch.setattr(browser_process, "_CHROME_PATHS", ())
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert find_chrome() is None


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
@pytest.mark.parametrize("body_error", [False, True])
def test_chrome_owned_group_profile_and_flags(tmp_path: Path, body_error: bool) -> None:
    executable = _fake_chrome(tmp_path, ready=True)
    process = ChromeProcess(str(executable))

    async def run() -> None:
        with pytest.raises(RuntimeError) if body_error else nullcontext():
            async with process as entered:
                assert entered is process
                with pytest.raises(ConfigError):
                    await process.__aenter__()
                assert process.endpoint == "ws://127.0.0.1:12345/devtools/browser/fake-browser"
                assert process.pid is not None and os.getpgid(process.pid) == process.pid
                assert process.profile is not None and process.profile.is_dir()
                args = json.loads((tmp_path / "argv.json").read_text())
                assert {
                    "--remote-debugging-port=0",
                    "--remote-debugging-address=127.0.0.1",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-background-networking",
                    "--disable-sync",
                    "--disable-extensions",
                    "--disable-quic",
                    "--proxy-server=http://127.0.0.1:9",
                    "--proxy-bypass-list=<-loopback>",
                }.issubset(args)
                assert any(arg.startswith("--headless") for arg in args)
                assert "--no-sandbox" not in args
                assert f"--user-data-dir={process.profile}" in args
                if body_error:
                    raise RuntimeError("body failed")
        _assert_clean(process, tmp_path)
        _assert_no_transport_tasks()

    asyncio.run(run())


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
@pytest.mark.parametrize("ready", [False, True])
def test_chrome_cancellation_reaps_owned_group(tmp_path: Path, ready: bool) -> None:
    executable = _fake_chrome(tmp_path, ready=ready, stubborn=True)
    process = ChromeProcess(str(executable))

    async def run() -> None:
        entered = asyncio.Event()

        async def owner() -> None:
            async with process:
                entered.set()
                await asyncio.Future[None]()

        task = asyncio.create_task(owner())
        await _wait_until(lambda: (tmp_path / "child.pid").exists())
        if ready:
            await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        await _wait_until(lambda: (tmp_path / "child-reaped").exists())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 4)
        _assert_clean(process, tmp_path)
        _assert_no_transport_tasks()

    asyncio.run(run())


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups")
@pytest.mark.parametrize("failure", ["missing", "incomplete", "invalid", "early", "unexecutable"])
def test_chrome_start_failure_cleans_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    executable = _fake_chrome(
        tmp_path,
        ready=failure in {"incomplete", "invalid"},
        early_exit=failure == "early",
        endpoint="12345\n" if failure == "incomplete" else "12345\nSECRET_BODY\n",
    )
    if failure == "unexecutable":
        executable.chmod(0o600)
    process = ChromeProcess(str(executable))
    monkeypatch.setattr(browser_process, "_START_TIMEOUT", 3.0)

    async def run() -> None:
        with pytest.raises(FetchError) as caught:
            async with process:
                pytest.fail("Chrome startup failure must reject the context")
        assert "SECRET_BODY" not in str(caught.value)
        assert process.profile is not None and not process.profile.exists()
        if failure in {"missing", "incomplete"}:
            assert "timed out" in str(caught.value)
        if failure in {"early", "unexecutable"}:
            assert isinstance(caught.value, NetworkError)
            if process.pid is not None:
                with pytest.raises(ChildProcessError):
                    os.waitpid(process.pid, os.WNOHANG)
        else:
            _assert_clean(process, tmp_path)
        _assert_no_transport_tasks()

    asyncio.run(run())


async def _with_server(
    handler: Callable[[ServerConnection], Awaitable[None]],
    client: Callable[[str], Awaitable[None]],
) -> None:
    async with serve(handler, "127.0.0.1", 0) as server:
        endpoint = f"ws://127.0.0.1:{next(iter(server.sockets)).getsockname()[1]}"
        async with asyncio.timeout(5):
            await client(endpoint)
    _assert_no_transport_tasks()


def test_cdp_routes_concurrent_responses_events_and_redacts_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")

    async def handler(socket: ServerConnection) -> None:
        requests = [json.loads(await socket.recv()) for _ in range(2)]
        assert all(request["sessionId"] == "session-1" for request in requests)
        await socket.send(json.dumps({"method": "Page.ready", "params": {}, "sessionId": "s"}))
        for request in reversed(requests):
            await socket.send(
                json.dumps({"id": request["id"], "result": {"echo": request["params"]["value"]}})
            )
        request = json.loads(await socket.recv())
        await socket.send(
            json.dumps({"id": request["id"], "error": {"code": -32000, "message": "SECRET_BODY"}})
        )
        await socket.wait_closed()

    async def client(endpoint: str) -> None:
        async with Cdp(endpoint) as cdp:
            with pytest.raises(ConfigError):
                await cdp.__aenter__()
            commands: list[tuple[str, dict[str, Any]]] = [
                ("", {"SECRET_BODY": "hidden"}),
                ("Test", {"SECRET_BODY": set()}),
            ]
            for method, params in commands:
                with pytest.raises(FetchError) as invalid:
                    await cdp.call(method, params)
                assert "SECRET_BODY" not in str(invalid.value)
                assert invalid.value.__context__ is None or invalid.value.__suppress_context__
            results = await asyncio.gather(
                cdp.call("Test.first", {"value": 1}, session_id="session-1"),
                cdp.call("Test.second", {"value": 2}, session_id="session-1"),
            )
            assert list(results) == [{"echo": 1}, {"echo": 2}]
            event = await cdp.events.get()
            assert event == {"method": "Page.ready", "params": {}, "sessionId": "s"}
            with pytest.raises(FetchError) as caught:
                await cdp.call("Test.error")
            assert "SECRET_BODY" not in str(caught.value)
            assert caught.value.__cause__ is None
        with pytest.raises(NetworkError):
            await cdp.call("Test.afterClose")

    asyncio.run(_with_server(handler, client))


@pytest.mark.parametrize(
    "failure", ["disconnect", "overflow", "malformed", "json", "envelope", "response", "event"]
)
def test_cdp_failure_immediately_rejects_pending_and_closes(failure: str) -> None:
    async def handler(socket: ServerConnection) -> None:
        await socket.recv()
        await socket.recv()
        if failure == "disconnect":
            socket.transport.abort()
        elif failure == "overflow":
            for _ in range(257):
                await socket.send('{"method":"Page.event","params":{}}')
        else:
            await socket.send(
                {
                    "malformed": '{"id":1,"result":"SECRET_BODY"}',
                    "json": '{"id":1,"result":{"SECRET_BODY":NaN}}',
                    "envelope": '["SECRET_BODY"]',
                    "response": '{"id":1,"SECRET_BODY":true}',
                    "event": '{"method":"Page.event","params":"SECRET_BODY"}',
                }[failure]
            )
        await socket.wait_closed()

    async def client(endpoint: str) -> None:
        async with Cdp(endpoint) as cdp:
            results = await asyncio.gather(
                cdp.call("Test.pending1"), cdp.call("Test.pending2"), return_exceptions=True
            )
            for result in results:
                assert isinstance(result, NetworkError if failure == "disconnect" else FetchError)
                assert "SECRET_BODY" not in str(result)
            if failure == "overflow":
                assert cdp.events.maxsize == 256 and cdp.events.qsize() == 256
                assert all("256" in str(result) for result in results)
            with pytest.raises(FetchError):
                await cdp.call("Test.afterFailure")

    asyncio.run(_with_server(handler, client))


def test_cdp_connection_refused_and_closed_calls() -> None:
    async def run() -> None:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            cdp = Cdp(f"ws://127.0.0.1:{listener.getsockname()[1]}/SECRET_BODY")
            with pytest.raises(NetworkError, match="not open"):
                await cdp.call("Test.beforeOpen")
            with pytest.raises(NetworkError) as caught:
                async with cdp:
                    pytest.fail("A non-listening port must reject the connection")
            assert "SECRET_BODY" not in str(caught.value)
            assert caught.value.__suppress_context__
            await cdp.close()
            with pytest.raises(NetworkError, match="closed"):
                await cdp.call("Test.afterClose")
        _assert_no_transport_tasks()

    asyncio.run(run())


def test_cdp_cancelled_call_late_response_and_context_cleanup() -> None:
    async def run() -> None:
        received = asyncio.Event()
        release = asyncio.Event()

        async def handler(socket: ServerConnection) -> None:
            request = json.loads(await socket.recv())
            received.set()
            await release.wait()
            await socket.send(json.dumps({"id": request["id"], "result": {}}))
            request = json.loads(await socket.recv())
            await socket.send(json.dumps({"id": request["id"], "result": {"ok": True}}))
            await socket.wait_closed()

        async def client(endpoint: str) -> None:
            active = asyncio.Event()

            async def owner() -> None:
                async with Cdp(endpoint) as cdp:
                    call = asyncio.create_task(cdp.call("Test.cancel"))
                    await received.wait()
                    call.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await call
                    release.set()
                    assert await cdp.call("Test.afterCancel") == {"ok": True}
                    active.set()
                    await asyncio.Future[None]()

            task = asyncio.create_task(owner())
            await active.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        await _with_server(handler, client)

    asyncio.run(run())
