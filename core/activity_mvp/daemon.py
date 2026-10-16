from __future__ import annotations

import hashlib
import json
import os
import plistlib
import select
import secrets
import sqlite3
import socket
import stat
import subprocess
import sys
import threading
import time
import fcntl
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import __version__
from .adaptive_capture import (
    CAPTURE_BACKOFF_REASONS,
    CAPTURE_SCHEDULE_MODE,
    AdaptiveCaptureState,
    advance_capture_state,
    capture_observation,
    hid_input_detected,
    initial_capture_state,
    observation_from_payload,
    observation_payload,
    reset_capture_state,
)
from .evidence_retention import ensure_evidence_policy, evidence_retention_status
from .capture_lease import capture_lease_snapshot, configure_capture_lease, expire_capture_lease
from .activity_confirmations import confirmed_activity_view
from .activity_sessions import activity_sessions_status_summary, observe_system_activity_session
from .attribution_context import attribution_health_for_range
from .confirmation_evidence import evidence_health
from .browser_signals import CHROMIUM_HOST_DIRS, NATIVE_HOST_NAME, list_recent_browser_signals, read_latest_browser_signal
from .data_hygiene import (
    DEFAULT_KEEP_BACKUPS,
    DEFAULT_MIN_AGE_DAYS,
    DEFAULT_QUARANTINE_DAYS,
    HYGIENE_CLEANUP_CONFIRMATION,
    hygiene_cleanup_report,
    hygiene_cleanup_state,
)
from .ha_automation import ensure_ha_automation_defaults, ha_automation_status, run_ha_automation_once
from .ha_source import expire_packages, retry_pending_package_cache_purges
from .lifecycle import delete_range, preview_delete_range
from .launch_agent_contract import build_launch_agent_payload
from .local_endpoints import validate_local_http_endpoint
from .macos_probe import capture_sck_keyframe_batch
from .pipeline import parse_time, record_capture_coverage, record_duplicate_event, utc_now
from .privacy import (
    DEFAULT_PRIVACY_PROFILE,
    PRIVACY_PROFILE_PERSONAL_HIGH_PRECISION,
    PRIVACY_PROFILE_STRICT,
    normalize_privacy_profile,
)
from .private_purge import private_purge_status
from .quality_advice import ensure_quality_advice_defaults, quality_advice_status, run_quality_advice_once
from .retention import (
    DEFAULT_RETENTION_DAYS,
    DEFAULT_RETENTION_POLICY,
    RETENTION_MAINTENANCE_HARD_DEADLINE_SECONDS,
    LEGACY_RETENTION_DAYS,
    RETENTION_LABEL,
    RETENTION_WINDOW_DESCRIPTION,
    cleanup_ai_attribution_receipts,
    cleanup_operational_records,
    retention_handoff_admission,
    retention_cleanup_cutoff,
    rolling_retention_cutoff,
)
from .storage import SQLITE_BUSY_TIMEOUT_MS, connect_readonly, ensure_ready, init_schema
from .stability import (
    build_soak_report,
    record_capture_cycle,
    record_session_transition_evidence,
    session_transition_status,
)
from .swift_helpers import ensure_all_helpers, helper_command
from .user_decisions import safe_decision_text


from .runtime_resources import power_state, system_idle_seconds
from .legacy_review_cleanup import cleanup_retired_review_files
from .runtime_liveness import HEARTBEAT_INTERVAL_SECONDS, HeartbeatWriter, runtime_health

DEFAULT_INTERVAL_SECONDS = 15
ADAPTIVE_CAPTURE_WAIT_POLL_SECONDS = 5.0
# File tracking has its own persistent FSEvents worker and does not need the
# capture pipeline to reopen and revalidate a multi-gigabyte SQLite database
# every 15 seconds.  Keep a bounded poll so a later Resume takes effect without
# a daemon restart, while the disabled screen pipeline stays genuinely quiet.
SCREEN_CAPTURE_DISABLED_POLL_SECONDS = 60.0
DEFAULT_PAUSE_MINUTES = 30
LAUNCH_AGENT_ID = "app.personalai.monitor.core"
PRIVACY_PROFILE_ENV_KEY = "PAM_PRIVACY_PROFILE"
DOCUMENT_CHANGE_STARTUP_RECONCILE_ENV_KEY = "PAM_DOCUMENT_CHANGE_STARTUP_RECONCILE_ENABLED"
# 0 keeps the idle field in every status payload while letting the file
# tracker start as soon as the session is usable.  The gate originally existed
# to defer the one-time baseline I/O storm to an unattended window; that cost
# is now handled by throttling the scan itself, and the gate's real-world
# effect had become "tracking never restarts after a daemon restart".
DOCUMENT_CHANGE_STARTUP_MIN_IDLE_SECONDS = 0
# The 15-minute value was tuned against a worker that killed itself seconds
# after every start; damping that loop was all it ever did.  With the loop gone
# the cooldown only has to damp a genuine startup failure -- a missing helper
# binary, revoked permission, an unreadable database -- and 15 minutes of
# blindness after every authorization edit is not a price worth paying for it.
DOCUMENT_CHANGE_WORKER_RETRY_COOLDOWN_SECONDS = 60
# A run that stayed up this long did not fail to start.  Whatever ended it -- an
# authorization change, a pause, a root edit -- should be followed immediately
# rather than sat out.
DOCUMENT_CHANGE_WORKER_HEALTHY_RUN_SECONDS = 10 * 60
# A lost-stream generation whose own watermark reset failed (SQLite busy) is
# retried by the lifecycle owner after the pipeline joined.  Failed retries back
# off from the 60-second fresh-stream cooldown up to this ceiling.
DOCUMENT_STREAM_RESET_RETRY_MAX_COOLDOWN_SECONDS = 15 * 60
SESSION_STATE_HELPER_TIMEOUT_SECONDS = 5.0
SESSION_STATE_HELPER_RESPONSE_MAX_BYTES = 16 * 1024
SESSION_STATE_HELPER_STOP_TIMEOUT_SECONDS = 0.5
BACKGROUND_MAINTENANCE_MIN_IDLE_SECONDS = 10 * 60
BACKGROUND_WORKER_RETRY_COOLDOWN_SECONDS = 15 * 60
RETENTION_MAINTENANCE_MAX_DEFERRAL_SECONDS = (
    RETENTION_MAINTENANCE_HARD_DEADLINE_SECONDS
)
_HA_AUTOMATION_THREAD: threading.Thread | None = None
_HA_AUTOMATION_THREAD_LOCK = threading.Lock()
_QUALITY_ADVICE_THREAD: threading.Thread | None = None
_QUALITY_ADVICE_THREAD_LOCK = threading.Lock()
_DAILY_MAINTENANCE_THREAD: threading.Thread | None = None
_DAILY_MAINTENANCE_THREAD_LOCK = threading.Lock()
_DAILY_MAINTENANCE_RETRY_NOT_BEFORE_MONOTONIC = 0.0
_HA_AUTOMATION_RETRY_NOT_BEFORE_MONOTONIC = 0.0
_QUALITY_ADVICE_RETRY_NOT_BEFORE_MONOTONIC = 0.0
_DOCUMENT_CHANGE_WORKER_THREAD: threading.Thread | None = None
_DOCUMENT_CHANGE_WORKER_LOCK = threading.Lock()
_DOCUMENT_CHANGE_WORKER_RETRY_NOT_BEFORE_MONOTONIC = 0.0
# Fixed reason code of a lost-stream reset that still has to be retried, and
# how many retries have failed.  Guarded by _DOCUMENT_CHANGE_WORKER_LOCK.
_DOCUMENT_STREAM_RESET_PENDING_REASON: str | None = None
_DOCUMENT_STREAM_RESET_FAILED_RETRIES = 0


def ensure_daemon_defaults(conn) -> None:
    for attempt in range(3):
        try:
            _ensure_daemon_defaults_once(conn)
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt >= 2:
                raise
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            time.sleep(1.0 + attempt)


def _ensure_daemon_defaults_once(conn) -> None:
    init_schema(conn)
    now = utc_now()
    defaults = {
        "capture_interval_seconds": str(DEFAULT_INTERVAL_SECONDS),
        "screen_capture_enabled": "false",
        "pause_minutes": str(DEFAULT_PAUSE_MINUTES),
        "retention_days": str(DEFAULT_RETENTION_DAYS),
        "retention_policy": DEFAULT_RETENTION_POLICY,
        "dev_dashboard_enabled": "false",
        "hygiene_auto_cleanup_enabled": "false",
        "hygiene_backup_rotation_enabled": "false",
        "hygiene_keep_backups": str(DEFAULT_KEEP_BACKUPS),
        "hygiene_min_age_days": str(DEFAULT_MIN_AGE_DAYS),
        "hygiene_quarantine_days": str(DEFAULT_QUARANTINE_DAYS),
        "privacy_profile": DEFAULT_PRIVACY_PROFILE,
    }
    for key, value in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value, updated_at) VALUES(?, ?, ?)",
            (key, value, now),
        )
    ensure_evidence_policy(conn)
    ensure_ha_automation_defaults(conn)
    ensure_quality_advice_defaults(conn)
    _migrate_default_retention(conn, now)
    for key, value in {
        "daemon_status": "ready",
        "pause_until": "",
        "pause_mode": "",
        "last_capture_at": "",
        "last_attempt_at": "",
        "last_success_at": "",
        "last_error": "",
        "locked": "true",
        "session_state": "unknown",
        "hygiene_cleanup_status": "disabled",
        "hygiene_cleanup_last_run_at": "",
        "hygiene_cleanup_last_error": "",
        "hygiene_cleanup_last_report": "",
        "retention_cleanup_pending_day": "",
        "retention_cleanup_pending_since": "",
        "retention_cleanup_last_completed_day": "",
        "retention_cleanup_status": "ready",
        "retention_cleanup_last_error": "",
        "private_purge_status": "ok",
        "private_purge_pending_count": "0",
        "private_purge_last_error": "",
        "capture_schedule_mode": CAPTURE_SCHEDULE_MODE,
        "effective_capture_interval_seconds": str(DEFAULT_INTERVAL_SECONDS),
        "capture_backoff_reason": "baseline",
        "capture_unchanged_streak": "0",
    }.items():
        conn.execute(
            "INSERT OR IGNORE INTO daemon_state(key, value, updated_at) VALUES(?, ?, ?)",
            (key, value, now),
        )
    conn.commit()


def get_status(conn, root_dir: str | Path) -> dict:
    from .storage import full_status_schema_read
    from .presentation_scope import display_scope
    with full_status_schema_read(conn), display_scope(root_dir) as scope:
        return scope.project(_get_status(conn, root_dir))


