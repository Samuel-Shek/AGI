#!/usr/bin/env python3
from __future__ import annotations

import re
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


RUNTIME_PATH_RE = re.compile(
    r"(^|/)(core/data|data/activity\.sqlite|data/ha_packages|data/raw_vault)(/|$)"
)
BLOCKED_SUFFIXES = {
    ".sqlite",
    ".sqlite3",
    ".db",
    ".log",
    ".png",
    ".jpg",
    ".jpeg",
    ".heic",
    ".icns",
    ".key",
    ".keychain",
    ".keychain-db",
    ".mobileprovision",
    ".p12",
    ".pfx",
    ".shm",
    ".wal",
}
FORBIDDEN_DIRECTORY_PARTS = {
    ".build",
    ".pytest_cache",
    ".swiftpm",
    "DerivedData",
    "__pycache__",
    "backups",
    "build",
    "data",
    "ha_packages",
    "raw_vault",
    "state",
}
ALLOWED_IMAGE_RE = re.compile(
    r"^apps/PersonalAIMonitorMenuBar/.*/(?:AppIcon|icon[_-]?[0-9]+x[0-9]+|Icon)[^/]*\.(?:png|icns)$",
    re.IGNORECASE,
)
# Approved source artwork, bound to bytes rather than a general image directory.
APP_ICON_SOURCE = "assets/agi/AGI-app-icon.png"
APP_ICON_SHA256 = "57dfcad94ed8a467c2e70ef6eeeafed40f5b08b0643c7c10bb52b3b9937795eb"
APP_ICON_COMPILED = "assets/agi/AGI-app-icon.icns"
APP_ICON_COMPILED_SHA256 = "395269636a6528165d20645c81e8078e12808d24480d0c7b28de8d1b3fe86270"
EXTENSION_FORBIDDEN_PERMISSIONS = {
    "history",
    "bookmarks",
    "clipboardRead",
    "clipboardWrite",
    "downloads",
    "cookies",
}
EXTENSION_ALLOWED_PERMISSIONS = {"nativeMessaging", "tabs", "webNavigation"}
EXTENSION_ALLOWED_HOST_PERMISSIONS = {"<all_urls>"}
EXTENSION_ALLOWED_CONTENT_SCRIPT_MATCHES = {"<all_urls>"}
CONTENT_SCRIPT_FORBIDDEN_PATTERNS = {
    "input_or_textarea_value_read": re.compile(r"\.value\b"),
    "selection_text_read": re.compile(r"getSelection\s*\("),
    "body_inner_text_read": re.compile(r"document\.body\.innerText|document\.documentElement\.innerText"),
    "body_text_content_read": re.compile(r"document\.body\.textContent|document\.documentElement\.textContent"),
}
SECRET_RULES = {
    "private_key_header": re.compile(
        r"-----BEGIN (?:(?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY|PGP PRIVATE KEY BLOCK)-----"
    ),
    "sk_api_token": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "github_token": re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b"
    ),
    "gitlab_token": re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    "aws_access_key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "google_api_key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "slack_token": re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{20,}\b"),
    "stripe_live_secret": re.compile(r"\b(?:sk|rk)_live_[0-9A-Za-z]{16,}\b"),
    "npm_token": re.compile(r"\bnpm_[A-Za-z0-9]{30,}\b"),
}
LOCAL_USER_PATH_RE = re.compile(r"/Users/(?!example\b|test\b|runner\b)[A-Za-z0-9._-]+/")
ALLOWED_LOCAL_PATH_CONTENTS = {
    "docs/UNINSTALL.md",
    "core/activity_mvp/ha_source.py",
    "core/activity_mvp/privacy.py",
}


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    findings, checked, source = scan_root(root)
    if findings:
        print("Personal AI Monitor release privacy scan failed:", file=sys.stderr)
        for finding in findings:
            print(f"- {finding}", file=sys.stderr)
        return 1
    print(f"Personal AI Monitor release privacy scan passed ({checked} {source} files checked).")
    return 0


def scan_root(root: Path) -> tuple[list[str], int, str]:
    tracked, source = _candidate_files(root)
    findings: list[str] = []
    for rel in tracked:
        path = root / rel
        if _is_forbidden_runtime_file(rel, path):
            findings.append(f"forbidden runtime/build/state file: {rel}")
            continue
        if _is_forbidden_environment_file(rel):
            findings.append(f"forbidden environment file: {rel}")
            continue
        if rel == "core/browser_extension/manifest.json":
            findings.extend(_browser_manifest_findings(path))
        if rel == "core/browser_extension/content.js":
            findings.extend(_content_script_findings(path))
        if not path.is_file() or _looks_binary(path):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        findings.extend(_secret_findings(rel, text))
        if rel not in ALLOWED_LOCAL_PATH_CONTENTS and LOCAL_USER_PATH_RE.search(text):
            findings.append(f"local user path: {rel}")

    return findings, len(tracked), source


