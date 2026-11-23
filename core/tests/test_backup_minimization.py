from pathlib import Path
import hashlib, json, shutil, sqlite3, tempfile, unittest, subprocess, os, plistlib
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
        with sqlite3.connect(self.live/'data/activity.sqlite') as d:d.execute("UPDATE meta SET value=? WHERE key='schema_version'",(str(SCHEMA_VERSION+1),))
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
        app=self.root/'PSI.app';app.mkdir();(app/'old').write_text('old app');(self.candidate/'build/PSI.app').mkdir(parents=True);(self.candidate/'build/PSI.app/new').write_text('new app')
        data=self.live/'data/unique-new-record.txt';data.write_text('later user data')
        def fail():data.write_text('new data written during publication');raise ValueError('injected acceptance failure')
        with self.assertRaisesRegex(ValueError,'acceptance failure'):
            release.exchange_pair(self.candidate,self.live,app,recovery,['activity_mvp/feedback/sources.py'],release.core_hash(self.candidate/'core'),fail)
        self.assertEqual(old.read_text(),(recovery/'activity_mvp/feedback/sources.py').read_text())
        self.assertTrue((app/'old').exists());self.assertEqual(data.read_text(),'new data written during publication')
        self.assertTrue(json.loads((recovery/'exchange.rolled-back.json').read_text())['current_data_preserved'])


