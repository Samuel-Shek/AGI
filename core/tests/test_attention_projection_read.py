"""Only synthetic policy files and paths; no production records or choices."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from activity_mvp import task_closure as closure


class ProjectionRetirementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.company = self.root / "company"
        self.personal = self.root / "personal"
        self.company.mkdir(); self.personal.mkdir()
        self.policy = {
            "version": "personal-v1", "owner_id": "local-owner",
            "cutover_at": "2026-01-01T00:00:00+00:00",
            "company_roots": [str(self.company)],
            "personal_roots": [str(self.personal)], "personal_files": [],
            "personal_source_ids": [], "personal_session_ids": [],
        }
        (self.root / "policy.json").write_text(json.dumps(self.policy))
        (self.root / "retirements.json").write_text(json.dumps({"workplace": {"active": True}}))
        self.bridge = self.root / "bridge.json"
        self.bridge.write_text(json.dumps({"retirement_active": True}))
        for guard in (patch.object(closure.personal_scope, "ROOT", self.root),
                      patch.object(closure.personal_scope, "BRIDGE", self.bridge)):
            guard.start(); self.addCleanup(guard.stop)

    def item(self, root=None, paths=()):
        return {"target_root": str(root or self.personal),
                "evidence": [{"path": str(path)} for path in paths]}

    def test_same_policy_and_all_registered_paths_match_fresh_guard(self):
        read = closure._ProjectionRetirement()
        for item in (self.item(), self.item(self.company),
                     self.item(self.root / "company-sibling"),
                     self.item(paths=[self.company / "evidence.txt"]),
                     self.item(paths=[self.personal / "missing.txt"]),
                     {"target_root": "relative"}):
            self.assertEqual(read(item), closure.owner_retired(item))
        read.check()

    def test_policy_changed_during_construction_is_rejected(self):
        original = closure.personal_scope.policy
        def changed_policy():
            policy = original()
            self.bridge.write_text(json.dumps({"retirement_active": False}))
            return policy
        with patch.object(closure.personal_scope, "policy", side_effect=changed_policy):
            with self.assertRaisesRegex(ValueError, "policy_changed"):
                closure._ProjectionRetirement()

    def test_changed_alias_is_classified_again_inside_same_read(self):
        alias = self.root / "alias"
        alias.symlink_to(self.personal, target_is_directory=True)
        read = closure._ProjectionRetirement()
        self.assertFalse(read(self.item(alias)))
        alias.unlink(); alias.symlink_to(self.company, target_is_directory=True)
        self.assertTrue(read(self.item(alias)))

    def test_policy_change_aborts_before_using_cached_classification(self):
        read = closure._ProjectionRetirement()
        self.assertFalse(read(self.item()))
        self.policy["company_roots"].append(str(self.personal))
        (self.root / "policy.json").write_text(json.dumps(self.policy))
        with self.assertRaisesRegex(ValueError, "policy_changed"):
            read(self.item())
        self.assertTrue(closure._ProjectionRetirement()(self.item()))

    def test_company_root_alias_change_invalidates_read(self):
        alias = self.root / "company-alias"
        alias.symlink_to(self.company, target_is_directory=True)
        self.policy["company_roots"] = [str(alias)]
        (self.root / "policy.json").write_text(json.dumps(self.policy))
        read = closure._ProjectionRetirement()
        alias.unlink(); alias.symlink_to(self.personal, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "policy_changed"):
            read(self.item())

    def test_retirement_change_invalidates_read(self):
        read = closure._ProjectionRetirement()
        self.bridge.write_text(json.dumps({"retirement_active": False}))
        with self.assertRaisesRegex(ValueError, "policy_changed"):
            read(self.item())


if __name__ == "__main__":
    unittest.main()
