from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .adult_policy import redact_terms as redact_adult_terms, text_hit as adult_text_hit
from .document_changes import AUTHORIZATION_LOCK_FILENAME, document_authorization_lock
from .pipeline import parse_time, timeline, utc_now
from .privacy import PRIVACY_FIREWALL_VERSION, REDACTION_POLICY_VERSION
from .private_purge import (
    complete_private_purge,
    mark_private_purge_failed,
    pending_private_purge_ids,
    private_purge_failure_code,
    schedule_private_purge,
)
from .retention import retention_until_for_range_start
from .timeline_export import cleaned_timeline_export
from .user_decisions import CONTEXT_ONLY, NEVER_WRITE, find_user_decision, item_key_for_memory_candidate, public_decision


FORBIDDEN_KEY_RE = re.compile(
    r"(raw|screenshot|frame_path|temp|ocr_original|document_original|file_path|api[_-]?key|password|secret)",
    re.I,
)
FORBIDDEN_VALUE_RE = re.compile(
    r"(-----BEGIN .*PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{20,}\b|/Users/|/tmp/|~/)",
    re.I,
)
LOCAL_PATH_RE = re.compile(r"(/Users/[^\s\"'`，。；;]*|/tmp/[^\s\"'`，。；;]*|~/[^\s\"'`，。；;]*)", re.I)
SECRET_VALUE_RE = re.compile(r"(-----BEGIN .*PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{20,}\b)", re.I)
SOURCE_STATUSES = {"active", "expired_deleted", "user_deleted", "revoked"}
HA_UNVERIFIED_CLUE_PROJECTION_VERSION = "HAUnverifiedClueV1"
HA_PACKAGE_ID_RE = re.compile(r"^ha_pkg_[0-9a-f]{32}$")
REQUIRED_PACKAGE_FIELDS = {
    "package_id",
    "source_device",
    "source_type",
    "time_range",
    "display_ids",
    "coverage_scope",
    "coverage_rate",
    "gap_summary",
    "quarantine_status",
    "raw_access",
    "privacy_firewall_version",
    "redaction_policy_version",
    "source_status",
    "deletion_group_ids",
    "created_at",
    "retention_until",
    "target",
    "source",
    "memory_write_policy",
    "learning_digest",
    "learnability_diagnostics",
    "activities",
    "safe_timeline_events",
}
EXPECTED_PACKAGE_VALUES = {
    "source_device": "this_mac",
    "source_type": "screen_activity",
    "quarantine_status": "clear_only",
    "raw_access": False,
    "privacy_firewall_version": PRIVACY_FIREWALL_VERSION,
    "redaction_policy_version": REDACTION_POLICY_VERSION,
    "source_status": "active",
    "target": "ha",
    "source": "extracted_only",
    "memory_write_policy": "ephemeral_only",
}


def _ensure_ha_authorization_lock(conn: sqlite3.Connection) -> None:
    """Create the shared revocation barrier before HA data can exist.

    The generic document lease permits a missing lock file on a brand-new
    database with no document rows.  HA packages can already contain cleaned
    screen activity in that state, so allowing the first reader to proceed
    lock-free would reopen a get/revoke race.  The short exclusive preflight is
    needed only once; all normal reads then take the shared lease below.
    """
    database_file = ""
    for row in conn.execute("PRAGMA database_list").fetchall():
        name = str(row[1] if not isinstance(row, sqlite3.Row) else row["name"])
        if name != "main":
            continue
        database_file = str(row[2] if not isinstance(row, sqlite3.Row) else row["file"])
        break
    if not database_file:
        return
    lock_path = Path(database_file).resolve().parent / AUTHORIZATION_LOCK_FILENAME
    if lock_path.exists():
        return
    # If delete/reclassification creates or acquires the lock first, this
    # waits for it to commit.  Reacquiring the shared lease afterwards then
    # guarantees that the HA read observes the post-revocation state.
    with document_authorization_lock(conn, exclusive=True):
        pass


def generate_package(
    conn: sqlite3.Connection,
    start: str,
    end: str,
    output_dir: str | Path,
    write_markdown: bool = True,
    generation_mode: str = "on_demand",
) -> dict:
    # Package generation is a public export.  Keep one authorization lease
    # from the first source read through the cache writes and database commit,
    # so delete/reclassification cannot commit between those phases and leave
    # a newly-active package containing pre-revocation content.
    # Callers historically leave default INSERT OR IGNORE statements pending.
    # They contain no package source data and must not make the long export take
    # SQLite's single writer slot before the authorization lease is acquired.
    if conn.in_transaction:
        conn.commit()
    _ensure_ha_authorization_lock(conn)
    with document_authorization_lock(conn, exclusive=False):
        try:
            # WAL readers do not block capture/status writers.  The shared
            # authorization lease, not a write-intent transaction, serializes
            # the source snapshot and cache against revocation.
            conn.execute("BEGIN")
            return _generate_package_locked(
                conn,
                start,
                end,
                output_dir,
                write_markdown=write_markdown,
                generation_mode=generation_mode,
            )
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise


