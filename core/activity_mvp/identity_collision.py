from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .app_identity import (
    IDENTITY_RULE_VERSION,
    deterministic_legacy_identity_collision,
    normalize_app_name,
    resolve_app_identity,
)
from .document_changes import document_authorization_lock
from .storage import SCHEMA_VERSION


IDENTITY_COLLISION_RULE_VERSION = f"v0.5.1-{IDENTITY_RULE_VERSION}"
IDENTITY_COLLISION_MATCH_STATUS = "identity_collision_invalidated"
PRIVATE_MANIFEST_SETTING_PREFIX = "private_identity_collision_manifest_v1:"
MANIFEST_VERSION = 1
_HISTORICAL_DOCUMENT_COLLISION_NAMES = frozenset({"obsidian", "1password", "knowledge"})


def preview_document_signal_identity_collisions(
    conn: sqlite3.Connection,
    *,
    historical_cutoff: str,
) -> dict[str, Any]:
    """Return a content-free preview; no row or manifest is written."""

    _require_schema_32(conn)
    cutoff = _validated_cutoff(historical_cutoff)
    with document_authorization_lock(conn, exclusive=False):
        entries = _collision_entries(conn, cutoff)
    return _safe_result(
        status="preview",
        entries=entries,
        manifest_hash=_manifest_hash(_manifest_payload(entries, cutoff)),
        historical_cutoff=cutoff,
        updated_count=0,
        restored_count=0,
    )


def apply_document_signal_identity_collisions(
    conn: sqlite3.Connection,
    *,
    confirm_hash: str,
    historical_cutoff: str,
) -> dict[str, Any]:
    """Invalidate only preview-bound deterministic collision rows atomically."""

    _require_clean_connection(conn, "apply")
    _require_schema_32(conn)
    expected_hash = _validated_hash(confirm_hash, field="confirm_hash")
    cutoff = _validated_cutoff(historical_cutoff)
    with document_authorization_lock(conn, exclusive=True, initialize_if_missing=True):
        try:
            conn.execute("BEGIN IMMEDIATE")
            entries = _collision_entries(conn, cutoff)
            payload = _manifest_payload(entries, cutoff)
            manifest_hash = _manifest_hash(payload)
            if manifest_hash != expected_hash:
                raise ValueError("identity_collision_preview_hash_mismatch")
            if not entries:
                conn.rollback()
                return _safe_result(
                    status="no_changes",
                    entries=entries,
                    manifest_hash=manifest_hash,
                    historical_cutoff=cutoff,
                    updated_count=0,
                    restored_count=0,
                )
            total_before = _document_signal_count(conn)
            now = _utc_now()
            manifest = {
                "manifest_hash": manifest_hash,
                "state": "applied",
                "applied_at": now,
                "reversed_at": None,
                "payload": payload,
            }
            conn.execute(
                "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (
                    _manifest_setting_key(manifest_hash),
                    _canonical_json(manifest),
                    now,
                ),
            )
            updated = 0
            for entry in entries:
                result = conn.execute(
                    "UPDATE document_signals SET semantic_match=0,match_status=? "
                    "WHERE id=? AND semantic_match IS ? AND match_status IS ?",
                    (
                        IDENTITY_COLLISION_MATCH_STATUS,
                        entry["id"],
                        entry["semantic_match"],
                        entry["match_status"],
                    ),
                )
                updated += int(result.rowcount)
            if updated != len(entries) or _document_signal_count(conn) != total_before:
                raise RuntimeError("identity_collision_quantity_conservation_failed")
            conn.commit()
            return _safe_result(
                status="applied",
                entries=entries,
                manifest_hash=manifest_hash,
                historical_cutoff=cutoff,
                updated_count=updated,
                restored_count=0,
            )
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise


