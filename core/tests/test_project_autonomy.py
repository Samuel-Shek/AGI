import json
import os
from pathlib import Path
import shutil
import stat
from types import SimpleNamespace
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from activity_mvp.feedback import projects, project_repair as repair
from activity_mvp.feedback.store import Store, packed
from activity_mvp.feedback.work import WorkQueue, POLICY
from tests.personal_scope_fixtures import register_personal


class ProjectAutonomyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name).resolve()
        register_personal(self, self.root, ['owner-fixture', 's'])
        self.store = Store(self.root/'ledger')
        self.store.set('config', {'enabled': True, 'python': sys.executable, 'codex_binary': shutil.which('codex') or 'codex'})
        self.store.set('autonomy', {'policy': POLICY, 'paused': False})
        self.specs = {}
        for key in ('agi', 'meeting-assistant'):
            repo = self.root/key; repo.mkdir()
            (repo/'package.json').write_text(json.dumps({'name': key, 'main': 'src/index.js', 'version': '1.0.0'}))
            subprocess.run(['/usr/bin/git','init',str(repo)], check=True, capture_output=True)
            projects.git(repo, 'add', 'package.json')
            projects.git(repo, '-c','user.name=Test', '-c','user.email=test@localhost', 'commit','-m','fixture')
            self.specs[key] = {**projects.PROJECTS[key], 'repository': str(repo), 'markers': ('package.json',), 'application': str(self.root/(key+'.app'))}
        self.patcher = patch.dict(projects.PROJECTS, self.specs); self.patcher.start()
        self.incident = {'id': 'i', 'title':'problem', 'cause':'unknown', 'evidence':'', 'revision':1}
        self.owner_source = self.root/'owner-fixture.jsonl'
        self.owner_source.write_text(packed({'sessionId':'owner-fixture','role':'user','content':'synthetic owner registration'})+'\n')
        self.store.conn.execute('INSERT INTO incidents(id,title,category,state,created,updated) VALUES(?,?,?,?,?,?)', ('i','problem','execution','investigating',1,1))
        self.store.conn.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)', ('owner-proof','fixture','owner-fixture',1,'synthetic owner registration','', packed({'path':str(self.owner_source)}),'classified',1,0))
        self.store.conn.execute('INSERT INTO quotes VALUES(?,?)', ('owner-proof','i'))

    def test_empty_and_mixed_sources_are_denied_before_project_read(self):
        from activity_mvp.feedback.sources import incident_admitted
        self.assertTrue(incident_admitted(self.store, 'i'))
        self.store.conn.execute('UPDATE messages SET session=?', ('unknown',))
        self.assertFalse(incident_admitted(self.store, 'i'))
        self.store.conn.execute('UPDATE messages SET session=?', ('owner-fixture',))
        company_source = self.root/'company'/'foreign.jsonl'
        company_source.parent.mkdir();company_source.write_text('unread synthetic company material\n')
        self.store.conn.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)', ('mixed','fixture','company',1,'unread synthetic material','', packed({'path':str(company_source)}),'classified',1,0))
        self.store.conn.execute('INSERT INTO quotes VALUES(?,?)', ('mixed','i'))
        with patch.object(projects,'identity',side_effect=AssertionError('denied before project read')):
            self.assertEqual(projects.resolve_project(self.store,self.incident)['reason'],'owner_retired_or_source_unknown')
            self.store.conn.execute('DELETE FROM quotes')
            self.assertEqual(projects.resolve_project(self.store,self.incident)['reason'],'owner_retired_or_source_unknown')

    def test_registered_session_missing_path_is_denied_before_project_read(self):
        from activity_mvp.feedback.sources import incident_admitted
        self.assertTrue(incident_admitted(self.store,'i'))
        self.store.conn.execute('UPDATE messages SET reference=?', ('{}',))
        self.assertFalse(incident_admitted(self.store,'i'))
        with patch.object(projects,'identity',side_effect=AssertionError('missing source denied before project read')):
            self.assertEqual(projects.resolve_project(self.store,self.incident)['reason'],'owner_retired_or_source_unknown')

    def tearDown(self):
        self.patcher.stop(); self.store.close(); self.temp.cleanup()

    def bind(self, project='agi'):
        self.store.set('project_bindings', {'i': {'authority':POLICY,'project_id':project,'repository':self.specs[project]['repository']}})

    def test_finite_identity_reads_preserve_actual_identity_and_detect_dirty_source(self):
        ordinary=projects.identity('agi')
        self.assertEqual(projects.identity('agi',finite_read=True),ordinary)
        repo=Path(self.specs['agi']['repository'])
        (repo/'package.json').write_text('{"changed":true}')
        changed=projects.identity('agi',finite_read=True)
        self.assertEqual(changed,projects.identity('agi'))
        self.assertNotEqual(changed['working_tree'],ordinary['working_tree'])

    def test_finite_read_rejects_writes_and_extra_args_before_launch(self):
        repo=Path(self.specs['agi']['repository'])
        with patch.object(projects.subprocess,'run') as run:
            for args in [('reset','--hard'),('status','--porcelain','--untracked-files=no','--ignored'),('diff','HEAD','--','package.json'),('rev-parse','--git-dir')]:
                with self.subTest(args=args),self.assertRaisesRegex(PermissionError,'finite_git_read_outside_scope'):
                    projects.git(repo,*args,finite_read=True)
        run.assert_not_called()

    def test_finite_identity_timeout_is_fail_closed_and_default_launcher_unchanged(self):
        repo=Path(self.specs['agi']['repository'])
        failure=subprocess.TimeoutExpired(['sensitive-path-not-logged'],30)
        with patch.object(projects.subprocess,'run',side_effect=failure) as run:
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                projects.git(repo,'rev-parse','HEAD',finite_read=True)
            self.assertEqual(caught.exception.identity_read,{'operation':['rev-parse','HEAD'],'timeout_seconds':30})
            self.assertEqual(run.call_args.kwargs['timeout'],30)
            if sys.platform=='darwin':self.assertEqual(run.call_args.args[0][:2],['/usr/sbin/taskpolicy','-a'])
        with patch.object(projects.subprocess,'run',return_value=SimpleNamespace(stdout='head')) as run:
            projects.git(repo,'rev-parse','HEAD')
            self.assertEqual(run.call_args.args[0][0],'/usr/bin/git')

    def test_finite_identity_does_not_refresh_index_or_touch_existing_lock(self):
        repo=Path(self.specs['agi']['repository']); tracked=repo/'package.json'
        index=repo/'.git/index'; lock=repo/'.git/index.lock'
        before=(index.read_bytes(),index.stat().st_ino,index.stat().st_mtime_ns)
        stamp=tracked.stat();os.utime(tracked,ns=(stamp.st_atime_ns,stamp.st_mtime_ns+2_000_000_000))
        self.assertEqual(projects.git(repo,'status','--porcelain','--untracked-files=no',finite_read=True),'')
        self.assertEqual((index.read_bytes(),index.stat().st_ino,index.stat().st_mtime_ns),before)
        self.assertFalse(lock.exists())
        # Control: ordinary status still refreshes stat-cache information.
        self.assertEqual(projects.git(repo,'status','--porcelain','--untracked-files=no'),'')
        self.assertNotEqual(index.read_bytes(),before[0])
        lock.write_bytes(b'unrelated test transaction')
        lock_before=(lock.read_bytes(),lock.stat().st_ino,lock.stat().st_mtime_ns)
        tracked.write_text('{"changed":true}')
        self.assertIn('package.json',projects.git(repo,'status','--porcelain','--untracked-files=no',finite_read=True))
        self.assertEqual((lock.read_bytes(),lock.stat().st_ino,lock.stat().st_mtime_ns),lock_before)
        # Required write locks remain enforced; read-only mode never bypasses them.
        with self.assertRaises(subprocess.CalledProcessError):projects.git(repo,'add','package.json')
        self.assertEqual((lock.read_bytes(),lock.stat().st_ino,lock.stat().st_mtime_ns),lock_before)

    def test_title_and_body_paths_do_not_grant_project(self):
        item = {**self.incident, 'title':'AGI at '+self.specs['agi']['repository'], 'project':'agi'}
        self.assertEqual(projects.resolve_project(self.store,item)['status'],'waiting_evidence')

    def test_owner_binding_checks_real_markers(self):
        self.bind('meeting-assistant')
        value = projects.resolve_project(self.store,self.incident)
        self.assertEqual(value['project_id'],'meeting-assistant')
        self.assertTrue(value['identity']['head']); self.assertTrue(value['source_revision'])
        (Path(self.specs['meeting-assistant']['repository'])/'package.json').write_text('{"name":"different"}')
        self.assertEqual(projects.resolve_project(self.store,self.incident)['status'],'waiting_evidence')

    def test_current_owner_alias_selects_only_fixed_registered_product(self):
        source = self.root/'s.jsonl'
        source.write_text(packed({'sessionId':'s','role':'user','content':'AGI 的文字显示错误'})+'\n')
        self.store.conn.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)', ('m','fixture','s',1,'AGI 的文字显示错误','', packed({'path':str(source)}),'classified',1,0))
        self.store.conn.execute('INSERT INTO quotes VALUES(?,?)',('m','i'))
        with patch('activity_mvp.feedback.sources.reference_is_current',return_value=True):
            target=projects.resolve_project(self.store,self.incident)
        self.assertEqual(target['project_id'],'agi')
        with patch('activity_mvp.feedback.sources.reference_is_current',return_value=False):
            self.assertEqual(projects.resolve_project(self.store,self.incident)['status'],'waiting_evidence')

    def test_other_local_project_is_preparation_only(self):
        repo=self.root/'other';repo.mkdir();(repo/'pyproject.toml').write_text('[project]\nname="other"\n')
        subprocess.run(['/usr/bin/git','init',str(repo)],check=True,capture_output=True)
        projects.git(repo,'add','.');projects.git(repo,'-c','user.name=Test','-c','user.email=test@localhost','commit','-m','fixture')
        with patch.object(projects,'ROOT',self.root),patch.object(projects,'_task_directories',return_value=[str(repo)]):
            target=projects.resolve_project(self.store,self.incident)
        self.assertTrue(target['prepare_only']);self.assertTrue(target['project_id'].startswith('local-'))
        self.assertNotIn(target['project_id'],projects.PROJECTS)

    def test_conflicting_current_task_and_binding_remain_unknown(self):
        self.bind('meeting-assistant')
        with patch.object(projects,'_task_directories',return_value=[self.specs['agi']['repository']]):
            value = projects.resolve_project(self.store,self.incident)
        self.assertEqual(value['reason'],'project_ambiguous')

    def test_owner_binding_cannot_register_arbitrary_path(self):
        self.bind(); conf=self.store.get('project_bindings'); conf['i']['repository']=str(self.root)
        self.store.set('project_bindings',conf)
        self.assertEqual(projects.resolve_project(self.store,self.incident)['status'],'waiting_evidence')

    def test_unavailable_task_directory_does_not_repeat_or_block_other_projects(self):
        blocked=self.root/'unavailable';blocked.mkdir();self.store._autonomy_project_identities={}
        original=projects.git;calls=[]
        def checked(root,*args,**kwargs):
            if Path(root)==blocked:
                calls.append(kwargs['timeout']);raise subprocess.TimeoutExpired('git',kwargs['timeout'])
            return original(root,*args,**kwargs)
        with patch.object(projects,'git',side_effect=checked):
            with patch.object(projects,'_task_directories',return_value=[str(blocked)]):
                self.assertEqual(projects.resolve_project(self.store,self.incident)['status'],'waiting_evidence')
            with patch.object(projects,'_task_directories',return_value=[str(blocked),self.specs['agi']['repository']]):
                self.assertEqual(projects.resolve_project(self.store,self.incident)['project_id'],'agi')
        self.assertEqual(calls,[3])

    def test_failed_task_locator_is_rechecked_in_new_pass(self):
        repo=self.specs['agi']['repository'];self.store._autonomy_project_identities={}
        with patch.object(projects,'_task_directories',return_value=[repo]):
            with patch.object(projects,'git',side_effect=subprocess.TimeoutExpired('git',3)):
                self.assertEqual(projects.resolve_project(self.store,self.incident)['status'],'waiting_evidence')
            self.store._autonomy_project_identities={}
            self.assertEqual(projects.resolve_project(self.store,self.incident)['project_id'],'agi')

    def test_repository_alias_refused(self):
        self.bind(); real=Path(self.specs['agi']['repository']); alias=self.root/'alias';alias.symlink_to(real)
        with patch.dict(projects.PROJECTS, {'agi':{**self.specs['agi'],'repository':str(alias)}}):
            self.store.set('project_bindings', {'i':{'authority':POLICY,'project_id':'agi','repository':str(alias)}})
            self.assertEqual(projects.resolve_project(self.store,self.incident)['status'],'waiting_evidence')

    def test_snapshot_preserves_dirty_user_worktree(self):
        from activity_mvp.feedback.repair import stage_snapshot
        source=Path(self.specs['agi']['repository']); (source/'package.json').write_text('user edit')
        (source/'user-note.txt').write_text('uncommitted')
        before=projects.git(source,'status','--porcelain');snapshot=stage_snapshot(source,self.root/'snapshot')
        self.assertEqual((source/'package.json').read_text(),'user edit')
        self.assertEqual((source/'user-note.txt').read_text(),'uncommitted')
        self.assertEqual(projects.git(source,'status','--porcelain'),before)
        self.assertNotEqual((snapshot/'package.json').read_text(),'user edit')

    def proposal(self,path,content):
        return {'risk':'bounded','diagnosis':'layout','changes':[{'path':path,'content':content}], 'regression_test':'assert sufficiently_long_regression'}

    def test_cross_project_and_traversal_writes_refused(self):
        for name in ('../agi/file.py','/tmp/evil','src/security.js','core/activity_mvp/feedback/store.py'):
            with self.assertRaises(PermissionError): repair.validate_changes('meeting-assistant',self.proposal(name,'bad'),self.root)

    def test_css_effects_and_visibility_protected(self):
        target=self.root/'src';target.mkdir();file=target/'subtitle.css';file.write_text('.x { font-size: 12px; opacity: 1; }')
        good=self.proposal('src/subtitle.css','.x { font-size: 16px; opacity: 1; }')
        self.assertEqual(len(repair.validate_changes('meeting-assistant',good,self.root)[0]),1)
        for bad in ('.x { font-size: 16px; opacity: 0; }','.x { font-size: 0px; opacity: 1; }','@import "https://bad";'):
            with self.assertRaises(PermissionError): repair.validate_changes('meeting-assistant',self.proposal('src/subtitle.css',bad),self.root)

    def test_swift_protects_actions_and_interpolation(self):
        path=projects.PROJECTS['agi']['editable'][2];target=self.root/path;target.parent.mkdir(parents=True)
        original='Text("Status").padding(12)\nButton("Open") { openSettings() }';target.write_text(original)
        changed=original.replace('Status','状态').replace('padding(12)','padding(16)')
        repair.validate_changes('agi',self.proposal(path,changed),self.root)
        for bad in (changed.replace('openSettings()','deleteAll()'),changed.replace('状态','\\(runCommand())')):
            with self.assertRaises(PermissionError): repair.validate_changes('agi',self.proposal(path,bad),self.root)

    def test_duplicate_paths_and_missing_regression_refused(self):
        p=self.proposal('src/subtitle.css','bad');p['changes']*=2
        with self.assertRaises(ValueError):repair.validate_changes('meeting-assistant',p,self.root)

    def test_code_rollback_keeps_new_live_data(self):
        app=self.root/'app';app.mkdir();(app/'code').write_text('old')
        stage=self.root/'staged';stage.mkdir();(stage/'code').write_text('new')
        recovery=self.root/'recovery';recovery.mkdir();data=self.root/'live-data';data.write_text('before')
        def fail():
            data.write_text('newly received data');self.assertEqual((app/'code').read_text(),'new');raise ValueError('bad startup')
        with self.assertRaises(ValueError):repair.exchange_app(app,stage,recovery,fail)
        self.assertEqual((app/'code').read_text(),'old');self.assertEqual(data.read_text(),'newly received data')
        self.assertTrue(json.loads((recovery/'rolled-back.json').read_text())['current_data_preserved'])

    def test_code_exchange_validates_actual_destination(self):
        app=self.root/'app';app.mkdir();(app/'code').write_text('old')
        stage=self.root/'stage';stage.mkdir();(stage/'code').write_text('new')
        recovery=self.root/'recovery';recovery.mkdir()
        result=repair.exchange_app(app,stage,recovery,lambda:self.assertEqual((app/'code').read_text(),'new'))
        self.assertTrue(result['code_exchanged']);self.assertEqual((recovery/'previous.app/code').read_text(),'old')

    def test_daily_quota_is_shared_by_projects(self):
        self.store.reserve('repair')
        with self.assertRaisesRegex(PermissionError,'daily_budget'):self.store.reserve('repair')

    def test_paused_autonomy_prevents_prepared_publish(self):
        self.store.set('autonomy',{'policy':POLICY,'paused':True})
        with patch.object(repair,'publish_candidate') as publish:
            self.assertEqual(repair.process_one(self.store)['status'],'paused');publish.assert_not_called()

    def _prepared_publication(self):
        job='c'*24
        queue=WorkQueue(self.store);item=queue.enqueue('feedback','i','v1','agi')
        result={'job':job,'verification':{'base':'fixture'},'candidate':str(self.root),'work_revision':'v1'}
        queue.update(item['id'],'ready',result=result)
        self.store.conn.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?)', (job,'i',1,'prepared',1,1,packed(result)))
        self.store.reserve('repair')
        from activity_mvp.feedback.acceptance import guard_hashes
        self.store.set('isolation',{'passed':True});self.store.set('model_capability',{'enabled':True})
        self.store.set('repair_capability',{'enabled':True,'guards':guard_hashes()})
        return queue,item,job

    def _tick_interrupted_publications(self):
        from activity_mvp.feedback import service
        with (patch.object(service,'filtered',side_effect=lambda store:store),
              patch.object(service.autonomy,'active',return_value=False),
              patch.object(service.sources,'scan',return_value=[]),
              patch.object(service,'review',return_value={}),
              patch.object(service.journal,'weekly'),patch.object(service.journal,'render',return_value={}),
              patch('activity_mvp.feedback.probes.observe_journal',return_value={}),
              patch('activity_mvp.reflection.service.scan',return_value={}),
              patch('activity_mvp.reflection.service.notify',return_value={}),
              patch.object(service.subprocess,'run') as external):
            service.tick(self.store.root,analyze=False,repair=False)
        external.assert_not_called()

    def test_busy_app_defers_without_deploy_or_new_budget(self):
        queue,item,job=self._prepared_publication()
        budget=self.store.rows('SELECT * FROM budget')
        # Recovery preflight and the UI check now belong to the publisher. The
        # queue must retain a prepared job when that publisher safely defers.
        for status in ('app_busy','deferred_t9'):
            with self.subTest(status=status):
                with patch.object(repair,'prepare_idle_exit') as exit_ui,patch.object(repair,'publish_candidate',return_value={'installed':False,'status':status}) as publish,patch.object(self.store,'notify') as notify:
                    result=repair._publish_prepared(self.store,queue,queue.get(item['id']),self.root,{},job)
                self.assertEqual(result['status'],status);publish.assert_called_once()
                exit_ui.assert_not_called();notify.assert_not_called()
                self.assertEqual(queue.get(item['id'])['state'],'ready')
                self.assertEqual(self.store.rows('SELECT state FROM jobs WHERE id=?',(job,)),[{'state':'prepared'}])
                self._tick_interrupted_publications()
                self.assertEqual(self.store.rows('SELECT state FROM jobs WHERE id=?',(job,)),[{'state':'prepared'}])
                self.assertEqual(self.store.rows('SELECT auto_disabled FROM incidents WHERE id=?',('i',)),[{'auto_disabled':0}])
                self.assertEqual(self.store.rows('SELECT * FROM budget'),budget)

    def test_uncertain_publication_is_not_returned_to_ready(self):
        queue,item,job=self._prepared_publication()
        budget=self.store.rows('SELECT * FROM budget')
        for receipt in ({'installed':False,'status':'unknown'},{'status':'app_busy'}):
            with self.subTest(receipt=receipt),patch.object(repair,'publish_candidate',return_value=receipt):
                result=repair._publish_prepared(self.store,queue,queue.get(item['id']),self.root,{},job)
            self.assertEqual(result['status'],'waiting_evidence')
            self.assertEqual(queue.get(item['id'])['state'],'verifying')
            self.assertEqual(self.store.rows('SELECT state FROM jobs WHERE id=?',(job,)),[{'state':'deploying'}])
        self._tick_interrupted_publications()
        self.assertEqual(self.store.rows('SELECT state FROM jobs WHERE id=?',(job,)),[{'state':'unknown'}])
        self.assertEqual(self.store.rows('SELECT auto_disabled FROM incidents WHERE id=?',('i',)),[{'auto_disabled':1}])
        with patch.object(repair,'publish_candidate') as publish:
            self.assertEqual(repair.process_one(self.store)['status'],'waiting_evidence')
        publish.assert_not_called();self.assertEqual(self.store.rows('SELECT * FROM budget'),budget)

    def test_t9_preparation_exception_defers_but_active_request_stays_unknown(self):
        from activity_mvp.feedback.recovery_storage import RecoveryDeferred
        queue,item,job=self._prepared_publication()
        budget=self.store.rows('SELECT * FROM budget')
        with patch.object(repair,'publish_candidate',side_effect=RecoveryDeferred('volume_unavailable')):
            result=repair._publish_prepared(self.store,queue,queue.get(item['id']),self.root,{},job)
        self.assertEqual(result['status'],'deferred_t9')
        self._tick_interrupted_publications()
        self.assertEqual(queue.get(item['id'])['state'],'ready')
        self.assertEqual(self.store.rows('SELECT state FROM jobs WHERE id=?',(job,)),[{'state':'prepared'}])
        self.assertEqual(self.store.rows('SELECT auto_disabled FROM incidents WHERE id=?',('i',)),[{'auto_disabled':0}])
        marker=self.store.root/'publication-requests'/(job+'.json');marker.parent.mkdir();marker.write_text('{}')
        with patch.object(repair,'publish_candidate',side_effect=RecoveryDeferred('volume_unavailable')):
            result=repair._publish_prepared(self.store,queue,queue.get(item['id']),self.root,{},job)
        self.assertEqual(result['status'],'waiting_evidence')
        self.assertEqual(queue.get(item['id'])['state'],'verifying')
        self.assertEqual(self.store.rows('SELECT state FROM jobs WHERE id=?',(job,)),[{'state':'deploying'}])
        self.assertEqual(self.store.rows('SELECT * FROM budget'),budget)

    def test_busy_response_with_durable_publication_is_not_replayed(self):
        queue,item,job=self._prepared_publication()
        for relative in ('publication-requests/'+job+'.json','publication-receipts/'+job+'.exchange.json',
                         'publication-receipts/'+job+'.installed.json'):
            with self.subTest(relative=relative):
                marker=self.store.root/relative;marker.parent.mkdir(exist_ok=True);marker.write_text('{}')
                with patch.object(repair,'publish_candidate',return_value={'installed':False,'status':'app_busy'}):
                    result=repair._publish_prepared(self.store,queue,queue.get(item['id']),self.root,{},job)
                self.assertEqual(result['status'],'waiting_evidence')
                self.assertEqual(queue.get(item['id'])['state'],'verifying')
                self.assertEqual(self.store.rows('SELECT state FROM jobs WHERE id=?',(job,)),[{'state':'deploying'}])
                marker.unlink()

    def test_interrupted_job_is_not_reinstalled(self):
        queue=WorkQueue(self.store);item=queue.enqueue('feedback','i','v1','agi');queue.update(item['id'],'verifying',result={'job':'a'*24})
        from activity_mvp.feedback.acceptance import guard_hashes
        self.store.set('isolation',{'passed':True});self.store.set('repair_capability',{'enabled':True,'guards':guard_hashes()});self.store.set('model_capability',{'enabled':True})
        with patch.object(repair,'publish_candidate') as publish:
            result=repair.process_one(self.store)
        publish.assert_not_called();self.assertEqual(result['status'],'waiting_evidence')
        self.assertEqual(queue.get(item['id'])['state'],'waiting_evidence')

    def test_receipt_alone_cannot_certify_current_install(self):
        self.assertFalse(repair.reconcile_installation({'project_id':'agi','result':{}},{'installed':True,'revision':'invented'}))

    def test_dependency_symlink_does_not_reach_user_files(self):
        repo=self.root/'dep';(repo/'node_modules').mkdir(parents=True)
        (repo/'node_modules/outside').symlink_to(self.root/'ledger')
        with self.assertRaises(PermissionError):repair.copy_dependencies(repo,self.root/'candidate')

    def test_static_icon_privacy_allowance_is_bound_to_exact_bytes(self):
        import runpy,hashlib
        repo=Path(__file__).resolve().parents[2]
        scan=runpy.run_path(str(repo/'tools/release_privacy_scan.py'))
        path=repo/'assets/agi/AGI-app-icon.icns';rule=scan['_is_forbidden_runtime_file']
        self.assertFalse(rule('assets/agi/AGI-app-icon.icns',path))
        changed=self.root/'changed.icns';changed.write_bytes(path.read_bytes()+b'changed')
        self.assertTrue(rule('assets/agi/AGI-app-icon.icns',changed))
        self.assertTrue(rule('assets/agi/unapproved.icns',path))
        for line in (repo/'assets/agi/AGI-app-icon.sha256').read_text().splitlines():
            expected,name=line.split();self.assertEqual(hashlib.sha256((repo/'assets/agi'/name).read_bytes()).hexdigest(),expected)

    def test_meeting_readiness_uses_fresh_private_native_snapshot(self):
        with patch.object(Path,'home',return_value=self.root):
            one=repair.meeting_readiness_path('job',self.root/'ledger/recoveries/job')
            two=repair.meeting_readiness_path('job2',self.root/'ledger/recoveries/job2')
        self.assertEqual(one.name,'install-ready.json')
        self.assertRegex(one.parent.name,r'^\d{8}-\d{6}-\d+$')
        self.assertEqual(one.parent.parent,self.root/'Library/Application Support/meeting-assistant-upgrade-backups')
        self.assertNotEqual(one,two);self.assertFalse(one.exists())
        self.assertEqual(one.parent.stat().st_mode & 0o777,0o700)
        self.assertEqual(json.loads((one.parent/'autonomy-readiness.json').read_text())['kind'],'code-only-readiness')

    def test_build_volume_rejects_wrong_mount_or_owner(self):
        image=self.root/'image';mount=self.root/'volume'
        good={'image-path':str(image),'owner-uid':os.getuid(),'system-entities':[{'mount-point':str(mount),'dev-entry':'/dev/disk99'}]}
        for change in ({'owner-uid':-1},{'system-entities':[{'mount-point':'/unrelated','dev-entry':'/dev/disk99'}]}):
            result=subprocess.CompletedProcess([],0,repair.plistlib.dumps({'images':[{**good,**change}]}),b'')
            with patch.object(repair.subprocess,'run',return_value=result),self.assertRaises(PermissionError):
                repair._image_mount(image,mount)

    def test_all_swift_commands_use_private_volume_and_fixed_engine(self):
        from contextlib import contextmanager
        volume=self.root/'volume';volume.mkdir()
        @contextmanager
        def mounted(root):
            (volume.parent/'receipt.json').write_text('{"state":"detached"}')
            yield volume
        for command in ('test','run','build'):
            with patch.object(repair,'compiler_volume',mounted),patch.object(repair,'trusted_swift_sdk',return_value=Path('/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk')),patch.object(repair,'_sandbox_check',return_value=subprocess.CompletedProcess([],0,'ok','')) as check:
                repair.sandbox_check(self.store,self.root,['/usr/bin/swift',command,'--package-path','apps/PersonalAIMonitorMenuBar'])
                args=check.call_args.args[2]
                self.assertIn('native',args);self.assertIn(str(volume/'swift-scratch'),args)
                self.assertEqual(args[args.index('--sdk')+1],'/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk')
                self.assertEqual(check.call_args.args[-1],volume)

    def test_swift_sdk_is_fixed_and_rejects_mutable_or_aliased_components(self):
        sdk=Path('/Library/Developer/CommandLineTools/SDKs/MacOSX26.5.sdk')
        def metadata(path):
            if path.name=='MacOSX15.4.sdk':raise FileNotFoundError()
            return SimpleNamespace(st_mode=stat.S_IFDIR|0o755,st_uid=0)
        with patch.object(Path,'lstat',metadata),patch.dict(os.environ,{'SDKROOT':'/untrusted','DEVELOPER_DIR':'/untrusted'}):
            self.assertEqual(repair.trusted_swift_sdk(),sdk)
        for mode,owner in [(stat.S_IFLNK|0o755,0),(stat.S_IFDIR|0o777,0),(stat.S_IFDIR|0o755,os.getuid())]:
            def bad(path):
                if path==sdk.parent:return SimpleNamespace(st_mode=mode,st_uid=owner)
                return metadata(path)
            with patch.object(Path,'lstat',bad),self.assertRaisesRegex(PermissionError,'swift_sdk_not_immutable'):
                repair.trusted_swift_sdk()
        with patch.object(Path,'lstat',side_effect=FileNotFoundError),self.assertRaisesRegex(PermissionError,'approved_swift_sdk_unavailable'):
            repair.trusted_swift_sdk()

    def test_swift_sdk_cannot_be_overridden_by_caller(self):
        for command in ('test','run','build'):
            for override in (['--sdk','/untrusted'],['--sdk=/untrusted'],['-sdk','/untrusted'],['-sdk=/untrusted']):
                with patch.object(repair,'compiler_volume') as mount,patch.object(repair,'_sandbox_check') as check:
                    with self.assertRaisesRegex(PermissionError,'swift_sdk_override_rejected'):
                        repair.sandbox_check(self.store,self.root,['/usr/bin/swift',command,*override])
                    mount.assert_not_called();check.assert_not_called()

    @unittest.skipUnless(os.environ.get('AGI_PROJECT_REAL_SANDBOX_TEST')=='1','explicit real OS sandbox gate')
    def test_busy_volume_cleanup_is_durable_and_reconciled_without_force(self):
        workspace=self.root/'busy-volume';workspace.mkdir();original=repair.subprocess.run
        calls=[]
        def busy(args,*a,**kw):
            if args[:2]==['/usr/bin/hdiutil','detach']:
                calls.append(args);return subprocess.CompletedProcess(args,16,b'',b'busy')
            return original(args,*a,**kw)
        with patch.object(repair.subprocess,'run',side_effect=busy):
            with repair.compiler_volume(workspace) as volume:
                (volume/'old-artifact').write_text('temporary output')
        self.assertTrue((volume.parent/'compiler.sparseimage').exists())
        self.assertEqual(json.loads((volume.parent/'receipt.json').read_text())['state'],'cleanup_pending')
        self.assertTrue(calls);self.assertFalse(any('-force' in args for args in calls))
        with repair.compiler_volume(workspace) as fresh:
            self.assertFalse((fresh/'old-artifact').exists())
        self.assertFalse((volume.parent/'compiler.sparseimage').exists())

    @unittest.skipUnless(os.environ.get('AGI_PROJECT_REAL_SANDBOX_TEST')=='1','explicit real OS sandbox gate')
    def test_real_private_volume_atomic_write_and_cleanup(self):
        workspace=self.root/'atomic-workspace';workspace.mkdir()
        with repair.compiler_volume(workspace) as volume:
            source=volume/'probe.swift'
            source.write_text('import Foundation\ntry Data("ok".utf8).write(to:URL(fileURLWithPath:CommandLine.arguments[1]),options:.atomic)\nprint("atomic-passed")\n')
            image=volume.parent/'compiler.sparseimage'
            result=repair._sandbox_check(self.store,workspace,['/usr/bin/swift',str(source),str(volume/'result.json')],timeout=60,volume=volume)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('atomic-passed',result.stdout)
            self.assertEqual((volume/'result.json').read_text(),'ok')
            self.assertTrue(image.exists())
        self.assertFalse(image.exists())
        self.assertIsNone(repair._image_mount(image,volume))

    def test_isolated_build_root_refuses_relative_shared_or_alias_paths(self):
        script=Path(__file__).resolve().parents[2]/'scripts/build-macos-app.sh'
        shared=self.root/'shared';shared.mkdir(mode=0o755);shared.chmod(0o755)
        private=self.root/'private';private.mkdir(mode=0o700)
        alias=self.root/'alias-build';alias.symlink_to(private)
        for root in ('relative',str(shared),str(alias)):
            result=subprocess.run(['/bin/sh',str(script),'--isolated-build-root',root,'--provenance-only'],capture_output=True,text=True,timeout=15)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('isolated build root',result.stderr)

    @unittest.skipUnless(os.environ.get('AGI_PROJECT_REAL_SANDBOX_TEST')=='1','explicit real OS sandbox gate')
    def test_real_isolated_build_provenance_preserves_scratch_sibling(self):
        origin=Path(__file__).resolve().parents[2];repo=self.root/'build-fixture';repo.mkdir()
        for name in ('scripts/build-macos-app.sh','scripts/build-macos-app-impl.sh'):
            target=repo/name;target.parent.mkdir(exist_ok=True);shutil.copy2(origin/name,target)
        (repo/'scripts/build-macos-app-impl.sh').chmod(0o644)
        (repo/'core/activity_mvp').mkdir(parents=True);(repo/'core/tools').mkdir()
        (repo/'core/activity_mvp/sample.py').write_text('VALUE=1\n')
        (repo/'core/tools/sample.swift').write_text('let value = 1\n')
        (repo/'core/pyproject.toml').write_text('[project]\nname="fixture"\n')
        subprocess.run(['/usr/bin/git','init',str(repo)],check=True,capture_output=True)
        projects.git(repo,'add','.');projects.git(repo,'-c','user.name=Test','-c','user.email=test@localhost','commit','-m','fixture')
        scratch=self.root/'sandbox-build-fixture';scratch.mkdir(mode=0o700)
        retained=scratch/'retained';retained.write_text('keep this')
        result=repair.sandbox_check(self.store,repo,['/bin/sh','scripts/build-macos-app.sh','--provenance-only','--isolated-build-root',str(scratch)],timeout=45,build=True)
        self.assertEqual(result.returncode,0,result.stderr);self.assertIn('git_dirty=false',result.stdout)
        self.assertEqual(retained.read_text(),'keep this')

    @unittest.skipUnless(os.environ.get('AGI_PROJECT_REAL_SANDBOX_TEST')=='1','explicit real OS sandbox gate')
    def test_real_js_ast_admission_allows_text_but_not_effects(self):
        acorn=Path.home()/'Code/active/meeting-assistant/node_modules/acorn'
        if not acorn.is_dir():self.skipTest('installed parser unavailable')
        workspace=self.root/'js';(workspace/'src/utils').mkdir(parents=True)
        (workspace/'node_modules').mkdir();shutil.copytree(acorn,workspace/'node_modules/acorn')
        name='src/utils/attachmentText.js'
        source='const NULL_CHARACTER="\\0"; function sanitizeText(text) { return String(text || "").trim(); } module.exports={sanitizeText};'
        (workspace/name).write_text(source)
        good=source.replace('.trim()','.replace(/\\r/g, "").trim()')
        repair.validate_changes('meeting-assistant',self.proposal(name,good),workspace,store=self.store)
        for bad in (source.replace('.trim()','.constructor("return process")()'),source.replace('String(text || "").trim()','process.exit(0)'),source.replace('module.exports={sanitizeText}','module.exports={sanitizeText,extra:1}')):
            with self.assertRaises(PermissionError):repair.validate_changes('meeting-assistant',self.proposal(name,bad),workspace,store=self.store)

    @unittest.skipUnless(os.environ.get('AGI_PROJECT_REAL_SANDBOX_TEST')=='1','explicit real OS sandbox gate')
    def test_real_sandbox_blocks_source_mutation_and_outside_read(self):
        workspace=self.root/'workspace';(workspace/'src').mkdir(parents=True)
        source=workspace/'src/module.py';source.write_text('old')
        secret=self.root/'sentinel';secret.write_text('private sentinel')
        code='''import json\nfrom pathlib import Path\nr={}\nfor key,p in [('source',%r),('outside',%r)]:\n try:\n  Path(p).write_text('changed') if key=='source' else Path(p).read_text()\n  r[key]=False\n except PermissionError:r[key]=True\nprint(json.dumps(r))'''%(str(source),str(secret))
        code += '''\nimport socket\ntry:\n socket.create_connection(('127.0.0.1',9),timeout=1)\n r['network']=False\nexcept PermissionError:r['network']=True\nexcept OSError:r['network']=False\nprint(json.dumps(r))'''
        for fixed_suite in (False,True):
            with self.subTest(finite_maintenance=fixed_suite):
                result=repair.sandbox_check(self.store,workspace,[self.store.get('config')['python'],'-c',code],timeout=30,fixed_suite=fixed_suite)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(json.loads(result.stdout.splitlines()[-1]),{'source':True,'outside':True,'network':True})
                self.assertEqual(source.read_text(),'old')


if __name__=='__main__': unittest.main()
