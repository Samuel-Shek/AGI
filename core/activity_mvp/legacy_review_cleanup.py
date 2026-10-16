"""Private-file cleanup for retired review jobs; never submits or processes images."""
from __future__ import annotations
import os
import json
import re
import uuid
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .pipeline import parse_time, utc_now
from .private_purge import complete_private_purge, mark_private_purge_failed, pending_private_purge_ids, private_purge_failure_code, schedule_private_purge

def cleanup_retired_review_files(conn, root_dir: str | Path) -> dict:
    now = utc_now()
    config = _retired_retention_config(conn)
    overdue_count = _overdue_count(conn, now)
    retry_before = retry_retired_review_purges(conn, root_dir)
    if config["retention_policy"] == "keep_until_processed":
        return {
            "expired": 0,
            "bytes_deleted": retry_before["bytes_deleted"],
            "file_purge_failed": retry_before["failed_refs"],
            "file_purge_pending": len(pending_private_purge_ids(conn, "vlm")),
            "overdue": overdue_count,
            "retention_policy": config["retention_policy"],
        }
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    rows = conn.execute(
        """
        SELECT id,status,processing_started_at,captured_at,display_id,frame_hash,temp_frame_ref,event_json
        FROM vlm_pending_jobs
        WHERE status IN ('pending', 'processing')
          AND datetime(expires_at) <= datetime(?)
        """,
        (now,),
    ).fetchall()
    expired = 0
    purge_ids: list[str] = []
    for row in rows:
        event = _event_json(row)
        claim_started_at = (
            str(row["processing_started_at"] or "")
            if str(row["status"] or "") == "processing"
            else None
        )
        if not _mark_terminal(
            conn,
            row["id"],
            "expired_deleted",
            result_status="expired",
            dropped_reason="ttl_expired",
            last_error="ttl_expired",
            expected_status=str(row["status"] or ""),
            expected_processing_started_at=claim_started_at,
        ):
            conn.rollback()
            raise RuntimeError("vlm_expired_cleanup_claim_changed")
        schedule_private_purge(conn, "vlm", str(row["id"]))
        purge_ids.append(str(row["id"]))
        _insert_legacy_gap_event(conn, event, "vlm_backlog_ttl_expired", "VLM pending frame expired and was deleted.")
        expired += 1
    conn.commit()
    purge = purge_retired_job_files(conn, root_dir, purge_ids)
    return {
        "expired": expired,
        "bytes_deleted": retry_before["bytes_deleted"] + purge["bytes_deleted"],
        "file_purge_failed": retry_before["failed_refs"] + purge["failed_refs"],
        "file_purge_pending": purge["pending_refs"],
        "overdue": overdue_count,
        "retention_policy": config["retention_policy"],
    }


def delete_retired_jobs_for_range(
    conn,
    start: str,
    end: str,
    *,
    defer_file_delete: bool = False,
) -> dict:
    rows = conn.execute(
        """
        SELECT id, temp_frame_ref, temp_ref_bytes
        FROM vlm_pending_jobs
        WHERE datetime(captured_at) >= datetime(?)
          AND datetime(captured_at) < datetime(?)
        """,
        (start, end),
    ).fetchall()
    deleted = 0
    bytes_deleted = 0
    job_ids_pending: list[str] = []
    for row in rows:
        temp_frame_ref = str(row["temp_frame_ref"] or "")
        if defer_file_delete:
            if temp_frame_ref:
                schedule_private_purge(conn, "vlm", str(row["id"]))
                job_ids_pending.append(str(row["id"]))
        else:
            bytes_deleted += _delete_file(Path(temp_frame_ref))
        deleted += 1
    conn.execute(
        """
        DELETE FROM vlm_pending_jobs
        WHERE datetime(captured_at) >= datetime(?)
          AND datetime(captured_at) < datetime(?)
        """,
        (start, end),
    )
    return {
        "jobs": deleted,
        "bytes": bytes_deleted,
        "job_ids_pending": job_ids_pending,
    }


def purge_retired_job_files(
    conn,
    root_dir: str | Path,
    job_ids: list[str],
) -> dict[str, int]:
    """Purge deterministic pending-frame paths and settle durable ID markers."""
    bytes_deleted = 0
    failed_refs = 0
    unique_ids = sorted({str(value or "") for value in job_ids if value})
    for job_id in unique_ids:
        try:
            bytes_deleted += _delete_file(
                _private_pending_job_path(root_dir, job_id)
            )
        except OSError as exc:
            failed_refs += 1
            mark_private_purge_failed(
                conn,
                "vlm",
                job_id,
                private_purge_failure_code(exc),
            )
        else:
            complete_private_purge(conn, "vlm", job_id)
    if unique_ids:
        conn.commit()
    return {
        "bytes_deleted": bytes_deleted,
        "failed_refs": failed_refs,
        "pending_refs": len(pending_private_purge_ids(conn, "vlm")),
    }


def retry_retired_review_purges(conn, root_dir: str | Path) -> dict[str, int]:
    return purge_retired_job_files(
        conn,
        root_dir,
        pending_private_purge_ids(conn, "vlm"),
    )