def _candidate_files(root: Path) -> tuple[list[str], str]:
    filesystem_files = _filesystem_files(root)
    git_files = _git_ls_files(root)
    if _is_git_worktree(root):
        # Release privacy acceptance is about what is physically present in
        # the candidate tree, not only what Git would commit. In particular,
        # ignored .env, database, build and runtime files are the highest-risk
        # packaging leaks, so Git and filesystem inventories are always
        # combined.
        return sorted(set(git_files) | set(filesystem_files)), "git+filesystem"
    return filesystem_files, "filesystem"


def _is_git_worktree(root: Path) -> bool:
    completed = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return completed.returncode == 0 and completed.stdout.strip() == "true"


def _git_ls_files(root: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        return []
    return [
        os.fsdecode(raw)
        for raw in completed.stdout.split(b"\0")
        if raw
    ]


def _filesystem_files(root: Path) -> list[str]:
    files: list[str] = []
    for path in root.rglob("*"):
        rel = path.relative_to(root).as_posix()
        if ".git" in path.relative_to(root).parts:
            continue
        if not path.is_file():
            continue
        files.append(rel)
    return sorted(files)


def _is_forbidden_runtime_file(rel: str, path: Path) -> bool:
    if RUNTIME_PATH_RE.search(rel):
        return True
    if any(part in FORBIDDEN_DIRECTORY_PARTS for part in Path(rel).parts):
        return True
    if rel in {APP_ICON_SOURCE, APP_ICON_COMPILED}:
        try:
            return (path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024
                    or hashlib.sha256(path.read_bytes()).hexdigest() !=
                    (APP_ICON_SHA256 if rel == APP_ICON_SOURCE else APP_ICON_COMPILED_SHA256))
        except OSError:
            return True
    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".heic", ".icns"}:
        return not ALLOWED_IMAGE_RE.match(rel)
    if path.suffix.lower() in BLOCKED_SUFFIXES:
        return True
    return False


def _is_forbidden_environment_file(rel: str) -> bool:
    name = Path(rel).name.lower()
    if name == ".env":
        return True
    return name.startswith(".env.") and not name.endswith(
        (".example", ".sample", ".template")
    )


def _secret_findings(rel: str, text: str) -> list[str]:
    findings: list[str] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for rule_name, pattern in SECRET_RULES.items():
            for match in pattern.finditer(line):
                # Never echo the matching line or token.  The relative path,
                # stable rule name and line number are sufficient to inspect
                # the candidate locally without leaking the suspected value.
                findings.append(
                    f"secret-looking value ({rule_name}): {rel}:{line_number}"
                )
                break
    return findings


def _browser_manifest_findings(path: Path) -> list[str]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [f"browser extension manifest unreadable: {path}"]
    permissions = {str(item) for item in manifest.get("permissions") or []}
    host_permissions = {str(item) for item in manifest.get("host_permissions") or []}
    findings = [f"forbidden browser extension permission: {permission}" for permission in sorted(permissions & EXTENSION_FORBIDDEN_PERMISSIONS)]
    if permissions != EXTENSION_ALLOWED_PERMISSIONS:
        findings.append("browser extension permissions changed; re-review privacy boundary")
    if host_permissions != EXTENSION_ALLOWED_HOST_PERMISSIONS:
        findings.append("browser extension host_permissions changed; re-review privacy boundary")
    content_scripts = manifest.get("content_scripts") or []
    matches = {
        str(match)
        for script in content_scripts
        if isinstance(script, dict)
        for match in (script.get("matches") or [])
    }
    if matches != EXTENSION_ALLOWED_CONTENT_SCRIPT_MATCHES:
        findings.append("browser extension content script matches changed; re-review privacy boundary")
    return findings


def _content_script_findings(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return [f"browser extension content script unreadable: {path}"]
    return [f"forbidden browser content access pattern: {name}" for name, pattern in CONTENT_SCRIPT_FORBIDDEN_PATTERNS.items() if pattern.search(text)]


def _looks_binary(path: Path) -> bool:
    try:
        chunk = path.read_bytes()[:4096]
    except OSError:
        return True
    return b"\0" in chunk


if __name__ == "__main__":
    raise SystemExit(main())
