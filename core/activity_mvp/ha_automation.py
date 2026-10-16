from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .document_changes import document_authorization_lock
from .ha_source import (
    _ensure_ha_authorization_lock,
    _get_package_memory_candidates_locked,
    generate_package,
)
from .memory_ledger import memory_ledger_status
from .pipeline import utc_now
from .retention import (
    HERMES_HANDOFF_MAX_RUNTIME_SECONDS,
    retention_handoff_admission,
)
from .storage import init_schema


DEFAULT_HA_AUTO_INTERVAL_MINUTES = 60
DEFAULT_HA_AUTO_TRIGGER_MODE = "direct_hermes"
DEFAULT_HA_AUTO_HERMES_PROFILE = "activity-analysis"
DEFAULT_HA_AUTO_TOOLSETS = "clarify"
DEFAULT_HA_AUTO_TIMEOUT_SECONDS = HERMES_HANDOFF_MAX_RUNTIME_SECONDS
DEFAULT_HA_AUTO_ENABLED = False
DEFAULT_HA_AUTO_MAX_ATTEMPTS = 3
DEFAULT_HA_AUTO_RETRY_BACKOFF_MINUTES = 15
DEFAULT_HA_AUTO_PENDING_STALE_MINUTES = 30
DEFAULT_MEMORY_REPORT_RETRY_BACKOFF_MINUTES = 15
DEFAULT_MEMORY_REPORT_CHANNEL_NAME = "记忆系统"
DEFAULT_FEISHU_NOTIFY_PROFILE = "feishubot"
MAX_MEMORY_REPORT_SUMMARIES = 8

SUCCESS_STATUSES = {"triggered", "processed", "completed"}
RETRYABLE_STATUSES = {"failed", "generated"}
TERMINAL_FAILURE_STATUSES = {"dead_letter"}
HERMES_COMPLETED_STATUSES = {"completed", "ok", "success", "triggered", "analysis_only", "no_action"}
SUPPORTED_TRIGGER_MODES = {DEFAULT_HA_AUTO_TRIGGER_MODE}
SECRET_RE = re.compile(r"(-----BEGIN .*PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{20,}\b)", re.I)
LOCAL_PATH_RE = re.compile(
    r"((?:/(?:Users|private|Volumes|tmp|var|Applications|Library|System|opt|usr|etc)/|~/)[^\s\"'`]+)",
    re.I,
)
HASH_RE = re.compile(r"\b[a-f0-9]{32,128}\b", re.I)
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
PHONE_RE = re.compile(r"(?<!\w)(?:\+\d[\d\s().-]{7,}\d|1[3-9]\d{9})(?!\w)")


def _ha_auto_package_rollout_allowed() -> bool:
    """Production gate for automatic HA packaging.

    The phase-0/phase-1 candidate intentionally has no governed Memory
    Gateway completion path, so automatic packaging remains impossible in
    every schema generation and after every restart.  Authorization-lease
    unit tests patch this narrow gate explicitly; persisted settings alone
    can never enable the production path.
    """
    return False


def ensure_ha_automation_defaults(conn: sqlite3.Connection) -> None:
    init_schema(conn)
    now = utc_now()
    _normalize_legacy_completed_jobs(conn)
    _normalize_unverified_memory_reports(conn, now=now)
    for key, value in {
        "ha_auto_package_enabled": "true" if DEFAULT_HA_AUTO_ENABLED else "false",
        "ha_auto_interval_minutes": str(DEFAULT_HA_AUTO_INTERVAL_MINUTES),
        "ha_auto_trigger_mode": DEFAULT_HA_AUTO_TRIGGER_MODE,
        "ha_auto_hermes_profile": DEFAULT_HA_AUTO_HERMES_PROFILE,
        "ha_auto_max_attempts": str(DEFAULT_HA_AUTO_MAX_ATTEMPTS),
        "ha_auto_retry_backoff_minutes": str(DEFAULT_HA_AUTO_RETRY_BACKOFF_MINUTES),
        "ha_auto_pending_stale_minutes": str(DEFAULT_HA_AUTO_PENDING_STALE_MINUTES),
        "ha_auto_memory_report_channel": os.environ.get("PAM_MEMORY_REPORT_CHANNEL_NAME", DEFAULT_MEMORY_REPORT_CHANNEL_NAME),
        "ha_auto_memory_report_profile": os.environ.get("PAM_MEMORY_REPORT_FEISHU_PROFILE", DEFAULT_FEISHU_NOTIFY_PROFILE),
    }.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value, updated_at) VALUES(?, ?, ?)",
            (key, value, now),
        )
    if not _ha_auto_package_rollout_allowed():
        conn.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES('ha_auto_package_enabled','false',?) "
            "ON CONFLICT(key) DO UPDATE SET value='false',updated_at=excluded.updated_at "
            "WHERE settings.value<>'false'",
            (now,),
        )

    for key, value in {
        "ha_auto_status": "disabled",
        "ha_auto_last_generated_at": "",
        "ha_auto_last_triggered_at": "",
        "ha_auto_last_error": "",
        "ha_auto_last_package_id": "",
        "ha_auto_last_report": "",
    }.items():
        conn.execute(
            "INSERT OR IGNORE INTO daemon_state(key, value, updated_at) VALUES(?, ?, ?)",
            (key, value, now),
        )


def _normalize_legacy_completed_jobs(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        """
        UPDATE ha_automation_jobs
        SET status = 'completed',
            hermes_result_status = COALESCE(NULLIF(hermes_result_status, ''), 'triggered_legacy')
        WHERE status = 'triggered'
          AND COALESCE(completed_at, '') != ''
          AND COALESCE(last_error, '') = ''
        """
    )
    return int(cursor.rowcount or 0)


def _normalize_unverified_memory_reports(conn: sqlite3.Connection, *, now: str) -> int:
    rows = conn.execute(
        """
        SELECT hour_key, hermes_result_json
        FROM ha_automation_jobs
        WHERE memories_written_count > 0
          AND COALESCE(memory_report_status, '') IN ('pending', 'failed')
        """
    ).fetchall()
    blocked = 0
    for row in rows:
        try:
            payload = json.loads(str(row["hermes_result_json"] or "{}"))
        except (json.JSONDecodeError, TypeError):
            payload = {}
        if _verified_mem0_write(payload if isinstance(payload, dict) else {}):
            continue
        conn.execute(
            """
            UPDATE ha_automation_jobs
            SET memory_report_status = 'blocked_unverified',
                memory_report_target = '',
                memory_report_error = 'unverified_write_claim',
                memory_reported_at = '',
                memory_report_attempt_count = 0,
                memory_report_next_retry_at = '',
                updated_at = ?
            WHERE hour_key = ?
            """,
            (now, str(row["hour_key"])),
        )
        blocked += 1
    return blocked


def configure_ha_automation(
    conn: sqlite3.Connection,
    root_dir: str | Path,
    *,
    enabled: bool | None = None,
    interval_minutes: int | None = None,
    trigger_mode: str | None = None,
    hermes_profile: str | None = None,
) -> dict:
    ensure_ha_automation_defaults(conn)
    if enabled is not None:
        effective_enabled = bool(enabled) and _ha_auto_package_rollout_allowed()
        _set_setting(conn, "ha_auto_package_enabled", "true" if effective_enabled else "false")
        _set_state(conn, "ha_auto_status", "ready" if effective_enabled else "disabled")
        _set_state(
            conn,
            "ha_auto_last_error",
            "" if effective_enabled or not enabled else "governed_memory_gateway_not_ready",
        )
    if interval_minutes is not None:
        interval = int(interval_minutes)
        if interval != DEFAULT_HA_AUTO_INTERVAL_MINUTES:
            raise ValueError("HA automation currently supports a 60 minute interval")
        _set_setting(conn, "ha_auto_interval_minutes", str(interval))
    if trigger_mode is not None:
        mode = trigger_mode.strip()
        if mode not in SUPPORTED_TRIGGER_MODES:
            raise ValueError(f"unsupported HA automation trigger mode: {trigger_mode}")
        _set_setting(conn, "ha_auto_trigger_mode", mode)
    if hermes_profile is not None:
        profile = hermes_profile.strip()
        if not profile:
            raise ValueError("HA automation Hermes profile cannot be empty")
        _set_setting(conn, "ha_auto_hermes_profile", profile)
    conn.commit()
    return ha_automation_status(conn, root_dir)


def validate_ha_automation(conn: sqlite3.Connection, root_dir: str | Path, *, include_status: bool = True) -> dict:
    ensure_ha_automation_defaults(conn)
    root = Path(root_dir)
    profile = _get_setting(conn, "ha_auto_hermes_profile", DEFAULT_HA_AUTO_HERMES_PROFILE)
    hermes_bin = _hermes_executable()
    profile_dir = Path.home() / ".hermes" / "profiles" / profile
    env_path = profile_dir / ".env"
    config_path = profile_dir / "config.yaml"
    plugin_path = profile_dir / "plugins" / "mem0_local"
    mem0_cli = Path.home() / "bin" / "mem0"
    report_channel = _get_setting(conn, "ha_auto_memory_report_channel", DEFAULT_MEMORY_REPORT_CHANNEL_NAME)
    report_profile = _get_setting(conn, "ha_auto_memory_report_profile", DEFAULT_FEISHU_NOTIFY_PROFILE)
    report_target = _resolve_memory_report_target(channel_name=report_channel, profile=report_profile)
    env_values = _read_env_values(env_path)
    config_text = _read_text(config_path)
    user_id = env_values.get("MEM0_USER_ID", "")
    checks = {
        "live_runtime": is_application_support_runtime(root),
        "hermes_available": hermes_bin is not None,
        "profile_exists": profile_dir.exists(),
        "mem0_provider_configured": "mem0_local" in config_text and plugin_path.exists(),
        "mem0_user_id_local_owner": user_id == "local-owner",
        "mem0_cli_available": mem0_cli.exists(),
        "messaging_isolated": _profile_messaging_isolated(config_text, env_values),
        "memory_report_target_resolved": bool(report_target.get("ok")),
        "least_privilege_toolset": DEFAULT_HA_AUTO_TOOLSETS == "clarify",
    }
    required = (
        "live_runtime",
        "hermes_available",
        "profile_exists",
        "messaging_isolated",
        "least_privilege_toolset",
    )
    missing = [key for key in required if not checks[key]]
    ready = not missing
    result = {
        "ready": ready,
        "summary": "HA 分析路径依赖正常（无终端、无 memory 写权）。" if ready else "HA 分析路径依赖异常：" + "、".join(missing),
        "profile": profile,
        "hermes": {
            "available": checks["hermes_available"],
            "executable": Path(hermes_bin).name if hermes_bin else "",
        },
        "runtime": {
            "allowed": checks["live_runtime"],
            "reason": "application_support_live_runtime" if checks["live_runtime"] else "repo_local_guard",
        },
        "mem0": {
            "provider": "mem0_local" if checks["mem0_provider_configured"] else "",
            "provider_configured": checks["mem0_provider_configured"],
            "user_id": user_id if user_id == "local-owner" else "",
            "user_id_ok": checks["mem0_user_id_local_owner"],
            "cli_available": checks["mem0_cli_available"],
            "write_enabled": False,
        },
        "execution_mode": "analysis_only",
        "messaging_isolated": checks["messaging_isolated"],
        "memory_report": {
            "channel_name": report_channel,
            "profile": report_profile,
            "target_resolved": bool(report_target.get("ok")),
            "target_source": str(report_target.get("source") or ""),
            "reason": _safe_error(str(report_target.get("reason") or "")),
        },
        "checks": checks,
        "missing": missing,
    }
    if include_status:
        result["enabled"] = _ha_auto_package_rollout_allowed() and _bool_setting(
            conn, "ha_auto_package_enabled", DEFAULT_HA_AUTO_ENABLED
        )
        result["trigger_mode"] = _get_setting(conn, "ha_auto_trigger_mode", DEFAULT_HA_AUTO_TRIGGER_MODE)
    return result


