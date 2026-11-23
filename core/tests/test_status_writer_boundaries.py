"""Status filesystem work must leave the real document consumer able to write."""
import contextlib
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from activity_mvp import daemon, ha_automation, quality_advice, stability, storage
from activity_mvp.document_changes import _adjust_document_change_pending
from activity_mvp.worker_connections import WorkerConnections


class BoundaryReached(Exception):
    pass


class DefaultsFailure(Exception):
    pass


SESSION = {"state": "unlocked", "locked": False, "on_console": True,
           "capture_allowed": True, "source": "fixture"}
LAUNCH = {"loaded": False, "running": False, "pid": "", "last_exit_code": ""}
READERS = ((ha_automation, "ha_automation_status"),
           (ha_automation, "validate_ha_automation"),
           (quality_advice, "quality_advice_status"),
           (quality_advice, "validate_quality_advice"))


class StatusWriterBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.database = self.root / "activity.sqlite"
        self.home = patch.object(Path, "home", return_value=self.root)
        self.home.start()
        self.addCleanup(self.home.stop)
        seed = storage.ensure_ready(self.database)
        daemon.ensure_daemon_defaults(seed)
        seed.close()

    def consumer_write(self):
        pool = WorkerConnections(self.database, storage.connect, storage.connect_readonly)
        pool.wait_seconds = 0.15  # Only this synthetic contention window is scaled.
        conn = pool.connect("pending")
        try:
            result = _adjust_document_change_pending(conn, 1, expected_epoch=0)
            conn.commit()
            return {"success": True, "pending": result}
        except sqlite3.OperationalError as exc:
            conn.rollback()
            return {"success": False, "code": exc.sqlite_errorcode,
                    "retry": getattr(exc, "_pam_sqlite_retry", None)}
        finally:
            pool.close()

    def concurrent_boundary(self, invoke, substitutes):
        reached, release = threading.Event(), threading.Event()
        state = {}

        def writer():
            conn = storage.connect(self.database)

            def boundary(*args, **kwargs):
                state["in_transaction"] = conn.in_transaction
                reached.set()
                if not release.wait(5):
                    raise TimeoutError("test boundary not released")
                raise BoundaryReached()

            try:
                with contextlib.ExitStack() as stack:
                    for module, name, replacement in substitutes(boundary):
                        stack.enter_context(patch.object(module, name, replacement))
                    invoke(conn)
                state["unexpected_return"] = True
            except BoundaryReached:
                state["boundary_exit"] = True
            except BaseException as exc:
                state["unexpected_exception"] = repr(exc)
            finally:
                if conn.in_transaction:
                    conn.rollback()
                conn.close()
                reached.set()

        self.assertTrue(self.consumer_write()["success"])
        thread = threading.Thread(target=writer)
        thread.start()
        try:
            self.assertTrue(reached.wait(10), state)
            self.assertIn("in_transaction", state, state)
            during = self.consumer_write()
        finally:
            release.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertTrue(state.get("boundary_exit"), state)
        self.assertTrue(self.consumer_write()["success"])
        self.assertTrue(during["success"], {"consumer": during, "status": state})

    def status_substitutes(self, point, boundary):
        def residue(_root):
            return boundary() if point == "early" else 0

        def soak_residue(_root):
            return boundary() if point == "late" else 0

        # These are OS/private-dependency observations, not SQLite results.
        # Real status/default/schema/pause/session/HA/quality bodies still run.
        return [(daemon, "session_state", lambda _root: dict(SESSION)),
                (daemon, "_launch_agent_status", lambda: dict(LAUNCH)),
                (daemon, "_dashboard_health", lambda *a, **k: {}),
                (daemon, "_runtime_identity", lambda *a, **k: {}),
                (daemon, "_hygiene_cleanup_snapshot", lambda *a, **k: {}),
                (daemon, "_attention_items", lambda *a, **k: []),
                (ha_automation, "validate_ha_automation",
                 lambda *a, **k: {"ready": False, "summary": "fixture"}),
                (quality_advice, "validate_quality_advice",
                 lambda *a, **k: {"ready": False, "summary": "fixture"}),
                (daemon, "count_raw_residue", residue),
                (stability, "_raw_residue_count", soak_residue)]

    def test_changed_session_does_not_hold_writer_at_early_directory_read(self):
        self.concurrent_boundary(
            lambda conn: daemon.get_status(conn, self.root),
            lambda boundary: self.status_substitutes("early", boundary))
        check = storage.connect_readonly(self.database)
        try:
            self.assertEqual(check.execute(
                "SELECT value FROM daemon_state WHERE key='session_state'"
            ).fetchone()[0], "unlocked")
            self.assertEqual(check.execute(
                "SELECT count(*) FROM session_transition_evidence"
            ).fetchone()[0], 1)
        finally:
            check.close()

    def test_same_session_defaults_do_not_hold_writer_at_late_soak_directory_read(self):
        seed = storage.connect(self.database)
        stability.record_session_transition_evidence(seed, SESSION)
        daemon._set_state_if_changed(seed, "locked", "false")
        daemon._set_state_if_changed(seed, "session_state", "unlocked")
        seed.commit()
        seed.close()
        self.concurrent_boundary(
            lambda conn: daemon.get_status(conn, self.root),
            lambda boundary: self.status_substitutes("late", boundary))

    def test_status_and_nested_validate_release_own_defaults_before_dependencies(self):
        for module, name in READERS:
            with self.subTest(reader=name):
                self.concurrent_boundary(
                    lambda conn: getattr(module, name)(conn, self.root),
                    lambda boundary: [(module, "_hermes_executable", boundary)])

    def test_existing_caller_transaction_is_not_committed_by_any_status_reader(self):
        for module, name in READERS:
            with self.subTest(reader=name):
                conn = storage.connect(self.database)
                conn.execute("INSERT INTO daemon_state VALUES('caller_marker','pending','fixture')")
                try:
                    with patch.object(module, "_hermes_executable", side_effect=BoundaryReached):
                        with self.assertRaises(BoundaryReached):
                            getattr(module, name)(conn, self.root)
                    self.assertTrue(conn.in_transaction)
                    other = storage.connect_readonly(self.database)
                    try:
                        self.assertIsNone(other.execute(
                            "SELECT value FROM daemon_state WHERE key='caller_marker'"
                        ).fetchone())
                    finally:
                        other.close()
                    conn.rollback()
                    self.assertIsNone(conn.execute(
                        "SELECT value FROM daemon_state WHERE key='caller_marker'"
                    ).fetchone())
                finally:
                    conn.close()

    def test_failed_own_defaults_roll_back_without_blocking_consumer(self):
        for module, name in READERS:
            with self.subTest(reader=name):
                conn = storage.connect(self.database)

                def fail_defaults(handle):
                    handle.execute("INSERT INTO daemon_state VALUES('partial_defaults','pending','fixture')")
                    raise DefaultsFailure()

                ensure_name = ("ensure_ha_automation_defaults" if module is ha_automation
                               else "ensure_quality_advice_defaults")
                try:
                    with patch.object(module, ensure_name, side_effect=fail_defaults):
                        with self.assertRaises(DefaultsFailure):
                            getattr(module, name)(conn, self.root)
                    self.assertTrue(self.consumer_write()["success"])
                    self.assertFalse(conn.in_transaction)
                    self.assertIsNone(conn.execute(
                        "SELECT value FROM daemon_state WHERE key='partial_defaults'"
                    ).fetchone())
                finally:
                    conn.rollback()
                    conn.close()

    def test_failed_defaults_do_not_roll_back_callers_transaction(self):
        for module, name in READERS:
            with self.subTest(reader=name):
                conn = storage.connect(self.database)
                conn.execute("INSERT INTO daemon_state VALUES('caller_marker','pending','fixture')")

                def fail_defaults(handle):
                    handle.execute("INSERT INTO daemon_state VALUES('partial_defaults','pending','fixture')")
                    raise DefaultsFailure()

                ensure_name = ("ensure_ha_automation_defaults" if module is ha_automation
                               else "ensure_quality_advice_defaults")
                try:
                    with patch.object(module, ensure_name, side_effect=fail_defaults):
                        with self.assertRaises(DefaultsFailure):
                            getattr(module, name)(conn, self.root)
                    self.assertTrue(conn.in_transaction)
                    self.assertEqual(conn.execute(
                        "SELECT count(*) FROM daemon_state WHERE key IN ('caller_marker','partial_defaults')"
                    ).fetchone()[0], 2)
                    conn.rollback()
                    self.assertEqual(conn.execute(
                        "SELECT count(*) FROM daemon_state WHERE key IN ('caller_marker','partial_defaults')"
                    ).fetchone()[0], 0)
                finally:
                    conn.close()


if __name__ == "__main__":
    unittest.main()
