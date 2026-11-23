"""Actual draft storage and source checks; no production data or sends.

The unrelated-reader sentinel is a deterministic work budget. It detects the
global dependency that made real draft receipts exceed their 20-second budget,
without timing a synthetic sleep or mocking the target's source decision.
"""
import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from activity_mvp import attention_center as center, attention_handoff as handoff
from activity_mvp.storage import ensure_ready
from activity_mvp.presentation_scope import POLICY_FILE
from tests import test_activity_confirmation_cards_v5 as cards_fixtures
from tests import test_attention_center as center_fixtures


class ActivityDraftSourceReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.database = self.root/'activity.sqlite'
        self.conn = ensure_ready(self.database)
        self.addCleanup(self.conn.close)
        self.base = datetime.now().astimezone().replace(hour=9, minute=0, second=0, microsecond=0)
        self.policy = self.root/POLICY_FILE
        self.policy_value = {'version':1, 'enabled':True, 'retired_names':['ExcludedFixture'],
                             'retired_paths':[], 'retired_root_refs':[]}
        self.write_policy()
        self.insert('past', at=self.base-timedelta(days=20), object_label='PersonalFixture announcement')
        self.insert('today', object_label='CurrentFixture announcement')
        self.conn.commit()
        self.queue = handoff.Queue(self.database)

    def write_policy(self):
        self.policy.write_text(json.dumps(self.policy_value))
        self.policy.chmod(0o600)

    def insert(self, key, **kw):
        cards_fixtures.ActivityConfirmationCardsV5Tests._insert(self, key, **kw)

    def no_unrelated_readers(self):
        stack = ExitStack()
        # Close even when a later import raises before the caller enters `with`.
        self.addCleanup(stack.close)
        for target, name in ((center.memory, 'memory_review_candidates'),
                             (center, 'get_quick_status'),
                             (center, '_hermes_compat_issues')):
            stack.enter_context(patch.object(target, name,
                side_effect=AssertionError('unrelated_subsystem_read_budget_exceeded')))
        from activity_mvp import task_closure
        from activity_mvp.feedback import autonomy
        stack.enter_context(patch.object(task_closure, 'projection',
            side_effect=AssertionError('unrelated_closure_read_budget_exceeded')))
        stack.enter_context(patch.object(autonomy, 'snapshot',
            side_effect=AssertionError('unrelated_autonomy_read_budget_exceeded')))
        return stack

    def test_latest_draft_and_receipt_do_not_depend_on_unrelated_history(self):
        with self.no_unrelated_readers():
            first = self.queue.save_draft('historical-questions', 'A', 100)
            second = self.queue.save_draft('historical-questions', 'B', 200)
            late = self.queue.save_draft('historical-questions', 'A', 100)
            reopened = handoff.Queue(self.database).status('historical-questions')
            cleared = self.queue.save_draft('historical-questions', '', 300)
        self.assertEqual((first['draft'], first['draft_revision']), ('A', 100))
        for value in (second, late, reopened):
            self.assertEqual((value['status'], value['draft'], value['draft_revision']), ('ok', 'B', 200))
            self.assertIsNone(value['handoff'])
        self.assertEqual((cleared['draft'], cleared['draft_revision']), ('', 300))
        with self.queue.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM submissions').fetchone()[0], 0)

    def test_current_and_historical_activity_use_real_source_cards(self):
        with self.no_unrelated_readers():
            for key in ('today', 'past'):
                value = center.detail(self.conn, 'activity-grp_'+key)
                self.assertEqual(value['status'], 'ok')
                self.assertEqual(value['item']['card']['group_key'], 'grp_'+key)
            self.assertEqual(center.detail(self.conn, 'activity-grp-missing')['status'], 'changed')

    def test_targeted_detail_matches_existing_full_projection(self):
        # Use existing synthetic setup for a complete snapshot, never stub its
        # activity reader or target detail route.
        fixture = center_fixtures.AttentionCenterTests(methodName='runTest')
        fixture.setUp()
        try:
            fixture.insert('old', at=fixture.base-timedelta(days=20), object_label='PersonalFixture announcement')
            fixture.conn.commit()
            policy = fixture.root/POLICY_FILE
            policy.write_text(json.dumps(self.policy_value)); policy.chmod(0o600)
            full = fixture.snapshot()
            group = next(x for x in full['resources'] if x['id']=='historical-questions')
            detail = center.detail(fixture.conn, 'historical-questions', **fixture.paths)
            self.assertEqual(detail['item'], group)
            child = group['children'][0]
            self.assertEqual(center.detail(fixture.conn, child['id'], **fixture.paths)['item'], child)
        finally:
            fixture.tearDown()

    def test_withdrawn_historical_parent_also_withholds_child(self):
        self.policy_value['retired_names'].append('历史待答复事项')
        self.write_policy()
        for key in ('historical-questions', 'activity-grp_past'):
            self.assertEqual(center.detail(self.conn, key)['status'], 'changed')
            with self.assertRaises(handoff.HandoffError):
                self.queue.save_draft(key, 'must not save', 100)
        self.assertEqual(center.detail(self.conn, 'activity-grp_today')['status'], 'ok')

    def test_partly_withdrawn_history_matches_full_projection(self):
        fixture = center_fixtures.AttentionCenterTests(methodName='runTest')
        fixture.setUp()
        try:
            for key, name in (('kept', 'PersonalFixture'), ('retired', 'ExcludedFixture')):
                fixture.insert(key, at=fixture.base-timedelta(days=20), object_label=name+' announcement')
            fixture.conn.commit()
            policy = fixture.root/POLICY_FILE
            policy.write_text(json.dumps(self.policy_value)); policy.chmod(0o600)
            full = fixture.snapshot()
            group = next(x for x in full['resources'] if x['id']=='historical-questions')
            detail = center.detail(fixture.conn, 'historical-questions', **fixture.paths)
            self.assertEqual(detail['item'], group)
            self.assertEqual([x['id'] for x in group['children']], ['activity-grp_kept'])
            self.assertEqual(center.detail(fixture.conn, 'activity-grp_retired')['status'], 'changed')
        finally:
            fixture.tearDown()

    def test_missing_activity_cannot_authorize_new_draft(self):
        self.conn.execute("DELETE FROM activity_confirmation_items WHERE item_key='past'")
        self.conn.commit()
        with self.no_unrelated_readers():
            with self.assertRaises(handoff.HandoffError):
                self.queue.save_draft('historical-questions', 'new', 100)
        with self.queue.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM drafts').fetchone()[0], 0)

    def test_retired_source_withholds_existing_draft_and_rejects_new_edit(self):
        self.queue.save_draft('historical-questions', 'saved', 100)
        self.policy_value['retired_names'].append('PersonalFixture')
        self.write_policy()
        value = self.queue.status('historical-questions')
        self.assertEqual((value['status'], value['draft']), ('unavailable', ''))
        with self.assertRaises(handoff.HandoffError):
            self.queue.save_draft('historical-questions', 'must not save', 200)
        with self.queue.db() as db:
            self.assertEqual(tuple(db.execute('SELECT content,client_revision FROM drafts').fetchone()), ('saved', 100))

    def test_policy_change_during_target_read_withholds_result(self):
        original = center._cards
        def change_policy(*args, **kw):
            result = original(*args, **kw)
            self.policy_value['retired_names'].append('PersonalFixture')
            self.write_policy()
            return result
        with patch.object(center, '_cards', side_effect=change_policy):
            value = center.detail(self.conn, 'historical-questions')
        self.assertEqual(value['status'], 'unavailable')
        self.assertNotIn('item', value)

    def test_read_failure_after_save_keeps_data_and_unknown_receipt(self):
        original = center._cards
        calls = 0
        def failure_after_write(*args, **kw):
            nonlocal calls
            calls += 1
            if calls >= 3:  # Two original pre-write checks, then the receipt read.
                raise sqlite3.OperationalError('synthetic source unavailable')
            return original(*args, **kw)
        with patch.object(center, '_cards', side_effect=failure_after_write):
            value = self.queue.save_draft('historical-questions', 'B', 200)
        self.assertEqual((value['status'], value['draft']), ('unavailable', ''))
        recovered = self.queue.status('historical-questions')
        self.assertEqual((recovered['draft'], recovered['draft_revision']), ('B', 200))

    def test_detail_does_not_answer_or_modify_activity(self):
        before = list(self.conn.iterdump())
        center.detail(self.conn, 'historical-questions')
        center.detail(self.conn, 'activity-grp_today')
        self.assertEqual(list(self.conn.iterdump()), before)


if __name__ == '__main__':
    unittest.main()
