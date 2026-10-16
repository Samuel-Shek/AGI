from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from activity_mvp.activity_confirmations import (
    activity_confirmation_revision,
    answer_activity_confirmation,
    list_activity_confirmation_cards,
)
from activity_mvp.document_changes import (
    AUTHORIZATION_LOCK_FILENAME,
    INTERACTIVE_AUTHORIZATION_LOCK_TIMEOUT_SECONDS,
    INTERACTIVE_WRITE_AUTHORIZATION_LOCK_TIMEOUT_SECONDS,
    document_authorization_lock,
)
from activity_mvp.retention import (
    AI_ATTRIBUTION_RETENTION_DAYS,
    OPERATIONAL_AUDIT_RETENTION_DAYS,
    OPERATIONAL_SESSION_RETENTION_DAYS,
    cleanup_ai_attribution_receipts,
    cleanup_operational_records,
    rolling_retention_cutoff,
)
from activity_mvp.storage import connect, init_schema


class _ExclusiveLeaseHolder:
    """Hold the authorization lease from another thread for a bounded window."""

    def __init__(self, lock_path: Path, hold_seconds: float) -> None:
        self._lock_path = lock_path
        self._hold_seconds = hold_seconds
        self._acquired = threading.Event()
        self._release = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        descriptor = os.open(self._lock_path, os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            self._acquired.set()
            self._release.wait(self._hold_seconds)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def __enter__(self) -> "_ExclusiveLeaseHolder":
        self._thread.start()
        if not self._acquired.wait(5):
            raise AssertionError("exclusive lease was never acquired")
        return self

    def __exit__(self, *_exc: object) -> None:
        self._release.set()
        self._thread.join(timeout=10)


class InteractiveLockTimeoutTests(unittest.TestCase):
    """An interactive caller must never wait on maintenance without a bound."""

    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        root = Path(self._tempdir.name)
        self.database_path = root / "activity.sqlite"
        self.conn = connect(self.database_path)
        init_schema(self.conn)
        self.lock_path = root / AUTHORIZATION_LOCK_FILENAME
        self.lock_path.touch(mode=0o600)
        os.chmod(self.lock_path, 0o600)

    def tearDown(self) -> None:
        self.conn.close()
        self._tempdir.cleanup()

    def test_default_lease_still_blocks_without_timeout(self) -> None:
        """Existing callers keep the original unbounded semantics."""
        with _ExclusiveLeaseHolder(self.lock_path, 0.6):
            started = time.monotonic()
            with document_authorization_lock(self.conn, exclusive=False):
                pass
            waited = time.monotonic() - started
        self.assertGreaterEqual(waited, 0.4, "default reader must wait the lease out")

    def test_card_surface_gives_up_and_reports_busy(self) -> None:
        """Cards must fail fast as `database_busy` instead of freezing the UI."""
        with _ExclusiveLeaseHolder(self.lock_path, 30):
            started = time.monotonic()
            with self.assertRaises(sqlite3.OperationalError) as caught:
                list_activity_confirmation_cards(self.conn, range_name="today")
            waited = time.monotonic() - started
        self.assertIn("database is locked", str(caught.exception).casefold())
        self.assertLess(
            waited,
            INTERACTIVE_AUTHORIZATION_LOCK_TIMEOUT_SECONDS + 2.0,
            "cards waited far past its budget",
        )

    def test_revision_probe_gives_up_and_reports_busy(self) -> None:
        with _ExclusiveLeaseHolder(self.lock_path, 30):
            with self.assertRaises(sqlite3.OperationalError):
                activity_confirmation_revision(self.conn, range_name="today")

    def test_answer_gives_up_without_writing(self) -> None:
        """A user write must fail closed, never leave a partial answer behind."""
        with _ExclusiveLeaseHolder(self.lock_path, 30):
            with self.assertRaises(sqlite3.OperationalError):
                answer_activity_confirmation(
                    self.conn,
                    item_key="missing-item",
                    answer="occurred",
                )
        self.assertFalse(
            self.conn.in_transaction,
            "a timed-out answer must not leave an open transaction",
        )

    def test_write_budget_exceeds_read_budget(self) -> None:
        """Losing a typed answer costs more than losing a page of cards."""
        self.assertGreater(
            INTERACTIVE_WRITE_AUTHORIZATION_LOCK_TIMEOUT_SECONDS,
            INTERACTIVE_AUTHORIZATION_LOCK_TIMEOUT_SECONDS,
        )

    def test_read_budget_outlasts_a_maintenance_pass(self) -> None:
        """A budget under the maintenance lease would fail every collision.

        One `cleanup_retention` pass holds the exclusive lease for roughly 1.9
        seconds on a live-sized database.  A shorter budget turns every
        collision into a forced manual refresh rather than a brief wait.
        """
        self.assertGreaterEqual(INTERACTIVE_AUTHORIZATION_LOCK_TIMEOUT_SECONDS, 2.5)

    def test_uncontended_reads_are_unaffected(self) -> None:
        payload = list_activity_confirmation_cards(self.conn, range_name="today")
        self.assertEqual(payload["status"], "ok")
        self.assertFalse(payload["raw_access"])


class OperationalRetentionTests(unittest.TestCase):
    """Tables outside the activity cascade must not grow without bound."""

    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        root = Path(self._tempdir.name)
        self.database_path = root / "activity.sqlite"
        self.conn = connect(self.database_path)
        init_schema(self.conn)
        self.lock_path = root / AUTHORIZATION_LOCK_FILENAME
        self.lock_path.touch(mode=0o600)

    def tearDown(self) -> None:
        self.conn.close()
        self._tempdir.cleanup()

    def _insert_session(self, key: str, started_at: str) -> None:
        self.conn.execute(
            "INSERT INTO sessions(id, started_at, ended_at, status) VALUES(?,?,?,?)",
            (key, started_at, started_at, "closed"),
        )

    def _insert_audit(self, key: str, at: str) -> None:
        self.conn.execute(
            "INSERT INTO audit_logs(id, at, action, target_type, target_id, detail) "
            "VALUES(?,?,?,?,?,?)",
            (key, at, "test_action", "test", key, "{}"),
        )

    def test_expired_rows_go_and_recent_rows_stay(self) -> None:
        old_session = rolling_retention_cutoff(
            OPERATIONAL_SESSION_RETENTION_DAYS + 3
        ).isoformat(timespec="seconds")
        new_session = rolling_retention_cutoff(0).isoformat(timespec="seconds")
        old_audit = rolling_retention_cutoff(
            OPERATIONAL_AUDIT_RETENTION_DAYS + 3
        ).isoformat(timespec="seconds")
        new_audit = rolling_retention_cutoff(1).isoformat(timespec="seconds")
        self._insert_session("s-old", old_session)
        self._insert_session("s-new", new_session)
        self._insert_audit("a-old", old_audit)
        self._insert_audit("a-new", new_audit)
        self.conn.commit()

        result = cleanup_operational_records(self.conn)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["deleted"]["sessions"], 1)
        self.assertEqual(result["deleted"]["audit_logs"], 1)
        remaining_sessions = {
            row[0] for row in self.conn.execute("SELECT id FROM sessions")
        }
        remaining_audits = {
            row[0] for row in self.conn.execute("SELECT id FROM audit_logs")
        }
        self.assertEqual(remaining_sessions, {"s-new"})
        self.assertEqual(remaining_audits, {"a-new"})

    def test_offsets_are_normalized_before_comparison(self) -> None:
        """A local `+08:00` stamp sorts after a UTC one that is actually later.

        Lexical comparison would keep `...T14:00:00+08:00` while dropping the
        UTC row five hours after it, so the cutoff must normalize both sides.
        """
        expired_utc = rolling_retention_cutoff(
            OPERATIONAL_SESSION_RETENTION_DAYS + 1
        )
        local_form = expired_utc.astimezone().isoformat(timespec="seconds")
        self.assertIn("+", local_form)
        self._insert_session("s-local-expired", local_form)
        self.conn.commit()

        result = cleanup_operational_records(self.conn)

        self.assertEqual(result["deleted"]["sessions"], 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
            0,
            "a local-offset stamp past the cutoff must still be collected",
        )

    def test_unparsable_timestamps_are_kept(self) -> None:
        self._insert_session("s-broken", "not-a-timestamp")
        self.conn.commit()

        result = cleanup_operational_records(self.conn)

        self.assertEqual(result["deleted"]["sessions"], 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1
        )

    def test_rerun_is_idempotent(self) -> None:
        self._insert_session(
            "s-old",
            rolling_retention_cutoff(
                OPERATIONAL_SESSION_RETENTION_DAYS + 3
            ).isoformat(timespec="seconds"),
        )
        self.conn.commit()
        cleanup_operational_records(self.conn)
        second = cleanup_operational_records(self.conn)
        self.assertEqual(second["deleted"]["sessions"], 0)
        self.assertEqual(second["deleted"]["audit_logs"], 0)

    def test_open_caller_transaction_is_a_backstop_only(self) -> None:
        """Skipping protects a caller's rollback boundary -- but must stay rare.

        Batched deletes commit as they go, so an open caller transaction cannot
        be worked through safely. Skipping is the right response, but it is only
        correct because the caller settles its transaction first; when that was
        not true, every production pass silently no-opped. The end-to-end test
        below is what holds that contract.
        """
        self._insert_session(
            "s-old",
            rolling_retention_cutoff(
                OPERATIONAL_SESSION_RETENTION_DAYS + 3
            ).isoformat(timespec="seconds"),
        )
        self.conn.commit()
        self.conn.execute("BEGIN IMMEDIATE")
        self._insert_session("s-pending", rolling_retention_cutoff(0).isoformat(timespec="seconds"))

        result = cleanup_operational_records(self.conn)

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "caller_transaction_open")
        self.assertTrue(self.conn.in_transaction, "caller transaction must survive")
        self.conn.rollback()
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM sessions WHERE id='s-pending'"
            ).fetchone()[0],
            0,
            "the caller must still be able to roll its own work back",
        )

    def test_cleanup_retention_actually_runs_this_pass(self) -> None:
        """End-to-end guard: the unit tests above all passed while production skipped.

        Only exercising the real `cleanup_retention` entry point catches a pass
        that silently no-ops inside it.
        """
        from activity_mvp.daemon import cleanup_retention

        self._insert_audit(
            "a-old",
            rolling_retention_cutoff(
                OPERATIONAL_AUDIT_RETENTION_DAYS + 5
            ).isoformat(timespec="seconds"),
        )
        self.conn.commit()

        result = cleanup_retention(self.conn, Path(self._tempdir.name))

        operational = result.get("operational_records") or {}
        self.assertEqual(
            operational.get("status"),
            "completed",
            f"cleanup_retention did not run the operational pass: {operational}",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM audit_logs WHERE id='a-old'"
            ).fetchone()[0],
            0,
            "the expired audit row survived a full retention pass",
        )

    def test_cleanup_does_not_take_the_authorization_lease(self) -> None:
        """Holding that lease here would extend the freeze this cleanup shortens."""
        self._insert_session(
            "s-old",
            rolling_retention_cutoff(
                OPERATIONAL_SESSION_RETENTION_DAYS + 3
            ).isoformat(timespec="seconds"),
        )
        self.conn.commit()
        with _ExclusiveLeaseHolder(self.lock_path, 30):
            started = time.monotonic()
            result = cleanup_operational_records(self.conn)
            waited = time.monotonic() - started
        self.assertEqual(result["deleted"]["sessions"], 1)
        self.assertLess(waited, 5.0, "cleanup must not queue behind the lease")


