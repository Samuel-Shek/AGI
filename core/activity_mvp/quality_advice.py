from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import quality as quality_module
from .document_changes import document_authorization_lock
from .ha_automation import is_application_support_runtime
from .ha_source import (
    _ensure_ha_authorization_lock,
    _get_package_locked,
    generate_package,
)
from .pipeline import utc_now
from .privacy import redact_for_storage
from .quality import quality_report
from .retention import (
    HERMES_HANDOFF_MAX_RUNTIME_SECONDS,
    retention_handoff_admission,
)
from .storage import init_schema
from .timeline_export import _cleaned_timeline_export_locked


DEFAULT_QUALITY_ADVICE_ENABLED = True
DEFAULT_QUALITY_ADVICE_INTERVAL_HOURS = 24
DEFAULT_QUALITY_ADVICE_PROFILE = "activity-quality-analysis"
DEFAULT_QUALITY_ADVICE_TOOLSETS = "clarify"
DEFAULT_QUALITY_ADVICE_TIMEOUT_SECONDS = HERMES_HANDOFF_MAX_RUNTIME_SECONDS
SECRET_RE = re.compile(r"(-----BEGIN .*PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{20,}\b)", re.I)
LOCAL_PATH_RE = re.compile(
    r"((?:/(?:Users|private|Volumes|tmp|var|Applications|Library|System|opt|usr|etc)/|~/)[^\s\"'`，。；;]*)",
    re.I,
)
HASH_RE = re.compile(r"\b[a-f0-9]{32,128}\b", re.I)
FORBIDDEN_KEY_RE = re.compile(r"(raw|screenshot|frame|ocr|sqlite|db|log|path|secret|token|password|key)", re.I)


def ensure_quality_advice_defaults(conn: sqlite3.Connection) -> None:
    init_schema(conn)
    now = utc_now()
    for key, value in {
        "quality_advice_enabled": "true" if DEFAULT_QUALITY_ADVICE_ENABLED else "false",
        "quality_advice_interval_hours": str(DEFAULT_QUALITY_ADVICE_INTERVAL_HOURS),
        "quality_advice_hermes_profile": DEFAULT_QUALITY_ADVICE_PROFILE,
    }.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value, updated_at) VALUES(?, ?, ?)",
            (key, value, now),
        )
    for key, value in {
        "quality_advice_status": "ready" if DEFAULT_QUALITY_ADVICE_ENABLED else "disabled",
        "quality_advice_last_run_at": "",
        "quality_advice_last_error": "",
        "quality_advice_last_report": "",
    }.items():
        conn.execute(
            "INSERT OR IGNORE INTO daemon_state(key, value, updated_at) VALUES(?, ?, ?)",
            (key, value, now),
        )


def configure_quality_advice(
    conn: sqlite3.Connection,
    root_dir: str | Path,
    *,
    enabled: bool | None = None,
    profile: str | None = None,
    interval_hours: int | None = None,
) -> dict:
    ensure_quality_advice_defaults(conn)
    if enabled is not None:
        _set_setting(conn, "quality_advice_enabled", "true" if enabled else "false")
        _set_state(conn, "quality_advice_status", "ready" if enabled else "disabled")
        _set_state(conn, "quality_advice_last_error", "")
    if profile is not None:
        clean_profile = profile.strip()
        if not clean_profile:
            raise ValueError("Hermes quality advice profile cannot be empty")
        _set_setting(conn, "quality_advice_hermes_profile", clean_profile)
    if interval_hours is not None:
        hours = int(interval_hours)
        if hours < 1:
            raise ValueError("quality advice interval must be at least 1 hour")
        _set_setting(conn, "quality_advice_interval_hours", str(hours))
    conn.commit()
    return quality_advice_status(conn, root_dir)


