"""Local, token-authenticated control panel. Never opens production actions."""

from __future__ import annotations

import hmac
import ipaddress
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

STATIC_ROOT = Path(__file__).with_name("static")
MAX_BODY = 16_384
CONTROLS = frozenset({"pause", "stop", "resume", "revoke", "takeover"})
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = True
    allow_reuse_address = False

    def __init__(self, address: tuple[str, int], controller: Any):
        self.controller = controller
        self.access_token = secrets.token_urlsafe(32)
        self._clients = threading.BoundedSemaphore(16)
        super().__init__(address, Handler)
        self.origin = f"http://127.0.0.1:{self.server_port}"

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(5)
        return connection, address

    def process_request(self, request, client_address):
        if not self._clients.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._clients.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._clients.release()

    def handle_error(self, request, client_address):
        # Disconnects and malformed requests do not leak request context.
        pass


class Handler(BaseHTTPRequestHandler):
    server: LocalServer
    server_version = "LocalControl"
    sys_version = ""
    protocol_version = "HTTP/1.0"

    def log_message(self, *_args):
        # Request paths, fragments supplied by malformed clients, and tokens
        # must never become HTTP logs. The controller maintains its own audit.
        pass

    def send_error(self, code, message=None, explain=None):
        # The stdlib parser otherwise reflects malformed request lines into an
        # HTML error. Keep even parser failures generic and security-headered.
        self._error(code, "request_rejected", "请求格式或方式不受支持。")

    def _send(self, status: int, body: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'; object-src 'none'",
        )
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, data: Any):
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, code: str, message: str):
        self._json(status, {"error": {"code": code, "message": message}})

    def _boundary(self, *, mutation: bool = False) -> bool:
        # BaseHTTPRequestHandler normalizes leading // before dispatch. Exact
        # routing also checks the original request target, including that case.
        if self.requestline.split()[1] != self.path:
            self._error(404, "not_found", "没有这个入口。")
            return False
        if not ipaddress.ip_address(self.client_address[0]).is_loopback:
            self._error(403, "local_only", "仅允许本机访问。")
            return False
        hosts = self.headers.get_all("Host", [])
        if hosts != [f"127.0.0.1:{self.server.server_port}"]:
            self._error(403, "untrusted_host", "访问地址不受信任，请使用本机启动地址。")
            return False
        origins = self.headers.get_all("Origin", [])
        if len(origins) > 1 or (origins and origins != [self.server.origin]):
            self._error(403, "untrusted_origin", "请求来源不受信任。")
            return False
        if mutation and origins != [self.server.origin]:
            self._error(403, "origin_required", "控制操作必须来自本机面板。")
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self._error(403, "cross_site", "不接受其他网站发起的请求。")
            return False
        return True

    def _authenticated(self) -> bool:
        values = self.headers.get_all("X-AI-CEO-Token", [])
        if len(values) != 1 or not hmac.compare_digest(
            values[0].encode("utf-8"), self.server.access_token.encode("ascii")
        ):
            self._error(401, "session_required", "请从本地启动输出重新打开带访问凭证的地址。")
            return False
        return True

    def do_GET(self):
        if not self._boundary():
            return
        if self.path in STATIC_FILES:
            filename, content_type = STATIC_FILES[self.path]
            try:
                body = (STATIC_ROOT / filename).read_bytes()
            except OSError:
                self._error(503, "panel_unavailable", "面板文件不可用，请检查本地安装。")
                return
            self._send(200, body, content_type)
            return
        if self.path not in {"/api/state", "/api/authstate"}:
            self._error(404, "not_found", "没有这个入口。")
            return
        if not self._authenticated():
            return
        try:
            result = (
                {"authenticated": True}
                if self.path == "/api/authstate"
                else self.server.controller.snapshot()
            )
            self._json(200, result)
        except Exception:
            self._error(503, "state_unavailable", "暂时无法读取状态；不要据此判断系统已经恢复。")

    def _body(self) -> dict | None:
        if self.headers.get("Transfer-Encoding"):
            self._error(400, "unsupported_encoding", "不接受分块请求。")
            return None
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
            self._error(411, "length_required", "请求长度缺失或无效。")
            return None
        if len(lengths[0]) > 6:
            self._error(413, "body_too_large", "请求内容超过允许大小。")
            return None
        length = int(lengths[0])
        if length > MAX_BODY:
            self._error(413, "body_too_large", "请求内容超过允许大小。")
            return None
        if self.headers.get_content_type() != "application/json":
            self._error(415, "json_required", "只接受 JSON 请求。")
            return None
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("incomplete body")
            def unique_object(pairs):
                value = {}
                for key, item in pairs:
                    if key in value:
                        raise ValueError("duplicate key")
                    value[key] = item
                return value
            value = json.loads(
                raw.decode("utf-8"), object_pairs_hook=unique_object,
                parse_constant=lambda _constant: (_ for _ in ()).throw(ValueError("constant")),
            )
            if not isinstance(value, dict):
                raise ValueError("object required")
            return value
        except (ValueError, UnicodeError, TimeoutError, RecursionError):
            self._error(400, "invalid_json", "请求内容格式不正确。")
            return None

    def do_POST(self):
        if not self._boundary(mutation=True) or not self._authenticated():
            return
        if self.path not in {"/api/control", "/api/observe", "/api/replay"}:
            self._error(404, "not_found", "没有这个入口。")
            return
        body = self._body()
        if body is None:
            return
        if self.path == "/api/control":
            action = body.get("action")
            asset_id = body.get("asset_id")
            if (
                set(body) - {"action", "asset_id"}
                or not isinstance(action, str)
                or action not in CONTROLS
                or (asset_id is not None and (
                    not isinstance(asset_id, str) or not 1 <= len(asset_id) <= 80
                    or not all(c.isascii() and (c.isalnum() or c in "-_") for c in asset_id)
                ))
                or (action in {"revoke", "takeover"} and asset_id is None)
                or (action in {"pause", "stop", "resume"} and asset_id is not None)
            ):
                self._error(400, "invalid_control", "控制操作或项目标识无效。")
                return
        elif body:
            self._error(400, "unexpected_fields", "本操作不接受额外参数。")
            return
        try:
            if self.path == "/api/control":
                result = self.server.controller.control(action, asset_id)
            elif self.path == "/api/observe":
                result = self.server.controller.observe()
            else:
                result = self.server.controller.replay()
            self._json(200, result)
        except PermissionError:
            self._error(403, "action_not_authorized", "当前授权不允许这项操作；既有权限不会自动扩大。")
        except (ValueError, KeyError):
            self._error(409, "action_conflict", "当前状态不允许这项操作。请刷新后查看授权与接管状态。")
        except Exception:
            self._error(503, "action_incomplete", "操作尚未确认完成，请刷新状态核对；不要直接重复操作。")

    def do_HEAD(self):
        if self._boundary():
            self._error(405, "method_not_allowed", "不支持此请求方式。")

    do_OPTIONS = do_HEAD
    do_PUT = do_HEAD
    do_PATCH = do_HEAD
    do_DELETE = do_HEAD


def serve(controller: Any, host: str = "127.0.0.1", port: int = 0) -> LocalServer:
    """Construct a local server; the caller decides when to serve and stop it."""
    if host != "127.0.0.1":
        raise ValueError("Only 127.0.0.1 binding is permitted")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
        raise ValueError("Invalid port")
    return LocalServer((host, port), controller)
