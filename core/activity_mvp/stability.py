from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import uuid
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any

from . import __version__ as CORE_VERSION
from .attribution_context import attribution_health_for_range
from .pipeline import parse_time, utc_now
from .storage import init_schema


SOAK_WINDOW_DAYS = 7
SOAK_RETENTION_DAYS = 31
RESOURCE_SAMPLE_INTERVAL_SECONDS = 15 * 60
SOAK_SNAPSHOT_INTERVAL_SECONDS = 15 * 60
REQUIRED_DRILLS = ("pause", "delete", "permission_revoke", "permission_restore")
ALLOWED_EVENT_TYPES = {"context_recovery", *REQUIRED_DRILLS}
ALLOWED_EVENT_RESULTS = {"passed", "failed", "skipped"}
SOAK_COMPONENTS = ("capture", "privacy", "storage", "context_fusion")
SOAK_GATE_COMPONENT_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "observation_window": ("capture",),
    "service_stability": ("capture",),
    "lock_unlock": ("capture",),
    "display_coverage": ("capture",),
    "raw_safety": ("privacy",),
    "attribution_quality": ("capture",),
    "resource_regression": ("storage",),
    "context_recovery": ("context_fusion",),
    "control_drills": ("capture", "privacy"),
}


def record_session_transition_evidence(
    conn: sqlite3.Connection,
    evidence: dict[str, Any],
    *,
    observed_at: str | None = None,
    daemon_start: bool = False,
) -> dict[str, Any]:
    """Persist only content-free session-state evidence when it changes."""
    init_schema(conn)
    now = observed_at or utc_now()
    state = _session_state(evidence.get("state"))
    on_console = evidence.get("on_console") if isinstance(evidence.get("on_console"), bool) else None
    source = _safe_token(evidence.get("source") or "session_state_helper") or "session_state_helper"
    reason = _safe_token(evidence.get("reason") or evidence.get("error") or f"explicit_{state}") or "unknown"
    capture_allowed = bool(state == "unlocked" and on_console is True and evidence.get("capture_allowed", True))
    latest = conn.execute(
        """
        SELECT * FROM session_transition_evidence
        ORDER BY datetime(observed_at) DESC, rowid DESC
        LIMIT 1
        """
    ).fetchone()
    previous_state = _session_state(latest["state"]) if latest else "none"
    same_evidence = bool(
        latest
        and previous_state == state
        and str(latest["source"] or "") == source
        and str(latest["reason"] or "") == reason
        and bool(latest["capture_allowed"]) == capture_allowed
        and _optional_bool(latest["on_console"]) == on_console
    )
    if same_evidence and not daemon_start:
        return _public_transition(latest)

    last_valid = conn.execute(
        """
        SELECT state, observed_at
        FROM session_transition_evidence
        WHERE state IN ('locked', 'unlocked')
        ORDER BY datetime(observed_at) DESC, rowid DESC
        LIMIT 1
        """
    ).fetchone()
    if state in {"locked", "unlocked"}:
        last_valid_state = state
        last_valid_at = now
    else:
        last_valid_state = str(last_valid["state"] or "") if last_valid else ""
        last_valid_at = str(last_valid["observed_at"] or "") if last_valid else ""
    transition_kind = "initial" if not latest else ("state_changed" if previous_state != state else "evidence_changed")
    transition_id = f"session_evidence_{uuid.uuid4().hex}"
    conn.execute(
        """
        INSERT INTO session_transition_evidence(
          id, observed_at, previous_state, state, on_console, source, reason,
          capture_allowed, transition_kind, daemon_start, last_valid_state, last_valid_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            transition_id,
            now,
            previous_state,
            state,
            None if on_console is None else int(on_console),
            source,
            reason,
            int(capture_allowed),
            transition_kind,
            int(bool(daemon_start)),
            last_valid_state,
            last_valid_at,
        ),
    )
    row = conn.execute("SELECT * FROM session_transition_evidence WHERE id = ?", (transition_id,)).fetchone()
    return _public_transition(row)


def session_transition_status(conn: sqlite3.Connection, *, limit: int = 12) -> dict[str, Any]:
    init_schema(conn)
    rows = conn.execute(
        """
        SELECT * FROM session_transition_evidence
        ORDER BY datetime(observed_at) DESC, rowid DESC
        LIMIT ?
        """,
        (max(1, min(int(limit), 50)),),
    ).fetchall()
    latest = _public_transition(rows[0]) if rows else _empty_transition()
    return {
        "status": "ok" if rows else "no_evidence",
        "latest": latest,
        "recent": [_public_transition(row) for row in rows],
        "evidence_count": len(rows),
        "capture_allowed": bool(latest.get("capture_allowed")),
        "raw_access": False,
    }


def record_capture_cycle(
    conn: sqlite3.Connection,
    root_dir: str | Path,
    result: dict[str, Any],
    *,
    observed_at: str | None = None,
) -> None:
    now = observed_at or utc_now()
    status = _safe_token(result.get("status") or "unknown") or "unknown"
    session_state = _session_state(_daemon_state(conn, "session_state"))
    raw_residue = _raw_residue_count(Path(root_dir))
    unresolved = _unresolved_raw_jobs(conn)
    conn.execute(
        """
        INSERT INTO soak_capture_cycles(
          id, observed_at, status, captured_count, skipped_count, error_count,
          session_state, raw_residue_count, unresolved_raw_jobs
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"soak_cycle_{uuid.uuid4().hex}",
            now,
            status,
            max(0, int(result.get("captured") or 0)),
            max(0, int(result.get("skipped") or 0)),
            max(0, int(result.get("errors") or 0)),
            session_state,
            raw_residue,
            unresolved,
        ),
    )
    maybe_record_resource_sample(conn, root_dir, observed_at=now)
    if _daily_snapshot_due(conn, now):
        _prune_soak_metrics(conn, now)
        refresh_daily_snapshot(conn, root_dir, observed_at=now)
    conn.commit()


