from __future__ import annotations

import contextvars
import ctypes
import errno
import fcntl
import hashlib
import hmac
import io
import json
import os
import posixpath
import queue
import re
import secrets
import sqlite3
import stat as stat_module
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator
from xml.etree import ElementTree

from .privacy import document_protected_context_hit, redact_for_storage
from .document_git_ignore import CURRENT as _BASELINE_IGNORE_PROOFS, baseline_pass as _baseline_git_pass


MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_PACKAGE_BYTES = 50 * 1024 * 1024
DEBOUNCE_SECONDS = 3
# The one-time baseline scan reads and fingerprints every tracked file, which
# is the heavy operation the old AC + idle startup gate existed to defer.  The
# gate made tracking unable to restart at all in normal use, so the scan now
# paces itself instead: after every batch it yields long enough to keep disk
# and CPU available to whatever the owner is actually doing.
BASELINE_SCAN_THROTTLE_EVERY_FILES = 50
BASELINE_SCAN_THROTTLE_SECONDS = 0.025
# Baseline snapshots are expensive to obtain but cheap to persist.  Keeping a
# bounded set of already-sanitized fingerprints in memory lets one exact write
# gate cover many rows without ever holding SQLite's writer lock across file
# I/O.  This is intentionally independent of the public-ish scan pacing
# ``batch_size`` argument below: callers may disable sleeps without allowing an
# unbounded persistence batch.
DOCUMENT_BASELINE_WRITE_BATCH_SIZE = 200
DOCUMENT_BASELINE_WRITE_BATCH_MAX_BYTES = 8 * 1024 * 1024
DOCUMENT_BASELINE_DIAGNOSTIC_INTERVAL_SECONDS = 60.0
_DOCUMENT_BASELINE_METRIC_RECORDER = contextvars.ContextVar(
    "document_baseline_metric_recorder",
    default=None,
)
_DOCUMENT_BASELINE_LIVE_SESSION_PROBE = contextvars.ContextVar(
    "document_baseline_live_session_probe",
    default=None,
)
_DOCUMENT_DATALESS_READ_RECOVERY = contextvars.ContextVar(
    "document_dataless_read_recovery", default=None,
)


class DatalessReadPolicyRestoreError(Exception):
    """Stop this reader if its temporary thread policy cannot be restored."""


def _dataless_thread_policy():
    # Apple TN3150 / sys/resource.h. Never change the process-wide policy.
    library = ctypes.CDLL(None, use_errno=True)
    getter, setter = library.getiopolicy_np, library.setiopolicy_np
    getter.argtypes = [ctypes.c_int, ctypes.c_int]
    setter.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int]
    getter.restype = setter.restype = ctypes.c_int
    return getter, setter


class _DatalessReadRecovery:
    def __init__(self, descriptor, metadata, content_allowed_check):
        self.descriptor = descriptor
        self.metadata = metadata
        self.content_allowed_check = content_allowed_check
        self.attempted = False
        self.policy = None

    def _check(self):
        if self.content_allowed_check is None or not bool(self.content_allowed_check()):
            raise PermissionError("content read deferred while reading")
        current = os.fstat(self.descriptor)
        before = self.metadata
        if not stat_module.S_ISREG(current.st_mode) or not 0 <= current.st_size <= MAX_PACKAGE_BYTES:
            raise OSError("document changed while fingerprinting")
        if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
            before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
        ):
            raise OSError("document changed while fingerprinting")
        return current

    def read(self, count):
        if self.policy is not None:
            self._check()
        try:
            return os.read(self.descriptor, count)
        except OSError as error:
            if error.errno != errno.EDEADLK or self.attempted or sys.platform != "darwin":
                raise
            # Only a live-authorized regular cloud placeholder on this exact
            # no-follow descriptor may cause one bounded materialization retry.
            if self.content_allowed_check is None:
                raise
            current = self._check()
            if not getattr(current, "st_flags", 0) & getattr(stat_module, "SF_DATALESS", 0x40000000):
                raise
            self.attempted = True
            getter, setter = _dataless_thread_policy()
            original = getter(3, 1)
            if original not in (0, 1, 2):
                raise error
            # Save before the setter, so even a reported setter failure has
            # to restore and read back the exact original thread value.
            self.policy = (getter, setter, original)
            if setter(3, 1, 2) != 0 or getter(3, 1) != 2:
                raise error
            self._check()
            return os.read(self.descriptor, count)

    def close(self):
        if self.policy is None:
            return
        getter, setter, original = self.policy
        try:
            if setter(3, 1, original) != 0 or getter(3, 1) != original:
                raise DatalessReadPolicyRestoreError("document reader thread policy restore failed")
        except DatalessReadPolicyRestoreError:
            raise
        except Exception as error:
            raise DatalessReadPolicyRestoreError("document reader thread policy restore failed") from error
        finally:
            self.policy = None


def _read_document_chunk(descriptor, count):
    recovery = _DOCUMENT_DATALESS_READ_RECOVERY.get()
    if recovery is not None and recovery.descriptor == descriptor:
        return recovery.read(count)
    return os.read(descriptor, count)
_DOCUMENT_BASELINE_FAILURE_REASONS = (
    "file_missing", "permission_denied", "content_changed", "session_content_denied",
    "invalid_snapshot", "invalid_package", "snapshot_runtime_error", "snapshot_io_error",
    "traversal_failed", "git_ignore_unavailable", "tracking_disabled", "root_removed",
    "paused", "authorization_changed", "batch_discarded", "concurrent_state_change",
)
_DOCUMENT_BASELINE_IO_STEPS = (
    "other", "snapshot", "open", "read", "read_limit", "hash", "key",
    "key_read", "authorization", "authorization_lock",
)
_DOCUMENT_BASELINE_IO_ERRNOS = (
    "not_provided", "other", "EIO", "ETIMEDOUT", "EBUSY", "EBADF", "EINTR",
    "EMFILE", "ENFILE", "ENOSPC", "EROFS", "EAGAIN", "EDEADLK", "EINVAL",
    "EOVERFLOW", "ENOTSUP", "ECANCELED", "ELOOP", "EISDIR", "ENOTDIR",
)


def _baseline_snapshot_io_details(exc: OSError) -> tuple[str, str]:
    # A closed label identifies the deepest recognized product frame. Never
    # export a traceback, filename, exception message or arbitrary errno.
    step = "other"
    known_frames = (
        ("_snapshot", "snapshot"), ("_open_file_beneath", "open"),
        ("_read_open_file", "read"), ("_file_content_hmac", "hash"),
        ("_hmac_key", "key"), ("_read_private_hmac_secret", "key_read"),
        ("_document_change_authorized_readonly", "authorization"),
        ("document_authorization_lock", "authorization_lock"),
        ("_acquire_authorization_flock", "authorization_lock"),
    )
    trace = exc.__traceback__
    while trace is not None:
        for name, label in known_frames:
            function = globals().get(name)
            codes = (
                getattr(function, "__code__", None),
                getattr(getattr(function, "__wrapped__", None), "__code__", None),
            )
            if trace.tb_frame.f_code in codes:
                step = label
                break
        trace = trace.tb_next
    if str(exc) == "document grew beyond the extraction cap":
        step = "read_limit"
    number = getattr(exc, "errno", None)
    label = "not_provided" if number is None else "other"
    if type(number) is int:
        for candidate in _DOCUMENT_BASELINE_IO_ERRNOS[2:]:
            if getattr(errno, candidate, None) == number:
                label = candidate
                break
    return step, label


def _baseline_snapshot_failure_reason(exc: Exception) -> str:
    # Only fixed categories leave this function. Exception text can contain
    # private paths or source content and must never enter diagnostics.
    if isinstance(exc, FileNotFoundError):
        return "file_missing"
    if isinstance(exc, PermissionError):
        if str(exc) in {
            "content read deferred by live session state",
            "content read deferred by unknown session state",
            "content read deferred after structure extraction",
            "content read deferred while hashing",
            "content read deferred while reading",
        }:
            return "session_content_denied"
        return "permission_denied"
    if isinstance(exc, zipfile.BadZipFile):
        return "invalid_package"
    if isinstance(exc, OSError):
        if str(exc) in {"document changed while fingerprinting", "document is still changing"}:
            return "content_changed"
        return "snapshot_io_error"
    if isinstance(exc, ValueError):
        return "invalid_snapshot"
    return "snapshot_runtime_error"
# Owner-approved file-tracking policy (2026-08-11): a verified local console
# session may continue hashing already-authorized roots while the display is
# locked.  This is deliberately narrower than the shared capture policy:
# screenshots/OCR still use ``capture_allowed`` and remain disabled while
# locked.  Unknown, contradictory, or off-console session evidence continues
# to fail closed.
DOCUMENT_CHANGE_ALLOW_LOCKED_CONSOLE = True


def _document_change_session_allows_content(value: object) -> bool:
    """Return whether fresh session evidence permits document content I/O."""

    if (
        not isinstance(value, dict)
        or value.get("on_console") is not True
        or bool(str(value.get("error") or "").strip())
    ):
        return False
    state = str(value.get("state") or "").strip().lower()
    if state == "unlocked":
        return bool(
            value.get("locked") is False
            and value.get("capture_allowed") is True
        )
    if state == "locked" and DOCUMENT_CHANGE_ALLOW_LOCKED_CONSOLE:
        # Canonical session payloads intentionally keep the generic capture
        # bit false while locked.  Requiring that exact shape prevents an
        # inconsistent or hand-crafted payload from widening this exception.
        return bool(
            value.get("locked") is True
            and value.get("capture_allowed") is False
        )
    return False

# Three full passes means reading the contents of every tracked file three
# times, and on a machine in use pass 2 and pass 3 fail for exactly the reason
# pass 1 did: something moved while we were reading.  One honest retry catches
# the genuinely quiet case; a third identical attempt only buys I/O.
DOCUMENT_BASELINE_MAX_PASSES = 2
# The per-file authorization gate used to open a brand-new connection for every
# single file, and ensure_ready runs the whole schema pipeline -- backup checks,
# init_schema, a write commit -- on every open.  Measured on this machine's
# 345MB database that cost ~1.5s per file, which turned an 11ms/file scan into
# hours (12k files: 134s without the gate, 5h+ with it).  Three seconds of
# memory keeps revocation latency within the same order as the existing 1s
# eligibility cache, while the write-time gate in _begin_document_change_write
# stays exact and uncached -- a revoked root can, at worst, have one file's
# content read into memory during the window, never persisted.
DOCUMENT_AUTHORIZATION_CACHE_SECONDS = 3.0
DOCUMENT_CHANGE_EVENT_QUEUE_MAXSIZE = 10_000
DOCUMENT_CHANGE_VALIDATION_QUEUE_MAXSIZE = 10_000
DOCUMENT_CHANGE_PROCESSOR_QUEUE_MAXSIZE = 10_000
# Superseded-entry merging in the single FIFO processor (see
# start_document_change_worker).  The debt limit bounds memory; once reached,
# entries for new paths are processed normally rather than skipped, which is
# the old, slower, but never less conservative behavior.
DOCUMENT_CHANGE_SUPERSEDED_DEBT_PATH_LIMIT = 4096
DOCUMENT_CHANGE_SLOW_STABILITY_SECONDS = 5.0
DOCUMENT_CHANGE_MERGE_DIAGNOSTIC_INTERVAL_SECONDS = 60.0
CONTROL_EVENT_FLAGS = {
    "must_scan_subdirs", "user_dropped", "kernel_dropped",
    "event_ids_wrapped", "root_changed", "mount", "unmount",
}
DOCUMENT_CHANGE_FATAL_REASON_CODES = frozenset(
    {
        "baseline_exception",
        "baseline_not_clean",
        "baseline_reconcile_window_closed",
        "event_consumer_exception",
        "event_handler_exception",
        "merged_events_unsettled_at_stop",
        "event_ignore_evaluation_failed",
        "event_validator_exception",
        "fsevents_control_event",
        "helper_barrier_failed",
        "helper_not_ready",
        "helper_protocol_error",
        "helper_reader_exception",
        "helper_root_count_mismatch",
        "helper_unexpected_exit",
        "processor_queue_overflow",
        "python_event_queue_overflow",
        "unexpected_barrier",
        "validation_queue_overflow",
        "worker_monitor_exception",
    }
)
KNOWN_EVENT_FLAGS = CONTROL_EVENT_FLAGS | {
    "item_created", "item_removed", "item_renamed", "item_modified",
    "inode_metadata_modified", "item_is_file", "item_is_dir", "item_is_symlink",
}
RUNTIME_ROOTS_FILENAME = "document_watch_roots.json"
RUNTIME_ROOTS_MAX_BYTES = 256 * 1024
RUNTIME_ROOTS_FORMAT_VERSION = 2
ROOT_AUTHORIZATION_REVISION_KEY = "document_change_root_authorization_revision"
AUTHORIZATION_LOCK_FILENAME = ".document_watch_authorization.lock"
# Interactive callers must never block on a maintenance lease long enough for
# the UI to look frozen; on timeout they surface `database_busy` and retry.
#
# The read budget is sized against the measured worst case rather than guessed:
# one `cleanup_retention` pass holds the exclusive lease for ~1.9s in total
# (`retry_pending_package_cache_purges` + `delete_range` + `expire_packages`).
# A budget below that would make every collision fail and force a manual
# refresh, so keep enough headroom to simply wait the maintenance pass out and
# return real data.
INTERACTIVE_AUTHORIZATION_LOCK_TIMEOUT_SECONDS = 3.0
# A user-initiated write (answering a card, clarifying, manual add) is far more
# costly to lose than a page of cards, so it waits longer before giving up.
INTERACTIVE_WRITE_AUTHORIZATION_LOCK_TIMEOUT_SECONDS = 5.0
AUTHORIZATION_LOCK_RETRY_SECONDS = 0.05
ROOT_REF_PREFIX = "rootref_"
ROOT_REF_RE = re.compile(r"\Arootref_[A-Za-z0-9_-]{24}\Z")
ROOT_ID_RE = re.compile(r"\Aroot_[0-9a-f]{32}\Z")
FILE_ID_RE = re.compile(r"\Afile_[0-9a-f]{40}\Z")
ABSOLUTE_PATH_SIGNAL_RE = re.compile(
    r"(?:~[/\\][^\s,，;；。)）\]】]+|[A-Za-z]:[/\\][^\s,，;；。)）\]】]+|"
    r"\\\\[^\\\s,，;；。]+\\[^\\\s,，;；。]+(?:\\[^\\\s,，;；。]+)*|"
    r"(?:/[^/\s,，;；。)）\]】]+){2,}|(?:^|[\s\"'`=({\[（【:：])/(?:[^/\s,，;；。)）\]】]+))"
)
HMAC_SECRET_FILENAME = "document_watch_secret.key"
HMAC_SECRET_LOCK_FILENAME = ".document_watch_secret.lock"
HMAC_VERIFIER_STATE_KEY = "document_change_hmac_verifier"
HMAC_VERIFIER_DOMAIN = "personal-ai-monitor-document-watch-key-v1"
TEXT_SUFFIXES = {
    ".md", ".markdown", ".txt", ".rst", ".py", ".js", ".jsx", ".ts", ".tsx",
    ".swift", ".go", ".rs", ".java", ".kt", ".c", ".h", ".cpp", ".hpp", ".css",
    ".scss", ".html", ".htm", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini",
    ".cfg", ".xml", ".sh", ".zsh", ".fish", ".sql",
}
PACKAGE_SUFFIXES = {".docx", ".odt", ".pptx", ".xlsx"}
METADATA_SUFFIXES = {".pdf", ".pages", ".keynote", ".numbers", ".doc", ".ppt", ".xls"}
ALLOWED_SUFFIXES = TEXT_SUFFIXES | PACKAGE_SUFFIXES | METADATA_SUFFIXES
PUBLIC_FILE_TYPES = {suffix.lstrip(".") for suffix in ALLOWED_SUFFIXES} | {"unknown", "protected"}
PUBLIC_BASELINE_STATUSES = {"disabled", "pending", "building", "partial", "complete"}
DOCUMENT_OWNER_LOCAL_PROJECTION_VERSION = "DocumentOwnerLocalObservationV2"
DOCUMENT_TRUSTED_PROJECTION_VERSION = "DocumentTrustedExportV1"
IGNORED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "vendor", "Pods", "DerivedData", "target",
    "dist", "build", ".build", ".cache", "__pycache__", ".venv", "venv", "site-packages",
    "Library", "raw_vault", "ha_packages",
}
TEMP_NAME_RE = re.compile(r"(?:^~\$|^\.#|\.sw[px]$|\.tmp$|\.temp$|\.lock$|~$)", re.I)
SENSITIVE_NAME_RE = re.compile(
    r"(password|secret|token|credential|private.?key|recovery|验证码|密码|密钥|凭证|私聊|聊天记录|"
    r"medical|health|diagnos(?:is|tic)?|prescription|patient|financial|bank(?:ing)?|invoice|payment|"
    r"credit.?card|private.?chat|direct.?message|client.?list|customer.?list|pii|passport|tax.?record|"
    r"病历|诊断|处方|体检|医疗|财务|发票|银行|银行卡|支付|账单|"
    r"客户资料|客户隐私|客户名单|客户姓名|联系方式|手机号|身份证)", re.I
)
SAFE_LABEL_RE = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff _.,:+()\-/]", re.UNICODE)
_EPHEMERAL_HMAC_KEY: bytes | None = None
_EPHEMERAL_HMAC_KEY_LOCK = threading.Lock()
_EPHEMERAL_AUTHORIZATION_LOCK = threading.RLock()


class GitIgnoreEvaluationError(RuntimeError):
    pass


class DocumentContextProjectionError(RuntimeError):
    """Abort the source document transaction when a governed projection fails."""


def default_document_roots(home: str | Path | None = None) -> list[tuple[str, Path]]:
    base = Path(home).expanduser() if home is not None else Path.home()
    return [
        ("Obsidian", base / "Library/Mobile Documents/iCloud~md~obsidian/Documents/默认仓库"),
        ("AI", base / "AI"),
        ("Documents", base / "Documents"),
        ("Desktop", base / "Desktop"),
        ("Downloads", base / "Downloads"),
        ("Code", base / "Code"),
        ("LocalOnly", base / "LocalOnly"),
    ]


def document_changes_status(conn: sqlite3.Connection) -> dict:
    try:
        with document_authorization_lock(conn, exclusive=False):
            return _document_changes_status_locked(conn)
    except RuntimeError:
        return _document_changes_status_locked(conn, force_authorization_unavailable=True)


def _document_changes_status_locked(
    conn: sqlite3.Connection,
    *,
    force_authorization_unavailable: bool = False,
) -> dict:
    enabled = _setting_bool(conn, "document_change_tracking_enabled", True)
    export_enabled = _setting_bool(conn, "document_change_export_enabled", False)
    rows = conn.execute(
        "SELECT root_id, root_label, enabled, baseline_status, baseline_started_at, baseline_completed_at "
        "FROM file_watch_roots ORDER BY root_label"
    ).fetchall()
    checkpoints = conn.execute(
        "SELECT c.coverage_status,c.event_gap_count,c.benign_event_count,c.coverage_started_at,c.last_event_at "
        "FROM file_watch_checkpoints c JOIN file_watch_roots r ON r.root_id=c.root_id WHERE r.enabled=1"
    ).fetchall()
    indexed_by_root = {
        str(row["root_id"]): int(row["indexed_count"] or 0)
        for row in conn.execute(
            "SELECT root_id,COUNT(*) AS indexed_count FROM file_watch_state WHERE present=1 GROUP BY root_id"
        ).fetchall()
    }
    statuses = [str(row["baseline_status"] or "pending") for row in rows if row["enabled"]]
    root_status_integrity_invalid = any(
        item not in PUBLIC_BASELINE_STATUSES for item in statuses
    )
    if not enabled:
        baseline_status = "disabled"
    elif not statuses:
        baseline_status = "pending"
    elif root_status_integrity_invalid:
        baseline_status = "partial"
    elif any(item in {"partial", "baseline_gap", "error"} for item in statuses):
        baseline_status = "partial"
    elif all(item == "complete" for item in statuses):
        baseline_status = "complete"
    else:
        baseline_status = "building"
    raw_last_events = [str(row["last_event_at"]) for row in checkpoints if row["last_event_at"]]
    raw_coverage_starts = [str(row["coverage_started_at"]) for row in checkpoints if row["coverage_started_at"]]
    raw_root_times = [
        str(row[key])
        for row in rows
        for key in ("baseline_started_at", "baseline_completed_at")
        if row[key]
    ]
    last_events = [value for value in map(_safe_public_timestamp, raw_last_events) if value]
    coverage_starts = [value for value in map(_safe_public_timestamp, raw_coverage_starts) if value]
    timestamp_integrity_invalid = any(
        not _safe_public_timestamp(value)
        for value in (*raw_last_events, *raw_coverage_starts, *raw_root_times)
    )
    pending_event_count = _document_change_pending_count(conn)
    enabled_rows = [row for row in rows if row["enabled"]]
    checkpoint_complete = bool(enabled_rows) and len(checkpoints) >= len(enabled_rows) and all(
        str(row["coverage_status"] or "pending") == "complete" for row in checkpoints
    )
    is_complete = bool(
        enabled
        and baseline_status == "complete"
        and checkpoint_complete
        and len(coverage_starts) == len(enabled_rows)
        and not sum(int(row["event_gap_count"] or 0) for row in checkpoints)
        and pending_event_count == 0
        and not _resume_pending(conn)
        and not timestamp_integrity_invalid
        and not root_status_integrity_invalid
    )
    coverage = {
        "enabled": enabled,
        "export_enabled": export_enabled,
        "baseline_status": baseline_status,
        "root_count": len(enabled_rows),
        "baseline_indexed_files": sum(indexed_by_root.get(str(row["root_id"]), 0) for row in enabled_rows),
        "event_gap_count": sum(int(row["event_gap_count"] or 0) for row in checkpoints),
        "benign_event_count": sum(int(row["benign_event_count"] or 0) for row in checkpoints),
        "last_event_at": max(last_events, default=""),
        "coverage_started_at": max(coverage_starts, default=""),
        "coverage_status": "complete" if is_complete else ("disabled" if not enabled else "partial"),
        "is_complete": is_complete,
        "pending_event_count": pending_event_count,
        "resume_pending": _resume_pending(conn),
    }
    limitations: list[str] = []
    if not enabled:
        limitations.append("document_change_tracking_disabled")
    elif not is_complete:
        limitations.append("coverage_incomplete")
    if not export_enabled:
        limitations.append("document_change_export_disabled")
    database_path = _connection_database_path(conn)
    registry_status, registry, registry_revision = (
        _load_runtime_registry(database_path.parent)
        if database_path is not None
        else ("missing", [], None)
    )
    root_refs_by_id = {
        str(item["root_id"]): str(item["root_ref"])
        for item in registry
        if _valid_root_ref(item.get("root_ref"))
    } if registry_status == "valid" else {}
    safe_roots = []
    internal_root_ids = {str(row["root_id"]) for row in rows}
    authorization_lock_valid = bool(
        not force_authorization_unavailable
        and _authorization_lock_is_valid_readonly(conn)
    )
    registry_root_ids = {
        str(item["root_id"]) for item in registry
    } if registry_status in {"valid", "legacy"} else set()
    database_revision = _root_authorization_revision(conn)
    revision_matches = (
        registry_status == "valid"
        and database_revision is not None
        and registry_revision == database_revision
    )
    registry_bindings_valid = (
        _registry_root_bindings_valid(conn, registry)
        if registry_status == "valid"
        else False
    )
    control_set_complete = (
        registry_status == "valid"
        and revision_matches
        and registry_root_ids == internal_root_ids
        and registry_bindings_valid
    )
    authorization_set_incomplete = (
        registry_status in {"invalid", "legacy"}
        or database_revision is None
        or timestamp_integrity_invalid
        or root_status_integrity_invalid
        or not authorization_lock_valid
        or (registry_status == "valid" and not revision_matches)
        or (bool(internal_root_ids or registry_root_ids) and not control_set_complete)
    )
    if authorization_set_incomplete:
        coverage["is_complete"] = False
        coverage["coverage_status"] = "partial" if enabled else "disabled"
        if enabled:
            coverage["baseline_status"] = "partial"
        if "coverage_incomplete" not in limitations:
            limitations.append("coverage_incomplete")
        if not authorization_lock_valid:
            limitations.append("authorization_lock_unavailable")
    if control_set_complete and not authorization_set_incomplete:
        for row in enabled_rows:
            root_ref = root_refs_by_id.get(str(row["root_id"]), "")
            label = _safe_root_label(str(row["root_label"] or "Tracked folder"))
            if (
                not root_ref
                or _contains_internal_root_identity(root_ref, internal_root_ids)
                or _contains_internal_root_identity(label, internal_root_ids)
            ):
                safe_roots = []
                break
            safe_roots.append(
                {
                    "root_ref": root_ref,
                    "label": label,
                    "display_path_redacted": label,
                    "enabled": bool(row["enabled"]),
                    "baseline_status": (
                        str(row["baseline_status"])
                        if str(row["baseline_status"]) in PUBLIC_BASELINE_STATUSES
                        else "partial"
                    ),
                    "baseline_started_at": _safe_public_timestamp(row["baseline_started_at"]),
                    "baseline_completed_at": _safe_public_timestamp(row["baseline_completed_at"]),
                    "baseline_indexed_files": indexed_by_root.get(str(row["root_id"]), 0),
                }
            )
    control_set_incomplete = authorization_set_incomplete
    if len(safe_roots) != len(enabled_rows) or control_set_incomplete:
        limitations.append("root_control_refs_incomplete")
    return {
        "source": "filesystem_confirmed",
        "projection_contract": DOCUMENT_OWNER_LOCAL_PROJECTION_VERSION,
        "projection_type": "owner_local_observation",
        "verification_state": "unverified",
        "trusted_export_eligible": False,
        "coverage": coverage,
        "totals": {
            "distinct_files": 0,
            "created": 0,
            "modified": 0,
            "renamed": 0,
            "deleted": 0,
            "detailed": 0,
            "metadata_only": 0,
            "protected": 0,
            "ai_only": 0,
            "user_modified": 0,
            "unattributed": 0,
        },
        "items": [],
        "limitations": limitations,
        "roots": safe_roots,
        "raw_access": False,
    }


def configure_document_changes(
    conn: sqlite3.Connection,
    *,
    enabled: bool | None = None,
    export_enabled: bool | None = None,
    add_root: str | Path | None = None,
    reauthorize_root: str | Path | None = None,
    root_label: str | None = None,
    remove_root: str | Path | None = None,
    suspend_root: str | None = None,
    reset_baseline: bool = False,
    runtime_root: str | Path | None = None,
) -> dict:
    if suspend_root is not None:
        if any(value is not None for value in (enabled, export_enabled, add_root, reauthorize_root, root_label, remove_root)) or reset_baseline:
            raise ValueError("document watch root actions are mutually exclusive")
        return suspend_document_root(conn, suspend_root, runtime_root=runtime_root)
    requires_exclusive = bool(
        enabled is not None
        or export_enabled is not None
        or add_root is not None
        or reauthorize_root is not None
        or remove_root is not None
        or reset_baseline
    )
    if requires_exclusive:
        with document_authorization_lock(conn, exclusive=True):
            return _configure_document_changes_locked(
                conn,
                enabled=enabled,
                export_enabled=export_enabled,
                add_root=add_root,
                reauthorize_root=reauthorize_root,
                root_label=root_label,
                remove_root=remove_root,
                reset_baseline=reset_baseline,
                runtime_root=runtime_root,
            )
    with document_authorization_lock(conn, exclusive=False):
        return _configure_document_changes_locked(
            conn,
            enabled=enabled,
            export_enabled=export_enabled,
            add_root=add_root,
            reauthorize_root=reauthorize_root,
            root_label=root_label,
            remove_root=remove_root,
            reset_baseline=reset_baseline,
            runtime_root=runtime_root,
        )


def _configure_document_changes_locked(
    conn: sqlite3.Connection,
    *,
    enabled: bool | None = None,
    export_enabled: bool | None = None,
    add_root: str | Path | None = None,
    reauthorize_root: str | Path | None = None,
    root_label: str | None = None,
    remove_root: str | Path | None = None,
    reset_baseline: bool = False,
    runtime_root: str | Path | None = None,
) -> dict:
    now = _now()
    root_actions = sum(value is not None for value in (add_root, reauthorize_root, remove_root))
    if root_actions > 1:
        raise ValueError("document watch root actions are mutually exclusive")
    root_to_add = reauthorize_root if reauthorize_root is not None else add_root
    root_registry_mutation = root_to_add is not None or remove_root is not None
    if enabled is False or root_registry_mutation or reset_baseline:
        # Invalidate every in-flight baseline/event snapshot before changing the
        # authorization or comparison state.  Event writers compare this epoch
        # again while holding SQLite's write lock, so a stale worker can either
        # commit before this transaction (and be deleted below) or fail after it;
        # it can never write after a completed revocation/reset.
        bump_document_change_epoch(conn)
        _set_document_change_pending(conn, 0)
    if enabled is not None:
        _set_setting(conn, "document_change_tracking_enabled", "true" if enabled else "false", now)
        if not enabled:
            # A disabled interval is deliberately not reconstructed later.  Drop
            # the comparison baseline now so re-enabling starts with a fresh,
            # event-free baseline instead of claiming edits from the off period.
            conn.execute("DELETE FROM file_watch_state")
            conn.execute(
                "UPDATE file_watch_roots SET baseline_status='pending',baseline_started_at=NULL,"
                "baseline_completed_at=NULL,updated_at=? WHERE enabled=1",
                (now,),
            )
            conn.execute(
                "UPDATE file_watch_checkpoints SET last_event_id=NULL,coverage_status='pending',"
                "event_gap_count=0,coverage_started_at=NULL,last_event_at=NULL,updated_at=?",
                (now,),
            )
    if export_enabled is not None:
        _set_setting(conn, "document_change_export_enabled", "true" if export_enabled else "false", now)
    registry_status, registry, registry_revision = (
        _load_runtime_registry(runtime_root)
        if runtime_root is not None and root_registry_mutation
        else ("missing", [], None)
    )
    previous_registry_status = registry_status
    previous_registry = [dict(item) for item in registry]
    previous_registry_revision = _root_authorization_revision(conn)
    previous_sidecar_revision = (
        registry_revision
        if registry_status == "valid" and registry_revision is not None
        else previous_registry_revision
    )
    new_registry_revision: int | None = None
    if root_registry_mutation:
        if runtime_root is None or previous_registry_revision is None:
            conn.rollback()
            raise ValueError("document watch root registry is unavailable")
        database_root_ids = {
            str(row["root_id"])
            for row in conn.execute("SELECT root_id FROM file_watch_roots").fetchall()
        }
        registry_root_ids = {
            str(item["root_id"])
            for item in registry
        } if registry_status in {"valid", "legacy"} else set()
        missing_initial_registry = (
            registry_status == "missing"
            and not database_root_ids
            and root_to_add is not None
            and remove_root is None
        )
        registry_consistent = (
            registry_status in {"valid", "legacy"}
            and registry_root_ids == database_root_ids
            and _registry_root_bindings_valid(conn, registry)
            and (
                registry_status == "legacy"
                or registry_revision == previous_registry_revision
            )
        )
        force_reauthorization = reauthorize_root is not None
        if force_reauthorization:
            from .context_console import _revoke_document_context_locked

            try:
                for revoked_root_id in sorted(database_root_ids):
                    _revoke_document_context_locked(
                        conn,
                        root_id=revoked_root_id,
                        reason_code="root_authorization_revoked",
                    )
            except sqlite3.Error:
                conn.rollback()
                return {
                    "status": "failed",
                    "reason": "document_root_revocation_failed",
                    "reason_code": "document_revocation_failed",
                    "raw_access": False,
                }
            conn.execute("DELETE FROM file_change_versions")
            conn.execute("DELETE FROM file_change_events")
            conn.execute("DELETE FROM file_watch_coverage_gaps")
            conn.execute("DELETE FROM file_watch_state")
            conn.execute("DELETE FROM file_watch_checkpoints")
            conn.execute("DELETE FROM file_watch_roots")
            registry = []
        elif not (missing_initial_registry or registry_consistent):
            if not force_reauthorization:
                conn.rollback()
                raise ValueError("document watch root registry is invalid or out of sync")
        previous_registry_revision, new_registry_revision = _bump_root_authorization_revision(conn)
    if runtime_root is not None and root_registry_mutation:
        _set_setting(conn, "document_change_roots_initialized", "true", now)
    if root_to_add is not None:
        try:
            path = _resolve_root(root_to_add)
            root_id = _root_id(conn, path)
        except BaseException:
            conn.rollback()
            raise
        label = _safe_root_label(root_label or path.name or "Tracked folder")
        conn.execute(
            "INSERT INTO file_watch_roots(root_id, root_label, enabled, baseline_status, created_at, updated_at) "
            "VALUES(?, ?, 1, 'pending', ?, ?) ON CONFLICT(root_id) DO UPDATE SET "
            "root_label=excluded.root_label, enabled=1, baseline_status='pending', updated_at=excluded.updated_at",
            (root_id, label, now, now),
        )
        conn.execute(
            "INSERT INTO file_watch_checkpoints(root_id, coverage_status, updated_at) VALUES(?, 'pending', ?) "
            "ON CONFLICT(root_id) DO UPDATE SET coverage_status='pending', updated_at=excluded.updated_at",
            (root_id, now),
        )
        previous = next((item for item in registry if item.get("root_id") == root_id), None)
        root_ref = _normalized_root_ref(previous.get("root_ref") if previous else None)
        registry = [item for item in registry if item.get("root_id") != root_id]
        registry.append({"root_id": root_id, "root_ref": root_ref, "root_label": label, "path": str(path)})
    if remove_root is not None:
        remove_value = str(remove_root)
        if remove_value.startswith(ROOT_REF_PREFIX):
            matched_root = next(
                (item for item in registry if item.get("root_ref") == remove_value),
                None,
            )
            if matched_root is None:
                conn.rollback()
                raise ValueError("unknown document watch root reference")
            root_id = str(matched_root["root_id"])
        elif remove_value.startswith("root_"):
            conn.rollback()
            raise ValueError("internal document watch root identifiers are not accepted")
        else:
            try:
                path = _resolve_root(remove_root)
                root_id = _root_id(conn, path)
            except BaseException:
                conn.rollback()
                raise
        from .context_console import _revoke_document_context_locked

        try:
            _revoke_document_context_locked(
                conn,
                root_id=root_id,
                reason_code="root_authorization_revoked",
            )
        except sqlite3.Error:
            conn.rollback()
            return {
                "status": "failed",
                "reason": "document_root_revocation_failed",
                "reason_code": "document_revocation_failed",
                "raw_access": False,
            }
        conn.execute("DELETE FROM file_change_versions WHERE root_id = ?", (root_id,))
        conn.execute("DELETE FROM file_change_events WHERE root_id = ?", (root_id,))
        conn.execute("DELETE FROM file_watch_coverage_gaps WHERE root_id = ?", (root_id,))
        conn.execute("DELETE FROM file_watch_state WHERE root_id = ?", (root_id,))
        conn.execute("DELETE FROM file_watch_checkpoints WHERE root_id = ?", (root_id,))
        conn.execute("DELETE FROM file_watch_roots WHERE root_id = ?", (root_id,))
        registry = [item for item in registry if item.get("root_id") != root_id]
        # The helper stream watches all roots as one authorization set.  Once
        # one root is removed, the remaining roots cannot stay "complete"
        # until the replacement helper establishes a fresh barrier.
        conn.execute(
            "UPDATE file_watch_roots SET baseline_status='pending',baseline_started_at=NULL,"
            "baseline_completed_at=NULL,updated_at=? WHERE enabled=1",
            (now,),
        )
        conn.execute(
            "UPDATE file_watch_checkpoints SET last_event_id=NULL,coverage_status='pending',"
            "event_gap_count=0,coverage_started_at=NULL,last_event_at=NULL,updated_at=? "
            "WHERE root_id IN (SELECT root_id FROM file_watch_roots WHERE enabled=1)",
            (now,),
        )
    if reset_baseline:
        conn.execute("DELETE FROM file_watch_state")
        conn.execute(
            "UPDATE file_watch_roots SET baseline_status='pending', baseline_started_at=NULL, "
            "baseline_completed_at=NULL, updated_at=? WHERE enabled=1",
            (now,),
        )
        conn.execute(
            "UPDATE file_watch_checkpoints SET coverage_status='pending', last_event_id=NULL, "
            "event_gap_count=0, coverage_started_at=NULL, last_event_at=NULL, updated_at=?",
            (now,),
        )
    try:
        if runtime_root is not None and root_registry_mutation:
            if new_registry_revision is None:
                raise RuntimeError("document watch authorization revision was not advanced")
            _write_runtime_registry(runtime_root, registry, new_registry_revision)
        conn.commit()
    except BaseException:
        conn.rollback()
        if runtime_root is not None and root_registry_mutation and previous_sidecar_revision is not None:
            try:
                _restore_runtime_registry(
                    runtime_root,
                    previous_registry_status,
                    previous_registry,
                    previous_sidecar_revision,
                )
            except BaseException:
                # A crash or failed compensation leaves a revision mismatch;
                # status/ensure treat that as incomplete and never re-authorize.
                pass
        raise
    return {"status": "configured", "document_changes": _document_changes_status_locked(conn), "raw_access": False}


