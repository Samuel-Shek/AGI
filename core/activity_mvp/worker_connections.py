"""Thread-owned SQLite handles with distinct slots for nested pipeline work."""
from functools import wraps
import threading
import sqlite3
import time


# A stale WAL snapshot needs a transaction restart, not another execute on the
# same snapshot.  Unknown extended codes retain the existing fail-closed path.
_RETRYABLE_CODES = frozenset(
    getattr(sqlite3, name)
    for name in ("SQLITE_BUSY", "SQLITE_LOCKED", "SQLITE_BUSY_RECOVERY",
                 "SQLITE_BUSY_TIMEOUT", "SQLITE_LOCKED_SHAREDCACHE")
    if hasattr(sqlite3, name)
)


class _Handle:
    def __init__(self, connection, pool):
        self.connection, self.pool = connection, pool

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, *args, **kwargs):
        return self.pool.retry(lambda: self.connection.execute(*args, **kwargs))



class WorkerConnections:
    def __init__(self, database, writer, reader):
        self.database, self.writer, self.reader = database, writer, reader
        self.local = threading.local()
        self.stop_check = lambda: False
        self.on_wait = lambda waiting: None
        self.wait_seconds = 30.0

    def retry(self, operation):
        started = time.monotonic()
        deadline = started + self.wait_seconds
        waiting = False
        attempts = 0
        try:
            while True:
                try:
                    attempts += 1
                    return operation()
                except sqlite3.OperationalError as exc:
                    code = getattr(exc, "sqlite_errorcode", 0)
                    reason = ""
                    if type(code) is not int or code not in _RETRYABLE_CODES:
                        reason = ("snapshot_requires_restart"
                                  if code == getattr(sqlite3, "SQLITE_BUSY_SNAPSHOT", None)
                                  else "not_retryable")
                    elif self.stop_check():
                        reason = "stopping"
                    elif time.monotonic() >= deadline:
                        reason = "deadline"
                    if reason:
                        # Fixed labels and bounded counters only.  The consumer
                        # diagnostic must never need SQL or exception text to
                        # distinguish a timeout from a stopped/stale snapshot.
                        exc._pam_sqlite_retry = {
                            "reason": reason,
                            "attempts": min(attempts, 2147483647),
                            "elapsed_ms": max(0, min(2147483647, int(
                                (time.monotonic() - started) * 1000))),
                        }
                        raise
                    waiting = True
                    self.on_wait(True)
                    time.sleep(0.05)
        finally:
            if waiting:
                self.on_wait(False)

    def connect(self, slot, *, readonly=False):
        handles = getattr(self.local, "handles", None)
        if handles is None:
            handles = self.local.handles = {}
        key = (slot, readonly)
        if key not in handles:
            handles[key] = _Handle(self.retry(lambda: (self.reader if readonly else self.writer)(self.database, busy_timeout_seconds=0.05)), self)
        return handles[key]

    def release(self, conn):
        # Never carry a write transaction or WAL snapshot into the next item.
        if conn.in_transaction:
            conn.rollback()

    def close(self):
        for conn in getattr(self.local, "handles", {}).values():
            conn.close()
        self.local.handles = {}

    def thread(self, function):
        @wraps(function)
        def run(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            finally:
                self.close()
        return run
