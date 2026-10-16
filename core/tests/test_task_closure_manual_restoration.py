import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from activity_mvp import task_closure as closure


class ManualRestorationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.parent = Path(self.tmp.name).resolve()
        self.root = self.parent / "corporate-project"
        self.root.mkdir()
        self.source = self.root / "source.txt"
        self.source.write_text("fixture evidence")
        self.permits = self.parent / "permits.json"
        self.registry = self.parent / "registry.json"
        self.bridge = self.parent / "bridge.json"
        self.registry.write_text(json.dumps({"workplace": {"active": True, "status": "owner_retired", "at": "batch-1"}}))
        self.bridge.write_text(json.dumps({"retirement_active": True, "retirement": {
            "at": "batch-1", "status": "owner_retired", "policy_version": closure.personal_scope.VERSION}}))
        self.store = closure.Store(self.parent / "activity.sqlite")
        self.task_id = self.store.begin({"platform": "codex", "session": "fixture-owner",
            "turn": "original", "objective": "manual project acceptance",
            "scope_root": str(self.root), "complex": True, "closure_mode": "full"})["task_id"]
        self.task = dict(self.store.conn.execute("SELECT * FROM closure_tasks WHERE id=?", (self.task_id,)).fetchone())
        now = time.time()
        self.grant = {"enabled": True, "purpose": "manual_closure", "automatic_execution": False,
            "root": str(self.root), "task_id": self.task_id, "platform": "codex", "session": "fixture-owner",
            "retirement_at": "batch-1", "issued_at": now - 5, "expires_at": now + 3600}
        self.write_permit(self.grant)
        self.patches = [patch.object(closure, "MANUAL_CLOSURE_PERMITS", self.permits),
            patch.object(closure, "MANUAL_RETIREMENT_REGISTRY", self.registry),
            patch.object(closure, "MANUAL_RETIREMENT_BRIDGE", self.bridge),
            patch.object(closure.personal_scope, "company_stopped", return_value=True),
            patch.object(closure.personal_scope, "policy", return_value={
                "company_roots": [str(self.root)], "personal_roots": [],
                "personal_files": [], "personal_session_ids": [], "personal_source_ids": []}),
            patch.object(closure.personal_scope, "classify", return_value="company")]
        for p in self.patches: p.start()

    def tearDown(self):
        for p in reversed(self.patches): p.stop()
        self.store.conn.close()
        self.tmp.cleanup()

    def write_permit(self, grant):
        self.permits.write_text(json.dumps({"version": 1, "owner_id": "local-owner", "grants": [grant]}))
        self.permits.chmod(0o600)

    def issue(self, category="main", key="fixture"):
        return self.store.register({"task_id": self.task_id, "key": key, "category": category,
            "summary": "synthetic acceptance", "target_root": str(self.root),
            "evidence": [{"path": str(self.source)}], "authority": {"source": "fixture authorization",
            "scope": str(self.root), "reversible": True, "cost": False, "new_permission": False,
            "external": False, "personal_choice": False}, "recommendation": "verify fixture evidence",
            "acceptance_rule": "source must match"})["issue"]

    def bind_complete_audit(self):
        final = self.root / "final.md"
        text = "全部完成。人工验收通过。无需本人操作。"
        final.write_text(text)
        review = self.root / "independent.json"
        final_hash = hashlib.sha256(text.encode()).hexdigest()
        review.write_text(json.dumps({"final_response_sha256": final_hash, "wording_approved": True}))
        return self.store.audit({"task_id": self.task_id,
            "evidence": [{"path": str(self.source)}, {"path": str(final)}],
            "independent": True, "reviewer": "fixture independent reviewer",
            "independent_artifact": {"path": str(review)},
            "review": {k: "fixture inspected" for k in
                ("main", "related", "discovered", "failed_steps", "verification_gaps", "downstream")},
            "wording_approved": True, "final_response": {"path": str(final), "sha256": final_hash, "completion": "complete",
                "scope": "synthetic fixture only", "remaining": [],
                "user_action": {"required": False, "scope": "fixture acceptance", "reason": "no action"}}})

    def test_manual_permit_does_not_restore_automatic_paths(self):
        item = self.issue(category="independent")
        self.assertTrue(closure.manual_closure_allowed(self.task, item))
        self.assertFalse(closure.manual_owner_retired(self.task, item))
        self.assertTrue(closure.owner_retired(item))
        self.assertTrue(closure.automatic_retired(item))
        self.assertEqual(closure.personal_limited([item]), [])
        self.assertEqual(self.store.recovery_view(item)["status"], "inactive")
        self.assertEqual(self.store.runtime_view(item)["auto_execution"], False)
        with patch("activity_mvp.attention_native.AppServer") as transport:
            with self.assertRaisesRegex(ValueError, "owner_retired"):
                self.store.dispatch(item["id"])
            transport.assert_not_called()

    def test_real_store_audit_and_exact_wording_are_restored_only_for_original_task(self):
        item = self.issue()
        self.store.accept({"issue_id": item["id"], "revision": item["revision"], "verified": True,
            "reviewer": "fixture reviewer", "review": "source read", "evidence": [{"path": str(self.source)}]})
        status = self.bind_complete_audit()
        self.assertTrue(status["audit_valid"])
        self.assertTrue(status["wording_valid"])
        self.assertTrue(status["all_complete"])
        self.assertTrue(status["manual_restoration_active"])
        automated = self.store.task_status(self.task_id)
        self.assertFalse(automated["manual_restoration_active"])
        self.assertFalse(automated["audit_valid"])
        self.assertFalse(automated["all_complete"])
        text = (self.root / "final.md").read_text()
        self.assertTrue(self.store.check_final_response(self.task_id, text)["valid"])
        self.assertIn("native_final_text_mismatch", self.store.check_final_response(self.task_id, text + "\n")["reasons"])
        new_task = self.store.begin({"platform": "codex", "session": "fixture-owner", "turn": "other-task",
            "objective": "different task", "scope_root": str(self.root), "complex": True})["task_id"]
        other = dict(self.store.conn.execute("SELECT * FROM closure_tasks WHERE id=?", (new_task,)).fetchone())
        self.assertFalse(closure.manual_closure_allowed(other))

    def test_resolved_source_drift_still_requires_verification(self):
        item = self.issue()
        self.store.accept({"issue_id": item["id"], "revision": item["revision"], "verified": True,
            "reviewer": "fixture reviewer", "review": "source read", "evidence": [{"path": str(self.source)}]})
        self.bind_complete_audit()
        self.source.write_text("changed after acceptance")
        status = self.store.task_status(self.task_id, allow_manual=True)
        self.assertFalse(status["audit_valid"])
        self.assertFalse(status["wording_valid"])
        self.assertEqual(status["remaining"][0]["state"], "verification")

    def test_revocation_invalidates_manual_audit_without_reopening_or_resetting(self):
        item = self.issue()
        self.store.accept({"issue_id": item["id"], "revision": item["revision"], "verified": True,
            "reviewer": "fixture reviewer", "review": "source read", "evidence": [{"path": str(self.source)}]})
        self.bind_complete_audit()
        self.store.conn.execute("INSERT INTO closure_guards(guard_key,attempts) VALUES(?,1)", (self.task_id,))
        self.write_permit({**self.grant, "enabled": False})
        status = self.store.task_status(self.task_id, allow_manual=True)
        self.assertFalse(status["audit_valid"])
        self.assertFalse(status["wording_valid"])
        self.assertEqual(self.store.get(item["id"])["state"], "resolved")
        self.assertEqual(self.store.conn.execute("SELECT attempts FROM closure_guards WHERE guard_key=?", (self.task_id,)).fetchone()[0], 1)

    def test_declared_block_stays_waiting_and_decision_is_not_accepted(self):
        item = self.issue()
        item = self.store.transition(item["id"], "blocked", "original quota full", item["revision"], "wait for original quota")
        status = {"closure_required": True, "all_complete": False, "audit_valid": True,
            "independent": True, "remaining": [item]}
        self.assertEqual(self.store.end_assessment(self.task, status)["decision"], "waiting")
        self.assertEqual(self.store.get(item["id"])["state"], "blocked")
        self.store.conn.execute("UPDATE closure_issues SET state='decision' WHERE id=?", (item["id"],))
        with self.assertRaisesRegex(ValueError, "issue_not_accepting"):
            self.store.accept({"issue_id": item["id"], "revision": item["revision"]})

    def test_wrong_identity_generation_timing_and_purpose_fail_closed(self):
        changes = [{"enabled": False}, {"automatic_execution": True}, {"purpose": "dispatch"},
            {"task_id": "other"}, {"session": "other"}, {"platform": "hermes"}, {"retirement_at": "old"},
            {"issued_at": time.time() + 100}, {"expires_at": time.time() - 1},
            {"expires_at": time.time() + 49 * 3600}, {"issued_at": True}, {"expires_at": float("nan")},
            {"root": str(self.root) + "/../corporate-project"}]
        for change in changes:
            with self.subTest(change=change):
                self.write_permit({**self.grant, **change})
                self.assertFalse(closure.manual_closure_allowed(self.task))
        self.write_permit(self.grant)
        self.bridge.write_text(json.dumps({"retirement_active": True, "retirement": {
            "at": "batch-2", "status": "owner_retired", "policy_version": closure.personal_scope.VERSION}}))
        self.assertFalse(closure.manual_closure_allowed(self.task))

    def test_all_nested_paths_and_cross_task_item_are_checked(self):
        item = self.issue()
        self.assertTrue(closure.manual_closure_allowed(self.task, item))
        outside = str(self.parent / "corporate-project-sibling" / "source.txt")
        variants = [{**item, "task_id": "other"},
            {**item, "evidence": [{"path": outside}]},
            {**item, "acceptance": {"evidence": [{"path": outside}]}},
            {**item, "authority": {"scope": {"files": [outside]}}},
            {**item, "evidence": [{"path": str(self.root) + "/../outside.txt"}]}]
        for variant in variants:
            self.assertFalse(closure.manual_closure_allowed(self.task, variant))
        self.assertFalse(closure.manual_closure_allowed(self.task, item, [outside]))

    def test_untrusted_permit_and_aliased_evidence_fail_closed(self):
        self.permits.chmod(0o644)
        self.assertFalse(closure.manual_closure_allowed(self.task))
        self.permits.chmod(0o600)
        link = self.root / "alias.txt"
        link.symlink_to(self.source)
        self.assertFalse(closure.manual_closure_allowed(self.task, paths=[str(link)]))
        second = self.parent / "hardlink.json"
        os.link(self.permits, second)
        self.assertFalse(closure.manual_closure_allowed(self.task))
        second.unlink()
        self.permits.write_text("broken JSON")
        self.assertFalse(closure.manual_closure_allowed(self.task))

    def test_permission_change_during_read_is_detected(self):
        read = closure._manual_owned_json
        reads = 0
        def changing(path, **kw):
            nonlocal reads
            result = read(path, **kw)
            if path == self.permits:
                reads += 1
                if reads == 1: self.write_permit({**self.grant, "enabled": False})
            return result
        with patch.object(closure, "_manual_owned_json", side_effect=changing):
            self.assertFalse(closure.manual_closure_allowed(self.task))

    def test_missing_retirement_batch_and_duplicate_grants_are_denied(self):
        self.registry.write_text(json.dumps({"workplace": {"active": True, "status": "owner_retired"}}))
        self.bridge.write_text(json.dumps({"retirement_active": True, "retirement": {
            "status": "owner_retired", "policy_version": closure.personal_scope.VERSION}}))
        self.write_permit({k: v for k, v in self.grant.items() if k != "retirement_at"})
        self.assertFalse(closure.manual_closure_allowed(self.task))
        self.registry.write_text(json.dumps({"workplace": {"active": True, "status": "owner_retired", "at": "batch-1"}}))
        self.bridge.write_text(json.dumps({"retirement_active": True, "retirement": {
            "at": "batch-1", "status": "owner_retired", "policy_version": closure.personal_scope.VERSION}}))
        self.permits.write_text(json.dumps({"version": 1, "owner_id": "local-owner", "grants": [self.grant, self.grant]}))
        self.assertFalse(closure.manual_closure_allowed(self.task))

    def test_expiry_during_validation_is_denied(self):
        with patch.object(closure.time, "time", side_effect=[self.grant["issued_at"] + 10, self.grant["expires_at"]]):
            self.assertFalse(closure.manual_closure_allowed(self.task))

    def test_hardlinked_evidence_is_denied(self):
        second = self.root / "hardlink.txt"
        os.link(self.source, second)
        self.assertFalse(closure.manual_closure_allowed(self.task, paths=[str(self.source)]))

    def test_changed_cancellation_evidence_cannot_be_hidden_by_fresh_audit(self):
        item = self.issue()
        item = self.store.transition(item["id"], "canceled", "owner canceled fixture", item["revision"])
        evidence = closure.evidence([{ "path": str(self.source)}], self.root)
        self.store.conn.execute("UPDATE closure_issues SET dispatch_state='sent',acceptance=? WHERE id=?",
            (json.dumps({"cancellation_checked": True, "evidence": evidence}), item["id"]))
        self.source.write_text("changed after cancellation check")
        self.assertTrue(closure.manual_effects_pending(self.task, self.store.get(item["id"])))
        with self.assertRaisesRegex(ValueError, "remaining_ledger_facts_mismatch"):
            self.bind_complete_audit()
        status = self.store.task_status(self.task_id, allow_manual=True)
        self.assertFalse(status["all_complete"])
        self.assertEqual(status["remaining"][0]["state"], "canceled")
        self.assertEqual(self.store.get(item["id"])["state"], "canceled")

    def test_linked_resolved_outside_root_invalidates_manual_audit(self):
        item = self.issue()
        self.store.accept({"issue_id": item["id"], "revision": item["revision"], "verified": True,
            "reviewer": "fixture reviewer", "review": "source read", "evidence": [{"path": str(self.source)}]})
        self.bind_complete_audit()
        outside = self.parent / "outside"
        outside.mkdir(); file = outside / "evidence.txt"; file.write_text("outside")
        other = self.store.register({"task_id": self.task_id, "key": "outside", "category": "related",
            "summary": "outside synthetic item", "target_root": str(outside), "evidence": [{"path": str(file)}],
            "authority": {"source": "fixture", "scope": str(outside), "reversible": True,
                "cost": False, "new_permission": False, "external": False, "personal_choice": False},
            "recommendation": "verify", "acceptance_rule": "read evidence"})["issue"]
        self.store.accept({"issue_id": other["id"], "revision": other["revision"], "verified": True,
            "reviewer": "fixture", "review": "read outside", "evidence": [{"path": str(file)}]})
        status = self.bind_complete_audit()
        self.assertFalse(status["audit_valid"])
        self.assertFalse(status["all_complete"])

    def test_parent_directory_swap_to_alias_is_not_followed(self):
        directory = self.parent / "controls"; directory.mkdir()
        controlled = directory / "registry.json"; controlled.write_bytes(self.registry.read_bytes())
        moved = self.parent / "moved"
        opened = os.open
        swapped = False
        def swap(path, flags, *args, **kwargs):
            nonlocal swapped
            if path == "controls" and not swapped:
                directory.rename(moved); directory.symlink_to(moved, target_is_directory=True); swapped = True
            return opened(path, flags, *args, **kwargs)
        with patch.object(closure.os, "open", side_effect=swap):
            with self.assertRaises(OSError): closure._manual_owned_json(controlled)

    def test_whole_status_rechecks_expiry_after_evidence_reads(self):
        item = self.issue()
        self.store.accept({"issue_id": item["id"], "revision": item["revision"], "verified": True,
            "reviewer": "fixture", "review": "read", "evidence": [{"path": str(self.source)}]})
        self.bind_complete_audit()
        real = closure.current
        def expire(rows):
            result = real(rows)
            self.write_permit({**self.grant, "expires_at": time.time() - 1})
            return result
        with patch.object(closure, "current", side_effect=expire): status = self.store.task_status(self.task_id, allow_manual=True)
        for key in ("audit_valid", "all_complete", "wording_valid", "manual_restoration_active"):
            self.assertFalse(status[key], key)
        self.assertNotEqual(status["stop_decision"]["decision"], "complete")

    def test_automated_assessment_cannot_use_manual_permission(self):
        item = self.issue(category="independent")
        status = {"closure_required": True, "all_complete": False, "audit_valid": False,
            "independent": False, "remaining": [item]}
        result = self.store.end_assessment(self.task, status, allow_manual=False)
        self.assertNotEqual(result["decision"], "continue_now")
        self.assertIn("本人停用", result["reasons"][1]["reason"])

    def test_scope_rejection_precedes_evidence_body_reads(self):
        item = self.issue()
        self.store.accept({"issue_id": item["id"], "revision": item["revision"], "verified": True,
            "reviewer": "fixture", "review": "read", "evidence": [{"path": str(self.source)}]})
        self.bind_complete_audit()
        outside = self.parent / "outside.txt"; outside.write_text("outside")
        acceptance = json.loads(self.store.get(item["id"])["acceptance"])
        acceptance["evidence"] = [{"path": str(outside), "sha256": hashlib.sha256(outside.read_bytes()).hexdigest()}]
        self.store.conn.execute("UPDATE closure_issues SET acceptance=? WHERE id=?", (json.dumps(acceptance), item["id"]))
        with patch.object(closure, "current", side_effect=AssertionError("body read after scope rejection")):
            status = self.store.task_status(self.task_id, allow_manual=True)
        self.assertFalse(status["audit_valid"])

    def test_duplicate_control_json_keys_are_denied(self):
        self.permits.write_text('{"version":1,"owner_id":"other","owner_id":"local-owner","grants":' + json.dumps([self.grant]) + '}')
        self.assertFalse(closure.manual_closure_allowed(self.task))

    def test_foreign_personal_cancellation_is_not_read_on_scope_denial(self):
        item = self.issue()
        item = self.store.transition(item["id"], "canceled", "fixture canceled", item["revision"])
        outside = self.parent / "personal"; outside.mkdir()
        source = outside / "source.txt"; source.write_text("private fixture")
        self.store.conn.execute("UPDATE closure_issues SET target_root=?,dispatch_state='sent',acceptance=? WHERE id=?",
            (str(outside), json.dumps({"cancellation_checked": True,
             "evidence": [{"path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}]}), item["id"]))
        def classify(meta): return "company" if Path(meta["root"]).is_relative_to(self.root) else "personal"
        with patch.object(closure.personal_scope, "classify", side_effect=classify), \
                patch.object(closure, "current", side_effect=AssertionError("foreign private body read")), \
                patch.object(self.store, "runtime_view", side_effect=AssertionError("foreign runtime read")), \
                patch.object(self.store, "recovery_view", side_effect=AssertionError("foreign recovery read")):
            status = self.store.task_status(self.task_id, allow_manual=True)
        self.assertFalse(status["audit_valid"])
        self.assertFalse(status["all_complete"])
        self.assertEqual(status["remaining"][0]["state"], "canceled")

    def test_fixed_maintenance_consumer_does_not_inherit_manual_acceptance(self):
        from activity_mvp.feedback import task_maintenance
        item = self.issue()
        self.store.accept({"issue_id": item["id"], "revision": item["revision"], "verified": True,
            "reviewer": "fixture", "review": "read", "evidence": [{"path": str(self.source)}]})
        self.assertTrue(self.bind_complete_audit()["all_complete"])
        with patch.object(task_maintenance, "closure_database", return_value=self.parent / "activity.sqlite"):
            self.assertFalse(task_maintenance.retirement_eligible({"closure_tasks": [self.task_id]}, "agi", {"engineering": True}))


class ProjectManualRestorationTests(unittest.TestCase):
    tearDown = ManualRestorationTests.tearDown
    issue = ManualRestorationTests.issue
    bind_complete_audit = ManualRestorationTests.bind_complete_audit
    write_permit = ManualRestorationTests.write_permit

    def setUp(self):
        ManualRestorationTests.setUp(self)
        self.project = {"enabled": True, "purpose": "manual_project_closure",
            "automatic_execution": False, "valid_until_revoked": True,
            "root": str(self.root), "platform": "codex", "retirement_at": "batch-1",
            "issued_at": time.time() - 5, "authorization_source": "owner explicitly approved project acceptance",
            "authorization_session": "approval-session", "authorization_turn": "approval-turn"}
        self.write_project()

    def write_project(self, project=None, legacy=None):
        self.permits.write_text(json.dumps({"version": 1, "owner_id": "local-owner",
            "grants": [] if legacy is None else legacy,
            "project_grants": [self.project if project is None else project]}))
        self.permits.chmod(0o600)

    def accept_issue(self):
        item = self.issue()
        self.store.accept({"issue_id": item["id"], "revision": item["revision"], "verified": True,
            "reviewer": "fixture reviewer", "review": "source independently read",
            "evidence": [{"path": str(self.source)}]})
        return item

    def test_new_manual_task_and_session_work_without_reissuing_project_permission(self):
        self.task_id = self.store.begin({"platform": "codex", "session": "another-owner-chat",
            "turn": "new-request", "objective": "new owner requested project edit",
            "scope_root": str(self.root), "complex": True, "closure_mode": "full"})["task_id"]
        self.accept_issue()
        self.assertTrue(self.bind_complete_audit()["all_complete"])
        self.assertFalse(self.store.task_status(self.task_id)["all_complete"])
        self.assertTrue(self.store.check_final_response(self.task_id, (self.root / "final.md").read_text())["valid"])

    def test_project_decision_supersedes_expired_task_permit_and_revocation_wins(self):
        self.write_project(legacy=[{**self.grant, "expires_at": time.time() - 1}])
        self.assertTrue(closure.manual_closure_allowed(self.task))
        self.write_project({**self.project, "enabled": False}, legacy=[self.grant])
        self.assertFalse(closure.manual_closure_allowed(self.task))

    def test_project_permission_survives_48_hours_but_new_retirement_batch_invalidates_it(self):
        self.write_project({**self.project, "issued_at": time.time() - 72 * 3600})
        self.assertTrue(closure.manual_closure_allowed(self.task))
        for target in (self.registry, self.bridge):
            obj = json.loads(target.read_text())
            (obj["workplace"] if target == self.registry else obj["retirement"])["at"] = "batch-2"
            target.write_text(json.dumps(obj))
        self.assertFalse(closure.manual_closure_allowed(self.task))

    def test_project_permit_never_restores_dispatch_recovery_or_fixed_maintenance(self):
        from activity_mvp.feedback import task_maintenance
        item = self.accept_issue()
        self.assertTrue(self.bind_complete_audit()["all_complete"])
        self.assertTrue(closure.owner_retired(item))
        self.assertTrue(closure.automatic_retired(item))
        self.assertEqual(closure.personal_limited([item]), [])
        self.assertEqual(self.store.recovery_view(item)["status"], "inactive")
        with patch("activity_mvp.attention_native.AppServer") as transport:
            with self.assertRaisesRegex(ValueError, "owner_retired"):
                self.store.dispatch(item["id"])
            transport.assert_not_called()
        with patch.object(task_maintenance, "closure_database", return_value=self.parent / "activity.sqlite"):
            self.assertFalse(task_maintenance.retirement_eligible({"closure_tasks": [self.task_id]}, "agi", {"engineering": True}))

    def test_project_scope_rejects_other_project_platform_cross_task_and_aliases(self):
        self.assertFalse(closure.manual_closure_allowed({**self.task, "scope_root": str(self.parent)}))
        self.assertFalse(closure.manual_closure_allowed({**self.task, "platform": "hermes"}))
        self.write_project({**self.project, "platform": "hermes"})
        self.assertFalse(closure.manual_closure_allowed({**self.task, "platform": "hermes"}))
        self.write_project()
        item = self.issue()
        self.assertFalse(closure.manual_closure_allowed(self.task, {**item, "task_id": "different"}))
        outside = self.parent / "corporate-project-sibling"; outside.mkdir()
        self.assertFalse(closure.manual_closure_allowed(self.task, paths=[str(outside)]))
        alias = self.root / "alias"; alias.symlink_to(self.source)
        self.assertFalse(closure.manual_closure_allowed(self.task, paths=[str(alias)]))
        hard = self.root / "hard"; os.link(self.source, hard)
        self.assertFalse(closure.manual_closure_allowed(self.task, paths=[str(self.source)]))

    def test_project_grant_requires_explicit_revocable_owner_decision(self):
        changes = [{"enabled": False}, {"automatic_execution": True}, {"purpose": "dispatch"},
            {"valid_until_revoked": False}, {"issued_at": True}, {"issued_at": float("nan")},
            {"issued_at": float("inf")}, {"issued_at": time.time() + 100}, {"issued_at": 0},
            {"authorization_source": ""}, {"authorization_session": ""}, {"authorization_turn": None},
            {"retirement_at": "other"}, {"task_id": self.task_id}, {"session": "old"},
            {"expires_at": time.time() + 3600}, {"root": str(self.root) + "/../corporate-project"}]
        for change in changes:
            with self.subTest(change=change):
                self.write_project({**self.project, **change})
                self.assertFalse(closure.manual_closure_allowed(self.task))

    def test_duplicate_project_grants_and_malformed_arrays_fail_closed(self):
        obj = json.loads(self.permits.read_text())
        for grants in ([self.project, self.project], {}, "unexpected"):
            self.permits.write_text(json.dumps({**obj, "project_grants": grants}))
            self.assertFalse(closure.manual_closure_allowed(self.task))
        self.write_project()
        self.permits.chmod(0o644)
        self.assertFalse(closure.manual_closure_allowed(self.task))

    def test_revocation_and_source_drift_still_invalidate_independent_acceptance(self):
        item = self.accept_issue()
        self.assertTrue(self.bind_complete_audit()["all_complete"])
        self.store.conn.execute("INSERT INTO closure_guards(guard_key,attempts) VALUES(?,1)", (self.task_id,))
        self.write_project({**self.project, "enabled": False})
        self.assertFalse(self.store.task_status(self.task_id, allow_manual=True)["wording_valid"])
        self.assertEqual(self.store.get(item["id"])["state"], "resolved")
        self.assertEqual(self.store.conn.execute("SELECT attempts FROM closure_guards WHERE guard_key=?", (self.task_id,)).fetchone()[0], 1)
        self.write_project()
        self.source.write_text("new source version")
        status = self.store.task_status(self.task_id, allow_manual=True)
        self.assertFalse(status["all_complete"])
        self.assertEqual(status["remaining"][0]["state"], "verification")

    def test_project_does_not_release_canceled_work_or_real_decision(self):
        item = self.issue()
        canceled = self.store.transition(item["id"], "canceled", "owner canceled", item["revision"])
        self.assertEqual(self.store.get(item["id"])["state"], "canceled")
        self.assertFalse(closure.manual_effects_pending(self.task, canceled))
        self.store.conn.execute("UPDATE closure_issues SET state='decision' WHERE id=?", (item["id"],))
        with self.assertRaisesRegex(ValueError, "issue_not_accepting"):
            self.store.accept({"issue_id": item["id"], "revision": canceled["revision"]})

    def test_project_revocation_during_read_is_detected(self):
        read = closure._manual_owned_json
        reads = 0
        def changing(path, **kw):
            nonlocal reads
            result = read(path, **kw)
            if path == self.permits:
                reads += 1
                if reads == 1: self.write_project({**self.project, "enabled": False})
            return result
        with patch.object(closure, "_manual_owned_json", side_effect=changing):
            self.assertFalse(closure.manual_closure_allowed(self.task))


if __name__ == "__main__": unittest.main()