def suspend_document_root(conn, root_ref: str, *, runtime_root) -> dict:
    """Revoke future reads without deleting history or resolving a moved root.

    This path accepts only the existing public reference and verifies every
    stored path/HMAC binding. It cannot authorize a replacement path.
    """
    if runtime_root is None or not _valid_root_ref(root_ref):
        raise ValueError("document watch root reference required")
    with document_authorization_lock(conn, exclusive=True):
        _acquire_document_change_write_lock(conn)
        status, registry, revision = _load_runtime_registry(runtime_root)
        rows = {r["root_id"]: dict(r) for r in conn.execute("SELECT root_id,enabled FROM file_watch_roots")}
        if (status != "valid" or revision != _root_authorization_revision(conn)
                or set(rows) != {r["root_id"] for r in registry}):
            conn.rollback()
            raise ValueError("document watch root registry is invalid or out of sync")
        target = next((r for r in registry if r.get("root_ref") == root_ref), None)
        if target is None:
            conn.rollback()
            raise ValueError("unknown document watch root reference")
        sidecar_written = False
        try:
            secret = _existing_hmac_key_readonly(conn)
            for item in registry:
                path = str(item["path"])
                if os.path.normpath(path) != path or not Path(path).is_absolute():
                    raise ValueError("document watch stored path is not canonical")
                expected = "root_" + _hmac(secret, path)[:32]
                if not hmac.compare_digest(expected, item["root_id"]):
                    raise ValueError("document watch stored binding mismatch")
                if item is not target and rows[item["root_id"]]["enabled"]:
                    if str(_resolve_root(path)) != path:
                        raise ValueError("document watch active binding mismatch")
            if not rows[target["root_id"]]["enabled"]:
                conn.rollback()
                return {"status": "configured", "raw_access": False}
            bump_document_change_epoch(conn)
            now = _now()
            conn.execute("UPDATE file_watch_roots SET enabled=0,updated_at=? WHERE root_id=?", (now, target["root_id"]))
            conn.execute("UPDATE file_watch_checkpoints SET last_event_id=NULL,coverage_status='partial',coverage_started_at=NULL,updated_at=? WHERE root_id IN (SELECT root_id FROM file_watch_roots WHERE enabled=1)", (now,))
            _, next_revision = _bump_root_authorization_revision(conn)
            sidecar_written = True
            _write_runtime_registry(runtime_root, registry, next_revision)
            conn.commit()
        except BaseException:
            conn.rollback()
            # Preserve the original sidecar revision after a failed database
            # commit; interrupted compensation remains fail-closed.
            if sidecar_written:
                _write_runtime_registry(runtime_root, registry, revision)
            raise
    return {"status": "configured", "raw_access": False}


def ensure_default_document_roots(conn: sqlite3.Connection, runtime_root: str | Path, *, home: str | Path | None = None) -> list[dict]:
    with document_authorization_lock(conn, exclusive=True):
        return _ensure_default_document_roots_locked(conn, runtime_root, home=home)


def _ensure_default_document_roots_locked(
    conn: sqlite3.Connection,
    runtime_root: str | Path,
    *,
    home: str | Path | None = None,
) -> list[dict]:
    # Read the sidecar only after taking the same SQLite write lock used by
    # configure. Otherwise a startup normalizer can wait behind an add/remove
    # transaction with a stale registry snapshot and then undo that change.
    _acquire_document_change_write_lock(conn)
    registry_status, registry, registry_revision = _load_runtime_registry(runtime_root)
    initialized = _setting_bool(conn, "document_change_roots_initialized", False)
    database_root_ids = {
        str(row["root_id"])
        for row in conn.execute("SELECT root_id FROM file_watch_roots").fetchall()
    }
    database_revision = _root_authorization_revision(conn)
    registry_root_ids = {
        str(item["root_id"])
        for item in registry
    } if registry_status in {"valid", "legacy"} else set()
    registry_consistent = bool(
        database_revision is not None
        and registry_status in {"valid", "legacy"}
        and registry_root_ids == database_root_ids
        and _registry_root_bindings_valid(conn, registry)
        and (
            registry_status == "legacy"
            or registry_revision == database_revision
        )
    )
    if registry_consistent:
        _set_setting(conn, "document_change_roots_initialized", "true", _now())
        normalized: list[dict] = []
        now = _now()
        for item in registry:
            row = conn.execute("SELECT enabled FROM file_watch_roots WHERE root_id=?", (item["root_id"],)).fetchone()
            if row is not None and not bool(row["enabled"]):
                normalized.append(dict(item))
                continue
            try:
                path = _resolve_root(str(item.get("path") or ""))
            except (OSError, ValueError):
                conn.rollback()
                return []
            try:
                root_id = _root_id(conn, path)
            except BaseException:
                conn.rollback()
                raise
            label = _safe_root_label(str(item.get("root_label") or path.name or "Tracked folder"))
            conn.execute(
                "UPDATE file_watch_roots SET root_label=?,updated_at=? WHERE root_id=?",
                (label, now, root_id),
            )
            normalized.append(
                {
                    "root_id": root_id,
                    "root_ref": _normalized_root_ref(item.get("root_ref")),
                    "root_label": label,
                    "path": str(path),
                }
            )
        try:
            _write_runtime_registry(runtime_root, normalized, database_revision)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return normalized
    if initialized or database_root_ids or registry_status in {"invalid", "valid", "legacy"}:
        # Missing, malformed, revision-skewed, or path-mismatched authorization
        # is never reconciled from the sidecar. Preserve both stores for audit,
        # mark coverage partial, and require an explicit safe reconfiguration.
        now = _now()
        _set_setting(conn, "document_change_roots_initialized", "true", now)
        conn.execute(
            "UPDATE file_watch_roots SET baseline_status='partial',updated_at=? WHERE enabled=1",
            (now,),
        )
        conn.execute(
            "UPDATE file_watch_checkpoints SET last_event_id=NULL,coverage_status='partial',"
            "event_gap_count=MAX(1,event_gap_count),coverage_started_at=NULL,updated_at=?",
            (now,),
        )
        conn.commit()
        return []
    for label, path in default_document_roots(home):
        if path.exists() and path.is_dir() and not path.is_symlink():
            _configure_document_changes_locked(
                conn,
                add_root=path,
                root_label=label,
                runtime_root=runtime_root,
            )
    return _read_runtime_registry(runtime_root)


def runtime_document_roots(runtime_root: str | Path) -> list[dict]:
    return _read_runtime_registry(runtime_root)


def defer_document_change_startup_reconcile(
    conn: sqlite3.Connection,
    *,
    observed_at: str | None = None,
    preserve_valid: bool = False,
) -> dict:
    """Fail closed when the daemon deliberately defers its full startup scan."""
    now = observed_at or _now()
    try:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        if not _setting_bool(conn, "document_change_tracking_enabled", True):
            conn.commit()
            return {
                "status": "disabled",
                "root_count": 0,
                "raw_access": False,
            }
        root_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM file_watch_roots WHERE enabled=1"
            ).fetchone()[0]
        )
        pending_before_startup = _document_change_pending_count(conn)
        if preserve_valid and root_count:
            saved = conn.execute("SELECT r.baseline_status,c.coverage_status,c.last_event_id FROM file_watch_roots r "
                                 "LEFT JOIN file_watch_checkpoints c ON c.root_id=r.root_id WHERE r.enabled=1").fetchall()
            valid = pending_before_startup == 0 and len(saved) == root_count and all(row["baseline_status"] == "complete"
                and row["coverage_status"] == "complete" and str(row["last_event_id"] or "").isdigit() for row in saved)
            if valid:
                _set_document_change_pending(conn, 0)
                _set_resume_pending(conn, True)
                conn.commit()
                return {"status": "replay_pending", "root_count": root_count, "raw_access": False}
        if pending_before_startup:
            # A cold start cannot recover process-local buffered entries.  A
            # later file may already have advanced the shared cursor past a
            # skipped entry, so retaining the cursor alone cannot prove that
            # every intermediate write was observed.  Persist the attribution
            # gap before resetting the old process's temporary count.  Both
            # operations roll back together if the startup transaction fails.
            for row in conn.execute(
                "SELECT root_id FROM file_watch_roots WHERE enabled=1"
            ).fetchall():
                _record_gap(
                    conn, str(row["root_id"]), now, "",
                    reason_code="startup_pending_events",
                )
        _set_resume_pending(conn, False)
        conn.execute(
            "UPDATE file_watch_roots SET baseline_status='partial',"
            "baseline_started_at=NULL,baseline_completed_at=NULL,updated_at=? "
            "WHERE enabled=1",
            (now,),
        )
        conn.execute(
            "INSERT INTO file_watch_checkpoints("
            "root_id,last_event_id,coverage_status,event_gap_count,coverage_started_at,last_event_at,updated_at"
            ") SELECT r.root_id,NULL,'partial',1,NULL,NULL,? "
            "FROM file_watch_roots r WHERE r.enabled=1 "
            "AND NOT EXISTS (SELECT 1 FROM file_watch_checkpoints c WHERE c.root_id=r.root_id)",
            (now,),
        )
        conn.execute(
            "UPDATE file_watch_checkpoints SET coverage_status='partial',"
            "event_gap_count=MAX(1,event_gap_count),coverage_started_at=NULL,updated_at=? "
            "WHERE root_id IN (SELECT root_id FROM file_watch_roots WHERE enabled=1)",
            (now,),
        )
        # Buffered worker queues are process-local.  After a cold start no such
        # queue survives, so expose the durable coverage gap instead of a stale
        # pending count.
        _set_document_change_pending(conn, 0)
        conn.commit()
        return {
            "status": "deferred",
            "root_count": root_count,
            "raw_access": False,
        }
    except BaseException:
        conn.rollback()
        raise


@_baseline_git_pass
def build_document_baseline(
    conn: sqlite3.Connection,
    root_path: str | Path,
    *,
    root_label: str | None = None,
    paths: Iterable[str | Path] | None = None,
    observed_at: str | None = None,
    batch_size: int = 200,
    batch_sleep_seconds: float = 0.05,
    finalize_coverage: bool = True,
    content_allowed_check=None,
    expected_epoch: int | None = None,
    deferred_delete_revisions: dict[str, str] | None = None,
    _progress_heartbeat=None,
) -> dict:
    _metric_recorder = _DOCUMENT_BASELINE_METRIC_RECORDER.get()
    _live_session_probe = _DOCUMENT_BASELINE_LIVE_SESSION_PROBE.get()
    metrics_enabled = _metric_recorder is not None

    def record_metric(name: str, value: int = 1) -> None:
        if not metrics_enabled:
            return
        try:
            _metric_recorder(name, max(0, int(value)))
        except Exception:
            # Optional diagnostics must never change baseline behavior.
            pass

    def metric_started() -> int:
        return time.monotonic_ns() if metrics_enabled else 0

    def record_elapsed(name: str, started_ns: int) -> None:
        if metrics_enabled:
            record_metric(name, time.monotonic_ns() - started_ns)

    if deferred_delete_revisions is not None:
        deferred_delete_revisions.clear()
    root = _resolve_root(root_path)
    now = observed_at or _now()
    root_id = _root_id(conn, root)
    baseline_epoch = _document_change_epoch(conn) if expected_epoch is None else int(expected_epoch)
    existing_root = conn.execute("SELECT enabled FROM file_watch_roots WHERE root_id=?", (root_id,)).fetchone()
    if existing_root is None:
        record_metric("failure_root_removed")
        # Registration is an explicit configuration action.  A stale worker
        # must never resurrect a root that Settings just removed.
        return {
            "root_id": root_id,
            "baseline_status": "unconfigured",
            "files_indexed": 0,
            "files_ignored": 0,
            "files_failed": 0,
            "files_skipped": 0,
            "raw_access": False,
        }
    if not _setting_bool(conn, "document_change_tracking_enabled", True) or not bool(existing_root["enabled"]):
        record_metric("failure_tracking_disabled" if not _setting_bool(conn, "document_change_tracking_enabled", True) else "failure_root_removed")
        return {
            "root_id": root_id,
            "baseline_status": "disabled" if not _setting_bool(conn, "document_change_tracking_enabled", True) else "removed",
            "files_indexed": 0,
            "files_ignored": 0,
            "files_failed": 0,
            "files_skipped": 0,
            "raw_access": False,
        }
    initial_rejection = _begin_document_change_write(conn, root_id, baseline_epoch)
    if initial_rejection:
        record_metric("failure_authorization_changed")
        return {
            "root_id": root_id,
            "baseline_status": (
                "disabled" if initial_rejection == "tracking_disabled"
                else "removed" if initial_rejection == "root_not_configured"
                else "authorization_changed"
            ),
            "files_indexed": 0,
            "files_ignored": 0,
            "files_failed": 0,
            "files_skipped": 0,
            "raw_access": False,
        }
    conn.execute(
        "UPDATE file_watch_roots SET baseline_status='building', baseline_started_at=?, updated_at=? WHERE root_id=?",
        (now, now, root_id),
    )
    if not finalize_coverage:
        conn.execute(
            "UPDATE file_watch_checkpoints SET coverage_status='partial',coverage_started_at=NULL,updated_at=? "
            "WHERE root_id=?",
            (now, root_id),
        )
    from .presentation_scope import for_connection
    baseline_display_scope = for_connection(conn)
    unseen_file_revisions = {
        str(row["file_id"]): str(row["updated_at"])
        for row in conn.execute(
            "SELECT file_id,updated_at,project_label,file_name_redacted FROM file_watch_state WHERE root_id=?",
            (root_id,),
        ).fetchall() if not baseline_display_scope.hidden(dict(row))
    }
    conn.commit()
    if _progress_heartbeat is not None and _progress_heartbeat.touch(conn):
        conn.commit()
    candidates = paths if paths is not None else _iter_safe_files(root)
    seen = 0
    processed = 0
    ignored = 0
    failed = 0
    written = 0
    unchanged = 0
    deleted = 0
    locked_during_scan = False
    authorization_revoked = False
    pending_snapshots: list[tuple[dict, str, bool, tuple | None]] = []
    pending_requires_live_content = False
    pending_snapshot_bytes = 0

    def persisted_snapshot_size(snapshot: dict) -> int:
        # The batch never retains raw ``structure`` units.  Bound the remaining
        # governed strings as well as the row count so a few large hashed
        # structure payloads cannot create an oversized WAL transaction.
        fields = (
            "file_id",
            "file_name_redacted",
            "file_type",
            "device_id",
            "inode_id",
            "modified_at",
            "content_hmac",
            "structure_hmac_json",
            "detail_level",
            "project_label",
        )
        return 256 + sum(
            len(str(snapshot.get(field) or "").encode("utf-8"))
            for field in fields
        ) + len(json.dumps(snapshot.get("privacy_flags") or []).encode("utf-8"))

    def clear_pending_snapshots() -> None:
        nonlocal pending_requires_live_content, pending_snapshot_bytes
        pending_snapshots.clear()
        pending_requires_live_content = False
        pending_snapshot_bytes = 0

    def discard_pending_snapshots() -> None:
        nonlocal failed
        record_metric("failure_batch_discarded", len(pending_snapshots))
        failed += len(pending_snapshots)
        clear_pending_snapshots()

    def flush_pending_snapshots() -> str:
        """Persist one bounded batch behind a single exact authorization gate.

        Filesystem reads are complete before this function starts.  Every row
        is re-read while holding SQLite's writer lock, so an optimistic
        baseline observation can never overwrite a newer state row.  If the
        batch contains fingerprints captured with content access, probe the
        live session both before waiting for the exact database gate and again
        after the writer lock is held; a denial discards the in-memory batch
        and leaves coverage partial.
        """
        nonlocal failed, locked_during_scan, pending_requires_live_content
        nonlocal seen, unchanged, written
        if not pending_snapshots:
            return ""
        if pending_requires_live_content:
            if content_allowed_check is None:
                live_content_allowed = not _daemon_state_bool(conn, "locked")
            else:
                try:
                    live_content_allowed = bool(content_allowed_check())
                except Exception:
                    live_content_allowed = False
            if not live_content_allowed:
                locked_during_scan = True
                record_metric("failure_session_content_denied")
                discard_pending_snapshots()
                return ""
        rejection = _begin_document_change_write(conn, root_id, baseline_epoch)
        if rejection:
            record_metric("failure_authorization_changed")
            discard_pending_snapshots()
            return rejection
        try:
            def locked_batch_session_allowed() -> bool:
                if not pending_requires_live_content:
                    return True
                if _live_session_probe is not None:
                    try:
                        return bool(_live_session_probe())
                    except Exception:
                        return False
                if content_allowed_check is not None:
                    # A generic content callback may open another SQLite
                    # connection.  Calling it while this connection owns the
                    # IMMEDIATE lock can self-deadlock; without the worker's
                    # private session-only probe, fail closed instead.
                    return False
                return not _daemon_state_bool(conn, "locked")

            if not locked_batch_session_allowed():
                conn.rollback()
                locked_during_scan = True
                record_metric("failure_session_content_denied")
                discard_pending_snapshots()
                return ""
            batch_seen = 0
            batch_unchanged = 0
            batch_written = 0
            batch_conflicts = 0
            for (
                pending_snapshot,
                captured_at,
                _used_content,
                optimistic_revision,
            ) in pending_snapshots:
                pending_file_id = str(pending_snapshot["file_id"])
                current = conn.execute(
                    "SELECT * FROM file_watch_state WHERE root_id=? AND file_id=?",
                    (root_id, pending_file_id),
                ).fetchone()
                if _baseline_snapshot_matches(current, pending_snapshot):
                    batch_unchanged += 1
                    batch_seen += 1
                    continue
                current_revision = tuple(current) if current is not None else None
                if current_revision != optimistic_revision:
                    # A second same-epoch writer produced a divergent state
                    # after the optimistic read.  Preserve the newer row and
                    # keep coverage partial instead of overwriting it.
                    batch_conflicts += 1
                    continue
                _upsert_state(conn, root_id, pending_snapshot, captured_at)
                batch_written += 1
                batch_seen += 1
            # Fresh session evidence can become unknown or off-console while a
            # large WAL transaction is applying its rows.  Probe once more
            # before commit so that transition rolls the whole batch back
            # instead of persisting up to 200 no-longer-authorized fingerprints.
            if not locked_batch_session_allowed():
                conn.rollback()
                locked_during_scan = True
                record_metric("failure_session_content_denied")
                discard_pending_snapshots()
                return ""
            conn.commit()
            unchanged += batch_unchanged
            written += batch_written
            failed += batch_conflicts
            record_metric("failure_concurrent_state_change", batch_conflicts)
            seen += batch_seen
        except BaseException:
            conn.rollback()
            raise
        clear_pending_snapshots()
        return ""

    try:
        for candidate in candidates:
            if _progress_heartbeat is not None and not conn.in_transaction:
                if _progress_heartbeat.touch(conn):
                    conn.commit()
            record_metric("candidate_count")
            path = Path(candidate)
            if not path.is_absolute():
                path = root / path
            safe_path_started = metric_started()
            try:
                path_is_safe = _safe_path(root, path)
            finally:
                record_elapsed("safe_path_elapsed_ns", safe_path_started)
            if not path_is_safe:
                ignored += 1
                continue
            try:
                pre_snapshot_gate_started = metric_started()
                try:
                    if not _setting_bool(conn, "document_change_tracking_enabled", True):
                        authorization_revoked = True
                        record_metric("failure_tracking_disabled")
                        failed += 1
                        break
                    authorized_root = conn.execute(
                        "SELECT enabled FROM file_watch_roots WHERE root_id=?",
                        (root_id,),
                    ).fetchone()
                    if authorized_root is None or not bool(authorized_root["enabled"]):
                        authorization_revoked = True
                        record_metric("failure_root_removed")
                        failed += 1
                        break
                    if _daemon_state_bool(conn, "pause_mode"):
                        record_metric("failure_paused")
                        failed += 1
                        break
                    if content_allowed_check is None:
                        live_content_allowed = not _daemon_state_bool(conn, "locked")
                    else:
                        try:
                            live_content_allowed = bool(content_allowed_check())
                        except Exception:
                            live_content_allowed = False
                    locked_now = not live_content_allowed
                    if locked_now:
                        record_metric("failure_session_content_denied")
                    locked_during_scan = locked_during_scan or locked_now
                finally:
                    record_elapsed(
                        "pre_snapshot_gate_inclusive_elapsed_ns",
                        pre_snapshot_gate_started,
                    )
                snapshot_started = metric_started()
                try:
                    snapshot = _snapshot(
                        conn,
                        root,
                        path,
                        allow_content=not locked_now,
                        content_allowed_check=content_allowed_check if not locked_now else None,
                    )
                    # ``structure`` may contain transient raw units used only to
                    # derive the governed hashes.  Baseline persistence never
                    # consumes it, so do not retain raw units in a write batch.
                    snapshot.pop("structure", None)
                finally:
                    record_elapsed("snapshot_inclusive_elapsed_ns", snapshot_started)
            except DisplaySourceRetired:
                unseen_file_revisions.pop(_file_id(conn, root, path), None)
                ignored += 1
                continue
            except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
                reason = _baseline_snapshot_failure_reason(exc)
                record_metric("failure_" + reason)
                if metrics_enabled and reason == "snapshot_io_error":
                    try:
                        step, error_number = _baseline_snapshot_io_details(exc)
                        record_metric("io_step_" + step)
                        record_metric("io_errno_" + error_number)
                    except Exception:
                        # Diagnostics cannot interrupt a scan or change its
                        # failure count, coverage or subsequent file handling.
                        pass
                failed += 1
                continue
            # Per-file scan time lets a later buffered FSEvent distinguish "the
            # baseline observed this content after the event" from an unchanged
            # replay.  Without it, a save during a long first scan could disappear
            # into the baseline and coverage would be reported as complete.
            database_write_started = metric_started()
            try:
                if conn.in_transaction:
                    raise RuntimeError("baseline candidate gate requires autocommit")
                conn.execute("BEGIN")
                try:
                    rejection = _document_change_authorization_rejection(
                        conn,
                        root_id,
                        baseline_epoch,
                    )
                    file_id = str(snapshot["file_id"])
                    previous = None
                    snapshot_matches = False
                    if not rejection:
                        previous = conn.execute(
                            "SELECT * FROM file_watch_state WHERE root_id=? AND file_id=?",
                            (root_id, file_id),
                        ).fetchone()
                        snapshot_matches = _baseline_snapshot_matches(previous, snapshot)
                finally:
                    # Never try to upgrade a WAL reader snapshot to a writer.
                    # A real write starts from a fresh IMMEDIATE transaction and
                    # repeats both authorization and row comparison under lock.
                    conn.rollback()
                if rejection:
                    authorization_revoked = True
                    record_metric("failure_authorization_changed")
                    failed += 1
                    break
                unseen_file_revisions.pop(file_id, None)
                if snapshot_matches:
                    unchanged += 1
                    seen += 1
                else:
                    used_content = not locked_now
                    optimistic_revision = (
                        tuple(previous) if previous is not None else None
                    )
                    pending_snapshots.append(
                        (snapshot, _now(), used_content, optimistic_revision)
                    )
                    pending_snapshot_bytes += persisted_snapshot_size(snapshot)
                    pending_requires_live_content = bool(
                        pending_requires_live_content or used_content
                    )
                processed += 1
                if not snapshot_matches:
                    if (
                        len(pending_snapshots) >= DOCUMENT_BASELINE_WRITE_BATCH_SIZE
                        or pending_snapshot_bytes
                        >= DOCUMENT_BASELINE_WRITE_BATCH_MAX_BYTES
                    ):
                        rejection = flush_pending_snapshots()
                        if rejection:
                            authorization_revoked = True
                            break
            finally:
                record_elapsed("database_write_elapsed_ns", database_write_started)
            if batch_size > 0 and processed % batch_size == 0:
                if batch_sleep_seconds > 0:
                    throttle_started = metric_started()
                    try:
                        time.sleep(batch_sleep_seconds)
                    finally:
                        record_elapsed(
                            "batch_throttle_inclusive_elapsed_ns",
                            throttle_started,
                        )
    except (GitIgnoreEvaluationError, OSError) as exc:
        record_metric("failure_git_ignore_unavailable" if isinstance(exc, GitIgnoreEvaluationError) else "failure_traversal_failed")
        failed += 1
    if authorization_revoked:
        discard_pending_snapshots()
    else:
        database_write_started = metric_started()
        try:
            final_batch_rejection = flush_pending_snapshots()
        finally:
            record_elapsed("database_write_elapsed_ns", database_write_started)
        if final_batch_rejection:
            authorization_revoked = True
    tracking_still_enabled = _setting_bool(conn, "document_change_tracking_enabled", True)
    root_still_authorized = conn.execute(
        "SELECT 1 FROM file_watch_roots WHERE root_id=? AND enabled=1",
        (root_id,),
    ).fetchone() is not None
    if authorization_revoked or not tracking_still_enabled or not root_still_authorized:
        if not authorization_revoked:
            record_metric("failure_tracking_disabled" if not tracking_still_enabled else "failure_root_removed")
        # Configuration/reset owns cleanup in the new authorization epoch.  A
        # stale scanner must never delete or downgrade state produced by the
        # replacement baseline after it notices that its epoch was revoked.
        # Returning without a write is the only safe action here.
        return {
            "root_id": root_id,
            "baseline_status": (
                "disabled" if not tracking_still_enabled
                else "removed" if not root_still_authorized
                else "authorization_changed"
            ),
            "files_indexed": 0,
            "files_ignored": ignored,
            "files_failed": failed,
            "files_skipped": ignored + failed,
            "raw_access": False,
        }
    final_rejection = _begin_document_change_write(conn, root_id, baseline_epoch)
    if final_rejection:
        record_metric("failure_authorization_changed")
        return {
            "root_id": root_id,
            "baseline_status": (
                "disabled" if final_rejection == "tracking_disabled"
                else "removed" if final_rejection == "root_not_configured"
                else "authorization_changed"
            ),
            "files_indexed": 0,
            "files_ignored": ignored,
            "files_failed": failed,
            "files_skipped": ignored + failed,
            "raw_access": False,
        }
    final_status = "partial" if failed or locked_during_scan else "complete"
    # A migrated child can now be an ignored symlink, so enumeration cannot
    # associate its old hashed state with individual paths. Keep all ambiguous
    # absence candidates in this root rather than deleting history by guess.
    # This changes no active file read and never follows the retired child.
    retired_child = any(Path(path) != root and Path(path).is_relative_to(root)
                        for path in baseline_display_scope.paths)
    if retired_child and unseen_file_revisions:
        unseen_file_revisions.clear()
        _record_gap(conn, root_id, now, '', downgrade_baseline=False,
                    reason_code='retired_subtree_history_preserved')
    if (
        final_status == "complete"
        and finalize_coverage
        and not _baseline_delete_revisions_match(
            conn,
            root_id,
            unseen_file_revisions,
        )
    ):
        # A concurrent writer changed a row that this scan considered absent.
        # Never delete or promote coverage from that stale plan.
        final_status = "partial"
        failed += 1
        record_metric("failure_concurrent_state_change")
    persisted_status = final_status if finalize_coverage or final_status != "complete" else "building"
    completed_at = now if observed_at is not None else _now()
    conn.execute(
        "UPDATE file_watch_roots SET baseline_status=?, baseline_completed_at=?, updated_at=? WHERE root_id=?",
        (
            persisted_status,
            completed_at if finalize_coverage else None,
            completed_at,
            root_id,
        ),
    )
    if final_status != "complete":
        conn.execute(
            "UPDATE file_watch_checkpoints SET coverage_status='partial',"
            "event_gap_count=MAX(1,event_gap_count),coverage_started_at=NULL,updated_at=? WHERE root_id=?",
            (completed_at, root_id),
        )
    elif finalize_coverage:
        conn.execute(
            "UPDATE file_watch_checkpoints SET coverage_status='complete',event_gap_count=0,"
            "coverage_started_at=?,updated_at=? WHERE root_id=?",
            (completed_at, completed_at, root_id),
        )
        # A successful reconcile supersedes short-lived rename tombstones and
        # removes files that are now deleted or newly ignored.
        deleted = _delete_baseline_state_revisions(
            conn,
            root_id,
            unseen_file_revisions,
        )
    else:
        if deferred_delete_revisions is not None:
            # The barriered worker owns these deletes.  It applies them only
            # after the helper barrier proves this exact scan pass clean.
            deferred_delete_revisions.update(unseen_file_revisions)
        conn.execute(
            "UPDATE file_watch_checkpoints SET coverage_status='partial',coverage_started_at=NULL,updated_at=? "
            "WHERE root_id=?",
            (completed_at, root_id),
        )
    conn.commit()
    return {
        "root_id": root_id,
        "baseline_status": final_status,
        "files_indexed": seen,
        "files_ignored": ignored,
        "files_failed": failed,
        "files_skipped": ignored + failed,
        "files_written": written,
        "files_unchanged": unchanged,
        "files_deleted": deleted,
        "raw_access": False,
    }


def process_document_event(
    conn: sqlite3.Connection,
    event: dict,
    *,
    root_path: str | Path,
    root_label: str | None = None,
    allow_content: bool = True,
    snapshot_override: dict | None = None,
    snapshot_failed: bool = False,
    content_allowed_check=None,
    expected_epoch: int | None = None,
) -> dict:
    # A caller-owned transaction could retain an older WAL snapshot across the
    # authorization lease and make the exact recheck below observe stale state.
    # Do not commit or roll back somebody else's transaction; fail closed before
    # resolving identity or touching document content.
    if conn.in_transaction:
        return {
            "recorded": False,
            "reason": "connection_transaction_active",
            "raw_access": False,
        }

    # Bind the event to the authorization epoch that existed before acquiring
    # the cross-process lease.  A revoke/reset that wins the lease first is
    # observed by the locked recheck below; an event that wins first commits
    # before the destructive operation, which then owns cleanup.  Root identity
    # is deliberately derived only after taking the lease: first-use verifier
    # migration may write SQLite, so doing it here would invert the global
    # authorization-lock -> SQLite-lock order.
    root = _resolve_root(root_path)
    event_epoch = (
        _document_change_epoch(conn)
        if expected_epoch is None
        else int(expected_epoch)
    )
    with document_authorization_lock(conn, exclusive=True, initialize_if_missing=True):
        try:
            result = _process_document_event_locked(
                conn,
                event,
                root_path=root_path,
                root_label=root_label,
                allow_content=allow_content,
                snapshot_override=snapshot_override,
                snapshot_failed=snapshot_failed,
                content_allowed_check=content_allowed_check,
                expected_epoch=event_epoch,
            )
            return result
        except DocumentContextProjectionError as exc:
            return {
                "recorded": False,
                "status": "failed",
                "reason": "document_context_projection_failed",
                "reason_code": str(exc) or "document_context_projection_failed",
                "raw_access": False,
            }
        except sqlite3.Error:
            return {
                "recorded": False,
                "status": "failed",
                "reason": "document_transaction_failed",
                "reason_code": "database_write_failed",
                "raw_access": False,
            }
        finally:
            # First-use HMAC verifier migration is intentionally performed only
            # after the authorization lease is held.  It can open a transaction
            # before an early return or an unrelated exception.  The wrapper
            # admitted only a clean caller connection, so every transaction
            # still open here belongs to this event and must be rolled back
            # before the exclusive privacy lease is released.
            if conn.in_transaction:
                conn.rollback()


