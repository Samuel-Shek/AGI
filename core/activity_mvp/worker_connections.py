"""Thread-owned SQLite handles with distinct slots for nested pipeline work."""
from functools import wraps
import threading
import sqlite3
import time


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
        deadline = time.monotonic() + self.wait_seconds
        waiting = False
        try:
            while True:
                try:
                    return operation()
                except sqlite3.OperationalError as exc:
                    code = getattr(exc, "sqlite_errorcode", 0)
                    if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                        raise
                    if self.stop_check() or time.monotonic() >= deadline:
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
