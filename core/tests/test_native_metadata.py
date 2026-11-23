"""Synthetic large desktop frames; no real conversation bodies or IPC calls."""
import json
from pathlib import Path
import socket
import struct
import time
import tracemalloc
import unittest
from unittest.mock import Mock, MagicMock, patch

from activity_mvp import attention_native as native
from activity_mvp.native_metadata import MetadataBudget, MetadataError, Projection
from tests.test_native_current_turn import NativeCurrentTurnTests, SESSION, SECOND, FIRST


class BytesSocket:
    def __init__(self, data, fragment=65536):
        self.data, self.pos, self.fragment = data, 0, fragment
        self.closed = False
    def recv(self, count):
        end = min(len(self.data), self.pos + min(count, self.fragment))
        value = self.data[self.pos:end]; self.pos = end
        return value
    def settimeout(self, timeout):
        self.timeout = timeout
    def sendall(self, value):
        pass
    def close(self):
        self.closed = True


def envelope(state):
    return {'method': 'thread-stream-state-changed', 'sourceClientId': 'verified-desktop-owner',
            'version': 11, 'params': {'conversationId': SESSION,
             'change': {'type': 'snapshot', 'conversationState': state}}}


class NativeMetadataTests(unittest.TestCase):
    state = NativeCurrentTurnTests.state

    def test_native_security_dependencies_invalidate_fixed_controller_acceptance(self):
        from activity_mvp.feedback import acceptance
        from activity_mvp import native_metadata, native_worktree
        current = acceptance.guard_hashes()
        state = {'repair_capability': {'enabled': True, 'guards': current},
                 'isolation': {'passed': True}, 'model_capability': {'enabled': True}}
        self.assertTrue(acceptance.release_ready(state))
        read = Path.read_bytes
        for dependency in (native_metadata, native_worktree):
            parser = Path(dependency.__file__).resolve()
            def changed(path):
                value = read(path)
                return value + b'\n# synthetic changed dependency\n' if path.resolve() == parser else value
            with self.subTest(dependency=parser.name), patch.object(Path, 'read_bytes', changed):
                self.assertFalse(acceptance.release_ready(state))
        for name in ('native_metadata.py', 'native_worktree.py'):
            state['repair_capability']['guards'] = {k: v for k, v in current.items() if k != name}
            self.assertFalse(acceptance.release_ready(state))

    def parse(self, raw, fragment=65536, budget=None):
        return Projection(BytesSocket(raw, fragment), len(raw), budget or MetadataBudget(),
                          time.monotonic() + 6).read()

    def ipc(self, state, *, metadata=True, budget=None, owner='verified-desktop-owner'):
        raw = json.dumps(envelope(state), ensure_ascii=False).encode()
        ipc = native.DesktopIPC(metadata_budget=(budget or MetadataBudget()) if metadata else None)
        ipc.client_id = 'fixture-reader'
        ipc.socket = BytesSocket(struct.pack('<I', len(raw)) + raw)
        ipc.owner = Mock(return_value=owner)
        return ipc

    def test_large_history_stream_succeeds_with_bounded_allocation_default_rejects(self):
        state = self.state()
        state['turnHistory']['history']['entitiesByKey']['second']['items'] = [
            {'text': 'private-body-only-' + 'x' * (9 * 1024 * 1024)}]
        old = self.ipc(state, metadata=False)
        with self.assertRaisesRegex(native.NativeError, 'too_large'):
            old.snapshot(SESSION, current_turn=True)
        ipc = self.ipc(state)
        tracemalloc.start()
        try:
            result = ipc.snapshot(SESSION, current_turn=True)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 2 * 1024 * 1024)
        self.assertEqual(result['turn_id'], SECOND)
        self.assertNotIn('private-body-only', json.dumps(result))
        self.assertGreater(ipc.metadata_budget.received, 8 * 1024 * 1024)

    def test_utf8_escapes_field_order_and_body_spoofing_across_single_byte_chunks(self):
        state = self.state()
        state['latestTokenUsageInfo'] = {'body': 'private-body-in-usage'}
        state['ignored'] = {'turnId': FIRST, 'status': 'completed',
                            'text': '中😀\\\"\n' + '{"sourceClientId":"spoof"}'}
        data = envelope(state)
        data = dict(reversed(list(data.items())))
        raw = json.dumps(data, ensure_ascii=False).encode()
        result = self.parse(raw, fragment=1)
        self.assertNotIn('ignored', result['params']['change']['conversationState'])
        self.assertNotIn('private-body-in-usage', json.dumps(result))
        self.assertEqual(result['sourceClientId'], 'verified-desktop-owner')

    def test_duplicate_control_keys_late_overrides_and_invalid_json_rejected(self):
        raw = json.dumps(envelope(self.state())).encode()
        bad = [raw[:-1], raw + b'{}', raw[:-1] + b',"version":12}',
               raw.replace(b'"version": 11', b'"version": 11,"version":11'),
               raw[:-1] + b',"ignored":"\\q"}', raw[:-1] + b',"ignored":"\xff"}',
               raw[:-1] + b',"ignored":"\x00"}', raw[:-1] + b',"ignored":01}',
               raw[:-1] + b',"ignored":[1,]}']
        for value in bad:
            with self.subTest(suffix=value[-25:]):
                with self.assertRaises(MetadataError):
                    self.parse(value, fragment=7)

    def test_unrelated_valid_frames_with_other_shapes_do_not_hide_current_turn(self):
        proper = json.dumps(envelope(self.state())).encode()
        prefixes = [
            {'type': 'response', 'requestId': 'unrelated-request', 'resultType': 'success',
             'result': {'body': 'private-body' * 100000}},
            {},
            {'params': None, 'method': 'unrelated-event', 'sourceClientId': 'verified-desktop-owner'},
            {'params': ['private-body'], 'sourceClientId': 'different-owner',
             'method': 'thread-stream-state-changed'},
            {'params': {'change': {'conversationState': ['private-body']}, 'conversationId': FIRST},
             'sourceClientId': 'verified-desktop-owner', 'method': 'thread-stream-state-changed'},
        ]
        for prefix in prefixes:
            for reverse in (False, True):
                raw = json.dumps(dict(reversed(list(prefix.items()))) if reverse else prefix).encode()
                ipc = self.ipc(self.state())
                ipc.socket = BytesSocket(struct.pack('<I', len(raw)) + raw + struct.pack('<I', len(proper)) + proper, 7)
                with self.subTest(prefix=list(prefix), reverse=reverse):
                    result = ipc.snapshot(SESSION, current_turn=True)
                    self.assertEqual(result['turn_id'], SECOND)
                    self.assertNotIn('private-body', json.dumps(result))

    def test_target_shape_errors_and_unreadable_routing_still_reject(self):
        cases = []
        message = envelope(self.state()); message['method'] = None; cases.append(message)
        for field in ('method', 'sourceClientId', 'params'):
            message = envelope(self.state()); message[field] = ['private-body']; cases.append(message)
        for field in ('conversationId', 'change'):
            message = envelope(self.state()); message['params'][field] = ['private-body']; cases.append(message)
        message = envelope(self.state()); message['version'] = {'body': 'private-body'}; cases.append(message)
        for message in cases:
            raw = json.dumps(message).encode(); ipc = self.ipc(self.state())
            ipc.socket = BytesSocket(struct.pack('<I', len(raw)) + raw)
            with self.assertRaisesRegex(native.NativeError, 'routing_invalid|metadata_shape'):
                ipc.snapshot(SESSION, current_turn=True)

    def test_invalid_shape_does_not_bypass_duplicate_or_unrelated_frame_budget(self):
        raw = b'{"method":"other","params":null,"params":[]}'
        ipc = self.ipc(self.state()); ipc.socket = BytesSocket(struct.pack('<I', len(raw)) + raw)
        with self.assertRaisesRegex(native.NativeError, 'metadata_rejected'):
            ipc.snapshot(SESSION, current_turn=True)
        raw = json.dumps({'method': 'other', 'params': ['private-body'] * 100}).encode()
        ipc = self.ipc(self.state()); ipc.socket = BytesSocket(struct.pack('<I', len(raw)) + raw)
        ipc.metadata_budget.TOTAL = len(raw) // 2
        with self.assertRaises(native.NativeError):
            ipc.snapshot(SESSION, current_turn=True)

    def test_structure_retention_and_wire_budgets_fail_closed(self):
        raw = json.dumps(envelope(self.state())).encode()
        with self.assertRaisesRegex(MetadataError, 'frame_limit'):
            Projection(BytesSocket(b''), MetadataBudget.FRAME + 1, MetadataBudget(), time.monotonic()+6)
        with self.assertRaisesRegex(MetadataError, 'structure_limit'):
            self.parse(raw[:-1] + b',"ignored":' + b'[' * 66 + b'0' + b']' * 66 + b'}')
        state = self.state()
        state['turns'] = [{'id': str(i), 'status': 'x' * 1024} for i in range(1100)]
        with self.assertRaisesRegex(MetadataError, 'retained_limit'):
            self.parse(json.dumps(envelope(state)).encode())
        budget = MetadataBudget(); budget.received = budget.TOTAL - 5
        with self.assertRaisesRegex(MetadataError, 'wire_limit'):
            self.parse(raw, budget=budget)

    def test_absolute_deadline_includes_read_wait_and_unrelated_frames(self):
        read, write = socket.socketpair()
        try:
            write.sendall(b'{')
            start = time.monotonic()
            with self.assertRaises((MetadataError, TimeoutError)):
                Projection(read, 10, MetadataBudget(), start + .03).read()
            self.assertLess(time.monotonic() - start, .3)
        finally:
            read.close(); write.close()
        ipc = self.ipc(self.state())
        ipc.metadata_budget.deadline = time.monotonic() - 1
        with self.assertRaisesRegex(native.NativeError, 'timeout'):
            ipc.snapshot(SESSION, current_turn=True)

    def test_unrelated_frames_share_wire_and_elapsed_budgets(self):
        message = envelope(self.state()); message['sourceClientId'] = 'unrelated-owner'
        data = json.dumps(message).encode()
        frame = struct.pack('<I', len(data)) + data
        ipc = self.ipc(self.state()); ipc.socket = BytesSocket(frame * 8)
        ipc.metadata_budget.TOTAL = len(frame) * 2
        with self.assertRaises(native.NativeError):
            ipc.snapshot(SESSION, current_turn=True)
        self.assertLessEqual(ipc.metadata_budget.received, len(frame) * 2)
        ipc = self.ipc(self.state()); ipc.socket = BytesSocket(frame * 8, fragment=16)
        receive = ipc.socket.recv
        def slow(count):
            time.sleep(.002)
            return receive(count)
        ipc.socket.recv = slow
        start = time.monotonic(); ipc.metadata_budget.deadline = start + .03
        with self.assertRaises(native.NativeError):
            ipc.snapshot(SESSION, current_turn=True)
        self.assertLess(time.monotonic() - start, .3)

    def test_wrong_identity_protocol_and_selector_are_not_certified(self):
        for field, value in [('id', FIRST), ('cwd', '/wrong')]:
            state = self.state(); state[field] = value
            with self.assertRaisesRegex(native.NativeError, 'target_mismatch'):
                self.ipc(state).snapshot(SESSION, expected_cwd=self.state()['cwd'], current_turn=True)
        ipc = self.ipc(self.state())
        with self.assertRaisesRegex(native.NativeError, 'current_turn_only'):
            ipc.snapshot(SESSION, turn_id=SECOND)
        ipc.owner.assert_not_called()
        with self.assertRaisesRegex(native.NativeError, 'read_only'):
            ipc.call('thread-follower-start-turn', {}, version=2)
        message = envelope(self.state()); message['version'] = 10
        data = json.dumps(message).encode(); ipc = self.ipc(self.state())
        ipc.socket = BytesSocket(struct.pack('<I', len(data)) + data)
        with self.assertRaisesRegex(native.NativeError, 'incompatible'):
            ipc.snapshot(SESSION, current_turn=True)

    def test_fresh_worktree_confirmation_shares_budget_and_rejects_changed_turn(self):
        from activity_mvp import native_worktree
        for changed in (False, True):
            state = self.state(); state['cwd'] = '/registered/worktree'
            first = self.ipc(state)
            next_state = self.state(); next_state['cwd'] = state['cwd']
            if changed:
                next_state['turnHistory']['history']['islands'][0]['entries'].reverse()
            second = self.ipc(next_state, budget=first.metadata_budget)
            context = MagicMock(); context.__enter__.return_value = second
            with patch.object(native_worktree, 'verify_registered_worktree') as verify, \
                 patch.object(native, 'DesktopIPC', return_value=context) as construct:
                if changed:
                    with self.assertRaisesRegex(native.NativeError, 'current_turn_changed'):
                        first.snapshot(SESSION, expected_cwd='/canonical', current_turn=True, allow_registered_worktree=True)
                else:
                    self.assertEqual(first.snapshot(SESSION, expected_cwd='/canonical', current_turn=True,
                        allow_registered_worktree=True)['turn_id'], SECOND)
                construct.assert_called_once_with(metadata_budget=first.metadata_budget)
                verify.assert_called_once_with('/canonical', '/registered/worktree')

    def test_worktree_reader_is_not_started_without_work_and_cleanup_time(self):
        from activity_mvp import native_worktree
        state = self.state(); state['cwd'] = '/registered/worktree'
        ipc = self.ipc(state); ipc.metadata_budget.deadline = time.monotonic() + 3.5
        with patch.object(native_worktree, 'verify_registered_worktree') as verify:
            with self.assertRaisesRegex(native.NativeError, 'metadata_timeout'):
                ipc.snapshot(SESSION, expected_cwd='/canonical', current_turn=True, allow_registered_worktree=True)
            verify.assert_not_called()


if __name__ == '__main__':
    unittest.main()