def quality_advice_status(conn: sqlite3.Connection, root_dir: str | Path) -> dict:
    ensure_quality_advice_defaults(conn)
    root = Path(root_dir)
    enabled = _bool_setting(conn, "quality_advice_enabled", DEFAULT_QUALITY_ADVICE_ENABLED)
    profile = _get_setting(conn, "quality_advice_hermes_profile", DEFAULT_QUALITY_ADVICE_PROFILE)
    interval_hours = max(1, _int_setting(conn, "quality_advice_interval_hours", DEFAULT_QUALITY_ADVICE_INTERVAL_HOURS))
    runtime_allowed = is_application_support_runtime(root)
    latest = _latest_run(conn)
    due = enabled and runtime_allowed and _run_due(latest, interval_hours)
    preflight = validate_quality_advice(conn, root)
    state_status = _get_state(conn, "quality_advice_status", "ready" if enabled else "disabled")
    last_error = _get_state(conn, "quality_advice_last_error", "")
    latest_summary = _latest_summary(latest)
    if not enabled:
        label = "未启用"
        kind = "neutral"
        summary = "Hermes 质量顾问未启用。"
    elif not runtime_allowed:
        label = "未在 live runtime 执行"
        kind = "warning"
        summary = "已启用，但当前不是 Application Support live runtime，自动质量分析会跳过。"
    elif not preflight["ready"]:
        label = "依赖异常"
        kind = "warning"
        summary = f"已启用；{preflight['summary']}"
    elif latest and str(latest.get("status") or "") == "failed":
        label = "最近失败"
        kind = "warning"
        summary = f"最近质量分析失败：{latest.get('last_error') or last_error or 'unknown'}"
    elif latest and latest.get("completed_at"):
        label = "正常"
        kind = "ok"
        summary = f"已启用；最近建议：{latest_summary}。"
    else:
        label = "等待首次分析"
        kind = "ok"
        summary = "已启用；等待下一个完整本地日窗口。"
    return {
        "enabled": enabled,
        "profile": profile,
        "interval_hours": interval_hours,
        "status": state_status,
        "label": label,
        "kind": kind,
        "summary": summary,
        "runtime_allowed": runtime_allowed,
        "runtime_reason": "application_support_live_runtime" if runtime_allowed else "repo_local_guard",
        "preflight": preflight,
        "due": due,
        "last_run_at": _get_state(conn, "quality_advice_last_run_at", "") or (latest.get("completed_at") if latest else ""),
        "last_error": last_error,
        "latest_recommendation_count": int(latest.get("recommendation_count") or 0) if latest else 0,
        "latest_issue_count": int(latest.get("issue_count") or 0) if latest else 0,
        "latest_memory_candidate_count": int(latest.get("memory_candidate_count") or 0) if latest else 0,
        "latest_document_match_count": int(latest.get("document_match_count") or 0) if latest else 0,
        "latest_summary": latest_summary,
        "latest_run": latest or {},
    }


def validate_quality_advice(conn: sqlite3.Connection, root_dir: str | Path) -> dict:
    ensure_quality_advice_defaults(conn)
    root = Path(root_dir)
    profile = _get_setting(conn, "quality_advice_hermes_profile", DEFAULT_QUALITY_ADVICE_PROFILE)
    hermes_bin = _hermes_executable()
    profile_dir = Path.home() / ".hermes" / "profiles" / profile
    checks = {
        "live_runtime": is_application_support_runtime(root),
        "hermes_available": hermes_bin is not None,
        "profile_exists": profile_dir.exists(),
    }
    missing = [key for key, ok in checks.items() if not ok]
    ready = not missing
    return {
        "ready": ready,
        "summary": "Hermes 质量顾问依赖正常。" if ready else "Hermes 质量顾问依赖异常：" + "、".join(missing),
        "profile": profile,
        "hermes": {
            "available": checks["hermes_available"],
            "executable": Path(hermes_bin).name if hermes_bin else "",
        },
        "runtime": {
            "allowed": checks["live_runtime"],
            "reason": "application_support_live_runtime" if checks["live_runtime"] else "repo_local_guard",
        },
        "checks": checks,
        "missing": missing,
    }


