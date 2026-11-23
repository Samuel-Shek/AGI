"""Synthetic shared roots: exact personal provenance and useful batch selection."""
import json
from pathlib import Path
import sqlite3
import shutil
import unittest
from unittest.mock import patch

from activity_mvp.feedback.store import Store as Feedback, packed
from activity_mvp.reflection import analysis, sources
from activity_mvp.reflection.store import Store
from activity_mvp.presentation_scope import POLICY_FILE
from tests import test_reflection as fixture
from tests import test_retired_source_maintenance as retired


class ControlMaterialTests(unittest.TestCase):
    setUp = fixture.ReflectionTests.setUp
    tearDown = fixture.ReflectionTests.tearDown
    add = fixture.ReflectionTests.add
    scan = fixture.ReflectionTests.scan

    def old_cache(self, text):
        self.add('user',text);self.scan()
        rows=self.store.rows('SELECT * FROM turns ORDER BY seq')
        # Parser version/IDs/raw source versions and progress are retained.
        for row in rows:
            ref=json.loads(row['reference']);ref['material_ranges']=[]
            self.store.conn.execute('UPDATE turns SET reference=? WHERE id=?',(packed(ref),row['id']))
        return self.store.rows('SELECT * FROM turns ORDER BY seq')

    def test_partial_batch_does_not_validate_unread_conversation_tail(self):
        for i in range(16):
            self.add('user', str(i) + 'x' * 12000)
        self.scan()
        before = self.store.rows('SELECT processed_seq FROM contexts')
        with patch.object(analysis, 'context_complete', side_effect=AssertionError('unread tail')):
            data, known, through = analysis.payload(self.store, self.now)
        self.assertTrue(through)
        self.assertTrue(known)
        self.assertFalse(data['current_contexts'][0]['complete'])
        self.assertLess(len(known), len(self.store.rows('SELECT id FROM turns')))
        self.assertEqual(self.store.rows('SELECT processed_seq FROM contexts'), before)

    def test_final_batch_still_requires_full_source_validation(self):
        self.add('user', 'What evidence would help?')
        self.scan()
        with patch.object(analysis, 'context_complete', return_value=False) as complete:
            data, known, through = analysis.payload(self.store, self.now)
        complete.assert_called_once()
        self.assertTrue(known)
        self.assertTrue(through)
        self.assertFalse(data['current_contexts'][0]['complete'])

    def test_context_completion_reuses_one_full_reader_inside_original_gate(self):
        self.add('user', 'Check the current complete source.'); self.scan()
        context = self.store.rows('SELECT id FROM contexts')[0]['id']
        before = self.store.rows('SELECT processed_seq FROM contexts')
        with patch.object(sources, 'FeedbackStore', wraps=Feedback) as opened:
            with sources.reference_reader(self.store):
                opened.assert_not_called()
                for _ in range(3):
                    self.assertTrue(sources.context_complete(self.store, context))
                full = [c for c in opened.call_args_list if not c.kwargs.get('controls_only')]
                self.assertEqual(len(full), 1)
        self.assertEqual(self.store.rows('SELECT processed_seq FROM contexts'), before)
        self.assertEqual(self.store.rows('SELECT id FROM calls'), [])

    def test_paused_context_completion_never_initializes_full_projection(self):
        self.add('user', 'A pause must stop the next read.'); self.scan()
        context = self.store.rows('SELECT id FROM contexts')[0]['id']
        self.feedback.set('paused', True)
        def controls_only(*args, **kwargs):
            if not kwargs.get('controls_only'):
                raise AssertionError('full projection opened before pause gate')
            return Feedback(*args, **kwargs)
        with patch.object(sources, 'FeedbackStore', side_effect=controls_only) as opened:
            self.assertFalse(sources.context_complete(self.store, context))
            self.assertTrue(all(c.kwargs.get('controls_only') for c in opened.call_args_list))

    def test_completion_policy_failure_poison_survives_restoration(self):
        self.add('user', 'Retain policy checks.'); self.scan()
        context = self.store.rows('SELECT id FROM contexts')[0]['id']
        with sources.reference_reader(self.store):
            self.assertTrue(sources.context_complete(self.store, context))
            policy = self.root / POLICY_FILE
            policy.write_text(packed({'enabled': False}))
            self.assertFalse(sources.context_complete(self.store, context))
            policy.unlink()
            self.assertFalse(sources.context_complete(self.store, context))
        with sources.reference_reader(self.store):
            self.assertTrue(sources.context_complete(self.store, context))

    def test_completion_timeout_is_not_hidden_and_releases_reader(self):
        from activity_mvp.local_checks import LocalCheckTimeout
        self.add('user', 'Keep the original local deadline.'); self.scan()
        context = self.store.rows('SELECT id FROM contexts')[0]['id']
        with self.assertRaises(LocalCheckTimeout):
            with sources.reference_reader(self.store):
                self.assertTrue(sources.context_complete(self.store, context))
                raw = sources._reference_reader.get()['authority'].conn.raw
                with patch.object(sources, '_context_complete', side_effect=LocalCheckTimeout('reflection_prepare')):
                    sources.context_complete(self.store, context)
        self.assertIsNone(sources._reference_reader.get())
        with self.assertRaises(sqlite3.ProgrammingError):
            raw.execute('SELECT 1')

    def test_reused_reader_observes_pause_without_caching_authority(self):
        self.add('user', 'Check current permission.'); self.scan()
        ref = json.loads(self.store.rows('SELECT reference FROM turns')[0]['reference'])
        with sources.reference_reader(self.store):
            self.assertTrue(sources.reference_current(ref, store=self.store))
            self.feedback.set('paused', True)
            self.assertFalse(sources.reference_current(ref, store=self.store))
        self.assertIsNone(sources._reference_reader.get())

    def test_gapped_final_batch_does_not_certify_whole_context(self):
        self.add('user', 'Missing prefix.'); self.add('user', 'Current tail.'); self.scan()
        rows = self.store.rows('SELECT * FROM turns ORDER BY seq')
        with patch.object(analysis, 'context_complete', side_effect=AssertionError('gapped source')):
            data, known, through = analysis.payload(self.store, self.now,
                gap_after={rows[0]['context_id']: rows[0]['seq']})
        part = data['current_contexts'][0]
        self.assertTrue(part['missing_prefix'])
        self.assertFalse(part['complete'])
        self.assertEqual(through[part['context_id']], rows[-1]['seq'])
        self.assertNotIn(rows[0]['id'], known)

    def test_policy_failure_cannot_revive_same_batch_and_next_batch_is_fresh(self):
        self.add('user', 'Current source.'); self.scan()
        ref = json.loads(self.store.rows('SELECT reference FROM turns')[0]['reference'])
        with sources.reference_reader(self.store):
            self.assertTrue(sources.reference_current(ref, store=self.store))
            reader = sources._reference_reader.get()
            with patch.object(sources, '_reader_identity', side_effect=ValueError('changed')):
                self.assertFalse(sources.reference_current(ref, store=self.store))
            self.assertFalse(sources.reference_current(ref, store=self.store))
        with sources.reference_reader(self.store):
            self.assertIsNot(sources._reference_reader.get(), reader)
            self.assertTrue(sources.reference_current(ref, store=self.store))

    def test_reader_observes_registration_revocation_from_second_wal_connection(self):
        self.add('user', 'Current registration.'); self.scan()
        self.assertEqual(self.feedback.conn.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
        ref = json.loads(self.store.rows('SELECT reference FROM turns')[0]['reference'])
        with sources.reference_reader(self.store):
            self.assertTrue(sources.reference_current(ref, store=self.store))
            raw = sources._reference_reader.get()['authority'].conn.raw
            self.assertFalse(raw.in_transaction)
            with sqlite3.connect(self.feedback.path) as writer:
                writer.execute("UPDATE sources SET path=? WHERE id='codex'", (str(self.root/'revoked'),))
            self.assertFalse(sources.reference_current(ref, store=self.store))
            self.assertFalse(raw.in_transaction)

    def test_timeout_closes_reader_and_restores_nested_root(self):
        from activity_mvp.local_checks import LocalCheckTimeout
        self.add('user', 'Current source.'); self.scan()
        ref = json.loads(self.store.rows('SELECT reference FROM turns')[0]['reference'])
        with self.assertRaises(LocalCheckTimeout):
            with sources.reference_reader(self.store):
                self.assertTrue(sources.reference_current(ref, store=self.store))
                reader = sources._reference_reader.get()
                raw = reader['authority'].conn.raw
                with sources.reference_reader({'feedback_root': str(self.root/'other')}):
                    self.assertIsNot(sources._reference_reader.get(), reader)
                self.assertIs(sources._reference_reader.get(), reader)
                raise LocalCheckTimeout('reflection_prepare')
        self.assertIsNone(sources._reference_reader.get())
        with self.assertRaises(sqlite3.ProgrammingError):
            raw.execute('SELECT 1')

    def test_reader_opens_lazily_and_invalidates_changed_projection(self):
        self.add('user', 'Observe current visibility.'); self.scan()
        ref = json.loads(self.store.rows('SELECT reference FROM turns')[0]['reference'])
        with patch.object(sources, 'FeedbackStore', wraps=Feedback) as opened:
            with sources.reference_reader(self.store):
                opened.assert_not_called()
                self.assertTrue(sources.reference_current(ref, store=self.store))
                self.assertTrue(sources.reference_current(ref, store=self.store))
                full_readers = [c for c in opened.call_args_list if not c.kwargs.get('controls_only')]
                self.assertEqual(len(full_readers), 1)
                (self.root / POLICY_FILE).write_text(packed({'enabled': False}))
                self.assertFalse(sources.reference_current(ref, store=self.store))
                (self.root / POLICY_FILE).unlink()
                self.assertFalse(sources.reference_current(ref, store=self.store))
        self.assertIsNone(sources._reference_reader.get())

    def test_reused_reader_observes_hermes_owner_revocation_in_wal(self):
        path = self.root / 'hermes.sqlite'
        with sqlite3.connect(path) as db:
            db.executescript('CREATE TABLE sessions(id,source,user_id);CREATE TABLE messages(id INTEGER,session_id,role,content,timestamp);')
            db.execute('INSERT INTO sessions VALUES(?,?,?)', ('local-session', 'telegram', 'synthetic-owner'))
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?)', (1, 'local-session', 'user', 'Synthetic owner text.', self.now))
        path.chmod(0o600)
        self.feedback.set('config', {'enabled': True, 'hermes_owner_ids': {'hermes': ['synthetic-owner']}})
        self.feedback.conn.execute('INSERT INTO sources(id,platform,path) VALUES(?,?,?)', ('hermes', 'hermes', str(path)))
        self.scan()
        ref = json.loads(self.store.rows("SELECT reference FROM turns WHERE source_id='hermes'")[0]['reference'])
        self.assertEqual(self.feedback.conn.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
        with sources.reference_reader(self.store):
            self.assertTrue(sources.reference_current(ref, store=self.store))
            with Feedback(self.feedback.root) as writer:
                writer.set('config', {'enabled': True, 'hermes_owner_ids': {}})
            self.assertFalse(sources.reference_current(ref, store=self.store))

    def test_reused_reader_rechecks_source_bytes_and_resets_on_exception(self):
        self.add('user', 'Original source.'); self.scan()
        ref = json.loads(self.store.rows('SELECT reference FROM turns')[0]['reference'])
        with self.assertRaisesRegex(RuntimeError, 'end batch'):
            with sources.reference_reader(self.store):
                self.assertTrue(sources.reference_current(ref, store=self.store))
                self.path.write_text(self.path.read_text().replace('Original source.', 'Modified source.'))
                self.assertFalse(sources.reference_current(ref, store=self.store))
                raise RuntimeError('end batch')
        self.assertIsNone(sources._reference_reader.get())

    def test_reused_reader_rejects_replaced_database_before_or_during_read(self):
        self.add('user', 'Preserve the current authority.'); self.scan()
        ref = json.loads(self.store.rows('SELECT reference FROM turns')[0]['reference'])
        def replace_database():
            replacement = self.feedback.path.with_suffix('.replacement')
            shutil.copyfile(self.feedback.path, replacement)
            replacement.chmod(0o600)
            replacement.replace(self.feedback.path)
        with sources.reference_reader(self.store):
            self.assertTrue(sources.reference_current(ref, store=self.store))
            replace_database()
            self.assertFalse(sources.reference_current(ref, store=self.store))
        original = sources._reference_metadata_rows
        def replace_after_query(*args, **kwargs):
            rows = original(*args, **kwargs)
            replace_database()
            return rows
        with sources.reference_reader(self.store):
            with patch.object(sources, '_reference_metadata_rows', side_effect=replace_after_query):
                self.assertFalse(sources.reference_current(ref, store=self.store))

    def quote(self, row, text, **kwargs):
        return analysis.quote(self.store,{'turn_id':row['id'],'quote':text,'attribution':'user_statement'},
            {row['id']:row},**kwargs)

    def test_old_control_wrappers_are_material_even_with_user_transport_role(self):
        for tag in ('heartbeat','hook_prompt','codex_delegation','codex_internal_context'):
            with self.subTest(tag=tag):
                rows=self.old_cache('<'+tag+'>我缺少判断标准。</'+tag+'>')
                row=rows[-1]
                with self.assertRaisesRegex(ValueError,'material_not_owner'):self.quote(row,'我缺少判断标准。')
                self.assertEqual(self.quote(row,'我缺少判断标准。',summary=True)['attribution'],'material')

    def test_split_old_control_body_does_not_become_owner_and_outside_text_survives(self):
        for tag in ('codex_delegation','codex_internal_context'):
            with self.subTest(tag=tag):
                rows=self.old_cache('<'+tag+'>'+('a'*11995)+'我仍反复比较。</'+tag+'>\n我自己的问题需要什么证据？')
                # Select this newest message, retaining its earlier chunks.
                message=json.loads(rows[-1]['reference'])['message_id']
                current=[r for r in rows if json.loads(r['reference'])['message_id']==message]
                row=next(r for r in current if '我仍反复比较。' in r['text'])
                with self.assertRaisesRegex(ValueError,'material_not_owner'):self.quote(row,'我仍反复比较。')
                own=next(r for r in current if '我自己的问题需要什么证据？' in r['text'])
                self.assertEqual(self.quote(own,'我自己的问题需要什么证据？')['attribution'],'user_statement')

    def test_missing_or_inconsistent_chunks_cannot_supply_owner_evidence(self):
        rows=self.old_cache('<hook_prompt>'+('b'*12000)+'我缺少依据。</hook_prompt>\n我自己的下一步是什么？')
        row=rows[-1]
        before=self.store.rows('SELECT processed_seq FROM contexts')
        self.store.conn.execute('DELETE FROM turns WHERE id=?',(rows[0]['id'],))
        self.assertIsNone(sources.cached_material_ranges(self.store,row))
        with self.assertRaisesRegex(ValueError,'material_not_owner'):self.quote(row,'我自己的下一步是什么？')
        self.assertEqual(self.store.rows('SELECT processed_seq FROM contexts'),before)

    def test_corrupt_chunk_positions_are_not_silently_joined(self):
        rows=self.old_cache(('c'*12000)+'我自己的下一步是什么？')
        ref=json.loads(rows[0]['reference']);ref['chunk_start']=1
        self.store.conn.execute('UPDATE turns SET reference=? WHERE id=?',(packed(ref),rows[0]['id']))
        self.assertIsNone(sources.cached_material_ranges(self.store,rows[-1]))

    def test_nested_and_unclosed_controls_are_conservative_without_hiding_other_owner_text(self):
        for text,material,own in (
            ('我需要证据。<HEARTBEAT><hook_prompt>自动内容</hook_prompt></HEARTBEAT>我会核查。','自动内容','我会核查。'),
            ('我需要证据。<hook_prompt>未闭合控制内容','未闭合控制内容','我需要证据。'),
            ('我需要证据。<heartbeat><hook_prompt>错配</heartbeat>仍是控制内容','仍是控制内容','我需要证据。'),
            ('我需要证据。<CODEX_INTERNAL_CONTEXT mode="resume">自动目标<heartbeat>嵌套控制</heartbeat></CODEX_INTERNAL_CONTEXT>我会核查。','自动目标','我会核查。'),
            ('我需要证据。<codex_internal_context>未闭合内部目标','未闭合内部目标','我需要证据。'),
            ('我需要证据。<codex_internal_context><hook_prompt>错配</codex_internal_context>仍是内部目标','仍是内部目标','我需要证据。')):
            with self.subTest(text=text):
                ranges=sources.material_ranges(text)
                for quote,expected in ((material,True),(own,False)):
                    i=text.index(quote)
                    self.assertEqual(any(a<i+len(quote) and b>i for a,b in ranges),expected)

    def test_internal_context_hints_and_hard_quotes_recheck_old_cache(self):
        rows=self.old_cache('<codex_internal_context><goal>我缺少判断标准。</goal></codex_internal_context>\n我会先验证两个条件。')
        row=rows[-1];before=self.store.rows('SELECT id,reference FROM turns')
        data,_,_=analysis.payload(self.store,self.now)
        turn=data['current_contexts'][0]['turns'][0]
        start=row['text'].index('我缺少判断标准。')
        self.assertTrue(any(a<=start<b for a,b in turn['material_ranges']))
        with self.assertRaisesRegex(ValueError,'material_not_owner'):
            self.quote(row,'我缺少判断标准。')
        self.assertEqual(self.quote(row,'我缺少判断标准。',summary=True)['attribution'],'material')
        self.assertEqual(self.quote(row,'我会先验证两个条件。')['attribution'],'user_statement')
        self.assertEqual(self.store.rows('SELECT id,reference FROM turns'),before)
        self.assertEqual(self.store.rows('SELECT * FROM calls'),[])

    def test_payload_exposes_current_material_offsets_and_never_mutates_old_references(self):
        rows=self.old_cache('<heartbeat><automation_id>automation-9</automation_id></heartbeat>\n我需要补哪项证据？')
        before=self.store.rows('SELECT id,reference FROM turns')
        data,_,_=analysis.payload(self.store,self.now)
        turn=data['current_contexts'][0]['turns'][0]
        self.assertTrue(turn['attribution_complete']);self.assertTrue(turn['material_ranges'])
        self.assertLessEqual(len(packed(data).encode()),analysis.MAX_BYTES)
        self.assertEqual(self.store.rows('SELECT id,reference FROM turns'),before)
        self.assertEqual(self.store.rows('SELECT * FROM calls'),[])

    def test_dense_material_hints_remain_bounded_and_hard_quotes_precise(self):
        prefix='我会核对现有依据。'
        middle='我需要比较哪些事实？'
        tail='我先验证两个判断标准。'
        text=prefix+'\n'+('"" '*1960)+middle+('"" '*1960)+'\n'+tail
        rows=self.old_cache(text);self.assertEqual(len(rows),1);row=rows[0]
        before=self.store.rows('SELECT id,reference FROM turns')
        exact=sources.cached_material_ranges(self.store,row);self.assertGreater(len(exact),128)
        data,_,through=analysis.payload(self.store,self.now)
        self.assertEqual(len(through),1)
        turn=data['current_contexts'][0]['turns'][0]
        self.assertTrue(turn['attribution_complete']);self.assertTrue(turn['material_ranges_coarsened'])
        self.assertEqual(turn['material_ranges'],[[exact[0][0],exact[-1][1]]])
        self.assertLessEqual(len((analysis.SYSTEM+'\n'+packed(data)).encode()),analysis.MAX_BYTES)
        a,b=turn['material_ranges'][0]
        self.assertLess(text.index(prefix),a);self.assertGreaterEqual(text.index(tail),b)
        self.assertTrue(a<text.index(middle)<b)
        for own in (prefix,middle,tail):self.assertEqual(self.quote(row,own)['attribution'],'user_statement')
        # The full exact collection is retained beyond the 128-hint boundary.
        with self.assertRaisesRegex(ValueError,'material_not_owner'):self.quote(row,'""')
        self.assertTrue(any(a<=text.rindex('""')<b for a,b in exact))
        self.assertEqual(self.store.rows('SELECT id,reference FROM turns'),before)
        self.assertEqual(self.store.rows('SELECT * FROM calls'),[])

    def test_saved_owner_claim_on_control_material_invalidates_old_summary(self):
        for tag in ('hook_prompt','codex_internal_context'):
            with self.subTest(tag=tag):
                row=self.old_cache('<'+tag+'>我没有明确判断标准。</'+tag+'>')[-1]
                evidence={'id':row['id'],'role':'user','observed':row['observed'],'quote':'我没有明确判断标准。','attribution':'user_statement'}
                self.assertFalse(sources.evidence_current(self.store,evidence))
                summary={'summary':'旧摘要不能复用。','complete':True,'evidence':[evidence]}
                self.store.conn.execute('UPDATE contexts SET summary=?,processed_seq=?',(packed(summary),row['seq']))
                context=self.store.rows('SELECT * FROM contexts')[0]
                self.assertEqual(analysis.previous_summary(self.store,context,self.now),{})
                self.assertEqual(self.store.rows('SELECT processed_seq FROM contexts')[0]['processed_seq'],row['seq'])

    def test_plain_owner_and_quoted_material_keep_their_distinct_attribution(self):
        row=self.old_cache('“别人还在比较。”\n我需要核查哪些新证据？')[0]
        self.assertEqual(self.quote(row,'我需要核查哪些新证据？')['attribution'],'user_statement')
        with self.assertRaisesRegex(ValueError,'material_not_owner'):self.quote(row,'别人还在比较。')


class ExactSourceMetadataTests(unittest.TestCase):
    setUp = retired.RetiredSourceMaintenanceTests.setUp
    tearDown = fixture.ReflectionTests.tearDown

    def shared_claude(self, filename='safe.name"quoted\\part.jsonl'):
        self.source = self.root / 'claude'
        self.source.mkdir()
        self.path = self.source / 'project' / filename
        self.path.parent.mkdir()
        sources.personal_scope.policy()['personal_session_ids'].append(self.path.stem)
        lines = [{'type': role, 'sessionId': 'synthetic-owner-session',
                  'userType': 'external', 'timestamp': self.now,
                  'message': {'role': role, 'content': text}}
                 for role, text in [('user', '需要核对哪些条件？'), ('assistant', '先核已知依据。')]]
        self.path.write_text(''.join(packed(x) + '\n' for x in lines))
        self.path.chmod(0o600)
        self.feedback.conn.execute('DELETE FROM sources')
        self.feedback.conn.execute('INSERT INTO sources(id,platform,path) VALUES(?,?,?)',
                                   ('claude', 'claude', str(self.source)))
        sources.scan(self.store, self.feedback, self.now)
        self.context = self.store.rows('SELECT id FROM contexts')[0]['id']
        self.ref = json.loads(self.store.rows('SELECT reference FROM turns ORDER BY seq')[0]['reference'])
        for store in (self.feedback, self.store):
            cursor = json.loads(store.rows("SELECT cursor FROM sources WHERE id='claude'")[0]['cursor'])
            cursor.setdefault('files', {})['FormerOrg/private.jsonl'] = {'offset': 999999}
            store.conn.execute("UPDATE sources SET cursor=? WHERE id='claude'", (packed(cursor),))
        self.policy = self.root / POLICY_FILE
        self.policy.write_text(packed({'version': 1, 'enabled': True,
            'retired_names': ['FormerOrg'], 'retired_paths': [], 'retired_root_refs': []}))
        self.policy.chmod(0o600)

    def test_allowed_reference_does_not_inherit_unrelated_cursor_exclusion(self):
        self.shared_claude()
        with Feedback(self.feedback.root, readonly=True) as authority:
            self.assertEqual(authority.rows("SELECT path,platform FROM sources WHERE id='claude'"), [])
        self.assertTrue(sources.reference_current(self.ref, store=self.store))

    def test_readonly_completion_uses_only_exact_file_progress(self):
        self.shared_claude()
        with Store(self.store.root, readonly=True) as read:
            self.assertEqual(read.rows("SELECT cursor FROM sources WHERE id='claude'"), [])
            self.assertTrue(sources.context_complete(read, self.context))

    def test_force_and_changed_tail_remain_incomplete(self):
        self.shared_claude()
        cursor = json.loads(self.store.rows("SELECT cursor FROM sources WHERE id='claude'")[0]['cursor'])
        cursor['files'][str(self.path.relative_to(self.source))]['force'] = True
        cursor['files']['neighbor.jsonl'] = {'offset': 999999, 'inode': self.path.stat().st_ino,
                                            'mtime_ns': self.path.stat().st_mtime_ns}
        self.store.conn.execute("UPDATE sources SET cursor=? WHERE id='claude'", (packed(cursor),))
        with Store(self.store.root, readonly=True) as read:
            self.assertFalse(sources.context_complete(read, self.context))
        cursor['files'][str(self.path.relative_to(self.source))].pop('force')
        self.store.conn.execute("UPDATE sources SET cursor=? WHERE id='claude'", (packed(cursor),))
        with self.path.open('a') as out:
            out.write(packed({'type': 'assistant', 'sessionId': 'synthetic-owner-session',
                             'timestamp': self.now, 'message': {'role': 'assistant', 'content': '新尾部。'}}) + '\n')
        with Store(self.store.root, readonly=True) as read:
            self.assertFalse(sources.context_complete(read, self.context))

    def test_registered_root_itself_stays_excluded_before_body_read(self):
        self.shared_claude()
        self.policy.write_text(packed({'version': 1, 'enabled': True,
            'retired_names': ['claude'], 'retired_paths': [], 'retired_root_refs': []}))
        original_open = Path.open
        def guarded(path, *args, **kwargs):
            if path == self.path:
                raise AssertionError('excluded root body read')
            return original_open(path, *args, **kwargs)
        with patch.object(Path, 'open', guarded):
            self.assertFalse(sources.reference_current(self.ref, store=self.store))

    def test_unregistered_identity_cannot_use_shared_root_metadata(self):
        self.shared_claude()
        with patch.object(sources, '_owned_regular', side_effect=AssertionError('unregistered source read')):
            self.assertFalse(sources._reference_current({**self.ref, 'session': 'unknown-session'}))

    def test_display_policy_change_during_registration_read_rejects_metadata(self):
        self.shared_claude()
        with Feedback(self.feedback.root, readonly=True) as authority:
            original_execute = authority.conn.raw.execute
            def changed(sql, args=()):
                result = original_execute(sql, args)
                if sql.startswith('SELECT path,platform FROM main.sources'):
                    self.policy.write_text(packed({'version': 1, 'enabled': True,
                        'retired_names': ['claude'], 'retired_paths': [], 'retired_root_refs': []}))
                return result
            with patch.object(authority.conn.raw, 'execute', side_effect=changed):
                self.assertEqual(sources._reference_metadata_rows(authority,
                    'SELECT path,platform FROM main.sources WHERE id=?', ('claude',), self.ref), [])

    def test_wrong_platform_and_missing_file_progress_cannot_certify_completion(self):
        self.shared_claude()
        self.feedback.conn.execute("UPDATE sources SET platform='codex' WHERE id='claude'")
        self.assertFalse(sources.reference_current(self.ref, store=self.store))
        self.feedback.conn.execute("UPDATE sources SET platform='claude' WHERE id='claude'")
        self.store.conn.execute("UPDATE sources SET cursor=? WHERE id='claude'",
            (packed({'files': {'neighbor.jsonl': {'offset': 999999}, 'FormerOrg/private.jsonl': {}}}),))
        with Store(self.store.root, readonly=True) as read:
            self.assertFalse(sources.context_complete(read, self.context))

    def test_every_original_message_version_is_still_checked(self):
        self.shared_claude()
        self.path.write_text(self.path.read_text().replace('先核已知依据', '助手已改结论'))
        self.assertTrue(sources.reference_current(self.ref, store=self.store))
        with Store(self.store.root, readonly=True) as read:
            self.assertFalse(sources.context_complete(read, self.context))

    def test_exact_hermes_session_progress_keeps_tail_and_owner_checks(self):
        sources.personal_scope.policy()['personal_session_ids'].append('local-session')
        path = self.root / 'hermes.sqlite'
        with sqlite3.connect(path) as db:
            db.executescript('CREATE TABLE sessions(id,source,user_id);CREATE TABLE messages(id INTEGER,session_id,role,content,timestamp);')
            db.execute('INSERT INTO sessions VALUES(?,?,?)', ('local-session', 'cli', 'local'))
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?)', (1, 'local-session', 'user', '本人发言。', self.now))
        path.chmod(0o600)
        self.feedback.conn.execute('INSERT INTO sources(id,platform,path) VALUES(?,?,?)', ('hermes', 'hermes', str(path)))
        sources.scan(self.store, self.feedback, self.now)
        context = self.store.rows("SELECT id FROM contexts WHERE source_id='hermes'")[0]['id']
        self.feedback.conn.execute("UPDATE sources SET cursor=? WHERE id='hermes'", (packed({'sessions': {'FormerOrg': 9}}),))
        self.store.conn.execute("UPDATE sources SET cursor=? WHERE id='hermes'",
            (packed({'sessions': {'local-session': 1, 'FormerOrg': 9}}),))
        policy = self.root / POLICY_FILE
        policy.write_text(packed({'version': 1, 'enabled': True,
            'retired_names': ['FormerOrg'], 'retired_paths': [], 'retired_root_refs': []}))
        policy.chmod(0o600)
        with Store(self.store.root, readonly=True) as read:
            self.assertTrue(sources.context_complete(read, context))
        with sqlite3.connect(path) as db:
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?)', (2, 'local-session', 'assistant', '新回复。', self.now))
        with Store(self.store.root, readonly=True) as read:
            self.assertFalse(sources.context_complete(read, context))
        self.store.conn.execute("UPDATE sources SET cursor=? WHERE id='hermes'",
            (packed({'sessions': {'local-session': 2, 'FormerOrg': 9}}),))
        with sqlite3.connect(path) as db:
            db.execute("UPDATE sessions SET source='telegram',user_id='other' WHERE id='local-session'")
        with Store(self.store.root, readonly=True) as read:
            self.assertFalse(sources.context_complete(read, context))


