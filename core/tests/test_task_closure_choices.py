import tempfile
import unittest
from pathlib import Path
from activity_mvp.task_closure import Store


class ClosureChoiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name).resolve()
        self.source = self.root / "fixture.txt"; self.source.write_text("synthetic fixture")
        self.store = Store(self.root / "activity.sqlite")
        self.task = self.store.begin({"platform": "codex", "session": "fixture", "turn": "original",
                  "objective": "isolated choice contract", "scope_root": str(self.root), "complex": True})["task_id"]

    def tearDown(self): self.store.conn.close(); self.tmp.cleanup()

    def issue(self, cost=False):
        return self.store.register({"task_id": self.task, "category": "independent", "key": "fixture",
            "summary": "合成待选择事项", "evidence": [{"path": str(self.source)}],
            "recommendation": "隔离范围内修复", "acceptance_rule": "独立核验",
            "authority": {"source": "fixture only", "scope": str(self.root), "reversible": True, "cost": cost,
                          "new_permission": False, "external": False, "personal_choice": True}})["issue"]

    def test_preview_does_not_choose_and_no_elapsed_time_resolves(self):
        item = self.issue(); preview = self.store.choice_preview(item["id"])
        self.assertIsNone(preview["default"])
        self.assertEqual(self.store.get(item["id"])["state"], "decision")
        self.assertIsNone(self.store.conn.execute("SELECT answered FROM closure_choices").fetchone()[0])

    def test_fixture_explicit_choice_only_releases_personal_choice_and_preserves_cost(self):
        item = self.issue(cost=True); preview = self.store.choice_preview(item["id"])
        answer = self.store.decide({"token": preview["token"], "issue_id": item["id"], "revision": item["revision"], "choice": "accept_recommendation"})
        self.assertEqual(answer["state"], "verification")
        with self.assertRaises(ValueError): self.store.dispatch(item["id"])
        with self.assertRaises(ValueError): self.store.decide({"token": preview["token"], "issue_id": item["id"], "revision": item["revision"], "choice": "cancel"})

    def test_changed_evidence_rejects_old_displayed_choice(self):
        item = self.issue(); preview = self.store.choice_preview(item["id"])
        self.source.write_text("changed fixture")
        with self.assertRaises(ValueError): self.store.decide({"token": preview["token"], "issue_id": item["id"], "revision": item["revision"], "choice": "accept_recommendation"})
        self.assertEqual(self.store.get(item["id"])["state"], "decision")

    def test_cancel_does_not_restore_on_duplicate_registration(self):
        item = self.issue(); preview = self.store.choice_preview(item["id"])
        self.store.decide({"token": preview["token"], "issue_id": item["id"], "revision": item["revision"], "choice": "cancel"})
        self.assertEqual(self.issue()["state"], "canceled")


if __name__ == "__main__": unittest.main()
