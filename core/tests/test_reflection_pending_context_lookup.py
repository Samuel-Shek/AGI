"""Synthetic replay barriers: validate referenced contexts without scanning others."""
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from activity_mvp.reflection import analysis, sources
from activity_mvp.conversation_scope import personal_store


class Raw:
    def __init__(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript('''
            CREATE TABLE contexts(id TEXT PRIMARY KEY,source_id TEXT,session TEXT);
            CREATE TABLE calls(id TEXT PRIMARY KEY,state TEXT,detail TEXT);
        ''')
        self.conn.executemany('INSERT INTO contexts VALUES(?,?,?)',
                             [(key, 'codex', key+'-session') for key in ['owned', 'denied'] + ['unrelated-' + str(i) for i in range(2000)]])
        self.checked = []
        self.queries = []
        self.values = {}
        self.conn.create_function('admitted', 1, self.admitted)

    def admitted(self, ident):
        self.checked.append(ident)
        return int(ident == 'owned' or ident.startswith('unrelated-'))

    def rows(self, sql, args=()):
        self.queries.append((sql, tuple(args)))
        return [dict(r) for r in self.conn.execute(sql, args)]

    def set(self, key, value):
        self.values[key] = value


class PendingContextLookupTests(unittest.TestCase):
    def setUp(self):
        self.raw = Raw()
        self.addCleanup(self.raw.conn.close)
        views = {
            'contexts': 'admitted(id)=1',
            'calls': "json_valid(detail) AND json_type(detail,'$.input_ranges')='object' "
                     "AND EXISTS(SELECT 1 FROM json_each(detail,'$.input_ranges')) "
                     "AND NOT EXISTS(SELECT 1 FROM json_each(detail,'$.input_ranges') j "
                     "WHERE j.key NOT IN (SELECT id FROM personal_contexts))",
        }
        self.store = personal_store.FilteredStore(self.raw, list(views), views)
        # This wrapper records SQL shape without changing its real SQLite view.
        original_rows = self.store.rows
        self.filtered_queries = []
        def rows(sql, args=()):
            self.filtered_queries.append((sql, tuple(args)))
            return original_rows(sql, args)
        object.__setattr__(self.store, 'rows', rows)
        self.feedback = type('Feedback', (), {'root': Path('/synthetic/no-private-runs')})()
        for target, name, value in (
                (sources.personal_scope, 'company_stopped', True),
                (sources.personal_scope, 'admitted', False)):
            guard = patch.object(target, name, return_value=value)
            guard.start(); self.addCleanup(guard.stop)
        for name in ('legacy_input_fingerprint',):
            guard = patch.object(analysis, name, side_effect=AssertionError('private run body read'))
            guard.start(); self.addCleanup(guard.stop)
        guard = patch.object(analysis.model, 'invoke', side_effect=AssertionError('model invoked'))
        guard.start(); self.addCleanup(guard.stop)

    def seed(self, ranges, state='unknown'):
        detail = {'input_fingerprint': 'f' * 64, 'input_ranges': ranges, 'reason': 'original'}
        self.raw.conn.execute('INSERT INTO calls VALUES(?,?,?)', ('a' * 24, state, json.dumps(detail)))
        return self.raw.rows('SELECT * FROM calls')

    def run_prior(self, active=lambda: True):
        return analysis.submitted_inputs(self.store, self.feedback, active)

    def test_mixed_batch_quarantines_without_unrelated_body_checks(self):
        original = self.seed({'owned': ['1' * 64], 'denied': ['2' * 64]})
        quarantined=set()
        self.assertEqual(analysis.submitted_inputs(self.store,self.feedback,lambda:True,quarantined_contexts=quarantined),
                         ({'f'*64},{'owned':{'1'*64},'denied':{'2'*64}}))
        self.assertEqual(quarantined, {'owned','denied'})
        self.assertFalse(any(key.startswith('unrelated-') for key in self.raw.checked))
        self.assertEqual(self.raw.rows('SELECT * FROM calls'), original)
        self.assertFalse(any('SELECT id,state,detail' in sql for sql, _ in self.raw.queries))

    def test_stop_prevents_context_metadata_validation(self):
        self.seed({'owned': ['1' * 64], 'denied': ['2' * 64]})
        with self.assertRaisesRegex(PermissionError, 'not_active'):
            self.run_prior(lambda: False)
        self.assertEqual(self.raw.checked, [])

    def test_no_pending_calls_do_not_validate_contexts(self):
        self.assertEqual(self.run_prior(), (set(), {}))
        self.assertEqual(self.raw.checked, [])

    def test_owned_unknown_input_keeps_identical_barrier_and_state(self):
        original = self.seed({'owned': ['1' * 64]})
        self.assertEqual(self.run_prior(), ({'f' * 64}, {'owned': {'1' * 64}}))
        self.assertEqual(self.raw.rows('SELECT * FROM calls'), original)
        self.assertEqual(self.raw.values, {})

    def test_unadmitted_only_call_remains_filtered_without_private_read(self):
        original = self.seed({'denied': ['2' * 64]})
        self.assertEqual(self.run_prior(), ({'f'*64}, {'denied': {'2'*64}}))
        self.assertEqual(self.raw.rows('SELECT * FROM calls'), original)

    def test_missing_range_still_fails_closed_without_context_scan(self):
        self.seed(None)
        self.assertIsNone(self.run_prior())
        self.assertEqual(self.raw.checked, [])

    def test_validation_failure_keeps_identical_replay_barrier(self):
        original = self.seed({'owned': ['1' * 64]}, 'validation_failed')
        self.assertEqual(self.run_prior(), ({'f' * 64}, {'owned': {'1' * 64}}))
        self.assertEqual(self.raw.rows('SELECT * FROM calls'), original)

    def test_large_metadata_set_is_bound_and_preserves_mixed_rejection(self):
        ranges = {'owned': ['1' * 64], **{'missing-' + str(i): ['2' * 64] for i in range(125)}}
        self.seed(ranges)
        self.assertIsNone(self.run_prior())
        self.assertIn('replay_gap',self.raw.values)
        self.assertFalse(any(key.startswith('unrelated-') for key in self.raw.checked))


if __name__ == '__main__':
    unittest.main()
