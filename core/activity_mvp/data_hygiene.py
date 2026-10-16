from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import uuid
from datetime import datetime, time, timedelta, timezone
from pathlib import Path


MAX_MARKERS_PER_LOCATION = 200
MARKER_DIR_NAMES = {"ha_packages", "raw_vault"}
SQLITE_FILE_NAMES = {"activity.sqlite", "activity.sqlite3", "activity.db"}
SQLITE_SUFFIXES = {".sqlite", ".sqlite3"}
HYGIENE_CLEANUP_CONFIRMATION = "AUTO_CLEAN_PERSONAL_AI_MONITOR_HYGIENE"
HA_CACHE_RESIDUE_CONFIRMATION = "DELETE_HA_CACHE_RESIDUE"
DEFAULT_KEEP_BACKUPS = 2
DEFAULT_MIN_AGE_DAYS = 14
DEFAULT_QUARANTINE_DAYS = 7
DEFAULT_ACTIVITY_DATA_RETENTION_DAYS = 2
ACTIVITY_EXPORT_CACHE_KEYWORDS = ("cleaned", "learning")
ACTIVITY_EXPORT_CACHE_SUFFIXES = {".md", ".json"}
ACTIVITY_EXPORT_CACHE_AUTO_CLEANUP_ENABLED = False
RUNTIME_DIRECTORY_MODE = 0o700
RUNTIME_FILE_MODE = 0o600
RUNTIME_EXECUTABLE_MODE = 0o700
RUNTIME_PERMISSION_SCAN_DIR_NAMES = {
    "backups",
    "bin",
    "exports",
    "ha_packages",
    "logs",
    "raw_vault",
    "runtime_cache",
    "structured_signals",
    "vlm_eval",
}
RUNTIME_PERMISSION_ROOT_FILE_NAMES = {
    ".metadata_never_index",
    "activity.db",
    "activity.db-shm",
    "activity.db-wal",
    "activity.sqlite",
    "activity.sqlite-shm",
    "activity.sqlite-wal",
    "activity.sqlite3",
    "activity.sqlite3-shm",
    "activity.sqlite3-wal",
    "daemon.lock",
    "server.pid",
}
RUNTIME_PERMISSION_ROOT_FILE_SUFFIXES = SQLITE_SUFFIXES | {
    ".db",
    ".json",
    ".jsonl",
    ".lock",
    ".log",
    ".md",
    ".pid",
    ".sqlite-shm",
    ".sqlite-wal",
    ".sqlite3-shm",
    ".sqlite3-wal",
    ".tmp",
    ".txt",
}
HA_CACHE_CLEANUP_STATUSES = {"revoked", "expired_deleted", "user_deleted", "missing_db_record"}
HA_CACHE_CLEANUP_REASONS = {
    "missing_db_record",
    "cache_invalidated",
    "cache_ref_mismatch",
    "db_status_revoked",
    "db_status_expired_deleted",
    "db_status_user_deleted",
}


def data_hygiene_report(core_root: str | Path, db_path: str | Path) -> dict:
    core = Path(core_root).expanduser().resolve()
    db = Path(db_path).expanduser()
    if not db.is_absolute():
        db = core / db
    home = Path.home()
    live_core = home / "Library/Application Support/Personal AI Monitor/core"
    live_data = live_core / "data"
    release_backups = _release_backups_inventory(live_core.parent / "release-backups")
    # Closed recovery packages are an explicit metadata-only inventory. Never
    # add the feedback root to generic recursive rotation/deletion allowlists.
    feedback_recoveries = _release_backups_inventory(live_data / "feedback/recoveries")
    candidates = [
        ("current_db_data", db.parent),
        ("application_support_live_data", live_data),
        ("application_support_core", live_core),
        ("personal_ai_monitor_symlink", home / ".personal-ai-monitor"),
        ("legacy_downloads_repo", home / "Downloads/监控系统"),
        ("current_downloads_repo", home / "Downloads/AI 个人监控系统"),
        ("personal_ai_monitor_backups", home / "PersonalAI-Monitor-backups"),
    ]
    candidates.extend((f"desktop_monitor_candidate_{index}", path) for index, path in enumerate(sorted(home.glob("Desktop/*监控系统*")), start=1))
    locations = [_describe_location(label, path, live_data=live_data) for label, path in candidates]
    current_time = _coerce_datetime(None)
    strict_cleanup_health = _strict_cleanup_health(db)
    ha_cache_residue_candidates = _ha_cache_residue_candidates(core, db, live_data=live_data)
    activity_export_cache_candidates = _activity_export_cache_actions(
        live_data / "exports",
        strict_cleanup_health=strict_cleanup_health,
        now=current_time,
    )
    runtime_permission_candidates = _runtime_permission_actions(live_data)
    backup_candidates = _backup_candidates(locations, extra_candidates=ha_cache_residue_candidates)
    automation_plan = _hygiene_automation_preview(core, db, ha_cache_residue_candidates)
    recommended_actions = _recommended_actions(backup_candidates)
    recommended_actions.extend(_activity_export_cache_recommended_actions(activity_export_cache_candidates))
    recommended_actions.extend(_runtime_permission_recommended_actions(runtime_permission_candidates))
    return {
        "mode": "read_only",
        "raw_access": False,
        "message": "只生成清单，不删除任何文件。旧 cleaned/learning 导出缓存只报告；daemon 自动清理不删除，只有人工 hygiene-cleanup apply 加确认词才删除。",
        "retention_scope_note": "today+yesterday 保留策略管理当前 live runtime 和 live runtime 外的活动数据快照；非活动源码/配置备份只列出不自动删除。",
        "activity_export_cache_auto_cleanup_enabled": ACTIVITY_EXPORT_CACHE_AUTO_CLEANUP_ENABLED,
        "locations": locations,
        "release_backups": release_backups,
        "feedback_recoveries": feedback_recoveries,
        "ha_cache_residue_candidates": ha_cache_residue_candidates,
        "ha_cache_residue_summary": _ha_cache_residue_summary(ha_cache_residue_candidates),
        "activity_export_cache_candidates": activity_export_cache_candidates,
        "activity_export_cache_summary": _activity_export_cache_summary(activity_export_cache_candidates),
        "runtime_permission_candidates": runtime_permission_candidates,
        "runtime_permission_summary": _runtime_permission_summary(runtime_permission_candidates),
        "backup_candidates": backup_candidates,
        "risk_summary": _risk_summary(backup_candidates),
        "recommended_actions": recommended_actions,
        "automation_plan": automation_plan,
    }


def backup_hygiene_report(core_root: str | Path, db_path: str | Path) -> dict:
    report = data_hygiene_report(core_root, db_path)
    return {
        "mode": "read_only",
        "raw_access": False,
        "message": "只生成备份清理候选，不删除任何文件。旧 cleaned/learning 导出缓存只报告；daemon 自动清理不删除，人工 hygiene-cleanup --apply 加确认词才删除。",
        "retention_scope_note": report["retention_scope_note"],
        "release_backups": report["release_backups"],
        "feedback_recoveries": report["feedback_recoveries"],
        "activity_export_cache_auto_cleanup_enabled": report["activity_export_cache_auto_cleanup_enabled"],
        "ha_cache_residue_candidates": report["ha_cache_residue_candidates"],
        "ha_cache_residue_summary": report["ha_cache_residue_summary"],
        "activity_export_cache_candidates": report["activity_export_cache_candidates"],
        "activity_export_cache_summary": report["activity_export_cache_summary"],
        "runtime_permission_candidates": report["runtime_permission_candidates"],
        "runtime_permission_summary": report["runtime_permission_summary"],
        "backup_candidates": report["backup_candidates"],
        "risk_summary": report["risk_summary"],
        "recommended_actions": report["recommended_actions"],
        "automation_plan": report["automation_plan"],
    }


def _release_backups_inventory(root: str | Path) -> dict:
    path = Path(root).expanduser()
    items: list[dict] = []
    root_kind = "missing"
    scan_error_count = 0
    scan_complete = True
    try:
        root_metadata = path.lstat()
    except FileNotFoundError:
        root_metadata = None
    except OSError:
        root_metadata = None
        root_kind = "unavailable"
        scan_error_count = 1
        scan_complete = False

    if root_metadata is not None:
        root_kind = _filesystem_entry_kind(root_metadata.st_mode)
        if root_kind == "directory":
            try:
                with os.scandir(path) as entries:
                    children = sorted((Path(entry.path) for entry in entries), key=lambda item: item.name)
            except OSError:
                children = []
                scan_error_count += 1
                scan_complete = False
            for child in children:
                usage = _metadata_only_filesystem_usage(child)
                scan_error_count += int(usage["scan_error_count"])
                scan_complete = scan_complete and bool(usage["scan_complete"])
                items.append(
                    {
                        "name": child.name,
                        "kind": usage["kind"],
                        "logical_bytes": usage["logical_bytes"],
                        "allocated_bytes": usage["allocated_bytes"],
                        "entry_count": usage["entry_count"],
                        "modified_at": usage["modified_at"],
                        "scan_complete": usage["scan_complete"],
                        "scan_error_count": usage["scan_error_count"],
                        "apfs_clone_uncertainty": True,
                    }
                )
        elif root_kind != "missing":
            scan_complete = False

    return {
        "mode": "read_only",
        "raw_access": False,
        "root": str(path),
        "exists": root_metadata is not None,
        "is_directory": root_kind == "directory",
        "root_kind": root_kind,
        "entry_count": len(items),
        "logical_bytes": sum(int(item["logical_bytes"]) for item in items),
        "allocated_bytes": sum(int(item["allocated_bytes"]) for item in items),
        "scan_complete": scan_complete,
        "scan_error_count": scan_error_count,
        "apfs_clone_uncertainty": True,
        "apfs_clone_uncertainty_reason": "POSIX metadata cannot prove whether APFS extents are cloned or shared.",
        "automatic_cleanup_enabled": False,
        "items": items,
    }