def run_quality_advice_once(
    conn: sqlite3.Connection,
    root_dir: str | Path,
    *,
    db_path: str | Path | None = None,
    start: str | None = None,
    end: str | None = None,
    force: bool = False,
    trigger: bool = True,
) -> dict:
    ensure_quality_advice_defaults(conn)
    # INSERT OR IGNORE defaults start a SQLite write transaction even when all
    # rows already exist.  End that tiny transaction before the regex-heavy
    # daily report so capture/status writers are not blocked by pure analysis.
    conn.commit()
    root = Path(root_dir)
    if not _bool_setting(conn, "quality_advice_enabled", DEFAULT_QUALITY_ADVICE_ENABLED):
        _set_state(conn, "quality_advice_status", "disabled")
        _set_state(conn, "quality_advice_last_error", "")
        conn.commit()
        return {"status": "disabled", "generated": False, "triggered": False}
    if not force and not is_application_support_runtime(root):
        _set_state(conn, "quality_advice_status", "skipped")
        _set_state(conn, "quality_advice_last_error", "")
        conn.commit()
        return {"status": "skipped", "reason": "repo_local_not_live_runtime", "generated": False, "triggered": False}

    if not start or not end:
        start, end, run_key = previous_complete_local_day()
    else:
        run_key = _run_key_for_range(start, end)
    latest_for_key = _run_for_key(conn, run_key)
    if latest_for_key and latest_for_key.get("status") == "completed" and not force:
        return {
            "status": "duplicate",
            "run_key": run_key,
            "package_id": latest_for_key.get("package_id") or "",
            "generated": False,
            "triggered": False,
        }

    profile = _get_setting(conn, "quality_advice_hermes_profile", DEFAULT_QUALITY_ADVICE_PROFILE)
    db = Path(db_path) if db_path else _default_db_path(root)
    admission = retention_handoff_admission(conn)
    if not admission["allowed"]:
        return _defer_quality_for_retention(
            conn,
            admission,
            run_key=run_key,
            package_id=str((latest_for_key or {}).get("package_id") or ""),
            generated=False,
        )
    report = quality_report(conn, start, end)
    try:
        result = generate_package(
            conn,
            start,
            end,
            _package_output_dir(root),
            write_markdown=False,
            generation_mode="quality_advice",
        )
    except Exception:
        error = "quality_advice_package_generation_failed"
        # Package generation releases its shared authorization lease before
        # propagating an exception. A delete/revoke may therefore have won.
        # Never reinsert the run or copy the pre-delete report back into its
        # summary. Finalize only an existing row, under a fresh shared lease;
        # an exclusive delete that wins first leaves no row to update.
        conn.commit()
        admission = retention_handoff_admission(conn)
        if not admission["allowed"]:
            return _defer_quality_for_retention(
                conn,
                admission,
                run_key=run_key,
                package_id=str((latest_for_key or {}).get("package_id") or ""),
                generated=False,
            )
        _ensure_ha_authorization_lock(conn)
        with document_authorization_lock(conn, exclusive=False):
            admission = retention_handoff_admission(conn)
            if not admission["allowed"]:
                return _defer_quality_for_retention(
                    conn,
                    admission,
                    run_key=run_key,
                    package_id=str((latest_for_key or {}).get("package_id") or ""),
                    generated=False,
                )
            _upsert_run(
                conn,
                run_key=run_key,
                start=start,
                end=end,
                profile=profile,
                status="failed",
                package_id=str((latest_for_key or {}).get("package_id") or ""),
                report={},
                advice={},
                last_error=error,
                completed_at=utc_now(),
                insert_if_missing=False,
            )
            _set_state(conn, "quality_advice_status", "error")
            _set_state(conn, "quality_advice_last_error", error)
            conn.commit()
        return {"status": "failed", "error": error, "generated": False, "triggered": False}

    package_id = str(result["package"]["package_id"])
    generated_at = utc_now()
    # generate_package() releases its lease before returning. Revalidate and
    # create the run under a fresh shared lease so a delete that wins between
    # those operations cannot be followed by a new overlapping run row.
    admission = retention_handoff_admission(conn)
    if not admission["allowed"]:
        return _defer_quality_for_retention(
            conn,
            admission,
            run_key=run_key,
            package_id=package_id,
            generated=True,
        )
    _ensure_ha_authorization_lock(conn)
    with document_authorization_lock(conn, exclusive=False):
        admission = retention_handoff_admission(conn)
        if not admission["allowed"]:
            return _defer_quality_for_retention(
                conn,
                admission,
                run_key=run_key,
                package_id=package_id,
                generated=True,
            )
        generated_package = _get_package_locked(conn, package_id)
        generated_source_status = str(
            generated_package.get("source_status") or "unknown"
        )
        if generated_source_status != "active":
            error = "quality_advice_source_unavailable"
            _set_state(conn, "quality_advice_status", "error")
            _set_state(conn, "quality_advice_last_error", error)
            conn.commit()
            return {
                "status": "failed",
                "run_key": run_key,
                "package_id": package_id,
                "source_status": generated_source_status,
                "error": error,
                "generated": True,
                "triggered": False,
                "raw_access": False,
            }
        _upsert_run(
            conn,
            run_key=run_key,
            start=start,
            end=end,
            profile=profile,
            status="generated",
            package_id=package_id,
            report=report,
            advice={},
            last_error="",
            completed_at="",
        )
        _set_state(conn, "quality_advice_status", "generated")
        conn.commit()
    if not trigger:
        return {
            "status": "generated",
            "run_key": run_key,
            "package_id": package_id,
            "generated": True,
            "triggered": False,
        }

    preflight = validate_quality_advice(conn, root)
    if not preflight["ready"] and not force:
        error = _safe_text(preflight["summary"], max_len=500)
        _upsert_run(
            conn,
            run_key=run_key,
            start=start,
            end=end,
            profile=profile,
            status="failed",
            package_id=package_id,
            report=report,
            advice={},
            last_error=error,
            completed_at=utc_now(),
            insert_if_missing=False,
        )
        _set_state(conn, "quality_advice_status", "error")
        _set_state(conn, "quality_advice_last_error", error)
        conn.commit()
        return {
            "status": "failed",
            "run_key": run_key,
            "package_id": package_id,
            "error": error,
            "generated": True,
            "triggered": False,
        }

    # validate_quality_advice() refreshes INSERT OR IGNORE defaults.  Release
    # that tiny writer transaction before waiting for the shared authorization
    # lease; otherwise an exclusive revoker that already owns the file lock
    # could wait on this connection's SQLite writer slot while this connection
    # waits on the revoker.
    conn.commit()
    admission = retention_handoff_admission(conn)
    if not admission["allowed"]:
        return _defer_quality_for_retention(
            conn,
            admission,
            run_key=run_key,
            package_id=package_id,
            generated=True,
        )
    _ensure_ha_authorization_lock(conn)
    with document_authorization_lock(conn, exclusive=False):
        # Recheck after acquiring the lease as well: the caller may have waited
        # behind an exclusive revoker long enough to enter the drain window.
        admission = retention_handoff_admission(conn)
        if not admission["allowed"]:
            return _defer_quality_for_retention(
                conn,
                admission,
                run_key=run_key,
                package_id=package_id,
                generated=True,
            )
        # The preliminary report was only useful for local generated/error
        # state. Rebuild every field that can enter the Hermes payload under
        # this transport lease so a delete between that report and package
        # generation cannot leak the pre-delete report.
        report = _quality_report_for_handoff_locked(conn, start, end)
        # Do not trust the in-memory object returned by generate_package(): a
        # delete/reclassification may have won after generation released its
        # lease.  Re-read under the transport lease without nesting flock.
        package = _get_package_locked(conn, package_id)
        source_status = str(package.get("source_status") or "unknown")
        if source_status != "active":
            error = "quality_advice_source_unavailable"
            completed_at = utc_now()
            _upsert_run(
                conn,
                run_key=run_key,
                start=start,
                end=end,
                profile=profile,
                status="failed",
                package_id=package_id,
                report=report,
                advice={},
                last_error=error,
                completed_at=completed_at,
                insert_if_missing=False,
            )
            _set_state(conn, "quality_advice_status", "error")
            _set_state(conn, "quality_advice_last_error", error)
            conn.commit()
            return {
                "status": "failed",
                "run_key": run_key,
                "package_id": package_id,
                "source_status": source_status,
                "error": error,
                "generated": True,
                "triggered": False,
                "raw_access": False,
            }

        command = build_quality_advice_command(
            package_id=package_id,
            start=start,
            end=end,
            run_key=run_key,
            hermes_profile=profile,
            root_dir=root,
            db_path=db,
            analysis_payload=_quality_advice_input(report, package),
        )
        _set_state(conn, "quality_advice_status", "pending")
        conn.commit()
        # Keep the same lease through the external handoff.  A competing
        # exclusive revocation resumes only after Hermes returns and the final
        # existing run row is committed. This keeps delete-range ordered after
        # finalization and forbids a deleted run from being inserted again.
        hermes_result = _run_hermes_command(command, cwd=_core_root(root))
        completed_at = utc_now()
        status = hermes_result["status"]
        advice = hermes_result["advice"]
        last_error = hermes_result["last_error"]
        _upsert_run(
            conn,
            run_key=run_key,
            start=start,
            end=end,
            profile=profile,
            status=status,
            package_id=package_id,
            report=report,
            advice=advice,
            last_error=last_error,
            completed_at=completed_at,
            insert_if_missing=False,
        )
        _set_state(conn, "quality_advice_status", "ok" if status == "completed" else "error")
        _set_state(conn, "quality_advice_last_run_at", completed_at)
        _set_state(conn, "quality_advice_last_error", last_error)
        _set_state(conn, "quality_advice_last_report", json.dumps(_compact_report(run_key, package_id, report, advice, status), ensure_ascii=False))
        conn.commit()
    return {
        "status": status,
        "run_key": run_key,
        "package_id": package_id,
        "generated": True,
        "triggered": status == "completed",
        "last_error": last_error,
        "issue_count": _list_count(advice.get("issues")),
        "recommendation_count": _list_count(advice.get("recommendations")),
        "generated_at": generated_at,
        "completed_at": completed_at,
    }


