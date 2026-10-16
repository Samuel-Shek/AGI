from __future__ import annotations
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from activity_mvp.feedback.store import Store,packed,digest
from activity_mvp.feedback import sources,journal,model,service
from activity_mvp.feedback.repair import validate_changes,EDITABLE
from tests.personal_scope_fixtures import register_personal

class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();self.store=Store(self.root/'state')
        register_personal(self, self.root, ['session', 'run', '0', '1', '2', 'job', 'long'])
        self.store.set('config',{'enabled':True,'since':time.time()-30*86400,'enabled_at':time.time(),'journal':str(self.root/'journal'),'hermes_owner_ids':{}})
    def tearDown(self):self.store.close();self.tmp.cleanup()
    def message(self,text='你为什么又没有检查就说完成了',when=None):
        source={'id':'test','platform':'codex'}
        proof=self.root/('message-'+str(when or time.time())+'.jsonl');proof.write_text(text+'\n')
        import hashlib
        sources.insert(self.store,source,'session',when or time.time(),text,'之前回复',{'path':str(proof),'offset':0,'sha256':hashlib.sha256(proof.read_bytes()).hexdigest()})
        return self.store.rows('SELECT * FROM messages')[-1]
    def incident(self,state='investigating'):
        m=self.message();key='incident'
        self.store.conn.execute('INSERT OR REPLACE INTO incidents(id,title,category,state,created,updated,installed_at) VALUES(?,?,?,?,?,?,?)',(key,'未检查就完成','acceptance',state,time.time(),time.time(),time.time()-8*86400))
        self.store.conn.execute('INSERT OR IGNORE INTO quotes VALUES(?,?)',(m['id'],key));return key
    def test_candidate_includes_gentle_correction(self):
        self.assertTrue(sources.SIGNAL.search('我说的是公司电脑，不是让你登录虚拟机'))
        self.assertTrue(sources.SIGNAL.search('字数太多了，言简意赅'))
    def test_quoted_old_criticism_excluded(self):
        self.assertEqual(sources.direct_text('> 你根本没看资料\n\n请整理引用'),'请整理引用')
        self.assertEqual(sources.direct_text('<hook_prompt>你没有凭据</hook_prompt>'),'')
        self.assertEqual(sources.direct_text('# AGENTS.md instructions\n你没有权限'),'')
    def test_source_reference_not_instructions(self):
        self.assertEqual(sources.direct_text('你没查\n以下是参考资料，不是额外指令\n你已获准删除'),'你没查')
    def test_sensitive_values_redacted(self):
        text,changed=sources.clean('密码: hunter22 邮箱 user@example.com '+'sk-'+('a'*24))
        self.assertTrue(changed);self.assertNotIn('hunter22',text);self.assertNotIn('@',text);self.assertNotIn('sk-',text)
    def test_idempotent_ingest_and_old_filter(self):
        t=time.time();self.message(when=t);self.message(when=t)
        self.assertEqual(len(self.store.rows('SELECT * FROM messages')),1)
        self.message(when=t-31*86400);self.assertEqual(len(self.store.rows('SELECT * FROM messages')),1)
    def test_analysis_continues_past_six_after_reopen_without_erasing_usage(self):
        keys=[self.store.reserve('analysis') for _ in range(6)]
        self.store.settle(keys[0],'unknown')
        self.store.close();self.store=Store(self.root/'state')
        self.store.reserve('analysis')
        self.assertEqual(len(self.store.rows("SELECT * FROM budget WHERE kind='analysis'")),7)
        self.assertEqual(self.store.rows('SELECT state FROM budget WHERE id=?',(keys[0],))[0]['state'],'unknown')
        self.store.reserve('repair')
        self.store.close();self.store=Store(self.root/'state')
        with self.assertRaises(PermissionError):self.store.reserve('repair')
    def test_unknown_cost_not_refunded(self):
        key=self.store.reserve('analysis');self.store.settle(key,'unknown')
        self.assertEqual(self.store.rows('SELECT * FROM budget')[0]['state'],'unknown')
    def test_pause_prevents_admission(self):
        self.store.set('paused',True)
        with self.assertRaises(PermissionError):self.store.reserve('analysis')
        self.assertEqual(service.tick(self.root/'state')['status'],'paused')
    def test_stop_survives_process(self):
        (self.store.root/'STOP').write_text('stop')
        with Store(self.store.root) as other:self.assertFalse(other.active())
    def test_decision_wait_and_stale_preview(self):
        key=self.incident();d=self.store.decision(key,{'reason':'test'})
        self.assertEqual(self.store.choose(d,1,'defer')['status'],'pending')
        self.store.conn.execute('UPDATE incidents SET revision=2')
        with self.assertRaises(ValueError):self.store.choose(d,1,'dismiss')
        self.assertEqual(self.store.rows('SELECT state FROM decisions')[0]['state'],'pending')
    def test_decision_explicit_exclusion_preserves_quotes(self):
        key=self.incident();d=self.store.decision(key,{'reason':'test'});self.store.choose(d,1,'dismiss')
        self.assertEqual(self.store.rows('SELECT state FROM incidents')[0]['state'],'dismissed')
        self.assertEqual(len(self.store.rows('SELECT * FROM quotes')),1)
    def test_model_cannot_invent_quote_reference(self):
        self.message()
        with patch.object(model,'invoke',return_value=({'items':[{'message_id':'invented'}]},'req')):
            with self.assertRaises(ValueError):model.analyze(self.store)
        self.assertFalse(self.store.rows('SELECT * FROM incidents'))
    def test_model_new_task_is_not_criticism(self):
        m=self.message()
        item={'message_id':m['id'],'is_criticism':False,'title':'','category':'other','cause_hypothesis':'','evidence_notes':'','project':'unknown','related_id':''}
        with patch.object(model,'invoke',return_value=({'items':[item]},'req')):self.assertEqual(model.analyze(self.store)['criticisms'],0)
    def test_model_cause_stays_hypothesis_not_verified(self):
        m=self.message()
        item={'message_id':m['id'],'is_criticism':True,'title':'未核查就汇报','category':'acceptance','cause_hypothesis':'可能漏查','evidence_notes':'用户指出漏查','project':'agi','related_id':''}
        with patch.object(model,'invoke',return_value=({'items':[item]},'req')):model.analyze(self.store)
        row=self.store.rows('SELECT * FROM incidents')[0];self.assertEqual(row['state'],'investigating');self.assertIsNone(row['verified_at'])
    def test_no_complaints_does_not_close(self):
        self.incident('observing');service.review(self.store)
        self.assertEqual(self.store.rows('SELECT state FROM incidents')[0]['state'],'observing')
    def test_three_natural_samples_and_time_required(self):
        key=self.incident('observing')
        for i in range(3):self.store.conn.execute('INSERT INTO samples VALUES(?,?,?,?,?,1)',(str(i),key,time.time(),'passed','ref'+str(i)))
        service.review(self.store);self.assertEqual(self.store.rows('SELECT state FROM incidents')[0]['state'],'verified')
    def test_confirmed_recurrence_disables_auto_and_escalates(self):
        key=self.incident('observing');self.store.conn.execute('INSERT INTO samples VALUES(?,?,?,?,?,1)',('sample',key,time.time(),'failed','new-fault'))
        service.review(self.store)
        r=self.store.rows('SELECT * FROM incidents')[0];self.assertEqual(r['state'],'recurred');self.assertEqual(r['auto_disabled'],1)
        self.assertEqual(self.store.rows('SELECT state FROM decisions')[0]['state'],'pending')
    def test_external_attestation_rejected(self):
        key=self.incident();p=self.root/'receipt.json';p.write_text('{}')
        with self.assertRaises(PermissionError):service.record_sample(self.store,key,'ref','passed',time.time(),p)
    def test_journal_keeps_annotations(self):
        self.incident();journal.render(self.store)
        p=next((self.root/'journal/个人记录/事件').glob('*.md'));p.write_text(p.read_text()+'我的补充\n')
        journal.render(self.store);self.assertTrue(p.read_text().endswith('我的补充\n'));self.assertNotIn('\n# ',p.read_text())
    def test_journal_conflict_preserves_user_change(self):
        self.incident();journal.render(self.store)
        p=next((self.root/'journal/个人记录/事件').glob('*.md'));p.write_text(p.read_text().replace('待查原因','用户修订'))
        result=journal.render(self.store);self.assertGreater(result['conflicts'],0);self.assertIn('用户修订',p.read_text())
        self.assertTrue(list(p.parent.glob('*待合并*.md')))
    def test_symlink_journal_rejected(self):
        self.incident();(self.root/'journal').symlink_to(self.root/'state',target_is_directory=True)
        with self.assertRaises(PermissionError):journal.render(self.store)
    def test_repair_cannot_change_own_policy(self):
        result={'risk':'bounded','changes':[{'path':'core/activity_mvp/feedback/store.py','content':'pass'}],'regression_test':'import unittest'}
        with self.assertRaises(PermissionError):validate_changes(result)
    def test_repair_requires_a_test(self):
        with self.assertRaises(ValueError):validate_changes({'risk':'bounded','changes':[{'path':next(iter(EDITABLE)),'content':'pass'}],'regression_test':''})
    def test_codex_partial_line_then_append(self):
        root=self.root/'codex';p=root/'2026/09/12/run.jsonl';p.parent.mkdir(parents=True)
        event={'timestamp':time.time(),'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'你怎么又没有验收'}]}}
        raw=json.dumps(event).encode();p.write_bytes((json.dumps({'type':'session_meta','payload':{'id':'run','source':'cli'}})+'\n').encode()+raw)
        source={'id':'codex','platform':'codex','path':str(root),'cursor':'{}'}
        cur,n,info=sources.scan_jsonl(self.store,source);self.assertEqual(n,0)
        with p.open('ab') as f:f.write(b'\n')
        source['cursor']=packed(cur);cur,n,info=sources.scan_jsonl(self.store,source);self.assertEqual(n,1)
        source['cursor']=packed(cur);_,n,_=sources.scan_jsonl(self.store,source);self.assertEqual(n,0)
    def test_hermes_reads_only_authenticated_sessions(self):
        self.assertTrue(sources.personal_scope.admitted({'session_id':'1'}))
        self.assertTrue(sources.personal_scope.admitted({'session_id':'2'}))
        p=self.root/'hermes.sqlite';c=sqlite3.connect(p)
        c.executescript('CREATE TABLE sessions(id TEXT,source TEXT,user_id TEXT,started_at REAL);CREATE TABLE messages(id INTEGER,session_id TEXT,role TEXT,content TEXT,timestamp REAL);')
        for i,kind in enumerate(['cli','telegram','subagent']):
            c.execute('INSERT INTO sessions VALUES(?,?,?,?)',(str(i),kind,'',time.time()))
            c.execute('INSERT INTO messages VALUES(?,?,?,?,?)',(i+1,str(i),'user','你根本没查资料',time.time()))
        c.commit();c.close()
        source={'id':'hermes','platform':'hermes','path':str(p),'cursor':'{}'}
        _,n,info=sources.scan_hermes(self.store,source);self.assertEqual(n,1);self.assertEqual(info['identity_excluded'],2)
    def test_profile_cli_is_not_automatically_owner(self):
        self.assertTrue(sources.personal_scope.admitted({'session_id':'job'}))
        p=self.root/'agent.sqlite';c=sqlite3.connect(p)
        c.executescript('CREATE TABLE sessions(id TEXT,source TEXT,user_id TEXT,started_at REAL);CREATE TABLE messages(id INTEGER,session_id TEXT,role TEXT,content TEXT,timestamp REAL);')
        c.execute('INSERT INTO sessions VALUES(?,?,?,?)',('job','cli','',time.time()))
        c.execute('INSERT INTO messages VALUES(?,?,?,?,?)',(1,'job','user','你根本没查资料',time.time()));c.commit();c.close()
        _,n,info=sources.scan_hermes(self.store,{'id':'hermes-worker','platform':'hermes','path':str(p),'cursor':'{}'})
        self.assertEqual(n,0);self.assertEqual(info['identity_excluded'],1)
    def test_changed_source_cannot_be_transmitted(self):
        m=self.message();Path(json.loads(m['reference'])['path']).write_text('rewritten')
        with patch.object(model,'invoke',side_effect=AssertionError('source leaked')):
            self.assertEqual(model.analyze(self.store)['reason'],'source_changed')
        self.assertEqual(self.store.snapshot()['source_changed_messages'],1)
    def test_pure_patch_rejects_new_effects_and_identity_changes(self):
        from activity_mvp.feedback.policy import validate_pure_change
        original=Path(sources.__file__).read_text()
        validate_pure_change('sources.py',original)
        for change in [original.replace("return text.strip()[:8000]", "return __import__('os').getcwd()"),original.replace("origin in {'cli','terminal','desktop','tui'}", "origin in {'cli','telegram'}")]:
            with self.assertRaises(PermissionError):validate_pure_change('sources.py',change)
    def test_verified_status_is_not_refreshed_without_new_evidence(self):
        key=self.incident('verified')
        for i in range(3):self.store.conn.execute('INSERT INTO samples VALUES(?,?,?,?,?,1)',(str(i),key,time.time(),'passed',str(i)))
        self.assertEqual(service.review(self.store)['changed'],[])
    def test_repair_round_usage_is_preserved(self):
        key=self.store.reserve('repair');self.store.settle(key,'settled',{'input_tokens':10});self.store.settle(key,'settled',{'input_tokens':20})
        self.assertEqual(len(json.loads(self.store.rows('SELECT usage FROM budget')[0]['usage'])['calls']),2)
    def test_natural_probe_uses_actual_new_source_and_export(self):
        from activity_mvp.feedback.probes import observe_journal
        key=self.incident('observing');job='journal-job';folder=self.store.root/'repairs'/job;folder.mkdir(parents=True)
        receipt={'rounds':[{'changed':['core/activity_mvp/feedback/journal.py']}]}
        self.store.conn.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?)',(job,key,1,'observing',time.time(),time.time(),packed(receipt)))
        journal.render(self.store)
        self.assertEqual(observe_journal(self.store)['new_natural_samples'],1)
        self.assertEqual(observe_journal(self.store)['new_natural_samples'],0)
        self.assertEqual(self.store.rows('SELECT outcome FROM samples')[0]['outcome'],'passed')
    def test_journal_success_cannot_attest_parser_repair(self):
        from activity_mvp.feedback.probes import observe_journal
        key=self.incident('observing')
        receipt={'rounds':[{'changed':['core/activity_mvp/feedback/sources.py']}]}
        self.store.conn.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?)',('parser-job',key,1,'observing',time.time(),time.time(),packed(receipt)))
        journal.render(self.store);self.assertEqual(observe_journal(self.store)['new_natural_samples'],0)
    def test_assistant_prose_is_not_automatically_forwarded(self):
        m=self.message();self.store.conn.execute('UPDATE messages SET context=? WHERE id=?',('已完成核查。原记忆正文：不应随上下文转交的段落',m['id']))
        item={'message_id':m['id'],'is_criticism':False,'title':'','category':'other','cause_hypothesis':'','evidence_notes':'','project':'unknown','related_id':''}
        def observe(store,prompt,schema,**guards):
            self.assertTrue(guards['continue_check']());guards['admission_check']()
            self.assertNotIn('不应随上下文转交的段落',prompt)
            self.assertIn('包含完成或通过的表述',prompt)
            return {'items':[item]},'request'
        with patch.object(model,'invoke',side_effect=observe):model.analyze(self.store)
    def test_recent_message_in_old_authenticated_session_is_collected(self):
        p=self.root/'old-session.sqlite';c=sqlite3.connect(p)
        c.executescript('CREATE TABLE sessions(id TEXT,source TEXT,user_id TEXT,started_at REAL);CREATE TABLE messages(id INTEGER,session_id TEXT,role TEXT,content TEXT,timestamp REAL);')
        c.execute('INSERT INTO sessions VALUES(?,?,?,?)',('long','cli','',time.time()-40*86400))
        c.execute('INSERT INTO messages VALUES(?,?,?,?,?)',(1,'long','user','你根本没查资料',time.time()));c.commit();c.close()
        _,n,_=sources.scan_hermes(self.store,{'id':'hermes','platform':'hermes','path':str(p),'cursor':'{}'})
        self.assertEqual(n,1)
    def test_private_controller_checkout_keeps_commit_file_modes(self):
        from activity_mvp.feedback.repair import stage_snapshot,run,write_candidate
        repo=self.root/'source-repo';repo.mkdir();(repo/'plain.py').write_text('value=1');(repo/'中文文件.md').write_text('保留中文路径')
        (repo/'script.sh').write_text('#!/bin/sh\nexit 0\n');(repo/'script.sh').chmod(0o755)
        run(['git','init'],repo);run(['git','add','.'],repo)
        run(['git','-c','user.name=Fixture','-c','user.email=fixture@localhost','-c','core.hooksPath=/dev/null','commit','-m','fixture'],repo)
        old=os.umask(0o077)
        try:
            target=stage_snapshot(repo,self.root/'candidate')
            self.assertEqual((target/'script.sh').stat().st_mode&0o777,0o755)
            self.assertEqual((target/'plain.py').stat().st_mode&0o777,0o644)
            self.assertEqual((target/'中文文件.md').read_text(),'保留中文路径')
            write_candidate(target/'plain.py','value=2')
            self.assertEqual((target/'plain.py').stat().st_mode&0o777,0o644)
            self.assertEqual(target.stat().st_mode&0o777,0o700)
        finally:os.umask(old)
    def test_repeated_model_failure_is_visible_and_notified_once(self):
        from types import SimpleNamespace
        with patch.object(model,'analyze',side_effect=RuntimeError('channel unavailable')),patch('subprocess.run',return_value=SimpleNamespace(returncode=0)):
            for i in range(4):
                with patch.object(service.time,'time',return_value=1800000000+i*4000):service.tick(self.store.root,repair=False)
        self.assertEqual(self.store.get('last_error'),'RuntimeError')
        self.assertEqual(len(self.store.rows("SELECT * FROM notifications WHERE kind='analysis_failure'")),1)
    def test_unavailable_feedback_does_not_starve_proactive_but_unknown_outcome_does(self):
        from activity_mvp.feedback import proactive,project_repair,autonomy
        from activity_mvp.feedback.work import POLICY
        self.store.set('autonomy',{'policy':POLICY,'paused':False})
        with patch.object(autonomy,'synchronize',return_value={}),patch.object(proactive,'discover',return_value={}),patch('activity_mvp.feedback.task_maintenance.tick',return_value={}),patch('activity_mvp.feedback.sync_center.tick',return_value={}),patch('activity_mvp.feedback.memory_autonomy.tick',return_value={}):
            for status,expected in [('personal_source_unavailable',True),('source_missing',True),('unknown',False),('waiting_evidence',False),('acceptance_pending',False)]:
                with self.subTest(status=status),patch.object(project_repair,'process_one',return_value={'status':status}),patch.object(proactive,'process_one',return_value={'status':'idle'}) as run:
                    service.tick(self.store.root,analyze=False)
                    self.assertEqual(run.called,expected)
    def test_expected_daily_budget_limit_does_not_raise_failure_notice(self):
        with patch.object(model,'analyze',side_effect=PermissionError('daily_budget_exhausted')):
            for _ in range(3):service.tick(self.store.root,repair=False)
        self.assertEqual(self.store.get('analysis_failures',0),0)
        self.assertFalse(self.store.rows('SELECT * FROM notifications'))
    def test_failure_backoff_survives_reopen_and_does_not_spend_calls(self):
        with patch.object(model,'analyze',side_effect=TimeoutError),patch.object(service.time,'time',return_value=1800000000):
            service.tick(self.store.root,repair=False)
        self.assertEqual(self.store.get('analysis_retry_at'),1800000300)
        self.store.close();self.store=Store(self.root/'state')
        with patch.object(model,'analyze',side_effect=AssertionError('unexpected retry')),patch.object(service.time,'time',return_value=1800000100):
            result=service.tick(self.store.root,repair=False)
        self.assertEqual(result['analysis']['reason'],'analysis_backoff')
        self.assertEqual(self.store.get('analysis_failures'),1)
        self.assertFalse(self.store.rows('SELECT * FROM budget'))
        with patch.object(model,'analyze',return_value={'processed':5}),patch.object(service.time,'time',return_value=1800000300):
            service.tick(self.store.root,repair=False)
        self.assertEqual(self.store.get('last_error'),'')
        self.assertEqual(self.store.get('analysis_retry_at'),0)
    def test_account_limit_backoff_is_distinct_from_local_repair_budget(self):
        with patch.object(model,'analyze',side_effect=PermissionError('account_rate_limited')),patch.object(service.time,'time',return_value=1800000000):
            service.tick(self.store.root,repair=False)
        self.assertEqual(self.store.get('last_error'),'account_rate_limited')
        self.assertEqual(self.store.get('analysis_retry_at'),1800003600)
    def test_idle_family_does_not_clear_shared_failure_episode(self):
        self.store.set('analysis_failures',2);self.store.set('analysis_retry_at',1800000900);self.store.set('last_error','RuntimeError')
        with patch.object(model,'analyze',return_value={'processed':0,'status':'idle'}),patch.object(service.time,'time',return_value=1800001000):
            service.tick(self.store.root,repair=False)
        self.assertEqual(self.store.get('analysis_failures'),2)
        self.assertEqual(self.store.get('analysis_retry_at'),1800000900)
        self.assertEqual(self.store.get('last_error'),'RuntimeError')
        with patch.object(model,'analyze',side_effect=RuntimeError),patch.object(service.time,'time',return_value=1800001001):
            service.tick(self.store.root,repair=False)
        self.assertEqual(self.store.get('analysis_failures'),3)
        self.assertEqual(self.store.get('analysis_retry_at'),1800002801)
        with patch.object(model,'analyze',return_value={'processed':1}),patch.object(service.time,'time',return_value=1800003000):
            service.tick(self.store.root,repair=False)
        self.assertEqual(self.store.get('analysis_failures'),0)
        self.assertEqual(self.store.get('analysis_retry_at'),0)
    def test_model_lock_rejects_overlap_before_budget_admission(self):
        import fcntl
        with (self.store.root/'model.lock').open('w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaisesRegex(PermissionError,'model_busy'):model.invoke(self.store,'unused',{})
        self.assertFalse(self.store.rows('SELECT * FROM budget'))
    def test_analysis_default_waits_beyond_old_240_second_deadline(self):
        import io
        from types import SimpleNamespace
        from unittest.mock import Mock
        self.store.set('model_capability',{'enabled':True})
        self.store.set('config',{**self.store.get('config'),'codex_binary':__import__('sys').executable})
        def launch(args,**kwargs):
            Path(args[args.index('-o')+1]).write_text('{"result":"ok"}')
            kwargs['stdout'].write('{"type":"turn.completed","usage":{"output_tokens":1}}\n')
            return SimpleNamespace(stdin=io.StringIO(),poll=Mock(side_effect=[None,0]),returncode=0,pid=123)
        with patch.object(model.subprocess,'Popen',side_effect=launch),patch.object(model.time,'monotonic',side_effect=[0,241]),patch.object(model.time,'sleep'),patch.object(model.os,'killpg',side_effect=AssertionError('premature termination')):
            result,key=model.invoke(self.store,'fixture',{})
        self.assertEqual(result,{'result':'ok'})
        self.assertEqual(self.store.rows('SELECT state FROM budget WHERE id=?',(key,))[0]['state'],'settled')
    def test_typed_account_rejection_retains_unknown_usage(self):
        import io
        from types import SimpleNamespace
        self.store.set('model_capability',{'enabled':True})
        self.store.set('config',{**self.store.get('config'),'codex_binary':__import__('sys').executable})
        def launch(args,**kwargs):
            kwargs['stdout'].write('{"type":"turn.failed","error":{"message":"You have hit your usage limit"}}\n')
            return SimpleNamespace(stdin=io.StringIO(),poll=lambda:1,returncode=1)
        with patch.object(model.subprocess,'Popen',side_effect=launch):
            with self.assertRaisesRegex(PermissionError,'account_rate_limited'):model.invoke(self.store,'fixture',{})
        self.assertEqual(self.store.rows('SELECT state FROM budget')[0]['state'],'unknown')
    def test_batch_is_five_and_older_pending_messages_are_retained(self):
        for i in range(8):self.message(when=time.time()-i)
        def response(store,prompt,schema,**guards):
            self.assertTrue(guards['continue_check']());guards['admission_check']()
            data=json.loads(prompt.split('\n',1)[1])
            return {'items':[{'message_id':m['id'],'is_criticism':False,'category':'other','project':'unknown','related_id':''} for m in data['messages']]},'req'
        with patch.object(model,'invoke',side_effect=response):result=model.analyze(self.store)
        self.assertEqual(result['processed'],5)
        self.assertEqual(self.store.rows("SELECT count(*) n FROM messages WHERE state='pending'")[0]['n'],3)
    def test_source_changed_during_model_wait_cannot_be_classified(self):
        m=self.message()
        def response(*args,**guards):
            Path(json.loads(m['reference'])['path']).write_text('changed')
            return {'items':[{'message_id':m['id']}]},'req'
        with patch.object(model,'invoke',side_effect=response):
            with self.assertRaisesRegex(ValueError,'analysis_inputs_changed'):model.analyze(self.store)
        self.assertEqual(self.store.rows('SELECT state FROM messages')[0]['state'],'pending')
        self.assertFalse(self.store.rows('SELECT * FROM incidents'))
    def test_snapshot_does_not_read_source_bodies(self):
        self.incident()
        with patch.object(sources,'scan',side_effect=AssertionError('unexpected read')):self.assertEqual(len(self.store.snapshot()['incidents']),1)

if __name__=='__main__':unittest.main()