class UsableBatchSelectionTests(unittest.TestCase):
    setUp = retired.RetiredSourceMaintenanceTests.setUp
    tearDown = fixture.ReflectionTests.tearDown

    def discussions(self, count, stale_count):
        sources.personal_scope.policy()['personal_session_ids'].extend('batch-' + str(i) for i in range(count))
        paths = []
        for i in range(count):
            path = self.path.with_name('batch-' + str(i) + '.jsonl')
            lines = [{'type': 'session_meta', 'payload': {'id': 'batch-' + str(i), 'source': 'cli'}},
                     {'timestamp': self.now - count + i, 'type': 'response_item',
                      'payload': {'type': 'message', 'role': 'user',
                                  'content': [{'type': 'input_text', 'text': '原条件需要检查。'}]}}]
            path.write_text(''.join(packed(x) + '\n' for x in lines)); path.chmod(0o600)
            paths.append(path)
        for _ in range((count + 19) // 20):
            sources.scan(self.store, self.feedback, self.now)
        for path in paths[:stale_count]:
            path.write_text(path.read_text().replace('原条件', '新条件'))
        return paths

    def test_five_stale_candidates_do_not_hide_the_sixth_valid_discussion(self):
        self.discussions(6, 5)
        data, known, through = analysis.payload(self.store, self.now)
        self.assertEqual(len(through), 1)
        self.assertEqual({row['session'] for row in known.values()}, {'batch-5'})
        self.assertLessEqual(len((analysis.SYSTEM + '\n' + packed(data)).encode()), 65536)
        self.assertIn('来源内容变化', self.store.get('coverage_gap'))

    def test_only_five_usable_discussions_enter_input(self):
        self.discussions(8, 1)
        data, known, through = analysis.payload(self.store, self.now)
        self.assertEqual(len(through), 5)
        self.assertEqual(data['coverage']['total_discussions'], 5)
        self.assertNotIn('batch-0', {row['session'] for row in known.values()})

    def test_more_than_one_metadata_page_does_not_hide_valid_later_contexts(self):
        self.discussions(52, 50)
        data, known, through = analysis.payload(self.store, self.now)
        self.assertEqual(len(through), 2)
        self.assertEqual({row['session'] for row in known.values()}, {'batch-50', 'batch-51'})
        self.assertIn('来源内容变化', self.store.get('coverage_gap'))

    def test_invalidation_removing_pending_rows_does_not_shift_the_next_page(self):
        self.discussions(52, 0)
        pending = analysis.pending_contexts(self.store, self.now, lambda: True)
        first = [next(pending)[0] for _ in range(50)]
        # Hermes invalidation can remove a failed source's derived turns;
        # OFFSET pagination would now skip the still-pending final two rows.
        self.store.conn.executemany('DELETE FROM turns WHERE context_id=?', [(c['id'],) for c in first])
        remaining = [c['session'] for c, _ in pending]
        self.assertEqual(remaining, ['batch-50', 'batch-51'])


if __name__ == '__main__':
    unittest.main()