def _defer_quality_for_retention(
    conn: sqlite3.Connection,
    admission: dict,
    *,
    run_key: str,
    package_id: str,
    generated: bool,
) -> dict:
    _set_state(conn, "quality_advice_status", "waiting_for_retention")
    _set_state(conn, "quality_advice_last_error", "")
    conn.commit()
    return {
        "status": "deferred",
        "reason": str(admission.get("reason") or "retention_deadline_imminent"),
        "run_key": run_key,
        "package_id": package_id,
        "generated": bool(generated),
        "triggered": False,
        "retention_deadline_at": str(admission.get("deadline_at") or ""),
        "retention_remaining_seconds": admission.get("remaining_seconds"),
        "raw_access": False,
    }


def build_quality_advice_command(
    *,
    package_id: str,
    start: str,
    end: str,
    run_key: str,
    hermes_profile: str,
    root_dir: str | Path,
    db_path: str | Path,
    hermes_bin: str | Path | None = None,
    toolsets: str = DEFAULT_QUALITY_ADVICE_TOOLSETS,
    analysis_payload: dict | None = None,
) -> list[str]:
    if str(toolsets or "").strip() != DEFAULT_QUALITY_ADVICE_TOOLSETS:
        raise ValueError("quality advice only permits the clarify toolset")
    executable = Path(hermes_bin or os.environ.get("PAM_HERMES_BIN") or (Path.home() / "bin" / "hermes"))
    prompt = _hermes_prompt(
        package_id=package_id,
        start=start,
        end=end,
        run_key=run_key,
        hermes_profile=hermes_profile,
        analysis_payload=analysis_payload or {},
    )
    return [str(executable), "--profile", hermes_profile, "--ignore-rules", "-t", DEFAULT_QUALITY_ADVICE_TOOLSETS, "-z", prompt]


