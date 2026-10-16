import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from activity_mvp.task_closure import Store, authority_scope, safe_authority, _notice_state, recovery_file, _tick_locked
from activity_mvp import attention_native as native


class ClosureScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root / "input.txt"
        self.source.write_text("synthetic input")
        self.store = Store(self.root / "activity.sqlite")
        self.task = self.store.begin({"platform": "codex", "session": "fixture", "turn": "original",
            "objective": "explicit file scope", "scope_root": str(self.root), "complex": True})["task_id"]
        self.authority = {"source": "fixture file authorization", "scope": str(self.root),
            "reversible": True, "cost": False, "new_permission": False, "external": False, "personal_choice": False}

    def tearDown(self):
        self.store.conn.close()
        self.tmp.cleanup()

    def register(self, scope=None, native_context=None, **extras):
        authority = dict(self.authority)
        if scope is not None: authority["scope"] = scope
        payload = {"task_id": self.task, "key": "fixture", "category": "main", "summary": "synthetic scope check",
            "target_root": str(self.root), "evidence": [{"path": str(self.source)}], "authority": authority,
            "recommendation": "verify exact files", "acceptance_rule": "no widened write range"}
        payload.update(extras)
        return self.store.register(payload, native_context=native_context)["issue"]

    def test_legacy_directory_and_new_missing_file_are_supported(self):
        self.assertTrue(safe_authority(self.register()))
        item = self.register({"files": [str(self.root / "new.md")]}, key="file")
        self.assertEqual(item["state"], "pending")
        self.assertEqual(json.loads(item["authority"])["scope"], {"files": [str(self.root / "new.md")]})
        self.assertFalse((self.root / "new.md").exists())

    def test_prose_scope_is_technical_block_without_any_choice(self):
        item = self.register("two files only")
        self.assertEqual(item["state"], "blocked")
        self.assertIn("原负责人", item["recovery"])
        self.assertFalse(safe_authority(item))
        self.assertEqual(self.store.conn.execute("select count(*) from closure_choices").fetchone()[0], 0)
        with self.assertRaisesRegex(ValueError, "decision_not_pending"): self.store.choice_preview(item["id"])
        with self.assertRaisesRegex(ValueError, "execution_authority_invalid"):
            self.store.transition(item["id"], "pending", "cannot skip encoding", item["revision"])

    def test_invalid_paths_never_become_executable(self):
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        scopes = [{}, {"files": []}, {"files": "not a list"}, {"unknown": []},
            {"files": [str(self.root.parent / "outside.md")]},
            {"files": [str(self.root) + "/../outside.md"]},
            {"files": [str(self.root) + "/./output.md"]},
            {"files": [str(self.root) + "//output.md"]},
            {"files": [str(alias / "output.md")]}, {"files": [str(self.root)]},
            {"trees": [str(self.source)]}, {"files": [True]}, {"files": [str(self.source)] * 41}]
        for i, scope in enumerate(scopes):
            with self.subTest(scope=scope):
                item = self.register(scope, key=f"bad-{i}")
                self.assertEqual(item["state"], "blocked")
                self.assertFalse(safe_authority(item))

    def test_genuine_choice_and_payment_win_even_with_bad_scope(self):
        for flag in ("cost", "new_permission", "external", "personal_choice"):
            authority = {**self.authority, "scope": "two files only", flag: True}
            item = self.register(authority=authority, key=flag)
            self.assertEqual(item["state"], "decision")
            preview = self.store.choice_preview(item["id"])
            self.assertIsNone(preview["default"])
            with self.assertRaisesRegex(ValueError, "explicit_human_decision_required"):
                self.store.transition(item["id"], "pending", "cannot auto choose", item["revision"])

    def test_missing_or_integer_flags_are_technical_block(self):
        for flags in ({"cost": None}, {"reversible": 1}, {"external": 0}):
            item = self.register(authority={**self.authority, **flags}, key=str(flags))
            self.assertEqual(item["state"], "blocked")
            self.assertFalse(safe_authority(item))

    def test_bad_scope_cannot_dispatch_even_if_legacy_row_says_pending(self):
        item = self.register("two files only", category="independent")
        self.store.conn.execute("update closure_issues set state='pending' where id=?", (item["id"],))
        with patch.object(native, "AppServer") as transport:
            with self.assertRaisesRegex(ValueError, "dispatch_authority_missing"): self.store.dispatch(item["id"])
            transport.assert_not_called()
        self.assertEqual(self.store.get(item["id"])["dispatch_state"], "new")

    def test_scope_is_rechecked_after_filesystem_alias_change(self):
        tree = self.root / "output"
        tree.mkdir()
        item = self.register({"trees": [str(tree)]}, category="independent")
        tree.rmdir()
        tree.symlink_to(self.root.parent, target_is_directory=True)
        self.assertFalse(safe_authority(item))
        with self.assertRaisesRegex(ValueError, "dispatch_authority_missing"): self.store.dispatch(item["id"])

    def test_recovery_only_checks_declared_files_and_subtrees(self):
        tree = self.root / "out"
        tree.mkdir()
        allowed = tree / "result.md"
        allowed.write_text("synthetic output")
        item = self.register({"files": [str(self.source)], "trees": [str(tree)]})
        context = {"platform": "codex", "session": "fixture", "turn": "original", "task_id": self.task}
        with patch.object(self.store, "native_root", return_value=True):
            self.store.register_recovery({"issue_id": item["id"], "revision": item["revision"],
                "conditions": [{"kind": "file_readable", "path": str(allowed)}]}, native_context=context)
            other = self.root / "private.md"
            other.write_text("outside explicit grant")
            with self.assertRaisesRegex(ValueError, "recovery_path_invalid"):
                self.store.register_recovery({"issue_id": item["id"], "revision": item["revision"],
                    "conditions": [{"kind": "file_readable", "path": str(other)}]}, native_context=context)
        with patch("activity_mvp.task_closure.recovery_file") as read:
            self.assertEqual(self.store.recovery_conditions(item, {"conditions": json.dumps([
                {"kind": "file_readable", "path": str(other)}])}), "stale")
            read.assert_not_called()

    def test_scope_correction_is_owner_bound_and_keeps_blocked(self):
        item = self.register("two files only")
        scope = {"files": [str(self.root / "result.md")]}
        context = {"platform": "codex", "session": "fixture", "turn": "original", "task_id": self.task}
        with patch.object(self.store, "native_root", return_value=True):
            fixed = self.register(scope, native_context=context, scope_correction={"revision": item["revision"], "basis": "same original file authorization"})
        self.assertEqual(fixed["id"], item["id"])
        self.assertEqual(fixed["owner"], item["owner"])
        self.assertEqual(fixed["state"], "blocked")
        self.assertEqual(fixed["dispatch_state"], "new")
        self.assertIsNone(fixed["receipt"])
        self.assertEqual(fixed["revision"], item["revision"] + 1)
        self.assertTrue(safe_authority(fixed))
        self.assertTrue(self.store.declared_block(fixed))

    def test_scope_correction_stays_blocked_during_tick_until_owner_transition(self):
        item = self.register("two files only", category="independent")
        context = {"platform": "codex", "session": "fixture", "turn": "original", "task_id": self.task}
        with patch.object(self.store, "native_root", return_value=True):
            fixed = self.register({"files": [str(self.source)]}, category="independent", native_context=context,
                scope_correction={"revision": item["revision"], "basis": "same original authorization"})
        with patch.object(native, "AppServer") as transport, patch.object(self.store, "observe_runtime"), patch("activity_mvp.task_closure._notify_pending"):
            _tick_locked(self.store)
            transport.assert_not_called()
        self.assertEqual(self.store.get(item["id"])["state"], "blocked")
        self.assertEqual(self.store.get(item["id"])["dispatch_state"], "new")
        pending = self.store.transition(item["id"], "pending", "original owner resumes fixture", fixed["revision"])
        self.assertFalse(self.store.declared_block(pending))

    def test_hardlink_targets_are_rejected_before_recovery_reads(self):
        alias = self.root / "alias.txt"
        alias.hardlink_to(self.source)
        item = self.register({"files": [str(alias)]})
        self.assertEqual(item["state"], "blocked")
        with patch("activity_mvp.task_closure.os.read") as read:
            self.assertEqual(recovery_file({"path": str(alias)}, self.root), "stale")
            read.assert_not_called()

    def test_scope_correction_rejects_changed_provenance_revision_or_sender(self):
        item = self.register("two files only")
        scope = {"files": [str(self.source)]}
        for extras in ({"scope_correction": {"revision": 99, "basis": "fixture"}},
                       {"scope_correction": {"revision": 1, "basis": "fixture"},
                        "authority": {**self.authority, "scope": scope, "source": "changed source"}}):
            with self.assertRaisesRegex(ValueError, "scope_correction_binding_invalid"): self.register(scope, **extras)
        other = self.store.begin({"platform": "codex", "session": "other", "turn": "other",
            "objective": "other executor", "scope_root": str(self.root)})["task_id"]
        with self.assertRaisesRegex(ValueError, "scope_correction_binding_invalid"):
            self.register(scope, task_id=other, scope_correction={"revision": 1, "basis": "fixture"})
        self.assertEqual(self.store.get(item["id"])["revision"], 1)

    def test_scope_correction_does_not_answer_choice_or_restore_cancel(self):
        authority = {**self.authority, "personal_choice": True}
        item = self.register(authority=authority)
        preview = self.store.choice_preview(item["id"])
        with self.assertRaisesRegex(ValueError, "scope_correction_binding_invalid"):
            self.register({"files": [str(self.source)]}, scope_correction={"revision": 1, "basis": "fixture"})
        self.assertIsNone(self.store.conn.execute("select answered from closure_choices where token=?", (preview["token"],)).fetchone()[0])
        self.store.transition(item["id"], "canceled", "synthetic cancellation", 1)
        with self.assertRaisesRegex(ValueError, "scope_correction_binding_invalid"):
            self.register({"files": [str(self.source)]}, scope_correction={"revision": 2, "basis": "fixture"})
        self.assertEqual(self.store.get(item["id"])["state"], "canceled")

    def test_scope_correction_rejects_borrowed_identity_wrong_key_and_evidence(self):
        item = self.register("two files only")
        scope = {"files": [str(self.source)]}
        correction = {"revision": 1, "basis": "fixture original source"}
        context = {"platform": "codex", "session": "fixture", "turn": "original", "task_id": self.task}
        with patch.object(self.store, "native_root", return_value=True):
            with self.assertRaisesRegex(ValueError, "scope_correction_binding_invalid"):
                self.register(scope, scope_correction=correction)
            with self.assertRaisesRegex(ValueError, "scope_correction_issue_missing"):
                self.register(scope, key="wrong-key", native_context=context, scope_correction=correction)
            other = self.root / "other.txt"
            other.write_text("synthetic alternate evidence")
            with self.assertRaisesRegex(ValueError, "scope_correction_binding_invalid"):
                self.register(scope, evidence=[{"path": str(other)}], native_context=context, scope_correction=correction)
            self.source.write_text("synthetic changed evidence")
            with self.assertRaisesRegex(ValueError, "scope_correction_binding_invalid"):
                self.register(scope, native_context=context, scope_correction=correction)
        self.assertEqual(self.store.get(item["id"]), item)
        self.assertEqual(self.store.conn.execute("select count(*) from closure_issues").fetchone()[0], 1)

    def test_dispatch_rechecks_source_before_creation_and_first_send(self):
        for phase in ("create", "send"):
            with self.subTest(phase=phase):
                self.source.write_text("synthetic input " + phase)
                item = self.register(category="independent", key=phase)
                server = MagicMock()
                server.project.return_value = "fixture-project"
                server.active_threads.return_value = []
                server.initial_collaboration_mode.return_value = {"mode": "default"}
                def created(project, title, callback):
                    callback("00000000-0000-4000-8000-000000000003")
                    return "00000000-0000-4000-8000-000000000003"
                server.create.side_effect = created
                def mutate():
                    self.source.write_text("source changed before " + phase)
                    return "fixture-project"
                if phase == "create": server.project.side_effect = mutate
                with patch.object(native, "AppServer") as factory, patch.object(native, "open_thread"), patch.object(native, "wait_owner") as owner, patch.object(native, "DesktopIPC") as ipc:
                    factory.return_value.__enter__.return_value = server
                    owner.side_effect = mutate if phase == "send" else None
                    result = self.store.dispatch(item["id"])
                    ipc.assert_not_called()
                    if phase == "create": server.create.assert_not_called()
                self.assertEqual(result["state"], "blocked")
                self.assertEqual(result["dispatch_state"], "new" if phase == "create" else "created")

    def test_duplicate_bad_format_does_not_release_existing_choice(self):
        item = self.register(authority={**self.authority, "personal_choice": True})
        preview = self.store.choice_preview(item["id"])
        repeated = self.register("two files only")
        self.assertEqual(repeated["state"], "decision")
        self.assertIsNone(self.store.conn.execute("select answered from closure_choices where token=?", (preview["token"],)).fetchone()[0])

    def test_notification_uses_recorded_technical_state_and_legacy_defaults(self):
        self.assertEqual(_notice_state({"kind": "registered", "data": json.dumps({"state": "blocked"})}), "blocked")
        self.assertEqual(_notice_state({"kind": "authority_restricted", "data": json.dumps({"state": "blocked"})}), "blocked")
        self.assertEqual(_notice_state({"kind": "registered", "data": "{}"}), "decision")


if __name__ == "__main__": unittest.main()
