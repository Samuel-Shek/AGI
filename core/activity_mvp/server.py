from __future__ import annotations

import json
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .daemon import get_status, pause, resume, run_once
from .daily_review import daily_review
from .document_changes import document_authorization_lock
from .ha_source import generate_package, get_package, list_packages
from .lifecycle import delete_range
from .local_endpoints import is_loopback_host
from .pipeline import search, timeline
from .privacy import sanitize_privacy_event_context
from .storage import ensure_ready


class DestructiveConfirmationStore:
    def __init__(self, ttl_seconds: int = 120) -> None:
        self.ttl_seconds = ttl_seconds
        self._items: dict[str, dict] = {}
        self._lock = threading.Lock()

    def issue(self, action: str, subject: dict) -> dict:
        code = secrets.token_urlsafe(24)
        now = time.monotonic()
        expires_at_monotonic = now + self.ttl_seconds
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=self.ttl_seconds)
        with self._lock:
            self._prune_locked(now)
            self._items[code] = {
                "action": action,
                "subject": dict(subject),
                "expires_at_monotonic": expires_at_monotonic,
            }
        return {
            "confirmation_code": code,
            "action": action,
            "expires_at": expires_at.isoformat(timespec="seconds"),
        }

    def consume(self, code: str, action: str, subject: dict) -> bool:
        if not code:
            return False
        now = time.monotonic()
        with self._lock:
            self._prune_locked(now)
            item = self._items.get(code)
            if item is None:
                return False
            if item["expires_at_monotonic"] <= now:
                self._items.pop(code, None)
                return False
            if item["action"] != action or item["subject"] != dict(subject):
                return False
            self._items.pop(code, None)
            return True

    def _prune_locked(self, now: float) -> None:
        expired = [
            code
            for code, item in self._items.items()
            if item["expires_at_monotonic"] <= now
        ]
        for code in expired:
            self._items.pop(code, None)


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8765) -> None:
    validate_loopback_bind_host(host)
    conn = ensure_ready(db_path)
    conn.close()

    class Handler(ActivityHandler):
        database_path = db_path
        bind_host = host
        bind_port = port
        destructive_confirmation_store = DestructiveConfirmationStore()

    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"Personal AI Monitor development dashboard: http://{host}:{port}")
    httpd.serve_forever()