def _generate_package_locked(
    conn: sqlite3.Connection,
    start: str,
    end: str,
    output_dir: str | Path,
    write_markdown: bool = True,
    generation_mode: str = "on_demand",
) -> dict:
    export = cleaned_timeline_export(conn, start, end)
    activities = list(export.get("trusted_activities") or [])
    coverage = _coverage(conn, start, end)
    gap_summary = _gap_summary(conn, start, end)
    package_id = f"ha_pkg_{uuid.uuid4().hex}"
    created_at = utc_now()
    retention_until_dt = retention_until_for_range_start(start)
    retention_until = retention_until_dt.isoformat(timespec="seconds")
    cache_ttl_seconds = max(
        0,
        int((retention_until_dt - datetime.now(timezone.utc)).total_seconds()),
    )
    deletion_group_ids = sorted(
        {item["deletion_group_id"] for item in activities if item.get("deletion_group_id")}
    )

    package = {
        "package_id": package_id,
        "source_device": "this_mac",
        "source_type": "screen_activity",
        "time_range": {"from": start, "to": end},
        "display_ids": coverage["display_ids"],
        "coverage_scope": "authorized_mac_displays_only",
        "coverage_rate": coverage["coverage_rate"],
        "gap_summary": gap_summary,
        "quarantine_status": "clear_only",
        "raw_access": False,
        "privacy_firewall_version": PRIVACY_FIREWALL_VERSION,
        "redaction_policy_version": REDACTION_POLICY_VERSION,
        "source_status": "active",
        "deletion_group_ids": deletion_group_ids,
        "created_at": created_at,
        "retention_until": retention_until,
        "target": "ha",
        "source": "extracted_only",
        "memory_write_policy": "ephemeral_only",
        "cloud_analysis_default": "off",
        "learning_digest": _sanitize_package_value(export.get("trusted_learning_digest") or {}),
        "learnability_diagnostics": _learnability_diagnostics(export, coverage, gap_summary),
        "safe_timeline_events": _safe_timeline_events(export.get("ha_source_events") or []),
        "activities": [
            {
                "activity_id": item["id"],
                "time_range": {"from": item["started_at"], "to": item["ended_at"]},
                "summary": _sanitize_package_text(item["activity_summary"]),
                "cleaned_content": _sanitize_package_text(item.get("cleaned_text") or item["activity_summary"]),
                "apps": item["apps"],
                "domains": item["domains"],
                "evidence_refs": item["evidence_refs"],
                "document_contexts": _safe_document_contexts(item.get("document_contexts") or []),
                "privacy_level": item["privacy_level"],
                "redaction_confidence": item["redaction_confidence"],
            }
            for item in activities
        ],
    }
    validate_package(package)
    # End the potentially expensive read/regex snapshot before serializing the
    # cache.  The authorization lease remains held through the later metadata
    # commit, so deletion still cannot land between source read and package
    # activation.
    conn.commit()

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir.chmod(0o700)
    json_path = out_dir / f"{package_id}.json"
    _write_private_text(json_path, json.dumps(package, ensure_ascii=False, indent=2))
    markdown_path = None
    if write_markdown:
        markdown_path = out_dir / f"{package_id}.md"
        _write_private_text(markdown_path, render_markdown(package))

    conn.execute("BEGIN IMMEDIATE")
    date_range = json.dumps({"from": start, "to": end}, ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO distribution_packages(
          id, target, date_range, format, redaction_policy, source, raw_access,
          generation_mode, cache_ref, cache_ttl_seconds, cache_expires_at,
          privacy_firewall_version, redaction_policy_version, memory_write_policy,
          capability_scope, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            package_id,
            "ha",
            date_range,
            "json",
            REDACTION_POLICY_VERSION,
            "extracted_only",
            0,
            generation_mode,
            str(json_path),
            cache_ttl_seconds,
            retention_until,
            PRIVACY_FIREWALL_VERSION,
            REDACTION_POLICY_VERSION,
            "ephemeral_only",
            "read_activity",
            created_at,
        ),
    )
    conn.execute(
        """
        INSERT INTO ha_source_records(
          id, package_id, source_status, source_device, source_type,
          time_range, coverage_rate, retention_until, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"ha_record_{uuid.uuid4().hex}",
            package_id,
            "active",
            "this_mac",
            "screen_activity",
            date_range,
            coverage["coverage_rate"],
            retention_until,
            created_at,
            created_at,
        ),
    )
    conn.execute(
        """
        INSERT INTO audit_logs(id, at, action, target_type, target_id, detail)
        VALUES(?, ?, ?, ?, ?, ?)
        """,
        (
            f"audit_{uuid.uuid4().hex}",
            created_at,
            "generate_ha_source_package",
            "distribution_package",
            package_id,
            json.dumps(
                {
                    "generation_mode": generation_mode,
                    "cloud_analysis": "off",
                    "activity_count": len(activities),
                    "safe_timeline_event_count": len(export["items"]),
                    "duplicate_event_count": len(export.get("duplicate_events", [])),
                    "privacy_event_count": len(export["privacy_events"]),
                    "gap_event_count": len(export["gap_events"]),
                },
                ensure_ascii=False,
            ),
        ),
    )
    conn.commit()
    return {
        "package": package,
        "json_path": str(json_path),
        "markdown_path": str(markdown_path) if markdown_path else None,
    }


def get_package(conn: sqlite3.Connection, package_id: str) -> dict:
    _ensure_ha_authorization_lock(conn)
    with document_authorization_lock(conn, exclusive=False):
        package = _get_package_locked(conn, package_id)
    reason = _implicit_revocation_reason(package)
    if reason:
        return _finalize_implicit_package_revocation(conn, package_id, reason)
    return package


def _package_access_now() -> datetime:
    """Return the authorization clock used for package access decisions."""
    return datetime.now(timezone.utc)


def _get_package_locked(conn: sqlite3.Connection, package_id: str) -> dict:
    record = conn.execute(
        """
        SELECT d.id, d.cache_ref, d.date_range, h.source_status, h.retention_until
        FROM distribution_packages d
        JOIN ha_source_records h ON h.package_id = d.id
        WHERE d.id = ?
        ORDER BY h.updated_at DESC
        LIMIT 1
        """,
        (package_id,),
    ).fetchone()
    if not record:
        return {"package_id": package_id, "source_status": "not_found"}
    if record["source_status"] != "active":
        return {
            "package_id": package_id,
            "source_status": record["source_status"],
            "time_range": json.loads(record["date_range"]),
            "raw_access": False,
        }
    try:
        retention_expired = (
            parse_time(str(record["retention_until"] or "")) <= _package_access_now()
        )
    except (TypeError, ValueError):
        retention_expired = True
    if retention_expired:
        # Cleanup is intentionally deferred away from cold start, but the
        # privacy boundary is not: an overdue cache must become unreadable at
        # its deadline even when the physical purge worker has not run yet.
        return {
            "package_id": package_id,
            "source_status": "expired_deleted",
            "reason": "retention_expired",
            "time_range": json.loads(record["date_range"]),
            "raw_access": False,
        }
    try:
        path = _safe_private_cache_path(
            Path(record["cache_ref"]),
        )
        cache_metadata = path.lstat()
    except FileNotFoundError:
        return {
            "package_id": package_id,
            "source_status": "revoked",
            "reason": "cache_missing",
            "time_range": json.loads(record["date_range"]),
            "raw_access": False,
        }
    except OSError:
        return {
            "package_id": package_id,
            "source_status": "revoked",
            "reason": "cache_unreadable",
            "time_range": json.loads(record["date_range"]),
            "raw_access": False,
        }
    if (
        not stat.S_ISREG(cache_metadata.st_mode)
        or cache_metadata.st_uid != os.getuid()
    ):
        return {
            "package_id": package_id,
            "source_status": "revoked",
            "reason": "cache_unreadable",
            "time_range": json.loads(record["date_range"]),
            "raw_access": False,
        }
    try:
        package = json.loads(path.read_text(encoding="utf-8"))
        validate_package(package)
    except FileNotFoundError:
        return {
            "package_id": package_id,
            "source_status": "revoked",
            "reason": "cache_missing",
            "time_range": json.loads(record["date_range"]),
            "raw_access": False,
        }
    except OSError:
        return {
            "package_id": package_id,
            "source_status": "revoked",
            "reason": "cache_unreadable",
            "time_range": json.loads(record["date_range"]),
            "raw_access": False,
        }
    except (ValueError, json.JSONDecodeError):
        return {
            "package_id": package_id,
            "source_status": "revoked",
            "reason": "package_validation_failed",
            "time_range": json.loads(record["date_range"]),
            "raw_access": False,
        }
    return package


def get_package_handoff_summary(conn: sqlite3.Connection, package_id: str, max_items: int = 40) -> dict:
    # Keep the lease after the cache read while the reduced handoff payload is
    # assembled; otherwise deletion could commit after get_package() returned
    # internally but before this public function returned stale content.
    _ensure_ha_authorization_lock(conn)
    with document_authorization_lock(conn, exclusive=False):
        summary = _get_package_handoff_summary_locked(conn, package_id, max_items=max_items)
    reason = _implicit_revocation_reason(summary)
    if reason:
        package = _finalize_implicit_package_revocation(conn, package_id, reason)
        return _unavailable_package_projection(
            package,
            package_id,
            projection_key="handoff_summary",
        )
    return summary


def _get_package_handoff_summary_locked(conn: sqlite3.Connection, package_id: str, max_items: int = 40) -> dict:
    package = _get_package_locked(conn, package_id)
    if package.get("source_status") != "active":
        return {
            "package_id": package_id,
            "source_status": package.get("source_status", "unknown"),
            "reason": package.get("reason", ""),
            "time_range": package.get("time_range") or package.get("date_range") or {},
            "raw_access": False,
            "handoff_summary": True,
        }
    limit = max(1, int(max_items or 40))
    activities = package.get("activities") or []
    safe_events = package.get("safe_timeline_events") or []
    learning_digest = package.get("learning_digest") or {}
    diagnostics = package.get("learnability_diagnostics") or {}
    return {
        "package_id": package["package_id"],
        "source_status": package["source_status"],
        "time_range": package["time_range"],
        "raw_access": package["raw_access"],
        "quarantine_status": package["quarantine_status"],
        "source": package["source"],
        "memory_write_policy": package["memory_write_policy"],
        "coverage_rate": package["coverage_rate"],
        "gap_summary": package["gap_summary"],
        "activity_count": len(activities),
        "safe_timeline_event_count": len(safe_events),
        "handoff_summary": True,
        "learning_digest": {
            "summary": learning_digest.get("summary") or "",
            "learnability_counts": learning_digest.get("learnability_counts") or {},
            "workflow_summary": _take(learning_digest.get("workflow_summary") or [], limit),
            "memory_candidates": _take(learning_digest.get("memory_candidates") or [], limit),
        },
        "learnability_diagnostics": {
            "summary": diagnostics.get("summary") or "",
            "learnability_counts": diagnostics.get("learnability_counts") or {},
            "quality": diagnostics.get("quality") or {},
            "coverage": diagnostics.get("coverage") or {},
        },
        "representative_activities": [
            {
                "activity_id": item.get("activity_id"),
                "time_range": item.get("time_range"),
                "summary": item.get("summary"),
                "apps": item.get("apps") or [],
                "domains": item.get("domains") or [],
                "privacy_level": item.get("privacy_level"),
                "redaction_confidence": item.get("redaction_confidence"),
            }
            for item in activities[:limit]
        ],
    }


def get_package_memory_candidates(conn: sqlite3.Connection, package_id: str, max_items: int = 40) -> dict:
    # Candidate filtering performs additional database reads after loading the
    # cache.  Hold one lease over both phases to avoid a mixed pre/post-delete
    # result.
    _ensure_ha_authorization_lock(conn)
    with document_authorization_lock(conn, exclusive=False):
        candidates = _get_package_memory_candidates_locked(conn, package_id, max_items=max_items)
    reason = _implicit_revocation_reason(candidates)
    if reason:
        package = _finalize_implicit_package_revocation(conn, package_id, reason)
        return _unavailable_package_projection(
            package,
            package_id,
            projection_key="memory_candidates_only",
        )
    return candidates


def _get_package_memory_candidates_locked(conn: sqlite3.Connection, package_id: str, max_items: int = 40) -> dict:
    package = _get_package_locked(conn, package_id)
    if package.get("source_status") != "active":
        return {
            "package_id": package_id,
            "source_status": package.get("source_status", "unknown"),
            "reason": package.get("reason", ""),
            "time_range": package.get("time_range") or package.get("date_range") or {},
            "raw_access": False,
            "memory_candidates_only": True,
        }
    limit = max(1, int(max_items or 40))
    learning_digest = package.get("learning_digest") or {}
    diagnostics = package.get("learnability_diagnostics") or {}
    candidate_quality = learning_digest.get("memory_candidate_quality") or {}
    all_candidates = learning_digest.get("memory_candidates") or []
    all_candidates = _apply_user_decisions_to_memory_candidates(conn, package["package_id"], all_candidates)
    eligible_items = [
        item
        for item in all_candidates
        if str(item.get("write_eligibility") or "") == "eligible"
        and str(item.get("evidence_layer") or "context_clue") == "confirmed_action"
    ]
    review_only_items = [
        item
        for item in all_candidates
        if str(item.get("write_eligibility") or "") == "review_only"
        and str(item.get("evidence_layer") or "context_clue") != "noise_or_reference"
    ]
    candidates = []
    for item in _take(eligible_items, limit):
        safe_summary = _compact_handoff_text(item.get("safe_summary") or "")
        if not safe_summary:
            continue
        candidates.append(
            {
                "time_label": _compact_handoff_text(item.get("time_label") or "", 80),
                "candidate_key": _compact_handoff_text(item.get("candidate_key") or "", 80),
                "workflow": _compact_handoff_text(item.get("workflow") or "", 120),
                "topic": _compact_handoff_text(item.get("topic") or "", 120),
                "app": _compact_handoff_text(item.get("app") or "", 80),
                "safe_summary": safe_summary,
                "candidate_type": _compact_handoff_text(item.get("candidate_type") or "activity_fact", 80),
                "confidence": float(item.get("confidence") or 0.0),
                "why_keep": _compact_handoff_text(item.get("why_keep") or "", 180),
                "evidence_count": int(item.get("evidence_count") or 1),
                "project_tags": [
                    _compact_handoff_text(value, 80)
                    for value in (item.get("project_tags") or [])[:5]
                    if _compact_handoff_text(value, 80)
                ],
                "anchor_type": _compact_handoff_text(item.get("anchor_type") or "weak_or_no_anchor", 80),
                "anchor_strength": int(item.get("anchor_strength") or 0),
                "source_category": _compact_handoff_text(item.get("source_category") or "general_document", 80),
                "source_scope": _compact_handoff_text(item.get("source_scope") or "context_only", 80),
                "memory_write_policy": _compact_handoff_text(item.get("memory_write_policy") or "workflow_only", 80),
                "subject_scope": _compact_handoff_text(item.get("subject_scope") or "user_workflow_only", 80),
                "subject_confidence": float(item.get("subject_confidence") or 0.0),
                "subject_reasons": [
                    _compact_handoff_text(value, 80)
                    for value in (item.get("subject_reasons") or [])[:6]
                    if _compact_handoff_text(value, 80)
                ],
                "subject_risk_flags": [
                    _compact_handoff_text(value, 80)
                    for value in (item.get("subject_risk_flags") or [])[:8]
                    if _compact_handoff_text(value, 80)
                ],
                "quality_flags": [
                    _compact_handoff_text(value, 80)
                    for value in (item.get("quality_flags") or [])[:8]
                    if _compact_handoff_text(value, 80)
                ],
                "write_risk_flags": [
                    _compact_handoff_text(value, 80)
                    for value in (item.get("write_risk_flags") or [])[:8]
                    if _compact_handoff_text(value, 80)
                ],
                "write_eligibility": "eligible",
                "evidence_layer": _compact_handoff_text(item.get("evidence_layer") or "confirmed_action", 80),
                "confirmation_signals": [
                    _compact_handoff_text(value, 80)
                    for value in (item.get("confirmation_signals") or [])[:8]
                    if _compact_handoff_text(value, 80)
                ],
                "evidence_explanation": _compact_handoff_text(item.get("evidence_explanation") or "", 220),
                "screen_region_signal": _compact_screen_region_signal(item.get("screen_region_signal") or {}),
                "screen_region_signals": [
                    _compact_screen_region_signal(value)
                    for value in (item.get("screen_region_signals") or [])[:8]
                    if _compact_screen_region_signal(value)
                ],
                "evidence_span_minutes": int(item.get("evidence_span_minutes") or 0),
                "merged_duplicate_count": int(item.get("merged_duplicate_count") or 0),
                "user_decision": item.get("user_decision") or {},
            }
        )
    review_only_candidates = []
    for item in _take(review_only_items, min(limit, 12)):
        safe_summary = _compact_handoff_text(item.get("safe_summary") or "")
        if not safe_summary:
            continue
        review_only_candidates.append(
            {
                "time_label": _compact_handoff_text(item.get("time_label") or "", 80),
                "candidate_key": _compact_handoff_text(item.get("candidate_key") or "", 80),
                "workflow": _compact_handoff_text(item.get("workflow") or "", 120),
                "topic": _compact_handoff_text(item.get("topic") or "", 120),
                "app": _compact_handoff_text(item.get("app") or "", 80),
                "safe_summary": safe_summary,
                "candidate_type": _compact_handoff_text(item.get("candidate_type") or "activity_fact", 80),
                "confidence": float(item.get("confidence") or 0.0),
                "why_keep": _compact_handoff_text(item.get("why_keep") or "", 180),
                "evidence_count": int(item.get("evidence_count") or 1),
                "project_tags": [
                    _compact_handoff_text(value, 80)
                    for value in (item.get("project_tags") or [])[:5]
                    if _compact_handoff_text(value, 80)
                ],
                "anchor_type": _compact_handoff_text(item.get("anchor_type") or "weak_or_no_anchor", 80),
                "anchor_strength": int(item.get("anchor_strength") or 0),
                "source_category": _compact_handoff_text(item.get("source_category") or "general_document", 80),
                "source_scope": _compact_handoff_text(item.get("source_scope") or "context_only", 80),
                "memory_write_policy": _compact_handoff_text(item.get("memory_write_policy") or "workflow_only", 80),
                "subject_scope": _compact_handoff_text(item.get("subject_scope") or "user_workflow_only", 80),
                "subject_confidence": float(item.get("subject_confidence") or 0.0),
                "subject_reasons": [
                    _compact_handoff_text(value, 80)
                    for value in (item.get("subject_reasons") or [])[:6]
                    if _compact_handoff_text(value, 80)
                ],
                "subject_risk_flags": [
                    _compact_handoff_text(value, 80)
                    for value in (item.get("subject_risk_flags") or [])[:8]
                    if _compact_handoff_text(value, 80)
                ],
                "quality_flags": [
                    _compact_handoff_text(value, 80)
                    for value in (item.get("quality_flags") or [])[:8]
                    if _compact_handoff_text(value, 80)
                ],
                "write_risk_flags": [
                    _compact_handoff_text(value, 80)
                    for value in (item.get("write_risk_flags") or [])[:8]
                    if _compact_handoff_text(value, 80)
                ],
                "write_eligibility": "review_only",
                "evidence_layer": _compact_handoff_text(item.get("evidence_layer") or "context_clue", 80),
                "confirmation_signals": [
                    _compact_handoff_text(value, 80)
                    for value in (item.get("confirmation_signals") or [])[:8]
                    if _compact_handoff_text(value, 80)
                ],
                "evidence_explanation": _compact_handoff_text(item.get("evidence_explanation") or "", 220),
                "screen_region_signal": _compact_screen_region_signal(item.get("screen_region_signal") or {}),
                "screen_region_signals": [
                    _compact_screen_region_signal(value)
                    for value in (item.get("screen_region_signals") or [])[:8]
                    if _compact_screen_region_signal(value)
                ],
                "evidence_span_minutes": int(item.get("evidence_span_minutes") or 0),
                "merged_duplicate_count": int(item.get("merged_duplicate_count") or 0),
                "user_decision": item.get("user_decision") or {},
            }
        )
    user_decision_suppressed_count = sum(1 for item in all_candidates if item.get("user_decision_suppressed"))
    effective_eligible_count = len(eligible_items)
    effective_review_only_count = len(review_only_items)
    original_ephemeral_count = int(candidate_quality.get("ephemeral_candidate_count") or 0)
    single_evidence_eligible_count = sum(1 for item in eligible_items if int(item.get("evidence_count") or 1) <= 1)
    effective_candidate_quality = {
        **candidate_quality,
        "eligible_candidate_count": effective_eligible_count,
        "review_only_candidate_count": effective_review_only_count,
        "ephemeral_candidate_count": original_ephemeral_count,
        "single_evidence_eligible_count": single_evidence_eligible_count,
        "user_decision_suppressed_count": user_decision_suppressed_count,
    }
    candidate_skip_reason = (
        "user_decision_suppressed"
        if user_decision_suppressed_count and not candidates
        else _memory_candidate_skip_reason(
            candidate_count=len(candidates),
            learnability_counts=learning_digest.get("learnability_counts") or {},
            diagnostics=diagnostics,
            candidate_quality=effective_candidate_quality,
        )
    )
    third_party_subject_count = int(candidate_quality.get("third_party_subject_count") or 0)
    ambiguous_subject_count = int(candidate_quality.get("ambiguous_subject_count") or 0)
    subject_risk_candidate_count = int(candidate_quality.get("subject_risk_candidate_count") or 0)
    reference_only_document_count = int(candidate_quality.get("reference_only_document_count") or 0)
    target_role_jd_document_count = int(candidate_quality.get("target_role_jd_document_count") or 0)
    screen_region_noise_count = int(candidate_quality.get("screen_region_noise_count") or 0)
    screen_region_conflict_count = int(candidate_quality.get("screen_region_conflict_count") or 0)
    write_guard_summary = (
        f"元数据/清洗摘要级诊断：单证据可写 {single_evidence_eligible_count} 条；"
        f"主体风险候选 {subject_risk_candidate_count} 条，其中 third_party_subject {third_party_subject_count} 条、"
        f"ambiguous_subject {ambiguous_subject_count} 条；JD/参考类文档 {target_role_jd_document_count + reference_only_document_count} 条；"
        f"区域噪声 {screen_region_noise_count} 条、区域冲突 {screen_region_conflict_count} 条、"
        f"confirmed_action {int(effective_candidate_quality.get('confirmed_action_count') or 0)} 条、"
        f"context_clue {int(effective_candidate_quality.get('context_clue_count') or 0)} 条、"
        f"noise_or_reference {int(effective_candidate_quality.get('noise_or_reference_count') or 0)} 条；"
        "third_party_subject / ambiguous_subject 不进入 Hermes 写入输入；reference_only_job_document、文件夹/侧边栏/任务列表/标签栏/只读预览也不进入 Hermes 写入输入。"
    )
    rejected_observations = [
        {
            "reason": "noise_or_reference",
            "count": int(effective_candidate_quality.get("noise_or_reference_count") or 0),
            "message": "文件/文件夹、侧边栏、任务列表、浏览器标签、媒体或系统 UI 只作为本地质量诊断。",
        },
        {
            "reason": "finder_or_system_reference",
            "count": int(effective_candidate_quality.get("finder_or_system_reference_count") or 0),
            "message": "Finder/system UI/只读预览不能单独证明 Local Owner 正在处理该主题。",
        },
        {
            "reason": "task_list_reference",
            "count": int(effective_candidate_quality.get("task_list_reference_count") or 0),
            "message": "任务列表旧项只说明任务存在，不能当作当前行动。",
        },
        {
            "reason": "single_evidence_demoted",
            "count": int(effective_candidate_quality.get("single_evidence_demoted_count") or 0),
            "message": "单证据默认降级，除非后续出现明确二次确认。",
        },
    ]
    rejected_observations = [item for item in rejected_observations if item["count"] > 0]
    return {
        "package_id": package["package_id"],
        "source_status": package["source_status"],
        "time_range": package["time_range"],
        "raw_access": package["raw_access"],
        "quarantine_status": package["quarantine_status"],
        "source": package["source"],
        "memory_write_policy": package["memory_write_policy"],
        "coverage_rate": package["coverage_rate"],
        "gap_summary": package["gap_summary"],
        "activity_count": len(package.get("activities") or []),
        "safe_timeline_event_count": len(package.get("safe_timeline_events") or []),
        "memory_candidates_only": True,
        "memory_candidate_count": len(candidates),
        "memory_candidate_total_count": int(candidate_quality.get("total_candidate_count") or len(all_candidates)),
        "total_candidate_count": int(candidate_quality.get("total_candidate_count") or len(all_candidates)),
        "exposed_candidate_count": int(candidate_quality.get("exposed_candidate_count") or len(all_candidates)),
        "display_candidate_count": len(candidates),
        "eligible_candidate_count": effective_eligible_count,
        "eligible_pool_count": effective_eligible_count,
        "write_input_count": len(candidates),
        "writable_candidate_count": len(candidates),
        "single_evidence_candidate_count": int(candidate_quality.get("single_evidence_candidate_count") or 0),
        "single_evidence_eligible_count": single_evidence_eligible_count,
        "third_party_subject_count": third_party_subject_count,
        "ambiguous_subject_count": ambiguous_subject_count,
        "subject_risk_candidate_count": subject_risk_candidate_count,
        "target_role_jd_document_count": target_role_jd_document_count,
        "reference_only_document_count": reference_only_document_count,
        "write_guard_summary": write_guard_summary,
        "review_only_candidate_count": effective_review_only_count,
        "ephemeral_candidate_count": original_ephemeral_count,
        "user_decision_suppressed_count": user_decision_suppressed_count,
        "memory_candidate_skip_reason": candidate_skip_reason,
        "candidate_quality": effective_candidate_quality,
        "screen_region_summary": learning_digest.get("screen_region_summary") or {},
        "learning_summary": _compact_handoff_text(learning_digest.get("summary") or "", 320),
        "learnability_counts": learning_digest.get("learnability_counts") or {},
        "workflow_summary": [
            {
                "label": _compact_handoff_text(item.get("label") or "", 120),
                "event_count": int(item.get("event_count") or 0),
                "useful_count": int(item.get("useful_count") or 0),
                "low_signal_count": int(item.get("low_signal_count") or 0),
            }
            for item in _take(learning_digest.get("workflow_summary") or [], min(limit, 12))
        ],
        "memory_candidates": candidates,
        "confirmed_actions": candidates,
        "review_only_candidates": review_only_candidates,
        "context_clues": review_only_candidates,
        "rejected_observations": rejected_observations,
        "learnability_diagnostics": {
            "summary": _compact_handoff_text(diagnostics.get("summary") or "", 320),
            "learnability_counts": diagnostics.get("learnability_counts") or {},
            "quality": diagnostics.get("quality") or {},
            "coverage": diagnostics.get("coverage") or {},
        },
    }


def _apply_user_decisions_to_memory_candidates(conn: sqlite3.Connection, package_id: str, items: list[dict]) -> list[dict]:
    adjusted: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        candidate = dict(item)
        candidate_key = item_key_for_memory_candidate(package_id, candidate)
        candidate["candidate_key"] = candidate_key
        candidate["item_key"] = candidate_key
        decision = find_user_decision(conn, candidate, source_kind="memory_candidate", item_type="memory_candidate")
        if decision and decision.get("action") in {CONTEXT_ONLY, NEVER_WRITE}:
            original = str(candidate.get("write_eligibility") or "ephemeral")
            if original == "eligible":
                candidate["write_eligibility"] = "review_only"
            flags = list(candidate.get("quality_flags") or [])
            marker = "user_never_write_rule" if decision.get("action") == NEVER_WRITE else "user_context_only_rule"
            if marker not in flags:
                flags.append(marker)
            candidate["quality_flags"] = flags[:8]
            candidate["user_decision"] = public_decision(decision)
            candidate["user_decision_suppressed"] = original == "eligible"
            candidate["why_keep"] = _compact_handoff_text(
                "按你的规则不写入长期记忆；保留为安全复核线索。" if decision.get("action") == NEVER_WRITE else "按你的规则仅作本次/同类参考；不进入长期记忆写入输入。",
                180,
            )
        adjusted.append(candidate)
    return adjusted


def _memory_candidate_skip_reason(*, candidate_count: int, learnability_counts: dict, diagnostics: dict, candidate_quality: dict | None = None) -> str:
    if candidate_count:
        return ""
    quality_summary = candidate_quality or {}
    review = int(quality_summary.get("review_only_candidate_count") or 0)
    ephemeral = int(quality_summary.get("ephemeral_candidate_count") or 0)
    total = int(quality_summary.get("total_candidate_count") or 0)
    subject_risk = int(quality_summary.get("subject_risk_candidate_count") or 0)
    third_party = int(quality_summary.get("third_party_subject_count") or 0)
    ambiguous = int(quality_summary.get("ambiguous_subject_count") or 0)
    reference_only = int(quality_summary.get("reference_only_document_count") or 0)
    target_role_jd = int(quality_summary.get("target_role_jd_document_count") or 0)
    third_party_resume = int(quality_summary.get("third_party_resume_document_count") or 0)
    ambiguous_document = int(quality_summary.get("ambiguous_document_count") or 0)
    if total and (reference_only + target_role_jd + third_party_resume + ambiguous_document) >= total:
        return "third_party_or_ambiguous_subject" if (third_party_resume or ambiguous_document) else "reference_only_job_document"
    if total and subject_risk >= total and (third_party or ambiguous):
        return "third_party_or_ambiguous_subject"
    if total and subject_risk:
        return "subject_ownership_unclear"
    if total and (review or ephemeral):
        return "no_write_eligible_memory_candidates"
    useful = int((learnability_counts or {}).get("useful") or 0)
    quality = (diagnostics or {}).get("quality") or {}
    if useful <= 0:
        return "no_useful_learning_items"
    if int(quality.get("short_or_empty_content_count") or 0) >= useful:
        return "low_signal_or_short_content"
    return "no_long_term_memory_candidates"


def list_packages(conn: sqlite3.Connection) -> list[dict]:
    _ensure_ha_authorization_lock(conn)
    with document_authorization_lock(conn, exclusive=False):
        packages = _list_packages_locked(conn)
    for item in packages:
        reason = _implicit_revocation_reason(item)
        if not reason:
            continue
        finalized = _finalize_implicit_package_revocation(
            conn,
            str(item["package_id"]),
            reason,
        )
        item["source_status"] = finalized.get("source_status", "revoked")
        item["reason"] = finalized.get("reason", reason)
        item["cache_available"] = False
        if "cache_purge_pending" in finalized:
            item["cache_purge_pending"] = bool(finalized["cache_purge_pending"])
    return packages


def _list_packages_locked(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT d.id AS package_id, d.date_range, d.created_at, d.cache_ref,
               h.source_status, h.coverage_rate, h.retention_until
        FROM distribution_packages d
        JOIN ha_source_records h ON h.package_id = d.id
        WHERE d.target = 'ha'
        ORDER BY d.created_at DESC
        """
    ).fetchall()
    output = []
    for row in rows:
        item = dict(row)
        item["date_range"] = json.loads(item["date_range"])
        cache_ref = item.pop("cache_ref", "")
        cache_available = False
        cache_reason = ""
        if item["source_status"] == "active" and cache_ref:
            try:
                cache_path = _safe_private_cache_path(
                    Path(cache_ref),
                )
                cache_metadata = cache_path.lstat()
                cache_available = bool(
                    stat.S_ISREG(cache_metadata.st_mode)
                    and cache_metadata.st_uid == os.getuid()
                )
                if not cache_available:
                    cache_reason = "cache_unreadable"
            except FileNotFoundError:
                cache_reason = "cache_missing"
            except OSError:
                cache_reason = "cache_unreadable"
        if item["source_status"] == "active" and not cache_available:
            item["source_status"] = "revoked"
            item["reason"] = cache_reason or "cache_missing"
            item["cache_available"] = False
        elif item["source_status"] == "active":
            item["cache_available"] = True
        output.append(item)
    return output