def previous_complete_local_day(now: datetime | None = None) -> tuple[str, str, str]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local_current = current.astimezone()
    local_end = local_current.replace(hour=0, minute=0, second=0, microsecond=0)
    local_start = local_end - timedelta(days=1)
    start_utc = local_start.astimezone(timezone.utc)
    end_utc = local_end.astimezone(timezone.utc)
    run_key = local_start.strftime("%Y-%m-%d")
    return start_utc.isoformat(timespec="seconds"), end_utc.isoformat(timespec="seconds"), run_key


def _hermes_prompt(
    *,
    package_id: str,
    start: str,
    end: str,
    run_key: str,
    hermes_profile: str,
    analysis_payload: dict,
) -> str:
    safe_input = _sanitize_value(analysis_payload)
    encoded_input = _bounded_quality_json(safe_input)
    return "\n".join(
        [
            "你是 Personal AI Monitor 的 Hermes 质量顾问，只负责分析和建议，不直接改代码、不直接改规则、不写长期记忆。",
            "你没有终端、文件或 memory 工具。只能分析父进程内联的脱敏 JSON；禁止读取 SQLite、raw vault、截图、OCR 原文、日志或本地文档。",
            f"profile: {hermes_profile}",
            f"run_key: {run_key}",
            f"time_range: {start} -> {end}",
            f"analysis_input_json: {encoded_input}",
            "分析重点：识别/清洗误杀、漏识别、DocumentSignal 高召回、长期记忆写入健康、重复/缺口、产品自优化建议。",
            "长期记忆质量要重点看 confirmed_action/context_clue/noise_or_reference、eligible/review_only/ephemeral 分布、单证据 eligible、Unknown eligible、重复合并数、Hermes 实际写入数；不要只看候选总数。",
            "尤其检查 Finder/桌面/侧边栏、任务列表旧项、浏览器标签、媒体文件名、只读 Preview/Photos 是否被错误升级成 confirmed_action。",
            "输出必须是 JSON；不要输出 Markdown，不要包含完整路径、原始 OCR、截图描述、密钥、验证码、付款信息或私密聊天原文。",
            "JSON 结构：status=completed|no_action|failed, summary, issues=[{issue_type,severity,evidence_count,why_it_matters}], recommendations=[{area,priority,proposal,expected_impact}], document_signal_advice, memory_candidate_advice, next_actions。",
            "如果没有建议，返回 status=no_action、summary 说明原因，issues/recommendations/next_actions 为空数组。",
        ]
    )


def _quality_report_for_handoff_locked(
    conn: sqlite3.Connection,
    start: str | None,
    end: str | None,
) -> dict:
    """Rebuild the quality report while the caller holds the transport lease."""
    # quality_report() and cleaned_timeline_export() are public projections
    # that acquire their own flock. Use only lock-private building blocks here:
    # a nested flock context could unlock the transport lease before Hermes
    # returns.
    export = _cleaned_timeline_export_locked(conn, start, end)
    quality = export.get("quality_diagnostics") or {}
    digest = export.get("learning_digest") or {}
    learnability = digest.get("learnability_counts") or {}
    memory_candidates = digest.get("memory_candidates") or []
    screen_region_summary = (
        digest.get("screen_region_summary")
        if isinstance(digest.get("screen_region_summary"), dict)
        else {}
    )
    memory_write_health = quality_module._memory_write_health(digest)
    memory_governance_trend = quality_module._memory_governance_trend(conn)
    document_signals = quality_module._document_signal_counts(conn, start, end)
    attribution = quality_module._attribution_health(conn, start, end)
    feedback = quality_module.list_quality_feedback(conn, start, end)
    feedback_counts = Counter(item["issue_type"] for item in feedback)
    candidate_skip_reason = quality_module._candidate_skip_reason(
        memory_candidates,
        quality,
        learnability,
    )
    report = {
        "time_range": {"from": start, "to": end},
        "status": quality_module._quality_status(
            quality,
            document_signals,
            attribution,
            feedback,
        ),
        "summary": quality_module._quality_summary(
            quality,
            document_signals,
            memory_candidates,
            feedback,
            memory_write_health,
        ),
        "cleaning": {
            "activity_count": int(quality.get("activity_count") or 0),
            "normal_clear_count": max(
                0,
                int(quality.get("activity_count") or 0)
                - int(quality.get("partial_redacted_count") or 0)
                - int(quality.get("communication_redaction_count") or 0),
            ),
            "partial_redacted_count": int(quality.get("partial_redacted_count") or 0),
            "communication_redaction_count": int(
                quality.get("communication_redaction_count") or 0
            ),
            "privacy_event_count": int(quality.get("privacy_event_count") or 0),
            "gap_event_count": int(quality.get("gap_event_count") or 0),
            "duplicate_event_count": int(quality.get("duplicate_event_count") or 0),
            "duplicate_frame_count": int(quality.get("duplicate_frame_count") or 0),
            "short_or_empty_content_count": int(
                quality.get("short_or_empty_content_count") or 0
            ),
            "near_cleaned_content_cap_count": int(
                quality.get("near_cleaned_content_cap_count") or 0
            ),
            "short_content_buckets": quality.get("short_content_buckets") or {},
        },
        "document_signals": document_signals,
        "attribution_health": attribution,
        "learning": {
            "summary": str(digest.get("summary") or ""),
            "learnability_counts": learnability,
            "workflow_summary": digest.get("workflow_summary") or [],
            "screen_region_summary": screen_region_summary,
            "memory_candidate_count": len(memory_candidates),
            "display_candidate_count": len(memory_candidates),
            "exposed_candidate_count": int(
                memory_write_health.get("exposed_candidate_count")
                or len(memory_candidates)
            ),
            "total_candidate_count": int(
                memory_write_health.get("total_candidate_count")
                or len(memory_candidates)
            ),
            "eligible_candidate_count": int(
                memory_write_health.get("eligible_candidate_count") or 0
            ),
            "write_input_count": int(
                memory_write_health.get("write_input_count") or 0
            ),
            "single_evidence_eligible_count": int(
                memory_write_health.get("single_evidence_eligible_count") or 0
            ),
            "subject_risk_candidate_count": int(
                memory_write_health.get("subject_risk_candidate_count") or 0
            ),
            "memory_candidate_skip_reason": candidate_skip_reason,
            "memory_write_health": memory_write_health,
            "memory_governance_trend": memory_governance_trend,
            "memory_candidates": memory_candidates[:12],
        },
        "memory_write_health": memory_write_health,
        "memory_governance_trend": memory_governance_trend,
        "feedback": {
            "count": len(feedback),
            "issue_counts": dict(feedback_counts),
            "items": feedback[:50],
        },
        "raw_access": False,
        "cloud_analysis": "off",
    }
    report["recommendations"] = quality_module._recommendations(report)
    return report


