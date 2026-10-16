from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .activity_sessions import refresh_activity_session
from .confirmation_evidence import purge_scheduled_evidence, schedule_evidence_release
from .document_changes import bump_document_change_epoch, document_authorization_lock
from .ha_source import _purge_package_cache_ids_locked, revoke_packages_for_range
from .pipeline import parse_time, utc_now
from .retention import file_change_retention_cutoff
from .legacy_review_cleanup import delete_retired_jobs_for_range, purge_retired_job_files


# SQLite caps bound parameters per statement. Chunk ID lists well below the
# limit so a growing database cannot turn maintenance into a hard failure.
_SQL_VARIABLE_CHUNK = 500


def preview_delete_range(
    conn: sqlite3.Connection,
    start: str,
    end: str,
    *,
    source_status: str = "user_deleted",
) -> dict[str, Any]:
    """Return the same database-row deletion counts used by ``delete_range``.

    Filesystem cache counts are deliberately excluded because a dry run cannot
    promise how many cache files still exist when the deletion is applied.
    """
    context = _deletion_context(
        conn,
        start,
        end,
        hold_unresolved=source_status == "expired_deleted",
        file_change_cutoff=(
            file_change_retention_cutoff().isoformat(timespec="seconds")
            if source_status == "expired_deleted"
            else None
        ),
    )
    cascade = _cascade_counts(context)
    if source_status == "expired_deleted":
        cascade["file_watch_state_reset"] = 0
    cascade["gap_events_created"] = len(
        _replacement_gap_ranges(context, start, end, source_status=source_status)
    )
    return {
        "activities_deleted": len(context["activity_ids"]),
        "deleted_activity_count": len(context["activity_ids"]),
        "deletion_group_ids": context["deletion_group_ids"],
        "packages_revoked": context["package_ids"],
        "cascade": cascade,
        "retention_held": context["retention_held"],
    }


def delete_range(
    conn: sqlite3.Connection,
    start: str,
    end: str,
    reason: str = "user_deleted",
    source_status: str = "user_deleted",
) -> dict[str, Any]:
    with document_authorization_lock(conn, exclusive=True):
        try:
            return _delete_range_locked(
                conn,
                start,
                end,
                reason=reason,
                source_status=source_status,
            )
        except BaseException:
            # delete_range commits on success and therefore owns its transaction
            # boundary.  No failure, including process-control exceptions, may
            # leave an earlier slice of the cascade pending for a later write to
            # commit accidentally.
            if conn.in_transaction:
                conn.rollback()
            raise


