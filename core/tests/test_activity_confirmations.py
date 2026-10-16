from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from activity_mvp.activity_confirmations import (
    CONFIRMATION_POLICY_VERSION,
    INTERVIEW_META_LANGUAGE_RE,
    _member_revision,
    _regroup_confirmation_items,
    _stable_cluster_group_key,
    answer_activity_confirmation,
    answer_activity_confirmation_group,
    backfill_unresolved_confirmation_groups,
    clarify_activity_confirmation_group,
    confirmed_activity_view,
    list_activity_confirmation_cards,
    list_activity_confirmations,
    manual_add_confirmed_activity,
    materialize_activity_confirmations,
    revalidate_unresolved_confirmation_policy,
    revoke_confirmation_calibration,
    settle_activity_confirmations,
)
from activity_mvp.ai_connector import build_ai_connector_package
from activity_mvp.ai_handoff import build_ai_handoff_package
from activity_mvp.confirmation_evidence import (
    evidence_root,
    promote_confirmation_evidence,
    reconcile_confirmation_evidence,
)
from activity_mvp.daily_review import daily_review
from activity_mvp.daemon import _confirmed_activity_status, get_status
from activity_mvp.document_changes import document_authorization_lock
from activity_mvp.ha_source import generate_package
from activity_mvp.lifecycle import delete_range, preview_delete_range
from activity_mvp.pipeline import (
    _promote_pending_episode_evidence,
    ingest_events,
    search,
    timeline,
)
from activity_mvp.report_context import report_context
from activity_mvp.reclassify import reclassify_existing
from activity_mvp.storage import SCHEMA_VERSION, ensure_ready
from activity_mvp.timeline_export import cleaned_timeline_export


class ActivityConfirmationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.db = self.root / "activity.sqlite"
        self.conn = ensure_ready(self.db)
        self.counter = 0
        self.today = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)

    def tearDown(self) -> None:
        states = {
            str(row["state"])
            for row in self.conn.execute("SELECT DISTINCT state FROM activity_confirmation_items")
        }
        self.assertLessEqual(
            states,
            {"pending", "confirmed", "rejected", "deferred", "ignored", "filtered"},
        )
        self.conn.close()
        self.tempdir.cleanup()

    def _insert_activity(
        self,
        summary: str,
        *,
        at: datetime | None = None,
        app: str = "ChatGPT",
        region_kind: str = "unknown",
        actionability: str = "unknown",
        reason_codes: list[str] | None = None,
    ) -> str:
        self.counter += 1
        activity_id = f"activity_{self.counter}"
        frame_id = f"frame_{self.counter}"
        group_id = f"group_{self.counter}"
        started = at or (self.today + timedelta(minutes=self.counter))
        ended = started + timedelta(minutes=10)
        start_text = started.isoformat(timespec="seconds")
        end_text = ended.isoformat(timespec="seconds")
        self.conn.execute(
            """
            INSERT INTO extracted_activities(
              id,session_id,started_at,ended_at,activity_summary,apps,domains,evidence_refs,
              privacy_level,redaction_confidence,quarantine_status,quarantine_reason,
              dedupe_status,deletion_group_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                activity_id,
                "test_session",
                start_text,
                end_text,
                summary,
                json.dumps([app], ensure_ascii=False),
                "[]",
                json.dumps([frame_id]),
                "normal",
                0.95,
                "clear",
                "test",
                "unique",
                group_id,
            ),
        )
        region = {
            "region_kind": region_kind,
            "actionability": actionability,
            "reason_codes": reason_codes or [],
        }
        self.conn.execute(
            """
            INSERT INTO frame_signals(
              id,session_id,captured_at,display_id,app_name,window_title_redacted,
              ocr_redacted_summary,privacy_flags,screen_region_signal,redaction_status,
              deletion_group_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                frame_id,
                "test_session",
                start_text,
                "main",
                app,
                summary,
                summary,
                "[]",
                json.dumps(region, ensure_ascii=False),
                "redacted",
                group_id,
            ),
        )
        self.conn.execute(
            """
            INSERT INTO activity_provenance(
              id,derived_type,derived_id,source_type,source_id,source_time_range,
              deletion_group_id,created_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                f"prov_{self.counter}",
                "extracted_activity",
                activity_id,
                "frame_signal",
                frame_id,
                json.dumps({"from": start_text, "to": end_text}),
                group_id,
                start_text,
            ),
        )
        self.conn.execute(
            "INSERT INTO search_index(entity_type,entity_id,started_at,app_name,domain,summary,keywords) VALUES(?,?,?,?,?,?,?)",
            ("extracted_activity", activity_id, start_text, app, "", summary, summary),
        )
        self.conn.commit()
        return activity_id

    def _materialize(self, start: datetime | None = None, end: datetime | None = None) -> dict:
        start = start or self.today.replace(hour=0, minute=0)
        end = end or (start + timedelta(days=1))
        materialize_activity_confirmations(
            self.conn,
            start.isoformat(timespec="seconds"),
            end.isoformat(timespec="seconds"),
        )
        return list_activity_confirmations(
            self.conn,
            start=start.isoformat(timespec="seconds"),
            end=end.isoformat(timespec="seconds"),
            include_resolved=True,
        )

    def _new_item(self, summary: str, **kwargs) -> dict:
        self._insert_activity(summary, **kwargs)
        payload = self._materialize()
        matching = [item for item in payload["items"] if item["safe_observation"] == summary]
        self.assertGreaterEqual(len(matching), 1)
        return max(matching, key=lambda item: item["observed_start"])

    def _attach_confirmation_evidence(
        self,
        item_key: str,
        *,
        payload: bytes = b"\x89PNG\r\n\x1a\ntest-png-payload",
    ) -> dict:
        item = self.conn.execute(
            "SELECT observed_start,source_activity_ids,deletion_group_ids "
            "FROM activity_confirmation_items WHERE item_key=?",
            (item_key,),
        ).fetchone()
        self.assertIsNotNone(item)
        source_activity_ids = json.loads(item["source_activity_ids"] or "[]")
        deletion_group_ids = json.loads(item["deletion_group_ids"] or "[]")
        self.assertTrue(source_activity_ids)
        provenance = self.conn.execute(
            "SELECT source_id FROM activity_provenance "
            "WHERE derived_type='extracted_activity' AND derived_id=? "
            "AND source_type='frame_signal' LIMIT 1",
            (source_activity_ids[0],),
        ).fetchone()
        self.assertIsNotNone(provenance)
        raw_root = self.root / "raw_vault"
        raw_root.mkdir(mode=0o700, exist_ok=True)
        raw_root.chmod(0o700)
        raw_path = raw_root / f"{item_key}.png"
        raw_path.write_bytes(payload)
        raw_path.chmod(0o600)
        promoted = promote_confirmation_evidence(
            self.conn,
            item_key=item_key,
            source_activity_id=source_activity_ids[0],
            deletion_group_id=deletion_group_ids[0] if deletion_group_ids else "",
            source_frame_id=str(provenance["source_id"]),
            captured_at=str(item["observed_start"]),
            display_id="main",
            source_path=raw_path,
        )
        self.assertEqual(promoted["status"], "available")
        self.conn.commit()
        raw_path.unlink(missing_ok=True)
        stored = self.conn.execute(
            "SELECT file_name,state FROM activity_confirmation_evidence_files "
            "WHERE evidence_id=?",
            (promoted["evidence_id"],),
        ).fetchone()
        self.assertEqual(stored["state"], "available")
        return {
            **promoted,
            "path": evidence_root(self.root) / str(stored["file_name"]),
        }

    def test_schema_and_public_contract_are_content_minimal(self) -> None:
        self.assertEqual(SCHEMA_VERSION, 32)
        tables = {row["name"] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("activity_confirmation_items", tables)
        self.assertIn("activity_confirmation_calibrations", tables)

        self.assertIn("activity_confirmation_evidence_files", tables)
        self.assertIn("activity_confirmation_evidence_refs", tables)
        columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(activity_confirmation_items)")
        }
        self.assertIn("clarification_note", columns)
        item = self._new_item("今天完成浩方面试")
        self.assertEqual(item["state"], "pending")
        self.assertEqual(
            item["answer_options"],
            ["occurred", "not_occurred", "ignored", "defer"],
        )
        listed = list_activity_confirmations(self.conn, range_name="today")
        self.assertEqual(listed["contract_version"], 4)
        self.assertIn("question_kind", columns)
        self.assertIn("candidate_origin", columns)
        self.assertIn("episode_id", columns)
        self.assertEqual(item["evidence_status"], "unavailable")
        self.assertEqual((item["evidence_count"], item["available_source_count"]), (0, 0))
        self.assertNotIn("source_activity_ids", item)
        self.assertNotIn("clarification_note", item)
        self.assertNotIn("今天", item["decision_question"])
        self.assertIn(f"{self.today.month}月{self.today.day}日", item["decision_question"])
        self.assertIn(f"{self.today.month}月{self.today.day}日", item["time_label"])

    def test_schema_26_to_27_backs_up_and_preserves_existing_confirmation_rows(self) -> None:
        migration_db = self.root / "schema26.sqlite"
        original = ensure_ready(migration_db)
        now = self.today.replace(hour=9).isoformat(timespec="seconds")
        later = self.today.replace(hour=9, minute=10).isoformat(timespec="seconds")
        original.executescript(
            """
            DROP TABLE activity_confirmation_evidence_refs;
            DROP TABLE activity_confirmation_evidence_files;
            ALTER TABLE activity_confirmation_items DROP COLUMN clarification_note;
            DROP TRIGGER pam_schema27_guard_insert;
            DROP TRIGGER pam_schema27_guard_update;
            DROP TRIGGER pam_schema28_guard_insert;
            DROP TRIGGER pam_schema28_guard_update;
            DROP TRIGGER pam_schema30_guard_insert;
            DROP TRIGGER pam_schema30_guard_update;
            DROP TRIGGER pam_schema31_guard_insert;
            DROP TRIGGER pam_schema31_guard_update;
            DROP TRIGGER pam_schema32_guard_insert;
            DROP TRIGGER pam_schema32_guard_update;
            """
        )
        rows = (
            ("answered", "今天完成浩方二面 PRIVATE_ORPHAN_ANSWERED", "ChatGPT", "confirmed", "occurred", "correct"),
            ("pending", "今天完成浩方二面", "ChatGPT", "pending", None, None),
            ("bare", "浩方面试", "ChatGPT", "pending", None, None),
            (
                "meta_pending",
                "待你确认：7月18日检测到一条可能表示面试已发生的活动",
                "Unknown",
                "pending",
                None,
                None,
            ),
            (
                "meta_discussion",
                "一条旧页面截图被反向识别成面试已发生",
                "ChatGPT",
                "pending",
                None,
                None,
            ),
            (
                "meta_language",
                "避免把真实的面试已完成句子一并过滤掉",
                "ChatGPT",
                "pending",
                None,
                None,
            ),
            ("orphan_pending", "今天完成浩方二面 PRIVATE_ORPHAN_PENDING", "ChatGPT", "pending", None, None),
            ("self_pending", "今天完成浩方二面", "Personal AI Monitor", "pending", None, None),
        )
        for item_key, observation, app_family, state, answer, answer_reason in rows:
            original.execute(
                """
                INSERT INTO activity_confirmation_items(
                  item_key,event_type,safe_observation,decision_question,why_uncertain,app_family,
                  observed_start,observed_end,confidence,reason_codes,region_kind,actionability,
                  source_activity_ids,deletion_group_ids,fingerprint,calibration_key,state,answer,
                  answer_reason,user_note,generalize_scope,created_at,updated_at,answered_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    item_key,"interview",observation,"旧问题","旧解释",app_family,now,later,0.5,"[]",
                    "unknown","unknown",json.dumps([f"activity_{item_key}"]),json.dumps([f"dg_{item_key}"]),
                    f"fp_{item_key}","cal_migration",state,answer,answer_reason,"","exact",now,now,
                    now if answer else None,
                ),
            )
        for item_key, summary in (
            ("pending", "今天完成浩方二面"),
            ("bare", "浩方面试"),
            ("meta_pending", "待你确认：7月18日检测到一条可能表示面试已发生的活动"),
            ("meta_discussion", "一条旧页面截图被反向识别成面试已发生"),
            ("meta_language", "避免把真实的面试已完成句子一并过滤掉"),
            ("unbound", "今天完成小鹏初面 SCHEMA26_UNBOUND_TIMELINE"),
        ):
            original.execute(
                """
                INSERT INTO extracted_activities(
                  id,session_id,started_at,ended_at,activity_summary,apps,domains,evidence_refs,
                  privacy_level,redaction_confidence,quarantine_status,quarantine_reason,
                  dedupe_status,deletion_group_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"activity_{item_key}","migration",now,later,summary,'["ChatGPT"]',"[]","[]",
                    "normal",0.9,"clear","migration","unique",f"dg_{item_key}",
                ),
            )
        original.execute(
            """
            INSERT INTO activity_confirmation_calibrations(
              calibration_key,event_type,app_family,region_kind,actionability,positive_count,
              negative_count,misrecognized_count,reason_counts,last_answer,last_reason,
              revoked_at,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            ("cal_migration","interview","ChatGPT","unknown","unknown",1,2,1,'{"old_content":2}',
             "not_occurred","old_content",None,now,now),
        )
        original.execute("UPDATE meta SET value='26' WHERE key='schema_version'")
        original.commit()
        original.close()

        migrated = ensure_ready(migration_db)
        backup_path = migration_db.with_name(f"{migration_db.name}.schema26-backup")
        try:
            self.assertTrue(backup_path.is_file())
            self.assertEqual(backup_path.stat().st_mode & 0o777, 0o600)
            backup = sqlite3.connect(backup_path)
            try:
                self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(backup.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "26")
                self.assertNotIn(
                    "clarification_note",
                    {row[1] for row in backup.execute("PRAGMA table_info(activity_confirmation_items)")},
                )
                self.assertIsNone(
                    backup.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='activity_confirmation_evidence_files'"
                    ).fetchone()
                )
            finally:
                backup.close()
            self.assertEqual(migrated.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "32")
            columns = {row["name"] for row in migrated.execute("PRAGMA table_info(activity_confirmation_items)")}
            self.assertTrue(
                {
                    "group_key",
                    "group_signature",
                    "subject",
                    "evidence",
                    "cleaned_content",
                    "clarification",
                    "clarification_note",
                }
                <= columns
            )
            migrated_tables = {
                row["name"]
                for row in migrated.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertTrue(
                {
                    "activity_confirmation_evidence_files",
                    "activity_confirmation_evidence_refs",
                }
                <= migrated_tables
            )
            answered = dict(migrated.execute("SELECT * FROM activity_confirmation_items WHERE item_key='answered'").fetchone())
            self.assertEqual((answered["state"], answered["answer"], answered["updated_at"]), ("confirmed","occurred",now))
            self.assertEqual(answered["group_key"], "")
            self.assertIsNone(
                migrated.execute(
                    "SELECT 1 FROM activity_confirmation_items WHERE item_key='orphan_pending'"
                ).fetchone()
            )
            self_filtered = migrated.execute(
                "SELECT state,answer,reason_codes FROM activity_confirmation_items "
                "WHERE item_key='self_pending'"
            ).fetchone()
            self.assertEqual((self_filtered["state"], self_filtered["answer"]), ("filtered", None))
            self.assertIn("schema26_self_app_filtered", json.loads(self_filtered["reason_codes"]))
            pending = dict(migrated.execute("SELECT * FROM activity_confirmation_items WHERE item_key='pending'").fetchone())
            self.assertTrue(pending["group_key"])
            self.assertEqual(json.loads(pending["subject"])["label"], "浩方第二轮面试")
            bare = migrated.execute("SELECT state,answer FROM activity_confirmation_items WHERE item_key='bare'").fetchone()
            self.assertEqual((bare["state"], bare["answer"]), ("filtered", None))
            meta_filtered = migrated.execute(
                "SELECT state,answer,reason_codes FROM activity_confirmation_items "
                "WHERE item_key='meta_pending'"
            ).fetchone()
            self.assertEqual((meta_filtered["state"], meta_filtered["answer"]), ("filtered", None))
            self.assertIn("schema26_non_occurrence_filtered", json.loads(meta_filtered["reason_codes"]))
            meta_discussion = migrated.execute(
                "SELECT state,answer,reason_codes FROM activity_confirmation_items "
                "WHERE item_key='meta_discussion'"
            ).fetchone()
            self.assertEqual((meta_discussion["state"], meta_discussion["answer"]), ("filtered", None))
            self.assertIn("schema26_non_occurrence_filtered", json.loads(meta_discussion["reason_codes"]))
            meta_language = migrated.execute(
                "SELECT state,answer,reason_codes FROM activity_confirmation_items "
                "WHERE item_key='meta_language'"
            ).fetchone()
            self.assertEqual((meta_language["state"], meta_language["answer"]), ("filtered", None))
            self.assertIn("schema26_non_occurrence_filtered", json.loads(meta_language["reason_codes"]))
            calibration = dict(migrated.execute("SELECT * FROM activity_confirmation_calibrations WHERE calibration_key='cal_migration'").fetchone())
            self.assertEqual((calibration["positive_count"], calibration["negative_count"], calibration["misrecognized_count"], calibration["reason_counts"]), (1,2,1,'{"old_content":2}'))
            before_first_list_count = migrated.execute(
                "SELECT COUNT(*) FROM activity_confirmation_items"
            ).fetchone()[0]
            public_payload = list_activity_confirmations(
                migrated,
                start=self.today.isoformat(timespec="seconds"),
                end=(self.today + timedelta(days=1)).isoformat(timespec="seconds"),
                include_resolved=True,
            )
            public_text = json.dumps(public_payload, ensure_ascii=False)
            self.assertEqual(
                migrated.execute("SELECT COUNT(*) FROM activity_confirmation_items").fetchone()[0],
                before_first_list_count,
            )
            self.assertNotIn("PRIVATE_ORPHAN_ANSWERED", public_text)
            self.assertNotIn("PRIVATE_ORPHAN_PENDING", public_text)
            self.assertNotIn("SCHEMA26_UNBOUND_TIMELINE", public_text)
            persisted_sources = {
                source_id
                for row in migrated.execute(
                    "SELECT source_activity_ids FROM activity_confirmation_items"
                ).fetchall()
                for source_id in json.loads(row["source_activity_ids"])
            }
            self.assertNotIn("activity_unbound", persisted_sources)
            migrated_group = next(
                group
                for group in public_payload["question_groups"]
                if group["subject_label"] == "浩方第二轮面试"
                and group["state"] == "pending"
            )
            self.assertEqual(
                answer_activity_confirmation_group(
                    migrated,
                    group_key=migrated_group["group_key"],
                    member_revision=migrated_group["member_revision"],
                    answer="occurred",
                    reason="correct",
                    workstream="面试求职",
                )["status"],
                "recorded",
            )
            self.assertEqual(
                migrated.execute("SELECT COUNT(*) FROM activity_confirmation_items").fetchone()[0],
                before_first_list_count,
            )
        finally:
            migrated.close()

    def test_report_context_unavailable_keeps_fixed_fail_closed_schema(self) -> None:
        unavailable_conn = sqlite3.connect(":memory:")
        unavailable_conn.close()
        payload = report_context(unavailable_conn, "2026-07-17")
        self.assertEqual(payload["version"], "0.9.1")
        self.assertEqual(payload["availability"], "unavailable")
        self.assertFalse(payload["raw_access"])
        self.assertIsNone(payload["coverage"])
        for field in ("pending_confirmation_count", "clear_count", "review_count", "blocked_count", "privacy_count"):
            self.assertIsNone(payload[field])
        self.assertEqual(payload["confirmation_completeness"]["status"], "unavailable")
        self.assertEqual(payload["activity_highlights"], [])
        self.assertEqual(payload["document_changes"], {})

    def test_report_context_holds_read_lease_until_response_is_built(self) -> None:
        self._insert_activity(
            "今天完成浩方二面",
            at=self.today.replace(hour=9),
            app="Report-Race",
        )
        group = self._materialize()["question_groups"][0]
        answer_activity_confirmation_group(
            self.conn,
            group_key=group["group_key"],
            member_revision=group["member_revision"],
            answer="occurred",
            reason="correct",
            workstream="面试求职",
        )
        # Ensure the shared/exclusive lease has a real secure lock file.
        with document_authorization_lock(self.conn, exclusive=True):
            pass
        view_read = threading.Event()
        allow_report = threading.Event()
        delete_started = threading.Event()
        delete_done = threading.Event()
        failures: list[BaseException] = []
        report_payloads: list[dict] = []
        start = self.today.isoformat(timespec="seconds")
        end = (self.today + timedelta(days=1)).isoformat(timespec="seconds")

        def paused_view(conn, query_start, query_end, *, materialize=False):
            view = confirmed_activity_view(
                conn,
                query_start,
                query_end,
                materialize=materialize,
            )
            view_read.set()
            if not allow_report.wait(5):
                raise TimeoutError("report lease barrier timed out")
            return view

        def run_report() -> None:
            conn = ensure_ready(self.db)
            try:
                report_payloads.append(report_context(conn, self.today.date().isoformat()))
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)
            finally:
                conn.close()

        def run_delete() -> None:
            delete_started.set()
            conn = ensure_ready(self.db)
            try:
                delete_range(conn, start, end)
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)
            finally:
                conn.close()
                delete_done.set()

        with patch("activity_mvp.report_context.confirmed_activity_view", side_effect=paused_view):
            reporter = threading.Thread(target=run_report)
            reporter.start()
            self.assertTrue(view_read.wait(5))
            deleter = threading.Thread(target=run_delete)
            deleter.start()
            self.assertTrue(delete_started.wait(5))
            self.assertFalse(delete_done.wait(0.2))
            allow_report.set()
            reporter.join(5)
            deleter.join(5)

        self.assertEqual(failures, [])
        self.assertEqual(report_payloads[0]["availability"], "available")
        self.assertFalse(reporter.is_alive())
        self.assertFalse(deleter.is_alive())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM extracted_activities").fetchone()[0], 0)

    def test_context_only_hofan_is_system_filtered_from_every_outbound_activity_fact(self) -> None:
        summary = "今天完成浩方面试"
        item = self._new_item(summary, region_kind="sidebar", actionability="context_only")
        self.assertEqual((item["state"], item["user_answer"]), ("filtered", ""))
        self.assertIn("policy_v7_non_actionable_region", item["reason_codes"])
        start = self.today.replace(hour=0, minute=0).isoformat(timespec="seconds")
        end = (self.today.replace(hour=0, minute=0) + timedelta(days=1)).isoformat(timespec="seconds")

        view = confirmed_activity_view(self.conn, start, end)
        self.assertEqual(view["pending_confirmation_count"], 0)
        self.assertEqual(view["trusted_activities"], [])
        self.assertEqual(len(view["owner_activities"]), 1)
        self.assertEqual(view["owner_activities"][0]["verification_state"], "unverified")
        self.assertIn(summary, daily_review(self.conn, start, end)["highlights"])
        pending_report = report_context(self.conn, self.today.date().isoformat())
        self.assertEqual(pending_report["clear_count"], 0)
        self.assertEqual(pending_report["pending_confirmation_count"], 0)
        self.assertNotIn(summary, json.dumps(build_ai_handoff_package(self.conn, start=start, end=end), ensure_ascii=False))
        self.assertNotIn(summary, json.dumps(build_ai_connector_package(self.conn), ensure_ascii=False))
        local_export = cleaned_timeline_export(self.conn, start, end)
        self.assertIn(summary, json.dumps(local_export["owner_observations"], ensure_ascii=False))
        self.assertNotIn(summary, json.dumps(local_export["trusted_activities"], ensure_ascii=False))
        self.assertEqual(search(self.conn, "浩方", start=start, end=end, confirmed_only=True), [])
        package = generate_package(self.conn, start, end, self.root / "packages", write_markdown=False)["package"]
        self.assertNotIn(summary, json.dumps(package, ensure_ascii=False))
        status_contract = _confirmed_activity_status(view, start, end)
        self.assertEqual(status_contract["confirmed_count"], 0)
        self.assertEqual(status_contract["pending_count"], 0)
        self.assertIsNone(status_contract["primary_activity"])

    def test_status_exposes_confirmed_activity_contract_without_pending_text(self) -> None:
        summary = "今天完成浩方面试 status 门禁"
        self._new_item(summary)
        with patch("activity_mvp.daemon.session_state", return_value={"locked": False}), patch(
            "activity_mvp.daemon._launch_agent_status",
            return_value={"loaded": True, "running": True, "pid": "123", "last_exit_code": "(never exited)"},
        ), patch(
            "activity_mvp.daemon._dashboard_listener",
            return_value={"listening": False, "pid": "", "command": ""},
        ):
            status = get_status(self.conn, self.root)
        confirmed = status["confirmed_activity"]
        self.assertEqual(confirmed["source"], "confirmed_activity_view")
        self.assertEqual(confirmed["confirmed_count"], 0)
        self.assertEqual(confirmed["pending_count"], 1)
        self.assertIsNone(confirmed["primary_activity"])
        self.assertNotIn(summary, json.dumps(confirmed, ensure_ascii=False))

    def test_user_confirmation_unlocks_review_report_handoff_and_connector(self) -> None:
        summary = "今天完成浩方面试"
        item = self._new_item(summary)
        answer_activity_confirmation(
            self.conn,
            item_key=item["item_key"],
            answer="occurred",
            reason="correct",
        )
        start = self.today.replace(hour=0, minute=0).isoformat(timespec="seconds")
        end = (self.today.replace(hour=0, minute=0) + timedelta(days=1)).isoformat(timespec="seconds")
        view = confirmed_activity_view(self.conn, start, end)
        self.assertEqual(view["activities"][0]["occurrence_state"], "user_confirmed")
        self.assertIn(summary, daily_review(self.conn, start, end)["highlights"])
        self.assertIn(summary, json.dumps(build_ai_handoff_package(self.conn, start=start, end=end), ensure_ascii=False))
        self.assertIn(summary, json.dumps(build_ai_connector_package(self.conn), ensure_ascii=False))
        report = report_context(self.conn, self.today.date().isoformat())
        self.assertEqual(report["availability"], "available")
        self.assertEqual(report["pending_confirmation_count"], 0)
        self.assertEqual(report["clear_count"], 1)
        self.assertTrue(any(item["occurrence_state"] == "user_confirmed" for item in report["activity_highlights"]))

    def test_list_and_report_never_retro_materialize_and_context_only_ingest_is_filtered(self) -> None:
        yesterday = self.today - timedelta(days=1)
        persisted_historical_id = self._insert_activity(
            "今天完成浩方面试 昨日已持久化确认项",
            at=yesterday.replace(hour=9),
        )
        materialize_activity_confirmations(
            self.conn,
            yesterday.isoformat(timespec="seconds"),
            self.today.isoformat(timespec="seconds"),
        )
        historical_id = self._insert_activity("今天完成浩方面试 昨日升级前历史", at=yesterday.replace(hour=10))
        today_id = self._insert_activity("今天完成浩方面试 今日升级前时间线", at=self.today.replace(hour=10))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM activity_confirmation_items").fetchone()[0], 1)

        listed = list_activity_confirmations(self.conn, range_name="7d", include_resolved=True)
        persisted_sources = {
            source_id
            for row in self.conn.execute("SELECT source_activity_ids FROM activity_confirmation_items")
            for source_id in json.loads(row["source_activity_ids"])
        }
        self.assertIn(persisted_historical_id, persisted_sources)
        self.assertNotIn(today_id, persisted_sources)
        self.assertNotIn(historical_id, persisted_sources)
        self.assertTrue(any(item["safe_observation"] == "今天完成浩方面试 昨日已持久化确认项" for item in listed["items"]))
        self.assertFalse(any(item["safe_observation"] == "今天完成浩方面试 今日升级前时间线" for item in listed["items"]))
        self.assertFalse(any(item["safe_observation"] == "今天完成浩方面试 昨日升级前历史" for item in listed["items"]))

        report = report_context(self.conn, yesterday.date().isoformat())
        self.assertEqual(report["pending_confirmation_count"], 1)
        persisted_sources = {
            source_id
            for row in self.conn.execute("SELECT source_activity_ids FROM activity_confirmation_items")
            for source_id in json.loads(row["source_activity_ids"])
        }
        self.assertNotIn(historical_id, persisted_sources)
        self.assertNotIn(today_id, persisted_sources)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM activity_confirmation_items").fetchone()[0], 1)

        ingest_events(
            self.conn,
            [
                {
                    "captured_at": self.today.replace(hour=11).isoformat(timespec="seconds"),
                    "display_id": "main",
                    "app_name": "Obsidian",
                    "window_title": "",
                    "ocr_text": "今天完成浩方二面",
                }
            ],
        )
        after_ingest = list_activity_confirmations(self.conn, range_name="today")
        stored_today = [
            dict(row)
            for row in self.conn.execute(
                "SELECT state,region_kind,actionability,reason_codes FROM activity_confirmation_items "
                "WHERE datetime(observed_start)>=datetime(?)",
                (self.today.isoformat(timespec="seconds"),),
            ).fetchall()
        ]
        self.assertEqual(after_ingest["pending_question_count"], 1, stored_today)
        self.assertEqual(after_ingest["pending_source_count"], 1)
        self.assertEqual(stored_today[0]["state"], "pending")
        self.assertIn("context_only_high_recall_review", json.loads(stored_today[0]["reason_codes"]))

    def test_terminal_note_does_not_rewrite_fact_and_clarify_must_precede_answer(self) -> None:
        shared_summary = "今天完成浩方面试 同小时相同摘要"
        first = self._new_item(shared_summary, at=self.today.replace(hour=0, minute=5))
        second = self._new_item(shared_summary, at=self.today.replace(hour=0, minute=10))
        self.assertNotEqual(first["item_key"], second["item_key"])
        group = next(
            group
            for group in self._materialize()["question_groups"]
            if group["source_count"] == 2
        )
        correction = "今天实际参加了浩方第二轮业务面试，已结束。"
        answered_directly = answer_activity_confirmation_group(
            self.conn,
            group_key=group["group_key"],
            member_revision=group["member_revision"],
            answer="occurred",
            reason="correct",
            note=correction,
            workstream="面试求职",
        )
        self.assertEqual(answered_directly["status"], "recorded")
        self.assertEqual(answered_directly["question_group"]["subject"]["round"], "")
        terminal_rows = self.conn.execute(
            "SELECT user_note,clarification_note FROM activity_confirmation_items "
            "WHERE group_key=?",
            (group["group_key"],),
        ).fetchall()
        self.assertEqual({tuple(row) for row in terminal_rows}, {("", "")})

        start = self.today.isoformat(timespec="seconds")
        end = (self.today + timedelta(days=1)).isoformat(timespec="seconds")
        view = confirmed_activity_view(self.conn, start, end)
        self.assertEqual([item["activity_summary"] for item in view["activities"]], [shared_summary])
        self.assertEqual(view["pending_confirmation_count"], 0)
        self.assertEqual(len(search(self.conn, "同小时相同摘要", start=start, end=end, confirmed_only=True)), 1)
        self.assertEqual(
            search(
                self.conn,
                "第二轮业务面试",
                start=start,
                end=end,
                confirmed_only=True,
            ),
            [],
        )

        refinable = self._new_item(
            "今天完成小鹏面试",
            at=self.today.replace(hour=3),
            app="ChatGPT-Clarify-Before-Answer",
        )
        refinable_group = next(
            candidate
            for candidate in self._materialize()["question_groups"]
            if candidate["app"] == "ChatGPT-Clarify-Before-Answer"
        )
        clarified = clarify_activity_confirmation_group(
            self.conn,
            group_key=refinable_group["group_key"],
            member_revision=refinable_group["member_revision"],
            note="补充：这是小鹏第二轮面试",
        )
        self.assertEqual(
            (clarified["result"]["outcome"], clarified["result"]["evidence_status"]),
            ("rewritten", "unavailable"),
        )
        self.assertIn("原截图不可用", clarified["result"]["message"])
        clarified_group = clarified["result"]["question_groups"][0]
        self.assertEqual(clarified_group["subject_label"], "小鹏第二轮面试")
        answered_after_clarify = answer_activity_confirmation_group(
            self.conn,
            group_key=clarified_group["group_key"],
            member_revision=clarified_group["member_revision"],
            answer="occurred",
            reason="correct",
            note="终态不应继续保存这段说明",
            workstream="面试求职",
        )
        self.assertEqual(answered_after_clarify["status"], "recorded")
        self.assertEqual(
            answered_after_clarify["question_group"]["subject_label"],
            "小鹏第二轮面试",
        )
        refined_row = self.conn.execute(
            "SELECT state,answer,user_note,clarification_note,subject "
            "FROM activity_confirmation_items WHERE item_key=?",
            (refinable["item_key"],),
        ).fetchone()
        self.assertEqual(tuple(refined_row)[:4], ("confirmed", "occurred", "", ""))
        self.assertEqual(json.loads(refined_row["subject"])["round"], "第二轮")

        payloads = [
            daily_review(self.conn, start, end),
            report_context(self.conn, self.today.date().isoformat()),
            build_ai_handoff_package(self.conn, range_name="today"),
            build_ai_connector_package(self.conn, range_name="today"),
            cleaned_timeline_export(self.conn, start, end),
            generate_package(self.conn, start, end, self.root / "handoff", write_markdown=False),
        ]
        for payload in payloads:
            self.assertNotIn(correction, json.dumps(payload, ensure_ascii=False))

    def test_user_confirmation_without_note_keeps_safe_activity_summary(self) -> None:
        summary = "今天完成浩方面试 保留原安全摘要"
        item = self._new_item(summary)
        answer_activity_confirmation(
            self.conn,
            item_key=item["item_key"],
            answer="occurred",
            reason="correct",
        )
        start = self.today.isoformat(timespec="seconds")
        end = (self.today + timedelta(days=1)).isoformat(timespec="seconds")
        self.assertEqual(confirmed_activity_view(self.conn, start, end)["activities"][0]["activity_summary"], summary)

    def test_interview_requires_occurrence_evidence_and_subject_extraction_never_guesses(self) -> None:
        ignored = [
            "面试",
            "明天安排浩方二面",
            "已完成浩方二面准备",
            "已完成浩方二面材料",
            "之前参加过浩方面试",
            "后面肯定还会有面试",
            "以后会完成浩方二面",
            "之后要去完成浩方二面",
            "预计届时完成浩方二面",
            "浩方二面尚未完成",
            "Interview preparation notes",
            "待你确认：7月18日检测到一条可能表示面试已发生的活动",
            "系统提示可能表示浩方二面已发生，请确认",
            "以及一条旧页面截图被反向识别成面试已发生",
            "避免为了挡住误报讨论而把真实的面试已完成句子一并过滤掉",
            "明确的今天完成浩方二面能提取触发句公司和轮次",
            "Please confirm: detected text may indicate the interview occurred",
        ]
        for index, summary in enumerate(ignored):
            self._insert_activity(
                summary,
                at=self.today.replace(hour=8) + timedelta(minutes=index * 2),
                app=f"Ignored-{index}",
            )
        for index, app_alias in enumerate(
            (
                "Personal AI Monitor",
                "Personal AI Monitor Menu Bar",
                "PersonalAIMonitor",
                "app.personalai.monitor",
                "com.exampleuser.PersonalAIMonitorMenuBar",
            )
        ):
            self._insert_activity(
                "今天完成浩方二面",
                at=self.today.replace(hour=9) + timedelta(minutes=index),
                app=app_alias,
            )
        self._materialize()
        unsafe_prep_occurrences = self.conn.execute(
            "SELECT safe_observation FROM activity_confirmation_items "
            "WHERE state IN ('pending','confirmed') AND question_kind='occurrence' "
            "AND (safe_observation LIKE '%准备%' OR safe_observation LIKE '%材料%' "
            "OR safe_observation LIKE '%明天%' OR safe_observation LIKE '%之前%')"
        ).fetchall()
        self.assertEqual(unsafe_prep_occurrences, [])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM activity_confirmation_items "
                "WHERE state IN ('pending','confirmed') "
                "AND (app_family LIKE 'Personal AI Monitor%' OR app_family='PersonalAIMonitor' "
                "OR app_family='app.personalai.monitor' "
                "OR app_family='com.exampleuser.PersonalAIMonitorMenuBar')"
            ).fetchone()[0],
            0,
        )

        explicit = self._new_item(
            "今天完成浩方二面",
            at=self.today.replace(hour=10),
            app="ChatGPT",
        )
        self.assertEqual(explicit["trigger_sentence"], "今天完成浩方二面")
        self.assertEqual(explicit["subject"]["company"], "浩方")
        self.assertEqual(explicit["subject"]["round"], "第二轮")
        self.assertEqual(explicit["subject_label"], "浩方第二轮面试")

        for index, summary in enumerate(
            (
                "天气预报误报但浩方二面已经结束",
                "我被面试官识别为适合人选今天浩方二面已完成",
                "浩方二面已完成可以生成感谢邮件",
                "讨论结束后今天完成浩方二面是一个真实案例",
                "今天完成浩方二面可以匹配下一轮岗位",
            )
        ):
            legitimate = self._new_item(
                summary,
                at=self.today.replace(hour=10, minute=20) + timedelta(minutes=index * 5),
                app=f"ChatGPT-Legitimate-{index}",
            )
            self.assertEqual(legitimate["trigger_sentence"], summary)

        for summary in (
            "浩方二面已完成可以生成感谢邮件",
            "今天完成浩方二面应该能生成复盘",
            "讨论结束后今天完成浩方二面是一个真实案例",
            "身份验证后今天完成浩方二面这段文本记录准确",
            "今天完成浩方二面可以匹配下一轮岗位",
        ):
            self.assertIsNone(INTERVIEW_META_LANGUAGE_RE.search(summary))

        first_round = self._new_item(
            "今天完成了浩方的初次面试",
            at=self.today.replace(hour=11),
            app="ChatGPT-First-Round",
        )
        self.assertEqual(first_round["subject"]["company"], "浩方")
        self.assertEqual(first_round["subject"]["round"], "第一轮")
        self.assertEqual(first_round["subject_label"], "浩方第一轮面试")

        generic_summaries = (
            "今天完成了一场技术面试",
            "今天完成线上面试",
            "今天完成HR面试",
            "今天完成一个面试",
            "今天完成业务面试",
            "今天完成招聘面试",
            "今天完成我的二面",
            "今天完成第二轮面试",
            "今天完成线上技术面试",
        )
        for index, summary in enumerate(generic_summaries):
            item = self._new_item(
                summary,
                at=self.today.replace(hour=12) + timedelta(minutes=index * 35),
                app=f"Generic-{index}",
            )
            self.assertEqual(item["subject"]["company"], "")
            self.assertIn("公司", item["subject_label"])

        quoted_meta = "避免为了挡住误报讨论而把真实的面试已完成句子一并过滤掉"
        self.conn.execute(
            "UPDATE activity_confirmation_items SET evidence=?,cleaned_content=? WHERE item_key=?",
            (quoted_meta, f"{quoted_meta}\n今天完成小鹏初面", explicit["item_key"]),
        )
        backfill_unresolved_confirmation_groups(self.conn)
        self.conn.commit()
        revalidated = self.conn.execute(
            "SELECT state,reason_codes FROM activity_confirmation_items WHERE item_key=?",
            (explicit["item_key"],),
        ).fetchone()
        self.assertEqual(revalidated["state"], "filtered")
        self.assertIn("schema26_non_occurrence_filtered", json.loads(revalidated["reason_codes"]))

    def test_question_groups_merge_only_compatible_sources(self) -> None:
        for index in range(8):
            self._insert_activity(
                "今天完成浩方二面",
                at=self.today.replace(hour=9) + timedelta(minutes=index * 5),
                app="ChatGPT",
            )
        payload = self._materialize()
        pending = [group for group in payload["question_groups"] if group["state"] == "pending"]
        self.assertEqual(len(pending), 1)
        merged = pending[0]
        self.assertEqual(merged["source_count"], 8)
        self.assertEqual(merged["subject_label"], "浩方第二轮面试")
        self.assertEqual(merged["trigger_sentence"], "今天完成浩方二面")
        self.assertEqual(merged["cleaned_contents"], [{"text": "今天完成浩方二面", "count": 8}])
        self.assertTrue(merged["group_signature"])
        self.assertTrue(merged["member_revision"].startswith("rev_"))
        self.assertEqual(payload["pending_question_count"], 1)
        self.assertEqual(payload["pending_source_count"], 8)

        self._insert_activity(
            "今天完成浩方初面",
            at=self.today.replace(hour=10),
            app="ChatGPT",
        )
        self._insert_activity(
            "今天完成小鹏二面",
            at=self.today.replace(hour=10, minute=5),
            app="ChatGPT",
        )
        self._insert_activity(
            "今天完成浩方二面",
            at=self.today.replace(hour=12),
            app="ChatGPT",
        )
        payload = self._materialize()
        labels = {group["subject_label"] for group in payload["question_groups"]}
        self.assertTrue({"浩方第二轮面试", "浩方第一轮面试", "小鹏第二轮面试"} <= labels)
        second_round_groups = [
            group for group in payload["question_groups"] if group["subject_label"] == "浩方第二轮面试"
        ]
        self.assertEqual(sorted(group["source_count"] for group in second_round_groups), [9])

        yesterday = self.today - timedelta(days=1)
        self._insert_activity(
            "今天完成浩方二面",
            at=yesterday.replace(hour=23, minute=55),
            app="ChatGPT",
        )
        materialize_activity_confirmations(
            self.conn,
            yesterday.isoformat(timespec="seconds"),
            self.today.isoformat(timespec="seconds"),
        )
        seven_day = list_activity_confirmations(self.conn, range_name="7d", include_resolved=True)
        cross_day_groups = [
            group for group in seven_day["question_groups"] if group["subject_label"] == "浩方第二轮面试"
        ]
        self.assertEqual(sorted(group["source_count"] for group in cross_day_groups), [1, 9])

        # The current policy groups the same concrete subject for the full local day,
        # regardless of capture gaps or App-family attribution drift.
        for minute in (0, 41):
            self._insert_activity(
                "今天完成理想三面",
                at=self.today.replace(hour=16, minute=minute),
                app="Thirty-Minute-Boundary",
            )
        boundary_groups = [
            group
            for group in self._materialize()["question_groups"]
            if group["app"] == "Thirty-Minute-Boundary"
        ]
        self.assertEqual(sorted(group["source_count"] for group in boundary_groups), [2])

    def test_incremental_materialization_regroups_exact_subject_for_the_affected_day(self) -> None:
        summary = "今天我已完成季度客户报告"
        first_at = self.today.replace(hour=9)
        self._insert_activity(summary, at=first_at, app="ChatGPT-Incremental")
        first_payload = self._materialize(
            first_at - timedelta(minutes=1),
            first_at + timedelta(minutes=11),
        )
        first_group = next(
            group
            for group in first_payload["question_groups"]
            if group["app"] == "ChatGPT-Incremental"
        )
        self.assertEqual(first_group["source_count"], 1)

        second_at = self.today.replace(hour=17)
        self._insert_activity(summary, at=second_at, app="ChatGPT-Incremental")
        materialize_activity_confirmations(
            self.conn,
            (second_at - timedelta(minutes=1)).isoformat(timespec="seconds"),
            (second_at + timedelta(minutes=11)).isoformat(timespec="seconds"),
        )
        current = list_activity_confirmations(self.conn, range_name="today")
        matching = [
            group
            for group in current["question_groups"]
            if group["subject_label"] == "季度客户报告的完成事项"
        ]
        self.assertEqual(len(matching), 1)
        merged = matching[0]
        self.assertEqual(merged["group_key"], first_group["group_key"])
        self.assertNotEqual(merged["member_revision"], first_group["member_revision"])
        self.assertEqual(merged["source_count"], 2)
        self.assertEqual(current["pending_source_count"], 2)

    def test_completion_prefix_grouping_is_conservative_and_keeps_a_stable_canonical_group(self) -> None:
        short = "今天我已完成客户报告扫描页码级溯源"
        medium = "今天我已完成客户报告扫描页码级溯源验收"
        long = "今天我已完成客户报告扫描页码级溯源验收报告"
        short_at = self.today.replace(hour=10)
        self._insert_activity(short, at=short_at, app="Word-Prefix")
        first = self._materialize(
            short_at - timedelta(minutes=1),
            short_at + timedelta(minutes=11),
        )
        first_group = next(
            group for group in first["question_groups"] if group["app"] == "Word-Prefix"
        )

        medium_at = short_at + timedelta(minutes=5)
        self._insert_activity(medium, at=medium_at, app="Word-Prefix")
        materialize_activity_confirmations(
            self.conn,
            (medium_at - timedelta(minutes=1)).isoformat(timespec="seconds"),
            (medium_at + timedelta(minutes=11)).isoformat(timespec="seconds"),
        )
        long_at = short_at + timedelta(minutes=10)
        self._insert_activity(long, at=long_at, app="unknown")
        materialize_activity_confirmations(
            self.conn,
            (long_at - timedelta(minutes=1)).isoformat(timespec="seconds"),
            (long_at + timedelta(minutes=11)).isoformat(timespec="seconds"),
        )
        merged_rows = self.conn.execute(
            "SELECT item_key,group_key,group_signature,subject,decision_question "
            "FROM activity_confirmation_items WHERE safe_observation IN (?,?,?) "
            "ORDER BY datetime(observed_start),item_key",
            (short, medium, long),
        ).fetchall()
        self.assertEqual(len(merged_rows), 3)
        self.assertEqual({row["group_key"] for row in merged_rows}, {first_group["group_key"]})
        self.assertEqual(len({row["group_signature"] for row in merged_rows}), 1)
        self.assertTrue(
            all(
                json.loads(row["subject"])["object"] == "客户报告扫描页码级溯源验收报告"
                for row in merged_rows
            )
        )
        self.assertTrue(
            all("客户报告扫描页码级溯源验收报告" in row["decision_question"] for row in merged_rows)
        )
        merged = next(
            group
            for group in list_activity_confirmations(self.conn, range_name="today")["question_groups"]
            if group["group_key"] == first_group["group_key"]
        )
        self.assertEqual(merged["subject_label"], "客户报告扫描页码级溯源验收报告的完成事项")
        self.assertEqual(merged["source_count"], 3)
        self.assertNotEqual(merged["member_revision"], first_group["member_revision"])

        cases = (
            (
                "今天我已完成海外销售渠道报告",
                "今天我已完成海外销售渠道报告复核",
                self.today.replace(hour=12),
                self.today.replace(hour=12, minute=31),
                "Chrome-Time",
                "Chrome-Time",
            ),
            (
                "今天我已完成产品路线图证据归档",
                "今天我已完成产品路线图证据归档验收",
                self.today.replace(hour=13),
                self.today.replace(hour=13, minute=5),
                "ChatGPT-App-A",
                "Obsidian-App-B",
            ),
            (
                "今天我已完成供应商评估报告",
                "今天我已完成市场进入研究报告",
                self.today.replace(hour=14),
                self.today.replace(hour=14, minute=5),
                "ChatGPT-Subjects",
                "ChatGPT-Subjects",
            ),
        )
        for left, right, left_at, right_at, left_app, right_app in cases:
            self._insert_activity(left, at=left_at, app=left_app)
            self._insert_activity(right, at=right_at, app=right_app)
        self._materialize()
        for left, right, *_ in cases:
            with self.subTest(left=left, right=right):
                rows = self.conn.execute(
                    "SELECT group_key FROM activity_confirmation_items "
                    "WHERE safe_observation IN (?,?) ORDER BY safe_observation",
                    (left, right),
                ).fetchall()
                self.assertEqual(len(rows), 2)
                self.assertEqual(len({row["group_key"] for row in rows}), 2)

    def test_unknown_subject_is_asked_anonymously_instead_of_filtered(self) -> None:
        rows = (
            ("今天完成面试", 9, 0),
            ("今天完成面试", 9, 5),
            ("今天结束面试", 9, 8),
            ("今天完成面试", 10, 0),
        )
        for summary, hour, minute in rows:
            self._insert_activity(
                summary,
                at=self.today.replace(hour=hour, minute=minute),
                app="Generic-Bounds",
            )
        payload = self._materialize()
        self.assertEqual(payload["pending_question_count"], 4)
        matching = [
            item
            for item in payload["items"]
            if item["app"] == "Generic-Bounds"
        ]
        self.assertEqual(len(matching), 4)
        self.assertTrue(all(item["state"] == "pending" for item in matching))
        self.assertTrue(all("anonymous_subject" in item["safe_signal_types"] for item in matching))

    def test_conditional_completion_copy_is_system_rejected_and_never_shown_as_pending(self) -> None:
        conditional = "现在已优化为：Desktop的异步子任务完成后显示待处理卡片"
        self._insert_activity(
            conditional,
            at=self.today.replace(hour=8),
            app="ChatGPT",
        )
        self._insert_activity(
            "今天我已完成客户市场报告",
            at=self.today.replace(hour=8, minute=20),
            app="Obsidian",
        )
        self._materialize()
        pending = list_activity_confirmations(self.conn, range_name="today")["question_groups"]
        self.assertNotIn(conditional, json.dumps(pending, ensure_ascii=False))
        self.assertTrue(any(group["subject"]["object"] == "客户市场报告" for group in pending))
        rejected = self.conn.execute(
            "SELECT state,answer,reason_codes FROM activity_confirmation_items WHERE safe_observation=?",
            (conditional,),
        ).fetchone()
        self.assertEqual((rejected["state"], rejected["answer"]), ("filtered", None))
        self.assertIn("policy_v7_conditional_completion", json.loads(rejected["reason_codes"]))

    def test_policy_v5_revalidates_v4_negated_and_game_ui_rows_without_user_side_effects(self) -> None:
        negated = "不能宣称整库已经完成 98% OCR 与扫描页码级溯源"
        game_ui = "挑战 +500XP 任务完成 等级6 Drakon狙击步枪蓝图 战术挂件蓝图"
        self._insert_activity(
            negated,
            at=self.today.replace(hour=8, minute=58),
            app="ChatGPT-Legacy-v4",
        )
        self._insert_activity(
            game_ui,
            at=self.today.replace(hour=14, minute=33),
            app="CoDBlkOps3_Exe",
        )
        self._materialize()
        item_keys = {
            row["safe_observation"]: row["item_key"]
            for row in self.conn.execute(
                "SELECT item_key,safe_observation FROM activity_confirmation_items "
                "WHERE safe_observation IN (?,?)",
                (negated, game_ui),
            ).fetchall()
        }
        self.assertEqual(set(item_keys), {negated, game_ui})
        self.conn.execute(
            "UPDATE activity_confirmation_items SET state='pending',answer=NULL,"
            "answer_reason=NULL,answered_at=NULL,clarification_note='',clarification='',"
            "reason_codes=? WHERE item_key IN (?,?)",
            (
                json.dumps(["possible_completion_occurrence", "policy_v4_evaluated"]),
                item_keys[negated],
                item_keys[game_ui],
            ),
        )
        self.conn.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            ("activity_confirmation_policy_version", "4", datetime.now().astimezone().isoformat()),
        )
        self.conn.commit()

        result = revalidate_unresolved_confirmation_policy(self.conn)

        self.assertEqual((result["status"], result["policy_version"]), ("applied", 7))
        self.assertEqual(CONFIRMATION_POLICY_VERSION, 7)
        rows = {
            row["safe_observation"]: row
            for row in self.conn.execute(
                "SELECT safe_observation,state,answer,answer_reason,user_note,clarification_note,"
                "reason_codes FROM activity_confirmation_items WHERE item_key IN (?,?)",
                (item_keys[negated], item_keys[game_ui]),
            ).fetchall()
        }
        self.assertEqual({(row["state"], row["answer"]) for row in rows.values()}, {("filtered", None)})
        self.assertIn("policy_v7_negated_occurrence", json.loads(rows[negated]["reason_codes"]))
        self.assertIn("policy_v7_game_ui", json.loads(rows[game_ui]["reason_codes"]))
        self.assertTrue(
            all(
                (row["answer_reason"], row["user_note"], row["clarification_note"])
                == (None, "", "")
                for row in rows.values()
            )
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM activity_confirmation_calibrations").fetchone()[0],
            0,
        )
        pending = list_activity_confirmations(self.conn, range_name="today")
        self.assertEqual((pending["pending_question_count"], pending["pending_source_count"]), (0, 0))

    def test_app_attribution_drift_does_not_split_same_concrete_subject(self) -> None:
        self._insert_activity(
            "今天完成浩方二面",
            at=self.today.replace(hour=14),
            app="ChatGPT",
        )
        self._insert_activity(
            "今天完成浩方二面",
            at=self.today.replace(hour=14, minute=5),
            app="unknown",
        )
        self._insert_activity(
            "今天完成浩方二面",
            at=self.today.replace(hour=14, minute=20),
            app="unknown",
        )
        groups = self._materialize()["question_groups"]
        matching = [group for group in groups if group["subject_label"] == "浩方第二轮面试"]
        self.assertEqual(sorted(group["source_count"] for group in matching), [3])

        self._insert_activity(
            "今天完成小鹏初面",
            at=self.today.replace(hour=16),
            app="ChatGPT",
        )
        self._insert_activity(
            "今天完成小鹏初面",
            at=self.today.replace(hour=16, minute=5),
            app="unknown",
        )
        self._insert_activity(
            "今天完成小鹏初面",
            at=self.today.replace(hour=16, minute=8),
            app="Chrome",
        )
        bridge_groups = [
            group
            for group in self._materialize()["question_groups"]
            if group["subject_label"] == "小鹏第一轮面试"
        ]
        self.assertEqual(sorted(group["source_count"] for group in bridge_groups), [3])

    def test_answer_group_stale_guard_atomicity_and_calibration_count_once(self) -> None:
        for minute in (0, 5):
            self._insert_activity(
                "今天完成浩方二面",
                at=self.today.replace(hour=9, minute=minute),
                app="ChatGPT-Group",
            )
        original_group = self._materialize()["question_groups"][0]
        self._insert_activity(
            "今天完成浩方二面",
            at=self.today.replace(hour=9, minute=8),
            app="ChatGPT-Group",
        )
        materialize_activity_confirmations(
            self.conn,
            self.today.isoformat(timespec="seconds"),
            (self.today + timedelta(days=1)).isoformat(timespec="seconds"),
        )
        stale = answer_activity_confirmation_group(
            self.conn,
            group_key=original_group["group_key"],
            member_revision=original_group["member_revision"],
            answer="not_occurred",
            reason="reference_only",
            workstream="面试求职",
            generalize_scope="similar",
        )
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["error"], "group_members_changed")
        self.assertEqual(stale["question_group"]["source_count"], 3)
        unchanged = self.conn.execute(
            "SELECT state,answer FROM activity_confirmation_items WHERE group_key=?",
            (original_group["group_key"],),
        ).fetchall()
        self.assertEqual({(row["state"], row["answer"]) for row in unchanged}, {("pending", None)})
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM activity_confirmation_calibrations").fetchone()[0],
            0,
        )
        refreshed = next(
            group
            for group in self._materialize()["question_groups"]
            if group["group_key"] == original_group["group_key"]
        )
        recorded = answer_activity_confirmation_group(
            self.conn,
            group_key=refreshed["group_key"],
            member_revision=refreshed["member_revision"],
            answer="not_occurred",
            reason="reference_only",
            workstream="面试求职",
            generalize_scope="similar",
        )
        self.assertEqual(recorded["status"], "recorded")
        answered = self.conn.execute(
            "SELECT state,answer FROM activity_confirmation_items WHERE group_key=?",
            (refreshed["group_key"],),
        ).fetchall()
        self.assertEqual({(row["state"], row["answer"]) for row in answered}, {("rejected", "not_occurred")})
        calibration = self.conn.execute(
            "SELECT negative_count,misrecognized_count,reason_counts "
            "FROM activity_confirmation_calibrations"
        ).fetchone()
        self.assertEqual((calibration["negative_count"], calibration["misrecognized_count"]), (1, 0))
        self.assertEqual(json.loads(calibration["reason_counts"]), {"reference_only": 1})
        blocked = answer_activity_confirmation_group(
            self.conn,
            group_key=refreshed["group_key"],
            member_revision=recorded["member_revision"],
            answer="occurred",
            reason="correct",
            workstream="面试求职",
        )
        self.assertEqual((blocked["status"], blocked["error"]), ("blocked", "group_already_resolved"))
        before_clarify = [
            tuple(row)
            for row in self.conn.execute(
                "SELECT item_key,state,answer,subject,evidence,clarification "
                "FROM activity_confirmation_items WHERE group_key=? ORDER BY item_key",
                (refreshed["group_key"],),
            ).fetchall()
        ]
        clarify_blocked = clarify_activity_confirmation_group(
            self.conn,
            group_key=refreshed["group_key"],
            member_revision=recorded["member_revision"],
        )
        self.assertEqual(
            (clarify_blocked["status"], clarify_blocked["error"]),
            ("blocked", "group_already_resolved"),
        )
        after_clarify = [
            tuple(row)
            for row in self.conn.execute(
                "SELECT item_key,state,answer,subject,evidence,clarification "
                "FROM activity_confirmation_items WHERE group_key=? ORDER BY item_key",
                (refreshed["group_key"],),
            ).fetchall()
        ]
        self.assertEqual(after_clarify, before_clarify)

        for minute in (0, 5):
            self._insert_activity(
                "今天完成小鹏初面",
                at=self.today.replace(hour=15, minute=minute),
                app="Atomic-App",
            )
        atomic_group = next(
            group
            for group in self._materialize()["question_groups"]
            if group["app"] == "Atomic-App"
        )
        with patch(
            "activity_mvp.activity_confirmations._update_confirmation_calibration",
            side_effect=RuntimeError("forced calibration failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "forced calibration failure"):
                answer_activity_confirmation_group(
                    self.conn,
                    group_key=atomic_group["group_key"],
                    member_revision=atomic_group["member_revision"],
                    answer="not_occurred",
                    reason="reference_only",
                    workstream="面试求职",
                    generalize_scope="similar",
                )
        rolled_back = self.conn.execute(
            "SELECT state,answer FROM activity_confirmation_items WHERE group_key=?",
            (atomic_group["group_key"],),
        ).fetchall()
        self.assertEqual({(row["state"], row["answer"]) for row in rolled_back}, {("pending", None)})

    def test_stable_group_key_owner_ignores_same_second_item_key_order(self) -> None:
        same_created_at = self.today.replace(hour=12).isoformat(timespec="seconds")
        cluster = [
            {
                "group_key": "grp_visible",
                "item_key": "zzz_old_member",
                "observed_start": self.today.replace(hour=9).isoformat(timespec="seconds"),
                "created_at": same_created_at,
            },
            {
                "group_key": "grp_visible",
                "item_key": "yyy_old_member",
                "observed_start": self.today.replace(hour=9, minute=5).isoformat(timespec="seconds"),
                "created_at": same_created_at,
            },
            {
                "group_key": "grp_new",
                "item_key": "aaa_new_member",
                "observed_start": self.today.replace(hour=9, minute=8).isoformat(timespec="seconds"),
                "created_at": same_created_at,
            },
        ]
        self.assertEqual(_stable_cluster_group_key(cluster), "grp_visible")

        # A singleton visible group also remains the owner when a later
        # observation arrives with a lexically smaller hashed item key.
        self.assertEqual(
            _stable_cluster_group_key([cluster[0], cluster[2]]),
            "grp_visible",
        )
        later_majority = [
            cluster[0],
            {
                **cluster[2],
                "group_key": "grp_later_majority",
                "observed_start": self.today.replace(
                    hour=9,
                    minute=5,
                ).isoformat(timespec="seconds"),
            },
            {
                **cluster[2],
                "group_key": "grp_later_majority",
                "item_key": "bbb_new_member",
                "observed_start": self.today.replace(
                    hour=9,
                    minute=6,
                ).isoformat(timespec="seconds"),
            },
        ]
        self.assertEqual(
            _stable_cluster_group_key(later_majority),
            "grp_visible",
        )
        backfill = [
            {
                **cluster[0],
                "group_key": "grp_visible",
                "observed_start": self.today.replace(
                    hour=10,
                ).isoformat(timespec="seconds"),
                "created_at": self.today.replace(
                    hour=11,
                ).isoformat(timespec="seconds"),
            },
            {
                **cluster[2],
                "group_key": "grp_backfill",
                "observed_start": self.today.replace(
                    hour=9,
                ).isoformat(timespec="seconds"),
                "created_at": self.today.replace(
                    hour=12,
                ).isoformat(timespec="seconds"),
            },
        ]
        self.assertEqual(
            _stable_cluster_group_key(backfill),
            "grp_visible",
        )

    def test_ignored_is_terminal_without_calibration_confirmed_fact_or_recent_answer(self) -> None:
        item = self._new_item(
            "今天完成浩方二面",
            at=self.today.replace(hour=9),
            app="ChatGPT-Ignored",
        )
        evidence = self._attach_confirmation_evidence(item["item_key"])
        self.conn.execute(
            "UPDATE activity_confirmation_items SET clarification_note=? WHERE item_key=?",
            ("这段说明不应随忽略保留", item["item_key"]),
        )
        self.conn.commit()

        recorded = answer_activity_confirmation(
            self.conn,
            item_key=item["item_key"],
            answer="ignored",
            reason="correct",
            note="也不应保存",
            generalize_scope="similar",
        )

        self.assertEqual(recorded["status"], "recorded")
        self.assertEqual(
            (
                recorded["item"]["state"],
                recorded["item"]["user_answer"],
                recorded["item"]["answer_reason"],
                recorded["item"]["generalize_scope"],
            ),
            ("ignored", "ignored", "user_ignored", "exact"),
        )
        stored = self.conn.execute(
            "SELECT state,answer,answer_reason,user_note,clarification_note "
            "FROM activity_confirmation_items WHERE item_key=?",
            (item["item_key"],),
        ).fetchone()
        self.assertEqual(
            tuple(stored),
            ("ignored", "ignored", "user_ignored", "", ""),
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM activity_confirmation_calibrations").fetchone()[0],
            0,
        )
        listed = list_activity_confirmations(self.conn, range_name="today")
        self.assertEqual(listed["question_groups"], [])
        self.assertNotIn(
            item["item_key"],
            {answer["item_key"] for answer in listed["recent_answers"]},
        )
        confirmed = confirmed_activity_view(
            self.conn,
            self.today.isoformat(timespec="seconds"),
            (self.today + timedelta(days=1)).isoformat(timespec="seconds"),
        )
        self.assertEqual(confirmed["user_confirmed_count"], 0)
        self.assertNotIn("今天完成浩方二面", json.dumps(confirmed["trusted_activities"], ensure_ascii=False))
        self.assertIn("今天完成浩方二面", json.dumps(confirmed["owner_activities"], ensure_ascii=False))
        evidence_row = self.conn.execute(
            "SELECT state,file_name FROM activity_confirmation_evidence_files WHERE evidence_id=?",
            (evidence["evidence_id"],),
        ).fetchone()
        self.assertEqual((evidence_row["state"], evidence_row["file_name"]), ("deleted", ""))
        self.assertFalse(evidence["path"].exists())

    def test_terminal_answer_releases_evidence_while_defer_retains_it(self) -> None:
        terminal = self._new_item(
            "今天完成浩方二面",
            at=self.today.replace(hour=9),
            app="ChatGPT-Terminal",
        )
        terminal_evidence = self._attach_confirmation_evidence(terminal["item_key"])
        self.conn.execute(
            "UPDATE activity_confirmation_items SET clarification_note='终态后清空' WHERE item_key=?",
            (terminal["item_key"],),
        )
        self.conn.commit()
        answer_activity_confirmation(
            self.conn,
            item_key=terminal["item_key"],
            answer="occurred",
            reason="correct",
        )
        terminal_row = self.conn.execute(
            "SELECT state,answer,clarification_note FROM activity_confirmation_items WHERE item_key=?",
            (terminal["item_key"],),
        ).fetchone()
        self.assertEqual(tuple(terminal_row), ("confirmed", "occurred", ""))
        terminal_file = self.conn.execute(
            "SELECT state,file_name FROM activity_confirmation_evidence_files WHERE evidence_id=?",
            (terminal_evidence["evidence_id"],),
        ).fetchone()
        self.assertEqual(tuple(terminal_file), ("deleted", ""))
        self.assertFalse(terminal_evidence["path"].exists())

        deferred = self._new_item(
            "今天完成小鹏初面",
            at=self.today.replace(hour=11),
            app="ChatGPT-Defer-Evidence",
        )
        deferred_evidence = self._attach_confirmation_evidence(deferred["item_key"])
        self.conn.execute(
            "UPDATE activity_confirmation_items SET clarification_note='暂时跳过后继续使用' "
            "WHERE item_key=?",
            (deferred["item_key"],),
        )
        self.conn.commit()
        deferred_result = answer_activity_confirmation(
            self.conn,
            item_key=deferred["item_key"],
            answer="defer",
        )
        self.assertEqual(
            (deferred_result["item"]["state"], deferred_result["item"]["evidence_status"]),
            ("deferred", "available"),
        )
        deferred_row = self.conn.execute(
            "SELECT state,answer,clarification_note FROM activity_confirmation_items WHERE item_key=?",
            (deferred["item_key"],),
        ).fetchone()
        self.assertEqual(
            tuple(deferred_row),
            ("deferred", "defer", "暂时跳过后继续使用"),
        )
        deferred_file = self.conn.execute(
            "SELECT state,file_name FROM activity_confirmation_evidence_files WHERE evidence_id=?",
            (deferred_evidence["evidence_id"],),
        ).fetchone()
        self.assertEqual(
            (deferred_file["state"], deferred_file["file_name"] != ""),
            ("available", True),
        )
        active_ref = self.conn.execute(
            "SELECT released_at FROM activity_confirmation_evidence_refs "
            "WHERE evidence_id=? AND item_key=?",
            (deferred_evidence["evidence_id"], deferred["item_key"]),
        ).fetchone()
        self.assertIsNone(active_ref["released_at"])
        self.assertTrue(deferred_evidence["path"].is_file())

    def test_answer_group_does_not_retro_materialize_other_day_sources(self) -> None:
        self._insert_activity(
            "今天完成浩方二面",
            at=self.today.replace(hour=9),
            app="Upgraded-Card",
        )
        group = self._materialize()["question_groups"][0]
        retro_source_id = self._insert_activity(
            "今天完成小鹏初面 升级前时间线",
            at=self.today.replace(hour=10),
            app="Retro-Source",
        )
        before_count = self.conn.execute(
            "SELECT COUNT(*) FROM activity_confirmation_items"
        ).fetchone()[0]
        recorded = answer_activity_confirmation_group(
            self.conn,
            group_key=group["group_key"],
            member_revision=group["member_revision"],
            answer="occurred",
            reason="correct",
            workstream="面试求职",
        )
        self.assertEqual(recorded["status"], "recorded")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM activity_confirmation_items").fetchone()[0],
            before_count,
        )
        persisted_source_ids = {
            source_id
            for row in self.conn.execute(
                "SELECT source_activity_ids FROM activity_confirmation_items"
            ).fetchall()
            for source_id in json.loads(row["source_activity_ids"])
        }
        self.assertNotIn(retro_source_id, persisted_source_ids)

    def test_confirmed_group_is_one_canonical_fact_for_every_downstream(self) -> None:
        summary = "今天完成浩方二面"
        for index in range(8):
            self._insert_activity(
                summary,
                at=self.today.replace(hour=9) + timedelta(minutes=index * 2),
                app="ChatGPT",
            )
        group = self._materialize()["question_groups"][0]
        answer_activity_confirmation_group(
            self.conn,
            group_key=group["group_key"],
            member_revision=group["member_revision"],
            answer="occurred",
            reason="correct",
            workstream="面试求职",
        )
        start = self.today.isoformat(timespec="seconds")
        end = (self.today + timedelta(days=1)).isoformat(timespec="seconds")
        view = confirmed_activity_view(self.conn, start, end)
        self.assertEqual(len(view["activities"]), 1)
        self.assertEqual(view["activities"][0]["confirmation_source_count"], 8)
        self.assertEqual(view["activities"][0]["confirmation_group_key"], group["group_key"])
        self.assertEqual(view["pending_confirmation_count"], 0)
        self.assertEqual(len(search(self.conn, "浩方二面", start=start, end=end, confirmed_only=True)), 1)
        exported = cleaned_timeline_export(self.conn, start, end)
        self.assertEqual(len(exported["activities"]), 1)
        report = report_context(self.conn, self.today.date().isoformat())
        self.assertEqual((report["clear_count"], len(report["activity_highlights"])), (1, 1))
        review = daily_review(self.conn, start, end)
        self.assertEqual(sum(summary in item for item in review["highlights"]), 1)
        handoff = build_ai_handoff_package(self.conn, start=start, end=end)
        self.assertEqual(handoff["summary"]["activity_count"], 1)
        with patch(
            "activity_mvp.ai_connector.resolve_ai_handoff_range",
            return_value=(start, end, "today"),
        ):
            connector = build_ai_connector_package(self.conn, range_name="today")
        self.assertEqual(connector["summary"]["activity_count"], 1)
        package = generate_package(
            self.conn,
            start,
            end,
            self.root / "canonical-package",
            write_markdown=False,
        )["package"]
        self.assertEqual(len(package["activities"]), 1)

    def test_delete_one_group_source_preserves_card_until_last_source_is_deleted(self) -> None:
        self._insert_activity(
            "今天完成浩方二面",
            at=self.today.replace(hour=9),
            app="ChatGPT",
        )
        self._insert_activity(
            "今天完成浩方二面",
            at=self.today.replace(hour=9, minute=25),
            app="ChatGPT",
        )
        group = self._materialize()["question_groups"][0]
        self.assertEqual(group["source_count"], 2)
        answer_activity_confirmation_group(
            self.conn,
            group_key=group["group_key"],
            member_revision=group["member_revision"],
            answer="not_occurred",
            reason="reference_only",
            workstream="面试求职",
            generalize_scope="similar",
        )
        calibration_key = self.conn.execute(
            "SELECT calibration_key FROM activity_confirmation_items WHERE group_key=? LIMIT 1",
            (group["group_key"],),
        ).fetchone()[0]
        first_start = self.today.replace(hour=8, minute=59).isoformat(timespec="seconds")
        first_end = self.today.replace(hour=9, minute=15).isoformat(timespec="seconds")
        preview = preview_delete_range(self.conn, first_start, first_end)
        self.assertEqual(preview["cascade"]["confirmation_items_deleted"], 1)
        delete_range(self.conn, first_start, first_end)
        remaining = list_activity_confirmations(
            self.conn,
            range_name="today",
            include_resolved=True,
        )
        self.assertEqual(remaining["question_groups"][0]["source_count"], 1)
        self.assertEqual(remaining["question_groups"][0]["group_key"], group["group_key"])

        last_start = self.today.replace(hour=9, minute=20).isoformat(timespec="seconds")
        last_end = self.today.replace(hour=9, minute=40).isoformat(timespec="seconds")
        self.assertEqual(
            preview_delete_range(self.conn, last_start, last_end)["cascade"]["confirmation_items_deleted"],
            1,
        )
        delete_range(self.conn, last_start, last_end)
        empty = list_activity_confirmations(self.conn, range_name="today", include_resolved=True)
        self.assertEqual(empty["question_groups"], [])
        self.assertIsNotNone(
            self.conn.execute(
                "SELECT 1 FROM activity_confirmation_calibrations WHERE calibration_key=?",
                (calibration_key,),
            ).fetchone()
        )
        self.assertEqual(
            revoke_confirmation_calibration(self.conn, calibration_key=calibration_key)["status"],
            "revoked",
        )

    def test_delete_wins_over_materialization_that_already_read_the_source(self) -> None:
        self._insert_activity(
            "今天完成浩方二面",
            at=self.today.replace(hour=9),
            app="Race-App",
        )
        source_read = threading.Event()
        allow_upsert = threading.Event()
        delete_started = threading.Event()
        delete_done = threading.Event()
        failures: list[BaseException] = []
        start = self.today.isoformat(timespec="seconds")
        end = (self.today + timedelta(days=1)).isoformat(timespec="seconds")

        def paused_timeline(conn, query_start=None, query_end=None):
            rows = timeline(conn, query_start, query_end)
            source_read.set()
            if not allow_upsert.wait(5):
                raise TimeoutError("materialization race barrier timed out")
            return rows

        def run_materialize() -> None:
            conn = ensure_ready(self.db)
            try:
                materialize_activity_confirmations(conn, start, end)
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)
            finally:
                conn.close()

        def run_delete() -> None:
            delete_started.set()
            conn = ensure_ready(self.db)
            try:
                delete_range(conn, start, end)
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)
            finally:
                conn.close()
                delete_done.set()

        with patch("activity_mvp.pipeline.timeline", side_effect=paused_timeline):
            materializer = threading.Thread(target=run_materialize)
            materializer.start()
            self.assertTrue(source_read.wait(5))
            deleter = threading.Thread(target=run_delete)
            deleter.start()
            self.assertTrue(delete_started.wait(5))
            # The write-intent transaction must keep deletion from committing
            # between the timeline read and source-bound upsert.
            self.assertFalse(delete_done.wait(0.2))
            allow_upsert.set()
            materializer.join(5)
            deleter.join(5)

        self.assertFalse(materializer.is_alive())
        self.assertFalse(deleter.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM extracted_activities").fetchone()[0], 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM activity_confirmation_items").fetchone()[0],
            0,
        )

    def test_privacy_reclassification_cascades_removed_confirmation_sources(self) -> None:
        adult_id = self._insert_activity(
            "今天完成浩方二面",
            at=self.today.replace(hour=9),
            app="Chrome",
        )
        medical_id = self._insert_activity(
            "今天完成小鹏初面",
            at=self.today.replace(hour=10),
            app="ChatGPT",
        )
        self.assertEqual(len(self._materialize()["question_groups"]), 2)
        sensitive_rows = (
            (
                adult_id,
                "Chrome / pornhub.com：今天完成浩方二面 PRIVATE_ADULT_TEXT",
                "Chrome",
                "pornhub.com",
            ),
            (
                medical_id,
                "ChatGPT：今天完成小鹏初面；我的体检报告血常规异常，医生建议复诊用药。",
                "ChatGPT",
                "chatgpt.com",
            ),
        )
        for activity_id, sensitive_text, app, domain in sensitive_rows:
            row = self.conn.execute(
                "SELECT deletion_group_id FROM extracted_activities WHERE id=?",
                (activity_id,),
            ).fetchone()
            self.conn.execute(
                "UPDATE extracted_activities SET activity_summary=?,apps=?,domains=? WHERE id=?",
                (
                    sensitive_text,
                    json.dumps([app], ensure_ascii=False),
                    json.dumps([domain], ensure_ascii=False),
                    activity_id,
                ),
            )
            self.conn.execute(
                "UPDATE frame_signals SET app_name=?,domain=?,window_title_redacted=?,"
                "ocr_redacted_summary=? WHERE deletion_group_id=?",
                (app, domain, sensitive_text, sensitive_text, row["deletion_group_id"]),
            )
        self.conn.commit()

        result = reclassify_existing(self.conn)
        self.assertEqual(result["blocked"], 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM extracted_activities").fetchone()[0], 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM activity_confirmation_items").fetchone()[0],
            0,
        )
        encoded = json.dumps(
            list_activity_confirmations(self.conn, range_name="today", include_resolved=True),
            ensure_ascii=False,
        )
        self.assertNotIn("PRIVATE_ADULT_TEXT", encoded)
        self.assertNotIn("血常规", encoded)

    def test_partial_redaction_rebuilds_confirmation_without_old_window_text(self) -> None:
        activity_id = self._insert_activity(
            "今天完成浩方二面；客户A 私聊正文",
            at=self.today.replace(hour=11),
            app="Feishu",
        )
        self.assertEqual(len(self._materialize()["question_groups"]), 1)
        deletion_group_id = self.conn.execute(
            "SELECT deletion_group_id FROM extracted_activities WHERE id=?",
            (activity_id,),
        ).fetchone()[0]
        self.conn.execute(
            "UPDATE frame_signals SET window_title_redacted=?,ocr_redacted_summary=? "
            "WHERE deletion_group_id=?",
            (
                "客户A 私聊正文",
                "今天完成浩方二面；客户A 私聊正文",
                deletion_group_id,
            ),
        )
        self.conn.commit()

        result = reclassify_existing(self.conn)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM activity_confirmation_items").fetchone()[0],
            0,
        )
        frame = self.conn.execute(
            "SELECT window_title_redacted,ocr_redacted_summary FROM frame_signals "
            "WHERE deletion_group_id=?",
            (deletion_group_id,),
        ).fetchone()
        self.assertEqual(frame["window_title_redacted"], "")
        encoded = json.dumps(
            list_activity_confirmations(self.conn, range_name="today", include_resolved=True),
            ensure_ascii=False,
        )
        self.assertNotIn("客户A", encoded)
        self.assertNotIn("私聊正文", encoded)

    def test_clarify_rewrites_from_unique_safe_context_without_answer_side_effect(self) -> None:
        item = self._new_item(
            "今天完成浩方面试",
            at=self.today.replace(hour=9),
            app="ChatGPT",
        )
        group = self._materialize()["question_groups"][0]
        self.assertEqual(group["subject"]["company"], "浩方")
        self.assertEqual(group["subject"]["round"], "")
        self.conn.execute(
            "UPDATE activity_confirmation_items SET cleaned_content=? WHERE item_key=?",
            ("今天完成浩方二面", item["item_key"]),
        )
        self.conn.commit()
        result = clarify_activity_confirmation_group(
            self.conn,
            group_key=group["group_key"],
            member_revision=group["member_revision"],
        )
        self.assertEqual((result["status"], result["result"]["outcome"]), ("clarified", "rewritten"))
        self.assertEqual(result["result"]["question_groups"][0]["subject_label"], "浩方第二轮面试")
        self.assertFalse(result["raw_access"])
        row = self.conn.execute(
            "SELECT state,answer FROM activity_confirmation_items WHERE item_key=?",
            (item["item_key"],),
        ).fetchone()
        self.assertEqual((row["state"], row["answer"]), ("pending", None))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM activity_confirmation_calibrations").fetchone()[0],
            0,
        )
        reloaded_group = next(
            item
            for item in list_activity_confirmations(self.conn, range_name="today")["question_groups"]
            if item["group_key"] == result["group_key"]
        )
        self.assertEqual(reloaded_group["subject_label"], "浩方第二轮面试")
        self.assertEqual(reloaded_group["clarification"]["status"], "completed")
        answered_after_clarify = answer_activity_confirmation_group(
            self.conn,
            group_key=reloaded_group["group_key"],
            member_revision=reloaded_group["member_revision"],
            answer="occurred",
            reason="correct",
            workstream="面试求职",
        )
        self.assertEqual(answered_after_clarify["status"], "recorded")
        self.assertEqual(answered_after_clarify["question_group"]["subject_label"], "浩方第二轮面试")

    def test_clarify_with_available_evidence_calls_local_ocr_helper(self) -> None:
        item = self._new_item(
            "今天完成浩方面试",
            at=self.today.replace(hour=9),
            app="ChatGPT-Retained-Evidence",
        )
        group = self._materialize()["question_groups"][0]
        retained = self._attach_confirmation_evidence(item["item_key"])

        def reviewed_event(path: Path, base_event: dict, *, root_dir: Path) -> dict:
            self.assertEqual(path, retained["path"])
            self.assertEqual(root_dir, self.root.resolve())
            return {**base_event, "ocr_text": "今天完成浩方二面"}

        with patch(
            "activity_mvp.macos_probe.build_ocr_event_from_confirmation_image",
            side_effect=reviewed_event,
        ) as local_ocr:
            result = clarify_activity_confirmation_group(
                self.conn,
                group_key=group["group_key"],
                member_revision=group["member_revision"],
            )

        local_ocr.assert_called_once()
        self.assertEqual(
            (
                result["status"],
                result["result"]["outcome"],
                result["result"]["severity"],
                result["result"]["evidence_status"],
            ),
            ("clarified", "rewritten", "success", "available"),
        )
        self.assertEqual(
            (result["result"]["evidence_count"], result["result"]["available_source_count"]),
            (1, 1),
        )
        self.assertIn("retained_frame_ocr", result["result"]["inputs_used"])
        self.assertIn("可用本机证据", result["result"]["message"])
        self.assertEqual(result["result"]["question_groups"][0]["subject_label"], "浩方第二轮面试")
        row = self.conn.execute(
            "SELECT state,answer,clarification_note,subject FROM activity_confirmation_items "
            "WHERE item_key=?",
            (item["item_key"],),
        ).fetchone()
        self.assertEqual(tuple(row)[:3], ("pending", None, ""))
        self.assertEqual(json.loads(row["subject"])["round"], "第二轮")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM activity_confirmation_calibrations").fetchone()[0],
            0,
        )
        evidence_row = self.conn.execute(
            "SELECT state,file_name FROM activity_confirmation_evidence_files WHERE evidence_id=?",
            (retained["evidence_id"],),
        ).fetchone()
        self.assertEqual((evidence_row["state"], evidence_row["file_name"] != ""), ("available", True))
        self.assertTrue(retained["path"].is_file())

    def test_clarify_keeps_each_retained_image_bound_to_its_item_when_group_splits(self) -> None:
        first = self._new_item(
            "今天完成浩方面试",
            at=self.today.replace(hour=9),
            app="ChatGPT-Path-Bound-A",
        )
        second = self._new_item(
            "今天完成浩方面试",
            at=self.today.replace(hour=11),
            app="ChatGPT-Path-Bound-B",
        )
        first_group = self.conn.execute(
            "SELECT group_key,group_signature FROM activity_confirmation_items WHERE item_key=?",
            (first["item_key"],),
        ).fetchone()
        self.conn.execute(
            "UPDATE activity_confirmation_items SET group_key=?,group_signature=? "
            "WHERE item_key IN (?,?)",
            (
                first_group["group_key"],
                first_group["group_signature"],
                first["item_key"],
                second["item_key"],
            ),
        )
        self.conn.commit()
        group = next(
            candidate
            for candidate in list_activity_confirmations(
                self.conn,
                range_name="today",
                include_resolved=True,
            )["question_groups"]
            if candidate["group_key"] == first_group["group_key"]
        )
        rows = self.conn.execute(
            "SELECT item_key FROM activity_confirmation_items WHERE group_key=? "
            "ORDER BY datetime(observed_start),item_key",
            (group["group_key"],),
        ).fetchall()
        self.assertEqual(len(rows), 2)
        first_key, second_key = (str(row["item_key"]) for row in rows)
        first_evidence = self._attach_confirmation_evidence(first_key)
        second_evidence = self._attach_confirmation_evidence(second_key)
        ocr_by_path = {
            first_evidence["path"]: "今天完成浩方二面",
            second_evidence["path"]: "今天完成小鹏初面",
        }

        def reviewed_event(path: Path, base_event: dict, *, root_dir: Path) -> dict:
            self.assertIn(path, ocr_by_path)
            self.assertEqual(root_dir, self.root.resolve())
            return {**base_event, "ocr_text": ocr_by_path[path]}

        with patch(
            "activity_mvp.macos_probe.build_ocr_event_from_confirmation_image",
            side_effect=reviewed_event,
        ) as local_ocr:
            result = clarify_activity_confirmation_group(
                self.conn,
                group_key=group["group_key"],
                member_revision=group["member_revision"],
            )

        self.assertEqual(local_ocr.call_count, 2)
        self.assertEqual(
            {call.args[0] for call in local_ocr.call_args_list},
            set(ocr_by_path),
        )
        self.assertEqual(
            (
                result["status"],
                result["result"]["outcome"],
                result["result"]["severity"],
                result["result"]["evidence_status"],
            ),
            ("clarified", "split", "success", "available"),
        )
        self.assertEqual(
            (result["result"]["evidence_count"], result["result"]["available_source_count"]),
            (2, 2),
        )
        self.assertEqual(
            {group["subject_label"] for group in result["result"]["question_groups"]},
            {"浩方第二轮面试", "小鹏第一轮面试"},
        )
        stored = {
            str(row["item_key"]): json.loads(row["subject"])
            for row in self.conn.execute(
                "SELECT item_key,subject FROM activity_confirmation_items "
                "WHERE item_key IN (?,?)",
                (first_key, second_key),
            ).fetchall()
        }
        self.assertEqual(stored[first_key]["label"], "浩方第二轮面试")
        self.assertEqual(stored[second_key]["label"], "小鹏第一轮面试")
        evidence_links = {
            str(row["evidence_id"]): str(row["item_key"])
            for row in self.conn.execute(
                "SELECT evidence_id,item_key FROM activity_confirmation_evidence_refs "
                "WHERE evidence_id IN (?,?) AND released_at IS NULL",
                (first_evidence["evidence_id"], second_evidence["evidence_id"]),
            ).fetchall()
        }
        self.assertEqual(
            evidence_links,
            {
                first_evidence["evidence_id"]: first_key,
                second_evidence["evidence_id"]: second_key,
            },
        )
        self.assertTrue(first_evidence["path"].is_file())
        self.assertTrue(second_evidence["path"].is_file())

    def test_temporary_local_ocr_failure_retains_evidence_and_retry_clears_error(self) -> None:
        item = self._new_item(
            "今天完成浩方面试",
            at=self.today.replace(hour=10),
            app="ChatGPT-OCR-Retry",
        )
        group = next(
            candidate
            for candidate in self._materialize()["question_groups"]
            if candidate["app"] == "ChatGPT-OCR-Retry"
        )
        retained = self._attach_confirmation_evidence(item["item_key"])

        with patch(
            "activity_mvp.macos_probe.build_ocr_event_from_confirmation_image",
            side_effect=RuntimeError("apple vision temporarily unavailable"),
        ) as failed_ocr:
            failed = clarify_activity_confirmation_group(
                self.conn,
                group_key=group["group_key"],
                member_revision=group["member_revision"],
            )

        failed_ocr.assert_called_once()
        self.assertEqual(
            (
                failed["status"],
                failed["result"]["outcome"],
                failed["result"]["severity"],
                failed["result"]["evidence_status"],
            ),
            ("clarified", "insufficient_information", "warning", "available"),
        )
        self.assertIn("原截图仍保留", failed["result"]["message"])
        item_after_failure = self.conn.execute(
            "SELECT state,answer FROM activity_confirmation_items WHERE item_key=?",
            (item["item_key"],),
        ).fetchone()
        self.assertEqual(tuple(item_after_failure), ("pending", None))
        file_after_failure = self.conn.execute(
            "SELECT state,file_name,last_error FROM activity_confirmation_evidence_files "
            "WHERE evidence_id=?",
            (retained["evidence_id"],),
        ).fetchone()
        self.assertEqual(
            (file_after_failure["state"], file_after_failure["last_error"]),
            ("available", "ocr_retry_pending"),
        )
        self.assertTrue(file_after_failure["file_name"])
        self.assertTrue(retained["path"].is_file())
        ref_after_failure = self.conn.execute(
            "SELECT released_at,release_reason FROM activity_confirmation_evidence_refs "
            "WHERE evidence_id=? AND item_key=?",
            (retained["evidence_id"], item["item_key"]),
        ).fetchone()
        self.assertEqual(tuple(ref_after_failure), (None, ""))

        retry_group = failed["result"]["question_groups"][0]
        with patch(
            "activity_mvp.macos_probe.build_ocr_event_from_confirmation_image",
            side_effect=lambda path, base_event, *, root_dir: {
                **base_event,
                "ocr_text": "今天完成浩方二面",
            },
        ) as successful_ocr:
            retried = clarify_activity_confirmation_group(
                self.conn,
                group_key=retry_group["group_key"],
                member_revision=retry_group["member_revision"],
            )

        successful_ocr.assert_called_once()
        self.assertEqual(
            (
                retried["status"],
                retried["result"]["outcome"],
                retried["result"]["severity"],
                retried["result"]["evidence_status"],
            ),
            ("clarified", "rewritten", "success", "available"),
        )
        self.assertEqual(retried["result"]["question_groups"][0]["subject_label"], "浩方第二轮面试")
        file_after_retry = self.conn.execute(
            "SELECT state,file_name,last_error FROM activity_confirmation_evidence_files "
            "WHERE evidence_id=?",
            (retained["evidence_id"],),
        ).fetchone()
        self.assertEqual(
            (file_after_retry["state"], file_after_retry["last_error"]),
            ("available", ""),
        )
        self.assertTrue(file_after_retry["file_name"])
        self.assertTrue(retained["path"].is_file())
        ref_after_retry = self.conn.execute(
            "SELECT released_at,release_reason FROM activity_confirmation_evidence_refs "
            "WHERE evidence_id=? AND item_key=?",
            (retained["evidence_id"], item["item_key"]),
        ).fetchone()
        self.assertEqual(tuple(ref_after_retry), (None, ""))

    def test_clarify_reports_corrupt_and_missing_retained_images_as_unavailable(self) -> None:
        corrupt = self._new_item(
            "今天完成浩方面试",
            at=self.today.replace(hour=8),
            app="ChatGPT-Corrupt-Evidence",
        )
        corrupt_group = next(
            group
            for group in self._materialize()["question_groups"]
            if group["app"] == "ChatGPT-Corrupt-Evidence"
        )
        corrupt_evidence = self._attach_confirmation_evidence(corrupt["item_key"])
        corrupt_evidence["path"].write_bytes(b"not-a-real-png")
        corrupt_evidence["path"].chmod(0o600)
        with patch(
            "activity_mvp.macos_probe.build_ocr_event_from_confirmation_image",
            side_effect=RuntimeError("corrupt image"),
        ) as corrupt_ocr:
            corrupt_result = clarify_activity_confirmation_group(
                self.conn,
                group_key=corrupt_group["group_key"],
                member_revision=corrupt_group["member_revision"],
            )
        corrupt_ocr.assert_not_called()
        self.assertEqual(
            (
                corrupt_result["result"]["outcome"],
                corrupt_result["result"]["severity"],
                corrupt_result["result"]["evidence_status"],
                corrupt_result["result"]["evidence_count"],
            ),
            ("evidence_unavailable", "warning", "unavailable", 0),
        )
        self.assertIn("原截图不可用", corrupt_result["result"]["message"])
        corrupt_file = self.conn.execute(
            "SELECT state,file_name FROM activity_confirmation_evidence_files WHERE evidence_id=?",
            (corrupt_evidence["evidence_id"],),
        ).fetchone()
        self.assertEqual(tuple(corrupt_file), ("deleted", ""))
        self.assertFalse(corrupt_evidence["path"].exists())

        missing = self._new_item(
            "今天完成小鹏面试",
            at=self.today.replace(hour=15),
            app="ChatGPT-Missing-Evidence",
        )
        missing_group = next(
            group
            for group in self._materialize()["question_groups"]
            if group["app"] == "ChatGPT-Missing-Evidence"
        )
        missing_evidence = self._attach_confirmation_evidence(missing["item_key"])
        missing_evidence["path"].unlink()
        reconciled = reconcile_confirmation_evidence(self.conn, self.root)
        self.assertEqual(reconciled["missing_marked"], 1)
        missing_file = self.conn.execute(
            "SELECT state,file_name,last_error FROM activity_confirmation_evidence_files "
            "WHERE evidence_id=?",
            (missing_evidence["evidence_id"],),
        ).fetchone()
        self.assertEqual(tuple(missing_file), ("unavailable", "", "source_missing"))
        with patch(
            "activity_mvp.macos_probe.build_ocr_event_from_confirmation_image"
        ) as missing_ocr:
            missing_result = clarify_activity_confirmation_group(
                self.conn,
                group_key=missing_group["group_key"],
                member_revision=missing_group["member_revision"],
            )
        missing_ocr.assert_not_called()
        self.assertEqual(
            (
                missing_result["result"]["outcome"],
                missing_result["result"]["severity"],
                missing_result["result"]["evidence_status"],
                missing_result["result"]["evidence_count"],
            ),
            ("evidence_unavailable", "warning", "unavailable", 0),
        )
        self.assertIn("原截图不可用", missing_result["result"]["message"])

    def test_exact_july_19_ocr_claim_clarify_is_system_filtered_without_answer(self) -> None:
        observed = datetime.fromisoformat("2026-07-19T08:58:00+08:00")
        day_start = observed.replace(hour=0, minute=0)
        day_end = day_start + timedelta(days=1)
        phrase = "不能宣称整库已经完成 98% OCR 与扫描页码级溯源"
        self._insert_activity(phrase, at=observed, app="ChatGPT")
        fixed_day = self._materialize(day_start, day_end)
        item = next(entry for entry in fixed_day["items"] if entry["safe_observation"] == phrase)
        self.assertEqual((item["state"], item["user_answer"]), ("filtered", ""))
        self.conn.execute(
            "UPDATE activity_confirmation_items SET state='pending',answer=NULL,"
            "answer_reason=NULL,answered_at=NULL,reason_codes=?,clarification='' WHERE item_key=?",
            (json.dumps(["possible_completion_occurrence"]), item["item_key"]),
        )
        self.conn.commit()
        group = next(
            candidate
            for candidate in list_activity_confirmations(
                self.conn,
                start=day_start.isoformat(timespec="seconds"),
                end=day_end.isoformat(timespec="seconds"),
                include_resolved=True,
            )["question_groups"]
            if candidate["group_key"] == item["group_key"]
        )
        retained = self._attach_confirmation_evidence(item["item_key"])

        with patch(
            "activity_mvp.macos_probe.build_ocr_event_from_confirmation_image",
            side_effect=lambda path, base_event, *, root_dir: {**base_event, "ocr_text": phrase},
        ) as local_ocr:
            result = clarify_activity_confirmation_group(
                self.conn,
                group_key=group["group_key"],
                member_revision=group["member_revision"],
                note="我不确定，请重新梳理该问题",
            )

        local_ocr.assert_called_once()
        self.assertEqual(
            (
                result["status"],
                result["result"]["outcome"],
                result["result"]["severity"],
                result["result"]["evidence_status"],
            ),
            ("clarified", "system_filtered", "success", "available"),
        )
        self.assertEqual(result["result"]["question_groups"], [])
        self.assertIn("已重新读取原截图", result["result"]["message"])
        self.assertIn("retained_frame_ocr", result["result"]["inputs_used"])
        row = self.conn.execute(
            "SELECT state,answer,answer_reason,user_note,clarification_note,answered_at,clarification "
            "FROM activity_confirmation_items WHERE item_key=?",
            (item["item_key"],),
        ).fetchone()
        self.assertEqual(tuple(row)[:6], ("filtered", None, None, "", "", None))
        self.assertEqual(json.loads(row["clarification"])["outcome"], "system_filtered")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM activity_confirmation_calibrations").fetchone()[0],
            0,
        )
        listed = list_activity_confirmations(
            self.conn,
            start=day_start.isoformat(timespec="seconds"),
            end=day_end.isoformat(timespec="seconds"),
            include_resolved=True,
        )
        self.assertNotIn(item["item_key"], {entry["item_key"] for entry in listed["recent_answers"]})
        confirmed = confirmed_activity_view(
            self.conn,
            day_start.isoformat(timespec="seconds"),
            day_end.isoformat(timespec="seconds"),
        )
        self.assertEqual((confirmed["trusted_activities"], confirmed["user_confirmed_count"]), ([], 0))
        self.assertEqual(confirmed["owner_activities"][0]["verification_state"], "unverified")
        evidence_row = self.conn.execute(
            "SELECT state,file_name FROM activity_confirmation_evidence_files WHERE evidence_id=?",
            (retained["evidence_id"],),
        ).fetchone()
        self.assertEqual(tuple(evidence_row), ("deleted", ""))
        self.assertFalse(retained["path"].exists())

    def test_clarify_game_context_note_filters_without_answer_or_calibration(self) -> None:
        phrase = "挑战 +500XP 任务完成 等级6 Drakon狙击步枪蓝图 战术挂件蓝图"
        self._insert_activity(
            phrase,
            at=self.today.replace(hour=14, minute=33),
            app="CoDBlkOps3_Exe",
        )
        self._materialize()
        item = self.conn.execute(
            "SELECT item_key,group_key FROM activity_confirmation_items WHERE safe_observation=?",
            (phrase,),
        ).fetchone()
        self.assertIsNotNone(item)
        self.conn.execute(
            "UPDATE activity_confirmation_items SET state='pending',answer=NULL,"
            "answer_reason=NULL,answered_at=NULL,clarification='',clarification_note='',"
            "reason_codes=? WHERE item_key=?",
            (json.dumps(["possible_completion_occurrence", "policy_v4_evaluated"]), item["item_key"]),
        )
        self.conn.commit()
        group = next(
            candidate
            for candidate in list_activity_confirmations(self.conn, range_name="today")["question_groups"]
            if candidate["group_key"] == item["group_key"]
        )

        result = clarify_activity_confirmation_group(
            self.conn,
            group_key=group["group_key"],
            member_revision=group["member_revision"],
            note="这很明显是游戏画面，直接忽略这种就可以了",
        )

        self.assertEqual(
            (result["status"], result["result"]["outcome"], result["result"]["severity"]),
            ("clarified", "system_filtered", "success"),
        )
        self.assertEqual(result["result"]["question_groups"], [])
        row = self.conn.execute(
            "SELECT state,answer,answer_reason,user_note,clarification_note,answered_at,"
            "reason_codes,clarification FROM activity_confirmation_items WHERE item_key=?",
            (item["item_key"],),
        ).fetchone()
        self.assertEqual(tuple(row)[:6], ("filtered", None, None, "", "", None))
        reason_codes = json.loads(row["reason_codes"])
        self.assertIn("policy_v7_user_context_only", reason_codes)
        self.assertEqual(json.loads(row["clarification"])["outcome"], "system_filtered")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM activity_confirmation_calibrations").fetchone()[0],
            0,
        )
        confirmed = confirmed_activity_view(
            self.conn,
            self.today.isoformat(timespec="seconds"),
            (self.today + timedelta(days=1)).isoformat(timespec="seconds"),
        )
        self.assertEqual((confirmed["trusted_activities"], confirmed["user_confirmed_count"]), ([], 0))
        self.assertEqual(confirmed["owner_activities"][0]["verification_state"], "unverified")

    def test_clarify_note_never_becomes_occurrence_and_blank_retry_stays_fail_closed(self) -> None:
        ordinary = self._new_item(
            "今天我已完成季度渠道复盘",
            at=self.today.replace(hour=9),
            app="ChatGPT-Ordinary-Note",
        )
        ordinary_group = next(
            group
            for group in self._materialize()["question_groups"]
            if group["app"] == "ChatGPT-Ordinary-Note"
        )
        with_note = clarify_activity_confirmation_group(
            self.conn,
            group_key=ordinary_group["group_key"],
            member_revision=ordinary_group["member_revision"],
            note="这件事现实中确实发生了",
        )
        self.assertEqual((with_note["status"], with_note["result"]["outcome"]), ("clarified", "evidence_unavailable"))
        ordinary_row = self.conn.execute(
            "SELECT state,answer,user_note,clarification_note FROM activity_confirmation_items "
            "WHERE item_key=?",
            (ordinary["item_key"],),
        ).fetchone()
        self.assertEqual(tuple(ordinary_row)[:3], ("pending", None, ""))
        self.assertEqual(ordinary_row["clarification_note"], "这件事现实中确实发生了")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM activity_confirmation_calibrations").fetchone()[0],
            0,
        )

        blank_item = self._new_item(
            "今天我已完成供应商准入复核",
            at=self.today.replace(hour=16),
            app="ChatGPT-Blank-Clarify",
        )
        blank_group = next(
            group
            for group in self._materialize()["question_groups"]
            if group["app"] == "ChatGPT-Blank-Clarify"
        )
        blank = clarify_activity_confirmation_group(
            self.conn,
            group_key=blank_group["group_key"],
            member_revision=blank_group["member_revision"],
            note="",
        )
        self.assertEqual(
            (
                blank["status"],
                blank["result"]["outcome"],
                blank["result"]["severity"],
                blank["result"]["evidence_status"],
            ),
            ("clarified", "evidence_unavailable", "warning", "unavailable"),
        )
        self.assertIn("原截图不可用", blank["result"]["message"])
        self.assertNotIn("已重新读取原截图", blank["result"]["message"])
        self.assertNotIn("user_note", blank["result"]["inputs_used"])
        blank_row = self.conn.execute(
            "SELECT state,answer,user_note,clarification_note FROM activity_confirmation_items "
            "WHERE item_key=?",
            (blank_item["item_key"],),
        ).fetchone()
        self.assertEqual(tuple(blank_row), ("pending", None, "", ""))

    def test_second_clarify_persists_latest_cleaned_note_without_answer_side_effects(self) -> None:
        item = self._new_item(
            "今天完成浩方面试",
            at=self.today.replace(hour=9),
            app="ChatGPT-Note",
        )
        group = self._materialize()["question_groups"][0]
        calibration_before = self.conn.execute(
            "SELECT COUNT(*) FROM activity_confirmation_calibrations"
        ).fetchone()[0]
        confirmed_before = confirmed_activity_view(
            self.conn,
            self.today.isoformat(timespec="seconds"),
            (self.today + timedelta(days=1)).isoformat(timespec="seconds"),
        )

        first = clarify_activity_confirmation_group(
            self.conn,
            group_key=group["group_key"],
            member_revision=group["member_revision"],
        )
        self.assertEqual(
            (first["status"], first["result"]["outcome"], first["result"]["severity"]),
            ("clarified", "evidence_unavailable", "warning"),
        )
        self.assertEqual(first["result"]["evidence_status"], "unavailable")
        self.assertIn("原截图不可用", first["result"]["message"])
        self.assertTrue(first["result"]["generated_at"])

        refreshed = first["result"]["question_groups"][0]
        temporary_note = "  补充：实际是浩方第二轮面试，只用于这次梳理  \n"
        second = clarify_activity_confirmation_group(
            self.conn,
            group_key=refreshed["group_key"],
            member_revision=refreshed["member_revision"],
            note=temporary_note,
        )
        self.assertEqual((second["status"], second["result"]["outcome"]), ("clarified", "rewritten"))
        self.assertEqual(
            second["result"]["message"],
            "原截图不可用；已根据仍可用的已清洗信息和你的补充说明补全主体并重写问题。",
        )
        self.assertEqual(second["result"]["question_groups"][0]["subject_label"], "浩方第二轮面试")
        self.assertTrue(second["member_revision"].startswith("rev_"))
        self.assertTrue(second["result"]["generated_at"])

        row = self.conn.execute(
            "SELECT state,answer,user_note,clarification_note,safe_observation,evidence,"
            "cleaned_content,clarification "
            "FROM activity_confirmation_items WHERE item_key=?",
            (item["item_key"],),
        ).fetchone()
        self.assertEqual((row["state"], row["answer"], row["user_note"]), ("pending", None, ""))
        self.assertEqual(
            row["clarification_note"],
            "补充：实际是浩方第二轮面试，只用于这次梳理",
        )
        serialized_row = json.dumps(dict(row), ensure_ascii=False)
        serialized_response = json.dumps(second, ensure_ascii=False)
        self.assertIn("补充：实际是浩方第二轮面试，只用于这次梳理", serialized_row)
        self.assertNotIn("补充：实际是浩方第二轮面试，只用于这次梳理", serialized_response)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM activity_confirmation_calibrations").fetchone()[0],
            calibration_before,
        )
        confirmed_after = confirmed_activity_view(
            self.conn,
            self.today.isoformat(timespec="seconds"),
            (self.today + timedelta(days=1)).isoformat(timespec="seconds"),
        )
        self.assertEqual(
            (
                confirmed_after["reportable_activity_count"],
                confirmed_after["pending_confirmation_count"],
                confirmed_after["user_confirmed_count"],
            ),
            (
                confirmed_before["reportable_activity_count"],
                confirmed_before["pending_confirmation_count"],
                confirmed_before["user_confirmed_count"],
            ),
        )
        self.assertFalse(second["raw_access"])
        self.assertFalse(second["memory_write_actions"])

        repeated_group = second["result"]["question_groups"][0]
        repeated = clarify_activity_confirmation_group(
            self.conn,
            group_key=repeated_group["group_key"],
            member_revision=repeated_group["member_revision"],
            note="补充：实际是浩方第二轮面试",
        )
        self.assertEqual(
            (
                repeated["status"],
                repeated["result"]["outcome"],
                repeated["result"]["severity"],
            ),
            ("clarified", "evidence_unavailable", "warning"),
        )
        self.assertIn("原截图不可用", repeated["result"]["message"])
        self.assertTrue(repeated["result"]["generated_at"])
        self.assertEqual(
            self.conn.execute(
                "SELECT clarification_note FROM activity_confirmation_items WHERE item_key=?",
                (item["item_key"],),
            ).fetchone()[0],
            "补充：实际是浩方第二轮面试",
        )

    def test_clarify_reuses_the_latest_stored_note_by_updated_at_without_exposing_it(self) -> None:
        for minute in (0, 5):
            self._insert_activity(
                "今天完成浩方面试",
                at=self.today.replace(hour=9, minute=minute),
                app="ChatGPT-Latest-Note",
            )
        group = next(
            candidate
            for candidate in self._materialize()["question_groups"]
            if candidate["app"] == "ChatGPT-Latest-Note"
        )
        members = self.conn.execute(
            "SELECT item_key FROM activity_confirmation_items WHERE group_key=? "
            "ORDER BY datetime(observed_start),item_key",
            (group["group_key"],),
        ).fetchall()
        self.assertEqual(len(members), 2)
        latest_at = self.today.replace(hour=20).isoformat(timespec="seconds")
        older_at = self.today.replace(hour=19).isoformat(timespec="seconds")
        latest_note = "补充：这组共享主体是浩方第二轮面试"
        older_note = "补充：这组共享主体是浩方第三轮面试"
        self.conn.execute(
            "UPDATE activity_confirmation_items SET clarification_note=?,updated_at=? "
            "WHERE item_key=?",
            (latest_note, latest_at, members[0]["item_key"]),
        )
        self.conn.execute(
            "UPDATE activity_confirmation_items SET clarification_note=?,updated_at=? "
            "WHERE item_key=?",
            (older_note, older_at, members[1]["item_key"]),
        )
        self.conn.commit()

        result = clarify_activity_confirmation_group(
            self.conn,
            group_key=group["group_key"],
            member_revision=group["member_revision"],
        )

        self.assertEqual((result["status"], result["result"]["outcome"]), ("clarified", "rewritten"))
        self.assertEqual(
            result["result"]["question_groups"][0]["subject_label"],
            "浩方第二轮面试",
        )
        listed = list_activity_confirmations(self.conn, range_name="today", include_resolved=True)
        public_json = json.dumps(listed, ensure_ascii=False)
        self.assertNotIn(latest_note, public_json)
        self.assertNotIn(older_note, public_json)

    def test_regroup_preserves_the_newest_note_across_previously_separate_prefix_groups(self) -> None:
        short = "今天我已完成战术装备蓝图归档"
        long = "今天我已完成战术装备蓝图归档验收"
        self._insert_activity(
            short,
            at=self.today.replace(hour=10),
            app="CoDBlkOps3_Exe",
        )
        self._insert_activity(
            long,
            at=self.today.replace(hour=10, minute=5),
            app="Obsidian-Separate-Group",
        )
        initial = self._materialize()
        initial_groups = [
            group
            for group in initial["question_groups"]
            if group["subject"]["object"] in {"战术装备蓝图归档", "战术装备蓝图归档验收"}
        ]
        self.assertEqual(len(initial_groups), 2)
        self.assertEqual(len({group["group_key"] for group in initial_groups}), 2)

        rows = {
            row["safe_observation"]: row
            for row in self.conn.execute(
                "SELECT item_key,safe_observation FROM activity_confirmation_items "
                "WHERE safe_observation IN (?,?)",
                (short, long),
            ).fetchall()
        }
        newest_note = "补充：这是游戏画面，不是现实完成事项"
        older_note = "补充：这是现实工作事项"
        self.conn.execute(
            "UPDATE activity_confirmation_items SET clarification_note=?,updated_at=? "
            "WHERE item_key=?",
            (
                newest_note,
                self.today.replace(hour=20).isoformat(timespec="seconds"),
                rows[short]["item_key"],
            ),
        )
        self.conn.execute(
            "UPDATE activity_confirmation_items SET clarification_note=?,updated_at=?,app_family=? "
            "WHERE item_key=?",
            (
                older_note,
                self.today.replace(hour=19).isoformat(timespec="seconds"),
                "CoDBlkOps3_Exe",
                rows[long]["item_key"],
            ),
        )
        _regroup_confirmation_items(self.conn, unresolved_only=True)
        self.conn.commit()

        regrouped_payload = list_activity_confirmations(
            self.conn,
            range_name="today",
            include_resolved=True,
        )
        regrouped = [
            group
            for group in regrouped_payload["question_groups"]
            if group["subject"]["object"] == "战术装备蓝图归档验收"
        ]
        self.assertEqual(len(regrouped), 1)
        self.assertEqual(regrouped[0]["source_count"], 2)
        public_json = json.dumps(regrouped_payload, ensure_ascii=False)
        self.assertNotIn(newest_note, public_json)
        self.assertNotIn(older_note, public_json)
        stored_notes = {
            row["clarification_note"]
            for row in self.conn.execute(
                "SELECT clarification_note FROM activity_confirmation_items "
                "WHERE item_key IN (?,?)",
                (rows[short]["item_key"], rows[long]["item_key"]),
            ).fetchall()
        }
        self.assertEqual(stored_notes, {newest_note})

        clarified = clarify_activity_confirmation_group(
            self.conn,
            group_key=regrouped[0]["group_key"],
            member_revision=regrouped[0]["member_revision"],
        )
        self.assertEqual(
            (clarified["status"], clarified["result"]["outcome"]),
            ("clarified", "system_filtered"),
        )
        filtered = self.conn.execute(
            "SELECT state,answer,reason_codes FROM activity_confirmation_items "
            "WHERE item_key IN (?,?) ORDER BY item_key",
            (rows[short]["item_key"], rows[long]["item_key"]),
        ).fetchall()
        self.assertEqual({(row["state"], row["answer"]) for row in filtered}, {("filtered", None)})
        self.assertTrue(
            all("policy_v7_user_context_only" in json.loads(row["reason_codes"]) for row in filtered)
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM activity_confirmation_calibrations").fetchone()[0],
            0,
        )

    def test_clarify_confusion_and_note_over_limit_remain_fail_closed(self) -> None:
        item = self._new_item(
            "今天完成浩方面试",
            at=self.today.replace(hour=9),
            app="ChatGPT-Note-Limit",
        )
        group = self._materialize()["question_groups"][0]
        confusion = clarify_activity_confirmation_group(
            self.conn,
            group_key=group["group_key"],
            member_revision=group["member_revision"],
            note="我不知道你说的是什么",
        )
        self.assertEqual(
            (confusion["result"]["outcome"], confusion["result"]["severity"]),
            ("evidence_unavailable", "warning"),
        )
        self.assertIn("原截图不可用", confusion["result"]["message"])
        self.assertNotIn("系统理解错了", confusion["result"]["message"])

        refreshed = confusion["result"]["question_groups"][0]
        over_limit = clarify_activity_confirmation_group(
            self.conn,
            group_key=refreshed["group_key"],
            member_revision=refreshed["member_revision"],
            note=("无" * 360) + "第二轮面试",
        )
        self.assertEqual(
            (over_limit["result"]["outcome"], over_limit["result"]["severity"]),
            ("evidence_unavailable", "warning"),
        )
        row = self.conn.execute(
            "SELECT state,answer,user_note,clarification_note,subject,clarification "
            "FROM activity_confirmation_items WHERE item_key=?",
            (item["item_key"],),
        ).fetchone()
        self.assertEqual((row["state"], row["answer"], row["user_note"]), ("pending", None, ""))
        self.assertEqual(row["clarification_note"], "无" * 360)
        self.assertEqual(json.loads(row["subject"])["round"], "")
        self.assertNotIn("第二轮面试", row["clarification"])

    def test_clarify_splits_only_when_each_existing_member_maps_to_one_subject(self) -> None:
        for minute in (0, 5):
            self._insert_activity(
                "今天完成浩方面试",
                at=self.today.replace(hour=9, minute=minute),
                app="ChatGPT",
            )
        group = self._materialize()["question_groups"][0]
        rows = self.conn.execute(
            "SELECT item_key FROM activity_confirmation_items WHERE group_key=? "
            "ORDER BY datetime(observed_start),item_key",
            (group["group_key"],),
        ).fetchall()
        self.conn.execute(
            "UPDATE activity_confirmation_items SET cleaned_content=? WHERE item_key=?",
            ("今天完成浩方二面", rows[0]["item_key"]),
        )
        self.conn.execute(
            "UPDATE activity_confirmation_items SET cleaned_content=? WHERE item_key=?",
            ("今天完成小鹏初面", rows[1]["item_key"]),
        )
        self.conn.commit()
        result = clarify_activity_confirmation_group(
            self.conn,
            group_key=group["group_key"],
            member_revision=group["member_revision"],
        )
        self.assertEqual(result["result"]["outcome"], "split")
        split_groups = result["result"]["question_groups"]
        self.assertEqual(len(split_groups), 2)
        self.assertEqual(
            {item["subject_label"] for item in split_groups},
            {"浩方第二轮面试", "小鹏第一轮面试"},
        )
        self.assertEqual(len({item["group_key"] for item in split_groups}), 2)
        states = self.conn.execute(
            "SELECT state,answer FROM activity_confirmation_items"
        ).fetchall()
        self.assertEqual({(row["state"], row["answer"]) for row in states}, {("pending", None)})
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM activity_confirmation_calibrations").fetchone()[0],
            0,
        )
        reloaded = list_activity_confirmations(self.conn, range_name="today")["question_groups"]
        reloaded_split = [
            item
            for item in reloaded
            if item["subject_label"] in {"浩方第二轮面试", "小鹏第一轮面试"}
        ]
        self.assertEqual(len(reloaded_split), 2)
        self.assertEqual(
            {item["subject_label"] for item in reloaded_split},
            {"浩方第二轮面试", "小鹏第一轮面试"},
        )
        self.assertEqual(len({item["group_key"] for item in reloaded_split}), 2)

    def test_one_note_is_not_copied_across_a_conflicting_multi_source_group(self) -> None:
        first = self._new_item(
            "今天完成浩方面试",
            at=self.today.replace(hour=9),
            app="ChatGPT-Shared-Note",
        )
        second = self._new_item(
            "今天完成小鹏面试",
            at=self.today.replace(hour=9, minute=20),
            app="ChatGPT-Shared-Note",
        )
        first_row = self.conn.execute(
            "SELECT group_key,group_signature FROM activity_confirmation_items WHERE item_key=?",
            (first["item_key"],),
        ).fetchone()
        self.conn.execute(
            "UPDATE activity_confirmation_items SET group_key=?,group_signature=?,subject='{}' "
            "WHERE item_key IN (?,?)",
            (
                first_row["group_key"],
                first_row["group_signature"],
                first["item_key"],
                second["item_key"],
            ),
        )
        self.conn.commit()
        forced_group = next(
            group
            for group in list_activity_confirmations(self.conn, range_name="today")["question_groups"]
            if group["group_key"] == first_row["group_key"]
        )
        result = clarify_activity_confirmation_group(
            self.conn,
            group_key=forced_group["group_key"],
            member_revision=forced_group["member_revision"],
            note="补充：这是第二轮面试",
        )
        self.assertEqual(result["result"]["outcome"], "insufficient_information")
        self.assertEqual(
            result["result"]["message"],
            "已使用你的补充说明重新梳理，但仍没有足够信息；可以继续补充，或忽略该问题。",
        )
        self.assertNotIn("系统理解错了", result["result"]["message"])
        self.assertEqual(len(result["result"]["question_groups"]), 1)
        stored = self.conn.execute(
            "SELECT state,answer,user_note,subject FROM activity_confirmation_items ORDER BY item_key"
        ).fetchall()
        self.assertEqual({(row["state"], row["answer"], row["user_note"]) for row in stored}, {("pending", None, "")})
        self.assertTrue(all(json.loads(row["subject"]) == {} for row in stored))

    def test_conflicting_complete_subjects_cannot_bypass_note_group_ambiguity(self) -> None:
        first = self._new_item(
            "今天完成浩方二面",
            at=self.today.replace(hour=9),
            app="ChatGPT-Conflicting-Complete",
        )
        second = self._new_item(
            "今天完成小鹏初面",
            at=self.today.replace(hour=9, minute=20),
            app="ChatGPT-Conflicting-Complete",
        )
        first_row = self.conn.execute(
            "SELECT group_key,group_signature FROM activity_confirmation_items WHERE item_key=?",
            (first["item_key"],),
        ).fetchone()
        self.conn.execute(
            "UPDATE activity_confirmation_items SET group_key=?,group_signature=? WHERE item_key=?",
            (first_row["group_key"], first_row["group_signature"], second["item_key"]),
        )
        self.conn.commit()
        group = next(
            item
            for item in list_activity_confirmations(self.conn, range_name="today")["question_groups"]
            if item["group_key"] == first_row["group_key"]
        )
        subjects_before = [
            row["subject"]
            for row in self.conn.execute(
                "SELECT subject FROM activity_confirmation_items ORDER BY item_key"
            ).fetchall()
        ]
        result = clarify_activity_confirmation_group(
            self.conn,
            group_key=group["group_key"],
            member_revision=group["member_revision"],
            note="补充：这是第二轮面试",
        )
        self.assertEqual(
            (result["result"]["outcome"], result["result"]["severity"]),
            ("insufficient_information", "warning"),
        )
        self.assertEqual(result["result"]["evidence_status"], "unavailable")
        self.assertEqual(len(result["result"]["question_groups"]), 1)
        self.assertEqual(
            [
                row["subject"]
                for row in self.conn.execute(
                    "SELECT subject FROM activity_confirmation_items ORDER BY item_key"
                ).fetchall()
            ],
            subjects_before,
        )

    def test_clarify_never_rewrites_an_already_answered_member_in_a_mixed_group(self) -> None:
        for minute in (0, 5):
            self._insert_activity(
                "今天完成浩方面试",
                at=self.today.replace(hour=11, minute=minute),
                app="Legacy-Immutable-Clarify",
            )
        group = self._materialize()["question_groups"][0]
        first = self.conn.execute(
            "SELECT item_key FROM activity_confirmation_items WHERE group_key=? "
            "ORDER BY datetime(observed_start),item_key LIMIT 1",
            (group["group_key"],),
        ).fetchone()
        answer_activity_confirmation(
            self.conn,
            item_key=first["item_key"],
            answer="occurred",
            reason="correct",
        )
        partial_group = next(
            item
            for item in list_activity_confirmations(self.conn, range_name="today")["question_groups"]
            if item["group_key"] == group["group_key"]
        )
        answered_before = tuple(
            self.conn.execute(
                "SELECT state,answer,user_note,subject,evidence,decision_question,clarification "
                "FROM activity_confirmation_items WHERE item_key=?",
                (first["item_key"],),
            ).fetchone()
        )
        calibration_before = tuple(
            self.conn.execute(
                "SELECT positive_count,negative_count,misrecognized_count,reason_counts "
                "FROM activity_confirmation_calibrations"
            ).fetchone()
        )
        result = clarify_activity_confirmation_group(
            self.conn,
            group_key=partial_group["group_key"],
            member_revision=partial_group["member_revision"],
            note="补充：这是浩方第二轮面试",
        )
        self.assertEqual((result["status"], result["result"]["outcome"]), ("clarified", "insufficient_information"))
        self.assertIn("未修改原回答", result["result"]["message"])
        answered_after = tuple(
            self.conn.execute(
                "SELECT state,answer,user_note,subject,evidence,decision_question,clarification "
                "FROM activity_confirmation_items WHERE item_key=?",
                (first["item_key"],),
            ).fetchone()
        )
        self.assertEqual(answered_after, answered_before)
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT positive_count,negative_count,misrecognized_count,reason_counts "
                    "FROM activity_confirmation_calibrations"
                ).fetchone()
            ),
            calibration_before,
        )
        pending = self.conn.execute(
            "SELECT state,answer,user_note,subject FROM activity_confirmation_items "
            "WHERE item_key<>?",
            (first["item_key"],),
        ).fetchone()
        self.assertEqual((pending["state"], pending["answer"], pending["user_note"]), ("pending", None, ""))
        self.assertEqual(json.loads(pending["subject"])["round"], "")

    def test_one_note_can_refine_the_verified_shared_subject_for_the_whole_group(self) -> None:
        for minute in (0, 5):
            self._insert_activity(
                "今天完成浩方面试",
                at=self.today.replace(hour=10, minute=minute),
                app="ChatGPT-Shared-Subject",
            )
        group = self._materialize()["question_groups"][0]
        self.assertEqual((group["source_count"], group["subject"]["company"], group["subject"]["round"]), (2, "浩方", ""))
        result = clarify_activity_confirmation_group(
            self.conn,
            group_key=group["group_key"],
            member_revision=group["member_revision"],
            note="补充：这组共享主体是浩方第二轮面试",
        )
        self.assertEqual((result["result"]["outcome"], len(result["result"]["question_groups"])), ("rewritten", 1))
        self.assertEqual(result["result"]["question_groups"][0]["subject_label"], "浩方第二轮面试")
        rows = self.conn.execute(
            "SELECT state,answer,user_note,subject FROM activity_confirmation_items ORDER BY item_key"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual({(row["state"], row["answer"], row["user_note"]) for row in rows}, {("pending", None, "")})
        self.assertTrue(all(json.loads(row["subject"])["round"] == "第二轮" for row in rows))

    def test_clarify_reports_conflicting_context_without_cloning_or_guessing(self) -> None:
        self._insert_activity(
            "今天完成浩方面试",
            at=self.today.replace(hour=9),
            app="ChatGPT",
        )
        group = self._materialize()["question_groups"][0]
        for minute, summary in ((11, "今天完成浩方二面"), (13, "今天完成小鹏初面")):
            self._insert_activity(
                summary,
                at=self.today.replace(hour=9, minute=minute),
                app="ChatGPT",
            )
        before_count = self.conn.execute("SELECT COUNT(*) FROM activity_confirmation_items").fetchone()[0]
        result = clarify_activity_confirmation_group(
            self.conn,
            group_key=group["group_key"],
            member_revision=group["member_revision"],
        )
        self.assertEqual(
            (result["result"]["outcome"], result["result"]["severity"]),
            ("evidence_unavailable", "warning"),
        )
        self.assertEqual(result["result"]["evidence_status"], "unavailable")
        self.assertEqual(result["result"]["subject_conflicts"], [])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM activity_confirmation_items").fetchone()[0],
            before_count,
        )
        row = self.conn.execute("SELECT state,answer,subject FROM activity_confirmation_items").fetchone()
        self.assertEqual((row["state"], row["answer"], json.loads(row["subject"])["company"]), ("pending", None, "浩方"))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM activity_confirmation_calibrations").fetchone()[0],
            0,
        )

    def test_partial_legacy_group_can_be_atomically_reconciled_once_per_calibration_key(self) -> None:
        for minute in (0, 5):
            self._insert_activity(
                "今天完成浩方二面",
                at=self.today.replace(hour=9, minute=minute),
                app="Legacy-Negative",
            )
        group = self._materialize()["question_groups"][0]
        first = self.conn.execute(
            "SELECT item_key,calibration_key FROM activity_confirmation_items WHERE group_key=? "
            "ORDER BY datetime(observed_start) LIMIT 1",
            (group["group_key"],),
        ).fetchone()
        answer_activity_confirmation(
            self.conn,
            item_key=first["item_key"],
            answer="occurred",
            reason="correct",
        )
        partial = list_activity_confirmations(self.conn, range_name="today")
        partial_group = next(item for item in partial["question_groups"] if item["group_key"] == group["group_key"])
        self.assertEqual((partial_group["state"], partial_group["user_answer"]), ("pending", ""))
        before_clarify_states = [
            tuple(row)
            for row in self.conn.execute(
                "SELECT item_key,state,answer FROM activity_confirmation_items "
                "WHERE group_key=? ORDER BY item_key",
                (group["group_key"],),
            ).fetchall()
        ]
        before_clarify_calibration = tuple(
            self.conn.execute(
                "SELECT positive_count,negative_count,misrecognized_count,reason_counts "
                "FROM activity_confirmation_calibrations WHERE calibration_key=?",
                (first["calibration_key"],),
            ).fetchone()
        )
        clarified_partial = clarify_activity_confirmation_group(
            self.conn,
            group_key=partial_group["group_key"],
            member_revision=partial_group["member_revision"],
        )
        self.assertEqual(clarified_partial["status"], "clarified")
        self.assertEqual(
            [
                tuple(row)
                for row in self.conn.execute(
                    "SELECT item_key,state,answer FROM activity_confirmation_items "
                    "WHERE group_key=? ORDER BY item_key",
                    (group["group_key"],),
                ).fetchall()
            ],
            before_clarify_states,
        )
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT positive_count,negative_count,misrecognized_count,reason_counts "
                    "FROM activity_confirmation_calibrations WHERE calibration_key=?",
                    (first["calibration_key"],),
                ).fetchone()
            ),
            before_clarify_calibration,
        )
        reconciled_negative = answer_activity_confirmation_group(
            self.conn,
            group_key=partial_group["group_key"],
            member_revision=clarified_partial["member_revision"],
            answer="not_occurred",
            reason="reference_only",
            workstream="面试求职",
            generalize_scope="similar",
        )
        self.assertEqual(reconciled_negative["status"], "recorded")
        negative_rows = self.conn.execute(
            "SELECT state,answer FROM activity_confirmation_items WHERE group_key=?",
            (group["group_key"],),
        ).fetchall()
        self.assertEqual(
            {(row["state"], row["answer"]) for row in negative_rows},
            {("rejected", "not_occurred")},
        )
        negative_calibration = self.conn.execute(
            "SELECT positive_count,negative_count,misrecognized_count,reason_counts "
            "FROM activity_confirmation_calibrations WHERE calibration_key=?",
            (first["calibration_key"],),
        ).fetchone()
        self.assertEqual(
            (
                negative_calibration["positive_count"],
                negative_calibration["negative_count"],
                negative_calibration["misrecognized_count"],
            ),
            (0, 1, 0),
        )
        self.assertEqual(json.loads(negative_calibration["reason_counts"]), {"reference_only": 1})

        for minute in (0, 5):
            self._insert_activity(
                "今天完成小鹏初面",
                at=self.today.replace(hour=13, minute=minute),
                app="Legacy-Positive",
            )
        positive_group = next(
            item
            for item in self._materialize()["question_groups"]
            if item["app"] == "Legacy-Positive"
        )
        positive_first = self.conn.execute(
            "SELECT item_key,calibration_key FROM activity_confirmation_items WHERE group_key=? "
            "ORDER BY datetime(observed_start) LIMIT 1",
            (positive_group["group_key"],),
        ).fetchone()
        answer_activity_confirmation(
            self.conn,
            item_key=positive_first["item_key"],
            answer="misrecognized",
            reason="vlm_or_ocr",
            generalize_scope="similar",
        )
        positive_partial = next(
            item
            for item in list_activity_confirmations(self.conn, range_name="today")["question_groups"]
            if item["group_key"] == positive_group["group_key"]
        )
        reconciled_positive = answer_activity_confirmation_group(
            self.conn,
            group_key=positive_partial["group_key"],
            member_revision=positive_partial["member_revision"],
            answer="occurred",
            reason="correct",
            workstream="面试求职",
        )
        self.assertEqual(reconciled_positive["status"], "recorded")
        positive_calibration = self.conn.execute(
            "SELECT positive_count,negative_count,misrecognized_count,reason_counts "
            "FROM activity_confirmation_calibrations WHERE calibration_key=?",
            (positive_first["calibration_key"],),
        ).fetchone()
        self.assertEqual(
            (
                positive_calibration["positive_count"],
                positive_calibration["negative_count"],
                positive_calibration["misrecognized_count"],
            ),
            (1, 0, 0),
        )
        self.assertEqual(json.loads(positive_calibration["reason_counts"]), {})

        for minute in (0, 5):
            self._insert_activity(
                "今天完成理想三面",
                at=self.today.replace(hour=15, minute=minute),
                app="Legacy-Defer",
            )
        deferred_group = next(
            item
            for item in self._materialize()["question_groups"]
            if item["app"] == "Legacy-Defer"
        )
        defer_first = self.conn.execute(
            "SELECT item_key,calibration_key FROM activity_confirmation_items WHERE group_key=? "
            "ORDER BY datetime(observed_start) LIMIT 1",
            (deferred_group["group_key"],),
        ).fetchone()
        answer_activity_confirmation(
            self.conn,
            item_key=defer_first["item_key"],
            answer="defer",
            reason="other",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM activity_confirmation_calibrations WHERE calibration_key=?",
                (defer_first["calibration_key"],),
            ).fetchone()[0],
            0,
        )
        deferred_partial = next(
            item
            for item in list_activity_confirmations(self.conn, range_name="today")["question_groups"]
            if item["group_key"] == deferred_group["group_key"]
        )
        reconciled_defer = answer_activity_confirmation_group(
            self.conn,
            group_key=deferred_partial["group_key"],
            member_revision=deferred_partial["member_revision"],
            answer="defer",
            reason="other",
        )
        self.assertEqual(reconciled_defer["status"], "recorded")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM activity_confirmation_calibrations WHERE calibration_key=?",
                (defer_first["calibration_key"],),
            ).fetchone()[0],
            0,
        )

        view = confirmed_activity_view(
            self.conn,
            self.today.isoformat(timespec="seconds"),
            (self.today + timedelta(days=1)).isoformat(timespec="seconds"),
        )
        self.assertEqual(len(view["trusted_activities"]), 1)
        self.assertEqual(view["trusted_activities"][0]["confirmation_source_count"], 2)
        self.assertEqual(
            (
                view["user_confirmed_count"],
                view["user_rejected_count"],
                view["pending_confirmation_count"],
            ),
            (1, 1, 1),
        )
        listed = list_activity_confirmations(self.conn, range_name="today")
        self.assertEqual([item["state"] for item in listed["question_groups"]], ["deferred"])
        deferred = next(item for item in listed["question_groups"] if item["state"] == "deferred")
        self.assertEqual(deferred["user_answer"], "defer")
        self.assertEqual(listed["pending_question_count"], 1)
        self.assertEqual(listed["pending_source_count"], 2)

    def test_two_matching_negative_reasons_generalize_but_mixed_or_positive_do_not_interfere(self) -> None:
        first = self._new_item("今天完成浩方面试 证据 A", app="ChatGPT-A")
        answer_activity_confirmation(
            self.conn, item_key=first["item_key"], answer="not_occurred",
            reason="old_content", generalize_scope="similar",
        )
        second = self._new_item("今天完成浩方面试 证据 B", app="ChatGPT-A")
        self.assertEqual(second["state"], "pending")
        self.assertLess(second["confidence"], first["confidence"])
        self.assertIn("calibration_negative_feedback_caution", second["reason_codes"])
        answer_activity_confirmation(
            self.conn, item_key=second["item_key"], answer="not_occurred",
            reason="old_content", generalize_scope="similar",
        )
        suppressed = self._new_item("今天完成浩方面试 证据 C", app="ChatGPT-A")
        self.assertEqual(suppressed["state"], "pending")
        self.assertIn("calibration_high_impact_review_required", suppressed["reason_codes"])
        calibration = next(
            item
            for item in self._materialize()["calibrations"]
            if item["negative_count"] >= 2 and item["reason"] == "old_content"
        )
        self.assertGreaterEqual(calibration["negative_count"], 2)
        self.assertEqual(calibration["answer"], "not_occurred")
        self.assertEqual(calibration["reason"], "old_content")
        self.assertEqual(calibration["generalize"], "similar")
        self.assertTrue(calibration["created_at"])
        self.assertTrue(calibration["can_revoke"])
        view = confirmed_activity_view(
            self.conn,
            self.today.isoformat(timespec="seconds"),
            (self.today + timedelta(days=1)).isoformat(timespec="seconds"),
        )
        self.assertEqual(view["user_rejected_count"], 2)
        self.assertEqual(view["auto_suppressed_count"], 0)
        self.assertEqual(view["pending_confirmation_count"], 1)

        mixed_a = self._new_item("今天完成浩方面试 混合 A", app="ChatGPT-B")
        answer_activity_confirmation(
            self.conn, item_key=mixed_a["item_key"], answer="not_occurred",
            reason="old_content", generalize_scope="similar",
        )
        mixed_b = self._new_item("今天完成浩方面试 混合 B", app="ChatGPT-B")
        answer_activity_confirmation(
            self.conn, item_key=mixed_b["item_key"], answer="misrecognized",
            reason="vlm_or_ocr", generalize_scope="similar",
        )
        self.assertEqual(self._new_item("今天完成浩方面试 混合 C", app="ChatGPT-B")["state"], "pending")

        positive = self._new_item("今天完成浩方面试 正例", app="ChatGPT-C")
        answer_activity_confirmation(self.conn, item_key=positive["item_key"], answer="occurred", reason="correct")
        for suffix in ("负例 A", "负例 B"):
            negative = self._new_item(f"今天完成浩方面试 {suffix}", app="ChatGPT-C")
            answer_activity_confirmation(
                self.conn, item_key=negative["item_key"], answer="not_occurred",
                reason="reference_only", generalize_scope="similar",
            )
        protected = self._new_item("今天完成浩方面试 负例 C", app="ChatGPT-C")
        self.assertEqual(protected["state"], "pending")
        self.assertIn("calibration_high_impact_review_required", protected["reason_codes"])

        revoke_confirmation_calibration(self.conn, calibration_key=calibration["calibration_key"])
        self.assertEqual(self._new_item("今天完成浩方面试 撤销后", app="ChatGPT-A")["state"], "pending")
        listed = self._materialize()
        revoked = next(item for item in listed["calibrations"] if item["calibration_key"] == calibration["calibration_key"])
        self.assertTrue(revoked["revoked"])

    def test_direct_submit_and_realtime_evidence_override_calibration(self) -> None:
        for suffix in ("A", "B"):
            item = self._new_item(
                f"购买成功 {suffix}",
                app="Store",
                region_kind="submit_area",
                reason_codes=["submit_interaction_evidence"],
            )
            self.assertEqual(item["state"], "pending")
            self.assertIn("direct_submit_screen_signal_requires_confirmation", item["reason_codes"])
            answer_activity_confirmation(
                self.conn, item_key=item["item_key"], answer="not_occurred",
                reason="vlm_or_ocr", generalize_scope="similar",
            )
        direct = self._new_item(
            "购买成功 C",
            app="Store",
            region_kind="submit_area",
            reason_codes=["submit_interaction_evidence"],
        )
        self.assertEqual(direct["state"], "pending")
        self.assertIn("direct_submit_screen_signal_requires_confirmation", direct["reason_codes"])
        sidebar = self._new_item(
            "购买成功 相同文案",
            at=self.today + timedelta(hours=14),
            app="Store-Same",
            region_kind="sidebar",
            actionability="context_only",
        )
        answer_activity_confirmation(
            self.conn, item_key=sidebar["item_key"], answer="not_occurred",
            reason="old_content", generalize_scope="similar",
        )
        later_direct = self._new_item(
            "购买成功 相同文案",
            at=self.today + timedelta(hours=16),
            app="Store-Same",
            region_kind="submit_area",
            reason_codes=["submit_interaction_evidence"],
        )
        self.assertNotEqual(sidebar["item_key"], later_direct["item_key"])
        self.assertEqual(later_direct["state"], "pending")
        realtime = self._new_item("Zoom meeting ended duration 30m", app="Zoom")
        self.assertEqual(realtime["state"], "pending")
        self.assertIn("realtime_screen_signal_requires_confirmation", realtime["reason_codes"])
        self.assertEqual(self._new_item("已完成报告", app="Obsidian")["event_type"], "completion")
        self.assertEqual(self._new_item("发布成功", app="CMS", region_kind="submit_area")["event_type"], "publish")

    def test_delete_range_cascades_source_items_but_preserves_revocable_calibration(self) -> None:
        items = []
        for suffix in ("A", "B"):
            item = self._new_item(f"今天完成浩方面试 删除测试 {suffix}", app="ChatGPT-Durable")
            items.append(item)
            answer_activity_confirmation(
                self.conn, item_key=item["item_key"], answer="not_occurred",
                reason="old_content", generalize_scope="similar",
            )
        item = items[0]
        calibration_key = self.conn.execute(
            "SELECT calibration_key FROM activity_confirmation_items WHERE item_key=?", (item["item_key"],)
        ).fetchone()[0]
        start = self.today.replace(hour=0, minute=0).isoformat(timespec="seconds")
        end = (self.today.replace(hour=0, minute=0) + timedelta(days=1)).isoformat(timespec="seconds")
        preview = preview_delete_range(self.conn, start, end)
        self.assertEqual(preview["cascade"]["confirmation_items_deleted"], 2)
        delete_range(self.conn, start, end)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM activity_confirmation_items WHERE item_key=?", (item["item_key"],)).fetchone())
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM activity_confirmation_calibrations WHERE calibration_key=?", (calibration_key,)).fetchone())
        suppressed = self._new_item(
            "今天完成浩方面试 删除后仍校准",
            at=self.today + timedelta(hours=12),
            app="ChatGPT-Durable",
        )
        self.assertEqual(suppressed["state"], "pending")
        self.assertIn("calibration_high_impact_review_required", suppressed["reason_codes"])
        self.assertEqual(
            revoke_confirmation_calibration(self.conn, calibration_key=calibration_key)["status"],
            "revoked",
        )
        restored = self._new_item(
            "今天完成浩方面试 撤销后恢复",
            at=self.today + timedelta(hours=13),
            app="ChatGPT-Durable",
        )
        self.assertEqual(restored["state"], "pending")
        answer_activity_confirmation(
            self.conn,
            item_key=restored["item_key"],
            answer="occurred",
            reason="correct",
        )
        still_revoked = self._new_item(
            "今天完成浩方面试 正反馈不重启规则",
            at=self.today + timedelta(hours=14),
            app="ChatGPT-Durable",
        )
        self.assertEqual(still_revoked["state"], "pending")

    def test_cli_defer_defaults_reason_and_yesterday_question_has_absolute_date(self) -> None:
        yesterday = self.today - timedelta(days=1)
        self._insert_activity("今天完成浩方面试 昨日线索", at=yesterday)
        start = yesterday.replace(hour=0, minute=0)
        item = self._materialize(start, start + timedelta(days=1))["items"][0]
        env = os.environ.copy()
        core_root = Path(__file__).resolve().parents[1]
        env["PYTHONPATH"] = str(core_root)
        command = [
            sys.executable, "-m", "activity_mvp", "activity-confirmation", "answer",
            "--db", str(self.db), "--item-key", item["item_key"], "--answer", "defer",
        ]
        completed = subprocess.run(command, cwd=core_root, env=env, text=True, capture_output=True, check=True)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["status"], "recorded")
        self.assertEqual(payload["item"]["answer_reason"], "other")
        self.assertNotIn("今天", item["decision_question"])
        self.assertIn(f"{yesterday.month}月{yesterday.day}日", item["time_label"])

    def test_cli_answer_group_and_clarify_use_v4_group_contract(self) -> None:
        self._insert_activity(
            "今天完成浩方二面",
            at=self.today.replace(hour=10),
            app="CLI-Answer",
        )
        self._insert_activity(
            "今天完成小鹏面试",
            at=self.today.replace(hour=12),
            app="CLI-Clarify",
        )
        groups = self._materialize()["question_groups"]
        answer_group = next(item for item in groups if item["app"] == "CLI-Answer")
        clarify_group = next(item for item in groups if item["app"] == "CLI-Clarify")
        env = os.environ.copy()
        core_root = Path(__file__).resolve().parents[1]
        env["PYTHONPATH"] = str(core_root)
        env["PYTHONDONTWRITEBYTECODE"] = "1"

        clarified = subprocess.run(
            [
                sys.executable,
                "-m",
                "activity_mvp",
                "activity-confirmation",
                "clarify",
                "--db",
                str(self.db),
                "--group-key",
                clarify_group["group_key"],
                "--member-revision",
                clarify_group["member_revision"],
                "--note-stdin",
            ],
            cwd=core_root,
            env=env,
            input="补充：我仍不知道这是哪一轮",
            text=True,
            capture_output=True,
            check=True,
        )
        clarified_payload = json.loads(clarified.stdout)
        self.assertEqual(clarified_payload["status"], "clarified")
        self.assertEqual(
            (
                clarified_payload["result"]["outcome"],
                clarified_payload["result"]["severity"],
            ),
            ("evidence_unavailable", "warning"),
        )
        self.assertIn("原截图不可用", clarified_payload["result"]["message"])
        self.assertNotIn("系统理解错了", clarified_payload["result"]["message"])
        self.assertFalse(clarified_payload["raw_access"])
        self.assertFalse(clarified_payload["memory_write_actions"])

        answered = subprocess.run(
            [
                sys.executable,
                "-m",
                "activity_mvp",
                "activity-confirmation",
                "answer-group",
                "--db",
                str(self.db),
                "--group-key",
                answer_group["group_key"],
                "--member-revision",
                answer_group["member_revision"],
                "--answer",
                "occurred",
                "--reason",
                "correct",
                "--details-stdin",
            ],
            cwd=core_root,
            env=env,
            input=json.dumps(
                {"workstream": "面试求职", "outcome": "unknown"},
                ensure_ascii=False,
            ),
            text=True,
            capture_output=True,
            check=True,
        )
        answered_payload = json.loads(answered.stdout)
        self.assertEqual(answered_payload["status"], "recorded")
        self.assertEqual(answered_payload["question_group"]["user_answer"], "occurred")
        self.assertFalse(answered_payload["destructive_actions"])
        stored = self.conn.execute(
            "SELECT state,answer FROM activity_confirmation_items WHERE group_key=?",
            (answer_group["group_key"],),
        ).fetchall()
        self.assertEqual({(row["state"], row["answer"]) for row in stored}, {("confirmed", "occurred")})

    def test_v4_fuyou_active_episode_is_one_pending_card_then_one_safe_fact(self) -> None:
        at = self.today.replace(hour=17, minute=50)
        self._insert_activity(
            "Meeting Assistant 会话中 正在转写",
            at=at,
            app="Meeting Assistant",
            reason_codes=["session_active"],
        )
        self._insert_activity(
            "富友 海外销售 面试 问：请介绍渠道经验",
            at=at + timedelta(minutes=1),
            app="Meeting Assistant",
        )
        self._insert_activity(
            "答：我负责海外渠道；问题2：如何推进GTM",
            at=at + timedelta(minutes=2),
            app="Meeting Assistant",
        )
        payload = self._materialize()
        occurrence = [
            group
            for group in payload["question_groups"]
            if group["question_kind"] == "occurrence"
        ]
        self.assertEqual(len(occurrence), 1)
        group = occurrence[0]
        self.assertEqual((group["event_type"], group["source_count"]), ("interview", 3))
        self.assertEqual(group["subject"]["company"], "富友")
        self.assertEqual(group["state"], "pending")
        answered = answer_activity_confirmation_group(
            self.conn,
            group_key=group["group_key"],
            member_revision=group["member_revision"],
            answer="occurred",
            reason="correct",
            workstream="富友海外销售面试",
            outcome="progressing",
        )
        self.assertEqual(answered["status"], "recorded")
        start = self.today.isoformat(timespec="seconds")
        end = (self.today + timedelta(days=1)).isoformat(timespec="seconds")
        view = confirmed_activity_view(self.conn, start, end)
        facts = [item for item in view["activities"] if item.get("candidate_origin") == "business_episode"]
        self.assertEqual(len(facts), 1)
        self.assertIn("已确认：", facts[0]["activity_summary"])
        self.assertNotIn("可能存在", facts[0]["activity_summary"])
        self.assertEqual((facts[0]["workstream"], facts[0]["outcome"]), ("富友海外销售面试", "progressing"))
        report = report_context(self.conn, self.today.date().isoformat())
        self.assertTrue(any("富友" in item["summary"] for item in report["activity_highlights"]))
        self.assertIn("富友", json.dumps(build_ai_handoff_package(self.conn, start=start, end=end), ensure_ascii=False))

    def test_information_insufficient_is_blocked_by_group_single_and_cli_writers(self) -> None:
        self._insert_activity(
            "国家市场已发送",
            at=self.today.replace(hour=9),
            app="ChatGPT-Insufficient",
        )
        legacy_payload = self._materialize()
        group = next(
            candidate
            for candidate in legacy_payload["question_groups"]
            if candidate["app"] == "ChatGPT-Insufficient"
        )
        card = next(
            candidate
            for candidate in list_activity_confirmation_cards(
                self.conn,
                range_name="today",
            )["cards"]
            if candidate["group_key"] == group["group_key"]
        )
        self.assertEqual(card["section"], "information_insufficient")
        self.assertEqual(card["answer_options"], [])
        item_key = self.conn.execute(
            "SELECT item_key FROM activity_confirmation_items WHERE group_key=?",
            (group["group_key"],),
        ).fetchone()[0]
        evidence = self._attach_confirmation_evidence(item_key)

        def protected_state() -> tuple[list[tuple], list[tuple], list[tuple], list[tuple]]:
            return (
                [
                    tuple(row)
                    for row in self.conn.execute(
                        "SELECT item_key,state,answer,answer_reason,user_workstream,"
                        "user_outcome,generalize_scope,answered_at "
                        "FROM activity_confirmation_items WHERE item_key=?",
                        (item_key,),
                    ).fetchall()
                ],
                [
                    tuple(row)
                    for row in self.conn.execute(
                        "SELECT evidence_id,item_key,source_activity_id,deletion_group_id,"
                        "released_at,release_reason "
                        "FROM activity_confirmation_evidence_refs WHERE item_key=? "
                        "ORDER BY evidence_id",
                        (item_key,),
                    ).fetchall()
                ],
                [
                    tuple(row)
                    for row in self.conn.execute(
                        "SELECT evidence_id,state,deleted_at,delete_reason "
                        "FROM activity_confirmation_evidence_files ORDER BY evidence_id"
                    ).fetchall()
                ],
                [
                    tuple(row)
                    for row in self.conn.execute(
                        "SELECT * FROM activity_confirmation_calibrations "
                        "ORDER BY calibration_key"
                    ).fetchall()
                ],
            )

        before = protected_state()
        group_blocked = answer_activity_confirmation_group(
            self.conn,
            group_key=group["group_key"],
            member_revision=group["member_revision"],
            answer="occurred",
            reason="correct",
            workstream="市场调研",
        )
        self.assertEqual(
            (group_blocked["status"], group_blocked["error"]),
            ("blocked", "information_insufficient"),
        )
        self.assertEqual(protected_state(), before)

        single_blocked = answer_activity_confirmation(
            self.conn,
            item_key=item_key,
            answer="occurred",
            reason="correct",
            workstream="市场调研",
        )
        self.assertEqual(
            (single_blocked["status"], single_blocked["error"]),
            ("blocked", "information_insufficient"),
        )
        self.assertEqual(protected_state(), before)

        env = os.environ.copy()
        core_root = Path(__file__).resolve().parents[1]
        env["PYTHONPATH"] = str(core_root)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        group_cli = subprocess.run(
            [
                sys.executable,
                "-m",
                "activity_mvp",
                "activity-confirmation",
                "answer-group",
                "--db",
                str(self.db),
                "--group-key",
                group["group_key"],
                "--member-revision",
                group["member_revision"],
                "--answer",
                "occurred",
                "--reason",
                "correct",
                "--details-stdin",
            ],
            cwd=core_root,
            env=env,
            input=json.dumps(
                {"workstream": "市场调研", "outcome": "unknown"},
                ensure_ascii=False,
            ),
            text=True,
            capture_output=True,
            check=True,
        )
        group_cli_payload = json.loads(group_cli.stdout)
        self.assertEqual(
            (group_cli_payload["status"], group_cli_payload["error"]),
            ("blocked", "information_insufficient"),
        )
        single_cli = subprocess.run(
            [
                sys.executable,
                "-m",
                "activity_mvp",
                "activity-confirmation",
                "answer",
                "--db",
                str(self.db),
                "--item-key",
                item_key,
                "--answer",
                "occurred",
                "--reason",
                "correct",
            ],
            cwd=core_root,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        single_cli_payload = json.loads(single_cli.stdout)
        self.assertEqual(
            (single_cli_payload["status"], single_cli_payload["error"]),
            ("blocked", "information_insufficient"),
        )
        self.assertEqual(protected_state(), before)
        self.assertTrue(evidence["path"].is_file())
        start = self.today.isoformat(timespec="seconds")
        end = (self.today + timedelta(days=1)).isoformat(timespec="seconds")
        self.assertFalse(
            any(
                activity.get("confirmation_group_key") == group["group_key"]
                for activity in confirmed_activity_view(
                    self.conn,
                    start,
                    end,
                    materialize=False,
                )["activities"]
            )
        )

    def test_terminal_information_insufficient_preserves_answer_idempotency_contract(self) -> None:
        self._insert_activity(
            "国家市场已发送",
            at=self.today.replace(hour=9),
            app="ChatGPT-Terminal-Insufficient",
        )
        group = next(
            candidate
            for candidate in self._materialize()["question_groups"]
            if candidate["app"] == "ChatGPT-Terminal-Insufficient"
        )
        item_key = self.conn.execute(
            "SELECT item_key FROM activity_confirmation_items WHERE group_key=?",
            (group["group_key"],),
        ).fetchone()[0]
        evidence = self._attach_confirmation_evidence(item_key)
        answered_at = self.today.replace(hour=12).isoformat(timespec="seconds")
        self.conn.execute(
            "UPDATE activity_confirmation_items "
            "SET state='confirmed',answer='occurred',answer_reason='correct',"
            "user_workstream='市场调研',user_outcome='unknown',"
            "generalize_scope='exact',answered_at=?,updated_at=? "
            "WHERE item_key=?",
            (answered_at, answered_at, item_key),
        )
        self.conn.commit()
        terminal_group = next(
            candidate
            for candidate in list_activity_confirmations(
                self.conn,
                range_name="today",
                include_resolved=True,
            )["question_groups"]
            if candidate["group_key"] == group["group_key"]
        )

        def terminal_state() -> tuple[list[tuple], list[tuple], list[tuple], list[tuple]]:
            return (
                [
                    tuple(row)
                    for row in self.conn.execute(
                        "SELECT item_key,state,answer,answer_reason,user_workstream,"
                        "user_outcome,generalize_scope,answered_at "
                        "FROM activity_confirmation_items WHERE item_key=?",
                        (item_key,),
                    ).fetchall()
                ],
                [
                    tuple(row)
                    for row in self.conn.execute(
                        "SELECT evidence_id,item_key,released_at,release_reason "
                        "FROM activity_confirmation_evidence_refs WHERE item_key=? "
                        "ORDER BY evidence_id",
                        (item_key,),
                    ).fetchall()
                ],
                [
                    tuple(row)
                    for row in self.conn.execute(
                        "SELECT evidence_id,state,deleted_at,delete_reason "
                        "FROM activity_confirmation_evidence_files ORDER BY evidence_id"
                    ).fetchall()
                ],
                [
                    tuple(row)
                    for row in self.conn.execute(
                        "SELECT * FROM activity_confirmation_calibrations "
                        "ORDER BY calibration_key"
                    ).fetchall()
                ],
            )

        before = terminal_state()
        same_single = answer_activity_confirmation(
            self.conn,
            item_key=item_key,
            answer="occurred",
            reason="correct",
            workstream="市场调研",
            outcome="unknown",
        )
        self.assertEqual(
            (same_single["status"], same_single["unchanged"]),
            ("recorded", True),
        )
        self.assertEqual(terminal_state(), before)
        conflicting_single = answer_activity_confirmation(
            self.conn,
            item_key=item_key,
            answer="not_occurred",
            reason="correct",
            workstream="市场调研",
            outcome="unknown",
        )
        self.assertEqual(
            (conflicting_single["status"], conflicting_single["error"]),
            ("blocked", "item_already_resolved"),
        )
        self.assertEqual(terminal_state(), before)

        same_group = answer_activity_confirmation_group(
            self.conn,
            group_key=terminal_group["group_key"],
            member_revision=terminal_group["member_revision"],
            answer="occurred",
            reason="correct",
            workstream="市场调研",
            outcome="unknown",
        )
        self.assertEqual(
            (same_group["status"], same_group["unchanged"]),
            ("recorded", True),
        )
        self.assertEqual(terminal_state(), before)
        conflicting_group = answer_activity_confirmation_group(
            self.conn,
            group_key=terminal_group["group_key"],
            member_revision=terminal_group["member_revision"],
            answer="not_occurred",
            reason="correct",
            workstream="市场调研",
            outcome="unknown",
        )
        self.assertEqual(
            (conflicting_group["status"], conflicting_group["error"]),
            ("blocked", "group_already_resolved"),
        )
        self.assertEqual(terminal_state(), before)
        self.assertTrue(evidence["path"].is_file())

    def test_mixed_terminal_group_cannot_make_unresolved_information_answerable(self) -> None:
        self._insert_activity(
            "国家市场调研报告已发送",
            at=self.today.replace(hour=9),
            app="ChatGPT-Mixed-Terminal",
        )
        self._insert_activity(
            "国家市场已发送",
            at=self.today.replace(hour=10),
            app="ChatGPT-Mixed-Pending",
        )
        self._materialize()
        rows_by_observation = {
            row["safe_observation"]: dict(row)
            for row in self.conn.execute(
                "SELECT * FROM activity_confirmation_items "
                "WHERE safe_observation IN (?,?)",
                ("国家市场调研报告已发送", "国家市场已发送"),
            ).fetchall()
        }
        terminal = rows_by_observation["国家市场调研报告已发送"]
        pending = rows_by_observation["国家市场已发送"]
        evidence = self._attach_confirmation_evidence(pending["item_key"])
        answered_at = self.today.replace(hour=12).isoformat(timespec="seconds")
        self.conn.execute(
            "UPDATE activity_confirmation_items "
            "SET state='confirmed',answer='occurred',answer_reason='correct',"
            "user_workstream='市场调研',user_outcome='unknown',"
            "generalize_scope='exact',answered_at=?,updated_at=? "
            "WHERE item_key=?",
            (answered_at, answered_at, terminal["item_key"]),
        )
        self.conn.execute(
            "UPDATE activity_confirmation_items SET group_key=?,group_signature=? "
            "WHERE item_key=?",
            (
                terminal["group_key"],
                terminal["group_signature"],
                pending["item_key"],
            ),
        )
        self.conn.commit()
        _regroup_confirmation_items(
            self.conn,
            start=self.today.isoformat(timespec="seconds"),
            end=(self.today + timedelta(days=1)).isoformat(timespec="seconds"),
            unresolved_only=True,
        )
        self.conn.commit()
        mixed_rows = [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM activity_confirmation_items WHERE group_key=? "
                "ORDER BY datetime(observed_start),item_key",
                (terminal["group_key"],),
            ).fetchall()
        ]
        before = [
            tuple(row)
            for row in self.conn.execute(
                "SELECT item_key,state,answer,answer_reason,user_workstream,"
                "user_outcome,generalize_scope,answered_at "
                "FROM activity_confirmation_items WHERE group_key=? ORDER BY item_key",
                (terminal["group_key"],),
            ).fetchall()
        ]
        refs_before = [
            tuple(row)
            for row in self.conn.execute(
                "SELECT evidence_id,item_key,released_at,release_reason "
                "FROM activity_confirmation_evidence_refs ORDER BY evidence_id,item_key"
            ).fetchall()
        ]
        blocked = answer_activity_confirmation_group(
            self.conn,
            group_key=terminal["group_key"],
            member_revision=_member_revision(mixed_rows),
            answer="occurred",
            reason="correct",
            workstream="市场调研",
            outcome="unknown",
        )
        self.assertEqual(
            (blocked["status"], blocked["error"]),
            ("blocked", "information_insufficient"),
        )
        after = [
            tuple(row)
            for row in self.conn.execute(
                "SELECT item_key,state,answer,answer_reason,user_workstream,"
                "user_outcome,generalize_scope,answered_at "
                "FROM activity_confirmation_items WHERE group_key=? ORDER BY item_key",
                (terminal["group_key"],),
            ).fetchall()
        ]
        refs_after = [
            tuple(row)
            for row in self.conn.execute(
                "SELECT evidence_id,item_key,released_at,release_reason "
                "FROM activity_confirmation_evidence_refs ORDER BY evidence_id,item_key"
            ).fetchall()
        ]
        self.assertEqual(after, before)
        self.assertEqual(refs_after, refs_before)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM activity_confirmation_calibrations"
            ).fetchone()[0],
            0,
        )
        self.assertTrue(evidence["path"].is_file())

    def test_v4_static_history_prep_and_weak_nouns_never_become_occurrence(self) -> None:
        base = self.today.replace(hour=8)
        values = (
            ("富友 JD 面试准备题库 问题1 回答1 问题2 回答2", "Meeting Assistant"),
            ("历史记录：去年富友面试 问：旧问题 答：旧回答", "Zoom"),
            ("邮件 表单 申请记录 提交列表", "Unknown App"),
        )
        for index, (summary, app) in enumerate(values):
            self._insert_activity(summary, at=base + timedelta(minutes=index * 40), app=app)
        payload = self._materialize()
        self.assertFalse(
            any(group["question_kind"] == "occurrence" for group in payload["question_groups"]),
            payload["question_groups"],
        )
        self.assertTrue(payload["question_groups"])
        self.assertTrue(all(group["priority"] == "low" for group in payload["question_groups"]))

    def test_v4_ordinary_anchor_keeps_adjacent_context_without_weak_bridging(self) -> None:
        at = self.today.replace(hour=9)
        self._insert_activity(
            "Decision: Local Owner is researching the Israel robot vacuum market",
            at=at,
            app="ChatGPT",
        )
        self._insert_activity(
            "Follow-up: continue channel research and competitor comparison",
            at=at + timedelta(minutes=1),
            app="ChatGPT",
        )
        # A later non-anchor frame cannot walk the episode forward from the
        # attached context; distance remains measured from the true anchor.
        self._insert_activity(
            "Draft the next section of the market report",
            at=at + timedelta(minutes=20),
            app="ChatGPT",
        )
        payload = self._materialize()
        ordinary = [
            group
            for group in payload["question_groups"]
            if group["candidate_origin"] == "business_episode"
            and group["question_kind"] == "business_context"
        ]
        self.assertEqual(len(ordinary), 1)
        self.assertEqual(ordinary[0]["source_count"], 2)

    def test_v5_business_context_writers_fail_closed_without_buttons(self) -> None:
        morning = self.today.replace(hour=9)
        afternoon = self.today.replace(hour=14)
        for at, app, topic in (
            (morning, "ChatGPT-Context-Group", "Israel robot vacuum market"),
            (afternoon, "ChatGPT-Context-Single", "Brazil channel market"),
        ):
            self._insert_activity(
                f"Decision: Local Owner is researching the {topic}",
                at=at,
                app=app,
            )
            self._insert_activity(
                "Follow-up: continue channel research and competitor comparison",
                at=at + timedelta(minutes=1),
                app=app,
            )
        legacy_groups = [
            group
            for group in self._materialize()["question_groups"]
            if group["question_kind"] == "business_context"
        ]
        self.assertEqual(len(legacy_groups), 2)
        cards = list_activity_confirmation_cards(
            self.conn,
            range_name="today",
        )["cards"]
        context_cards = [
            card
            for card in cards
            if card["group_key"] in {
                group["group_key"] for group in legacy_groups
            }
        ]
        self.assertEqual(len(context_cards), 2)
        self.assertTrue(
            all(
                card["section"] == "information_insufficient"
                and card["answer_options"] == []
                and card["display_question"] == ""
                for card in context_cards
            )
        )

        grouped = next(
            group
            for group in legacy_groups
            if group["app"] == "ChatGPT-Context-Group"
        )
        blocked_group = answer_activity_confirmation_group(
            self.conn,
            group_key=grouped["group_key"],
            member_revision=grouped["member_revision"],
            answer="occurred",
            reason="correct",
            workstream="市场调研",
            outcome="progressing",
        )
        self.assertEqual(
            (blocked_group["status"], blocked_group["error"]),
            ("blocked", "information_insufficient"),
        )

        single_group = next(
            group
            for group in legacy_groups
            if group["app"] == "ChatGPT-Context-Single"
        )
        item_key = self.conn.execute(
            "SELECT item_key FROM activity_confirmation_items WHERE group_key=?",
            (single_group["group_key"],),
        ).fetchone()[0]
        blocked_single = answer_activity_confirmation(
            self.conn,
            item_key=item_key,
            answer="occurred",
            reason="correct",
            workstream="市场调研",
            outcome="progressing",
        )
        self.assertEqual(
            (blocked_single["status"], blocked_single["error"]),
            ("blocked", "information_insufficient"),
        )
        self.assertEqual(
            {
                (row["state"], row["answer"])
                for row in self.conn.execute(
                    "SELECT state,answer FROM activity_confirmation_items "
                    "WHERE group_key IN (?,?)",
                    (grouped["group_key"], single_group["group_key"]),
                ).fetchall()
            },
            {("pending", None)},
        )

    def test_v4_active_anchor_wins_over_adjacent_negated_history_without_bridging(self) -> None:
        at = self.today.replace(hour=10)
        self._insert_activity(
            "Meeting Assistant 会话中 正在转写 session id: live001",
            at=at,
            app="Meeting Assistant",
            reason_codes=["session_active"],
        )
        self._insert_activity(
            "旧记录：没有参加过富友面试",
            at=at + timedelta(minutes=1),
            app="Obsidian",
        )
        self._insert_activity(
            "富友面试 问：渠道经验 答：海外销售 问2：GTM",
            at=at + timedelta(minutes=2),
            app="Meeting Assistant",
        )
        first = self._materialize()
        live = [
            group
            for group in first["question_groups"]
            if group["question_kind"] == "occurrence" and group["state"] == "pending"
        ]
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["state"], "pending")

        # A weak context chain between two strong sessions cannot merge them.
        later = at + timedelta(minutes=25)
        self._insert_activity(
            "Meeting Assistant 会话中 正在转写 session id: live002 小鹏正在面试",
            at=later,
            app="Meeting Assistant",
            reason_codes=["session_active"],
        )
        for minute in (11, 16, 21):
            self._insert_activity("海外渠道资料浏览", at=at + timedelta(minutes=minute), app="Obsidian")
        second = self._materialize()
        occurrences = [
            group
            for group in second["question_groups"]
            if group["question_kind"] == "occurrence" and group["state"] == "pending"
        ]
        self.assertEqual(len(occurrences), 2)
        self.assertEqual(len({group["episode_id"] for group in occurrences}), 2)

    def test_v4_same_bucket_subjects_and_realtime_session_ids_do_not_merge(self) -> None:
        at = self.today.replace(hour=12)
        self._insert_activity("富友正在面试", at=at, app="Unknown App")
        self._insert_activity("小鹏正在面试", at=at + timedelta(minutes=2), app="Unknown App")
        later = self.today.replace(hour=14)
        self._insert_activity(
            "Meeting Assistant 会话中 session id: subject01 富友正在面试",
            at=later,
            app="Meeting Assistant",
            reason_codes=["session_active"],
        )
        self._insert_activity(
            "Meeting Assistant 会话中 session id: subject02 富友正在面试",
            at=later + timedelta(minutes=2),
            app="Meeting Assistant",
            reason_codes=["session_active"],
        )
        payload = self._materialize()
        episodes = [
            group
            for group in payload["question_groups"]
            if group["candidate_origin"] == "business_episode" and group["state"] == "pending"
        ]
        self.assertEqual(len(episodes), 4)
        noon = [group for group in episodes if "12:" in group["time_label"]]
        self.assertEqual({group["subject"]["company"] for group in noon}, {"富友", "小鹏"})
        afternoon = [group for group in episodes if "14:" in group["time_label"]]
        self.assertEqual(len({group["episode_id"] for group in afternoon}), 2)

    def test_v4_ingest_materializes_business_episodes_once_per_batch(self) -> None:
        captured = self.today.replace(hour=15)
        events = [
            {
                "captured_at": (captured + timedelta(minutes=index)).isoformat(timespec="seconds"),
                "display_id": "main",
                "app_name": "Meeting Assistant",
                "window_title": "会话中",
                "ocr_text": f"富友面试 问题{index}",
            }
            for index in range(3)
        ]
        with patch(
            "activity_mvp.pipeline.materialize_business_episodes_for_window",
            return_value=0,
        ) as materialize:
            ingest_events(self.conn, events)
        materialize.assert_called_once()

    def test_v4_episode_identity_sampling_and_evidence_representatives_are_stable(self) -> None:
        at = self.today.replace(hour=9)
        source_ids = []
        for index in range(7):
            source_ids.append(
                self._insert_activity(
                    f"Meeting Assistant 会话中 正在转写 session id: stable01 富友面试 问{index}：渠道",
                    at=at + timedelta(minutes=index),
                    app="Meeting Assistant",
                    reason_codes=["session_active"],
                )
            )
        first = self._materialize()
        episode = next(group for group in first["question_groups"] if group["question_kind"] == "occurrence")
        episode_id = episode["episode_id"]
        item_key = self.conn.execute(
            "SELECT item_key FROM activity_confirmation_items WHERE episode_id=?",
            (episode_id,),
        ).fetchone()[0]
        frame_sources = {
            source_id: {
                "source_path": "",
                "privacy_status": "unavailable",
                "source_frame_id": f"frame-{index}",
                "captured_at": (at + timedelta(minutes=index)).isoformat(timespec="seconds"),
                "display_id": "main",
                "deletion_group_id": f"group_{index + 1}",
            }
            for index, source_id in enumerate(source_ids)
        }
        promotions: list[dict] = []
        _promote_pending_episode_evidence(
            self.conn,
            current_activity_ids=set(source_ids),
            frame_sources=frame_sources,
            evidence_promotions=promotions,
        )
        active_refs = self.conn.execute(
            "SELECT source_activity_id FROM activity_confirmation_evidence_refs "
            "WHERE item_key=? AND released_at IS NULL ORDER BY source_activity_id",
            (item_key,),
        ).fetchall()
        self.assertEqual(len(active_refs), 3)

        added = self._insert_activity(
            "Meeting Assistant 会话中 正在转写 session id: stable01 富友面试 问8：GTM",
            at=at + timedelta(minutes=7),
            app="Meeting Assistant",
            reason_codes=["session_active"],
        )
        second = self._materialize()
        refreshed = next(group for group in second["question_groups"] if group["episode_id"] == episode_id)
        self.assertEqual(refreshed["source_count"], 8)
        frame_sources[added] = {
            "source_path": "",
            "privacy_status": "unavailable",
            "source_frame_id": "frame-7",
            "captured_at": (at + timedelta(minutes=7)).isoformat(timespec="seconds"),
            "display_id": "main",
            "deletion_group_id": "group_8",
        }
        _promote_pending_episode_evidence(
            self.conn,
            current_activity_ids={added},
            frame_sources=frame_sources,
            evidence_promotions=promotions,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM activity_confirmation_evidence_refs "
                "WHERE item_key=? AND released_at IS NULL",
                (item_key,),
            ).fetchone()[0],
            3,
        )

        # Standard mode freezes the first 12 sampled ordinary questions.
        ordinary_start = self.today.replace(hour=1)
        for index in range(20):
            self._insert_activity(
                f"项目{index}海外渠道资料浏览",
                at=ordinary_start + timedelta(minutes=index * 35),
                app=f"Docs-{index}",
            )
        sampled = self._materialize()
        ordinary_keys = {
            group["group_key"]
            for group in sampled["question_groups"]
            if group["question_kind"] == "business_context"
        }
        self.assertEqual(len(ordinary_keys), 12)
        self._insert_activity("新的海外渠道资料", at=self.today.replace(hour=0, minute=10), app="Docs-New")
        resampled = self._materialize()
        self.assertTrue(
            ordinary_keys.issubset(
                {
                    group["group_key"]
                    for group in resampled["question_groups"]
                    if group["question_kind"] == "business_context"
                }
            )
        )

    def test_v4_answer_and_manual_idempotency_with_preparation_safety(self) -> None:
        item = self._new_item("今天完成富友面试", app="ChatGPT-Idempotent")
        group = next(
            group
            for group in self._materialize()["question_groups"]
            if group["group_key"] == item["group_key"]
        )
        with self.assertRaisesRegex(ValueError, "requires workstream"):
            answer_activity_confirmation_group(
                self.conn,
                group_key=group["group_key"],
                member_revision=group["member_revision"],
                answer="occurred",
                reason="correct",
                outcome="progressing",
            )
        first = answer_activity_confirmation_group(
            self.conn,
            group_key=group["group_key"],
            member_revision=group["member_revision"],
            answer="occurred",
            reason="correct",
            workstream="富友面试",
            outcome="progressing",
        )
        answered_at = self.conn.execute(
            "SELECT answered_at FROM activity_confirmation_items WHERE item_key=?",
            (item["item_key"],),
        ).fetchone()[0]
        retry = answer_activity_confirmation_group(
            self.conn,
            group_key=group["group_key"],
            member_revision=first["member_revision"],
            answer="occurred",
            reason="correct",
            workstream="富友面试",
            outcome="progressing",
        )
        self.assertEqual((retry["status"], retry["unchanged"]), ("recorded", True))
        self.assertEqual(
            self.conn.execute(
                "SELECT answered_at FROM activity_confirmation_items WHERE item_key=?",
                (item["item_key"],),
            ).fetchone()[0],
            answered_at,
        )
        conflict = answer_activity_confirmation_group(
            self.conn,
            group_key=group["group_key"],
            member_revision=first["member_revision"],
            answer="not_occurred",
            reason="other",
            workstream="富友面试",
            outcome="unknown",
        )
        self.assertEqual(conflict["status"], "blocked")

        with self.assertRaisesRegex(ValueError, "preparation_only"):
            answer_activity_confirmation(
                self.conn,
                item_key=item["item_key"],
                answer="occurred",
                reason="correct",
                outcome="preparation_only",
            )
        prep = self._new_item("今天完成小鹏面试", at=self.today.replace(hour=13), app="ChatGPT-Prep")
        with self.assertRaisesRegex(ValueError, "submitted together"):
            answer_activity_confirmation(
                self.conn,
                item_key=prep["item_key"],
                answer="not_occurred",
                reason="preparation_only",
                outcome="unknown",
            )
        with self.assertRaisesRegex(ValueError, "submitted together"):
            answer_activity_confirmation(
                self.conn,
                item_key=prep["item_key"],
                answer="not_occurred",
                reason="other",
                outcome="preparation_only",
            )
        answer_activity_confirmation(
            self.conn,
            item_key=prep["item_key"],
            answer="not_occurred",
            reason="preparation_only",
            workstream="小鹏面试准备",
            outcome="preparation_only",
        )
        start = self.today.isoformat(timespec="seconds")
        end = (self.today + timedelta(days=1)).isoformat(timespec="seconds")
        prep_facts = [
            activity
            for activity in confirmed_activity_view(self.conn, start, end)["activities"]
            if activity.get("event_type") == "preparation"
        ]
        self.assertEqual(len(prep_facts), 1)
        self.assertIn("原候选事件未作为已发生事实", prep_facts[0]["activity_summary"])
        prep_highlights = [
            highlight
            for highlight in report_context(
                self.conn,
                self.today.date().isoformat(),
            )["activity_highlights"]
            if highlight.get("event_type") == "preparation"
        ]
        self.assertEqual(len(prep_highlights), 1)
        self.assertEqual(
            (
                prep_highlights[0]["occurrence_state"],
                prep_highlights[0]["outcome"],
                prep_highlights[0]["workstream"],
            ),
            ("user_confirmed_preparation", "preparation_only", "小鹏面试准备"),
        )
        self.assertNotIn("面试已发生", prep_highlights[0]["summary"])

        manual_args = {
            "confirmation_action": "MANUAL_CONFIRMED_ACTIVITY",
            "safe_label": "补记富友面试准备",
            "workstream": "富友面试准备",
            "outcome": "preparation_only",
            "started_at": self.today.replace(hour=14).isoformat(timespec="seconds"),
        }
        manual_first = manual_add_confirmed_activity(self.conn, **manual_args)
        manual_answered_at = self.conn.execute(
            "SELECT answered_at FROM activity_confirmation_items WHERE item_key=?",
            (manual_first["item"]["item_key"],),
        ).fetchone()[0]
        manual_retry = manual_add_confirmed_activity(self.conn, **manual_args)
        self.assertTrue(manual_retry["unchanged"])
        self.assertEqual(manual_retry["item"]["item_key"], manual_first["item"]["item_key"])
        self.assertEqual(
            self.conn.execute(
                "SELECT answered_at FROM activity_confirmation_items WHERE item_key=?",
                (manual_first["item"]["item_key"],),
            ).fetchone()[0],
            manual_answered_at,
        )
        manual_highlight = next(
            highlight
            for highlight in report_context(
                self.conn,
                self.today.date().isoformat(),
            )["activity_highlights"]
            if highlight.get("event_type") == "preparation"
            and highlight.get("occurrence_state") == "user_confirmed_preparation"
            and "Manual confirmation" in highlight.get("apps", [])
        )
        self.assertEqual(
            (manual_highlight["outcome"], manual_highlight["workstream"]),
            ("preparation_only", "富友面试准备"),
        )
        self.assertIn("已确认仅进行了准备", manual_highlight["summary"])
        self.assertNotIn("面试已发生", manual_highlight["summary"])
        manual_stored = self.conn.execute(
            "SELECT candidate_origin,event_type,answer,answer_reason,user_outcome "
            "FROM activity_confirmation_items WHERE item_key=?",
            (manual_first["item"]["item_key"],),
        ).fetchone()
        self.assertEqual(
            tuple(manual_stored),
            ("manual", "preparation", "occurred", "correct", "preparation_only"),
        )
        with self.assertRaisesRegex(ValueError, "workstream is required"):
            manual_add_confirmed_activity(
                self.conn,
                confirmation_action="MANUAL_CONFIRMED_ACTIVITY",
                safe_label="补记普通业务活动",
                started_at=self.today.replace(hour=15).isoformat(timespec="seconds"),
            )
        with self.assertRaisesRegex(ValueError, "sensitive"):
            manual_add_confirmed_activity(
                self.conn,
                confirmation_action="MANUAL_CONFIRMED_ACTIVITY",
                safe_label="password=do-not-store",
                started_at=self.today.replace(hour=15).isoformat(timespec="seconds"),
            )

    def test_v4_settlement_revision_is_idempotent_and_stale_snapshots_are_partial(self) -> None:
        item = self._new_item("今天完成富友面试 结算测试", app="Settlement")
        report_day = self.today.date().isoformat()
        pending = settle_activity_confirmations(self.conn, report_date=report_day)
        self.assertEqual(pending["confirmation_completeness"]["status"], "pending")
        repeated_pending = settle_activity_confirmations(self.conn, report_date=report_day)
        self.assertEqual(
            (repeated_pending["revision"], repeated_pending["settled_at"]),
            (pending["revision"], pending["settled_at"]),
        )
        answer_activity_confirmation(
            self.conn,
            item_key=item["item_key"],
            answer="occurred",
            reason="correct",
            workstream="富友面试",
            outcome="completed",
        )
        complete = settle_activity_confirmations(self.conn, report_date=report_day)
        self.assertNotEqual(complete["revision"], pending["revision"])
        self.assertEqual(complete["confirmation_completeness"]["status"], "complete")
        repeated_complete = settle_activity_confirmations(self.conn, report_date=report_day)
        self.assertEqual(
            (repeated_complete["revision"], repeated_complete["settled_at"]),
            (complete["revision"], complete["settled_at"]),
        )
        start = self.today.isoformat(timespec="seconds")
        end = (self.today + timedelta(days=1)).isoformat(timespec="seconds")
        view = confirmed_activity_view(self.conn, start, end)
        context = report_context(self.conn, report_day)
        self.assertEqual(
            context["confirmation_completeness"]["candidate_funnel"]["reportable"],
            view["reportable_activity_count"],
        )
        self._insert_activity(
            "今天完成小鹏面试 结算后新候选",
            at=self.today.replace(hour=20),
            app="Settlement-New",
        )
        self._materialize()
        stale = report_context(self.conn, report_day)["confirmation_completeness"]
        self.assertEqual(stale["status"], "pending")
        self.assertIsNone(stale["settled_at"])
        self.assertNotEqual(stale["revision"], complete["revision"])

    def test_v4_settlement_revision_binds_safe_report_projection(self) -> None:
        manual = manual_add_confirmed_activity(
            self.conn,
            confirmation_action="MANUAL_CONFIRMED_ACTIVITY",
            safe_label="补记富友沟通",
            workstream="富友求职",
            outcome="completed",
            started_at=self.today.replace(hour=18).isoformat(timespec="seconds"),
        )
        report_day = self.today.date().isoformat()
        initial = settle_activity_confirmations(self.conn, report_date=report_day)
        self.assertEqual(initial["confirmation_completeness"]["status"], "complete")
        self.conn.execute(
            "UPDATE activity_confirmation_items SET safe_observation=? WHERE item_key=?",
            ("补记富友沟通（修正版）", manual["item"]["item_key"]),
        )
        self.conn.commit()

        stale_context = report_context(self.conn, report_day)
        stale = stale_context["confirmation_completeness"]
        self.assertEqual(stale["status"], "partial")
        self.assertIsNone(stale["settled_at"])
        self.assertNotEqual(stale["revision"], initial["revision"])
        self.assertTrue(
            any(
                highlight["summary"] == "补记富友沟通（修正版）"
                for highlight in stale_context["activity_highlights"]
            )
        )
        corrected = settle_activity_confirmations(self.conn, report_date=report_day)
        self.assertEqual(corrected["confirmation_completeness"]["status"], "complete")
        self.assertEqual(corrected["revision"], stale["revision"])

    def test_v4_orphan_source_forces_partial_settlement(self) -> None:
        source_id = self._insert_activity(
            "今天完成富友面试 orphan 结算",
            at=self.today.replace(hour=19),
            app="Orphan-Settlement",
        )
        item = self._materialize()["items"][0]
        stored_sources = json.loads(
            self.conn.execute(
                "SELECT source_activity_ids FROM activity_confirmation_items WHERE item_key=?",
                (item["item_key"],),
            ).fetchone()["source_activity_ids"]
        )
        self.assertIn(source_id, stored_sources)
        self.conn.execute("DELETE FROM extracted_activities WHERE id=?", (source_id,))
        self.conn.commit()

        report_day = self.today.date().isoformat()
        settled = settle_activity_confirmations(self.conn, report_date=report_day)
        completeness = settled["confirmation_completeness"]
        self.assertEqual(completeness["status"], "partial")
        self.assertEqual(completeness["pending_total"], 0)
        persisted = self.conn.execute(
            "SELECT completeness_status FROM activity_confirmation_settlements "
            "WHERE report_date=?",
            (report_day,),
        ).fetchone()
        self.assertEqual(persisted["completeness_status"], "partial")
        context = report_context(self.conn, report_day)["confirmation_completeness"]
        self.assertEqual(context["status"], "partial")
        self.assertEqual(context["revision"], settled["revision"])

    def test_v4_retention_holds_legacy_unresolved_without_provenance_until_terminal(self) -> None:
        old_at = self.today - timedelta(days=5)
        self._insert_activity("今天完成旧项目面试", at=old_at, app="Legacy-Pending")
        old_payload = self._materialize(
            start=old_at.replace(hour=0, minute=0),
            end=old_at.replace(hour=0, minute=0) + timedelta(days=1),
        )
        item = next(
            candidate
            for candidate in old_payload["items"]
            if candidate["safe_observation"] == "今天完成旧项目面试"
        )
        self.conn.execute(
            "UPDATE activity_confirmation_items SET source_activity_ids='[]',deletion_group_ids='[]' "
            "WHERE item_key=?",
            (item["item_key"],),
        )
        self.conn.commit()
        start = (old_at - timedelta(hours=1)).isoformat(timespec="seconds")
        end = (old_at + timedelta(hours=2)).isoformat(timespec="seconds")
        retained = delete_range(
            self.conn,
            start,
            end,
            reason="retention_expired",
            source_status="expired_deleted",
        )
        self.assertEqual(retained["retention_held"]["confirmation_count"], 1)
        self.assertIsNotNone(
            self.conn.execute(
                "SELECT 1 FROM activity_confirmation_items WHERE item_key=?",
                (item["item_key"],),
            ).fetchone()
        )
        answer_activity_confirmation(
            self.conn,
            item_key=item["item_key"],
            answer="ignored",
            reason="user_ignored",
        )
        delete_range(
            self.conn,
            start,
            end,
            reason="retention_expired",
            source_status="expired_deleted",
        )
        self.assertIsNone(
            self.conn.execute(
                "SELECT 1 FROM activity_confirmation_items WHERE item_key=?",
                (item["item_key"],),
            ).fetchone()
        )

    def test_schema_migration_preserves_explicit_owner_answer(self) -> None:
        migration_db = self.root / "schema28-explicit-owner-answer.sqlite"
        original = ensure_ready(migration_db)
        stamp = self.today.replace(hour=14).isoformat(timespec="seconds")
        rows = (
            (
                "owner_answer_occurred",
                "evidence_confirmed",
                "occurred",
                "correct",
                "owner note",
                "owner workstream",
                "completed",
                "exact",
                "explicit_claim",
            ),
            (
                "manual_owner_answer",
                "confirmed",
                "occurred",
                "correct",
                "",
                "manual workstream",
                "progressing",
                "exact",
                "manual",
            ),
        )
        for (
            item_key,
            state,
            answer,
            answer_reason,
            user_note,
            user_workstream,
            user_outcome,
            generalize_scope,
            candidate_origin,
        ) in rows:
            original.execute(
                "INSERT INTO activity_confirmation_items(item_key,event_type,safe_observation,"
                "decision_question,why_uncertain,observed_start,observed_end,fingerprint,calibration_key,"
                "state,answer,answer_reason,user_note,user_workstream,user_outcome,generalize_scope,"
                "candidate_origin,notification_eligible,created_at,updated_at,answered_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    item_key,
                    "observed_activity",
                    f"safe {item_key}",
                    "Did this happen?",
                    "legacy screen state",
                    stamp,
                    stamp,
                    f"fingerprint_{item_key}",
                    f"calibration_{item_key}",
                    state,
                    answer,
                    answer_reason,
                    user_note,
                    user_workstream,
                    user_outcome,
                    generalize_scope,
                    candidate_origin,
                    0,
                    stamp,
                    stamp,
                    stamp,
                ),
            )
        before = {
            row["item_key"]: dict(row)
            for row in original.execute(
                "SELECT item_key,answer,answer_reason,user_note,user_workstream,user_outcome,"
                "generalize_scope,candidate_origin,answered_at FROM activity_confirmation_items "
                "WHERE item_key IN ('owner_answer_occurred','manual_owner_answer')"
            ).fetchall()
        }
        original.execute("DROP TRIGGER IF EXISTS pam_schema30_guard_insert")
        original.execute("DROP TRIGGER IF EXISTS pam_schema30_guard_update")
        original.execute("DROP TRIGGER IF EXISTS pam_schema31_guard_insert")
        original.execute("DROP TRIGGER IF EXISTS pam_schema31_guard_update")
        original.execute("DROP TRIGGER IF EXISTS pam_schema32_guard_insert")
        original.execute("DROP TRIGGER IF EXISTS pam_schema32_guard_update")
        original.execute("UPDATE meta SET value='28' WHERE key='schema_version'")
        original.commit()
        original.close()

        migrated = ensure_ready(migration_db)
        try:
            after = {
                row["item_key"]: dict(row)
                for row in migrated.execute(
                    "SELECT item_key,answer,answer_reason,user_note,user_workstream,user_outcome,"
                    "generalize_scope,candidate_origin,answered_at FROM activity_confirmation_items "
                    "WHERE item_key IN ('owner_answer_occurred','manual_owner_answer')"
                ).fetchall()
            }
            self.assertEqual(after, before)
            self.assertEqual(
                dict(
                    migrated.execute(
                        "SELECT item_key,state FROM activity_confirmation_items "
                        "WHERE item_key IN ('owner_answer_occurred','manual_owner_answer')"
                    ).fetchall()
                ),
                {
                    "owner_answer_occurred": "confirmed",
                    "manual_owner_answer": "confirmed",
                },
            )
        finally:
            migrated.close()

        repeated = ensure_ready(migration_db)
        try:
            self.assertEqual(
                {
                    row["item_key"]: dict(row)
                    for row in repeated.execute(
                        "SELECT item_key,answer,answer_reason,user_note,user_workstream,user_outcome,"
                        "generalize_scope,candidate_origin,answered_at FROM activity_confirmation_items "
                        "WHERE item_key IN ('owner_answer_occurred','manual_owner_answer')"
                    ).fetchall()
                },
                before,
            )
        finally:
            repeated.close()

    def test_schema_27_partial_v28_columns_resume_backup_and_outcome_migration_fail_closed(self) -> None:
        migration_db = self.root / "schema27.sqlite"
        original = ensure_ready(migration_db)
        manual = manual_add_confirmed_activity(
            original,
            confirmation_action="MANUAL_CONFIRMED_ACTIVITY",
            safe_label="旧结果迁移",
            workstream="迁移测试",
            outcome="progressing",
            started_at=self.today.replace(hour=16).isoformat(timespec="seconds"),
        )
        item_key = manual["item"]["item_key"]
        original.execute(
            "UPDATE activity_confirmation_items SET event_type='interview',"
            "candidate_origin='explicit_claim',question_kind='occurrence',"
            "priority='normal',safe_signal_types='[]',notification_eligible=0,"
            "suggested_outcome='progressed',user_outcome='preparation' "
            "WHERE item_key=?",
            (item_key,),
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
        backup_path = migration_db.with_name(f"{migration_db.name}.schema27-backup")
        try:
            self.assertTrue(backup_path.is_file())
            self.assertEqual(backup_path.stat().st_mode & 0o777, 0o600)
            backup = sqlite3.connect(backup_path)
            try:
                self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(backup.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "27")
            finally:
                backup.close()
            row = migrated.execute(
                "SELECT suggested_outcome,user_outcome,priority,safe_signal_types,"
                "notification_eligible FROM activity_confirmation_items WHERE item_key=?",
                (item_key,),
            ).fetchone()
            self.assertEqual(
                (
                    row["suggested_outcome"],
                    row["user_outcome"],
                    row["priority"],
                    row["safe_signal_types"],
                    row["notification_eligible"],
                ),
                (
                    "progressing",
                    "preparation_only",
                    "high",
                    '["legacy_explicit_claim"]',
                    1,
                ),
            )
            self.assertEqual(migrated.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "32")
        finally:
            migrated.close()

        corrupt_db = self.root / "schema27-corrupt.sqlite"
        corrupt = ensure_ready(corrupt_db)
        corrupt.executescript(
            "DROP TRIGGER pam_schema28_guard_insert;"
            "DROP TRIGGER pam_schema28_guard_update;"
            "DROP TRIGGER pam_schema30_guard_insert;"
            "DROP TRIGGER pam_schema30_guard_update;"
            "DROP TRIGGER pam_schema31_guard_insert;"
            "DROP TRIGGER pam_schema31_guard_update;"
            "DROP TRIGGER pam_schema32_guard_insert;"
            "DROP TRIGGER pam_schema32_guard_update;"
        )
        corrupt.execute("UPDATE meta SET value='27' WHERE key='schema_version'")
        corrupt.commit()
        corrupt.close()
        corrupt_backup = corrupt_db.with_name(f"{corrupt_db.name}.schema27-backup")
        corrupt_backup.write_bytes(b"not-a-sqlite-backup")
        corrupt_backup.chmod(0o600)
        with self.assertRaisesRegex(RuntimeError, "schema_27_backup_invalid"):
            ensure_ready(corrupt_db)

        symlink_db = self.root / "schema27-symlink.sqlite"
        symlink_source = ensure_ready(symlink_db)
        symlink_source.executescript(
            "DROP TRIGGER pam_schema28_guard_insert;"
            "DROP TRIGGER pam_schema28_guard_update;"
            "DROP TRIGGER pam_schema30_guard_insert;"
            "DROP TRIGGER pam_schema30_guard_update;"
            "DROP TRIGGER pam_schema31_guard_insert;"
            "DROP TRIGGER pam_schema31_guard_update;"
            "DROP TRIGGER pam_schema32_guard_insert;"
            "DROP TRIGGER pam_schema32_guard_update;"
        )
        symlink_source.execute("UPDATE meta SET value='27' WHERE key='schema_version'")
        symlink_source.commit()
        symlink_target = self.root / "schema27-valid-external.sqlite"
        target_conn = sqlite3.connect(symlink_target)
        try:
            symlink_source.backup(target_conn)
        finally:
            target_conn.close()
            symlink_source.close()
        symlink_backup = symlink_db.with_name(f"{symlink_db.name}.schema27-backup")
        symlink_backup.symlink_to(symlink_target)
        with self.assertRaisesRegex(RuntimeError, "schema_27_backup_invalid"):
            ensure_ready(symlink_db)

        permissions_db = self.root / "schema27-permissions.sqlite"
        permissions = ensure_ready(permissions_db)
        permissions.executescript(
            "DROP TRIGGER pam_schema28_guard_insert;"
            "DROP TRIGGER pam_schema28_guard_update;"
            "DROP TRIGGER pam_schema30_guard_insert;"
            "DROP TRIGGER pam_schema30_guard_update;"
            "DROP TRIGGER pam_schema31_guard_insert;"
            "DROP TRIGGER pam_schema31_guard_update;"
            "DROP TRIGGER pam_schema32_guard_insert;"
            "DROP TRIGGER pam_schema32_guard_update;"
        )
        permissions.execute("UPDATE meta SET value='27' WHERE key='schema_version'")
        permissions.commit()
        permissions_backup = permissions_db.with_name(
            f"{permissions_db.name}.schema27-backup"
        )
        backup_conn = sqlite3.connect(permissions_backup)
        try:
            permissions.backup(backup_conn)
        finally:
            backup_conn.close()
            permissions.close()
        permissions_backup.chmod(0o644)
        remigrated = ensure_ready(permissions_db)
        try:
            self.assertEqual(permissions_backup.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                remigrated.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()[0],
                "32",
            )
        finally:
            remigrated.close()

    def test_late_user_confirmed_event_survives_bounded_report_and_review_sampling(self) -> None:
        for index in range(40):
            self._insert_activity(
                f"普通工作片段 {index}",
                at=self.today.replace(hour=8, minute=0) + timedelta(minutes=index),
                app="Codex",
            )
        late = self._new_item("今天完成浩方面试 晚间确认", at=self.today.replace(hour=21, minute=30), app="ChatGPT")
        answer_activity_confirmation(self.conn, item_key=late["item_key"], answer="occurred", reason="correct")
        report = report_context(self.conn, self.today.date().isoformat())
        self.assertLessEqual(len(report["activity_highlights"]), 32)
        self.assertTrue(any("晚间确认" in item["summary"] for item in report["activity_highlights"]))
        start = self.today.replace(hour=0, minute=0).isoformat(timespec="seconds")
        end = (self.today.replace(hour=0, minute=0) + timedelta(days=1)).isoformat(timespec="seconds")
        review = daily_review(self.conn, start, end)
        self.assertLessEqual(len(review["highlights"]), 8)
        self.assertTrue(any("晚间确认" in item for item in review["highlights"]))


if __name__ == "__main__":
    unittest.main()
