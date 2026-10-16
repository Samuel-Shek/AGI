from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .evidence_retention import (admission_reason, evidence_expired, evidence_policy, schedule_expired_evidence)


INTERNAL_FRAME_REF = "_local_confirmation_frame_ref"
INTERNAL_FRAME_OWNER = "_local_confirmation_frame_owner"
EVIDENCE_DIRECTORY = "confirmation_evidence.noindex"
BACKUP_EXCLUSION_XATTR = "com.apple.metadata:com_apple_backup_excludeItem"
BACKUP_EXCLUSION_VALUE = b"com.apple.backupd"
ORPHAN_GRACE_SECONDS = 5 * 60


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def database_root(conn: sqlite3.Connection) -> Path:
    rows = conn.execute("PRAGMA database_list").fetchall()
    for row in rows:
        name = str(row[1] if not hasattr(row, "keys") else row["name"])
        filename = str(row[2] if not hasattr(row, "keys") else row["file"])
        if name == "main" and filename:
            return Path(filename).expanduser().resolve().parent
    raise RuntimeError("confirmation_evidence_database_path_unavailable")


def evidence_root(root: str | Path) -> Path:
    return Path(root).expanduser().resolve() / EVIDENCE_DIRECTORY


def ensure_evidence_root(root: str | Path) -> Path:
    directory = evidence_root(root)
    _ensure_private_directory(directory)
    marker = directory / ".metadata_never_index"
    _ensure_private_regular_file(marker)
    lock = directory / ".evidence.lock"
    _ensure_private_regular_file(lock)
    if sys.platform == "darwin":
        try:
            subprocess.run(
                [
                    "/usr/bin/xattr",
                    "-w",
                    BACKUP_EXCLUSION_XATTR,
                    BACKUP_EXCLUSION_VALUE.decode("ascii"),
                    str(directory),
                ],
                check=True,
                timeout=5,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            checked = subprocess.run(
                ["/usr/bin/xattr", "-p", BACKUP_EXCLUSION_XATTR, str(directory)],
                check=True,
                timeout=5,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            applied = checked.stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("confirmation_evidence_backup_exclusion_failed") from exc
        if applied != BACKUP_EXCLUSION_VALUE:
            raise RuntimeError("confirmation_evidence_backup_exclusion_failed")
    return directory


def _ensure_private_directory(directory: Path) -> None:
    parent = directory.parent.resolve(strict=True)
    if directory.exists() or directory.is_symlink():
        details = directory.lstat()
        if (
            stat.S_ISLNK(details.st_mode)
            or not stat.S_ISDIR(details.st_mode)
            or details.st_uid != os.getuid()
        ):
            raise RuntimeError("confirmation_evidence_directory_unsafe")
    else:
        directory.mkdir(mode=0o700)
    details = directory.lstat()
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
        or directory.resolve(strict=True).parent != parent
    ):
        raise RuntimeError("confirmation_evidence_directory_unsafe")
    directory.chmod(0o700)


def _ensure_private_regular_file(path: Path) -> None:
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
            raise RuntimeError("confirmation_evidence_control_file_unsafe")
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def _existing_private_directory(directory: Path) -> bool:
    try:
        details = directory.lstat()
        return bool(
            stat.S_ISDIR(details.st_mode)
            and not stat.S_ISLNK(details.st_mode)
            and details.st_uid == os.getuid()
            and stat.S_IMODE(details.st_mode) == 0o700
            and directory.resolve(strict=True).parent == directory.parent.resolve(strict=True)
        )
    except OSError:
        return False


def _open_private_lock(lock_path: Path) -> int:
    flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags)
    details = os.fstat(descriptor)
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != 0o600
    ):
        os.close(descriptor)
        raise RuntimeError("confirmation_evidence_lock_unsafe")
    return descriptor