def _delete_range_locked(
    conn: sqlite3.Connection,
    start: str,
    end: str,
    reason: str = "user_deleted",
    source_status: str = "user_deleted",
) -> dict[str, Any]:
    # Serialize the deletion boundary before discovering rows.  A document
    # writer that wins this lock first is included in the deletion context; a
    # queued writer that loses sees the bumped epoch and cannot write afterward.
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    if source_status != "expired_deleted":
        bump_document_change_epoch(conn)
    else:
        # Retention preserves the authorization epoch, but still takes the
        # writer lock before selecting its deletion context.
        conn.execute(
            "UPDATE daemon_state SET updated_at=updated_at "
            "WHERE key='document_change_authorization_epoch'"
        )
    context = _deletion_context(
        conn,
        start,
        end,
        hold_unresolved=source_status == "expired_deleted",
        file_change_cutoff=(
            file_change_retention_cutoff().isoformat(timespec="seconds")
            if source_status == "expired_deleted"
            else None
        ),
    )
    if source_status != "expired_deleted":
        # A manual deletion invalidates the lifetime epoch of the shared helper
        # even when the selected range contains no persisted document row.  No
        # root may continue to advertise complete coverage while that old
        # helper is stopping and a fresh barrier has not yet completed.
        pending_at = utc_now()
        conn.execute(
            "UPDATE file_watch_roots SET baseline_status='pending',baseline_started_at=NULL,"
            "baseline_completed_at=NULL,updated_at=? WHERE enabled=1",
            (pending_at,),
        )
        conn.execute(
            "UPDATE file_watch_checkpoints SET last_event_id=NULL,coverage_status='pending',"
            "event_gap_count=0,coverage_started_at=NULL,last_event_at=NULL,updated_at=? "
            "WHERE root_id IN (SELECT root_id FROM file_watch_roots WHERE enabled=1)",
            (pending_at,),
        )
        conn.execute(
            "INSERT INTO daemon_state(key,value,updated_at) VALUES('document_change_pending_events','0',?) "
            "ON CONFLICT(key) DO UPDATE SET value='0',updated_at=excluded.updated_at",
            (pending_at,),
        )
    preview = _cascade_counts(context)
    if source_status == "expired_deleted":
        preview["file_watch_state_reset"] = 0
    gap_ranges = _replacement_gap_ranges(
        context,
        start,
        end,
        source_status=source_status,
    )

    package_cascade = revoke_packages_for_range(
        conn,
        start,
        end,
        reason,
        source_status=source_status,
        return_details=True,
        defer_cache_delete=True,
    )
    revoked = package_cascade["package_ids"]

    _delete_ids(conn, "search_index", "entity_id", context["activity_ids"])
    confirmation_item_keys = list(context["activity_confirmation_item_keys"])
    if confirmation_item_keys and source_status != "expired_deleted":
        group_keys = _values_for_ids(conn, "activity_confirmation_items", "group_key", "item_key", confirmation_item_keys)
        from .context_console import _revoke_activity_confirmation_links_locked

        _revoke_activity_confirmation_links_locked(
            conn,
            (*confirmation_item_keys, *group_keys),
            reason_code=f"time_range_{source_status}",
            now=datetime.now(timezone.utc),
        )
    evidence_scheduled = schedule_evidence_release(
        conn,
        confirmation_item_keys,
        reason=f"time_range_{source_status}",
    )
    _delete_ids(
        conn,
        "activity_confirmation_items",
        "item_key",
        confirmation_item_keys,
    )
    _delete_ids(conn, "extracted_activities", "id", context["activity_ids"])
    _delete_ids(conn, "frame_signals", "id", context["frame_signal_ids"])
    _delete_ids(conn, "document_signals", "id", context["document_signal_ids"])
    if source_status != "expired_deleted":
        _delete_ids(conn, "file_watch_state", "file_id", context["file_change_state_ids"])
    _delete_ids(conn, "file_change_versions", "id", context["file_change_version_ids"])
    _delete_ids(conn, "file_change_events", "id", context["file_change_event_ids"])
    _delete_ids(conn, "file_watch_coverage_gaps", "id", context["file_watch_coverage_gap_ids"])
    if source_status != "expired_deleted" and context["file_change_root_ids"]:
        now_for_baseline = utc_now()
        for chunk in _id_chunks(context["file_change_root_ids"]):
            placeholders = ",".join("?" for _ in chunk)
            conn.execute(
                f"UPDATE file_watch_roots SET baseline_status='pending',baseline_started_at=NULL,"
                f"baseline_completed_at=NULL,updated_at=? WHERE root_id IN ({placeholders})",
                (now_for_baseline, *chunk),
            )
            conn.execute(
                f"UPDATE file_watch_checkpoints SET last_event_id=NULL,coverage_status='pending',event_gap_count=0,"
                f"coverage_started_at=NULL,last_event_at=NULL,updated_at=? WHERE root_id IN ({placeholders})",
                (now_for_baseline, *chunk),
            )
    _delete_ids(conn, "activity_provenance", "id", context["provenance_ids"])
    _delete_ids(conn, "capture_fingerprints", "display_id", context["capture_fingerprint_ids"])
    # Only source-traceable decisions are eligible for cascade deletion.
    # Migrated legacy rows deliberately remain because their source cannot be
    # located safely from a requested time range.
    _delete_ids(conn, "user_decisions", "item_key", context["user_decision_ids"])

    _delete_range_rows(conn, "raw_frame_candidates", "captured_at", start, end)
    _delete_range_rows(conn, "display_frame_contexts", "captured_at", start, end)
    _delete_overlap_rows(conn, "raw_capture_jobs", "started_at", "COALESCE(ended_at, started_at)", start, end)
    _delete_overlap_rows(conn, "privacy_events", "started_at", "COALESCE(ended_at, started_at)", start, end)
    _delete_overlap_rows(conn, "gap_events", "started_at", "COALESCE(ended_at, started_at)", start, end)
    _delete_overlap_rows(conn, "duplicate_events", "started_at", "COALESCE(ended_at, started_at)", start, end)
    _delete_overlap_rows(conn, "capture_coverage_ledger", "started_at", "ended_at", start, end)
    _delete_overlap_rows(conn, "ha_automation_jobs", "started_at", "ended_at", start, end)
    _delete_overlap_rows(conn, "quality_advice_runs", "started_at", "ended_at", start, end)
    _delete_range_rows(conn, "session_transition_evidence", "observed_at", start, end)
    _delete_range_rows(conn, "soak_capture_cycles", "observed_at", start, end)
    _delete_range_rows(conn, "soak_resource_samples", "observed_at", start, end)
    _delete_range_rows(conn, "soak_evidence_events", "occurred_at", start, end)
    _delete_soak_daily_snapshots(conn, start, end)

    feedback_count = _delete_quality_feedback(
        conn,
        start,
        end,
        deletion_group_ids=context["deletion_group_ids"],
        target_ids=context["activity_ids"],
    )
    conn.execute(
        """
        UPDATE segments
        SET summary = 'deleted_or_partially_deleted', top_apps = '[]', top_domains = '[]'
        WHERE datetime(started_at) < datetime(?) AND datetime(ended_at) > datetime(?)
          AND (
            COALESCE(summary, '') <> 'deleted_or_partially_deleted'
            OR COALESCE(top_apps, '') <> '[]'
            OR COALESCE(top_domains, '') <> '[]'
          )
        """,
        (end, start),
    )
    vlm_deleted = delete_retired_jobs_for_range(
        conn,
        start,
        end,
        defer_file_delete=True,
    )

    for gap_start, gap_end in gap_ranges:
        _insert_replacement_gap(
            conn,
            gap_start,
            gap_end,
            reason=reason,
            source_status=source_status,
            deleted_activities=len(context["activity_ids"]),
        )

    rebuilt_sample_count = 0
    rebuilt_rollup_count = 0
    for session in context["session_rows"]:
        effective_end = str(
            session.get("ended_at")
            or session.get("last_observed_at")
            or session["started_at"]
        )
        refresh_activity_session(
            conn,
            str(session["id"]),
            active_end=effective_end,
        )
        rebuilt_sample_count += _count_for_ids(
            conn,
            "system_activity_samples",
            "session_id",
            [str(session["id"])],
        )
        rebuilt_rollup_count += _count_for_ids(
            conn,
            "system_activity_session_rollups",
            "session_id",
            [str(session["id"])],
        )

    context_cascade: dict[str, Any] = {"observations": 0}
    if source_status != "expired_deleted":
        # Explicit user deletion must revoke the additive schema-30 context
        # graph in the same transaction. Activity retention is deliberately
        # excluded because VerifiedContext and product wins have independent
        # 90/30-day policies.
        from .context_console import _revoke_context_observations_for_range_locked

        context_cascade = _revoke_context_observations_for_range_locked(
            conn,
            start,
            end,
            reason_code="legacy_manual_range_delete",
        )

    now = utc_now()
    conn.execute(
        """
        INSERT INTO audit_logs(id, at, action, target_type, target_id, deletion_group_id, detail)
        VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"audit_{uuid.uuid4().hex}",
            now,
            "delete_time_range",
            "time_range",
            f"{start}..{end}",
            ",".join(context["deletion_group_ids"]),
            json.dumps(
                {
                    "reason": reason,
                    "source_status": source_status,
                    "activities_deleted": len(context["activity_ids"]),
                    "packages_revoked": len(revoked),
                    "confirmation_items_deleted": len(context["activity_confirmation_item_keys"]),
                    "confirmation_evidence_scheduled": evidence_scheduled,
                    "user_decisions_deleted": len(context["user_decision_ids"]),
                    "legacy_unknown_user_decisions_preserved": context["legacy_unknown_user_decision_count"],
                    "system_activity_sessions_rebuilt": len(context["session_ids"]),
                    "retention_held": context["retention_held"],
                    "context_cascade": context_cascade,
                },
                ensure_ascii=False,
            ),
        ),
    )
    conn.commit()
    package_purge = _purge_package_cache_ids_locked(
        conn,
        list(package_cascade.get("package_ids_pending") or []),
    )
    vlm_purge = purge_retired_job_files(
        conn,
        _database_root(conn),
        list(vlm_deleted.get("job_ids_pending") or []),
    )
    evidence_purge = _purge_confirmation_evidence_after_commit(
        conn,
        evidence_scheduled,
    )

    cascade = {
        **preview,
        "gap_events_created": len(gap_ranges),
        "quality_feedback": int(feedback_count or 0),
        "vlm_pending_bytes": int(vlm_purge["bytes_deleted"] or 0),
        "vlm_pending_file_purge_failed": int(vlm_purge["failed_refs"] or 0),
        "ha_source_records": int(package_cascade["records_updated"] or 0),
        "ha_package_cache_files": int(package_purge["files_deleted"] or 0),
        "ha_package_cache_purge_failed": int(package_purge["failed_refs"] or 0),
        "system_activity_samples_rebuilt": rebuilt_sample_count,
        "system_activity_session_rollups_rebuilt": rebuilt_rollup_count,
        "confirmation_evidence_scheduled": evidence_scheduled,
        "confirmation_evidence_purged": int(evidence_purge["deleted"]),
        "confirmation_evidence_purge_failed": int(evidence_purge["failed"]),
        "confirmation_evidence_bytes_deleted": int(evidence_purge["bytes_deleted"]),
        "context_observations": int(context_cascade.get("observations") or 0),
        "context_verified_contexts": int(context_cascade.get("verified_contexts_revoked") or 0),
        "context_memory_candidates": int(context_cascade.get("memory_candidates_revoked") or 0),
        "context_bundle_members": int(context_cascade.get("bundle_members_revoked") or 0),
        "context_withdrawal_receipts": int(context_cascade.get("withdrawal_receipts_created") or 0),
    }
    purge_pending = int(package_purge["pending_refs"] or 0) + int(
        vlm_purge["pending_refs"] or 0
    )
    return {
        "status": "partial" if purge_pending else "completed",
        "activities_deleted": len(context["activity_ids"]),
        "deleted_activity_count": len(context["activity_ids"]),
        "deletion_group_ids": context["deletion_group_ids"],
        "packages_revoked": revoked,
        "cascade": cascade,
        "retention_held": context["retention_held"],
        "private_purge_pending": purge_pending,
    }


def _purge_confirmation_evidence_after_commit(
    conn: sqlite3.Connection,
    scheduled_count: int,
) -> dict[str, int]:
    """Best-effort physical purge after the owning row deletion commits.

    A purge error must never roll back the terminal user action. Rolling back
    only the post-commit purge transaction restores any affected evidence rows
    to ``delete_pending`` so reconciliation can retry safely.
    """
    if scheduled_count <= 0:
        return {"deleted": 0, "failed": 0, "bytes_deleted": 0}
    try:
        return purge_scheduled_evidence(conn)
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        return {"deleted": 0, "failed": scheduled_count, "bytes_deleted": 0}


def _database_root(conn: sqlite3.Connection) -> Path:
    for row in conn.execute("PRAGMA database_list").fetchall():
        name = str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
        if name != "main":
            continue
        path = str(row["file"] if isinstance(row, sqlite3.Row) else row[2])
        if path:
            return Path(path).resolve().parent
    raise RuntimeError("Private purge requires a file-backed SQLite database")


def _deletion_context(
    conn: sqlite3.Connection,
    start: str,
    end: str,
    *,
    hold_unresolved: bool = False,
    file_change_cutoff: str | None = None,
) -> dict[str, Any]:
    """Collect every row a deletion of ``[start, end)`` must remove.

    ``file_change_cutoff`` narrows only the file-tracking tables so the daily
    retention pass can expire screen activity after two days while file change
    history keeps its own longer window.  A manual deletion leaves it unset and
    therefore still removes everything the owner selected.
    """
    activity_rows = conn.execute(
        """
        SELECT id, deletion_group_id
        FROM extracted_activities
        WHERE datetime(started_at) < datetime(?)
          AND (
            datetime(COALESCE(ended_at, started_at)) > datetime(?)
            OR datetime(started_at) >= datetime(?)
          )
        """,
        (end, start, start),
    ).fetchall()
    held_item_keys: list[str] = []
    held_activity_ids: set[str] = set()
    held_deletion_group_ids: set[str] = set()
    if hold_unresolved:
        selected_activity_ids = {str(row["id"]) for row in activity_rows}
        selected_deletion_groups = {
            str(row["deletion_group_id"])
            for row in activity_rows
            if row["deletion_group_id"]
        }
        for item in conn.execute(
            "SELECT item_key,source_activity_ids,deletion_group_ids "
            "FROM activity_confirmation_items "
            "WHERE state IN ('pending','deferred') AND answer IS NULL"
        ).fetchall():
            source_ids = set(_json_ids(item["source_activity_ids"]))
            deletion_groups = set(_json_ids(item["deletion_group_ids"]))
            matched_activity_ids = selected_activity_ids.intersection(source_ids)
            matched_deletion_groups = selected_deletion_groups.intersection(deletion_groups)
            if matched_activity_ids or matched_deletion_groups:
                held_item_keys.append(str(item["item_key"]))
                held_activity_ids.update(matched_activity_ids)
                held_deletion_group_ids.update(matched_deletion_groups)
                # One episode may bind sources that fall just outside the
                # cleanup selection. Holding all of its live sources preserves
                # a coherent question/evidence lifecycle.
                held_activity_ids.update(source_ids)
                held_deletion_group_ids.update(deletion_groups)
        # Pre-v28 unresolved rows may have neither provenance list populated.
        # Retention holds them by their own observed interval so the ordinary
        # today+yesterday cleanup cannot silently erase an unanswered card.
        for item in conn.execute(
            "SELECT item_key FROM activity_confirmation_items "
            "WHERE state IN ('pending','deferred') AND answer IS NULL "
            "AND COALESCE(source_activity_ids,'[]') IN ('','[]') "
            "AND COALESCE(deletion_group_ids,'[]') IN ('','[]') "
            "AND datetime(observed_start)<datetime(?) "
            "AND datetime(COALESCE(observed_end,observed_start))>datetime(?)",
            (end, start),
        ).fetchall():
            held_item_keys.append(str(item["item_key"]))
        activity_rows = [
            row
            for row in activity_rows
            if str(row["id"]) not in held_activity_ids
            and str(row["deletion_group_id"] or "") not in held_deletion_group_ids
        ]
    activity_ids = [str(row["id"]) for row in activity_rows]
    deletion_group_ids = sorted(
        {str(row["deletion_group_id"]) for row in activity_rows if row["deletion_group_id"]}
    )
    frame_signal_ids = _time_or_group_ids(
        conn,
        "frame_signals",
        "captured_at",
        start,
        end,
        deletion_group_ids,
    )
    if held_deletion_group_ids:
        frame_signal_ids = _exclude_ids_in_deletion_groups(
            conn,
            "frame_signals",
            frame_signal_ids,
            held_deletion_group_ids,
        )
    document_signal_ids = _time_or_group_ids(
        conn,
        "document_signals",
        "captured_at",
        start,
        end,
        deletion_group_ids,
    )
    if held_deletion_group_ids:
        document_signal_ids = _exclude_ids_in_deletion_groups(
            conn,
            "document_signals",
            document_signal_ids,
            held_deletion_group_ids,
        )
    file_change_query = (
        "SELECT id, file_id, root_id FROM file_change_events WHERE datetime(last_changed_at)>=datetime(?) "
        "AND datetime(first_changed_at)<datetime(?)"
    )
    file_change_params: list[Any] = [start, end]
    file_state_query = (
        "SELECT file_id,root_id FROM file_watch_state "
        "WHERE datetime(last_seen_at)>=datetime(?) AND datetime(first_seen_at)<datetime(?)"
    )
    file_state_params: list[Any] = [start, end]
    if file_change_cutoff:
        # Keep file history (and the baseline fingerprints it is read against)
        # until its own longer cutoff.  Without the state exemption every daily
        # pass would discard the baseline for any file untouched that day and
        # force a fresh full rescan.
        file_change_query += " AND datetime(last_changed_at)<datetime(?)"
        file_change_params.append(file_change_cutoff)
        file_state_query += " AND datetime(last_seen_at)<datetime(?)"
        file_state_params.append(file_change_cutoff)
    file_change_rows = conn.execute(file_change_query, tuple(file_change_params)).fetchall()
    file_change_event_ids = [str(row["id"]) for row in file_change_rows]
    file_change_version_ids = _values_for_ids(
        conn, "file_change_versions", "id", "aggregate_event_id", file_change_event_ids)
    coverage_gap_query = (
        "SELECT id FROM file_watch_coverage_gaps "
        "WHERE datetime(observed_at)>=datetime(?) AND datetime(observed_at)<datetime(?)"
    )
    coverage_gap_params: list[Any] = [start, end]
    if file_change_cutoff:
        # Attribution continuity is part of the 90-day file-history evidence,
        # not the two-day screen-activity window.  A daily retention pass may
        # delete only gaps that have themselves crossed the file cutoff.
        coverage_gap_query += " AND datetime(observed_at)<datetime(?)"
        coverage_gap_params.append(file_change_cutoff)
    file_watch_coverage_gap_ids = [
        str(row["id"])
        for row in conn.execute(
            coverage_gap_query,
            tuple(coverage_gap_params),
        ).fetchall()
    ]
    file_state_rows = conn.execute(file_state_query, tuple(file_state_params)).fetchall()
    file_change_state_ids = sorted(
        {
            str(row["file_id"])
            for row in [*file_change_rows, *file_state_rows]
        }
    )
    file_change_root_ids = sorted(
        {
            str(row["root_id"])
            for row in [*file_change_rows, *file_state_rows]
        }
    )
    provenance_ids = _provenance_ids(
        conn,
        deletion_group_ids=deletion_group_ids,
        derived_ids=[*activity_ids, *frame_signal_ids, *document_signal_ids],
        source_ids=[*frame_signal_ids, *document_signal_ids],
    )
    session_rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT id, started_at, ended_at, last_observed_at
            FROM system_activity_sessions
            WHERE datetime(started_at) < datetime(?)
              AND (
                datetime(COALESCE(ended_at, last_observed_at, started_at)) > datetime(?)
                OR datetime(started_at) >= datetime(?)
              )
            ORDER BY started_at ASC
            """,
            (end, start, start),
        ).fetchall()
    ]
    session_ids = [str(row["id"]) for row in session_rows]
    capture_fingerprint_ids = [
        str(row["display_id"])
        for row in conn.execute(
            """
            SELECT display_id FROM capture_fingerprints
            WHERE datetime(captured_at) >= datetime(?) AND datetime(captured_at) < datetime(?)
            """,
            (start, end),
        ).fetchall()
    ]
    package_ids = _overlapping_package_ids(conn, start, end)
    user_decision_ids = _user_decision_ids_for_source(
        conn,
        start=start,
        end=end,
        package_ids=package_ids,
        activity_ids=activity_ids,
        deletion_group_ids=deletion_group_ids,
    )
    activity_confirmation_item_keys = _activity_confirmation_item_keys_for_source(
        conn,
        start=start,
        end=end,
        activity_ids=activity_ids,
        deletion_group_ids=deletion_group_ids,
    )
    if held_item_keys:
        held_key_set = set(held_item_keys)
        activity_confirmation_item_keys = [
            item_key
            for item_key in activity_confirmation_item_keys
            if item_key not in held_key_set
        ]
    legacy_unknown_user_decision_count = _legacy_unknown_user_decision_count(conn)
    held_evidence_count = len(set(_values_for_ids(
        conn, "activity_confirmation_evidence_refs", "evidence_id", "item_key", held_item_keys,
        where="released_at IS NULL AND evidence_id IS NOT NULL")))
    retention_held = {
        "activity_count": len(held_activity_ids.intersection({
            str(row["id"])
            for row in conn.execute(
                "SELECT id FROM extracted_activities "
                "WHERE datetime(started_at)<datetime(?) AND datetime(COALESCE(ended_at,started_at))>datetime(?)",
                (end, start),
            ).fetchall()
        })),
        "confirmation_count": len(set(held_item_keys)),
        "evidence_count": held_evidence_count,
    }
    return {
        "activity_ids": activity_ids,
        "deletion_group_ids": deletion_group_ids,
        "frame_signal_ids": frame_signal_ids,
        "document_signal_ids": document_signal_ids,
        "file_change_event_ids": file_change_event_ids,
        "file_change_version_ids": file_change_version_ids,
        "file_watch_coverage_gap_ids": file_watch_coverage_gap_ids,
        "file_change_state_ids": file_change_state_ids,
        "file_change_root_ids": file_change_root_ids,
        "provenance_ids": provenance_ids,
        "session_rows": session_rows,
        "session_ids": session_ids,
        "capture_fingerprint_ids": capture_fingerprint_ids,
        "package_ids": package_ids,
        "activity_confirmation_item_keys": activity_confirmation_item_keys,
        "user_decision_ids": user_decision_ids,
        "legacy_unknown_user_decision_count": legacy_unknown_user_decision_count,
        "retention_held": retention_held,
        "counts": {
            "search_index": _count_for_ids(conn, "search_index", "entity_id", activity_ids),
            "segments_touched": _count_overlap(conn, "segments", "started_at", "ended_at", start, end),
            "raw_frame_candidates": _count_range(conn, "raw_frame_candidates", "captured_at", start, end),
            "display_frame_contexts": _count_range(conn, "display_frame_contexts", "captured_at", start, end),
            "raw_capture_jobs": _count_overlap(conn, "raw_capture_jobs", "started_at", "COALESCE(ended_at, started_at)", start, end),
            "privacy_events": _count_overlap(conn, "privacy_events", "started_at", "COALESCE(ended_at, started_at)", start, end),
            "gap_events": _count_overlap(conn, "gap_events", "started_at", "COALESCE(ended_at, started_at)", start, end),
            "duplicate_events": _count_overlap(conn, "duplicate_events", "started_at", "COALESCE(ended_at, started_at)", start, end),
            "coverage_ledger": _count_overlap(conn, "capture_coverage_ledger", "started_at", "ended_at", start, end),
            "vlm_pending_jobs": _count_range(conn, "vlm_pending_jobs", "captured_at", start, end),
            "quality_feedback": _count_quality_feedback(conn, start, end, deletion_group_ids, activity_ids),
            "ha_automation_jobs": _count_overlap(conn, "ha_automation_jobs", "started_at", "ended_at", start, end),
            "quality_advice_runs": _count_overlap(conn, "quality_advice_runs", "started_at", "ended_at", start, end),
            "session_transition_evidence": _count_range(conn, "session_transition_evidence", "observed_at", start, end),
            "soak_capture_cycles": _count_range(conn, "soak_capture_cycles", "observed_at", start, end),
            "soak_resource_samples": _count_range(conn, "soak_resource_samples", "observed_at", start, end),
            "soak_evidence_events": _count_range(conn, "soak_evidence_events", "occurred_at", start, end),
            "soak_daily_snapshots": _count_soak_daily_snapshots(conn, start, end),
            "system_activity_samples": _count_for_ids(conn, "system_activity_samples", "session_id", session_ids),
            "system_activity_session_rollups": _count_for_ids(conn, "system_activity_session_rollups", "session_id", session_ids),
        },
    }


