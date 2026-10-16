"""An explicit, bounded screen grant; reading status never extends it."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import secrets
import time

LEASE_SECONDS = 1800
LEASE_KEY = "screen_capture_lease"
_monotonic_limits: dict[tuple[str, str], float] = {}


def _setting(conn, key, default=""):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return str(row[0]) if row else default


def _put(conn, key, value, stamp):
    conn.execute("INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                 (key, value, stamp))


def capture_lease_snapshot(conn, *, now=None, monotonic_now=None):
    current = now or datetime.now(timezone.utc)
    raw = _setting(conn, LEASE_KEY)
    enabled = _setting(conn, "screen_capture_enabled", "false") == "true"
    result = {"policy": "manual_30_minutes", "enabled": False, "expires_at": "",
              "remaining_seconds": 0, "reason_code": "screen_capture_disabled", "raw_access": False}
    if not enabled:
        return result
    try:
        lease = json.loads(raw)
        started = datetime.fromisoformat(lease["started_at"])
        expires = datetime.fromisoformat(lease["expires_at"])
        if not started.tzinfo or not expires.tzinfo or expires - started != timedelta(seconds=LEASE_SECONDS):
            raise ValueError("invalid lease")
        if current < started - timedelta(seconds=5):
            raise ValueError("clock regression")
        remaining = (expires - current).total_seconds()
        # Bound wall-clock rollback during a running process. The persisted UTC
        # expiry also includes sleep and is never replaced during a restart.
        if now is None or monotonic_now is not None:
            tick = time.monotonic() if monotonic_now is None else monotonic_now
            db = str(conn.execute("PRAGMA database_list").fetchone()[2])
            key = (db, str(lease["id"]))
            if key not in _monotonic_limits:
                if len(_monotonic_limits) > 128:
                    _monotonic_limits.clear()
                _monotonic_limits[key] = tick + max(0, min(LEASE_SECONDS, remaining))
            remaining = min(remaining, _monotonic_limits[key] - tick)
        result.update(expires_at=expires.isoformat(), remaining_seconds=max(0, int(remaining)))
        result["enabled"] = remaining > 0
        result["reason_code"] = "" if remaining > 0 else "screen_capture_lease_expired"
    except (ValueError, TypeError, KeyError, OverflowError):
        result["reason_code"] = "screen_capture_lease_invalid"
    return result


def configure_capture_lease(conn, *, enabled: bool, now=None):
    current = now or datetime.now(timezone.utc)
    stamp = current.isoformat()
    lease = {"id": secrets.token_hex(16), "started_at": stamp,
             "expires_at": (current + timedelta(seconds=LEASE_SECONDS)).isoformat()} if enabled else {}
    _put(conn, LEASE_KEY, json.dumps(lease, separators=(",", ":")), stamp)
    _put(conn, "screen_capture_enabled", "true" if enabled else "false", stamp)
    return capture_lease_snapshot(conn, now=current)


def expire_capture_lease(conn, *, now=None):
    """Writer-only enforcement. Serialize against a concurrent new owner grant."""
    if conn.in_transaction:
        raise RuntimeError("capture lease requires a fresh transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = capture_lease_snapshot(conn, now=now)
        if not result["enabled"] and _setting(conn, "screen_capture_enabled") == "true":
            _put(conn, "screen_capture_enabled", "false", (now or datetime.now(timezone.utc)).isoformat())
        conn.commit()
        return result
    except BaseException:
        conn.rollback()
        raise
