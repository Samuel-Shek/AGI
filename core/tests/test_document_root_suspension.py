import json
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_document_changes as fixtures
from activity_mvp import document_changes as docs


class RootSuspensionTests(unittest.TestCase):
    setUp = fixtures.DocumentChangeTests.setUp
    tearDown = fixtures.DocumentChangeTests.tearDown
    _event = fixtures.DocumentChangeTests._event
    _all_changes = fixtures.DocumentChangeTests._all_changes

    def prepare_two_roots(self):
        self.watch = self.watch.resolve()
        other = self.root / "personal"
        other.mkdir()
        docs.configure_document_changes(self.conn, add_root=other, runtime_root=self.db.parent)
        registry = json.loads((self.db.parent / docs.RUNTIME_ROOTS_FILENAME).read_text())
        ref = next(x["root_ref"] for x in registry["roots"] if x["path"] == str(self.watch))
        path = self.watch / "note.md"
        path.write_text("Original content\n")
        docs.build_document_baseline(self.conn, self.watch)
        path.write_text("Original content\nA real update\n")
        docs.process_document_event(self.conn, self._event(path, 101), root_path=self.watch)
        moved = self.root / "external-company"
        self.watch.rename(moved)
        self.watch.symlink_to(moved, target_is_directory=True)
        return other, ref

    def test_suspend_symlink_root_preserves_history_and_restores_remaining_roots(self):
        other, ref = self.prepare_two_roots()
        tables = ("file_change_events", "file_change_versions", "file_watch_state", "file_watch_coverage_gaps")
        before = {t: [tuple(r) for r in self.conn.execute("SELECT * FROM " + t)] for t in tables}
        with patch.object(docs, "_resolve_root", wraps=docs._resolve_root) as resolver:
            docs.configure_document_changes(self.conn, suspend_root=ref, runtime_root=self.db.parent)
            docs.ensure_default_document_roots(self.conn, self.db.parent)
            with self.assertRaisesRegex(RuntimeError, 'helper_not_started'):
                docs.start_document_change_worker(self.db, ['synthetic-helper'],
                    popen_factory=lambda *a, **k: (_ for _ in ()).throw(RuntimeError('helper_not_started')))
            self.assertNotIn(str(self.watch), [str(call.args[0]) for call in resolver.call_args_list])
        for table in tables:
            self.assertEqual(before[table], [tuple(r) for r in self.conn.execute("SELECT * FROM " + table)])
        status = docs.document_changes_status(self.conn)
        self.assertEqual(status["coverage"]["root_count"], 1)
        self.assertEqual(len(status["roots"]), 1)
        self.assertNotIn("root_control_refs_incomplete", status["limitations"])
        self.assertEqual(self._all_changes()["totals"]["distinct_files"], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM file_watch_roots").fetchone()[0], 2)
        # A restart/normalizer and idempotent repeat cannot revive the old root.
        docs.configure_document_changes(self.conn, suspend_root=ref, runtime_root=self.db.parent)
        self.assertEqual(self.conn.execute("SELECT sum(enabled) FROM file_watch_roots").fetchone()[0], 1)

    def test_forged_reference_and_swapped_path_do_not_modify_registry_or_data(self):
        _, ref = self.prepare_two_roots()
        path = self.db.parent / docs.RUNTIME_ROOTS_FILENAME
        registry = json.loads(path.read_text())
        registry["roots"][0]["path"] = str(self.root / "unapproved")
        path.write_text(json.dumps(registry));path.chmod(0o600)
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            docs.configure_document_changes(self.conn, suspend_root=ref, runtime_root=self.db.parent)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.conn.execute("SELECT sum(enabled) FROM file_watch_roots").fetchone()[0], 2)
        with self.assertRaises(ValueError):
            docs.configure_document_changes(self.conn, suspend_root="root_internal", runtime_root=self.db.parent)

    def test_excluded_file_in_enabled_root_is_not_deleted_by_rescan_or_deferred_barrier(self):
        from activity_mvp.presentation_scope import POLICY_FILE
        path = self.watch / 'FormerOrg-note.md'
        path.write_text('Preserve the historical comparison state\n')
        docs.build_document_baseline(self.conn, self.watch)
        before = [tuple(r) for r in self.conn.execute('SELECT * FROM file_watch_state')]
        policy = self.db.parent / POLICY_FILE
        policy.write_text(json.dumps({'version':1,'enabled':True,'retired_names':['FormerOrg'],
            'retired_paths':[], 'retired_root_refs':[]})); policy.chmod(0o600)
        for finalize in (True, False):
            deferred = {}
            result = docs.build_document_baseline(self.conn, self.watch,
                finalize_coverage=finalize, deferred_delete_revisions=deferred)
            self.assertEqual(result['files_failed'], 0)
            self.assertEqual(before, [tuple(r) for r in self.conn.execute('SELECT * FROM file_watch_state')])
            self.assertEqual(deferred, {})

    def test_migrated_child_link_with_generic_filename_preserves_ambiguous_old_state(self):
        from activity_mvp.presentation_scope import POLICY_FILE
        self.watch = self.watch.resolve()
        child = self.watch / 'moved-folder'; child.mkdir()
        (child / 'note.md').write_text('Generic historical material\n')
        docs.build_document_baseline(self.conn, self.watch)
        before = [tuple(r) for r in self.conn.execute('SELECT * FROM file_watch_state')]
        archive = self.root / 'offline-archive'; child.rename(archive); child.symlink_to(archive)
        policy = self.db.parent / POLICY_FILE
        policy.write_text(json.dumps({'version':1,'enabled':True,'retired_names':[],
            'retired_paths':[str(child)], 'retired_root_refs':[]})); policy.chmod(0o600)
        for finalize in (True, False):
            deferred = {}
            docs.build_document_baseline(self.conn, self.watch, finalize_coverage=finalize,
                deferred_delete_revisions=deferred)
            self.assertEqual(before, [tuple(r) for r in self.conn.execute('SELECT * FROM file_watch_state')])
            self.assertEqual(deferred, {})

    def test_overlapping_parent_root_cannot_redisplay_generic_retired_document(self):
        from activity_mvp.presentation_scope import POLICY_FILE
        self.watch = self.watch.resolve()
        parent = self.watch.parent
        docs.configure_document_changes(self.conn, add_root=parent, runtime_root=self.db.parent)
        path = self.watch / 'generic-note.md'; path.write_text('Initial\n')
        docs.build_document_baseline(self.conn, self.watch)
        docs.build_document_baseline(self.conn, parent)
        path.write_text('Initial\nChanged\n')
        for root in (self.watch, parent):
            docs.process_document_event(self.conn, self._event(path, 101), root_path=root)
        self.assertGreater(self._all_changes()['totals']['distinct_files'], 0)
        registry = json.loads((self.db.parent/docs.RUNTIME_ROOTS_FILENAME).read_text())
        ref = next(row['root_ref'] for row in registry['roots'] if row['path']==str(self.watch))
        policy = self.db.parent/POLICY_FILE
        policy.write_text(json.dumps({'version':1,'enabled':True,'retired_names':[],
            'retired_paths':[str(self.watch)],'retired_root_refs':[ref]}));policy.chmod(0o600)
        docs.configure_document_changes(self.conn, suspend_root=ref, runtime_root=self.db.parent)
        before = self.conn.execute('SELECT count(*) FROM file_change_events').fetchone()[0]
        # Supported older live databases have lineage on state/versions only.
        # Exercise the actual owner history/list path after retirement.
        self.conn.execute('ALTER TABLE file_change_events DROP COLUMN document_path_hmac')
        self.conn.commit()
        self.assertEqual(docs.document_changes_status(self.conn)['coverage']['root_count'], 1)
        self.assertEqual(self._all_changes()['totals']['distinct_files'], 0)
        self.assertEqual(self._all_changes(for_export=True)['totals']['distinct_files'], 0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM file_change_events').fetchone()[0], before)


if __name__ == "__main__":
    unittest.main()
