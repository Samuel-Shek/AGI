"""Task closure sidecar. No activity, raw screen or memory writes.

Native receipts prove reception only. Only a separate acceptance record can
close an issue. An uncertain side effect is never automatically repeated.
"""
from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import functools
import hashlib
import json
import os
import re
import stat
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
import sys

from activity_mvp.source_governance import personal_sources as personal_scope
from .feedback.store import atomic, packed


def metadata_paths(value):
    """Only explicit path fields from registered evidence/authority, never prose."""
    if isinstance(value, str):
        value = json.loads(value)
    if isinstance(value, list):
        return [path for row in value for path in metadata_paths(row)]
    if not isinstance(value, dict): return []
    paths = [value[key] for key in ("path", "target_root", "scope_root") if value.get(key)]
    for key in ("files", "trees"):
        paths.extend(value.get(key) or [])
    scope = value.get("scope")
    if isinstance(scope, str) and Path(scope).is_absolute(): paths.append(scope)
    elif isinstance(scope, dict): paths.extend(metadata_paths(scope))
    for key in ("evidence", "acceptance", "authority", "conditions", "independent_artifact", "wording_artifact", "final_response"):
        if value.get(key): paths.extend(metadata_paths(value[key]))
    return paths


def owner_retired(item, paths=(), *, _classify=None):
    """Check every registered path that would be read before opening evidence."""
    try:
        from .retirement_snapshot import CURRENT
        view = CURRENT.get()
        if view is not None and _classify is None:
            return view.retired(item.get("target_root") or item.get("scope_root"),
                                (*metadata_paths(item), *paths))
        if not personal_scope.company_stopped(): return False
        root = item.get("target_root") or item.get("scope_root")
        if not isinstance(root, str) or not Path(root).is_absolute(): return True
        classify = _classify or personal_scope.classify
        return any(classify({"root": path}) == "company"
                   for path in dict.fromkeys([root, *metadata_paths(item), *paths]))
    except (OSError, ValueError, TypeError, KeyError): return True


def automatic_retired(item): return owner_retired(item)


MANUAL_CLOSURE_PERMITS = Path.home() / "AI/source-governance/closure-manual-restorations.json"
MANUAL_RETIREMENT_REGISTRY = Path.home() / "AI/source-governance/retirements.json"
MANUAL_RETIREMENT_BRIDGE = Path.home() / "AI/LocalData/WORKPLACE-Memory-Bridge.nosync/config.json"


def _manual_json_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value: raise ValueError("manual_restoration_duplicate_key")
        value[key] = item
    return value


def _manual_owned_json(path, *, private_file=False):
    path = Path(path)
    if not path.is_absolute() or any(x in {".", ".."} for x in str(path).split("/")):
        raise ValueError("manual_restoration_path_invalid")
    descriptors = []
    try:
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW); descriptors.append(fd)
        for part in path.parts[1:-1]:
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd); descriptors.append(fd)
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd); descriptors.append(fd)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & (0o077 if private_file else 0o022)):
            raise ValueError("manual_restoration_not_owned")
        raw = b""
        while len(raw) <= 2 * 1024 * 1024:
            block = os.read(fd, min(65536, 2 * 1024 * 1024 + 1 - len(raw)))
            if not block: break
            raw += block
        if len(raw) > 2 * 1024 * 1024: raise ValueError("manual_restoration_oversized")
        after = os.fstat(fd)
        identity = lambda row: (row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns, row.st_ctime_ns, row.st_mode, row.st_nlink, row.st_uid)
        if identity(info) != identity(after): raise ValueError("manual_restoration_changed_during_read")
        value = json.loads(raw, object_pairs_hook=_manual_json_pairs)
        if not isinstance(value, dict): raise ValueError("manual_restoration_invalid")
        return value, raw, identity(after)
    finally:
        for fd in reversed(descriptors): os.close(fd)


def _manual_path_identity(path):
    """Walk explicit evidence metadata without following aliases or hard links."""
    member = Path(path)
    if not member.is_absolute() or str(member) != path or any(x in {".", ".."} for x in path.split("/")):
        raise ValueError("manual_evidence_path_invalid")
    descriptors = []
    try:
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW); descriptors.append(fd)
        for index, part in enumerate(member.parts[1:]):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if index < len(member.parts[1:]) - 1: flags |= os.O_DIRECTORY
            try: fd = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                # An exact declared output may not exist yet. Every extant ancestor was checked.
                return None
            descriptors.append(fd)
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) and not (stat.S_ISREG(info.st_mode) and info.st_nlink == 1):
            raise ValueError("manual_evidence_alias_or_special_file")
        return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink)
    finally:
        for fd in reversed(descriptors): os.close(fd)


def _project_manual_closure_allowed(task, item=None, paths=()):
    """Owner-issued task or revocable project permit; never used by automation.

    This permits manual closure evidence, not source ingestion, dispatch,
    notifications, recovery workers, new tasks, or a changed acceptance rule.
    """
    try:
        task = dict(task)
        item = dict(item) if item is not None else task
        task_id = task.get("id")
        if not task_id or item.get("task_id", task_id) != task_id: return False
        inputs = (MANUAL_CLOSURE_PERMITS, MANUAL_RETIREMENT_REGISTRY, MANUAL_RETIREMENT_BRIDGE)
        initial = [_manual_owned_json(path, private_file=i == 0) for i, path in enumerate(inputs)]
        permit, registry, bridge = [row[0] for row in initial]
        project_grants = permit.get("project_grants", [])
        retired, bridge_retired = registry.get("workplace"), bridge.get("retirement")
        if (permit.get("version") != 1 or type(permit.get("version")) is not int
                or permit.get("owner_id") != "local-owner"
                or not isinstance(permit.get("grants"), list) or len(permit["grants"]) > 40
                or not isinstance(project_grants, list) or len(project_grants) > 40
                or not isinstance(retired, dict) or not isinstance(bridge_retired, dict)
                or retired.get("active") is not True or retired.get("status") != "owner_retired"
                or bridge.get("retirement_active") is not True
                or bridge_retired.get("status") != "owner_retired"
                or bridge_retired.get("policy_version") != personal_scope.VERSION
                or not isinstance(retired.get("at"), str) or not retired["at"].strip()
                or retired.get("at") != bridge_retired.get("at")):
            return False
        now = time.time()
        grants = [g for g in permit["grants"] if isinstance(g, dict)
                  and g.get("task_id") == task_id and g.get("platform") == task.get("platform")
                  and g.get("session") == task.get("session")]
        projects = [g for g in project_grants if isinstance(g, dict)
                    and g.get("root") == task.get("scope_root")
                    and g.get("platform") == task.get("platform")]
        if len(grants) > 1 or len(projects) > 1: return False
        # The newer project decision supersedes its legacy task permits,
        # including on revocation; an old live grant cannot bypass that choice.
        project_bound = len(projects) == 1
        if project_bound: grants = projects
        if len(grants) != 1: return False
        for grant in grants:
            if not isinstance(grant, dict): continue
            if (grant.get("enabled") is not True
                    or grant.get("purpose") != ("manual_project_closure" if project_bound else "manual_closure")
                    or grant.get("automatic_execution") is not False
                    or grant.get("platform") != task.get("platform")
                    or grant.get("retirement_at") != retired.get("at")):
                continue
            if not project_bound and (grant.get("task_id") != task_id
                                      or grant.get("session") != task.get("session")):
                continue
            issued, expires = grant.get("issued_at"), grant.get("expires_at")
            if project_bound:
                if (task.get("platform") != "codex"
                        or grant.get("valid_until_revoked") is not True
                        or any(k in grant for k in ("task_id", "session", "expires_at"))
                        or type(issued) not in (int, float) or not 0 < issued <= now
                        or any(not isinstance(grant.get(k), str) or not grant[k].strip()
                               for k in ("authorization_source", "authorization_session", "authorization_turn"))):
                    continue
            elif (type(issued) not in (int, float) or type(expires) not in (int, float)
                    or not issued <= now < expires or not 0 < expires - issued <= 48 * 3600):
                continue
            root = grant.get("root")
            if not isinstance(root, str) or not Path(root).is_absolute(): continue
            target = Path(root)
            if (str(target.resolve()) != root or not target.is_dir()
                    or any(member.is_symlink() for member in (target, *target.parents))
                    or task.get("scope_root") != root
                    or (item.get("target_root") or item.get("scope_root")) != root):
                continue
            explicit = [*metadata_paths(task), *metadata_paths(item), *paths]
            if len(explicit) > 2000: return False
            identities = []
            for path in explicit:
                if not isinstance(path, str) or not Path(path).is_absolute(): return False
                member = Path(path)
                if not member.is_relative_to(target):
                    return False
                identities.append(_manual_path_identity(path))
            if any(_manual_owned_json(path, private_file=i == 0)[1:] != initial[i][1:]
                   for i, path in enumerate(inputs)):
                return False
            if any(_manual_path_identity(path) != identity for path, identity in zip(explicit, identities)):
                return False
            checked_at = time.time()
            return issued <= checked_at if project_bound else issued <= checked_at < expires
        return False
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError):
        return False


def manual_closure_allowed(task, item=None, paths=()):
    """Explicit project permits or a current owner-authorized foreground task.

    Source classification is not permission to restart retired automation.
    Background dispatch, recovery and notification paths retain owner_retired.
    """
    if _project_manual_closure_allowed(task, item, paths):
        return True
    try:
        from activity_mvp.source_governance.manual_access import closure_override
        return closure_override(task, item, paths, core=sys.modules[__name__]) is True
    except (ImportError, OSError, ValueError, TypeError, KeyError, RuntimeError):
        return False


def manual_owner_retired(task, item=None, paths=()):
    checked = dict(item) if item is not None else dict(task)
    return owner_retired(checked, paths) and not manual_closure_allowed(task, checked, paths)


def manual_effects_pending(task, item):
    if not manual_closure_allowed(task, item): return effects_pending(item)
    if item["state"] not in {"canceled", "dismissed"} or item["dispatch_state"] in {"new", "preparing", "created"}:
        return False
    acceptance = json.loads(item.get("acceptance") or "{}")
    return not (acceptance.get("cancellation_checked") is True and acceptance.get("evidence")
                and current(acceptance["evidence"]))


def personal_limited(rows, limit=4):
    """Filter registered metadata before the existing per-cycle action budget."""
    admitted = []
    for row in rows:
        if not automatic_retired(dict(row)):
            admitted.append(row)
            if len(admitted) == limit: break
    return admitted

LOADED_MODULE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

STATES = {"pending", "running", "verification", "blocked", "decision", "resolved", "canceled", "dismissed"}
TERMINAL = {"resolved", "canceled", "dismissed"}
LABELS = {"pending": "待处理", "running": "执行中", "verification": "待验收", "blocked": "受阻",
          "decision": "待你决定", "resolved": "已解决", "canceled": "已取消", "dismissed": "已关闭"}
RUNTIME_MAX_AGE = 30
RECOVERY_LABELS = {"unregistered": "未安排恢复检查", "waiting": "已安排恢复检查，条件尚未满足",
                   "ready": "上次检查条件已满足，未安排自动执行", "stale": "恢复依据已变化，需要负责人重新核对",
                   "unknown": "恢复条件尚无法核对", "inactive": "恢复检查已停止"}
SENSITIVE = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\b(?:sk-[A-Za-z0-9_-]{16,}|Bearer\s+\S+)|(?:password|api_key|access_token)\s*[=:]", re.I)

# A quality-check applicability declaration, never an authorization boundary.
# Exact artifact files are allowed; executable/configuration targets win over
# the declaration even when their extension happens to be Markdown.
CLOSURE_APPLICABILITY_VERSION = 1
PROTECTED_ARTIFACT_NAMES = {
    "agents.md", "claude.md", "skill.md", "ai_commands.md", "指令要求.md",
    "hooks.json", "config.toml", "config.yaml", "config.yml", "config.json",
    "package.json", "pyproject.toml", "requirements.txt", "dockerfile",
    "ai_memory.md", "settings.json", ".zshrc", ".zprofile", ".zshenv", ".bashrc", ".bash_profile", ".profile", ".env",
}
PROTECTED_ARTIFACT_PARTS = {
    ".git", ".codex", ".claude", ".hermes", ".workbuddy", ".openclaw",
    ".config", "launchagents", "launchdaemons", "ai 规则模块",
}
EXECUTABLE_ARTIFACT_SUFFIXES = {".py", ".pyc", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".swift", ".sh", ".zsh", ".bash", ".command", ".plist", ".so", ".dylib", ".c", ".h", ".cc", ".cpp", ".hpp", ".rs", ".go", ".rb", ".php", ".java", ".kt", ".ps1", ".bat", ".cmd", ".scpt", ".applescript"}


