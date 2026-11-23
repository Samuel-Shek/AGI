"""Isolated fresh-human renewal; never changes a production grant/scheduler."""
import contextlib
import copy
import hashlib
import json
import os
import time
import unittest
from unittest.mock import patch

from activity_mvp.feedback import task_maintenance as tasks
from activity_mvp.task_closure import Store as ClosureStore, digest as closure_digest
from tests.closure_legacy_fixture import legacy_begin
from tests import test_task_maintenance_continuation as fixtures


class SourceRenewalTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ContinuationTests()
        self.fixture.owner_session = '00000000-0000-0000-0000-000000000101'
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture.f
        prepared = self.fixture.prepare(); self.fixture.native_update(prepared); self.fixture.commit(prepared)
        self.original = copy.deepcopy(self.fixture.records())
        self.snapshot = self.f.frozen_tables()
        self.volume = '00000000-0000-0000-0000-000000000131'
        mock = patch.object(tasks, '_native_volume_uuid', side_effect=lambda _: self.volume)
        mock.start(); self.addCleanup(mock.stop)
        native_file = tasks._continuation_native_file
        @contextlib.contextmanager
        def device_drift(session):
            with native_file(session) as (fd, info):
                if session == self.fixture.source['session']:
                    parts = list(info); parts[2] += 1; info = os.stat_result(parts)
                yield fd, info
        mock = patch.object(tasks, '_continuation_native_file', side_effect=device_drift)
        mock.start(); self.addCleanup(mock.stop)
        self.source = {'platform': 'codex', 'session': self.f.binding['owner_session'],
                       'turn': '00000000-0000-0000-0000-000000000102'}
        self.person(self.fixture.synthetic_request)
        self.payload = {'renew_native_source': True, 'intent_id': self.original[0]['id'],
                        'scheduler_sha256': tasks.digest(self.f.metadata), 'source_task_id': self.source['task_id']}

    def person(self, text, *, trigger='composer', provenance='user'):
        source = self.source
        source['prompt_hash'] = hashlib.sha256(text.encode()).hexdigest()
        source['task_id'] = closure_digest(['codex', source['session'], source['turn']])[:32]
        records = copy.deepcopy(self.fixture.native_records[:4])
        records[0]['payload']['id'] = source['session']
        root = records[1]['payload']; root['turn_id'] = root['root_turn_id'] = source['turn']
        root['turn_attribution'].update(turn_id=source['turn'], root_turn_id=source['turn'], turn_trigger=trigger)
        message = records[2]['payload']; message['content'][0]['text'] = text
        message['internal_chat_message_metadata_passthrough'].update(turn_id=source['turn'], create_time=time.time(),
            content_item_metadata=[{'provenance': {'type': provenance}}])
        event = records[3]['payload']; event.update(thread_id=source['session'], turn_id=source['turn'])
        event['item']['content'][0]['text'] = text
        if source['session'] != self.f.binding['owner_session']:
            route = copy.deepcopy(self.fixture.native_records[4])
            route['payload'].update(thread_id=source['session'], turn_id=source['turn'])
            records.append(route)
        self.path = self.fixture.native_path.with_name('rollout-synthetic-' + source['session'] + '.jsonl')
        self.path.write_text(''.join(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n' for row in records))
        with ClosureStore(self.f.database) as closure:
            if not closure.conn.execute('SELECT 1 FROM closure_tasks WHERE id=?', (source['task_id'],)).fetchone():
                legacy_begin(closure, {'platform': 'codex', 'session': source['session'], 'turn': source['turn'],
                    'objective': 'synthetic current original owner', 'scope_root': str(self.f.root), 'constraints': [], 'complex': False})
            closure.conn.execute('INSERT OR REPLACE INTO closure_request_sources VALUES(?,?,?,?,?,?,?)',
                (source['task_id'], 'codex', source['session'], source['turn'], source['prompt_hash'], 2, time.time()))
            closure.conn.execute('INSERT OR REPLACE INTO closure_hook_roots VALUES(?,?,?,?,?)',
                ('codex', source['session'], source['turn'], source['task_id'], source['prompt_hash']))
        if source['session'] == self.f.binding['owner_session']:
            self.f.context.update(session=source['session'], turn=source['turn'], hook_task_id=source['task_id'])

    def renew(self, payload=None):
        return tasks._renew_native_source_locked(None, self.f.ledger, 'agi', self.f.conf,
                                                self.payload if payload is None else payload)

    def assert_originals(self):
        self.assertEqual(self.fixture.records(), self.original)
        self.fixture.assert_originals()

    def test_ordinary_read_and_legacy_proof_still_reject_device_drift(self):
        with self.assertRaisesRegex(PermissionError, 'frozen_native_proof_invalid'):
            tasks._load_task_locked(self.f.ledger, 'agi', self.f.conf)
        self.assertIsNone(self.f.ledger.get(tasks._source_renewal_key()))
        self.assert_originals()

    def test_one_explicit_fresh_human_append_enables_real_read_without_rewriting_history(self):
        first = self.renew(); second = self.renew()
        self.assertTrue(first['recorded']); self.assertFalse(first['idempotent']); self.assertTrue(second['idempotent'])
        self.assertEqual(first['receipt'], second['receipt']); self.assertFalse(first['original_proof_renewed'])
        self.assertEqual(self.f.frozen_tables(), self.snapshot)  # append is not native use
        self.assert_originals()
        read = tasks._load_task_locked(self.f.ledger, 'agi', self.f.conf)
        self.assertTrue(read['native_use_recorded']); self.assertFalse(read['spec']['stages']['natural_effect'])
        self.assertEqual(read['spec']['schedule_continuation']['native_source_renewal_id'], first['receipt']['id'])
        self.assertEqual(read['schedule_continuation']['source_renewal'], first['receipt'])
        self.assertEqual(read['current_schedule'], self.f.metadata)
        self.assertEqual(set(first['receipt']['body']['native_proof']['records']), {'header', 'root', 'prior', 'user'})

    def test_question_heartbeat_delegation_and_quoted_grants_never_mint(self):
        for text, trigger, provenance in [
            ('接下来干啥', 'composer', 'user'),
            (self.fixture.synthetic_request, 'heartbeat', 'user'),
            (self.fixture.synthetic_request, 'composer', 'tool'),
            ('<codex_delegation>' + self.fixture.synthetic_request, 'composer', 'user'),
            ('请解释“' + self.fixture.synthetic_request + '”', 'composer', 'user'),
        ]:
            with self.subTest(trigger=trigger, provenance=provenance, text=text):
                self.person(text, trigger=trigger, provenance=provenance)
                with self.assertRaises(PermissionError): self.renew()
                self.assertIsNone(self.f.ledger.get(tasks._source_renewal_key()))

    def test_self_claim_and_nonexact_target_or_schedule_are_rejected(self):
        for change in [{'authorized': True}, {'used_version': 99}, {'intent_id': '0' * 64},
                       {'scheduler_sha256': '0' * 64}, {'renew_native_source': 1}]:
            with self.subTest(change=change), self.assertRaises((PermissionError, ValueError)):
                self.renew({**self.payload, **change})
        self.assertIsNone(self.f.ledger.get(tasks._source_renewal_key()))

    def test_existing_cross_chat_human_and_successful_native_route_can_renew(self):
        self.source['session'] = '00000000-0000-0000-0000-000000000111'
        self.person(self.fixture.synthetic_request)
        self.payload['source_task_id'] = self.source['task_id']
        result = self.renew()
        self.assertTrue(result['recorded'])
        self.assertIn('route', result['receipt']['body']['native_proof']['records'])
        self.assertEqual(result['receipt']['body']['native_source']['session'], self.source['session'])
        self.assertEqual(tasks.effective_scheduler(self.f.ledger, 'agi', self.f.binding), self.f.metadata)
        self.assert_originals()

    def test_cross_chat_without_successful_exact_route_never_renews(self):
        self.source['session'] = '00000000-0000-0000-0000-000000000111'
        self.person(self.fixture.synthetic_request)
        self.payload['source_task_id'] = self.source['task_id']
        records = self.path.read_text().splitlines(keepends=True)
        for route in [None, {'isError': True}, {'isError': False, 'content': [{'type': 'text', 'text': '{}'}]}]:
            lines = records[:-1]
            if route is not None:
                row = json.loads(records[-1]); row['payload']['item']['result'] = route
                lines.append(json.dumps(row) + '\n')
            self.path.write_text(''.join(lines))
            with self.assertRaisesRegex(PermissionError, 'original_task_route_required'): self.renew()
            self.assertIsNone(self.f.ledger.get(tasks._source_renewal_key()))

    def test_new_volume_or_inode_or_frozen_bytes_fail_after_renewal(self):
        saved = self.renew()['receipt']; self.volume = '00000000-0000-0000-0000-000000000132'
        with self.assertRaises(PermissionError): tasks.effective_scheduler(self.f.ledger, 'agi', self.f.binding)
        self.volume = saved['body']['native_proof']['volume_uuid']
        body = self.path.read_bytes(); self.path.unlink(); self.path.write_bytes(body)
        with self.assertRaises(PermissionError): tasks.effective_scheduler(self.f.ledger, 'agi', self.f.binding)
        self.assert_originals()

    def test_prospective_volume_proof_survives_device_number_drift_without_rewriting_proof(self):
        saved = self.renew()['receipt']; native_file = tasks._continuation_native_file
        @contextlib.contextmanager
        def next_boot(session):
            with native_file(session) as (fd, info):
                if session == self.source['session']:
                    parts = list(info); parts[2] += 2; info = os.stat_result(parts)
                yield fd, info
        with patch.object(tasks, '_continuation_native_file', side_effect=next_boot):
            self.assertEqual(tasks.effective_scheduler(self.f.ledger, 'agi', self.f.binding), self.f.metadata)
        self.assertEqual(self.f.ledger.get(tasks._source_renewal_key()), saved)
        self.assert_originals()

    def test_uuid_unavailable_has_no_device_or_path_fallback(self):
        with patch.object(tasks, '_native_volume_uuid', side_effect=PermissionError('uuid_unavailable')):
            with self.assertRaisesRegex(PermissionError, 'uuid_unavailable'): self.renew()
        self.assertIsNone(self.f.ledger.get(tasks._source_renewal_key()))

    def test_changed_original_bytes_cannot_be_repaired_by_new_source(self):
        path = self.fixture.native_path
        path.write_bytes(path.read_bytes().replace(b'synthetic-client', b'synthetic-other!'))
        with self.assertRaisesRegex(PermissionError, 'frozen_native_record_changed'): self.renew()
        self.assertIsNone(self.f.ledger.get(tasks._source_renewal_key()))

    def test_changed_original_inode_cannot_mint(self):
        path = self.fixture.native_path; raw = path.read_bytes(); path.unlink(); path.write_bytes(raw)
        with self.assertRaisesRegex(PermissionError, 'frozen_native_proof_invalid'): self.renew()
        self.assertIsNone(self.f.ledger.get(tasks._source_renewal_key()))

    def test_control_race_revocation_and_old_read_history_are_not_bypassed(self):
        with patch.object(tasks, '_schedule_authority_current', side_effect=PermissionError('revoked')):
            with self.assertRaisesRegex(PermissionError, 'revoked'): self.renew()
        self.assertIsNone(self.f.ledger.get(tasks._source_renewal_key()))
        self.f.ledger.db.execute('DELETE FROM use_sources WHERE id=?', (self.original[0]['body']['read_use_id'],))
        with self.assertRaisesRegex(ValueError, 'actual_schedule_read_required'): self.renew()
        self.assertIsNone(self.f.ledger.get(tasks._source_renewal_key()))

    def test_pause_or_native_schedule_change_rolls_back_append(self):
        def pause(*args):
            self.f.ledger.set('enabled', False)
            raise PermissionError('synthetic_commit_failure')
        with patch.object(tasks, '_append_schedule_record', side_effect=pause):
            with self.assertRaisesRegex(PermissionError, 'commit_failure'): self.renew()
        self.assertTrue(self.f.ledger.get('enabled')); self.assertIsNone(self.f.ledger.get(tasks._source_renewal_key()))
        self.f.metadata['status'] = 'PAUSED'
        with self.assertRaisesRegex(PermissionError, 'readback_mismatch'): self.renew()
        self.assertIsNone(self.f.ledger.get(tasks._source_renewal_key()))

    def test_superseded_native_source_and_changed_turn_cannot_append(self):
        with patch.object(tasks, '_continuation_source_is_current', side_effect=PermissionError('source_superseded')):
            with self.assertRaisesRegex(PermissionError, 'source_superseded'): self.renew()
        with patch.object(tasks, 'native_context', side_effect=[dict(self.f.context), {**self.f.context, 'turn': 'changed'}]):
            with self.assertRaisesRegex(PermissionError, 'turn_changed'): self.renew()
        self.assertIsNone(self.f.ledger.get(tasks._source_renewal_key()))

    def test_pause_at_serial_gate_and_source_change_at_final_check_cannot_append(self):
        @contextlib.contextmanager
        def pause(_):
            self.f.metadata['status'] = 'PAUSED'
            yield
        with patch.object(tasks, '_schedule_control', side_effect=pause):
            with self.assertRaisesRegex(PermissionError, 'scheduler_changed_before_append'): self.renew()
        self.f.metadata['status'] = 'ACTIVE'
        with patch.object(tasks, '_continuation_snapshot_unchanged', side_effect=PermissionError('source_changed')):
            with self.assertRaisesRegex(PermissionError, 'source_changed'): self.renew()
        self.assertIsNone(self.f.ledger.get(tasks._source_renewal_key()))

    def test_person_withdrawal_during_last_uuid_read_is_checked_before_append(self):
        injected = []
        def volume(_):
            if self.f.ledger.db.in_transaction and not injected:
                injected.append(True)
                # Native transcript appends preserve every frozen old byte.
                with self.path.open('a') as out:
                    out.write(json.dumps({'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'new-person'}}) + '\n')
                with ClosureStore(self.f.database) as closure:
                    closure.conn.execute('UPDATE closure_hook_roots SET turn=? WHERE session=?',
                                         ('new-person', self.source['session']))
            return self.volume
        with patch.object(tasks, '_native_volume_uuid', side_effect=volume):
            with self.assertRaisesRegex(PermissionError, 'source_superseded'): self.renew()
        self.assertTrue(injected)
        self.assertIsNone(self.f.ledger.get(tasks._source_renewal_key()))
        self.assert_originals()

    def test_native_use_failure_preserves_single_committed_receipt_and_can_reconcile(self):
        with patch.object(tasks, '_load_task_locked', side_effect=ValueError('synthetic_read_failure')):
            result = tasks.load_task(None, 'agi', source_renewal=self.payload)
        self.assertTrue(result['source_renewal']['recorded']); self.assertFalse(result['native_use_recorded'])
        self.assertEqual(result['read_error'], 'synthetic_read_failure')
        result2 = tasks.load_task(None, 'agi', source_renewal=self.payload)
        self.assertTrue(result2['native_use_recorded']); self.assertTrue(result2['source_renewal']['idempotent'])
        self.assertEqual(result['source_renewal']['receipt'], result2['source_renewal']['receipt'])


if __name__ == '__main__': unittest.main()
