"""Failure tests exercise the native external copier and original-object swaps."""
from pathlib import Path
import fcntl,json,os,subprocess,sys,tempfile,unittest
from unittest.mock import patch
from activity_mvp.feedback import autonomy,project_repair,release,recovery_storage as storage
from activity_mvp.feedback.store import Store,packed,atomic,private_directory
import plistlib

class ExternalRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.local=self.root/'metadata';self.external=self.root/'external';self.external.mkdir()
        self.b=storage.backend();self.b.MOUNT=self.external;self.b.ROOT=self.root/'copier-state'
        self.bound={'uuid':self.b.UUID,'mount':str(self.external),'device':self.external.stat().st_dev}
        self.app=self.root/'AGI.app';self.app.mkdir();(self.app/'binary').write_bytes(b'old signed fixture')
        self.code=self.root/'old.py';self.code.write_text('old code')
        self.before={'revision':'before','core_hash':'oldhash','job':'test'}
        for obj,name,value in [(storage,'backend',self.b),(self.b,'volume',self.bound),(self.b,'verify_apps',None),(os.path,'ismount',True)]:
            p=patch.object(obj,name,return_value=value);p.start();self.addCleanup(p.stop)
    def writer(self,payload,copy,guard):
        copy(self.code,payload/'old.py');return {'database_restore_allowed':False}
    def prepare(self):return storage.prepare_external(self.local,'test',self.app,self.before,self.writer)
    def test_direct_external_readback_and_repeat_without_new_payload(self):
        target,receipt=self.prepare();first=self.b.tree_identity(target/'payload')
        self.assertTrue(target.is_relative_to(self.external));self.assertFalse((self.local/'recoveries').exists())
        again,other=self.prepare();self.assertEqual((target,receipt),(again,other));self.assertEqual(first,self.b.tree_identity(target/'payload'))
        self.assertEqual((target/'prepared.json').read_bytes(),receipt.read_bytes());storage.check_external(target,receipt)
    def test_offline_wrong_readonly_and_insufficient_space_leave_originals(self):
        for error in ['offline','wrong_volume','read_only']:
            with self.subTest(error=error),patch.object(self.b,'volume',side_effect=self.b.Unsafe(error)):
                with self.assertRaises(storage.RecoveryDeferred):self.prepare()
            self.assertFalse(list(self.external.rglob('payload')))
            pending=json.loads((self.local/'publication-pending/test.json').read_text())
            self.assertNotIn('old code',json.dumps(pending));self.assertNotIn('old signed fixture',json.dumps(pending))
        with patch.object(storage.shutil,'disk_usage',return_value=type('Space',(),{'free':1})()):
            with self.assertRaisesRegex(storage.RecoveryDeferred,'insufficient_space'):self.prepare()
        self.assertEqual(self.code.read_text(),'old code');self.assertTrue((self.app/'binary').exists())
    def test_source_change_keeps_external_partial_and_original(self):
        def writer(payload,copy,guard):
            self.writer(payload,copy,guard);self.code.write_text('later code');return {}
        with self.assertRaisesRegex(storage.RecoveryDeferred,'source_changed'):
            storage.prepare_external(self.local,'test',self.app,self.before,writer)
        self.assertEqual(self.code.read_text(),'later code');self.assertTrue(list(self.external.rglob('old.py')))
    def test_readback_conflict_never_overwrites(self):
        target,receipt=self.prepare();(target/'payload/old.py').write_text('conflicting target')
        with self.assertRaises(storage.RecoveryDeferred):self.prepare()
        self.assertEqual((target/'payload/old.py').read_text(),'conflicting target');self.assertEqual(self.code.read_text(),'old code')
    def test_replaced_same_content_original_is_not_authorized_for_deletion(self):
        target,receipt=self.prepare();proof=storage.check_external(target,receipt);check=storage.original_checker(proof)
        self.assertEqual(check(self.code),release._token(self.code))
        replacement=self.code.with_suffix('.replacement');replacement.write_text(self.code.read_text());os.replace(replacement,self.code)
        with self.assertRaisesRegex(ValueError,'prepared_original_changed'):check(self.code)
    def test_interrupted_local_receipt_can_finish_without_recopied_payload(self):
        original=self.b.atomic
        def fail(path,data):
            if Path(path).parent.name=='publication-receipts':raise OSError('injected receipt interruption')
            return original(path,data)
        with patch.object(self.b,'atomic',side_effect=fail):
            with self.assertRaises(storage.RecoveryDeferred):self.prepare()
        target=next(self.external.rglob('release-test'));objects=self.b.tree_identity(target/'payload')
        finished,receipt=self.prepare();self.assertEqual(target,finished);self.assertEqual(objects,self.b.tree_identity(target/'payload'))
        storage.check_external(target,receipt)

class PublicationInterruptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve()
        self.live=self.root/'live';self.candidate=self.root/'candidate';self.app=self.root/'AGI.app';self.app.mkdir()
        for base in [self.live,self.candidate/'core']:
            for name in release.FOLDERS:(base/name).mkdir(parents=True)
            for name in release.FILES:(base/name).write_text('unchanged')
        self.rel='activity_mvp/code.py';(self.live/self.rel).write_text('old code');(self.candidate/'core'/self.rel).write_text('new code')
        (self.app/'old').write_text('old app');new=self.candidate/'build/AGI.app';new.mkdir(parents=True);(new/'new').write_text('new app')
        self.data=self.live/'data/current.txt';self.data.parent.mkdir();self.data.write_text('later user data')
        self.journal=self.root/'metadata/exchange.json';self.recovery=self.root/'offline-t9';self.recovery.mkdir()
    def test_unplug_after_app_exchange_rolls_back_without_external_read(self):
        old=release._token(self.live/self.rel);app=release._token(self.app);online=[True]
        def guard():
            if not online[0]:raise ValueError('external unplugged')
        def validate():online[0]=False;self.data.write_text('new data while publishing')
        with self.assertRaisesRegex(ValueError,'unplugged'):
            release.exchange_pair(self.candidate,self.live,self.app,self.recovery,[self.rel],release.core_hash(self.candidate/'core'),validate,guard,self.journal)
        self.assertEqual(release._token(self.live/self.rel),old);self.assertEqual(release._token(self.app),app)
        self.assertEqual(self.data.read_text(),'new data while publishing');self.assertTrue(json.loads(self.journal.with_name('exchange.rolled-back.json').read_text())['code_restored'])
    def test_killed_after_each_native_swap_recovers_original_inodes(self):
        for kill_after in [1,2]:
            with self.subTest(kill_after=kill_after):
                old=release._token(self.live/self.rel);app=release._token(self.app)
                script='''import os,sys
from pathlib import Path
from activity_mvp.feedback import release
root=Path(sys.argv[1]);real=release._swap;count=[0]
def kill(left,right):
 real(left,right);count[0]+=1
 if count[0]==int(sys.argv[2]):os._exit(77)
release._swap=kill
release.exchange_pair(root/'candidate',root/'live',root/'AGI.app',root/'offline-t9',['activity_mvp/code.py'],release.core_hash(root/'candidate/core'),lambda:None,journal=root/'metadata/exchange.json')
'''
                p=subprocess.run([sys.executable,'-B','-c',script,str(self.root),str(kill_after)],capture_output=True,text=True)
                self.assertEqual(p.returncode,77,p.stderr);release.recover_exchange(self.journal,self.live,self.app)
                self.assertEqual(release._token(self.live/self.rel),old);self.assertEqual(release._token(self.app),app);self.assertEqual(self.data.read_text(),'later user data')
                # Derived candidate stages are deliberately not a second old
                # backup; remove this test's staged candidates between cases.
                import shutil
                for stage in self.root.glob('.AGI-*'):shutil.rmtree(stage)
                self.journal.unlink()
    def test_git_commit_then_receipt_failure_keeps_pair_and_resumes_once(self):
        repo=self.root/'repo';repo.mkdir()
        def git(*args):return release.run(['git',*args],repo).stdout.strip()
        git('init');git('config','user.name','fixture');git('config','user.email','fixture@localhost')
        (repo/'source').write_text('old');git('add','source');git('commit','-m','old');base=git('rev-parse','HEAD')
        (repo/'source').write_text('new');git('add','source');git('commit','-m','new');revision=git('rev-parse','HEAD');git('reset','--hard',base)
        binding={'repository':str(repo),'base':base,'revision':revision}
        def commit():git('merge','--ff-only',revision);raise OSError('injected receipt failure after ff')
        expected=release.core_hash(self.candidate/'core')
        with self.assertRaisesRegex(ValueError,'committed_receipt_pending'):
            release.exchange_pair(self.candidate,self.live,self.app,self.recovery,[self.rel],expected,lambda:None,journal=self.journal,commit=commit,commit_info=binding)
        self.assertEqual(git('rev-parse','HEAD'),revision);self.assertEqual((self.live/self.rel).read_text(),'new code');self.assertTrue((self.app/'new').exists())
        self.data.write_text('data after commit')
        result=release.exchange_pair(self.candidate,self.live,self.app,self.recovery,[self.rel],expected,lambda:None,journal=self.journal,
            commit=lambda:self.fail('committed retry must not rerun commit'),commit_info=binding)
        self.assertTrue(result['resumed']);self.assertTrue(result['original_cleanup_complete']);self.assertEqual(self.data.read_text(),'data after commit')

    def test_pause_before_swap_preserves_pair(self):
        old=release._token(self.live/self.rel);app=release._token(self.app)
        with self.assertRaisesRegex(PermissionError,'paused'):
            release.exchange_pair(self.candidate,self.live,self.app,self.recovery,[self.rel],release.core_hash(self.candidate/'core'),lambda:None,
                guard=lambda:(_ for _ in ()).throw(PermissionError('paused')),journal=self.journal)
        self.assertEqual(release._token(self.live/self.rel),old);self.assertEqual(release._token(self.app),app)
    def test_swap_completed_then_directory_flush_fails_recovers_pair(self):
        old=release._token(self.live/self.rel);app=release._token(self.app);real=release._swap;calls=[0]
        def failed_flush(left,right):
            real(left,right);calls[0]+=1
            if calls[0]==1:raise OSError('injected directory flush failure')
        with patch.object(release,'_swap',side_effect=failed_flush),self.assertRaisesRegex(OSError,'flush failure'):
            release.exchange_pair(self.candidate,self.live,self.app,self.recovery,[self.rel],release.core_hash(self.candidate/'core'),lambda:None,journal=self.journal)
        self.assertEqual(release._token(self.live/self.rel),old);self.assertEqual(release._token(self.app),app)

