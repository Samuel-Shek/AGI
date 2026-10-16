"""One immutable source-policy read per projection, never reused for actions."""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import errno
import os
import stat
import hashlib
import json
import select
import subprocess
import sys
import time
from types import FunctionType

CURRENT = ContextVar("agi_retirement_read_snapshot", default=None)
EVIDENCE_READ_SECONDS = 2.0


class EvidenceReadUnavailable(ValueError):
    """A bounded content read is unknown, distinct from policy withdrawal."""


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _known_change(error):
    return isinstance(error, (FileNotFoundError, NotADirectoryError)) or error.errno in {
        errno.ENOENT, errno.ENOTDIR, errno.ELOOP}


def _read_evidence(path):
    """The same complete content check in either process; never grant access."""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 2 * 1024 * 1024:
        return {"status": "changed"}
    identity = _identity(info)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if identity != _identity(os.fstat(fd)):
            return {"status": "changed"}
        data = bytearray()
        while len(data) <= 2 * 1024 * 1024:
            block = os.read(fd, min(65536, 2 * 1024 * 1024 + 1 - len(data)))
            if not block:
                break
            data.extend(block)
        if len(data) > 2 * 1024 * 1024:
            return {"status": "changed"}
    finally:
        os.close(fd)
    if identity != _identity(path.lstat()):
        return {"status": "changed"}
    return {"status": "ok", "identity": list(identity), "sha256": hashlib.sha256(data).hexdigest()}


def _evidence_worker():
    """Private parent/child pipe only; no bodies, persistent cache or new grant."""
    while True:
        line = sys.stdin.buffer.readline(4097)
        if not line:
            break
        if len(line) > 4096 or not line.endswith(b"\n"):
            break
        try:
            request = json.loads(line)
            result = _read_evidence(Path(request["path"]))
        except OSError as error:
            result = {"status": "changed" if _known_change(error) else "unavailable"}
        except (ValueError, TypeError, KeyError):
            result = {"status": "unavailable"}
        print(json.dumps(result, separators=(",", ":")), flush=True)


