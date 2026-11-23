"""Synthetic source fan-out, revocation and membership edge cases."""
import sqlite3
import time
import unittest
from unittest.mock import patch

from activity_mvp import conversation_scope as scope
from activity_mvp.feedback.store import Store as FeedbackStore, packed
from activity_mvp.reflection.store import Store
from activity_mvp.reflection import sources
from tests import test_conversation_scope as fixtures


class ContextQueryTests(unittest.TestCase):
    setUp = fixtures.ConversationAdmissionTests.setUp
    write_source = fixtures.ConversationAdmissionTests.write_source
    write_grant = fixtures.ConversationAdmissionTests.write_grant

    def populate(self, store, count=400):
        now = time.time()
        store.conn.execute("INSERT INTO contexts(id,source_id,session,updated) VALUES('ctx','codex',?,?)",
                           (self.session, now))
        reference = packed({'path': str(self.path), 'parser_version': 2})
        store.conn.executemany('INSERT INTO turns(id,context_id,source_id,session,role,observed,text,reference) VALUES(?,?,?,?,?,?,?,?)',
            [(str(i), 'ctx', 'codex', self.session, 'user', now, 'synthetic', reference) for i in range(count)])

    def test_many_rows_of_one_source_need_one_current_identity_check(self):
        with Store(self.root / 'reflection') as store:
            self.populate(store)
            view = scope.filtered(store, 'reflection')
            with patch.object(scope, 'allowed', wraps=scope.allowed) as checked:
                self.assertEqual(view.rows('SELECT id FROM contexts'), [{'id': 'ctx'}])
            self.assertLessEqual(checked.call_count, 2)

    def test_reusing_readonly_projection_preserves_its_bound_cache_and_revocation(self):
        with Store(self.root / 'reflection') as store:
            self.populate(store, 300)
            with Store(store.root, readonly=True) as read:
                self.assertIs(scope.filtered(read, 'reflection'), read)
                with self.assertRaisesRegex(ValueError, 'kind_mismatch'):
                    scope.filtered(read, 'feedback')
                with patch.object(scope, 'allowed', wraps=scope.allowed) as checked:
                    self.assertEqual(len(scope.filtered(read, 'reflection').rows('SELECT id FROM turns')), 300)
                self.assertLessEqual(checked.call_count, 2)
                self.grant['enabled'] = False; self.grant['revision'] += 1; self.write_grant()
                self.assertEqual(scope.filtered(read, 'reflection').rows('SELECT id FROM turns'), [])

    def test_point_lookup_and_readiness_do_not_admit_unrelated_contexts(self):
        with Store(self.root / 'reflection') as store:
            self.populate(store, 2)
            now = time.time()
            for i in range(200):
                ident = 'unrelated-' + str(i)
                store.conn.execute('INSERT INTO contexts(id,source_id,session,updated) VALUES(?,?,?,?)',
                    (ident, 'codex', ident, now))
                store.conn.execute('INSERT INTO turns(id,context_id,source_id,session,role,observed,text,reference) VALUES(?,?,?,?,?,?,?,?)',
                    (ident, ident, 'codex', ident, 'user', now, 'synthetic', packed({'path': str(self.root / ident)})))
            with Store(store.root, readonly=True) as read:
                with patch.object(scope, 'allowed', wraps=scope.allowed) as checked:
                    self.assertEqual(read.rows("SELECT id FROM turns WHERE id='0'"), [{'id':'0'}])
                    self.assertTrue(sources.automatic_contexts_admitted(read))
                self.assertLessEqual(checked.call_count, 3)

    def test_archived_context_indexes_are_added_only_by_writable_open(self):
        with Store(self.root / 'reflection') as store:
            self.populate(store, 2)
            store.conn.execute('CREATE TABLE retired_turns AS SELECT * FROM turns')
            store.conn.execute('CREATE TABLE retired_contexts AS SELECT * FROM contexts')
            before = list(store.conn.iterdump())
            with Store(store.root, readonly=True):
                pass
            self.assertEqual(list(store.conn.iterdump()), before)
        with Store(self.root / 'reflection') as store:
            indexes = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            self.assertTrue({'retired_turns_context', 'retired_contexts_id'} <= indexes)

    def test_same_connection_rechecks_native_source_after_change(self):
        with Store(self.root / 'reflection') as store:
            self.populate(store)
            view = scope.filtered(store, 'reflection')
            self.assertEqual(len(view.rows('SELECT id FROM contexts')), 1)
            self.lines[0]['payload']['cwd'] = str(self.root / 'company')
            self.write_source()
            self.assertEqual(view.rows('SELECT id FROM contexts'), [])
            self.assertEqual(view.rows('SELECT id FROM turns'), [])

    def test_readonly_checks_all_distinct_paths_and_preserves_rows(self):
        with Store(self.root / 'reflection') as store:
            self.populate(store, 30)
            other = self.path.with_name('other.jsonl')
            other.write_text(self.path.read_text()); other.chmod(0o600)
            store.conn.execute('UPDATE turns SET reference=? WHERE id=?',
                               (packed({'path': str(other), 'parser_version': 2}), '29'))
            before = list(store.conn.iterdump())
            with Store(store.root, readonly=True) as read:
                self.assertEqual(len(read.rows('SELECT id FROM contexts')), 1)
                other.unlink()
                self.assertEqual(read.rows('SELECT id FROM contexts'), [])
            self.assertEqual(list(store.conn.iterdump()), before)

    def test_retired_denied_source_still_excludes_the_whole_context(self):
        with Store(self.root / 'reflection') as store:
            self.populate(store, 2)
            store.conn.execute('CREATE TABLE retired_turns AS SELECT * FROM turns WHERE 0')
            store.conn.execute('CREATE TABLE retired_contexts AS SELECT * FROM contexts WHERE 0')
            store.conn.execute('INSERT INTO retired_turns SELECT * FROM turns WHERE id=?', ('1',))
            store.conn.execute('UPDATE retired_turns SET reference=?', (packed({'path': str(self.root / 'missing')}),))
            view = scope.filtered(store, 'reflection')
            self.assertEqual(view.rows('SELECT id FROM contexts'), [])
            self.assertEqual(view.rows('SELECT id FROM turns'), [])

    def test_invalid_reference_in_one_row_is_not_lost_by_deduplication(self):
        with Store(self.root / 'reflection') as store:
            self.populate(store, 2)
            for reference in ('{}', '{broken', packed({'path': None}), packed({'path': 12})):
                with self.subTest(reference=reference):
                    store.conn.execute('UPDATE turns SET reference=? WHERE id=?', (reference, '1'))
                    self.assertEqual(scope.filtered(store, 'reflection').rows('SELECT id FROM contexts'), [])

    def test_old_rows_do_not_create_a_recent_observation_witness(self):
        with Store(self.root / 'reflection') as store:
            self.populate(store, 2)
            store.conn.execute('UPDATE turns SET observed=?', (time.time() - 15 * 86400,))
            view = scope.filtered(store, 'reflection')
            self.assertEqual(view.rows('SELECT id FROM contexts'), [])
            store.conn.execute('UPDATE turns SET observed=? WHERE id=?', (time.time(), '1'))
            self.assertEqual(view.rows('SELECT id FROM contexts'), [{'id': 'ctx'}])

    def test_registered_legacy_observations_keep_their_existing_window_rule(self):
        with Store(self.root / 'reflection') as store:
            self.populate(store, 2)
            store.conn.execute('UPDATE turns SET observed=?', (time.time() - 15 * 86400,))
            self.policy['personal_session_ids'].append(self.session)
            self.assertEqual(scope.filtered(store, 'reflection').rows('SELECT id FROM contexts'), [{'id': 'ctx'}])

    def test_same_path_with_another_session_is_still_a_separate_denied_source(self):
        with Store(self.root / 'reflection') as store:
            self.populate(store, 2)
            store.conn.execute("UPDATE turns SET session='another-session' WHERE id='1'")
            self.assertEqual(scope.filtered(store, 'reflection').rows('SELECT id FROM contexts'), [])

    def test_query_time_revocation_invalidates_the_bound_action(self):
        with Store(self.root / 'reflection') as store:
            self.populate(store, 2)
            view = scope.filtered(store, 'reflection'); active = scope.bind()
            original = scope.allowed
            def checked(*args):
                result = original(*args)
                self.grant['enabled'] = False; self.grant['revision'] += 1; self.write_grant()
                return result
            with patch.object(scope, 'allowed', side_effect=checked):
                view.rows('SELECT id FROM contexts')
            self.assertFalse(active())
            self.assertEqual(scope.filtered(store, 'reflection').rows('SELECT id FROM contexts'), [])

    def test_no_legacy_rows_do_not_reopen_current_native_sources_for_migration(self):
        with Store(self.root / 'reflection') as store, FeedbackStore(self.root / 'feedback') as feedback:
            self.populate(store, 400)
            store.set('enabled', True); store.set('source_parser_version', 2)
            feedback.set('config', {'enabled': True})
            before = store.rows('SELECT id,reference FROM turns')
            with patch.object(scope, 'allowed', wraps=scope.allowed) as checked:
                result = sources.scan(store, feedback)
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(checked.call_count, 0)
            self.assertEqual(store.rows('SELECT id,reference FROM turns'), before)

    def test_automatic_admission_still_rejects_turns_without_context(self):
        with Store(self.root / 'reflection') as store:
            self.populate(store, 2)
            self.assertTrue(sources.automatic_contexts_admitted(store))
            store.conn.execute('DELETE FROM contexts')
            self.assertFalse(sources.automatic_contexts_admitted(store))

    def test_feedback_job_does_not_check_unreferenced_messages(self):
        with FeedbackStore(self.root / 'feedback') as store:
            now = time.time(); ref = packed({'path': str(self.path)})
            store.conn.executemany('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)',
                [(str(i), 'codex', self.session, now, 'synthetic', '', ref, 'pending', now, 0) for i in range(400)])
            store.conn.execute("INSERT INTO incidents(id,title,category,state,created,updated) VALUES('i','synthetic','execution','investigating',?,?)", (now, now))
            store.conn.execute("INSERT INTO quotes VALUES('0','i')")
            store.conn.execute("INSERT INTO jobs(id,incident_id,revision,state,created,updated) VALUES('j','i',1,'prepared',?,?)", (now, now))
            view = scope.filtered(store)
            with patch.object(scope, 'allowed', wraps=scope.allowed) as checked:
                self.assertEqual(view.rows("SELECT id FROM jobs WHERE id='j'"), [{'id': 'j'}])
            self.assertLessEqual(checked.call_count, 2)
            self.lines[0]['payload']['source'] = 'exec'; self.write_source()
            self.assertEqual(view.rows("SELECT id FROM jobs WHERE id='j'"), [])


