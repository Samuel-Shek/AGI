from __future__ import annotations

import fcntl
import json
import os
import secrets
import sqlite3
import stat
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


DEFAULT_RETENTION_POLICY = "today_and_yesterday"
RETENTION_LABEL = "today+yesterday"
RETENTION_WINDOW_DESCRIPTION = "keeps today and yesterday by local calendar day, not rolling 24 hours"
DEFAULT_RETENTION_DAYS = 1
LEGACY_RETENTION_DAYS = 30
# A pending daily cleanup is a hard privacy boundary. Keep the normal Hermes
# runtime allowance, then reserve one short minute for its final commit and
# shared-lease release before the 24-hour deadline.
RETENTION_MAINTENANCE_HARD_DEADLINE_SECONDS = 24 * 60 * 60
HERMES_HANDOFF_MAX_RUNTIME_SECONDS = 20 * 60
RETENTION_HANDOFF_RELEASE_GRACE_SECONDS = 60
RETENTION_HANDOFF_DRAIN_SECONDS = (
    HERMES_HANDOFF_MAX_RUNTIME_SECONDS + RETENTION_HANDOFF_RELEASE_GRACE_SECONDS
)
# `sessions` and `audit_logs` sit outside the activity retention cascade:
# `delete_range` never selects them and no reader ever queries them back.  Left
# alone they grow without bound, and every maintenance pass then scans a larger
# table while holding the exclusive authorization lease the card surface waits
# on.  Trimming them keeps that lease short instead of letting it drift upward
# month after month.
OPERATIONAL_SESSION_RETENTION_DAYS = 7
OPERATIONAL_AUDIT_RETENTION_DAYS = 30
# File change events keep a far longer window than screen activity on purpose.
# A row here is a filename, a change kind, a count and a structural summary --
# no screen text, no OCR, no file contents -- so the privacy cost of keeping it
# is small, while "which files did I touch last month" is the question the
# surface exists to answer.  Screenshots and OCR keep the two-day window.
FILE_CHANGE_RETENTION_DAYS = 90
AI_ATTRIBUTION_RETENTION_DAYS = 90
AI_ATTRIBUTION_LOG_NAMES = ("edits.jsonl.1", "edits.jsonl")
AI_ATTRIBUTION_LOG_MAX_BYTES = 4 * 1024 * 1024
# Delete in bounded batches so a large backlog never becomes one long writer
# transaction that blocks the daemon's own capture writes.
OPERATIONAL_CLEANUP_BATCH_ROWS = 5000
OPERATIONAL_CLEANUP_TABLES = (
    ("sessions", "started_at", OPERATIONAL_SESSION_RETENTION_DAYS),
    ("audit_logs", "at", OPERATIONAL_AUDIT_RETENTION_DAYS),
)