class OperatorRecoveryEvidenceTests(unittest.TestCase):
    """Synthetic operator receipts test binding; they never authorize production."""
    def setUp(self):
        MinimalRecoveryTests.setUp(self)
        for base in (self.live,self.candidate/'core'):
            (base/'activity_mvp/display.py').write_text('def label(value):\n    return str(value)\n')
        (self.candidate/'core/activity_mvp/display.py').write_text('def label(value):\n    return str(value).strip()\n')
        subprocess.run(['git','init','-q',str(self.candidate)],check=True)
        subprocess.run(['git','-C',str(self.candidate),'add','core'],check=True)
        subprocess.run(['git','-C',str(self.candidate),'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','synthetic fixture'],check=True)
        revision=subprocess.check_output(['git','-C',str(self.candidate),'rev-parse','HEAD'],text=True).strip()
        source={p.relative_to(self.candidate).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (self.candidate/'core').rglob('*') if p.is_file()}
        self.verification={'operator_release':True,'signature':True,'revision':revision,
                           'core_hash':release.core_hash(self.candidate/'core'),'source_manifest':source,
                           'tests':{name:{'passed':True} for name in ('core','evolution','swift','interaction','privacy','publication','comparison','build')}}
        app=self.candidate/'build/PSI.app/Contents';app.mkdir(parents=True)
        (app/'Info.plist').write_bytes(plistlib.dumps({'PAMCoreCodeHash':self.verification['core_hash'],'PAMGitRevision':revision[:12]}))
        (app/'binary-fixture').write_bytes(b'synthetic app, not executable')
        self.review_path=self.root/'independent-review.json'
        self.review={'schema':'independent-operator-database-effects-v1','database_effects':'unchanged',
                     'initialization_effects':'unchanged','evidence':['synthetic display-only function; initialization unchanged'],
                     'binding':release.operator_database_effect_binding(self.live,self.candidate,self.verification)}
        self.save_review()

    def save_review(self,raw=None):
        self.review_path.write_bytes(raw if raw is not None else json.dumps(self.review).encode())
        self.review_path.chmod(0o600)
        self.verification['database_effect_review']={'path':str(self.review_path),'sha256':hashlib.sha256(self.review_path.read_bytes()).hexdigest()}

    def policy(self):
        return release.database_snapshot_policy(self.live,self.candidate,self.feedback,verification=self.verification)

    def refresh_qualification(self):
        subprocess.run(['git','-C',str(self.candidate),'add','-A','core'],check=True)
        subprocess.run(['git','-C',str(self.candidate),'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','new synthetic qualification'],check=True)
        revision=subprocess.check_output(['git','-C',str(self.candidate),'rev-parse','HEAD'],text=True).strip()
        self.verification.update(revision=revision,core_hash=release.core_hash(self.candidate/'core'),
            source_manifest={p.relative_to(self.candidate).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in (self.candidate/'core').rglob('*') if p.is_file()})
        (self.candidate/'build/PSI.app/Contents/Info.plist').write_bytes(plistlib.dumps(
            {'PAMCoreCodeHash':self.verification['core_hash'],'PAMGitRevision':revision[:12]}))

    def test_reviewed_display_change_omits_database_snapshot(self):
        result=self.policy();self.assertFalse(result['databases_required']);self.assertIn('operator_reviewed',result['reason'])
        recovery=self.root/'reviewed-recovery';recovery.mkdir()
        with patch.object(release,'backup_database',side_effect=AssertionError('unnecessary database copy')):
            release.prepare_recovery(self.live,self.candidate,recovery,self.feedback,verification=self.verification)
        self.assertFalse((recovery/'activity.sqlite').exists())

    def test_unreviewed_display_change_remains_unknown(self):
        self.assertTrue(release.database_snapshot_policy(self.live,self.candidate,self.feedback)['databases_required'])

    def test_operator_flag_alone_is_not_omission_evidence(self):
        self.verification.pop('database_effect_review');self.assertTrue(self.policy()['databases_required'])

    def test_automatic_candidate_cannot_consume_operator_evidence(self):
        self.verification['operator_release']=False;self.assertTrue(self.policy()['databases_required'])

    def test_live_code_drift_invalidates_omission(self):
        (self.live/'activity_mvp/display.py').write_text('def label(value): return value\n')
        self.assertTrue(self.policy()['databases_required'])

    def test_candidate_code_and_source_manifest_drift_invalidates_omission(self):
        (self.candidate/'core/activity_mvp/display.py').write_text('def label(value): return value.upper()\n')
        self.assertTrue(self.policy()['databases_required'])

    def test_paired_application_drift_invalidates_omission(self):
        (self.candidate/'build/PSI.app/Contents/binary-fixture').write_bytes(b'changed native behavior')
        self.assertTrue(self.policy()['databases_required'])

    def test_initializer_dependency_change_never_uses_omission(self):
        (self.candidate/'core/activity_mvp/storage.py').write_text('new initializer')
        self.refresh_qualification()
        with self.assertRaisesRegex(ValueError,'backup_review_initialization_or_dependency_changed'):
            release.operator_database_effect_binding(self.live,self.candidate,self.verification)
        result=self.policy();self.assertTrue(result['databases_required'])
        self.assertEqual(result['detail'],'backup_review_initialization_or_dependency_changed')

    def test_changed_import_is_not_assumed_side_effect_free(self):
        (self.candidate/'core/activity_mvp/display.py').write_text('import unknown_dependency\ndef label(value): return str(value)\n')
        self.refresh_qualification()
        with self.assertRaisesRegex(ValueError,'backup_review_import_dependency_changed'):
            release.operator_database_effect_binding(self.live,self.candidate,self.verification)
        result=self.policy();self.assertTrue(result['databases_required'])
        self.assertEqual(result['detail'],'backup_review_import_dependency_changed')

    def test_new_runtime_module_requires_recovery_even_after_qualification(self):
        (self.candidate/'core/activity_mvp/new_dependency.py').write_text('VALUE = 1\n')
        self.refresh_qualification()
        with self.assertRaisesRegex(ValueError,'backup_review_initialization_or_dependency_changed'):
            release.operator_database_effect_binding(self.live,self.candidate,self.verification)

    def test_dependency_change_outside_python_edit_surface_still_snapshots(self):
        (self.candidate/'core/pyproject.toml').write_text('changed dependency')
        self.assertTrue(self.policy()['databases_required'])

    def test_non_python_runtime_resource_cannot_use_automatic_omission(self):
        (self.candidate/'core/activity_mvp/display.py').write_bytes((self.live/'activity_mvp/display.py').read_bytes())
        (self.candidate/'core/activity_mvp/schema.sql').write_text('DROP TABLE protected_data;')
        self.assertTrue(release.database_snapshot_policy(self.live,self.candidate,self.feedback)['databases_required'])

    def test_no_positive_data_effects_conclusion_means_snapshot(self):
        self.review['database_effects']='unknown';self.save_review();self.assertTrue(self.policy()['databases_required'])

    def test_operator_app_only_change_requires_positive_review(self):
        (self.live/'activity_mvp/display.py').write_bytes((self.candidate/'core/activity_mvp/display.py').read_bytes())
        self.verification.pop('database_effect_review')
        self.assertTrue(self.policy()['databases_required'])
        self.review['binding']=release.operator_database_effect_binding(self.live,self.candidate,self.verification)
        self.save_review();self.assertFalse(self.policy()['databases_required'])

    def test_non_regular_review_reference_does_not_block_or_omit(self):
        fifo=self.root/'review-fifo';os.mkfifo(fifo,0o600)
        self.verification['database_effect_review']={'path':str(fifo),'sha256':'0'*64}
        self.assertTrue(self.policy()['databases_required'])

    def test_duplicate_json_keys_and_nonfinite_values_fail_closed(self):
        for raw in [b'{"schema":"a","schema":"b"}',b'{"value":NaN}']:
            with self.subTest(raw=raw):
                self.save_review(raw);self.assertTrue(self.policy()['databases_required'])

    def test_review_file_alias_and_mutation_fail_closed(self):
        self.review_path.write_text('changed receipt');self.assertTrue(self.policy()['databases_required'])
        self.save_review();link=self.root/'review-alias.json';link.symlink_to(self.review_path)
        self.verification['database_effect_review']['path']=str(link);self.assertTrue(self.policy()['databases_required'])

if __name__=='__main__':unittest.main()
