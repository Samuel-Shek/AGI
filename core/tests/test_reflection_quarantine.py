"""Synthetic control isolation; these fixtures are not natural help cases."""
import json
import unittest
from unittest.mock import patch
from activity_mvp.reflection import analysis
from activity_mvp.feedback.store import packed,digest
from tests import test_reflection as fixture
from tests import test_retired_source_maintenance as retired
from tests import test_reflection_pending_context_lookup as metadata

class QuarantineMetadataTests(metadata.PendingContextLookupTests):
    def test_session_move_denies_new_context_identity_without_opening_body(self):
        self.seed({'owned':['1'*64],'denied':['2'*64]})
        self.raw.conn.execute('INSERT INTO contexts VALUES(?,?,?)',('moved','codex','owned-session'))
        blocked=set()
        self.assertIsNotNone(analysis.submitted_inputs(self.store,self.feedback,lambda:True,quarantined_contexts=blocked))
        self.assertEqual(blocked,{'owned','denied','moved'})

    def test_conflicting_archive_identity_keeps_global_barrier(self):
        self.seed({'owned':['1'*64]})
        self.raw.conn.executescript("CREATE TABLE retired_contexts(id TEXT,source_id TEXT,session TEXT); INSERT INTO retired_contexts VALUES('owned','codex','different-session');")
        self.assertIsNone(self.run_prior())

    def test_invalid_range_metadata_still_blocks_everything(self):
        for ranges in ({'owned':[]},{'owned':['not-a-hash']},{'owned':'bad'}, {'missing':['1'*64]}):
            with self.subTest(ranges=ranges):
                self.raw.conn.execute('DELETE FROM calls');self.seed(ranges)
                self.assertIsNone(self.run_prior())

    def test_invalid_fingerprint_still_blocks_without_legacy_body(self):
        self.seed({'owned':['1'*64]})
        self.raw.conn.execute("UPDATE calls SET detail=json_set(detail,'$.input_fingerprint','bad')")
        self.assertIsNone(self.run_prior())

    def test_registered_run_cannot_authorize_missing_prompt_metadata(self):
        self.seed(None)
        with patch.object(analysis.personal_scope,'admitted',return_value=True):
            self.assertIsNone(self.run_prior())
        self.assertFalse(any('SELECT id,state,detail' in sql for sql,_ in self.raw.queries))

    def test_duplicate_context_key_is_rejected_instead_of_losing_a_range(self):
        self.raw.conn.execute('INSERT INTO calls VALUES(?,?,?)',('a'*24,'unknown',
            '{"input_fingerprint":"'+'f'*64+'","input_ranges":{"owned":["'+'1'*64+'"],"owned":["'+'2'*64+'"]}}'))
        self.assertIsNone(self.run_prior())

    def test_duplicate_top_level_control_field_is_rejected(self):
        for key in ('input_ranges','input_fingerprint'):
            with self.subTest(key=key):
                self.raw.conn.execute('DELETE FROM calls')
                values={'input_ranges':'{"owned":["'+'1'*64+'"]}','input_fingerprint':'"'+'f'*64+'"'}
                fields=[(k,v) for k,v in values.items()]+[(key,values[key])]
                raw='{'+','.join('"'+k+'":'+v for k,v in fields)+'}'
                self.raw.conn.execute('INSERT INTO calls VALUES(?,?,?)',('a'*24,'unknown',raw))
                self.assertIsNone(self.run_prior())

