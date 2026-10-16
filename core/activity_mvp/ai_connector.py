from __future__ import annotations

import json
import os
import signal
import sqlite3
import socket
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__ as CORE_VERSION
from .activity_confirmations import confirmed_activity_view
from .ai_handoff import (
    DEFAULT_MAX_ITEMS,
    downstream_ai_rules,
    privacy_boundary,
    render_ai_handoff_markdown,
    resolve_ai_handoff_range,
    validate_ai_handoff_payload,
)
from .diagnostics import diagnostics_status
from .document_changes import document_authorization_lock, document_context_projections
from .local_endpoints import is_loopback_host
from .storage import ensure_ready


AI_CONNECTOR_VERSION = "0.9.1"
DEFAULT_AI_CONNECTOR_HOST = "127.0.0.1"
DEFAULT_AI_CONNECTOR_PORT = 8766
AI_CONNECTOR_PID_FILENAME = "ai_connector.pid"
AI_CONNECTOR_STARTUP_TIMEOUT_SECONDS = 3.0
VALID_PACKAGE_FORMATS = {"json", "markdown"}


def ai_connector_status(
    conn: sqlite3.Connection,
    root_dir: str | Path,
    *,
    host: str = DEFAULT_AI_CONNECTOR_HOST,
    port: int = DEFAULT_AI_CONNECTOR_PORT,
    server_running: bool = False,
) -> dict:
    _validate_loopback_host(host)
    root = Path(root_dir)
    managed = _managed_process_status(root, host=host, port=port)
    server_running = bool(server_running or managed["server_running"])
    return {
        "status": "ok",
        "interface": "ai-connector",
        "version": AI_CONNECTOR_VERSION,
        "core_version": CORE_VERSION,
        "mode": "managed_loopback_read_only",
        "managed_by_app": True,
        "server_running": server_running,
        "pid": managed.get("pid") if server_running else None,
        "listener_status": managed["listener_status"],
        "port_conflict": bool(managed["port_conflict"]),
        "stale_pid": bool(managed["stale_pid"]),
        "base_url": f"http://{host}:{port}",
        "endpoints": [
            {"method": "GET", "path": "/status", "description": "Connector status and safety boundary."},
            {"method": "GET", "path": "/package?range=today&format=json", "description": "Safe AI handoff package."},
            {"method": "GET", "path": "/diagnostics?mode=quick", "description": "Fast safe local diagnostics package."},
            {"method": "GET", "path": "/diagnostics?mode=full", "description": "Full safe local diagnostics package with probe timings."},
        ],
        "start_command": f"python3 -m activity_mvp ai-connector start --host {host} --port {port}",
        "stop_command": f"python3 -m activity_mvp ai-connector stop --host {host} --port {port}",
        "available_ranges": ["7d", "today", "yesterday"],
        "available_formats": sorted(VALID_PACKAGE_FORMATS),
        "safety_boundary": _safety_boundary(),
        "raw_access": False,
        "destructive_actions": False,
        "memory_write_actions": False,
        "rule_write_actions": False,
        "cloud_analysis": "off",
    }


