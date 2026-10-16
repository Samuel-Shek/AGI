from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from activity_mvp.activity_confirmations import list_activity_confirmations
from activity_mvp.confirmation_evidence import evidence_health
from activity_mvp.context_console import record_component_baseline
from activity_mvp.daemon import _attention_items, ensure_daemon_defaults
from activity_mvp.macos_probe import _display_attribution
from activity_mvp.lifecycle import delete_range, preview_delete_range
from activity_mvp.stability import (
    build_soak_report,
    record_resource_baseline,
    record_session_transition_evidence,
    record_soak_evidence_event,
    render_soak_report_markdown,
    session_transition_status,
)
from activity_mvp.storage import SCHEMA_VERSION, ensure_ready


class V039StabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.db_path = self.root / "activity.sqlite"
        self.conn = ensure_ready(self.db_path)
        ensure_daemon_defaults(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.tempdir.cleanup()

    def _evidence_attention_items(self, health: dict) -> list[dict]:
        return _attention_items(
            launch_agent={"loaded": True, "running": True},
            lock={"state": "unlocked"},
            raw_residue=0,
            last_error="",
            capture_paused=False,
            dashboard_health={"foreign_listener": False, "health_in_scope": True, "port_conflict": False},
            capture_freshness={"is_stale": False},
            coverage_health={"coverage_ledger_gap_count": 0, "displays": []},
            screen_recording_permission={"state": "granted"},
            confirmation_evidence_health=health,
        )

    def _insert_evidence_health_row(self, *, evidence_id: str, state: str, last_error: str = "") -> None:
        self.conn.execute(
            """
            INSERT INTO activity_confirmation_evidence_files(
              evidence_id,source_frame_id,captured_at,display_id,file_name,
              content_sha256,byte_size,state,backup_excluded,last_error,
              created_at,updated_at,deleted_at,delete_reason
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                evidence_id,
                "frame-test",
                "2026-07-21T08:00:00+00:00",
                "main",
                "",
                "",
                0,
                state,
                0,
                last_error,
                "2026-07-21T08:00:00+00:00",
                "2026-07-21T08:00:00+00:00",
                None,
                "test",
            ),
        )
        self.conn.commit()

    def test_schema_and_transition_evidence_are_content_free_and_deduplicated(self) -> None:
        self.assertEqual(SCHEMA_VERSION, 32)
        first = record_session_transition_evidence(
            self.conn,
            {
                "state": "unlocked",
                "on_console": True,
                "capture_allowed": True,
                "source": "session_state_helper",
                "reason": "explicit_unlocked_console",
            },
            observed_at="2026-07-13T00:00:00+00:00",
        )
        duplicate = record_session_transition_evidence(
            self.conn,
            {
                "state": "unlocked",
                "on_console": True,
                "capture_allowed": True,
                "source": "session_state_helper",
                "reason": "explicit_unlocked_console",
            },
            observed_at="2026-07-13T00:00:15+00:00",
        )
        unknown = record_session_transition_evidence(
            self.conn,
            {
                "state": "unknown",
                "on_console": None,
                "capture_allowed": False,
                "source": "session_state_helper",
                "reason": "session_state_timeout",
            },
            observed_at="2026-07-13T00:00:30+00:00",
        )
        self.conn.commit()

        count = self.conn.execute("SELECT COUNT(*) AS count FROM session_transition_evidence").fetchone()["count"]
        self.assertEqual(count, 2)
        self.assertEqual(first["state"], duplicate["state"])
        self.assertEqual(unknown["last_valid_state"], "unlocked")
        self.assertFalse(unknown["capture_allowed"])
        payload = session_transition_status(self.conn)
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(str(self.root), encoded)
        self.assertNotIn("user_id", encoded)
        self.assertFalse(payload["raw_access"])

    def test_schema27_migration_keeps_high_priority_explicit_claim_without_episode_v4_safe(self) -> None:
        migration_db = self.root / "schema27-explicit-claim.sqlite"
        original = ensure_ready(migration_db)
        original.execute(
            """
            INSERT INTO activity_confirmation_items(
              item_key,event_type,safe_observation,decision_question,why_uncertain,
              app_family,observed_start,observed_end,confidence,reason_codes,
              region_kind,actionability,source_activity_ids,deletion_group_ids,
              fingerprint,calibration_key,candidate_origin,priority,episode_id,
              notification_eligible,state,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "schema27-explicit-claim",
                "meeting",
                "今天完成一次计划内会议",
                "这次计划内会议是否真实发生？",
                "仅有本机清洗后的线索",
                "Obsidian",
                "2026-07-21T09:00:00+08:00",
                "2026-07-21T09:10:00+08:00",
                0.9,
                '["legacy_explicit_claim"]',
                "editor_body",
                "actionable",
                "[]",
                "[]",
                "schema27-fingerprint",
                "schema27-calibration",
                "explicit_claim",
                "normal",
                "",
                0,
                "pending",
                "2026-07-21T09:10:00+08:00",
                "2026-07-21T09:10:00+08:00",
            ),
        )
        original.executescript(
            "DROP TRIGGER pam_schema28_guard_insert;"
            "DROP TRIGGER pam_schema28_guard_update;"
            "DROP TRIGGER pam_schema30_guard_insert;"
            "DROP TRIGGER pam_schema30_guard_update;"
            "DROP TRIGGER pam_schema31_guard_insert;"
            "DROP TRIGGER pam_schema31_guard_update;"
            "DROP TRIGGER pam_schema32_guard_insert;"
            "DROP TRIGGER pam_schema32_guard_update;"
        )
        original.execute("UPDATE meta SET value='27' WHERE key='schema_version'")
        original.commit()
        original.close()

        migrated = ensure_ready(migration_db)
        try:
            payload = list_activity_confirmations(
                migrated,
                start="2026-07-21T00:00:00+08:00",
                end="2026-07-22T00:00:00+08:00",
            )
            row = migrated.execute(
                "SELECT candidate_origin,priority,episode_id,notification_eligible "
                "FROM activity_confirmation_items WHERE item_key='schema27-explicit-claim'"
            ).fetchone()
            schema = migrated.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()["value"]
        finally:
            migrated.close()

        self.assertEqual(schema, "32")
        self.assertEqual(tuple(row), ("explicit_claim", "high", "", 1))
        self.assertEqual((payload["status"], payload["contract_version"]), ("ok", 4))
        self.assertEqual(payload["pending_question_count"], 1)
        group = payload["question_groups"][0]
        self.assertEqual((group["candidate_origin"], group["priority"]), ("explicit_claim", "high"))
        self.assertEqual(group["episode_id"], "")
        self.assertTrue(group["notification_eligible"])

    def test_health_issues_expose_severity_scope_repair_and_recheck(self) -> None:
        items = _attention_items(
            launch_agent={"loaded": True, "running": True},
            lock={"state": "unknown"},
            raw_residue=0,
            last_error="",
            capture_paused=True,
            dashboard_health={"foreign_listener": True, "health_in_scope": False, "port_conflict": False},
            capture_freshness={"is_stale": False},
            coverage_health={"coverage_ledger_gap_count": 0, "displays": []},
            screen_recording_permission={"state": "unknown"},
        )
        by_code = {item["code"]: item for item in items}
        session = by_code["session_state_unknown"]
        self.assertEqual(session["severity"], "degraded")
        self.assertEqual(session["scope"], "session_state")
        self.assertEqual(session["remediation"]["action_id"], "refresh_session_state")
        self.assertEqual(session["recheck_result"]["status"], "pending")
        dashboard = by_code["orphan_dashboard_listener"]
        self.assertEqual(dashboard["severity"], "info")
        self.assertEqual(dashboard["scope"], "optional_dashboard")
        self.assertEqual(dashboard["recheck_result"]["status"], "not_applicable")

    def test_unavailable_confirmation_evidence_has_its_own_attention_item(self) -> None:
        items = self._evidence_attention_items(
            {"unavailable_count": 2, "delete_pending_count": 0, "error_count": 2}
        )

        self.assertEqual([item["code"] for item in items], ["confirmation_evidence_unavailable"])
        self.assertEqual(items[0]["title"], "待确认证据不可用")
        self.assertIn("2 条待确认证据不可用", items[0]["message"])
        self.assertNotIn("等待清理", items[0]["message"])

    def test_delete_pending_confirmation_evidence_has_its_own_attention_item(self) -> None:
        items = self._evidence_attention_items(
            {"unavailable_count": 0, "delete_pending_count": 3, "error_count": 3}
        )

        self.assertEqual([item["code"] for item in items], ["confirmation_evidence_delete_pending"])
        self.assertEqual(items[0]["title"], "待确认证据等待清理")
        self.assertIn("3 条待确认证据等待清理", items[0]["message"])
        self.assertNotIn("不可用", items[0]["message"])

    def test_confirmation_evidence_attention_counts_do_not_cross_contaminate(self) -> None:
        items = self._evidence_attention_items(
            {"unavailable_count": 2, "delete_pending_count": 5, "error_count": 99}
        )
        by_code = {item["code"]: item for item in items}

        self.assertEqual(
            set(by_code),
            {"confirmation_evidence_unavailable", "confirmation_evidence_delete_pending"},
        )
        self.assertIn("2 条待确认证据不可用", by_code["confirmation_evidence_unavailable"]["message"])
        self.assertNotIn("5 条", by_code["confirmation_evidence_unavailable"]["message"])
        self.assertIn("5 条待确认证据等待清理", by_code["confirmation_evidence_delete_pending"]["message"])
        self.assertNotIn("2 条", by_code["confirmation_evidence_delete_pending"]["message"])
        self.assertNotIn("99", json.dumps(items, ensure_ascii=False))

    def test_historical_evidence_errors_do_not_warn_when_actionable_bucket_is_clear(self) -> None:
        items = self._evidence_attention_items(
            {
                "unavailable_count": 7,
                "delete_pending_count": 4,
                "actionable": {"unavailable_count": 0, "delete_pending_count": 0},
                "historical": {"unavailable_count": 7, "delete_pending_count": 4},
            }
        )

        self.assertEqual(items, [])

    def test_nested_actionable_evidence_bucket_drives_current_warning(self) -> None:
        items = self._evidence_attention_items(
            {
                "unavailable_count": 9,
                "delete_pending_count": 8,
                "actionable": {"unavailable_count": 1, "delete_pending_count": 2},
                "historical": {"unavailable_count": 8, "delete_pending_count": 6},
            }
        )
        by_code = {item["code"]: item for item in items}

        self.assertIn("1 条待确认证据不可用", by_code["confirmation_evidence_unavailable"]["message"])
        self.assertIn("2 条待确认证据等待清理", by_code["confirmation_evidence_delete_pending"]["message"])

    def test_evidence_health_issues_do_not_increment_confirmation_v4_pending_counts(self) -> None:
        self._insert_evidence_health_row(
            evidence_id="confirm_ev_unavailable_00000000000000000000000000000001",
            state="unavailable",
            last_error="source_unavailable",
        )
        self._insert_evidence_health_row(
            evidence_id="confirm_ev_00000000000000000000000000000002",
            state="delete_pending",
        )

        health = evidence_health(self.conn)
        confirmation_list = list_activity_confirmations(self.conn, range_name="today")

        self.assertEqual((health["unavailable_count"], health["delete_pending_count"]), (1, 1))
        self.assertEqual(confirmation_list["contract_version"], 4)
        self.assertEqual(confirmation_list["pending_count"], 0)
        self.assertEqual(confirmation_list["pending_question_count"], 0)
        self.assertEqual(confirmation_list["pending_source_count"], 0)

    def test_soak_report_is_machine_readable_and_keeps_future_gates_pending(self) -> None:
        now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
        for offset in range(7):
            observed = (now - timedelta(days=offset)).isoformat()
            self.conn.execute(
                """
                INSERT INTO soak_capture_cycles(
                  id, observed_at, status, captured_count, skipped_count, error_count,
                  session_state, raw_residue_count, unresolved_raw_jobs
                ) VALUES(?, ?, 'ready', 1, 0, 0, 'unlocked', 0, 0)
                """,
                (f"cycle-{offset}", observed),
            )
        for index in reversed(range(20)):
            locked_at = now - timedelta(hours=index * 2 + 1)
            unlocked_at = locked_at + timedelta(minutes=1)
            record_session_transition_evidence(
                self.conn,
                {"state": "locked", "on_console": True, "capture_allowed": False, "reason": "explicit_locked"},
                observed_at=locked_at.isoformat(),
            )
            record_session_transition_evidence(
                self.conn,
                {"state": "unlocked", "on_console": True, "capture_allowed": True, "reason": "explicit_unlocked_console"},
                observed_at=unlocked_at.isoformat(),
            )
        for index in range(5):
            record_soak_evidence_event(
                self.conn,
                event_type="context_recovery",
                result="passed",
                minutes_saved=5,
                occurred_at=(now - timedelta(hours=index)).isoformat(),
            )
        for drill in ("pause", "delete", "permission_revoke", "permission_restore"):
            record_soak_evidence_event(
                self.conn,
                event_type=drill,
                result="passed",
                occurred_at=(now - timedelta(minutes=10)).isoformat(),
            )
        record_resource_baseline(
            self.conn,
            source_version="0.3.8",
            cpu_percent=5.0,
            rss_bytes=100_000_000,
            disk_bytes=100_000_000,
            recorded_at=(now - timedelta(days=7)).isoformat(),
        )
        for index, component in enumerate(("capture", "privacy", "storage", "context_fusion"), start=1):
            baseline = record_component_baseline(
                self.conn,
                component=component,
                version_ref=f"version_f100000{index}",
                reset_reason_code=f"{component}_changed",
            )
            self.assertEqual(baseline["generation"], 1)
        self.conn.executemany(
            "UPDATE context_component_baselines SET recorded_at=? WHERE component=?",
            [
                ((now - timedelta(days=7)).isoformat(), component)
                for component in ("capture", "privacy", "storage", "context_fusion")
            ],
        )
        self.conn.commit()

        report = build_soak_report(self.conn, self.root, end_at=(now + timedelta(minutes=1)).isoformat())
        self.assertEqual(report["window"]["observed_days"], 7)
        self.assertEqual(report["session_transitions"]["unlock_cycles"], 20)
        self.assertEqual(report["user_evidence"]["context_recovery_wins"], 5)
        self.assertFalse(report["raw_access"])
        self.assertIn(report["status"], {"failed", "in_progress"})
        gates = {gate["code"]: gate for gate in report["gates"]}
        self.assertEqual(gates["observation_window"]["status"], "passed")
        self.assertEqual(gates["control_drills"]["status"], "passed")
        self.assertEqual(gates["attribution_quality"]["status"], "pending")
        markdown = render_soak_report_markdown(report)
        self.assertIn("raw_access: `false`", markdown)
        self.assertNotIn(str(self.root), markdown)

    def test_soak_campaign_does_not_grade_pre_upgrade_history(self) -> None:
        now = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
        old = (now - timedelta(hours=1)).isoformat()
        self.conn.execute(
            """
            INSERT INTO soak_capture_cycles(
              id, observed_at, status, captured_count, skipped_count, error_count,
              session_state, raw_residue_count, unresolved_raw_jobs
            ) VALUES('old-cycle', ?, 'ready', 1, 0, 0, 'unlocked', 0, 0)
            """,
            (old,),
        )
        self.conn.execute(
            """
            INSERT INTO capture_coverage_ledger(
              id, session_id, started_at, ended_at, capture_mode,
              coverage_ratio, frames_sampled, frames_dropped, gap_count
            ) VALUES('old-coverage', 'old-session', ?, ?, 'multi_display', 1.0, 100, 0, 0)
            """,
            (old, old),
        )
        self.conn.execute(
            """
            INSERT INTO frame_signals(
              id, session_id, captured_at, display_id, app_name, deletion_group_id
            ) VALUES('old-frame', 'old-session', ?, 'main', 'Unknown', 'old-delete')
            """,
            (old,),
        )
        record_resource_baseline(
            self.conn,
            source_version="0.3.8",
            cpu_percent=1.0,
            rss_bytes=1,
            disk_bytes=1,
            recorded_at=now.isoformat(),
        )
        capture_baseline = record_component_baseline(
            self.conn,
            component="capture",
            version_ref="version_f2000001",
            reset_reason_code="capture_changed",
        )
        self.assertEqual(capture_baseline["generation"], 1)
        self.conn.execute(
            "UPDATE context_component_baselines SET recorded_at=? WHERE component='capture'",
            (now.isoformat(),),
        )
        self.conn.commit()

        report = build_soak_report(self.conn, self.root, end_at=(now + timedelta(minutes=1)).isoformat())
        gates = {gate["code"]: gate for gate in report["gates"]}
        self.assertEqual(report["window"]["campaign_started_at"], now.isoformat())
        self.assertEqual(report["window"]["observed_days"], 0)
        self.assertEqual(report["coverage"]["frames_sampled"], 0)
        self.assertEqual(report["attribution"]["frame_signal_count"], 0)
        self.assertEqual(gates["display_coverage"]["status"], "pending")
        self.assertEqual(gates["attribution_quality"]["status"], "pending")
        self.assertEqual(gates["resource_regression"]["status"], "unknown")
        self.assertEqual(report["status"], "in_progress")

    def test_resource_sampling_is_patchable_without_exposing_process_details(self) -> None:
        from activity_mvp.stability import maybe_record_resource_sample

        with patch("activity_mvp.stability._process_resource_usage", return_value=(3.0, 50_000_000)), patch(
            "activity_mvp.stability._bounded_directory_size", return_value=80_000_000
        ):
            payload = maybe_record_resource_sample(self.conn, self.root, force=True)
        self.assertEqual(payload["status"], "recorded")
        self.assertFalse(payload["raw_access"])
        self.assertNotIn("pid", payload)

    def test_display_attribution_uses_safe_window_fallback_and_overlap_scoring(self) -> None:
        main = _display_attribution(
            {
                "is_main": True,
                "visible_windows": [
                    {
                        "window_id": "wps-main",
                        "app_name": "WPS Office",
                        "bundle_id": "cn.wps.mac",
                        "title": "private title used only before privacy filtering",
                        "frame": {"x": 0, "y": 0, "width": 1000, "height": 700},
                        "overlap_ratio": 0.88,
                    }
                ],
            },
            frontmost={"app_name": "Unknown", "bundle_id": "", "window_title": ""},
        )
        self.assertEqual(main["app_name"], "WPS Office")
        self.assertEqual(main["reason_codes"], ["main_display_window_fallback"])
        self.assertGreaterEqual(main["confidence"], 0.72)

        secondary = _display_attribution(
            {
                "is_main": False,
                "visible_windows": [
                    {
                        "window_id": "browser-small",
                        "app_name": "Google Chrome",
                        "bundle_id": "com.google.Chrome",
                        "title": "tab",
                        "overlap_ratio": 0.10,
                    },
                    {
                        "window_id": "finder-large",
                        "app_name": "Finder",
                        "bundle_id": "com.apple.finder",
                        "title": "folder",
                        "overlap_ratio": 0.80,
                    },
                ],
            },
            frontmost={"app_name": "Codex"},
        )
        self.assertEqual(secondary["app_name"], "Finder")
        self.assertEqual(secondary["reason_codes"], ["display_window_match"])
        self.assertGreaterEqual(secondary["confidence"], 0.75)

    def test_user_deletion_cascades_time_locatable_soak_metrics(self) -> None:
        inside = "2026-07-13T08:00:00+00:00"
        outside = "2026-07-12T08:00:00+00:00"
        record_session_transition_evidence(
            self.conn,
            {"state": "locked", "on_console": True, "capture_allowed": False, "reason": "explicit_locked"},
            observed_at=inside,
        )
        self.conn.execute(
            """
            INSERT INTO soak_capture_cycles(
              id, observed_at, status, captured_count, skipped_count, error_count,
              session_state, raw_residue_count, unresolved_raw_jobs
            ) VALUES('inside-cycle', ?, 'locked', 0, 0, 0, 'locked', 0, 0)
            """,
            (inside,),
        )
        self.conn.execute(
            """
            INSERT INTO soak_capture_cycles(
              id, observed_at, status, captured_count, skipped_count, error_count,
              session_state, raw_residue_count, unresolved_raw_jobs
            ) VALUES('outside-cycle', ?, 'locked', 0, 0, 0, 'locked', 0, 0)
            """,
            (outside,),
        )
        self.conn.execute(
            """
            INSERT INTO soak_resource_samples(id, observed_at, cpu_percent, rss_bytes, disk_bytes, core_version)
            VALUES('inside-resource', ?, 1.0, 1, 1, '0.3.9')
            """,
            (inside,),
        )
        self.conn.execute(
            """
            INSERT INTO soak_evidence_events(id, occurred_at, event_type, result, minutes_saved, source)
            VALUES('inside-event', ?, 'context_recovery', 'passed', 1, 'user_confirmed')
            """,
            (inside,),
        )
        self.conn.execute(
            """
            INSERT INTO soak_daily_snapshots(day, generated_at, status, payload_json)
            VALUES('2026-07-13', ?, 'in_progress', '{"raw_access":false}')
            """,
            (inside,),
        )
        self.conn.commit()

        start = "2026-07-13T00:00:00+00:00"
        end = "2026-07-14T00:00:00+00:00"
        preview = preview_delete_range(self.conn, start, end)
        self.assertEqual(preview["cascade"]["session_transition_evidence"], 1)
        self.assertEqual(preview["cascade"]["soak_capture_cycles"], 1)
        self.assertEqual(preview["cascade"]["soak_resource_samples"], 1)
        self.assertEqual(preview["cascade"]["soak_evidence_events"], 1)
        self.assertEqual(preview["cascade"]["soak_daily_snapshots"], 1)

        applied = delete_range(self.conn, start, end)
        for key in (
            "session_transition_evidence",
            "soak_capture_cycles",
            "soak_resource_samples",
            "soak_evidence_events",
            "soak_daily_snapshots",
        ):
            self.assertEqual(applied["cascade"][key], preview["cascade"][key])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS count FROM soak_capture_cycles").fetchone()["count"],
            1,
        )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) AS count FROM gap_events").fetchone()["count"], 0)


if __name__ == "__main__":
    unittest.main()
