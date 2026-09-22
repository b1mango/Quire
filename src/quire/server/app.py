"""回环 HTTP 服务：令牌/Origin 校验、REST 路由、SSE 推送与内嵌前端。

安全契约（项目设计.md §3.3、§27.3）：
* 只允许绑定回环地址；
* 每次启动生成随机令牌，所有请求必须携带（``X-Quire-Token`` 头、
  ``?token=`` 查询参数或 ``SameSite=Strict`` Cookie）；
* 带 ``Origin`` 头的请求必须与服务的回环源一致，跨站请求一律 403；
* 错误响应对外只有人话文案，不暴露堆栈与内部路径。
"""

from __future__ import annotations

import json
import logging
import secrets
import subprocess
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ..errors import ConfigError, LedgerError, ParseError, QuireError
from ..store.models import JsonValue
from . import endpoints, files, follows
from .jobs import JobManager

_LOG = logging.getLogger(__name__)

_LOOPBACK = {"127.0.0.1", "::1"}
_MAX_BODY = 65_536
_STATIC = {
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "library.js": "text/javascript; charset=utf-8",
    "run-events.js": "text/javascript; charset=utf-8",
    "capture.js": "text/javascript; charset=utf-8",
    "chapters.js": "text/javascript; charset=utf-8",
    "follows.js": "text/javascript; charset=utf-8",
}


class QuireServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: object, client_address: object) -> None:
        # 客户端中途断开（连接重置/半开连接）不是服务错误，不打堆栈噪声。
        _LOG.debug("client connection error from %s", client_address, exc_info=True)

    def __init__(
        self,
        address: tuple[str, int],
        *,
        token: str,
        data_root: Path,
        manager: JobManager,
        opener: Callable[[Path], None],
        revealer: Callable[[Path], None] | None = None,
    ) -> None:
        self.token = token
        self.data_root = data_root.absolute()
        self.settings_path = self.data_root / "settings.json"
        self.manager = manager
        self.opener = opener
        self.revealer = revealer or _reveal_with_system
        super().__init__(address, _Handler)

    @property
    def origin(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{str(host)}:{port}"

    @property
    def url(self) -> str:
        return f"{self.origin}/?token={self.token}"


def _open_with_system(path: Path) -> None:
    subprocess.Popen(["open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _reveal_with_system(path: Path) -> None:
    subprocess.Popen(
        ["open", "-R", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )


def make_server(
    host: str,
    port: int,
    *,
    data_root: Path,
    opener: Callable[[Path], None] | None = None,
    revealer: Callable[[Path], None] | None = None,
    manager: JobManager | None = None,
) -> QuireServer:
    if host not in _LOOPBACK:
        raise ConfigError("Web UI 只允许绑定回环地址（127.0.0.1）")
    data_root = data_root.absolute()
    data_root.mkdir(parents=True, exist_ok=True)
    return QuireServer(
        (host, port),
        token=secrets.token_urlsafe(24),
        data_root=data_root,
        manager=manager or JobManager(data_root),
        opener=opener or _open_with_system,
        revealer=revealer,
    )


def _json_bytes(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    server: QuireServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        _LOG.debug("http " + fmt, *args)

    # ------------------------------------------------------------ 入口

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        try:
            self._route(method)
        except ParseError as exc:
            self._reply_json(422, {"error": exc.message, "hint": exc.hint})
        except (ConfigError, LedgerError) as exc:
            self._reply_json(400, {"error": exc.message, "hint": exc.hint})
        except QuireError as exc:
            self._reply_json(400, {"error": exc.message, "hint": exc.hint})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            _LOG.exception("unhandled request error")
            self._reply_json(500, {"error": "服务器内部错误"})

    # ------------------------------------------------------------ 安全

    def _authorized(self, query: dict[str, list[str]]) -> bool:
        header = self.headers.get("X-Quire-Token", "")
        cookie = ""
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "quire_token":
                cookie = value
        candidates = (header, query.get("token", [""])[0], cookie)
        return any(secrets.compare_digest(c, self.server.token) for c in candidates if c)

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        return origin is None or origin == self.server.origin

    def _require_auth(self, query: dict[str, list[str]]) -> bool:
        if not self._origin_ok():
            self._reply_json(403, {"error": "拒绝跨站请求"})
            return False
        if not self._authorized(query):
            self._reply_json(401, {"error": "缺少或错误的访问令牌"})
            return False
        return True

    # ------------------------------------------------------------ 路由

    def _route(self, method: str) -> None:
        split = urlsplit(self.path)
        path = split.path
        query = parse_qs(split.query)
        if not self._require_auth(query):
            return
        if method == "GET" and path == "/":
            return self._index()
        if method == "GET" and path.startswith("/static/"):
            return self._static(path.removeprefix("/static/"))
        if method == "GET" and path == "/api/capabilities":
            return self._reply_json(200, endpoints.capabilities(self.server))
        if path == "/api/settings":
            if method == "GET":
                return self._reply_json(200, endpoints.get_settings(self.server))
            if method == "PUT":
                return self._reply_json(200, endpoints.put_settings(self.server, self._body()))
        if method == "POST" and path == "/api/probe":
            return self._probe()
        if path == "/api/jobs":
            if method == "GET":
                return self._reply_json(200, endpoints.list_jobs(self.server))
            if method == "POST":
                return self._reply_json(201, endpoints.submit_job(self.server, self._body()))
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "jobs":
            job_id = parts[2]
            if method == "GET" and len(parts) == 3:
                return self._job(job_id)
            if method == "POST" and len(parts) == 4 and parts[3] == "cancel":
                return self._cancel(job_id)
            if method == "POST" and len(parts) == 4 and parts[3] == "pause":
                return self._pause(job_id)
            if method == "GET" and len(parts) == 4 and parts[3] == "events":
                return self._events(job_id, query)
            if method == "GET" and len(parts) == 5 and parts[3] == "thumbs":
                return self._thumb(job_id, parts[4])
        if method == "POST" and len(parts) == 3 and parts[:2] == ["api", "books"]:
            if parts[2] == "check-all-updates":
                return self._reply_json(200, follows.check_all(self.server))
            if parts[2] == "batch":
                return self._reply_json(200, endpoints.batch_books(self.server, self._body()))
        if len(parts) >= 2 and parts[:2] == ["api", "groups"]:
            if method == "POST" and len(parts) == 2:
                return self._reply_json(201, endpoints.create_group(self.server, self._body()))
            if len(parts) == 3:
                if method == "PUT":
                    return self._reply_json(
                        200, endpoints.rename_group(self.server, parts[2], self._body())
                    )
                if method == "DELETE":
                    return self._reply_json(200, endpoints.delete_group(self.server, parts[2]))
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "books":
            if method == "DELETE":
                return self._reply_json(200, endpoints.delete_book(self.server, parts[2]))
        if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "books"]:
            if parts[3] == "open":
                return self._reply_json(200, endpoints.open_book(self.server, parts[2]))
            if parts[3] == "reveal":
                return self._reply_json(200, endpoints.reveal_book(self.server, parts[2]))
            if parts[3] == "check-update":
                return self._reply_json(200, follows.check_update(self.server, parts[2]))
            if parts[3] == "follow":
                return self._reply_json(201, follows.follow_submit(self.server, parts[2]))
        if method == "GET" and len(parts) == 4 and parts[:2] == ["api", "books"]:
            if parts[3] == "cover":
                return self._cover(parts[2])
        if method == "GET" and path == "/api/books":
            search = query.get("search", [""])[0]
            return self._reply_json(200, endpoints.list_books(self.server, search))
        self._reply_json(404, {"error": "没有这个接口"})

    # ------------------------------------------------------------ 处理

    def _index(self) -> None:
        body = _web_bytes("index.html")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Set-Cookie",
            f"quire_token={self.server.token}; SameSite=Strict; Path=/; HttpOnly",
        )
        self.end_headers()
        self.wfile.write(body)

    def _static(self, name: str) -> None:
        if name not in _STATIC:
            return self._reply_json(404, {"error": "没有这个资源"})
        body = _web_bytes(name)
        self.send_response(200)
        self.send_header("Content-Type", _STATIC[name])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> object:
        length = int(self.headers.get("Content-Length") or 0)
        if length > _MAX_BODY:
            self.close_connection = True  # 未读走的正文不能留在 keep-alive 连接上
            raise ConfigError("请求体过大")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise ConfigError("请求体不是有效的 JSON") from None

    def _probe(self) -> None:
        """流式识别：首帧才发响应头（之前的校验失败仍走普通 JSON 错误码）。

        阶段帧让前端能显示「请求目录 → 识别章节 → 估算体积」的进度文案；
        识别中途的失败以 ``{"error": …}`` 帧收尾（项目设计.md §3.3）。
        """
        started = False

        def send_frame(obj: dict[str, JsonValue]) -> None:
            nonlocal started
            if not started:
                started = True
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
            self.wfile.write(_json_bytes(obj) + b"\n")
            self.wfile.flush()

        endpoints.probe_stream(self.server, self._body(), send_frame)

    def _job(self, job_id: str) -> None:
        job = self.server.manager.get(job_id)
        if job is None:
            return self._reply_json(404, {"error": "没有这个任务"})
        self._reply_json(200, job.snapshot())

    def _cancel(self, job_id: str) -> None:
        job = self.server.manager.cancel(job_id)
        if job is None:
            return self._reply_json(404, {"error": "没有这个任务"})
        self._reply_json(200, job.snapshot())

    def _pause(self, job_id: str) -> None:
        job = self.server.manager.pause(job_id)
        if job is None:
            return self._reply_json(404, {"error": "没有这个任务"})
        self._reply_json(200, job.snapshot())

    def _events(self, job_id: str, query: dict[str, list[str]]) -> None:
        job = self.server.manager.get(job_id)
        if job is None:
            return self._reply_json(404, {"error": "没有这个任务"})
        try:
            after = int(query.get("after", ["0"])[0])
        except ValueError:
            after = 0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        while True:
            events = job.events_after(after, timeout=15)
            if not events:
                self.wfile.write(b": ping\n\n")
                self.wfile.flush()
                continue
            for event in events:
                payload = _json_bytes(event.data)
                self.wfile.write(
                    f"id: {event.seq}\nevent: {event.kind}\n".encode()
                    + b"data: "
                    + payload
                    + b"\n\n"
                )
                after = event.seq
            self.wfile.flush()
            if job.status not in {"pending", "running"}:
                return

    def _thumb(self, job_id: str, page: str) -> None:
        files.thumb(self, job_id, page)

    def _cover(self, book_id: str) -> None:
        files.cover(self, book_id)

    def _reply_json(self, status: int, payload: dict[str, JsonValue]) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


def _web_bytes(name: str) -> bytes:
    return (resources.files("quire.server.web") / name).read_bytes()
