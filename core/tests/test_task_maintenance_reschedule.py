"""Synthetic operator protocol tests; never invoke a native scheduler or live DB."""
import datetime as dt
import contextlib
import json
import sqlite3
import sys
import os
import fcntl
import threading
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from activity_mvp.feedback import task_maintenance as tasks
from activity_mvp.feedback.store import atomic, packed
from activity_mvp.task_closure import Store as ClosureStore
from activity_mvp.feedback.control import gate as owner_gate


class ScheduleAmendmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name).resolve()
        self.sources = self.root / 'reports' / 'agi-file-merge-followup-20261006'; self.sources.mkdir(parents=True)
        self.source = self.sources / 'current.json'; self.plan = self.root / 'approved-plan.json'
        atomic(self.plan, packed({'synthetic_owner_plan': True}))
        self.receipt = {'at': 1, 'engineering_and_installation_complete': True,
                        'natural_effectiveness_accepted': False, 'external_archive_complete': False}
        self.write(self.receipt)
        self.metadata = {'kind': 'heartbeat', 'name': 'fixture', 'status': 'ACTIVE',
                         'rrule': 'FREQ=WEEKLY;BYDAY=SA;BYHOUR=20;BYMINUTE=30;BYSECOND=0;COUNT=1',
                         'target_thread_id': 'original', 'prompt_sha256': 'a' * 64,
                         'model': 'unchanged', 'notificationPolicy': 'failed_runs_only'}
        self.context = {'platform': 'codex', 'session': 'original', 'turn': '00000000-0000-0000-0000-000000000001'}
        self.database = self.root / 'data/activity.sqlite'; self.database.parent.mkdir()
        with ClosureStore(self.database) as s:
            self.task_id = s.begin({'platform': 'codex', 'session': 'original', 'turn': 'fixture',
                                   'objective': 'synthetic natural observation', 'scope_root': str(self.root),
                                   'constraints': [], 'complex': False})['task_id']
            self.issue = s.register({'task_id': self.task_id, 'category': 'related', 'key': tasks.SCHEDULE_ISSUE_KEY,
                                    'summary': 'synthetic natural evidence pending', 'target_root': str(self.root),
                                    'project_id': None, 'evidence': [{'path': str(self.plan)}],
                                    'authority': {'source': 'synthetic explicit operator plan', 'scope': str(self.root),
                                                  'reversible': True, 'cost': False, 'new_permission': False,
                                                  'external': False, 'personal_choice': False},
                                    'recommendation': 'bounded original recheck', 'acceptance_rule': 'natural evidence required'})['issue']
        self.binding = {'source': str(self.source), 'archive_source': None, 'owner_session': 'original',
                        'scheduler': dict(self.metadata), 'closure_tasks': [self.task_id]}
        self.conf = {'policy': tasks.POLICY, 'tasks': {'agi': self.binding}}
        self.ledger = tasks.Ledger(self.root / 'private'); self.ledger.set('enabled', True)
        self.auth = self.ledger.root / 'authorization.json'; atomic(self.auth, packed(self.conf))
        self.native_file = self.root / 'automation.toml'; self.native_file.write_text('synthetic immutable native file')
        self.patches = [patch.object(tasks, 'source_root', return_value=self.sources),
                        patch.object(tasks, 'scheduler_metadata', side_effect=lambda _: dict(self.metadata)),
                        patch.object(tasks, 'closure_database', return_value=self.database),
                        patch.object(tasks, '_schedule_control', side_effect=lambda _: owner_gate(self.root), create=True),
                        patch.object(tasks, 'authorized', side_effect=lambda _, **kw:
                                     (self.ledger.root, self.conf, tasks.read_json(self.auth)[1]) if kw.get('with_fingerprint')
                                     else (self.ledger.root, self.conf))]
        for p in self.patches: p.start()
        self.native_patch = patch.object(tasks, 'native_context', side_effect=lambda *a: dict(self.context)); self.native_patch.start()
        self.read = tasks._load_task_locked(self.ledger, 'agi', self.conf)
        self.at = (int(time.time()) // 60 + 26 * 60) * 60
        # Ensure a genuinely observable definition change, even across test dates.
        if tasks._schedule_rrule(self.at, time.time()) == self.metadata['rrule']: self.at += 60

    def tearDown(self):
        self.native_patch.stop()
        for p in reversed(self.patches): p.stop()
        self.ledger.close(); self.temp.cleanup()

    def write(self, value): atomic(self.source, packed(value))
    def payload(self): return {'used_version': self.read['spec']['version'], 'at': self.at, 'issue_id': self.issue['id']}
    def prepare(self): return tasks.prepare_reschedule(None, 'agi', self.payload())
    def commit(self, prepared): return tasks.commit_reschedule(None, 'agi', {'intent_id': prepared['intent_id']})
    def native_update(self, prepared): self.metadata['rrule'] = prepared['rrule']
    def records(self): return [self.ledger.get(k) for k in tasks._schedule_keys()]
    def frozen_tables(self):
        return {table: [tuple(x) for x in self.ledger.db.execute('SELECT * FROM ' + table)]
                for table in ('tasks', 'task_versions', 'uses', 'use_sources', 'events')}
    def state(self, state):
        with ClosureStore(self.database) as s:
            item = s.get(self.issue['id'])
            s.transition(item['id'], state, 'synthetic state change', item['revision'], 'synthetic recovery' if state == 'blocked' else None)

    def test_formal_flow_restores_read_and_record_without_rewriting_originals(self):
        frozen = self.frozen_tables(); original_auth = self.auth.read_bytes(); native_file = self.native_file.read_bytes()
        prepared = self.prepare(); self.assertTrue(prepared['native_update_required'])
        self.assertEqual(self.frozen_tables(), frozen)
        self.native_update(prepared)
        with self.assertRaisesRegex(PermissionError, 'original_scheduler_definition_changed'):
            tasks.refresh(self.ledger, 'agi', self.binding)
        committed = self.commit(prepared); self.assertTrue(committed['committed']); self.assertFalse(committed['native_trigger_verified'])
        self.assertEqual(self.frozen_tables(), frozen)
        read = tasks._load_task_locked(self.ledger, 'agi', self.conf)
        self.assertEqual(read['spec'], self.read['spec']); self.assertEqual(read['current_schedule'], self.metadata)
        self.assertFalse(read['spec']['stages']['natural_effect']); self.assertFalse(read['spec']['stages']['archive'])
        result = tasks.record_run(None, 'agi', {'used_version': read['spec']['version'], 'source': str(self.source),
                                               'archive_source': None, 'outcome': 'partial'})
        self.assertTrue(result['recorded']); self.assertFalse(result['retirement_eligible'])
        self.assertEqual(self.auth.read_bytes(), original_auth); self.assertEqual(self.native_file.read_bytes(), native_file)
        self.assertEqual(json.loads(self.source.read_text()), self.receipt)

    def test_unprepared_direct_change_still_rejected(self):
        self.metadata['rrule'] = tasks._schedule_rrule(self.at, time.time())
        with self.assertRaisesRegex(PermissionError, 'original_scheduler_definition_changed'):
            tasks.refresh(self.ledger, 'agi', self.binding)
        self.assertEqual(self.records(), [None, None])

    def test_prepare_loss_recovers_same_intent_without_reissuing_native_update(self):
        first = self.prepare(); second = self.prepare()
        self.assertEqual(first['intent_id'], second['intent_id']); self.assertTrue(second['idempotent'])
        self.assertTrue(second['reconcile_original_native_update_only']); self.assertNotIn('native_update_required', second)
        self.assertEqual(second['rrule'], first['rrule']); self.assertIsNone(self.records()[1])

    def test_only_one_plan_intent_and_no_second_committed_recheck(self):
        prepared = self.prepare(); original = self.records()
        other = {**self.payload(), 'at': self.at + 60}
        with self.assertRaisesRegex(PermissionError, 'already_prepared'): tasks.prepare_reschedule(None, 'agi', other)
        self.native_update(prepared); self.commit(prepared)
        with self.assertRaisesRegex(PermissionError, 'already_prepared'): tasks.prepare_reschedule(None, 'agi', other)
        self.assertEqual(self.records()[0], original[0])

    def test_commit_reply_loss_returns_original_receipt_after_time_and_version_advance(self):
        prepared = self.prepare(); self.native_update(prepared); first = self.commit(prepared)
        self.write({**self.receipt, 'at': time.time(), 'natural_effectiveness_accepted': True})
        tasks.refresh(self.ledger, 'agi', self.binding)
        with patch.object(tasks.time, 'time', return_value=self.at + 3600):
            again = self.commit(prepared)
            recovered = self.prepare()
        self.assertTrue(again['idempotent']); self.assertEqual(first['receipt'], again['receipt'])
        self.assertEqual(recovered['state'], 'committed'); self.assertNotIn('native_update_required', recovered)

    def test_commit_from_later_owner_turn_needs_original_read_not_a_new_blocked_read(self):
        prepared = self.prepare(); self.native_update(prepared)
        self.context['turn'] = '00000000-0000-0000-0000-000000000002'
        with self.assertRaises(PermissionError): tasks._load_task_locked(self.ledger, 'agi', self.conf)
        self.assertTrue(self.commit(prepared)['committed'])
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM uses').fetchone()[0], 1)

    def test_unchanged_native_definition_is_not_a_schedule_receipt(self):
        prepared = self.prepare()
        with self.assertRaisesRegex(PermissionError, 'native_schedule_readback_mismatch'): self.commit(prepared)
        self.assertIsNone(self.records()[1])

    def test_all_other_native_fields_stay_frozen(self):
        prepared = self.prepare(); self.native_update(prepared); expected = dict(self.metadata)
        for key, value in [('target_thread_id', 'foreign'), ('status', 'PAUSED'), ('status', 'CANCELLED'),
                           ('prompt_sha256', 'b' * 64), ('name', 'different'), ('model', 'changed'),
                           ('notificationPolicy', None), ('execution_environment', 'cloud'), ('budget', 0)]:
            with self.subTest(key=key, value=value):
                self.metadata = {**expected, key: value}
                with self.assertRaises(PermissionError): self.commit(prepared)
                self.assertIsNone(self.records()[1])
        self.metadata = expected; self.assertTrue(self.commit(prepared)['committed'])

    def test_paused_maintenance_blocks_preparation_and_recovery(self):
        self.ledger.set('enabled', False)
        with self.assertRaisesRegex(PermissionError, 'maintenance_paused'): self.prepare()
        self.ledger.set('enabled', True); prepared = self.prepare(); self.native_update(prepared)
        self.ledger.set('enabled', False)
        with self.assertRaisesRegex(PermissionError, 'maintenance_paused'): self.commit(prepared)
        self.assertIsNone(self.records()[1])

    def test_unregistered_task_or_foreign_owner_cannot_prepare(self):
        with self.assertRaises(PermissionError): tasks.prepare_reschedule(None, 'automation-9', self.payload())
        self.context['session'] = 'foreign'
        with self.assertRaisesRegex(PermissionError, 'original_native_owner_required'): self.prepare()
        self.assertEqual(self.records(), [None, None])

    def test_real_native_guard_rejects_foreign_hook_before_desktop_access(self):
        self.native_patch.stop()
        from activity_mvp import attention_native as native
        with patch.object(ClosureStore, 'recovery_caller', return_value={'platform': 'codex', 'session': 'foreign'}), patch.object(native, 'DesktopIPC') as ipc:
            with self.assertRaisesRegex(PermissionError, 'original_native_owner_required'): self.prepare()
            ipc.assert_not_called()
        self.native_patch.start()

    def test_missing_actual_read_is_not_replaced_by_payload_version(self):
        self.ledger.db.execute('DELETE FROM uses')
        with self.assertRaisesRegex(ValueError, 'actual_schedule_read_required'): self.prepare()
        self.assertEqual(self.records(), [None, None])

    def test_wrong_used_version_and_added_fields_rejected(self):
        for payload in ({**self.payload(), 'used_version': True}, {**self.payload(), 'used_version': 2},
                        {**self.payload(), 'rrule': 'FREQ=MINUTELY;COUNT=99'}):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError): tasks.prepare_reschedule(None, 'agi', payload)
        self.assertEqual(self.records(), [None, None])

    def test_bounded_time_rejects_boolean_seconds_past_and_more_than_two_days(self):
        for at in (True, float('nan'), self.at + 1, int(time.time()) - 60, (int(time.time()) // 60 + 49 * 60) * 60):
            with self.subTest(at=at):
                with self.assertRaises(ValueError): tasks.prepare_reschedule(None, 'agi', {**self.payload(), 'at': at})
        self.assertEqual(self.records(), [None, None])

    def test_generated_rule_is_beijing_single_occurrence(self):
        at = int(dt.datetime(2030, 1, 5, 0, 0, tzinfo=dt.timezone.utc).timestamp())
        self.assertEqual(tasks._schedule_rrule(at, at - 3600), 'FREQ=WEEKLY;BYDAY=SA;BYHOUR=8;BYMINUTE=0;BYSECOND=0;COUNT=1')

    def test_identical_rule_cannot_pretend_to_reset_count(self):
        self.metadata['rrule'] = tasks._schedule_rrule(self.at, time.time())
        self.binding['scheduler'] = dict(self.metadata); atomic(self.auth, packed(self.conf))
        with self.assertRaisesRegex(PermissionError, 'only_observable'): self.prepare()
        self.assertEqual(self.records(), [None, None])

    def test_original_infinite_or_multiple_count_is_not_reauthorized(self):
        self.metadata['rrule'] = 'FREQ=HOURLY;COUNT=2'; self.binding['scheduler'] = dict(self.metadata)
        atomic(self.auth, packed(self.conf))
        with self.assertRaisesRegex(PermissionError, 'original_single_active'): self.prepare()

    def test_case_decision_cancel_and_unknown_dispatch_are_not_bypassed(self):
        for state in ('decision', 'canceled'):
            with self.subTest(state=state):
                with ClosureStore(self.database) as s:
                    s.conn.execute('UPDATE closure_issues SET state=? WHERE id=?', (state, self.issue['id']))
                with self.assertRaisesRegex(PermissionError, 'waiting_stopped_or_dispatched'): self.prepare()
        with ClosureStore(self.database) as s:
            s.conn.execute("UPDATE closure_issues SET state='blocked',dispatch_state='unknown' WHERE id=?", (self.issue['id'],))
        with self.assertRaisesRegex(PermissionError, 'waiting_stopped_or_dispatched'): self.prepare()

    def test_same_owner_other_issue_cannot_replace_original_natural_issue(self):
        with ClosureStore(self.database) as s:
            s.conn.execute('UPDATE closure_issues SET fingerprint=? WHERE id=?', ('f' * 64, self.issue['id']))
        with self.assertRaisesRegex(PermissionError, 'original_bound_issue_required'): self.prepare()

    def test_issue_cancellation_during_native_update_blocks_commit(self):
        prepared = self.prepare(); self.native_update(prepared); self.state('canceled')
        with self.assertRaisesRegex(PermissionError, 'waiting_stopped_or_dispatched'): self.commit(prepared)
        self.assertIsNone(self.records()[1])

    def test_source_and_spec_version_drift_block_first_commit(self):
        prepared = self.prepare(); self.native_update(prepared)
        self.write({**self.receipt, 'at': 2})
        with self.assertRaisesRegex(ValueError, 'schedule_read_source_changed'): self.commit(prepared)
        self.write(self.receipt); self.ledger.db.execute('UPDATE tasks SET version=2')
        with self.assertRaisesRegex(ValueError, 'schedule_read_version_changed'): self.commit(prepared)
        self.assertIsNone(self.records()[1])

    def adopt_same_stage_source(self):
        other = self.sources / 'new-progress.json'; atomic(other, packed({**self.receipt, 'at': time.time()}))
        result = tasks.record_run(None, 'agi', {'used_version': self.read['spec']['version'],
                                              'source': str(other), 'archive_source': None, 'outcome': 'partial'})
        self.assertTrue(result['recorded'])
        self.assertEqual(self.ledger.db.execute('SELECT version FROM tasks').fetchone()[0], self.read['spec']['version'])

    def test_prepare_rejects_new_source_adopted_at_same_version_before_refresh(self):
        self.adopt_same_stage_source()
        with self.assertRaisesRegex(ValueError, 'schedule_read_latest_source_changed'): self.prepare()
        self.assertEqual(self.records(), [None, None])

    def test_first_commit_rejects_current_sources_replaced_without_semantic_version_change(self):
        prepared = self.prepare(); self.adopt_same_stage_source()
        self.assertFalse(tasks.refresh(self.ledger, 'agi', self.binding)['changed'])
        self.native_update(prepared)
        with self.assertRaisesRegex(ValueError, 'schedule_read_current_sources_changed'): self.commit(prepared)
        self.assertIsNone(self.records()[1])

    def test_authorization_fingerprint_cannot_be_rebound_through_schedule(self):
        prepared = self.prepare(); self.native_update(prepared)
        atomic(self.auth, packed({**self.conf, 'tampered': True}))
        with self.assertRaisesRegex(PermissionError, 'schedule_original_authorization_changed'): self.commit(prepared)
        with self.assertRaises(PermissionError): tasks.effective_scheduler(self.ledger, 'agi', self.binding)

    def test_intent_checksum_and_full_new_definition_checked_on_every_refresh(self):
        prepared = self.prepare(); self.native_update(prepared); self.commit(prepared)
        key = tasks._schedule_keys()[0]; intent = self.ledger.get(key)
        intent['body']['new_scheduler']['model'] = 'foreign'; intent['id'] = tasks.digest(intent['body'])
        self.ledger.set(key, intent)
        with self.assertRaises(PermissionError): tasks.refresh(self.ledger, 'agi', self.binding)

    def test_missing_intent_or_forged_commit_never_enables_new_definition(self):
        self.ledger.set(tasks._schedule_keys()[1], {'intent_id': 'f' * 64})
        with self.assertRaises(ValueError): tasks.effective_scheduler(self.ledger, 'agi', self.binding)
        self.assertEqual(tasks.effective_scheduler(self.ledger, 'automation-9', self.binding), self.binding['scheduler'])

    def test_wrong_intent_id_and_elapsed_first_commit_rejected(self):
        prepared = self.prepare(); self.native_update(prepared)
        with self.assertRaisesRegex(ValueError, 'intent_id_mismatch'):
            tasks.commit_reschedule(None, 'agi', {'intent_id': '0' * 64})
        with patch.object(tasks.time, 'time', return_value=self.at + 60):
            with self.assertRaisesRegex(ValueError, 'schedule_target_elapsed'): self.commit(prepared)
        self.assertIsNone(self.records()[1])

    def test_prepare_transaction_failure_rolls_back_without_clearing_history(self):
        real = tasks._append_schedule_record
        def fail(*args): real(*args); raise OSError('synthetic commit failure')
        frozen = self.frozen_tables()
        with patch.object(tasks, '_append_schedule_record', side_effect=fail):
            with self.assertRaises(OSError): self.prepare()
        self.assertEqual(self.records(), [None, None]); self.assertEqual(self.frozen_tables(), frozen)
        self.assertEqual(self.prepare()['state'], 'prepared')

    def test_commit_transaction_failure_is_recoverable_with_same_intent(self):
        prepared = self.prepare(); self.native_update(prepared); real = tasks._append_schedule_record
        def fail(*args): real(*args); raise OSError('synthetic commit failure')
        with patch.object(tasks, '_append_schedule_record', side_effect=fail):
            with self.assertRaises(OSError): self.commit(prepared)
        self.assertIsNone(self.records()[1]); self.assertTrue(self.commit(prepared)['committed'])

    def test_native_change_observed_inside_transaction_rejects_commit(self):
        prepared = self.prepare(); self.native_update(prepared)
        with patch.object(tasks, 'scheduler_metadata', side_effect=[dict(self.metadata), {**self.metadata, 'status': 'PAUSED'}]):
            with self.assertRaisesRegex(PermissionError, 'scheduler_changed_before_commit'): self.commit(prepared)
        self.assertIsNone(self.records()[1])

    def pause_during_native_read(self, _):
        with tasks.Ledger(self.ledger.root) as other: other.set('enabled', False)
        return dict(self.metadata)

    def test_pause_before_prepare_transaction_prevents_intent(self):
        with patch.object(tasks, 'scheduler_metadata', side_effect=self.pause_during_native_read):
            with self.assertRaisesRegex(PermissionError, 'maintenance_paused'): self.prepare()
        self.assertEqual(self.records(), [None, None])

    def test_pause_before_first_commit_transaction_prevents_receipt(self):
        prepared = self.prepare(); self.native_update(prepared)
        with patch.object(tasks, 'scheduler_metadata', side_effect=self.pause_during_native_read):
            with self.assertRaisesRegex(PermissionError, 'maintenance_paused'): self.commit(prepared)
        self.assertIsNone(self.records()[1])

    def delayed_transaction(self, at):
        original_tx = tasks.Ledger.tx
        @contextlib.contextmanager
        def delayed(ledger):
            with original_tx(ledger):
                with patch.object(tasks.time, 'time', return_value=at): yield
        return delayed

    def test_target_expired_while_entering_prepare_transaction_prevents_intent(self):
        with patch.object(tasks.Ledger, 'tx', self.delayed_transaction(self.at + 60)):
            with self.assertRaisesRegex(ValueError, 'bounded_future_minute_required'): self.prepare()
        self.assertEqual(self.records(), [None, None])

    def test_target_expired_while_entering_first_commit_transaction_prevents_receipt(self):
        prepared = self.prepare(); self.native_update(prepared)
        with patch.object(tasks.Ledger, 'tx', self.delayed_transaction(self.at + 60)):
            with self.assertRaisesRegex(ValueError, 'schedule_target_elapsed'): self.commit(prepared)
        self.assertIsNone(self.records()[1])

    def test_first_commit_receipt_records_time_after_transaction_acquired(self):
        prepared = self.prepare(); self.native_update(prepared)
        with patch.object(tasks.Ledger, 'tx', self.delayed_transaction(self.at - 1)):
            committed = self.commit(prepared)
        self.assertEqual(committed['receipt']['committed_at'], self.at - 1)

    def test_definition_changed_after_commit_is_still_rejected_by_later_entry(self):
        prepared = self.prepare(); self.native_update(prepared); self.commit(prepared)
        self.metadata['prompt_sha256'] = 'b' * 64
        with self.assertRaisesRegex(PermissionError, 'original_scheduler_definition_changed'):
            tasks._load_task_locked(self.ledger, 'agi', self.conf)

    def test_single_intent_lock_rejects_competing_prepare(self):
        with tasks.task_lock(self.ledger, 'agi'):
            with self.assertRaises(BlockingIOError): self.prepare()
        self.assertEqual(self.records(), [None, None])

    def test_natural_effect_already_accepted_does_not_prepare_another_recheck(self):
        self.write({**self.receipt, 'at': time.time(), 'natural_effectiveness_accepted': True})
        self.context['turn'] = '00000000-0000-0000-0000-000000000002'
        self.read = tasks._load_task_locked(self.ledger, 'agi', self.conf)
        with self.assertRaisesRegex(PermissionError, 'natural_effect_already_accepted'): self.prepare()
        self.assertEqual(self.records(), [None, None])

    def test_foreign_owner_cannot_commit_an_existing_original_intent(self):
        prepared = self.prepare(); self.native_update(prepared); self.context['session'] = 'foreign'
        with self.assertRaisesRegex(PermissionError, 'original_native_owner_required'): self.commit(prepared)
        self.assertIsNone(self.records()[1])

    def test_commit_chain_is_checked_instead_of_trusting_a_meta_flag(self):
        prepared = self.prepare(); self.native_update(prepared); self.commit(prepared)
        key = tasks._schedule_keys()[1]; receipt = self.ledger.get(key)
        for broken in ({**receipt, 'intent_id': '0' * 64}, {**receipt, 'committed_at': True},
                       {**receipt, 'scheduler_sha256': '0' * 64},
                       {**receipt, 'committed_by': {'session': 'foreign', 'turn': 'fixture'}}):
            with self.subTest(broken=broken):
                self.ledger.set(key, broken)
                with self.assertRaises(ValueError): tasks.refresh(self.ledger, 'agi', self.binding)
        self.ledger.set(key, receipt)
        self.assertFalse(tasks.refresh(self.ledger, 'agi', self.binding)['changed'])

    def test_concurrent_prepare_and_commit_keep_one_intent_and_one_receipt(self):
        def concurrent(action):
            barrier = threading.Barrier(2); results = []; errors = []
            def worker():
                barrier.wait()
                try: results.append(action())
                except (BlockingIOError, sqlite3.OperationalError) as exc: errors.append(type(exc).__name__)
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for t in threads: t.start()
            for t in threads: t.join(5)
            self.assertTrue(all(not t.is_alive() for t in threads)); self.assertGreaterEqual(len(results), 1)
            return results
        prepared = concurrent(self.prepare)
        self.assertEqual(len({x['intent_id'] for x in prepared}), 1)
        self.native_update(prepared[0]); committed = concurrent(lambda: self.commit(prepared[0]))
        self.assertEqual(len({packed(x['receipt']) for x in committed}), 1)
        keys = self.ledger.db.execute('SELECT key FROM meta WHERE key IN (?,?)', tasks._schedule_keys()).fetchall()
        self.assertEqual(len(keys), 2)


class RealScheduleAuthorityTests(unittest.TestCase):
    """Two real private feedback connections; no mocked authority/release gates."""
    def setUp(self):
        from activity_mvp.feedback.store import Store
        from activity_mvp.feedback import acceptance
        from activity_mvp.feedback.work import POLICY
        self.fixture = ScheduleAmendmentTests('test_formal_flow_restores_read_and_record_without_rewriting_originals')
        self.fixture.setUp(); f = self.fixture
        # Retain synthetic native/source fixtures, remove the successful authority mock.
        f.patches[-1].stop(); f.patches[-2].stop()
        f.conf['tasks']['automation-9'] = json.loads(packed(f.binding))
        atomic(f.auth, packed(f.conf))
        self.feedback = Store(f.root/'main-feedback'); self.other = Store(self.feedback.root)
        self.feedback.set('config', {'enabled': True})
        self.feedback.set('autonomy', {'policy': POLICY, 'paused': False})
        self.feedback.set('repair_capability', {'enabled': True, 'guards': acceptance.guard_hashes()})
        self.feedback.set('isolation', {'passed': True}); self.feedback.set('model_capability', {'enabled': True})
        self.feedback.set('evolution_maintenance', {'enabled': True, 'policy': tasks.POLICY,
                                                 'root': str(f.ledger.root), 'authorization_sha256': tasks.read_json(f.auth)[1]})
        self.assertEqual(tasks.authorized(self.feedback), (f.ledger.root, f.conf))

    def tearDown(self):
        self.other.close(); self.feedback.close(); self.fixture.tearDown()

    def revoke(self, gate):
        f = self.fixture; s = self.other
        if gate == 'feedback_pause': s.set('paused', True)
        elif gate == 'feedback_disabled': s.set('config', {'enabled': False})
        elif gate == 'stop': (s.root/'STOP').write_text('synthetic owner stop')
        elif gate == 'autonomy_pause': s.set('autonomy', {**s.get('autonomy'), 'paused': True})
        elif gate == 'autonomy_policy': s.set('autonomy', {**s.get('autonomy'), 'policy': 'revoked'})
        elif gate == 'maintenance_revoked': s.set('evolution_maintenance', {**s.get('evolution_maintenance'), 'enabled': False})
        elif gate == 'maintenance_removed': s.set('evolution_maintenance', {})
        elif gate == 'maintenance_root':
            from activity_mvp.feedback.store import private_directory
            root = private_directory(f.root/'other-authorized-root'); atomic(root/'authorization.json', packed(f.conf))
            s.set('evolution_maintenance', {**s.get('evolution_maintenance'), 'root': str(root)})
        elif gate in {'maintenance_conf', 'maintenance_raw_reencoded'}:
            conf = json.loads(f.auth.read_text())
            if gate == 'maintenance_conf': conf['tasks']['automation-9']['owner_session'] = 'different-owner'
            atomic(f.auth, json.dumps(conf, indent=2))
            s.set('evolution_maintenance', {**s.get('evolution_maintenance'), 'authorization_sha256': tasks.read_json(f.auth)[1]})
        elif gate == 'release_revoked': s.set('repair_capability', {**s.get('repair_capability'), 'enabled': False})
        elif gate == 'release_guards':
            cap = s.get('repair_capability'); cap['guards']['task_maintenance.py'] = '0'*64; s.set('repair_capability', cap)
        elif gate == 'isolation_revoked': s.set('isolation', {'passed': False})
        elif gate == 'model_revoked': s.set('model_capability', {'enabled': False})
        else: raise AssertionError(gate)

    def race(self, action, gate):
        f = self.fixture; prepared = None
        if action == 'commit':
            prepared = tasks.prepare_reschedule(self.feedback, 'agi', f.payload()); f.native_update(prepared)
        before = f.records(); frozen = f.frozen_tables(); calls = [0]
        def final_native_read(_):
            calls[0] += 1
            if calls[0] == 2: self.revoke(gate)
            return dict(f.metadata)
        with patch.object(tasks, 'scheduler_metadata', side_effect=final_native_read):
            with self.assertRaises(PermissionError):
                if action == 'prepare': tasks.prepare_reschedule(self.feedback, 'agi', f.payload())
                else: tasks.commit_reschedule(self.feedback, 'agi', {'intent_id': prepared['intent_id']})
        self.assertEqual(calls[0], 2); self.assertEqual(f.records(), before); self.assertEqual(f.frozen_tables(), frozen)
        self.assertIsNone(f.records()[1])

    def test_real_authority_allows_unchanged_prepare_and_commit(self):
        f = self.fixture; auth = f.auth.read_bytes(); frozen = f.frozen_tables()
        prepared = tasks.prepare_reschedule(self.feedback, 'agi', f.payload()); f.native_update(prepared)
        result = tasks.commit_reschedule(self.feedback, 'agi', {'intent_id': prepared['intent_id']})
        self.assertTrue(result['committed']); self.assertFalse(result['native_trigger_verified'])
        self.assertEqual(f.auth.read_bytes(), auth); self.assertEqual(f.frozen_tables(), frozen)

    def entry_identity_race(self, gate):
        f = self.fixture; before = f.records(); frozen = f.frozen_tables()
        def during_identity(*_):
            self.revoke(gate)
            return dict(f.context)
        with patch.object(tasks, 'native_context', side_effect=during_identity):
            with self.assertRaises(PermissionError): tasks.prepare_reschedule(self.feedback, 'agi', f.payload())
        self.assertEqual(f.records(), before); self.assertEqual(f.frozen_tables(), frozen)

    def test_rejects_entry_raw_reencoding_during_native_identity(self): self.entry_identity_race('maintenance_raw_reencoded')
    def test_rejects_entry_root_rebinding_during_native_identity(self): self.entry_identity_race('maintenance_root')
    def test_rejects_entry_conf_rebinding_during_native_identity(self): self.entry_identity_race('maintenance_conf')

    def authority_io_crosses_target(self, action):
        from activity_mvp.feedback import acceptance
        f = self.fixture; prepared = None
        if action == 'commit':
            prepared = tasks.prepare_reschedule(self.feedback, 'agi', f.payload()); f.native_update(prepared)
        before = f.records(); actual_guards = acceptance.guard_hashes; calls = [0]; now = [time.time()]
        def guard_io():
            result = actual_guards(); calls[0] += 1
            if calls[0] == 2: now[0] = f.at + 60
            return result
        # Run the real gate and real guard-file reads; only advance the synthetic clock.
        with patch.object(tasks.time, 'time', side_effect=lambda: now[0]), patch.object(acceptance, 'guard_hashes', side_effect=guard_io):
            with self.assertRaisesRegex(ValueError, 'bounded_future_minute_required|schedule_target_elapsed'):
                if action == 'prepare': tasks.prepare_reschedule(self.feedback, 'agi', f.payload())
                else: tasks.commit_reschedule(self.feedback, 'agi', {'intent_id': prepared['intent_id']})
        self.assertEqual(calls[0], 2); self.assertEqual(f.records(), before)

    def test_prepare_rechecks_time_after_real_authority_file_reads(self): self.authority_io_crosses_target('prepare')
    def test_commit_rechecks_time_after_real_authority_file_reads(self): self.authority_io_crosses_target('commit')

    def formal_control_order(self, action, command, first):
        from activity_mvp.feedback import cli, autonomy
        f = self.fixture; prepared = None
        if action == 'commit':
            prepared = tasks.prepare_reschedule(self.feedback, 'agi', f.payload()); f.native_update(prepared)
        key = tasks._schedule_keys()[0 if action == 'prepare' else 1]
        before = f.records(); frozen = f.frozen_tables()
        attempted = threading.Event(); done = threading.Event(); errors = []; owner_saw_record = []
        @contextlib.contextmanager
        def observed_owner_gate(root):
            attempted.set()
            with owner_gate(root):
                with tasks.Ledger(f.ledger.root, readonly=True) as current:
                    owner_saw_record.append(current.get(key) is not None)
                yield
        def owner_control():
            try:
                with patch.object(sys, 'argv', ['feedback', command, '--root', str(self.feedback.root)]), patch.object(cli, 'gate', observed_owner_gate):
                    cli.main()
            except BaseException as exc: errors.append(exc)
            finally: done.set()
        thread = threading.Thread(target=owner_control, daemon=True); started = [False]
        def start_owner():
            thread.start(); started[0] = True
        def operation():
            if action == 'prepare': return tasks.prepare_reschedule(self.feedback, 'agi', f.payload())
            return tasks.commit_reschedule(self.feedback, 'agi', {'intent_id': prepared['intent_id']})
        try:
            if first:
                @contextlib.contextmanager
                def after_owner(store):
                    start_owner(); self.assertTrue(done.wait(5)); self.assertEqual(errors, [])
                    with owner_gate(store.root): yield
                with patch.object(tasks, '_schedule_control', side_effect=after_owner, create=True):
                    with self.assertRaises(PermissionError): operation()
                self.assertEqual(f.records(), before); self.assertEqual(owner_saw_record, [False])
            else:
                real_append = tasks._append_schedule_record; held = []; owner_finished_before_append = []
                def short_append(ledger, append_key, value):
                    fd = os.open(self.feedback.root/'control.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                    try:
                        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB); held.append(False)
                        except BlockingIOError: held.append(True)
                    finally: os.close(fd)
                    start_owner(); self.assertTrue(attempted.wait(5))
                    # Make the older unprotected window deterministic, without sleeping.
                    if not held[-1]: self.assertTrue(done.wait(5))
                    owner_finished_before_append.append(done.is_set())
                    real_append(ledger, append_key, value)
                with patch.object(tasks, '_append_schedule_record', side_effect=short_append): result = operation()
                self.assertTrue(result.get('committed') or result.get('state') == 'prepared')
                thread.join(5); self.assertFalse(thread.is_alive()); self.assertEqual(errors, [])
                self.assertEqual(held, [True]); self.assertEqual(owner_finished_before_append, [False])
                self.assertEqual(owner_saw_record, [True])
            self.assertTrue(done.is_set()); self.assertEqual(errors, [])
            self.assertFalse(autonomy.active(self.feedback)); self.assertEqual(f.frozen_tables(), frozen)
        finally:
            if started[0]: thread.join(5)
            self.assertFalse(thread.is_alive())

    def failure_releases_owner_control(self, action):
        f = self.fixture; prepared = None
        if action == 'commit':
            prepared = tasks.prepare_reschedule(self.feedback, 'agi', f.payload()); f.native_update(prepared)
        before = f.records(); real_append = tasks._append_schedule_record
        def fail(*args): real_append(*args); raise OSError('synthetic failure before ledger commit')
        with patch.object(tasks, '_append_schedule_record', side_effect=fail):
            with self.assertRaises(OSError):
                if action == 'prepare': tasks.prepare_reschedule(self.feedback, 'agi', f.payload())
                else: tasks.commit_reschedule(self.feedback, 'agi', {'intent_id': prepared['intent_id']})
        self.assertEqual(f.records(), before)
        fd = os.open(self.feedback.root/'control.lock', os.O_RDWR | os.O_NOFOLLOW)
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally: os.close(fd)
        self.formal_control_order(action, 'pause', True)

    def test_prepare_rollback_releases_real_owner_gate(self): self.failure_releases_owner_control('prepare')
    def test_commit_rollback_releases_real_owner_gate(self): self.failure_releases_owner_control('commit')


def _real_gate_race(action, gate):
    def verify(self): self.race(action, gate)
    return verify


for _action in ('prepare', 'commit'):
    for _gate in ('feedback_pause', 'feedback_disabled', 'stop', 'autonomy_pause', 'autonomy_policy',
                  'maintenance_revoked', 'maintenance_removed', 'maintenance_root', 'maintenance_conf',
                  'maintenance_raw_reencoded', 'release_revoked', 'release_guards', 'isolation_revoked', 'model_revoked'):
        setattr(RealScheduleAuthorityTests, f'test_{_action}_rejects_real_{_gate}_before_append', _real_gate_race(_action, _gate))


def _formal_control_case(action, command, first):
    def verify(self): self.formal_control_order(action, command, first)
    return verify


for _action in ('prepare', 'commit'):
    for _command in ('pause', 'stop', 'autonomy-pause'):
        for _first in (True, False):
            _order = 'wins_before_gate' if _first else 'waits_for_commit'
            setattr(RealScheduleAuthorityTests, f'test_{_action}_formal_{_command.replace("-", "_")}_{_order}',
                    _formal_control_case(_action, _command, _first))


if __name__ == '__main__': unittest.main()