def _quality_advice_input(report: dict, package: dict) -> dict:
    digest = package.get("learning_digest") if isinstance(package.get("learning_digest"), dict) else {}
    diagnostics = package.get("learnability_diagnostics") if isinstance(package.get("learnability_diagnostics"), dict) else {}
    candidates = digest.get("memory_candidates") if isinstance(digest.get("memory_candidates"), list) else []
    safe_candidates = []
    for candidate in candidates[:20]:
        if not isinstance(candidate, dict):
            continue
        safe_candidates.append(
            {
                key: candidate.get(key)
                for key in (
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
                )
            }
        )
    return {
        "quality_summary": {
            "status": report.get("status"),
            "summary": report.get("summary"),
            "time_range": report.get("time_range") if isinstance(report.get("time_range"), dict) else {},
            "cleaning": report.get("cleaning") if isinstance(report.get("cleaning"), dict) else {},
            "document_signals": report.get("document_signals") if isinstance(report.get("document_signals"), dict) else {},
            "learning": {
                key: (report.get("learning") or {}).get(key)
                for key in (
                    "learnability_counts",
                    "total_candidate_count",
                    "display_candidate_count",
                    "eligible_candidate_count",
                    "write_input_count",
                    "single_evidence_eligible_count",
                    "subject_risk_candidate_count",
                    "memory_candidate_skip_reason",
                )
            }
            if isinstance(report.get("learning"), dict)
            else {},
            "memory_write_health": report.get("memory_write_health")
            if isinstance(report.get("memory_write_health"), dict)
            else {},
            "attribution_health": {
                key: (report.get("attribution_health") or {}).get(key)
                for key in (
                    "status",
                    "confidence_label",
                    "frame_signal_count",
                    "known_app_ratio",
                    "low_confidence_ratio",
                    "summary",
                )
            }
            if isinstance(report.get("attribution_health"), dict)
            else {},
            "recommendations": (report.get("recommendations") or [])[:10]
            if isinstance(report.get("recommendations"), list)
            else [],
        },
        "package_summary": {
            "source_status": package.get("source_status"),
            "raw_access": False,
            "coverage_rate": package.get("coverage_rate"),
            "gap_summary": package.get("gap_summary"),
            "learnability_counts": digest.get("learnability_counts") or {},
            "memory_candidate_quality": digest.get("memory_candidate_quality") or {},
            "diagnostics": diagnostics,
            "memory_candidates": safe_candidates,
        },
    }


def _bounded_quality_json(value: object, *, max_chars: int = 24000) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(encoded) <= max_chars:
        return encoded
    source = value if isinstance(value, dict) else {}
    quality = source.get("quality_summary") if isinstance(source.get("quality_summary"), dict) else {}
    package = source.get("package_summary") if isinstance(source.get("package_summary"), dict) else {}
    compact = {
        "input_truncated": True,
        "quality_summary": {
            "status": quality.get("status"),
            "summary": quality.get("summary"),
            "learning": quality.get("learning") if isinstance(quality.get("learning"), dict) else {},
            "document_signals": quality.get("document_signals") if isinstance(quality.get("document_signals"), dict) else {},
            "memory_write_health": quality.get("memory_write_health")
            if isinstance(quality.get("memory_write_health"), dict)
            else {},
        },
        "package_summary": {
            "source_status": package.get("source_status"),
            "learnability_counts": package.get("learnability_counts")
            if isinstance(package.get("learnability_counts"), dict)
            else {},
            "memory_candidate_quality": package.get("memory_candidate_quality")
            if isinstance(package.get("memory_candidate_quality"), dict)
            else {},
            "memory_candidates": (package.get("memory_candidates") or [])[:5]
            if isinstance(package.get("memory_candidates"), list)
            else [],
        },
    }
    encoded = json.dumps(compact, ensure_ascii=False, sort_keys=True)
    if len(encoded) <= max_chars:
        return encoded
    minimal = {
        "input_truncated": True,
        "quality_status": quality.get("status"),
        "quality_summary": _safe_text(str(quality.get("summary") or ""), max_len=800),
        "source_status": package.get("source_status"),
    }
    return json.dumps(minimal, ensure_ascii=False, sort_keys=True)


