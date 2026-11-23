import json
from contextlib import contextmanager
from pathlib import Path
import plistlib
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from activity_mvp.feedback import project_repair as repair, swift_verification as swift


class SwiftVerificationTests(unittest.TestCase):
    @contextmanager
    def fixed_toolchain_fixture(self, version='27.2', identifier='com.apple.dt.Xcode', mutable=False):
        # All contents are synthetic; root ownership is represented only for
        # these fixture paths. No installed Xcode or real signature is used.
        with tempfile.TemporaryDirectory() as temp:
            app=Path(temp)/'Xcode.app';developer=app/'Contents/Developer'
            compiler=developer/'Toolchains/XcodeDefault.xctoolchain/usr/bin/swift-frontend'
            compiler.parent.mkdir(parents=True);compiler.write_bytes(b'synthetic compiler')
            info=app/'Contents/Info.plist'
            info.write_bytes(plistlib.dumps({'CFBundleIdentifier':identifier,
                'CFBundleShortVersionString':version}))
            seal=app/'Contents/_CodeSignature/CodeResources'
            seal.parent.mkdir();seal.write_bytes(b'synthetic seal')
            sdk=Path(temp)/'SDK';sdk.mkdir();(sdk/'SDKSettings.json').write_text('{}')
            real_lstat=Path.lstat;real_stat=Path.stat
            def metadata(path, original, *args, **kwargs):
                path=Path(path)
                if path==Path('/Applications'):
                    return SimpleNamespace(st_mode=stat.S_IFDIR|0o755,st_uid=0)
                result=original(path,*args,**kwargs)
                if path==app or app in path.parents:
                    mode=result.st_mode|(0o020 if mutable and path==developer else 0)
                    return SimpleNamespace(st_mode=mode,st_uid=0)
                return result
            with patch.object(swift,'XCODE',app),patch.object(swift,'DEVELOPER',developer),\
                 patch.object(Path,'lstat',lambda path,*a,**kw:metadata(path,real_lstat,*a,**kw)),\
                 patch.object(Path,'stat',lambda path,*a,**kw:metadata(path,real_stat,*a,**kw)),\
                 patch.object(repair,'trusted_swift_sdk',return_value=sdk),\
                 patch.object(swift.subprocess,'run',return_value=subprocess.CompletedProcess([],0,b'',b'')) as signature:
                yield app,signature

    def test_fixed_current_official_toolchain_is_accepted(self):
        with self.fixed_toolchain_fixture() as (app,signature):
            try:
                actual=swift.toolchain_identity(verify_signature=True)
            except PermissionError as error:
                self.fail('fixed current version rejected: '+str(error))
            self.assertEqual(actual['version'],'27.2')
            self.assertEqual(actual['developer'],str(app/'Contents/Developer'))
            self.assertEqual(len(actual['compiler_sha256']),64)
            signature.assert_called_once()
            self.assertIn('=anchor apple generic and identifier "com.apple.dt.Xcode"',signature.call_args.args[0])

    def test_other_versions_and_bundle_identity_remain_rejected(self):
        for version,identifier in [('27.0','com.apple.dt.Xcode'),('27.1','com.apple.dt.Xcode'),
                ('27.3','com.apple.dt.Xcode'),('28.0','com.apple.dt.Xcode'),
                ('27.1 beta','com.apple.dt.Xcode'),('27.2 beta','com.apple.dt.Xcode'),
                ('27.2','other.bundle')]:
            with self.subTest(version=version,identifier=identifier):
                with self.fixed_toolchain_fixture(version,identifier) as (_,signature):
                    with self.assertRaisesRegex(PermissionError,'version_unapproved'):
                        swift.toolchain_identity(verify_signature=True)
                    signature.assert_not_called()

    def test_signature_mutability_and_info_alias_still_fail_closed(self):
        with self.fixed_toolchain_fixture(mutable=True):
            with self.assertRaisesRegex(PermissionError,'not_trusted'):
                swift.toolchain_identity(verify_signature=True)
        with self.fixed_toolchain_fixture() as (_,signature):
            signature.side_effect=subprocess.CalledProcessError(1,['synthetic signature check'])
            with self.assertRaises(subprocess.CalledProcessError):
                swift.toolchain_identity(verify_signature=True)
        with self.fixed_toolchain_fixture() as (app,signature):
            info=app/'Contents/Info.plist';info.rename(info.with_name('original.plist'))
            info.symlink_to('original.plist')
            with self.assertRaisesRegex(PermissionError,'toolchain_alias'):
                swift.toolchain_identity(verify_signature=True)
            signature.assert_not_called()

    def test_previous_toolchain_checkpoint_is_not_reused_after_version_change(self):
        with tempfile.TemporaryDirectory() as temp:
            logs=Path(temp);result=subprocess.CompletedProcess([],0,'','')
            result.swift_test_evidence=self.evidence()
            result.swift_test_evidence['toolchain']={'version':'27.1','compiler_sha256':'unchanged'}
            gate=swift.save(logs,result)
            with patch.object(swift,'toolchain_identity',return_value={'version':'27.2','compiler_sha256':'unchanged'}):
                self.assertFalse(swift.checkpoint_valid(logs,gate))

    def evidence(self):
        # Synthetic unit fixture; real release evidence is generated by the
        # existing exact-list shell gate and retained separately.
        summary = {'contract_version':'PAMSwiftReleaseTestEvidenceV1','status':'passed',
            'swift_testing_module':'available','expected_xctest':2,'expected_swift_testing':1,
            'executed_filtered_xctest':2,'executed_full_xctest':2,
            'executed_full_swift_testing':1,'executed_full_total':3,
            'skipped':0,'failures':0,'errors':0}
        files = {name:'synthetic fixed evidence' for name in
            ('list.log','xctest.log','xctest.xml','full.log','full.xml','full-swift-testing.xml')}
        files.update({'summary.json':json.dumps(summary),
            'gate-status.json':json.dumps({'status':'passed','exit_code':0}),
            'expected-tests.json':json.dumps({'active_counts':{'xctest':2,'swift-testing':1}})})
        return {'contract':swift.CONTRACT,'toolchain':{'fixed':'fixture'},'files':files}

    def test_zero_or_partial_execution_cannot_pass(self):
        self.assertEqual(swift.validate_evidence(self.evidence())['executed_full_total'],3)
        for field,value in [('executed_full_total',0),('executed_filtered_xctest',0),
                            ('executed_full_swift_testing',0),('skipped',1),('failures',1)]:
            payload=self.evidence();summary=json.loads(payload['files']['summary.json'])
            summary[field]=value;payload['files']['summary.json']=json.dumps(summary)
            with self.assertRaises(ValueError):swift.validate_evidence(payload)

    def test_success_summary_without_original_results_is_rejected(self):
        for name in ('list.log','xctest.xml','full.xml','full-swift-testing.xml'):
            payload=self.evidence();del payload['files'][name]
            with self.assertRaises(ValueError):swift.validate_evidence(payload)
        payload=self.evidence();payload['files']['gate-status.json']='{"status":"failed","exit_code":0}'
        with self.assertRaises(ValueError):swift.validate_evidence(payload)

    def test_malformed_objects_fail_closed(self):
        for payload in ([],None,'text',{'contract':swift.CONTRACT,'files':[]}):
            with self.assertRaises(ValueError):swift.validate_evidence(payload)
        for name in ('summary.json','gate-status.json','expected-tests.json'):
            payload=self.evidence();payload['files'][name]='[]'
            with self.assertRaises(ValueError):swift.validate_evidence(payload)

    def test_legacy_or_changed_swift_checkpoint_never_reuses_exit_zero(self):
        with tempfile.TemporaryDirectory() as temp:
            logs=Path(temp)
            self.assertFalse(swift.checkpoint_valid(logs,{'passed':True,'output_sha256':'old'}))
            result=subprocess.CompletedProcess([],0,'','');result.swift_test_evidence=self.evidence()
            gate=swift.save(logs,result)
            with patch.object(swift,'toolchain_identity',return_value={'fixed':'fixture'}):
                self.assertTrue(swift.checkpoint_valid(logs,gate))
                (logs/'swift-exact-evidence.json').write_text('{}')
                self.assertFalse(swift.checkpoint_valid(logs,gate))

    def test_passed_swift_gate_uses_exact_inventory_runner(self):
        with tempfile.TemporaryDirectory() as temp:
            logs=Path(temp)
            result=subprocess.CompletedProcess([],0,'actual fixed runner log','')
            result.swift_test_evidence=self.evidence()
            with patch.object(repair,'source_manifest',return_value={'fixed':'unchanged'}),\
                 patch.object(repair,'sandbox_check',return_value=result) as check:
                gate=repair._passed(None,logs,['/usr/bin/swift','test'],'swift',logs)
            self.assertEqual(check.call_args.args[2],swift.COMMAND)
            self.assertEqual(gate['swift_contract'],swift.CONTRACT)
            self.assertEqual(gate['executed_tests'],3)
            self.assertTrue((logs/'swift-exact-evidence.json').is_file())

    def test_native_environment_requires_both_frameworks_and_private_paths(self):
        with patch.object(repair,'trusted_swift_sdk',return_value=Path('/fixed/sdk')):
            env=swift.environment(Path('/private/isolated'),'/usr/local/bin/python3.12')
        self.assertEqual(env['PAM_REQUIRE_SWIFT_TESTING'],'1')
        self.assertEqual(env['PAM_SWIFT_TEST_EVIDENCE_DIR'],'/private/isolated/evidence')
        self.assertEqual(env['PAM_SWIFT_TEST_ISOLATED_ROOT'],'/private/isolated')
        self.assertEqual(env['PAM_SWIFT_TEST_SDK'],'/fixed/sdk')


if __name__=='__main__':unittest.main()
