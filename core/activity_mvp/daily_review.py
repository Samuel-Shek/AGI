from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta

from .activity_confirmations import confirmed_activity_view, select_confirmed_activities
from .pipeline import parse_time
from .document_changes import document_authorization_lock, document_context_projections
from .presentation_scope import display_read


@display_read
def daily_review(conn: sqlite3.Connection, start: str | None = None, end: str | None = None) -> dict:
    with document_authorization_lock(conn, exclusive=False):
        return _daily_review_locked(conn, start, end)


def _daily_review_locked(conn: sqlite3.Connection, start: str | None = None, end: str | None = None) -> dict:
    if start is None or end is None:
        start, end = today_range()
    confirmation_view = confirmed_activity_view(conn, start, end)
    verified_activities = list(confirmation_view.get("trusted_activities") or [])
    owner_activities = list(confirmation_view.get("owner_activities") or [])
    unverified_activity_clues = list(confirmation_view.get("unverified_activity_clues") or [])
    local_activities = sorted(
        owner_activities,
        key=lambda item: str(item.get("started_at") or ""),
    )
    app_counter: Counter[str] = Counter()
    domain_counter: Counter[str] = Counter()
    for item in local_activities:
        for app in _json_list(item.get("apps")):
            if app:
                app_counter[app] += 1
        for domain in _json_list(item.get("domains")):
            if domain:
                domain_counter[domain] += 1

    highlights = [
        _compact_summary(item["activity_summary"])
        for item in select_confirmed_activities(local_activities, limit=8)
    ]
    trusted_highlights = [
        _compact_summary(item["activity_summary"])
        for item in select_confirmed_activities(verified_activities, limit=8)
    ]
    coverage = _coverage_summary(conn, start, end)
    # Native Today/Review is an owner-local surface.  Keep its unverified
    # document observation separate from the verified-only downstream export.
    document_projections = document_context_projections(conn, start, end, limit=100)
    document_changes = document_projections["owner_local_projection"]
    trusted_document_projection = document_projections["trusted_projection"]
    return {
        "source": "local_owner_observation_v1",
        "confirmation_gate": "confirmed_activity_view",
        "owner_observation_projection": "local_observation_v2",
        "trusted_projection": "trusted_activity_v2",
        "cloud_analysis": "off",
        "time_range": {"from": start, "to": end},
        # Legacy owner-facing count remains the number of locally clear
        # observations. Trusted prose is separately gated below.
        "activity_count": len(local_activities),
        "verified_activity_count": len(verified_activities),
        "pending_confirmation_count": int(confirmation_view.get("pending_confirmation_count") or 0),
        "user_confirmed_count": int(confirmation_view.get("user_confirmed_count") or 0),
        "user_rejected_count": int(confirmation_view.get("user_rejected_count") or 0),
        "auto_suppressed_count": int(confirmation_view.get("auto_suppressed_count") or 0),
        "unverified_clue_count": int(confirmation_view.get("unverified_clue_count") or 0),
        "top_apps": [name for name, _ in app_counter.most_common(5)],
        "top_domains": [name for name, _ in domain_counter.most_common(5)],
        "privacy_event_count": coverage["privacy_event_count"],
        "gap_event_count": coverage["gap_event_count"],
        "coverage": coverage,
        # Compatibility field remains owner-local, but it is now a named,
        # versioned unverified projection rather than an untyped export array.
        "document_changes": document_changes,
        "document_owner_local_projection": document_changes,
        "document_trusted_projection": trusted_document_projection,
        "document_unverified_clue_count": len(document_changes.get("items") or []),
        "highlights": highlights,
        "trusted_highlights": trusted_highlights,
        "summary": _summary_sentence(
            len(local_activities),
            app_counter,
            domain_counter,
            verified_count=len(verified_activities),
        ),
    }


def today_range() -> tuple[str, str]:
    start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")


def _json_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return [str(item) for item in parsed if item]


def _compact_summary(value: str) -> str:
    text = " ".join(str(value).split())
    return text[:180]


def _summary_sentence(
    count: int,
    apps: Counter[str],
    domains: Counter[str],
    *,
    verified_count: int,
) -> str:
    if count == 0:
        return "No clear local activity was available for this range."
    top_apps = ", ".join(name for name, _ in apps.most_common(3)) or "local apps"
    top_domains = ", ".join(name for name, _ in domains.most_common(3))
    trust_note = f" {verified_count} were explicitly user-confirmed; the rest are unverified local clues."
    if top_domains:
        return (
            f"{count} clear local observations, mainly in {top_apps}, with web context from {top_domains}."
            + trust_note
        )
    return f"{count} clear local observations, mainly in {top_apps}." + trust_note


def _coverage_summary(conn: sqlite3.Connection, start: str, end: str) -> dict:
    ledger_rows = conn.execute(
        """
        SELECT started_at, clear_count, blocked_count, review_required_count, gap_count, frames_dropped
        FROM capture_coverage_ledger
        WHERE datetime(ended_at) > datetime(?)
          AND datetime(started_at) < datetime(?)
        ORDER BY datetime(started_at) ASC
        """,
        (start, end),
    ).fetchall()
    hourly: dict[str, Counter[str]] = {}
    for row in ledger_rows:
        hour = parse_time(row["started_at"]).astimezone().replace(minute=0, second=0, microsecond=0)
        label = hour.isoformat(timespec="minutes")
        bucket = hourly.setdefault(label, Counter())
        bucket["clear"] += int(row["clear_count"] or 0)
        bucket["blocked"] += int(row["blocked_count"] or 0)
        bucket["review"] += int(row["review_required_count"] or 0)
        bucket["gap"] += int(row["gap_count"] or 0)
        bucket["frames_dropped"] += int(row["frames_dropped"] or 0)

    privacy_count = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM privacy_events
        WHERE datetime(COALESCE(ended_at, started_at)) > datetime(?)
          AND datetime(started_at) < datetime(?)
        """,
        (start, end),
    ).fetchone()["count"]
    gap_count = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM gap_events
        WHERE datetime(COALESCE(ended_at, started_at)) > datetime(?)
          AND datetime(started_at) < datetime(?)
        """,
        (start, end),
    ).fetchone()["count"]
    hourly_counts = [
        {
            "hour": hour,
            "clear": counts["clear"],
            "blocked": counts["blocked"],
            "review": counts["review"],
            "gap": counts["gap"],
            "frames_dropped": counts["frames_dropped"],
        }
        for hour, counts in sorted(hourly.items())
    ]
    return {
        "ledger_rows": len(ledger_rows),
        "recorded_hours": len(hourly_counts),
        "privacy_event_count": int(privacy_count or 0),
        "gap_event_count": int(gap_count or 0),
        "frames_dropped": sum(item["frames_dropped"] for item in hourly_counts),
        "hourly_counts": hourly_counts,
    }
