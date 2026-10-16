from __future__ import annotations

import json
import re
import sqlite3

from .pipeline import parse_time, utc_now


PRIVATE_PURGE_RETRY_PREFIX = "private_purge_retry:"
_RESOURCE_PATTERNS = {
    "ha": re.compile(r"^ha_pkg_[0-9a-f]{32}$"),
    "vlm": re.compile(r"^vlm_job_[0-9a-f]{32}$"),
}
_FAILURE_CODES = {
    "delete_failed",
    "is_directory",
    "os_error",
    "permission_denied",
}


def schedule_private_purge(
    conn: sqlite3.Connection,
    kind: str,
    resource_id: str,
) -> None:
    """Persist a content-free retry token inside the caller's transaction."""
    kind, resource_id = _validated_identity(kind, resource_id)
    key = _retry_key(kind, resource_id)
    existing = _retry_payload(conn, key)
    now = utc_now()
    payload = {
        "kind": kind,
        "resource_id": resource_id,
        "state": "pending",
        "attempt_count": _safe_attempt_count(existing.get("attempt_count")),
        "first_pending_at": _safe_timestamp(
            existing.get("first_pending_at"),
            now,
        ),
        "last_attempt_at": _safe_timestamp(
            existing.get("last_attempt_at"),
            "",
        ),
        "last_error": _safe_error_code(existing.get("last_error")),
    }
    conn.execute(
        """
        INSERT INTO daemon_state(key, value, updated_at)
        VALUES(?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
          value = excluded.value,
          updated_at = excluded.updated_at
        """,
        (key, json.dumps(payload, ensure_ascii=True, sort_keys=True), now),
    )


def mark_private_purge_failed(
    conn: sqlite3.Connection,
    kind: str,
    resource_id: str,
    error_code: str,
) -> None:
    """Keep the retry token and record only a fixed, content-free error code."""
    kind, resource_id = _validated_identity(kind, resource_id)
    error_code = error_code if error_code in _FAILURE_CODES else "delete_failed"
    key = _retry_key(kind, resource_id)
    existing = _retry_payload(conn, key)
    now = utc_now()
    payload = {
        "kind": kind,
        "resource_id": resource_id,
        "state": "pending",
        "attempt_count": _safe_attempt_count(existing.get("attempt_count")) + 1,
        "first_pending_at": _safe_timestamp(
            existing.get("first_pending_at"),
            now,
        ),
        "last_attempt_at": now,
        "last_error": error_code,
    }
    conn.execute(
        """
        INSERT INTO daemon_state(key, value, updated_at)
        VALUES(?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
          value = excluded.value,
          updated_at = excluded.updated_at
        """,
        (key, json.dumps(payload, ensure_ascii=True, sort_keys=True), now),
    )


def complete_private_purge(
    conn: sqlite3.Connection,
    kind: str,
    resource_id: str,
) -> None:
    kind, resource_id = _validated_identity(kind, resource_id)
    conn.execute(
        "DELETE FROM daemon_state WHERE key = ?",
        (_retry_key(kind, resource_id),),
    )


def pending_private_purge_ids(
    conn: sqlite3.Connection,
    kind: str | None = None,
) -> list[str]:
    if kind is not None and kind not in _RESOURCE_PATTERNS:
        raise ValueError("Unsupported private purge kind")
    prefix = (
        f"{PRIVATE_PURGE_RETRY_PREFIX}{kind}:"
        if kind is not None
        else PRIVATE_PURGE_RETRY_PREFIX
    )
    rows = conn.execute(
        "SELECT key FROM daemon_state WHERE key GLOB ? ORDER BY key",
        (f"{prefix}*",),
    ).fetchall()
    output: list[str] = []
    for row in rows:
        key = str(row["key"] if isinstance(row, sqlite3.Row) else row[0])
        parts = key.split(":", 2)
        if len(parts) != 3:
            continue
        marker_kind, resource_id = parts[1], parts[2]
        if kind is not None and marker_kind != kind:
            continue
        pattern = _RESOURCE_PATTERNS.get(marker_kind)
        if pattern is not None and pattern.fullmatch(resource_id):
            output.append(resource_id)
    return output


def private_purge_status(conn: sqlite3.Connection) -> dict:
    ha_ids = pending_private_purge_ids(conn, "ha")
    vlm_ids = pending_private_purge_ids(conn, "vlm")
    return {
        "status": "pending" if ha_ids or vlm_ids else "ok",
        "pending_count": len(ha_ids) + len(vlm_ids),
        "ha_pending_count": len(ha_ids),
        "legacy_review_pending_count": len(vlm_ids),
        "raw_access": False,
    }


def private_purge_failure_code(exc: BaseException) -> str:
    if isinstance(exc, PermissionError):
        return "permission_denied"
    if isinstance(exc, IsADirectoryError):
        return "is_directory"
    if isinstance(exc, OSError):
        return "os_error"
    return "delete_failed"


def _validated_identity(kind: str, resource_id: str) -> tuple[str, str]:
    kind = str(kind or "")
    resource_id = str(resource_id or "")
    pattern = _RESOURCE_PATTERNS.get(kind)
    if pattern is None or pattern.fullmatch(resource_id) is None:
        raise ValueError("Invalid private purge retry identity")
    return kind, resource_id


def _retry_key(kind: str, resource_id: str) -> str:
    return f"{PRIVATE_PURGE_RETRY_PREFIX}{kind}:{resource_id}"


def _retry_payload(conn: sqlite3.Connection, key: str) -> dict:
    row = conn.execute(
        "SELECT value FROM daemon_state WHERE key = ?",
        (key,),
    ).fetchone()
    if row is None:
        return {}
    value = row["value"] if isinstance(row, sqlite3.Row) else row[0]
    try:
        payload = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _safe_attempt_count(value: object) -> int:
    try:
        return max(0, min(1_000_000, int(value or 0)))
    except (TypeError, ValueError):
        return 0


def _safe_timestamp(value: object, fallback: str) -> str:
    text = str(value or "")
    if not text:
        return fallback
    try:
        parse_time(text)
    except (TypeError, ValueError):
        return fallback
    return text


def _safe_error_code(value: object) -> str:
    text = str(value or "")
    return text if text in _FAILURE_CODES else ""