def _take(items, limit: int):
    if not isinstance(items, list):
        return []
    return items[:limit]


def _compact_handoff_text(value: str, max_chars: int = 360) -> str:
    text = _sanitize_package_text(str(value or ""))
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)].rstrip() + "…"


def _compact_screen_region_signal(value: dict) -> dict:
    if not isinstance(value, dict):
        return {}
    region_kind = _compact_handoff_text(value.get("region_kind") or "", 80)
    actionability = _compact_handoff_text(value.get("actionability") or "", 80)
    if not region_kind and not actionability:
        return {}
    source = _compact_handoff_text(value.get("source") or "", 80)
    try:
        confidence = float(value.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "region_kind": region_kind,
        "actionability": actionability,
        "source": source,
        "confidence": round(max(0.0, min(1.0, confidence)), 2),
        "reason_codes": [
            _compact_handoff_text(reason, 80)
            for reason in (value.get("reason_codes") or [])[:8]
            if _compact_handoff_text(reason, 80)
        ],
    }


def revoke_packages_for_range(
    conn: sqlite3.Connection,
    start: str,
    end: str,
    reason: str,
    source_status: str = "user_deleted",
    *,
    return_details: bool = False,
    defer_cache_delete: bool = False,
) -> list[str] | dict:
    source_status = _normalize_source_status(source_status)
    rows = conn.execute(
        """
        SELECT d.id, d.date_range, d.cache_ref, h.source_status
        FROM distribution_packages d
        JOIN ha_source_records h ON h.package_id = d.id
        WHERE d.target = 'ha' AND h.source_status = 'active'
        """
    ).fetchall()
    revoked: list[str] = []
    cache_files_deleted = 0
    package_ids_pending: list[str] = []
    now = utc_now()
    deletion_group_ids = _deletion_group_ids(conn, start, end)
    for row in rows:
        date_range = json.loads(row["date_range"])
        if not _overlaps(start, end, date_range["from"], date_range["to"]):
            continue
        package_id = row["id"]
        revoked.append(package_id)
        conn.execute(
            """
            UPDATE ha_source_records
            SET source_status = ?, updated_at = ?
            WHERE package_id = ?
            """,
            (source_status, now, package_id),
        )
        conn.execute(
            """
            UPDATE distribution_packages
            SET cache_invalidated_at = ?
            WHERE id = ?
            """,
            (now, package_id),
        )
        cache_ref = row["cache_ref"]
        if cache_ref:
            schedule_private_purge(conn, "ha", str(package_id))
            package_ids_pending.append(str(package_id))
        conn.execute(
            """
            INSERT INTO source_revocation_events(
              id, package_id, source_status, time_range, reason, deleted_at, deletion_group_ids
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"revocation_{uuid.uuid4().hex}",
                package_id,
                source_status,
                row["date_range"],
                reason,
                now,
                json.dumps(deletion_group_ids, ensure_ascii=False),
            ),
        )
    if return_details:
        return {
            "package_ids": revoked,
            "records_updated": len(revoked),
            "cache_files_deleted": cache_files_deleted,
            "package_ids_pending": package_ids_pending,
            "source_status": source_status,
        }
    return revoked


def revoke_package(conn: sqlite3.Connection, package_id: str, reason: str = "manual_revoked") -> dict:
    # Revocation deletes the cache as well as changing the database status.
    # Serialize both effects against every public package reader.
    _require_clean_private_mutation(conn, "revoke_package")
    with document_authorization_lock(conn, exclusive=True):
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = _revoke_package_locked(conn, package_id, reason=reason)
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        purge_ids = list(result.pop("_purge_package_ids", []))
        if package_id in pending_private_purge_ids(conn, "ha"):
            purge_ids.append(package_id)
        purge = _purge_package_cache_ids_locked(conn, purge_ids)
        result.update(
            {
                "cache_files_deleted": purge["files_deleted"],
                "cache_purge_failed": purge["failed_refs"],
                "cache_purge_pending": purge["pending_refs"],
                "status": "partial" if purge["pending_refs"] else "completed",
            }
        )
        return result


def _revoke_package_locked(conn: sqlite3.Connection, package_id: str, reason: str = "manual_revoked") -> dict:
    row = conn.execute(
        """
        SELECT d.id, d.date_range, d.cache_ref, h.source_status
        FROM distribution_packages d
        JOIN ha_source_records h ON h.package_id = d.id
        WHERE d.id = ?
        """,
        (package_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"Unknown HA Source Package: {package_id}")
    if row["source_status"] != "active":
        return {
            "package_id": package_id,
            "source_status": row["source_status"],
            "revoked": False,
            "_purge_package_ids": [],
        }

    now = utc_now()
    conn.execute(
        "UPDATE ha_source_records SET source_status = ?, updated_at = ? WHERE package_id = ?",
        ("revoked", now, package_id),
    )
    conn.execute(
        "UPDATE distribution_packages SET cache_invalidated_at = ? WHERE id = ?",
        (now, package_id),
    )
    conn.execute(
        """
        INSERT INTO source_revocation_events(
          id, package_id, source_status, time_range, reason, deleted_at, deletion_group_ids
        ) VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"revocation_{uuid.uuid4().hex}",
            package_id,
            "revoked",
            row["date_range"],
            reason,
            now,
            "[]",
        ),
    )
    conn.execute(
        """
        INSERT INTO audit_logs(id, at, action, target_type, target_id, detail)
        VALUES(?, ?, ?, ?, ?, ?)
        """,
        (
            f"audit_{uuid.uuid4().hex}",
            now,
            "revoke_ha_source_package",
            "distribution_package",
            package_id,
            json.dumps({"reason": reason, "source_status": "revoked"}, ensure_ascii=False),
        ),
    )
    purge_ids: list[str] = []
    if row["cache_ref"]:
        schedule_private_purge(conn, "ha", package_id)
        purge_ids.append(package_id)
    return {
        "package_id": package_id,
        "source_status": "revoked",
        "revoked": True,
        "_purge_package_ids": purge_ids,
    }


