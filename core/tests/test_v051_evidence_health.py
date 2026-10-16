from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from activity_mvp.confirmation_evidence import evidence_health
from activity_mvp.storage import ensure_ready


class V051EvidenceHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.conn = ensure_ready(self.root / "activity.sqlite")

    def tearDown(self) -> None:
        self.conn.close()
        self.tempdir.cleanup()

    def insert_item(self, key: str, state: str) -> None:
        now = "2026-08-23T00:00:00+00:00"
        self.conn.execute(
            """
            INSERT INTO activity_confirmation_items(
              item_key,event_type,safe_observation,decision_question,why_uncertain,
              observed_start,observed_end,fingerprint,calibration_key,state,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (key, "test", "safe", "question", "uncertain", now, now, key, key, state, now, now),
        )

    def insert_evidence(self, evidence_id: str, state: str, error: str = "") -> None:
        now = "2026-08-23T00:00:00+00:00"
        self.conn.execute(
            """
            INSERT INTO activity_confirmation_evidence_files(
              evidence_id,source_frame_id,captured_at,state,last_error,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (evidence_id, "frame", now, state, error, now, now),
        )

    def link(self, evidence_id: str, item_key: str, *, released: bool = False) -> None:
        now = "2026-08-23T00:00:00+00:00"
        self.conn.execute(
            """
            INSERT INTO activity_confirmation_evidence_refs(
              evidence_id,item_key,created_at,released_at
            ) VALUES(?,?,?,?)
            """,
            (evidence_id, item_key, now, now if released else None),
        )

    def test_only_unanswered_linked_evidence_is_actionable(self) -> None:
        current = "confirm_ev_00000000000000000000000000000001"
        historical = "confirm_ev_00000000000000000000000000000002"
        self.insert_item("item-current", "pending")
        self.insert_item("item-answered", "answered")
        self.insert_evidence(current, "unavailable", "source_missing")
        self.insert_evidence(historical, "unavailable", "source_missing")
        self.link(current, "item-current")
        self.link(historical, "item-answered", released=True)
        self.conn.commit()

        health = evidence_health(self.conn)

        self.assertEqual(health["contract_version"], 2)
        self.assertEqual(health["unavailable_count"], 2)
        self.assertEqual(health["actionable"]["unavailable_count"], 1)
        self.assertEqual(health["actionable"]["error_count"], 1)
        self.assertEqual(health["historical"]["unavailable_count"], 1)
        self.assertEqual(health["historical"]["error_count"], 1)
        self.assertFalse(health["raw_access"])

    def test_shared_evidence_is_actionable_when_any_unanswered_ref_remains(self) -> None:
        evidence_id = "confirm_ev_00000000000000000000000000000003"
        self.insert_item("item-open", "deferred")
        self.insert_item("item-done", "answered")
        self.insert_evidence(evidence_id, "delete_pending", "unlink_failed")
        self.link(evidence_id, "item-open")
        self.link(evidence_id, "item-done", released=True)
        self.conn.commit()

        health = evidence_health(self.conn)

        self.assertEqual(health["actionable"]["delete_pending_count"], 1)
        self.assertEqual(health["historical"]["delete_pending_count"], 0)


if __name__ == "__main__":
    unittest.main()