class _EvidenceReader:
    def __init__(self):
        self.deadline = time.monotonic() + EVIDENCE_READ_SECONDS
        self.process = subprocess.Popen(
            [sys.executable, "-I", "-S", "-B", str(Path(__file__).resolve()), "--evidence-reader"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        os.set_blocking(self.process.stdin.fileno(), False)
        os.set_blocking(self.process.stdout.fileno(), False)

    def read(self, path):
        request = json.dumps({"path": str(path)}, ensure_ascii=False).encode() + b"\n"
        if len(request) > 4096 or time.monotonic() >= self.deadline:
            raise EvidenceReadUnavailable("closure_evidence_read_unavailable")
        try:
            if os.write(self.process.stdin.fileno(), request) != len(request):
                raise EvidenceReadUnavailable("closure_evidence_read_unavailable")
            response = bytearray()
            while b"\n" not in response:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
                    raise EvidenceReadUnavailable("closure_evidence_read_unavailable")
                block = os.read(self.process.stdout.fileno(), 1025 - len(response))
                if not block or len(response) + len(block) > 1024:
                    raise EvidenceReadUnavailable("closure_evidence_read_unavailable")
                response.extend(block)
            if not response.endswith(b"\n"):
                raise EvidenceReadUnavailable("closure_evidence_read_unavailable")
            result = json.loads(response)
            if result.get("status") not in {"ok", "changed"}:
                raise EvidenceReadUnavailable("closure_evidence_read_unavailable")
            return result
        except (OSError, json.JSONDecodeError, AttributeError) as exc:
            raise EvidenceReadUnavailable("closure_evidence_read_unavailable") from exc

    def close(self):
        # Only this request's child is signaled, including a blocked open().
        self.process.stdin.close()
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=.2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=.5)
        finally:
            self.process.stdout.close()


class Snapshot:
    def __init__(self, sources, *, bounded_evidence=False):
        self.sources = sources
        self.policy_files = ((sources.ROOT/'policy.json', sources.ROOT/'retirements.json', sources.BRIDGE,
                              sources.ROOT/'conversation-authorization.json')
                             if hasattr(sources, 'ROOT') and hasattr(sources, 'BRIDGE') else ())
        self.file_binding = self._policy_binding()
        self.stopped = sources.company_stopped()
        self.policy = sources.policy()
        self.resolved = {}
        self.path_bindings = {}
        self.roots = tuple(self.resolve(x) for x in self.policy["company_roots"])
        self.company_paths = tuple(str(root) for root in self.roots)
        self.path_retirements = {}
        self.cache = {}
        self.evidence_cache = {}
        self.bounded_evidence = bounded_evidence
        self.evidence_reader = None
        self.classifier = None

    def resolve(self, path):
        text = str(path)
        if text not in self.resolved:
            value = Path(text).expanduser()
            if not value.is_absolute():
                value = Path.cwd() / value
            if value == value.parent:
                result = value
            else:
                parent = self.resolve(str(value.parent))
                result = parent.parent if value.name == ".." else parent / value.name
                try:
                    info = result.lstat()
                except (FileNotFoundError, NotADirectoryError):
                    info = None
                self.path_bindings[str(result)] = self._path_binding(result, info)
                if info is not None and stat.S_ISLNK(info.st_mode):
                    result = result.resolve()
            self.resolved[text] = result
        return self.resolved[text]

    @staticmethod
    def _path_binding(path, info):
        return None if info is None else (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode),
            str(Path(path).resolve()) if stat.S_ISLNK(info.st_mode) else None)

    def _policy_binding(self):
        result = []
        for path in self.policy_files:
            try:
                info = path.stat()
                result.append((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode))
            except FileNotFoundError:
                result.append(None)
        return tuple(result)

    def evidence_current(self, rows):
        try:
            return self._evidence_current(rows)
        except OSError as exc:
            if self.bounded_evidence:
                if _known_change(exc):
                    return False
                raise EvidenceReadUnavailable("closure_evidence_read_unavailable") from exc
            raise

    def _evidence_current(self, rows):
        for row in rows:
            path = Path(row["path"])
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > 2 * 1024 * 1024:
                return False
            identity = (str(path), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            if identity not in self.evidence_cache:
                if self.bounded_evidence:
                    if self.evidence_reader is None:
                        self.evidence_reader = _EvidenceReader()
                    result = self.evidence_reader.read(path)
                else:
                    result = _read_evidence(path)
                if result["status"] != "ok" or identity[1:] != tuple(result["identity"]):
                    return False
                if identity[1:] != _identity(path.lstat()):
                    return False
                self.evidence_cache[identity] = result["sha256"]
            if self.evidence_cache[identity] != row["sha256"]:
                return False
        return True

    def close(self):
        if self.evidence_reader is not None:
            self.evidence_reader.close()

    def retired(self, root, paths):
        if not self.stopped:
            return False
        if not isinstance(root, str) or not Path(root).is_absolute():
            return True
        key = (root, *paths)
        if key not in self.cache:
            self.cache[key] = any(self.company_path(x) for x in (root, *paths))
        return self.cache[key]

    @staticmethod
    def beneath(path, roots):
        return any(path == root or path.startswith(root.rstrip('/') + '/') for root in roots)

    def company_path(self, path):
        key = str(path)
        if key not in self.path_retirements:
            self.path_retirements[key] = self.beneath(str(self.resolve(key)), self.company_paths)
        return self.path_retirements[key]

    def classify(self, meta):
        """Reuse the installed classifier with immutable read dependencies.

        Its module and action checks remain untouched. No owner-specific source
        names, session registrations or policy configuration enter this code.
        """
        if self.classifier is None:
            original = self.sources.classify
            def within(path, root):
                try:
                    return self.beneath(str(self.resolve(path)), (str(self.resolve(root)),))
                except (OSError, RuntimeError, ValueError, TypeError):
                    return False
            self.classifier = FunctionType(original.__code__,
                {**original.__globals__, 'policy': lambda: self.policy, 'within': within},
                original.__name__, original.__defaults__, original.__closure__)
        return self.classifier(meta)

    def unchanged(self):
        if (self.file_binding != self._policy_binding() or self.stopped != self.sources.company_stopped()
                or self.policy != self.sources.policy()):
            return False
        for path, expected in self.path_bindings.items():
            try:
                info = os.lstat(path)
            except (FileNotFoundError, NotADirectoryError):
                info = None
            if expected != self._path_binding(path, info):
                return False
        return True


@contextmanager
def source_snapshot(sources, *, bounded_evidence=False):
    prior = CURRENT.get()
    if prior is not None:
        yield prior
        return
    view = Snapshot(sources, bounded_evidence=bounded_evidence)
    token = CURRENT.set(view)
    try:
        yield view
        if not view.unchanged():
            raise ValueError("source_policy_changed_during_projection")
    finally:
        try:
            view.close()
        finally:
            CURRENT.reset(token)


if __name__ == "__main__" and sys.argv[1:] == ["--evidence-reader"]:
    _evidence_worker()