def maybe_record_resource_sample(
    conn: sqlite3.Connection,
    root_dir: str | Path,
    *,
    observed_at: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    now = observed_at or utc_now()
    latest = conn.execute(
        "SELECT observed_at FROM soak_resource_samples ORDER BY datetime(observed_at) DESC LIMIT 1"
    ).fetchone()
    if latest and not force:
        try:
            age = (parse_time(now) - parse_time(str(latest["observed_at"]))).total_seconds()
        except (TypeError, ValueError):
            age = RESOURCE_SAMPLE_INTERVAL_SECONDS
        if age < RESOURCE_SAMPLE_INTERVAL_SECONDS:
            return {"status": "throttled", "raw_access": False}
    cpu_percent, rss_bytes = _process_resource_usage(os.getpid())
    disk_bytes = _bounded_directory_size(Path(root_dir))
    sample_id = f"soak_resource_{uuid.uuid4().hex}"
    conn.execute(
        """
        INSERT INTO soak_resource_samples(id, observed_at, cpu_percent, rss_bytes, disk_bytes, core_version)
        VALUES(?, ?, ?, ?, ?, ?)
        """,
        (sample_id, now, cpu_percent, rss_bytes, disk_bytes, CORE_VERSION),
    )
    return {
        "status": "recorded",
        "observed_at": now,
        "cpu_percent": cpu_percent,
        "rss_bytes": rss_bytes,
        "disk_bytes": disk_bytes,
        "raw_access": False,
    }


def record_resource_baseline(
    conn: sqlite3.Connection,
    *,
    source_version: str,
    cpu_percent: float | None,
    rss_bytes: int | None,
    disk_bytes: int | None,
    recorded_at: str | None = None,
) -> dict[str, Any]:
    init_schema(conn)
    baseline_id = f"soak_baseline_{uuid.uuid4().hex}"
    when = recorded_at or utc_now()
    conn.execute(
        """
        INSERT INTO soak_resource_baselines(
          id, recorded_at, source_version, cpu_percent, rss_bytes, disk_bytes
        ) VALUES(?, ?, ?, ?, ?, ?)
        """,
        (
            baseline_id,
            when,
            _safe_token(source_version) or "unknown",
            None if cpu_percent is None else max(0.0, float(cpu_percent)),
            None if rss_bytes is None else max(0, int(rss_bytes)),
            None if disk_bytes is None else max(0, int(disk_bytes)),
        ),
    )
    conn.commit()
    return {"status": "recorded", "baseline_id": baseline_id, "recorded_at": when, "raw_access": False}


def record_soak_evidence_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    result: str = "passed",
    minutes_saved: int = 0,
    source: str = "user_confirmed",
    occurred_at: str | None = None,
) -> dict[str, Any]:
    init_schema(conn)
    normalized_type = _safe_token(event_type)
    normalized_result = _safe_token(result)
    if normalized_type not in ALLOWED_EVENT_TYPES:
        raise ValueError(f"unsupported soak event type: {normalized_type or 'empty'}")
    if normalized_result not in ALLOWED_EVENT_RESULTS:
        raise ValueError(f"unsupported soak event result: {normalized_result or 'empty'}")
    event_id = f"soak_event_{uuid.uuid4().hex}"
    when = occurred_at or utc_now()
    conn.execute(
        """
        INSERT INTO soak_evidence_events(id, occurred_at, event_type, result, minutes_saved, source)
        VALUES(?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            when,
            normalized_type,
            normalized_result,
            max(0, min(int(minutes_saved or 0), 24 * 60)),
            _safe_token(source) or "user_confirmed",
        ),
    )
    conn.commit()
    return {"status": "recorded", "event_id": event_id, "event_type": normalized_type, "result": normalized_result, "raw_access": False}


def build_soak_report(
    conn: sqlite3.Connection,
    root_dir: str | Path,
    *,
    days: int = SOAK_WINDOW_DAYS,
    end_at: str | None = None,
) -> dict[str, Any]:
    init_schema(conn)
    end_dt = parse_time(end_at) if end_at else datetime.now(timezone.utc)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=timezone.utc)
    else:
        end_dt = end_dt.astimezone(timezone.utc)
    window_days = max(1, min(int(days), 31))
    requested_start_dt = end_dt - timedelta(days=window_days)
    baseline = _latest_baseline(conn)
    component_windows = component_baseline_windows(conn, requested_start_dt, end_dt, window_days)
    gate_windows = {
        code: _gate_effective_window(component_windows, dependencies, end_dt, window_days)
        for code, dependencies in SOAK_GATE_COMPONENT_DEPENDENCIES.items()
    }
    capture_start_dt = component_windows["capture"]["from_dt"]
    privacy_start_dt = component_windows["privacy"]["from_dt"]
    storage_start_dt = component_windows["storage"]["from_dt"]
    context_start_dt = component_windows["context_fusion"]["from_dt"]
    start_dt = requested_start_dt
    start = start_dt.isoformat()
    end = end_dt.isoformat()
    capture_start = capture_start_dt.isoformat()
    privacy_start = privacy_start_dt.isoformat()
    storage_start = storage_start_dt.isoformat()
    context_start = context_start_dt.isoformat()

    cycles = conn.execute(
        """
        SELECT observed_at, status, captured_count, skipped_count, error_count,
               session_state, raw_residue_count, unresolved_raw_jobs
        FROM soak_capture_cycles
        WHERE datetime(observed_at) >= datetime(?) AND datetime(observed_at) < datetime(?)
        ORDER BY datetime(observed_at) ASC, rowid ASC
        """,
        (capture_start, end),
    ).fetchall()
    cycle_statuses = [str(row["status"] or "unknown") for row in cycles]
    eligible_cycles = [row for row in cycles if str(row["session_state"] or "") == "unlocked"]
    error_cycles = sum(1 for row in eligible_cycles if int(row["error_count"] or 0) > 0 or str(row["status"] or "").startswith("error"))
    partial_cycles = sum(1 for row in eligible_cycles if str(row["status"] or "").startswith("partial"))
    healthy_cycles = max(0, len(eligible_cycles) - error_cycles - partial_cycles)
    availability_ratio = round(healthy_cycles / len(eligible_cycles), 4) if eligible_cycles else 0.0
    observed_days = len({_local_day(str(row["observed_at"] or "")) for row in cycles if str(row["observed_at"] or "")})
    elapsed_hours = float(component_windows["capture"]["elapsed_hours"] or 0.0)
    capture_ready = bool(component_windows["capture"]["window_ready"] and observed_days >= window_days)
    privacy_ready = bool(component_windows["privacy"]["window_ready"])
    storage_ready = bool(component_windows["storage"]["window_ready"])
    context_ready = bool(component_windows["context_fusion"]["window_ready"])
    max_error_streak = _max_error_streak(cycle_statuses)

    transitions = conn.execute(
        """
        SELECT previous_state, state, reason, observed_at
        FROM session_transition_evidence
        WHERE datetime(observed_at) >= datetime(?) AND datetime(observed_at) < datetime(?)
        ORDER BY datetime(observed_at) ASC, rowid ASC
        """,
        (capture_start, end),
    ).fetchall()
    unlock_cycles = sum(1 for row in transitions if row["previous_state"] == "locked" and row["state"] == "unlocked")
    unknown_transitions = sum(1 for row in transitions if row["state"] == "unknown")

    coverage = conn.execute(
        """
        SELECT COALESCE(SUM(frames_sampled), 0) AS sampled,
               COALESCE(SUM(frames_dropped), 0) AS dropped,
               COALESCE(SUM(gap_count), 0) AS gaps,
               COUNT(DISTINCT display_id) AS display_count
        FROM capture_coverage_ledger
        WHERE datetime(started_at) >= datetime(?) AND datetime(started_at) < datetime(?)
        """,
        (capture_start, end),
    ).fetchone()
    sampled = int(coverage["sampled"] or 0)
    dropped = int(coverage["dropped"] or 0)
    gaps = int(coverage["gaps"] or 0)
    coverage_denominator = sampled + dropped + gaps
    coverage_ratio = round(sampled / coverage_denominator, 4) if coverage_denominator else 0.0

    attribution = _attribution_summary(conn, capture_start_dt, end_dt)
    privacy_cycles = conn.execute(
        "SELECT raw_residue_count,unresolved_raw_jobs FROM soak_capture_cycles "
        "WHERE datetime(observed_at)>=datetime(?) AND datetime(observed_at)<datetime(?)",
        (privacy_start, end),
    ).fetchall()
    raw_residue_current = _raw_residue_count(Path(root_dir))
    raw_residue_peak = max([raw_residue_current, *[int(row["raw_residue_count"] or 0) for row in privacy_cycles]])
    unresolved_current = _unresolved_raw_jobs(conn)
    unresolved_peak = max([unresolved_current, *[int(row["unresolved_raw_jobs"] or 0) for row in privacy_cycles]])

    resources = _resource_summary(conn, storage_start, end)
    resource_regression = _resource_regression(resources, baseline)

    evidence_rows = conn.execute(
        """
        SELECT event_type, result, minutes_saved, occurred_at
        FROM soak_evidence_events
        WHERE datetime(occurred_at) >= datetime(?) AND datetime(occurred_at) < datetime(?)
        ORDER BY datetime(occurred_at) ASC
        """,
        (min(capture_start, privacy_start, context_start), end),
    ).fetchall()
    context_evidence = [
        row for row in evidence_rows if parse_time(str(row["occurred_at"])) >= context_start_dt
    ]
    recovery_wins = sum(1 for row in context_evidence if row["event_type"] == "context_recovery" and row["result"] == "passed")
    recovery_minutes = sum(int(row["minutes_saved"] or 0) for row in context_evidence if row["event_type"] == "context_recovery" and row["result"] == "passed")
    control_drill_start = gate_windows["control_drills"]["from_dt"]
    drills = {
        kind: _latest_event_result(
            [row for row in evidence_rows if parse_time(str(row["occurred_at"])) >= control_drill_start],
            kind,
        )
        for kind in REQUIRED_DRILLS
    }

    gates = [
        _gate(
            "observation_window",
            "连续观察 7 天",
            capture_ready,
            capture_ready,
            f"capture 连续 {elapsed_hours:.1f}/168 小时，覆盖 {observed_days}/7 个自然日",
            dependencies=SOAK_GATE_COMPONENT_DEPENDENCIES["observation_window"],
            effective_window=gate_windows["observation_window"],
        ),
        _gate(
            "service_stability",
            "记录服务稳定",
            capture_ready and bool(eligible_cycles),
            capture_ready and bool(eligible_cycles) and max_error_streak < 3 and availability_ratio >= 0.99,
            f"可采集周期健康率 {availability_ratio:.1%}，最长错误连续 {max_error_streak} 次",
            dependencies=SOAK_GATE_COMPONENT_DEPENDENCIES["service_stability"],
            effective_window=gate_windows["service_stability"],
        ),
        _gate(
            "lock_unlock",
            "锁屏—解锁 20 次",
            capture_ready and (unlock_cycles >= 20 or unknown_transitions > 0),
            capture_ready and unlock_cycles >= 20 and unknown_transitions == 0,
            f"已验证 {unlock_cycles}/20 次，未知状态转移 {unknown_transitions} 次",
            dependencies=SOAK_GATE_COMPONENT_DEPENDENCIES["lock_unlock"],
            effective_window=gate_windows["lock_unlock"],
        ),
        _gate(
            "display_coverage",
            "多屏覆盖率不低于 99%",
            capture_ready and coverage_denominator > 0,
            capture_ready and coverage_denominator > 0 and coverage_ratio >= 0.99,
            f"覆盖率 {coverage_ratio:.1%}，缺口 {gaps} 次",
            dependencies=SOAK_GATE_COMPONENT_DEPENDENCIES["display_coverage"],
            effective_window=gate_windows["display_coverage"],
        ),
        _gate(
            "raw_safety",
            "原始材料无残留",
            privacy_ready and bool(privacy_cycles),
            privacy_ready and bool(privacy_cycles) and raw_residue_peak == 0 and unresolved_peak == 0,
            f"privacy 窗口残留峰值 {raw_residue_peak}，未解决任务峰值 {unresolved_peak}",
            dependencies=SOAK_GATE_COMPONENT_DEPENDENCIES["raw_safety"],
            effective_window=gate_windows["raw_safety"],
        ),
        _gate(
            "attribution_quality",
            "App 识别率 7 天中位数达到 85%",
            capture_ready and int(attribution.get("sample_days") or 0) >= window_days,
            capture_ready and float(attribution.get("daily_median_known_app_ratio") or 0.0) >= 0.85 and float(attribution.get("daily_median_low_confidence_ratio") or 0.0) <= 0.15,
            f"日中位识别率 {float(attribution.get('daily_median_known_app_ratio') or 0.0):.1%}，低置信 {float(attribution.get('daily_median_low_confidence_ratio') or 0.0):.1%}，有效 {int(attribution.get('sample_days') or 0)}/7 天",
            dependencies=SOAK_GATE_COMPONENT_DEPENDENCIES["attribution_quality"],
            effective_window=gate_windows["attribution_quality"],
        ),
        _gate(
            "resource_regression",
            "资源开销不恶化超过 20%",
            storage_ready and resource_regression["comparable"],
            storage_ready and resource_regression["passed"],
            resource_regression["summary"],
            dependencies=SOAK_GATE_COMPONENT_DEPENDENCIES["resource_regression"],
            effective_window=gate_windows["resource_regression"],
        ),
        _gate(
            "context_recovery",
            "找回上下文成功 5 次",
            context_ready and recovery_wins >= 5,
            context_ready and recovery_wins >= 5,
            f"context_fusion 窗口已记录 {recovery_wins}/5 次，累计节省 {recovery_minutes} 分钟",
            dependencies=SOAK_GATE_COMPONENT_DEPENDENCIES["context_recovery"],
            effective_window=gate_windows["context_recovery"],
        ),
        _gate(
            "control_drills",
            "完成暂停、删除和权限演练",
            capture_ready and privacy_ready and all(value != "not_recorded" for value in drills.values()),
            capture_ready and privacy_ready and all(value == "passed" for value in drills.values()),
            "；".join(f"{_drill_label(key)}={value}" for key, value in drills.items()),
            dependencies=SOAK_GATE_COMPONENT_DEPENDENCIES["control_drills"],
            effective_window=gate_windows["control_drills"],
        ),
    ]
    failed = [gate for gate in gates if gate["status"] == "failed"]
    pending = [gate for gate in gates if gate["status"] == "pending"]
    unknown = [gate for gate in gates if gate["status"] == "unknown"]
    status = "passed" if not failed and not pending and not unknown else ("failed" if failed else "in_progress")
    daily = _daily_summaries(conn, start_dt, end_dt)
    return {
        "status": status,
        "version": CORE_VERSION,
        "window": {
            "days": window_days,
            "from": start,
            "requested_from": requested_start_dt.isoformat(),
            # Backward-compatible alias, now tied only to the capture
            # component.  A resource baseline may not impersonate capture.
            "campaign_started_at": component_windows["capture"]["effective_start"],
            "to": end,
            "elapsed_hours": round((end_dt - requested_start_dt).total_seconds() / 3600.0, 2),
            "observed_days": observed_days,
            "component_windows": {
                name: {key: value for key, value in payload.items() if key != "from_dt"}
                for name, payload in component_windows.items()
            },
        },
        "summary": _report_summary(status, failed, pending, unknown),
        "gates": gates,
        "gate_counts": {
            "passed": sum(1 for gate in gates if gate["status"] == "passed"),
            "failed": len(failed),
            "pending": len(pending),
            "unknown": len(unknown),
            "total": len(gates),
        },
        "session_transitions": {
            "unlock_cycles": unlock_cycles,
            "unknown_transitions": unknown_transitions,
            "evidence_count": len(transitions),
        },
        "capture": {
            "cycle_count": len(cycles),
            "eligible_cycle_count": len(eligible_cycles),
            "healthy_cycle_count": healthy_cycles,
            "partial_cycle_count": partial_cycles,
            "error_cycle_count": error_cycles,
            "availability_ratio": availability_ratio,
            "max_error_streak": max_error_streak,
        },
        "coverage": {
            "ratio": coverage_ratio,
            "frames_sampled": sampled,
            "frames_dropped": dropped,
            "gap_count": gaps,
            "display_count": int(coverage["display_count"] or 0),
        },
        "attribution": {
            "known_app_ratio": float(attribution.get("known_app_ratio") or 0.0),
            "low_confidence_ratio": float(attribution.get("low_confidence_ratio") or 0.0),
            "frame_signal_count": int(attribution.get("frame_signal_count") or 0),
            "daily_median_known_app_ratio": float(attribution.get("daily_median_known_app_ratio") or 0.0),
            "daily_median_low_confidence_ratio": float(attribution.get("daily_median_low_confidence_ratio") or 0.0),
            "sample_days": int(attribution.get("sample_days") or 0),
            "status": str(attribution.get("status") or "unknown"),
        },
        "raw_safety": {
            "raw_residue_current": raw_residue_current,
            "raw_residue_peak": raw_residue_peak,
            "unresolved_raw_jobs_current": unresolved_current,
            "unresolved_raw_jobs_peak": unresolved_peak,
        },
        "resources": {**resources, "baseline": baseline, "regression": resource_regression},
        "user_evidence": {
            "context_recovery_wins": recovery_wins,
            "minutes_saved": recovery_minutes,
            "drills": drills,
        },
        "daily": daily,
        "raw_access": False,
        "cloud_analysis": "off",
    }


def refresh_daily_snapshot(
    conn: sqlite3.Connection,
    root_dir: str | Path,
    *,
    observed_at: str | None = None,
) -> dict[str, Any]:
    now = observed_at or utc_now()
    report = build_soak_report(conn, root_dir, days=SOAK_WINDOW_DAYS, end_at=now)
    day = str(now)[:10]
    compact = {
        "status": report["status"],
        "gate_counts": report["gate_counts"],
        "capture": report["capture"],
        "coverage": report["coverage"],
        "attribution": report["attribution"],
        "raw_safety": report["raw_safety"],
        "raw_access": False,
    }
    conn.execute(
        """
        INSERT INTO soak_daily_snapshots(day, generated_at, status, payload_json)
        VALUES(?, ?, ?, ?)
        ON CONFLICT(day) DO UPDATE SET
          generated_at = excluded.generated_at,
          status = excluded.status,
          payload_json = excluded.payload_json
        """,
        (day, now, report["status"], json.dumps(compact, ensure_ascii=False, separators=(",", ":"))),
    )
    return report


def render_soak_report_markdown(report: dict[str, Any]) -> str:
    counts = report.get("gate_counts") or {}
    window = report.get("window") or {}
    lines = [
        "# PAM 7 天稳定性记分卡",
        "",
        "> 只包含运行指标、状态证据和人工确认计数；不包含截图、OCR、窗口标题、完整 URL 或本机路径。",
        "",
        f"- 状态：`{report.get('status', 'unknown')}`",
        f"- 观察：{window.get('observed_days', 0)}/7 个自然日",
        f"- 门禁：通过 {counts.get('passed', 0)}，失败 {counts.get('failed', 0)}，"
        f"待观察 {counts.get('pending', 0)}，未建立组件基线 {counts.get('unknown', 0)}",
        "- raw_access: `false`",
        "",
        "## 验收门禁",
        "",
    ]
    for gate in report.get("gates") or []:
        lines.append(f"- **{gate.get('label', '')}** `{gate.get('status', 'pending')}`：{gate.get('summary', '')}")
    lines.extend(["", "## 结论", "", str(report.get("summary") or "暂无结论。"), ""])
    return "\n".join(lines)


def _gate(
    code: str,
    label: str,
    has_evidence: bool,
    passed: bool,
    summary: str,
    *,
    dependencies: tuple[str, ...],
    effective_window: dict[str, Any],
) -> dict[str, Any]:
    if effective_window["baseline_status"] != "baselined":
        status = "unknown"
    else:
        status = "passed" if passed else ("failed" if has_evidence else "pending")
    return {
        "code": code,
        "label": label,
        "status": status,
        "summary": summary,
        "dependencies": list(dependencies),
        "effective_window": {
            key: value
            for key, value in effective_window.items()
            if key != "from_dt"
        },
    }


def _report_summary(
    status: str,
    failed: list[dict],
    pending: list[dict],
    unknown: list[dict],
) -> str:
    if status == "passed":
        return "7 天稳定性门禁全部通过，可以进入下一阶段产品评审。"
    if failed:
        labels = "、".join(str(item.get("label") or "") for item in failed[:3])
        return f"稳定性门禁尚未通过；优先修复：{labels}。"
    if unknown:
        labels = "、".join(str(item.get("label") or "") for item in unknown[:3])
        return f"组件基线不完整，以下门禁状态未知：{labels}。"
    return f"稳定性观察进行中；还有 {len(pending)} 项等待足够证据。"


def _resource_summary(conn: sqlite3.Connection, start: str, end: str) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT cpu_percent, rss_bytes, disk_bytes, observed_at
        FROM soak_resource_samples
        WHERE datetime(observed_at) >= datetime(?) AND datetime(observed_at) < datetime(?)
        ORDER BY datetime(observed_at) ASC
        """,
        (start, end),
    ).fetchall()
    cpus = [float(row["cpu_percent"]) for row in rows if row["cpu_percent"] is not None]
    rss_values = [int(row["rss_bytes"]) for row in rows if row["rss_bytes"] is not None]
    disk_values = [int(row["disk_bytes"]) for row in rows if row["disk_bytes"] is not None]
    return {
        "sample_count": len(rows),
        "cpu_average_percent": round(sum(cpus) / len(cpus), 3) if cpus else None,
        "cpu_p95_percent": round(_percentile(cpus, 0.95), 3) if cpus else None,
        "rss_peak_bytes": max(rss_values) if rss_values else None,
        "disk_start_bytes": disk_values[0] if disk_values else None,
        "disk_end_bytes": disk_values[-1] if disk_values else None,
        "disk_growth_bytes": (disk_values[-1] - disk_values[0]) if len(disk_values) >= 2 else None,
    }


