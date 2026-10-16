"""Status request reuse must never skip migration progress or fresh guard checks."""
import sqlite3
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from activity_mvp import storage


class StatusSchemaReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'activity.sqlite'
        self.conn = storage.ensure_ready(self.path); self.addCleanup(self.conn.close)

    def count_migrations(self):
        return patch.object(storage, '_init_schema_locked', wraps=storage._init_schema_locked)

    def test_one_completed_noop_is_reused_only_inside_status_request(self):
        with self.count_migrations() as calls:
            with storage.full_status_schema_read(self.conn):
                storage.init_schema(self.conn); storage.init_schema(self.conn)
                self.assertEqual(calls.call_count, 1)
            storage.init_schema(self.conn)
            self.assertEqual(calls.call_count, 2)
            with storage.full_status_schema_read(self.conn): storage.init_schema(self.conn)
            self.assertEqual(calls.call_count, 3)

    def test_existing_safe_flags_keep_their_original_change_times(self):
        keys=('context_memory_writer_enabled','context_source_revocation_cascade_ready')
        before=list(self.conn.execute('SELECT key,value,updated_at FROM settings WHERE key IN (?,?) ORDER BY key',keys))
        storage.init_schema(self.conn)
        after=list(self.conn.execute('SELECT key,value,updated_at FROM settings WHERE key IN (?,?) ORDER BY key',keys))
        self.assertEqual([tuple(x) for x in before], [tuple(x) for x in after])
        self.assertTrue(all(x['value']=='false' for x in after))

    def test_same_connection_write_invalidates_and_forces_guard_false(self):
        with storage.full_status_schema_read(self.conn), self.count_migrations() as calls:
            storage.init_schema(self.conn)
            self.conn.execute("UPDATE settings SET value='true' WHERE key='context_memory_writer_enabled'");self.conn.commit()
            storage.init_schema(self.conn)
            self.assertEqual(self.conn.execute("SELECT value FROM settings WHERE key='context_memory_writer_enabled'").fetchone()[0],'false')
            self.assertEqual(calls.call_count,2)
            storage.init_schema(self.conn);storage.init_schema(self.conn)
            self.assertEqual(calls.call_count,3)

    def test_external_commit_invalidates_the_same_request(self):
        other=storage.connect(self.path);self.addCleanup(other.close)
        with storage.full_status_schema_read(self.conn),self.count_migrations() as calls:
            storage.init_schema(self.conn)
            other.execute("UPDATE settings SET value='true' WHERE key='context_source_revocation_cascade_ready'");other.commit()
            storage.init_schema(self.conn)
            self.assertEqual(calls.call_count,2)
            self.assertEqual(self.conn.execute("SELECT value FROM settings WHERE key='context_source_revocation_cascade_ready'").fetchone()[0],'false')

    def test_rolled_back_writes_do_not_reuse_prior_observation(self):
        with storage.full_status_schema_read(self.conn),self.count_migrations() as calls:
            storage.init_schema(self.conn)
            self.conn.execute("UPDATE settings SET value='true' WHERE key='context_memory_writer_enabled'");self.conn.rollback()
            storage.init_schema(self.conn)
            self.assertEqual(calls.call_count,2)

    def test_existing_no_write_transaction_keeps_the_committed_observation(self):
        with storage.full_status_schema_read(self.conn),self.count_migrations() as calls:
            storage.init_schema(self.conn)
            self.conn.execute('BEGIN')
            storage.init_schema(self.conn)
            self.assertTrue(self.conn.in_transaction)
            self.assertEqual(calls.call_count,1)
            self.conn.rollback()

    def test_ddl_invalidates_even_without_row_writes(self):
        with storage.full_status_schema_read(self.conn),self.count_migrations() as calls:
            storage.init_schema(self.conn)
            self.conn.execute('CREATE TABLE status_fixture(id INTEGER)');self.conn.commit()
            storage.init_schema(self.conn)
            self.assertEqual(calls.call_count,2)

    def test_different_connection_never_borrows_the_observation(self):
        other=storage.connect(self.path);self.addCleanup(other.close)
        with storage.full_status_schema_read(self.conn),self.count_migrations() as calls:
            storage.init_schema(self.conn);storage.init_schema(other);storage.init_schema(other)
            self.assertEqual(calls.call_count,3)

    def test_downgrade_rejection_runs_even_after_a_cached_noop(self):
        with storage.full_status_schema_read(self.conn):
            storage.init_schema(self.conn)
            self.conn.execute("UPDATE meta SET value='999' WHERE key='schema_version'");self.conn.commit()
            with self.assertRaisesRegex(sqlite3.DatabaseError,'refusing downgrade'):storage.init_schema(self.conn)

    def test_multi_batch_backfill_runs_to_a_true_noop_before_reuse(self):
        self.conn.executemany('INSERT INTO frame_signals(id,session_id,captured_at,deletion_group_id) VALUES(?,?,?,?)',
            [(str(n),'fixture','2026-10-16T00:00:00+00:00','fixture') for n in range(5001)])
        self.conn.commit()
        missing=lambda:self.conn.execute("SELECT count(*) FROM frame_signals WHERE COALESCE(screen_region_signal,'')=''").fetchone()[0]
        with storage.full_status_schema_read(self.conn),self.count_migrations() as calls:
            storage.init_schema(self.conn);self.assertEqual(missing(),1)
            storage.init_schema(self.conn);self.assertEqual(missing(),0)
            storage.init_schema(self.conn);storage.init_schema(self.conn)
            self.assertEqual(calls.call_count,3)

    def test_fault_injection_never_uses_or_leaves_cached_success(self):
        with storage.full_status_schema_read(self.conn),self.count_migrations() as calls:
            storage.init_schema(self.conn)
            fault=lambda phase:None
            storage.init_schema(self.conn,candidate_rebuild_fault=fault)
            self.assertEqual(calls.call_count,2)
            storage.init_schema(self.conn);self.assertEqual(calls.call_count,3)

    def test_failed_initialization_cannot_be_reused(self):
        with storage.full_status_schema_read(self.conn):
            with patch.object(storage,'_init_schema_locked',side_effect=sqlite3.OperationalError('fixture failure')):
                with self.assertRaisesRegex(sqlite3.OperationalError,'fixture failure'):storage.init_schema(self.conn)
            with self.count_migrations() as calls:
                storage.init_schema(self.conn);self.assertEqual(calls.call_count,1)

    def test_first_observation_inside_transaction_cannot_create_a_stamp(self):
        with storage.full_status_schema_read(self.conn),self.count_migrations() as calls:
            self.conn.execute('BEGIN')
            storage.init_schema(self.conn);storage.init_schema(self.conn)
            self.assertEqual(calls.call_count,2)
            self.assertTrue(self.conn.in_transaction);self.conn.rollback()

    def test_stale_wal_read_transaction_cannot_borrow_a_committed_stamp(self):
        other=storage.connect(self.path);self.addCleanup(other.close)
        with storage.full_status_schema_read(self.conn):
            storage.init_schema(self.conn)
            self.conn.execute('BEGIN')
            self.conn.execute("SELECT value FROM settings WHERE key='context_memory_writer_enabled'").fetchone()
            other.execute("UPDATE settings SET value='true' WHERE key='context_memory_writer_enabled'");other.commit()
            with self.assertRaisesRegex(sqlite3.OperationalError,'locked'):
                storage.init_schema(self.conn)
            self.assertTrue(self.conn.in_transaction)
            self.conn.rollback();storage.init_schema(self.conn)
            self.assertEqual(self.conn.execute("SELECT value FROM settings WHERE key='context_memory_writer_enabled'").fetchone()[0],'false')

    def test_ignored_insert_reuse_never_commits_or_rolls_back_callers_work(self):
        with storage.full_status_schema_read(self.conn),self.count_migrations() as calls:
            storage.init_schema(self.conn)
            self.conn.execute("INSERT OR IGNORE INTO settings(key,value,updated_at) VALUES('context_memory_writer_enabled','true','fixture')")
            storage.init_schema(self.conn)
            self.assertEqual(calls.call_count,1);self.assertTrue(self.conn.in_transaction)
            self.conn.execute("INSERT INTO settings(key,value,updated_at) VALUES('caller-fixture','private','fixture')")
            self.conn.rollback()
            self.assertIsNone(self.conn.execute("SELECT value FROM settings WHERE key='caller-fixture'").fetchone())
            storage.init_schema(self.conn);self.assertEqual(calls.call_count,2)

    def test_root_lineage_lookup_uses_covering_index_and_keeps_rows(self):
        before=self.conn.execute('SELECT count(*) FROM file_change_versions').fetchone()[0]
        plans=self.conn.execute('EXPLAIN QUERY PLAN SELECT document_path_hmac FROM file_change_versions WHERE root_id=?',('retired-fixture',)).fetchall()
        self.assertTrue(any('COVERING INDEX idx_file_change_versions_root_document' in r[3] for r in plans))
        storage.init_schema(self.conn)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM file_change_versions').fetchone()[0],before)

    def test_legacy_version_column_is_migrated_before_index_creation(self):
        self.conn.execute('DROP INDEX idx_file_change_versions_root_document')
        self.conn.execute('ALTER TABLE file_change_versions DROP COLUMN document_path_hmac');self.conn.commit()
        storage.init_schema(self.conn)
        names={r[1] for r in self.conn.execute('PRAGMA table_info(file_change_versions)')}
        self.assertIn('document_path_hmac',names)
        self.assertTrue(any('COVERING INDEX idx_file_change_versions_root_document' in r[3] for r in
            self.conn.execute('EXPLAIN QUERY PLAN SELECT document_path_hmac FROM file_change_versions WHERE root_id=?',('fixture',))))

    def test_missing_or_changed_forced_off_flag_is_still_repaired(self):
        self.conn.execute("DELETE FROM settings WHERE key='context_memory_writer_enabled'")
        self.conn.execute("UPDATE settings SET value='true',updated_at='fixture' WHERE key='context_source_revocation_cascade_ready'");self.conn.commit()
        with storage.full_status_schema_read(self.conn):storage.init_schema(self.conn)
        rows=self.conn.execute("SELECT key,value,updated_at FROM settings WHERE key IN ('context_memory_writer_enabled','context_source_revocation_cascade_ready')").fetchall()
        self.assertEqual(len(rows),2);self.assertTrue(all(r['value']=='false' and r['updated_at']!='fixture' for r in rows))

    def test_ha_forced_off_noop_keeps_stamp_but_actual_changes_are_repaired(self):
        from activity_mvp.ha_automation import ensure_ha_automation_defaults
        ensure_ha_automation_defaults(self.conn);self.conn.commit()
        before=tuple(self.conn.execute("SELECT value,updated_at FROM settings WHERE key='ha_auto_package_enabled'").fetchone())
        with storage.full_status_schema_read(self.conn),self.count_migrations() as calls:
            storage.init_schema(self.conn)
            ensure_ha_automation_defaults(self.conn);self.conn.commit();storage.init_schema(self.conn)
            self.assertEqual(calls.call_count,1)
        self.assertEqual(tuple(self.conn.execute("SELECT value,updated_at FROM settings WHERE key='ha_auto_package_enabled'").fetchone()),before)
        self.conn.execute("UPDATE settings SET value='true',updated_at='fixture' WHERE key='ha_auto_package_enabled'");self.conn.commit()
        ensure_ha_automation_defaults(self.conn);self.conn.commit()
        after=self.conn.execute("SELECT value,updated_at FROM settings WHERE key='ha_auto_package_enabled'").fetchone()
        self.assertEqual(after['value'],'false');self.assertNotEqual(after['updated_at'],'fixture')
        self.conn.execute("DELETE FROM settings WHERE key='ha_auto_package_enabled'");self.conn.commit()
        ensure_ha_automation_defaults(self.conn)
        self.assertEqual(self.conn.execute("SELECT value FROM settings WHERE key='ha_auto_package_enabled'").fetchone()[0],'false')

    def test_full_status_observes_each_session_but_only_persists_real_transitions(self):
        from activity_mvp import daemon
        daemon.ensure_daemon_defaults(self.conn)
        evidence={'state':'unlocked','locked':False,'on_console':True,'source':'fixture','capture_allowed':True}
        def states():return [tuple(r) for r in self.conn.execute("SELECT key,value,updated_at FROM daemon_state WHERE key IN ('locked','session_state') ORDER BY key")]
        with patch.object(daemon,'session_state',return_value=evidence) as observe,patch.object(daemon,'_launch_agent_status',return_value={'loaded':False,'running':False,'pid':'','last_exit_code':''}):
            with patch.object(daemon,'utc_now',return_value='2026-10-16T00:00:00+00:00'):
                daemon.get_status(self.conn,self.path.parent)
            before=states();transitions=self.conn.execute('SELECT count(*) FROM session_transition_evidence').fetchone()[0]
            with patch.object(daemon,'utc_now',return_value='2026-10-16T00:01:00+00:00'):
                daemon.get_status(self.conn,self.path.parent)
            self.assertEqual(states(),before)
            self.assertEqual(self.conn.execute('SELECT count(*) FROM session_transition_evidence').fetchone()[0],transitions)
            self.assertEqual(observe.call_count,2)
            evidence.update(state='locked',locked=True,capture_allowed=False)
            with patch.object(daemon,'utc_now',return_value='2026-10-16T00:02:00+00:00'):
                daemon.get_status(self.conn,self.path.parent)
            self.assertEqual(observe.call_count,3)
            self.assertEqual(self.conn.execute('SELECT count(*) FROM session_transition_evidence').fetchone()[0],transitions+1)
            self.assertTrue(all(r[2]=='2026-10-16T00:02:00+00:00' for r in states()))