def ha_automation_status(conn: sqlite3.Connection, root_dir: str | Path) -> dict:
    ensure_ha_automation_defaults(conn)
    root = Path(root_dir)
    enabled = _ha_auto_package_rollout_allowed() and _bool_setting(
        conn, "ha_auto_package_enabled", DEFAULT_HA_AUTO_ENABLED
    )
    interval = _int_setting(conn, "ha_auto_interval_minutes", DEFAULT_HA_AUTO_INTERVAL_MINUTES)
    trigger_mode = _get_setting(conn, "ha_auto_trigger_mode", DEFAULT_HA_AUTO_TRIGGER_MODE)
    hermes_profile = _get_setting(conn, "ha_auto_hermes_profile", DEFAULT_HA_AUTO_HERMES_PROFILE)
    runtime_allowed = is_application_support_runtime(root)
    active_package_count = _count_active_packages(conn)
    active_auto_package_count = _count_active_auto_packages(conn)
    pending_count = _count_jobs(conn, {"pending", "generated"})
    failed_count = _count_jobs(conn, {"failed"})
    dead_letter_count = _count_jobs(conn, {"dead_letter"})
    dead_letter_resolution = _dead_letter_resolution(conn)
    unresolved_dead_letter_count = int(dead_letter_resolution["unresolved_dead_letter_count"])
    historical_dead_letter_count = int(dead_letter_resolution["historical_dead_letter_count"])
    now_dt = datetime.now(timezone.utc)
    memory_report_pending_count = _count_memory_report_jobs(conn, {"pending"})
    memory_report_failed_count = _count_memory_report_jobs(conn, {"failed"})
    memory_report_due_count = _count_memory_report_retry_due(conn, now_dt)
    retry_due_count = _count_retry_due_jobs(conn, now_dt)
    stale_pending_count = _count_stale_pending_jobs(conn, now_dt)
    latest = _latest_job(conn)
    start, end, hour_key = previous_complete_hour()
    current_job = _job_for_hour(conn, hour_key)
    current_hour_due = current_job is None or _job_retry_due(current_job, now_dt)
    due = bool(enabled and runtime_allowed and (retry_due_count > 0 or current_hour_due or stale_pending_count > 0))
    last_error = _get_state(conn, "ha_auto_last_error", "")
    state_status = _get_state(conn, "ha_auto_status", "disabled" if not enabled else "ready")
    preflight = validate_ha_automation(conn, root, include_status=False)
    latest_summary = _latest_result_summary(latest)
    latest_candidate_count = int(latest.get("candidate_count") or 0) if latest else 0
    latest_candidate_skip_reason = str(latest.get("candidate_skip_reason") or "") if latest else ""
    latest_eligible_candidate_count = int(latest.get("eligible_candidate_count") or 0) if latest else 0
    latest_review_only_candidate_count = int(latest.get("review_only_candidate_count") or 0) if latest else 0
    latest_ephemeral_candidate_count = int(latest.get("ephemeral_candidate_count") or 0) if latest else 0
    latest_confirmed_action_count = int(latest.get("confirmed_action_count") or 0) if latest else 0
    latest_context_clue_count = int(latest.get("context_clue_count") or 0) if latest else 0
    latest_noise_or_reference_count = int(latest.get("noise_or_reference_count") or 0) if latest else 0
    latest_single_evidence_eligible_count = int(latest.get("single_evidence_eligible_count") or 0) if latest else 0
    latest_subject_risk_candidate_count = int(latest.get("subject_risk_candidate_count") or 0) if latest else 0
    latest_single_evidence_demoted_count = int(latest.get("single_evidence_demoted_count") or 0) if latest else 0
    latest_media_or_file_listing_ephemeral_count = int(latest.get("media_or_file_listing_ephemeral_count") or 0) if latest else 0
    latest_memories_written_count = int(latest.get("memories_written_count") or 0) if latest else 0
    latest_written_candidate_count = int(latest.get("written_candidate_count") or latest_memories_written_count) if latest else 0
    latest_skipped_candidate_count = int(latest.get("skipped_candidate_count") or 0) if latest else 0
    latest_written_candidate_types = _decode_audit_list(latest.get("written_candidate_types") if latest else "")
    latest_skipped_reasons = _decode_audit_list(latest.get("skipped_reasons") if latest else "")
    latest_write_audit_summary = str(latest.get("write_audit_summary") or "") if latest else ""
    latest_written_memory_summaries = _decode_memory_summary_list(latest.get("written_memory_summaries") if latest else "")
    latest_memory_report_status = str(latest.get("memory_report_status") or "") if latest else ""
    latest_memory_report_error = str(latest.get("memory_report_error") or "") if latest else ""
    latest_memory_reported_at = str(latest.get("memory_reported_at") or "") if latest else ""
    latest_memory_report_attempt_count = int(latest.get("memory_report_attempt_count") or 0) if latest else 0
    ledger = memory_ledger_status(conn)
    local_window = _local_window(start, end)
    if not enabled:
        label = "未启用"
        kind = "neutral"
        summary = f"未启用；当前 active HA 包 {active_package_count} 个。"
    elif not runtime_allowed:
        label = "未在 live runtime 执行"
        kind = "warning"
        summary = "已配置但当前路径不是 Application Support live runtime，自动生成被保护性跳过。"
    elif not preflight["ready"] and not (
        unresolved_dead_letter_count or failed_count or pending_count or memory_report_failed_count or memory_report_pending_count
    ):
        label = "依赖异常"
        kind = "warning"
        summary = f"已启用；{preflight['summary']}"
    elif unresolved_dead_letter_count:
        label = "有死信"
        kind = "warning"
        summary = f"已启用；未解释死信 {unresolved_dead_letter_count} 个，失败 {failed_count} 个，待处理 {pending_count} 个。"
    elif failed_count:
        label = "有失败待重试"
        kind = "warning"
        summary = f"已启用；失败 {failed_count} 个，待处理 {pending_count} 个，当前可重试 {retry_due_count} 个。"
    elif memory_report_failed_count:
        label = "记忆汇报失败"
        kind = "warning"
        summary = f"已启用；Hermes 写入后飞书记忆系统汇报失败 {memory_report_failed_count} 个，待补投 {memory_report_pending_count} 个。"
    elif memory_report_pending_count:
        label = "记忆汇报待补投"
        kind = "warning"
        summary = f"已启用；Hermes 写入已完成，飞书记忆系统汇报待补投 {memory_report_pending_count} 个，当前可重试 {memory_report_due_count} 个。"
    elif not preflight["ready"]:
        label = "依赖异常"
        kind = "warning"
        summary = f"已启用；{preflight['summary']}"
    elif pending_count:
        label = "处理中"
        kind = "warning"
        stale = f"，疑似卡住 {stale_pending_count} 个" if stale_pending_count else ""
        summary = f"已启用；待处理 {pending_count} 个{stale}，当前 active HA 包 {active_package_count} 个。"
    elif latest and (latest.get("completed_at") or latest.get("triggered_at")):
        label = "正常"
        kind = "ok"
        historical = f"；历史死信 {historical_dead_letter_count} 个已由后续成功任务解释" if historical_dead_letter_count else ""
        summary = f"已启用；最近 Hermes 结果：{latest_summary}{historical}。"
    else:
        label = "等待首次自动包"
        kind = "ok"
        summary = f"已启用；等待下一次完整小时窗口（本地 {local_window['label']}）。"
    return {
        "enabled": enabled,
        "interval_minutes": interval,
        "trigger_mode": trigger_mode,
        "hermes_profile": hermes_profile,
        "max_attempts": _int_setting(conn, "ha_auto_max_attempts", DEFAULT_HA_AUTO_MAX_ATTEMPTS),
        "retry_backoff_minutes": _int_setting(conn, "ha_auto_retry_backoff_minutes", DEFAULT_HA_AUTO_RETRY_BACKOFF_MINUTES),
        "pending_stale_minutes": _int_setting(conn, "ha_auto_pending_stale_minutes", DEFAULT_HA_AUTO_PENDING_STALE_MINUTES),
        "status": state_status,
        "label": label,
        "kind": kind,
        "summary": summary,
        "runtime_allowed": runtime_allowed,
        "runtime_reason": "application_support_live_runtime" if runtime_allowed else "repo_local_guard",
        "preflight": preflight,
        "current_hour_key": hour_key,
        "current_window": {"from": start, "to": end},
        "current_window_local": local_window,
        "current_hour_due": bool(current_hour_due),
        "due": due,
        "last_generated_at": _get_state(conn, "ha_auto_last_generated_at", "") or (latest.get("generated_at") if latest else ""),
        "last_triggered_at": _get_state(conn, "ha_auto_last_triggered_at", "") or (latest.get("triggered_at") if latest else ""),
        "last_package_id": _get_state(conn, "ha_auto_last_package_id", "") or (latest.get("package_id") if latest else ""),
        "last_error": last_error,
        "pending_count": pending_count,
        "failed_count": failed_count,
        "dead_letter_count": dead_letter_count,
        "historical_dead_letter_count": historical_dead_letter_count,
        "unresolved_dead_letter_count": unresolved_dead_letter_count,
        "dead_letter_latest_completed_at": dead_letter_resolution["latest_completed_at"],
        "retry_due_count": retry_due_count,
        "stale_pending_count": stale_pending_count,
        "memory_report_pending_count": memory_report_pending_count,
        "memory_report_failed_count": memory_report_failed_count,
        "memory_report_retry_due_count": memory_report_due_count,
        "active_package_count": active_package_count,
        "active_auto_package_count": active_auto_package_count,
        "latest_result_summary": latest_summary,
        "latest_candidate_count": latest_candidate_count,
        "latest_eligible_candidate_count": latest_eligible_candidate_count,
        "latest_review_only_candidate_count": latest_review_only_candidate_count,
        "latest_ephemeral_candidate_count": latest_ephemeral_candidate_count,
        "latest_confirmed_action_count": latest_confirmed_action_count,
        "latest_context_clue_count": latest_context_clue_count,
        "latest_noise_or_reference_count": latest_noise_or_reference_count,
        "latest_single_evidence_eligible_count": latest_single_evidence_eligible_count,
        "latest_subject_risk_candidate_count": latest_subject_risk_candidate_count,
        "latest_single_evidence_demoted_count": latest_single_evidence_demoted_count,
        "latest_media_or_file_listing_ephemeral_count": latest_media_or_file_listing_ephemeral_count,
        "latest_memories_written_count": latest_memories_written_count,
        "latest_written_candidate_count": latest_written_candidate_count,
        "latest_skipped_candidate_count": latest_skipped_candidate_count,
        "latest_written_candidate_types": latest_written_candidate_types,
        "latest_skipped_reasons": latest_skipped_reasons,
        "latest_write_audit_summary": latest_write_audit_summary,
        "latest_written_memory_summaries": latest_written_memory_summaries,
        "latest_memory_report_status": latest_memory_report_status,
        "latest_memory_report_error": latest_memory_report_error,
        "latest_memory_reported_at": latest_memory_reported_at,
        "latest_memory_report_attempt_count": latest_memory_report_attempt_count,
        "memory_ledger": ledger,
        "latest_candidate_skip_reason": latest_candidate_skip_reason,
        "last_report": _get_state(conn, "ha_auto_last_report", ""),
        "latest_job": latest or {},
    }


