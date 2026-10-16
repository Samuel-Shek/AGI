import json
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch
from activity_mvp.task_closure import Store, projection
from activity_mvp import attention_native as native


class ClosureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.db = self.root / "activity.sqlite"
        sqlite3.connect(self.db).close()
        self.source = self.root / "scope.txt"
        self.source.write_text("isolated fixture\n")
        self.store = Store(self.db)
        self.task = self.begin()

    def tearDown(self):
        self.store.conn.close(); self.tmp.cleanup()

    def begin(self, platform="codex", turn="original"):
        return self.store.begin({"platform": platform, "session": "test-session", "turn": turn,
            "objective": "修好主任务且处理本轮发现", "scope_root": str(self.root), "complex": True})["task_id"]

    def issue(self, key="fixture", category="independent", **extra):
        value = {"task_id": self.task, "category": category, "key": key, "summary": "隔离样本支线遗漏",
                 "target_root": str(self.root), "evidence": [{"path": str(self.source)}],
                 "authority": {"source": "user-approved isolated tests", "scope": str(self.root),
                   "reversible": True, "cost": False, "new_permission": False, "external": False, "personal_choice": False},
                 "recommendation": "补齐支线并验收", "acceptance_rule": "支线证据修正且独立验收"}
        value.update(extra)
        return self.store.register(value)["issue"]

    def audit(self, independent=True):
        return self.store.audit({"task_id": self.task, "evidence": [{"path": str(self.source)}],
          "independent": independent, "reviewer": "independent fixture", "independent_artifact": {"path": str(self.source)}, "review": {key: "checked fixture" for key in
          ("main", "related", "discovered", "failed_steps", "verification_gaps", "downstream")}})

    def test_related_omission_keeps_package_partial_after_main_complete(self):
        item = self.issue(category="related")
        self.audit()
        self.assertFalse(self.store.task_status(self.task)["all_complete"])
        self.store.accept({"issue_id": item["id"], "revision": item["revision"], "verified": True,
                           "reviewer": "independent fixture", "review": "source reread passed", "evidence": [{"path": str(self.source)}]})
        self.assertTrue(self.store.task_status(self.task)["all_complete"])

    def test_five_platforms_share_same_ledger(self):
        for platform in ("codex", "claude", "hermes", "workbuddy", "openclaw"):
            task = self.begin(platform)
            self.assertTrue(self.store.task_status(task)["task_id"])
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM closure_tasks").fetchone()[0], 5)

    def test_duplicate_issue_linked_to_both_packages_and_cancel_survives_restart(self):
        item = self.issue()
        self.task = self.begin("claude")
        second = self.issue()
        self.assertEqual(item["id"], second["id"])
        self.assertEqual(len(self.store.task_status(self.task)["remaining"]), 1)
        self.store.transition(item["id"], "canceled", "user canceled fixture", 1)
        with Store(self.db) as reopened:
            self.assertEqual(reopened.get(item["id"])["state"], "canceled")
            with self.assertRaises(ValueError): reopened.dispatch(item["id"])

    def test_user_choice_never_time_resolves(self):
        item = self.issue(authority={"source": "new choice required", "scope": str(self.root), "personal_choice": True})
        self.assertEqual(item["state"], "decision")
        with self.assertRaises(ValueError): self.store.transition(item["id"], "running", "default", 1)
        with self.assertRaises(ValueError): self.store.dispatch(item["id"])

    def test_source_changed_invalidates_audit_and_blocks_old_dispatch(self):
        item = self.issue(); self.audit()
        self.source.write_text("user edited version\n")
        self.assertFalse(self.store.task_status(self.task)["audit_valid"])
        with self.assertRaises(ValueError): self.store.dispatch(item["id"])
        self.assertEqual(self.store.get(item["id"])["dispatch_state"], "new")

    def test_repair_changes_require_explicit_final_version_explanation(self):
        item = self.issue(); self.source.write_text("fixed\n")
        payload = {"issue_id": item["id"], "revision": 1, "verified": True, "reviewer": "independent",
                   "review": "checked changed result", "evidence": [{"path": str(self.source)}]}
        with self.assertRaises(ValueError): self.store.accept(payload)
        payload["source_change_explanation"] = "repair replaced isolated fixture"
        self.assertEqual(self.store.accept(payload)["state"], "resolved")

    def test_missing_acceptance_cannot_close_and_stale_revision_rejected(self):
        item = self.issue()
        with self.assertRaises(ValueError): self.store.transition(item["id"], "resolved", "child said done", 1)
        self.store.transition(item["id"], "verification", "received without proof", 1)
        with self.assertRaises(ValueError): self.store.transition(item["id"], "canceled", "stale", 1)

    def test_private_scope_and_secrets_fail_closed(self):
        with self.assertRaises(ValueError): self.issue(summary="api_key=private-fixture")
        alias = self.root / "alias.txt"; alias.symlink_to(self.source)
        with self.assertRaises(ValueError): self.issue(evidence=[{"path": str(alias)}])
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM closure_issues").fetchone()[0], 0)
        self.assertEqual(self.store.path.stat().st_mode & 0o077, 0)

    def transport(self, fail=None):
        server = MagicMock()
        thread = str(uuid.uuid4())
        server.project.return_value = "fixture-project"
        server.active_threads.return_value = []
        server.initial_collaboration_mode.return_value = {"mode": "default", "settings": {"model": "unchanged"}}
        def create(*args):
            if fail == "create": raise native.NativeError("codex_rpc_timeout")
            args[2](thread)
            if fail == "after_create": raise native.NativeError("codex_rpc_timeout")
            return thread
        server.create.side_effect = create
        server.read.return_value = {"cwd": str(self.root), "projectId": "fixture-project", "turns": []}
        return server, thread

    def test_unknown_creation_never_recreates(self):
        item = self.issue(); server, _ = self.transport("create")
        with patch.object(native, "AppServer") as factory:
            factory.return_value.__enter__.return_value = server
            first = self.store.dispatch(item["id"])
            second = self.store.dispatch(item["id"])
        self.assertEqual(server.create.call_count, 1)
        self.assertEqual(second["dispatch_state"], "creation_uncertain")
        self.assertEqual(first["state"], "blocked")

    def test_unknown_send_exact_receipt_recovers_once_without_second_send(self):
        item = self.issue(); server, thread = self.transport()
        ipc = MagicMock(); ipc.snapshot.return_value = {"source": "desktop_owner", "activity": "idle", "turn_status": "completed"}; ipc.submit.side_effect = native.NativeError("codex_ipc_timeout")
        with patch.object(native, "AppServer") as factory, patch.object(native, "DesktopIPC") as ipc_factory, patch.object(native, "open_thread"), patch.object(native, "wait_owner", return_value="owner"):
            factory.return_value.__enter__.return_value = server
            ipc_factory.return_value.__enter__.return_value = ipc
            self.store.dispatch(item["id"])
            saved = self.store.get(item["id"])
            server.read.return_value = {"cwd": str(self.root), "projectId": "fixture-project", "turns": [{"id": "turn", "status": "completed", "items": [{
                "type": "userMessage", "clientId": saved["message_id"], "id": "native-message",
                "content": [{"type": "text", "text": saved["prompt"]}]}]}]}
            recovered = self.store.reconcile(item["id"])
            with self.assertRaises(ValueError): self.store.dispatch(item["id"])
        self.assertEqual(ipc.submit.call_count, 1)
        self.assertEqual(recovered["state"], "verification")
        self.assertIsNone(recovered["acceptance"])

    def test_concurrent_target_keeps_second_undispatched(self):
        first = self.issue(); second = self.issue(key="second")
        self.store.conn.execute("UPDATE closure_issues SET dispatch_state='creation_uncertain' WHERE id=?", (first["id"],))
        with self.assertRaisesRegex(ValueError, "target_has_active_owner"):
            self.store.dispatch(second["id"])
        self.assertEqual(self.store.get(second["id"])["dispatch_state"], "new")

    def test_native_targets_do_not_change_global_or_other_instances(self):
        previous = native.PROJECT_ROOT
        one = native.AppServer(target={"root": str(self.root), "project_id": "one"})
        other = native.AppServer(target={"root": str(self.root.parent), "project_id": "other"})
        self.assertEqual(one.project_id, "one")
        self.assertNotEqual(one.project_root, other.project_root)
        self.assertEqual(native.PROJECT_ROOT, previous)

    def test_readonly_projection_does_not_change_activity_database(self):
        self.issue()
        before = self.db.read_bytes()
        values = projection(self.db)
        self.assertEqual(values[0]["kind"], "closure")
        self.assertEqual(self.db.read_bytes(), before)


if __name__ == "__main__": unittest.main()
