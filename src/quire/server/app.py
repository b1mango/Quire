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
from . import endpoints
from .jobs import JobConflictError, JobManager

_LOG = logging.getLogger(__name__)

_LOOPBACK = {"127.0.0.1", "::1"}
_MAX_BODY = 65_536
_STATIC = {"app.css": "text/css; charset=utf-8", "app.js": "text/javascript; charset=utf-8"}


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
    ) -> None:
        self.token = token
        self.data_root = data_root.absolute()
        self.settings_path = self.data_root / "settings.json"
        self.manager = manager
        self.opener = opener
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


def make_server(
    host: str,
    port: int,
    *,
    data_root: Path,
    opener: Callable[[Path], None] | None = None,
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
        except JobConflictError as exc:
            self._reply_json(409, {"error": str(exc)})
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
            return self._reply_json(200, endpoints.probe(self.server, self._body()))
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
            if method == "GET" and len(parts) == 4 and parts[3] == "events":
                return self._events(job_id, query)
            if method == "GET" and len(parts) == 5 and parts[3] == "thumbs":
                return self._thumb(job_id, parts[4])
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "books":
            if method == "DELETE":
                return self._reply_json(200, endpoints.delete_book(self.server, parts[2]))
        if method == "POST" and len(parts) == 4 and parts[:2] == ["api", "books"]:
            if parts[3] == "open":
                return self._reply_json(200, endpoints.open_book(self.server, parts[2]))
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
        job = self.server.manager.get(job_id)
        if job is None or not page.isdigit():
            return self._reply_json(404, {"error": "没有这张缩略图"})
        path = self.server.manager.thumbs_root / job.id / f"p{int(page)}.jpg"
        if not path.is_file() or path.is_symlink():
            return self._reply_json(404, {"error": "没有这张缩略图"})
        self._file(path, "image/jpeg")

    def _cover(self, book_id: str) -> None:
        from ..store import library

        try:
            book = library.get_book(self.server.data_root, book_id)
        except LedgerError:
            return self._reply_json(404, {"error": "没有这本书"})
        if not book.cover:
            return self._reply_json(404, {"error": "这本书没有封面"})
        path = self.server.data_root / book.cover
        if not path.is_file() or path.is_symlink() or path.parent.parent != self.server.data_root:
            return self._reply_json(404, {"error": "这本书没有封面"})
        self._file(path, "image/jpeg")

    def _file(self, path: Path, content_type: str) -> None:
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

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
