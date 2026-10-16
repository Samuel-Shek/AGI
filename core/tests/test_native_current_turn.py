"""Exercise the desktop stream protocol without any real chats or writes."""
import json
from pathlib import Path
import struct
import unittest
from unittest.mock import Mock

from activity_mvp import attention_native as native

SESSION = '00000000-0000-0000-0000-000000000010'
FIRST = '00000000-0000-0000-0000-000000000001'
SECOND = '00000000-0000-0000-0000-000000000002'


class NativeCurrentTurnTests(unittest.TestCase):
    def state(self):
        # A current desktop schema-11 snapshot has an empty direct turns array;
        # its ordered tail island, not entity insertion order, identifies latest.
        return {'id': SESSION, 'cwd': str(Path.cwd()), 'turns': [],
                'threadRuntimeStatus': {'type': 'active'},
                'turnHistory': {'history': {
                    'entitiesByKey': {'second': {'turnId': SECOND, 'status': 'inProgress'},
                                      'first': {'turnId': FIRST, 'status': 'completed'}},
                    'islands': [{'entries': [{'value': 'first'}, {'value': 'second'}],
                                 'newerBoundary': {'status': 'exhausted'}}]}}}

    def ipc(self, state):
        ipc = native.DesktopIPC(); ipc.client_id = 'fixture-reader'; ipc.socket = Mock()
        ipc.owner = Mock(return_value='verified-desktop-owner')
        response = {'method': 'thread-stream-state-changed', 'sourceClientId': 'verified-desktop-owner',
                    'version': 11, 'params': {'conversationId': SESSION, 'change': {'type': 'snapshot', 'conversationState': state}}}
        data = json.dumps(response).encode()
        ipc.read_exact = Mock(side_effect=[struct.pack('<I', len(data)), data])
        return ipc

    def test_paginated_tail_uses_ordered_authoritative_end_and_discards_text(self):
        state = self.state()
        state['turnHistory']['history']['entitiesByKey']['second']['items'] = [{'text': 'private fixture'}]
        result = self.ipc(state).snapshot(SESSION, expected_cwd=Path.cwd(), current_turn=True)
        self.assertEqual((result['turn_id'], result['turn_status']), (SECOND, 'inProgress'))
        self.assertTrue(result['current_turn_matches'])
        self.assertNotIn('private fixture', json.dumps(result))
        self.assertNotIn('items', result)

    def test_direct_current_turn_supported_and_requested_historical_turn_unchanged(self):
        state = self.state(); state['turns'] = [{'turnId': SECOND, 'status': 'inProgress'}]
        self.assertEqual(self.ipc(state).snapshot(SESSION, current_turn=True)['turn_id'], SECOND)
        result = self.ipc(state).snapshot(SESSION, turn_id=FIRST)
        self.assertEqual((result['turn_id'], result['turn_status']), (FIRST, 'completed'))
        self.assertFalse(result['current_turn_matches'])

    def test_no_authoritative_tail_ambiguous_tail_or_missing_entity_fails_closed(self):
        for mutation in ('unfinished', 'ambiguous', 'missing'):
            with self.subTest(mutation=mutation):
                state = self.state(); history = state['turnHistory']['history']
                if mutation == 'unfinished': history['islands'][0]['newerBoundary']['status'] = 'notLoaded'
                if mutation == 'ambiguous': history['islands'].append(dict(history['islands'][0]))
                if mutation == 'missing': history['entitiesByKey'].pop('second')
                with self.assertRaisesRegex(native.NativeError, 'current_turn_unavailable'):
                    self.ipc(state).snapshot(SESSION, current_turn=True)

    def test_wrong_cwd_target_protocol_or_selector_never_certifies_current_turn(self):
        state = self.state(); state['cwd'] = '/wrong/project'
        with self.assertRaisesRegex(native.NativeError, 'target_mismatch'):
            self.ipc(state).snapshot(SESSION, expected_cwd=Path.cwd(), current_turn=True)
        state = self.state(); state['id'] = FIRST
        with self.assertRaisesRegex(native.NativeError, 'target_mismatch'):
            self.ipc(state).snapshot(SESSION, current_turn=True)
        ipc = self.ipc(self.state())
        with self.assertRaisesRegex(native.NativeError, 'selector_conflict'):
            ipc.snapshot(SESSION, current_turn=True, turn_id=FIRST)
        ipc.owner.assert_not_called()
        ipc = self.ipc(self.state())
        response = {'method': 'thread-stream-state-changed', 'sourceClientId': 'verified-desktop-owner',
                    'version': 10, 'params': {'conversationId': SESSION, 'change': {'type': 'snapshot', 'conversationState': self.state()}}}
        data = json.dumps(response).encode(); ipc.read_exact.side_effect = [struct.pack('<I', len(data)), data]
        with self.assertRaisesRegex(native.NativeError, 'incompatible'):
            ipc.snapshot(SESSION, current_turn=True)
