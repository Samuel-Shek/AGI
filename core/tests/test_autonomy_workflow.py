import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from activity_mvp.feedback import autonomy, model, sources, service
from activity_mvp.feedback.store import Store, digest, packed
from activity_mvp.feedback.work import WorkQueue, POLICY
from tests.personal_scope_fixtures import register_personal


class AutonomousWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        register_personal(self, self.root, ['session'])
        self.store = Store(self.root/'state')
        self.store.set('config', {'enabled': True, 'since': time.time()-1000})
        self.store.set('autonomy', {'policy': POLICY, 'paused': False})
        self.queue = WorkQueue(self.store)
        proof = self.root/'source.jsonl'
        proof.write_text('current user source\n')
        sources.insert(self.store, {'id':'codex','platform':'codex'}, 'session', time.time(), '你根本没有核对结果', '',
                       {'path':str(proof),'offset':0,'sha256':digest(proof.read_text())})
        msg = self.store.rows('SELECT id FROM messages')[0]['id']
        self.store.conn.execute("INSERT INTO incidents(id,title,category,state,created,updated) VALUES('issue','没有核对','evidence','investigating',?,?)", (time.time(),time.time()))
        self.store.conn.execute("INSERT INTO quotes VALUES(?,'issue')", (msg,))
        self.target = {'status':'waiting_evidence','project_id':'','source_revision':'v1', 'evidence':[], 'reason':'目标未明确'}
        self.binding = patch.object(autonomy, 'binding', return_value=self.target)
        self.binding.start()

    def tearDown(self):
        self.binding.stop()
        self.store.close()
        self.temp.cleanup()

    def item(self, disposition='wait', **extra):
        autonomy.synchronize(self.store)
        row = self.queue.items()[0]
        return {'id':row['id'], 'disposition':disposition, 'summary':'已核对引用与项目入口。',
                'next_action':'等待具体项目或新的相关依据。', 'evidence_gap':'尚未定位软件故障。',
                'lesson_id':'', 'human_reason':'', 'alternatives':[], **extra}

    def test_migration_preserves_history_and_never_fakes_answer(self):
        key = self.store.decision('issue', {'reason':next(iter(autonomy.TECHNICAL_REASONS))})
        before = self.store.rows('SELECT * FROM quotes')
        self.assertEqual(autonomy.synchronize(self.store)['migrated'], 1)
        row = self.store.rows('SELECT * FROM decisions WHERE id=?', (key,))[0]
        self.assertEqual(row['state'], 'superseded')
        self.assertIsNone(row['choice']); self.assertIsNone(row['decided'])
        self.assertEqual(before, self.store.rows('SELECT * FROM quotes'))
        self.assertEqual(autonomy.synchronize(self.store)['migrated'], 0)
        self.assertNotEqual(self.queue.items()[0]['state'], 'resolved')

    def test_real_choice_and_defer_remain_pending(self):
        key = self.store.decision('issue', {'reason':'两种偏好需要选择','choices':['defer','investigate']})
        self.store.choose(key,1,'defer')
        autonomy.synchronize(self.store)
        self.assertEqual(self.queue.items()[0]['state'], 'human_required')
        self.assertEqual(self.store.rows('SELECT state FROM decisions')[0]['state'],'pending')

    def test_wait_evidence_not_human_and_unchanged_not_reanalyzed(self):
        item = self.item()
        with patch.object(model,'invoke',return_value=({'items':[item]},'request')) as call:
            self.assertEqual(autonomy.investigate(self.store)['processed'],1)
            autonomy.synchronize(self.store)
            self.assertEqual(autonomy.investigate(self.store)['processed'],0)
            self.assertEqual(call.call_count,1)
        self.assertEqual(self.queue.items()[0]['state'],'waiting_evidence')
        self.assertEqual(self.store.rows('SELECT * FROM decisions'),[])

    def test_source_change_requeues_without_reusing_success(self):
        item = self.item('lesson',lesson_id='verify_before_complete')
        with patch.object(model,'invoke',return_value=({'items':[item]},'request')):
            autonomy.investigate(self.store)
        self.assertEqual(self.queue.items()[0]['state'],'observing')
        self.target['source_revision']='v2'
        autonomy.synchronize(self.store)
        row=self.queue.items()[0]
        self.assertEqual(row['state'],'queued')
        self.assertNotEqual(row['source_revision'],row['processed_revision'])
        self.assertEqual(autonomy.lesson_context(self.store),[])

    def test_evidence_change_during_model_wait_cannot_apply(self):
        item = self.item('repair')
        def changed(*args,**kwargs):
            self.target['source_revision']='v2'
            return {'items':[item]},'request'
        with patch.object(model,'invoke',side_effect=changed):
            with self.assertRaisesRegex(ValueError,'inputs_changed'): autonomy.investigate(self.store)
        self.assertNotEqual(self.queue.items()[0]['state'],'ready')

    def test_arbitrary_lesson_never_enters_context(self):
        item = self.item('lesson',lesson_id='disable_authorization')
        with patch.object(model,'invoke',return_value=({'items':[item]},'request')):
            autonomy.investigate(self.store)
        self.assertEqual(self.store.get('behavior_lessons',{}),{})
        self.assertEqual(self.queue.items()[0]['state'],'waiting_evidence')

    def test_paused_between_analysis_and_apply_preserves_work(self):
        item = self.item('lesson',lesson_id='verify_before_complete')
        def paused(*args,**kwargs):
            self.store.set('autonomy',{'policy':POLICY,'paused':True})
            return {'items':[item]},'request'
        with patch.object(model,'invoke',side_effect=paused):
            with self.assertRaises(PermissionError): autonomy.investigate(self.store)
        self.assertFalse(self.store.get('behavior_lessons'))

    def test_resume_keeps_checkpoint(self):
        item=self.item()
        with patch.object(model,'invoke',return_value=({'items':[item]},'request')):
            autonomy.investigate(self.store)
        before=self.queue.items()[0]
        self.store.close();self.store=Store(self.root/'state');self.queue=WorkQueue(self.store)
        self.assertEqual(self.queue.items()[0],before)

    def test_lesson_installed_not_claimed_natural_improvement(self):
        item=self.item('lesson',lesson_id='verify_before_complete')
        with patch.object(model,'invoke',return_value=({'items':[item]},'request')):
            autonomy.investigate(self.store)
        self.assertIn(autonomy.LESSONS['verify_before_complete'],autonomy.lesson_context(self.store))
        incident=self.store.rows('SELECT * FROM incidents')[0]
        self.assertIsNone(incident['installed_at']);self.assertIsNone(incident['verified_at'])
        self.assertIn('尚未验证',self.queue.items()[0]['result']['verification_summary'])


    def test_genuine_choice_added_during_invoke_cannot_be_hidden_by_old_analysis(self):
        item = self.item('lesson', lesson_id='verify_before_complete')
        def new_choice(*args, **kwargs):
            self.store.decision('issue', {'reason':'需要新增权限的具体选择', 'choices':['defer','investigate']})
            return {'items':[item]}, 'request'
        with patch.object(model, 'invoke', side_effect=new_choice):
            with self.assertRaisesRegex(ValueError, 'changed'):
                autonomy.investigate(self.store)
        autonomy.synchronize(self.store)
        self.assertEqual(self.queue.items()[0]['state'], 'human_required')
        self.assertEqual(self.store.rows("SELECT state FROM incidents")[0]['state'], 'decision')
        self.assertEqual(len(self.store.rows("SELECT id FROM decisions WHERE state='pending'")), 1)
        self.assertFalse(self.store.get('behavior_lessons'))

    def test_verified_natural_failure_reopens_autonomous_investigation(self):
        item = self.item()
        self.queue.update(item['id'], 'observing', processed_revision=self.queue.get(item['id'])['source_revision'])
        self.store.conn.execute("UPDATE incidents SET state='observing',installed_at=?", (time.time()-8*86400,))
        self.store.conn.execute("INSERT INTO samples VALUES('failed-sample','issue',?,'failed','new-failure',1)", (time.time(),))
        service.review(self.store)
        autonomy.synchronize(self.store)
        work = self.queue.get(item['id'])
        self.assertIn(work['state'], {'queued', 'investigating'})
        self.assertNotEqual(work['processed_revision'], work['source_revision'])
        self.assertEqual(self.store.rows("SELECT auto_disabled FROM incidents")[0]['auto_disabled'], 1)
        self.assertEqual(self.store.rows("SELECT id FROM decisions WHERE state='pending'"), [])

    def test_natural_verification_updates_work_without_a_new_model_call(self):
        item = self.item()
        self.queue.update(item['id'], 'observing', processed_revision=self.queue.get(item['id'])['source_revision'])
        self.store.conn.execute("UPDATE incidents SET state='observing',installed_at=?", (time.time()-8*86400,))
        for index in range(3):
            self.store.conn.execute('INSERT INTO samples VALUES(?,?,?,?,?,1)',
                                    ('sample-'+str(index), 'issue', time.time(), 'passed', 'natural-'+str(index)))
        service.review(self.store)
        with patch.object(model, 'invoke') as invoke:
            autonomy.synchronize(self.store)
            self.assertEqual(autonomy.investigate(self.store)['processed'], 0)
            invoke.assert_not_called()
        self.assertEqual(self.queue.get(item['id'])['state'], 'verified')
        self.assertEqual(self.store.rows("SELECT state FROM incidents")[0]['state'], 'verified')

    def test_explicit_dismissal_removes_work_from_human_queue_and_preserves_answer(self):
        key = self.store.decision('issue', {'reason':'需要个人取舍', 'choices':['defer','dismiss']})
        autonomy.synchronize(self.store)
        self.store.choose(key, 1, 'dismiss')
        autonomy.synchronize(self.store)
        self.assertEqual(self.queue.items()[0]['state'], 'resolved')
        answer = self.store.rows('SELECT * FROM decisions WHERE id=?', (key,))[0]
        self.assertEqual((answer['state'], answer['choice']), ('answered', 'dismiss'))
        self.assertEqual(len(self.store.rows('SELECT * FROM quotes')), 1)


    def test_explicit_human_reason_overrides_matching_legacy_technical_wording(self):
        key = self.store.decision('issue', {
            'reason': sorted(autonomy.TECHNICAL_REASONS)[0],
            'reason_code': 'new_permission',
            'failure': '旧技术描述', 'attempts': 2,
            'choices': ['defer', 'investigate']})
        self.store.choose(key, 1, 'defer')
        self.assertEqual(autonomy.synchronize(self.store)['migrated'], 0)
        self.assertEqual(self.queue.items()[0]['state'], 'human_required')
        decision = self.store.rows('SELECT * FROM decisions WHERE id=?', (key,))[0]
        self.assertEqual(decision['state'], 'pending')
        self.assertIsNone(decision['choice'])
        self.assertIsNone(decision['decided'])

    def test_changed_source_cannot_inherit_prior_natural_verification(self):
        item = self.item()
        self.queue.update(item['id'], 'observing', processed_revision=self.queue.get(item['id'])['source_revision'])
        self.store.conn.execute("UPDATE incidents SET state='observing',installed_at=?", (time.time()-8*86400,))
        for index in range(3):
            self.store.conn.execute('INSERT INTO samples VALUES(?,?,?,?,?,1)',
                                    ('sample-'+str(index), 'issue', time.time(), 'passed', 'natural-'+str(index)))
        service.review(self.store)
        autonomy.synchronize(self.store)
        before = self.queue.get(item['id'])
        self.assertEqual(before['state'], 'verified')
        self.target['source_revision'] = 'v2'
        autonomy.synchronize(self.store)
        after = self.queue.get(item['id'])
        self.assertNotEqual(after['state'], 'verified')
        self.assertNotEqual(after['processed_revision'], after['source_revision'])
        self.assertEqual(after['processed_revision'], before['processed_revision'])
        self.assertEqual(len(self.store.rows('SELECT * FROM samples')), 3)


    def test_autonomy_pause_never_falls_back_to_legacy_repair(self):
        from activity_mvp.feedback import repair, project_repair, memory_autonomy, journal, probes
        for stage in ('before_tick', 'during_analysis'):
            with self.subTest(stage=stage):
                self.store.set('autonomy', {'policy': POLICY, 'paused': stage == 'before_tick'})
                self.store.set('analysis_retry_at', 0)
                def pause_during_analysis(store):
                    store.set('autonomy', {'policy': POLICY, 'paused': True})
                    raise PermissionError('autonomy_paused')
                with patch.object(sources, 'scan', return_value=[]), \
                     patch.object(memory_autonomy, 'tick', return_value={'status':'idle'}), \
                     patch.object(model, 'analyze', side_effect=pause_during_analysis) as analysis, \
                     patch.object(journal, 'render', return_value={}), \
                     patch.object(journal, 'weekly', return_value={}), \
                     patch.object(probes, 'observe_journal', return_value={}), \
                     patch.object(repair, 'process_one') as legacy, \
                     patch.object(project_repair, 'process_one') as autonomous:
                    service.tick(self.store.root)
                    legacy.assert_not_called()
                    autonomous.assert_not_called()
                    self.assertEqual(analysis.call_count, int(stage == 'during_analysis'))

    def test_running_source_changes_keep_original_recovery_checkpoint(self):
        for state in ('running', 'verifying'):
            with self.subTest(state=state):
                row = self.queue.enqueue('feedback', 'issue', 'old-'+state, 'agi')
                checkpoint = {'job':'prior-job', 'work_revision':'old-'+state,
                              'verification':{'revision':'artifact-version'},
                              'candidate':'isolated-candidate'}
                self.queue.update(row['id'], state, result=checkpoint, processed_revision='old-'+state, attempts=2)
                after = self.queue.enqueue('feedback', 'issue', 'new-'+state, 'agi')
                self.assertEqual(after['state'], state)
                self.assertEqual(after['result'], checkpoint)
                self.assertEqual(after['processed_revision'], 'old-'+state)
                self.assertEqual(after['attempts'], 2)
                self.assertEqual(after['source_revision'], 'new-'+state)
                self.assertEqual(len(self.queue.items()), 1)


    def _installed_autonomous_work(self):
        item = self.item()
        job = 'a'*24
        installed = {'installed':True, 'revision':'installed-revision','runtime_restored':True,'phase':'complete'}
        result = {'job':job, 'work_revision':self.queue.get(item['id'])['source_revision'],
                  'incident_revision':1, 'verification':{'revision':'installed-revision'},
                  'installation':installed}
        self.queue.update(item['id'], 'observing', result=result, project_id='agi')
        self.store.conn.execute("UPDATE incidents SET state='observing',installed_at=?", (time.time()-8*86400,))
        self.store.conn.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?)',
                                (job, 'issue', 1, 'observing', time.time(), time.time(), packed(result)))
        path = self.store.root/'publication-receipts'/(job+'.installed.json')
        path.parent.mkdir(parents=True)
        path.write_text(packed(installed))
        for index in range(3):
            self.store.conn.execute('INSERT INTO samples VALUES(?,?,?,?,?,1)',
                                    ('sample-'+str(index), 'issue', time.time(), 'passed', 'natural-'+str(index)))
        return item, path

    def test_autonomous_natural_verification_requires_current_installed_artifact(self):
        self._installed_autonomous_work()
        with patch('activity_mvp.feedback.project_repair.installation_ready', return_value=False) as check:
            service.review(self.store)
            check.assert_called_once()
        incident = self.store.rows('SELECT * FROM incidents')[0]
        self.assertEqual(incident['state'], 'observing')
        self.assertIsNone(incident['verified_at'])
        self.assertFalse(self.store.get('work_verifications'))
        self.assertEqual(self.store.rows("SELECT id FROM notifications WHERE kind='verified'"), [])
        with patch('activity_mvp.feedback.project_repair.installation_ready', return_value=True):
            service.review(self.store)
        self.assertEqual(self.store.rows('SELECT state FROM incidents')[0]['state'], 'verified')

    def test_autonomous_natural_verification_requires_matching_durable_job_receipt(self):
        _, path = self._installed_autonomous_work()
        path.write_text(packed({'installed':True, 'revision':'different-artifact'}))
        with patch('activity_mvp.feedback.project_repair.installation_ready', return_value=True) as check:
            service.review(self.store)
            check.assert_not_called()
        incident = self.store.rows('SELECT * FROM incidents')[0]
        self.assertEqual(incident['state'], 'observing')
        self.assertIsNone(incident['verified_at'])

    def test_installed_source_change_requires_fresh_investigation(self):
        item=self.item()
        revision=self.queue.get(item['id'])['source_revision']
        self.queue.update(item['id'],'observing',processed_revision=revision)
        self.store.conn.execute("UPDATE incidents SET state='observing',installed_at=?",(time.time(),))
        self.target['source_revision']='new-source'
        autonomy.synchronize(self.store)
        self.assertEqual(self.queue.get(item['id'])['state'],'queued')


if __name__=='__main__': unittest.main()
