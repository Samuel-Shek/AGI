"""Synthetic status reads: preserve admission, bound repeated path work."""
from pathlib import Path
from types import SimpleNamespace
import io
import json
import sqlite3
import time
import unittest
from unittest.mock import Mock, patch

from activity_mvp import conversation_scope as scope, presentation_scope as display
from activity_mvp.feedback import cli, store as feedback_store
from activity_mvp.feedback.work import WorkQueue
from activity_mvp.retirement_snapshot import CURRENT, Snapshot, source_snapshot
from tests import test_conversation_scope as fixtures


class FeedbackStatusSnapshotTests(unittest.TestCase):
    # Reuse setup helpers without inheriting or rerunning their test methods.
    write_source = fixtures.ConversationAdmissionTests.write_source
    write_grant = fixtures.ConversationAdmissionTests.write_grant
    revoke = fixtures.ConversationAdmissionTests.revoke

    def setUp(self):
        fixtures.ConversationAdmissionTests.setUp(self)
        bridge = patch.object(scope.personal_scope, 'BRIDGE', self.governance / 'bridge.json')
        bridge.start(); self.addCleanup(bridge.stop)
        self.now = time.time()
        self.feedback = self.root / 'feedback'
        self.write_policy()
        policy = self.root / display.POLICY_FILE
        policy.write_text(feedback_store.packed({'version': 1, 'enabled': True,
            'retired_names': [], 'retired_paths': [str(self.root / 'company')],
            'retired_root_refs': []}))
        policy.chmod(0o600)
        self.seed_rows(3)
        coverage = patch('activity_mvp.feedback.memory_coverage.status',
                         return_value={'available': False, 'reason': 'synthetic_fixture'})
        coverage.start(); self.addCleanup(coverage.stop)

    def write_policy(self):
        path = self.governance / 'policy.json'
        path.write_text(feedback_store.packed(self.policy)); path.chmod(0o600)

    def seed_rows(self, count):
        with feedback_store.Store(self.feedback) as store:
            store.set('config', {'enabled': True, 'since': self.now - 14 * 86400})
            store.conn.execute("INSERT OR IGNORE INTO sources(id,platform,path) VALUES('codex','codex',?)",
                               (str(self.native),))
            reference = feedback_store.packed({'path': str(self.path)})
            for i in range(count):
                store.conn.execute('INSERT OR IGNORE INTO messages'
                    '(id,source_id,session,observed,quote,context,reference,state,imported,redacted) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?)', (str(i), 'codex', self.session, self.now,
                    'Synthetic feedback for status verification.', '', reference,
                    'pending', self.now, 0))
            store.conn.execute("INSERT OR IGNORE INTO incidents(id,title,category,state,created,updated) "
                "VALUES('fixture','Synthetic item','acceptance','investigating',?,?)", (self.now, self.now))
            store.conn.execute("INSERT OR IGNORE INTO quotes VALUES('0','fixture')")
            WorkQueue(store).enqueue('feedback', 'fixture', '1')

    def call(self, action='status', payload=None):
        with patch('sys.argv', ['feedback', action, '--root', str(self.feedback), '--payload-stdin']), \
                patch('sys.stdin', io.StringIO(json.dumps(payload or {}))):
            return cli.main()

    def test_status_reuses_bound_classifier_for_many_real_readonly_rows(self):
        self.seed_rows(500)
        scopes = []
        real_store = cli.Store
        real_project = display.project_for_runtime

        def opened(*args, **kwargs):
            source = CURRENT.get()
            shown = display._CURRENT.get()
            store = real_store(*args, **kwargs)
            scopes.append((source, shown, store.conn.scope, store.conn.scope.policy_view()))
            self.assertTrue(store.readonly)
            return store

        def projected(payload, root):
            scopes.append((CURRENT.get(), display._CURRENT.get()))
            return real_project(payload, root)

        # The classifier itself remains the real installed implementation.
        with patch.object(scope.personal_scope, 'admitted', wraps=scope.personal_scope.admitted) as raw, \
                patch.object(cli, 'Store', side_effect=opened), \
                patch.object(display, 'project_for_runtime', side_effect=projected):
            result = self.call()
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['pending_messages'], 500)
        self.assertEqual(result['progress']['pending_message_count'], 500)
        self.assertEqual([x['id'] for x in result['incidents']], ['fixture'])
        self.assertLessEqual(raw.call_count, 8)
        self.assertIsNotNone(scopes[0][0])
        self.assertIs(scopes[0][0].sources, scope.personal_scope)
        self.assertIs(scopes[0][1], scopes[0][2])
        self.assertIs(scopes[0][0], scopes[0][3])
        self.assertIs(scopes[1][0], scopes[0][0])
        self.assertIs(scopes[1][1], scopes[0][1])
        self.assertIsNone(CURRENT.get())
        self.assertIsNone(display._CURRENT.get())

    def test_snapshot_observation_keeps_original_dates_types_paths_and_legacy_order(self):
        self.policy['personal_session_ids'].append('legacy')
        self.write_policy()
        dates = (None, True, 'invalid', float('nan'), self.now - 14 * 86400 - 1,
                 self.now - 14 * 86400, self.now, self.now + 300, self.now + 301)
        paths = (str(self.path), str(self.root / 'company' / 'source'), '', None, 'bad\x00path', 7)
        cases = [(session, path, observed) for session in ('legacy', self.session, None)
                 for path in paths for observed in dates]
        def outcome(case):
            try: return ('value', scope.observation_allowed(*case))
            except (OSError, RuntimeError, ValueError, TypeError, KeyError) as error:
                return ('error', type(error).__name__)
        with patch.object(scope.time, 'time', return_value=self.now):
            expected = [outcome(case) for case in cases]
            with source_snapshot(scope.personal_scope):
                actual = [outcome(case) for case in cases]
        self.assertEqual(actual, expected)
        # An original legacy registration precedes even an invalid date.
        self.assertTrue(scope.observation_allowed('legacy', None, None))
        self.assertFalse(scope.observation_allowed(self.session, str(self.path), None))

    def test_foreign_current_with_same_root_does_not_supply_classification(self):
        other_sources = SimpleNamespace(ROOT=scope.personal_scope.ROOT)
        foreign = SimpleNamespace(sources=other_sources,
            classify=Mock(side_effect=AssertionError('foreign classifier used')))
        token = CURRENT.set(foreign)
        try:
            with patch.object(scope.personal_scope, 'admitted', wraps=scope.personal_scope.admitted) as raw:
                self.assertTrue(scope.observation_allowed(self.session, str(self.path), self.now))
                self.assertEqual(raw.call_count, 1)
            foreign.classify.assert_not_called()
            with self.assertRaises(ValueError): self.call()
            self.assertIs(CURRENT.get(), foreign)
        finally: CURRENT.reset(token)

    def test_classification_error_never_turns_into_date_permission(self):
        with source_snapshot(scope.personal_scope) as snapshot:
            with patch.object(snapshot, 'classify', side_effect=ValueError('synthetic class failure')):
                with self.assertRaises(ValueError):
                    scope.observation_allowed(self.session, str(self.path), self.now)

    def test_no_snapshot_keeps_original_classification_error(self):
        with patch.object(scope.personal_scope, 'admitted', side_effect=ValueError('synthetic class failure')):
            with self.assertRaises(ValueError):
                scope.observation_allowed(self.session, str(self.path), self.now)

    def assert_withheld_before_projection(self, mutation):
        original = display.project_for_runtime
        def change(payload, root):
            mutation()
            return original(payload, root)
        with patch.object(display, 'project_for_runtime', side_effect=change):
            try: result = self.call()
            except (ValueError, PermissionError, sqlite3.OperationalError): result = None
        self.assertTrue(result is None or result.get('status') != 'ok')
        self.assertIsNone(CURRENT.get())
        self.assertIsNone(display._CURRENT.get())

    def test_grant_revocation_before_projection_withholds_success(self):
        self.assert_withheld_before_projection(self.revoke)

    def test_policy_change_before_projection_withholds_success(self):
        def change():
            self.policy['personal_session_ids'].append('unrelated-new-registration')
            self.write_policy()
        self.assert_withheld_before_projection(change)

    def test_display_policy_change_before_projection_withholds_success(self):
        def change():
            path = self.root / display.POLICY_FILE
            value = json.loads(path.read_text()); value['retired_names'] = ['Synthetic']
            path.write_text(feedback_store.packed(value)); path.chmod(0o600)
        self.assert_withheld_before_projection(change)

    def test_company_alias_retarget_before_projection_withholds_success(self):
        first = self.root / 'old-company'; first.mkdir()
        second = self.root / 'new-company'; second.mkdir()
        alias = self.root / 'company-alias'; alias.symlink_to(first)
        self.policy['company_roots'] = [str(alias)]; self.write_policy()
        def change(): alias.unlink(); alias.symlink_to(second)
        self.assert_withheld_before_projection(change)

    def test_source_parent_alias_change_before_projection_withholds_success(self):
        def change():
            moved = self.root / 'moved-synthetic-native'
            self.native.rename(moved); self.native.symlink_to(moved)
        self.assert_withheld_before_projection(change)

    def test_changed_native_company_metadata_is_not_admitted_on_next_status(self):
        self.assertEqual(self.call()['pending_messages'], 3)
        self.lines[0]['payload']['cwd'] = str(self.root / 'company'); self.write_source()
        result = self.call()
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['pending_messages'], 0)
        self.assertEqual(result['incidents'], [])

    def test_revocation_between_status_calls_is_not_cached(self):
        self.assertEqual(self.call()['pending_messages'], 3)
        self.revoke()
        self.assertEqual(self.call()['pending_messages'], 0)

    def test_nested_matching_snapshot_is_reused_and_restored(self):
        with source_snapshot(scope.personal_scope) as outer:
            self.assertEqual(self.call()['status'], 'ok')
            self.assertIs(CURRENT.get(), outer)
            self.assertIsNone(display._CURRENT.get())
        self.assertIsNone(CURRENT.get())

    def test_nested_snapshot_revocation_after_projection_is_rejected_before_return(self):
        outer = Snapshot(scope.personal_scope)
        token = CURRENT.set(outer)
        original = display.project_for_runtime
        def change_after(payload, root):
            result = original(payload, root)
            self.assertEqual(result['status'], 'ok')
            self.revoke()
            return result
        try:
            with patch.object(display, 'project_for_runtime', side_effect=change_after):
                # No outer context-manager exit can make this assertion pass.
                with self.assertRaisesRegex(ValueError, 'source_policy_changed'):
                    self.call()
            self.assertIs(CURRENT.get(), outer)
            self.assertIsNone(display._CURRENT.get())
        finally:
            CURRENT.reset(token)
            outer.close()

    def assert_display_change_after_projection_rejected(self, nested):
        outer = Snapshot(scope.personal_scope) if nested else None
        token = CURRENT.set(outer) if nested else None
        original = display.project_for_runtime
        def change_after(payload, root):
            result = original(payload, root)
            self.assertEqual(result['status'], 'ok')
            path = self.root / display.POLICY_FILE
            value = json.loads(path.read_text()); value['retired_names'] = ['Synthetic']
            path.write_text(feedback_store.packed(value)); path.chmod(0o600)
            return result
        try:
            with patch.object(display, 'project_for_runtime', side_effect=change_after):
                with self.assertRaisesRegex(ValueError, 'display_policy_changed'):
                    self.call()
            self.assertIs(CURRENT.get(), outer)
            self.assertIsNone(display._CURRENT.get())
        finally:
            if nested:
                CURRENT.reset(token)
                outer.close()

    def test_display_change_after_projection_is_rejected_before_return(self):
        self.assert_display_change_after_projection_rejected(False)

    def test_nested_display_change_after_projection_is_rejected_before_return(self):
        self.assert_display_change_after_projection_rejected(True)

    def test_nested_memory_registry_change_after_projection_is_rejected_before_return(self):
        registry = self.root / '.hermes/memory-review-center/state/autonomy-policy.json'
        registry.parent.mkdir(parents=True)
        value = {'owner_id': 'local-owner', 'bindings': {'fixture': [{'path': str(self.path)}]},
                 'investigation_sources': {}}
        registry.write_text(feedback_store.packed(value)); registry.chmod(0o600)
        outer = Snapshot(scope.personal_scope)
        token = CURRENT.set(outer)
        original = display.project_for_runtime
        def change_after(payload, root):
            shown = display._CURRENT.get()
            shown.hidden_memory('fixture')
            self.assertIsNotNone(shown.memory_source_fingerprint)
            result = original(payload, root)
            self.assertEqual(result['status'], 'ok')
            value['bindings'] = {}
            registry.write_text(feedback_store.packed(value)); registry.chmod(0o600)
            return result
        try:
            with patch.object(display.Path, 'home', return_value=self.root), \
                    patch.object(display, 'project_for_runtime', side_effect=change_after):
                with self.assertRaisesRegex(ValueError, 'display_policy_changed'):
                    self.call()
            self.assertIs(CURRENT.get(), outer)
            self.assertIsNone(display._CURRENT.get())
        finally:
            CURRENT.reset(token)
            outer.close()

    def test_nested_stale_display_snapshot_is_not_rebuilt_to_emit_payload(self):
        with display.display_scope(self.root) as shown:
            previous = shown.policy_view()
            with self.assertRaises(ValueError): self.call()
            self.assertIs(shown.source_policy, previous)
            self.assertIs(display._CURRENT.get(), shown)
            self.assertIsNone(CURRENT.get())

    def test_query_exception_restores_both_contexts(self):
        with patch.object(feedback_store.Store, 'snapshot', side_effect=RuntimeError('synthetic read failure')):
            with self.assertRaises(RuntimeError): self.call()
        self.assertIsNone(CURRENT.get())
        self.assertIsNone(display._CURRENT.get())

    def test_pause_action_does_not_enter_read_snapshot(self):
        with patch('activity_mvp.retirement_snapshot.source_snapshot',
                   side_effect=AssertionError('read snapshot entered for action')):
            self.assertEqual(self.call('pause'), {'status': 'paused'})
        self.assertIsNone(CURRENT.get())
        self.assertIsNone(display._CURRENT.get())

    def test_detail_branch_keeps_existing_fresh_read_path(self):
        with patch('activity_mvp.retirement_snapshot.source_snapshot',
                   side_effect=AssertionError('status snapshot entered for detail')):
            self.assertEqual(self.call('detail', {'id': 'fixture'})['status'], 'ok')
        self.assertIsNone(CURRENT.get())
        self.assertIsNone(display._CURRENT.get())


if __name__ == '__main__':
    unittest.main()