def start_ai_connector(
    db_path: str | Path,
    root_dir: str | Path | None = None,
    *,
    host: str = DEFAULT_AI_CONNECTOR_HOST,
    port: int = DEFAULT_AI_CONNECTOR_PORT,
    timeout_seconds: float = AI_CONNECTOR_STARTUP_TIMEOUT_SECONDS,
) -> dict:
    _validate_loopback_host(host)
    db = Path(db_path)
    root = Path(root_dir) if root_dir is not None else db.parent
    _ensure_connector_runtime_dirs(root)
    conn = ensure_ready(db)
    conn.close()

    current = _managed_process_status(root, host=host, port=port)
    if current["server_running"]:
        return _connector_action_status(root, host, port, "start", "already_running", "AI Connector 已在受控进程中运行。")
    if current["managed_pid_alive"]:
        return _connector_action_status(root, host, port, "start", "managed_process_unhealthy", "受控 Connector 进程存在但端口尚未就绪；未重复启动。", status="blocked")
    if current["stale_pid"]:
        _pidfile_path(root).unlink(missing_ok=True)
    if current["port_conflict"] or _port_accepts_connection(host, port):
        return _connector_action_status(root, host, port, "start", "port_conflict", "端口已被其它进程占用；未启动 Connector。", status="blocked")

    stdout, stderr = _open_connector_logs(root)
    command = [
        sys.executable,
        "-m",
        "activity_mvp",
        "--db",
        str(db),
        "ai-connector",
        "serve",
        "--host",
        host,
        "--port",
        str(port),
    ]
    env = os.environ.copy()
    package_root = Path(__file__).resolve().parents[1]
    env["PYTHONPATH"] = str(package_root) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PERSONAL_AI_MONITOR_AI_CONNECTOR_MANAGED"] = "1"
    process = subprocess.Popen(
        command,
        cwd=str(package_root),
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        env=env,
        start_new_session=True,
        close_fds=True,
    )
    process._child_created = False  # The managed connector outlives this short control command.
    stdout.close()
    stderr.close()
    _write_pidfile(root, process.pid, host, port, db, command)

    deadline = time.time() + max(0.1, timeout_seconds)
    while time.time() < deadline:
        if process.poll() is not None:
            _pidfile_path(root).unlink(missing_ok=True)
            return _connector_action_status(root, host, port, "start", "process_exited", "Connector 启动后立即退出；请查看本地日志。", status="blocked")
        if _port_accepts_connection(host, port):
            return _connector_action_status(root, host, port, "start", "started", "AI Connector 已手动启动。")
        time.sleep(0.1)
    return _connector_action_status(root, host, port, "start", "starting", "AI Connector 进程已启动，端口仍在就绪中。")


def stop_ai_connector(
    root_dir: str | Path,
    *,
    host: str = DEFAULT_AI_CONNECTOR_HOST,
    port: int = DEFAULT_AI_CONNECTOR_PORT,
    timeout_seconds: float = 3.0,
) -> dict:
    _validate_loopback_host(host)
    root = Path(root_dir)
    record = _read_pidfile(root)
    pid = _record_pid(record)
    if not pid:
        return _connector_action_status(root, host, port, "stop", "not_running", "没有受控 Connector PID。")
    if not _process_alive(pid):
        _pidfile_path(root).unlink(missing_ok=True)
        return _connector_action_status(root, host, port, "stop", "stale_pid_removed", "已清理过期 Connector PID。")
    command = _command_for_pid(pid)
    if not _managed_command_matches(command, host=host, port=port):
        return _connector_action_status(root, host, port, "stop", "refused_unmanaged_pid", "PID 不属于受控 AI Connector；未停止任何进程。", status="blocked")

    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + max(0.1, timeout_seconds)
    while time.time() < deadline:
        if not _process_alive(pid):
            _pidfile_path(root).unlink(missing_ok=True)
            return _connector_action_status(root, host, port, "stop", "stopped", "AI Connector 已停止。")
        time.sleep(0.1)
    if _process_alive(pid):
        os.kill(pid, signal.SIGKILL)
    _pidfile_path(root).unlink(missing_ok=True)
    return _connector_action_status(root, host, port, "stop", "force_stopped", "AI Connector 未及时退出，已停止受控进程。")


def build_ai_connector_package(
    conn: sqlite3.Connection,
    *,
    range_name: str = "today",
    max_items: int = DEFAULT_MAX_ITEMS,
) -> dict:
    with document_authorization_lock(conn, exclusive=False):
        return _build_ai_connector_package_locked(
            conn,
            range_name=range_name,
            max_items=max_items,
        )