def _get_status(conn, root_dir: str | Path) -> dict:
    ensure_daemon_defaults(conn)
    root = Path(root_dir)
    launch_agent = _launch_agent_status()
    lock = session_state(root)
    record_session_transition_evidence(conn, lock)
    session_state_kind = _session_state_kind(lock)
    locked = session_state_kind == "locked"
    capture_blocked_by_session = session_state_kind != "unlocked"
    _set_state_if_changed(conn, "locked", "true" if capture_blocked_by_session else "false")
    _set_state_if_changed(conn, "session_state", session_state_kind)
    pause_state = _pause_snapshot(conn, locked=locked)
    capture_lease = capture_lease_snapshot(conn)
    screen_capture_enabled = capture_lease["enabled"]
    pause_until = pause_state["pause_until"]
    paused_remaining = pause_state["paused_remaining_seconds"]
    status = _get_state(conn, "daemon_status", "ready")
    if not screen_capture_enabled:
        status = "paused"
    elif pause_state["active"]:
        status = "paused"
    elif locked:
        status = "locked"
    elif session_state_kind == "unknown":
        status = "session_state_unknown"

    liveness = _runtime_health_snapshot(conn, launch_agent, bool(pause_state["active"]), session_state_kind)
    if liveness["reason_code"]:
        status = "stopped" if liveness["reason_code"] == "service_stopped" else "error"
    # A mixed capture batch retains its internal partial receipt and error.
    # Public status uses the existing error contract after safety overrides.
    if status == "partial":
        status = "error"
    interval_seconds = int(_get_setting(conn, "capture_interval_seconds", str(DEFAULT_INTERVAL_SECONDS)))
    adaptive_capture = _adaptive_capture_status_snapshot(conn, interval_seconds)
    effective_interval_seconds = adaptive_capture["effective_capture_interval_seconds"]
    last_capture_at = _get_state(conn, "last_capture_at", "")
    last_success_at = _get_state(conn, "last_success_at", "") or last_capture_at
    last_attempt_at = _get_state(conn, "last_attempt_at", "")
    last_error = _get_state(conn, "last_error", "")
    next_capture_at, next_capture_in = _next_capture_window(
        last_success_at,
        effective_interval_seconds,
        status if screen_capture_enabled else "paused",
        launch_agent_running=launch_agent["running"],
    )
    capture_freshness = _capture_freshness(
        last_success_at,
        effective_interval_seconds,
        status,
        launch_agent_loaded=launch_agent["loaded"],
        launch_agent_running=launch_agent["running"],
        screen_capture_enabled=screen_capture_enabled,
    )
    screen_recording_permission = _screen_recording_permission_status(
        last_error=last_error,
        last_attempt_at=last_attempt_at,
        last_success_at=last_success_at,
        launch_agent_running=launch_agent["running"],
        capture_freshness=capture_freshness,
    )
    dashboard_health = _dashboard_health(conn, launch_agent)
    runtime_identity = _runtime_identity(conn, root)

    start, end = _today_bounds()
    activity_count = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM extracted_activities
        WHERE deleted_at IS NULL
          AND datetime(started_at) >= datetime(?)
          AND datetime(started_at) < datetime(?)
        """,
        (start, end),
    ).fetchone()["count"]
    privacy_count = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM privacy_events
        WHERE datetime(started_at) >= datetime(?)
          AND datetime(started_at) < datetime(?)
        """,
        (start, end),
    ).fetchone()["count"]
    gap_count = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM gap_events
        WHERE datetime(started_at) >= datetime(?)
          AND datetime(started_at) < datetime(?)
        """,
        (start, end),
    ).fetchone()["count"]
    raw_residue = count_raw_residue(root)
    structured_signal_health = _structured_signal_health(conn, root, start, end)
    quality_health = _quality_health(conn, start, end)
    privacy_profile_snapshot = _privacy_profile_snapshot(conn)
    coverage_health = _coverage_health(
        conn,
        start,
        end,
        activity_count=int(activity_count or 0),
        privacy_count=int(privacy_count or 0),
        gap_count=int(gap_count or 0),
    )
    privacy_health = _privacy_health(
        coverage_health,
        activity_count=int(activity_count or 0),
        privacy_count=int(privacy_count or 0),
        gap_count=int(gap_count or 0),
    )
    confirmation_evidence_health = evidence_health(conn)
    private_purge_health = private_purge_status(conn)
    hygiene_cleanup = _hygiene_cleanup_snapshot(conn, root)
    ha_automation = _status_health_subcheck(
        conn,
        lambda: ha_automation_status(conn, root),
        _ha_automation_busy_status,
    )
    activity_session_summary = _status_health_subcheck(
        conn,
        lambda: activity_sessions_status_summary(conn),
        _activity_sessions_busy_status,
    )
    quality_advice = _status_health_subcheck(
        conn,
        lambda: quality_advice_status(conn, root),
        _quality_advice_busy_status,
    )
    attention_items = _attention_items(
        launch_agent=launch_agent,
        lock=lock,
        raw_residue=raw_residue,
        last_error=last_error,
        capture_paused=(
            bool(pause_state["active"])
            or capture_blocked_by_session
            or not screen_capture_enabled
        ),
        dashboard_health=dashboard_health,
        capture_freshness=capture_freshness,
        coverage_health=coverage_health,
        screen_recording_permission=screen_recording_permission,
        confirmation_evidence_health=confirmation_evidence_health,
    )
    if int(private_purge_health["pending_count"]):
        attention_items.append(
            {
                "kind": "warning",
                "code": "private_purge_pending",
                "title": "私密缓存等待清理",
                "message": "有私密缓存删除失败，系统会继续重试，当前未标记为清理成功。",
            }
        )
    if liveness["reason_code"]:
        attention_items.append({"kind": "warning", "code": liveness["reason_code"],
                                "title": "后台进度需要检查", "message": _runtime_health_explanation(liveness)})
    if _get_state(conn, "retention_cleanup_last_error", ""):
        attention_items.append({"kind": "warning", "code": "retention_cleanup_failed",
                                "title": "后台维护未完成", "message": "最近一次维护失败，请检查后重试。"})
    transition_evidence = session_transition_status(conn)
    soak_scorecard = build_soak_report(conn, root)
    guided_repairs = _guided_repair_plan(
        attention_items,
        launch_agent=launch_agent,
        runtime_identity=runtime_identity,
        screen_recording_permission=screen_recording_permission,
    )
    conn.commit()
    payload = {
        "status": status,
        "runtime_health": liveness,
        "reason_code": liveness["reason_code"],
        "screen_capture_enabled": screen_capture_enabled,
        "screen_capture_lease": capture_lease,
        "recording_pause_active": bool(pause_state["active"]),
        "file_progress_label": _file_progress_label(conn),
        "confirmation_image_retention": evidence_retention_status(conn),
        "capture_interval_seconds": interval_seconds,
        **adaptive_capture,
        "pause_mode": "screen_capture" if not screen_capture_enabled else pause_state["mode"],
        "pause_label": (
            "屏幕采集已关闭"
            if not screen_capture_enabled
            else pause_state["label"]
        ),
        "pause_until": "" if not screen_capture_enabled else pause_until,
        "paused_remaining_seconds": 0 if not screen_capture_enabled else paused_remaining,
        "locked": capture_blocked_by_session,
        "session_state": session_state_kind,
        "capture_allowed_by_session": screen_capture_enabled and session_state_kind == "unlocked",
        "last_capture_at": last_capture_at,
        "last_success_at": last_success_at,
        "last_attempt_at": last_attempt_at,
        "next_capture_at": next_capture_at,
        "next_capture_in_seconds": next_capture_in,
        "last_error": last_error,
        "today_activity_count": int(activity_count or 0),
        "today_privacy_count": int(privacy_count or 0),
        "today_gap_count": int(gap_count or 0),
        # Filled from a fresh confirmed view while the final shared
        # authorization lease is held below.
        "confirmed_activity": {},
        "document_changes": {},
        "raw_residue_count": raw_residue,
        "runtime_identity": runtime_identity,
        "dashboard_health": dashboard_health,
        "capture_freshness": capture_freshness,
        "screen_recording_permission": screen_recording_permission,
        "status_explanation": _runtime_health_explanation(liveness) or _status_explanation(
            status,
            pause_state=pause_state,
            locked=locked,
            launch_agent=launch_agent,
            raw_residue=raw_residue,
            last_error=last_error,
            capture_freshness=capture_freshness,
            ),
        "attention_items": attention_items,
        "guided_repairs": guided_repairs,
        "session_transition_evidence": transition_evidence,
        "soak_scorecard": soak_scorecard,
        "coverage_health": coverage_health,
        "privacy_health": privacy_health,
        "confirmation_evidence_health": confirmation_evidence_health,
        "private_purge": private_purge_health,
        "privacy_profile": privacy_profile_snapshot,
        "quality_health": quality_health,
        "structured_signal_health": structured_signal_health,
        "hygiene_cleanup": hygiene_cleanup,
        "ha_automation": ha_automation,
        **activity_session_summary,
        "quality_advice": quality_advice,
        "dashboard_bind": "127.0.0.1",
        "raw_access": False,
        "retention_policy": _get_setting(conn, "retention_policy", DEFAULT_RETENTION_POLICY),
        "retention_label": RETENTION_LABEL,
        "retention_window_description": RETENTION_WINDOW_DESCRIPTION,
        "dev_dashboard_enabled": _dev_dashboard_enabled(conn),
        "launch_agent_label": LAUNCH_AGENT_ID,
        "launch_agent_loaded": launch_agent["loaded"],
        "launch_agent_running": launch_agent["running"],
        "launch_agent_pid": launch_agent["pid"],
        "launch_agent_last_exit_code": launch_agent["last_exit_code"],
    }
    return _complete_status_authorized(conn, payload, start, end)


def _complete_status_authorized(conn, payload: dict, start: str, end: str) -> dict:
    """Attach content-bearing status fields under one final shared lease."""
    from .document_changes import (
        INTERACTIVE_AUTHORIZATION_LOCK_TIMEOUT_SECONDS,
        _document_changes_status_locked,
        _list_document_changes_locked,
        document_authorization_lock,
    )

    with document_authorization_lock(
        conn,
        exclusive=False,
        timeout=INTERACTIVE_AUTHORIZATION_LOCK_TIMEOUT_SECONDS,
    ):
        # This is intentionally re-read at the end instead of retained from the
        # long-running health probes above.  The lease covers both the read and
        # final response assembly, so a delete/reclassification cannot commit
        # and then be followed by a status payload containing the old summary.
        from .presentation_scope import for_connection, read_view, ACTIVITY_TABLES
        confirmation_view = confirmed_activity_view(read_view(conn, for_connection(conn), ACTIVITY_TABLES), start, end)
        payload["confirmed_activity"] = _confirmed_activity_status(confirmation_view, start, end)
        document_tracking = _document_changes_status_locked(conn)
        document_changes = _list_document_changes_locked(
            conn,
            start,
            end,
            limit=100,
            for_export=False,
            sort_order="recent",
        )
        document_changes["roots"] = document_tracking.get("roots", [])
        if "root_control_refs_incomplete" in document_tracking.get("limitations", []):
            document_changes["limitations"] = list(dict.fromkeys([
                *document_changes.get("limitations", []), "root_control_refs_incomplete"
            ]))
        payload["document_changes"] = document_changes
        return payload


def _confirmed_activity_status(view: dict, start: str, end: str) -> dict:
    # Status is an export/health contract, not the owner-facing local
    # observation timeline.  Count only explicit user-confirmed activities.
    activities = list(view.get("trusted_activities") or [])
    normalized: list[dict] = []
    total_duration_seconds = 0
    for item in activities:
        started_at = str(item.get("started_at") or "")
        ended_at = str(item.get("ended_at") or started_at)
        try:
            duration_seconds = max(0, int((parse_time(ended_at) - parse_time(started_at)).total_seconds()))
        except (TypeError, ValueError):
            duration_seconds = 0
        total_duration_seconds += duration_seconds
        apps = item.get("apps")
        if isinstance(apps, str):
            try:
                apps = json.loads(apps)
            except json.JSONDecodeError:
                apps = []
        app = safe_decision_text(apps[0] if isinstance(apps, list) and apps else "", max_len=80)
        normalized.append(
            {
                "summary": safe_decision_text(item.get("activity_summary") or "", max_len=180),
                "app": app,
                "started_at": started_at,
                "ended_at": ended_at,
                "duration_seconds": duration_seconds,
                "occurrence_state": str(item.get("occurrence_state") or "evidence_confirmed"),
                "confirmation_source": str(item.get("confirmation_source") or "screen_observation"),
            }
        )
    primary = max(
        normalized,
        key=lambda item: (int(item["duration_seconds"]), str(item["ended_at"])),
        default=None,
    )
    return {
        "source": "confirmed_activity_view",
        "raw_access": False,
        "time_range": {"from": start, "to": end},
        "confirmed_count": len(activities),
        "pending_count": int(view.get("pending_confirmation_count") or 0),
        "user_confirmed_count": int(view.get("user_confirmed_count") or 0),
        "user_rejected_count": int(view.get("user_rejected_count") or 0),
        "auto_suppressed_count": int(view.get("auto_suppressed_count") or 0),
        "total_duration_seconds": total_duration_seconds,
        "primary_activity": primary,
    }


def _file_progress_label(conn):
    row = conn.execute("SELECT COUNT(*),COALESCE(SUM(baseline_status='complete'),0) FROM file_watch_roots WHERE enabled=1").fetchone()
    gaps = conn.execute("SELECT COUNT(*) FROM file_watch_coverage_gaps WHERE affects_attribution=1").fetchone()[0]
    pending = _get_state(conn, "document_change_pending_events", "0")
    replay = " · 续读核验中" if _get_state(conn, "document_resume_pending", "false") == "true" else ""
    return f"基线 {row[1]}/{row[0]} · 待处理 {pending} · 历史缺口 {gaps}{replay}"


def get_quick_status(conn: sqlite3.Connection) -> dict:
    """Return a read-only, content-free snapshot for lightweight menu refreshes.

    Only a bounded LaunchAgent read is allowed beside indexed SQLite reads.
    No session/port probes, heavy diagnostics, writes, or migrations run here.
    """
    service = _quick_launch_agent_status()
    if service is None:
        observed_at = datetime.now(timezone.utc).isoformat()
        return {"status": "unavailable", "kind": "warning", "interface": "quick_status",
                "checked_at": observed_at, "observed_at": observed_at,
                "last_successful_read_at": None, "actions_enabled": False,
                "reason_code": "service_status_unavailable", "capture_allowed_by_session": False,
                "attention_codes": ["service_status_unavailable"], "raw_access": False}
    interval_seconds = _safe_positive_int(
        _get_setting(conn, "capture_interval_seconds", str(DEFAULT_INTERVAL_SECONDS)),
        DEFAULT_INTERVAL_SECONDS,
    )
    adaptive_capture = _adaptive_capture_status_snapshot(conn, interval_seconds)
    effective_interval_seconds = adaptive_capture["effective_capture_interval_seconds"]
    capture_lease = capture_lease_snapshot(conn)
    screen_capture_enabled = capture_lease["enabled"]
    stored_status = _get_state(conn, "daemon_status", "unknown")
    stored_locked = _get_state(conn, "locked", "true").strip().lower() == "true"
    stored_session_state = _get_state(conn, "session_state", "").strip().lower()
    if stored_session_state not in {"unlocked", "locked", "unknown"}:
        stored_session_state = "locked" if stored_locked else "unknown"

    pause_mode = _get_state(conn, "pause_mode", "").strip().lower()
    pause_until = _get_state(conn, "pause_until", "")
    paused_remaining = _paused_remaining_seconds(pause_until)
    pause_active = pause_mode in {"until_lock", "manual"} or paused_remaining > 0
    if pause_mode == "until_lock" and stored_session_state == "locked":
        pause_active = False

    status = stored_status
    if not screen_capture_enabled:
        status = "paused"
    elif pause_active:
        status = "paused"
    elif stored_session_state == "locked":
        status = "locked"
    elif stored_session_state == "unknown":
        status = "session_state_unknown"

    last_capture_at = _get_state(conn, "last_capture_at", "")
    last_success_at = _get_state(conn, "last_success_at", "") or last_capture_at
    last_attempt_at = _get_state(conn, "last_attempt_at", "")
    liveness = _runtime_health_snapshot(conn, service, pause_active, stored_session_state)
    if liveness["reason_code"]:
        status = "stopped" if liveness["reason_code"] == "service_stopped" else "error"
    if status == "partial":
        status = "error"
    last_error_present = bool(_get_state(conn, "last_error", ""))
    private_purge_pending = _safe_nonnegative_int(
        _get_state(conn, "private_purge_pending_count", "0"),
    )
    next_capture_at, next_capture_in = _next_capture_window(
        last_success_at,
        effective_interval_seconds,
        status if screen_capture_enabled else "paused",
        launch_agent_running=service["running"],
    )

    attention_codes: list[str] = []
    if liveness["reason_code"]:
        attention_codes.append(liveness["reason_code"])
    if _get_state(conn, "retention_cleanup_last_error", ""):
        attention_codes.append("retention_cleanup_failed")
    freshness = _capture_freshness(last_success_at, effective_interval_seconds, status,
        launch_agent_loaded=service["loaded"], launch_agent_running=service["running"],
        screen_capture_enabled=screen_capture_enabled)
    if freshness.get("is_stale") is True:
        attention_codes.append("capture_stale")
    if last_error_present and screen_capture_enabled:
        attention_codes.append("last_error")
    if stored_session_state == "unknown":
        attention_codes.append("session_state_unknown")
    if private_purge_pending:
        attention_codes.append("private_purge_pending")

    return {
        "status": status,
        "screen_capture_enabled": screen_capture_enabled,
        "screen_capture_lease": capture_lease,
        "recording_pause_active": pause_active,
        "file_progress_label": _file_progress_label(conn),
        "confirmation_image_retention": evidence_retention_status(conn),
        "kind": "warning" if attention_codes else "ok",
        "interface": "quick_status",
        "runtime_health": liveness,
        "reason_code": liveness["reason_code"],
        "status_explanation": _runtime_health_explanation(liveness),
        "launch_agent_loaded": service["loaded"],
        "launch_agent_running": service["running"],
        "capture_freshness": freshness,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "capture_interval_seconds": interval_seconds,
        **adaptive_capture,
        "pause_mode": "screen_capture" if not screen_capture_enabled else (pause_mode if pause_active else ""),
        "pause_label": "屏幕采集已关闭" if not screen_capture_enabled else "",
        "pause_until": pause_until if pause_active and screen_capture_enabled else "",
        "paused_remaining_seconds": paused_remaining if pause_active and screen_capture_enabled else 0,
        "locked": stored_session_state != "unlocked",
        "session_state": stored_session_state,
        "capture_allowed_by_session": screen_capture_enabled and stored_session_state == "unlocked",
        "last_attempt_at": last_attempt_at,
        "last_success_at": last_success_at,
        "last_capture_at": last_capture_at,
        "next_capture_at": next_capture_at,
        "next_capture_in_seconds": next_capture_in,
        "last_error_present": last_error_present,
        "attention_codes": attention_codes,
        "private_purge": {
            "status": "pending" if private_purge_pending else "ok",
            "pending_count": private_purge_pending,
            "raw_access": False,
        },
        "raw_access": False,
    }



def _runtime_health_snapshot(conn, service: dict, pause_active: bool, session: str) -> dict:
    expected = (not pause_active and session in {"unlocked", "locked"}
                and _bool_setting(conn, "document_change_tracking_enabled", True)
                and conn.execute("SELECT 1 FROM file_watch_roots WHERE enabled=1 LIMIT 1").fetchone() is not None)
    return runtime_health(conn, service, document_expected=expected)


def _runtime_health_explanation(health: dict) -> str:
    reason = health.get("reason_code", "")
    if reason == "service_stopped":
        return "自动记录后台已停止，请点击开始记录恢复。"
    if reason.startswith("daemon_heartbeat_"):
        return "后台进度尚未确认或已停止更新，请检查服务。"
    if reason.startswith("document_worker_heartbeat_"):
        return "文件追踪进度尚未确认或已停止更新，请检查文件监控。"
    return ""


def _quick_launch_agent_status() -> dict | None:
    try:
        result = subprocess.run(
            ["/bin/launchctl", "print", f"gui/{os.getuid()}/{LAUNCH_AGENT_ID}"],
            capture_output=True, text=True, timeout=0.5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        # Only an explicit missing-service response proves stopped. A denied,
        # malformed or unavailable query must not masquerade as a stopped job.
        if "Could not find service" in result.stderr:
            return {"loaded": False, "running": False, "pid": ""}
        return None
    pid = next((line.strip().removeprefix("pid = ") for line in result.stdout.splitlines()
                if line.strip().startswith("pid = ")), "")
    return {"loaded": True, "running": "state = running" in result.stdout and pid.isdigit(), "pid": pid}


def _safe_positive_int(value: str, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return int(default)


def _safe_nonnegative_int(value: str, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return max(0, int(default))


def _adaptive_capture_status_snapshot(
    conn: sqlite3.Connection,
    base_interval_seconds: int,
) -> dict:
    base_interval = _safe_positive_int(base_interval_seconds, DEFAULT_INTERVAL_SECONDS)
    allowed_intervals = {
        base_interval,
        max(base_interval, 30),
        max(base_interval, 60),
    }
    effective_interval = _safe_positive_int(
        _get_state(conn, "effective_capture_interval_seconds", str(base_interval)),
        base_interval,
    )
    if effective_interval not in allowed_intervals:
        effective_interval = base_interval
    reason = str(_get_state(conn, "capture_backoff_reason", "baseline") or "baseline").strip().lower()
    if reason not in CAPTURE_BACKOFF_REASONS:
        reason = "baseline"
    return {
        "capture_schedule_mode": CAPTURE_SCHEDULE_MODE,
        "effective_capture_interval_seconds": effective_interval,
        "capture_backoff_reason": reason,
        "capture_unchanged_streak": _safe_nonnegative_int(
            _get_state(conn, "capture_unchanged_streak", "0"),
        ),
    }


STATUS_HEALTH_BUSY_TIMEOUT_MS = 1000


def _status_health_subcheck(conn: sqlite3.Connection, build, fallback):
    conn.execute(f"PRAGMA busy_timeout = {STATUS_HEALTH_BUSY_TIMEOUT_MS}")
    try:
        return build()
    except sqlite3.OperationalError as exc:
        if _sqlite_busy(exc):
            return fallback()
        raise
    finally:
        conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")


def _sqlite_busy(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "database is locked" in message or "database table is locked" in message


def _safe_daemon_failure_code(scope: str, exc: Exception) -> str:
    """Return a stable error code without persisting exception details."""

    if isinstance(exc, sqlite3.OperationalError) and _sqlite_busy(exc):
        return "database_busy"
    return f"{scope}_failed"


def _emit_document_change_transition_diagnostic(payload: dict) -> None:
    """Append one already-sanitized worker transition to the private daemon log."""

    print(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ),
        file=sys.stderr,
        flush=True,
    )


def _ha_automation_busy_status() -> dict:
    return {
        "enabled": True,
        "interval_minutes": 60,
        "trigger_mode": "direct_hermes",
        "hermes_profile": "activity-analysis",
        "status": "unavailable",
        "label": "暂时不可读",
        "kind": "warning",
        "summary": "HA 自动化状态暂时不可读：数据库正在被后台记录任务占用；下次刷新会重试。",
        "runtime_allowed": True,
        "runtime_reason": "application_support_live_runtime",
        "preflight": {"ready": False, "summary": "数据库忙，暂时无法读取 HA 自动化状态。"},
        "current_hour_due": False,
        "due": False,
        "last_generated_at": "",
        "last_triggered_at": "",
        "last_package_id": "",
        "last_error": "database_busy",
        "pending_count": 0,
        "failed_count": 0,
        "dead_letter_count": 0,
        "historical_dead_letter_count": 0,
        "unresolved_dead_letter_count": 0,
        "retry_due_count": 0,
        "stale_pending_count": 0,
        "memory_report_pending_count": 0,
        "memory_report_failed_count": 0,
        "memory_report_retry_due_count": 0,
        "active_package_count": 0,
        "active_auto_package_count": 0,
        "latest_result_summary": "数据库忙，暂时无法读取最近 HA 结果。",
        "latest_candidate_count": 0,
        "latest_eligible_candidate_count": 0,
        "latest_review_only_candidate_count": 0,
        "latest_ephemeral_candidate_count": 0,
        "latest_single_evidence_eligible_count": 0,
        "latest_subject_risk_candidate_count": 0,
        "latest_single_evidence_demoted_count": 0,
        "latest_media_or_file_listing_ephemeral_count": 0,
        "latest_memories_written_count": 0,
        "latest_written_candidate_count": 0,
        "latest_skipped_candidate_count": 0,
        "latest_written_candidate_types": [],
        "latest_skipped_reasons": [],
        "latest_write_audit_summary": "",
        "latest_written_memory_summaries": [],
        "latest_memory_report_status": "",
        "latest_memory_report_error": "",
        "latest_memory_reported_at": "",
        "latest_memory_report_attempt_count": 0,
        "latest_candidate_skip_reason": "",
    }


def _quality_advice_busy_status() -> dict:
    return {
        "enabled": True,
        "profile": "activity-quality-analysis",
        "interval_hours": 24,
        "status": "unavailable",
        "label": "暂时不可读",
        "kind": "warning",
        "summary": "质量顾问状态暂时不可读：数据库正在被后台记录任务占用；下次刷新会重试。",
        "runtime_allowed": True,
        "runtime_reason": "application_support_live_runtime",
        "preflight": {"ready": False, "summary": "数据库忙，暂时无法读取质量顾问状态。"},
        "due": False,
        "last_run_at": "",
        "last_error": "database_busy",
        "latest_recommendation_count": 0,
        "latest_issue_count": 0,
        "latest_memory_candidate_count": 0,
        "latest_document_match_count": 0,
        "latest_summary": "数据库忙，暂时无法读取最近质量建议。",
        "latest_run": {},
    }


def _activity_sessions_busy_status() -> dict:
    return {
        "current_activity_session": None,
        "today_activity_sessions": {
            "session_count": 0,
            "summary": {
                "total_duration_seconds": 0,
                "total_duration_label": "0s",
                "session_count": 0,
                "work_seconds": 0,
                "work_share": 0.0,
                "media_seconds": 0,
                "media_share": 0.0,
                "top_categories": [],
                "top_apps": [],
            },
            "screen_time_boundary": {
                "source": "pam_local_timeline",
                "system_screen_time_used": False,
                "reason": "activity session summary temporarily unavailable because SQLite is busy.",
            },
        },
    }


def pause(conn, minutes: int | None = DEFAULT_PAUSE_MINUTES, mode: str = "duration") -> dict:
    from .document_changes import (
        document_authorization_lock,
        reset_document_baseline_for_pause,
    )

    # A content checkpoint uses the same lock in shared mode before its
    # read-only authorization probe.  Taking the exclusive lease before the
    # pause transaction means that probe either finishes first or observes the
    # committed epoch/pause state; it can never read the old grant alongside an
    # in-flight pause writer.
    with document_authorization_lock(conn, exclusive=True):
        ensure_daemon_defaults(conn)
        mode = (mode or "duration").strip().lower()
        if mode in {"until_lock", "manual"}:
            until_value = ""
        elif mode == "today":
            tomorrow = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            until_value = tomorrow.astimezone(timezone.utc).isoformat(timespec="seconds")
        else:
            mode = "duration"
            until = datetime.now(timezone.utc) + timedelta(minutes=max(1, int(minutes or DEFAULT_PAUSE_MINUTES)))
            until_value = until.isoformat(timespec="seconds")
        _set_state(conn, "pause_until", until_value)
        _set_state(conn, "pause_mode", mode)
        _set_state(conn, "daemon_status", "paused")
        _set_state(conn, "last_error", "")
        reset_document_baseline_for_pause(conn)
        conn.commit()
        snapshot = _pause_snapshot(conn, locked=False)
    return {
        "paused": True,
        "pause_mode": mode,
        "pause_until": until_value,
        "pause_label": snapshot["label"],
    }


def resume(conn) -> dict:
    ensure_daemon_defaults(conn)
    _clear_pause_state(conn)
    _set_state(conn, "daemon_status", "ready")
    _set_state(conn, "last_error", "")
    conn.commit()
    return {"paused": False, "status": "ready"}


def run_once(db_path: str | Path, *, ignore_pause: bool = False) -> dict:
    root = _runtime_root_for_db_path(db_path)
    try:
        _raw_vault(root)
    except RawVaultSafetyError:
        return {"status": "error", "error": "raw_cleanup_failed", "captured": 0, "skipped": 0, "errors": 1}
    conn = ensure_ready(db_path)
    try:
        with _capture_lock(root) as acquired:
            if not acquired:
                result = {"status": "busy", "captured": 0, "skipped": 0, "errors": 0}
            else:
                result = _run_once_locked(conn, root, db_path=db_path, ignore_pause=ignore_pause)
            record_capture_cycle(conn, root, result)
            return result
    finally:
        conn.close()


def _run_once_locked(conn, root: Path, *, db_path: str | Path, ignore_pause: bool = False) -> dict:
    try:
        ensure_daemon_defaults(conn)
        _apply_privacy_profile_from_settings(conn)
        cleanup_raw_vault(root)
        vlm_cleanup = cleanup_retired_review_files(conn, root)
        _sync_private_purge_daemon_state(
            conn,
            failed=bool(vlm_cleanup.get("file_purge_failed")),
        )
        blocked = _capture_block_reason(conn, root, ignore_pause=ignore_pause)
        if blocked:
            return blocked

        last_attempt_at = utc_now()
        _set_state(conn, "last_attempt_at", last_attempt_at)
        _set_state(conn, "locked", "false")
        _set_state(conn, "session_state", "unlocked")
        _set_state(conn, "daemon_status", "capturing")
        conn.commit()

        def should_process(metadata: dict) -> bool:
            frame_hash = str(metadata.get("frame_hash") or "")
            display_id = str(metadata.get("display_id") or "")
            if not frame_hash or not display_id:
                return True
            row = conn.execute(
                "SELECT frame_hash FROM capture_fingerprints WHERE display_id = ?",
                (display_id,),
            ).fetchone()
            return row is None or row["frame_hash"] != frame_hash

        batch = capture_sck_keyframe_batch(root, should_process=should_process, retain_for_ingest=True)
        events = batch["events"]
        skipped = batch["skipped"]
        errors = batch["errors"]
        adaptive_observation = observation_payload(
            capture_observation(events, skipped, errors)
        )
        blocked_after_capture = _capture_block_reason(conn, root, ignore_pause=ignore_pause, suffix="_after_capture")
        if blocked_after_capture:
            cleanup_raw_vault(root)
            blocked_after_capture["discarded"] = len(events) + len(skipped) + len(errors)
            return blocked_after_capture
        report = (
            _ingest_and_fingerprint(conn, events, skipped, errors)
            if events or skipped or errors
            else {"ingested": 0, "clear": 0, "partial_redacted": 0, "blocked": 0, "review_required": 0, "duplicate_exact": 0, "partial_duplicate": 0}
        )
        if errors:
            batch_status = "partial" if events or skipped else "error"
            _set_state(conn, "last_error", _capture_batch_last_error(errors))
            _set_state(conn, "daemon_status", batch_status)
        else:
            batch_status = "ready"
            last_success_at = utc_now()
            _set_state(conn, "last_capture_at", last_success_at)
            _set_state(conn, "last_success_at", last_success_at)
            _set_state(conn, "last_error", "")
            _set_state(conn, "daemon_status", "ready")
        conn.commit()
        cleanup_raw_vault(root)
        return {
            "status": batch_status,
            "captured": len(events),
            "skipped": len(skipped),
            "errors": len(errors),
            "ingest": report,
            "capture_observation": adaptive_observation,
        }
    except Exception as exc:
        cleanup_failed = False
        try:
            cleanup_raw_vault(root)
        except Exception:
            cleanup_failed = True
        message = "raw_cleanup_failed" if cleanup_failed else _safe_daemon_failure_code("capture", exc)
        _set_state(conn, "last_error", message)
        _set_state(conn, "daemon_status", "error")
        conn.commit()
        return {"status": "error", "error": message, "captured": 0, "skipped": 0, "errors": 1}


def _capture_batch_last_error(errors: list[dict]) -> str:
    safe_signals = " ".join(
        str(item.get(key) or "")
        for item in errors
        if isinstance(item, dict)
        for key in ("status", "reason", "error")
    ).lower()
    structured_denial = "screen_recording_permission_denied" in safe_signals
    capture_context = any(
        token in safe_signals
        for token in ("screen recording", "screencapturekit", "cgdisplaystream", "tcc")
    )
    denial_signal = any(
        token in safe_signals
        for token in ("permission denied", "not authorized", "user declined", "权限", "拒绝")
    )
    if structured_denial or (capture_context and denial_signal):
        return "screen_recording_permission_denied"
    return f"capture_batch_failed:{len(errors)}_display(s)"


def run_daemon(
    db_path: str | Path,
    *,
    once: bool = False,
    interval_seconds: int | None = None,
    dev_dashboard: bool = False,
) -> dict | None:
    root = _runtime_root_for_db_path(db_path)
    try:
        _raw_vault(root)
    except RawVaultSafetyError:
        return {"status": "error", "error": "raw_cleanup_failed"}
    cleanup_legacy_server_pid(root)
    conn = ensure_ready(db_path)
    try:
        ensure_daemon_defaults(conn)
        lock = session_state(root)
        observed_at = utc_now()
        record_session_transition_evidence(conn, lock, observed_at=observed_at, daemon_start=True)
        observe_system_activity_session(conn, locked=bool(lock.get("locked")), observed_at=observed_at, daemon_start=True)
        if interval_seconds is not None:
            _set_setting(conn, "capture_interval_seconds", str(max(5, interval_seconds)))
        if dev_dashboard:
            _set_setting(conn, "dev_dashboard_enabled", "true")
        conn.commit()
    finally:
        conn.close()

    if once:
        return run_once(db_path)

    document_reconcile_enabled = _document_change_startup_reconcile_enabled()
    startup_maintenance = _run_daemon_startup_maintenance(
        db_path,
        root,
        document_reconcile_enabled=document_reconcile_enabled,
    )
    conn = ensure_ready(db_path)
    try:
        base_interval = _safe_positive_int(
            _get_setting(conn, "capture_interval_seconds", str(DEFAULT_INTERVAL_SECONDS)),
            DEFAULT_INTERVAL_SECONDS,
        )
    finally:
        conn.close()
    capture_state = initial_capture_state(base_interval)
    _persist_adaptive_capture_state(db_path, capture_state)
    wait_session_state = _session_state_kind(lock)
    session_provider = _PersistentSessionStateProvider(root)
    # run_once() already cleans the raw vault on every capture cycle.  Start
    # the hourly fallback clock here so cold start does not immediately repeat
    # that filesystem pass before the first capture.
    last_raw_cleanup = time.monotonic()
    last_evidence_cleanup = 0.0
    last_closure_check = 0.0
    last_retention_day = str(startup_maintenance["retention_day"])
    heartbeat = HeartbeatWriter("daemon")
    try:
        while True:
            loop_start = time.monotonic()
            conn = ensure_ready(db_path)
            try:
                ensure_daemon_defaults(conn)
                heartbeat.touch(conn)
                conn.commit()
                screen_capture_enabled = expire_capture_lease(conn)["enabled"]
                if loop_start - last_evidence_cleanup >= 60:
                    from .confirmation_evidence import purge_scheduled_evidence
                    try:
                        cleanup = purge_scheduled_evidence(conn)
                        _set_state_if_changed(conn, "confirmation_evidence_cleanup", json.dumps(cleanup))
                    except (OSError, sqlite3.Error, RuntimeError):
                        conn.rollback()
                        _set_state_if_changed(conn, "confirmation_evidence_cleanup", '{"status":"retry_pending"}')
                    conn.commit()
                    last_evidence_cleanup = loop_start
                interval = _safe_positive_int(
                    _get_setting(conn, "capture_interval_seconds", str(DEFAULT_INTERVAL_SECONDS)),
                    DEFAULT_INTERVAL_SECONDS,
                )
                _set_state_if_changed(
                    conn,
                    "daemon_status",
                    "running" if screen_capture_enabled else "paused",
                )
                if dev_dashboard or _dev_dashboard_enabled(conn):
                    dashboard_start = _start_dashboard_thread(db_path)
                    _set_state(conn, "dashboard_start_status", str(dashboard_start.get("status") or "unknown"))
                    _set_state(
                        conn,
                        "dashboard_last_error",
                        "dashboard_port_conflict" if dashboard_start.get("status") == "port_conflict" else "",
                    )
                conn.commit()
            finally:
                conn.close()

            if loop_start - last_closure_check >= 60:
                # Independent sidecar, outside the activity transaction. No new
                # service and no semantic/model calls in the capture loop.
                from .task_closure import wake as closure_tick
                try:
                    closure_tick(db_path)
                except (OSError, sqlite3.Error, ValueError, RuntimeError):
                    pass
                last_closure_check = loop_start

            if loop_start - last_raw_cleanup > 3600:
                cleanup_raw_vault(Path(db_path).resolve().parent)
                last_raw_cleanup = loop_start

            today = datetime.now().astimezone().date().isoformat()
            if today != last_retention_day:
                conn = ensure_ready(db_path)
                try:
                    _mark_daily_maintenance_pending(conn, today)
                finally:
                    conn.close()
                last_retention_day = today

            if not screen_capture_enabled:
                _maybe_start_daily_maintenance_worker(db_path)
                if document_reconcile_enabled:
                    _maybe_start_document_change_worker_when_eligible(
                        db_path,
                        root,
                    )
                _maybe_start_ha_automation_worker(db_path)
                _maybe_start_quality_advice_worker(db_path)
                _sleep_between_daemon_cycles(SCREEN_CAPTURE_DISABLED_POLL_SECONDS, grant_db_path=db_path)
                continue

            result = run_once(db_path)
            capture_state, idle_seconds, wait_session_state = _advance_adaptive_capture_schedule(
                capture_state,
                result,
                base_interval_seconds=interval,
                previous_session_state=wait_session_state,
            )
            _persist_adaptive_capture_state(db_path, capture_state)
            _maybe_start_daily_maintenance_worker(db_path)
            if document_reconcile_enabled:
                _maybe_start_document_change_worker_when_eligible(
                    db_path,
                    root,
                )
            _maybe_start_ha_automation_worker(db_path)
            _maybe_start_quality_advice_worker(db_path)
            elapsed = time.monotonic() - loop_start
            wait_seconds = max(0.0, capture_state.effective_interval_seconds - elapsed)
            deadline_conn = connect_readonly(db_path, busy_timeout_seconds=0.2)
            try:
                lease_view = capture_lease_snapshot(deadline_conn)
                wait_seconds = min(wait_seconds, max(0, lease_view["remaining_seconds"]))
            finally:
                deadline_conn.close()
            long_wait = capture_state.effective_interval_seconds > interval
            session_blocked = wait_session_state != "unlocked"
            if long_wait or session_blocked:
                capture_state, wait_session_state, _ = _wait_for_adaptive_capture_deadline(
                    db_path,
                    root,
                    capture_state,
                    wait_seconds=wait_seconds,
                    previous_session_state=wait_session_state,
                    previous_idle_seconds=idle_seconds,
                    base_interval_seconds=interval,
                    document_reconcile_enabled=document_reconcile_enabled,
                    session_probe=session_provider,
                    heartbeat=heartbeat,
                )
            else:
                _sleep_between_daemon_cycles(max(1.0, wait_seconds))
    finally:
        session_provider.close()


def _sleep_between_daemon_cycles(seconds: float, *, grant_db_path=None) -> None:
    """Keep daemon-loop test control isolated from process-global time.sleep.

    Patching ``time.sleep`` on one imported module mutates the shared stdlib
    module and can consume test side effects inside subprocess/SQLite startup
    helpers before the daemon reaches its loop.  This narrow seam controls
    only the intended cycle boundary and leaves production behavior unchanged.
    """
    if grant_db_path is None:
        time.sleep(seconds)
        return
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(min(2.0, max(0.0, deadline - time.monotonic())))
        try:
            read_conn = connect_readonly(grant_db_path, busy_timeout_seconds=0.2)
            try:
                if capture_lease_snapshot(read_conn)["enabled"]:
                    return
            finally:
                read_conn.close()
        except sqlite3.Error:
            pass


def _advance_adaptive_capture_schedule(
    state: AdaptiveCaptureState,
    result: object,
    *,
    base_interval_seconds: int,
    previous_session_state: str,
) -> tuple[AdaptiveCaptureState, int | None, str]:
    payload = result if isinstance(result, dict) else {}
    status = str(payload.get("status") or "error").strip().lower()
    session_kind = previous_session_state
    if status.startswith("locked"):
        session_kind = "locked"
        return (
            reset_capture_state(
                state,
                reason="session_locked",
                base_interval_seconds=base_interval_seconds,
                clear_display_set=True,
            ),
            None,
            session_kind,
        )
    if status.startswith("session_state_unknown"):
        session_kind = "unknown"
        return (
            reset_capture_state(
                state,
                reason="session_unknown",
                base_interval_seconds=base_interval_seconds,
                clear_display_set=True,
            ),
            None,
            session_kind,
        )
    if status.startswith("paused"):
        return (
            reset_capture_state(
                state,
                reason="paused",
                base_interval_seconds=base_interval_seconds,
                clear_display_set=True,
            ),
            None,
            session_kind,
        )

    observation = observation_from_payload(payload.get("capture_observation"))
    if observation is None:
        failed = status in {"error", "partial"} or _safe_nonnegative_int(payload.get("errors", 0)) > 0
        return (
            reset_capture_state(
                state,
                reason="capture_error" if failed else "ambiguous_skip",
                base_interval_seconds=base_interval_seconds,
                clear_display_set=True,
            ),
            None,
            session_kind,
        )

    session_kind = "unlocked"
    idle_probe = _safe_system_idle_probe()
    idle_seconds = _idle_seconds_from_probe(idle_probe)
    potential_streak = (
        state.unchanged_streak + 1
        if observation.all_unchanged
        and bool(state.display_ids)
        and observation.display_ids == state.display_ids
        else 0
    )
    on_battery = False
    low_power_mode = False
    if potential_streak >= 5:
        try:
            power = power_state()
        except Exception:
            power = {}
        if isinstance(power, dict):
            on_battery = str(power.get("source") or "").strip().lower() == "battery_power"
            low_power_mode = power.get("low_power_mode") is True
    return (
        advance_capture_state(
            state,
            observation,
            base_interval_seconds=base_interval_seconds,
            idle_seconds=idle_seconds,
            on_battery=on_battery,
            low_power_mode=low_power_mode,
        ),
        idle_seconds,
        session_kind,
    )


def _persist_adaptive_capture_state(
    db_path: str | Path,
    state: AdaptiveCaptureState,
) -> None:
    conn = ensure_ready(db_path)
    try:
        ensure_daemon_defaults(conn)
        _set_state_if_changed(conn, "capture_schedule_mode", CAPTURE_SCHEDULE_MODE)
        _set_state_if_changed(
            conn,
            "effective_capture_interval_seconds",
            str(max(1, int(state.effective_interval_seconds))),
        )
        reason = state.reason if state.reason in CAPTURE_BACKOFF_REASONS else "baseline"
        _set_state_if_changed(conn, "capture_backoff_reason", reason)
        _set_state_if_changed(
            conn,
            "capture_unchanged_streak",
            str(max(0, int(state.unchanged_streak))),
        )
        conn.commit()
    finally:
        conn.close()


def _safe_system_idle_probe() -> dict:
    try:
        probe = system_idle_seconds()
    except Exception:
        return {"available": False}
    return probe if isinstance(probe, dict) else {"available": False}


def _idle_seconds_from_probe(probe: object) -> int | None:
    if not isinstance(probe, dict) or probe.get("available") is not True:
        return None
    try:
        return max(0, int(probe.get("idle_seconds")))
    except (TypeError, ValueError):
        return None


def _wait_for_adaptive_capture_deadline(
    db_path: str | Path,
    root: Path,
    state: AdaptiveCaptureState,
    *,
    wait_seconds: float,
    previous_session_state: str,
    previous_idle_seconds: int | None,
    base_interval_seconds: int,
    document_reconcile_enabled: bool,
    session_probe,
    heartbeat: HeartbeatWriter | None = None,
    monotonic_clock=None,
    wall_clock=None,
    sleeper=None,
) -> tuple[AdaptiveCaptureState, str, str]:
    """Wait in monotonic five-second slices and wake on session/HID changes."""

    clock = monotonic_clock or time.monotonic
    wall = wall_clock or time.time
    sleep = sleeper or _sleep_between_daemon_cycles
    deadline = clock() + max(0.0, float(wait_seconds))
    session_kind = (
        previous_session_state
        if previous_session_state in {"unlocked", "locked", "unknown"}
        else "unknown"
    )
    session_blocked = session_kind != "unlocked"
    idle_seconds = previous_idle_seconds

    while True:
        if session_blocked:
            sleep_seconds = ADAPTIVE_CAPTURE_WAIT_POLL_SECONDS
        else:
            remaining = deadline - clock()
            if remaining <= 0:
                return state, session_kind, "deadline"
            sleep_seconds = min(ADAPTIVE_CAPTURE_WAIT_POLL_SECONDS, remaining)

        sleep_started = clock()
        wall_sleep_started = wall()
        sleep(max(0.001, sleep_seconds))
        sleep_elapsed = max(0.0, clock() - sleep_started)
        wall_sleep_elapsed = max(0.0, wall() - wall_sleep_started)
        resumed_after_suspend = bool(
            sleep_elapsed > sleep_seconds + 1.0
            or wall_sleep_elapsed - sleep_elapsed > 1.0
        )
        if document_reconcile_enabled:
            _maybe_start_document_change_worker_when_eligible(db_path, root)

        try:
            lock = session_probe()
        except Exception:
            lock = _unknown_session_state("session_state_unavailable")
        observed_session_kind = _session_state_kind(lock if isinstance(lock, dict) else {})
        current_idle_seconds = _idle_seconds_from_probe(_safe_system_idle_probe())

        # Locked/unknown sessions can stay in this real worker loop indefinitely.
        # Renew progress only after the probes completed, and avoid opening the
        # database on every five-second poll while the heartbeat is still fresh.
        if heartbeat is not None and time.monotonic() - heartbeat.last_write >= HEARTBEAT_INTERVAL_SECONDS:
            conn = ensure_ready(db_path)
            try:
                heartbeat.touch(conn)
                conn.commit()
                if not expire_capture_lease(conn)["enabled"]:
                    return state, session_kind, "screen_grant_expired"
            finally:
                conn.close()

        if observed_session_kind != session_kind:
            _record_adaptive_wait_session_transition(db_path, lock)

        if observed_session_kind != "unlocked":
            reason = "session_locked" if observed_session_kind == "locked" else "session_unknown"
            reset_state = reset_capture_state(
                state,
                reason=reason,
                base_interval_seconds=base_interval_seconds,
                clear_display_set=True,
            )
            if reset_state != state:
                state = reset_state
                _persist_adaptive_capture_state(db_path, state)
            session_kind = observed_session_kind
            session_blocked = True
            idle_seconds = current_idle_seconds
            continue

        if session_kind != "unlocked":
            state = reset_capture_state(
                state,
                reason="session_resumed",
                base_interval_seconds=base_interval_seconds,
                clear_display_set=True,
            )
            _persist_adaptive_capture_state(db_path, state)
            return state, "unlocked", "session_resumed"

        if resumed_after_suspend:
            state = reset_capture_state(
                state,
                reason="system_resumed",
                base_interval_seconds=base_interval_seconds,
                clear_display_set=True,
            )
            _persist_adaptive_capture_state(db_path, state)
            return state, "unlocked", "system_resumed"

        if idle_seconds is None or current_idle_seconds is None:
            state = reset_capture_state(
                state,
                reason="hid_unavailable",
                base_interval_seconds=base_interval_seconds,
            )
            _persist_adaptive_capture_state(db_path, state)
            return state, "unlocked", "hid_unavailable"

        if hid_input_detected(
            idle_seconds,
            current_idle_seconds,
            elapsed_seconds=sleep_elapsed,
        ):
            state = reset_capture_state(
                state,
                reason="input_activity",
                base_interval_seconds=base_interval_seconds,
            )
            _persist_adaptive_capture_state(db_path, state)
            return state, "unlocked", "input_activity"

        session_kind = "unlocked"
        session_blocked = False
        idle_seconds = current_idle_seconds


def _record_adaptive_wait_session_transition(db_path: str | Path, lock: object) -> None:
    normalized_lock = lock if isinstance(lock, dict) else _unknown_session_state("session_state_unavailable")
    session_kind = _session_state_kind(normalized_lock)
    observed_at = utc_now()
    conn = ensure_ready(db_path)
    try:
        ensure_daemon_defaults(conn)
        record_session_transition_evidence(conn, normalized_lock, observed_at=observed_at)
        observe_system_activity_session(
            conn,
            locked=session_kind != "unlocked",
            observed_at=observed_at,
        )
        _set_state(conn, "locked", "false" if session_kind == "unlocked" else "true")
        _set_state(conn, "session_state", session_kind)
        if session_kind == "unlocked":
            _set_state(conn, "daemon_status", "running")
        elif session_kind == "locked":
            _set_state(conn, "daemon_status", "locked")
        else:
            _set_state(conn, "daemon_status", "session_state_unknown")
            _set_state(conn, "last_error", "session_state_unavailable")
        conn.commit()
    finally:
        conn.close()


def _run_daemon_startup_maintenance(
    db_path: str | Path,
    root_dir: str | Path,
    *,
    document_reconcile_enabled: bool,
) -> dict:
    """Record deferred maintenance without doing bulk I/O during cold start."""
    retention_day = datetime.now().astimezone().date().isoformat()
    conn = ensure_ready(db_path)
    try:
        ensure_daemon_defaults(conn)
        retention = _mark_daily_maintenance_pending(conn, retention_day)
        hygiene = {
            "status": "waiting_for_eligibility",
            "applied": False,
            "raw_access": False,
        }
    finally:
        conn.close()

    # Preserve a valid baseline/cursor while withholding complete coverage until
    # the replay barrier drains. A missing/invalid cursor still requires a scan.
    from .document_changes import defer_document_change_startup_reconcile

    conn = ensure_ready(db_path)
    try:
        document_changes = defer_document_change_startup_reconcile(conn, preserve_valid=True)
        document_changes["startup_reconcile_status"] = (
            "waiting_for_eligibility"
            if document_reconcile_enabled
            else "disabled"
        )
    except Exception as exc:
        message = _safe_daemon_failure_code("document_change_startup_deferral", exc)
        print(message, file=sys.stderr)
        try:
            conn.rollback()
            _set_state(conn, "last_error", message)
            _set_state(conn, "daemon_status", "running")
            conn.commit()
        except Exception:
            pass
        document_changes = {
            "status": "error",
            "error": message,
            "raw_access": False,
        }
    finally:
        conn.close()
    return {
        "retention_day": retention_day,
        "retention": retention,
        "hygiene": hygiene,
        "document_changes": document_changes,
        "raw_access": False,
    }


def _document_change_worker_startup_eligibility(
    root_dir: str | Path,
    *,
    session: dict | None = None,
    power: dict | None = None,
    idle: dict | None = None,
) -> dict:
    if session is None:
        try:
            session = session_state(root_dir)
        except Exception:
            session = {}
    # File tracking has its own owner-approved locked-console policy.  Keep it
    # separate from the generic capture gate so screenshots/OCR remain blocked
    # while the display is locked.
    from .document_changes import _document_change_session_allows_content

    session_allowed = _document_change_session_allows_content(session)
    if not session_allowed:
        return {
            "allowed": False,
            "reason": "session_not_eligible",
            "idle_seconds": 0,
            "min_idle_seconds": DOCUMENT_CHANGE_STARTUP_MIN_IDLE_SECONDS,
            "raw_access": False,
        }
    if power is None:
        try:
            power = power_state()
        except Exception:
            power = {}
    # Battery use no longer blocks file tracking: the owner asked for coverage
    # whenever the Mac is in use, and Low Power Mode is the explicit signal
    # that the machine should stop doing optional work.  The heavy part of
    # this worker is its one-time baseline scan, which is throttled instead of
    # gated (see build_document_baseline).
    if isinstance(power, dict) and power.get("low_power_mode") is True:
        return {
            "allowed": False,
            "reason": "low_power_mode_active",
            "idle_seconds": 0,
            "min_idle_seconds": DOCUMENT_CHANGE_STARTUP_MIN_IDLE_SECONDS,
            "raw_access": False,
        }
    if DOCUMENT_CHANGE_STARTUP_MIN_IDLE_SECONDS <= 0:
        # Idle cannot change the decision at a zero threshold. Avoid launching
        # a redundant system probe while preserving the public status shape.
        idle_seconds = 0
        idle_available = True
        allowed = True
    else:
        if idle is None:
            try:
                idle = system_idle_seconds()
            except Exception:
                idle = {}
        try:
            idle_seconds = max(0, int(idle.get("idle_seconds") or 0)) if isinstance(idle, dict) else 0
        except (TypeError, ValueError):
            idle_seconds = 0
        idle_available = bool(isinstance(idle, dict) and idle.get("available") is True)
        allowed = idle_seconds >= DOCUMENT_CHANGE_STARTUP_MIN_IDLE_SECONDS
    return {
        "allowed": allowed,
        "reason": "" if allowed else ("idle_required" if idle_available else "idle_unavailable"),
        "idle_seconds": idle_seconds,
        "min_idle_seconds": DOCUMENT_CHANGE_STARTUP_MIN_IDLE_SECONDS,
        "raw_access": False,
    }


def _maybe_start_document_change_worker_when_eligible(
    db_path: str | Path,
    root_dir: str | Path,
) -> dict:
    with _DOCUMENT_CHANGE_WORKER_LOCK:
        already_running = bool(
            _DOCUMENT_CHANGE_WORKER_THREAD is not None
            and _DOCUMENT_CHANGE_WORKER_THREAD.is_alive()
        )
        retry_cooldown_active = bool(
            time.monotonic()
            < _DOCUMENT_CHANGE_WORKER_RETRY_NOT_BEFORE_MONOTONIC
        )
    if already_running:
        return {
            "allowed": True,
            "started": False,
            "running": True,
            "reason": "already_running",
            "raw_access": False,
        }
    if retry_cooldown_active:
        return {
            "allowed": False,
            "started": False,
            "running": False,
            "reason": "worker_retry_cooldown",
            "retry_cooldown_seconds": DOCUMENT_CHANGE_WORKER_RETRY_COOLDOWN_SECONDS,
            "raw_access": False,
        }
    if _daily_maintenance_blocks_document_worker(db_path):
        return {
            "allowed": False,
            "started": False,
            "running": False,
            "reason": "daily_maintenance_pending",
            "raw_access": False,
        }
    eligibility = _document_change_worker_startup_eligibility(root_dir)
    if not eligibility["allowed"]:
        return {**eligibility, "started": False, "running": False}
    started = _maybe_start_document_change_worker(db_path)
    return {
        **eligibility,
        "started": started,
        "running": started,
    }


def _maybe_start_document_change_worker(db_path: str | Path) -> bool:
    global _DOCUMENT_CHANGE_WORKER_THREAD
    with _DOCUMENT_CHANGE_WORKER_LOCK:
        if _DOCUMENT_CHANGE_WORKER_THREAD is not None and _DOCUMENT_CHANGE_WORKER_THREAD.is_alive():
            return False

        def run() -> None:
            global _DOCUMENT_CHANGE_WORKER_RETRY_NOT_BEFORE_MONOTONIC
            global _DOCUMENT_STREAM_RESET_PENDING_REASON
            global _DOCUMENT_STREAM_RESET_FAILED_RETRIES
            from .document_changes import (
                join_document_change_worker,
                start_document_change_worker,
            )

            root = Path(db_path).resolve().parent
            started_at = time.monotonic()
            session_provider = _PersistentSessionStateProvider(root)
            result = None
            pipeline_joined = False
            reset_retry_failed = False
            with _DOCUMENT_CHANGE_WORKER_LOCK:
                pending_reset_reason = _DOCUMENT_STREAM_RESET_PENDING_REASON
            try:
                if pending_reset_reason is None:
                    # A reset left unfinished by an earlier daemon process.
                    from .document_changes import document_stream_reset_required_reason

                    pending_reset_reason = document_stream_reset_required_reason(root)
                    if pending_reset_reason is not None:
                        with _DOCUMENT_CHANGE_WORKER_LOCK:
                            _DOCUMENT_STREAM_RESET_PENDING_REASON = pending_reset_reason
                if pending_reset_reason is not None:
                    # The previous generation stopped and fully joined but could
                    # not clear its stream IDs.  Re-run that terminal reset now;
                    # a replacement may only start once the re-read shows every
                    # enabled watermark is NULL, and then never with --since.
                    if not _retry_document_stream_reset(db_path, pending_reset_reason):
                        reset_retry_failed = True
                        raise RuntimeError("document_stream_reset_retry_failed")
                result = start_document_change_worker(
                    db_path,
                    helper_command(root, "fsevents_watch"),
                    session_state_provider=session_provider,
                    reconcile_eligibility_provider=lambda: (
                        _document_change_worker_startup_eligibility(
                            root,
                            session=session_provider(),
                        )
                    ),
                    _diagnostic_sink=_emit_document_change_transition_diagnostic,
                    require_fresh_stream=pending_reset_reason is not None,
                )
                if pending_reset_reason is not None:
                    if (
                        isinstance(result, dict)
                        and result.get("reason") == "stream_watermark_retained"
                    ):
                        from .document_changes import mark_document_stream_reset_required

                        reset_retry_failed = True
                        mark_document_stream_reset_required(root, pending_reset_reason)
                        raise RuntimeError("document_stream_reset_retry_failed")
                    with _DOCUMENT_CHANGE_WORKER_LOCK:
                        _DOCUMENT_STREAM_RESET_PENDING_REASON = None
                        _DOCUMENT_STREAM_RESET_FAILED_RETRIES = 0
                # The outer lifecycle remains active until the reader and every
                # downstream queue consumer have exited. Otherwise a retry can
                # overlap a stale validator/processor that still owns the same
                # database and runtime directory.
                join_document_change_worker(result)
                pipeline_joined = True
                result_process = (
                    result.get("process") if isinstance(result, dict) else None
                )
                if getattr(result_process, "restart_blocked", False) is True:
                    # Fixed code only: never log the SQLite exception text or a
                    # watched path.  The finally block sees the same process
                    # flag and prevents an automatic stale-watermark restart.
                    raise RuntimeError("document_stream_reset_failed")
            except Exception as exc:
                # Filesystem exceptions often embed the absolute watched path.
                # Runtime logs retain only a fixed failure code; coverage details
                # are represented by the content-free checkpoint state.
                print(_safe_daemon_failure_code("document_change_worker", exc), file=sys.stderr)
            finally:
                # The validator and processor can still perform content checks
                # until join_document_change_worker() returns. Keep their shared
                # session helper alive through that barrier, then retire it.
                try:
                    session_provider.close()
                except Exception:
                    # Never persist a helper or pipe exception: it may contain
                    # a watched path. The fixed code is sufficient for runtime
                    # diagnosis, and close failure cannot bypass restart gates.
                    print(
                        "document_change_session_provider_close_failed",
                        file=sys.stderr,
                        flush=True,
                    )
                finally:
                    process = result.get("process") if isinstance(result, dict) else None
                    restart_cooldown_required = (
                        getattr(process, "restart_cooldown_required", False) is True
                    )
                    restart_blocked = (
                        getattr(process, "restart_blocked", False) is True
                    )
                    # Only a short run looks like a failure to start.  A long one
                    # ended for a reason the owner caused, and making them wait out
                    # a retry cooldown for it is the behaviour that made tracking
                    # feel dead after every settings change. A stream gap is the
                    # exception: its process explicitly requests a fresh helper
                    # generation after the same bounded cooldown even when the old
                    # generation had otherwise been healthy for a long time.
                    elapsed = time.monotonic() - started_at
                    cooldown = (
                        DOCUMENT_CHANGE_WORKER_RETRY_COOLDOWN_SECONDS
                        if restart_cooldown_required
                        or elapsed < DOCUMENT_CHANGE_WORKER_HEALTHY_RUN_SECONDS
                        else 0.0
                    )
                    with _DOCUMENT_CHANGE_WORKER_LOCK:
                        if reset_retry_failed:
                            _DOCUMENT_STREAM_RESET_FAILED_RETRIES += 1
                            _DOCUMENT_CHANGE_WORKER_RETRY_NOT_BEFORE_MONOTONIC = (
                                time.monotonic()
                                + _document_stream_reset_retry_cooldown(
                                    _DOCUMENT_STREAM_RESET_FAILED_RETRIES
                                )
                            )
                        elif restart_blocked and not pipeline_joined:
                            # Old-generation threads may still own the database.
                            # Never retry their reset or start a replacement in
                            # this daemon process.
                            _DOCUMENT_CHANGE_WORKER_RETRY_NOT_BEFORE_MONOTONIC = float("inf")
                        elif restart_blocked:
                            # The old watermark is invalid and its reset did not
                            # commit.  Every pipeline thread has exited, so the
                            # next start first retries the same reset after the
                            # fresh-stream cooldown (see run() above).
                            reason = getattr(process, "stream_reset_reason", "")
                            _DOCUMENT_STREAM_RESET_PENDING_REASON = (
                                reason
                                if isinstance(reason, str) and reason
                                else "unknown_fatal_transition"
                            )
                            _DOCUMENT_STREAM_RESET_FAILED_RETRIES = 0
                            _DOCUMENT_CHANGE_WORKER_RETRY_NOT_BEFORE_MONOTONIC = max(
                                _DOCUMENT_CHANGE_WORKER_RETRY_NOT_BEFORE_MONOTONIC,
                                time.monotonic()
                                + DOCUMENT_CHANGE_WORKER_RETRY_COOLDOWN_SECONDS,
                            )
                        else:
                            _DOCUMENT_CHANGE_WORKER_RETRY_NOT_BEFORE_MONOTONIC = max(
                                _DOCUMENT_CHANGE_WORKER_RETRY_NOT_BEFORE_MONOTONIC,
                                time.monotonic() + cooldown,
                            )

        _DOCUMENT_CHANGE_WORKER_THREAD = threading.Thread(
            target=run,
            name="pam-document-change-worker",
            daemon=True,
        )
        _DOCUMENT_CHANGE_WORKER_THREAD.start()
        return True


def _document_stream_reset_retry_cooldown(failed_retries: int) -> float:
    exponent = max(0, min(int(failed_retries), 8))
    return float(
        min(
            DOCUMENT_CHANGE_WORKER_RETRY_COOLDOWN_SECONDS * (2 ** exponent),
            DOCUMENT_STREAM_RESET_RETRY_MAX_COOLDOWN_SECONDS,
        )
    )


def _retry_document_stream_reset(db_path: str | Path, reason: str) -> bool:
    """Retry a failed lost-stream reset; True only after verified NULL watermarks."""
    from .document_changes import reset_document_change_stream_watermarks

    try:
        verified = bool(
            reset_document_change_stream_watermarks(db_path, reason_code=reason)
        )
        outcome = "verified" if verified else "watermark_retained"
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        verified = False
        outcome = (
            "database_busy"
            if isinstance(exc, sqlite3.OperationalError)
            and any(token in str(exc).casefold() for token in ("locked", "busy"))
            else "failed"
        )
    _emit_document_change_transition_diagnostic(
        {
            "event": "document_change_stream_reset_retry",
            "outcome": outcome,
            "raw_access": False,
        }
    )
    return verified


def _background_maintenance_eligibility(
    root_dir: str | Path,
    *,
    session: dict | None = None,
    power: dict | None = None,
    idle: dict | None = None,
    privacy_deadline_override: bool = False,
) -> dict:
    """Allow heavy local work only on AC while locked or continuously idle."""
    if privacy_deadline_override:
        return {
            "allowed": True,
            "reason": "privacy_deadline_override",
            "idle_seconds": 0,
            "min_idle_seconds": BACKGROUND_MAINTENANCE_MIN_IDLE_SECONDS,
            "session_mode": "privacy_deadline_override",
            "deadline_override": True,
            "raw_access": False,
        }
    if session is None:
        try:
            session = session_state(root_dir)
        except Exception:
            session = {}
    state = str(session.get("state") or "").strip().lower() if isinstance(session, dict) else ""
    locked = bool(state == "locked" and session.get("locked") is True)
    unlocked = bool(
        state == "unlocked"
        and session.get("locked") is False
        and session.get("on_console") is True
        and session.get("capture_allowed") is True
    )
    if not locked and not unlocked:
        return {
            "allowed": False,
            "reason": "session_not_eligible",
            "idle_seconds": 0,
            "min_idle_seconds": BACKGROUND_MAINTENANCE_MIN_IDLE_SECONDS,
            "deadline_override": False,
            "raw_access": False,
        }
    if power is None:
        try:
            power = power_state()
        except Exception:
            power = {}
    if not isinstance(power, dict) or power.get("external_power") is not True:
        return {
            "allowed": False,
            "reason": "external_power_required",
            "idle_seconds": 0,
            "min_idle_seconds": BACKGROUND_MAINTENANCE_MIN_IDLE_SECONDS,
            "deadline_override": False,
            "raw_access": False,
        }
    if locked:
        return {
            "allowed": True,
            "reason": "",
            "idle_seconds": 0,
            "min_idle_seconds": BACKGROUND_MAINTENANCE_MIN_IDLE_SECONDS,
            "session_mode": "locked",
            "deadline_override": False,
            "raw_access": False,
        }
    if idle is None:
        try:
            idle = system_idle_seconds()
        except Exception:
            idle = {}
    try:
        idle_seconds = max(0, int(idle.get("idle_seconds") or 0)) if isinstance(idle, dict) else 0
    except (TypeError, ValueError):
        idle_seconds = 0
    idle_available = bool(isinstance(idle, dict) and idle.get("available") is True)
    allowed = bool(idle_available and idle_seconds >= BACKGROUND_MAINTENANCE_MIN_IDLE_SECONDS)
    return {
        "allowed": allowed,
        "reason": "" if allowed else ("idle_required" if idle_available else "idle_unavailable"),
        "idle_seconds": idle_seconds,
        "min_idle_seconds": BACKGROUND_MAINTENANCE_MIN_IDLE_SECONDS,
        "session_mode": "unlocked",
        "deadline_override": False,
        "raw_access": False,
    }


def _parse_daemon_state_timestamp(value: str) -> datetime | None:
    try:
        parsed = parse_time(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _mark_daily_maintenance_pending(conn, day: str) -> dict:
    completed_day = _get_state(conn, "retention_cleanup_last_completed_day", "")
    if completed_day == day:
        changed = _set_state_if_changed(conn, "retention_cleanup_pending_day", "")
        changed = _set_state_if_changed(conn, "retention_cleanup_pending_since", "") or changed
        changed = _set_state_if_changed(conn, "retention_cleanup_status", "ok") or changed
        if changed:
            conn.commit()
        return {
            "status": "not_due",
            "pending_day": "",
            "completed_day": completed_day,
            "raw_access": False,
        }
    current_pending_day = _get_state(conn, "retention_cleanup_pending_day", "")
    current_pending_since = _get_state(conn, "retention_cleanup_pending_since", "")
    current_status = _get_state(conn, "retention_cleanup_status", "")
    pending_since = current_pending_since
    if (
        _parse_daemon_state_timestamp(current_pending_since) is None
        or not current_pending_day
        or (current_pending_day != day and current_status == "running")
    ):
        pending_since = utc_now()
    changed = _set_state_if_changed(conn, "retention_cleanup_pending_day", day)
    changed = _set_state_if_changed(conn, "retention_cleanup_pending_since", pending_since) or changed
    changed = _set_state_if_changed(conn, "retention_cleanup_status", "waiting_for_eligibility") or changed
    if changed:
        conn.commit()
    return {
        "status": "waiting_for_eligibility",
        "pending_day": day,
        "pending_since": pending_since,
        "completed_day": completed_day,
        "raw_access": False,
    }


def _daily_maintenance_state_readonly(db_path: str | Path) -> dict:
    conn = None
    try:
        conn = connect_readonly(db_path)
        pending_day = _get_state(conn, "retention_cleanup_pending_day", "")
        pending_since = _get_state(conn, "retention_cleanup_pending_since", "")
        completed_day = _get_state(conn, "retention_cleanup_last_completed_day", "")
        status = _get_state(conn, "retention_cleanup_status", "")
        due = bool(pending_day and pending_day != completed_day)
        deferred_seconds = 0
        deadline_override = False
        pending_since_dt = _parse_daemon_state_timestamp(pending_since)
        now = _parse_daemon_state_timestamp(utc_now())
        if due and pending_since_dt is not None and now is not None:
            deferred_seconds = max(0, int((now - pending_since_dt).total_seconds()))
            deadline_override = (
                deferred_seconds >= RETENTION_MAINTENANCE_MAX_DEFERRAL_SECONDS
            )
        return {
            "available": True,
            "due": due,
            "pending_day": pending_day,
            "pending_since": pending_since,
            "status": status,
            "deferred_seconds": deferred_seconds,
            "deadline_override": deadline_override,
            "raw_access": False,
        }
    except sqlite3.Error:
        return {
            "available": False,
            "due": False,
            "pending_day": "",
            "pending_since": "",
            "status": "",
            "deferred_seconds": 0,
            "deadline_override": False,
            "reason": "daily_maintenance_state_unavailable",
            "raw_access": False,
        }
    finally:
        if conn is not None:
            conn.close()


def _daily_maintenance_due(db_path: str | Path) -> tuple[bool, str]:
    state = _daily_maintenance_state_readonly(db_path)
    return bool(state["due"]), str(state["pending_day"])


def _daily_maintenance_blocks_document_worker(db_path: str | Path) -> bool:
    """Keep initial document reconciliation behind the daily bulk writer.

    Only a writer that is running, or that is allowed to start right now, goes
    first.  Pending maintenance that is still waiting for AC power plus ten idle
    minutes (or a locked screen) must not hold file tracking hostage: on a Mac
    in continuous use that wait lasts until the 24-hour privacy deadline, and
    file tracking was off for the whole day.  Once tracking is running, the
    daily writer already waits for its baseline (see
    _document_baseline_reconcile_in_progress) unless the deadline overrides.
    """
    with _DAILY_MAINTENANCE_THREAD_LOCK:
        if _DAILY_MAINTENANCE_THREAD and _DAILY_MAINTENANCE_THREAD.is_alive():
            return True
    state = _daily_maintenance_state_readonly(db_path)
    if not state["available"]:
        return True
    if not state["due"]:
        return False
    with _DAILY_MAINTENANCE_THREAD_LOCK:
        retry_cooldown_active = bool(
            time.monotonic()
            < _DAILY_MAINTENANCE_RETRY_NOT_BEFORE_MONOTONIC
        )
    deadline_override = bool(state["deadline_override"])
    # Mirror _maybe_start_daily_maintenance_worker: a writer in its retry
    # cooldown cannot start this cycle, so it must not block tracking either.
    if retry_cooldown_active and (
        not deadline_override or str(state.get("status") or "") == "error"
    ):
        return False
    eligibility = _background_maintenance_eligibility(
        Path(db_path).resolve().parent,
        privacy_deadline_override=deadline_override,
    )
    return bool(eligibility.get("allowed"))


def _document_baseline_gate_readonly(db_path: str | Path) -> dict:
    """Expose only content-free readiness for downstream heavy workers."""
    conn = None
    try:
        from .document_changes import document_changes_status

        conn = connect_readonly(db_path)
        status = document_changes_status(conn)
        coverage = status.get("coverage") if isinstance(status, dict) else {}
        coverage = coverage if isinstance(coverage, dict) else {}
        tracking_enabled = coverage.get("enabled")
        if tracking_enabled is False:
            return {
                "allowed": True,
                "reason": "document_tracking_disabled",
                "raw_access": False,
            }
        if tracking_enabled is not True:
            return {
                "allowed": False,
                "reason": "document_baseline_unavailable",
                "raw_access": False,
            }
        try:
            root_count = max(0, int(coverage.get("root_count") or 0))
        except (TypeError, ValueError):
            root_count = 0
        if root_count == 0:
            return {
                "allowed": False,
                "reason": "document_roots_uninitialized",
                "raw_access": False,
            }
        complete = bool(
            coverage.get("is_complete") is True
            and str(coverage.get("coverage_status") or "") == "complete"
        )
        return {
            "allowed": complete,
            "reason": "" if complete else "document_baseline_incomplete",
            "raw_access": False,
        }
    except Exception:
        return {
            "allowed": False,
            "reason": "document_baseline_unavailable",
            "raw_access": False,
        }
    finally:
        if conn is not None:
            conn.close()


def _document_baseline_reconcile_in_progress(db_path: str | Path) -> dict:
    with _DOCUMENT_CHANGE_WORKER_LOCK:
        worker_running = bool(
            _DOCUMENT_CHANGE_WORKER_THREAD is not None
            and _DOCUMENT_CHANGE_WORKER_THREAD.is_alive()
        )
    if not worker_running:
        return {
            "blocked": False,
            "reason": "",
            "raw_access": False,
        }
    baseline = _document_baseline_gate_readonly(db_path)
    return {
        "blocked": not baseline["allowed"],
        "reason": (
            "document_baseline_reconciling"
            if not baseline["allowed"]
            else ""
        ),
        "raw_access": False,
    }


def _heavy_background_worker_running(*, exclude: str = "") -> bool:
    checks = []
    if exclude != "daily":
        with _DAILY_MAINTENANCE_THREAD_LOCK:
            checks.append(bool(_DAILY_MAINTENANCE_THREAD and _DAILY_MAINTENANCE_THREAD.is_alive()))
    if exclude != "ha":
        with _HA_AUTOMATION_THREAD_LOCK:
            checks.append(bool(_HA_AUTOMATION_THREAD and _HA_AUTOMATION_THREAD.is_alive()))
    if exclude != "quality":
        with _QUALITY_ADVICE_THREAD_LOCK:
            checks.append(bool(_QUALITY_ADVICE_THREAD and _QUALITY_ADVICE_THREAD.is_alive()))
    return any(checks)


def _maybe_start_daily_maintenance_worker(db_path: str | Path) -> dict:
    global _DAILY_MAINTENANCE_THREAD
    root = Path(db_path).resolve().parent
    with _DAILY_MAINTENANCE_THREAD_LOCK:
        if _DAILY_MAINTENANCE_THREAD and _DAILY_MAINTENANCE_THREAD.is_alive():
            return {"status": "daily_maintenance_already_running", "raw_access": False}
        retry_cooldown_active = bool(
            time.monotonic()
            < _DAILY_MAINTENANCE_RETRY_NOT_BEFORE_MONOTONIC
        )
    state = _daily_maintenance_state_readonly(db_path)
    if not state["available"]:
        return {
            "status": "daily_maintenance_waiting",
            "reason": "daily_maintenance_state_unavailable",
            "raw_access": False,
        }
    if not state["due"]:
        return {"status": "daily_maintenance_not_due", "raw_access": False}
    pending_day = str(state["pending_day"])
    deadline_override = bool(state["deadline_override"])
    # A stale scheduler cooldown must not extend a never-attempted privacy
    # deadline.  Once an actual deadline attempt reports an error, retain the
    # bounded retry cooldown to avoid a failing cleanup loop every 15 seconds.
    if retry_cooldown_active and (
        not deadline_override or str(state.get("status") or "") == "error"
    ):
        return {
            "status": "daily_maintenance_retry_cooldown",
            "retry_cooldown_seconds": BACKGROUND_WORKER_RETRY_COOLDOWN_SECONDS,
            "deadline_override": deadline_override,
            "raw_access": False,
        }
    baseline = _document_baseline_reconcile_in_progress(db_path)
    if baseline["blocked"] and not deadline_override:
        return {**baseline, "status": "daily_maintenance_waiting"}
    eligibility = _background_maintenance_eligibility(
        root,
        privacy_deadline_override=deadline_override,
    )
    if not eligibility["allowed"]:
        return {**eligibility, "status": "daily_maintenance_waiting"}
    # Retention is a privacy deadline.  Once the bounded deferral expires it
    # may outrank opportunistic baseline/HA/quality serialization.
    if not deadline_override and _heavy_background_worker_running(exclude="daily"):
        return {
            "status": "daily_maintenance_waiting",
            "reason": "heavy_worker_running",
            "raw_access": False,
        }
    with _DAILY_MAINTENANCE_THREAD_LOCK:
        if _DAILY_MAINTENANCE_THREAD and _DAILY_MAINTENANCE_THREAD.is_alive():
            return {"status": "daily_maintenance_already_running", "raw_access": False}
        thread = threading.Thread(
            target=_daily_maintenance_worker,
            args=(str(Path(db_path)), pending_day),
            name="pam-daily-maintenance",
            daemon=True,
        )
        _DAILY_MAINTENANCE_THREAD = thread
        thread.start()
    return {
        **eligibility,
        "status": "daily_maintenance_started",
        "pending_day": pending_day,
        "deadline_override": deadline_override,
    }


def _daily_maintenance_worker(db_path: str, pending_day: str) -> None:
    global _DAILY_MAINTENANCE_THREAD
    global _DAILY_MAINTENANCE_RETRY_NOT_BEFORE_MONOTONIC
    conn = None
    failed = False
    try:
        db = Path(db_path)
        root = db.resolve().parent
        conn = ensure_ready(db)
        ensure_daemon_defaults(conn)
        state = _daily_maintenance_state_readonly(db)
        if (
            not state["available"]
            or not state["due"]
            or str(state["pending_day"]) != pending_day
        ):
            return
        deadline_override = bool(state["deadline_override"])
        baseline = _document_baseline_reconcile_in_progress(db)
        if baseline["blocked"] and not deadline_override:
            _set_state(
                conn,
                "retention_cleanup_status",
                "waiting_for_document_baseline",
            )
            _set_state(conn, "retention_cleanup_last_error", "")
            conn.commit()
            return
        eligibility = _background_maintenance_eligibility(
            root,
            privacy_deadline_override=deadline_override,
        )
        if not eligibility["allowed"]:
            _set_state(conn, "retention_cleanup_status", "waiting_for_eligibility")
            _set_state(conn, "retention_cleanup_last_error", "")
            conn.commit()
            return
        _set_state(conn, "retention_cleanup_status", "running")
        _set_state(conn, "retention_cleanup_last_error", "")
        conn.commit()
        retention = _safe_cleanup_retention_for_daemon(conn, root)
        failed = str(retention.get("status") or "") in {"error", "partial"}
        if failed:
            _set_state(conn, "retention_cleanup_status", "error")
            _set_state(
                conn,
                "retention_cleanup_last_error",
                str(retention.get("error") or "private_purge_pending"),
            )
            conn.commit()
            return
        # A maintenance pass can cross local midnight.  Take a short writer
        # transaction before completing the marker so a newly queued day is
        # either observed here or written after this commit, never erased.
        conn.execute("BEGIN IMMEDIATE")
        current_pending_day = _get_state(
            conn,
            "retention_cleanup_pending_day",
            "",
        )
        _set_state(conn, "retention_cleanup_last_completed_day", pending_day)
        if current_pending_day == pending_day:
            _set_state(conn, "retention_cleanup_pending_day", "")
            _set_state(conn, "retention_cleanup_pending_since", "")
            _set_state(conn, "retention_cleanup_status", "ok")
        else:
            _set_state(
                conn,
                "retention_cleanup_status",
                "waiting_for_eligibility",
            )
        _set_state(conn, "retention_cleanup_last_error", "")
        conn.commit()
        _safe_hygiene_cleanup_for_daemon(conn, root, db)
    except Exception as exc:
        failed = True
        print(_safe_daemon_failure_code("daily_maintenance", exc), file=sys.stderr)
        if conn is not None:
            try:
                conn.rollback()
                _set_state(conn, "retention_cleanup_status", "error")
                _set_state(conn, "retention_cleanup_last_error", "daily_maintenance_failed")
                conn.commit()
            except Exception:
                pass
    finally:
        if failed:
            _DAILY_MAINTENANCE_RETRY_NOT_BEFORE_MONOTONIC = max(
                _DAILY_MAINTENANCE_RETRY_NOT_BEFORE_MONOTONIC,
                time.monotonic() + BACKGROUND_WORKER_RETRY_COOLDOWN_SECONDS,
            )
        if conn is not None:
            conn.close()
        with _DAILY_MAINTENANCE_THREAD_LOCK:
            if _DAILY_MAINTENANCE_THREAD is threading.current_thread():
                _DAILY_MAINTENANCE_THREAD = None


def _safe_cleanup_retention_for_daemon(conn, root_dir: str | Path, days: int | None = None) -> dict:
    try:
        result = cleanup_retention(conn, root_dir, days)
        conn.commit()
        return result
    except Exception as exc:
        message = _safe_daemon_failure_code("retention_cleanup", exc)
        print(message, file=sys.stderr)
        _set_state(conn, "last_error", message)
        _set_state(conn, "daemon_status", "running")
        conn.commit()
        return {"status": "error", "error": message}


def _safe_hygiene_cleanup_for_daemon(conn, root_dir: str | Path, db_path: str | Path | None = None) -> dict:
    ensure_daemon_defaults(conn)
    if not _hygiene_auto_cleanup_enabled(conn):
        _set_state(conn, "hygiene_cleanup_status", "disabled")
        _set_state(conn, "hygiene_cleanup_last_error", "")
        conn.commit()
        return {"status": "disabled", "applied": False}
    root = Path(root_dir)
    database_path = Path(db_path).resolve() if db_path is not None else Path(_database_path(conn)).resolve()
    core_root = root.parent if root.name == "data" else root
    try:
        result = hygiene_cleanup_report(
            core_root,
            database_path,
            apply=True,
            confirm=HYGIENE_CLEANUP_CONFIRMATION,
            keep_backups=_hygiene_int_setting(conn, "hygiene_keep_backups", DEFAULT_KEEP_BACKUPS),
            min_age_days=_hygiene_int_setting(conn, "hygiene_min_age_days", DEFAULT_MIN_AGE_DAYS),
            quarantine_days=_hygiene_int_setting(conn, "hygiene_quarantine_days", DEFAULT_QUARANTINE_DAYS),
            include_ha_cache=True,
            include_backup_rotation=_hygiene_backup_rotation_enabled(conn),
            include_activity_export_cache=False,
        )
        _set_state(conn, "hygiene_cleanup_status", "ok")
        _set_state(conn, "hygiene_cleanup_last_run_at", utc_now())
        _set_state(conn, "hygiene_cleanup_last_error", "")
        _set_state(conn, "hygiene_cleanup_last_report", json.dumps(_compact_hygiene_report(result), ensure_ascii=False))
        conn.commit()
        return result
    except Exception as exc:
        message = _safe_daemon_failure_code("hygiene_cleanup", exc)
        print(message, file=sys.stderr)
        _set_state(conn, "hygiene_cleanup_status", "error")
        _set_state(conn, "hygiene_cleanup_last_run_at", utc_now())
        _set_state(conn, "hygiene_cleanup_last_error", message)
        _set_state(conn, "daemon_status", "running")
        conn.commit()
        return {"status": "error", "error": message, "applied": False}


def _setting_enabled_readonly(db_path: str | Path, key: str, default: bool) -> bool:
    conn = None
    try:
        conn = connect_readonly(db_path)
        return _bool_setting(conn, key, default)
    except sqlite3.Error:
        return default
    finally:
        if conn is not None:
            conn.close()


def _retention_handoff_admission_readonly(db_path: str | Path) -> dict:
    conn = None
    try:
        conn = connect_readonly(db_path)
        now = _parse_daemon_state_timestamp(utc_now())
        return retention_handoff_admission(conn, now=now)
    except sqlite3.Error:
        return {
            "allowed": False,
            "reason": "retention_deadline_unavailable",
            "pending": True,
            "deadline_at": "",
            "drain_starts_at": "",
            "remaining_seconds": 0,
            "raw_access": False,
        }
    finally:
        if conn is not None:
            conn.close()


def _maybe_start_ha_automation_worker(db_path: str | Path) -> dict:
    global _HA_AUTOMATION_THREAD
    db = Path(db_path)
    root = db.resolve().parent
    if not _setting_enabled_readonly(db, "ha_auto_package_enabled", False):
        return {"status": "ha_automation_disabled"}
    admission = _retention_handoff_admission_readonly(db)
    if not admission["allowed"]:
        return {**admission, "status": "ha_automation_waiting"}
    baseline = _document_baseline_gate_readonly(db)
    if not baseline["allowed"]:
        return {**baseline, "status": "ha_automation_waiting"}
    eligibility = _background_maintenance_eligibility(root)
    if not eligibility["allowed"]:
        return {**eligibility, "status": "ha_automation_waiting"}
    if _heavy_background_worker_running(exclude="ha"):
        return {"status": "ha_automation_waiting", "reason": "heavy_worker_running", "raw_access": False}
    with _HA_AUTOMATION_THREAD_LOCK:
        if _HA_AUTOMATION_THREAD and _HA_AUTOMATION_THREAD.is_alive():
            return {"status": "ha_automation_already_running"}
        if time.monotonic() < _HA_AUTOMATION_RETRY_NOT_BEFORE_MONOTONIC:
            return {
                "status": "ha_automation_retry_cooldown",
                "retry_cooldown_seconds": BACKGROUND_WORKER_RETRY_COOLDOWN_SECONDS,
                "raw_access": False,
            }

        conn = ensure_ready(db)
        try:
            ensure_daemon_defaults(conn)
            status = ha_automation_status(conn, root)
            if not status["enabled"]:
                return {"status": "ha_automation_disabled"}
            if not status["runtime_allowed"]:
                _set_state(conn, "ha_auto_status", "skipped")
                _set_state(conn, "ha_auto_last_error", "")
                conn.commit()
                return {"status": "ha_automation_skipped", "reason": "repo_local_not_live_runtime"}
            if not status["due"]:
                return {"status": "ha_automation_skipped", "reason": "not_due"}
            _set_state(conn, "ha_auto_status", "starting")
            conn.commit()
        finally:
            conn.close()

        thread = threading.Thread(
            target=_ha_automation_worker,
            args=(str(db),),
            name="ha-hourly-automation",
            daemon=True,
        )
        _HA_AUTOMATION_THREAD = thread
        thread.start()
        return {"status": "ha_automation_started"}


def _ha_automation_worker(db_path: str) -> None:
    global _HA_AUTOMATION_THREAD
    global _HA_AUTOMATION_RETRY_NOT_BEFORE_MONOTONIC
    db = Path(db_path)
    root = db.resolve().parent
    conn = None
    failed = False
    try:
        conn = ensure_ready(db)
        ensure_daemon_defaults(conn)
        admission = retention_handoff_admission(conn)
        if not admission["allowed"]:
            _set_state(conn, "ha_auto_status", "waiting_for_retention")
            _set_state(conn, "ha_auto_last_error", "")
            conn.commit()
            return
        baseline = _document_baseline_gate_readonly(db)
        if not baseline["allowed"]:
            _set_state(conn, "ha_auto_status", "waiting_for_document_baseline")
            _set_state(conn, "ha_auto_last_error", "")
            conn.commit()
            return
        eligibility = _background_maintenance_eligibility(root)
        if not eligibility["allowed"]:
            _set_state(conn, "ha_auto_status", "waiting_for_eligibility")
            _set_state(conn, "ha_auto_last_error", "")
            conn.commit()
            return
        result = run_ha_automation_once(conn, root, db_path=db)
        failed = str(result.get("status") or "") in {"error", "failed", "dead_letter"}
    except Exception as exc:
        failed = True
        message = _safe_daemon_failure_code("ha_automation", exc)
        print(message, file=sys.stderr)
        if conn is not None:
            try:
                _set_state(conn, "ha_auto_status", "error")
                _set_state(conn, "ha_auto_last_error", message)
                _set_state(conn, "daemon_status", "running")
                conn.commit()
            except Exception:
                pass
    finally:
        if failed:
            _HA_AUTOMATION_RETRY_NOT_BEFORE_MONOTONIC = max(
                _HA_AUTOMATION_RETRY_NOT_BEFORE_MONOTONIC,
                time.monotonic() + BACKGROUND_WORKER_RETRY_COOLDOWN_SECONDS,
            )
        if conn is not None:
            conn.close()
        with _HA_AUTOMATION_THREAD_LOCK:
            if _HA_AUTOMATION_THREAD is threading.current_thread():
                _HA_AUTOMATION_THREAD = None


def _maybe_start_quality_advice_worker(db_path: str | Path) -> dict:
    global _QUALITY_ADVICE_THREAD
    db = Path(db_path)
    root = db.resolve().parent
    if not _setting_enabled_readonly(db, "quality_advice_enabled", True):
        return {"status": "quality_advice_disabled"}
    admission = _retention_handoff_admission_readonly(db)
    if not admission["allowed"]:
        return {**admission, "status": "quality_advice_waiting"}
    baseline = _document_baseline_gate_readonly(db)
    if not baseline["allowed"]:
        return {**baseline, "status": "quality_advice_waiting"}
    eligibility = _background_maintenance_eligibility(root)
    if not eligibility["allowed"]:
        return {**eligibility, "status": "quality_advice_waiting"}
    if _heavy_background_worker_running(exclude="quality"):
        return {"status": "quality_advice_waiting", "reason": "heavy_worker_running", "raw_access": False}
    with _QUALITY_ADVICE_THREAD_LOCK:
        if _QUALITY_ADVICE_THREAD and _QUALITY_ADVICE_THREAD.is_alive():
            return {"status": "quality_advice_already_running"}
        if time.monotonic() < _QUALITY_ADVICE_RETRY_NOT_BEFORE_MONOTONIC:
            return {
                "status": "quality_advice_retry_cooldown",
                "retry_cooldown_seconds": BACKGROUND_WORKER_RETRY_COOLDOWN_SECONDS,
                "raw_access": False,
            }

        conn = ensure_ready(db)
        try:
            ensure_daemon_defaults(conn)
            status = quality_advice_status(conn, root)
            if not status["enabled"]:
                return {"status": "quality_advice_disabled"}
            if not status["runtime_allowed"]:
                _set_state(conn, "quality_advice_status", "skipped")
                _set_state(conn, "quality_advice_last_error", "")
                conn.commit()
                return {"status": "quality_advice_skipped", "reason": "repo_local_not_live_runtime"}
            if not status["due"]:
                return {"status": "quality_advice_skipped", "reason": "not_due"}
            if not status["preflight"]["ready"]:
                _set_state(conn, "quality_advice_status", "error")
                _set_state(conn, "quality_advice_last_error", status["preflight"]["summary"])
                conn.commit()
                return {"status": "quality_advice_skipped", "reason": "preflight_not_ready"}
            _set_state(conn, "quality_advice_status", "starting")
            conn.commit()
        finally:
            conn.close()

        thread = threading.Thread(
            target=_quality_advice_worker,
            args=(str(db),),
            name="quality-advice-daily",
            daemon=True,
        )
        _QUALITY_ADVICE_THREAD = thread
        thread.start()
        return {"status": "quality_advice_started"}


def _quality_advice_worker(db_path: str) -> None:
    global _QUALITY_ADVICE_THREAD
    global _QUALITY_ADVICE_RETRY_NOT_BEFORE_MONOTONIC
    db = Path(db_path)
    root = db.resolve().parent
    conn = None
    failed = False
    try:
        conn = ensure_ready(db)
        ensure_daemon_defaults(conn)
        admission = retention_handoff_admission(conn)
        if not admission["allowed"]:
            _set_state(conn, "quality_advice_status", "waiting_for_retention")
            _set_state(conn, "quality_advice_last_error", "")
            conn.commit()
            return
        baseline = _document_baseline_gate_readonly(db)
        if not baseline["allowed"]:
            _set_state(
                conn,
                "quality_advice_status",
                "waiting_for_document_baseline",
            )
            _set_state(conn, "quality_advice_last_error", "")
            conn.commit()
            return
        eligibility = _background_maintenance_eligibility(root)
        if not eligibility["allowed"]:
            _set_state(conn, "quality_advice_status", "waiting_for_eligibility")
            _set_state(conn, "quality_advice_last_error", "")
            conn.commit()
            return
        result = run_quality_advice_once(conn, root, db_path=db)
        failed = str(result.get("status") or "") in {"error", "failed"}
    except Exception as exc:
        failed = True
        message = _safe_daemon_failure_code("quality_advice", exc)
        print(message, file=sys.stderr)
        if conn is not None:
            try:
                _set_state(conn, "quality_advice_status", "error")
                _set_state(conn, "quality_advice_last_error", message)
                _set_state(conn, "daemon_status", "running")
                conn.commit()
            except Exception:
                pass
    finally:
        if failed:
            _QUALITY_ADVICE_RETRY_NOT_BEFORE_MONOTONIC = max(
                _QUALITY_ADVICE_RETRY_NOT_BEFORE_MONOTONIC,
                time.monotonic() + BACKGROUND_WORKER_RETRY_COOLDOWN_SECONDS,
            )
        if conn is not None:
            conn.close()
        with _QUALITY_ADVICE_THREAD_LOCK:
            if _QUALITY_ADVICE_THREAD is threading.current_thread():
                _QUALITY_ADVICE_THREAD = None


def configure_hygiene_cleanup(
    conn,
    root_dir: str | Path,
    *,
    auto_cleanup: bool | None = None,
    backup_rotation: bool | None = None,
    keep_backups: int | None = None,
    min_age_days: int | None = None,
    quarantine_days: int | None = None,
) -> dict:
    ensure_daemon_defaults(conn)
    if auto_cleanup is not None:
        _set_setting(conn, "hygiene_auto_cleanup_enabled", "true" if auto_cleanup else "false")
        _set_state(conn, "hygiene_cleanup_status", "ready" if auto_cleanup else "disabled")
    if backup_rotation is not None:
        _set_setting(conn, "hygiene_backup_rotation_enabled", "true" if backup_rotation else "false")
    if keep_backups is not None:
        keep_backups = int(keep_backups)
        if keep_backups < 1:
            raise ValueError("hygiene keep_backups must be at least 1")
        _set_setting(conn, "hygiene_keep_backups", str(keep_backups))
    if min_age_days is not None:
        min_age_days = int(min_age_days)
        if min_age_days < 0:
            raise ValueError("hygiene min_age_days must be at least 0")
        _set_setting(conn, "hygiene_min_age_days", str(min_age_days))
    if quarantine_days is not None:
        quarantine_days = int(quarantine_days)
        if quarantine_days < 1:
            raise ValueError("hygiene quarantine_days must be at least 1")
        _set_setting(conn, "hygiene_quarantine_days", str(quarantine_days))
    conn.commit()
    return _hygiene_cleanup_snapshot(conn, root_dir)


def cleanup_retention_preview(conn, root_dir: str | Path, days: int | None = None) -> dict:
    ensure_daemon_defaults(conn)
    if days is not None and days <= 0:
        raise ValueError("cleanup-retention --days must be at least 1")
    cutoff = rolling_retention_cutoff(days) if days is not None else retention_cleanup_cutoff()
    start = "0001-01-01T00:00:00+00:00"
    end = cutoff.isoformat(timespec="seconds")
    delete_counts = _retention_delete_counts(conn, start, end)
    mask_counts = _retention_mask_counts(conn, start, end)
    from .context_console import preview_context_retention

    context_retention = preview_context_retention(conn)
    return {
        "mode": "dry_run",
        "retention_policy": DEFAULT_RETENTION_POLICY if days is None else f"rolling_{days}_days",
        "retention_cutoff": end,
        "retention_range": {"from": start, "to": end},
        "placeholder_gap_filtered": True,
        "would_delete": delete_counts,
        "would_mask": {"segments": mask_counts["would_mask_segments"]},
        "already_masked": {"segments": mask_counts["already_masked_segments"]},
        "raw_residue_count": count_raw_residue(root_dir),
        "retention_held": {
            "activity_count": int(delete_counts.get("retention_held_activities") or 0),
            "confirmation_count": int(delete_counts.get("retention_held_confirmations") or 0),
            "evidence_count": int(delete_counts.get("retention_held_evidence") or 0),
        },
        "context_retention": context_retention,
        "message": "Dry run only. Add --apply and --confirm-retention-delete DELETE_EXPIRED_LOCAL_ACTIVITY to delete expired local data; overlapping segments are masked, not deleted.",
    }


def cleanup_retention(conn, root_dir: str | Path, days: int | None = None) -> dict:
    ensure_daemon_defaults(conn)
    if days is not None and days <= 0:
        raise ValueError("cleanup-retention --days must be at least 1")
    retry_before = {
        "ha": retry_pending_package_cache_purges(conn),
    }
    cutoff = rolling_retention_cutoff(days) if days is not None else retention_cleanup_cutoff()
    deletion = delete_range(
        conn,
        "0001-01-01T00:00:00+00:00",
        cutoff.isoformat(timespec="seconds"),
        reason="retention_expired",
        source_status="expired_deleted",
    )
    expired = expire_packages(conn, return_details=True)
    expired_vlm = cleanup_retired_review_files(conn, root_dir)
    raw_removed = cleanup_raw_vault(root_dir)
    # Context policies are intentionally independent of the activity cutoff:
    # unverified observations keep today+yesterday, VerifiedContext 90 days,
    # and content-free product wins 30 days.
    if conn.in_transaction:
        conn.commit()
    from .context_console import cleanup_context_retention

    context_retention = cleanup_context_retention(conn)
    purge_status = _sync_private_purge_daemon_state(
        conn,
        failed=bool(
            retry_before["ha"].get("failed_refs")
            or expired.get("cache_purge_failed")
            or expired_vlm.get("file_purge_failed")
            or deletion.get("status") == "partial"
            or context_retention.get("status") == "failed"
        ),
    )
    result = {
        "status": "partial" if purge_status["pending_count"] else "completed",
        "mode": "applied",
        "retention_policy": DEFAULT_RETENTION_POLICY if days is None else f"rolling_{days}_days",
        "retention_cutoff": cutoff.isoformat(timespec="seconds"),
        "deleted": deletion,
        "expired_packages": expired,
        "legacy_private_cleanup": expired_vlm,
        "raw_removed": raw_removed,
        "context_retention": context_retention,
        "private_purge_retry": retry_before,
        "private_purge": purge_status,
    }
    if purge_status["pending_count"]:
        result["error"] = "private_purge_pending"
    # Settle the retention work before trimming the operational tables.  Those
    # deletes commit in batches, so sharing a transaction boundary with the
    # cascade above would either fold them into its rollback or force this pass
    # to skip entirely.  They are independent concerns: retention governs
    # activity content under the authorization lease, this is content-free
    # housekeeping that must stay outside it.
    if conn.in_transaction:
        conn.commit()
    result["operational_records"] = cleanup_operational_records(conn)
    canonical_data_root = (
        Path.home()
        / "Library/Application Support/Personal AI Monitor/core/data"
    ).resolve(strict=False)
    if Path(root_dir).expanduser().resolve(strict=False) == canonical_data_root:
        receipt_cleanup = cleanup_ai_attribution_receipts(
            Path.home() / ".pam-ai-attribution"
        )
    else:
        receipt_cleanup = {
            "status": "skipped",
            "reason": "noncanonical_runtime",
            "retention_days": 90,
            "raw_access": False,
        }
    result["ai_attribution_receipts"] = receipt_cleanup
    if receipt_cleanup.get("status") == "blocked":
        result["status"] = "partial"
        result["error"] = "ai_attribution_retention_pending"
    result["summary"] = summarize_retention_cleanup(result)
    return result


def summarize_retention_cleanup(result: dict) -> dict:
    deleted = result.get("deleted") or {}
    cascade = deleted.get("cascade") if isinstance(deleted, dict) else {}
    cascade = cascade if isinstance(cascade, dict) else {}
    return {
        "status": result.get("status") or "completed",
        "mode": result.get("mode") or "applied",
        "retention_policy": result.get("retention_policy") or "",
        "retention_cutoff": result.get("retention_cutoff") or "",
        "activities_deleted": int(deleted.get("activities_deleted") or deleted.get("deleted_activity_count") or 0) if isinstance(deleted, dict) else 0,
        "privacy_events_deleted": int(cascade.get("privacy_events") or 0),
        "gap_events_deleted": int(cascade.get("gap_events") or 0),
        "duplicate_events_deleted": int(cascade.get("duplicate_events") or 0),
        "coverage_ledger_deleted": int(cascade.get("coverage_ledger") or 0),
        "vlm_pending_jobs_deleted": int(cascade.get("vlm_pending_jobs") or 0),
        "ha_source_records_expired": int(cascade.get("ha_source_records") or 0),
        "ha_package_cache_files_deleted": int(cascade.get("ha_package_cache_files") or 0),
        "raw_removed": int(result.get("raw_removed") or 0),
        "expired_package_count": int(
            (result.get("expired_packages") or {}).get("expired") or 0
        ),
        "private_purge_pending": int(
            (result.get("private_purge") or {}).get("pending_count") or 0
        ),
        "retention_held_activities": int(cascade.get("retention_held_activities") or 0),
        "retention_held_confirmations": int(cascade.get("retention_held_confirmations") or 0),
        "retention_held_evidence": int(cascade.get("retention_held_evidence") or 0),
        "context_unverified_observations_expired": int(
            (result.get("context_retention") or {}).get("unverified_observations_expired") or 0
        ),
        "context_verified_contexts_expired": int(
            (result.get("context_retention") or {}).get("verified_contexts_expired") or 0
        ),
        "context_product_wins_deleted": int(
            (result.get("context_retention") or {}).get("product_wins_deleted") or 0
        ),
    }


def _sync_private_purge_daemon_state(
    conn,
    *,
    failed: bool = False,
) -> dict:
    status = private_purge_status(conn)
    pending = int(status["pending_count"])
    _set_state(conn, "private_purge_status", "pending" if pending else "ok")
    _set_state(conn, "private_purge_pending_count", str(pending))
    _set_state(
        conn,
        "private_purge_last_error",
        "private_purge_pending" if pending and failed else "",
    )
    conn.commit()
    return status


def _retention_delete_counts(conn, start: str, end: str) -> dict:
    preview = preview_delete_range(conn, start, end, source_status="expired_deleted")
    counts = dict(preview.get("cascade") or {})
    # Compatibility alias for existing retention UI/CLI clients.
    counts["expired_ha_source_records"] = int(counts.get("ha_source_records") or 0)
    counts.pop("segments_touched", None)
    counts.pop("system_activity_sessions_touched", None)
    return counts


def _retention_mask_counts(conn, start: str, end: str) -> dict:
    rows = conn.execute(
        """
        SELECT summary
        FROM segments
        WHERE datetime(started_at) < datetime(?) AND datetime(ended_at) > datetime(?)
        """,
        (end, start),
    ).fetchall()
    already = sum(1 for row in rows if str(row["summary"] or "") == "deleted_or_partially_deleted")
    return {
        "would_mask_segments": max(0, len(rows) - already),
        "already_masked_segments": already,
    }


def cleanup_raw_vault(root_dir: str | Path) -> int:
    vault = _raw_vault(Path(root_dir))
    removed = 0
    failed = False
    for child in vault.iterdir():
        if child.name == ".metadata_never_index":
            continue
        try:
            metadata = child.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                child.unlink()
            elif child.name == "vlm_pending":
                _tighten_owned_private_directory(child)
                continue
            elif stat.S_ISDIR(metadata.st_mode):
                _remove_raw_vault_tree(child)
            else:
                if metadata.st_uid != os.getuid():
                    raise RawVaultSafetyError("raw_vault_cleanup_failed")
                child.unlink()
            removed += 1
        except OSError:
            failed = True
    if failed:
        raise RawVaultSafetyError("raw_vault_cleanup_failed")
    return removed


def cleanup_legacy_server_pid(root_dir: str | Path) -> bool:
    pid_path = Path(root_dir) / "server.pid"
    try:
        pid_path.unlink()
        return True
    except FileNotFoundError:
        return False


def count_raw_residue(root_dir: str | Path) -> int:
    try:
        vault = _raw_vault(Path(root_dir))
        residue = 0
        for child in vault.iterdir():
            if child.name == ".metadata_never_index":
                continue
            if child.name == "vlm_pending":
                try:
                    metadata = child.lstat()
                except OSError:
                    residue += 1
                    continue
                if (
                    stat.S_ISLNK(metadata.st_mode)
                    or not stat.S_ISDIR(metadata.st_mode)
                    or metadata.st_uid != os.getuid()
                    or stat.S_IMODE(metadata.st_mode) != 0o700
                ):
                    residue += 1
                continue
            residue += 1
        return residue
    except OSError:
        # Status remains readable, but never reports an unsafe vault as empty.
        return 1


class _PersistentSessionStateProvider:
    """Serialize fresh session probes through one bounded helper process."""

    def __init__(
        self,
        root_dir: str | Path,
        *,
        timeout_seconds: float = SESSION_STATE_HELPER_TIMEOUT_SECONDS,
        popen_factory=subprocess.Popen,
    ) -> None:
        self._root = Path(root_dir)
        self._timeout_seconds = max(0.01, float(timeout_seconds))
        self._popen_factory = popen_factory
        self._lock = threading.Lock()
        self._process = None
        self._closed = False

    def __call__(self) -> dict:
        with self._lock:
            if self._closed:
                return _unknown_session_state("session_state_provider_closed")
            process = self._process
            if process is None:
                process = self._spawn()
                if process is None:
                    return _unknown_session_state("session_state_unavailable")
            try:
                if process.poll() is not None:
                    self._retire(process, force=True)
                    return _unknown_session_state("session_state_failed")
            except (OSError, ValueError):
                self._retire(process, force=True)
                return _unknown_session_state("session_state_failed")

            nonce = secrets.token_hex(16)
            request = json.dumps(
                {"type": "probe", "nonce": nonce},
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii") + b"\n"
            try:
                process.stdin.write(request)
                process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                self._retire(process, force=True)
                return _unknown_session_state("session_state_unavailable")

            status, raw_line = self._read_response(process)
            if status != "ok":
                self._retire(process, force=True)
                return _unknown_session_state(
                    {
                        "timeout": "session_state_timeout",
                        "eof": "session_state_eof",
                        "oversized": "session_state_invalid_payload",
                    }.get(status, "session_state_unavailable")
                )
            try:
                if process.poll() is not None:
                    self._retire(process, force=True)
                    return _unknown_session_state("session_state_failed")
                response = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._retire(process, force=True)
                return _unknown_session_state("session_state_json_failed")
            except (OSError, ValueError):
                self._retire(process, force=True)
                return _unknown_session_state("session_state_failed")

            if (
                not isinstance(response, dict)
                or set(response) != {"nonce", "session"}
                or response.get("nonce") != nonce
                or not isinstance(response.get("session"), dict)
            ):
                self._retire(process, force=True)
                return _unknown_session_state("session_state_invalid_payload")

            normalized = _session_state_from_payload(response["session"])
            if normalized.get("error") in {
                "session_state_conflicting_fields",
                "session_state_missing_fields",
                "session_state_invalid_state",
                "session_state_invalid_payload",
            }:
                self._retire(process, force=True)
            return normalized

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            process = self._process
            if process is not None:
                self._retire(process, force=False)

    def _spawn(self):
        try:
            process = self._popen_factory(
                [*helper_command(self._root, "session_state"), "--serve"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=False,
                bufsize=0,
            )
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError):
            return None
        self._process = process
        if process.stdin is None or process.stdout is None:
            self._retire(process, force=True)
            return None
        return process

    def _read_response(self, process) -> tuple[str, bytes]:
        stdout = process.stdout
        if stdout is None:
            return "unavailable", b""
        try:
            descriptor = stdout.fileno()
        except (AttributeError, OSError, ValueError):
            return "unavailable", b""
        deadline = time.monotonic() + self._timeout_seconds
        buffered = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timeout", b""
            try:
                readable, _, _ = select.select([descriptor], [], [], remaining)
            except InterruptedError:
                continue
            except (OSError, ValueError):
                return "unavailable", b""
            if not readable:
                return "timeout", b""
            try:
                chunk = os.read(descriptor, 4096)
            except InterruptedError:
                continue
            except (BlockingIOError, OSError, ValueError):
                return "unavailable", b""
            if not chunk:
                return "eof", b""
            buffered.extend(chunk)
            newline = buffered.find(b"\n")
            if newline >= 0:
                if newline > SESSION_STATE_HELPER_RESPONSE_MAX_BYTES:
                    return "oversized", b""
                if buffered[newline + 1:]:
                    return "unavailable", b""
                return "ok", bytes(buffered[:newline])
            if len(buffered) > SESSION_STATE_HELPER_RESPONSE_MAX_BYTES:
                return "oversized", b""

    def _retire(self, process, *, force: bool) -> None:
        if self._process is process:
            self._process = None
        stdin = getattr(process, "stdin", None)
        if not force and stdin is not None:
            try:
                stdin.close()
            except OSError:
                pass
        try:
            running = process.poll() is None
        except (OSError, ValueError):
            running = True
        if force and running:
            try:
                process.terminate()
            except (OSError, ProcessLookupError):
                pass
        if running:
            try:
                process.wait(timeout=SESSION_STATE_HELPER_STOP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except (OSError, ProcessLookupError):
                    pass
                try:
                    process.wait(timeout=SESSION_STATE_HELPER_STOP_TIMEOUT_SECONDS)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            except OSError:
                pass
        for stream_name in ("stdin", "stdout", "stderr"):
            stream = getattr(process, stream_name, None)
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


def session_state(root_dir: str | Path | None = None) -> dict:
    root = Path(root_dir) if root_dir is not None else Path(__file__).resolve().parents[1] / "data"
    try:
        completed = subprocess.run(
            helper_command(root, "session_state"),
            check=False,
            timeout=SESSION_STATE_HELPER_TIMEOUT_SECONDS,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.TimeoutExpired:
        return _unknown_session_state("session_state_timeout")
    except OSError:
        return _unknown_session_state("session_state_unavailable")
    if completed.returncode != 0:
        return _unknown_session_state("session_state_failed")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return _unknown_session_state("session_state_json_failed")
    return _session_state_from_payload(payload)


def _session_state_from_payload(payload: object) -> dict:
    if not isinstance(payload, dict):
        return _unknown_session_state("session_state_invalid_payload")

    locked_value = payload.get("locked")
    on_console = payload.get("on_console")
    declared_state = str(payload.get("state") or "").strip().lower()
    evidence_source = str(payload.get("evidence_source") or "session_state_helper").strip().lower()
    allowed_sources = {
        "session_state_helper",
        "session_state_helper_cgs",
        "session_state_helper_io_registry",
    }
    if evidence_source not in allowed_sources:
        evidence_source = "session_state_helper"
    if not isinstance(locked_value, bool) or not isinstance(on_console, bool):
        return _unknown_session_state("session_state_missing_fields", on_console=on_console, source=evidence_source)
    if declared_state == "unknown":
        return _unknown_session_state("session_state_unknown", on_console=on_console, source=evidence_source)
    if declared_state and declared_state not in {"unlocked", "locked"}:
        return _unknown_session_state("session_state_invalid_state", on_console=on_console, source=evidence_source)
    if locked_value:
        if declared_state == "unlocked":
            return _unknown_session_state(
                "session_state_conflicting_fields",
                on_console=on_console,
                source=evidence_source,
            )
        state = "locked"
    elif declared_state == "locked":
        return _unknown_session_state(
            "session_state_conflicting_fields",
            on_console=on_console,
            source=evidence_source,
        )
    elif on_console and declared_state in {"", "unlocked"}:
        state = "unlocked"
    else:
        return _unknown_session_state("session_state_off_console", on_console=on_console, source=evidence_source)
    if state == "unlocked" and evidence_source == "session_state_helper_io_registry":
        reason = "explicit_unlocked_console_io_registry"
    elif state == "locked" and evidence_source == "session_state_helper_io_registry":
        reason = "explicit_locked_io_registry"
    else:
        reason = "explicit_unlocked_console" if state == "unlocked" else "explicit_locked"
    return {
        "state": state,
        "locked": state != "unlocked",
        "on_console": on_console,
        "capture_allowed": state == "unlocked",
        "source": evidence_source,
        "reason": reason,
        "observed_at": utc_now(),
    }


def _unknown_session_state(error: str, *, on_console=None, source: str = "session_state_helper") -> dict:
    return {
        "state": "unknown",
        "locked": True,
        "on_console": on_console if isinstance(on_console, bool) else None,
        "capture_allowed": False,
        "error": error,
        "source": source,
        "reason": error,
        "observed_at": utc_now(),
    }


def _session_state_kind(lock: dict) -> str:
    state = str(lock.get("state") or "").strip().lower()
    if state in {"unlocked", "locked", "unknown"}:
        return state
    if lock.get("error"):
        return "unknown"
    # Compatibility for existing in-process callers and older tests. The real
    # helper always passes through session_state(), which validates on_console.
    if lock.get("locked") is True:
        return "locked"
    if lock.get("locked") is False:
        return "unlocked"
    return "unknown"


def install_launch_agent(
    db_path: str | Path,
    *,
    privacy_profile: str | None = None,
    preserve_privacy_profile: bool = False,
) -> dict:
    project_root = Path(__file__).resolve().parents[1]
    db = Path(db_path).resolve()
    runtime_root = _ensure_ascii_runtime_link(project_root)
    try:
        runtime_db = runtime_root / db.relative_to(project_root)
    except ValueError:
        runtime_db = db
    launch_logs_dir = Path.home() / "Library" / "Logs" / "Personal AI Monitor"
    launch_logs_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(launch_logs_dir, 0o700)
    stdout_path = launch_logs_dir / "daemon.out.log"
    stderr_path = launch_logs_dir / "daemon.err.log"
    for log_path in (stdout_path, stderr_path):
        log_path.touch(exist_ok=True)
        os.chmod(log_path, 0o600)
    helpers = ensure_all_helpers(db.parent)
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_ID}.plist"
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    existing_launch_env = _launch_agent_environment(plist_path)
    environment = {
        "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        PRIVACY_PROFILE_ENV_KEY: DEFAULT_PRIVACY_PROFILE,
    }
    privacy_env = _privacy_profile_environment(
        privacy_profile,
        preserve=preserve_privacy_profile,
        launch_env=existing_launch_env,
    )
    if privacy_env:
        environment.update(privacy_env)
    document_change_startup_reconcile_raw = _document_change_startup_reconcile_raw(
        existing_launch_env
    )
    document_change_startup_reconcile_enabled = _document_change_startup_reconcile_enabled(
        existing_launch_env
    )
    if document_change_startup_reconcile_raw is not None:
        environment[DOCUMENT_CHANGE_STARTUP_RECONCILE_ENV_KEY] = (
            "1" if document_change_startup_reconcile_enabled else "0"
        )
    plist = build_launch_agent_payload(
        {
            "kind": "core",
            "label": LAUNCH_AGENT_ID,
            "program": sys.executable,
            "core_root": str(runtime_root),
            "database": str(runtime_db),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "environment": environment,
        }
    )
    with plist_path.open("wb") as fh:
        plistlib.dump(plist, fh)
    os.chmod(plist_path, 0o600)

    target = f"gui/{os.getuid()}"
    _launchctl(["bootout", target, str(plist_path)])
    bootstrap = _launchctl(["bootstrap", target, str(plist_path)])
    enable = _launchctl(["enable", f"{target}/{LAUNCH_AGENT_ID}"])
    return {
        "plist": str(plist_path),
        "runtime_root": str(runtime_root),
        "logs": str(launch_logs_dir),
        "helpers": helpers,
        "privacy_profile": environment.get(PRIVACY_PROFILE_ENV_KEY, DEFAULT_PRIVACY_PROFILE),
        "privacy_profile_preserved": bool(preserve_privacy_profile and not privacy_profile and privacy_env),
        "document_change_startup_reconcile_enabled": document_change_startup_reconcile_enabled,
        "bootstrap": bootstrap,
        "enable": enable,
    }


def configure_privacy_profile(conn, root_dir: str | Path, *, profile: str) -> dict:
    ensure_daemon_defaults(conn)
    normalized = normalize_privacy_profile(profile)
    if normalized != str(profile or "").strip().lower():
        raise ValueError(f"unsupported privacy profile: {profile}")
    _set_setting(conn, "privacy_profile", normalized)
    conn.commit()
    _apply_privacy_profile_from_settings(conn)
    return {
        "profile": normalized,
        "runtime_profile": normalized,
        "source_default": DEFAULT_PRIVACY_PROFILE,
        "personal_high_precision": normalized == PRIVACY_PROFILE_PERSONAL_HIGH_PRECISION,
        "summary": _privacy_profile_summary(normalized),
    }


def configure_screen_capture(conn, *, enabled: bool) -> dict:
    """Grant at most 30 minutes of screen capture; file tracking is independent."""
    ensure_daemon_defaults(conn)
    active = bool(enabled)
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    lease = configure_capture_lease(conn, enabled=active)
    _set_state(conn, "daemon_status", "ready" if active else "paused")
    _set_state(conn, "last_error", "")
    conn.commit()
    return {
        "screen_capture_enabled": active,
        "screen_capture_lease": lease,
        "status": "ready" if active else "paused",
        "document_change_tracking_enabled": _bool_setting(
            conn,
            "document_change_tracking_enabled",
            True,
        ),
        "pause_mode": _get_state(conn, "pause_mode", ""),
        "raw_access": False,
    }


def uninstall_launch_agent() -> dict:
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_ID}.plist"
    target = f"gui/{os.getuid()}"
    bootout = _launchctl(["bootout", target, str(plist_path)])
    if plist_path.exists():
        plist_path.unlink()
    return {"plist": str(plist_path), "removed": True, "bootout": bootout}


def _ensure_ascii_runtime_link(project_root: Path) -> Path:
    project_root = project_root.resolve()
    try:
        str(project_root).encode("ascii")
    except UnicodeEncodeError:
        pass
    else:
        return project_root

    runtime_root = Path.home() / ".personal-ai-monitor"
    if runtime_root.is_symlink():
        if runtime_root.resolve() != project_root:
            runtime_root.unlink()
            runtime_root.symlink_to(project_root, target_is_directory=True)
    elif runtime_root.exists():
        raise RuntimeError(f"{runtime_root} exists and is not a symlink")
    else:
        runtime_root.symlink_to(project_root, target_is_directory=True)
    return runtime_root


def _ingest_and_fingerprint(conn, events: list[dict], skipped: list[dict], errors: list[dict]) -> dict:
    from .pipeline import ingest_events

    report = (
        ingest_events(conn, events)
        if events
        else {"ingested": 0, "clear": 0, "partial_redacted": 0, "blocked": 0, "review_required": 0, "duplicate_exact": 0, "partial_duplicate": 0}
    )
    for item in skipped:
        reason = str(item.get("reason") or "skipped")
        is_duplicate = reason == "duplicate_frame"
        record_capture_coverage(
            conn,
            str(item.get("display_id") or "unknown"),
            str(item.get("captured_at") or utc_now()),
            "keyframe_duplicate_skip" if is_duplicate else "keyframe_context_skip",
            frames_sampled=1,
            frames_dropped=1,
            gap=0 if is_duplicate else 1,
            duplicate_exact=1 if is_duplicate else 0,
            degraded_reason=reason,
        )
        if is_duplicate:
            record_duplicate_event(
                conn,
                str(item.get("display_id") or "unknown"),
                str(item.get("captured_at") or utc_now()),
                "frame_hash_duplicate",
                note="与上一条同显示器截屏画面完全重复，已跳过 OCR 和内容记录。",
            )
    for item in errors:
        record_capture_coverage(
            conn,
            str(item.get("display_id") or "unknown"),
            str(item.get("captured_at") or utc_now()),
            "keyframe_error",
            frames_sampled=0,
            frames_dropped=1,
            raw_jobs_created=0,
            raw_jobs_deleted=0,
            gap=1,
            degraded_reason=str(item.get("reason") or "capture_error"),
        )
    for item in [*events, *skipped]:
        _update_fingerprint(conn, str(item.get("display_id") or ""), str(item.get("frame_hash") or ""), str(item.get("captured_at") or utc_now()))
    conn.commit()
    return report


def _update_fingerprint(conn, display_id: str, frame_hash: str, captured_at: str) -> None:
    if not display_id or not frame_hash:
        return
    conn.execute(
        """
        INSERT INTO capture_fingerprints(display_id, frame_hash, captured_at, updated_at)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(display_id) DO UPDATE SET
          frame_hash = excluded.frame_hash,
          captured_at = excluded.captured_at,
          updated_at = excluded.updated_at
        """,
        (display_id, frame_hash, captured_at, utc_now()),
    )


def _migrate_default_retention(conn, now: str) -> None:
    row = conn.execute("SELECT value FROM settings WHERE key = 'retention_days'").fetchone()
    if row and str(row["value"]) == str(LEGACY_RETENTION_DAYS):
        conn.execute(
            """
            UPDATE settings
            SET value = ?, updated_at = ?
            WHERE key = 'retention_days'
            """,
            (str(DEFAULT_RETENTION_DAYS), now),
        )
    conn.execute(
        """
        INSERT INTO settings(key, value, updated_at) VALUES('retention_policy', ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (DEFAULT_RETENTION_POLICY, now),
    )


