"""A rewritten input must not re-authorize unrelated source history."""
import json
import os
import time
import unittest
from unittest.mock import patch

from activity_mvp import conversation_scope as scope
from activity_mvp.feedback.store import Store as FeedbackStore, packed
from activity_mvp.reflection import sources
from activity_mvp.reflection.store import Store
from tests import test_conversation_scope as fixtures


class RewriteScopeTests(unittest.TestCase):
    setUp = fixtures.ConversationAdmissionTests.setUp
    write_source = fixtures.ConversationAdmissionTests.write_source
    write_grant = fixtures.ConversationAdmissionTests.write_grant
    seed = fixtures.ConversationAdmissionTests.seed

    def unrelated(self, store, count=5000):
        store.conn.execute('BEGIN')
        try:
            for n in range(count):
                store.conn.execute('INSERT INTO turns(id,context_id,source_id,session,role,observed,text,reference) VALUES(?,?,?,?,?,?,?,?)',
                    ('unrelated-'+str(n), 'unrelated-context', 'codex', 'unrelated-session',
                     'user', time.time(), 'synthetic unrelated', packed({'path':str(self.root/'unrelated.jsonl'),'parser_version':2})))
            store.conn.execute('INSERT INTO contexts(id,source_id,session,updated) VALUES(?,?,?,?)',
                ('unrelated-context','codex','unrelated-session',time.time()))
            store.conn.execute('COMMIT')
        except BaseException:
            store.conn.execute('ROLLBACK');raise

    def test_rewrite_checks_only_selected_file_without_unrelated_admission(self):
        with FeedbackStore(self.root/'feedback') as feedback, Store(self.root/'reflection') as store:
            self.seed(feedback,store);self.unrelated(store)
            projected=scope.filtered(store,'reflection');original=scope.observation_allowed
            def checked(session,*args):
                self.assertNotEqual(session,'unrelated-session')
                return original(session,*args)
            with patch.object(scope,'observation_allowed',side_effect=checked):
                rows=sources._rewritten_turns(projected,'codex',self.path)
            self.assertEqual(len(rows),1)
            self.assertEqual(json.loads(rows[0]['reference'])['path'],str(self.path))

    def test_empty_file_does_not_expand_unrelated_history(self):
        with FeedbackStore(self.root/'feedback') as feedback, Store(self.root/'reflection') as store:
            self.seed(feedback,store);self.unrelated(store)
            projected=scope.filtered(store,'reflection')
            with patch.object(scope,'observation_allowed',side_effect=AssertionError('unrelated row inspected')):
                self.assertEqual(sources._rewritten_turns(projected,'codex',self.root/'empty.jsonl'),[])

    def test_matching_raw_id_does_not_bypass_revoked_native_grant(self):
        with FeedbackStore(self.root/'feedback') as feedback, Store(self.root/'reflection') as store:
            self.seed(feedback,store);projected=scope.filtered(store,'reflection')
            self.grant['enabled']=False;self.grant['revision']+=1;self.write_grant()
            self.assertEqual(sources._rewritten_turns(projected,'codex',self.path),[])
            self.assertEqual(store.conn.execute('SELECT COUNT(*) FROM main.turns').fetchone()[0],1)

    def test_target_still_requires_every_context_source(self):
        with FeedbackStore(self.root/'feedback') as feedback, Store(self.root/'reflection') as store:
            self.seed(feedback,store);context=store.rows('SELECT id FROM contexts')[0]['id']
            store.conn.execute('INSERT INTO turns(id,context_id,source_id,session,role,observed,text,reference) VALUES(?,?,?,?,?,?,?,?)',
                ('mixed-row',context,'codex','denied-session','user',time.time(),'synthetic blocked',
                 packed({'path':str(self.root/'company/unknown.jsonl'),'parser_version':2})))
            self.assertEqual(sources._rewritten_turns(scope.filtered(store,'reflection'),'codex',self.path),[])
            self.assertEqual(store.conn.execute('SELECT COUNT(*) FROM main.turns').fetchone()[0],2)

    def test_source_id_and_malformed_reference_cannot_expand_selection(self):
        with FeedbackStore(self.root/'feedback') as feedback, Store(self.root/'reflection') as store:
            self.seed(feedback,store)
            store.conn.execute("INSERT INTO turns(id,context_id,source_id,session,role,observed,text,reference) VALUES('malformed','elsewhere','codex','elsewhere','user',?,'synthetic','{')",(time.time(),))
            projected=scope.filtered(store,'reflection')
            self.assertEqual(sources._rewritten_turns(projected,'other-source',self.path),[])
            self.assertEqual(len(sources._rewritten_turns(projected,'codex',self.path)),1)

    def test_real_rewrite_rebuilds_only_target_and_resets_its_summary(self):
        with FeedbackStore(self.root/'feedback') as feedback, Store(self.root/'reflection') as store:
            self.seed(feedback,store);self.unrelated(store,2)
            target=store.rows("SELECT id FROM contexts WHERE id!='unrelated-context'")[0]['id']
            store.conn.execute("UPDATE contexts SET processed_seq=7,summary='{\"old\":true}'")
            prior=self.path.stat()
            self.lines[-1]['payload']['content'][0]['text']='这是改写后的合成验收文字。'
            self.write_source();os.utime(self.path,ns=(prior.st_atime_ns,prior.st_mtime_ns+1000000))
            sources.scan(store,feedback)
            rows=store.rows('SELECT text FROM main.turns WHERE context_id=?',(target,))
            self.assertEqual([r['text'] for r in rows],['这是改写后的合成验收文字。'])
            self.assertEqual(store.rows('SELECT processed_seq,summary FROM main.contexts WHERE id=?',(target,))[0],{'processed_seq':0,'summary':'{}'})
            self.assertEqual(store.conn.execute("SELECT count(*) FROM main.turns WHERE context_id='unrelated-context'").fetchone()[0],2)
            self.assertEqual(store.rows("SELECT processed_seq,summary FROM main.contexts WHERE id='unrelated-context'")[0],{'processed_seq':7,'summary':'{"old":true}'})
            self.assertEqual(store.rows('SELECT * FROM calls'),[])


if __name__=='__main__':unittest.main()
