#!/usr/bin/env python3
"""Record AI file writes for PAM without recording file contents.

The hook accepts the existing Claude ``file_path`` / ``notebook_path``
payloads and Codex ``apply_patch`` payloads.  Codex paths are taken only from
the patch operation headers; diff bodies, prompts, and file contents are never
written to the attribution log.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO


LOG_DIR = Path.home() / ".pam-ai-attribution"
LOG_PATH = LOG_DIR / "edits.jsonl"
ROTATED_PATH = LOG_DIR / "edits.jsonl.1"
MAX_BYTES = 4 * 1024 * 1024
ROOTS_MAX_BYTES = 256 * 1024
STDIN_MAX_BYTES = 4 * 1024 * 1024
MAX_CANDIDATE_PATHS = 256
MAX_PATH_BYTES = 4096
MAX_BATCH_BYTES = 256 * 1024
MAX_SOURCE_BYTES = 64
MAX_ACTION_BYTES = 128
MAX_SESSION_BYTES = 256

ROOTS_JSON = (
    Path.home()
    / "Library/Application Support/Personal AI Monitor/core/data/document_watch_roots.json"
)
DATABASE_PATH = ROOTS_JSON.parent / "activity.sqlite"
DATABASE_WAL_PATH = Path(f"{DATABASE_PATH}-wal")
DATABASE_SHM_PATH = Path(f"{DATABASE_PATH}-shm")
AUTHORIZATION_LOCK_PATH = ROOTS_JSON.parent / ".document_watch_authorization.lock"
HMAC_SECRET_PATH = ROOTS_JSON.parent / "document_watch_secret.key"
HMAC_VERIFIER_STATE_KEY = "document_change_hmac_verifier"
HMAC_VERIFIER_DOMAIN = "personal-ai-monitor-document-watch-key-v1"
LEGACY_HMAC_STATE_KEY = "document_change_hmac_key"
SQLITE_TIMEOUT_SECONDS = 0.05

_PATCH_FILE_HEADER_RE = re.compile(
    r"^\*\*\* (?P<operation>Add File|Update File|Delete File|Move to): (?P<path>.+?)\r?$",
    re.MULTILINE,
)
_ROOT_ID_RE = re.compile(r"\Aroot_[0-9a-f]{32}\Z")
_ROOT_REF_RE = re.compile(r"\Arootref_[A-Za-z0-9_-]{24}\Z")
_SAFE_LOG_TOKEN_RE = re.compile(r"\A[A-Za-z0-9._:@+\-]*\Z")


def _has_parent_reference(path: Path) -> bool:
    return ".." in path.parts


def _normalized_absolute_path(
    raw_path: object,
    *,
    cwd: object | None = None,
) -> Path | None:
    """Return a canonical absolute path, rejecting traversal syntax.

    ``resolve(strict=False)`` follows existing symlinks while still supporting
    paths for files that an Add operation has not created yet.  Explicit
    parent components are rejected before resolution so they cannot be used to
    cross an authorization boundary or disguise a duplicate path.
    """

    if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
        return None
    try:
        if len(raw_path.encode("utf-8")) > MAX_PATH_BYTES:
            return None
    except UnicodeEncodeError:
        return None
    try:
        candidate = Path(raw_path).expanduser()
    except (OSError, RuntimeError, ValueError):
        return None
    if _has_parent_reference(candidate):
        return None
    if not candidate.is_absolute():
        if not isinstance(cwd, str) or not cwd or "\x00" in cwd:
            return None
        try:
            if len(cwd.encode("utf-8")) > MAX_PATH_BYTES:
                return None
        except UnicodeEncodeError:
            return None
        try:
            working_directory = Path(cwd).expanduser()
        except (OSError, RuntimeError, ValueError):
            return None
        if not working_directory.is_absolute() or _has_parent_reference(working_directory):
            return None
        candidate = working_directory / candidate
    try:
        return candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None


def _resolve_authorized_root(raw_path: str) -> Path | None:
    """Resolve a sidecar root without accepting user-controlled symlinks."""

    try:
        lexical = Path(raw_path).expanduser()
        if not lexical.is_absolute():
            return None
        current = Path(lexical.anchor)
        for component in lexical.parts[1:]:
            current = current / component
            # macOS exposes these fixed aliases into /private.  Core treats
            # them as system path aliases rather than user consent indirection.
            system_alias = str(current) in {"/var", "/tmp", "/etc"}
            if current.is_symlink() and not system_alias:
                return None
        resolved = lexical.resolve(strict=True)
        return resolved if resolved.is_dir() else None
    except (OSError, RuntimeError, ValueError):
        return None


def _contains_internal_root_identity(value: str, root_ids: set[str]) -> bool:
    normalized = value.casefold()
    for root_id in root_ids:
        root_id_text = root_id.casefold()
        root_hash = root_id_text.removeprefix("root_")
        if root_id_text in normalized:
            return True
        if any(
            root_hash[start : start + 24] in normalized
            for start in range(len(root_hash) - 23)
        ):
            return True
    return False


def _tracked_root_authorization() -> tuple[int, list[tuple[str, Path]]] | None:
    """Load and canonicalize PAM's private watch-root authorization set.

    A missing, unreadable, malformed, or empty authorization set returns no
    roots.  Callers must treat that result as fail-closed.
    """

    try:
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            return None
        descriptor = os.open(
            ROOTS_JSON,
            os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0),
        )
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                return None
            if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
                return None
            chunks: list[bytes] = []
            total = 0
            while total <= ROOTS_MAX_BYTES:
                chunk = os.read(
                    descriptor,
                    min(64 * 1024, ROOTS_MAX_BYTES + 1 - total),
                )
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            if total > ROOTS_MAX_BYTES:
                return None
        finally:
            os.close(descriptor)
        payload = json.loads(b"".join(chunks).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict) or set(payload) != {
        "format_version",
        "authorization_revision",
        "roots",
    }:
        return None
    version = payload.get("format_version")
    revision = payload.get("authorization_revision")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != 2
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
    ):
        return None
    entries = payload.get("roots")
    if not isinstance(entries, list) or not entries:
        return None

    root_bindings: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    root_ids: set[str] = set()
    root_refs: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        root_id = entry.get("root_id")
        root_ref = entry.get("root_ref")
        root_label = entry.get("root_label")
        raw_root = entry.get("path")
        has_root_ref = "root_ref" in entry
        has_root_label = "root_label" in entry
        if (
            "root_id" not in entry
            or "path" not in entry
            or not isinstance(root_id, str)
            or _ROOT_ID_RE.fullmatch(root_id) is None
            or root_id in root_ids
            or (has_root_ref and not isinstance(root_ref, str))
            or (
                has_root_ref
                and isinstance(root_ref, str)
                and (_ROOT_REF_RE.fullmatch(root_ref) is None or root_ref in root_refs)
            )
            or (has_root_label and not isinstance(root_label, str))
            or (isinstance(root_label, str) and "\x00" in root_label)
            or not isinstance(raw_root, str)
            or not raw_root
            or "\x00" in raw_root
            or not Path(raw_root).is_absolute()
            or os.path.normpath(raw_root) != raw_root
        ):
            return None
        root_ids.add(root_id)
        if isinstance(root_ref, str):
            root_refs.add(root_ref)
        root = _resolve_authorized_root(raw_root)
        if root is None or root in seen:
            return None
        root_bindings.append((root_id, root))
        seen.add(root)
    if any(
        (
            isinstance(entry.get("root_ref"), str)
            and _contains_internal_root_identity(str(entry["root_ref"]), root_ids)
        )
        or _contains_internal_root_identity(str(entry.get("root_label") or ""), root_ids)
        for entry in entries
    ):
        return None
    return revision, root_bindings


def tracked_roots() -> list[Path]:
    authorization = _tracked_root_authorization()
    return [root for _, root in authorization[1]] if authorization is not None else []


def is_tracked(path: Path, roots: list[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _candidate_operations(payload: dict) -> list[tuple[object, str]]:
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return []

    candidate = tool_input.get("file_path") or tool_input.get("notebook_path")
    if candidate:
        return [(candidate, "write")]

    if payload.get("tool_name") != "apply_patch":
        return []
    command = tool_input.get("command")
    if not isinstance(command, str) or not command:
        return []
    lines = command.strip().splitlines()
    if (
        len(lines) < 2
        or lines[0] != "*** Begin Patch"
        or lines[-1] != "*** End Patch"
        or lines.count("*** Begin Patch") != 1
        or lines.count("*** End Patch") != 1
    ):
        return []
    body = "\n".join(lines[1:-1])
    candidates: list[tuple[object, str]] = []
    for match in _PATCH_FILE_HEADER_RE.finditer(body):
        operation = {
            "Add File": "created",
            "Update File": "modified",
            "Delete File": "deleted",
            "Move to": "renamed",
        }[match.group("operation")]
        candidates.append((match.group("path"), operation))
        if len(candidates) > MAX_CANDIDATE_PATHS:
            return []
    return candidates


def _candidate_paths(payload: dict) -> list[object]:
    """Compatibility helper used by older tests and integrations."""

    return [path for path, _ in _candidate_operations(payload)]


def _safe_log_token(value: object, max_bytes: int) -> str | None:
    if value is None:
        return ""
    if not isinstance(value, str) or _SAFE_LOG_TOKEN_RE.fullmatch(value) is None:
        return None
    try:
        return value if len(value.encode("utf-8")) <= max_bytes else None
    except UnicodeEncodeError:
        return None


def _read_stdin_bounded(stream: TextIO) -> str | None:
    try:
        raw = stream.read(STDIN_MAX_BYTES + 1)
    except Exception:
        return None
    if not isinstance(raw, str):
        return None
    try:
        return raw if len(raw.encode("utf-8")) <= STDIN_MAX_BYTES else None
    except UnicodeEncodeError:
        return None


def _authorized_paths(payload: dict, roots: list[Path]) -> list[tuple[Path, str]]:
    cwd = payload.get("cwd")
    paths: list[tuple[Path, str]] = []
    seen: dict[Path, int] = {}
    operation_rank = {"write": 0, "modified": 1, "renamed": 2, "deleted": 3, "created": 4}
    for raw_path, operation in _candidate_operations(payload):
        path = _normalized_absolute_path(raw_path, cwd=cwd)
        if path is None or not is_tracked(path, roots):
            continue
        if path in seen:
            index = seen[path]
            if operation_rank.get(operation, 0) > operation_rank.get(paths[index][1], 0):
                paths[index] = (path, operation)
            continue
        seen[path] = len(paths)
        paths.append((path, operation))
    return paths


def _identity_for_path(
    secret: bytes,
    path: Path,
    root_bindings: list[tuple[str, Path]],
) -> tuple[str, str] | None:
    for root_id, root in root_bindings:
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            continue
        if not relative or any(part in {"", ".", ".."} for part in Path(relative).parts):
            return None
        file_id = "file_" + _hmac_value(secret, f"{root_id}:{relative}")[:40]
        return root_id, file_id
    return None


def _identity_secret_readonly() -> bytes | None:
    opened = _open_private_regular(HMAC_SECRET_PATH)
    if opened is None:
        return None
    descriptor, _ = opened
    try:
        secret = os.read(descriptor, 33)
    finally:
        os.close(descriptor)
    return secret if len(secret) == 32 else None


def _open_log_directory() -> int | None:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory_flag = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory_flag is None:
        return None
    try:
        LOG_DIR.mkdir(mode=0o700, exist_ok=True)
        descriptor = os.open(
            LOG_DIR,
            os.O_RDONLY
            | directory_flag
            | nofollow
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
            os.close(descriptor)
            return None
        os.fchmod(descriptor, 0o700)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except (OSError, RuntimeError):
        try:
            os.close(descriptor)
        except (NameError, OSError):
            pass
        return None


def _open_regular_at(
    directory_fd: int,
    name: str,
    flags: int,
    *,
    create: bool = False,
    exclusive: bool = False,
) -> tuple[int, os.stat_result] | None:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("safe no-follow open is unavailable")
    open_flags = flags | nofollow | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    if create:
        open_flags |= os.O_CREAT
    if exclusive:
        open_flags |= os.O_EXCL
    try:
        descriptor = os.open(name, open_flags, 0o600, dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise OSError("unsafe attribution log target")
        return descriptor, metadata
    except Exception:
        os.close(descriptor)
        raise


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _prepare_rotated_target(directory_fd: int) -> bool:
    try:
        opened = _open_regular_at(directory_fd, ROTATED_PATH.name, os.O_RDONLY)
        if opened is None:
            return True
        descriptor, _ = opened
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        return True
    except OSError:
        return False


def rotate_if_needed(
    directory_fd: int,
    active_metadata: os.stat_result,
    incoming_bytes: int,
) -> bool:
    """Rotate before an append would exceed the cap; fail closed on errors."""

    if incoming_bytes < 0 or incoming_bytes > MAX_BATCH_BYTES or incoming_bytes > MAX_BYTES:
        return False
    if active_metadata.st_size < MAX_BYTES and active_metadata.st_size + incoming_bytes <= MAX_BYTES:
        return True
    if not _prepare_rotated_target(directory_fd):
        return False
    try:
        os.replace(
            LOG_PATH.name,
            ROTATED_PATH.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        opened = _open_regular_at(directory_fd, ROTATED_PATH.name, os.O_RDONLY)
        if opened is None:
            return False
        descriptor, rotated_metadata = opened
        try:
            if not _same_file(active_metadata, rotated_metadata):
                return False
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        return True
    except OSError:
        return False


def _append_records(records: list[dict]) -> bool:
    if not records:
        return False
    try:
        encoded = "".join(
            json.dumps(record, ensure_ascii=False) + "\n" for record in records
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError, ValueError):
        return False
    if not encoded or len(encoded) > MAX_BATCH_BYTES or len(encoded) > MAX_BYTES:
        return False

    directory_fd = _open_log_directory()
    if directory_fd is None:
        return False
    active_descriptor: int | None = None
    try:
        if not _prepare_rotated_target(directory_fd):
            return False
        try:
            opened = _open_regular_at(
                directory_fd,
                LOG_PATH.name,
                os.O_WRONLY | os.O_APPEND,
                create=True,
            )
        except OSError:
            return False
        if opened is None:
            return False
        active_descriptor, active_metadata = opened
        try:
            os.fchmod(active_descriptor, 0o600)
        except OSError:
            return False

        needs_rotation = (
            active_metadata.st_size >= MAX_BYTES
            or active_metadata.st_size + len(encoded) > MAX_BYTES
        )
        if needs_rotation:
            os.close(active_descriptor)
            active_descriptor = None
            if not rotate_if_needed(directory_fd, active_metadata, len(encoded)):
                return False
            try:
                opened = _open_regular_at(
                    directory_fd,
                    LOG_PATH.name,
                    os.O_WRONLY | os.O_APPEND,
                    create=True,
                    exclusive=True,
                )
            except OSError:
                return False
            if opened is None:
                return False
            active_descriptor, active_metadata = opened
            try:
                os.fchmod(active_descriptor, 0o600)
            except OSError:
                return False

        current = os.fstat(active_descriptor)
        if not _same_file(active_metadata, current) or current.st_size + len(encoded) > MAX_BYTES:
            return False
        written = 0
        while written < len(encoded):
            count = os.write(active_descriptor, encoded[written:])
            if count <= 0:
                return False
            written += count
        return True
    except OSError:
        return False
    finally:
        if active_descriptor is not None:
            try:
                os.close(active_descriptor)
            except OSError:
                pass
        try:
            os.close(directory_fd)
        except OSError:
            pass


def _open_private_regular(path: Path) -> tuple[int, os.stat_result] | None:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        return None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | nofollow
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            os.close(descriptor)
            return None
        return descriptor, metadata
    except OSError:
        return None


def _acquire_authorization_lock() -> int | None:
    opened = _open_private_regular(AUTHORIZATION_LOCK_PATH)
    if opened is None:
        return None
    descriptor, _ = opened
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return descriptor
    except OSError:
        os.close(descriptor)
        return None


def _hmac_value(secret: bytes, value: str) -> str:
    return hmac.new(
        secret,
        value.encode("utf-8", errors="replace"),
        hashlib.sha256,
    ).hexdigest()


def _hmac_verifier(secret: bytes) -> str:
    return "v1:" + hmac.new(
        secret,
        HMAC_VERIFIER_DOMAIN.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _existing_hmac_key_readonly(connection: sqlite3.Connection) -> bytes:
    """Read Core's existing identity key without creating or migrating it."""

    opened = _open_private_regular(HMAC_SECRET_PATH)
    if opened is None:
        raise RuntimeError("document watch secret is unavailable")
    descriptor, _ = opened
    try:
        secret = os.read(descriptor, 33)
    finally:
        os.close(descriptor)
    if len(secret) != 32:
        raise RuntimeError("document watch secret has an invalid length")

    verifier_rows = connection.execute(
        "SELECT value FROM daemon_state WHERE key=?",
        (HMAC_VERIFIER_STATE_KEY,),
    ).fetchall()
    if len(verifier_rows) != 1 or not verifier_rows[0][0]:
        raise RuntimeError("document watch secret verifier is unavailable")
    stored_verifier = str(verifier_rows[0][0])
    if not hmac.compare_digest(stored_verifier, _hmac_verifier(secret)):
        raise RuntimeError("document watch secret verifier mismatch")

    legacy_rows = connection.execute(
        "SELECT value FROM daemon_state WHERE key=?",
        (LEGACY_HMAC_STATE_KEY,),
    ).fetchall()
    if len(legacy_rows) > 1:
        raise RuntimeError("legacy document watch secret is invalid")
    if legacy_rows:
        try:
            legacy = bytes.fromhex(str(legacy_rows[0][0]))
        except ValueError:
            raise RuntimeError("legacy document watch secret is invalid") from None
        if len(legacy) != 32 or not hmac.compare_digest(legacy, secret):
            raise RuntimeError("document watch secret mismatch")
        raise RuntimeError("document watch secret migration is pending")
    return secret