def _get_setting(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else default


def _set_setting(conn, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO settings(key, value, updated_at) VALUES(?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, utc_now()),
    )


def _dev_dashboard_enabled(conn) -> bool:
    value = _get_setting(conn, "dev_dashboard_enabled", "false").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _hygiene_auto_cleanup_enabled(conn) -> bool:
    return _bool_setting(conn, "hygiene_auto_cleanup_enabled", False)


def _hygiene_backup_rotation_enabled(conn) -> bool:
    return _bool_setting(conn, "hygiene_backup_rotation_enabled", False)


def _bool_setting(conn, key: str, default: bool = False) -> bool:
    fallback = "true" if default else "false"
    return _get_setting(conn, key, fallback).strip().lower() in {"1", "true", "yes", "on"}


def _hygiene_int_setting(conn, key: str, default: int) -> int:
    try:
        return int(_get_setting(conn, key, str(default)))
    except ValueError:
        return int(default)


def _hygiene_cleanup_snapshot(conn, root_dir: str | Path) -> dict:
    root = Path(root_dir)
    core_root = root.parent if root.name == "data" else root
    database_path = _database_path(conn)
    quarantine = hygiene_cleanup_state(core_root, database_path)
    enabled = _hygiene_auto_cleanup_enabled(conn)
    backup_rotation_enabled = _hygiene_backup_rotation_enabled(conn)
    last_run_at = _get_state(conn, "hygiene_cleanup_last_run_at", "")
    last_error = _get_state(conn, "hygiene_cleanup_last_error", "")
    status = _get_state(conn, "hygiene_cleanup_status", "disabled" if not enabled else "ready")
    if enabled and status == "disabled":
        status = "ready"
    quarantine_count = int(quarantine.get("quarantine_count") or 0)
    if not enabled:
        label = "未启用"
        kind = "neutral"
        summary = f"未启用；隔离区 {quarantine_count} 项。"
    elif status == "error" and last_error:
        label = "异常"
        kind = "warning"
        summary = f"已启用；最近运行失败；隔离区 {quarantine_count} 项。"
    elif last_run_at:
        label = "已启用"
        kind = "ok"
        summary = f"已启用；最近运行 {last_run_at}；隔离区 {quarantine_count} 项。"
    else:
        label = "已启用"
        kind = "ok"
        summary = f"已启用；等待首次自动运行；隔离区 {quarantine_count} 项。"
    return {
        "enabled": enabled,
        "backup_rotation_enabled": backup_rotation_enabled,
        "keep_backups": _hygiene_int_setting(conn, "hygiene_keep_backups", DEFAULT_KEEP_BACKUPS),
        "min_age_days": _hygiene_int_setting(conn, "hygiene_min_age_days", DEFAULT_MIN_AGE_DAYS),
        "quarantine_days": _hygiene_int_setting(conn, "hygiene_quarantine_days", DEFAULT_QUARANTINE_DAYS),
        "status": status,
        "label": label,
        "kind": kind,
        "summary": summary,
        "last_run_at": last_run_at,
        "last_error": last_error,
        "last_report": _get_state(conn, "hygiene_cleanup_last_report", ""),
        "quarantine_root": quarantine.get("quarantine_root") or "",
        "quarantine_count": quarantine_count,
        "quarantine_size_bytes": int(quarantine.get("quarantine_size_bytes") or 0),
        "next_delete_after": quarantine.get("next_delete_after") or "",
    }


def _compact_hygiene_report(result: dict) -> dict:
    summary = result.get("summary") if isinstance(result, dict) else {}
    policy = result.get("policy") if isinstance(result, dict) else {}
    return {
        "mode": result.get("mode") if isinstance(result, dict) else "",
        "summary": summary if isinstance(summary, dict) else {},
        "policy": policy if isinstance(policy, dict) else {},
    }


def _get_state(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM daemon_state WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else default


def _set_state(conn, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO daemon_state(key, value, updated_at) VALUES(?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, utc_now()),
    )


def _set_state_if_changed(conn, key: str, value: str) -> bool:
    if _get_state(conn, key, "") == value:
        return False
    _set_state(conn, key, value)
    return True


def _paused_remaining_seconds(pause_until: str) -> int:
    if not pause_until:
        return 0
    try:
        until = parse_time(pause_until)
    except ValueError:
        return 0
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    remaining = (until - datetime.now(timezone.utc)).total_seconds()
    return max(0, int(remaining))


def _clear_pause_state(conn) -> None:
    _set_state(conn, "pause_until", "")
    _set_state(conn, "pause_mode", "")


def _pause_snapshot(conn, *, locked: bool) -> dict:
    mode = _get_state(conn, "pause_mode", "").strip().lower()
    pause_until = _get_state(conn, "pause_until", "")
    remaining = _paused_remaining_seconds(pause_until)

    if mode in {"duration", "today"} and pause_until and remaining <= 0:
        _clear_pause_state(conn)
        mode = ""
        pause_until = ""
        remaining = 0
    if mode == "until_lock" and locked:
        _clear_pause_state(conn)
        mode = ""
        pause_until = ""
        remaining = 0

    active = mode in {"until_lock", "manual"} or remaining > 0
    if not active:
        label = "未暂停"
    elif mode == "until_lock":
        label = "暂停到下次锁屏"
    elif mode == "manual":
        label = "暂停（直到手动开启）"
    elif mode == "today":
        label = f"今天不记录，还剩 {_format_duration_cn(remaining)}"
    else:
        label = f"暂停中，还剩 {_format_duration_cn(remaining)}"

    return {
        "active": active,
        "mode": mode,
        "pause_until": pause_until,
        "paused_remaining_seconds": remaining,
        "label": label,
    }


def _format_duration_cn(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} 分钟"
    hours = minutes // 60
    remainder = minutes % 60
    if remainder == 0:
        return f"{hours} 小时"
    return f"{hours} 小时 {remainder} 分钟"


def _today_bounds() -> tuple[str, str]:
    start_dt = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    end_dt = start_dt + timedelta(days=1)
    return start_dt.isoformat(timespec="seconds"), end_dt.isoformat(timespec="seconds")


def _next_capture_window(
    last_capture_at: str,
    interval_seconds: int,
    status: str,
    *,
    launch_agent_running: bool,
) -> tuple[str, int | None]:
    if not launch_agent_running or status in {
        "paused",
        "locked",
        "error",
        "session_state_unknown",
    }:
        return "", None
    if not last_capture_at:
        return "", 0
    try:
        last = parse_time(last_capture_at)
    except ValueError:
        return "", 0
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    next_capture = last + timedelta(seconds=max(1, interval_seconds))
    remaining = max(0, int((next_capture - datetime.now(timezone.utc)).total_seconds() + 0.999))
    return next_capture.isoformat(timespec="seconds"), remaining


def _capture_freshness(
    last_capture_at: str,
    interval_seconds: int,
    status: str,
    *,
    launch_agent_loaded: bool,
    launch_agent_running: bool,
    screen_capture_enabled: bool = True,
) -> dict:
    threshold = max(3 * max(1, int(interval_seconds or DEFAULT_INTERVAL_SECONDS)), 90)
    active_capture_expected = screen_capture_enabled and launch_agent_running and status not in {
        "paused",
        "locked",
        "session_state_unknown",
    }
    expectation_reason = ""
    expectation_summary = ""
    if not launch_agent_running:
        expectation_reason = "service_error" if launch_agent_loaded else "service_not_running"
        expectation_summary = (
            "记录服务已安装但当前没有运行；不期待新的自动采集。"
            if launch_agent_loaded
            else "记录服务未启动；不期待新的自动采集。"
        )
    elif not screen_capture_enabled:
        expectation_reason = "screen_capture_disabled"
        expectation_summary = "当前仅文件追踪；屏幕关闭期间不期待新的采集。"
    elif status == "paused":
        expectation_reason = "paused"
        expectation_summary = "当前已暂停；暂停期间不期待新的自动采集。"
    elif status == "locked":
        expectation_reason = "locked"
        expectation_summary = "Mac 当前锁屏；锁屏期间不期待新的 keyframe 采集。"
    elif status == "session_state_unknown":
        expectation_reason = "session_state_unknown"
        expectation_summary = "当前无法确认 Mac 会话状态；系统已停止新采集。"
    if not last_capture_at:
        is_stale = bool(active_capture_expected)
        return {
            "last_capture_age_seconds": None,
            "stale_threshold_seconds": threshold,
            "is_stale": is_stale,
            "active_capture_expected": active_capture_expected,
            "reason": "never_captured" if is_stale else expectation_reason,
            "summary": "记录服务在运行，但还没有成功采集。" if is_stale else (expectation_summary or "还没有采集记录。"),
        }
    try:
        last = parse_time(last_capture_at)
    except ValueError:
        return {
            "last_capture_age_seconds": None,
            "stale_threshold_seconds": threshold,
            "is_stale": bool(active_capture_expected),
            "active_capture_expected": active_capture_expected,
            "reason": "invalid_last_capture_at",
            "summary": "最近采集时间无法识别，需要刷新或重启记录服务。",
        }
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    age = max(0, int((datetime.now(timezone.utc) - last).total_seconds()))
    is_stale = bool(active_capture_expected and age > threshold)
    summary = (
        f"记录服务在运行，但最近 {_format_duration_cn(age)} 没有新采集。"
        if is_stale
        else (
            f"最近一次采集在 {_format_duration_cn(age)} 前；{expectation_summary}"
            if expectation_summary
            else f"最近一次采集在 {_format_duration_cn(age)} 前。"
        )
    )
    return {
        "last_capture_age_seconds": age,
        "stale_threshold_seconds": threshold,
        "is_stale": is_stale,
        "active_capture_expected": active_capture_expected,
        "reason": "stale_capture" if is_stale else expectation_reason,
        "summary": summary,
    }


def _screen_recording_permission_status(
    *,
    last_error: str,
    last_attempt_at: str,
    last_success_at: str,
    launch_agent_running: bool,
    capture_freshness: dict,
) -> dict:
    """Report backend capture evidence without trusting the App's own TCC identity."""
    normalized_error = str(last_error or "").strip().lower()
    if normalized_error == "screen_recording_permission_denied":
        return {
            "state": "denied",
            "source": "daemon_capture_error",
            "backend_authoritative": True,
            "summary": "记录核心报告屏幕录制权限异常。",
        }
    if normalized_error:
        return {
            "state": "unknown",
            "source": "daemon_capture_error",
            "backend_authoritative": True,
            "summary": "记录核心最近一次采集存在错误，暂不能确认后台权限链路。",
        }
    success_covers_attempt = _timestamp_not_older(last_success_at, last_attempt_at)
    capture_age = capture_freshness.get("last_capture_age_seconds")
    freshness_threshold = capture_freshness.get("stale_threshold_seconds")
    recent_success = (
        isinstance(capture_age, int)
        and isinstance(freshness_threshold, int)
        and capture_age <= freshness_threshold
    )
    if launch_agent_running and last_success_at and success_covers_attempt and recent_success:
        return {
            "state": "granted_observed",
            "source": "daemon_recent_success",
            "backend_authoritative": True,
            "summary": "记录核心最近成功采集，后台权限链路可用。",
        }
    return {
        "state": "unknown",
        "source": "daemon_no_recent_evidence" if launch_agent_running else "service_not_running",
        "backend_authoritative": True,
        "summary": "记录核心尚无足够的新鲜证据判断屏幕录制权限。",
    }


def _timestamp_not_older(candidate: str, reference: str) -> bool:
    if not candidate:
        return False
    if not reference:
        return True
    try:
        return parse_time(candidate) >= parse_time(reference)
    except (TypeError, ValueError):
        return False


def _raw_vault(root: Path) -> Path:
    try:
        supplied_root = root.expanduser()
        supplied_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        root_metadata = supplied_root.lstat()
        if (
            stat.S_ISLNK(root_metadata.st_mode)
            or not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_uid != os.getuid()
        ):
            raise RawVaultSafetyError("raw_vault_unsafe")
        resolved_root = supplied_root.resolve(strict=True)
        vault = resolved_root / "raw_vault"
        try:
            vault.mkdir(mode=0o700)
        except FileExistsError:
            pass
        _tighten_owned_private_directory(vault)
        marker = vault / ".metadata_never_index"
        _ensure_raw_vault_marker(marker)
        return vault
    except RawVaultSafetyError:
        raise
    except OSError:
        raise RawVaultSafetyError("raw_vault_unsafe") from None


class RawVaultSafetyError(PermissionError):
    """Content-free signal for unsafe raw-vault paths or incomplete cleanup."""


def _runtime_root_for_db_path(db_path: str | Path) -> Path:
    expanded = os.path.expanduser(os.fspath(db_path))
    return Path(os.path.abspath(expanded)).parent


def _tighten_owned_private_directory(path: Path) -> None:
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
    ):
        raise RawVaultSafetyError("raw_vault_unsafe")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        path.chmod(0o700)
    verified = path.lstat()
    if (
        stat.S_ISLNK(verified.st_mode)
        or not stat.S_ISDIR(verified.st_mode)
        or verified.st_uid != os.getuid()
        or stat.S_IMODE(verified.st_mode) != 0o700
    ):
        raise RawVaultSafetyError("raw_vault_unsafe")


def _ensure_raw_vault_marker(marker: Path) -> None:
    try:
        metadata = marker.lstat()
    except FileNotFoundError:
        metadata = None
    if metadata is not None:
        if stat.S_ISLNK(metadata.st_mode):
            marker.unlink()
        elif not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise RawVaultSafetyError("raw_vault_unsafe")
        else:
            return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(marker, flags, 0o600)
    os.close(descriptor)


def _remove_raw_vault_tree(path: Path) -> None:
    metadata = path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
    ):
        raise RawVaultSafetyError("raw_vault_cleanup_failed")
    for child in path.iterdir():
        child_metadata = child.lstat()
        if stat.S_ISLNK(child_metadata.st_mode):
            child.unlink()
        elif stat.S_ISDIR(child_metadata.st_mode):
            _remove_raw_vault_tree(child)
        else:
            if child_metadata.st_uid != os.getuid():
                raise RawVaultSafetyError("raw_vault_cleanup_failed")
            child.unlink()
    path.rmdir()