class PublicationEntryTests(unittest.TestCase):
    def setUp(self):
        PublicationInterruptionTests.setUp(self);self.b=storage.backend();self.b.MOUNT=self.root/'external';self.b.MOUNT.mkdir();self.b.ROOT=self.root/'backup-state'
        self.bound={'uuid':self.b.UUID,'mount':str(self.b.MOUNT),'device':self.b.MOUNT.stat().st_dev}
        for obj,name,value in [(storage,'backend',self.b),(self.b,'volume',self.bound),(self.b,'verify_apps',None),(os.path,'ismount',True)]:
            p=patch.object(obj,name,return_value=value);p.start();self.addCleanup(p.stop)
        self.repo=self.root/'repo';self.repo.mkdir()
        def git(*args):return release.run(['git',*args],self.repo).stdout.strip()
        git('init');git('config','user.name','fixture');git('config','user.email','fixture@localhost')
        (self.repo/'source').write_text('old');git('add','source');git('commit','-m','old');self.base=git('rev-parse','HEAD')
        (self.repo/'source').write_text('new');git('commit','-am','new');self.revision=git('rev-parse','HEAD');git('reset','--hard',self.base)
        self.expected=release.core_hash(self.candidate/'core')
        for app,revision,core in [(self.app,self.base,release.core_hash(self.live)),(self.candidate/'build/AGI.app',self.revision,self.expected)]:
            (app/'Contents').mkdir();(app/'Contents/Info.plist').write_bytes(plistlib.dumps({'PAMCoreCodeHash':core,'PAMGitRevision':revision[:7],'PAMGitDirty':False}))
        self.metadata=self.live/'data/feedback'
        with Store(self.metadata) as s:
            s.set('config',{'enabled':True});s.set('autonomy',{'policy':autonomy.POLICY,'paused':False})
        before={'revision':self.base,'core_hash':release.core_hash(self.live),'job':'entry'}
        def writer(payload,copy,guard):copy(self.live/self.rel,payload/self.rel);return {}
        self.external,self.local=storage.prepare_external(self.metadata,'entry',self.app,before,writer)
        self.journal=self.local.with_suffix('.exchange.json');self.request=private_directory(self.metadata/'publication-requests')/'entry.json'
        verified={'revision':self.revision,'core_hash':self.expected,'application_identity':self.b.tree_identity(self.candidate/'build/AGI.app'),
                  'application_manifest_sha256':self.b.fingerprint(self.b.manifest(self.candidate/'build/AGI.app'))}
        self.r={'config':{'repository':str(self.repo),'live_core':str(self.live),'application':str(self.app)},'candidate':str(self.candidate),
                'recovery':str(self.external),'local_receipt':str(self.local),'base':self.base,'verification':verified,'service_was_loaded':True}
        atomic(self.request,packed(self.r));self.boot_fail=False
        self.realrun=release.run;self.realsubprocess=subprocess.run
        def run(args,cwd=None,timeout=900):
            args=list(map(str,args))
            if args[0] in ['/bin/launchctl','/usr/bin/open','/usr/bin/codesign']:
                if self.boot_fail and args[:2]==['/bin/launchctl','bootstrap']:raise subprocess.CalledProcessError(1,args)
                return subprocess.CompletedProcess(args,0,'','')
            if '-m' in args and 'activity_mvp.feedback.cli' in args:return subprocess.CompletedProcess(args,0,'{"status":"ok"}','')
            if args[:3]==['git','diff','--name-only']:return subprocess.CompletedProcess(args,0,'core/'+self.rel+'\n','')
            if args[:2]==['git','fetch']:return self.realrun(['git','fetch',str(self.repo),self.revision],cwd,timeout)
            return self.realrun(args,cwd,timeout)
        def subprocess_run(args,**kwargs):
            if str(args[0]) in ['/bin/launchctl','/usr/bin/pkill']:return subprocess.CompletedProcess(args,1,'','')
            return self.realsubprocess(args,**kwargs)
        p=patch.object(release,'run',side_effect=run);p.start();self.addCleanup(p.stop)
        p=patch.object(subprocess,'run',side_effect=subprocess_run);p.start();self.addCleanup(p.stop)
    def pause_autonomy(self):
        with Store(self.metadata) as current:
            setting=current.get('autonomy');setting['paused']=True;current.set('autonomy',setting)
            self.assertTrue(current.active());self.assertFalse(autonomy.active(current))
    def test_autonomy_pause_prevents_standalone_exchange(self):
        old=release._token(self.live/self.rel);app=release._token(self.app);self.pause_autonomy()
        with self.assertRaisesRegex(PermissionError,'authorization_revoked'):release.standalone_publish(self.request)
        self.assertEqual(release._token(self.live/self.rel),old);self.assertEqual(release._token(self.app),app)
        self.assertFalse(self.journal.exists())
        self.assertEqual(json.loads(self.local.with_suffix('.pending.json').read_text())['status'],'paused')
    def test_autonomy_pause_during_exchange_restores_originals_and_new_data(self):
        old=release._token(self.live/self.rel);app=release._token(self.app);swap=release._swap;calls=[]
        def pause_after_first_swap(left,right):
            swap(left,right);calls.append(str(left))
            if len(calls)==1:
                self.data.write_text('new data after first swap');self.pause_autonomy()
        with patch.object(release,'_swap',side_effect=pause_after_first_swap),self.assertRaisesRegex(PermissionError,'authorization_revoked'):
            release.standalone_publish(self.request)
        self.assertEqual(release._token(self.live/self.rel),old);self.assertEqual(release._token(self.app),app)
        self.assertEqual(self.data.read_text(),'new data after first swap')
        self.assertEqual(json.loads(self.journal.read_text())['state'],'rolled_back')
        self.assertEqual(self.realrun(['git','rev-parse','HEAD'],self.repo).stdout.strip(),self.base)
        self.assertFalse(self.local.with_suffix('.installed.json').exists())
    def test_autonomy_pause_at_cleanup_retains_committed_pair_and_originals(self):
        retained=storage.retained_guard
        def pause_during_readback(*args):
            check=retained(*args)
            def guard():
                check();self.data.write_text('new data after commit');self.pause_autonomy()
            return guard
        with patch.object(storage,'retained_guard',side_effect=pause_during_readback),self.assertRaisesRegex(PermissionError,'authorization_revoked'):
            release.standalone_publish(self.request)
        self.assertEqual(self.realrun(['git','rev-parse','HEAD'],self.repo).stdout.strip(),self.revision)
        self.assertEqual((self.live/self.rel).read_text(),'new code');self.assertTrue((self.app/'new').exists())
        self.assertEqual(self.data.read_text(),'new data after commit')
        record=json.loads(self.journal.read_text());self.assertEqual(record['state'],'accepted')
        self.assertTrue(all(Path(e['stage']).exists() for e in record['entries'] if e['old']))
        self.assertEqual(json.loads(self.local.with_suffix('.installed.json').read_text())['phase'],'runtime_verified_cleanup_pending')
    def test_production_entry_bootstrap_failure_preserves_originals_then_resumes(self):
        self.boot_fail=True;first=release.standalone_publish(self.request)
        self.assertTrue(first['installed']);self.assertFalse(first['runtime_restored']);self.assertFalse(first['original_cleanup_complete'])
        record=json.loads(self.journal.read_text());self.assertTrue(all(Path(e['stage']).exists() for e in record['entries'] if e['old']))
        self.data.write_text('data after runtime failure');self.boot_fail=False
        second=release.standalone_publish(self.request)
        self.assertTrue(second['runtime_restored']);self.assertTrue(second['original_cleanup_complete']);self.assertEqual(self.data.read_text(),'data after runtime failure')
        self.assertEqual((self.external/'installed.json').read_bytes(),self.local.with_suffix('.installed.json').read_bytes())
    def test_added_unarchived_member_in_original_app_blocks_cleanup(self):
        self.boot_fail=True;release.standalone_publish(self.request)
        record=json.loads(self.journal.read_text());old=next(Path(e['stage']) for e in record['entries'] if Path(e['target'])==self.app)
        (old/'later-unique-file').write_text('never archived');self.boot_fail=False
        with self.assertRaisesRegex(ValueError,'cleanup_member_identity_changed'):release.standalone_publish(self.request)
        self.assertEqual((old/'later-unique-file').read_text(),'never archived');self.assertTrue((self.app/'new').exists())
    def interrupt_before_commit(self):
        script='''import os,sys,json
from pathlib import Path
from activity_mvp.feedback import release
r=json.loads(Path(sys.argv[1]).read_text());real=release._swap
def kill(left,right):real(left,right);os._exit(77)
release._swap=kill
release.exchange_pair(Path(r['candidate']),Path(r['config']['live_core']),Path(r['config']['application']),Path(r['recovery']),['activity_mvp/code.py'],r['verification']['core_hash'],lambda:None,journal=Path(r['local_receipt']).with_suffix('.exchange.json'),commit_info={'repository':r['config']['repository'],'base':r['base'],'revision':r['verification']['revision']})
'''
        process=subprocess.run([sys.executable,'-B','-c',script,str(self.request)],capture_output=True,text=True)
        self.assertEqual(process.returncode,77,process.stderr)
    def test_real_entry_restores_precommit_originals_while_t9_is_offline(self):
        old=release._token(self.live/self.rel);app=release._token(self.app);self.interrupt_before_commit()
        with patch.object(self.b,'volume',side_effect=self.b.Unsafe('offline')):result=release.standalone_publish(self.request)
        self.assertFalse(result['installed']);self.assertEqual(result['status'],'deferred_t9')
        self.assertEqual(release._token(self.live/self.rel),old);self.assertEqual(release._token(self.app),app);self.assertEqual(self.data.read_text(),'later user data')
    def test_paused_entry_restores_precommit_originals_without_t9_or_new_install(self):
        old=release._token(self.live/self.rel);app=release._token(self.app);self.interrupt_before_commit()
        self.pause_autonomy();self.data.write_text('new data after interruption')
        with patch.object(self.b,'volume',side_effect=self.b.Unsafe('offline')) as volume,self.assertRaisesRegex(PermissionError,'authorization_revoked'):
            release.standalone_publish(self.request)
        volume.assert_not_called()
        self.assertEqual(release._token(self.live/self.rel),old);self.assertEqual(release._token(self.app),app)
        self.assertEqual(self.data.read_text(),'new data after interruption')
        self.assertEqual(json.loads(self.journal.read_text())['state'],'rolled_back')
        self.assertFalse(self.local.with_suffix('.installed.json').exists())
    def fail_after_native_swaps(self):
        previous=release.run
        def fail(args,*a,**kw):
            if list(map(str,args))[:2]==['git','merge']:
                raise subprocess.CalledProcessError(1,args,stderr='injected required-write failure')
            return previous(args,*a,**kw)
        with patch.object(release,'run',side_effect=fail),self.assertRaises(subprocess.CalledProcessError):
            release.standalone_publish(self.request)
        self.assertEqual(json.loads(self.journal.read_text())['state'],'rolled_back')
        self.assertEqual(self.realrun(['git','rev-parse','HEAD'],self.repo).stdout.strip(),self.base)
        return {p:p.read_bytes() for p in [self.request,self.local,self.journal,self.local.with_suffix('.runtime.json'),self.local.with_suffix('.exchange.rolled-back.json')]}
    def retry_known_rollback(self):
        # Native publication uses a new process; give its staging paths a new
        # process identity while exercising actual filesystem swaps in this test.
        self.retry_offset=getattr(self,'retry_offset',0)+100000
        next_pid=os.getpid()+self.retry_offset
        with patch.object(release.os,'getpid',return_value=next_pid):
            return release.standalone_publish(self.request)
    def test_known_rollback_same_request_reconciles_only_root_ctime(self):
        original=self.b.tree_identity(self.external/'payload');prior=self.fail_after_native_swaps()
        old_journal=json.loads(prior[self.journal]);self.data.write_text('new data after failed installation')
        result=self.retry_known_rollback()
        self.assertTrue(result['installed']);self.assertTrue(result['runtime_restored']);self.assertTrue(result['original_cleanup_complete'])
        self.assertEqual(self.data.read_text(),'new data after failed installation')
        self.assertEqual(self.b.tree_identity(self.external/'payload'),original)
        self.assertEqual(self.local.read_bytes(),prior[self.local]);self.assertEqual((self.external/'prepared.json').read_bytes(),prior[self.local])
        self.assertEqual(self.request.read_bytes(),prior[self.request])
        self.assertEqual(self.realrun(['git','rev-parse','HEAD'],self.repo).stdout.strip(),self.revision)
        history=list((self.local.parent/(self.local.stem+'.rollback-history')).glob('*.json'))
        self.assertEqual(len(history),1);saved=json.loads(history[0].read_text())
        for path,content in prior.items():self.assertEqual(saved['snapshots'][str(path)]['text'].encode(),content)
        for entry in old_journal['entries']:
            self.assertEqual(release._token(Path(entry['stage'])),entry['new'])
    def test_known_rollback_replaced_same_content_original_is_rejected(self):
        self.fail_after_native_swaps();target=self.live/self.rel
        replacement=target.with_suffix('.replacement')
        import shutil
        shutil.copy2(target,replacement);os.replace(replacement,target)
        with self.assertRaisesRegex(ValueError,'rollback_original_identity_changed'):self.retry_known_rollback()
        self.assertEqual(self.realrun(['git','rev-parse','HEAD'],self.repo).stdout.strip(),self.base)
        self.assertFalse(self.local.with_suffix('.installed.json').exists())
    def test_known_rollback_descendant_ctime_change_is_rejected(self):
        self.fail_after_native_swaps();child=self.app/'old';mode=child.stat().st_mode
        os.chmod(child,0o600 if mode&0o777!=0o600 else 0o644);os.chmod(child,mode)
        with self.assertRaisesRegex(ValueError,'rollback_original_identity_changed'):self.retry_known_rollback()
        self.assertFalse(self.local.with_suffix('.installed.json').exists())
    def test_known_rollback_root_metadata_change_is_rejected(self):
        self.fail_after_native_swaps();os.chmod(self.live/self.rel,0o700)
        with self.assertRaisesRegex(ValueError,'rollback_original_identity_changed'):self.retry_known_rollback()
        self.assertFalse(self.local.with_suffix('.installed.json').exists())
    def test_known_rollback_wrong_transaction_is_rejected(self):
        self.fail_after_native_swaps();record=json.loads(self.journal.read_text());record['commit_info']['revision']='f'*40
        atomic(self.journal,packed(record))
        with self.assertRaisesRegex(ValueError,'rollback_request_binding_changed'):self.retry_known_rollback()
        self.assertFalse(self.local.with_suffix('.installed.json').exists())
    def test_known_rollback_changed_retained_candidate_is_rejected(self):
        self.fail_after_native_swaps();record=json.loads(self.journal.read_text())
        entry=next(e for e in record['entries'] if Path(e['target'])!=self.app)
        Path(entry['stage']).write_text('different staged code')
        with self.assertRaisesRegex(ValueError,'rollback_retained_candidate_changed'):self.retry_known_rollback()
        self.assertFalse(self.local.with_suffix('.installed.json').exists())
    def test_known_rollback_pause_prevents_reconciliation_or_exchange(self):
        prior=self.fail_after_native_swaps();self.pause_autonomy()
        with self.assertRaisesRegex(PermissionError,'authorization_revoked'):self.retry_known_rollback()
        self.assertEqual(self.journal.read_bytes(),prior[self.journal])
        self.assertFalse((self.local.parent/(self.local.stem+'.rollback-history')).exists())
        self.assertFalse(self.local.with_suffix('.installed.json').exists())
    def test_known_rollback_root_ctime_after_record_is_rejected(self):
        self.fail_after_native_swaps();target=self.live/self.rel;mode=target.stat().st_mode
        os.chmod(target,0o600 if mode&0o777!=0o600 else 0o644);os.chmod(target,mode)
        with self.assertRaisesRegex(ValueError,'rollback_ctime_outside_recorded_exchange'):self.retry_known_rollback()
        self.assertFalse(self.local.with_suffix('.installed.json').exists())
    def test_reconciliation_interrupted_before_exclusive_publish_can_continue(self):
        prior=self.fail_after_native_swaps()
        with patch.object(self.b,'exclusive_move',side_effect=OSError('injected metadata interruption')):
            with self.assertRaisesRegex(OSError,'metadata interruption'):self.retry_known_rollback()
        history=self.local.parent/(self.local.stem+'.rollback-history')
        pending={p:p.read_bytes() for p in history.glob('*.pending')}
        self.assertTrue(pending);self.assertFalse(list(history.glob('*.json')))
        self.assertEqual(self.journal.read_bytes(),prior[self.journal])
        self.assertTrue(self.retry_known_rollback()['installed'])
        for path,content in pending.items():self.assertEqual(path.read_bytes(),content)
    def test_retry_before_first_intent_keeps_previous_journal_and_can_continue(self):
        prior=self.fail_after_native_swaps();previous=release._rollback_original_guard
        def pause_before_intent(*args):
            checker=previous(*args)
            def fail(path):self.pause_autonomy();raise PermissionError('injected pause before intent')
            fail.cleanup=checker.cleanup
            return fail
        with patch.object(release,'_rollback_original_guard',side_effect=pause_before_intent):
            with self.assertRaisesRegex(PermissionError,'pause before intent'):self.retry_known_rollback()
        self.assertEqual(self.journal.read_bytes(),prior[self.journal])
        self.assertTrue(list(self.journal.parent.glob(self.journal.stem+'.unexchanged-*.json')))
        with Store(self.metadata) as current:
            setting=current.get('autonomy');setting['paused']=False;current.set('autonomy',setting)
        self.assertTrue(self.retry_known_rollback()['installed'])
    def test_same_rollback_request_reformat_cannot_create_new_reconciliation(self):
        self.fail_after_native_swaps();r=json.loads(self.request.read_text());prepared=storage.check_external(self.external,self.local)
        release._rollback_original_guard(self.request,r,prepared,self.journal)
        self.request.write_text(json.dumps(r,indent=3))
        with self.assertRaisesRegex(ValueError,'rollback_reconciliation_conflict'):
            release._rollback_original_guard(self.request,r,prepared,self.journal)
        self.assertEqual(len(list((self.local.parent/(self.local.stem+'.rollback-history')).glob('*.json'))),1)
    def test_precommit_interruption_records_completion_time_and_resumes(self):
        self.interrupt_before_commit();old=json.loads(self.journal.read_text())
        binding={'repository':str(self.repo),'base':self.base,'revision':self.revision}
        release.recover_exchange(self.journal,self.live,self.app,binding)
        restored=json.loads(self.journal.read_text())
        self.assertEqual(restored['at'],old['at']);self.assertGreater(restored['rollback_completed_at'],old['at'])
        self.assertTrue(self.retry_known_rollback()['installed'])
        self.assertEqual(self.data.read_text(),'later user data')
    def test_unknown_exchange_state_is_not_reclassified_or_replayed(self):
        self.fail_after_native_swaps();record=json.loads(self.journal.read_text());record['state']='unknown'
        atomic(self.journal,packed(record));original=self.journal.read_bytes()
        with self.assertRaisesRegex(ValueError,'publication_state_unknown'):self.retry_known_rollback()
        self.assertEqual(self.journal.read_bytes(),original)
        self.assertFalse((self.local.parent/(self.local.stem+'.rollback-history')).exists())
        self.assertFalse(self.local.with_suffix('.installed.json').exists())
    def test_candidate_replaced_by_other_signed_app_is_rejected_before_swap(self):
        old=release._token(self.live/self.rel);(self.candidate/'build/AGI.app/new').write_text('other signed app')
        with self.assertRaisesRegex(ValueError,'candidate_application_changed'):release.standalone_publish(self.request)
        self.assertEqual(release._token(self.live/self.rel),old);self.assertTrue((self.app/'old').exists())