@contextmanager
def evidence_lock(
    root: str | Path,
    *,
    exclusive: bool,
    create: bool = True,
) -> Iterator[Path | None]:
    directory = evidence_root(root)
    if create:
        directory = ensure_evidence_root(root)
    elif not _existing_private_directory(directory):
        yield None
        return
    lock_path = directory / ".evidence.lock"
    try:
        descriptor = _open_private_lock(lock_path)
    except (OSError, RuntimeError):
        if create:
            raise
        yield None
        return
    with os.fdopen(descriptor, "r+b", closefd=True) as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield directory
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def promote_confirmation_evidence(
    conn: sqlite3.Connection,
    *,
    item_key: str,
    source_activity_id: str,
    deletion_group_id: str,
    source_frame_id: str,
    captured_at: str,
    display_id: str,
    source_path: str | Path,
) -> dict[str, Any]:
    root = database_root(conn)
    source = Path(source_path)
    if not _valid_capture_source(root, source):
        return record_unavailable_evidence(
            conn,
            item_key=item_key,
            source_activity_id=source_activity_id,
            deletion_group_id=deletion_group_id,
            source_frame_id=source_frame_id,
            captured_at=captured_at,
            display_id=display_id,
            error="source_unavailable",
        )
    copied_destination: Path | None = None
    destination_was_new = False
    try:
        digest, byte_size = _hash_file(source)
        evidence_id = "confirm_ev_" + hashlib.sha256(
            f"{source_frame_id}|{digest}".encode("utf-8")
        ).hexdigest()[:32]
        now = utc_now()
        with evidence_lock(root, exclusive=True) as directory:
            if directory is None:
                raise RuntimeError("confirmation_evidence_directory_unavailable")
            existing = conn.execute(
                "SELECT * FROM activity_confirmation_evidence_files WHERE evidence_id=?",
                (evidence_id,),
            ).fetchone()
            rejection = admission_reason(conn, captured_at=captured_at, byte_size=byte_size, existing=existing)
            if rejection:
                if existing is not None:
                    return {"status": "unavailable", "error": rejection, "created_new": False}
                return record_unavailable_evidence(
                    conn, item_key=item_key, source_activity_id=source_activity_id,
                    deletion_group_id=deletion_group_id, source_frame_id=source_frame_id,
                    captured_at=captured_at, display_id=display_id, error=rejection,
                )
            if existing and str(existing["state"] or "") == "available":
                existing_path = _verified_evidence_path(
                    directory,
                    existing,
                    verify_content=True,
                )
                if existing_path is not None:
                    _insert_reference(
                        conn,
                        evidence_id=evidence_id,
                        item_key=item_key,
                        source_activity_id=source_activity_id,
                        deletion_group_id=deletion_group_id,
                        created_at=now,
                    )
                    return {
                        "status": "available",
                        "evidence_id": evidence_id,
                        "file_name": str(existing["file_name"] or ""),
                        "content_sha256": str(existing["content_sha256"] or ""),
                        "byte_size": int(existing["byte_size"] or 0),
                        "created_new": False,
                    }
                raise RuntimeError("confirmation_evidence_existing_file_invalid")

            filename = f"{evidence_id}.png"
            destination = directory / filename
            destination_was_new = not destination.exists() and not destination.is_symlink()
            if not destination_was_new:
                raise RuntimeError("confirmation_evidence_destination_exists")
            temporary_name = ""
            try:
                with tempfile.NamedTemporaryFile(
                    dir=directory,
                    prefix=f".{evidence_id}-",
                    suffix=".tmp",
                    delete=False,
                ) as temporary:
                    temporary_name = temporary.name
                    _copy_private_capture_source(source, temporary)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                temporary_path = Path(temporary_name)
                temporary_path.chmod(0o600)
                copied_digest, copied_size, png_signature = _hash_evidence_file(temporary_path)
                if not png_signature or copied_digest != digest or copied_size != byte_size:
                    raise OSError("confirmation_evidence_source_changed")
                os.link(temporary_path, destination, follow_symlinks=False)
                temporary_path.unlink()
                copied_destination = destination
            except Exception:
                if temporary_name:
                    Path(temporary_name).unlink(missing_ok=True)
                destination.unlink(missing_ok=True)
                raise

            conn.execute(
                """
                INSERT INTO activity_confirmation_evidence_files(
                  evidence_id,source_frame_id,captured_at,display_id,file_name,
                  content_sha256,byte_size,state,backup_excluded,last_error,
                  created_at,updated_at,deleted_at,delete_reason
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(evidence_id) DO UPDATE SET
                  file_name=excluded.file_name,content_sha256=excluded.content_sha256,
                  byte_size=excluded.byte_size,state='available',backup_excluded=1,
                  last_error='',updated_at=excluded.updated_at,deleted_at=NULL,delete_reason=''
                """,
                (
                    evidence_id,
                    source_frame_id,
                    captured_at,
                    display_id,
                    filename,
                    digest,
                    byte_size,
                    "available",
                    1,
                    "",
                    now,
                    now,
                    None,
                    "",
                ),
            )
            _insert_reference(
                conn,
                evidence_id=evidence_id,
                item_key=item_key,
                source_activity_id=source_activity_id,
                deletion_group_id=deletion_group_id,
                created_at=now,
            )
            return {
                "status": "available",
                "evidence_id": evidence_id,
                "file_name": filename,
                "content_sha256": digest,
                "byte_size": byte_size,
                "created_new": True,
            }
    except Exception:
        if copied_destination is not None and destination_was_new:
            try:
                with evidence_lock(root, exclusive=True, create=False) as directory:
                    if directory is not None:
                        candidate = _evidence_path(directory, copied_destination.name)
                        if candidate is not None:
                            candidate.unlink(missing_ok=True)
            except OSError:
                pass
        return record_unavailable_evidence(
            conn,
            item_key=item_key,
            source_activity_id=source_activity_id,
            deletion_group_id=deletion_group_id,
            source_frame_id=source_frame_id,
            captured_at=captured_at,
            display_id=display_id,
            error="promotion_failed",
        )


