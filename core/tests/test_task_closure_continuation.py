"""Closure v1: stop decisions, bounded readiness and honest native runtime."""
import hashlib
import json
import time
import unittest
from unittest.mock import patch

from tests.test_task_closure import ClosureTests
from activity_mvp import task_closure as closure


class ContinuationTests(ClosureTests):
    def root_receipt(self):
        self.store.conn.execute("CREATE TABLE IF NOT EXISTS closure_hook_roots(platform TEXT,session TEXT,turn TEXT,task_id TEXT,prompt_hash TEXT,PRIMARY KEY(platform,session))")
        self.store.conn.execute("INSERT OR REPLACE INTO closure_hook_roots VALUES('codex','test-session','original',?,'fixture')", (self.task,))

    def plan(self, item, conditions=None):
        self.root_receipt()
        return self.store.register_recovery({"issue_id": item["id"], "revision": item["revision"],
                                            "conditions": conditions or [{"kind": "after_time", "at": 1}]},
                                           native_context={"platform": "codex", "session": "test-session", "turn": "original", "task_id": self.task})

    def decision(self):
        return self.store.task_status(self.task)["stop_decision"]["decision"]

    def test_valid_audit_and_owner_do_not_close_runnable_main_or_related(self):
        self.root_receipt(); self.audit()
        for category in ("main", "related"):
            item = self.issue(key=category, category=category)
            self.assertEqual(self.decision(), "continue_now")
            self.assertFalse(self.store.task_status(self.task)["all_complete"])
            self.store.accept({"issue_id": item["id"], "revision": 1, "verified": True,
                               "reviewer": "independent", "review": "fixture verified", "evidence": [{"path": str(self.source)}]})
        self.assertEqual(self.decision(), "complete")

    def test_waiting_does_not_hide_missing_independent_review(self):
        item = self.issue(category="main")
        self.store.transition(item["id"], "blocked", "tool unavailable", 1, "original owner checks tool availability")
        self.assertEqual(self.decision(), "unknown")
        self.audit(); self.assertEqual(self.decision(), "waiting")
        self.assertFalse(self.store.task_status(self.task)["all_complete"])

    def test_no_native_root_or_changed_evidence_is_unknown(self):
        self.issue(category="main"); self.audit()
        self.assertEqual(self.decision(), "unknown")
        self.root_receipt(); self.source.write_text("changed")
        self.assertEqual(self.decision(), "unknown")

    def test_ready_never_unblocks_or_sends_and_remains_once_after_restart(self):
        item = self.issue(category="main")
        item = self.store.transition(item["id"], "blocked", "missing fixture", 1, "file appears")
        self.plan(item)
        with patch.object(self.store, "observe_runtime"), patch.object(self.store, "dispatch") as dispatch:
            self.store.check_recovery(item["id"]); self.store.check_recovery(item["id"])
        dispatch.assert_not_called()
        self.assertEqual(self.store.get(item["id"])["state"], "blocked")
        self.assertEqual(self.store.get(item["id"])["revision"], item["revision"])
        self.assertEqual(self.store.recovery_view(item)["status"], "ready")
        self.assertFalse(self.store.recovery_view(item)["auto_execution"])
        with closure.Store(self.db) as reopened, patch.object(reopened, "observe_runtime"):
            reopened.check_recovery(item["id"])
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM closure_events WHERE kind='recovery_ready'").fetchone()[0], 1)

    def test_registration_requires_current_original_root_and_exact_owner(self):
        item = self.issue(category="main")
        with self.assertRaises(ValueError): self.store.register_recovery({"issue_id": item["id"], "revision": 1, "conditions": [{"kind": "after_time", "at": 1}]})
        self.root_receipt()
        self.store.conn.execute("UPDATE closure_issues SET owner='codex:other' WHERE id=?", (item["id"],))
        with self.assertRaises(ValueError): self.plan(item)

    def test_foreign_native_caller_cannot_arm_original_task(self):
        item = self.issue(category="main"); self.root_receipt()
        payload = {"issue_id": item["id"], "revision": 1, "conditions": [{"kind": "after_time", "at": 1}]}
        with patch.dict(closure.os.environ, {"CODEX_THREAD_ID": "different-real-caller"}):
            self.store.conn.execute("INSERT INTO closure_hook_roots VALUES('codex','different-real-caller','foreign','foreign-task','fixture')")
            with self.assertRaises(ValueError): self.store.register_recovery(payload, native_context=self.store.recovery_caller())
        with patch.dict(closure.os.environ, {"CODEX_THREAD_ID": "test-session"}):
            self.store.register_recovery(payload, native_context=self.store.recovery_caller())

    def test_uncertain_actual_send_states_cannot_continue(self):
        self.root_receipt(); self.audit(); item = self.issue(category="main")
        for state in ("send_uncertain", "creation_uncertain"):
            self.store.conn.execute("UPDATE closure_issues SET dispatch_state=? WHERE id=?", (state, item["id"]))
            self.assertEqual(self.decision(), "waiting")

    def test_delayed_ready_notice_rechecks_extra_file_without_refreshing_time(self):
        item = self.issue(category="main")
        extra = self.root / "recovery.txt"; extra.write_text("available")
        self.plan(item, [{"kind": "file_readable", "path": str(extra)}]); self.store.check_recovery(item["id"])
        self.assertEqual(self.store.recovery_view(item)["status"], "ready")
        checked = self.store.conn.execute("SELECT checked_at FROM closure_recovery_plans").fetchone()[0]
        extra.unlink()
        self.assertEqual(self.store.recovery_view(item)["status"], "waiting")
        with patch.object(closure.subprocess, "run") as notice: closure._notify_pending(self.store)
        notice.assert_not_called()
        self.assertEqual(self.store.conn.execute("SELECT checked_at FROM closure_recovery_plans").fetchone()[0], checked)
        pending = self.store.conn.execute("SELECT requested,error FROM closure_notifications").fetchone()
        self.assertEqual(tuple(pending), (0, None))
        extra.write_text("available again")
        with patch.object(closure.subprocess, "run", return_value=__import__('subprocess').CompletedProcess([],0)) as notice:
            closure._notify_pending(self.store); closure._notify_pending(self.store)
        self.assertEqual(notice.call_count, 1)
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM closure_events WHERE kind='recovery_ready'").fetchone()[0], 1)

    def test_expired_owner_readiness_is_unknown_without_refresh(self):
        item = self.issue(category="main")
        self.store.conn.execute("UPDATE closure_issues SET owner='codex:00000000-0000-4000-8000-000000000002' WHERE id=?", (item["id"],))
        item = self.store.get(item["id"])
        observation = {"activity": "idle", "source": "desktop_owner", "exact_turn": True, "thread_id": closure.codex_thread(item), "observed_at": time.time()-31}
        self.store.conn.execute("INSERT OR REPLACE INTO closure_runtime_observations VALUES(?,?,?)", (item["id"], self.store.binding(item), json.dumps(observation)))
        row = {"conditions": json.dumps([{"kind": "owner_available"}])}
        self.assertEqual(self.store.recovery_conditions(item, row), "unknown")

    def test_no_arbitrary_commands_symlinks_or_outside_paths(self):
        item = self.issue(category="main")
        alias = self.root / "alias"; alias.symlink_to(self.source)
        for condition in ({"kind": "command", "cmd": "anything"}, {"kind": "file_readable", "path": str(alias)},
                          {"kind": "file_readable", "path": "/etc/hosts"}):
            with self.subTest(condition=condition), self.assertRaises(ValueError): self.plan(item, [condition])

    def test_readiness_is_bound_to_file_hash_and_issue_revision(self):
        item = self.issue(category="main")
        sha = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.plan(item, [{"kind": "file_readable", "path": str(self.source), "sha256": sha}])
        self.source.write_text("changed")
        with patch.object(self.store, "observe_runtime"): self.store.check_recovery(item["id"])
        self.assertEqual(self.store.recovery_view(item)["status"], "stale")
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM closure_events WHERE kind='recovery_ready'").fetchone()[0], 0)

    def test_cancel_and_choice_disable_recovery_without_resurrecting(self):
        item = self.issue(category="main"); self.plan(item)
        item = self.store.transition(item["id"], "canceled", "user canceled", 1)
        self.store.check_recovery(item["id"])
        self.assertEqual(self.store.recovery_view(item)["status"], "inactive")
        self.assertEqual(self.store.get(item["id"])["state"], "canceled")
        decision = self.issue(key="decision", category="main", authority={"source": "choice required", "scope": str(self.root), "personal_choice": True})
        with self.assertRaises(ValueError): self.plan(decision)

    def test_legacy_row_has_no_armed_recovery_and_no_running_claim(self):
        item = self.issue(category="main")
        item = self.store.transition(item["id"], "running", "executor started", 1)
        value = closure.projection(self.db, item["id"])[0]
        self.assertIn("登记为执行中", value["summary"])
        self.assertEqual(value["closure_runtime_label"], "运行状态待核查")
        self.assertEqual(value["closure_recovery_label"], "未安排恢复检查")

    def test_native_runtime_requires_exact_turn_source_and_fresh_age(self):
        item = self.issue(category="main")
        self.store.conn.execute("UPDATE closure_issues SET owner='codex:00000000-0000-4000-8000-000000000002' WHERE id=?", (item["id"],))
        item = self.store.get(item["id"])
        base = {"activity": "active", "source": "desktop_owner", "exact_turn": True,
                "thread_id": closure.codex_thread(item), "observed_at": time.time(), "observed_monotonic": closure.elapsed_time(), "boot_identity": closure.boot_identity(), "clock_kind": "continuous_boot_v1"}
        for change in ({"observed_at": time.time()-31}, {"observed_at": time.time()+3},
                       {"source": "offline"}, {"exact_turn": False}, {"thread_id": "other"}):
            self.store.conn.execute("INSERT OR REPLACE INTO closure_runtime_observations VALUES(?,?,?)",
                                    (item["id"], self.store.binding(item), json.dumps({**base, **change})))
            self.assertEqual(self.store.runtime_view(item)["activity"], "unknown")
        self.store.conn.execute("INSERT OR REPLACE INTO closure_runtime_observations VALUES(?,?,?)",
                                (item["id"], self.store.binding(item), json.dumps(base)))
        self.assertEqual(self.store.runtime_view(item)["label"], "正在执行")
        self.assertEqual(self.store.get(item["id"])["state"], "pending")

    def test_real_elapsed_boot_and_clock_rollback_do_not_renew_evidence(self):
        base = {"observed_at": 1000, "observed_monotonic": 2000, "boot_identity": closure.boot_identity(), "clock_kind": "continuous_boot_v1"}
        with patch.object(closure.time, "time", return_value=1001), patch.object(closure, "elapsed_time", return_value=2031):
            self.assertFalse(closure.observation_fresh(base))
        with patch.object(closure.time, "time", return_value=1001), patch.object(closure, "elapsed_time", return_value=2001):
            self.assertTrue(closure.observation_fresh(base))
            self.assertFalse(closure.observation_fresh({**base, "boot_identity": "other-boot"}))
            self.assertFalse(closure.observation_fresh({"observed_at": 1000}))

    def test_recovery_read_rejects_parent_and_leaf_swaps_without_outside_reads(self):
        folder = self.root / "recover-dir"; folder.mkdir()
        path = folder / "ready.txt"; path.write_text("inside")
        outside = self.root.parent / (self.root.name + "-outside"); outside.mkdir()
        (outside / "ready.txt").write_text("outside fixture")
        real_open = closure.os.open
        condition = {"kind": "file_readable", "path": str(path)}
        for leaf in (True, False):
            with self.subTest(leaf=leaf):
                def swap(name, flags, **kwargs):
                    if name == ("ready.txt" if leaf else "recover-dir"):
                        if leaf: path.unlink(); path.symlink_to(outside / "ready.txt")
                        else: folder.rename(self.root / "original-dir"); folder.symlink_to(outside, target_is_directory=True)
                    return real_open(name, flags, **kwargs)
                with patch.object(closure.os, "open", side_effect=swap), patch.object(closure.os, "read") as read:
                    self.assertEqual(closure.recovery_file(condition, str(self.root)), "stale")
                read.assert_not_called()
                if leaf: path.unlink(); path.write_text("inside")
                else: folder.unlink(); (self.root / "original-dir").rename(folder)
        (outside / "ready.txt").unlink(); outside.rmdir()

    def test_fifo_and_growing_file_are_bounded_and_not_ready(self):
        fifo = self.root / "fifo"; closure.os.mkfifo(fifo)
        self.assertEqual(closure.recovery_file({"path": str(fifo)}, str(self.root)), "stale")
        path = self.root / "growing.txt"; path.write_text("small")
        real_read=closure.os.read; sizes=[]
        def grow(descriptor, count):
            if not sizes: path.write_bytes(b"x" * (2*1024*1024+10))
            data=real_read(descriptor, count);sizes.append(len(data));return data
        with patch.object(closure.os, "read", side_effect=grow):
            self.assertEqual(closure.recovery_file({"path": str(path)}, str(self.root)), "unknown")
        self.assertLessEqual(sum(sizes), 2*1024*1024+1)

    def test_recovery_does_not_consume_stop_budget(self):
        item = self.issue(category="main"); self.plan(item)
        with patch.object(self.store, "observe_runtime"): self.store.check_recovery(item["id"])
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM closure_guards").fetchone()[0], 0)

    def test_ready_tick_never_creates_submits_or_steers_a_turn(self):
        from activity_mvp import attention_native as native
        item = self.issue(category="main"); self.plan(item)
        with patch.object(native, "AppServer") as server, patch.object(native, "DesktopIPC") as ipc, patch.object(closure, "_notify_pending"):
            closure._tick_locked(self.store); closure._tick_locked(self.store)
        server.assert_not_called(); ipc.assert_not_called()
        self.assertEqual(self.store.get(item["id"])["state"], "pending")
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM closure_events WHERE kind='recovery_ready'").fetchone()[0], 1)


if __name__ == "__main__": unittest.main()