def _build_ai_connector_package_locked(
    conn: sqlite3.Connection,
    *,
    range_name: str = "today",
    max_items: int = DEFAULT_MAX_ITEMS,
) -> dict:
    start, end, range_label = resolve_ai_handoff_range(range_name)
    confirmation_view = confirmed_activity_view(conn, start, end)
    confirmed_activities = list(confirmation_view.get("trusted_activities") or [])
    counts = _connector_counts(
        conn,
        start,
        end,
        confirmed_activities=confirmed_activities,
        pending_confirmation_count=int(confirmation_view.get("pending_confirmation_count") or 0),
    )
    samples = _connector_samples(confirmed_activities, max_items=max_items)
    document_projections = document_context_projections(conn, start, end, limit=max_items)
    owner_local_documents = document_projections["owner_local_projection"]
    trusted_documents = document_projections["trusted_projection"]
    app_counter: Counter[str] = Counter()
    domain_counter: Counter[str] = Counter()
    for item in samples:
        if item.get("app"):
            app_counter[str(item["app"])] += 1
        if item.get("domain"):
            domain_counter[str(item["domain"])] += 1
    payload = {
        "status": "ok",
        "interface": "ai-handoff",
        "package_mode": "connector_lightweight",
        "version": CORE_VERSION,
        "source": "local_confirmed_activity_only",
        "cloud_analysis": "off",
        "raw_access": False,
        "destructive_actions": False,
        "memory_write_actions": False,
        "rule_write_actions": False,
        "confirmation_gate": "confirmed_activity_view",
        "pending_confirmation_count": int(confirmation_view.get("pending_confirmation_count") or 0),
        "time_range": {"label": range_label, "from": start, "to": end},
        "summary": counts,
        "data_trust": _connector_data_trust(counts),
        "privacy_boundary": privacy_boundary(),
        "top_apps": _top_rows(app_counter),
        "top_domains": _top_rows(domain_counter),
        "project_summaries": _connector_project_summaries(samples),
        "decision_signals": [],
        "follow_ups": [],
        "blockers": [],
        "safe_activity_samples": samples,
        "document_changes": trusted_documents,
        "document_unverified_clue_count": len(owner_local_documents.get("items") or []),
        "downstream_ai_rules": downstream_ai_rules(),
    }
    scan = validate_ai_handoff_payload(payload)
    if not scan["ok"]:
        return {
            "status": "blocked",
            "interface": "ai-handoff",
            "package_mode": "connector_lightweight",
            "version": CORE_VERSION,
            "raw_access": False,
            "destructive_actions": False,
            "memory_write_actions": False,
            "rule_write_actions": False,
            "cloud_analysis": "off",
            "time_range": payload["time_range"],
            "privacy_scan": scan,
        }
    payload["privacy_scan"] = scan
    return payload


def create_ai_connector_server(db_path: str | Path, host: str = DEFAULT_AI_CONNECTOR_HOST, port: int = DEFAULT_AI_CONNECTOR_PORT) -> ThreadingHTTPServer:
    _validate_loopback_host(host)
    db = str(db_path)
    conn = ensure_ready(db)
    conn.close()

    class Handler(AIConnectorHandler):
        database_path = db
        bind_host = host
        bind_port = port

    httpd = ThreadingHTTPServer((host, port), Handler)
    Handler.bind_port = int(httpd.server_address[1])
    return httpd


def serve_ai_connector(db_path: str | Path, host: str = DEFAULT_AI_CONNECTOR_HOST, port: int = DEFAULT_AI_CONNECTOR_PORT) -> None:
    httpd = create_ai_connector_server(db_path, host, port)
    actual_host, actual_port = httpd.server_address
    print(f"AGI read-only AI connector: http://{actual_host}:{actual_port}")
    httpd.serve_forever()


