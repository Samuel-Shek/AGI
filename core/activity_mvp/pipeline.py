from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from typing import Iterable

from .attribution_context import display_context_payload, safe_reason_codes
from .activity_confirmations import (
    materialize_business_episodes_for_window,
    upsert_activity_confirmation_candidate,
)
from .confirmation_evidence import (
    INTERNAL_FRAME_OWNER,
    INTERNAL_FRAME_REF,
    cleanup_uncommitted_confirmation_evidence,
    promote_confirmation_evidence,
    record_unavailable_evidence,
    schedule_item_evidence_source_release,
)
from .document_signals import public_document_signal
from .privacy import classify_event, safe_evidence_id
from .screen_regions import classify_screen_region
from .storage import init_schema


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_time(value: str) -> datetime:
    if not value:
        raise ValueError("missing timestamp")
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)


def iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def segment_bounds(ts: datetime, minutes: int = 10) -> tuple[datetime, datetime]:
    minute = (ts.minute // minutes) * minutes
    start = ts.replace(minute=minute, second=0, microsecond=0)
    return start, start + timedelta(minutes=minutes)


def load_jsonl(path: str | Path) -> list[dict]:
    records: list[dict] = []
    with Path(path).open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no} is not valid JSON") from exc
    return records


def _safe_display_attribution(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    reasons = safe_reason_codes(value.get("reason_codes"))
    return {
        "app_name": _safe_context_text(value.get("app_name"), max_len=80),
        "bundle_id": _safe_context_text(value.get("bundle_id"), max_len=120),
        "window_id": _safe_context_text(value.get("window_id"), max_len=80),
        "confidence": _safe_context_float(value.get("confidence"), default=0.0),
        "source": _safe_context_token(value.get("source"), fallback="unknown"),
        "reason_codes": reasons,
        "overlap_ratio": _safe_context_float(value.get("overlap_ratio"), default=0.0),
    }


def _safe_visible_windows_context(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    windows: list[dict] = []
    for item in value[:8]:
        if not isinstance(item, dict):
            continue
        app_name = _safe_context_text(item.get("app_name"), max_len=80)
        bundle_id = _safe_context_text(item.get("bundle_id"), max_len=120)
        if not app_name and not bundle_id:
            continue
        windows.append(
            {
                "window_id": _safe_context_text(item.get("window_id"), max_len=80),
                "app_name": app_name,
                "bundle_id": bundle_id,
                "has_title": bool(str(item.get("title") or item.get("window_title") or "").strip()),
                "frame": _safe_context_rect(item.get("frame") or item.get("bounds")),
                "overlap_ratio": _safe_context_float(item.get("overlap_ratio"), default=0.0),
            }
        )
    return windows


def _safe_context_rect(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    rect = {}
    for key in ("x", "y", "width", "height"):
        number = _safe_context_float(value.get(key), default=0.0)
        if number:
            rect[key] = number
    return rect


def _safe_context_text(value: object, *, max_len: int) -> str:
    return " ".join(str(value or "").split())[:max_len]


def _safe_context_float(value: object, *, default: float) -> float:
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return default


def _safe_context_token(value: object, *, fallback: str) -> str:
    token = "".join(ch if ch.isalnum() or ch in {"_", "-", "+", "."} else "_" for ch in str(value or "").lower())[:80].strip("_")
    return token or fallback


def _safe_context_reason_codes(value: object) -> list[str]:
    if not isinstance(value, list):
        value = [value]
    output: list[str] = []
    for item in value:
        token = _safe_context_token(item, fallback="")
        if token and token not in output:
            output.append(token)
    return output[:8]


def ingest_events(
    conn: sqlite3.Connection,
    events: Iterable[dict],
    *,
    commit: bool = True,
) -> dict:
    """Strip private frame references before classification and always release temp ownership."""
    sanitized_events: list[dict] = []
    frame_sources: dict[int, tuple[str, str]] = {}
    evidence_promotions: list[dict] = []
    for original in events:
        event = dict(original)
        frame_ref = str(event.pop(INTERNAL_FRAME_REF, "") or "")
        frame_owner = str(event.pop(INTERNAL_FRAME_OWNER, "") or "")
        sanitized_events.append(event)
        if frame_ref:
            frame_sources[id(event)] = (frame_ref, frame_owner)
    try:
        return _ingest_sanitized_events(
            conn,
            sanitized_events,
            frame_sources,
            evidence_promotions,
            commit=commit,
        )
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        try:
            cleanup_uncommitted_confirmation_evidence(conn, evidence_promotions)
        except Exception:
            # Preserve the ingest failure. Startup reconciliation provides the
            # fallback for a cleanup failure without concealing the root cause.
            pass
        raise
    finally:
        for frame_ref, owner in frame_sources.values():
            if owner != "capture":
                continue
            try:
                Path(frame_ref).unlink(missing_ok=True)
            except OSError:
                pass


def _ingest_sanitized_events(
    conn: sqlite3.Connection,
    events: Iterable[dict],
    frame_sources: dict[int, tuple[str, str]],
    evidence_promotions: list[dict],
    *,
    commit: bool = True,
) -> dict:
    init_schema(conn)
    event_list = sorted(events, key=lambda item: item.get("captured_at", ""))
    if not event_list:
        return {"ingested": 0, "clear": 0, "partial_redacted": 0, "blocked": 0, "review_required": 0}

    timestamps = [parse_time(str(event["captured_at"])) for event in event_list]
    session_id = f"session_{uuid.uuid4().hex}"
    conn.execute(
        "INSERT INTO sessions(id, started_at, ended_at, status) VALUES(?, ?, ?, ?)",
        (session_id, iso(min(timestamps)), iso(max(timestamps) + timedelta(minutes=1)), "closed"),
    )

    counts = Counter()
    segment_stats: dict[str, dict[str, Counter]] = defaultdict(
        lambda: {"apps": Counter(), "domains": Counter(), "privacy": Counter()}
    )
    episode_frame_sources: dict[str, dict[str, str]] = {}

    for event in event_list:
        frame_source = frame_sources.get(id(event))
        captured_at = parse_time(str(event["captured_at"]))
        seg_start, seg_end = segment_bounds(captured_at)
        segment_id = f"segment_{seg_start.strftime('%Y%m%dT%H%M%S%z')}"
        _upsert_segment(conn, segment_id, session_id, seg_start, seg_end)

        display_id = str(event.get("display_id") or "main")
        deletion_group_id = f"dg_{uuid.uuid4().hex}"
        raw_job_id = f"raw_job_{uuid.uuid4().hex}"
        candidate_id = f"raw_frame_{uuid.uuid4().hex}"
        context_id = f"display_ctx_{uuid.uuid4().hex}"
        now = utc_now()
        raw_refs_deleted = _raw_refs_confirmed_deleted(
            temp_capture_ref=None,
            temp_frame_ref=None,
            ocr_candidate_ref=None,
            raw_deleted_at=now,
            candidate_deleted_at=now,
        )

        conn.execute(
            """
            INSERT INTO raw_capture_jobs(
              id, session_id, started_at, ended_at, display_ids, temp_capture_ref,
              capture_type, processing_status, privacy_status, temp_ref_cleared, deleted_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                raw_job_id,
                session_id,
                iso(captured_at),
                iso(captured_at + timedelta(seconds=30)),
                json.dumps([display_id], ensure_ascii=False),
                None,
                "synthetic_event_adapter",
                "processed_deleted",
                "normal",
                1,
                now,
            ),
        )

        raw_document_signal = event.get("document_signal") if isinstance(event.get("document_signal"), dict) else None
        if raw_document_signal and raw_document_signal.get("status") == "rejected":
            raw_document_signal = None
        document_signal = public_document_signal(raw_document_signal)
        if document_signal and not event.get("document_text_for_privacy"):
            event = {
                **event,
                "document_text_for_privacy": str(
                    (raw_document_signal or {}).get("context_text_for_privacy")
                    or document_signal.get("change_summary_redacted")
                    or ""
                ),
            }
        result = classify_event(event)
        context_suppressed = bool(getattr(result, "context_suppressed", False))
        safe_display_attribution = _safe_display_attribution(event.get("display_attribution"))
        safe_display_attribution.update(
            display_context_payload(
                display_id,
                source=str(event.get("context_source") or ""),
                attribution=safe_display_attribution,
            )
        )
        app_contexts = (
            {}
            if context_suppressed
            else {
                "app_name": result.app_name,
                "bundle_id": result.bundle_id,
                "window_title_redacted": result.window_title_redacted,
                "display_attribution": safe_display_attribution,
                "display_class": safe_display_attribution.get("display_class") or "",
                "attribution_source": safe_display_attribution.get("attribution_source") or "",
                "attribution_reason_codes": safe_display_attribution.get("attribution_reason_codes") or [],
                "context_confidence": _safe_context_float(event.get("context_confidence"), default=0.0),
            }
        )
        conn.execute(
            """
            INSERT INTO raw_frame_candidates(
              id, session_id, segment_id, captured_at, display_id, temp_frame_ref,
              frame_hash, change_score, window_context_ref, url_risk_tags,
              ocr_candidate_ref, risk_flags, expires_at, deleted_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate_id,
                session_id,
                segment_id,
                iso(captured_at),
                display_id,
                None,
                str(event.get("frame_hash") or ""),
                float(event.get("change_score") or 1.0),
                context_id,
                json.dumps(result.privacy_flags, ensure_ascii=False),
                None,
                result.status,
                iso(captured_at + timedelta(minutes=5)),
                now,
            ),
        )

        conn.execute(
            """
            INSERT INTO display_frame_contexts(
              id, session_id, segment_id, captured_at, display_id, visible_windows,
              frontmost_window_id, window_bounds, app_contexts, url_contexts,
              context_confidence, source, privacy_flags
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                context_id,
                session_id,
                segment_id,
                iso(captured_at),
                display_id,
                json.dumps([] if context_suppressed else _safe_visible_windows_context(event.get("visible_windows")), ensure_ascii=False),
                "" if context_suppressed else str(event.get("frontmost_window_id") or ""),
                json.dumps({} if context_suppressed else event.get("window_bounds") or {}, ensure_ascii=False),
                json.dumps(app_contexts, ensure_ascii=False),
                json.dumps(
                    {"source": "context_suppressed"}
                    if context_suppressed
                    else {
                            "domain": result.domain,
                            "url_redacted": result.url_redacted,
                            "source": event.get("url_source") or "manual_or_unknown",
                            "browser_signal": bool(event.get("browser_signal")),
                            "browser_risk_tags": event.get("browser_risk_tags") or [],
                        },
                    ensure_ascii=False,
                ),
                float(event.get("context_confidence") or 0.7),
                str(event.get("context_source") or "synthetic_event_adapter"),
                json.dumps(result.privacy_flags, ensure_ascii=False),
            ),
        )

        if result.app_name:
            segment_stats[segment_id]["apps"][result.app_name] += 1
        if result.domain:
            segment_stats[segment_id]["domains"][result.domain] += 1

        if result.status == "blocked":
            counts["blocked"] += 1
            segment_stats[segment_id]["privacy"]["blocked"] += 1
            privacy_id = f"privacy_{uuid.uuid4().hex}"
            conn.execute(
                """
                INSERT INTO privacy_events(
                  id, started_at, ended_at, type, reason, app_name, domain, action, deletion_group_id
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    privacy_id,
                    iso(captured_at),
                    iso(captured_at + timedelta(seconds=30)),
                    "privacy_firewall_blocked",
                    result.reason,
                    result.app_name,
                    result.domain,
                    result.action,
                    deletion_group_id,
                ),
            )
            _insert_coverage(
                conn,
                session_id,
                segment_id,
                display_id,
                captured_at,
                "keyframe",
                blocked=1,
            )
            _insert_provenance(
                conn,
                "privacy_event",
                privacy_id,
                "raw_frame_candidate",
                candidate_id,
                captured_at,
                deletion_group_id,
            )
            continue

        if result.status == "review_required":
            counts["review_required"] += 1
            segment_stats[segment_id]["privacy"]["review_required"] += 1
            gap_id = f"gap_{uuid.uuid4().hex}"
            conn.execute(
                """
                INSERT INTO gap_events(
                  id, started_at, ended_at, reason, display_ids, raw_deleted_at, detail, deletion_group_id
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    gap_id,
                    iso(captured_at),
                    iso(captured_at + timedelta(seconds=30)),
                    result.reason,
                    json.dumps([display_id], ensure_ascii=False),
                    now,
                    "review_required converted to content-free GapEvent in P0",
                    deletion_group_id,
                ),
            )
            _insert_coverage(
                conn,
                session_id,
                segment_id,
                display_id,
                captured_at,
                "keyframe",
                review_required=1,
                gap=1,
            )
            _insert_provenance(
                conn,
                "gap_event",
                gap_id,
                "raw_frame_candidate",
                candidate_id,
                captured_at,
                deletion_group_id,
            )
            continue

        result, dedupe = _apply_cleaned_text_dedupe(conn, display_id, captured_at, result)
        if dedupe["status"] == "exact_duplicate":
            counts["duplicate_exact"] += 1
            duplicate_id = _insert_duplicate_event(
                conn,
                display_id,
                captured_at,
                "cleaned_content_duplicate",
                duplicate_count=1,
                source_activity_id=dedupe.get("source_activity_id") or "",
                note=dedupe["note"],
                deletion_group_id=deletion_group_id,
            )
            _insert_coverage(
                conn,
                session_id,
                segment_id,
                display_id,
                captured_at,
                "keyframe_content_duplicate",
                duplicate_exact=1,
                degraded_reason="cleaned_content_duplicate",
            )
            _insert_provenance(
                conn,
                "duplicate_event",
                duplicate_id,
                "raw_frame_candidate",
                candidate_id,
                captured_at,
                deletion_group_id,
            )
            continue

        counts["clear"] += 1
        if result.status == "partial_redacted":
            counts["partial_redacted"] += 1
        if dedupe["status"] == "partial_duplicate":
            counts["partial_duplicate"] += 1
        frame_signal_id = f"frame_{uuid.uuid4().hex}"
        activity_id = f"activity_{uuid.uuid4().hex}"
        evidence_ref = safe_evidence_id(f"{candidate_id}:{context_id}:{captured_at.isoformat()}")
        screen_region_signal = classify_screen_region(
            {
                **event,
                "app_name": result.app_name,
                "window_title": result.window_title_redacted,
            }
        )

        conn.execute(
            """
            INSERT INTO frame_signals(
              id, session_id, segment_id, captured_at, display_id, app_name, bundle_id,
              window_title_redacted, url_redacted, domain, ocr_redacted_summary,
              safe_keywords, privacy_flags, screen_region_signal, redaction_status, dedupe_status, dedupe_note,
              dedupe_duplicate_lines, dedupe_retained_lines, dedupe_content_hash,
              dedupe_line_hashes, raw_ref_deleted, deletion_group_id
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                frame_signal_id,
                session_id,
                segment_id,
                iso(captured_at),
                display_id,
                result.app_name,
                result.bundle_id,
                result.window_title_redacted,
                result.url_redacted,
                result.domain,
                result.ocr_redacted_summary,
                result.safe_keywords,
                json.dumps(result.privacy_flags, ensure_ascii=False),
                json.dumps(screen_region_signal, ensure_ascii=False, sort_keys=True),
                "partial_redacted" if result.status == "partial_redacted" else "redacted",
                dedupe["status"],
                dedupe["note"],
                dedupe["duplicate_lines"],
                dedupe["retained_lines"],
                dedupe["content_hash"],
                json.dumps(dedupe["line_hashes"], ensure_ascii=False),
                1 if raw_refs_deleted else 0,
                deletion_group_id,
            ),
        )
        conn.execute(
            """
            INSERT INTO extracted_activities(
              id, session_id, segment_id, started_at, ended_at, activity_summary,
              apps, domains, evidence_refs, privacy_level, redaction_confidence,
              quarantine_status, quarantine_reason, quarantine_expires_at, dedupe_status,
              dedupe_note, dedupe_duplicate_lines, dedupe_retained_lines,
              dedupe_content_hash, deletion_group_id
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                activity_id,
                session_id,
                segment_id,
                iso(captured_at),
                iso(captured_at + timedelta(seconds=30)),
                result.activity_summary,
                json.dumps([result.app_name], ensure_ascii=False),
                json.dumps([result.domain] if result.domain else [], ensure_ascii=False),
                json.dumps([evidence_ref], ensure_ascii=False),
                result.privacy_level,
                result.redaction_confidence,
                "clear",
                result.reason,
                None,
                dedupe["status"],
                dedupe["note"],
                dedupe["duplicate_lines"],
                dedupe["retained_lines"],
                dedupe["content_hash"],
                deletion_group_id,
            ),
        )
        confirmation = upsert_activity_confirmation_candidate(
            conn,
            {
                "id": activity_id,
                "started_at": iso(captured_at),
                "ended_at": iso(captured_at + timedelta(seconds=30)),
                "activity_summary": result.activity_summary,
                "cleaned_text": result.ocr_redacted_summary,
                "cleaned_title": result.window_title_redacted,
                "apps": [result.app_name],
                "redaction_confidence": result.redaction_confidence,
                "quarantine_status": "clear",
                "deletion_group_id": deletion_group_id,
                "screen_region_signal": screen_region_signal,
            },
        )
        if confirmation and str(confirmation.get("state") or "") in {"pending", "deferred"} and frame_source:
            frame_ref, _frame_owner = frame_source
            evidence_kwargs = {
                "item_key": str(confirmation.get("item_key") or ""),
                "source_activity_id": activity_id,
                "deletion_group_id": deletion_group_id,
                "source_frame_id": candidate_id,
                "captured_at": iso(captured_at),
                "display_id": display_id,
            }
            if result.status == "clear":
                promotion = promote_confirmation_evidence(
                    conn,
                    source_path=frame_ref,
                    **evidence_kwargs,
                )
                evidence_promotions.append(promotion)
            else:
                record_unavailable_evidence(
                    conn,
                    error="privacy_not_clear",
                    **evidence_kwargs,
                )
        conn.execute(
            """
            INSERT INTO search_index(entity_type, entity_id, started_at, app_name, domain, summary, keywords)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "extracted_activity",
                activity_id,
                iso(captured_at),
                result.app_name,
                result.domain,
                result.activity_summary,
                result.safe_keywords,
            ),
        )
        if raw_document_signal:
            document_signal_id = _insert_document_signal(
                conn,
                raw_document_signal,
                session_id=session_id,
                segment_id=segment_id,
                captured_at=captured_at,
                deletion_group_id=deletion_group_id,
            )
            _insert_provenance(
                conn,
                "extracted_activity",
                activity_id,
                "document_signal",
                document_signal_id,
                captured_at,
                deletion_group_id,
            )
        _insert_coverage(
            conn,
            session_id,
            segment_id,
            display_id,
            captured_at,
            "keyframe",
            clear=1,
            partial_redacted=1 if result.status == "partial_redacted" else 0,
            partial_duplicate=1 if dedupe["status"] == "partial_duplicate" else 0,
        )
        _insert_provenance(
            conn,
            "frame_signal",
            frame_signal_id,
            "raw_frame_candidate",
            candidate_id,
            captured_at,
            deletion_group_id,
        )
        _insert_provenance(
            conn,
            "extracted_activity",
            activity_id,
            "frame_signal",
            frame_signal_id,
            captured_at,
            deletion_group_id,
        )
        episode_frame_sources[activity_id] = {
            "source_path": str(frame_source[0]) if frame_source else "",
            "privacy_status": str(result.status or ""),
            "source_frame_id": candidate_id,
            "captured_at": iso(captured_at),
            "display_id": display_id,
            "deletion_group_id": deletion_group_id,
        }

    if episode_frame_sources:
        episode_times = [parse_time(source["captured_at"]) for source in episode_frame_sources.values()]
        materialize_business_episodes_for_window(
            conn,
            iso(min(episode_times) - timedelta(minutes=5)),
            iso(max(episode_times) + timedelta(minutes=5, seconds=30)),
            commit=False,
        )
        _promote_pending_episode_evidence(
            conn,
            current_activity_ids=set(episode_frame_sources),
            frame_sources=episode_frame_sources,
            evidence_promotions=evidence_promotions,
        )

    _refresh_segments(conn, segment_stats)
    conn.execute(
        """
        INSERT INTO audit_logs(id, at, action, target_type, target_id, detail)
        VALUES(?, ?, ?, ?, ?, ?)
        """,
        (
            f"audit_{uuid.uuid4().hex}",
            utc_now(),
            "ingest_events",
            "session",
            session_id,
            json.dumps(
                {
                    "count": len(event_list),
                    "clear": counts["clear"],
                    "partial_redacted": counts["partial_redacted"],
                    "blocked": counts["blocked"],
                    "duplicate_exact": counts["duplicate_exact"],
                    "partial_duplicate": counts["partial_duplicate"],
                },
                ensure_ascii=False,
            ),
        ),
    )
    if commit:
        conn.commit()
    return {
        "session_id": session_id,
        "ingested": len(event_list),
        "clear": counts["clear"],
        "partial_redacted": counts["partial_redacted"],
        "blocked": counts["blocked"],
        "review_required": counts["review_required"],
        "duplicate_exact": counts["duplicate_exact"],
        "partial_duplicate": counts["partial_duplicate"],
    }


def _promote_pending_episode_evidence(
    conn: sqlite3.Connection,
    *,
    current_activity_ids: set[str],
    frame_sources: dict[str, dict[str, str]],
    evidence_promotions: list[dict],
) -> None:
    rows = conn.execute(
        "SELECT item_key,source_activity_ids FROM activity_confirmation_items "
        "WHERE candidate_origin='business_episode' AND question_kind='occurrence' "
        "AND state IN ('pending','deferred')"
    ).fetchall()
    for row in rows:
        try:
            source_ids = [str(value) for value in json.loads(row["source_activity_ids"] or "[]") if str(value)]
        except (TypeError, json.JSONDecodeError):
            source_ids = []
        if not current_activity_ids.intersection(source_ids):
            continue
        item_key = str(row["item_key"] or "")
        representative_ids = _representative_episode_source_ids(conn, source_ids)
        schedule_item_evidence_source_release(
            conn,
            item_key=item_key,
            retain_source_activity_ids=representative_ids,
            reason="episode_evidence_representative_cap",
        )
        for source_activity_id in representative_ids:
            existing_ref = conn.execute(
                "SELECT 1 FROM activity_confirmation_evidence_refs "
                "WHERE item_key=? AND source_activity_id=? AND released_at IS NULL LIMIT 1",
                (item_key, source_activity_id),
            ).fetchone()
            if existing_ref:
                continue
            source = frame_sources.get(source_activity_id)
            if source is None:
                provenance = conn.execute(
                    "SELECT fs.id,fs.captured_at,fs.display_id,ea.deletion_group_id "
                    "FROM extracted_activities AS ea "
                    "LEFT JOIN activity_provenance AS prov "
                    "ON prov.derived_type='extracted_activity' AND prov.derived_id=ea.id "
                    "AND prov.source_type='frame_signal' "
                    "LEFT JOIN frame_signals AS fs ON fs.id=prov.source_id "
                    "WHERE ea.id=? LIMIT 1",
                    (source_activity_id,),
                ).fetchone()
                source = {
                    "source_path": "",
                    "privacy_status": "unavailable",
                    "source_frame_id": str(provenance["id"] or "") if provenance else "",
                    "captured_at": str(provenance["captured_at"] or "") if provenance else "",
                    "display_id": str(provenance["display_id"] or "") if provenance else "",
                    "deletion_group_id": str(provenance["deletion_group_id"] or "") if provenance else "",
                }
            evidence_kwargs = {
                "item_key": item_key,
                "source_activity_id": source_activity_id,
                "deletion_group_id": str(source.get("deletion_group_id") or ""),
                "source_frame_id": str(source.get("source_frame_id") or ""),
                "captured_at": str(source.get("captured_at") or ""),
                "display_id": str(source.get("display_id") or ""),
            }
            if source.get("privacy_status") == "clear" and source.get("source_path"):
                promotion = promote_confirmation_evidence(
                    conn,
                    source_path=str(source["source_path"]),
                    **evidence_kwargs,
                )
            else:
                promotion = record_unavailable_evidence(
                    conn,
                    error="source_unavailable",
                    **evidence_kwargs,
                )
            evidence_promotions.append(promotion)


def _representative_episode_source_ids(
    conn: sqlite3.Connection,
    source_activity_ids: list[str],
) -> list[str]:
    source_ids = list(dict.fromkeys(str(value) for value in source_activity_ids if str(value)))
    if len(source_ids) <= 3:
        return source_ids
    placeholders = ",".join("?" for _ in source_ids)
    rows = conn.execute(
        f"SELECT id,started_at FROM extracted_activities WHERE id IN ({placeholders})",
        tuple(source_ids),
    ).fetchall()
    observed = {str(row["id"]): str(row["started_at"] or "") for row in rows}
    ordered = sorted(source_ids, key=lambda source_id: (observed.get(source_id, ""), source_id))
    indexes = (0, (len(ordered) - 1) // 2, len(ordered) - 1)
    return [ordered[index] for index in dict.fromkeys(indexes)]


from .presentation_scope import display_read


@display_read
def timeline(conn: sqlite3.Connection, start: str | None = None, end: str | None = None) -> list[dict]:
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
        SELECT id, started_at, ended_at, activity_summary, apps, domains, evidence_refs,
               privacy_level, redaction_confidence, quarantine_status, deletion_group_id,
               dedupe_status, dedupe_note, dedupe_duplicate_lines, dedupe_retained_lines,
               dedupe_content_hash,
               (
                 SELECT fs.ocr_redacted_summary
                 FROM activity_provenance AS prov
                 JOIN frame_signals AS fs ON fs.id = prov.source_id
                 WHERE prov.derived_type = 'extracted_activity'
                   AND prov.derived_id = extracted_activities.id
                   AND prov.source_type = 'frame_signal'
                 LIMIT 1
               ) AS cleaned_text,
               (
                 SELECT fs.window_title_redacted
                 FROM activity_provenance AS prov
                 JOIN frame_signals AS fs ON fs.id = prov.source_id
                 WHERE prov.derived_type = 'extracted_activity'
                   AND prov.derived_id = extracted_activities.id
                   AND prov.source_type = 'frame_signal'
                 LIMIT 1
               ) AS cleaned_title,
               (
                 SELECT fs.screen_region_signal
                 FROM activity_provenance AS prov
                 JOIN frame_signals AS fs ON fs.id = prov.source_id
                 WHERE prov.derived_type = 'extracted_activity'
                   AND prov.derived_id = extracted_activities.id
                   AND prov.source_type = 'frame_signal'
                 LIMIT 1
               ) AS screen_region_signal,
               (
                 SELECT json_group_array(
                   json_object(
                     'document_ref', ds.file_path_hmac,
                     'file_name', ds.file_name_redacted,
                     'file_type', ds.file_type,
                     'file_modified_at', ds.file_modified_at,
                     'subsequent_mtime_event', ds.subsequent_mtime_event,
                     'visible_location_hint', ds.visible_location_hint,
                     'topic_tags', ds.topic_tags,
                     'change_summary', ds.change_summary_redacted,
                     'document_source_category', ds.document_source_category,
                     'document_source_scope', ds.document_source_scope,
                     'document_subject_scope', ds.document_subject_scope,
                     'document_memory_policy', ds.document_memory_policy,
                     'document_classification_confidence', ds.document_classification_confidence,
                     'document_classification_reasons', ds.document_classification_reasons,
                     'document_risk_flags', ds.document_risk_flags,
                     'document_parse_status', ds.document_parse_status,
                     'document_parse_method', ds.document_parse_method,
                     'redaction_confidence', ds.redaction_confidence,
                     'redaction_status', ds.redaction_status
                   )
                 )
                 FROM activity_provenance AS prov
                 JOIN document_signals AS ds ON ds.id = prov.source_id
                 WHERE prov.derived_type = 'extracted_activity'
                   AND prov.derived_id = extracted_activities.id
                   AND prov.source_type = 'document_signal'
                   AND COALESCE(ds.semantic_match, 1) = 1
               ) AS document_contexts
        FROM extracted_activities
        WHERE {' AND '.join(where)}
        ORDER BY started_at ASC
        """,
        params,
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


@display_read
def search(
    conn: sqlite3.Connection,
    query: str,
    app: str | None = None,
    start: str | None = None,
    end: str | None = None,
    *,
    confirmed_only: bool = False,
) -> list[dict]:
    if not query.strip():
        return []
    if confirmed_only:
        from .document_changes import document_authorization_lock

        # Searching the confirmed view is a public read.  Hold the lease until
        # the returned rows have been fully copied out of the database-backed
        # view so delete/reclassification cannot complete first and leak stale
        # text afterwards.
        with document_authorization_lock(conn, exclusive=False):
            return _search_confirmed_activity_view(conn, query, app=app, start=start, end=end)
    rows_by_id: dict[str, sqlite3.Row] = {}
    where = ["search_index MATCH ?"]
    from .presentation_scope import ReadView
    visible_source = "entity_type='extracted_activity' AND entity_id IN (SELECT id FROM extracted_activities) AND agi_display_visible(summary,keywords,app_name,domain)=1"
    if isinstance(conn, ReadView):
        where.append(visible_source)
    params: list[str] = [_fts_query(query)]
    if app:
        where.append("app_name = ?")
        params.append(app)
    if start:
        where.append("datetime(started_at) >= datetime(?)")
        params.append(start)
    if end:
        where.append("datetime(started_at) < datetime(?)")
        params.append(end)
    try:
        fts_rows = conn.execute(
            f"""
            SELECT entity_id, started_at, app_name, domain, summary
            FROM search_index
            WHERE {' AND '.join(where)}
            ORDER BY started_at DESC
            LIMIT 50
            """,
            params,
        ).fetchall()
        for row in fts_rows:
            rows_by_id[row["entity_id"]] = row
    except sqlite3.OperationalError:
        pass

    like = f"%{query.strip()}%"
    like_where = ["(summary LIKE ? OR keywords LIKE ? OR app_name LIKE ? OR domain LIKE ?)"]
    if isinstance(conn, ReadView):
        like_where.append(visible_source)
    like_params: list[str] = [like, like, like, like]
    if app:
        like_where.append("app_name = ?")
        like_params.append(app)
    if start:
        like_where.append("datetime(started_at) >= datetime(?)")
        like_params.append(start)
    if end:
        like_where.append("datetime(started_at) < datetime(?)")
        like_params.append(end)
    like_rows = conn.execute(
        f"""
        SELECT entity_id, started_at, app_name, domain, summary
        FROM search_index
        WHERE {' AND '.join(like_where)}
        ORDER BY started_at DESC
        LIMIT 50
        """,
        like_params,
    ).fetchall()
    for row in like_rows:
        rows_by_id[row["entity_id"]] = row

    rows = sorted(rows_by_id.values(), key=lambda row: parse_time(row["started_at"]), reverse=True)[:50]
    return [_row_to_dict(row) for row in rows]


def _search_confirmed_activity_view(
    conn: sqlite3.Connection,
    query: str,
    *,
    app: str | None,
    start: str | None,
    end: str | None,
) -> list[dict]:
    """Search only the corrected, confirmed view instead of stale index text."""
    from .activity_confirmations import confirmed_activity_view

    needle = query.strip().casefold()
    terms = [term for term in re.findall(r"[\w-]+", needle) if term]
    requested_app = str(app or "").strip().casefold()
    results: list[dict] = []
    view = confirmed_activity_view(conn, start, end)
    searchable_activities = list(view.get("trusted_activities") or [])
    for activity in searchable_activities:
        apps = [str(value) for value in activity.get("apps") or [] if str(value).strip()]
        domains = [str(value) for value in activity.get("domains") or [] if str(value).strip()]
        if requested_app and all(value.casefold() != requested_app for value in apps):
            continue
        searchable = "\n".join(
            [
                str(activity.get("activity_summary") or ""),
                str(activity.get("cleaned_text") or ""),
                str(activity.get("cleaned_title") or ""),
                *apps,
                *domains,
            ]
        ).casefold()
        if needle not in searchable and (not terms or not all(term in searchable for term in terms)):
            continue
        results.append(
            {
                "entity_id": str(activity.get("id") or ""),
                "started_at": str(activity.get("started_at") or ""),
                "app_name": apps[0] if apps else "",
                "domain": domains[0] if domains else "",
                "summary": str(activity.get("activity_summary") or ""),
                "verification_state": str(activity.get("verification_state") or "verified"),
                "confirmation_source": str(activity.get("confirmation_source") or "user"),
            }
        )
    results.sort(key=lambda row: parse_time(row["started_at"]), reverse=True)
    return results[:50]


def record_capture_coverage(
    conn: sqlite3.Connection,
    display_id: str,
    captured_at: str,
    capture_mode: str,
    *,
    clear: int = 0,
    partial_redacted: int = 0,
    review_required: int = 0,
    blocked: int = 0,
    gap: int = 0,
    duplicate_exact: int = 0,
    partial_duplicate: int = 0,
    frames_sampled: int = 1,
    frames_dropped: int = 0,
    raw_jobs_created: int = 1,
    raw_jobs_deleted: int = 1,
    degraded_reason: str | None = None,
) -> None:
    captured_dt = parse_time(captured_at)
    session_id = f"session_{uuid.uuid4().hex}"
    segment_start, segment_end = segment_bounds(captured_dt)
    segment_id = f"segment_{segment_start.strftime('%Y%m%dT%H%M%S%z')}"
    conn.execute(
        "INSERT INTO sessions(id, started_at, ended_at, status) VALUES(?, ?, ?, ?)",
        (session_id, iso(captured_dt), iso(captured_dt + timedelta(seconds=30)), "closed"),
    )
    _upsert_segment(conn, segment_id, session_id, segment_start, segment_end)
    _insert_coverage(
        conn,
        session_id,
        segment_id,
        display_id,
        captured_dt,
        capture_mode,
        clear=clear,
        partial_redacted=partial_redacted,
        review_required=review_required,
        blocked=blocked,
        gap=gap,
        duplicate_exact=duplicate_exact,
        partial_duplicate=partial_duplicate,
        frames_sampled=frames_sampled,
        frames_dropped=frames_dropped,
        raw_jobs_created=raw_jobs_created,
        raw_jobs_deleted=raw_jobs_deleted,
        degraded_reason=degraded_reason,
    )
    conn.commit()


def _upsert_segment(
    conn: sqlite3.Connection,
    segment_id: str,
    session_id: str,
    started_at: datetime,
    ended_at: datetime,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO segments(id, session_id, started_at, ended_at, summary)
        VALUES(?, ?, ?, ?, ?)
        """,
        (segment_id, session_id, iso(started_at), iso(ended_at), "pending"),
    )


def _refresh_segments(conn: sqlite3.Connection, segment_stats: dict[str, dict[str, Counter]]) -> None:
    for segment_id, stats in segment_stats.items():
        top_apps = [name for name, _ in stats["apps"].most_common(5)]
        top_domains = [name for name, _ in stats["domains"].most_common(5)]
        privacy_count = sum(stats["privacy"].values())
        summary = "、".join(top_apps[:3]) if top_apps else "无活动"
        conn.execute(
            """
            UPDATE segments
            SET top_apps = ?, top_domains = ?, summary = ?, privacy_event_count = ?
            WHERE id = ?
            """,
            (
                json.dumps(top_apps, ensure_ascii=False),
                json.dumps(top_domains, ensure_ascii=False),
                f"主要活动：{summary}",
                privacy_count,
                segment_id,
            ),
        )


def _insert_coverage(
    conn: sqlite3.Connection,
    session_id: str,
    segment_id: str,
    display_id: str,
    captured_at: datetime,
    capture_mode: str,
    clear: int = 0,
    partial_redacted: int = 0,
    review_required: int = 0,
    blocked: int = 0,
    gap: int = 0,
    duplicate_exact: int = 0,
    partial_duplicate: int = 0,
    frames_sampled: int = 1,
    frames_dropped: int = 0,
    raw_jobs_created: int = 1,
    raw_jobs_deleted: int = 1,
    degraded_reason: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO capture_coverage_ledger(
          id, session_id, segment_id, display_id, started_at, ended_at, capture_mode,
          coverage_ratio, frames_sampled, frames_dropped, raw_jobs_created, raw_jobs_deleted,
          clear_count, partial_redacted_count, review_required_count, blocked_count, gap_count,
          duplicate_exact_count, partial_duplicate_count,
          degraded_reason
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"coverage_{uuid.uuid4().hex}",
            session_id,
            segment_id,
            display_id,
            iso(captured_at),
            iso(captured_at + timedelta(seconds=30)),
            capture_mode,
            1.0 if clear or duplicate_exact else 0.0,
            frames_sampled,
            frames_dropped,
            raw_jobs_created,
            raw_jobs_deleted,
            clear,
            partial_redacted,
            review_required,
            blocked,
            gap,
            duplicate_exact,
            partial_duplicate,
            degraded_reason if degraded_reason is not None else (None if clear else "privacy_or_review_gate"),
        ),
    )


def record_duplicate_event(
    conn: sqlite3.Connection,
    display_id: str,
    captured_at: str,
    reason: str,
    *,
    duplicate_count: int = 1,
    source_activity_id: str = "",
    note: str = "",
    deletion_group_id: str | None = None,
) -> str:
    duplicate_id = _insert_duplicate_event(
        conn,
        display_id,
        parse_time(captured_at),
        reason,
        duplicate_count=duplicate_count,
        source_activity_id=source_activity_id,
        note=note,
        deletion_group_id=deletion_group_id or f"dg_{uuid.uuid4().hex}",
    )
    conn.commit()
    return duplicate_id


def _insert_duplicate_event(
    conn: sqlite3.Connection,
    display_id: str,
    captured_at: datetime,
    reason: str,
    *,
    duplicate_count: int = 1,
    source_activity_id: str = "",
    note: str = "",
    deletion_group_id: str | None = None,
) -> str:
    duplicate_id = f"duplicate_{uuid.uuid4().hex}"
    conn.execute(
        """
        INSERT INTO duplicate_events(
          id, started_at, ended_at, display_id, reason, duplicate_count,
          source_activity_id, note, deletion_group_id
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            duplicate_id,
            iso(captured_at),
            iso(captured_at + timedelta(seconds=30)),
            display_id,
            reason,
            max(1, int(duplicate_count or 1)),
            source_activity_id,
            note,
            deletion_group_id or f"dg_{uuid.uuid4().hex}",
        ),
    )
    return duplicate_id


def _apply_cleaned_text_dedupe(conn: sqlite3.Connection, display_id: str, captured_at: datetime, result) -> tuple[object, dict]:
    profile = _dedupe_profile(result.ocr_redacted_summary)
    meta = {
        "status": "unique",
        "note": "",
        "duplicate_lines": 0,
        "retained_lines": len(profile["lines"]),
        "content_hash": profile["content_hash"],
        "line_hashes": profile["line_hashes"],
        "source_activity_id": "",
    }
    if not profile["content_hash"]:
        return result, meta

    previous = _previous_dedupe_profile(conn, display_id, captured_at)
    if not previous:
        return result, meta

    previous_hash = str(previous.get("dedupe_content_hash") or "")
    if previous_hash and previous_hash == profile["content_hash"]:
        return result, {
            **meta,
            "status": "exact_duplicate",
            "note": "与上一条同显示器截屏内容完全重复，不再重复记录内容。",
            "duplicate_lines": len(profile["lines"]),
            "retained_lines": 0,
            "source_activity_id": str(previous.get("activity_id") or ""),
        }

    previous_line_hashes = _previous_line_hashes(previous)
    if not previous_line_hashes:
        return result, meta

    retained_lines: list[str] = []
    duplicate_lines = 0
    for line, line_hash in zip(profile["lines"], profile["line_hashes"]):
        if line_hash in previous_line_hashes:
            duplicate_lines += 1
        else:
            retained_lines.append(line)

    if duplicate_lines and retained_lines and len(retained_lines) < len(profile["lines"]):
        retained_text = "\n".join(retained_lines)
        note = f"已省略 {duplicate_lines} 行与上一帧重复的清洗后内容，保留 {len(retained_lines)} 行新增或变化内容。"
        updated = replace(
            result,
            ocr_redacted_summary=retained_text,
            activity_summary=_deduped_activity_summary(result.app_name, retained_text),
            safe_keywords=_deduped_safe_keywords(result.app_name, result.domain, retained_text),
        )
        return updated, {
            **meta,
            "status": "partial_duplicate",
            "note": note,
            "duplicate_lines": duplicate_lines,
            "retained_lines": len(retained_lines),
            "source_activity_id": str(previous.get("activity_id") or ""),
        }

    return result, meta


def _previous_dedupe_profile(conn: sqlite3.Connection, display_id: str, captured_at: datetime) -> dict | None:
    row = conn.execute(
        """
        SELECT ea.id AS activity_id,
               fs.ocr_redacted_summary AS cleaned_text,
               COALESCE(fs.dedupe_content_hash, ea.dedupe_content_hash, '') AS dedupe_content_hash,
               COALESCE(fs.dedupe_line_hashes, '') AS dedupe_line_hashes
        FROM frame_signals AS fs
        JOIN activity_provenance AS prov
          ON prov.source_id = fs.id
         AND prov.source_type = 'frame_signal'
         AND prov.derived_type = 'extracted_activity'
        JOIN extracted_activities AS ea
          ON ea.id = prov.derived_id
        WHERE fs.display_id = ?
          AND datetime(fs.captured_at) < datetime(?)
          AND ea.deleted_at IS NULL
          AND ea.quarantine_status = 'clear'
        ORDER BY datetime(fs.captured_at) DESC
        LIMIT 1
        """,
        (display_id, iso(captured_at)),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def _previous_line_hashes(previous: dict) -> set[str]:
    encoded = str(previous.get("dedupe_line_hashes") or "")
    if encoded:
        try:
            parsed = json.loads(encoded)
        except json.JSONDecodeError:
            parsed = []
        if isinstance(parsed, list):
            hashes = {str(item) for item in parsed if str(item)}
            if hashes:
                return hashes
    return set(_dedupe_profile(str(previous.get("cleaned_text") or ""))["line_hashes"])


def _dedupe_profile(text: str) -> dict:
    lines = _dedupe_lines(text)
    normalized = [_normalize_dedupe_line(line) for line in lines]
    line_hashes = [_hash_text(line) for line in normalized]
    content_hash = _hash_text("\n".join(normalized)) if normalized else ""
    return {"lines": lines, "line_hashes": line_hashes, "content_hash": content_hash}


def _dedupe_lines(text: str) -> list[str]:
    lines = []
    for raw in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        cleaned = raw.strip()
        if cleaned:
            lines.append(cleaned)
    return lines


def _normalize_dedupe_line(line: str) -> str:
    return " ".join(str(line or "").strip().split()).lower()


def _hash_text(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _deduped_activity_summary(app_name: str, retained_text: str) -> str:
    body = " ".join(str(retained_text or "").split())
    if len(body) > 240:
        body = body[:239].rstrip() + "…"
    return f"{app_name or 'Unknown'}：{body or '清洗后内容与上一帧重复，仅保留变化部分。'}"


def _deduped_safe_keywords(app_name: str, domain: str, retained_text: str) -> str:
    return " ".join(part for part in [str(app_name or ""), str(domain or ""), str(retained_text or "")] if part).strip()[:800]




def _nonnegative_int(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _insert_provenance(
    conn: sqlite3.Connection,
    derived_type: str,
    derived_id: str,
    source_type: str,
    source_id: str,
    captured_at: datetime,
    deletion_group_id: str,
) -> None:
    conn.execute(
        """
        INSERT INTO activity_provenance(
          id, derived_type, derived_id, source_type, source_id,
          source_time_range, deletion_group_id, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"prov_{uuid.uuid4().hex}",
            derived_type,
            derived_id,
            source_type,
            source_id,
            json.dumps(
                {
                    "from": iso(captured_at),
                    "to": iso(captured_at + timedelta(seconds=30)),
                },
                ensure_ascii=False,
            ),
            deletion_group_id,
            utc_now(),
        ),
    )


def _insert_document_signal(
    conn: sqlite3.Connection,
    signal: dict,
    *,
    session_id: str,
    segment_id: str,
    captured_at: datetime,
    deletion_group_id: str,
) -> str:
    signal_id = f"doc_signal_{uuid.uuid4().hex}"
    conn.execute(
        """
        INSERT INTO document_signals(
          id, session_id, segment_id, captured_at, app_name, file_path_hmac,
          file_name_redacted, file_type, file_modified_at, content_hash_at_capture,
          subsequent_mtime_event, visible_location_hint, topic_tags,
          change_summary_redacted, document_source_category, document_source_scope,
          document_subject_scope, document_memory_policy, document_classification_confidence,
          document_classification_reasons, document_risk_flags, document_parse_status,
          document_parse_method, privacy_flags, redaction_confidence,
          redaction_status, semantic_match, match_status, source,
          source_confidence, deletion_group_id
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            signal_id,
            session_id,
            segment_id,
            str(signal.get("captured_at") or iso(captured_at)),
            str(signal.get("app_name") or "Unknown"),
            str(signal.get("file_ref") or ""),
            str(signal.get("file_name_redacted") or ""),
            str(signal.get("file_type") or ""),
            str(signal.get("file_modified_at") or ""),
            str(signal.get("content_hash_at_capture") or ""),
            str(signal.get("subsequent_mtime_event") or ""),
            str(signal.get("visible_location_hint") or ""),
            json.dumps(signal.get("topic_tags") or [], ensure_ascii=False),
            str(signal.get("change_summary_redacted") or ""),
            str(signal.get("document_source_category") or ""),
            str(signal.get("document_source_scope") or ""),
            str(signal.get("document_subject_scope") or ""),
            str(signal.get("document_memory_policy") or ""),
            float(signal.get("document_classification_confidence") or 0.0),
            json.dumps(signal.get("document_classification_reasons") or [], ensure_ascii=False),
            json.dumps(signal.get("document_risk_flags") or [], ensure_ascii=False),
            str(signal.get("document_parse_status") or ""),
            str(signal.get("document_parse_method") or ""),
            json.dumps(signal.get("privacy_flags") or [], ensure_ascii=False),
            float(signal.get("redaction_confidence") or 0.0),
            str(signal.get("redaction_status") or "redacted"),
            1 if signal.get("semantic_match") is not False else 0,
            str(signal.get("match_status") or ("matched_text" if signal.get("semantic_match") is not False else "metadata_only")),
            str(signal.get("source") or "foreground_document"),
            float(signal.get("source_confidence") or 0.0),
            deletion_group_id,
        ),
    )
    return signal_id


def _raw_refs_confirmed_deleted(
    *,
    temp_capture_ref: str | None,
    temp_frame_ref: str | None,
    ocr_candidate_ref: str | None,
    raw_deleted_at: str | None,
    candidate_deleted_at: str | None,
) -> bool:
    return (
        not temp_capture_ref
        and not temp_frame_ref
        and not ocr_candidate_ref
        and bool(raw_deleted_at)
        and bool(candidate_deleted_at)
    )


def _row_to_dict(row: sqlite3.Row) -> dict:
    output = dict(row)
    for key in ("apps", "domains", "evidence_refs", "document_contexts", "screen_region_signal"):
        if key in output and isinstance(output[key], str):
            try:
                parsed = json.loads(output[key])
                if key == "document_contexts" and isinstance(parsed, list):
                    for item in parsed:
                        if not isinstance(item, dict):
                            continue
                        for list_key in ("topic_tags", "document_classification_reasons", "document_risk_flags"):
                            if isinstance(item.get(list_key), str):
                                try:
                                    item[list_key] = json.loads(item[list_key])
                                except json.JSONDecodeError:
                                    item[list_key] = []
                output[key] = parsed
            except json.JSONDecodeError:
                pass
    return output


def _fts_query(query: str) -> str:
    escaped = query.strip().replace('"', '""')
    return f'"{escaped}"'
