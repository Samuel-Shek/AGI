import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from activity_mvp.feedback import fixed_verification as fixed


class FixedVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve()
        self.old=self.root/'baseline';self.new=self.root/'candidate'
        (self.old/'core/tests').mkdir(parents=True)
        (self.old/'core/probe.py').write_text('def value():\n return 1\n')
        self.report=self.root/'report.json'
    def tearDown(self): self.temp.cleanup()
    def prepare(self, body):
        (self.old/'core/tests/test_fixture.py').write_text('import unittest,probe\n'+body)
        shutil.copytree(self.old,self.new)
        (self.new/'core/probe.py').write_text('def value():\n return 2\n')
    def baseline(self):
        spec=self.root/'spec.json'
        spec.write_text(json.dumps({'candidate':str(self.new),'suite':'core/tests','changed':{'core/probe.py':['value']}}))
        return subprocess.run([sys.executable,'-I','-B',fixed.__file__,'baseline',str(self.old),str(spec),'--output',str(self.report)],capture_output=True,text=True)
    def candidate(self):
        return subprocess.run([sys.executable,'-I','-B',fixed.__file__,'candidate',str(self.new),str(self.report)],capture_output=True,text=True)
    def test_called_function_is_retested_and_failure_is_not_hidden(self):
        self.prepare('class Fixture(unittest.TestCase):\n def test_changed(self): self.assertEqual(probe.value(),1)\n def test_other(self): self.assertTrue(True)\n')
        result=self.baseline();self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(len(json.loads(self.report.read_text())['affected']),2)
        self.assertNotEqual(self.candidate().returncode,0)
    def test_unchanged_assertion_stays_in_full_baseline(self):
        self.prepare('class Fixture(unittest.TestCase):\n def test_unchanged(self): self.assertEqual(2+2,4)\n')
        self.assertEqual(self.baseline().returncode,0)
        report=json.loads(self.report.read_text());self.assertEqual(report['run'],1);self.assertEqual(report['affected'],[])
        self.assertEqual(self.candidate().returncode,0)
    def test_unknown_subprocess_requires_isolated_candidate(self):
        self.prepare('import subprocess,sys\nclass Fixture(unittest.TestCase):\n def test_child(self): subprocess.run([sys.executable,"-c","pass"],check=True)\n')
        self.assertEqual(self.baseline().returncode,0)
        self.assertEqual(json.loads(self.report.read_text())['affected'],['test_fixture.Fixture.test_child'])
    def test_module_level_source_cache_retests_whole_inventory(self):
        self.prepare('from pathlib import Path\nCACHED=Path(probe.__file__).read_text()\nclass Fixture(unittest.TestCase):\n def test_cache(self): self.assertIn("return 1",CACHED)\n def test_other(self): pass\n')
        self.assertEqual(self.baseline().returncode,0)
        report=json.loads(self.report.read_text());self.assertEqual(set(report['affected']),set(report['tests']))
        self.assertTrue(report['unscoped']);self.assertNotEqual(self.candidate().returncode,0)
    def test_joined_background_function_retests_owning_test(self):
        self.prepare('import threading\nclass Fixture(unittest.TestCase):\n def test_thread(self):\n  t=threading.Thread(target=probe.value);t.start();t.join()\n def test_other(self): pass\n')
        self.assertEqual(self.baseline().returncode,0)
        report=json.loads(self.report.read_text());self.assertEqual(set(report['affected']),set(report['tests']))
        self.assertEqual(report['unscoped'],[])
    def test_dependency_thread_outliving_owner_retests_everything(self):
        self.prepare('import threading\nready=threading.Event()\nthread=None\nclass Fixture(unittest.TestCase):\n def test_1_start(self):\n  global thread\n  thread=threading.Thread(target=lambda:(ready.wait(),probe.value()));thread.start()\n def test_2_join(self): ready.set();thread.join()\n')
        self.assertEqual(self.baseline().returncode,0)
        report=json.loads(self.report.read_text());self.assertEqual(set(report['affected']),set(report['tests']))
        self.assertTrue(report['unscoped'])
    def test_repeated_skip_count_cannot_increase(self):
        self.prepare('calls=0\nclass Fixture(unittest.TestCase):\n def test_repeated(self):\n  global calls\n  calls+=1\n  if probe.value()==2 or calls==1:self.skipTest("fixture")\ndef load_tests(loader,tests,pattern):return unittest.TestSuite([Fixture("test_repeated"),Fixture("test_repeated")])\n')
        self.assertEqual(self.baseline().returncode,0)
        self.assertEqual(len(json.loads(self.report.read_text())['skipped']),1)
        self.assertIn('new_candidate_test_skip',self.candidate().stderr)
    def test_reload_and_later_cached_assertion_are_retested(self):
        self.prepare('import importlib\nCACHED=None\nclass Fixture(unittest.TestCase):\n def test_1_reload(self): importlib.reload(probe)\n def test_2_cache(self):\n  global CACHED\n  CACHED=probe.value()\n def test_3_assert(self): self.assertEqual(CACHED,1)\n')
        self.assertEqual(self.baseline().returncode,0)
        report=json.loads(self.report.read_text());self.assertEqual(set(report['affected']),set(report['tests']))
        self.assertNotEqual(self.candidate().returncode,0)
    def test_modules_have_separate_import_state_in_both_layers(self):
        self.prepare('class Fixture(unittest.TestCase):\n def test_changes_cache(self): probe.cached=probe.value()\n')
        second='import unittest,probe\nclass Separate(unittest.TestCase):\n def test_no_shared_cache(self): self.assertFalse(hasattr(probe,"cached"))\n'
        for root in (self.old,self.new):(root/'core/tests/test_second.py').write_text(second)
        self.assertEqual(self.baseline().returncode,0)
        self.assertFalse(json.loads(self.report.read_text())['modules']['test_second.py']['selected'])
        self.assertEqual(self.candidate().returncode,0)
    def test_baseline_cannot_read_candidate_or_load_generated_test(self):
        self.prepare('from pathlib import Path\nclass Fixture(unittest.TestCase):\n def test_read(self):\n  with self.assertRaises(PermissionError): Path('+repr(str(self.new/'core/probe.py'))+').read_text()\n')
        (self.old/'core/tests/test_project_candidate_regression.py').write_text('raise AssertionError("generated execution")\n')
        self.assertEqual(self.baseline().returncode,0)
        self.assertEqual(json.loads(self.report.read_text())['run'],1)
    def test_failed_baseline_and_changed_inventory_block_candidate(self):
        self.prepare('class Fixture(unittest.TestCase):\n def test_bad(self): self.fail("baseline defect")\n')
        self.assertNotEqual(self.baseline().returncode,0);self.assertNotEqual(self.candidate().returncode,0)
        report=json.loads(self.report.read_text());report['passed']=True;self.report.write_text(json.dumps(report))
        (self.new/'core/tests/test_extra.py').write_text('import unittest\nclass Extra(unittest.TestCase):\n def test_extra(self): pass\n')
        self.assertIn('fixed_test_inventory_changed',self.candidate().stderr)
    def test_file_dependency_and_new_skip_are_not_ignored(self):
        self.prepare('from pathlib import Path\nclass Fixture(unittest.TestCase):\n def test_read(self):\n  text=Path(probe.__file__).read_text()\n  if "return 2" in text: self.skipTest("candidate changed")\n')
        self.assertEqual(self.baseline().returncode,0)
        self.assertEqual(len(json.loads(self.report.read_text())['affected']),1)
        self.assertIn('new_candidate_test_skip',self.candidate().stderr)
    def test_native_exception_requires_exact_command_and_absent_editable_code(self):
        fixture=self.root/'client-core';(fixture/'activity_mvp').mkdir(parents=True)
        (fixture/'activity_mvp/evolution_bridge.py').write_text('# fixed fixture')
        (fixture/'activity_mvp/__init__.py').write_text('# ordinary package')
        args=[sys.executable,[sys.executable,'-I','-B','-c',fixed.NATIVE_COMMAND,str(fixture),'status'],None,
              {'PATH':'/usr/bin:/bin','LANG':'en_US.UTF-8'}]
        self.assertTrue(fixed.independent_native_child(fixed.NATIVE_CHILD,args,fixed.LEAVES))
        self.assertFalse(fixed.independent_native_child('untrusted_fixture',args,fixed.LEAVES))
        self.assertFalse(fixed.independent_native_child(fixed.NATIVE_CHILD,args,{'core/other.py'}))
        (fixture/'activity_mvp/feedback').mkdir()
        self.assertFalse(fixed.independent_native_child(fixed.NATIVE_CHILD,args,fixed.LEAVES))

    def test_accepted_generated_file_can_be_copied_but_not_executed(self):
        generated=self.old/'core/tests/test_feedback_candidate_regression.py'
        self.prepare('from pathlib import Path\nimport shutil,runpy\nclass Fixture(unittest.TestCase):\n def test_data(self):\n  original=Path('+repr(str(generated))+')\n  copied=original.parent.parent/"test_feedback_candidate_regression.py"\n  shutil.copy2(original,copied)\n  self.assertIn("old accepted",copied.read_text())\n  with self.assertRaises(PermissionError): runpy.run_path(str(original))\n  with self.assertRaises(PermissionError): runpy.run_path(str(copied))\n  with self.assertRaises(PermissionError): __import__("test_feedback_candidate_regression")\n')
        generated.write_text('# old accepted generated fixture\nraise AssertionError("must not execute")\n')
        result=self.baseline();self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(self.report.read_text())['run'],1)

    def test_metadata_exception_is_exact_and_private(self):
        directory=self.root/'confirmation_evidence.noindex';directory.mkdir(mode=0o700)
        prefix=['/usr/bin/xattr','-w','com.apple.metadata:com_apple_backup_excludeItem','com.apple.backupd']
        with patch.dict(os.environ,{'TMPDIR':str(self.root)}):
            args=['/usr/bin/xattr',prefix+[str(directory)],None,None]
            self.assertTrue(fixed.independent_metadata_child(args,fixed.LEAVES))
            self.assertTrue(fixed.independent_metadata_child(['/usr/bin/xattr',['/usr/bin/xattr','-p',prefix[2],str(directory)],None,None],fixed.LEAVES))
            for command in (prefix+[str(self.root)],prefix[:-1]+['other',str(directory)],['/usr/bin/xattr','-r','-d',prefix[2],str(directory)]):
                self.assertFalse(fixed.independent_metadata_child(['/usr/bin/xattr',command,None,None],fixed.LEAVES))
            self.assertFalse(fixed.independent_metadata_child([*args[:3],{}],fixed.LEAVES))
            self.assertFalse(fixed.independent_metadata_child([args[0],args[1],str(self.root),None],fixed.LEAVES))
            directory.chmod(0o755)
            self.assertFalse(fixed.independent_metadata_child(args,fixed.LEAVES))

    def test_connector_exception_requires_closed_fixture_and_exact_environment(self):
        fixture=self.root/'connector-core';(fixture/'activity_mvp').mkdir(parents=True);fixture.chmod(0o700)
        for name in ('__init__.py','__main__.py','ai_connector.py'):(fixture/'activity_mvp'/name).write_text('# fixed fixture')
        with patch.dict(os.environ,{'TMPDIR':str(self.root),'PYTHONPATH':'','PYTHONNOUSERSITE':'1','PYTHONSAFEPATH':'1','PATH':'/usr/bin:/bin:/usr/sbin:/sbin'}):
            env={**os.environ,'PYTHONPATH':str(fixture),'PERSONAL_AI_MONITOR_AI_CONNECTOR_MANAGED':'1'}
            command=[sys.executable,'-m','activity_mvp','--db',str(self.root/'activity.sqlite'),'ai-connector','serve','--host','127.0.0.1','--port','12345']
            args=[sys.executable,command,str(fixture),env]
            self.assertTrue(fixed.independent_connector_child(fixed.CONNECTOR_CHILD,args,fixed.LEAVES))
            self.assertFalse(fixed.independent_connector_child('other',args,fixed.LEAVES))
            self.assertFalse(fixed.independent_connector_child(fixed.CONNECTOR_CHILD,[*args[:3],{**env,'PYTHONPATH':str(self.old)}],fixed.LEAVES))
            query=['ps',['ps','-p','12345','-o','command='],None,None]
            self.assertTrue(fixed.independent_connector_child(fixed.CONNECTOR_CHILD,query,fixed.LEAVES))
            self.assertFalse(fixed.independent_connector_child(fixed.CONNECTOR_CHILD,['ps',['ps','aux'],None,None],fixed.LEAVES))
            init=fixture/'activity_mvp/__init__.py';init.unlink()
            self.assertFalse(fixed.independent_connector_child(fixed.CONNECTOR_CHILD,args,fixed.LEAVES))
            init.symlink_to(fixture/'activity_mvp/__main__.py')
            self.assertFalse(fixed.independent_connector_child(fixed.CONNECTOR_CHILD,args,fixed.LEAVES))
            init.unlink();init.write_text('# ordinary package')
            (fixture/'activity_mvp/feedback').mkdir()
            self.assertFalse(fixed.independent_connector_child(fixed.CONNECTOR_CHILD,args,fixed.LEAVES))

    def test_capture_meter_only_queries_own_process(self):
        with patch.dict(os.environ,{'PATH':'/usr/bin:/bin:/usr/sbin:/sbin'}):
            command=['ps','-o','%cpu=,rss=','-p',str(os.getpid())]
            self.assertTrue(fixed.independent_connector_child(fixed.CAPTURE_PROCESS_QUERY,['ps',command,None,None],fixed.LEAVES))
            self.assertFalse(fixed.independent_connector_child(fixed.CAPTURE_PROCESS_QUERY,['ps',command[:-1]+['1'],None,None],fixed.LEAVES))

    def test_forged_process_fixture_binds_inert_private_path_and_exact_query(self):
        path=self.root/'Applications/Personal AI Monitor.app/Contents/MacOS/PersonalAIMonitor'
        path.parent.mkdir(parents=True);path.write_bytes(b'trusted but not running');path.chmod(0o755)
        command=['/bin/bash','-c','exec -a "$1" /bin/sleep 30','pam-forged-argv',str(path)]
        args=['/bin/bash',command,None,None];fixtures={}
        env={'GIT_CONFIG_GLOBAL':'/dev/null','GIT_CONFIG_NOSYSTEM':'1','GIT_NO_REPLACE_OBJECTS':'1',
             'GIT_OPTIONAL_LOCKS':'0','LANG':'C','LC_ALL':'C','PATH':'/usr/bin:/bin:/usr/sbin:/sbin',
             'DEVELOPER_DIR':'/Library/Developer/CommandLineTools'}
        query=['/usr/bin/pgrep','-U',str(os.getuid()),'-f','^'+fixed.re.escape(str(path))+'([[:space:]]|$)']
        with patch.dict(os.environ,{'TMPDIR':str(self.root.parent)}):
            self.assertFalse(fixed.independent_forged_process_child(fixed.FORGED_PROCESS_CHILD,['/usr/bin/pgrep',query,None,env],fixed.LEAVES,fixtures))
            self.assertTrue(fixed.independent_forged_process_child(fixed.FORGED_PROCESS_CHILD,args,fixed.LEAVES,fixtures))
            self.assertTrue(fixed.independent_forged_process_child(fixed.FORGED_PROCESS_CHILD,['/usr/bin/pgrep',query,None,env],fixed.LEAVES,fixtures))
            for probe in (['/bin/ps','-o','uid=,lstart=','-p','123'],['/usr/sbin/lsof','-a','-p','123','-d','txt','-F','fDni']):
                self.assertTrue(fixed.independent_forged_process_child(fixed.FORGED_PROCESS_CHILD,[probe[0],probe,None,env],fixed.LEAVES,fixtures))
            self.assertFalse(fixed.independent_forged_process_child(fixed.FORGED_PROCESS_CHILD,['/bin/ps',['/bin/ps','aux'],None,env],fixed.LEAVES,fixtures))
            self.assertFalse(fixed.independent_forged_process_child('other',args,fixed.LEAVES,fixtures))
            self.assertFalse(fixed.independent_forged_process_child(fixed.FORGED_PROCESS_CHILD,args,{'core/other.py'},fixtures))
            self.assertFalse(fixed.independent_forged_process_child(fixed.FORGED_PROCESS_CHILD,['/bin/bash',[*command[:2],'exec "$1"',*command[3:]],None,None],fixed.LEAVES,fixtures))
            self.assertFalse(fixed.independent_forged_process_child(fixed.FORGED_PROCESS_CHILD,['/usr/bin/pgrep',query,None,{**env,'DEVELOPER_DIR':'/tmp/other'}],fixed.LEAVES,fixtures))
            path.write_bytes(b'changed')
            self.assertFalse(fixed.independent_forged_process_child(fixed.FORGED_PROCESS_CHILD,args,fixed.LEAVES,{}))


    def test_native_boot_query_is_exact_and_leaf_only(self):
        args=('/usr/sbin/sysctl',['/usr/sbin/sysctl','-n','kern.bootsessionuuid'],None,None)
        self.assertTrue(fixed.independent_boot_query(fixed.BOOT_QUERY,args,fixed.LEAVES))
        self.assertFalse(fixed.independent_boot_query('other',args,fixed.LEAVES))
        self.assertFalse(fixed.independent_boot_query(fixed.BOOT_QUERY,args,{'core/other.py'}))
        self.assertFalse(fixed.independent_boot_query(fixed.BOOT_QUERY,(*args[:3],{}),fixed.LEAVES))
        self.assertFalse(fixed.independent_boot_query(fixed.BOOT_QUERY,(args[0],[args[0],'-a'],None,None),fixed.LEAVES))

    def test_native_swift_fixture_requires_exact_source_private_output_and_bound_binary(self):
        ident=next(k for k,v in fixed.NATIVE_SWIFT.items() if v=='session_state.swift')
        source=self.old/'core/tools/session_state.swift';source.parent.mkdir();source.write_text('import Foundation')
        scratch=self.root/'scratch';scratch.mkdir(mode=0o700);target=scratch/'session_state';binaries={}
        args=('xcrun',['xcrun','swiftc',str(source),'-o',str(target)],None,None)
        env={'TMPDIR':str(self.root),'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','DEVELOPER_DIR':'/Library/Developer/CommandLineTools'}
        with patch.dict(os.environ,env):
            execute=(str(target),[str(target),'--serve'],None,None)
            self.assertFalse(fixed.independent_swift_fixture(ident,execute,fixed.LEAVES,self.old,binaries))
            self.assertTrue(fixed.independent_swift_fixture(ident,args,fixed.LEAVES,self.old,binaries))
            self.assertFalse(fixed.independent_swift_fixture('other',args,fixed.LEAVES,self.old,{}))
            self.assertFalse(fixed.independent_swift_fixture(ident,args,{'core/other.py'},self.old,{}))
            self.assertFalse(fixed.independent_swift_fixture(ident,(*args[:3],{}),fixed.LEAVES,self.old,{}))
            self.assertFalse(fixed.independent_swift_fixture(ident,('xcrun',['xcrun','swift',str(source)],None,None),fixed.LEAVES,self.old,{}))
            target.write_bytes(b'fixed fixture');target.chmod(0o700)
            self.assertTrue(fixed.independent_swift_fixture(ident,execute,fixed.LEAVES,self.old,binaries))
            self.assertFalse(fixed.independent_swift_fixture(ident,(str(target),[str(target),'--other'],None,None),fixed.LEAVES,self.old,binaries))
            target.chmod(0o777)
            self.assertFalse(fixed.independent_swift_fixture(ident,execute,fixed.LEAVES,self.old,binaries))
            target.unlink();target.symlink_to(source)
            self.assertFalse(fixed.independent_swift_fixture(ident,args,fixed.LEAVES,self.old,{}))
            target.unlink();scratch.chmod(0o755)
            self.assertFalse(fixed.independent_swift_fixture(ident,args,fixed.LEAVES,self.old,{}))


if __name__ == '__main__': unittest.main()
