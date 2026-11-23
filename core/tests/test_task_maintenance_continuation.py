"""Synthetic continuation protocol: no native scheduler or production database."""
import contextlib
import copy
import ast
import fcntl
import hashlib
import json
import shlex
import time
import unittest
from unittest.mock import patch

from activity_mvp.feedback import task_maintenance as tasks
from activity_mvp.feedback.store import packed
from activity_mvp.task_closure import Store as ClosureStore
from activity_mvp.task_closure import digest as closure_digest
from tests.closure_legacy_fixture import legacy_begin
from tests import test_task_maintenance_reschedule as reschedule_tests


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.f = reschedule_tests.ScheduleAmendmentTests()
        if hasattr(self, 'owner_session'): self.f.owner_session = self.owner_session
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        home = patch.object(tasks.Path, 'home', return_value=self.f.root)
        home.start(); self.addCleanup(home.stop)
        old = self.f.prepare(); self.f.native_update(old); self.f.commit(old)
        self.synthetic_request = '请继续处理原任务，完成文件追踪和自然验收，只在需要本人决定时通知本人。'
        source = self.source = {
            'platform': 'codex', 'session': '00000000-0000-0000-0000-000000000011',
            'turn': '00000000-0000-0000-0000-000000000012',
            'prompt_hash': hashlib.sha256(self.synthetic_request.encode()).hexdigest()}
        source['task_id'] = closure_digest([source['platform'], source['session'], source['turn']])[:32]
        with ClosureStore(self.f.database) as closure:
            identifier = legacy_begin(closure, {
                'platform': source['platform'], 'session': source['session'], 'turn': source['turn'],
                'objective': 'synthetic inspected human continuation', 'scope_root': str(self.f.root),
                'constraints': [], 'complex': False})['task_id']
            self.assertEqual(identifier, source['task_id'])
            closure.conn.execute('INSERT INTO closure_request_sources VALUES(?,?,?,?,?,?,?)',
                                 (identifier, source['platform'], source['session'], source['turn'],
                                  source['prompt_hash'], 2, time.time()))
            closure.conn.execute('CREATE TABLE IF NOT EXISTS closure_hook_roots(platform TEXT,session TEXT,turn TEXT,task_id TEXT,prompt_hash TEXT,PRIMARY KEY(platform,session))')
            closure.conn.execute('INSERT OR REPLACE INTO closure_hook_roots VALUES(?,?,?,?,?)',
                                 ('codex', source['session'], source['turn'], identifier, source['prompt_hash']))
        self.native_records = [
            {'type': 'session_meta', 'payload': {'id': source['session'], 'source': 'vscode'}},
            {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': source['turn'],
             'root_turn_id': source['turn'], 'turn_attribution': {'turn_id': source['turn'],
             'root_turn_id': source['turn'], 'turn_trigger': 'composer', 'parent_turn_id': None, 'initiating_agent_path': None}}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
             'content': [{'type': 'input_text', 'text': self.synthetic_request}],
             'internal_chat_message_metadata_passthrough': {'turn_id': source['turn'], 'create_time': time.time(),
             'content_item_kinds': ['user.text'], 'content_item_metadata': [{'provenance': {'type': 'user'}}]}}},
            {'type': 'event_msg', 'payload': {'type': 'item_completed', 'thread_id': source['session'],
             'turn_id': source['turn'], 'item': {'type': 'UserMessage', 'id': 'synthetic-human', 'client_id': 'synthetic-client',
             'content': [{'type': 'text', 'text': self.synthetic_request}]}}},
            {'type': 'event_msg', 'payload': {'type': 'item_completed', 'thread_id': source['session'],
             'turn_id': source['turn'], 'item': {'type': 'McpToolCall', 'id': 'synthetic-route',
             'server': 'codex_app', 'tool': 'send_message_to_thread', 'status': 'completed',
             'arguments': {'threadId': self.f.binding['owner_session']},
             'result': {'isError': False, 'content': [{'type': 'text', 'text': json.dumps({'threadId': self.f.binding['owner_session']})}]}}}},
        ]
        self.native_path = self.f.root / '.codex/sessions/2026/01/01' / ('rollout-synthetic-' + source['session'] + '.jsonl')
        self.native_path.parent.mkdir(parents=True); self.write_native()
        self.original_records = copy.deepcopy(self.f.records())
        self.original_auth = self.f.auth.read_bytes()
        self.original_native = self.f.native_file.read_bytes()

    def write_native(self):
        self.native_path.write_text(''.join(json.dumps(row, ensure_ascii=False, separators=(',', ':'))+'\n'
                                          for row in self.native_records))

    def payload(self):
        return {'used_version': self.f.read['spec']['version'], 'issue_id': self.f.issue['id'],
                'source_task_id': self.source['task_id']}

    def prepare(self): return tasks.prepare_continuation(None, 'agi', self.payload())
    def commit(self, intent): return tasks.commit_continuation(None, 'agi', {'intent_id': intent['intent_id']})
    def records(self): return [self.f.ledger.get(k) for k in tasks._continuation_keys()]
    def native_update(self, intent): self.f.metadata = dict(intent['expected_scheduler'])

    def new_turn(self, number):
        self.f.context['turn'] = '00000000-0000-0000-0000-' + str(number).zfill(12)
        self.f.read = tasks._load_task_locked(self.f.ledger, 'agi', self.f.conf)
        return self.f.read

    def assert_originals(self):
        self.assertEqual(self.f.records(), self.original_records)
        self.assertEqual(self.f.auth.read_bytes(), self.original_auth)
        self.assertEqual(self.f.native_file.read_bytes(), self.original_native)

    def test_formal_flow_preserves_originals_and_does_not_modify_native(self):
        old = self.f.frozen_tables(); prepared = self.prepare()
        self.assertTrue(prepared['native_update_required']); self.assertFalse(prepared['scheduler_modified'])
        self.assertEqual(self.f.frozen_tables(), old)
        self.native_update(prepared)
        self.assertEqual(self.commit(prepared)['state'], 'committed')
        self.assertEqual(self.f.frozen_tables(), old)
        self.assertEqual(tasks.effective_scheduler(self.f.ledger, 'agi', self.f.binding), self.f.metadata)
        self.assert_originals()

    def test_phase_unchanged_still_creates_exactly_one_new_spec_version(self):
        version = self.f.read['spec']['version']
        old_versions = [tuple(x) for x in self.f.ledger.db.execute('SELECT * FROM task_versions')]
        prepared = self.prepare(); self.native_update(prepared); self.commit(prepared)
        read = self.new_turn(2)
        self.assertEqual(read['spec']['version'], version + 1)
        self.assertIn('schedule_continuation', read['spec'])
        self.assertNotIn('原目标、时间、模型', read['spec']['boundary'])
        self.assertEqual(self.new_turn(3)['spec'], read['spec'])
        self.assertEqual([tuple(x) for x in self.f.ledger.db.execute('SELECT * FROM task_versions WHERE version<=?', (version,))], old_versions)
        result = tasks.record_run(None, 'agi', {
            'used_version': read['spec']['version'], 'source': str(self.f.source),
            'archive_source': None, 'outcome': 'partial'})
        self.assertTrue(result['recorded']); self.assertFalse(result['retirement_eligible'])

    def test_repeated_prepare_and_commit_only_reconcile_same_intent(self):
        prepared = self.prepare(); again = self.prepare()
        self.assertEqual(again['intent_id'], prepared['intent_id'])
        self.assertTrue(again['reconcile_original_native_update_only'])
        self.assertNotIn('native_update_required', again)
        with self.assertRaisesRegex(PermissionError, 'readback_mismatch'): self.commit(prepared)
        self.native_update(prepared); first = self.commit(prepared); second = self.commit(prepared)
        self.assertEqual(first['receipt'], second['receipt']); self.assertTrue(second['idempotent'])
        self.assertFalse(first['native_trigger_verified']); self.assert_originals()

    def test_source_progress_between_native_update_and_commit_recovers_same_intent(self):
        prepared = self.prepare(); self.native_update(prepared)
        self.f.write({**self.f.receipt, 'at': 2, 'engineering_and_installation_complete': False})
        with self.assertRaises(ValueError): self.commit(prepared)
        self.assertIsNone(self.records()[1])
        # Ordinary background refresh remains closed, but one owner read can
        # load genuinely current evidence in a new native turn for reconciliation.
        with self.assertRaisesRegex(PermissionError, 'definition_changed'):
            tasks.refresh(self.f.ledger, 'agi', self.f.binding)
        read = self.new_turn(2)
        self.assertTrue(read['native_use_recorded']); self.assertTrue(read['schedule_continuation']['pending_commit'])
        self.assertFalse(read['spec']['stages']['engineering'])
        self.assertEqual(self.prepare()['intent_id'], prepared['intent_id'])
        result = self.commit(prepared)
        self.assertEqual(result['receipt']['read']['used_version'], read['spec']['version'])
        self.assertEqual(self.records()[0]['id'], prepared['intent_id']); self.assert_originals()

    def test_pending_native_update_does_not_allow_record_or_retirement(self):
        prepared = self.prepare()
        self.assertFalse(tasks.native_retirement_ready(self.f.ledger, 'agi', self.f.conf, self.f.context))
        with self.assertRaisesRegex(PermissionError, 'commit_pending'):
            tasks.record_run(None, 'agi', {'used_version': self.f.read['spec']['version'],
                             'source': str(self.f.source), 'archive_source': None, 'outcome': 'partial'})
        self.native_update(prepared); self.new_turn(2)
        with self.assertRaisesRegex(PermissionError, 'commit_pending'):
            tasks.record_run(None, 'agi', {'used_version': self.f.read['spec']['version'],
                             'source': str(self.f.source), 'archive_source': None, 'outcome': 'complete'})
        self.assertFalse(tasks.native_retirement_ready(self.f.ledger, 'agi', self.f.conf, self.f.context))

    def test_pending_recovery_read_requires_the_original_native_owner(self):
        prepared = self.prepare(); self.native_update(prepared)
        self.f.context['session'] = 'other'
        with self.assertRaisesRegex(PermissionError, 'original_native_owner'):
            tasks._load_task_locked(self.f.ledger, 'agi', self.f.conf)
        self.assertIsNone(self.records()[1])

    def test_prompt_keeps_future_authorized_repair_with_original_task(self):
        prompt = self.prepare()['prompt']
        self.assertIn(str(self.f.root / 'Library/Application Support/PSI/core'), prompt)
        self.assertNotIn('/Users/', prompt)
        self.assertIn('没有正在执行的同一修复时，由原agi任务', prompt)
        self.assertIn('不要求本人盯进度或另开聊天', prompt)
        self.assertIn('superseded_file_events_merged', prompt)
        self.assertIn('不误当新故障或自然帮助', prompt)

    def test_runtime_home_is_safely_quoted_without_executing_the_entry(self):
        quoted_home = self.f.root / "owner's work folder"
        with patch.object(tasks.Path, 'home', return_value=quoted_home):
            entry = shlex.split(tasks._continuation_prompt().splitlines()[1])
        self.assertEqual(entry[1:4], ['-I', '-S', '-c'])
        self.assertEqual(entry[-3:], ['read', '--task', 'agi'])
        tree = ast.parse(entry[4])
        insert = next(node.value for node in tree.body if isinstance(node, ast.Expr)
                      and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute)
                      and node.value.func.attr == 'insert')
        self.assertEqual(ast.literal_eval(insert.args[1]),
                         str(quoted_home / 'Library/Application Support/PSI/core'))

    def test_exact_quiet_preference_normalizes_alias_and_covers_original_missing_policy(self):
        prepared = self.prepare(); expected = prepared['expected_scheduler']
        self.assertEqual(expected['notification_policy'], 'failed_runs_only')
        self.assertNotIn('notificationPolicy', expected)
        previous = self.records()[0]['body']['old_scheduler']
        missing = {key: value for key, value in previous.items()
                   if key not in {'notification_policy', 'notificationPolicy'}}
        proposal = tasks._continuation_scheduler(missing)
        self.assertEqual(proposal['notification_policy'], 'failed_runs_only')
        self.assertEqual({key: value for key, value in proposal.items()
                          if key not in {'rrule', 'prompt_sha256', 'notification_policy'}},
                         {key: value for key, value in missing.items() if key not in {'rrule', 'prompt_sha256'}})
        self.native_update(prepared); self.assertTrue(self.commit(prepared)['native_readback_verified'])

    def test_conflicting_notification_aliases_and_nonexact_native_preference_are_rejected(self):
        with self.assertRaisesRegex(PermissionError, 'notification_alias_conflict'):
            tasks._continuation_scheduler({'notificationPolicy': None, 'notification_policy': 'failed_runs_only'})
        prepared = self.prepare(); self.native_update(prepared)
        self.f.metadata['notification_policy'] = 'all'
        with self.assertRaisesRegex(PermissionError, 'readback_mismatch'): self.commit(prepared)
        self.assertIsNone(self.records()[1])

    def test_same_turn_cannot_replace_its_loaded_version_after_commit(self):
        prepared = self.prepare(); self.native_update(prepared); self.commit(prepared)
        with self.assertRaisesRegex(ValueError, 'already_loaded_other_version'):
            tasks._load_task_locked(self.f.ledger, 'agi', self.f.conf)
        self.assertTrue(self.new_turn(2)['native_use_recorded'])

    def test_native_source_missing_or_changed_fails_without_any_intent(self):
        with ClosureStore(self.f.database) as closure:
            closure.conn.execute('UPDATE closure_request_sources SET prompt_hash=? WHERE task_id=?',
                                 ('0' * 64, self.source['task_id']))
        with self.assertRaisesRegex(PermissionError, 'native_source_(?:mismatch|superseded)'): self.prepare()
        self.assertEqual(self.records(), [None, None]); self.assert_originals()

    def test_native_source_policy_future_time_or_task_mismatch_is_rejected(self):
        for column, value in [('policy', 1), ('at', time.time() + 120), ('session', 'another')]:
            with self.subTest(column=column), ClosureStore(self.f.database) as closure:
                row = dict(closure.conn.execute('SELECT * FROM closure_request_sources WHERE task_id=?',
                                                (self.source['task_id'],)).fetchone())
                closure.conn.execute('UPDATE closure_request_sources SET '+column+'=? WHERE task_id=?',
                                     (value, row['task_id']))
                with self.assertRaises(PermissionError): self.prepare()
                closure.conn.execute('UPDATE closure_request_sources SET '+column+'=? WHERE task_id=?',
                                     (row[column], row['task_id']))
        self.assertEqual(self.records(), [None, None])

    def test_self_claim_fields_or_other_task_do_not_grant_authority(self):
        for extra in [{'authorized': True}, {'rrule': tasks.CONTINUATION_RRULE},
                      {'source': dict(self.source)}, {'model': 'other'}]:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                tasks.prepare_continuation(None, 'agi', {**self.payload(), **extra})
        with self.assertRaises(PermissionError): tasks.prepare_continuation(None, 'automation-9', self.payload())
        self.f.context['session'] = 'other'
        with self.assertRaisesRegex(PermissionError, 'original_native_owner'): self.prepare()
        self.assertEqual(self.records(), [None, None])

    def test_unprepared_native_change_and_nonexact_new_definition_are_rejected(self):
        self.f.metadata['rrule'] = tasks.CONTINUATION_RRULE
        with self.assertRaisesRegex(PermissionError, 'definition_changed'): self.prepare()
        self.assertEqual(self.records(), [None, None])

    def test_paused_definition_after_prepare_is_never_restored(self):
        prepared = self.prepare(); self.native_update(prepared); self.f.metadata['status'] = 'PAUSED'
        with self.assertRaisesRegex(PermissionError, 'readback_mismatch'): self.commit(prepared)
        with self.assertRaisesRegex(PermissionError, 'definition_changed'): self.new_turn(2)
        self.assertEqual(self.f.metadata['status'], 'PAUSED'); self.assertIsNone(self.records()[1])

    def test_maintenance_pause_blocks_prepare_and_commit(self):
        self.f.ledger.set('enabled', False)
        with self.assertRaisesRegex(PermissionError, 'maintenance_paused'): self.prepare()
        self.f.ledger.set('enabled', True); prepared = self.prepare(); self.native_update(prepared)
        self.f.ledger.set('enabled', False)
        with self.assertRaisesRegex(PermissionError, 'maintenance_paused'): self.commit(prepared)
        self.assertIsNone(self.records()[1])

    def test_natural_effect_already_accepted_cannot_start_a_continuation(self):
        self.f.write({**self.f.receipt, 'at': 2, 'natural_effectiveness_accepted': True})
        self.new_turn(2)
        with self.assertRaisesRegex(PermissionError, 'already_accepted'): self.prepare()
        self.assertEqual(self.records(), [None, None])

    def test_held_lock_is_not_cleared(self):
        with open(self.f.ledger.root/'agi.lock', 'a') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError): self.prepare()
        self.assertEqual(self.records(), [None, None])

    def test_source_revoked_inside_gate_is_rechecked_before_append(self):
        @contextlib.contextmanager
        def revoke():
            with ClosureStore(self.f.database) as closure:
                closure.conn.execute('DELETE FROM closure_request_sources WHERE task_id=?',
                                     (self.source['task_id'],))
            yield
        with patch.object(tasks, '_schedule_control', side_effect=lambda _: revoke()):
            with self.assertRaises(PermissionError): self.prepare()
        self.assertEqual(self.records(), [None, None])

    def test_real_authorized_root_conf_and_raw_hash_rechecked_inside_gate(self):
        for changed in ('root', 'conf', 'hash'):
            @contextlib.contextmanager
            def revoke():
                conf = copy.deepcopy(self.f.conf)
                if changed == 'conf': conf['unexpected'] = True
                root = self.f.root if changed == 'root' else self.f.ledger.root
                sha = '0' * 64 if changed == 'hash' else tasks.read_json(self.f.auth)[1]
                with patch.object(tasks, 'authorized', return_value=(root, conf, sha)):
                    yield
            with self.subTest(changed=changed), patch.object(tasks, '_schedule_control', side_effect=lambda _: revoke()):
                with self.assertRaises(PermissionError): self.prepare()
            self.assertEqual(self.records(), [None, None])

    def test_commit_source_revoked_inside_gate_does_not_append(self):
        prepared = self.prepare(); self.native_update(prepared)
        @contextlib.contextmanager
        def revoke():
            with ClosureStore(self.f.database) as closure:
                closure.conn.execute('UPDATE closure_request_sources SET prompt_hash=? WHERE task_id=?',
                                     ('0' * 64, self.source['task_id']))
            yield
        with patch.object(tasks, '_schedule_control', side_effect=lambda _: revoke()):
            with self.assertRaises(PermissionError): self.commit(prepared)
        self.assertIsNone(self.records()[1]); self.assert_originals()

    def test_cancelled_original_case_is_rejected(self):
        self.f.state('canceled')
        with self.assertRaises(PermissionError): self.prepare()
        self.assertEqual(self.records(), [None, None])

    def test_prepared_intent_cannot_be_rehashed_to_change_prompt_or_model(self):
        self.prepare(); intent = copy.deepcopy(self.records()[0])
        intent['body']['new_scheduler']['model'] = 'changed'
        intent['id'] = tasks.digest(intent['body']); self.f.ledger.set(tasks._continuation_keys()[0], intent)
        with self.assertRaisesRegex(PermissionError, 'exact_scheduler'):
            tasks.effective_scheduler(self.f.ledger, 'agi', self.f.binding)

    def test_policy_and_hash_only_row_without_native_file_does_not_authorize(self):
        self.native_path.unlink()
        with self.assertRaisesRegex(PermissionError, 'native_file_ambiguous'): self.prepare()
        self.assertEqual(self.records(), [None, None])

    def test_real_native_user_pair_and_primary_root_are_required(self):
        original = copy.deepcopy(self.native_records)
        changes = [
            (2, ('payload', 'role'), 'assistant'),
            (2, ('payload', 'internal_chat_message_metadata_passthrough', 'content_item_kinds'), ['tool.text']),
            (2, ('payload', 'internal_chat_message_metadata_passthrough', 'content_item_metadata'), [{'provenance': {'type': 'agent'}}]),
            (3, ('payload', 'item', 'client_id'), ''),
            (3, ('payload', 'thread_id'), 'unrelated'),
            (1, ('payload', 'turn_attribution', 'parent_turn_id'), 'parent'),
            (1, ('payload', 'turn_attribution', 'initiating_agent_path'), '/agent'),
            (1, ('payload', 'root_turn_id'), 'unrelated'),
        ]
        for index, path, value in changes:
            with self.subTest(path=path):
                self.native_records = copy.deepcopy(original); node = self.native_records[index]
                for key in path[:-1]: node = node[key]
                node[path[-1]] = value; self.write_native()
                with self.assertRaises(PermissionError): self.prepare()
                self.assertEqual(self.records(), [None, None])

    def test_unrelated_native_source_cannot_grant_without_original_task_delivery(self):
        self.native_records[4]['payload']['item']['arguments']['threadId'] = 'other-task'
        self.write_native()
        with self.assertRaisesRegex(PermissionError, 'original_task_route_required'): self.prepare()
        self.assertEqual(self.records(), [None, None])

    def test_route_must_be_in_the_same_originating_native_turn(self):
        self.native_records[4]['payload']['turn_id'] = '00000000-0000-0000-0000-000000000080'
        self.write_native()
        with self.assertRaisesRegex(PermissionError, 'original_task_route_required'): self.prepare()
        self.assertEqual(self.records(), [None, None])

    def test_same_turn_label_after_native_turn_end_cannot_supply_route(self):
        self.native_records.insert(4, {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': self.source['turn']}})
        self.write_native()
        with self.assertRaisesRegex(PermissionError, 'original_task_route_required'): self.prepare()
        self.assertEqual(self.records(), [None, None])

    def test_quoted_or_delegated_text_is_not_a_human_continuation_request(self):
        for text in ['> '+self.synthetic_request, '<codex_delegation>'+self.synthetic_request,
                     '请解释“'+self.synthetic_request+'”',
                     '请继续处理模拟原任务，完成文件追踪和自然验收，只在需要本人决定时通知本人。',
                     '请继续处理原任务，完成文件追踪和自然验收，需要本人决定并通知本人每步进度。',
                     '请继续处理原任务的文件追踪和自然验收方案，完成分析，只在需要本人决定时通知本人。不要执行。',
                     '请继续处理原任务的文件追踪和自然验收是否需要完成？需要本人决定时通知本人。']:
            with self.subTest(kind=text[:8]), self.assertRaises(PermissionError):
                tasks._continuation_request(text)

    def test_fresh_source_superseded_before_prepare_is_rejected(self):
        self.append_later_human()
        with self.assertRaises(PermissionError): self.prepare()
        self.assertEqual(self.records(), [None, None])

    def append_later_human(self):
        turn = '00000000-0000-0000-0000-000000000099'
        prior = copy.deepcopy(self.native_records[2]); user = copy.deepcopy(self.native_records[3])
        text = '这是一条不改变既有授权的合成普通消息。'
        prior['payload']['content'][0]['text'] = text
        prior['payload']['internal_chat_message_metadata_passthrough']['turn_id'] = turn
        user['payload']['turn_id'] = turn; user['payload']['item']['id'] = 'later-human'
        user['payload']['item']['content'][0]['text'] = text
        with self.native_path.open('a') as stream:
            for row in (prior, user): stream.write(json.dumps(row, ensure_ascii=False)+'\n')
        with ClosureStore(self.f.database) as closure:
            closure.conn.execute('UPDATE closure_hook_roots SET turn=?,task_id=?,prompt_hash=? WHERE session=?',
                                 (turn, closure_digest(['codex', self.source['session'], turn])[:32],
                                  hashlib.sha256(text.encode()).hexdigest(), self.source['session']))

    def test_frozen_exact_source_survives_normal_later_messages_and_append(self):
        prepared = self.prepare(); proof = copy.deepcopy(self.records()[0]['body']['native_proof'])
        self.native_update(prepared); self.append_later_human()
        self.assertTrue(self.commit(prepared)['committed'])
        self.assertEqual(self.records()[0]['body']['native_proof'], proof)
        self.assertTrue(self.new_turn(2)['native_use_recorded'])

    def test_frozen_original_record_rewrite_is_rejected(self):
        prepared = self.prepare(); self.native_update(prepared)
        self.native_records[3]['payload']['item']['client_id'] = 'rewritten-client'
        self.write_native()
        with self.assertRaisesRegex(PermissionError, 'frozen_native_record_changed'): self.commit(prepared)
        self.assertIsNone(self.records()[1])

    def test_exact_record_run_waiting_for_lock_sees_intervening_prepare(self):
        original_lock = tasks.task_lock
        @contextlib.contextmanager
        def intervening_prepare(ledger, task):
            with patch.object(tasks, 'task_lock', original_lock): self.prepare()
            with original_lock(ledger, task): yield
        with patch.object(tasks, 'task_lock', side_effect=intervening_prepare):
            with self.assertRaisesRegex(PermissionError, 'continuation_commit_pending'):
                tasks.record_run(None, 'agi', {'used_version': self.f.read['spec']['version'],
                    'source': str(self.f.source), 'archive_source': None, 'outcome': 'partial'})
        self.assertIsNotNone(self.records()[0]); self.assertIsNone(self.records()[1])
        row = self.f.ledger.db.execute('SELECT result FROM uses WHERE id=?', (self.f.read['use_id'],)).fetchone()
        self.assertIsNone(row['result'])

    def test_already_recorded_native_run_cannot_cross_first_prepare(self):
        tasks.record_run(None, 'agi', {'used_version': self.f.read['spec']['version'],
            'source': str(self.f.source), 'archive_source': None, 'outcome': 'partial'})
        with self.assertRaisesRegex(ValueError, 'run_already_recorded'): self.prepare()
        self.assertEqual(self.records(), [None, None])
        self.new_turn(2); self.assertTrue(self.prepare()['native_update_required'])


if __name__ == '__main__': unittest.main()