def _process_document_event_locked(
    conn: sqlite3.Connection,
    event: dict,
    *,
    root_path: str | Path,
    root_label: str | None = None,
    allow_content: bool = True,
    snapshot_override: dict | None = None,
    snapshot_failed: bool = False,
    content_allowed_check=None,
    expected_epoch: int | None = None,
) -> dict:
    """Turn one transient helper event into safe state and an optional event row.

    The absolute path is intentionally accepted only as an in-memory argument and
    is never put into SQLite or the returned payload.  The optional content
    predicate runs while the caller owns the exclusive authorization lease; it
    must be a bounded live-session probe and must not acquire another document
    authorization lease or open a SQLite transaction.
    """
    root = _resolve_root(root_path)
    path = Path(str(event.get("path") or "")).expanduser()
    if not path.is_absolute():
        path = root / path
    observed = _iso(str(event.get("observed_at") or event.get("timestamp") or ""))
    root_id = _root_id(conn, root)
    event_epoch = _document_change_epoch(conn) if expected_epoch is None else int(expected_epoch)
    rejection = _document_change_authorization_rejection(
        conn,
        root_id,
        event_epoch,
    )
    if rejection:
        return {"recorded": False, "reason": rejection, "raw_access": False}
    helper_flags = {str(item) for item in (event.get("flags") or [])}
    event_id = str(event.get("event_id") or "")
    merged_count = max(0, int(event.get("_superseded_merged_count") or 0))
    if not event_id.isdigit():
        rejection = _begin_document_change_write(conn, root_id, event_epoch)
        if rejection:
            return {"recorded": False, "reason": rejection, "raw_access": False}
        _record_gap(conn, root_id, observed, "")
        conn.commit()
        return {"recorded": False, "coverage_status": "partial", "reason": "invalid_event_id", "raw_access": False}
    if helper_flags & CONTROL_EVENT_FLAGS:
        rejection = _begin_document_change_write(conn, root_id, event_epoch)
        if rejection:
            return {"recorded": False, "reason": rejection, "raw_access": False}
        _record_gap(
            conn,
            root_id,
            observed,
            event_id,
            reset_event_id="event_ids_wrapped" in helper_flags,
        )
        conn.commit()
        return {"recorded": False, "coverage_status": "partial", "reason": "event_gap", "raw_access": False}
    try:
        definitely_ignored = _definitely_ignored_event(
            root,
            path,
            is_directory="item_is_dir" in helper_flags,
            is_symlink="item_is_symlink" in helper_flags,
        )
    except GitIgnoreEvaluationError:
        rejection = _begin_document_change_write(conn, root_id, event_epoch)
        if rejection:
            return {"recorded": False, "reason": rejection, "raw_access": False}
        _record_gap(conn, root_id, observed, event_id or "git_ignore_unavailable")
        conn.commit()
        return {"recorded": False, "reason": "git_ignore_unavailable", "coverage_status": "partial", "raw_access": False}
    if definitely_ignored and not (
        _is_ignore_control(path) and not _ignore_control_has_skipped_parent(root, path)
    ):
        rejection = _begin_document_change_write(conn, root_id, event_epoch)
        if rejection:
            return {"recorded": False, "reason": rejection, "raw_access": False}
        if _event_id_is_replay(conn, root_id, event_id):
            conn.rollback()
            return {"recorded": False, "reason": "event_replay", "raw_access": False}
        _checkpoint(conn, root_id, event_id, observed)
        conn.commit()
        return {"recorded": False, "reason": "ignored_path", "raw_access": False}
    if _is_ignore_control(path):
        rejection = _begin_document_change_write(conn, root_id, event_epoch)
        if rejection:
            return {"recorded": False, "reason": rejection, "raw_access": False}
        _record_gap(conn, root_id, observed, event_id)
        conn.commit()
        return {
            "recorded": False,
            "reason": "ignore_rules_reconcile_required",
            "coverage_status": "partial",
            "raw_access": False,
        }
    if allow_content and content_allowed_check is not None:
        try:
            live_content_allowed = bool(content_allowed_check())
        except Exception:
            live_content_allowed = False
    else:
        # Direct/internal callers without fresh session evidence retain the
        # legacy fail-closed database gate.  Only the live worker can exercise
        # the explicit locked-console exception.
        live_content_allowed = not _daemon_state_bool(conn, "locked")
    if not live_content_allowed:
        return {"recorded": False, "reason": "locked_deferred", "raw_access": False}
    if _event_id_is_replay(conn, root_id, event_id):
        return {"recorded": False, "reason": "event_replay", "raw_access": False}
    if "item_is_dir" in helper_flags:
        rejection = _begin_document_change_write(conn, root_id, event_epoch)
        if rejection:
            return {"recorded": False, "reason": rejection, "raw_access": False}
        _record_gap(
            conn,
            root_id,
            observed,
            event_id,
            downgrade_baseline=False,
            affects_attribution=False,
            reason_code="directory_event",
        )
        conn.commit()
        return {
            "recorded": False,
            "reason": "directory_reconcile_required",
            "coverage_status": "complete",
            "raw_access": False,
        }
    try:
        safe_path = _safe_path(root, path, allow_missing=True)
    except GitIgnoreEvaluationError:
        rejection = _begin_document_change_write(conn, root_id, event_epoch)
        if rejection:
            return {"recorded": False, "reason": rejection, "raw_access": False}
        _record_gap(conn, root_id, observed, event_id or "git_ignore_unavailable")
        conn.commit()
        return {"recorded": False, "reason": "git_ignore_unavailable", "coverage_status": "partial", "raw_access": False}
    if not safe_path:
        rejection = _begin_document_change_write(conn, root_id, event_epoch)
        if rejection:
            return {"recorded": False, "reason": rejection, "raw_access": False}
        if _event_id_is_replay(conn, root_id, event_id):
            conn.rollback()
            return {"recorded": False, "reason": "event_replay", "raw_access": False}
        _checkpoint(conn, root_id, event_id, observed)
        conn.commit()
        return {"recorded": False, "reason": "ignored_path", "raw_access": False}

    if bool(event.get("_arrived_during_baseline")):
        # The first scan may already have absorbed this event's post-save state,
        # but no trustworthy pre-save version exists.  Do not manufacture a
        # modification; move the complete-coverage boundary past a reconcile.
        rejection = _begin_document_change_write(conn, root_id, event_epoch)
        if rejection:
            return {"recorded": False, "reason": rejection, "raw_access": False}
        _record_gap(conn, root_id, observed, event_id or "baseline_buffered_event")
        conn.commit()
        return {
            "recorded": False,
            "reason": "baseline_gap",
            "coverage_status": "partial",
            "raw_access": False,
        }
    if snapshot_failed:
        rejection = _begin_document_change_write(conn, root_id, event_epoch)
        if rejection:
            return {"recorded": False, "reason": rejection, "raw_access": False}
        _record_gap(conn, root_id, observed, event_id or "snapshot_capture_failed")
        conn.commit()
        return {
            "recorded": False,
            "reason": "snapshot_capture_failed",
            "coverage_status": "partial",
            "raw_access": False,
        }

    file_id = _file_id(conn, root, path)
    trusted_snapshot = snapshot_override if isinstance(snapshot_override, dict) and snapshot_override.get("file_id") == file_id else None
    if trusted_snapshot is not None:
        exists = True
    else:
        try:
            _safe_file_stat(root, path)
            exists = True
        except OSError:
            exists = False
    snapshot = None
    if exists:
        try:
            snapshot = trusted_snapshot or _stable_snapshot(
                conn,
                root,
                path,
                allow_content=allow_content,
                stable_seconds=float(event.get("stable_seconds") or 0.0),
                content_allowed_check=content_allowed_check,
            )
        except DisplaySourceRetired:
            conn.rollback()
            return {"recorded": False, "reason": "source_not_in_display_scope", "raw_access": False}
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile):
            conn.rollback()
            return {"recorded": False, "reason": "unstable_or_unreadable", "raw_access": False}

    # All state/event/checkpoint decisions below are made after acquiring the
    # SQLite write lock and atomically rechecking the arrival epoch.  This closes
    # remove/disable/delete-range races without holding a lock during file I/O.
    rejection = _begin_document_change_write(conn, root_id, event_epoch)
    if rejection:
        return {"recorded": False, "reason": rejection, "raw_access": False}
    if _event_id_is_replay(conn, root_id, event_id):
        conn.rollback()
        return {"recorded": False, "reason": "event_replay", "raw_access": False}
    previous_row = conn.execute("SELECT * FROM file_watch_state WHERE file_id=?", (file_id,)).fetchone()

    if not exists:
        if previous_row is None:
            _checkpoint(conn, root_id, event_id, observed)
            conn.commit()
            return {"recorded": False, "reason": "unknown_delete", "raw_access": False}
        if not bool(previous_row["present"]):
            _checkpoint(conn, root_id, event_id, observed)
            conn.commit()
            return {"recorded": False, "reason": "duplicate_delete", "raw_access": False}
        conn.execute("UPDATE file_watch_state SET present=0, last_seen_at=?, updated_at=? WHERE file_id=?", (observed, observed, file_id))
        previous_detail = str(previous_row["detail_level"] or "metadata_only")
        item = _record_change(
            conn,
            previous_row,
            root_id,
            "deleted",
            observed,
            "检测到受保护文件发生变化；未保留文件名或内容摘要。" if previous_detail == "protected" else "检测到文件已删除。",
            "protected" if previous_detail == "protected" else "metadata_only",
            helper_event_id=event_id,
        )
        _mark_superseded_merged_version(conn, file_id, event_id, observed, merged_count)
        _checkpoint(conn, root_id, event_id, observed)
        conn.commit()
        return {"recorded": True, "item": item, "raw_access": False}

    assert snapshot is not None
    # A tombstone at the same logical path is not the current file identity.
    # Treat a later appearance as a create (or as the destination of a strongly
    # evidenced rename), never as a modification of the deleted file.
    if previous_row is not None and not bool(previous_row["present"]):
        previous_row = None

    root_status = conn.execute(
        "SELECT baseline_status,baseline_completed_at FROM file_watch_roots WHERE root_id=?",
        (root_id,),
    ).fetchone()
    if previous_row is None and root_status and str(root_status["baseline_status"] or "") != "complete":
        _upsert_state(conn, root_id, snapshot, observed)
        _record_gap(conn, root_id, observed, event_id or "baseline_gap")
        conn.commit()
        return {"recorded": False, "reason": "baseline_gap", "coverage_status": "partial", "raw_access": False}

    rename_previous = None
    if previous_row is None and snapshot["inode_id"]:
        rename_previous = conn.execute(
            "SELECT * FROM file_watch_state WHERE file_id<>? AND device_id=? AND inode_id=? "
            "AND content_hmac=? AND present=0 AND datetime(updated_at)>=datetime(?,'-30 seconds') "
            "ORDER BY present DESC,datetime(updated_at) DESC LIMIT 1",
            (snapshot["file_id"], snapshot["device_id"], snapshot["inode_id"], snapshot["content_hmac"], observed),
        ).fetchone()
    if rename_previous is not None:
        # A delete-first FSEvents sequence may already have produced a provisional
        # delete.  Identical device+inode+content within 30 seconds is strong
        # enough to pair the rename; remove only that provisional row.
        provisional_delete = conn.execute(
            "SELECT id FROM file_change_events WHERE file_id=? AND change_kind='deleted' "
            "AND datetime(last_changed_at)>=datetime(?,'-30 seconds') "
            "ORDER BY datetime(last_changed_at) DESC LIMIT 1",
            (rename_previous["file_id"], observed),
        ).fetchone()
        provisional_id = str(provisional_delete["id"]) if provisional_delete else ""
        provisional_versions = conn.execute(
            "SELECT coverage_state,observed_version_count FROM file_change_versions "
            "WHERE aggregate_event_id=?",
            (provisional_id,),
        ).fetchall()
        # Pairing replaces this provisional delete, but must not erase its
        # durable evidence of unobserved intermediate writes.  The old path's
        # process-local debt may already be settled; transfer the actual rows
        # being removed to the final rename inside this same transaction.
        merged_count += sum(
            max(1, int(row["observed_version_count"] or 1))
            for row in provisional_versions
            if str(row["coverage_state"] or "") == "file_unresolved"
            or int(row["observed_version_count"] or 1) > 1
        )
        # Fix the selected identity once. Even when two candidates share
        # a second, evidence and both deletions refer to that exact aggregate.
        conn.execute("DELETE FROM file_change_versions WHERE aggregate_event_id=?", (provisional_id,))
        conn.execute("DELETE FROM file_change_events WHERE id=?", (provisional_id,))
        conn.execute(
            "UPDATE file_change_events SET file_id=?,updated_at=? WHERE file_id=?",
            (snapshot["file_id"], observed, rename_previous["file_id"]),
        )
        conn.execute(
            "UPDATE file_change_versions SET file_id=?,updated_at=? WHERE file_id=?",
            (snapshot["file_id"], observed, rename_previous["file_id"]),
        )
        conn.execute("UPDATE file_watch_state SET present=0, last_seen_at=?, updated_at=? WHERE file_id=?", (observed, observed, rename_previous["file_id"]))
        _upsert_state(conn, root_id, snapshot, observed)
        item = _record_change(
            conn, snapshot, root_id, "renamed", observed, "检测到文件已重命名。",
            snapshot["detail_level"], helper_event_id=event_id,
        )
    elif previous_row is None:
        _upsert_state(conn, root_id, snapshot, observed)
        item = _record_change(
            conn, snapshot, root_id, "created", observed, _created_summary(snapshot),
            snapshot["detail_level"], helper_event_id=event_id,
        )
    elif str(previous_row["content_hmac"] or "") == snapshot["content_hmac"] and int(previous_row["size_bytes"] or 0) == snapshot["size_bytes"]:
        if merged_count:
            # The final snapshot is unchanged, but intermediate queued writes
            # were not individually observed.  Keep that fact durably in the
            # same transaction as the final cursor.  Do not invent a content
            # difference, replace the real content fingerprint, or rely on
            # process-local debt surviving a normal restart.
            _upsert_state(conn, root_id, snapshot, observed)
            detail = "protected" if snapshot["detail_level"] == "protected" else "metadata_only"
            item = _record_change(
                conn, snapshot, root_id, "modified", observed,
                (
                    "检测到受保护文件更新事件；中间版本已合并，归属待确认。"
                    if detail == "protected"
                    else "检测到文件更新事件；当前内容与前次相同，中间版本已合并，归属待确认。"
                ),
                detail, helper_event_id=event_id,
            )
            _mark_superseded_merged_version(conn, file_id, event_id, observed, merged_count)
            _checkpoint(conn, root_id, event_id, observed)
            conn.commit()
            return {"recorded": True, "item": item, "reason": "superseded_file_events_merged", "raw_access": False}
        content_flags = {"item_created", "item_removed", "item_renamed", "item_modified"}
        baseline_completed_at = str(root_status["baseline_completed_at"] or "") if root_status else ""
        if helper_flags & content_flags and (
            _timestamp_at_or_before(observed, str(previous_row["last_seen_at"] or ""))
            or _timestamp_at_or_before(observed, baseline_completed_at)
        ):
            # FSEvents can emit several distinct IDs for the same file in the
            # same one-second timestamp bucket.  The first event advances
            # ``last_seen_at``; treating the next identical snapshot as a lost
            # pre-save version downgraded the whole root even though the stream
            # and current state were both intact.  The same rule applies to an
            # event already absorbed by the baseline: coverage begins at the
            # baseline barrier, so activity at or before that boundary cannot
            # invalidate the interval that follows it.  Retain a safe audit row
            # and advance the watermark, but do not create an attribution gap.
            _record_gap(
                conn,
                root_id,
                observed,
                event_id or "same_content_event",
                downgrade_baseline=False,
                affects_attribution=False,
                reason_code="same_content_event",
            )
            conn.commit()
            return {
                "recorded": False,
                "reason": "same_content_event",
                "coverage_status": "complete",
                "raw_access": False,
            }
        _upsert_state(conn, root_id, snapshot, observed)
        _checkpoint(conn, root_id, event_id, observed)
        conn.commit()
        return {"recorded": False, "reason": "metadata_only_no_content_change", "raw_access": False}
    else:
        old = _state_dict(previous_row)
        _upsert_state(conn, root_id, snapshot, observed)
        old_or_new_protected = (
            str(old.get("detail_level") or "") == "protected"
            or str(snapshot.get("detail_level") or "") == "protected"
        )
        if old_or_new_protected:
            protected_snapshot = {
                **snapshot,
                "file_name_redacted": "",
                "detail_level": "protected",
                "privacy_flags": sorted(set(snapshot.get("privacy_flags") or []) | {"protected_content"}),
            }
            item = _record_change(
                conn,
                protected_snapshot,
                root_id,
                "modified",
                observed,
                "检测到受保护文档发生变化；未保存文件名或内容摘要。",
                "protected",
                helper_event_id=event_id,
            )
        elif str(old.get("structure_hmac_json") or "") == str(snapshot.get("structure_hmac_json") or ""):
            metadata_snapshot = {**snapshot, "detail_level": "metadata_only"}
            item = _record_change(
                conn,
                metadata_snapshot,
                root_id,
                "modified",
                observed,
                "检测到文件变化，但没有可安全概括的前后内容。",
                "metadata_only",
                helper_event_id=event_id,
            )
        else:
            item = _record_change(
                conn, snapshot, root_id, "modified", observed, _change_summary(old, snapshot),
                snapshot["detail_level"], helper_event_id=event_id,
            )
    _mark_superseded_merged_version(conn, file_id, event_id, observed, merged_count)
    _checkpoint(conn, root_id, event_id, observed)
    conn.commit()
    return {"recorded": True, "item": item, "raw_access": False}


def _mark_superseded_merged_version(
    conn: sqlite3.Connection,
    file_id: str,
    helper_event_id: str,
    changed_at: str,
    merged_count: int,
) -> None:
    """Force a version that absorbed skipped queued events out of AI-only.

    The live processor may release older queued events for a file that still
    has a newer event queued; the newer event's snapshot then stands for writes
    that were never observed one by one.  Use the same eligibility mechanism as
    an unresolved snapshot (``coverage_state='file_unresolved'``) and record the
    multiplicity, inside the caller's write transaction so the version is never
    committed without the flag.  Only fixed codes and counts are stored.
    """

    if merged_count <= 0:
        return
    conn.execute(
        "UPDATE file_change_versions SET coverage_state='file_unresolved',"
        "observed_version_count=observed_version_count+?,"
        "actor_reason_codes=CASE WHEN actor_source='unattributed' THEN ? ELSE actor_reason_codes END,"
        "updated_at=? WHERE file_id=? AND helper_event_id=? AND changed_at=?",
        (
            int(merged_count),
            json.dumps(["superseded_file_events_merged"], ensure_ascii=False),
            changed_at,
            file_id,
            helper_event_id,
            changed_at,
        ),
    )


def _record_unresolved_document_event(
    conn: sqlite3.Connection,
    event: dict,
    *,
    root_path: str | Path,
    root_label: str,
    expected_epoch: int,
    superseded_merged_count: int = 0,
) -> dict:
    """Keep a transient file write as an unattributed file fact, not a stream gap.

    Office apps, sync clients, and indexers can leave a file changing or briefly
    malformed for longer than the normal snapshot retry window. FSEvents still
    delivered that event; treating the unreadable *file* as a lost event stream
    makes every other file under the root look uncovered. Persist only safe
    metadata for the affected logical file, force that version out of AI-only
    eligibility, and advance the durable helper checkpoint. No content or raw
    path is stored here.
    """

    root = _resolve_root(root_path)
    path = Path(str(event.get("path") or "")).expanduser()
    if not path.is_absolute():
        path = root / path
    observed = _iso(str(event.get("observed_at") or event.get("timestamp") or ""))
    event_id = str(event.get("event_id") or "")
    root_id = _root_id(conn, root)
    rejection = _document_change_authorization_rejection(
        conn,
        root_id,
        expected_epoch,
    )
    if rejection:
        return {"recorded": False, "reason": rejection, "raw_access": False}
    if not _safe_path(root, path, allow_missing=True):
        rejection = _begin_document_change_write(conn, root_id, expected_epoch)
        if rejection:
            return {"recorded": False, "reason": rejection, "raw_access": False}
        if _event_id_is_replay(conn, root_id, event_id):
            conn.rollback()
            return {"recorded": False, "reason": "event_replay", "raw_access": False}
        _checkpoint(conn, root_id, event_id, observed)
        conn.commit()
        return {"recorded": False, "reason": "ignored_path", "raw_access": False}

    file_id = _file_id(conn, root, path)
    previous = conn.execute(
        "SELECT * FROM file_watch_state WHERE file_id=?",
        (file_id,),
    ).fetchone()
    current_stat = None
    try:
        current_stat = _safe_file_stat(root, path)
    except OSError:
        pass

    rejection = _begin_document_change_write(conn, root_id, expected_epoch)
    if rejection:
        return {"recorded": False, "reason": rejection, "raw_access": False}
    if _event_id_is_replay(conn, root_id, event_id):
        conn.rollback()
        return {"recorded": False, "reason": "event_replay", "raw_access": False}
    previous = conn.execute(
        "SELECT * FROM file_watch_state WHERE file_id=?",
        (file_id,),
    ).fetchone()
    previous_data = _state_dict(previous) if previous is not None else {}
    protected = (
        str(previous_data.get("detail_level") or "") == "protected"
        or _protected_name(path.name)
    )
    detail = "protected" if protected else "metadata_only"
    safe_name = "" if protected else _safe_label(
        redact_for_storage(path.name, max_len=120),
        120,
    )
    file_type = path.suffix.lower().lstrip(".") or "unknown"
    if file_type not in PUBLIC_FILE_TYPES:
        file_type = "unknown"
    modified_at = ""
    size_bytes = int(previous_data.get("size_bytes") or 0)
    if current_stat is not None:
        modified_at = datetime.fromtimestamp(
            current_stat.st_mtime,
            timezone.utc,
        ).isoformat(timespec="seconds")
        size_bytes = int(current_stat.st_size)
    elif previous_data:
        modified_at = str(previous_data.get("modified_at") or "")

    helper_flags = {str(item) for item in event.get("flags") or []}
    if current_stat is None and "item_removed" in helper_flags:
        kind = "deleted"
    elif previous is None and current_stat is not None:
        kind = "created"
    elif "item_renamed" in helper_flags:
        kind = "renamed"
    else:
        kind = "modified"
    source = {
        "file_id": file_id,
        "project_label": str(
            previous_data.get("project_label")
            or _project_label_for_path(conn, root, path)
            or root_label
        ),
        "document_path_hmac": str(
            previous_data.get("document_path_hmac")
            or _document_signal_path_hmac(conn, path)
            or ""
        ),
        "file_name_redacted": safe_name,
        "file_type": str(previous_data.get("file_type") or file_type),
        "modified_at": modified_at,
        "size_bytes": size_bytes,
        "content_hmac": "unresolved_file_snapshot",
        "detail_level": detail,
        "privacy_flags": sorted(
            {
                "document_change",
                "file_snapshot_unresolved",
                *(["protected_content"] if protected else []),
            }
        ),
    }
    item = _record_change(
        conn,
        source,
        root_id,
        kind,
        observed,
        (
            "检测到受保护文件发生变化；本次只保留计数，未保存文件名或内容摘要。"
            if protected
            else "检测到文件变化；该次写入尚未稳定，已保守列入待确认归因。"
        ),
        detail,
        helper_event_id=event_id,
    )
    conn.execute(
        "UPDATE file_change_versions SET coverage_state='file_unresolved',"
        "observed_version_count=observed_version_count+?,"
        "actor_reason_codes=CASE WHEN actor_source='unattributed' THEN ? ELSE actor_reason_codes END,"
        "updated_at=? WHERE file_id=? AND helper_event_id=? AND changed_at=?",
        (
            max(0, int(superseded_merged_count)),
            json.dumps(["file_snapshot_unresolved"], ensure_ascii=False),
            observed,
            file_id,
            event_id,
            observed,
        ),
    )
    _checkpoint(conn, root_id, event_id, observed)
    conn.commit()
    return {
        "recorded": True,
        "item": item,
        "reason": "file_snapshot_unresolved",
        "coverage_status": "complete",
        "raw_access": False,
    }


def reset_document_baseline_for_pause(conn: sqlite3.Connection) -> None:
    """Forget comparison state at pause entry without reconstructing the gap.

    Paused file events are ignored completely.  Re-enabling capture therefore
    starts with a fresh baseline whose first scan emits no modifications.
    """
    now = _now()
    bump_document_change_epoch(conn)
    _set_document_change_pending(conn, 0)
    conn.execute("DELETE FROM file_watch_state")
    conn.execute(
        "UPDATE file_watch_roots SET baseline_status='pending',baseline_started_at=NULL,"
        "baseline_completed_at=NULL,updated_at=? WHERE enabled=1",
        (now,),
    )
    conn.execute(
        "UPDATE file_watch_checkpoints SET last_event_id=NULL,coverage_status='pending',"
        "event_gap_count=0,coverage_started_at=NULL,last_event_at=NULL,updated_at=?",
        (now,),
    )


def resolve_document_change_range(
    range_name: str,
    *,
    start: str | None = None,
    end: str | None = None,
    now: datetime | None = None,
) -> tuple[str, str]:
    """Resolve owner-facing file history ranges through the current instant.

    Activity-confirmation ranges use the next local midnight as the end of
    "today".  Reusing that range for a live file-history query makes every
    otherwise continuous day look incomplete because its end is in the future.
    File history therefore ends at ``now`` for every range containing today;
    yesterday remains the one closed calendar-day range.
    """

    if start and end:
        return start, end
    current = now.astimezone() if now is not None else datetime.now().astimezone()
    today = current.replace(hour=0, minute=0, second=0, microsecond=0)
    if range_name == "yesterday":
        resolved_start = today - timedelta(days=1)
        resolved_end = today
    else:
        days = {"today": 1, "7d": 7, "30d": 30, "90d": 90}.get(range_name, 1)
        resolved_start = today - timedelta(days=days - 1)
        resolved_end = current
    return (
        resolved_start.isoformat(timespec="seconds"),
        resolved_end.isoformat(timespec="seconds"),
    )


def list_document_changes(
    conn: sqlite3.Connection,
    start: str,
    end: str,
    *,
    limit: int = 100,
    for_export: bool = False,
    sort_order: str = "relevance",
) -> dict:
    with document_authorization_lock(conn, exclusive=False):
        return _list_document_changes_locked(
            conn,
            start,
            end,
            limit=limit,
            for_export=for_export,
            sort_order=sort_order,
        )


def document_context_projections(
    conn: sqlite3.Connection,
    start: str,
    end: str,
    *,
    limit: int = 100,
) -> dict:
    """Return two named, versioned projections under one authorization lease.

    The owner-local projection may contain firewall-cleaned summaries but is
    always marked unverified.  The trusted projection is sourced only from the
    verified ContextBundle adapter.  Keeping both reads under the same shared
    authorization lease prevents a root revocation from landing between the
    two snapshots.
    """
    with document_authorization_lock(conn, exclusive=False):
        owner_local = _list_document_changes_locked(
            conn,
            start,
            end,
            limit=limit,
            for_export=False,
        )
        from .context_console import document_trusted_context_adapter

        trusted = document_trusted_context_adapter(
            conn,
            start_at=start,
            end_at=end,
            limit=limit,
        )
        return {
            "owner_local_projection": {
                "contract_version": DOCUMENT_OWNER_LOCAL_PROJECTION_VERSION,
                "source": "document_owner_local_observation",
                "projection_type": "owner_local_observation",
                "verification_state": "unverified",
                "trusted_export_eligible": False,
                "items": list(owner_local.get("items") or []),
                "coverage": dict(owner_local.get("coverage") or {}),
                "totals": dict(owner_local.get("totals") or {}),
                "limitations": list(owner_local.get("limitations") or []),
                "export_enabled": bool(owner_local.get("export_enabled")),
                "raw_access": False,
            },
            "trusted_projection": {
                "contract_version": DOCUMENT_TRUSTED_PROJECTION_VERSION,
                "source": "context_trusted_export",
                "projection_type": "trusted_verified_context",
                "verification_state": "verified_only",
                "trusted_export_eligible": True,
                "items": list(trusted.get("verified_contexts") or [])[
                    : max(0, min(int(limit), 1000))
                ],
                "status": str(trusted.get("status") or "unavailable"),
                "bundle_hash": str(trusted.get("bundle_hash") or ""),
                "raw_access": False,
            },
            "raw_access": False,
        }


def _list_document_changes_locked(
    conn: sqlite3.Connection,
    start: str,
    end: str,
    *,
    limit: int = 100,
    for_export: bool = False,
    sort_order: str = "relevance",
) -> dict:
    authorization_epoch_at_start = _document_change_epoch(conn)
    from .presentation_scope import for_connection
    display_scope = for_connection(conn)
    retired_documents = display_scope.retired_documents(conn)
    rows = conn.execute(
        "SELECT file_id,root_id,"
        "COALESCE((SELECT v.document_path_hmac FROM file_change_versions v "
        "WHERE v.aggregate_event_id=file_change_events.id AND v.document_path_hmac!='' "
        "ORDER BY v.changed_at DESC,v.id DESC LIMIT 1),"
        "(SELECT s.document_path_hmac FROM file_watch_state s WHERE s.file_id=file_change_events.file_id)) AS scope_document_ref,"
        "project_label,file_name_redacted,file_type,change_kind,first_changed_at,last_changed_at,"
        "version_count,change_summary_redacted,detail_level,confidence,privacy_flags "
        "FROM file_change_events WHERE root_id IN (SELECT root_id FROM file_watch_roots WHERE enabled=1) "
        "AND datetime(last_changed_at)>=datetime(?) AND datetime(first_changed_at)<datetime(?) "
        "ORDER BY datetime(last_changed_at) ASC",
        (start, end),
    ).fetchall()
    rows = [row for row in rows if row['scope_document_ref'] not in retired_documents]
    internal_root_ids = {
        str(row["root_id"])
        for row in conn.execute("SELECT root_id FROM file_watch_roots").fetchall()
    }
    internal_root_ids.update(str(row["root_id"]) for row in rows if row["root_id"])
    grouped: dict[str, dict] = {}
    action_files: dict[str, set[str]] = {kind: set() for kind in ("created", "modified", "renamed", "deleted")}
    for raw in rows:
        row = dict(raw)
        row.pop('document_path_hmac', None)
        row.pop('scope_document_ref', None)
        if display_scope.hidden(row):
            continue
        file_id = str(row.get("file_id") or "")
        if not file_id:
            continue
        kind = str(row.get("change_kind") or "modified")
        if kind in action_files:
            action_files[kind].add(file_id)
        current = grouped.get(file_id)
        content_versions = int(row.get("version_count") or 1) if kind in {"created", "modified"} else 0
        if current is None:
            row["version_count"] = max(1, content_versions)
            row["_content_version_count"] = content_versions
            grouped[file_id] = row
            continue
        current["first_changed_at"] = min(
            str(current.get("first_changed_at") or row.get("first_changed_at") or ""),
            str(row.get("first_changed_at") or current.get("first_changed_at") or ""),
        )
        accumulated_versions = int(current.get("_content_version_count") or 0) + content_versions
        current["_content_version_count"] = accumulated_versions
        current["version_count"] = max(1, accumulated_versions)
        if str(row.get("last_changed_at") or "") >= str(current.get("last_changed_at") or ""):
            first_at = current["first_changed_at"]
            versions = current["version_count"]
            content_version_count = current["_content_version_count"]
            current.update(row)
            current["first_changed_at"] = first_at
            current["version_count"] = versions
            current["_content_version_count"] = content_version_count
        if str(row.get("detail_level") or "") == "protected":
            current["detail_level"] = "protected"

    aggregated = list(grouped.values())
    # Registering per call keeps this out of shared connection setup and cannot
    # affect any other query.  The function is total -- it never raises -- so it
    # can never turn a listing into an OperationalError.
    conn.create_function(
        "pam_document_name_join_key", 1, _document_name_join_key, deterministic=True
    )
    try:
        attribution_history_start = (
            _parse_public_timestamp(start) - timedelta(days=90)
        ).isoformat()
    except Exception:
        attribution_history_start = start
    ai_edits = _load_ai_edit_attribution(conn, attribution_history_start, end)
    for item in aggregated:
        item["foreground_linked"] = _foreground_linked(conn, item)
        item["_ai_attribution"] = _ai_attribution_for(
            ai_edits.get(str(item.get("file_id") or "")), item
        )
        classification = _classify_file_attribution(
            conn,
            item,
            end=end,
            ai_records=ai_edits,
        )
        item["actor_classification"] = classification["classification"]
        item["attribution_confidence"] = classification["confidence"]
        item["attribution_reason_codes"] = classification["reason_codes"]
        item["attribution_tool"] = classification["tool"]
    aggregated.sort(
        key=_document_change_recent_sort_key
        if sort_order == "recent"
        else _document_change_sort_key
    )
    safe_items = [item for item in aggregated if str(item.get("detail_level") or "") != "protected"]
    selected = safe_items[: max(0, min(int(limit), 1000))]
    totals = {
        "distinct_files": 0,
        "created": 0,
        "modified": 0,
        "renamed": 0,
        "deleted": 0,
        "detailed": 0,
        "metadata_only": 0,
        "protected": 0,
        "ai_only": 0,
        "user_modified": 0,
        "unattributed": 0,
    }
    totals["distinct_files"] = len(aggregated)
    for kind, file_ids in action_files.items():
        totals[kind] = len(file_ids)
    for item in aggregated:
        detail = str(item.get("detail_level") or "metadata_only")
        totals[detail if detail in {"detailed", "metadata_only", "protected"} else "metadata_only"] += 1
        classification = str(item.get("actor_classification") or "unattributed")
        totals[classification if classification in {"ai_only", "user_modified", "unattributed"} else "unattributed"] += 1
    document_status = _document_changes_status_locked(conn)
    status = dict(document_status["coverage"])
    authorization_unverifiable = "root_control_refs_incomplete" in document_status.get("limitations", [])
    coverage_started_at = str(status.get("coverage_started_at") or "")
    range_is_covered = False
    if coverage_started_at:
        start_dt = _parse_datetime_strict(start)
        end_dt = _parse_datetime_strict(end)
        coverage_dt = _parse_datetime_strict(coverage_started_at)
        now_dt = _parse_datetime_strict(_now())
        range_is_covered = bool(
            start_dt is not None
            and end_dt is not None
            and coverage_dt is not None
            and now_dt is not None
            and start_dt < end_dt <= now_dt
            and start_dt >= coverage_dt
        )
    if not range_is_covered:
        status["is_complete"] = False
        status["coverage_status"] = "partial" if status.get("enabled") else "disabled"
    limitations = []
    if not status["enabled"]:
        limitations.append("document_change_tracking_disabled")
    if not status["is_complete"]:
        limitations.append("coverage_incomplete")
    if status.get("enabled") and not range_is_covered:
        limitations.append("requested_range_not_fully_covered")
    export_blocked = bool(for_export and not status["export_enabled"])
    if export_blocked:
        limitations.append("document_change_export_disabled")
    if authorization_unverifiable:
        status["is_complete"] = False
        status["coverage_status"] = "partial" if status.get("enabled") else "disabled"
        if "coverage_incomplete" not in limitations:
            limitations.append("coverage_incomplete")
        limitations.append("authorization_unverifiable")
    latest_epoch, latest_root_ids = _fresh_document_authorization(conn)
    event_root_ids = {str(item.get("root_id") or "") for item in aggregated if item.get("root_id")}
    authorization_changed = bool(
        latest_epoch is None
        or latest_epoch != authorization_epoch_at_start
        or not event_root_ids.issubset(latest_root_ids)
    )
    if authorization_changed:
        status["is_complete"] = False
        status["coverage_status"] = "partial" if status.get("enabled") else "disabled"
        if "coverage_incomplete" not in limitations:
            limitations.append("coverage_incomplete")
        limitations.append("authorization_changed_during_read")
    detail_blocked = bool(export_blocked or authorization_changed or authorization_unverifiable)
    export_state = "blocked" if detail_blocked else "open"
    authorization_blocked = bool(authorization_changed or authorization_unverifiable)
    public_totals = totals if not authorization_blocked else {
        "distinct_files": 0,
        "created": 0,
        "modified": 0,
        "renamed": 0,
        "deleted": 0,
        "detailed": 0,
        "metadata_only": 0,
        "protected": 0,
        "ai_only": 0,
        "user_modified": 0,
        "unattributed": 0,
    }
    return {
        "source": "filesystem_confirmed",
        "projection_contract": DOCUMENT_OWNER_LOCAL_PROJECTION_VERSION,
        "projection_type": "owner_local_observation",
        "verification_state": "unverified",
        "trusted_export_eligible": False,
        "coverage": {key: status[key] for key in ("enabled", "export_enabled", "baseline_status", "root_count", "baseline_indexed_files", "event_gap_count", "benign_event_count", "coverage_started_at", "last_event_at", "coverage_status", "is_complete", "pending_event_count", "resume_pending")},
        "totals": public_totals,
        "items": [] if detail_blocked else [
            _public_event(item, internal_root_ids=internal_root_ids) for item in selected
        ],
        "limitations": limitations,
        "export_enabled": status["export_enabled"],
        "export_state": export_state,
        "observed_changes": bool(public_totals["distinct_files"]),
        "blocked_message": (
            "已观测到文件变化，但安全明细尚未开放。"
            if export_blocked and not authorization_blocked and totals["distinct_files"]
            else ""
        ),
        "raw_access": False,
    }


def _document_change_sort_key(item: dict) -> tuple:
    detail_rank = {"detailed": 0, "metadata_only": 1, "protected": 2}
    return (
        detail_rank.get(str(item.get("detail_level") or "metadata_only"), 3),
        0 if item.get("foreground_linked") else 1,
        -int(item.get("version_count") or 1),
        _reverse_timestamp_key(str(item.get("last_changed_at") or "")),
        str(item.get("project_label") or ""),
        str(item.get("file_name_redacted") or ""),
    )


def _document_change_recent_sort_key(item: dict) -> tuple:
    """Order a listing the way a person reads their own day: newest first.

    The relevance key ranks detail level and foreground linkage above time, so
    a richly-parsed file from this morning outranks the file edited a minute
    ago.  That is right for a digest and wrong for "what did I touch today",
    which is what the owner's own screen asks.
    """

    return (
        _reverse_timestamp_key(str(item.get("last_changed_at") or "")),
        str(item.get("project_label") or ""),
        str(item.get("file_name_redacted") or ""),
    )


def _reverse_timestamp_key(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return -parsed.timestamp()
    except ValueError:
        return 0.0


# FSEvents reports that a file changed but never which process changed it, and
# all three macOS APIs that carry process identity are unavailable to an app
# like this one (EndpointSecurity needs an Apple entitlement, OpenBSM is
# deprecated, fs_usage is root-only and cannot be daemonized).  So the AI tools
# report their own writes instead: ~/bin/pam-ai-file-hook appends one line per
# edit, and this reads it.
#
# This is a separate read channel on purpose.  It is NOT a watch root: roots are
# directories that get enumerated, fingerprinted and content-read, and
# _resolve_root rightly refuses a single file.  This channel only ever reads one
# append-only log, joins it on file identity, and returns tool names -- no path
# ever leaves this function.
AI_EDIT_LOG_PATH = Path.home() / ".pam-ai-attribution" / "edits.jsonl"
AI_EDIT_ROTATED_LOG_PATH = Path.home() / ".pam-ai-attribution" / "edits.jsonl.1"
AI_EDIT_LOG_MAX_BYTES = 8 * 1024 * 1024
AI_EDIT_ATTRIBUTION_WINDOW_SECONDS = 15
_ATTRIBUTION_CACHE_ENTRIES = 4096
_ATTRIBUTION_CACHE_BYTES = 2 * 1024 * 1024


def _safe_transition_caller_chain(max_frames: int = 6) -> list[str]:
    """Return code locations without filesystem paths or runtime payloads."""

    chain: list[str] = []
    try:
        frame = sys._getframe(1)
        while frame is not None and len(chain) < max(1, int(max_frames)):
            chain.append(f"{frame.f_code.co_name}:{int(frame.f_lineno)}")
            frame = frame.f_back
    except (AttributeError, ValueError):
        return []
    return chain


def _load_ai_edit_attribution(
    conn: sqlite3.Connection, start: str, end: str
) -> dict[str, list[dict]]:
    """Map file identity -> the AI writes recorded for it in this range.

    The range here is only a coarse filter to bound parsing.  Whether a given
    write actually explains a given change is decided per item by
    _ai_attribution_for, against that item's own timestamps -- otherwise a wide
    listing range would let an edit from last week explain today's change.

    Fails open to "unknown".  A missing, truncated or malformed log means the
    surface says nothing about attribution; it must never mean the listing
    breaks, because an attribution hint is not worth an empty screen.
    """

    raw_parts: list[str] = []
    total_bytes = 0
    for log_path in (AI_EDIT_ROTATED_LOG_PATH, AI_EDIT_LOG_PATH):
        try:
            if not log_path.exists():
                continue
            size = int(log_path.stat().st_size)
            total_bytes += size
            if size < 0 or total_bytes > AI_EDIT_LOG_MAX_BYTES:
                return {}
            raw_parts.append(log_path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return {}
    if not raw_parts:
        return {}
    raw = "\n".join(raw_parts)

    database = _connection_database_path(conn)

    def private_binding(path, *, include_ctime=True):
        info = path.lstat()
        if (not stat_module.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat_module.S_IMODE(info.st_mode) != 0o600):
            raise OSError("attribution binding unavailable")
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                info.st_ctime_ns if include_ctime else None)

    def read_binding():
        if database is None:
            return None
        # The existing identity call chmods its secret even when the mode is
        # unchanged. Bind verified key bytes too, rather than treating that
        # expected ctime change as a new key or skipping any key validation.
        secret = _existing_hmac_key_readonly(conn)
        return (
            private_binding(database.parent / HMAC_SECRET_FILENAME, include_ctime=False),
            hashlib.sha256(secret).digest(),
            private_binding(database.parent / RUNTIME_ROOTS_FILENAME),
            _document_change_epoch(conn), _stored_hmac_verifier(conn),
        )

    try:
        initial_binding = read_binding()
    except (OSError, sqlite3.Error, RuntimeError):
        initial_binding = None
    registry = _read_runtime_registry(database.parent if database else None)
    enabled_root_ids = {
        str(row[0]) for row in conn.execute("SELECT root_id FROM file_watch_roots WHERE enabled=1")
    }
    if initial_binding is not None:
        try:
            if read_binding() != initial_binding:
                return {}
        except (OSError, sqlite3.Error, RuntimeError):
            return {}
    roots: list[tuple[Path, str]] = []
    authorized_root_ids: set[str] = set()
    for entry in registry:
        raw_path = str(entry.get("path") or "")
        root_id = str(entry.get("root_id") or "")
        if raw_path and ROOT_ID_RE.fullmatch(root_id) is not None and root_id in enabled_root_ids:
            roots.append((Path(raw_path), raw_path))
            authorized_root_ids.add(root_id)
    if not roots:
        return {}

    window = timedelta(seconds=AI_EDIT_ATTRIBUTION_WINDOW_SECONDS)
    try:
        lower = _parse_public_timestamp(start) - window
        upper = _parse_public_timestamp(end) + window
    except Exception:
        return {}

    def root_binding(root):
        info = root.lstat()
        return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, str(root.resolve()))

    file_ids: dict[tuple[Path, Path], str] = {}
    outside_roots: set[tuple[Path, Path]] = set()
    cache_bytes = {"file": 0, "outside": 0}
    def admit(cache, kind, pair):
        size = sum(len(os.fspath(path).encode("utf-8", errors="surrogatepass")) for path in pair)
        if len(cache) >= _ATTRIBUTION_CACHE_ENTRIES or cache_bytes[kind] + size > _ATTRIBUTION_CACHE_BYTES:
            return False
        cache_bytes[kind] += size
        return True
    root_bindings = {}
    attribution: dict[str, list[dict]] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            observed = _parse_public_timestamp(str(record.get("at") or ""))
        except Exception:
            continue
        if not (lower <= observed <= upper):
            continue
        stable_file_id = str(record.get("file_id") or "")
        stable_root_id = str(record.get("root_id") or "")
        if (
            FILE_ID_RE.fullmatch(stable_file_id) is not None
            and stable_root_id in authorized_root_ids
        ):
            attribution.setdefault(stable_file_id, []).append(
                {
                    "at": observed,
                    "tool": str(record.get("tool") or "unknown"),
                    "operation": str(record.get("operation") or "write"),
                }
            )
            continue
        candidate = Path(str(record.get("path") or ""))
        if not candidate.is_absolute():
            continue
        for root, _ in roots:
            # Reuse the identity path the events themselves take.  A plain
            # Path.relative_to would miss here, because the registry stores a
            # resolved root while a tool reports the path it was given, and on
            # macOS those differ by the /var -> /private/var alias that
            # _lexical_relative_path exists to normalize.
            try:
                # Only this read's successful, exact path pair is reusable.
                # The first call retains the complete existing key checks;
                # no action or future refresh inherits this projection cache.
                pair = (root, candidate)
                if pair in outside_roots:
                    continue
                if pair in file_ids:
                    file_id = file_ids[pair]
                else:
                    try:
                        _lexical_relative_path(root, candidate)
                    except OSError:
                        # This is a pure absolute-path containment result,
                        # never a cached file/key/authorization read failure.
                        if admit(outside_roots, "outside", pair):
                            outside_roots.add(pair)
                        continue
                    try:
                        before = root_binding(root) if initial_binding is not None else None
                    except OSError:
                        before = None
                    file_id = _file_id(conn, root, candidate)
                    if before is not None:
                        if before != root_binding(root):
                            return {}
                        if root in root_bindings and root_bindings[root] != before:
                            return {}
                        root_bindings[root] = before
                        if admit(file_ids, "file", pair):
                            file_ids[pair] = file_id
            except OSError:
                continue
            except Exception:
                break
            attribution.setdefault(file_id, []).append(
                {
                    "at": observed,
                    "tool": str(record.get("tool") or "unknown"),
                    "operation": str(record.get("operation") or "write"),
                }
            )
            break
    if initial_binding is not None:
        try:
            if (read_binding() != initial_binding
                    or any(root_binding(root) != value for root, value in root_bindings.items())):
                return {}
        except (OSError, sqlite3.Error, RuntimeError):
            return {}
    return attribution


