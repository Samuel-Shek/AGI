import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from activity_mvp import document_changes as d
from activity_mvp import document_root_migration as m
from activity_mvp import storage
from activity_mvp.presentation_scope import DisplayScope
from tests import test_document_changes as fixtures


class CommitFault:
    def __init__(self, conn, after):
        self.conn, self.after = conn, after

    def __getattr__(self, key):
        return getattr(self.conn, key)

    def commit(self):
        if self.after:
            self.conn.commit()
        raise sqlite3.OperationalError('unknown_commit_outcome')


class RootMigrationTests(unittest.TestCase):
    setUp = fixtures.DocumentChangeTests.setUp
    tearDown = fixtures.DocumentChangeTests.tearDown
    _event = fixtures.DocumentChangeTests._event

    def prepare(self):
        self.watch = self.watch.resolve()
        self.runtime = self.db.parent.resolve()
        policy = self.runtime / 'agi-display-scope.json'
        policy.write_text(json.dumps(dict(version=1, enabled=True,
            retired_names=['retired-company'], retired_paths=[], retired_root_refs=[])))
        policy.chmod(0o600)
        self.policy_raw = policy.read_bytes()
        old2 = self.root / 'old2'; old2.mkdir()
        self.keep = self.root / 'keep'; self.keep.mkdir()
        company = self.root / 'retired-company'; company.mkdir()
        for root in (old2, self.keep, company):
            d.configure_document_changes(self.conn, add_root=root, runtime_root=self.runtime)
        registry = d.runtime_document_roots(self.runtime)
        company_ref = next(r['root_ref'] for r in registry if r['path'] == str(company.resolve()))
        d.suspend_document_root(self.conn, company_ref, runtime_root=self.runtime)
        note = self.watch / 'note.md'; note.write_text('old data\n')
        d.build_document_baseline(self.conn, self.watch)
        note.write_text('old data\nwith real change\n')
        d.process_document_event(self.conn, self._event(note, 101), root_path=self.watch)
        self.history_tables = ('file_change_events', 'file_change_versions', 'file_watch_state', 'file_watch_coverage_gaps')
        self.history = {t: [tuple(r) for r in self.conn.execute('SELECT * FROM ' + t)] for t in self.history_tables}
        old_paths = {str(self.watch), str(old2.resolve())}
        self.old_ids = [r['root_id'] for r in registry if r['path'] in old_paths]
        self.old_checkpoints = [tuple(r) for r in self.conn.execute(
            'SELECT * FROM file_watch_checkpoints WHERE root_id IN (?,?) ORDER BY root_id', self.old_ids)]
        self.watch.rename(self.root / 'moved1'); old2.rmdir()
        new1 = self.root / 'new1'; new1.mkdir()
        new2 = self.root / 'new2'; new2.mkdir()
        self.new1 = new1.resolve()
        self.alias = self.new1 / 'excluded'
        replacements = [dict(old_ref=r['root_ref'], new_path=str(n.resolve()), new_label=n.name)
            for r, n in zip([r for r in registry if r['path'] in old_paths], (new1, new2))]
        self.plan = m.prepare_migration(self.conn, self.runtime, replacements, [str(self.alias)], 'a' * 64)
        self.digest = m.plan_sha256(self.plan)
        return self.plan

    def apply(self):
        return m.apply_migration(self.conn, self.plan, approved_sha256=self.digest)

    def test_both_missing_history_preserved_and_repeat_has_no_new_epoch(self):
        self.prepare(); controls = m._controls(self.conn)
        self.assertEqual(self.apply()['status'], 'configured')
        epoch = d._document_change_epoch(self.conn)
        self.assertEqual(self.apply()['status'], 'configured')
        self.assertEqual(d._document_change_epoch(self.conn), epoch)
        self.assertEqual(m._controls(self.conn), controls)
        self.assertEqual(self.conn.execute('SELECT count(*),sum(enabled) FROM file_watch_roots').fetchone()[:], (6, 3))
        self.assertEqual(self.conn.execute('SELECT count(*) FROM file_watch_root_migrations').fetchone()[0], 1)
        for t in self.history_tables:
            self.assertEqual(self.history[t], [tuple(r) for r in self.conn.execute('SELECT * FROM ' + t)])
        self.assertEqual(self.old_checkpoints, [tuple(r) for r in self.conn.execute(
            'SELECT * FROM file_watch_checkpoints WHERE root_id IN (?,?) ORDER BY root_id', self.old_ids)])
        self.assertEqual((self.runtime / 'agi-display-scope.json').read_bytes(), self.policy_raw)
        self.assertTrue(d._registry_root_bindings_valid(self.conn, d.runtime_document_roots(self.runtime)))

    def test_excluded_alias_becomes_directory_blocks_before_open_but_not_sibling_or_feedback(self):
        self.prepare(); self.apply()
        self.alias.mkdir(); secret = self.alias / 'note.md'; secret.write_text('must not read')
        with patch.object(d, '_open_file_beneath', side_effect=AssertionError('opened source')):
            with self.assertRaises(d.DisplaySourceRetired):
                d._snapshot(self.conn, self.new1, secret)
        sibling = self.new1 / 'excluded-allowed'; sibling.mkdir()
        note = sibling / 'note.md'; note.write_text('permitted')
        self.assertIsInstance(d._snapshot(self.conn, self.new1, note), dict)
        self.assertFalse(DisplayScope(self.runtime).hidden('Pending decision about ' + str(self.alias)))

    def test_path_spellings_cannot_bypass_exclusion(self):
        self.prepare(); self.apply()
        variants = (self.new1 / 'allowed/../excluded/note.md',
                    self.new1 / 'EXCLUDED/note.md')
        for candidate in variants:
            with patch.object(d, '_open_file_beneath', side_effect=AssertionError('source opened')):
                with self.assertRaises(d.DisplaySourceRetired):
                    d._snapshot(self.conn, self.new1, candidate)
        secret = d._existing_hmac_key_readonly(self.conn)
        self.assertEqual(d._document_subtree_hmac(secret, self.new1 / 'caf\u00e9'),
                         d._document_subtree_hmac(secret, self.new1 / 'cafe\u0301'))

    def test_installation_before_activation_keeps_old_reader_compatible(self):
        self.prepare()
        self.assertEqual(self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], '32')
        with patch.object(storage, 'SCHEMA_VERSION', 32):
            storage.init_schema(self.conn)
        self.assertIsNone(m._receipt(self.conn, self.digest))
        self.apply()
        storage.init_schema(self.conn)
        self.assertEqual(self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], '33')

    def test_activation_is_never_silently_downgraded_or_repaired(self):
        self.prepare(); self.apply()
        self.conn.execute('DELETE FROM file_watch_root_exclusions')
        self.conn.commit()
        storage.init_schema(self.conn)
        self.assertEqual(self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], '33')
        # Corrupt only this isolated fixture to model interrupted legacy tooling.
        self.conn.execute('DROP TRIGGER pam_schema33_guard_update')
        self.conn.execute("UPDATE meta SET value='32' WHERE key='schema_version'")
        self.conn.commit()
        with self.assertRaisesRegex(sqlite3.DatabaseError, 'inconsistent schema activation'):
            storage.init_schema(self.conn)
        self.assertEqual(self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], '32')

    def assert_prepare_and_apply_refuse_schema(self, reason):
        replacements = [dict(old_ref=r['old_ref'], new_path=a['path'], new_label=a['root_label'])
                        for r,a in zip(self.plan['replacements'], self.plan['additions'])]
        with self.assertRaisesRegex(ValueError, reason):
            m.prepare_migration(self.conn, self.runtime, replacements, [str(self.alias)], 'a'*64)
        with self.assertRaisesRegex(ValueError, reason):
            self.apply()
        self.assertIsNone(m._receipt(self.conn, self.digest))

    def test_future_schema_cannot_be_bound_then_downgraded_by_migration(self):
        self.prepare()
        self.conn.execute("UPDATE meta SET value='34' WHERE key='schema_version'"); self.conn.commit()
        self.assert_prepare_and_apply_refuse_schema('schema_unsupported')
        self.assertEqual(self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], '34')

    def test_inconsistent_activation_cannot_be_bound_then_silently_repaired(self):
        self.prepare()
        self.conn.execute('INSERT INTO file_watch_root_exclusions VALUES(?,?)', (self.old_ids[0], 'f'*64)); self.conn.commit()
        self.assert_prepare_and_apply_refuse_schema('schema_activation_inconsistent')
        self.assertEqual(self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], '32')

    def test_existing_readers_report_actual_supported_version(self):
        from activity_mvp.identity_collision import preview_document_signal_identity_collisions
        from activity_mvp.feedback.release import database_structure_policy
        from activity_mvp.feedback.store import Store
        self.prepare()
        with Store(self.runtime / 'feedback') as store:
            for version in (32, 33):
                if version == 33:
                    self.apply()
                preview = preview_document_signal_identity_collisions(self.conn,
                    historical_cutoff='2026-01-01T00:00:00+00:00')
                self.assertEqual(preview['schema_version'], version)
                policy = database_structure_policy(self.runtime.parent, store.path)
                self.assertFalse(policy['databases_required'], policy)
                self.assertEqual(policy['schema'], version)

    def test_older_schema_reader_and_relabel_are_rejected_without_data_changes(self):
        self.prepare(); self.apply()
        before = m._roots(self.conn)
        with patch.object(storage, 'SCHEMA_VERSION', 32):
            with self.assertRaisesRegex(sqlite3.DatabaseError, 'refusing downgrade'):
                storage.init_schema(self.conn)
        for sql in ("UPDATE meta SET value='32' WHERE key='schema_version'",
                    "INSERT OR REPLACE INTO meta VALUES('schema_version','32')"):
            with self.assertRaisesRegex(sqlite3.DatabaseError, 'schema 33'):
                self.conn.execute(sql)
            self.conn.rollback()
        self.assertEqual(m._roots(self.conn), before)
        self.assertEqual(self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], '33')

    def test_bad_approval_revision_company_reenable_and_pause_all_refuse(self):
        self.prepare()
        with self.assertRaises(ValueError):
            m.apply_migration(self.conn, self.plan, approved_sha256='0' * 64)
        original_roots = m._roots(self.conn)
        state = [tuple(r) for r in self.conn.execute('SELECT * FROM daemon_state')]
        for sql, args in (
            ("UPDATE file_watch_roots SET enabled=1 WHERE enabled=0", ()),
            ("INSERT OR REPLACE INTO daemon_state VALUES('pause_mode','manual','now')", ()),
            ("UPDATE daemon_state SET value='999' WHERE key=?", (d.ROOT_AUTHORIZATION_REVISION_KEY,)),
        ):
            self.conn.execute(sql, args); self.conn.commit()
            with self.assertRaisesRegex(ValueError, 'precondition_changed'):
                self.apply()
            # Restore only this isolated fixture's authorization for next fault.
            self.conn.executemany('UPDATE file_watch_roots SET enabled=? WHERE root_id=?',
                                  [(r[2], r[0]) for r in original_roots])
            self.conn.execute('DELETE FROM daemon_state')
            self.conn.executemany('INSERT INTO daemon_state VALUES(?,?,?)', state)
            self.conn.commit()
        self.assertIsNone(m._receipt(self.conn, self.digest))

    def test_committed_pause_cannot_be_undone_by_reconciliation(self):
        self.prepare(); self.apply()
        self.conn.execute("INSERT OR REPLACE INTO daemon_state VALUES('pause_mode','manual','now')"); self.conn.commit()
        with self.assertRaisesRegex(ValueError, 'committed_state_changed'):
            m.reconcile_migration(self.conn, self.plan, approved_sha256=self.digest)
        self.assertEqual(self.conn.execute("SELECT value FROM daemon_state WHERE key='pause_mode'").fetchone()[0], 'manual')

    def test_target_symlink_or_replaced_directory_refuses(self):
        self.prepare()
        target = self.new1.parent / 'elsewhere'; self.new1.rename(target)
        self.new1.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError): self.apply()
        self.new1.unlink(); self.new1.mkdir()
        with self.assertRaisesRegex(ValueError, 'identity_changed'): self.apply()
        self.assertIsNone(m._receipt(self.conn, self.digest))

    def test_sidecar_failure_commits_once_then_reconciles_exact_receipt(self):
        self.prepare()
        with patch.object(d, '_write_runtime_registry', side_effect=OSError('disk fault')):
            with self.assertRaises(OSError): self.apply()
        receipt = m._receipt(self.conn, self.digest)
        self.assertIsNotNone(receipt)
        self.assertNotEqual(d._load_runtime_registry(self.runtime)[2], d._root_authorization_revision(self.conn))
        self.assertEqual(d.ensure_default_document_roots(self.conn, self.runtime), [])
        self.assertEqual(self.old_checkpoints, [tuple(r) for r in self.conn.execute(
            'SELECT * FROM file_watch_checkpoints WHERE root_id IN (?,?) ORDER BY root_id', self.old_ids)])
        m.reconcile_migration(self.conn, self.plan, approved_sha256=self.digest)
        self.assertEqual(d._document_change_epoch(self.conn), receipt['epoch_after'])
        self.assertEqual(d._load_runtime_registry(self.runtime)[2], receipt['revision_after'])

    def test_unknown_commit_outcome_requires_receipt_and_never_writes_sidecar(self):
        self.prepare()
        raw = (self.runtime / d.RUNTIME_ROOTS_FILENAME).read_bytes()
        for after in (False, True):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'unknown_commit'):
                m.apply_migration(CommitFault(self.conn, after), self.plan, approved_sha256=self.digest)
            self.assertEqual((self.runtime / d.RUNTIME_ROOTS_FILENAME).read_bytes(), raw)
            with sqlite3.connect('file:' + str(self.db) + '?mode=ro', uri=True) as fresh:
                self.assertEqual(m._receipt(fresh, self.digest) is not None, after)
            if not after:
                with self.assertRaisesRegex(ValueError, 'commit_not_proven'):
                    m.reconcile_migration(self.conn, self.plan, approved_sha256=self.digest)
        m.reconcile_migration(self.conn, self.plan, approved_sha256=self.digest)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM file_watch_root_migrations').fetchone()[0], 1)

    def test_unknown_sidecar_write_is_reconciled_without_rewriting(self):
        self.prepare()
        original = d._write_runtime_registry
        def unknown(*args):
            original(*args)
            raise OSError('sidecar_result_unknown')
        with patch.object(d, '_write_runtime_registry', side_effect=unknown):
            with self.assertRaises(OSError): self.apply()
        with patch.object(d, '_write_runtime_registry', side_effect=AssertionError('rewritten')):
            m.reconcile_migration(self.conn, self.plan, approved_sha256=self.digest)

    def test_old_worker_cannot_write_after_migration(self):
        self.prepare(); epoch = self.plan['epoch']; self.apply()
        note = self.keep.resolve() / 'note.md'; note.write_text('new event')
        result = d.process_document_event(self.conn, self._event(note, 202),
            root_path=self.keep.resolve(), expected_epoch=epoch)
        self.assertFalse(result['recorded'])
        self.assertEqual(result['reason'], 'authorization_epoch_changed')

    def test_forged_registry_binding_refuses_preparation(self):
        self.prepare()
        path = self.runtime / d.RUNTIME_ROOTS_FILENAME
        registry = json.loads(path.read_text())
        registry['roots'][0]['path'] = str(self.root.resolve() / 'forged')
        path.write_text(json.dumps(registry))
        with self.assertRaises(ValueError): self.apply()
        self.assertIsNone(m._receipt(self.conn, self.digest))

    def test_policy_withdrawal_or_registry_edit_prevents_projection(self):
        self.prepare()
        with patch.object(d, '_write_runtime_registry', side_effect=OSError()):
            with self.assertRaises(OSError): self.apply()
        path = self.runtime / 'agi-display-scope.json'
        path.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'policy_changed'):
            m.reconcile_migration(self.conn, self.plan, approved_sha256=self.digest)
        self.assertEqual(path.read_text(), '{}')


if __name__ == '__main__':
    unittest.main()
