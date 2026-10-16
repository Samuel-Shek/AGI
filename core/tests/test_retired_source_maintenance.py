"""Current retirement policy, synthetic sources, retained uncertainty and history."""
import json
import sqlite3
import unittest
from unittest.mock import patch
from tests import test_reflection as fixtures
from activity_mvp.reflection import sources, analysis
from activity_mvp.feedback.store import packed
from activity_mvp.feedback import sources as feedback_sources


class RetiredSourceMaintenanceTests(unittest.TestCase):
    def setUp(self):
        fixtures.ReflectionTests.setUp(self)
        policy={'version':'personal-v1','owner_id':'local-owner','cutover_at':'2026-01-01T00:00:00+00:00',
                'company_roots':[str(self.root/'company')],'personal_roots':[str(self.root)],
                'personal_files':[],'personal_source_ids':[],
                'personal_session_ids':['synthetic-owner-session','fixture']}
        for guard in (patch.object(sources.personal_scope,'company_stopped',return_value=True),
                      patch.object(sources.personal_scope,'policy',return_value=policy)):
            guard.start();self.addCleanup(guard.stop)
    tearDown = fixtures.ReflectionTests.tearDown
    add = fixtures.ReflectionTests.add
    scan = fixtures.ReflectionTests.scan
    seed = fixtures.ReflectionTests.seed
    fail_model = fixtures.ReflectionTests.fail_model

    def excluded_rows(self):
        turn=self.store.rows('SELECT * FROM turns LIMIT 1')[0]
        context=self.store.rows('SELECT * FROM contexts LIMIT 1')[0]
        for session in ('company','unregistered'):
            c={**context,'id':session,'session':session,'summary':packed({'synthetic':session})}
            self.store.conn.execute('INSERT INTO contexts('+','.join(c)+') VALUES('+','.join('?' for _ in c)+')',tuple(c.values()))
            t={**turn,'seq':None,'id':session,'context_id':session,'session':session,'text':'synthetic retained '+session,'reference':packed({'parser_version':1})}
            self.store.conn.execute('INSERT INTO turns('+','.join(t)+') VALUES('+','.join('?' for _ in t)+')',tuple(t.values()))
        return self.store.rows("SELECT * FROM turns WHERE session IN ('company','unregistered')"),self.store.rows("SELECT * FROM contexts WHERE session IN ('company','unregistered')")

    def old_personal_parser(self):
        for row in self.store.rows("SELECT id,reference FROM turns WHERE session='synthetic-owner-session'"):
            ref=json.loads(row['reference']);ref.pop('parser_version',None)
            self.store.conn.execute('UPDATE turns SET reference=? WHERE id=?',(packed(ref),row['id']))
        self.store.set('source_parser_version',1)

    def test_migration_preserves_excluded_rows_and_cursor(self):
        self.seed();before=self.excluded_rows();self.old_personal_parser()
        cursor=json.loads(self.store.rows("SELECT cursor FROM sources WHERE id='codex'")[0]['cursor'])
        cursor['files']['2026/10/05/company.jsonl']={'offset':17,'synthetic':'unchanged'}
        self.store.conn.execute('UPDATE sources SET cursor=? WHERE id=?',(packed(cursor),'codex'))
        self.scan()
        self.assertEqual(before,self.excluded_snapshot())
        retired=self.store.rows('SELECT session FROM retired_turns')
        self.assertEqual([r['session'] for r in retired],['synthetic-owner-session']*3)
        current=self.store.rows("SELECT reference FROM turns WHERE session='synthetic-owner-session'")
        self.assertTrue(all(json.loads(r['reference'])['parser_version']==2 for r in current))
        cursor=json.loads(self.store.rows("SELECT cursor FROM sources WHERE id='codex'")[0]['cursor'])
        self.assertEqual(cursor['files']['2026/10/05/company.jsonl'],{'offset':17,'synthetic':'unchanged'})

    def excluded_snapshot(self):
        return self.store.rows("SELECT * FROM turns WHERE session IN ('company','unregistered')"),self.store.rows("SELECT * FROM contexts WHERE session IN ('company','unregistered')")

    def test_personal_legacy_rows_migrate_after_global_version_two(self):
        self.seed();self.old_personal_parser();self.store.set('source_parser_version',2)
        self.scan();self.assertEqual(len(self.store.rows('SELECT * FROM retired_turns')),3)
        self.assertTrue(all(json.loads(r['reference'])['parser_version']==2 for r in self.store.rows('SELECT reference FROM turns')))

    def test_path_switch_archives_only_personal_derived_input(self):
        self.seed();before=self.excluded_rows();old=self.path
        prior=self.store.rows("SELECT * FROM sources WHERE id='codex'")[0]
        cursor=json.loads(prior['cursor']);cursor['files']['company.jsonl']={'offset':17}
        prior['cursor']=packed(cursor)
        self.store.conn.execute('UPDATE sources SET cursor=? WHERE id=?',(prior['cursor'],'codex'))
        new=self.root/'new-source';new.mkdir()
        self.feedback.conn.execute('UPDATE sources SET path=? WHERE id=?',(str(new),'codex'))
        self.scan();self.assertEqual(before,self.excluded_snapshot());self.assertTrue(old.exists())
        self.assertEqual(self.store.rows("SELECT id FROM turns WHERE session='synthetic-owner-session'"),[])
        self.assertEqual(len(self.store.rows('SELECT * FROM retired_turns')),3)
        self.assertFalse(analysis.ready(self.store))
        self.assertEqual(self.store.rows('SELECT * FROM retired_sources'),[prior])

    def test_registered_personal_name_in_company_path_never_reads_body(self):
        self.seed();company=self.root/'company';path=company/'2026/10/05/fixture.jsonl'
        path.parent.mkdir(parents=True);path.write_text(self.path.read_text());path.chmod(0o600)
        source={**self.store.rows("SELECT * FROM sources WHERE id='codex'")[0],'path':str(company),'cursor':'{}'}
        with patch.object(sources,'visible_text',side_effect=AssertionError('company body read')):
            _,count,_=sources.jsonl(self.store,source,self.now)
        self.assertEqual(count,0)
        self.assertFalse(sources._reference_current({'session':'synthetic-owner-session','path':str(path)}))

    def test_registered_filename_with_unknown_real_header_never_reads_body(self):
        self.seed();self.lines[0]['payload']['id']='unregistered-owner'
        self.path.write_text(''.join(packed(x)+'\n' for x in self.lines))
        source={**self.store.rows("SELECT * FROM sources WHERE id='codex'")[0],'cursor':'{}'}
        with patch.object(sources,'visible_text',side_effect=AssertionError('unknown body read')):
            _,count,_=sources.jsonl(self.store,source,self.now)
        self.assertEqual(count,0)

    def test_cached_company_path_cannot_borrow_personal_session(self):
        self.seed();row=self.store.rows('SELECT id,reference FROM turns LIMIT 1')[0]
        ref=json.loads(row['reference']);ref['path']=str(self.root/'company'/'fixture.jsonl')
        self.store.conn.execute('UPDATE turns SET reference=? WHERE id=?',(packed(ref),row['id']))
        view=sources.filtered(self.store,'reflection')
        self.assertEqual(view.rows('SELECT id FROM turns WHERE id=?',(row['id'],)),[])
        self.assertEqual(view.rows('SELECT id FROM contexts'),[])

    def test_feedback_reader_checks_real_session_and_path_before_text(self):
        self.seed();self.feedback.set('config',{'enabled':True,'since':self.now-14*86400})
        source=self.feedback.rows("SELECT * FROM sources WHERE id='codex'")[0]
        self.lines[0]['payload']['id']='unregistered-owner'
        self.path.write_text(''.join(packed(x)+'\n' for x in self.lines))
        with patch.object(feedback_sources,'_text',side_effect=AssertionError('unknown feedback body read')):
            _,count,_=feedback_sources.scan_jsonl(self.feedback,source)
        self.assertEqual(count,0)
        company=self.root/'company';path=company/'2026/10/05/fixture.jsonl'
        path.parent.mkdir(parents=True);path.write_text(self.path.read_text());path.chmod(0o600)
        with patch.object(feedback_sources,'_text',side_effect=AssertionError('company feedback body read')):
            _,count,_=feedback_sources.scan_jsonl(self.feedback,{**source,'path':str(company)})
        self.assertEqual(count,0)
        with patch.object(feedback_sources,'_owned_regular',side_effect=AssertionError('company original reopened')):
            self.assertFalse(feedback_sources.reference_is_current({'session':'synthetic-owner-session','source_id':'codex','reference':packed({'path':str(path)})}))

    def test_cached_feedback_company_path_is_not_claimed_or_admitted(self):
        ref=packed({'path':str(self.root/'company'/'fixture.jsonl')})
        self.feedback.conn.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)',('m','codex','synthetic-owner-session',self.now,'synthetic failure','',''+ref,'pending',self.now,0))
        self.feedback.conn.execute('INSERT INTO incidents(id,title,category,state,created,updated) VALUES(?,?,?,?,?,?)',('i','synthetic','execution','investigating',self.now,self.now))
        self.feedback.conn.execute('INSERT INTO quotes VALUES(?,?)',('m','i'))
        view=sources.filtered(self.feedback,'feedback')
        self.assertEqual(view.rows('SELECT id FROM messages'),[])
        self.assertEqual(view.rows('SELECT id FROM incidents'),[])
        self.assertFalse(feedback_sources.incident_admitted(self.feedback,'i'))

    def test_missing_or_invalid_cache_paths_are_excluded_readonly_and_mutable(self):
        self.seed();turn=self.store.rows('SELECT id,reference FROM turns LIMIT 1')[0]
        ref=json.loads(turn['reference'])
        self.feedback.conn.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)',('m','codex','synthetic-owner-session',self.now,'synthetic failure','','{}','pending',self.now,0))
        self.feedback.conn.execute('INSERT INTO incidents(id,title,category,state,created,updated) VALUES(?,?,?,?,?,?)',('i','synthetic','execution','investigating',self.now,self.now))
        self.feedback.conn.execute('INSERT INTO quotes VALUES(?,?)',('m','i'))
        for invalid in (None,'','relative.jsonl',17,{},'/invalid\x00path'):
            with self.subTest(path=invalid):
                with patch.object(feedback_sources,'direct_text',side_effect=AssertionError('invalid source body processed')),patch.object(sources,'visible_text',side_effect=AssertionError('invalid reflection body processed')):
                    self.assertFalse(feedback_sources.insert(self.feedback,{'id':'codex'},'synthetic-owner-session',self.now,'synthetic failure','',{'path':invalid}))
                    self.assertFalse(sources.admit(self.store,{'id':'codex'},'synthetic-owner-session','user',self.now,'synthetic',{'path':invalid},self.now))
                with patch.object(feedback_sources,'_owned_regular',side_effect=AssertionError('invalid original reopened')):
                    self.assertFalse(feedback_sources.reference_is_current({'session':'synthetic-owner-session','source_id':'codex','reference':packed({'path':invalid})}))
                self.assertFalse(feedback_sources.source_metadata_admitted(None,str(self.path),'codex'))
                altered={**ref,'path':invalid}
                self.store.conn.execute('UPDATE turns SET reference=? WHERE id=?',(packed(altered),turn['id']))
                self.feedback.conn.execute('UPDATE messages SET reference=? WHERE id=?',(packed({'path':invalid}),'m'))
                self.assertFalse(feedback_sources.incident_admitted(self.feedback,'i'))
                for readonly in (False,True):
                    with self.subTest(readonly=readonly):
                        reflection=self.store;feedback=self.feedback
                        if readonly:
                            reflection=fixtures.Store(self.store.root,readonly=True)
                            feedback=fixtures.FeedbackStore(self.feedback.root,readonly=True)
                        try:
                            view=sources.filtered(reflection,'reflection')
                            self.assertEqual(view.rows('SELECT id FROM turns WHERE id=?',(turn['id'],)),[])
                            self.assertEqual(view.rows('SELECT id FROM contexts'),[])
                            view=sources.filtered(feedback,'feedback')
                            self.assertEqual(view.rows('SELECT id FROM messages'),[])
                            self.assertEqual(view.rows('SELECT id FROM incidents'),[])
                        finally:
                            if readonly:reflection.conn.close();feedback.close()

    def test_excluded_hermes_identities_do_not_starve_personal_session(self):
        path=self.root/'synthetic-hermes.sqlite'
        with sqlite3.connect(path) as conn:
            conn.execute('CREATE TABLE sessions(id TEXT,source TEXT,user_id TEXT)')
            conn.execute('CREATE TABLE messages(id INTEGER,session_id TEXT,role TEXT,content TEXT,timestamp REAL)')
            sessions=['company-'+str(i) for i in range(20)]+['synthetic-owner-session']
            for index,session in enumerate(sessions):
                conn.execute('INSERT INTO sessions VALUES(?,?,?)',(session,'cli','local'))
                conn.execute('INSERT INTO messages VALUES(?,?,?,?,?)',(index+1,session,'user','synthetic personal content',self.now))
        path.chmod(0o600)
        prior={'sessions':{'company-0':7},'seen':{'company-0':1}}
        source={'id':'hermes','path':str(path),'cursor':packed(prior)}
        cursor,added,status=sources.hermes(self.store,source,self.feedback,self.now)
        self.assertEqual(added,1);self.assertEqual(status['identity_excluded'],20)
        self.assertEqual(cursor['sessions']['company-0'],7);self.assertEqual(cursor['seen']['company-0'],1)
        self.assertIn('synthetic-owner-session',cursor['sessions'])
        source['cursor']=packed(cursor)
        cursor,added,status=sources.hermes(self.store,source,self.feedback,self.now)
        self.assertEqual(added,0);self.assertEqual(status['status'],'partial_identity')
        self.assertEqual(len(cursor['sessions']),2);self.assertEqual(len(cursor['seen']),2)

    def test_retired_only_personal_context_preserves_history_notices(self):
        self.seed();result,_,_,_=fixtures.ReflectionTests.result(self)
        fixtures.ReflectionTests.run_model(self,result)
        self.store.conn.execute('CREATE TABLE IF NOT EXISTS retired_turns AS SELECT * FROM turns WHERE 0')
        self.store.conn.execute('CREATE TABLE IF NOT EXISTS retired_contexts AS SELECT * FROM contexts WHERE 0')
        self.store.conn.execute('INSERT INTO retired_turns SELECT * FROM turns')
        self.store.conn.execute('INSERT INTO retired_contexts SELECT * FROM contexts')
        self.store.conn.execute('DELETE FROM turns');self.store.conn.execute('DELETE FROM contexts')
        before=self.store.rows('SELECT * FROM retired_turns')
        for readonly in (False,True):
            view_store=self.store if not readonly else fixtures.Store(self.store.root,readonly=True)
            try:
                view=sources.filtered(view_store,'reflection')
                self.assertEqual(len(view.rows('SELECT id FROM retired_contexts')),1)
                self.assertEqual(len(view.rows('SELECT id FROM retired_turns')),3)
                self.assertEqual(len(view.rows('SELECT id FROM insights')),1)
                self.assertEqual(len(view.rows('SELECT id FROM notices')),1)
            finally:
                if readonly:view_store.conn.close()
        row=before[0];ref=json.loads(row['reference']);ref['path']=str(self.root/'company'/'fixture.jsonl')
        self.store.conn.execute('UPDATE retired_turns SET reference=? WHERE id=?',(packed(ref),row['id']))
        for readonly in (False,True):
            view_store=self.store if not readonly else fixtures.Store(self.store.root,readonly=True)
            try:
                view=sources.filtered(view_store,'reflection')
                for table in ('retired_contexts','retired_turns','insights','notices'):
                    self.assertEqual(view.rows('SELECT id FROM '+table),[])
            finally:
                if readonly:view_store.conn.close()
        self.assertEqual(len(self.store.rows('SELECT id FROM retired_turns')),3)

    def test_path_switch_cannot_replay_uncertain_same_source_bytes(self):
        self.seed();self.fail_model();original=self.path.read_bytes()
        new=self.root/'moved-codex';path=new/'2026/10/05/fixture.jsonl'
        path.parent.mkdir(parents=True);path.write_bytes(original);path.chmod(0o600)
        self.feedback.conn.execute('UPDATE sources SET path=? WHERE id=?',(str(new),'codex'))
        self.scan();calls=self.store.rows('SELECT * FROM calls');budget=self.feedback.rows('SELECT * FROM budget')
        progress=self.store.rows('SELECT id,processed_seq FROM contexts')
        with patch.object(analysis.model,'invoke',side_effect=AssertionError('copied unknown input replayed')):
            result=analysis.analyze(self.store,self.feedback,self.now)
        self.assertEqual(result['processed'],0);self.assertEqual(result['reason'],'prior_input_unresolved')
        self.assertEqual(calls,self.store.rows('SELECT * FROM calls'));self.assertEqual(budget,self.feedback.rows('SELECT * FROM budget'))
        self.assertEqual(progress,self.store.rows('SELECT id,processed_seq FROM contexts'))

    def test_unknown_legacy_never_opens_input_or_replays(self):
        self.seed();self.fail_model();self.store.conn.execute('UPDATE calls SET detail=?',(packed({'reason':'synthetic unknown'}),))
        before=self.store.rows('SELECT * FROM calls');budget=self.feedback.rows('SELECT * FROM budget')
        sources.personal_scope.policy()['personal_roots']=[]
        with patch.object(analysis,'legacy_input_fingerprint',side_effect=AssertionError('unknown body opened')),patch.object(analysis.model,'invoke',side_effect=AssertionError('unknown request replayed')):
            result=analysis.analyze(self.store,self.feedback,self.now)
        self.assertEqual(result['reason'],'prior_input_unverifiable')
        self.assertEqual(before,self.store.rows('SELECT * FROM calls'));self.assertEqual(budget,self.feedback.rows('SELECT * FROM budget'))

    def test_mixed_uncertain_ranges_stay_barrier_without_replay(self):
        self.seed();self.fail_model();context=self.store.rows('SELECT id FROM contexts')[0]['id']
        detail={'input_fingerprint':'a'*64,'input_ranges':{context:['b'*64],'unknown-context':['c'*64]}}
        self.store.conn.execute('UPDATE calls SET detail=?',(packed(detail),))
        before=self.store.rows('SELECT * FROM calls');budget=self.feedback.rows('SELECT * FROM budget')
        with patch.object(analysis,'legacy_input_fingerprint',side_effect=AssertionError('mixed body opened')),patch.object(analysis.model,'invoke',side_effect=AssertionError('mixed request replayed')):
            result=analysis.analyze(self.store,self.feedback,self.now)
        self.assertEqual(result['reason'],'prior_input_unverifiable')
        self.assertEqual(before,self.store.rows('SELECT * FROM calls'));self.assertEqual(budget,self.feedback.rows('SELECT * FROM budget'))

    def test_failed_archive_rolls_back_personal_and_excluded_rows(self):
        self.seed();self.excluded_rows();self.old_personal_parser();before=self.store.rows('SELECT * FROM turns')
        raw=self.store.conn
        class Interrupted:
            def __getattr__(self,key):return getattr(raw,key)
            def execute(self,sql,args=()):
                if sql.startswith('INSERT INTO retired_turns'):raise RuntimeError('synthetic interruption')
                return raw.execute(sql,args)
        self.store.conn=Interrupted()
        with self.assertRaisesRegex(RuntimeError,'synthetic interruption'):self.scan()
        self.store.conn=raw
        self.assertEqual(before,self.store.rows('SELECT * FROM turns'));self.assertEqual(self.store.get('source_parser_version'),1)


if __name__=='__main__':unittest.main()