def _ai_attribution_for(
    records: list[dict] | None, item: dict
) -> dict | None:
    """Pick the AI write that plausibly explains this specific change."""

    if not records:
        return None
    window = timedelta(seconds=AI_EDIT_ATTRIBUTION_WINDOW_SECONDS)
    try:
        lower = _parse_public_timestamp(str(item.get("first_changed_at") or "")) - window
        upper = _parse_public_timestamp(str(item.get("last_changed_at") or "")) + window
    except Exception:
        return None
    matched = [entry for entry in records if lower <= entry["at"] <= upper]
    if not matched:
        return None
    selected = max(matched, key=lambda entry: entry["at"])
    return {
        "tool": str(selected.get("tool") or "unknown"),
        "operation": str(selected.get("operation") or "write"),
        "at": selected["at"].isoformat(),
    }


def _parse_public_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _foreground_linked(conn: sqlite3.Connection, item: dict) -> bool:
    file_name = str(item.get("file_name_redacted") or "")
    if not file_name or str(item.get("detail_level") or "") == "protected":
        return False
    key = _document_name_join_key(file_name)
    if not key:
        return False
    row = conn.execute(
        "SELECT 1 FROM document_signals "
        "WHERE datetime(captured_at)>=datetime(?,'-15 minutes') "
        "AND datetime(captured_at)<=datetime(?,'+15 minutes') "
        "AND file_name_redacted<>'' "
        "AND COALESCE(semantic_match,1)=1 "
        "AND pam_document_name_join_key(file_name_redacted)=? LIMIT 1",
        (item.get("first_changed_at"), item.get("last_changed_at"), key),
    ).fetchone()
    return row is not None


def _strong_human_edit_evidence(conn: sqlite3.Connection, item: dict) -> dict | None:
    """Return conservative editor evidence for one concrete file version."""

    file_name = str(item.get("file_name_redacted") or "")
    document_path_hmac = str(item.get("document_path_hmac") or "")
    if (
        not file_name
        or not document_path_hmac.startswith("doc_")
        or str(item.get("detail_level") or "") == "protected"
    ):
        return None
    key = _document_name_join_key(file_name)
    if not key:
        return None
    from .app_identity import DOCUMENT_EDITOR_FAMILIES, app_identity_in

    target_time = _parse_datetime_strict(
        str(item.get("content_modified_at") or item.get("modified_at") or item.get("changed_at") or "")
    )
    if target_time is None:
        return None
    rows = conn.execute(
        "SELECT captured_at,app_name,file_name_redacted,file_modified_at,source,source_confidence "
        "FROM document_signals WHERE source='foreground_app_or_file_url' "
        "AND COALESCE(semantic_match,1)=1 "
        "AND file_path_hmac=? "
        "AND subsequent_mtime_event='modified_near_capture' "
        "AND datetime(captured_at)>=datetime(?,'-15 minutes') "
        "AND datetime(captured_at)<=datetime(?,'+15 minutes') "
        "AND file_name_redacted<>''",
        (document_path_hmac, target_time.isoformat(), target_time.isoformat()),
    ).fetchall()
    for row in rows:
        app_name = str(row["app_name"] or "").strip()
        if not app_identity_in(app_name, "", DOCUMENT_EDITOR_FAMILIES):
            continue
        if float(row["source_confidence"] or 0.0) < 0.6:
            continue
        if _document_name_join_key(str(row["file_name_redacted"] or "")) != key:
            continue
        modified = _parse_datetime_strict(str(row["file_modified_at"] or ""))
        if modified is None or abs((modified - target_time).total_seconds()) > 3:
            continue
        return {
            "confidence": "confirmed_human_editor_evidence",
            "reason_codes": [
                "supported_editor_foreground",
                "modified_near_capture",
                "exact_file_mtime_match",
            ],
        }
    return None


def _coverage_state_for_version(conn: sqlite3.Connection, root_id: str, changed_at: str) -> str:
    row = conn.execute(
        "SELECT coverage_status,event_gap_count,coverage_started_at "
        "FROM file_watch_checkpoints WHERE root_id=?",
        (root_id,),
    ).fetchone()
    if row is None or str(row["coverage_status"] or "") != "complete" or int(row["event_gap_count"] or 0):
        return "gap_affected"
    coverage_started = _parse_datetime_strict(str(row["coverage_started_at"] or ""))
    changed = _parse_datetime_strict(changed_at)
    if coverage_started is None or changed is None or changed < coverage_started:
        return "gap_affected"
    return "continuous"


def _actor_for_file_version(conn: sqlite3.Connection, item: dict) -> dict:
    changed_at = str(item.get("changed_at") or item.get("last_changed_at") or "")
    file_id = str(item.get("file_id") or "")
    ai_records = _load_ai_edit_attribution(conn, changed_at, changed_at)
    ai = _ai_attribution_for(ai_records.get(file_id), {
        "first_changed_at": changed_at,
        "last_changed_at": changed_at,
    })
    if ai is not None:
        operation = str(ai.get("operation") or "write")
        if operation == "write" and str(item.get("change_kind") or "") in {
            "created", "modified"
        }:
            # Claude's generic Write hook cannot know pre-write existence on
            # its own. The matched PAM event can: a stable-identity Write
            # receipt plus a created event is direct AI-create evidence.
            operation = str(item["change_kind"])
        return {
            "source": "ai_tool",
            "tool": str(ai.get("tool") or "unknown"),
            "confidence": "confirmed_ai_write_receipt",
            "reason_codes": [
                "stable_file_identity",
                "ai_write_receipt_time_match",
                f"ai_operation_{operation}",
            ],
        }
    human = _strong_human_edit_evidence(conn, item)
    if human is not None:
        return {
            "source": "user_evidence",
            "tool": "",
            "confidence": str(human["confidence"]),
            "reason_codes": list(human["reason_codes"]),
        }
    return {
        "source": "unattributed",
        "tool": "",
        "confidence": "unknown",
        "reason_codes": ["no_actor_evidence"],
    }


def _classify_file_attribution(
    conn: sqlite3.Connection,
    item: dict,
    *,
    end: str,
    ai_records: dict[str, list[dict]],
) -> dict:
    file_id = str(item.get("file_id") or "")
    versions = [
        dict(row)
        for row in conn.execute(
            "SELECT file_id,root_id,document_path_hmac,file_name_redacted,detail_level,change_kind,changed_at,content_modified_at,"
            "actor_source,actor_tool,actor_confidence,actor_reason_codes,coverage_state,observed_version_count "
            "FROM file_change_versions WHERE file_id=? AND datetime(changed_at)<datetime(?) "
            "ORDER BY datetime(changed_at) ASC,id ASC",
            (file_id, end),
        ).fetchall()
    ]
    if not versions:
        return {
            "classification": "unattributed",
            "confidence": "unknown",
            "reason_codes": ["missing_version_ledger"],
            "tool": "",
        }

    for version in versions:
        try:
            reasons = json.loads(str(version.get("actor_reason_codes") or "[]"))
        except (TypeError, json.JSONDecodeError):
            reasons = []
        version["_reason_codes"] = [str(value) for value in reasons if value]
        if str(version.get("actor_source") or "") != "unattributed":
            continue
        dynamic_ai = _ai_attribution_for(
            ai_records.get(file_id),
            {
                "first_changed_at": version.get("changed_at"),
                "last_changed_at": version.get("changed_at"),
            },
        )
        if dynamic_ai is not None and int(version.get("observed_version_count") or 1) == 1:
            operation = str(dynamic_ai.get("operation") or "write")
            if operation == "write" and str(version.get("change_kind") or "") in {
                "created", "modified"
            }:
                operation = str(version["change_kind"])
            version.update(
                {
                    "actor_source": "ai_tool",
                    "actor_tool": str(dynamic_ai.get("tool") or "unknown"),
                    "actor_confidence": "confirmed_ai_write_receipt",
                    "_reason_codes": [
                        "stable_file_identity",
                        "ai_write_receipt_time_match",
                        f"ai_operation_{operation}",
                    ],
                }
            )
            continue
        human = _strong_human_edit_evidence(conn, version)
        if human is not None and int(version.get("observed_version_count") or 1) == 1:
            version.update(
                {
                    "actor_source": "user_evidence",
                    "actor_confidence": str(human["confidence"]),
                    "_reason_codes": list(human["reason_codes"]),
                }
            )

    human_versions = [
        version for version in versions if str(version.get("actor_source") or "") == "user_evidence"
    ]
    if human_versions:
        return {
            "classification": "user_modified",
            "confidence": "confirmed_human_editor_evidence",
            "reason_codes": ["human_edit_evidence_overrides_origin"],
            "tool": "",
        }

    created_index = next(
        (
            index
            for index, version in enumerate(versions)
            if str(version.get("change_kind") or "") == "created"
            and str(version.get("actor_source") or "") == "ai_tool"
            and "ai_operation_created" in version.get("_reason_codes", [])
            and str(version.get("coverage_state") or "") == "continuous"
            and int(version.get("observed_version_count") or 1) == 1
        ),
        None,
    )
    if created_index is not None:
        created = versions[created_index]
        content_versions = [
            version
            for version in versions[created_index:]
            if str(version.get("change_kind") or "") in {"created", "modified"}
        ]
        # A merged delete/rename carries the same unknown intermediate writes
        # as a merged content version.  Check the complete existing origin
        # chain before the AI-only early return; confirmed human evidence
        # above still has priority and the creation boundary is unchanged.
        complete_origin_chain = all(
            str(version.get("coverage_state") or "") != "file_unresolved"
            and int(version.get("observed_version_count") or 1) == 1
            for version in versions[created_index:]
        )
        fully_ai = complete_origin_chain and bool(content_versions) and all(
            str(version.get("actor_source") or "") == "ai_tool"
            and str(version.get("coverage_state") or "") == "continuous"
            and int(version.get("observed_version_count") or 1) == 1
            for version in content_versions
        )
        fatal_gap = conn.execute(
            "SELECT 1 FROM file_watch_coverage_gaps WHERE root_id=? AND affects_attribution=1 "
            "AND datetime(observed_at)>=datetime(?) AND datetime(observed_at)<datetime(?) LIMIT 1",
            (str(created.get("root_id") or ""), str(created.get("changed_at") or ""), end),
        ).fetchone()
        if fully_ai and fatal_gap is None:
            tool = str(content_versions[-1].get("actor_tool") or "")
            return {
                "classification": "ai_only",
                "confidence": "confirmed_continuous_ai_only",
                "reason_codes": [
                    "ai_created_receipt",
                    "all_content_versions_ai_attributed",
                    "continuous_monitoring_since_creation",
                ],
                "tool": tool,
            }

    unknown_reasons = ["no_confirmed_human_evidence", "ai_only_requirements_not_met"]
    if any(
        str(version.get("coverage_state") or "") == "backfill_unknown"
        or "historical_aggregate_only" in version.get("_reason_codes", [])
        for version in versions
    ):
        unknown_reasons.append("historical_evidence_incomplete")
    if any(
        str(version.get("coverage_state") or "") == "file_unresolved"
        or "file_snapshot_unresolved" in version.get("_reason_codes", [])
        for version in versions
    ):
        unknown_reasons.append("file_event_evidence_incomplete")
    return {
        "classification": "unattributed",
        "confidence": "unknown",
        "reason_codes": unknown_reasons,
        "tool": "",
    }


class FileEventHelperProcess:
    """Injectable, argv-only JSONL subprocess wrapper for the Swift helper."""

    def __init__(self, command: list[str], roots: Iterable[str | Path], *, popen_factory=subprocess.Popen):
        self.command = [*command, *[str(Path(root).expanduser()) for root in roots]]
        self._popen_factory = popen_factory
        self.process = None
        self.stopping = False
        self.malformed_line_count = 0
        self.ready = threading.Event()
        self.ready_event_id = ""
        self.ready_root_count: int | None = None
        # Lost-stream generations must cool down before the daemon starts a
        # fresh no-watermark helper, even when the old generation had otherwise
        # run long enough to count as healthy.  This is internal lifecycle
        # state, never part of a public status payload.
        self.restart_cooldown_required = False
        # A failed terminal watermark reset must be visible to the lifecycle
        # owner and must block automatic replacement in this daemon process.
        # It is cleared only after the NULL-watermark transaction commits.
        self.restart_blocked = False
        # Fixed reason code of the lost-stream transition, so the lifecycle
        # owner can retry the same terminal reset after this generation joins.
        self.stream_reset_reason = ""

    def start(self) -> "FileEventHelperProcess":
        self.stopping = False
        self.process = self._popen_factory(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            close_fds=True,
        )
        return self

    def events(self) -> Iterator[dict]:
        if self.process is None or self.process.stdout is None:
            return
        for line in self.process.stdout:
            try:
                value = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                self.malformed_line_count += 1
                yield {"type": "protocol_error"}
                return
            if not isinstance(value, dict):
                self.malformed_line_count += 1
                yield {"type": "protocol_error"}
                return
            message_type = value.get("type")
            if message_type == "ready":
                event_id = str(value.get("event_id") or "")
                root_count = value.get("root_count")
                if not event_id.isdigit() or not isinstance(root_count, int) or isinstance(root_count, bool):
                    self.malformed_line_count += 1
                    yield {"type": "protocol_error"}
                    return
                self.ready_event_id = event_id
                self.ready_root_count = root_count
                self.ready.set()
                continue
            if message_type == "barrier":
                token = value.get("token")
                event_id = str(value.get("event_id") or "")
                if not isinstance(token, str) or not token or not event_id.isdigit():
                    self.malformed_line_count += 1
                    yield {"type": "protocol_error"}
                    return
                yield value
                continue
            path = value.get("path")
            event_id = str(value.get("event_id") or "")
            flags = value.get("flags")
            if (
                message_type is not None
                or not isinstance(path, str)
                or not path
                or not Path(path).is_absolute()
                or not event_id.isdigit()
                or not isinstance(flags, list)
                or not all(isinstance(flag, str) and flag in KNOWN_EVENT_FLAGS for flag in flags)
            ):
                self.malformed_line_count += 1
                yield {"type": "protocol_error"}
                return
            yield value

    def request_barrier(self, token: str) -> bool:
        if self.process is None or self.process.poll() is not None:
            return False
        custom = getattr(self.process, "request_barrier", None)
        if callable(custom):
            custom(token)
            return True
        stdin = getattr(self.process, "stdin", None)
        if stdin is None:
            return False
        try:
            stdin.write(json.dumps({"type": "barrier", "token": token}) + "\n")
            stdin.flush()
            return True
        except (BrokenPipeError, OSError, ValueError):
            return False

    def stop(self) -> None:
        self.stopping = True
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            waiter = getattr(self.process, "wait", None)
            if waiter is None:
                return
            try:
                waiter(timeout=3)
            except subprocess.TimeoutExpired:
                killer = getattr(self.process, "kill", None)
                if killer is not None:
                    killer()
                try:
                    waiter(timeout=2)
                except subprocess.TimeoutExpired:
                    pass


DOCUMENT_CHANGE_BACKPRESSURE_SECONDS = 30.0


DOCUMENT_CHANGE_WORKER_THREAD_KEYS = (
    "reader",
    "consumer",
    "validator",
    "processor",
    "monitor",
)


def join_document_change_worker(
    worker: dict,
    *,
    timeout_seconds: float | None = None,
) -> tuple[str, ...]:
    """Wait for the complete worker pipeline using one shared deadline.

    The queue sentinels flow reader -> consumer -> validator -> processor, so a
    caller must not treat the worker as stopped when only the consumer exits.
    ``None`` is intentionally an unbounded, fail-closed wait for the daemon's
    lifecycle owner. Tests use a bounded deadline and fail if any thread leaks.
    """
    deadline = (
        None
        if timeout_seconds is None
        else time.monotonic() + max(0.0, float(timeout_seconds))
    )
    joined: list[str] = []
    for name in DOCUMENT_CHANGE_WORKER_THREAD_KEYS:
        thread = worker.get(name) if isinstance(worker, dict) else None
        if thread is None:
            continue
        if thread is threading.current_thread():
            continue
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        thread.join(timeout=remaining)
        if not thread.is_alive():
            joined.append(name)

    alive = [
        name
        for name in DOCUMENT_CHANGE_WORKER_THREAD_KEYS
        if (
            (thread := worker.get(name) if isinstance(worker, dict) else None) is not None
            and thread.is_alive()
        )
    ]
    if alive:
        raise TimeoutError("document_change_worker_threads_alive:" + ",".join(alive))
    return tuple(joined)


def stop_document_change_worker(
    worker: dict,
    *,
    timeout_seconds: float | None = None,
) -> tuple[str, ...]:
    """Stop the helper and wait until every pipeline thread has exited."""
    process = worker.get("process") if isinstance(worker, dict) else None
    if process is not None:
        process.stop()
    return join_document_change_worker(worker, timeout_seconds=timeout_seconds)


