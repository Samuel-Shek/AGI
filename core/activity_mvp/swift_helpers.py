from __future__ import annotations

import fcntl
import os
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


HELPERS = {
    "sck_keyframe": "sck_keyframe.swift",
    "session_state": "session_state.swift",
    "accessibility_probe": "accessibility_probe.swift",
    "fsevents_watch": "fsevents_watch.swift",
    "vision_ocr": "vision_ocr.swift",
}

COMPILED_ONLY_HELPERS = {"vision_ocr"}


def ensure_all_helpers(root_dir: str | Path) -> dict[str, str]:
    return {name: str(ensure_helper(root_dir, name)) for name in HELPERS}


def helper_command(root_dir: str | Path, name: str) -> list[str]:
    if name in COMPILED_ONLY_HELPERS:
        return compiled_helper_command(root_dir, name)
    try:
        return [str(ensure_helper(root_dir, name))]
    except RuntimeError:
        source = _source_path(name)
        return ["xcrun", "swift", str(source)]


def compiled_helper_command(root_dir: str | Path, name: str) -> list[str]:
    """Validate a prebuilt helper without building or falling back to source.

    Runtime callers must use :func:`compiled_helper_lease` so a concurrent
    ``build-helpers`` cannot replace the executable between validation and
    spawn.  This command-only form is retained for diagnostics and tests.
    """
    with compiled_helper_lease(root_dir, name) as command:
        return command


@contextmanager
def compiled_helper_lease(root_dir: str | Path, name: str) -> Iterator[list[str]]:
    """Hold a shared lifecycle lock while a compiled helper may be spawned."""
    if name not in COMPILED_ONLY_HELPERS:
        raise ValueError(f"helper is not compiled-only: {name}")
    root = Path(root_dir)
    binary = root / "bin" / name
    lifecycle_path = _lifecycle_lock_path(root, name)
    try:
        lock = _open_existing_private_lock(lifecycle_path)
    except OSError as exc:
        raise RuntimeError(f"compiled helper lifecycle unavailable: {name}") from exc
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
        yield _validated_compiled_command(binary, _source_path(name), name)
    finally:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        lock.close()


def ensure_helper(root_dir: str | Path, name: str) -> Path:
    if name not in HELPERS:
        raise ValueError(f"unknown Swift helper: {name}")
    root = Path(root_dir)
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(bin_dir, 0o700)
    source = _source_path(name)
    binary = bin_dir / name
    lock_path = bin_dir / f".{name}.build.lock"
    lifecycle_path = _lifecycle_lock_path(root, name)
    with _open_or_create_private_lock(lock_path) as build_lock:
        # Fixed lock order: build exclusive, then lifecycle exclusive. Runtime
        # only takes lifecycle shared, so a manual build cannot deadlock a
        # running helper generation.
        fcntl.flock(build_lock.fileno(), fcntl.LOCK_EX)
        with _open_or_create_private_lock(lifecycle_path) as lifecycle_lock:
            fcntl.flock(lifecycle_lock.fileno(), fcntl.LOCK_EX)
            if _current_binary(binary, source):
                os.chmod(binary, 0o700)
                return binary
            descriptor, temp_name = tempfile.mkstemp(prefix=f".{name}.", dir=bin_dir)
            os.close(descriptor)
            temp_binary = Path(temp_name)
            temp_binary.unlink(missing_ok=True)
            try:
                completed = subprocess.run(
                    ["xcrun", "swiftc", str(source), "-o", str(temp_binary)],
                    check=False,
                    timeout=45,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                if completed.returncode != 0:
                    raise RuntimeError(completed.stderr.strip() or f"failed to build {name}")
                os.chmod(temp_binary, 0o700)
                os.replace(temp_binary, binary)
            finally:
                temp_binary.unlink(missing_ok=True)
    return binary


def _validated_compiled_command(binary: Path, source: Path, name: str) -> list[str]:
    try:
        bin_dir_info = binary.parent.lstat()
        binary_info = binary.lstat()
        source_info = source.stat()
    except OSError as exc:
        raise RuntimeError(f"compiled helper unavailable: {name}") from exc
    if (
        binary.parent.is_symlink()
        or not stat.S_ISDIR(bin_dir_info.st_mode)
        or bin_dir_info.st_uid != os.getuid()
        or stat.S_IMODE(bin_dir_info.st_mode) != 0o700
        or binary.is_symlink()
        or not stat.S_ISREG(binary_info.st_mode)
        or binary_info.st_uid != os.getuid()
        or stat.S_IMODE(binary_info.st_mode) != 0o700
        or binary_info.st_mtime < source_info.st_mtime
    ):
        raise RuntimeError(f"compiled helper validation failed: {name}")
    return [str(binary.resolve())]


def _lifecycle_lock_path(root: Path, name: str) -> Path:
    return root / "bin" / f".{name}.lifecycle.lock"


def _open_or_create_private_lock(path: Path):
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        return os.fdopen(descriptor, "a+b", buffering=0)
    except Exception:
        os.close(descriptor)
        raise


def _open_existing_private_lock(path: Path):
    descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise OSError("compiled helper lifecycle lock is not private")
        return os.fdopen(descriptor, "a+b", buffering=0)
    except Exception:
        os.close(descriptor)
        raise


def _current_binary(binary: Path, source: Path) -> bool:
    try:
        return (
            not binary.is_symlink()
            and stat.S_ISREG(binary.stat().st_mode)
            and binary.stat().st_mtime >= source.stat().st_mtime
        )
    except OSError:
        return False


def _source_path(name: str) -> Path:
    return Path(__file__).resolve().parents[1] / "tools" / HELPERS[name]