def run_ha_automation_once(
    conn: sqlite3.Connection,
    root_dir: str | Path,
    *,
    db_path: str | Path | None = None,
    now: datetime | None = None,
    force: bool = False,
    trigger: bool = True,
) -> dict:
    ensure_ha_automation_defaults(conn)
    root = Path(root_dir)
    if not _ha_auto_package_rollout_allowed() or not _bool_setting(
        conn, "ha_auto_package_enabled", DEFAULT_HA_AUTO_ENABLED
    ):
        _set_state(conn, "ha_auto_status", "disabled")
        _set_state(conn, "ha_auto_last_error", "")
        conn.commit()
        return {"status": "disabled", "generated": False, "triggered": False}
    if not force and not is_application_support_runtime(root):
        _set_state(conn, "ha_auto_status", "skipped")
        _set_state(conn, "ha_auto_last_error", "")
        conn.commit()
        return {
            "status": "skipped",
            "reason": "repo_local_not_live_runtime",
            "generated": False,
            "triggered": False,
        }
    interval = _int_setting(conn, "ha_auto_interval_minutes", DEFAULT_HA_AUTO_INTERVAL_MINUTES)
    if interval != DEFAULT_HA_AUTO_INTERVAL_MINUTES:
        error = "HA automation interval must be 60 minutes"
        _set_state(conn, "ha_auto_status", "error")
        _set_state(conn, "ha_auto_last_error", error)
        conn.commit()
        return {"status": "error", "error": error, "generated": False, "triggered": False}

    now_dt = _utc_datetime(now)
    current_start, current_end, current_hour_key = previous_complete_hour(now)
    profile = _get_setting(conn, "ha_auto_hermes_profile", DEFAULT_HA_AUTO_HERMES_PROFILE)
    trigger_mode = _get_setting(conn, "ha_auto_trigger_mode", DEFAULT_HA_AUTO_TRIGGER_MODE)
    admission = retention_handoff_admission(conn)
    if not admission["allowed"]:
        return _defer_ha_for_retention(
            conn,
            admission,
            hour_key=current_hour_key,
            package_id="",
            activity_count=0,
            safe_event_count=0,
            generated=False,
        )

    _mark_stale_pending_jobs(conn, now_dt)
    _mark_dead_letter_jobs(conn)
    _retry_pending_memory_reports(conn, now_dt)
    conn.commit()

    retry_job = _oldest_retryable_job(conn, now_dt)
    if retry_job:
        job = retry_job
        start = str(job["started_at"])
        end = str(job["ended_at"])
        hour_key = str(job["hour_key"])
    else:
        job = _job_for_hour(conn, current_hour_key)
        start = current_start
        end = current_end
        hour_key = current_hour_key

    if job and str(job["status"]) in SUCCESS_STATUSES:
        return {
            "status": "duplicate",
            "hour_key": hour_key,
            "package_id": job["package_id"],
            "generated": False,
            "triggered": False,
        }
    if job and str(job["status"]) in TERMINAL_FAILURE_STATUSES:
        return {
            "status": str(job["status"]),
            "hour_key": hour_key,
            "package_id": job["package_id"],
            "generated": False,
            "triggered": False,
            "dead_letter_reason": job["dead_letter_reason"] if "dead_letter_reason" in job.keys() else "",
        }
    if trigger_mode not in SUPPORTED_TRIGGER_MODES:
        error = f"unsupported HA automation trigger mode: {trigger_mode}"
        _set_state(conn, "ha_auto_status", "error")
        _set_state(conn, "ha_auto_last_error", error)
        conn.commit()
        return {"status": "error", "error": error, "generated": False, "triggered": False}

    generated = False
    if job and job["package_id"]:
        package_id = str(job["package_id"])
        activity_count = int(job["activity_count"] or 0)
        safe_event_count = int(job["safe_timeline_event_count"] or 0)
        generated_at = str(job["generated_at"] or "")
    else:
        try:
            result = generate_package(
                conn,
                start,
                end,
                _package_output_dir(root),
                write_markdown=False,
                generation_mode="hourly_auto",
            )
        except Exception:
            error = "ha_package_generation_failed"
            _set_state(conn, "ha_auto_status", "error")
            _set_state(conn, "ha_auto_last_error", error)
            conn.commit()
            return {"status": "error", "error": error, "generated": False, "triggered": False}
        package = result["package"]
        package_id = str(package["package_id"])
        activity_count = len(package.get("activities") or [])
        safe_event_count = len(package.get("safe_timeline_events") or [])
        candidate_summary = _package_candidate_summary(package)
        generated_at = utc_now()
        _upsert_job(
            conn,
            hour_key=hour_key,
            start=start,
            end=end,
            package_id=package_id,
            activity_count=activity_count,
            safe_event_count=safe_event_count,
            trigger_mode=trigger_mode,
            hermes_profile=profile,
            status="generated",
            generated_at=generated_at,
            candidate_count=candidate_summary["candidate_count"],
            candidate_skip_reason=candidate_summary["candidate_skip_reason"],
            eligible_candidate_count=candidate_summary["eligible_candidate_count"],
            review_only_candidate_count=candidate_summary["review_only_candidate_count"],
            ephemeral_candidate_count=candidate_summary["ephemeral_candidate_count"],
            confirmed_action_count=candidate_summary["confirmed_action_count"],
            context_clue_count=candidate_summary["context_clue_count"],
            noise_or_reference_count=candidate_summary["noise_or_reference_count"],
            single_evidence_eligible_count=candidate_summary["single_evidence_eligible_count"],
            subject_risk_candidate_count=candidate_summary["subject_risk_candidate_count"],
            single_evidence_demoted_count=candidate_summary["single_evidence_demoted_count"],
            media_or_file_listing_ephemeral_count=candidate_summary["media_or_file_listing_ephemeral_count"],
        )
        _set_state(conn, "ha_auto_last_generated_at", generated_at)
        _set_state(conn, "ha_auto_last_package_id", package_id)
        generated = True
        conn.commit()

    if not trigger:
        _audit_handoff(
            conn,
            package_id=package_id,
            hour_key=hour_key,
            start=start,
            end=end,
            activity_count=activity_count,
            safe_event_count=safe_event_count,
            profile=profile,
            trigger_mode=trigger_mode,
            status="generated",
            handed_to_hermes=False,
            attempt_count=int(job["attempt_count"] or 0) if job else 0,
            failure_reason="trigger_disabled_for_manual_run",
        )
        conn.commit()
        return {
            "status": "generated",
            "hour_key": hour_key,
            "package_id": package_id,
            "activity_count": activity_count,
            "safe_timeline_event_count": safe_event_count,
            "generated": generated,
            "triggered": False,
        }

    job = _job_for_hour(conn, hour_key)
    attempt_count = int(job["attempt_count"] or 0) + 1 if job else 1
    blocked_source_status = ""
    admission = retention_handoff_admission(conn)
    if not admission["allowed"]:
        return _defer_ha_for_retention(
            conn,
            admission,
            hour_key=hour_key,
            package_id=package_id,
            activity_count=activity_count,
            safe_event_count=safe_event_count,
            generated=generated,
        )
    _ensure_ha_authorization_lock(conn)
    with document_authorization_lock(conn, exclusive=False):
        # Recheck after acquiring the lease as well: the caller may have waited
        # behind an exclusive revoker long enough to enter the drain window.
        admission = retention_handoff_admission(conn)
        if not admission["allowed"]:
            return _defer_ha_for_retention(
                conn,
                admission,
                hour_key=hour_key,
                package_id=package_id,
                activity_count=activity_count,
                safe_event_count=safe_event_count,
                generated=generated,
            )
        # Re-read the package after acquiring the transport lease.  The public
        # reader would take a nested flock and could release the outer lease on
        # some platforms, so use its lock-private helper directly.
        analysis_payload = _get_package_memory_candidates_locked(
            conn,
            package_id,
            max_items=40,
        )
        source_status = str(analysis_payload.get("source_status") or "unknown")
        if source_status != "active":
            blocked_source_status = source_status
            hermes_result = _hermes_run_result(
                "failed",
                "ha_source_unavailable",
                hermes_result_status="not_started",
                skipped_reason="source_not_active",
                hermes_result_json={
                    "status": "not_started",
                    "source_status": source_status,
                    "raw_access": False,
                },
            )
        else:
            command = build_hermes_trigger_command(
                package_id=package_id,
                start=start,
                end=end,
                hour_key=hour_key,
                hermes_profile=profile,
                root_dir=root,
                db_path=db_path or _default_db_path(root),
                analysis_payload=analysis_payload,
            )
            now_iso = utc_now()
            conn.execute(
                """
                UPDATE ha_automation_jobs
                SET status = ?, attempt_count = ?, last_error = ?, command = ?,
                    triggered_at = ?, processing_started_at = ?, next_retry_at = ?,
                    dead_letter_reason = ?, updated_at = ?
                WHERE hour_key = ?
                """,
                (
                    "pending",
                    attempt_count,
                    "",
                    json.dumps(_command_descriptor(command, package_id, profile), ensure_ascii=False),
                    now_iso,
                    now_iso,
                    "",
                    "",
                    now_iso,
                    hour_key,
                ),
            )
            _set_state(conn, "ha_auto_status", "pending")
            conn.commit()
            # Hold the same shared lease until Hermes has consumed the inline
            # payload and returned.  An exclusive delete/reclassification that
            # loses this race must wait for the in-flight handoff to finish.
            hermes_result = _run_hermes_command(command, cwd=_core_root(root))
    completed_at = utc_now()
    result_status = hermes_result["status"]
    last_error = hermes_result["last_error"]
    handed_to_hermes = result_status in SUCCESS_STATUSES
    dead_letter_reason = ""
    next_retry_at = ""
    if blocked_source_status:
        result_status = "dead_letter"
        handed_to_hermes = False
        dead_letter_reason = "ha_source_unavailable"
    elif not handed_to_hermes:
        max_attempts = _int_setting(conn, "ha_auto_max_attempts", DEFAULT_HA_AUTO_MAX_ATTEMPTS)
        if attempt_count >= max_attempts:
            result_status = "dead_letter"
            handed_to_hermes = False
            dead_letter_reason = last_error or f"failed after {attempt_count} attempts"
        else:
            backoff_minutes = _int_setting(conn, "ha_auto_retry_backoff_minutes", DEFAULT_HA_AUTO_RETRY_BACKOFF_MINUTES)
            next_retry_at = _iso_after_minutes(completed_at, backoff_minutes * max(1, attempt_count))
    conn.execute(
        """
        UPDATE ha_automation_jobs
        SET status = ?, attempt_count = ?, last_error = ?, completed_at = ?, next_retry_at = ?,
            dead_letter_reason = ?, hermes_result_status = ?,
            memories_written_count = ?, skipped_reason = ?,
            hermes_result_json = ?, written_candidate_count = ?,
            skipped_candidate_count = ?, written_candidate_types = ?,
            skipped_reasons = ?, write_audit_summary = ?,
            written_memory_summaries = ?, updated_at = ?
        WHERE hour_key = ?
        """,
        (
            result_status,
            attempt_count,
            last_error,
            completed_at,
            next_retry_at,
            dead_letter_reason,
            hermes_result["hermes_result_status"],
            hermes_result["memories_written_count"],
            hermes_result["skipped_reason"],
            hermes_result["hermes_result_json"],
            hermes_result["written_candidate_count"],
            hermes_result["skipped_candidate_count"],
            hermes_result["written_candidate_types"],
            hermes_result["skipped_reasons"],
            hermes_result["write_audit_summary"],
            hermes_result["written_memory_summaries"],
            completed_at,
            hour_key,
        ),
    )
    memory_report = _attempt_memory_write_report(
        conn,
        hour_key=hour_key,
        package_id=package_id,
        hour_start=start,
        hour_end=end,
        hermes_profile=profile,
        hermes_result=hermes_result,
        now_iso=completed_at,
    )
    memory_ledger = _run_memory_ledger_maintenance(conn, root)
    _set_state(conn, "ha_auto_status", "ok" if handed_to_hermes else result_status)
    _set_state(conn, "ha_auto_last_triggered_at", completed_at if handed_to_hermes else "")
    _set_state(conn, "ha_auto_last_error", last_error)
    _set_state(conn, "ha_auto_last_package_id", package_id)
    report = {
        "hour_key": hour_key,
        "package_id": package_id,
        "activity_count": activity_count,
        "safe_timeline_event_count": safe_event_count,
        "hermes_profile": profile,
        "trigger_status": result_status,
        "handed_to_hermes": handed_to_hermes,
        "attempt_count": attempt_count,
        "hermes_result_status": hermes_result["hermes_result_status"],
        "memories_written_count": hermes_result["memories_written_count"],
        "skipped_reason": hermes_result["skipped_reason"],
        "written_candidate_count": hermes_result["written_candidate_count"],
        "skipped_candidate_count": hermes_result["skipped_candidate_count"],
        "written_candidate_types": _decode_audit_list(hermes_result["written_candidate_types"]),
        "skipped_reasons": _decode_audit_list(hermes_result["skipped_reasons"]),
        "write_audit_summary": hermes_result["write_audit_summary"],
        "written_memory_summaries": _decode_memory_summary_list(hermes_result["written_memory_summaries"]),
        "memory_report": memory_report,
        "memory_ledger": memory_ledger,
        "candidate_count": int(job["candidate_count"] or 0) if job and "candidate_count" in job.keys() else 0,
        "eligible_candidate_count": int(job["eligible_candidate_count"] or 0) if job and "eligible_candidate_count" in job.keys() else 0,
        "review_only_candidate_count": int(job["review_only_candidate_count"] or 0) if job and "review_only_candidate_count" in job.keys() else 0,
        "ephemeral_candidate_count": int(job["ephemeral_candidate_count"] or 0) if job and "ephemeral_candidate_count" in job.keys() else 0,
        "confirmed_action_count": int(job["confirmed_action_count"] or 0) if job and "confirmed_action_count" in job.keys() else 0,
        "context_clue_count": int(job["context_clue_count"] or 0) if job and "context_clue_count" in job.keys() else 0,
        "noise_or_reference_count": int(job["noise_or_reference_count"] or 0) if job and "noise_or_reference_count" in job.keys() else 0,
        "single_evidence_eligible_count": int(job["single_evidence_eligible_count"] or 0) if job and "single_evidence_eligible_count" in job.keys() else 0,
        "subject_risk_candidate_count": int(job["subject_risk_candidate_count"] or 0) if job and "subject_risk_candidate_count" in job.keys() else 0,
        "single_evidence_demoted_count": int(job["single_evidence_demoted_count"] or 0) if job and "single_evidence_demoted_count" in job.keys() else 0,
        "media_or_file_listing_ephemeral_count": int(job["media_or_file_listing_ephemeral_count"] or 0) if job and "media_or_file_listing_ephemeral_count" in job.keys() else 0,
        "candidate_skip_reason": str(job["candidate_skip_reason"] or "") if job and "candidate_skip_reason" in job.keys() else "",
        "next_retry_at": next_retry_at,
        "dead_letter_reason": dead_letter_reason,
    }
    _set_state(conn, "ha_auto_last_report", json.dumps(report, ensure_ascii=False))
    _audit_handoff(
        conn,
        package_id=package_id,
        hour_key=hour_key,
        start=start,
        end=end,
        activity_count=activity_count,
        safe_event_count=safe_event_count,
        profile=profile,
        trigger_mode=trigger_mode,
        status=result_status,
        handed_to_hermes=handed_to_hermes,
        attempt_count=attempt_count,
        failure_reason=last_error,
        hermes_result=hermes_result,
        memory_report=memory_report,
        next_retry_at=next_retry_at,
        dead_letter_reason=dead_letter_reason,
    )
    conn.commit()
    response = {
        "status": result_status,
        "hour_key": hour_key,
        "package_id": package_id,
        "activity_count": activity_count,
        "safe_timeline_event_count": safe_event_count,
        "generated": generated,
        "triggered": handed_to_hermes,
        "attempt_count": attempt_count,
        "last_error": last_error,
        "hermes_result_status": hermes_result["hermes_result_status"],
        "memories_written_count": hermes_result["memories_written_count"],
        "skipped_reason": hermes_result["skipped_reason"],
        "written_candidate_count": hermes_result["written_candidate_count"],
        "skipped_candidate_count": hermes_result["skipped_candidate_count"],
        "written_candidate_types": _decode_audit_list(hermes_result["written_candidate_types"]),
        "skipped_reasons": _decode_audit_list(hermes_result["skipped_reasons"]),
        "write_audit_summary": hermes_result["write_audit_summary"],
        "written_memory_summaries": _decode_memory_summary_list(hermes_result["written_memory_summaries"]),
        "memory_report": memory_report,
        "memory_ledger": memory_ledger,
        "candidate_count": int(job["candidate_count"] or 0) if job and "candidate_count" in job.keys() else 0,
        "eligible_candidate_count": int(job["eligible_candidate_count"] or 0) if job and "eligible_candidate_count" in job.keys() else 0,
        "review_only_candidate_count": int(job["review_only_candidate_count"] or 0) if job and "review_only_candidate_count" in job.keys() else 0,
        "ephemeral_candidate_count": int(job["ephemeral_candidate_count"] or 0) if job and "ephemeral_candidate_count" in job.keys() else 0,
        "confirmed_action_count": int(job["confirmed_action_count"] or 0) if job and "confirmed_action_count" in job.keys() else 0,
        "context_clue_count": int(job["context_clue_count"] or 0) if job and "context_clue_count" in job.keys() else 0,
        "noise_or_reference_count": int(job["noise_or_reference_count"] or 0) if job and "noise_or_reference_count" in job.keys() else 0,
        "single_evidence_eligible_count": int(job["single_evidence_eligible_count"] or 0) if job and "single_evidence_eligible_count" in job.keys() else 0,
        "subject_risk_candidate_count": int(job["subject_risk_candidate_count"] or 0) if job and "subject_risk_candidate_count" in job.keys() else 0,
        "single_evidence_demoted_count": int(job["single_evidence_demoted_count"] or 0) if job and "single_evidence_demoted_count" in job.keys() else 0,
        "media_or_file_listing_ephemeral_count": int(job["media_or_file_listing_ephemeral_count"] or 0) if job and "media_or_file_listing_ephemeral_count" in job.keys() else 0,
        "candidate_skip_reason": str(job["candidate_skip_reason"] or "") if job and "candidate_skip_reason" in job.keys() else "",
        "next_retry_at": next_retry_at,
        "dead_letter_reason": dead_letter_reason,
    }
    if blocked_source_status:
        response.update(
            {
                "source_status": blocked_source_status,
                "raw_access": False,
            }
        )
    return response