class ActivityHandler(BaseHTTPRequestHandler):
    database_path = "data/activity.sqlite"
    bind_host = "127.0.0.1"
    bind_port = 8765
    destructive_confirmation_store = DestructiveConfirmationStore()

    def log_message(self, format: str, *args) -> None:
        return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            if not self._authorized(require_origin=False):
                return
            self._send_html(render_dashboard())
            return
        if not self._authorized(require_origin=True):
            return

        qs = parse_qs(parsed.query)
        conn = ensure_ready(self.database_path)
        try:
            if parsed.path == "/api/status":
                self._send_json(get_status(conn, Path(self.database_path).parent))
                return
            if parsed.path == "/api/timeline":
                self._send_confirmed_timeline(
                    conn,
                    qs.get("from", [None])[0],
                    qs.get("to", [None])[0],
                )
                return
            if parsed.path == "/api/search":
                self._send_confirmed_search(
                    conn,
                    qs.get("q", [""])[0],
                    qs.get("app", [None])[0],
                )
                return
            if parsed.path == "/api/daily-review":
                self._send_json(daily_review(conn, qs.get("from", [None])[0], qs.get("to", [None])[0]))
                return
            if parsed.path == "/api/packages":
                self._send_package_list(conn)
                return
            if parsed.path == "/api/privacy-events":
                self._send_json(list_privacy_events(conn))
                return
            if parsed.path == "/api/gap-events":
                self._send_json(list_gap_events(conn))
                return
            if parsed.path.startswith("/api/package/"):
                package_id = parsed.path.rsplit("/", 1)[-1]
                self._send_package(conn, package_id)
                return
        except Exception as exc:
            self._send_exception(exc)
            return
        finally:
            conn.close()
        self.send_error(HTTPStatus.NOT_FOUND)

    def _send_confirmed_timeline(self, conn, start: str | None, end: str | None) -> None:
        from .activity_confirmations import confirmed_activity_view

        # Keep the authorization lease through local response serialization and
        # socket write.  Once a delete/reclassification completes, this endpoint
        # can no longer finish sending a payload built from the old view.
        with document_authorization_lock(conn, exclusive=False):
            activities = list(confirmed_activity_view(conn, start, end).get("activities") or [])
            self._send_json(activities)

    def _send_confirmed_search(self, conn, query: str, app: str | None) -> None:
        # `search(..., confirmed_only=True)` protects result construction; this
        # outer lease additionally prevents a delete from completing before the
        # already-built result has finished crossing the local HTTP boundary.
        with document_authorization_lock(conn, exclusive=False):
            results = search(conn, query, app, confirmed_only=True)
            self._send_json(results)

    def _send_package_list(self, conn) -> None:
        with document_authorization_lock(conn, exclusive=False):
            self._send_json(list_packages(conn))

    def _send_package(self, conn, package_id: str) -> None:
        with document_authorization_lock(conn, exclusive=False):
            self._send_json(get_package(conn, package_id))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)

        allowed_paths = {
            "/api/capture-now",
            "/api/pause",
            "/api/resume",
            "/api/package",
            "/api/package-today",
            "/api/delete-confirmation",
            "/api/delete",
        }
        if parsed.path not in allowed_paths:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not self._authorized(require_origin=True):
            return
        body = self._read_json_body()

        if parsed.path == "/api/delete-confirmation":
            try:
                subject = _delete_confirmation_subject(body)
                self._send_json(self.destructive_confirmation_store.issue("delete_time_range", subject))
            except Exception as exc:
                self._send_exception(exc)
            return

        if parsed.path == "/api/capture-now":
            try:
                self._send_json(run_once(self.database_path))
            except Exception as exc:
                self._send_exception(exc)
            return

        conn = ensure_ready(self.database_path)
        try:
            if parsed.path == "/api/pause":
                mode = str(body.get("mode") or "duration")
                if mode == "manual":
                    self._send_json(pause(conn, mode="manual"))
                elif mode == "until_lock":
                    self._send_json(pause(conn, mode="until_lock"))
                elif mode == "today":
                    self._send_json(pause(conn, mode="today"))
                else:
                    self._send_json(pause(conn, int(body.get("minutes") or 30)))
                return
            if parsed.path == "/api/resume":
                self._send_json(resume(conn))
                return
            if parsed.path == "/api/package":
                result = generate_package(conn, body["from"], body["to"], Path(self.database_path).parent / "ha_packages")
                self._send_json(_package_result(result))
                return
            if parsed.path == "/api/package-today":
                start, end = today_range()
                result = generate_package(conn, start, end, Path(self.database_path).parent / "ha_packages")
                payload = _package_result(result)
                payload.update({"from": start, "to": end})
                self._send_json(payload)
                return
            if parsed.path == "/api/delete":
                subject = _delete_confirmation_subject(body)
                if not self.destructive_confirmation_store.consume(
                    str(body.get("confirmation_code") or ""),
                    "delete_time_range",
                    subject,
                ):
                    self._send_json(
                        {
                            "error": "destructive_confirmation_required",
                            "message": "Delete requires a fresh one-time confirmation code",
                        },
                        status=HTTPStatus.FORBIDDEN,
                    )
                    return
                result = delete_range(conn, subject["from"], subject["to"], subject["reason"])
                self._send_json(result)
                return
        except Exception as exc:
            self._send_exception(exc)
            return
        finally:
            conn.close()
        self.send_error(HTTPStatus.NOT_FOUND)

    def _authorized(self, require_origin: bool = False) -> bool:
        if not _host_allowed(self.headers.get("Host"), self.bind_host, self.bind_port):
            self.send_error(HTTPStatus.FORBIDDEN, "Host rejected")
            return False
        origin = self.headers.get("Origin")
        referer = self.headers.get("Referer")
        origin_or_referer = origin or referer
        if require_origin and not origin_or_referer:
            self.send_error(HTTPStatus.FORBIDDEN, "Origin or Referer required")
            return False
        if origin_or_referer and not _origin_allowed(origin_or_referer, self.bind_host, self.bind_port):
            self.send_error(HTTPStatus.FORBIDDEN, "Origin rejected")
            return False
        return True

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length) if length else b"{}"
        return json.loads(data.decode("utf-8"))

    def _send_exception(self, exc: Exception) -> None:
        status = (
            HTTPStatus.BAD_REQUEST
            if isinstance(exc, (ValueError, KeyError, json.JSONDecodeError))
            else HTTPStatus.INTERNAL_SERVER_ERROR
        )
        self._send_json(
            {
                "error": type(exc).__name__,
                "message": str(exc) or "Request failed",
            },
            status=status,
        )

    def _send_json(self, payload, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _send_html(self, html: str) -> None:
        encoded = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)


def _origin_allowed(origin: str | None, bind_host: str, bind_port: int) -> bool:
    if not origin:
        return False
    parsed = urlparse(origin)
    if parsed.scheme != "http" or not is_loopback_host(parsed.hostname or ""):
        return False
    try:
        port = parsed.port
    except ValueError:
        return False
    return port == bind_port


def validate_loopback_bind_host(host: str) -> str:
    if not is_loopback_host(host):
        raise ValueError("Development dashboard must bind to a loopback host")
    return host


def _host_allowed(host_header: str | None, bind_host: str, bind_port: int) -> bool:
    if not host_header:
        return False
    parsed = urlparse(f"//{host_header.strip()}")
    host = parsed.hostname or ""
    if not is_loopback_host(host):
        return False
    try:
        port = parsed.port
    except ValueError:
        return False
    return port == bind_port


def _delete_confirmation_subject(body: dict) -> dict:
    return {
        "from": str(body["from"]),
        "to": str(body["to"]),
        "reason": str(body.get("reason", "user_deleted")),
    }


def _package_result(result: dict) -> dict:
    return {
        "package_id": result["package"]["package_id"],
        "activity_count": len(result["package"]["activities"]),
    }


def today_range() -> tuple[str, str]:
    start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")


