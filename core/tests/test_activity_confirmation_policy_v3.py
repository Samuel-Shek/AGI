from __future__ import annotations

import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from activity_mvp.activity_confirmations import (
    CONDITIONAL_COMPLETION_RE,
    CONFIRMATION_POLICY_SETTING_KEY,
    CONFIRMATION_POLICY_VERSION,
    _candidate_for_activity,
    _event_match,
    _rows_can_group,
    revalidate_unresolved_confirmation_policy,
    revalidate_unresolved_confirmation_policy_v3,
    upsert_activity_confirmation_candidate,
)
from activity_mvp.storage import connect, ensure_ready


class ActivityConfirmationPolicyV5Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "activity.sqlite"
        self.conn = ensure_ready(self.db_path)
        self.base = datetime.now().astimezone().replace(hour=9, minute=0, second=0, microsecond=0)

    def tearDown(self) -> None:
        self.conn.close()
        self.tempdir.cleanup()

    def _activity(
        self,
        text: str,
        *,
        app: str = "Obsidian",
        at: datetime | None = None,
        region_kind: str = "main_content",
        actionability: str = "actionable",
        reason_codes: list[str] | None = None,
        document_contexts: list[dict] | None = None,
    ) -> dict:
        started = at or self.base
        return {
            "activity_summary": text,
            "cleaned_text": text,
            "cleaned_title": "",
            "apps": [app],
            "started_at": started.isoformat(timespec="seconds"),
            "ended_at": (started + timedelta(minutes=5)).isoformat(timespec="seconds"),
            "screen_region_signal": {
                "region_kind": region_kind,
                "actionability": actionability,
                "reason_codes": reason_codes or [],
            },
            "document_contexts": document_contexts or [],
            "deletion_group_id": "",
        }

    def test_noise_is_system_filtered_and_concrete_main_content_remains_pending(self) -> None:
        cases = (
            self._activity("已完成 & 已放弃 30", app="TickTick", region_kind="task_list", actionability="context_only"),
            self._activity("全量测试已完成", app="ChatGPT"),
            self._activity("已完成 2/4", app="WorkBuddy"),
            self._activity("待确认 判断依据 发生了 没有发生 忽略该问题 已完成", app="Accessibility Services"),
        )
        for activity in cases:
            candidate = _candidate_for_activity(self.conn, activity)
            self.assertIsNotNone(candidate)
            self.assertEqual(candidate["state"], "filtered")
            self.assertTrue(any(code.startswith("policy_v7_") for code in candidate["reason_codes"]))

        anonymous = _candidate_for_activity(self.conn, self._activity("今天完成面试"))
        self.assertEqual(anonymous["state"], "pending")
        self.assertFalse(anonymous["subject"]["company_identified"])
        self.assertEqual(anonymous["decision_question"], "")

        concrete = _candidate_for_activity(self.conn, self._activity("今天我已完成石头科技以色列市场报告"))
        self.assertEqual(concrete["state"], "pending")
        self.assertEqual(concrete["subject"]["object"], "石头科技以色列市场报告")
        self.assertTrue(concrete["subject"]["object_identified"])

        interview = _candidate_for_activity(self.conn, self._activity("今天完成浩方二面"))
        self.assertEqual(interview["state"], "pending")
        self.assertEqual(interview["subject"]["company"], "浩方")
        self.assertEqual(interview["subject"]["round"], "第二轮")

    def test_tool_status_filter_does_not_reject_concrete_work_in_normal_apps(self) -> None:
        for text in (
            "今天我已完成客户投诉处理方案",
            "今天我已完成客户部署方案",
            "Customer deployment completed",
            "Client build completed",
        ):
            candidate = _candidate_for_activity(self.conn, self._activity(text, app="Obsidian"))
            self.assertEqual(candidate["state"], "pending")
            self.assertNotIn("policy_v7_tool_status", candidate["reason_codes"])

        misattributed_status = _candidate_for_activity(
            self.conn,
            self._activity("全量测试已完成", app="Accessibility Services"),
        )
        self.assertEqual(misattributed_status["state"], "filtered")
        self.assertIn("policy_v7_tool_status", misattributed_status["reason_codes"])

    def test_ai_plans_and_execution_claims_require_independent_execution_evidence(self) -> None:
        for text in (
            "AI 计划总结：客户报告已完成（待执行）",
            "AI 执行声明：已经完成客户报告",
            "进度总结：下一步将标记为已完成客户报告",
        ):
            candidate = _candidate_for_activity(self.conn, self._activity(text, app="ChatGPT"))
            self.assertIsNotNone(candidate, text)
            self.assertEqual(candidate["state"], "filtered", text)
            self.assertIn("policy_v7_ai_plan_or_summary", candidate["reason_codes"], text)

        independent_submit = _candidate_for_activity(
            self.conn,
            self._activity(
                "AI 执行声明：已提交浩方申请",
                app="ChatGPT",
                region_kind="submit_area",
                reason_codes=["submit_interaction_evidence"],
            ),
        )
        self.assertEqual(independent_submit["state"], "pending")
        self.assertIn("direct_submit_screen_signal_requires_confirmation", independent_submit["reason_codes"])
        self.assertNotIn("policy_v7_ai_plan_or_summary", independent_submit["reason_codes"])
        self.assertEqual(independent_submit["decision_question"], "")
        self.assertFalse(independent_submit["notification_eligible"])
        self.assertIn(
            "policy_v7_information_insufficient_ai_tool_activity_unverified",
            independent_submit["reason_codes"],
        )

    def test_ai_chat_submit_geometry_does_not_make_aggregate_counts_answerable(self) -> None:
        for app in ("ChatGPT", "Claude", "Codex"):
            for text in (
                "已提交8个内容项和132条文件变动记录都完整处理",
                "Atlas季度预算申请已提交",
                "Atlas报价单已发送",
                "Atlas网站已发布",
                "Atlas账目核对已完成",
            ):
                with self.subTest(app=app, text=text):
                    candidate = _candidate_for_activity(
                        self.conn, self._activity(
                            text, app=app, region_kind="submit_area",
                            reason_codes=["submit_control_geometry"],
                        ),
                    )
                    self.assertIsNotNone(candidate)
                    self.assertEqual(candidate["state"], "pending")
                    self.assertEqual(candidate["decision_question"], "")
                    self.assertFalse(candidate["notification_eligible"])
                    self.assertEqual(candidate["priority"], "low")

    def test_generated_document_completion_needs_independent_file_evidence_to_be_asked(
        self,
    ) -> None:
        text = "已生成一份25页、可直接交付的 Word 版报告，报告已完成"
        unverified = _candidate_for_activity(
            self.conn,
            self._activity(text, app="Dia"),
        )
        verified = _candidate_for_activity(
            self.conn,
            self._activity(
                text,
                app="Dia",
                at=self.base + timedelta(hours=1),
                document_contexts=[
                    {
                        "document_ref": "hmac-ref",
                        "file_name": "report.docx",
                        "change_summary": "检测到文档修改",
                        "subsequent_mtime_event": "modified_near_capture",
                    }
                ],
            ),
        )

        self.assertEqual(unverified["state"], "pending")
        self.assertEqual(unverified["decision_question"], "")
        self.assertFalse(unverified["notification_eligible"])
        self.assertIn(
            "policy_v7_information_insufficient_generated_artifact_unverified",
            unverified["reason_codes"],
        )
        self.assertEqual(verified["state"], "pending")
        self.assertIn("25页 Word 版报告", verified["decision_question"])
        self.assertTrue(verified["notification_eligible"])
        self.assertIn("independent_file_evidence", verified["safe_signal_types"])

    def test_candidate_creation_fail_closes_malformed_subject_classes(self) -> None:
        cases = (
            ("外贸已发送", "Dia", "information"),
            ("有了已发送", "Dia", "information"),
            ("Duo Staging 已发布", "ChatGPT", "information"),
            (
                "在 entry 的 emitted_report_hashes 中检测到同日正文哈希，已发送",
                "ChatGPT",
                "filtered",
            ),
            ("发布成功，但还没有", "ChatGPT", "filtered"),
        )
        for index, (text, app, expected) in enumerate(cases):
            candidate = _candidate_for_activity(
                self.conn,
                self._activity(
                    text,
                    app=app,
                    at=self.base + timedelta(minutes=index * 20),
                ),
            )
            self.assertIsNotNone(candidate, text)
            if expected == "filtered":
                self.assertEqual(candidate["state"], "filtered", text)
            else:
                self.assertEqual(candidate["state"], "pending", text)
                self.assertEqual(candidate["decision_question"], "", text)
                self.assertFalse(candidate["notification_eligible"], text)

        private_direct = _candidate_for_activity(
            self.conn,
            self._activity(
                "候选人林女士 星河人力资源专员 ［送达］ 收到 已发送",
                app="Dia",
                at=self.base + timedelta(hours=3),
                region_kind="submit_area",
                reason_codes=["submit_interaction_evidence"],
            ),
        )
        rendered = json.dumps(private_direct, ensure_ascii=False)
        self.assertEqual(private_direct["state"], "pending")
        self.assertEqual(private_direct["decision_question"], "")
        self.assertNotIn("林女士", rendered)
        self.assertNotIn("星河", rendered)
        self.assertNotIn("收到", rendered)

    def test_candidate_preserves_named_publish_products_and_technical_report(self) -> None:
        for index, object_label in enumerate(
            (
                "天命鉴 Staging 网站",
                "Solo Staging 网站",
                "TianMing 天命鉴 Staging 网站",
            )
        ):
            candidate = _candidate_for_activity(
                self.conn,
                self._activity(
                    f"{object_label}已发布",
                    at=self.base + timedelta(minutes=index * 20),
                ),
            )
            self.assertEqual(candidate["state"], "pending")
            self.assertEqual(candidate["subject"]["object"], object_label)
            self.assertIn(object_label, candidate["decision_question"])

        technical_report = _candidate_for_activity(
            self.conn,
            self._activity(
                "rate_limit 状态报告已完成",
                at=self.base + timedelta(hours=2),
            ),
        )
        self.assertEqual(technical_report["state"], "pending")
        self.assertEqual(
            technical_report["subject"]["object"],
            "rate_limit 状态报告",
        )
        self.assertNotIn(
            "policy_v7_internal_field_status",
            technical_report["reason_codes"],
        )

    def test_recruitment_app_context_sanitizes_delivery_ocr_without_false_positive(
        self,
    ) -> None:
        text = "张女士 ［送达］ 收到 已发送"
        recruitment = _candidate_for_activity(
            self.conn,
            self._activity(text, app="BOSS直聘"),
        )
        ordinary_chat = _candidate_for_activity(
            self.conn,
            self._activity(
                text,
                app="Messages",
                at=self.base + timedelta(hours=1),
            ),
        )

        rendered = json.dumps(recruitment, ensure_ascii=False)
        self.assertEqual(recruitment["state"], "pending")
        self.assertEqual(recruitment["decision_question"], "")
        self.assertNotIn("张女士", rendered)
        self.assertNotIn("收到", rendered)
        self.assertNotIn("送达］", rendered)
        self.assertEqual(ordinary_chat["state"], "pending")
        self.assertTrue(ordinary_chat["decision_question"])
        self.assertNotIn(
            "policy_v7_information_insufficient_recruitment_chat_privacy",
            ordinary_chat["reason_codes"],
        )

    def test_negated_occurrence_language_is_filtered(self) -> None:
        for text in (
            "不能宣称整库已经完成 98% OCR 与扫描页码级溯源",
            "这不代表发布已经完成",
            "目前无法确认任务已经完成",
        ):
            candidate = _candidate_for_activity(self.conn, self._activity(text, app="ChatGPT"))
            self.assertIsNotNone(candidate, text)
            self.assertEqual(candidate["state"], "filtered", text)
            self.assertIn("policy_v7_negated_occurrence", candidate["reason_codes"], text)

        self.assertIsNone(
            _candidate_for_activity(
                self.conn,
                self._activity("该计划尚未完成客户报告", app="ChatGPT"),
            )
        )

    def test_policy_v5_rejects_conditional_completion_without_overfiltering_explicit_completion(self) -> None:
        for text in (
            "After the task is complete, show the pending card",
            "When the job completes, display the status",
            "Once the task finishes, enable the next step",
            "Show the pending card after the task is complete",
        ):
            self.assertIsNotNone(CONDITIONAL_COMPLETION_RE.search(text), text)
        self.assertIsNone(
            CONDITIONAL_COMPLETION_RE.search(
                "Before the task completed, I had already sent the client report"
            )
        )

        conditional_cases = (
            "现在已优化为：Desktop的异步子任务完成后显示待处理卡片",
            "当任务完成时自动弹出通知",
            "Once the background task is completed then show the pending card",
            "After the background task is completed, show the pending card",
        )
        for text in conditional_cases:
            candidate = _candidate_for_activity(self.conn, self._activity(text, app="ChatGPT"))
            self.assertIsNotNone(candidate)
            self.assertEqual(candidate["state"], "filtered")
            self.assertIn("policy_v7_conditional_completion", candidate["reason_codes"])

        for text in (
            "今天我已完成客户市场报告",
            "异步任务已经完成",
            "After lunch, I completed the client report",
            "Before the task completed, I had already sent the client report",
        ):
            candidate = _candidate_for_activity(self.conn, self._activity(text, app="Obsidian"))
            self.assertIsNotNone(candidate)
            self.assertEqual(candidate["state"], "pending")
            self.assertNotIn("policy_v7_conditional_completion", candidate["reason_codes"])

        explicit_with_separate_product_copy = _candidate_for_activity(
            self.conn,
            self._activity(
                "今天我已完成客户市场报告。产品设计是任务完成后显示待处理卡片",
                app="Obsidian",
            ),
        )
        self.assertEqual(explicit_with_separate_product_copy["state"], "pending")
        self.assertNotIn(
            "policy_v7_conditional_completion",
            explicit_with_separate_product_copy["reason_codes"],
        )

    def test_direct_submit_remains_pending_without_a_guessed_object(self) -> None:
        candidate = _candidate_for_activity(
            self.conn,
            self._activity(
                "发布成功",
                app="CMS",
                region_kind="submit_area",
                reason_codes=["submit_interaction_evidence"],
            ),
        )
        self.assertEqual(candidate["state"], "pending")
        self.assertIn("direct_submit_screen_signal_requires_confirmation", candidate["reason_codes"])
        self.assertFalse(candidate["subject"]["object_identified"])

    def test_trigger_excerpt_preserves_match_after_long_prefix(self) -> None:
        match = _event_match("前缀" * 800 + " 今天我已完成客户市场报告 后续")
        self.assertIsNotNone(match)
        self.assertIn("已完成", match[2])
        self.assertLessEqual(len(match[2]), 280)

    def test_grouping_uses_day_event_and_concrete_subject_only(self) -> None:
        subject = {"object": "客户市场报告", "object_identified": True}
        left = {
            "event_type": "completion",
            "observed_start": self.base.isoformat(timespec="seconds"),
            "observed_end": (self.base + timedelta(minutes=5)).isoformat(timespec="seconds"),
            "app_family": "Obsidian",
            "subject": subject,
            "evidence": "09:00 今天我已完成客户市场报告 1/4",
        }
        right = {
            **left,
            "observed_start": (self.base + timedelta(hours=8)).isoformat(timespec="seconds"),
            "observed_end": (self.base + timedelta(hours=8, minutes=5)).isoformat(timespec="seconds"),
            "app_family": "Accessibility Services",
            "evidence": "17:00 客户市场报告已完成 4/4",
        }
        self.assertTrue(_rows_can_group(left, right))
        self.assertFalse(_rows_can_group(left, {**right, "subject": {"object": "其他报告", "object_identified": True}}))
        self.assertFalse(
            _rows_can_group(
                left,
                {**right, "observed_start": (self.base + timedelta(days=1)).isoformat(timespec="seconds")},
            )
        )
        short_prefix = {
            **left,
            "subject": {"object": "客户报告", "object_identified": True},
        }
        self.assertFalse(
            _rows_can_group(
                short_prefix,
                {
                    **short_prefix,
                    "observed_start": (self.base + timedelta(minutes=5)).isoformat(timespec="seconds"),
                    "observed_end": (self.base + timedelta(minutes=10)).isoformat(timespec="seconds"),
                    "subject": {"object": "客户报告验收", "object_identified": True},
                },
            )
        )

    def test_repeatable_events_only_group_inside_continuous_window(self) -> None:
        subject = {"object": "客户日报", "object_identified": True}
        left = {
            "event_type": "send",
            "observed_start": self.base.isoformat(timespec="seconds"),
            "observed_end": (self.base + timedelta(minutes=5)).isoformat(timespec="seconds"),
            "app_family": "WeChat",
            "subject": subject,
            "evidence": "客户日报已发送",
        }
        self.assertTrue(
            _rows_can_group(
                left,
                {
                    **left,
                    "observed_start": (self.base + timedelta(minutes=5)).isoformat(timespec="seconds"),
                    "observed_end": (self.base + timedelta(minutes=10)).isoformat(timespec="seconds"),
                },
            )
        )
        self.assertFalse(
            _rows_can_group(
                left,
                {
                    **left,
                    "observed_start": (self.base + timedelta(hours=8)).isoformat(timespec="seconds"),
                    "observed_end": (self.base + timedelta(hours=8, minutes=5)).isoformat(timespec="seconds"),
                },
            )
        )

    def test_revalidator_is_idempotent_and_preserves_answers_and_calibration(self) -> None:
        noisy = upsert_activity_confirmation_candidate(
            self.conn,
            self._activity("已完成 & 已放弃 30", app="TickTick", region_kind="task_list", actionability="context_only"),
        )
        deferred = upsert_activity_confirmation_candidate(
            self.conn,
            self._activity("今天我已完成客户市场报告", at=self.base + timedelta(hours=1)),
        )
        answered = upsert_activity_confirmation_candidate(
            self.conn,
            self._activity("今天完成浩方二面", at=self.base + timedelta(hours=2)),
        )
        self.conn.execute("UPDATE activity_confirmation_items SET state='pending' WHERE item_key=?", (noisy["item_key"],))
        self.conn.execute("UPDATE activity_confirmation_items SET state='deferred' WHERE item_key=?", (deferred["item_key"],))
        self.conn.execute(
            "UPDATE activity_confirmation_items SET state='confirmed',answer='occurred',answered_at=? WHERE item_key=?",
            (self.base.isoformat(timespec="seconds"), answered["item_key"]),
        )
        now = self.base.isoformat(timespec="seconds")
        self.conn.execute(
            "INSERT INTO activity_confirmation_calibrations("
            "calibration_key,event_type,app_family,region_kind,actionability,positive_count,negative_count,"
            "misrecognized_count,reason_counts,last_answer,last_reason,revoked_at,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("cal_keep", "completion", "TickTick", "task_list", "context_only", 2, 3, 1, '{"old_content":3}', "not_occurred", "old_content", None, now, now),
        )
        # Simulate a database created before the current policy was deployed.
        self.conn.execute(
            "DELETE FROM settings WHERE key=?",
            (CONFIRMATION_POLICY_SETTING_KEY,),
        )
        self.conn.commit()
        calibration_before = tuple(self.conn.execute("SELECT * FROM activity_confirmation_calibrations WHERE calibration_key='cal_keep'").fetchone())

        result = revalidate_unresolved_confirmation_policy(self.conn)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["examined"], 2)
        self.assertEqual(result["rejected"], 1)
        self.assertEqual(result["retained_deferred"], 1)
        noisy_row = self.conn.execute("SELECT state,answer,reason_codes FROM activity_confirmation_items WHERE item_key=?", (noisy["item_key"],)).fetchone()
        self.assertEqual((noisy_row["state"], noisy_row["answer"]), ("filtered", None))
        self.assertIn("policy_v7_non_actionable_region", json.loads(noisy_row["reason_codes"]))
        self.assertEqual(self.conn.execute("SELECT state FROM activity_confirmation_items WHERE item_key=?", (deferred["item_key"],)).fetchone()[0], "deferred")
        answered_row = self.conn.execute("SELECT state,answer FROM activity_confirmation_items WHERE item_key=?", (answered["item_key"],)).fetchone()
        self.assertEqual(tuple(answered_row), ("confirmed", "occurred"))
        self.assertEqual(tuple(self.conn.execute("SELECT * FROM activity_confirmation_calibrations WHERE calibration_key='cal_keep'").fetchone()), calibration_before)
        self.assertEqual(self.conn.execute("SELECT value FROM settings WHERE key=?", (CONFIRMATION_POLICY_SETTING_KEY,)).fetchone()[0], str(CONFIRMATION_POLICY_VERSION))
        self.assertEqual(revalidate_unresolved_confirmation_policy_v3(self.conn)["status"], "current")

    def test_policy_v5_revalidates_policy_v3_conditional_tail_without_user_side_effects(self) -> None:
        candidate = upsert_activity_confirmation_candidate(
            self.conn,
            self._activity(
                "现在已优化为：Desktop的异步子任务完成后显示待处理卡片",
                app="ChatGPT",
            ),
        )
        self.conn.execute(
            "UPDATE activity_confirmation_items "
            "SET state='pending',answer=NULL,answer_reason=NULL,user_note='',reason_codes=? "
            "WHERE item_key=?",
            (json.dumps(["possible_completion_occurrence", "policy_v3_evaluated"]), candidate["item_key"]),
        )
        self.conn.execute(
            "UPDATE settings SET value='3' WHERE key=?",
            (CONFIRMATION_POLICY_SETTING_KEY,),
        )
        self.conn.commit()

        result = revalidate_unresolved_confirmation_policy(self.conn)
        self.assertEqual((result["status"], result["policy_version"], result["rejected"]), ("applied", 7, 1))
        row = self.conn.execute(
            "SELECT state,answer,user_note,reason_codes FROM activity_confirmation_items WHERE item_key=?",
            (candidate["item_key"],),
        ).fetchone()
        reason_codes = json.loads(row["reason_codes"])
        self.assertEqual((row["state"], row["answer"], row["user_note"]), ("filtered", None, ""))
        self.assertIn("policy_v3_evaluated", reason_codes)
        self.assertIn("policy_v7_evaluated", reason_codes)
        self.assertIn("policy_v7_conditional_completion", reason_codes)
        self.assertEqual(revalidate_unresolved_confirmation_policy(self.conn)["status"], "current")

    def test_current_marker_rechecks_unevaluated_tail_writes(self) -> None:
        noisy = upsert_activity_confirmation_candidate(
            self.conn,
            self._activity(
                "已完成 & 已放弃 30",
                app="TickTick",
                region_kind="task_list",
                actionability="context_only",
            ),
        )
        self.assertEqual(revalidate_unresolved_confirmation_policy(self.conn)["status"], "current")

        # Simulate an older writer inserting after the global current-policy marker was set.
        self.conn.execute(
            "UPDATE activity_confirmation_items "
            "SET state='pending',answer=NULL,reason_codes=? WHERE item_key=?",
            (json.dumps(["possible_completion_occurrence"]), noisy["item_key"]),
        )
        self.conn.commit()

        result = revalidate_unresolved_confirmation_policy(self.conn)
        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["examined"], 1)
        row = self.conn.execute(
            "SELECT state,answer,reason_codes FROM activity_confirmation_items WHERE item_key=?",
            (noisy["item_key"],),
        ).fetchone()
        self.assertEqual((row["state"], row["answer"]), ("filtered", None))
        self.assertIn("policy_v7_evaluated", json.loads(row["reason_codes"]))
        self.assertEqual(revalidate_unresolved_confirmation_policy(self.conn)["status"], "current")

    def test_migration_serializes_with_a_concurrent_user_answer(self) -> None:
        candidate = upsert_activity_confirmation_candidate(
            self.conn,
            self._activity("今天我已完成客户市场报告"),
        )
        calibration_key = self.conn.execute(
            "SELECT calibration_key FROM activity_confirmation_items WHERE item_key=?",
            (candidate["item_key"],),
        ).fetchone()[0]
        self.conn.execute(
            "DELETE FROM settings WHERE key=?",
            (CONFIRMATION_POLICY_SETTING_KEY,),
        )
        self.conn.execute(
            "UPDATE activity_confirmation_items SET reason_codes=? WHERE item_key=?",
            (json.dumps(["possible_completion_occurrence"]), candidate["item_key"]),
        )
        self.conn.commit()

        answered_at = self.base.isoformat(timespec="seconds")
        answer_conn = connect(self.db_path)
        answer_conn.execute("BEGIN IMMEDIATE")
        answer_conn.execute(
            "UPDATE activity_confirmation_items "
            "SET state='confirmed',answer='occurred',answered_at=? WHERE item_key=?",
            (answered_at, candidate["item_key"]),
        )
        answer_conn.execute(
            "INSERT INTO activity_confirmation_calibrations("
            "calibration_key,event_type,app_family,region_kind,actionability,positive_count,negative_count,"
            "misrecognized_count,reason_counts,last_answer,last_reason,revoked_at,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                calibration_key,
                "completion",
                "Obsidian",
                "main_content",
                "actionable",
                1,
                0,
                0,
                "{}",
                "occurred",
                "",
                None,
                answered_at,
                answered_at,
            ),
        )

        attempted = threading.Event()
        finished = threading.Event()
        errors: list[BaseException] = []

        def migrate() -> None:
            migration_conn = connect(self.db_path, busy_timeout_seconds=5)
            try:
                attempted.set()
                revalidate_unresolved_confirmation_policy(migration_conn)
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
            finally:
                migration_conn.close()
                finished.set()

        worker = threading.Thread(target=migrate, daemon=True)
        worker.start()
        self.assertTrue(attempted.wait(timeout=1))
        self.assertFalse(finished.wait(timeout=0.05))
        answer_conn.commit()
        answer_conn.close()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])

        row = self.conn.execute(
            "SELECT state,answer,answered_at FROM activity_confirmation_items WHERE item_key=?",
            (candidate["item_key"],),
        ).fetchone()
        self.assertEqual(tuple(row), ("confirmed", "occurred", answered_at))
        calibration = self.conn.execute(
            "SELECT positive_count,last_answer FROM activity_confirmation_calibrations "
            "WHERE calibration_key=?",
            (calibration_key,),
        ).fetchone()
        self.assertEqual(tuple(calibration), (1, "occurred"))


if __name__ == "__main__":
    unittest.main()
