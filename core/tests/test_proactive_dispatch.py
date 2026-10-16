"""Actual controller transitions with synthetic stores; no model/network calls."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from activity_mvp.feedback import model, proactive, acceptance, repair, project_repair
from activity_mvp.feedback.store import Store, packed, atomic
from activity_mvp.feedback.work import WorkQueue, POLICY


class ProactiveDispatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = Store(self.root/'state')
        self.addCleanup(self.store.close)
        self.store.set('config', {'enabled':True, 'codex_binary':sys.executable, 'python':sys.executable})
        self.store.set('autonomy', {'policy':POLICY, 'paused':False})
        self.store.set('proactive', {'policy':proactive.POLICY})
        self.store.set('model_capability', {'enabled':True})
        self.queue = WorkQueue(self.store)
        self.row = self.queue.enqueue('personal_system',proactive.PREFIX+'b'*64,'b'*64,'agi')
        self.key = self.store.reserve('repair')
        for mocker in (
            patch.object(proactive,'source',return_value=({'head':'a'*40},'b'*64,{})),
            patch.object(acceptance,'release_ready',return_value=True),
        ):
            mocker.start();self.addCleanup(mocker.stop)

    def reserved(self, key):
        self.run = max((self.store.root/'runs'/key).iterdir(),key=lambda p:int(p.name))
        self.queue.update(self.row['id'],'running',result={
            'job':'c'*24,'reservation':key,'attempt':1,'phase':'model_sent','model_run':str(self.run)})

    def invoke(self, **kwargs):
        return model.invoke(self.store,'synthetic',{},kind='repair',reservation=self.key,
                            include_lessons=False,on_reserved=self.reserved,**kwargs)

    def assert_recoverable_without_new_budget(self):
        self.assertTrue(model.not_dispatched(self.store,self.key,self.run))
        budget = self.store.rows('SELECT * FROM budget')
        with patch.object(model,'invoke') as invoke:
            self.assertEqual(proactive.process_one(self.store)['status'],'resumed_unstarted_model')
        invoke.assert_not_called()
        row = self.queue.get(self.row['id'])
        self.assertEqual(row['state'],'ready')
        self.assertEqual(row['result']['attempt'],1)
        self.assertEqual(row['result']['reservation'],self.key)
        self.assertEqual(self.store.rows('SELECT * FROM budget'),budget)

    def test_pause_before_dispatch_resumes_only_after_owner_resume(self):
        original = self.reserved
        def pause(key):
            original(key)
            self.store.set('autonomy',{'policy':POLICY,'paused':True})
        with patch.object(model.subprocess,'Popen') as spawn:
            with self.assertRaisesRegex(PermissionError,'not_active'):
                model.invoke(self.store,'synthetic',{},kind='repair',reservation=self.key,
                             include_lessons=False,on_reserved=pause)
        spawn.assert_not_called()
        self.assertEqual(proactive.process_one(self.store)['status'],'paused')
        self.store.set('autonomy',{'policy':POLICY,'paused':False})
        self.assert_recoverable_without_new_budget()

    def test_continue_check_rejection_before_popen_has_fixed_receipt(self):
        with patch.object(model.subprocess,'Popen') as spawn:
            with self.assertRaisesRegex(PermissionError,'not_active'):
                self.invoke(continue_check=Mock(side_effect=[True,False]))
        spawn.assert_not_called();self.assert_recoverable_without_new_budget()

    def test_exec_failure_reuses_reservation_but_not_previous_run_receipt(self):
        with patch.object(model.subprocess,'Popen',side_effect=FileNotFoundError('synthetic binary removed')):
            with self.assertRaises(FileNotFoundError):self.invoke()
        self.assert_recoverable_without_new_budget();previous=self.run
        def completed(args, **kwargs):
            atomic(Path(args[args.index('-o')+1]),packed({'synthetic':True}))
            kwargs['stdout'].write(packed({'type':'turn.completed','usage':{'input_tokens':1}})+'\n')
            return Mock(returncode=0,poll=Mock(return_value=0))
        with patch.object(model.subprocess,'Popen',side_effect=completed):
            result,key=self.invoke()
        self.assertEqual(result,{'synthetic':True});self.assertEqual(key,self.key)
        self.assertNotEqual(previous,self.run)
        self.assertFalse(model.not_dispatched(self.store,self.key,self.run))
        self.assertTrue(model.not_dispatched(self.store,self.key,previous))
        self.assertEqual(len(self.store.rows('SELECT * FROM budget')),1)

    def test_unknown_start_and_started_failure_never_get_retry_proof(self):
        for started in (False,True):
            with self.subTest(started=started):
                outcome = (Mock(returncode=1,poll=Mock(return_value=1)) if started else KeyboardInterrupt())
                with patch.object(model.subprocess,'Popen',**({'return_value':outcome} if started else {'side_effect':outcome})):
                    with self.assertRaises(RuntimeError if started else KeyboardInterrupt):self.invoke()
                self.assertFalse(model.not_dispatched(self.store,self.key,self.run))
                # A model-writable lookalike file cannot authorize another call.
                atomic(self.run/'dispatch.json',packed({'reservation':self.key,'run':str(self.run),'state':'not_started'}))
                with patch.object(model,'invoke') as invoke:
                    self.assertEqual(proactive.process_one(self.store)['status'],'unknown')
                invoke.assert_not_called()


class ProactiveSnapshotTests(unittest.TestCase):
    def check_interruption(self, role):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();repository=root/'repository';repository.mkdir()
            project_repair.git(repository,'init')
            (repository/'sample').write_text('original\n')
            project_repair.git(repository,'add','sample')
            project_repair.git(repository,'-c','user.name=Test','-c','user.email=test@localhost','commit','-m','fixture')
            head=project_repair.git(repository,'rev-parse','HEAD')
            with Store(root/'state') as store:
                store.set('config',{'enabled':True,'python':sys.executable})
                store.set('autonomy',{'policy':POLICY,'paused':False})
                store.set('proactive',{'policy':proactive.POLICY})
                queue=WorkQueue(store);row=queue.enqueue('personal_system',proactive.PREFIX+'b'*64,'b'*64,'agi')
                queue.update(row['id'],'ready',result={'hypothesis':{'contract':'text_blocks'}})
                real=repair.stage_snapshot;interrupted=[False]
                def interrupt(source,target):
                    if Path(target).name==role and not interrupted[0]:
                        interrupted[0]=True
                        project_repair.git(target,'init')
                        raise OSError('synthetic checkout interruption')
                    return real(source,target)
                with (patch.object(proactive,'source',return_value=({'head':head},'b'*64,{})),
                      patch.object(acceptance,'release_ready',return_value=True),
                      patch.dict(proactive.PROJECTS,{'agi':{'repository':str(repository),'editable':('sample',)}}),
                      patch.object(repair,'stage_snapshot',side_effect=interrupt),
                      patch.object(model,'invoke',side_effect=PermissionError('model_busy')) as invoke):
                    self.assertEqual(proactive.process_one(store)['status'],'deferred');invoke.assert_not_called()
                    current=queue.get(row['id']);job=current['result']['job']
                    queue.update(row['id'],'ready',next_check=0)
                    self.assertEqual(proactive.process_one(store)['reason'],'model_busy')
                    folder=store.root/'proactive-repairs'/job
                    for name in ('baseline','candidate'):
                        self.assertEqual(project_repair.git(folder/name,'rev-parse','HEAD'),head)
                        intent=json.loads((folder/(name+'-intent.json')).read_text())
                        self.assertEqual(intent['state'],'complete')
                        self.assertEqual(intent['target'],str(folder/name))
                    # Completed candidate content must never be reset on resume.
                    (folder/'candidate/sample').write_text('later candidate content\n')
                    queue.update(row['id'],'ready',next_check=0)
                    self.assertEqual(proactive.process_one(store)['reason'],'model_busy')
                    self.assertEqual((folder/'candidate/sample').read_text(),'later candidate content\n')
                    self.assertEqual(len(store.rows('SELECT * FROM budget')),1)

    def test_initial_baseline_checkout_interruption_is_resumable(self):
        self.check_interruption('baseline')

    def test_initial_candidate_checkout_interruption_is_resumable(self):
        self.check_interruption('candidate')
