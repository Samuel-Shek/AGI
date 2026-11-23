from tests.closure_legacy_fixture import legacy_begin
import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from activity_mvp import task_closure as closure


class ClosureNotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.database = self.root / "activity.sqlite"
        sqlite3.connect(self.database).close()
        self.source = self.root / "fixture.txt"
        self.source.write_text("notification isolation fixture")
        self.store = closure.Store(self.database)
        self.task = legacy_begin(self.store, {"platform": "codex", "session": "notification-fixture",
            "turn": "one", "objective": "isolated notification regression", "scope_root": str(self.root),
            "complex": True})["task_id"]

    def tearDown(self):
        self.store.conn.close()
        self.temp.cleanup()

    def issue(self, key, choice=False):
        return self.store.register({"task_id": self.task, "category": "main", "key": key,
            "summary": "本机私密摘要不得进入通知 " + key, "target_root": str(self.root),
            "evidence": [{"path": str(self.source)}],
            "authority": {"source": "isolated fixture authorization", "scope": str(self.root),
                "reversible": True, "cost": False, "new_permission": False,
                "external": False, "personal_choice": choice},
            "recommendation": "fixture", "acceptance_rule": "fixture"})["issue"]

    def block(self, item, reason="同一限制", state="blocked", actionable=True):
        if state == "blocked" and actionable:
            self.store.conn.execute("UPDATE closure_issues SET dispatch_state='send_uncertain' WHERE id=?", (item["id"],))
        return self.store.transition(item["id"], state, reason, item["revision"],
            "fixture recovery" if state == "blocked" else None)

    def tick(self, result=None, error=None):
        with patch.object(closure.subprocess, "run", return_value=result or subprocess.CompletedProcess([], 0),
                          side_effect=error) as sender:
            closure._tick_locked(self.store)
        return sender

    def notices(self):
        return [tuple(row) for row in self.store.conn.execute(
            "SELECT event_seq,requested,error FROM closure_notifications ORDER BY event_seq")]

    def test_many_events_produce_one_digest_for_unique_items(self):
        for index in range(12): self.block(self.issue(str(index)))
        sender = self.tick()
        self.assertEqual(sender.call_count, 1)
        script = sender.call_args.args[0][-1]
        self.assertIn("12项受阻", script)
        self.assertEqual(sum(row[1] for row in self.notices()), 12)
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM closure_issues").fetchone()[0], 12)

    def test_unchanged_block_keeps_event_revision_but_no_new_notice(self):
        item = self.block(self.issue("same")); self.tick()
        before = len(self.notices())
        item = self.block(item, reason="补充恢复说明")
        self.assertEqual(len(self.notices()), before)
        self.assertEqual(item["revision"], 3)
        self.assertEqual(item["error"], "补充恢复说明")
        self.assertEqual(self.tick().call_count, 0)

    def test_digest_counts_unique_items_and_keeps_choice_waiting(self):
        item = self.issue("decision", choice=True)
        # An old duplicated request remains traceable but counts only once.
        self.store.event("authority_restricted", {"authority": {}}, issue=item["id"], task=self.task, notify=True)
        self.block(self.issue("blocked"))
        sender = self.tick()
        self.assertEqual(sender.call_count, 1)
        self.assertIn("1项待你决定、1项受阻", sender.call_args.args[0][-1])
        self.assertEqual(self.store.get(item["id"])["state"], "decision")
        self.assertEqual(sum(row[1] for row in self.notices()), 3)

    def test_private_task_text_and_paths_never_enter_notification(self):
        self.block(self.issue("customer-project"))
        script = self.tick().call_args.args[0][-1]
        self.assertNotIn("customer-project", script)
        self.assertNotIn("私密摘要", script)
        self.assertNotIn(str(self.root), script)
        self.assertNotIn("打开 PSI", script)
        self.assertIn("未重发", script)
        self.assertIn("原事项", script)

    def test_cooldown_defers_without_mutating_request_then_sends(self):
        with patch.object(closure.time, "time", return_value=1000):
            self.block(self.issue("first")); self.tick()
        with patch.object(closure.time, "time", return_value=1100):
            self.block(self.issue("later")); before = self.notices()
            self.assertEqual(self.tick().call_count, 0)
            self.assertEqual(self.notices(), before)
        with patch.object(closure.time, "time", return_value=1901):
            self.assertEqual(self.tick().call_count, 1)
        self.assertTrue(all(row[1] for row in self.notices()))

    def test_new_block_episode_after_recovery_is_not_suppressed(self):
        with patch.object(closure.time, "time", return_value=1000):
            item = self.block(self.issue("episode")); self.tick()
        item = self.block(item, state="running")
        item = self.block(item)
        with patch.object(closure.time, "time", return_value=2000):
            sender = self.tick()
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(self.store.get(item["id"])["state"], "blocked")

    def test_obsolete_block_is_not_sent_after_acceptance(self):
        item = self.block(self.issue("resolved"))
        self.store.accept({"issue_id": item["id"], "revision": item["revision"], "verified": True,
            "reviewer": "isolated reviewer", "review": "read fixture", "evidence": [{"path": str(self.source)}]})
        self.assertEqual(self.tick().call_count, 0)
        rows = self.notices()
        self.assertEqual(rows[0][1:], (0, "notification_obsolete"))
        self.assertEqual(rows[1][1:], (0, "notification_obsolete"))

    def test_entire_batch_unknown_before_subprocess_and_never_retried(self):
        for index in range(3): self.block(self.issue(str(index)))
        def timeout(*args, **kwargs):
            self.assertEqual([row[2] for row in self.notices()], ["request_uncertain"] * 3)
            raise subprocess.TimeoutExpired(args[0], 8)
        sender = self.tick(error=timeout)
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(self.tick().call_count, 0)
        self.assertTrue(all(row[1:] == (0, "request_uncertain") for row in self.notices()))

    def test_rejection_keeps_all_failed_requests_and_no_retry(self):
        for index in range(3): self.block(self.issue(str(index)))
        self.assertEqual(self.tick(result=subprocess.CompletedProcess([], 1)).call_count, 1)
        self.assertTrue(all(row[1:] == (0, "notification_rejected") for row in self.notices()))
        self.assertEqual(self.tick().call_count, 0)

    def test_existing_requested_and_unknown_history_untouched(self):
        for index in range(3): self.block(self.issue(str(index)))
        rows = self.notices()
        self.store.conn.execute("UPDATE closure_notifications SET requested=1 WHERE event_seq=?", (rows[0][0],))
        self.store.conn.execute("UPDATE closure_notifications SET error='request_uncertain' WHERE event_seq=?", (rows[1][0],))
        before = self.notices()[:2]
        with patch.object(closure.time, "time", return_value=10**12): self.tick()
        self.assertEqual(self.notices()[:2], before)

    def test_restart_keeps_global_cooldown(self):
        with patch.object(closure.time, "time", return_value=1000):
            self.block(self.issue("before")); self.tick()
        self.block(self.issue("after"))
        self.store.conn.close(); self.store = closure.Store(self.database)
        with patch.object(closure.time, "time", return_value=1100):
            self.assertEqual(self.tick().call_count, 0)
        self.assertEqual(self.notices()[-1][1:], (0, None))

    def test_resuming_suspended_sender_starts_cooldown_at_call_end(self):
        for uncertain in (False, True):
            base = 30000 if uncertain else 10000
            with self.subTest(uncertain=uncertain), patch.object(closure.time, "time", return_value=base) as clock:
                self.block(self.issue("paused-" + str(uncertain)))
                def resumed(*args, **kwargs):
                    clock.return_value = base + 2000
                    if uncertain: raise subprocess.TimeoutExpired(args[0], 8)
                    return subprocess.CompletedProcess([], 0)
                self.tick(error=resumed)
                clock.return_value = base + 2001
                self.block(self.issue("after-pause-" + str(uncertain)))
                before = self.notices()
                self.assertEqual(self.tick().call_count, 0)
                self.assertEqual(self.notices(), before)
                clock.return_value = base + 2901
                self.assertEqual(self.tick().call_count, 1)

    def test_same_decision_update_preserves_version_without_notification(self):
        item = self.issue("decision", choice=True); self.tick()
        before = len(self.notices())
        changed = self.block(item, state="decision", reason="补充选择说明")
        self.assertEqual(changed["state"], "decision")
        self.assertEqual(changed["revision"], item["revision"] + 1)
        self.assertEqual(len(self.notices()), before)

    def test_claim_transaction_failure_rolls_back_and_never_sends(self):
        for index in range(3): self.block(self.issue(str(index)))
        before = self.notices()
        with patch.object(self.store, "event", side_effect=sqlite3.OperationalError("fixture write failure")), \
             patch.object(closure.subprocess, "run") as sender:
            with self.assertRaises(sqlite3.OperationalError): closure._tick_locked(self.store)
        self.assertEqual(sender.call_count, 0)
        self.assertEqual(self.notices(), before)

    def test_process_interruption_keeps_whole_batch_unknown_after_restart(self):
        for index in range(3): self.block(self.issue(str(index)))
        with self.assertRaises(KeyboardInterrupt): self.tick(error=KeyboardInterrupt)
        self.store.conn.close(); self.store = closure.Store(self.database)
        self.assertEqual(self.tick().call_count, 0)
        self.assertTrue(all(row[1:] == (0, "request_uncertain") for row in self.notices()))

    def test_new_event_during_submission_is_not_falsely_acknowledged(self):
        self.block(self.issue("batch"))
        def submitted(*args, **kwargs):
            self.block(self.issue("later"))
            return subprocess.CompletedProcess([], 0)
        self.tick(error=submitted)
        self.assertEqual(self.notices()[0][1:], (1, None))
        self.assertEqual(self.notices()[1][1:], (0, None))

    def test_canceled_completion_with_changed_evidence_is_not_announced(self):
        item = self.issue("canceled")
        item = self.block(item, state="canceled")
        self.store.cancellation_check({"issue_id": item["id"], "revision": item["revision"],
            "verified": True, "reviewer": "isolated reviewer", "review": "no side effects",
            "evidence": [{"path": str(self.source)}]})
        self.source.write_text("new evidence invalidates the former check")
        self.assertEqual(self.tick().call_count, 0)
        self.assertEqual(self.store.get(item["id"])["state"], "canceled")
        self.assertEqual(self.notices()[0][1:], (0, "notification_obsolete"))

    def test_current_canceled_completion_stays_local(self):
        item = self.block(self.issue("canceled"), state="canceled")
        self.store.cancellation_check({"issue_id": item["id"], "revision": item["revision"],
            "verified": True, "reviewer": "isolated reviewer", "review": "no side effects",
            "evidence": [{"path": str(self.source)}]})
        self.assertEqual(self.tick().call_count, 0)
        self.assertEqual(self.tick().call_count, 0)

    def test_agent_owned_technical_wait_stays_local_then_decision_is_delivered(self):
        item = self.block(self.issue("recoverable"), actionable=False)
        self.assertEqual(self.tick().call_count, 0)
        self.assertEqual(self.notices()[0][1:], (0, "notification_local_only"))
        item = self.block(item, state="decision")
        sender = self.tick()
        self.assertEqual(sender.call_count, 1)
        self.assertIn(item["id"][:8], sender.call_args.args[0][-1])
        self.assertIn("相关操作保持等待", sender.call_args.args[0][-1])
        self.assertEqual(self.store.get(item["id"])["state"], "decision")

    def test_unknown_dispatch_after_local_wait_is_not_silenced(self):
        item = self.block(self.issue("unknown-later"), actionable=False)
        self.tick()
        self.store.conn.execute("UPDATE closure_issues SET dispatch_state='creation_uncertain' WHERE id=?", (item["id"],))
        self.store.event("dispatch_uncertain", {"code": "fixture"}, issue=item["id"], task=self.task, notify=True)
        self.assertEqual(self.tick().call_count, 1)
        self.assertEqual(self.tick().call_count, 0)

    def test_canceled_after_claim_never_enters_sender(self):
        item = self.issue("cancel-before-send", choice=True)
        original = self.store.event
        armed = False
        def event(kind, *args, **kwargs):
            nonlocal armed
            result = original(kind, *args, **kwargs)
            if kind == "notification_digest_started": armed = True
            return result
        transaction = self.store.transaction
        @contextmanager
        def concurrent_choice():
            nonlocal armed
            with transaction(): yield
            if armed:
                armed = False
                current = self.store.get(item["id"])
                self.store.transition(current["id"], "canceled", "fixture human cancellation", current["revision"])
        with patch.object(self.store, "event", side_effect=event), patch.object(self.store, "transaction", concurrent_choice):
            self.assertEqual(self.tick().call_count, 0)
        self.assertEqual(self.tick().call_count, 0)
        self.assertEqual(self.store.get(item["id"])["state"], "canceled")
        self.assertEqual(self.notices()[0][1], 0)

    def test_new_decision_version_after_claim_remains_pending_for_next_tick(self):
        item = self.issue("new-version-before-send", choice=True)
        original = self.store.event
        armed = False
        def event(kind, *args, **kwargs):
            nonlocal armed
            result = original(kind, *args, **kwargs)
            if kind == "notification_digest_started": armed = True
            return result
        transaction = self.store.transaction
        @contextmanager
        def concurrent_choice():
            nonlocal armed
            with transaction(): yield
            if armed:
                armed = False
                current = self.store.get(item["id"])
                self.store.transition(current["id"], "decision", "updated terms", current["revision"])
        with patch.object(closure.time, "time", return_value=1000), patch.object(self.store, "event", side_effect=event), patch.object(self.store, "transaction", concurrent_choice):
            self.assertEqual(self.tick().call_count, 0)
        self.assertEqual(self.notices()[0][1:], (0, None))
        with patch.object(closure.time, "time", return_value=2000):
            sender = self.tick()
        self.assertEqual(sender.call_count, 1)
        self.assertIn("版本 2", sender.call_args.args[0][-1])

    def test_explicit_keep_waiting_silences_old_notice_but_not_new_decision(self):
        item = self.issue("human-wait", choice=True)
        preview = self.store.choice_preview(item["id"])
        item = self.store.decide({"token": preview["token"], "issue_id": item["id"],
            "revision": item["revision"], "choice": "keep_waiting"})
        self.assertEqual(self.tick().call_count, 0)
        self.assertEqual(self.notices()[0][1:], (0, "notification_human_wait"))
        self.assertEqual(self.store.get(item["id"])["state"], "decision")
        item = self.store.transition(item["id"], "decision", "new independent terms", item["revision"])
        self.store.event("authority_restricted", {"state": "decision"}, issue=item["id"], task=self.task, notify=True)
        sender = self.tick()
        self.assertEqual(sender.call_count, 1)
        self.assertIn("版本 3", sender.call_args.args[0][-1])

    def test_resolved_completion_with_changed_evidence_is_not_announced(self):
        item = self.issue("resolved")
        self.store.accept({"issue_id": item["id"], "revision": item["revision"], "verified": True,
            "reviewer": "isolated reviewer", "review": "read fixture", "evidence": [{"path": str(self.source)}]})
        self.source.write_text("new evidence")
        self.assertEqual(self.tick().call_count, 0)
        self.assertEqual(self.store.get(item["id"])["state"], "verification")


if __name__ == "__main__": unittest.main()