class PublicationPostbootTests(unittest.TestCase):
    """Native swaps with a simulated prior boot's transient device number.

    Boot, volume, lifecycle, signature and CLI probes use fixture substitutes.
    Original inodes, metadata, content, immutable receipts, Git and exchanges
    use the filesystem; the separate cross-version run checks real signatures.
    """
    setUp=PublicationEntryTests.setUp
    fail_after_native_swaps=PublicationEntryTests.fail_after_native_swaps
    retry_known_rollback=PublicationEntryTests.retry_known_rollback
    pause_autonomy=PublicationEntryTests.pause_autonomy
    def previous_boot(self):
        self.fail_after_native_swaps()
        r=json.loads(self.request.read_text());proof=json.loads(self.local.read_text());record=json.loads(self.journal.read_text())
        self.current_device=self.root.stat().st_dev;self.old_device=self.current_device+100000
        for row in r['verification']['application_identity'].values():row[0]=self.old_device
        logs=self.root/'qualified';logs.mkdir()
        r['verification'].update(operator_release=True,acceptance_evidence_path=str(logs/'acceptance-evidence.json'))
        atomic(logs/'release-verification.json',packed(r['verification']))
        for bound in proof['source_bindings'].values():
            for row in bound['identity'].values():row[0]=self.old_device
        for entry in record['entries']:
            entry['old'][0]=self.old_device;entry['new'][0]=self.old_device
        atomic(self.request,packed(r));atomic(self.local,packed(proof));atomic(self.external/'prepared.json',packed(proof));atomic(self.journal,packed(record))
        self.boot={'at':record['at']+0.000001,'uuid':'11111111-2222-3333-4444-555555555555'}
        self.volume={'uuid':'AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE','mount':str(self.root),'device':self.current_device,'identifier':'synthetic-data'}
        self.enterContext(patch.object(release,'_publication_boot_identity',return_value=self.boot))
        self.volume_probe=self.enterContext(patch.object(self.b,'source_volume',return_value=self.volume))
        self.prior={p:p.read_bytes() for p in [self.request,self.local,self.journal,self.external/'prepared.json']}
        return r,proof
    def history(self):return self.local.parent/(self.local.stem+'.postboot-history')
    def context(self):
        return release._postboot_rollback_context(self.request,json.loads(self.request.read_text()),json.loads(self.local.read_text()),self.journal)
    def test_postboot_known_rollback_continues_native_pair_without_rewriting_old_proofs(self):
        self.previous_boot();self.data.write_text('new data after simulated reboot')
        result=self.retry_known_rollback()
        self.assertTrue(result['installed']);self.assertTrue(result['original_cleanup_complete'])
        self.assertEqual(self.data.read_text(),'new data after simulated reboot')
        for path in [self.request,self.local,self.external/'prepared.json']:self.assertEqual(path.read_bytes(),self.prior[path])
        receipt=result['executor']['postboot_revalidation'];saved=json.loads(Path(receipt['receipt']).read_text())
        self.assertFalse(saved['legacy_source_uuid_recorded']);self.assertEqual(saved['boot'],self.boot)
        self.assertEqual(saved['snapshots'][str(self.journal)]['text'].encode(),self.prior[self.journal])
        self.assertEqual(self.realrun(['git','rev-parse','HEAD'],self.repo).stdout.strip(),self.revision)
    def test_postboot_runtime_failure_can_finish_same_committed_receipt(self):
        self.previous_boot();self.boot_fail=True
        first=self.retry_known_rollback();self.assertTrue(first['installed']);self.assertFalse(first['runtime_restored'])
        self.data.write_text('data after committed runtime failure');self.boot_fail=False
        second=self.retry_known_rollback();self.assertTrue(second['runtime_restored']);self.assertTrue(second['original_cleanup_complete'])
        self.assertEqual(first['executor']['postboot_revalidation'],second['executor']['postboot_revalidation'])
        self.assertEqual(self.data.read_text(),'data after committed runtime failure')
    def test_postboot_new_boot_cannot_reuse_committed_context(self):
        self.previous_boot();self.boot_fail=True;self.retry_known_rollback();self.boot_fail=False
        self.boot['uuid']='22222222-3333-4444-5555-666666666666'
        with self.assertRaisesRegex(ValueError,'postboot_committed_identity_changed'):self.retry_known_rollback()
        self.assertEqual(self.realrun(['git','rev-parse','HEAD'],self.repo).stdout.strip(),self.revision)
    def test_postboot_replaced_same_content_candidate_is_rejected(self):
        self.previous_boot();target=self.candidate/'build/AGI.app/new'
        import shutil
        other=target.with_suffix('.replacement');shutil.copy2(target,other);os.replace(other,target)
        with self.assertRaisesRegex(ValueError,'postboot_candidate_changed'):self.retry_known_rollback()
        self.assertFalse(self.history().exists())
    def test_postboot_replaced_original_is_rejected(self):
        self.previous_boot();target=self.live/self.rel
        import shutil
        other=target.with_suffix('.replacement');shutil.copy2(target,other);os.replace(other,target)
        with self.assertRaisesRegex(ValueError,'postboot_entry_identity_changed'):self.retry_known_rollback()
        self.assertFalse(self.history().exists())
    def test_postboot_original_descendant_ctime_is_not_ignored(self):
        self.previous_boot();child=self.app/'old';mode=child.stat().st_mode
        os.chmod(child,0o600);os.chmod(child,mode)
        with self.assertRaisesRegex(ValueError,'postboot_original_identity_changed'):self.retry_known_rollback()
        self.assertFalse(self.history().exists())
    def test_postboot_retained_candidate_tamper_is_rejected(self):
        self.previous_boot();record=json.loads(self.journal.read_text());Path(record['entries'][0]['stage']).write_text('tampered')
        with self.assertRaisesRegex(ValueError,'postboot_retained_candidate_changed'):self.retry_known_rollback()
        self.assertFalse(self.history().exists())
    def test_postboot_mixed_prior_device_numbers_are_rejected(self):
        self.previous_boot();proof=json.loads(self.local.read_text())
        next(iter(proof['source_bindings'].values()))['identity'][str(self.app)][0]+=1
        atomic(self.local,packed(proof));atomic(self.external/'prepared.json',packed(proof))
        with self.assertRaisesRegex(ValueError,'postboot_device_binding_conflict'):self.retry_known_rollback()
    def test_postboot_volume_change_during_revalidation_is_rejected(self):
        self.previous_boot();self.volume_probe.side_effect=[self.volume,{**self.volume,'uuid':'different'}]
        with self.assertRaisesRegex(ValueError,'postboot_source_volume_changed'):self.retry_known_rollback()
        self.assertFalse(self.history().exists())
    def test_postboot_rollback_after_observed_boot_is_not_eligible(self):
        self.previous_boot();self.boot['at']=0
        with self.assertRaisesRegex(ValueError,'postboot_preboot_rollback_required'):self.retry_known_rollback()
        self.assertFalse(self.history().exists())
    def test_postboot_other_product_qualification_is_rejected(self):
        r,_=self.previous_boot();p=Path(r['verification']['acceptance_evidence_path']).parent/'release-verification.json'
        p.write_text('{}')
        with self.assertRaisesRegex(ValueError,'postboot_product_qualification_changed'):self.retry_known_rollback()
        self.assertFalse(self.history().exists())
    def test_postboot_pause_preserves_old_evidence_without_new_context(self):
        self.previous_boot();self.pause_autonomy()
        with self.assertRaisesRegex(PermissionError,'authorization_revoked'):self.retry_known_rollback()
        self.assertFalse(self.history().exists())
        for path,body in self.prior.items():self.assertEqual(path.read_bytes(),body)
    def test_postboot_offline_external_proof_is_not_replaced(self):
        self.previous_boot()
        with patch.object(self.b,'volume',side_effect=self.b.Unsafe('offline')):
            result=self.retry_known_rollback()
        self.assertFalse(result['installed']);self.assertFalse(self.history().exists())
        for path,body in self.prior.items():self.assertEqual(path.read_bytes(),body)
    def test_postboot_context_is_exclusive_durable_and_byte_exact(self):
        self.previous_boot();first=self.context();self.assertEqual(first,self.context())
        p=Path(first['receipt']);body=p.read_bytes();p.write_bytes(body+b' ')
        with self.assertRaisesRegex(ValueError,'postboot_context_identity_changed'):self.context()
        self.assertEqual(p.read_bytes(),body+b' ')
    def test_postboot_ui_busy_before_exchange_keeps_anchor_for_next_attempt(self):
        self.previous_boot();r=json.loads(self.request.read_text());r['require_ui_absent']=True;atomic(self.request,packed(r))
        old_journal=self.journal.read_bytes()
        with patch.object(project_repair,'app_busy',return_value=True):
            with self.assertRaisesRegex(PermissionError,'application_reopened'):self.retry_known_rollback()
        self.assertEqual(self.journal.read_bytes(),old_journal)
        anchor=next(self.history().glob('*.json'));body=anchor.read_bytes()
        with patch.object(project_repair,'app_busy',return_value=False):self.assertTrue(self.retry_known_rollback()['installed'])
        self.assertEqual(anchor.read_bytes(),body)
    def test_postboot_second_precommit_rollback_continues_with_current_device_tokens(self):
        self.previous_boot();previous=release.run
        def fail(args,*a,**kw):
            if list(map(str,args))[:2]==['git','merge']:raise subprocess.CalledProcessError(1,args,stderr='another required-write failure')
            return previous(args,*a,**kw)
        with patch.object(release,'run',side_effect=fail),self.assertRaises(subprocess.CalledProcessError):self.retry_known_rollback()
        record=json.loads(self.journal.read_text());self.assertEqual(record['state'],'rolled_back')
        self.assertTrue(all(entry['old'][0]==self.current_device for entry in record['entries']))
        self.data.write_text('data after another rollback')
        self.assertTrue(self.retry_known_rollback()['installed']);self.assertEqual(self.data.read_text(),'data after another rollback')
    def test_postboot_committed_before_first_installed_receipt_does_not_recommit(self):
        self.previous_boot();original=release._journal_write;failed=[]
        def fail(path,value):
            if Path(path)==self.local.with_suffix('.installed.json') and not failed:
                failed.append(True);raise OSError('injected first installed receipt failure')
            return original(path,value)
        with patch.object(release,'_journal_write',side_effect=fail),self.assertRaisesRegex(OSError,'first installed receipt failure'):self.retry_known_rollback()
        self.assertFalse(self.local.with_suffix('.installed.json').exists())
        self.assertEqual(self.realrun(['git','rev-parse','HEAD'],self.repo).stdout.strip(),self.revision)
        previous=release.run
        def no_commit(args,*a,**kw):
            if list(map(str,args))[:2]==['git','merge']:self.fail('committed continuation must not merge again')
            return previous(args,*a,**kw)
        with patch.object(release,'run',side_effect=no_commit):result=self.retry_known_rollback()
        self.assertTrue(result['installed']);self.assertTrue(result['original_cleanup_complete'])
    def test_postboot_preserves_an_existing_preboot_rollback_history(self):
        r,proof=self.previous_boot();tree=self.b.tree_identity;token=release._token
        def prior_tree(path):return {name:[self.old_device,*row[1:]] for name,row in tree(path).items()}
        def prior_token(path):return [self.old_device,token(path)[1]]
        with patch.object(self.b,'tree_identity',side_effect=prior_tree),patch.object(release,'_token',side_effect=prior_token):
            release._rollback_original_guard(self.request,r,proof,self.journal)
        history=self.local.parent/(self.local.stem+'.rollback-history')
        before={p:p.read_bytes() for p in history.glob('*.json')};self.assertEqual(len(before),1)
        self.assertTrue(self.retry_known_rollback()['installed'])
        self.assertEqual(len(list(history.glob('*.json'))),2)
        for path,body in before.items():self.assertEqual(path.read_bytes(),body)
    def test_postboot_metadata_interruption_preserves_originals_and_can_continue(self):
        self.previous_boot()
        with patch.object(self.b,'exclusive_move',side_effect=OSError('injected postboot metadata interruption')):
            with self.assertRaisesRegex(OSError,'postboot metadata interruption'):self.retry_known_rollback()
        pending={p:p.read_bytes() for p in self.history().glob('*.pending')};self.assertTrue(pending)
        self.assertEqual(self.journal.read_bytes(),self.prior[self.journal])
        self.assertTrue(self.retry_known_rollback()['installed'])
        for p,body in pending.items():self.assertEqual(p.read_bytes(),body)

class PublicationCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve()
        self.home=self.root/'home';self.live=self.home/'Library/Application Support/Personal AI Monitor/core'
        self.app=Path('/Applications/AGI.app');self.candidate=self.root/'candidate';self.base='a'*40
        self.store=self.enterContext(Store(self.root/'feedback'))
        self.store.set('config',{'enabled':True,'repository':str(self.root/'repo'),'live_core':str(self.live),'application':str(self.app),'python':sys.executable})
        self.store.set('autonomy',{'policy':autonomy.POLICY,'paused':False})
        self.external=self.root/'external';self.local=self.root/'metadata/prepared.json'
        self.enterContext(patch.object(Path,'home',return_value=self.home))
        self.enterContext(patch.object(release,'run',side_effect=lambda args,*a,**kw:subprocess.CompletedProcess(args,0,self.base if args[1]=='rev-parse' else '','')))
        self.enterContext(patch.object(release,'core_hash',side_effect=lambda path:'old' if path==self.live else 'new'))
        read=Path.read_bytes;info=self.app/'Contents/Info.plist'
        def read_bytes(path):
            if path==info:return plistlib.dumps({'PAMGitRevision':self.base[:7],'PAMCoreCodeHash':'old'})
            return read(path)
        self.enterContext(patch.object(Path,'read_bytes',new=read_bytes))
        self.prepare=self.enterContext(patch.object(storage,'prepare_external',return_value=(self.external,self.local)))
        self.enterContext(patch.object(storage,'check_external',return_value={}))
        self.process=self.enterContext(patch.object(release.subprocess,'Popen'))
        self.process.return_value.communicate.return_value=(' {"installed":true} ','');self.process.return_value.returncode=0
        self.reopen=self.enterContext(patch.object(release.subprocess,'run'))
    def pause_autonomy(self):
        setting=self.store.get('autonomy');setting['paused']=True;self.store.set('autonomy',setting)
        self.assertTrue(self.store.active());self.assertFalse(autonomy.active(self.store))
    def publish(self,**kwargs):
        return release.publish_candidate(self.store,self.candidate,self.base,{'core_hash':'new'},'coordinator',**kwargs)
    def test_autonomy_pause_prevents_recovery_preparation_and_dispatch(self):
        self.pause_autonomy()
        with self.assertRaisesRegex(PermissionError,'authorization_revoked'):self.publish()
        self.prepare.assert_not_called();self.process.assert_not_called()
    def test_autonomy_pause_during_recovery_preparation_prevents_dispatch(self):
        def prepare(*args):self.pause_autonomy();return self.external,self.local
        self.prepare.side_effect=prepare
        with self.assertRaisesRegex(PermissionError,'authorization_revoked'):self.publish()
        self.process.assert_not_called();self.assertFalse((self.store.root/'publication-requests/coordinator.json').exists())
    def test_autonomy_pause_after_ui_yield_prevents_dispatch_and_restores_ui(self):
        def idle(*args):self.pause_autonomy();return True
        with patch.object(project_repair,'app_busy',return_value=True),patch.object(project_repair,'prepare_idle_exit',side_effect=idle):
            with self.assertRaisesRegex(PermissionError,'authorization_revoked'):self.publish(require_ui_absent=True)
        self.process.assert_not_called()
        self.reopen.assert_called_once_with(['/usr/bin/open','-gj',str(self.app),'--args','--background-maintenance'],capture_output=True,timeout=15)
    def test_pause_after_request_write_preserves_known_unexecuted_evidence(self):
        write=release.atomic
        def pause_after_request(path,content):
            write(path,content)
            if path.name=='coordinator.json':self.pause_autonomy()
        with patch.object(release,'atomic',side_effect=pause_after_request),self.assertRaisesRegex(PermissionError,'authorization_revoked'):self.publish()
        self.process.assert_not_called();self.assertFalse((self.store.root/'publication-requests/coordinator.json').exists())
        receipts=list((self.store.root/'publication-requests').glob('*.receipt.json'))
        self.assertEqual(len(receipts),1);self.assertEqual(json.loads(receipts[0].read_text())['reason'],'process_not_started')
        self.assertFalse(json.loads(receipts[0].read_text())['exchange_started'])
    def test_dispatch_holds_the_same_short_lock_as_owner_pause(self):
        process=self.process.return_value
        def dispatch(*args,**kwargs):
            fd=os.open(self.store.root/'control.lock',os.O_RDWR|os.O_NOFOLLOW)
            try:
                with self.assertRaises(BlockingIOError):fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            finally:os.close(fd)
            return process
        self.process.side_effect=dispatch;self.assertTrue(self.publish()['installed'])
        fd=os.open(self.store.root/'control.lock',os.O_RDWR|os.O_NOFOLLOW)
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        finally:os.close(fd)
    def test_dispatch_already_started_keeps_unknown_request_if_gate_exit_fails(self):
        with patch.object(release,'gate') as boundary:
            boundary.return_value.__exit__.side_effect=OSError('lock exit interrupted after dispatch')
            with self.assertRaisesRegex(OSError,'lock exit interrupted'):self.publish()
        self.process.assert_called_once();self.reopen.assert_not_called()
        self.assertTrue((self.store.root/'publication-requests/coordinator.json').is_file())
        self.assertFalse(list((self.store.root/'publication-requests').glob('*.receipt.json')))
    def test_idle_exit_checks_autonomy_before_absent_app_fast_path(self):
        self.pause_autonomy()
        with patch.object(project_repair,'app_busy',return_value=False) as busy:
            self.assertFalse(project_repair.prepare_idle_exit(self.store,'agi'))
        busy.assert_not_called()

if __name__=='__main__':unittest.main()
