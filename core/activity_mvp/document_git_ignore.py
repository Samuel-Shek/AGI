"""Reuse a baseline's Git result only while every ignore dependency matches.

Unsupported repository/configuration forms use the existing per-file check.
There is no TTL, persistent cache or permission result in these proofs.
"""
from __future__ import annotations

from contextvars import ContextVar
from functools import wraps
import os
from pathlib import Path
import stat
import subprocess

CURRENT = ContextVar("document_baseline_git_ignore_proofs", default=None)
MAX_PATHS = 2000


def baseline_pass(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        if CURRENT.get() is not None:
            return function(*args, **kwargs)
        token = CURRENT.set(IgnoreProofs())
        try:
            return function(*args, **kwargs)
        finally:
            CURRENT.reset(token)
    return wrapped


def _version(path):
    try:
        info = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    result = (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    if stat.S_ISLNK(info.st_mode):
        target = path.stat()
        result += (str(path.resolve()), target.st_dev, target.st_ino, target.st_size,
                   target.st_mtime_ns, target.st_ctime_ns)
    return result


def _signature(paths):
    return tuple(_version(path) for path in paths)


def _environment():
    return tuple(sorted((k, v) for k, v in os.environ.items()
                        if k.startswith("GIT_") or k in {"HOME", "XDG_CONFIG_HOME"}))


def _configuration_dependencies(repo):
    git = repo / ".git"
    # Linked/orphan worktrees retain the existing verified fallback unchanged.
    if not git.is_dir() or git.is_symlink():
        return None
    home = Path.home()
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config")))
    config_files = [git / "config", git / "config.worktree", Path("/etc/gitconfig"),
                    home / ".gitconfig", xdg / "git/config"]
    config_files.extend(Path(os.environ[key]).expanduser()
                        for key in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM") if os.environ.get(key))
    # Vendor Git builds may use a prefix fallback when /etc/gitconfig is
    # absent. Bind that missing path as well as discovered actual origins.
    prefix = subprocess.run(["git", "--exec-path"], cwd=repo,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5)
    if prefix.returncode or not Path(prefix.stdout.strip()).is_absolute():
        return None
    config_files.append(Path(prefix.stdout.strip()).parent.parent / "share/git-core/gitconfig")
    result = subprocess.run(["git", "config", "--show-origin", "--name-only", "--null", "--list"],
        cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5)
    if result.returncode:
        return None
    parts = result.stdout.split("\0")
    if parts and parts[-1] == "":
        parts.pop()
    if len(parts) % 2:
        return None
    config = []
    has_excludes = False
    for origin, name in zip(parts[::2], parts[1::2]):
        # Includes can add previously nonexistent origins. Until those forms
        # have their own complete dependency proof, perform the original check.
        if not origin.startswith("file:") or name.lower().startswith(("include.", "includeif.")):
            return None
        path = Path(origin[5:]).expanduser()
        path = path if path.is_absolute() else repo / path
        config.append(path)
        has_excludes |= name.lower() == "core.excludesfile"
    config_files = tuple(dict.fromkeys([*config_files, *config]))
    config_before = _signature(config_files)
    # Discovery is not a proof. Read again after the complete origin set is
    # bound, so a configuration change during discovery cannot certify a batch.
    confirmed = subprocess.run(["git", "config", "--show-origin", "--name-only", "--null", "--list"],
        cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5)
    if confirmed.returncode or confirmed.stdout != result.stdout:
        return None
    dependencies = [git / "config", git / "config.worktree", git / "index", git / "HEAD",
                    git / "info/exclude", xdg / "git/ignore", *config_files]
    for key in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"):
        if os.environ.get(key):
            dependencies.append(Path(os.environ[key]).expanduser())
    if has_excludes:
        value = subprocess.run(["git", "config", "--path", "--get", "core.excludesfile"],
            cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=5)
        if value.returncode or not value.stdout.strip():
            return None
        path = Path(value.stdout.strip()).expanduser()
        dependencies.append(path if path.is_absolute() else repo / path)
    if config_before != _signature(config_files):
        return None
    return tuple(dict.fromkeys(dependencies))


class IgnoreProofs:
    def __init__(self):
        self.entries = {}

    def prepare(self, repo, paths):
        self.entries.clear()
        if len(paths) > MAX_PATHS:
            return None
        try:
            environment = _environment()
            common = _configuration_dependencies(repo)
            if common is None:
                return None
            proof = {}
            for path in paths:
                parent = path.parent
                ignore_files = []
                while True:
                    parent.relative_to(repo)
                    ignore_files.extend((parent / ".gitignore", parent / ".git"))
                    if parent == repo:
                        break
                    parent = parent.parent
                dependencies = (*common, *ignore_files)
                proof[path] = (dependencies, _signature(dependencies))
            return environment, proof
        except (OSError, ValueError, subprocess.SubprocessError, UnicodeError):
            return None

    def remember(self, prepared, ignored):
        if prepared is None:
            return
        environment, proofs = prepared
        if environment != _environment():
            return
        for path, (dependencies, before) in proofs.items():
            try:
                if before == _signature(dependencies):
                    self.entries[path] = (path in ignored, environment, dependencies, before)
            except OSError:
                continue

    def lookup(self, path):
        proof = self.entries.get(path)
        if proof is None:
            return None
        ignored, environment, dependencies, expected = proof
        try:
            if environment == _environment() and expected == _signature(dependencies):
                return ignored
        except OSError:
            pass
        self.entries.pop(path, None)
        return None
