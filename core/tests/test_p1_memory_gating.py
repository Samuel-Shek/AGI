from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from activity_mvp import ha_automation, memory_approval, memory_ledger, quality_advice, timeline_export
from activity_mvp.canonical_paths import (
    LEGACY_PAM_LEDGER_RELATIVE_PATHS,
    discover_frozen_pam_ledger_paths,
    frozen_pam_ledger_path,
)
from activity_mvp.decision_queue import _queue_item_from_candidate, _queue_item_from_review_signal
from activity_mvp.lifecycle import delete_range, preview_delete_range
from activity_mvp.screen_regions import (
    ACTIONABILITY_ACTIONABLE,
    ACTIONABILITY_CONTEXT_ONLY,
    ACTIONABILITY_NOISE,
    REGION_CHAT_THREAD,
    REGION_FILE_LIST,
    REGION_MAIN_CONTENT,
    REGION_SUBMIT_AREA,
    classify_screen_region,
)
from activity_mvp.storage import ensure_ready, init_schema
from activity_mvp.user_decisions import (
    APPROVED_WRITE,
    CONTEXT_ONLY,
    PROVENANCE_COMPLETE,
    PROVENANCE_LEGACY_UNKNOWN,
    find_user_decision,
    upsert_user_decision,
)


class P1RegionAndSubjectGatingTests(unittest.TestCase):
    def test_runtime_error_sanitizers_redact_local_paths_and_internal_hashes(self) -> None:
        internal_hash = "a" * 64
        sample = (
            f"/private/var/customer/report.md /Volumes/secret/client.docx "
            f"/Applications/Private.app/data {internal_hash} raw customer sentence"
        )

        quality = quality_advice._safe_text(sample, max_len=500)
        automation = ha_automation._safe_error(sample)

        for sanitized in (quality, automation):
            self.assertNotIn("/private/", sanitized)
            self.assertNotIn("/Volumes/", sanitized)
            self.assertNotIn("/Applications/", sanitized)
            self.assertNotIn(internal_hash, sanitized)

    def test_layout_noise_and_control_geometry_override_app_prior(self) -> None:
        file_list = classify_screen_region(
            {
                "app_name": "ChatGPT",
                "screen_region_signal": {
                    "region_kind": REGION_FILE_LIST,
                    "actionability": ACTIONABILITY_ACTIONABLE,
                    "source": "ocr_geometry",
                    "reason_codes": ["ocr_region_main_content"],
                    "confidence": 0.97,
                },
            }
        )
        self.assertEqual(file_list["region_kind"], REGION_FILE_LIST)
        self.assertEqual(file_list["actionability"], ACTIONABILITY_NOISE)
        self.assertIn("layout_override_app_prior", file_list["reason_codes"])

        keyword_only = classify_screen_region(
            {"app_name": "ChatGPT", "ocr_text": "Please send this when ready"}
        )
        self.assertEqual(keyword_only["region_kind"], REGION_CHAT_THREAD)
        self.assertEqual(keyword_only["actionability"], ACTIONABILITY_CONTEXT_ONLY)

        geometry = classify_screen_region(
            {
                "app_name": "ChatGPT",
                "ocr_blocks": [
                    {
                        "text": "Send",
                        "accessibility_role": "AXButton",
                        "bounding_box": {"x": 0.86, "y": 0.84, "width": 0.08, "height": 0.08},
                    }
                ],
            }
        )
        self.assertEqual(geometry["region_kind"], REGION_SUBMIT_AREA)
        self.assertEqual(geometry["actionability"], ACTIONABILITY_ACTIONABLE)
        self.assertIn("submit_control_geometry", geometry["reason_codes"])

        main_content = classify_screen_region(
            {
                "app_name": "Obsidian",
                "screen_region_signal": {
                    "region_kind": REGION_MAIN_CONTENT,
                    "actionability": ACTIONABILITY_ACTIONABLE,
                    "source": "ocr_geometry",
                    "reason_codes": ["ocr_region_main_content"],
                    "confidence": 0.91,
                },
            }
        )
        self.assertEqual(main_content["actionability"], ACTIONABILITY_ACTIONABLE)
        self.assertIn("layout_override_app_prior", main_content["reason_codes"])

        conflict = classify_screen_region(
            {
                "app_name": "Finder",
                "screen_region_signal": {
                    "region_kind": REGION_MAIN_CONTENT,
                    "actionability": ACTIONABILITY_ACTIONABLE,
                    "source": "ocr_geometry",
                    "reason_codes": ["ocr_region_main_content"],
                    "confidence": 0.99,
                },
            }
        )
        self.assertEqual(conflict["region_kind"], REGION_FILE_LIST)
        self.assertEqual(conflict["actionability"], ACTIONABILITY_NOISE)
        self.assertIn("layout_conflict_fail_closed", conflict["reason_codes"])

    def test_untrusted_same_region_label_cannot_upgrade_app_prior(self) -> None:
        signal = classify_screen_region(
            {
                "app_name": "ChatGPT",
                "screen_region_signal": {
                    "region_kind": REGION_CHAT_THREAD,
                    "actionability": ACTIONABILITY_ACTIONABLE,
                    "source": "provided",
                    "confidence": 0.99,
                },
            }
        )
        self.assertEqual(signal["actionability"], ACTIONABILITY_CONTEXT_ONLY)
        self.assertIn("same_region_actionability_conflict_fail_closed", signal["reason_codes"])

    def test_missing_self_evidence_is_ambiguous_and_workflow_scope_is_ephemeral(self) -> None:
        assessment = timeline_export._candidate_subject_assessment(
            {
                "topic": "channel plan",
                "safe_summary": "Prepared a regional channel plan for later review.",
                "app": "Obsidian",
                "workflow": "writing",
                "candidate_type": "decision",
                "anchor_type": "trusted_app",
                "project_tags": [],
                "_document_source_categories": [],
                "_document_source_scopes": [],
                "_document_subject_scopes": [],
                "_document_memory_policies": [],
                "_document_risk_flags": [],
                "_document_classification_reasons": [],
            }
        )
        self.assertEqual(assessment["subject_scope"], "ambiguous_subject")
        self.assertIn("missing_explicit_self_ownership_evidence", assessment["subject_reasons"])

        eligibility = timeline_export._write_eligibility(
            {
                "subject_scope": "user_workflow_only",
                "evidence_count": 3,
                "candidate_type": "decision",
                "workflow": "writing",
                "anchor_type": "trusted_app",
                "app": "Obsidian",
                "project_tags": ["project"],
                "_screen_region_signals": [
                    {
                        "region_kind": REGION_MAIN_CONTENT,
                        "actionability": ACTIONABILITY_ACTIONABLE,
                        "source": "ocr_geometry",
                    "reason_codes": ["ocr_region_main_content"],
                        "confidence": 0.9,
                        "reason_codes": ["actionable_layout_preferred"],
                    }
                ],
            },
            [],
        )
        self.assertEqual(eligibility, "ephemeral")

    def test_screen_region_enforcement_is_on_by_default(self) -> None:
        with patch.dict(os.environ, {"PAM_SCREEN_REGION_ENFORCEMENT": ""}):
            self.assertTrue(timeline_export._screen_region_enforcement_enabled())
        with patch.dict(os.environ, {"PAM_SCREEN_REGION_ENFORCEMENT": "false"}):
            self.assertFalse(timeline_export._screen_region_enforcement_enabled())


class P1LedgerAndApprovalGatingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.conn = ensure_ready(self.root / "activity.sqlite")

    def tearDown(self) -> None:
        self.conn.close()
        self.temp.cleanup()

    def test_frozen_ledger_refresh_sync_and_approval_are_read_only(self) -> None:
        ledger_path = self.root / "legacy-ledger.md"
        ledger_path.write_text("# historical ledger\nkeep me\n", encoding="utf-8")
        before = ledger_path.read_bytes()
        database_changes = self.conn.total_changes

        with patch.object(memory_ledger, "_run_mem0", side_effect=AssertionError("mem0 must not run")):
            refreshed = memory_ledger.refresh_memory_ledger(self.conn, ledger_path=ledger_path)
            synced = memory_ledger.sync_memory_ledger(
                self.conn,
                ledger_path=ledger_path,
                backup_dir=self.root / "backups",
            )

        self.assertEqual(refreshed["status"], "frozen_legacy")
        self.assertEqual(refreshed["action_status"], "blocked_read_only")
        self.assertEqual(synced["action_status"], "blocked_read_only")
        self.assertEqual(ledger_path.read_bytes(), before)
        self.assertEqual(self.conn.total_changes, database_changes)
        self.assertFalse((self.root / "backups").exists())

        preview = memory_approval.preview_memory_approval(
            self.conn,
            action="delete",
            ledger_id="legacy-id",
        )
        self.assertEqual(preview["status"], "frozen_legacy_read_only")
        with self.assertRaises(RuntimeError):
            memory_ledger._rewrite_ledger_blocks(ledger_path, {})
        self.assertEqual(memory_ledger._mem0_delete("legacy-memory")["status"], "blocked_read_only")
        self.assertEqual(
            memory_ledger._mem0_update("legacy-memory", "replacement", {})["status"],
            "blocked_read_only",
        )

        legacy_plan = {
            "plan_id": "legacy-plan",
            "action": "delete",
            "expires_at_utc": "2999-01-01T00:00:00+00:00",
        }
        with patch.object(memory_approval, "_load_plan", return_value={"ok": True, "plan": legacy_plan}), patch.object(
            memory_approval,
            "_append_approval_record",
            side_effect=AssertionError("blocked ledger apply must not write a record"),
        ):
            blocked_apply = memory_approval.apply_memory_approval(self.conn, plan_id="legacy-plan")
        self.assertEqual(blocked_apply["status"], "frozen_legacy_read_only")

    def test_candidate_gate_requires_self_confirmed_action_and_complete_provenance(self) -> None:
        candidate = {
            "write_eligibility": "eligible",
            "subject_scope": "self_or_user_owned",
            "evidence_layer": "confirmed_action",
            "source_category": "general_document",
            "source_scope": "self_owned",
            "memory_write_policy": "human_approved_safe_summary",
            "quality_flags": [],
            "write_risk_flags": [],
            "subject_risk_flags": [],
            "screen_region_signal": {
                "region_kind": REGION_MAIN_CONTENT,
                "actionability": ACTIONABILITY_ACTIONABLE,
            },
            "screen_region_signals": [
                {"region_kind": REGION_MAIN_CONTENT, "actionability": ACTIONABILITY_ACTIONABLE}
            ],
            "source_package_id": "pkg-1",
            "activity_ids": ["activity-1"],
            "time_range": {"from": "2026-07-13T10:00:00+00:00", "to": "2026-07-13T11:00:00+00:00"},
            "deletion_group_ids": ["group-1"],
        }
        allowed = memory_approval._candidate_gate(
            candidate,
            action="approve_candidate",
            memory_text="Local Owner confirmed this durable preference.",
        )
        self.assertTrue(allowed["allowed"])

        missing_provenance = {**candidate, "deletion_group_ids": []}
        blocked = memory_approval._candidate_gate(
            missing_provenance,
            action="approve_candidate",
            memory_text="Local Owner confirmed this durable preference.",
        )
        self.assertFalse(blocked["allowed"])
        self.assertIn("来源回执不完整", blocked["message"])

        workflow_only = {**candidate, "subject_scope": "user_workflow_only"}
        blocked_subject = memory_approval._candidate_gate(
            workflow_only,
            action="approve_candidate",
            memory_text="Local Owner confirmed this durable preference.",
        )
        self.assertFalse(blocked_subject["allowed"])
        self.assertIn("主体归属不安全", blocked_subject["message"])

    def test_mem0_response_needs_an_id_receipt(self) -> None:
        self.assertFalse(memory_approval._mem0_receipt({"status": "ok"})["verified"])
        receipt = memory_approval._mem0_receipt({"results": [{"id": "memory-123"}]})
        self.assertTrue(receipt["verified"])
        self.assertEqual(receipt["memory_ids"], ["memory-123"])

    def test_controlled_mem0_writer_stays_disabled_without_source_revocation_cascade(self) -> None:
        plan = {
            "plan_id": "plan-1",
            "action": "approve_candidate",
            "package_id": "pkg-1",
            "candidate_key": "candidate-1",
            "memory_text": "Local Owner confirmed this durable preference.",
        }
        database_changes = self.conn.total_changes
        self.assertFalse(memory_approval.CONTROLLED_MEM0_WRITER_ENABLED)
        with patch.object(
            memory_approval,
            "_load_candidate_package",
            side_effect=AssertionError("disabled writer must fail before loading a package"),
        ), patch.object(
            memory_approval,
            "_write_backup",
            side_effect=AssertionError("disabled writer must not create a backup"),
        ), patch.object(
            memory_approval,
            "_mem0_add",
            side_effect=AssertionError("disabled writer must not call mem0"),
        ), patch.object(
            memory_approval,
            "_insert_audit_log",
            side_effect=AssertionError("disabled writer must not create an audit row"),
        ):
            result = memory_approval._apply_candidate_action(self.conn, plan, plan_dir=self.root)
        self.assertEqual(result["status"], "controlled_mem0_writer_disabled")
        self.assertIn("delete/retention", result["message"])
        self.assertEqual(self.conn.total_changes, database_changes)

        with patch.object(
            memory_approval,
            "_run_mem0",
            side_effect=AssertionError("disabled writer must not launch the mem0 CLI"),
        ):
            direct = memory_approval._mem0_add("safe summary", {"source_package_id": "pkg-1"})
        self.assertFalse(direct["ok"])
        self.assertEqual(direct["status"], "controlled_mem0_writer_disabled")

        preview_dir = self.root / "plans"
        preview = memory_approval.preview_memory_approval(
            self.conn,
            action="approve_candidate",
            package_id="pkg-1",
            candidate_key="candidate-1",
            plan_dir=preview_dir,
        )
        self.assertEqual(preview["status"], "controlled_mem0_writer_disabled")
        self.assertFalse(preview_dir.exists())

        queue_item = _queue_item_from_candidate(
            {
                "candidate_key": "candidate-1",
                "safe_summary": "Candidate summary",
                "write_eligibility": "eligible",
                "can_approve": True,
                "can_promote": False,
            },
            package_id="pkg-1",
        )
        self.assertFalse(queue_item["can_direct_write"])
        self.assertNotIn("direct-write", queue_item["available_actions"])
        self.assertEqual(queue_item["queue_bucket"], "context_only")
        self.assertIn("delete/retention", queue_item["direct_write_disabled_reason"])

    def test_review_signal_has_no_write_action(self) -> None:
        item = _queue_item_from_review_signal(
            {"safe_summary": "Possible follow up", "time_label": "10:00"},
            source_kind="ai_review_follow_up",
            source_label="Review follow up",
            time_range={"from": "2026-07-13T10:00:00+00:00", "to": "2026-07-13T11:00:00+00:00"},
        )
        self.assertFalse(item["can_direct_write"])
        self.assertFalse(item["can_rewrite_write"])
        self.assertNotIn("rewrite-write", item["available_actions"])
        self.assertEqual(item["time_range"]["from"], "2026-07-13T10:00:00+00:00")