def _cascade_counts(context: dict[str, Any]) -> dict[str, int]:
    counts = context["counts"]
    retention_held = context.get("retention_held") or {}
    return {
        "activities": len(context["activity_ids"]),
        "search_index": int(counts["search_index"]),
        "segments_touched": int(counts["segments_touched"]),
        "frame_signals": len(context["frame_signal_ids"]),
        "document_signals": len(context["document_signal_ids"]),
        "file_change_events": len(context["file_change_event_ids"]),
        "file_change_versions": len(context["file_change_version_ids"]),
        "file_watch_coverage_gaps": len(context["file_watch_coverage_gap_ids"]),
        "file_watch_state_reset": len(context["file_change_state_ids"]),
        "raw_frame_candidates": int(counts["raw_frame_candidates"]),
        "display_frame_contexts": int(counts["display_frame_contexts"]),
        "raw_capture_jobs": int(counts["raw_capture_jobs"]),
        "privacy_events": int(counts["privacy_events"]),
        "gap_events": int(counts["gap_events"]),
        "duplicate_events": int(counts["duplicate_events"]),
        "coverage_ledger": int(counts["coverage_ledger"]),
        "vlm_pending_jobs": int(counts["vlm_pending_jobs"]),
        "activity_provenance": len(context["provenance_ids"]),
        "quality_feedback": int(counts["quality_feedback"]),
        "ha_automation_jobs": int(counts["ha_automation_jobs"]),
        "quality_advice_runs": int(counts["quality_advice_runs"]),
        "session_transition_evidence": int(counts["session_transition_evidence"]),
        "soak_capture_cycles": int(counts["soak_capture_cycles"]),
        "soak_resource_samples": int(counts["soak_resource_samples"]),
        "soak_evidence_events": int(counts["soak_evidence_events"]),
        "soak_daily_snapshots": int(counts["soak_daily_snapshots"]),
        "capture_fingerprints": len(context["capture_fingerprint_ids"]),
        "confirmation_items_deleted": len(context["activity_confirmation_item_keys"]),
        "user_decisions": len(context["user_decision_ids"]),
        "user_decisions_legacy_unknown_preserved": int(context["legacy_unknown_user_decision_count"]),
        "system_activity_sessions_touched": len(context["session_ids"]),
        "system_activity_samples_removed": int(counts["system_activity_samples"]),
        "system_activity_session_rollups_removed": int(counts["system_activity_session_rollups"]),
        "ha_source_records": len(context["package_ids"]),
        "index_references": int(counts["search_index"]) + len(context["provenance_ids"]),
        "retention_held_activities": int(retention_held.get("activity_count") or 0),
        "retention_held_confirmations": int(retention_held.get("confirmation_count") or 0),
        "retention_held_evidence": int(retention_held.get("evidence_count") or 0),
    }