def _run_hermes_command(command: list[str], *, cwd: Path) -> dict:
    try:
        completed = subprocess.run(
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=DEFAULT_QUALITY_ADVICE_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return {"status": "failed", "last_error": "hermes_executable_missing", "advice": {}}
    except subprocess.TimeoutExpired:
        return {"status": "failed", "last_error": "hermes_quality_advice_timeout", "advice": {}}
    if completed.returncode != 0:
        return {
            "status": "failed",
            "last_error": "hermes_quality_advice_failed",
            "advice": {},
        }
    parsed = _extract_json(completed.stdout)
    if not parsed:
        return {
            "status": "completed",
            "last_error": "",
            "advice": {"status": "unknown", "summary": "Hermes completed but returned no structured JSON.", "issues": [], "recommendations": [], "next_actions": []},
        }
    advice = _sanitize_advice(parsed)
    raw_status = str(advice.get("status") or "completed").strip().lower()
    status = "completed" if raw_status in {"completed", "ok", "success", "no_action"} else "failed"
    error = "" if status == "completed" else "hermes_quality_advice_failed"
    return {"status": status, "last_error": error, "advice": advice}


def _upsert_run(
    conn: sqlite3.Connection,
    *,
    run_key: str,
    start: str,
    end: str,
    profile: str,
    status: str,
    package_id: str,
    report: dict,
    advice: dict,
    last_error: str,
    completed_at: str,
    insert_if_missing: bool = True,
) -> bool:
    now = utc_now()
    summary = _safe_text(str(advice.get("summary") or report.get("summary") or ""), max_len=1000)
    learning = report.get("learning") or {}
    doc = report.get("document_signals") or {}
    memory_candidate_count = int(learning.get("memory_candidate_count") or 0)
    document_candidate_count = int(doc.get("document_candidate_count") or 0)
    document_match_count = int(doc.get("document_match_count") or 0)
    issue_count = _list_count(advice.get("issues"))
    recommendation_count = _list_count(advice.get("recommendations"))
    advice_json = _advice_json_for_storage(advice)
    safe_error = _safe_text(last_error, max_len=500)
    if not insert_if_missing:
        cursor = conn.execute(
            """
            UPDATE quality_advice_runs
            SET started_at = ?, ended_at = ?, profile = ?, status = ?,
                package_id = ?, memory_candidate_count = ?,
                document_candidate_count = ?, document_match_count = ?,
                issue_count = ?, recommendation_count = ?,
                summary_redacted = ?, advice_json = ?, last_error = ?,
                completed_at = ?, updated_at = ?
            WHERE run_key = ?
            """,
            (
                start,
                end,
                profile,
                status,
                package_id,
                memory_candidate_count,
                document_candidate_count,
                document_match_count,
                issue_count,
                recommendation_count,
                summary,
                advice_json,
                safe_error,
                completed_at,
                now,
                run_key,
            ),
        )
        return bool(cursor.rowcount)
    conn.execute(
        """
        INSERT INTO quality_advice_runs(
          id, run_key, started_at, ended_at, profile, status, package_id,
          memory_candidate_count, document_candidate_count, document_match_count,
          issue_count, recommendation_count, summary_redacted, advice_json,
          last_error, created_at, completed_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_key) DO UPDATE SET
          started_at = excluded.started_at,
          ended_at = excluded.ended_at,
          profile = excluded.profile,
          status = excluded.status,
          package_id = excluded.package_id,
          memory_candidate_count = excluded.memory_candidate_count,
          document_candidate_count = excluded.document_candidate_count,
          document_match_count = excluded.document_match_count,
          issue_count = excluded.issue_count,
          recommendation_count = excluded.recommendation_count,
          summary_redacted = excluded.summary_redacted,
          advice_json = excluded.advice_json,
          last_error = excluded.last_error,
          completed_at = excluded.completed_at,
          updated_at = excluded.updated_at
        """,
        (
            f"qadv_{uuid.uuid4().hex}",
            run_key,
            start,
            end,
            profile,
            status,
            package_id,
            memory_candidate_count,
            document_candidate_count,
            document_match_count,
            issue_count,
            recommendation_count,
            summary,
            advice_json,
            safe_error,
            now,
            completed_at,
            now,
        ),
    )
    return True


def _latest_run(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        """
        SELECT run_key, started_at, ended_at, profile, status, package_id,
               memory_candidate_count, document_candidate_count, document_match_count,
               issue_count, recommendation_count, summary_redacted, advice_json,
               last_error, completed_at, updated_at
        FROM quality_advice_runs
        ORDER BY datetime(updated_at) DESC
        LIMIT 1
        """
    ).fetchone()
    return dict(row) if row else None


def _run_for_key(conn: sqlite3.Connection, run_key: str) -> dict | None:
    row = conn.execute("SELECT * FROM quality_advice_runs WHERE run_key = ?", (run_key,)).fetchone()
    return dict(row) if row else None


def _run_due(latest: dict | None, interval_hours: int) -> bool:
    if not latest:
        return True
    value = str(latest.get("completed_at") or latest.get("updated_at") or "")
    if not value:
        return True
    try:
        last = datetime.fromisoformat(value)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - last.astimezone(timezone.utc) >= timedelta(hours=interval_hours)


def _latest_summary(latest: dict | None) -> str:
    if not latest:
        return "暂无"
    summary = str(latest.get("summary_redacted") or "").strip()
    if not summary:
        status = str(latest.get("status") or "unknown")
        issues = int(latest.get("issue_count") or 0)
        recommendations = int(latest.get("recommendation_count") or 0)
        return f"{status}，问题 {issues} 个，建议 {recommendations} 条"
    return summary


def _compact_report(run_key: str, package_id: str, report: dict, advice: dict, status: str) -> dict:
    return {
        "run_key": run_key,
        "package_id": package_id,
        "status": status,
        "quality_status": report.get("status") or "",
        "memory_candidate_count": int((report.get("learning") or {}).get("memory_candidate_count") or 0),
        "document_match_count": int((report.get("document_signals") or {}).get("document_match_count") or 0),
        "issue_count": _list_count(advice.get("issues")),
        "recommendation_count": _list_count(advice.get("recommendations")),
    }


def _extract_json(text: str) -> dict | None:
    stripped = (text or "").strip()
    if not stripped:
        return None
    candidates = [stripped]
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        candidates.append(stripped[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        return parsed if isinstance(parsed, dict) else None
    return None


def _sanitize_advice(value) -> dict:
    sanitized = _sanitize_value(value)
    if not isinstance(sanitized, dict):
        return {"status": "unknown", "summary": "Hermes returned a non-object result.", "issues": [], "recommendations": []}
    sanitized.setdefault("status", "completed")
    for key in ("issues", "recommendations", "next_actions"):
        if not isinstance(sanitized.get(key), list):
            sanitized[key] = []
    return sanitized


def _sanitize_value(value, *, key: str = "", depth: int = 0):
    if depth > 5:
        return ""
    if FORBIDDEN_KEY_RE.search(key or ""):
        return "[redacted]"
    if isinstance(value, str):
        return _safe_text(value, max_len=800)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, list):
        return [_sanitize_value(item, depth=depth + 1) for item in value[:20]]
    if isinstance(value, dict):
        output = {}
        for raw_key, raw_value in list(value.items())[:50]:
            clean_key = _safe_text(str(raw_key), max_len=80)
            output[clean_key] = _sanitize_value(raw_value, key=clean_key, depth=depth + 1)
        return output
    return _safe_text(str(value), max_len=400)


def _advice_json_for_storage(advice: dict) -> str:
    compact = _sanitize_advice(advice)
    text = json.dumps(compact, ensure_ascii=False)
    if len(text) <= 12000:
        return text
    trimmed = {
        "status": compact.get("status") or "unknown",
        "summary": compact.get("summary") or "",
        "issues": (compact.get("issues") or [])[:10],
        "recommendations": (compact.get("recommendations") or [])[:10],
        "next_actions": (compact.get("next_actions") or [])[:10],
    }
    return json.dumps(trimmed, ensure_ascii=False)


def _safe_text(text: str, *, max_len: int) -> str:
    redacted = SECRET_RE.sub("[REDACTED_SECRET]", str(text or ""))
    redacted = LOCAL_PATH_RE.sub("[REDACTED_PATH]", redacted)
    redacted = HASH_RE.sub("[REDACTED_HASH]", redacted)
    redacted = redact_for_storage(redacted, max_len=max_len)
    return redacted[:max_len]


def _list_count(value) -> int:
    return len(value) if isinstance(value, list) else 0


def _run_key_for_range(start: str, end: str) -> str:
    raw = f"{start}_{end}"
    safe = re.sub(r"[^0-9A-Za-z_-]+", "_", raw).strip("_")
    return safe[:160] or datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _core_root(root: Path) -> Path:
    return root.parent if root.name == "data" else root


def _data_root(root: Path) -> Path:
    return root if root.name == "data" else root / "data"


def _package_output_dir(root: Path) -> Path:
    return _data_root(root) / "ha_packages" / "quality_advice"


def _default_db_path(root: Path) -> Path:
    return _data_root(root) / "activity.sqlite"


def _db_arg_for_prompt(core_root: Path, db_path: Path) -> str:
    try:
        return str(db_path.resolve().relative_to(core_root.resolve()))
    except ValueError:
        return str(db_path)


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
