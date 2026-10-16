from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from activity_mvp.confirmation_evidence import (
    INTERNAL_FRAME_OWNER,
    INTERNAL_FRAME_REF,
    cleanup_uncommitted_confirmation_evidence,
    evidence_root,
    held_evidence_paths,
    promote_confirmation_evidence,
    reconcile_confirmation_evidence,
)
from activity_mvp.pipeline import ingest_events
from activity_mvp.storage import ensure_ready


PNG = b"\x89PNG\r\n\x1a\nconfirmation-evidence-test"


class ConfirmationEvidenceLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.conn = ensure_ready(self.root / "pam.sqlite")
        self.raw_root = self.root / "raw_vault"
        self.raw_root.mkdir(mode=0o700, exist_ok=True)
        self.raw_root.chmod(0o700)

    def tearDown(self) -> None:
        self.conn.close()
        self.tempdir.cleanup()

    def _event(self, text: str, captured_at: str) -> dict:
        return {
            "captured_at": captured_at,
            "display_id": "main",
            "app_name": "Obsidian",
            "bundle_id": "md.obsidian",
            "window_title": "日报",
            "ocr_text": text,
            "context_confidence": 0.9,
            "screen_region_signal": {
                "region_kind": "editor_body",
                "actionability": "actionable",
                "confidence": 0.9,
                "source": "ocr_geometry_test",
                "reason_codes": ["ocr_region_main_content"],
            },
        }

    def _new_pending(self, text: str, captured_at: str) -> dict:
        ingest_events(self.conn, [self._event(text, captured_at)])
        row = self.conn.execute(
            "SELECT * FROM activity_confirmation_items "
            "WHERE state='pending' AND safe_observation LIKE ? "
            "ORDER BY datetime(updated_at) DESC LIMIT 1",
            (f"%{text}%",),
        ).fetchone()
        self.assertIsNotNone(row)
        return dict(row)

    def _promote(self, item: dict, *, name: str, payload: bytes = PNG) -> dict:
        source_activity_id = json.loads(item["source_activity_ids"])[0]
        deletion_group_ids = json.loads(item["deletion_group_ids"])
        provenance = self.conn.execute(
            "SELECT source_id FROM activity_provenance "
            "WHERE derived_type='extracted_activity' AND derived_id=? "
            "AND source_type='frame_signal' LIMIT 1",
            (source_activity_id,),
        ).fetchone()
        self.assertIsNotNone(provenance)
        source = self.raw_root / f"{name}.png"
        source.write_bytes(payload)
        source.chmod(0o600)
        result = promote_confirmation_evidence(
            self.conn,
            item_key=item["item_key"],
            source_activity_id=source_activity_id,
            deletion_group_id=deletion_group_ids[0] if deletion_group_ids else "",
            source_frame_id=str(provenance["source_id"]),
            captured_at=item["observed_start"],
            display_id="main",
            source_path=source,
        )
        self.assertEqual(result["status"], "available")
        self.conn.commit()
        source.unlink(missing_ok=True)
        return result

    def test_ingest_rollback_immediately_removes_newly_promoted_orphan(self) -> None:
        source = self.raw_root / "rollback.png"
        source.write_bytes(PNG)
        source.chmod(0o600)
        event = {
            **self._event("参加了浩方面试", "2026-07-20T09:00:00+08:00"),
            INTERNAL_FRAME_REF: str(source),
            INTERNAL_FRAME_OWNER: "capture",
        }

        with patch(
            "activity_mvp.pipeline._insert_coverage",
            side_effect=RuntimeError("forced_after_evidence_promotion"),
        ):
            with self.assertRaisesRegex(RuntimeError, "forced_after_evidence_promotion"):
                ingest_events(self.conn, [event])

        self.assertFalse(self.conn.in_transaction)
        self.assertFalse(source.exists())
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM activity_confirmation_evidence_files"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(list(evidence_root(self.root).glob("confirm_ev_*.png")), [])

    def test_cleanup_never_deletes_a_committed_shared_file(self) -> None:
        item = self._new_pending("参加了浩方面试", "2026-07-20T09:10:00+08:00")
        promoted = self._promote(item, name="shared")
        path = evidence_root(self.root) / promoted["file_name"]

        result = cleanup_uncommitted_confirmation_evidence(
            self.conn,
            [{**promoted, "created_new": True}],
        )

        self.assertEqual(result, {"deleted": 0, "retained": 1, "failed": 0})
        self.assertTrue(path.is_file())

    def test_held_paths_reject_same_size_hash_tampering_without_mutating_db(self) -> None:
        item = self._new_pending("参加了浩方面试", "2026-07-20T09:20:00+08:00")
        promoted = self._promote(item, name="hash-tamper")
        path = evidence_root(self.root) / promoted["file_name"]
        tampered = bytearray(path.read_bytes())
        tampered[-1] ^= 0x01
        path.write_bytes(tampered)
        path.chmod(0o600)

        with held_evidence_paths(self.conn, [item["item_key"]]) as held:
            records, summary, issues = held

        self.assertEqual(records, [])
        self.assertEqual(summary["evidence_status"], "unavailable")
        self.assertEqual(issues["invalid_evidence_ids"], [promoted["evidence_id"]])
        self.assertEqual(issues["invalid_file_names"], [promoted["file_name"]])
        state = self.conn.execute(
            "SELECT state FROM activity_confirmation_evidence_files WHERE evidence_id=?",
            (promoted["evidence_id"],),
        ).fetchone()[0]
        self.assertEqual(state, "available")

    def test_held_paths_require_png_signature_even_when_hash_matches(self) -> None:
        item = self._new_pending("参加了浩方面试", "2026-07-20T09:30:00+08:00")
        promoted = self._promote(item, name="signature")
        path = evidence_root(self.root) / promoted["file_name"]
        invalid_png = b"NOT_PNG!" + path.read_bytes()[8:]
        path.write_bytes(invalid_png)
        path.chmod(0o600)
        self.conn.execute(
            "UPDATE activity_confirmation_evidence_files "
            "SET content_sha256=?,byte_size=? WHERE evidence_id=?",
            (
                hashlib.sha256(invalid_png).hexdigest(),
                len(invalid_png),
                promoted["evidence_id"],
            ),
        )
        self.conn.commit()

        with held_evidence_paths(self.conn, [item["item_key"]]) as held:
            records, summary, issues = held

        self.assertEqual(records, [])
        self.assertEqual(summary["evidence_status"], "unavailable")
        self.assertEqual(issues["invalid_evidence_ids"], [promoted["evidence_id"]])

    def test_reconcile_purges_no_active_ref_but_retains_active_shared_file(self) -> None:
        orphan_item = self._new_pending(
            "参加了浩方面试",
            "2026-07-20T09:40:00+08:00",
        )
        active_item = self._new_pending(
            "参加了小鹏面试",
            "2026-07-20T10:40:00+08:00",
        )
        orphan = self._promote(orphan_item, name="orphan")
        active = self._promote(active_item, name="active")
        orphan_path = evidence_root(self.root) / orphan["file_name"]
        active_path = evidence_root(self.root) / active["file_name"]
        self.conn.execute(
            "UPDATE activity_confirmation_evidence_refs "
            "SET released_at='2026-07-20T11:00:00+08:00',release_reason='test' "
            "WHERE evidence_id=?",
            (orphan["evidence_id"],),
        )
        self.conn.commit()

        result = reconcile_confirmation_evidence(self.conn, self.root)

        self.assertEqual(result["delete_pending_deleted"], 1)
        self.assertFalse(orphan_path.exists())
        self.assertTrue(active_path.is_file())
        states = {
            row["evidence_id"]: row["state"]
            for row in self.conn.execute(
                "SELECT evidence_id,state FROM activity_confirmation_evidence_files"
            )
        }
        self.assertEqual(states[orphan["evidence_id"]], "deleted")
        self.assertEqual(states[active["evidence_id"]], "available")


if __name__ == "__main__":
    unittest.main()
