"""Controller-owned persistence over an isolated, pinned Hermes Kanban capsule.

No profile resolution, live board migration, plugin hooks or worker dispatch. All
writes hold a thread lock, an interprocess flock and an IMMEDIATE SQLite
transaction. The capsule's native task/run CAS lives in that same transaction.
This is a crash-consistency boundary, not an OS privilege boundary: only the
trusted controller may open this module/state directory.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import secrets
import sqlite3
import stat
import threading
import time
from pathlib import Path
from typing import Any

from .vendor import hermes_kanban as kernel

_FORMAT = "ai-ceo-state-v1"
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()
_PHASES = {"observed", "candidate", "triaged", "planned", "building", "verifying",
           "release_ready", "deploying", "measuring", "retained", "reverted",
           "no_change", "blocked", "paused", "cancelled", "effect_unknown"}
_EFFECT_TRANSITIONS = {
    "prepared": {"applying", "cancelled"},
    "applying": {"confirmed", "unknown"},
    "unknown": {"reconciled"},
    "confirmed": set(), "reconciled": set(), "cancelled": set(),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ceo_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ceo_tasks (
 task_id TEXT PRIMARY KEY REFERENCES tasks(id),
 dedupe_key TEXT NOT NULL UNIQUE,
 asset_id TEXT NOT NULL,
 phase TEXT NOT NULL,
 payload TEXT NOT NULL,
 payload_hash TEXT NOT NULL,
 attempt_id TEXT,
 lease_epoch INTEGER,
 grant_revision INTEGER,
 lease_until INTEGER,
 native_run_id INTEGER,
 reason TEXT,
 result TEXT,
 next_check_at TEXT,
 created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ceo_effects (
 effect_key TEXT PRIMARY KEY,
 task_id TEXT NOT NULL REFERENCES tasks(id),
 attempt_id TEXT NOT NULL,
 state TEXT NOT NULL,
 receipt TEXT,
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ceo_audit (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 occurred_at REAL NOT NULL,
 event TEXT NOT NULL,
 details TEXT NOT NULL,
 previous_hash TEXT NOT NULL,
 entry_hash TEXT NOT NULL
);
"""


def _json(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)
    if len(text.encode()) > 262144:
        raise ValueError("Structured record exceeds 256 KiB")
    return text


def _label(value: str, name: str, limit: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"Invalid {name}")
    if any(ord(c) < 32 for c in value):
        raise ValueError(f"Invalid control character in {name}")
    return value


def _integer(value: Any, name: str, minimum=0, maximum=2**53):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"Invalid {name}")
    return value


