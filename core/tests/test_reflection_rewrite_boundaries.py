"""Independent synthetic checks for rewrite selection and context boundaries."""
import os
import time
import unittest

from activity_mvp import conversation_scope as scope
from activity_mvp.feedback.store import Store as FeedbackStore, packed
from activity_mvp.reflection import sources
from activity_mvp.reflection.store import Store
from tests import test_conversation_scope as fixtures


class RewriteBoundaryTests(unittest.TestCase):
    setUp = fixtures.ConversationAdmissionTests.setUp
    write_source = fixtures.ConversationAdmissionTests.write_source
    write_grant = fixtures.ConversationAdmissionTests.write_grant
    seed = fixtures.ConversationAdmissionTests.seed

    def test_retired_denied_source_still_blocks_matching_current_candidate(self):
        with FeedbackStore(self.root / 'feedback') as feedback, Store(self.root / 'reflection') as store:
            self.seed(feedback, store)
            context = store.rows('SELECT id FROM main.contexts')[0]['id']
            store.conn.execute('CREATE TABLE IF NOT EXISTS retired_turns AS SELECT * FROM turns WHERE 0')
            store.conn.execute('CREATE TABLE IF NOT EXISTS retired_contexts AS SELECT * FROM contexts WHERE 0')
            store.conn.execute(
                'INSERT INTO retired_turns(id,context_id,source_id,session,role,observed,text,reference) '
                'VALUES(?,?,?,?,?,?,?,?)',
                ('retired-denied', context, 'codex', 'denied-session', 'user', time.time(),
                 'synthetic retired material', packed({'path': str(self.root / 'company/denied.jsonl'),
                                                       'parser_version': 2})))
            before = list(store.conn.iterdump())
            projected = scope.filtered(store, 'reflection')
            self.assertEqual(sources._rewritten_turns(projected, 'codex', self.path), [])
            self.assertEqual(list(store.conn.iterdump()), before)

    def test_same_file_rewrite_preserves_blocked_context_and_resets_all_visible_targets(self):
        with FeedbackStore(self.root / 'feedback') as feedback, Store(self.root / 'reflection') as store:
            self.seed(feedback, store)
            target = store.rows('SELECT * FROM main.turns')[0]
            for ident, context in [('second-target', 'second-visible-context'),
                                   ('blocked-target', 'blocked-context')]:
                store.conn.execute(
                    'INSERT INTO contexts(id,source_id,session,updated) VALUES(?,?,?,?)',
                    (context, 'codex', self.session, time.time()))
                store.conn.execute(
                    'INSERT INTO turns(id,context_id,source_id,session,role,observed,text,reference) '
                    'VALUES(?,?,?,?,?,?,?,?)',
                    (ident, context, 'codex', self.session, target['role'], target['observed'],
                     target['text'], target['reference']))
            store.conn.execute(
                'INSERT INTO turns(id,context_id,source_id,session,role,observed,text,reference) '
                'VALUES(?,?,?,?,?,?,?,?)',
                ('denied-witness', 'blocked-context', 'codex', 'denied-session', 'user', time.time(),
                 'synthetic denied material', packed({'path': str(self.root / 'company/denied.jsonl'),
                                                      'parser_version': 2})))
            store.conn.execute('UPDATE contexts SET processed_seq=99,summary=?', (packed({'prior': True}),))
            blocked_before = store.rows("SELECT * FROM main.turns WHERE context_id='blocked-context' ORDER BY id")
            selected = sources._rewritten_turns(scope.filtered(store, 'reflection'), 'codex', self.path)
            self.assertEqual({row['id'] for row in selected}, {target['id'], 'second-target'})

            previous = self.path.stat()
            self.lines[-1]['payload']['content'][0]['text'] = '这是新的合成改写内容。'
            self.write_source()
            os.utime(self.path, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1000000))
            sources.scan(store, feedback)

            current = store.rows('SELECT text FROM main.turns WHERE context_id=?', (target['context_id'],))
            self.assertEqual(current, [{'text': '这是新的合成改写内容。'}])
            self.assertEqual(store.rows("SELECT id FROM main.turns WHERE context_id='second-visible-context'"), [])
            for context in (target['context_id'], 'second-visible-context'):
                self.assertEqual(store.rows('SELECT processed_seq,summary FROM main.contexts WHERE id=?', (context,)),
                                 [{'processed_seq': 0, 'summary': '{}'}])
            self.assertEqual(store.rows("SELECT * FROM main.turns WHERE context_id='blocked-context' ORDER BY id"),
                             blocked_before)
            self.assertEqual(store.rows("SELECT processed_seq,summary FROM main.contexts WHERE id='blocked-context'"),
                             [{'processed_seq': 99, 'summary': packed({'prior': True})}])
            self.assertEqual(store.rows('SELECT * FROM calls'), [])


if __name__ == '__main__':
    unittest.main()
