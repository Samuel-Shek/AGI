import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from activity_mvp.feedback import task_maintenance as tasks
from activity_mvp.feedback import evolution_notices as notices
from activity_mvp.feedback.store import atomic, packed


class TaskMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name).resolve()
        self.sources = self.root / 'sources'; self.sources.mkdir()
        self.source = self.sources / 'current.json'
        self.receipt = {'at': 1, 'engineering_and_installation_complete': True,
                        'natural_effectiveness_accepted': False, 'external_archive_complete': False}
        self.write(self.receipt)
        self.metadata = {'kind': 'heartbeat', 'status': 'ACTIVE', 'rrule': 'unchanged', 'target_thread_id': 'original'}
        self.binding = {'source': str(self.source), 'archive_source': None, 'scheduler': dict(self.metadata),
                        'owner_session': 'original', 'closure_tasks': ['a' * 32]}
        self.patches = [patch.object(tasks, 'source_root', return_value=self.sources),
                        patch.object(tasks, 'scheduler_metadata', side_effect=lambda _: dict(self.metadata))]
        for p in self.patches: p.start()
        self.ledger = tasks.Ledger(self.root / 'private')
    def tearDown(self):
        self.ledger.close()
        for p in reversed(self.patches): p.stop()
        self.temp.cleanup()
    def write(self, value): atomic(self.source, packed(value))
    def test_change_updates_spec_but_no_change_keeps_exact_version_and_one_event(self):
        first = tasks.refresh(self.ledger, 'agi', self.binding)
        original = self.ledger.db.execute('SELECT spec FROM tasks').fetchone()[0]
        self.write({**self.receipt, 'at': 2})
        self.assertFalse(tasks.refresh(self.ledger, 'agi', self.binding)['changed'])
        self.assertEqual(original, self.ledger.db.execute('SELECT spec FROM tasks').fetchone()[0])
        self.write({**self.receipt, 'at': 3, 'natural_effectiveness_accepted': True})
        second = tasks.refresh(self.ledger, 'agi', self.binding)
        self.assertEqual((first['version'], second['version']), (1, 2))
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM events').fetchone()[0], 2)
    def test_source_missing_does_not_replace_spec(self):
        tasks.refresh(self.ledger, 'agi', self.binding); self.source.unlink()
        with self.assertRaises(OSError): tasks.refresh(self.ledger, 'agi', self.binding)
        self.assertEqual(self.ledger.db.execute('SELECT version FROM tasks').fetchone()[0], 1)
    def test_refresh_cannot_lower_observed_source_high_water(self):
        self.write({**self.receipt, 'at': 100}); tasks.refresh(self.ledger, 'agi', self.binding)
        self.write({**self.receipt, 'at': 80})
        with self.assertRaisesRegex(ValueError, 'older_progress_source'):
            tasks.refresh(self.ledger, 'agi', self.binding)
        self.assertEqual(self.ledger.get('validated_sources:agi')['observed_at'], 100)
        self.assertEqual(self.ledger.get('source_observation_high_water:agi'), 100)
    def test_pause_cancel_and_schedule_change_never_update(self):
        tasks.refresh(self.ledger, 'agi', self.binding)
        self.metadata['status'] = 'PAUSED'
        with self.assertRaises(PermissionError): tasks.refresh(self.ledger, 'agi', self.binding)
        self.metadata['status'] = 'CANCELLED'
        with self.assertRaises(PermissionError): tasks.refresh(self.ledger, 'agi', self.binding)
        self.metadata['status'] = 'ACTIVE'; self.metadata['rrule'] = 'changed'
        # Binding is a copy of the frozen native fields.
        frozen = {**self.binding, 'scheduler': {**self.metadata, 'rrule': 'unchanged'}}
        with self.assertRaises(PermissionError): tasks.refresh(self.ledger, 'agi', frozen)
    def test_source_race_keeps_previous_version(self):
        tasks.refresh(self.ledger, 'agi', self.binding)
        self.write({**self.receipt, 'natural_effectiveness_accepted': True})
        real = tasks.checked_source; count = [0]
        def racing(*args):
            count[0] += 1
            if count[0] == 2: self.write({**self.receipt, 'natural_effectiveness_accepted': False})
            return real(*args)
        with patch.object(tasks, 'checked_source', side_effect=racing):
            with self.assertRaises(ValueError): tasks.refresh(self.ledger, 'agi', self.binding)
        self.assertEqual(self.ledger.db.execute('SELECT version FROM tasks').fetchone()[0], 1)
    def test_interleaved_newer_task_cannot_be_overwritten(self):
        tasks.refresh(self.ledger, 'agi', self.binding)
        self.write({**self.receipt, 'natural_effectiveness_accepted': True})
        real = tasks.checked_source; count = [0]
        def racing(*args):
            count[0] += 1
            if count[0] == 2: self.ledger.db.execute('UPDATE tasks SET version=7')
            return real(*args)
        with patch.object(tasks, 'checked_source', side_effect=racing):
            with self.assertRaisesRegex(ValueError, 'progress_changed'): tasks.refresh(self.ledger, 'agi', self.binding)
        self.assertEqual(self.ledger.db.execute('SELECT version FROM tasks').fetchone()[0], 7)
    def test_fake_completion_and_date_alone_are_insufficient(self):
        stages = {'engineering': True, 'whole_complete': True, 'help_cases': 0, 'remaining': 0,
                  'earliest': '2020-01-01', 'archive': True}
        self.assertFalse(tasks.retirement_eligible(self.binding, 'automation-9', stages))
        self.assertFalse(tasks.retirement_eligible(self.binding, 'agi', {'engineering': True, 'archive': True, 'natural_effect': False}))
    def test_registry_retirement_is_not_archive_readback(self):
        receipt = {'verified_help_cases': 0, 'remaining': [], 'original_earliest_time_lower_bound': '2026-10-13',
                   'source_app_live_pair': True, 'controller_ready': True, 'whole_plan_complete': False,
                   'original_registry_retired': True}
        self.assertFalse(tasks.normalize('automation-9', receipt)['archive'])
        receipt['original_earliest_time_lower_bound'] = '2026-10-01'
        with self.assertRaisesRegex(PermissionError, 'earliest_date_changed'): tasks.normalize('automation-9', receipt)
    def test_alias_hardlink_and_duplicate_json_rejected(self):
        alias = self.sources / 'alias.json'; alias.symlink_to(self.source)
        with self.assertRaises(OSError): tasks.read_json(alias)
        hard = self.sources / 'hard.json'; os.link(self.source, hard)
        with self.assertRaises(PermissionError): tasks.read_json(hard)
        hard.unlink(); self.source.write_text('{"at":1,"at":2}')
        with self.assertRaises(ValueError): tasks.read_json(self.source)
    def test_outside_source_is_not_a_grant(self):
        with self.assertRaises(PermissionError): tasks.checked_source('agi', self.root / 'other.json')
    def test_search_only_directory_traversal_keeps_ancestor_alias_rejected(self):
        target=self.root/'real-directory';target.mkdir(mode=0o700)
        file=target/'source.json';file.write_text('{"safe":true}')
        self.assertEqual(tasks.read_file(file),b'{"safe":true}')
        alias=self.root/'directory-alias';alias.symlink_to(target,target_is_directory=True)
        with self.assertRaises(OSError):tasks.read_file(alias/'source.json')
    def test_unregistered_task_rejected(self):
        with self.assertRaises(PermissionError):
            with tasks.task_lock(self.ledger, 'not-registered'): pass
    def _use(self, turn='turn', created=1):
        key = tasks.digest(['agi', 'original', turn])
        spec = {'task': 'agi', 'version': 1}; _, ref = tasks.checked_source('agi', self.source)
        self.ledger.db.execute('INSERT INTO uses VALUES(?,?,?,?,?,?,?,NULL)', (key, 'agi', 'original', turn, 1, tasks.digest(spec), created))
        self.ledger.db.execute('INSERT INTO use_sources VALUES(?,?,?)', (key, packed([ref]), packed(spec)))
        return key
    def _payload(self, outcome='partial'):
        return {'used_version': 1, 'source': str(self.source), 'archive_source': None, 'outcome': outcome}
    def _conf(self): return {'tasks': {'agi': self.binding}}
    def test_run_requires_actual_read_and_preserves_paused_state(self):
        context = {'session': 'original', 'turn': 'turn'}
        with patch.object(tasks, 'authorized', return_value=(self.ledger.root, self._conf())), patch.object(tasks, 'native_context', return_value=context):
            with self.assertRaises(PermissionError): tasks.record_run(None, 'agi', self._payload())
            self.ledger.set('enabled', True)
            with self.assertRaisesRegex(ValueError, 'actual_loaded'): tasks.record_run(None, 'agi', self._payload())
            self._use(); self.ledger.set('enabled', False)
            with self.assertRaises(PermissionError): tasks.record_run(None, 'agi', self._payload())
    def test_two_different_receipts_for_one_turn_cannot_both_commit(self):
        self.ledger.set('enabled', True); key = self._use(); errors = []; accepted = []
        context = {'session': 'original', 'turn': 'turn'}; start = threading.Barrier(2)
        def worker(outcome):
            start.wait()
            try: accepted.append(tasks.record_run(None, 'agi', self._payload(outcome)))
            except (ValueError, OSError) as exc: errors.append(type(exc).__name__)
        with patch.object(tasks, 'authorized', return_value=(self.ledger.root, self._conf())), patch.object(tasks, 'native_context', return_value=context):
            threads = [threading.Thread(target=worker, args=(x,)) for x in ['partial', 'blocked']]
            for thread in threads: thread.start()
            for thread in threads: thread.join(5)
        self.assertEqual(len(accepted), 1); self.assertEqual(len(errors), 1)
        saved = json.loads(self.ledger.db.execute('SELECT result FROM uses WHERE id=?', (key,)).fetchone()[0])
        self.assertEqual(saved['source'], self.ledger.get('latest_source:agi')['source'])
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM events').fetchone()[0], 1)
    def test_older_run_cannot_replace_newer_recorded_result(self):
        old = self._use(); newer = self._use('newer', 2)
        self.ledger.db.execute('UPDATE uses SET result=? WHERE id=?', (packed({'outcome': 'partial'}), newer))
        receipt, ref = tasks.checked_source('agi', self.source); stages = tasks.normalize('agi', receipt)
        with self.assertRaisesRegex(ValueError, 'newer_native'):
            tasks._record_run_locked(self.ledger, self._conf(), 'agi', {'session': 'original', 'turn': 'turn'}, self._payload(), stages, ref, None)
        self.assertIsNone(self.ledger.db.execute('SELECT result FROM uses WHERE id=?', (old,)).fetchone()[0])
    def test_newer_spec_withdraws_old_run_adoption(self):
        tasks.refresh(self.ledger, 'agi', self.binding); key = self._use()
        self.write({**self.receipt, 'at': 2, 'natural_effectiveness_accepted': True})
        tasks.refresh(self.ledger, 'agi', self.binding)
        receipt, ref = tasks.checked_source('agi', self.source)
        with self.assertRaisesRegex(ValueError, 'current_task_version_changed'):
            tasks._record_run_locked(self.ledger, self._conf(), 'agi', {'session': 'original', 'turn': 'turn'}, self._payload(), tasks.normalize('agi', receipt), ref, None)
        self.assertIsNone(self.ledger.db.execute('SELECT result FROM uses WHERE id=?', (key,)).fetchone()[0])
    def test_complete_stages_alone_do_not_authorize_native_retirement(self):
        self.ledger.set('enabled', True); self._use()
        with patch.object(tasks, 'retirement_eligible', return_value=True):
            self.assertFalse(tasks.native_retirement_ready(self.ledger, 'agi', self._conf(), {'session': 'original', 'turn': 'turn'}))
    def test_export_stays_bound_to_database_and_lock_blocks_parallel_old_export(self):
        tasks.refresh(self.ledger, 'agi', self.binding)
        with tasks.task_lock(self.ledger, 'agi'):
            with self.assertRaises(BlockingIOError): tasks.refresh(self.ledger, 'agi', self.binding)
        db = json.loads(self.ledger.db.execute('SELECT spec FROM tasks').fetchone()[0])
        exported, _ = tasks.read_json(self.ledger.root / 'tasks/agi.json')
        self.assertEqual(db, exported)
    def test_actual_native_read_binds_full_spec_and_current_source_fingerprint(self):
        with patch.object(tasks, 'native_context', return_value={'session': 'original', 'turn': 'turn'}):
            result = tasks._load_task_locked(self.ledger, 'agi', self._conf())
        self.assertTrue(result['native_use_recorded'])
        saved = self.ledger.db.execute('SELECT sources,spec FROM use_sources WHERE id=?', (result['use_id'],)).fetchone()
        self.assertEqual(json.loads(saved['sources']), result['current_sources'])
        self.assertEqual(json.loads(saved['spec']), result['spec'])
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM task_versions').fetchone()[0], 1)
    def test_read_by_other_session_is_observation_without_native_use(self):
        with patch.object(tasks, 'native_context', side_effect=PermissionError('wrong_owner')):
            result = tasks._load_task_locked(self.ledger, 'agi', self._conf())
        self.assertFalse(result['native_use_recorded'])
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM uses').fetchone()[0], 0)
    def test_real_closure_store_uses_activity_database_anchor(self):
        from activity_mvp.task_closure import Store as ClosureStore
        from activity_mvp import attention_native as native
        database = self.root / 'data/activity.sqlite'; database.parent.mkdir()
        with ClosureStore(database) as closure: expected = closure.path
        self.assertEqual(expected, database.parent / 'attention-center/closure.sqlite')
        context = {'platform': 'codex', 'session': 'original', 'turn': 'fixture', 'task_id': 'hook-task'}
        snapshot = {'source': 'desktop_owner', 'activity': 'active', 'turn_status': 'inProgress',
                    'turn_id': '00000000-0000-0000-0000-000000000001', 'current_turn_matches': True, 'observed_at': time.time()}
        with patch.object(tasks, 'closure_database', return_value=database), patch.object(ClosureStore, 'recovery_caller', return_value=context), patch.object(native, 'DesktopIPC') as ipc:
            ipc.return_value.__enter__.return_value.snapshot.return_value = snapshot
            actual = tasks.native_context('agi', self._conf())
            self.assertEqual(actual['turn'], snapshot['turn_id'])
            self.assertEqual(actual['hook_turn'], context['turn'])
            self.assertEqual(actual['hook_task_id'], 'hook-task')
            self.assertNotIn('task_id', actual)

    def test_stale_hook_root_allows_next_turn_to_load_new_spec_without_rewriting_first_use(self):
        from activity_mvp.task_closure import Store as ClosureStore
        from activity_mvp import attention_native as native
        context = {'platform': 'codex', 'session': 'original', 'turn': 'old-hook', 'task_id': 'old-hook-task'}
        snapshot = {'source': 'desktop_owner', 'activity': 'active', 'turn_status': 'inProgress',
                    'turn_id': '00000000-0000-0000-0000-000000000001', 'current_turn_matches': True, 'observed_at': time.time()}
        with patch.object(tasks, 'closure_database', return_value=self.root / 'data/activity.sqlite'), patch.object(ClosureStore, 'recovery_caller', return_value=context), patch.object(native, 'DesktopIPC') as ipc:
            with ClosureStore(self.root / 'data/activity.sqlite'): pass
            channel = ipc.return_value.__enter__.return_value
            channel.snapshot.side_effect = lambda *a, **kw: dict(snapshot)
            first = tasks._load_task_locked(self.ledger, 'agi', self._conf())
            first_saved = dict(self.ledger.db.execute('SELECT * FROM uses WHERE id=?', (first['use_id'],)).fetchone())
            self.write({**self.receipt, 'at': time.time(), 'natural_effectiveness_accepted': True})
            with self.assertRaisesRegex(ValueError, 'native_turn_already_loaded_other_version'):
                tasks._load_task_locked(self.ledger, 'agi', self._conf())
            snapshot['turn_id'] = '00000000-0000-0000-0000-000000000002'
            snapshot['observed_at'] = time.time()
            second = tasks._load_task_locked(self.ledger, 'agi', self._conf())
            self.assertEqual((first['spec']['version'], second['spec']['version']), (1, 2))
            self.assertNotEqual(first['use_id'], second['use_id'])
            self.assertEqual(first_saved, dict(self.ledger.db.execute('SELECT * FROM uses WHERE id=?', (first['use_id'],)).fetchone()))
            self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM uses').fetchone()[0], 2)
            self.assertTrue(all(c.kwargs['current_turn'] for c in channel.snapshot.call_args_list))

    def test_missing_idle_stale_future_or_wrong_owner_metadata_cannot_certify_native_use(self):
        from activity_mvp.task_closure import Store as ClosureStore
        from activity_mvp import attention_native as native
        context = {'platform': 'codex', 'session': 'original', 'turn': 'old-hook'}
        valid = {'source': 'desktop_owner', 'activity': 'active', 'turn_status': 'inProgress',
                 'turn_id': '00000000-0000-0000-0000-000000000001', 'current_turn_matches': True, 'observed_at': time.time()}
        database = self.root / 'data/activity.sqlite'
        with ClosureStore(database): pass
        with patch.object(tasks, 'closure_database', return_value=database), patch.object(ClosureStore, 'recovery_caller', return_value=context), patch.object(native, 'DesktopIPC') as ipc:
            channel = ipc.return_value.__enter__.return_value
            for invalid in ({'activity': 'idle'}, {'turn_status': 'completed'}, {'source': 'app_server'},
                            {'current_turn_matches': False}, {'turn_id': None}, {'observed_at': time.time()-31},
                            {'observed_at': time.time()+2}, {'observed_at': True}):
                with self.subTest(invalid=invalid):
                    channel.snapshot.return_value = {**valid, **invalid}
                    result = tasks._load_task_locked(self.ledger, 'agi', self._conf())
                    self.assertFalse(result['native_use_recorded'])
            channel.snapshot.side_effect = native.NativeError('codex_snapshot_timeout')
            self.assertFalse(tasks._load_task_locked(self.ledger, 'agi', self._conf())['native_use_recorded'])
            context['session'] = 'other-owner'; ipc.reset_mock()
            with self.assertRaisesRegex(PermissionError, 'original_native_owner_required'):
                tasks.native_context('agi', self._conf())
            ipc.assert_not_called()
            self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM uses').fetchone()[0], 0)