def _defer_ha_for_retention(
    conn: sqlite3.Connection,
    admission: dict,
    *,
    hour_key: str,
    package_id: str,
    activity_count: int,
    safe_event_count: int,
    generated: bool,
) -> dict:
    _set_state(conn, "ha_auto_status", "waiting_for_retention")
    _set_state(conn, "ha_auto_last_error", "")
    conn.commit()
    return {
        "status": "deferred",
        "reason": str(admission.get("reason") or "retention_deadline_imminent"),
        "hour_key": hour_key,
        "package_id": package_id,
        "activity_count": int(activity_count),
        "safe_timeline_event_count": int(safe_event_count),
        "generated": bool(generated),
        "triggered": False,
        "retention_deadline_at": str(admission.get("deadline_at") or ""),
        "retention_remaining_seconds": admission.get("remaining_seconds"),
        "raw_access": False,
    }


def _run_memory_ledger_maintenance(conn: sqlite3.Connection, root: Path) -> dict:
    ledger = memory_ledger_status(conn)
    return {
        "status": ledger.get("status") or "frozen_legacy",
        "read_only": True,
        "write_enabled": False,
        "entry_count": int(ledger.get("entry_count") or 0),
        "raw_access": False,
        "destructive_actions": False,
    }


def build_hermes_trigger_command(
    *,
    package_id: str,
    start: str,
    end: str,
    hour_key: str,
    hermes_profile: str,
    root_dir: str | Path,
    db_path: str | Path,
    hermes_bin: str | Path | None = None,
    toolsets: str = DEFAULT_HA_AUTO_TOOLSETS,
    analysis_payload: dict | None = None,
) -> list[str]:
    if str(toolsets or "").strip() != DEFAULT_HA_AUTO_TOOLSETS:
        raise ValueError("HA analysis only permits the clarify toolset")
    executable = Path(
        hermes_bin
        or os.environ.get("PAM_HERMES_BIN")
        or (Path.home() / "bin" / "hermes")
    )
    prompt = _hermes_prompt(
        package_id=package_id,
        start=start,
        end=end,
        hour_key=hour_key,
        hermes_profile=hermes_profile,
        analysis_payload=analysis_payload or {},
    )
    return [
        str(executable),
        "--profile",
        hermes_profile,
        "--ignore-rules",
        "-t",
        DEFAULT_HA_AUTO_TOOLSETS,
        "-z",
        prompt,
    ]


def previous_complete_hour(now: datetime | None = None) -> tuple[str, str, str]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    end = current.replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=1)
    start_iso = start.isoformat(timespec="seconds")
    end_iso = end.isoformat(timespec="seconds")
    hour_key = start.strftime("%Y-%m-%dT%H:00:00+00:00")
    return start_iso, end_iso, hour_key


def is_application_support_runtime(root_dir: str | Path) -> bool:
    core = _core_root(Path(root_dir)).resolve()
    expected = (Path.home() / "Library" / "Application Support" / "Personal AI Monitor" / "core").resolve()
    return core == expected


def _run_hermes_command(command: list[str], *, cwd: Path) -> dict:
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=DEFAULT_HA_AUTO_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return _hermes_run_result("failed", "hermes_executable_missing")
    except subprocess.TimeoutExpired:
        return _hermes_run_result("failed", "hermes_trigger_timeout")
    if completed.returncode == 0:
        parsed = _extract_hermes_json(completed.stdout)
        if not parsed:
            return _hermes_run_result(
                "completed",
                "",
                hermes_result_status="unknown",
                skipped_reason="hermes_returned_no_json",
                hermes_result_json={"status": "unknown", "skipped_reason": "hermes_returned_no_json"},
            )
        hermes_status = str(parsed.get("status") or "unknown").strip().lower()
        # Hermes is analysis-only and has no memory tool.  Never convert a
        # model self-report into a write receipt.
        memories_written = 0
        skipped_reason = _safe_error(str(parsed.get("skipped_reason") or "analysis_only_no_memory_tool"))
        written_candidate_count = 0
        skipped_candidate_count = _safe_int(parsed.get("skipped_candidate_count"), 0)
        written_candidate_types = _safe_audit_list_json([])
        skipped_reasons = _safe_audit_list_json(parsed.get("skipped_reasons"))
        write_audit_summary = _safe_error(str(parsed.get("write_audit_summary") or ""))
        written_memory_summaries = _safe_memory_summary_list_json([])
        sanitized_json = _sanitize_hermes_result_json(parsed)
        sanitized_json.update(
            {
                "status": "analysis_only",
                "memories_written_count": 0,
                "written_candidate_count": 0,
                "written_memory_summaries": [],
                "write_verified": False,
            }
        )
        if hermes_status in HERMES_COMPLETED_STATUSES:
            return _hermes_run_result(
                "completed",
                "",
                hermes_result_status="analysis_only",
                memories_written_count=memories_written,
                skipped_reason=skipped_reason,
                written_candidate_count=written_candidate_count,
                skipped_candidate_count=skipped_candidate_count,
                written_candidate_types=written_candidate_types,
                skipped_reasons=skipped_reasons,
                write_audit_summary=write_audit_summary,
                written_memory_summaries=written_memory_summaries,
                hermes_result_json=sanitized_json,
            )
        return _hermes_run_result(
            "failed",
            "hermes_analysis_failed",
            hermes_result_status=hermes_status,
            memories_written_count=memories_written,
            skipped_reason=skipped_reason,
            written_candidate_count=written_candidate_count,
            skipped_candidate_count=skipped_candidate_count,
            written_candidate_types=written_candidate_types,
            skipped_reasons=skipped_reasons,
            write_audit_summary=write_audit_summary,
            written_memory_summaries=written_memory_summaries,
            hermes_result_json=sanitized_json,
        )
    return _hermes_run_result("failed", "hermes_command_failed")


