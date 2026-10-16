"""An unreadable historical receipt cannot erase a current personal choice."""
import json
import unittest
from unittest.mock import patch

from activity_mvp import retirement_snapshot as reader
from activity_mvp import task_closure as closure
from activity_mvp import attention_native as native
from tests import test_task_closure as fixtures
from tests import test_task_closure_continuation as continuation


class ProjectionIsolationTests(unittest.TestCase):
    setUp = fixtures.ClosureTests.setUp
    tearDown = fixtures.ClosureTests.tearDown
    begin = fixtures.ClosureTests.begin
    issue = fixtures.ClosureTests.issue
    root_receipt = continuation.ContinuationTests.root_receipt
    plan = continuation.ContinuationTests.plan

    def accepted(self):
        item = self.issue(key="historical", category="related")
        return self.store.accept({"issue_id": item["id"], "revision": item["revision"],
            "verified": True, "reviewer": "independent synthetic reviewer",
            "review": "Read the synthetic source", "evidence": [{"path": str(self.source)}]})

    def choice(self):
        return self.issue(key="personal-choice", authority={"source": "Synthetic personal choice",
            "scope": str(self.root), "personal_choice": True})

    def database_rows(self):
        return {name: [tuple(row) for row in self.store.conn.execute('SELECT * FROM "' + name + '"')]
            for (name,) in self.store.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}

    def denied(self):
        return patch.object(reader._EvidenceReader, "read",
            side_effect=reader.EvidenceReadUnavailable("closure_evidence_read_unavailable"))

    def test_unknown_history_keeps_current_choice_and_entire_database(self):
        old = self.accepted(); choice = self.choice(); before = self.database_rows()
        with self.denied():
            rows = closure.projection(self.db)
            detail = closure.projection(self.db, old["id"])[0]
        indexed = {row["id"]: row for row in rows}
        history = indexed["closure:" + old["id"]]
        self.assertEqual(history["state"], "resolved")
        self.assertEqual(history["current_verification"], "unknown")
        self.assertIn("当前依据未读", history["summary"])
        self.assertNotIn("acceptance_version_changed", history["reason"])
        self.assertFalse(history["codex_handoff_available"])
        self.assertEqual(detail["current_verification"], "unknown")
        current = indexed["closure:" + choice["id"]]
        self.assertEqual(current["state"], "decision")
        self.assertNotIn("current_verification", current)
        self.assertEqual(sum(bool(row.get("requires_decision")) for row in rows), 1)
        self.assertIn("closure-evidence-read-unavailable", indexed)
        self.assertEqual(self.database_rows(), before)

    def test_actual_hash_change_still_projects_verification(self):
        old = self.accepted(); before = self.store.get(old["id"])
        self.source.write_text("A different actual synthetic version\n")
        row = closure.projection(self.db, old["id"])[0]
        self.assertEqual(row["state"], "verification")
        self.assertEqual(row["reason"], "acceptance_version_changed")
        self.assertNotIn("current_verification", row)
        self.assertEqual(self.store.get(old["id"]), before)
        self.assertFalse(self.store.task_status(self.task)["all_complete"])

    def test_other_value_errors_are_not_treated_as_unreadable_history(self):
        self.accepted(); self.choice()
        with patch.object(reader._EvidenceReader, "read", side_effect=ValueError("policy_changed")):
            with self.assertRaisesRegex(ValueError, "policy_changed"):
                closure.projection(self.db)

    def test_policy_withdrawal_still_rejects_whole_projection(self):
        self.accepted(); self.choice()
        with self.denied(), patch.object(reader.Snapshot, "unchanged", return_value=False):
            with self.assertRaisesRegex(ValueError, "policy_changed"):
                closure.projection(self.db)

    def test_recovery_read_unknown_is_never_ready_or_armed(self):
        item = self.issue(key="recovery", category="main"); self.plan(item)
        before = self.database_rows()
        with self.denied():
            row = closure.projection(self.db, item["id"])[0]
        self.assertEqual(row["recovery_status"], {"status": "unknown",
            "label": closure.RECOVERY_LABELS["unknown"], "auto_execution": False})
        self.assertEqual(row["current_verification"], "unknown")
        self.assertFalse(row["codex_handoff_available"])
        self.assertEqual(self.database_rows(), before)

    def test_unknown_cancellation_cannot_erase_entered_effects(self):
        item = self.issue(key="canceled-effects", category="main")
        self.store.conn.execute("UPDATE closure_issues SET state='canceled',dispatch_state='send_uncertain',acceptance=? WHERE id=?",
            (json.dumps({"cancellation_checked": True, "evidence": json.loads(item["evidence"])}), item["id"]))
        before = self.database_rows()
        with self.denied():
            row = closure.projection(self.db, item["id"])[0]
        self.assertEqual(row["state"], "canceled")
        self.assertTrue(row["requires_check"])
        self.assertEqual(row["current_verification"], "unknown")
        self.assertEqual(self.database_rows(), before)

    def test_read_projection_never_supplies_fresh_action_acceptance_or_status(self):
        old = self.accepted(); pending = self.issue(key="pending", category="related")
        with self.denied():
            self.assertTrue(closure.projection(self.db))
        self.source.write_text("Changed after the read, requiring fresh checks\n")
        self.assertFalse(closure.current(json.loads(old["evidence"])))
        self.assertFalse(self.store.task_status(self.task)["all_complete"])
        with patch.object(native, "AppServer") as transport:
            with self.assertRaises(ValueError):
                self.store.dispatch(pending["id"])
            transport.assert_not_called()
        with self.assertRaises(ValueError):
            self.store.accept({"issue_id": old["id"], "revision": old["revision"],
                "verified": True, "reviewer": "synthetic independent reviewer", "review": "Cannot reuse old read",
                "evidence": [{"path": str(self.source)}]})


if __name__ == "__main__":
    unittest.main()