def cleanup_operational_records(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> dict:
    """Trim content-free operational tables the retention cascade never reaches.

    These rows carry no activity content, no evidence reference, and no public
    contract, so this deliberately runs *outside* ``document_authorization_lock``.
    Taking that exclusive lease here would lengthen precisely the freeze this
    cleanup exists to shorten.

    Comparison goes through ``datetime()`` on both sides rather than raw string
    ordering.  These tables mix offsets -- ``sessions`` stores local ``+08:00``
    stamps while ``audit_logs`` stores ``+00:00`` -- and lexical ordering would
    read ``2026-07-20T14:00:00+08:00`` as later than ``2026-07-20T11:00:00+00:00``
    even though it is five hours earlier.  Neither column is indexed, so the
    normalizing call costs nothing that a scan was not already paying.  A row
    whose stamp will not parse yields NULL and is kept, which fails closed.

    Batched deletes commit as they go, so the caller must settle its own
    transaction first -- `cleanup_retention` does exactly that before calling
    here.  An earlier version instead guarded on `in_transaction` and returned
    early, which meant every production pass skipped while still reporting
    success; the guard below is now only a backstop, and the caller-level
    integration test is what proves the pass actually runs.
    """
    if conn.in_transaction:
        return _operational_cleanup_skip("caller_transaction_open")
    deleted: dict[str, int] = {}
    cutoffs: dict[str, str] = {}
    try:
        for table, column, days in OPERATIONAL_CLEANUP_TABLES:
            cutoff = rolling_retention_cutoff(days, now).isoformat(timespec="seconds")
            cutoffs[table] = cutoff
            removed = 0
            while True:
                cursor = conn.execute(
                    f"DELETE FROM {table} WHERE rowid IN ("
                    f"SELECT rowid FROM {table} "
                    f"WHERE datetime({column}) < datetime(?) LIMIT ?)",
                    (cutoff, OPERATIONAL_CLEANUP_BATCH_ROWS),
                )
                batch = int(cursor.rowcount or 0)
                conn.commit()
                removed += batch
                if batch < OPERATIONAL_CLEANUP_BATCH_ROWS:
                    break
            deleted[table] = removed
    except sqlite3.OperationalError:
        # Losing a housekeeping pass to writer contention is harmless; the next
        # daily run retries.  Failing the whole retention result over it is not.
        if conn.in_transaction:
            conn.rollback()
        return _operational_cleanup_skip("database_busy")
    return {
        "status": "completed",
        "deleted": deleted,
        "cutoffs": cutoffs,
        "session_retention_days": OPERATIONAL_SESSION_RETENTION_DAYS,
        "audit_retention_days": OPERATIONAL_AUDIT_RETENTION_DAYS,
        "raw_access": False,
    }


def _operational_cleanup_skip(reason: str) -> dict:
    return {
        "status": "skipped",
        "reason": reason,
        "deleted": {},
        "cutoffs": {},
        "raw_access": False,
    }


def retention_cleanup_cutoff(now: datetime | None = None) -> datetime:
    """Return the UTC cutoff that keeps only today and yesterday in local time."""
    current = _aware_now(now)
    local_now = current.astimezone()
    today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    return yesterday_start.astimezone(timezone.utc)


def rolling_retention_cutoff(days: int, now: datetime | None = None) -> datetime:
    current = _aware_now(now).astimezone(timezone.utc)
    return current - timedelta(days=max(0, int(days)))


def file_change_retention_cutoff(now: datetime | None = None) -> datetime:
    """Return the UTC cutoff before which file change events expire."""
    return rolling_retention_cutoff(FILE_CHANGE_RETENTION_DAYS, now)


def cleanup_ai_attribution_receipts(
    log_dir: str | Path,
    *,
    now: datetime | None = None,
    lock_timeout_seconds: float = 1.0,
) -> dict:
    """Apply the same 90-day retention window to the private AI receipt log.

    The hook and this cleanup coordinate on an exclusive lock of the private
    log directory.  Reads and replacements are no-follow, owner-only and
    inode-checked; any unsafe shape returns a fixed blocked reason without
    exposing paths or record contents.
    """

    current = _aware_now(now).astimezone(timezone.utc)
    cutoff = current - timedelta(days=AI_ATTRIBUTION_RETENTION_DAYS)
    directory = Path(log_dir)
    directory_fd: int | None = None
    temporary_names: list[str] = []
    try:
        directory_fd = os.open(
            directory,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
    except FileNotFoundError:
        return _ai_receipt_cleanup_result("completed", cutoff=cutoff)
    except OSError:
        return _ai_receipt_cleanup_result(
            "blocked", reason="unsafe_log_directory", cutoff=cutoff
        )

    try:
        metadata = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            return _ai_receipt_cleanup_result(
                "blocked", reason="unsafe_log_directory", cutoff=cutoff
            )

        deadline = time.monotonic() + max(0.0, float(lock_timeout_seconds))
        while True:
            try:
                fcntl.flock(directory_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return _ai_receipt_cleanup_result(
                        "blocked", reason="log_busy", cutoff=cutoff
                    )
                time.sleep(0.02)

        plans: list[tuple[str, bytes, os.stat_result]] = []
        removed = 0
        retained = 0
        for name in AI_ATTRIBUTION_LOG_NAMES:
            opened = _read_private_attribution_log(directory_fd, name)
            if opened is None:
                continue
            raw, original = opened
            kept_lines: list[bytes] = []
            for line in raw.splitlines():
                if not line.strip():
                    continue
                observed = _attribution_record_time(line)
                if observed is None or observed < cutoff or observed > current + timedelta(minutes=5):
                    removed += 1
                    continue
                kept_lines.append(line)
                retained += 1
            replacement = b"".join(line + b"\n" for line in kept_lines)
            if replacement != raw:
                plans.append((name, replacement, original))

        for name, replacement, _ in plans:
            temporary = f".{name}.retention-{secrets.token_hex(8)}.tmp"
            temporary_names.append(temporary)
            descriptor = os.open(
                temporary,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=directory_fd,
            )
            try:
                os.fchmod(descriptor, 0o600)
                offset = 0
                while offset < len(replacement):
                    written = os.write(descriptor, replacement[offset:])
                    if written <= 0:
                        raise OSError("attribution receipt rewrite failed")
                    offset += written
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

        for name, _, original in plans:
            current_metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if not _same_stat_identity(original, current_metadata):
                return _ai_receipt_cleanup_result(
                    "blocked", reason="log_changed_during_cleanup", cutoff=cutoff
                )

        for index, (name, _, _) in enumerate(plans):
            temporary = temporary_names[index]
            os.replace(
                temporary,
                name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            temporary_names[index] = ""
        if plans:
            os.fsync(directory_fd)
        return _ai_receipt_cleanup_result(
            "completed",
            cutoff=cutoff,
            files_rewritten=len(plans),
            records_removed=removed,
            records_retained=retained,
        )
    except (OSError, RuntimeError, ValueError):
        return _ai_receipt_cleanup_result(
            "blocked", reason="receipt_cleanup_failed", cutoff=cutoff
        )
    finally:
        if directory_fd is not None:
            for temporary in temporary_names:
                if not temporary:
                    continue
                try:
                    os.unlink(temporary, dir_fd=directory_fd)
                except OSError:
                    pass
            try:
                fcntl.flock(directory_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(directory_fd)


def _read_private_attribution_log(
    directory_fd: int,
    name: str,
) -> tuple[bytes, os.stat_result] | None:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=directory_fd,
        )
    except FileNotFoundError:
        return None
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size < 0
            or before.st_size > AI_ATTRIBUTION_LOG_MAX_BYTES
        ):
            raise OSError("unsafe attribution receipt log")
        chunks: list[bytes] = []
        total = 0
        while total <= AI_ATTRIBUTION_LOG_MAX_BYTES:
            chunk = os.read(
                descriptor,
                min(64 * 1024, AI_ATTRIBUTION_LOG_MAX_BYTES + 1 - total),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        after = os.fstat(descriptor)
        if total > AI_ATTRIBUTION_LOG_MAX_BYTES or not _same_stat_identity(before, after):
            raise OSError("attribution receipt log changed during read")
        return b"".join(chunks), before
    finally:
        os.close(descriptor)


def _same_stat_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return bool(
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
    )


def _attribution_record_time(line: bytes) -> datetime | None:
    try:
        payload = json.loads(line.decode("utf-8"))
        value = str(payload.get("at") or "") if isinstance(payload, dict) else ""
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _ai_receipt_cleanup_result(
    status: str,
    *,
    cutoff: datetime,
    reason: str = "",
    files_rewritten: int = 0,
    records_removed: int = 0,
    records_retained: int = 0,
) -> dict:
    return {
        "status": status,
        "reason": reason,
        "retention_days": AI_ATTRIBUTION_RETENTION_DAYS,
        "cutoff": cutoff.isoformat(timespec="seconds"),
        "files_rewritten": max(0, int(files_rewritten)),
        "records_removed": max(0, int(records_removed)),
        "records_retained": max(0, int(records_retained)),
        "raw_access": False,
    }


def retention_until_for_range_start(start: str) -> datetime:
    start_dt = _parse_timestamp(start).astimezone()
    start_day = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return (start_day + timedelta(days=2)).astimezone(timezone.utc)


def retention_until_iso_for_range_start(start: str) -> str:
    return retention_until_for_range_start(start).isoformat(timespec="seconds")


def retention_handoff_admission(
    conn: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> dict:
    """Keep new Hermes leases outside the final retention deadline drain window."""
    rows = conn.execute(
        """
        SELECT key, value
        FROM daemon_state
        WHERE key IN (
          'retention_cleanup_pending_day',
          'retention_cleanup_pending_since',
          'retention_cleanup_last_completed_day'
        )
        """
    ).fetchall()
    state = {
        str(row["key"] if hasattr(row, "keys") else row[0]): str(
            (row["value"] if hasattr(row, "keys") else row[1]) or ""
        )
        for row in rows
    }
    pending_day = state.get("retention_cleanup_pending_day", "")
    completed_day = state.get("retention_cleanup_last_completed_day", "")
    pending_since = state.get("retention_cleanup_pending_since", "")
    due = bool(pending_day and pending_day != completed_day)
    base = {
        "pending": due,
        "hard_deadline_seconds": RETENTION_MAINTENANCE_HARD_DEADLINE_SECONDS,
        "max_handoff_runtime_seconds": HERMES_HANDOFF_MAX_RUNTIME_SECONDS,
        "release_grace_seconds": RETENTION_HANDOFF_RELEASE_GRACE_SECONDS,
        "drain_seconds": RETENTION_HANDOFF_DRAIN_SECONDS,
        "raw_access": False,
    }
    if not due:
        return {
            **base,
            "allowed": True,
            "reason": "",
            "deadline_at": "",
            "drain_starts_at": "",
            "remaining_seconds": None,
        }
    try:
        pending_at = _parse_timestamp(pending_since).astimezone(timezone.utc)
    except ValueError:
        return {
            **base,
            "allowed": False,
            "reason": "retention_deadline_unknown",
            "deadline_at": "",
            "drain_starts_at": "",
            "remaining_seconds": 0,
        }
    current = _aware_now(now).astimezone(timezone.utc)
    deadline = pending_at + timedelta(
        seconds=RETENTION_MAINTENANCE_HARD_DEADLINE_SECONDS
    )
    drain_starts = deadline - timedelta(seconds=RETENTION_HANDOFF_DRAIN_SECONDS)
    remaining_seconds = max(0, int((deadline - current).total_seconds()))
    allowed = current < drain_starts
    return {
        **base,
        "allowed": allowed,
        "reason": "" if allowed else "retention_deadline_imminent",
        "deadline_at": deadline.isoformat(timespec="seconds"),
        "drain_starts_at": drain_starts.isoformat(timespec="seconds"),
        "remaining_seconds": remaining_seconds,
    }


def _aware_now(now: datetime | None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current


def _parse_timestamp(value: str) -> datetime:
    if not value:
        raise ValueError("missing timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed
