from __future__ import annotations

import json
import sqlite3
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from .attribution_context import attribution_reason_codes, reason_count_rows, safe_reason_codes
from .pipeline import iso, parse_time, utc_now
from .screen_regions import (
    ACTIONABILITY_ACTIONABLE,
    ACTIONABILITY_CONTEXT_ONLY,
    ACTIONABILITY_NOISE,
    REGION_BROWSER_TAB,
    REGION_DESKTOP_ICON,
    REGION_EDITOR_BODY,
    REGION_FILE_LIST,
    REGION_MEDIA_CHROME,
    REGION_MENU_BAR,
    REGION_TASK_LIST,
    sanitize_screen_region_signal,
)
from .storage import init_schema


DEFAULT_MAX_SLICE_SECONDS = 45
UNKNOWN_LABEL = "未知"
GAP_LABEL = "未记录缺口"
PRIVACY_LABEL = "隐私保护"
SCREEN_TIME_BOUNDARY = {
    "source": "pam_local_timeline",
    "system_screen_time_used": False,
    "reason": "macOS Screen Time / DeviceActivity detail APIs are not a stable background data source for this runtime.",
}
BROWSER_APPS = {"dia", "chrome", "google chrome", "safari", "arc", "firefox", "edge", "microsoft edge"}
COMMUNICATION_APPS = {"wechat", "微信", "discord", "slack", "telegram", "messages", "imessage", "zoom", "lark", "飞书"}
COMMUNICATION_DOMAINS = {"mail.yahoo.com", "wx.mail.qq.com", "mail.google.com", "gmail.com", "discord.com", "slack.com"}
MEDIA_APPS = {"iina", "vlc", "quicktime player", "quicktime", "mpv", "infuse", "elmedia"}
MEDIA_DOMAINS = {"youtube.com", "bilibili.com", "netflix.com", "disneyplus.com", "iqiyi.com", "youku.com", "v.qq.com"}
TASK_APPS = {"ticktick", "滴答清单", "things", "todoist", "reminders", "提醒事项", "omnifocus"}
SYSTEM_APPS = {"finder", "system settings", "系统设置", "launchpad", "dock", "menubar", "menu bar"}
WORK_APPS = {
    "codex",
    "claude",
    "chatgpt",
    "hermes",
    "obsidian",
    "typora",
    "textedit",
    "pages",
    "microsoft word",
    "word",
    "wps",
    "terminal",
    "warp",
    "cursor",
    "visual studio code",
    "vscode",
}
WORK_DOMAINS = {"chatgpt.com", "claude.ai", "github.com", "developer.apple.com", "openai.com", "docs.anthropic.com"}
PRIMARY_DEMOTE_REGIONS = {
    REGION_BROWSER_TAB,
    REGION_DESKTOP_ICON,
    REGION_FILE_LIST,
    REGION_MEDIA_CHROME,
    REGION_MENU_BAR,
    REGION_TASK_LIST,
}
ACTIONABLE_REGION_BONUS = {
    REGION_EDITOR_BODY: 1.1,
}


