"""Prospective retention for confirmation images, never an archive migration."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

POLICY_SINCE = "confirmation_evidence_policy_since"
CAP_BYTES = 1024 ** 3
RETENTION_DAYS = 7


def _time(value):
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp.astimezone(timezone.utc) if stamp.tzinfo else None
    except (ValueError, TypeError):
        return None


def ensure_evidence_policy(conn, *, now=None):
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    conn.execute("INSERT OR IGNORE INTO settings(key,value,updated_at) VALUES(?,?,?)",
                 (POLICY_SINCE, stamp, stamp))


def evidence_policy(conn):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (POLICY_SINCE,)).fetchone()
    return _time(row[0]) if row else None


def evidence_expired(row, policy, *, now=None):
    created = _time(row["created_at"])
    if policy is None:
        return False
    if created is None:
        return True
    if created < policy:
        return False
    captured = _time(row["captured_at"])
    current = now or datetime.now(timezone.utc)
    return captured is None or captured > current + timedelta(seconds=5) or captured + timedelta(days=RETENTION_DAYS) <= current


def admission_reason(conn, *, captured_at, byte_size, existing=None, now=None):
    policy = evidence_policy(conn)
    if policy is None:
        return ""
    current = now or datetime.now(timezone.utc)
    if existing is not None:
        created = _time(existing["created_at"])
        if created is not None and created < policy:
            return ""
        if str(existing["delete_reason"] or "") == "confirmation_image_expired":
            return "confirmation_image_expired"
    candidate = {"created_at": current.isoformat(), "captured_at": captured_at}
    if evidence_expired(candidate, policy, now=current):
        return "confirmation_image_expired"
    if existing is not None and str(existing["state"]) == "available":
        return ""
    used = conn.execute("SELECT COALESCE(SUM(byte_size),0) FROM activity_confirmation_evidence_files "
                        "WHERE state IN ('available','delete_pending') AND created_at>=?",
                        (policy.isoformat(),)).fetchone()[0]
    return "confirmation_image_capacity_reached" if int(used) + byte_size > CAP_BYTES else ""


def schedule_expired_evidence(conn, *, now=None):
    from .confirmation_evidence import schedule_evidence_file_release
    policy = evidence_policy(conn)
    if policy is None:
        return 0
    rows = conn.execute("SELECT evidence_id,created_at,captured_at FROM activity_confirmation_evidence_files "
                        "WHERE state='available' AND created_at>=?", (policy.isoformat(),)).fetchall()
    ids = [str(row["evidence_id"]) for row in rows if evidence_expired(row, policy, now=now)]
    for offset in range(0, len(ids), 100):
        schedule_evidence_file_release(conn, ids[offset:offset+100], reason="confirmation_image_expired")
    return len(ids)


def evidence_retention_status(conn):
    policy = evidence_policy(conn)
    if policy is None:
        return {"enabled": False, "raw_access": False}
    row = conn.execute("SELECT COALESCE(SUM(byte_size),0),COUNT(*) FROM activity_confirmation_evidence_files "
                       "WHERE state IN ('available','delete_pending') AND created_at>=?",
                       (policy.isoformat(),)).fetchone()
    pending = conn.execute("SELECT COUNT(*) FROM activity_confirmation_evidence_files WHERE state='delete_pending' AND delete_reason='confirmation_image_expired'").fetchone()[0]
    errors = conn.execute("SELECT COUNT(*) FROM activity_confirmation_evidence_files WHERE last_error='delete_failed' AND created_at>=?", (policy.isoformat(),)).fetchone()[0]
    return {"enabled": True, "expired_delete_pending": int(pending), "deletion_errors": int(errors), "new_images_since": policy.isoformat(), "maximum_days": RETENTION_DAYS,
            "capacity_bytes": CAP_BYTES, "new_images_bytes": int(row[0]), "new_images_count": int(row[1]),
            "capacity_reached": int(row[0]) >= CAP_BYTES, "legacy_images_preserved": True, "raw_access": False}
