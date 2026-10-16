from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from .confirmation_evidence import purge_scheduled_evidence, schedule_evidence_release
from .document_changes import document_authorization_lock
from .ha_source import _purge_package_cache_ids_locked, revoke_packages_for_range
from .pipeline import utc_now
from .privacy import PRIVACY_FIREWALL_VERSION, REDACTION_POLICY_VERSION, adult_policy_metadata, classify_event


def reclassify_existing(
    conn: sqlite3.Connection,
    start: str | None = None,
    end: str | None = None,
    *,
    dry_run: bool = False,
) -> dict:
    # Match delete_range's lock order: authorization lease first, then SQLite
    # write intent. Public readers holding a shared lease cannot outlive a
    # completed privacy reclassification with stale content.
    if not dry_run and conn.in_transaction:
        raise RuntimeError("reclassify_existing requires a clean SQLite connection")
    with document_authorization_lock(conn, exclusive=not dry_run):
        started_transaction = bool(not dry_run)
        try:
            if started_transaction:
                conn.execute("BEGIN IMMEDIATE")
            return _reclassify_existing_locked(
                conn,
                start=start,
                end=end,
                dry_run=dry_run,
            )
        except Exception:
            if started_transaction and conn.in_transaction:
                conn.rollback()
            raise


def _reclassify_existing_locked(
    conn: sqlite3.Connection,
    start: str | None = None,
    end: str | None = None,
    *,
    dry_run: bool = False,
) -> dict:
    where = ["deleted_at IS NULL"]
    params: list[str] = []
    if start:
        where.append("datetime(COALESCE(ended_at, started_at)) > datetime(?)")
        params.append(start)
    if end:
        where.append("datetime(started_at) < datetime(?)")
        params.append(end)
    rows = conn.execute(
        f"""
        SELECT id, started_at, ended_at, activity_summary, apps, domains,
               privacy_level, deletion_group_id,
               (
                 SELECT fs.ocr_redacted_summary
                 FROM activity_provenance AS prov
                 JOIN frame_signals AS fs ON fs.id = prov.source_id
                 WHERE prov.derived_type = 'extracted_activity'
                   AND prov.derived_id = extracted_activities.id
                   AND prov.source_type = 'frame_signal'
                 LIMIT 1
               ) AS cleaned_text
        FROM extracted_activities
        WHERE {' AND '.join(where)}
        ORDER BY started_at ASC
        """,
        params,
    ).fetchall()

    checked = updated = blocked = gaps = 0
    evidence_scheduled = 0
    revoked_packages: set[str] = set()
    revoked_cache_package_ids: list[str] = []
    dry_run_findings: list[dict] = []
    for row in rows:
        checked += 1
        event = _row_to_event(row)
        result = classify_event(event)
        if result.status == "blocked":
            blocked += 1
            if dry_run:
                package_ids = _package_ids_for_range(conn, row["started_at"], row["ended_at"])
                revoked_packages.update(package_ids)
                dry_run_findings.append(_dry_run_finding(row, result, "would_block_and_remove_activity", package_ids))
            else:
                revoked, scheduled, cache_package_ids = _remove_activity(
                    conn,
                    row,
                    result,
                    "privacy_reclassified",
                )
                revoked_packages.update(revoked)
                evidence_scheduled += scheduled
                revoked_cache_package_ids.extend(cache_package_ids)
                _insert_privacy_event(conn, row, result)
            continue
        if result.status == "review_required":
            gaps += 1
            if dry_run:
                package_ids = _package_ids_for_range(conn, row["started_at"], row["ended_at"])
                revoked_packages.update(package_ids)
                dry_run_findings.append(_dry_run_finding(row, result, "would_convert_to_gap_event", package_ids))
            else:
                revoked, scheduled, cache_package_ids = _remove_activity(
                    conn,
                    row,
                    result,
                    "gap_reclassified",
                )
                revoked_packages.update(revoked)
                evidence_scheduled += scheduled
                revoked_cache_package_ids.extend(cache_package_ids)
                _insert_gap_event(conn, row, result)
            continue
        if _needs_update(row, result):
            updated += 1
            if dry_run:
                package_ids = _package_ids_for_range(conn, row["started_at"], row["ended_at"])
                revoked_packages.update(package_ids)
                dry_run_findings.append(_dry_run_finding(row, result, "would_update_redacted_activity", package_ids))
            else:
                revocation = revoke_packages_for_range(
                    conn,
                    row["started_at"],
                    row["ended_at"],
                    "privacy_reclassified_update",
                    source_status="revoked",
                    return_details=True,
                    defer_cache_delete=True,
                )
                revoked_packages.update(revocation["package_ids"])
                revoked_cache_package_ids.extend(revocation["package_ids_pending"])
                evidence_scheduled += _update_activity(conn, row, result)

    metadata = {
        "checked": checked,
        "updated": updated,
        "blocked": blocked,
        "gaps": gaps,
        "packages_revoked": len(revoked_packages),
        "dry_run": dry_run,
        "privacy_firewall_version": PRIVACY_FIREWALL_VERSION,
        "redaction_policy_version": REDACTION_POLICY_VERSION,
        "adult_policy": adult_policy_metadata(),
        "confirmation_evidence_scheduled": evidence_scheduled,
    }
    if not dry_run:
        conn.execute(
            """
            INSERT INTO audit_logs(id, at, action, target_type, target_id, detail)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                f"audit_{uuid.uuid4().hex}",
                utc_now(),
                "reclassify_existing",
                "extracted_activities",
                "*",
                json.dumps(metadata, ensure_ascii=False),
            ),
        )
        conn.commit()
    cache_purge = (
        {
            "files_deleted": 0,
            "failed_refs": 0,
            "pending_refs": 0,
        }
        if dry_run
        else _purge_package_cache_ids_locked(conn, revoked_cache_package_ids)
    )
    evidence_purge = _purge_confirmation_evidence_after_commit(
        conn,
        evidence_scheduled,
    )
    output = {
        "status": "partial" if cache_purge["pending_refs"] else "completed",
        "checked": checked,
        "updated": updated,
        "blocked": blocked,
        "gaps": gaps,
        "packages_revoked": sorted(revoked_packages),
        "dry_run": dry_run,
        "privacy_firewall_version": PRIVACY_FIREWALL_VERSION,
        "redaction_policy_version": REDACTION_POLICY_VERSION,
        "adult_policy": adult_policy_metadata(),
        "confirmation_evidence_scheduled": evidence_scheduled,
        "confirmation_evidence_purged": int(evidence_purge["deleted"]),
        "confirmation_evidence_purge_failed": int(evidence_purge["failed"]),
        "confirmation_evidence_bytes_deleted": int(evidence_purge["bytes_deleted"]),
        "ha_package_cache_files_deleted": int(cache_purge["files_deleted"]),
        "ha_package_cache_purge_failed": int(cache_purge["failed_refs"]),
        "private_purge_pending": int(cache_purge["pending_refs"]),
    }
    if dry_run:
        output["dry_run_findings"] = dry_run_findings
        output["destructive_actions"] = False
        output["apply_note"] = "Dry run only. Confirm separately before running reclassify without --dry-run or delete-range."
    return output


def _row_to_event(row: sqlite3.Row) -> dict:
    apps = _loads(row["apps"], [])
    domains = _loads(row["domains"], [])
    app_name = apps[0] if apps else "Unknown"
    domain = domains[0] if domains else ""
    detail = str(row["cleaned_text"] or "").strip() or _strip_summary_context(str(row["activity_summary"] or ""), app_name, domain)
    return {
        "captured_at": row["started_at"],
        "display_id": "reclassify",
        "app_name": app_name,
        "window_title": "",
        "domain": domain,
        "ocr_text": detail,
        "context_confidence": 0.7,
    }


def _needs_update(row: sqlite3.Row, result) -> bool:
    return (
        row["activity_summary"] != result.activity_summary
        or row["apps"] != json.dumps([result.app_name], ensure_ascii=False)
        or row["privacy_level"] != result.privacy_level
        or str(row["cleaned_text"] or "") != result.ocr_redacted_summary
    )


def _update_activity(conn: sqlite3.Connection, row: sqlite3.Row, result) -> int:
    evidence_scheduled = _delete_activity_confirmation_items(conn, row)
    apps = json.dumps([result.app_name], ensure_ascii=False)
    domains = json.dumps([result.domain] if result.domain else [], ensure_ascii=False)
    conn.execute(
        """
        UPDATE extracted_activities
        SET activity_summary = ?, apps = ?, domains = ?, privacy_level = ?,
            redaction_confidence = ?, quarantine_reason = ?
        WHERE id = ?
        """,
        (
            result.activity_summary,
            apps,
            domains,
            result.privacy_level,
            result.redaction_confidence,
            result.reason,
            row["id"],
        ),
    )
    conn.execute("DELETE FROM search_index WHERE entity_id = ?", (row["id"],))
    conn.execute(
        """
        INSERT INTO search_index(entity_type, entity_id, started_at, app_name, domain, summary, keywords)
        VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "extracted_activity",
            row["id"],
            row["started_at"],
            result.app_name,
            result.domain,
            result.activity_summary,
            result.safe_keywords,
        ),
    )
    conn.execute(
        """
        UPDATE frame_signals
        SET app_name = ?, domain = ?, window_title_redacted = '',
            ocr_redacted_summary = ?, safe_keywords = ?,
            privacy_flags = ?, redaction_status = ?
        WHERE deletion_group_id = ?
        """,
        (
            result.app_name,
            result.domain,
            result.ocr_redacted_summary,
            result.safe_keywords,
            json.dumps(result.privacy_flags, ensure_ascii=False),
            "partial_redacted" if result.status == "partial_redacted" else "redacted",
            row["deletion_group_id"],
        ),
    )
    return evidence_scheduled