def start_document_change_worker(
    db_path: str | Path,
    command: list[str],
    *,
    popen_factory=subprocess.Popen,
    session_state_provider=None,
    reconcile_eligibility_provider=None,
    reconcile_eligibility_check_interval_seconds: float = 1.0,
    _transition_hook=None,
    _diagnostic_sink=None,
    require_fresh_stream: bool = False,
) -> dict:
    """Run a fail-closed FSEvents stream with a barriered baseline.

    The worker uses a fixed reader/consumer/validator/processor thread set.  Raw
    event bursts therefore cannot create one Timer thread per event.  A baseline
    remains persistently partial until the helper flush barrier proves all prior
    events were observed and handled.
    """
    from .storage import connect, connect_readonly, ensure_ready

    database = Path(db_path).resolve()
    runtime_root = database.parent
    conn = ensure_ready(database)
    try:
        registry = ensure_default_document_roots(conn, runtime_root)
        enabled = _setting_bool(conn, "document_change_tracking_enabled", True)
        checkpoints = conn.execute(
            "SELECT checkpoints.last_event_id,checkpoints.coverage_status,roots.baseline_status,roots.root_id FROM file_watch_checkpoints AS checkpoints "
            "JOIN file_watch_roots AS roots ON roots.root_id=checkpoints.root_id "
            "WHERE roots.enabled=1"
        ).fetchall()
        since_values = [
            int(str(row["last_event_id"]))
            for row in checkpoints
            if str(row["last_event_id"] or "").isdigit()
        ]
        # Bind the entire helper lifetime to the authorization/delete boundary
        # that existed before the OS stream was started.  Events already queued
        # by this helper can never borrow a later epoch after a range deletion,
        # root removal, pause or baseline reset.
        worker_authorization_epoch = _document_change_epoch(conn)
        roots: list[dict] = []
        for item in registry:
            root_id = str(item.get("root_id") or "")
            row = conn.execute(
                "SELECT enabled FROM file_watch_roots WHERE root_id=?", (root_id,)
            ).fetchone()
            if row is None or not bool(row["enabled"]):
                continue
            try:
                path = _resolve_root(str(item.get("path") or ""))
            except (OSError, ValueError):
                continue
            if root_id != _root_id(conn, path):
                continue
            roots.append({**item, "path": str(path), "root_id": root_id})
        resume_existing_baseline = bool(roots) and _document_change_pending_count(conn) == 0 and len(checkpoints) == len(roots) and all(
            str(row["last_event_id"] or "").isdigit() and row["coverage_status"] == "complete"
            and row["baseline_status"] == "complete" for row in checkpoints
        ) and {row["root_id"] for row in checkpoints} == {item["root_id"] for item in roots}
    finally:
        conn.close()
    if not enabled or not roots:
        return {"started": False, "reason": "disabled_or_no_roots", "raw_access": False}
    if require_fresh_stream and since_values:
        # The lifecycle owner just verified a lost-stream reset.  A watermark
        # that reappeared before this read must never become ``--since``.
        return {"started": False, "reason": "stream_watermark_retained", "raw_access": False}

    from .runtime_liveness import HeartbeatWriter, HEARTBEAT_TTL_SECONDS
    heartbeat = HeartbeatWriter("document_worker")
    baseline_metrics_enabled = _diagnostic_sink is not None
    baseline_metrics_lock = threading.Lock()
    baseline_metrics_emit_lock = threading.Lock()
    baseline_metrics_active = threading.Event()
    baseline_metrics_started_ns = 0
    # Reuse only already registered public random references; never create a
    # reference for diagnostics or disclose path-derived internal root IDs.
    diagnostic_root_refs = {
        str(item["root_id"]): str(item["root_ref"])
        for item in roots
        if _valid_root_ref(item.get("root_ref"))
        and not _contains_internal_root_identity(str(item["root_ref"]), (r["root_id"] for r in roots))
        and sum(r.get("root_ref") == item["root_ref"] for r in roots) == 1
    }
    baseline_root_scan_issue_counts: dict[str, dict[str, int]] = {
        ref: {} for ref in diagnostic_root_refs.values()
    }
    baseline_root_io_counts: dict[str, dict[str, int]] = {
        ref: {} for ref in diagnostic_root_refs.values()
    }
    baseline_metrics = {
        "last_emit_ns": 0,
        "pass_number": 0,
        "max_passes": 0,
        "root_scan_attempts_finished_count": 0,
        "candidate_count": 0,
        "session_probe_count": 0,
        "session_probe_elapsed_ns": 0,
        "authorization_cache_hit_count": 0,
        "authorization_cache_miss_count": 0,
        "authorization_db_probe_count": 0,
        "authorization_db_probe_elapsed_ns": 0,
        "reconcile_probe_count": 0,
        "reconcile_probe_elapsed_ns": 0,
        "enumeration_elapsed_ns": 0,
        "candidate_total_elapsed_ns": 0,
        "safe_path_elapsed_ns": 0,
        "pre_snapshot_gate_inclusive_elapsed_ns": 0,
        "snapshot_inclusive_elapsed_ns": 0,
        "database_write_elapsed_ns": 0,
        "batch_throttle_inclusive_elapsed_ns": 0,
        "scan_throttle_elapsed_ns": 0,
        "files_indexed_count": 0,
        "files_ignored_count": 0,
        "files_failed_count": 0,
        "files_written_count": 0,
        "files_unchanged_count": 0,
        **{"failure_" + reason: 0 for reason in _DOCUMENT_BASELINE_FAILURE_REASONS},
        **{"io_step_" + step: 0 for step in _DOCUMENT_BASELINE_IO_STEPS},
        **{"io_errno_" + label: 0 for label in _DOCUMENT_BASELINE_IO_ERRNOS},
    }

    def record_baseline_metric(name: str, value: int = 1, *, root_ref: str | None = None) -> None:
        if (
            not baseline_metrics_enabled
            or not baseline_metrics_active.is_set()
            or name not in baseline_metrics
        ):
            return
        try:
            amount = max(0, int(value))
        except (TypeError, ValueError):
            return
        with baseline_metrics_lock:
            if not baseline_metrics_active.is_set():
                return
            baseline_metrics[name] += amount
            if name.startswith("failure_") and root_ref in baseline_root_scan_issue_counts and amount:
                reason = name.removeprefix("failure_")
                bucket = baseline_root_scan_issue_counts[root_ref]
                bucket[reason] = bucket.get(reason, 0) + amount
            if name.startswith("io_") and root_ref in baseline_root_io_counts and amount:
                bucket = baseline_root_io_counts[root_ref]
                bucket[name] = bucket.get(name, 0) + amount

    def maybe_emit_baseline_progress(*, stage: str = "running", force: bool = False) -> None:
        # Replaced with the queue-aware implementation after worker setup.
        return None

    if session_state_provider is None:
        def live_session_state() -> dict:
            from .daemon import session_state

            return session_state(runtime_root)
    else:
        live_session_state = session_state_provider

    def live_content_allowed() -> bool:
        record_probe_metrics = (
            baseline_metrics_enabled and baseline_metrics_active.is_set()
        )
        probe_started = time.monotonic_ns() if record_probe_metrics else 0
        try:
            try:
                value = live_session_state()
            except Exception:
                return False
            return _document_change_session_allows_content(value)
        finally:
            if record_probe_metrics:
                record_baseline_metric("session_probe_count")
                record_baseline_metric(
                    "session_probe_elapsed_ns",
                    time.monotonic_ns() - probe_started,
                )
                maybe_emit_baseline_progress()

    reconcile_eligibility_state = {
        "checked_at": 0.0,
        "allowed": True,
    }

    def reconcile_window_open(*, force: bool = False) -> bool:
        if reconcile_eligibility_provider is None:
            return True
        now_monotonic = time.monotonic()
        interval = max(0.0, float(reconcile_eligibility_check_interval_seconds))
        if (
            not force
            and now_monotonic - float(reconcile_eligibility_state["checked_at"]) < interval
        ):
            return bool(reconcile_eligibility_state["allowed"])
        record_probe_metrics = (
            baseline_metrics_enabled and baseline_metrics_active.is_set()
        )
        probe_started = time.monotonic_ns() if record_probe_metrics else 0
        try:
            try:
                value = reconcile_eligibility_provider()
                allowed = bool(
                    value.get("allowed") is True
                    if isinstance(value, dict)
                    else value
                )
            except Exception:
                allowed = False
        finally:
            if record_probe_metrics:
                record_baseline_metric("reconcile_probe_count")
                record_baseline_metric(
                    "reconcile_probe_elapsed_ns",
                    time.monotonic_ns() - probe_started,
                )
        reconcile_eligibility_state["checked_at"] = now_monotonic
        reconcile_eligibility_state["allowed"] = allowed
        return allowed

    if not reconcile_window_open(force=True):
        return {
            "started": False,
            "reason": "reconcile_eligibility_lost",
            "raw_access": False,
        }

    def root_authorized(state_conn: sqlite3.Connection, root_id: str) -> bool:
        if not _setting_bool(state_conn, "document_change_tracking_enabled", True):
            return False
        if _daemon_state_bool(state_conn, "pause_mode"):
            return False
        row = state_conn.execute(
            "SELECT enabled FROM file_watch_roots WHERE root_id=?",
            (root_id,),
        ).fetchone()
        return bool(row and row["enabled"])

    authorization_verdict_lock = threading.Lock()
    authorization_verdicts: dict[tuple[str, int | None], tuple[float, bool]] = {}

    def content_allowed_for_root(root_id: str, expected_epoch: int | None = None) -> bool:
        # Fresh session evidence stays outside the authorization cache:
        # unknown/off-console transitions must stop content reads immediately,
        # not within the cache window. A verified local locked console is the
        # explicit owner-approved exception handled by live_content_allowed().
        if not live_content_allowed():
            return False
        key = (str(root_id), None if expected_epoch is None else int(expected_epoch))
        ttl = DOCUMENT_AUTHORIZATION_CACHE_SECONDS
        now = time.monotonic()
        if ttl > 0:
            with authorization_verdict_lock:
                cached = authorization_verdicts.get(key)
            if cached is not None and now - cached[0] < ttl:
                record_baseline_metric("authorization_cache_hit_count")
                return cached[1]
        record_baseline_metric("authorization_cache_miss_count")
        probe_started = time.monotonic_ns() if baseline_metrics_enabled else 0
        try:
            verdict = _document_change_authorized_readonly(
                database,
                root_id,
                expected_epoch,
            )
        finally:
            if baseline_metrics_enabled:
                record_baseline_metric("authorization_db_probe_count")
                record_baseline_metric(
                    "authorization_db_probe_elapsed_ns",
                    time.monotonic_ns() - probe_started,
                )
        if ttl > 0:
            with authorization_verdict_lock:
                if len(authorization_verdicts) > 64:
                    # Stale epochs accumulate one key each; the set stays tiny
                    # in practice and this guard only exists so it cannot not.
                    authorization_verdicts.clear()
                # A busy schema-ready probe can wait behind another SQLite
                # writer for longer than the whole TTL.  Start the cache window
                # when the verdict was actually observed, not before that wait,
                # or the next content checkpoint immediately launches another
                # writer probe and recreates the contention loop.
                authorization_verdicts[key] = (time.monotonic(), verdict)
        return verdict

    from .worker_connections import WorkerConnections
    worker_connections = WorkerConnections(database, connect, connect_readonly)
    helper_argv = list(command)
    if since_values:
        helper_argv.extend(["--since", str(min(since_values))])
    process = FileEventHelperProcess(helper_argv, [item["path"] for item in roots], popen_factory=popen_factory).start()

    event_queue: queue.Queue[dict | None] = queue.Queue(
        maxsize=DOCUMENT_CHANGE_EVENT_QUEUE_MAXSIZE
    )
    validation_queue: queue.Queue[dict | None] = queue.Queue(
        maxsize=DOCUMENT_CHANGE_VALIDATION_QUEUE_MAXSIZE
    )
    processor_queue: queue.Queue[dict | None] = queue.Queue(
        maxsize=DOCUMENT_CHANGE_PROCESSOR_QUEUE_MAXSIZE
    )
    baseline_lock = threading.Lock()
    baseline_epoch_lock = threading.Lock()
    epoch_state = {"active": not resume_existing_baseline, "dirty": False}
    sequence_lock = threading.Lock()
    sequence_value = 0
    # Latest queued order key per path, for content events only (not ignored,
    # not a coverage transition).  Guarded by ``sequence_lock``.  The processor
    # uses it to release an older entry whose file already has a newer event
    # queued behind it instead of sleeping through stability retries on a file
    # that is still being written: the newer entry reads the same file later
    # and covers the older change.  Without this, one hot file costs ~11 s per
    # event in the single FIFO processor, the queues fill, FSEvents drops
    # events, and the generation takes a fatal reset.
    latest_order_by_path: dict[str, int] = {}
    # Processor-thread only: superseded entries skipped per path whose merged
    # version has not been recorded yet.  That version must be forced out of
    # AI-only eligibility because intermediate writes were never observed.
    superseded_debt_by_path: dict[str, int] = {}
    merge_metrics = {
        "superseded_skipped_count": 0,
        "merged_version_marked_count": 0,
        "slow_stability_entry_count": 0,
        "emitted_signature": (0, 0, 0),
        "last_emit_monotonic": time.monotonic(),
    }
    barrier_lock = threading.Lock()
    barrier_results: dict[str, dict] = {}
    # Two different facts used to share one latch, and the benign one starved
    # the baseline forever.  `stream_gap` means the event stream lost data --
    # the helper died, a queue overflowed, FSEvents reported a drop -- and it
    # keeps failing closed for the rest of this worker's life.  `scan_gap` means
    # the tracked set moved while a baseline pass was reading it, which is the
    # normal state of a machine in use, so it only invalidates the pass it
    # happened in and is cleared at the start of the next one.
    stream_gap = threading.Event()
    scan_gap = threading.Event()
    coverage_transition_lock = threading.Lock()
    baseline_eligibility_lost = threading.Event()

    def emit_internal_diagnostic(payload: dict) -> None:
        if _diagnostic_sink is None:
            return
        try:
            _diagnostic_sink(payload)
        except Exception:
            # Diagnostics must never weaken the state transition or scan they
            # are observing.
            pass

    def maybe_emit_baseline_progress(*, stage: str = "running", force: bool = False) -> None:
        if not baseline_metrics_enabled or not baseline_metrics_active.is_set():
            return
        if stage not in {"started", "running", "pass_finished", "completed"}:
            stage = "running"
        now_ns = time.monotonic_ns()
        interval_ns = max(
            0,
            int(float(DOCUMENT_BASELINE_DIAGNOSTIC_INTERVAL_SECONDS) * 1_000_000_000),
        )
        with baseline_metrics_emit_lock:
            with baseline_metrics_lock:
                if not baseline_metrics_active.is_set():
                    return
                if (
                    not force
                    and now_ns - int(baseline_metrics["last_emit_ns"]) < interval_ns
                ):
                    return
                baseline_metrics["last_emit_ns"] = now_ns
                snapshot = dict(baseline_metrics)
                root_issue_snapshot = {
                    ref: dict(counts) for ref, counts in baseline_root_scan_issue_counts.items() if counts
                }
                root_io_snapshot = {
                    ref: dict(counts) for ref, counts in baseline_root_io_counts.items() if counts
                }
                if stage == "completed":
                    # A completed record is terminal.  Clear the gate before
                    # emitting it so no later event-path probe can append a
                    # misleading baseline "running" record.
                    baseline_metrics_active.clear()
            emit_internal_diagnostic(
                {
                "event": "document_change_baseline_progress",
                "observed_at": _now(),
                "pid": os.getpid(),
                # Counts describe observed failures and denied reads, not a
                # second files_failed total: one denied batch can also have a
                # session denial. No raw exception or per-file identity leaves.
                "scan_issue_counts": {
                    reason: int(snapshot["failure_" + reason])
                    for reason in _DOCUMENT_BASELINE_FAILURE_REASONS
                    if snapshot["failure_" + reason]
                },
                "root_scan_issue_counts": root_issue_snapshot,
                "snapshot_io_failure_details": {
                    "steps": {step: int(snapshot["io_step_" + step]) for step in _DOCUMENT_BASELINE_IO_STEPS if snapshot["io_step_" + step]},
                    "errnos": {label: int(snapshot["io_errno_" + label]) for label in _DOCUMENT_BASELINE_IO_ERRNOS if snapshot["io_errno_" + label]},
                    "by_root": root_io_snapshot,
                    "association": "independent_aggregate_counts",
                },
                "stage": stage,
                "pass_number": int(snapshot["pass_number"]),
                "max_passes": int(snapshot["max_passes"]),
                "root_count": len(roots),
                "root_scan_attempts_finished_count": int(
                    snapshot["root_scan_attempts_finished_count"]
                ),
                "candidate_count": int(snapshot["candidate_count"]),
                "files_indexed_count": int(snapshot["files_indexed_count"]),
                "files_ignored_count": int(snapshot["files_ignored_count"]),
                "files_failed_count": int(snapshot["files_failed_count"]),
                "files_written_count": int(snapshot["files_written_count"]),
                "files_unchanged_count": int(snapshot["files_unchanged_count"]),
                "elapsed_ms": max(
                    0, (now_ns - baseline_metrics_started_ns) // 1_000_000
                ),
                "session_probe_count": int(snapshot["session_probe_count"]),
                "session_probe_elapsed_ms": int(
                    snapshot["session_probe_elapsed_ns"] // 1_000_000
                ),
                "authorization_cache_hit_count": int(
                    snapshot["authorization_cache_hit_count"]
                ),
                "authorization_cache_miss_count": int(
                    snapshot["authorization_cache_miss_count"]
                ),
                "authorization_db_probe_count": int(
                    snapshot["authorization_db_probe_count"]
                ),
                "authorization_db_probe_elapsed_ms": int(
                    snapshot["authorization_db_probe_elapsed_ns"] // 1_000_000
                ),
                "reconcile_probe_count": int(snapshot["reconcile_probe_count"]),
                "reconcile_probe_elapsed_ms": int(
                    snapshot["reconcile_probe_elapsed_ns"] // 1_000_000
                ),
                "enumeration_elapsed_ms": int(
                    snapshot["enumeration_elapsed_ns"] // 1_000_000
                ),
                "candidate_total_elapsed_ms": int(
                    snapshot["candidate_total_elapsed_ns"] // 1_000_000
                ),
                "safe_path_elapsed_ms": int(
                    snapshot["safe_path_elapsed_ns"] // 1_000_000
                ),
                "pre_snapshot_gate_inclusive_elapsed_ms": int(
                    snapshot["pre_snapshot_gate_inclusive_elapsed_ns"] // 1_000_000
                ),
                "snapshot_inclusive_elapsed_ms": int(
                    snapshot["snapshot_inclusive_elapsed_ns"] // 1_000_000
                ),
                "database_write_elapsed_ms": int(
                    snapshot["database_write_elapsed_ns"] // 1_000_000
                ),
                "batch_throttle_inclusive_elapsed_ms": int(
                    snapshot["batch_throttle_inclusive_elapsed_ns"] // 1_000_000
                ),
                "scan_throttle_elapsed_ms": int(
                    snapshot["scan_throttle_elapsed_ns"] // 1_000_000
                ),
                "queue_depths": {
                    "event": event_queue.qsize(),
                    "validation": validation_queue.qsize(),
                    "processor": processor_queue.qsize(),
                },
                "helper_stopping": bool(process.stopping),
                "epoch_active": bool(epoch_state["active"]),
                "raw_access": False,
                }
            )

    def mark_all_partial(
        reason: str,
        *,
        event_id: str = "",
        observed_at: str | None = None,
        reset_event_id: bool = False,
        fatal: bool = True,
        affects_attribution: bool | None = None,
        root_ids: list[str] | None = None,
        diagnostic_flags: Iterable[str] = (),
        _allow_stopped: bool = False,
    ) -> None:
        # A gap and a successful barrier are opposing coverage transitions.
        # Serialize their commits so a late barrier can never erase a gap that
        # the helper reader has already accepted.
        #
        # `fatal` defaults to True so a call site added later fails closed
        # unless its author states that the gap is benign churn rather than
        # lost data.  `root_ids` scopes a per-root fact -- one directory rename
        # -- to that root instead of marking all seven of them partial.
        stream_gap_preexisting = stream_gap.is_set()
        attribution_gap = fatal if affects_attribution is None else bool(affects_attribution)
        with coverage_transition_lock:
            # A terminal lost-stream reset is the last coverage writer of this
            # generation.  Any older directory/ignore/event work that resumes
            # after the helper has stopped must not resurrect its watermark.
            if process.stopping and not _allow_stopped:
                return
            if fatal:
                stream_gap.set()
            else:
                scan_gap.set()
            if fatal:
                helper_returncode = None
                if process.process is not None:
                    helper_returncode = process.process.poll()
                emit_internal_diagnostic(
                    {
                        "event": "document_change_coverage_downgraded",
                        "reason": (
                            reason
                            if reason in DOCUMENT_CHANGE_FATAL_REASON_CODES
                            else "unknown_fatal_transition"
                        ),
                        "fatal": True,
                        "reset_event_id": bool(reset_event_id),
                        "affected_root_count": (
                            len(root_ids) if root_ids is not None else len(roots)
                        ),
                        "caller_chain": _safe_transition_caller_chain(),
                        "control_flags": sorted(
                            {str(flag) for flag in diagnostic_flags}
                            & CONTROL_EVENT_FLAGS
                        ),
                        "queue_depths": {
                            "event": event_queue.qsize(),
                            "validation": validation_queue.qsize(),
                            "processor": processor_queue.qsize(),
                        },
                        "helper_stopping": bool(process.stopping),
                        "helper_returncode": helper_returncode,
                        "epoch_active": bool(epoch_state["active"]),
                        "stream_gap_preexisting": stream_gap_preexisting,
                        "scan_gap_set": scan_gap.is_set(),
                        "raw_access": False,
                    }
                )
            # Startup owns schema initialization.  Benign directory and
            # ignore-control transitions also reach this steady-state path, so
            # reopening through ensure_ready() here would run the full schema
            # transaction for ordinary event traffic and contend with the
            # processor/monitor writers.
            gap_conn = connect(database)
            try:
                _acquire_document_change_write_lock(gap_conn)
                if _document_change_epoch(gap_conn) != worker_authorization_epoch:
                    gap_conn.rollback()
                    return
                selected = None if root_ids is None else {str(value) for value in root_ids}
                authorized = [
                    str(item["root_id"])
                    for item in roots
                    if (selected is None or str(item["root_id"]) in selected)
                    and root_authorized(gap_conn, str(item["root_id"]))
                ]
                at = _iso(observed_at or "")
                if reset_event_id and authorized:
                    # _record_gap(reset_event_id=True) resets the shared stream
                    # watermark and marks every enabled root partial in one step.
                    _record_gap(
                        gap_conn,
                        authorized[0],
                        at,
                        event_id,
                        reset_event_id=True,
                        downgrade_baseline=fatal,
                        affects_attribution=attribution_gap,
                        reason_code=reason,
                    )
                else:
                    for root_id in authorized:
                        _record_gap(
                            gap_conn,
                            root_id,
                            at,
                            event_id,
                            downgrade_baseline=fatal,
                            affects_attribution=attribution_gap,
                            reason_code=reason,
                        )
                gap_conn.commit()
            finally:
                gap_conn.close()

    stream_reset_attempted = threading.Event()
    stream_reset_gate = threading.Lock()

    def stop_for_fresh_stream(
        reason: str,
        *,
        observed_at: str | None = None,
        diagnostic_flags: Iterable[str] = (),
    ) -> None:
        """Retire a lossy helper and clear every enabled stream watermark.

        Stopping comes first so no new work is admitted.  The baseline lock
        then waits out any scan/event writer already in flight, after which the
        coverage transition is the final durable write of this generation.
        The daemon observes the private cooldown flag after the full pipeline
        joins and starts the replacement without ``--since``.
        """

        with stream_reset_gate:
            if stream_reset_attempted.is_set():
                return
            stream_reset_attempted.set()
        process.restart_cooldown_required = True
        process.restart_blocked = True
        process.stream_reset_reason = (
            reason
            if reason in DOCUMENT_CHANGE_FATAL_REASON_CODES
            else "unknown_fatal_transition"
        )
        try:
            mark_document_stream_reset_required(runtime_root, reason)
        except OSError:
            # Best effort: the in-process retry still holds the reason.
            pass
        stream_gap.set()
        process.stop()
        with baseline_lock:
            mark_all_partial(
                reason,
                event_id="",
                observed_at=observed_at,
                reset_event_id=True,
                diagnostic_flags=diagnostic_flags,
                _allow_stopped=True,
            )
            # An authorization/config transaction can win after this worker
            # stops but before its reset acquires SQLite.  The old epoch then
            # (correctly) refuses to write; adding a root, however, does not by
            # itself clear the older roots' stream IDs.  Verify the actual
            # postcondition before permitting a replacement instead of treating
            # an epoch-mismatch no-op as success.
            from .storage import connect_readonly

            verify_conn = connect_readonly(database)
            try:
                retained = _retained_stream_watermark_count(verify_conn)
            finally:
                verify_conn.close()
            if retained != 0:
                raise RuntimeError("document_stream_watermark_reset_incomplete")
        clear_document_stream_reset_required(runtime_root)
        process.restart_blocked = False

    def fatal_worker(reason: str) -> None:
        # Every lossy stage must retire its stream IDs, not replay a poisoned
        # generation forever. This also verifies the reset before restart.
        try:
            stop_for_fresh_stream(reason)
        except (OSError, RuntimeError, sqlite3.Error):
            process.restart_blocked = True
            process.stop()

    def close_pipeline_queue(
        target: queue.Queue,
        *,
        release_counted_entries: bool,
    ) -> None:
        """Boundedly discard stopped-stage work, then wake a live consumer.

        Each pipeline queue has exactly one producer and a finite ``maxsize``.
        Once that producer reaches ``finally`` it cannot add more entries, so a
        non-blocking drain is bounded by the queue capacity.  This is important
        when the downstream thread has crashed: waiting for room to append a
        sentinel would otherwise wait forever.  Admitted entries are released
        as ownership is discarded; raw helper events have no pending count.
        """

        if not process.stopping:
            process.stop()
        cleanup_failed = False
        try:
            while True:
                try:
                    queued = target.get_nowait()
                except queue.Empty:
                    break
                if release_counted_entries and isinstance(queued, dict):
                    forget_latest_order(queued)
                    if not cleanup_failed:
                        try:
                            release_pending(queued)
                        except Exception:
                            # Preserve failed durable counts. One failed write
                            # must not impose a busy wait on every remaining
                            # queued entry or prevent the stop notification.
                            cleanup_failed = True
        finally:
            # This is the sole producer, already in its finalizer: no writer
            # can refill the drained queue. Wake the downstream stage even
            # when pending cleanup fails; never bypass its eventual join.
            target.put_nowait(None)
        if cleanup_failed:
            fatal_worker("event_handler_exception")

    def _drain_queue(target: "queue.Queue") -> None:
        """Discard everything queued so a fresh watermark starts from empty."""
        while True:
            try:
                target.get_nowait()
            except queue.Empty:
                return

    stage_wait_reason = {"reader": "", "consumer": "", "validator": "", "processor": ""}
    def database_wait(waiting):
        name = threading.current_thread().name
        stage = {"pam-document-events": "consumer", "pam-document-validator": "validator",
                 "pam-document-processor": "processor", "pam-document-event-reader": "reader"}.get(name, "monitor")
        stage_wait_reason[stage] = "database_busy" if waiting else ""
    worker_connections.stop_check = lambda: process.stopping
    worker_connections.on_wait = database_wait
    queue_high_water = {"reader": 0, "consumer": 0, "validator": 0}
    stage_completed = {"consumer": 0, "validator": 0, "processor": 0}

    def bounded_put(target, entry, stage):
        deadline = time.monotonic() + DOCUMENT_CHANGE_BACKPRESSURE_SECONDS
        try:
            while not process.stopping:
                try:
                    target.put_nowait(entry)
                    queue_high_water[stage] = max(queue_high_water[stage], target.qsize())
                    return
                except queue.Full:
                    stage_wait_reason[stage] = "backpressure"
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
            raise queue.Full
        finally:
            stage_wait_reason[stage] = ""

    @worker_connections.thread
    def read_helper() -> None:
        failed = False
        try:
            for event in process.events():
                if event.get("type") == "protocol_error":
                    failed = True
                    fatal_worker("helper_protocol_error")
                    break
                try:
                    bounded_put(event_queue, event, "reader")
                except queue.Full:
                    if process.stopping:
                        break  # A deliberate stop retains its replayable cursor.
                    # This generation has already lost an event and can never
                    # regain complete coverage. Clear every watermark without
                    # retaining the overflow event ID, then stop the old stream
                    # so the daemon can start a fresh no-`--since` generation
                    # after its bounded cooldown. Draining and continuing would
                    # leave `stream_gap` permanently set and make this worker
                    # incapable of ever committing a baseline.
                    stop_for_fresh_stream(
                        "python_event_queue_overflow",
                        observed_at=str(event.get("observed_at") or ""),
                    )
                    _drain_queue(event_queue)
                    failed = True
                    break
        except Exception:
            failed = True
            fatal_worker("helper_reader_exception")
        finally:
            if not process.stopping and not failed:
                fatal_worker("helper_unexpected_exit")
            close_pipeline_queue(event_queue, release_counted_entries=False)

    reader = threading.Thread(target=read_helper, name="pam-document-event-reader", daemon=True)

    def stat_guard(root: Path, path: Path) -> tuple[int, int, int, int] | None:
        try:
            value = _safe_file_stat(root, path)
        except OSError:
            return None
        return (int(value.st_dev), int(value.st_ino), int(value.st_size), int(value.st_mtime_ns))

    consumer_active_entry: dict[str, dict | None] = {"entry": None}
    # Waiting for input is healthy; an in-flight stage must keep making progress.
    stage_busy_since = {"consumer": None, "validator": None, "processor": None}

    def next_stage_entry(target, stage):
        if stage_busy_since[stage] is not None:
            stage_completed[stage] += 1
        stage_busy_since[stage] = None
        stage_wait_reason[stage] = ""
        entry = target.get()
        stage_busy_since[stage] = time.monotonic() if entry is not None else None
        return entry


    def consume_body() -> None:
        nonlocal sequence_value
        discarding = False
        while True:
            event = next_stage_entry(event_queue, "consumer")
            if event is None:
                return
            # Once the helper is stopped, raw events that have not reached this
            # stage have not incremented the persistent pending counter and must
            # not begin new content or database work. The checkpoint is left
            # unchanged so the next FSEvents stream replays them before its
            # barriered reconcile can restore complete coverage.
            if process.stopping:
                continue
            if discarding:
                continue
            if event.get("type") == "barrier":
                token = str(event.get("token") or "")
                resume = None
                with barrier_lock:
                    result = barrier_results.get(token)
                    if result is None:
                        fatal_worker("unexpected_barrier")
                        discarding = True
                        continue
                    result["payload"] = event
                    resume = result.get("resume")
                    result["event"].set()
                # A helper barrier is the cutover between the baseline and the
                # normal event pipeline.  Pause here until the baseline owner
                # holds epoch_lock for its atomic finish/retry decision.  Normal
                # post-barrier events then wait on that same lock; fatal control
                # events may still stop the helper immediately and win against
                # the coverage transition.  This prevents a final approximate
                # pass from consuming a real event as mere "dirty" work and
                # advancing the watermark past it.
                if isinstance(resume, threading.Event):
                    stage_busy_since["consumer"] = None
                    while not resume.wait(timeout=0.1):
                        if process.stopping:
                            break
                continue
            flags = {str(flag) for flag in event.get("flags") or []}
            if flags & CONTROL_EVENT_FLAGS:
                # Dropped/wrapped events may legitimately use "/" as their path,
                # and root_changed/unmount may arrive after that path vanished.
                # These are stream-wide integrity failures, so never require a
                # path-to-root match before failing closed.
                reset_stream_watermark = bool(
                    flags
                    & {
                        "must_scan_subdirs",
                        "user_dropped",
                        "kernel_dropped",
                        "event_ids_wrapped",
                    }
                )
                if reset_stream_watermark:
                    stop_for_fresh_stream(
                        "fsevents_control_event",
                        observed_at=str(event.get("observed_at") or ""),
                        diagnostic_flags=flags,
                    )
                else:
                    mark_all_partial(
                        "fsevents_control_event",
                        event_id=str(event.get("event_id") or ""),
                        observed_at=str(event.get("observed_at") or ""),
                        diagnostic_flags=flags,
                    )
                    process.stop()
                discarding = True
                continue
            with baseline_epoch_lock:
                # This event may have waited here while the baseline owner made
                # its cutover decision.  A reader overflow, revocation, or
                # monitor stop during that wait retires the generation; do not
                # resume with path resolution, ignore-rule reads, stat calls,
                # pending writes, or content work after the stop boundary.
                if process.stopping:
                    continue
                if epoch_state["active"]:
                    # The helper protocol already validated an absolute path,
                    # and this generation subscribes only to the authorized
                    # roots captured at startup.  During a baseline, no raw
                    # event needs filesystem, ignore-rule, database, or content
                    # access: the durable start checkpoint remains replayable,
                    # this bit invalidates the current pass, and the closing
                    # FIFO barrier is the only operation allowed to advance the
                    # watermark.  Avoiding all per-event I/O here is what keeps
                    # a live 10k queue drainable while a large root is scanned.
                    epoch_state["dirty"] = True
                    continue

            path_key = str(event.get("path") or "")
            match = _registry_match(roots, Path(path_key))
            if match is None:
                continue
            root_id = str(match["root_id"])
            coverage_transition = None
            ignore_control = _is_ignore_control(Path(path_key))
            skipped_control = ignore_control and _ignore_control_has_skipped_parent(
                Path(str(match["path"])), Path(path_key)
            )
            if ignore_control and not skipped_control:
                # An ignore-rule change moves the authorized baseline set, so
                # coverage for this root is honestly marked partial. It is
                # benign churn rather than lost stream data, so the helper stays
                # alive and the final busy pass may still close its baseline.
                # Keep this transition in the same FIFO pipeline as ordinary
                # file work.  Marking the gap here would advance the durable
                # watermark immediately while an earlier file event is still in
                # the validator/debounce stages; that older event would then be
                # misclassified as a replay and silently lost.
                coverage_transition = "ignore_rules_reconcile_required"
                definitely_ignored = False
            else:
                try:
                    definitely_ignored = _definitely_ignored_event(
                        Path(str(match["path"])),
                        Path(path_key),
                        is_directory="item_is_dir" in flags,
                        is_symlink="item_is_symlink" in flags,
                    )
                except GitIgnoreEvaluationError:
                    mark_all_partial(
                        "event_ignore_evaluation_failed",
                        event_id=str(event.get("event_id") or ""),
                        observed_at=str(event.get("observed_at") or ""),
                    )
                    process.stop()
                    discarding = True
                    continue
                definitely_ignored = definitely_ignored or skipped_control
            if "item_is_dir" in flags and not definitely_ignored:
                # A directory event can hide a bulk move that file-level events
                # do not describe. Persist that bounded uncertainty, but do not
                # turn normal iCloud/download churn into a fatal stream gap.
                coverage_transition = "directory_event"
            state_conn = worker_connections.connect("state")
            try:
                if not root_authorized(state_conn, root_id):
                    continue
                if _document_change_epoch(state_conn) != worker_authorization_epoch:
                    process.stop()
                    discarding = True
                    continue
                candidate_path = Path(path_key)
                candidate_root = Path(str(match["path"]))
                guard = None
                try:
                    if (
                        coverage_transition is None
                        and not definitely_ignored
                        and _safe_path(candidate_root, candidate_path, allow_missing=True)
                    ):
                        guard = stat_guard(candidate_root, candidate_path)
                except GitIgnoreEvaluationError:
                    with baseline_lock:
                        if not process.stopping:
                            rejection = _begin_document_change_write(
                                state_conn, root_id, worker_authorization_epoch
                            )
                            if not rejection:
                                _record_gap(
                                    state_conn,
                                    root_id,
                                    _iso(str(event.get("observed_at") or "")),
                                    str(event.get("event_id") or ""),
                                )
                                state_conn.commit()
                    process.stop()
                    discarding = True
                    continue
                with sequence_lock:
                    sequence_value += 1
                    order_key = sequence_value
                    if coverage_transition is None and not definitely_ignored:
                        latest_order_by_path[path_key] = order_key
                now_monotonic = time.monotonic()
                arrival_epoch = worker_authorization_epoch
                pending_value = _adjust_document_change_pending(
                    state_conn, 1, expected_epoch=arrival_epoch
                )
                if pending_value is None:
                    state_conn.rollback()
                    process.stop()
                    discarding = True
                    continue
                state_conn.commit()
                entry = {
                    "event": event,
                    "match": match,
                    "order_key": order_key,
                    "guard": guard,
                    "authorization_epoch": arrival_epoch,
                    "pending_counted": True,
                    "ignored_event": definitely_ignored,
                    "coverage_transition": coverage_transition,
                    "validate_at": (
                        now_monotonic
                        if coverage_transition is not None
                        else now_monotonic + 1.0
                    ),
                    "process_at": (
                        now_monotonic
                        if coverage_transition is not None
                        else now_monotonic + DEBOUNCE_SECONDS
                    ),
                }
                consumer_active_entry["entry"] = entry
                try:
                    bounded_put(validation_queue, entry, "consumer")
                    consumer_active_entry["entry"] = None
                except queue.Full:
                    release_pending(entry)
                    consumer_active_entry["entry"] = None
                    if not process.stopping:
                        fatal_worker("validation_queue_overflow")
                    discarding = True
                    continue
            finally:
                worker_connections.release(state_conn)

    @worker_connections.thread
    def consume() -> None:
        try:
            consume_body()
        except Exception:
            active_entry = consumer_active_entry.get("entry")
            if isinstance(active_entry, dict):
                release_pending_after_failure(active_entry)
                consumer_active_entry["entry"] = None
            fatal_worker("event_consumer_exception")
        finally:
            close_pipeline_queue(validation_queue, release_counted_entries=True)

    def wait_until_monotonic(target: float) -> bool:
        while True:
            remaining = target - time.monotonic()
            if remaining <= 0:
                return not process.stopping
            if process.stopping:
                return False
            time.sleep(min(0.1, remaining))

    def release_pending(entry: dict) -> None:
        forget_latest_order(entry)
        if not entry.get("pending_counted", False):
            return
        pending_conn = worker_connections.connect("pending")
        try:
            released = _adjust_document_change_pending(
                pending_conn,
                -1,
                expected_epoch=int(entry["authorization_epoch"]),
            )
            if released is None:
                pending_conn.rollback()
            else:
                pending_conn.commit()
            # Only a committed release or an obsolete authorization epoch
            # relinquishes this entry's count. Failure remains outstanding.
            entry["pending_counted"] = False
        finally:
            worker_connections.release(pending_conn)

    def release_pending_after_failure(entry: dict) -> None:
        try:
            release_pending(entry)
        except Exception:
            # The caller performs the fatal transition next. Cleanup cannot
            # mask that transition; its marker, full join and fresh baseline
            # own recovery of any durable count that could not be released.
            pass

    @worker_connections.thread
    def validate_events() -> None:
        candidate_cache: dict[tuple[str, tuple[int, int, int, int], int], dict] = {}
        active_entry: dict | None = None
        try:
            while True:
                entry = next_stage_entry(validation_queue, "validator")
                if entry is None:
                    return
                active_entry = entry
                if not wait_until_monotonic(float(entry["validate_at"])):
                    release_pending(entry)
                    active_entry = None
                    continue
                if entry.get("coverage_transition"):
                    try:
                        bounded_put(processor_queue, entry, "validator")
                        active_entry = None
                    except queue.Full:
                        release_pending(entry)
                        active_entry = None
                        if not process.stopping:
                            fatal_worker("processor_queue_overflow")
                        return
                    continue
                event = entry["event"]
                match = entry["match"]
                root_id = str(match["root_id"])
                arrival_epoch = int(entry["authorization_epoch"])
                state_conn = worker_connections.connect("state", readonly=True)
                try:
                    if not root_authorized(state_conn, root_id):
                        entry["drop"] = True
                    elif (
                        _document_change_epoch(state_conn) == arrival_epoch
                        and content_allowed_for_root(root_id, arrival_epoch)
                    ):
                        path = Path(str(event.get("path") or ""))
                        expected = entry.get("guard")
                        cache_key = (str(path), expected, arrival_epoch) if expected is not None else None
                        current = stat_guard(Path(str(match["path"])), path)
                        if expected is not None and current == expected:
                            candidate = candidate_cache.get(cache_key) if cache_key is not None else None
                            if candidate is None:
                                try:
                                    candidate = _snapshot(
                                        state_conn,
                                        Path(str(match["path"])),
                                        path,
                                        allow_content=True,
                                        content_allowed_check=lambda: content_allowed_for_root(root_id, arrival_epoch),
                                    )
                                    candidate.pop("structure", None)
                                except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, GitIgnoreEvaluationError):
                                    candidate = None
                                if candidate is not None and cache_key is not None:
                                    candidate_cache[cache_key] = candidate
                                    if len(candidate_cache) > 1024:
                                        candidate_cache.pop(next(iter(candidate_cache)))
                            if candidate is not None:
                                entry["candidate"] = candidate
                    else:
                        entry["locked_observation"] = True
                finally:
                    worker_connections.release(state_conn)
                try:
                    bounded_put(processor_queue, entry, "validator")
                    active_entry = None
                except queue.Full:
                    release_pending(entry)
                    active_entry = None
                    if not process.stopping:
                        fatal_worker("processor_queue_overflow")
                    return
        except Exception:
            if active_entry is not None:
                release_pending_after_failure(active_entry)
            fatal_worker("event_validator_exception")
        finally:
            close_pipeline_queue(processor_queue, release_counted_entries=True)

    def flush_ignored_batch(entries):
        batch_conn = worker_connections.connect("ignored_batch")
        try:
            with baseline_lock:
                with document_authorization_lock(batch_conn, exclusive=True):
                    if process.stopping:
                        return False
                    for item in entries:
                        root_id = str(item["match"]["root_id"])
                        epoch = int(item["authorization_epoch"])
                        rejection = _begin_document_change_write(batch_conn, root_id, epoch)
                        if rejection:
                            batch_conn.rollback()
                            return False
                        event = item["event"]
                        event_id = str(event.get("event_id") or "")
                        if not _event_id_is_replay(batch_conn, root_id, event_id):
                            _checkpoint(batch_conn, root_id, event_id, _iso(str(event.get("observed_at") or "")))
                    delta = sum(1 for item in entries if item.get("pending_counted"))
                    if _adjust_document_change_pending(batch_conn, -delta,
                            expected_epoch=int(entries[0]["authorization_epoch"])) is None:
                        batch_conn.rollback()
                        return False
                    batch_conn.commit()
                    for item in entries:
                        item["pending_counted"] = False
                    stage_completed["processor"] += max(0, len(entries) - 1)
                    return True
        finally:
            worker_connections.release(batch_conn)

    def newer_content_event_queued(entry: dict) -> bool:
        path_key = str(entry["event"].get("path") or "")
        with sequence_lock:
            latest = latest_order_by_path.get(path_key)
        return latest is not None and latest > int(entry["order_key"])

    def forget_latest_order(entry: dict) -> None:
        # Prune once the path's latest entry is done, so the map only holds
        # paths that still have an entry somewhere in the pipeline.
        if entry.get("ignored_event") or entry.get("coverage_transition"):
            return
        path_key = str(entry["event"].get("path") or "")
        with sequence_lock:
            if latest_order_by_path.get(path_key) == int(entry["order_key"]):
                del latest_order_by_path[path_key]

    def settle_superseded_debt(path_key: str, merged_count: int, result: dict) -> None:
        # The debt survives until a version for this path is actually written
        # with the merged flag, or until the file is gone or out of scope.  A
        # same-content or failed outcome keeps it for the next recorded
        # version, which is the conservative direction.
        if not merged_count:
            return
        reason = str(result.get("reason") or "")
        if result.get("recorded"):
            merge_metrics["merged_version_marked_count"] += 1
        elif reason not in {"ignored_path", "unknown_delete", "duplicate_delete"}:
            return
        superseded_debt_by_path.pop(path_key, None)

    def maybe_emit_merge_summary(*, force: bool = False) -> None:
        # Counts only: no path, file, root, or event identity leaves here.
        signature = (
            int(merge_metrics["superseded_skipped_count"]),
            int(merge_metrics["merged_version_marked_count"]),
            int(merge_metrics["slow_stability_entry_count"]),
        )
        if signature == merge_metrics["emitted_signature"]:
            return
        now_monotonic = time.monotonic()
        if (
            not force
            and now_monotonic - float(merge_metrics["last_emit_monotonic"])
            < DOCUMENT_CHANGE_MERGE_DIAGNOSTIC_INTERVAL_SECONDS
        ):
            return
        merge_metrics["emitted_signature"] = signature
        merge_metrics["last_emit_monotonic"] = now_monotonic
        emit_internal_diagnostic(
            {
                "event": "document_change_processor_merge_summary",
                "superseded_skipped_count": signature[0],
                "merged_version_marked_count": signature[1],
                "slow_stability_entry_count": signature[2],
                "queue_depths": {
                    "event": event_queue.qsize(),
                    "validation": validation_queue.qsize(),
                    "processor": processor_queue.qsize(),
                },
                "raw_access": False,
            }
        )

    @worker_connections.thread
    def process_events() -> None:
        active_entry: dict | None = None
        active_batch = []
        no_lookahead = object()
        lookahead = no_lookahead
        try:
            while True:
                if lookahead is no_lookahead:
                    entry = next_stage_entry(processor_queue, "processor")
                else:
                    entry, lookahead = lookahead, no_lookahead
                    stage_busy_since["processor"] = time.monotonic()

                if entry is None:
                    maybe_emit_merge_summary(force=True)
                    return
                active_entry = entry
                if entry.get("drop"):
                    forget_latest_order(entry)
                    release_pending(entry)
                    continue
                if not wait_until_monotonic(float(entry["process_at"])):
                    forget_latest_order(entry)
                    release_pending(entry)
                    continue
                event = entry["event"]
                match = entry["match"]
                root_id = str(match["root_id"])
                arrival_epoch = int(entry["authorization_epoch"])
                coverage_transition = str(
                    entry.get("coverage_transition") or ""
                )
                if coverage_transition:
                    # This entry contains no content work.  Keeping it behind
                    # earlier file entries in the single FIFO processor is the
                    # ordering guarantee; the transition itself reopens a fresh
                    # connection and rechecks the authorization epoch/root.
                    if not process.stopping:
                        mark_all_partial(
                            coverage_transition,
                            event_id=str(event.get("event_id") or ""),
                            observed_at=str(event.get("observed_at") or ""),
                            fatal=False,
                            affects_attribution=(
                                coverage_transition == "ignore_rules_reconcile_required"
                            ),
                            root_ids=[root_id],
                        )
                    release_pending(entry)
                    active_entry = None
                    continue
                if entry.get("ignored_event"):
                    active_batch = [entry]
                    batch_deadline = time.monotonic() + 0.05
                    while len(active_batch) < 100 and time.monotonic() < batch_deadline:
                        try:
                            later = processor_queue.get_nowait()
                        except queue.Empty:
                            break
                        if (isinstance(later, dict) and later.get("ignored_event")
                                and not later.get("coverage_transition") and not later.get("drop")
                                and later.get("authorization_epoch") == arrival_epoch):
                            active_batch.append(later)
                        else:
                            lookahead = later
                            break
                    if not flush_ignored_batch(active_batch):
                        for item in active_batch:
                            release_pending(item)
                    active_batch = []
                    active_entry = None
                    continue
                retries = 0
                content_started = time.monotonic()
                path_key = str(event.get("path") or "")
                flags = {str(flag) for flag in event.get("flags") or []}
                candidate = entry.get("candidate")
                if flags & {"item_removed", "item_renamed"}:
                    candidate = None
                while not process.stopping:
                    stage_wait_reason["processor"] = ""
                    if (
                        candidate is None
                        and newer_content_event_queued(entry)
                        and (
                            path_key in superseded_debt_by_path
                            or len(superseded_debt_by_path)
                            < DOCUMENT_CHANGE_SUPERSEDED_DEBT_PATH_LIMIT
                        )
                    ):
                        # No content was read or written for this entry.  The
                        # newer entry for the same file is already behind it
                        # in this FIFO and will read the file later; its
                        # version is the one that gets recorded, flagged as
                        # merged.  Its event id is never checkpointed here, so
                        # the later entry still owns the watermark.
                        superseded_debt_by_path[path_key] = (
                            superseded_debt_by_path.get(path_key, 0) + 1
                        )
                        merge_metrics["superseded_skipped_count"] += 1
                        break
                    merged_count = superseded_debt_by_path.get(path_key, 0)
                    event_conn = worker_connections.connect("event")
                    try:
                        if (
                            not root_authorized(event_conn, root_id)
                            or _document_change_epoch(event_conn) != arrival_epoch
                        ):
                            release_pending(entry)
                            break
                        if entry.get("ignored_event"):
                            deferred = False
                            with baseline_lock:
                                if process.stopping:
                                    result = {"reason": "worker_stopping"}
                                else:
                                    rejection = _begin_document_change_write(
                                        event_conn, root_id, arrival_epoch
                                    )
                                    if rejection:
                                        result = {"reason": rejection}
                                    elif _event_id_is_replay(
                                        event_conn, root_id, str(event.get("event_id") or "")
                                    ):
                                        event_conn.rollback()
                                        result = {"reason": "event_replay"}
                                    else:
                                        _checkpoint(
                                            event_conn,
                                            root_id,
                                            str(event.get("event_id") or ""),
                                            _iso(str(event.get("observed_at") or "")),
                                        )
                                        event_conn.commit()
                                        result = {"reason": "ignored_event"}
                        elif not content_allowed_for_root(root_id, arrival_epoch):
                            deferred = True
                            result = {"reason": "locked_deferred"}
                        else:
                            deferred = False
                            content_event = {**event, "stable_seconds": 1.0}
                            if merged_count:
                                content_event["_superseded_merged_count"] = merged_count
                            with baseline_lock:
                                if process.stopping:
                                    result = {"reason": "worker_stopping"}
                                else:
                                    result = process_document_event(
                                        event_conn,
                                        content_event,
                                        root_path=str(match["path"]),
                                        root_label=str(match.get("root_label") or "Tracked folder"),
                                        allow_content=True,
                                        snapshot_override=candidate,
                                        # process_document_event owns the exclusive
                                        # authorization lease and rechecks the bound
                                        # epoch before content I/O.  Re-entering the
                                        # shared authorization flock from this
                                        # callback would self-deadlock on macOS, so
                                        # only the fresh session probe belongs here.
                                        content_allowed_check=live_content_allowed,
                                        expected_epoch=arrival_epoch,
                                    )
                        reason = str(result.get("reason") or "")
                        if result.get("status") == "failed":
                            # A known failed write did not commit. This also
                            # covers rename pairing that inherits merged debt
                            # inside the transaction: the new path has no
                            # process-local debt to test here. A later cursor
                            # must not bypass either failed event. Ignored,
                            # replayed and revoked entries are not failures.
                            fatal_worker("event_handler_exception")
                            break
                        if reason == "unstable_or_unreadable" and retries >= 5:
                            with baseline_lock:
                                if not process.stopping:
                                    result = _record_unresolved_document_event(
                                        event_conn,
                                        event,
                                        root_path=str(match["path"]),
                                        root_label=str(
                                            match.get("root_label") or "Tracked folder"
                                        ),
                                        expected_epoch=arrival_epoch,
                                        superseded_merged_count=merged_count,
                                    )
                            settle_superseded_debt(path_key, merged_count, result)
                            break
                    finally:
                        worker_connections.release(event_conn)
                    if deferred or reason == "locked_deferred":
                        stage_wait_reason["processor"] = "waiting_for_session"
                        stage_busy_since["processor"] = time.monotonic()
                        time.sleep(0.5)
                        continue
                    if reason == "unstable_or_unreadable":
                        retries += 1
                        time.sleep(1.0)
                        continue
                    settle_superseded_debt(path_key, merged_count, result)
                    release_pending(entry)
                    break
                if (
                    retries
                    and time.monotonic() - content_started
                    > DOCUMENT_CHANGE_SLOW_STABILITY_SECONDS
                ):
                    merge_metrics["slow_stability_entry_count"] += 1
                forget_latest_order(entry)
                release_pending(entry)
                active_entry = None
                maybe_emit_merge_summary()
        except Exception:
            for item in active_batch:
                release_pending_after_failure(item)
            if isinstance(lookahead, dict):
                release_pending_after_failure(lookahead)
            if active_entry is not None:
                release_pending_after_failure(active_entry)
            fatal_worker("event_handler_exception")
        finally:
            # A normal stop can discard the final queued event after another
            # file has already advanced the shared cursor. Pending cleanup
            # alone cannot preserve its uncommitted merged evidence. Retire
            # that generation through the existing durable recovery, outside
            # every event transaction and authorization/baseline lock.
            if superseded_debt_by_path:
                fatal_worker("merged_events_unsettled_at_stop")

    consumer = threading.Thread(target=consume, name="pam-document-events", daemon=True)
    validator = threading.Thread(target=validate_events, name="pam-document-validator", daemon=True)
    processor = threading.Thread(target=process_events, name="pam-document-processor", daemon=True)
    startup_worker = {
        "process": process,
        "consumer": consumer,
        "reader": reader,
        "validator": validator,
        "processor": processor,
        "monitor": None,
    }

    def stop_and_join_startup_worker(reason: str) -> Exception | None:
        shutdown_error: Exception | None = None
        try:
            fatal_worker(reason)
        except Exception as exc:
            shutdown_error = exc
        try:
            join_document_change_worker(startup_worker)
        except Exception as exc:
            if shutdown_error is None:
                shutdown_error = exc
        return shutdown_error

    consumer.start()
    validator.start()
    processor.start()
    reader.start()
    if not process.ready.wait(timeout=5.0):
        error = RuntimeError("FSEvents helper did not become ready")
        shutdown_error = stop_and_join_startup_worker("helper_not_ready")
        if shutdown_error is not None:
            raise error from shutdown_error
        raise error
    if process.ready_root_count != len(roots):
        error = RuntimeError("FSEvents helper root count mismatch")
        shutdown_error = stop_and_join_startup_worker("helper_root_count_mismatch")
        if shutdown_error is not None:
            raise error from shutdown_error
        raise error

    # ready_event_id is the requested *minimum* per-root cursor, not the
    # global current event ID. Different retained root cursors are valid;
    # the later flush barrier and dropped/wrapped flags prove continuity.

    def begin_baseline_epoch() -> int | None:
        now = _now()
        begin_conn = ensure_ready(database)
        try:
            if not begin_conn.in_transaction:
                begin_conn.execute("BEGIN IMMEDIATE")
            # This check must share the writer transaction with the startup
            # checkpoint writes.  A reader-thread overflow can clear every
            # watermark before the main thread reaches this function; without
            # the in-transaction check, the helper's ready ID would be written
            # back after that reset and the replacement would incorrectly use
            # ``--since`` again.
            if process.stopping or stream_gap.is_set():
                begin_conn.rollback()
                return None
            if not _setting_bool(begin_conn, "document_change_tracking_enabled", True):
                raise RuntimeError("document tracking disabled before baseline")
            if _document_change_epoch(begin_conn) != worker_authorization_epoch:
                raise RuntimeError("document authorization changed before baseline")
            baseline_authorization_epoch = worker_authorization_epoch
            for item in roots:
                root_id = str(item["root_id"])
                if not root_authorized(begin_conn, root_id):
                    raise RuntimeError("document root authorization changed before baseline")
                begin_conn.execute(
                    "UPDATE file_watch_roots SET baseline_status='building',baseline_started_at=?,"
                    "baseline_completed_at=NULL,updated_at=? WHERE root_id=? AND enabled=1",
                    (now, now, root_id),
                )
                row = begin_conn.execute(
                    "SELECT last_event_id FROM file_watch_checkpoints WHERE root_id=?",
                    (root_id,),
                ).fetchone()
                previous = str(row["last_event_id"] or "") if row else ""
                start_id = previous if previous.isdigit() else process.ready_event_id
                begin_conn.execute(
                    "INSERT INTO file_watch_checkpoints(root_id,last_event_id,coverage_status,event_gap_count,"
                    "coverage_started_at,updated_at) VALUES(?,?,'partial',0,NULL,?) "
                    "ON CONFLICT(root_id) DO UPDATE SET last_event_id=excluded.last_event_id,"
                    "coverage_status='partial',coverage_started_at=NULL,updated_at=excluded.updated_at",
                    (root_id, start_id, now),
                )
            begin_conn.commit()
            return baseline_authorization_epoch
        finally:
            begin_conn.close()

    def finish_baseline_epoch(
        barrier: dict,
        baseline_authorization_epoch: int,
        deferred_deletes_by_root: dict[str, dict[str, str]],
    ) -> bool:
        at = _iso(str(barrier.get("observed_at") or ""))
        barrier_id = str(barrier.get("event_id") or "")
        if _transition_hook is not None:
            _transition_hook("before_finish_lock")
        with coverage_transition_lock:
            if stream_gap.is_set() or process.stopping:
                return False
            finish_conn = ensure_ready(database)
            try:
                if not finish_conn.in_transaction:
                    finish_conn.execute("BEGIN IMMEDIATE")
                if not _setting_bool(finish_conn, "document_change_tracking_enabled", True):
                    finish_conn.rollback()
                    return False
                if _document_change_epoch(finish_conn) != baseline_authorization_epoch:
                    finish_conn.rollback()
                    return False
                if any(not root_authorized(finish_conn, str(item["root_id"])) for item in roots):
                    finish_conn.rollback()
                    return False
                if any(
                    not _baseline_delete_revisions_match(
                        finish_conn,
                        str(item["root_id"]),
                        deferred_deletes_by_root.get(str(item["root_id"]), {}),
                    )
                    for item in roots
                ):
                    # An event writer changed a row after this pass captured its
                    # missing-file plan.  Discard the whole pass and rescan;
                    # coverage and deletion must never be promoted from stale
                    # absence evidence.
                    finish_conn.rollback()
                    return False
                for item in roots:
                    root_id = str(item["root_id"])
                    current = finish_conn.execute(
                        "SELECT last_event_id FROM file_watch_checkpoints WHERE root_id=?",
                        (root_id,),
                    ).fetchone()
                    previous = str(current["last_event_id"] or "") if current else ""
                    high_watermark = barrier_id
                    if previous.isdigit() and int(previous) > int(barrier_id):
                        high_watermark = previous
                    finish_conn.execute(
                        "UPDATE file_watch_roots SET baseline_status='complete',baseline_completed_at=?,updated_at=? "
                        "WHERE root_id=? AND enabled=1",
                        (at, at, root_id),
                    )
                    finish_conn.execute(
                        "UPDATE file_watch_checkpoints SET last_event_id=?,coverage_status='complete',"
                        "event_gap_count=0,coverage_started_at=?,updated_at=? WHERE root_id=?",
                        (high_watermark, at, at, root_id),
                    )
                    _delete_baseline_state_revisions(
                        finish_conn,
                        root_id,
                        deferred_deletes_by_root.get(root_id, {}),
                    )
                if stream_gap.is_set() or process.stopping:
                    finish_conn.rollback()
                    return False
                _set_document_change_pending(finish_conn, 0)
                _set_resume_pending(finish_conn, False)
                finish_conn.commit()
                return True
            finally:
                finish_conn.close()

    class BaselineInterrupted(Exception):
        """Stop a reconcile as soon as its helper can no longer protect it."""

    def baseline_should_stop() -> bool:
        if process.stopping:
            return True
        if not reconcile_window_open():
            baseline_eligibility_lost.set()
            return True
        return False

    def interruptible_baseline_candidates(root: Path) -> Iterator[Path]:
        scanned = 0
        candidate_iterator = iter(_iter_safe_files(root, stop_check=baseline_should_stop))
        while True:
            enumeration_started = (
                time.monotonic_ns() if baseline_metrics_enabled else 0
            )
            try:
                candidate = next(candidate_iterator)
            except StopIteration:
                if baseline_metrics_enabled:
                    record_baseline_metric(
                        "enumeration_elapsed_ns",
                        time.monotonic_ns() - enumeration_started,
                    )
                break
            except BaseException:
                if baseline_metrics_enabled:
                    record_baseline_metric(
                        "enumeration_elapsed_ns",
                        time.monotonic_ns() - enumeration_started,
                    )
                raise
            if baseline_metrics_enabled:
                record_baseline_metric(
                    "enumeration_elapsed_ns",
                    time.monotonic_ns() - enumeration_started,
                )
            if baseline_should_stop():
                raise BaselineInterrupted
            scanned += 1
            if (
                BASELINE_SCAN_THROTTLE_EVERY_FILES > 0
                and scanned % BASELINE_SCAN_THROTTLE_EVERY_FILES == 0
            ):
                # Pace the scan so it can run while the owner works instead of
                # waiting for an unattended window that never arrives.
                throttle_started = (
                    time.monotonic_ns() if baseline_metrics_enabled else 0
                )
                try:
                    time.sleep(BASELINE_SCAN_THROTTLE_SECONDS)
                finally:
                    if baseline_metrics_enabled:
                        record_baseline_metric(
                            "scan_throttle_elapsed_ns",
                            time.monotonic_ns() - throttle_started,
                        )
            candidate_started = (
                time.monotonic_ns() if baseline_metrics_enabled else 0
            )
            try:
                yield candidate
            finally:
                if baseline_metrics_enabled:
                    record_baseline_metric(
                        "candidate_total_elapsed_ns",
                        time.monotonic_ns() - candidate_started,
                    )
                    maybe_emit_baseline_progress()
        # The helper may stop while the final candidate is being fingerprinted.
        # Check once more after the underlying iterator is exhausted so a scan
        # cannot be promoted after losing its live event barrier at the tail.
        if baseline_should_stop():
            raise BaselineInterrupted

    def baseline_content_allowed(root_id: str, baseline_authorization_epoch: int) -> bool:
        if not reconcile_window_open():
            baseline_eligibility_lost.set()
            return False
        return bool(
            not process.stopping
            and content_allowed_for_root(root_id, baseline_authorization_epoch)
        )

    def stop_for_baseline_eligibility_loss() -> bool:
        baseline_eligibility_lost.set()
        with baseline_epoch_lock:
            epoch_state["active"] = False
        mark_all_partial("baseline_reconcile_window_closed")
        process.stop()
        return False

    def resume_barriered_baseline() -> bool:
        """Replay the retained cursor before acknowledging continuity; never rescan as history."""
        token = uuid.uuid4().hex
        waiter, resume = threading.Event(), threading.Event()
        with barrier_lock:
            barrier_results[token] = {"event": waiter, "resume": resume, "payload": None}
        deadline = time.monotonic() + 180
        try:
            if not process.request_barrier(token):
                fatal_worker("helper_barrier_failed")
                return False
            while not waiter.wait(timeout=0.05):
                if process.stopping:
                    return False
                if time.monotonic() >= deadline:
                    fatal_worker("helper_barrier_failed")
                    return False
            # The consumer is paused at the barrier. All preceding entries must
            # finish through the normal ordered pipeline, including ignored ones.
            while not process.stopping:
                probe = connect_readonly(database)
                try:
                    pending = _document_change_pending_count(probe)
                finally:
                    probe.close()
                drained = (pending == 0 and validation_queue.empty() and processor_queue.empty()
                           and stage_busy_since["validator"] is None and stage_busy_since["processor"] is None)
                if drained:
                    break
                if time.monotonic() >= deadline:
                    fatal_worker("helper_barrier_failed")
                    return False
                time.sleep(0.05)
            with barrier_lock:
                barrier = barrier_results.get(token, {}).get("payload")
            if not isinstance(barrier, dict) or not str(barrier.get("event_id", "")).isdigit():
                fatal_worker("helper_barrier_failed")
                return False
            # Match the fatal transition and normal baseline commit order.
            # Reversing these two locks can deadlock a resume against a drop.
            with baseline_lock, coverage_transition_lock:
                finish = connect(database)
                try:
                    finish.execute("BEGIN IMMEDIATE")
                    if process.stopping or stream_gap.is_set() or _document_change_epoch(finish) != worker_authorization_epoch:
                        return False
                    for item in roots:
                        root_id = str(item["root_id"])
                        row = finish.execute("SELECT c.last_event_id,c.coverage_status,r.baseline_status FROM file_watch_checkpoints c "
                            "JOIN file_watch_roots r ON c.root_id=r.root_id WHERE c.root_id=? AND r.enabled=1", (root_id,)).fetchone()
                        if not row or row["coverage_status"] != "complete" or row["baseline_status"] != "complete" or not root_authorized(finish, root_id):
                            return False
                        cursor = str(max(int(row["last_event_id"]), int(barrier["event_id"])))
                        finish.execute("UPDATE file_watch_checkpoints SET last_event_id=?,updated_at=? WHERE root_id=?", (cursor, _now(), root_id))
                    if stream_gap.is_set() or process.stopping:
                        return False
                    _set_resume_pending(finish, False)
                    finish.commit()
                    return True
                finally:
                    finish.close()
        finally:
            with barrier_lock:
                barrier_results.pop(token, None)
            resume.set()

    def perform_barriered_baseline(max_passes: int = DOCUMENT_BASELINE_MAX_PASSES) -> bool:
        with baseline_metrics_lock:
            baseline_metrics["max_passes"] = max(0, int(max_passes))
        maybe_emit_baseline_progress(stage="started", force=True)
        baseline_authorization_epoch = begin_baseline_epoch()
        if baseline_authorization_epoch is None:
            return False
        for attempt in range(max_passes):
            # The last attempt accepts a pass during which tracked files kept
            # changing.  Demanding a perfectly quiet pass is unreachable on a
            # machine in use -- 44k tracked files means something always moves
            # -- and the cost of that purity was that the baseline never
            # completed and not one change was ever recorded.  A busy pass
            # still holds a real helper barrier and the live event stream keeps
            # covering whatever moved during it, so the residual risk is a
            # first diff computed against a mid-edit fingerprint, not lost
            # history.  A genuine stream gap stays fail-closed: stream_gap is
            # re-checked here and again inside finish_baseline_epoch.  Benign
            # churn only sets scan_gap, which is cleared per pass so a quiet
            # pass is still promoted as exact rather than approximate.
            final_attempt = attempt == max_passes - 1
            with baseline_metrics_lock:
                baseline_metrics["pass_number"] = attempt + 1
            with baseline_epoch_lock:
                epoch_state["active"] = True
                epoch_state["dirty"] = False
                scan_gap.clear()
            scan_complete = True
            any_root_scanned = False
            incomplete_roots: list[str] = []
            authorization_lost = False
            deferred_deletes_by_root: dict[str, dict[str, str]] = {}
            for item in roots:
                root_id = str(item["root_id"])
                deferred_delete_revisions: dict[str, str] = {}
                baseline_conn = ensure_ready(database)
                try:
                    try:
                        with baseline_lock:
                            if baseline_should_stop():
                                raise BaselineInterrupted
                            metric_token = None
                            live_session_token = _DOCUMENT_BASELINE_LIVE_SESSION_PROBE.set(
                                live_content_allowed
                            )
                            if baseline_metrics_enabled:
                                public_root_ref = diagnostic_root_refs.get(root_id)
                                metric_token = _DOCUMENT_BASELINE_METRIC_RECORDER.set(
                                    lambda name, value=1, ref=public_root_ref: record_baseline_metric(
                                        name, value, root_ref=ref
                                    )
                                )
                            try:
                                result = build_document_baseline(
                                    baseline_conn,
                                    str(item["path"]),
                                    root_label=str(
                                        item.get("root_label") or "Tracked folder"
                                    ),
                                    paths=interruptible_baseline_candidates(
                                        Path(str(item["path"]))
                                    ),
                                    finalize_coverage=False,
                                    content_allowed_check=lambda root_id=str(item["root_id"]): baseline_content_allowed(
                                        root_id, baseline_authorization_epoch
                                    ),
                                    expected_epoch=baseline_authorization_epoch,
                                    deferred_delete_revisions=deferred_delete_revisions,
                                    _progress_heartbeat=heartbeat,
                                )
                            finally:
                                if metric_token is not None:
                                    _DOCUMENT_BASELINE_METRIC_RECORDER.reset(metric_token)
                                _DOCUMENT_BASELINE_LIVE_SESSION_PROBE.reset(
                                    live_session_token
                                )
                    except BaselineInterrupted:
                        if baseline_eligibility_lost.is_set():
                            return stop_for_baseline_eligibility_loss()
                        with baseline_epoch_lock:
                            epoch_state["active"] = False
                        return False
                    scan_complete = scan_complete and result.get("baseline_status") == "complete"
                    if result.get("baseline_status") == "complete":
                        any_root_scanned = True
                        deferred_deletes_by_root[root_id] = deferred_delete_revisions
                    else:
                        incomplete_roots.append(root_id)
                    authorization_lost = authorization_lost or result.get("baseline_status") in {
                        "disabled", "removed", "unconfigured", "authorization_changed"
                    }
                    record_baseline_metric("root_scan_attempts_finished_count")
                    for metric_name, result_name in (
                        ("files_indexed_count", "files_indexed"),
                        ("files_ignored_count", "files_ignored"),
                        ("files_failed_count", "files_failed"),
                        ("files_written_count", "files_written"),
                        ("files_unchanged_count", "files_unchanged"),
                    ):
                        record_baseline_metric(metric_name, int(result.get(result_name) or 0))
                    maybe_emit_baseline_progress()
                finally:
                    baseline_conn.close()
            maybe_emit_baseline_progress(stage="pass_finished", force=True)
            if authorization_lost:
                with baseline_epoch_lock:
                    epoch_state["active"] = False
                process.stop()
                return False
            if not scan_complete and not (final_attempt and any_root_scanned):
                # build_document_baseline counts one unreadable file the same as
                # a total failure: a single OSError anywhere under a root marks
                # that root partial.  This used to `break`, abandoning the epoch
                # before the barrier and without ever retrying -- so across 44k
                # files spanning encrypted disk images, in-flight downloads and
                # iCloud placeholders, where something is always unreadable, a
                # closed baseline was unreachable on this machine.
                #
                # Retry instead, and let the last attempt proceed provided at
                # least one root did scan cleanly.  That floor is what keeps a
                # genuinely broken volume -- nothing readable anywhere -- failing
                # closed rather than promoting an empty baseline.
                #
                # The cost, stated plainly: a root whose scan was incomplete is
                # still promoted to baseline_status='complete', so files it
                # missed have no fingerprint and their first change is recorded
                # as `created` rather than `modified`.  Coverage stays partial
                # and event_gap_count keeps counting, so nothing claims that gap
                # does not exist.  Deletion promotion is unaffected: an
                # incompletely scanned root contributes no deferred deletes, and
                # _baseline_delete_revisions_match over an empty set removes
                # nothing.
                continue
            if not reconcile_window_open(force=True):
                return stop_for_baseline_eligibility_loss()
            token = uuid.uuid4().hex
            waiter = threading.Event()
            resume = threading.Event()
            with barrier_lock:
                barrier_results[token] = {
                    "event": waiter,
                    "resume": resume,
                    "payload": None,
                }
            if not process.request_barrier(token) or not waiter.wait(timeout=10.0):
                with barrier_lock:
                    barrier_results.pop(token, None)
                resume.set()
                fatal_worker("helper_barrier_failed")
                break
            with barrier_lock:
                barrier_result = barrier_results.pop(token, {})
                barrier = barrier_result.get("payload")
            try:
                if not reconcile_window_open(force=True):
                    return stop_for_baseline_eligibility_loss()
                with baseline_epoch_lock:
                    # Release the consumer only after this thread owns the
                    # epoch decision lock.  Normal/directory events cannot pass
                    # the active check until completion or retry is decided,
                    # while a dropped/wrapped control event remains able to
                    # stop the helper before finish commits.
                    resume.set()
                    dirty = bool(
                        epoch_state["dirty"] or scan_gap.is_set() or stream_gap.is_set()
                    )
                    accept_approximate = final_attempt and not stream_gap.is_set()
                    if (not dirty or accept_approximate) and isinstance(barrier, dict):
                        with baseline_lock:
                            finished = finish_baseline_epoch(
                                barrier,
                                baseline_authorization_epoch,
                                deferred_deletes_by_root,
                            )
                        if finished:
                            epoch_state["active"] = False
                            if incomplete_roots:
                                # finish_baseline_epoch just zeroed the gap counters
                                # for everything it re-covered, which is right for
                                # the roots it actually read.  A root whose scan
                                # skipped files was not re-covered, and letting that
                                # promotion stand would report complete coverage
                                # over files nobody managed to read.  Record it
                                # after the commit, as benign: the enumeration is
                                # accepted, the gap in it is not hidden.
                                mark_all_partial(
                                    "baseline_scan_incomplete",
                                    fatal=False,
                                    affects_attribution=True,
                                    root_ids=incomplete_roots,
                                )
                            maybe_emit_baseline_progress(stage="completed", force=True)
                            return True
            finally:
                resume.set()
        with baseline_epoch_lock:
            epoch_state["active"] = False
        mark_all_partial("baseline_not_clean")
        return False

    if baseline_metrics_enabled:
        metrics_started_ns = time.monotonic_ns()
        with baseline_metrics_emit_lock:
            with baseline_metrics_lock:
                baseline_metrics_started_ns = metrics_started_ns
                baseline_metrics["last_emit_ns"] = metrics_started_ns
                baseline_metrics_active.set()
    try:
        baseline_ready = resume_barriered_baseline() if resume_existing_baseline else perform_barriered_baseline()
        if resume_existing_baseline and not baseline_ready and not process.stopping:
            fatal_worker("baseline_not_clean")
    except Exception as baseline_error:
        shutdown_error = stop_and_join_startup_worker("baseline_exception")
        if shutdown_error is not None:
            raise baseline_error from shutdown_error
        raise
    finally:
        if baseline_metrics_enabled:
            with baseline_metrics_emit_lock:
                baseline_metrics_active.clear()

    if not baseline_ready and process.stopping:
        # A lost-stream reset is allowed to wait for the current root scan's
        # baseline lock.  Do not expose this startup result until the terminal
        # partial/NULL-watermark transition and all queue sentinels have
        # completed; otherwise a direct caller can briefly observe the earlier
        # building/zero-gap state or start a replacement too soon.  The daemon's
        # outer join remains harmless and keeps one lifecycle contract.
        join_document_change_worker(startup_worker)
        return {
            "started": False,
            "reason": (
                "stream_reset_failed"
                if process.restart_blocked
                else (
                    "reconcile_eligibility_lost"
                    if baseline_eligibility_lost.is_set()
                    else "authorization_changed_or_reconcile_restart"
                )
            ),
            "root_count": len(roots),
            "process": process,
            "consumer": consumer,
            "reader": reader,
            "validator": validator,
            "processor": processor,
            "monitor": None,
            "raw_access": False,
        }

    registry_mtime = _registry_path(runtime_root).stat().st_mtime_ns if _registry_path(runtime_root).exists() else 0
    @worker_connections.thread
    def monitor() -> None:
        try:
            while process.process is not None and process.process.poll() is None:
                time.sleep(2)
                state_conn = worker_connections.connect("state")
                try:
                    if _document_change_epoch(state_conn) != worker_authorization_epoch:
                        process.stop()
                        return
                    if not _setting_bool(state_conn, "document_change_tracking_enabled", True):
                        process.stop()
                        return
                    now = time.monotonic()
                    stalled = any(start is not None and now - start > HEARTBEAT_TTL_SECONDS
                                  for start in tuple(stage_busy_since.values()))
                    alive = all(thread.is_alive() for thread in (reader, consumer, validator, processor))
                    phase = "running" if alive and not stalled else "stalled"
                    if phase == "running" and any(stage_wait_reason.values()):
                        phase = ("database_busy" if "database_busy" in stage_wait_reason.values() else
                                 "waiting_for_session" if "waiting_for_session" in stage_wait_reason.values() else "backpressure")
                    if heartbeat.touch(state_conn, phase=phase):
                        stamp = _now()
                        metrics = json.dumps({"queue_depths": {"input": event_queue.qsize(), "validation": validation_queue.qsize(), "processor": processor_queue.qsize()},
                            "queue_high_water": dict(queue_high_water), "completed": dict(stage_completed),
                            "waiting": dict(stage_wait_reason),
                            "in_flight_seconds": {key: round(now - value, 2) if value is not None else 0 for key, value in tuple(stage_busy_since.items())},
                            "raw_access": False}, separators=(",", ":"))
                        state_conn.execute("INSERT INTO daemon_state(key,value,updated_at) VALUES('document_pipeline_metrics',?,?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at", (metrics, stamp))
                    authorized_ids = {
                        str(row["root_id"])
                        for row in state_conn.execute("SELECT root_id FROM file_watch_roots WHERE enabled=1")
                    }
                    state_conn.execute(
                        "DELETE FROM file_watch_state WHERE present=0 AND datetime(updated_at)<datetime(?,'-30 seconds')",
                        (_now(),),
                    )
                    state_conn.commit()
                finally:
                    worker_connections.release(state_conn)
                current_mtime = _registry_path(runtime_root).stat().st_mtime_ns if _registry_path(runtime_root).exists() else 0
                if current_mtime != registry_mtime or authorized_ids != {str(item["root_id"]) for item in roots}:
                    process.stop()
                    return
                # Incomplete coverage used to stop the helper here so the daemon
                # would start a fresh barriered stream.  On a machine in use it
                # is the permanent steady state -- benign directory churn marks
                # a gap continuously -- so this fired about two seconds after
                # every start, the daemon then sat out its retry cooldown, and
                # not one file change was ever recorded.  Restoring complete
                # coverage is a reconcile decision, not a reason to destroy the
                # only channel that records anything; the surface already
                # refuses to claim complete coverage on its own.  Real stream
                # failures either stop the helper at their own call site or are
                # recorded as a gap.
        except Exception:
            fatal_worker("worker_monitor_exception")
    monitor_thread = threading.Thread(target=monitor, name="pam-document-config-monitor", daemon=True)
    monitor_thread.start()
    return {
        "started": True,
        "root_count": len(roots),
        "process": process,
        "consumer": consumer,
        "reader": reader,
        "validator": validator,
        "processor": processor,
        "monitor": monitor_thread,
        # Counts only; the path maps themselves never leave the worker.
        "merge_state": lambda: {
            "latest_path_count": len(latest_order_by_path),
            "debt_path_count": len(superseded_debt_by_path),
            "superseded_skipped_count": int(merge_metrics["superseded_skipped_count"]),
            "merged_version_marked_count": int(
                merge_metrics["merged_version_marked_count"]
            ),
            "slow_stability_entry_count": int(
                merge_metrics["slow_stability_entry_count"]
            ),
        },
        "raw_access": False,
    }


def _document_signal_path_hmac(conn: sqlite3.Connection, path: Path) -> str:
    """Derive the same private path reference used by foreground signals."""

    database_path = _connection_database_path(conn)
    if database_path is None:
        return ""
    try:
        from .document_signals import _path_hmac

        value = str(_path_hmac(database_path.parent, path) or "")
    except (OSError, RuntimeError, ValueError):
        return ""
    return value if value.startswith("doc_") else ""


class DisplaySourceRetired(PermissionError):
    """Expected owner-local exclusion, never a scan failure or source deletion."""


def _snapshot(
    conn: sqlite3.Connection,
    root: Path,
    path: Path,
    *,
    allow_content: bool = True,
    content_allowed_check=None,
) -> dict:
    from .presentation_scope import for_connection
    scope = for_connection(conn)
    if scope.invalid or not scope.unchanged():
        raise PermissionError("display scope unavailable")
    if scope.hidden(str(path)):
        raise DisplaySourceRetired("source retired from local tracking")
    descriptor, relative = _open_file_beneath(root, path)
    recovery = None
    recovery_token = None
    snapshot_restore_error = None
    try:
        stat = os.fstat(descriptor)
        recovery = _DatalessReadRecovery(descriptor, stat, content_allowed_check)
        recovery_token = _DOCUMENT_DATALESS_READ_RECOVERY.set(recovery)
        if allow_content and content_allowed_check is not None:
            try:
                if not bool(content_allowed_check()):
                    raise PermissionError("content read deferred by live session state")
            except PermissionError:
                raise
            except Exception as exc:
                raise PermissionError("content read deferred by unknown session state") from exc
        # Local key/lock setup must finish before temporary cloud materialization
        # is enabled. Only the target reads and their live gates belong inside it.
        secret = _hmac_key(conn)
        suffix = path.suffix.lower()
        text_bytes: bytes | None = None
        archive_file = None
        if allow_content and suffix in TEXT_SUFFIXES and stat.st_size <= MAX_TEXT_BYTES:
            text_bytes = _read_open_file(
                descriptor,
                max_bytes=MAX_TEXT_BYTES,
                content_allowed_check=content_allowed_check,
            )
        elif allow_content and suffix in PACKAGE_SUFFIXES and stat.st_size <= MAX_PACKAGE_BYTES:
            archive_file = os.fdopen(os.dup(descriptor), "rb", closefd=True)
        if text_bytes is None and archive_file is None:
            structure = {"kind": "metadata", "size_bucket": _size_bucket(stat.st_size)}
            detail = "metadata_only"
            protected = _protected_name(path.name)
        else:
            try:
                structure, detail, protected = _extract_structure(
                    path,
                    raw_bytes=text_bytes,
                    archive_file=archive_file,
                    file_size=stat.st_size,
                )
            except (KeyError, OSError, RuntimeError, ValueError, zipfile.BadZipFile, ElementTree.ParseError):
                # Encrypted, malformed, over-expanded, or otherwise unsupported
                # containers remain observable only as metadata/content fingerprints.
                structure = {"kind": "metadata", "size_bucket": _size_bucket(stat.st_size)}
                detail = "metadata_only"
                protected = _protected_name(path.name)
            finally:
                if archive_file is not None:
                    archive_file.close()
        if allow_content and content_allowed_check is not None and not bool(content_allowed_check()):
            raise PermissionError("content read deferred after structure extraction")
        after = os.fstat(descriptor)
        if (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns
        ):
            raise OSError("document changed while fingerprinting")
        if protected:
            structure["labels"] = []
        structure_hmac = _hmac(secret, json.dumps(structure, ensure_ascii=False, sort_keys=True))
        content_hmac = (
            _file_content_hmac(secret, descriptor, stat.st_size, content_allowed_check=content_allowed_check)
            if allow_content and stat.st_size <= MAX_PACKAGE_BYTES
            else _hmac(secret, f"metadata:{stat.st_size}:{stat.st_mtime_ns}")
        )
        if recovery.policy is not None:
            # Do not adopt a result revoked or changed during the last read.
            recovery._check()
            # Restore before path resolution, auxiliary keys and project lookup.
            # The outer finally still owns restoration on every failure path.
            recovery.close()
        safe_name = "" if protected else _safe_label(redact_for_storage(path.name, max_len=120), 120)
        flags = ["document_change"] + (["protected_content"] if protected else [])
        return {
            "file_id": _file_id_for_relative(conn, root, relative),
            "document_path_hmac": _document_signal_path_hmac(conn, path),
            "file_name_redacted": safe_name,
            "file_type": path.suffix.lower().lstrip(".") or "unknown",
            "device_id": _hmac(secret, str(stat.st_dev)),
            "inode_id": _hmac(secret, f"{stat.st_dev}:{stat.st_ino}"),
            "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds"),
            "size_bytes": int(stat.st_size),
            "content_hmac": content_hmac,
            "structure": structure,
            "structure_hmac_json": json.dumps(_hashed_structure(secret, structure), ensure_ascii=False, sort_keys=True),
            "detail_level": "protected" if protected else detail,
            "privacy_flags": flags,
            "project_label": _project_label_for_path(conn, root, path),
        }
    except DatalessReadPolicyRestoreError as error:
        snapshot_restore_error = error
        raise
    finally:
        restore_error = snapshot_restore_error
        cleanup_error = None
        try:
            if recovery is not None:
                recovery.close()
        except BaseException as error:
            restore_error = error
        try:
            if recovery_token is not None:
                _DOCUMENT_DATALESS_READ_RECOVERY.reset(recovery_token)
        except BaseException as error:
            cleanup_error = DatalessReadPolicyRestoreError(
                "document reader recovery context restore failed"
            )
            cleanup_error.__cause__ = error
        try:
            os.close(descriptor)
        except BaseException as error:
            if cleanup_error is None:
                cleanup_error = error
        # Cleanup must still be attempted, but a close/reset failure must never
        # replace the policy-restore exception with a skippable file error.
        if restore_error is not None:
            raise restore_error from cleanup_error
        if cleanup_error is not None:
            raise cleanup_error


def _stable_snapshot(
    conn: sqlite3.Connection,
    root: Path,
    path: Path,
    *,
    allow_content: bool,
    stable_seconds: float,
    content_allowed_check=None,
) -> dict:
    before = _safe_file_stat(root, path)
    if stable_seconds > 0:
        time.sleep(min(stable_seconds, 2.0))
        after = _safe_file_stat(root, path)
        if (before.st_dev, before.st_size, before.st_mtime_ns, before.st_ino) != (
            after.st_dev, after.st_size, after.st_mtime_ns, after.st_ino
        ):
            raise OSError("document is still changing")
    if allow_content and content_allowed_check is not None:
        try:
            if not bool(content_allowed_check()):
                raise PermissionError("content read deferred by live session state")
        except PermissionError:
            raise
        except Exception as exc:
            raise PermissionError("content read deferred by unknown session state") from exc
    return _snapshot(
        conn,
        root,
        path,
        allow_content=allow_content,
        content_allowed_check=content_allowed_check,
    )


def _extract_structure(
    path: Path,
    *,
    raw_bytes: bytes | None = None,
    archive_file=None,
    file_size: int | None = None,
) -> tuple[dict, str, bool]:
    suffix = path.suffix.lower()
    if suffix in TEXT_SUFFIXES:
        raw = raw_bytes if raw_bytes is not None else path.read_bytes()[: MAX_TEXT_BYTES + 1]
        if len(raw) > MAX_TEXT_BYTES:
            return {"kind": "metadata", "size_bucket": _size_bucket(len(raw))}, "metadata_only", _protected_name(path.name)
        text = raw.decode("utf-8", errors="replace")
        protected = _protected_name(path.name) or _protected_text(text)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        labels = _text_structure_labels(path.suffix.lower(), lines)
        return {"kind": "text", "line_count": len(lines), "units": lines, "labels": labels}, "detailed", protected
    if suffix in {".docx", ".odt"}:
        # Headings are what makes a Word change readable ("涉及：本周结论"),
        # exactly like Markdown headings and slide titles elsewhere in this
        # dispatcher.  Without them a .docx change can only say that the file
        # moved, which is the least useful thing the surface can report.
        headings: list[str] = []
        texts = _zip_document_paragraphs(
            path,
            ("word/document.xml", "content.xml"),
            raw_bytes=raw_bytes,
            archive_file=archive_file,
            headings_out=headings,
        )
        protected = _protected_name(path.name) or _protected_text(" ".join(texts))
        return (
            {
                "kind": "document",
                "unit_count": len(texts),
                "units": texts,
                "labels": headings,
            },
            "detailed",
            protected,
        )
    if suffix == ".pptx":
        with zipfile.ZipFile(archive_file or (io.BytesIO(raw_bytes) if raw_bytes is not None else path)) as archive:
            presentation_data = _safe_zip_read(archive, "ppt/presentation.xml") if "ppt/presentation.xml" in archive.namelist() else b""
            relationship_data = _safe_zip_read(archive, "ppt/_rels/presentation.xml.rels") if "ppt/_rels/presentation.xml.rels" in archive.namelist() else b""
            ordered = _ordered_package_members(
                presentation_data,
                relationship_data,
                item_tag="sldId",
                base_directory="ppt",
            )
            names = [member for _, member in ordered if member in archive.namelist()]
            if not names:
                names = sorted(
                    (name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
                    key=_natural_package_key,
                )
            slides = []
            titles = []
            extracted_bytes = 0
            for name in names:
                data = _safe_zip_read(archive, name)
                extracted_bytes += len(data)
                if extracted_bytes > MAX_TEXT_BYTES:
                    raise ValueError("presentation text exceeds extraction cap")
                texts = _xml_text(data)
                slides.append(" ".join(texts))
                titles.append(_safe_label(texts[0], 80) if texts else "")
        protected = _protected_name(path.name) or _protected_text(" ".join(slides))
        return {"kind": "presentation", "unit_count": len(slides), "units": slides, "labels": titles}, "detailed", protected
    if suffix == ".xlsx":
        with zipfile.ZipFile(archive_file or (io.BytesIO(raw_bytes) if raw_bytes is not None else path)) as archive:
            workbook_data = _safe_zip_read(archive, "xl/workbook.xml") if "xl/workbook.xml" in archive.namelist() else b""
            workbook = _xlsx_sheet_names(workbook_data)
            relationship_data = _safe_zip_read(archive, "xl/_rels/workbook.xml.rels") if "xl/_rels/workbook.xml.rels" in archive.namelist() else b""
            ordered = _ordered_package_members(
                workbook_data,
                relationship_data,
                item_tag="sheet",
                base_directory="xl",
                label_attribute="name",
            )
            shared_data = _safe_zip_read(archive, "xl/sharedStrings.xml") if "xl/sharedStrings.xml" in archive.namelist() else b""
            shared = _xlsx_shared_strings(shared_data)
            ordered = [(label, member) for label, member in ordered if member in archive.namelist()]
            if not ordered:
                names = sorted(
                    (name for name in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)),
                    key=_natural_package_key,
                )
                ordered = [
                    (workbook[index] if index < len(workbook) else f"sheet_{index + 1}", name)
                    for index, name in enumerate(names)
                ]
            sheets = []
            sensitive_text = [*workbook, *shared]
            extracted_bytes = sum(len(item.encode("utf-8", errors="replace")) for item in sensitive_text)
            for sheet_name, name in ordered:
                data = _safe_zip_read(archive, name)
                extracted_bytes += len(data)
                if extracted_bytes > MAX_TEXT_BYTES:
                    raise ValueError("spreadsheet text exceeds extraction cap")
                values = _xlsx_sheet_values(data, shared)
                sensitive_text.extend(values)
                sheets.append(" | ".join([sheet_name, *values]))
        protected = _protected_name(path.name) or _protected_text(" ".join(sensitive_text))
        return {"kind": "spreadsheet", "unit_count": len(sheets), "units": sheets, "labels": [_safe_label(item, 60) for item in workbook[:40]]}, "detailed", protected
    size = file_size if file_size is not None else (len(raw_bytes) if raw_bytes is not None else path.stat().st_size)
    return {"kind": "metadata", "size_bucket": _size_bucket(size)}, "metadata_only", _protected_name(path.name)


def _paragraph_is_heading(node) -> bool:
    """Report whether a Word/ODF paragraph node carries a heading style.

    ODF marks headings structurally (``text:h``); Word keeps them as ordinary
    paragraphs whose ``w:pStyle`` names a Heading or Title style.  Only the
    style name is inspected, never any body text beyond what the caller
    already collects.
    """
    if node.tag.rsplit("}", 1)[-1] == "h":
        return True
    for child in node.iter():
        if child.tag.rsplit("}", 1)[-1] != "pStyle":
            continue
        for key, value in child.attrib.items():
            if key.rsplit("}", 1)[-1] != "val":
                continue
            style = str(value or "").strip().lower().replace(" ", "")
            if style.startswith("heading") or style in {"title", "subtitle"}:
                return True
    return False


def _zip_document_paragraphs(
    path: Path,
    candidates: tuple[str, ...],
    *,
    raw_bytes: bytes | None = None,
    archive_file=None,
    headings_out: list[str] | None = None,
) -> list[str]:
    with zipfile.ZipFile(archive_file or (io.BytesIO(raw_bytes) if raw_bytes is not None else path)) as archive:
        for name in candidates:
            if name in archive.namelist():
                data = _safe_zip_read(archive, name)
                try:
                    root = _parse_office_xml(data)
                except ElementTree.ParseError:
                    return []
                paragraphs: list[str] = []
                for node in root.iter():
                    if node.tag.rsplit("}", 1)[-1] not in {"p", "h"}:
                        continue
                    text = "".join(str(child.text or "") for child in node.iter() if child.text).strip()
                    if text:
                        paragraphs.append(text)
                        if headings_out is not None and _paragraph_is_heading(node):
                            headings_out.append(text)
                return paragraphs or _xml_text(data)
    return []


def _safe_zip_read(archive: zipfile.ZipFile, name: str) -> bytes:
    info = archive.getinfo(name)
    if info.flag_bits & 0x1:
        raise RuntimeError("encrypted office container")
    if info.file_size > MAX_TEXT_BYTES:
        raise ValueError("office XML exceeds extraction cap")
    data = archive.read(info)
    if len(data) > MAX_TEXT_BYTES:
        raise ValueError("office XML exceeds extraction cap")
    return data


# Entity expansion turns a few hundred bytes of markup into gigabytes of text
# inside the parser, long before the byte caps above get to weigh anything.  A
# recent libexpat refuses that amplification on its own, but the protection
# belongs to whichever copy of the library the running interpreter links rather
# than to this module: the identical call is still a memory bomb under an older
# one.  No part of an OOXML or ODF package has a legitimate reason to carry a
# DTD, and entities can only be declared inside one, so refusing the doctype
# outright retires the whole class -- expansion bombs and external-entity reads
# alike -- without depending on the expat build underneath.
_DOCTYPE_SCAN_BYTES = 64 * 1024
_DOCTYPE_MARKERS = tuple(
    "<!DOCTYPE".encode(encoding) for encoding in ("utf-8", "utf-16-le", "utf-16-be")
)


def _parse_office_xml(data: bytes) -> ElementTree.Element:
    """Parse a package member, refusing any part that declares a DTD.

    Rejection raises ``ElementTree.ParseError`` so that every caller's existing
    handling of a malformed part covers a refused one as well: the document
    degrades to a metadata-only record instead of being expanded.
    """
    if any(marker in data[:_DOCTYPE_SCAN_BYTES] for marker in _DOCTYPE_MARKERS):
        raise ElementTree.ParseError("DTD is not allowed in an office document part")
    return ElementTree.fromstring(data)


def _natural_package_key(value: str) -> tuple:
    return tuple(
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", value)
    )


def _ordered_package_members(
    manifest_data: bytes,
    relationship_data: bytes,
    *,
    item_tag: str,
    base_directory: str,
    label_attribute: str | None = None,
) -> list[tuple[str, str]]:
    """Resolve Office package members in presentation/workbook order."""
    if not manifest_data or not relationship_data:
        return []
    try:
        manifest = _parse_office_xml(manifest_data)
        relationships = _parse_office_xml(relationship_data)
    except ElementTree.ParseError:
        return []
    targets: dict[str, str] = {}
    for node in relationships.iter():
        if node.tag.rsplit("}", 1)[-1] != "Relationship":
            continue
        if str(node.attrib.get("TargetMode") or "").lower() == "external":
            continue
        relationship_id = str(node.attrib.get("Id") or "")
        target = str(node.attrib.get("Target") or "")
        normalized = posixpath.normpath(
            target.lstrip("/")
            if target.startswith("/")
            else posixpath.join(base_directory, target)
        )
        if relationship_id and target and normalized != ".." and not normalized.startswith("../"):
            targets[relationship_id] = normalized
    ordered: list[tuple[str, str]] = []
    for node in manifest.iter():
        if node.tag.rsplit("}", 1)[-1] != item_tag:
            continue
        relationship_id = ""
        for key, value in node.attrib.items():
            local_name = key.rsplit("}", 1)[-1]
            namespace = key[1:].split("}", 1)[0] if key.startswith("{") and "}" in key else ""
            if key == "r:id" or (local_name == "id" and namespace.endswith("/relationships")):
                relationship_id = str(value)
                break
        member = targets.get(relationship_id, "")
        if not member:
            continue
        label = str(node.attrib.get(label_attribute) or "") if label_attribute else ""
        ordered.append((label, member))
    return ordered


def _text_structure_labels(suffix: str, lines: list[str]) -> list[str]:
    labels: list[str] = []
    patterns: tuple[re.Pattern[str], ...]
    if suffix in {".md", ".markdown", ".rst", ".txt"}:
        patterns = (re.compile(r"^#{1,6}\s+(.+)$"),)
    elif suffix == ".py":
        patterns = (re.compile(r"^(?:async\s+)?def\s+([A-Za-z_][\w]*)"), re.compile(r"^class\s+([A-Za-z_][\w]*)"))
    elif suffix in {".swift", ".kt", ".java", ".go", ".rs", ".c", ".h", ".cpp", ".hpp"}:
        patterns = (re.compile(r"^(?:public\s+|private\s+|internal\s+|static\s+)*(?:func|class|struct|enum|protocol|interface|fn)\s+([A-Za-z_][\w]*)"),)
    elif suffix in {".js", ".jsx", ".ts", ".tsx"}:
        patterns = (
            re.compile(r"^(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)"),
            re.compile(r"^(?:export\s+)?class\s+([A-Za-z_$][\w$]*)"),
            re.compile(r"^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*="),
        )
    elif suffix in {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg"}:
        patterns = (re.compile(r'^["\']?([^"\'=:#]{1,60})["\']?\s*[:=]'), re.compile(r"^\[([^\]]+)\]"))
    else:
        patterns = ()
    for line in lines:
        for pattern in patterns:
            match = pattern.search(line)
            if not match:
                continue
            label = _safe_label(redact_for_storage(match.group(1), max_len=80), 80)
            if label and label not in labels:
                labels.append(label)
            break
        if len(labels) >= 40:
            break
    return labels


def _xml_text(data: bytes) -> list[str]:
    try:
        root = _parse_office_xml(data)
    except ElementTree.ParseError:
        return []
    return [str(node.text).strip() for node in root.iter() if node.text and str(node.text).strip()]


def _xlsx_sheet_names(data: bytes) -> list[str]:
    if not data:
        return []
    try:
        root = _parse_office_xml(data)
    except ElementTree.ParseError:
        return []
    return [
        str(node.attrib.get("name") or "").strip()
        for node in root.iter()
        if node.tag.rsplit("}", 1)[-1] == "sheet" and str(node.attrib.get("name") or "").strip()
    ]


def _xlsx_shared_strings(data: bytes) -> list[str]:
    if not data:
        return []
    try:
        root = _parse_office_xml(data)
    except ElementTree.ParseError:
        return []
    values: list[str] = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] != "si":
            continue
        value = "".join(str(child.text or "") for child in node.iter() if child.tag.rsplit("}", 1)[-1] == "t").strip()
        values.append(value)
    return values