class P1HermesIsolationTests(unittest.TestCase):
    def test_quality_and_hourly_commands_are_inline_analysis_only(self) -> None:
        quality_command = quality_advice.build_quality_advice_command(
            package_id="pkg-quality",
            start="2026-07-12T00:00:00+00:00",
            end="2026-07-13T00:00:00+00:00",
            run_key="2026-07-12",
            hermes_profile="activity-quality-analysis",
            root_dir="/opt/pam-private/runtime",
            db_path="/opt/pam-private/activity.sqlite",
            hermes_bin="/usr/local/bin/hermes",
            analysis_payload={"safe_summary": "bounded summary", "raw_ocr": "DO_NOT_EXPOSE"},
        )
        hourly_command = ha_automation.build_hermes_trigger_command(
            package_id="pkg-hour",
            start="2026-07-13T10:00:00+00:00",
            end="2026-07-13T11:00:00+00:00",
            hour_key="2026-07-13T10:00:00+00:00",
            hermes_profile="activity-analysis",
            root_dir="/opt/pam-private/runtime",
            db_path="/opt/pam-private/activity.sqlite",
            hermes_bin="/usr/local/bin/hermes",
            analysis_payload={"source_status": "active", "memory_candidates": []},
        )

        for command in (quality_command, hourly_command):
            self.assertIn("--ignore-rules", command)
            self.assertEqual(command[command.index("-t") + 1], "clarify")
            encoded = " ".join(command)
            self.assertNotIn("/opt/pam-private/runtime", encoded)
            self.assertNotIn("/opt/pam-private/activity.sqlite", encoded)
            input_line = next(
                line for line in command[-1].splitlines() if line.startswith("analysis_input_json: ")
            )
            json.loads(input_line.removeprefix("analysis_input_json: "))
        self.assertNotIn("DO_NOT_EXPOSE", quality_command[-1])

        with self.assertRaisesRegex(ValueError, "clarify"):
            quality_advice.build_quality_advice_command(
                package_id="pkg",
                start="start",
                end="end",
                run_key="key",
                hermes_profile="profile",
                root_dir="/opt/pam-private/runtime",
                db_path="/opt/pam-private/activity.sqlite",
                hermes_bin="/usr/local/bin/hermes",
                toolsets="terminal",
            )
        with self.assertRaisesRegex(ValueError, "clarify"):
            ha_automation.build_hermes_trigger_command(
                package_id="pkg",
                start="start",
                end="end",
                hour_key="hour",
                hermes_profile="profile",
                root_dir="/opt/pam-private/runtime",
                db_path="/opt/pam-private/activity.sqlite",
                hermes_bin="/usr/local/bin/hermes",
                toolsets="terminal,memory",
            )

    def test_large_inline_analysis_inputs_remain_bounded_valid_json(self) -> None:
        candidate = {
            "candidate_key": "candidate",
            "safe_summary": "summary " * 800,
            "confirmation_signals": ["confirmed " * 100] * 12,
            "quality_flags": ["quality " * 100] * 12,
            "write_risk_flags": ["risk " * 100] * 12,
        }
        safe_hourly = ha_automation._safe_analysis_payload(
            {
                "source_status": "active",
                "memory_candidates": [candidate] * 20,
                "review_only_candidates": [candidate] * 20,
            }
        )
        hourly_json = ha_automation._bounded_analysis_json(safe_hourly)
        self.assertLessEqual(len(hourly_json), 24000)
        self.assertTrue(json.loads(hourly_json)["input_truncated"])

        safe_quality = quality_advice._sanitize_value(
            {
                "quality_summary": {"status": "ok", "summary": "quality " * 5000},
                "package_summary": {"memory_candidates": [candidate] * 20},
            }
        )
        quality_json = quality_advice._bounded_quality_json(safe_quality)
        self.assertLessEqual(len(quality_json), 24000)
        self.assertTrue(json.loads(quality_json)["input_truncated"])

    def test_model_self_report_never_becomes_a_write_receipt(self) -> None:
        completed = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "status": "completed",
                    "memories_written_count": 7,
                    "written_candidate_count": 7,
                    "written_memory_summaries": ["model claimed write"],
                }
            ),
            stderr="",
        )
        with patch.object(ha_automation.subprocess, "run", return_value=completed):
            result = ha_automation._run_hermes_command(["hermes"], cwd=Path.cwd())

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["hermes_result_status"], "analysis_only")
        self.assertEqual(result["memories_written_count"], 0)
        self.assertEqual(result["written_candidate_count"], 0)
        self.assertFalse(result["write_verified"])
        parsed = json.loads(result["hermes_result_json"])
        self.assertEqual(parsed["memories_written_count"], 0)
        self.assertFalse(parsed["write_verified"])

    def test_feishu_copy_marks_unverified_model_claim_as_analysis(self) -> None:
        message = ha_automation._memory_write_report_message(
            package_id="pkg",
            hour_key="hour",
            hour_start="start",
            hour_end="end",
            hermes_profile="activity-analysis",
            hermes_result={"memories_written_count": 3, "write_verified": True, "mem0_receipt_ids": []},
        )
        self.assertIn("未核验", message)
        self.assertIn("analysis/advice", message)
        self.assertIn("不代表长期记忆已写入", message)

    def test_unverified_model_claim_never_reaches_feishu_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = ensure_ready(Path(tmp) / "activity.sqlite")
            try:
                with patch.object(
                    ha_automation,
                    "_resolve_memory_report_target",
                    side_effect=AssertionError("unverified claim must not resolve a Feishu target"),
                ), patch.object(
                    ha_automation,
                    "_send_memory_report_message",
                    side_effect=AssertionError("unverified claim must not be delivered"),
                ):
                    result = ha_automation._attempt_memory_write_report(
                        conn,
                        hour_key="2026-07-13T10:00:00+00:00",
                        package_id="pkg",
                        hour_start="2026-07-13T10:00:00+00:00",
                        hour_end="2026-07-13T11:00:00+00:00",
                        hermes_profile="activity-analysis",
                        hermes_result={
                            "memories_written_count": 3,
                            "write_verified": True,
                            "mem0_receipt_ids": [],
                        },
                    )
            finally:
                conn.close()

        self.assertEqual(result["status"], "blocked_unverified")
        self.assertEqual(result["error"], "unverified_write_claim")

    def test_legacy_unverified_memory_report_is_removed_from_retry_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = ensure_ready(Path(tmp) / "activity.sqlite")
            try:
                conn.execute(
                    """
                    INSERT INTO ha_automation_jobs(
                      id, hour_key, started_at, ended_at, trigger_mode, hermes_profile,
                      status, memories_written_count, hermes_result_json,
                      memory_report_status, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "legacy-job",
                        "2026-07-13T09:00:00+00:00",
                        "2026-07-13T09:00:00+00:00",
                        "2026-07-13T10:00:00+00:00",
                        "hermes",
                        "activity-analysis",
                        "completed",
                        2,
                        json.dumps({"status": "completed", "write_verified": False}),
                        "pending",
                        "2026-07-13T10:00:00+00:00",
                        "2026-07-13T10:00:00+00:00",
                    ),
                )
                conn.commit()
                ha_automation.ensure_ha_automation_defaults(conn)
                row = conn.execute(
                    "SELECT memory_report_status, memory_report_error, memory_report_next_retry_at "
                    "FROM ha_automation_jobs WHERE id = 'legacy-job'"
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(row["memory_report_status"], "blocked_unverified")
        self.assertEqual(row["memory_report_error"], "unverified_write_claim")
        self.assertEqual(row["memory_report_next_retry_at"], "")


class P1DecisionProvenanceLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.conn = ensure_ready(self.root / "activity.sqlite")

    def tearDown(self) -> None:
        self.conn.close()
        self.temp.cleanup()

    def test_legacy_schema_migrates_to_unknown_without_inventing_provenance(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE user_decisions(
              item_key TEXT PRIMARY KEY, item_type TEXT NOT NULL, source_kind TEXT NOT NULL,
              action TEXT NOT NULL, safe_summary TEXT NOT NULL, user_note TEXT,
              decision_signature TEXT NOT NULL, generalize_scope TEXT NOT NULL,
              applied_plan_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO user_decisions VALUES(
              'legacy', 'memory_candidate', 'memory_candidate', 'approved-write',
              'legacy summary', '', 'sig-legacy', 'exact', '',
              '2026-07-01T00:00:00+00:00', '2026-07-01T00:00:00+00:00'
            )
            """
        )

        init_schema(conn)
        row = conn.execute(
            "SELECT provenance_status, source_package_id, source_activity_ids, deletion_group_ids FROM user_decisions"
        ).fetchone()
        self.assertEqual(row["provenance_status"], PROVENANCE_LEGACY_UNKNOWN)
        self.assertIsNone(row["source_package_id"])
        self.assertEqual(json.loads(row["source_activity_ids"]), [])
        self.assertEqual(json.loads(row["deletion_group_ids"]), [])
        conn.close()

    def test_write_decision_requires_complete_reversible_provenance(self) -> None:
        item = {
            "item_key": "write-without-source",
            "safe_summary": "Local Owner confirmed a stable preference.",
            "decision_signature": "sig-write-without-source",
            "subject_scope": "self_or_user_owned",
        }
        with self.assertRaisesRegex(ValueError, "complete reversible source provenance"):
            upsert_user_decision(
                self.conn,
                item=item,
                source_kind="memory_candidate",
                item_type="memory_candidate",
                action=APPROVED_WRITE,
                generalize_scope="exact",
            )

    def test_delete_range_cascades_traceable_decisions_and_preserves_legacy_unknown(self) -> None:
        now = "2026-07-13T10:00:00+00:00"
        self.conn.execute(
            """
            INSERT INTO user_decisions(
              item_key, item_type, source_kind, action, safe_summary, user_note,
              decision_signature, generalize_scope, applied_plan_id, created_at, updated_at
            ) VALUES('legacy-unknown', 'memory_candidate', 'memory_candidate', 'approved-write',
                     'legacy unknown summary', '', 'sig-legacy-unknown', 'exact', '', ?, ?)
            """,
            (now, now),
        )
        upsert_user_decision(
            self.conn,
            item={
                "item_key": "traceable-context",
                "safe_summary": "Context decision tied to this source window.",
                "decision_signature": "sig-traceable-context",
                "time_range": {"from": now, "to": "2026-07-13T11:00:00+00:00"},
            },
            source_kind="ai_review_follow_up",
            item_type="review_signal",
            action=CONTEXT_ONLY,
            generalize_scope="exact",
        )
        complete_item = {
            "item_key": "complete-write",
            "safe_summary": "Local Owner confirmed a durable preference tied to a reversible source.",
            "decision_signature": "sig-complete-write",
            "subject_scope": "self_or_user_owned",
            "source_package_id": "pkg-complete",
            "activity_ids": ["activity-complete"],
            "time_range": {"from": now, "to": "2026-07-13T11:00:00+00:00"},
            "deletion_group_ids": ["group-complete"],
        }
        stored = upsert_user_decision(
            self.conn,
            item=complete_item,
            source_kind="memory_candidate",
            item_type="memory_candidate",
            action=APPROVED_WRITE,
            generalize_scope="exact",
        )
        self.assertEqual(stored["provenance_status"], PROVENANCE_COMPLETE)
        self.assertIsNotNone(
            find_user_decision(
                self.conn,
                {**complete_item, "candidate_key": complete_item["item_key"]},
                source_kind="memory_candidate",
                item_type="memory_candidate",
            )
        )

        start = "2026-07-13T10:15:00+00:00"
        end = "2026-07-13T10:45:00+00:00"
        preview = preview_delete_range(self.conn, start, end)
        applied = delete_range(self.conn, start, end)

        self.assertEqual(preview["cascade"]["user_decisions"], 2)
        self.assertEqual(applied["cascade"]["user_decisions"], 2)
        self.assertEqual(preview["cascade"]["user_decisions_legacy_unknown_preserved"], 1)
        self.assertEqual(applied["cascade"]["user_decisions_legacy_unknown_preserved"], 1)
        remaining = [
            row["item_key"]
            for row in self.conn.execute("SELECT item_key FROM user_decisions ORDER BY item_key").fetchall()
        ]
        self.assertEqual(remaining, ["legacy-unknown"])
        ignored = find_user_decision(
            self.conn,
            {
                "item_key": "legacy-unknown",
                "safe_summary": "legacy unknown summary",
                "decision_signature": "sig-legacy-unknown",
            },
            source_kind="memory_candidate",
            item_type="memory_candidate",
        )
        self.assertIsNone(ignored)
        legacy_row = self.conn.execute("SELECT * FROM user_decisions WHERE item_key = 'legacy-unknown'").fetchone()
        self.assertEqual(legacy_row["provenance_status"], PROVENANCE_LEGACY_UNKNOWN)

    def test_canonical_ledger_discovery_is_bounded_and_read_only(self) -> None:
        canonical = frozen_pam_ledger_path(home=self.root)
        canonical.parent.mkdir(parents=True)
        canonical.write_text("canonical", encoding="utf-8")
        legacy = self.root / LEGACY_PAM_LEDGER_RELATIVE_PATHS[0]
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text("legacy", encoding="utf-8")

        discovered = discover_frozen_pam_ledger_paths(home=self.root)
        self.assertEqual(discovered, [canonical, legacy])

    def test_capture_coverage_today_grouping_uses_expression_index_after_migration(self) -> None:
        self.conn.execute("DROP INDEX idx_capture_coverage_day_display")
        init_schema(self.conn)
        index_row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'idx_capture_coverage_day_display'"
        ).fetchone()
        self.assertIsNotNone(index_row)
        self.assertIn("datetime(started_at)", index_row["sql"])

        plan = self.conn.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT COALESCE(display_id, 'unknown') AS display_id,
                   MAX(started_at), COALESCE(SUM(gap_count), 0)
            FROM capture_coverage_ledger
            WHERE datetime(started_at) >= datetime(?)
              AND datetime(started_at) < datetime(?)
            GROUP BY display_id
            """,
            ("2026-07-13T00:00:00+00:00", "2026-07-14T00:00:00+00:00"),
        ).fetchall()
        details = " ".join(str(row["detail"]) for row in plan)
        self.assertIn("USING INDEX idx_capture_coverage_day_display", details)
        self.assertNotIn("SCAN capture_coverage_ledger", details)


if __name__ == "__main__":
    unittest.main()
