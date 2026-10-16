from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import date, datetime, time, timedelta
from typing import Any

from .activity_confirmations import (
    confirmation_completeness,
    confirmed_activity_view,
    select_confirmed_activities,
)
from .document_changes import (
    DOCUMENT_TRUSTED_PROJECTION_VERSION,
    document_authorization_lock,
    document_context_projections,
)
from .user_decisions import safe_decision_text


REPORT_CONTEXT_VERSION = "0.9.1"
REPORT_CONTEXT_CONTRACT_VERSION = 2
HIGH_SIGNAL_APP_TERMS = (
    "面试",
    "interview",
    "tencentmeeting",
    "腾讯会议",
    "zoom",
    "google meet",
    "teams",
)


def report_context(conn: sqlite3.Connection, report_date: str | None = None) -> dict[str, Any]:
    requested_day = _report_day(report_date)
    start, end = _day_range(requested_day)
    try:
        # Keep the complete read and response construction under the same
        # revocation lease used by destructive deletion.  Returning a report
        # assembled before a completed delete would violate delete-wins.
        with document_authorization_lock(conn, exclusive=False):
            view = confirmed_activity_view(conn, start, end, materialize=False)
            activities = list(view.get("trusted_activities") or [])
            status_counts = _status_counts(activities)
            privacy_count = _privacy_count(conn, start, end)
            coverage = _coverage(conn, start, end)
            top_apps, high_signal_apps = _app_summaries(activities)
            highlights = [_highlight(item) for item in select_confirmed_activities(activities, limit=32)]
            highlights = [item for item in highlights if item]
            document_projections = document_context_projections(conn, start, end, limit=100)
            owner_local_documents = document_projections["owner_local_projection"]
            document_changes = document_projections["trusted_projection"]
            completeness = confirmation_completeness(conn, start, end, view=view)
            from .context_console import report_context_bundle_adapter

            context_bundle = report_context_bundle_adapter(
                conn,
                start_at=start,
                end_at=end,
            )
            return {
                "version": REPORT_CONTEXT_VERSION,
                "report_context_contract_version": REPORT_CONTEXT_CONTRACT_VERSION,
                "raw_access": False,
                "report_date": requested_day.isoformat(),
                "availability": "available",
                "pending_confirmation_count": int(view.get("pending_confirmation_count") or 0),
                "confirmation_completeness": completeness,
                "clear_count": int(status_counts.get("clear", 0)),
                "review_count": int(status_counts.get("review_required", 0)),
                "blocked_count": int(status_counts.get("blocked", 0)),
                "privacy_count": privacy_count,
                "coverage": coverage,
                "top_apps": top_apps,
                "high_signal_apps": high_signal_apps,
                "activity_highlights": highlights,
                # Trusted report input receives verified contexts only. Local
                # document observations remain a count-only clue on this surface.
                "document_changes": document_changes,
                "document_unverified_clue_count": len(owner_local_documents.get("items") or []),
                # Additive compatibility projection. Contract-v2 consumers can
                # ignore it; new consumers receive only verified ContextBundle
                # items and never the clue partition.
                "context_bundle_v1": context_bundle,
            }
    except (sqlite3.Error, TypeError, ValueError, RuntimeError):
        return _unavailable_payload(requested_day)


def _unavailable_payload(requested_day: date) -> dict[str, Any]:
    return {
        "version": REPORT_CONTEXT_VERSION,
        "report_context_contract_version": REPORT_CONTEXT_CONTRACT_VERSION,
        "raw_access": False,
        "report_date": requested_day.isoformat(),
        "availability": "unavailable",
        "pending_confirmation_count": None,
        "confirmation_completeness": {
            "status": "unavailable",
            "pending_total": None,
            "pending_high_priority": None,
            "pending_by_kind": None,
            "candidate_funnel": None,
            "settled_at": None,
            "revision": "",
        },
        "clear_count": None,
        "review_count": None,
        "blocked_count": None,
        "privacy_count": None,
        "coverage": None,
        "top_apps": [],
        "high_signal_apps": [],
        "activity_highlights": [],
        # Preserve the established fixed fail-closed schema on database
        # unavailability.  No projection was actually read, so an empty legacy
        # field is safer than manufacturing a typed trusted result.
        "document_changes": {},
        "document_unverified_clue_count": 0,
        "context_bundle_v1": {
            "contract_version": "ContextBundleV1",
            "status": "unavailable",
            "raw_access": False,
            "verified_contexts": [],
            "unverified_clues": [],
        },
    }


