"""Reflection provenance, lifecycle and interruption behavior on synthetic sources."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from activity_mvp.feedback.store import Store as FeedbackStore,packed,digest
from activity_mvp.reflection.store import Store,record_feedback
from activity_mvp.reflection import sources,analysis,service
from tests.personal_scope_fixtures import register_personal

class ReflectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve();self.now=time.time()
        register_personal(self, self.root, ['synthetic-owner-session', 'fixture', 'local-session', 'trusted-session', 'session', 'another-synthetic-fixture', 'another', 'synthetic-sixth-context', 'sixth', 'synthetic-other', 'other', 'synthetic-history', 'history'] + ['synthetic-context-'+str(i) for i in range(4)] + ['context-'+str(i) for i in range(4)] + ['synthetic-long-'+str(i) for i in range(5)] + ['long-'+str(i) for i in range(5)])
        self.feedback=FeedbackStore(self.root/'feedback');self.feedback.set('config',{'enabled':True})
        self.store=Store(self.root/'reflection');self.store.set('enabled',True)
        self.source=self.root/'codex';self.path=self.source/'2026'/'10'/'05'/'fixture.jsonl';self.path.parent.mkdir(parents=True)
        self.feedback.conn.execute('INSERT INTO sources(id,platform,path) VALUES(?,?,?)',('codex','codex',str(self.source)))
        self.lines=[{'type':'session_meta','payload':{'id':'synthetic-owner-session','source':'cli'}}]
    def tearDown(self):
        self.store.conn.close();self.feedback.close();self.temp.cleanup()
    def add(self,role,text,age=0):
        self.lines.append({'timestamp':self.now-age,'type':'response_item','payload':{'type':'message','role':role,'content':[{'type':'input_text' if role=='user' else 'output_text','text':text}]}})
        self.path.write_text(''.join(packed(line)+'\n' for line in self.lines));self.path.chmod(0o600)
    def scan(self):return sources.scan(self.store,self.feedback,self.now)
    def result(self,judgment='preliminary'):
        data,known,through=analysis.payload(self.store,self.now)
        contexts=[];refs=[]
        for c in data['current_contexts']:
            evidence=[{'turn_id':t['turn_id'],'quote':t['text'][:30],'attribution':'user_statement' if t['role']=='user' else 'assistant_answer'} for t in c['turns']]
            contexts.append({'context_id':c['context_id'],'summary':'讨论目标、依据、现实条件和AI回答；不能据此判断私人动机。','evidence':evidence[:3]})
            refs.extend(evidence)
        insight={'related_insight':'','judgment':judgment,**{k:'可核查的具体条件，尚待检验。' for k in analysis.FIELDS},'evidence':refs[:8]}
        insight['question']='我需要什么证据才能决定下一步？'
        if judgment=='not_found':insight['evidence']=[];insight['question']='';insight['pattern']=''
        reviews=[{'id':i['id'],'action':'keep','reason':'本批无足够反证。','evidence':[]} for i in data['previous_insights'] if i['state']=='active']
        return {'contexts':contexts,'insight':insight,'reviews':reviews},data,known,through
    def run_model(self,result,side_effect=None):
        def invoke(feedback,prompt,schema,**kw):
            key=feedback.reserve('analysis');kw['on_reserved'](key)
            self.assertTrue(kw['continue_check']());self.assertLessEqual(len(prompt.encode()),65536)
            feedback.settle(key,'settled',{'fixture':True})
            if side_effect:side_effect()
            return result,key
        with patch.object(analysis.model,'invoke',side_effect=invoke):return analysis.analyze(self.store,self.feedback,self.now)
    def seed(self):
        self.add('user','我还缺哪项证据才能决定？');self.add('assistant','已经回答了两项，但尚未给出可检查的依据。');self.add('user','同样两个方案，我仍没有决策标准。');self.scan()
    def test_primary_roles_window_and_credentials(self):
        self.add('user','旧记录',15*86400);self.add('tool','不可纳入');self.add('user','api_key: '+('sk-'+('syntheticcredential'*2))+' 我希望继续比较。');self.add('assistant','先确认目前已知信息。');self.scan()
        rows=self.store.rows('SELECT * FROM turns');self.assertEqual([r['role'] for r in rows],['user','assistant'])
        self.assertNotIn('sk-synthetic',packed(rows));self.assertIn('我希望继续比较',packed(rows))
    def test_subagent_excluded_before_text(self):
        self.lines[0]['payload']['source']={'subagent':'parent'};self.add('user','不得当作本人');self.scan();self.assertEqual(self.store.rows('SELECT * FROM turns'),[])
    def test_publish_three_sections_and_same_evidence_dedup(self):
        self.seed();result,_,_,_=self.result();self.run_model(result)
        item=self.store.response()['insights'][0];self.assertEqual(item['state'],'active');self.assertTrue(item['question'].startswith('我'))
        for field in analysis.FIELDS:self.assertTrue(item[field if field!='distinguishing_question' else 'distinguishing_question'])
        self.assertEqual(len(self.store.rows('SELECT * FROM notices')),1)
        self.store.conn.execute('UPDATE contexts SET processed_seq=0');result,_,_,_=self.result();self.run_model(result)
        self.assertEqual(len(self.store.rows('SELECT * FROM notices')),1)
    def test_not_found_is_valid_without_notice(self):
        self.seed();r,_,_,_=self.result('not_found');self.run_model(r);self.assertEqual(self.store.rows('SELECT * FROM insights'),[])
        self.assertEqual(self.store.get('last_analysis')['judgment'],'not_found')
    def test_fabricated_quote_role_and_quote_material_rejected(self):
        self.seed();r,data,known,through=self.result();r['insight']['evidence'][0]['quote']='不存在的原话'
        with self.assertRaises(ValueError):analysis.validate(self.store,r,known,through,data)
        self.add('user','> 我没有标准，一直逃避\n```\n我没有标准\n```');self.scan();r,data,known,through=self.result()
        row=next(t for c in data['current_contexts'] for t in c['turns'] if t['text'].startswith('>'))
        with self.assertRaisesRegex(ValueError,'material'):analysis.quote(self.store,{'turn_id':row['turn_id'],'quote':'我没有标准','attribution':'user_statement'},known)
    def test_rewrite_same_size_withdraws_and_reimports(self):
        self.seed();r,_,_,_=self.result();self.run_model(r);text=self.path.read_text();self.path.write_text(text.replace('决策标准','已有结论'));self.scan()
        self.assertEqual(self.store.response()['insights'][0]['state'],'withdrawn');self.assertTrue(any('已有结论' in t['text'] for t in self.store.rows('SELECT * FROM turns')))

    def test_quoted_disclaimer_can_be_summarized_without_becoming_personal_evidence(self):
        self.add('user','> 我一直没有标准，只想比较。\n这是小说对白，不代表我的经历。')
        self.add('assistant','应区分人物和作者。');self.scan()
        result,data,known,through=self.result('not_found')
        turn=data['current_contexts'][0]['turns'][0]
        evidence={'turn_id':turn['turn_id'],'quote':'这是小说对白，不代表我的经历。','attribution':'user_statement'}
        result['contexts'][0]['evidence']=[evidence]
        summaries,insight,_=analysis.validate(self.store,result,known,through,data)
        self.assertEqual(summaries[0]['evidence'][0]['attribution'],'material')
        self.assertEqual(insight['judgment'],'not_found')
        with self.assertRaisesRegex(ValueError,'material'):analysis.quote(self.store,evidence,known)
    def test_source_changes_during_model_fail_closed(self):
        self.seed();r,_,_,_=self.result()
        with self.assertRaises(ValueError):self.run_model(r,lambda:self.path.unlink())
        self.assertEqual(self.store.rows('SELECT * FROM insights'),[]);self.assertEqual(self.store.rows('SELECT * FROM calls')[0]['state'],'validation_failed')
    def test_stop_and_reenable_discards_late_result(self):
        self.seed();r,_,_,_=self.result()
        def stop():self.store.set('enabled',False);self.store.set('authorization_epoch',1);self.store.set('enabled',True)
        with self.assertRaises(PermissionError):self.run_model(r,stop)
        self.assertEqual(self.store.rows('SELECT * FROM notices'),[])
    def test_feedback_not_truth_and_mute_persists_restart(self):
        self.seed();r,_,_,_=self.result();self.run_model(r);item=self.store.response()['insights'][0]
        record_feedback(self.store,{'id':item['id'],'revision':item['revision'],'choice':'helpful'});self.assertEqual(self.store.rows('SELECT * FROM insights')[0]['state'],'active')
        record_feedback(self.store,{'id':item['id'],'revision':item['revision'],'choice':'mute'})
        with Store(self.root/'reflection',readonly=True) as read:self.assertEqual(read.response()['insights'][0]['state'],'muted')
    def test_long_turns_continue_without_premature_insight(self):
        self.add('user','甲'*25000);self.add('assistant','进一步解释。');self.add('user','已有结论，下一步可以检查。');self.scan()
        pieces=self.store.rows('SELECT * FROM turns WHERE role=\'user\'');self.assertGreater(len(pieces),2)
        self.assertEqual(len({json.loads(p['reference'])['message_id'] for p in pieces[:-1]}),1)
        r,data,_,_=self.result('not_found');self.assertGreater(data['coverage']['partial_current'],0);self.run_model(r)
        self.assertTrue(analysis.ready(self.store));r,data,_,_=self.result('not_found');self.assertEqual(data['coverage']['partial_current'],0);self.run_model(r);self.assertFalse(analysis.ready(self.store))
    def test_pause_skips_reading_and_notification(self):
        self.seed();before=self.store.get('last_scan');self.feedback.set('paused',True)
        with patch.object(sources,'jsonl',side_effect=AssertionError('read after pause')),patch.object(service.subprocess,'run',side_effect=AssertionError('notify after pause')):
            self.assertEqual(self.scan()['status'],'paused');self.assertEqual(service.notify(self.feedback)['requested'],0)
        self.assertEqual(self.store.get('last_scan'),before)
    def test_new_progress_withdraws_prior_judgment(self):
        self.seed();r,_,_,_=self.result();self.run_model(r)
        self.add('user','已经确定标准并完成了比较，这个取舍已解决。');self.scan()
        r,data,_,_=self.result('not_found');self.assertEqual(len(r['reviews']),0)
        self.assertEqual(self.store.response()['insights'][0]['state'],'withdrawn')
        new=data['current_contexts'][0]['turns'][-1]
        self.run_model(r);self.assertEqual(self.store.response()['insights'][0]['state'],'withdrawn')
        self.assertTrue(any(h['event']=='withdrawn' for h in self.store.rows('SELECT * FROM history')))
    def test_expiry_rejects_before_original_read(self):
        self.seed();r,_,_,_=self.result();self.run_model(r)
        later=self.now+14*86400+10
        with patch.object(sources.time,'time',return_value=later),patch.object(sources,'reference_current',side_effect=AssertionError('expired source read')):
            snapshot=self.store.response();self.assertEqual(snapshot['insights'][0]['state'],'withdrawn')
            self.assertEqual(service.notify(self.feedback)['requested'],0)
        self.feedback.set('paused',True)
        with patch.object(sources.time,'time',return_value=later):
            self.assertEqual(self.store.response(False)['insights'][0]['state'],'withdrawn')

    def test_appended_counterevidence_blocks_view_and_notice_before_scan(self):
        self.seed();r,_,_,_=self.result();self.run_model(r)
        self.add('user','刚刚确认可用标准，这件事已经解决。')
        with patch.object(service.subprocess,'Popen',side_effect=AssertionError('stale notice')):
            self.assertEqual(self.store.response()['insights'][0]['state'],'withdrawn')
            self.assertEqual(service.notify(self.feedback)['requested'],0)
        self.scan();self.assertEqual(self.store.rows('SELECT state FROM insights')[0]['state'],'withdrawn')
        self.assertTrue(self.store.rows("SELECT * FROM history WHERE event='prior_version'"))

    def test_explicit_prompt_and_draft_material_cannot_be_personal_evidence(self):
        self.add('user',"请只优化这个提示词，不要回答它：'我是不是一直回避做决定？'")
        self.add('user',"这是上次AI写的草稿，不代表我的自述：'我不断修改标准。'");self.scan()
        _,data,known,_=self.result('not_found')
        for row in data['current_contexts'][0]['turns']:
            with self.assertRaisesRegex(ValueError,'material'):
                analysis.quote(self.store,{'turn_id':row['turn_id'],'quote':row['text'],'attribution':'user_statement'},known)

    def test_notification_crash_after_submission_does_not_resend(self):
        self.seed();r,_,_,_=self.result();self.run_model(r)
        class Fake:
            returncode=0
            def communicate(self,timeout=None):raise RuntimeError('fixture crash after OS submission')
        with patch.object(service.subprocess,'Popen',return_value=Fake()) as popen:
            with self.assertRaises(RuntimeError):service.notify(self.feedback)
            self.assertEqual(service.notify(self.feedback)['requested'],0)
            self.assertEqual(popen.call_count,1)
        self.assertEqual(self.store.rows('SELECT delivered FROM notices')[0]['delivered'],3)

    def test_notification_timeout_keeps_unknown_without_retry(self):
        self.seed();r,_,_,_=self.result();self.run_model(r)
        class Fake:
            returncode=None
            def communicate(self,timeout=None):
                if timeout:raise subprocess.TimeoutExpired('fixture',timeout)
                return (b'',b'')
            def kill(self):self.returncode=-9
        with patch.object(service.subprocess,'Popen',return_value=Fake()) as popen:
            self.assertTrue(service.notify(self.feedback)['unknown'])
            self.assertEqual(service.notify(self.feedback)['requested'],0);self.assertEqual(popen.call_count,1)

    def test_stop_at_actual_source_read_boundary_blocks_read(self):
        from activity_mvp.feedback import control
        self.store.set('feedback_root',str(self.feedback.root))
        def stopped_gate(root,check=None):
            self.feedback.set('paused',True)
            return control.gate(root,check)
        with patch.object(sources,'gate',side_effect=stopped_gate):
            with self.assertRaises(PermissionError):sources.read_active(self.store,lambda:self.feedback.active(),lambda:self.fail('read after stop'))

    def test_partial_other_discussion_cannot_support_new_publication(self):
        self.seed();r,_,_,_=self.result();self.run_model(r)
        self.add('user','新的讨论进展。');self.scan()
        r,data,known,through=self.result()
        with patch.object(analysis,'context_version',return_value=None):
            with self.assertRaisesRegex(ValueError,'incomplete_context'):analysis.validate(self.store,r,known,through,data)

    def test_stop_at_model_launch_boundary_does_not_start_runner(self):
        from activity_mvp.feedback import control,model
        self.feedback.set('model_capability',{'enabled':True})
        self.feedback.set('config',{'enabled':True,'codex_binary':'/usr/bin/true'})
        def stopped_gate(root,check=None):
            self.feedback.set('paused',True)
            return control.gate(root,check)
        with patch.object(model,'gate',side_effect=stopped_gate),patch.object(model.subprocess,'Popen',side_effect=AssertionError('runner after stop')):
            with self.assertRaises(PermissionError):model.invoke(self.feedback,'synthetic no tools',{'type':'object'})
        self.assertEqual(self.feedback.rows('SELECT state FROM budget')[0]['state'],'unknown')

    def test_hermes_timestamp_edit_invalidates_before_body(self):
        path=self.root/'hermes-time.sqlite'
        with sqlite3.connect(path) as db:
            db.executescript('CREATE TABLE sessions(id,source,user_id);CREATE TABLE messages(id INTEGER,session_id,role,content,timestamp);')
            db.execute('INSERT INTO sessions VALUES(?,?,?)',('local-session','cli','local'))
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?)',(1,'local-session','user','本人发言',self.now))
        path.chmod(0o600);self.feedback.conn.execute('INSERT INTO sources(id,platform,path) VALUES(?,?,?)',('hermes','hermes',str(path)));self.scan()
        row=next(t for t in self.store.rows('SELECT * FROM turns') if t['source_id']=='hermes');ref=json.loads(row['reference']);self.assertTrue(sources.reference_current(ref))
        with sqlite3.connect(path) as db:db.execute('UPDATE messages SET timestamp=?',(self.now-15*86400,))
        self.assertFalse(sources.reference_current(ref))

    def test_hermes_owner_mapping_revocation_invalidates_evidence(self):
        path=self.root/'hermes.sqlite'
        with sqlite3.connect(path) as db:
            db.executescript('CREATE TABLE sessions(id,source,user_id);CREATE TABLE messages(id INTEGER,session_id,role,content,timestamp);')
            db.execute('INSERT INTO sessions VALUES(?,?,?)',('trusted-session','telegram','synthetic-owner'))
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?)',(1,'trusted-session','user','本人发言',self.now))
            db.execute('INSERT INTO sessions VALUES(?,?,?)',('third-party','telegram','other'))
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?)',(2,'third-party','user','第三方私密材料',self.now))
        path.chmod(0o600);self.feedback.conn.execute('INSERT INTO sources(id,platform,path) VALUES(?,?,?)',('remote','hermes',str(path)))
        self.feedback.set('config',{'enabled':True,'hermes_owner_ids':{'remote':['synthetic-owner']}});self.scan()
        rows=self.store.rows("SELECT * FROM turns WHERE source_id='remote'");self.assertEqual(len(rows),1)
        reference=json.loads(rows[0]['reference']);self.assertTrue(sources.reference_current(reference))
        self.feedback.set('config',{'enabled':True,'hermes_owner_ids':{}});self.assertFalse(sources.reference_current(reference))
    def test_source_registration_revocation_invalidates_before_publish(self):
        self.seed();r,_,_,_=self.result()
        with self.assertRaises(ValueError):self.run_model(r,lambda:self.feedback.conn.execute("DELETE FROM sources WHERE id='codex'"))
        self.assertEqual(self.store.rows('SELECT * FROM insights'),[])

    def test_paused_read_snapshot_does_not_reopen_sources(self):
        self.seed();r,_,_,_=self.result();self.run_model(r)
        with patch.object(sources,'evidence_current',side_effect=AssertionError('raw source opened')):
            snapshot=self.store.response(False)
        self.assertTrue(snapshot['paused']);self.assertFalse(snapshot['insights'][0]['source_checked'])

    def test_previous_hypothesis_remains_reviewable_after_summary_moves_on(self):
        self.seed();r,_,_,_=self.result();self.run_model(r)
        self.store.conn.execute("UPDATE contexts SET summary='{}'")
        self.add('user','这个判断已经解决，新的问题是执行条件。');self.scan()
        data,known,through=analysis.payload(self.store,self.now)
        self.assertEqual(len(data['previous_insights']),1)
        self.assertTrue(all(e['id'] in known for e in data['previous_insights'][0]['evidence']))

    def test_two_pending_analysis_families_rotate(self):
        from activity_mvp.feedback import service as controller
        self.seed();self.feedback.set('config',{'enabled':True,'since':self.now-14*86400,'journal':str(self.root/'journal')});self.feedback.set('investigate_requests',['synthetic-pending'])
        order=[]
        def reflection_call(_):order.append('reflection');return {'processed':0}
        def feedback_call(_):order.append('feedback');return {'processed':0}
        with patch.object(service,'analyze',side_effect=reflection_call),patch.object(controller.model,'investigate_requested',side_effect=feedback_call),patch.object(controller.journal,'render',return_value={}):
            for _ in range(4):controller.tick(self.feedback.root,repair=False)
        self.assertEqual(order,['reflection','feedback','reflection','feedback'])

    def test_model_failure_preserves_progress_and_unknown_charge(self):
        self.seed()
        def fail(feedback,prompt,schema,**kw):
            key=feedback.reserve('analysis');kw['on_reserved'](key);feedback.settle(key,'unknown');raise TimeoutError('synthetic failure')
        with patch.object(analysis.model,'invoke',side_effect=fail):
            with self.assertRaises(TimeoutError):analysis.analyze(self.store,self.feedback)
        self.assertTrue(analysis.ready(self.store));self.assertEqual(self.store.rows('SELECT * FROM calls')[0]['state'],'unknown')
        self.assertEqual(self.feedback.rows('SELECT state FROM budget')[0]['state'],'unknown')
        self.assertEqual(self.store.rows('SELECT * FROM notices'),[])

    def fail_model(self):
        def fail(feedback,prompt,schema,**kw):
            key=feedback.reserve('analysis');kw['on_reserved'](key);feedback.settle(key,'unknown')
            raise RuntimeError('model_result_unavailable')
        with patch.object(analysis.model,'invoke',side_effect=fail):
            with self.assertRaises(RuntimeError):analysis.analyze(self.store,self.feedback,self.now)

    def test_unknown_unchanged_input_is_not_resubmitted_after_reopen(self):
        self.seed();self.fail_model()
        prior=self.feedback.rows('SELECT * FROM budget');watermarks=self.store.rows('SELECT id,processed_seq FROM contexts')
        self.store.conn.close();self.store=Store(self.root/'reflection')
        with patch.object(analysis.model,'invoke',side_effect=AssertionError('must not resend')):
            result=analysis.analyze(self.store,self.feedback,self.now)
        self.assertEqual(result['reason'],'prior_input_unresolved')
        self.assertEqual(self.feedback.rows('SELECT * FROM budget'),prior)
        self.assertEqual(self.store.rows('SELECT id,processed_seq FROM contexts'),watermarks)
        self.assertEqual(self.store.rows('SELECT state FROM calls')[0]['state'],'unknown')
        self.assertIn('未重复提交',self.store.response()['coverage_gap'])

    def test_new_conversation_progress_changes_input_without_replaying_unknown(self):
        self.seed();self.fail_model();old=self.store.rows('SELECT id,detail FROM calls')[0]
        self.add('user','有一份新测量结果，需要重新检查两种解释。');self.scan()
        result,seen=self.run_not_found_batch();self.assertEqual(len(seen[0]['current_contexts'][0]['turns']),1)
        calls=self.store.rows('SELECT state,detail FROM calls ORDER BY created,id')
        self.assertEqual(len(calls),2);self.assertEqual({c['state'] for c in calls},{'settled','unknown'})
        self.assertEqual(len({json.loads(c['detail'])['input_fingerprint'] for c in calls}),2)
        self.assertEqual(self.store.rows('SELECT state FROM calls WHERE id=?',(old['id'],))[0]['state'],'unknown')

    def test_failed_batch_does_not_starve_other_pending_context(self):
        self.seed();self.fail_model()
        original=self.store.rows('SELECT id,processed_seq FROM contexts')[0]
        path=self.path.with_name('another.jsonl')
        lines=[{'type':'session_meta','payload':{'id':'another-synthetic-fixture','source':'cli'}},
               {'timestamp':self.now,'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'新的独立讨论有明确限制条件。'}]}}]
        path.write_text(''.join(packed(x)+'\n' for x in lines));path.chmod(0o600);self.scan()
        # Same failed batch (including history) is excluded before later work.
        self.store.conn.execute('UPDATE contexts SET updated=? WHERE id=?',(self.now-1,original['id']))
        real_payload=analysis.payload
        def one_at_a_time(*args,**kw):
            data,known,through=real_payload(*args,**kw)
            if not kw.get('exclude_contexts'):
                data['current_contexts']=data['current_contexts'][:1]
                through={k:v for k,v in through.items() if k==original['id']}
                data['coverage']['complete_current']=1;data['coverage']['total_discussions']=1
            return data,known,through
        # Record the exact selected batch; do not change its watermark.
        with patch.object(analysis,'payload',side_effect=one_at_a_time):
            data,known,through=one_at_a_time(self.store,self.now)
            key=analysis.digest([analysis.SYSTEM+'\n'+packed(data),analysis.SCHEMA])
            self.store.conn.execute('UPDATE calls SET detail=?',(packed({'input_fingerprint':key,'input_ranges':analysis.input_ranges(data,known),'input_through':through}),))
            def invoke(feedback,prompt,schema,**kw):
                data=json.loads(prompt[len(analysis.SYSTEM)+1:]);self.assertNotIn(original['id'],[c['context_id'] for c in data['current_contexts']])
                rid=feedback.reserve('analysis');kw['on_reserved'](rid);feedback.settle(rid,'settled')
                result={'contexts':[{'context_id':c['context_id'],'summary':'独立讨论已读。','evidence':[]} for c in data['current_contexts']],
                    'insight':{'related_insight':'','judgment':'not_found',**{k:'' for k in analysis.FIELDS},'evidence':[]},'reviews':[]}
                return result,rid
            with patch.object(analysis.model,'invoke',side_effect=invoke):analysis.analyze(self.store,self.feedback,self.now)
        self.assertEqual(self.store.rows('SELECT processed_seq FROM contexts WHERE id=?',(original['id'],))[0]['processed_seq'],0)
        self.assertEqual(len(self.feedback.rows('SELECT * FROM budget')),2)

    def test_legacy_private_input_is_hashed_without_adopting_result_or_spending(self):
        self.seed();self.fail_model();call=self.store.rows('SELECT * FROM calls')[0]
        data,_,_=analysis.payload(self.store,self.now);run=self.feedback.root/'runs'/call['id']/'1234567890123456789';run.mkdir(parents=True,mode=0o700)
        for directory in (run.parent.parent,run.parent):directory.chmod(0o700)
        for name,value in [('input.txt',analysis.SYSTEM+'\n'+packed(data)),('output-schema.json',packed(analysis.SCHEMA))]:
            (run/name).write_text(value);(run/name).chmod(0o600)
        self.store.conn.execute('UPDATE calls SET detail=? WHERE id=?',(packed({'reason':'RuntimeError'}),call['id']))
        budgets=self.feedback.rows('SELECT * FROM budget')
        with patch.object(analysis.model,'invoke',side_effect=AssertionError('legacy must not resend')):
            self.assertEqual(analysis.analyze(self.store,self.feedback,self.now)['reason'],'prior_input_unresolved')
        self.assertEqual(self.feedback.rows('SELECT * FROM budget'),budgets)
        self.assertEqual(self.store.rows('SELECT state FROM calls')[0]['state'],'unknown')
        self.assertEqual(json.loads(self.store.rows('SELECT detail FROM calls')[0]['detail'])['reason'],'RuntimeError')

    def test_unverifiable_legacy_input_fails_closed_before_model(self):
        self.seed();self.fail_model();self.store.conn.execute('UPDATE calls SET detail=?',(packed({'reason':'RuntimeError'}),))
        with patch.object(analysis.model,'invoke',side_effect=AssertionError('unverified legacy')):
            self.assertEqual(analysis.analyze(self.store,self.feedback,self.now)['reason'],'prior_input_unverifiable')
        self.assertEqual(len(self.feedback.rows('SELECT * FROM budget')),1)

    def test_validation_failed_same_input_is_not_retried_or_weakened(self):
        self.seed();r,_,_,_=self.result();r['insight']['evidence'][0]['quote']='不存在的引文'
        with self.assertRaises(ValueError):self.run_model(r)
        old=self.feedback.rows('SELECT * FROM budget')
        with patch.object(analysis.model,'invoke',side_effect=AssertionError('do not retry invalid output')):
            self.assertEqual(analysis.analyze(self.store,self.feedback,self.now)['reason'],'prior_input_unresolved')
        self.assertEqual(self.feedback.rows('SELECT * FROM budget'),old)
        self.assertFalse(self.store.rows('SELECT * FROM insights'))
        self.assertEqual(json.loads(self.store.rows('SELECT detail FROM calls')[0]['detail'])['code'],'reflection_quote_not_in_source')

    def legacy_run(self):
        self.seed();self.fail_model();call=self.store.rows('SELECT * FROM calls')[0]
        data,_,_=analysis.payload(self.store,self.now);run=self.feedback.root/'runs'/call['id']/'1234567890123456789';run.mkdir(parents=True,mode=0o700)
        for directory in (run.parent.parent,run.parent):directory.chmod(0o700)
        for name,value in [('input.txt',analysis.SYSTEM+'\n'+packed(data)),('output-schema.json',packed(analysis.SCHEMA))]:
            (run/name).write_text(value);(run/name).chmod(0o600)
        self.store.conn.execute('UPDATE calls SET detail=?',(packed({'reason':'RuntimeError'}),))
        return call,run

    def test_owner_stop_before_legacy_gate_prevents_original_read(self):
        self.legacy_run();real_gate=analysis.gate
        def stop_at_gate(*args,**kwargs):
            self.store.set('enabled',False)
            return real_gate(*args,**kwargs)
        with patch.object(analysis,'gate',side_effect=stop_at_gate),patch.object(analysis,'legacy_input_fingerprint',side_effect=AssertionError('read after stop')):
            with self.assertRaisesRegex(PermissionError,'not_active'):
                analysis.submitted_inputs(self.store,self.feedback,self.store.enabled)
        self.assertNotIn('input_fingerprint',json.loads(self.store.rows('SELECT detail FROM calls')[0]['detail']))

    def test_legacy_alias_swap_is_rejected_on_actual_open(self):
        _,run=self.legacy_run();outside=self.root/'unrelated-private.txt';outside.write_text('outside authorized request');outside.chmod(0o600)
        real_open=os.open;real_read=os.read
        def swap_on_open(path,flags,*args,**kwargs):
            if path=='input.txt':
                (run/path).unlink();(run/path).symlink_to(outside)
            return real_open(path,flags,*args,**kwargs)
        with patch.object(analysis.os,'open',side_effect=swap_on_open),patch.object(analysis.os,'read',wraps=real_read) as reads:
            self.assertIsNone(analysis.submitted_inputs(self.store,self.feedback,self.store.enabled))
            self.assertEqual(reads.call_count,0)
        self.assertNotIn('input_fingerprint',json.loads(self.store.rows('SELECT detail FROM calls')[0]['detail']))

    def test_prior_rechecked_inside_model_lock_before_budget_admission(self):
        self.seed();self.feedback.set('model_capability',{'enabled':True})
        self.feedback.set('config',{'enabled':True,'codex_binary':sys.executable})
        real_invoke=analysis.model.invoke
        def another_failure_before_lock(feedback,prompt,schema,**kwargs):
            # Another analyzer failed after this analyzer computed its prior.
            ident=feedback.reserve('analysis');feedback.settle(ident,'unknown')
            data,known,through=analysis.payload(self.store,self.now)
            self.store.conn.execute('INSERT INTO calls VALUES(?,?,?,?)',(ident,'unknown',self.now,packed({'input_fingerprint':digest([prompt,schema]),'input_ranges':analysis.input_ranges(data,known),'input_through':through})))
            return real_invoke(feedback,prompt,schema,**kwargs)
        with patch.object(analysis.model,'invoke',side_effect=another_failure_before_lock),patch.object(analysis.model.subprocess,'Popen',side_effect=AssertionError('duplicate process')):
            result=analysis.analyze(self.store,self.feedback,self.now)
        self.assertEqual(result['reason'],'reflection_input_duplicate')
        self.assertEqual(len(self.feedback.rows('SELECT * FROM budget')),1)
        self.assertEqual(len(self.store.rows('SELECT * FROM calls')),1)

    def test_published_insight_does_not_replace_input_fingerprint(self):
        self.seed();result,data,_,_=self.result()
        expected=digest([analysis.SYSTEM+'\n'+packed(data),analysis.SCHEMA]);self.run_model(result)
        call=json.loads(self.store.rows('SELECT detail FROM calls')[0]['detail'])
        self.assertEqual(call['input_fingerprint'],expected)
        self.assertNotEqual(call['input_fingerprint'],self.store.rows('SELECT fingerprint FROM insights')[0]['fingerprint'])

    def test_five_failed_contexts_allow_sixth_context_without_payload_mock(self):
        self.seed()
        for i in range(4):
            path=self.path.with_name('context-'+str(i)+'.jsonl')
            lines=[{'type':'session_meta','payload':{'id':'synthetic-context-'+str(i),'source':'cli'}},
                   {'timestamp':self.now,'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'独立讨论的已知条件。'}]}}]
            path.write_text(''.join(packed(x)+'\n' for x in lines));path.chmod(0o600)
        self.scan();self.assertEqual(len(analysis.payload(self.store,self.now)[2]),5);self.fail_model()
        original=self.store.rows('SELECT id,processed_seq FROM contexts')
        path=self.path.with_name('sixth.jsonl')
        lines=[{'type':'session_meta','payload':{'id':'synthetic-sixth-context','source':'cli'}},
               {'timestamp':self.now+1,'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'第六段新讨论，需要检查独立限制。'}]}}]
        path.write_text(''.join(packed(x)+'\n' for x in lines));path.chmod(0o600);self.scan()
        def invoke(feedback,prompt,schema,**kw):
            data=json.loads(prompt[len(analysis.SYSTEM)+1:]);self.assertEqual(len(data['current_contexts']),1)
            self.assertNotIn(data['current_contexts'][0]['context_id'],{c['id'] for c in original})
            key=feedback.reserve('analysis');kw['on_reserved'](key);feedback.settle(key,'settled')
            result={'contexts':[{'context_id':data['current_contexts'][0]['context_id'],'summary':'新的独立讨论。','evidence':[]}],
                'insight':{'related_insight':'','judgment':'not_found',**{k:'' for k in analysis.FIELDS},'evidence':[]},'reviews':[]}
            return result,key
        with patch.object(analysis.model,'invoke',side_effect=invoke):self.assertEqual(analysis.analyze(self.store,self.feedback,self.now)['processed'],1)
        self.assertEqual([c for c in self.store.rows('SELECT id,processed_seq FROM contexts') if c['id'] in {o['id'] for o in original}],original)
        self.assertEqual(len(self.feedback.rows('SELECT * FROM budget')),2)

    def test_new_assistant_progress_can_run_with_old_unknown_preserved(self):
        self.seed();self.fail_model();self.add('assistant','补上此前漏答的可检查证据与下一步。');self.scan()
        result,seen=self.run_not_found_batch();self.assertEqual(len(seen[0]['current_contexts'][0]['turns']),1)
        self.assertEqual({c['state'] for c in self.store.rows('SELECT state FROM calls')},{'unknown','settled'})
        self.assertEqual(len(self.feedback.rows('SELECT * FROM budget')),2)

    def run_not_found_batch(self,with_quotes=False):
        seen=[]
        def invoke(feedback,prompt,schema,**kw):
            data=json.loads(prompt[len(analysis.SYSTEM)+1:]);seen.append(data)
            key=feedback.reserve('analysis');kw['on_reserved'](key);feedback.settle(key,'settled')
            result={'contexts':[{'context_id':c['context_id'],'summary':'仅记录本批可见进展。','evidence':[{'turn_id':t['turn_id'],'quote':t['text'][:30],'attribution':'user_statement' if t['role']=='user' else 'assistant_answer'} for t in c['turns'][:1]] if with_quotes else []} for c in data['current_contexts']],
                'insight':{'related_insight':'','judgment':'not_found',**{k:'' for k in analysis.FIELDS},'evidence':[]},'reviews':[]}
            return result,key
        with patch.object(analysis.model,'invoke',side_effect=invoke):result=analysis.analyze(self.store,self.feedback,self.now)
        return result,seen

    def test_unknown_long_prefix_allows_new_tail_without_false_coverage(self):
        self.add('user','甲'*25000);self.scan();self.fail_model()
        original=json.loads(self.store.rows('SELECT detail FROM calls')[0]['detail'])['input_through']
        self.add('assistant','此前漏答，现在给出可检查的下一步。');self.scan()
        result,seen=self.run_not_found_batch();part=seen[0]['current_contexts'][0]
        self.assertEqual(result['processed'],1);self.assertTrue(part['missing_prefix']);self.assertFalse(part['complete'])
        selected=[self.store.rows('SELECT seq FROM turns WHERE id=?',(t['turn_id'],))[0]['seq'] for t in part['turns']]
        self.assertGreater(min(selected),original[part['context_id']])
        self.assertEqual(self.store.rows('SELECT processed_seq FROM contexts')[0]['processed_seq'],0)
        self.assertFalse(analysis.ready(self.store));self.assertFalse(self.store.rows('SELECT * FROM insights'))
        old=self.feedback.rows('SELECT * FROM budget')
        self.store.conn.close();self.store=Store(self.root/'reflection')
        result,seen=self.run_not_found_batch();self.assertFalse(seen);self.assertEqual(self.feedback.rows('SELECT * FROM budget'),old)
        self.add('user','新的证据已经到齐，下一步检查实施结果。');self.scan()
        result,seen=self.run_not_found_batch();self.assertEqual(result['processed'],1)
        self.assertEqual(len(seen[0]['current_contexts'][0]['turns']),1)
        self.assertFalse(seen[0]['current_contexts'][0]['complete'])
        self.assertEqual(self.store.rows('SELECT processed_seq FROM contexts')[0]['processed_seq'],0)
        self.assertEqual(len(self.store.rows("SELECT * FROM calls WHERE state='unknown'")),1)

    def test_unknown_prefix_outside_window_does_not_permanently_block_context(self):
        self.add('user','甲'*25000,13*86400);self.scan();self.fail_model()
        self.add('assistant','新进展。');self.scan();self.run_not_found_batch()
        future=self.now+2*86400
        self.lines.append({'timestamp':future,'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'当前窗口的新证据。'}]}})
        self.path.write_text(''.join(packed(x)+'\n' for x in self.lines));self.path.chmod(0o600)
        sources.scan(self.store,self.feedback,future)
        with patch.object(analysis.time,'time',return_value=future):
            data,_,_=analysis.payload(self.store,future)
        self.assertTrue(data['current_contexts'][0]['complete']);self.assertNotIn('missing_prefix',data['current_contexts'][0])
        self.assertTrue(all(t['text']!='甲'*6000 for t in data['current_contexts'][0]['turns']))
        self.now=future
        with patch.object(analysis.time,'time',return_value=future):
            self.run_not_found_batch();self.assertFalse(analysis.ready(self.store))
        self.assertFalse(self.store.get('replay_gap'))
        self.assertEqual(len(self.store.rows("SELECT * FROM calls WHERE state='unknown'")),1)

    def test_exhausted_unknown_prefixes_do_not_starve_sixth_context(self):
        for i in range(5):
            path=self.path.with_name('long-'+str(i)+'.jsonl')
            lines=[{'type':'session_meta','payload':{'id':'synthetic-long-'+str(i),'source':'cli'}},
                {'timestamp':self.now,'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'甲'*25000}]}}]
            path.write_text(''.join(packed(x)+'\n' for x in lines));path.chmod(0o600)
        self.scan()
        # Each unknown prefix is preserved, but its distinct tail can be read.
        for _ in range(5):self.fail_model();self.run_not_found_batch()
        self.assertEqual(len(self.store.rows("SELECT * FROM calls WHERE state='unknown'")),5)
        self.assertFalse(analysis.ready(self.store))
        self.add('user','第六段独立讨论的新条件。');self.scan()
        result,seen=self.run_not_found_batch();self.assertEqual(result['processed'],1)
        self.assertEqual(len(seen[0]['current_contexts']),1);self.assertTrue(seen[0]['current_contexts'][0]['complete'])
        self.assertEqual(len(self.store.rows("SELECT * FROM calls WHERE state='unknown'")),5)

    def test_unknown_gap_tail_cannot_publish_and_rewrite_invalidates_cursor(self):
        self.add('user','甲'*25000);self.scan();self.fail_model()
        self.add('user','我还需要两项标准。');self.add('user','我缺同一项标准。');self.scan()
        def invalid(feedback,prompt,schema,**kw):
            data=json.loads(prompt[len(analysis.SYSTEM)+1:]);part=data['current_contexts'][0]
            evidence=[{'turn_id':t['turn_id'],'quote':t['text'][:30],'attribution':'user_statement'} for t in part['turns'][-2:]]
            key=feedback.reserve('analysis');kw['on_reserved'](key);feedback.settle(key,'settled')
            return {'contexts':[{'context_id':part['context_id'],'summary':'仅本批。','evidence':[]}],
                'insight':{'related_insight':'','judgment':'preliminary',**{k:'具体待核查的条件。' for k in analysis.FIELDS},'question':'我怎样确认标准？','evidence':evidence},'reviews':[]},key
        with patch.object(analysis.model,'invoke',side_effect=invalid):
            with self.assertRaises(ValueError):analysis.analyze(self.store,self.feedback,self.now)
        self.assertFalse(self.store.rows('SELECT * FROM insights'));self.assertFalse(self.store.rows('SELECT * FROM notices'))
        self.path.write_text(self.path.read_text().replace('甲','乙'));self.scan()
        data,_,_=analysis.payload(self.store,self.now)
        self.assertNotIn('missing_prefix',data['current_contexts'][0]);self.assertTrue(data['current_contexts'][0]['turns'][0]['text'].startswith('乙'))

    def test_metadata_reordering_cannot_replay_unknown_ranges(self):
        self.seed();path=self.path.with_name('other.jsonl')
        lines=[{'type':'session_meta','payload':{'id':'synthetic-other','source':'cli'}},
            {'timestamp':self.now,'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'另一段独立限制。'}]}}]
        path.write_text(''.join(packed(x)+'\n' for x in lines));path.chmod(0o600);self.scan();self.fail_model()
        original=self.store.rows('SELECT id,processed_seq FROM contexts');budgets=self.feedback.rows('SELECT * FROM budget')
        self.store.conn.execute('UPDATE contexts SET updated=updated+10 WHERE id=?',(original[0]['id'],))
        result,seen=self.run_not_found_batch();self.assertFalse(seen)
        self.assertEqual(self.feedback.rows('SELECT * FROM budget'),budgets)
        self.assertEqual(self.store.rows('SELECT id,processed_seq FROM contexts'),original)

    def test_unrelated_suppression_cannot_replay_unknown_ranges(self):
        self.seed();self.fail_model();budgets=self.feedback.rows('SELECT * FROM budget')
        row={'id':'synthetic-muted','state':'muted','judgment':'preliminary',**{k:'合成的无关抑制问题。' for k in analysis.FIELDS},'evidence':'[]','fingerprint':'synthetic-muted-hash','created':self.now,'updated':self.now}
        self.store.conn.execute('INSERT INTO insights('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',tuple(row.values()))
        result,seen=self.run_not_found_batch();self.assertFalse(seen);self.assertEqual(self.feedback.rows('SELECT * FROM budget'),budgets)

    def test_other_historical_summary_cannot_replay_unknown_ranges(self):
        self.seed();self.fail_model();path=self.path.with_name('history.jsonl')
        lines=[{'type':'session_meta','payload':{'id':'synthetic-history','source':'cli'}},
            {'timestamp':self.now,'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'另一段已有检查方法的讨论。'}]}}]
        path.write_text(''.join(packed(x)+'\n' for x in lines));path.chmod(0o600);self.scan()
        result,seen=self.run_not_found_batch(with_quotes=True);self.assertEqual(result['processed'],1)
        self.assertTrue(analysis.payload(self.store,self.now)[0]['historical_contexts'])
        budgets=self.feedback.rows('SELECT * FROM budget');result,seen=self.run_not_found_batch();self.assertFalse(seen)
        self.assertEqual(self.feedback.rows('SELECT * FROM budget'),budgets)

    def test_legacy_parser_migration_cannot_replay_same_visible_ranges(self):
        self.seed();self.fail_model();data,known,_=analysis.payload(self.store,self.now)
        ids={}
        for row in self.store.rows('SELECT * FROM turns'):
            ref=json.loads(row['reference'])
            for name in ('parser_version','reflection_root','observed','message_id'):ref.pop(name,None)
            old=digest([row['source_id'],row['session'],ref])[:32];ids[row['id']]=old
            self.store.conn.execute('UPDATE turns SET id=?,reference=? WHERE id=?',(old,packed(ref),row['id']))
        for c in data['current_contexts']:
            for t in c['turns']:t['turn_id']=ids[t['turn_id']]
        call=self.store.rows('SELECT * FROM calls')[0];run=self.feedback.root/'runs'/call['id']/'1234567890123456789';run.mkdir(parents=True,mode=0o700)
        for directory in (run.parent.parent,run.parent):directory.chmod(0o700)
        for name,value in [('input.txt',analysis.SYSTEM+'\n'+packed(data)),('output-schema.json',packed(analysis.SCHEMA))]:(run/name).write_text(value);(run/name).chmod(0o600)
        self.store.conn.execute('UPDATE calls SET detail=?',(packed({'reason':'RuntimeError'}),))
        self.store.set('source_parser_version',1);self.scan()
        self.assertFalse(set(ids.values())&{r['id'] for r in self.store.rows('SELECT * FROM turns')})
        budgets=self.feedback.rows('SELECT * FROM budget');result,seen=self.run_not_found_batch();self.assertFalse(seen)
        self.assertEqual(result['reason'],'prior_input_unresolved');self.assertEqual(self.feedback.rows('SELECT * FROM budget'),budgets)
        self.assertEqual(self.store.rows('SELECT processed_seq FROM contexts')[0]['processed_seq'],0)

    def test_unknown_summary_completion_cannot_replay_with_metadata_change(self):
        self.seed()
        for _ in range(1501):self.lines.append({'type':'synthetic_tool_event'})
        self.path.write_text(''.join(packed(x)+'\n' for x in self.lines));self.path.chmod(0o600)
        sources.scan(self.store,self.feedback,self.now)
        self.run_not_found_batch(with_quotes=True);self.scan()
        data,_,_=analysis.payload(self.store,self.now)
        self.assertEqual(data['current_contexts'][0]['turns'],[]);self.assertTrue(data['current_contexts'][0]['complete'])
        self.fail_model();budgets=self.feedback.rows('SELECT * FROM budget')
        row={'id':'synthetic-completion-muted','state':'muted','judgment':'preliminary',**{k:'无关的合成抑制。' for k in analysis.FIELDS},'evidence':'[]','fingerprint':'synthetic-completion-hash','created':self.now,'updated':self.now}
        self.store.conn.execute('INSERT INTO insights('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',tuple(row.values()))
        result,seen=self.run_not_found_batch();self.assertFalse(seen);self.assertEqual(self.feedback.rows('SELECT * FROM budget'),budgets)
        self.assertEqual(result['reason'],'prior_input_unresolved')
        self.add('assistant','实际补上此前漏答的新检查方法。');self.scan()
        result,seen=self.run_not_found_batch();self.assertEqual(result['processed'],1)
        self.assertEqual(len(seen[0]['current_contexts'][0]['turns']),1)

    def test_source_tail_backfill_cannot_publish_partial_context(self):
        self.seed();registered=self.store.rows('SELECT * FROM sources')[0]
        # A tail beyond the current per-scan limit contains the resolving answer.
        for _ in range(1501):self.lines.append({'type':'synthetic_tool_event'})
        self.add('assistant','前面的疑问已经解决，可以按明确标准执行。')
        sources.scan(self.store,self.feedback,self.now)
        r,data,known,through=self.result('not_found');self.assertFalse(data['current_contexts'][0]['complete'])
        self.run_model(r);self.assertFalse(analysis.ready(self.store))
        self.scan();self.assertTrue(analysis.ready(self.store))
        r,data,known,through=self.result('not_found');self.assertTrue(data['current_contexts'][0]['complete'])
        self.run_model(r);self.assertFalse(analysis.ready(self.store))

    def test_appended_tail_during_model_blocks_publication(self):
        self.seed();r,_,_,_=self.result()
        with self.assertRaisesRegex(ValueError,'tail_changed'):
            self.run_model(r,lambda:self.add('assistant','新答复澄清了此前的条件。'))
        self.assertEqual(self.store.rows('SELECT * FROM notices'),[])

    def test_updated_hypothesis_preserves_old_version(self):
        self.seed();r,_,_,_=self.result();self.run_model(r);old=self.store.rows('SELECT * FROM insights')[0]
        self.add('user','我仍缺同一项标准，还需明确验证条件。');self.scan()
        r,data,known,through=self.result();r['insight']['related_insight']=old['id']
        prior=next(e for e in json.loads(old['evidence']) if e['attribution']=='user_statement');r['insight']['evidence'].append({'turn_id':prior['id'],'quote':prior['quote'],'attribution':prior['attribution']});self.run_model(r)
        versions=[json.loads(h['detail']) for h in self.store.rows("SELECT * FROM history WHERE event='prior_version'")]
        self.assertEqual(versions[0]['evidence'],old['evidence']);self.assertEqual(versions[0]['question'],old['question'])

    def test_more_than_thirty_muted_issues_remain_in_suppression(self):
        self.seed();r,_,_,_=self.result();self.run_model(r);old=self.store.rows('SELECT * FROM insights')[0]
        record_feedback(self.store,{'id':old['id'],'revision':old['revision'],'choice':'mute'})
        for i in range(35):
            row={**old,'id':'muted-'+str(i),'state':'muted','question':'我怎样判断合成问题'+str(i)+'？','fingerprint':'unique-'+str(i),'updated':self.now+i}
            self.store.conn.execute('INSERT INTO insights('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',tuple(row.values()))
        self.add('user','讨论出现新的条件。');self.scan();data,_,_=analysis.payload(self.store)
        self.assertIn(old['id'],{i['id'] for i in data['previous_insights']});self.assertTrue(data['suppression_complete'])

    def test_credential_families_and_quoted_keys_are_redacted(self):
        samples=['refresh_token: synthetic-refresh-only','"api_key":"synthetic-key-only"','authorization: Basic synthetic-value-only','xoxb-'+('fictional'*4),'glpat-'+('fictional'*4),'id_token: synthetic-id-only','ASIA'+('Z'*16),'gho_'+('fictional'*5)]
        for value in samples:
            cleaned=sources.visible_text(value);self.assertIn('[凭据已拦截]',cleaned);self.assertNotIn(value,cleaned)
            for segment in value.replace(':',' ').split():
                if segment.startswith(('synthetic-','xoxb-','glpat-','ASIA','gho_')):self.assertNotIn(segment,cleaned)

    def test_quote_continuations_inline_code_html_not_owner_statements(self):
        self.add('user','> 第三方原话：\n我缺少判断标准。\n\n这是转述材料。\n`我不行动`\n<blockquote>我反复比较</blockquote>');self.scan();data,known,_=analysis.payload(self.store)
        row=data['current_contexts'][0]['turns'][0]
        for text in ['我缺少判断标准','我不行动','我反复比较']:
            with self.assertRaisesRegex(ValueError,'material'):analysis.quote(self.store,{'turn_id':row['turn_id'],'quote':text,'attribution':'user_statement'},known)

    def test_source_path_switch_drops_only_derived_pending_input(self):
        self.seed();old_path=self.path
        new=self.root/'new-codex';new.mkdir();self.feedback.conn.execute('UPDATE sources SET path=? WHERE id=?',(str(new),'codex'))
        self.scan();self.assertFalse(analysis.ready(self.store));self.assertTrue(old_path.exists());self.assertEqual(self.store.rows('SELECT * FROM turns'),[])

    def test_uncited_answer_same_length_edit_blocks_complete(self):
        self.seed();self.add('assistant','已有的标准甲。');self.scan();r,_,_,_=self.result()
        def rewrite():self.path.write_text(self.path.read_text().replace('已有的标准甲','已有的标准乙'))
        with self.assertRaises(ValueError):self.run_model(r,rewrite)
        self.assertEqual(self.store.rows('SELECT * FROM notices'),[])

    def test_suppression_overflow_never_publishes_or_displays_hypothesis(self):
        self.seed();r,_,_,_=self.result();self.run_model(r);old=self.store.rows('SELECT * FROM insights')[0]
        for i in range(80):
            row={**old,'id':'muted-long-'+str(i),'state':'muted','question':'我'+('甲'*598),'pattern':'乙'*600,'fingerprint':'long-'+str(i),'updated':self.now+i}
            self.store.conn.execute('INSERT INTO insights('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',tuple(row.values()))
        self.add('user','新的讨论条件。');self.scan();r,data,known,through=self.result('not_found')
        self.assertFalse(data['suppression_complete']);r['insight']['explanation']='禁止展示的假设。';self.run_model(r)
        latest=self.store.get('last_analysis');self.assertTrue(latest['publication_deferred']);self.assertNotIn('禁止展示',latest['explanation']);self.assertEqual(len(self.store.rows('SELECT * FROM notices')),1)

    def test_historical_summary_does_not_reintroduce_expired_quotes(self):
        self.seed();r,_,_,_=self.result();self.run_model(r)
        row=self.store.rows('SELECT * FROM contexts')[0];summary=json.loads(row['summary'])
        for evidence in summary['evidence']:evidence['observed']=self.now-15*86400
        self.store.conn.execute('UPDATE contexts SET summary=? WHERE id=?',(packed(summary),row['id']))
        data,_,_=analysis.payload(self.store,self.now);self.assertEqual(data['historical_contexts'],[])

    def test_hermes_reads_only_recent_visible_roles(self):
        path=self.root/'hermes-window.sqlite'
        with sqlite3.connect(path) as db:
            db.executescript('CREATE TABLE sessions(id,source,user_id);CREATE TABLE messages(id INTEGER,session_id,role,content,timestamp);')
            db.execute('INSERT INTO sessions VALUES(?,?,?)',('session','cli',''))
            for i,role,age in [(1,'user',15*86400),(2,'tool',0),(3,'user',0),(4,'assistant',0)]:db.execute('INSERT INTO messages VALUES(?,?,?,?,?)',(i,'session',role,'recent' if i>2 else 'must not be read',self.now-age))
        path.chmod(0o600);self.feedback.conn.execute('INSERT INTO sources(id,platform,path) VALUES(?,?,?)',('hermes','hermes',str(path)));self.scan()
        turns=self.store.rows("SELECT * FROM turns WHERE source_id='hermes'");self.assertEqual([r['role'] for r in turns],['user','assistant']);self.assertTrue(sources.context_complete(self.store,turns[0]['context_id']))

    def test_read_interface_does_not_create_database_or_invoke(self):
        missing=self.root/'absent';result=subprocess.run([sys.executable,'-B','-m','activity_mvp.reflection.cli','status','--root',str(missing)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0);self.assertEqual(json.loads(result.stdout)['status'],'not_enabled');self.assertFalse(missing.exists())

    def test_parser_migration_preserves_old_evidence_and_notice_identity(self):
        self.seed();result,_,_,_=self.result();self.run_model(result)
        old=self.store.rows('SELECT * FROM insights')[0]
        evidence=json.loads(old['evidence'])
        for e in evidence:e.pop('message_id',None);e.pop('context_version',None);e.pop('context_id',None)
        self.store.conn.execute('UPDATE insights SET evidence=?',(packed(evidence),))
        old_ids={}
        for row in self.store.rows('SELECT * FROM turns'):
            ref=json.loads(row['reference']);ref.pop('parser_version',None);ref.pop('reflection_root',None);ref.pop('observed',None)
            old_id=digest([row['source_id'],row['session'],ref])[:32];old_ids[row['id']]=old_id
            self.store.conn.execute('UPDATE turns SET id=?,reference=? WHERE id=?',(old_id,packed(ref),row['id']))
        for e in evidence:e['id']=old_ids[e['id']]
        self.store.conn.execute('UPDATE insights SET evidence=?',(packed(evidence),))
        self.assertEqual(next(i for i in self.store.response(False)['insights'] if i['id']==old['id'])['state'],'withdrawn')
        # Put the original issue beyond the old 30-record search window.
        for i in range(32):
            row={**old,'id':'different-'+str(i),'fingerprint':'different-'+str(i),'evidence':'[]','updated':self.now+i+1}
            self.store.conn.execute('INSERT INTO insights('+','.join(row)+') VALUES('+','.join('?' for _ in row)+')',tuple(row.values()))
        self.store.set('source_parser_version',1)
        self.scan()
        self.assertEqual(len(self.store.rows('SELECT * FROM retired_turns')),3)
        self.assertTrue(all(json.loads(r['reference'])['parser_version']==2 for r in self.store.rows('SELECT * FROM turns')))
        result,_,_,_=self.result();result['insight']['question']='我换一种说法还要问同一件事吗？';self.run_model(result)
        self.assertEqual(len(self.store.rows('SELECT * FROM notices')),1)
        self.assertEqual(len(self.store.rows('SELECT * FROM insights')),33)
        self.assertFalse(set(old_ids.values())&{r['id'] for r in self.store.rows('SELECT * FROM turns')})

    def test_material_with_unquoted_explicit_owner_switch(self):
        self.add('user','这是AI写的草稿，不代表我的自述："我没有标准，一直比较。" 我自己的问题是：我已有明确标准，明天开始使用。')
        self.scan();_,data,known,_=self.result('not_found');ident=data['current_contexts'][0]['turns'][0]['turn_id']
        def q(text):return sources.packed(analysis.quote(self.store,{'turn_id':ident,'quote':text,'attribution':'user_statement'},known))
        self.assertIn('user_statement',q('我已有明确标准，明天开始使用。'))
        with self.assertRaisesRegex(ValueError,'material'):q('我没有标准，一直比较。')

    def test_off_on_at_reference_gate_never_reads_original(self):
        self.seed();row=self.store.rows('SELECT * FROM turns')[0];ref=json.loads(row['reference'])
        original=sources.gate
        from contextlib import contextmanager
        @contextmanager
        def interrupted(root,check=None):
            self.store.set('enabled',False);self.store.set('authorization_epoch',1)
            self.store.set('enabled',True);self.store.set('authorization_epoch',2)
            with original(root,check):yield
        with patch.object(sources,'gate',interrupted),patch.object(sources,'_reference_current') as read:
            self.assertFalse(sources.reference_current(ref,store=self.store));read.assert_not_called()

    def test_mute_between_notice_selection_and_claim_prevents_notification(self):
        self.seed();result,_,_,_=self.result();self.run_model(result)
        insight=self.store.rows('SELECT * FROM insights')[0];original=service.gate
        from contextlib import contextmanager
        @contextmanager
        def muted(root,check=None):
            record_feedback(self.store,{'id':insight['id'],'revision':insight['revision'],'choice':'mute'})
            with original(root,check):yield
        with patch.object(service,'gate',muted),patch.object(service.subprocess,'Popen') as submit:
            self.assertEqual(service.notify(self.feedback)['requested'],0);submit.assert_not_called()
        self.assertEqual(self.store.rows('SELECT * FROM insights')[0]['state'],'muted')

    def test_quoted_owner_switch_and_unclosed_quotes_remain_material(self):
        for opening,closing in [('‘','’'),('“','”'),('「','」'),('『','』'),("'","'"),('"','"'),('‘',''),('“',''),("'",''),('"','')]:
            with self.subTest(opening=opening,closing=closing):
                text='这是AI写的草稿，不代表我的自述：'+opening+'我自己的问题是：我不想决定。'+closing
                position=text.index('我不想决定。')
                self.assertTrue(any(a<=position<b for a,b in sources.material_ranges(text)))

    def test_new_assistant_evidence_updates_same_issue(self):
        self.seed();r,_,_,_=self.result();self.run_model(r);old=self.store.rows('SELECT * FROM insights')[0]
        self.add('assistant','此前遗漏了检验标准：请先检查两项可观测结果。');self.scan()
        r,data,known,through=self.result();r['insight']['related_insight']=old['id']
        for prior in json.loads(old['evidence']):
            if prior['role']=='user':r['insight']['evidence'].append({'turn_id':prior['id'],'quote':prior['quote'],'attribution':'user_statement'})
        self.run_model(r)
        updated=self.store.rows('SELECT * FROM insights WHERE id=?',(old['id'],))[0]
        self.assertEqual(updated['state'],'active');self.assertGreater(updated['revision'],old['revision'])
        self.assertTrue(any('此前遗漏' in e['quote'] for e in json.loads(updated['evidence'])))

if __name__=='__main__':unittest.main()