class FakeClient:
    def __init__(self): self.sent = []; self.messages = {}; self.unknown = False; self.reject = False; self.readfail = False
    def preflight(self, target): return {'private': True}
    def send(self, target, text, uid):
        self.sent.append((text, uid)); self.messages['real-id'] = text
        if self.reject: raise notices.RejectedWrite(230006)
        if self.unknown: raise notices.UnknownWrite('timeout')
        return 'real-id'
    def get_message(self, mid):
        if self.readfail: raise notices.ReadUnavailable('offline')
        return {'text': self.messages[mid]}
    def matches(self, message, target, text): return message == {'text': text}
    def reconcile(self, target, text, attempted):
        return {'candidate_message_id': 'real-id', 'original_request_verified': True} if self.messages.get('real-id') == text else None


class NoticeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.ledger = tasks.Ledger(Path(self.temp.name).resolve() / 'private')
        self.target_patch = patch.object(notices, 'target_for', return_value={'chat_id': 'only-group'}); self.target_patch.start()
        self.event = {'kind':'functional_verified','work_id':'1'*24,'job':'2'*24,'contract':'text_blocks','revision':'3'*40,'proof':'4'*64}
        self.proof_patch = patch.object(notices,'current_functional',return_value=True); self.proof_patch.start()
        self.client = FakeClient()
    def tearDown(self): self.proof_patch.stop(); self.target_patch.stop(); self.ledger.close(); self.temp.cleanup()
    def state(self): return self.ledger.db.execute('SELECT state FROM events').fetchone()[0]
    def test_exact_three_sections_basis_and_privacy(self):
        text = notices.compile_functional(self.event)
        self.assertEqual(len(text.splitlines()),2)
        self.assertTrue(text.startswith('做了什么：')); self.assertIn('\n为什么做：',text)
        self.assertIn('安装后检查',text); self.assertIn('尚未验收',text)
        for private in ['客户名称', '/Users/private', 'password', 'oc_private']:
            with self.assertRaises(ValueError): notices.compile_batch([{**self.event, 'raw': private}])
        with self.assertRaises(ValueError): notices.compile_batch([{'kind': 'source_error', 'task': '客户名称'}])
    def test_dedup_and_real_id_full_readback(self):
        self.ledger.event(self.event); self.ledger.event(self.event)
        result = notices.deliver_pending(self.ledger, self.client)
        self.assertTrue(result['readback']); self.assertEqual(self.state(), 'delivered')
        notices.deliver_pending(self.ledger, self.client)
        self.assertEqual(len(self.client.sent), 1)
        self.assertEqual(self.ledger.db.execute('SELECT message_id FROM events').fetchone()[0], 'real-id')
    def test_timeout_reconciles_original_request_without_resend(self):
        self.ledger.event(self.event); self.client.unknown = True
        self.assertEqual(notices.deliver_pending(self.ledger, self.client)['status'], 'unknown')
        self.assertEqual(self.state(), 'unknown'); self.client.unknown = False
        self.assertTrue(notices.deliver_pending(self.ledger, self.client)['reconciled'])
        self.assertEqual(len(self.client.sent), 1)
    def test_unknown_without_match_never_replayed(self):
        self.ledger.event(self.event); self.client.unknown = True
        notices.deliver_pending(self.ledger, self.client); self.client.messages.clear()
        for _ in range(3): self.assertEqual(notices.deliver_pending(self.ledger, self.client)['status'], 'unknown')
        self.assertEqual(len(self.client.sent), 1)
    def test_killed_dispatch_does_not_resend(self):
        key = self.ledger.event(self.event); text = notices.compile_functional(self.event)
        receipt = {'text': text, 'events': [key], 'target_sha256': self.ledger.get('target_sha256')}
        self.ledger.db.execute("UPDATE events SET state='dispatch_started',attempted=1,receipt=?", (packed(receipt),))
        self.assertEqual(notices.deliver_pending(self.ledger, self.client)['status'], 'unknown')
        self.assertEqual(len(self.client.sent), 0)
    def test_readback_failure_does_not_resend(self):
        self.ledger.event(self.event); self.client.readfail = True
        self.assertEqual(notices.deliver_pending(self.ledger, self.client)['status'], 'readback_pending')
        self.client.readfail = False
        self.assertTrue(notices.deliver_pending(self.ledger, self.client)['readback'])
        self.assertEqual(len(self.client.sent), 1)
    def test_matching_body_alone_does_not_resolve_unknown_original_request(self):
        self.ledger.event(self.event); self.client.unknown = True
        notices.deliver_pending(self.ledger, self.client)
        with patch.object(self.client, 'reconcile', return_value={'candidate_message_id': 'similar-id', 'original_request_verified': False}):
            result = notices.deliver_pending(self.ledger, self.client)
        self.assertEqual(result['status'], 'unknown'); self.assertEqual(self.state(), 'unknown')
        self.assertTrue(result['matching_candidate_only']); self.assertEqual(len(self.client.sent), 1)
    def test_explicit_remote_reject_is_not_dispatched_and_delays_retry(self):
        self.ledger.event(self.event); self.client.reject = True
        self.assertEqual(notices.deliver_pending(self.ledger, self.client)['status'], 'not_dispatched')
        self.assertEqual(self.state(), 'pending')
        self.client.reject = False
        self.assertEqual(notices.deliver_pending(self.ledger, self.client)['status'], 'quiet')
        self.assertEqual(len(self.client.sent), 1)
    def test_group_membership_change_blocks_before_post(self):
        self.ledger.event(self.event)
        with patch.object(self.client, 'preflight', side_effect=PermissionError('member_changed')):
            with self.assertRaises(PermissionError): notices.deliver_pending(self.ledger, self.client)
        self.assertEqual(len(self.client.sent), 0); self.assertEqual(self.state(), 'pending')
    def test_progress_is_local_and_stale_proof_never_sends(self):
        self.ledger.event({'kind':'baseline','identity_restored':True,'paired_installed':True})
        self.ledger.event(self.event)
        with patch.object(notices,'current_functional',return_value=False):
            self.assertEqual(notices.deliver_pending(self.ledger,self.client)['status'],'quiet')
        self.assertEqual(len(self.client.sent),0)
        states={r[0] for r in self.ledger.db.execute('SELECT state FROM events')}
        self.assertEqual(states,{'pending','local_only'})
    def test_each_delivery_contains_one_improvement(self):
        self.ledger.event(self.event)
        self.ledger.event({**self.event,'job':'5'*24,'contract':'timestamp_format'})
        self.assertEqual(notices.deliver_pending(self.ledger,self.client)['count'],1)
        self.assertEqual(len(self.client.sent),1)
        self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM events WHERE state='pending'").fetchone()[0],1)
    def test_foreign_sender_and_target_never_match(self):
        client = object.__new__(notices.Client); client.app_id = 'app'
        target = {'chat_id': 'group', 'bot_open_id': 'bot'}
        message = {'chat_id': 'group', 'msg_type': 'text', 'body': {'content': packed({'text': 'safe'})}, 'sender': {'sender_type': 'app', 'id': 'app'}}
        self.assertTrue(client.matches(message, target, 'safe'))
        message['sender']['id'] = 'other'
        self.assertFalse(client.matches(message, target, 'safe'))
        message['sender']['id'] = 'app'; message['chat_id'] = 'other'
        self.assertFalse(client.matches(message, target, 'safe'))


if __name__ == '__main__': unittest.main()
