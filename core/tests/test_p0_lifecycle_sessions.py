from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import nullcontext
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from activity_mvp.activity_sessions import (
    observe_system_activity_session,
    refresh_activity_session,
    show_activity_session,
)
from activity_mvp.daemon import cleanup_retention
from activity_mvp.ha_source import generate_package
from activity_mvp.lifecycle import delete_range, preview_delete_range
from activity_mvp.macos_probe import capture_sck_keyframe_batch
from activity_mvp.ocr_service import OcrOutcome
from activity_mvp.pipeline import ingest_events
from activity_mvp.retention import retention_cleanup_cutoff
from activity_mvp.storage import ensure_ready


def private_vlm_test_image(root: Path, name: str) -> Path:
    vault = root / "raw_vault"
    vault.mkdir(mode=0o700, exist_ok=True)
    vault.chmod(0o700)
    path = vault / name
    path.touch(mode=0o600, exist_ok=False)
    path.chmod(0o600)
    return path


class ImmediateOcrTask:
    def __init__(self, outcome: OcrOutcome) -> None:
        self.outcome = outcome

    def result(self) -> OcrOutcome:
        return self.outcome

    def cancel(self) -> None:
        return None

    def done(self) -> bool:
        return True

    def wait_terminal(self) -> None:
        return None