def record_unavailable_evidence(
    conn: sqlite3.Connection,
    *,
    item_key: str,
    source_activity_id: str,
    deletion_group_id: str,
    source_frame_id: str,
    captured_at: str,
    display_id: str,
    error: str,
) -> dict[str, Any]:
    now = utc_now()
    evidence_id = "confirm_ev_unavailable_" + uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO activity_confirmation_evidence_files(
          evidence_id,source_frame_id,captured_at,display_id,file_name,
          content_sha256,byte_size,state,backup_excluded,last_error,
          created_at,updated_at,deleted_at,delete_reason
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            evidence_id,
            source_frame_id,
            captured_at,
            display_id,
            "",
            "",
            0,
            "unavailable",
            0,
            _safe_error(error),
            now,
            now,
            None,
            "",
        ),
    )
    _insert_reference(
        conn,
        evidence_id=evidence_id,
        item_key=item_key,
        source_activity_id=source_activity_id,
        deletion_group_id=deletion_group_id,
        created_at=now,
    )
    return {
        "status": "unavailable",
        "evidence_id": evidence_id,
        "file_name": "",
        "content_sha256": "",
        "byte_size": 0,
        "created_new": False,
    }


def _insert_reference(
    conn: sqlite3.Connection,
    *,
    evidence_id: str,
    item_key: str,
    source_activity_id: str,
    deletion_group_id: str,
    created_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO activity_confirmation_evidence_refs(
          evidence_id,item_key,source_activity_id,deletion_group_id,created_at,released_at,release_reason
        ) VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(evidence_id,item_key) DO UPDATE SET
          source_activity_id=excluded.source_activity_id,
          deletion_group_id=excluded.deletion_group_id,
          released_at=NULL,release_reason=''
        """,
        (evidence_id, item_key, source_activity_id, deletion_group_id, created_at, None, ""),
    )


def schedule_evidence_release(
    conn: sqlite3.Connection,
    item_keys: list[str] | tuple[str, ...],
    *,
    reason: str,
) -> int:
    keys = _unique_safe_ids(item_keys)
    if not keys:
        return 0
    placeholders = ",".join("?" for _ in keys)
    evidence_ids = [
        str(row["evidence_id"])
        for row in conn.execute(
            f"SELECT DISTINCT evidence_id FROM activity_confirmation_evidence_refs "
            f"WHERE item_key IN ({placeholders}) AND released_at IS NULL",
            tuple(keys),
        ).fetchall()
    ]
    if not evidence_ids:
        return 0
    now = utc_now()
    conn.execute(
        f"UPDATE activity_confirmation_evidence_refs SET released_at=?,release_reason=? "
        f"WHERE item_key IN ({placeholders}) AND released_at IS NULL",
        (now, _safe_error(reason), *keys),
    )
    scheduled = 0
    for evidence_id in evidence_ids:
        active = conn.execute(
            "SELECT 1 FROM activity_confirmation_evidence_refs "
            "WHERE evidence_id=? AND released_at IS NULL LIMIT 1",
            (evidence_id,),
        ).fetchone()
        if active:
            continue
        updated = conn.execute(
            "UPDATE activity_confirmation_evidence_files "
            "SET state='delete_pending',delete_reason=?,updated_at=?,last_error='' "
            "WHERE evidence_id=? AND state='available'",
            (_safe_error(reason), now, evidence_id),
        )
        scheduled += int(updated.rowcount or 0)
        conn.execute(
            "UPDATE activity_confirmation_evidence_files "
            "SET state='deleted',deleted_at=?,delete_reason=?,updated_at=?,last_error='' "
            "WHERE evidence_id=? AND state='unavailable'",
            (now, _safe_error(reason), now, evidence_id),
        )
    return scheduled


def schedule_evidence_file_release(
    conn: sqlite3.Connection,
    evidence_ids: list[str] | tuple[str, ...],
    *,
    reason: str,
) -> int:
    """Release every active question reference for specific evidence files."""
    ids = _unique_safe_ids(evidence_ids)
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    now = utc_now()
    conn.execute(
        f"UPDATE activity_confirmation_evidence_refs SET released_at=?,release_reason=? "
        f"WHERE evidence_id IN ({placeholders}) AND released_at IS NULL",
        (now, _safe_error(reason), *ids),
    )
    scheduled = 0
    for evidence_id in ids:
        active = conn.execute(
            "SELECT 1 FROM activity_confirmation_evidence_refs "
            "WHERE evidence_id=? AND released_at IS NULL LIMIT 1",
            (evidence_id,),
        ).fetchone()
        if active:
            continue
        updated = conn.execute(
            "UPDATE activity_confirmation_evidence_files "
            "SET state='delete_pending',delete_reason=?,updated_at=?,last_error='' "
            "WHERE evidence_id=? AND state='available'",
            (_safe_error(reason), now, evidence_id),
        )
        scheduled += int(updated.rowcount or 0)
        conn.execute(
            "UPDATE activity_confirmation_evidence_files "
            "SET state='deleted',deleted_at=?,delete_reason=?,updated_at=?,last_error='' "
            "WHERE evidence_id=? AND state='unavailable'",
            (now, _safe_error(reason), now, evidence_id),
        )
    return scheduled


def schedule_item_evidence_source_release(
    conn: sqlite3.Connection,
    *,
    item_key: str,
    retain_source_activity_ids: list[str] | tuple[str, ...],
    reason: str,
) -> int:
    """Release one item's non-representative refs without touching shared refs."""
    safe_item_key = str(item_key or "").strip()
    if not safe_item_key:
        return 0
    retained = set(_unique_safe_ids(retain_source_activity_ids))
    rows = conn.execute(
        "SELECT evidence_id,source_activity_id FROM activity_confirmation_evidence_refs "
        "WHERE item_key=? AND released_at IS NULL",
        (safe_item_key,),
    ).fetchall()
    release_ids = [
        str(row["evidence_id"])
        for row in rows
        if str(row["source_activity_id"] or "") not in retained
    ]
    if not release_ids:
        return 0
    placeholders = ",".join("?" for _ in release_ids)
    now = utc_now()
    conn.execute(
        f"UPDATE activity_confirmation_evidence_refs SET released_at=?,release_reason=? "
        f"WHERE item_key=? AND released_at IS NULL AND evidence_id IN ({placeholders})",
        (now, _safe_error(reason), safe_item_key, *release_ids),
    )
    scheduled = 0
    for evidence_id in release_ids:
        active = conn.execute(
            "SELECT 1 FROM activity_confirmation_evidence_refs "
            "WHERE evidence_id=? AND released_at IS NULL LIMIT 1",
            (evidence_id,),
        ).fetchone()
        if active:
            continue
        updated = conn.execute(
            "UPDATE activity_confirmation_evidence_files "
            "SET state='delete_pending',delete_reason=?,updated_at=?,last_error='' "
            "WHERE evidence_id=? AND state='available'",
            (_safe_error(reason), now, evidence_id),
        )
        scheduled += int(updated.rowcount or 0)
        conn.execute(
            "UPDATE activity_confirmation_evidence_files "
            "SET state='deleted',deleted_at=?,delete_reason=?,updated_at=?,last_error='' "
            "WHERE evidence_id=? AND state='unavailable'",
            (now, _safe_error(reason), now, evidence_id),
        )
    return scheduled


def cleanup_uncommitted_confirmation_evidence(
    conn: sqlite3.Connection,
    promotions: list[dict[str, Any]] | tuple[dict[str, Any], ...],
) -> dict[str, int]:
    """Delete only newly-created files whose owning DB rows did not commit."""
    candidates: dict[str, str] = {}
    for promotion in promotions:
        if not bool(promotion.get("created_new")):
            continue
        evidence_id = str(promotion.get("evidence_id") or "")
        file_name = str(promotion.get("file_name") or "")
        if (
            evidence_id.startswith("confirm_ev_")
            and file_name == f"{evidence_id}.png"
            and Path(file_name).name == file_name
        ):
            candidates[evidence_id] = file_name
    if not candidates or not _table_exists(conn, "activity_confirmation_evidence_files"):
        return {"deleted": 0, "retained": 0, "failed": 0}
    try:
        root = database_root(conn)
    except RuntimeError:
        return {"deleted": 0, "retained": 0, "failed": len(candidates)}
    deleted = retained = failed = 0
    with evidence_lock(root, exclusive=True, create=False) as directory:
        if directory is None:
            return {"deleted": 0, "retained": 0, "failed": len(candidates)}
        for evidence_id, file_name in candidates.items():
            committed = conn.execute(
                "SELECT 1 FROM activity_confirmation_evidence_files "
                "WHERE evidence_id=? AND state='available' AND file_name=? LIMIT 1",
                (evidence_id, file_name),
            ).fetchone()
            active_ref = conn.execute(
                "SELECT 1 FROM activity_confirmation_evidence_refs "
                "WHERE evidence_id=? AND released_at IS NULL LIMIT 1",
                (evidence_id,),
            ).fetchone()
            if committed or active_ref:
                retained += 1
                continue
            path = _owned_regular_evidence_path(directory, file_name)
            if path is None:
                failed += 1
                continue
            try:
                path.unlink(missing_ok=True)
                deleted += 1
            except OSError:
                failed += 1
    return {"deleted": deleted, "retained": retained, "failed": failed}


def purge_scheduled_evidence(conn: sqlite3.Connection, root: str | Path | None = None) -> dict[str, int]:
    resolved_root = Path(root).resolve() if root is not None else database_root(conn)
    deleted = 0
    failed = 0
    bytes_deleted = 0
    with evidence_lock(resolved_root, exclusive=True) as directory:
        schedule_expired_evidence(conn)
        if directory is None:
            conn.commit()
            return {"deleted": 0, "failed": 1, "bytes_deleted": 0}
        now = utc_now()
        orphan_ids = [
            str(row["evidence_id"])
            for row in conn.execute(
                "SELECT f.evidence_id FROM activity_confirmation_evidence_files AS f "
                "WHERE f.state='available' AND NOT EXISTS ("
                "SELECT 1 FROM activity_confirmation_evidence_refs AS r "
                "WHERE r.evidence_id=f.evidence_id AND r.released_at IS NULL"
                ")"
            ).fetchall()
        ]
        for offset in range(0, len(orphan_ids), 100):
            batch = orphan_ids[offset:offset + 100]
            placeholders = ",".join("?" for _ in batch)
            conn.execute(
                "UPDATE activity_confirmation_evidence_files "
                "SET state='delete_pending',delete_reason='orphan_no_active_ref',"
                "updated_at=?,last_error='' "
                f"WHERE evidence_id IN ({placeholders})",
                (now, *batch),
            )
        rows = conn.execute(
            "SELECT * FROM activity_confirmation_evidence_files WHERE state='delete_pending'"
        ).fetchall()
        for row in rows:
            path = _evidence_path(directory, str(row["file_name"] or ""))
            byte_size = int(row["byte_size"] or 0)
            try:
                if path is not None:
                    path.unlink(missing_ok=True)
                now = utc_now()
                conn.execute(
                    "UPDATE activity_confirmation_evidence_files "
                    "SET file_name='',content_sha256='',byte_size=0,state='deleted',"
                    "last_error='',deleted_at=?,updated_at=? WHERE evidence_id=?",
                    (now, now, str(row["evidence_id"])),
                )
                deleted += 1
                bytes_deleted += byte_size
            except OSError:
                conn.execute(
                    "UPDATE activity_confirmation_evidence_files "
                    "SET last_error='delete_failed',updated_at=? WHERE evidence_id=?",
                    (utc_now(), str(row["evidence_id"])),
                )
                failed += 1
        conn.commit()
    return {"deleted": deleted, "failed": failed, "bytes_deleted": bytes_deleted}


def evidence_summary(conn: sqlite3.Connection, item_keys: list[str] | tuple[str, ...]) -> dict[str, Any]:
    keys = _unique_safe_ids(item_keys)
    if not keys:
        return {"evidence_status": "unavailable", "evidence_count": 0, "available_source_count": 0}
    if not _table_exists(conn, "activity_confirmation_evidence_refs"):
        return {"evidence_status": "unavailable", "evidence_count": 0, "available_source_count": 0}
    try:
        root = database_root(conn)
    except RuntimeError:
        return {"evidence_status": "unavailable", "evidence_count": 0, "available_source_count": 0}
    available_items: set[str] = set()
    evidence_ids: set[str] = set()
    with evidence_lock(root, exclusive=False, create=False) as directory:
        if directory is None:
            return {"evidence_status": "unavailable", "evidence_count": 0, "available_source_count": 0}
        placeholders = ",".join("?" for _ in keys)
        rows = conn.execute(
            f"""
            SELECT r.item_key,f.*
            FROM activity_confirmation_evidence_refs AS r
            JOIN activity_confirmation_evidence_files AS f ON f.evidence_id=r.evidence_id
            WHERE r.item_key IN ({placeholders}) AND r.released_at IS NULL
            """,
            tuple(keys),
        ).fetchall()
        policy = evidence_policy(conn)
        expired_count = 0
        for row in rows:
            if evidence_expired(row, policy):
                expired_count += 1
                continue
            if str(row["state"] or "") != "available":
                continue
            if _verified_evidence_path(directory, row) is None:
                continue
            available_items.add(str(row["item_key"] or ""))
            evidence_ids.add(str(row["evidence_id"] or ""))
    if len(available_items) == len(keys):
        status = "available"
    elif available_items:
        status = "partial"
    else:
        status = "expired" if expired_count else "unavailable"
    return {
        "evidence_status": status,
        "evidence_count": len(evidence_ids),
        "available_source_count": len(available_items),
        "expired_count": expired_count,
    }


@contextmanager
def held_evidence_paths(
    conn: sqlite3.Connection,
    item_keys: list[str] | tuple[str, ...],
) -> Iterator[tuple[list[dict[str, Any]], dict[str, Any], dict[str, list[str]]]]:
    keys = _unique_safe_ids(item_keys)
    unavailable = {
        "evidence_status": "unavailable",
        "evidence_count": 0,
        "available_source_count": 0,
    }
    no_issues = {"invalid_evidence_ids": [], "invalid_file_names": []}
    if not keys or not _table_exists(conn, "activity_confirmation_evidence_refs"):
        yield [], unavailable, no_issues
        return
    try:
        root = database_root(conn)
    except RuntimeError:
        yield [], unavailable, no_issues
        return
    with evidence_lock(root, exclusive=False, create=False) as directory:
        if directory is None:
            yield [], unavailable, no_issues
            return
        records_by_id: dict[str, dict[str, Any]] = {}
        invalid_evidence_ids: list[str] = []
        invalid_file_names: list[str] = []
        placeholders = ",".join("?" for _ in keys)
        rows = conn.execute(
            f"""
            SELECT r.item_key,f.*
            FROM activity_confirmation_evidence_refs AS r
            JOIN activity_confirmation_evidence_files AS f ON f.evidence_id=r.evidence_id
            WHERE r.item_key IN ({placeholders}) AND r.released_at IS NULL
            ORDER BY datetime(f.captured_at),f.evidence_id
            """,
            tuple(keys),
        ).fetchall()
        policy = evidence_policy(conn)
        for row in rows:
            if str(row["state"] or "") != "available" or evidence_expired(row, policy):
                continue
            evidence_id = str(row["evidence_id"] or "")
            file_name = str(row["file_name"] or "")
            if evidence_id in invalid_evidence_ids:
                continue
            path = _verified_evidence_path(directory, row, verify_content=True)
            if path is None:
                invalid_evidence_ids.append(evidence_id)
                if file_name and Path(file_name).name == file_name:
                    invalid_file_names.append(file_name)
                records_by_id.pop(evidence_id, None)
                continue
            item_key = str(row["item_key"] or "")
            record = records_by_id.setdefault(
                evidence_id,
                {
                    "path": path,
                    "evidence_id": evidence_id,
                    "item_keys": [],
                    "file_name": file_name,
                },
            )
            if item_key and item_key not in record["item_keys"]:
                record["item_keys"].append(item_key)
        records = list(records_by_id.values())
        available_items = {
            item_key
            for record in records
            for item_key in record["item_keys"]
            if item_key
        }
        if keys and len(available_items) == len(keys):
            status = "available"
        elif available_items:
            status = "partial"
        else:
            status = "unavailable"
        yield (
            records,
            {
                "evidence_status": status,
                "evidence_count": len(records),
                "available_source_count": len(available_items),
            },
            {
                "invalid_evidence_ids": _unique_safe_ids(invalid_evidence_ids),
                "invalid_file_names": _unique_safe_ids(invalid_file_names),
            },
        )


def reconcile_confirmation_evidence(conn: sqlite3.Connection, root: str | Path | None = None) -> dict[str, int]:
    resolved_root = Path(root).resolve() if root is not None else database_root(conn)
    purged = purge_scheduled_evidence(conn, resolved_root)
    missing = 0
    orphan_files = 0
    with evidence_lock(resolved_root, exclusive=True) as directory:
        if directory is None:
            return {
                "delete_pending_deleted": int(purged["deleted"]),
                "delete_pending_failed": int(purged["failed"]),
                "missing_marked": 0,
                "orphan_files_deleted": 0,
            }
        referenced_names: set[str] = set()
        rows = conn.execute(
            "SELECT * FROM activity_confirmation_evidence_files WHERE state='available'"
        ).fetchall()
        for row in rows:
            name = str(row["file_name"] or "")
            path = _verified_evidence_path(directory, row, verify_content=True)
            if path is None:
                conn.execute(
                    "UPDATE activity_confirmation_evidence_files "
                    "SET state='unavailable',file_name='',content_sha256='',byte_size=0,"
                    "last_error='source_missing',updated_at=? WHERE evidence_id=?",
                    (utc_now(), str(row["evidence_id"])),
                )
                missing += 1
            else:
                referenced_names.add(name)
        for child in directory.iterdir():
            if child.name in {".metadata_never_index", ".evidence.lock"}:
                continue
            try:
                if time.time() - child.lstat().st_mtime < ORPHAN_GRACE_SECONDS:
                    continue
            except OSError:
                continue
            if child.is_symlink() or child.is_dir() or child.name not in referenced_names:
                try:
                    if child.is_dir() and not child.is_symlink():
                        shutil.rmtree(child)
                    else:
                        child.unlink(missing_ok=True)
                    orphan_files += 1
                except OSError:
                    pass
        conn.commit()
    return {
        "delete_pending_deleted": int(purged["deleted"]),
        "delete_pending_failed": int(purged["failed"]),
        "missing_marked": missing,
        "orphan_files_deleted": orphan_files,
    }


def evidence_health(conn: sqlite3.Connection) -> dict[str, Any]:
    empty_bucket = {
        "status": "ok",
        "available_count": 0,
        "delete_pending_count": 0,
        "unavailable_count": 0,
        "retained_bytes": 0,
        "error_count": 0,
        "raw_access": False,
    }
    if not _table_exists(conn, "activity_confirmation_evidence_files"):
        return {
            "contract_version": 2,
            "status": "ok",
            "available_count": 0,
            "delete_pending_count": 0,
            "unavailable_count": 0,
            "retained_bytes": 0,
            "error_count": 0,
            "actionable": dict(empty_bucket),
            "historical": dict(empty_bucket),
            "raw_access": False,
        }
    counts = {
        str(row["state"]): int(row["count"] or 0)
        for row in conn.execute(
            "SELECT state,COUNT(*) AS count FROM activity_confirmation_evidence_files GROUP BY state"
        ).fetchall()
    }
    row = conn.execute(
        "SELECT COALESCE(SUM(byte_size),0) AS bytes FROM activity_confirmation_evidence_files WHERE state='available'"
    ).fetchone()
    errors = int(
        conn.execute(
            "SELECT COUNT(*) AS count FROM activity_confirmation_evidence_files "
            "WHERE last_error<>'' OR state IN ('delete_pending','unavailable')"
        ).fetchone()["count"]
        or 0
    )
    top_level = {
        "status": "attention" if errors else "ok",
        "available_count": counts.get("available", 0),
        "delete_pending_count": counts.get("delete_pending", 0),
        "unavailable_count": counts.get("unavailable", 0),
        "retained_bytes": int(row["bytes"] or 0) if row else 0,
        "error_count": errors,
        "raw_access": False,
    }
    if not _table_exists(conn, "activity_confirmation_evidence_refs") or not _table_exists(
        conn, "activity_confirmation_items"
    ):
        actionable = {
            **empty_bucket,
            "status": "attention",
            "error_count": 1,
            "reason_code": "relationship_unavailable",
        }
        return {
            "contract_version": 2,
            **top_level,
            "actionable": actionable,
            "historical": {**top_level},
        }

    bucket_rows = conn.execute(
        """
        WITH classified AS (
          SELECT
            f.state,
            f.byte_size,
            f.last_error,
            CASE WHEN EXISTS (
              SELECT 1
              FROM activity_confirmation_evidence_refs AS r
              JOIN activity_confirmation_items AS i ON i.item_key = r.item_key
              WHERE r.evidence_id = f.evidence_id
                AND r.released_at IS NULL
                AND i.state IN ('pending','deferred')
            ) THEN 'actionable' ELSE 'historical' END AS bucket
          FROM activity_confirmation_evidence_files AS f
        )
        SELECT
          bucket,
          SUM(CASE WHEN state='available' THEN 1 ELSE 0 END) AS available_count,
          SUM(CASE WHEN state='delete_pending' THEN 1 ELSE 0 END) AS delete_pending_count,
          SUM(CASE WHEN state='unavailable' THEN 1 ELSE 0 END) AS unavailable_count,
          COALESCE(SUM(CASE WHEN state='available' THEN byte_size ELSE 0 END),0) AS retained_bytes,
          SUM(CASE WHEN last_error<>'' OR state IN ('delete_pending','unavailable') THEN 1 ELSE 0 END) AS error_count
        FROM classified
        GROUP BY bucket
        """
    ).fetchall()
    buckets = {"actionable": dict(empty_bucket), "historical": dict(empty_bucket)}
    for bucket_row in bucket_rows:
        bucket_name = str(bucket_row["bucket"] or "historical")
        error_count = int(bucket_row["error_count"] or 0)
        buckets[bucket_name] = {
            "status": "attention" if error_count else "ok",
            "available_count": int(bucket_row["available_count"] or 0),
            "delete_pending_count": int(bucket_row["delete_pending_count"] or 0),
            "unavailable_count": int(bucket_row["unavailable_count"] or 0),
            "retained_bytes": int(bucket_row["retained_bytes"] or 0),
            "error_count": error_count,
            "raw_access": False,
        }
    return {
        "contract_version": 2,
        **top_level,
        "actionable": buckets["actionable"],
        "historical": buckets["historical"],
    }


def _valid_capture_source(root: Path, path: Path) -> bool:
    try:
        if path.is_symlink() or not path.is_file():
            return False
        resolved = path.resolve(strict=True)
        raw_candidate = root / "raw_vault"
        if raw_candidate.is_symlink():
            return False
        raw_details = raw_candidate.stat()
        if not stat.S_ISDIR(raw_details.st_mode) or raw_details.st_uid != os.getuid():
            return False
        raw_root = raw_candidate.resolve(strict=True)
        if not resolved.is_relative_to(raw_root):
            return False
        details = resolved.stat()
        return bool(
            stat.S_ISREG(details.st_mode)
            and details.st_uid == os.getuid()
            and stat.S_IMODE(details.st_mode) & 0o077 == 0
        )
    except OSError:
        return False


def _verified_evidence_path(
    directory: Path,
    row: Any,
    *,
    verify_content: bool = False,
) -> Path | None:
    path = _evidence_path(directory, str(row["file_name"] or ""))
    if path is None:
        return None
    try:
        details = path.lstat()
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            return None
        if details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) != 0o600:
            return None
        if verify_content:
            expected_size = int(row["byte_size"] or 0)
            expected_digest = str(row["content_sha256"] or "")
            if expected_size <= 0 or details.st_size != expected_size or len(expected_digest) != 64:
                return None
            digest, byte_size, png_signature = _hash_evidence_file(path)
            if not png_signature or byte_size != expected_size or digest != expected_digest:
                return None
        return path
    except OSError:
        return None


