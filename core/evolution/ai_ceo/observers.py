"""Read-only, content-free Sync Center receipt observation.

This module does not import the observed project, invoke its validators, inspect
its databases, or acquire its locks. ``healthy`` is scoped to a matching set of
validation receipts, never to the full business system or fresh source contents.
The runtime projection is based on the four schema files inspected on 2026-09-08.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
import stat
from datetime import datetime, timedelta, timezone
from typing import Any

ASSET_ID = "sync-center"
FILES = {
    "request": "validation-request.json",
    "worker": "automatic-memory-validation.json",
    "success": "last-memory-validation-success.json",
    "failure": "last-memory-validation-failure.json",
}
MAX_FILE_BYTES = 128 * 1024
MAX_AGE_SECONDS = 30 * 60  # Confidence cutoff, not an application failure SLA.
FUTURE_TOLERANCE_SECONDS = 60
ERROR_CODES = frozenset({
    "validation_inputs_changed", "automatic_validation_timeout",
    "automatic_validation_interrupted", "automatic_validation_state_invalid",
    "validation_permission_denied", "validation_context_unavailable",
    "validation_output", "validation_command_unavailable", "validation_failed",
    "validation_receipt_unproven", "source_inventory_refresh_timeout",
    "source_inventory_refresh_failed", "memory_index_update_failed",
    "validation_already_running", "memory_core_busy", "maintenance_active",
    "memory_index_update_pending", "other_error",
})
WAIT_CODES = frozenset({"validation_inputs_changed", "validation_already_running",
                        "memory_core_busy", "maintenance_active", "memory_index_update_pending"})
WORKER_STATUSES = frozenset({"running", "retry_wait", "verified", "waiting_stable",
                            "failed", "interrupted", "blocked", "superseded"})
READ_STATUSES = frozenset({"ok", "missing", "unreadable", "unsafe_path", "not_regular",
                          "too_large", "invalid_json", "changed_during_read"})
CONTEXT_KEYS = ("code_sha256", "database_snapshot_sha256", "source_snapshot_sha256")
TIME_FIELDS = {
    "request": ("requested_at",),
    "worker": ("updated_at", "started_at", "completed_at", "pending_since", "observed_at",
               "stable_since", "next_retry_at", "validation_receipt_at"),
    "success": ("recorded_at",), "failure": ("recorded_at",),
}
INT_FIELDS = {
    "request": ("schema_version", "required_generation"),
    "worker": ("schema_version", "index_generation", "attempt_count", "timeout_count", "defer_count"),
    "success": ("schema_version", "index_dirty_generation", "index_applied_generation"),
    "failure": ("index_generation", "attempt_count"),
}
ID_FIELDS = {
    "request": ("source_run_id",), "worker": ("run_id", "job_id", "input_key"),
    "success": ("run_id",), "failure": ("run_id", "input_key"),
}
REASON_CODES = frozenset({
    "sources_unavailable", "snapshot_incomplete", "invalid_schema", "invalid_timestamp",
    "future_timestamp", "stale_observation", "invalid_accounting", "request_pending",
    "validation_running", "waiting_for_stability", "retry_scheduled", "resource_wait",
    "receipt_missing", "receipt_binding_mismatch", "receipt_context_mismatch",
    "receipt_generation_mismatch", "worker_receipt_conflict", "latest_failure",
    "historical_failure_recovered", "matching_validation_receipt", "state_unresolved",
    "receipt_order_uncertain",
})


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _stamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("now must contain a timezone")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode("utf-8")).hexdigest()


def _opaque(value: Any) -> str | None:
    # Digest untrusted identifiers instead of storing source values. Equality is
    # sufficient for binding; no identifier ever becomes a filesystem path.
    if isinstance(value, str) and value.startswith("sha256:") and len(value) == 71 and all(c in "0123456789abcdef" for c in value[7:]):
        return value
    if isinstance(value, str) and 0 < len(value) <= 4096:
        return "sha256:" + _digest(value)
    return None


def _fingerprint(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    if len(value) == 64 and all(c in "0123456789abcdef" for c in value):
        return _opaque(value)
    if value.startswith("sha256:") and len(value) == 71 and all(c in "0123456789abcdef" for c in value[7:]):
        return value
    return None


def _integer(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 1_000_000_000 else None


def _project(logical: str, raw: Any) -> dict:
    if not isinstance(raw, dict):
        return {}
    result: dict[str, Any] = {}
    for key in TIME_FIELDS[logical]:
        if key in raw:
            timestamp = _time(raw[key])
            result[key] = _stamp(timestamp) if timestamp else None
    for key in INT_FIELDS[logical]:
        if key in raw:
            result[key] = _integer(raw[key])
    for key in ID_FIELDS[logical]:
        if key in raw:
            result[key] = _fingerprint(raw[key]) if key == "input_key" else _opaque(raw[key])
    if logical in {"worker", "success"}:
        context = raw.get("validation_context")
        result["validation_context"] = {
            key: _fingerprint(context.get(key)) for key in CONTEXT_KEYS
        } if isinstance(context, dict) else {}
    if logical == "worker":
        result["status"] = raw.get("status") if isinstance(raw.get("status"), str) and raw.get("status") in WORKER_STATUSES else None
        result["phase"] = "inventory" if raw.get("phase") == "inventory" else None
    if logical in {"worker", "failure"}:
        for key in ("error_code", "preflight_error") if logical == "worker" else ("error_code",):
            value = raw.get(key)
            result[key] = value if isinstance(value, str) and value in ERROR_CODES else (
                "other_error" if value is not None else None)
    if logical == "success":
        result["full_validation"] = raw.get("full_validation") is True
        result["index_state"] = "up_to_date" if raw.get("index_state") == "up_to_date" else None
    if logical in {"success", "failure"}:
        result["verdict"] = raw.get("verdict") if isinstance(raw.get("verdict"), str) and raw.get("verdict") in {"pass", "fail", "deferred"} else None
    return result


def _safe_directory(path: Path) -> int:
    """Open each absolute path component without following symlinks."""
    path = path.absolute()
    parts = path.parts
    if ".." in parts:
        raise OSError(errno.ELOOP, "unsafe path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(path.anchor, flags)
    try:
        for name in parts[1:]:
            next_descriptor = os.open(name, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _signature(info: os.stat_result) -> tuple:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _json_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _read_source(directory_fd: int, filename: str) -> tuple[dict, str, tuple | None]:
    descriptor = None
    try:
        descriptor = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            return {}, "not_regular", None
        if before.st_size > MAX_FILE_BYTES:
            return {}, "too_large", None
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            content = stream.read(MAX_FILE_BYTES + 1)
            after = os.fstat(stream.fileno())
        if len(content) > MAX_FILE_BYTES:
            return {}, "too_large", None
        if _signature(before) != _signature(after):
            return {}, "changed_during_read", None
        raw = json.loads(content, object_pairs_hook=_json_object,
                         parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
        if not isinstance(raw, dict):
            return {}, "invalid_json", None
        return raw, "ok", _signature(after)
    except FileNotFoundError:
        return {}, "missing", None
    except (ValueError, UnicodeError, RecursionError):
        return {}, "invalid_json", None
    except OSError as error:
        return {}, "unsafe_path" if error.errno in {errno.ELOOP, errno.ENOTDIR} else "unreadable", None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def read_sync_snapshot(root: Path) -> dict:
    """Read precisely four fixed, bounded, regular files; return no raw values.

    Root is the project directory, not its state directory. Missing optional
    failure/request files are represented explicitly. Any unsafe directory
    component is refused, including a symlink in an ancestor path.
    """
    snapshot: dict[str, Any] = {"schema_version": 1, "sources": []}
    directory_fd = None
    signatures = {}
    try:
        directory_fd = _safe_directory(Path(root) / "state")
        for logical, filename in FILES.items():
            raw, status_code, signature = _read_source(directory_fd, filename)
            snapshot[logical] = _project(logical, raw) if status_code == "ok" else {}
            snapshot["sources"].append({"source": logical, "read_status": status_code})
            signatures[logical] = signature
        # Detect atomic replacements during multi-file sampling; never stitch a
        # known inconsistent collection into a green status.
        for source in snapshot["sources"]:
            logical = source["source"]
            if source["read_status"] not in {"ok", "missing"}:
                continue
            try:
                final = os.stat(FILES[logical], dir_fd=directory_fd, follow_symlinks=False)
                consistent = source["read_status"] == "ok" and stat.S_ISREG(final.st_mode) and _signature(final) == signatures[logical]
            except FileNotFoundError:
                consistent = source["read_status"] == "missing"
            except OSError:
                consistent = False
            if not consistent:
                source["read_status"] = "changed_during_read"
                snapshot[logical] = {}
        # Refuse a state directory renamed/replaced while descriptors were open.
        check_fd = _safe_directory(Path(root) / "state")
        try:
            original_dir, current_dir = os.fstat(directory_fd), os.fstat(check_fd)
            if (original_dir.st_dev, original_dir.st_ino) != (current_dir.st_dev, current_dir.st_ino):
                snapshot.update({logical: {} for logical in FILES})
                snapshot["sources"] = [{"source": name, "read_status": "changed_during_read"} for name in FILES]
        finally:
            os.close(check_fd)
    except OSError as error:
        code = "unsafe_path" if error.errno in {errno.ELOOP, errno.ENOTDIR} else (
            "missing" if error.errno == errno.ENOENT else "unreadable")
        snapshot.update({logical: {} for logical in FILES})
        snapshot["sources"] = [{"source": name, "read_status": code} for name in FILES]
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
    return snapshot


def evaluate_sync_snapshot(snapshot: dict, now: datetime | None = None) -> dict:
    """Classify a content-free snapshot; raw-shaped synthetic fixtures also work.

    ``evidence_quality=partial`` is intentional: matching these files does not
    recompute current business data context. Future/stale or contradictory
    receipts cannot establish current success, nor do they prove failure.
    """
    now = now or datetime.now(timezone.utc)
    observed_at = _stamp(now)
    now = _time(observed_at)
    if not isinstance(snapshot, dict):
        snapshot = {}
    records = {logical: _project(logical, snapshot.get(logical, {})) for logical in FILES}
    request, worker, success, failure = (records[key] for key in FILES)
    supplied_sources = snapshot.get("sources")
    sources = {}
    if isinstance(supplied_sources, list):
        for item in supplied_sources[:8]:
            if isinstance(item, dict) and isinstance(item.get("source"), str) and item.get("source") in FILES:
                code = item.get("read_status")
                sources[item["source"]] = code if isinstance(code, str) and code in READ_STATUSES else "unreadable"
    for logical in FILES:
        sources.setdefault(logical, "ok" if snapshot.get(logical) else "missing")
    evidence = [{"source": logical, "read_status": sources[logical],
                 "recorded_at": records[logical].get("updated_at") or records[logical].get("recorded_at")
                 or records[logical].get("requested_at")}
                for logical in FILES]
    metrics = {field: worker.get(field) for field in ("attempt_count", "timeout_count", "defer_count")}
    metrics["receipt_scope_only"] = True
    pending = _time(worker.get("pending_since"))
    terminal = worker.get("status") in {"verified", "failed", "interrupted", "blocked"}
    ended = _time(worker.get("completed_at")) or _time(success.get("recorded_at")) if terminal else now
    metrics["wait_seconds"] = max(0, int((ended - pending).total_seconds())) if pending and ended and pending <= ended <= now else None
    # Stable issue identity excludes observation time, retry counters and file
    # mtime. Retries within one job aggregate instead of creating new tasks.
    identity = worker.get("job_id") or request.get("source_run_id") or worker.get("input_key") or "unbound"

    def result(status: str, reason: str, decision: str = "observe") -> dict:
        assert reason in REASON_CODES
        return {
            "asset_id": ASSET_ID, "status": status,
            "evidence_quality": "insufficient" if status == "unknown" else "partial",
            "reason_code": reason, "observed_at": observed_at,
            "issue_key": "sync:" + _digest([ASSET_ID, identity]),
            "evidence": evidence, "metrics": metrics, "decision": decision,
        }

    if all(sources[key] != "ok" for key in FILES):
        return result("unknown", "sources_unavailable")
    if sources["worker"] != "ok" or any(code not in {"ok", "missing"} for code in sources.values()):
        return result("unknown", "snapshot_incomplete")
    if not worker or worker.get("schema_version") not in {1, 2} or worker.get("status") not in WORKER_STATUSES:
        return result("unknown", "invalid_schema")
    if sources["request"] == "ok" and (request.get("schema_version") != 1 or not request.get("source_run_id")):
        return result("unknown", "invalid_schema")
    # All supplied relevant historical timestamps must be valid and ordered.
    # Optional null worker timestamps are normal, but null mandatory times are not.
    mandatory_times = {"worker": "updated_at", "request": "requested_at",
                       "success": "recorded_at", "failure": "recorded_at"}
    for logical, record in records.items():
        if sources[logical] == "ok" and not _time(record.get(mandatory_times[logical])):
            return result("unknown", "invalid_timestamp")
        for key in TIME_FIELDS[logical]:
            timestamp = _time(record.get(key))
            if timestamp and key != "next_retry_at" and timestamp > now + timedelta(seconds=FUTURE_TOLERANCE_SECONDS):
                return result("unknown", "future_timestamp")
    updated = _time(worker.get("updated_at"))
    if (now - updated).total_seconds() > MAX_AGE_SECONDS:
        return result("unknown", "stale_observation")
    counts = [worker.get(key) for key in ("attempt_count", "timeout_count", "defer_count")]
    if any(value is None for value in counts) or counts[1] > 3 or counts[1] > counts[0] or counts[2] > counts[0]:
        return result("unknown", "invalid_accounting")
    request_at = _time(request.get("requested_at"))
    success_at = _time(success.get("recorded_at"))
    failure_at = _time(failure.get("recorded_at"))
    status = worker["status"]
    error = worker.get("preflight_error") or worker.get("error_code")
    # A freshly created request supersedes the worker's previous disposition,
    # even if its fingerprint has returned to a previously passing value.
    if request_at and request_at >= updated:
        return result("waiting", "request_pending")
    if success_at and failure_at and success_at == failure_at:
        return result("unknown", "receipt_order_uncertain")
    latest_failure = bool(failure_at and (not success_at or failure_at > success_at))
    failure_matches_input = bool(failure.get("input_key") and failure.get("input_key") == worker.get("input_key")
                                 and failure.get("index_generation") == worker.get("index_generation"))
    failure_matches_run = bool(failure.get("run_id") and failure.get("run_id") == worker.get("run_id"))
    if latest_failure and (now - failure_at).total_seconds() > MAX_AGE_SECONDS:
        return result("unknown", "stale_observation")
    if latest_failure and failure.get("verdict") != "fail":
        return result("unknown", "invalid_schema")
    if latest_failure and not failure_matches_input:
        # It could concern a different generation/run. No global failure or
        # success claim can be made from this restricted snapshot.
        return result("unknown", "receipt_binding_mismatch")
    # A recent running/waiting state after a retained failure indicates the
    # existing project controller owns recovery; this observer never retries.
    if status == "running":
        started = _time(worker.get("started_at"))
        if not started or not worker.get("run_id") or not worker.get("job_id"):
            return result("unknown", "invalid_schema")
        if latest_failure and (failure_at >= started or failure_matches_run):
            return result("unknown", "worker_receipt_conflict")
        return result("waiting", "validation_running")
    if status in {"waiting_stable", "retry_wait", "superseded"} or error in WAIT_CODES:
        if latest_failure and failure_at > updated:
            return result("unknown", "worker_receipt_conflict")
        if error in WAIT_CODES - {"validation_inputs_changed"}:
            return result("waiting", "resource_wait")
        if status == "retry_wait":
            if not _time(worker.get("next_retry_at")) or not 0 < counts[1] < 3:
                return result("unknown", "invalid_accounting")
            return result("waiting", "retry_scheduled")
        return result("waiting", "waiting_for_stability")
    if latest_failure:
        if failure_matches_run and (not request_at or failure_at > request_at):
            return result("fault", "latest_failure", "investigate")
        return result("unknown", "receipt_binding_mismatch")
    if status != "verified" or error is not None:
        return result("unknown", "state_unresolved")
    if sources["success"] != "ok":
        return result("unknown", "receipt_missing")
    if success.get("schema_version") != 2 or success.get("verdict") != "pass" or not success.get("full_validation"):
        return result("unknown", "invalid_schema")
    if request_at and success_at <= request_at:
        return result("waiting", "request_pending")
    if (now - success_at).total_seconds() > MAX_AGE_SECONDS:
        return result("unknown", "stale_observation")
    if success_at > updated + timedelta(seconds=1):
        return result("unknown", "worker_receipt_conflict")
    context = worker.get("validation_context", {})
    if not all(context.get(key) for key in CONTEXT_KEYS) or success.get("validation_context") != context:
        return result("unknown", "receipt_context_mismatch")
    generation = worker.get("index_generation")
    required = request.get("required_generation")
    if (generation is None or success.get("index_dirty_generation") != generation
            or success.get("index_applied_generation") != generation
            or success.get("index_state") != "up_to_date"
            or (required is not None and generation < required)):
        return result("unknown", "receipt_generation_mismatch")
    # An explicit accepted receipt timestamp supports a manually validated run;
    # absent that linkage, matching worker/receipt run identities are necessary.
    explicitly_accepted = _time(worker.get("validation_receipt_at")) == success_at
    same_run = bool(worker.get("run_id") and worker.get("run_id") == success.get("run_id"))
    if not success.get("run_id") or not (same_run or explicitly_accepted):
        return result("unknown", "receipt_binding_mismatch")
    started_at = _time(worker.get("started_at"))
    completed_at = _time(worker.get("completed_at"))
    if same_run and started_at and (success_at < started_at or (completed_at and completed_at < started_at)):
        return result("unknown", "invalid_timestamp")
    if failure_at:
        return result("recovered", "historical_failure_recovered", "no_change")
    return result("healthy", "matching_validation_receipt", "no_change")


def collect_sync(root: Path, now: datetime | None = None) -> dict:
    return evaluate_sync_snapshot(read_sync_snapshot(root), now=now)