def _xlsx_sheet_values(data: bytes, shared: list[str]) -> list[str]:
    try:
        root = _parse_office_xml(data)
    except ElementTree.ParseError:
        return []
    values: list[str] = []
    for cell in root.iter():
        if cell.tag.rsplit("}", 1)[-1] != "c":
            continue
        reference = str(cell.attrib.get("r") or "cell")
        kind = str(cell.attrib.get("t") or "")
        raw = ""
        inline_parts: list[str] = []
        for child in cell.iter():
            local = child.tag.rsplit("}", 1)[-1]
            if local == "v" and child.text is not None:
                raw = str(child.text).strip()
            elif local == "t" and child.text is not None:
                inline_parts.append(str(child.text))
        if kind == "s" and raw.isdigit() and int(raw) < len(shared):
            value = shared[int(raw)]
        elif inline_parts:
            value = "".join(inline_parts).strip()
        else:
            value = raw
        if value:
            values.append(f"{reference}={value}")
    return values


def _hashed_structure(secret: bytes, structure: dict) -> dict:
    units = [str(item) for item in structure.get("units") or []]
    return {
        "kind": str(structure.get("kind") or "metadata"),
        "unit_count": int(structure.get("unit_count") or len(units)),
        "line_count": int(structure.get("line_count") or 0),
        "size_bucket": str(structure.get("size_bucket") or ""),
        "unit_hmacs": [_hmac(secret, item) for item in units],
        "labels": [
            _safe_label(redact_for_storage(str(item), max_len=80), 80)
            for item in (structure.get("labels") or []) if item
        ][:40],
    }


def _change_summary(old: dict, new: dict) -> str:
    if new["detail_level"] == "protected":
        return "检测到受保护文档发生变化；未保存文件名或内容摘要。"
    if new["detail_level"] != "detailed":
        return "检测到文件变化，但没有可安全概括的前后内容。"
    try:
        before = json.loads(str(old.get("structure_hmac_json") or "{}"))
        after = json.loads(str(new.get("structure_hmac_json") or "{}"))
    except json.JSONDecodeError:
        return "检测到文件内容发生变化。"
    before_units = set(before.get("unit_hmacs") or [])
    after_units = set(after.get("unit_hmacs") or [])
    added = len(after_units - before_units)
    removed = len(before_units - after_units)
    after_labels = [str(item) for item in (after.get("labels") or [])]
    labels = [item for item in after_labels if item and item not in (before.get("labels") or [])][:3]
    kind = str(after.get("kind") or "document")
    noun = {"presentation": "幻灯片", "spreadsheet": "工作表/单元格", "document": "段落", "text": "行/段落"}.get(kind, "内容单元")
    before_ordered = list(before.get("unit_hmacs") or [])
    after_ordered = list(after.get("unit_hmacs") or [])
    changed_positions = [
        index + 1
        for index in range(min(len(before_ordered), len(after_ordered)))
        if before_ordered[index] != after_ordered[index]
    ][:6]
    if added == 0 and removed == 0:
        summary = f"{noun}内容发生变化，数量大致不变。"
    else:
        summary = f"{noun}结构发生变化：新增约 {added}，移除约 {removed}。"
    if changed_positions and kind in {"presentation", "spreadsheet"}:
        unit = "页" if kind == "presentation" else "个工作表"
        summary += " 变化位置：第 " + "、".join(str(item) for item in changed_positions) + f" {unit}。"
        for position in changed_positions:
            if position <= len(after_labels) and after_labels[position - 1] and after_labels[position - 1] not in labels:
                labels.append(after_labels[position - 1])
            if len(labels) >= 3:
                break
    if labels:
        summary += " 涉及：" + "、".join(labels) + "。"
    return redact_for_storage(summary, max_len=360)


def _created_summary(snapshot: dict) -> str:
    if snapshot["detail_level"] == "protected":
        return "检测到受保护文档已创建；未保存文件名或内容摘要。"
    if snapshot["detail_level"] == "metadata_only":
        return "检测到文件已创建，但没有可安全概括的内容。"
    return "检测到文件已创建并建立安全结构指纹。"


def _record_change(
    conn: sqlite3.Connection,
    source: dict | sqlite3.Row,
    root_id: str,
    kind: str,
    at: str,
    summary: str,
    detail: str,
    *,
    helper_event_id: str,
) -> dict:
    data = _state_dict(source) if isinstance(source, sqlite3.Row) else source
    file_id = str(data["file_id"])
    internal_root_ids = {
        str(row["root_id"])
        for row in conn.execute("SELECT root_id FROM file_watch_roots").fetchall()
    }
    internal_root_ids.add(root_id)
    raw_project = str(data.get("project_label") or _root_label(conn, root_id))
    project = _safe_root_label(raw_project)
    if (
        _contains_internal_root_identity(project, internal_root_ids)
        or _contains_absolute_path_signal(raw_project)
        or "\\" in raw_project
    ):
        project = "Tracked folder"
    raw_file_name = str(data.get("file_name_redacted") or "")
    file_name = "" if detail == "protected" else _safe_label(
        redact_for_storage(raw_file_name, max_len=120),
        120,
    )
    if _contains_internal_root_identity(file_name, internal_root_ids):
        file_name = "file_redacted"
    if "/" in raw_file_name or "\\" in raw_file_name or raw_file_name.startswith("~"):
        file_name = "file_redacted"
    raw_file_type = str(data.get("file_type") or "unknown")
    file_type = raw_file_type if raw_file_type in PUBLIC_FILE_TYPES else "unknown"
    summary = redact_for_storage(str(summary or ""), max_len=360)
    if _contains_internal_root_identity(summary, internal_root_ids) or _contains_absolute_path_signal(summary):
        summary = "检测到文件变化，但摘要未能安全展示。"
    existing = conn.execute(
        "SELECT id, version_count, detail_level FROM file_change_events WHERE file_id=? AND change_kind=? "
        "AND date(last_changed_at, 'localtime')=date(?, 'localtime') ORDER BY last_changed_at DESC LIMIT 1",
        (file_id, kind, at),
    ).fetchone()
    flags = data.get("privacy_flags") or []
    flags_json = flags if isinstance(flags, str) else json.dumps(flags, ensure_ascii=False)
    if existing:
        event_id = str(existing["id"])
        if str(existing["detail_level"] or "") == "protected":
            detail = "protected"
            file_name = ""
            summary = "检测到受保护文档发生变化；未保存文件名或内容摘要。"
            flags_json = json.dumps(["document_change", "protected_content"], ensure_ascii=False)
        conn.execute(
            "UPDATE file_change_events SET last_changed_at=?, version_count=?, change_summary_redacted=?, detail_level=?, "
            "file_name_redacted=?, privacy_flags=?, updated_at=? WHERE id=?",
            (at, int(existing["version_count"] or 0) + 1, summary, detail, file_name, flags_json, at, event_id),
        )
    else:
        event_id = f"fce_{uuid.uuid4().hex}"
        conn.execute(
            "INSERT INTO file_change_events(id,file_id,root_id,project_label,file_name_redacted,file_type,change_kind,"
            "first_changed_at,last_changed_at,version_count,change_summary_redacted,detail_level,confidence,privacy_flags,deletion_group_id,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?)",
            (event_id, file_id, root_id, project, file_name, file_type, kind, at, at, summary, detail, "confirmed_file_event", flags_json, f"file_{uuid.uuid4().hex}", at, at),
        )
    row = conn.execute(
        "SELECT id,file_id,root_id,project_label,file_name_redacted,file_type,change_kind,first_changed_at,last_changed_at,"
        "version_count,change_summary_redacted,detail_level,confidence,privacy_flags,deletion_group_id "
        "FROM file_change_events WHERE id=?",
        (event_id,),
    ).fetchone()
    version_item = {
        **dict(row),
        "changed_at": at,
        "content_modified_at": str(data.get("modified_at") or ""),
        "document_path_hmac": str(data.get("document_path_hmac") or ""),
    }
    actor = _actor_for_file_version(conn, version_item)
    coverage_state = _coverage_state_for_version(conn, root_id, at)
    conn.execute(
        "INSERT INTO file_change_versions("
        "id,aggregate_event_id,file_id,root_id,project_label,document_path_hmac,file_name_redacted,file_type,change_kind,changed_at,"
        "content_modified_at,detail_level,actor_source,actor_tool,actor_confidence,actor_reason_codes,coverage_state,"
        "helper_event_id,observed_version_count,deletion_group_id,created_at,updated_at"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)",
        (
            f"fcv_{uuid.uuid4().hex}",
            event_id,
            file_id,
            root_id,
            project,
            str(data.get("document_path_hmac") or ""),
            file_name,
            file_type,
            kind,
            at,
            str(data.get("modified_at") or ""),
            detail,
            actor["source"],
            actor["tool"],
            actor["confidence"],
            json.dumps(actor["reason_codes"], ensure_ascii=False),
            coverage_state,
            str(helper_event_id or ""),
            str(row["deletion_group_id"]),
            at,
            at,
        ),
    )
    from .context_console import record_document_change_context_locked

    context_result = record_document_change_context_locked(
        conn,
        source_event_id=str(row["id"]),
        occurred_at=str(row["last_changed_at"]),
        root_id=root_id,
        project_label=str(row["project_label"] or "Tracked folder"),
        file_identity=str(row["file_id"]),
        helper_event_id=str(helper_event_id),
        content_version_identity=hashlib.sha256(
            "|".join(
                (
                    str(data.get("content_hmac") or "metadata"),
                    str(data.get("size_bytes") or 0),
                    str(data.get("modified_at") or at),
                    str(kind),
                )
            ).encode("utf-8")
        ).hexdigest(),
        version_count=int(row["version_count"] or 1),
        change_kind=str(row["change_kind"] or "modified"),
        detail_level=str(row["detail_level"] or "metadata_only"),
    )
    if context_result.get("status") not in {"accepted", "replayed", "disabled", "skipped", "completed"}:
        raise DocumentContextProjectionError(
            str(context_result.get("reason_code") or "document_context_projection_failed")
        )
    public_row = dict(row)
    direct_classification = (
        "user_modified" if actor["source"] == "user_evidence" else "unattributed"
    )
    public_row.update(
        {
            "_actor_source": actor["source"],
            "_actor_tool": actor["tool"],
            "_actor_confidence": actor["confidence"],
            "_actor_reason_codes": actor["reason_codes"],
            "actor_classification": direct_classification,
            "attribution_confidence": (
                "confirmed_human_editor_evidence"
                if direct_classification == "user_modified"
                else "unknown"
            ),
            "attribution_reason_codes": (
                actor["reason_codes"]
                if direct_classification == "user_modified"
                else ["file_level_classification_pending"]
            ),
            "attribution_tool": "",
        }
    )
    return _public_event(public_row, internal_root_ids=internal_root_ids)


def _public_event(row: dict, *, internal_root_ids: Iterable[str] = ()) -> dict:
    root_ids = {str(value) for value in internal_root_ids if value}
    if row.get("root_id"):
        root_ids.add(str(row["root_id"]))
    detail_level = str(row.get("detail_level") or "metadata_only")
    if detail_level not in {"detailed", "metadata_only", "protected"}:
        detail_level = "metadata_only"
    protected = detail_level == "protected"
    foreground_linked = bool(row.get("foreground_linked")) and not protected
    raw_project = str(row.get("project_label") or "Tracked folder")
    project = _safe_root_label(raw_project)
    raw_file_name = str(row.get("file_name_redacted") or "")
    file_name = _safe_label(
        redact_for_storage(raw_file_name, max_len=120),
        120,
    )
    raw_file_type = str(row.get("file_type") or "unknown")
    file_type = raw_file_type if raw_file_type in PUBLIC_FILE_TYPES else "unknown"
    summary = redact_for_storage(str(row.get("change_summary_redacted") or ""), max_len=360)
    if (
        _contains_internal_root_identity(project, root_ids)
        or _contains_absolute_path_signal(raw_project)
        or "\\" in raw_project
    ):
        project = "Tracked folder"
    if _contains_internal_root_identity(file_name, root_ids):
        file_name = "file_redacted"
    if "/" in raw_file_name or "\\" in raw_file_name or raw_file_name.startswith("~"):
        file_name = "file_redacted"
    if _contains_internal_root_identity(file_type, root_ids):
        file_type = "unknown"
    if _contains_internal_root_identity(summary, root_ids) or _contains_absolute_path_signal(summary):
        summary = "检测到文件变化，但摘要未能安全展示。"
    change_kind = str(row.get("change_kind") or "modified")
    if change_kind not in {"created", "modified", "renamed", "deleted"}:
        change_kind = "modified"
    # "unattributed" is an honest absence, not a claim that the owner did it:
    # the hook only covers the AI tools that have it installed, and a file
    # changed by anything else lands here too.
    attribution = row.get("_ai_attribution") or {}
    actor_classification = str(row.get("actor_classification") or "unattributed")
    if actor_classification not in {"ai_only", "user_modified", "unattributed"}:
        actor_classification = "unattributed"
    raw_attribution_tool = (
        str(row.get("attribution_tool") or attribution.get("tool") or "")
        if actor_classification == "ai_only" and not protected
        else ""
    )
    attribution_tool = _safe_label(
        redact_for_storage(raw_attribution_tool, max_len=80),
        80,
    )
    if (
        _contains_internal_root_identity(attribution_tool, root_ids)
        or _contains_absolute_path_signal(raw_attribution_tool)
        or "/" in raw_attribution_tool
        or "\\" in raw_attribution_tool
    ):
        attribution_tool = ""
    attribution_source = {
        "ai_only": "ai_tool",
        "user_modified": "user_evidence",
        "unattributed": "unattributed",
    }[actor_classification]
    default_attribution_confidence = {
        "ai_only": "confirmed_continuous_ai_only",
        "user_modified": "confirmed_human_editor_evidence",
        "unattributed": "unknown",
    }[actor_classification]
    attribution_confidence = str(
        row.get("attribution_confidence") or default_attribution_confidence
    )
    if attribution_confidence not in {
        "confirmed_continuous_ai_only",
        "confirmed_human_editor_evidence",
        "unknown",
    }:
        attribution_confidence = default_attribution_confidence
    raw_reason_codes = row.get("attribution_reason_codes") or []
    if not isinstance(raw_reason_codes, list):
        raw_reason_codes = []
    attribution_reason_codes = [
        _safe_label(str(reason), 80)
        for reason in raw_reason_codes
        if _safe_label(str(reason), 80)
    ][:12]
    first_changed_at = _safe_public_timestamp(row.get("first_changed_at"))
    last_changed_at = _safe_public_timestamp(row.get("last_changed_at"))
    if _contains_internal_root_identity(first_changed_at, root_ids):
        first_changed_at = ""
    if _contains_internal_root_identity(last_changed_at, root_ids):
        last_changed_at = ""
    return {
        "project_label": "" if protected else project,
        "file_name_redacted": "" if protected else file_name,
        "file_type": "protected" if protected else file_type,
        "change_kind": change_kind,
        "first_changed_at": first_changed_at,
        "last_changed_at": last_changed_at,
        "version_count": int(row.get("version_count") or 1),
        "change_summary_redacted": summary,
        "detail_level": detail_level,
        "confidence": "likely_user_edit" if foreground_linked else "local_file_changed",
        "foreground_linked": foreground_linked,
        # Deliberately a new field rather than a new `confidence` value: the
        # Swift contract validates confidence against a fixed set, and an
        # unknown member there blocks the entire snapshot rather than one row.
        # These two are additive, so an older app simply ignores them.
        "attribution_source": attribution_source,
        "attribution_tool": attribution_tool,
        "actor_classification": actor_classification,
        "attribution_confidence": attribution_confidence,
        "attribution_reason_codes": attribution_reason_codes,
        "projection_contract": DOCUMENT_OWNER_LOCAL_PROJECTION_VERSION,
        "projection_type": "owner_local_observation",
        "verification_state": "unverified",
        "trusted_export_eligible": False,
        "memory_eligible": False,
        "delivery_state": "not_delivered",
    }


def _baseline_snapshot_matches(row: sqlite3.Row | None, snapshot: dict) -> bool:
    if row is None or not bool(row["present"]):
        return False
    try:
        stored_privacy_flags = json.loads(str(row["privacy_flags"] or "[]"))
    except json.JSONDecodeError:
        return False
    return bool(
        str(row["file_name_redacted"] or "") == str(snapshot["file_name_redacted"] or "")
        and str(row["document_path_hmac"] or "") == str(snapshot["document_path_hmac"] or "")
        and str(row["file_type"] or "") == str(snapshot["file_type"] or "")
        and str(row["device_id"] or "") == str(snapshot["device_id"] or "")
        and str(row["inode_id"] or "") == str(snapshot["inode_id"] or "")
        and str(row["modified_at"] or "") == str(snapshot["modified_at"] or "")
        and int(row["size_bytes"] or 0) == int(snapshot["size_bytes"] or 0)
        and str(row["content_hmac"] or "") == str(snapshot["content_hmac"] or "")
        and str(row["structure_hmac_json"] or "") == str(snapshot["structure_hmac_json"] or "")
        and str(row["detail_level"] or "") == str(snapshot["detail_level"] or "")
        and stored_privacy_flags == list(snapshot["privacy_flags"])
    )


def _baseline_delete_revisions_match(
    conn: sqlite3.Connection,
    root_id: str,
    file_revisions: dict[str, str],
    *,
    batch_size: int = 500,
) -> bool:
    ordered = sorted(file_revisions)
    for offset in range(0, len(ordered), max(1, batch_size)):
        batch = ordered[offset:offset + max(1, batch_size)]
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"SELECT file_id,updated_at FROM file_watch_state "
            f"WHERE root_id=? AND file_id IN ({placeholders})",
            (root_id, *batch),
        ).fetchall()
        if any(
            str(row["updated_at"]) != file_revisions.get(str(row["file_id"]))
            for row in rows
        ):
            return False
    return True


def _delete_baseline_state_revisions(
    conn: sqlite3.Connection,
    root_id: str,
    file_revisions: dict[str, str],
    *,
    batch_size: int = 250,
) -> int:
    deleted = 0
    ordered = sorted(file_revisions.items())
    for offset in range(0, len(ordered), max(1, batch_size)):
        batch = ordered[offset:offset + max(1, batch_size)]
        predicates = " OR ".join("(file_id=? AND updated_at=?)" for _ in batch)
        parameters: list[str] = [root_id]
        for file_id, revision in batch:
            parameters.extend((file_id, revision))
        cursor = conn.execute(
            f"DELETE FROM file_watch_state WHERE root_id=? AND ({predicates})",
            parameters,
        )
        deleted += max(0, int(cursor.rowcount))
    return deleted


def _upsert_state(conn: sqlite3.Connection, root_id: str, snapshot: dict, at: str) -> None:
    conn.execute(
        "INSERT INTO file_watch_state(file_id,root_id,project_label,document_path_hmac,file_name_redacted,file_type,device_id,inode_id,modified_at,size_bytes,"
        "content_hmac,structure_hmac_json,detail_level,privacy_flags,present,first_seen_at,last_seen_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?) ON CONFLICT(file_id) DO UPDATE SET "
        "project_label=excluded.project_label,document_path_hmac=excluded.document_path_hmac,file_name_redacted=excluded.file_name_redacted,file_type=excluded.file_type,device_id=excluded.device_id,"
        "inode_id=excluded.inode_id,modified_at=excluded.modified_at,size_bytes=excluded.size_bytes,content_hmac=excluded.content_hmac,"
        "structure_hmac_json=excluded.structure_hmac_json,detail_level=excluded.detail_level,privacy_flags=excluded.privacy_flags,"
        "present=1,last_seen_at=excluded.last_seen_at,updated_at=excluded.updated_at",
        (snapshot["file_id"], root_id, snapshot.get("project_label") or _root_label(conn, root_id), snapshot.get("document_path_hmac") or "", snapshot["file_name_redacted"], snapshot["file_type"], snapshot["device_id"], snapshot["inode_id"], snapshot["modified_at"], snapshot["size_bytes"], snapshot["content_hmac"], snapshot["structure_hmac_json"], snapshot["detail_level"], json.dumps(snapshot["privacy_flags"], ensure_ascii=False), at, at, at),
    )


def _checkpoint(conn: sqlite3.Connection, root_id: str, event_id: str, at: str) -> None:
    numeric_event_id = str(event_id) if str(event_id).isdigit() else None
    previous_row = conn.execute(
        "SELECT last_event_id FROM file_watch_checkpoints WHERE root_id=?",
        (root_id,),
    ).fetchone()
    previous = str(previous_row["last_event_id"] or "") if previous_row else ""
    high_watermark = previous if previous.isdigit() else None
    if numeric_event_id is not None and (
        high_watermark is None or int(numeric_event_id) >= int(high_watermark)
    ):
        high_watermark = numeric_event_id
    conn.execute(
        "INSERT INTO file_watch_checkpoints(root_id,last_event_id,coverage_status,last_event_at,updated_at) VALUES(?,?,'complete',?,?) "
        "ON CONFLICT(root_id) DO UPDATE SET last_event_id=COALESCE(excluded.last_event_id,file_watch_checkpoints.last_event_id),"
        "last_event_at=CASE "
        "WHEN COALESCE(file_watch_checkpoints.last_event_at,'')='' OR excluded.last_event_at>file_watch_checkpoints.last_event_at "
        "THEN excluded.last_event_at ELSE file_watch_checkpoints.last_event_at END,"
        "coverage_status=CASE WHEN file_watch_checkpoints.coverage_status='partial' THEN 'partial' ELSE 'complete' END,"
        "updated_at=excluded.updated_at",
        (root_id, high_watermark, at, at),
    )


STREAM_RESET_MARKER_FILENAME = ".document_stream_reset_required"


def _stream_reset_marker_path(runtime_root: str | Path) -> Path:
    return Path(runtime_root) / STREAM_RESET_MARKER_FILENAME


def mark_document_stream_reset_required(runtime_root: str | Path, reason_code: str) -> None:
    """Persist, outside SQLite, that the stream watermarks must be cleared.

    The lost-stream reset itself needs the SQLite write lock and can fail with
    ``database_busy``.  Startup reconcile deliberately preserves watermarks, so
    without this marker a daemon restart in that window would resume the next
    helper from the invalid ``--since`` cursor.  Content is a fixed reason code.
    """
    code = (
        reason_code
        if reason_code in DOCUMENT_CHANGE_FATAL_REASON_CODES
        else "unknown_fatal_transition"
    )
    descriptor = os.open(
        _stream_reset_marker_path(runtime_root),
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.write(descriptor, code.encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def document_stream_reset_required_reason(runtime_root: str | Path) -> str | None:
    """Return the pending reset reason, or None when no reset is required."""
    path = _stream_reset_marker_path(runtime_root)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError:
        # Unreadable or replaced by a symlink: fail closed and reset anyway.
        return "unknown_fatal_transition"
    try:
        raw = os.read(descriptor, 128).decode("ascii", "replace").strip()
    finally:
        os.close(descriptor)
    return raw if raw in DOCUMENT_CHANGE_FATAL_REASON_CODES else "unknown_fatal_transition"


def clear_document_stream_reset_required(runtime_root: str | Path) -> None:
    _stream_reset_marker_path(runtime_root).unlink(missing_ok=True)


def _retained_stream_watermark_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM file_watch_checkpoints AS checkpoints "
        "JOIN file_watch_roots AS roots ON roots.root_id=checkpoints.root_id "
        "WHERE roots.enabled=1 AND checkpoints.last_event_id IS NOT NULL"
    ).fetchone()
    return int(row[0] or 0)


def reset_document_change_stream_watermarks(
    db_path: str | Path,
    *,
    reason_code: str,
    busy_timeout_seconds: float | None = None,
) -> bool:
    """Retry a lost-stream reset after its generation has fully exited.

    The in-worker reset in ``stop_for_fresh_stream`` can lose its SQLite write
    lock (``database_busy``).  Its whole pipeline has joined by the time the
    daemon calls this, so no writer of that generation can resurrect a stream
    ID.  The transaction is the same fail-closed transition: every enabled
    watermark becomes NULL and every enabled root partial.  It is deliberately
    not bound to the old authorization epoch -- clearing a stream ID only ever
    makes coverage more conservative.  Returns True only when the visible
    postcondition (no enabled watermark retained) is re-read and holds.
    """
    from .storage import connect, connect_readonly

    database = Path(db_path).resolve()
    timeout = {} if busy_timeout_seconds is None else {"busy_timeout_seconds": busy_timeout_seconds}
    conn = connect(database, **timeout)
    try:
        _acquire_document_change_write_lock(conn)
        row = conn.execute(
            "SELECT root_id FROM file_watch_roots WHERE enabled=1 ORDER BY root_id LIMIT 1"
        ).fetchone()
        if row is not None:
            _record_gap(
                conn,
                str(row["root_id"]),
                _now(),
                "",
                reset_event_id=True,
                downgrade_baseline=True,
                affects_attribution=True,
                reason_code=(
                    reason_code
                    if reason_code in DOCUMENT_CHANGE_FATAL_REASON_CODES
                    else "unknown_fatal_transition"
                ),
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
    verify_conn = connect_readonly(database, **timeout)
    try:
        verified = _retained_stream_watermark_count(verify_conn) == 0
    finally:
        verify_conn.close()
    if verified:
        clear_document_stream_reset_required(database.parent)
    return verified


def _record_gap(
    conn: sqlite3.Connection,
    root_id: str,
    at: str,
    event_id: str,
    *,
    reset_event_id: bool = False,
    downgrade_baseline: bool = True,
    affects_attribution: bool = True,
    reason_code: str = "coverage_gap",
) -> None:
    """Record a coverage gap for one root.

    ``downgrade_baseline`` separates two questions that used to share one
    column.  ``baseline_status`` answers "has this root been fully enumerated
    and fingerprinted"; a directory rename during normal use does not undo that
    enumeration, it makes some *paths* uncertain.  That uncertainty is exactly
    what ``coverage_status`` / ``event_gap_count`` / ``coverage_started_at``
    exist to express, and they are always written -- so ``is_complete`` still
    refuses to claim complete coverage.  Only a gap that invalidates the
    enumeration itself passes ``downgrade_baseline=True``, which stays the
    default so every existing caller keeps its current behaviour.
    """

    safe_reason = _safe_label(str(reason_code or "coverage_gap"), 80) or "coverage_gap"
    conn.execute(
        "INSERT INTO file_watch_coverage_gaps("
        "id,root_id,observed_at,reason_code,affects_attribution,helper_event_id,created_at"
        ") VALUES(?,?,?,?,?,?,?)",
        (
            f"fcg_{uuid.uuid4().hex}",
            root_id,
            at,
            safe_reason,
            1 if affects_attribution else 0,
            str(event_id or ""),
            at,
        ),
    )
    if not affects_attribution:
        _checkpoint(conn, root_id, event_id, at)
        conn.execute(
            "UPDATE file_watch_checkpoints SET benign_event_count=benign_event_count+1,updated_at=? "
            "WHERE root_id=?",
            (at, root_id),
        )
        return

    if reset_event_id:
        # FSEvents explicitly reports that its UInt64 sequence wrapped.  Keeping
        # the old high watermark would classify every new low ID as a replay,
        # even after a successful metadata reconcile.
        conn.execute(
            "INSERT INTO file_watch_checkpoints(root_id,last_event_id,coverage_status,event_gap_count,coverage_started_at,updated_at) "
            "SELECT root_id,NULL,'partial',1,NULL,? FROM file_watch_roots WHERE enabled=1 "
            "ON CONFLICT(root_id) DO UPDATE SET last_event_id=NULL,coverage_status='partial',"
            "event_gap_count=file_watch_checkpoints.event_gap_count+1,coverage_started_at=NULL,"
            "updated_at=excluded.updated_at",
            (at,),
        )
        conn.execute(
            "UPDATE file_watch_roots SET baseline_status='partial',updated_at=? WHERE enabled=1",
            (at,),
        )
        # A dropped/wrapped stream has no trustworthy successor watermark.
        # Do not retain the control event's ID (or a helper-ready ID); the
        # replacement generation must start without ``--since`` and rebuild a
        # new baseline before it advances coverage again.
    else:
        _checkpoint(conn, root_id, event_id, at)
        conn.execute(
            "UPDATE file_watch_checkpoints SET coverage_status='partial',event_gap_count=event_gap_count+1,"
            "coverage_started_at=NULL,updated_at=? "
            "WHERE root_id=?",
            (at, root_id),
        )
    if downgrade_baseline:
        conn.execute("UPDATE file_watch_roots SET baseline_status='partial',updated_at=? WHERE root_id=?", (at, root_id))


def _event_id_is_replay(conn: sqlite3.Connection, root_id: str, event_id: str) -> bool:
    if not event_id.isdigit():
        return False
    row = conn.execute("SELECT last_event_id FROM file_watch_checkpoints WHERE root_id=?", (root_id,)).fetchone()
    if row is None:
        return False
    previous = str(row["last_event_id"] or "")
    # FSEvents can associate one event ID with more than one file path.  Equal
    # IDs must still be drained; content/inode comparison makes those replays
    # idempotent.  Only strictly older IDs are unambiguously stale.
    return previous.isdigit() and int(event_id) < int(previous)


def _ensure_root_row(conn: sqlite3.Connection, root_id: str, label: str, at: str) -> None:
    conn.execute(
        "INSERT INTO file_watch_roots(root_id,root_label,enabled,baseline_status,created_at,updated_at) VALUES(?,?,1,'pending',?,?) "
        "ON CONFLICT(root_id) DO UPDATE SET enabled=1,updated_at=excluded.updated_at",
        (root_id, _safe_root_label(label), at, at),
    )


def _iter_safe_files(root: Path, *, stop_check=None) -> Iterator[Path]:
    repo_candidates: dict[Path, list[Path]] = {}
    def walk_error(error: OSError) -> None:
        raise OSError("document baseline traversal failed") from error

    for current, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        if stop_check is not None and bool(stop_check()):
            return
        dirs[:] = [name for name in dirs if not name.startswith(".") and name not in IGNORED_DIRS]
        base = Path(current)
        for name in files:
            if stop_check is not None and bool(stop_check()):
                return
            path = base / name
            if not _safe_path(root, path, check_git_ignore=False):
                continue
            repo = _git_repo_for_path(root, path)
            if repo is None:
                yield path
            else:
                repo_candidates.setdefault(repo, []).append(path)
    # One NUL-delimited git invocation per repository is materially cheaper
    # than spawning `git check-ignore` for every source file in a large root.
    for repo, candidates in repo_candidates.items():
        if stop_check is not None and bool(stop_check()):
            return
        for offset in range(0, len(candidates), 2000):
            batch = candidates[offset:offset + 2000]
            proofs = _BASELINE_IGNORE_PROOFS.get()
            prepared = proofs.prepare(repo, batch) if proofs is not None else None
            ignored = _git_ignored_batch(repo, batch, stop_check=stop_check)
            if stop_check is not None and bool(stop_check()):
                return
            if ignored is None:
                raise GitIgnoreEvaluationError("git ignore evaluation unavailable")
            if proofs is not None:
                proofs.remember(prepared, ignored)
            for path in batch:
                if stop_check is not None and bool(stop_check()):
                    return
                if path not in ignored:
                    yield path


def _safe_path(root: Path, path: Path, *, allow_missing: bool = False, check_git_ignore: bool = True) -> bool:
    try:
        resolved_root = root.resolve(strict=True)
        resolved = path.resolve(strict=not allow_missing)
    except OSError:
        return False
    try:
        resolved.relative_to(resolved_root)
    except ValueError:
        return False
    relative = resolved.relative_to(resolved_root)
    if any(part.startswith(".") or part in IGNORED_DIRS for part in relative.parts):
        return False
    if TEMP_NAME_RE.search(path.name) or path.suffix.lower() not in ALLOWED_SUFFIXES:
        return False
    if path.is_symlink():
        return False
    if check_git_ignore and path.exists() and _git_ignored(root, path):
        return False
    return allow_missing or path.is_file()


def _definitely_ignored_event(
    root: Path,
    path: Path,
    *,
    is_directory: bool = False,
    is_symlink: bool = False,
) -> bool:
    """Return True only when an event is provably outside the tracked set.

    This check runs before the startup-baseline dirty decision, so dependency,
    cache and editor-temp churn cannot keep coverage permanently partial.  An
    ambiguous path remains relevant and therefore fails closed.
    """
    try:
        resolved_root = root.resolve(strict=True)
        candidate = path.expanduser()
        if not candidate.is_absolute():
            candidate = resolved_root / candidate
        lexical_root = _normalize_fixed_system_alias(resolved_root)
        lexical_candidate = _normalize_fixed_system_alias(candidate)
        lexical_relative = lexical_candidate.relative_to(lexical_root)
    except (OSError, ValueError):
        return False
    if is_symlink:
        return True
    current = resolved_root
    for component in lexical_relative.parts[:-1]:
        current = current / component
        try:
            if current.is_symlink():
                return True
            if not current.exists():
                break
        except OSError:
            return False
    if candidate.is_symlink():
        return True
    if any(part.startswith(".") or part in IGNORED_DIRS for part in lexical_relative.parts):
        return True
    if TEMP_NAME_RE.search(candidate.name):
        return True
    if not is_directory and candidate.suffix.lower() not in ALLOWED_SUFFIXES:
        return True
    try:
        candidate.resolve(strict=False).relative_to(resolved_root)
    except (OSError, ValueError):
        return False
    repo = _git_repo_for_path(resolved_root, candidate)
    if repo is None:
        return False
    ignored = _git_ignored_batch(repo, [candidate])
    if ignored is None:
        raise GitIgnoreEvaluationError("git ignore evaluation unavailable")
    return candidate in ignored


def _is_ignore_control(path: Path) -> bool:
    normalized = "/".join(path.parts)
    return path.name in {".git", ".gitignore"} or normalized.endswith("/.git/info/exclude")


def _ignore_control_has_skipped_parent(root: Path, path: Path) -> bool:
    try:
        relative = _normalize_fixed_system_alias(path).relative_to(
            _normalize_fixed_system_alias(root)
        )
    except ValueError:
        return False
    parts = list(relative.parts)
    if parts[-3:] == [".git", "info", "exclude"]:
        parents = parts[:-3]
    else:
        parents = parts[:-1]
    return any(part.startswith(".") or part in IGNORED_DIRS for part in parents)


def mark_document_root_reconciled(conn: sqlite3.Connection, root_id: str) -> None:
    now = _now()
    conn.execute(
        "UPDATE file_watch_checkpoints SET coverage_status='complete',event_gap_count=0,"
        "coverage_started_at=?,updated_at=? WHERE root_id=?",
        (now, now, root_id),
    )
    conn.execute("UPDATE file_watch_roots SET baseline_status='complete',updated_at=? WHERE root_id=?", (now, root_id))
    conn.commit()


def _resolve_root(value: str | Path) -> Path:
    lexical = Path(value).expanduser()
    if not lexical.is_absolute():
        lexical = Path.cwd() / lexical
    # Resolve only after checking every lexical component.  Otherwise the final
    # is_symlink() test observes the target and silently expands user consent.
    current = Path(lexical.anchor)
    for component in lexical.parts[1:]:
        current = current / component
        try:
            # macOS exposes /var, /tmp, and /etc as fixed system aliases into
            # /private.  They are not user-selected consent indirections.
            system_alias = str(current) in {"/var", "/tmp", "/etc"}
            if current.is_symlink() and not system_alias:
                raise ValueError("document watch root cannot traverse a symlink")
        except OSError as exc:
            raise ValueError("document watch root must be inspectable") from exc
    path = lexical.resolve(strict=True)
    if not path.is_dir():
        raise ValueError("document watch root must be a real directory")
    return path


def _root_id(conn: sqlite3.Connection, path: Path) -> str:
    return "root_" + _hmac(_hmac_key(conn), str(path.resolve()))[:32]


def _file_id(conn: sqlite3.Connection, root: Path, path: Path) -> str:
    relative = _lexical_relative_path(root, path)
    return _file_id_for_relative(conn, root, relative)


def _file_id_for_relative(conn: sqlite3.Connection, root: Path, relative: str) -> str:
    return "file_" + _hmac(_hmac_key(conn), f"{_root_id(conn, root)}:{relative}")[:40]


def _lexical_relative_path(root: Path, path: Path) -> str:
    root_absolute = _normalize_fixed_system_alias(root)
    path_absolute = _normalize_fixed_system_alias(path)
    try:
        relative = path_absolute.relative_to(root_absolute)
    except ValueError as exc:
        raise OSError("document path is outside the watched root") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise OSError("invalid document path")
    return relative.as_posix()


def _normalize_fixed_system_alias(path: Path) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path)))
    parts = absolute.parts
    if len(parts) >= 2 and parts[1] in {"var", "tmp", "etc"}:
        return Path("/private").joinpath(*parts[1:])
    return absolute


def _open_file_beneath(root: Path, path: Path) -> tuple[int, str]:
    """Open a regular file without following any path-component symlink."""
    relative = _lexical_relative_path(root, path)
    parts = Path(relative).parts
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory_flag is None:
        raise OSError("safe no-follow open is unavailable")
    common_flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
    current = os.open(os.fspath(root), common_flags | directory_flag)
    try:
        for component in parts[:-1]:
            next_descriptor = os.open(
                component,
                common_flags | directory_flag,
                dir_fd=current,
            )
            os.close(current)
            current = next_descriptor
        descriptor = os.open(parts[-1], common_flags, dir_fd=current)
    finally:
        os.close(current)
    try:
        opened = os.fstat(descriptor)
    except Exception:
        os.close(descriptor)
        raise
    if not stat_module.S_ISREG(opened.st_mode):
        os.close(descriptor)
        raise OSError("document path is not a regular file")
    return descriptor, relative


