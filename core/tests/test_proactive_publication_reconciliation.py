"""Synthetic publication histories; no live data, models, installs or network."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from activity_mvp.feedback import proactive as p, release, acceptance
from activity_mvp.feedback.store import Store, atomic, packed, private_directory, digest
from activity_mvp.feedback.work import WorkQueue, POLICY

PAIR_PROBE = p._publication_pair


class PublicationReconciliationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve(); self.home = self.root/'home'; self.home.mkdir()
        self.repo = self.root/'repo'; self.repo.mkdir()
        self.live = self.home/'Library/Application Support/Personal AI Monitor/core'
        self.store = Store(self.live/'data/feedback'); self.addCleanup(self.store.close)
        self.job = 'c'*24; self.app = self.root/'AGI.app'; (self.app/'Contents').mkdir(parents=True)
        (self.app/'Contents/Info.plist').write_bytes(b'synthetic paired marker')
        (self.app/'Contents/MacOS').mkdir();self.native=self.app/'Contents/MacOS/PersonalAIMonitor';self.native.write_bytes(b'synthetic native')
        s=self.native.lstat();self.native_identity=(s.st_dev,s.st_ino,s.st_mode,s.st_uid,s.st_nlink,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
        self.config = {'enabled':True,'python':sys.executable,'repository':str(self.repo),'live_core':str(self.live),'application':str(self.app)}
        self.store.set('config',self.config);self.store.set('autonomy',{'policy':POLICY,'paused':False})
        self.store.set('proactive',{'policy':p.POLICY}); self.store.set('proactive_last_day',p.day())
        self.key = self.store.reserve('repair');self.store.settle(self.key,'settled',{'synthetic':1})
        self.candidate = private_directory(self.store.root/'proactive-repairs'/self.job/'candidate')
        self.current = self.commit(self.repo,'current'); self.revision = self.commit(self.candidate,'candidate')
        self.verification = {'base':'a'*40,'revision':self.revision,
            'tests':{k:{'passed':True} for k in ('core','evolution','swift','interaction','privacy','build')},
            'accepted_guards':{'release.py':'9bc7b3e06c40bc51450f231c1454add93c5bb62940605de78e2a6468326befe4','recovery_storage.py':'f'*64}}
        self.requests = private_directory(self.store.root/'publication-requests')
        self.receipts = private_directory(self.store.root/'publication-receipts')
        self.request = self.requests/(self.job+'.json'); self.local = self.receipts/(self.job+'.json')
        self.runtime = self.receipts/(self.job+'.runtime.json')
        self.unexchanged = self.receipts/(self.job+'.exchange.unexchanged-200000000000.json')
        atomic(self.request,packed({'candidate':str(self.candidate),'base':'a'*40,'verification':self.verification,
            'config':{k:self.config[k] for k in ('repository','application','live_core')},'require_ui_absent':True,
            'service_was_loaded':True,'local_receipt':str(self.local),'recovery':'synthetic-external-reference'}))
        atomic(self.local,packed({'status':'direct_recovery_prepared','before':{'job':self.job,'revision':'a'*12,'core_hash':'f'*64},
            'recovery':'synthetic-external-reference','database_restore_allowed':False,'prepared_at':100}))
        atomic(self.runtime,packed({'runtime_restored':True,'errors':[],'at':300,'executor':{
            'release_controller_sha256':self.verification['accepted_guards']['release.py'],'recovery_storage_sha256':'f'*64}}))
        atomic(self.unexchanged,packed({'state':'no_new_exchange','candidate':str(self.candidate),
            'candidate_app_stage':'/Applications/.AGI-feedback-99999999.app',
            'candidate_code_stage':str(self.live.parent/'.AGI-publication-99999999'),
            'current_data_preserved':True,'previous_journal_sha256':None}))
        self.queue=WorkQueue(self.store); row=self.queue.enqueue('personal_system',p.PREFIX+'b'*64,'b'*64,'agi')
        self.row=self.queue.update(row['id'],'verifying',result={'job':self.job,'reservation':self.key,
            'attempt':1,'phase':'verification','publish_error':'RuntimeError','identity':{'head':'a'*40},
            'verification':self.verification,'candidate':str(self.candidate)})
        for mocker in (patch.object(Path,'home',return_value=self.home),
            patch.dict(p.PROJECTS,{'agi':{'repository':str(self.repo),'application':str(self.app)}}),
            patch.object(p,'source',return_value=({'head':self.current},'d'*64,{})),
            patch.object(p,'_publication_pair',return_value=self.native_identity),
            patch.object(p,'_installed_source'),
            patch.object(p.repair,'helper_hashes',return_value={'synthetic':'e'*64}),
            patch.object(acceptance,'release_ready',return_value=True),
            patch.object(p.model,'invoke',side_effect=AssertionError('no model dispatch')),
            patch.object(p.repair,'publish_candidate',side_effect=AssertionError('no publication replay'))):
            mocker.start();self.addCleanup(mocker.stop)

    def commit(self,root,text):
        p.git(root,'init');(root/'sample').write_text(text)
        p.git(root,'add','sample');p.git(root,'-c','user.name=Synthetic','-c','user.email=synthetic@localhost','commit','-m','fixture')
        return p.git(root,'rev-parse','HEAD')

    def mutate(self,path,**changes):
        value=json.loads(path.read_bytes());value.update(changes);atomic(path,packed(value))

    def unknown(self):
        before=self.store.rows('SELECT * FROM work_items')[0].copy()
        self.assertEqual(p.process_one(self.store)['status'],'unknown')
        self.assertEqual(self.store.rows('SELECT * FROM work_items')[0],before)
        self.assertEqual(self.store.rows("SELECT key FROM meta WHERE key LIKE 'proactive_publication_supersession:%'"),[])

    def test_legacy_pre_exchange_failure_becomes_obsolete_without_replay_or_cost_change(self):
        original=self.store.rows('SELECT * FROM work_items')[0];budget=self.store.rows('SELECT * FROM budget')
        originals={str(x):x.read_bytes() for x in [self.request,self.local,self.runtime,self.unexchanged]}
        self.assertEqual(p.process_one(self.store)['status'],'source_changed_publication_unknown')
        saved=self.store.rows('SELECT * FROM work_items')[0]
        for field in original:
            if field not in ('state','summary','updated'): self.assertEqual(saved[field],original[field])
        self.assertEqual(saved['state'],'blocked');self.assertFalse(p.ready(self.store))
        self.assertEqual(self.store.rows('SELECT * FROM budget'),budget)
        self.assertEqual({str(x):x.read_bytes() for x in [self.request,self.local,self.runtime,self.unexchanged]},originals)
        record=self.store.get('proactive_publication_supersession:'+digest([self.row['id'],'b'*64,'d'*64]))
        self.assertEqual(record['publication_outcome'],'unknown');self.assertNotIn('model_outcome',record)
        with self.assertRaisesRegex(PermissionError,'daily_budget'):self.store.reserve('repair')
        self.assertEqual(p.process_one(self.store)['status'],'idle')

    def test_same_source_or_same_candidate_or_ancestor_cannot_be_superseded(self):
        with patch.object(p,'source',return_value=({'head':self.current},'b'*64,{})):self.unknown()
        with patch.object(p,'source',return_value=({'head':self.revision},'d'*64,{})):self.unknown()
        self.assertFalse(p._candidate_outside_history(self.candidate,self.revision,self.revision))
        (self.candidate/'sample').write_text('descendant');p.git(self.candidate,'add','sample')
        p.git(self.candidate,'-c','user.name=Synthetic','-c','user.email=synthetic@localhost','commit','-m','descendant')
        self.assertFalse(p._candidate_outside_history(self.candidate,p.git(self.candidate,'rev-parse','HEAD'),self.revision))

    def test_candidate_need_not_exist_in_current_object_database_for_complete_absence_proof(self):
        with self.assertRaises(subprocess.CalledProcessError):p.git(self.repo,'cat-file','-e',self.revision)
        self.assertTrue(p._candidate_outside_history(self.repo,self.current,self.revision))
        (self.repo/'.git/info/grafts').write_text('')
        self.assertFalse(p._candidate_outside_history(self.repo,self.current,self.revision))

    def test_incomplete_shallow_or_truncated_history_fails_closed(self):
        for response in ('true',):
            with patch.object(p,'git',return_value=response):self.assertFalse(p._candidate_outside_history(self.repo,self.current,self.revision))
        for output in (self.current+' '+'e'*40, '\n'.join([self.current]*10001),self.current+' nonhex'):
            with patch.object(p,'git',side_effect=['false','.git/info/grafts',output]):
                self.assertFalse(p._candidate_outside_history(self.repo,self.current,self.revision))

    def test_any_additional_transaction_history_including_dangling_alias_refuses(self):
        for suffix in ('.exchange.json','.installed.json','.exchange.rolled-back.json','.pending.json','.rollback-history','.postboot-history','.unknown'):
            path=self.receipts/(self.job+suffix);path.symlink_to(self.root/'absent')
            try:self.unknown()
            finally:path.unlink()
        path=self.receipts/(self.job+'.exchange.unexchanged-201000000000.json');atomic(path,self.unexchanged.read_text())
        self.unknown()

    def test_real_pending_json_and_interrupted_temporary_preparation_refuse(self):
        folder=private_directory(self.store.root/'publication-pending')
        for name in (self.job+'.json','.'+self.job+'.json.interrupted',self.job):
            path=folder/name;atomic(path,'{}')
            try:self.unknown()
            finally:path.unlink()

    def test_future_terminal_diagnostics_are_bound_and_never_authorize_retry(self):
        guard='e'*64;self.verification['accepted_guards']['release.py']=guard
        result=copy.deepcopy(self.row['result']);result['verification']=self.verification
        self.row=self.queue.update(self.row['id'],'verifying',result=result)
        self.mutate(self.request,verification=self.verification)
        self.mutate(self.runtime,executor={'release_controller_sha256':guard,'recovery_storage_sha256':'f'*64})
        self.unknown()  # A new executor cannot borrow the legacy completion proof.
        for kind,fields in (('failure',{'outcome':'raised','at':301}),('process',{'outcome':'returned','returncode':1,'at':302})):
            atomic(self.receipts/(self.job+'.'+kind+'-99999999-303000000000.json'),packed({
                'job':self.job,'pid':99999999,'request_sha256':hashlib.sha256(self.request.read_bytes()).hexdigest(),**fields}))
        self.assertEqual(p.process_one(self.store)['status'],'source_changed_publication_unknown')

    def test_wrong_or_partial_bound_evidence_refuses(self):
        for path,change in ((self.unexchanged,{'previous_journal_sha256':'f'*64}),
            (self.unexchanged,{'candidate':'outside'}),(self.runtime,{'errors':['bootstrap_failed']}),
            (self.runtime,{'at':time.time()+1000}),(self.runtime,{'executor':{}}),
            (self.request,{'base':'e'*40}),(self.request,{'verification':{}}),
            (self.local,{'before':{'job':'e'*24,'revision':'a'*12}})):
            original=path.read_text();self.mutate(path,**change)
            try:self.unknown()
            finally:atomic(path,original)

    def test_live_or_indeterminate_pid_is_not_termination_proof(self):
        for effect in (None,PermissionError('synthetic permission')):
            with patch.object(p.os,'kill',side_effect=effect):self.unknown()

    def test_pause_cooldown_unaccepted_and_unpaired_sources_refuse(self):
        self.queue.update(self.row['id'],'verifying',next_check=time.time()+300);self.unknown()
        self.queue.update(self.row['id'],'verifying',next_check=0)
        with patch.object(acceptance,'release_ready',return_value=False):self.unknown()
        with patch.object(p,'_publication_pair',side_effect=PermissionError('unpaired')):self.unknown()
        self.store.set('autonomy',{'policy':POLICY,'paused':True})
        self.assertEqual(p.process_one(self.store)['status'],'paused')

    def test_work_or_evidence_race_is_not_overwritten(self):
        original=p._publication_files;calls=[]
        def changed(store,row):
            value=original(store,row);calls.append(1)
            if len(calls)==1:self.queue.update(row['id'],'verifying',summary='concurrent owner update')
            return value
        with patch.object(p,'_publication_files',side_effect=changed):self.assertEqual(p.process_one(self.store)['status'],'unknown')
        self.assertEqual(self.queue.get(self.row['id'])['summary'],'concurrent owner update')
        self.assertEqual(self.store.rows("SELECT key FROM meta WHERE key LIKE 'proactive_publication_supersession:%'"),[])

    def test_helper_drift_after_preflight_refuses_even_with_same_head_and_app(self):
        with patch.object(p.repair,'helper_hashes',side_effect=[{'before':'a'*64},{'after':'b'*64}]):self.unknown()

    def test_live_core_drift_in_final_gate_refuses(self):
        with patch.object(p,'_installed_source',side_effect=PermissionError('core drift')):self.unknown()

    def test_native_identity_drift_after_preflight_refuses(self):
        original=p._publication_files;calls=[]
        def changed(store,row):
            value=original(store,row);calls.append(1)
            if len(calls)==2:self.native.write_bytes(b'concurrent replacement')
            return value
        with patch.object(p,'_publication_files',side_effect=changed):self.unknown()

    def test_installation_reconciliation_keeps_precedence(self):
        with patch.object(p,'_reconcile',return_value=True),patch.object(p,'_supersede_publication') as supersede:
            self.assertEqual(p.process_one(self.store)['status'],'reconciled');supersede.assert_not_called()


class PublicationDiagnosticsTests(unittest.TestCase):
    def test_invalid_current_native_signature_is_not_an_accepted_pair(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();app=root/'AGI.app'
            (app/'Contents/MacOS').mkdir(parents=True)
            (app/'Contents/Info.plist').write_bytes(b'synthetic info')
            (app/'Contents/MacOS/PersonalAIMonitor').write_bytes(b'corrupt native')
            with (patch.dict(p.PROJECTS,{'agi':{'application':str(app)}}),
                  patch.object(p,'_installed_source'),
                  patch.object(p.subprocess,'run',side_effect=subprocess.CalledProcessError(1,'codesign')) as verify):
                with self.assertRaises(subprocess.CalledProcessError):PAIR_PROBE(None,{'head':'a'*40})
            self.assertIn('/usr/bin/codesign',verify.call_args.args[0])

    def test_failure_metadata_preserves_actual_exception_without_private_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();requests=private_directory(root/'publication-requests')
            receipts=private_directory(root/'publication-receipts');job='c'*24;request=requests/(job+'.json')
            atomic(request,packed({'local_receipt':str(receipts/(job+'.json'))}))
            secret='private secret /Users/example/customer.txt'
            error=ValueError(secret)
            with patch.object(release,'standalone_publish',side_effect=error):
                with self.assertRaises(ValueError) as raised:release._publish_with_diagnostic(request)
            self.assertIs(raised.exception,error)
            files=list(receipts.glob(job+'.failure-*.json'));self.assertEqual(len(files),1)
            record=json.loads(files[0].read_bytes());self.assertEqual(record['error'],'unknown')
            self.assertNotIn(secret,files[0].read_text());self.assertEqual(record['pid'],os.getpid())
            self.assertEqual(record['request_sha256'],hashlib.sha256(request.read_bytes()).hexdigest())
            release._publication_diagnostic(request,'process',os.getpid(),outcome='returned',returncode=1)
            release._publication_diagnostic(request,'process',os.getpid(),outcome='timeout',returncode=None)
            self.assertEqual(len(list(receipts.glob(job+'.process-*.json'))),2)

    def test_diagnostic_failure_does_not_replace_original_publication_error(self):
        error=PermissionError('authorization_revoked')
        with patch.object(release,'standalone_publish',side_effect=error),patch.object(release,'_publication_diagnostic',side_effect=OSError('disk')):
            with self.assertRaises(PermissionError) as raised:release._publish_with_diagnostic('unread')
        self.assertIs(raised.exception,error)


if __name__=='__main__':unittest.main()
