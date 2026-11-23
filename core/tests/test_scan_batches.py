"""Synthetic bounded backfill: fairness is not source or submission approval."""
import json
import os
import sqlite3
import time
import unittest
from unittest.mock import patch

from activity_mvp import local_checks as checks
from activity_mvp.feedback import sources as feedback_sources, service, model
from activity_mvp.feedback.store import Store as FeedbackStore, packed
from activity_mvp.reflection import sources, service as reflection_service, analysis
from activity_mvp.reflection.store import Store
from tests import test_conversation_scope as fixtures
from tests import test_local_checks as deadline_fixtures


class ScanBatchTests(unittest.TestCase):
    setUp = fixtures.ConversationAdmissionTests.setUp
    write_source = fixtures.ConversationAdmissionTests.write_source
    write_grant = fixtures.ConversationAdmissionTests.write_grant
    seed = fixtures.ConversationAdmissionTests.seed
    messages = deadline_fixtures.ResumeTests.messages

    def test_slow_setup_gets_one_complete_unit_but_never_extends_hard_deadline(self):
        clock = [0.0]
        with FeedbackStore(self.root / 'feedback') as feedback:
            with patch.object(checks.time, 'monotonic', side_effect=lambda: clock[0]):
                with checks.phase(feedback, 'feedback_scan', 60):
                    parent = checks.ScanBatch(20)
                    child = checks.ScanBatch(5, parent=parent)
                    clock[0] = 21
                    self.assertFalse(child.yield_now())
                    child.complete()
                    self.assertTrue(child.yield_now())
                    self.assertTrue(parent.yield_now())
                with self.assertRaises(checks.LocalCheckTimeout):
                    with checks.phase(feedback, 'feedback_scan', 60):
                        child = checks.ScanBatch(5)
                        clock[0] += 61
                        child.yield_now()

    def test_rotation_never_adds_a_removed_or_unknown_source(self):
        rows = [{'id':'a'}, {'id':'c'}]
        self.assertEqual(checks.scan_order(rows, 'c'), [rows[1], rows[0]])
        self.assertEqual(checks.scan_order(rows, 'removed'), rows)
        self.assertEqual(checks.scan_order([], 'removed'), [])

    def fake_backfill(self, clock, visited):
        def read(store, source, *, batch=None, **kwargs):
            visited.append(source['id'])
            count = 0
            for _ in range(100):
                if batch is not None and batch.yield_now():break
                clock[0] += 2
                checks.checkpoint()
                count += 1
                if batch is not None:batch.complete()
            return {}, 0, {'status':'backfill', 'completed_records':count}
        return read

    def test_sources_rotate_and_soft_backfill_reaches_existing_analysis_path(self):
        clock = [0.0]; visited = []
        with FeedbackStore(self.root / 'feedback') as feedback:
            feedback.set('config', {'enabled':True, 'since':time.time()-86400, 'journal':str(self.root/'journal')})
            for i in range(6):
                feedback.conn.execute('INSERT INTO sources(id,platform,path) VALUES(?,?,?)',
                    ('source-'+str(i), 'codex', str(self.native)))
            unknown = feedback.reserve('analysis'); feedback.settle(unknown, 'unknown')
            budget = feedback.rows('SELECT * FROM budget')
            with patch.object(checks.time, 'monotonic', side_effect=lambda:clock[0]), \
                    patch.object(feedback_sources, 'scan_jsonl', side_effect=self.fake_backfill(clock, visited)), \
                    patch.object(reflection_service, 'scan', return_value={'status':'ok'}), \
                    patch.object(reflection_service, 'ready', return_value=False), \
                    patch.object(reflection_service, 'notify', return_value={}), \
                    patch.object(service, 'notification_summary', return_value={'pending':[]}), \
                    patch.object(model, 'analyze', return_value={'status':'idle','processed':0}) as analyze:
                first = service.tick(feedback.root, repair=False)
                self.assertEqual(first['status'], 'ok')
                self.assertEqual(visited, ['source-0','source-1','source-2','source-3'])
                self.assertEqual(feedback.get('source_scan_next'), 'source-4')
                self.assertLess(clock[0], 60)
                service.tick(feedback.root, repair=False)
                self.assertEqual(visited[4:6], ['source-4','source-5'])
                self.assertEqual(analyze.call_count, 2)
            self.assertEqual(feedback.rows('SELECT * FROM budget'), budget)

    def test_hard_failure_rotates_source_without_allowing_analysis(self):
        with FeedbackStore(self.root / 'feedback') as feedback:
            feedback.set('config', {'enabled':True, 'since':time.time()-86400})
            for name in ('a','b'):
                feedback.conn.execute('INSERT INTO sources(id,platform,path) VALUES(?,?,?)',
                    (name,'codex',str(self.native)))
            with patch.object(feedback_sources, 'scan_jsonl', side_effect=checks.LocalCheckTimeout('feedback_scan')), \
                    patch.object(model, 'invoke') as invoke:
                result = service.tick(feedback.root, repair=False)
            self.assertEqual(result['reason'], 'local_check_timeout')
            self.assertEqual(feedback.get('source_scan_next'), 'b')
            invoke.assert_not_called()
            self.assertEqual(feedback.rows('SELECT * FROM budget'), [])

    def test_legacy_cursor_rechecks_once_then_resumes_to_stable_completion(self):
        clock = [0.0]
        with FeedbackStore(self.root / 'feedback') as feedback:
            self.messages(feedback)
            feedback_sources.scan(feedback)
            cursor = json.loads(feedback.rows('SELECT cursor FROM sources')[0]['cursor'])
            for progress in cursor.values():
                progress.pop('mtime_ns'); progress.pop('scanned_at', None)
            feedback.conn.execute('UPDATE sources SET cursor=?', (packed(cursor),))
            original = feedback_sources.insert
            def slow(*args, **kwargs):
                value = original(*args, **kwargs); clock[0] += 2; return value
            offsets = []
            with patch.object(checks.time, 'monotonic', side_effect=lambda:clock[0]), \
                    patch.object(feedback_sources, 'insert', side_effect=slow):
                for _ in range(4):
                    feedback_sources.scan(feedback)
                    cursor = json.loads(feedback.rows('SELECT cursor FROM sources')[0]['cursor'])
                    offsets.append(next(iter(cursor.values()))['offset'])
            self.assertGreater(offsets[0], 0)
            self.assertLess(offsets[0], offsets[1])
            self.assertLess(offsets[1], offsets[2])
            self.assertEqual(offsets[2], offsets[3])
            self.assertEqual(offsets[-1], self.path.stat().st_size)
            self.assertEqual(feedback.rows('SELECT status FROM sources')[0]['status'], 'current')
            self.assertEqual(feedback.conn.execute('SELECT count(*) FROM messages').fetchone()[0], 9)

    def test_growing_first_file_does_not_starve_second_file(self):
        clock = [0.0]
        with FeedbackStore(self.root / 'feedback') as feedback:
            self.messages(feedback)
            other = self.path.with_name('rollout-12345678-0000-4000-8000-000000000002.jsonl')
            second = json.loads(packed(self.lines))
            second[0]['payload']['id'] = '12345678-0000-4000-8000-000000000002'
            for row in second[1:]:row['payload']['content'][0]['text'] += ' second file'
            other.write_text(''.join(packed(row)+'\n' for row in second));other.chmod(0o600)
            os.utime(other, (time.time()-10,)*2)
            original = feedback_sources.insert
            def slow(*args, **kwargs):
                result = original(*args, **kwargs);clock[0] += 2;return result
            with patch.object(checks.time, 'monotonic', side_effect=lambda:clock[0]), \
                    patch.object(feedback_sources, 'insert', side_effect=slow):
                feedback_sources.scan(feedback)
                self.lines.append(self.lines[-1]);self.write_source()
                feedback_sources.scan(feedback)
            cursor = json.loads(feedback.rows('SELECT cursor FROM sources')[0]['cursor'])
            self.assertGreater(cursor[str(other.relative_to(self.native))]['offset'], 0)
            self.assertTrue(feedback.rows('SELECT id FROM messages WHERE session=?', (second[0]['payload']['id'],)))

    def test_non_message_and_invalid_records_also_yield_at_record_boundaries(self):
        with FeedbackStore(self.root / 'feedback') as feedback:
            self.seed(feedback)
            self.path.write_text(packed(self.lines[0])+'\n'+'not json\n'+packed({'type':'event_msg'})+'\n'+packed(self.lines[1])+'\n')
            source = feedback.rows('SELECT * FROM sources')[0]
            clock = [0.0]; original = feedback_sources.read_active
            def slow(*args, **kwargs):
                result = original(*args, **kwargs);clock[0] += 2;return result
            with patch.object(checks.time, 'monotonic', side_effect=lambda:clock[0]), \
                    patch.object(feedback_sources, 'read_active', side_effect=slow):
                cursor, count, info = feedback_sources.scan_jsonl(feedback, source, batch=checks.ScanBatch(1))
            progress = next(iter(cursor.values()))
            self.assertEqual(progress['offset'], len((packed(self.lines[0])+'\n').encode()))
            self.assertEqual(count, 0);self.assertEqual(info['status'], 'backfill')
            self.assertLess(progress['offset'], self.path.stat().st_size)
            feedback.conn.execute('UPDATE sources SET cursor=?', (packed(cursor),))
            feedback_sources.scan(feedback)
            self.assertEqual(feedback.conn.execute('SELECT count(*) FROM messages').fetchone()[0], 1)

    def test_unfinished_oversized_line_is_never_marked_current(self):
        with FeedbackStore(self.root / 'feedback') as feedback, Store(self.root / 'reflection') as store:
            self.seed(feedback);store.set('enabled', True)
            start = self.path.stat().st_size
            with self.path.open('ab') as stream:stream.write(b'x'*(feedback_sources.MAX_LINE+1))
            feedback_sources.scan(feedback);sources.scan(store, feedback)
            f = json.loads(feedback.rows('SELECT cursor FROM sources')[0]['cursor'])
            r = json.loads(store.rows('SELECT cursor FROM sources')[0]['cursor'])['files']
            self.assertEqual(next(iter(f.values()))['offset'], start)
            self.assertEqual(next(iter(r.values()))['offset'], start)
            self.assertFalse(sources.context_complete(store, store.rows('SELECT id FROM contexts')[0]['id']))
            with self.path.open('ab') as stream:stream.write(b'\n')
            feedback_sources.scan(feedback);sources.scan(store, feedback)
            f = json.loads(feedback.rows('SELECT cursor FROM sources')[0]['cursor'])
            self.assertEqual(next(iter(f.values()))['offset'], self.path.stat().st_size)

    def test_reflection_yield_preserves_incomplete_guard_and_withdraws_stale_insight(self):
        clock = [0.0]
        with FeedbackStore(self.root / 'feedback') as feedback, Store(self.root / 'reflection') as store:
            self.messages(feedback);store.set('enabled', True)
            sources.scan(store, feedback)
            turn = store.rows('SELECT * FROM turns ORDER BY seq LIMIT 1')[0]
            item = {'id':'stale','state':'active','judgment':'preliminary',
                **{key:'synthetic' for key in analysis.FIELDS}, 'evidence':packed([{
                    'id':turn['id'],'observed':turn['observed'],'role':turn['role'],'quote':turn['text'],
                    'context_version':sources.context_version(store,turn['context_id'])}]),
                'fingerprint':'synthetic','created':time.time(),'updated':time.time()}
            store.conn.execute('INSERT INTO insights('+','.join(item)+') VALUES('+','.join('?' for _ in item)+')', tuple(item.values()))
            self.lines.extend(json.loads(packed(self.lines[-8:])))
            self.write_source()
            original = sources.admit
            def slow(*args, **kwargs):
                result = original(*args, **kwargs);clock[0] += 2;return result
            with patch.object(checks.time, 'monotonic', side_effect=lambda:clock[0]), patch.object(sources, 'admit', side_effect=slow):
                result = sources.scan(store, feedback)
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(store.rows('SELECT status FROM sources')[0]['status'], 'backfill')
            context = store.rows('SELECT id FROM contexts')[0]['id']
            self.assertFalse(sources.context_complete(store, context))
            data, *_ = analysis.payload(store)
            self.assertTrue(data['current_contexts'])
            self.assertTrue(all(not context['complete'] for context in data['current_contexts']))
            self.assertEqual(store.conn.execute("SELECT state FROM insights WHERE id='stale'").fetchone()[0], 'withdrawn')
            self.assertEqual(store.rows('SELECT * FROM calls'), [])

    def test_hermes_yield_keeps_per_session_positions_and_rotates(self):
        path = self.root/'hermes.sqlite'
        sessions = [self.session, '12345678-0000-4000-8000-000000000002']
        with sqlite3.connect(path) as db:
            db.executescript('CREATE TABLE sessions(id TEXT,source TEXT,user_id TEXT,started_at REAL,parent_session_id TEXT,cwd TEXT);CREATE TABLE messages(id INTEGER,session_id TEXT,role TEXT,content TEXT,timestamp REAL);')
            for index, session in enumerate(sessions):
                db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?)', (session,'cli','',time.time(),None,str(self.root)))
                for n in range(1,7):
                    db.execute('INSERT INTO messages VALUES(?,?,?,?,?)', (index*100+n,session,'user','你没有核对证据。synthetic '+str(n),time.time()))
        path.chmod(0o600)
        self.grant['sources']=[{'id':'hermes','platform':'hermes','path':str(path)}];self.write_grant()
        with FeedbackStore(self.root / 'feedback') as feedback, Store(self.root / 'reflection') as store:
            feedback.set('config', {'enabled':True,'since':time.time()-86400});store.set('enabled', True)
            feedback.conn.execute("INSERT INTO sources(id,platform,path) VALUES('hermes','hermes',?)", (str(path),))
            for scan, module, function, target in ((lambda:feedback_sources.scan(feedback),feedback_sources,'insert',feedback),
                    (lambda:sources.scan(store,feedback),sources,'admit',store)):
                clock = [0.0]; original = getattr(module,function)
                def slow(*args, **kwargs):
                    result = original(*args, **kwargs);clock[0] += 2;return result
                with patch.object(checks.time, 'monotonic', side_effect=lambda:clock[0]), patch.object(module,function,side_effect=slow):
                    scan()
                    first = json.loads(target.rows('SELECT cursor FROM sources')[0]['cursor'])['sessions']
                    self.assertEqual(first, {sessions[0]:3})
                    scan()
                    second = json.loads(target.rows('SELECT cursor FROM sources')[0]['cursor'])['sessions']
                    self.assertEqual(second, {sessions[0]:3, sessions[1]:103})
                    for _ in range(3):scan()
                complete = json.loads(target.rows('SELECT cursor FROM sources')[0]['cursor'])['sessions']
                self.assertEqual(complete, {sessions[0]:6, sessions[1]:106})


if __name__ == '__main__':
    unittest.main()