class P0LifecycleSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.conn = ensure_ready(self.root / "activity.sqlite")

    def tearDown(self) -> None:
        self.conn.close()
        self.temp.cleanup()

    def test_primary_wall_clock_is_mutually_exclusive_with_privacy_and_gap_overlays(self) -> None:
        opened = observe_system_activity_session(
            self.conn,
            locked=False,
            observed_at="2026-07-13T10:00:00+08:00",
        )
        session_id = opened["current_session"]["id"]
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-07-13T10:00:00+08:00",
                    "display_id": "main",
                    "app_name": "Obsidian",
                    "window_title": "Safe work",
                    "ocr_text": "safe primary activity",
                }
            ],
        )
        self.conn.execute(
            """
            INSERT INTO privacy_events(id, started_at, ended_at, type, reason, app_name, domain, action, deletion_group_id)
            VALUES('privacy-overlap', ?, ?, 'protected', 'test_privacy', '', '', 'drop', 'privacy-group')
            """,
            ("2026-07-13T10:00:00+08:00", "2026-07-13T10:00:45+08:00"),
        )
        self.conn.execute(
            """
            INSERT INTO gap_events(id, started_at, ended_at, reason, display_ids, raw_deleted_at, detail, deletion_group_id)
            VALUES('gap-overlap', ?, ?, 'test_gap', '[]', ?, '{}', 'gap-group')
            """,
            (
                "2026-07-13T10:00:00+08:00",
                "2026-07-13T10:00:45+08:00",
                "2026-07-13T10:00:45+08:00",
            ),
        )
        observe_system_activity_session(
            self.conn,
            locked=True,
            observed_at="2026-07-13T10:01:00+08:00",
        )

        rows = self.conn.execute(
            """
            SELECT started_at, ended_at, duration_seconds
            FROM system_activity_samples
            WHERE session_id = ? AND attribution_kind = 'primary_focus'
            ORDER BY started_at
            """,
            (session_id,),
        ).fetchall()
        session = show_activity_session(self.conn, session_id)["session"]
        category_rollups = session["primary_focus_share"]

        self.assertEqual(sum(int(row["duration_seconds"]) for row in rows), 60)
        self.assertEqual(sum(item["duration_seconds"] for item in category_rollups), 60)
        self.assertLessEqual(sum(item["share"] for item in category_rollups), 1.01)
        self.assertTrue(all(0.0 <= item["share"] <= 1.0 for item in category_rollups))
        for left, right in zip(rows, rows[1:]):
            self.assertLessEqual(left["ended_at"], right["started_at"])

    def test_delete_range_rebuilds_overlapping_session_and_matches_preview_counts(self) -> None:
        opened = observe_system_activity_session(
            self.conn,
            locked=False,
            observed_at="2026-07-13T11:00:00+08:00",
        )
        session_id = opened["current_session"]["id"]
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": "2026-07-13T11:00:00+08:00",
                    "display_id": "main",
                    "app_name": "Obsidian",
                    "window_title": "Before",
                    "ocr_text": "safe before deletion",
                },
                {
                    "captured_at": "2026-07-13T11:01:00+08:00",
                    "display_id": "main",
                    "app_name": "DeleteMeApp",
                    "window_title": "DELETE_ME_TITLE",
                    "ocr_text": "DELETE_ME_CONTENT",
                },
                {
                    "captured_at": "2026-07-13T11:02:00+08:00",
                    "display_id": "main",
                    "app_name": "Codex",
                    "window_title": "After",
                    "ocr_text": "safe after deletion",
                },
            ],
        )
        observe_system_activity_session(
            self.conn,
            locked=True,
            observed_at="2026-07-13T11:03:00+08:00",
        )
        deleted_frame = self.conn.execute(
            "SELECT id FROM frame_signals WHERE captured_at = '2026-07-13T11:01:00+08:00'"
        ).fetchone()["id"]
        self.conn.execute(
            """
            INSERT OR REPLACE INTO capture_fingerprints(display_id, frame_hash, captured_at, updated_at)
            VALUES('main', 'deleted-fingerprint', ?, ?)
            """,
            ("2026-07-13T11:01:00+08:00", "2026-07-13T11:01:00+08:00"),
        )
        self.conn.execute(
            """
            INSERT INTO quality_advice_runs(
              id, run_key, started_at, ended_at, profile, status, created_at, updated_at
            ) VALUES('advice-delete', 'advice-delete-key', ?, ?, 'test', 'completed', ?, ?)
            """,
            (
                "2026-07-13T11:00:30+08:00",
                "2026-07-13T11:01:30+08:00",
                "2026-07-13T11:01:30+08:00",
                "2026-07-13T11:01:30+08:00",
            ),
        )
        self.conn.execute(
            """
            INSERT INTO ha_automation_jobs(
              id, hour_key, started_at, ended_at, trigger_mode, hermes_profile,
              status, created_at, updated_at
            ) VALUES('ha-delete', '2026-07-13T11', ?, ?, 'test', 'test', 'completed', ?, ?)
            """,
            (
                "2026-07-13T11:00:00+08:00",
                "2026-07-13T11:02:00+08:00",
                "2026-07-13T11:02:00+08:00",
                "2026-07-13T11:02:00+08:00",
            ),
        )
        self.conn.commit()
        start = "2026-07-13T11:00:50+08:00"
        end = "2026-07-13T11:01:10+08:00"
        preview = preview_delete_range(self.conn, start, end)

        applied = delete_range(self.conn, start, end)

        for key in (
            "activities",
            "frame_signals",
            "display_frame_contexts",
            "capture_fingerprints",
            "ha_automation_jobs",
            "quality_advice_runs",
            "system_activity_sessions_touched",
            "system_activity_samples_removed",
            "system_activity_session_rollups_removed",
            "gap_events_created",
        ):
            self.assertEqual(preview["cascade"][key], applied["cascade"][key], key)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS count FROM frame_signals WHERE id = ?", (deleted_frame,)).fetchone()["count"],
            0,
        )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) AS count FROM capture_fingerprints").fetchone()["count"], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) AS count FROM quality_advice_runs WHERE id = 'advice-delete'").fetchone()["count"], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) AS count FROM ha_automation_jobs WHERE id = 'ha-delete'").fetchone()["count"], 0)
        encoded_samples = json.dumps(
            [dict(row) for row in self.conn.execute("SELECT * FROM system_activity_samples WHERE session_id = ?", (session_id,)).fetchall()],
            ensure_ascii=False,
        )
        self.assertNotIn("DELETE_ME", encoded_samples)
        primary_seconds = self.conn.execute(
            """
            SELECT COALESCE(SUM(duration_seconds), 0) AS seconds
            FROM system_activity_samples
            WHERE session_id = ? AND attribution_kind = 'primary_focus'
            """,
            (session_id,),
        ).fetchone()["seconds"]
        self.assertEqual(primary_seconds, 180)
        self.assertEqual(applied["cascade"]["system_activity_sessions_touched"], 1)
        self.assertGreater(applied["cascade"]["system_activity_samples_rebuilt"], 0)

    def test_refresh_activity_session_preserves_owning_transaction(self) -> None:
        opened = observe_system_activity_session(
            self.conn,
            locked=False,
            observed_at="2026-07-13T12:00:00+08:00",
        )
        session_id = opened["current_session"]["id"]
        self.conn.commit()
        self.conn.execute("BEGIN IMMEDIATE")

        refresh_activity_session(
            self.conn,
            session_id,
            active_end="2026-07-13T12:01:00+08:00",
        )

        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()


    def test_delete_range_rolls_back_partial_writes_for_process_control_exceptions(self) -> None:
        captured_at = "2026-07-13T13:00:00+08:00"
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": captured_at,
                    "display_id": "main",
                    "app_name": "RollbackControlApp",
                    "window_title": "ROLLBACK_CONTROL_TITLE",
                    "ocr_text": "ROLLBACK_CONTROL_CONTENT",
                }
            ],
        )
        self.conn.commit()
        activity_id = str(
            self.conn.execute(
                "SELECT id FROM extracted_activities WHERE started_at=?",
                (captured_at,),
            ).fetchone()["id"]
        )

        failures: tuple[BaseException, ...] = (
            KeyboardInterrupt("injected_delete_range_interrupt"),
            SystemExit(73),
        )
        for index, failure in enumerate(failures):
            marker = f"partial-context-write-{index}"
            unrelated_key = f"post-failure-unrelated-{index}"

            def fail_after_partial_context_write(conn, *_args, **_kwargs):
                conn.execute(
                    "INSERT INTO audit_logs(id,at,action,detail) VALUES(?,?,?,?)",
                    (marker, captured_at, "partial_context_cascade", "must_rollback"),
                )
                raise failure

            with self.subTest(exception=type(failure).__name__), patch(
                "activity_mvp.context_console._revoke_context_observations_for_range_locked",
                side_effect=fail_after_partial_context_write,
            ):
                with self.assertRaises(type(failure)) as raised:
                    delete_range(
                        self.conn,
                        "2026-07-13T12:59:00+08:00",
                        "2026-07-13T13:01:00+08:00",
                    )

            self.assertIs(raised.exception, failure)
            self.assertEqual(raised.exception.args, failure.args)
            self.assertFalse(self.conn.in_transaction)

            self.conn.execute(
                "INSERT INTO daemon_state(key,value,updated_at) VALUES(?,?,?)",
                (unrelated_key, "committed", captured_at),
            )
            self.conn.commit()

            self.assertEqual(
                self.conn.execute(
                    "SELECT COUNT(*) FROM extracted_activities WHERE id=?",
                    (activity_id,),
                ).fetchone()[0],
                1,
            )
            self.assertIsNone(
                self.conn.execute(
                    "SELECT id FROM audit_logs WHERE id=?",
                    (marker,),
                ).fetchone()
            )
            self.assertEqual(
                self.conn.execute(
                    "SELECT value FROM daemon_state WHERE key=?",
                    (unrelated_key,),
                ).fetchone()["value"],
                "committed",
            )

    def test_retention_uses_same_cascade_and_leaves_only_content_free_session_gap(self) -> None:
        cutoff = retention_cleanup_cutoff()
        old_start = cutoff - timedelta(hours=2)
        old_end = old_start + timedelta(minutes=1)
        kept_start = cutoff + timedelta(hours=1)
        kept_end = kept_start + timedelta(minutes=1)

        old = observe_system_activity_session(
            self.conn,
            locked=False,
            observed_at=old_start.isoformat(timespec="seconds"),
        )
        old_session_id = old["current_session"]["id"]
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": old_start.isoformat(timespec="seconds"),
                    "display_id": "main",
                    "app_name": "Obsidian",
                    "window_title": "OLD_RETENTION_TITLE",
                    "ocr_text": "OLD_RETENTION_CONTENT",
                }
            ],
        )
        observe_system_activity_session(
            self.conn,
            locked=True,
            observed_at=old_end.isoformat(timespec="seconds"),
        )
        kept = observe_system_activity_session(
            self.conn,
            locked=False,
            observed_at=kept_start.isoformat(timespec="seconds"),
        )
        kept_session_id = kept["current_session"]["id"]
        ingest_events(
            self.conn,
            [
                {
                    "captured_at": kept_start.isoformat(timespec="seconds"),
                    "display_id": "main",
                    "app_name": "Codex",
                    "window_title": "KEPT_RETENTION_TITLE",
                    "ocr_text": "KEPT_RETENTION_CONTENT",
                }
            ],
        )
        observe_system_activity_session(
            self.conn,
            locked=True,
            observed_at=kept_end.isoformat(timespec="seconds"),
        )

        start = "0001-01-01T00:00:00+00:00"
        end = cutoff.isoformat(timespec="seconds")
        preview = preview_delete_range(self.conn, start, end, source_status="expired_deleted")
        with patch("activity_mvp.daemon.retention_cleanup_cutoff", return_value=cutoff):
            result = cleanup_retention(self.conn, self.root)
        applied = result["deleted"]

        self.assertEqual(preview["cascade"]["system_activity_sessions_touched"], 1)
        self.assertEqual(preview["cascade"]["system_activity_samples_removed"], applied["cascade"]["system_activity_samples_removed"])
        self.assertEqual(applied["cascade"]["gap_events_created"], 0)
        old_samples = [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM system_activity_samples WHERE session_id = ?",
                (old_session_id,),
            ).fetchall()
        ]
        self.assertTrue(old_samples)
        self.assertTrue(all(row["category"] == "gap" for row in old_samples if row["attribution_kind"] == "primary_focus"))
        self.assertNotIn("OLD_RETENTION", json.dumps(old_samples, ensure_ascii=False))
        old_primary_seconds = sum(row["duration_seconds"] for row in old_samples if row["attribution_kind"] == "primary_focus")
        self.assertEqual(old_primary_seconds, 60)
        kept_samples = [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM system_activity_samples WHERE session_id = ?",
                (kept_session_id,),
            ).fetchall()
        ]
        self.assertIn("KEPT_RETENTION_TITLE", json.dumps(kept_samples, ensure_ascii=False))

    def test_multi_display_batch_uses_one_capture_time_and_batch_id_despite_processing_delay(self) -> None:
        first_image = self.root / "display-1.png"
        second_image = self.root / "display-2.png"
        first_image.write_bytes(b"first")
        second_image.write_bytes(b"second")
        completed = SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=json.dumps(
                [
                    {
                        "status": "captured",
                        "image_path": str(first_image),
                        "display_id": 1,
                        "frame_hash": "hash-1",
                        "is_main": True,
                        "width": 100,
                        "height": 100,
                        "visible_windows": [],
                    },
                    {
                        "status": "captured",
                        "image_path": str(second_image),
                        "display_id": 2,
                        "frame_hash": "hash-2",
                        "is_main": False,
                        "width": 100,
                        "height": 100,
                        "visible_windows": [],
                    },
                ]
            ),
        )

        class AdvancingDateTime:
            calls = 0

            @classmethod
            def now(cls):
                cls.calls += 1
                return datetime.fromisoformat("2026-07-13T12:00:00+08:00") + timedelta(seconds=30 * cls.calls)

        def delayed_ocr(_path: Path) -> dict:
            return {"lines": [], "coordinate_space": "vision"}

        with patch("activity_mvp.macos_probe.helper_command", return_value=["helper"]), patch(
            "activity_mvp.macos_probe.subprocess.run", return_value=completed
        ), patch("activity_mvp.macos_probe.datetime", AdvancingDateTime), patch(
            "activity_mvp.macos_probe._frontmost_context",
            return_value={"app_name": "Obsidian", "bundle_id": "md.obsidian", "window_title": "Safe"},
        ), patch("activity_mvp.macos_probe._accessibility_context", return_value={}), patch(
            "activity_mvp.macos_probe._browser_url", return_value={"url": "", "domain": "", "source": "test"}
        ), patch(
            "activity_mvp.macos_probe.compiled_helper_lease",
            return_value=nullcontext(["/private/vision_ocr"]),
        ), patch(
            "activity_mvp.macos_probe.submit_ocr",
            side_effect=lambda _root, _command, path: ImmediateOcrTask(
                OcrOutcome(payload=delayed_ocr(Path(path)))
            ),
        ):
            batch = capture_sck_keyframe_batch(self.root, should_process=lambda _metadata: True)

        self.assertEqual(AdvancingDateTime.calls, 1)
        self.assertEqual(len(batch["events"]), 2)
        self.assertEqual({event["captured_at"] for event in batch["events"]}, {batch["captured_at"]})
        self.assertEqual({event["capture_batch_id"] for event in batch["events"]}, {batch["capture_batch_id"]})
        self.assertTrue(batch["capture_batch_id"].startswith("sck_batch_"))


if __name__ == "__main__":
    unittest.main()