def _hermes_prompt(
    *,
    package_id: str,
    start: str,
    end: str,
    hour_key: str,
    hermes_profile: str,
    analysis_payload: dict,
) -> str:
    encoded_input = _bounded_analysis_json(_safe_analysis_payload(analysis_payload))
    return "\n".join(
        [
            "你是 Personal AI Monitor 的 Hermes activity-analysis 专用 profile。",
            "只处理这一个 HA Source Package 的脱敏内联摘要，不读取 SQLite 原表、raw vault、日志、截图、OCR 原文或任意本地文件。",
            "你没有终端、文件或 memory 工具，只能输出 analysis/advice，不能写 mem0。",
            f"package_id: {package_id}",
            f"hour_key: {hour_key}",
            f"time_range: {start} -> {end}",
            f"analysis_input_json: {encoded_input}",
            "如果 source_status 不是 active，只返回 no_action，不分析候选正文。",
            "memory_candidates/confirmed_actions 仅作分析输入；不得宣称已写入长期记忆。",
            "context_clues/review_only_candidates 只允许用于趋势判断，禁止写入长期记忆，且不能在日报里写成 Local Owner 已经在做的事实；rejected_observations 只用于质量诊断。",
            "文件夹名、Finder/桌面/侧边栏、任务列表旧项、浏览器标签、媒体文件名、只读 Preview/Photos 只能是 context 或 noise，不能写成投递、调研、修改、提交等当前行动。",
            "每条候选都有 subject_scope、source_category、source_scope、memory_write_policy；user_workflow_only、ambiguous 或 third_party 只能当建议线索。",
            "如果清洗后的 PAM 数据仍无法判断内容是不是 Local Owner 自己的情况，必须选择不写入长期记忆；0 条写入是正确且可接受的结果，宁可不写也不要写错。",
            "source_category=target_role_jd / third_party_resume / ambiguous_document 或 memory_write_policy=reference_only/blocked_subject 只能用于当天工作流总结，禁止写入长期记忆。",
            "不得把第三方简历、候选人资料、网页档案、教育/工作经历、联系方式、JD、面试公司要求、EMEA/APAC/渠道打法写成 Local Owner 的事实；遇到 third_party_subject、ambiguous_subject、reference_only_job_document 必须跳过，并使用 skipped_reasons=third_party_or_ambiguous_subject、subject_ownership_unclear 或 reference_only_job_document。",
            "PAM 已保证该包 raw_access=false、quarantine_status=clear_only、source=extracted_only；你仍要做最小必要复核。",
            "可以建议哪些 self-owned confirmed facts 值得后续人工审批，但 memories_written_count 必须为 0。",
            "不要把包正文、私密聊天原文、路径、截图/OCR、账号状态、密钥或临时过程写入长期记忆。",
            "最终只返回简短 JSON：status=analysis_only|no_action|failed, package_id, memories_written_count=0, recommended_candidate_count, skipped_candidate_count, skipped_reasons, analysis_summary, skipped_reason。",
            "write_audit_summary 只能是安全总摘要，不得包含候选全文、OCR、路径、截图、账号、密钥或私密内容。",
            "若无 eligible 长期记忆候选，或主体归属不清，memories_written_count=0，并优先沿用内联 JSON 的 memory_candidate_skip_reason；主体归属不清使用 skipped_reason=subject_ownership_unclear 或 third_party_or_ambiguous_subject，JD/参考材料场景使用 skipped_reason=reference_only_job_document。",
        ]
    )


def _safe_analysis_payload(value: dict) -> dict:
    if not isinstance(value, dict):
        return {"source_status": "invalid", "raw_access": False}
    candidates = value.get("memory_candidates") if isinstance(value.get("memory_candidates"), list) else []
    review = value.get("review_only_candidates") if isinstance(value.get("review_only_candidates"), list) else []

    def safe_candidates(items: list) -> list[dict]:
        output: list[dict] = []
        for item in items[:20]:
            if not isinstance(item, dict):
                continue
            safe_item: dict = {}
            for key in (
                        "candidate_key",
                        "candidate_type",
                        "workflow",
                        "topic",
                        "app",
                        "safe_summary",
                        "evidence_count",
                        "subject_scope",
                        "source_category",
                        "write_eligibility",
                        "evidence_layer",
                        "confirmation_signals",
                        "quality_flags",
                        "write_risk_flags",
                        "screen_region_signal",
                    ):
                child = item.get(key)
                if isinstance(child, str):
                    safe_item[key] = _safe_error(child)
                elif isinstance(child, list):
                    safe_item[key] = [_safe_error(str(entry))[:120] for entry in child[:12]]
                elif isinstance(child, dict):
                    safe_item[key] = {
                        _safe_error(str(child_key))[:80]: _safe_error(str(child_value))[:120]
                        for child_key, child_value in list(child.items())[:12]
                    }
                elif isinstance(child, (int, float, bool)) or child is None:
                    safe_item[key] = child
            output.append(safe_item)
        return output

    return {
        "package_id": _safe_error(str(value.get("package_id") or "")),
        "source_status": _safe_error(str(value.get("source_status") or "unknown")),
        "raw_access": False,
        "time_range": {
            "from": _safe_error(str((value.get("time_range") or {}).get("from") or "")),
            "to": _safe_error(str((value.get("time_range") or {}).get("to") or "")),
        }
        if isinstance(value.get("time_range"), dict)
        else {},
        "candidate_counts": {
            key: _safe_int(value.get(key), 0)
            for key in (
                "total_candidate_count",
                "eligible_candidate_count",
                "review_only_candidate_count",
                "ephemeral_candidate_count",
                "confirmed_action_count",
                "context_clue_count",
                "noise_or_reference_count",
            )
        },
        "memory_candidate_skip_reason": _safe_error(str(value.get("memory_candidate_skip_reason") or "")),
        "write_guard_summary": _safe_error(str(value.get("write_guard_summary") or "")),
        "memory_candidates": safe_candidates(candidates),
        "review_only_candidates": safe_candidates(review),
    }


def _bounded_analysis_json(value: dict, *, max_chars: int = 24000) -> str:
    """Return bounded, structurally valid JSON for the analysis-only prompt."""
    safe_value = value if isinstance(value, dict) else {"source_status": "invalid", "raw_access": False}
    encoded = json.dumps(safe_value, ensure_ascii=False, sort_keys=True)
    if len(encoded) <= max_chars:
        return encoded

    def compact_candidates(items: object) -> list[dict]:
        output: list[dict] = []
        if not isinstance(items, list):
            return output
        for item in items[:6]:
            if not isinstance(item, dict):
                continue
            compact_item = dict(item)
            summary = compact_item.get("safe_summary")
            if isinstance(summary, str):
                compact_item["safe_summary"] = summary[:240]
            for key in ("confirmation_signals", "quality_flags", "write_risk_flags"):
                child = compact_item.get(key)
                if isinstance(child, list):
                    compact_item[key] = child[:6]
            output.append(compact_item)
        return output

    compact = {
        "input_truncated": True,
        "package_id": safe_value.get("package_id"),
        "source_status": safe_value.get("source_status"),
        "raw_access": False,
        "time_range": safe_value.get("time_range") if isinstance(safe_value.get("time_range"), dict) else {},
        "candidate_counts": safe_value.get("candidate_counts")
        if isinstance(safe_value.get("candidate_counts"), dict)
        else {},
        "memory_candidate_skip_reason": safe_value.get("memory_candidate_skip_reason"),
        "write_guard_summary": safe_value.get("write_guard_summary"),
        "memory_candidates": compact_candidates(safe_value.get("memory_candidates")),
        "review_only_candidates": compact_candidates(safe_value.get("review_only_candidates")),
    }
    encoded = json.dumps(compact, ensure_ascii=False, sort_keys=True)
    if len(encoded) <= max_chars:
        return encoded

    minimal = {
        "input_truncated": True,
        "package_id": safe_value.get("package_id"),
        "source_status": safe_value.get("source_status"),
        "raw_access": False,
        "time_range": safe_value.get("time_range") if isinstance(safe_value.get("time_range"), dict) else {},
        "candidate_counts": safe_value.get("candidate_counts")
        if isinstance(safe_value.get("candidate_counts"), dict)
        else {},
        "memory_candidate_skip_reason": safe_value.get("memory_candidate_skip_reason"),
    }
    encoded = json.dumps(minimal, ensure_ascii=False, sort_keys=True)
    if len(encoded) <= max_chars:
        return encoded
    return json.dumps({"input_truncated": True, "raw_access": False}, ensure_ascii=False, sort_keys=True)