class AIAttributionReceiptRetentionTests(unittest.TestCase):
    """The private AI evidence log obeys the same bounded 90-day contract."""

    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.log_dir = Path(self._tempdir.name) / ".pam-ai-attribution"
        self.log_dir.mkdir(mode=0o700)
        os.chmod(self.log_dir, 0o700)
        self.now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self._tempdir.cleanup()

    def _write_log(self, name: str, records: list[object]) -> Path:
        path = self.log_dir / name
        lines = [
            json.dumps(record, separators=(",", ":"))
            if isinstance(record, dict)
            else str(record)
            for record in records
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    def test_expired_invalid_and_future_receipts_are_removed(self) -> None:
        recent = {
            "at": (self.now - timedelta(days=2)).isoformat(),
            "tool": "codex",
            "file_id": "file_recent",
        }
        old = {
            "at": (
                self.now - timedelta(days=AI_ATTRIBUTION_RETENTION_DAYS + 1)
            ).isoformat(),
            "tool": "codex",
            "file_id": "file_old",
        }
        future = {
            "at": (self.now + timedelta(minutes=6)).isoformat(),
            "tool": "codex",
            "file_id": "file_future",
        }
        active = self._write_log("edits.jsonl", [recent, old, "not-json"])
        rotated = self._write_log("edits.jsonl.1", [future])

        result = cleanup_ai_attribution_receipts(self.log_dir, now=self.now)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["files_rewritten"], 2)
        self.assertEqual(result["records_removed"], 3)
        self.assertEqual(result["records_retained"], 1)
        self.assertEqual(
            [json.loads(line) for line in active.read_text().splitlines()],
            [recent],
        )
        self.assertEqual(rotated.read_text(encoding="utf-8"), "")
        self.assertEqual(active.stat().st_mode & 0o777, 0o600)
        self.assertEqual(rotated.stat().st_mode & 0o777, 0o600)
        self.assertFalse(result["raw_access"])

    def test_unsafe_log_permissions_fail_closed_without_rewrite(self) -> None:
        path = self._write_log(
            "edits.jsonl",
            [{"at": (self.now - timedelta(days=100)).isoformat()}],
        )
        before = path.read_bytes()
        os.chmod(path, 0o644)

        result = cleanup_ai_attribution_receipts(self.log_dir, now=self.now)

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "receipt_cleanup_failed")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)

    def test_symlink_log_fails_closed_without_touching_target(self) -> None:
        target = Path(self._tempdir.name) / "outside.jsonl"
        target.write_text("outside\n", encoding="utf-8")
        os.chmod(target, 0o600)
        (self.log_dir / "edits.jsonl").symlink_to(target)

        result = cleanup_ai_attribution_receipts(self.log_dir, now=self.now)

        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["reason"], "receipt_cleanup_failed")
        self.assertEqual(target.read_text(encoding="utf-8"), "outside\n")


if __name__ == "__main__":
    unittest.main()
