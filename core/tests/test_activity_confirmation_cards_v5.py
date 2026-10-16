from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from activity_mvp.activity_confirmations import (
    CONFIRMATION_POLICY_QUESTION_SAFETY_REASON,
    CONFIRMATION_POLICY_SETTING_KEY,
    INDEPENDENT_FILE_EVIDENCE_SIGNAL,
    _business_episode_candidate,
    _rows_can_group,
    _subject_for_event,
    _upsert_confirmation_candidate,
    activity_confirmation_revision,
    answer_activity_confirmation,
    answer_activity_confirmation_group,
    list_activity_confirmation_cards,
    list_activity_confirmations,
    revalidate_unresolved_confirmation_policy,
)
from activity_mvp.storage import ensure_ready
from activity_mvp.user_decisions import contains_absolute_path, safe_decision_text


class ActivityConfirmationCardsV5Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "activity.sqlite"
        self.conn = ensure_ready(self.db_path)
        self.base = datetime.now().astimezone().replace(
            hour=9,
            minute=0,
            second=0,
            microsecond=0,
        )

    def tearDown(self) -> None:
        self.conn.close()
        self.tempdir.cleanup()

    def _insert(
        self,
        item_key: str,
        *,
        event_type: str = "publish",
        object_label: str = "客户公告",
        evidence: str | None = None,
        at: datetime | None = None,
        # General grouping/presentation fixtures use a normal web app. AI
        # execution fixtures explicitly select an AI app and evidence below.
        app: str = "Safari",
        group_key: str | None = None,
        candidate_origin: str = "explicit_claim",
        episode_id: str = "",
        question_kind: str = "occurrence",
        reason_codes: list[str] | None = None,
        safe_signal_types: list[str] | None = None,
    ) -> None:
        observed = at or self.base
        label = {
            "publish": "发布",
            "completion": "完成事项",
            "meeting": "会议",
            "send": "发送",
            "submission": "提交",
            "call": "通话",
            "business_context": "业务工作片段",
        }.get(event_type, "活动")
        subject = {
            "event_label": label,
            "company": "",
            "round": "",
            "object": object_label,
            "label": f"{object_label}{label}" if object_label else label,
            "company_identified": False,
            "round_identified": False,
            "object_identified": bool(object_label),
        }
        _upsert_confirmation_candidate(
            self.conn,
            {
                "item_key": item_key,
                "event_type": event_type,
                "safe_observation": evidence or f"{object_label}已{label}",
                "decision_question": "legacy question",
                "why_uncertain": "需要独立证据",
                "app_family": app,
                "observed_start": observed.isoformat(timespec="seconds"),
                "observed_end": (observed + timedelta(minutes=1)).isoformat(timespec="seconds"),
                "confidence": 0.55,
                "reason_codes": reason_codes
                or ["possible_occurrence", "policy_v5_evaluated"],
                "region_kind": "main_content",
                "actionability": "actionable",
                "source_activity_ids": [],
                "deletion_group_ids": [],
                "fingerprint": f"fp_{item_key}",
                "calibration_key": f"cal_{item_key}",
                "group_key": group_key or f"grp_{item_key}",
                "group_signature": f"sig_{item_key}",
                "subject": subject,
                "evidence": evidence or f"{object_label}已{label}",
                "cleaned_content": evidence or f"{object_label}已{label}",
                "question_kind": question_kind,
                "candidate_origin": candidate_origin,
                "priority": "high",
                "episode_id": episode_id,
                "safe_signal_types": safe_signal_types
                or ["post_firewall_activity"],
                "suggested_workstream": object_label,
                "suggested_outcome": "completed",
                "notification_eligible": True,
                "state": "pending",
            },
        )

    def test_unnamed_count_summaries_never_require_a_binary_answer(self) -> None:
        summaries = (
            "8个内容项和132条文件变动记录都完整处理",
            "8 个内容项和 132 条文件变动记录都完整处理",
            "本轮共处理十二个条目和九条记录",
            "所有文件都已完整处理",
            "8 content items and 132 file changes fully processed",
        )
        for index, subject in enumerate(summaries):
            self._insert(
                f"aggregate_{index}", event_type="submission",
                object_label=subject, evidence=f"已提交{subject}",
                app="Safari", at=self.base + timedelta(minutes=index * 20),
                reason_codes=["submit_control_geometry"],
                safe_signal_types=["independent_file_evidence"],
            )
        self.conn.commit()
        payload = list_activity_confirmation_cards(self.conn, range_name="today")
        self.assertEqual(payload["pending_confirmation_count"], 0)
        self.assertEqual(len(payload["cards"]), len(summaries))
        for card in payload["cards"]:
            self.assertEqual(card["section"], "information_insufficient")
            self.assertEqual(card["priority"], "low")
            self.assertEqual(card["display_question"], "")
            self.assertEqual(card["answer_options"], [])
            self.assertIn("只有处理数量", card["display_title"])
        rows = self.conn.execute(
            "SELECT state,answer,decision_question,notification_eligible "
            "FROM activity_confirmation_items"
        ).fetchall()
        self.assertTrue(all(tuple(row) == ("pending", None, "", 0) for row in rows))

    def test_named_batch_document_is_not_mistaken_for_unnamed_statistics(self) -> None:
        for index, app in enumerate(("Safari", "ChatGPT")):
            self._insert(
                f"named_batch_{index}", event_type="completion",
                object_label=f"Atlas 项目{index}的12项检查清单",
                evidence=f"Atlas 项目{index}的12项检查清单已完成",
                app=app, safe_signal_types=["independent_file_evidence"],
            )
        self.conn.commit()
        payload = list_activity_confirmation_cards(self.conn, range_name="today")
        self.assertEqual(payload["pending_confirmation_count"], 2)
        self.assertTrue(all(card["answer_options"] for card in payload["cards"]))

    def test_old_ai_submit_card_is_readonly_downgraded_and_stale_answers_blocked(self) -> None:
        self._insert(
            "old_chat_submit", event_type="submission", object_label="Atlas季度申请",
            evidence="Atlas季度申请已提交", app="ChatGPT",
        )
        old_codes = json.dumps([
            "possible_submission_occurrence", "policy_v7_evaluated",
            "policy_v7_question_safety_r2", "submit_control_geometry",
        ])
        self.conn.execute(
            "UPDATE activity_confirmation_items SET reason_codes=?,priority='high',"
            "decision_question='legacy question',notification_eligible=1 "
            "WHERE item_key='old_chat_submit'", (old_codes,),
        )
        self.conn.commit()
        before = self.conn.total_changes
        payload = list_activity_confirmation_cards(self.conn, range_name="today")
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual(payload["pending_confirmation_count"], 0)
        self.assertEqual(activity_confirmation_revision(self.conn)["pending_question_count"], 0)
        legacy = list_activity_confirmations(self.conn, range_name="today")
        for item in [*legacy["items"], *legacy["question_groups"]]:
            self.assertEqual(item["decision_question"], "")
            self.assertEqual(item["answer_options"], [])
            self.assertFalse(item["notification_eligible"])
        self.assertEqual(self.conn.total_changes, before)
        card = payload["cards"][0]
        self.assertEqual(card["priority"], "low")
        self.assertEqual(card["answer_options"], [])
        for answer in ("occurred", "not_occurred"):
            result = answer_activity_confirmation(
                self.conn, item_key="old_chat_submit", answer=answer, reason="correct",
            )
            self.assertEqual((result["status"], result["error"]), ("blocked", "information_insufficient"))
        group_result = answer_activity_confirmation_group(
            self.conn, group_key=card["group_key"], member_revision=card["member_revision"],
            answer="occurred", reason="correct", workstream="Atlas申请", outcome="completed",
        )
        self.assertEqual((group_result["status"], group_result["error"]), ("stale", "group_members_changed"))
        card = list_activity_confirmation_cards(self.conn, range_name="today")["cards"][0]
        group_result = answer_activity_confirmation_group(
            self.conn, group_key=card["group_key"], member_revision=card["member_revision"],
            answer="occurred", reason="correct", workstream="Atlas申请", outcome="completed",
        )
        self.assertEqual((group_result["status"], group_result["error"]), ("blocked", "information_insufficient"))
        row = self.conn.execute(
            "SELECT state,answer,notification_eligible,priority FROM activity_confirmation_items "
            "WHERE item_key='old_chat_submit'"
        ).fetchone()
        self.assertEqual(tuple(row), ("pending", None, 1, "high"))
        identity_sql = (
            "SELECT subject,group_key,group_signature,source_activity_ids,evidence,"
            "cleaned_content,clarification_note FROM activity_confirmation_items "
            "WHERE item_key='old_chat_submit'"
        )
        identity_before = tuple(self.conn.execute(identity_sql).fetchone())
        preview = revalidate_unresolved_confirmation_policy(self.conn, dry_run=True)
        self.assertEqual(preview["status"], "preview")
        self.assertEqual(self.conn.execute(
            "SELECT notification_eligible FROM activity_confirmation_items "
            "WHERE item_key='old_chat_submit'"
        ).fetchone()[0], 1)
        applied = revalidate_unresolved_confirmation_policy(self.conn)
        self.assertEqual(applied["rejected"], 0)
        self.assertEqual(applied["groups_split"], 0)
        self.assertEqual(applied["rows_regrouped"], 0)
        self.assertEqual(tuple(self.conn.execute(identity_sql).fetchone()), identity_before)
        row = self.conn.execute(
            "SELECT state,answer,notification_eligible,priority FROM activity_confirmation_items "
            "WHERE item_key='old_chat_submit'"
        ).fetchone()
        self.assertEqual(tuple(row), ("pending", None, 0, "low"))

    def test_cards_are_unresolved_only_paginated_and_exclude_legacy_heavy_fields(self) -> None:
        for index in range(14):
            self._insert(
                f"item_{index:02d}",
                event_type="completion",
                object_label=f"客户报告{index:02d}",
                at=self.base + timedelta(minutes=index),
            )
        self.conn.commit()

        first = list_activity_confirmation_cards(self.conn, range_name="today")
        self.assertEqual(first["contract_version"], 5)
        self.assertEqual(len(first["cards"]), 12)
        self.assertTrue(first["has_more"])
        self.assertEqual(first["pending_question_count"], 14)
        self.assertNotIn("candidate_funnel", first)
        self.assertNotIn("question_groups", first)
        self.assertNotIn("items", first)
        self.assertTrue(all("cleaned_contents" not in card for card in first["cards"]))

        second = list_activity_confirmation_cards(
            self.conn,
            range_name="today",
            cursor=first["next_cursor"],
        )
        self.assertEqual(len(second["cards"]), 2)
        self.assertFalse(second["has_more"])
        self.assertFalse(second["cursor_reset"])

    def test_solo_ocr_variants_merge_and_render_as_normal_chinese(self) -> None:
        self._insert(
            "solo_1",
            object_label=(
                "zc: 20 23 日 检查天命鉴全链路 2 查看 Solo 同意项设置 2 "
                "展开 Solo 同意项设置 2 打开 Solo 完成页设置 二进"
            ),
            evidence="Solo Staging 表单发布成功",
            at=self.base,
            candidate_origin="business_episode",
            episode_id="episode_solo",
        )
        self._insert(
            "solo_2",
            object_label="Solo Staging 表单",
            evidence="Solo Staging 表单已发布",
            at=self.base + timedelta(minutes=6),
            candidate_origin="business_episode",
            episode_id="episode_solo",
        )
        self.conn.commit()

        payload = list_activity_confirmation_cards(self.conn, range_name="today")

        self.assertEqual(payload["pending_question_count"], 1)
        card = payload["cards"][0]
        self.assertEqual(card["source_count"], 2)
        self.assertEqual(card["canonical_subject"]["object"], "Solo Staging 表单")
        self.assertEqual(card["display_title"], "发布 Solo Staging 表单")
        self.assertIn("你是否发布了 Solo Staging 表单", card["display_question"])
        self.assertNotIn("zc:", json.dumps(card, ensure_ascii=False))
        self.assertNotIn("真的已经发生了吗", json.dumps(card, ensure_ascii=False))

        # The read surface merges semantically even before the durable v6
        # regroup, then the explicit apply makes the mapping persistent.
        stored_before = self.conn.execute(
            "SELECT COUNT(DISTINCT group_key) FROM activity_confirmation_items "
            "WHERE item_key IN ('solo_1','solo_2')"
        ).fetchone()[0]
        self.assertEqual(stored_before, 2)
        self.conn.execute(
            "UPDATE settings SET value='5' WHERE key=?",
            (CONFIRMATION_POLICY_SETTING_KEY,),
        )
        self.conn.commit()
        revalidate_unresolved_confirmation_policy(self.conn)
        stored_after = self.conn.execute(
            "SELECT COUNT(DISTINCT group_key) FROM activity_confirmation_items "
            "WHERE item_key IN ('solo_1','solo_2')"
        ).fetchone()[0]
        self.assertEqual(stored_after, 1)

    def test_solo_menu_fragment_is_information_insufficient_by_itself(self) -> None:
        self._insert(
            "solo_menu",
            object_label="Solo 同意项设置 2 展开 Solo 完成页设置",
            evidence="Solo 同意项设置发布成功",
        )
        self.conn.commit()
        card = list_activity_confirmation_cards(self.conn, range_name="today")["cards"][0]
        self.assertEqual(card["section"], "information_insufficient")
        self.assertEqual(card["display_question"], "")
        self.assertNotIn("同意项设置", card["display_title"])

    def test_missing_object_is_information_insufficient_without_binary_question(self) -> None:
        self._insert("missing", object_label="", evidence="发布成功")
        self.conn.commit()
        card = list_activity_confirmation_cards(self.conn, range_name="today")["cards"][0]
        self.assertEqual(card["section"], "information_insufficient")
        self.assertEqual(card["subject_status"], "information_insufficient")
        self.assertEqual(card["display_question"], "")
        self.assertEqual(card["answer_options"], [])
        self.assertIn("发布对象未识别", card["display_title"])

    def test_business_context_never_becomes_a_binary_occurrence_question(self) -> None:
        self._insert(
            "context",
            event_type="business_context",
            object_label="简历优化与",
            evidence="简历优化与海外业务",
            question_kind="business_context",
            candidate_origin="business_episode",
        )
        self.conn.commit()
        card = list_activity_confirmation_cards(self.conn, range_name="today")["cards"][0]
        self.assertEqual(card["section"], "information_insufficient")
        self.assertEqual(card["display_question"], "")
        self.assertEqual(card["display_title"], "信息不足：业务片段归属未确定")

    def test_counts_and_pagination_prioritize_answerable_cards(self) -> None:
        self._insert("answerable", object_label="客户公告", at=self.base)
        self._insert("missing_later", object_label="", at=self.base + timedelta(hours=2))
        self._insert(
            "context_later",
            event_type="business_context",
            object_label="简历优化与",
            at=self.base + timedelta(hours=3),
            question_kind="business_context",
            candidate_origin="business_episode",
        )
        self.conn.commit()

        payload = list_activity_confirmation_cards(
            self.conn,
            range_name="today",
            limit=1,
        )
        self.assertEqual(payload["total_card_count"], 3)
        self.assertEqual(payload["pending_confirmation_count"], 1)
        self.assertEqual(payload["pending_question_count"], 1)
        self.assertEqual(payload["information_insufficient_count"], 2)
        self.assertEqual(payload["task_status_count"], 0)
        self.assertEqual(payload["cards"][0]["section"], "pending_confirmation")
        self.assertTrue(payload["has_more"])

        revision = activity_confirmation_revision(self.conn, range_name="today")
        self.assertEqual(revision["total_card_count"], 3)
        self.assertEqual(revision["pending_confirmation_count"], 1)
        self.assertEqual(revision["information_insufficient_count"], 2)

    def test_distinct_publish_objects_do_not_merge(self) -> None:
        left = {
            "event_type": "publish",
            "observed_start": self.base.isoformat(timespec="seconds"),
            "observed_end": (self.base + timedelta(minutes=1)).isoformat(timespec="seconds"),
            "subject": {"event_label": "发布", "object": "客户公告", "object_identified": True},
            "candidate_origin": "business_episode",
            "episode_id": "episode_left",
        }
        right = {
            **left,
            "observed_start": (self.base + timedelta(minutes=5)).isoformat(timespec="seconds"),
            "observed_end": (self.base + timedelta(minutes=6)).isoformat(timespec="seconds"),
            "subject": {"event_label": "发布", "object": "产品手册", "object_identified": True},
            "episode_id": "episode_right",
        }
        self.assertFalse(_rows_can_group(left, right))

    def test_same_solo_environment_different_publish_targets_do_not_merge(self) -> None:
        self._insert(
            "solo_form",
            object_label="Solo Staging 表单",
            evidence="Solo Staging 表单已发布",
            at=self.base,
        )
        self._insert(
            "solo_docs",
            object_label="Solo Staging 文档",
            evidence="Solo Staging 文档已发布",
            at=self.base + timedelta(minutes=4),
        )
        self.conn.commit()

        payload = list_activity_confirmation_cards(self.conn, range_name="today")
        self.assertEqual(payload["pending_confirmation_count"], 2)
        self.assertEqual(
            {card["canonical_subject"]["object"] for card in payload["cards"]},
            {"Solo Staging 表单", "Solo Staging 文档"},
        )

    def test_named_publish_products_preserve_unicode_identity_and_group_writer_scope(
        self,
    ) -> None:
        objects = (
            ("tianmingjian", "天命鉴 Staging 网站"),
            ("ziwei", "紫微盘 Staging 网站"),
            ("solo", "Solo Staging 网站"),
            ("mixed", "TianMing 天命鉴 Staging 网站"),
        )
        for index, (item_key, object_label) in enumerate(objects):
            self._insert(
                item_key,
                object_label=object_label,
                evidence=f"{object_label}已发布",
                at=self.base + timedelta(minutes=index * 3),
            )
        self.conn.commit()

        payload = list_activity_confirmation_cards(self.conn, range_name="today")
        cards_by_object = {
            card["canonical_subject"]["object"]: card for card in payload["cards"]
        }
        self.assertEqual(set(cards_by_object), {value for _, value in objects})
        self.assertEqual(payload["pending_confirmation_count"], 4)
        self.assertNotIn("Staging Staging", json.dumps(payload, ensure_ascii=False))

        target = cards_by_object["天命鉴 Staging 网站"]
        recorded = answer_activity_confirmation_group(
            self.conn,
            group_key=target["group_key"],
            member_revision=target["member_revision"],
            answer="occurred",
            reason="correct",
            workstream="网站发布",
            outcome="completed",
        )
        if recorded["status"] == "stale":
            target = next(
                card
                for card in list_activity_confirmation_cards(
                    self.conn, range_name="today"
                )["cards"]
                if card["canonical_subject"]["object"] == "天命鉴 Staging 网站"
            )
            recorded = answer_activity_confirmation_group(
                self.conn,
                group_key=target["group_key"],
                member_revision=target["member_revision"],
                answer="occurred",
                reason="correct",
                workstream="网站发布",
                outcome="completed",
            )
        self.assertEqual(recorded["status"], "recorded")
        states = {
            row["item_key"]: row["state"]
            for row in self.conn.execute(
                "SELECT item_key,state FROM activity_confirmation_items "
                "WHERE item_key IN ('tianmingjian','ziwei')"
            ).fetchall()
        }
        self.assertEqual(
            states,
            {"tianmingjian": "confirmed", "ziwei": "pending"},
        )

    def test_legacy_group_key_collision_keeps_every_card_fail_closed(self) -> None:
        self._insert(
            "legacy_notice",
            object_label="客户公告",
            evidence="客户公告已发布",
            at=self.base,
            group_key="legacy_same",
        )
        self._insert(
            "legacy_manual",
            object_label="产品手册",
            evidence="产品手册已发布",
            at=self.base + timedelta(minutes=4),
            group_key="legacy_same",
        )
        self.conn.commit()

        payload = list_activity_confirmation_cards(self.conn, range_name="today")

        self.assertEqual(payload["total_card_count"], 2)
        self.assertEqual(payload["pending_confirmation_count"], 0)
        self.assertEqual(payload["information_insufficient_count"], 2)
        self.assertEqual(len({card["group_key"] for card in payload["cards"]}), 2)
        self.assertTrue(
            all(card["group_key"].startswith("collision_") for card in payload["cards"])
        )
        self.assertTrue(
            all(card["section"] == "information_insufficient" for card in payload["cards"])
        )
        self.assertTrue(all(card["answer_options"] == [] for card in payload["cards"]))
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(DISTINCT group_key) FROM activity_confirmation_items "
                "WHERE item_key IN ('legacy_notice','legacy_manual')"
            ).fetchone()[0],
            1,
        )

    def test_conflicting_solo_subject_and_evidence_are_information_insufficient(self) -> None:
        self._insert(
            "solo_conflict",
            object_label="Solo Staging 文档",
            evidence="Solo Staging 表单已发布",
            at=self.base,
        )
        self.conn.commit()

        card = list_activity_confirmation_cards(
            self.conn,
            range_name="today",
        )["cards"][0]
        self.assertEqual(card["section"], "information_insufficient")
        self.assertEqual(card["display_question"], "")

    def test_safe_solo_subject_ignores_unsafe_menu_evidence_before_and_after_revalidate(
        self,
    ) -> None:
        menu_evidence = (
            "zc: 20 23 日 检查天命鉴全链路 2 查看 Solo 同意项设置 2 "
            "展开 Solo 完成页设置 Solo Staging 表单已发布"
        )
        tally_evidence = (
            "Tally 完成修改 2 查看发布设置 2 展开完成页 "
            "Tally Staging 表单已发布"
        )
        self._insert(
            "safe_solo_zc",
            object_label="Solo Staging 表单",
            evidence=menu_evidence,
            at=self.base,
        )
        self._insert(
            "safe_solo_tally",
            object_label="Solo Staging 表单",
            evidence=tally_evidence,
            at=self.base + timedelta(minutes=4),
        )
        self._insert(
            "unsafe_menu_only",
            object_label="",
            evidence=tally_evidence,
            at=self.base + timedelta(minutes=8),
        )
        self.conn.commit()

        before = list_activity_confirmation_cards(self.conn, range_name="today")
        self.assertEqual(before["pending_confirmation_count"], 1)
        self.assertEqual(before["information_insufficient_count"], 1)

        self.conn.execute(
            "UPDATE settings SET value='5' WHERE key=?",
            (CONFIRMATION_POLICY_SETTING_KEY,),
        )
        self.conn.commit()
        revalidate_unresolved_confirmation_policy(self.conn)
        after = list_activity_confirmation_cards(self.conn, range_name="today")
        pending = [
            card for card in after["cards"]
            if card["section"] == "pending_confirmation"
        ]

        self.assertEqual(len(pending), 1)
        self.assertEqual(
            pending[0]["canonical_subject"]["object"],
            "Solo Staging 表单",
        )
        self.assertEqual(pending[0]["display_title"], "发布 Solo Staging 表单")
        self.assertIn(
            "你是否发布了 Solo Staging 表单？",
            pending[0]["display_question"],
        )
        rendered = json.dumps(pending[0], ensure_ascii=False)
        self.assertNotIn("zc:", rendered)
        self.assertNotIn("Tally", rendered)
        self.assertNotIn("查看", rendered)
        self.assertNotIn("展开", rendered)

    def test_same_publish_target_in_different_episodes_does_not_merge(self) -> None:
        self._insert(
            "episode_one",
            object_label="Solo Staging 表单",
            evidence="Solo Staging 表单已发布",
            at=self.base,
            candidate_origin="business_episode",
            episode_id="episode_one",
        )
        self._insert(
            "episode_two",
            object_label="Solo Staging 表单",
            evidence="Solo Staging 表单已发布",
            at=self.base + timedelta(minutes=4),
            candidate_origin="business_episode",
            episode_id="episode_two",
        )
        self.conn.commit()

        payload = list_activity_confirmation_cards(self.conn, range_name="today")
        self.assertEqual(payload["pending_confirmation_count"], 2)

    def test_repeated_publish_events_have_distinct_semantic_keys(self) -> None:
        self._insert(
            "morning_publish",
            object_label="客户公告",
            evidence="客户公告已发布",
            at=self.base,
        )
        self._insert(
            "afternoon_publish",
            object_label="客户公告",
            evidence="客户公告已发布",
            at=self.base + timedelta(hours=2),
        )
        self.conn.commit()

        cards = list_activity_confirmation_cards(
            self.conn,
            range_name="today",
        )["cards"]
        self.assertEqual(len(cards), 2)
        self.assertEqual(len({card["semantic_key"] for card in cards}), 2)

    def test_short_ocr_menu_steps_are_information_insufficient(self) -> None:
        for index, object_label in enumerate(
            (
                "zc: 20 23 日 检查天命鉴全链路 2 查看",
                "发布 打开 设置",
            )
        ):
            self._insert(
                f"menu_{index}",
                object_label=object_label,
                evidence=object_label,
                at=self.base + timedelta(minutes=index * 20),
            )
        self.conn.commit()

        payload = list_activity_confirmation_cards(self.conn, range_name="today")
        self.assertEqual(payload["pending_confirmation_count"], 0)
        self.assertEqual(payload["information_insufficient_count"], 2)
        self.assertTrue(
            all(card["display_question"] == "" for card in payload["cards"])
        )
        self.assertNotIn("zc:", json.dumps(payload["cards"], ensure_ascii=False))

    def test_outline_fragments_never_form_binary_meeting_questions(self) -> None:
        fragments = (
            "、纪要和下一步销售管线 1）渠道伙伴画像",
            "、纪要和下一步销售管线 交付物 1）渠道伙伴画像",
        )
        for index, fragment in enumerate(fragments):
            self._insert(
                f"outline_{index}",
                event_type="meeting",
                object_label=fragment,
                evidence=f"参加了{fragment}会议",
                at=self.base + timedelta(minutes=index * 20),
            )
        self.conn.commit()

        payload = list_activity_confirmation_cards(self.conn, range_name="today")

        self.assertEqual(payload["pending_confirmation_count"], 0)
        self.assertEqual(payload["information_insufficient_count"], 2)
        self.assertTrue(all(card["display_question"] == "" for card in payload["cards"]))
        rendered = json.dumps(payload["cards"], ensure_ascii=False)
        self.assertNotIn("渠道伙伴画像？", rendered)
        self.assertNotIn("销售管线 交付物", rendered)

    def test_punctuation_connector_and_numbered_prefixes_are_information_insufficient(self) -> None:
        for index, object_label in enumerate(
            (
                "、客户调研报告",
                "以及客户调研报告",
                "1）客户调研报告",
            )
        ):
            self._insert(
                f"fragment_prefix_{index}",
                event_type="completion",
                object_label=object_label,
                evidence=f"{object_label}已完成",
                at=self.base + timedelta(minutes=index * 20),
            )
        self.conn.commit()

        payload = list_activity_confirmation_cards(self.conn, range_name="today")

        self.assertEqual(payload["pending_confirmation_count"], 0)
        self.assertEqual(payload["information_insufficient_count"], 3)
        self.assertTrue(all(card["display_question"] == "" for card in payload["cards"]))

    def test_send_requires_a_concrete_object_but_accepts_market_report(self) -> None:
        self._insert(
            "market_category",
            event_type="send",
            object_label="国家市场",
            evidence="国家市场已发送",
            at=self.base,
        )
        self._insert(
            "market_report",
            event_type="send",
            object_label="国家市场调研报告",
            evidence="国家市场调研报告已发送",
            at=self.base + timedelta(minutes=40),
        )
        self.conn.commit()

        cards = list_activity_confirmation_cards(self.conn, range_name="today")["cards"]
        category = next(card for card in cards if card["source_count"] == 1 and card["section"] == "information_insufficient")
        report = next(card for card in cards if card["section"] == "pending_confirmation")

        self.assertEqual(category["display_question"], "")
        self.assertNotIn("是否发送了国家市场", json.dumps(category, ensure_ascii=False))
        self.assertEqual(report["canonical_subject"]["object"], "国家市场调研报告")
        self.assertEqual(report["display_title"], "发送国家市场调研报告")
        self.assertIn("你是否发送了国家市场调研报告", report["display_question"])

    def test_candidate_creation_uses_the_same_deterministic_object_organizer(self) -> None:
        category = _subject_for_event(
            "send",
            "发送",
            "国家市场已发送",
            "国家市场已发送",
        )
        report = _subject_for_event(
            "send",
            "发送",
            "国家市场调研报告已发送",
            "国家市场调研报告已发送",
        )
        meeting = _subject_for_event(
            "meeting",
            "会议",
            "今天参加了客户项目会议",
            "今天参加了客户项目会议",
        )
        outline = _subject_for_event(
            "meeting",
            "会议",
            "参加了、纪要和下一步销售管线 1）渠道伙伴画像会议",
            "参加了、纪要和下一步销售管线 1）渠道伙伴画像会议",
        )

        self.assertFalse(category["object_identified"])
        self.assertEqual(category["object"], "")
        self.assertTrue(report["object_identified"])
        self.assertEqual(report["object"], "国家市场调研报告")
        self.assertTrue(meeting["object_identified"])
        self.assertEqual(meeting["object"], "客户项目会议")
        self.assertFalse(outline["object_identified"])
        self.assertEqual(outline["object"], "")

    def test_generated_report_claim_without_independent_receipt_is_information_insufficient(
        self,
    ) -> None:
        noisy = "已生成一份18页、可直接提交的Word 版报告"
        self._insert(
            "generated_report",
            event_type="completion",
            object_label=noisy,
            evidence=f"{noisy}已完成",
            app="ChatGPT",
            at=self.base,
        )
        self.conn.commit()

        card = list_activity_confirmation_cards(self.conn, range_name="today")["cards"][0]

        self.assertEqual(card["section"], "information_insufficient")
        self.assertEqual(card["canonical_subject"]["object"], "18页 Word 版报告")
        self.assertEqual(card["display_title"], "信息不足：报告完成证据不足")
        self.assertEqual(card["display_question"], "")
        self.assertEqual(card["answer_options"], [])
        self.assertNotIn("已生成一份", json.dumps(card, ensure_ascii=False))
        self.assertNotIn("可直接提交的", json.dumps(card, ensure_ascii=False))

    def test_live_malformed_semantics_leave_only_typed_solo_publish_answerable(
        self,
    ) -> None:
        cases = (
            {
                "item_key": "generated_report",
                "event_type": "completion",
                "object_label": "25页 Word 版报告",
                "evidence": "已生成一份25页、可直接交付的 Word 版报告",
                "app": "Dia",
            },
            {
                "item_key": "truncated_report",
                "event_type": "completion",
                "object_label": "25页、可直",
                "evidence": "25页、可直已完成",
                "app": "Dia",
            },
            {
                "item_key": "internal_status",
                "event_type": "send",
                "object_label": "在 entry 的 emitted_report_hashes 中检测到同日正文哈希",
                "evidence": "在 entry 的 emitted_report_hashes 中检测到同日正文哈希已发送",
                "app": "ChatGPT",
            },
            {
                "item_key": "generic_trade",
                "event_type": "send",
                "object_label": "外贸",
                "evidence": "外贸已发送",
                "app": "Dia",
            },
            {
                "item_key": "recruitment_chat",
                "event_type": "send",
                "object_label": "林女士 星河高级招聘 [送达] 收到",
                "evidence": "林女士 星河高级招聘 [送达] 收到 已发送",
                "app": "Dia",
            },
            {
                "item_key": "negated_fragment",
                "event_type": "publish",
                "object_label": "但还没有",
                "evidence": "但还没有发布成功",
                "app": "ChatGPT",
            },
            {
                "item_key": "pronoun_fragment",
                "event_type": "send",
                "object_label": "有了",
                "evidence": "有了已发送",
                "app": "Dia",
            },
            {
                "item_key": "solo_form",
                "event_type": "publish",
                "object_label": "Solo Staging 表单",
                "evidence": "Solo Staging 表单已发布",
                "app": "ChatGPT",
                "safe_signal_types": ["independent_platform_receipt"],
            },
            {
                "item_key": "duo_environment",
                "event_type": "publish",
                "object_label": "Duo Staging",
                "evidence": "Duo Staging 已发布",
                "app": "ChatGPT",
            },
            {
                "item_key": "menu_trigger",
                "event_type": "publish",
                "object_label": "CHAOSPACE",
                "evidence": "CHAOSPACE 菜单任务",
                "app": "ChatGPT",
                "reason_codes": [
                    "business_episode_candidate",
                    "policy_v7_trigger_unverified",
                ],
            },
        )
        for index, case in enumerate(cases):
            self._insert(
                at=self.base + timedelta(minutes=index * 20),
                **case,
            )
        self.conn.commit()

        payload = list_activity_confirmation_cards(
            self.conn,
            range_name="today",
            limit=48,
        )

        self.assertEqual(payload["pending_confirmation_count"], 1)
        answerable = [
            card for card in payload["cards"]
            if card["section"] == "pending_confirmation"
        ]
        self.assertEqual(
            [card["canonical_subject"]["object"] for card in answerable],
            ["Solo Staging 表单"],
        )
        self.assertTrue(
            all(
                card["display_question"] == "" and card["answer_options"] == []
                for card in payload["cards"]
                if card["section"] == "information_insufficient"
            )
        )
        states = {
            row["item_key"]: row["state"]
            for row in self.conn.execute(
                "SELECT item_key,state FROM activity_confirmation_items "
                "WHERE item_key IN ('internal_status','negated_fragment')"
            ).fetchall()
        }
        self.assertEqual(
            states,
            {
                "internal_status": "filtered",
                "negated_fragment": "filtered",
            },
        )

    def test_recruitment_chat_information_card_never_echoes_person_or_message(
        self,
    ) -> None:
        self._insert(
            "recruitment_private",
            event_type="send",
            object_label="张女士 ［送达］ 收到",
            evidence="张女士 ［送达］ 收到 已发送",
            app="BOSS直聘",
        )
        self.conn.commit()

        card = list_activity_confirmation_cards(
            self.conn,
            range_name="today",
        )["cards"][0]
        rendered = json.dumps(card, ensure_ascii=False)

        self.assertEqual(card["section"], "information_insufficient")
        self.assertEqual(card["display_question"], "")
        self.assertEqual(card["canonical_subject"]["object"], "")
        self.assertNotIn("张女士", rendered)
        self.assertNotIn("收到", rendered)
        self.assertNotIn("送达］", rendered)

    def test_concrete_objects_remain_answerable_with_required_evidence(self) -> None:
        cases = (
            ("market_report", "send", "国家市场调研报告", "国家市场调研报告已发送", "Dia", None),
            ("trade_quote", "send", "外贸报价单", "外贸报价单已发送", "Dia", None),
            ("recruitment_weekly", "send", "招聘周报", "招聘周报已发送", "Dia", None),
            ("duo_site", "publish", "Duo Staging 网站", "Duo Staging 网站已发布", "ChatGPT", ["independent_platform_receipt"]),
            ("hash_report", "completion", "哈希校验报告", "哈希校验报告已完成", "ChatGPT", [INDEPENDENT_FILE_EVIDENCE_SIGNAL]),
            (
                "rate_limit_report",
                "completion",
                "rate_limit 状态报告",
                "rate_limit 状态报告已完成",
                "Obsidian",
                None,
            ),
            (
                "word_report_with_file",
                "completion",
                "25页 Word 版报告",
                "已生成一份25页 Word 版报告",
                "Dia",
                [INDEPENDENT_FILE_EVIDENCE_SIGNAL],
            ),
        )
        for index, (
            item_key,
            event_type,
            object_label,
            evidence,
            app,
            signals,
        ) in enumerate(cases):
            self._insert(
                item_key,
                event_type=event_type,
                object_label=object_label,
                evidence=evidence,
                app=app,
                at=self.base + timedelta(minutes=index * 40),
                safe_signal_types=signals,
            )
        self.conn.commit()

        payload = list_activity_confirmation_cards(
            self.conn,
            range_name="today",
            limit=48,
        )
        answerable = {
            card["canonical_subject"]["object"]
            for card in payload["cards"]
            if card["section"] == "pending_confirmation"
        }

        self.assertEqual(
            answerable,
            {
                "国家市场调研报告",
                "外贸报价单",
                "招聘周报",
                "Duo Staging 网站",
                "哈希校验报告",
                "rate_limit 状态报告",
                "25页 Word 版报告",
            },
        )
        self.assertEqual(payload["information_insufficient_count"], 0)

    def test_generated_report_information_card_is_blocked_by_all_writers(
        self,
    ) -> None:
        self._insert(
            "generated_writer_guard",
            event_type="completion",
            object_label="25页 Word 版报告",
            evidence="已生成一份25页 Word 版报告",
            app="Dia",
        )
        self.conn.commit()
        card = list_activity_confirmation_cards(
            self.conn,
            range_name="today",
        )["cards"][0]
        before = tuple(
            self.conn.execute(
                "SELECT state,answer,answer_reason,notification_eligible "
                "FROM activity_confirmation_items "
                "WHERE item_key='generated_writer_guard'"
            ).fetchone()
        )

        grouped = answer_activity_confirmation_group(
            self.conn,
            group_key=card["group_key"],
            member_revision=card["member_revision"],
            answer="occurred",
            reason="correct",
            workstream="报告交付",
            outcome="completed",
        )
        if grouped["status"] == "stale":
            refreshed = list_activity_confirmation_cards(
                self.conn,
                range_name="today",
            )["cards"][0]
            grouped = answer_activity_confirmation_group(
                self.conn,
                group_key=refreshed["group_key"],
                member_revision=refreshed["member_revision"],
                answer="occurred",
                reason="correct",
                workstream="报告交付",
                outcome="completed",
            )
        single = answer_activity_confirmation(
            self.conn,
            item_key="generated_writer_guard",
            answer="occurred",
            reason="correct",
            workstream="报告交付",
            outcome="completed",
        )

        self.assertEqual(
            (grouped["status"], grouped["error"]),
            ("blocked", "information_insufficient"),
        )
        self.assertEqual(
            (single["status"], single["error"]),
            ("blocked", "information_insufficient"),
        )
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT state,answer,answer_reason,notification_eligible "
                    "FROM activity_confirmation_items "
                    "WHERE item_key='generated_writer_guard'"
                ).fetchone()
            ),
            before,
        )

    def test_same_episode_uses_canonical_object_across_ocr_spacing_variants(self) -> None:
        self._insert(
            "same_episode_one",
            event_type="send",
            object_label="国家市场调研报告",
            evidence="国家市场调研报告已发送",
            at=self.base,
            candidate_origin="business_episode",
            episode_id="episode_market_report",
        )
        self._insert(
            "same_episode_two",
            event_type="send",
            object_label="国家市场调研 报告",
            evidence="国家市场调研 报告已发送",
            at=self.base + timedelta(hours=2),
            candidate_origin="business_episode",
            episode_id="episode_market_report",
        )
        self.conn.commit()

        payload = list_activity_confirmation_cards(self.conn, range_name="today")

        self.assertEqual(payload["pending_confirmation_count"], 1)
        self.assertEqual(payload["cards"][0]["source_count"], 2)
        self.assertTrue(payload["cards"][0]["semantic_key"].startswith("sem_"))
        self.assertEqual(
            payload["cards"][0]["canonical_subject"]["object"],
            "国家市场调研报告",
        )

    def test_same_episode_does_not_merge_different_canonical_objects(self) -> None:
        self._insert(
            "same_episode_report",
            event_type="send",
            object_label="国家市场调研报告",
            evidence="国家市场调研报告已发送",
            at=self.base,
            candidate_origin="business_episode",
            episode_id="episode_shared",
        )
        self._insert(
            "same_episode_proposal",
            event_type="send",
            object_label="国家市场渠道方案",
            evidence="国家市场渠道方案已发送",
            at=self.base + timedelta(minutes=2),
            candidate_origin="business_episode",
            episode_id="episode_shared",
        )
        self.conn.commit()

        payload = list_activity_confirmation_cards(self.conn, range_name="today")

        self.assertEqual(payload["pending_confirmation_count"], 2)
        self.assertEqual(
            {card["canonical_subject"]["object"] for card in payload["cards"]},
            {"国家市场调研报告", "国家市场渠道方案"},
        )

    def test_event_specific_presentations_are_natural_and_unambiguous(self) -> None:
        self._insert(
            "interview_company",
            event_type="interview",
            object_label="",
            evidence="浩方面试可能已完成",
            at=self.base,
        )
        self._insert(
            "interview_round_only",
            event_type="interview",
            object_label="",
            evidence="第二轮面试可能已完成",
            at=self.base + timedelta(minutes=20),
        )
        for item_key, company, round_label in (
            ("interview_company", "浩方", ""),
            ("interview_round_only", "", "第二轮"),
        ):
            subject = {
                "event_label": "面试",
                "company": company,
                "round": round_label,
                "object": "",
                "label": "面试",
                "company_identified": bool(company),
                "round_identified": bool(round_label),
                "object_identified": False,
            }
            self.conn.execute(
                "UPDATE activity_confirmation_items SET subject=? WHERE item_key=?",
                (json.dumps(subject, ensure_ascii=False), item_key),
            )
        self._insert(
            "completion_copy",
            event_type="completion",
            object_label="客户报告",
            evidence="客户报告已完成",
            at=self.base + timedelta(minutes=40),
        )
        self._insert(
            "meeting_copy",
            event_type="meeting",
            object_label="客户周会",
            evidence="客户周会可能已举行",
            at=self.base + timedelta(minutes=60),
        )
        self._insert(
            "call_copy",
            event_type="call",
            object_label="客户通话",
            evidence="客户通话可能已完成",
            at=self.base + timedelta(minutes=80),
        )
        self.conn.commit()

        cards = {
            card["event_type"] + ":" + card["display_title"]: card
            for card in list_activity_confirmation_cards(
                self.conn,
                range_name="today",
                limit=12,
            )["cards"]
        }
        interview = cards["interview:浩方面试"]
        self.assertIn("你是否参加了浩方面试", interview["display_question"])
        self.assertIn("可能参加了浩方面试", interview["display_evidence"])
        self.assertNotIn("面试浩方面试", json.dumps(interview, ensure_ascii=False))

        round_only = next(
            card
            for card in cards.values()
            if card["event_type"] == "interview"
            and card["section"] == "information_insufficient"
        )
        self.assertEqual(round_only["display_question"], "")

        completion = cards["completion:完成客户报告"]
        self.assertIn("可能已完成客户报告", completion["display_evidence"])
        meeting = cards["meeting:参加客户周会"]
        self.assertIn("可能参加了客户周会", meeting["display_evidence"])
        call = cards["call:进行客户通话"]
        self.assertIn("可能进行了客户通话", call["display_evidence"])

    def test_aggregated_accessibility_episode_rechecks_self_ui(self) -> None:
        activity = {
            "id": "source_self_ui",
            "cleaned_text": "待确认 判断依据 7月21日，发布真的已经发生了吗？",
            "activity_summary": "PAM 待确认页面正在显示发布问题",
            "cleaned_title": "待确认",
            "apps": ["Accessibility Services"],
            "started_at": self.base.isoformat(timespec="seconds"),
            "ended_at": (self.base + timedelta(minutes=3)).isoformat(timespec="seconds"),
            "screen_region_signal": {
                "region_kind": "main_content",
                "actionability": "actionable",
                "reason_codes": [],
            },
            "_business_episode_anchor": True,
            "_business_episode_strong_anchor": True,
            "_business_episode_profile": {"event_hint": "publish"},
        }
        self.assertIsNone(_business_episode_candidate(self.conn, [activity]))

    def test_cards_final_self_loop_filter_preserves_real_accessibility_work_and_counts(
        self,
    ) -> None:
        legacy_loop_reasons = [
            "business_episode_candidate",
            "policy_v6_evaluated",
            "policy_v7_evaluated",
            "policy_v7_trigger_unverified",
            "weak_occurrence_signal",
            "policy_v7_information_insufficient_missing_subject",
        ]
        self._insert(
            "explicit_pam_self_loop",
            event_type="send",
            object_label="",
            evidence="待确认 判断依据 发生了 没有发生 安全线索",
            app="Accessibility Services",
            candidate_origin="business_episode",
            reason_codes=legacy_loop_reasons,
            safe_signal_types=["anonymous_subject", "post_firewall_activity"],
        )
        # A current-policy, pure-v7 generic AX clue is not enough to prove a
        # PAM loop.  Keep it in information-insufficient for later context.
        self._insert(
            "pure_v7_accessibility_clue",
            event_type="send",
            object_label="",
            evidence="发送线索对象未识别",
            app="Accessibility Services",
            at=self.base + timedelta(minutes=3),
            candidate_origin="business_episode",
            reason_codes=[
                reason
                for reason in legacy_loop_reasons
                if reason != "policy_v6_evaluated"
            ],
            safe_signal_types=["anonymous_subject", "post_firewall_activity"],
        )
        # Old rows may retain only the weak/unverified episode signature after
        # semantic normalization has removed the original PAM UI words.
        self._insert(
            "normalized_legacy_pam_loop",
            event_type="send",
            object_label="",
            evidence="发送线索对象未识别",
            app="Accessibility Services",
            at=self.base + timedelta(minutes=2),
            candidate_origin="business_episode",
            reason_codes=legacy_loop_reasons,
            safe_signal_types=["anonymous_subject", "post_firewall_activity"],
        )
        # A concrete business event that happens to arrive through the macOS
        # accessibility bridge must remain visible.  Use the same old reason
        # signature so answerability, not the app label alone, is decisive.
        self._insert(
            "real_accessibility_business_event",
            event_type="send",
            object_label="客户调研报告",
            evidence="客户调研报告已发送",
            app="Accessibility Services",
            at=self.base + timedelta(minutes=4),
            candidate_origin="business_episode",
            reason_codes=legacy_loop_reasons,
            safe_signal_types=["post_firewall_activity", "structured_surface"],
        )
        for index in range(13):
            self._insert(
                f"normal_visible_{index:02d}",
                event_type="completion",
                object_label=f"客户交付报告{index:02d}",
                evidence=f"客户交付报告{index:02d}已完成",
                app="Obsidian",
                at=self.base + timedelta(minutes=10 + index),
            )
        self.conn.commit()

        first = list_activity_confirmation_cards(
            self.conn,
            range_name="today",
            limit=12,
        )
        revision = activity_confirmation_revision(self.conn, range_name="today")
        second = list_activity_confirmation_cards(
            self.conn,
            range_name="today",
            limit=12,
            cursor=first["next_cursor"],
        )

        self.assertEqual(first["contract_version"], 5)
        self.assertFalse(first["raw_access"])
        self.assertEqual(first["revision"], revision["revision"])
        self.assertEqual(first["total_card_count"], 15)
        self.assertEqual(revision["total_card_count"], 15)
        self.assertEqual(first["pending_confirmation_count"], 14)
        self.assertEqual(revision["pending_confirmation_count"], 14)
        self.assertEqual(first["information_insufficient_count"], 1)
        self.assertEqual(revision["information_insufficient_count"], 1)
        self.assertEqual(first["pending_source_count"], 14)
        self.assertEqual(revision["pending_source_count"], 14)
        self.assertEqual(len(first["cards"]), 12)
        self.assertTrue(first["has_more"])
        self.assertEqual(len(second["cards"]), 3)
        self.assertFalse(second["has_more"])
        self.assertFalse(second["cursor_reset"])

        visible_cards = [*first["cards"], *second["cards"]]
        self.assertEqual(len(visible_cards), 15)
        accessibility_card = next(
            card
            for card in visible_cards
            if card["canonical_subject"]["object"] == "客户调研报告"
        )
        self.assertEqual(accessibility_card["app"], "系统辅助识别")
        self.assertEqual(accessibility_card["section"], "pending_confirmation")
        accessibility_clue = next(
            card
            for card in visible_cards
            if card["section"] == "information_insufficient"
        )
        self.assertEqual(accessibility_clue["app"], "系统辅助识别")
        self.assertEqual(accessibility_clue["display_question"], "")
        encoded = json.dumps(
            {"first": first, "second": second, "revision": revision},
            ensure_ascii=False,
        )
        self.assertNotIn("Accessibility Services", encoded)
        self.assertNotIn("explicit_pam_self_loop", encoded)
        self.assertNotIn("normalized_legacy_pam_loop", encoded)
        # Filtering is read-only presentation logic; retained evidence rows are
        # not silently mutated or deleted.
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM activity_confirmation_items "
                "WHERE state='pending' AND answer IS NULL"
            ).fetchone()[0],
            17,
        )

    def test_revision_is_stable_and_stale_cursor_resets(self) -> None:
        self._insert("one", object_label="客户公告")
        self.conn.commit()
        first = activity_confirmation_revision(self.conn, range_name="today")
        second = activity_confirmation_revision(self.conn, range_name="today")
        self.assertEqual(first["revision"], second["revision"])

        self._insert("two", object_label="产品手册", at=self.base + timedelta(minutes=20))
        self.conn.commit()
        changed = activity_confirmation_revision(self.conn, range_name="today")
        self.assertNotEqual(first["revision"], changed["revision"])
        page = list_activity_confirmation_cards(
            self.conn,
            range_name="today",
            cursor=f"{first['revision']}.1",
        )
        self.assertTrue(page["cursor_reset"])
        self.assertEqual(len(page["cards"]), 2)

    def test_policy_v7_preview_and_apply_preserve_answer_and_evidence(self) -> None:
        self._insert(
            "self_loop",
            object_label="",
            evidence="待确认 判断依据 发布真的已经发生了吗",
            app="Accessibility Services",
        )
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.conn.execute(
            "UPDATE settings SET value='5',updated_at=? WHERE key=?",
            (now, CONFIRMATION_POLICY_SETTING_KEY),
        )
        self.conn.execute(
            "INSERT INTO activity_confirmation_evidence_files("
            "evidence_id,source_frame_id,captured_at,display_id,file_name,content_sha256,"
            "byte_size,state,backup_excluded,last_error,created_at,updated_at,delete_reason) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "evidence_1", "frame_1", now, "display", "", "", 0,
                "unavailable", 1, "source_unavailable", now, now, "",
            ),
        )
        self.conn.execute(
            "INSERT INTO activity_confirmation_evidence_refs("
            "evidence_id,item_key,source_activity_id,deletion_group_id,created_at) "
            "VALUES(?,?,?,?,?)",
            ("evidence_1", "self_loop", "", "", now),
        )
        self.conn.commit()
        before = tuple(
            self.conn.execute(
                "SELECT state,answer,evidence,reason_codes FROM activity_confirmation_items "
                "WHERE item_key='self_loop'"
            ).fetchone()
        )

        preview = revalidate_unresolved_confirmation_policy(self.conn, dry_run=True)
        self.assertEqual((preview["status"], preview["rejected"]), ("preview", 1))
        self.assertEqual(
            (
                preview["raw_access"],
                preview["destructive_actions"],
                preview["memory_write_actions"],
            ),
            (False, False, False),
        )
        after_preview = tuple(
            self.conn.execute(
                "SELECT state,answer,evidence,reason_codes FROM activity_confirmation_items "
                "WHERE item_key='self_loop'"
            ).fetchone()
        )
        self.assertEqual(after_preview, before)
        self.assertEqual(
            self.conn.execute(
                "SELECT value FROM settings WHERE key=?",
                (CONFIRMATION_POLICY_SETTING_KEY,),
            ).fetchone()[0],
            "5",
        )

        applied = revalidate_unresolved_confirmation_policy(self.conn)
        self.assertEqual((applied["status"], applied["rejected"]), ("applied", 1))
        current = revalidate_unresolved_confirmation_policy(self.conn)
        self.assertEqual(current["status"], "current")
        self.assertFalse(current["raw_access"])
        row = self.conn.execute(
            "SELECT state,answer,evidence FROM activity_confirmation_items "
            "WHERE item_key='self_loop'"
        ).fetchone()
        self.assertEqual(tuple(row), ("filtered", None, "待确认 判断依据 发布真的已经发生了吗"))
        ref = self.conn.execute(
            "SELECT released_at FROM activity_confirmation_evidence_refs "
            "WHERE evidence_id='evidence_1' AND item_key='self_loop'"
        ).fetchone()
        self.assertIsNotNone(ref)
        self.assertIsNone(ref["released_at"])

    def test_policy_v7_question_safety_revision_rechecks_existing_v7_rows(
        self,
    ) -> None:
        self._insert(
            "legacy_v7_generated",
            event_type="completion",
            object_label="25页 Word 版报告",
            evidence="已生成一份25页 Word 版报告",
            app="Dia",
        )
        self._insert(
            "legacy_v7_internal",
            event_type="send",
            object_label="在 payload 的 report_output_hashes 字段中检测到状态",
            evidence="在 payload 的 report_output_hashes 字段中检测到状态已发送",
            app="ChatGPT",
            at=self.base + timedelta(minutes=20),
        )
        old_codes = json.dumps(
            ["possible_occurrence", "policy_v7_evaluated"],
            ensure_ascii=False,
        )
        self.conn.execute(
            "UPDATE activity_confirmation_items "
            "SET state='pending',reason_codes=?,decision_question='legacy question',"
            "notification_eligible=1 WHERE item_key IN (?,?)",
            (
                old_codes,
                "legacy_v7_generated",
                "legacy_v7_internal",
            ),
        )
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.conn.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (CONFIRMATION_POLICY_SETTING_KEY, "7", now),
        )
        self.conn.commit()

        preview = revalidate_unresolved_confirmation_policy(
            self.conn,
            dry_run=True,
        )
        applied = revalidate_unresolved_confirmation_policy(self.conn)

        self.assertEqual((preview["status"], preview["examined"]), ("preview", 2))
        self.assertEqual((applied["status"], applied["rejected"]), ("applied", 1))
        generated = self.conn.execute(
            "SELECT state,answer,decision_question,notification_eligible,reason_codes "
            "FROM activity_confirmation_items WHERE item_key='legacy_v7_generated'"
        ).fetchone()
        internal = self.conn.execute(
            "SELECT state,answer FROM activity_confirmation_items "
            "WHERE item_key='legacy_v7_internal'"
        ).fetchone()
        self.assertEqual(
            tuple(generated)[:4],
            ("pending", None, "", 0),
        )
        self.assertIn(
            CONFIRMATION_POLICY_QUESTION_SAFETY_REASON,
            json.loads(generated["reason_codes"]),
        )
        self.assertEqual(tuple(internal), ("filtered", None))
        self.assertEqual(
            revalidate_unresolved_confirmation_policy(self.conn)["status"],
            "current",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()[0],
            "32",
        )

    def test_revalidate_preserves_safe_stored_object_and_accepts_strict_refinement(
        self,
    ) -> None:
        self._insert(
            "stored_notice",
            object_label="客户公告",
            evidence="发布成功",
        )
        self._insert(
            "refined_notice",
            object_label="公告",
            evidence="客户公告已发布",
            at=self.base + timedelta(minutes=20),
        )
        old_codes = json.dumps(
            ["possible_publish_occurrence", "policy_v7_evaluated"],
            ensure_ascii=False,
        )
        self.conn.execute(
            "UPDATE activity_confirmation_items "
            "SET reason_codes=?,decision_question='legacy question' "
            "WHERE item_key IN ('stored_notice','refined_notice')",
            (old_codes,),
        )
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.conn.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (CONFIRMATION_POLICY_SETTING_KEY, "7", now),
        )
        self.conn.commit()

        applied = revalidate_unresolved_confirmation_policy(self.conn)

        self.assertEqual((applied["status"], applied["rejected"]), ("applied", 0))
        rows = {
            row["item_key"]: row
            for row in self.conn.execute(
                "SELECT item_key,subject,decision_question "
                "FROM activity_confirmation_items "
                "WHERE item_key IN ('stored_notice','refined_notice')"
            ).fetchall()
        }
        self.assertEqual(
            json.loads(rows["stored_notice"]["subject"])["object"],
            "客户公告",
        )
        self.assertIn("客户公告", rows["stored_notice"]["decision_question"])
        self.assertEqual(
            json.loads(rows["refined_notice"]["subject"])["object"],
            "客户公告",
        )
        self.assertIn("客户公告", rows["refined_notice"]["decision_question"])

    def test_policy_v7_restructure_never_updates_answered_rows_or_their_evidence(self) -> None:
        self._insert(
            "unanswered_fragment",
            event_type="send",
            object_label="国家市场",
            evidence="国家市场已发送",
            group_key="legacy_unanswered",
        )
        self._insert(
            "answered_history",
            event_type="completion",
            object_label="历史客户报告",
            evidence="历史客户报告已完成",
            group_key="legacy_answered",
        )
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.conn.execute(
            "UPDATE activity_confirmation_items "
            "SET answer='occurred',answer_reason='correct',answered_at=? "
            "WHERE item_key='answered_history'",
            (now,),
        )
        self.conn.execute(
            "UPDATE settings SET value='6',updated_at=? WHERE key=?",
            (now, CONFIRMATION_POLICY_SETTING_KEY),
        )
        self.conn.execute(
            "INSERT INTO activity_confirmation_evidence_files("
            "evidence_id,source_frame_id,captured_at,display_id,file_name,content_sha256,"
            "byte_size,state,backup_excluded,last_error,created_at,updated_at,delete_reason) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "answered_evidence", "answered_frame", now, "display", "", "", 0,
                "unavailable", 1, "source_unavailable", now, now, "",
            ),
        )
        self.conn.execute(
            "INSERT INTO activity_confirmation_evidence_refs("
            "evidence_id,item_key,source_activity_id,deletion_group_id,created_at) "
            "VALUES(?,?,?,?,?)",
            ("answered_evidence", "answered_history", "", "", now),
        )
        self.conn.commit()
        answered_before = tuple(
            self.conn.execute(
                "SELECT state,answer,answer_reason,answered_at,group_key,group_signature,"
                "subject,evidence,decision_question,reason_codes,updated_at "
                "FROM activity_confirmation_items WHERE item_key='answered_history'"
            ).fetchone()
        )
        ref_before = tuple(
            self.conn.execute(
                "SELECT evidence_id,item_key,released_at,release_reason "
                "FROM activity_confirmation_evidence_refs "
                "WHERE evidence_id='answered_evidence'"
            ).fetchone()
        )
        unanswered_before = tuple(
            self.conn.execute(
                "SELECT subject,group_key,reason_codes FROM activity_confirmation_items "
                "WHERE item_key='unanswered_fragment'"
            ).fetchone()
        )

        preview = revalidate_unresolved_confirmation_policy(self.conn, dry_run=True)

        self.assertEqual(preview["status"], "preview")
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT subject,group_key,reason_codes FROM activity_confirmation_items "
                    "WHERE item_key='unanswered_fragment'"
                ).fetchone()
            ),
            unanswered_before,
        )
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT state,answer,answer_reason,answered_at,group_key,group_signature,"
                    "subject,evidence,decision_question,reason_codes,updated_at "
                    "FROM activity_confirmation_items WHERE item_key='answered_history'"
                ).fetchone()
            ),
            answered_before,
        )

        applied = revalidate_unresolved_confirmation_policy(self.conn)

        self.assertEqual(applied["status"], "applied")
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT state,answer,answer_reason,answered_at,group_key,group_signature,"
                    "subject,evidence,decision_question,reason_codes,updated_at "
                    "FROM activity_confirmation_items WHERE item_key='answered_history'"
                ).fetchone()
            ),
            answered_before,
        )
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT evidence_id,item_key,released_at,release_reason "
                    "FROM activity_confirmation_evidence_refs "
                    "WHERE evidence_id='answered_evidence'"
                ).fetchone()
            ),
            ref_before,
        )
        unanswered = self.conn.execute(
            "SELECT answer,subject FROM activity_confirmation_items "
            "WHERE item_key='unanswered_fragment'"
        ).fetchone()
        self.assertIsNone(unanswered["answer"])
        self.assertEqual(json.loads(unanswered["subject"])["object"], "")

    def test_platform_absolute_paths_are_redacted_without_matching_normal_objects(
        self,
    ) -> None:
        absolute_paths = (
            "/Users/example/private.txt",
            "/UsersGuide",
            "/custom/private.txt",
            "/nix/store/private-package",
            "/variety",
            "/Systematic",
            "/optimize",
            "path:/custom/private.txt",
            "path: /nix/store/private-package",
            "/tmp/private.txt",
            "~/private.txt",
            "/Volumes/PrivateVault/private.txt",
            "/private/var/db/private.sqlite",
            "/var/log/private.log",
            "/Library/Preferences/private.plist",
            "/Applications/Private.app",
            "/System/Library/private.bundle",
            "/opt/homebrew/private",
            "/usr/local/private",
            "/etc/private.conf",
            "/dev/private-device",
            "file:///Volumes/PrivateVault/private.txt",
            r"C:\Users\Example\private.txt",
            "D:/Private/private.txt",
            r"\\fileserver\private-share\private.docx",
            "file://fileserver/private-share/private.docx",
        )
        for path in absolute_paths:
            with self.subTest(path=path):
                self.assertTrue(contains_absolute_path(path))
                sanitized = safe_decision_text(path)
                self.assertEqual(sanitized, "[path_redacted]")
                self.assertNotIn("private", sanitized.casefold())

        self.assertEqual(
            safe_decision_text(
                "回执位于 /Volumes/Private Vault/cursor-secret"
            ),
            "[path_redacted]",
        )

        normal_objects = (
            "客户公告",
            "天命鉴 Staging 网站",
            "海外渠道/代理商招募",
            "https://example.com/Users/example/help",
            "//cdn.example.com/assets/app.js",
            "//intranet/docs/help",
            "//fileserver/private-share/private.docx",
            "file:report",
            "file:reports/private.docx",
        )
        for object_label in normal_objects:
            with self.subTest(object_label=object_label):
                self.assertFalse(contains_absolute_path(object_label))
                self.assertEqual(safe_decision_text(object_label), object_label)

    def test_cjk_glued_posix_paths_fail_closed_across_cards_v5_and_legacy_v4(
        self,
    ) -> None:
        # Keep the realistic non-fixture username split in source so the
        # release privacy scanner can still reject accidentally committed
        # machine-local paths while this test exercises the exact runtime text.
        glued_path = "文件在/" + "Users/alex/Secret Project/result.txt"
        self.assertTrue(contains_absolute_path(glued_path))
        self.assertEqual(safe_decision_text(glued_path), "[path_redacted]")
        self.assertTrue(
            contains_absolute_path("输出到/custom-root/project/result.pdf")
        )
        # Arbitrary roots remain path-bearing at a normal punctuation boundary.
        self.assertTrue(
            contains_absolute_path("路径：/arbitrary-root/project/readme")
        )

        normal_slash_text = (
            "海外渠道/代理商招募",
            "普通中文/斜杠表达",
            "普通分类/一级/二级",
            "https://example.com/Users/example/help",
            "//intranet/docs/help",
            "文档//intranet/docs/help",
            "file:relative",
        )
        for text in normal_slash_text:
            with self.subTest(text=text):
                self.assertFalse(contains_absolute_path(text))
                self.assertEqual(safe_decision_text(text), text)

        self._insert(
            "cjk_glued_path_surface",
            event_type="completion",
            object_label="安全占位对象",
            evidence="安全占位线索",
        )
        path_subject = {
            "event_label": "完成事项",
            "company": "",
            "round": "",
            "object": glued_path,
            "label": f"{glued_path}完成事项",
            "object_identified": True,
        }
        self.conn.execute(
            "UPDATE activity_confirmation_items "
            "SET subject=?,evidence=?,cleaned_content=?,safe_observation=?,"
            "decision_question=?,why_uncertain=?,app_family=? WHERE item_key=?",
            (
                json.dumps(path_subject, ensure_ascii=False),
                glued_path,
                glued_path,
                glued_path,
                glued_path,
                glued_path,
                glued_path,
                "cjk_glued_path_surface",
            ),
        )
        self.conn.commit()

        cards = list_activity_confirmation_cards(self.conn, range_name="today")
        self.assertEqual(cards["contract_version"], 5)
        self.assertFalse(cards["raw_access"])
        self.assertEqual(cards["pending_confirmation_count"], 0)
        self.assertEqual(cards["information_insufficient_count"], 1)
        card = cards["cards"][0]
        self.assertEqual(card["section"], "information_insufficient")
        self.assertEqual(card["subject_status"], "information_insufficient")
        self.assertEqual(card["canonical_subject"]["object"], "")
        self.assertFalse(card["canonical_subject"]["object_identified"])
        self.assertEqual(card["display_question"], "")
        self.assertEqual(card["answer_options"], [])

        legacy = list_activity_confirmations(self.conn, range_name="today")
        self.assertEqual(legacy["contract_version"], 4)
        self.assertFalse(legacy["raw_access"])
        self.assertEqual(len(legacy["question_groups"]), 1)
        self.assertEqual(len(legacy["items"]), 1)
        group = legacy["question_groups"][0]
        item = legacy["items"][0]
        self.assertEqual(group["subject"]["object"], "[path_redacted]")
        self.assertEqual(group["trigger_sentence"], "[path_redacted]")
        self.assertEqual(group["app"], "[path_redacted]")
        self.assertEqual(
            group["cleaned_contents"],
            [{"text": "[path_redacted]", "count": 1}],
        )
        self.assertEqual(item["subject"]["object"], "[path_redacted]")
        self.assertEqual(item["trigger_sentence"], "[path_redacted]")
        self.assertEqual(item["app"], "[path_redacted]")

        serialized_cards = json.dumps(cards, ensure_ascii=False)
        serialized_legacy = json.dumps(legacy, ensure_ascii=False)
        for leaked_fragment in (
            "/Users/",
            "alex",
            "Secret Project",
            "result.txt",
        ):
            with self.subTest(leaked_fragment=leaked_fragment):
                self.assertNotIn(leaked_fragment, serialized_cards)
                self.assertNotIn(leaked_fragment, serialized_legacy)

    def test_legacy_v4_groups_and_items_recursively_redact_paths(self) -> None:
        secret_fragments = (
            "/custom/legacy-object-secret",
            "/nix/store/legacy-evidence-secret",
            r"\\fileserver\private-share\legacy-app-secret",
        )
        self._insert(
            "legacy_path_surface",
            event_type="completion",
            object_label="安全占位对象",
            evidence="安全占位线索",
        )
        legacy_subject = {
            "event_label": "完成事项",
            "company": "",
            "round": "",
            "object": secret_fragments[0],
            "label": f"{secret_fragments[0]}完成事项",
            "object_identified": True,
        }
        self.conn.execute(
            "UPDATE activity_confirmation_items "
            "SET subject=?,evidence=?,cleaned_content=?,safe_observation=?,"
            "decision_question=?,why_uncertain=?,app_family=? WHERE item_key=?",
            (
                json.dumps(legacy_subject, ensure_ascii=False),
                f"结果保存在 {secret_fragments[1]} extra-sensitive-context",
                f"结果保存在 {secret_fragments[1]} extra-sensitive-context",
                f"结果保存在 {secret_fragments[1]} extra-sensitive-context",
                f"是否完成 {secret_fragments[0]} adjacent-question-context？",
                f"核对 {secret_fragments[1]} adjacent-reason-context",
                f"Tool {secret_fragments[2]} adjacent-sensitive-context",
                "legacy_path_surface",
            ),
        )
        self.conn.commit()

        payload = list_activity_confirmations(self.conn, range_name="today")

        self.assertEqual(payload["contract_version"], 4)
        self.assertFalse(payload["raw_access"])
        self.assertEqual(len(payload["question_groups"]), 1)
        self.assertEqual(len(payload["items"]), 1)
        group = payload["question_groups"][0]
        item = payload["items"][0]
        self.assertEqual(group["subject"]["object"], "[path_redacted]")
        self.assertEqual(group["trigger_sentence"], "[path_redacted]")
        self.assertEqual(group["app"], "[path_redacted]")
        self.assertEqual(
            group["cleaned_contents"],
            [{"text": "[path_redacted]", "count": 1}],
        )
        self.assertEqual(item["subject"]["object"], "[path_redacted]")
        self.assertEqual(item["trigger_sentence"], "[path_redacted]")
        self.assertEqual(item["app"], "[path_redacted]")
        serialized = json.dumps(payload, ensure_ascii=False)
        for leaked_fragment in (
            "legacy-object-secret",
            "legacy-evidence-secret",
            "legacy-app-secret",
            "extra-sensitive-context",
            "adjacent-sensitive-context",
            "adjacent-question-context",
            "adjacent-reason-context",
        ):
            with self.subTest(leaked_fragment=leaked_fragment):
                self.assertNotIn(leaked_fragment, serialized)

    def test_cards_fail_closed_for_paths_in_object_evidence_and_company(self) -> None:
        rows = (
            (
                "path_object",
                "completion",
                {
                    "event_label": "完成事项",
                    "company": "",
                    "round": "",
                    "object": "/custom/object-secret.txt",
                    "label": "/custom/object-secret.txt完成事项",
                    "object_identified": True,
                },
                "客户报告已完成",
                "客户报告已完成",
            ),
            (
                "path_evidence",
                "send",
                {
                    "event_label": "发送",
                    "company": "",
                    "round": "",
                    "object": "客户公告",
                    "label": "客户公告发送",
                    "object_identified": True,
                },
                "回执位于 file:///nix/store/evidence-secret.json",
                "回执位于 file:///nix/store/evidence-secret.json",
            ),
            (
                "path_company",
                "interview",
                {
                    "event_label": "面试",
                    "company": r"C:\Users\Example\company-secret.txt",
                    "round": "终面",
                    "object": "",
                    "label": r"C:\Users\Example\company-secret.txt终面",
                    "company_identified": True,
                    "round_identified": True,
                    "object_identified": False,
                },
                r"面试材料来自 \\fileserver\private-share\company-secret.docx",
                r"面试材料来自 \\fileserver\private-share\company-secret.docx",
            ),
        )
        for index, (item_key, event_type, subject, evidence, cleaned_content) in enumerate(
            rows
        ):
            self._insert(
                item_key,
                event_type=event_type,
                object_label="安全占位对象",
                evidence="安全占位线索",
                at=self.base + timedelta(minutes=index * 10),
                group_key=f"grp_{item_key}",
            )
            self.conn.execute(
                "UPDATE activity_confirmation_items "
                "SET subject=?,evidence=?,cleaned_content=?,safe_observation=? "
                "WHERE item_key=?",
                (
                    json.dumps(subject, ensure_ascii=False),
                    evidence,
                    cleaned_content,
                    evidence,
                    item_key,
                ),
            )
        self.conn.commit()

        payload = list_activity_confirmation_cards(
            self.conn,
            range_name="today",
            cursor="/Volumes/Private Vault/cursor-secret",
        )

        self.assertEqual(payload["contract_version"], 5)
        self.assertEqual(payload["cursor"], "[path_redacted]")
        self.assertEqual(payload["total_card_count"], 3)
        self.assertEqual(payload["pending_confirmation_count"], 0)
        self.assertEqual(payload["information_insufficient_count"], 3)
        self.assertEqual(payload["pending_question_count"], 0)
        self.assertEqual(len(payload["cards"]), 3)
        for card in payload["cards"]:
            with self.subTest(group_key=card["group_key"]):
                self.assertEqual(card["section"], "information_insufficient")
                self.assertEqual(card["subject_status"], "information_insufficient")
                self.assertEqual(card["canonical_subject"]["object"], "")
                self.assertFalse(card["canonical_subject"]["object_identified"])
                self.assertEqual(card["display_question"], "")
                self.assertEqual(card["answer_options"], [])
                self.assertEqual(card["display_title"], "信息不足：本机路径已隐藏")
                self.assertEqual(
                    card["verification_route"],
                    "information_insufficient",
                )

        serialized = json.dumps(payload, ensure_ascii=False)
        for leaked_fragment in (
            "/Volumes/",
            "/custom/",
            "/nix/",
            "/private/",
            "file://",
            r"C:\Users",
            r"\\fileserver",
            "object-secret",
            "evidence-secret",
            "company-secret",
            "cursor-secret",
        ):
            with self.subTest(leaked_fragment=leaked_fragment):
                self.assertNotIn(leaked_fragment, serialized)

    def test_normal_chinese_object_remains_answerable_after_path_hardening(self) -> None:
        self._insert(
            "normal_chinese_object",
            event_type="publish",
            object_label="天命鉴 Staging 网站",
            evidence="天命鉴 Staging 网站已发布",
        )
        self.conn.commit()

        payload = list_activity_confirmation_cards(self.conn, range_name="today")

        self.assertEqual(payload["pending_confirmation_count"], 1)
        self.assertEqual(payload["information_insufficient_count"], 0)
        card = payload["cards"][0]
        self.assertEqual(card["section"], "pending_confirmation")
        self.assertEqual(card["subject_status"], "identified")
        self.assertEqual(card["canonical_subject"]["object"], "天命鉴 Staging 网站")
        self.assertIn("天命鉴 Staging 网站", card["display_question"])
        self.assertTrue(card["answer_options"])

    def test_ai_chat_and_terminal_completion_degrade_to_information_insufficient(self) -> None:
        """AI-tool/terminal claims must become low-disturbance clues, not questions."""
        apps = ("iTerm2", "Warp", "Terminal", "ChatGPT", "Claude", "Codex")
        for index, app in enumerate(apps):
            self._insert(
                f"ai_surface_{index}",
                event_type="completion",
                object_label=f"部署脚本{index}",
                evidence=f"部署脚本{index}已完成部署",
                app=app,
                reason_codes=["submit_control_geometry", "submit_interaction_evidence"],
                at=self.base + timedelta(minutes=index),
            )
        self.conn.commit()

        rows = self.conn.execute(
            "SELECT state, decision_question, notification_eligible "
            "FROM activity_confirmation_items WHERE item_key LIKE 'ai_surface_%'"
        ).fetchall()
        self.assertEqual(len(rows), len(apps))
        for row in rows:
            self.assertEqual(row["state"], "pending")
            self.assertEqual(row["decision_question"], "")
            self.assertFalse(bool(row["notification_eligible"]))

        payload = list_activity_confirmation_cards(self.conn, range_name="today")
        self.assertEqual(payload["pending_confirmation_count"], 0)
        self.assertEqual(payload["information_insufficient_count"], len(payload["cards"]))
        for card in payload["cards"]:
            self.assertEqual(card["section"], "information_insufficient")
            self.assertFalse(card["display_question"])
            self.assertFalse(card["answer_options"])

    def test_ai_tool_with_independent_evidence_still_asks(self) -> None:
        """Independent artifact evidence keeps a real event answerable."""
        self._insert(
            "ai_with_receipt",
            event_type="completion",
            object_label="季度复盘报告",
            evidence="季度复盘报告已完成",
            app="iTerm2",
            safe_signal_types=["post_firewall_activity", "git_commit_receipt"],
        )
        self.conn.commit()

        payload = list_activity_confirmation_cards(self.conn, range_name="today")
        self.assertEqual(payload["pending_confirmation_count"], 1)
        card = payload["cards"][0]
        self.assertEqual(card["section"], "pending_confirmation")
        self.assertTrue(card["answer_options"])

    def test_terminal_tokens_do_not_catch_normal_apps_or_realtime_events(self) -> None:
        """Ordinary apps and real-time human events stay answerable."""
        self._insert(
            "word_completion",
            event_type="completion",
            object_label="项目总结文档",
            evidence="项目总结文档已完成",
            app="Microsoft Word",
        )
        self._insert(
            "terminal_meeting",
            event_type="meeting",
            object_label="客户评审会议",
            evidence="客户评审会议已进行",
            app="iTerm2",
            at=self.base + timedelta(minutes=5),
        )
        self.conn.commit()

        payload = list_activity_confirmation_cards(self.conn, range_name="today")
        sections = {
            card["canonical_subject"]["object"]: card["section"]
            for card in payload["cards"]
        }
        self.assertEqual(sections.get("项目总结文档"), "pending_confirmation")
        self.assertEqual(sections.get("客户评审会议"), "pending_confirmation")

    def test_ocr_artifact_objects_are_information_insufficient(self) -> None:
        """Interface-noise objects (unclosed brackets, symbol debris, tooling
        identifiers, bare platform names, office-menu words) never become
        questions."""
        artifacts = (
            ("wps_menu", "completion", "季度提纲第三节任务（4"),
            ("symbol_debris", "publish", "发布报告〉 数据表整理好了"),
            ("tooling_id", "completion", "后台运行 deploy:prod 全量检查"),
            ("platform_only", "send", "GitHub"),
            ("office_menu", "completion", "当页开始格式刷粘贴新提纲"),
            ("ellipsis_cut", "publish", "swer 数据表 in R2 pac... 结果找到了"),
            ("circled_marker", "completion", "新提纲主线 ⑧• 排查准确性你"),
            ("audience_only", "send", "海外大客户"),
            ("conjunction_verb", "completion", "并通过最终验收"),
            ("conjunction_verb2", "submission", "并提交季度预算"),
        )
        for index, (key, event, obj) in enumerate(artifacts):
            self._insert(
                f"artifact_{key}",
                event_type=event,
                object_label=obj,
                evidence=f"{obj}已完成",
                app="wpsoffice" if key != "platform_only" else "Dia",
                at=self.base + timedelta(minutes=index),
            )
        self.conn.commit()

        payload = list_activity_confirmation_cards(self.conn, range_name="today")
        self.assertEqual(payload["pending_confirmation_count"], 0)
        for card in payload["cards"]:
            self.assertEqual(card["section"], "information_insufficient")
            self.assertFalse(card["answer_options"])

    def test_normal_objects_survive_ocr_artifact_screen(self) -> None:
        """Well-formed objects, including closed brackets, stay answerable."""
        self._insert(
            "closed_bracket",
            event_type="completion",
            object_label="产品方案（终版）定稿",
            evidence="产品方案（终版）定稿已完成",
            app="Pages",
        )
        self._insert(
            "plain_object",
            event_type="send",
            object_label="海外渠道月度报告",
            evidence="海外渠道月度报告已发送",
            app="Mail",
            at=self.base + timedelta(minutes=1),
        )
        # Ordinary words that merely begin with a conjunction character must
        # survive the leading-conjunction screen.
        self._insert(
            "conjunction_lookalike",
            event_type="completion",
            object_label="和平协议签署文件",
            evidence="和平协议签署文件已完成",
            app="Pages",
            at=self.base + timedelta(minutes=2),
        )
        self.conn.commit()

        payload = list_activity_confirmation_cards(self.conn, range_name="today")
        self.assertEqual(payload["pending_confirmation_count"], 3)
        for card in payload["cards"]:
            self.assertEqual(card["section"], "pending_confirmation")
            self.assertTrue(card["answer_options"])

    def test_existing_pending_ai_cards_reclassify_on_read(self) -> None:
        """Legacy pending AI cards written before this policy demote on read."""
        self._insert(
            "legacy_ai_card",
            event_type="completion",
            object_label="发布流水线",
            evidence="发布流水线已完成",
            app="Xcode",
        )
        self.conn.commit()
        stored = self.conn.execute(
            "SELECT decision_question FROM activity_confirmation_items "
            "WHERE item_key='legacy_ai_card'"
        ).fetchone()
        self.assertTrue(stored["decision_question"], "legacy card must start answerable")
        # Simulate a pre-policy row whose surface was an AI terminal all along.
        self.conn.execute(
            "UPDATE activity_confirmation_items SET app_family='iTerm2' "
            "WHERE item_key='legacy_ai_card'"
        )
        self.conn.commit()

        payload = list_activity_confirmation_cards(self.conn, range_name="today")
        self.assertEqual(payload["pending_confirmation_count"], 0)
        self.assertEqual(payload["information_insufficient_count"], 1)
        self.assertEqual(payload["cards"][0]["section"], "information_insufficient")


if __name__ == "__main__":
    unittest.main()