def _replacement_gap_ranges(
    context: dict[str, Any],
    start: str,
    end: str,
    *,
    source_status: str,
) -> list[tuple[str, str]]:
    if source_status == "expired_deleted":
        return []
    deletable_count = sum(
        value
        for key, value in _cascade_counts(context).items()
        if key
        not in {
            "segments_touched",
            "system_activity_sessions_touched",
            "ha_source_records",
            "index_references",
            "user_decisions_legacy_unknown_preserved",
            "session_transition_evidence",
            "soak_capture_cycles",
            "soak_resource_samples",
            "soak_evidence_events",
            "soak_daily_snapshots",
        }
    )
    if deletable_count <= 0 and not context["session_ids"]:
        return []
    return [(start, end)]


def _insert_replacement_gap(
    conn: sqlite3.Connection,
    start: str,
    end: str,
    *,
    reason: str,
    source_status: str,
    deleted_activities: int,
) -> None:
    conn.execute(
        """
        INSERT INTO gap_events(id, started_at, ended_at, reason, display_ids, raw_deleted_at, detail, deletion_group_id)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"gap_{uuid.uuid4().hex}",
            start,
            end,
            "user_deleted" if source_status == "user_deleted" else "source_deleted",
            "[]",
            utc_now(),
            json.dumps(
                {
                    "reason": reason,
                    "source_status": source_status,
                    "deleted_activities": deleted_activities,
                    "summary": "content-free gap generated after user deletion because overlapping activities were not recomputed",
                },
                ensure_ascii=False,
            ),
            f"delete_gap_{uuid.uuid4().hex}",
        ),
    )


def _time_or_group_ids(conn, table, time_column, start, end, deletion_group_ids) -> list[str]:
    rows = conn.execute(
        f"SELECT id FROM {table} WHERE datetime({time_column}) >= datetime(?) AND datetime({time_column}) < datetime(?)",
        (start, end)).fetchall()
    ids = [str(row["id"]) for row in rows]
    ids.extend(_values_for_ids(conn, table, "id", "deletion_group_id", deletion_group_ids))
    return list(dict.fromkeys(ids))


def _exclude_ids_in_deletion_groups(
    conn: sqlite3.Connection,
    table: str,
    ids: list[str],
    held_deletion_group_ids: set[str],
) -> list[str]:
    """Drop IDs whose row belongs to a held deletion group.

    Binding both growing sets into one statement made the parameter count their
    sum. Keep the group comparison in Python and stream IDs in bounded chunks;
    this also works when the caller has a read-only connection.
    """
    if not ids or not held_deletion_group_ids:
        return ids
    held = {str(value) for value in held_deletion_group_ids}
    kept: list[str] = []
    for offset in range(0, len(ids), _SQL_VARIABLE_CHUNK):
        chunk = ids[offset : offset + _SQL_VARIABLE_CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT id, COALESCE(deletion_group_id, '') AS held_group FROM {table} "
            f"WHERE id IN ({placeholders})",
            chunk,
        ).fetchall()
        kept.extend(
            str(row["id"]) for row in rows if str(row["held_group"]) not in held
        )
    return kept


def _provenance_ids(conn, *, deletion_group_ids, derived_ids, source_ids) -> list[str]:
    ids = []
    for column, values in (("deletion_group_id", deletion_group_ids), ("derived_id", derived_ids), ("source_id", source_ids)):
        ids.extend(_values_for_ids(conn, "activity_provenance", "id", column, values))
    return list(dict.fromkeys(ids))


def _quality_feedback_ids(conn, start, end, deletion_group_ids, target_ids) -> list[str]:
    rows = conn.execute(
        "SELECT id FROM quality_feedback WHERE "
        "datetime(COALESCE(target_started_at,target_ended_at,created_at)) < datetime(?) "
        "AND (datetime(COALESCE(target_ended_at,target_started_at,created_at)) > datetime(?) "
        "OR datetime(COALESCE(target_started_at,target_ended_at,created_at)) >= datetime(?))",
        (end, start, start)).fetchall()
    ids = [str(row["id"]) for row in rows]
    ids.extend(_values_for_ids(conn, "quality_feedback", "id", "deletion_group_id", deletion_group_ids))
    ids.extend(_values_for_ids(conn, "quality_feedback", "id", "target_id", target_ids))
    return list(dict.fromkeys(ids))


def _count_quality_feedback(conn, start, end, deletion_group_ids, activity_ids) -> int:
    return len(_quality_feedback_ids(conn, start, end, deletion_group_ids, activity_ids))


def _delete_quality_feedback(conn, start, end, *, deletion_group_ids, target_ids) -> int:
    ids = _quality_feedback_ids(conn, start, end, deletion_group_ids, target_ids)
    _delete_ids(conn, "quality_feedback", "id", ids)
    return len(ids)


def _overlapping_package_ids(conn: sqlite3.Connection, start: str, end: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT d.id, d.date_range
        FROM distribution_packages d
        JOIN ha_source_records h ON h.package_id = d.id
        WHERE d.target = 'ha' AND h.source_status = 'active'
        """
    ).fetchall()
    package_ids: list[str] = []
    for row in rows:
        try:
            date_range = json.loads(row["date_range"] or "{}")
            overlaps = parse_time(end) > parse_time(str(date_range["from"])) and parse_time(start) < parse_time(str(date_range["to"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            overlaps = False
        if overlaps:
            package_ids.append(str(row["id"]))
    return package_ids


def _count_range(conn: sqlite3.Connection, table: str, column: str, start: str, end: str) -> int:
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS count FROM {table}
        WHERE datetime({column}) >= datetime(?) AND datetime({column}) < datetime(?)
        """,
        (start, end),
    ).fetchone()
    return int(row["count"] or 0)


def _count_soak_daily_snapshots(conn: sqlite3.Connection, start: str, end: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM soak_daily_snapshots
        WHERE date(day) >= date(?) AND date(day) <= date(datetime(?, '-1 second'))
        """,
        (start, end),
    ).fetchone()
    return int(row["count"] or 0)


def _count_overlap(
    conn: sqlite3.Connection,
    table: str,
    start_column: str,
    end_column: str,
    start: str,
    end: str,
) -> int:
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS count FROM {table}
        WHERE datetime({start_column}) < datetime(?)
          AND (
            datetime({end_column}) > datetime(?)
            OR datetime({start_column}) >= datetime(?)
          )
        """,
        (end, start, start),
    ).fetchone()
    return int(row["count"] or 0)


def _id_chunks(ids):
    # Disjoint values preserve SQL IN semantics when callers pass duplicates.
    values = list(dict.fromkeys(ids))
    for offset in range(0, len(values), _SQL_VARIABLE_CHUNK):
        yield values[offset:offset + _SQL_VARIABLE_CHUNK]


def _values_for_ids(conn, table, selected_column, column, ids, *, where="1=1") -> list[str]:
    values = []
    for chunk in _id_chunks(ids):
        placeholders = ",".join("?" for _ in chunk)
        values.extend(str(row[0] or "") for row in conn.execute(
            f"SELECT {selected_column} FROM {table} WHERE ({where}) AND {column} IN ({placeholders})", chunk))
    return values


def _count_for_ids(conn, table, column, ids) -> int:
    count = 0
    for chunk in _id_chunks(ids):
        placeholders = ",".join("?" for _ in chunk)
        count += conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} IN ({placeholders})", chunk).fetchone()[0]
    return count


def _delete_ids(conn, table, column, ids) -> None:
    for chunk in _id_chunks(ids):
        placeholders = ",".join("?" for _ in chunk)
        conn.execute(f"DELETE FROM {table} WHERE {column} IN ({placeholders})", chunk)


def _delete_range_rows(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    start: str,
    end: str,
) -> None:
    conn.execute(
        f"""
        DELETE FROM {table}
        WHERE datetime({column}) >= datetime(?) AND datetime({column}) < datetime(?)
        """,
        (start, end),
    )


def _delete_soak_daily_snapshots(conn: sqlite3.Connection, start: str, end: str) -> None:
    conn.execute(
        """
        DELETE FROM soak_daily_snapshots
        WHERE date(day) >= date(?) AND date(day) <= date(datetime(?, '-1 second'))
        """,
        (start, end),
    )


def _delete_overlap_rows(
    conn: sqlite3.Connection,
    table: str,
    start_column: str,
    end_column: str,
    start: str,
    end: str,
) -> None:
    conn.execute(
        f"""
        DELETE FROM {table}
        WHERE datetime({start_column}) < datetime(?)
          AND (
            datetime({end_column}) > datetime(?)
            OR datetime({start_column}) >= datetime(?)
          )
        """,
        (end, start, start),
    )


def _user_decision_ids_for_source(
    conn: sqlite3.Connection,
    *,
    start: str,
    end: str,
    package_ids: list[str],
    activity_ids: list[str],
    deletion_group_ids: list[str],
) -> list[str]:
    columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(user_decisions)").fetchall()}
    required = {
        "item_key",
        "source_package_id",
        "source_activity_ids",
        "source_start",
        "source_end",
        "deletion_group_ids",
        "provenance_status",
    }
    if not required.issubset(columns):
        return []
    rows = conn.execute(
        """
        SELECT item_key, source_package_id, source_activity_ids,
               source_start, source_end, deletion_group_ids, provenance_status
        FROM user_decisions
        WHERE provenance_status != 'legacy_unknown'
        """
    ).fetchall()
    package_set = set(package_ids)
    activity_set = set(activity_ids)
    deletion_group_set = set(deletion_group_ids)
    matched: list[str] = []
    for row in rows:
        source_package_id = str(row["source_package_id"] or "")
        source_activity_ids = _json_ids(row["source_activity_ids"])
        source_deletion_groups = _json_ids(row["deletion_group_ids"])
        time_match = _source_range_overlaps(
            str(row["source_start"] or ""),
            str(row["source_end"] or ""),
            start,
            end,
        )
        if (
            time_match
            or bool(source_package_id and source_package_id in package_set)
            or bool(activity_set.intersection(source_activity_ids))
            or bool(deletion_group_set.intersection(source_deletion_groups))
        ):
            matched.append(str(row["item_key"]))
    return matched


def _activity_confirmation_item_keys_for_source(
    conn: sqlite3.Connection,
    *,
    start: str,
    end: str,
    activity_ids: list[str],
    deletion_group_ids: list[str],
) -> list[str]:
    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(activity_confirmation_items)").fetchall()
    }
    required = {
        "item_key",
        "observed_start",
        "observed_end",
        "source_activity_ids",
        "deletion_group_ids",
    }
    if not required.issubset(columns):
        return []
    rows = conn.execute(
        """
        SELECT item_key,observed_start,observed_end,source_activity_ids,deletion_group_ids
        FROM activity_confirmation_items
        """
    ).fetchall()
    activity_set = set(activity_ids)
    deletion_group_set = set(deletion_group_ids)
    matched: list[str] = []
    for row in rows:
        row_activity_ids = _json_ids(row["source_activity_ids"])
        row_deletion_groups = _json_ids(row["deletion_group_ids"])
        traceable = bool(row_activity_ids or row_deletion_groups)
        source_match = bool(activity_set.intersection(row_activity_ids)) or bool(
            deletion_group_set.intersection(row_deletion_groups)
        )
        legacy_time_match = not traceable and _source_range_overlaps(
            str(row["observed_start"] or ""),
            str(row["observed_end"] or row["observed_start"] or ""),
            start,
            end,
        )
        if source_match or legacy_time_match:
            matched.append(str(row["item_key"]))
    return matched


def _legacy_unknown_user_decision_count(conn: sqlite3.Connection) -> int:
    columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(user_decisions)").fetchall()}
    if "provenance_status" not in columns:
        row = conn.execute("SELECT COUNT(*) AS count FROM user_decisions").fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM user_decisions WHERE provenance_status = 'legacy_unknown'"
        ).fetchone()
    return int(row["count"] or 0)


def _source_range_overlaps(source_start: str, source_end: str, start: str, end: str) -> bool:
    if not source_start or not source_end:
        return False
    try:
        source_start_dt = parse_time(source_start)
        source_end_dt = parse_time(source_end)
        start_dt = parse_time(start)
        end_dt = parse_time(end)
    except (TypeError, ValueError):
        return False
    if source_start_dt == source_end_dt:
        return start_dt <= source_start_dt < end_dt
    return source_start_dt < end_dt and source_end_dt > start_dt


def _json_ids(value: Any) -> set[str]:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return set()
    elif isinstance(value, list):
        parsed = value
    else:
        return set()
    return {str(item) for item in parsed if isinstance(item, (str, int)) and str(item)}