def _safe_file_stat(root: Path, path: Path) -> os.stat_result:
    descriptor, _ = _open_file_beneath(root, path)
    try:
        return os.fstat(descriptor)
    finally:
        os.close(descriptor)


def _read_open_file(descriptor: int, *, max_bytes: int, content_allowed_check=None) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    total = 0
    while True:
        if content_allowed_check is not None and not bool(content_allowed_check()):
            raise PermissionError("content read deferred while reading")
        chunk = _read_document_chunk(descriptor, 128 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise OSError("document grew beyond the extraction cap")
        chunks.append(chunk)
    return b"".join(chunks)


def _hmac_key(conn: sqlite3.Connection) -> bytes:
    global _EPHEMERAL_HMAC_KEY

    database_path = _connection_database_path(conn)
    if database_path is None:
        with _EPHEMERAL_HMAC_KEY_LOCK:
            if _EPHEMERAL_HMAC_KEY is None:
                _EPHEMERAL_HMAC_KEY = secrets.token_bytes(32)
            return _EPHEMERAL_HMAC_KEY

    secret_path = database_path.parent / HMAC_SECRET_FILENAME
    lock_path = database_path.parent / HMAC_SECRET_LOCK_FILENAME
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret_path.parent.chmod(0o700)
    lock_flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    lock_descriptor = os.open(lock_path, lock_flags, 0o600)
    try:
        lock_metadata = os.fstat(lock_descriptor)
        if not stat_module.S_ISREG(lock_metadata.st_mode) or lock_metadata.st_uid != os.getuid():
            raise RuntimeError("document watch secret lock is unsafe")
        os.fchmod(lock_descriptor, 0o600)
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        legacy_present, legacy = _legacy_hmac_key(conn)
        stored_verifier = _stored_hmac_verifier(conn)
        identity_exists = _document_identity_exists(conn)
        try:
            value = _read_private_hmac_secret(secret_path)
            if legacy is not None and legacy != value:
                raise RuntimeError("document watch secret mismatch")
        except FileNotFoundError:
            if legacy is None and (stored_verifier is not None or identity_exists):
                raise RuntimeError("document watch secret missing")
            value = legacy or secrets.token_bytes(32)
            _write_private_hmac_secret(secret_path, value)
        expected_verifier = _hmac_verifier(value)
        if stored_verifier is not None:
            if not hmac.compare_digest(stored_verifier, expected_verifier):
                raise RuntimeError("document watch secret verifier mismatch")
        else:
            if identity_exists and legacy is None:
                raise RuntimeError("document watch secret verifier missing")
            conn.execute(
                "INSERT INTO daemon_state(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (HMAC_VERIFIER_STATE_KEY, expected_verifier, _now()),
            )
        if legacy_present:
            conn.execute("DELETE FROM daemon_state WHERE key='document_change_hmac_key'")
        return value
    finally:
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)


def _existing_hmac_key_readonly(conn: sqlite3.Connection) -> bytes:
    """Read and verify existing key material without migrations or chmods."""
    database_path = _connection_database_path(conn)
    if database_path is None:
        with _EPHEMERAL_HMAC_KEY_LOCK:
            if _EPHEMERAL_HMAC_KEY is None:
                raise RuntimeError("document watch secret is unavailable")
            return _EPHEMERAL_HMAC_KEY
    value = _read_private_hmac_secret_readonly(database_path.parent / HMAC_SECRET_FILENAME)
    stored_verifier = _stored_hmac_verifier(conn)
    if stored_verifier is None or not hmac.compare_digest(stored_verifier, _hmac_verifier(value)):
        raise RuntimeError("document watch secret verifier mismatch")
    legacy_present, legacy = _legacy_hmac_key(conn)
    if legacy_present:
        if legacy is None or not hmac.compare_digest(legacy, value):
            raise RuntimeError("document watch secret mismatch")
        raise RuntimeError("document watch secret migration is pending")
    return value


def _connection_database_path(conn: sqlite3.Connection) -> Path | None:
    row = conn.execute("PRAGMA database_list").fetchone()
    raw = str(row[2] or "") if row is not None and len(row) > 2 else ""
    return Path(raw).resolve() if raw else None


def _authorization_lock_is_valid_readonly(conn: sqlite3.Connection) -> bool:
    database_path = _connection_database_path(conn)
    if database_path is None:
        return True
    lock_path = database_path.parent / AUTHORIZATION_LOCK_FILENAME
    try:
        descriptor = os.open(
            lock_path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
    except FileNotFoundError:
        return not any(
            conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
            for table in ("file_watch_roots", "file_change_events")
        )
    except OSError:
        return False
    try:
        metadata = os.fstat(descriptor)
        return bool(
            stat_module.S_ISREG(metadata.st_mode)
            and metadata.st_uid == os.getuid()
            and metadata.st_mode & 0o777 == 0o600
        )
    finally:
        os.close(descriptor)


def _acquire_authorization_flock(
    descriptor: int,
    *,
    exclusive: bool,
    timeout: float | None,
) -> None:
    """Take the authorization flock, optionally bounding how long a reader waits.

    Exclusive writers always block.  They own a destructive transaction boundary
    and must not abandon a cascade halfway just because a reader is queued.

    A shared reader may pass ``timeout`` when its caller can retry safely.  The
    interactive card surfaces do: a maintenance lease can hold the exclusive
    lock for seconds, and an unbounded reader would freeze the UI for exactly
    that long.  Timing out raises the same ``database is locked`` error the CLI
    already maps to ``database_busy``, so callers keep one retry path.
    """
    if exclusive or timeout is None:
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        return
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                raise
            if time.monotonic() >= deadline:
                raise sqlite3.OperationalError(
                    "database is locked: authorization lock is held by maintenance"
                ) from None
            time.sleep(AUTHORIZATION_LOCK_RETRY_SECONDS)


@contextmanager
def document_authorization_lock(
    conn: sqlite3.Connection,
    *,
    exclusive: bool,
    timeout: float | None = None,
    initialize_if_missing: bool = False,
) -> Iterator[None]:
    """Serialize public reads against revocation and destructive deletion."""
    database_path = _connection_database_path(conn)
    if database_path is None:
        with _EPHEMERAL_AUTHORIZATION_LOCK:
            yield
        return
    lock_path = database_path.parent / AUTHORIZATION_LOCK_FILENAME
    if exclusive or initialize_if_missing:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    else:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except FileNotFoundError:
        if exclusive:
            raise RuntimeError("document authorization lock is unavailable") from None
        has_private_rows = any(
            conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
            for table in ("file_watch_roots", "file_change_events")
        )
        if has_private_rows:
            raise RuntimeError("document authorization lock is missing")
        yield
        return
    except OSError:
        raise RuntimeError("document authorization lock is unavailable") from None
    try:
        metadata = os.fstat(descriptor)
        if not stat_module.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise RuntimeError("document authorization lock is unsafe")
        if exclusive or initialize_if_missing:
            os.fchmod(descriptor, 0o600)
        elif metadata.st_mode & 0o777 != 0o600:
            raise RuntimeError("document authorization lock permissions are unsafe")
        _acquire_authorization_flock(
            descriptor,
            exclusive=exclusive,
            timeout=timeout,
        )
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _fresh_document_authorization(conn: sqlite3.Connection) -> tuple[int | None, set[str]]:
    """Recheck revocation state outside any stale reader snapshot."""
    database_path = _connection_database_path(conn)
    if database_path is None:
        return _document_change_epoch(conn), {
            str(row["root_id"])
            for row in conn.execute("SELECT root_id FROM file_watch_roots").fetchall()
        }
    fresh: sqlite3.Connection | None = None
    try:
        fresh = sqlite3.connect(
            f"file:{database_path}?mode=ro",
            uri=True,
            timeout=5,
        )
        fresh.row_factory = sqlite3.Row
        fresh.execute("PRAGMA query_only = ON")
        return _document_change_epoch(fresh), {
            str(row["root_id"])
            for row in fresh.execute("SELECT root_id FROM file_watch_roots").fetchall()
        }
    except (OSError, sqlite3.DatabaseError):
        return None, set()
    finally:
        if fresh is not None:
            fresh.close()


def _legacy_hmac_key(conn: sqlite3.Connection) -> tuple[bool, bytes | None]:
    row = conn.execute("SELECT value FROM daemon_state WHERE key='document_change_hmac_key'").fetchone()
    if row is None:
        return False, None
    try:
        value = bytes.fromhex(str(row["value"]))
    except ValueError:
        raise RuntimeError("legacy document watch secret is invalid") from None
    if len(value) != 32:
        raise RuntimeError("legacy document watch secret is invalid")
    return True, value


def _stored_hmac_verifier(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT value FROM daemon_state WHERE key=?",
        (HMAC_VERIFIER_STATE_KEY,),
    ).fetchone()
    return str(row["value"]) if row is not None and row["value"] else None


def _document_identity_exists(conn: sqlite3.Connection) -> bool:
    for table in ("file_watch_roots", "file_watch_checkpoints", "file_watch_state", "file_change_events"):
        if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None:
            return True
    return False


def _hmac_verifier(value: bytes) -> str:
    return "v1:" + hmac.new(value, HMAC_VERIFIER_DOMAIN.encode("ascii"), hashlib.sha256).hexdigest()


def _read_private_hmac_secret(path: Path) -> bytes:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat_module.S_ISREG(metadata.st_mode):
            raise RuntimeError("document watch secret is not a regular file")
        os.fchmod(descriptor, 0o600)
        value = os.read(descriptor, 33)
    finally:
        os.close(descriptor)
    if len(value) != 32:
        raise RuntimeError("document watch secret has an invalid length")
    return value


def _read_private_hmac_secret_readonly(path: Path) -> bytes:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat_module.S_ISREG(metadata.st_mode):
            raise RuntimeError("document watch secret is not a regular file")
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o777 != 0o600:
            raise RuntimeError("document watch secret permissions are unsafe")
        value = os.read(descriptor, 33)
    finally:
        os.close(descriptor)
    if len(value) != 32:
        raise RuntimeError("document watch secret has an invalid length")
    return value


def _write_private_hmac_secret(path: Path, value: bytes) -> None:
    if len(value) != 32:
        raise RuntimeError("document watch secret has an invalid length")
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(8)}.tmp"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        written = 0
        while written < len(value):
            written += os.write(descriptor, value[written:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        temporary.replace(path)
        path.chmod(0o600)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        directory_descriptor = os.open(path.parent, directory_flags)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _hmac(secret: bytes, value: str) -> str:
    return hmac.new(secret, value.encode("utf-8", errors="replace"), hashlib.sha256).hexdigest()


def _file_content_hmac(secret: bytes, descriptor: int, size: int, *, content_allowed_check=None) -> str:
    digest = hmac.new(secret, digestmod=hashlib.sha256)
    digest.update(f"size:{size}:".encode("ascii"))
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        if content_allowed_check is not None and not bool(content_allowed_check()):
            raise PermissionError("content read deferred while hashing")
        chunk = _read_document_chunk(descriptor, 128 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def _document_change_epoch(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM daemon_state WHERE key='document_change_authorization_epoch'"
    ).fetchone()
    try:
        return max(0, int(str(row["value"]))) if row is not None else 0
    except (TypeError, ValueError):
        return 0


def _root_authorization_revision(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT value FROM daemon_state WHERE key=?",
        (ROOT_AUTHORIZATION_REVISION_KEY,),
    ).fetchone()
    if row is None:
        return 0
    try:
        value = int(str(row["value"]))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _bump_root_authorization_revision(conn: sqlite3.Connection) -> tuple[int, int]:
    previous = _root_authorization_revision(conn)
    if previous is None:
        raise RuntimeError("document watch authorization revision is invalid")
    current = previous + 1
    conn.execute(
        "INSERT INTO daemon_state(key,value,updated_at) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
        (ROOT_AUTHORIZATION_REVISION_KEY, str(current), _now()),
    )
    return previous, current


def _acquire_document_change_write_lock(conn: sqlite3.Connection) -> None:
    """Serialize daemon-state read/modify/write operations."""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    else:
        conn.execute(
            "UPDATE daemon_state SET updated_at=updated_at "
            "WHERE key='document_change_authorization_epoch'"
        )


def bump_document_change_epoch(conn: sqlite3.Connection) -> int:
    """Invalidate in-flight document reads/writes inside the caller transaction."""
    _acquire_document_change_write_lock(conn)
    value = _document_change_epoch(conn) + 1
    conn.execute(
        "INSERT INTO daemon_state(key,value,updated_at) VALUES('document_change_authorization_epoch',?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
        (str(value), _now()),
    )
    return value


def _resume_pending(conn) -> bool:
    row = conn.execute("SELECT value FROM daemon_state WHERE key='document_resume_pending'").fetchone()
    return bool(row and row[0] == "true")


def _set_resume_pending(conn, pending) -> None:
    conn.execute("INSERT INTO daemon_state(key,value,updated_at) VALUES('document_resume_pending',?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                 ("true" if pending else "false", _now()))


def _document_change_pending_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM daemon_state WHERE key='document_change_pending_events'"
    ).fetchone()
    try:
        return max(0, int(str(row["value"]))) if row is not None else 0
    except (TypeError, ValueError):
        # A malformed pending marker must fail closed.
        return 1


def _adjust_document_change_pending(
    conn: sqlite3.Connection,
    delta: int,
    *,
    expected_epoch: int | None = None,
) -> int | None:
    _acquire_document_change_write_lock(conn)
    if expected_epoch is not None and _document_change_epoch(conn) != int(expected_epoch):
        return None
    value = max(0, _document_change_pending_count(conn) + int(delta))
    conn.execute(
        "INSERT INTO daemon_state(key,value,updated_at) VALUES('document_change_pending_events',?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
        (str(value), _now()),
    )
    return value


def _set_document_change_pending(conn: sqlite3.Connection, value: int) -> None:
    conn.execute(
        "INSERT INTO daemon_state(key,value,updated_at) VALUES('document_change_pending_events',?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
        (str(max(0, int(value))), _now()),
    )


def _begin_document_change_write(
    conn: sqlite3.Connection,
    root_id: str,
    expected_epoch: int,
) -> str:
    """Acquire the write lock and atomically recheck authorization + epoch.

    Returning an empty string leaves an IMMEDIATE transaction open for the
    caller's state/event/checkpoint writes.  A non-empty reason has already
    rolled the transaction back.
    """
    _acquire_document_change_write_lock(conn)
    rejection = _document_change_authorization_rejection(
        conn,
        root_id,
        expected_epoch,
    )
    if rejection:
        conn.rollback()
    return rejection


def _document_change_authorization_rejection(
    conn: sqlite3.Connection,
    root_id: str,
    expected_epoch: int,
) -> str:
    """Return the current document-write rejection without changing a transaction.

    Callers that will persist state must invoke this only after acquiring the
    exact write gate above.  The baseline also uses it inside a short deferred
    read transaction after every filesystem snapshot: an unchanged candidate
    has nothing to persist, but it must still notice an epoch/revocation change
    without relying on the three-second content-read cache.
    """
    if _document_change_epoch(conn) != int(expected_epoch):
        return "authorization_epoch_changed"
    if not _setting_bool(conn, "document_change_tracking_enabled", True):
        return "tracking_disabled"
    if _daemon_state_bool(conn, "pause_mode"):
        return "paused"
    row = conn.execute(
        "SELECT enabled FROM file_watch_roots WHERE root_id=?",
        (root_id,),
    ).fetchone()
    if row is None or not bool(row["enabled"]):
        return "root_not_configured"
    return ""


def _document_change_authorized_readonly(
    database: str | Path,
    root_id: str,
    expected_epoch: int | None,
) -> bool:
    """Read current content authorization without joining SQLite's writer queue.

    Destructive document operations take the authorization flock exclusively
    before opening their SQLite transaction.  This shared lease therefore waits
    for an in-flight revoke/reset/pause to commit, while unrelated capture
    writers no longer turn a read-only content checkpoint into ``BEGIN
    IMMEDIATE`` contention.  The connection is opened before the flock but does
    not establish a SQLite snapshot until the SELECTs below run under the lease.
    """
    from .storage import connect_readonly

    authorization_conn = connect_readonly(database)
    try:
        with document_authorization_lock(authorization_conn, exclusive=False):
            if expected_epoch is None:
                row = authorization_conn.execute(
                    "SELECT enabled FROM file_watch_roots WHERE root_id=?",
                    (root_id,),
                ).fetchone()
                return bool(
                    _setting_bool(
                        authorization_conn,
                        "document_change_tracking_enabled",
                        True,
                    )
                    and not _daemon_state_bool(authorization_conn, "pause_mode")
                    and row is not None
                    and bool(row["enabled"])
                )
            return not bool(
                _document_change_authorization_rejection(
                    authorization_conn,
                    root_id,
                    int(expected_epoch),
                )
            )
    finally:
        authorization_conn.close()


def _setting_bool(conn: sqlite3.Connection, key: str, default: bool) -> bool:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    return str(row["value"]).lower() in {"1", "true", "yes", "on"}


def _daemon_state_bool(conn: sqlite3.Connection, key: str) -> bool:
    row = conn.execute("SELECT value FROM daemon_state WHERE key=?", (key,)).fetchone()
    if row is None:
        return False
    value = str(row["value"] or "").strip().lower()
    if key == "pause_mode":
        return value in {"manual", "until_lock", "today", "duration"}
    return value in {"1", "true", "yes", "on"}


def _set_setting(conn: sqlite3.Connection, key: str, value: str, at: str) -> None:
    conn.execute(
        "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
        (key, value, at),
    )


def _state_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def _root_label(conn: sqlite3.Connection, root_id: str) -> str:
    row = conn.execute("SELECT root_label FROM file_watch_roots WHERE root_id=?", (root_id,)).fetchone()
    return str(row["root_label"] or "Tracked folder") if row else "Tracked folder"


def _project_label_for_path(conn: sqlite3.Connection, root: Path, path: Path) -> str:
    """Resolve a safe, human project label without persisting the raw path."""

    root_id = _root_id(conn, root)
    registered = conn.execute(
        "SELECT labels.label FROM context_document_mappings AS mappings "
        "JOIN context_label_registry AS labels ON labels.ref=mappings.project_ref "
        "WHERE mappings.root_id=? AND mappings.status='active' "
        "AND labels.kind='project' AND labels.status='active' "
        "ORDER BY mappings.mapping_version DESC LIMIT 1",
        (root_id,),
    ).fetchone()
    if registered and str(registered["label"] or "").strip():
        return _safe_root_label(str(registered["label"]))

    repo = _git_repo_for_path(root, path)
    if repo is not None and repo.name:
        return _safe_root_label(repo.name)

    relative = ""
    try:
        relative = _lexical_relative_path(root, path)
    except OSError:
        pass
    parts = Path(relative).parts if relative else ()
    root_label = _safe_root_label(_root_label(conn, root_id))
    if "obsidian" in root_label.casefold() and len(parts) > 1:
        return _safe_root_label(parts[0])

    mapping_row = conn.execute(
        "SELECT value FROM settings WHERE key='document_change_project_mappings'"
    ).fetchone()
    try:
        configured = json.loads(str(mapping_row["value"] or "[]")) if mapping_row else []
    except (TypeError, json.JSONDecodeError):
        configured = []
    if isinstance(configured, list):
        candidates: list[tuple[int, str]] = []
        for item in configured:
            if not isinstance(item, dict):
                continue
            configured_root = str(item.get("root_label") or "").strip()
            prefix = str(item.get("relative_prefix") or "").strip().strip("/")
            label = str(item.get("project_label") or "").strip()
            if (
                label
                and (not configured_root or configured_root.casefold() == root_label.casefold())
                and (not prefix or relative == prefix or relative.startswith(prefix + "/"))
            ):
                candidates.append((len(prefix), label))
        if candidates:
            return _safe_root_label(max(candidates, key=lambda item: item[0])[1])

    return root_label or "未归类"


def _protected_name(name: str) -> bool:
    return document_protected_context_hit(name) or bool(SENSITIVE_NAME_RE.search(name))


def _protected_text(text: str) -> bool:
    return document_protected_context_hit(text) or bool(SENSITIVE_NAME_RE.search(text))


def _safe_label(value: str, limit: int) -> str:
    compact = " ".join(str(value or "").split())
    compact = SAFE_LABEL_RE.sub("", compact)
    return compact[:limit]


# The two sides of the foreground join redact file names under different rules.
# The change side (_snapshot) keeps case and digits and strips every character
# outside SAFE_LABEL_RE; document_signals._redact_file_name masks runs of four
# or more digits, lowercases the suffix and truncates the stem at 80.  Measured
# on this machine's 61,630 tracked-eligible files, 1.6% of names come out
# different, so an equality join silently drops them.  Normalizing both sides at
# read time repairs the join without rewriting a single stored row and without
# relaxing _safe_label, which is a privacy boundary rather than a join detail.
#
# Two residual classes stay unrecoverable and are deliberately not chased.
#
# 1. The two sides run redact_for_storage over different substrings (whole name
#    at 120 vs stem at 80), so when a redaction rule fires the texts genuinely
#    differ.  Measured residual after normalizing: 9 of 61,630 files.
#
# 2. Digit runs are lossy on the *signal* side at write time -- it stores
#    "[number]_letter_to_shareholders.md" for every year -- so a whole family of
#    dated siblings already shares one stored spelling there.  Measured here:
#    102 normalized keys cover more than one name, the largest folding 60 files.
#    Normalizing cannot undo information that was destroyed before it was
#    written; the most this side can do is match the signal side's precision.
#    The consequence is real and must not be dressed up: a foreground sighting
#    of one member marks the whole family foreground-linked.  That is why
#    foreground_linked stays a ranking and context hint and is never allowed to
#    become a factual claim about a specific file.
_DOCUMENT_NAME_NUMBER_TOKEN_RE = re.compile(r"\[number\]")
_DOCUMENT_NAME_DIGIT_RUN_RE = re.compile(r"\d{4,}")


def _document_name_join_key(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    stem, dot, suffix = text.rpartition(".")
    if not dot:
        stem, suffix = text, ""
    # No \b anchors here: the signal side uses \b, and a CJK character counts as
    # a word character, so "展会2021-2023" masks only its second run there.
    # Dropping both the token and the raw runs collapses the two spellings.
    stem = _DOCUMENT_NAME_NUMBER_TOKEN_RE.sub(" ", stem)
    stem = _DOCUMENT_NAME_DIGIT_RUN_RE.sub(" ", stem)
    stem = " ".join(_safe_label(stem, 80).split()).casefold()
    if not stem:
        # A name that is nothing but digits ("20260807.md") normalizes to its
        # suffix alone, which would make every dated note in a folder share one
        # key and link them all to any single foreground sighting.  Measured
        # here that was a 60-file collision group.  Refusing to produce a key is
        # correct: an empty key means "cannot judge", and _foreground_linked
        # then answers no rather than guessing yes.
        return ""
    suffix = _safe_label(suffix, 12).casefold()
    return f"{stem}.{suffix}" if suffix else stem


def _safe_root_label(value: str) -> str:
    raw = str(value or "")
    if "\\" in raw or _contains_absolute_path_signal(raw) or raw.startswith(("/", "~")):
        return "path_redacted"
    compact = " ".join(
        _safe_label(redact_for_storage(raw, max_len=80), 80).split()
    )
    path_probe = compact.replace(" / ", "")
    if not compact or compact.startswith(("/", "\\", "~", ".")) or "/" in path_probe or "\\" in path_probe:
        return "Tracked folder"
    return compact


def _git_ignored(root: Path, path: Path) -> bool:
    proofs = _BASELINE_IGNORE_PROOFS.get()
    if proofs is not None:
        answer = proofs.lookup(path)
        if answer is not None:
            return answer
    repo = _git_repo_for_path(root, path)
    if repo is None:
        return False
    ignored = _git_ignored_batch(repo, [path])
    if ignored is None:
        raise GitIgnoreEvaluationError("git ignore evaluation unavailable")
    return path in ignored


def _git_repo_for_path(root: Path, path: Path) -> Path | None:
    current = path.resolve(strict=False).parent
    while True:
        if (current / ".git").exists():
            return current
        if current.parent == current:
            break
        current = current.parent
    return None


_GIT_WORKTREE_ADMIN_NAME_RE = re.compile(r"\A[A-Za-z0-9._-]{1,255}\Z")


def _orphan_worktree_common_dir(repo: Path) -> Path | None:
    """Return a verified common Git dir for a missing linked-worktree admin.

    A stale linked-worktree pointer is not the same thing as an arbitrary Git
    failure.  Git has already lost that worktree's index, so normal
    ``check-ignore`` cannot decide whether an ignore-pattern match used to be
    tracked.  The privacy-safe fallback is therefore ``--no-index``: exclude
    every matching path rather than risk reading something the ignore rules
    protect.  Any malformed pointer, symlink, ownership mismatch, live admin
    entry, or invalid common repository returns ``None`` and preserves the
    ordinary fail-closed error path.
    """

    try:
        resolved_repo = _resolve_root(repo)
        descriptor, _ = _open_file_beneath(resolved_repo, resolved_repo / ".git")
    except (OSError, ValueError):
        return None
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_uid != os.getuid() or metadata.st_size > 4096:
            return None
        chunks: list[bytes] = []
        total = 0
        while total <= 4096:
            chunk = os.read(descriptor, min(4097 - total, 4097))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > 4096:
            return None
        raw = b"".join(chunks).decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    finally:
        os.close(descriptor)
    match = re.fullmatch(r"gitdir: ([^\x00\r\n]+)\n?", raw)
    if match is None:
        return None
    admin = Path(match.group(1))
    if not admin.is_absolute():
        admin = resolved_repo / admin
    if not _GIT_WORKTREE_ADMIN_NAME_RE.fullmatch(admin.name):
        return None
    try:
        admin_parent = _resolve_root(admin.parent)
        common = _resolve_root(admin_parent.parent)
    except ValueError:
        return None
    if admin_parent.name != "worktrees" or admin_parent != common / "worktrees":
        return None
    admin = admin_parent / admin.name
    if os.path.lexists(admin):
        return None
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_")
    }
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        probe = subprocess.run(
            ["git", "--git-dir", str(common), "rev-parse", "--git-common-dir"],
            cwd=resolved_repo,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if probe.returncode != 0:
        return None
    try:
        reported = Path(probe.stdout.strip())
        if not reported.is_absolute():
            reported = resolved_repo / reported
        if _resolve_root(reported) != common:
            return None
    except ValueError:
        return None
    return common


def _git_ignored_batch(repo: Path, paths: list[Path], *, stop_check=None) -> set[Path] | None:
    if not paths:
        return set()
    relative_to_path: dict[str, Path] = {}
    for path in paths:
        if stop_check is not None and bool(stop_check()):
            return None
        relative_to_path[str(path.resolve(strict=False).relative_to(repo))] = path
    ignored_names: set[str] = set()
    names = list(relative_to_path)
    orphan_common = _orphan_worktree_common_dir(repo)
    orphan_environment = None
    if orphan_common is not None:
        orphan_environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        orphan_environment["GIT_OPTIONAL_LOCKS"] = "0"
    for offset in range(0, len(names), 2000):
        if stop_check is not None and bool(stop_check()):
            return None
        payload = "\0".join(names[offset : offset + 2000]) + "\0"
        try:
            command = ["git", "check-ignore", "-z", "--stdin"]
            if orphan_common is not None:
                command = [
                    "git",
                    "--git-dir",
                    str(orphan_common),
                    "--work-tree",
                    str(repo),
                    "check-ignore",
                    "--no-index",
                    "-z",
                    "--stdin",
                ]
            # A full stdout pipe must never strand the parent in a blocking
            # stdin write, outside communicate()'s timeout checks.  On macOS
            # TemporaryFile is private and unlinked before this path list is
            # written.  Git reads the same NUL-delimited text from a regular
            # file while communicate() only has to drain its output pipe.
            with tempfile.TemporaryFile(mode="w+t", newline="") as request:
                request.write(payload)
                request.seek(0)
                result = subprocess.run(
                    command,
                    cwd=repo,
                    env=orphan_environment,
                    stdin=request,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=5,
                    text=True,
                )
        except (OSError, subprocess.SubprocessError):
            return None
        if stop_check is not None and bool(stop_check()):
            return None
        if result.returncode not in {0, 1}:
            return None
        returned = [value for value in result.stdout.split("\0") if value]
        if orphan_common is not None and (
            len(returned) != len(set(returned))
            or any(value not in relative_to_path for value in returned)
        ):
            return None
        ignored_names.update(returned)
    return {relative_to_path[value] for value in ignored_names if value in relative_to_path}


def _registry_path(runtime_root: str | Path) -> Path:
    return Path(runtime_root).expanduser() / RUNTIME_ROOTS_FILENAME


def _read_runtime_registry(runtime_root: str | Path | None) -> list[dict]:
    if runtime_root is None:
        return []
    status, items, _ = _load_runtime_registry(runtime_root)
    return items if status in {"valid", "legacy"} else []


def _load_runtime_registry(runtime_root: str | Path) -> tuple[str, list[dict], int | None]:
    path = _registry_path(runtime_root)
    try:
        payload = json.loads(_read_private_registry(path).decode("utf-8"))
    except FileNotFoundError:
        return "missing", [], None
    except (OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError):
        return "invalid", [], None
    if isinstance(payload, list):
        payload_status = "legacy"
        revision = None
        raw_items = payload
    elif isinstance(payload, dict):
        if set(payload) != {"format_version", "authorization_revision", "roots"}:
            return "invalid", [], None
        raw_revision = payload.get("authorization_revision")
        if (
            payload.get("format_version") != RUNTIME_ROOTS_FORMAT_VERSION
            or isinstance(raw_revision, bool)
            or not isinstance(raw_revision, int)
            or raw_revision < 0
            or not isinstance(payload.get("roots"), list)
        ):
            return "invalid", [], None
        payload_status = "valid"
        revision = raw_revision
        raw_items = payload["roots"]
    else:
        return "invalid", [], None
    items: list[dict] = []
    root_ids: set[str] = set()
    root_refs: set[str] = set()
    for item in raw_items:
        if not isinstance(item, dict):
            return "invalid", [], None
        root_id_value = item.get("root_id")
        root_path = item.get("path")
        root_label = item.get("root_label")
        root_ref = item.get("root_ref")
        has_root_label = "root_label" in item
        has_root_ref = "root_ref" in item
        if (
            "root_id" not in item
            or "path" not in item
            or not isinstance(root_id_value, str)
            or not _valid_registry_path(root_path)
            or (has_root_label and not isinstance(root_label, str))
            or (isinstance(root_label, str) and "\x00" in root_label)
            or (has_root_ref and not isinstance(root_ref, str))
        ):
            return "invalid", [], None
        root_id = root_id_value
        if ROOT_ID_RE.fullmatch(root_id) is None or root_id in root_ids:
            return "invalid", [], None
        root_ids.add(root_id)
        # A missing reference is the one accepted legacy shape and is upgraded
        # by a mutating configure/ensure call. Invalid or duplicate references
        # fail closed instead of creating ambiguous removal controls.
        if has_root_ref:
            if not _valid_root_ref(root_ref) or str(root_ref) in root_refs:
                return "invalid", [], None
            root_refs.add(str(root_ref))
        items.append(item)
    # Validate every public control token against every private root identity,
    # not only the identity in the same registry row. This prevents one row
    # from laundering another row's HMAC (or a long prefix) into public status.
    for item in items:
        root_ref = item.get("root_ref")
        if not _valid_root_ref(root_ref):
            continue
        root_ref_text = str(root_ref)
        if _contains_internal_root_identity(root_ref_text, root_ids):
            return "invalid", [], None
    for item in items:
        if _contains_internal_root_identity(str(item.get("root_label") or ""), root_ids):
            return "invalid", [], None
    return payload_status, items, revision


def _write_runtime_registry(
    runtime_root: str | Path,
    items: list[dict],
    authorization_revision: int,
) -> None:
    if isinstance(authorization_revision, bool) or not isinstance(authorization_revision, int) or authorization_revision < 0:
        raise ValueError("invalid document watch authorization revision")
    path = _registry_path(runtime_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    safe_items = []
    candidate_root_ids = {
        str(item.get("root_id") or "")
        for item in items
        if ROOT_ID_RE.fullmatch(str(item.get("root_id") or "")) is not None
    }
    seen_root_ids: set[str] = set()
    seen_root_refs: set[str] = set()
    for item in items:
        root_id_value = item.get("root_id")
        root_path_value = item.get("path")
        root_label_value = item.get("root_label")
        if (
            not isinstance(root_id_value, str)
            or root_id_value not in candidate_root_ids
            or not _valid_registry_path(root_path_value)
            or ("root_label" in item and not isinstance(root_label_value, str))
            or (isinstance(root_label_value, str) and "\x00" in root_label_value)
            or root_id_value in seen_root_ids
        ):
            raise ValueError("invalid document watch registry item")
        root_id = root_id_value
        root_path = str(root_path_value)
        root_ref = _normalized_root_ref(item.get("root_ref"))
        while root_ref in seen_root_refs or _contains_internal_root_identity(root_ref, candidate_root_ids):
            root_ref = _new_root_ref()
        safe_items.append(
            {
                "root_id": root_id,
                "root_ref": root_ref,
                "root_label": _safe_root_label(str(item.get("root_label") or "Tracked folder")),
                "path": root_path,
            }
        )
        seen_root_ids.add(root_id)
        seen_root_refs.add(root_ref)
    payload = {
        "format_version": RUNTIME_ROOTS_FORMAT_VERSION,
        "authorization_revision": authorization_revision,
        "roots": safe_items,
    }
    encoded = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(8)}.tmp"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        written = 0
        while written < len(encoded):
            written += os.write(descriptor, encoded[written:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        temporary.replace(path)
        path.chmod(0o600)
        directory_descriptor = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _restore_runtime_registry(
    runtime_root: str | Path,
    previous_status: str,
    previous_items: list[dict],
    previous_revision: int,
) -> None:
    if previous_status == "missing":
        try:
            _registry_path(runtime_root).unlink()
        except FileNotFoundError:
            pass
        return
    if previous_status == "invalid":
        # Preserve the newly written higher revision. The rolled-back database
        # revision cannot match it, so startup stays fail-closed.
        return
    _write_runtime_registry(runtime_root, previous_items, previous_revision)


def _valid_root_ref(value: object) -> bool:
    return isinstance(value, str) and ROOT_REF_RE.fullmatch(value) is not None


def _new_root_ref() -> str:
    return ROOT_REF_PREFIX + secrets.token_urlsafe(18)


def _normalized_root_ref(value: object) -> str:
    return str(value) if _valid_root_ref(value) else _new_root_ref()


def _contains_internal_root_identity(value: str, root_ids: Iterable[str]) -> bool:
    if not value:
        return False
    normalized_value = value.casefold()
    for root_id in root_ids:
        root_id_text = str(root_id).casefold()
        root_hash = root_id_text.removeprefix("root_")
        if root_id_text in normalized_value:
            return True
        if len(root_hash) >= 24 and any(
            root_hash[start:start + 24] in normalized_value
            for start in range(len(root_hash) - 23)
        ):
            return True
    return False


def _contains_absolute_path_signal(value: str) -> bool:
    return bool(value and ABSOLUTE_PATH_SIGNAL_RE.search(value))


def _safe_public_timestamp(value: object) -> str:
    text = str(value or "").strip()
    if not text or "T" not in text or _contains_absolute_path_signal(text):
        return ""
    return text if _parse_datetime_strict(text) is not None else ""


def _valid_registry_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    if not Path(value).is_absolute():
        return False
    return os.path.normpath(value) == value


def _registry_root_bindings_valid(conn: sqlite3.Connection, registry: list[dict]) -> bool:
    """Confirm every private root ID was derived from its authorized real path."""
    if not registry:
        return True
    try:
        secret = _existing_hmac_key_readonly(conn)
        for item in registry:
            row = conn.execute("SELECT enabled FROM file_watch_roots WHERE root_id=?", (item["root_id"],)).fetchone()
            stored = str(item["path"])
            if row is not None and not bool(row["enabled"]):
                if not Path(stored).is_absolute() or os.path.normpath(stored) != stored:
                    return False
                resolved = stored
            else:
                resolved = _resolve_root(stored)
            expected = "root_" + _hmac(secret, str(resolved))[:32]
            if not hmac.compare_digest(expected, str(item["root_id"])):
                return False
    except (KeyError, OSError, RuntimeError, ValueError, sqlite3.DatabaseError):
        return False
    return True


def _read_private_registry(path: Path) -> bytes:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat_module.S_ISREG(metadata.st_mode):
            raise RuntimeError("document watch registry is not a regular file")
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o777 != 0o600:
            raise RuntimeError("document watch registry permissions are unsafe")
        chunks: list[bytes] = []
        total = 0
        while total <= RUNTIME_ROOTS_MAX_BYTES:
            chunk = os.read(descriptor, min(64 * 1024, RUNTIME_ROOTS_MAX_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > RUNTIME_ROOTS_MAX_BYTES:
            raise RuntimeError("document watch registry is too large")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _registry_match(registry: list[dict], path: Path) -> dict | None:
    matches = []
    for item in registry:
        try:
            root = Path(str(item["path"])).resolve(strict=True)
            path.resolve(strict=False).relative_to(root)
        except (OSError, ValueError, KeyError):
            continue
        matches.append((len(root.parts), item))
    return max(matches, default=(0, None), key=lambda value: value[0])[1]


def _size_bucket(size: int) -> str:
    if size < 1024:
        return "lt_1k"
    if size < 1024 * 1024:
        return "lt_1m"
    if size < 50 * 1024 * 1024:
        return "lt_50m"
    return "gte_50m"


def _iso(value: str) -> str:
    if not value:
        return _now()
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return _now()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_datetime_strict(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _timestamp_at_or_before(value: str, boundary: str) -> bool:
    parsed = _parse_datetime_strict(value)
    parsed_boundary = _parse_datetime_strict(boundary)
    return bool(
        parsed is not None
        and parsed_boundary is not None
        and parsed <= parsed_boundary
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")
