"""Discovery and lifetime management for an isolated, owned Chrome process."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

from ..errors import ConfigError, FetchError, NetworkError

_START_TIMEOUT = 10.0
_STOP_TIMEOUT = 1.0
_CHROME_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)
_CHROME_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")


def _executable(path: str) -> str | None:
    candidate = Path(path).expanduser()
    try:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
    except (OSError, ValueError):
        return None
    return None


def find_chrome(path: str | None = None) -> str | None:
    if path is not None:
        found = _executable(path)
        if found is None:
            raise ConfigError("Chrome path must refer to an executable file")
        return found
    for candidate in _CHROME_PATHS:
        if found := _executable(candidate):
            return found
    for name in _CHROME_NAMES:
        if (on_path := shutil.which(name)) and (found := _executable(on_path)):
            return found
    return None


class ChromeProcess:
    def __init__(
        self,
        executable: str,
        *,
        extra_args: tuple[str, ...] = (),
        proxy_bypass: str = "",
    ) -> None:
        """隔离 Chrome 进程。

        默认所有浏览器流量打进黑洞代理由 Fetch 拦截层代发;
        ``proxy_bypass``(分号分隔的域名单)是站点适配的显式例外——
        仅当页面接口绑定浏览器自身 TLS/指纹上下文、代理重放必败时
        (实测:B 站漫画 twirp 接口)才放行这些域名直连,其余流量照旧。
        """
        self.executable = executable
        self.extra_args = extra_args
        self.proxy_bypass = proxy_bypass
        self.endpoint = ""
        self.profile: Path | None = None
        self.pid: int | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._entered = False

    async def __aenter__(self) -> ChromeProcess:
        if self._entered or self._close_task is not None:
            raise ConfigError("ChromeProcess cannot be entered more than once")
        if os.name != "posix":
            raise ConfigError("Isolated Chrome process groups require a POSIX system")
        self._entered = True
        try:
            self._temporary = tempfile.TemporaryDirectory(prefix="quire-chrome-")
            self.profile = Path(self._temporary.name)
            # Synchronous spawn records ownership before cancellation can be delivered.
            bypass = "<-loopback>" + (f";{self.proxy_bypass}" if self.proxy_bypass else "")
            self._process = subprocess.Popen(
                [
                    self.executable,
                    f"--user-data-dir={self.profile}",
                    "--headless=new",
                    "--remote-debugging-port=0",
                    "--remote-debugging-address=127.0.0.1",
                    "--no-first-run",
                    "--no-default-browser-check",
                    # 沙箱/无钥匙串环境下启动会弹系统钥匙串告警框,永久卡住进程;
                    # 配置目录本就一次性隔离,内存 mock 钥匙串即可。
                    "--use-mock-keychain",
                    "--disable-background-networking",
                    "--disable-sync",
                    "--disable-extensions",
                    "--disable-component-update",
                    "--disable-default-apps",
                    "--disable-quic",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                    "--proxy-server=http://127.0.0.1:9",
                    f"--proxy-bypass-list={bypass}",
                    *self.extra_args,
                    "about:blank",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.pid = self._process.pid
            async with asyncio.timeout(_START_TIMEOUT):
                while True:
                    if self._process.poll() is not None:
                        raise NetworkError("Chrome exited before its debugging endpoint was ready")
                    if endpoint := self._read_endpoint():
                        self.endpoint = endpoint
                        return self
                    await asyncio.sleep(0.02)
        except BaseException as exc:
            await self.close()
            if isinstance(exc, TimeoutError):
                raise FetchError("Chrome debugging endpoint startup timed out") from None
            if isinstance(exc, (OSError, ValueError)):
                raise NetworkError("Unable to start isolated Chrome") from None
            raise

    def _read_endpoint(self) -> str | None:
        assert self.profile is not None
        try:
            with (self.profile / "DevToolsActivePort").open(encoding="ascii") as stream:
                text = stream.read(4097)
        except FileNotFoundError:
            return None
        except UnicodeError:
            raise FetchError("Invalid Chrome debugging endpoint file") from None
        lines = text.splitlines()
        if len(text) > 4096:
            raise FetchError("Invalid Chrome debugging endpoint file")
        if len(lines) < 2:
            return None
        if (
            len(lines) != 2
            or not lines[0].isascii()
            or not lines[0].isdigit()
            or not 1 <= int(lines[0]) <= 65535
            or re.fullmatch(r"/devtools/browser/[A-Za-z0-9_-]+", lines[1]) is None
        ):
            raise FetchError("Invalid Chrome debugging endpoint file")
        return f"ws://127.0.0.1:{lines[0]}{lines[1]}"

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._cleanup(), name="quire-chrome-cleanup")
        cancelled = False
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError:
                cancelled = True
        self._close_task.result()
        if cancelled:
            raise asyncio.CancelledError

    def _signal_group(self, sig: int) -> bool:
        assert self._process is not None
        try:
            os.killpg(self._process.pid, sig)
        except ProcessLookupError:
            return False
        return True

    async def _cleanup(self) -> None:
        try:
            if self._process is not None:
                self._signal_group(signal.SIGTERM)
                deadline = asyncio.get_running_loop().time() + _STOP_TIMEOUT
                # macOS can deny killpg(..., 0) once only sandboxed helpers remain.
                # Reap our owned parent; Chrome normally shuts its helpers down on TERM.
                while self._process.poll() is None:
                    if asyncio.get_running_loop().time() >= deadline:
                        self._signal_group(signal.SIGKILL)
                        break
                    await asyncio.sleep(0.02)
                await asyncio.to_thread(self._process.wait)
        except OSError:
            raise NetworkError("Unable to clean up isolated Chrome process") from None
        finally:
            if self._temporary is not None:
                self._temporary.cleanup()