def _attribution_summary(conn: sqlite3.Connection, start_dt: datetime, end_dt: datetime) -> dict[str, Any]:
    overall = dict(attribution_health_for_range(conn, start_dt.isoformat(), end_dt.isoformat()))
    daily_known: list[float] = []
    daily_low_confidence: list[float] = []
    for _, local_start, local_end in _local_day_windows(start_dt, end_dt):
        day_start = max(start_dt, local_start).isoformat()
        day_end = min(end_dt, local_end).isoformat()
        if parse_time(day_start) >= parse_time(day_end):
            continue
        daily = attribution_health_for_range(conn, day_start, day_end)
        if int(daily.get("frame_signal_count") or 0) <= 0:
            continue
        daily_known.append(float(daily.get("known_app_ratio") or 0.0))
        daily_low_confidence.append(float(daily.get("low_confidence_ratio") or 0.0))
    overall.update(
        {
            "daily_median_known_app_ratio": round(float(median(daily_known)), 4) if daily_known else 0.0,
            "daily_median_low_confidence_ratio": round(float(median(daily_low_confidence)), 4) if daily_low_confidence else 0.0,
            "sample_days": len(daily_known),
        }
    )
    return overall


def component_baseline_windows(
    conn: sqlite3.Connection,
    requested_start: datetime,
    end_at: datetime,
    window_days: int,
) -> dict[str, dict[str, Any]]:
    """Return authoritative per-component windows without a global fallback."""
    if requested_start.tzinfo is None:
        requested_start = requested_start.replace(tzinfo=timezone.utc)
    else:
        requested_start = requested_start.astimezone(timezone.utc)
    if end_at.tzinfo is None:
        end_at = end_at.replace(tzinfo=timezone.utc)
    else:
        end_at = end_at.astimezone(timezone.utc)
    rows = conn.execute(
        "SELECT component,version_ref,generation,reset_reason_code,recorded_at "
        "FROM context_component_baselines"
    ).fetchall()
    by_component = {str(row["component"]): row for row in rows}
    output: dict[str, dict[str, Any]] = {}
    for component in SOAK_COMPONENTS:
        row = by_component.get(component)
        if not row:
            output[component] = {
                "from_dt": end_at,
                "from": None,
                "effective_start": None,
                "to": end_at.isoformat(),
                "elapsed_hours": 0.0,
                "window_ready": False,
                "baseline_status": "unbaselined",
                "readiness": "unknown",
                "baseline_recorded_at": "",
                "version_ref": "",
                "generation": 0,
                "reset_reason_code": "",
            }
            continue
        recorded_at = str(row["recorded_at"] or "")
        try:
            candidate = parse_time(recorded_at)
            if candidate.tzinfo is None:
                candidate = candidate.replace(tzinfo=timezone.utc)
            candidate = candidate.astimezone(timezone.utc)
        except (TypeError, ValueError):
            candidate = end_at + timedelta(microseconds=1)
        if not recorded_at or candidate > end_at:
            output[component] = {
                "from_dt": end_at,
                "from": None,
                "effective_start": None,
                "to": end_at.isoformat(),
                "elapsed_hours": 0.0,
                "window_ready": False,
                "baseline_status": "unbaselined",
                "readiness": "unknown",
                "baseline_recorded_at": recorded_at,
                "version_ref": str(row["version_ref"] or ""),
                "generation": int(row["generation"] or 0),
                "reset_reason_code": str(row["reset_reason_code"] or ""),
            }
            continue
        start = max(requested_start, candidate)
        elapsed_hours = max(0.0, (end_at - start).total_seconds() / 3600.0)
        output[component] = {
            "from_dt": start,
            "from": start.isoformat(),
            "effective_start": start.isoformat(),
            "to": end_at.isoformat(),
            "elapsed_hours": round(elapsed_hours, 2),
            "window_ready": elapsed_hours >= window_days * 24,
            "baseline_status": "baselined",
            "readiness": "ready" if elapsed_hours >= window_days * 24 else "pending",
            "baseline_recorded_at": recorded_at,
            "version_ref": str(row["version_ref"] or ""),
            "generation": int(row["generation"] or 0),
            "reset_reason_code": str(row["reset_reason_code"] or ""),
        }
    return output