def _mark_terminal(
    conn,
    job_id: str,
    status: str,
    *,
    result_status: str = "",
    dropped_reason: str = "",
    last_error: str = "",
    expected_status: str | None = None,
    expected_processing_started_at: str | None = None,
) -> bool:
    now = utc_now()
    fences: list[str] = []
    params: list[object] = [status, now, now, result_status, dropped_reason, last_error, job_id]
    if expected_status is not None:
        fences.append("status=?")
        params.append(expected_status)
    if expected_processing_started_at is not None:
        if expected_status is None:
            fences.append("status='processing'")
        fences.append("processing_started_at=?")
        params.append(expected_processing_started_at)
    fence = "" if not fences else " AND " + " AND ".join(fences)
    cursor = conn.execute(
        f"""
        UPDATE vlm_pending_jobs
        SET status = ?,
            updated_at = ?,
            completed_at = ?,
            result_status = ?,
            dropped_reason = ?,
            last_error = ?,
            temp_ref_deleted = 1,
            temp_frame_ref = '',
            temp_ref_bytes = 0,
            frame_hash = '',
            event_json = '{{}}'
        WHERE id = ?{fence}
        """,
        tuple(params),
    )
    return int(cursor.rowcount or 0) == 1


def _overdue_count(conn, now: str | None = None) -> int:
    now = now or utc_now()
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM vlm_pending_jobs
        WHERE status IN ('pending', 'processing')
          AND datetime(expires_at) <= datetime(?)
        """,
        (now,),
    ).fetchone()
    return int(row["count"] or 0) if row else 0


def _event_json(row) -> dict:
    try:
        parsed = json.loads(row_get(row, "event_json", "{}") or "{}")
    except (TypeError, json.JSONDecodeError):
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    parsed.setdefault("captured_at", str(row_get(row, "captured_at", utc_now()) or utc_now()))
    parsed.setdefault("display_id", str(row_get(row, "display_id", "unknown") or "unknown"))
    parsed.setdefault("frame_hash", str(row_get(row, "frame_hash", "") or ""))
    return parsed


def _insert_legacy_gap_event(conn, event: dict, reason: str, detail: str) -> None:
    captured_at = str(event.get("captured_at") or utc_now())
    try:
        started = parse_time(captured_at)
    except ValueError:
        started = datetime.now(timezone.utc)
        captured_at = started.isoformat(timespec="seconds")
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    ended = (started + timedelta(seconds=30)).isoformat(timespec="seconds")
    deletion_group_id = f"dg_{uuid.uuid4().hex}"
    conn.execute(
        """
        INSERT INTO gap_events(
          id, started_at, ended_at, reason, display_ids, raw_deleted_at, detail, deletion_group_id
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"gap_{uuid.uuid4().hex}",
            captured_at,
            ended,
            reason,
            json.dumps([str(event.get("display_id") or "unknown")], ensure_ascii=False),
            utc_now(),
            detail,
            deletion_group_id,
        ),
    )


def _private_pending_job_path(
    root_dir: str | Path,
    job_id: str,
) -> Path:
    if re.fullmatch(r"vlm_job_[0-9a-f]{32}", str(job_id or "")) is None:
        raise OSError("Unsafe VLM purge identity")
    root = _validated_retired_root(Path(root_dir))
    vault = root / "raw_vault"
    _validate_private_directory(vault)
    pending = vault / "vlm_pending"
    _validate_private_directory(pending)
    target = pending / f"{job_id}.png"
    _validate_private_regular_file(target, allow_missing=True)
    return target


def _validated_retired_root(root: Path) -> Path:
    supplied = root.expanduser()
    try:
        metadata = supplied.lstat()
    except FileNotFoundError as exc:
        raise OSError("VLM storage root is missing") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise OSError("Refusing VLM storage through a symbolic-link root")
    if not stat.S_ISDIR(metadata.st_mode):
        raise NotADirectoryError("VLM storage root is not a directory")
    if metadata.st_uid != os.getuid():
        raise PermissionError("VLM storage root is not owned by the current user")

    resolved = supplied.resolve(strict=True)
    resolved_metadata = resolved.lstat()
    if not stat.S_ISDIR(resolved_metadata.st_mode):
        raise NotADirectoryError("Resolved VLM storage root is not a directory")
    if resolved_metadata.st_uid != os.getuid():
        raise PermissionError("Resolved VLM storage root is not owned by the current user")
    return resolved


def _validate_private_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise OSError("Private VLM directory is missing") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise OSError("Refusing private storage through a symbolic link")
    if not stat.S_ISDIR(metadata.st_mode):
        raise NotADirectoryError("Private VLM storage is not a directory")
    if metadata.st_uid != os.getuid():
        raise PermissionError("Private VLM directory is not owned by the current user")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise PermissionError("Private VLM directory permissions must be 0700")


def _validate_private_regular_file(
    path: Path,
    *,
    allow_missing: bool = False,
) -> os.stat_result | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return None
        raise
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise OSError("Private VLM target must be a regular non-symlink file")
    if metadata.st_uid != os.getuid():
        raise PermissionError("Private VLM target is not owned by the current user")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise PermissionError("Private VLM target permissions are not private")
    return metadata


def _delete_file(path: Path) -> int:
    if path == Path(""):
        return 0
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return 0
    if not stat.S_ISREG(metadata.st_mode):
        if stat.S_ISLNK(metadata.st_mode):
            raise OSError("Refusing to purge a symbolic link")
        raise IsADirectoryError("Refusing to purge a non-regular file")
    try:
        path.unlink()
    except FileNotFoundError:
        # Another successful deleter won the race; this attempt deleted zero.
        return 0
    return int(metadata.st_size)


def _get_setting(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else default


def row_get(row, key: str, default=None):
    if row is None:
        return default
    try:
        if hasattr(row, "keys") and key not in row.keys():
            return default
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def _retired_retention_config(conn) -> dict:
    # Retired feature: only read the old retention policy for private-file cleanup.
    return {"retention_policy": _get_setting(conn, "vlm_backlog_retention_policy", "ttl_delete")}