def _metadata_only_filesystem_usage(path: Path) -> dict:
    logical_bytes = 0
    allocated_bytes = 0
    entry_count = 0
    scan_error_count = 0
    modified_at = ""
    root_kind = "unavailable"
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            metadata = current.lstat()
        except OSError:
            scan_error_count += 1
            continue
        kind = _filesystem_entry_kind(metadata.st_mode)
        if entry_count == 0:
            root_kind = kind
            modified_at = _format_mtime(metadata.st_mtime)
        entry_count += 1
        if kind in {"file", "symlink", "other"}:
            logical_bytes += int(metadata.st_size)
        allocated_bytes += int(getattr(metadata, "st_blocks", 0) or 0) * 512
        if kind != "directory":
            continue
        try:
            with os.scandir(current) as entries:
                stack.extend(Path(entry.path) for entry in entries)
        except OSError:
            scan_error_count += 1
    return {
        "kind": root_kind,
        "logical_bytes": logical_bytes,
        "allocated_bytes": allocated_bytes,
        "entry_count": entry_count,
        "modified_at": modified_at,
        "scan_complete": scan_error_count == 0,
        "scan_error_count": scan_error_count,
    }


def _filesystem_entry_kind(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"


def safe_delete_ha_cache_residue(
    paths: list[str | Path],
    cache_root: str | Path,
    *,
    apply: bool = False,
    confirm: str = "",
) -> dict:
    root = Path(cache_root).expanduser().resolve()
    if apply and confirm != HA_CACHE_RESIDUE_CONFIRMATION:
        raise ValueError(f"apply requires confirm={HA_CACHE_RESIDUE_CONFIRMATION}")
    results: list[dict] = []
    deleted = 0
    for raw_path in paths:
        path = Path(raw_path).expanduser()
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        allowed = (
            _is_relative_to(resolved, root)
            and path.name.startswith("ha_pkg_")
            and path.suffix.lower() in {".json", ".md"}
            and path.is_file()
        )
        item = {
            "path": str(path),
            "resolved_path": str(resolved),
            "allowed": allowed,
            "deleted": False,
        }
        if allowed and apply:
            try:
                path.unlink()
                item["deleted"] = True
                deleted += 1
            except OSError as exc:
                item["error"] = str(exc)
        results.append(item)
    return {
        "mode": "applied" if apply else "dry_run",
        "deleted_count": deleted,
        "candidates": results,
    }


def hygiene_cleanup_report(
    core_root: str | Path,
    db_path: str | Path,
    *,
    apply: bool = False,
    confirm: str = "",
    keep_backups: int = DEFAULT_KEEP_BACKUPS,
    min_age_days: int = DEFAULT_MIN_AGE_DAYS,
    quarantine_days: int = DEFAULT_QUARANTINE_DAYS,
    include_ha_cache: bool = True,
    include_backup_rotation: bool = True,
    include_activity_export_cache: bool = True,
    include_runtime_permissions: bool = True,
    now: datetime | str | None = None,
) -> dict:
    if apply and confirm != HYGIENE_CLEANUP_CONFIRMATION:
        raise ValueError(f"apply requires confirm={HYGIENE_CLEANUP_CONFIRMATION}")
    keep_backups, min_age_days, quarantine_days = _normalize_hygiene_policy(
        keep_backups=keep_backups,
        min_age_days=min_age_days,
        quarantine_days=quarantine_days,
    )
    current_time = _coerce_datetime(now)
    core = Path(core_root).expanduser().resolve()
    db = Path(db_path).expanduser()
    if not db.is_absolute():
        db = core / db
    hygiene = data_hygiene_report(core, db)
    home = Path.home()
    live_core = home / "Library/Application Support/Personal AI Monitor/core"
    live_data = live_core / "data"
    live_cache_root = live_data / "ha_packages"
    quarantine_root = _quarantine_root()
    strict_cleanup_health = _strict_cleanup_health(db)
    policy = {
        "keep_backups": keep_backups,
        "min_age_days": min_age_days,
        "quarantine_days": quarantine_days,
        "activity_data_retention_days": DEFAULT_ACTIVITY_DATA_RETENTION_DAYS,
        "activity_data_retention_rule": "today_and_yesterday",
        "activity_export_cache_auto_cleanup_enabled": ACTIVITY_EXPORT_CACHE_AUTO_CLEANUP_ENABLED,
        "activity_export_cache_manual_apply_required": True,
        "strict_cleanup_health": strict_cleanup_health,
        "quarantine_root": str(quarantine_root),
        "ha_cache_live_root": str(live_cache_root),
        "mode": "applied" if apply else "dry_run",
    }

    ha_candidates = (
        _ha_cache_cleanup_actions(hygiene.get("ha_cache_residue_candidates") or [], live_cache_root)
        if include_ha_cache
        else []
    )
    backup_actions = (
        _backup_rotation_actions(
            live_data=live_data,
            keep_backups=keep_backups,
            min_age_days=min_age_days,
            quarantine_days=quarantine_days,
            strict_cleanup_health=strict_cleanup_health,
            now=current_time,
        )
        if include_backup_rotation
        else []
    )
    export_actions = (
        _activity_export_cache_actions(
            live_data / "exports",
            strict_cleanup_health=strict_cleanup_health,
            now=current_time,
        )
        if include_activity_export_cache
        else []
    )
    permission_actions = _runtime_permission_actions(live_data) if include_runtime_permissions else []
    purge_actions = _quarantine_purge_actions(quarantine_root, current_time)

    ha_result = _ha_cache_cleanup_result(ha_candidates)
    backup_result = _backup_rotation_result(backup_actions)
    export_result = _activity_export_cache_result(export_actions)
    permission_result = _runtime_permission_result(permission_actions)
    purge_result = _quarantine_purge_result(purge_actions)

    if apply:
        ha_result = _apply_ha_cache_cleanup(ha_candidates, live_cache_root)
        backup_result = _apply_backup_rotation(
            backup_actions,
            quarantine_root=quarantine_root,
            quarantine_days=quarantine_days,
            now=current_time,
        )
        export_result = _apply_activity_export_cache_cleanup(export_actions, live_data / "exports", current_time)
        permission_result = _apply_runtime_permission_cleanup(permission_actions)
        purge_result = _apply_quarantine_purge(purge_actions, quarantine_root)

    return {
        "mode": "applied" if apply else "dry_run",
        "activity_export_cache_auto_cleanup_enabled": ACTIVITY_EXPORT_CACHE_AUTO_CLEANUP_ENABLED,
        "policy": policy,
        "ha_cache": ha_result,
        "backup_rotation": backup_result,
        "activity_export_cache": export_result,
        "runtime_permissions": permission_result,
        "quarantine_purge": purge_result,
        "quarantine_state": hygiene_cleanup_state(core, db),
        "summary": {
            "ha_cache_candidates": len(ha_candidates),
            "ha_cache_deleted": int(ha_result.get("deleted_count") or 0),
            "backup_candidates": len(backup_actions),
            "backup_deleted": int(backup_result.get("deleted_count") or 0),
            "backup_quarantined": int(backup_result.get("quarantined_count") or 0),
            "activity_export_cache_candidates": len([item for item in export_actions if item.get("action") == "delete"]),
            "activity_export_cache_deleted": int(export_result.get("deleted_count") or 0),
            "runtime_permission_candidates": len(permission_actions),
            "runtime_permission_fixed": int(permission_result.get("fixed_count") or 0),
            "quarantine_purge_candidates": len(purge_actions),
            "quarantine_purged": int(purge_result.get("purged_count") or 0),
        },
        "message": (
            "已执行安全旧数据清理。"
            if apply
            else f"Dry run only. Add --apply and --confirm-hygiene-cleanup {HYGIENE_CLEANUP_CONFIRMATION} to execute."
        ),
    }


def hygiene_cleanup_state(core_root: str | Path, db_path: str | Path | None = None) -> dict:
    del core_root, db_path
    root = _quarantine_root()
    items: list[dict] = []
    if root.exists() and root.is_dir():
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            manifest = _read_manifest(child / "manifest.json")
            try:
                stat = child.stat()
                modified_at = _format_mtime(stat.st_mtime)
            except OSError:
                modified_at = ""
            items.append(
                {
                    "path": str(child),
                    "item_id": manifest.get("item_id") or child.name,
                    "original_path": manifest.get("original_path") or "",
                    "type": manifest.get("type") or "unknown",
                    "quarantined_at": manifest.get("quarantined_at") or "",
                    "delete_after": manifest.get("delete_after") or "",
                    "size_bytes": int(manifest.get("size_bytes") or _dir_size(child)),
                    "modified_at": modified_at,
                }
            )
    total_size = sum(int(item.get("size_bytes") or 0) for item in items)
    last_run_at = ""
    if items:
        last_run_at = max(str(item.get("quarantined_at") or item.get("modified_at") or "") for item in items)
    return {
        "quarantine_root": str(root),
        "quarantine_exists": root.exists(),
        "quarantine_count": len(items),
        "quarantine_size_bytes": int(total_size),
        "last_quarantined_at": last_run_at,
        "next_delete_after": _min_non_empty(str(item.get("delete_after") or "") for item in items),
        "items": items[:50],
    }


def _describe_location(label: str, path: Path, *, live_data: Path) -> dict:
    exists = path.exists()
    resolved = path.resolve() if exists else path.expanduser()
    marker_paths = [
        path / "activity.sqlite",
        path / "data/activity.sqlite",
        path / "core/data/activity.sqlite",
        path / "ha_packages",
        path / "data/ha_packages",
        path / "core/data/ha_packages",
        path / "raw_vault",
        path / "data/raw_vault",
        path / "core/data/raw_vault",
    ]
    markers = [str(marker) for marker in marker_paths if marker.exists()]
    recursive_markers = _scan_runtime_markers(path, live_data=live_data) if exists and path.is_dir() else []
    return {
        "label": label,
        "path": str(path),
        "exists": exists,
        "is_dir": path.is_dir() if exists else False,
        "is_symlink": path.is_symlink(),
        "resolved_path": str(resolved),
        "possible_runtime_data": bool(markers or recursive_markers),
        "runtime_markers": markers,
        "recursive_runtime_markers": recursive_markers,
        "recursive_marker_count": len(recursive_markers),
        "recursive_marker_limit_reached": len(recursive_markers) >= MAX_MARKERS_PER_LOCATION,
        "recommendation": _recommendation(label, exists, bool(markers or recursive_markers)),
    }


def _recommendation(label: str, exists: bool, possible_runtime_data: bool) -> str:
    if not exists:
        return "未发现。"
    if possible_runtime_data:
        return "可能包含本机活动数据；如需删除或归档，先单独确认范围。"
    if "backup" in label or "legacy" in label or "desktop" in label:
        return "可能是旧副本或备份；建议人工确认后再决定是否清理。"
    return "位置存在；当前只记录，不做改动。"


def _scan_runtime_markers(root: Path, *, live_data: Path) -> list[dict]:
    markers: list[dict] = []
    live_resolved = _resolve_existing(live_data)
    root_resolved = _resolve_existing(root)
    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        dirs[:] = [name for name in dirs if not name.startswith(".git") and name not in {"node_modules", "__pycache__"}]
        for directory in list(dirs):
            if directory in MARKER_DIR_NAMES:
                markers.append(_marker_info(current_path / directory, root_resolved, live_resolved, marker_type=f"{directory}_dir"))
                if len(markers) >= MAX_MARKERS_PER_LOCATION:
                    return markers
        for filename in files:
            candidate = current_path / filename
            suffix = candidate.suffix.lower()
            if filename in SQLITE_FILE_NAMES or suffix in SQLITE_SUFFIXES:
                markers.append(_marker_info(candidate, root_resolved, live_resolved, marker_type="sqlite_database"))
                if len(markers) >= MAX_MARKERS_PER_LOCATION:
                    return markers
    return markers


def _marker_info(path: Path, root_resolved: Path, live_resolved: Path, *, marker_type: str) -> dict:
    resolved = _resolve_existing(path)
    outside_live_runtime = not _is_relative_to(resolved, live_resolved) or _is_live_runtime_backup(resolved, live_resolved)
    try:
        stat = path.stat()
        size_bytes = int(stat.st_size) if path.is_file() else _dir_size(path)
        modified_at = _format_mtime(stat.st_mtime)
        permissions = oct(stat.st_mode & 0o777)
    except OSError:
        size_bytes = 0
        modified_at = ""
        permissions = ""
    risk_level = _risk_level(marker_type, outside_live_runtime, size_bytes)
    return {
        "type": marker_type,
        "path": str(path),
        "relative_path": _relative_path(resolved, root_resolved),
        "resolved_path": str(resolved),
        "size_bytes": size_bytes,
        "modified_at": modified_at,
        "permissions": permissions,
        "outside_live_runtime": outside_live_runtime,
        "risk_level": risk_level,
        "recommendation": _marker_recommendation(marker_type, outside_live_runtime, risk_level),
    }


def _backup_candidates(locations: list[dict], *, extra_candidates: list[dict] | None = None) -> list[dict]:
    candidates: list[dict] = []
    seen: set[str] = set()
    for location in locations:
        for marker in location.get("recursive_runtime_markers", []):
            if marker.get("outside_live_runtime"):
                resolved_path = str(marker.get("resolved_path") or marker.get("path") or "")
                if resolved_path in seen:
                    continue
                seen.add(resolved_path)
                candidates.append(
                    {
                        "location_label": location["label"],
                        **marker,
                    }
                )
    for marker in extra_candidates or []:
        resolved_path = str(marker.get("resolved_path") or marker.get("path") or "")
        if resolved_path in seen:
            continue
        seen.add(resolved_path)
        candidates.append(marker)
    candidates.sort(key=lambda item: (item.get("risk_level", ""), -int(item.get("size_bytes") or 0), item.get("path", "")))
    return candidates


def _hygiene_automation_preview(core: Path, db: Path, ha_cache_candidates: list[dict]) -> dict:
    del core
    home = Path.home()
    live_data = home / "Library/Application Support/Personal AI Monitor/core/data"
    live_cache_root = live_data / "ha_packages"
    current_time = _coerce_datetime(None)
    strict_health = _strict_cleanup_health(db)
    backup_actions = _backup_rotation_actions(
        live_data=live_data,
        keep_backups=DEFAULT_KEEP_BACKUPS,
        min_age_days=DEFAULT_MIN_AGE_DAYS,
        quarantine_days=DEFAULT_QUARANTINE_DAYS,
        strict_cleanup_health=strict_health,
        now=current_time,
    )
    ha_actions = _ha_cache_cleanup_actions(ha_cache_candidates, live_cache_root)
    export_actions = _activity_export_cache_actions(live_data / "exports", strict_cleanup_health=strict_health, now=current_time)
    permission_actions = _runtime_permission_actions(live_data)
    return {
        "mode": "dry_run",
        "policy": {
            "keep_backups": DEFAULT_KEEP_BACKUPS,
            "min_age_days": DEFAULT_MIN_AGE_DAYS,
            "quarantine_days": DEFAULT_QUARANTINE_DAYS,
            "activity_data_retention_days": DEFAULT_ACTIVITY_DATA_RETENTION_DAYS,
            "activity_data_retention_rule": "today_and_yesterday",
            "activity_export_cache_auto_cleanup_enabled": ACTIVITY_EXPORT_CACHE_AUTO_CLEANUP_ENABLED,
            "activity_export_cache_manual_apply_required": True,
            "quarantine_root": str(_quarantine_root()),
        },
        "ha_cache_cleanup": _ha_cache_cleanup_result(ha_actions),
        "backup_rotation": _backup_rotation_result(backup_actions),
        "activity_export_cache": _activity_export_cache_result(export_actions),
        "runtime_permissions": _runtime_permission_result(permission_actions),
        "quarantine_purge": _quarantine_purge_result(_quarantine_purge_actions(_quarantine_root(), current_time)),
    }


def _normalize_hygiene_policy(*, keep_backups: int, min_age_days: int, quarantine_days: int) -> tuple[int, int, int]:
    keep_backups = int(keep_backups)
    min_age_days = int(min_age_days)
    quarantine_days = int(quarantine_days)
    if keep_backups < 1:
        raise ValueError("keep_backups must be at least 1")
    if min_age_days < 0:
        raise ValueError("min_age_days must be at least 0")
    if quarantine_days < 1:
        raise ValueError("quarantine_days must be at least 1")
    return keep_backups, min_age_days, quarantine_days


def _coerce_datetime(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _quarantine_root() -> Path:
    return Path.home() / "Library/Application Support/Personal AI Monitor/quarantine"


def _ha_cache_cleanup_actions(candidates: list[dict], live_cache_root: Path) -> list[dict]:
    actions: list[dict] = []
    root = live_cache_root.expanduser()
    try:
        root_resolved = root.resolve()
    except OSError:
        root_resolved = root
    for item in candidates:
        path = Path(str(item.get("path") or "")).expanduser()
        resolved_text = str(item.get("resolved_path") or "")
        try:
            resolved = Path(resolved_text).expanduser().resolve() if resolved_text else path.resolve()
        except OSError:
            resolved = Path(resolved_text).expanduser() if resolved_text else path
        reasons = {str(reason) for reason in (item.get("residue_reasons") or [])}
        record_status = str(item.get("record_status") or "")
        allowed = (
            item.get("type") == "ha_package_cache_residue"
            and _is_relative_to(resolved, root_resolved)
            and path.name.startswith("ha_pkg_")
            and path.suffix.lower() in {".json", ".md"}
            and (record_status in HA_CACHE_CLEANUP_STATUSES or bool(reasons & HA_CACHE_CLEANUP_REASONS))
        )
        reason = "delete_invalid_ha_cache_residue" if allowed else "skip_not_safe_ha_cache_residue"
        actions.append(
            {
                "action": "delete" if allowed else "skip",
                "reason": reason,
                "path": str(path),
                "resolved_path": str(resolved),
                "package_id": item.get("package_id") or "",
                "record_status": record_status,
                "residue_reasons": sorted(reasons),
                "size_bytes": int(item.get("size_bytes") or 0),
                "live_cache_root": str(root),
                "allowed": allowed,
            }
        )
    actions.sort(key=lambda item: (item["action"] != "delete", item["path"]))
    return actions


def _ha_cache_cleanup_result(actions: list[dict]) -> dict:
    delete_actions = [item for item in actions if item.get("action") == "delete"]
    skipped = [item for item in actions if item.get("action") != "delete"]
    return {
        "mode": "dry_run",
        "candidate_count": len(delete_actions),
        "deleted_count": 0,
        "skipped_count": len(skipped),
        "total_size_bytes": sum(int(item.get("size_bytes") or 0) for item in delete_actions),
        "actions": actions,
    }


def _apply_ha_cache_cleanup(actions: list[dict], live_cache_root: Path) -> dict:
    delete_paths = [item["path"] for item in actions if item.get("action") == "delete"]
    deletion = safe_delete_ha_cache_residue(
        delete_paths,
        live_cache_root,
        apply=True,
        confirm=HA_CACHE_RESIDUE_CONFIRMATION,
    )
    deleted_by_path = {item.get("path"): item for item in deletion.get("candidates") or []}
    applied_actions: list[dict] = []
    for action in actions:
        if action.get("action") == "delete":
            result = deleted_by_path.get(action.get("path") or "", {})
            applied_actions.append(
                {
                    **action,
                    "deleted": bool(result.get("deleted")),
                    "error": result.get("error") or "",
                }
            )
        else:
            applied_actions.append({**action, "deleted": False})
    return {
        "mode": "applied",
        "candidate_count": len(delete_paths),
        "deleted_count": int(deletion.get("deleted_count") or 0),
        "skipped_count": sum(1 for item in actions if item.get("action") != "delete"),
        "total_size_bytes": sum(int(item.get("size_bytes") or 0) for item in actions if item.get("action") == "delete"),
        "actions": applied_actions,
    }


def _backup_rotation_actions(
    *,
    live_data: Path,
    keep_backups: int,
    min_age_days: int,
    quarantine_days: int,
    strict_cleanup_health: dict,
    now: datetime,
) -> list[dict]:
    del quarantine_days
    home = Path.home()
    groups: list[tuple[str, str, Path, list[Path]]] = [
        (
            "personal_ai_monitor_backups",
            "whole_backup_snapshot",
            home / "PersonalAI-Monitor-backups",
            _existing_children(home / "PersonalAI-Monitor-backups", directories_only=True),
        ),
        (
            "live_sqlite_backups",
            "live_sqlite_backup",
            live_data / "backups",
            _existing_children(live_data / "backups", suffixes=SQLITE_SUFFIXES),
        ),
    ]
    actions: list[dict] = []
    health_ok = bool(strict_cleanup_health.get("ok"))
    for group_name, item_type, allowed_root, paths in groups:
        ranked = sorted(paths, key=_path_mtime, reverse=True)
        for index, path in enumerate(ranked):
            metadata = _backup_item_metadata(path, item_type=item_type, group_name=group_name, rank=index + 1, now=now)
            markers = _activity_data_markers(path)
            activity_marker_count = len(markers)
            outside_retention = _outside_activity_retention_window(markers, now)
            newest_marker = _newest_marker_modified_at(markers)
            marker_types = sorted({str(marker.get("type") or "") for marker in markers if marker.get("type")})
            if activity_marker_count and outside_retention and health_ok:
                action = "delete"
                reason = "old_activity_data_snapshot_outside_today_yesterday"
            elif activity_marker_count and outside_retention and not health_ok:
                action = "keep"
                reason = "strict_cleanup_health_not_ok"
            elif activity_marker_count:
                action = "keep"
                reason = "within_today_yesterday_activity_retention"
            elif item_type == "whole_backup_snapshot":
                action = "keep"
                reason = "non_activity_backup_retained"
            elif index < keep_backups:
                action = "keep"
                reason = "keep_latest_backup"
            elif metadata["age_days"] < min_age_days:
                action = "keep"
                reason = "below_min_age"
            else:
                action = "quarantine"
                reason = "old_backup_rotation"
            actions.append(
                {
                    **metadata,
                    "action": action,
                    "reason": reason,
                    "keep_backups": keep_backups,
                    "min_age_days": min_age_days,
                    "activity_marker_count": activity_marker_count,
                    "activity_marker_types": marker_types,
                    "activity_newest_marker_modified_at": newest_marker,
                    "outside_activity_retention_window": bool(outside_retention),
                    "activity_retention_cutoff": _activity_retention_cutoff(now).astimezone().isoformat(timespec="seconds"),
                    "strict_cleanup_health_ok": health_ok,
                    "strict_cleanup_health_summary": strict_cleanup_health.get("summary") or "",
                    "allowed_delete_root": str(allowed_root),
                }
            )
    actions.sort(key=lambda item: (item["group"], item["action"] not in {"delete", "quarantine"}, item["rank"]))
    return actions


def _activity_export_cache_actions(
    exports_root: Path,
    *,
    strict_cleanup_health: dict,
    now: datetime,
) -> list[dict]:
    root = exports_root.expanduser()
    if not root.exists() or not root.is_dir():
        return []
    actions: list[dict] = []
    health_ok = bool(strict_cleanup_health.get("ok"))
    cutoff = _activity_retention_cutoff(now)
    for path in sorted(root.iterdir()):
        if not path.is_file() or path.is_symlink() or not _looks_like_activity_export_cache(path):
            continue
        metadata = _export_cache_item_metadata(path, root=root, now=now)
        outside_retention = metadata["mtime_utc"] < cutoff
        if outside_retention and health_ok:
            action = "delete"
            reason = "old_activity_export_cache_outside_today_yesterday"
        elif outside_retention:
            action = "keep"
            reason = "strict_cleanup_health_not_ok"
        else:
            action = "keep"
            reason = "within_today_yesterday_activity_retention"
        actions.append(
            {
                **{key: value for key, value in metadata.items() if key != "mtime_utc"},
                "action": action,
                "reason": reason,
                "outside_activity_retention_window": bool(outside_retention),
                "activity_retention_cutoff": cutoff.astimezone().isoformat(timespec="seconds"),
                "strict_cleanup_health_ok": health_ok,
                "strict_cleanup_health_summary": strict_cleanup_health.get("summary") or "",
                "allowed_delete_root": str(root),
                "recommendation": (
                    "旧 cleaned/learning 导出属于活动派生缓存；超过 today+yesterday 且 live DB/HA 健康时，"
                    "daemon 自动清理不删除，只有显式 hygiene-cleanup apply 才可删除，并保留 metadata-only 审计。"
                    if outside_retention
                    else "当前处于 today+yesterday 保留窗口内。"
                ),
            }
        )
    actions.sort(key=lambda item: (item["action"] != "delete", item["modified_at"], item["path"]))
    return actions


def _looks_like_activity_export_cache(path: Path) -> bool:
    name = path.name.lower()
    return path.suffix.lower() in ACTIVITY_EXPORT_CACHE_SUFFIXES and any(
        keyword in name for keyword in ACTIVITY_EXPORT_CACHE_KEYWORDS
    )


def _export_cache_item_metadata(path: Path, *, root: Path, now: datetime) -> dict:
    try:
        stat = path.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        size_bytes = int(stat.st_size)
        permissions = oct(stat.st_mode & 0o777)
    except OSError:
        mtime = datetime.fromtimestamp(0, tz=timezone.utc)
        size_bytes = 0
        permissions = ""
    return {
        "type": "activity_export_cache",
        "group": "live_activity_exports",
        "path": str(path),
        "relative_path": _relative_path(_resolve_existing(path), _resolve_existing(root)),
        "resolved_path": str(_resolve_existing(path)),
        "rank": 0,
        "size_bytes": size_bytes,
        "modified_at": mtime.astimezone().isoformat(timespec="seconds"),
        "age_days": max(0, int((now - mtime).total_seconds() // 86400)),
        "permissions": permissions,
        "activity_marker_count": 1,
        "activity_marker_types": ["activity_export_cache"],
        "activity_newest_marker_modified_at": mtime.astimezone().isoformat(timespec="seconds"),
        "mtime_utc": mtime,
    }


def _activity_export_cache_result(actions: list[dict]) -> dict:
    delete = [item for item in actions if item.get("action") == "delete"]
    kept = [item for item in actions if item.get("action") != "delete"]
    return {
        "mode": "dry_run",
        "auto_cleanup_enabled": ACTIVITY_EXPORT_CACHE_AUTO_CLEANUP_ENABLED,
        "manual_apply_required": True,
        "candidate_count": len(delete),
        "deleted_count": 0,
        "kept_count": len(kept),
        "total_size_bytes": sum(int(item.get("size_bytes") or 0) for item in delete),
        "actions": actions,
    }


def _apply_activity_export_cache_cleanup(actions: list[dict], exports_root: Path, now: datetime) -> dict:
    applied: list[dict] = []
    deleted = 0
    for action in actions:
        if action.get("action") != "delete":
            applied.append({**action, "deleted": False})
            continue
        result = _delete_activity_export_cache_item(action, exports_root)
        if result.get("deleted"):
            deleted += 1
        applied.append({**action, **result})
    audit_path = _write_backup_cleanup_audit(applied, now) if deleted else ""
    return {
        "mode": "applied",
        "auto_cleanup_enabled": ACTIVITY_EXPORT_CACHE_AUTO_CLEANUP_ENABLED,
        "manual_apply_required": True,
        "candidate_count": sum(1 for item in actions if item.get("action") == "delete"),
        "deleted_count": deleted,
        "kept_count": sum(1 for item in actions if item.get("action") != "delete"),
        "total_size_bytes": sum(int(item.get("size_bytes") or 0) for item in actions if item.get("action") == "delete"),
        "audit_path": audit_path,
        "actions": applied,
    }


def _delete_activity_export_cache_item(action: dict, exports_root: Path) -> dict:
    source = Path(str(action.get("path") or "")).expanduser()
    root = exports_root.expanduser()
    if not source.exists():
        return {"deleted": False, "error": "source_missing"}
    try:
        source_resolved = source.resolve()
    except OSError:
        source_resolved = source
    try:
        root_resolved = root.resolve()
    except OSError:
        root_resolved = root
    allowed = (
        action.get("type") == "activity_export_cache"
        and action.get("action") == "delete"
        and bool(action.get("strict_cleanup_health_ok"))
        and bool(action.get("outside_activity_retention_window"))
        and source.is_file()
        and _looks_like_activity_export_cache(source)
        and _is_relative_to(source_resolved, root_resolved)
        and source_resolved != root_resolved
    )
    if not allowed:
        return {"deleted": False, "error": "delete_not_allowed_by_policy"}
    try:
        source.unlink()
        return {"deleted": True, "permanent_deleted": True}
    except OSError as exc:
        return {"deleted": False, "error": str(exc)}


def _activity_export_cache_summary(actions: list[dict]) -> dict:
    delete = [item for item in actions if item.get("action") == "delete"]
    keep = [item for item in actions if item.get("action") != "delete"]
    return {
        "candidate_count": len(delete),
        "kept_count": len(keep),
        "total_size_bytes": sum(int(item.get("size_bytes") or 0) for item in actions),
        "delete_size_bytes": sum(int(item.get("size_bytes") or 0) for item in delete),
    }


def _runtime_permission_actions(data_root: Path) -> list[dict]:
    root = data_root.expanduser()
    if not root.exists():
        return []
    actions: list[dict] = []
    for path in _iter_runtime_permission_paths(root):
        if path.is_symlink():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if path.is_dir():
            desired = RUNTIME_DIRECTORY_MODE
            item_type = "runtime_directory"
        elif path.is_file():
            desired = _desired_file_mode(path, root, stat.st_mode & 0o777)
            item_type = "runtime_executable" if desired == RUNTIME_EXECUTABLE_MODE else "runtime_file"
        else:
            continue
        current = stat.st_mode & 0o777
        if current == desired:
            continue
        actions.append(
            {
                "action": "chmod",
                "reason": "runtime_permission_not_private",
                "type": item_type,
                "path": str(path),
                "relative_path": _relative_path(_resolve_existing(path), _resolve_existing(root)),
                "resolved_path": str(_resolve_existing(path)),
                "current_mode": oct(current),
                "desired_mode": oct(desired),
                "size_bytes": int(stat.st_size) if path.is_file() else 0,
                "modified_at": _format_mtime(stat.st_mtime),
                "allowed_root": str(root),
                "recommendation": "运行目录统一为 0700，普通文件统一为 0600，data/bin 中已有执行位的 helper 保留为 0700。",
            }
        )
    actions.sort(key=lambda item: (item["type"], item["path"]))
    return actions


def _iter_runtime_permission_paths(root: Path):
    yield root
    if not root.is_dir():
        return
    try:
        children = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError:
        return
    for child in children:
        if child.is_symlink():
            continue
        if child.is_dir():
            if child.name not in RUNTIME_PERMISSION_SCAN_DIR_NAMES:
                continue
            yield child
            for current, dirs, files in os.walk(child, followlinks=False):
                current_path = Path(current)
                dirs[:] = [name for name in dirs if name not in {"__pycache__"}]
                for directory in sorted(dirs):
                    yield current_path / directory
                for filename in sorted(files):
                    yield current_path / filename
            continue
        if child.is_file() and _is_runtime_permission_root_file(child):
            yield child


def _is_runtime_permission_root_file(path: Path) -> bool:
    name = path.name
    lower = name.lower()
    suffix = path.suffix.lower()
    return (
        name in RUNTIME_PERMISSION_ROOT_FILE_NAMES
        or suffix in RUNTIME_PERMISSION_ROOT_FILE_SUFFIXES
        or lower.endswith((".sqlite-wal", ".sqlite-shm", ".sqlite3-wal", ".sqlite3-shm", ".db-wal", ".db-shm"))
    )


def _desired_file_mode(path: Path, root: Path, current_mode: int) -> int:
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = Path(path.name)
    if relative.parts and relative.parts[0] == "bin" and current_mode & 0o111:
        return RUNTIME_EXECUTABLE_MODE
    return RUNTIME_FILE_MODE


def _runtime_permission_result(actions: list[dict]) -> dict:
    return {
        "mode": "dry_run",
        "candidate_count": len(actions),
        "fixed_count": 0,
        "actions": actions,
    }


def _apply_runtime_permission_cleanup(actions: list[dict]) -> dict:
    applied: list[dict] = []
    fixed = 0
    for action in actions:
        path = Path(str(action.get("path") or "")).expanduser()
        desired_text = str(action.get("desired_mode") or "")
        try:
            desired = int(desired_text, 8)
        except ValueError:
            applied.append({**action, "fixed": False, "error": "invalid_desired_mode"})
            continue
        try:
            path.chmod(desired)
            fixed += 1
            applied.append({**action, "fixed": True})
        except OSError as exc:
            applied.append({**action, "fixed": False, "error": str(exc)})
    return {
        "mode": "applied",
        "candidate_count": len(actions),
        "fixed_count": fixed,
        "actions": applied,
    }


def _runtime_permission_summary(actions: list[dict]) -> dict:
    by_type: dict[str, int] = {}
    for item in actions:
        item_type = str(item.get("type") or "unknown")
        by_type[item_type] = by_type.get(item_type, 0) + 1
    return {
        "candidate_count": len(actions),
        "by_type": by_type,
    }


def _activity_retention_cutoff(now: datetime) -> datetime:
    local_now = now.astimezone()
    yesterday = local_now.date() - timedelta(days=1)
    local_cutoff = datetime.combine(yesterday, time.min, tzinfo=local_now.tzinfo)
    return local_cutoff.astimezone(timezone.utc)


def _activity_data_markers(path: Path) -> list[dict]:
    if not path.exists():
        return []
    if path.is_file():
        if path.name in SQLITE_FILE_NAMES or path.suffix.lower() in SQLITE_SUFFIXES:
            return [_activity_marker_record(path, "sqlite_database")]
        return []
    if not path.is_dir():
        return []
    markers: list[dict] = []
    for current, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [name for name in dirs if not name.startswith(".git") and name not in {"node_modules", "__pycache__"}]
        current_path = Path(current)
        for directory in list(dirs):
            if directory in MARKER_DIR_NAMES:
                marker_type = f"{directory}_dir"
                markers.append(_activity_marker_record(current_path / directory, marker_type))
                if len(markers) >= MAX_MARKERS_PER_LOCATION:
                    return markers
        for filename in files:
            candidate = current_path / filename
            if filename in SQLITE_FILE_NAMES or candidate.suffix.lower() in SQLITE_SUFFIXES:
                markers.append(_activity_marker_record(candidate, "sqlite_database"))
                if len(markers) >= MAX_MARKERS_PER_LOCATION:
                    return markers
    return markers


def _activity_marker_record(path: Path, marker_type: str) -> dict:
    try:
        stat = path.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        size_bytes = int(stat.st_size) if path.is_file() else _dir_size(path)
    except OSError:
        mtime = datetime.fromtimestamp(0, tz=timezone.utc)
        size_bytes = 0
    return {
        "type": marker_type,
        "path": str(path),
        "size_bytes": size_bytes,
        "modified_at": mtime.astimezone().isoformat(timespec="seconds"),
        "mtime_epoch": mtime.timestamp(),
    }


def _outside_activity_retention_window(markers: list[dict], now: datetime) -> bool:
    if not markers:
        return False
    cutoff = _activity_retention_cutoff(now)
    newest_epoch = max(float(marker.get("mtime_epoch") or 0.0) for marker in markers)
    newest = datetime.fromtimestamp(newest_epoch, tz=timezone.utc)
    return newest < cutoff


def _newest_marker_modified_at(markers: list[dict]) -> str:
    if not markers:
        return ""
    newest_epoch = max(float(marker.get("mtime_epoch") or 0.0) for marker in markers)
    return datetime.fromtimestamp(newest_epoch, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def _strict_cleanup_health(db: Path) -> dict:
    if not db.exists():
        return {"ok": False, "summary": "live DB 不存在，旧活动数据不自动永久删除。", "checks": {"db_exists": False}}
    checks = {"db_exists": True, "quick_check_ok": False, "ha_jobs_clear": True}
    issues: list[str] = []
    try:
        conn = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            quick = conn.execute("PRAGMA quick_check").fetchone()
            checks["quick_check_ok"] = bool(quick and str(quick[0]).lower() == "ok")
            if not checks["quick_check_ok"]:
                issues.append("db_quick_check_failed")
            if _table_exists(conn, "ha_automation_jobs"):
                ha_health = _ha_cleanup_job_health(conn)
                checks.update(ha_health)
                if not checks["ha_jobs_clear"]:
                    if int(checks.get("ha_pending_failed_job_count") or 0):
                        issues.append("ha_automation_has_pending_or_failed_jobs")
                    if int(checks.get("ha_blocking_dead_letter_count") or 0):
                        issues.append("ha_automation_has_unresolved_dead_letters")
            else:
                checks["ha_blocking_job_count"] = 0
        finally:
            conn.close()
    except sqlite3.Error as exc:
        checks["quick_check_ok"] = False
        issues.append(f"db_read_failed:{exc}")
    ok = bool(checks.get("quick_check_ok") and checks.get("ha_jobs_clear"))
    return {
        "ok": ok,
        "summary": _strict_cleanup_summary(ok, checks),
        "checks": checks,
        "issues": issues,
    }


def _ha_cleanup_job_health(conn: sqlite3.Connection) -> dict:
    pending_failed = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM ha_automation_jobs
        WHERE status IN ('pending', 'failed')
        """
    ).fetchone()
    latest_completed = _latest_completed_ha_job_time(conn)
    dead_letters = _dead_letter_resolution_counts(conn, latest_completed)
    pending_failed_count = int(pending_failed["count"] or 0)
    blocking_dead_letter_count = int(dead_letters["blocking_dead_letter_count"])
    blocking_count = pending_failed_count + blocking_dead_letter_count
    return {
        "ha_pending_failed_job_count": pending_failed_count,
        "ha_dead_letter_count": int(dead_letters["dead_letter_count"]),
        "ha_archived_dead_letter_count": int(dead_letters["archived_dead_letter_count"]),
        "ha_blocking_dead_letter_count": blocking_dead_letter_count,
        "ha_latest_completed_at": latest_completed or "",
        "ha_blocking_job_count": blocking_count,
        "ha_jobs_clear": blocking_count == 0,
    }


def _latest_completed_ha_job_time(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        """
        SELECT MAX(
          COALESCE(
            NULLIF(completed_at, ''),
            NULLIF(updated_at, ''),
            NULLIF(triggered_at, ''),
            NULLIF(generated_at, ''),
            NULLIF(created_at, ''),
            hour_key
          )
        ) AS latest_completed_at
        FROM ha_automation_jobs
        WHERE status = 'completed'
        """
    ).fetchone()
    return str(row["latest_completed_at"] or "") if row else ""


def _dead_letter_resolution_counts(conn: sqlite3.Connection, latest_completed_at: str) -> dict:
    row = conn.execute(
        """
        SELECT
          COUNT(*) AS dead_letter_count,
          SUM(
            CASE
              WHEN ? != ''
               AND COALESCE(
                 NULLIF(updated_at, ''),
                 NULLIF(completed_at, ''),
                 NULLIF(triggered_at, ''),
                 NULLIF(generated_at, ''),
                 NULLIF(created_at, ''),
                 hour_key
               ) < ?
              THEN 1 ELSE 0
            END
          ) AS archived_dead_letter_count,
          SUM(
            CASE
              WHEN ? = ''
                OR COALESCE(
                  NULLIF(updated_at, ''),
                  NULLIF(completed_at, ''),
                  NULLIF(triggered_at, ''),
                  NULLIF(generated_at, ''),
                  NULLIF(created_at, ''),
                  hour_key
                ) >= ?
              THEN 1 ELSE 0
            END
          ) AS blocking_dead_letter_count
        FROM ha_automation_jobs
        WHERE status = 'dead_letter'
        """,
        (latest_completed_at, latest_completed_at, latest_completed_at, latest_completed_at),
    ).fetchone()
    return {
        "dead_letter_count": int(row["dead_letter_count"] or 0) if row else 0,
        "archived_dead_letter_count": int(row["archived_dead_letter_count"] or 0) if row else 0,
        "blocking_dead_letter_count": int(row["blocking_dead_letter_count"] or 0) if row else 0,
    }


def _strict_cleanup_summary(ok: bool, checks: dict) -> str:
    if not ok:
        return "live DB/HA 状态未确认正常，旧活动快照不自动永久删除。"
    archived = int(checks.get("ha_archived_dead_letter_count") or 0)
    if archived:
        return "live DB/HA 状态正常；历史 HA 死信已由后续成功任务解释，可按 today+yesterday 严格清理旧活动快照。"
    return "live DB/HA 状态正常，可按 today+yesterday 严格清理旧活动快照。"


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def _existing_children(root: Path, *, directories_only: bool = False, suffixes: set[str] | None = None) -> list[Path]:
    if not root.exists() or not root.is_dir():
        return []
    children: list[Path] = []
    for child in root.iterdir():
        if child.name.startswith("."):
            continue
        if directories_only and not child.is_dir():
            continue
        if suffixes is not None and (not child.is_file() or child.suffix.lower() not in suffixes):
            continue
        children.append(child)
    return children


def _path_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _backup_item_metadata(path: Path, *, item_type: str, group_name: str, rank: int, now: datetime) -> dict:
    try:
        stat = path.stat()
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        permissions = oct(stat.st_mode & 0o777)
        size_bytes = int(stat.st_size) if path.is_file() else _dir_size(path)
    except OSError:
        mtime = datetime.fromtimestamp(0, tz=timezone.utc)
        permissions = ""
        size_bytes = 0
    age_days = max(0, int((now - mtime).total_seconds() // 86400))
    return {
        "type": item_type,
        "group": group_name,
        "path": str(path),
        "resolved_path": str(_resolve_existing(path)),
        "rank": int(rank),
        "size_bytes": size_bytes,
        "modified_at": mtime.astimezone().isoformat(timespec="seconds"),
        "age_days": age_days,
        "permissions": permissions,
    }


def _backup_rotation_result(actions: list[dict]) -> dict:
    delete = [item for item in actions if item.get("action") == "delete"]
    quarantine = [item for item in actions if item.get("action") == "quarantine"]
    kept = [item for item in actions if item.get("action") == "keep"]
    return {
        "mode": "dry_run",
        "candidate_count": len(delete) + len(quarantine),
        "delete_candidate_count": len(delete),
        "deleted_count": 0,
        "permanent_delete_count": 0,
        "quarantine_candidate_count": len(quarantine),
        "quarantined_count": 0,
        "kept_count": len(kept),
        "total_size_bytes": sum(int(item.get("size_bytes") or 0) for item in [*delete, *quarantine]),
        "actions": actions,
    }


def _apply_backup_rotation(
    actions: list[dict],
    *,
    quarantine_root: Path,
    quarantine_days: int,
    now: datetime,
) -> dict:
    _ensure_quarantine_root(quarantine_root)
    applied: list[dict] = []
    deleted = 0
    quarantined = 0
    for action in actions:
        if action.get("action") == "delete":
            result = _delete_backup_item(action)
            if result.get("deleted"):
                deleted += 1
            applied.append({**action, **result, "quarantined": False})
            continue
        if action.get("action") != "quarantine":
            applied.append({**action, "deleted": False, "quarantined": False})
            continue
        result = _quarantine_item(action, quarantine_root=quarantine_root, quarantine_days=quarantine_days, now=now)
        if result.get("quarantined"):
            quarantined += 1
        applied.append({**action, "deleted": False, **result})
    audit_path = _write_backup_cleanup_audit(applied, now) if deleted else ""
    return {
        "mode": "applied",
        "candidate_count": sum(1 for item in actions if item.get("action") in {"delete", "quarantine"}),
        "delete_candidate_count": sum(1 for item in actions if item.get("action") == "delete"),
        "deleted_count": deleted,
        "permanent_delete_count": deleted,
        "quarantine_candidate_count": sum(1 for item in actions if item.get("action") == "quarantine"),
        "quarantined_count": quarantined,
        "kept_count": sum(1 for item in actions if item.get("action") == "keep"),
        "total_size_bytes": sum(int(item.get("size_bytes") or 0) for item in actions if item.get("action") in {"delete", "quarantine"}),
        "audit_path": audit_path,
        "actions": applied,
    }


def _delete_backup_item(action: dict) -> dict:
    source = Path(str(action.get("path") or "")).expanduser()
    allowed_root = Path(str(action.get("allowed_delete_root") or "")).expanduser()
    if not source.exists():
        return {"deleted": False, "error": "source_missing"}
    try:
        source_resolved = source.resolve()
    except OSError:
        source_resolved = source
    try:
        root_resolved = allowed_root.resolve()
    except OSError:
        root_resolved = allowed_root
    allowed = (
        bool(action.get("strict_cleanup_health_ok"))
        and bool(action.get("activity_marker_count"))
        and bool(action.get("outside_activity_retention_window"))
        and _is_relative_to(source_resolved, root_resolved)
        and source_resolved != root_resolved
    )
    if not allowed:
        return {"deleted": False, "error": "delete_not_allowed_by_policy"}
    try:
        if source.is_dir():
            shutil.rmtree(source)
        else:
            source.unlink()
        return {"deleted": True, "permanent_deleted": True}
    except OSError as exc:
        return {"deleted": False, "error": str(exc)}


def _write_backup_cleanup_audit(actions: list[dict], now: datetime) -> str:
    deleted = [item for item in actions if item.get("deleted")]
    if not deleted:
        return ""
    root = Path.home() / "Library/Application Support/Personal AI Monitor/cleanup-audits"
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    path = root / f"hygiene-cleanup-{now.astimezone().strftime('%Y%m%d-%H%M%S')}.md"
    lines = [
        "# Personal AI Monitor hygiene cleanup audit",
        "",
        f"- created_at: {now.astimezone().isoformat(timespec='seconds')}",
        f"- deleted_count: {len(deleted)}",
        "- note: metadata only; no content copy retained.",
        "",
        "## Deleted Items",
        "",
    ]
    for item in deleted:
        lines.extend(
            [
                f"- path: {item.get('path')}",
                f"  type: {item.get('type')}",
                f"  size_bytes: {int(item.get('size_bytes') or 0)}",
                f"  modified_at: {item.get('modified_at') or ''}",
                f"  reason: {item.get('reason') or ''}",
                f"  activity_marker_types: {', '.join(item.get('activity_marker_types') or [])}",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")
    path.chmod(0o600)
    return str(path)


def _ensure_quarantine_root(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)


def _quarantine_item(action: dict, *, quarantine_root: Path, quarantine_days: int, now: datetime) -> dict:
    source = Path(str(action.get("path") or "")).expanduser()
    if not source.exists():
        return {"quarantined": False, "error": "source_missing"}
    item_id = _quarantine_item_id(source, str(action.get("type") or "backup"), now)
    item_dir = quarantine_root / item_id
    payload_dir = item_dir / "payload"
    payload_dir.mkdir(parents=True, exist_ok=False)
    destination = payload_dir / source.name
    delete_after = now + timedelta(days=quarantine_days)
    manifest = {
        "item_id": item_id,
        "type": action.get("type") or "backup",
        "group": action.get("group") or "",
        "reason": action.get("reason") or "old_backup_rotation",
        "original_path": str(source),
        "resolved_path": str(_resolve_existing(source)),
        "payload_path": str(destination),
        "size_bytes": int(action.get("size_bytes") or 0),
        "modified_at": action.get("modified_at") or "",
        "quarantined_at": now.isoformat(timespec="seconds"),
        "delete_after": delete_after.isoformat(timespec="seconds"),
        "policy": {
            "keep_backups": int(action.get("keep_backups") or DEFAULT_KEEP_BACKUPS),
            "min_age_days": int(action.get("min_age_days") or DEFAULT_MIN_AGE_DAYS),
            "quarantine_days": quarantine_days,
        },
    }
    (item_dir / "manifest.pending.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.move(str(source), str(destination))
    (item_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (item_dir / "manifest.pending.json").unlink(missing_ok=True)
    return {
        "quarantined": True,
        "quarantine_item_id": item_id,
        "quarantine_path": str(item_dir),
        "payload_path": str(destination),
        "delete_after": manifest["delete_after"],
    }


def _quarantine_item_id(source: Path, item_type: str, now: datetime) -> str:
    safe_name = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in source.name)[:64].strip("-")
    if not safe_name:
        safe_name = "item"
    timestamp = now.strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{item_type}-{safe_name}-{uuid.uuid4().hex[:8]}"


def _quarantine_purge_actions(quarantine_root: Path, now: datetime) -> list[dict]:
    if not quarantine_root.exists() or not quarantine_root.is_dir():
        return []
    actions: list[dict] = []
    for item_dir in sorted(quarantine_root.iterdir()):
        if not item_dir.is_dir():
            continue
        manifest_path = item_dir / "manifest.json"
        manifest = _read_manifest(manifest_path)
        delete_after_text = str(manifest.get("delete_after") or "")
        purge = False
        reason = "missing_delete_after"
        if delete_after_text:
            try:
                purge = _coerce_datetime(delete_after_text) <= now
                reason = "quarantine_expired" if purge else "quarantine_not_expired"
            except ValueError:
                reason = "invalid_delete_after"
        actions.append(
            {
                "action": "purge" if purge else "keep",
                "reason": reason,
                "path": str(item_dir),
                "manifest_path": str(manifest_path),
                "item_id": manifest.get("item_id") or item_dir.name,
                "original_path": manifest.get("original_path") or "",
                "delete_after": delete_after_text,
                "size_bytes": int(manifest.get("size_bytes") or _dir_size(item_dir)),
            }
        )
    actions.sort(key=lambda item: (item["action"] != "purge", item["path"]))
    return actions


def _quarantine_purge_result(actions: list[dict]) -> dict:
    purge = [item for item in actions if item.get("action") == "purge"]
    kept = [item for item in actions if item.get("action") == "keep"]
    return {
        "mode": "dry_run",
        "candidate_count": len(purge),
        "purged_count": 0,
        "kept_count": len(kept),
        "total_size_bytes": sum(int(item.get("size_bytes") or 0) for item in purge),
        "actions": actions,
    }


def _apply_quarantine_purge(actions: list[dict], quarantine_root: Path) -> dict:
    purged = 0
    applied: list[dict] = []
    root = quarantine_root.expanduser()
    try:
        root_resolved = root.resolve()
    except OSError:
        root_resolved = root
    for action in actions:
        if action.get("action") != "purge":
            applied.append({**action, "purged": False})
            continue
        path = Path(str(action.get("path") or "")).expanduser()
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if not _is_relative_to(resolved, root_resolved) or resolved == root_resolved:
            applied.append({**action, "purged": False, "error": "outside_quarantine"})
            continue
        try:
            shutil.rmtree(path)
            purged += 1
            applied.append({**action, "purged": True})
        except OSError as exc:
            applied.append({**action, "purged": False, "error": str(exc)})
    return {
        "mode": "applied",
        "candidate_count": sum(1 for item in actions if item.get("action") == "purge"),
        "purged_count": purged,
        "kept_count": sum(1 for item in actions if item.get("action") == "keep"),
        "total_size_bytes": sum(int(item.get("size_bytes") or 0) for item in actions if item.get("action") == "purge"),
        "actions": applied,
    }


def _read_manifest(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _min_non_empty(values) -> str:
    candidates = sorted(value for value in values if value)
    return candidates[0] if candidates else ""


def _ha_cache_residue_candidates(core: Path, db: Path, *, live_data: Path) -> list[dict]:
    package_index = _ha_package_index(db)
    live_resolved = _resolve_existing(live_data)
    scan_roots = [
        ("core_ha_cache", core / "data" / "ha_packages"),
        ("db_ha_cache", db.parent / "ha_packages"),
        ("application_support_ha_cache", live_data / "ha_packages"),
    ]
    candidates: list[dict] = []
    seen: set[str] = set()
    for label, root in scan_roots:
        if not root.exists() or not root.is_dir():
            continue
        root_resolved = _resolve_existing(root)
        for path in sorted(root.glob("ha_pkg_*")):
            if path.suffix.lower() not in {".json", ".md"} or not path.is_file():
                continue
            resolved = _resolve_existing(path)
            key = str(resolved)
            if key in seen:
                continue
            seen.add(key)
            package_id = path.stem
            record = package_index.get(package_id)
            cache_ref = str((record or {}).get("cache_ref") or "")
            cache_ref_matches_file = _cache_ref_matches_file(cache_ref, resolved)
            record_status = str((record or {}).get("source_status") or "missing_db_record")
            cache_invalidated_at = str((record or {}).get("cache_invalidated_at") or "")
            if record and record_status == "active" and not cache_invalidated_at and cache_ref_matches_file:
                continue
            try:
                stat = path.stat()
                size_bytes = int(stat.st_size)
                modified_at = _format_mtime(stat.st_mtime)
                permissions = oct(stat.st_mode & 0o777)
            except OSError:
                size_bytes = 0
                modified_at = ""
                permissions = ""
            companion = path.with_suffix(".md" if path.suffix.lower() == ".json" else ".json")
            reasons = _ha_cache_residue_reasons(
                record=record,
                record_status=record_status,
                cache_invalidated_at=cache_invalidated_at,
                cache_ref_matches_file=cache_ref_matches_file,
            )
            candidates.append(
                {
                    "location_label": label,
                    "type": "ha_package_cache_residue",
                    "path": str(path),
                    "relative_path": _relative_path(resolved, root_resolved),
                    "resolved_path": str(resolved),
                    "size_bytes": size_bytes,
                    "modified_at": modified_at,
                    "permissions": permissions,
                    "outside_live_runtime": not _is_relative_to(resolved, live_resolved),
                    "risk_level": "medium",
                    "package_id": package_id,
                    "companion_path": str(companion),
                    "companion_exists": companion.exists(),
                    "record_status": record_status,
                    "cache_ref": cache_ref,
                    "cache_ref_matches_file": cache_ref_matches_file,
                    "cache_invalidated_at": cache_invalidated_at,
                    "residue_reasons": reasons,
                    "recommendation": "HA 包缓存已撤销、过期、失联或不再匹配 DB 记录；当前只读列出，确认范围后才可清理。",
                }
            )
    return candidates


def _ha_package_index(db: Path) -> dict[str, dict]:
    if not db.exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute(
            """
            SELECT d.id, d.cache_ref, d.cache_invalidated_at,
                   h.source_status, h.retention_until, h.updated_at
            FROM distribution_packages d
            LEFT JOIN ha_source_records h ON h.package_id = d.id
            WHERE d.target = 'ha'
            """
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    index: dict[str, dict] = {}
    for row in rows:
        index[str(row["id"])] = dict(row)
    return index


def _cache_ref_matches_file(cache_ref: str, path: Path) -> bool:
    if not cache_ref:
        return False
    try:
        cache_path = Path(cache_ref).expanduser().resolve()
    except OSError:
        cache_path = Path(cache_ref).expanduser()
    return path == cache_path or path == cache_path.with_suffix(".md")


def _ha_cache_residue_reasons(
    *,
    record: dict | None,
    record_status: str,
    cache_invalidated_at: str,
    cache_ref_matches_file: bool,
) -> list[str]:
    reasons: list[str] = []
    if record is None:
        reasons.append("missing_db_record")
    if record_status in {"revoked", "expired_deleted", "user_deleted"}:
        reasons.append(f"db_status_{record_status}")
    if cache_invalidated_at:
        reasons.append("cache_invalidated")
    if record is not None and not cache_ref_matches_file:
        reasons.append("cache_ref_mismatch")
    return reasons or ["unknown_residue"]


def _ha_cache_residue_summary(candidates: list[dict]) -> dict:
    by_status: dict[str, int] = {}
    by_reason: dict[str, int] = {}
    total_size = 0
    for item in candidates:
        status = str(item.get("record_status") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
        total_size += int(item.get("size_bytes") or 0)
        for reason in item.get("residue_reasons") or []:
            key = str(reason)
            by_reason[key] = by_reason.get(key, 0) + 1
    return {
        "candidate_count": len(candidates),
        "total_size_bytes": total_size,
        "by_record_status": by_status,
        "by_reason": by_reason,
    }


def _risk_summary(candidates: list[dict]) -> dict:
    buckets = {"high": 0, "medium": 0, "low": 0, "live": 0}
    total_size = 0
    type_counts: dict[str, int] = {}
    for item in candidates:
        risk = str(item.get("risk_level") or "medium")
        buckets[risk] = buckets.get(risk, 0) + 1
        total_size += int(item.get("size_bytes") or 0)
        marker_type = str(item.get("type") or "unknown")
        type_counts[marker_type] = type_counts.get(marker_type, 0) + 1
    return {
        "candidate_count": len(candidates),
        "high_count": buckets.get("high", 0),
        "medium_count": buckets.get("medium", 0),
        "low_count": buckets.get("low", 0),
        "live_count": buckets.get("live", 0),
        "total_size_bytes": total_size,
        "by_type": type_counts,
    }


def _recommended_actions(candidates: list[dict]) -> list[dict]:
    if not candidates:
        return [
            {
                "priority": "none",
                "action": "no_backup_candidates",
                "summary": "未发现 live runtime 外的旧 SQLite、HA 包或 raw_vault 候选。",
            }
        ]
    high_count = sum(1 for item in candidates if item.get("risk_level") == "high")
    medium_count = sum(1 for item in candidates if item.get("risk_level") == "medium")
    low_count = sum(1 for item in candidates if item.get("risk_level") == "low")
    actions: list[dict] = []
    if high_count:
        actions.append(
            {
                "priority": "high",
                "action": "delete_old_activity_data_snapshots",
                "summary": f"发现 {high_count} 个高风险旧活动数据候选；超过 today+yesterday 且 live DB/HA 健康时，hygiene-cleanup 会列为永久删除候选并保留元数据审计。",
            }
        )
    if medium_count:
        actions.append(
            {
                "priority": "medium",
                "action": "review_old_ha_packages",
                "summary": f"发现 {medium_count} 个中风险旧 HA 包或备份候选；含活动数据且超过 today+yesterday 时按严格边界清理，非活动源码/配置备份保留。",
            }
        )
    if low_count:
        actions.append(
            {
                "priority": "low",
                "action": "optionally_remove_empty_placeholders",
                "summary": f"发现 {low_count} 个低风险空占位；可人工确认后清理。",
            }
        )
    actions.append(
        {
            "priority": "safety",
            "action": "automation_policy_note",
            "summary": "hygiene-cleanup 可自动清理失效 HA cache，并对旧活动数据快照执行 today+yesterday 严格边界；非活动备份只列出不自动删除。",
        }
    )
    return actions


def _activity_export_cache_recommended_actions(candidates: list[dict]) -> list[dict]:
    delete_count = sum(1 for item in candidates if item.get("action") == "delete")
    stale_count = sum(1 for item in candidates if item.get("outside_activity_retention_window"))
    if not stale_count:
        return []
    return [
        {
            "priority": "medium" if delete_count else "safety",
            "action": "review_old_activity_export_cache",
            "summary": (
                f"发现 {stale_count} 个超过 today+yesterday 的 cleaned/learning 导出缓存；"
                "daemon 自动清理不删除，只在显式 hygiene-cleanup apply 且 live DB/HA 健康时删除，并写 metadata-only 审计。"
            ),
        }
    ]


def _runtime_permission_recommended_actions(candidates: list[dict]) -> list[dict]:
    if not candidates:
        return []
    return [
        {
            "priority": "medium",
            "action": "fix_runtime_permissions",
            "summary": f"发现 {len(candidates)} 个 core/data 权限不符合私有运行时约束；可由 hygiene-cleanup apply 统一修正。",
        }
    ]


def _risk_level(marker_type: str, outside_live_runtime: bool, size_bytes: int = 0) -> str:
    if not outside_live_runtime:
        return "live"
    if marker_type == "sqlite_database" and int(size_bytes or 0) == 0:
        return "low"
    if marker_type == "sqlite_database":
        return "high"
    if marker_type in {"raw_vault_dir", "ha_packages_dir"}:
        return "high" if marker_type == "raw_vault_dir" else "medium"
    return "medium"


def _marker_recommendation(marker_type: str, outside_live_runtime: bool, risk_level: str) -> str:
    if not outside_live_runtime:
        return "当前 live runtime 内的数据；不要按旧备份清理。"
    if risk_level == "low":
        return "空的旧数据占位文件；可人工确认后清理。"
    if marker_type == "sqlite_database":
        return "旧 SQLite 快照可能绕过今昨保留和删除级联；超过 today+yesterday 且 live 健康时进入永久删除候选。"
    if marker_type == "raw_vault_dir":
        return "旧 raw vault 可能含临时原始材料；超过 today+yesterday 且 live 健康时进入永久删除候选。"
    if marker_type == "ha_packages_dir":
        return "旧 HA 包缓存可能含清洗后活动摘要；超过 today+yesterday 且 live 健康时进入永久删除候选。"
    return f"{risk_level} 风险候选；人工确认后处理。"


def _dir_size(path: Path) -> int:
    total = 0
    for current, _, files in os.walk(path, followlinks=False):
        for filename in files:
            try:
                total += (Path(current) / filename).stat().st_size
            except OSError:
                continue
    return int(total)


def _format_mtime(timestamp: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def _resolve_existing(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path.expanduser()


def _relative_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_live_runtime_backup(path: Path, live_data: Path) -> bool:
    try:
        relative = path.relative_to(live_data)
    except ValueError:
        return False
    return bool(relative.parts and relative.parts[0] == "backups")