def expire_packages(
    conn: sqlite3.Connection,
    *,
    return_details: bool = False,
) -> list[str] | dict:
    # Expiry is an automatic revocation and must observe the same lease
    # boundary as an explicit revoke/delete.
    _require_clean_private_mutation(conn, "expire_packages")
    with document_authorization_lock(conn, exclusive=True):
        try:
            conn.execute("BEGIN IMMEDIATE")
            expired = _expire_packages_locked(conn)
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        purge = _purge_package_cache_ids_locked(conn, expired)
        details = {
            "package_ids": expired,
            "expired": len(expired),
            "cache_files_deleted": purge["files_deleted"],
            "cache_purge_failed": purge["failed_refs"],
            "cache_purge_pending": purge["pending_refs"],
            "status": "partial" if purge["pending_refs"] else "completed",
        }
        return details if return_details else expired


def _expire_packages_locked(conn: sqlite3.Connection) -> list[str]:
    now = utc_now()
    rows = conn.execute(
        """
        SELECT h.package_id, h.time_range, d.cache_ref
        FROM ha_source_records h
        JOIN distribution_packages d ON d.id = h.package_id
        WHERE h.source_status = 'active' AND h.retention_until < ?
        """,
        (now,),
    ).fetchall()
    expired: list[str] = []
    for row in rows:
        package_id = row["package_id"]
        expired.append(package_id)
        conn.execute(
            "UPDATE ha_source_records SET source_status = ?, updated_at = ? WHERE package_id = ?",
            ("expired_deleted", now, package_id),
        )
        conn.execute(
            "UPDATE distribution_packages SET cache_invalidated_at = ? WHERE id = ?",
            (now, package_id),
        )
        cache_ref = row["cache_ref"]
        if cache_ref:
            schedule_private_purge(conn, "ha", str(package_id))
        conn.execute(
            """
            INSERT INTO source_revocation_events(
              id, package_id, source_status, time_range, reason, deleted_at, deletion_group_ids
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"revocation_{uuid.uuid4().hex}",
                package_id,
                "expired_deleted",
                row["time_range"],
                "retention_expired",
                now,
                "[]",
            ),
        )
    return expired


def render_markdown(package: dict) -> str:
    lines = [
        f"# HA Source Package {package['package_id']}",
        "",
        f"- source_device: `{package['source_device']}`",
        f"- source_type: `{package['source_type']}`",
        f"- time_range: `{package['time_range']['from']}` to `{package['time_range']['to']}`",
        f"- source_status: `{package['source_status']}`",
        f"- raw_access: `{str(package['raw_access']).lower()}`",
        f"- quarantine_status: `{package['quarantine_status']}`",
        f"- coverage_rate: `{package['coverage_rate']}`",
        f"- safe_timeline_events: `{len(package.get('safe_timeline_events', []))}`",
        f"- clear_activities: `{len(package.get('activities', []))}`",
        f"- learning_useful: `{package.get('learning_digest', {}).get('learnability_counts', {}).get('useful', 0)}`",
        f"- low_signal: `{package.get('learnability_diagnostics', {}).get('learnability_counts', {}).get('low_signal', 0)}`",
        f"- protected: `{package.get('learnability_diagnostics', {}).get('learnability_counts', {}).get('protected', 0)}`",
        f"- gap: `{package.get('learnability_diagnostics', {}).get('learnability_counts', {}).get('gap', 0)}`",
        "",
        "## Learnability Diagnostics",
        "",
        package.get("learnability_diagnostics", {}).get("summary") or "No learnability diagnostics available.",
        "",
        "## Learning Digest",
        "",
        package.get("learning_digest", {}).get("summary") or "No learning digest available.",
        "",
        "### Workflows",
        "",
    ]
    for item in package.get("learning_digest", {}).get("workflow_summary", []):
        lines.append(
            f"- {item.get('label')}: {item.get('event_count', 0)} events, useful {item.get('useful_count', 0)}, low_signal {item.get('low_signal_count', 0)}"
        )
    if not package.get("learning_digest", {}).get("workflow_summary"):
        lines.append("- none")
    lines.extend(
        [
            "",
            "### Memory Candidates",
            "",
        ]
    )
    for item in package.get("learning_digest", {}).get("memory_candidates", []):
        lines.append(f"- {item.get('time_label')} / {item.get('topic')}: {item.get('safe_summary')}")
    if not package.get("learning_digest", {}).get("memory_candidates"):
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Safe Timeline",
            "",
        ]
    )
    for item in package.get("safe_timeline_events", []):
        title = item["kind"].replace("_", " ").title()
        lines.extend([f"### {title} - {item['time_range']['from']}", ""])
        if item["kind"] == "activity":
            lines.extend(
                [
                    item["summary"],
                    "",
                    f"- apps: `{', '.join(item['apps'])}`",
                    f"- domains: `{', '.join(item['domains'])}`",
                    f"- evidence_refs: `{', '.join(item['evidence_refs'])}`",
                    "",
                ]
            )
        elif item["kind"] == "duplicate":
            lines.extend(
                [
                    "Content duplicated from the previous same-display frame; not retained again.",
                    "",
                    f"- display: `{item.get('display_id') or 'none'}`",
                    f"- reason: `{item.get('reason') or 'duplicate'}`",
                    f"- duplicate_count: `{item.get('duplicate_count') or 1}`",
                    "",
                ]
            )
        elif item["kind"] == "protected":
            lines.extend(
                [
                    "Content protected by Privacy Firewall.",
                    "",
                    f"- app: `{item['app'] or 'none'}`",
                    f"- domain: `{item['domain'] or 'none'}`",
                    f"- reason: `{item['reason']}`",
                    "",
                ]
            )
        elif item["kind"] == "unverified_clue":
            lines.extend(
                [
                    "Unverified local observation retained without content.",
                    "",
                    f"- verification_state: `{item.get('verification_state') or 'unverified'}`",
                    f"- reason: `{item.get('reason_code') or 'unverified_observation'}`",
                    f"- app_class: `{item.get('app_class') or 'unknown'}`",
                    "",
                ]
            )
        else:
            lines.extend(
                [
                    "No content retained for this interval.",
                    "",
                    f"- reason: `{item['reason']}`",
                    f"- displays: `{', '.join(item['display_ids'])}`",
                    "",
                ]
            )
    if not package.get("safe_timeline_events"):
        lines.append("No safe timeline events in this range.")
    return "\n".join(lines)


def validate_package(package: dict) -> None:
    missing = sorted(field for field in REQUIRED_PACKAGE_FIELDS if field not in package)
    if missing:
        raise ValueError(f"HA Source Package missing required fields: {', '.join(missing)}")
    for field, expected in EXPECTED_PACKAGE_VALUES.items():
        if package.get(field) != expected:
            raise ValueError(f"HA Source Package has invalid {field}")
    encoded = json.dumps(package, ensure_ascii=False)
    if FORBIDDEN_VALUE_RE.search(encoded):
        raise ValueError("HA Source Package contains a forbidden raw or secret-looking value")
    if adult_text_hit(encoded).matched:
        raise ValueError("HA Source Package contains adult-content policy residue")
    _walk_keys(package)
    if package.get("raw_access") is not False:
        raise ValueError("HA Source Package must set raw_access=false")
    if package.get("quarantine_status") != "clear_only":
        raise ValueError("HA Source Package must be clear_only")


def _sanitize_package_text(value: str) -> str:
    text = str(value)
    text = SECRET_VALUE_RE.sub("[secret_redacted]", text)
    text = LOCAL_PATH_RE.sub("[path_redacted]", text)
    return redact_adult_terms(text)


def _sanitize_package_value(value):
    if isinstance(value, dict):
        return {str(key): _sanitize_package_value(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_sanitize_package_value(child) for child in value]
    if isinstance(value, str):
        return _sanitize_package_text(value)
    return value


def _learnability_diagnostics(export: dict, coverage: dict, gap_summary: dict) -> dict:
    digest = export.get("trusted_learning_digest") or {}
    quality = export.get("quality_diagnostics") or {}
    learnability_counts = digest.get("learnability_counts") or {}
    diagnostics = {
        "coverage": {
            "coverage_scope": "authorized_mac_displays_only",
            "coverage_rate": coverage.get("coverage_rate", 0.0),
            "display_count": len(coverage.get("display_ids") or []),
        },
        "learnability_counts": {
            "useful": int(learnability_counts.get("useful") or 0),
            "low_signal": int(learnability_counts.get("low_signal") or 0),
            "duplicate": int(learnability_counts.get("duplicate") or 0),
            "gap": int(learnability_counts.get("gap") or 0),
            "protected": int(learnability_counts.get("protected") or 0),
        },
        "quality": {
            "activity_count": int(quality.get("activity_count") or 0),
            "partial_redacted_count": int(quality.get("partial_redacted_count") or 0),
            "communication_redaction_count": int(quality.get("communication_redaction_count") or 0),
            "duplicate_event_count": int(quality.get("duplicate_event_count") or 0),
            "duplicate_frame_count": int(quality.get("duplicate_frame_count") or 0),
            "privacy_event_count": int(quality.get("privacy_event_count") or 0),
            "gap_event_count": int(quality.get("gap_event_count") or 0),
            "short_or_empty_content_count": int(quality.get("short_or_empty_content_count") or 0),
            "near_cleaned_content_cap_count": int(quality.get("near_cleaned_content_cap_count") or 0),
        },
        "gap_summary": {
            "privacy_events": int(gap_summary.get("privacy_events") or 0),
            "gap_events": int(gap_summary.get("gap_events") or 0),
            "duplicate_events": int(gap_summary.get("duplicate_events") or 0),
            "blocked_count": int(gap_summary.get("blocked_count") or 0),
            "review_required_count": int(gap_summary.get("review_required_count") or 0),
            "duplicate_exact_count": int(gap_summary.get("duplicate_exact_count") or 0),
            "partial_duplicate_count": int(gap_summary.get("partial_duplicate_count") or 0),
        },
    }
    diagnostics["summary"] = (
        f"可学习 {diagnostics['learnability_counts']['useful']} 条，"
        f"低信号 {diagnostics['learnability_counts']['low_signal']} 条，"
        f"重复 {diagnostics['learnability_counts']['duplicate']} 条，"
        f"缺口 {diagnostics['learnability_counts']['gap']} 条，"
        f"隐私保护 {diagnostics['learnability_counts']['protected']} 条；"
        f"coverage_rate={diagnostics['coverage']['coverage_rate']}。"
    )
    return _sanitize_package_value(diagnostics)


def _safe_timeline_events(items: list[dict]) -> list[dict]:
    output = []
    for item in items:
        kind = item.get("kind")
        if kind == "activity":
            output.append(
                {
                    "kind": "activity",
                    "event_id": item["id"],
                    "time_range": {"from": item["started_at"], "to": item["ended_at"]},
                    "summary": _sanitize_package_text(item["activity_summary"]),
                    "cleaned_content": _sanitize_package_text(item.get("cleaned_text") or item["activity_summary"]),
                    "apps": [_sanitize_package_text(value) for value in item.get("apps", [])],
                    "domains": [_sanitize_package_text(value) for value in item.get("domains", [])],
                    "evidence_refs": [_sanitize_package_text(value) for value in item.get("evidence_refs", [])],
                    "document_contexts": _safe_document_contexts(item.get("document_contexts") or []),
                    "privacy_level": item.get("privacy_level"),
                    "redaction_confidence": item.get("redaction_confidence"),
                    "dedupe_status": _sanitize_package_text(item.get("dedupe_status") or "unique"),
                    "dedupe_note": _sanitize_package_text(item.get("dedupe_note") or ""),
                }
            )
        elif kind == "unverified_clue":
            output.append(
                {
                    "kind": "unverified_clue",
                    "contract_version": HA_UNVERIFIED_CLUE_PROJECTION_VERSION,
                    "source_type": "content_free_unverified_clue",
                    "source_ref": _sanitize_package_text(item.get("source_ref") or ""),
                    "time_range": {
                        "from": _sanitize_package_text(item.get("started_at") or item.get("observed_at") or ""),
                        "to": _sanitize_package_text(item.get("ended_at") or item.get("observed_at") or ""),
                    },
                    "verification_state": "unverified",
                    "reason": _sanitize_package_text(item.get("reason_code") or "screen_observation_unverified"),
                    "reason_code": _sanitize_package_text(item.get("reason_code") or "screen_observation_unverified"),
                    "display_ids": [],
                    "privacy_level": _sanitize_package_text(item.get("privacy_level") or "normal"),
                    "dedupe_status": _sanitize_package_text(item.get("dedupe_status") or "unique"),
                    "app_class": _sanitize_package_text(item.get("app_class") or "unknown"),
                    "trusted": False,
                    "consumed": False,
                    "delivered": False,
                    "cleaned_text_included": False,
                    "raw_access": False,
                }
            )
        elif kind == "privacy_event":
            output.append(
                {
                    "kind": "protected",
                    "event_id": item["id"],
                    "time_range": {"from": item["started_at"], "to": item["ended_at"]},
                    "reason": _sanitize_package_text(item.get("reason") or item.get("type") or "privacy_firewall_blocked"),
                    "app": _sanitize_package_text(item.get("app_name") or ""),
                    "domain": _sanitize_package_text(item.get("domain") or ""),
                    "action": _sanitize_package_text(item.get("action") or "blocked"),
                }
            )
        elif kind == "gap_event":
            output.append(
                {
                    "kind": "gap",
                    "event_id": item["id"],
                    "time_range": {"from": item["started_at"], "to": item["ended_at"]},
                    "reason": _sanitize_package_text(item.get("reason") or item.get("detail") or "not_retained"),
                    "display_ids": [_sanitize_package_text(value) for value in item.get("display_ids", [])],
                }
            )
        elif kind == "duplicate_event":
            output.append(
                {
                    "kind": "duplicate",
                    "event_id": item["id"],
                    "time_range": {"from": item["started_at"], "to": item["ended_at"]},
                    "display_id": _sanitize_package_text(item.get("display_id") or ""),
                    "reason": _sanitize_package_text(item.get("reason") or "duplicate"),
                    "duplicate_count": int(item.get("duplicate_count") or 1),
                    "note": _sanitize_package_text(item.get("note") or "content duplicated; not retained again"),
                }
            )
    return output


def _write_private_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


def _safe_document_contexts(items: list[dict]) -> list[dict]:
    contexts = []
    for item in items:
        if not isinstance(item, dict):
            continue
        contexts.append(
            {
                "document_ref": _sanitize_package_text(item.get("document_ref") or ""),
                "file_name": _sanitize_package_text(item.get("file_name") or ""),
                "file_type": _sanitize_package_text(item.get("file_type") or ""),
                "file_modified_at": _sanitize_package_text(item.get("file_modified_at") or ""),
                "subsequent_mtime_event": _sanitize_package_text(item.get("subsequent_mtime_event") or ""),
                "visible_location_hint": _sanitize_package_text(item.get("visible_location_hint") or ""),
                "topic_tags": [_sanitize_package_text(value) for value in item.get("topic_tags") or []],
                "change_summary": _sanitize_package_text(item.get("change_summary") or ""),
                "document_source_category": _sanitize_package_text(item.get("document_source_category") or ""),
                "document_source_scope": _sanitize_package_text(item.get("document_source_scope") or ""),
                "document_subject_scope": _sanitize_package_text(item.get("document_subject_scope") or ""),
                "document_memory_policy": _sanitize_package_text(item.get("document_memory_policy") or ""),
                "document_classification_confidence": item.get("document_classification_confidence") or 0.0,
                "document_classification_reasons": [
                    _sanitize_package_text(value)
                    for value in (item.get("document_classification_reasons") or [])
                ],
                "document_risk_flags": [
                    _sanitize_package_text(value)
                    for value in (item.get("document_risk_flags") or [])
                ],
                "document_parse_status": _sanitize_package_text(item.get("document_parse_status") or ""),
                "document_parse_method": _sanitize_package_text(item.get("document_parse_method") or ""),
                "redaction_confidence": item.get("redaction_confidence") or 0.0,
                "redaction_status": _sanitize_package_text(item.get("redaction_status") or "redacted"),
            }
        )
    return contexts


def _normalize_source_status(source_status: str) -> str:
    return source_status if source_status in SOURCE_STATUSES else "revoked"


def _delete_package_cache(
    cache_ref: str | None,
    package_id: str | None = None,
) -> int:
    if not cache_ref:
        return 0
    removed = 0
    raw_cache_path = Path(cache_ref)
    cache_path = _safe_private_cache_path(raw_cache_path)
    expected_id = str(package_id or cache_path.stem)
    if (
        HA_PACKAGE_ID_RE.fullmatch(expected_id) is None
        or cache_path.name != f"{expected_id}.json"
    ):
        raise OSError("Unsafe HA cache identity")
    try:
        removed += _unlink_private_cache_file(cache_path)
        markdown_path = cache_path.with_suffix(".md")
        removed += _unlink_private_cache_file(markdown_path)
    except OSError as exc:
        exc.files_deleted = removed
        raise
    return removed


def _unlink_private_cache_file(path: Path) -> int:
    absolute = path.absolute()
    try:
        metadata = absolute.lstat()
    except FileNotFoundError:
        return 0
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError("Refusing to purge a non-regular HA cache")
    if metadata.st_uid != os.getuid():
        raise OSError("Refusing to purge an unowned HA cache")
    try:
        absolute.unlink()
    except FileNotFoundError:
        return 0
    return 1


def _safe_private_cache_path(path: Path) -> Path:
    lexical = path if path.is_absolute() else Path.cwd() / path
    try:
        parent_metadata = lexical.parent.lstat()
    except FileNotFoundError:
        return lexical.parent.resolve(strict=False) / lexical.name
    if (
        not stat.S_ISDIR(parent_metadata.st_mode)
        or stat.S_ISLNK(parent_metadata.st_mode)
        or parent_metadata.st_uid != os.getuid()
        or parent_metadata.st_mode & 0o777 != 0o700
    ):
        raise OSError("HA cache parent is not private")
    resolved_parent = lexical.parent.resolve(strict=False)
    return resolved_parent / lexical.name


def purge_package_cache_refs(cache_refs: list[str]) -> dict[str, int]:
    """Delete legacy explicit refs while reporting every failure truthfully."""
    files_deleted = 0
    failed_refs = 0
    for cache_ref in sorted({str(value or "") for value in cache_refs if value}):
        try:
            files_deleted += _delete_package_cache(cache_ref)
        except OSError as exc:
            files_deleted += int(getattr(exc, "files_deleted", 0) or 0)
            failed_refs += 1
    return {
        "files_deleted": files_deleted,
        "failed_refs": failed_refs,
    }


def _purge_package_cache_ids_locked(
    conn: sqlite3.Connection,
    package_ids: list[str],
) -> dict[str, int]:
    """Purge HA caches and settle durable ID markers under an exclusive lease."""
    files_deleted = 0
    failed_refs = 0
    unique_ids = sorted({str(value or "") for value in package_ids if value})
    for package_id in unique_ids:
        row = conn.execute(
            "SELECT cache_ref FROM distribution_packages WHERE id = ?",
            (package_id,),
        ).fetchone()
        cache_ref = str(row["cache_ref"] or "") if row is not None else ""
        try:
            files_deleted += _delete_package_cache(
                cache_ref,
                package_id,
            )
        except OSError as exc:
            files_deleted += int(getattr(exc, "files_deleted", 0) or 0)
            failed_refs += 1
            mark_private_purge_failed(
                conn,
                "ha",
                package_id,
                private_purge_failure_code(exc),
            )
        else:
            complete_private_purge(conn, "ha", package_id)
    if unique_ids:
        conn.commit()
    return {
        "files_deleted": files_deleted,
        "failed_refs": failed_refs,
        "pending_refs": len(pending_private_purge_ids(conn, "ha")),
    }


def retry_pending_package_cache_purges(conn: sqlite3.Connection) -> dict[str, int]:
    _require_clean_private_mutation(conn, "retry_pending_package_cache_purges")
    with document_authorization_lock(conn, exclusive=True):
        return _purge_package_cache_ids_locked(
            conn,
            pending_private_purge_ids(conn, "ha"),
        )


def _mark_package_revoked(
    conn: sqlite3.Connection,
    package_id: str,
    date_range: str,
    reason: str,
    deletion_group_ids: str,
) -> None:
    """Record a revocation in the caller-owned exclusive transaction."""
    now = utc_now()
    conn.execute(
        "UPDATE ha_source_records SET source_status = ?, updated_at = ? WHERE package_id = ?",
        ("revoked", now, package_id),
    )
    conn.execute(
        "UPDATE distribution_packages SET cache_invalidated_at = ? WHERE id = ?",
        (now, package_id),
    )
    conn.execute(
        """
        INSERT INTO source_revocation_events(
          id, package_id, source_status, time_range, reason, deleted_at, deletion_group_ids
        ) VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"revocation_{uuid.uuid4().hex}",
            package_id,
            "revoked",
            date_range,
            reason,
            now,
            deletion_group_ids,
        ),
    )


