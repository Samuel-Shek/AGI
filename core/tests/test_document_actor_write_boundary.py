"""Real temporary event/SQLite boundaries; never uses owner attribution logs."""
import json
from pathlib import Path
import sqlite3
import threading
import unittest
from unittest.mock import patch

from activity_mvp import document_changes as d
from activity_mvp.storage import connect, connect_readonly
from activity_mvp.worker_connections import WorkerConnections
from tests import test_document_changes as fixtures


class EventActorWriteBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.DocumentChangeTests(); self.f.setUp(); self.addCleanup(self.f.tearDown)
        self.log = self.f.root / 'synthetic-attribution.jsonl'; self.log.write_text('')
        self.rotated = self.f.root / 'synthetic-rotated.jsonl'
        for name, value in [('AI_EDIT_LOG_PATH', self.log), ('AI_EDIT_ROTATED_LOG_PATH', self.rotated)]:
            mock = patch.object(d, name, value); mock.start(); self.addCleanup(mock.stop)
        self.target = self.f.watch / 'synthetic.md'; self.target.write_text('initial\n')
        d.build_document_baseline(self.f.conn, self.f.watch, batch_size=0)
        self.root_id = self.f.conn.execute('SELECT root_id FROM file_watch_roots WHERE enabled=1').fetchone()[0]
        self.file_id = d._file_id(self.f.conn, self.f.watch, self.target)
        self.epoch = d._document_change_epoch(self.f.conn)
        self.target.write_text('changed\n')
        self.event = self.f._event(self.target, 2)
        self.snapshot = d._snapshot(self.f.conn, self.f.watch, self.target, allow_content=True, content_allowed_check=lambda: True)

    def receipt(self, operation='write'):
        self.log.write_text(json.dumps({'at': self.event['observed_at'], 'tool': 'synthetic',
            'root_id': self.root_id, 'file_id': self.file_id, 'operation': operation}) + '\n')

    def process(self, **extra):
        return d.process_document_event(self.f.conn, self.event, root_path=self.f.watch,
            snapshot_override=self.snapshot, content_allowed_check=lambda: True, expected_epoch=self.epoch, **extra)

    def test_slow_attribution_read_does_not_hold_writer_or_block_real_pending(self):
        reached = threading.Event(); release = threading.Event(); result = {}
        original = Path.read_text
        def slow(path, *a, **kw):
            if path == self.log:
                result['transaction_at_read'] = result['conn'].in_transaction
                reached.set()
                if not release.wait(3): raise AssertionError('synthetic file-read wait')
            return original(path, *a, **kw)
        def event_worker():
            conn = result['conn'] = connect(self.f.db)
            try:
                result['event'] = d.process_document_event(conn, self.event, root_path=self.f.watch,
                    snapshot_override=self.snapshot, content_allowed_check=lambda: True, expected_epoch=self.epoch)
            except BaseException as error: result['error'] = type(error).__name__
            finally: conn.close()
        pool = WorkerConnections(self.f.db, connect, connect_readonly); pool.wait_seconds = .2
        thread = threading.Thread(target=event_worker)
        try:
            with patch.object(Path, 'read_text', slow):
                thread.start(); self.assertTrue(reached.wait(3))
                self.assertFalse(result['transaction_at_read'])
                pending = pool.connect('synthetic-consumer')
                self.assertEqual(d._adjust_document_change_pending(pending, 1, expected_epoch=self.epoch), 1)
                pending.commit()
                self.assertTrue(thread.is_alive())
                release.set(); thread.join(3)
            self.assertFalse(thread.is_alive()); self.assertNotIn('error', result)
            self.assertTrue(result['event']['recorded'])
            self.assertEqual(WorkerConnections(None, None, None).wait_seconds, 30.)
        finally:
            release.set(); thread.join(3); pool.close()

    def test_normal_ai_attribution_same_input_preserved_and_no_log_parse_under_writer(self):
        self.receipt(); read = Path.read_text; states = []
        def observe(path, *a, **kw):
            if path == self.log: states.append(self.f.conn.in_transaction)
            return read(path, *a, **kw)
        with patch.object(Path, 'read_text', observe): result = self.process()
        self.assertTrue(result['recorded']); self.assertEqual(states, [False])
        row = self.f.conn.execute('SELECT actor_source,actor_tool FROM file_change_versions WHERE helper_event_id=?', ('2',)).fetchone()
        self.assertEqual(tuple(row), ('ai_tool', 'synthetic'))
        self.assertIsNone(d._DOCUMENT_EVENT_ACTOR_EVIDENCE.get())

    def test_deleted_and_unresolved_paths_do_not_parse_attribution_inside_writer(self):
        self.receipt('delete'); self.target.unlink(); read = Path.read_text; states = []
        def observe(path, *a, **kw):
            if path == self.log: states.append(self.f.conn.in_transaction)
            return read(path, *a, **kw)
        with patch.object(Path, 'read_text', observe):
            deleted = d.process_document_event(self.f.conn, self.event, root_path=self.f.watch,
                content_allowed_check=lambda: True, expected_epoch=self.epoch)
            self.assertTrue(deleted['recorded'])
            self.target.write_text('still changing\n'); self.event = self.f._event(self.target, 3)
            self.receipt()
            unresolved = d._record_unresolved_document_event(self.f.conn, self.event,
                root_path=self.f.watch, root_label='Synthetic', expected_epoch=self.epoch)
        self.assertTrue(unresolved['recorded']); self.assertTrue(states); self.assertFalse(any(states))
        row = self.f.conn.execute('SELECT coverage_state FROM file_change_versions WHERE helper_event_id=?', ('3',)).fetchone()
        self.assertEqual(row[0], 'file_unresolved'); self.assertIsNone(d._DOCUMENT_EVENT_ACTOR_EVIDENCE.get())

    def test_changed_log_or_credentials_or_registry_or_event_never_reuses_old_actor(self):
        self.receipt()
        original_log = self.log.read_bytes()
        registry = self.f.db.parent / d.RUNTIME_ROOTS_FILENAME
        original_registry = registry.read_bytes(); secret = self.f.db.parent / d.HMAC_SECRET_FILENAME
        original_secret = secret.read_bytes()
        item = {}
        cases = [lambda: self.log.write_text(self.log.read_text() + '{}\n'),
                 lambda: registry.write_bytes(original_registry + b' '),
                 lambda: secret.write_bytes(b'x' * 32),
                 lambda: item.update(file_id='different'),
                 lambda: item.update(changed_at='2000-01-01T00:00:00+00:00')]
        for change in cases:
            with self.subTest(change=change):
                self.log.write_bytes(original_log); registry.write_bytes(original_registry); secret.write_bytes(original_secret)
                item.clear(); item.update(file_id=self.file_id, root_id=self.root_id,
                    changed_at=self.event['observed_at'], change_kind='modified')
                evidence = d._prepare_event_actor_evidence(self.f.conn, self.f.watch, self.root_id, self.file_id, self.event['observed_at'])
                self.assertIsNotNone(evidence)
                token = d._DOCUMENT_EVENT_ACTOR_EVIDENCE.set({'snapshot': evidence})
                try:
                    change(); self.f.conn.execute('BEGIN IMMEDIATE')
                    with patch.object(d, '_load_ai_edit_attribution', side_effect=AssertionError('writer log fallback')):
                        self.assertEqual(d._actor_for_file_version(self.f.conn, item)['source'], 'unattributed')
                finally:
                    self.f.conn.rollback(); d._DOCUMENT_EVENT_ACTOR_EVIDENCE.reset(token)

    def test_root_revocation_at_write_gate_keeps_checkpoint_and_version_uncommitted(self):
        self.receipt(); original = d._begin_document_change_write
        def revoke(conn, root_id, epoch):
            conn.execute('UPDATE file_watch_roots SET enabled=0 WHERE root_id=?', (root_id,)); conn.commit()
            return original(conn, root_id, epoch)
        with patch.object(d, '_begin_document_change_write', side_effect=revoke): result = self.process()
        self.assertFalse(result['recorded']); self.assertEqual(result['reason'], 'root_not_configured')
        self.assertEqual(self.f.conn.execute('SELECT count(*) FROM file_change_versions WHERE helper_event_id=?', ('2',)).fetchone()[0], 0)
        self.assertFalse(self.f.conn.in_transaction); self.assertIsNone(d._DOCUMENT_EVENT_ACTOR_EVIDENCE.get())

    def test_independent_strong_human_evidence_survives_invalid_ai_log_key_or_registry_binding(self):
        item = {'file_id': self.file_id, 'root_id': self.root_id, 'changed_at': self.event['observed_at'],
                'file_name_redacted': self.snapshot['file_name_redacted'],
                'document_path_hmac': d._document_signal_path_hmac(self.f.conn, self.target),
                'content_modified_at': self.snapshot['modified_at'], 'detail_level': 'detailed', 'change_kind': 'modified'}
        self.f.conn.execute('INSERT INTO document_signals(id,session_id,captured_at,app_name,file_path_hmac,file_name_redacted,file_modified_at,subsequent_mtime_event,source,source_confidence,deletion_group_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ('synthetic-human', 'synthetic-session', item['changed_at'], 'Obsidian', item['document_path_hmac'],
             item['file_name_redacted'], item['content_modified_at'], 'modified_near_capture', 'foreground_app_or_file_url', .86, 'synthetic-group'))
        self.f.conn.commit()
        registry = self.f.db.parent / d.RUNTIME_ROOTS_FILENAME; reg_bytes = registry.read_bytes()
        secret = self.f.db.parent / d.HMAC_SECRET_FILENAME; secret_bytes = secret.read_bytes()
        changes = [lambda: self.log.write_text('{}\n'), lambda: secret.write_bytes(b'x' * 32),
                   lambda: registry.write_bytes(reg_bytes + b' ')]
        for change in changes:
            with self.subTest(change=change):
                self.log.write_text(''); secret.write_bytes(secret_bytes); registry.write_bytes(reg_bytes)
                evidence = d._prepare_event_actor_evidence(self.f.conn, self.f.watch, self.root_id, self.file_id, item['changed_at'])
                self.assertIsNotNone(evidence); change(); self.f.conn.execute('BEGIN IMMEDIATE')
                token = d._DOCUMENT_EVENT_ACTOR_EVIDENCE.set({'snapshot': evidence})
                try:
                    with patch.object(d, '_load_ai_edit_attribution', side_effect=AssertionError('writer log fallback')):
                        self.assertEqual(d._actor_for_file_version(self.f.conn, item)['source'], 'user_evidence')
                finally:
                    self.f.conn.rollback(); d._DOCUMENT_EVENT_ACTOR_EVIDENCE.reset(token)


if __name__ == '__main__': unittest.main()
