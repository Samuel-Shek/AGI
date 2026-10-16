from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from activity_mvp import ha_automation, quality_advice
from activity_mvp.ha_source import get_package, revoke_package
from activity_mvp.lifecycle import delete_range
from activity_mvp.storage import ensure_ready


START = "2026-07-26T09:00:00+08:00"
END = "2026-07-26T09:30:00+08:00"
HA_NOW = datetime(2026, 7, 26, 10, 5, tzinfo=timezone(timedelta(hours=8)))


def _quality_report() -> dict:
    return {
        "status": "ok",
        "summary": "bounded quality report",
        "time_range": {"from": START, "to": END},
        "cleaning": {},
        "document_signals": {},
        "learning": {},
        "memory_write_health": {},
        "vlm": {},
        "attribution_health": {},
        "recommendations": [],
    }


def _ha_success() -> dict:
    return ha_automation._hermes_run_result(
        "completed",
        "",
        hermes_result_status="analysis_only",
        skipped_reason="analysis_only_no_memory_tool",
        hermes_result_json={"status": "analysis_only"},
    )


def _retention_admission(allowed: bool) -> dict:
    return {
        "allowed": allowed,
        "reason": "" if allowed else "retention_deadline_imminent",
        "deadline_at": "2026-07-27T00:00:00+00:00" if not allowed else "",
        "remaining_seconds": 20 * 60 if not allowed else None,
        "raw_access": False,
    }


class HermesHandoffAuthorizationLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        package_clock = patch(
            "activity_mvp.ha_source._package_access_now",
            return_value=HA_NOW.astimezone(timezone.utc),
        )
        package_clock.start()
        self.addCleanup(package_clock.stop)
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.db_path = self.root / "activity.sqlite"
        # Production startup deliberately forces HA automation off. These tests
        # exercise only the pre-existing handoff authorization lease internals,
        # so bypass the rollout gate without changing persisted settings.
        ha_rollout = patch(
            "activity_mvp.ha_automation._ha_auto_package_rollout_allowed",
            return_value=True,
        )
        ha_rollout.start()
        self.addCleanup(ha_rollout.stop)
        ha_gate = patch("activity_mvp.ha_automation._bool_setting", return_value=True)
        ha_gate.start()
        self.addCleanup(ha_gate.stop)
        conn = ensure_ready(self.db_path)
        try:
            ha_automation.configure_ha_automation(
                conn,
                self.root,
                enabled=True,
                interval_minutes=60,
                trigger_mode="direct_hermes",
            )
            quality_advice.configure_quality_advice(
                conn,
                self.root,
                enabled=True,
            )
        finally:
            conn.close()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _package_status(self, package_id: str) -> str:
        conn = ensure_ready(self.db_path)
        try:
            return str(get_package(conn, package_id)["source_status"])
        finally:
            conn.close()

    def _start_revoke(self, package_id: str):
        started = threading.Event()
        done = threading.Event()
        errors: list[BaseException] = []

        def run() -> None:
            conn = ensure_ready(self.db_path)
            try:
                started.set()
                revoke_package(conn, package_id, "handoff_authorization_test")
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                conn.close()
                done.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, started, done, errors

    def _start_delete_range(self):
        started = threading.Event()
        done = threading.Event()
        errors: list[BaseException] = []

        def run() -> None:
            conn = ensure_ready(self.db_path)
            try:
                started.set()
                delete_range(
                    conn,
                    START,
                    END,
                    reason="handoff_range_delete_test",
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                conn.close()
                done.set()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, started, done, errors

    def test_ha_handoff_holds_shared_lease_until_hermes_returns(self) -> None:
        handoff_started = threading.Event()
        release_handoff = threading.Event()
        errors: list[BaseException] = []
        result: dict = {}

        def blocked_hermes(*_args, **_kwargs):
            handoff_started.set()
            if not release_handoff.wait(5):
                raise AssertionError("test did not release HA handoff")
            return _ha_success()

        def run_handoff() -> None:
            conn = ensure_ready(self.db_path)
            try:
                result.update(
                    ha_automation.run_ha_automation_once(
                        conn,
                        self.root,
                        db_path=self.db_path,
                        now=HA_NOW,
                        force=True,
                    )
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
                handoff_started.set()
                release_handoff.set()
            finally:
                conn.close()

        with patch(
            "activity_mvp.ha_automation._run_hermes_command",
            side_effect=blocked_hermes,
        ):
            handoff_thread = threading.Thread(target=run_handoff, daemon=True)
            handoff_thread.start()
            self.assertTrue(handoff_started.wait(5), "HA handoff did not start")

            conn = ensure_ready(self.db_path)
            try:
                row = conn.execute(
                    "SELECT package_id FROM ha_automation_jobs ORDER BY updated_at DESC LIMIT 1"
                ).fetchone()
                package_id = str(row["package_id"])
            finally:
                conn.close()

            revoke_thread, revoke_started, revoke_done, revoke_errors = self._start_revoke(package_id)
            self.assertTrue(revoke_started.wait(5), "revocation did not start")
            try:
                self.assertFalse(
                    revoke_done.wait(0.25),
                    "revocation completed while HA handoff still held content",
                )
            finally:
                release_handoff.set()

            handoff_thread.join(5)
            revoke_thread.join(5)

        self.assertFalse(handoff_thread.is_alive())
        self.assertFalse(revoke_thread.is_alive())
        if errors:
            raise errors[0]
        if revoke_errors:
            raise revoke_errors[0]
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["triggered"])
        self.assertEqual(self._package_status(package_id), "revoked")

    def test_ha_retry_does_not_handoff_a_revoked_package(self) -> None:
        conn = ensure_ready(self.db_path)
        try:
            generated = ha_automation.run_ha_automation_once(
                conn,
                self.root,
                db_path=self.db_path,
                now=HA_NOW,
                force=True,
                trigger=False,
            )
            package_id = str(generated["package_id"])
            revoke_package(conn, package_id, "revoked_before_retry")

            with patch("activity_mvp.ha_automation._run_hermes_command") as hermes:
                blocked = ha_automation.run_ha_automation_once(
                    conn,
                    self.root,
                    db_path=self.db_path,
                    now=HA_NOW,
                    force=True,
                )

            hermes.assert_not_called()
            self.assertEqual(blocked["status"], "dead_letter")
            self.assertEqual(blocked["last_error"], "ha_source_unavailable")
            self.assertEqual(blocked["source_status"], "revoked")
            self.assertFalse(blocked["raw_access"])
            row = conn.execute(
                "SELECT status,dead_letter_reason FROM ha_automation_jobs WHERE package_id=?",
                (package_id,),
            ).fetchone()
            self.assertEqual(row["status"], "dead_letter")
            self.assertEqual(row["dead_letter_reason"], "ha_source_unavailable")
        finally:
            conn.close()

    def test_ha_and_quality_fail_closed_before_package_generation(self) -> None:
        blocked_admission = _retention_admission(False)
        conn = ensure_ready(self.db_path)
        try:
            with patch(
                "activity_mvp.ha_automation.retention_handoff_admission",
                return_value=blocked_admission,
            ), patch(
                "activity_mvp.quality_advice.retention_handoff_admission",
                return_value=blocked_admission,
            ), patch(
                "activity_mvp.ha_automation._run_hermes_command",
            ) as ha_hermes, patch(
                "activity_mvp.quality_advice._run_hermes_command",
            ) as quality_hermes, patch(
                "activity_mvp.ha_automation.generate_package",
            ) as ha_generate, patch(
                "activity_mvp.quality_advice.generate_package",
            ) as quality_generate, patch(
                "activity_mvp.quality_advice.quality_report",
            ) as quality_report:
                ha = ha_automation.run_ha_automation_once(
                    conn,
                    self.root,
                    db_path=self.db_path,
                    now=HA_NOW,
                    force=True,
                )
                quality = quality_advice.run_quality_advice_once(
                    conn,
                    self.root,
                    db_path=self.db_path,
                    start=START,
                    end=END,
                    force=True,
                )

            ha_hermes.assert_not_called()
            quality_hermes.assert_not_called()
            ha_generate.assert_not_called()
            quality_generate.assert_not_called()
            quality_report.assert_not_called()
            self.assertEqual(ha["status"], "deferred")
            self.assertEqual(quality["status"], "deferred")
            self.assertEqual(ha["reason"], "retention_deadline_imminent")
            self.assertEqual(quality["reason"], "retention_deadline_imminent")
            self.assertFalse(ha["raw_access"])
            self.assertFalse(quality["raw_access"])
            self.assertEqual(
                ha_automation.DEFAULT_HA_AUTO_TIMEOUT_SECONDS,
                20 * 60,
            )
            self.assertEqual(
                quality_advice.DEFAULT_QUALITY_ADVICE_TIMEOUT_SECONDS,
                20 * 60,
            )
        finally:
            conn.close()

    def test_ha_and_quality_recheck_retention_after_generation_before_transport_lease(
        self,
    ) -> None:
        allowed = _retention_admission(True)
        blocked = _retention_admission(False)
        conn = ensure_ready(self.db_path)
        try:
            with patch(
                "activity_mvp.ha_automation.retention_handoff_admission",
                side_effect=[allowed, blocked],
            ), patch(
                "activity_mvp.quality_advice.retention_handoff_admission",
                side_effect=[allowed, blocked],
            ), patch(
                "activity_mvp.quality_advice.quality_report",
                return_value=_quality_report(),
            ), patch(
                "activity_mvp.ha_automation._ensure_ha_authorization_lock",
            ) as ha_transport_lock, patch(
                "activity_mvp.quality_advice._ensure_ha_authorization_lock",
            ) as quality_post_generation_lock, patch(
                "activity_mvp.ha_automation._run_hermes_command",
            ) as ha_hermes, patch(
                "activity_mvp.quality_advice._run_hermes_command",
            ) as quality_hermes:
                ha = ha_automation.run_ha_automation_once(
                    conn,
                    self.root,
                    db_path=self.db_path,
                    now=HA_NOW,
                    force=True,
                )
                quality = quality_advice.run_quality_advice_once(
                    conn,
                    self.root,
                    db_path=self.db_path,
                    start=START,
                    end=END,
                    force=True,
                )

            self.assertEqual(ha["status"], "deferred")
            self.assertEqual(quality["status"], "deferred")
            self.assertTrue(ha["generated"])
            self.assertTrue(quality["generated"])
            self.assertTrue(ha["package_id"])
            self.assertTrue(quality["package_id"])
            ha_transport_lock.assert_not_called()
            quality_post_generation_lock.assert_not_called()
            ha_hermes.assert_not_called()
            quality_hermes.assert_not_called()
        finally:
            conn.close()

    def test_ha_and_quality_recheck_retention_after_transport_lease_wait(self) -> None:
        allowed = _retention_admission(True)
        blocked = _retention_admission(False)
        conn = ensure_ready(self.db_path)
        try:
            with patch(
                "activity_mvp.ha_automation.retention_handoff_admission",
                side_effect=[allowed, allowed, blocked],
            ), patch(
                "activity_mvp.quality_advice.retention_handoff_admission",
                side_effect=[allowed, allowed, allowed, allowed, blocked],
            ), patch(
                "activity_mvp.quality_advice.quality_report",
                return_value=_quality_report(),
            ), patch(
                "activity_mvp.ha_automation._run_hermes_command",
            ) as ha_hermes, patch(
                "activity_mvp.quality_advice._run_hermes_command",
            ) as quality_hermes:
                ha = ha_automation.run_ha_automation_once(
                    conn,
                    self.root,
                    db_path=self.db_path,
                    now=HA_NOW,
                    force=True,
                )
                quality = quality_advice.run_quality_advice_once(
                    conn,
                    self.root,
                    db_path=self.db_path,
                    start=START,
                    end=END,
                    force=True,
                )

            self.assertEqual(ha["status"], "deferred")
            self.assertEqual(quality["status"], "deferred")
            self.assertTrue(ha["generated"])
            self.assertTrue(quality["generated"])
            ha_hermes.assert_not_called()
            quality_hermes.assert_not_called()
        finally:
            conn.close()

    def test_quality_handoff_holds_shared_lease_until_hermes_returns(self) -> None:
        handoff_started = threading.Event()
        release_handoff = threading.Event()
        errors: list[BaseException] = []
        result: dict = {}

        def blocked_hermes(*_args, **_kwargs):
            handoff_started.set()
            if not release_handoff.wait(5):
                raise AssertionError("test did not release quality handoff")
            return {
                "status": "completed",
                "last_error": "",
                "advice": {
                    "status": "completed",
                    "summary": "safe advice",
                    "issues": [],
                    "recommendations": [],
                    "next_actions": [],
                },
            }

        def run_handoff() -> None:
            conn = ensure_ready(self.db_path)
            try:
                result.update(
                    quality_advice.run_quality_advice_once(
                        conn,
                        self.root,
                        db_path=self.db_path,
                        start=START,
                        end=END,
                        force=True,
                    )
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
                handoff_started.set()
                release_handoff.set()
            finally:
                conn.close()

        with patch(
            "activity_mvp.quality_advice.quality_report",
            return_value=_quality_report(),
        ), patch(
            "activity_mvp.quality_advice._run_hermes_command",
            side_effect=blocked_hermes,
        ):
            handoff_thread = threading.Thread(target=run_handoff, daemon=True)
            handoff_thread.start()
            self.assertTrue(handoff_started.wait(5), "quality handoff did not start")

            conn = ensure_ready(self.db_path)
            try:
                row = conn.execute(
                    "SELECT package_id FROM quality_advice_runs ORDER BY updated_at DESC LIMIT 1"
                ).fetchone()
                package_id = str(row["package_id"])
            finally:
                conn.close()

            revoke_thread, revoke_started, revoke_done, revoke_errors = self._start_revoke(package_id)
            self.assertTrue(revoke_started.wait(5), "revocation did not start")
            try:
                self.assertFalse(
                    revoke_done.wait(0.25),
                    "revocation completed while quality handoff still held content",
                )
            finally:
                release_handoff.set()

            handoff_thread.join(5)
            revoke_thread.join(5)

        self.assertFalse(handoff_thread.is_alive())
        self.assertFalse(revoke_thread.is_alive())
        if errors:
            raise errors[0]
        if revoke_errors:
            raise revoke_errors[0]
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["triggered"])
        self.assertEqual(self._package_status(package_id), "revoked")

    def test_quality_range_delete_cannot_run_before_final_result_commit(self) -> None:
        finalization_started = threading.Event()
        release_finalization = threading.Event()
        errors: list[BaseException] = []
        result: dict = {}
        real_upsert = quality_advice._upsert_run

        def pause_final_upsert(*args, **kwargs):
            if str(kwargs.get("status") or "") == "completed":
                finalization_started.set()
                if not release_finalization.wait(5):
                    raise AssertionError("test did not release quality finalization")
            return real_upsert(*args, **kwargs)

        def run_handoff() -> None:
            conn = ensure_ready(self.db_path)
            try:
                result.update(
                    quality_advice.run_quality_advice_once(
                        conn,
                        self.root,
                        db_path=self.db_path,
                        start=START,
                        end=END,
                        force=True,
                    )
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
                finalization_started.set()
                release_finalization.set()
            finally:
                conn.close()

        with patch(
            "activity_mvp.quality_advice.quality_report",
            return_value=_quality_report(),
        ), patch(
            "activity_mvp.quality_advice._run_hermes_command",
            return_value={
                "status": "completed",
                "last_error": "",
                "advice": {
                    "status": "completed",
                    "summary": "safe advice",
                    "issues": [],
                    "recommendations": [],
                    "next_actions": [],
                },
            },
        ), patch(
            "activity_mvp.quality_advice._upsert_run",
            side_effect=pause_final_upsert,
        ):
            handoff_thread = threading.Thread(target=run_handoff, daemon=True)
            handoff_thread.start()
            self.assertTrue(
                finalization_started.wait(5),
                "quality finalization did not start",
            )

            conn = ensure_ready(self.db_path)
            try:
                row = conn.execute(
                    "SELECT package_id FROM quality_advice_runs "
                    "ORDER BY updated_at DESC LIMIT 1"
                ).fetchone()
                package_id = str(row["package_id"])
            finally:
                conn.close()

            delete_thread, delete_started, delete_done, delete_errors = (
                self._start_delete_range()
            )
            self.assertTrue(delete_started.wait(5), "range delete did not start")
            try:
                self.assertFalse(
                    delete_done.wait(0.25),
                    "range delete crossed quality finalization's shared lease",
                )
            finally:
                release_finalization.set()

            handoff_thread.join(5)
            delete_thread.join(5)

        self.assertFalse(handoff_thread.is_alive())
        self.assertFalse(delete_thread.is_alive())
        if errors:
            raise errors[0]
        if delete_errors:
            raise delete_errors[0]
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["triggered"])
        conn = ensure_ready(self.db_path)
        try:
            row_count = conn.execute(
                "SELECT COUNT(*) FROM quality_advice_runs WHERE package_id=?",
                (package_id,),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(row_count, 0)
        self.assertEqual(self._package_status(package_id), "user_deleted")

    def test_quality_does_not_handoff_package_revoked_after_generation(self) -> None:
        real_generate = quality_advice.generate_package
        real_ensure_lock = quality_advice._ensure_ha_authorization_lock
        generated_package_id = ""

        def generate_then_revoke(*args, **kwargs):
            nonlocal generated_package_id
            generated = real_generate(*args, **kwargs)
            generated_package_id = str(generated["package"]["package_id"])
            revoke_conn = ensure_ready(self.db_path)
            try:
                revoke_package(
                    revoke_conn,
                    generated_package_id,
                    "revoked_before_quality_handoff",
                )
            finally:
                revoke_conn.close()
            return generated

        def ensure_lock_without_writer_transaction(conn) -> None:
            self.assertFalse(
                conn.in_transaction,
                "quality advice waited for the file lease while holding SQLite's writer slot",
            )
            real_ensure_lock(conn)

        conn = ensure_ready(self.db_path)
        try:
            with patch(
                "activity_mvp.quality_advice.quality_report",
                return_value=_quality_report(),
            ), patch(
                "activity_mvp.quality_advice.generate_package",
                side_effect=generate_then_revoke,
            ), patch(
                "activity_mvp.quality_advice._ensure_ha_authorization_lock",
                side_effect=ensure_lock_without_writer_transaction,
            ), patch(
                "activity_mvp.quality_advice._run_hermes_command",
            ) as hermes:
                blocked = quality_advice.run_quality_advice_once(
                    conn,
                    self.root,
                    db_path=self.db_path,
                    start=START,
                    end=END,
                    force=True,
                )

            hermes.assert_not_called()
            self.assertEqual(blocked["status"], "failed")
            self.assertEqual(blocked["error"], "quality_advice_source_unavailable")
            self.assertEqual(blocked["source_status"], "revoked")
            self.assertFalse(blocked["raw_access"])
            row = conn.execute(
                "SELECT status,last_error FROM quality_advice_runs WHERE package_id=?",
                (generated_package_id,),
            ).fetchone()
            self.assertIsNone(row)
        finally:
            conn.close()

    def test_quality_does_not_create_run_after_range_delete_wins_generation_race(
        self,
    ) -> None:
        real_generate = quality_advice.generate_package
        generated_package_id = ""

        def generate_then_delete(*args, **kwargs):
            nonlocal generated_package_id
            generated = real_generate(*args, **kwargs)
            generated_package_id = str(generated["package"]["package_id"])
            delete_conn = ensure_ready(self.db_path)
            try:
                delete_range(
                    delete_conn,
                    START,
                    END,
                    reason="delete_after_quality_generation",
                )
            finally:
                delete_conn.close()
            return generated

        conn = ensure_ready(self.db_path)
        try:
            with patch(
                "activity_mvp.quality_advice.quality_report",
                return_value=_quality_report(),
            ), patch(
                "activity_mvp.quality_advice.generate_package",
                side_effect=generate_then_delete,
            ), patch(
                "activity_mvp.quality_advice._run_hermes_command",
            ) as hermes:
                blocked = quality_advice.run_quality_advice_once(
                    conn,
                    self.root,
                    db_path=self.db_path,
                    start=START,
                    end=END,
                    force=True,
                )

            hermes.assert_not_called()
            self.assertEqual(blocked["status"], "failed")
            self.assertEqual(blocked["error"], "quality_advice_source_unavailable")
            self.assertEqual(blocked["source_status"], "user_deleted")
            self.assertFalse(blocked["raw_access"])
            row = conn.execute(
                "SELECT 1 FROM quality_advice_runs WHERE package_id=?",
                (generated_package_id,),
            ).fetchone()
            self.assertIsNone(row)
        finally:
            conn.close()

    def test_quality_generation_failure_cannot_resurrect_deleted_run_or_summary(
        self,
    ) -> None:
        conn = ensure_ready(self.db_path)
        try:
            with patch(
                "activity_mvp.quality_advice.quality_report",
                return_value=_quality_report(),
            ):
                seeded = quality_advice.run_quality_advice_once(
                    conn,
                    self.root,
                    db_path=self.db_path,
                    start=START,
                    end=END,
                    force=True,
                    trigger=False,
                )
            package_id = str(seeded["package_id"])
            run_key = quality_advice._run_key_for_range(START, END)
            real_upsert = quality_advice._upsert_run
            failure_upserts: list[dict] = []

            def delete_then_fail(*_args, **_kwargs):
                delete_conn = ensure_ready(self.db_path)
                try:
                    delete_range(
                        delete_conn,
                        START,
                        END,
                        reason="delete_wins_quality_generation_failure",
                    )
                finally:
                    delete_conn.close()
                raise RuntimeError("deterministic package generation failure")

            def capture_upsert(*args, **kwargs):
                if str(kwargs.get("status") or "") == "failed":
                    failure_upserts.append(dict(kwargs))
                return real_upsert(*args, **kwargs)

            with patch(
                "activity_mvp.quality_advice.quality_report",
                return_value=_quality_report(),
            ), patch(
                "activity_mvp.quality_advice.generate_package",
                side_effect=delete_then_fail,
            ), patch(
                "activity_mvp.quality_advice._upsert_run",
                side_effect=capture_upsert,
            ):
                failed = quality_advice.run_quality_advice_once(
                    conn,
                    self.root,
                    db_path=self.db_path,
                    start=START,
                    end=END,
                    force=True,
                )

            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["error"], "quality_advice_package_generation_failed")
            self.assertEqual(len(failure_upserts), 1)
            self.assertFalse(failure_upserts[0]["insert_if_missing"])
            self.assertEqual(failure_upserts[0]["report"], {})
            self.assertEqual(failure_upserts[0]["advice"], {})
            self.assertEqual(failure_upserts[0]["package_id"], package_id)
            self.assertIsNone(
                conn.execute(
                    "SELECT 1 FROM quality_advice_runs WHERE run_key=?",
                    (run_key,),
                ).fetchone()
            )
            self.assertEqual(
                conn.execute(
                    "SELECT value FROM daemon_state "
                    "WHERE key='quality_advice_last_report'"
                ).fetchone()[0],
                "",
            )
            self.assertEqual(self._package_status(package_id), "user_deleted")
        finally:
            conn.close()

    def test_quality_generation_failure_after_revoke_stores_no_report_summary(
        self,
    ) -> None:
        conn = ensure_ready(self.db_path)
        try:
            with patch(
                "activity_mvp.quality_advice.quality_report",
                return_value=_quality_report(),
            ):
                seeded = quality_advice.run_quality_advice_once(
                    conn,
                    self.root,
                    db_path=self.db_path,
                    start=START,
                    end=END,
                    force=True,
                    trigger=False,
                )
            package_id = str(seeded["package_id"])
            run_key = quality_advice._run_key_for_range(START, END)

            def revoke_then_fail(*_args, **_kwargs):
                revoke_conn = ensure_ready(self.db_path)
                try:
                    revoke_package(
                        revoke_conn,
                        package_id,
                        "revoke_wins_quality_generation_failure",
                    )
                finally:
                    revoke_conn.close()
                raise RuntimeError("deterministic package generation failure")

            with patch(
                "activity_mvp.quality_advice.quality_report",
                return_value=_quality_report(),
            ), patch(
                "activity_mvp.quality_advice.generate_package",
                side_effect=revoke_then_fail,
            ):
                failed = quality_advice.run_quality_advice_once(
                    conn,
                    self.root,
                    db_path=self.db_path,
                    start=START,
                    end=END,
                    force=True,
                )

            self.assertEqual(failed["status"], "failed")
            row = conn.execute(
                "SELECT package_id,status,summary_redacted,advice_json "
                "FROM quality_advice_runs WHERE run_key=?",
                (run_key,),
            ).fetchone()
            self.assertEqual(row["package_id"], package_id)
            self.assertEqual(row["status"], "failed")
            self.assertEqual(row["summary_redacted"], "")
            self.assertNotIn("bounded quality report", row["advice_json"])
            self.assertEqual(self._package_status(package_id), "revoked")
        finally:
            conn.close()

    def test_quality_rebuilds_stale_report_under_transport_lease(self) -> None:
        stale_report = _quality_report()
        stale_report["summary"] = "STALE_REPORT_MUST_NOT_LEAVE"
        captured_prompt = ""

        def capture_hermes(command, **_kwargs):
            nonlocal captured_prompt
            captured_prompt = str(command[-1])
            return {
                "status": "completed",
                "last_error": "",
                "advice": {
                    "status": "completed",
                    "summary": "",
                    "issues": [],
                    "recommendations": [],
                    "next_actions": [],
                },
            }

        conn = ensure_ready(self.db_path)
        try:
            with patch(
                "activity_mvp.quality_advice.quality_report",
                return_value=stale_report,
            ), patch(
                "activity_mvp.quality_advice._run_hermes_command",
                side_effect=capture_hermes,
            ):
                result = quality_advice.run_quality_advice_once(
                    conn,
                    self.root,
                    db_path=self.db_path,
                    start=START,
                    end=END,
                    force=True,
                )

            self.assertEqual(result["status"], "completed")
            self.assertNotIn("STALE_REPORT_MUST_NOT_LEAVE", captured_prompt)
            self.assertIn("活动 0 条", captured_prompt)
            row = conn.execute(
                "SELECT summary_redacted FROM quality_advice_runs WHERE package_id=?",
                (result["package_id"],),
            ).fetchone()
            self.assertNotIn("STALE_REPORT_MUST_NOT_LEAVE", row["summary_redacted"])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