def _launchctl(args: list[str]) -> dict:
    completed = subprocess.run(
        ["launchctl", *args],
        check=False,
        timeout=12,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def _launch_agent_status() -> dict:
    result = _launchctl(["print", f"gui/{os.getuid()}/{LAUNCH_AGENT_ID}"])
    stdout = result.get("stdout", "")
    pid = ""
    last_exit_code = ""
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("pid = "):
            pid = stripped.removeprefix("pid = ").strip()
        elif stripped.startswith("last exit code = "):
            last_exit_code = stripped.removeprefix("last exit code = ").strip()
    return {
        "label": LAUNCH_AGENT_ID,
        "loaded": result["returncode"] == 0,
        "running": result["returncode"] == 0 and "state = running" in stdout and bool(pid),
        "pid": pid,
        "last_exit_code": last_exit_code,
    }


def _runtime_identity(conn, root_dir: Path) -> dict:
    core_root = Path(__file__).resolve().parents[1]
    data_root = Path(root_dir)
    database_path = _database_path(conn)
    wrapper_root = Path.home() / ".personal-ai-monitor"
    app_support_core = Path.home() / "Library" / "Application Support" / "Personal AI Monitor" / "core"
    core_resolved = core_root.resolve()
    data_resolved = data_root.resolve()
    db_resolved = Path(database_path).resolve() if database_path else Path()
    wrapper_resolved = wrapper_root.resolve()
    app_support_resolved = app_support_core.resolve()
    is_wrapper = (
        _is_under_literal(core_root, wrapper_root)
        or _is_under_literal(data_root, wrapper_root)
        or (database_path and _is_under_literal(Path(database_path), wrapper_root))
    )
    is_app_support = (
        _is_under(core_resolved, app_support_resolved)
        or _is_under(data_resolved, app_support_resolved)
        or (database_path and _is_under(db_resolved, app_support_resolved))
    )
    is_repo_local = (core_root.parent / ".git").exists()
    if is_app_support:
        path_kind = "application_support"
        label = "Application Support live runtime"
    elif is_wrapper:
        path_kind = "wrapper"
        label = ".personal-ai-monitor wrapper"
    elif is_repo_local:
        path_kind = "repo_local"
        label = "repo-local development core"
    else:
        path_kind = "other"
        label = "custom local core"
    return {
        "core_root": str(core_root),
        "core_root_resolved": str(core_resolved),
        "data_root": str(data_root),
        "data_root_resolved": str(data_resolved),
        "database_path": database_path,
        "database_path_resolved": str(db_resolved) if database_path else "",
        "python_executable": sys.executable,
        "process_pid": os.getpid(),
        "core_version": __version__,
        "core_code_hash": _runtime_core_code_hash(core_root),
        "path_kind": path_kind,
        "path_label": label,
        "is_personal_ai_monitor_wrapper": is_wrapper,
        "is_application_support_runtime": is_app_support,
        "is_repo_local": is_repo_local,
    }


def _runtime_core_code_hash(core_root: Path) -> str:
    try:
        candidates: list[Path] = [
            core_root / "pyproject.toml",
            core_root / "pam_isolated_launcher.py",
        ]
        for relative_root in (Path("activity_mvp"), Path("tools"), Path("browser_extension"), Path("evolution")):
            directory = core_root / relative_root
            if not directory.is_dir():
                continue
            candidates.extend(
                path
                for path in directory.rglob("*")
                if path.is_file() and path.name != ".DS_Store" and path.suffix != ".pyc"
            )
        candidates = [path for path in set(candidates) if path.is_file()]
        if not candidates:
            return "unknown"

        digest = hashlib.sha256()
        for path in sorted(candidates, key=lambda item: item.relative_to(core_root).as_posix()):
            relative = f"core/{path.relative_to(core_root).as_posix()}"
            file_digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    file_digest.update(chunk)
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(file_digest.hexdigest().encode("ascii"))
            digest.update(b"\n")
    except (OSError, ValueError):
        return "unknown"
    return digest.hexdigest()


def _database_path(conn) -> str:
    row = conn.execute("PRAGMA database_list").fetchone()
    if row is None:
        return ""
    try:
        return str(row["file"])
    except (KeyError, TypeError, IndexError):
        return str(row[2] or "")


def _dashboard_health(conn, launch_agent: dict, host: str = "127.0.0.1", port: int = 8765) -> dict:
    dev_enabled = _dev_dashboard_enabled(conn)
    listener = _dashboard_listener(host, port)
    listener_pid = str(listener.get("pid") or "")
    launch_pid = str(launch_agent.get("pid") or "")
    managed_by_daemon = bool(
        dev_enabled
        and listener.get("listening")
        and listener_pid
        and launch_pid
        and listener_pid == launch_pid
    )
    foreign_listener = bool(listener.get("listening") and not managed_by_daemon)
    if managed_by_daemon:
        status = "managed_by_daemon"
        port_status = "managed_by_daemon"
        summary = "开发调试面板由记录服务托管。"
    elif foreign_listener and not dev_enabled:
        # Keep the legacy status/field for older UI clients, but explicitly mark
        # this as a foreign port that is outside PAM health when dev mode is off.
        status = "orphan_listener"
        port_status = "foreign_listener"
        summary = "PAM 开发调试面板未启用；该端口由其他本机进程占用，不计入 PAM 健康。"
    elif foreign_listener:
        status = "port_conflict"
        port_status = "port_conflict"
        summary = "PAM 开发调试面板已启用，但端口由其他本机进程占用。"
    elif dev_enabled:
        status = "enabled_not_listening"
        port_status = "not_listening"
        summary = "开发调试面板已启用，但当前没有监听端口。"
    else:
        status = "off"
        port_status = "not_checked"
        summary = "开发调试面板未运行。"
    return {
        "host": host,
        "port": port,
        "enabled": dev_enabled,
        "listening": bool(listener.get("listening")),
        "pid": listener_pid,
        "command": str(listener.get("command") or ""),
        "managed_by_daemon": managed_by_daemon,
        "orphan_listener": foreign_listener and not dev_enabled,
        "foreign_listener": foreign_listener,
        "port_conflict": bool(dev_enabled and foreign_listener),
        "health_in_scope": dev_enabled,
        "port_status": port_status,
        "last_start_status": _get_state(conn, "dashboard_start_status", ""),
        "status": status,
        "summary": summary,
    }


def _dashboard_listener(host: str, port: int) -> dict:
    listening = _is_port_listening(host, port)
    if not listening:
        return {"listening": False, "pid": "", "command": ""}
    try:
        completed = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
            check=False,
            timeout=2,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return {"listening": True, "pid": "", "command": ""}
    for line in completed.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            return {"listening": True, "pid": parts[1], "command": parts[0]}
    return {"listening": True, "pid": "", "command": ""}


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _is_under_literal(path: Path, root: Path) -> bool:
    path = path.expanduser()
    root = root.expanduser()
    if not path.is_absolute():
        return False
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _coverage_health(
    conn,
    start: str,
    end: str,
    *,
    activity_count: int,
    privacy_count: int,
    gap_count: int,
    raw_jobs_pending_override: int | None = None,
) -> dict:
    totals = conn.execute(
        """
        SELECT
          COUNT(*) AS rows_count,
          COALESCE(SUM(frames_sampled), 0) AS frames_sampled,
          COALESCE(SUM(frames_dropped), 0) AS frames_dropped,
          COALESCE(SUM(clear_count), 0) AS clear_count,
          COALESCE(SUM(partial_redacted_count), 0) AS partial_redacted_count,
          COALESCE(SUM(review_required_count), 0) AS review_required_count,
          COALESCE(SUM(blocked_count), 0) AS blocked_count,
          COALESCE(SUM(gap_count), 0) AS coverage_gap_count,
          COALESCE(SUM(duplicate_exact_count), 0) AS duplicate_exact_count,
          COALESCE(SUM(partial_duplicate_count), 0) AS partial_duplicate_count,
          COALESCE(SUM(raw_jobs_created), 0) AS raw_jobs_created,
          COALESCE(SUM(raw_jobs_deleted), 0) AS raw_jobs_deleted
        FROM capture_coverage_ledger
        WHERE datetime(started_at) >= datetime(?)
          AND datetime(started_at) < datetime(?)
        """,
        (start, end),
    ).fetchone()
    reason_rows = conn.execute(
        """
        SELECT degraded_reason, COUNT(*) AS count
        FROM capture_coverage_ledger
        WHERE datetime(started_at) >= datetime(?)
          AND datetime(started_at) < datetime(?)
          AND degraded_reason IS NOT NULL
          AND degraded_reason != ''
        GROUP BY degraded_reason
        ORDER BY count DESC, degraded_reason ASC
        LIMIT 5
        """,
        (start, end),
    ).fetchall()
    display_rows = conn.execute(
        """
        SELECT
          COALESCE(display_id, 'unknown') AS display_id,
          COALESCE(SUM(frames_sampled), 0) AS frames_sampled,
          COALESCE(SUM(frames_dropped), 0) AS frames_dropped,
          COALESCE(SUM(clear_count), 0) AS clear_count,
          COALESCE(SUM(partial_redacted_count), 0) AS partial_redacted_count,
          COALESCE(SUM(blocked_count), 0) AS blocked_count,
          COALESCE(SUM(gap_count), 0) AS gap_count,
          COALESCE(SUM(duplicate_exact_count), 0) AS duplicate_exact_count,
          COALESCE(SUM(partial_duplicate_count), 0) AS partial_duplicate_count,
          MAX(started_at) AS last_attempt_at,
          MAX(CASE WHEN COALESCE(gap_count, 0) = 0 THEN started_at END) AS last_success_at
        FROM capture_coverage_ledger
        WHERE datetime(started_at) >= datetime(?)
          AND datetime(started_at) < datetime(?)
        GROUP BY display_id
        ORDER BY display_id ASC
        """,
        (start, end),
    ).fetchall()

    frames_sampled = int(totals["frames_sampled"] or 0)
    frames_dropped = int(totals["frames_dropped"] or 0)
    partial_redacted_count = int(totals["partial_redacted_count"] or 0)
    duplicate_exact_count = int(totals["duplicate_exact_count"] or 0)
    partial_duplicate_count = int(totals["partial_duplicate_count"] or 0)
    coverage_gap_count = int(totals["coverage_gap_count"] or 0)
    raw_jobs_created = int(totals["raw_jobs_created"] or 0)
    raw_jobs_deleted = int(totals["raw_jobs_deleted"] or 0)
    raw_jobs_pending = (
        max(0, int(raw_jobs_pending_override))
        if raw_jobs_pending_override is not None
        else max(0, raw_jobs_created - raw_jobs_deleted)
    )
    raw_jobs_processed_legacy = max(0, raw_jobs_created - raw_jobs_deleted - raw_jobs_pending)
    raw_jobs_completed = max(0, raw_jobs_processed_legacy + raw_jobs_deleted)
    raw_jobs_accounting_gap = max(0, raw_jobs_created - raw_jobs_deleted - raw_jobs_pending - raw_jobs_processed_legacy)
    main_reasons = [
        {
            "reason": row["degraded_reason"],
            "label": _reason_label(
                row["degraded_reason"],
                raw_jobs_pending=raw_jobs_pending,
                raw_jobs_processed_legacy=raw_jobs_processed_legacy,
            ),
            "count": int(row["count"] or 0),
        }
        for row in reason_rows
    ]
    protected_total = max(privacy_count, int(totals["blocked_count"] or 0))
    active_gap_count = int(gap_count or 0)
    missed_total = max(active_gap_count, coverage_gap_count)
    coverage_only_gap_count = max(0, coverage_gap_count - active_gap_count)
    if active_gap_count > 0 and coverage_gap_count > active_gap_count:
        gap_semantics = "active_and_coverage_ledger"
    elif active_gap_count > 0:
        gap_semantics = "active_gap_events"
    elif coverage_gap_count > 0:
        gap_semantics = "coverage_ledger_only"
    else:
        gap_semantics = "none"
    if main_reasons:
        reason_text = "主要原因：" + "、".join(f"{item['label']} {item['count']} 次" for item in main_reasons[:3])
    else:
        reason_text = "暂无明显缺口原因"
    pending_raw_text = f"，短时待处理原图 {raw_jobs_pending} 个" if raw_jobs_pending else ""
    processed_legacy_text = f"，本地视觉复核已补处理 {raw_jobs_processed_legacy} 个" if raw_jobs_processed_legacy else ""
    summary = (
        f"今天采集 {frames_sampled} 次，形成 {activity_count} 条可回顾记录，"
        f"其中局部清洗 {partial_redacted_count} 条，完全重复跳过 {duplicate_exact_count} 次，"
        f"部分重复清洗 {partial_duplicate_count} 次，保护 {protected_total} 次，当前缺口 {active_gap_count} 条，"
        f"覆盖账本缺口 {coverage_gap_count} 次"
        f"{pending_raw_text}{processed_legacy_text}。{reason_text}。"
    )
    displays = [
        {
            "display_id": row["display_id"],
            "frames_sampled": int(row["frames_sampled"] or 0),
            "frames_dropped": int(row["frames_dropped"] or 0),
            "clear_count": int(row["clear_count"] or 0),
            "partial_redacted_count": int(row["partial_redacted_count"] or 0),
            "blocked_count": int(row["blocked_count"] or 0),
            "gap_count": int(row["gap_count"] or 0),
            "coverage_ledger_gap_count": int(row["gap_count"] or 0),
            "active_gap_count": 0,
            "duplicate_exact_count": int(row["duplicate_exact_count"] or 0),
            "partial_duplicate_count": int(row["partial_duplicate_count"] or 0),
            "last_attempt_at": row["last_attempt_at"] or "",
            "last_success_at": row["last_success_at"] or "",
            "last_capture_at": row["last_success_at"] or "",
            "status": "attention" if int(row["gap_count"] or 0) > 0 else "ok",
        }
        for row in display_rows
    ]
    return {
        "time_range": {"from": start, "to": end},
        "frames_sampled": frames_sampled,
        "frames_dropped": frames_dropped,
        "clear_activity_count": activity_count,
        "partial_redacted_count": partial_redacted_count,
        "duplicate_exact_count": duplicate_exact_count,
        "partial_duplicate_count": partial_duplicate_count,
        "normal_clear_count": max(0, activity_count - partial_redacted_count),
        "protected_count": protected_total,
        "gap_count": missed_total,
        "active_gap_count": active_gap_count,
        "coverage_ledger_gap_count": coverage_gap_count,
        "coverage_only_gap_count": coverage_only_gap_count,
        "gap_count_source": gap_semantics,
        "gap_semantics": {
            "active_gap_count": active_gap_count,
            "coverage_ledger_gap_count": coverage_gap_count,
            "coverage_only_gap_count": coverage_only_gap_count,
            "reported_gap_count": missed_total,
            "source": gap_semantics,
            "summary": (
                f"当前 active gap {active_gap_count} 条；coverage ledger gap {coverage_gap_count} 次。"
                "二者分开展示，避免把历史/账本覆盖缺口误读成当前采集失败。"
            ),
        },
        "review_required_count": int(totals["review_required_count"] or 0),
        "blocked_count": int(totals["blocked_count"] or 0),
        "raw_jobs_created": raw_jobs_created,
        "raw_jobs_processed_or_deleted": raw_jobs_completed,
        "raw_jobs_processed": raw_jobs_completed,
        "raw_jobs_processed_legacy": raw_jobs_processed_legacy,
        "raw_jobs_pending": raw_jobs_pending,
        "raw_jobs_unresolved": raw_jobs_pending,
        "raw_jobs_deleted_ledger": raw_jobs_deleted,
        "raw_jobs_accounting_gap": raw_jobs_accounting_gap,
        "raw_jobs_deleted": raw_jobs_deleted,
        "degraded_count": sum(item["count"] for item in main_reasons),
        "main_reasons": main_reasons,
        "display_count": len(displays),
        "display_ids": [item["display_id"] for item in displays],
        "displays": displays,
        "summary": summary,
    }


def _privacy_health(coverage: dict, *, activity_count: int, privacy_count: int, gap_count: int) -> dict:
    partial = int(coverage.get("partial_redacted_count") or 0)
    review_required = int(coverage.get("review_required_count") or 0)
    blocked = max(int(coverage.get("blocked_count") or 0), int(privacy_count or 0))
    active_gaps = int(gap_count or 0)
    coverage_ledger_gaps = int(coverage.get("coverage_ledger_gap_count") or coverage.get("gap_count") or 0)
    gaps = max(active_gaps, coverage_ledger_gaps)
    normal = max(0, int(activity_count or 0) - partial)
    other_gap = max(0, gaps - review_required)
    summary = (
        f"今天保留 {activity_count} 条安全活动，其中 {partial} 条做了局部清洗；"
        f"整帧保护 {blocked} 条，复核缺口 {review_required} 条，当前缺口 {active_gaps} 条，"
        f"覆盖账本缺口 {coverage_ledger_gaps} 次，其他缺口 {other_gap} 条。"
    )
    return {
        "time_range": coverage.get("time_range") or {},
        "clear_count": int(activity_count or 0),
        "normal_clear_count": normal,
        "partial_redacted_count": partial,
        "blocked_count": blocked,
        "review_required_count": review_required,
        "gap_count": gaps,
        "active_gap_count": active_gaps,
        "coverage_ledger_gap_count": coverage_ledger_gaps,
        "other_gap_count": other_gap,
        "summary": summary,
        "explanations": [
            "clear：已通过本地隐私过滤，可进入时间线和搜索。",
            "partial_redacted：只保存清洗后的安全摘要，被清洗原文不入库。",
            "blocked：整帧保护，只留下不含内容的隐私事件。",
            "review_required：不确定片段在 P0 阶段转成不含内容的缺口。",
        ],
    }


def _structured_signal_health(conn, root: Path, start: str, end: str) -> dict:
    doc_row = conn.execute(
        """
        SELECT
          COUNT(*) AS total_count,
          SUM(CASE WHEN COALESCE(semantic_match, 1) = 1 THEN 1 ELSE 0 END) AS semantic_count,
          SUM(CASE WHEN COALESCE(semantic_match, 1) = 0 OR match_status = 'metadata_only' THEN 1 ELSE 0 END) AS metadata_count
        FROM document_signals
        WHERE datetime(captured_at) >= datetime(?)
          AND datetime(captured_at) < datetime(?)
        """,
        (start, end),
    ).fetchone()
    ax_count = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM frame_signals
        WHERE datetime(captured_at) >= datetime(?)
          AND datetime(captured_at) < datetime(?)
          AND privacy_flags LIKE '%accessibility_text_used%'
        """,
        (start, end),
    ).fetchone()["count"]
    browser_row = conn.execute(
        """
        SELECT
          COUNT(*) AS total_count,
          SUM(CASE WHEN url_contexts LIKE '%chromium_extension_native_host%' THEN 1 ELSE 0 END) AS extension_count,
          SUM(CASE
            WHEN url_contexts LIKE '%applescript_dia%'
              OR url_contexts LIKE '%applescript_chrome%'
              OR url_contexts LIKE '%applescript_arc%'
              OR url_contexts LIKE '%applescript_safari%'
            THEN 1 ELSE 0 END) AS applescript_count
        FROM display_frame_contexts
        WHERE datetime(captured_at) >= datetime(?)
          AND datetime(captured_at) < datetime(?)
          AND (
            url_contexts LIKE '%chromium_extension_native_host%'
            OR url_contexts LIKE '%applescript_dia%'
            OR url_contexts LIKE '%applescript_chrome%'
            OR url_contexts LIKE '%applescript_arc%'
            OR url_contexts LIKE '%applescript_safari%'
          )
        """,
        (start, end),
    ).fetchone()
    latest_browser = read_latest_browser_signal(root)
    latest_extension_signal_active = any(
        str(signal.get("source") or "") == "chromium_extension_native_host"
        for signal in list_recent_browser_signals(root)
    )
    browser_count = int(browser_row["total_count"] or 0)
    extension_count = int(browser_row["extension_count"] or 0)
    applescript_count = int(browser_row["applescript_count"] or 0)
    doc_count = int(doc_row["total_count"] or 0)
    doc_semantic_count = int(doc_row["semantic_count"] or 0)
    doc_metadata_count = int(doc_row["metadata_count"] or 0)
    document_diagnostic = _document_signal_diagnostic(
        total_count=doc_count,
        semantic_count=doc_semantic_count,
        metadata_count=doc_metadata_count,
        accessibility_count=int(ax_count or 0),
        browser_count=browser_count,
    )
    host_manifests = {
        key: {
            "installed": (path / f"{NATIVE_HOST_NAME}.json").exists(),
            "manifest": str(path / f"{NATIVE_HOST_NAME}.json"),
            "origin_configured": _browser_host_origin_configured(path / f"{NATIVE_HOST_NAME}.json"),
        }
        for key, path in CHROMIUM_HOST_DIRS.items()
    }
    summary = (
        f"结构化信号：扩展 {extension_count} 条，浏览器兜底 {applescript_count} 条，"
        f"Accessibility {int(ax_count or 0)} 条，文档匹配 {doc_semantic_count} 条，"
        f"文档仅元数据 {doc_metadata_count} 条。"
    )
    return {
        "browser_signal_count": browser_count,
        "extension_browser_signal_count": extension_count,
        "applescript_browser_signal_count": applescript_count,
        "accessibility_signal_count": int(ax_count or 0),
        "document_signal_count": doc_count,
        "document_candidate_count": doc_count,
        "document_match_count": doc_semantic_count,
        "document_semantic_signal_count": doc_semantic_count,
        "document_metadata_only_count": doc_metadata_count,
        "document_signal_status": document_diagnostic["status"],
        "document_signal_reason": document_diagnostic["reason"],
        "document_signal_diagnostic": document_diagnostic,
        "latest_browser_signal_active": bool(latest_browser),
        "latest_extension_signal_active": latest_extension_signal_active,
        "browser_native_hosts": host_manifests,
        "summary": summary,
    }


def _quality_health(conn, start: str, end: str) -> dict:
    activity = conn.execute(
        """
        SELECT
          COUNT(*) AS activity_count,
          SUM(CASE WHEN privacy_level = 'partial_redacted' THEN 1 ELSE 0 END) AS partial_redacted_count,
          SUM(CASE WHEN LENGTH(COALESCE(activity_summary, '')) < 12 THEN 1 ELSE 0 END) AS short_count,
          SUM(CASE WHEN LENGTH(COALESCE(activity_summary, '')) >= 900 THEN 1 ELSE 0 END) AS near_cap_count,
          SUM(CASE
            WHEN privacy_level IN ('normal', 'partial_redacted')
             AND dedupe_status != 'duplicate_exact'
             AND LENGTH(COALESCE(activity_summary, '')) >= 80
            THEN 1 ELSE 0 END) AS candidate_like_count
        FROM extracted_activities
        WHERE deleted_at IS NULL
          AND datetime(started_at) >= datetime(?)
          AND datetime(COALESCE(ended_at, started_at)) < datetime(?)
        """,
        (start, end),
    ).fetchone()
    privacy_count = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM privacy_events
        WHERE datetime(started_at) >= datetime(?)
          AND datetime(COALESCE(ended_at, started_at)) < datetime(?)
        """,
        (start, end),
    ).fetchone()["count"]
    gap_count = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM gap_events
        WHERE datetime(started_at) >= datetime(?)
          AND datetime(COALESCE(ended_at, started_at)) < datetime(?)
        """,
        (start, end),
    ).fetchone()["count"]
    doc = conn.execute(
        """
        SELECT
          COUNT(*) AS candidate_count,
          SUM(CASE WHEN COALESCE(semantic_match, 1) = 1 THEN 1 ELSE 0 END) AS match_count,
          SUM(CASE WHEN COALESCE(semantic_match, 1) = 0 OR match_status = 'metadata_only' THEN 1 ELSE 0 END) AS metadata_count
        FROM document_signals
        WHERE datetime(captured_at) >= datetime(?)
          AND datetime(captured_at) < datetime(?)
        """,
        (start, end),
    ).fetchone()
    attribution = _attribution_health_for_status(conn, start, end)
    feedback_count = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM quality_feedback
        WHERE datetime(COALESCE(target_ended_at, target_started_at, created_at)) > datetime(?)
          AND datetime(COALESCE(target_started_at, target_ended_at, created_at)) < datetime(?)
        """,
        (start, end),
    ).fetchone()["count"]
    latest_ha = conn.execute(
        """
        SELECT candidate_count, eligible_candidate_count, review_only_candidate_count,
               ephemeral_candidate_count, memories_written_count,
               written_candidate_count, skipped_candidate_count,
               skipped_reasons, write_audit_summary,
               single_evidence_eligible_count, subject_risk_candidate_count,
               single_evidence_demoted_count, media_or_file_listing_ephemeral_count
        FROM ha_automation_jobs
        ORDER BY
          datetime(COALESCE(NULLIF(completed_at, ''), NULLIF(triggered_at, ''), NULLIF(generated_at, ''), NULLIF(updated_at, ''))) DESC,
          datetime(updated_at) DESC,
          rowid DESC
        LIMIT 1
        """
    ).fetchone()
    memory_governance_trend = _memory_governance_trend(conn)

    activity_count = int(activity["activity_count"] or 0)
    partial_count = int(activity["partial_redacted_count"] or 0)
    privacy_count = int(privacy_count or 0)
    gap_count = int(gap_count or 0)
    doc_candidate_count = int(doc["candidate_count"] or 0)
    doc_match_count = int(doc["match_count"] or 0)
    memory_candidate_count = min(40, int(activity["candidate_like_count"] or 0))
    if latest_ha:
        total_candidate_count = int(latest_ha["candidate_count"] or 0)
        eligible_candidate_count = int(latest_ha["eligible_candidate_count"] or 0)
        review_only_candidate_count = int(latest_ha["review_only_candidate_count"] or 0)
        ephemeral_candidate_count = int(latest_ha["ephemeral_candidate_count"] or 0)
        single_evidence_eligible_count = int(latest_ha["single_evidence_eligible_count"] or 0)
        subject_risk_candidate_count = int(latest_ha["subject_risk_candidate_count"] or 0)
        latest_memories_written_count = int(latest_ha["memories_written_count"] or 0)
        latest_written_candidate_count = int(latest_ha["written_candidate_count"] or latest_memories_written_count)
        latest_skipped_candidate_count = int(latest_ha["skipped_candidate_count"] or 0)
        latest_skipped_reasons = _decode_status_list(latest_ha["skipped_reasons"] or "")
        latest_write_audit_summary = str(latest_ha["write_audit_summary"] or "")
        single_evidence_demoted_count = int(latest_ha["single_evidence_demoted_count"] or 0)
        media_or_file_listing_ephemeral_count = int(latest_ha["media_or_file_listing_ephemeral_count"] or 0)
    else:
        total_candidate_count = memory_candidate_count
        eligible_candidate_count = 0
        review_only_candidate_count = 0
        ephemeral_candidate_count = 0
        single_evidence_eligible_count = 0
        subject_risk_candidate_count = 0
        latest_memories_written_count = 0
        latest_written_candidate_count = 0
        latest_skipped_candidate_count = 0
        latest_skipped_reasons = []
        latest_write_audit_summary = ""
        single_evidence_demoted_count = 0
        media_or_file_listing_ephemeral_count = 0
    memory_write_health = {
        "total_candidate_count": total_candidate_count,
        "eligible_candidate_count": eligible_candidate_count,
        "review_only_candidate_count": review_only_candidate_count,
        "ephemeral_candidate_count": ephemeral_candidate_count,
        "write_input_count": eligible_candidate_count,
        "single_evidence_eligible_count": single_evidence_eligible_count,
        "subject_risk_candidate_count": subject_risk_candidate_count,
        "latest_memories_written_count": latest_memories_written_count,
        "latest_written_candidate_count": latest_written_candidate_count,
        "latest_skipped_candidate_count": latest_skipped_candidate_count,
        "latest_skipped_reasons": latest_skipped_reasons,
        "latest_write_audit_summary": latest_write_audit_summary,
        "single_evidence_demoted_count": single_evidence_demoted_count,
        "media_or_file_listing_ephemeral_count": media_or_file_listing_ephemeral_count,
        "write_guard_summary": (
            f"元数据/清洗摘要级诊断：单证据可写 {single_evidence_eligible_count} 条；"
            f"主体风险候选 {subject_risk_candidate_count} 条；third_party_subject / ambiguous_subject 不进入 Hermes 写入输入。"
        ),
        "summary": (
            f"最近 HA 长期记忆：全量候选池 {total_candidate_count} 条，可写池 {eligible_candidate_count} 条，"
            f"实际写入输入 {eligible_candidate_count} 条，仅观察 {review_only_candidate_count} 条，临时降级 {ephemeral_candidate_count} 条，"
            f"单证据可写 {single_evidence_eligible_count} 条，主体风险 {subject_risk_candidate_count} 条，"
            f"Hermes 写入 {latest_memories_written_count} 条，跳过 {latest_skipped_candidate_count} 条。"
        ),
    }
    memory_candidate_skip_reason = ""
    if memory_candidate_count == 0:
        memory_candidate_skip_reason = "no_useful_learning_items" if activity_count == 0 else "no_long_term_memory_candidates"
    status = "ok"
    if int(feedback_count or 0) > 0:
        status = "needs_review"
    elif gap_count > 0 or doc_match_count == 0 or attribution.get("status") == "watch":
        status = "watch"
    recommendations: list[str] = []
    if int(feedback_count or 0) > 0:
        recommendations.append("优先查看人工反馈，确认是否需要调整隐私规则、文档匹配或工作流分类。")
    if doc_match_count == 0:
        recommendations.append("DocumentSignal 没有稳定语义匹配，建议检查前台文档权限、窗口标题和允许根目录。")
    if int(activity["short_count"] or 0) > 0:
        recommendations.append("存在极短清洗内容，可能来自 OCR 弱信号、过度清洗或重复压缩。")
    return {
        "status": status,
        "summary": (
            f"活动 {activity_count} 条，局部清洗 {partial_count} 条，隐私保护 {privacy_count} 条，"
            f"缺口 {gap_count} 条；"
            f"DocumentSignal 匹配 {doc_match_count} 条；展示候选约 {memory_candidate_count} 条，全量候选池 {total_candidate_count} 条，"
            f"人工反馈 {int(feedback_count or 0)} 条。"
        ),
        "memory_candidate_count": memory_candidate_count,
        "display_candidate_count": memory_candidate_count,
        "total_candidate_count": total_candidate_count,
        "memory_candidate_skip_reason": memory_candidate_skip_reason,
        "memory_write_health": memory_write_health,
        "memory_governance_trend": memory_governance_trend,
        "attribution_health": attribution,
        "eligible_candidate_count": eligible_candidate_count,
        "review_only_candidate_count": review_only_candidate_count,
        "ephemeral_candidate_count": ephemeral_candidate_count,
        "single_evidence_eligible_count": single_evidence_eligible_count,
        "subject_risk_candidate_count": subject_risk_candidate_count,
        "latest_memories_written_count": latest_memories_written_count,
        "latest_written_candidate_count": latest_written_candidate_count,
        "latest_skipped_candidate_count": latest_skipped_candidate_count,
        "latest_skipped_reasons": latest_skipped_reasons,
        "latest_write_audit_summary": latest_write_audit_summary,
        "single_evidence_demoted_count": single_evidence_demoted_count,
        "media_or_file_listing_ephemeral_count": media_or_file_listing_ephemeral_count,
        "feedback_count": int(feedback_count or 0),
        "document_candidate_count": doc_candidate_count,
        "document_match_count": doc_match_count,
        "recommendations": recommendations,
    }


