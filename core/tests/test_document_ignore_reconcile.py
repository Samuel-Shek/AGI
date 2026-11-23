from __future__ import annotations

import json
import subprocess
import threading
import unittest
from pathlib import Path
from unittest import mock

import test_document_changes as fixtures
from activity_mvp import document_changes as d
from activity_mvp.storage import ensure_ready


class IgnoreRuleReconcileTests(unittest.TestCase):
    # Reuse the real temporary database/helper fixtures without inheriting (and
    # rerunning) the unrelated document suite.
    setUp = fixtures.DocumentChangeTests.setUp
    tearDown = fixtures.DocumentChangeTests.tearDown
    _event = fixtures.DocumentChangeTests._event
    _wait_until = staticmethod(fixtures.DocumentChangeTests._wait_until)
    _stop_worker = staticmethod(fixtures.DocumentChangeTests._stop_worker)

    def _start(self, *, count=1, hook=None, eligibility=None, diagnostics=None):
        holder = {}
        class Process(fixtures._ControlledProcess):
            root_count = count
        def factory(command, **kwargs):
            holder['process'] = Process(command, **kwargs)
            return holder['process']
        worker = d.start_document_change_worker(
            self.db, ['/safe/helper'], popen_factory=factory,
            session_state_provider=lambda: {'state': 'unlocked', 'locked': False,
                                           'on_console': True, 'capture_allowed': True},
            reconcile_eligibility_provider=eligibility,
            reconcile_eligibility_check_interval_seconds=0,
            _transition_hook=hook, _diagnostic_sink=diagnostics,
        )
        return worker, holder['process']

    def _requests(self):
        row = self.conn.execute("SELECT value FROM daemon_state WHERE key='document_ignore_reconcile_requests'").fetchone()
        return json.loads(row[0]) if row else {}

    def _coverage(self, label='Watch'):
        row = self.conn.execute('SELECT c.coverage_status,c.event_gap_count FROM file_watch_checkpoints c JOIN file_watch_roots r USING(root_id) WHERE r.root_label=?', (label,)).fetchone()
        return tuple(row)

    def _emit_rule(self, process, event_id=700):
        process.emit(self._event(self.watch / '.gitignore', event_id,
                                flags=['item_modified', 'item_is_file']))

    def _fast_cooldown(self):
        patch = mock.patch.object(d, 'DOCUMENT_IGNORE_RECONCILE_COOLDOWN_SECONDS', 0, create=True)
        patch.start(); self.addCleanup(patch.stop)

    def test_live_unignore_is_really_scanned_without_helper_restart_and_history_is_kept(self):
        subprocess.run(['git', 'init', '-q'], cwd=self.watch, check=True)
        rule = self.watch / '.gitignore'; rule.write_text('hidden.md\n')
        hidden = self.watch / 'hidden.md'; hidden.write_text('previously outside the tracked set')
        worker, process = self._start(); self._fast_cooldown()
        try:
            self.assertEqual(self.conn.execute('SELECT count(*) FROM file_watch_state WHERE present=1').fetchone()[0], 0)
            rule.write_text('')
            self._emit_rule(process)
            self.assertTrue(self._wait_until(lambda: self._coverage()[1] > 0, timeout=6))
            self.assertTrue(self._wait_until(lambda: self._coverage() == ('complete', 0) and not self._requests(), timeout=12))
            self.assertEqual(self.conn.execute('SELECT count(*) FROM file_watch_state WHERE present=1').fetchone()[0], 1)
            self.assertGreater(self.conn.execute("SELECT count(*) FROM file_watch_coverage_gaps WHERE reason_code='ignore_rules_reconcile_required'").fetchone()[0], 0)
            self.assertFalse(process.terminated)
        finally:
            self._stop_worker(worker)

    def test_other_root_event_is_persisted_while_target_scanner_is_blocked(self):
        other = self.root / 'other'; other.mkdir()
        d.configure_document_changes(self.conn, add_root=other, root_label='Other', runtime_root=self.db.parent)
        (self.watch / 'tracked.md').write_text('before')
        entered, release = threading.Event(), threading.Event()
        original = d.build_document_baseline
        def blocked(*args, **kwargs):
            if threading.current_thread().name == 'pam-document-rule-reconcile':
                entered.set(); self.assertTrue(release.wait(8))
            return original(*args, **kwargs)
        with mock.patch.object(d, 'build_document_baseline', side_effect=blocked):
            diagnostics = []
            worker, process = self._start(count=2, diagnostics=diagnostics.append); self._fast_cooldown()
            try:
                (self.watch / '.gitignore').write_text('build/\n'); self._emit_rule(process)
                self.assertTrue(entered.wait(8))
                target = other / 'during-target-scan.md'; target.write_text('must be recorded now')
                process.emit(self._event(target, 701, flags=['item_created', 'item_is_file']))
                self.assertTrue(self._wait_until(lambda: self.conn.execute("SELECT count(*) FROM file_change_versions v JOIN file_watch_roots r USING(root_id) WHERE r.root_label='Other'").fetchone()[0] > 0, timeout=5))
                self.assertEqual(self._coverage('Other'), ('complete', 0))
                self.assertTrue(worker['monitor'].is_alive())
                self.assertFalse(process.terminated)
                release.set()
                self.assertTrue(self._wait_until(lambda: self._coverage() == ('complete', 0) and not worker['reconciler'].is_alive(), timeout=8))
                runtime_progress = [item for item in diagnostics if item.get('event') == 'document_change_baseline_progress' and item.get('baseline_kind') == 'ignore_rules_reconcile']
                self.assertTrue(runtime_progress)
                self.assertEqual(runtime_progress[-1]['stage'], 'completed')
                self.assertTrue(all(item['root_count'] == 1 for item in runtime_progress))
                self.assertLessEqual(runtime_progress[-1]['root_scan_attempts_finished_count'], 2)
            finally:
                release.set(); self._stop_worker(worker)

    def test_two_pass_rule_churn_stays_partial_and_keeps_request(self):
        (self.watch / 'tracked.md').write_text('before')
        rule = self.watch / '.gitignore'; rule.write_text('build/\n')
        original = d._iter_safe_files; count = []; holder = {}
        def changing(root, **kwargs):
            yield from original(root, **kwargs)
            if threading.current_thread().name == 'pam-document-rule-reconcile':
                count.append(1); rule.write_text('build/\n' + ('*.tmp\n' if len(count) % 2 else '*.cache\n'))
                self._emit_rule(holder['process'], 710 + len(count))
        worker, process = self._start(); holder['process'] = process; self._fast_cooldown()
        with mock.patch.object(d, '_iter_safe_files', side_effect=changing):
            try:
                self._emit_rule(process)
                self.assertTrue(self._wait_until(lambda: len(count) >= 2 and worker.get('reconciler') is not None and not worker['reconciler'].is_alive(), timeout=12))
                self.assertEqual(len(count), 2)
                self.assertEqual(self._coverage()[0], 'partial')
                self.assertTrue(self._requests())
                self.assertFalse(process.terminated)
            finally:
                self._stop_worker(worker)

    def test_same_event_id_new_direct_rule_request_cannot_be_erased_by_old_finish(self):
        (self.watch / 'tracked.md').write_text('before')
        rule = self.watch / '.gitignore'; rule.write_text('build/\n'); finishes = []
        def hook(stage):
            if stage == 'before_finish_lock' and threading.current_thread().name == 'pam-document-rule-reconcile':
                finishes.append(1); rule.write_text('build/\n' + '*.tmp\n' * len(finishes))
                conn = ensure_ready(self.db)
                try:
                    result = d.process_document_event(conn, self._event(rule, 700, flags=['item_modified', 'item_is_file']), root_path=self.watch)
                    self.assertEqual(result['reason'], 'ignore_rules_reconcile_required')
                finally:
                    conn.close()
        worker, process = self._start(hook=hook); self._fast_cooldown()
        try:
            self._emit_rule(process)
            self.assertTrue(self._wait_until(lambda: len(finishes) >= 2 and not worker['reconciler'].is_alive(), timeout=12))
            self.assertEqual(len(finishes), 2)
            self.assertEqual(self._coverage()[0], 'partial')
            self.assertTrue(self._requests())
            self.assertFalse(process.terminated)
        finally:
            self._stop_worker(worker)

    def test_loss_of_reconcile_window_defers_without_killing_channel(self):
        (self.watch / 'tracked.md').write_text('before')
        entered, release = threading.Event(), threading.Event(); allowed = [True]
        original = d.build_document_baseline
        def blocked(*args, **kwargs):
            if threading.current_thread().name == 'pam-document-rule-reconcile':
                entered.set(); self.assertTrue(release.wait(8))
            return original(*args, **kwargs)
        with mock.patch.object(d, 'build_document_baseline', side_effect=blocked):
            worker, process = self._start(eligibility=lambda: {'allowed': allowed[0]}); self._fast_cooldown()
            try:
                (self.watch / '.gitignore').write_text('build/\n'); self._emit_rule(process)
                self.assertTrue(entered.wait(8)); allowed[0] = False; release.set()
                self.assertTrue(self._wait_until(lambda: not worker['reconciler'].is_alive(), timeout=6))
                self.assertTrue(self._requests()); self.assertEqual(self._coverage()[0], 'partial')
                self.assertFalse(process.terminated)
                allowed[0] = True
                self.assertTrue(self._wait_until(lambda: self._coverage() == ('complete', 0), timeout=10))
            finally:
                release.set(); self._stop_worker(worker)

    def test_real_stream_drop_during_target_scan_keeps_original_reset(self):
        (self.watch / 'tracked.md').write_text('before')
        entered, release = threading.Event(), threading.Event(); original = d.build_document_baseline
        def blocked(*args, **kwargs):
            if threading.current_thread().name == 'pam-document-rule-reconcile':
                entered.set(); self.assertTrue(release.wait(8))
            return original(*args, **kwargs)
        with mock.patch.object(d, 'build_document_baseline', side_effect=blocked):
            worker, process = self._start(); self._fast_cooldown()
            try:
                (self.watch / '.gitignore').write_text('build/\n'); self._emit_rule(process)
                self.assertTrue(entered.wait(8))
                process.emit(self._event(self.watch, 702, flags=['must_scan_subdirs', 'user_dropped']))
                self.assertTrue(self._wait_until(lambda: process.terminated, timeout=5)); release.set()
                self.assertTrue(self._wait_until(lambda: self.conn.execute('SELECT last_event_id FROM file_watch_checkpoints').fetchone()[0] is None, timeout=5))
                self.assertEqual(self._coverage()[0], 'partial')
                self.assertTrue(self._requests())
            finally:
                release.set(); self._stop_worker(worker)

    def test_live_git_directory_creation_preserves_rule_transition(self):
        rule = self.watch / '.gitignore'; rule.write_text('ignored.md\n')
        (self.watch / 'ignored.md').write_text('tracked until a Git boundary exists')
        worker, process = self._start(); self._fast_cooldown()
        try:
            self.assertEqual(self.conn.execute('SELECT count(*) FROM file_watch_state WHERE present=1').fetchone()[0], 1)
            subprocess.run(['git', 'init', '-q'], cwd=self.watch, check=True)
            process.emit(self._event(self.watch / '.git', 720, flags=['item_created', 'item_is_dir']))
            self.assertTrue(self._wait_until(lambda: self.conn.execute("SELECT count(*) FROM file_watch_coverage_gaps WHERE reason_code='ignore_rules_reconcile_required'").fetchone()[0] > 0, timeout=6))
            self.assertTrue(self._wait_until(lambda: self._coverage() == ('complete', 0) and not self._requests(), timeout=12))
            self.assertEqual(self.conn.execute('SELECT count(*) FROM file_watch_state WHERE present=1').fetchone()[0], 0)
            self.assertFalse(process.terminated)
        finally:
            self._stop_worker(worker)

    def test_skipped_subtree_rule_noise_does_not_invalidate_target_pass(self):
        (self.watch / 'tracked.md').write_text('before')
        noise = self.watch / 'node_modules/pkg/.gitignore'; noise.parent.mkdir(parents=True); noise.write_text('*.tmp\n')
        original = d._iter_safe_files; passes = []; holder = {}
        def enumerate_with_noise(root, **kwargs):
            yield from original(root, **kwargs)
            if threading.current_thread().name == 'pam-document-rule-reconcile':
                passes.append(1)
                for i in range(6):
                    holder['process'].emit(self._event(noise, 730 + i, flags=['item_modified', 'item_is_file']))
        worker, process = self._start(); holder['process'] = process; self._fast_cooldown()
        with mock.patch.object(d, '_iter_safe_files', side_effect=enumerate_with_noise):
            try:
                (self.watch / '.gitignore').write_text('build/\n'); self._emit_rule(process)
                self.assertTrue(self._wait_until(lambda: passes and self._coverage() == ('complete', 0) and not self._requests(), timeout=12))
                self.assertEqual(len(passes), 1)
                self.assertFalse(process.terminated)
            finally:
                self._stop_worker(worker)

    def test_window_lost_at_final_commit_keeps_request_and_partial(self):
        (self.watch / 'tracked.md').write_text('before'); allowed = [True]; checked = []
        def hook(stage):
            if stage == 'before_finish_lock' and threading.current_thread().name == 'pam-document-rule-reconcile':
                checked.append(1); allowed[0] = False
        worker, process = self._start(hook=hook, eligibility=lambda: {'allowed': allowed[0]}); self._fast_cooldown()
        try:
            (self.watch / '.gitignore').write_text('build/\n'); self._emit_rule(process)
            self.assertTrue(self._wait_until(lambda: checked and not worker['reconciler'].is_alive(), timeout=10))
            self.assertTrue(self._requests()); self.assertEqual(self._coverage()[0], 'partial')
            self.assertFalse(process.terminated)
        finally:
            self._stop_worker(worker)

    def test_rule_gap_and_request_rollback_together_when_commit_fails(self):
        import sqlite3
        (self.watch / 'tracked.md').write_text('before'); d.build_document_baseline(self.conn, self.watch)
        (self.watch / '.gitignore').write_text('build/\n')
        before = tuple(self.conn.execute('SELECT last_event_id,coverage_status,event_gap_count FROM file_watch_checkpoints').fetchone())
        failing = sqlite3.connect(self.db, factory=fixtures._FailingCommitConnection); failing.row_factory = sqlite3.Row
        try:
            failing.fail_next_commit = True
            with self.assertRaises(OSError):
                d.process_document_event(failing, self._event(self.watch / '.gitignore', 700, flags=['item_modified', 'item_is_file']), root_path=self.watch)
            self.assertFalse(failing.in_transaction)
            self.assertEqual(tuple(self.conn.execute('SELECT last_event_id,coverage_status,event_gap_count FROM file_watch_checkpoints').fetchone()), before)
            self.assertFalse(self._requests())
            self.assertEqual(self.conn.execute("SELECT count(*) FROM file_watch_coverage_gaps WHERE reason_code='ignore_rules_reconcile_required'").fetchone()[0], 0)
        finally:
            failing.close()

    def test_stale_monitor_request_after_success_does_not_rescan_covered_root(self):
        (self.watch / 'tracked.md').write_text('before')
        completed, injected, fresh_read = threading.Event(), threading.Event(), threading.Event()
        scans = []; stale = {}; worker_holder = {}; old_thread = []; original_requests = d._ignore_reconcile_requests; original_build = d.build_document_baseline
        def requests(conn):
            current = original_requests(conn)
            if threading.current_thread().name == 'pam-document-config-monitor':
                if current:
                    stale.update(current)
                elif (completed.is_set() and stale and not injected.is_set()
                      and worker_holder['worker'].get('reconciler') is not None
                      and not worker_holder['worker']['reconciler'].is_alive()):
                    old_thread.append(worker_holder['worker']['reconciler'])
                    injected.set()
                    return dict(stale)
            elif threading.current_thread().name == 'pam-document-rule-reconcile' and injected.is_set():
                fresh_read.set()
            return current
        def build(*args, **kwargs):
            if threading.current_thread().name == 'pam-document-rule-reconcile':
                scans.append(1)
            return original_build(*args, **kwargs)
        def diagnostic(item):
            if item.get('baseline_kind') == 'ignore_rules_reconcile' and item.get('stage') == 'completed':
                completed.set()
        with mock.patch.object(d, '_ignore_reconcile_requests', side_effect=requests), mock.patch.object(d, 'build_document_baseline', side_effect=build):
            worker, process = self._start(diagnostics=diagnostic); worker_holder['worker'] = worker; self._fast_cooldown()
            try:
                (self.watch / '.gitignore').write_text('build/\n'); self._emit_rule(process)
                self.assertTrue(completed.wait(10)); self.assertTrue(injected.wait(5))
                self.assertTrue(fresh_read.wait(5))
                self.assertIsNot(worker['reconciler'], old_thread[0])
                self.assertTrue(self._wait_until(lambda: not worker['reconciler'].is_alive(), timeout=4))
                self.assertEqual(len(scans), 1)
                self.assertEqual(self._coverage(), ('complete', 0)); self.assertFalse(original_requests(self.conn))
                self.assertFalse(process.terminated)
            finally:
                self._stop_worker(worker)
