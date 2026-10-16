"""Independent regressions for closure races, recovery and durable visibility.

All state is synthetic and private to TemporaryDirectory. Native transport and
notifications are mocked; these checks do not certify a deployed client or UI.
"""
from __future__ import annotations

import json
import importlib.util
import os
import sqlite3
import tempfile
import unittest
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from activity_mvp import attention_native as native
from activity_mvp import task_closure as closure


class IndependentClosureReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.database = self.root / "activity.sqlite"
        sqlite3.connect(self.database).close()
        self.source = self.root / "source.txt"
        self.source.write_text("original isolated source\n")
        self.review_file = self.root / "review.txt"
        self.review_file.write_text("independent isolated review\n")
        self.independent_artifact = self.root / "independent-artifact.txt"
        self.independent_artifact.write_text("separate independent reviewer evidence\n")
        self.store = closure.Store(self.database)
        self.task_id = self.begin("original")

    def tearDown(self):
        self.store.conn.close()
        self.temp.cleanup()

    def begin(self, turn):
        return self.store.begin({
            "platform": "codex", "session": "independent-review-fixture",
            "turn": turn, "objective": "Close the isolated work package",
            "scope_root": str(self.root), "complex": True,
        })["task_id"]

    def issue(self, key="fixture", category="independent", **extra):
        value = {
            "task_id": self.task_id, "category": category, "key": key,
            "summary": "Isolated closure regression",
            "target_root": str(self.root), "project_id": "fixture-project",
            "evidence": [{"path": str(self.source)}],
            "authority": {
                "source": "Explicit synthetic regression fixture only",
                "scope": str(self.root), "reversible": True, "cost": False,
                "new_permission": False, "external": False, "personal_choice": False,
            },
            "recommendation": "Repair and independently verify this fixture",
            "acceptance_rule": "Re-read the current source and acceptance evidence",
        }
        value.update(extra)
        return self.store.register(value)["issue"]

    def audit(self):
        review = {name: "Independent fixture inspection recorded" for name in (
            "main", "related", "discovered", "failed_steps", "verification_gaps", "downstream",
        )}
        return self.store.audit({
            "task_id": self.task_id, "independent": True,
            "reviewer": "independent-closure-review-tests",
            "independent_artifact": {"path": str(self.independent_artifact)},
            "evidence": [{"path": str(self.review_file)}], "review": review,
        })

    def accept(self, issue):
        latest = self.store.get(issue["id"])
        return self.store.accept({
            "issue_id": issue["id"], "revision": latest["revision"],
            "verified": True, "reviewer": "independent-closure-review-tests",
            "review": "Current fixture read and completion condition checked",
            "evidence": [{"path": str(self.source)}],
        })

    def reopen(self):
        self.store.conn.close()
        self.store = closure.Store(self.database)

    def transport(self):
        server = MagicMock()
        thread_id = str(uuid.uuid4())
        server.project.return_value = "fixture-project"
        server.initial_collaboration_mode.return_value = {
            "mode": "default", "settings": {"model": "existing-default"},
        }
        server.read.return_value = {
            "cwd": str(self.root), "projectId": "fixture-project", "turns": [],
        }
        server.active_owners.return_value = []
        server.active_threads.return_value = []

        def create(project, title, callback):
            callback(thread_id)
            return thread_id

        server.create.side_effect = create
        ipc = MagicMock()
        ipc.submit.return_value = {"received": True}
        def desktop_snapshot(*args, **kwargs):
            turns = server.read.return_value.get("turns", [])
            status = turns[0].get("status") if turns else None
            return {"source": "desktop_owner", "activity": "idle" if status else "active",
                    "turn_status": status, "turn_id": "fixture-turn"}
        ipc.snapshot.side_effect = desktop_snapshot
        return server, ipc, thread_id

    @contextmanager
    def native_transport(self, server, ipc):
        with ExitStack() as stack:
            factory = stack.enter_context(patch.object(native, "AppServer"))
            factory.return_value.__enter__.return_value = server
            ipc_factory = stack.enter_context(patch.object(native, "DesktopIPC"))
            ipc_factory.return_value.__enter__.return_value = ipc
            opened = stack.enter_context(patch.object(native, "open_thread"))
            owner = stack.enter_context(patch.object(native, "wait_owner", return_value="fixture-owner"))
            yield factory, opened, owner

    def persisted_native_receipt(self, issue_id, status="completed", cwd=True):
        item = self.store.get(issue_id)
        value = {
            "projectId": "fixture-project", "turns": [{
                "id": "fixture-turn", "status": status, "items": [{
                    "type": "userMessage", "id": "fixture-native-message",
                    "clientId": item["message_id"],
                    "content": [{"type": "text", "text": item["prompt"]}],
                }],
            }],
        }
        if cwd:
            value["cwd"] = str(self.root)
        return value

    def attach_received_thread(self, item, state="running", updated=None):
        self.store.conn.execute(
            "UPDATE closure_issues SET thread_id=?,message_id=?,prompt=?,"
            "dispatch_state='received',state=? WHERE id=?",
            (str(uuid.uuid4()), str(uuid.uuid4()), "Synthetic persisted request", state, item["id"]),
        )
        if updated is not None:
            self.store.conn.execute("UPDATE closure_issues SET updated=? WHERE id=?", (updated, item["id"]))

    def test_ack_after_user_cancel_does_not_resurrect_and_restart_keeps_cancel(self):
        item = self.issue()
        server, ipc, _ = self.transport()

        def cancel_during_submit(*args, **kwargs):
            latest = self.store.get(item["id"])
            self.store.transition(item["id"], "canceled", "User canceled during native send", latest["revision"])
            return {"received": True}

        ipc.submit.side_effect = cancel_during_submit
        with self.native_transport(server, ipc):
            self.store.dispatch(item["id"])
        self.assertEqual(self.store.get(item["id"])["state"], "canceled")
        self.reopen()
        self.assertEqual(self.store.get(item["id"])["state"], "canceled")
        self.assertEqual(ipc.submit.call_count, 1)

    def test_ack_after_new_personal_choice_preserves_waiting_state(self):
        item = self.issue()
        server, ipc, _ = self.transport()

        def require_choice_during_submit(*args, **kwargs):
            latest = self.store.get(item["id"])
            self.store.transition(item["id"], "decision", "Actual personal choice now required", latest["revision"])
            return {"received": True}

        ipc.submit.side_effect = require_choice_during_submit
        with self.native_transport(server, ipc):
            self.store.dispatch(item["id"])
        self.assertEqual(self.store.get(item["id"])["state"], "decision")

    def test_send_error_after_new_personal_choice_preserves_waiting_state(self):
        item = self.issue()
        server, ipc, _ = self.transport()

        def require_choice_then_timeout(*args, **kwargs):
            latest = self.store.get(item["id"])
            self.store.transition(item["id"], "decision", "Actual personal choice now required", latest["revision"])
            raise native.NativeError("codex_ipc_timeout")

        ipc.submit.side_effect = require_choice_then_timeout
        with self.native_transport(server, ipc):
            self.store.dispatch(item["id"])
        self.assertEqual(self.store.get(item["id"])["state"], "decision")

    def test_choice_before_submit_prevents_first_send(self):
        item = self.issue()
        server, ipc, _ = self.transport()
        with self.native_transport(server, ipc) as (_, _, owner):
            def require_choice_while_opening(*args, **kwargs):
                latest = self.store.get(item["id"])
                self.store.transition(item["id"], "decision", "Choice required before sending", latest["revision"])
                return "fixture-owner"
            owner.side_effect = require_choice_while_opening
            self.store.dispatch(item["id"])
        self.assertEqual(self.store.get(item["id"])["state"], "decision")
        ipc.submit.assert_not_called()

    def test_manual_receipt_check_cannot_resolve_or_resume_a_choice(self):
        item = self.issue()
        self.attach_received_thread(item)
        self.store.transition(item["id"], "decision", "User decision is pending", item["revision"])
        server, ipc, _ = self.transport()
        server.read.return_value = self.persisted_native_receipt(item["id"])
        with self.native_transport(server, ipc):
            self.store.reconcile(item["id"])
        self.assertEqual(self.store.get(item["id"])["state"], "decision")
        self.assertIsNone(self.store.get(item["id"])["acceptance"])

    def test_definitely_precreation_failure_is_recoverable_without_lost_intent(self):
        item = self.issue()
        with patch.object(native, "AppServer") as factory:
            factory.return_value.__enter__.side_effect = native.NativeError("codex_missing")
            self.store.dispatch(item["id"])
        self.assertIsNone(self.store.get(item["id"])["thread_id"])
        self.reopen()
        server, ipc, thread_id = self.transport()
        with self.native_transport(server, ipc):
            self.store.dispatch(item["id"])
        self.assertEqual(server.create.call_count, 1)
        self.assertEqual(ipc.submit.call_count, 1)
        self.assertEqual(self.store.get(item["id"])["thread_id"], thread_id)

    def test_created_but_never_sent_resumes_same_chat_after_restart(self):
        item = self.issue()
        server, ipc, thread_id = self.transport()
        with self.native_transport(server, ipc) as (_, opened, _):
            opened.side_effect = OSError("Synthetic app opening failure before submit")
            self.store.dispatch(item["id"])
        self.assertEqual(self.store.get(item["id"])["thread_id"], thread_id)
        original_message = self.store.get(item["id"])["message_id"]
        ipc.submit.assert_not_called()
        self.reopen()
        with self.native_transport(server, ipc):
            self.store.dispatch(item["id"])
        self.assertEqual(server.create.call_count, 1)
        self.assertEqual(ipc.submit.call_count, 1)
        self.assertEqual(ipc.submit.call_args.args[0], thread_id)
        self.assertEqual(ipc.submit.call_args.args[2], original_message)

    def test_unknown_send_recovers_exact_receipt_without_any_second_send(self):
        item = self.issue()
        server, ipc, _ = self.transport()
        ipc.submit.side_effect = native.NativeError("codex_ipc_timeout")
        with self.native_transport(server, ipc):
            self.store.dispatch(item["id"])
        self.reopen()
        server.read.return_value = self.persisted_native_receipt(item["id"])
        with self.native_transport(server, ipc):
            result = self.store.reconcile(item["id"])
        self.assertEqual(ipc.submit.call_count, 1)
        self.assertEqual(server.create.call_count, 1)
        self.assertEqual(result["state"], "verification")
        self.assertIsNone(result["acceptance"])

    def test_saved_chat_moved_to_another_scope_is_checked_before_first_send(self):
        item = self.issue()
        server, ipc, thread_id = self.transport()
        with self.native_transport(server, ipc) as (_, opened, _):
            opened.side_effect = OSError("App unavailable before first send")
            self.store.dispatch(item["id"])
        self.assertEqual(self.store.get(item["id"])["thread_id"], thread_id)
        ipc.submit.assert_not_called()
        self.reopen()
        server.read.return_value = {
            "cwd": str(self.root.parent), "projectId": "another-project", "turns": [],
        }
        with self.native_transport(server, ipc):
            self.store.dispatch(item["id"])
        ipc.submit.assert_not_called()
        self.assertEqual(server.create.call_count, 1)
        self.assertEqual(self.store.get(item["id"])["state"], "blocked")

    def test_missing_target_directory_cannot_promote_a_receipt(self):
        item = self.issue()
        self.attach_received_thread(item)
        server, ipc, _ = self.transport()
        server.read.return_value = self.persisted_native_receipt(item["id"], cwd=False)
        with self.native_transport(server, ipc):
            with self.assertRaises((ValueError, native.NativeError)):
                self.store.reconcile(item["id"])
        self.assertNotEqual(self.store.get(item["id"])["state"], "verification")

    def test_failed_native_turn_keeps_reason_and_explicit_recovery_condition(self):
        item = self.issue()
        self.attach_received_thread(item)
        server, ipc, _ = self.transport()
        server.read.return_value = self.persisted_native_receipt(item["id"], status="failed")
        with self.native_transport(server, ipc):
            result = self.store.reconcile(item["id"])
        self.assertEqual(result["state"], "blocked")
        self.assertTrue(result["error"], "Native failure must retain a cause code")
        self.assertTrue(result["recovery"], "A blocked issue needs an explicit recovery condition")
        self.assertIsNone(result["acceptance"])

    def test_explicit_block_survives_active_and_completed_observation_after_restart(self):
        for dispatch_state, activity, status in (("received", "active", None), ("received", "idle", "completed"),
                                                ("acknowledged", "active", None), ("acknowledged", "idle", "completed")):
            with self.subTest(dispatch_state=dispatch_state, activity=activity, status=status):
                item = self.issue("explicit-block-" + dispatch_state + "-" + activity)
                self.attach_received_thread(item)
                self.store.conn.execute("UPDATE closure_issues SET dispatch_state=? WHERE id=?", (dispatch_state, item["id"]))
                blocked = self.store.transition(
                    item["id"], "blocked", "Deployment would cross the explicit external-delivery boundary",
                    item["revision"], recovery="Use a legally managed deployment without external delivery",
                )
                self.reopen()
                server, ipc, _ = self.transport()
                server.read.return_value = self.persisted_native_receipt(item["id"])
                ipc.snapshot.side_effect = None
                ipc.snapshot.return_value = {"source": "desktop_owner", "activity": activity,
                                             "turn_status": status, "turn_id": "fixture-turn",
                                             "observed_at": closure.time.time()}
                with self.native_transport(server, ipc):
                    observed = self.store.reconcile(item["id"])
                self.assertEqual(json.loads(observed["receipt"])["runtime"]["activity"], activity)
                self.assertEqual(observed["state"], "blocked")
                self.assertEqual(observed["error"], blocked["error"])
                self.assertEqual(observed["recovery"], blocked["recovery"])
                self.assertEqual(observed["revision"], blocked["revision"])
                server.create.assert_not_called()
                ipc.submit.assert_not_called()

    def test_native_failure_can_resume_when_the_actual_owner_is_active_again(self):
        item = self.issue()
        self.attach_received_thread(item)
        server, ipc, _ = self.transport()
        server.read.return_value = self.persisted_native_receipt(item["id"], status="failed")
        with self.native_transport(server, ipc):
            failed = self.store.reconcile(item["id"])
            self.assertEqual(failed["state"], "blocked")
            self.assertEqual(failed["error"], "native_turn_failed")
            ipc.snapshot.side_effect = None
            ipc.snapshot.return_value = {"source": "desktop_owner", "activity": "active",
                                         "turn_status": None, "turn_id": "fixture-turn"}
            resumed = self.store.reconcile(item["id"])
        self.assertEqual(resumed["state"], "running")
        self.assertIsNone(resumed["error"])
        self.assertIsNone(resumed["recovery"])
        self.assertIsNone(resumed["acceptance"])

    def test_explicit_resume_releases_block_for_subsequent_native_observation(self):
        item = self.issue()
        self.attach_received_thread(item)
        blocked = self.store.transition(item["id"], "blocked", "Explicit isolated deployment hold",
                                        item["revision"], recovery="Wait for the current owner to restore safe scope")
        self.store.transition(item["id"], "pending", "Current owner explicitly restored a safe scope", blocked["revision"])
        server, ipc, _ = self.transport()
        server.read.return_value = self.persisted_native_receipt(item["id"])
        ipc.snapshot.side_effect = None
        with self.native_transport(server, ipc):
            ipc.snapshot.return_value = {"source": "desktop_owner", "activity": "active",
                                         "turn_status": None, "turn_id": "fixture-turn"}
            self.assertEqual(self.store.reconcile(item["id"])["state"], "running")
            ipc.snapshot.return_value = {"source": "desktop_owner", "activity": "idle",
                                         "turn_status": "completed", "turn_id": "fixture-turn"}
            observed = self.store.reconcile(item["id"])
        self.assertEqual(observed["state"], "verification")
        self.assertIsNone(observed["error"])
        self.assertIsNone(observed["recovery"])
        self.assertIsNone(observed["acceptance"])

    def test_offline_interrupted_status_does_not_override_completed_desktop_owner(self):
        item = self.issue()
        self.attach_received_thread(item)
        server, ipc, _ = self.transport()
        server.read.return_value = self.persisted_native_receipt(item["id"], status="interrupted")
        ipc.snapshot.side_effect = None
        ipc.snapshot.return_value = {"source": "desktop_owner", "activity": "idle",
                                     "turn_status": "completed", "turn_id": "fixture-turn"}
        with self.native_transport(server, ipc):
            result = self.store.reconcile(item["id"])
        self.assertEqual(result["state"], "verification")
        self.assertIsNone(result["acceptance"])

    def test_completed_offline_history_cannot_certify_unknown_live_execution(self):
        item = self.issue()
        self.attach_received_thread(item)
        server, ipc, _ = self.transport()
        server.read.return_value = self.persisted_native_receipt(item["id"])
        ipc.snapshot.side_effect = native.NativeError("codex_not_ready")
        with self.native_transport(server, ipc):
            result = self.store.reconcile(item["id"])
        self.assertEqual(result["state"], "running")
        self.assertIsNone(result["acceptance"])

    def test_live_snapshot_failure_cannot_leave_previous_active_receipt_as_latest(self):
        item = self.issue()
        self.attach_received_thread(item)
        self.store.conn.execute("UPDATE closure_issues SET dispatch_state='acknowledged' WHERE id=?", (item["id"],))
        server, ipc, _ = self.transport()
        server.read.return_value = self.persisted_native_receipt(item["id"], status="interrupted")
        ipc.snapshot.side_effect = None
        ipc.snapshot.return_value = {"source": "desktop_owner", "activity": "active",
                                     "turn_status": None, "observed_at": 1.0}
        with self.native_transport(server, ipc):
            initial = self.store.reconcile(item["id"])
            self.assertEqual(initial["state"], "running")
            ipc.snapshot.side_effect = native.NativeError("codex_not_ready")
            unknown = self.store.reconcile(item["id"])
        runtime = json.loads(unknown["receipt"])["runtime"]
        self.assertEqual(runtime["activity"], "unknown")
        self.assertEqual(runtime["reason"], "codex_not_ready")
        self.assertIsNone(unknown["acceptance"])

    def test_offline_interrupted_history_does_not_override_active_owner(self):
        item = self.issue()
        self.attach_received_thread(item)
        server, ipc, _ = self.transport()
        server.read.return_value = self.persisted_native_receipt(item["id"], status="interrupted")
        ipc.snapshot.side_effect = None
        ipc.snapshot.return_value = {"source": "desktop_owner", "activity": "active",
                                     "turn_status": None, "turn_id": "fixture-turn"}
        with self.native_transport(server, ipc):
            result = self.store.reconcile(item["id"])
        self.assertEqual(result["state"], "running")
        self.assertIsNone(result["acceptance"])

    def test_new_source_invalidates_acceptance_even_when_audit_file_unchanged(self):
        item = self.issue(category="related")
        self.accept(item)
        self.audit()
        self.assertTrue(self.store.task_status(self.task_id)["all_complete"])
        self.source.write_text("new source after acceptance\n")
        state = self.store.task_status(self.task_id)
        self.assertFalse(state["all_complete"])
        self.assertEqual(len(state["remaining"]), 1)
        display = next(r for r in closure.projection(self.database) if r["id"] == "closure:" + item["id"])
        self.assertEqual(display["state"], "verification")

    def test_changed_independent_artifact_invalidates_an_unchanged_audit(self):
        self.audit()
        self.assertTrue(self.store.task_status(self.task_id)["all_complete"])
        self.independent_artifact.write_text("replacement independent review\n")
        state = self.store.task_status(self.task_id)
        self.assertFalse(state["audit_valid"])
        self.assertFalse(state["all_complete"])

    def test_source_change_and_duplicate_registration_do_not_restore_cancel(self):
        item = self.issue()
        self.store.transition(item["id"], "canceled", "User canceled this finding", item["revision"])
        self.source.write_text("later source version\n")
        self.task_id = self.begin("later-task-after-cancel")
        duplicate = self.issue()
        self.assertEqual(duplicate["id"], item["id"])
        self.assertEqual(duplicate["state"], "canceled")
        self.assertIsNone(duplicate["thread_id"])

    def test_duplicate_finding_cannot_reuse_permission_after_new_user_restriction(self):
        item = self.issue()
        self.task_id = self.begin("new-restricted-human-turn")
        authority = json.loads(item["authority"])
        authority["source"] = "New explicit constraint: payment requires a real user decision"
        authority["cost"] = True
        duplicate = self.issue(authority=authority)
        self.assertEqual(duplicate["id"], item["id"])
        self.assertEqual(duplicate["state"], "decision")
        with patch.object(native, "AppServer") as factory:
            try:
                self.store.dispatch(item["id"])
            except ValueError:
                pass
        factory.assert_not_called()

    def test_choice_token_cannot_decide_a_different_displayed_issue(self):
        first = self.issue("choice-first")
        second = self.issue("choice-second")
        self.store.transition(first["id"], "decision", "First personal choice", first["revision"])
        self.store.transition(second["id"], "decision", "Second personal choice", second["revision"])
        preview = self.store.choice_preview(first["id"])
        latest_second = self.store.get(second["id"])
        with self.assertRaises(ValueError):
            self.store.decide({"token": preview["token"], "choice": "accept_recommendation",
                               "issue_id": second["id"], "revision": latest_second["revision"]})
        self.assertEqual(self.store.get(first["id"])["state"], "decision")
        self.assertEqual(self.store.get(second["id"])["state"], "decision")

    def test_choice_token_rejects_a_different_explicit_displayed_revision(self):
        item = self.issue("choice-version")
        self.store.transition(item["id"], "decision", "Personal choice", item["revision"])
        preview = self.store.choice_preview(item["id"])
        with self.assertRaises(ValueError):
            self.store.decide({"token": preview["token"], "choice": "accept_recommendation",
                               "issue_id": item["id"], "revision": preview["revision"] + 1})
        self.assertEqual(self.store.get(item["id"])["state"], "decision")

    def test_recurrence_reuses_original_chat_and_requires_new_acceptance(self):
        item = self.issue()
        self.attach_received_thread(item)
        original_thread = self.store.get(item["id"])["thread_id"]
        self.accept(item)
        self.source.write_text("changed source for recurrence\n")
        self.task_id = self.begin("new-human-turn")
        duplicate = self.issue()
        self.assertEqual(duplicate["id"], item["id"])
        self.assertEqual(duplicate["thread_id"], original_thread)
        self.assertNotEqual(duplicate["state"], "resolved")
        self.assertEqual(len(self.store.task_status(self.task_id)["remaining"]), 1)

    def test_recent_history_cannot_hide_older_unfinished_or_stale_resolved(self):
        pending = self.issue("old-pending", category="related")
        stale = self.issue("old-accepted", category="related")
        self.accept(stale)
        for index in range(210):
            historical = self.issue("history-" + str(index), category="related")
            self.store.transition(historical["id"], "dismissed", "Isolated finding ruled out", historical["revision"])
        self.source.write_text("new version invalidates old accepted fixture\n")
        visible = {r["id"]: r for r in closure.projection(self.database)}
        self.assertIn("closure:" + pending["id"], visible)
        self.assertIn("closure:" + stale["id"], visible)
        self.assertEqual(visible["closure:" + stale["id"]]["state"], "verification")

    def test_bounded_background_checks_rotate_without_changing_business_state(self):
        items = [self.issue("round-robin-" + str(i)) for i in range(8)]
        for index, item in enumerate(items):
            self.attach_received_thread(item, updated=float(index + 1))
        visited = []
        def unchanged_receipt(issue_id):
            visited.append(issue_id)
            return self.store.get(issue_id)
        with patch.object(self.store, "reconcile", side_effect=unchanged_receipt), patch.object(closure.subprocess, "run"):
            for _ in range(3):
                closure._tick_locked(self.store)
        self.assertEqual(set(visited), {item["id"] for item in items})
        self.assertTrue(all(self.store.get(item["id"])["state"] == "running" for item in items))

    def test_existing_related_executor_blocks_an_independent_parallel_writer(self):
        first = self.issue("current-related-writer", category="related")
        self.store.transition(first["id"], "running", "Current executor is modifying this scope", first["revision"])
        self.task_id = self.begin("independent-followup-turn")
        second = self.issue("independent-writer")
        server, ipc, _ = self.transport()
        with self.native_transport(server, ipc):
            try:
                self.store.dispatch(second["id"])
            except ValueError:
                pass
        server.create.assert_not_called()
        ipc.submit.assert_not_called()

    def closure_hook(self):
        skill_root = os.environ.get("TASK_CLOSURE_SKILL_ROOT")
        if not skill_root:
            self.skipTest("Explicit shared-skill path required for cross-repository hook checks")
        path = Path(skill_root) / "scripts" / "closure_hook.py"
        self.assertTrue(path.is_file(), "The explicitly selected closure hook must exist")
        spec = importlib.util.spec_from_file_location("independent_closure_hook_fixture", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.engine = lambda: (closure, self.database)
        return module

    def test_hook_missing_begin_after_edit_corrects_once_and_preserves_original_turn(self):
        hook = self.closure_hook()
        session = "native-hook-fixture"
        prompt = {"session_id": session, "turn_id": "human-original", "prompt": "Repair isolated fixture"}
        hook.run(prompt, "codex", "prompt")
        hook.run({"session_id": session, "tool_name": "functions.apply_patch"}, "codex", "tool")
        original = self.store.conn.execute("SELECT task_id,turn FROM closure_hook_roots WHERE session=?", (session,)).fetchone()
        self.assertIsNone(self.store.conn.execute("SELECT id FROM closure_tasks WHERE id=?", (original["task_id"],)).fetchone())
        correction = hook.run({"session_id": session}, "codex", "stop")
        self.assertTrue(correction["block"])
        hook.run({"session_id": session, "turn_id": "automatic-correction", "prompt": correction["reason"]}, "codex", "prompt")
        after = self.store.conn.execute("SELECT task_id,turn FROM closure_hook_roots WHERE session=?", (session,)).fetchone()
        self.assertEqual(tuple(after), tuple(original))
        second = hook.run({"session_id": session}, "codex", "stop")
        self.assertFalse(second.get("block", False))
        self.assertTrue(second.get("gap"))

    def test_hook_readonly_command_does_not_create_a_complex_task_or_corrective_loop(self):
        hook = self.closure_hook()
        session = "native-readonly-fixture"
        before = self.store.conn.execute("SELECT count(*) FROM closure_tasks").fetchone()[0]
        hook.run({"session_id": session, "turn_id": "simple-human-turn", "prompt": "Read current fixture state"}, "codex", "prompt")
        hook.run({"session_id": session, "tool_name": "functions.exec_command"}, "codex", "tool")
        self.assertEqual(hook.run({"session_id": session}, "codex", "stop"), {})
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM closure_tasks").fetchone()[0], before)

    def test_hook_registered_canceled_child_cannot_continue_tools(self):
        hook = self.closure_hook()
        item = self.issue()
        self.attach_received_thread(item)
        thread_id = self.store.get(item["id"])["thread_id"]
        self.store.transition(item["id"], "canceled", "Explicit isolated cancellation", item["revision"])
        response = hook.run({"session_id": thread_id, "tool_name": "functions.apply_patch"}, "codex", "pre")
        self.assertTrue(response["deny"])

    def cancel_received(self, state="canceled"):
        item = self.issue()
        self.attach_received_thread(item)
        latest = self.store.get(item["id"])
        return self.store.transition(item["id"], state, "Explicit isolated cancellation or dismissal", latest["revision"])

    def cancellation_payload(self, item):
        latest = self.store.get(item["id"])
        return {"issue_id": item["id"], "revision": latest["revision"],
                "verified": True, "reviewer": "independent-closure-review-tests",
                "review": "Re-read the actual final fixture and exact original stopped turn",
                "evidence": [{"path": str(self.source)}]}

    def stopped_snapshot(self, *, turn_id="fixture-turn", age=0, activity="idle", source="desktop_owner"):
        return {"source": source, "activity": activity, "turn_status": "completed",
                "turn_id": turn_id, "observed_at": closure.time.time() - age}

    def test_entered_canceled_effects_stay_visible_and_incomplete_after_restart(self):
        item = self.cancel_received()
        self.audit()
        self.reopen()
        status = self.store.task_status(self.task_id)
        self.assertFalse(status["all_complete"])
        self.assertEqual([r["id"] for r in status["remaining"]], [item["id"]])
        for index in range(110):
            historical = self.issue("later-canceled-history-" + str(index), category="related")
            self.store.transition(historical["id"], "dismissed", "Isolated historical finding", historical["revision"])
        projected = [r for r in closure.projection(self.database) if r["id"] == "closure:" + item["id"]]
        self.assertEqual(len(projected), 1, "Pending canceled effects must not be hidden or duplicated by history")
        self.assertEqual(projected[0]["state"], "canceled")
        self.assertTrue(projected[0]["requires_check"])

    def test_cancellation_preserves_ambiguous_creation_without_creating_again(self):
        item = self.issue()
        self.store.conn.execute("UPDATE closure_issues SET dispatch_state='creation_uncertain' WHERE id=?", (item["id"],))
        item = self.store.transition(item["id"], "canceled", "Creation outcome unknown when canceled", item["revision"])
        server, ipc, _ = self.transport()
        with self.native_transport(server, ipc), patch.object(closure.subprocess, "run"):
            closure._tick_locked(self.store)
            with self.assertRaisesRegex(ValueError, "canceled_effects_not_verified_stopped"):
                self.store.cancellation_check(self.cancellation_payload(item))
        self.assertEqual(self.store.get(item["id"])["state"], "canceled")
        self.assertTrue(closure.effects_pending(self.store.get(item["id"])))
        server.create.assert_not_called()
        ipc.submit.assert_not_called()

    def test_canceled_live_execution_is_checked_without_revival_or_dispatch(self):
        item = self.cancel_received()
        server, ipc, _ = self.transport()
        server.read.return_value = self.persisted_native_receipt(item["id"], status="interrupted")
        ipc.snapshot.side_effect = None
        ipc.snapshot.return_value = self.stopped_snapshot(activity="active")
        before = self.store.get(item["id"])["revision"]
        with self.native_transport(server, ipc), patch.object(closure.subprocess, "run"):
            closure._tick_locked(self.store)
            with self.assertRaisesRegex(ValueError, "canceled_effects_not_verified_stopped"):
                self.store.cancellation_check(self.cancellation_payload(item))
        latest = self.store.get(item["id"])
        self.assertEqual(latest["state"], "canceled")
        self.assertEqual(latest["revision"], before, "Read-only checks cannot revive or alter business cancellation")
        self.assertTrue(closure.effects_pending(latest))
        server.read.assert_called()
        ipc.snapshot.assert_called()
        server.create.assert_not_called()
        ipc.submit.assert_not_called()

    def test_cancellation_check_rejects_unknown_old_or_wrong_stopping_receipt(self):
        item = self.cancel_received()
        server, ipc, _ = self.transport()
        server.read.return_value = self.persisted_native_receipt(item["id"])
        ipc.snapshot.side_effect = None
        for live in ({"activity": "unknown", "observed_at": closure.time.time()},
                     self.stopped_snapshot(age=60),
                     self.stopped_snapshot(age=-60),
                     self.stopped_snapshot(turn_id="another-turn"),
                     self.stopped_snapshot(source="offline_history")):
            with self.subTest(live=live), self.native_transport(server, ipc):
                ipc.snapshot.return_value = live
                with self.assertRaisesRegex(ValueError, "canceled_effects_not_verified_stopped"):
                    self.store.cancellation_check(self.cancellation_payload(item))
            self.assertIsNone(self.store.get(item["id"])["acceptance"])
        server.create.assert_not_called()
        ipc.submit.assert_not_called()

    def test_cancellation_check_requires_current_exact_message_before_using_saved_stop(self):
        item = self.cancel_received()
        old = {"turn_id": "fixture-turn", "runtime": self.stopped_snapshot()}
        self.store.conn.execute("UPDATE closure_issues SET receipt=? WHERE id=?", (json.dumps(old), item["id"]))
        server, ipc, _ = self.transport()
        server.read.return_value = {"cwd": str(self.root), "projectId": "fixture-project", "turns": []}
        with self.native_transport(server, ipc):
            with self.assertRaises(ValueError):
                self.store.cancellation_check(self.cancellation_payload(item))
        self.assertTrue(closure.effects_pending(self.store.get(item["id"])))
        self.assertIsNone(self.store.get(item["id"])["acceptance"])
        ipc.snapshot.assert_not_called()

    def test_verified_cancel_becomes_history_and_changed_evidence_reopens_check_only(self):
        for state in ("canceled", "dismissed"):
            with self.subTest(state=state):
                item = self.issue("verified-" + state)
                self.attach_received_thread(item)
                item = self.store.transition(item["id"], state, "Explicit isolated terminal choice", item["revision"])
                server, ipc, _ = self.transport()
                server.read.return_value = self.persisted_native_receipt(item["id"])
                ipc.snapshot.side_effect = lambda *args, **kwargs: self.stopped_snapshot()
                with self.native_transport(server, ipc):
                    checked = self.store.cancellation_check(self.cancellation_payload(item))
                self.assertEqual(checked["state"], state)
                self.assertFalse(closure.effects_pending(checked))
                acceptance = json.loads(checked["acceptance"])
                self.assertTrue(acceptance["cancellation_checked"])
                self.assertEqual(acceptance["stop_receipt"]["turn_id"], "fixture-turn")
                self.audit()
                self.assertTrue(self.store.task_status(self.task_id)["all_complete"])
                projected = next(r for r in closure.projection(self.database) if r["id"] == "closure:" + item["id"])
                self.assertEqual(projected["state"], state)
                self.assertFalse(projected["requires_check"])
                with patch.object(self.store, "reconcile") as readback, patch.object(self.store, "dispatch") as dispatch, patch.object(closure.subprocess, "run"):
                    closure._tick_locked(self.store)
                readback.assert_not_called()
                dispatch.assert_not_called()
                server.create.assert_not_called()
                ipc.submit.assert_not_called()
        self.source.write_text("new source after canceled effects were reviewed\n")
        self.reopen()
        with patch.object(self.store, "reconcile", side_effect=self.store.get) as readback, patch.object(self.store, "dispatch") as dispatch, patch.object(closure.subprocess, "run"):
            closure._tick_locked(self.store)
        self.assertEqual(readback.call_count, 2, "Changed cancellation evidence must resume read-only checks")
        dispatch.assert_not_called()
        status = self.store.task_status(self.task_id)
        self.assertFalse(status["all_complete"])
        self.assertEqual({r["state"] for r in status["remaining"]}, {"canceled", "dismissed"})
        self.assertTrue(all(r["acceptance"] is None for r in status["remaining"]))
        visible = [r for r in closure.projection(self.database) if r["requires_check"]]
        self.assertEqual({r["state"] for r in visible}, {"canceled", "dismissed"})

    def test_cancellation_check_rejects_stale_revision_and_changed_final_file(self):
        item = self.cancel_received()
        server, ipc, _ = self.transport()
        server.read.return_value = self.persisted_native_receipt(item["id"])
        ipc.snapshot.side_effect = lambda *args, **kwargs: self.stopped_snapshot()
        payload = self.cancellation_payload(item)
        payload["revision"] -= 1
        with self.native_transport(server, ipc):
            with self.assertRaisesRegex(ValueError, "revision_changed"):
                self.store.cancellation_check(payload)
        server.read.assert_not_called()
        payload = self.cancellation_payload(item)
        payload["evidence"] = closure.evidence(payload["evidence"], self.root)
        self.source.write_text("changed after cancellation review readback\n")
        with self.native_transport(server, ipc):
            with self.assertRaisesRegex(ValueError, "evidence_version_changed"):
                self.store.cancellation_check(payload)
        self.assertTrue(closure.effects_pending(self.store.get(item["id"])))
        self.assertIsNone(self.store.get(item["id"])["acceptance"])

    def test_pending_canceled_effects_keep_target_ownership_before_another_writer(self):
        first = self.cancel_received()
        self.task_id = self.begin("another-writer-after-cancel")
        second = self.issue("another-independent-writer")
        server, ipc, _ = self.transport()
        with self.native_transport(server, ipc):
            try:
                self.store.dispatch(second["id"])
            except ValueError:
                pass
        self.assertTrue(closure.effects_pending(self.store.get(first["id"])))
        server.create.assert_not_called()
        ipc.submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