class QuarantineAnalysisTests(unittest.TestCase):
    setUp=retired.RetiredSourceMaintenanceTests.setUp
    tearDown=fixture.ReflectionTests.tearDown
    add=fixture.ReflectionTests.add
    scan=fixture.ReflectionTests.scan
    seed=fixture.ReflectionTests.seed
    fail_model=fixture.ReflectionTests.fail_model
    result=fixture.ReflectionTests.result
    run_model=fixture.ReflectionTests.run_model
    excluded_rows=retired.RetiredSourceMaintenanceTests.excluded_rows

    def mixed_unknown(self):
        self.fail_model();self.excluded_rows()
        call=self.store.rows('SELECT * FROM calls ORDER BY created DESC LIMIT 1')[0]
        detail=json.loads(call['detail']);detail['input_ranges']['company']=['c'*64]
        self.store.conn.execute('UPDATE calls SET detail=? WHERE id=?',(packed(detail),call['id']))
        return self.store.rows('SELECT * FROM calls WHERE id=?',(call['id'],))[0]

    def fresh(self,session='synthetic-other'):
        analysis.personal_scope.policy()['personal_session_ids'].extend([session,'fresh'])
        path=self.path.with_name('fresh.jsonl')
        lines=[{'type':'session_meta','payload':{'id':session,'source':'cli'}},
               {'timestamp':self.now+1,'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'一段独立的新讨论，有待核实的条件。'}]}}]
        path.write_text(''.join(packed(x)+'\n' for x in lines));path.chmod(0o600);self.scan()
        return next(c['id'] for c in self.store.rows('SELECT id,session FROM contexts') if c['session']==session)

    def invoke_new(self,old_ids,seen):
        def invoke(feedback,prompt,schema,**kw):
            data=json.loads(prompt[len(analysis.SYSTEM)+1:]);seen.append(data)
            self.assertLessEqual(len(prompt.encode()),65536)
            self.assertLessEqual(data['coverage']['total_discussions'],5)
            contexts={c['context_id'] for group in ('current_contexts','historical_contexts') for c in data[group]}
            contexts.update(e['context_id'] for i in data['previous_insights'] for e in i.get('evidence',[]))
            self.assertFalse(contexts&old_ids)
            kw['admission_check']()
            key=feedback.reserve('analysis');kw['on_reserved'](key);feedback.settle(key,'settled')
            return {'contexts':[{'context_id':c['context_id'],'summary':'本批只处理独立新讨论。','evidence':[]} for c in data['current_contexts']],
                    'insight':{'related_insight':'','judgment':'not_found',**{k:'' for k in analysis.FIELDS},'evidence':[]},'reviews':[]},key
        return invoke

    def test_mixed_old_call_preserved_while_independent_new_context_progresses(self):
        self.seed();old_id=self.store.rows('SELECT id FROM contexts')[0]['id'];old=self.mixed_unknown();new=self.fresh();seen=[]
        with patch.object(analysis.model,'invoke',side_effect=self.invoke_new({old_id,'company'},seen)),patch.object(analysis,'legacy_input_fingerprint',side_effect=AssertionError('old prompt opened')):
            result=analysis.analyze(self.store,self.feedback,self.now+1)
        self.assertEqual(result['processed'],1);self.assertEqual(len(seen),1)
        self.assertEqual(self.store.rows('SELECT * FROM calls WHERE id=?',(old['id'],))[0],old)
        progress={c['id']:c['processed_seq'] for c in self.store.rows('SELECT id,processed_seq FROM contexts')}
        self.assertEqual(progress[old_id],0);self.assertGreater(progress[new],0)
        self.assertIn('未重复提交',self.store.get('replay_gap'))

    def test_mixed_context_excluded_from_history_active_evidence_and_current_input(self):
        self.seed();value,*_=self.result();self.run_model(value)
        old_id=self.store.rows('SELECT id FROM contexts')[0]['id']
        self.add('user','继续核实原讨论的新条件。');self.scan();self.mixed_unknown();self.fresh();seen=[]
        with patch.object(analysis.model,'invoke',side_effect=self.invoke_new({old_id,'company'},seen)):
            analysis.analyze(self.store,self.feedback,self.now+1)
        self.assertEqual(len(seen),1);self.assertEqual(seen[0]['previous_insights'],[])
        self.assertEqual(seen[0]['historical_contexts'],[])

    def test_muted_description_is_retained_without_its_quarantined_evidence(self):
        self.seed();value,*_=self.result();self.run_model(value)
        item=self.store.rows('SELECT id FROM insights')[0]['id']
        self.store.conn.execute("UPDATE insights SET state='muted' WHERE id=?",(item,))
        self.add('user','继续核实原讨论的新条件。');self.scan();self.mixed_unknown();self.fresh();seen=[]
        with patch.object(analysis.model,'invoke',side_effect=self.invoke_new(set(),seen)):
            analysis.analyze(self.store,self.feedback,self.now+1)
        self.assertEqual([i['id'] for i in seen[0]['previous_insights']],[item])
        self.assertEqual(seen[0]['previous_insights'][0]['evidence'],[])
        self.assertTrue(seen[0]['suppression_complete'])

    def test_concurrent_unknown_is_rechecked_for_all_input_before_budget(self):
        self.seed();self.mixed_unknown();new=self.fresh();before=self.feedback.rows('SELECT * FROM budget')
        def invoke(feedback,prompt,schema,**kw):
            data=json.loads(prompt[len(analysis.SYSTEM)+1:])
            known={t['id']:t for t in self.store.rows('SELECT * FROM turns WHERE context_id=?',(new,))}
            detail={'input_fingerprint':digest([prompt,schema]),'input_ranges':analysis.input_ranges(data,known,self.store)}
            detail['input_ranges']['company']=['c'*64]
            self.store.conn.execute('INSERT INTO calls VALUES(?,?,?,?)',('d'*24,'unknown',self.now,packed(detail)))
            kw['admission_check']();raise AssertionError('must reject before reserve')
        with patch.object(analysis.model,'invoke',side_effect=invoke):
            result=analysis.analyze(self.store,self.feedback,self.now+1)
        self.assertEqual(result['reason'],'reflection_input_quarantined')
        self.assertEqual(self.feedback.rows('SELECT * FROM budget'),before)

    def test_pause_before_admission_keeps_budget_unchanged(self):
        self.seed();self.mixed_unknown();self.fresh();before=self.feedback.rows('SELECT * FROM budget')
        def invoke(feedback,prompt,schema,**kw):
            self.store.set('enabled',False);kw['admission_check']();raise AssertionError('not active')
        with patch.object(analysis.model,'invoke',side_effect=invoke):
            with self.assertRaisesRegex(PermissionError,'not_active'):
                analysis.analyze(self.store,self.feedback,self.now+1)
        self.assertEqual(self.feedback.rows('SELECT * FROM budget'),before)

if __name__=='__main__':unittest.main()
