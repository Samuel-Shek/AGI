"""Independent regressions for late-file/session progress and rejected metadata.

All source bodies, grants and SQLite writes belong to the test's temporary root.
Delays advance a synthetic monotonic clock; no model or production store is used.
"""
import hashlib
import json
import os
import sqlite3
import time
import unittest
from unittest.mock import patch

from activity_mvp import local_checks as checks
from activity_mvp.feedback import sources as feedback_sources
from activity_mvp.feedback.store import Store as FeedbackStore, packed
from activity_mvp.reflection import sources as reflection_sources
from activity_mvp.reflection.store import Store as ReflectionStore
from tests import test_conversation_scope as fixtures


class LaterInputFairnessTests(unittest.TestCase):
    setUp = fixtures.ConversationAdmissionTests.setUp
    write_source = fixtures.ConversationAdmissionTests.write_source
    write_grant = fixtures.ConversationAdmissionTests.write_grant
    seed = fixtures.ConversationAdmissionTests.seed

    def scan(self, kind, feedback, reflection):
        with checks.phase(feedback, kind+'_scan', 60):
            if kind == 'feedback': return feedback_sources.scan(feedback)
            return reflection_sources.scan(reflection, feedback)

    def selected(self, kind, feedback, reflection):
        if kind == 'feedback': return feedback, feedback_sources, 'messages'
        return reflection, reflection_sources, 'turns'

    def files(self, kind, target):
        cursor = json.loads(target.rows('SELECT cursor FROM sources')[0]['cursor'])
        return cursor if kind == 'feedback' else cursor['files']

    def late_file(self):
        identity = '12345678-0000-4000-8000-000000000002'
        path = self.path.with_name('rollout-'+identity+'.jsonl')
        lines = json.loads(packed(self.lines))
        lines[0]['payload']['id'] = identity
        lines[-1]['payload']['content'][0]['text'] += ' later synthetic'
        path.write_text(''.join(packed(row)+'\n' for row in lines));path.chmod(0o600)
        os.utime(path, (time.time()-10,)*2)
        return identity, path

    def rejected_files(self, kind):
        with FeedbackStore(self.root/'feedback') as feedback, ReflectionStore(self.root/'reflection') as reflection:
            self.seed(feedback);reflection.set('enabled', True)
            initial = {str(self.path.relative_to(self.native)): {'scanned_at':1}}
            feedback.conn.execute('UPDATE sources SET cursor=?', (packed(initial),))
            reflection.conn.execute('INSERT INTO sources(id,platform,path,cursor) VALUES(?,?,?,?)',
                ('codex','codex',str(self.native),packed({'files':initial})))
            denied = []
            for index in range(2):
                identity = f'{index+2:08d}-4455-6677-8899-aabbccddeeff'
                path = self.path.with_name('rollout-'+identity+'.jsonl')
                path.write_text(packed({'type':'session_meta','payload':{
                    'id':identity,'source':'vscode','cwd':str(self.root/'company')}})+'\n')
                path.chmod(0o600);denied.append(path)
            target, module, table = self.selected(kind, feedback, reflection)
            original = module.source_metadata_admitted;clock = [0.0]
            def slow_denial(*args, **kwargs):
                value = original(*args, **kwargs)
                if not value:clock[0] += 3
                return value
            with patch.object(checks.time, 'monotonic', side_effect=lambda:clock[0]), \
                    patch.object(module, 'source_metadata_admitted', side_effect=slow_denial):
                self.scan(kind, feedback, reflection)
                self.assertEqual(target.conn.execute('SELECT count(*) FROM '+table).fetchone()[0], 0)
                self.scan(kind, feedback, reflection)
                self.assertEqual(target.conn.execute('SELECT count(*) FROM '+table).fetchone()[0], 1)
            cursor = self.files(kind, target)
            for path in denied:self.assertNotIn(str(path.relative_to(self.native)), cursor)
            seen = target.get('source_scan_seen:codex')
            self.assertEqual(set(seen), {hashlib.sha256(str(path).encode()).hexdigest() for path in denied})
            for path in denied:self.assertNotIn(str(path), packed(seen))
            self.assertTrue(all(isinstance(value, (int,float)) for value in seen.values()))

    def test_feedback_denied_metadata_rotates_without_retaining_its_path(self):
        self.rejected_files('feedback')

    def test_reflection_denied_metadata_rotates_without_retaining_its_path(self):
        self.rejected_files('reflection')

    def later_file_setup(self, kind):
        with FeedbackStore(self.root/'feedback') as feedback, ReflectionStore(self.root/'reflection') as reflection:
            self.seed(feedback);reflection.set('enabled', True)
            self.scan(kind, feedback, reflection)
            target, module, table = self.selected(kind, feedback, reflection)
            identity, later = self.late_file()
            cursor = json.loads(target.rows('SELECT cursor FROM sources')[0]['cursor'])
            files = cursor if kind == 'feedback' else cursor['files']
            files[str(self.path.relative_to(self.native))]['scanned_at'] = 0
            files[str(later.relative_to(self.native))] = {'scanned_at':1}
            target.conn.execute('UPDATE sources SET cursor=?', (packed(cursor),))
            clock = [0.0];delayed = [False];original = module.read_active
            checked_paths = module.checked_paths
            def slow_setup(store, active, operation):
                value = original(store, active, operation)
                cells = [cell.cell_contents for cell in (operation.__closure__ or ())]
                if not delayed[0] and any(getattr(cell, 'name', None) == str(later) for cell in cells):
                    clock[0] += 6;delayed[0] = True
                return value
            with patch.object(checks.time, 'monotonic', side_effect=lambda:clock[0]), \
                    patch.object(module, 'checked_paths', side_effect=lambda paths:sorted(checked_paths(paths), key=lambda p:p.name)), \
                    patch.object(module, 'read_active', side_effect=slow_setup):
                for _ in range(4):
                    delayed[0] = False
                    self.scan(kind, feedback, reflection)
            self.assertEqual(target.conn.execute('SELECT count(*) FROM '+table+' WHERE session=?', (identity,)).fetchone()[0], 1)
            self.assertEqual(self.files(kind, target)[str(later.relative_to(self.native))]['offset'], later.stat().st_size)

    def test_feedback_later_slow_setup_cannot_be_repeatedly_deprioritized(self):
        self.later_file_setup('feedback')

    def test_reflection_later_slow_setup_cannot_be_repeatedly_deprioritized(self):
        self.later_file_setup('reflection')

    def later_session_setup(self, kind):
        identities = [self.session, '12345678-0000-4000-8000-000000000002']
        path = self.root/'hermes.sqlite'
        with sqlite3.connect(path) as db:
            db.executescript('CREATE TABLE sessions(id TEXT,source TEXT,user_id TEXT,started_at REAL,parent_session_id TEXT,cwd TEXT);CREATE TABLE messages(id INTEGER,session_id TEXT,role TEXT,content TEXT,timestamp REAL);')
            for index, identity in enumerate(identities):
                db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?)', (identity,'cli','',time.time(),None,str(self.root)))
                db.execute('INSERT INTO messages VALUES(?,?,?,?,?)', (index*100+1,identity,'user','你没有核对证据。synthetic '+str(index),time.time()))
        path.chmod(0o600)
        self.grant['sources'] = [{'id':'hermes','platform':'hermes','path':str(path)}];self.write_grant()
        with FeedbackStore(self.root/'feedback') as feedback, ReflectionStore(self.root/'reflection') as reflection:
            feedback.set('config', {'enabled':True,'since':time.time()-86400})
            reflection.set('enabled', True);reflection.set('source_parser_version', 2)
            cursor = packed({'sessions':{identities[0]:1},'seen':{}})
            for target in (feedback, reflection):
                target.conn.execute('INSERT INTO sources(id,platform,path,cursor) VALUES(?,?,?,?)', ('hermes','hermes',str(path),cursor))
            target, module, table = self.selected(kind, feedback, reflection)
            clock = [0.0];delayed = [False];original = module.read_active
            def slow_setup(store, active, operation):
                value = original(store, active, operation)
                cells = [cell.cell_contents for cell in (operation.__closure__ or ())]
                if not delayed[0] and any(isinstance(cell, str) and cell == identities[1] for cell in cells):
                    clock[0] += 6;delayed[0] = True
                return value
            with patch.object(checks.time, 'monotonic', side_effect=lambda:clock[0]), \
                    patch.object(module, 'read_active', side_effect=slow_setup):
                self.scan(kind, feedback, reflection)
                first = json.loads(target.rows('SELECT cursor FROM sources')[0]['cursor'])
                self.assertEqual(first['sessions'][identities[1]], 0)
                delayed[0] = False
                self.scan(kind, feedback, reflection)
            cursor = json.loads(target.rows('SELECT cursor FROM sources')[0]['cursor'])
            self.assertEqual(cursor['sessions'], {identities[0]:1, identities[1]:101})
            self.assertEqual(target.conn.execute('SELECT count(*) FROM '+table+' WHERE session=?', (identities[1],)).fetchone()[0], 1)

    def test_feedback_later_slow_session_keeps_its_priority(self):
        self.later_session_setup('feedback')

    def test_reflection_later_slow_session_keeps_its_priority(self):
        self.later_session_setup('reflection')

    def test_reflection_growing_first_file_does_not_starve_second_file(self):
        with FeedbackStore(self.root/'feedback') as feedback, ReflectionStore(self.root/'reflection') as reflection:
            self.seed(feedback);reflection.set('enabled', True)
            self.lines.extend({'timestamp':time.time(),'type':'response_item','payload':{
                'type':'message','role':'user','content':[{'type':'input_text','text':'你没有核对已有证据。synthetic '+str(i)}]}}
                for i in range(8));self.write_source()
            identity, later = self.late_file()
            clock = [0.0];original = reflection_sources.admit
            checked_paths = reflection_sources.checked_paths
            def slow(*args, **kwargs):
                value = original(*args, **kwargs);clock[0] += 2;return value
            with patch.object(checks.time, 'monotonic', side_effect=lambda:clock[0]), \
                    patch.object(reflection_sources, 'checked_paths', side_effect=lambda paths:sorted(checked_paths(paths), key=lambda p:p.name)), \
                    patch.object(reflection_sources, 'admit', side_effect=slow):
                self.scan('reflection', feedback, reflection)
                self.lines.append(self.lines[-1]);self.write_source()
                self.scan('reflection', feedback, reflection)
            self.assertGreater(self.files('reflection', reflection)[str(later.relative_to(self.native))]['offset'], 0)
            self.assertTrue(reflection.rows('SELECT id FROM turns WHERE session=?', (identity,)))


if __name__ == '__main__':
    unittest.main()