def _remove_activity(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    result,
    reason: str,
) -> tuple[list[str], int, list[str]]:
    revocation = revoke_packages_for_range(
        conn,
        row["started_at"],
        row["ended_at"],
        reason,
        source_status="revoked",
        return_details=True,
        defer_cache_delete=True,
    )
    evidence_scheduled = _delete_activity_confirmation_items(conn, row)
    conn.execute("DELETE FROM search_index WHERE entity_id = ?", (row["id"],))
    conn.execute("DELETE FROM extracted_activities WHERE id = ?", (row["id"],))
    conn.execute("DELETE FROM frame_signals WHERE deletion_group_id = ?", (row["deletion_group_id"],))
    conn.execute("DELETE FROM document_signals WHERE deletion_group_id = ?", (row["deletion_group_id"],))
    conn.execute("DELETE FROM activity_provenance WHERE deletion_group_id = ?", (row["deletion_group_id"],))
    return (
        list(revocation["package_ids"]),
        evidence_scheduled,
        list(revocation["package_ids_pending"]),
    )


def _delete_activity_confirmation_items(conn: sqlite3.Connection, row: sqlite3.Row) -> int:
    """Cascade privacy reclassification through source-bound confirmations."""
    columns = {
        str(item["name"])
        for item in conn.execute("PRAGMA table_info(activity_confirmation_items)").fetchall()
    }
    required = {"item_key", "source_activity_ids", "deletion_group_ids"}
    if not required.issubset(columns):
        return 0
    activity_id = str(row["id"] or "")
    deletion_group_id = str(row["deletion_group_id"] or "")
    matched: list[str] = []
    matched_group_keys: set[str] = set()
    for candidate in conn.execute(
        "SELECT item_key,group_key,source_activity_ids,deletion_group_ids "
        "FROM activity_confirmation_items"
    ).fetchall():
        source_ids = {str(value) for value in _loads(candidate["source_activity_ids"], [])}
        deletion_groups = {str(value) for value in _loads(candidate["deletion_group_ids"], [])}
        if (activity_id and activity_id in source_ids) or (
            deletion_group_id and deletion_group_id in deletion_groups
        ):
            matched.append(str(candidate["item_key"]))
            matched_group_keys.add(str(candidate["group_key"] or ""))
    if matched:
        # Phase 2 confirmation actions bind a legacy group/item to either a
        # governed graph or a durable non-verifying action receipt. Privacy
        # reclassification must revoke both in the same transaction before the
        # legacy source row disappears.
        link_keys = sorted(
            {key for key in (*matched, *matched_group_keys) if key}
        )
        if link_keys:
            from .context_console import _revoke_activity_confirmation_links_locked

            _revoke_activity_confirmation_links_locked(
                conn,
                link_keys,
                reason_code="privacy_reclassification",
                now=datetime.now(timezone.utc),
            )
        scheduled = schedule_evidence_release(
            conn,
            matched,
            reason="privacy_reclassification",
        )
        placeholders = ",".join("?" for _ in matched)
        conn.execute(
            f"DELETE FROM activity_confirmation_items WHERE item_key IN ({placeholders})",
            tuple(matched),
        )
        return scheduled
    return 0