def artifact_file(path, root):
    """Validate one explicit ordinary artifact, without reading its contents."""
    if not isinstance(path, str) or len(path) > 4096 or not Path(path).is_absolute():
        raise ValueError("artifact_path_invalid")
    target = Path(path)
    if str(target) != path or any(part in {".", ".."} for part in path.split("/")):
        raise ValueError("artifact_path_not_canonical")
    root = Path(root)
    try: relative = target.relative_to(root)
    except ValueError: raise ValueError("artifact_outside_scope")
    if not relative.parts: raise ValueError("artifact_directory_rejected")
    for member in (target, *target.parents):
        if member.is_symlink(): raise ValueError("artifact_alias_rejected")
    if target.exists():
        info = target.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("artifact_not_single_regular_file")
        if info.st_mode & 0o111: raise ValueError("artifact_executable_target")
    parts = {part.casefold() for part in target.parts}
    if target.name.casefold() in PROTECTED_ARTIFACT_NAMES or parts & PROTECTED_ARTIFACT_PARTS:
        raise ValueError("artifact_operational_target")
    protected = (Path.home() / "AI/shared-skills", Path.home() / "AI/source-governance", Path.home() / "bin", Path.home() / ".local/bin",
                 Path.home() / "Library/Application Support/Personal AI Monitor/core",
                 Path.home() / "Code/active/artificial-general-intelligence/core",
                 Path.home() / "Code/active/artificial-general-intelligence/apps")
    if any(target == base or base in target.parents for base in protected):
        raise ValueError("artifact_operational_target")
    if target.suffix.casefold() in EXECUTABLE_ARTIFACT_SUFFIXES | {".toml", ".yaml", ".yml", ".ini", ".cfg", ".env"}:
        raise ValueError("artifact_executable_target")
    return str(target)


def artifact_declaration(paths, root):
    if not isinstance(paths, list) or len(paths) > 64:
        raise ValueError("artifact_paths_invalid")
    validated = [artifact_file(path, root) for path in paths]
    if len(set(validated)) != len(validated): raise ValueError("artifact_paths_duplicate")
    return validated


@functools.lru_cache(maxsize=1)
def boot_identity():
    try:
        if sys.platform == "darwin":
            value = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"], capture_output=True, text=True, timeout=2, check=True).stdout.strip()
        else: value = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return str(uuid.UUID(value))
    except (OSError, ValueError, subprocess.SubprocessError): return None


def elapsed_time():
    # Darwin raw monotonic and Linux boottime include system suspension.
    clock = getattr(time, "CLOCK_MONOTONIC_RAW" if sys.platform == "darwin" else "CLOCK_BOOTTIME", None)
    if clock is None: return float("nan")
    return time.clock_gettime(clock)


def observation_fresh(live):
    try:
        wall = time.time() - float(live["observed_at"])
        elapsed = elapsed_time() - float(live["observed_monotonic"])
        boot = boot_identity()
        # Both clocks constrain the original observation. Sleep, rollback,
        # reboot or missing provenance cannot extend its 30-second lease.
        return bool(boot and live.get("boot_identity") == boot and live.get("clock_kind") == "continuous_boot_v1" and 0 <= wall <= RUNTIME_MAX_AGE
                    and 0 <= elapsed <= RUNTIME_MAX_AGE and abs(wall-elapsed) <= 2)
    except (ValueError, TypeError, KeyError): return False


def recovery_file(condition, root):
    """Open every path component without following links; hash this same fd."""
    descriptors = []
    try:
        root = Path(root)
        path = Path(condition["path"])
        relative = path.relative_to(root)
        if not relative.parts or any(part in {".", ".."} for part in relative.parts): return "stale"
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open("/", flags); descriptors.append(descriptor)
        for part in [*root.parts[1:], *relative.parts[:-1]]:
            descriptor = os.open(part, flags, dir_fd=descriptor); descriptors.append(descriptor)
        file = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor); descriptors.append(file)
        before = os.fstat(file)
        limit = 2 * 1024 * 1024
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit: return "stale"
        chunks, length = [], 0
        while length <= limit:
            data = os.read(file, min(65536, limit + 1 - length))
            if not data: break
            chunks.append(data); length += len(data)
        after = os.fstat(file)
        if length > limit or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns) or length != after.st_size: return "unknown"
        data = b"".join(chunks)
        if condition.get("sha256") and hashlib.sha256(data).hexdigest() != condition["sha256"]: return "stale"
        return "ready"
    except FileNotFoundError: return "waiting"
    except ValueError: return "stale"
    except OSError as error: return "stale" if error.errno in {errno.ELOOP, errno.ENOTDIR} else "unknown"
    finally:
        for descriptor in reversed(descriptors): os.close(descriptor)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def root_for(database):
    return Path(database).expanduser().resolve().parent / "attention-center"


def private(path, directory=False):
    if path.is_symlink():
        raise ValueError("closure_path_alias")
    if path.exists():
        info = path.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("closure_path_not_private")
    elif directory:
        path.mkdir(parents=True, mode=0o700)


def bounded_text(value, field, limit=4000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or SENSITIVE.search(value):
        raise ValueError("invalid_" + field)
    return value.strip()


def evidence(rows, root):
    """Only explicit task files under the confirmed root; read at most 2 MiB each."""
    if not isinstance(rows, list) or not rows or len(rows) > 40:
        raise ValueError("evidence_required")
    values = []
    for row in rows:
        path = Path(row["path"]).expanduser()
        if not path.is_absolute() or path.is_symlink():
            raise ValueError("evidence_path_invalid")
        path = path.resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("evidence_outside_scope_or_oversize")
        value = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        if row.get("sha256") and row["sha256"] != value["sha256"]:
            raise ValueError("evidence_version_changed")
        values.append(value)
    return values


def current(rows):
    try:
        from .retirement_snapshot import CURRENT
        view = CURRENT.get()
        if view is not None:
            return view.evidence_current(rows)
        return all(Path(r["path"]).is_file() and not Path(r["path"]).is_symlink()
                   and Path(r["path"]).stat().st_size <= 2 * 1024 * 1024
                   and hashlib.sha256(Path(r["path"]).read_bytes()).hexdigest() == r["sha256"] for r in rows)
    except OSError:
        return False


def final_wording(text, facts, remaining):
    """Known narrow claim contradictions; open-ended semantics need a reviewer.

    Text is transient. Return only stable reason codes, never quoted content.
    Out-of-scope follow-ups describe boundaries, not new executable issues.
    """
    # Quotations and code examples are not the assistant's completion claims.
    text = re.sub(r"(?ms)^\s*```.*?^\s*```[^\n]*", "", text)
    text = re.sub(r"(?m)^\s*>[^\n]*", "", text)
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"(?m)^\s*(?:[-*]|\d+[.)])\s+", "", text)
    reasons = []
    complete = facts["completion"] == "complete"
    if complete and (remaining or facts["remaining"]):
        reasons.append("completion_exceeds_scope")
    if not complete and re.search(r"(?:^|[\n。；])\s*(?:Local Owner[，,:：]\s*)?(?:已)?(?:全部完成|整体完成|所有工作已完成)[。！!，,\s]*", text):
        reasons.append("unqualified_completion_claim")
    if facts["remaining"] and re.search(r"(?:^|[\n。；])\s*(?:你|您|用户)?(?:现在)?(?:无需|不需要)(?:用户)?(?:操作|动手)[。！!，,\s]*(?:$|[\n。])", text):
        reasons.append("unqualified_no_action_claim")
    if facts["completion"] == "stage_complete" and facts["scope"] not in text:
        reasons.append("completed_scope_not_stated")
    if facts["user_action"]["required"] and facts["user_action"]["reason"] not in text:
        reasons.append("required_action_not_stated")
    return reasons