class AIConnectorHandler(BaseHTTPRequestHandler):
    database_path = "data/activity.sqlite"
    bind_host = DEFAULT_AI_CONNECTOR_HOST
    bind_port = DEFAULT_AI_CONNECTOR_PORT

    def log_message(self, format: str, *args) -> None:
        return

    def do_OPTIONS(self) -> None:
        if not self._authorized():
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_common_headers("application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:
        if not self._authorized():
            return
        conn: sqlite3.Connection | None = None
        try:
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            conn = ensure_ready(self.database_path)
            root = Path(self.database_path).parent
            if parsed.path == "/status":
                self._send_json(ai_connector_status(conn, root, host=self.bind_host, port=self.bind_port, server_running=True))
                return
            if parsed.path == "/package":
                package_format = _first(qs, "format", "json")
                if package_format not in VALID_PACKAGE_FORMATS:
                    self._send_json(_error("invalid_format", "format must be json or markdown"), HTTPStatus.BAD_REQUEST)
                    return
                package = build_ai_connector_package(
                    conn,
                    range_name=_first(qs, "range", "today"),
                    max_items=_int_param(qs, "max_items", 40, minimum=1, maximum=100),
                )
                if package_format == "markdown":
                    self._send_text(render_ai_handoff_markdown(package), "text/markdown; charset=utf-8")
                    return
                self._send_json(package)
                return
            if parsed.path == "/diagnostics":
                mode = _first(qs, "mode", "quick")
                if mode not in {"quick", "full"}:
                    self._send_json(_error("invalid_mode", "mode must be quick or full"), HTTPStatus.BAD_REQUEST)
                    return
                self._send_json(diagnostics_status(conn, root, self.database_path, mode=mode))
                return
            self._send_json(_error("not_found", "unknown endpoint"), HTTPStatus.NOT_FOUND)
        except Exception as exc:
            # The connector is a public-to-local boundary. Exception messages can
            # contain SQLite details or absolute paths, so keep the response
            # generic and log only the exception class for local diagnosis.
            self.log_error("request failed: %s", type(exc).__name__)
            self._send_json(
                _error("request_failed", "The local request could not be completed."),
                HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        finally:
            if conn is not None:
                conn.close()

    def do_POST(self) -> None:
        self._reject_write()

    def do_PUT(self) -> None:
        self._reject_write()

    def do_PATCH(self) -> None:
        self._reject_write()

    def do_DELETE(self) -> None:
        self._reject_write()

    def _reject_write(self) -> None:
        if not self._authorized():
            return
        self._send_json(_error("read_only", "AI connector accepts only GET requests."), HTTPStatus.METHOD_NOT_ALLOWED)

    def _authorized(self) -> bool:
        if not _client_allowed(self.client_address[0]):
            self.send_error(HTTPStatus.FORBIDDEN, "Client rejected")
            return False
        if not _host_allowed(self.headers.get("Host"), self.bind_port):
            self.send_error(HTTPStatus.FORBIDDEN, "Host rejected")
            return False
        origin = self.headers.get("Origin")
        if origin and not _origin_allowed(origin):
            self.send_error(HTTPStatus.FORBIDDEN, "Origin rejected")
            return False
        return True

    def _send_json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self._send_common_headers("application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(encoded)

    def _send_text(self, text: str, content_type: str) -> None:
        encoded = text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self._send_common_headers(content_type)
        self.end_headers()
        self.wfile.write(encoded)

    def _send_common_headers(self, content_type: str) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        origin = self.headers.get("Origin")
        if origin and _origin_allowed(origin):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")


def _safety_boundary() -> dict:
    return {
        "bind": "loopback_only",
        "methods": ["GET"],
        "raw_access": False,
        "destructive_actions": False,
        "memory_write_actions": False,
        "rule_write_actions": False,
        "cloud_analysis": "off",
        "excludes": [
            "raw screenshots",
            "raw OCR",
            "SQLite raw activity tables",
            "full local paths",
            "tokens",
            "private chat transcripts",
            "HA package bodies",
        ],
    }


def _connector_action_status(root: Path, host: str, port: int, action: str, action_status: str, message: str, *, status: str = "ok") -> dict:
    managed = _managed_process_status(root, host=host, port=port)
    payload = {
        "action": action,
        "action_status": action_status,
        "message": message,
        **ai_connector_status(_NullConnection(), root, host=host, port=port),
    }
    payload["status"] = status
    payload["listener_status"] = managed["listener_status"]
    payload["port_conflict"] = bool(managed["port_conflict"] or action_status == "port_conflict")
    return payload


class _NullConnection:
    pass


def _ensure_connector_runtime_dirs(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _chmod_private(root, directory=True)
    log_dir = root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    _chmod_private(log_dir, directory=True)


def _open_connector_logs(root: Path):
    log_dir = root / "logs"
    stdout = (log_dir / "ai_connector.out.log").open("ab")
    stderr = (log_dir / "ai_connector.err.log").open("ab")
    _chmod_private(log_dir / "ai_connector.out.log")
    _chmod_private(log_dir / "ai_connector.err.log")
    return stdout, stderr


def _chmod_private(path: Path, *, directory: bool = False) -> None:
    try:
        path.chmod(0o700 if directory else 0o600)
    except OSError:
        pass


def _pidfile_path(root: Path) -> Path:
    return root / AI_CONNECTOR_PID_FILENAME


def _write_pidfile(root: Path, pid: int, host: str, port: int, db: Path, command: list[str]) -> None:
    payload = {
        "pid": int(pid),
        "host": host,
        "port": int(port),
        "db_name": db.name,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "version": AI_CONNECTOR_VERSION,
        "command": " ".join(command),
    }
    path = _pidfile_path(root)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _chmod_private(path)


def _read_pidfile(root: Path) -> dict:
    try:
        path = _pidfile_path(root)
        if not path.exists():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _record_pid(record: dict) -> int | None:
    try:
        pid = int(record.get("pid") or 0)
    except (TypeError, ValueError):
        return None
    return pid if pid > 0 else None


def _managed_process_status(root: Path, *, host: str, port: int) -> dict:
    record = _read_pidfile(root)
    pid = _record_pid(record)
    pid_alive = bool(pid and _process_alive(pid))
    command = _command_for_pid(pid) if pid_alive and pid else ""
    command_matches = bool(pid_alive and _managed_command_matches(command, host=host, port=port))
    listener_open = _port_accepts_connection(host, port)
    server_running = bool(command_matches and listener_open)
    stale_pid = bool(record and pid and not pid_alive)
    managed_pid_alive = bool(command_matches and pid_alive)
    port_conflict = bool(listener_open and not command_matches)
    if server_running:
        listener_status = "managed"
    elif port_conflict:
        listener_status = "port_in_use"
    elif stale_pid:
        listener_status = "stale_pid"
    elif managed_pid_alive:
        listener_status = "starting"
    else:
        listener_status = "stopped"
    return {
        "pid": pid,
        "server_running": server_running,
        "managed_pid_alive": managed_pid_alive,
        "stale_pid": stale_pid,
        "port_conflict": port_conflict,
        "listener_status": listener_status,
    }


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return not _pid_is_zombie(pid)
    except OSError:
        return False


def _pid_is_zombie(pid: int) -> bool:
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.stdout.strip().startswith("Z")


def _command_for_pid(pid: int | None) -> str:
    if not pid:
        return ""
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout.strip()


def _managed_command_matches(command: str, *, host: str, port: int) -> bool:
    text = " ".join(str(command or "").split())
    return (
        "activity_mvp" in text
        and "ai-connector" in text
        and "serve" in text
        and f"--port {int(port)}" in text
        and (f"--host {host}" in text or host == DEFAULT_AI_CONNECTOR_HOST)
    )


def _port_accepts_connection(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=0.2):
            return True
    except OSError:
        return False


def _connector_counts(
    conn: sqlite3.Connection,
    start: str,
    end: str,
    *,
    confirmed_activities: list[dict],
    pending_confirmation_count: int,
) -> dict:
    activity_count = len(confirmed_activities)
    readable_count = sum(1 for item in confirmed_activities if item.get("privacy_level") == "normal")
    partial_count = activity_count - readable_count
    privacy_count = _count_rows(
        conn,
        """
        SELECT COUNT(*) AS count
        FROM privacy_events
        WHERE datetime(started_at) >= datetime(?)
          AND datetime(started_at) < datetime(?)
        """,
        (start, end),
    )
    gap_count = _count_rows(
        conn,
        """
        SELECT COUNT(*) AS count
        FROM gap_events
        WHERE datetime(started_at) >= datetime(?)
          AND datetime(started_at) < datetime(?)
        """,
        (start, end),
    )
    return {
        "activity_count": activity_count,
        "readable_activity_count": readable_count,
        "pending_confirmation_count": max(0, int(pending_confirmation_count)),
        "partial_redacted_count": partial_count,
        "communication_redacted_count": 0,
        "privacy_event_count": privacy_count,
        "gap_event_count": gap_count,
        "duplicate_event_count": 0,
        "low_signal_count": 0,
        "protected_count": privacy_count,
        "recorded_hours": 0,
        "frames_dropped": 0,
    }


def _connector_samples(confirmed_activities: list[dict], *, max_items: int) -> list[dict]:
    rows = sorted(
        (item for item in confirmed_activities if item.get("privacy_level") == "normal"),
        key=lambda item: str(item.get("started_at") or ""),
        reverse=True,
    )[: max(0, max_items)]
    items = []
    for row in reversed(rows):
        apps = _json_list(row.get("apps"))
        domains = _json_list(row.get("domains"))
        app = apps[0] if apps else ""
        domain = domains[0] if domains else ""
        topic = domain or app or "未分类"
        items.append(
            {
                "time_label": str(row.get("started_at") or ""),
                "app": app,
                "domain": domain,
                "topic": topic,
                "workflow_label": topic,
                "safe_summary": str(row.get("activity_summary") or "")[:500],
                "confidence": float(row.get("redaction_confidence") or 0.0),
                "occurrence_state": str(row.get("occurrence_state") or "evidence_confirmed"),
                "confirmation_source": str(row.get("confirmation_source") or "screen_observation"),
                "recommendation": "仅作为近期安全活动线索，不要扩写成未经验证事实。",
            }
        )
    return items


def _connector_data_trust(counts: dict) -> dict:
    warnings = []
    if int(counts.get("activity_count") or 0) == 0:
        warnings.append({"type": "no_activity", "message": "这个时间段没有可展示的 clear 活动；不要推断用户没有工作。"})
    if int(counts.get("privacy_event_count") or 0) > 0:
        warnings.append({"type": "privacy_events_present", "message": "存在隐私保护事件；这些内容只进入计数，不进入正文。"})
    return {
        "level": "low" if warnings and int(counts.get("readable_activity_count") or 0) == 0 else "medium",
        "warnings": warnings,
        "coverage": {
            "ledger_rows": 0,
            "recorded_hours": 0,
            "privacy_event_count": int(counts.get("privacy_event_count") or 0),
            "gap_event_count": int(counts.get("gap_event_count") or 0),
            "frames_dropped": 0,
            "hourly_counts": [],
        },
    }


def _connector_project_summaries(samples: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for item in samples:
        grouped.setdefault(str(item.get("topic") or "未分类"), []).append(item)
    projects = []
    for topic, items in list(grouped.items())[:8]:
        projects.append(
            {
                "topic": topic,
                "activity_count": len(items),
                "representative_summaries": [str(item.get("safe_summary") or "") for item in items[:3]],
                "confidence": min(0.8, max((float(item.get("confidence") or 0.0) for item in items), default=0.0)),
                "recommendation": "轻量连接器摘要；需要完整智能回顾时使用 app 内 Review。",
            }
        )
    return projects


def _count_rows(conn: sqlite3.Connection, sql: str, params: tuple[str, str]) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row["count"] or 0) if row else 0


def _json_list(raw) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item)]
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return []


def _top_rows(counter: Counter[str]) -> list[dict]:
    return [{"name": name, "count": count} for name, count in counter.most_common(8)]


def _validate_loopback_host(host: str) -> None:
    if not is_loopback_host(host):
        raise ValueError("AI connector must bind to a loopback host")


def _client_allowed(host: str) -> bool:
    return is_loopback_host(host)


def _host_allowed(host_header: str | None, bind_port: int) -> bool:
    if not host_header:
        return False
    parsed = urlparse(f"//{host_header.strip()}")
    if not is_loopback_host(parsed.hostname or ""):
        return False
    try:
        port = parsed.port
    except ValueError:
        return False
    return port == bind_port


def _origin_allowed(origin: str) -> bool:
    parsed = urlparse(origin)
    return parsed.scheme == "http" and is_loopback_host(parsed.hostname or "")


def _first(qs: dict[str, list[str]], key: str, default: str) -> str:
    values = qs.get(key)
    if not values:
        return default
    return str(values[0] or default)


def _int_param(qs: dict[str, list[str]], key: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = _first(qs, key, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer") from exc
    return min(max(value, minimum), maximum)


def _error(code: str, message: str) -> dict:
    return {
        "status": "error",
        "error": code,
        "message": message,
        "raw_access": False,
        "destructive_actions": False,
        "memory_write_actions": False,
        "rule_write_actions": False,
    }