def _component_windows(
    conn: sqlite3.Connection,
    requested_start: datetime,
    end_at: datetime,
    window_days: int,
) -> dict[str, dict[str, Any]]:
    """Backward-compatible internal alias for the authoritative helper."""
    return component_baseline_windows(conn, requested_start, end_at, window_days)


def _gate_effective_window(
    component_windows: dict[str, dict[str, Any]],
    dependencies: tuple[str, ...],
    end_at: datetime,
    window_days: int,
) -> dict[str, Any]:
    missing = [
        component
        for component in dependencies
        if component_windows[component]["baseline_status"] != "baselined"
    ]
    generations = {
        component: int(component_windows[component]["generation"] or 0)
        for component in dependencies
    }
    if missing:
        return {
            "from_dt": end_at,
            "from": None,
            "effective_start": None,
            "to": end_at.isoformat(),
            "elapsed_hours": 0.0,
            "window_ready": False,
            "baseline_status": "unbaselined",
            "readiness": "unknown",
            "missing_components": missing,
            "generations": generations,
        }
    start = max(component_windows[component]["from_dt"] for component in dependencies)
    elapsed_hours = max(0.0, (end_at - start).total_seconds() / 3600.0)
    return {
        "from_dt": start,
        "from": start.isoformat(),
        "effective_start": start.isoformat(),
        "to": end_at.isoformat(),
        "elapsed_hours": round(elapsed_hours, 2),
        "window_ready": elapsed_hours >= window_days * 24,
        "baseline_status": "baselined",
        "readiness": "ready" if elapsed_hours >= window_days * 24 else "pending",
        "missing_components": [],
        "generations": generations,
    }