def _root_bindings_valid(
    secret: bytes,
    root_bindings: list[tuple[str, Path]],
) -> bool:
    if not root_bindings:
        return False
    for root_id, root in root_bindings:
        expected = "root_" + _hmac_value(secret, str(root))[:32]
        if not hmac.compare_digest(expected, root_id):
            return False
    return True


def _database_authorization_matches(
    revision: int,
    root_bindings: list[tuple[str, Path]],
) -> bool:
    descriptors: list[int] = []
    connection: sqlite3.Connection | None = None
    try:
        root_ids = {root_id for root_id, _ in root_bindings}
        if not root_bindings or len(root_ids) != len(root_bindings):
            return False
        for path in (DATABASE_PATH, DATABASE_WAL_PATH, DATABASE_SHM_PATH):
            opened = _open_private_regular(path)
            if opened is None:
                return False
            descriptor, _ = opened
            descriptors.append(descriptor)

        uri = f"{DATABASE_PATH.resolve(strict=False).as_uri()}?mode=ro&cache=private"
        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=SQLITE_TIMEOUT_SECONDS,
            isolation_level=None,
        )
        connection.execute(f"PRAGMA busy_timeout = {int(SQLITE_TIMEOUT_SECONDS * 1000)}")
        connection.execute("PRAGMA query_only = ON")
        query_only = connection.execute("PRAGMA query_only").fetchone()
        if query_only is None or int(query_only[0]) != 1:
            return False
        connection.execute("BEGIN")

        tracking_rows = connection.execute(
            "SELECT value FROM settings WHERE key='document_change_tracking_enabled'"
        ).fetchall()
        if len(tracking_rows) != 1 or str(tracking_rows[0][0]).lower() not in {
            "1",
            "true",
            "yes",
            "on",
        }:
            return False

        pause_rows = connection.execute(
            "SELECT value FROM daemon_state WHERE key='pause_mode'"
        ).fetchall()
        if len(pause_rows) > 1:
            return False
        if pause_rows and str(pause_rows[0][0] or "").strip():
            return False
        pause_until_rows = connection.execute(
            "SELECT value FROM daemon_state WHERE key='pause_until'"
        ).fetchall()
        if len(pause_until_rows) > 1:
            return False
        if pause_until_rows and str(pause_until_rows[0][0] or "").strip():
            return False

        revision_rows = connection.execute(
            "SELECT value FROM daemon_state "
            "WHERE key='document_change_root_authorization_revision'"
        ).fetchall()
        if len(revision_rows) > 1:
            return False
        if revision_rows:
            try:
                database_revision = int(str(revision_rows[0][0]))
            except (TypeError, ValueError):
                return False
            if database_revision < 0:
                return False
        else:
            database_revision = 0
        if database_revision != revision:
            return False

        enabled_rows = connection.execute(
            "SELECT root_id FROM file_watch_roots WHERE enabled=1"
        ).fetchall()
        enabled_root_ids = {str(row[0]) for row in enabled_rows}
        if len(enabled_rows) != len(enabled_root_ids) or enabled_root_ids != root_ids:
            return False
        secret = _existing_hmac_key_readonly(connection)
        return _root_bindings_valid(secret, root_bindings)
    except (OSError, RuntimeError, TypeError, ValueError, sqlite3.Error):
        return False
    finally:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass


def main(argv: list[str] | None = None, stdin: TextIO | None = None) -> None:
    arguments = list(sys.argv[1:] if argv is None else argv)
    source = "unknown"
    if "--source" in arguments:
        index = arguments.index("--source")
        if index + 1 < len(arguments):
            source = arguments[index + 1]
    safe_source = _safe_log_token(source, MAX_SOURCE_BYTES)
    if safe_source is None:
        return

    raw = _read_stdin_bounded(sys.stdin if stdin is None else stdin)
    if raw is None or not raw.strip():
        return
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict):
        return
    action = _safe_log_token(payload.get("tool_name"), MAX_ACTION_BYTES)
    session = _safe_log_token(payload.get("session_id"), MAX_SESSION_BYTES)
    if action is None or session is None:
        return

    authorization_lock = _acquire_authorization_lock()
    if authorization_lock is None:
        return
    try:
        authorization = _tracked_root_authorization()
        if authorization is None:
            return
        revision, root_bindings = authorization
        if not _database_authorization_matches(revision, root_bindings):
            return
        roots = [root for _, root in root_bindings]
        paths = _authorized_paths(payload, roots)
        secret = _identity_secret_readonly()
        if not paths or secret is None or not _root_bindings_valid(secret, root_bindings):
            return

        observed_at = datetime.now(timezone.utc).isoformat()
        records = []
        for path, operation in paths:
            identity = _identity_for_path(secret, path, root_bindings)
            if identity is None:
                continue
            root_id, file_id = identity
            records.append(
                {
                    "at": observed_at,
                    "tool": safe_source,
                    "action": action,
                    "operation": operation,
                    "root_id": root_id,
                    "file_id": file_id,
                }
            )
        _append_records(records)
    finally:
        try:
            fcntl.flock(authorization_lock, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(authorization_lock)
        except OSError:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Attribution is best-effort.  It must never interrupt a successful
        # editor operation; a missing line is safer than a blocked tool.
        pass