def _purge_confirmation_evidence_after_commit(
    conn: sqlite3.Connection,
    scheduled_count: int,
) -> dict[str, int]:
    """Purge only after reclassification commits; preserve retry state on error."""
    if scheduled_count <= 0:
        return {"deleted": 0, "failed": 0, "bytes_deleted": 0}
    try:
        return purge_scheduled_evidence(conn)
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        return {"deleted": 0, "failed": scheduled_count, "bytes_deleted": 0}


def _package_ids_for_range(conn: sqlite3.Connection, start: str, end: str | None) -> list[str]:
    rows = conn.execute(
        """
        SELECT d.id, d.date_range
        FROM distribution_packages d
        JOIN ha_source_records h ON h.package_id = d.id
        WHERE d.target = 'ha' AND h.source_status = 'active'
        """
    ).fetchall()
    output: list[str] = []
    row_end = end or start
    for row in rows:
        try:
            package_range = json.loads(row["date_range"])
        except (TypeError, json.JSONDecodeError):
            continue
        if _ranges_overlap(start, row_end, package_range.get("from"), package_range.get("to")):
            output.append(row["id"])
    return output


def _dry_run_finding(row: sqlite3.Row, result, action: str, package_ids: list[str]) -> dict:
    apps = _loads(row["apps"], [])
    domains = _loads(row["domains"], [])
    return {
        "activity_id": str(row["id"] or ""),
        "started_at": row["started_at"],
        "ended_at": row["ended_at"],
        "old_app": _safe_label(apps[0] if apps else ""),
        "old_domain": _safe_label(domains[0] if domains else ""),
        "new_status": result.status,
        "reason": result.reason,
        "action": action,
        "packages_to_revoke": package_ids,
        "raw_content_included": False,
    }