def _implicit_revocation_reason(package: dict) -> str:
    if str(package.get("source_status") or "") != "revoked":
        return ""
    reason = str(package.get("reason") or "")
    return reason if reason in {
        "cache_missing",
        "cache_unreadable",
        "package_validation_failed",
    } else ""


def _unavailable_package_projection(
    package: dict,
    package_id: str,
    *,
    projection_key: str,
) -> dict:
    output = {
        "package_id": package_id,
        "source_status": package.get("source_status", "revoked"),
        "reason": package.get("reason", ""),
        "time_range": package.get("time_range") or package.get("date_range") or {},
        "raw_access": False,
        projection_key: True,
    }
    if "cache_purge_pending" in package:
        output["cache_purge_pending"] = bool(package["cache_purge_pending"])
    return output


def _finalize_implicit_package_revocation(
    conn: sqlite3.Connection,
    package_id: str,
    reason: str,
) -> dict:
    """Revalidate a failed cache read and revoke only under an exclusive lease."""
    _require_clean_private_mutation(conn, "implicit_package_revocation")
    with document_authorization_lock(conn, exclusive=True):
        try:
            conn.execute("BEGIN IMMEDIATE")
            package = _get_package_locked(conn, package_id)
            current_reason = _implicit_revocation_reason(package)
            if not current_reason:
                conn.commit()
                if str(package.get("source_status") or "") == "active":
                    return {
                        "package_id": package_id,
                        "source_status": "unavailable",
                        "reason": "cache_state_changed",
                        "time_range": package.get("time_range") or {},
                        "raw_access": False,
                    }
                return package
            row = conn.execute(
                """
                SELECT d.id, d.date_range, d.cache_ref, h.source_status
                FROM distribution_packages d
                JOIN ha_source_records h ON h.package_id = d.id
                WHERE d.id = ?
                """,
                (package_id,),
            ).fetchone()
            if row is None or str(row["source_status"] or "") != "active":
                conn.commit()
                return package
            _mark_package_revoked(
                conn,
                package_id,
                str(row["date_range"]),
                current_reason or reason,
                "[]",
            )
            purge_ids: list[str] = []
            if row["cache_ref"]:
                schedule_private_purge(conn, "ha", package_id)
                purge_ids.append(package_id)
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        purge = _purge_package_cache_ids_locked(conn, purge_ids)
        return {
            "package_id": package_id,
            "source_status": "revoked",
            "reason": current_reason or reason,
            "time_range": json.loads(str(row["date_range"])),
            "raw_access": False,
            "cache_purge_pending": bool(purge["pending_refs"]),
        }


