"""Controller tests use synthetic state and inputs; never production data/model."""
import copy
import json
from pathlib import Path
import plistlib
import sys
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from activity_mvp.feedback import proactive as p, proactive_contracts as contracts, acceptance, project_repair as repair
from activity_mvp.feedback.store import Store, atomic, packed, digest
from activity_mvp.feedback.work import WorkQueue, POLICY

class ProactiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name).resolve()
        self.store=Store(self.root/'state'); self.queue=WorkQueue(self.store)
        self.store.set('config',{'enabled':True,'python':sys.executable,'live_core':str(self.root/'core')})
        self.store.set('autonomy',{'policy':POLICY,'paused':False})
        self.store.set('proactive',{'policy':p.POLICY})
        self.source=patch.object(p,'source',return_value=({'head':'a'*40},'b'*64,{})); self.source.start()
        self.release=patch.object(acceptance,'release_ready',return_value=True); self.release.start()
    def tearDown(self):
        self.release.stop(); self.source.stop(); self.store.close(); self.temp.cleanup()
    def row(self,state='ready',extra=None):
        row=self.queue.enqueue('personal_system',p.PREFIX+'b'*64,'b'*64,'agi')
        result={'origin':p.POLICY,'identity':{'head':'a'*40},'hypothesis':{'contract':'text_blocks'},'rounds':[]}
        result.update(extra or {})
        return self.queue.update(row['id'],state,result=result)
    def test_pause_and_stop_prevent_admission(self):
        self.store.set('autonomy',{'policy':POLICY,'paused':True})
        self.assertFalse(p.ready(self.store)); self.assertEqual(p.process_one(self.store)['status'],'paused')
        with self.assertRaises(PermissionError):p.enable(self.store,p.POLICY)
        self.store.set('autonomy',{'policy':POLICY,'paused':False});(self.store.root/'STOP').touch()
        self.assertFalse(p.active(self.store));self.assertEqual(self.store.rows('SELECT * FROM budget'),[])
    def test_enable_never_resets_budget_pause_or_unknown(self):
        self.store.reserve('repair'); self.row('verifying',{'job':'c'*24})
        self.assertTrue(p.enable(self.store,p.POLICY)['enabled'])
        self.assertEqual(len(self.store.rows('SELECT * FROM budget')),1)
        self.assertEqual(self.queue.items()[0]['state'],'verifying')
    def test_source_identity_drift_blocks_old_candidate(self):
        row=self.row()
        with patch.object(p,'source',return_value=({'head':'a'*40},'d'*64,{})),patch.object(p.model,'invoke') as model:
            self.assertEqual(p.process_one(self.store)['status'],'source_changed')
        model.assert_not_called();self.assertEqual(self.queue.get(row['id'])['state'],'blocked')
    def test_shared_budget_and_backoff_prevent_new_model(self):
        self.row();self.store.reserve('repair')
        with patch.object(p.model,'invoke') as model:
            with self.assertRaisesRegex(PermissionError,'daily_budget'):p.process_one(self.store)
            self.store.set('analysis_retry_at',time.time()+100)
            self.assertEqual(p.process_one(self.store)['status'],'backoff')
        model.assert_not_called()
    def test_pending_publication_is_read_back_never_replayed(self):
        row=self.row('verifying',{'job':'c'*24});d=self.store.root/'publication-requests';d.mkdir();atomic(d/('c'*24+'.json'),'{}')
        with patch.object(p,'_reconcile',return_value=False),patch.object(repair,'publish_candidate') as publish:
            self.assertEqual(p.process_one(self.store)['status'],'unknown')
        publish.assert_not_called();self.assertEqual(self.queue.get(row['id'])['state'],'verifying')
    def test_model_unknown_is_readback_only(self):
        run=self.store.root/'runs'/('e'*24)/'123';run.mkdir(parents=True);atomic(run/'events.jsonl','')
        row=self.row('running',{'job':'c'*24,'reservation':'e'*24,'attempt':1,'phase':'model_sent','model_run':str(run)})
        with patch.object(p.model,'invoke') as model:
            self.assertEqual(p.process_one(self.store)['status'],'unknown')
        model.assert_not_called();self.assertEqual(self.queue.get(row['id'])['state'],'running')
    def test_completed_model_output_resumes_without_new_request(self):
        run=self.store.root/'runs'/('e'*24)/'123';run.mkdir(parents=True)
        atomic(run/'events.jsonl',packed({'type':'turn.completed'})+'\n');atomic(run/'result.json',packed({'risk':'bounded'}))
        (self.store.root/'proactive-repairs'/('c'*24)).mkdir(parents=True)
        row=self.row('running',{'job':'c'*24,'reservation':'e'*24,'attempt':1,'phase':'model_sent','model_run':str(run)})
        with patch.object(p.model,'invoke') as model:
            self.assertEqual(p.process_one(self.store)['status'],'resumed_known_stage')
        model.assert_not_called();self.assertEqual(self.queue.get(row['id'])['result']['phase'],'proposal_received')

    def unknown_request(self):
        key=self.store.reserve('repair');self.store.settle(key,'unknown')
        run=self.store.root/'runs'/key/'123';run.mkdir(parents=True)
        atomic(run/'events.jsonl',packed({'type':'turn.started'})+'\n')
        self.store.set('model_dispatch:'+digest([key,str(run)]),{'reservation':key,'run':str(run),'state':'started'})
        self.store.set('proactive_last_day',p.day())
        row=self.row('verifying',{'job':'c'*24,'reservation':key,'attempt':1,
            'phase':'model_sent','model_run':str(run),'reason':'TimeoutExpired'})
        return row,run

    def test_accepted_new_source_invalidates_only_candidate_keeps_unknown_request(self):
        row,run=self.unknown_request();budget=self.store.rows('SELECT * FROM budget')
        with patch.object(p,'source',return_value=({'head':'d'*40},'d'*64,{})),patch.object(p,'_installed_source'),patch.object(p.model,'invoke') as model:
            self.assertEqual(p.process_one(self.store)['status'],'source_changed_unknown')
            saved=self.queue.get(row['id'])
            self.assertEqual(saved['result'],row['result'])
            for field in ('attempts','next_check','source_revision'):
                self.assertEqual(saved[field],row[field])
            self.assertEqual(saved['state'],'blocked');self.assertFalse(p.ready(self.store))
            self.assertEqual(self.store.rows('SELECT * FROM budget'),budget)
            with self.assertRaisesRegex(PermissionError,'daily_budget'):self.store.reserve('repair')
            record=self.store.get('proactive_supersession:'+digest([row['id'],'b'*64,'d'*64]))
            self.assertEqual(record['model_outcome'],'unknown');self.assertEqual(record['run'],str(run))
            self.assertEqual(record['installed_head'],'d'*40)
            self.assertFalse(p.model.not_dispatched(self.store,row['result']['reservation'],run))
            with patch.object(p,'day',return_value='next-day'):
                self.assertTrue(p.ready(self.store))
            # A late response remains evidence for the invalidated source, never a new candidate.
            atomic(run/'events.jsonl',packed({'type':'turn.completed'})+'\n')
            atomic(run/'result.json',packed({'risk':'bounded'}))
            self.assertEqual(p.process_one(self.store)['status'],'idle')
            self.assertEqual(self.queue.get(row['id'])['state'],'blocked')
        model.assert_not_called()

    def test_unknown_same_source_and_unavailable_or_unaccepted_source_stay_unknown(self):
        row,run=self.unknown_request();budget=self.store.rows('SELECT * FROM budget')
        scenarios=[
            (patch.object(p,'source',return_value=({'head':'a'*40},'b'*64,{})),patch.object(p,'_installed_source')),
            (patch.object(acceptance,'release_ready',return_value=False),patch.object(p,'source',return_value=({'head':'d'*40},'d'*64,{}))),
            (patch.object(p,'source',side_effect=subprocess.TimeoutExpired('synthetic-identity',30)),patch.object(p,'_installed_source')),
            (patch.object(p,'source',return_value=({'head':'d'*40},'d'*64,{})),patch.object(p,'_installed_source',side_effect=PermissionError('mismatched_install'))),
            (patch.object(p,'source',side_effect=[({'head':'d'*40},'d'*64,{}),({'head':'e'*40},'e'*64,{})]),patch.object(p,'_installed_source')),
        ]
        with patch.object(p.model,'invoke') as model:
            for source_check,installation_check in scenarios:
                with source_check,installation_check:
                    self.assertEqual(p.process_one(self.store)['status'],'unknown')
                self.assertEqual(self.queue.get(row['id']),row)
                self.assertEqual(self.store.rows('SELECT * FROM budget'),budget)
        model.assert_not_called()
        self.assertEqual(self.store.rows("SELECT key FROM meta WHERE key LIKE 'proactive_supersession:%'"),[])

    def test_unknown_supersession_keeps_cooldown_pause_and_publication_precedence(self):
        row,run=self.unknown_request()
        row=self.queue.update(row['id'],'verifying',next_check=time.time()+300)
        with patch.object(p,'source',side_effect=AssertionError('must_not_read_source')),patch.object(p.model,'invoke') as model:
            self.assertEqual(p.process_one(self.store)['status'],'unknown')
            self.store.set('autonomy',{'policy':POLICY,'paused':True})
            self.assertEqual(p.process_one(self.store)['status'],'paused')
            self.store.set('autonomy',{'policy':POLICY,'paused':False})
            row=self.queue.update(row['id'],'verifying',next_check=0)
            folder=self.store.root/'publication-requests';folder.mkdir()
            atomic(folder/('c'*24+'.json'),'{}')
            self.assertEqual(p.process_one(self.store)['status'],'unknown')
        model.assert_not_called();self.assertEqual(self.queue.get(row['id']),row)
    def test_known_preflight_failure_remains_ready(self):
        row=self.row(extra={'job':'c'*24,'verification':{},'candidate':str(self.root)})
        with patch('activity_mvp.feedback.recovery_storage.backend'),patch.object(repair,'publish_candidate',return_value={'installed':False,'status':'app_busy'}):
            self.assertEqual(p._publish(self.store,row)['status'],'app_busy')
        self.assertEqual(self.queue.get(row['id'])['state'],'ready')

    def test_known_account_rejection_preserves_budget_and_two_attempt_limit(self):
        run=self.store.root/'runs'/('e'*24)/'123';run.mkdir(parents=True)
        atomic(run/'events.jsonl',packed({'type':'turn.failed','error':{'message':'rate_limit_exceeded'}})+'\n')
        row=self.row('running',{'job':'c'*24,'reservation':'e'*24,'attempt':1,'phase':'model_sent','model_run':str(run)})
        with patch.object(p.model,'invoke') as model:
            self.assertEqual(p.process_one(self.store)['status'],'backoff')
            result=self.queue.get(row['id'])['result'];self.assertEqual(result['attempt'],2)
            result['phase']='model_sent';self.queue.update(row['id'],'running',result=result)
            self.assertEqual(p.process_one(self.store)['status'],'round_limit')
        model.assert_not_called();self.assertEqual(self.queue.get(row['id'])['state'],'blocked')

    def test_committed_candidate_recovery_is_exact_and_idempotent(self):
        from activity_mvp.feedback.projects import git
        candidate=self.root/'candidate';candidate.mkdir();git(candidate,'init')
        (candidate/'sample').write_text('before');git(candidate,'add','sample')
        git(candidate,'-c','user.name=Test','-c','user.email=test@localhost','commit','-m','baseline')
        base=git(candidate,'rev-parse','HEAD');logs=self.root/'verification';logs.mkdir()
        (candidate/'sample').write_text('after');git(candidate,'add','sample')
        revision=repair._commit_candidate(candidate,base,logs)
        self.assertEqual(repair._commit_candidate(candidate,base,logs),revision)
        (candidate/'sample').write_text('unreviewed');git(candidate,'add','sample')
        with self.assertRaises(PermissionError):repair._commit_candidate(candidate,base,logs)

    def test_generated_test_cannot_pollute_fixed_suite_interpreter(self):
        root=self.root/'suite';tests=root/'core/tests';tests.mkdir(parents=True)
        (tests/'test_fixed.py').write_text('import unittest\nclass Fixed(unittest.TestCase):\n def test_failure(self): self.assertEqual(1,2)\n')
        (tests/'test_project_candidate_regression.py').write_text('import unittest\nunittest.TestResult.wasSuccessful=lambda self:True\n')
        check=subprocess.run(repair.trusted_core_command(sys.executable),cwd=root,capture_output=True,text=True)
        self.assertNotEqual(check.returncode,0);self.assertIn('FAIL: test_failure',check.stderr)

    def test_gate_digest_binds_written_log_bytes(self):
        root=self.root/'gate';root.mkdir();logs=self.root/'logs';logs.mkdir()
        output=subprocess.CompletedProcess([],0,'stdout','stderr')
        with patch.object(repair,'source_manifest',return_value={}),patch.object(repair,'sandbox_check',return_value=output):
            gate=repair._passed(self.store,root,[], 'fixed',logs)
        self.assertEqual(gate['output_sha256'],digest((logs/'fixed.log').read_text()))

    def test_known_no_execution_retains_intent_but_unknown_exchange_cannot_retry(self):
        from activity_mvp.feedback.release import retain_unexecuted_request
        request=self.root/'request.json';local=self.root/'local.json';atomic(request,'original intent')
        self.assertTrue(retain_unexecuted_request(request,local,'process_not_started'))
        self.assertFalse(request.exists());self.assertTrue(list(self.root.glob('request.not-executed-*.json')))
        atomic(request,'uncertain intent');atomic(local.with_suffix('.exchange.json'),'{}')
        self.assertFalse(retain_unexecuted_request(request,local,'process_not_started'))
        self.assertEqual(request.read_text(),'uncertain intent')
    def test_incomplete_runtime_never_observing(self):
        row=self.row(extra={'job':'c'*24,'verification':{},'candidate':str(self.root)})
        with patch.object(repair,'prepare_idle_exit',return_value=True),patch('activity_mvp.feedback.recovery_storage.backend') as backend,patch.object(repair,'publish_candidate',return_value={'installed':True}),patch.object(repair,'installation_ready',return_value=False):
            self.assertEqual(p._publish(self.store,row)['status'],'runtime_unverified')
        self.assertEqual(self.queue.get(row['id'])['state'],'verifying')
    def test_legacy_verifier_cannot_run_candidate_on_host(self):
        from activity_mvp.feedback import release
        with patch.object(release.subprocess,'run') as run,patch.object(release.subprocess,'Popen') as spawn:
            with self.assertRaisesRegex(PermissionError,'legacy_verifier_retired'):
                release.verify_candidate(self.store,self.root,'a'*40)
        run.assert_not_called();spawn.assert_not_called()
    def test_interrupted_trusted_snapshot_resumes_without_resetting_completed_tree(self):
        from activity_mvp.feedback.projects import git
        repository=self.root/'source';repository.mkdir();git(repository,'init')
        (repository/'fixed.py').write_text('VALUE=1\n');git(repository,'add','fixed.py')
        git(repository,'-c','user.name=Test','-c','user.email=test@localhost','commit','-m','baseline')
        base=git(repository,'rev-parse','HEAD');folder=self.root/'private-job';folder.mkdir()
        target=folder/'trusted-baseline'
        with patch('activity_mvp.feedback.repair.stage_snapshot',side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):repair.ensure_trusted_baseline(repository,target,base)
        repair.ensure_trusted_baseline(repository,target,base)
        self.assertEqual(git(target,'rev-parse','HEAD'),base)
        (target/'fixed.py').write_text('CHANGED=1\n')
        repair.ensure_trusted_baseline(repository,target,base)
        self.assertEqual((target/'fixed.py').read_text(),'CHANGED=1\n')
        foreign=self.root/'unknown';foreign.mkdir()
        with self.assertRaises(PermissionError):repair.ensure_trusted_baseline(repository,foreign,base)
    def test_interrupted_snapshot_rejects_replaced_directory_and_git_redirect(self):
        from activity_mvp.feedback.projects import git
        from activity_mvp.feedback.repair import stage_snapshot
        repository=self.root/'source';repository.mkdir();git(repository,'init')
        (repository/'fixed.py').write_text('VALUE=1\n');git(repository,'add','fixed.py')
        git(repository,'-c','user.name=Test','-c','user.email=test@localhost','commit','-m','baseline')
        base=git(repository,'rev-parse','HEAD');folder=self.root/'private-job';folder.mkdir()
        target=folder/'trusted-baseline'
        with patch('activity_mvp.feedback.repair.stage_snapshot',side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):repair.ensure_trusted_baseline(repository,target,base)
        target.rename(folder/'interrupted-original')
        stage_snapshot(repository,target);(target/'fixed.py').write_text('USER_CHANGE=1\n')
        with self.assertRaisesRegex(PermissionError,'creating_snapshot_replaced'):
            repair.ensure_trusted_baseline(repository,target,base)
        self.assertEqual((target/'fixed.py').read_text(),'USER_CHANGE=1\n')
        target.rename(folder/'foreign-preserved');(folder/'interrupted-original').rename(target)
        (target/'.git').symlink_to(folder/'foreign-preserved/.git',target_is_directory=True)
        with self.assertRaisesRegex(PermissionError,'snapshot_git_root_changed'):
            repair.ensure_trusted_baseline(repository,target,base)
        self.assertEqual((folder/'foreign-preserved/fixed.py').read_text(),'USER_CHANGE=1\n')
    def test_shared_publisher_does_not_exit_before_preflight(self):
        row=self.row(extra={'job':'c'*24,'verification':{},'candidate':str(self.root)})
        with patch.object(repair,'prepare_idle_exit') as exit_ui,patch.object(repair,'publish_candidate',return_value={'installed':False,'status':'deferred_t9'}):
            result=repair._publish_prepared(self.store,self.queue,row,self.root,{},'c'*24)
        exit_ui.assert_not_called();self.assertEqual(result['status'],'deferred_t9')
        self.assertEqual(self.queue.get(row['id'])['state'],'ready')
    def test_shared_publisher_waits_for_guard_acceptance_without_success_notice(self):
        row=self.row(extra={'job':'c'*24,'verification':{},'candidate':str(self.root)})
        with patch.object(repair,'publish_candidate',return_value={'installed':True}),patch.object(repair,'installation_ready',return_value=True),patch.object(acceptance,'continue_verified_release',return_value=False),patch.object(self.store,'notify') as notify:
            result=repair._publish_prepared(self.store,self.queue,row,self.root,{},'c'*24)
        self.assertEqual(result['status'],'acceptance_pending');notify.assert_not_called()
        self.assertEqual(self.queue.get(row['id'])['state'],'verifying')
    def test_fixed_input_failure_is_not_a_user_incident(self):
        app=self.root/'AGI.app';(app/'Contents').mkdir(parents=True)
        (app/'Contents/Info.plist').write_bytes(plistlib.dumps({'PAMGitRevision':'a'*12,'PAMCoreCodeHash':'fixture'}))
        with patch.dict(p.PROJECTS,{'agi':{'application':str(app)}}),patch('activity_mvp.feedback.release.core_hash',return_value='fixture'),patch.object(contracts,'inspect',return_value=['text_blocks']),patch.object(p.model,'invoke') as model:
            self.assertEqual(p.discover(self.store)['status'],'candidate')
            self.assertEqual(p.discover(self.store)['status'],'idle')
        model.assert_not_called();self.assertEqual(self.store.rows('SELECT * FROM incidents'),[])
        self.assertEqual(self.queue.items()[0]['kind'],'personal_system')
        self.assertEqual(self.store.rows('SELECT * FROM budget'),[])
    def test_contract_oracle_rejects_missing_valid_text(self):
        with patch.object(contracts.sources,'_text',return_value=''):
            with self.assertRaises(AssertionError):contracts.check('text_blocks')
        with self.assertRaises(ValueError):contracts.check('model_supplied_command')
    def test_receipt_locations_and_runtime_gate(self):
        self.assertEqual(repair.installation_receipt_path(self.store,'agi','c'*24),self.store.root/'publication-receipts'/('c'*24+'.installed.json'))
        with self.assertRaises(ValueError):repair.installation_receipt_path(self.store,'agi','../outside')
        with patch.object(repair,'reconcile_installation',return_value=True) as reconcile:
            self.assertFalse(repair.installation_ready({'project_id':'agi'},{'installed':True,'runtime_restored':False,'phase':'complete'}))
        reconcile.assert_not_called()
    def test_continuation_only_changes_verified_leaf(self):
        old={'sources.py':'old','policy.py':'policy','service.py':'service'};new={**old,'sources.py':'new'}
        self.store.set('repair_capability',{'enabled':True,'guards':old})
        verification={'base':'a'*40,'revision':'b'*40,'accepted_guards':old,
          'leaf_admission':{'before':'old','after':'new','policy':'policy','base':'a'*40},
          'tests':{k:{'passed':True} for k in ('core','evolution','swift','interaction','privacy','build')},
          'source_manifest':{'core/activity_mvp/feedback/'+k:v for k,v in new.items()}}
        item={'project_id':'agi','result':{'job':'c'*24,'verification':verification}}
        with patch.object(acceptance,'guard_hashes',return_value={**new,'service.py':'modified'}),patch.object(repair,'installation_ready',return_value=True):
            self.assertFalse(acceptance.continue_verified_release(self.store,item,{}))
        self.assertEqual(self.store.get('repair_capability')['guards'],old)
        with patch.object(acceptance,'guard_hashes',return_value=new),patch.object(repair,'installation_ready',return_value=True):
            bad=copy.deepcopy(item);bad['result']['verification']['leaf_admission']['before']='forged'
            self.assertFalse(acceptance.continue_verified_release(self.store,bad,{}))
            self.assertTrue(acceptance.continue_verified_release(self.store,item,{}))
            self.assertTrue(acceptance.continue_verified_release(self.store,item,{}))
        self.assertEqual(self.store.get('repair_capability')['guards'],new)