def _owned_regular_evidence_path(directory: Path, file_name: str) -> Path | None:
    path = _evidence_path(directory, file_name)
    if path is None:
        return None
    try:
        details = path.lstat()
        if (
            stat.S_ISLNK(details.st_mode)
            or not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
        ):
            return None
        return path
    except OSError:
        return None


def _evidence_path(directory: Path, file_name: str) -> Path | None:
    name = str(file_name or "")
    if not name or Path(name).name != name or not name.endswith(".png"):
        return None
    candidate = directory / name
    try:
        if not candidate.resolve(strict=False).is_relative_to(directory.resolve()):
            return None
    except OSError:
        return None
    return candidate


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    details = os.fstat(descriptor)
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) & 0o077
    ):
        os.close(descriptor)
        raise OSError("unsafe_capture_source")
    with os.fdopen(descriptor, "rb", closefd=True) as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _copy_private_capture_source(path: Path, output_handle: Any) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    details = os.fstat(descriptor)
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) & 0o077
    ):
        os.close(descriptor)
        raise OSError("unsafe_capture_source")
    with os.fdopen(descriptor, "rb", closefd=True) as input_handle:
        shutil.copyfileobj(input_handle, output_handle)


def _hash_evidence_file(path: Path) -> tuple[str, int, bool]:
    digest = hashlib.sha256()
    size = 0
    signature = b""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    details = os.fstat(descriptor)
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != 0o600
    ):
        os.close(descriptor)
        raise OSError("unsafe_confirmation_evidence")
    with os.fdopen(descriptor, "rb", closefd=True) as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            if not signature:
                signature = chunk[:8]
            digest.update(chunk)
            size += len(chunk)
    if size != details.st_size:
        raise OSError("confirmation_evidence_changed_during_read")
    return digest.hexdigest(), size, signature == b"\x89PNG\r\n\x1a\n"


def _unique_safe_ids(values: list[str] | tuple[str, ...]) -> list[str]:
    output: list[str] = []
    for value in values:
        token = str(value or "")[:160]
        if token and token not in output:
            output.append(token)
    return output


def _safe_error(value: str) -> str:
    token = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in str(value or "").lower())
    return token.strip("_")[:80] or "unknown"


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None
