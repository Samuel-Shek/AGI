from pathlib import Path
import hashlib, json, shutil, sqlite3, tempfile, unittest, subprocess
from unittest.mock import patch
from activity_mvp.storage import init_schema,SCHEMA_VERSION
from activity_mvp.feedback.store import Store
from activity_mvp.feedback import release, sources

class MinimalRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.live=self.root/'live';self.candidate=self.root/'candidate'
        (self.live/'data').mkdir(parents=True)
        with sqlite3.connect(self.live/'data/activity.sqlite') as d:
            d.row_factory=sqlite3.Row;init_schema(d)
        with Store(self.live/'data/feedback') as s:self.feedback=s.path
        for base in [self.live,self.candidate/'core']:
            (base/'activity_mvp/feedback').mkdir(parents=True)
            (base/'activity_mvp/storage.py').write_text('unchanged trusted startup marker')
            (base/'activity_mvp/feedback/sources.py').write_text(Path(sources.__file__).read_text())
            for name in release.FILES:(base/name).write_text('unchanged')
            for name in release.FOLDERS:(base/name).mkdir(exist_ok=True)
    def test_pure_code_only_does_not_copy_databases(self):
        path=self.candidate/'core/activity_mvp/feedback/sources.py';path.write_text(path.read_text()+'\n# formatting-only fixture\n')
        recovery=self.root/'recovery';recovery.mkdir()
        with patch.object(release,'backup_database',side_effect=AssertionError('unnecessary database copy')):
            result=release.prepare_recovery(self.live,self.candidate,recovery,self.feedback)
        self.assertFalse(result['backup_policy']['databases_required'])
        self.assertEqual(list(result['retained_code']),['activity_mvp/feedback/sources.py'])
        self.assertFalse((recovery/'activity.sqlite').exists())
    def test_initializer_change_requires_dedicated_database_recovery(self):
        (self.candidate/'core/activity_mvp/storage.py').write_text('changed startup')
        self.assertTrue(release.database_snapshot_policy(self.live,self.candidate,self.feedback)['databases_required'])
    def test_version_mismatch_fails_closed(self):
        with sqlite3.connect(self.live/'data/activity.sqlite') as d:d.execute("UPDATE meta SET value='33' WHERE key='schema_version'")
        self.assertTrue(release.database_snapshot_policy(self.live,self.candidate,self.feedback)['databases_required'])
    def test_startup_structural_migration_fails_closed(self):
        with sqlite3.connect(self.live/'data/activity.sqlite') as d:d.execute('ALTER TABLE file_watch_state DROP COLUMN document_path_hmac')
        result=release.database_snapshot_policy(self.live,self.candidate,self.feedback)
        self.assertTrue(result['databases_required']);self.assertEqual(result['detail'],'startup_changes_existing_structure')
    def test_valid_historical_shape_not_confused_with_fresh_shape(self):
        with sqlite3.connect(self.live/'data/activity.sqlite') as d:d.execute('ALTER TABLE file_change_events DROP COLUMN document_path_hmac')
        self.assertFalse(release.database_snapshot_policy(self.live,self.candidate,self.feedback)['databases_required'])
    def test_unknown_registration_does_not_mutate_closed_package(self):
        recovery=self.live/'data/feedback/recoveries/test-job';recovery.mkdir(parents=True,mode=0o700);(recovery/'old-code.py').write_text('old code')
        fakehome=self.root/'fakehome';manager=fakehome/'AI/backup-governance/backup_manager.py';manager.parent.mkdir(parents=True);manager.write_text('fixture')
        before={str(p.relative_to(recovery)):hashlib.sha256(p.read_bytes()).hexdigest() for p in recovery.rglob('*') if p.is_file()}
        with patch.object(Path,'home',return_value=fakehome),patch.object(release,'run',side_effect=subprocess.TimeoutExpired('register',180)):
            result=release.register_recovery_archive(recovery)
        after={str(p.relative_to(recovery)):hashlib.sha256(p.read_bytes()).hexdigest() for p in recovery.rglob('*') if p.is_file()}
        self.assertEqual(before,after);self.assertEqual(result['status'],'pending_registration')
        self.assertTrue((recovery.parent.parent/'archive-registration-errors/test-job.json').is_file())

    def test_exchange_failure_restores_code_and_retains_later_data(self):
        old=self.live/'activity_mvp/feedback/sources.py';new=self.candidate/'core/activity_mvp/feedback/sources.py'
        new.write_text('candidate fixture');recovery=self.root/'recovery';recovery.mkdir(mode=0o700);(recovery/'activity_mvp/feedback').mkdir(parents=True);shutil.copy2(old,recovery/'activity_mvp/feedback/sources.py')
        app=self.root/'AGI.app';app.mkdir();(app/'old').write_text('old app');(self.candidate/'build/AGI.app').mkdir(parents=True);(self.candidate/'build/AGI.app/new').write_text('new app')
        data=self.live/'data/unique-new-record.txt';data.write_text('later user data')
        def fail():data.write_text('new data written during publication');raise ValueError('injected acceptance failure')
        with self.assertRaisesRegex(ValueError,'acceptance failure'):
            release.exchange_pair(self.candidate,self.live,app,recovery,['activity_mvp/feedback/sources.py'],release.core_hash(self.candidate/'core'),fail)
        self.assertEqual(old.read_text(),(recovery/'activity_mvp/feedback/sources.py').read_text())
        self.assertTrue((app/'old').exists());self.assertEqual(data.read_text(),'new data written during publication')
        self.assertTrue(json.loads((recovery/'exchange.rolled-back.json').read_text())['current_data_preserved'])

if __name__=='__main__':unittest.main()