def _safe_file(path: Path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
        raise PermissionError("State files must be private regular files without aliases")
    if info.st_mode & 0o077:
        raise PermissionError("State files must not be readable by other users")


class Store:
    def __init__(self, state_dir: Path):
        requested = Path(state_dir).absolute()
        if requested.is_symlink():
            raise PermissionError("Symlink state directory is not permitted")
        self.state_dir = requested.resolve()
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.state_dir.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError("State directory must be owned by current user with mode 0700")
        self.path = self.state_dir / "kanban.db"
        self.db_path = self.path
        # Refuse an unrelated directory before even creating our lock file.
        marker_probe = self.state_dir / ".ai-ceo-state.json"
        if not marker_probe.exists() and any(p.name != ".writer.lock" for p in self.state_dir.iterdir()):
            raise PermissionError("Refusing to initialize inside an existing nonempty directory")
        with _LOCKS_GUARD:
            self._thread_lock = _LOCKS.setdefault(str(self.state_dir), threading.RLock())
        lock_path = self.state_dir / ".writer.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self._lock_fd = fd
        self._closed = False
        try:
            _safe_file(lock_path)
            with self._locked():
                marker = self.state_dir / ".ai-ceo-state.json"
                if not marker.exists():
                    if self.path.exists() or self.path.is_symlink():
                        raise PermissionError("Refusing to adopt or migrate an existing database")
                    # Do not initialize inside an unrelated nonempty directory.
                    if any(p.name != ".writer.lock" for p in self.state_dir.iterdir()):
                        raise PermissionError("New state directory must be empty")
                    mfd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    with os.fdopen(mfd, "w") as handle:
                        handle.write(_json({"format": _FORMAT}))
                        handle.flush()
                        os.fsync(handle.fileno())
                _safe_file(marker)
                if json.loads(marker.read_text()).get("format") != _FORMAT:
                    raise PermissionError("Unrecognized state ownership marker")
                if not self.path.exists() and not self.path.is_symlink():
                    db_fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    os.close(db_fd)
                _safe_file(self.path)
                self.conn = sqlite3.connect(str(self.path), isolation_level=None,
                                            check_same_thread=False, timeout=10)
                self.conn.row_factory = sqlite3.Row
                # Rollback journal avoids relying on WAL on the system SQLite.
                journal = self.conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
                if str(journal).lower() != "delete":
                    raise RuntimeError("Rollback journal mode required")
                self.conn.execute("PRAGMA synchronous=FULL")
                self.conn.execute("PRAGMA foreign_keys=ON")
                self.conn.execute("PRAGMA busy_timeout=10000")
                existing = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                names = {r[0] for r in existing}
                if names and "ceo_meta" not in names:
                    raise PermissionError("Refusing external database schema")
                if names:
                    row = self.conn.execute("SELECT value FROM ceo_meta WHERE key='format'").fetchone()
                    if row is None or json.loads(row[0]) != _FORMAT:
                        raise PermissionError("State schema version mismatch; no automatic migrations")
                else:
                    # Schema and marker commit atomically, including native tables.
                    self.conn.executescript("BEGIN IMMEDIATE;" + kernel.SCHEMA_SQL + _SCHEMA +
                        "INSERT INTO ceo_meta(key,value) VALUES ('format','\"" + _FORMAT + "\"');COMMIT;")
                check = self.conn.execute("PRAGMA quick_check").fetchone()[0]
                if check != "ok":
                    raise sqlite3.DatabaseError("Database integrity check failed")
        except BaseException:
            if hasattr(self, "conn"):
                self.conn.close()
            os.close(fd)
            self._closed = True
            raise

    @contextlib.contextmanager
    def _locked(self):
        with self._thread_lock:
            if self._closed:
                raise RuntimeError("Store is closed")
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)

    @contextlib.contextmanager
    def _write(self):
        with self._locked():
            with kernel.write_txn(self.conn):
                yield

    def close(self):
        if not self._closed:
            with self._locked():
                self.conn.close()
            os.close(self._lock_fd)
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _state_get(self, key, default=None):
        row = self.conn.execute("SELECT value FROM ceo_meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def state_get(self, key, default=None):
        with self._locked():
            return self._state_get(key, default)

    def _state_set(self, key, value):
        self.conn.execute("INSERT INTO ceo_meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                          (key, _json(value)))

    def state_set(self, key, value):
        if key == "format":
            raise ValueError("State format is immutable")
        with self._write():
            self._state_set(_label(key, "state key"), value)
            self._audit("state_changed", {"key": key, "value_hash": hashlib.sha256(_json(value).encode()).hexdigest()})

    def _audit(self, event, details):
        serialized = _json(details)
        prior = self.conn.execute("SELECT entry_hash FROM ceo_audit ORDER BY id DESC LIMIT 1").fetchone()
        previous_hash = prior[0] if prior else "0" * 64
        occurred = time.time()
        digest = hashlib.sha256(_json([occurred, event, serialized, previous_hash]).encode()).hexdigest()
        cursor = self.conn.execute("INSERT INTO ceo_audit(occurred_at,event,details,previous_hash,entry_hash) VALUES (?,?,?,?,?)",
                                  (occurred, event, serialized, previous_hash, digest))
        return cursor.lastrowid

    def append_audit(self, event: str, details: dict):
        with self._write():
            return self._audit(_label(event, "audit event"), details)

    def audit_list(self, limit=200):
        _integer(limit, "audit limit", 1, 10000)
        with self._locked():
            rows = self.conn.execute("SELECT * FROM ceo_audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [{**dict(r), "details": json.loads(r["details"])} for r in reversed(rows)]

    def verify_audit(self):
        with self._locked():
            previous = "0" * 64
            count = 0
            for r in self.conn.execute("SELECT * FROM ceo_audit ORDER BY id"):
                digest = hashlib.sha256(_json([r["occurred_at"], r["event"], r["details"], previous]).encode()).hexdigest()
                if r["previous_hash"] != previous or r["entry_hash"] != digest:
                    return {"ok": False, "checked": count, "failed_id": r["id"]}
                previous = digest
                count += 1
            return {"ok": True, "checked": count, "head": previous,
                    "boundary": "Detects changed retained entries; no external immutable anchor or truncation proof"}

    def _task(self, task_id):
        row = self.conn.execute("SELECT t.title,t.status,t.current_run_id,t.created_at AS native_created_at,c.* FROM ceo_tasks c JOIN tasks t ON t.id=c.task_id WHERE c.task_id=?", (task_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["id"] = result.pop("task_id")
        result["payload"] = json.loads(result["payload"])
        result["result"] = json.loads(result["result"]) if result["result"] else None
        return result

    def get_task(self, task_id):
        with self._locked():
            return self._task(task_id)

    def list_tasks(self):
        with self._locked():
            ids = [r[0] for r in self.conn.execute("SELECT task_id FROM ceo_tasks ORDER BY created_at,task_id")]
            return [self._task(tid) for tid in ids]

    def enqueue(self, dedupe_key, title, asset_id, payload):
        _label(dedupe_key, "dedupe key", 1000)
        _label(title, "title")
        _label(asset_id, "asset id", 100)
        if not isinstance(payload, dict):
            raise ValueError("Payload must be an object")
        encoded = _json(payload)
        with self._write():
            row = self.conn.execute("SELECT task_id FROM ceo_tasks WHERE dedupe_key=?", (dedupe_key,)).fetchone()
            if row:
                existing = self._task(row[0])
                if existing["asset_id"] != asset_id:
                    raise ValueError("Dedupe key is already bound to a different asset")
                return {**existing, "created": False}
            task_id = kernel.create_task(self.conn, title=title, body=None,
                workspace_kind="dir", workspace_path=str(self.state_dir / "not-an-execution-workspace"),
                created_by="ai-ceo-controller", idempotency_key=dedupe_key,
                session_id=None, project_id="", max_retries=2)
            # This record shares the SAME outer transaction as native create.
            self.conn.execute("INSERT INTO ceo_tasks(task_id,dedupe_key,asset_id,phase,payload,payload_hash,created_at) VALUES (?,?,?,?,?,?,?)",
                (task_id, dedupe_key, asset_id, "observed", encoded, hashlib.sha256(encoded.encode()).hexdigest(), time.time()))
            self._audit("task_enqueued", {"task_id": task_id, "asset_id": asset_id, "dedupe_hash": hashlib.sha256(dedupe_key.encode()).hexdigest()})
            return {**self._task(task_id), "created": True}

    def _revision(self, asset_id, fallback=None):
        return self._state_get("grant_revision:" + asset_id, self._state_get("grant_revision", fallback))

    def claim(self, task_id, grant_revision, epoch, ttl_seconds=300):
        _integer(grant_revision, "grant revision", 1)
        _integer(epoch, "epoch", 1)
        _integer(ttl_seconds, "TTL", 1, 3600)
        with self._write():
            task = self._task(task_id)
            if not task or task["phase"] not in {"observed", "candidate", "triaged", "planned"}:
                return None
            if self._state_get("epoch", epoch) != epoch or self._revision(task["asset_id"], grant_revision) != grant_revision:
                return None
            # Persist initial fencing values; subsequent mismatches are rejected.
            if self._state_get("epoch") is None:
                self._state_set("epoch", epoch)
            if self._revision(task["asset_id"]) is None:
                self._state_set("grant_revision:" + task["asset_id"], grant_revision)
            if self.conn.execute("SELECT 1 FROM ceo_effects WHERE task_id=? AND state IN ('applying','unknown')", (task_id,)).fetchone():
                return None
            if self.conn.execute("SELECT 1 FROM ceo_tasks c JOIN tasks t ON t.id=c.task_id WHERE c.asset_id=? AND t.status='running'", (task["asset_id"],)).fetchone():
                return None
            attempt_id = secrets.token_hex(24)
            native = kernel.claim_task(self.conn, task_id, ttl_seconds=ttl_seconds, claimer=attempt_id)
            if native is None:
                return None
            self.conn.execute("UPDATE ceo_tasks SET phase='building',attempt_id=?,lease_epoch=?,grant_revision=?,lease_until=?,native_run_id=? WHERE task_id=?",
                              (attempt_id, epoch, grant_revision, native.claim_expires, native.current_run_id, task_id))
            self._audit("task_claimed", {"task_id": task_id, "run_id": native.current_run_id, "epoch": epoch, "grant_revision": grant_revision})
            return self._task(task_id)

    def _valid_attempt(self, task, attempt_id, epoch=None, grant_revision=None):
        if not task or task["status"] != "running" or not task["attempt_id"]:
            return False
        expected_epoch = task["lease_epoch"]
        expected_revision = task["grant_revision"]
        return (secrets.compare_digest(str(attempt_id), task["attempt_id"])
                and (epoch is None or epoch == expected_epoch)
                and (grant_revision is None or grant_revision == expected_revision)
                and self._state_get("epoch") == expected_epoch
                and self._revision(task["asset_id"]) == expected_revision
                and task["lease_until"] > time.time()
                and task["current_run_id"] == task["native_run_id"])

    def complete(self, task_id, attempt_id, epoch, grant_revision, result):
        if not isinstance(result, dict):
            raise ValueError("Result must be an object")
        encoded = _json(result)
        with self._write():
            task = self._task(task_id)
            if not self._valid_attempt(task, attempt_id, epoch, grant_revision):
                self._audit("completion_rejected", {"task_id": task_id, "reason": "stale_or_invalid_attempt"})
                return False
            if self.conn.execute("SELECT 1 FROM ceo_effects WHERE task_id=? AND state IN ('prepared','applying','unknown')", (task_id,)).fetchone():
                self._audit("completion_rejected", {"task_id": task_id, "reason": "unresolved_effect"})
                return False
            if not kernel.complete_task(self.conn, task_id, result=encoded,
                expected_run_id=task["native_run_id"], fire_lifecycle_hook=False):
                return False
            # Completion of an execution is not proof that an application improved.
            phase = result.get("phase", "verifying") if result.get("task_kind") == "observation_analysis" else "verifying"
            if phase not in {"verifying", "no_change", "observed"}:
                raise ValueError("Observation analysis cannot assert release or improvement")
            self.conn.execute("UPDATE ceo_tasks SET phase=?,result=?,lease_until=NULL WHERE task_id=?", (phase, encoded, task_id))
            self._audit("execution_completed", {"task_id": task_id, "run_id": task["native_run_id"], "result_hash": hashlib.sha256(encoded.encode()).hexdigest()})
            return True

    def defer(self, task_id, phase, reason):
        if phase not in _PHASES or phase in {"building", "deploying", "retained", "reverted"}:
            raise ValueError("Invalid deferred phase")
        _label(reason, "reason", 4000)
        with self._write():
            task = self._task(task_id)
            if not task:
                raise KeyError(task_id)
            if task["status"] == "running":
                raise ValueError("Active attempt must be retired, not deferred")
            if task["phase"] == "effect_unknown" and phase != "effect_unknown":
                raise ValueError("Unknown effect requires reconciliation")
            self.conn.execute("UPDATE ceo_tasks SET phase=?,reason=? WHERE task_id=?", (phase, reason, task_id))
            # Terminal decisions require no synthetic successful execution run.
            native_status = "done" if phase == "no_change" else "archived" if phase == "cancelled" else "blocked"
            self.conn.execute("UPDATE tasks SET status=? WHERE id=?", (native_status, task_id))
            kernel._append_event(self.conn, task_id, "blocked" if native_status == "blocked" else "decision", {"phase": phase, "reason": reason})
            self._audit("task_deferred", {"task_id": task_id, "phase": phase, "reason": reason})

    def register_effect(self, effect_key, task_id, attempt_id):
        _label(effect_key, "effect key", 1000)
        with self._write():
            row = self.conn.execute("SELECT * FROM ceo_effects WHERE effect_key=?", (effect_key,)).fetchone()
            if row:
                if row["task_id"] != task_id or row["attempt_id"] != attempt_id:
                    raise PermissionError("Effect key belongs to another execution; never execute again")
                return {**dict(row), "created": False}
            if not self._valid_attempt(self._task(task_id), attempt_id):
                raise PermissionError("Effect requires a current unexpired attempt")
            now = time.time()
            self.conn.execute("INSERT INTO ceo_effects VALUES (?,?,?,'prepared',NULL,?,?)", (effect_key, task_id, attempt_id, now, now))
            self._audit("effect_prepared", {"effect_key_hash": hashlib.sha256(effect_key.encode()).hexdigest(), "task_id": task_id})
            return {"effect_key": effect_key, "task_id": task_id, "attempt_id": attempt_id, "state": "prepared", "created": True}

    def set_effect_state(self, effect_key, state, receipt=None):
        with self._write():
            row = self.conn.execute("SELECT * FROM ceo_effects WHERE effect_key=?", (effect_key,)).fetchone()
            if not row:
                raise KeyError(effect_key)
            if state not in _EFFECT_TRANSITIONS.get(row["state"], set()):
                raise ValueError("Forbidden effect transition; unknown effects cannot retry")
            if state in {"applying", "confirmed"} and not self._valid_attempt(self._task(row["task_id"]), row["attempt_id"]):
                raise PermissionError("Stale attempt cannot apply or confirm effects")
            if state in {"confirmed", "reconciled"} and (not isinstance(receipt, dict) or not receipt):
                raise ValueError("Confirmation or reconciliation requires an independent receipt")
            encoded = _json(receipt) if receipt is not None else None
            self.conn.execute("UPDATE ceo_effects SET state=?,receipt=?,updated_at=? WHERE effect_key=?", (state, encoded, time.time(), effect_key))
            self._audit("effect_transition", {"task_id": row["task_id"], "before": row["state"], "after": state})

    def effects_list(self):
        with self._locked():
            return [{**dict(row), "receipt": json.loads(row["receipt"]) if row["receipt"] else None}
                    for row in self.conn.execute("SELECT * FROM ceo_effects ORDER BY created_at,effect_key")]

    def _retire(self, task_id, reason):
        task = self._task(task_id)
        if not task or task["status"] != "running":
            return False
        self.conn.execute("UPDATE ceo_effects SET state='unknown',updated_at=? WHERE task_id=? AND state='applying'", (time.time(), task_id))
        self.conn.execute("UPDATE ceo_effects SET state='cancelled',updated_at=? WHERE task_id=? AND state='prepared'", (time.time(), task_id))
        unknown = self.conn.execute("SELECT 1 FROM ceo_effects WHERE task_id=? AND state='unknown'", (task_id,)).fetchone()
        phase = "effect_unknown" if unknown else "blocked"
        native_run = kernel._end_run(self.conn, task_id, outcome="interrupted", status="blocked", error=reason)
        self.conn.execute("UPDATE tasks SET status='blocked',claim_lock=NULL,claim_expires=NULL,worker_pid=NULL WHERE id=?", (task_id,))
        self.conn.execute("UPDATE ceo_tasks SET phase=?,reason=?,lease_until=NULL WHERE task_id=?", (phase, reason, task_id))
        kernel._append_event(self.conn, task_id, "blocked", {"reason": reason, "phase": phase}, run_id=native_run)
        self._audit("execution_retired", {"task_id": task_id, "phase": phase, "reason": reason})
        return True

    def retire(self, task_id, reason="controller_interrupt"):
        _label(reason, "reason", 4000)
        with self._write():
            return self._retire(task_id, reason)

    def recover(self, epoch):
        _integer(epoch, "epoch", 1)
        with self._write():
            current = self._state_get("epoch", 0)
            if epoch < current:
                raise ValueError("Epoch may not move backwards")
            tasks = [r[0] for r in self.conn.execute("SELECT id FROM tasks WHERE status='running'")]
            for tid in tasks:
                self._retire(tid, "controller_restart_requires_reconciliation")
            # Interrupted side effects can exist after application-specific bugs;
            # keep them unknown even if native task state is not running.
            self.conn.execute("UPDATE ceo_effects SET state='unknown',updated_at=? WHERE state='applying'", (time.time(),))
            self._state_set("epoch", epoch)
            self._audit("controller_recovered", {"epoch": epoch, "retired_tasks": tasks})
            return {"retired": len(tasks), "epoch": epoch}

    def snapshot(self):
        tasks = self.list_tasks()
        counts = {}
        for task in tasks:
            counts[task["phase"]] = counts.get(task["phase"], 0) + 1
        return {"task_counts": counts, "tasks": tasks, "effects": self.effects_list(),
                "audit_integrity": self.verify_audit(), "epoch": self.state_get("epoch")}

    def backup(self, destination):
        dest = Path(destination).absolute()
        if dest.exists() or dest.is_symlink():
            raise FileExistsError("Backup destination must be new; never overwrite")
        if dest.parent.resolve() == self.state_dir and dest.name in {"kanban.db", ".writer.lock", ".ai-ceo-state.json"}:
            raise ValueError("Reserved backup path")
        with self._locked():
            descriptor = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(descriptor)
            target = sqlite3.connect(str(dest))
            try:
                self.conn.backup(target)
                result = target.execute("PRAGMA integrity_check").fetchone()[0]
                if result != "ok":
                    raise sqlite3.DatabaseError("Backup failed integrity check")
            finally:
                target.close()
            return {"path": str(dest), "integrity": "ok", "bytes": dest.stat().st_size}