def _safe_label(value: str, max_len: int = 120) -> str:
    text = " ".join(str(value or "").split())
    return text[:max_len]


def _insert_privacy_event(conn: sqlite3.Connection, row: sqlite3.Row, result) -> None:
    conn.execute(
        """
        INSERT INTO privacy_events(id, started_at, ended_at, type, reason, app_name, domain, action, deletion_group_id)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"privacy_{uuid.uuid4().hex}",
            row["started_at"],
            row["ended_at"],
            "privacy_firewall_reclassified",
            result.reason,
            result.app_name,
            result.domain,
            result.action,
            row["deletion_group_id"],
        ),
    )


def _insert_gap_event(conn: sqlite3.Connection, row: sqlite3.Row, result) -> None:
    conn.execute(
        """
        INSERT INTO gap_events(id, started_at, ended_at, reason, display_ids, raw_deleted_at, detail, deletion_group_id)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"gap_{uuid.uuid4().hex}",
            row["started_at"],
            row["ended_at"],
            result.reason,
            json.dumps(["reclassify"], ensure_ascii=False),
            utc_now(),
            "existing activity reclassified to content-free GapEvent",
            row["deletion_group_id"],
        ),
    )


def _loads(value: str, default):
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _strip_summary_context(summary: str, app_name: str, domain: str) -> str:
    detail = summary.strip()
    prefixes = [f"{app_name}："]
    if domain:
        prefixes.append(f"{app_name} / {domain}：")
    changed = True
    while changed:
        changed = False
        for prefix in prefixes:
            if detail.startswith(prefix):
                detail = detail[len(prefix) :].strip()
                changed = True
    return detail


def _ranges_overlap(start_a: str, end_a: str | None, start_b: str | None, end_b: str | None) -> bool:
    if not start_a or not start_b:
        return False
    end_a = end_a or start_a
    end_b = end_b or start_b
    return start_a < end_b and start_b < end_a