def _report_day(value: str | None) -> date:
    if value:
        return date.fromisoformat(value)
    return datetime.now().astimezone().date()


def _day_range(value: date) -> tuple[str, str]:
    tz = datetime.now().astimezone().tzinfo
    start = datetime.combine(value, time.min, tzinfo=tz)
    end = start + timedelta(days=1)
    return start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")


def _status_counts(activities: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter(
        str(item.get("quarantine_status") or "clear")
        for item in activities
    )
    return dict(counts)


def _privacy_count(conn: sqlite3.Connection, start: str, end: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS count FROM privacy_events WHERE datetime(started_at)>=datetime(?) AND datetime(started_at)<datetime(?)",
        (start, end),
    ).fetchone()
    return int(row["count"] or 0) if row else 0


def _coverage(conn: sqlite3.Connection, start: str, end: str) -> float | None:
    row = conn.execute(
        "SELECT AVG(coverage_ratio) AS ratio FROM capture_coverage_ledger WHERE datetime(started_at)>=datetime(?) AND datetime(started_at)<datetime(?)",
        (start, end),
    ).fetchone()
    if not row or row["ratio"] is None:
        return None
    return round(max(0.0, min(1.0, float(row["ratio"]))), 4)


def _app_summaries(activities: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    counter: Counter[str] = Counter()
    windows: dict[str, dict[str, Any]] = {}
    for activity in activities:
        apps = _string_list(activity.get("apps"))
        for app in apps:
            counter[app] += 1
            if any(term in app.lower() for term in HIGH_SIGNAL_APP_TERMS):
                state = windows.setdefault(
                    app,
                    {"app": app, "count": 0, "first_seen": None, "last_seen": None},
                )
                state["count"] = int(state["count"]) + 1
                observed_start = str(activity.get("started_at") or "") or None
                observed_end = str(activity.get("ended_at") or activity.get("started_at") or "") or None
                if observed_start and (state["first_seen"] is None or observed_start < state["first_seen"]):
                    state["first_seen"] = observed_start
                if observed_end and (state["last_seen"] is None or observed_end > state["last_seen"]):
                    state["last_seen"] = observed_end
    top_apps = [{"name": name, "count": count} for name, count in counter.most_common(10)]
    high_signal_apps = sorted(windows.values(), key=lambda item: int(item["count"]), reverse=True)[:8]
    return top_apps, high_signal_apps


def _highlight(activity: dict[str, Any]) -> dict[str, Any] | None:
    state = str(activity.get("occurrence_state") or "")
    # Defense in depth: legacy deterministic/screen evidence must never enter
    # the trusted report body even if another caller constructs a malformed
    # confirmed_activity_view payload.
    if str(activity.get("confirmation_source") or "") != "user":
        return None
    if state not in {"user_confirmed", "user_confirmed_preparation"}:
        return None
    apps = _string_list(activity.get("apps"))
    summary = safe_decision_text(activity.get("activity_summary") or "", max_len=220)
    if not summary:
        return None
    return {
        "time": _time_label(str(activity.get("started_at") or ""), str(activity.get("ended_at") or "")),
        "apps": apps[:4],
        "summary": summary,
        "evidence_type": "confirmed_activity_view",
        "occurrence_state": state,
        "confirmation_source": str(activity.get("confirmation_source") or "deterministic_evidence"),
        "event_type": str(activity.get("event_type") or "observed_activity"),
        "outcome": str(activity.get("outcome") or "unknown"),
        "workstream": safe_decision_text(activity.get("workstream") or "", max_len=120),
    }


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = [value]
    if not isinstance(value, list):
        return []
    output: list[str] = []
    for item in value:
        safe = safe_decision_text(item, max_len=80)
        if safe and safe not in output:
            output.append(safe)
    return output


def _time_label(start: str, end: str) -> str:
    try:
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat((end or start).replace("Z", "+00:00"))
        return f"{start_dt.strftime('%H:%M')}-{end_dt.strftime('%H:%M')}"
    except ValueError:
        return ""
