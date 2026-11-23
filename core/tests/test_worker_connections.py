from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from activity_mvp.document_changes import _consumer_exception_evidence
from activity_mvp.worker_connections import WorkerConnections


class WorkerConnectionRetryTests(unittest.TestCase):
    def pool(self):
        return WorkerConnections(None, None, None)

    def test_known_transient_extended_codes_retry_without_replaying_success(self):
        for name in ("SQLITE_BUSY_RECOVERY", "SQLITE_BUSY_TIMEOUT",
                     "SQLITE_LOCKED_SHAREDCACHE"):
            with self.subTest(code=name):
                error = sqlite3.OperationalError("private SQL/path")
                error.sqlite_errorcode = getattr(sqlite3, name)
                operation = mock.Mock(side_effect=[error, "committed-once"])
                waiting = []
                pool = self.pool()
                pool.on_wait = waiting.append
                with mock.patch("activity_mvp.worker_connections.time.sleep"):
                    self.assertEqual(pool.retry(operation), "committed-once")
                self.assertEqual(operation.call_count, 2)
                self.assertEqual(waiting, [True, False])

    def test_real_shared_cache_lock_recovers_after_reader_releases(self):
        uri = "file:agi-worker-test?mode=memory&cache=shared"
        reader = sqlite3.connect(uri, uri=True)
        writer = sqlite3.connect(uri, uri=True)
        try:
            reader.executescript("CREATE TABLE t(n INTEGER); INSERT INTO t VALUES(1),(2);")
            cursor = reader.execute("SELECT n FROM t")
            self.assertEqual(cursor.fetchone(), (1,))
            pool = self.pool()
            seen = []

            def release(waiting):
                seen.append(waiting)
                if waiting:
                    cursor.close()

            pool.on_wait = release
            pool.retry(lambda: writer.execute("UPDATE t SET n=n+10"))
            writer.commit()
            self.assertEqual(reader.execute("SELECT n FROM t ORDER BY n").fetchall(), [(11,), (12,)])
            self.assertEqual(seen, [True, False])
        finally:
            writer.close()
            reader.close()

    def test_real_wal_writer_contention_recovers_and_persistent_lock_expires(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "isolated.sqlite"
            owner = sqlite3.connect(database)
            try:
                owner.execute("PRAGMA journal_mode=WAL").fetchone()
                owner.executescript("CREATE TABLE t(n INTEGER); INSERT INTO t VALUES(1);")
                owner.execute("BEGIN IMMEDIATE")
                released = threading.Event()
                waiting = threading.Event()
                result = []

                def contender():
                    conn = sqlite3.connect(database, timeout=0.001)
                    pool = self.pool()
                    pool.wait_seconds = 1
                    pool.on_wait = lambda value: waiting.set() if value else None
                    try:
                        pool.retry(lambda: conn.execute("BEGIN IMMEDIATE"))
                        conn.execute("UPDATE t SET n=n+1")
                        conn.commit()
                        result.append("committed-once")
                    except Exception as error:
                        result.append(type(error).__name__)
                    finally:
                        conn.close()
                        released.set()

                thread = threading.Thread(target=contender)
                thread.start()
                try:
                    self.assertTrue(waiting.wait(2))
                    owner.rollback()
                    self.assertTrue(released.wait(3))
                finally:
                    owner.rollback()
                    thread.join(3)
                self.assertEqual(result, ["committed-once"])
                self.assertEqual(owner.execute("SELECT n FROM t").fetchone(), (2,))

                # A lock that never releases still fails, preserving the row
                # and the existing bounded fail-closed recovery contract.
                owner.execute("BEGIN IMMEDIATE")
                blocked = sqlite3.connect(database, timeout=0.001)
                try:
                    pool = self.pool()
                    pool.wait_seconds = 0.06
                    with self.assertRaises(sqlite3.OperationalError) as raised:
                        pool.retry(lambda: blocked.execute("BEGIN IMMEDIATE"))
                    self.assertEqual(raised.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)
                    self.assertEqual(_consumer_exception_evidence(raised.exception)["sqlite_retry"]["reason"], "deadline")
                    self.assertFalse(blocked.in_transaction)
                    self.assertEqual(owner.execute("SELECT n FROM t").fetchone(), (2,))
                finally:
                    blocked.close()
            finally:
                owner.rollback()
                owner.close()

    def test_real_stale_wal_cursor_requires_restart_and_preserves_snapshot(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "isolated.sqlite"
            reader = sqlite3.connect(database)
            writer = sqlite3.connect(database)
            try:
                reader.execute("PRAGMA journal_mode=WAL").fetchone()
                reader.executescript("CREATE TABLE t(n INTEGER); INSERT INTO t VALUES(1),(2);")
                cursor = reader.execute("SELECT n FROM t ORDER BY rowid")
                self.assertEqual(cursor.fetchone(), (1,))
                writer.execute("UPDATE t SET n=3 WHERE n=2")
                writer.commit()
                self.assertFalse(reader.in_transaction)
                pool = self.pool()
                operation = mock.Mock(side_effect=lambda: reader.execute("BEGIN IMMEDIATE"))
                with self.assertRaises(sqlite3.OperationalError) as raised:
                    pool.retry(operation)
                self.assertEqual(raised.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY_SNAPSHOT)
                self.assertEqual(operation.call_count, 1)
                evidence = _consumer_exception_evidence(raised.exception)
                self.assertEqual(evidence["sqlite_extended_code"], "SQLITE_BUSY_SNAPSHOT")
                self.assertEqual(evidence["sqlite_retry"]["reason"], "snapshot_requires_restart")
                # The retry helper did not discard caller state to make a write succeed.
                self.assertEqual(cursor.fetchone(), (2,))
                cursor.close()
                reader.rollback()
                reader.execute("BEGIN IMMEDIATE")
                reader.rollback()
            finally:
                writer.close()
                reader.close()

    def test_stop_deadline_and_unknown_code_keep_original_failure(self):
        cases = [(sqlite3.SQLITE_BUSY, True, 30, "stopping"),
                 (sqlite3.SQLITE_BUSY, False, 0, "deadline"),
                 (sqlite3.SQLITE_BUSY | (20 << 8), False, 30, "not_retryable")]
        for code, stopped, duration, reason in cases:
            with self.subTest(reason=reason):
                error = sqlite3.OperationalError("private SQL and /private/customer/document")
                error.sqlite_errorcode = code
                pool = self.pool()
                pool.stop_check = lambda: stopped
                pool.wait_seconds = duration
                operation = mock.Mock(side_effect=error)
                with self.assertRaises(sqlite3.OperationalError) as raised:
                    pool.retry(operation)
                self.assertIs(raised.exception, error)
                self.assertEqual(operation.call_count, 1)
                evidence = _consumer_exception_evidence(error)
                self.assertEqual(evidence["sqlite_retry"]["reason"], reason)
                self.assertEqual(evidence["sqlite_retry"]["attempts"], 1)
                self.assertNotIn("private", str(evidence))
                self.assertNotIn("customer", str(evidence))

    def test_retry_diagnostic_discards_unbounded_or_private_attributes(self):
        error = sqlite3.OperationalError("private SQL")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY_RECOVERY
        error._pam_sqlite_retry = {"reason": "deadline", "attempts": True,
                                  "elapsed_ms": 2147483648, "sql": "private SQL"}
        self.assertEqual(_consumer_exception_evidence(error)["sqlite_retry"], {"reason": "deadline"})
        error._pam_sqlite_retry = {"reason": "/private/customer/path"}
        self.assertNotIn("sqlite_retry", _consumer_exception_evidence(error))


if __name__ == "__main__":
    unittest.main()