class MembershipCompatibilityTests(unittest.TestCase):
    def test_negative_membership_keeps_empty_and_null_set_behavior(self):
        old = ("EXISTS(SELECT 1 FROM main.quotes q WHERE q.incident_id=incidents.id) "
               "AND NOT EXISTS(SELECT 1 FROM main.quotes q WHERE q.incident_id=incidents.id "
               "AND q.message_id NOT IN (SELECT id FROM temp.personal_messages))")
        views = {'incidents': old}
        scope._bounded_relations(views, set(), 'feedback')
        with sqlite3.connect(':memory:') as db:
            db.executescript("CREATE TABLE messages(id TEXT); CREATE TABLE quotes(message_id TEXT,incident_id TEXT); "
                             "CREATE TABLE incidents(id TEXT); INSERT INTO incidents VALUES('i'); "
                             "CREATE TEMP VIEW personal_messages AS SELECT * FROM main.messages;")
            for ids in ([], ['known'], [None], ['known', None]):
                for reference in ('known', 'missing', None):
                    with self.subTest(ids=ids, reference=reference):
                        db.execute('DELETE FROM messages'); db.execute('DELETE FROM quotes')
                        db.executemany('INSERT INTO messages VALUES(?)', [(x,) for x in ids])
                        db.execute("INSERT INTO quotes VALUES(?,'i')", (reference,))
                        original = db.execute('SELECT id FROM incidents WHERE ' + old).fetchall()
                        candidate = db.execute('SELECT id FROM incidents WHERE ' + views['incidents']).fetchall()
                        self.assertEqual(candidate, original)


if __name__ == '__main__':
    unittest.main()