def reverse_document_signal_identity_collisions(
    conn: sqlite3.Connection,
    *,
    manifest_hash: str,
) -> dict[str, Any]:
    """Restore exactly the original fields protected by one private manifest."""

    _require_clean_connection(conn, "reverse")
    _require_schema_32(conn)
    requested_hash = _validated_hash(manifest_hash, field="manifest_hash")
    with document_authorization_lock(conn, exclusive=True, initialize_if_missing=True):
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT value FROM settings WHERE key=?",
                (_manifest_setting_key(requested_hash),),
            ).fetchone()
            if row is None:
                raise ValueError("identity_collision_manifest_missing")
            manifest = _validated_manifest(_row_value(row), requested_hash)
            entries = list(manifest["payload"]["entries"])
            if manifest["state"] == "reversed":
                conn.rollback()
                return _safe_result(
                    status="already_reversed",
                    entries=entries,
                    manifest_hash=requested_hash,
                    historical_cutoff=str(manifest["payload"]["historical_cutoff"]),
                    updated_count=0,
                    restored_count=0,
                )
            if manifest["state"] != "applied":
                raise ValueError("identity_collision_manifest_state_invalid")
            total_before = _document_signal_count(conn)
            restored = 0
            for entry in entries:
                result = conn.execute(
                    "UPDATE document_signals SET semantic_match=?,match_status=? "
                    "WHERE id=? AND semantic_match=0 AND match_status=?",
                    (
                        entry["semantic_match"],
                        entry["match_status"],
                        entry["id"],
                        IDENTITY_COLLISION_MATCH_STATUS,
                    ),
                )
                restored += int(result.rowcount)
            if restored != len(entries) or _document_signal_count(conn) != total_before:
                raise RuntimeError("identity_collision_reverse_conservation_failed")
            now = _utc_now()
            manifest["state"] = "reversed"
            manifest["reversed_at"] = now
            conn.execute(
                "UPDATE settings SET value=?,updated_at=? WHERE key=?",
                (_canonical_json(manifest), now, _manifest_setting_key(requested_hash)),
            )
            conn.commit()
            return _safe_result(
                status="reversed",
                entries=entries,
                manifest_hash=requested_hash,
                historical_cutoff=str(manifest["payload"]["historical_cutoff"]),
                updated_count=0,
                restored_count=restored,
            )
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise


def _collision_entries(conn: sqlite3.Connection, historical_cutoff: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT ds.id,ds.app_name,ds.semantic_match,ds.match_status,ds.source,
               fs.app_name AS frame_app_name,fs.bundle_id AS frame_bundle_id
        FROM document_signals AS ds
        LEFT JOIN activity_provenance AS document_provenance
          ON document_provenance.source_type='document_signal'
         AND document_provenance.source_id=ds.id
         AND document_provenance.derived_type='extracted_activity'
        LEFT JOIN activity_provenance AS frame_provenance
          ON frame_provenance.derived_type='extracted_activity'
         AND frame_provenance.derived_id=document_provenance.derived_id
         AND frame_provenance.source_type='frame_signal'
        LEFT JOIN frame_signals AS fs ON fs.id=frame_provenance.source_id
        WHERE ds.source='foreground_app_or_file_url'
          AND COALESCE(ds.match_status,'')<>?
          AND datetime(ds.captured_at)<datetime(?)
        ORDER BY ds.id,fs.id
        """,
        (IDENTITY_COLLISION_MATCH_STATUS, historical_cutoff),
    ).fetchall()
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        signal_id = str(item.get("id") or "")
        group = grouped.setdefault(
            signal_id,
            {
                "id": signal_id,
                "app_name": str(item.get("app_name") or ""),
                "semantic_match": item.get("semantic_match"),
                "match_status": item.get("match_status"),
                "frame_app_names": set(),
                "frame_bundle_ids": set(),
            },
        )
        frame_app = normalize_app_name(item.get("frame_app_name"))
        frame_bundle = str(item.get("frame_bundle_id") or "").strip()
        if frame_app:
            group["frame_app_names"].add(frame_app)
        if frame_bundle:
            group["frame_bundle_ids"].add(frame_bundle)

    entries: list[dict[str, Any]] = []
    for signal_id in sorted(grouped):
        group = grouped[signal_id]
        app_name = str(group["app_name"] or "")
        normalized_name = normalize_app_name(app_name)
        frame_app_names = set(group["frame_app_names"])
        if frame_app_names and frame_app_names != {normalized_name}:
            continue
        frame_bundle_ids = set(group["frame_bundle_ids"])
        if len(frame_bundle_ids) > 1:
            continue
        bundle_id = next(iter(frame_bundle_ids), "")
        identity = resolve_app_identity(app_name, bundle_id)
        recognizable_conflict = bool(bundle_id and identity.status == "conflict")
        known_legacy_collision = bool(
            normalized_name in _HISTORICAL_DOCUMENT_COLLISION_NAMES
            and deterministic_legacy_identity_collision(app_name, bundle_id)
        )
        if not (recognizable_conflict or known_legacy_collision):
            continue
        entries.append(
            {
                "id": signal_id,
                "semantic_match": group["semantic_match"],
                "match_status": group["match_status"],
            }
        )
    return entries


def _manifest_payload(entries: list[dict[str, Any]], historical_cutoff: str) -> dict[str, Any]:
    return {
        "manifest_version": MANIFEST_VERSION,
        "rule_version": IDENTITY_COLLISION_RULE_VERSION,
        "schema_version": SCHEMA_VERSION,
        "historical_cutoff": historical_cutoff,
        "entries": sorted(entries, key=lambda item: str(item.get("id") or "")),
    }


def _validated_manifest(raw: str, expected_hash: str) -> dict[str, Any]:
    try:
        manifest = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("identity_collision_manifest_invalid") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("payload"), dict):
        raise ValueError("identity_collision_manifest_invalid")
    payload = manifest["payload"]
    entries = payload.get("entries")
    if (
        int(payload.get("manifest_version") or 0) != MANIFEST_VERSION
        or str(payload.get("rule_version") or "") != IDENTITY_COLLISION_RULE_VERSION
        or int(payload.get("schema_version") or 0) != SCHEMA_VERSION
        or not _is_valid_cutoff(payload.get("historical_cutoff"))
        or not isinstance(entries, list)
        or _manifest_hash(payload) != expected_hash
        or str(manifest.get("manifest_hash") or "") != expected_hash
    ):
        raise ValueError("identity_collision_manifest_invalid")
    entry_ids: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"id", "semantic_match", "match_status"}:
            raise ValueError("identity_collision_manifest_invalid")
        entry_id = str(entry.get("id") or "")
        if not entry_id or entry_id in entry_ids:
            raise ValueError("identity_collision_manifest_invalid")
        entry_ids.add(entry_id)
        if entry.get("semantic_match") not in {None, 0, 1}:
            raise ValueError("identity_collision_manifest_invalid")
        if entry.get("match_status") is not None and not isinstance(entry.get("match_status"), str):
            raise ValueError("identity_collision_manifest_invalid")
    return manifest


def _safe_result(
    *,
    status: str,
    entries: list[dict[str, Any]],
    manifest_hash: str,
    historical_cutoff: str,
    updated_count: int,
    restored_count: int,
) -> dict[str, Any]:
    return {
        "interface": "document_signal_identity_collisions",
        "status": status,
        "safe_status": "content_free",
        "schema_version": SCHEMA_VERSION,
        "rule_version": IDENTITY_COLLISION_RULE_VERSION,
        "historical_cutoff": historical_cutoff,
        "candidate_count": len(entries),
        "updated_count": int(updated_count),
        "restored_count": int(restored_count),
        "manifest_hash": manifest_hash,
        "deleted_count": 0,
        "attribution_changed_count": 0,
        "raw_access": False,
        "destructive_actions": False,
    }


def _require_schema_32(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    value = int(_row_value(row)) if row is not None else 0
    if value != SCHEMA_VERSION:
        raise RuntimeError("identity_collision_requires_schema_32")


def _require_clean_connection(conn: sqlite3.Connection, action: str) -> None:
    if conn.in_transaction:
        raise RuntimeError(f"identity_collision_{action}_requires_clean_connection")


def _manifest_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _manifest_setting_key(manifest_hash: str) -> str:
    return f"{PRIVATE_MANIFEST_SETTING_PREFIX}{manifest_hash}"


def _validated_hash(value: str, *, field: str) -> str:
    normalized = str(value or "").strip().casefold()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized):
        raise ValueError(f"identity_collision_{field}_invalid")
    return normalized


def _validated_cutoff(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("identity_collision_historical_cutoff_required")
    normalized = f"{raw[:-1]}+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("identity_collision_historical_cutoff_invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("identity_collision_historical_cutoff_invalid")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _is_valid_cutoff(value: Any) -> bool:
    try:
        normalized = _validated_cutoff(value)
    except ValueError:
        return False
    return normalized == str(value or "")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _row_value(row: Any) -> str:
    if isinstance(row, sqlite3.Row):
        return str(row["value"])
    return str(row[0])


def _document_signal_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS count FROM document_signals").fetchone()
    return int(row["count"] if isinstance(row, sqlite3.Row) else row[0])


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