def _latest_baseline(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM soak_resource_baselines ORDER BY datetime(recorded_at) DESC, rowid DESC LIMIT 1"
    ).fetchone()
    if not row:
        return {"status": "missing"}
    return {
        "status": "recorded",
        "source_version": str(row["source_version"] or "unknown"),
        "recorded_at": str(row["recorded_at"] or ""),
        "cpu_percent": _optional_float(row["cpu_percent"]),
        "rss_bytes": _optional_int(row["rss_bytes"]),
        "disk_bytes": _optional_int(row["disk_bytes"]),
    }


def _resource_regression(resources: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    if baseline.get("status") != "recorded" or int(resources.get("sample_count") or 0) < 2:
        return {"comparable": False, "passed": False, "max_regression_ratio": None, "summary": "等待 v0.3.8 基线和至少 2 个 v0.3.9 资源样本。"}
    ratios: list[float] = []
    comparisons: list[str] = []
    pairs = (
        ("CPU", resources.get("cpu_average_percent"), baseline.get("cpu_percent")),
        ("内存", resources.get("rss_peak_bytes"), baseline.get("rss_bytes")),
    )
    for label, current, base in pairs:
        if current is None or base is None or float(base) <= 0:
            continue
        ratio = max(0.0, float(current) / float(base) - 1.0)
        ratios.append(ratio)
        comparisons.append(f"{label} {ratio:+.1%}")
    disk_growth = resources.get("disk_growth_bytes")
    baseline_disk = baseline.get("disk_bytes")
    if disk_growth is not None and baseline_disk is not None and int(baseline_disk) > 0:
        ratio = max(0.0, float(disk_growth) / float(baseline_disk))
        ratios.append(ratio)
        comparisons.append(f"磁盘增长 {ratio:+.1%}")
    if not ratios:
        return {"comparable": False, "passed": False, "max_regression_ratio": None, "summary": "资源样本与基线暂不可比较。"}
    maximum = max(ratios)
    return {
        "comparable": True,
        "passed": maximum <= 0.20,
        "max_regression_ratio": round(maximum, 4),
        "summary": "，".join(comparisons) + f"；最大恶化 {maximum:.1%}",
    }


def _daily_summaries(conn: sqlite3.Connection, start_dt: datetime, end_dt: datetime) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for day, local_start, local_end in _local_day_windows(start_dt, end_dt):
        day_start = max(start_dt, local_start).isoformat()
        day_end = min(end_dt, local_end).isoformat()
        cycle = conn.execute(
            """
            SELECT COUNT(*) AS count,
                   SUM(CASE WHEN error_count > 0 OR status LIKE 'error%' THEN 1 ELSE 0 END) AS errors,
                   MAX(raw_residue_count) AS raw_peak,
                   MAX(unresolved_raw_jobs) AS unresolved_peak
            FROM soak_capture_cycles
            WHERE datetime(observed_at) >= datetime(?) AND datetime(observed_at) < datetime(?)
            """,
            (day_start, day_end),
        ).fetchone()
        rows.append(
            {
                "day": day,
                "capture_cycles": int(cycle["count"] or 0),
                "error_cycles": int(cycle["errors"] or 0),
                "raw_residue_peak": int(cycle["raw_peak"] or 0),
                "unresolved_raw_jobs_peak": int(cycle["unresolved_peak"] or 0),
            }
        )
    return rows[-SOAK_WINDOW_DAYS:]


def _local_day_windows(start_dt: datetime, end_dt: datetime) -> list[tuple[str, datetime, datetime]]:
    local_tz = datetime.now().astimezone().tzinfo or timezone.utc
    cursor = start_dt.astimezone(local_tz).date()
    last = end_dt.astimezone(local_tz).date()
    windows: list[tuple[str, datetime, datetime]] = []
    while cursor <= last:
        local_start = datetime.combine(cursor, time.min, tzinfo=local_tz).astimezone(timezone.utc)
        local_end = (datetime.combine(cursor, time.min, tzinfo=local_tz) + timedelta(days=1)).astimezone(timezone.utc)
        windows.append((cursor.isoformat(), local_start, local_end))
        cursor += timedelta(days=1)
    return windows


def _local_day(value: str) -> str:
    try:
        return parse_time(value).astimezone().date().isoformat()
    except (TypeError, ValueError):
        return str(value)[:10]


def _process_resource_usage(pid: int) -> tuple[float | None, int | None]:
    try:
        completed = subprocess.run(
            ["ps", "-o", "%cpu=,rss=", "-p", str(pid)],
            check=False,
            timeout=2,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    parts = completed.stdout.strip().split()
    if len(parts) < 2:
        return None, None
    try:
        return max(0.0, float(parts[0])), max(0, int(float(parts[1])) * 1024)
    except ValueError:
        return None, None


def _bounded_directory_size(root: Path, *, max_files: int = 20_000) -> int | None:
    if not root.exists():
        return 0
    total = 0
    seen = 0
    try:
        for directory, _, filenames in os.walk(root):
            for filename in filenames:
                seen += 1
                if seen > max_files:
                    return total
                path = Path(directory) / filename
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
    except OSError:
        return None
    return total


def _raw_residue_count(root: Path) -> int:
    vault = root / "raw_vault"
    if not vault.is_dir():
        return 0
    try:
        return sum(1 for child in vault.iterdir() if child.name not in {".metadata_never_index", "vlm_pending"})
    except OSError:
        return 0


def _unresolved_raw_jobs(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM vlm_pending_jobs
        WHERE status IN ('pending', 'processing')
        """
    ).fetchone()
    return int(row["count"] or 0)


def _prune_soak_metrics(conn: sqlite3.Connection, now: str) -> None:
    try:
        cutoff = (parse_time(now) - timedelta(days=SOAK_RETENTION_DAYS)).isoformat()
    except (TypeError, ValueError):
        return
    conn.execute("DELETE FROM soak_capture_cycles WHERE datetime(observed_at) < datetime(?)", (cutoff,))
    conn.execute("DELETE FROM soak_resource_samples WHERE datetime(observed_at) < datetime(?)", (cutoff,))
    conn.execute("DELETE FROM soak_daily_snapshots WHERE date(day) < date(?)", (cutoff,))


def _daily_snapshot_due(conn: sqlite3.Connection, now: str) -> bool:
    day = str(now)[:10]
    row = conn.execute("SELECT generated_at FROM soak_daily_snapshots WHERE day = ?", (day,)).fetchone()
    if not row:
        return True
    try:
        return (parse_time(now) - parse_time(str(row["generated_at"]))).total_seconds() >= SOAK_SNAPSHOT_INTERVAL_SECONDS
    except (TypeError, ValueError):
        return True


def _public_transition(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return _empty_transition()
    return {
        "observed_at": str(row["observed_at"] or ""),
        "previous_state": str(row["previous_state"] or "none"),
        "state": _session_state(row["state"]),
        "on_console": _optional_bool(row["on_console"]),
        "source": str(row["source"] or "unknown"),
        "reason": str(row["reason"] or "unknown"),
        "capture_allowed": bool(row["capture_allowed"]),
        "transition_kind": str(row["transition_kind"] or "unknown"),
        "daemon_start": bool(row["daemon_start"]),
        "last_valid_state": str(row["last_valid_state"] or ""),
        "last_valid_at": str(row["last_valid_at"] or ""),
    }


def _empty_transition() -> dict[str, Any]:
    return {
        "observed_at": "",
        "previous_state": "none",
        "state": "unknown",
        "on_console": None,
        "source": "no_evidence",
        "reason": "no_evidence",
        "capture_allowed": False,
        "transition_kind": "none",
        "daemon_start": False,
        "last_valid_state": "",
        "last_valid_at": "",
    }


def _session_state(value: Any) -> str:
    state = str(value or "").strip().lower()
    return state if state in {"locked", "unlocked", "unknown"} else "unknown"


def _safe_token(value: Any) -> str:
    return "".join(ch if ch.isalnum() or ch in {"_", "-", "."} else "_" for ch in str(value or "").lower())[:80].strip("_")


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _daemon_state(conn: sqlite3.Connection, key: str) -> str:
    row = conn.execute("SELECT value FROM daemon_state WHERE key = ?", (key,)).fetchone()
    return str(row["value"] or "") if row else ""


def _max_error_streak(statuses: list[str]) -> int:
    longest = 0
    current = 0
    for status in statuses:
        if status.startswith("error"):
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return float(ordered[index])


def _latest_event_result(rows: list[sqlite3.Row], event_type: str) -> str:
    for row in reversed(rows):
        if row["event_type"] == event_type:
            return str(row["result"] or "not_recorded")
    return "not_recorded"


def _drill_label(kind: str) -> str:
    return {
        "pause": "暂停",
        "delete": "删除",
        "permission_revoke": "撤销权限",
        "permission_restore": "恢复权限",
    }.get(kind, kind)