def list_privacy_events(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT started_at, ended_at, type, reason, app_name, domain, action
        FROM privacy_events
        ORDER BY started_at DESC
        LIMIT 100
        """
    ).fetchall()
    return [sanitize_privacy_event_context(dict(row)) for row in rows]


def list_gap_events(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT started_at, ended_at, reason, display_ids, detail
        FROM gap_events
        ORDER BY started_at DESC
        LIMIT 100
        """
    ).fetchall()
    output = []
    for row in rows:
        item = dict(row)
        try:
            item["display_ids"] = json.loads(item.get("display_ids") or "[]")
        except json.JSONDecodeError:
            item["display_ids"] = []
        output.append(item)
    return output


def render_dashboard() -> str:
    html = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#f6f7f8">
  <link rel="icon" href="data:,">
  <title>Personal AI Monitor Dev Dashboard</title>
  <style>
    :root {
      --bg: #f6f7f8;
      --surface: #ffffff;
      --surface-soft: #fafafa;
      --ink: #171717;
      --muted: #666666;
      --faint: #8a8a8a;
      --line: #e5e5e5;
      --accent: #0a72ef;
      --accent-dark: #0057c2;
      --ok: #067647;
      --ok-bg: #ecfdf3;
      --warn: #9a6700;
      --warn-bg: #fffaeb;
      --danger: #b42318;
      --danger-bg: #fef3f2;
      --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", monospace;
      --sans: ui-sans-serif, -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Segoe UI", sans-serif;
      --shadow-border: 0 0 0 1px rgba(0, 0, 0, .08);
      --shadow-card: 0 0 0 1px rgba(0, 0, 0, .08), 0 6px 18px rgba(0, 0, 0, .04);
    }
    * { box-sizing: border-box; }
    html { overflow-x: hidden; -webkit-tap-highlight-color: transparent; }
    body { margin: 0; background: var(--bg); color: var(--ink); font-family: var(--sans); font-size: 14px; letter-spacing: 0; }
    button, input { font: inherit; }
    button { touch-action: manipulation; }
    .skip-link { position: absolute; left: 16px; top: 8px; transform: translateY(-160%); z-index: 20; background: var(--ink); color: white; padding: 8px 12px; border-radius: 6px; }
    .skip-link:focus-visible { transform: translateY(0); outline: 2px solid var(--accent); outline-offset: 2px; }
    .shell { min-height: 100vh; display: grid; grid-template-columns: 248px minmax(0, 1fr); }
    .rail { position: sticky; top: 0; height: 100vh; background: #171717; color: white; padding: 24px 18px; display: flex; flex-direction: column; gap: 22px; }
    .brand { display: grid; gap: 8px; }
    .brand strong { font-size: 20px; line-height: 1.15; font-weight: 700; }
    .brand span { color: #a3a3a3; line-height: 1.4; }
    .nav { display: grid; gap: 4px; }
    .nav button { width: 100%; min-height: 42px; border: 0; border-radius: 8px; background: transparent; color: #d4d4d4; text-align: left; padding: 0 12px; cursor: pointer; }
    .nav button:hover { background: #262626; color: white; }
    .nav button.active { background: white; color: #171717; font-weight: 650; }
    .rail-meta { margin-top: auto; display: grid; gap: 8px; color: #a3a3a3; font-size: 12px; line-height: 1.45; }
    .rail-meta code { color: white; font-family: var(--mono); }
    main { min-width: 0; display: grid; grid-template-rows: auto 1fr; }
    .page-head { border-bottom: 1px solid var(--line); background: rgba(255, 255, 255, .86); backdrop-filter: blur(14px); padding: 26px 32px 22px; display: grid; gap: 16px; }
    h1, h2, h3 { margin: 0; letter-spacing: 0; }
    h1 { font-size: clamp(30px, 3vw, 44px); line-height: 1.08; font-weight: 760; text-wrap: balance; }
    h2 { font-size: 18px; line-height: 1.25; font-weight: 720; }
    h3 { font-size: 16px; line-height: 1.35; font-weight: 700; overflow-wrap: anywhere; }
    .tags { display: flex; flex-wrap: wrap; gap: 8px; }
    .tag, .pill { min-height: 26px; display: inline-flex; align-items: center; justify-content: center; gap: 6px; padding: 4px 9px; border-radius: 999px; background: #f5f5f5; color: #404040; box-shadow: var(--shadow-border); font: 600 12px/1 var(--mono); white-space: nowrap; }
    .pill.ok { background: var(--ok-bg); color: var(--ok); }
    .pill.warn { background: var(--warn-bg); color: var(--warn); }
    .pill.danger { background: var(--danger-bg); color: var(--danger); }
    .content { padding: 22px 32px 44px; display: grid; gap: 18px; align-content: start; }
    .notice { min-height: 44px; display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 11px 14px; border-radius: 8px; background: var(--surface); box-shadow: var(--shadow-card); color: var(--ink); }
    .notice[hidden] { display: none; }
    .notice strong { font-weight: 700; }
    .notice span { color: var(--muted); overflow-wrap: anywhere; }
    .notice.ok { box-shadow: 0 0 0 1px rgba(6, 118, 71, .24), 0 6px 18px rgba(6, 118, 71, .07); }
    .notice.warn { box-shadow: 0 0 0 1px rgba(154, 103, 0, .28), 0 6px 18px rgba(154, 103, 0, .08); }
    .notice.danger { box-shadow: 0 0 0 1px rgba(180, 35, 24, .24), 0 6px 18px rgba(180, 35, 24, .07); }
    .actions, .filters { display: grid; gap: 10px; align-items: end; }
    .actions { grid-template-columns: repeat(5, minmax(136px, max-content)); justify-content: start; }
    .filters { grid-template-columns: repeat(4, minmax(160px, 1fr)) minmax(92px, max-content); }
    label { display: grid; gap: 6px; color: var(--muted); font-size: 12px; line-height: 1.2; }
    input { width: 100%; height: 40px; border: 0; border-radius: 8px; background: var(--surface); color: var(--ink); padding: 0 12px; box-shadow: var(--shadow-border); }
    input::placeholder { color: #a3a3a3; }
    input:focus-visible, button:focus-visible, .package-row:focus-visible, summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
    .btn { min-height: 40px; border: 0; border-radius: 8px; padding: 0 14px; display: inline-flex; align-items: center; justify-content: center; gap: 8px; background: var(--surface); color: var(--ink); box-shadow: var(--shadow-border); font-weight: 650; cursor: pointer; white-space: nowrap; }
    .btn:hover { background: #f0f0f0; }
    .btn.primary { background: var(--accent); color: white; box-shadow: none; }
    .btn.primary:hover { background: var(--accent-dark); }
    .btn.danger { background: var(--danger); color: white; box-shadow: none; }
    .btn.danger:hover { background: #912018; }
    .btn:disabled { opacity: .62; cursor: progress; }
    .metrics { display: grid; grid-template-columns: repeat(6, minmax(118px, 1fr)); gap: 10px; }
    .metric { min-height: 88px; padding: 14px; border-radius: 8px; background: var(--surface); box-shadow: var(--shadow-card); display: grid; align-content: space-between; gap: 10px; }
    .metric span { color: var(--muted); font-size: 12px; }
    .metric strong { font: 760 24px/1 var(--mono); font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
    .panel { display: none; gap: 14px; }
    .panel.active { display: grid; }
    .panel-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; min-height: 40px; }
    .panel-actions { display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end; }
    .sync { color: var(--muted); font: 500 12px/1.2 var(--mono); }
    .table { display: grid; border-radius: 8px; overflow: hidden; background: var(--surface); box-shadow: var(--shadow-card); }
    .row { display: grid; grid-template-columns: minmax(128px, 176px) minmax(112px, 150px) minmax(0, 1fr) minmax(82px, max-content); gap: 14px; padding: 13px 14px; align-items: start; border-bottom: 1px solid var(--line); }
    .row:last-child { border-bottom: 0; }
    .row:hover { background: var(--surface-soft); }
    .row.selected { background: #eef6ff; box-shadow: inset 3px 0 0 var(--accent); }
    .package-row { cursor: pointer; }
    .time, .domain, .mono { font-family: var(--mono); font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
    .app { font-weight: 700; overflow-wrap: anywhere; }
    .summary { line-height: 1.48; overflow-wrap: anywhere; }
    .empty { min-height: 120px; display: grid; place-items: center; padding: 28px; color: var(--muted); background: var(--surface); border-radius: 8px; box-shadow: var(--shadow-card); }
    .split { display: grid; grid-template-columns: minmax(0, 1.12fr) minmax(360px, .88fr); gap: 14px; align-items: start; }
    .detail { position: sticky; top: 14px; max-height: calc(100vh - 28px); overflow: auto; border-radius: 8px; background: var(--surface); box-shadow: var(--shadow-card); }
    .detail[hidden] { display: none; }
    .detail-head { display: grid; gap: 10px; padding: 16px; border-bottom: 1px solid var(--line); }
    .detail-title { display: flex; align-items: start; justify-content: space-between; gap: 12px; }
    .detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; padding: 16px; border-bottom: 1px solid var(--line); }
    .kv { display: grid; gap: 5px; padding: 10px; background: var(--surface-soft); border-radius: 8px; min-width: 0; }
    .kv span { color: var(--muted); font-size: 12px; }
    .kv strong { font: 700 13px/1.35 var(--mono); overflow-wrap: anywhere; }
    .activity-list { display: grid; gap: 8px; padding: 16px; }
    .activity-item { display: grid; gap: 6px; padding: 10px; border-radius: 8px; background: var(--surface-soft); }
    .activity-item strong { font-size: 13px; overflow-wrap: anywhere; }
    details { margin: 0 16px 16px; border-radius: 8px; background: #171717; color: white; overflow: hidden; }
    summary { min-height: 40px; display: flex; align-items: center; padding: 0 12px; cursor: pointer; font-weight: 650; }
    pre { margin: 0; white-space: pre-wrap; word-break: break-word; background: #171717; color: #f5f5f5; padding: 14px; font: 400 12px/1.55 var(--mono); max-height: 420px; overflow: auto; }
    pre[hidden] { display: none; }
    .danger-panel { display: grid; gap: 12px; padding: 16px; background: var(--surface); border-radius: 8px; box-shadow: 0 0 0 1px rgba(180, 35, 24, .24), 0 6px 18px rgba(180, 35, 24, .06); }
    @media (max-width: 1240px) {
      .actions { grid-template-columns: repeat(3, minmax(136px, 1fr)); }
      .filters { grid-template-columns: repeat(2, minmax(160px, 1fr)); }
      .metrics { grid-template-columns: repeat(3, minmax(118px, 1fr)); }
      .split { grid-template-columns: 1fr; }
      .detail { position: static; max-height: none; }
    }
    @media (max-width: 780px) {
      .shell { grid-template-columns: 1fr; }
      .rail { position: static; height: auto; }
      .nav { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .nav button { text-align: center; padding: 0 8px; }
      .page-head, .content { padding-left: 16px; padding-right: 16px; }
      .actions, .filters, .metrics { grid-template-columns: 1fr; }
      .row { grid-template-columns: 1fr; }
      .detail-grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <a class="skip-link" href="#main-content">跳到主内容</a>
  <div class="shell">
    <aside class="rail">
      <div class="brand">
        <strong>Personal AI Monitor Dev</strong>
        <span>Local context recall core v0.2</span>
      </div>
      <nav class="nav" aria-label="主导航">
        <button type="button" class="active" data-tab="overview">总览</button>
        <button type="button" data-tab="timeline">时间线</button>
        <button type="button" data-tab="search">搜索</button>
        <button type="button" data-tab="privacy">隐私/缺口</button>
        <button type="button" data-tab="packages">HA 包</button>
        <button type="button" data-tab="delete">删除</button>
      </nav>
      <div class="rail-meta">
        <div><code>127.0.0.1:8765</code></div>
        <div><code>raw_access=false</code></div>
      </div>
    </aside>
    <main>
      <header class="page-head">
        <h1>Local context dashboard</h1>
        <div class="tags">
          <span class="tag">target=HA</span>
          <span class="tag">cloud=off</span>
          <span class="tag">retention=today+yesterday</span>
          <span class="tag" id="last-sync">sync=--</span>
        </div>
      </header>
      <section class="content" id="main-content">
        <div class="notice" id="notice" role="status" aria-live="polite" hidden>
          <strong id="notice-title"></strong>
          <span id="notice-detail"></span>
        </div>
        <div class="actions">
          <button type="button" class="btn primary" id="pause30">暂停 15 分钟</button>
          <button type="button" class="btn" id="resume-now">立即恢复</button>
          <button type="button" class="btn" id="capture-now">立即采集一次</button>
          <button type="button" class="btn" id="make-today-package">生成今日 HA 包</button>
          <button type="button" class="btn" id="refresh">刷新</button>
        </div>
        <div class="filters">
          <label>开始时间 <input id="from" name="from" type="datetime-local" autocomplete="off"></label>
          <label>结束时间 <input id="to" name="to" type="datetime-local" autocomplete="off"></label>
          <label>关键词 <input id="query" name="query" type="search" autocomplete="off" placeholder="输入关键词..."></label>
          <label>App 过滤 <input id="app" name="app" type="search" autocomplete="off" placeholder="可选..."></label>
          <button type="button" class="btn" id="apply-filter">应用筛选</button>
        </div>
        <div class="metrics" aria-live="polite">
          <div class="metric"><span>当前状态</span><strong id="m-status">--</strong></div>
          <div class="metric"><span>最近采集</span><strong id="m-last">--</strong></div>
          <div class="metric"><span>暂停剩余</span><strong id="m-pause">0m</strong></div>
          <div class="metric"><span>今日活动</span><strong id="m-activities">0</strong></div>
          <div class="metric"><span>局部清洗</span><strong id="m-partial">0</strong></div>
          <div class="metric"><span>隐私/缺口</span><strong id="m-risk">0/0</strong></div>
          <div class="metric"><span>HA 自动包</span><strong id="m-ha">--</strong></div>
          <div class="metric"><span>raw 残留</span><strong id="m-raw">0</strong></div>
        </div>

        <section class="panel active" id="overview">
          <div class="panel-head"><h2>总览</h2></div>
          <div class="table" id="overview-table"></div>
          <pre id="action-result" hidden></pre>
        </section>

        <section class="panel" id="timeline">
          <div class="panel-head"><h2>时间线</h2><span class="sync" id="timeline-count"></span></div>
          <div class="table" id="timeline-table"></div>
        </section>

        <section class="panel" id="search">
          <div class="panel-head"><h2>搜索</h2><span class="sync" id="search-count"></span></div>
          <div class="table" id="search-table"></div>
        </section>

        <section class="panel" id="privacy">
          <div class="panel-head"><h2>隐私/缺口</h2></div>
          <div class="table" id="privacy-table"></div>
          <div class="table" id="gap-table"></div>
        </section>

        <section class="panel" id="packages">
          <div class="panel-head">
            <h2>HA 包</h2>
            <div class="panel-actions">
              <button type="button" class="btn" id="make-package">生成所选区间 HA 包</button>
            </div>
          </div>
          <div class="split">
            <div class="table" id="package-table"></div>
            <div class="detail" id="package-detail" tabindex="-1" hidden></div>
          </div>
        </section>

        <section class="panel" id="delete">
          <div class="panel-head"><h2>删除</h2></div>
          <div class="danger-panel">
            <button type="button" class="btn danger" id="delete-range">删除所选时间段</button>
            <pre id="delete-result" hidden></pre>
          </div>
        </section>
      </section>
    </main>
  </div>
  <script>
    const state = {
      status: {},
      timeline: [],
      search: [],
      packages: [],
      privacy: [],
      gaps: [],
      selectedPackageId: ''
    };

    function $(id) {
      return document.getElementById(id);
    }
    function escapeHtml(value) {
      return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    }
    async function api(path, options = {}) {
      const headers = { ...(options.headers || {}) };
      const response = await fetch(path, { ...options, headers, credentials: 'same-origin' });
      const text = await response.text();
      let payload = null;
      if (text) {
        try {
          payload = JSON.parse(text);
        } catch {
          payload = text;
        }
      }
      if (!response.ok) {
        const detail = typeof payload === 'object' && payload
          ? (payload.message || payload.error || JSON.stringify(payload))
          : (payload || response.statusText);
        throw new Error(`${response.status} ${detail}`);
      }
      return payload;
    }
    function post(path, body = {}) {
      return api(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    }
    function dateValue(id) {
      const value = $(id).value;
      return value ? new Date(value).toISOString() : '';
    }
    function currentRange() {
      const from = dateValue('from') || (state.timeline[0] && state.timeline[0].started_at) || '';
      const to = dateValue('to') || (state.timeline[state.timeline.length - 1] && state.timeline[state.timeline.length - 1].ended_at) || '';
      return { from, to };
    }
    function formatTime(value) {
      if (!value) return '--';
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return value;
      return new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(date);
    }
    function formatDuration(seconds) {
      const n = Number(seconds || 0);
      return n <= 0 ? '0m' : `${Math.floor(n / 60)}m`;
    }
    function formatRange(range) {
      if (!range) return '--';
      const from = range.from || range.started_at || '';
      const to = range.to || range.ended_at || '';
      return `${formatTime(from)} -> ${formatTime(to)}`;
    }
    function sourceClass(status) {
      if (status === 'active') return 'ok';
      if (status === 'revoked' || status === 'user_deleted' || status === 'expired_deleted') return 'danger';
      return 'warn';
    }
    function showNotice(title, detail = '', kind = '') {
      const notice = $('notice');
      notice.className = `notice ${kind}`.trim();
      $('notice-title').textContent = title;
      $('notice-detail').textContent = detail;
      notice.hidden = false;
    }
    function showPre(id, value) {
      const el = $(id);
      if (!el) return;
      el.hidden = false;
      el.textContent = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
    }
    function buttonBusy(button, busy, label) {
      if (!button.dataset.label) button.dataset.label = button.textContent;
      button.disabled = busy;
      button.textContent = busy ? label : button.dataset.label;
    }
    async function runAction(buttonId, busyLabel, successTitle, action) {
      const button = $(buttonId);
      buttonBusy(button, true, busyLabel);
      showNotice(busyLabel, '', 'warn');
      try {
        const detail = await action();
        showNotice(successTitle, detail || '', 'ok');
      } catch (error) {
        showNotice('操作失败', error && error.message ? error.message : String(error), 'danger');
      } finally {
        buttonBusy(button, false, busyLabel);
      }
    }
    function showTab(tab) {
      const panel = $(tab);
      if (!panel) return;
      document.querySelectorAll('.nav button').forEach(button => {
        button.classList.toggle('active', button.dataset.tab === tab);
      });
      document.querySelectorAll('.panel').forEach(item => item.classList.remove('active'));
      panel.classList.add('active');
    }
    function renderGenericRows(target, rows, mode) {
      const el = $(target);
      if (!rows.length) {
        el.innerHTML = '<div class="empty">暂无记录</div>';
        return;
      }
      el.innerHTML = rows.map(row => {
        const rawTime = row.started_at || row.created_at || '';
        const app = row.app_name || (row.apps || []).join(', ') || row.source_status || row.type || '';
        const domain = row.domain || (row.domains || []).join(', ') || row.reason || '';
        const summary = row.summary || row.activity_summary || row.detail || JSON.stringify(row.date_range || row.time_range || {});
        const status = mode === 'privacy' || mode === 'gap' ? `<span class="pill warn">${escapeHtml(row.action || row.type || row.reason || 'event')}</span>` : '';
        return `<div class="row">
          <div class="time">${escapeHtml(formatTime(rawTime))}</div>
          <div class="app">${escapeHtml(app)}</div>
          <div><div class="summary">${escapeHtml(summary)}</div><div class="domain">${escapeHtml(domain)}</div></div>
          <div>${status}</div>
        </div>`;
      }).join('');
    }
    function renderOverview() {
      const s = state.status || {};
      const pauseLabel = s.status === 'paused' ? (s.pause_label || '暂停中') : '未暂停';
      const privacy = s.privacy_health || {};
      const ha = s.ha_automation || {};
      renderGenericRows('overview-table', [
        { started_at: s.last_capture_at, app_name: '采集', domain: `${s.capture_interval_seconds || 15}s`, summary: `状态：${s.status || 'ready'}` },
        { started_at: '', app_name: '清洗解释', domain: `partial=${privacy.partial_redacted_count || 0}`, summary: privacy.summary || '暂无清洗统计' },
        { started_at: ha.last_generated_at || '', app_name: 'HA 自动包', domain: `${ha.pending_count || 0} pending / ${ha.failed_count || 0} failed / ${ha.dead_letter_count || 0} dead`, summary: ha.summary || 'HA 自动包状态未知' },
        { started_at: s.pause_until, app_name: '暂停', domain: pauseLabel, summary: pauseLabel },
        { started_at: '', app_name: '锁屏', domain: s.locked ? 'locked' : 'unlocked', summary: s.locked ? '锁屏期间停止采集，只补处理既有积压' : '可采集' },
        { started_at: '', app_name: '最近错误', domain: '', summary: s.last_error || '无' }
      ], 'overview');
    }
    function renderPackages() {
      const el = $('package-table');
      if (!state.packages.length) {
        el.innerHTML = '<div class="empty">暂无记录</div>';
        return;
      }
      el.innerHTML = state.packages.map(row => {
        const selected = row.package_id === state.selectedPackageId ? ' selected' : '';
        const pill = `<span class="pill ${sourceClass(row.source_status)}">${escapeHtml(row.source_status)}</span>`;
        return `<div class="row package-row${selected}" data-package="${escapeHtml(row.package_id)}" role="button" tabindex="0">
          <div class="time">${escapeHtml(formatTime(row.created_at))}</div>
          <div>${pill}</div>
          <div>
            <div class="summary">${escapeHtml(formatRange(row.date_range))}</div>
            <div class="domain">${escapeHtml(row.package_id)}</div>
          </div>
          <div><button type="button" class="btn" data-package-action="${escapeHtml(row.package_id)}">查看</button></div>
        </div>`;
      }).join('');
    }
    function renderPackageDetail(pkg) {
      const detail = $('package-detail');
      const activities = Array.isArray(pkg.activities) ? pkg.activities : [];
      const range = pkg.time_range || pkg.date_range || {};
      const activityRows = activities.slice(0, 10).map(item => `<div class="activity-item">
        <strong>${escapeHtml(item.summary || item.activity_id || '')}</strong>
        <span class="time">${escapeHtml(formatRange(item.time_range))}</span>
        <span class="domain">${escapeHtml((item.apps || []).join(', '))}</span>
      </div>`).join('');
      const json = escapeHtml(JSON.stringify(pkg, null, 2));
      detail.hidden = false;
      detail.innerHTML = `<div class="detail-head">
        <div class="detail-title">
          <h3>${escapeHtml(pkg.package_id || 'HA 包')}</h3>
          <span class="pill ${sourceClass(pkg.source_status)}">${escapeHtml(pkg.source_status || 'unknown')}</span>
        </div>
        <div class="mono">${escapeHtml(formatRange(range))}</div>
      </div>
      <div class="detail-grid">
        <div class="kv"><span>活动数</span><strong>${activities.length}</strong></div>
        <div class="kv"><span>覆盖率</span><strong>${escapeHtml(pkg.coverage_rate ?? '--')}</strong></div>
        <div class="kv"><span>raw_access</span><strong>${escapeHtml(String(pkg.raw_access ?? false))}</strong></div>
        <div class="kv"><span>cloud</span><strong>${escapeHtml(pkg.cloud_analysis_default || 'off')}</strong></div>
      </div>
      <div class="activity-list">${activityRows || '<div class="empty">暂无活动</div>'}</div>
      <details>
        <summary>完整 JSON</summary>
        <pre>${json}</pre>
      </details>`;
      detail.focus({ preventScroll: true });
    }
    function renderPackageLoading(packageId) {
      const detail = $('package-detail');
      detail.hidden = false;
      detail.innerHTML = `<div class="detail-head"><h3>${escapeHtml(packageId)}</h3><div class="mono">读取中...</div></div>`;
      detail.focus({ preventScroll: true });
    }
    function renderMetrics(status) {
      $('m-status').textContent = status.status || 'ready';
      $('m-activities').textContent = status.today_activity_count || state.timeline.length || 0;
      $('m-partial').textContent = (status.privacy_health && status.privacy_health.partial_redacted_count) || 0;
      $('m-risk').textContent = `${status.today_privacy_count || 0}/${status.today_gap_count || 0}`;
      $('m-ha').textContent = status.ha_automation ? `${status.ha_automation.active_package_count || 0}` : '--';
      $('m-raw').textContent = status.raw_residue_count || 0;
      $('m-last').textContent = status.last_capture_at ? formatTime(status.last_capture_at) : '--';
      $('m-pause').textContent = status.status === 'paused' ? (status.pause_label || '暂停中') : formatDuration(status.paused_remaining_seconds);
      $('last-sync').textContent = `sync=${formatTime(new Date().toISOString())}`;
    }
    async function refreshAll(options = {}) {
      const silent = Boolean(options.silent);
      if (!silent) showNotice('刷新中', '', 'warn');
      const params = new URLSearchParams();
      const from = dateValue('from');
      const to = dateValue('to');
      if (from) params.set('from', from);
      if (to) params.set('to', to);
      const q = $('query').value.trim();
      const app = $('app').value.trim();
      const [status, timeline, packages, privacy, gaps] = await Promise.all([
        api('/api/status'),
        api('/api/timeline?' + params.toString()),
        api('/api/packages'),
        api('/api/privacy-events'),
        api('/api/gap-events')
      ]);
      state.status = status;
      state.timeline = timeline || [];
      state.packages = packages || [];
      state.privacy = privacy || [];
      state.gaps = gaps || [];
      if (q) {
        const sp = new URLSearchParams({ q });
        if (app) sp.set('app', app);
        state.search = await api('/api/search?' + sp.toString());
      } else {
        state.search = [];
      }
      renderMetrics(status);
      renderOverview();
      renderGenericRows('timeline-table', state.timeline, 'timeline');
      renderGenericRows('search-table', state.search, 'search');
      renderGenericRows('privacy-table', state.privacy, 'privacy');
      renderGenericRows('gap-table', state.gaps, 'gap');
      renderPackages();
      $('timeline-count').textContent = `${state.timeline.length} 条`;
      $('search-count').textContent = `${state.search.length} 条`;
      if (!silent) showNotice('已刷新', '', 'ok');
    }
    async function openPackage(packageId) {
      state.selectedPackageId = packageId;
      renderPackages();
      renderPackageLoading(packageId);
      try {
        const result = await api('/api/package/' + encodeURIComponent(packageId));
        renderPackageDetail(result);
        showNotice('已读取 HA 包', packageId, 'ok');
      } catch (error) {
        $('package-detail').innerHTML = `<div class="detail-head"><h3>${escapeHtml(packageId)}</h3><div class="mono">${escapeHtml(error.message || String(error))}</div></div>`;
        showNotice('读取失败', error.message || String(error), 'danger');
      }
    }

    document.querySelectorAll('.nav button').forEach(button => {
      button.addEventListener('click', () => showTab(button.dataset.tab));
    });
    $('package-table').addEventListener('click', event => {
      const row = event.target.closest('.package-row');
      if (!row) return;
      openPackage(row.dataset.package);
    });
    $('package-table').addEventListener('keydown', event => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      const row = event.target.closest('.package-row');
      if (!row) return;
      event.preventDefault();
      openPackage(row.dataset.package);
    });
    $('apply-filter').addEventListener('click', () => refreshAll().catch(error => showNotice('刷新失败', error.message, 'danger')));
    ['query', 'app', 'from', 'to'].forEach(id => {
      $(id).addEventListener('keydown', event => {
        if (event.key === 'Enter') refreshAll().catch(error => showNotice('刷新失败', error.message, 'danger'));
      });
    });
    $('refresh').addEventListener('click', () => refreshAll().catch(error => showNotice('刷新失败', error.message, 'danger')));
    $('pause30').addEventListener('click', () => runAction('pause30', '暂停中', '已暂停', async () => {
      const result = await post('/api/pause', { minutes: 15 });
      showPre('action-result', result);
      await refreshAll({ silent: true });
      return '15 分钟';
    }));
    $('resume-now').addEventListener('click', () => runAction('resume-now', '恢复中', '已恢复', async () => {
      const result = await post('/api/resume');
      showPre('action-result', result);
      await refreshAll({ silent: true });
    }));
    $('capture-now').addEventListener('click', () => runAction('capture-now', '采集中', '采集完成', async () => {
      const result = await post('/api/capture-now');
      showPre('action-result', result);
      await refreshAll({ silent: true });
    }));
    $('make-today-package').addEventListener('click', () => runAction('make-today-package', '生成中', '已生成 HA 包', async () => {
      const result = await post('/api/package-today');
      await refreshAll({ silent: true });
      showTab('packages');
      state.selectedPackageId = result.package_id;
      renderPackages();
      await openPackage(result.package_id);
      return `${result.package_id}，${result.activity_count} 条活动`;
    }));
    $('make-package').addEventListener('click', () => runAction('make-package', '生成中', '已生成 HA 包', async () => {
      const { from, to } = currentRange();
      if (!from || !to) throw new Error('请选择开始时间和结束时间');
      const result = await post('/api/package', { from, to });
      await refreshAll({ silent: true });
      state.selectedPackageId = result.package_id;
      renderPackages();
      await openPackage(result.package_id);
      return `${result.package_id}，${result.activity_count} 条活动`;
    }));
    $('delete-range').addEventListener('click', () => runAction('delete-range', '删除中', '已删除', async () => {
      const { from, to } = currentRange();
      if (!from || !to) throw new Error('请选择开始时间和结束时间');
      if (!confirm('确认删除所选时间范围的本地活动数据和相关 HA 包？')) return '已取消';
      const subject = { from, to, reason: 'user_deleted_from_dashboard' };
      const confirmation = await post('/api/delete-confirmation', subject);
      const result = await post('/api/delete', { ...subject, confirmation_code: confirmation.confirmation_code });
      showPre('delete-result', result);
      await refreshAll({ silent: true });
      return `${result.deleted_activity_count || 0} 条活动`;
    }));

    refreshAll().catch(error => showNotice('加载失败', error.message, 'danger'));
    window.setInterval(() => refreshAll({ silent: true }).catch(error => showNotice('自动刷新失败', error.message, 'danger')), 15000);
  </script>
</body>
</html>"""
    return html