def _require_clean_private_mutation(
    conn: sqlite3.Connection,
    operation: str,
) -> None:
    if conn.in_transaction:
        raise RuntimeError(f"{operation} requires a clean SQLite connection")


def _walk_keys(value) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if FORBIDDEN_KEY_RE.search(str(key)) and key not in {
                "raw_access",
                "redaction_policy_version",
            }:
                raise ValueError(f"HA Source Package contains forbidden key: {key}")
            _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            _walk_keys(child)


def _coverage(conn: sqlite3.Connection, start: str, end: str) -> dict:
    rows = conn.execute(
        """
        SELECT display_id, AVG(coverage_ratio) AS coverage_rate
        FROM capture_coverage_ledger
        WHERE datetime(ended_at) > datetime(?)
          AND datetime(started_at) < datetime(?)
        GROUP BY display_id
        """,
        (start, end),
    ).fetchall()
    if not rows:
        return {"display_ids": [], "coverage_rate": 0.0}
    display_ids = [row["display_id"] for row in rows if row["display_id"]]
    rate = sum(float(row["coverage_rate"] or 0.0) for row in rows) / len(rows)
    return {"display_ids": display_ids, "coverage_rate": round(rate, 3)}


def _gap_summary(conn: sqlite3.Connection, start: str, end: str) -> dict:
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
    duplicate_count = conn.execute(
        """
        SELECT COALESCE(SUM(duplicate_count), 0) AS count
        FROM duplicate_events
        WHERE datetime(COALESCE(ended_at, started_at)) > datetime(?)
          AND datetime(started_at) < datetime(?)
        """,
        (start, end),
    ).fetchone()["count"]
    ledger = conn.execute(
        """
        SELECT SUM(blocked_count) AS blocked, SUM(review_required_count) AS review_required,
               SUM(gap_count) AS gaps,
               SUM(duplicate_exact_count) AS duplicate_exact,
               SUM(partial_duplicate_count) AS partial_duplicate
        FROM capture_coverage_ledger
        WHERE datetime(ended_at) > datetime(?)
          AND datetime(started_at) < datetime(?)
        """,
        (start, end),
    ).fetchone()
    return {
        "privacy_events": int(privacy_count or 0),
        "gap_events": int(gap_count or 0),
        "blocked_count": int(ledger["blocked"] or 0),
        "review_required_count": int(ledger["review_required"] or 0),
        "ledger_gap_count": int(ledger["gaps"] or 0),
        "duplicate_events": int(duplicate_count or 0),
        "duplicate_exact_count": int(ledger["duplicate_exact"] or 0),
        "partial_duplicate_count": int(ledger["partial_duplicate"] or 0),
    }


def _deletion_group_ids(conn: sqlite3.Connection, start: str, end: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT deletion_group_id
        FROM extracted_activities
        WHERE datetime(COALESCE(ended_at, started_at)) > datetime(?)
          AND datetime(started_at) < datetime(?)
          AND deletion_group_id IS NOT NULL
        """,
        (start, end),
    ).fetchall()
    return [row["deletion_group_id"] for row in rows]


def _overlaps(start_a: str, end_a: str, start_b: str, end_b: str) -> bool:
    return parse_time(end_a) > parse_time(start_b) and parse_time(start_a) < parse_time(end_b)
