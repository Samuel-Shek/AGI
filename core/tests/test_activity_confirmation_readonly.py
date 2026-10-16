from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from activity_mvp.__main__ import _connect_for_command, _read_only_command
from activity_mvp.activity_confirmations import (
    activity_confirmation_revision,
    list_activity_confirmation_cards,
    list_activity_confirmations,
)
from activity_mvp.storage import connect_readonly, ensure_ready


class ActivityConfirmationReadonlyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.db = self.root / "activity.sqlite"
        conn = ensure_ready(self.db)
        now = datetime.now().astimezone().replace(microsecond=0)
        self.start = (now - timedelta(hours=1)).isoformat(timespec="seconds")
        self.end = (now + timedelta(hours=1)).isoformat(timespec="seconds")
        subject = json.dumps(
            {
                "event_label": "面试",
                "company": "示例公司",
                "round": "第一轮",
                "label": "示例公司第一轮面试",
                "company_identified": True,
                "round_identified": True,
                "object": "示例公司第一轮面试",
                "object_identified": True,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        rows = (
            ("live", "[]", "grp_live", "sig_live"),
            ("orphan", '["missing-source"]', "grp_orphan", "sig_orphan"),
        )
        for item_key, source_ids, group_key, signature in rows:
            conn.execute(
                """
                INSERT INTO activity_confirmation_items(
                  item_key,event_type,safe_observation,decision_question,why_uncertain,
                  app_family,observed_start,observed_end,confidence,reason_codes,
                  region_kind,actionability,source_activity_ids,deletion_group_ids,
                  fingerprint,calibration_key,group_key,group_signature,subject,evidence,
                  cleaned_content,clarification,state,answer,answer_reason,user_note,
                  generalize_scope,created_at,updated_at,answered_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    item_key,
                    "interview",
                    "示例公司第一轮面试已经结束",
                    "示例公司第一轮面试真的已经发生了吗？",
                    "需要确认",
                    "Example",
                    self.start,
                    self.end,
                    0.55,
                    "[]",
                    "main_content",
                    "actionable",
                    source_ids,
                    "[]",
                    f"fp_{item_key}",
                    f"cal_{item_key}",
                    group_key,
                    signature,
                    subject,
                    "示例公司第一轮面试已经结束",
                    "示例公司第一轮面试已经结束",
                    "{}",
                    "pending",
                    None,
                    None,
                    "",
                    "exact",
                    self.start,
                    self.start,
                    None,
                ),
            )
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_cli_routes_confirmation_list_to_readonly_connection(self) -> None:
        args = argparse.Namespace(
            command="activity-confirmation",
            activity_confirmation_command="list",
        )
        self.assertTrue(_read_only_command(args))

        for command in ("cards", "revision"):
            with self.subTest(command=command):
                self.assertTrue(
                    _read_only_command(
                        argparse.Namespace(
                            command="activity-confirmation",
                            activity_confirmation_command=command,
                        )
                    )
                )

    def test_list_filters_orphans_without_regrouping_committing_or_deleting(self) -> None:
        before_conn = connect_readonly(self.db)
        before = [
            tuple(row)
            for row in before_conn.execute(
                "SELECT item_key,state,group_key,group_signature,updated_at "
                "FROM activity_confirmation_items ORDER BY item_key"
            ).fetchall()
        ]
        before_conn.close()

        conn = connect_readonly(self.db)
        payload = list_activity_confirmations(
            conn,
            start=self.start,
            end=self.end,
            include_resolved=True,
        )
        self.assertEqual(conn.total_changes, 0)
        conn.close()

        self.assertEqual([item["item_key"] for item in payload["items"]], ["live"])
        after_conn = connect_readonly(self.db)
        after = [
            tuple(row)
            for row in after_conn.execute(
                "SELECT item_key,state,group_key,group_signature,updated_at "
                "FROM activity_confirmation_items ORDER BY item_key"
            ).fetchall()
        ]
        after_conn.close()
        self.assertEqual(after, before)
        self.assertEqual([row[0] for row in after], ["live", "orphan"])

    def test_card_and_revision_fast_paths_are_read_only_and_filter_orphans(self) -> None:
        conn = connect_readonly(self.db)
        cards = list_activity_confirmation_cards(
            conn,
            start=self.start,
            end=self.end,
        )
        revision = activity_confirmation_revision(
            conn,
            start=self.start,
            end=self.end,
        )
        self.assertEqual(conn.total_changes, 0)
        conn.close()

        self.assertEqual([card["group_key"] for card in cards["cards"]], ["grp_live"])
        self.assertEqual(cards["revision"], revision["revision"])
        self.assertEqual(revision["pending_question_count"], 1)

    def test_missing_confirmation_database_does_not_fabricate_empty_results(self) -> None:
        missing = self.root / "missing.sqlite"
        for command in ("list", "cards", "revision"):
            with self.subTest(command=command), self.assertRaises(FileNotFoundError):
                _connect_for_command(
                    argparse.Namespace(
                        command="activity-confirmation",
                        activity_confirmation_command=command,
                        db=str(missing),
                    )
                )


if __name__ == "__main__":
    unittest.main()