def observe_system_activity_session(
    conn: sqlite3.Connection,
    *,
    locked: bool,
    observed_at: str | None = None,
    daemon_start: bool = False,
) -> dict[str, Any]:
    init_schema(conn)
    now = observed_at or utc_now()
    closed_id = ""
    if daemon_start:
        stale = _open_session(conn)
        if stale:
            closed_id = _close_session(conn, stale, now, "daemon_gap_closed")
    current = _open_session(conn)
    if locked:
        if current:
            closed_id = _close_session(conn, current, now, "lock_detected")
        return {"locked": True, "current_session": None, "closed_session_id": closed_id}
    if current:
        conn.execute(
            """
            UPDATE system_activity_sessions
            SET last_observed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, now, current["id"]),
        )
        refreshed = refresh_activity_session(conn, current["id"], active_end=now)
        return {"locked": False, "current_session": refreshed, "closed_session_id": closed_id}
    session_id = f"system_session_{uuid.uuid4().hex}"
    reason = "daemon_started_unlocked" if daemon_start else "unlock_detected"
    conn.execute(
        """
        INSERT INTO system_activity_sessions(
          id, started_at, ended_at, status, start_reason, end_reason,
          duration_seconds, last_observed_at, coverage_ratio, sample_count,
          privacy_event_count, gap_event_count, created_at, updated_at
        ) VALUES(?, ?, NULL, 'open', ?, '', 0, ?, 0, 0, 0, 0, ?, ?)
        """,
        (session_id, now, reason, now, now, now),
    )
    refreshed = refresh_activity_session(conn, session_id, active_end=now)
    return {"locked": False, "current_session": refreshed, "closed_session_id": closed_id}


def close_open_activity_session(conn: sqlite3.Connection, *, ended_at: str | None = None, reason: str = "manual_closed") -> dict[str, Any]:
    init_schema(conn)
    current = _open_session(conn)
    if not current:
        return {"closed": False, "session_id": ""}
    now = ended_at or utc_now()
    session_id = _close_session(conn, current, now, reason)
    return {"closed": True, "session_id": session_id}


def refresh_activity_session(conn: sqlite3.Connection, session_id: str, *, active_end: str | None = None) -> dict[str, Any]:
    session = _session_row(conn, session_id)
    if not session:
        raise ValueError(f"activity session not found: {session_id}")
    started_at = str(session["started_at"])
    ended_at = str(session["ended_at"] or active_end or utc_now())
    start_dt = parse_time(started_at)
    end_dt = parse_time(ended_at)
    if end_dt < start_dt:
        end_dt = start_dt
        ended_at = iso(end_dt)
    max_slice = _max_slice_seconds(conn)
    samples, coverage_seconds, privacy_count, gap_count = _build_samples(conn, session_id, start_dt, end_dt, max_slice)
    duration = max(0, int((end_dt - start_dt).total_seconds()))
    _replace_samples(conn, session_id, samples)
    rollups = _rollups(samples, duration_seconds=max(duration, 1))
    _replace_rollups(conn, session_id, rollups)
    coverage_ratio = min(1.0, float(coverage_seconds) / float(duration)) if duration > 0 else 0.0
    now = utc_now()
    conn.execute(
        """
        UPDATE system_activity_sessions
        SET duration_seconds = ?, coverage_ratio = ?, sample_count = ?,
            privacy_event_count = ?, gap_event_count = ?, updated_at = ?
        WHERE id = ?
        """,
        (duration, coverage_ratio, len(samples), privacy_count, gap_count, now, session_id),
    )
    # This helper is used inside the lifecycle deletion transaction.  Calling
    # the public getter here used to run init_schema(), whose executescript and
    # commit split one retention delete into hundreds of partial commits.
    return _get_activity_session_ready(conn, session_id, active_end=active_end)


def get_current_activity_session(conn: sqlite3.Connection) -> dict[str, Any]:
    init_schema(conn)
    current = _open_session(conn)
    if not current:
        return {
            "status": "none",
            "raw_access": False,
            "screen_time_boundary": SCREEN_TIME_BOUNDARY,
            "session": None,
        }
    active_end = utc_now()
    refreshed = refresh_activity_session(conn, str(current["id"]), active_end=active_end)
    # The public current-session read historically persisted its refreshed
    # rollups through the nested schema initializer. Keep that contract
    # explicit while the lower-level refresh helper remains transaction-safe
    # for lifecycle deletion.
    conn.commit()
    return {
        "status": "ok",
        "raw_access": False,
        "screen_time_boundary": SCREEN_TIME_BOUNDARY,
        "session": refreshed,
    }


def get_activity_session(conn: sqlite3.Connection, session_id: str, *, active_end: str | None = None) -> dict[str, Any] | None:
    init_schema(conn)
    return _get_activity_session_ready(conn, session_id, active_end=active_end)


def _get_activity_session_ready(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    active_end: str | None = None,
) -> dict[str, Any] | None:
    """Read one session after the caller has established schema readiness."""
    row = _session_row(conn, session_id)
    if not row:
        return None
    end_value = str(row["ended_at"] or active_end or utc_now())
    start_dt = parse_time(str(row["started_at"]))
    end_dt = parse_time(end_value)
    duration = max(0, int((end_dt - start_dt).total_seconds()))
    rollups = _session_rollups(conn, session_id)
    from .presentation_scope import ReadView
    displayed_samples = ([dict(r) for r in conn.execute('SELECT * FROM system_activity_samples WHERE session_id=?', (session_id,))]
                         if isinstance(conn, ReadView) else None)
    attribution_quality = _session_attribution_quality(conn, session_id, duration_seconds=max(duration, 1), rollups=rollups)
    return {
        "id": row["id"],
        "started_at": row["started_at"],
        "ended_at": row["ended_at"] or "",
        "effective_ended_at": end_value,
        "status": row["status"],
        "start_reason": row["start_reason"],
        "end_reason": row["end_reason"] or "",
        "duration_seconds": duration,
        "duration_label": _duration_label(duration),
        "coverage_ratio": float(row["coverage_ratio"] or 0.0),
        "sample_count": len(displayed_samples) if displayed_samples is not None else int(row["sample_count"] or 0),
        "privacy_event_count": int(row["privacy_event_count"] or 0),
        "gap_event_count": int(row["gap_event_count"] or 0),
        "primary_focus_share": rollups.get("primary_category", []),
        "visible_screen_share": rollups.get("visible_category", []),
        "top_apps": rollups.get("primary_app", []),
        "visible_apps": rollups.get("visible_app", []),
        "top_domains": rollups.get("primary_domain", []),
        "visible_domains": rollups.get("visible_domain", []),
        "summary": _session_summary(rollups, duration),
        "attribution_quality": attribution_quality,
        "unknown_share": attribution_quality["unknown_share"],
        "low_confidence_share": attribution_quality["low_confidence_share"],
        "explanation_lines": attribution_quality["explanation_lines"],
        "raw_access": False,
    }


def list_activity_sessions(
    conn: sqlite3.Connection,
    *,
    range_name: str = "today",
    start: str | None = None,
    end: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    init_schema(conn)
    resolved_start, resolved_end, label = resolve_session_range(range_name, start, end)
    rows = conn.execute(
        """
        SELECT *
        FROM system_activity_sessions
        WHERE datetime(started_at) < datetime(?)
          AND datetime(COALESCE(ended_at, last_observed_at, started_at)) >= datetime(?)
        ORDER BY started_at DESC
        LIMIT ?
        """,
        (resolved_end, resolved_start, max(1, int(limit))),
    ).fetchall()
    sessions = [_get_activity_session_ready(conn, str(row["id"])) for row in rows]
    sessions = [session for session in sessions if session]
    return {
        "status": "ok",
        "raw_access": False,
        "screen_time_boundary": SCREEN_TIME_BOUNDARY,
        "range": {"label": label, "from": resolved_start, "to": resolved_end},
        "session_count": len(sessions),
        "sessions": sessions,
        "summary": summarize_activity_sessions(sessions),
    }


def display_activity_sessions(conn, payload, scope):
    """Rebuild owner-visible rollups without changing stored samples/history."""
    from .presentation_scope import read_view, ACTIVITY_TABLES
    if not scope.enabled:
        return scope.project(payload)
    view = read_view(conn, scope, (*ACTIVITY_TABLES, 'system_activity_samples'))
    result = dict(payload)
    if isinstance(result.get('session'), dict):
        session = result['session']
        result['session'] = _get_activity_session_ready(view, session['id'], active_end=session.get('effective_ended_at'))
    if isinstance(result.get('sessions'), list):
        result['sessions'] = [_get_activity_session_ready(view, s['id'], active_end=s.get('effective_ended_at')) for s in result['sessions']]
        result['sessions'] = [s for s in result['sessions'] if s]
        result['session_count'] = len(result['sessions'])
        result['summary'] = summarize_activity_sessions(result['sessions'])
    return scope.project(result)


def show_activity_session(conn: sqlite3.Connection, session_id: str) -> dict[str, Any]:
    session = get_activity_session(conn, session_id)
    return {
        "status": "ok" if session else "not_found",
        "raw_access": False,
        "screen_time_boundary": SCREEN_TIME_BOUNDARY,
        "session": session,
    }


def summarize_activity_sessions(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    total_duration = sum(int(item.get("duration_seconds") or 0) for item in sessions)
    category_seconds: Counter[str] = Counter()
    app_seconds: Counter[str] = Counter()
    media_seconds = 0
    work_seconds = 0
    for session in sessions:
        for item in session.get("primary_focus_share") or []:
            seconds = int(item.get("duration_seconds") or 0)
            label = str(item.get("label") or UNKNOWN_LABEL)
            category_seconds[label] += seconds
            if item.get("category") == "media_entertainment":
                media_seconds += seconds
            if item.get("category") == "work_ai":
                work_seconds += seconds
        for item in session.get("top_apps") or []:
            app_seconds[str(item.get("label") or UNKNOWN_LABEL)] += int(item.get("duration_seconds") or 0)
    return {
        "total_duration_seconds": total_duration,
        "total_duration_label": _duration_label(total_duration),
        "session_count": len(sessions),
        "work_seconds": work_seconds,
        "work_share": _share(work_seconds, total_duration),
        "media_seconds": media_seconds,
        "media_share": _share(media_seconds, total_duration),
        "top_categories": _counter_rows(category_seconds, total_duration, limit=6),
        "top_apps": _counter_rows(app_seconds, total_duration, limit=6),
    }


def activity_sessions_status_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    current = get_current_activity_session(conn)
    today = list_activity_sessions(conn, range_name="today", limit=12)
    return {
        "current_activity_session": current.get("session"),
        "today_activity_sessions": {
            "session_count": today["session_count"],
            "summary": today["summary"],
            "screen_time_boundary": SCREEN_TIME_BOUNDARY,
        },
    }


def render_activity_sessions_markdown(payload: dict[str, Any]) -> str:
    lines = ["# 解锁会话用时", ""]
    boundary = payload.get("screen_time_boundary") or SCREEN_TIME_BOUNDARY
    lines.append(f"- 数据源：PAM 本地安全时间线；系统屏幕使用时间明细：{'已使用' if boundary.get('system_screen_time_used') else '未使用'}。")
    if payload.get("range"):
        range_info = payload["range"]
        lines.append(f"- 范围：{range_info.get('label') or ''} `{range_info.get('from')}` → `{range_info.get('to')}`。")
    lines.append("")
    sessions = payload.get("sessions") or ([payload.get("session")] if payload.get("session") else [])
    summary = payload.get("summary") or summarize_activity_sessions([s for s in sessions if s])
    if summary:
        lines.extend(
            [
                "## 总览",
                "",
                f"- 会话数：{summary.get('session_count', len(sessions))}",
                f"- 总时长：{summary.get('total_duration_label') or _duration_label(int(summary.get('total_duration_seconds') or 0))}",
                f"- 工作 / AI 协作：{_pct(summary.get('work_share'))}",
                f"- 媒体娱乐：{_pct(summary.get('media_share'))}",
                "",
            ]
        )
    if not sessions:
        lines.extend(["暂无解锁会话。", ""])
        return "\n".join(lines)
    for session in sessions:
        if not session:
            continue
        lines.extend(
            [
                f"## {session.get('duration_label') or ''} · {session.get('started_at')}",
                "",
                f"- 状态：{session.get('status')}; 结束：{session.get('ended_at') or '进行中'}",
                f"- 摘要：{session.get('summary') or '暂无'}",
                f"- 覆盖率：{_pct(session.get('coverage_ratio'))}; 隐私保护 {session.get('privacy_event_count', 0)}; 缺口 {session.get('gap_event_count', 0)}",
                _quality_markdown_line(session.get("attribution_quality") or {}),
                "",
            ]
        )
        for line in (session.get("explanation_lines") or [])[:4]:
            lines.append(f"- 解释：{_safe_short(str(line), max_len=160)}")
        if session.get("explanation_lines"):
            lines.append("")
        _append_rollup(lines, "主要活动", session.get("primary_focus_share") or [])
        _append_rollup(lines, "Top Apps", session.get("top_apps") or [])
        _append_rollup(lines, "Top Domains", session.get("top_domains") or [])
    return "\n".join(lines)


def resolve_session_range(range_name: str = "today", start: str | None = None, end: str | None = None) -> tuple[str, str, str]:
    if start and end:
        return start, end, "custom"
    now = datetime.now().astimezone()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if range_name == "yesterday":
        begin = today_start - timedelta(days=1)
        finish = today_start
        return begin.isoformat(timespec="seconds"), finish.isoformat(timespec="seconds"), "yesterday"
    if range_name == "7d":
        begin = now - timedelta(days=7)
        return begin.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"), "7d"
    return today_start.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"), "today"


def _build_samples(
    conn: sqlite3.Connection,
    session_id: str,
    start_dt: datetime,
    end_dt: datetime,
    max_slice_seconds: int,
) -> tuple[list[dict[str, Any]], int, int, int]:
    frame_rows = _frame_rows(conn, iso(start_dt), iso(end_dt))
    visible_samples = _visible_samples(session_id, frame_rows, end_dt, max_slice_seconds)
    frame_primary_samples = _primary_samples(session_id, frame_rows, end_dt, max_slice_seconds)
    privacy_samples = _event_samples(conn, session_id, start_dt, end_dt, max_slice_seconds, kind="privacy")
    gap_samples = _event_samples(conn, session_id, start_dt, end_dt, max_slice_seconds, kind="gap")
    primary_samples, synthetic_gap_count = _compose_primary_timeline(
        session_id,
        start_dt,
        end_dt,
        frame_primary_samples,
        privacy_samples,
        gap_samples,
    )
    coverage_seconds = sum(
        int(item["duration_seconds"])
        for item in primary_samples
        if item.get("source_type") not in {"gap_event", "coverage_gap"}
    )
    return (
        [*visible_samples, *primary_samples],
        coverage_seconds,
        len(privacy_samples),
        len(gap_samples) + synthetic_gap_count,
    )


def _compose_primary_timeline(
    session_id: str,
    start_dt: datetime,
    end_dt: datetime,
    frame_samples: list[dict[str, Any]],
    privacy_samples: list[dict[str, Any]],
    gap_samples: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Build one mutually-exclusive primary sample for every wall-clock slice.

    Privacy events override frame attribution so protected content cannot leak back
    into Top Apps. A valid frame beats an overlapping gap event because a gap on a
    secondary display must not erase a trustworthy primary signal. Remaining time
    becomes a content-free synthetic gap.
    """
    if end_dt <= start_dt:
        return [], 0
    candidates: list[dict[str, Any]] = []
    boundaries = {start_dt, end_dt}
    for priority, source_samples in ((30, privacy_samples), (20, frame_samples), (10, gap_samples)):
        for source in source_samples:
            source_start = max(start_dt, parse_time(str(source["started_at"])))
            source_end = min(end_dt, parse_time(str(source["ended_at"])))
            if source_end <= source_start:
                continue
            candidate = dict(source)
            candidate["_started_dt"] = source_start
            candidate["_ended_dt"] = source_end
            candidate["_priority"] = priority
            candidates.append(candidate)
            boundaries.update((source_start, source_end))

    ordered = sorted(boundaries)
    timeline: list[dict[str, Any]] = []
    synthetic_gap_count = 0
    for index in range(len(ordered) - 1):
        slice_start = ordered[index]
        slice_end = ordered[index + 1]
        duration = int((slice_end - slice_start).total_seconds())
        if duration <= 0:
            continue
        covering = [
            item
            for item in candidates
            if item["_started_dt"] <= slice_start and item["_ended_dt"] >= slice_end
        ]
        if covering:
            selected = max(
                covering,
                key=lambda item: (
                    int(item["_priority"]),
                    float(item.get("confidence") or 0.0),
                    str(item.get("source_id") or ""),
                ),
            )
            sample = {
                key: value
                for key, value in selected.items()
                if not key.startswith("_")
            }
        else:
            sample = _synthetic_gap_sample(session_id)
            synthetic_gap_count += 1
        sample.update(
            {
                "started_at": iso(slice_start),
                "ended_at": iso(slice_end),
                "duration_seconds": duration,
            }
        )
        timeline.append(sample)
    merged = _merge_adjacent_primary_samples(timeline)
    synthetic_gap_count = sum(1 for sample in merged if sample.get("synthetic_gap"))
    return merged, synthetic_gap_count


def _synthetic_gap_sample(session_id: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "display_id": "",
        "app_name": "",
        "domain": "",
        "page_title": "",
        "category": "gap",
        "category_label": GAP_LABEL,
        "attribution_kind": "primary_focus",
        "source_type": "coverage_gap",
        "source_id": "",
        "confidence": 0.2,
        "domain_trusted": False,
        "privacy_flags": ["coverage_gap"],
        "synthetic_gap": True,
    }


def _merge_adjacent_primary_samples(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    identity_keys = (
        "display_id",
        "app_name",
        "domain",
        "page_title",
        "category",
        "category_label",
        "attribution_kind",
        "source_type",
        "source_id",
        "confidence",
        "domain_trusted",
        "privacy_flags",
        "synthetic_gap",
    )
    for sample in samples:
        if (
            merged
            and merged[-1]["ended_at"] == sample["started_at"]
            and all(merged[-1].get(key) == sample.get(key) for key in identity_keys)
        ):
            merged[-1]["ended_at"] = sample["ended_at"]
            merged[-1]["duration_seconds"] += int(sample["duration_seconds"])
            continue
        merged.append(dict(sample))
    return merged


def _frame_rows(conn: sqlite3.Connection, start: str, end: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT fs.id, fs.captured_at, fs.display_id, fs.app_name, fs.bundle_id,
               fs.window_title_redacted, fs.domain, fs.ocr_redacted_summary,
               fs.safe_keywords, fs.privacy_flags, fs.screen_region_signal,
               dfc.context_confidence, dfc.source AS context_source,
               dfc.app_contexts AS context_app_contexts,
               dfc.url_contexts AS context_url_contexts
        FROM frame_signals fs
        LEFT JOIN display_frame_contexts dfc
          ON dfc.session_id = fs.session_id
         AND dfc.segment_id = fs.segment_id
         AND dfc.display_id = fs.display_id
         AND dfc.captured_at = fs.captured_at
        WHERE datetime(fs.captured_at) >= datetime(?)
          AND datetime(fs.captured_at) < datetime(?)
        ORDER BY fs.captured_at ASC, fs.display_id ASC
        """,
        (start, end),
    ).fetchall()
    return [dict(row) for row in rows]


def _visible_samples(session_id: str, rows: list[dict[str, Any]], end_dt: datetime, max_slice_seconds: int) -> list[dict[str, Any]]:
    by_display: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_display[str(row.get("display_id") or "")].append(row)
    samples: list[dict[str, Any]] = []
    for display_rows in by_display.values():
        for index, row in enumerate(display_rows):
            started = parse_time(str(row["captured_at"]))
            next_at = parse_time(str(display_rows[index + 1]["captured_at"])) if index + 1 < len(display_rows) else end_dt
            ended = min(next_at, started + timedelta(seconds=max_slice_seconds), end_dt)
            duration = max(0, int((ended - started).total_seconds()))
            if duration <= 0:
                continue
            samples.append(_sample_from_frame(session_id, row, started, ended, duration, "visible_screen"))
    return samples


def _primary_samples(session_id: str, rows: list[dict[str, Any]], end_dt: datetime, max_slice_seconds: int) -> list[dict[str, Any]]:
    by_time: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_time[str(row.get("captured_at") or "")].append(row)
    ordered_times = sorted(by_time, key=parse_time)
    samples: list[dict[str, Any]] = []
    for index, captured_at in enumerate(ordered_times):
        started = parse_time(captured_at)
        next_at = parse_time(ordered_times[index + 1]) if index + 1 < len(ordered_times) else end_dt
        ended = min(next_at, started + timedelta(seconds=max_slice_seconds), end_dt)
        duration = max(0, int((ended - started).total_seconds()))
        if duration <= 0:
            continue
        best = max(by_time[captured_at], key=_primary_score)
        samples.append(_sample_from_frame(session_id, best, started, ended, duration, "primary_focus"))
    return samples


def _sample_from_frame(
    session_id: str,
    row: dict[str, Any],
    started: datetime,
    ended: datetime,
    duration: int,
    attribution_kind: str,
) -> dict[str, Any]:
    app = _safe_short(str(row.get("app_name") or ""))
    domain = _safe_domain(str(row.get("domain") or ""))
    trusted_domain = _is_browser_app(app) and bool(domain)
    if not trusted_domain:
        domain = ""
    category, label = _category_for(app, domain, _screen_region(row), str(row.get("ocr_redacted_summary") or ""))
    confidence = _confidence(row, trusted_domain=trusted_domain)
    privacy_flags = safe_reason_codes([*_json_list(row.get("privacy_flags")), *_attribution_flags(row, trusted_domain=trusted_domain)])
    return {
        "session_id": session_id,
        "started_at": iso(started),
        "ended_at": iso(ended),
        "duration_seconds": duration,
        "display_id": _safe_short(str(row.get("display_id") or "")),
        "app_name": app,
        "domain": domain,
        "page_title": _safe_short(str(row.get("window_title_redacted") or ""), max_len=140),
        "category": category,
        "category_label": label,
        "attribution_kind": attribution_kind,
        "source_type": "frame_signal",
        "source_id": str(row.get("id") or ""),
        "confidence": confidence,
        "domain_trusted": trusted_domain,
        "privacy_flags": privacy_flags,
    }


def _event_samples(
    conn: sqlite3.Connection,
    session_id: str,
    start_dt: datetime,
    end_dt: datetime,
    max_slice_seconds: int,
    *,
    kind: str,
) -> list[dict[str, Any]]:
    if kind == "privacy":
        table = "privacy_events"
        label = PRIVACY_LABEL
        category = "privacy_protected"
        source_type = "privacy_event"
        fields = "id, started_at, ended_at, app_name, domain, reason"
    else:
        table = "gap_events"
        label = GAP_LABEL
        category = "gap"
        source_type = "gap_event"
        fields = "id, started_at, ended_at, '' AS app_name, '' AS domain, reason"
    rows = conn.execute(
        f"""
        SELECT {fields}
        FROM {table}
        WHERE datetime(started_at) < datetime(?)
          AND (
            ended_at IS NULL
            OR datetime(ended_at) <= datetime(started_at)
            OR datetime(ended_at) > datetime(?)
          )
        ORDER BY started_at ASC
        """,
        (iso(end_dt), iso(start_dt)),
    ).fetchall()
    samples = []
    for row in rows:
        raw_start = parse_time(str(row["started_at"]))
        started = max(start_dt, raw_start)
        raw_end = parse_time(str(row["ended_at"] or row["started_at"]))
        natural_end = raw_end if raw_end > raw_start else raw_start + timedelta(seconds=max_slice_seconds)
        ended = min(natural_end, raw_start + timedelta(seconds=max_slice_seconds), end_dt)
        duration = int((ended - started).total_seconds())
        if duration <= 0:
            continue
        samples.append(
            {
                "session_id": session_id,
                "started_at": iso(started),
                "ended_at": iso(ended),
                "duration_seconds": duration,
                "display_id": "",
                "app_name": _safe_short(str(row["app_name"] or "")),
                "domain": _safe_domain(str(row["domain"] or "")),
                "page_title": "",
                "category": category,
                "category_label": label,
                "attribution_kind": "primary_focus",
                "source_type": source_type,
                "source_id": str(row["id"] or ""),
                "confidence": 0.9,
                "domain_trusted": False,
                "privacy_flags": safe_reason_codes(
                    [str(row["reason"] or kind), "privacy_protected" if kind == "privacy" else "coverage_gap"]
                ),
            }
        )
    return samples


def _replace_samples(conn: sqlite3.Connection, session_id: str, samples: list[dict[str, Any]]) -> None:
    conn.execute("DELETE FROM system_activity_samples WHERE session_id = ?", (session_id,))
    now = utc_now()
    for sample in samples:
        conn.execute(
            """
            INSERT INTO system_activity_samples(
              id, session_id, started_at, ended_at, duration_seconds, display_id,
              app_name, domain, page_title, category, category_label,
              attribution_kind, source_type, source_id, confidence, domain_trusted,
              privacy_flags, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"activity_sample_{uuid.uuid4().hex}",
                session_id,
                sample["started_at"],
                sample["ended_at"],
                int(sample["duration_seconds"]),
                sample.get("display_id") or "",
                sample.get("app_name") or "",
                sample.get("domain") or "",
                sample.get("page_title") or "",
                sample.get("category") or "unknown",
                sample.get("category_label") or UNKNOWN_LABEL,
                sample.get("attribution_kind") or "",
                sample.get("source_type") or "",
                sample.get("source_id") or "",
                float(sample.get("confidence") or 0.0),
                1 if sample.get("domain_trusted") else 0,
                json.dumps(sample.get("privacy_flags") or [], ensure_ascii=False),
                now,
            ),
        )


def _rollups(samples: list[dict[str, Any]], *, duration_seconds: int) -> list[dict[str, Any]]:
    counters: dict[str, Counter[str]] = {
        "primary_category": Counter(),
        "primary_app": Counter(),
        "primary_domain": Counter(),
        "visible_category": Counter(),
        "visible_app": Counter(),
        "visible_domain": Counter(),
    }
    confidence: dict[tuple[str, str], list[float]] = defaultdict(list)
    for sample in samples:
        seconds = int(sample.get("duration_seconds") or 0)
        if seconds <= 0:
            continue
        prefix = "primary" if sample.get("attribution_kind") == "primary_focus" else "visible"
        category_label = str(sample.get("category_label") or UNKNOWN_LABEL)
        counters[f"{prefix}_category"][category_label] += seconds
        confidence[(f"{prefix}_category", category_label)].append(float(sample.get("confidence") or 0.0))
        app = str(sample.get("app_name") or "").strip() or UNKNOWN_LABEL
        counters[f"{prefix}_app"][app] += seconds
        confidence[(f"{prefix}_app", app)].append(float(sample.get("confidence") or 0.0))
        domain = str(sample.get("domain") or "").strip()
        if domain and sample.get("domain_trusted"):
            counters[f"{prefix}_domain"][domain] += seconds
            confidence[(f"{prefix}_domain", domain)].append(float(sample.get("confidence") or 0.0))
    rows: list[dict[str, Any]] = []
    for kind, counter in counters.items():
        denominator = duration_seconds if kind.startswith("primary_") else max(1, sum(counter.values()))
        for rank, (label, seconds) in enumerate(counter.most_common(12), 1):
            rows.append(
                {
                    "rollup_kind": kind,
                    "label": label,
                    "category": _category_key_from_label(label),
                    "duration_seconds": int(seconds),
                    "share": _share(seconds, denominator),
                    "sample_count": 0,
                    "confidence": _average(confidence.get((kind, label), [])),
                    "rank": rank,
                }
            )
    return rows


def _replace_rollups(conn: sqlite3.Connection, session_id: str, rollups: list[dict[str, Any]]) -> None:
    conn.execute("DELETE FROM system_activity_session_rollups WHERE session_id = ?", (session_id,))
    now = utc_now()
    for row in rollups:
        conn.execute(
            """
            INSERT INTO system_activity_session_rollups(
              id, session_id, rollup_kind, label, category, duration_seconds,
              share, sample_count, confidence, rank, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"activity_rollup_{uuid.uuid4().hex}",
                session_id,
                row["rollup_kind"],
                row["label"],
                row.get("category") or "",
                int(row["duration_seconds"]),
                float(row["share"]),
                int(row.get("sample_count") or 0),
                float(row.get("confidence") or 0.0),
                int(row.get("rank") or 0),
                now,
            ),
        )


def _session_rollups(conn: sqlite3.Connection, session_id: str) -> dict[str, list[dict[str, Any]]]:
    from .presentation_scope import ReadView
    if isinstance(conn, ReadView):
        samples = [dict(r) for r in conn.execute('SELECT * FROM system_activity_samples WHERE session_id=?', (session_id,))]
        session = _session_row(conn, session_id)
        rows = _rollups(samples, duration_seconds=max(1, int(session['duration_seconds'] or 0)))
    else:
        rows = conn.execute(
            """
            SELECT rollup_kind, label, category, duration_seconds, share, sample_count, confidence, rank
            FROM system_activity_session_rollups
            WHERE session_id = ?
            ORDER BY rollup_kind ASC, rank ASC
            """,
            (session_id,),
        ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["rollup_kind"])].append(
            {
                "label": row["label"],
                "category": row["category"],
                "duration_seconds": int(row["duration_seconds"] or 0),
                "duration_label": _duration_label(int(row["duration_seconds"] or 0)),
                "share": float(row["share"] or 0.0),
                "share_label": _pct(row["share"]),
                "confidence": float(row["confidence"] or 0.0),
                "rank": int(row["rank"] or 0),
            }
        )
    return dict(grouped)


def _session_attribution_quality(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    duration_seconds: int,
    rollups: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT attribution_kind, category, category_label, duration_seconds, confidence,
               app_name, domain, source_type, privacy_flags
        FROM system_activity_samples
        WHERE session_id = ?
        """,
        (session_id,),
    ).fetchall()
    primary_rows = [dict(row) for row in rows if row["attribution_kind"] == "primary_focus"]
    visible_rows = [dict(row) for row in rows if row["attribution_kind"] == "visible_screen"]
    denominator = max(1, int(duration_seconds or sum(int(row.get("duration_seconds") or 0) for row in primary_rows) or 1))
    unknown_seconds = sum(int(row.get("duration_seconds") or 0) for row in primary_rows if _sample_is_unknown(row))
    low_confidence_seconds = sum(int(row.get("duration_seconds") or 0) for row in primary_rows if float(row.get("confidence") or 0.0) < 0.55)
    gap_seconds = sum(int(row.get("duration_seconds") or 0) for row in primary_rows if row.get("category") == "gap")
    privacy_seconds = sum(int(row.get("duration_seconds") or 0) for row in primary_rows if row.get("category") == "privacy_protected")
    visible_total = sum(int(row.get("duration_seconds") or 0) for row in visible_rows)
    visible_media_seconds = sum(int(row.get("duration_seconds") or 0) for row in visible_rows if row.get("category") == "media_entertainment")
    reason_counter: Counter[str] = Counter()
    for row in primary_rows:
        flags = safe_reason_codes(_json_list(row.get("privacy_flags")))
        low_confidence = float(row.get("confidence") or 0.0) < 0.55
        if _sample_is_unknown(row):
            unknown_reasons = [
                flag for flag in flags
                if (
                    flag.startswith("display_window_")
                    and flag != "display_window_match"
                ) or flag.startswith("browser_signal_") or flag in {"unknown_app", "low_confidence"}
            ]
            if not unknown_reasons:
                unknown_reasons = ["unknown_app"]
            reason_counter.update(unknown_reasons)
        if low_confidence:
            if flags:
                reason_counter.update(flag for flag in flags if flag not in {"main_display_frontmost", "display_window_match"})
            else:
                reason_counter["low_confidence"] += 1
        if row.get("category") == "gap":
            reason_counter["coverage_gap"] += 1
        if row.get("category") == "privacy_protected":
            reason_counter["privacy_protected"] += 1
    reason_counts = reason_count_rows(reason_counter)
    unknown_share = _share(unknown_seconds, denominator)
    low_confidence_share = _share(low_confidence_seconds, denominator)
    visible_media_share = _share(visible_media_seconds, max(1, visible_total))
    status = "ok"
    if unknown_share >= 0.35 or low_confidence_share >= 0.45:
        status = "poor"
    elif unknown_share >= 0.15 or low_confidence_share >= 0.25:
        status = "watch"
    label = {"ok": "归因质量正常", "watch": "归因需关注", "poor": "归因质量偏低"}[status]
    lines: list[str] = []
    if not primary_rows:
        lines.append("本次会话还没有可解释的安全样本。")
    if unknown_share >= 0.15:
        lines.append(f"Unknown 占比 {_pct(unknown_share)}：窗口和显示器无法可信匹配时会保留未知，不从主屏继承。")
    if low_confidence_share >= 0.25:
        lines.append(f"低置信度占比 {_pct(low_confidence_share)}：主要来自弱窗口归因、采集缺口或隐私保护降级。")
    if gap_seconds > 0:
        lines.append(f"未记录缺口 {_duration_label(gap_seconds)}：停采或锁屏边界附近不会虚增到具体 App。")
    if privacy_seconds > 0:
        lines.append(f"隐私保护 {_duration_label(privacy_seconds)}：只计入时长，不展示或汇总受保护内容。")
    if visible_media_seconds > 0 and not _primary_rollup_has_category(rollups, "media_entertainment"):
        lines.append(f"检测到多屏媒体曝光 {_duration_label(visible_media_seconds)}，但主焦点仍按工作/正文等高权重区域判定。")
    if reason_counts:
        top_reasons = "、".join(f"{item['label']} {item['count']} 次" for item in reason_counts[:3])
        lines.append(f"主要原因：{top_reasons}。")
    if not lines:
        lines.append("主焦点、可见曝光和可信域名的归因信号一致，未发现明显串屏或低置信问题。")
    return {
        "status": status,
        "label": label,
        "unknown_seconds": int(unknown_seconds),
        "unknown_share": unknown_share,
        "unknown_share_label": _pct(unknown_share),
        "low_confidence_seconds": int(low_confidence_seconds),
        "low_confidence_share": low_confidence_share,
        "low_confidence_share_label": _pct(low_confidence_share),
        "visible_media_seconds": int(visible_media_seconds),
        "visible_media_share": visible_media_share,
        "visible_media_share_label": _pct(visible_media_share),
        "gap_seconds": int(gap_seconds),
        "privacy_seconds": int(privacy_seconds),
        "attribution_reason_counts": reason_counts,
        "explanation_lines": [_safe_short(line, max_len=180) for line in lines[:5]],
        "raw_access": False,
    }


def _sample_is_unknown(row: dict[str, Any]) -> bool:
    app = str(row.get("app_name") or "").strip().lower()
    category = str(row.get("category") or "").strip()
    label = str(row.get("category_label") or "").strip()
    return category == "unknown" or label in {UNKNOWN_LABEL, "Unknown"} or app in {"", "unknown", "未知"}


def _primary_rollup_has_category(rollups: dict[str, list[dict[str, Any]]], category: str) -> bool:
    return any(row.get("category") == category and int(row.get("duration_seconds") or 0) > 0 for row in rollups.get("primary_category") or [])


def _quality_markdown_line(quality: dict[str, Any]) -> str:
    if not quality:
        return "- 归因质量：暂无。"
    line = (
        f"- 归因质量：{quality.get('label') or '未知'}；"
        f"Unknown {quality.get('unknown_share_label') or _pct(quality.get('unknown_share'))}；"
        f"低置信 {quality.get('low_confidence_share_label') or _pct(quality.get('low_confidence_share'))}。"
    )
    reasons = quality.get("attribution_reason_counts") if isinstance(quality.get("attribution_reason_counts"), list) else []
    if reasons:
        reason_text = "、".join(
            f"{str(item.get('label') or item.get('reason') or '')} {int(item.get('count') or 0)} 次"
            for item in reasons[:3]
            if isinstance(item, dict)
        )
        if reason_text:
            line += f" 主要原因：{reason_text}。"
    return line


def _close_session(conn: sqlite3.Connection, row: sqlite3.Row, ended_at: str, reason: str) -> str:
    start_dt = parse_time(str(row["started_at"]))
    end_dt = parse_time(ended_at)
    if end_dt < start_dt:
        end_dt = start_dt
        ended_at = iso(end_dt)
    duration = max(0, int((end_dt - start_dt).total_seconds()))
    now = utc_now()
    conn.execute(
        """
        UPDATE system_activity_sessions
        SET ended_at = ?, status = 'closed', end_reason = ?, duration_seconds = ?,
            last_observed_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (ended_at, reason, duration, ended_at, now, row["id"]),
    )
    refresh_activity_session(conn, str(row["id"]), active_end=ended_at)
    return str(row["id"])


def _open_session(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM system_activity_sessions
        WHERE status = 'open'
        ORDER BY started_at DESC
        LIMIT 1
        """
    ).fetchone()


def _session_row(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM system_activity_sessions WHERE id = ?", (session_id,)).fetchone()


def _max_slice_seconds(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = 'capture_interval_seconds'").fetchone()
        interval = int(str(row["value"])) if row else 15
    except (sqlite3.Error, ValueError, TypeError):
        interval = 15
    return max(30, min(180, interval * 2, DEFAULT_MAX_SLICE_SECONDS))


def _primary_score(row: dict[str, Any]) -> float:
    app = str(row.get("app_name") or "")
    signal = _screen_region(row)
    actionability = str(signal.get("actionability") or "")
    region_kind = str(signal.get("region_kind") or "")
    score = 0.0
    app_l = app.lower()
    if app and app_l != "unknown":
        score += 4
    if _is_browser_app(app) and row.get("domain"):
        score += 2
    if app_l in WORK_APPS or any(str(row.get("domain") or "").lower().endswith(item) for item in WORK_DOMAINS):
        score += 1.5
    if app_l in MEDIA_APPS:
        score -= 1.2
    if actionability == ACTIONABILITY_ACTIONABLE:
        score += 3.5 + ACTIONABLE_REGION_BONUS.get(region_kind, 0.0)
    elif actionability == ACTIONABILITY_CONTEXT_ONLY:
        score += 0.35
    elif actionability == ACTIONABILITY_NOISE:
        score -= 4.0
    if region_kind in PRIMARY_DEMOTE_REGIONS:
        score -= 1.2
    if region_kind == REGION_MEDIA_CHROME:
        score -= 1.0
    score += float(signal.get("confidence") or 0.0)
    return score


def _confidence(row: dict[str, Any], *, trusted_domain: bool) -> float:
    signal = _screen_region(row)
    score = 0.45
    if str(row.get("app_name") or "").lower() != "unknown":
        score += 0.2
    if trusted_domain:
        score += 0.15
    if str(signal.get("actionability") or "") == ACTIONABILITY_ACTIONABLE:
        score += 0.12
    elif str(signal.get("actionability") or "") == ACTIONABILITY_CONTEXT_ONLY:
        score -= 0.04
    elif str(signal.get("actionability") or "") == ACTIONABILITY_NOISE:
        score -= 0.18
    context_confidence = _context_confidence(row)
    if context_confidence > 0:
        score += max(-0.12, min(0.12, (context_confidence - 0.5) * 0.24))
    if 0 < context_confidence < 0.45:
        score -= 0.08
    score += min(0.08, max(0.0, float(signal.get("confidence") or 0.0) * 0.08))
    return round(max(0.05, min(0.98, score)), 3)


def _attribution_flags(row: dict[str, Any], *, trusted_domain: bool) -> list[str]:
    flags = attribution_reason_codes(
        source=str(row.get("context_source") or ""),
        app_contexts=row.get("context_app_contexts"),
    )
    app = str(row.get("app_name") or "").strip().lower()
    if app in {"", "unknown", "未知"}:
        flags.append("unknown_app")
    if 0 < _context_confidence(row) < 0.45:
        flags.append("low_confidence")
    if row.get("domain") and not trusted_domain and _is_browser_app(app):
        flags.append("browser_signal_ambiguous")
    return safe_reason_codes(flags)


def _context_confidence(row: dict[str, Any]) -> float:
    try:
        return float(row.get("context_confidence") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _screen_region(row: dict[str, Any]) -> dict[str, Any]:
    return sanitize_screen_region_signal(row.get("screen_region_signal")) or {}


def _category_for(app: str, domain: str, signal: dict[str, Any], text: str) -> tuple[str, str]:
    app_l = app.lower()
    domain_l = domain.lower()
    actionability = str(signal.get("actionability") or "")
    region_kind = str(signal.get("region_kind") or "")
    combined = " ".join([app_l, domain_l, text.lower(), region_kind])
    if app_l in MEDIA_APPS or any(domain_l.endswith(item) for item in MEDIA_DOMAINS):
        return "media_entertainment", "媒体娱乐"
    if app_l in COMMUNICATION_APPS or any(domain_l.endswith(item) for item in COMMUNICATION_DOMAINS):
        return "communication", "沟通"
    if app_l in TASK_APPS or region_kind == "task_list":
        return "task_background", "任务清单/背景"
    if app_l in SYSTEM_APPS:
        return "file_system", "文件/系统"
    if app_l in WORK_APPS or any(domain_l.endswith(item) for item in WORK_DOMAINS):
        return "work_ai", "工作 / AI 协作"
    if _is_browser_app(app) and domain:
        return "research_browser", "研究浏览"
    if actionability == ACTIONABILITY_NOISE:
        return "task_background", "任务清单/背景"
    if any(token in combined for token in ("movie", "drama", "anime", "电视剧", "动漫", "番剧")):
        return "media_entertainment", "媒体娱乐"
    if app:
        return "unknown", UNKNOWN_LABEL
    return "unknown", UNKNOWN_LABEL


def _category_key_from_label(label: str) -> str:
    mapping = {
        "工作 / AI 协作": "work_ai",
        "研究浏览": "research_browser",
        "沟通": "communication",
        "媒体娱乐": "media_entertainment",
        "文件/系统": "file_system",
        "任务清单/背景": "task_background",
        PRIVACY_LABEL: "privacy_protected",
        GAP_LABEL: "gap",
    }
    return mapping.get(label, "unknown")


def _is_browser_app(app: str) -> bool:
    return app.lower() in BROWSER_APPS


def _json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _safe_domain(value: str) -> str:
    text = value.strip().lower()
    if not text:
        return ""
    return text.split("/")[0].split("?")[0].split("@")[-1][:120]


def _safe_short(value: str, *, max_len: int = 120) -> str:
    text = " ".join(str(value or "").split())
    return text[:max_len]


def _share(seconds: int | float, denominator: int | float) -> float:
    denom = float(denominator or 0)
    if denom <= 0:
        return 0.0
    return round(float(seconds or 0) / denom, 4)


def _average(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(sum(values) / len(values), 3)


def _pct(value: Any) -> str:
    try:
        return f"{float(value or 0) * 100:.1f}%"
    except (TypeError, ValueError):
        return "0.0%"


def _duration_label(seconds: int) -> str:
    seconds = max(0, int(seconds or 0))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _counter_rows(counter: Counter[str], denominator: int, *, limit: int) -> list[dict[str, Any]]:
    return [
        {
            "label": label,
            "duration_seconds": int(seconds),
            "duration_label": _duration_label(int(seconds)),
            "share": _share(seconds, denominator),
            "share_label": _pct(_share(seconds, denominator)),
        }
        for label, seconds in counter.most_common(limit)
    ]


def _session_summary(rollups: dict[str, list[dict[str, Any]]], duration: int) -> str:
    primary = rollups.get("primary_category") or []
    if not primary:
        return "暂无可归因活动。"
    top = primary[0]
    return f"主要是 {top.get('label')}，约 {top.get('share_label')}；本次解锁 { _duration_label(duration) }。"


def _append_rollup(lines: list[str], title: str, rows: list[dict[str, Any]]) -> None:
    lines.extend([f"### {title}", ""])
    if not rows:
        lines.extend(["暂无。", ""])
        return
    for row in rows[:6]:
        lines.append(f"- {row.get('label')}: {row.get('duration_label')} / {row.get('share_label')}")
    lines.append("")
