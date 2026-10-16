#!/usr/bin/env -S -i PATH=/usr/bin:/bin:/usr/sbin:/sbin LANG=C LC_ALL=C /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 -I -S
"""Fail-closed PAM 0.4.0 local rollout orchestration.

The tool is dry-run by default.  It never deletes an App, Core, database, or
rollback artifact: cutover and rollback preserve every displaced directory by
same-volume rename.  Runtime configuration is supplied in a private JSON file
so machine-specific paths never become repository defaults.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import plistlib
import pwd
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import time
import unicodedata
import uuid
from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit


RELEASE_VERSION = "0.4.0"
ROLLBACK_VERSION = "0.3.22"
BASELINE_REVISION = "85706ef13da39fbd50b229b1435132cc34a51bb1"
PHASE01_REVISION = "4a7c2e54ced277f56f2d42ca07a6bfbeb5cfe03f"
PHASE2_REVISION = "475e336a46ae7bf96a8bd6e44915d3599e4f66f7"
PHASE01_ARCHIVE_SHA256 = "8f115cc1b1263d97caeb1e4e2b1a51248e4d45ce07c4a30199c46b9a64775c38"
PHASE01_PAYLOAD_TREE_SHA256 = "04dff9cadb515dd118767b669028b21b9c931793f26d4c579beb5aec3342fbdd"
PHASE2_ARCHIVE_SHA256 = "abff7c7a496e1d47604e941c304488183c4903202d1d60f09dd6feb253fbf11d"
PHASE2_PAYLOAD_TREE_SHA256 = "7c57c139d9d2189aedd8bade16e5ab838a46eff0237b042dbfe7613b7151c607"
SOURCE_SCHEMA = 28
TARGET_SCHEMA = 31
SERVICE_LABEL = "app.personalai.monitor.core"
RELEASE_BRANCH = "release/v0.4.0-context-console"
SYSTEM_EXECUTABLES = {
    "codesign": "/usr/bin/codesign",
    "csrutil": "/usr/bin/csrutil",
    "defaults": "/usr/bin/defaults",
    "ditto": "/usr/bin/ditto",
    "git": "/usr/bin/git",
    "launchctl": "/bin/launchctl",
    "lsof": "/usr/sbin/lsof",
    "open": "/usr/bin/open",
    "osascript": "/usr/bin/osascript",
    "pgrep": "/usr/bin/pgrep",
    "ps": "/bin/ps",
    "xattr": "/usr/bin/xattr",
}
SYSTEM_EXECUTION_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
LAUNCH_AGENT_PATH = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
ISOLATED_LAUNCHER_NAME = "pam_isolated_launcher.py"
ISOLATED_MODULE_BOOTSTRAP = (
    "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));"
    "runpy.run_module('activity_mvp',run_name='__main__',alter_sys=True)"
)
CORE_LAUNCH_AGENT_KEYS = {
    "Label",
    "ProgramArguments",
    "WorkingDirectory",
    "EnvironmentVariables",
    "RunAtLoad",
    "KeepAlive",
    "StandardOutPath",
    "StandardErrorPath",
}
SHADOW_LAUNCH_AGENT_KEYS = CORE_LAUNCH_AGENT_KEYS | {"ProcessType", "ThrottleInterval"}
CORE_OPTIONAL_ENV_KEYS = {
    "PAM_VLM_REVIEW_ENABLED",
    "PAM_VLM_MODEL",
    "PAM_VLM_ENDPOINT",
    "PAM_VLM_TIMEOUT_SECONDS",
    "PAM_VLM_CACHE_TTL_SECONDS",
    "PAM_VLM_KEEP_ALIVE",
    "PAM_VLM_CANARY_ENABLED",
    "PAM_VLM_MAX_IMAGE_EDGE",
    "PAM_DOCUMENT_CHANGE_STARTUP_RECONCILE_ENABLED",
}
PYTHON_LAUNCHER_IDENTIFIER = "python3"
PYTHON_PROCESS_IDENTIFIER = "org.python.python"
PYTHON_TEAM_IDENTIFIER = "BMM5U3QVKW"
PYTHON_ADMIN_GROUP_ID = 80
PYTHON_TRUSTED_GROUP_IDS = (0, PYTHON_ADMIN_GROUP_ID)
PYTHON_SIGNING_AUTHORITY = (
    "Developer ID Application: Python Software Foundation (BMM5U3QVKW)"
)
PYTHON_SIGNING_AUTHORITY_CHAIN = (
    PYTHON_SIGNING_AUTHORITY,
    "Developer ID Certification Authority",
    "Apple Root CA",
)
SHADOW_CONFIRMATION = "CREATE_SCHEMA31_SHADOW"
INSTALL_CONFIRMATION = "INSTALL_PAM_V040"
ROLLBACK_CONFIRMATION = "ROLLBACK_PAM_V0322"
APP_DEFAULTS_DOMAIN = "app.personalai.monitor"
PHASE2_UI_FLAG = "context_console_phase2_ui_enabled"
STATE_FILENAME = "phase3-rollout-state.json"
CONTEXT_CORE_GATE = "context_console_phase1_enabled"
PERMANENT_SAFETY_GATES = (
    "ha_auto_package_enabled",
    "context_memory_writer_enabled",
    "context_source_revocation_cascade_ready",
)
INITIAL_DISABLED_GATES = (CONTEXT_CORE_GATE, *PERMANENT_SAFETY_GATES)
# Backward-compatible name used by schema fixture tests.
REQUIRED_GATES = INITIAL_DISABLED_GATES
REQUIRED_SCHEMA31_TABLES = (
    "context_activity_confirmation_links",
    "context_bundle_manifests",
    "context_bundle_members",
    "context_candidate_evidence",
    "context_candidates",
    "context_component_baselines",
    "context_deliveries",
    "context_delivery_receipts",
    "context_document_mappings",
    "context_document_source_versions",
    "context_document_tombstones",
    "context_evidence",
    "context_external_receipt_bindings",
    "context_ingest_receipts",
    "context_label_registry",
    "context_memory_candidates",
    "context_observation_evidence_links",
    "context_observations",
    "context_principals",
    "context_product_experiment_attempts",
    "context_product_experiments",
    "context_product_feedback",
    "context_product_wins",
    "context_search_index",
    "context_source_event_tombstones",
    "context_source_events",
    "context_target_policies",
    "context_verified",
    "context_withdrawal_receipts",
)
CONFIG_FIELDS = {
    "source_root",
    "candidate_app",
    "live_core",
    "live_database",
    "installed_app",
    "launch_agent_plist",
    "workspace",
    "expected_revision",
    "expected_source_tree_sha256",
    "expected_core_code_sha256",
    "expected_app_payload_sha256",
    "expected_app_executable_sha256",
    "expected_app_cdhash",
    "expected_app_signature_kind",
    "expected_app_team_identifier",
    "gh_executable",
    "expected_gh_sha256",
    "service_label",
    "shadow_launch_agent_label",
    "shadow_launch_agent_plist",
    "expected_privacy_profile",
    "github_repository",
    "github_actions_run_id",
    "release_branch",
    "release_candidate_tag",
}
REQUIRED_CONFIG_FIELDS = CONFIG_FIELDS - {"service_label"}
RUNTIME_DATA_EXCLUDES = {
    "activity.sqlite",
    "activity.sqlite-wal",
    "activity.sqlite-shm",
    "daemon.lock",
    "dashboard.pid",
}
STORAGE_OPERATION_JOURNAL_BYTES = 4 * 1024 * 1024
STORAGE_RELEASE_MINIMUM_RESERVE_BYTES = 1024 * 1024 * 1024
STORAGE_DATABASE_GROWTH_NUMERATOR = 3
STORAGE_DATABASE_GROWTH_DENOMINATOR = 2
STORAGE_DATABASE_MINIMUM_BYTES = 1024 * 1024
SHADOW_COMPATIBLE_SCHEMAS = {RELEASE_VERSION: frozenset({29, 30, 31})}


class RolloutError(RuntimeError):
    """One content-free fail-closed rollout error."""


class _RollbackPreMutationAbort(BaseException):
    """Escape the forward-recovery handler before any release mutation."""

    def __init__(self, error: RolloutError):
        super().__init__(str(error))
        self.error = error


@dataclass(frozen=True)
class RolloutConfig:
    source_root: Path
    candidate_app: Path
    live_core: Path
    live_database: Path
    installed_app: Path
    launch_agent_plist: Path
    workspace: Path
    expected_revision: str
    expected_source_tree_sha256: str
    expected_core_code_sha256: str
    expected_app_payload_sha256: str = ""
    expected_app_executable_sha256: str = ""
    expected_app_cdhash: str = ""
    expected_app_signature_kind: str = "adhoc"
    expected_app_team_identifier: str = "not set"
    gh_executable: Path = Path("/invalid/gh")
    expected_gh_sha256: str = ""
    service_label: str = SERVICE_LABEL
    shadow_launch_agent_label: str = ""
    shadow_launch_agent_plist: Path = Path("/invalid/shadow.plist")
    expected_privacy_profile: str = "personal_high_precision"
    github_repository: str = ""
    github_actions_run_id: int = 0
    release_branch: str = RELEASE_BRANCH
    release_candidate_tag: str = ""

    @property
    def state_path(self) -> Path:
        return self.workspace / STATE_FILENAME


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RolloutError("config_not_regular_file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RolloutError("config_unreadable") from exc
    if not isinstance(payload, dict):
        raise RolloutError("config_not_object")
    return payload


def _validated_path(value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise RolloutError(f"invalid_{field}")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise RolloutError(f"relative_{field}_forbidden")
    normalized = Path(os.path.normpath(str(path)))
    forbidden = {Path("/"), Path.home(), Path.home().parent}
    if normalized in forbidden or len(normalized.parts) < 3:
        raise RolloutError(f"broad_{field}_forbidden")
    return normalized


def load_config(path: Path) -> RolloutConfig:
    if not path.is_absolute():
        raise RolloutError("relative_config_forbidden")
    raw, metadata = _read_stable_nofollow_file(
        path,
        max_bytes=1024 * 1024,
        error="config_not_regular_file",
    )
    if (
        stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
    ):
        raise RolloutError("config_permissions_invalid")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RolloutError("config_unreadable") from exc
    if not isinstance(payload, dict):
        raise RolloutError("config_not_object")
    unknown = set(payload) - CONFIG_FIELDS
    missing = REQUIRED_CONFIG_FIELDS - set(payload)
    if unknown or missing:
        raise RolloutError("config_fields_invalid")
    expected_revision = str(payload["expected_revision"] or "")
    expected_tree = str(payload["expected_source_tree_sha256"] or "")
    expected_core = str(payload["expected_core_code_sha256"] or "")
    expected_app_payload = str(payload["expected_app_payload_sha256"] or "")
    expected_app_executable = str(payload["expected_app_executable_sha256"] or "")
    expected_app_cdhash = str(payload["expected_app_cdhash"] or "")
    expected_app_signature_kind = str(payload["expected_app_signature_kind"] or "")
    expected_app_team_identifier = str(payload["expected_app_team_identifier"] or "")
    expected_gh = str(payload["expected_gh_sha256"] or "")
    if not re.fullmatch(r"[0-9a-f]{40}", expected_revision):
        raise RolloutError("expected_revision_invalid")
    for value, name in (
        (expected_tree, "expected_source_tree_sha256"),
        (expected_core, "expected_core_code_sha256"),
        (expected_app_payload, "expected_app_payload_sha256"),
        (expected_app_executable, "expected_app_executable_sha256"),
        (expected_gh, "expected_gh_sha256"),
    ):
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise RolloutError(f"{name}_invalid")
    if re.fullmatch(r"[0-9a-f]{40,64}", expected_app_cdhash) is None:
        raise RolloutError("expected_app_cdhash_invalid")
    if expected_app_signature_kind != "adhoc" or expected_app_team_identifier != "not set":
        raise RolloutError("expected_app_signing_identity_invalid")
    label = str(payload.get("service_label") or SERVICE_LABEL)
    if label != SERVICE_LABEL:
        raise RolloutError("service_label_invalid")
    shadow_label = str(payload["shadow_launch_agent_label"] or "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{3,127}", shadow_label) or shadow_label == SERVICE_LABEL:
        raise RolloutError("shadow_launch_agent_label_invalid")
    privacy_profile = str(payload["expected_privacy_profile"] or "")
    if privacy_profile not in {"strict", "personal_high_precision"}:
        raise RolloutError("expected_privacy_profile_invalid")
    github_repository = str(payload["github_repository"] or "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", github_repository):
        raise RolloutError("github_repository_invalid")
    run_id_value = payload["github_actions_run_id"]
    if isinstance(run_id_value, bool):
        raise RolloutError("github_actions_run_id_invalid")
    try:
        github_actions_run_id = int(run_id_value)
    except (TypeError, ValueError) as exc:
        raise RolloutError("github_actions_run_id_invalid") from exc
    if github_actions_run_id <= 0:
        raise RolloutError("github_actions_run_id_invalid")
    release_branch = str(payload["release_branch"] or "")
    if release_branch != RELEASE_BRANCH:
        raise RolloutError("release_branch_invalid")
    release_candidate_tag = str(payload["release_candidate_tag"] or "")
    if not re.fullmatch(r"v0\.4\.0-rc\.[1-9][0-9]*", release_candidate_tag):
        raise RolloutError("release_candidate_tag_invalid")
    config = RolloutConfig(
        source_root=_validated_path(payload["source_root"], "source_root"),
        candidate_app=_validated_path(payload["candidate_app"], "candidate_app"),
        live_core=_validated_path(payload["live_core"], "live_core"),
        live_database=_validated_path(payload["live_database"], "live_database"),
        installed_app=_validated_path(payload["installed_app"], "installed_app"),
        launch_agent_plist=_validated_path(payload["launch_agent_plist"], "launch_agent_plist"),
        workspace=_validated_path(payload["workspace"], "workspace"),
        expected_revision=expected_revision,
        expected_source_tree_sha256=expected_tree,
        expected_core_code_sha256=expected_core,
        expected_app_payload_sha256=expected_app_payload,
        expected_app_executable_sha256=expected_app_executable,
        expected_app_cdhash=expected_app_cdhash,
        expected_app_signature_kind=expected_app_signature_kind,
        expected_app_team_identifier=expected_app_team_identifier,
        gh_executable=_validated_path(payload["gh_executable"], "gh_executable"),
        expected_gh_sha256=expected_gh,
        service_label=label,
        shadow_launch_agent_label=shadow_label,
        shadow_launch_agent_plist=_validated_path(
            payload["shadow_launch_agent_plist"],
            "shadow_launch_agent_plist",
        ),
        expected_privacy_profile=privacy_profile,
        github_repository=github_repository,
        github_actions_run_id=github_actions_run_id,
        release_branch=release_branch,
        release_candidate_tag=release_candidate_tag,
    )
    # Loading the recovery configuration must remain possible when the current
    # release has already corrupted or replaced its canonical data path.  The
    # lexical relationship is still fixed here; rollback performs the stronger
    # identity checks and quarantines the whole release Core without following
    # that path.
    _validate_path_relationships(config, allow_invalid_live_database=True)
    return config


def _resolved_without_symlink(path: Path, field: str, *, must_exist: bool = True) -> Path:
    if must_exist and not path.exists():
        raise RolloutError(f"{field}_missing")
    cursor = path
    while True:
        if cursor.exists() and cursor.is_symlink():
            raise RolloutError(f"{field}_symlink_forbidden")
        if cursor == cursor.parent:
            break
        cursor = cursor.parent
    return path.resolve(strict=must_exist)


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _validate_path_relationships(
    config: RolloutConfig,
    *,
    allow_invalid_live_database: bool = False,
) -> None:
    source = config.source_root.resolve(strict=False)
    candidate = config.candidate_app.resolve(strict=False)
    live_core = config.live_core.resolve(strict=False)
    live_db = (
        Path(os.path.abspath(config.live_database))
        if allow_invalid_live_database
        else config.live_database.resolve(strict=False)
    )
    installed = config.installed_app.resolve(strict=False)
    workspace = config.workspace.resolve(strict=False)
    core_plist = config.launch_agent_plist.resolve(strict=False)
    shadow_plist = config.shadow_launch_agent_plist.resolve(strict=False)

    # The built App may live below the clean release worktree.  No other
    # source/candidate/live/install relationship is safe: each of those trees
    # is copied or renamed independently during cutover and compensation.
    if source in candidate.parents:
        pass
    elif source == candidate:
        raise RolloutError("candidate_app_must_not_equal_source_root")
    elif candidate in source.parents:
        raise RolloutError("source_inside_candidate_forbidden")
    for left_name, left, right_name, right in (
        ("source_root", source, "live_core", live_core),
        ("source_root", source, "installed_app", installed),
        ("candidate_app", candidate, "live_core", live_core),
        ("candidate_app", candidate, "installed_app", installed),
        ("live_core", live_core, "installed_app", installed),
    ):
        if _paths_overlap(left, right):
            raise RolloutError(f"{left_name}_{right_name}_overlap_forbidden")

    expected_live_db_path = config.live_core / "data" / "activity.sqlite"
    expected_live_db = live_core / "data" / "activity.sqlite"
    if config.live_database != expected_live_db_path or (
        not allow_invalid_live_database and live_db != expected_live_db
    ):
        raise RolloutError("live_database_not_canonical")
    if shadow_plist == core_plist:
        raise RolloutError("shadow_and_core_launch_agents_must_differ")
    rename_trees = (candidate, live_core, installed)
    for plist_name, plist in (
        ("launch_agent_plist", core_plist),
        ("shadow_launch_agent_plist", shadow_plist),
    ):
        if any(_paths_overlap(plist, tree) for tree in rename_trees):
            raise RolloutError(f"{plist_name}_inside_rename_tree")

    protected = (source, candidate, live_core, installed, core_plist, shadow_plist)
    if any(workspace == item or item in workspace.parents or workspace in item.parents for item in protected):
        raise RolloutError("workspace_overlaps_protected_path")


def _trusted_user_home() -> str:
    try:
        record = pwd.getpwuid(os.getuid())
        home = Path(record.pw_dir)
        metadata = os.lstat(home)
    except (KeyError, OSError) as exc:
        raise RolloutError("trusted_user_home_unavailable") from exc
    if (
        not home.is_absolute()
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or home.resolve(strict=True) != home
    ):
        raise RolloutError("trusted_user_home_invalid")
    return str(home)


def _minimal_command_environment(*, include_home: bool = False) -> dict[str, str]:
    environment = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": SYSTEM_EXECUTION_PATH,
        "DEVELOPER_DIR": "/Library/Developer/CommandLineTools",
    }
    if include_home:
        environment["HOME"] = _trusted_user_home()
    return environment


def _system_command(command: list[str]) -> list[str]:
    if not command or not isinstance(command[0], str):
        raise RolloutError("external_command_invalid")
    executable = SYSTEM_EXECUTABLES.get(command[0])
    if executable is None:
        raise RolloutError("external_command_not_allowlisted")
    path = Path(executable)
    try:
        metadata = path.stat()
    except OSError as exc:
        raise RolloutError("system_executable_unavailable") from exc
    if not path.is_absolute() or path.is_symlink() or not path.is_file() or not os.access(path, os.X_OK):
        raise RolloutError("system_executable_untrusted")
    if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
        raise RolloutError("system_executable_untrusted")
    return [executable, *command[1:]]


def _run(command: list[str], *, cwd: Path | None = None, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            _system_command(command),
            cwd=cwd,
            env=_minimal_command_environment(include_home=command[0] == "defaults"),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        raise RolloutError("required_command_unavailable") from exc


def _run_bytes(
    command: list[str],
    *,
    cwd: Path | None = None,
    input_bytes: bytes | None = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            _system_command(command),
            cwd=cwd,
            env=_minimal_command_environment(),
            input=input_bytes,
            check=False,
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RolloutError("required_command_unavailable") from exc


def _trusted_tool_snapshot(path: Path, expected_sha256: str) -> dict[str, Any]:
    if not path.is_absolute() or path.name != "gh" or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise RolloutError("gh_executable_identity_invalid")
    cursor = Path("/")
    pending = list(path.parts[1:])
    symlinks_seen: set[tuple[int, int]] = set()
    chain: list[tuple[Any, ...]] = []
    try:
        while pending:
            component = pending.pop(0)
            candidate = cursor / component
            metadata = os.lstat(candidate)
            mode = stat.S_IMODE(metadata.st_mode)
            chain.append(
                (
                    str(candidate),
                    int(metadata.st_dev),
                    int(metadata.st_ino),
                    int(metadata.st_uid),
                    int(metadata.st_gid),
                    mode,
                    int(metadata.st_mtime_ns),
                )
            )
            if stat.S_ISLNK(metadata.st_mode):
                identity = (int(metadata.st_dev), int(metadata.st_ino))
                if identity in symlinks_seen or len(symlinks_seen) >= 40:
                    raise RolloutError("gh_executable_identity_invalid")
                symlinks_seen.add(identity)
                target = Path(os.readlink(candidate))
                if target.is_absolute():
                    cursor = Path("/")
                    pending = list(target.parts[1:]) + pending
                else:
                    pending = list(target.parts) + pending
                continue
            if pending:
                if not stat.S_ISDIR(metadata.st_mode) or mode & 0o002:
                    raise RolloutError("gh_executable_identity_invalid")
                cursor = candidate
                continue
            if not stat.S_ISREG(metadata.st_mode) or not os.access(candidate, os.X_OK):
                raise RolloutError("gh_executable_identity_invalid")
            resolved = candidate
    except OSError as exc:
        raise RolloutError("gh_executable_identity_invalid") from exc
    if "resolved" not in locals() or _sha256_file(resolved) != expected_sha256:
        raise RolloutError("gh_executable_hash_mismatch")
    final = resolved.stat()
    return {
        "configured_path": str(path),
        "resolved_path": str(resolved),
        "device": int(final.st_dev),
        "inode": int(final.st_ino),
        "size": int(final.st_size),
        "mtime_ns": int(final.st_mtime_ns),
        "sha256": expected_sha256,
        "chain": chain,
    }


def _gh_run(
    config: RolloutConfig,
    arguments: list[str],
    *,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    before = _trusted_tool_snapshot(config.gh_executable, config.expected_gh_sha256)
    try:
        completed = subprocess.run(
            [str(before["resolved_path"]), *arguments],
            env=_minimal_command_environment(include_home=True),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        raise RolloutError("github_cli_unavailable") from exc
    after = _trusted_tool_snapshot(config.gh_executable, config.expected_gh_sha256)
    if after != before:
        raise RolloutError("gh_executable_identity_changed")
    return completed


def _git_output(config: RolloutConfig, *args: str) -> str:
    completed = _run(["git", *args], cwd=config.source_root)
    if completed.returncode != 0:
        raise RolloutError("git_check_failed")
    return completed.stdout.strip()


def _assert_git_repository_controls(config: RolloutConfig) -> None:
    if _git_output(config, "for-each-ref", "--format=%(refname)", "refs/replace"):
        raise RolloutError("git_replace_refs_forbidden")
    if _git_output(config, "ls-files", "-u", "-z"):
        raise RolloutError("git_unmerged_index_forbidden")
    grafts_value = _git_output(config, "rev-parse", "--git-path", "info/grafts")
    grafts = Path(grafts_value)
    if not grafts.is_absolute():
        grafts = config.source_root / grafts
    if grafts.exists() or grafts.is_symlink():
        raise RolloutError("git_grafts_forbidden")


def _verify_release_commit_chain(config: RolloutConfig, revision: str) -> dict[str, Any]:
    if _git_output(config, "merge-base", BASELINE_REVISION, revision) != BASELINE_REVISION:
        raise RolloutError("release_baseline_mismatch")
    commit_chain = _git_output(
        config,
        "rev-list",
        "--reverse",
        f"{BASELINE_REVISION}..{revision}",
    ).splitlines()
    first_parent_chain = _git_output(
        config,
        "rev-list",
        "--first-parent",
        "--reverse",
        f"{BASELINE_REVISION}..{revision}",
    ).splitlines()
    if (
        len(commit_chain) != 4
        or commit_chain != first_parent_chain
        or commit_chain[:2] != [PHASE01_REVISION, PHASE2_REVISION]
        or commit_chain[-1] != revision
    ):
        raise RolloutError("release_four_commit_chain_mismatch")
    expected_parent = BASELINE_REVISION
    for commit in commit_chain:
        row = _git_output(config, "rev-list", "--parents", "-n", "1", commit).split()
        if row != [commit, expected_parent]:
            raise RolloutError("release_non_linear_or_merge_commit")
        expected_parent = commit
    return {
        "baseline_revision": BASELINE_REVISION,
        "commit_chain": commit_chain,
        "phase01_archive_evidence": {
            "commit": PHASE01_REVISION,
            "zip_sha256": PHASE01_ARCHIVE_SHA256,
            "payload_tree_sha256": PHASE01_PAYLOAD_TREE_SHA256,
            "verification_scope": "prior_independent_archive_audit",
        },
        "phase2_archive_evidence": {
            "commit": PHASE2_REVISION,
            "zip_sha256": PHASE2_ARCHIVE_SHA256,
            "payload_tree_sha256": PHASE2_PAYLOAD_TREE_SHA256,
            "verification_scope": "prior_independent_archive_audit",
        },
    }


def _verify_remote_ci(config: RolloutConfig) -> dict[str, Any]:
    origin = _git_output(config, "remote", "get-url", "origin")
    origin_match = re.search(
        r"(?:github\.com[:/])([^/\s:]+/[^/\s]+?)(?:\.git)?$",
        origin,
        flags=re.IGNORECASE,
    )
    if not origin_match or origin_match.group(1).lower() != config.github_repository.lower():
        raise RolloutError("github_repository_origin_mismatch")
    local_tag = _git_output(config, "rev-parse", f"{config.release_candidate_tag}^{{commit}}")
    if local_tag != config.expected_revision:
        raise RolloutError("local_release_candidate_tag_mismatch")

    repository = _gh_run(
        config,
        ["api", f"repos/{config.github_repository}"],
        timeout=30.0,
    )
    branch = _gh_run(
        config,
        [
            "api",
            f"repos/{config.github_repository}/git/ref/heads/{config.release_branch}",
        ],
        timeout=30.0,
    )
    tag = _gh_run(
        config,
        [
            "api",
            f"repos/{config.github_repository}/git/ref/tags/{config.release_candidate_tag}",
        ],
        timeout=30.0,
    )
    run = _gh_run(
        config,
        [
            "api",
            f"repos/{config.github_repository}/actions/runs/{config.github_actions_run_id}",
        ],
        timeout=30.0,
    )
    jobs = _gh_run(
        config,
        [
            "api",
            f"repos/{config.github_repository}/actions/runs/{config.github_actions_run_id}/jobs?per_page=100",
        ],
        timeout=30.0,
    )
    if any(
        item.returncode != 0
        for item in (repository, branch, tag, run, jobs)
    ):
        raise RolloutError("github_ci_attestation_unavailable")
    try:
        repository_payload = json.loads(repository.stdout)
        branch_payload = json.loads(branch.stdout)
        tag_payload = json.loads(tag.stdout)
        run_payload = json.loads(run.stdout)
        jobs_payload = json.loads(jobs.stdout)
    except json.JSONDecodeError as exc:
        raise RolloutError("github_ci_attestation_invalid") from exc
    if (
        not isinstance(repository_payload, dict)
        or repository_payload.get("private") is not True
        or str(repository_payload.get("full_name") or "").lower()
        != config.github_repository.lower()
    ):
        raise RolloutError("github_repository_not_private")
    branch_object = branch_payload.get("object") if isinstance(branch_payload, dict) else None
    if (
        not isinstance(branch_object, dict)
        or branch_object.get("type") != "commit"
        or branch_object.get("sha") != config.expected_revision
    ):
        raise RolloutError("remote_release_branch_mismatch")
    tag_object = tag_payload.get("object") if isinstance(tag_payload, dict) else None
    if not isinstance(tag_object, dict):
        raise RolloutError("remote_release_candidate_tag_mismatch")
    tag_type = tag_object.get("type")
    tag_sha = tag_object.get("sha")
    if tag_type == "tag" and isinstance(tag_sha, str):
        annotated = _gh_run(
            config,
            ["api", f"repos/{config.github_repository}/git/tags/{tag_sha}"],
            timeout=30.0,
        )
        if annotated.returncode != 0:
            raise RolloutError("github_ci_attestation_unavailable")
        try:
            annotated_payload = json.loads(annotated.stdout)
        except json.JSONDecodeError as exc:
            raise RolloutError("github_ci_attestation_invalid") from exc
        annotated_object = (
            annotated_payload.get("object") if isinstance(annotated_payload, dict) else None
        )
        if not isinstance(annotated_object, dict):
            raise RolloutError("remote_release_candidate_tag_mismatch")
        tag_type = annotated_object.get("type")
        tag_sha = annotated_object.get("sha")
    if tag_type != "commit" or tag_sha != config.expected_revision:
        raise RolloutError("remote_release_candidate_tag_mismatch")
    if (
        not isinstance(run_payload, dict)
        or run_payload.get("head_sha") != config.expected_revision
        or run_payload.get("head_branch") != config.release_branch
        or run_payload.get("path") != ".github/workflows/ci.yml"
        or run_payload.get("event") != "push"
        or run_payload.get("status") != "completed"
        or run_payload.get("conclusion") != "success"
    ):
        raise RolloutError("github_ci_run_mismatch")
    job_rows = jobs_payload.get("jobs") if isinstance(jobs_payload, dict) else None
    if not isinstance(job_rows, list) or not job_rows:
        raise RolloutError("github_ci_jobs_missing")
    if any(
        not isinstance(job, dict)
        or job.get("status") != "completed"
        or job.get("conclusion") != "success"
        for job in job_rows
    ):
        raise RolloutError("github_ci_job_failed")
    names = {str(job.get("name") or "") for job in job_rows}
    required_names = {
        "core",
        "Swift tests (Xcode 15.4)",
        "Swift tests (Xcode 16.4)",
        "app-build",
    }
    if not required_names.issubset(names):
        raise RolloutError("github_ci_required_job_missing")
    return {
        "run_id": config.github_actions_run_id,
        "head_sha": config.expected_revision,
        "head_branch": config.release_branch,
        "workflow_path": ".github/workflows/ci.yml",
        "release_candidate_tag": config.release_candidate_tag,
        "required_jobs": sorted(required_names),
        "conclusion": "success",
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_tree_hash(config: RolloutConfig) -> str:
    listing = _git_output(
        config,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        config.expected_revision,
    )
    entries: list[tuple[str, str]] = []
    portable_seen: set[str] = set()
    for raw in listing.split("\0"):
        if not raw:
            continue
        match = re.fullmatch(r"(100644|100755) blob ([0-9a-f]{40})\t(.+)", raw)
        if match is None:
            raise RolloutError("source_git_manifest_invalid")
        name = match.group(3)
        path = Path(name)
        portable = unicodedata.normalize("NFC", name).casefold()
        if (
            path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
            or unicodedata.normalize("NFC", name) != name
            or portable in portable_seen
            or any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in name)
        ):
            raise RolloutError("source_git_manifest_invalid")
        portable_seen.add(portable)
        entries.append((name, match.group(2)))
    if not entries:
        raise RolloutError("source_git_manifest_invalid")
    flags = _git_output(config, "ls-files", "-v", "-z")
    indexed: set[str] = set()
    for row in flags.split("\0"):
        if not row:
            continue
        if len(row) < 3 or row[:2] != "H ":
            raise RolloutError("git_index_flags_unsafe")
        indexed.add(row[2:])
    if indexed != {name for name, _object_id in entries}:
        raise RolloutError("git_index_manifest_mismatch")
    digest = hashlib.sha256()
    for relative, object_id in sorted(entries, key=lambda item: item[0].encode("utf-8")):
        payload = _git_blob(config, object_id)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(payload).hexdigest().encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _core_code_hash(core_root: Path) -> str:
    candidates: list[Path] = [
        core_root / "pyproject.toml",
        core_root / ISOLATED_LAUNCHER_NAME,
    ]
    for relative in ("activity_mvp", "tools", "browser_extension", "evolution"):
        candidates.extend(
            path
            for path in core_root.joinpath(relative).rglob("*")
            if path.is_file() and path.name not in {".DS_Store"} and path.suffix != ".pyc"
        )
    digest = hashlib.sha256()
    selected = {item for item in candidates if item.is_file()}
    for path in sorted(
        selected,
        key=lambda item: item.relative_to(core_root).as_posix().encode("utf-8"),
    ):
        # The physical staging directory includes a per-run basename.  Core
        # provenance is a logical source identity and must therefore use the
        # same stable `core/` projection as the Git/build and live-runtime
        # hashers, regardless of where the candidate is materialized.
        relative = f"core/{path.relative_to(core_root).as_posix()}"
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256_file(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


@dataclass(frozen=True)
class GitCoreBlob:
    relative_path: str
    object_id: str
    mode: int
    size: int
    sha256: str


def _git_blob(config: RolloutConfig, object_id: str) -> bytes:
    if re.fullmatch(r"[0-9a-f]{40}", object_id) is None:
        raise RolloutError("git_core_object_invalid")
    completed = _run_bytes(
        ["git", "cat-file", "blob", object_id],
        cwd=config.source_root,
        timeout=30.0,
    )
    if completed.returncode != 0:
        raise RolloutError("git_core_object_unreadable")
    payload = completed.stdout
    canonical = hashlib.sha1(f"blob {len(payload)}\0".encode("ascii") + payload).hexdigest()
    if canonical != object_id:
        raise RolloutError("git_core_object_hash_mismatch")
    return payload


def _git_core_manifest(config: RolloutConfig) -> tuple[GitCoreBlob, ...]:
    object_format = _git_output(config, "rev-parse", "--show-object-format")
    if object_format != "sha1":
        raise RolloutError("git_object_format_unsupported")
    listing = _git_output(
        config,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        config.expected_revision,
        "--",
        "core",
    )
    rows: list[GitCoreBlob] = []
    seen: set[str] = set()
    portable_seen: set[str] = set()
    for raw in listing.split("\0"):
        if not raw:
            continue
        match = re.fullmatch(r"(100644|100755) blob ([0-9a-f]{40})\t(core/(.+))", raw)
        if match is None:
            raise RolloutError("git_core_manifest_invalid")
        relative = match.group(4)
        path = Path(relative)
        portable = unicodedata.normalize("NFC", relative).casefold()
        if (
            path.is_absolute()
            or relative in seen
            or portable in portable_seen
            or unicodedata.normalize("NFC", relative) != relative
            or any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in relative)
            or len(relative.encode("utf-8")) > 1024
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.parts[0] == "data"
            or relative == "AGENTS.md"
        ):
            raise RolloutError("git_core_manifest_invalid")
        seen.add(relative)
        portable_seen.add(portable)
        payload = _git_blob(config, match.group(2))
        rows.append(
            GitCoreBlob(
                relative_path=relative,
                object_id=match.group(2),
                mode=0o755 if match.group(1) == "100755" else 0o644,
                size=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            )
        )
    rows.sort(key=lambda item: item.relative_path.encode("utf-8"))
    if not rows:
        raise RolloutError("git_core_manifest_empty")
    flags = _git_output(config, "ls-files", "-v", "-z", "--", "core")
    tracked: set[str] = set()
    for row in flags.split("\0"):
        if not row:
            continue
        if len(row) < 3 or row[0] != "H" or row[1] != " ":
            raise RolloutError("git_core_index_flags_unsafe")
        name = row[2:]
        if not name.startswith("core/") or name[5:] in tracked:
            raise RolloutError("git_core_index_invalid")
        tracked.add(name[5:])
    if tracked != {item.relative_path for item in rows}:
        raise RolloutError("git_core_index_manifest_mismatch")
    return tuple(rows)


def _git_core_code_hash(
    config: RolloutConfig,
    manifest: tuple[GitCoreBlob, ...],
) -> str:
    digest = hashlib.sha256()
    for item in manifest:
        path = item.relative_path
        if path in {"pyproject.toml", ISOLATED_LAUNCHER_NAME} or path.startswith(
            ("activity_mvp/", "tools/", "browser_extension/", "evolution/")
        ):
            digest.update(f"core/{path}".encode("utf-8"))
            digest.update(b"\0")
            digest.update(item.sha256.encode("ascii"))
            digest.update(b"\n")
    return digest.hexdigest()


def _git_core_manifest_identity(
    config: RolloutConfig,
    manifest: tuple[GitCoreBlob, ...],
) -> dict[str, Any]:
    tree_object = _git_output(config, "rev-parse", f"{config.expected_revision}:core")
    if re.fullmatch(r"[0-9a-f]{40}", tree_object) is None:
        raise RolloutError("git_core_tree_object_invalid")
    digest = hashlib.sha256()
    content_mode_digest = hashlib.sha256()
    directory_names = {"."}
    for item in manifest:
        parent = Path(item.relative_path).parent
        while parent != Path("."):
            directory_names.add(parent.as_posix())
            parent = parent.parent
    content_rows: list[tuple[str, int, int, str]] = [
        (f"d:{name}", 0o755, 0, "") for name in directory_names
    ]
    content_rows.extend(
        (f"f:{item.relative_path}", item.mode, item.size, item.sha256)
        for item in manifest
    )
    total_bytes = 0
    for item in manifest:
        digest.update(
            json.dumps(
                [
                    item.relative_path,
                    item.object_id,
                    item.mode,
                    item.size,
                    item.sha256,
                ],
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        )
        digest.update(b"\n")
        total_bytes += item.size
    for row in sorted(content_rows, key=lambda item: item[0].encode("utf-8")):
        content_mode_digest.update(
            json.dumps(row, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        )
        content_mode_digest.update(b"\n")
    return {
        "revision": config.expected_revision,
        "core_tree_object": tree_object,
        "manifest_sha256": digest.hexdigest(),
        "content_mode_manifest_sha256": content_mode_digest.hexdigest(),
        "entry_count": len(content_rows),
        "logical_bytes": total_bytes,
        "core_code_sha256": _git_core_code_hash(config, manifest),
    }


def _materialized_core_identity(
    core_root: Path,
    *,
    exclude_runtime_agents: bool = False,
) -> dict[str, Any]:
    if core_root.is_symlink() or not core_root.is_dir():
        raise RolloutError("staged_git_core_invalid")
    rows: list[tuple[str, int, int, str]] = []
    for directory, directories, files in os.walk(core_root, topdown=True, followlinks=False):
        base = Path(directory)
        if base == core_root:
            directories[:] = [name for name in directories if name != "data"]
            if "__pycache__" in directories or any(
                name in sys.stdlib_module_names for name in directories
            ):
                raise RolloutError("runtime_core_root_import_shadow")
        directories[:] = sorted(directories)
        relative_base = base.relative_to(core_root)
        directory_name = "." if relative_base == Path(".") else relative_base.as_posix()
        directory_mode = _mode(base)
        if directory_mode != 0o755:
            raise RolloutError("staged_git_core_directory_permissions_invalid")
        rows.append((f"d:{directory_name}", directory_mode, 0, ""))
        for name in directories:
            child = base / name
            if child.is_symlink() or not child.is_dir():
                raise RolloutError("staged_git_core_special_entry")
        for name in sorted(files):
            path = base / name
            if exclude_runtime_agents and base == core_root and name == "AGENTS.md":
                continue
            if path.is_symlink() or not path.is_file():
                raise RolloutError("staged_git_core_special_entry")
            metadata = path.stat()
            file_mode = stat.S_IMODE(metadata.st_mode)
            if file_mode & 0o022:
                raise RolloutError("staged_git_core_file_permissions_invalid")
            rows.append(
                (
                    f"f:{path.relative_to(core_root).as_posix()}",
                    file_mode,
                    int(metadata.st_size),
                    _sha256_file(path),
                )
            )
    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda item: item[0].encode("utf-8")):
        digest.update(
            json.dumps(row, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        )
        digest.update(b"\n")
    return {
        "entry_count": len(rows),
        "logical_bytes": sum(row[2] for row in rows),
        "content_mode_manifest_sha256": digest.hexdigest(),
    }


def _assert_materialized_core_identity(core_root: Path, expected: Any) -> None:
    if not isinstance(expected, dict) or _materialized_core_identity(core_root) != expected:
        raise RolloutError("staged_git_core_identity_drift")


def _assert_git_materialized_core_identity(
    core_root: Path,
    expected_git_materialized_identity: Any,
    expected_agents_sha256: Any,
) -> None:
    if (
        not isinstance(expected_git_materialized_identity, dict)
        or _materialized_core_identity(core_root, exclude_runtime_agents=True)
        != expected_git_materialized_identity
    ):
        raise RolloutError("staged_git_core_identity_drift")
    if (
        not isinstance(expected_agents_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_agents_sha256) is None
    ):
        raise RolloutError("runtime_agents_hash_invalid")
    _verify_runtime_agents(core_root, expected_agents_sha256)


def _runtime_core_full_identity(core_root: Path) -> dict[str, Any]:
    """Hash every executable legacy Core entry except its separately governed data."""

    if core_root.is_symlink() or not core_root.is_dir():
        raise RolloutError("runtime_core_tree_invalid")
    allowed_root_directories = {
        "activity_mvp",
        "browser_extension",
        "samples",
        "tests",
        "tools",
    }
    allowed_root_files = {
        ".gitignore",
        "AGENTS.md",
        "CHANGELOG.md",
        "README.md",
        "pyproject.toml",
    }
    rows: list[tuple[str, int, int, str]] = []
    for directory, directories, files in os.walk(core_root, topdown=True, followlinks=False):
        base = Path(directory)
        if base == core_root:
            directories[:] = [name for name in directories if name != "data"]
            if set(directories) - allowed_root_directories:
                raise RolloutError("runtime_core_root_allowlist_violation")
            if set(files) - allowed_root_files:
                raise RolloutError("runtime_core_root_import_shadow")
        directories[:] = sorted(directories)
        relative_base = base.relative_to(core_root)
        name = "." if relative_base == Path(".") else relative_base.as_posix()
        directory_mode = _mode(base)
        if directory_mode & 0o022:
            raise RolloutError("runtime_core_tree_permissions_invalid")
        rows.append((f"d:{name}", directory_mode, 0, ""))
        for child_name in directories:
            child = base / child_name
            if child.is_symlink() or not child.is_dir():
                raise RolloutError("runtime_core_tree_special_entry")
        for file_name in sorted(files):
            path = base / file_name
            if path.suffix.lower() in {".so", ".dylib"}:
                raise RolloutError("runtime_core_native_extension_forbidden")
            if path.suffix.lower() == ".pyc":
                if path.parent.name != "__pycache__":
                    raise RolloutError("runtime_core_orphan_bytecode")
                source_stem = path.name.split(".", 1)[0]
                source = path.parent.parent / f"{source_stem}.py"
                if source.is_symlink() or not source.is_file():
                    raise RolloutError("runtime_core_orphan_bytecode")
            if path.is_symlink() or not path.is_file():
                raise RolloutError("runtime_core_tree_special_entry")
            before = os.lstat(path)
            mode = stat.S_IMODE(before.st_mode)
            if mode & 0o022:
                raise RolloutError("runtime_core_tree_permissions_invalid")
            digest = _sha256_file(path)
            after = os.lstat(path)
            if (
                before.st_dev,
                before.st_ino,
                before.st_mode,
                before.st_size,
                before.st_mtime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_mode,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise RolloutError("runtime_core_tree_changed")
            rows.append(
                (
                    f"f:{path.relative_to(core_root).as_posix()}",
                    mode,
                    int(before.st_size),
                    digest,
                )
            )
    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda item: item[0].encode("utf-8")):
        digest.update(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        digest.update(b"\n")
    return {
        "entry_count": len(rows),
        "logical_bytes": sum(row[2] for row in rows),
        "content_mode_manifest_sha256": digest.hexdigest(),
    }


def _git_core_version(
    config: RolloutConfig,
    manifest: tuple[GitCoreBlob, ...],
) -> str:
    matching = [item for item in manifest if item.relative_path == "activity_mvp/__init__.py"]
    if len(matching) != 1:
        raise RolloutError("git_core_version_missing")
    try:
        source = _git_blob(config, matching[0].object_id).decode("utf-8")
    except UnicodeError as exc:
        raise RolloutError("git_core_version_invalid") from exc
    match = re.search(r'^__version__\s*=\s*"([^"]+)"\s*$', source, re.MULTILINE)
    if match is None:
        raise RolloutError("git_core_version_missing")
    return match.group(1)


def _core_version(core_root: Path) -> str:
    init_path = core_root / "activity_mvp" / "__init__.py"
    try:
        source = init_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RolloutError("core_version_unreadable") from exc
    match = re.search(r'^__version__\s*=\s*"([^"]+)"\s*$', source, re.MULTILINE)
    if not match:
        raise RolloutError("core_version_missing")
    return match.group(1)


def _tree_content_mode_hash(root: Path) -> str:
    if root.is_symlink() or not root.is_dir():
        raise RolloutError("identity_tree_invalid")
    digest = hashlib.sha256()
    for directory, directories, files in os.walk(root, topdown=True, followlinks=False):
        base = Path(directory)
        if _mode(base) & 0o022:
            raise RolloutError("identity_tree_permissions_invalid")
        directories[:] = sorted(directories)
        for name in directories:
            metadata = os.lstat(base / name)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise RolloutError("identity_tree_special_entry")
            if stat.S_IMODE(metadata.st_mode) & 0o022:
                raise RolloutError("identity_tree_permissions_invalid")
        relative_base = base.relative_to(root)
        directory_name = "." if relative_base == Path(".") else relative_base.as_posix()
        digest.update(f"d\0{directory_name}\0{_mode(base):04o}\n".encode("utf-8"))
        for name in sorted(files):
            path = base / name
            before = os.lstat(path)
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
                raise RolloutError("identity_tree_special_entry")
            if stat.S_IMODE(before.st_mode) & 0o022:
                raise RolloutError("identity_tree_permissions_invalid")
            relative = path.relative_to(root).as_posix()
            digest.update(
                f"f\0{relative}\0{stat.S_IMODE(before.st_mode):04o}\0{before.st_size}\0".encode("utf-8")
            )
            digest.update(_sha256_file(path).encode("ascii"))
            digest.update(b"\n")
            after = os.lstat(path)
            if (
                before.st_dev,
                before.st_ino,
                before.st_mode,
                before.st_size,
                before.st_mtime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_mode,
                after.st_size,
                after.st_mtime_ns,
            ):
                raise RolloutError("identity_tree_changed")
    return digest.hexdigest()


def _app_info(app: Path) -> dict[str, Any]:
    plist_path = app / "Contents" / "Info.plist"
    if app.is_symlink() or not app.is_dir() or plist_path.is_symlink() or not plist_path.is_file():
        raise RolloutError("app_bundle_invalid")
    try:
        with plist_path.open("rb") as handle:
            info = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException) as exc:
        raise RolloutError("app_plist_invalid") from exc
    return info


def _verify_signature(app: Path) -> None:
    completed = _run(["codesign", "--verify", "--deep", "--strict", str(app)])
    if completed.returncode != 0:
        raise RolloutError("app_signature_invalid")


def _app_signature_identity(app: Path) -> dict[str, str]:
    _verify_signature(app)
    details = _run(["codesign", "-dv", "--verbose=4", str(app)], timeout=15.0)
    text = "\n".join((details.stdout, details.stderr))
    identifiers = re.findall(r"(?m)^Identifier=(\S+)\s*$", text)
    cdhashes = re.findall(r"(?m)^CDHash=([0-9a-fA-F]+)\s*$", text)
    signatures = re.findall(r"(?m)^Signature=(.*?)\s*$", text)
    teams = re.findall(r"(?m)^TeamIdentifier=(.*?)\s*$", text)
    if (
        details.returncode != 0
        or identifiers != ["app.personalai.monitor"]
        or len(cdhashes) != 1
        or signatures != ["adhoc"]
        or teams != ["not set"]
        or re.fullmatch(r"[0-9a-fA-F]{40,64}", cdhashes[0]) is None
    ):
        raise RolloutError("app_signature_identity_invalid")
    return {
        "identifier": identifiers[0],
        "cdhash": cdhashes[0].lower(),
        "signature_kind": signatures[0],
        "team_identifier": teams[0],
    }


def _app_payload_identity(app: Path) -> dict[str, str]:
    info = _app_info(app)
    executable_name = info.get("CFBundleExecutable")
    if (
        not isinstance(executable_name, str)
        or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", executable_name) is None
    ):
        raise RolloutError("app_executable_invalid")
    executable = app / "Contents" / "MacOS" / executable_name
    if executable.is_symlink() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise RolloutError("app_executable_invalid")
    signature = _app_signature_identity(app)
    return {
        "payload_sha256": _tree_content_mode_hash(app),
        "executable_sha256": _sha256_file(executable),
        "cdhash": signature["cdhash"],
        "signature_kind": signature["signature_kind"],
        "team_identifier": signature["team_identifier"],
    }


def _assert_expected_app_identity(config: RolloutConfig, app: Path, *, error: str) -> dict[str, str]:
    identity = _app_payload_identity(app)
    expected = {
        "payload_sha256": config.expected_app_payload_sha256,
        "executable_sha256": config.expected_app_executable_sha256,
        "cdhash": config.expected_app_cdhash,
        "signature_kind": config.expected_app_signature_kind,
        "team_identifier": config.expected_app_team_identifier,
    }
    if identity != expected:
        raise RolloutError(error)
    return identity


def _verify_app_xattrs(app: Path) -> None:
    completed = _run(["xattr", "-lr", str(app)])
    if completed.returncode != 0:
        raise RolloutError("app_xattr_check_failed")
    forbidden = (
        "com.apple.quarantine:",
        "com.apple.FinderInfo:",
        "com.apple.ResourceFork:",
    )
    if any(attribute in completed.stdout for attribute in forbidden):
        raise RolloutError("app_forbidden_xattr")


def _read_stable_runtime_file(
    path: Path,
    *,
    expected_mode: int,
    maximum_bytes: int,
) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or stat.S_IMODE(before.st_mode) != expected_mode
                or before.st_size < 0
                or before.st_size > maximum_bytes
            ):
                raise RolloutError("stable_file_identity_invalid")
            chunks: list[bytes] = []
            remaining = maximum_bytes + 1
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        observed = os.stat(path, follow_symlinks=False)
    except RolloutError:
        raise
    except OSError as exc:
        raise RolloutError("stable_file_unreadable") from exc
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
    )
    path_identity = (
        observed.st_dev,
        observed.st_ino,
        observed.st_mode,
        observed.st_size,
        observed.st_mtime_ns,
    )
    if (
        len(payload) != before.st_size
        or len(payload) > maximum_bytes
        or before_identity != after_identity
        or after_identity != path_identity
    ):
        raise RolloutError("stable_file_identity_changed")
    return payload


def _verify_runtime_agents(core_root: Path, expected_sha256: str | None = None) -> str:
    path = core_root / "AGENTS.md"
    try:
        payload = _read_stable_runtime_file(
            path,
            expected_mode=0o644,
            maximum_bytes=4 * 1024 * 1024,
        )
    except RolloutError as exc:
        raise RolloutError("runtime_agents_file_invalid") from exc
    digest = hashlib.sha256(payload).hexdigest()
    if expected_sha256 is not None and digest != expected_sha256:
        raise RolloutError("runtime_agents_hash_mismatch")
    return digest


def _copy_live_runtime_agents(
    source_core: Path,
    destination_core: Path,
    *,
    expected_sha256: str,
) -> str:
    source = source_core / "AGENTS.md"
    payload = _read_stable_runtime_file(
        source,
        expected_mode=0o644,
        maximum_bytes=4 * 1024 * 1024,
    )
    observed_sha256 = hashlib.sha256(payload).hexdigest()
    if observed_sha256 != expected_sha256:
        raise RolloutError("runtime_agents_hash_mismatch")
    destination = destination_core / "AGENTS.md"
    if destination.exists() or destination.is_symlink():
        raise RolloutError("candidate_agents_file_already_exists")
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, "wb") as destination_handle:
            destination_handle.write(payload)
            destination_handle.flush()
            os.fsync(destination_handle.fileno())
        os.chmod(destination, 0o644, follow_symlinks=False)
    except Exception:
        raise
    _verify_runtime_agents(destination_core, expected_sha256)
    if _verify_runtime_agents(source_core, expected_sha256) != observed_sha256:
        raise RolloutError("runtime_agents_source_identity_changed")
    return expected_sha256


def _copy_candidate_app(source: Path, destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        raise RolloutError("incoming_app_exists")
    completed = _run(
        ["ditto", "--rsrc", "--extattr", "--acl", str(source), str(destination)],
        timeout=120.0,
    )
    if completed.returncode != 0:
        raise RolloutError("candidate_app_copy_failed")
    _fsync_tree(destination)


def _plist_payload(
    path: Path,
    *,
    expected_label: str,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    try:
        raw, metadata = _read_stable_nofollow_file(
            path,
            max_bytes=1024 * 1024,
            error="launch_agent_plist_invalid",
        )
        payload = plistlib.loads(raw)
    except (OSError, plistlib.InvalidFileException, RolloutError) as exc:
        raise RolloutError("launch_agent_plist_invalid") from exc
    if expected_sha256 is not None and (
        re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
        or hashlib.sha256(raw).hexdigest() != expected_sha256
    ):
        raise RolloutError("launch_agent_plist_identity_drift")
    if (
        stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or not isinstance(payload, dict)
    ):
        raise RolloutError("launch_agent_plist_invalid")
    if payload.get("Label") != expected_label:
        raise RolloutError("launch_agent_label_mismatch")
    return payload


def _root_owned_regular_path_snapshot(
    path: Path,
    *,
    executable: bool,
    error: str,
) -> tuple[tuple[Any, ...], ...]:
    if (
        not path.is_absolute()
        or len(path.parts) < 2
        or any(component in {"", ".", ".."} for component in path.parts[1:])
    ):
        raise RolloutError(error)
    cursor = Path("/")
    snapshot: list[tuple[Any, ...]] = []
    try:
        for index, component in enumerate(path.parts[1:]):
            cursor /= component
            metadata = os.lstat(cursor)
            is_final = index == len(path.parts[1:]) - 1
            mode = stat.S_IMODE(metadata.st_mode)
            if (
                metadata.st_uid != 0
                or stat.S_ISLNK(metadata.st_mode)
                or mode & 0o002
                or (mode & 0o020 and metadata.st_gid not in PYTHON_TRUSTED_GROUP_IDS)
            ):
                raise RolloutError(error)
            if is_final:
                if not stat.S_ISREG(metadata.st_mode):
                    raise RolloutError(error)
                if executable and not os.access(cursor, os.X_OK):
                    raise RolloutError(error)
            elif not stat.S_ISDIR(metadata.st_mode):
                raise RolloutError(error)
            snapshot.append(
                (
                    str(cursor),
                    int(metadata.st_dev),
                    int(metadata.st_ino),
                    int(metadata.st_uid),
                    int(metadata.st_gid),
                    mode,
                    int(metadata.st_size) if is_final else None,
                    int(metadata.st_mtime_ns) if is_final else None,
                )
            )
    except OSError as exc:
        raise RolloutError(error) from exc
    return tuple(snapshot)


def _python_codesign_identity(
    path: Path,
    *,
    expected_identifier: str,
    error: str,
) -> dict[str, Any]:
    requirement = (
        f'anchor apple generic and identifier "{expected_identifier}" '
        f'and certificate leaf[subject.OU] = "{PYTHON_TEAM_IDENTIFIER}"'
    )
    requirement_check = _run(
        ["codesign", "--verify", "--strict", f"-R={requirement}", str(path)],
        timeout=15.0,
    )
    details = _run(
        ["codesign", "-dv", "--verbose=4", str(path)],
        timeout=15.0,
    )
    details_text = "\n".join((details.stdout, details.stderr))
    identifiers = re.findall(r"(?m)^Identifier=(\S+)\s*$", details_text)
    teams = re.findall(r"(?m)^TeamIdentifier=(\S+)\s*$", details_text)
    cdhashes = re.findall(r"(?m)^CDHash=([0-9a-fA-F]+)\s*$", details_text)
    authorities = re.findall(r"(?m)^Authority=(.*?)\s*$", details_text)
    if (
        requirement_check.returncode != 0
        or details.returncode != 0
        or identifiers != [expected_identifier]
        or teams != [PYTHON_TEAM_IDENTIFIER]
        or len(cdhashes) != 1
        or re.fullmatch(r"[0-9a-fA-F]{40,64}", cdhashes[0]) is None
        or authorities != list(PYTHON_SIGNING_AUTHORITY_CHAIN)
    ):
        raise RolloutError(error)
    return {
        "identifier": identifiers[0],
        "team_identifier": teams[0],
        "cdhash": cdhashes[0].lower(),
        "authority": authorities[0],
        "authority_chain": authorities,
    }


def _read_stable_nofollow_file(
    path: Path,
    *,
    max_bytes: int,
    error: str,
) -> tuple[bytes, os.stat_result]:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise RolloutError(error)
    flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RolloutError(error) from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size < 0
            or before.st_size > max_bytes
        ):
            raise RolloutError(error)
        payload = bytearray()
        while True:
            chunk = os.read(descriptor, min(65536, max_bytes + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
            if len(payload) > max_bytes:
                raise RolloutError(error)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise RolloutError(error) from exc
    finally:
        os.close(descriptor)
    try:
        path_after = os.lstat(path)
    except OSError as exc:
        raise RolloutError(error) from exc
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_uid,
        before.st_gid,
        before.st_nlink,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_uid,
        after.st_gid,
        after.st_nlink,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    path_identity = (
        path_after.st_dev,
        path_after.st_ino,
        path_after.st_mode,
        path_after.st_uid,
        path_after.st_gid,
        path_after.st_nlink,
        path_after.st_size,
        path_after.st_mtime_ns,
        path_after.st_ctime_ns,
    )
    if (
        before_identity != after_identity
        or after_identity != path_identity
        or len(payload) != after.st_size
    ):
        raise RolloutError(error)
    return bytes(payload), after


def _trusted_python_process_image(resolved: Path) -> dict[str, Any]:
    name_match = re.fullmatch(r"python([0-9]+)\.([0-9]+)", resolved.name)
    if (
        name_match is None
        or resolved.parent.name != "bin"
    ):
        raise RolloutError("launch_agent_process_image_invalid")
    version_root = resolved.parent.parent
    expected_framework_version = ".".join(name_match.groups())
    if (
        version_root.name != expected_framework_version
        or version_root.parent.name != "Versions"
        or version_root.parent.parent.name != "Python.framework"
    ):
        raise RolloutError("launch_agent_process_image_invalid")
    bundle = version_root / "Resources" / "Python.app"
    info_plist = bundle / "Contents" / "Info.plist"
    process_image = bundle / "Contents" / "MacOS" / "Python"
    image_before = _root_owned_regular_path_snapshot(
        process_image,
        executable=True,
        error="launch_agent_process_image_untrusted",
    )
    plist_before = _root_owned_regular_path_snapshot(
        info_plist,
        executable=False,
        error="launch_agent_process_image_untrusted",
    )
    try:
        image_stat_before = process_image.stat()
        image_sha_before = _sha256_file(process_image)
        plist_payload, plist_stat_before = _read_stable_nofollow_file(
            info_plist,
            max_bytes=1024 * 1024,
            error="launch_agent_process_image_invalid",
        )
        plist_sha_before = hashlib.sha256(plist_payload).hexdigest()
        bundle_info = plistlib.loads(plist_payload)
    except (OSError, plistlib.InvalidFileException) as exc:
        raise RolloutError("launch_agent_process_image_invalid") from exc
    short_version = (
        bundle_info.get("CFBundleShortVersionString")
        if isinstance(bundle_info, dict)
        else None
    )
    bundle_version = (
        bundle_info.get("CFBundleVersion") if isinstance(bundle_info, dict) else None
    )
    version_match = (
        re.fullmatch(
            r"(0|[1-9][0-9]{0,5})\."
            r"(0|[1-9][0-9]{0,5})\."
            r"(0|[1-9][0-9]{0,5})",
            short_version,
        )
        if isinstance(short_version, str)
        else None
    )
    if (
        not isinstance(bundle_info, dict)
        or bundle_info.get("CFBundleIdentifier") != PYTHON_PROCESS_IDENTIFIER
        or bundle_info.get("CFBundleExecutable") != "Python"
        or not isinstance(bundle_version, str)
        or bundle_version != short_version
        or version_match is None
        or version_match.groups()[:2] != name_match.groups()
        or tuple(int(value) for value in version_match.groups()) < (3, 12, 0)
    ):
        raise RolloutError("launch_agent_process_image_bundle_mismatch")
    version = ".".join(version_match.groups())
    bundle_signature = _run(
        ["codesign", "--verify", "--deep", "--strict", str(bundle)],
        timeout=15.0,
    )
    image_signature = _run(
        ["codesign", "--verify", "--strict", str(process_image)],
        timeout=15.0,
    )
    if bundle_signature.returncode != 0 or image_signature.returncode != 0:
        raise RolloutError("launch_agent_process_image_signature_invalid")
    bundle_identity = _python_codesign_identity(
        bundle,
        expected_identifier=PYTHON_PROCESS_IDENTIFIER,
        error="launch_agent_process_image_signature_identity_mismatch",
    )
    image_identity = _python_codesign_identity(
        process_image,
        expected_identifier=PYTHON_PROCESS_IDENTIFIER,
        error="launch_agent_process_image_signature_identity_mismatch",
    )
    image_after = _root_owned_regular_path_snapshot(
        process_image,
        executable=True,
        error="launch_agent_process_image_untrusted",
    )
    plist_after = _root_owned_regular_path_snapshot(
        info_plist,
        executable=False,
        error="launch_agent_process_image_untrusted",
    )
    try:
        image_stat_after = process_image.stat()
        image_sha_after = _sha256_file(process_image)
    except OSError as exc:
        raise RolloutError("launch_agent_process_image_invalid") from exc
    plist_snapshot_identity = plist_after[-1][1:8]
    plist_fd_identity = (
        int(plist_stat_before.st_dev),
        int(plist_stat_before.st_ino),
        int(plist_stat_before.st_uid),
        int(plist_stat_before.st_gid),
        stat.S_IMODE(plist_stat_before.st_mode),
        int(plist_stat_before.st_size),
        int(plist_stat_before.st_mtime_ns),
    )
    if (
        image_before != image_after
        or plist_before != plist_after
        or plist_snapshot_identity != plist_fd_identity
        or (
            image_stat_before.st_dev,
            image_stat_before.st_ino,
            image_stat_before.st_size,
            image_stat_before.st_mtime_ns,
        )
        != (
            image_stat_after.st_dev,
            image_stat_after.st_ino,
            image_stat_after.st_size,
            image_stat_after.st_mtime_ns,
        )
        or image_sha_before != image_sha_after
    ):
        raise RolloutError("launch_agent_process_image_identity_changed")
    bundle_identity_final = _python_codesign_identity(
        bundle,
        expected_identifier=PYTHON_PROCESS_IDENTIFIER,
        error="launch_agent_process_image_signature_identity_mismatch",
    )
    image_identity_final = _python_codesign_identity(
        process_image,
        expected_identifier=PYTHON_PROCESS_IDENTIFIER,
        error="launch_agent_process_image_signature_identity_mismatch",
    )
    if (
        bundle_identity_final != bundle_identity
        or image_identity_final != image_identity
    ):
        raise RolloutError("launch_agent_process_image_identity_changed")
    return {
        "path": str(process_image),
        "bundle_path": str(bundle),
        "bundle_identifier": bundle_identity["identifier"],
        "bundle_team_identifier": bundle_identity["team_identifier"],
        "bundle_cdhash": bundle_identity["cdhash"],
        "bundle_authority": bundle_identity["authority"],
        "bundle_authority_chain": bundle_identity["authority_chain"],
        "sha256": image_sha_after,
        "device": int(image_stat_after.st_dev),
        "inode": int(image_stat_after.st_ino),
        "size": int(image_stat_after.st_size),
        "version": version,
        "identifier": image_identity["identifier"],
        "team_identifier": image_identity["team_identifier"],
        "cdhash": image_identity["cdhash"],
        "authority": image_identity["authority"],
        "authority_chain": image_identity["authority_chain"],
        "info_plist_sha256": plist_sha_before,
    }


def _trusted_python_interpreter(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        raise RolloutError("launch_agent_interpreter_invalid")
    path = Path(value)
    if not path.is_absolute() or path.name not in {"python", "python3"}:
        raise RolloutError("launch_agent_interpreter_invalid")

    # Resolve one component at a time so every directory and every symlink in
    # the interpreter chain is authenticated before launchctl is trusted.
    # The Python.org framework on this Mac is root:admin 0775, so replacement
    # detection is provided by its strict Developer ID identity plus an exact
    # content hash that is rechecked at cutover and against the running PID.
    pending = list(path.parts[1:])
    cursor = Path("/")
    symlinks_seen: set[tuple[int, int]] = set()
    try:
        while pending:
            component = pending.pop(0)
            if component in {"", "."}:
                continue
            if component == "..":
                cursor = cursor.parent
                continue
            candidate = cursor / component
            metadata = os.lstat(candidate)
            mode = stat.S_IMODE(metadata.st_mode)
            if (
                metadata.st_uid != 0
                or mode & 0o002
                or (mode & 0o020 and metadata.st_gid not in PYTHON_TRUSTED_GROUP_IDS)
            ):
                raise RolloutError("launch_agent_interpreter_untrusted")
            if stat.S_ISLNK(metadata.st_mode):
                identity = (int(metadata.st_dev), int(metadata.st_ino))
                if identity in symlinks_seen or len(symlinks_seen) >= 40:
                    raise RolloutError("launch_agent_interpreter_untrusted")
                symlinks_seen.add(identity)
                target = Path(os.readlink(candidate))
                if target.is_absolute():
                    cursor = Path("/")
                    target_parts = list(target.parts[1:])
                else:
                    target_parts = list(target.parts)
                pending = target_parts + pending
                continue
            if pending:
                if not stat.S_ISDIR(metadata.st_mode):
                    raise RolloutError("launch_agent_interpreter_untrusted")
                cursor = candidate
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise RolloutError("launch_agent_interpreter_untrusted")
            resolved = candidate
    except OSError as exc:
        raise RolloutError("launch_agent_interpreter_invalid") from exc
    if "resolved" not in locals() or not os.access(path, os.X_OK):
        raise RolloutError("launch_agent_interpreter_untrusted")
    if re.fullmatch(r"python3(?:\.\d+)+", resolved.name) is None:
        raise RolloutError("launch_agent_interpreter_untrusted")
    try:
        before = resolved.stat()
        sha256_before = _sha256_file(resolved)
    except OSError as exc:
        raise RolloutError("launch_agent_interpreter_invalid") from exc
    signature = _run(["codesign", "--verify", "--strict", str(resolved)], timeout=15.0)
    if signature.returncode != 0:
        raise RolloutError("launch_agent_interpreter_signature_invalid")
    launcher_identity = _python_codesign_identity(
        resolved,
        expected_identifier=PYTHON_LAUNCHER_IDENTIFIER,
        error="launch_agent_interpreter_signature_identity_mismatch",
    )
    try:
        after = resolved.stat()
        sha256_after = _sha256_file(resolved)
    except OSError as exc:
        raise RolloutError("launch_agent_interpreter_invalid") from exc
    if (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or sha256_before != sha256_after
    ):
        raise RolloutError("launch_agent_interpreter_identity_changed")
    process_image = _trusted_python_process_image(resolved)
    version = str(process_image["version"])
    try:
        final = resolved.stat()
        sha256_final = _sha256_file(resolved)
    except OSError as exc:
        raise RolloutError("launch_agent_interpreter_invalid") from exc
    if (
        (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        != (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns)
        or sha256_after != sha256_final
    ):
        raise RolloutError("launch_agent_interpreter_identity_changed")
    return {
        "identity_format": "python_org_dual_image_v1",
        "program": str(path),
        "resolved_path": str(resolved),
        "framework_version_root": str(resolved.parent.parent),
        "sha256": sha256_final,
        "device": int(final.st_dev),
        "inode": int(final.st_ino),
        "size": int(final.st_size),
        "version": version,
        "identifier": launcher_identity["identifier"],
        "team_identifier": launcher_identity["team_identifier"],
        "cdhash": launcher_identity["cdhash"],
        "authority": launcher_identity["authority"],
        "authority_chain": launcher_identity["authority_chain"],
        "process_image": process_image,
    }


def _validate_launch_agent_envelope(
    payload: dict[str, Any],
    *,
    allowed_keys: set[str],
) -> None:
    if (
        set(payload) != allowed_keys
        or payload.get("RunAtLoad") is not True
        or payload.get("KeepAlive") is not True
    ):
        raise RolloutError("launch_agent_schema_invalid")
    for key in ("StandardOutPath", "StandardErrorPath"):
        value = payload.get(key)
        if (
            not isinstance(value, str)
            or not Path(value).is_absolute()
            or "\x00" in value
            or any(ord(character) < 0x20 for character in value)
        ):
            raise RolloutError("launch_agent_log_path_invalid")


def _validate_launch_environment(
    environment: Any,
    *,
    core: bool,
    legacy_pythonpath: str | None,
) -> dict[str, str]:
    if not isinstance(environment, dict):
        raise RolloutError("launch_agent_environment_invalid")
    required = {"PATH", "PAM_PRIVACY_PROFILE"} if core else {"PATH"}
    optional = CORE_OPTIONAL_ENV_KEYS if core else set()
    allowed = required | optional
    if legacy_pythonpath is not None:
        allowed.add("PYTHONPATH")
    if any(key.startswith("PYTHON") and key != "PYTHONPATH" for key in environment):
        raise RolloutError("launch_agent_python_environment_forbidden")
    if not required.issubset(environment) or not set(environment).issubset(allowed):
        raise RolloutError("launch_agent_environment_invalid")
    if environment.get("PATH") != LAUNCH_AGENT_PATH:
        raise RolloutError("launch_agent_path_environment_invalid")
    if core and environment.get("PAM_PRIVACY_PROFILE") not in {"strict", "personal_high_precision"}:
        raise RolloutError("launch_agent_privacy_environment_invalid")
    if legacy_pythonpath is None and "PYTHONPATH" in environment:
        raise RolloutError("launch_agent_python_environment_forbidden")
    if legacy_pythonpath is not None and environment.get("PYTHONPATH") != legacy_pythonpath:
        raise RolloutError("launch_agent_python_environment_forbidden")
    for key, value in environment.items():
        if (
            not isinstance(key, str)
            or not isinstance(value, str)
            or not value
            or len(value) > 4096
            or "\x00" in value
            or any(ord(character) < 0x20 for character in value)
        ):
            raise RolloutError("launch_agent_environment_invalid")
    endpoint = environment.get("PAM_VLM_ENDPOINT")
    if endpoint is not None:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise RolloutError("launch_agent_environment_invalid")
    return {str(key): str(value) for key, value in environment.items()}


def _verify_core_launch_agent_payload(
    config: RolloutConfig,
    payload: dict[str, Any],
    *,
    mode: str = "legacy",
    isolated_launcher_root: Path | None = None,
) -> dict[str, Any]:
    if mode not in {"legacy", "isolated"}:
        raise RolloutError("launch_agent_validation_mode_invalid")
    if payload.get("Label") != config.service_label:
        raise RolloutError("launch_agent_label_mismatch")
    _validate_launch_agent_envelope(payload, allowed_keys=CORE_LAUNCH_AGENT_KEYS)
    environment = payload.get("EnvironmentVariables")
    arguments = payload.get("ProgramArguments")
    isolated_arguments = [
        "-I",
        "-S",
        str(config.live_core / ISOLATED_LAUNCHER_NAME),
        "--db",
        str(config.live_database),
        "run-daemon",
    ]
    legacy_arguments = [
        "-m",
        "activity_mvp",
        "--db",
        str(config.live_database),
        "run-daemon",
    ]
    isolated = (
        isinstance(arguments, list)
        and len(arguments) == 1 + len(isolated_arguments)
        and arguments[1:] == isolated_arguments
    )
    legacy = (
        isinstance(arguments, list)
        and len(arguments) == 1 + len(legacy_arguments)
        and arguments[1:] == legacy_arguments
    )
    if (
        payload.get("WorkingDirectory") != str(config.live_core)
        or not isinstance(arguments, list)
        or not arguments
        or (mode == "isolated" and not isolated)
        or (mode == "legacy" and not legacy)
    ):
        raise RolloutError("launch_agent_canonical_path_mismatch")
    validated_environment = _validate_launch_environment(
        environment,
        core=True,
        legacy_pythonpath=None if isolated else str(config.live_core),
    )
    if validated_environment.get("PAM_PRIVACY_PROFILE") != config.expected_privacy_profile:
        raise RolloutError("launch_agent_privacy_environment_invalid")
    if isolated:
        launcher = (isolated_launcher_root or config.live_core) / ISOLATED_LAUNCHER_NAME
        if (
            launcher.is_symlink()
            or not launcher.is_file()
            or _mode(launcher) != 0o755
            or not os.access(launcher, os.X_OK)
        ):
            raise RolloutError("isolated_launcher_invalid")
    interpreter = _trusted_python_interpreter(arguments[0])
    return {
        "program": interpreter["program"],
        "resolved_program": interpreter["resolved_path"],
        "program_sha256": interpreter["sha256"],
        "interpreter": interpreter,
        "arguments": [str(value) for value in arguments],
        "working_directory": str(config.live_core),
        "database": str(config.live_database),
        "isolated": isolated,
        "environment": validated_environment,
    }


def _verify_core_launch_agent_path(
    config: RolloutConfig,
    path: Path,
    *,
    mode: str = "legacy",
    isolated_launcher_root: Path | None = None,
) -> dict[str, Any]:
    payload = _plist_payload(path, expected_label=config.service_label)
    return _verify_core_launch_agent_payload(
        config,
        payload,
        mode=mode,
        isolated_launcher_root=isolated_launcher_root,
    )


def _verify_core_launch_agent(
    config: RolloutConfig,
    *,
    mode: str = "legacy",
) -> dict[str, Any]:
    return _verify_core_launch_agent_path(
        config,
        config.launch_agent_plist,
        mode=mode,
    )


def _shadow_core_database_pair(shadow_core: Path, database: Path) -> dict[str, Any]:
    version = _core_version(shadow_core)
    core_hash = _core_code_hash(shadow_core)
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=1.0)
        try:
            quick = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
            row = connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            schema = int(row[0]) if row else -1
        finally:
            connection.close()
    except (sqlite3.Error, TypeError, ValueError) as exc:
        raise RolloutError("shadow_launch_agent_database_invalid") from exc
    compatible = SHADOW_COMPATIBLE_SCHEMAS.get(version)
    if quick != "ok" or foreign_keys or compatible is None or schema not in compatible:
        raise RolloutError("shadow_core_database_pair_invalid")
    if schema == TARGET_SCHEMA:
        _sqlite_probe(database, expected_schema=TARGET_SCHEMA, require_context_disabled=True)
    material = json.dumps(
        {"core_hash": core_hash, "core_version": version, "schema": schema},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "core_version": version,
        "core_code_sha256": core_hash,
        "database_schema": schema,
        "pair_sha256": hashlib.sha256(material).hexdigest(),
    }


def _verify_shadow_launch_agent_payload(
    config: RolloutConfig,
    payload: dict[str, Any],
    *,
    mode: str = "legacy",
) -> dict[str, Any]:
    if mode not in {"legacy", "isolated"}:
        raise RolloutError("launch_agent_validation_mode_invalid")
    if payload.get("Label") != config.shadow_launch_agent_label:
        raise RolloutError("launch_agent_label_mismatch")
    _validate_launch_agent_envelope(payload, allowed_keys=SHADOW_LAUNCH_AGENT_KEYS)
    if payload.get("ProcessType") != "Background" or payload.get("ThrottleInterval") != 2:
        raise RolloutError("shadow_launch_agent_schema_invalid")
    working_directory = payload.get("WorkingDirectory")
    environment = payload.get("EnvironmentVariables")
    arguments = payload.get("ProgramArguments")
    isolated_prefix = ["-I", "-S", "-c", ISOLATED_MODULE_BOOTSTRAP, working_directory]
    isolated = (
        isinstance(arguments, list)
        and len(arguments) == 12
        and arguments[1:6] == isolated_prefix
        and arguments[6] == "--db"
        and arguments[8:] == ["task-sources", "serve-receipts", "--socket", arguments[11]]
    )
    legacy = (
        isinstance(arguments, list)
        and len(arguments) == 9
        and arguments[1] == "-m"
        and arguments[2] == "activity_mvp"
        and arguments[3] == "--db"
        and arguments[5:] == ["task-sources", "serve-receipts", "--socket", arguments[8]]
    )
    if (
        not isinstance(working_directory, str)
        or not Path(working_directory).is_absolute()
        or not isinstance(arguments, list)
        or (mode == "isolated" and not isolated)
        or (mode == "legacy" and not legacy)
    ):
        raise RolloutError("shadow_launch_agent_canonical_path_mismatch")
    validated_environment = _validate_launch_environment(
        environment,
        core=False,
        legacy_pythonpath=None if isolated else working_directory,
    )
    interpreter = _trusted_python_interpreter(arguments[0])
    shadow_core = Path(working_directory)
    database_index = 7 if isolated else 4
    socket_index = 11 if isolated else 8
    database = Path(arguments[database_index]) if isinstance(arguments[database_index], str) else Path("")
    socket_path = Path(arguments[socket_index]) if isinstance(arguments[socket_index], str) else Path("")
    if (
        not database.is_absolute()
        or not socket_path.is_absolute()
        or shadow_core.is_symlink()
        or not shadow_core.is_dir()
        or database.is_symlink()
        or not database.is_file()
        or _mode(database) != 0o600
    ):
        raise RolloutError("shadow_launch_agent_database_invalid")
    try:
        shadow_core_resolved = shadow_core.resolve(strict=True)
        live_core_resolved = config.live_core.resolve(strict=True)
        database_resolved = database.resolve(strict=True)
        live_database_resolved = config.live_database.resolve(strict=True)
        database_stat = database.stat()
        live_database_stat = config.live_database.stat()
    except OSError as exc:
        raise RolloutError("shadow_launch_agent_database_invalid") from exc
    if (
        _paths_overlap(shadow_core, config.live_core)
        or _paths_overlap(shadow_core_resolved, live_core_resolved)
    ):
        raise RolloutError("shadow_launch_agent_core_not_independent")
    if (
        database == config.live_database
        or database_resolved == live_database_resolved
        or (database_stat.st_dev, database_stat.st_ino)
        == (live_database_stat.st_dev, live_database_stat.st_ino)
    ):
        raise RolloutError("shadow_launch_agent_database_not_independent")
    pair = _shadow_core_database_pair(shadow_core, database)
    return {
        "program": interpreter["program"],
        "resolved_program": interpreter["resolved_path"],
        "program_sha256": interpreter["sha256"],
        "interpreter": interpreter,
        "arguments": [str(value) for value in arguments],
        "working_directory": str(shadow_core),
        "database": str(database),
        "socket": str(socket_path),
        "isolated": isolated,
        "environment": validated_environment,
        "shadow_pair": pair,
    }


def _verify_shadow_launch_agent_path(
    config: RolloutConfig,
    path: Path,
    *,
    mode: str = "legacy",
) -> dict[str, Any]:
    payload = _plist_payload(
        path,
        expected_label=config.shadow_launch_agent_label,
    )
    return _verify_shadow_launch_agent_payload(
        config,
        payload,
        mode=mode,
    )


def _verify_shadow_launch_agent(
    config: RolloutConfig,
    *,
    mode: str = "legacy",
) -> dict[str, Any]:
    return _verify_shadow_launch_agent_path(
        config,
        config.shadow_launch_agent_plist,
        mode=mode,
    )


def _assert_matching_interpreter_identity(
    core_agent: dict[str, Any],
    shadow_agent: dict[str, Any],
    *,
    expected: Any = None,
) -> dict[str, Any]:
    core_identity = core_agent.get("interpreter")
    shadow_identity = shadow_agent.get("interpreter")
    if not isinstance(core_identity, dict) or core_identity != shadow_identity:
        raise RolloutError("launch_agent_interpreter_identity_mismatch")
    if expected is not None and core_identity != expected:
        raise RolloutError("launch_agent_interpreter_identity_drift")
    return core_identity


def _recorded_interpreter_identity(source_identity: dict[str, Any]) -> dict[str, Any]:
    expected = source_identity.get("python_interpreter")
    if not isinstance(expected, dict):
        raise RolloutError("launch_agent_interpreter_source_identity_missing")
    return expected


def _launchctl_print_identity(output: str) -> dict[str, Any]:
    def scalar(name: str) -> str | None:
        matches = re.findall(
            rf"(?m)^\s*{re.escape(name)}\s*=\s*(.*?)\s*$",
            output,
        )
        if not matches:
            return None
        if len(matches) != 1:
            raise RolloutError("launch_agent_runtime_identity_ambiguous")
        return matches[0]

    block = re.search(
        r"(?ms)^\s*arguments\s*=\s*\{\s*\n(.*?)^\s*\}\s*$",
        output,
    )
    if block is None:
        arguments: list[str] = []
    else:
        arguments = [line.strip() for line in block.group(1).splitlines() if line.strip()]
    pid_value = scalar("pid")
    try:
        pid = int(pid_value) if pid_value is not None else None
    except ValueError as exc:
        raise RolloutError("launch_agent_runtime_pid_invalid") from exc
    return {
        "program": scalar("program"),
        "arguments": arguments,
        "working_directory": scalar("working directory"),
        "pid": pid,
    }


def _verify_pid_os_identity(pid: int, expected: dict[str, Any]) -> None:
    if pid <= 0:
        raise RolloutError("launch_agent_runtime_pid_invalid")
    working_directory = Path(str(expected.get("working_directory") or ""))
    try:
        if working_directory.is_symlink() or not working_directory.is_dir():
            raise RolloutError("launch_agent_pid_file_identity_mismatch")
        cwd_before = working_directory.stat()
    except OSError as exc:
        raise RolloutError("launch_agent_pid_file_identity_mismatch") from exc
    lsof = _run(
        ["lsof", "-a", "-p", str(pid), "-d", "txt,cwd", "-F", "fDni"],
        timeout=10.0,
    )
    if lsof.returncode != 0:
        raise RolloutError("launch_agent_pid_lsof_unavailable")
    records: list[dict[str, Any]] = []
    record: dict[str, Any] | None = None
    for line in lsof.stdout.splitlines():
        if line.startswith("f"):
            if record is not None:
                records.append(record)
            record = {"descriptor": line[1:]}
        elif record is not None and line.startswith("D"):
            try:
                record["device"] = int(line[1:], 0)
            except ValueError as exc:
                raise RolloutError("launch_agent_pid_lsof_identity_invalid") from exc
        elif record is not None and line.startswith("i"):
            try:
                record["inode"] = int(line[1:])
            except ValueError as exc:
                raise RolloutError("launch_agent_pid_lsof_identity_invalid") from exc
        elif record is not None and line.startswith("n"):
            record["path"] = line[1:]
    if record is not None:
        records.append(record)
    cwd_records = [item for item in records if item.get("descriptor") == "cwd"]
    text_records = [item for item in records if item.get("descriptor") == "txt"]
    try:
        cwd_after = working_directory.stat()
    except OSError as exc:
        raise RolloutError("launch_agent_pid_file_identity_mismatch") from exc
    cwd_matches = [
        item
        for item in cwd_records
        if item.get("path") == str(working_directory)
        and item.get("device") == int(cwd_before.st_dev)
        and item.get("inode") == int(cwd_before.st_ino)
    ]
    if (
        (cwd_before.st_dev, cwd_before.st_ino)
        != (cwd_after.st_dev, cwd_after.st_ino)
        or len(cwd_matches) != 1
    ):
        raise RolloutError("launch_agent_pid_file_identity_mismatch")

    current_interpreter = _trusted_python_interpreter(expected["program"])
    if current_interpreter != expected.get("interpreter"):
        raise RolloutError("launch_agent_interpreter_identity_drift")
    process_image = current_interpreter.get("process_image")
    if not isinstance(process_image, dict):
        raise RolloutError("launch_agent_process_image_identity_missing")
    process_path = Path(str(process_image.get("path") or ""))
    try:
        binary = process_path.stat()
    except OSError as exc:
        raise RolloutError("launch_agent_pid_file_identity_mismatch") from exc
    if (
        not process_path.is_absolute()
        or process_image.get("device") != int(binary.st_dev)
        or process_image.get("inode") != int(binary.st_ino)
        or process_image.get("size") != int(binary.st_size)
    ):
        raise RolloutError("launch_agent_pid_file_identity_mismatch")
    binary_matches = [
        item
        for item in text_records
        if item.get("path") == str(process_path)
        and item.get("device") == int(binary.st_dev)
        and item.get("inode") == int(binary.st_ino)
    ]
    if len(binary_matches) != 1:
        raise RolloutError("launch_agent_pid_file_identity_mismatch")

    process = _run(
        ["ps", "-ww", "-p", str(pid), "-o", "uid=", "-o", "command="],
        timeout=10.0,
    )
    rows = [line.strip() for line in process.stdout.splitlines() if line.strip()]
    if process.returncode != 0 or len(rows) != 1:
        raise RolloutError("launch_agent_pid_process_identity_unavailable")
    pieces = rows[0].split(maxsplit=1)
    try:
        process_uid = int(pieces[0])
    except (IndexError, ValueError) as exc:
        raise RolloutError("launch_agent_pid_process_identity_invalid") from exc
    arguments = expected.get("arguments")
    if (
        not isinstance(arguments, list)
        or not arguments
        or arguments[0] != expected.get("program")
        or not all(isinstance(argument, str) for argument in arguments)
    ):
        raise RolloutError("launch_agent_pid_process_identity_invalid")
    expected_command = " ".join([str(process_path), *arguments[1:]])
    if len(pieces) != 2 or process_uid != os.getuid() or pieces[1] != expected_command:
        raise RolloutError("launch_agent_pid_process_identity_mismatch")
    final_interpreter = _trusted_python_interpreter(expected["program"])
    if final_interpreter != current_interpreter:
        raise RolloutError("launch_agent_interpreter_identity_drift")


def _verify_launchctl_runtime(
    output: str,
    expected: dict[str, Any],
    *,
    target: str,
) -> int | None:
    actual = _launchctl_print_identity(output)
    if (
        actual["program"] != expected["program"]
        or actual["arguments"] != expected["arguments"]
        or actual["working_directory"] != expected["working_directory"]
    ):
        raise RolloutError("launch_agent_runtime_identity_mismatch")
    pid = actual["pid"]
    if pid is None or pid <= 0:
        return None
    _verify_pid_os_identity(pid, expected)
    final = _run(["launchctl", "print", target], timeout=10.0)
    if final.returncode != 0:
        raise RolloutError("launch_agent_runtime_identity_recheck_unavailable")
    final_actual = _launchctl_print_identity(final.stdout)
    if final_actual != actual:
        raise RolloutError("launch_agent_runtime_identity_changed")
    return pid


def _verify_shadow_runtime(
    config: RolloutConfig,
    *,
    expected_interpreter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    identity = _verify_shadow_launch_agent_path(config, config.shadow_launch_agent_plist)
    if expected_interpreter is not None and identity.get("interpreter") != expected_interpreter:
        raise RolloutError("launch_agent_interpreter_identity_drift")
    target = f"{_launch_target()}/{config.shadow_launch_agent_label}"
    for _ in range(20):
        completed = _run(["launchctl", "print", target], timeout=10.0)
        if completed.returncode == 0:
            pid = _verify_launchctl_runtime(completed.stdout, identity, target=target)
            if pid is None:
                time.sleep(0.25)
                continue
            return {
                "running": True,
                "pid": pid,
                "canonical_core": True,
                "independent_database": True,
            }
        time.sleep(0.25)
    raise RolloutError("shadow_launch_agent_not_running")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _sqlite_probe(
    path: Path,
    *,
    expected_schema: int,
    require_context_disabled: bool = True,
) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RolloutError("database_invalid")
    if _mode(path) != 0o600:
        raise RolloutError("database_permissions_invalid")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
        try:
            quick = str(conn.execute("PRAGMA quick_check").fetchone()[0])
            foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
            row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            schema = int(row[0]) if row else -1
            tables = {
                str(item[0])
                for item in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            gates: dict[str, str | None] = {}
            if "settings" in tables:
                for key in INITIAL_DISABLED_GATES:
                    value = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
                    gates[key] = str(value[0]).lower() if value else None
            aggregate_counts: dict[str, int] = {}
            for table in (
                "activities",
                "activity_confirmation_items",
                "activity_confirmation_evidence_files",
            ):
                if table in tables:
                    aggregate_counts[table] = int(
                        conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                    )
            confirmation_digest = None
            if "activity_confirmation_items" in tables:
                rows = conn.execute(
                    "SELECT item_key,answer,answer_reason,answered_at "
                    "FROM activity_confirmation_items WHERE answer IS NOT NULL ORDER BY item_key"
                ).fetchall()
                aggregate_counts["explicit_owner_answers"] = len(rows)
                digest = hashlib.sha256()
                for item in rows:
                    digest.update(
                        json.dumps(
                            [str(value or "") for value in item],
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    )
                    digest.update(b"\n")
                confirmation_digest = digest.hexdigest()
        finally:
            conn.close()
    except (sqlite3.Error, TypeError, ValueError) as exc:
        raise RolloutError("database_verification_failed") from exc
    if quick != "ok" or foreign_keys or schema != expected_schema:
        raise RolloutError("database_integrity_or_schema_mismatch")
    if expected_schema == TARGET_SCHEMA:
        if not set(REQUIRED_SCHEMA31_TABLES).issubset(tables):
            raise RolloutError("schema31_tables_missing")
        if any(gates.get(key) != "false" for key in PERMANENT_SAFETY_GATES):
            raise RolloutError("permanent_safety_gate_enabled")
        if require_context_disabled and gates.get(CONTEXT_CORE_GATE) != "false":
            raise RolloutError("context_core_gate_enabled")
    return {
        "schema": schema,
        "quick_check": "ok",
        "foreign_key_violations": 0,
        "sha256": _sha256_file(path),
        "mode": "0600",
        "gates": gates,
        "aggregate_counts": aggregate_counts,
        "explicit_confirmation_sha256": confirmation_digest,
    }


def _canonical_runtime_database_identity(core: Path, database: Path) -> tuple[Any, ...]:
    expected_data = core / "data"
    expected_database = expected_data / "activity.sqlite"
    if database != expected_database:
        raise RolloutError("runtime_database_not_canonical")
    try:
        core_metadata = os.lstat(core)
        data_metadata = os.lstat(expected_data)
        database_metadata = os.lstat(database)
    except OSError as exc:
        raise RolloutError("runtime_database_path_invalid") from exc
    if (
        stat.S_ISLNK(core_metadata.st_mode)
        or not stat.S_ISDIR(core_metadata.st_mode)
        or stat.S_ISLNK(data_metadata.st_mode)
        or not stat.S_ISDIR(data_metadata.st_mode)
        or stat.S_IMODE(data_metadata.st_mode) != 0o700
        or stat.S_ISLNK(database_metadata.st_mode)
        or not stat.S_ISREG(database_metadata.st_mode)
        or stat.S_IMODE(database_metadata.st_mode) != 0o600
    ):
        raise RolloutError("runtime_database_path_invalid")
    if database.resolve(strict=True) != expected_data.resolve(strict=True) / database.name:
        raise RolloutError("runtime_database_path_invalid")
    for sidecar in (Path(f"{database}-wal"), Path(f"{database}-shm")):
        if not sidecar.exists() and not sidecar.is_symlink():
            continue
        metadata = os.lstat(sidecar)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise RolloutError("runtime_database_sidecar_invalid")
    return (
        int(data_metadata.st_dev),
        int(data_metadata.st_ino),
        stat.S_IMODE(data_metadata.st_mode),
        int(database_metadata.st_dev),
        int(database_metadata.st_ino),
        stat.S_IMODE(database_metadata.st_mode),
    )


def _assert_legacy_data_preserved(before: dict[str, Any], after: dict[str, Any]) -> None:
    if before.get("aggregate_counts") != after.get("aggregate_counts"):
        raise RolloutError("migration_aggregate_count_mismatch")
    if before.get("explicit_confirmation_sha256") != after.get("explicit_confirmation_sha256"):
        raise RolloutError("explicit_confirmation_mismatch")


def _database_file_identity(path: Path) -> str:
    metadata = path.stat()
    material = f"{int(metadata.st_dev)}:{int(metadata.st_ino)}".encode("ascii")
    return "sha256:" + hashlib.sha256(material).hexdigest()


def _verify_migration_backup(database: Path, *, source_schema: int = SOURCE_SCHEMA) -> dict[str, Any]:
    backup = database.with_name(f"{database.name}.schema{source_schema}-context-backup")
    identity_path = backup.with_name(f"{backup.name}.identity.json")
    backup_probe = _sqlite_probe(backup, expected_schema=source_schema)
    if identity_path.is_symlink() or not identity_path.is_file() or _mode(identity_path) != 0o600:
        raise RolloutError("migration_backup_identity_invalid")
    try:
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RolloutError("migration_backup_identity_invalid") from exc
    expected_fields = {
        "format_version",
        "schema_version",
        "database_identity",
        "source_content_sha256",
        "backup_content_sha256",
    }
    expected_digest = "sha256:" + backup_probe["sha256"]
    if (
        not isinstance(identity, dict)
        or set(identity) != expected_fields
        or identity.get("format_version") != 1
        or identity.get("schema_version") != str(source_schema)
        or identity.get("database_identity") != _database_file_identity(database)
        or identity.get("source_content_sha256") != expected_digest
        or identity.get("backup_content_sha256") != expected_digest
    ):
        raise RolloutError("migration_backup_identity_mismatch")
    return {
        "schema": source_schema,
        "sha256": backup_probe["sha256"],
        "identity_verified": True,
        "mode": "0600",
    }


def _nearest_existing_parent(path: Path) -> Path:
    cursor = path
    while not cursor.exists():
        if cursor == cursor.parent:
            raise RolloutError("path_has_no_existing_parent")
        cursor = cursor.parent
    return cursor


def _same_volume(left: Path, right: Path) -> bool:
    return os.stat(_nearest_existing_parent(left)).st_dev == os.stat(_nearest_existing_parent(right)).st_dev


def _regular_storage_bytes(path: Path) -> int:
    if path.is_symlink() or not path.is_file():
        raise RolloutError("storage_source_not_regular_file")
    try:
        metadata = path.stat()
    except OSError as exc:
        raise RolloutError("storage_source_unreadable") from exc
    return int(metadata.st_size)


def _tree_storage_bytes(
    root: Path,
    *,
    ignored_root_names: set[str] | None = None,
    ignore_python_cache: bool = False,
) -> int:
    if root.is_symlink() or not root.is_dir():
        raise RolloutError("storage_source_tree_invalid")
    ignored = set(ignored_root_names or ())
    total = 0
    for directory, directories, files in os.walk(root, topdown=True, followlinks=False):
        base = Path(directory)
        relative_base = base.relative_to(root)
        if relative_base == Path("."):
            directories[:] = [name for name in directories if name not in ignored]
            files = [name for name in files if name not in ignored]
        if ignore_python_cache:
            directories[:] = [name for name in directories if name not in {"__pycache__", ".pytest_cache"}]
            files = [name for name in files if not name.endswith(".pyc")]
        for name in directories:
            path = base / name
            if path.is_symlink() or not path.is_dir():
                raise RolloutError("storage_source_tree_invalid")
        for name in files:
            path = base / name
            total += _regular_storage_bytes(path)
    return total


def _database_snapshot_storage_bytes(
    path: Path,
    *,
    connection: sqlite3.Connection | None = None,
) -> int:
    if path.is_symlink() or not path.is_file():
        raise RolloutError("storage_database_invalid")
    try:
        conn = connection or sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
        try:
            page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
            page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        finally:
            if connection is None:
                conn.close()
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        raise RolloutError("storage_database_unreadable") from exc
    if page_count <= 0 or page_size <= 0:
        raise RolloutError("storage_database_geometry_invalid")
    return max(STORAGE_DATABASE_MINIMUM_BYTES, page_count * page_size)


def _scaled_database_bytes(database_bytes: int) -> int:
    return (
        database_bytes * STORAGE_DATABASE_GROWTH_NUMERATOR
        + STORAGE_DATABASE_GROWTH_DENOMINATOR
        - 1
    ) // STORAGE_DATABASE_GROWTH_DENOMINATOR


@dataclass(frozen=True)
class StorageRequirement:
    destination: Path
    required_bytes: int
    recovery_bytes: int = 0
    reserve_basis_bytes: int = 0
    minimum_reserve_bytes: int = 0


def _storage_requirement_entries(
    config: RolloutConfig,
    phase: str,
    *,
    database_bytes: int | None = None,
) -> list[StorageRequirement]:
    if phase not in {
        "preflight",
        "shadow",
        "install",
        "install_remaining",
        "install_barrier",
        "install_rename",
    }:
        raise RolloutError("storage_phase_invalid")
    if database_bytes is None:
        database_bytes = _database_snapshot_storage_bytes(config.live_database)
    migrated_database_bytes = _scaled_database_bytes(database_bytes)
    rollback_recovery = database_bytes + STORAGE_OPERATION_JOURNAL_BYTES
    candidate_core_bytes = sum(item.size for item in _git_core_manifest(config))
    candidate_core_bytes += len(
        _read_stable_runtime_file(
            config.live_core / "AGENTS.md",
            expected_mode=0o644,
            maximum_bytes=4 * 1024 * 1024,
        )
    )
    runtime_excludes = set(RUNTIME_DATA_EXCLUDES)
    runtime_excludes.update(
        {
            config.live_database.name,
            f"{config.live_database.name}-wal",
            f"{config.live_database.name}-shm",
        }
    )
    entries: list[StorageRequirement] = [
        StorageRequirement(
            destination=config.workspace,
            required_bytes=STORAGE_OPERATION_JOURNAL_BYTES,
            recovery_bytes=STORAGE_OPERATION_JOURNAL_BYTES,
            reserve_basis_bytes=STORAGE_OPERATION_JOURNAL_BYTES * 2,
        )
    ]
    if phase in {"preflight", "shadow"}:
        entries.append(
            StorageRequirement(
                destination=config.workspace,
                required_bytes=(
                    candidate_core_bytes + migrated_database_bytes + database_bytes
                ),
                recovery_bytes=database_bytes,
                reserve_basis_bytes=(
                    candidate_core_bytes
                    + migrated_database_bytes
                    + database_bytes * 2
                ),
            )
        )
    if phase in {
        "preflight",
        "install",
        "install_remaining",
        "install_barrier",
        "install_rename",
    }:
        candidate_app_bytes = _tree_storage_bytes(config.candidate_app)
        runtime_bytes = _tree_storage_bytes(
            config.live_database.parent,
            ignored_root_names=runtime_excludes,
            ignore_python_cache=True,
        )
        core_peak_basis = (
            candidate_core_bytes
            + runtime_bytes
            + migrated_database_bytes
            + database_bytes * 2
            + rollback_recovery
        )
    if phase in {"preflight", "install"}:
        entries.append(
            StorageRequirement(
                destination=config.live_core.parent,
                required_bytes=candidate_core_bytes
                + runtime_bytes
                + migrated_database_bytes
                + database_bytes * 2,
                recovery_bytes=rollback_recovery,
                reserve_basis_bytes=core_peak_basis,
            )
        )
        entries.append(
            StorageRequirement(
                destination=config.installed_app.parent,
                required_bytes=candidate_app_bytes,
                reserve_basis_bytes=candidate_app_bytes,
            )
        )
    elif phase in {"install_remaining", "install_barrier"}:
        entries.append(
            StorageRequirement(
                destination=config.live_core.parent,
                required_bytes=runtime_bytes + migrated_database_bytes + database_bytes * 2,
                recovery_bytes=rollback_recovery,
                reserve_basis_bytes=core_peak_basis,
            )
        )
        entries.append(
            StorageRequirement(
                destination=config.installed_app.parent,
                required_bytes=0,
                reserve_basis_bytes=candidate_app_bytes,
            )
        )
    elif phase == "install_rename":
        entries.append(
            StorageRequirement(
                destination=config.live_core.parent,
                required_bytes=0,
                recovery_bytes=rollback_recovery,
                reserve_basis_bytes=core_peak_basis,
            )
        )
        entries.append(
            StorageRequirement(
                destination=config.installed_app.parent,
                required_bytes=0,
                reserve_basis_bytes=candidate_app_bytes,
            )
        )
    return entries


def _rollback_storage_requirement_entries(
    config: RolloutConfig,
    state: dict[str, Any],
    artifacts: RolloutArtifacts,
    rollback_preflight_identity: dict[str, Any] | None,
) -> list[StorageRequirement]:
    journal_bytes = (
        _regular_storage_bytes(config.state_path)
        if config.state_path.exists() and not config.state_path.is_symlink()
        else STORAGE_OPERATION_JOURNAL_BYTES
    )
    database_bytes = 0
    if artifacts.rollback_database.exists() or artifacts.rollback_database.is_symlink():
        probe = _sqlite_probe(artifacts.rollback_database, expected_schema=SOURCE_SCHEMA)
        expected_hash = state.get("rollback_database_sha256")
        if not isinstance(expected_hash, str) or probe.get("sha256") != expected_hash:
            raise RolloutError("rollback_storage_database_identity_mismatch")
        database_bytes = _database_snapshot_storage_bytes(artifacts.rollback_database)
    elif state.get("phase") not in {"install_prepared", "install_failed_recovery_required"}:
        raise RolloutError("rollback_storage_database_missing")
    phase = str(state.get("phase") or "")
    recovery_required = True
    if rollback_preflight_identity is not None:
        if not isinstance(rollback_preflight_identity, dict):
            raise RolloutError("rollback_storage_preflight_identity_missing")
        recovery_value = rollback_preflight_identity.get(
            "rollback_database_recovery_required"
        )
        if not isinstance(recovery_value, bool):
            raise RolloutError("rollback_storage_recovery_status_invalid")
        recovery_required = recovery_value
    elif phase == "installed":
        raise RolloutError("rollback_storage_preflight_identity_missing")
    elif phase in {"rollback_prepared", "rollback_failed_recovery_required"}:
        preflight = state.get("rollback_preflight_identity")
        recovery_value = (
            preflight.get("rollback_database_recovery_required")
            if isinstance(preflight, dict)
            else None
        )
        recovery_required = recovery_value if isinstance(recovery_value, bool) else True
    plist_bytes = 0
    for path in (artifacts.old_plist, artifacts.old_shadow_plist):
        if path.exists() or path.is_symlink():
            plist_bytes += _regular_storage_bytes(path)
    return [
        StorageRequirement(
            destination=config.live_core.parent,
            required_bytes=database_bytes if recovery_required else 0,
            recovery_bytes=0,
            reserve_basis_bytes=database_bytes if recovery_required else 0,
        ),
        StorageRequirement(
            destination=config.launch_agent_plist.parent,
            required_bytes=plist_bytes,
            reserve_basis_bytes=plist_bytes,
        ),
        StorageRequirement(
            destination=config.workspace,
            required_bytes=max(STORAGE_OPERATION_JOURNAL_BYTES, journal_bytes * 2),
            reserve_basis_bytes=max(STORAGE_OPERATION_JOURNAL_BYTES, journal_bytes * 2),
        ),
    ]


def _storage_headroom(
    config: RolloutConfig,
    phase: str,
    *,
    database_bytes: int | None = None,
    state: dict[str, Any] | None = None,
    artifacts: RolloutArtifacts | None = None,
    rollback_preflight_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    grouped: dict[int, dict[str, int]] = {}
    requirements = (
        _rollback_storage_requirement_entries(
            config,
            state,
            artifacts,
            rollback_preflight_identity,
        )
        if phase == "rollback" and state is not None and artifacts is not None
        else _storage_requirement_entries(config, phase, database_bytes=database_bytes)
    )
    for requirement in requirements:
        anchor = _nearest_existing_parent(requirement.destination)
        try:
            metadata = anchor.stat()
            filesystem = os.statvfs(anchor)
        except OSError as exc:
            raise RolloutError("storage_volume_unreadable") from exc
        device = int(metadata.st_dev)
        free_bytes = int(filesystem.f_bavail) * int(filesystem.f_frsize)
        volume = grouped.setdefault(
            device,
            {
                "required_bytes": 0,
                "recovery_bytes": 0,
                "reserve_basis_bytes": 0,
                "minimum_reserve_bytes": 0,
                "free_bytes": free_bytes,
            },
        )
        fragment = max(1, int(filesystem.f_frsize))

        def rounded(value: int) -> int:
            value = max(0, int(value))
            return ((value + fragment - 1) // fragment) * fragment

        volume["required_bytes"] += rounded(requirement.required_bytes)
        volume["recovery_bytes"] += rounded(requirement.recovery_bytes)
        volume["reserve_basis_bytes"] += rounded(requirement.reserve_basis_bytes)
        volume["minimum_reserve_bytes"] = max(
            volume["minimum_reserve_bytes"],
            rounded(requirement.minimum_reserve_bytes),
        )
        volume["free_bytes"] = min(volume["free_bytes"], free_bytes)

    volumes: list[dict[str, Any]] = []
    insufficient = False
    for device, values in sorted(grouped.items()):
        required_bytes = values["required_bytes"]
        recovery_bytes = values["recovery_bytes"]
        reserve_basis_bytes = max(
            values["reserve_basis_bytes"],
            required_bytes + recovery_bytes,
        )
        percentage_reserve = (reserve_basis_bytes + 3) // 4
        reserve_bytes = max(
            STORAGE_RELEASE_MINIMUM_RESERVE_BYTES,
            percentage_reserve,
            values["minimum_reserve_bytes"],
        )
        free_bytes = values["free_bytes"]
        available_after_bytes = free_bytes - required_bytes - recovery_bytes
        status = "ready" if available_after_bytes >= reserve_bytes else "insufficient"
        insufficient = insufficient or status != "ready"
        volume_ref = "sha256:" + hashlib.sha256(str(device).encode("ascii")).hexdigest()
        volumes.append(
            {
                "volume_ref": volume_ref,
                "free_bytes": free_bytes,
                "required_bytes": required_bytes,
                "recovery_bytes": recovery_bytes,
                "reserve_basis_bytes": values["reserve_basis_bytes"],
                "reserve_bytes": reserve_bytes,
                "available_after_bytes": available_after_bytes,
                "status": status,
            }
        )
    if insufficient or not volumes:
        raise RolloutError("insufficient_storage_headroom")
    return {
        "status": "ready",
        "phase": phase,
        "journal_reserve_bytes": STORAGE_OPERATION_JOURNAL_BYTES,
        "volumes": volumes,
    }


def _assert_release_identity(config: RolloutConfig) -> dict[str, Any]:
    _validate_path_relationships(config)
    _resolved_without_symlink(config.source_root, "source_root")
    _resolved_without_symlink(config.candidate_app, "candidate_app")
    _resolved_without_symlink(config.live_core, "live_core")
    _resolved_without_symlink(config.live_database, "live_database")
    _resolved_without_symlink(config.installed_app, "installed_app")
    _resolved_without_symlink(config.launch_agent_plist, "launch_agent_plist")
    _resolved_without_symlink(
        config.shadow_launch_agent_plist,
        "shadow_launch_agent_plist",
    )
    _assert_git_repository_controls(config)
    revision = _git_output(config, "rev-parse", "HEAD")
    if revision != config.expected_revision:
        raise RolloutError("source_revision_mismatch")
    release_chain = _verify_release_commit_chain(config, revision)
    if _git_output(
        config,
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "status",
        "--porcelain=v1",
        "--untracked-files=normal",
    ):
        raise RolloutError("source_tree_dirty")
    source_tree_hash = _source_tree_hash(config)
    git_core_manifest = _git_core_manifest(config)
    source_core_hash = _git_core_code_hash(config, git_core_manifest)
    if source_tree_hash != config.expected_source_tree_sha256:
        raise RolloutError("source_tree_hash_mismatch")
    if source_core_hash != config.expected_core_code_sha256:
        raise RolloutError("source_core_hash_mismatch")
    if _git_core_version(config, git_core_manifest) != RELEASE_VERSION:
        raise RolloutError("source_core_version_mismatch")
    candidate = _app_info(config.candidate_app)
    if (
        candidate.get("CFBundleIdentifier") != "app.personalai.monitor"
        or candidate.get("CFBundleShortVersionString") != RELEASE_VERSION
        or candidate.get("PAMGitRevision") != config.expected_revision[:12]
        or candidate.get("PAMGitDirty") is not False
        or candidate.get("PAMSourceTreeHash") != config.expected_source_tree_sha256
        or candidate.get("PAMCoreCodeHash") != config.expected_core_code_sha256
    ):
        raise RolloutError("candidate_app_identity_mismatch")
    candidate_app_payload = _assert_expected_app_identity(
        config,
        config.candidate_app,
        error="candidate_app_payload_identity_mismatch",
    )
    installed = _app_info(config.installed_app)
    if (
        installed.get("CFBundleIdentifier") != "app.personalai.monitor"
        or installed.get("CFBundleShortVersionString") != ROLLBACK_VERSION
    ):
        raise RolloutError("installed_rollback_identity_mismatch")
    if _core_version(config.live_core) != ROLLBACK_VERSION:
        raise RolloutError("live_rollback_core_version_mismatch")
    live_core_full_identity = _runtime_core_full_identity(config.live_core)
    live_agents_sha256 = _verify_runtime_agents(config.live_core)
    remote_ci = _verify_remote_ci(config)
    _verify_app_xattrs(config.candidate_app)
    _verify_signature(config.installed_app)
    _verify_app_xattrs(config.installed_app)
    core_agent = _verify_core_launch_agent(config)
    shadow_agent = _verify_shadow_launch_agent(config)
    interpreter = _assert_matching_interpreter_identity(core_agent, shadow_agent)
    if _mode(config.live_database.parent) != 0o700:
        raise RolloutError("live_data_directory_permissions_invalid")
    database = _sqlite_probe(config.live_database, expected_schema=SOURCE_SCHEMA)
    if not _same_volume(config.installed_app, config.installed_app.parent / ".pam-phase3-rollbacks"):
        raise RolloutError("app_rename_cross_volume")
    if not _same_volume(config.live_core, config.live_core.parent / ".pam-phase3-rollbacks"):
        raise RolloutError("core_rename_cross_volume")
    return {
        "revision": revision,
        "baseline_revision": BASELINE_REVISION,
        "release_commit_chain": release_chain["commit_chain"],
        "source_archive_evidence": {
            "phase01": release_chain["phase01_archive_evidence"],
            "phase2": release_chain["phase2_archive_evidence"],
        },
        "source_tree_sha256": source_tree_hash,
        "core_code_sha256": source_core_hash,
        "git_core_identity": _git_core_manifest_identity(config, git_core_manifest),
        "candidate_app_version": RELEASE_VERSION,
        "candidate_app_payload": candidate_app_payload,
        "installed_app_version": ROLLBACK_VERSION,
        "installed_app_content_mode_sha256": _tree_content_mode_hash(config.installed_app),
        "installed_app_executable_snapshot": list(_app_executable_snapshot(config)),
        "live_core_version": ROLLBACK_VERSION,
        "live_core_code_sha256": _core_code_hash(config.live_core),
        "live_core_full_identity": live_core_full_identity,
        "live_agents_sha256": live_agents_sha256,
        "launch_agent_plist_sha256": _sha256_file(config.launch_agent_plist),
        "shadow_launch_agent_plist_sha256": _sha256_file(config.shadow_launch_agent_plist),
        "shadow_core_database_pair": shadow_agent["shadow_pair"],
        "python_interpreter": interpreter,
        "database": database,
        "remote_ci": remote_ci,
    }


def _system_integrity_protection_identity() -> dict[str, str]:
    completed = _run(["csrutil", "status"])
    expected = "System Integrity Protection status: enabled."
    if (
        completed.returncode != 0
        or completed.stderr.strip()
        or completed.stdout.strip() != expected
    ):
        raise RolloutError("system_integrity_protection_required")
    return {
        "status": "enabled",
        "executable": SYSTEM_EXECUTABLES["csrutil"],
    }


def _assert_secure_release_identity(config: RolloutConfig) -> dict[str, Any]:
    identity = _assert_release_identity(config)
    identity["system_integrity_protection"] = _system_integrity_protection_identity()
    return identity


def _verify_candidate_code(
    config: RolloutConfig,
    *,
    app: Path,
    core: Path,
) -> dict[str, Any]:
    info = _app_info(app)
    if (
        info.get("CFBundleIdentifier") != "app.personalai.monitor"
        or info.get("CFBundleShortVersionString") != RELEASE_VERSION
        or info.get("PAMGitRevision") != config.expected_revision[:12]
        or info.get("PAMGitDirty") is not False
        or info.get("PAMSourceTreeHash") != config.expected_source_tree_sha256
        or info.get("PAMCoreCodeHash") != config.expected_core_code_sha256
    ):
        raise RolloutError("staged_app_identity_mismatch")
    app_payload = _assert_expected_app_identity(
        config,
        app,
        error="staged_app_payload_identity_mismatch",
    )
    _verify_app_xattrs(app)
    if _core_version(core) != RELEASE_VERSION:
        raise RolloutError("staged_core_version_mismatch")
    core_hash = _core_code_hash(core)
    if core_hash != config.expected_core_code_sha256:
        raise RolloutError("staged_core_hash_mismatch")
    return {
        "app_version": RELEASE_VERSION,
        "app_payload": app_payload,
        "core_version": RELEASE_VERSION,
        "core_code_sha256": core_hash,
        "signature_valid": True,
        "quarantine_xattr_absent": True,
    }


def _verify_candidate_runtime(
    config: RolloutConfig,
    *,
    app: Path,
    core: Path,
    database: Path,
    require_context_disabled: bool = True,
) -> dict[str, Any]:
    code_identity = _verify_candidate_code(config, app=app, core=core)
    database_identity_before = _canonical_runtime_database_identity(core, database)
    database_probe = _sqlite_probe(
        database,
        expected_schema=TARGET_SCHEMA,
        require_context_disabled=require_context_disabled,
    )
    migration_backup = _verify_migration_backup(database)
    database_identity_after = _canonical_runtime_database_identity(core, database)
    if database_identity_before != database_identity_after:
        raise RolloutError("runtime_database_identity_changed")
    return {
        **code_identity,
        "database": database_probe,
        "migration_backup": migration_backup,
    }


def _config_fingerprint(config: RolloutConfig) -> str:
    payload = {
        "source_root": str(config.source_root),
        "candidate_app": str(config.candidate_app),
        "live_core": str(config.live_core),
        "live_database": str(config.live_database),
        "installed_app": str(config.installed_app),
        "launch_agent_plist": str(config.launch_agent_plist),
        "workspace": str(config.workspace),
        "expected_revision": config.expected_revision,
        "expected_source_tree_sha256": config.expected_source_tree_sha256,
        "expected_core_code_sha256": config.expected_core_code_sha256,
        "expected_app_payload_sha256": config.expected_app_payload_sha256,
        "expected_app_executable_sha256": config.expected_app_executable_sha256,
        "expected_app_cdhash": config.expected_app_cdhash,
        "expected_app_signature_kind": config.expected_app_signature_kind,
        "expected_app_team_identifier": config.expected_app_team_identifier,
        "gh_executable": str(config.gh_executable),
        "expected_gh_sha256": config.expected_gh_sha256,
        "service_label": config.service_label,
        "shadow_launch_agent_label": config.shadow_launch_agent_label,
        "shadow_launch_agent_plist": str(config.shadow_launch_agent_plist),
        "expected_privacy_profile": config.expected_privacy_profile,
        "github_repository": config.github_repository,
        "github_actions_run_id": config.github_actions_run_id,
        "release_branch": config.release_branch,
        "release_candidate_tag": config.release_candidate_tag,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _ensure_private_directory(path: Path) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_dir() or _mode(path) != 0o700:
            raise RolloutError("private_directory_invalid")
        return
    path.mkdir(parents=True, mode=0o700)
    path.chmod(0o700)


def _fsync_directory(path: Path) -> None:
    """Persist a directory-entry change before journaling it as complete."""
    if path.is_symlink() or not path.is_dir():
        raise RolloutError("durability_directory_invalid")
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise RolloutError("durability_directory_fsync_failed") from exc


def _fsync_regular_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise RolloutError("durability_regular_file_invalid")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise RolloutError("durability_regular_file_invalid")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except RolloutError:
        raise
    except OSError as exc:
        raise RolloutError("durability_file_fsync_failed") from exc


def _fsync_tree(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise RolloutError("durability_tree_invalid")
    for directory, directories, files in os.walk(root, topdown=False, followlinks=False):
        base = Path(directory)
        for name in directories:
            child = base / name
            if child.is_symlink() or not child.is_dir():
                raise RolloutError("durability_tree_special_entry")
        for name in files:
            path = base / name
            if path.is_symlink() or not path.is_file():
                raise RolloutError("durability_tree_special_entry")
            _fsync_regular_file(path)
        _fsync_directory(base)


def _durable_rename(source: Path, destination: Path) -> None:
    """Rename without deletion, then make both affected directory entries durable."""
    source_parent = source.parent
    destination_parent = destination.parent
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_fd = os.open(source_parent, flags)
        destination_fd = os.open(destination_parent, flags)
        source_parent_before = os.fstat(source_fd)
        destination_parent_before = os.fstat(destination_fd)
        if not stat.S_ISDIR(source_parent_before.st_mode) or not stat.S_ISDIR(
            destination_parent_before.st_mode
        ):
            raise RolloutError("durable_rename_parent_invalid")
        try:
            os.stat(destination.name, dir_fd=destination_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise RolloutError("durable_rename_destination_exists")
        libc = ctypes.CDLL(None, use_errno=True)
        renameatx = libc.renameatx_np
        renameatx.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameatx.restype = ctypes.c_int
        result = renameatx(
            source_fd,
            os.fsencode(source.name),
            destination_fd,
            os.fsencode(destination.name),
            0x00000004,  # RENAME_EXCL: atomically fail if destination exists.
        )
        if result != 0:
            error_number = ctypes.get_errno()
            if error_number == errno.EEXIST:
                raise RolloutError("durable_rename_destination_exists")
            raise OSError(error_number, os.strerror(error_number))
        os.fsync(source_fd)
        if destination_fd != source_fd:
            os.fsync(destination_fd)
        for path, expected in (
            (source_parent, source_parent_before),
            (destination_parent, destination_parent_before),
        ):
            observed = os.stat(path, follow_symlinks=False)
            if (
                observed.st_dev,
                observed.st_ino,
            ) != (
                expected.st_dev,
                expected.st_ino,
            ):
                raise RolloutError("durable_rename_parent_identity_changed")
    except AttributeError as exc:
        raise RolloutError("exclusive_rename_unavailable") from exc
    except RolloutError:
        raise
    except OSError as exc:
        raise RolloutError("durable_rename_failed") from exc
    finally:
        for name in ("source_fd", "destination_fd"):
            descriptor = locals().get(name)
            if isinstance(descriptor, int):
                try:
                    os.close(descriptor)
                except OSError:
                    pass


@contextmanager
def _rollout_lock(config: RolloutConfig) -> Iterator[None]:
    """Serialize every mutating command without deleting the lock artifact."""
    parent = config.workspace.parent
    if parent.is_symlink() or not parent.is_dir():
        raise RolloutError("rollout_lock_parent_invalid")
    lock_path = parent / f".{config.workspace.name}.phase3-rollout.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
        lock_path.chmod(0o600)
        if _mode(lock_path) != 0o600:
            raise RolloutError("rollout_lock_permissions_invalid")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RolloutError("rollout_already_in_progress") from exc
        yield
    except OSError as exc:
        raise RolloutError("rollout_lock_unavailable") from exc
    finally:
        if "descriptor" in locals():
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _write_state(config: RolloutConfig, state: dict[str, Any]) -> None:
    _ensure_private_directory(config.workspace)
    payload = {**state, "config_fingerprint": _config_fingerprint(config), "updated_at": _utc_now()}
    temporary = config.workspace / f".{STATE_FILENAME}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, config.state_path)
        _fsync_directory(config.workspace)
        if _mode(config.state_path) != 0o600 or _read_json(config.state_path) != payload:
            raise RolloutError("rollout_state_permissions_invalid")
    except Exception:
        # The unique private temporary is deliberately retained for forensic
        # recovery instead of being automatically deleted.
        raise


def _load_state(config: RolloutConfig) -> dict[str, Any]:
    state = _read_json(config.state_path)
    if state.get("config_fingerprint") != _config_fingerprint(config):
        raise RolloutError("rollout_state_config_mismatch")
    return state


def _sqlite_backup(source: Path, destination: Path) -> dict[str, Any]:
    if source.is_symlink() or not source.is_file() or _mode(source) != 0o600:
        raise RolloutError("backup_source_invalid")
    if destination.exists() or destination.is_symlink():
        raise RolloutError("backup_destination_exists")
    _ensure_private_directory(destination.parent)
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        destination.chmod(0o600)
        source_conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=2.0)
        destination_conn = sqlite3.connect(destination)
        try:
            source_conn.backup(destination_conn)
        finally:
            destination_conn.close()
            source_conn.close()
        destination.chmod(0o600)
        _fsync_regular_file(destination)
        for sidecar in (Path(f"{destination}-wal"), Path(f"{destination}-shm")):
            if sidecar.exists() or sidecar.is_symlink():
                _fsync_regular_file(sidecar)
        _fsync_directory(destination.parent)
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        raise RolloutError("sqlite_backup_failed") from exc
    return _sqlite_probe(destination, expected_schema=SOURCE_SCHEMA)


def _migrate_with_candidate_core(
    config: RolloutConfig,
    database: Path,
    *,
    interpreter_identity: dict[str, Any],
    core_root: Path,
    expected_materialized_identity: dict[str, Any],
    expected_git_materialized_identity: dict[str, Any],
    expected_agents_sha256: str,
) -> None:
    candidate_core = core_root
    try:
        resolved_candidate = candidate_core.resolve(strict=True)
        resolved_source = config.source_root.resolve(strict=True)
    except OSError as exc:
        raise RolloutError("migration_core_invalid") from exc
    if resolved_candidate == resolved_source or resolved_source in resolved_candidate.parents:
        raise RolloutError("migration_worktree_core_forbidden")
    expected_from_git = _git_core_manifest_identity(
        config,
        _git_core_manifest(config),
    )
    if expected_git_materialized_identity != {
        key: expected_from_git[key]
        for key in (
            "entry_count",
            "logical_bytes",
            "content_mode_manifest_sha256",
        )
    }:
        raise RolloutError("migration_git_core_identity_drift")
    _assert_git_materialized_core_identity(
        candidate_core,
        expected_git_materialized_identity,
        expected_agents_sha256,
    )
    _assert_materialized_core_identity(
        candidate_core,
        expected_materialized_identity,
    )
    if not isinstance(interpreter_identity, dict):
        raise RolloutError("migration_interpreter_identity_missing")
    program = interpreter_identity.get("program")
    if not isinstance(program, str) or not program:
        raise RolloutError("migration_interpreter_identity_missing")
    if _trusted_python_interpreter(program) != interpreter_identity:
        raise RolloutError("migration_interpreter_identity_drift")
    script = (
        "import sys;sys.dont_write_bytecode=True;sys.path.insert(0,sys.argv[1]);"
        "from activity_mvp.storage import ensure_ready;"
        "c=ensure_ready(sys.argv[2]);c.close()"
    )
    try:
        completed = subprocess.run(
            [program, "-I", "-S", "-c", script, str(candidate_core), str(database)],
            cwd=Path("/"),
            env={"LANG": "C", "LC_ALL": "C", "PATH": SYSTEM_EXECUTION_PATH},
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RolloutError("shadow_migration_unavailable") from exc
    if completed.returncode != 0:
        raise RolloutError("shadow_migration_failed")
    _assert_git_materialized_core_identity(
        candidate_core,
        expected_git_materialized_identity,
        expected_agents_sha256,
    )
    _assert_materialized_core_identity(
        candidate_core,
        expected_materialized_identity,
    )


def _set_initial_rollout_gates_off(database: Path) -> None:
    """Disable rollout gates once on the isolated schema-31 candidate.

    The Core must not permanently clamp the UI/Core feature flag on every
    startup because the owner explicitly enables it after old-UI verification.
    This mutation is therefore restricted to the shadow/staging database.
    """
    try:
        conn = sqlite3.connect(database, timeout=2.0)
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = _utc_now()
            for key in INITIAL_DISABLED_GATES:
                conn.execute(
                    "INSERT INTO settings(key,value,updated_at) VALUES(?, 'false', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value='false',updated_at=excluded.updated_at",
                    (key, now),
                )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise RolloutError("initial_rollout_gate_write_failed") from exc


def _read_release_gate_status_for_rollback(database: Path) -> dict[str, Any]:
    status = {
        "context_core_gate": "unknown",
        "permanent_safety_gates": {key: "unknown" for key in PERMANENT_SAFETY_GATES},
    }
    if database.is_symlink() or not database.is_file():
        return status
    try:
        conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=0.5)
        try:
            schema_row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if schema_row is None or int(schema_row[0]) != TARGET_SCHEMA:
                return status
            rows = {
                str(row[0]): str(row[1]).lower()
                for row in conn.execute(
                    "SELECT key,value FROM settings WHERE key IN (?,?,?,?)",
                    INITIAL_DISABLED_GATES,
                ).fetchall()
            }
        finally:
            conn.close()
    except (sqlite3.Error, TypeError, ValueError):
        return status
    status["context_core_gate"] = rows.get(CONTEXT_CORE_GATE, "unknown")
    status["permanent_safety_gates"] = {
        key: rows.get(key, "unknown") for key in PERMANENT_SAFETY_GATES
    }
    return status


def _write_context_core_gate_for_rollback(
    database: Path,
    value: str,
    *,
    required: bool,
) -> str:
    if value not in {"true", "false"}:
        if required:
            raise RolloutError("context_core_gate_restore_value_invalid")
        return "unavailable"
    if database.is_symlink() or not database.is_file():
        if required:
            raise RolloutError("context_core_gate_database_invalid")
        return "unavailable"
    try:
        before = database.stat()
        read_only = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=0.5)
        try:
            schema_row = read_only.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
        finally:
            read_only.close()
        if schema_row is None or int(schema_row[0]) != TARGET_SCHEMA:
            raise sqlite3.DatabaseError("not schema31")
        after = database.stat()
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise sqlite3.DatabaseError("database identity changed")
        conn = sqlite3.connect(f"file:{database}?mode=rw", uri=True, timeout=1.0)
        try:
            conn.execute("BEGIN IMMEDIATE")
            schema_row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if schema_row is None or int(schema_row[0]) != TARGET_SCHEMA:
                raise sqlite3.DatabaseError("not schema31")
            conn.execute(
                "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (CONTEXT_CORE_GATE, value, _utc_now()),
            )
            conn.commit()
        finally:
            conn.close()
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        if required:
            raise RolloutError("context_core_gate_restore_failed") from exc
        return "unavailable"
    return "applied"


def preflight(config: RolloutConfig) -> dict[str, Any]:
    identity = _assert_secure_release_identity(config)
    storage = _storage_headroom(config, "preflight")
    return {
        "status": "ready",
        "phase": "preflight",
        "mode": "read_only",
        "identity": identity,
        "planned_order": [
            "sqlite_backup_rehearsal",
            "direct_launchctl_bootout",
            "active_handle_check",
            "sqlite_backup_final",
            "schema31_stage_migration",
            "same_volume_core_rename",
            "same_volume_app_rename",
            "launchctl_bootstrap",
            "installed_verification",
        ],
        "storage": storage,
        "raw_access": False,
    }


def shadow(config: RolloutConfig, *, apply: bool, confirmation: str) -> dict[str, Any]:
    _validate_path_relationships(config)
    shadow_dir = config.workspace / "shadow"
    shadow_db = shadow_dir / "activity.sqlite"
    shadow_core = shadow_dir / "core"
    if not apply:
        identity = _assert_secure_release_identity(config)
        storage = _storage_headroom(config, "shadow")
        return {
            "status": "planned",
            "phase": "shadow",
            "mode": "dry_run",
            "source_schema": SOURCE_SCHEMA,
            "target_schema": TARGET_SCHEMA,
            "would_create_private_workspace": True,
            "would_use_sqlite_backup_api": True,
            "would_open_live_database_with_candidate_core": False,
            "would_execute_git_materialized_core_only": True,
            "system_integrity_protection": identity[
                "system_integrity_protection"
            ],
            "storage": storage,
            "raw_access": False,
        }
    if confirmation != SHADOW_CONFIRMATION:
        raise RolloutError("shadow_confirmation_required")
    with _rollout_lock(config):
        identity = _assert_secure_release_identity(config)
        storage = _storage_headroom(config, "shadow")
        if config.workspace.exists() or shadow_db.exists():
            raise RolloutError("shadow_workspace_must_be_new")
        _ensure_private_directory(config.workspace)
        _ensure_private_directory(shadow_dir)
        shadow_git_identity = _copy_candidate_core(config, shadow_core)
        shadow_agents_sha256 = _copy_live_runtime_agents(
            config.live_core,
            shadow_core,
            expected_sha256=str(identity["live_agents_sha256"]),
        )
        _fsync_tree(shadow_core)
        shadow_materialized_identity = _materialized_core_identity(shadow_core)
        _assert_git_materialized_core_identity(
            shadow_core,
            shadow_git_identity["materialized"],
            shadow_agents_sha256,
        )
        _assert_materialized_core_identity(
            shadow_core,
            shadow_materialized_identity,
        )
        source_backup = _sqlite_backup(config.live_database, shadow_db)
        _migrate_with_candidate_core(
            config,
            shadow_db,
            interpreter_identity=_recorded_interpreter_identity(identity),
            core_root=shadow_core,
            expected_materialized_identity=shadow_materialized_identity,
            expected_git_materialized_identity=shadow_git_identity["materialized"],
            expected_agents_sha256=shadow_agents_sha256,
        )
        _assert_materialized_core_identity(
            shadow_core,
            shadow_materialized_identity,
        )
        _verify_runtime_agents(shadow_core, shadow_agents_sha256)
        _set_initial_rollout_gates_off(shadow_db)
        migrated = _sqlite_probe(shadow_db, expected_schema=TARGET_SCHEMA)
        migration_backup = _verify_migration_backup(shadow_db)
        _assert_legacy_data_preserved(source_backup, migrated)
        if migration_backup["sha256"] != source_backup["sha256"]:
            raise RolloutError("shadow_source_backup_mismatch")
        state = {
            "phase": "shadow_verified",
            "run_id": uuid.uuid4().hex,
            "source_identity": identity,
            "shadow_database": str(shadow_db),
            "shadow_migration_core": str(shadow_core),
            "shadow_migration_core_git_identity": shadow_git_identity,
            "shadow_migration_core_materialized_identity": shadow_materialized_identity,
            "shadow_migration_core_agents_sha256": shadow_agents_sha256,
            "source_backup": source_backup,
            "shadow_verified": migrated,
            "migration_backup_verified": migration_backup,
            "storage_preflight": storage,
        }
        _write_state(config, state)
    return {
        "status": "verified",
        "phase": "shadow",
        "mode": "apply",
        "source_schema": SOURCE_SCHEMA,
        "target_schema": TARGET_SCHEMA,
        "shadow_sha256": migrated["sha256"],
        "shadow_core_manifest_sha256": shadow_materialized_identity[
            "content_mode_manifest_sha256"
        ],
        "raw_access": False,
    }


def _verify_shadow(config: RolloutConfig) -> dict[str, Any]:
    state = _load_state(config)
    if state.get("phase") not in {"shadow_verified", "install_prepared", "installed", "rolled_back"}:
        raise RolloutError("shadow_state_invalid")
    shadow_db = _validated_path(state.get("shadow_database"), "shadow_database")
    expected_shadow_directory = config.workspace / "shadow"
    if shadow_db != expected_shadow_directory / "activity.sqlite":
        raise RolloutError("shadow_database_path_invalid")
    if (
        config.workspace.is_symlink()
        or not config.workspace.is_dir()
        or _mode(config.workspace) != 0o700
        or expected_shadow_directory.is_symlink()
        or not expected_shadow_directory.is_dir()
        or _mode(expected_shadow_directory) != 0o700
        or shadow_db.is_symlink()
        or not shadow_db.is_file()
        or _mode(shadow_db) != 0o600
    ):
        raise RolloutError("shadow_workspace_permissions_invalid")
    shadow_core = _validated_path(
        state.get("shadow_migration_core"),
        "shadow_migration_core",
    )
    if shadow_core != config.workspace / "shadow" / "core":
        raise RolloutError("shadow_migration_core_path_invalid")
    source_identity = state.get("source_identity")
    recorded_git_identity = state.get("shadow_migration_core_git_identity")
    recorded_git_materialized = _assert_recorded_git_core_identity(
        config,
        recorded_git_identity,
    )
    if (
        not isinstance(source_identity, dict)
        or not isinstance(recorded_git_identity, dict)
        or recorded_git_identity.get("git") != source_identity.get("git_core_identity")
    ):
        raise RolloutError("shadow_migration_core_git_identity_invalid")
    _assert_materialized_core_identity(
        shadow_core,
        state.get("shadow_migration_core_materialized_identity"),
    )
    agents_sha256 = state.get("shadow_migration_core_agents_sha256")
    if not isinstance(agents_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", agents_sha256
    ):
        raise RolloutError("shadow_migration_core_agents_identity_invalid")
    _verify_runtime_agents(shadow_core, agents_sha256)
    _assert_git_materialized_core_identity(
        shadow_core,
        recorded_git_materialized,
        agents_sha256,
    )
    verified = _sqlite_probe(shadow_db, expected_schema=TARGET_SCHEMA)
    expected = state.get("shadow_verified") or {}
    if verified["sha256"] != expected.get("sha256"):
        raise RolloutError("shadow_database_drift")
    migration_backup = _verify_migration_backup(shadow_db)
    if migration_backup != state.get("migration_backup_verified"):
        raise RolloutError("shadow_migration_backup_drift")
    source_backup = state.get("source_backup")
    if (
        not isinstance(source_backup, dict)
        or migration_backup.get("sha256") != source_backup.get("sha256")
    ):
        raise RolloutError("shadow_source_backup_mismatch")
    return {
        "status": "verified",
        "phase": "verify",
        "target": "shadow",
        "schema": TARGET_SCHEMA,
        "sha256": verified["sha256"],
        "migration_backup_verified": True,
        "shadow_core_manifest_sha256": state[
            "shadow_migration_core_materialized_identity"
        ]["content_mode_manifest_sha256"],
        "raw_access": False,
    }


def _launch_target() -> str:
    return f"gui/{os.getuid()}"


def _launch_agent_loaded(label: str) -> bool:
    completed = _run(["launchctl", "print", f"{_launch_target()}/{label}"])
    if completed.returncode == 0:
        return True
    missing = f"Could not find service \"{label}\"" in completed.stderr or "service not found" in completed.stderr.lower()
    if completed.returncode == 113 and missing:
        return False
    raise RolloutError("launch_agent_state_unknown")


def _assert_launch_agent_stopped(label: str) -> None:
    if _launch_agent_loaded(label):
        raise RolloutError("launch_agent_still_running")


def _bootout_plist(label: str, plist: Path) -> None:
    completed = _run(
        ["launchctl", "bootout", _launch_target(), str(plist)],
        timeout=15.0,
    )
    if completed.returncode != 0 and _launch_agent_loaded(label):
        raise RolloutError("launch_agent_bootout_failed")
    _assert_launch_agent_stopped(label)


def _bootout_label(label: str) -> None:
    completed = _run(
        ["launchctl", "bootout", f"{_launch_target()}/{label}"],
        timeout=15.0,
    )
    if completed.returncode != 0 and _launch_agent_loaded(label):
        raise RolloutError("launch_agent_bootout_failed")
    _assert_launch_agent_stopped(label)


def _bootout(config: RolloutConfig) -> None:
    _bootout_plist(config.service_label, config.launch_agent_plist)


def _bootout_shadow(config: RolloutConfig) -> None:
    _bootout_plist(config.shadow_launch_agent_label, config.shadow_launch_agent_plist)


def _quit_app() -> None:
    completed = _run(
        [
            "osascript",
            "-e",
            'tell application id "app.personalai.monitor" to quit',
        ],
        timeout=10.0,
    )
    # AppleScript returns non-zero when the App is already absent; process
    # ownership is authoritatively checked after bootout.
    if completed.returncode not in {0, 1}:
        raise RolloutError("app_quit_failed")


def _app_pids(config: RolloutConfig) -> set[int]:
    executable = config.installed_app / "Contents" / "MacOS" / "PersonalAIMonitor"
    expression = f"^{re.escape(str(executable))}([[:space:]]|$)"
    completed = _run(["pgrep", "-U", str(os.getuid()), "-f", expression], timeout=10.0)
    if completed.returncode == 0 and completed.stdout.strip():
        try:
            pids = {int(value) for value in completed.stdout.split()}
        except ValueError as exc:
            raise RolloutError("installed_app_process_check_failed") from exc
        if not pids or any(pid <= 0 for pid in pids):
            raise RolloutError("installed_app_process_check_failed")
        return pids
    if completed.returncode not in {0, 1}:
        raise RolloutError("installed_app_process_check_failed")
    return set()


def _app_bundle_executable_snapshot(app: Path) -> tuple[Any, ...]:
    executable = app / "Contents" / "MacOS" / "PersonalAIMonitor"
    try:
        metadata = os.lstat(executable)
    except OSError as exc:
        raise RolloutError("installed_app_executable_invalid") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or not os.access(executable, os.X_OK)
    ):
        raise RolloutError("installed_app_executable_invalid")
    return (
        str(executable.resolve(strict=True)),
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        _sha256_file(executable),
    )


def _app_executable_snapshot(config: RolloutConfig) -> tuple[Any, ...]:
    return _app_bundle_executable_snapshot(config.installed_app)


def _recorded_app_executable_snapshot(value: Any) -> tuple[Any, ...]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 6
        or not isinstance(value[0], str)
        or not all(isinstance(item, int) and item >= 0 for item in value[1:5])
        or not isinstance(value[5], str)
        or re.fullmatch(r"[0-9a-f]{64}", value[5]) is None
    ):
        raise RolloutError("app_executable_snapshot_invalid")
    return tuple(value)


def _verified_app_artifact_snapshot_for_signal(
    config: RolloutConfig,
    app: Path,
    *,
    release: bool,
    source_identity: dict[str, Any] | None = None,
    recorded_snapshot: Any = None,
) -> tuple[Any, ...]:
    info = _app_info(app)
    if release:
        if info.get("CFBundleShortVersionString") != RELEASE_VERSION:
            raise RolloutError("signal_app_artifact_identity_invalid")
        _assert_expected_app_identity(
            config,
            app,
            error="signal_app_artifact_identity_invalid",
        )
        _verify_app_xattrs(app)
    else:
        if (
            not isinstance(source_identity, dict)
            or info.get("CFBundleShortVersionString") != ROLLBACK_VERSION
            or _tree_content_mode_hash(app)
            != source_identity.get("installed_app_content_mode_sha256")
        ):
            raise RolloutError("signal_app_artifact_identity_invalid")
        _verify_signature(app)
        _verify_app_xattrs(app)
    observed = _app_bundle_executable_snapshot(app)
    if recorded_snapshot is not None:
        recorded = _recorded_app_executable_snapshot(recorded_snapshot)
        if observed[1:] != recorded[1:]:
            raise RolloutError("signal_app_artifact_snapshot_drift")
    return observed


def _app_pid_start_identity(pid: int) -> str:
    completed = _run(["ps", "-o", "uid=,lstart=", "-p", str(pid)], timeout=10.0)
    line = " ".join(completed.stdout.split())
    match = re.fullmatch(r"([0-9]+) (.+)", line)
    if completed.returncode != 0 or match is None or int(match.group(1)) != os.getuid():
        raise RolloutError("installed_app_process_identity_invalid")
    return line


def _verify_app_pid_executable(
    config: RolloutConfig,
    pid: int,
    expected: tuple[Any, ...],
    *,
    require_current: bool = True,
    require_path: bool = True,
) -> None:
    if pid <= 0 or len(expected) != 6:
        raise RolloutError("installed_app_process_image_mismatch")
    if require_current and _app_executable_snapshot(config) != expected:
        raise RolloutError("installed_app_process_image_mismatch")
    start_before = _app_pid_start_identity(pid)
    completed = _run(
        ["lsof", "-a", "-p", str(pid), "-d", "txt", "-F", "fDni"],
        timeout=10.0,
    )
    if completed.returncode != 0:
        raise RolloutError("installed_app_process_image_unavailable")
    records: list[dict[str, Any]] = []
    record: dict[str, Any] | None = None
    for line in completed.stdout.splitlines():
        if line.startswith("f"):
            if record is not None:
                records.append(record)
            record = {"descriptor": line[1:]}
        elif record is not None and line.startswith("D"):
            try:
                record["device"] = int(line[1:], 0)
            except ValueError as exc:
                raise RolloutError("installed_app_process_image_invalid") from exc
        elif record is not None and line.startswith("i"):
            try:
                record["inode"] = int(line[1:])
            except ValueError as exc:
                raise RolloutError("installed_app_process_image_invalid") from exc
        elif record is not None and line.startswith("n"):
            record["path"] = line[1:]
    if record is not None:
        records.append(record)
    executable_path, device, inode, _size, _mtime, _sha256 = expected
    matches = [
        item
        for item in records
        if item.get("descriptor") == "txt"
        and (not require_path or item.get("path") == executable_path)
        and item.get("device") == device
        and item.get("inode") == inode
    ]
    start_after = _app_pid_start_identity(pid)
    if (
        len(matches) != 1
        or start_before != start_after
        or (require_current and _app_executable_snapshot(config) != expected)
    ):
        raise RolloutError("installed_app_process_image_mismatch")


def _verify_app_pid_for_signal(
    config: RolloutConfig,
    pid: int,
    allowed_stale_snapshots: tuple[tuple[Any, ...], ...],
) -> None:
    for item in allowed_stale_snapshots:
        snapshot = _recorded_app_executable_snapshot(item)
        try:
            _verify_app_pid_executable(
                config,
                pid,
                snapshot,
                require_current=False,
                require_path=False,
            )
            return
        except RolloutError:
            continue
    raise RolloutError("app_process_identity_ambiguous")


def _verified_app_pids(config: RolloutConfig) -> set[int]:
    pids = _app_pids(config)
    if not pids:
        return set()
    expected = _app_executable_snapshot(config)
    for pid in sorted(pids):
        _verify_app_pid_executable(config, pid, expected)
    return pids


def _app_running(config: RolloutConfig) -> bool:
    return bool(_verified_app_pids(config))


def _assert_app_stopped(config: RolloutConfig) -> None:
    if _app_running(config):
        raise RolloutError("installed_app_still_running")


def _stop_app(
    config: RolloutConfig,
    *,
    force: bool,
    allowed_stale_snapshots: tuple[tuple[Any, ...], ...] = (),
) -> None:
    try:
        _quit_app()
    except RolloutError:
        if not force:
            raise
    for _ in range(20):
        if not _app_pids(config):
            return
        time.sleep(0.25)
    if not force:
        raise RolloutError("installed_app_still_running")
    for requested_signal, attempts in ((signal.SIGTERM, 8), (signal.SIGKILL, 8)):
        for pid in sorted(_app_pids(config)):
            _verify_app_pid_for_signal(config, pid, allowed_stale_snapshots)
            try:
                os.kill(pid, requested_signal)
            except ProcessLookupError:
                continue
            except OSError as exc:
                raise RolloutError("installed_app_force_stop_failed") from exc
        for _ in range(attempts):
            if not _app_pids(config):
                return
            time.sleep(0.25)
    _assert_app_stopped(config)


def _restore_app_state(
    config: RolloutConfig,
    was_running: bool,
    *,
    allowed_stale_snapshots: tuple[tuple[Any, ...], ...] = (),
) -> None:
    # A process can be relaunched while the canonical bundle is being renamed.
    # Never accept an existing argv match as proof of the newly canonical App.
    if _app_pids(config):
        _stop_app(
            config,
            force=True,
            allowed_stale_snapshots=allowed_stale_snapshots,
        )
    _assert_app_stopped(config)
    if not was_running:
        return
    expected = _app_executable_snapshot(config)
    completed = _run(["open", str(config.installed_app)], timeout=15.0)
    if completed.returncode != 0:
        raise RolloutError("app_restore_launch_failed")
    last_error: RolloutError | None = None
    for _ in range(20):
        pids = _app_pids(config)
        if pids:
            try:
                for pid in sorted(pids):
                    _verify_app_pid_executable(config, pid, expected)
                return
            except RolloutError as exc:
                last_error = exc
        time.sleep(0.25)
    if last_error is not None:
        raise RolloutError("app_restore_process_image_mismatch") from last_error
    raise RolloutError("app_restore_launch_failed")


def _read_ui_flag() -> dict[str, Any]:
    completed = _run(["defaults", "read", APP_DEFAULTS_DOMAIN, PHASE2_UI_FLAG])
    if completed.returncode != 0:
        diagnostic = f"{completed.stdout}\n{completed.stderr}"
        missing_pair = (
            completed.returncode == 1
            and "The domain/default pair of" in diagnostic
            and "does not exist" in diagnostic
            and APP_DEFAULTS_DOMAIN in diagnostic
            and PHASE2_UI_FLAG in diagnostic
        )
        if missing_pair:
            return {"exists": False, "value": None}
        raise RolloutError("phase2_ui_preference_unreadable")
    normalized = completed.stdout.strip().lower()
    if normalized in {"1", "true", "yes"}:
        return {"exists": True, "value": True}
    if normalized in {"0", "false", "no"}:
        return {"exists": True, "value": False}
    raise RolloutError("phase2_ui_preference_invalid")


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    if path.exists() or path.is_symlink():
        raise RolloutError("private_snapshot_exists")
    temporary = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _durable_rename(temporary, path)
        if _mode(path) != 0o600:
            raise RolloutError("private_snapshot_permissions_invalid")
    except Exception:
        # Keep a partial private temporary for diagnosis.  The canonical
        # snapshot path is never exposed until a complete fsynced JSON payload
        # can be atomically renamed into place.
        raise


def _snapshot_and_disable_ui_flag(snapshot_path: Path) -> dict[str, Any]:
    previous = _read_ui_flag()
    _write_private_json(snapshot_path, previous)
    _set_ui_disabled()
    return previous


def _set_ui_disabled() -> None:
    completed = _run(
        ["defaults", "write", APP_DEFAULTS_DOMAIN, PHASE2_UI_FLAG, "-bool", "false"]
    )
    if completed.returncode != 0 or _read_ui_flag() != {"exists": True, "value": False}:
        raise RolloutError("phase2_ui_disable_failed")


def _restore_ui_value(snapshot: dict[str, Any]) -> None:
    if set(snapshot) != {"exists", "value"} or not isinstance(snapshot["exists"], bool):
        raise RolloutError("phase2_ui_snapshot_invalid")
    if snapshot["exists"]:
        if not isinstance(snapshot["value"], bool):
            raise RolloutError("phase2_ui_snapshot_invalid")
        completed = _run(
            [
                "defaults",
                "write",
                APP_DEFAULTS_DOMAIN,
                PHASE2_UI_FLAG,
                "-bool",
                "true" if snapshot["value"] else "false",
            ]
        )
    else:
        completed = _run(["defaults", "delete", APP_DEFAULTS_DOMAIN, PHASE2_UI_FLAG])
        # ``defaults delete`` returns non-zero if the key already disappeared,
        # which is an equivalent restored state.
        if completed.returncode != 0 and _read_ui_flag()["exists"]:
            raise RolloutError("phase2_ui_restore_failed")
        return
    if completed.returncode != 0 or _read_ui_flag() != snapshot:
        raise RolloutError("phase2_ui_restore_failed")


def _restore_ui_flag(snapshot_path: Path) -> None:
    if snapshot_path.is_symlink() or not snapshot_path.is_file() or _mode(snapshot_path) != 0o600:
        raise RolloutError("phase2_ui_snapshot_invalid")
    _restore_ui_value(_read_json(snapshot_path))


def _restore_ui_flag_with_journal_fallback(
    snapshot_path: Path,
    journal_value: Any,
) -> str:
    if not isinstance(journal_value, dict):
        raise RolloutError("phase2_ui_journal_value_missing")
    if snapshot_path.exists() or snapshot_path.is_symlink():
        try:
            if (
                snapshot_path.is_symlink()
                or not snapshot_path.is_file()
                or _mode(snapshot_path) != 0o600
            ):
                raise RolloutError("phase2_ui_snapshot_invalid")
            snapshot = _read_json(snapshot_path)
            if snapshot != journal_value:
                _restore_ui_value(journal_value)
                return "journal_fallback_snapshot_drift"
            _restore_ui_value(snapshot)
            return "snapshot"
        except RolloutError:
            # Preserve an invalid snapshot for diagnosis, but do not let it
            # override the already durable original value in the journal.
            _restore_ui_value(journal_value)
            return "journal_fallback"
    _restore_ui_value(journal_value)
    return "journal_fallback"


def _database_family(path: Path) -> tuple[Path, Path, Path]:
    return path, Path(f"{path}-wal"), Path(f"{path}-shm")


def _database_handle_pids(path: Path) -> set[int]:
    pids: set[int] = set()
    for candidate in _database_family(path):
        if not candidate.exists():
            continue
        completed = _run(["lsof", "-t", "--", str(candidate)], timeout=10.0)
        if completed.returncode == 0 and completed.stdout.strip():
            try:
                values = {int(value) for value in completed.stdout.split()}
            except ValueError as exc:
                raise RolloutError("database_handle_check_failed") from exc
            if not values or any(value <= 0 for value in values):
                raise RolloutError("database_handle_check_failed")
            pids.update(values)
        if completed.returncode not in {0, 1}:
            raise RolloutError("database_handle_check_failed")
    return pids


def _assert_database_handles_allowed(path: Path, allowed_pids: set[int]) -> None:
    unexpected = _database_handle_pids(path) - allowed_pids
    if unexpected:
        raise RolloutError("database_has_active_handles")


def _assert_database_not_open(path: Path) -> None:
    _assert_database_handles_allowed(path, set())


@dataclass
class _DatabaseWriterBarrier:
    path: Path
    connection: sqlite3.Connection
    device: int
    inode: int

    @property
    def identity(self) -> str:
        material = f"{self.device}:{self.inode}".encode("ascii")
        return "sha256:" + hashlib.sha256(material).hexdigest()

    def verify(self) -> None:
        try:
            current = self.path.stat()
        except OSError as exc:
            raise RolloutError("database_barrier_identity_missing") from exc
        if (current.st_dev, current.st_ino) != (self.device, self.inode):
            raise RolloutError("database_barrier_identity_changed")
        _assert_database_handles_allowed(self.path, {os.getpid()})


@contextmanager
def _live_database_writer_barrier(path: Path) -> Iterator[_DatabaseWriterBarrier]:
    if path.is_symlink() or not path.is_file() or _mode(path) != 0o600:
        raise RolloutError("database_barrier_source_invalid")
    try:
        before = path.stat()
        connection = sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=2.0)
        connection.execute("PRAGMA busy_timeout=2000")
        # BEGIN IMMEDIATE is the writer exclusion primitive.  No SQL mutation
        # is executed through this connection; it is only rolled back/closed.
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None or int(row[0]) != SOURCE_SCHEMA:
            raise RolloutError("database_barrier_schema_mismatch")
        barrier = _DatabaseWriterBarrier(
            path=path,
            connection=connection,
            device=int(before.st_dev),
            inode=int(before.st_ino),
        )
        barrier.verify()
    except RolloutError:
        if "connection" in locals():
            if connection.in_transaction:
                connection.rollback()
            connection.close()
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError) as exc:
        if "connection" in locals():
            if connection.in_transaction:
                connection.rollback()
            connection.close()
        raise RolloutError("database_writer_barrier_failed") from exc
    try:
        yield barrier
    finally:
        try:
            if connection.in_transaction:
                connection.rollback()
        finally:
            connection.close()


def _assert_tree_no_symlinks(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise RolloutError("runtime_tree_invalid")
    for directory, directories, files in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in [*directories, *files]:
            if (base / name).is_symlink():
                raise RolloutError("runtime_symlink_forbidden")


def _verify_sensitive_runtime_permissions(data_root: Path) -> None:
    if data_root.is_symlink() or not data_root.is_dir() or _mode(data_root) != 0o700:
        raise RolloutError("runtime_data_permissions_invalid")
    for relative in (
        "activity.sqlite",
        "document_watch_secret.key",
        "document_watch_roots.json",
        ".debug-tokens.json",
    ):
        path = data_root / relative
        if path.exists() and (path.is_symlink() or not path.is_file() or _mode(path) != 0o600):
            raise RolloutError("runtime_sensitive_file_permissions_invalid")
    for relative in ("raw_vault", "confirmation_evidence.noindex"):
        path = data_root / relative
        if path.exists() and (path.is_symlink() or not path.is_dir() or _mode(path) != 0o700):
            raise RolloutError("runtime_sensitive_directory_permissions_invalid")


def _runtime_tree_manifest(root: Path, database_name: str) -> tuple[dict[str, tuple[Any, ...]], str]:
    excluded = set(RUNTIME_DATA_EXCLUDES)
    excluded.update({database_name, f"{database_name}-wal", f"{database_name}-shm"})
    rows: dict[str, tuple[Any, ...]] = {}
    for directory, directories, files in os.walk(root, topdown=True, followlinks=False):
        base = Path(directory)
        directories[:] = sorted(name for name in directories if name != "__pycache__")
        relative_base = base.relative_to(root)
        relative_directory = "." if relative_base == Path(".") else relative_base.as_posix()
        rows[f"d:{relative_directory}"] = ("directory", _mode(base))
        for name in sorted(files):
            if name.endswith(".pyc") or (base == root and name in excluded):
                continue
            path = base / name
            if path.is_symlink() or not path.is_file():
                raise RolloutError("runtime_tree_invalid")
            relative = path.relative_to(root).as_posix()
            rows[f"f:{relative}"] = ("file", _mode(path), path.stat().st_size, _sha256_file(path))
    digest = hashlib.sha256()
    for key, value in sorted(rows.items()):
        digest.update(
            json.dumps([key, *value], separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        )
        digest.update(b"\n")
    return rows, digest.hexdigest()


def _copy_runtime_data(source: Path, destination: Path, database_name: str) -> dict[str, Any]:
    _assert_tree_no_symlinks(source)
    _verify_sensitive_runtime_permissions(source)
    source_manifest, source_digest = _runtime_tree_manifest(source, database_name)
    excluded = set(RUNTIME_DATA_EXCLUDES)
    excluded.update({database_name, f"{database_name}-wal", f"{database_name}-shm"})

    def ignore(directory: str, names: list[str]) -> set[str]:
        ignored = {name for name in names if name == "__pycache__" or name.endswith(".pyc")}
        if Path(directory).resolve() == source.resolve():
            ignored.update(excluded.intersection(names))
        return ignored

    shutil.copytree(source, destination, symlinks=False, ignore=ignore)
    destination.chmod(0o700)
    _fsync_tree(destination)
    _verify_sensitive_runtime_permissions(destination)
    destination_manifest, destination_digest = _runtime_tree_manifest(destination, database_name)
    if source_manifest != destination_manifest or source_digest != destination_digest:
        raise RolloutError("runtime_tree_manifest_mismatch")
    return {
        "entry_count": len(source_manifest),
        "content_mode_manifest_sha256": source_digest,
    }


def _copy_candidate_core(config: RolloutConfig, incoming_core: Path) -> dict[str, Any]:
    if incoming_core.exists() or incoming_core.is_symlink():
        raise RolloutError("incoming_core_exists")
    manifest = _git_core_manifest(config)
    git_identity = _git_core_manifest_identity(config, manifest)
    if git_identity.get("core_code_sha256") != config.expected_core_code_sha256:
        raise RolloutError("git_core_hash_mismatch")
    incoming_core.mkdir(mode=0o755)
    incoming_core.chmod(0o755)
    for item in manifest:
        destination = incoming_core / item.relative_path
        relative_parent = Path(item.relative_path).parent
        parent = incoming_core
        for component in relative_parent.parts:
            parent = parent / component
            try:
                parent.mkdir(mode=0o755)
            except FileExistsError:
                pass
            if parent.is_symlink() or not parent.is_dir():
                raise RolloutError("staged_git_core_special_entry")
            parent.chmod(0o755)
            if _mode(parent) != 0o755:
                raise RolloutError("staged_git_core_directory_permissions_invalid")
        payload = _git_blob(config, item.object_id)
        if len(payload) != item.size or hashlib.sha256(payload).hexdigest() != item.sha256:
            raise RolloutError("git_core_object_identity_changed")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(destination, flags, item.mode)
        try:
            os.fchmod(descriptor, item.mode)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            raise
    _fsync_tree(incoming_core)
    if _core_code_hash(incoming_core) != config.expected_core_code_sha256:
        raise RolloutError("staged_git_core_hash_mismatch")
    materialized = _materialized_core_identity(incoming_core)
    if (
        materialized.get("entry_count") != git_identity.get("entry_count")
        or materialized.get("logical_bytes") != git_identity.get("logical_bytes")
        or materialized.get("content_mode_manifest_sha256")
        != git_identity.get("content_mode_manifest_sha256")
    ):
        raise RolloutError("staged_git_core_manifest_mismatch")
    return {"git": git_identity, "materialized": materialized}


def _assert_recorded_git_core_identity(
    config: RolloutConfig,
    recorded: Any,
) -> dict[str, Any]:
    manifest = _git_core_manifest(config)
    expected_git = _git_core_manifest_identity(config, manifest)
    expected_materialized = {
        key: expected_git[key]
        for key in (
            "entry_count",
            "logical_bytes",
            "content_mode_manifest_sha256",
        )
    }
    expected = {
        "git": expected_git,
        "materialized": expected_materialized,
    }
    if not isinstance(recorded, dict) or recorded != expected:
        raise RolloutError("recorded_git_core_identity_drift")
    return expected_materialized


def _unique_path(parent: Path, stem: str, run_id: str) -> Path:
    path = parent / f"{stem}.{run_id}"
    if path.exists() or path.is_symlink():
        raise RolloutError("rollout_target_exists")
    return path


@dataclass(frozen=True)
class RolloutArtifacts:
    app_rollbacks: Path
    core_rollbacks: Path
    incoming_app: Path
    incoming_core: Path
    incoming_plist: Path
    incoming_shadow_plist: Path
    old_app: Path
    old_core: Path
    old_plist: Path
    old_shadow_plist: Path
    rollback_database: Path
    failed_app: Path
    failed_core: Path
    failed_plist: Path
    failed_shadow_plist: Path
    attempted_old_plist: Path
    attempted_old_shadow_plist: Path
    ui_snapshot: Path


def _rollout_artifacts(config: RolloutConfig, run_id: str) -> RolloutArtifacts:
    if not re.fullmatch(r"[0-9a-f]{32}", run_id):
        raise RolloutError("rollout_run_id_invalid")
    app_rollbacks = config.installed_app.parent / ".pam-phase3-rollbacks" / run_id
    core_rollbacks = config.live_core.parent / ".pam-phase3-rollbacks" / run_id
    return RolloutArtifacts(
        app_rollbacks=app_rollbacks,
        core_rollbacks=core_rollbacks,
        incoming_app=config.installed_app.parent / f".Personal AI Monitor.app.v0.4.0.incoming.{run_id}",
        incoming_core=config.live_core.parent / f".core.v0.4.0.incoming.{run_id}",
        incoming_plist=config.launch_agent_plist.parent
        / f".{config.launch_agent_plist.name}.v0.4.0.incoming.{run_id}",
        incoming_shadow_plist=config.shadow_launch_agent_plist.parent
        / f".{config.shadow_launch_agent_plist.name}.v0.4.0.incoming.{run_id}",
        old_app=app_rollbacks / "Personal AI Monitor.app.v0.3.22",
        old_core=core_rollbacks / "core.v0.3.22",
        old_plist=core_rollbacks / config.launch_agent_plist.name,
        old_shadow_plist=core_rollbacks / config.shadow_launch_agent_plist.name,
        rollback_database=core_rollbacks / "activity.sqlite.schema28.backup",
        failed_app=app_rollbacks / "Personal AI Monitor.app.v0.4.0.failed",
        failed_core=core_rollbacks / "core.v0.4.0.failed",
        failed_plist=config.launch_agent_plist.parent
        / f".{config.launch_agent_plist.name}.v0.4.0.failed.{run_id}",
        failed_shadow_plist=config.shadow_launch_agent_plist.parent
        / f".{config.shadow_launch_agent_plist.name}.v0.4.0.failed.{run_id}",
        attempted_old_plist=config.launch_agent_plist.parent
        / f".{config.launch_agent_plist.name}.v0.3.22.rollback-attempt.{run_id}",
        attempted_old_shadow_plist=config.shadow_launch_agent_plist.parent
        / f".{config.shadow_launch_agent_plist.name}.v0.3.22.rollback-attempt.{run_id}",
        ui_snapshot=core_rollbacks / "phase2-ui-default.json",
    )


def _verified_app_signal_snapshots(
    config: RolloutConfig,
    state: dict[str, Any],
    artifacts: RolloutArtifacts,
) -> tuple[tuple[Any, ...], ...]:
    source_identity = state.get("source_identity")
    if not isinstance(source_identity, dict):
        raise RolloutError("rollback_source_identity_missing")
    recorded_old = source_identity.get("installed_app_executable_snapshot")
    rows: list[tuple[Any, ...]] = []
    for app in (
        config.installed_app,
        artifacts.old_app,
        artifacts.incoming_app,
        artifacts.failed_app,
    ):
        if not app.exists() and not app.is_symlink():
            continue
        if app.is_symlink() or not app.is_dir():
            raise RolloutError("signal_app_artifact_identity_invalid")
        version = _app_info(app).get("CFBundleShortVersionString")
        if version == RELEASE_VERSION:
            snapshot = _verified_app_artifact_snapshot_for_signal(
                config,
                app,
                release=True,
            )
        elif version == ROLLBACK_VERSION:
            snapshot = _verified_app_artifact_snapshot_for_signal(
                config,
                app,
                release=False,
                source_identity=source_identity,
                recorded_snapshot=recorded_old,
            )
        else:
            raise RolloutError("signal_app_artifact_identity_invalid")
        if not any(existing[1:3] == snapshot[1:3] for existing in rows):
            rows.append(snapshot)
    if not rows:
        raise RolloutError("signal_app_artifact_identity_missing")
    return tuple(rows)


def _artifact_state(artifacts: RolloutArtifacts) -> dict[str, str]:
    return {
        field: str(getattr(artifacts, field))
        for field in (
            "incoming_app",
            "incoming_core",
            "incoming_plist",
            "incoming_shadow_plist",
            "old_app",
            "old_core",
            "old_plist",
            "old_shadow_plist",
            "rollback_database",
            "failed_app",
            "failed_core",
            "failed_plist",
            "failed_shadow_plist",
            "attempted_old_plist",
            "attempted_old_shadow_plist",
            "ui_snapshot",
        )
    }


def _assert_state_artifacts(state: dict[str, Any], artifacts: RolloutArtifacts) -> None:
    if any(state.get(key) != value for key, value in _artifact_state(artifacts).items()):
        raise RolloutError("rollout_artifact_state_mismatch")


def _assert_paths_absent(paths: tuple[Path, ...], error: str) -> None:
    if any(path.exists() or path.is_symlink() for path in paths):
        raise RolloutError(error)


def _copy_private_file(source: Path, destination: Path) -> None:
    try:
        payload, source_metadata = _read_stable_nofollow_file(
            source,
            max_bytes=1024 * 1024,
            error="private_source_file_invalid",
        )
    except RolloutError as exc:
        raise RolloutError("private_source_file_invalid") from exc
    if stat.S_IMODE(source_metadata.st_mode) != 0o600:
        raise RolloutError("private_source_file_invalid")
    if destination.exists() or destination.is_symlink():
        raise RolloutError("private_snapshot_exists")
    temporary = destination.parent / (
        f".{destination.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as destination_handle:
            destination_handle.write(payload)
            destination_handle.flush()
            os.fsync(destination_handle.fileno())
        _durable_rename(temporary, destination)
        if _mode(destination) != 0o600:
            raise RolloutError("private_snapshot_permissions_invalid")
        copied, _copied_metadata = _read_stable_nofollow_file(
            destination,
            max_bytes=1024 * 1024,
            error="private_snapshot_identity_invalid",
        )
        if copied != payload:
            raise RolloutError("private_snapshot_identity_invalid")
    except Exception:
        # Preserve any partial private artifact for diagnosis; never delete it.
        raise


def _write_private_plist(path: Path, payload: dict[str, Any]) -> None:
    if path.exists() or path.is_symlink():
        raise RolloutError("private_snapshot_exists")
    temporary = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            plistlib.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        _durable_rename(temporary, path)
        if _mode(path) != 0o600:
            raise RolloutError("private_snapshot_permissions_invalid")
    except Exception:
        raise


def _render_staged_launch_agent(
    core_root: Path,
    interpreter: dict[str, Any],
    request: dict[str, Any],
) -> dict[str, Any]:
    program = interpreter.get("program")
    if not isinstance(program, str) or _trusted_python_interpreter(program) != interpreter:
        raise RolloutError("launch_agent_renderer_interpreter_drift")
    launcher = core_root / ISOLATED_LAUNCHER_NAME
    if launcher.is_symlink() or not launcher.is_file() or _mode(launcher) != 0o755:
        raise RolloutError("launch_agent_renderer_invalid")
    try:
        completed = subprocess.run(
            [program, "-I", "-S", str(launcher), "--render-launch-agent"],
            cwd=Path("/"),
            env={"LANG": "C", "LC_ALL": "C", "PATH": SYSTEM_EXECUTION_PATH},
            input=json.dumps(request, sort_keys=True, separators=(",", ":")),
            check=False,
            capture_output=True,
            text=True,
            timeout=30.0,
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        raise RolloutError("launch_agent_renderer_unavailable") from exc
    if completed.returncode != 0 or len(completed.stdout.encode("utf-8")) > 65536:
        raise RolloutError("launch_agent_renderer_failed")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RolloutError("launch_agent_renderer_invalid") from exc
    if not isinstance(payload, dict):
        raise RolloutError("launch_agent_renderer_invalid")
    if _trusted_python_interpreter(program) != interpreter:
        raise RolloutError("launch_agent_renderer_interpreter_drift")
    return payload


def _stage_isolated_launch_agents(
    config: RolloutConfig,
    artifacts: RolloutArtifacts,
    *,
    expected_core_identity: dict[str, Any],
    expected_git_core_identity: dict[str, Any],
    expected_agents_sha256: str,
    source_identity: dict[str, Any],
) -> dict[str, Any]:
    _assert_recorded_git_core_identity(
        config,
        {
            "git": source_identity.get("git_core_identity")
            if isinstance(source_identity, dict)
            else None,
            "materialized": expected_git_core_identity,
        },
    )
    def assert_staged_core() -> None:
        _assert_git_materialized_core_identity(
            artifacts.incoming_core,
            expected_git_core_identity,
            expected_agents_sha256,
        )
        _assert_materialized_core_identity(
            artifacts.incoming_core,
            expected_core_identity,
        )

    assert_staged_core()
    # Render exclusively from the already sealed, private rollback plists.
    # Canonical LaunchAgent files remain mutable until service stop and must
    # never be plain-opened after a separate identity check.
    if not isinstance(source_identity, dict):
        raise RolloutError("rollback_source_identity_missing")
    core_payload = _plist_payload(
        artifacts.old_plist,
        expected_label=config.service_label,
        expected_sha256=source_identity.get("launch_agent_plist_sha256"),
    )
    shadow_payload = _plist_payload(
        artifacts.old_shadow_plist,
        expected_label=config.shadow_launch_agent_label,
        expected_sha256=source_identity.get("shadow_launch_agent_plist_sha256"),
    )
    core_identity = _verify_core_launch_agent_payload(
        config,
        core_payload,
    )
    shadow_identity = _verify_shadow_launch_agent_payload(
        config,
        shadow_payload,
    )
    assert_staged_core()
    interpreter = core_identity.get("interpreter")
    if not isinstance(interpreter, dict) or interpreter != shadow_identity.get("interpreter"):
        raise RolloutError("launch_agent_interpreter_identity_mismatch")
    core_environment = dict(core_identity.get("environment") or {})
    shadow_environment = dict(shadow_identity.get("environment") or {})
    core_environment.pop("PYTHONPATH", None)
    shadow_environment.pop("PYTHONPATH", None)
    _validate_launch_environment(
        core_environment,
        core=True,
        legacy_pythonpath=None,
    )
    _validate_launch_environment(
        shadow_environment,
        core=False,
        legacy_pythonpath=None,
    )
    core_payload = _render_staged_launch_agent(
        artifacts.incoming_core,
        interpreter,
        {
            "kind": "core",
            "label": config.service_label,
            "program": core_identity["program"],
            "core_root": str(config.live_core),
            "database": str(config.live_database),
            "stdout_path": core_payload["StandardOutPath"],
            "stderr_path": core_payload["StandardErrorPath"],
            "environment": core_environment,
        },
    )
    assert_staged_core()
    shadow_payload = _render_staged_launch_agent(
        artifacts.incoming_core,
        interpreter,
        {
            "kind": "shadow",
            "label": config.shadow_launch_agent_label,
            "program": shadow_identity["program"],
            "core_root": shadow_identity["working_directory"],
            "database": shadow_identity["database"],
            "socket": shadow_identity["socket"],
            "stdout_path": shadow_payload["StandardOutPath"],
            "stderr_path": shadow_payload["StandardErrorPath"],
            "environment": shadow_environment,
        },
    )
    assert_staged_core()
    _write_private_plist(artifacts.incoming_plist, core_payload)
    _write_private_plist(artifacts.incoming_shadow_plist, shadow_payload)
    staged_core = _verify_core_launch_agent_path(
        config,
        artifacts.incoming_plist,
        mode="isolated",
        isolated_launcher_root=artifacts.incoming_core,
    )
    staged_shadow = _verify_shadow_launch_agent_path(
        config,
        artifacts.incoming_shadow_plist,
        mode="isolated",
    )
    if staged_shadow.get("shadow_pair") != shadow_identity.get("shadow_pair"):
        raise RolloutError("shadow_core_database_pair_changed")
    assert_staged_core()
    return {
        "core_plist_sha256": _sha256_file(artifacts.incoming_plist),
        "shadow_plist_sha256": _sha256_file(artifacts.incoming_shadow_plist),
        "python_interpreter": staged_core["interpreter"],
        "shadow_pair": staged_shadow["shadow_pair"],
    }


def _assert_activated_launch_agent_hashes(
    config: RolloutConfig,
    staged_identity: Any,
) -> None:
    if not isinstance(staged_identity, dict):
        raise RolloutError("staged_launch_agent_identity_missing")
    expected_core = staged_identity.get("core_plist_sha256")
    expected_shadow = staged_identity.get("shadow_plist_sha256")
    if (
        not isinstance(expected_core, str)
        or not isinstance(expected_shadow, str)
        or _sha256_file(config.launch_agent_plist) != expected_core
        or _sha256_file(config.shadow_launch_agent_plist) != expected_shadow
    ):
        raise RolloutError("activated_launch_agent_identity_drift")


def _ensure_legacy_launch_agent_seals(
    config: RolloutConfig,
    source_identity: Any,
    artifacts: RolloutArtifacts,
    *,
    require_canonical_legacy: bool = True,
) -> None:
    if not isinstance(source_identity, dict):
        raise RolloutError("rollback_source_identity_missing")
    rows = (
        (
            config.launch_agent_plist,
            artifacts.old_plist,
            source_identity.get("launch_agent_plist_sha256"),
        ),
        (
            config.shadow_launch_agent_plist,
            artifacts.old_shadow_plist,
            source_identity.get("shadow_launch_agent_plist_sha256"),
        ),
    )
    for canonical, snapshot, expected_sha256 in rows:
        if not isinstance(expected_sha256, str) or re.fullmatch(
            r"[0-9a-f]{64}", expected_sha256
        ) is None:
            raise RolloutError("sealed_launch_agent_plist_identity_missing")
        snapshot_missing = not snapshot.exists() and not snapshot.is_symlink()
        if require_canonical_legacy or snapshot_missing:
            if (
                canonical.is_symlink()
                or not canonical.is_file()
                or _mode(canonical) != 0o600
                or _sha256_file(canonical) != expected_sha256
            ):
                raise RolloutError("canonical_launch_agent_plist_identity_drift")
        if snapshot_missing:
            _copy_private_file(canonical, snapshot)
        if (
            snapshot.is_symlink()
            or not snapshot.is_file()
            or _mode(snapshot) != 0o600
            or _sha256_file(snapshot) != expected_sha256
        ):
            raise RolloutError("sealed_launch_agent_plist_identity_drift")
    sealed_core_agent = _verify_core_launch_agent_path(
        config,
        artifacts.old_plist,
        mode="legacy",
    )
    sealed_shadow_agent = _verify_shadow_launch_agent_path(
        config,
        artifacts.old_shadow_plist,
        mode="legacy",
    )
    if sealed_shadow_agent.get("shadow_pair") != source_identity.get(
        "shadow_core_database_pair"
    ):
        raise RolloutError("sealed_shadow_pair_identity_drift")
    _assert_matching_interpreter_identity(
        sealed_core_agent,
        sealed_shadow_agent,
        expected=_recorded_interpreter_identity(source_identity),
    )
    if require_canonical_legacy:
        canonical_core_agent = _verify_core_launch_agent(config, mode="legacy")
        canonical_shadow_agent = _verify_shadow_launch_agent(config, mode="legacy")
        if canonical_shadow_agent.get("shadow_pair") != source_identity.get(
            "shadow_core_database_pair"
        ):
            raise RolloutError("canonical_shadow_pair_identity_drift")
        _assert_matching_interpreter_identity(
            canonical_core_agent,
            canonical_shadow_agent,
            expected=_recorded_interpreter_identity(source_identity),
        )


def _record_transaction_step(
    config: RolloutConfig,
    journal: dict[str, Any],
    step: str,
) -> None:
    steps = journal.setdefault("transaction_steps", [])
    if not isinstance(steps, list):
        raise RolloutError("transaction_journal_invalid")
    steps.append(step)
    journal["active_step"] = step
    _write_state(config, journal)


def _restore_original_directory(
    *,
    current: Path,
    original: Path,
    incoming: Path,
    failed: Path,
) -> None:
    if original.exists():
        if current.exists():
            if failed.exists() or failed.is_symlink():
                raise RolloutError("install_compensation_quarantine_exists")
            _durable_rename(current, failed)
        _durable_rename(original, current)
    if incoming.exists():
        if failed.exists() or failed.is_symlink():
            raise RolloutError("install_compensation_quarantine_exists")
        _durable_rename(incoming, failed)


def _restore_original_plist(
    *,
    current: Path,
    original: Path,
    incoming: Path,
    failed: Path,
) -> None:
    if original.exists() or original.is_symlink():
        if current.exists() or current.is_symlink():
            if failed.exists() or failed.is_symlink():
                raise RolloutError("install_compensation_quarantine_exists")
            _durable_rename(current, failed)
        _durable_rename(original, current)
    elif incoming.exists() or incoming.is_symlink():
        if failed.exists() or failed.is_symlink():
            raise RolloutError("install_compensation_quarantine_exists")
        _durable_rename(incoming, failed)


def _restore_agent_state(label: str, plist: Path, was_loaded: bool) -> None:
    loaded = _launch_agent_loaded(label)
    if was_loaded and not loaded:
        _bootstrap_plist(label, plist)
    elif not was_loaded and loaded:
        _bootout_label(label)
    if _launch_agent_loaded(label) is not was_loaded:
        raise RolloutError("launch_agent_original_state_not_restored")


def _bootstrap_plist(label: str, plist: Path) -> None:
    completed = _run(
        ["launchctl", "bootstrap", _launch_target(), str(plist)],
        timeout=15.0,
    )
    if completed.returncode != 0:
        raise RolloutError("launch_agent_bootstrap_failed")
    enabled = _run(["launchctl", "enable", f"{_launch_target()}/{label}"])
    if enabled.returncode != 0:
        raise RolloutError("launch_agent_enable_failed")
    if not _launch_agent_loaded(label):
        raise RolloutError("launch_agent_not_running")


def _bootstrap(config: RolloutConfig) -> None:
    _bootstrap_plist(config.service_label, config.launch_agent_plist)


def _bootstrap_shadow(config: RolloutConfig) -> None:
    _bootstrap_plist(config.shadow_launch_agent_label, config.shadow_launch_agent_plist)


def _verify_launch_agent(
    config: RolloutConfig,
    *,
    expected_interpreter: dict[str, Any] | None = None,
    mode: str = "legacy",
) -> dict[str, Any]:
    identity = _verify_core_launch_agent_path(
        config,
        config.launch_agent_plist,
        mode=mode,
    )
    if expected_interpreter is not None and identity.get("interpreter") != expected_interpreter:
        raise RolloutError("launch_agent_interpreter_identity_drift")
    target = f"{_launch_target()}/{config.service_label}"
    for _ in range(20):
        completed = _run(["launchctl", "print", target], timeout=10.0)
        if completed.returncode == 0:
            pid = _verify_launchctl_runtime(completed.stdout, identity, target=target)
            if pid is None:
                time.sleep(0.25)
                continue
            return {"running": True, "pid": pid, "canonical_core": True}
        time.sleep(0.25)
    raise RolloutError("launch_agent_not_running")


def _verify_old_release_artifacts(
    config: RolloutConfig,
    state: dict[str, Any],
    artifacts: RolloutArtifacts,
    *,
    tolerate_database_recovery: bool = False,
) -> dict[str, Any]:
    source_identity = state.get("source_identity")
    if not isinstance(source_identity, dict):
        raise RolloutError("rollback_source_identity_missing")
    if _core_version(artifacts.old_core) != ROLLBACK_VERSION:
        raise RolloutError("rollback_core_identity_mismatch")
    old_core_hash = _core_code_hash(artifacts.old_core)
    if old_core_hash != source_identity.get("live_core_code_sha256"):
        raise RolloutError("rollback_core_hash_mismatch")
    if _runtime_core_full_identity(artifacts.old_core) != source_identity.get(
        "live_core_full_identity"
    ):
        raise RolloutError("rollback_core_full_identity_mismatch")
    _verify_runtime_agents(
        artifacts.old_core,
        str(source_identity.get("live_agents_sha256") or ""),
    )
    old_app = _app_info(artifacts.old_app)
    if (
        old_app.get("CFBundleIdentifier") != "app.personalai.monitor"
        or old_app.get("CFBundleShortVersionString") != ROLLBACK_VERSION
        or _tree_content_mode_hash(artifacts.old_app)
        != source_identity.get("installed_app_content_mode_sha256")
    ):
        raise RolloutError("rollback_app_identity_mismatch")
    _verify_signature(artifacts.old_app)
    _verify_app_xattrs(artifacts.old_app)
    if _sha256_file(artifacts.old_plist) != source_identity.get("launch_agent_plist_sha256"):
        raise RolloutError("rollback_plist_hash_mismatch")
    if (
        _sha256_file(artifacts.old_shadow_plist)
        != source_identity.get("shadow_launch_agent_plist_sha256")
    ):
        raise RolloutError("rollback_shadow_plist_hash_mismatch")
    core_agent = _verify_core_launch_agent_path(config, artifacts.old_plist)
    shadow_agent = _verify_shadow_launch_agent_path(config, artifacts.old_shadow_plist)
    if shadow_agent.get("shadow_pair") != source_identity.get("shadow_core_database_pair"):
        raise RolloutError("rollback_shadow_core_database_pair_mismatch")
    _assert_matching_interpreter_identity(
        core_agent,
        shadow_agent,
        expected=_recorded_interpreter_identity(source_identity),
    )
    database_relative = config.live_database.relative_to(config.live_core)
    old_database = artifacts.old_core / database_relative
    backup_probe = _sqlite_probe(artifacts.rollback_database, expected_schema=SOURCE_SCHEMA)
    final_source = state.get("final_schema28")
    if not isinstance(final_source, dict):
        raise RolloutError("rollback_database_identity_missing")
    _assert_legacy_data_preserved(final_source, backup_probe)
    if backup_probe.get("sha256") != state.get("rollback_database_sha256"):
        raise RolloutError("rollback_database_hash_mismatch")
    try:
        old_probe: dict[str, Any] | None = _sqlite_probe(
            old_database,
            expected_schema=SOURCE_SCHEMA,
        )
        _assert_legacy_data_preserved(final_source, old_probe)
    except RolloutError:
        if not tolerate_database_recovery:
            raise
        old_probe = None
    return {
        "app_version": ROLLBACK_VERSION,
        "app_content_mode_sha256": source_identity["installed_app_content_mode_sha256"],
        "core_version": ROLLBACK_VERSION,
        "core_code_sha256": old_core_hash,
        "core_full_identity": source_identity["live_core_full_identity"],
        "agents_sha256": source_identity["live_agents_sha256"],
        "database": old_probe,
        "rollback_database": backup_probe,
        "core_plist_sha256": source_identity["launch_agent_plist_sha256"],
        "shadow_plist_sha256": source_identity["shadow_launch_agent_plist_sha256"],
        "shadow_core_database_pair": source_identity["shadow_core_database_pair"],
    }


def _rollback_identity_requires_database_recovery(
    recorded: dict[str, Any],
    observed: dict[str, Any],
) -> bool:
    recorded_without_database = {key: value for key, value in recorded.items() if key != "database"}
    observed_without_database = {key: value for key, value in observed.items() if key != "database"}
    if observed_without_database != recorded_without_database:
        raise RolloutError("rollback_artifact_identity_drift")
    return observed.get("database") != recorded.get("database")


def _database_quarantine_target(
    parent: Path,
    source: Path,
    run_id: str,
) -> Path:
    preferred = parent / f"{source.name}.invalid.{run_id}"
    if not preferred.exists() and not preferred.is_symlink():
        return preferred
    return _unique_path(parent, f"{source.name}.rebuild-attempt", uuid.uuid4().hex)


def _restore_schema28_database_from_backup(
    config: RolloutConfig,
    state: dict[str, Any],
    artifacts: RolloutArtifacts,
    *,
    core_root: Path,
) -> dict[str, Any]:
    final_source = state.get("final_schema28")
    recorded_identity = state.get("rollback_identity")
    if not isinstance(final_source, dict) or not isinstance(recorded_identity, dict):
        raise RolloutError("rollback_database_identity_missing")
    backup_probe = _sqlite_probe(artifacts.rollback_database, expected_schema=SOURCE_SCHEMA)
    if backup_probe.get("sha256") != state.get("rollback_database_sha256"):
        raise RolloutError("rollback_database_hash_mismatch")
    _assert_legacy_data_preserved(final_source, backup_probe)

    if core_root.is_symlink() or not core_root.is_dir():
        raise RolloutError("rollback_core_identity_mismatch")
    database = core_root / config.live_database.relative_to(config.live_core)
    data_root = database.parent
    if data_root.is_symlink() or not data_root.is_dir() or _mode(data_root) != 0o700:
        raise RolloutError("rollback_database_parent_invalid")
    accepted_hashes = {
        str(backup_probe.get("sha256") or ""),
        str((recorded_identity.get("database") or {}).get("sha256") or ""),
    }
    try:
        existing = _sqlite_probe(database, expected_schema=SOURCE_SCHEMA)
        _assert_legacy_data_preserved(final_source, existing)
        if existing.get("sha256") in accepted_hashes:
            return {"status": "original_or_recovered", "database": existing}
    except RolloutError:
        pass

    _assert_database_not_open(database)
    quarantine = artifacts.core_rollbacks / "database-quarantine"
    _ensure_private_directory(quarantine)
    for member in _database_family(database):
        if not member.exists() and not member.is_symlink():
            continue
        target = _database_quarantine_target(quarantine, member, str(state.get("run_id") or ""))
        if not _same_volume(member.parent, target.parent):
            raise RolloutError("rollback_database_quarantine_cross_volume")
        _durable_rename(member, target)
    recovered = _sqlite_backup(artifacts.rollback_database, database)
    _assert_legacy_data_preserved(final_source, recovered)
    if recovered.get("sha256") != backup_probe.get("sha256"):
        raise RolloutError("rollback_database_rebuild_hash_mismatch")
    _fsync_directory(data_root)
    return {
        "status": "recovered_from_independent_backup",
        "database": recovered,
        "quarantine": str(quarantine),
    }


def _verify_restored_rollback_runtime(config: RolloutConfig, state: dict[str, Any]) -> None:
    source_identity = state.get("source_identity")
    if not isinstance(source_identity, dict):
        raise RolloutError("rollback_source_identity_missing")
    info = _app_info(config.installed_app)
    if (
        info.get("CFBundleShortVersionString") != ROLLBACK_VERSION
        or _tree_content_mode_hash(config.installed_app)
        != source_identity.get("installed_app_content_mode_sha256")
    ):
        raise RolloutError("restored_app_identity_mismatch")
    _verify_signature(config.installed_app)
    _verify_app_xattrs(config.installed_app)
    if (
        _core_version(config.live_core) != ROLLBACK_VERSION
        or _core_code_hash(config.live_core) != source_identity.get("live_core_code_sha256")
        or _runtime_core_full_identity(config.live_core)
        != source_identity.get("live_core_full_identity")
    ):
        raise RolloutError("restored_core_identity_mismatch")
    _verify_runtime_agents(
        config.live_core,
        str(source_identity.get("live_agents_sha256") or ""),
    )
    restored_database = _sqlite_probe(config.live_database, expected_schema=SOURCE_SCHEMA)
    final_source = state.get("final_schema28")
    if isinstance(final_source, dict):
        _assert_legacy_data_preserved(final_source, restored_database)
    if (
        _sha256_file(config.launch_agent_plist)
        != source_identity.get("launch_agent_plist_sha256")
        or _sha256_file(config.shadow_launch_agent_plist)
        != source_identity.get("shadow_launch_agent_plist_sha256")
    ):
        raise RolloutError("restored_launch_agent_plist_identity_mismatch")
    core_agent = _verify_core_launch_agent(config, mode="legacy")
    shadow_agent = _verify_shadow_launch_agent(config, mode="legacy")
    if shadow_agent.get("shadow_pair") != source_identity.get("shadow_core_database_pair"):
        raise RolloutError("restored_shadow_core_database_pair_mismatch")
    _assert_matching_interpreter_identity(
        core_agent,
        shadow_agent,
        expected=_recorded_interpreter_identity(source_identity),
    )


def _compensate_failed_install(
    config: RolloutConfig,
    journal: dict[str, Any],
    artifacts: RolloutArtifacts,
) -> None:
    # A staging failure can occur before either seal exists.  Establish and
    # verify both rollback plists from the still-live legacy canonical files
    # before stopping any healthy service.
    _ensure_legacy_launch_agent_seals(
        config,
        journal.get("source_identity"),
        artifacts,
        require_canonical_legacy=False,
    )
    source_identity = journal.get("source_identity")
    if not isinstance(source_identity, dict):
        raise RolloutError("rollback_source_identity_missing")
    allowed_app_snapshots = _verified_app_signal_snapshots(
        config, journal, artifacts
    )
    _stop_app(
        config,
        force=True,
        allowed_stale_snapshots=allowed_app_snapshots,
    )
    if _launch_agent_loaded(config.service_label):
        _bootout_label(config.service_label)
    if _launch_agent_loaded(config.shadow_launch_agent_label):
        _bootout_label(config.shadow_launch_agent_label)
    if config.live_database.exists():
        _assert_database_not_open(config.live_database)
    _restore_original_directory(
        current=config.live_core,
        original=artifacts.old_core,
        incoming=artifacts.incoming_core,
        failed=artifacts.failed_core,
    )
    _stop_app(
        config,
        force=True,
        allowed_stale_snapshots=allowed_app_snapshots,
    )
    _assert_app_stopped(config)
    allowed_app_snapshots = _verified_app_signal_snapshots(
        config, journal, artifacts
    )
    _restore_original_directory(
        current=config.installed_app,
        original=artifacts.old_app,
        incoming=artifacts.incoming_app,
        failed=artifacts.failed_app,
    )
    _assert_app_stopped(config)
    _activate_sealed_old_plist(
        current=config.launch_agent_plist,
        snapshot=artifacts.old_plist,
        failed=artifacts.failed_plist,
        failure_stem=f".{config.launch_agent_plist.name}.compensation-attempt",
    )
    _activate_sealed_old_plist(
        current=config.shadow_launch_agent_plist,
        snapshot=artifacts.old_shadow_plist,
        failed=artifacts.failed_shadow_plist,
        failure_stem=f".{config.shadow_launch_agent_plist.name}.compensation-attempt",
    )
    journal["ui_restore_source"] = _restore_ui_flag_with_journal_fallback(
        artifacts.ui_snapshot,
        journal.get("ui_preference_original"),
    )
    _verify_restored_rollback_runtime(config, journal)
    _restore_agent_state(
        config.service_label,
        config.launch_agent_plist,
        bool(journal.get("core_was_loaded")),
    )
    _restore_agent_state(
        config.shadow_launch_agent_label,
        config.shadow_launch_agent_plist,
        bool(journal.get("shadow_was_loaded")),
    )
    expected_interpreter = _recorded_interpreter_identity(journal["source_identity"])
    if bool(journal.get("shadow_was_loaded")):
        _verify_shadow_runtime(config, expected_interpreter=expected_interpreter)
    else:
        _assert_launch_agent_stopped(config.shadow_launch_agent_label)
    if bool(journal.get("core_was_loaded")):
        _verify_launch_agent(config, expected_interpreter=expected_interpreter)
    else:
        _assert_launch_agent_stopped(config.service_label)
    _restore_app_state(
        config,
        bool(journal.get("app_was_running")),
        allowed_stale_snapshots=allowed_app_snapshots,
    )


def install(config: RolloutConfig, *, apply: bool, confirmation: str) -> dict[str, Any]:
    _validate_path_relationships(config)
    state = _load_state(config)
    if state.get("phase") != "shadow_verified":
        raise RolloutError("install_requires_verified_shadow")
    _verify_shadow(config)
    identity = _assert_secure_release_identity(config)
    if not apply:
        storage = _storage_headroom(config, "install")
        return {
            "status": "planned",
            "phase": "install",
            "mode": "dry_run",
            "first_core_stop_action": "direct_launchctl_bootout",
            "legacy_install_helper_used_for_stop": False,
            "pauses_shadow_launch_agent": True,
            "forces_new_ui_off_before_bootstrap": True,
            "preserves_displaced_artifacts": True,
            "system_integrity_protection": identity[
                "system_integrity_protection"
            ],
            "storage": storage,
            "raw_access": False,
        }
    if confirmation != INSTALL_CONFIRMATION:
        raise RolloutError("install_confirmation_required")

    with _rollout_lock(config):
        state = _load_state(config)
        if state.get("phase") != "shadow_verified":
            raise RolloutError("install_requires_verified_shadow")
        _verify_shadow(config)
        identity = _assert_secure_release_identity(config)
        storage = _storage_headroom(config, "install")
        run_id = str(state.get("run_id") or "")
        artifacts = _rollout_artifacts(config, run_id)
        _ensure_private_directory(artifacts.app_rollbacks.parent)
        _ensure_private_directory(artifacts.core_rollbacks.parent)
        _ensure_private_directory(artifacts.app_rollbacks)
        _ensure_private_directory(artifacts.core_rollbacks)
        _assert_paths_absent(
            tuple(Path(value) for value in _artifact_state(artifacts).values()),
            "install_artifact_already_exists",
        )
        core_was_loaded = _launch_agent_loaded(config.service_label)
        if not core_was_loaded:
            raise RolloutError("live_core_not_running_before_install")
        shadow_was_loaded = _launch_agent_loaded(config.shadow_launch_agent_label)
        app_was_running = _app_running(config)
        ui_preference_original = _read_ui_flag()
        journal: dict[str, Any] = {
            **state,
            **_artifact_state(artifacts),
            "phase": "install_prepared",
            "source_identity": identity,
            "core_was_loaded": core_was_loaded,
            "shadow_was_loaded": shadow_was_loaded,
            "app_was_running": app_was_running,
            "ui_preference_original": ui_preference_original,
            "storage_preflight": storage,
            "transaction_steps": [],
            "active_step": "install_journal_persisted",
        }
        _write_state(config, journal)
        try:
            _ensure_legacy_launch_agent_seals(config, identity, artifacts)
            _record_transaction_step(
                config,
                journal,
                "legacy_launch_agent_plists_sealed",
            )
            _copy_candidate_app(config.candidate_app, artifacts.incoming_app)
            journal["staged_core_git_identity"] = _copy_candidate_core(
                config,
                artifacts.incoming_core,
            )
            journal["live_agents_sha256"] = _copy_live_runtime_agents(
                config.live_core,
                artifacts.incoming_core,
                expected_sha256=str(identity["live_agents_sha256"]),
            )
            _fsync_tree(artifacts.incoming_core)
            journal["staged_core_materialized_identity"] = _materialized_core_identity(
                artifacts.incoming_core
            )
            _assert_materialized_core_identity(
                artifacts.incoming_core,
                journal["staged_core_materialized_identity"],
            )
            journal["staged_launch_agents"] = _stage_isolated_launch_agents(
                config,
                artifacts,
                expected_core_identity=journal["staged_core_materialized_identity"],
                expected_git_core_identity=journal["staged_core_git_identity"][
                    "materialized"
                ],
                expected_agents_sha256=journal["live_agents_sha256"],
                source_identity=identity,
            )
            _assert_materialized_core_identity(
                artifacts.incoming_core,
                journal["staged_core_materialized_identity"],
            )
            _fsync_tree(artifacts.incoming_app)
            _fsync_tree(artifacts.incoming_core)
            journal["staged_code_identity"] = _verify_candidate_code(
                config,
                app=artifacts.incoming_app,
                core=artifacts.incoming_core,
            )
            _record_transaction_step(config, journal, "static_candidate_and_plists_preserved")

            _ensure_legacy_launch_agent_seals(config, identity, artifacts)
            journal["storage_before_service_stop"] = _storage_headroom(
                config,
                "install_remaining",
            )
            _record_transaction_step(config, journal, "storage_rechecked_before_service_stop")

            _stop_app(config, force=False)
            _record_transaction_step(config, journal, "app_stopped")
            _bootout_label(config.service_label)
            _record_transaction_step(config, journal, "core_stopped_directly")
            _bootout_label(config.shadow_launch_agent_label)
            _record_transaction_step(config, journal, "shadow_stopped")
            _assert_database_not_open(config.live_database)
            _record_transaction_step(config, journal, "database_family_has_no_handles")
            with _live_database_writer_barrier(config.live_database) as barrier:
                journal["live_database_barrier_identity"] = barrier.identity
                _record_transaction_step(config, journal, "database_writer_barrier_acquired")
                barrier_database_bytes = _database_snapshot_storage_bytes(
                    config.live_database,
                    connection=barrier.connection,
                )
                journal["storage_after_writer_barrier"] = _storage_headroom(
                    config,
                    "install_barrier",
                    database_bytes=barrier_database_bytes,
                )
                _record_transaction_step(
                    config,
                    journal,
                    "storage_rechecked_after_writer_barrier",
                )
                observed_ui = _snapshot_and_disable_ui_flag(artifacts.ui_snapshot)
                if observed_ui != ui_preference_original:
                    raise RolloutError("phase2_ui_snapshot_raced")
                _record_transaction_step(config, journal, "phase2_ui_disabled")

                incoming_data = artifacts.incoming_core / "data"
                _assert_materialized_core_identity(
                    artifacts.incoming_core,
                    journal.get("staged_core_materialized_identity"),
                )
                journal["runtime_tree_manifest"] = _copy_runtime_data(
                    config.live_database.parent,
                    incoming_data,
                    config.live_database.name,
                )
                incoming_database = incoming_data / config.live_database.name
                final_source = _sqlite_backup(config.live_database, incoming_database)
                _migrate_with_candidate_core(
                    config,
                    incoming_database,
                    interpreter_identity=_recorded_interpreter_identity(identity),
                    core_root=artifacts.incoming_core,
                    expected_materialized_identity=journal[
                        "staged_core_materialized_identity"
                    ],
                    expected_git_materialized_identity=journal[
                        "staged_core_git_identity"
                    ]["materialized"],
                    expected_agents_sha256=journal["live_agents_sha256"],
                )
                _set_initial_rollout_gates_off(incoming_database)
                staged_database = _sqlite_probe(incoming_database, expected_schema=TARGET_SCHEMA)
                staged_migration_backup = _verify_migration_backup(incoming_database)
                _assert_legacy_data_preserved(final_source, staged_database)
                rollback_database = _sqlite_backup(
                    config.live_database,
                    artifacts.rollback_database,
                )
                if (
                    staged_migration_backup["sha256"] != final_source["sha256"]
                    or rollback_database["sha256"] != final_source["sha256"]
                ):
                    raise RolloutError("final_schema28_backup_mismatch")
                journal["final_schema28"] = final_source
                journal["rollback_database_sha256"] = rollback_database["sha256"]
                journal["staged_identity"] = _verify_candidate_runtime(
                    config,
                    app=artifacts.incoming_app,
                    core=artifacts.incoming_core,
                    database=incoming_database,
                )
                _verify_runtime_agents(
                    artifacts.incoming_core,
                    str(journal["live_agents_sha256"]),
                )
                _fsync_tree(artifacts.incoming_core)
                barrier.verify()
                _record_transaction_step(config, journal, "staging_schema31_verified")
                journal["storage_before_rename"] = _storage_headroom(
                    config,
                    "install_rename",
                    database_bytes=barrier_database_bytes,
                )
                _record_transaction_step(config, journal, "storage_rechecked_before_rename")
                _assert_materialized_core_identity(
                    artifacts.incoming_core,
                    journal.get("staged_core_materialized_identity"),
                )
                # All known launch paths are stopped and the schema-28 writer
                # barrier remains held across the durable directory rename.
                # The only SQLite operations after the rename are rollback and
                # close on that already-open connection.
                _durable_rename(config.live_core, artifacts.old_core)
            sealed_database = artifacts.old_core / config.live_database.relative_to(config.live_core)
            sealed_probe = _sqlite_probe(sealed_database, expected_schema=SOURCE_SCHEMA)
            _assert_legacy_data_preserved(final_source, sealed_probe)
            _record_transaction_step(config, journal, "old_core_preserved")
            _durable_rename(artifacts.incoming_core, config.live_core)
            _record_transaction_step(config, journal, "new_core_activated")
            _stop_app(
                config,
                force=True,
                allowed_stale_snapshots=_verified_app_signal_snapshots(
                    config, journal, artifacts
                ),
            )
            _assert_app_stopped(config)
            _record_transaction_step(config, journal, "app_zero_pid_before_bundle_switch")
            _durable_rename(config.installed_app, artifacts.old_app)
            _record_transaction_step(config, journal, "old_app_preserved")
            _durable_rename(artifacts.incoming_app, config.installed_app)
            _assert_app_stopped(config)
            _record_transaction_step(config, journal, "new_app_activated")
            _durable_rename(config.launch_agent_plist, artifacts.attempted_old_plist)
            _durable_rename(artifacts.incoming_plist, config.launch_agent_plist)
            _record_transaction_step(config, journal, "new_core_plist_activated")
            _durable_rename(
                config.shadow_launch_agent_plist,
                artifacts.attempted_old_shadow_plist,
            )
            _durable_rename(
                artifacts.incoming_shadow_plist,
                config.shadow_launch_agent_plist,
            )
            _record_transaction_step(config, journal, "new_shadow_plist_activated")
            _assert_activated_launch_agent_hashes(
                config,
                journal.get("staged_launch_agents"),
            )
            journal["rollback_identity"] = _verify_old_release_artifacts(
                config,
                journal,
                artifacts,
            )
            _assert_materialized_core_identity(
                config.live_core,
                journal.get("staged_core_materialized_identity"),
            )
            journal["canonical_identity_before_bootstrap"] = _verify_candidate_runtime(
                config,
                app=config.installed_app,
                core=config.live_core,
                database=config.live_database,
            )
            _verify_runtime_agents(
                config.live_core,
                str(journal["live_agents_sha256"]),
            )
            core_agent = _verify_core_launch_agent(config, mode="isolated")
            shadow_agent = _verify_shadow_launch_agent(config, mode="isolated")
            if shadow_agent.get("shadow_pair") != (
                journal.get("staged_launch_agents") or {}
            ).get("shadow_pair"):
                raise RolloutError("activated_shadow_pair_identity_drift")
            _assert_matching_interpreter_identity(
                core_agent,
                shadow_agent,
                expected=_recorded_interpreter_identity(journal["source_identity"]),
            )
            _assert_launch_agent_stopped(config.shadow_launch_agent_label)
            if _read_ui_flag() != {"exists": True, "value": False}:
                raise RolloutError("phase2_ui_not_disabled_before_bootstrap")
            _record_transaction_step(config, journal, "canonical_candidate_verified_before_bootstrap")
            _bootstrap(config)
            runtime = _verify_launch_agent(
                config,
                expected_interpreter=_recorded_interpreter_identity(
                    journal["source_identity"]
                ),
                mode="isolated",
            )
            _assert_materialized_core_identity(
                config.live_core,
                journal.get("staged_core_materialized_identity"),
            )
            _assert_launch_agent_stopped(config.shadow_launch_agent_label)
            _restore_app_state(
                config,
                app_was_running,
                allowed_stale_snapshots=_verified_app_signal_snapshots(
                    config, journal, artifacts
                ),
            )
            journal["phase"] = "installed"
            journal["runtime"] = runtime
            journal["active_step"] = "install_complete"
            _write_state(config, journal)
        except BaseException as original_error:
            failed_step = str(journal.get("active_step") or "install_unknown")
            try:
                _compensate_failed_install(config, journal, artifacts)
                journal["phase"] = "install_failed_rolled_back"
                journal["failed_step"] = failed_step
                _write_state(config, journal)
            except BaseException as compensation_error:
                journal["phase"] = "install_failed_recovery_required"
                journal["failed_step"] = failed_step
                try:
                    _write_state(config, journal)
                except BaseException:
                    pass
                if not isinstance(compensation_error, Exception):
                    raise
                raise RolloutError("install_failed_compensation_failed") from compensation_error
            if not isinstance(original_error, Exception):
                raise
            raise RolloutError(f"install_transaction_failed_after_{failed_step}") from original_error

        return {
            "status": "installed",
            "phase": "install",
            "version": RELEASE_VERSION,
            "schema": TARGET_SCHEMA,
            "runtime": runtime,
            "shadow_paused": True,
            "phase2_ui_enabled": False,
            "rollback_preserved": True,
            "raw_access": False,
        }


def _verify_installed(config: RolloutConfig) -> dict[str, Any]:
    state = _load_state(config)
    if state.get("phase") != "installed":
        raise RolloutError("installed_state_invalid")
    _assert_materialized_core_identity(
        config.live_core,
        state.get("staged_core_materialized_identity"),
    )
    expected_agents = state.get("live_agents_sha256")
    staged_git_identity = state.get("staged_core_git_identity")
    if (
        not isinstance(staged_git_identity, dict)
        or not isinstance(expected_agents, str)
    ):
        raise RolloutError("installed_git_core_identity_missing")
    expected_git_materialized = _assert_recorded_git_core_identity(
        config,
        staged_git_identity,
    )
    _assert_git_materialized_core_identity(
        config.live_core,
        expected_git_materialized,
        expected_agents,
    )
    identity = _verify_candidate_runtime(
        config,
        app=config.installed_app,
        core=config.live_core,
        database=config.live_database,
        require_context_disabled=False,
    )
    if not isinstance(expected_agents, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_agents):
        raise RolloutError("installed_agents_identity_missing")
    _verify_runtime_agents(config.live_core, expected_agents)
    database = identity["database"]
    core_agent = _verify_core_launch_agent(config, mode="isolated")
    shadow_agent = _verify_shadow_launch_agent(config, mode="isolated")
    source_identity = state.get("source_identity")
    if not isinstance(source_identity, dict):
        raise RolloutError("installed_source_identity_missing")
    _assert_activated_launch_agent_hashes(
        config,
        state.get("staged_launch_agents"),
    )
    if shadow_agent.get("shadow_pair") != (
        state.get("staged_launch_agents") or {}
    ).get("shadow_pair"):
        raise RolloutError("installed_shadow_pair_identity_drift")
    _assert_matching_interpreter_identity(
        core_agent,
        shadow_agent,
        expected=_recorded_interpreter_identity(source_identity),
    )
    _assert_launch_agent_stopped(config.shadow_launch_agent_label)
    ui_preference = _read_ui_flag()
    runtime = _verify_launch_agent(
        config,
        expected_interpreter=_recorded_interpreter_identity(source_identity),
        mode="isolated",
    )
    return {
        "status": "verified",
        "phase": "verify",
        "target": "installed",
        "version": RELEASE_VERSION,
        "identity": identity,
        "database": database,
        "runtime": runtime,
        "shadow_paused": True,
        "phase2_ui_enabled": ui_preference.get("value") is True,
        "raw_access": False,
    }


def verify(config: RolloutConfig, *, target: str) -> dict[str, Any]:
    if target == "shadow":
        return _verify_shadow(config)
    if target == "installed":
        return _verify_installed(config)
    raise RolloutError("verify_target_invalid")


def _verify_release_before_rollback(
    config: RolloutConfig,
    state: dict[str, Any],
    artifacts: RolloutArtifacts,
) -> dict[str, Any]:
    _assert_state_artifacts(state, artifacts)
    release_core_identity_status = "missing"
    if config.live_core.exists():
        try:
            _assert_materialized_core_identity(
                config.live_core,
                state.get("staged_core_materialized_identity"),
            )
            _canonical_runtime_database_identity(config.live_core, config.live_database)
            release_core_identity_status = "valid"
        except RolloutError:
            # A corrupt candidate is a reason to restore the sealed 0.3.22
            # tree, not a reason to follow or repair anything inside it.
            release_core_identity_status = "invalid_quarantine_only"
    _assert_paths_absent(
        (
            artifacts.incoming_app,
            artifacts.incoming_core,
            artifacts.incoming_plist,
            artifacts.incoming_shadow_plist,
            artifacts.failed_app,
            artifacts.failed_core,
            artifacts.failed_plist,
            artifacts.failed_shadow_plist,
        ),
        "rollback_collision_detected",
    )
    current_directory_states: dict[str, str] = {}
    for name, path in (
        ("app", config.installed_app),
        ("core", config.live_core),
    ):
        if path.is_symlink():
            raise RolloutError("current_release_path_invalid")
        if path.exists() and not path.is_dir():
            raise RolloutError("current_release_path_invalid")
        current_directory_states[name] = "directory" if path.is_dir() else "missing"
    if (
        not _same_volume(config.launch_agent_plist, artifacts.failed_plist)
        or not _same_volume(config.shadow_launch_agent_plist, artifacts.failed_shadow_plist)
    ):
        raise RolloutError("rollback_plist_rename_cross_volume")
    rollback_identity = _verify_old_release_artifacts(
        config,
        state,
        artifacts,
        tolerate_database_recovery=True,
    )
    recorded_rollback_identity = state.get("rollback_identity")
    if not isinstance(recorded_rollback_identity, dict):
        raise RolloutError("rollback_artifact_identity_drift")
    database_recovery_required = _rollback_identity_requires_database_recovery(
        recorded_rollback_identity,
        rollback_identity,
    )
    database_path_state = (
        "regular"
        if config.live_database.is_file() and not config.live_database.is_symlink()
        else "missing_or_invalid"
    )
    release_gate_status = (
        _read_release_gate_status_for_rollback(config.live_database)
        if release_core_identity_status == "valid"
        else {
            "context_core_gate": "unknown",
            "permanent_safety_gates": {
                key: "unknown" for key in PERMANENT_SAFETY_GATES
            },
        }
    )
    release_app_snapshot: list[Any] | None = None
    if config.installed_app.exists():
        try:
            release_app_snapshot = list(_app_executable_snapshot(config))
        except RolloutError:
            release_app_snapshot = None
    return {
        "current_quarantine_paths": {
            "app": current_directory_states["app"],
            "core": current_directory_states["core"],
            "database": database_path_state,
            "core_plist_present": config.launch_agent_plist.exists()
            or config.launch_agent_plist.is_symlink(),
            "shadow_plist_present": config.shadow_launch_agent_plist.exists()
            or config.shadow_launch_agent_plist.is_symlink(),
        },
        "release_core_identity_status": release_core_identity_status,
        "release_gate_status": release_gate_status,
        "release_ui_preference": _read_ui_flag(),
        "release_app_was_running": _app_running(config),
        "release_app_executable_snapshot": release_app_snapshot,
        "release_core_was_loaded": _launch_agent_loaded(config.service_label),
        "release_shadow_was_loaded": _launch_agent_loaded(config.shadow_launch_agent_label),
        "rollback": rollback_identity,
        "rollback_database_recovery_required": database_recovery_required,
    }


def _activate_sealed_old_directory(
    *,
    current: Path,
    old: Path,
    failed: Path,
    failure_stem: str,
) -> None:
    old_present = old.exists() or old.is_symlink()
    current_present = current.exists() or current.is_symlink()
    if old_present:
        if old.is_symlink() or not old.is_dir():
            raise RolloutError("sealed_old_directory_invalid")
        if current_present:
            target = failed
            if target.exists() or target.is_symlink():
                target = _unique_path(current.parent, failure_stem, uuid.uuid4().hex)
            _durable_rename(current, target)
        _durable_rename(old, current)
    elif not current_present:
        raise RolloutError("sealed_old_directory_missing")


def _activate_sealed_old_plist(
    *,
    current: Path,
    snapshot: Path,
    failed: Path,
    failure_stem: str,
) -> None:
    if snapshot.is_symlink() or not snapshot.is_file() or _mode(snapshot) != 0o600:
        raise RolloutError("sealed_old_plist_invalid")
    expected = _sha256_file(snapshot)
    current_present = current.exists() or current.is_symlink()
    if (
        current_present
        and not current.is_symlink()
        and current.is_file()
        and _mode(current) == 0o600
        and _sha256_file(current) == expected
    ):
        return
    if current_present:
        target = failed
        if target.exists() or target.is_symlink():
            target = _unique_path(current.parent, failure_stem, uuid.uuid4().hex)
        _durable_rename(current, target)
    _copy_private_file(snapshot, current)


def _complete_rollback_forward(
    config: RolloutConfig,
    journal: dict[str, Any],
    artifacts: RolloutArtifacts,
) -> dict[str, Any]:
    allowed_app_snapshots = _verified_app_signal_snapshots(config, journal, artifacts)
    _stop_app(
        config,
        force=True,
        allowed_stale_snapshots=allowed_app_snapshots,
    )
    if _launch_agent_loaded(config.service_label):
        _bootout_label(config.service_label)
    if _launch_agent_loaded(config.shadow_launch_agent_label):
        _bootout_label(config.shadow_launch_agent_label)
    if config.live_database.exists():
        _assert_database_not_open(config.live_database)
    _set_ui_disabled()
    _activate_sealed_old_directory(
        current=config.live_core,
        old=artifacts.old_core,
        failed=artifacts.failed_core,
        failure_stem=".core.v0.4.0.failed-extra",
    )
    journal["rollback_database_restore"] = _restore_schema28_database_from_backup(
        config,
        journal,
        artifacts,
        core_root=config.live_core,
    )
    _record_transaction_step(
        config,
        journal,
        "rollback_schema28_database_ready",
    )
    _stop_app(
        config,
        force=True,
        allowed_stale_snapshots=allowed_app_snapshots,
    )
    _assert_app_stopped(config)
    _activate_sealed_old_directory(
        current=config.installed_app,
        old=artifacts.old_app,
        failed=artifacts.failed_app,
        failure_stem=".Personal AI Monitor.app.v0.4.0.failed-extra",
    )
    _assert_app_stopped(config)
    allowed_app_snapshots = _verified_app_signal_snapshots(config, journal, artifacts)
    _activate_sealed_old_plist(
        current=config.launch_agent_plist,
        snapshot=artifacts.old_plist,
        failed=artifacts.failed_plist,
        failure_stem=f".{config.launch_agent_plist.name}.rollback-attempt",
    )
    _activate_sealed_old_plist(
        current=config.shadow_launch_agent_plist,
        snapshot=artifacts.old_shadow_plist,
        failed=artifacts.failed_shadow_plist,
        failure_stem=f".{config.shadow_launch_agent_plist.name}.rollback-attempt",
    )
    journal["ui_restore_source"] = _restore_ui_flag_with_journal_fallback(
        artifacts.ui_snapshot,
        journal.get("ui_preference_original"),
    )
    _verify_restored_rollback_runtime(config, journal)
    _restore_agent_state(
        config.service_label,
        config.launch_agent_plist,
        bool(journal.get("core_was_loaded")),
    )
    _restore_agent_state(
        config.shadow_launch_agent_label,
        config.shadow_launch_agent_plist,
        bool(journal.get("shadow_was_loaded")),
    )
    expected_interpreter = _recorded_interpreter_identity(journal["source_identity"])
    if bool(journal.get("shadow_was_loaded")):
        _verify_shadow_runtime(config, expected_interpreter=expected_interpreter)
    else:
        _assert_launch_agent_stopped(config.shadow_launch_agent_label)
    if bool(journal.get("core_was_loaded")):
        runtime = _verify_launch_agent(config, expected_interpreter=expected_interpreter)
    else:
        _assert_launch_agent_stopped(config.service_label)
        runtime = {"running": False, "pid": None, "canonical_core": True}
    _restore_app_state(
        config,
        bool(journal.get("release_app_was_running_before_rollback")),
        allowed_stale_snapshots=allowed_app_snapshots,
    )
    return runtime


def _restore_release_runtime_after_pre_mutation_abort(
    config: RolloutConfig,
    journal: dict[str, Any],
) -> None:
    """Restore and prove the still-canonical release after a safe abort."""

    artifacts = _rollout_artifacts(config, str(journal.get("run_id") or ""))
    allowed_app_snapshots = _verified_app_signal_snapshots(
        config, journal, artifacts
    )

    _restore_agent_state(
        config.service_label,
        config.launch_agent_plist,
        bool(journal.get("release_core_was_loaded_before_rollback")),
    )
    _restore_agent_state(
        config.shadow_launch_agent_label,
        config.shadow_launch_agent_plist,
        bool(journal.get("release_shadow_was_loaded_before_rollback")),
    )
    _restore_app_state(
        config,
        bool(journal.get("release_app_was_running_before_rollback")),
        allowed_stale_snapshots=allowed_app_snapshots,
    )
    _assert_materialized_core_identity(
        config.live_core,
        journal.get("staged_core_materialized_identity"),
    )
    _verify_candidate_runtime(
        config,
        app=config.installed_app,
        core=config.live_core,
        database=config.live_database,
        require_context_disabled=False,
    )
    expected_agents = journal.get("live_agents_sha256")
    if not isinstance(expected_agents, str) or re.fullmatch(r"[0-9a-f]{64}", expected_agents) is None:
        raise RolloutError("installed_agents_identity_missing")
    _verify_runtime_agents(config.live_core, expected_agents)
    _assert_activated_launch_agent_hashes(config, journal.get("staged_launch_agents"))
    source_identity = journal.get("source_identity")
    if not isinstance(source_identity, dict):
        raise RolloutError("installed_source_identity_missing")
    expected_interpreter = _recorded_interpreter_identity(source_identity)
    core_agent = _verify_core_launch_agent(config, mode="isolated")
    shadow_agent = _verify_shadow_launch_agent(config, mode="isolated")
    if shadow_agent.get("shadow_pair") != (
        journal.get("staged_launch_agents") or {}
    ).get("shadow_pair"):
        raise RolloutError("installed_shadow_pair_identity_drift")
    _assert_matching_interpreter_identity(
        core_agent,
        shadow_agent,
        expected=expected_interpreter,
    )
    if bool(journal.get("release_core_was_loaded_before_rollback")):
        _verify_launch_agent(
            config,
            expected_interpreter=expected_interpreter,
            mode="isolated",
        )
    else:
        _assert_launch_agent_stopped(config.service_label)
    if bool(journal.get("release_shadow_was_loaded_before_rollback")):
        _verify_shadow_runtime(config, expected_interpreter=expected_interpreter)
    else:
        _assert_launch_agent_stopped(config.shadow_launch_agent_label)
    if _app_running(config) is not bool(
        journal.get("release_app_was_running_before_rollback")
    ):
        raise RolloutError("installed_app_original_state_not_restored")
    expected_ui = journal.get("release_ui_preference_before_rollback")
    if not isinstance(expected_ui, dict) or _read_ui_flag() != expected_ui:
        raise RolloutError("release_ui_preference_not_restored")
    expected_gate = str(journal.get("release_context_gate_before_rollback") or "unknown")
    if expected_gate in {"true", "false"}:
        observed_gate = _read_release_gate_status_for_rollback(config.live_database).get(
            "context_core_gate"
        )
        if observed_gate != expected_gate:
            raise RolloutError("release_context_gate_not_restored")


def _fail_closed_rollback(
    config: RolloutConfig,
    journal: dict[str, Any],
) -> None:
    failures: list[str] = []
    try:
        artifacts = _rollout_artifacts(config, str(journal.get("run_id") or ""))
        allowed_app_snapshots = _verified_app_signal_snapshots(
            config, journal, artifacts
        )
    except RolloutError:
        allowed_app_snapshots = ()
    for code, action in (
        (
            "app_stop",
            lambda: _stop_app(
                config,
                force=True,
                allowed_stale_snapshots=allowed_app_snapshots,
            ),
        ),
        (
            "core_stop",
            lambda: _bootout_label(config.service_label)
            if _launch_agent_loaded(config.service_label)
            else None,
        ),
        (
            "shadow_stop",
            lambda: _bootout_label(config.shadow_launch_agent_label)
            if _launch_agent_loaded(config.shadow_launch_agent_label)
            else None,
        ),
        ("ui_disable", _set_ui_disabled),
    ):
        try:
            action()
        except Exception:
            failures.append(code)
    for code, proof in (
        ("app_zero_pid_proof", lambda: _assert_app_stopped(config)),
        ("core_stopped_proof", lambda: _assert_launch_agent_stopped(config.service_label)),
        (
            "shadow_stopped_proof",
            lambda: _assert_launch_agent_stopped(config.shadow_launch_agent_label),
        ),
    ):
        try:
            proof()
        except Exception:
            failures.append(code)
    journal["phase"] = "rollback_failed_recovery_required"
    journal["active_step"] = "rollback_fail_closed"
    journal["fail_closed_action_failures"] = failures
    journal["fail_closed_all_actions_verified"] = not failures
    _write_state(config, journal)


def _recover_incomplete_install(
    config: RolloutConfig,
    state: dict[str, Any],
    artifacts: RolloutArtifacts,
) -> dict[str, Any]:
    _assert_state_artifacts(state, artifacts)
    _compensate_failed_install(config, state, artifacts)
    state["phase"] = "install_failed_rolled_back"
    state["active_step"] = "incomplete_install_recovered"
    _write_state(config, state)
    return {
        "status": "recovered",
        "phase": "rollback",
        "recovered_from": "incomplete_install",
        "version": ROLLBACK_VERSION,
        "schema": SOURCE_SCHEMA,
        "raw_access": False,
    }


def rollback(config: RolloutConfig, *, apply: bool, confirmation: str) -> dict[str, Any]:
    # A damaged release may have replaced live Core/data with a symlink.  That
    # must not make the recovery entry point unusable: rollback validates the
    # configured path lexically and treats the current release as
    # quarantine-only.  Install and normal release verification stay strict.
    _validate_path_relationships(config, allow_invalid_live_database=True)
    state = _load_state(config)
    allowed_phases = {
        "installed",
        "install_prepared",
        "install_failed_recovery_required",
        "rollback_prepared",
        "rollback_aborted_pre_mutation_recovery_required",
        "rollback_failed_recovery_required",
    }
    if state.get("phase") not in allowed_phases:
        raise RolloutError("rollback_state_invalid")
    artifacts = _rollout_artifacts(config, str(state.get("run_id") or ""))
    if not apply:
        if state.get("phase") == "rollback_aborted_pre_mutation_recovery_required":
            return {
                "status": "planned",
                "phase": "rollback",
                "mode": "dry_run",
                "current_state": state.get("phase"),
                "next_action": "restore_release_process_state_then_repeat_full_preflight",
                "raw_access": False,
            }
        dry_identity = (
            _verify_release_before_rollback(config, state, artifacts)
            if state.get("phase") == "installed"
            else None
        )
        storage = _storage_headroom(
            config,
            "rollback",
            state=state,
            artifacts=artifacts,
            rollback_preflight_identity=dry_identity,
        )
        return {
            "status": "planned",
            "phase": "rollback",
            "mode": "dry_run",
            "current_state": state.get("phase"),
            "first_core_stop_action": "direct_launchctl_bootout",
            "restores_schema": SOURCE_SCHEMA,
            "restores_original_shadow_state": True,
            "restores_original_ui_preference": True,
            "quarantines_new_schema31_core": True,
            "storage": storage,
            "raw_access": False,
        }
    if confirmation != ROLLBACK_CONFIRMATION:
        raise RolloutError("rollback_confirmation_required")

    with _rollout_lock(config):
        state = _load_state(config)
        phase = str(state.get("phase") or "")
        if phase not in allowed_phases:
            raise RolloutError("rollback_state_invalid")
        artifacts = _rollout_artifacts(config, str(state.get("run_id") or ""))
        if phase == "rollback_aborted_pre_mutation_recovery_required":
            try:
                _restore_release_runtime_after_pre_mutation_abort(config, state)
            except BaseException as recovery_error:
                state["active_step"] = "rollback_pre_mutation_process_restore_failed"
                state["last_rollback_attempt"] = {
                    "status": "pre_mutation_abort_recovery_required",
                    "failed_at": _utc_now(),
                }
                _write_state(config, state)
                if not isinstance(recovery_error, Exception):
                    raise
                raise RolloutError("rollback_pre_mutation_process_restore_failed") from recovery_error
            state["phase"] = "installed"
            state["active_step"] = "rollback_pre_mutation_process_state_restored"
            state["last_rollback_attempt"] = {
                "status": "pre_mutation_abort_recovered_retry_required",
                "recovered_at": _utc_now(),
            }
            _write_state(config, state)
            raise RolloutError("rollback_pre_mutation_abort_recovered_retry_required")
        identities = (
            _verify_release_before_rollback(config, state, artifacts)
            if phase == "installed"
            else None
        )
        storage = _storage_headroom(
            config,
            "rollback",
            state=state,
            artifacts=artifacts,
            rollback_preflight_identity=identities,
        )
        if phase in {"install_prepared", "install_failed_recovery_required"}:
            return _recover_incomplete_install(config, state, artifacts)
        if phase in {"rollback_prepared", "rollback_failed_recovery_required"}:
            _assert_state_artifacts(state, artifacts)
            try:
                runtime = _complete_rollback_forward(config, state, artifacts)
            except BaseException as recovery_error:
                try:
                    _fail_closed_rollback(config, state)
                except BaseException as state_error:
                    if not isinstance(state_error, Exception):
                        raise
                    raise RolloutError("rollback_fail_closed_state_write_failed") from state_error
                if not isinstance(recovery_error, Exception):
                    raise
                raise RolloutError("rollback_failed_recovery_required") from recovery_error
            state["phase"] = "rolled_back"
            state["active_step"] = "incomplete_rollback_completed_forward"
            state["rollback_runtime"] = runtime
            _write_state(config, state)
            return {
                "status": "recovered",
                "phase": "rollback",
                "recovered_from": "incomplete_rollback",
                "version": ROLLBACK_VERSION,
                "schema": SOURCE_SCHEMA,
                "raw_access": False,
            }

        if not isinstance(identities, dict):
            raise RolloutError("rollback_preflight_identity_missing")
        journal = {
            **state,
            "phase": "rollback_prepared",
            "rollback_preflight_identity": identities,
            "release_ui_preference_before_rollback": identities["release_ui_preference"],
            "release_context_gate_before_rollback": identities["release_gate_status"][
                "context_core_gate"
            ],
            "release_core_was_loaded_before_rollback": identities[
                "release_core_was_loaded"
            ],
            "release_app_was_running_before_rollback": identities[
                "release_app_was_running"
            ],
            "release_app_executable_snapshot": identities.get(
                "release_app_executable_snapshot"
            ),
            "release_shadow_was_loaded_before_rollback": identities[
                "release_shadow_was_loaded"
            ],
            "release_core_plist_existed_before_rollback": identities[
                "current_quarantine_paths"
            ]["core_plist_present"],
            "release_shadow_plist_existed_before_rollback": identities[
                "current_quarantine_paths"
            ]["shadow_plist_present"],
            "rollback_storage_preflight": storage,
            "transaction_steps": [],
            "active_step": "rollback_journal_persisted",
        }
        _write_state(config, journal)
        try:
            _stop_app(config, force=False)
            _record_transaction_step(config, journal, "rollback_app_stopped")
            _bootout_label(config.service_label)
            _record_transaction_step(config, journal, "rollback_core_stopped_directly")
            _bootout_label(config.shadow_launch_agent_label)
            _record_transaction_step(config, journal, "rollback_shadow_stopped")
            if identities.get("release_core_identity_status") == "valid":
                _assert_database_not_open(config.live_database)
                _record_transaction_step(
                    config,
                    journal,
                    "rollback_database_family_has_no_handles",
                )
            else:
                _record_transaction_step(
                    config,
                    journal,
                    "rollback_invalid_candidate_database_not_followed",
                )
            latest_rollback = _verify_old_release_artifacts(
                config,
                journal,
                artifacts,
                tolerate_database_recovery=True,
            )
            recorded_rollback = journal.get("rollback_identity")
            if not isinstance(recorded_rollback, dict):
                raise RolloutError("rollback_artifact_identity_drift")
            latest_recovery_required = _rollback_identity_requires_database_recovery(
                recorded_rollback,
                latest_rollback,
            )
            latest_storage_identity = {
                **identities,
                "rollback_database_recovery_required": latest_recovery_required,
            }
            journal["rollback_preflight_identity"] = latest_storage_identity
            journal["active_step"] = (
                "rollback_storage_identity_refreshed_after_service_stop"
            )
            _write_state(config, journal)
            try:
                journal["rollback_storage_after_service_stop"] = _storage_headroom(
                    config,
                    "rollback",
                    state=journal,
                    artifacts=artifacts,
                    rollback_preflight_identity=latest_storage_identity,
                )
            except RolloutError as storage_error:
                if str(storage_error) != "insufficient_storage_headroom":
                    raise
                journal["last_rollback_attempt"] = {
                    "status": "pre_mutation_capacity_blocked",
                    "failed_at": _utc_now(),
                    "error": "insufficient_storage_headroom",
                }
                try:
                    _restore_release_runtime_after_pre_mutation_abort(config, journal)
                except BaseException as restore_error:
                    journal["phase"] = (
                        "rollback_aborted_pre_mutation_recovery_required"
                    )
                    journal["active_step"] = (
                        "rollback_pre_mutation_process_restore_failed"
                    )
                    _write_state(config, journal)
                    raise _RollbackPreMutationAbort(
                        RolloutError("rollback_pre_mutation_process_restore_failed")
                    ) from restore_error
                journal["phase"] = "installed"
                journal["active_step"] = "rollback_capacity_blocked_services_restored"
                journal["last_rollback_attempt"]["services_restored_at"] = _utc_now()
                _write_state(config, journal)
                raise _RollbackPreMutationAbort(storage_error)
            _record_transaction_step(
                config,
                journal,
                "rollback_storage_rechecked_after_service_stop",
            )
            _set_ui_disabled()
            release_gate_before = str(
                identities["release_gate_status"].get("context_core_gate") or "unknown"
            )
            if release_gate_before in {"true", "false"}:
                journal["release_context_gate_disable_status"] = (
                    _write_context_core_gate_for_rollback(
                        config.live_database,
                        "false",
                        required=False,
                    )
                )
            else:
                journal["release_context_gate_disable_status"] = "skipped_unknown"
            _record_transaction_step(config, journal, "release_features_disabled_for_quarantine")

            if config.live_core.exists():
                _durable_rename(config.live_core, artifacts.failed_core)
                _record_transaction_step(config, journal, "release_core_quarantined")
            else:
                _record_transaction_step(config, journal, "release_core_already_missing")
            _durable_rename(artifacts.old_core, config.live_core)
            _record_transaction_step(config, journal, "rollback_core_activated")
            journal["rollback_database_restore"] = (
                _restore_schema28_database_from_backup(
                    config,
                    journal,
                    artifacts,
                    core_root=config.live_core,
                )
            )
            _record_transaction_step(
                config,
                journal,
                "rollback_schema28_database_ready",
            )
            if config.installed_app.exists():
                rollback_app_snapshots = _verified_app_signal_snapshots(
                    config, journal, artifacts
                )
                _stop_app(
                    config,
                    force=True,
                    allowed_stale_snapshots=rollback_app_snapshots,
                )
                _assert_app_stopped(config)
                _record_transaction_step(
                    config,
                    journal,
                    "release_app_zero_pid_before_quarantine",
                )
                _durable_rename(config.installed_app, artifacts.failed_app)
                _record_transaction_step(config, journal, "release_app_quarantined")
            else:
                _record_transaction_step(config, journal, "release_app_already_missing")
            _durable_rename(artifacts.old_app, config.installed_app)
            _assert_app_stopped(config)
            rollback_app_snapshots = _verified_app_signal_snapshots(
                config, journal, artifacts
            )
            _record_transaction_step(config, journal, "rollback_app_activated")
            if config.launch_agent_plist.exists() or config.launch_agent_plist.is_symlink():
                _durable_rename(config.launch_agent_plist, artifacts.failed_plist)
                _record_transaction_step(config, journal, "release_plist_quarantined")
            _copy_private_file(artifacts.old_plist, config.launch_agent_plist)
            _record_transaction_step(config, journal, "rollback_plist_activated")
            if (
                config.shadow_launch_agent_plist.exists()
                or config.shadow_launch_agent_plist.is_symlink()
            ):
                _durable_rename(
                    config.shadow_launch_agent_plist,
                    artifacts.failed_shadow_plist,
                )
                _record_transaction_step(config, journal, "release_shadow_plist_quarantined")
            _copy_private_file(
                artifacts.old_shadow_plist,
                config.shadow_launch_agent_plist,
            )
            _record_transaction_step(config, journal, "rollback_shadow_plist_activated")
            journal["ui_restore_source"] = _restore_ui_flag_with_journal_fallback(
                artifacts.ui_snapshot,
                journal.get("ui_preference_original"),
            )
            _record_transaction_step(config, journal, "original_ui_preference_restored")

            _verify_restored_rollback_runtime(config, journal)
            _record_transaction_step(config, journal, "rollback_identity_verified_before_bootstrap")
            _restore_agent_state(
                config.service_label,
                config.launch_agent_plist,
                bool(journal.get("core_was_loaded")),
            )
            _restore_agent_state(
                config.shadow_launch_agent_label,
                config.shadow_launch_agent_plist,
                bool(journal.get("shadow_was_loaded")),
            )
            expected_interpreter = _recorded_interpreter_identity(
                journal["source_identity"]
            )
            if bool(journal.get("shadow_was_loaded")):
                _verify_shadow_runtime(config, expected_interpreter=expected_interpreter)
            else:
                _assert_launch_agent_stopped(config.shadow_launch_agent_label)
            if bool(journal.get("core_was_loaded")):
                runtime = _verify_launch_agent(
                    config,
                    expected_interpreter=expected_interpreter,
                )
            else:
                _assert_launch_agent_stopped(config.service_label)
                runtime = {"running": False, "pid": None, "canonical_core": True}
            _restore_app_state(
                config,
                bool(journal.get("release_app_was_running_before_rollback")),
                allowed_stale_snapshots=rollback_app_snapshots,
            )
        except BaseException as original_error:
            if isinstance(original_error, _RollbackPreMutationAbort):
                raise original_error.error
            failed_step = str(journal.get("active_step") or "rollback_unknown")
            try:
                runtime = _complete_rollback_forward(config, journal, artifacts)
            except BaseException as forward_error:
                journal["last_rollback_failed_step"] = failed_step
                try:
                    _fail_closed_rollback(config, journal)
                except BaseException as state_error:
                    if not isinstance(state_error, Exception):
                        raise
                    raise RolloutError("rollback_fail_closed_state_write_failed") from state_error
                if not isinstance(forward_error, Exception):
                    raise
                raise RolloutError("rollback_failed_recovery_required") from forward_error
            journal["phase"] = "rolled_back"
            journal["last_rollback_failed_step"] = failed_step
            journal["active_step"] = "rollback_failure_completed_forward"
            journal["rollback_runtime"] = runtime
            _write_state(config, journal)
            if not isinstance(original_error, Exception):
                raise
            return {
                "status": "rolled_back_after_failure",
                "phase": "rollback",
                "failed_step": failed_step,
                "version": ROLLBACK_VERSION,
                "schema": SOURCE_SCHEMA,
                "new_candidate_quarantined": True,
                "raw_access": False,
            }

        journal["phase"] = "rolled_back"
        journal["active_step"] = "rollback_complete"
        journal["rollback_runtime"] = runtime
        _write_state(config, journal)
        return {
            "status": "rolled_back",
            "phase": "rollback",
            "version": ROLLBACK_VERSION,
            "schema": SOURCE_SCHEMA,
            "new_candidate_quarantined": True,
            "shadow_original_state_restored": True,
            "ui_preference_restored": True,
            "raw_access": False,
        }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dry-run-first PAM 0.4.0 Phase 3 rollout")
    parser.add_argument("--config", required=True, type=Path, help="Private 0600 rollout config JSON")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("preflight", help="Read-only source/build/live v0.3.22 preflight")
    for command in ("shadow", "install", "rollback"):
        item = subparsers.add_parser(command)
        item.add_argument("--apply", action="store_true")
        item.add_argument("--confirm", default="")
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--target", choices=("shadow", "installed"), required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    if (
        sys.flags.isolated != 1
        or sys.flags.no_site != 1
        or sys.flags.safe_path is not True
    ):
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "error": "rollout_isolated_runtime_required",
                    "raw_access": False,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    os.umask(0o077)
    args = _parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "preflight":
            payload = preflight(config)
        elif args.command == "shadow":
            payload = shadow(config, apply=args.apply, confirmation=args.confirm)
        elif args.command == "verify":
            payload = verify(config, target=args.target)
        elif args.command == "install":
            payload = install(config, apply=args.apply, confirmation=args.confirm)
        elif args.command == "rollback":
            payload = rollback(config, apply=args.apply, confirmation=args.confirm)
        else:
            raise RolloutError("unsupported_command")
    except RolloutError as exc:
        print(
            json.dumps(
                {"status": "blocked", "error": str(exc), "raw_access": False},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except Exception:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "error": "unexpected_rollout_failure",
                    "raw_access": False,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