def read_delivery_file(path, root, limit=128 * 1024):
    """No-follow path walk, bounded read and single-file identity verification."""
    path, root = Path(path), Path(root)
    relative = path.relative_to(root)
    if not path.is_absolute() or not relative.parts or any(x in {".", ".."} for x in relative.parts):
        raise ValueError("final_response_path_invalid")
    descriptors = []
    try:
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW); descriptors.append(fd)
        for part in [*root.parts[1:], *relative.parts[:-1]]:
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd); descriptors.append(fd)
        fd = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd); descriptors.append(fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise ValueError("final_response_file_invalid")
        data = b""
        while len(data) <= limit:
            block = os.read(fd, min(65536, limit + 1 - len(data)))
            if not block: break
            data += block
        after = os.fstat(fd)
        if len(data) > limit or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("final_response_changed_during_read")
        return data
    finally:
        for descriptor in reversed(descriptors): os.close(descriptor)


def delivery_snapshot(rows):
    return digest([[r["id"], r["revision"], r["state"], r["acceptance"]] for r in sorted(rows, key=lambda r: r["id"])])


def effects_pending(item):
    """Canceling the obligation never erases already-entered side effects."""
    if item["state"] not in {"canceled", "dismissed"}: return False
    if item["dispatch_state"] in {"new", "preparing", "created"}: return False
    acceptance = json.loads(item.get("acceptance") or "{}")
    if owner_retired(item): return acceptance.get("cancellation_checked") is not True
    return not (acceptance.get("cancellation_checked") is True and acceptance.get("evidence")
                and current(acceptance["evidence"]))


def authority_scope(authority, root):
    """Validate explicit write targets without interpreting prose or widening it.

    The legacy root string still grants that root. A structured scope grants
    exact files and/or named subtrees; evidence may separately name read inputs.
    Missing output files are valid, but aliases and non-canonical paths are not.
    """
    root = Path(root)
    scope = authority.get("scope")
    if isinstance(scope, str):
        if scope != str(root): raise ValueError("authority_scope_format")
        scope = {"files": [], "trees": [scope]}
    if not isinstance(scope, dict) or set(scope) - {"files", "trees"}:
        raise ValueError("authority_scope_format")
    files, trees = scope.get("files", []), scope.get("trees", [])
    if (not isinstance(files, list) or not isinstance(trees, list)
            or not 1 <= len(files) + len(trees) <= 40):
        raise ValueError("authority_scope_format")
    for kind, paths in (("files", files), ("trees", trees)):
        for value in paths:
            if not isinstance(value, str) or not value or len(value) > 4000 or SENSITIVE.search(value):
                raise ValueError("authority_scope_format")
            path = Path(value)
            if (not path.is_absolute() or str(path) != value or ".." in path.parts
                    or not path.is_relative_to(root) or kind == "files" and path == root):
                raise ValueError("authority_scope_outside_target")
            if any(part.is_symlink() for part in [path, *path.parents]):
                raise ValueError("authority_scope_alias")
            if path.exists() and (not path.is_file() if kind == "files" else not path.is_dir()):
                raise ValueError("authority_scope_type")
            if kind == "files" and path.exists() and path.stat().st_nlink != 1:
                raise ValueError("authority_scope_hardlink")
    return {"files": files, "trees": trees}


def authority_scope_allows(authority, root, path):
    try:
        scope = authority_scope(authority, root)
        path = Path(path)
        if (not path.is_absolute() or ".." in path.parts
                or any(part.is_symlink() for part in [path, *path.parents])): return False
        return str(path) in scope["files"] or any(path.is_relative_to(Path(tree)) for tree in scope["trees"])
    except (ValueError, OSError): return False


def authority_readiness(authority, root):
    # A genuine choice or risk retains its human gate. Bad technical metadata
    # must never fabricate a personal choice, and is never permission to act.
    if (authority.get("reversible") is False
            or any(authority.get(key) is True for key in ("cost", "new_permission", "external", "personal_choice"))):
        return "decision", "本次真实选择或风险边界尚待核对；保留人工等待。"
    if not (authority.get("reversible") is True
            and all(authority.get(key) is False for key in ("cost", "new_permission", "external", "personal_choice"))):
        return "blocked", "授权登记字段不完整，由原负责人核对来源；不代用户作选择。"
    try: authority_scope(authority, root)
    except (ValueError, OSError):
        return "blocked", "授权范围格式尚未核清；由原负责人按原授权登记明确文件或子目录，保留原任务与范围，不需用户重新授权。"
    return "pending", None


def safe_authority(item):
    return authority_readiness(json.loads(item["authority"]), item["target_root"])[0] == "pending"


class Store:
    def __init__(self, database, readonly=False):
        self.database = Path(database).resolve()
        self.root = root_for(database)
        self.path = self.root / "closure.sqlite"
        private(self.root, directory=not readonly)
        private(self.path)
        self.conn = sqlite3.connect(f"file:{self.path}?mode=ro" if readonly else self.path,
                                    uri=readonly, timeout=3, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.readonly = readonly
        if readonly:
            self.conn.execute("PRAGMA query_only=ON")
        else:
            os.chmod(self.path, 0o600)
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript("""
              CREATE TABLE IF NOT EXISTS closure_tasks(
                id TEXT PRIMARY KEY, platform TEXT NOT NULL, session TEXT NOT NULL, turn TEXT NOT NULL,
                objective TEXT NOT NULL, scope_root TEXT NOT NULL, complex INTEGER NOT NULL,
                requirements_hash TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
                UNIQUE(platform,session,turn));
              CREATE TABLE IF NOT EXISTS closure_issues(
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES closure_tasks(id),
                fingerprint TEXT NOT NULL UNIQUE, category TEXT NOT NULL, summary TEXT NOT NULL,
                target_root TEXT NOT NULL, project_id TEXT, evidence TEXT NOT NULL, authority TEXT NOT NULL,
                owner TEXT NOT NULL, recommendation TEXT NOT NULL, acceptance_rule TEXT NOT NULL,
                state TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, thread_id TEXT,
                dispatch_state TEXT NOT NULL DEFAULT 'new', message_id TEXT, prompt TEXT, receipt TEXT,
                acceptance TEXT, error TEXT, recovery TEXT, created REAL NOT NULL, updated REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS closure_events(
                seq INTEGER PRIMARY KEY AUTOINCREMENT, issue_id TEXT, task_id TEXT,
                kind TEXT NOT NULL, data TEXT NOT NULL, at REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS closure_audits(
                task_id TEXT PRIMARY KEY REFERENCES closure_tasks(id), requirements_hash TEXT NOT NULL,
                evidence TEXT NOT NULL, review TEXT NOT NULL, independent INTEGER NOT NULL, at REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS closure_task_modes(
                task_id TEXT PRIMARY KEY REFERENCES closure_tasks(id), mode TEXT NOT NULL,
                artifact_paths TEXT NOT NULL, upgraded_reason TEXT, created REAL NOT NULL, updated REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS closure_native_effects(
                task_id TEXT NOT NULL, path TEXT NOT NULL, operational INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(task_id,path));
              CREATE TABLE IF NOT EXISTS closure_notifications(
                event_seq INTEGER PRIMARY KEY REFERENCES closure_events(seq), requested INTEGER NOT NULL DEFAULT 0,
                error TEXT);
              CREATE TABLE IF NOT EXISTS closure_guards(
                guard_key TEXT PRIMARY KEY, attempts INTEGER NOT NULL DEFAULT 0);
              CREATE TABLE IF NOT EXISTS closure_checks(
                issue_id TEXT PRIMARY KEY REFERENCES closure_issues(id), checked_at REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS closure_choices(
                token TEXT PRIMARY KEY, issue_id TEXT NOT NULL REFERENCES closure_issues(id),
                revision INTEGER NOT NULL, choice TEXT, created REAL NOT NULL, answered REAL);
              CREATE TABLE IF NOT EXISTS closure_recovery_plans(
                issue_id TEXT PRIMARY KEY REFERENCES closure_issues(id), task_id TEXT NOT NULL,
                issue_revision INTEGER NOT NULL, version INTEGER NOT NULL, binding TEXT NOT NULL,
                conditions TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'waiting', checked_at REAL,
                ready_notified INTEGER NOT NULL DEFAULT 0);
              CREATE TABLE IF NOT EXISTS closure_runtime_observations(
                issue_id TEXT PRIMARY KEY REFERENCES closure_issues(id), binding TEXT NOT NULL,
                observation TEXT NOT NULL);
            """)

    def __enter__(self): return self
    def __exit__(self, *_): self.conn.close()

    @contextlib.contextmanager
    def transaction(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    def event(self, kind, data, issue=None, task=None, notify=False):
        cursor = self.conn.execute("INSERT INTO closure_events(issue_id,task_id,kind,data,at) VALUES(?,?,?,?,?)",
                                   (issue, task, kind, json.dumps(data, ensure_ascii=False), time.time()))
        if notify:
            self.conn.execute("INSERT INTO closure_notifications(event_seq) VALUES(?)", (cursor.lastrowid,))

    def get(self, identifier):
        row = self.conn.execute("SELECT * FROM closure_issues WHERE id=?", (identifier,)).fetchone()
        if not row: raise ValueError("issue_missing")
        return dict(row)

    def declared_block(self, item):
        if item["state"] != "blocked": return False
        row = self.conn.execute("""SELECT kind,data FROM closure_events WHERE issue_id=?
            AND kind IN ('state_changed','receipt_checked','dispatch_uncertain','authority_scope_corrected')
            ORDER BY seq DESC LIMIT 1""", (item["id"],)).fetchone()
        # Only the executor may release a declared limitation. Native activity
        # and completion describe the chat, not permission or repair acceptance.
        return bool(row and (row["kind"] == "authority_scope_corrected"
                    or row["kind"] == "state_changed" and json.loads(row["data"]).get("to") == "blocked"))

    def has_table(self, name):
        return bool(self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())

    def native_root(self, task):
        if not task or not self.has_table("closure_hook_roots"): return False
        row = self.conn.execute("SELECT * FROM closure_hook_roots WHERE platform=? AND session=?",
                                (task["platform"], task["session"])).fetchone()
        return bool(row and row["task_id"] == task["id"] and row["turn"] == task["turn"])

    def binding(self, item):
        return digest([item["task_id"], item["revision"], item["owner"], item["target_root"],
                       item["project_id"], item["authority"], item["evidence"]])

    def runtime_view(self, item, *, _retired=None):
        if owner_retired(item) if _retired is None else _retired:
            return {"activity": "inactive", "label": "本人停用", "auto_execution": False}
        unknown = {"activity": "unknown", "label": "运行状态待核查", "auto_execution": False}
        if not self.has_table("closure_runtime_observations"): return unknown
        row = self.conn.execute("SELECT * FROM closure_runtime_observations WHERE issue_id=?", (item["id"],)).fetchone()
        if not row or row["binding"] != self.binding(item): return unknown
        try:
            live = json.loads(row["observation"])
        except (ValueError, TypeError, AttributeError): return unknown
        if (not observation_fresh(live) or live.get("source") != "desktop_owner"
                or live.get("exact_turn") is not True or live.get("thread_id") != codex_thread(item)):
            return unknown
        activity = live.get("activity", "unknown")
        label = "正在执行" if activity == "active" else "当前未执行" if activity == "idle" else "运行状态待核查"
        return {"activity": activity, "label": label, "observed_at": live["observed_at"], "observed_monotonic": live["observed_monotonic"], "clock_kind": live["clock_kind"], "auto_execution": False}

    def observe_runtime(self, item):
        """Read only the exact original native turn; never submit or steer."""
        if owner_retired(item): return
        from .attention_native import DesktopIPC
        task = self.conn.execute("SELECT * FROM closure_tasks WHERE id=?", (item["task_id"],)).fetchone()
        identifier = codex_thread(item)
        if not identifier or not task: return
        receipt = json.loads(item.get("receipt") or "{}")
        turn = receipt.get("turn_id") if item["thread_id"] else task["turn"]
        if not turn: return
        prior = self.conn.execute("SELECT * FROM closure_runtime_observations WHERE issue_id=?", (item["id"],)).fetchone()
        if prior and prior["binding"] == self.binding(item):
            if observation_fresh(json.loads(prior["observation"])): return
        observed_monotonic = elapsed_time()
        boot = boot_identity()
        try:
            with DesktopIPC() as ipc:
                if owner_retired(item): return
                live = ipc.snapshot(identifier, expected_cwd=item["target_root"],
                                    message_id=item["message_id"] if item["thread_id"] else None, turn_id=turn)
            live = dict(live)
            live["exact_turn"] = bool(live.get("turn_id") == turn and live.get("turn_status") and live.get("current_turn_matches") is True)
        except (OSError, ValueError, RuntimeError, TimeoutError):
            live = {"activity": "unknown", "source": "unavailable", "observed_at": time.time(), "exact_turn": False}
        live.pop("usage", None)
        live["thread_id"] = identifier
        live["observed_monotonic"] = observed_monotonic
        live["boot_identity"] = boot
        live["clock_kind"] = "continuous_boot_v1"
        with self.transaction():
            if self.binding(self.get(item["id"])) == self.binding(item):
                self.conn.execute("INSERT OR REPLACE INTO closure_runtime_observations VALUES(?,?,?)",
                                  (item["id"], self.binding(item), json.dumps(live)))

    def recovery_view(self, item, *, _retired=None):
        if automatic_retired(item) if _retired is None else _retired:
            return {"status": "inactive", "label": "本人停用", "auto_execution": False}
        status = "unregistered"
        if self.has_table("closure_recovery_plans"):
            row = self.conn.execute("SELECT * FROM closure_recovery_plans WHERE issue_id=?", (item["id"],)).fetchone()
            if row:
                if owner_retired({**item, "conditions": row["conditions"]}): return {"status": "inactive", "label": "本人停用", "auto_execution": False}
                status = ("inactive" if item["state"] in TERMINAL or item["state"] == "decision" else
                          "stale" if row["binding"] != self.binding(item) or not current(json.loads(item["evidence"])) else
                          self.recovery_conditions(item, row) if row["status"] == "ready" else row["status"])
        return {"status": status, "label": RECOVERY_LABELS[status], "auto_execution": False}

    def recovery_caller(self):
        """Codex native process identity plus its current hook receipt; never payload identity."""
        session = os.environ.get("CODEX_THREAD_ID")
        if not session or not self.has_table("closure_hook_roots"): return None
        row = self.conn.execute("SELECT * FROM closure_hook_roots WHERE platform='codex' AND session=?", (session,)).fetchone()
        return {"platform": "codex", "session": session, "turn": row["turn"], "task_id": row["task_id"]} if row else None

    def register_recovery(self, payload, *, native_context=None):
        with self.transaction():
            item = self.get(payload["issue_id"])
            task = self.conn.execute("SELECT * FROM closure_tasks WHERE id=?", (item["task_id"],)).fetchone()
            expected = {"platform": task["platform"], "session": task["session"], "turn": task["turn"], "task_id": task["id"]}
            if (native_context != expected or payload.get("revision") != item["revision"] or not self.native_root(task)
                    or item["owner"] != task["platform"] + ":" + task["session"]
                    or item["state"] in TERMINAL or item["state"] == "decision" or not safe_authority(item)
                    or not current(json.loads(item["evidence"]))):
                raise ValueError("recovery_binding_invalid")
            conditions = payload["conditions"]
            if not isinstance(conditions, list) or not 1 <= len(conditions) <= 8: raise ValueError("recovery_conditions_invalid")
            normalized = []
            for condition in conditions:
                kind = condition.get("kind")
                if kind == "file_readable" and set(condition) <= {"kind", "path", "sha256"}:
                    path = Path(condition["path"]).expanduser()
                    root = Path(item["target_root"])
                    if (not path.is_absolute() or any(p.is_symlink() for p in [path, *path.parents])
                            or not path.resolve().is_relative_to(root)
                            or not authority_scope_allows(json.loads(item["authority"]), root, path)):
                        raise ValueError("recovery_path_invalid")
                    sha = condition.get("sha256")
                    if sha is not None and not re.fullmatch(r"[0-9a-f]{64}", sha): raise ValueError("recovery_hash_invalid")
                    normalized.append({"kind": kind, "path": str(path), "sha256": sha})
                elif kind == "after_time" and set(condition) == {"kind", "at"}:
                    at = condition["at"]
                    if type(at) not in (int, float) or not 0 < at < 10**11: raise ValueError("recovery_time_invalid")
                    normalized.append({"kind": kind, "at": at})
                elif kind == "owner_available" and set(condition) == {"kind"} and codex_thread(item):
                    normalized.append({"kind": kind})
                else: raise ValueError("recovery_probe_not_allowed")
            old = self.conn.execute("SELECT * FROM closure_recovery_plans WHERE issue_id=?", (item["id"],)).fetchone()
            encoded = json.dumps(normalized, sort_keys=True)
            if old and old["binding"] == self.binding(item) and old["conditions"] == encoded:
                return self.recovery_view(item)
            version = old["version"] + 1 if old else 1
            self.conn.execute("INSERT OR REPLACE INTO closure_recovery_plans VALUES(?,?,?,?,?,?, 'waiting',NULL,0)",
                              (item["id"], task["id"], item["revision"], version, self.binding(item), encoded))
            self.event("recovery_registered", {"version": version}, issue=item["id"], task=task["id"])
        return self.recovery_view(item)

    def recovery_conditions(self, item, row):
        """Recheck readiness without refreshing observations or executing any action."""
        conditions = json.loads(row["conditions"])
        if owner_retired(item, [x["path"] for x in conditions if x.get("path")]): return "inactive"
        for condition in conditions:
            if owner_retired(item, metadata_paths(conditions)): return "inactive"
            try:
                if condition["kind"] == "after_time": met = time.time() >= condition["at"]
                elif condition["kind"] == "owner_available":
                    runtime = self.runtime_view(item)
                    if runtime["activity"] == "unknown": return "unknown"
                    met = runtime["activity"] == "idle"
                else:
                    if not authority_scope_allows(json.loads(item["authority"]), item["target_root"], condition["path"]):
                        return "stale"
                    file_status = recovery_file(condition, item["target_root"])
                    if file_status != "ready": return file_status
                    met = True
                if not met: return "waiting"
            except OSError: return "unknown"
        return "ready"

    def check_recovery(self, identifier):
        item = self.get(identifier)
        row = self.conn.execute("SELECT * FROM closure_recovery_plans WHERE issue_id=?", (identifier,)).fetchone()
        if not row: return
        status = "inactive" if owner_retired({**item, "conditions": row["conditions"]}) else self.recovery_view(item)["status"]
        if status not in {"stale", "inactive"}:
            merged = {**self.get(identifier), "conditions": row["conditions"]}
            if owner_retired(merged): status = "inactive"
            else:
                if any(c["kind"] == "owner_available" for c in json.loads(row["conditions"])):
                    self.observe_runtime(merged)
                status = self.recovery_conditions(merged, row)
        with self.transaction():
            fresh = self.conn.execute("SELECT * FROM closure_recovery_plans WHERE issue_id=?", (identifier,)).fetchone()
            if fresh["version"] != row["version"]: return
            latest = self.get(identifier)
            if latest["state"] in TERMINAL or latest["state"] == "decision": status = "inactive"
            elif fresh["binding"] != self.binding(latest): status = "stale"
            self.conn.execute("UPDATE closure_recovery_plans SET status=?,checked_at=? WHERE issue_id=?", (status, time.time(), identifier))
            if status == "ready" and not fresh["ready_notified"]:
                self.conn.execute("UPDATE closure_recovery_plans SET ready_notified=1 WHERE issue_id=?", (identifier,))
                self.event("recovery_ready", {"version": row["version"]}, issue=identifier, task=row["task_id"], notify=True)

    def end_assessment(self, task, status, *, allow_manual=True):
        if status.get("closure_required") is False:
            return {"decision": "not_applicable", "reasons": []}
        if status["all_complete"]: return {"decision": "complete", "reasons": []}
        reasons, runnable, unknown = [], False, False
        if not status["audit_valid"] or task["complex"] and not status["independent"]:
            unknown = True; reasons.append("有效独立收尾审查尚未齐全")
        native = self.native_root(task)
        for item in status["remaining"]:
            reason = None
            if (manual_owner_retired(task, item) if allow_manual else owner_retired(item)): reason = "本人停用，保留原记录和人工等待，不恢复自动执行"
            elif not item["owner"]: unknown = True; reason = "未确认负责人或实际去向"
            elif (manual_effects_pending(task, item) if allow_manual else effects_pending(item)): reason = "取消后的已发生动作尚待核清"
            elif item["state"] == "decision": reason = "等待本次真实人工选择"
            elif self.declared_block(item): reason = item["recovery"] or "原执行者声明的阻碍尚未解除"
            elif not safe_authority(item): reason = authority_readiness(json.loads(item["authority"]), item["target_root"])[1]
            elif item["dispatch_state"] in {"uncertain", "send_uncertain", "creation_uncertain"}: reason = "原发送回执不明，不能重发"
            elif not current(json.loads(item["evidence"])): unknown = True; reason = "来源版本已变化，需要核查受影响部分"
            elif item["category"] == "independent" and item["dispatch_state"] == "new": runnable = True
            elif (native and item["owner"] == task["platform"] + ":" + task["session"]
                  and item["category"] in {"main", "related"} and item["state"] in {"pending", "running", "verification"}):
                runnable = True
            elif self.runtime_view(item)["activity"] == "active": reason = "原负责人仍在执行，等待实际验收"
            else: unknown = True; reason = "原执行状态或恢复条件尚无法确认"
            if reason: reasons.append({"issue_id": item["id"], "reason": reason})
        return {"decision": "continue_now" if runnable else "unknown" if unknown else "waiting", "reasons": reasons}

    def begin(self, payload):
        platform = payload["platform"]
        if platform not in {"codex", "claude", "hermes", "workbuddy", "openclaw"}:
            raise ValueError("platform_invalid")
        session = bounded_text(payload["session"], "session", 200)
        turn = bounded_text(payload["turn"], "turn", 200)
        objective = bounded_text(payload["objective"], "objective")
        root = Path(payload["scope_root"]).expanduser().resolve()
        if not root.is_dir(): raise ValueError("scope_unavailable")
        identifier = digest([platform, session, turn])[:32]
        requirement = digest([objective, str(root), payload.get("constraints", [])])
        mode = payload.get("closure_mode")
        if mode is not None and mode not in {"light", "full"}: raise ValueError("closure_mode_invalid")
        if "artifact_paths" in payload and mode is None: raise ValueError("closure_mode_required")
        paths = artifact_declaration(payload.get("artifact_paths", []), root) if mode == "light" else []
        with self.transaction():
            old = self.conn.execute("SELECT * FROM closure_tasks WHERE id=?", (identifier,)).fetchone()
            if old and old["requirements_hash"] != requirement:
                raise ValueError("task_identity_reused_with_new_requirements")
            self.conn.execute("INSERT OR IGNORE INTO closure_tasks VALUES(?,?,?,?,?,?,?,?,?,?)",
                              (identifier, platform, session, turn, objective, str(root),
                               int(payload.get("complex") is True), requirement, time.time(), time.time()))
            previous = self.conn.execute("SELECT * FROM closure_task_modes WHERE task_id=?", (identifier,)).fetchone()
            # An existing legacy registration cannot be relabeled as content.
            if old and not previous and mode == "light": raise ValueError("legacy_task_mode_immutable")
            if previous and previous["mode"] == "light" and mode == "light" and json.loads(previous["artifact_paths"]) != paths:
                raise ValueError("artifact_declaration_immutable")
            if mode and not previous:
                self.conn.execute("INSERT INTO closure_task_modes VALUES(?,?,?,?,?,?)",
                                  (identifier, mode, json.dumps(paths), None, time.time(), time.time()))
            elif mode == "full": self._upgrade_closure(identifier, "explicit_full")
            self.closure_applicability(identifier)
        return {"task_id": identifier, "requirements_hash": requirement}

    def _upgrade_closure(self, identifier, reason):
        """Called under the caller's transaction; never resets identity/guards."""
        if self.readonly or not self.has_table("closure_task_modes"): return
        self.conn.execute("UPDATE closure_task_modes SET mode='full',upgraded_reason=?,updated=? WHERE task_id=? AND mode='light'",
                          (reason, time.time(), identifier))

    def observe_closure_operation(self, identifier, paths=(), *, operational=False):
        """Retain only bounded path metadata, never file bodies or command text."""
        if not isinstance(paths, (tuple, list)) or len(paths) > 4096:
            paths, operational = (), True
        values = []
        for path in paths:
            if not isinstance(path, str) or not Path(path).is_absolute() or len(path) > 4096:
                operational = True
            else: values.append(path)
        values = list(dict.fromkeys(values))
        if not values: values = [""]  # An opaque effect cannot claim an exemption.
        with self.transaction():
            existing = {row[0] for row in self.conn.execute("SELECT path FROM closure_native_effects WHERE task_id=?", (identifier,))}
            if len(existing | set(values)) > 64: values, operational = [""], True
            for path in values:
                self.conn.execute("INSERT INTO closure_native_effects VALUES(?,?,?) ON CONFLICT(task_id,path) DO UPDATE SET operational=MAX(operational,excluded.operational)",
                                  (identifier, path, int(bool(operational))))
            if self.conn.execute("SELECT 1 FROM closure_tasks WHERE id=?", (identifier,)).fetchone():
                self.closure_applicability(identifier)

    def closure_applicability(self, identifier):
        task = self.conn.execute("SELECT * FROM closure_tasks WHERE id=?", (identifier,)).fetchone()
        if not task: raise ValueError("task_missing")
        mode = self.conn.execute("SELECT * FROM closure_task_modes WHERE task_id=?", (identifier,)).fetchone() if self.has_table("closure_task_modes") else None
        issues = self.conn.execute("SELECT 1 FROM closure_issues WHERE task_id=? OR id IN (SELECT issue_id FROM closure_events WHERE task_id=? AND kind='duplicate_linked') LIMIT 1", (identifier, identifier)).fetchone()
        effects = self.conn.execute("SELECT * FROM closure_native_effects WHERE task_id=?", (identifier,)).fetchall() if self.has_table("closure_native_effects") else []
        activity = self.conn.execute("SELECT mutated FROM closure_hook_activity WHERE task_id=?", (identifier,)).fetchone() if self.has_table("closure_hook_activity") else None
        reason = None
        if mode and mode["mode"] == "light":
            try:
                paths = artifact_declaration(json.loads(mode["artifact_paths"]), task["scope_root"])
                if issues: reason = "registered_issue"
                elif any(row["operational"] or not row["path"] or row["path"] not in paths for row in effects): reason = "operational_or_undeclared_effect"
                elif activity and activity[0] and not effects: reason = "legacy_effect_unverified"
            except (ValueError, OSError, TypeError): reason = "artifact_scope_changed"
            if reason: self._upgrade_closure(identifier, reason)
            return {"closure_mode": "full" if reason else "light", "closure_required": bool(reason), "closure_reasons": [reason] if reason else []}
        required = bool(mode or task["complex"] or issues or effects or activity and activity[0])
        return {"closure_mode": "full" if mode else "legacy", "closure_required": required,
                "closure_reasons": [mode["upgraded_reason"]] if mode and mode["upgraded_reason"] else []}

    def register(self, payload, *, native_context=None):
        task = self.conn.execute("SELECT * FROM closure_tasks WHERE id=?", (payload["task_id"],)).fetchone()
        if not task: raise ValueError("task_missing")
        category = payload["category"]
        if category not in {"main", "related", "independent"}: raise ValueError("category_invalid")
        root = Path(payload.get("target_root") or task["scope_root"]).expanduser().resolve()
        if not root.is_dir(): raise ValueError("target_unavailable")
        summary = bounded_text(payload["summary"], "summary", 1000)
        rows = evidence(payload["evidence"], root)
        authority = payload["authority"]
        if not isinstance(authority, dict) or not authority.get("source"):
            raise ValueError("authority_required")
        # Explicit limits are part of the immutable dispatch record, not inferred
        # from a model's confidence or a receiver's acknowledgement.
        if set(authority) - {"source", "scope", "reversible", "cost", "new_permission", "external", "personal_choice"}:
            raise ValueError("authority_fields_invalid")
        bounded_text(authority["source"], "authority", 2000)
        initial_state, authority_recovery = authority_readiness(authority, root)
        safe = initial_state == "pending"
        fingerprint = digest([str(root), bounded_text(payload["key"], "key", 300)])
        owner = "codex" if category == "independent" and safe else task["platform"] + ":" + task["session"]
        identifier = uuid.uuid4().hex
        with self.transaction():
            self._upgrade_closure(task["id"], "registered_issue")
            old = self.conn.execute("SELECT * FROM closure_issues WHERE fingerprint=?", (fingerprint,)).fetchone()
            if not old and payload.get("scope_correction") is not None:
                raise ValueError("scope_correction_issue_missing")
            if old:
                old_authority = json.loads(old["authority"])
                if payload.get("scope_correction") is not None:
                    correction = payload["scope_correction"]
                    expected_context = {"platform": task["platform"], "session": task["session"], "turn": task["turn"], "task_id": task["id"]}
                    # The original executor can repair an unusable encoding,
                    # never release a choice or resume an existing execution.
                    if (not isinstance(correction, dict) or set(correction) != {"revision", "basis"}
                            or correction["revision"] != old["revision"] or not safe
                            or native_context != expected_context or not self.native_root(task)
                            or task["scope_root"] != old["target_root"]
                            or old["owner"] != task["platform"] + ":" + task["session"]
                            or old["state"] != "blocked" or old["dispatch_state"] != "new"
                            or old["thread_id"] or old["message_id"] or old["receipt"]
                            or not current(json.loads(old["evidence"])) or rows != json.loads(old["evidence"])
                            or {k: v for k, v in old_authority.items() if k != "scope"} != {k: v for k, v in authority.items() if k != "scope"}
                            or authority_readiness(old_authority, root)[0] != "blocked"):
                        raise ValueError("scope_correction_binding_invalid")
                    basis = bounded_text(correction["basis"], "scope_correction_basis", 2000)
                    self.conn.execute("UPDATE closure_issues SET authority=?,revision=revision+1,recovery=?,updated=? WHERE id=?",
                                      (json.dumps(authority), "范围格式已核清；保留受阻与原负责人，不自动恢复或派发。", time.time(), old["id"]))
                    self.event("authority_scope_corrected", {"before": old_authority["scope"], "after": authority["scope"], "basis": basis}, issue=old["id"], task=task["id"])
                    self.event("duplicate_linked", {"source_task": task["id"]}, issue=old["id"], task=task["id"])
                    return {"issue": self.get(old["id"]), "reused": True}
                restricted = (not safe and old["state"] not in {"canceled", "dismissed"})
                if restricted:
                    state = "decision" if old["state"] == "decision" else initial_state
                    self.conn.execute("UPDATE closure_issues SET state=?,authority=?,acceptance=NULL,revision=revision+1,recovery=?,updated=? WHERE id=?",
                                      (state, json.dumps(authority), authority_recovery, time.time(), old["id"]))
                    self.event("authority_restricted", {"authority": authority, "state": state}, issue=old["id"], task=task["id"], notify=old["state"] != state)
                elif old["state"] == "resolved":
                    accepted = json.loads(old["acceptance"] or "{}")
                    if not current(accepted.get("evidence", [])) or rows != accepted.get("evidence"):
                        self.conn.execute("UPDATE closure_issues SET state='verification',evidence=?,acceptance=NULL,revision=revision+1,updated=?,recovery=? WHERE id=?",
                                          (json.dumps(rows), time.time(), "新来源版本已登记；复核原会话与受影响部分，不重新派发。", old["id"]))
                        self.event("recurrence_version", {"evidence": rows}, issue=old["id"], task=task["id"])
                self.event("duplicate_linked", {"source_task": task["id"]}, issue=old["id"], task=task["id"])
                return {"issue": self.get(old["id"]), "reused": True}
            self.conn.execute("""INSERT INTO closure_issues(id,task_id,fingerprint,category,summary,target_root,project_id,
              evidence,authority,owner,recommendation,acceptance_rule,state,created,updated)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (identifier, task["id"], fingerprint, category, summary, str(root), payload.get("project_id"),
               json.dumps(rows), json.dumps(authority), owner,
               bounded_text(payload["recommendation"], "recommendation"),
               bounded_text(payload["acceptance_rule"], "acceptance_rule"),
               initial_state, time.time(), time.time()))
            if authority_recovery:
                self.conn.execute("UPDATE closure_issues SET recovery=? WHERE id=?", (authority_recovery, identifier))
            self.event("registered", {"category": category, "evidence": rows, "state": initial_state}, issue=identifier,
                       task=task["id"], notify=not safe)
        return {"issue": self.get(identifier), "reused": False}

    def transition(self, identifier, state, reason, expected_revision=None, recovery=None):
        if state not in STATES or state == "resolved": raise ValueError("acceptance_required")
        reason = bounded_text(reason, "reason")
        with self.transaction():
            item = self.get(identifier)
            if expected_revision != item["revision"]: raise ValueError("revision_changed")
            if item["state"] in TERMINAL: raise ValueError("terminal_requires_new_issue")
            if item["state"] == "decision" and state not in {"canceled", "dismissed", "decision"}:
                raise ValueError("explicit_human_decision_required")
            if state in {"pending", "running"} and not safe_authority(item):
                raise ValueError("execution_authority_invalid")
            if state == "blocked" and not recovery: raise ValueError("recovery_required")
            self.conn.execute("UPDATE closure_issues SET state=?,error=?,recovery=?,revision=revision+1,updated=? WHERE id=?",
                              (state, reason, recovery, time.time(), identifier))
            self.event("state_changed", {"from": item["state"], "to": state, "reason": reason}, issue=identifier,
                       task=item["task_id"], notify=state in {"blocked", "decision"} and state != item["state"])
        return self.get(identifier)

    def choice_preview(self, identifier):
        with self.transaction():
            item = self.get(identifier)
            if item["state"] != "decision": raise ValueError("decision_not_pending")
            token = uuid.uuid4().hex
            self.conn.execute("INSERT INTO closure_choices VALUES(?,?,?,?,?,?)",
                              (token, identifier, item["revision"], None, time.time(), None))
        return {"token": token, "issue_id": identifier, "revision": item["revision"], "recommendation": item["recommendation"],
                "choices": ["accept_recommendation", "cancel", "keep_waiting"], "default": None}

    def decide(self, payload):
        """Native explicit choice; no timeout/default and no new monetary action.

        Like existing AGI choice APIs, calling this action requires the real
        owner's current click/reply. A model must never manufacture that event.
        The token binds displayed terms; it is not itself proof of human consent.
        """
        with self.transaction():
            row = self.conn.execute("SELECT * FROM closure_choices WHERE token=?", (payload["token"],)).fetchone()
            if not row or row["answered"] is not None: raise ValueError("choice_missing_or_already_answered")
            if payload.get("issue_id") != row["issue_id"] or payload.get("revision") != row["revision"]:
                raise ValueError("choice_display_binding_mismatch")
            item = self.get(row["issue_id"])
            if item["state"] != "decision" or item["revision"] != row["revision"] or not current(json.loads(item["evidence"])):
                raise ValueError("choice_terms_changed")
            choice = payload["choice"]
            if choice not in {"accept_recommendation", "cancel", "keep_waiting"}: raise ValueError("choice_invalid")
            state = "decision"
            authority = json.loads(item["authority"])
            if choice == "cancel": state = "canceled"
            elif choice == "accept_recommendation":
                authority["personal_choice"] = False
                readiness, _ = authority_readiness(authority, item["target_root"])
                state = "verification" if readiness == "decision" else readiness
            self.conn.execute("UPDATE closure_choices SET choice=?,answered=? WHERE token=?", (choice, time.time(), row["token"]))
            self.conn.execute("UPDATE closure_issues SET state=?,authority=?,revision=revision+1,recovery=?,updated=? WHERE id=?",
                              (state, json.dumps(authority), "已记录本次选择；实际费用、账号新权限和对外行为仍需具体授权与事实核对。", time.time(), item["id"]))
            self.event("explicit_choice", {"choice": choice, "token": row["token"], "displayed_revision": row["revision"]}, issue=item["id"], task=item["task_id"])
        return self.get(item["id"])

    def accept(self, payload):
        with self.transaction():
            item = self.get(payload["issue_id"])
            if item["revision"] != payload.get("revision"): raise ValueError("revision_changed")
            if item["state"] in TERMINAL or item["state"] == "decision": raise ValueError("issue_not_accepting")
            root = Path(item["target_root"])
            final = evidence(payload["evidence"], root)
            # Repairs legitimately change source files. Require the reviewer to
            # bind final versions and explicitly account for the discovery delta.
            review = bounded_text(payload["review"], "review")
            if payload.get("verified") is not True or not payload.get("reviewer"):
                raise ValueError("verification_required")
            if not current(json.loads(item["evidence"])) and not payload.get("source_change_explanation"):
                raise ValueError("source_change_explanation_required")
            acceptance = {"evidence": final, "review": review, "reviewer": payload["reviewer"],
                          "source_change_explanation": payload.get("source_change_explanation"), "at": time.time()}
            self.conn.execute("UPDATE closure_issues SET state='resolved',acceptance=?,revision=revision+1,updated=? WHERE id=?",
                              (json.dumps(acceptance), time.time(), item["id"]))
            self.event("accepted", acceptance, issue=item["id"], task=item["task_id"], notify=True)
        return self.get(item["id"])

    def cancellation_check(self, payload):
        identifier = payload["issue_id"]
        item = self.get(identifier)
        if item["state"] not in {"canceled", "dismissed"}: raise ValueError("issue_not_canceled")
        if item["revision"] != payload.get("revision"): raise ValueError("revision_changed")
        check_started = time.time()
        self.reconcile(identifier)  # Read original effects; never submit/start.
        with self.transaction():
            item = self.get(identifier)
            if item["revision"] != payload.get("revision") or item["state"] not in {"canceled", "dismissed"}:
                raise ValueError("revision_changed")
            receipt = json.loads(item["receipt"] or "{}")
            live = receipt.get("runtime", {})
            stopped = (live.get("source") == "desktop_owner" and live.get("activity") == "idle"
                       and live.get("turn_status") in {"completed", "failed", "interrupted"}
                       and live.get("turn_id") == receipt.get("turn_id")
                       and check_started <= live.get("observed_at", 0) <= time.time()
                       and time.time() - live.get("observed_at", 0) <= 30)
            if effects_pending(item) and not stopped: raise ValueError("canceled_effects_not_verified_stopped")
            if payload.get("verified") is not True or not payload.get("reviewer"): raise ValueError("verification_required")
            final = evidence(payload["evidence"], Path(item["target_root"]))
            checked = {"cancellation_checked": True, "evidence": final,
                       "review": bounded_text(payload["review"], "review"),
                       "reviewer": bounded_text(payload["reviewer"], "reviewer", 300),
                       "stop_receipt": live, "at": time.time()}
            self.conn.execute("UPDATE closure_issues SET acceptance=?,revision=revision+1,updated=? WHERE id=?",
                              (json.dumps(checked), time.time(), identifier))
            self.event("canceled_effects_checked", checked, issue=identifier, task=item["task_id"], notify=True)
        return self.get(identifier)

    def audit(self, payload):
        task = self.conn.execute("SELECT * FROM closure_tasks WHERE id=?", (payload["task_id"],)).fetchone()
        if not task: raise ValueError("task_missing")
        applicability = self.closure_applicability(task["id"])
        if applicability["closure_mode"] == "light" and not applicability["closure_required"]:
            return self.task_status(task["id"], allow_manual=True)
        review = payload["review"]
        for key in ("main", "related", "discovered", "failed_steps", "verification_gaps", "downstream"):
            bounded_text(review.get(key), "review_" + key)
        independent = payload.get("independent") is True
        if independent:
            review["reviewer"] = bounded_text(payload.get("reviewer"), "reviewer", 300)
            review["independent_artifact"] = evidence([payload["independent_artifact"]], Path(task["scope_root"]))[0]
        if task["complex"] and not independent and not review.get("independent_gap"):
            raise ValueError("independent_review_or_gap_required")
        rows = evidence(payload["evidence"], Path(task["scope_root"]))
        # Optional for historical callers: old structural acceptance stays valid,
        # but it never gains final-wording qualification without a fresh review.
        if payload.get("final_response") is not None:
            applicability = self.closure_applicability(task["id"])
            self_wording = not task["complex"] and applicability["closure_mode"] == "full"
            if (not independent and not self_wording) or payload.get("wording_approved") is not True:
                raise ValueError("independent_wording_review_required")
            artifact_key = "independent_artifact" if independent else "wording_artifact"
            if not independent:
                review["reviewer"] = bounded_text(payload.get("reviewer"), "reviewer", 300)
                review[artifact_key] = evidence([payload[artifact_key]], Path(task["scope_root"]))[0]
            final = payload["final_response"]
            bound = evidence([final], Path(task["scope_root"]))[0]
            if final.get("sha256") != bound["sha256"]:
                raise ValueError("final_response_hash_required")
            text = read_delivery_file(final["path"], Path(task["scope_root"])).decode("utf-8")
            if hashlib.sha256(text.encode()).hexdigest() != bound["sha256"]:
                raise ValueError("final_response_changed_during_read")
            artifact = json.loads(Path(review[artifact_key]["path"]).read_text())
            if (artifact.get("final_response_sha256") != bound["sha256"]
                    or artifact.get("wording_approved") is not True):
                raise ValueError("independent_artifact_not_bound_to_wording")
            if not text.strip() or len(text.encode()) > 128 * 1024:
                raise ValueError("final_response_invalid")
            facts = {"completion": final.get("completion"), "scope": bounded_text(final.get("scope"), "completion_scope", 500)}
            if facts["completion"] not in {"complete", "stage_complete", "partial", "incomplete"}:
                raise ValueError("completion_invalid")
            followups = final.get("remaining")
            if not isinstance(followups, list) or len(followups) > 40:
                raise ValueError("remaining_facts_required")
            facts["remaining"] = []
            for followup in followups:
                item = {key: bounded_text(followup.get(key), "remaining_" + key, 1000)
                        for key in ("summary", "blocker", "owner", "next_step", "continuation")}
                if followup.get("scope") not in {"in_scope", "out_of_scope"}:
                    raise ValueError("remaining_scope_required")
                item["scope"] = followup["scope"]
                if item["scope"] == "in_scope":
                    item["issue_id"] = bounded_text(followup.get("issue_id"), "remaining_issue_id", 100)
                facts["remaining"].append(item)
            action = final.get("user_action", {})
            if type(action.get("required")) is not bool:
                raise ValueError("user_action_required")
            facts["user_action"] = {"required": action["required"],
                "scope": bounded_text(action.get("scope"), "action_scope", 500),
                "reason": bounded_text(action.get("reason"), "action_reason", 1000)}
            status = self.task_status(task["id"], allow_manual=True)
            if {x["issue_id"] for x in facts["remaining"] if x["scope"] == "in_scope"} != {x["id"] for x in status["remaining"]}:
                raise ValueError("remaining_ledger_facts_mismatch")
            if facts["completion"] == "stage_complete" and (status["remaining"] or any(x["scope"] == "in_scope" for x in facts["remaining"])):
                raise ValueError("stage_has_in_scope_work")
            reasons = final_wording(text, facts, status["remaining"])
            if reasons: raise ValueError(reasons[0])
            linked = self.conn.execute("SELECT * FROM closure_issues WHERE task_id=? OR id IN (SELECT issue_id FROM closure_events WHERE task_id=? AND kind='duplicate_linked')", (task["id"], task["id"])).fetchall()
            review["final_response"] = {**bound, **facts, "snapshot": delivery_snapshot(linked), "schema": 1}
        with self.transaction():
            self.conn.execute("INSERT OR REPLACE INTO closure_audits VALUES(?,?,?,?,?,?)",
                              (task["id"], task["requirements_hash"], json.dumps(rows), json.dumps(review),
                               int(independent), time.time()))
            self.event("audited", {"independent": independent, "evidence": rows}, task=task["id"])
        return self.task_status(task["id"], allow_manual=True)

    def task_status(self, identifier, *, allow_manual=False):
        task = self.conn.execute("SELECT * FROM closure_tasks WHERE id=?", (identifier,)).fetchone()
        if not task: raise ValueError("task_missing")
        rows = self.conn.execute("SELECT * FROM closure_issues WHERE task_id=? OR id IN (SELECT issue_id FROM closure_events WHERE task_id=? AND kind='duplicate_linked')",
                                 (identifier, identifier)).fetchall()
        audit = self.conn.execute("SELECT * FROM closure_audits WHERE task_id=?", (identifier,)).fetchone()
        review = json.loads(audit["review"]) if audit else {}
        bundle = {**dict(task), "acceptance": review, "evidence": json.loads(audit["evidence"]) if audit else []}
        restored = allow_manual and manual_closure_allowed(task)
        check_retired = manual_owner_retired if allow_manual else lambda task, item, paths=(): owner_retired(item, paths)
        check_effects = manual_effects_pending if allow_manual else lambda task, item: effects_pending(item)
        linked_paths = [p for row in rows for p in metadata_paths(dict(row))]
        scope_allowed = not check_retired(task, bundle, linked_paths)
        if restored and any(not manual_closure_allowed(task, dict(row)) for row in rows): scope_allowed = False
        valid = bool(scope_allowed and audit and audit["requirements_hash"] == task["requirements_hash"] and current(json.loads(audit["evidence"]))
                     and (not audit["independent"] or current([review["independent_artifact"]])))
        remaining = []
        for row in rows:
            item = dict(row)
            if scope_allowed and item["state"] == "resolved" and not check_retired(task, item) and not current(json.loads(item["acceptance"] or "{}").get("evidence", [])):
                item["state"] = "verification"
                item["error"] = "acceptance_version_changed"
            needs_effects = (check_effects(task, item) if scope_allowed else
                item["state"] in {"canceled", "dismissed"} and item["dispatch_state"] not in {"new", "preparing", "created"})
            if item["state"] not in TERMINAL or needs_effects: remaining.append(item)
        status = {"task_id": identifier, "audit_valid": valid, "independent": bool(audit and audit["independent"]),
                  "remaining": remaining, "all_complete": valid and not remaining and (not task["complex"] or bool(audit["independent"]))}
        status["manual_restoration_active"] = restored
        status.update(self.closure_applicability(identifier))
        final = review.get("final_response")
        covered = {x["issue_id"] for x in final["remaining"] if x["scope"] == "in_scope"} if final else set()
        status["wording_valid"] = bool(valid and final and current([final]) and final.get("snapshot") == delivery_snapshot(rows)
                                       and (not review.get("wording_artifact") or current([review["wording_artifact"]]))
                                       and covered == {x["id"] for x in remaining}
                                       and (final["completion"] not in {"complete", "stage_complete"} or not remaining))
        status["final_response_sha256"] = final.get("sha256") if status["wording_valid"] else None
        status["delivery_ready"] = bool(status["wording_valid"] and not remaining)
        if not scope_allowed:
            # Scope rejection precedes *all* body/runtime/recovery reads,
            # including terminal cancellation records in a foreign project.
            status["manual_restoration_active"] = False
            status["stop_decision"] = {"decision": "unknown", "reasons": ["本人停用或人工许可范围不符，需要原负责人核对"]}
            status["runtime"] = {item["id"]: {"activity": "unknown", "label": "范围尚未获准", "auto_execution": False} for item in remaining}
            status["recovery_status"] = {item["id"]: {"status": "inactive", "label": "未恢复自动检查", "auto_execution": False} for item in remaining}
            return status
        status["stop_decision"] = self.end_assessment(task, status, allow_manual=allow_manual)
        status["runtime"] = {item["id"]: self.runtime_view(item) for item in remaining}
        status["recovery_status"] = {item["id"]: self.recovery_view(item) for item in remaining}
        # A shared status call can hash files or inspect runtime after the first
        # permit read. Bind its final result to a still-current complete scope.
        if restored and not (manual_closure_allowed(task, bundle, linked_paths)
                and all(manual_closure_allowed(task, dict(row)) for row in rows)):
            status.update(audit_valid=False, all_complete=False, wording_valid=False,
                          final_response_sha256=None, delivery_ready=False, manual_restoration_active=False)
            status["stop_decision"] = {"decision": "unknown", "reasons": ["临时人工许可已失效或范围不符，需要原负责人重新核对"]}
        return status

    def check_final_response(self, identifier, text):
        status = self.task_status(identifier, allow_manual=True)
        if status.get("closure_required") is False:
            return {"valid": False, "not_applicable": True, "reasons": ["closure_not_applicable"]}
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > 128 * 1024:
            return {"valid": False, "reasons": ["native_final_text_missing_or_invalid"]}
        sha = hashlib.sha256(text.encode()).hexdigest()
        reasons = []
        if not status["wording_valid"]: reasons.append("final_wording_review_missing_or_stale")
        elif sha != status["final_response_sha256"]: reasons.append("native_final_text_mismatch")
        else:
            review = json.loads(self.conn.execute("SELECT review FROM closure_audits WHERE task_id=?", (identifier,)).fetchone()[0])
            reasons.extend(final_wording(text, review["final_response"], status["remaining"]))
        return {"valid": not reasons, "sha256": sha, "reasons": reasons}

    def dispatch(self, identifier):
        self.get(identifier)
        lock = self.root / ("closure-dispatch-" + identifier + ".lock")
        private(lock)
        with open(lock, "a") as handle:
            os.chmod(lock, 0o600)
            try: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise ValueError("dispatch_already_active")
            return self._dispatch_locked(identifier)

    def _dispatch_locked(self, identifier):
        from .attention_native import AppServer, DesktopIPC, open_thread, wait_owner
        with self.transaction():
            item = self.get(identifier)
            if automatic_retired(item): raise ValueError("owner_retired")
            if item["category"] != "independent" or item["state"] not in {"pending", "blocked"}:
                raise ValueError("issue_not_dispatchable")
            if self.declared_block(item): return item
            if item["dispatch_state"] not in {"new", "created", "preparing"}: return item
            if not current(json.loads(item["evidence"])):
                raise ValueError("source_changed_before_dispatch")
            authority = json.loads(item["authority"])
            if not safe_authority(item):
                raise ValueError("dispatch_authority_missing")
            owners = self.conn.execute("SELECT * FROM closure_issues WHERE id!=?", (identifier,)).fetchall()
            for row in owners:
                other = dict(row)
                left, right = Path(item["target_root"]), Path(other["target_root"])
                overlaps = left.is_relative_to(right) or right.is_relative_to(left)
                active = effects_pending(other) or (other["state"] not in TERMINAL and
                    (other["dispatch_state"] != "new" or (other["category"] in {"main", "related"} and other["state"] == "running")))
                if overlaps and active: raise ValueError("target_has_active_owner:" + other["id"])
            message_id = item["message_id"] or str(uuid.uuid4())
            prompt = ("请处理已授权的独立闭环事项。先复核当前项目入口、文件版本和下列权限边界；"
                      "不恢复已取消工作，不扩大权限。修复后提供真实验收依据，并通过 task-closure 验收接口登记；"
                      "接收消息不算解决。若目标有其他任务在修改，先协调归属。\n" + json.dumps({
                          "issue_id": identifier, "ledger_database": str(self.database), "summary": item["summary"], "target_root": item["target_root"],
                          "evidence": json.loads(item["evidence"]), "authority": authority,
                          "recommendation": item["recommendation"], "acceptance_rule": item["acceptance_rule"]}, ensure_ascii=False))
            if SENSITIVE.search(prompt): raise ValueError("sensitive_dispatch_blocked")
            prompt = item["prompt"] or prompt
            self.conn.execute("UPDATE closure_issues SET dispatch_state='preparing',message_id=?,prompt=?,updated=? WHERE id=?",
                              (message_id, prompt, time.time(), identifier))
            self.event("dispatch_started", {"message_id": message_id}, issue=identifier, task=item["task_id"])
            dispatch_binding = self.binding(item)
        def require_active():
            latest = self.get(identifier)
            if automatic_retired(latest): raise ValueError("owner_retired")
            return latest
        def created(thread):
            with self.transaction():
                self.conn.execute("UPDATE closure_issues SET thread_id=?,dispatch_state='created',updated=? WHERE id=?",
                                  (thread, time.time(), identifier))
        try:
            target = {"root": item["target_root"], "project_id": item["project_id"]}
            require_active()
            with AppServer(target=target) as server:
                require_active()
                project = server.project()
                mode = server.initial_collaboration_mode()
                require_active()
                if server.active_threads(exclude=item["thread_id"]):
                    raise ValueError("target_has_active_native_owner")
                thread = item["thread_id"]
                if thread:
                    require_active()
                    existing = server.read(thread)
                    if not existing.get("cwd") or Path(existing["cwd"]).resolve() != Path(item["target_root"]) or existing.get("projectId") != project:
                        raise ValueError("saved_thread_target_changed")
                if not thread:
                    # Read-only preparation is safely recoverable. Record
                    # uncertainty only immediately before the native creation.
                    with self.transaction():
                        latest = require_active()
                        if latest["state"] in TERMINAL or latest["state"] == "decision" or self.declared_block(latest) or not safe_authority(latest): return latest
                        if self.binding(latest) != dispatch_binding or not current(json.loads(latest["evidence"])):
                            raise ValueError("dispatch_binding_changed")
                        self.conn.execute("UPDATE closure_issues SET dispatch_state='creation_uncertain',project_id=? WHERE id=?", (project, identifier))
                        dispatch_binding = self.binding({**item, "project_id": project})
                    require_active()
                    thread = server.create(project, "闭环修复：" + item["summary"][:60], created)
            require_active()
            open_thread(thread)
            require_active()
            owner = wait_owner(thread)
            require_active()
            # Persist uncertainty before send. A timeout may still mean accepted.
            with self.transaction():
                latest = require_active()
                if latest["state"] in TERMINAL or latest["state"] == "decision" or self.declared_block(latest) or not safe_authority(latest): return latest
                if self.binding(latest) != dispatch_binding or not current(json.loads(latest["evidence"])):
                    raise ValueError("dispatch_binding_changed")
                self.conn.execute("UPDATE closure_issues SET dispatch_state='send_uncertain' WHERE id=?", (identifier,))
            require_active()
            with DesktopIPC() as ipc:
                require_active()
                ack = ipc.submit(thread, owner, message_id, prompt, collaboration_mode=mode)
            with self.transaction():
                latest = self.get(identifier)
                self.conn.execute("UPDATE closure_issues SET receipt=?,dispatch_state='acknowledged',updated=? WHERE id=?",
                                  (json.dumps({"ack": ack}), time.time(), identifier))
                if latest["state"] not in TERMINAL and latest["state"] != "decision" and not self.declared_block(latest):
                    self.conn.execute("UPDATE closure_issues SET state='running',revision=revision+1 WHERE id=? AND revision=?",
                                      (identifier, latest["revision"]))
                self.event("native_acknowledged", {"thread_id": thread}, issue=identifier, task=item["task_id"])
            return self.reconcile(identifier)
        except Exception as exc:
            with self.transaction():
                latest = self.get(identifier)
                if latest["state"] not in TERMINAL and latest["state"] != "decision" and not self.declared_block(latest):
                    code = getattr(exc, "code", type(exc).__name__)
                    if latest["dispatch_state"] == "preparing":
                        known = "created" if latest["thread_id"] else "new"
                        self.conn.execute("UPDATE closure_issues SET dispatch_state=? WHERE id=?", (known, identifier))
                    recovery = ("准备失败，尚未开始创建或发送；恢复入口后可继续同一派发。" if latest["dispatch_state"] == "preparing"
                                else "已创建但尚未发送；恢复后只继续原会话首次发送。" if latest["dispatch_state"] == "created"
                                else "先核对已保存会话和原消息回执；不得重复创建或发送。")
                    self.conn.execute("UPDATE closure_issues SET state='blocked',error=?,recovery=?,revision=revision+1,updated=? WHERE id=?",
                                      (code, recovery, time.time(), identifier))
                    self.event("dispatch_uncertain", {"code": code}, issue=identifier, task=item["task_id"],
                               notify=latest["state"] != "blocked" or latest["error"] != code)
            return self.get(identifier)

    def reconcile(self, identifier):
        from .attention_native import AppServer, DesktopIPC, receipt_in_thread
        item = self.get(identifier)
        if owner_retired(item): return item
        if item["state"] == "resolved" or item["state"] == "decision" or not item["thread_id"]: return item
        with AppServer(target={"root": item["target_root"], "project_id": item["project_id"]}) as server:
            if owner_retired(self.get(identifier)): return item
            project = server.project()
            if owner_retired(self.get(identifier)): return item
            thread = server.read(item["thread_id"])
        if not thread.get("cwd") or Path(thread["cwd"]).resolve() != Path(item["target_root"]):
            raise ValueError("receipt_target_mismatch")
        if thread.get("projectId") != project:
            raise ValueError("receipt_project_mismatch")
        receipt = receipt_in_thread(thread, item["message_id"], item["prompt"])
        if not receipt: return item
        # Offline history proves reception only. A desktop owner snapshot is
        # required before interpreting an execution outcome.
        try:
            with DesktopIPC() as ipc:
                if owner_retired(self.get(identifier)): return self.get(identifier)
                live = ipc.snapshot(item["thread_id"], expected_cwd=item["target_root"],
                                    message_id=item["message_id"], turn_id=receipt.get("turn_id"))
                if not isinstance(live, dict): raise ValueError("runtime_snapshot_invalid")
        except Exception as exc:
            live = {"activity": "unknown", "reason": getattr(exc, "code", type(exc).__name__), "observed_at": time.time()}
        receipt["runtime"] = live
        state = item["state"]
        if item["state"] not in TERMINAL and not self.declared_block(item) and live.get("source") == "desktop_owner":
            if live.get("turn_status") == "completed": state = "verification"
            elif live.get("turn_status") in {"failed", "interrupted"}: state = "blocked"
            elif live.get("activity") == "active": state = "running"
        if item["state"] in {"canceled", "dismissed"} or self.declared_block(item):
            with self.transaction():
                latest = self.get(identifier)
                if latest["revision"] == item["revision"] and latest["state"] == item["state"]:
                    self.conn.execute("UPDATE closure_issues SET receipt=?,dispatch_state='received' WHERE id=?", (json.dumps(receipt), identifier))
            return self.get(identifier)
        if state == item["state"] and item["dispatch_state"] == "received":
            with self.transaction():
                latest = self.get(identifier)
                if latest["revision"] == item["revision"] and latest["state"] not in TERMINAL and latest["state"] != "decision":
                    # Observation freshness is not a business transition and
                    # never produces a notification or advances its revision.
                    self.conn.execute("UPDATE closure_issues SET receipt=? WHERE id=?", (json.dumps(receipt), identifier))
        else:
            with self.transaction():
                latest = self.get(identifier)
                if latest["revision"] != item["revision"] or latest["state"] in TERMINAL or latest["state"] == "decision": return latest
                error = "native_turn_" + str(live.get("turn_status")) if state == "blocked" else None
                recovery = "核对原会话真实失败原因、当前来源和有效权限后，在同一会话继续；不得自动恢复用户取消或重复不明发送。" if state == "blocked" else None
                self.conn.execute("UPDATE closure_issues SET state=?,receipt=?,error=?,recovery=?,dispatch_state='received',revision=revision+1,updated=? WHERE id=?",
                                  (state, json.dumps(receipt), error, recovery, time.time(), identifier))
                self.event("receipt_checked", receipt, issue=identifier, task=item["task_id"], notify=state == "blocked")
        return self.get(identifier)


def codex_thread(item):
    """Navigation only: never relabel the owner or rewrite dispatch receipts."""
    from .attention_native import thread_id, NativeError
    dispatched = item.get("thread_id")
    owner = item.get("owner", "")
    candidate = dispatched or (owner[len("codex:"):] if owner.startswith("codex:") else None)
    try:
        return thread_id(candidate)
    except NativeError:
        return None


def handoff_threads(database):
    """Read only the local queue's saved conversation identities, never bodies."""
    path = root_for(database) / "handoff.sqlite"
    if not path.exists(): return {}
    try:
        private(path)
        with contextlib.closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
            conn.execute("PRAGMA query_only=ON")
            rows = conn.execute("SELECT p.id,p.thread_id,s.source FROM problems p JOIN submissions s ON s.problem_id=p.id "
                                "WHERE p.id LIKE 'closure:%' AND p.thread_id IS NOT NULL "
                                "AND s.id=(SELECT latest.id FROM submissions latest WHERE latest.problem_id=p.id ORDER BY created DESC LIMIT 1)").fetchall()
        return {identifier: {"thread_id": thread, "source": json.loads(source or "{}")} for identifier, thread, source in rows}
    except (OSError, ValueError, sqlite3.Error):
        return {}


class _ProjectionRetirement:
    """Deduplicate read-only classification within one workspace response.

    No result survives a refresh or is used by a write/dispatch/acceptance
    gate. Check the policy files and company-root aliases before admitting
    each item, and again before returning the response.
    """
    def __init__(self):
        self.files = (personal_scope.ROOT / "policy.json",
                      personal_scope.ROOT / "retirements.json", personal_scope.BRIDGE)
        self.roots = ()
        before = self._binding()[0]
        self.roots = tuple(Path(path).expanduser() for path in personal_scope.policy()["company_roots"])
        self.binding = self._binding()
        if before != self.binding[0]:
            raise ValueError("closure_projection_policy_changed")
        self.cache = {}
        self.company_roots = tuple((path, path.rstrip("/") + "/") for path in self.binding[1])

    def _binding(self):
        identities = []
        for path in self.files:
            info = path.stat()
            identities.append((info.st_dev, info.st_ino, info.st_size,
                               info.st_mtime_ns, info.st_ctime_ns, info.st_mode))
        return tuple(identities), tuple(str(path.resolve()) for path in self.roots)

    def check(self):
        if self.binding != self._binding():
            raise ValueError("closure_projection_policy_changed")

    def classify(self, meta):
        # Resolve on every use: changing a path alias cannot reuse the old
        # target's classification even during this single read.
        path = str(Path(meta["root"]).expanduser().resolve())
        if path not in self.cache:
            # owner_retired passes only an explicit root and only consumes
            # the company's result. Personal/unknown classification cannot
            # change that result and need not re-resolve every policy root.
            self.cache[path] = "company" if any(path == root or path.startswith(prefix)
                for root, prefix in self.company_roots) else "unknown"
        return self.cache[path]

    def __call__(self, item, paths=()):
        self.check()
        result = owner_retired(item, paths, _classify=self.classify)
        self.check()
        return result


def projection(database, identifier=None):
    from .retirement_snapshot import source_snapshot
    from .presentation_scope import display_scope
    with source_snapshot(personal_scope, bounded_evidence=True), display_scope(Path(database).resolve().parent) as scope:
        result = scope.project(_projection(database, identifier))
        if not isinstance(result, list):
            raise ValueError("display_scope_unavailable")
        return result


def _projection(database, identifier=None):
    if not (root_for(database) / "closure.sqlite").exists(): return []
    with Store(database, readonly=True) as store:
        from .presentation_scope import for_connection, read_view
        scope = for_connection(store.conn)
        if scope.invalid or not scope.unchanged():
            raise ValueError("display_scope_unavailable")
        store.conn = read_view(store.conn, scope, ("closure_tasks", "closure_issues"))
        values = []
        from .retirement_snapshot import CURRENT, EvidenceReadUnavailable
        source_view = CURRENT.get()
        retirement = owner_retired if source_view is not None else _ProjectionRetirement()
        saved_threads = handoff_threads(database)
        acceptance_reads, evidence_unavailable = {}, set()
        pending_reads = {}

        def accepted_current(row):
            if row["id"] not in acceptance_reads:
                try:
                    acceptance_reads[row["id"]] = current(json.loads(row["acceptance"] or "{}").get("evidence", []))
                except EvidenceReadUnavailable:
                    acceptance_reads[row["id"]] = None
                    evidence_unavailable.add(row["id"])
            return acceptance_reads[row["id"]]

        def pending_effects(item):
            if item["id"] not in pending_reads:
                try:
                    pending_reads[item["id"]] = effects_pending(item)
                except EvidenceReadUnavailable:
                    # Unknown cancellation evidence cannot erase entered effects.
                    pending_reads[item["id"]] = True
                    evidence_unavailable.add(item["id"])
            return pending_reads[item["id"]]

        # Detail/open must not hash every other issue's evidence or rebuild the
        # memory/activity workspace before responding to a single click.
        if identifier is not None:
            active = store.conn.execute("SELECT * FROM closure_issues WHERE id=?", (identifier,)).fetchall()
            historical, stale = [], []
        else:
            # Never let recent historical items hide older unresolved obligations.
            active = store.conn.execute("SELECT * FROM closure_issues WHERE state NOT IN ('resolved','canceled','dismissed') ORDER BY updated DESC").fetchall()
            active += [r for r in store.conn.execute("SELECT * FROM closure_issues WHERE state IN ('canceled','dismissed')").fetchall() if not scope.hidden(dict(r)) and pending_effects(dict(r))]
            historical = store.conn.execute("SELECT * FROM closure_issues WHERE state IN ('resolved','canceled','dismissed') ORDER BY updated DESC LIMIT 100").fetchall()
            stale = store.conn.execute("SELECT * FROM closure_issues WHERE state='resolved'").fetchall()
            stale = [r for r in stale if not scope.hidden(dict(r)) and not retirement(dict(r)) and accepted_current(r) is False]
        known = {r["id"] for r in active + historical}
        active_ids = {r["id"] for r in active}
        for row in active + [r for r in historical if r["id"] not in active_ids] + [r for r in stale if r["id"] not in known]:
            item = dict(row)
            if scope.hidden(item):
                continue
            # One item/version is projected within this read only. Submission,
            # execution and the next refresh still perform fresh policy checks.
            retired = retirement(item)
            if scope.enabled and retired:
                continue
            if item["state"] == "resolved" and not retired and accepted_current(item) is False:
                item["state"] = "verification"; item["error"] = "acceptance_version_changed"
            saved = saved_threads.get("closure:" + item["id"], {})
            saved_target = saved.get("source", {}).get("closure_target")
            helper_thread = codex_thread({"thread_id": saved.get("thread_id")}) if saved_target == {"root": item["target_root"], "project_id": item["project_id"]} else None
            from .attention_handoff import navigation_failure
            failure = navigation_failure(database, item) if not retired and item["state"] not in TERMINAL and item["dispatch_state"] == "new" and item["category"] != "independent" else None
            assistance = saved.get("source", {}).get("closure_assistance")
            # Sending remains bound to its submitted revision. The existing
            # assistance chat stays readable after an ordinary state change.
            assistance_thread = helper_thread if (assistance and assistance.get("thread_id") == codex_thread(item)) else None
            if assistance: helper_thread = None
            direct_thread = codex_thread(item) or helper_thread
            can_handoff = (not retired and not direct_thread and item["state"] not in TERMINAL and item["dispatch_state"] == "new" and item["category"] != "independent")
            runtime = store.runtime_view(item, _retired=retired)
            try:
                recovery = store.recovery_view(item, _retired=retired)
            except EvidenceReadUnavailable:
                recovery = {"status": "unknown", "label": RECOVERY_LABELS["unknown"], "auto_execution": False}
                evidence_unavailable.add(item["id"])
            entered_effects = pending_effects(item)
            unavailable = item["id"] in evidence_unavailable
            business_label = "本人停用" if retired else "登记为执行中" if item["state"] == "running" else LABELS[item["state"]]
            if unavailable:
                business_label = "历史记录为已结束，当前依据未读" if item["state"] in TERMINAL else "登记状态保留，当前依据未读"
            values.append({"id": "closure:" + item["id"], "kind": "closure", "title": item["summary"],
                           "summary": business_label + (" · 已进入执行的动作仍待核对" if entered_effects else "") + " · 负责人：" + item["owner"],
                           **({"current_verification": "unknown"} if unavailable else {}),
                           "runtime": runtime, "recovery_status": recovery,
                           "closure_runtime_label": runtime["label"], "closure_recovery_label": recovery["label"],
                           "state": item["state"], "revision": str(item["revision"]), "thread_id": item["thread_id"],
                           "codex_thread_id": direct_thread, "codex_open_via_handoff": not codex_thread(item) and bool(helper_thread),
                           "codex_handoff_available": can_handoff and not unavailable,
                           "codex_assistance_available": bool(failure and not assistance_thread) and not unavailable,
                           "codex_assistance_token": failure["token"] if failure else None,
                           "codex_assistance_thread_id": assistance_thread,
                           "requires_check": entered_effects, "requires_decision": item["state"] == "decision", "members": [], "children": [],
                           "background": item["summary"], "reason": "本次未读完当前依据，不能确认是否仍满足原验收条件；原历史记录保留。" if unavailable else item["error"] or "执行期间发现，持续跟踪到验收。",
                           "impact": "主任务交付不能替代该事项的修复与验收。", "inaction": "当前复验未知，不自动重跑或放行；原记录保留。" if unavailable else recovery["label"] + "；自动启动执行未启用。",
                           "recommendation": "先核对当前依据，再判断原条件是否仍成立。" if unavailable else item["recommendation"], "attempts": item["recovery"] or "证据版本与派发回执保留在独立台账。",
                           "completion_rule": item["acceptance_rule"]})
        if evidence_unavailable and identifier is None:
            # Every active decision row was read above. Historical freshness
            # cannot invent or remove a personal choice; it remains explicit.
            values.append({"id": "closure-evidence-read-unavailable", "kind": "system",
                "title": "部分事项的当前依据暂不可读", "summary": f"{len(evidence_unavailable)} 项当前复验未知，原记录保留。",
                "background": "完整待处理清单已经读取；部分历史验收或恢复依据本次未能读完。",
                "reason": "读取拒绝或超过本次核对时间，不能据此判断依据已经变化。",
                "impact": "待你决定的数量仍按已读取的个人事项统计；这些资料不能用于新的验收或放行。",
                "inaction": "保留未知，不自动重跑、重新批准或替你选择。",
                "recommendation": "核对当前依据后再判断；原决定和审核记录保留。",
                "requires_decision": False, "current_verification": "unknown"})
        if source_view is not None:
            if not source_view.unchanged():
                raise ValueError('closure_projection_policy_changed')
        else:
            retirement.check()
        return values


def tick(database):
    """Called by the existing daemon; only already-registered items, no models."""
    if not (root_for(database) / "closure.sqlite").exists(): return
    with Store(database) as store:
        lock = store.root / "closure-worker.lock"
        private(lock)
        with open(lock, "a") as handle:
            os.chmod(lock, 0o600)
            try: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: return
            _tick_locked(store)


def _tick_locked(store):
        from .presentation_scope import DisplayScope
        display = DisplayScope(store.database.parent)
        def retired(item):
            return automatic_retired(item) or display.hidden_closure(store.conn, item)
        # Actual imported module identity, separate from old acceptance/business state.
        atomic(store.root / "owner-retirement-runtime.json", packed({"pid": os.getpid(), "module_sha256": LOADED_MODULE_SHA256, "company_stopped": personal_scope.company_stopped(), "observed_at": time.time(), **personal_scope.binding()}))
        for row in store.conn.execute("SELECT * FROM closure_issues WHERE state='resolved' OR (state IN ('canceled','dismissed') AND json_extract(acceptance,'$.cancellation_checked')=1)").fetchall():
            if retired(dict(row)): continue
            if not current(json.loads(row["acceptance"] or "{}").get("evidence", [])):
                with store.transaction():
                    latest = store.get(row["id"])
                    if not retired(latest) and latest["state"] == row["state"] and latest["revision"] == row["revision"]:
                        state = "verification" if row["state"] == "resolved" else row["state"]
                        store.conn.execute("UPDATE closure_issues SET state=?,acceptance=NULL,revision=revision+1,error='acceptance_version_changed',recovery=?,updated=? WHERE id=?",
                                           (state, "重读变化来源与验收依据，只复核受影响部分；取消状态不恢复执行。", time.time(), row["id"]))
                        store.event("acceptance_invalidated", {}, issue=row["id"], task=row["task_id"])
        rows = personal_limited(store.conn.execute("""SELECT i.id,i.dispatch_state,i.target_root,i.evidence,i.acceptance,i.authority FROM closure_issues i LEFT JOIN closure_checks c ON c.issue_id=i.id
            WHERE (i.state IN ('pending','running','verification','blocked') OR
             (i.state IN ('canceled','dismissed') AND i.dispatch_state NOT IN ('new','preparing','created')
              AND coalesce(json_extract(i.acceptance,'$.cancellation_checked'),0)!=1)) AND
            (i.thread_id IS NOT NULL OR (i.category='independent' AND i.dispatch_state='new' AND
             (SELECT count(*) FROM closure_events e WHERE e.issue_id=i.id AND e.kind='dispatch_started')<3))
            ORDER BY coalesce(c.checked_at,0),i.created"""))
        for row in rows:
            if retired(store.get(row[0])): continue
            try:
                if store.get(row[0])["state"] in {"canceled", "dismissed"}: store.reconcile(row[0])
                elif row["dispatch_state"] in {"new", "created", "preparing"}: store.dispatch(row[0])
                else: store.reconcile(row[0])
            except (OSError, ValueError, RuntimeError): pass
            finally: store.conn.execute("INSERT OR REPLACE INTO closure_checks VALUES(?,?)", (row[0], time.time()))
        # Independently bounded readiness probes. They never dispatch or change
        # business state, even if the original blocker has disappeared.
        plans = personal_limited(store.conn.execute("SELECT r.issue_id,i.target_root,i.evidence,i.acceptance,i.authority,r.conditions FROM closure_recovery_plans r JOIN closure_issues i ON i.id=r.issue_id WHERE r.status NOT IN ('stale','inactive') ORDER BY coalesce(r.checked_at,0)"))
        for row in plans:
            if retired(store.get(row[0])): continue
            try: store.check_recovery(row[0])
            except (OSError, ValueError, RuntimeError): pass
        # Legacy owner-only rows are observed without arming recovery plans.
        observations = personal_limited(store.conn.execute("""SELECT i.* FROM closure_issues i LEFT JOIN closure_runtime_observations o ON i.id=o.issue_id
            WHERE i.state NOT IN ('resolved','canceled','dismissed','decision') AND i.owner LIKE 'codex:%'
            ORDER BY coalesce(json_extract(o.observation,'$.observed_at'),0)"""))
        for row in observations:
            if retired(dict(row)): continue
            try: store.observe_runtime(dict(row))
            except (OSError, ValueError, RuntimeError): pass
        _notify_pending(store)


NOTIFICATION_INTERVAL = 15 * 60
NOTIFICATION_STATES = ("decision", "blocked", "resolved", "canceled", "dismissed", "recovery_ready")


def _notice_state(row):
    kind = row["kind"]
    data = json.loads(row["data"])
    if kind == "state_changed":
        # Keep the event and updated explanation, but unchanged waiting states
        # are never new reasons to interrupt the owner.
        return data.get("to") if data.get("from") != data.get("to") else None
    if kind in {"registered", "authority_restricted"}: return data.get("state", "decision")
    if kind in {"dispatch_uncertain", "receipt_checked"}: return "blocked"
    if kind == "accepted": return "resolved"
    if kind == "canceled_effects_checked": return row["state"]
    if kind == "recovery_ready": return "recovery_ready"
    return None


def _notify_pending(store):
    """One bounded digest; retain events, deferred requests and uncertainty.

    Only counts and fixed local instructions enter the notification. Issue
    summaries, reasons, evidence paths and private task text stay in AGI.
    Existing requested/unknown records are neither replayed nor reclassified.
    """
    now = time.time()
    from .presentation_scope import DisplayScope
    display = DisplayScope(store.database.parent)
    def retired(identifier):
        item = store.get(identifier)
        return automatic_retired(item) or display.hidden_closure(store.conn, item)
    if display.invalid or not display.unchanged():
        return
    with store.transaction():
        rows = store.conn.execute("""SELECT n.event_seq,e.issue_id,e.kind,e.data,i.state,i.acceptance,i.dispatch_state
            FROM closure_notifications n JOIN closure_events e ON e.seq=n.event_seq
            LEFT JOIN closure_issues i ON i.id=e.issue_id
            WHERE n.requested=0 AND n.error IS NULL ORDER BY n.event_seq""").fetchall()
        if not rows: return
        eligible = []
        for row in rows:
            if row["issue_id"] and retired(row["issue_id"]): continue
            state = _notice_state(row)
            recovery_notice = state == "recovery_ready"
            recovery_valid = False
            if recovery_notice and row["issue_id"]:
                item = store.get(row["issue_id"])
                plan = store.conn.execute("SELECT * FROM closure_recovery_plans WHERE issue_id=?", (item["id"],)).fetchone()
                same_plan = bool(plan and plan["version"] == json.loads(row["data"]).get("version"))
                readiness = store.recovery_view(item)["status"]
                # A temporary unmet condition defers this same unsent notice;
                # it neither permanently drops it nor creates another event.
                if same_plan and readiness in {"waiting", "unknown"}: continue
                recovery_valid = same_plan and readiness == "ready"
            proof = json.loads(row["acceptance"] or "{}")
            stale_completion = state in TERMINAL and (
                not proof.get("evidence") or not current(proof["evidence"])
                or (state in {"canceled", "dismissed"} and proof.get("cancellation_checked") is not True))
            if (state not in NOTIFICATION_STATES or (not recovery_valid if recovery_notice else state != row["state"])
                    or stale_completion):
                store.conn.execute("UPDATE closure_notifications SET error='notification_obsolete' WHERE event_seq=?", (row["event_seq"],))
            else:
                eligible.append(row)
        eligible = [row for row in eligible if not row["issue_id"] or not retired(row["issue_id"])]
        if not eligible: return
        last = store.conn.execute("""SELECT max(at) FROM closure_events WHERE kind IN
            ('notification_digest_started','notification_digest_requested','notification_digest_uncertain')""").fetchone()[0]
        if last is None:
            # Honor the previous sender's most recent request at upgrade time.
            last = store.conn.execute("""SELECT max(e.at) FROM closure_notifications n
                JOIN closure_events e ON e.seq=n.event_seq WHERE n.requested=1 OR n.error='request_uncertain'""").fetchone()[0]
        if last is not None and now - last < NOTIFICATION_INTERVAL: return
        unique = {row["issue_id"]: _notice_state(row) for row in eligible}
        counts = {state: sum(value == state for value in unique.values()) for state in NOTIFICATION_STATES}
        notice_labels = {**LABELS, "recovery_ready": "恢复条件已满足"}
        labels = [f"{count}项{notice_labels[state]}" for state, count in counts.items() if count]
        title = "AGI · " + "、".join(labels)
        explanations = {"decision": "待决定事项需要你选择", "blocked": "受阻事项由负责人继续跟进",
                        "resolved": "已解决事项已有验收记录", "canceled": "已核对取消事项的执行影响",
                        "dismissed": "已核对关闭事项的执行影响", "recovery_ready": "恢复条件已满足，未安排自动执行"}
        body = "；".join(explanations[state] for state, count in counts.items() if count) + "。打开 AGI → 注意力中心，查看具体事项。"
        sequences = [row["event_seq"] for row in eligible]
        # Persist the whole batch before entering the side effect. A crash or
        # uncertain subprocess outcome cannot fan this batch out or resend it.
        if any(row["issue_id"] and retired(row["issue_id"]) for row in eligible): return
        store.conn.executemany("UPDATE closure_notifications SET error='request_uncertain' WHERE event_seq=?", [(seq,) for seq in sequences])
        store.event("notification_digest_started", {"event_seqs": sequences, "counts": counts})
    if any(row["issue_id"] and retired(row["issue_id"]) for row in eligible):
        with store.transaction():
            retired_sequences = [row["event_seq"] for row in eligible if row["issue_id"] and retired(row["issue_id"])]
            personal = [seq for seq in sequences if seq not in retired_sequences]
            store.conn.executemany("UPDATE closure_notifications SET error='owner_retired_before_send' WHERE event_seq=? AND requested=0 AND error='request_uncertain'", [(seq,) for seq in retired_sequences])
            # These exact rows were reserved by this call and no sender was
            # entered. Preserve personal notices for the existing next tick;
            # never reset an earlier unknown/requested batch.
            store.conn.executemany("UPDATE closure_notifications SET error=NULL WHERE event_seq=? AND requested=0 AND error='request_uncertain'", [(seq,) for seq in personal])
            store.event("notification_digest_owner_retired_before_send", {"retired_event_seqs": retired_sequences, "personal_deferred_event_seqs": personal})
        return
    try:
        script = "display notification " + json.dumps(body, ensure_ascii=False) + " with title " + json.dumps(title, ensure_ascii=False)
        result = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired) as error:
        # The worker lock stays held until this call finishes. Count the end
        # too, so waking a suspended sender cannot produce adjacent digests.
        # This records uncertainty without changing or retrying the requests.
        with store.transaction():
            store.event("notification_digest_uncertain", {"event_seqs": sequences, "counts": counts,
                                                        "error_type": type(error).__name__})
        return
    with store.transaction():
        requested = int(result.returncode == 0)
        store.conn.executemany("UPDATE closure_notifications SET requested=?,error=? WHERE event_seq=? AND error='request_uncertain'",
                               [(requested, None if requested else "notification_rejected", seq) for seq in sequences])
        store.event("notification_digest_requested", {"event_seqs": sequences, "counts": counts, "requested": bool(requested)})


def wake(database):
    if not (root_for(database) / "closure.sqlite").exists(): return
    launcher = Path(__file__).resolve().parents[1] / "pam_isolated_launcher.py"
    subprocess.Popen([sys.executable, "-I", "-S", str(launcher), "--db", str(database), "task-closure", "tick"],
                     cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def _main(argv=None, database=None):
    parser = argparse.ArgumentParser(description="Closure Steward private ledger")
    parser.add_argument("action", choices=("begin", "register", "list", "issue", "status", "audit", "transition", "accept", "dispatch", "reconcile", "tick", "choice-preview", "decide", "cancellation-check", "recovery-register"))
    parser.add_argument("--id")
    parser.add_argument("--input", help="JSON input file; otherwise standard input")
    parser.add_argument("--db", default=database)
    args = parser.parse_args(argv)
    payload = {}
    if args.action in {"begin", "register", "audit", "transition", "accept", "decide", "cancellation-check", "recovery-register"}:
        raw = Path(args.input).read_text() if args.input else sys.stdin.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024: raise ValueError("payload_oversize")
        payload = json.loads(raw)
    if not args.db: raise ValueError("database_path_required")
    if args.action == "tick": tick(args.db); result = {"status": "checked"}
    elif args.action == "list": result = {"items": projection(args.db)}
    else:
        with Store(args.db, readonly=args.action in {"status", "issue"}) as store:
            if args.action == "begin": result = store.begin(payload)
            elif args.action == "register": result = store.register(payload, native_context=store.recovery_caller() if payload.get("scope_correction") is not None else None)
            elif args.action == "recovery-register": result = store.register_recovery(payload, native_context=store.recovery_caller())
            elif args.action == "audit": result = store.audit(payload)
            elif args.action == "status": result = store.task_status(args.id, allow_manual=True)
            elif args.action == "issue": result = {"issue": store.get(args.id)}
            elif args.action == "accept": result = store.accept(payload)
            elif args.action == "cancellation-check": result = store.cancellation_check(payload)
            elif args.action == "choice-preview": result = store.choice_preview(args.id)
            elif args.action == "decide": result = store.decide(payload)
            elif args.action == "transition": result = store.transition(args.id, payload["state"], payload["reason"], payload.get("revision"), payload.get("recovery"))
            elif args.action == "dispatch": result = store.dispatch(args.id)
            else: result = store.reconcile(args.id)
    print(json.dumps(result, ensure_ascii=False))


def main(argv=None, database=None):
    try:
        _main(argv, database)
    except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"interface": "task-closure", "status": "blocked", "error":
                          str(exc) if isinstance(exc, ValueError) else type(exc).__name__}, ensure_ascii=False))
        raise SystemExit(2)


if __name__ == "__main__": main()