def _upsert_job(
    conn: sqlite3.Connection,
    *,
    hour_key: str,
    start: str,
    end: str,
    package_id: str,
    activity_count: int,
    safe_event_count: int,
    trigger_mode: str,
    hermes_profile: str,
    status: str,
    generated_at: str,
    candidate_count: int = 0,
    candidate_skip_reason: str = "",
    eligible_candidate_count: int = 0,
    review_only_candidate_count: int = 0,
    ephemeral_candidate_count: int = 0,
    confirmed_action_count: int = 0,
    context_clue_count: int = 0,
    noise_or_reference_count: int = 0,
    single_evidence_eligible_count: int = 0,
    subject_risk_candidate_count: int = 0,
    single_evidence_demoted_count: int = 0,
    media_or_file_listing_ephemeral_count: int = 0,
) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO ha_automation_jobs(
          id, hour_key, started_at, ended_at, package_id, activity_count,
          safe_timeline_event_count, trigger_mode, hermes_profile, status,
          attempt_count, last_error, command, generated_at, triggered_at,
          completed_at, candidate_count, candidate_skip_reason,
          eligible_candidate_count, review_only_candidate_count, ephemeral_candidate_count,
          confirmed_action_count, context_clue_count, noise_or_reference_count,
          single_evidence_eligible_count, subject_risk_candidate_count,
          single_evidence_demoted_count, media_or_file_listing_ephemeral_count,
          created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(hour_key) DO UPDATE SET
          package_id = excluded.package_id,
          activity_count = excluded.activity_count,
          safe_timeline_event_count = excluded.safe_timeline_event_count,
          trigger_mode = excluded.trigger_mode,
          hermes_profile = excluded.hermes_profile,
          status = excluded.status,
          last_error = excluded.last_error,
          generated_at = excluded.generated_at,
          candidate_count = excluded.candidate_count,
          candidate_skip_reason = excluded.candidate_skip_reason,
          eligible_candidate_count = excluded.eligible_candidate_count,
          review_only_candidate_count = excluded.review_only_candidate_count,
          ephemeral_candidate_count = excluded.ephemeral_candidate_count,
          confirmed_action_count = excluded.confirmed_action_count,
          context_clue_count = excluded.context_clue_count,
          noise_or_reference_count = excluded.noise_or_reference_count,
          single_evidence_eligible_count = excluded.single_evidence_eligible_count,
          subject_risk_candidate_count = excluded.subject_risk_candidate_count,
          single_evidence_demoted_count = excluded.single_evidence_demoted_count,
          media_or_file_listing_ephemeral_count = excluded.media_or_file_listing_ephemeral_count,
          updated_at = excluded.updated_at
        """,
        (
            f"ha_auto_{uuid.uuid4().hex}",
            hour_key,
            start,
            end,
            package_id,
            int(activity_count),
            int(safe_event_count),
            trigger_mode,
            hermes_profile,
            status,
            0,
            "",
            "",
            generated_at,
            "",
            "",
            int(candidate_count),
            str(candidate_skip_reason or ""),
            int(eligible_candidate_count),
            int(review_only_candidate_count),
            int(ephemeral_candidate_count),
            int(confirmed_action_count),
            int(context_clue_count),
            int(noise_or_reference_count),
            int(single_evidence_eligible_count),
            int(subject_risk_candidate_count),
            int(single_evidence_demoted_count),
            int(media_or_file_listing_ephemeral_count),
            now,
            now,
        ),
    )


def _audit_handoff(
    conn: sqlite3.Connection,
    *,
    package_id: str,
    hour_key: str,
    start: str,
    end: str,
    activity_count: int,
    safe_event_count: int,
    profile: str,
    trigger_mode: str,
    status: str,
    handed_to_hermes: bool,
    attempt_count: int,
    failure_reason: str,
    hermes_result: dict | None = None,
    memory_report: dict | None = None,
    next_retry_at: str = "",
    dead_letter_reason: str = "",
) -> None:
    now = utc_now()
    hermes_result = hermes_result or {}
    memory_report = memory_report or {}
    conn.execute(
        """
        INSERT INTO audit_logs(id, at, action, target_type, target_id, detail)
        VALUES(?, ?, ?, ?, ?, ?)
        """,
        (
            f"audit_{uuid.uuid4().hex}",
            now,
            "ha_hourly_package_handoff",
            "distribution_package",
            package_id,
            json.dumps(
                {
                    "hour_key": hour_key,
                    "time_range": {"from": start, "to": end},
                    "activity_count": int(activity_count),
                    "safe_timeline_event_count": int(safe_event_count),
                    "hermes_profile": profile,
                    "trigger_mode": trigger_mode,
                    "trigger_status": status,
                    "handed_to_hermes": bool(handed_to_hermes),
                    "attempt_count": int(attempt_count),
                    "failure_reason": failure_reason,
                    "hermes_result_status": hermes_result.get("hermes_result_status") or "",
                    "memories_written_count": int(hermes_result.get("memories_written_count") or 0),
                    "skipped_reason": hermes_result.get("skipped_reason") or "",
                    "written_candidate_count": int(hermes_result.get("written_candidate_count") or 0),
                    "skipped_candidate_count": int(hermes_result.get("skipped_candidate_count") or 0),
                    "written_candidate_types": _decode_audit_list(hermes_result.get("written_candidate_types") or ""),
                    "skipped_reasons": _decode_audit_list(hermes_result.get("skipped_reasons") or ""),
                    "write_audit_summary": hermes_result.get("write_audit_summary") or "",
                    "written_memory_summaries": _decode_memory_summary_list(hermes_result.get("written_memory_summaries") or ""),
                    "memory_report_status": memory_report.get("status") or "",
                    "memory_report_target_source": memory_report.get("target_source") or "",
                    "memory_report_error": memory_report.get("error") or "",
                    "memory_report_attempt_count": int(memory_report.get("attempt_count") or 0),
                    "next_retry_at": next_retry_at,
                    "dead_letter_reason": dead_letter_reason,
                },
                ensure_ascii=False,
            ),
        ),
    )


def _attempt_memory_write_report(
    conn: sqlite3.Connection,
    *,
    hour_key: str,
    package_id: str,
    hour_start: str,
    hour_end: str,
    hermes_profile: str,
    hermes_result: dict,
    now_iso: str | None = None,
) -> dict:
    written_count = int(hermes_result.get("memories_written_count") or 0)
    current = _job_for_hour(conn, hour_key)
    attempt_count = int(current["memory_report_attempt_count"] or 0) + 1 if current else 1
    now_value = now_iso or utc_now()
    if written_count <= 0:
        _update_memory_report_status(
            conn,
            hour_key=hour_key,
            status="not_required",
            target_label="",
            error="",
            reported_at="",
            attempt_count=0,
            next_retry_at="",
            updated_at=now_value,
        )
        return {"status": "not_required", "attempt_count": 0}

    if not _verified_mem0_write(hermes_result):
        _update_memory_report_status(
            conn,
            hour_key=hour_key,
            status="blocked_unverified",
            target_label="",
            error="unverified_write_claim",
            reported_at="",
            attempt_count=0,
            next_retry_at="",
            updated_at=now_value,
        )
        return {
            "status": "blocked_unverified",
            "error": "unverified_write_claim",
            "attempt_count": 0,
        }

    channel_name = _get_setting(conn, "ha_auto_memory_report_channel", DEFAULT_MEMORY_REPORT_CHANNEL_NAME)
    report_profile = _get_setting(conn, "ha_auto_memory_report_profile", DEFAULT_FEISHU_NOTIFY_PROFILE)
    target = _resolve_memory_report_target(channel_name=channel_name, profile=report_profile)
    if not target.get("ok"):
        error = _safe_error(str(target.get("reason") or "memory report target not resolved"))
        next_retry_at = _iso_after_minutes(now_value, DEFAULT_MEMORY_REPORT_RETRY_BACKOFF_MINUTES * max(1, attempt_count))
        _update_memory_report_status(
            conn,
            hour_key=hour_key,
            status="failed",
            target_label=f"feishu:{channel_name}",
            error=error,
            reported_at="",
            attempt_count=attempt_count,
            next_retry_at=next_retry_at,
            updated_at=now_value,
        )
        return {
            "status": "failed",
            "error": error,
            "attempt_count": attempt_count,
            "next_retry_at": next_retry_at,
            "target_source": str(target.get("source") or ""),
        }

    message = _memory_write_report_message(
        package_id=package_id,
        hour_key=hour_key,
        hour_start=hour_start,
        hour_end=hour_end,
        hermes_profile=hermes_profile,
        hermes_result=hermes_result,
    )
    sent = _send_memory_report_message(
        target=str(target.get("target") or ""),
        message=message,
        profile=report_profile,
    )
    if sent.get("ok"):
        status = "dry_run" if sent.get("dry_run") else "sent"
        _update_memory_report_status(
            conn,
            hour_key=hour_key,
            status=status,
            target_label=f"feishu:{channel_name}",
            error="",
            reported_at=now_value,
            attempt_count=attempt_count,
            next_retry_at="",
            updated_at=now_value,
        )
        return {
            "status": status,
            "attempt_count": attempt_count,
            "target_source": str(target.get("source") or ""),
        }

    error = _safe_error(str(sent.get("reason") or sent.get("output") or "memory report send failed"))
    next_retry_at = _iso_after_minutes(now_value, DEFAULT_MEMORY_REPORT_RETRY_BACKOFF_MINUTES * max(1, attempt_count))
    _update_memory_report_status(
        conn,
        hour_key=hour_key,
        status="pending",
        target_label=f"feishu:{channel_name}",
        error=error,
        reported_at="",
        attempt_count=attempt_count,
        next_retry_at=next_retry_at,
        updated_at=now_value,
    )
    return {
        "status": "pending",
        "error": error,
        "attempt_count": attempt_count,
        "next_retry_at": next_retry_at,
        "target_source": str(target.get("source") or ""),
    }


def _retry_pending_memory_reports(conn: sqlite3.Connection, now: datetime) -> None:
    now_iso = now.isoformat(timespec="seconds")
    rows = conn.execute(
        """
        SELECT hour_key, started_at, ended_at, package_id, hermes_profile,
               memories_written_count, written_candidate_count, skipped_candidate_count,
               written_candidate_types, skipped_reasons, write_audit_summary,
               written_memory_summaries, skipped_reason
        FROM ha_automation_jobs
        WHERE memories_written_count > 0
          AND COALESCE(memory_report_status, '') IN ('pending', 'failed')
        ORDER BY updated_at ASC
        LIMIT 100
        """,
    ).fetchall()
    for row in rows:
        hermes_result = {
            "memories_written_count": int(row["memories_written_count"] or 0),
            "written_candidate_count": int(row["written_candidate_count"] or 0),
            "skipped_candidate_count": int(row["skipped_candidate_count"] or 0),
            "written_candidate_types": row["written_candidate_types"] or "",
            "skipped_reasons": row["skipped_reasons"] or "",
            "write_audit_summary": row["write_audit_summary"] or "",
            "written_memory_summaries": row["written_memory_summaries"] or "",
            "skipped_reason": row["skipped_reason"] or "",
        }
        _attempt_memory_write_report(
            conn,
            hour_key=str(row["hour_key"]),
            package_id=str(row["package_id"] or ""),
            hour_start=str(row["started_at"] or ""),
            hour_end=str(row["ended_at"] or ""),
            hermes_profile=str(row["hermes_profile"] or DEFAULT_HA_AUTO_HERMES_PROFILE),
            hermes_result=hermes_result,
            now_iso=now_iso,
        )


def _update_memory_report_status(
    conn: sqlite3.Connection,
    *,
    hour_key: str,
    status: str,
    target_label: str,
    error: str,
    reported_at: str,
    attempt_count: int,
    next_retry_at: str,
    updated_at: str,
) -> None:
    conn.execute(
        """
        UPDATE ha_automation_jobs
        SET memory_report_status = ?,
            memory_report_target = ?,
            memory_report_error = ?,
            memory_reported_at = ?,
            memory_report_attempt_count = ?,
            memory_report_next_retry_at = ?,
            updated_at = ?
        WHERE hour_key = ?
        """,
        (
            status,
            _safe_error(target_label),
            _safe_error(error),
            reported_at,
            max(0, int(attempt_count)),
            next_retry_at,
            updated_at,
            hour_key,
        ),
    )


def _count_memory_report_jobs(conn: sqlite3.Connection, statuses: set[str]) -> int:
    placeholders = ",".join("?" for _ in statuses)
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM ha_automation_jobs
        WHERE memories_written_count > 0
          AND COALESCE(memory_report_status, '') IN ({placeholders})
        """,
        tuple(sorted(statuses)),
    ).fetchone()
    return int(row["count"] or 0)