def _attribution_health_for_status(conn, start: str, end: str) -> dict:
    return attribution_health_for_range(conn, start, end)

    signal_row = conn.execute(
        """
        SELECT
          COUNT(*) AS frame_count,
          SUM(CASE WHEN COALESCE(app_name, '') = '' OR LOWER(app_name) = 'unknown' THEN 1 ELSE 0 END) AS unknown_app_count,
          SUM(CASE WHEN COALESCE(domain, '') != '' THEN 1 ELSE 0 END) AS domain_attributed_count,
          SUM(CASE WHEN COALESCE(display_id, '') NOT IN ('', 'main') THEN 1 ELSE 0 END) AS secondary_display_count,
          SUM(CASE WHEN privacy_flags LIKE '%accessibility_text_used%' THEN 1 ELSE 0 END) AS accessibility_count
        FROM frame_signals
        WHERE datetime(captured_at) >= datetime(?)
          AND datetime(captured_at) < datetime(?)
        """,
        (start, end),
    ).fetchone()
    context_row = conn.execute(
        """
        SELECT
          COUNT(*) AS context_count,
          SUM(CASE WHEN COALESCE(context_confidence, 0.0) < 0.45 THEN 1 ELSE 0 END) AS low_confidence_count,
          SUM(CASE
            WHEN COALESCE(source, '') = 'browser_extension'
              OR url_contexts LIKE '%chromium_extension_native_host%'
            THEN 1 ELSE 0 END) AS browser_extension_count,
          SUM(CASE
            WHEN url_contexts LIKE '%applescript_dia%'
              OR url_contexts LIKE '%applescript_chrome%'
              OR url_contexts LIKE '%applescript_arc%'
              OR url_contexts LIKE '%applescript_safari%'
              OR COALESCE(source, '') LIKE '%applescript%'
            THEN 1 ELSE 0 END) AS applescript_count
        FROM display_frame_contexts
        WHERE datetime(captured_at) >= datetime(?)
          AND datetime(captured_at) < datetime(?)
        """,
        (start, end),
    ).fetchone()
    frame_count = int(signal_row["frame_count"] or 0)
    unknown_count = int(signal_row["unknown_app_count"] or 0)
    context_count = int(context_row["context_count"] or 0)
    low_confidence = int(context_row["low_confidence_count"] or 0)
    extension_count = int(context_row["browser_extension_count"] or 0)
    applescript_count = int(context_row["applescript_count"] or 0)
    known_app_count = max(0, frame_count - unknown_count)
    known_app_ratio = round(known_app_count / frame_count, 3) if frame_count else 0.0
    low_confidence_ratio = round(low_confidence / context_count, 3) if context_count else 0.0
    if frame_count == 0:
        status = "no_frames"
        confidence = "none"
        summary = "当前范围没有 frame signal；不要据此推断没有活动。"
    elif unknown_count == 0 and low_confidence == 0:
        status = "ok"
        confidence = "high"
        summary = "归因健康正常；未发现 Unknown app 或低可信上下文聚集。"
    elif unknown_count > max(10, frame_count // 3) or low_confidence > max(10, context_count // 3):
        status = "watch"
        confidence = "low"
        summary = "Unknown app 或低可信上下文比例偏高；副屏不会继承主屏上下文。"
    else:
        status = "observe"
        confidence = "medium"
        summary = "存在少量 Unknown app 或低可信上下文，可继续观察。"
    return {
        "status": status,
        "summary": summary,
        "confidence_label": confidence,
        "frame_signal_count": frame_count,
        "known_app_frame_count": known_app_count,
        "unknown_app_frame_count": unknown_count,
        "known_app_ratio": known_app_ratio,
        "domain_attributed_frame_count": int(signal_row["domain_attributed_count"] or 0),
        "secondary_display_frame_count": int(signal_row["secondary_display_count"] or 0),
        "context_count": context_count,
        "low_confidence_context_count": low_confidence,
        "low_confidence_ratio": low_confidence_ratio,
        "extension_browser_signal_count": extension_count,
        "browser_extension_context_count": extension_count,
        "applescript_browser_signal_count": applescript_count,
        "accessibility_signal_count": int(signal_row["accessibility_count"] or 0),
        "fallback_context_count": max(0, context_count - extension_count),
        "cross_display_inheritance": "disabled",
    }


def _decode_status_list(value) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
        if isinstance(parsed, list):
            return [str(item)[:80] for item in parsed if str(item or "").strip()][:12]
    except json.JSONDecodeError:
        pass
    text = str(value or "").strip()
    return [text[:80]] if text else []


def _memory_governance_trend(conn, *, limit: int = 6) -> dict:
    rows = conn.execute(
        """
        SELECT hour_key, status, candidate_count, eligible_candidate_count,
               review_only_candidate_count, ephemeral_candidate_count,
               single_evidence_eligible_count, subject_risk_candidate_count,
               single_evidence_demoted_count, media_or_file_listing_ephemeral_count,
               memories_written_count,
               COALESCE(NULLIF(completed_at, ''), NULLIF(triggered_at, ''), NULLIF(generated_at, ''), NULLIF(updated_at, '')) AS event_at
        FROM ha_automation_jobs
        ORDER BY datetime(event_at) DESC, datetime(updated_at) DESC, rowid DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    totals = {
        "candidate_count": 0,
        "eligible_candidate_count": 0,
        "review_only_candidate_count": 0,
        "ephemeral_candidate_count": 0,
        "single_evidence_eligible_count": 0,
        "subject_risk_candidate_count": 0,
        "single_evidence_demoted_count": 0,
        "media_or_file_listing_ephemeral_count": 0,
        "memories_written_count": 0,
    }
    items: list[dict] = []
    for row in rows:
        item = {
            "hour_key": row["hour_key"] or "",
            "event_at": row["event_at"] or "",
            "status": row["status"] or "unknown",
            "candidate_count": int(row["candidate_count"] or 0),
            "eligible_candidate_count": int(row["eligible_candidate_count"] or 0),
            "review_only_candidate_count": int(row["review_only_candidate_count"] or 0),
            "ephemeral_candidate_count": int(row["ephemeral_candidate_count"] or 0),
            "single_evidence_eligible_count": int(row["single_evidence_eligible_count"] or 0),
            "subject_risk_candidate_count": int(row["subject_risk_candidate_count"] or 0),
            "single_evidence_demoted_count": int(row["single_evidence_demoted_count"] or 0),
            "media_or_file_listing_ephemeral_count": int(row["media_or_file_listing_ephemeral_count"] or 0),
            "memories_written_count": int(row["memories_written_count"] or 0),
        }
        for key in totals:
            totals[key] += int(item[key])
        items.append(item)
    if items:
        summary = (
            f"最近 {len(items)} 次 HA：候选 {totals['candidate_count']}，可写 {totals['eligible_candidate_count']}，"
            f"review-only {totals['review_only_candidate_count']}，ephemeral {totals['ephemeral_candidate_count']}，"
            f"主体风险 {totals['subject_risk_candidate_count']}，单证据降级 {totals['single_evidence_demoted_count']}。"
        )
    else:
        summary = "还没有可用于趋势统计的 HA 记忆治理记录。"
    return {
        "window_job_count": len(items),
        "items": items,
        "totals": totals,
        "latest": items[0] if items else {},
        "summary": summary,
        "raw_access": False,
    }


def _document_signal_diagnostic(
    *,
    total_count: int,
    semantic_count: int,
    metadata_count: int,
    accessibility_count: int,
    browser_count: int,
) -> dict:
    reason_options = [
        "no_candidates",
        "permission_limited",
        "metadata_only",
        "semantic_not_matched",
        "privacy_rule_dropped",
    ]
    if semantic_count > 0:
        return {
            "status": "matched",
            "reason": "semantic_match",
            "reason_options": reason_options,
            "summary": f"DocumentSignal 有 {semantic_count} 条语义匹配，可作为清洗后文档上下文使用。",
        }
    if metadata_count > 0:
        return {
            "status": "metadata_only",
            "reason": "metadata_only",
            "reason_options": reason_options,
            "summary": f"DocumentSignal 只有 {metadata_count} 条元数据；系统没有保存文档正文。",
        }
    if total_count > 0:
        return {
            "status": "no_semantic_match",
            "reason": "semantic_not_matched",
            "reason_options": reason_options,
            "summary": f"DocumentSignal 有 {total_count} 条候选，但没有可确认的语义匹配。",
        }
    if accessibility_count or browser_count:
        return {
            "status": "none",
            "reason": "no_document_candidates_or_semantic_match",
            "reason_options": reason_options,
            "summary": "DocumentSignal 为 0：本时间段有其它结构化信号，但没有可用文档候选；也可能是权限不足、只拿到元数据、语义未匹配或被隐私规则丢弃。",
        }
    return {
        "status": "none",
        "reason": "no_visible_structured_candidates",
        "reason_options": reason_options,
        "summary": "DocumentSignal 为 0：本时间段没有可用文档候选；也可能是权限不足、只拿到元数据、语义未匹配或被隐私规则丢弃。",
    }




def _browser_host_origin_configured(manifest_path: Path) -> bool:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return False
    origins = payload.get("allowed_origins") or []
    return bool(origins and "__EXTENSION_ID__" not in json.dumps(origins))


def _utc_datetime() -> datetime:
    return datetime.now(timezone.utc)
























































def _launch_agent_environment(plist_path: Path | None = None) -> dict:
    plist_path = plist_path or Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_ID}.plist"
    try:
        with plist_path.open("rb") as fh:
            payload = plistlib.load(fh)
    except (OSError, plistlib.InvalidFileException):
        return {}
    env = payload.get("EnvironmentVariables") or {}
    return env if isinstance(env, dict) else {}


def _document_change_startup_reconcile_raw(
    launch_env: dict | None = None,
) -> str | None:
    raw = os.environ.get(DOCUMENT_CHANGE_STARTUP_RECONCILE_ENV_KEY)
    if raw is None:
        existing = launch_env if launch_env is not None else _launch_agent_environment()
        raw = existing.get(DOCUMENT_CHANGE_STARTUP_RECONCILE_ENV_KEY)
    return None if raw is None else str(raw).strip()


def _document_change_startup_reconcile_enabled(
    launch_env: dict | None = None,
) -> bool:
    raw = _document_change_startup_reconcile_raw(launch_env)
    if raw is None:
        return True
    return raw.lower() in {"1", "true", "yes", "on"}




def _privacy_profile_environment(profile: str | None, *, preserve: bool = False, launch_env: dict | None = None) -> dict:
    if profile:
        return {PRIVACY_PROFILE_ENV_KEY: normalize_privacy_profile(profile)}
    if not preserve:
        return {}
    existing = launch_env if launch_env is not None else _launch_agent_environment()
    value = existing.get(PRIVACY_PROFILE_ENV_KEY) or os.environ.get(PRIVACY_PROFILE_ENV_KEY) or ""
    if not str(value or "").strip():
        return {}
    normalized = normalize_privacy_profile(str(value))
    return {PRIVACY_PROFILE_ENV_KEY: normalized}


def _apply_privacy_profile_from_settings(conn) -> str:
    profile = normalize_privacy_profile(_get_setting(conn, "privacy_profile", DEFAULT_PRIVACY_PROFILE))
    os.environ[PRIVACY_PROFILE_ENV_KEY] = profile
    return profile


def _privacy_profile_snapshot(conn) -> dict:
    configured = normalize_privacy_profile(_get_setting(conn, "privacy_profile", DEFAULT_PRIVACY_PROFILE))
    launch = normalize_privacy_profile(str(_launch_agent_environment().get(PRIVACY_PROFILE_ENV_KEY) or ""))
    process = normalize_privacy_profile(os.environ.get(PRIVACY_PROFILE_ENV_KEY))
    return {
        "configured": configured,
        "launch_agent": launch,
        "process": process,
        "active": process if process != DEFAULT_PRIVACY_PROFILE else configured,
        "source_default": DEFAULT_PRIVACY_PROFILE,
        "personal_high_precision": configured == PRIVACY_PROFILE_PERSONAL_HIGH_PRECISION,
        "summary": _privacy_profile_summary(configured),
    }


def _privacy_profile_summary(profile: str) -> str:
    if profile == PRIVACY_PROFILE_PERSONAL_HIGH_PRECISION:
        return "个人高精度：硬隐私继续保护，技术讨论和个人知识工作流里的软敏感词减少误清洗。"
    return "严格隐私：手动降级模式，对软敏感词保持更保守的清洗策略。"


def _positive_int(value, default: int, *, allow_zero: bool = False) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    floor = 0 if allow_zero else 1
    return max(floor, number)


def _reason_label(reason: str | None, *, raw_jobs_pending: int = 0, raw_jobs_processed_legacy: int = 0) -> str:
    raw = str(reason or "")
    lowered = raw.lower()
    if "tcc" in lowered or "permission" in lowered or "denied" in lowered or "权限" in raw or "拒绝" in raw:
        return "权限异常"
    if raw == "vlm_pending_queued":
        if raw_jobs_pending > 0:
            return "历史视觉复核帧已排队，仍有短时原图等待补处理"
        if raw_jobs_processed_legacy > 0:
            return "历史视觉复核帧已完成本地复核"
        return "历史视觉复核帧曾进入本地复核队列"
    labels = {
        "duplicate_frame": "画面重复，已跳过",
        "frame_hash_duplicate": "画面完全重复，已跳过",
        "cleaned_content_duplicate": "清洗后内容完全重复，已跳过",
        "privacy_or_review_gate": "隐私保护或需要复核",
        "capture_error": "采集错误",
        "permission_error": "权限异常",
        "screen_recording_permission": "屏幕录制权限异常",
        "session_state_failed": "锁屏状态读取失败",
        "keyframe_error": "关键帧采集错误",
        "locked": "锁屏期间暂停",
        "service_not_running": "记录服务未运行",
        "vlm_pending_duplicate": "历史视觉复核帧与待处理队列重复，已跳过",
        "vlm_backlog_full": "历史复核队列已达上限，暂停新增排队",
        "vlm_backlog_ttl_expired": "历史复核队列任务超过目标处理时间",
    }
    return labels.get(raw, raw or "未知原因")


def _status_explanation(
    status: str,
    *,
    pause_state: dict,
    locked: bool,
    launch_agent: dict,
    raw_residue: int,
    last_error: str,
    capture_freshness: dict,
) -> str:
    if status == "paused" and not pause_state["active"]:
        return "屏幕采集与 OCR 已关闭；文件变更追踪可独立继续运行。"
    if pause_state["active"]:
        if launch_agent["loaded"] and not launch_agent["running"]:
            return f"{pause_state['label']}；同时记录服务已安装但当前没有运行，可以点击重启记录。"
        if not launch_agent["loaded"] and status != "capturing":
            return f"{pause_state['label']}；同时记录服务未启动，启动后才会继续自动采集。"
        return f"{pause_state['label']}；暂停期间不会保存关键帧、OCR 或摘要。"
    if status == "session_state_unknown":
        return "当前无法确认 Mac 是否处于可采集的本人前台会话；系统已按隐私优先原则停止新采集。"
    if last_error:
        return f"最近一次采集出错：{last_error}"
    if launch_agent["loaded"] and not launch_agent["running"]:
        return "记录服务已经安装，但当前没有运行。可以点击重启记录。"
    if not launch_agent["loaded"] and status != "capturing":
        return "记录服务还没有启动。启动后才会按间隔采集 keyframe。"
    if locked:
        return "Mac 当前锁屏，锁屏期间不会采集；解锁后恢复。"
    if capture_freshness.get("is_stale"):
        return str(capture_freshness.get("summary") or "记录服务在运行，但最近没有新采集。")
    if raw_residue > 0:
        return f"发现 {raw_residue} 个临时原始材料残留，系统会优先清理后再继续采集。"
    return "记录服务正常运行，只保存通过隐私过滤后的活动数据。"


def _attention_items(
    *,
    launch_agent: dict,
    lock: dict,
    raw_residue: int,
    last_error: str,
    capture_paused: bool,
    dashboard_health: dict,
    capture_freshness: dict,
    coverage_health: dict,
    screen_recording_permission: dict,
    confirmation_evidence_health: dict | None = None,
) -> list[dict]:
    items: list[dict] = []
    evidence_snapshot = confirmation_evidence_health or {}
    actionable_evidence = evidence_snapshot.get("actionable")
    if not isinstance(actionable_evidence, dict):
        actionable_evidence = evidence_snapshot
    unavailable_evidence = int(actionable_evidence.get("unavailable_count") or 0)
    if unavailable_evidence:
        items.append(
            {
                "kind": "warning",
                "code": "confirmation_evidence_unavailable",
                "title": "待确认证据不可用",
                "message": (
                    f"有 {unavailable_evidence} 条待确认证据不可用；"
                    "待确认问题数量仍只按问题本身的状态统计。"
                ),
            }
        )
    delete_pending_evidence = int(actionable_evidence.get("delete_pending_count") or 0)
    if delete_pending_evidence:
        items.append(
            {
                "kind": "warning",
                "code": "confirmation_evidence_delete_pending",
                "title": "待确认证据等待清理",
                "message": (
                    f"有 {delete_pending_evidence} 条待确认证据等待清理；"
                    "系统会在启动时重试，不会把它们标记为已成功删除。"
                ),
            }
        )
    permission_state = str(screen_recording_permission.get("state") or "unknown")
    if permission_state == "denied":
        items.append(
            {
                "kind": "danger",
                "code": "screen_recording_permission_denied",
                "title": "屏幕录制权限未授权",
                "message": "记录核心已确认权限被拒绝；新采集保持停止。",
            }
        )
    if last_error and not capture_paused and last_error != "screen_recording_permission_denied":
        items.append(
            {
                "kind": "danger",
                "code": "last_error",
                "title": "最近采集出错",
                "message": last_error,
            }
        )
    if launch_agent["loaded"] and not launch_agent["running"]:
        items.append(
            {
                "kind": "danger",
                "code": "launch_agent_not_running",
                "title": "记录服务异常",
                "message": "LaunchAgent 已加载，但采集进程没有运行。",
            }
        )
    elif not launch_agent["loaded"]:
        items.append(
            {
                "kind": "warning",
                "code": "launch_agent_not_loaded",
                "title": "记录服务未启动",
                "message": "启动记录服务后才会自动采集。",
            }
        )
    if capture_freshness.get("is_stale"):
        items.append(
            {
                "kind": "danger",
                "code": "stale_capture",
                "title": "采集可能停住了",
                "message": str(capture_freshness.get("summary") or "记录服务在运行，但最近没有新采集。"),
            }
        )
    coverage_gap_count = int(coverage_health.get("coverage_ledger_gap_count") or 0)
    affected_displays = [
        item
        for item in (coverage_health.get("displays") or [])
        if isinstance(item, dict) and int(item.get("gap_count") or 0) > 0
    ]
    if coverage_gap_count > 0 or affected_displays:
        items.append(
            {
                "kind": "warning",
                "code": "display_coverage_gap",
                "title": "部分屏幕存在采集缺口",
                "message": f"今天有 {len(affected_displays)} 个屏幕出现过采集缺口，共 {coverage_gap_count} 次。",
            }
        )
    if dashboard_health.get("port_conflict"):
        items.append(
            {
                "kind": "warning",
                "code": "dashboard_port_conflict",
                "title": "开发调试面板端口冲突",
                "message": "开发调试面板已启用，但端口由其他本机进程占用。",
            }
        )
    elif dashboard_health.get("foreign_listener") and not dashboard_health.get("health_in_scope"):
        items.append(
            {
                "kind": "neutral",
                "code": "orphan_dashboard_listener",
                "title": "其他本机服务正在使用调试端口",
                "message": "PAM 开发调试面板未启用；该端口占用不计入 PAM 健康。",
            }
        )

    if raw_residue > 0:
        items.append(
            {
                "kind": "warning",
                "code": "raw_residue",
                "title": "临时原始材料残留",
                "message": f"发现 {raw_residue} 个残留，下一轮会优先清理。",
            }
        )
    if _session_state_kind(lock) == "unknown":
        items.append(
            {
                "kind": "warning",
                "code": "session_state_unknown",
                "title": "会话状态无法确认",
                "message": "系统已停止新采集；只有明确解锁且本人会话在前台时才会恢复。",
            }
        )
    return [_normalize_health_issue(item) for item in items]


def _normalize_health_issue(item: dict) -> dict:
    code = str(item.get("code") or "unknown")
    kind = str(item.get("kind") or "neutral")
    severity = "blocking" if kind == "danger" else ("degraded" if kind == "warning" else "info")
    scope = _health_issue_scope(code)
    remediation = _health_issue_remediation(code)
    return {
        **item,
        "severity": severity,
        "scope": scope,
        "evidence": {
            "code": code,
            "source": "pam_local_health",
            "capture_blocking": severity == "blocking" or code == "session_state_unknown",
        },
        "remediation": remediation,
        "recheck_result": {
            "status": "not_applicable" if severity == "info" else "pending",
            "summary": "提示项不影响 PAM 核心健康。" if severity == "info" else "执行建议后刷新完整状态，系统会自动复验。",
        },
    }


def _health_issue_scope(code: str) -> str:
    if code.startswith("dashboard_") or code == "orphan_dashboard_listener":
        return "optional_dashboard"
    if code in {"raw_residue"}:
        return "privacy"
    if code in {"display_coverage_gap"}:
        return "display_coverage"
    if code.startswith("launch_agent"):
        return "recording_service"
    if code.startswith("screen_recording"):
        return "screen_recording_permission"
    if code == "session_state_unknown":
        return "session_state"
    return "capture"


def _health_issue_remediation(code: str) -> dict:
    actions = {
        "screen_recording_permission_denied": (
            "open_screen_recording_settings",
            "打开系统设置中的屏幕与系统音频录制权限，授权后重启记录服务并刷新。",
            "confirm_required",
        ),
        "session_state_unknown": (
            "refresh_session_state",
            "先刷新完整状态；若连续两轮仍未知，再重启记录服务。不要绕过 fail-closed。",
            "read_only_first",
        ),
        "launch_agent_not_running": (
            "restart_recording_service",
            "重新安装并启动记录服务，保留现有隐私档位。",
            "confirm_required",
        ),
        "launch_agent_not_loaded": (
            "start_recording_service",
            "启动记录服务后刷新状态。",
            "confirm_required",
        ),
        "stale_capture": (
            "refresh_then_restart",
            "先刷新权限和会话证据；仍停滞时重启记录服务。",
            "read_only_first",
        ),
        "display_coverage_gap": (
            "recheck_display_coverage",
            "确认当前显示器数量，下一轮逐屏采集后重新检查覆盖率。",
            "read_only",
        ),
        "dashboard_port_conflict": (
            "disable_or_rebind_dev_dashboard",
            "关闭开发调试面板，或停止占用端口的本机进程后再启用。",
            "confirm_required",
        ),
        "orphan_dashboard_listener": (
            "none",
            "PAM 调试面板未启用，无需处理；该端口不计入核心健康。",
            "none",
        ),
        "raw_residue": (
            "review_raw_cleanup",
            "先查看只读诊断；确认来源后再执行清理，不自动删除。",
            "confirm_required",
        ),
    }
    action_id, summary, kind = actions.get(
        code,
        ("refresh_full_status", "刷新完整状态并根据最新证据复验。", "read_only"),
    )
    return {"action_id": action_id, "kind": kind, "summary": summary}


def _guided_repair_plan(
    issues: list[dict],
    *,
    launch_agent: dict,
    runtime_identity: dict,
    screen_recording_permission: dict,
) -> dict:
    repairs = [
        {
            "issue_code": str(issue.get("code") or "unknown"),
            "severity": str(issue.get("severity") or "info"),
            "scope": str(issue.get("scope") or "capture"),
            "why": str(issue.get("message") or ""),
            "action": issue.get("remediation") or {},
            "recheck": issue.get("recheck_result") or {},
        }
        for issue in issues
        if str(issue.get("severity") or "info") != "info"
    ]
    if not runtime_identity.get("is_application_support_runtime"):
        repairs.append(
            {
                "issue_code": "runtime_identity_unexpected",
                "severity": "degraded",
                "scope": "runtime_identity",
                "why": "当前记录核心不是 Application Support live runtime。",
                "action": {
                    "action_id": "sync_live_runtime",
                    "kind": "confirm_required",
                    "summary": "从同一干净构建同步 live core，再重装 LaunchAgent。",
                },
                "recheck": {"status": "pending", "summary": "复验 App/core 版本和代码指纹。"},
            }
        )
    return {
        "status": "action_required" if repairs else "clear",
        "repair_count": len(repairs),
        "repairs": repairs,
        "evidence": {
            "launch_agent_loaded": bool(launch_agent.get("loaded")),
            "launch_agent_running": bool(launch_agent.get("running")),
            "runtime_path_kind": str(runtime_identity.get("path_kind") or "unknown"),
            "screen_recording_permission": str(screen_recording_permission.get("state") or "unknown"),
        },
        "raw_access": False,
    }


def _start_dashboard_thread(db_path: str | Path) -> dict:
    listener = _dashboard_listener("127.0.0.1", 8765)
    if listener.get("listening"):
        listener_pid = str(listener.get("pid") or "")
        if listener_pid and listener_pid == str(os.getpid()):
            return {"status": "already_running", "port": 8765, "foreign_listener": False}
        return {"status": "port_conflict", "port": 8765, "foreign_listener": True}

    def target() -> None:
        from .server import serve

        try:
            serve(str(db_path), "127.0.0.1", 8765)
        except Exception as exc:
            print(_safe_daemon_failure_code("dashboard", exc), file=sys.stderr)

    thread = threading.Thread(target=target, name="monitor-dashboard", daemon=True)
    thread.start()
    time.sleep(0.2)
    listener = _dashboard_listener("127.0.0.1", 8765)
    listener_pid = str(listener.get("pid") or "")
    if listener.get("listening") and (not listener_pid or listener_pid == str(os.getpid())):
        return {"status": "started", "port": 8765, "foreign_listener": False}
    if listener.get("listening"):
        return {"status": "port_conflict", "port": 8765, "foreign_listener": True}
    return {"status": "start_failed", "port": 8765, "foreign_listener": False}


def _is_port_listening(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


def _capture_block_reason(conn, root: Path, *, ignore_pause: bool = False, suffix: str = "") -> dict | None:
    conn.commit()
    if not expire_capture_lease(conn)["enabled"]:
        _set_state(conn, "daemon_status", "paused")
        _set_state(conn, "last_error", "")
        _set_state(conn, "last_check_at", utc_now())
        conn.commit()
        return {
            "status": f"paused{suffix}",
            "screen_capture_enabled": False,
            "pause_reason": "screen_capture_disabled",
            "captured": 0,
            "skipped": 0,
            "errors": 0,
        }
    lock = session_state(root)
    state = _session_state_kind(lock)
    locked = state == "locked"
    capture_blocked = state != "unlocked"
    observed_at = utc_now()
    record_session_transition_evidence(conn, lock, observed_at=observed_at)
    observe_system_activity_session(conn, locked=capture_blocked, observed_at=observed_at)
    pause_state = _pause_snapshot(conn, locked=locked)
    if not ignore_pause and pause_state["active"]:
        _set_state(conn, "daemon_status", "paused")
        conn.commit()
        return {
            "status": f"paused{suffix}",
            "pause_mode": pause_state["mode"],
            "pause_label": pause_state["label"],
            "captured": 0,
            "skipped": 0,
            "errors": 0,
        }
    if state == "unknown":
        _set_state(conn, "locked", "true")
        _set_state(conn, "session_state", "unknown")
        _set_state(conn, "daemon_status", "session_state_unknown")
        _set_state(conn, "last_error", "session_state_unavailable")
        _set_state(conn, "last_check_at", utc_now())
        conn.commit()
        return {
            "status": f"session_state_unknown{suffix}",
            "captured": 0,
            "skipped": 0,
            "errors": 1,
        }
    if locked:
        _set_state(conn, "locked", "true")
        _set_state(conn, "session_state", "locked")
        _set_state(conn, "daemon_status", "locked")
        _set_state(conn, "last_check_at", utc_now())
        conn.commit()
        return {"status": f"locked{suffix}", "captured": 0, "skipped": 0, "errors": 0}
    _set_state(conn, "locked", "false")
    _set_state(conn, "session_state", "unlocked")
    return None


@contextmanager
def _capture_lock(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / "daemon.lock"
    with lock_path.open("w", encoding="utf-8") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield True
        except BlockingIOError:
            yield False
        finally:
            try:
                fcntl.flock(fh, fcntl.LOCK_UN)
            except OSError:
                pass
