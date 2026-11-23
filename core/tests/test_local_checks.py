"""Synthetic deadlines, resumable reads and unchanged unknown-call barriers."""
import fcntl
import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from activity_mvp import local_checks as checks
from activity_mvp.feedback import service, sources as feedback_sources, model
from activity_mvp.feedback.store import Store as FeedbackStore, packed
from activity_mvp.reflection import sources, analysis
from activity_mvp.reflection.store import Store
from tests import test_conversation_scope as fixtures


class DeadlineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = FeedbackStore(Path(self.temp.name).resolve() / 'feedback'); self.addCleanup(self.store.close)
        self.store.set('config', {'enabled': True})

    def test_sql_timeout_rolls_back_and_connection_is_usable(self):
        clock = [0.0]
        def advance(value):
            clock[0] += .01
            return value
        self.store.conn.create_function('advance', 1, advance)
        with patch.object(checks.time, 'monotonic', side_effect=lambda: clock[0]):
            with self.assertRaises(checks.LocalCheckTimeout):
                with checks.phase(self.store, 'reflection_ready', 1):
                    with self.store.tx():
                        self.store.set('must_rollback', True)
                        self.store.conn.execute('WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<100000) SELECT sum(advance(x)) FROM n').fetchone()
        self.assertIsNone(self.store.get('must_rollback'))
        self.assertEqual(checks.snapshot(self.store)['status'], 'deferred')
        self.assertEqual(self.store.conn.execute('SELECT 1').fetchone()[0], 1)

    def test_lazy_cursor_cannot_outlive_phase_deadline(self):
        clock = [0.0]
        with patch.object(checks.time, 'monotonic', side_effect=lambda: clock[0]):
            with self.assertRaises(checks.LocalCheckTimeout):
                with checks.phase(self.store, 'reflection_ready', 1):
                    cursor = self.store.conn.execute('SELECT 1 UNION ALL SELECT 2')
                    self.assertEqual(cursor.fetchone()[0], 1)
                    clock[0] = 2
                    cursor.fetchall()
        self.assertEqual(self.store.conn.execute('SELECT 3').fetchone()[0], 3)

    def test_nested_callback_preserves_outer_query_deadline(self):
        clock = [0.0]; calls = [0]
        def advance(value):
            calls[0] += 1
            if calls[0] == 1:
                self.assertEqual(self.store.conn.execute('SELECT 7').fetchone()[0], 7)
            clock[0] += .001
            return value
        self.store.conn.create_function('advance', 1, advance)
        with patch.object(checks.time, 'monotonic', side_effect=lambda: clock[0]):
            with self.assertRaises(checks.LocalCheckTimeout):
                with checks.phase(self.store, 'reflection_ready', 1):
                    self.store.conn.execute('WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<20000) SELECT sum(advance(x)) FROM n').fetchone()
        self.assertLess(calls[0], 1200)
        self.assertEqual(self.store.conn.execute('SELECT 3').fetchone()[0], 3)

    def test_cte_read_with_write_words_in_data_is_still_bounded(self):
        clock = [0.0]; calls = [0]
        def advance(value):
            calls[0] += 1; clock[0] += .001
            return value
        self.store.conn.create_function('advance', 1, advance)
        with patch.object(checks.time, 'monotonic', side_effect=lambda: clock[0]):
            with self.assertRaises(checks.LocalCheckTimeout):
                with checks.phase(self.store, 'reflection_ready', 1):
                    self.store.conn.execute("-- UPDATE ignored\nWITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<20000), \"DELETE\" AS (SELECT 'UPDATE' AS tag) SELECT sum(advance(x)) FROM n, \"DELETE\"").fetchone()
        self.assertLess(calls[0], 1200)

    def test_cte_write_is_not_interrupted_by_read_handler(self):
        clock = [0.0]
        def advance(value):
            clock[0] += .01
            return value
        self.store.conn.create_function('advance', 1, advance)
        self.store.conn.execute('CREATE TABLE completed_local_write(value)')
        with patch.object(checks.time, 'monotonic', side_effect=lambda: clock[0]):
            with self.assertRaises(checks.LocalCheckTimeout):
                with checks.phase(self.store, 'reflection_scan', 1):
                    self.store.conn.execute('WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<1000) INSERT INTO completed_local_write SELECT advance(x) FROM n')
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM completed_local_write').fetchone()[0], 1001)

    def test_native_context_tail_query_obeys_the_same_read_deadline(self):
        clock = [0.0]; calls = [0]; now = time.time()
        raw = sqlite3.connect(':memory:'); self.addCleanup(raw.close)
        def advance(value):
            calls[0] += 1; clock[0] += .001
            return now
        raw.create_function('advance', 1, advance)
        raw.execute("CREATE VIEW messages AS WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<20000) SELECT x AS id, 'session' AS session_id, advance(x) AS timestamp, 'user' AS role FROM n")
        reference = {'path': str(self.store.root / 'synthetic.sqlite'), 'session':'session', 'row':1, 'platform':'hermes'}
        # Exercise the real metadata/progress projections before the expensive
        # native tail read; a rows-only fake no longer models the Store API.
        store = Store(Path(self.temp.name).resolve() / 'reflection'); self.addCleanup(store.conn.close)
        store.conn.execute('INSERT INTO sources(id,platform,path,cursor,status) VALUES(?,?,?,?,?)',
            ('hermes','hermes',reference['path'],packed({'sessions':{'session':99999}}),'current'))
        store.conn.execute('INSERT INTO turns(id,context_id,source_id,session,role,observed,text,reference) VALUES(?,?,?,?,?,?,?,?)',
            ('turn','synthetic','hermes','session','user',now,'synthetic',packed(reference)))
        with patch.object(checks.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(sources.sqlite3, 'connect', return_value=raw), patch.object(sources, 'reference_current', return_value=True):
            with self.assertRaises(checks.LocalCheckTimeout):
                with checks.phase(self.store, 'reflection_ready', 1):
                    sources._context_complete(store, 'synthetic')
                    self.fail('native tail query returned before noticing deadline')
        self.assertLess(calls[0], 1200)
        self.assertGreater(calls[0], 0)

    def test_busy_control_lock_does_not_extend_local_read_budget(self):
        from activity_mvp.feedback.control import gate
        fd = os.open(self.store.root / 'control.lock', os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            started = time.monotonic()
            with self.assertRaises(checks.LocalCheckTimeout):
                with checks.phase(self.store, 'reflection_scan', .04):
                    with gate(self.store.root): self.fail('lock acquired')
            self.assertLess(time.monotonic()-started, 1)
            # The other owner still holds its original lock.
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally: os.close(fd)

    def test_expired_tick_returns_reason_releases_lock_and_does_not_submit(self):
        unknown = self.store.reserve('analysis'); self.store.settle(unknown, 'unknown')
        before = self.store.rows('SELECT * FROM budget')
        with patch.object(service.sources, 'scan', side_effect=checks.LocalCheckTimeout('feedback_scan')), patch.object(model, 'invoke') as invoke:
            result = service.tick(self.store.root)
        self.assertEqual(result, {'status':'deferred', 'reason':'local_check_timeout', 'phase':'feedback_scan'})
        invoke.assert_not_called()
        self.assertEqual(self.store.rows('SELECT * FROM budget'), before)
        fd = os.open(self.store.root / 'controller.lock', os.O_WRONLY)
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally: os.close(fd)
        self.assertIsNone(self.store.get('last_tick'))

    def test_progress_only_exposes_fixed_metadata_and_original_check_time(self):
        self.store.set('local_check_progress', {'phase':'reflection_ready', 'status':'deferred',
            'checked_at':10, 'started_at':1, 'limit_seconds':60, 'body':'private synthetic'})
        value = checks.snapshot(self.store)
        self.assertEqual(value['checked_at'], 10)
        self.assertNotIn('private', packed(value))
        self.store.set('local_check_progress', {'phase':'private synthetic', 'status':'deferred'})
        self.assertEqual(checks.snapshot(self.store), {})


class ResumeTests(unittest.TestCase):
    setUp = fixtures.ConversationAdmissionTests.setUp
    write_source = fixtures.ConversationAdmissionTests.write_source
    write_grant = fixtures.ConversationAdmissionTests.write_grant
    seed = fixtures.ConversationAdmissionTests.seed

    def messages(self, feedback):
        self.seed(feedback)
        self.lines.extend({'timestamp':time.time(), 'type':'response_item', 'payload':{
            'type':'message', 'role':'user', 'content':[{'type':'input_text','text':'你没有核对已有证据，请先核对。synthetic '+str(i)}]}}
            for i in range(8))
        self.write_source()

    def test_feedback_timeout_resumes_before_interrupted_record(self):
        with FeedbackStore(self.root / 'feedback') as feedback:
            self.messages(feedback)
            original = feedback_sources.insert; calls = [0]
            def interrupted(*args, **kwargs):
                calls[0] += 1
                if calls[0] == 4: raise checks.LocalCheckTimeout('feedback_scan')
                return original(*args, **kwargs)
            with patch.object(feedback_sources, 'insert', side_effect=interrupted):
                with self.assertRaises(checks.LocalCheckTimeout): feedback_sources.scan(feedback)
            cursor = json.loads(feedback.rows('SELECT cursor FROM sources')[0]['cursor'])
            self.assertGreater(next(iter(cursor.values()))['offset'], 0)
            feedback_sources.scan(feedback)
            self.assertEqual(feedback.conn.execute('SELECT count(*) FROM messages').fetchone()[0], 9)

    def test_feedback_same_length_tail_rewrite_is_reread(self):
        with FeedbackStore(self.root / 'feedback') as feedback:
            self.messages(feedback)
            self.lines.append({'timestamp': time.time(), 'type':'response_item', 'payload':{
                'type':'message', 'role':'assistant', 'content':[{'type':'output_text','text':'已完成全部文件。'}]}})
            self.write_source()
            feedback_sources.scan(feedback)
            before = self.path.read_bytes(); info = self.path.stat()
            after = before.replace(b'synthetic 7', b'synthetic 8')
            self.assertEqual(before[:256], after[:256]); self.assertEqual(len(before), len(after))
            self.path.write_bytes(after)
            os.utime(self.path, ns=(info.st_atime_ns, info.st_mtime_ns+1000000))
            with patch.object(feedback_sources, 'insert', wraps=feedback_sources.insert) as insert:
                feedback_sources.scan(feedback)
            self.assertEqual(insert.call_args_list[0].args[5], '')
            self.assertTrue(feedback.rows("SELECT id FROM messages WHERE quote LIKE '%synthetic 8%'"))
            self.assertEqual(feedback.conn.execute('SELECT count(*) FROM messages').fetchone()[0], 10)

    def test_reflection_timeout_resumes_without_missing_or_duplicate_turns(self):
        with FeedbackStore(self.root / 'feedback') as feedback, Store(self.root / 'reflection') as store:
            self.messages(feedback); store.set('enabled', True)
            original = sources.admit; calls = [0]
            def interrupted(*args, **kwargs):
                calls[0] += 1
                if calls[0] == 4: raise checks.LocalCheckTimeout('reflection_scan')
                return original(*args, **kwargs)
            with patch.object(sources, 'admit', side_effect=interrupted):
                with self.assertRaises(checks.LocalCheckTimeout): sources.scan(store, feedback)
            cursor = json.loads(store.rows('SELECT cursor FROM sources')[0]['cursor'])
            self.assertGreater(next(iter(cursor['files'].values()))['offset'], 0)
            sources.scan(store, feedback)
            self.assertEqual(store.conn.execute('SELECT count(*) FROM turns').fetchone()[0], 9)
            self.assertEqual(store.rows('SELECT * FROM calls'), [])

    def test_model_preparation_timeout_does_not_reserve_or_advance(self):
        with FeedbackStore(self.root / 'feedback') as feedback, Store(self.root / 'reflection') as store:
            self.seed(feedback, store)
            before = store.rows('SELECT id,processed_seq FROM contexts')
            with patch.object(analysis, 'payload', side_effect=checks.LocalCheckTimeout('reflection_prepare')), patch.object(model, 'invoke') as invoke:
                with self.assertRaises(checks.LocalCheckTimeout): analysis.analyze(store, feedback)
            invoke.assert_not_called()
            self.assertEqual(store.rows('SELECT id,processed_seq FROM contexts'), before)
            self.assertEqual(store.rows('SELECT * FROM calls'), [])
            self.assertEqual(feedback.rows('SELECT * FROM budget'), [])


if __name__ == '__main__':
    unittest.main()