def _count_memory_report_retry_due(conn: sqlite3.Connection, now: datetime) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM ha_automation_jobs
        WHERE memories_written_count > 0
          AND COALESCE(memory_report_status, '') IN ('pending', 'failed')
          AND (memory_report_next_retry_at IS NULL OR memory_report_next_retry_at = ''
               OR datetime(memory_report_next_retry_at) <= datetime(?))
        """,
        (now.isoformat(timespec="seconds"),),
    ).fetchone()
    return int(row["count"] or 0)


def _memory_write_report_message(
    *,
    package_id: str,
    hour_key: str,
    hour_start: str,
    hour_end: str,
    hermes_profile: str,
    hermes_result: dict,
) -> str:
    write_verified = _verified_mem0_write(hermes_result)
    summaries = _decode_memory_summary_list(hermes_result.get("written_memory_summaries") or "")
    written_count = int(hermes_result.get("memories_written_count") or 0)
    if not write_verified:
        return "\n".join(
            [
                "【PAM 分析建议】未核验的模型自报",
                f"模型自报 {written_count} 条写入，但没有 mem0 可核验回执。",
                "结论：本消息只是 analysis/advice，不代表长期记忆已写入。",
            ]
        )
    if not summaries:
        summaries = ["已验证写入回执，但未保留逐条安全摘要。"]
    lines = [
        "【记忆系统】本次写入的记忆内容",
        f"本次写入 {written_count} 条 mem0 长期记忆：",
    ]
    lines.extend(
        f"{index}. {_safe_error(summary)}"
        for index, summary in enumerate(summaries[:MAX_MEMORY_REPORT_SUMMARIES], start=1)
    )
    lines.append("边界：本汇报只展示安全摘要，不含内部编号、调度细节、路径、截图、OCR、账号、密钥或私密原文。")
    return "\n".join(lines)


def _verified_mem0_write(hermes_result: dict) -> bool:
    receipt_ids = hermes_result.get("mem0_receipt_ids")
    if isinstance(receipt_ids, str):
        try:
            receipt_ids = json.loads(receipt_ids)
        except json.JSONDecodeError:
            receipt_ids = []
    return bool(
        hermes_result.get("write_verified")
        and isinstance(receipt_ids, list)
        and any(str(value or "").strip() for value in receipt_ids)
    )


def _resolve_memory_report_target(*, channel_name: str, profile: str) -> dict:
    override = os.environ.get("PAM_MEMORY_REPORT_FEISHU_TARGET", "").strip()
    if override:
        target = override if override.startswith("feishu:") else f"feishu:{override}"
        return {"ok": True, "target": target, "source": "env_override"}
    override = _load_feishu_target_override("memory_system")
    if override:
        return {"ok": True, "target": override, "source": "override_file"}
    directory = Path.home() / ".hermes" / "profiles" / profile / "channel_directory.json"
    rows, load_error = _load_feishu_rows(directory)
    exact = [row for row in rows if str(row.get("name", "")).strip() == channel_name]
    groups = [row for row in exact if row.get("type") == "group"]
    candidates = groups or exact
    if not candidates:
        known = ", ".join(str(row.get("name") or row.get("id")) for row in rows[:10])
        return {"ok": False, "reason": load_error or f"未找到飞书“{channel_name}”；当前可见目标：{known or '空'}"}
    target_id = str(candidates[0].get("id") or "").strip()
    if not target_id:
        return {"ok": False, "reason": f"飞书“{channel_name}”目标缺少 id"}
    return {"ok": True, "target": f"feishu:{target_id}", "source": "channel_directory"}


def _load_feishu_target_override(key: str) -> str | None:
    targets = Path.home() / ".hermes" / "feishu-channel-targets.json"
    if not targets.exists():
        return None
    try:
        data = json.loads(targets.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = data.get(key) or data.get(f"{key}_target")
    if isinstance(value, str) and value.strip():
        value = value.strip()
        return value if value.startswith("feishu:") else f"feishu:{value}"
    return None


def _load_feishu_rows(directory: Path) -> tuple[list[dict], str | None]:
    if not directory.exists():
        return [], "feishubot_channel_directory_missing"
    try:
        data = json.loads(directory.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], "feishubot_channel_directory_unreadable"
    rows = data.get("platforms", {}).get("feishu") or []
    return [row for row in rows if isinstance(row, dict)], None


def _send_memory_report_message(*, target: str, message: str, profile: str) -> dict:
    if os.environ.get("PAM_MEMORY_REPORT_DRY_RUN") == "1" or os.environ.get("HERMES_MEMORY_SYSTEM_DRY_RUN") == "1":
        return {"ok": True, "dry_run": True}
    hermes_python = Path.home() / ".hermes" / "hermes-agent" / "venv" / "bin" / "python"
    if not hermes_python.exists():
        return {"ok": False, "reason": "hermes_python_missing"}
    try:
        completed = subprocess.run(
            [
                str(hermes_python),
                "-m",
                "hermes_cli.main",
                "--profile",
                profile,
                "send",
                "--to",
                target,
                "--json",
                message,
            ],
            capture_output=True,
            text=True,
            timeout=45,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {"ok": False, "reason": "memory_report_delivery_failed"}
    if completed.returncode == 0:
        return {"ok": True, "output": "memory_report_delivered"}
    return {"ok": False, "output": "memory_report_delivery_failed"}


def _latest_job(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        """
        SELECT hour_key, started_at, ended_at, package_id, activity_count,
               safe_timeline_event_count, trigger_mode, hermes_profile, status,
               attempt_count, last_error, generated_at, triggered_at, completed_at,
               processing_started_at, next_retry_at, dead_letter_reason,
               hermes_result_status, memories_written_count, skipped_reason,
               hermes_result_json, candidate_count, candidate_skip_reason,
               eligible_candidate_count, review_only_candidate_count, ephemeral_candidate_count,
               confirmed_action_count, context_clue_count, noise_or_reference_count,
               single_evidence_eligible_count, subject_risk_candidate_count,
               single_evidence_demoted_count, media_or_file_listing_ephemeral_count,
               written_candidate_count, skipped_candidate_count, written_candidate_types,
               skipped_reasons, write_audit_summary, written_memory_summaries,
               memory_report_status, memory_report_target, memory_report_error,
               memory_reported_at, memory_report_attempt_count, memory_report_next_retry_at,
               updated_at
        FROM ha_automation_jobs
        ORDER BY
          datetime(COALESCE(NULLIF(completed_at, ''), NULLIF(triggered_at, ''), NULLIF(generated_at, ''), NULLIF(updated_at, ''))) DESC,
          datetime(updated_at) DESC,
          rowid DESC
        LIMIT 1
        """
    ).fetchone()
    return dict(row) if row else None


def _job_for_hour(conn: sqlite3.Connection, hour_key: str):
    return conn.execute(
        "SELECT * FROM ha_automation_jobs WHERE hour_key = ?",
        (hour_key,),
    ).fetchone()


def _oldest_retryable_job(conn: sqlite3.Connection, now: datetime):
    placeholders = ",".join("?" for _ in RETRYABLE_STATUSES)
    now_iso = now.isoformat(timespec="seconds")
    return conn.execute(
        f"""
        SELECT *
        FROM ha_automation_jobs
        WHERE status IN ({placeholders})
          AND package_id IS NOT NULL
          AND package_id != ''
          AND attempt_count < ?
          AND (next_retry_at IS NULL OR next_retry_at = '' OR datetime(next_retry_at) <= datetime(?))
        ORDER BY updated_at ASC
        LIMIT 1
        """,
        (*tuple(sorted(RETRYABLE_STATUSES)), _ha_max_attempts(conn), now_iso),
    ).fetchone()


def _count_jobs(conn: sqlite3.Connection, statuses: set[str]) -> int:
    placeholders = ",".join("?" for _ in statuses)
    row = conn.execute(
        f"SELECT COUNT(*) AS count FROM ha_automation_jobs WHERE status IN ({placeholders})",
        tuple(sorted(statuses)),
    ).fetchone()
    return int(row["count"] or 0)


def _dead_letter_resolution(conn: sqlite3.Connection) -> dict:
    latest_completed = _latest_completed_job_time(conn)
    row = conn.execute(
        """
        SELECT
          COUNT(*) AS dead_letter_count,
          SUM(
            CASE
              WHEN ? != ''
               AND COALESCE(
                 NULLIF(updated_at, ''),
                 NULLIF(completed_at, ''),
                 NULLIF(triggered_at, ''),
                 NULLIF(generated_at, ''),
                 NULLIF(created_at, ''),
                 hour_key
               ) < ?
              THEN 1 ELSE 0
            END
          ) AS historical_dead_letter_count,
          SUM(
            CASE
              WHEN ? = ''
                OR COALESCE(
                  NULLIF(updated_at, ''),
                  NULLIF(completed_at, ''),
                  NULLIF(triggered_at, ''),
                  NULLIF(generated_at, ''),
                  NULLIF(created_at, ''),
                  hour_key
                ) >= ?
              THEN 1 ELSE 0
            END
          ) AS unresolved_dead_letter_count
        FROM ha_automation_jobs
        WHERE status = 'dead_letter'
        """,
        (latest_completed, latest_completed, latest_completed, latest_completed),
    ).fetchone()
    return {
        "dead_letter_count": int(row["dead_letter_count"] or 0) if row else 0,
        "historical_dead_letter_count": int(row["historical_dead_letter_count"] or 0) if row else 0,
        "unresolved_dead_letter_count": int(row["unresolved_dead_letter_count"] or 0) if row else 0,
        "latest_completed_at": latest_completed,
    }


def _latest_completed_job_time(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        """
        SELECT MAX(
          COALESCE(
            NULLIF(completed_at, ''),
            NULLIF(updated_at, ''),
            NULLIF(triggered_at, ''),
            NULLIF(generated_at, ''),
            NULLIF(created_at, ''),
            hour_key
          )
        ) AS latest_completed_at
        FROM ha_automation_jobs
        WHERE status = 'completed'
        """
    ).fetchone()
    return str(row["latest_completed_at"] or "") if row else ""


def _count_active_packages(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS count FROM ha_source_records WHERE source_status = 'active'"
    ).fetchone()
    return int(row["count"] or 0)


def _count_active_auto_packages(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM ha_source_records h
        JOIN distribution_packages d ON d.id = h.package_id
        WHERE h.source_status = 'active'
          AND d.generation_mode = 'hourly_auto'
        """
    ).fetchone()
    return int(row["count"] or 0)


def _count_retry_due_jobs(conn: sqlite3.Connection, now: datetime) -> int:
    placeholders = ",".join("?" for _ in RETRYABLE_STATUSES)
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS count
        FROM ha_automation_jobs
        WHERE status IN ({placeholders})
          AND package_id IS NOT NULL
          AND package_id != ''
          AND attempt_count < ?
          AND (next_retry_at IS NULL OR next_retry_at = '' OR datetime(next_retry_at) <= datetime(?))
        """,
        (*tuple(sorted(RETRYABLE_STATUSES)), _ha_max_attempts(conn), now.isoformat(timespec="seconds")),
    ).fetchone()
    return int(row["count"] or 0)


def _count_stale_pending_jobs(conn: sqlite3.Connection, now: datetime) -> int:
    cutoff = (now - timedelta(minutes=_pending_stale_minutes(conn))).isoformat(timespec="seconds")
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM ha_automation_jobs
        WHERE status = 'pending'
          AND processing_started_at IS NOT NULL
          AND processing_started_at != ''
          AND datetime(processing_started_at) <= datetime(?)
        """,
        (cutoff,),
    ).fetchone()
    return int(row["count"] or 0)


def _mark_stale_pending_jobs(conn: sqlite3.Connection, now: datetime) -> None:
    cutoff = (now - timedelta(minutes=_pending_stale_minutes(conn))).isoformat(timespec="seconds")
    next_retry_at = now.isoformat(timespec="seconds")
    conn.execute(
        """
        UPDATE ha_automation_jobs
        SET status = 'failed',
            last_error = 'pending stale after timeout',
            next_retry_at = ?,
            updated_at = ?
        WHERE status = 'pending'
          AND processing_started_at IS NOT NULL
          AND processing_started_at != ''
          AND datetime(processing_started_at) <= datetime(?)
        """,
        (next_retry_at, next_retry_at, cutoff),
    )


def _mark_dead_letter_jobs(conn: sqlite3.Connection) -> None:
    now = utc_now()
    max_attempts = _ha_max_attempts(conn)
    conn.execute(
        """
        UPDATE ha_automation_jobs
        SET status = 'dead_letter',
            dead_letter_reason = COALESCE(NULLIF(last_error, ''), 'max retry attempts reached'),
            next_retry_at = '',
            updated_at = ?
        WHERE status = 'failed'
          AND attempt_count >= ?
        """,
        (now, max_attempts),
    )


def _job_retry_due(job, now: datetime) -> bool:
    if job is None:
        return True
    status = str(job["status"] or "")
    if status in SUCCESS_STATUSES or status in TERMINAL_FAILURE_STATUSES:
        return False
    if status not in RETRYABLE_STATUSES:
        return False
    next_retry_at = str(job["next_retry_at"] or "") if "next_retry_at" in job.keys() else ""
    if not next_retry_at:
        return True
    try:
        retry_at = datetime.fromisoformat(next_retry_at)
    except ValueError:
        return True
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    return retry_at.astimezone(timezone.utc) <= now.astimezone(timezone.utc)


def _ha_max_attempts(conn: sqlite3.Connection) -> int:
    return max(1, _int_setting(conn, "ha_auto_max_attempts", DEFAULT_HA_AUTO_MAX_ATTEMPTS))


def _pending_stale_minutes(conn: sqlite3.Connection) -> int:
    return max(1, _int_setting(conn, "ha_auto_pending_stale_minutes", DEFAULT_HA_AUTO_PENDING_STALE_MINUTES))


