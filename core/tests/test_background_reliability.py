"""Synthetic failures; no production data, writer, model or notification."""
from contextlib import ExitStack
from pathlib import Path
import json, sqlite3, tempfile, time, unittest
from unittest.mock import patch
from activity_mvp.feedback.store import Store, digest
from activity_mvp.feedback.work import WorkQueue, POLICY
from activity_mvp.feedback import service, notifications
from tests.personal_scope_fixtures import register_personal

class BackgroundReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();register_personal(self,self.root,['fixture'])
        self.store=Store(self.root/'feedback');self.addCleanup(self.store.close)
        self.store.set('config',{'enabled':True,'since':time.time()-1000,'journal':str(self.root/'批评与改进')});self.queue=WorkQueue(self.store)
    def hidden_queue(self):
        from activity_mvp.source_governance.personal_store import FilteredStore
        return WorkQueue(FilteredStore(self.store,['work_items'],{'work_items':"reference!='hidden'"}))
    def test_retired_sync_source_stays_stopped_and_is_not_summarized_as_failure(self):
        from activity_mvp.feedback import health, sync_center
        self.store.set('sync_center_error', 'ValueError')
        with patch.object(sync_center.personal_scope, 'company_stopped', return_value=True), \
             patch.object(sync_center, 'read_export', side_effect=AssertionError('retired source read')), \
             patch.object(sync_center.model, 'invoke', side_effect=AssertionError('retired model invoked')):
            result = sync_center.tick(self.store, allow_repair=True)
        self.assertEqual(health.summary(result), {'status': 'owner_retired'})
        self.assertEqual(health.summary(health.summary(result)), {'status': 'owner_retired'})
        self.assertFalse(health.incomplete(result))
        self.assertFalse(health.needs_attention(result))
        self.assertEqual(self.store.get('sync_center_error'), 'ValueError')
        self.assertEqual(health.summary({'status':'owner_retired','reason':'private source text'}), {'status':'owner_retired'})
        blocked = health.summary({'status':'blocked','reason':'private source text'})
        self.assertEqual(blocked, {'status':'blocked','reason':'stage_failed'})
        self.assertTrue(health.incomplete(blocked))
    def test_hidden_existing_row_unchanged(self):
        self.queue.enqueue('memory','hidden','old')
        self.store.conn.execute("UPDATE work_items SET state='verifying',attempts=2")
        before=list(self.store.conn.iterdump())
        with self.assertRaisesRegex(PermissionError,'work_item_not_visible'):self.hidden_queue().enqueue('memory','hidden','new')
        self.assertEqual(list(self.store.conn.iterdump()),before)
    def test_current_evidence_wait_preserves_exact_status_without_exposing_other_reasons(self):
        from activity_mvp.feedback import health
        waiting = {'status':'waiting_evidence','reason':'current_work_required'}
        self.assertEqual(health.summary({**waiting,'private':'fixture secret'}), waiting)
        self.assertEqual(health.summary(health.summary(waiting)), waiting)
        self.assertFalse(health.incomplete(waiting))
        self.assertFalse(health.needs_attention(waiting))
        self.assertEqual(health.summary({'status':'waiting_evidence','reason':'fixture secret'}), {'reason':'stage_failed'})
        blocked = health.summary({'status':'blocked','reason':'current_work_required'})
        self.assertEqual(blocked, {'status':'blocked','reason':'stage_failed'})
        self.assertTrue(health.incomplete(blocked))
        self.assertTrue(health.needs_attention(blocked))
        self.assertEqual(health.exception_evidence(ValueError('current_work_required'))['reason'], 'stage_failed')
    def test_current_evidence_wait_does_not_clear_another_failed_analysis_family(self):
        from activity_mvp.feedback import health
        health.record_analysis(self.store,'reflection',{'status':'blocked','reason':'prior_input_unverifiable','processed':0})
        waiting = {'status':'waiting_evidence','reason':'current_work_required'}
        with ExitStack() as stack:
            stack.enter_context(patch.object(service.sources,'scan',return_value=[]))
            stack.enter_context(patch('activity_mvp.reflection.service.scan',return_value={'status':'ok'}))
            stack.enter_context(patch('activity_mvp.reflection.service.ready',return_value=False))
            stack.enter_context(patch.object(service.model,'analyze',return_value={'processed':1}))
            stack.enter_context(patch('activity_mvp.feedback.project_repair.process_one',return_value=waiting))
            stack.enter_context(patch.object(notifications,'deliver',return_value={'requested':0}))
            result=service.tick(self.store.root)
        self.assertEqual(result['status'],'partial')
        recorded=self.store.get('last_tick_result')
        self.assertEqual(recorded['stages']['repair'],waiting)
        self.assertEqual(recorded['analysis_health']['reflection']['reason'],'prior_input_unverifiable')
    def test_new_hidden_row_rolls_back(self):
        before=list(self.store.conn.iterdump())
        with self.assertRaisesRegex(PermissionError,'work_item_not_visible'):self.hidden_queue().enqueue('memory','hidden','new')
        self.assertEqual(list(self.store.conn.iterdump()),before)
    def test_unrelated_integrity_error_not_silenced(self):
        with self.assertRaises(sqlite3.IntegrityError):self.queue.enqueue(None,'invalid','new')
        self.assertEqual(self.queue.items(),[])
    def test_revocation_rolls_back_only_own_savepoint(self):
        self.store.conn.execute('BEGIN IMMEDIATE');self.store.set('earlier','preserved')
        original=self.queue.get;calls=[0]
        def revoked(key):
            calls[0]+=1
            return original(key) if calls[0]==1 else None
        with patch.object(self.queue,'get',side_effect=revoked):
            with self.assertRaises(PermissionError):self.queue.enqueue('memory','row','v1')
        self.assertTrue(self.store.conn.in_transaction);self.store.conn.execute('COMMIT')
        self.assertEqual(self.store.get('earlier'),'preserved');self.assertEqual(self.queue.items(),[])
    def test_source_change_preserves_wait_and_unknown_result(self):
        for state in ['human_required','running','verifying']:
            row=self.queue.enqueue('memory',state,'old');self.queue.update(row['id'],state,attempts=2,result={'outcome':'unknown'})
            row=self.queue.enqueue('memory',state,'new')
            self.assertEqual((row['state'],row['attempts'],row['result']),(state,2,{'outcome':'unknown'}))
    def test_memory_failure_partial_and_only_independent_reads_continue(self):
        self.store.set('autonomy',{'policy':POLICY,'paused':False})
        def failed(store,**kwargs):
            store.set('prior_writer_outcome','unknown');raise sqlite3.IntegrityError('private fixture')
        with ExitStack() as stack:
            stack.enter_context(patch.object(service.sources,'scan',return_value=[]))
            stack.enter_context(patch.object(service.autonomy,'synchronize',return_value={}))
            for target in ['task_maintenance.tick','sync_center.tick']:
                stack.enter_context(patch('activity_mvp.feedback.'+target,return_value={}))
            stack.enter_context(patch('activity_mvp.feedback.memory_autonomy.tick',side_effect=failed))
            scan=stack.enter_context(patch('activity_mvp.reflection.service.scan',return_value={'status':'ok'}))
            for target in ['model.analyze','proactive.discover','project_repair.process_one']:
                stack.enter_context(patch('activity_mvp.feedback.'+target,side_effect=AssertionError('unsafe continuation')))
            stack.enter_context(patch.object(notifications,'deliver',return_value={'requested':0}))
            result=service.tick(self.store.root)
        self.assertEqual(result['status'],'partial');self.assertEqual(result['memory']['reason'],'IntegrityError')
        self.assertTrue(result['memory']['partial_writes_possible']);self.assertNotIn('private fixture',json.dumps(result))
        self.assertEqual(self.store.get('prior_writer_outcome'),'unknown')
        self.assertEqual(self.store.get('last_tick_result')['status'],'partial');scan.assert_called_once()
    def test_analysis_failure_survives_other_family_success_and_safe_diagnostics(self):
        from activity_mvp.feedback import health
        self.store.set('analysis_last_family','reflection')
        self.store.set('analysis_last_result',{'status':'blocked','reason':'prior_input_unverifiable','processed':0})
        with ExitStack() as stack:
            stack.enter_context(patch.object(service.sources,'scan',return_value=[]))
            stack.enter_context(patch('activity_mvp.reflection.service.scan',return_value={'status':'ok'}))
            stack.enter_context(patch('activity_mvp.reflection.service.ready',return_value=False))
            stack.enter_context(patch.object(service.model,'analyze',return_value={'processed':5}))
            stack.enter_context(patch.object(notifications,'deliver',return_value={'requested':0}))
            result=service.tick(self.store.root,repair=False)
        self.assertEqual(result['status'],'partial')
        recorded=self.store.get('last_tick_result')
        self.assertEqual(recorded['stages']['analysis']['processed'],5)
        self.assertEqual(recorded['analysis_health']['reflection']['reason'],'prior_input_unverifiable')
        try:raise ValueError('private fixture path and content')
        except ValueError as error:diagnostic=health.exception_evidence(error)
        self.assertEqual(diagnostic['reason'],'stage_failed')
        self.assertNotIn('private fixture',json.dumps(diagnostic))
        self.assertEqual(health.exception_evidence(ValueError('source_read_timeout'))['reason'],'source_read_timeout')
        try:self.queue.update('unused','private invalid state')
        except ValueError as error:diagnostic=health.exception_evidence(error)
        self.assertEqual(diagnostic['frames'][-1]['module'],'feedback/work.py')
        self.assertNotIn(str(self.root),json.dumps(diagnostic))
    def test_blocked_family_notice_survives_rotation_and_unknown_delivery_is_not_retried(self):
        from activity_mvp.feedback import health
        blocked={'status':'blocked','reason':'prior_input_unverifiable','processed':0}
        for n in range(3):
            health.record_analysis(self.store,'reflection',blocked)
            health.record_analysis(self.store,'feedback',{'processed':5})
            self.assertEqual(len(notifications.summary(self.store)['pending']),int(n==2))
        with patch.object(notifications.subprocess,'run',side_effect=__import__('subprocess').TimeoutExpired('fixture',8)) as send:
            self.assertTrue(notifications.deliver(self.store)['unknown'])
            episode=health.unresolved_analysis(self.store)['reflection']['episode']
            health.record_analysis(self.store,'reflection',{**blocked,'reason':'prior_input_unresolved'})
            self.assertEqual(health.unresolved_analysis(self.store)['reflection']['episode'],episode)
            self.assertEqual(notifications.deliver(self.store)['requested'],0)
            send.assert_called_once()
        health.record_analysis(self.store,'reflection',{'processed':1})
        self.assertEqual(notifications.summary(self.store)['pending'],[])
    def test_failed_source_persists_across_scan_rotation_but_recovery_clears(self):
        from activity_mvp.feedback import health
        self.store.conn.execute("INSERT INTO sources(id,platform,path,status) VALUES('fixture','codex',?,'unavailable')",(str(self.root),))
        self.assertEqual(health.source_health(self.store,[])['status'],'partial')
        self.store.conn.execute("UPDATE sources SET status='no_owner_events'")
        self.assertEqual(health.source_health(self.store,[])['status'],'ok')
        self.assertEqual(health.source_health(self.store,[{'source':'fixture','error':'ValueError'}])['status'],'partial')
    def test_source_failure_notice_does_not_claim_unknown_execution_receipt(self):
        from activity_mvp.feedback import health
        for _ in range(3):health.record_analysis(self.store,'feedback',{'status':'unavailable','reason':'personal_source_unavailable'})
        value=notifications.summary(self.store)
        self.assertEqual(len(value['pending']),1)
        self.assertIn('改进分析',value['body'])
        self.assertNotIn('回执',value['body'])
    def test_prepare_timeout_preserves_failure_without_marking_tick_completed(self):
        from activity_mvp.local_checks import LocalCheckTimeout
        with patch.object(service.sources,'scan',side_effect=LocalCheckTimeout('feedback_scan')),patch.object(notifications,'deliver',return_value={'requested':0}) as deliver:
            result=service.tick(self.store.root,repair=False)
        deliver.assert_called_once()
        self.assertEqual(result['reason'],'local_check_timeout')
        self.assertIsNone(self.store.get('last_tick'))
        self.assertEqual(self.store.get('last_tick_result')['status'],'partial')
        self.assertEqual(self.store.rows("SELECT id FROM audit WHERE event='tick_completed'"),[])
    def test_timeout_threshold_and_changed_reason_share_one_outbox_identity(self):
        from activity_mvp.feedback import health
        for _ in range(3):health.record_analysis(self.store,'feedback',{'status':'deferred','reason':'local_check_timeout'})
        self.assertEqual(len(notifications.summary(self.store)['pending']),1)
        with patch.object(notifications.subprocess,'run',side_effect=__import__('subprocess').TimeoutExpired('fixture',8)) as send:
            self.assertTrue(notifications.deliver(self.store)['unknown'])
            health.record_analysis(self.store,'feedback',{'status':'blocked','reason':'prior_input_unverifiable'})
            self.assertEqual(notifications.deliver(self.store)['requested'],0)
            send.assert_called_once()
        self.assertEqual(len(self.store.rows("SELECT id FROM notifications WHERE kind='analysis_blocked'")),1)
    def incident(self,ident='issue'):
        from activity_mvp.feedback import sources
        source=self.root/(ident+'.jsonl');source.write_text('synthetic user fixture')
        sources.insert(self.store,{'id':'codex','platform':'codex'},'fixture',time.time(),'你根本没有核对结果','',{'path':str(source),'offset':0,'sha256':digest(source.read_text())})
        message=self.store.rows('SELECT id FROM messages ORDER BY observed DESC LIMIT 1')[0]['id']
        self.store.conn.execute("INSERT INTO incidents(id,title,category,state,created,updated) VALUES(?,'synthetic','other','investigating',?,?)",(ident,time.time(),time.time()))
        self.store.conn.execute('INSERT INTO quotes VALUES(?,?)',(message,ident));return ident
    def test_ordinary_notices_quiet_required_decision_visible(self):
        ident=self.incident();self.store.notify(ident,'installed');self.store.notify(ident,'verified')
        self.assertEqual(notifications.summary(self.store)['pending'],[])
        self.store.decision(ident,{'reason':'synthetic'});result=notifications.summary(self.store)
        self.assertEqual([n['kind'] for n in result['pending']],['decision'])
        self.assertNotIn('打开 PSI',result['body']);self.assertNotIn('synthetic',result['body'])
    def test_unknown_not_retried_new_decision_can_send(self):
        self.store.decision(self.incident(),{'reason':'synthetic'})
        with patch.object(notifications.subprocess,'run',side_effect=__import__('subprocess').TimeoutExpired('fixture',8)) as send:
            self.assertTrue(notifications.deliver(self.store)['unknown']);self.assertEqual(notifications.deliver(self.store)['requested'],0);send.assert_called_once()
        self.store.decision(self.incident('new-issue'),{'reason':'another'})
        with patch.object(notifications.subprocess,'run',return_value=type('Result',(),{'returncode':0})()) as send:
            self.assertEqual(notifications.deliver(self.store)['requested'],1);self.assertEqual(notifications.deliver(self.store)['requested'],0);send.assert_called_once()
    def test_resolved_or_stale_notice_not_sent(self):
        ident=self.incident();self.store.decision(ident,{'reason':'synthetic'})
        self.store.conn.execute('UPDATE incidents SET revision=revision+1');self.assertEqual(notifications.summary(self.store)['pending'],[])
        self.store.conn.execute("UPDATE incidents SET revision=1,state='resolved'");self.assertEqual(notifications.summary(self.store)['pending'],[])

    def test_canceled_before_submission_releases_claim_for_later(self):
        self.store.decision(self.incident(),{'reason_code':'new_permission'})
        summary=notifications.summary
        def revoked(*args,**kwargs):
            if kwargs.get('delivery_state')==3:
                return {'pending':[]}
            return summary(*args,**kwargs)
        with patch.object(notifications,'summary',side_effect=revoked),patch.object(notifications.subprocess,'run') as send:
            self.assertEqual(notifications.deliver(self.store)['status'],'canceled_before_submission');send.assert_not_called()
        with patch.object(notifications.subprocess,'run',return_value=type('Result',(),{'returncode':0})()) as send:
            self.assertEqual(notifications.deliver(self.store)['requested'],1);send.assert_called_once()

    def test_paused_notice_stays_pending_and_resumes_once(self):
        self.store.decision(self.incident(),{'reason_code':'payment'});self.store.set('paused',True)
        with patch.object(notifications.subprocess,'run') as send:
            self.assertEqual(notifications.deliver(self.store)['status'],'paused');send.assert_not_called()
        self.store.set('paused',False)
        with patch.object(notifications.subprocess,'run',return_value=type('Result',(),{'returncode':0})()):
            self.assertEqual(notifications.deliver(self.store)['requested'],1)

    def test_persistent_system_failure_visible_without_incident_and_recovery_quiet(self):
        self.store.set('analysis_failures',3);self.store.set('analysis_failure_episode',123)
        self.store.notify('analysis-123','analysis_failure')
        self.store.set('memory_failure_episode',{'started':456,'failures':3})
        self.store.notify('memory-456','memory_failure')
        self.assertEqual({n['kind'] for n in notifications.summary(self.store)['pending']},{'analysis_failure','memory_failure'})
        self.store.set('analysis_failures',0);self.store.set('memory_failure_episode',{})
        self.assertEqual(notifications.summary(self.store)['pending'],[])

    def test_investigation_only_candidate_never_reads_or_mutates_hidden_work(self):
        from activity_mvp.feedback import memory_autonomy as worker
        from activity_mvp.conversation_scope import filtered
        registry={'bindings':{},'investigation_sources':{'hidden':[{'path':str(self.root/'authority.md')}]}}
        self.queue.enqueue('memory','hidden','original')
        before=self.store.rows('SELECT * FROM work_items')
        original=Path.read_text
        def registry_read(path,*args,**kwargs):
            if path==Path.home()/'.hermes/memory-review-center/state/autonomy-policy.json':return json.dumps(registry)
            return original(path,*args,**kwargs)
        with patch.object(Path,'read_text',registry_read),patch.object(worker,'load_registry',return_value=registry),patch.object(worker,'memory_review_candidates',return_value={'items':[{'memory_id':'hidden','review_state':'pending'}]}),patch.object(worker,'read_memory_text',side_effect=AssertionError('hidden text read')),patch.object(worker,'apply',side_effect=AssertionError('hidden replay')):
            scoped=filtered(self.store)
            self.assertEqual(WorkQueue(scoped).items('memory'),[])
            self.store.set('autonomy',{'policy':POLICY,'paused':False})
            self.assertEqual(worker._tick_original(scoped)['processed'],0)
        self.assertEqual(self.store.rows('SELECT * FROM work_items'),before)

    def test_unknown_memory_result_never_replayed(self):
        from activity_mvp.feedback import memory_autonomy as worker
        self.store.set('autonomy',{'policy':POLICY,'paused':False})
        row=self.queue.enqueue('memory','known','original');self.queue.update(row['id'],'verifying',result={'outcome':'unknown'})
        with patch.object(worker,'work_candidates',return_value=[{'memory_id':'known'}]),patch.object(worker,'load_registry',return_value={}),patch.object(worker.personal_scope,'memory_admitted',return_value=True),patch.object(worker,'read_memory_text',side_effect=AssertionError('unknown replay')),patch.object(worker,'apply',side_effect=AssertionError('unknown replay')):
            result=worker._tick_original(self.store)
        self.assertEqual(result['unknown_results'],1);self.assertEqual(result['status'],'partial')
        self.assertEqual(self.queue.get(row['id'])['state'],'verifying')

if __name__=='__main__':unittest.main()