def _utc_datetime(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _iso_after_minutes(base_iso: str, minutes: int) -> str:
    try:
        base = datetime.fromisoformat(base_iso)
    except ValueError:
        base = datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return (base.astimezone(timezone.utc) + timedelta(minutes=max(1, minutes))).isoformat(timespec="seconds")


def _local_window(start: str, end: str) -> dict:
    try:
        start_dt = datetime.fromisoformat(start).astimezone()
        end_dt = datetime.fromisoformat(end).astimezone()
    except ValueError:
        return {"from": start, "to": end, "label": f"{start} -> {end}"}
    return {
        "from": start_dt.isoformat(timespec="seconds"),
        "to": end_dt.isoformat(timespec="seconds"),
        "label": f"{start_dt.strftime('%Y-%m-%d %H:%M')} - {end_dt.strftime('%H:%M %Z')}",
    }


def _latest_result_summary(latest: dict | None) -> str:
    if not latest:
        return "暂无"
    status = str(latest.get("hermes_result_status") or latest.get("status") or "unknown")
    memories = int(latest.get("memories_written_count") or 0)
    skipped = str(latest.get("skipped_reason") or "")
    audit = str(latest.get("write_audit_summary") or "")
    completed = latest.get("completed_at") or latest.get("triggered_at") or latest.get("updated_at") or ""
    local_completed = _local_time_label(str(completed)) if completed else ""
    if skipped:
        return f"{status}，写入 {memories} 条，跳过原因 {skipped}，{local_completed}".strip("，")
    if audit:
        return f"{status}，写入 {memories} 条，{audit}，{local_completed}".strip("，")
    return f"{status}，写入 {memories} 条，{local_completed}".strip("，")


def _package_candidate_summary(package: dict) -> dict:
    digest = package.get("learning_digest") if isinstance(package.get("learning_digest"), dict) else {}
    diagnostics = package.get("learnability_diagnostics") if isinstance(package.get("learnability_diagnostics"), dict) else {}
    candidates = digest.get("memory_candidates") if isinstance(digest.get("memory_candidates"), list) else []
    quality = digest.get("memory_candidate_quality") if isinstance(digest.get("memory_candidate_quality"), dict) else {}
    candidate_count = int(quality.get("total_candidate_count") or len(candidates))
    eligible_count = int(quality.get("eligible_candidate_count") or 0)
    review_only_count = int(quality.get("review_only_candidate_count") or 0)
    ephemeral_count = int(quality.get("ephemeral_candidate_count") or 0)
    if not quality:
        eligible_count = sum(1 for item in candidates if str(item.get("write_eligibility") or "eligible") == "eligible")
        review_only_count = sum(1 for item in candidates if str(item.get("write_eligibility") or "") == "review_only")
        ephemeral_count = sum(1 for item in candidates if str(item.get("write_eligibility") or "") == "ephemeral")
    if eligible_count:
        skip_reason = ""
    elif candidate_count:
        reference_only = int(quality.get("reference_only_document_count") or 0)
        target_role_jd = int(quality.get("target_role_jd_document_count") or 0)
        third_party_resume = int(quality.get("third_party_resume_document_count") or 0)
        ambiguous_document = int(quality.get("ambiguous_document_count") or 0)
        subject_risk = int(quality.get("subject_risk_candidate_count") or 0)
        if candidate_count and (third_party_resume or ambiguous_document or subject_risk) and (third_party_resume + ambiguous_document + subject_risk) >= candidate_count:
            skip_reason = "third_party_or_ambiguous_subject"
        elif candidate_count and (reference_only or target_role_jd) and (reference_only + target_role_jd) >= candidate_count:
            skip_reason = "reference_only_job_document"
        else:
            skip_reason = "no_write_eligible_memory_candidates"
    else:
        learnability = digest.get("learnability_counts") if isinstance(digest.get("learnability_counts"), dict) else {}
        quality = diagnostics.get("quality") if isinstance(diagnostics.get("quality"), dict) else {}
        useful = int(learnability.get("useful") or 0)
        if useful <= 0:
            skip_reason = "no_useful_learning_items"
        elif int(quality.get("short_or_empty_content_count") or 0) >= useful:
            skip_reason = "low_signal_or_short_content"
        else:
            skip_reason = "no_long_term_memory_candidates"
    return {
        "candidate_count": candidate_count,
        "candidate_skip_reason": skip_reason,
        "eligible_candidate_count": eligible_count,
        "review_only_candidate_count": review_only_count,
        "ephemeral_candidate_count": ephemeral_count,
        "confirmed_action_count": int(quality.get("confirmed_action_count") or 0),
        "context_clue_count": int(quality.get("context_clue_count") or 0),
        "noise_or_reference_count": int(quality.get("noise_or_reference_count") or 0),
        "single_evidence_eligible_count": int(quality.get("single_evidence_eligible_count") or 0),
        "subject_risk_candidate_count": int(quality.get("subject_risk_candidate_count") or 0),
        "target_role_jd_document_count": int(quality.get("target_role_jd_document_count") or 0),
        "reference_only_document_count": int(quality.get("reference_only_document_count") or 0),
        "third_party_resume_document_count": int(quality.get("third_party_resume_document_count") or 0),
        "ambiguous_document_count": int(quality.get("ambiguous_document_count") or 0),
        "single_evidence_demoted_count": int(quality.get("single_evidence_demoted_count") or 0),
        "media_or_file_listing_ephemeral_count": int(quality.get("media_or_file_listing_ephemeral_count") or 0),
    }


def _local_time_label(value: str) -> str:
    try:
        dt = datetime.fromisoformat(value).astimezone()
    except ValueError:
        return value
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z")


def _core_root(root: Path) -> Path:
    return root.parent if root.name == "data" else root


def _data_root(root: Path) -> Path:
    return root if root.name == "data" else root / "data"


def _package_output_dir(root: Path) -> Path:
    return _data_root(root) / "ha_packages" / "auto"


def _default_db_path(root: Path) -> Path:
    return _data_root(root) / "activity.sqlite"


def _db_arg_for_prompt(core_root: Path, db_path: Path) -> str:
    try:
        return str(db_path.resolve().relative_to(core_root.resolve()))
    except ValueError:
        return str(db_path)


def _get_setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else default


def _set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO settings(key, value, updated_at) VALUES(?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, utc_now()),
    )


def _get_state(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM daemon_state WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else default


def _set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO daemon_state(key, value, updated_at) VALUES(?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value, utc_now()),
    )


def _bool_setting(conn: sqlite3.Connection, key: str, default: bool = False) -> bool:
    fallback = "true" if default else "false"
    return _get_setting(conn, key, fallback).strip().lower() in {"1", "true", "yes", "on"}


def _int_setting(conn: sqlite3.Connection, key: str, default: int) -> int:
    try:
        return int(_get_setting(conn, key, str(default)))
    except ValueError:
        return int(default)


def _command_descriptor(command: list[str], package_id: str, profile: str) -> dict:
    return {
        "executable": Path(command[0]).name if command else "hermes",
        "profile": profile,
        "toolsets": DEFAULT_HA_AUTO_TOOLSETS,
        "package_id": package_id,
        "prompt_policy": "inline_sanitized_json_analysis_only",
    }


def _hermes_executable() -> Path | None:
    configured = os.environ.get("PAM_HERMES_BIN")
    candidates = []
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.append(Path.home() / "bin" / "hermes")
    found = shutil.which("hermes")
    if found:
        candidates.append(Path(found))
    for candidate in candidates:
        if candidate.exists() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _read_env_values(path: Path) -> dict:
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _profile_messaging_isolated(config_text: str, env_values: dict) -> bool:
    encoded_env_keys = " ".join(env_values.keys()).lower()
    encoded_config = config_text.lower()
    platform_terms = ("feishu", "discord", "telegram", "slack", "dingtalk", "wecom")
    active_env = any(term in encoded_env_keys for term in platform_terms)
    active_config = any(f"{term}:" in encoded_config and "not configured" not in encoded_config for term in platform_terms)
    return not active_env and not active_config


def _hermes_run_result(
    status: str,
    last_error: str,
    *,
    hermes_result_status: str = "",
    memories_written_count: int = 0,
    skipped_reason: str = "",
    written_candidate_count: int = 0,
    skipped_candidate_count: int = 0,
    written_candidate_types: str = "",
    skipped_reasons: str = "",
    write_audit_summary: str = "",
    written_memory_summaries: str = "",
    hermes_result_json: dict | None = None,
) -> dict:
    result_json = hermes_result_json or {}
    memories_written = max(0, _safe_int(memories_written_count, 0))
    return {
        "status": status,
        "last_error": _safe_error(last_error),
        "hermes_result_status": _safe_error(hermes_result_status),
        "memories_written_count": memories_written,
        "skipped_reason": _safe_error(skipped_reason),
        "written_candidate_count": max(0, _safe_int(written_candidate_count, memories_written)),
        "skipped_candidate_count": max(0, _safe_int(skipped_candidate_count, 0)),
        "written_candidate_types": written_candidate_types if isinstance(written_candidate_types, str) else _safe_audit_list_json(written_candidate_types),
        "skipped_reasons": skipped_reasons if isinstance(skipped_reasons, str) else _safe_audit_list_json(skipped_reasons),
        "write_audit_summary": _safe_error(write_audit_summary),
        "written_memory_summaries": written_memory_summaries if isinstance(written_memory_summaries, str) else _safe_memory_summary_list_json(written_memory_summaries),
        "hermes_result_json": json.dumps(result_json, ensure_ascii=False)[:1000] if result_json else "",
        "write_verified": False,
    }


def _extract_hermes_json(output: str) -> dict:
    text = str(output or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass
    for line in reversed(text.splitlines()):
        candidate = line.strip()
        if not (candidate.startswith("{") and candidate.endswith("}")):
            continue
        try:
            parsed = json.loads(candidate)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            continue
    for index in reversed([i for i, char in enumerate(text) if char == "{"]):
        candidate = text[index:]
        try:
            parsed = json.loads(candidate)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            continue
    return {}


def _sanitize_hermes_result_json(value: dict) -> dict:
    allowed = {
        "status",
        "package_id",
        "memories_written_count",
        "written_candidate_count",
        "skipped_candidate_count",
        "written_candidate_types",
        "skipped_reasons",
        "write_audit_summary",
        "written_memory_summaries",
        "eligible_candidate_count",
        "review_only_candidate_count",
        "ephemeral_candidate_count",
        "skipped_reason",
        "error",
        "failure_reason",
    }
    output = {}
    for key in allowed:
        if key not in value:
            continue
        child = value.get(key)
        if isinstance(child, (int, float)):
            output[key] = child
        elif isinstance(child, list):
            if key == "written_memory_summaries":
                output[key] = _decode_memory_summary_list(_safe_memory_summary_list_json(child))
            else:
                output[key] = _decode_audit_list(_safe_audit_list_json(child))
        else:
            output[key] = _safe_error(str(child))
    return output


def _safe_audit_list_json(value) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        items = [f"{key}:{val}" for key, val in value.items()]
    elif isinstance(value, list):
        items = value
    else:
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                items = parsed
            elif isinstance(parsed, dict):
                items = [f"{key}:{val}" for key, val in parsed.items()]
            else:
                items = [text]
        except json.JSONDecodeError:
            items = [part.strip() for part in re.split(r"[,;，；]", text) if part.strip()]
    safe_items: list[str] = []
    for item in items:
        safe = _safe_error(str(item or ""))[:80]
        if safe and safe not in safe_items:
            safe_items.append(safe)
    return json.dumps(safe_items[:12], ensure_ascii=False)


def _decode_audit_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [_safe_error(str(item))[:80] for item in value if _safe_error(str(item))]
    try:
        parsed = json.loads(str(value))
        if isinstance(parsed, list):
            return [_safe_error(str(item))[:80] for item in parsed if _safe_error(str(item))]
    except json.JSONDecodeError:
        pass
    text = _safe_error(str(value))
    return [text[:80]] if text else []


def _safe_memory_summary_list_json(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        items = value
    else:
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            parsed = json.loads(text)
            items = parsed if isinstance(parsed, list) else [text]
        except json.JSONDecodeError:
            items = [part.strip() for part in re.split(r"[\n;；]", text) if part.strip()]
    safe_items: list[str] = []
    for item in items:
        safe = _safe_error(str(item or ""))[:180]
        if safe and safe not in safe_items:
            safe_items.append(safe)
    return json.dumps(safe_items[:MAX_MEMORY_REPORT_SUMMARIES], ensure_ascii=False)


def _decode_memory_summary_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [_safe_error(str(item))[:180] for item in value if _safe_error(str(item))]
    try:
        parsed = json.loads(str(value))
        if isinstance(parsed, list):
            return [_safe_error(str(item))[:180] for item in parsed if _safe_error(str(item))]
    except json.JSONDecodeError:
        pass
    text = _safe_error(str(value))
    return [text[:180]] if text else []


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _safe_error(value: str) -> str:
    collapsed = " ".join(str(value or "").split())
    collapsed = SECRET_RE.sub("[secret]", collapsed)
    collapsed = LOCAL_PATH_RE.sub("[path]", collapsed)
    collapsed = HASH_RE.sub("[hash]", collapsed)
    collapsed = EMAIL_RE.sub("[email]", collapsed)
    collapsed = PHONE_RE.sub("[phone]", collapsed)
    return collapsed[:500]
