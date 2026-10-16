"""Trusted baseline coverage and isolated candidate verification.

Only the accepted baseline executes on the host. Candidate code, generated tests
and every fixed test that reads/calls changed code remain in the OS sandbox.
Unknown child-process dependencies are conservatively rerun in that sandbox.
"""
from __future__ import annotations
import argparse
import ast
import importlib
import json
import os
import re
import stat
from pathlib import Path
import subprocess
import sys
import threading
import unittest
import weakref
from collections import Counter

GENERATED = {'test_project_candidate_regression.py','test_feedback_candidate_regression.py'}
# This precise status/stop fixture omits the entire feedback package. Its
# original real HTTP/start/stop/restart assertions remain unchanged.
NATIVE_CHILD = 'test_agi_integration.AGIIntegrationTests.test_real_client_restart_and_duplicate_start_preserve_stop'
LEAVES = {'core/activity_mvp/feedback/sources.py','core/activity_mvp/feedback/journal.py'}
NATIVE_COMMAND = 'import sys;sys.path.insert(0,sys.argv[1]);from activity_mvp.evolution_bridge import main;main()'
CONNECTOR_CHILD = 'test_core_network.CoreNetworkTests.test_ai_connector_managed_start_stop_and_pid_safety'
CAPTURE_PROCESS_QUERY = 'test_core_network.CoreNetworkTests.test_capture_now_api_respects_pause'
FORGED_PROCESS_CHILD = 'test_phase3_native.Phase3NativeTests.test_force_stop_never_signals_forged_app_argv'


def regular_file(path):
    try: return stat.S_ISREG(Path(path).lstat().st_mode)
    except OSError: return False


def private_fixture(path, name):
    """An exact real directory inside this run's private temporary root."""
    try:
        path = Path(path); temp = Path(os.environ['TMPDIR'])
        details = path.lstat()
        return (path.is_absolute() and path.resolve(strict=True) == path and path.name == name
                and temp.is_absolute() and temp.resolve(strict=True) == temp and path.is_relative_to(temp)
                and path != temp and stat.S_ISDIR(details.st_mode) and details.st_uid == os.getuid()
                and stat.S_IMODE(details.st_mode) == 0o700)
    except (OSError, KeyError, TypeError, ValueError): return False


def independent_metadata_child(args, changed):
    if not set(changed) <= LEAVES: return False
    executable, command, cwd, env = args
    if executable != '/usr/bin/xattr' or cwd is not None or env is not None or not isinstance(command,(list,tuple)):
        return False
    prefix = ['/usr/bin/xattr','-w','com.apple.metadata:com_apple_backup_excludeItem','com.apple.backupd']
    read = ['/usr/bin/xattr','-p','com.apple.metadata:com_apple_backup_excludeItem']
    return (list(command[:-1]) in (prefix,read)
            and private_fixture(command[-1], 'confirmation_evidence.noindex'))


def independent_connector_child(identifier,args,changed):
    if not set(changed) <= LEAVES: return False
    executable,command,cwd,env = args
    if not isinstance(command,(list,tuple)): return False
    if (identifier == CAPTURE_PROCESS_QUERY and executable == 'ps'
            and command == ['ps','-o','%cpu=,rss=','-p',str(os.getpid())]
            and cwd is None and env is None and os.environ.get('PATH') == '/usr/bin:/bin:/usr/sbin:/sbin'):
        return True
    if identifier != CONNECTOR_CHILD: return False
    # These two system process queries execute no Python or candidate source.
    if (executable == 'ps' and len(command)==5 and command[:2]==['ps','-p']
            and str(command[2]).isdigit() and int(command[2])>0 and command[3]=='-o'
            and command[4] in {'stat=','command='} and cwd is None and env is None
            and os.environ.get('PATH') == '/usr/bin:/bin:/usr/sbin:/sbin'):
        return True
    if (executable != sys.executable or len(command)!=11 or command[:4]!=[sys.executable,'-m','activity_mvp','--db']
            or command[5:10]!=['ai-connector','serve','--host','127.0.0.1','--port']
            or not str(command[10]).isdigit() or not 0<int(command[10])<65536
            or not isinstance(env,dict) or not private_fixture(cwd,'connector-core')): return False
    fixture = Path(cwd)
    expected = {**os.environ,'PYTHONPATH':str(fixture),'PERSONAL_AI_MONITOR_AI_CONNECTOR_MANAGED':'1'}
    if env != expected or env.get('PYTHONNOUSERSITE')!='1' or env.get('PYTHONSAFEPATH')!='1': return False
    if Path(command[4]) != fixture.parent/'activity.sqlite': return False
    return (all(regular_file(fixture/'activity_mvp'/name) for name in ('__init__.py','__main__.py','ai_connector.py'))
            and not (fixture/'activity_mvp/feedback').exists())


def independent_native_child(identifier, args, changed):
    if identifier != NATIVE_CHILD or not set(changed) <= LEAVES: return False
    executable, command, cwd, env = args
    if (not isinstance(command,(list,tuple)) or len(command) != 7
            or executable != sys.executable or command[0] != sys.executable
            or command[1:5] != ['-I','-B','-c',NATIVE_COMMAND]
            or command[6] not in {'status','stop'}
            or env != {'PATH':'/usr/bin:/bin','LANG':'en_US.UTF-8'}): return False
    fixture = Path(command[5])
    if (not fixture.is_absolute() or fixture.resolve() != fixture or fixture.name != 'client-core'
            or not all(regular_file(fixture/'activity_mvp'/name) for name in ('__init__.py','evolution_bridge.py'))
            or (fixture/'activity_mvp/feedback').exists()): return False
    return all(not (fixture/p.removeprefix('core/')).exists() for p in LEAVES)


def independent_forged_process_child(identifier, args, changed, fixtures):
    """The fixed fake-argv fixture only runs system sleep and its exact lookup.

    The pretend application is inert data, never an executable input. Bind the
    process query to the same private fixture first observed at sleep dispatch.
    """
    if identifier != FORGED_PROCESS_CHILD or not set(changed) <= LEAVES: return False
    executable, command, cwd, env = args
    if cwd is not None or not isinstance(command,(list,tuple)): return False
    prefix = ['/bin/bash','-c','exec -a "$1" /bin/sleep 30','pam-forged-argv']
    if executable == '/bin/bash' and list(command[:-1]) == prefix and env is None:
        try:
            path = Path(command[-1]); root = path.parents[4]
            expected = root/'Applications/Personal AI Monitor.app/Contents/MacOS/PersonalAIMonitor'
            metadata = path.lstat()
            if (path != expected or path.resolve(strict=True) != path or not private_fixture(root,root.name)
                    or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                    or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o755
                    or path.read_bytes() != b'trusted but not running'): return False
        except (OSError,IndexError,TypeError,ValueError): return False
        fixtures[identifier] = path
        return True
    path = fixtures.get(identifier)
    expected_env = {'GIT_CONFIG_GLOBAL':'/dev/null','GIT_CONFIG_NOSYSTEM':'1',
                    'GIT_NO_REPLACE_OBJECTS':'1','GIT_OPTIONAL_LOCKS':'0','LANG':'C','LC_ALL':'C',
                    'PATH':'/usr/bin:/bin:/usr/sbin:/sbin','DEVELOPER_DIR':'/Library/Developer/CommandLineTools'}
    if not path or env != expected_env: return False
    if (executable == '/usr/bin/pgrep'
            and list(command) == ['/usr/bin/pgrep','-U',str(os.getuid()),'-f',
                                  '^'+re.escape(str(path))+'([[:space:]]|$)']): return True
    # Exact read-only OS identity probes; no path, code or environment supplied
    # by a candidate is interpreted. They can only inspect a positive PID.
    if (executable == '/bin/ps' and len(command) == 5
            and list(command[:4]) == ['/bin/ps','-o','uid=,lstart=','-p']):
        return str(command[4]).isdigit() and int(command[4]) > 0
    if (executable == '/usr/sbin/lsof' and len(command) == 8
            and list(command[:3]) == ['/usr/sbin/lsof','-a','-p']
            and list(command[4:]) == ['-d','txt','-F','fDni']):
        return str(command[3]).isdigit() and int(command[3]) > 0
    return False



NATIVE_SWIFT = {
    'test_native_swift.NativeSwiftTests.test_real_vision_helper_compiles_to_mode_0700':'vision_ocr.swift',
    'test_native_swift.NativeSwiftTests.test_swift_helper_keeps_one_shot_output_and_serves_two_jsonl_requests':'session_state.swift',
}
BOOT_QUERY = 'test_task_closure_continuation.ContinuationTests.test_native_runtime_requires_exact_turn_source_and_fresh_age'


def independent_boot_query(identifier, args, changed):
    return (identifier == BOOT_QUERY and set(changed) <= LEAVES
            and args == ('/usr/sbin/sysctl',['/usr/sbin/sysctl','-n','kern.bootsessionuuid'],None,None))


def independent_swift_fixture(identifier, args, changed, root, binaries):
    """Only unchanged fixed Swift source, with private compiler output.

    These native fixtures never import Python. Unknown compilation or execution
    continues to require the candidate sandbox.
    """
    if identifier not in NATIVE_SWIFT or not set(changed) <= LEAVES: return False
    executable, command, cwd, env = args
    if cwd is not None or env is not None or not isinstance(command,(list,tuple)): return False
    if os.environ.get('PATH') != '/usr/bin:/bin:/usr/sbin:/sbin': return False
    source = Path(root)/'core/tools'/NATIVE_SWIFT[identifier]
    if (executable == 'xcrun' and len(command) == 5
            and list(command[:4]) == ['xcrun','swiftc',str(source),'-o']
            and os.environ.get('DEVELOPER_DIR') == '/Library/Developer/CommandLineTools'):
        try:
            target = Path(command[4]); parent = target.parent
            if (not regular_file(source) or source.resolve(strict=True) != source
                    or target.resolve() != target or target.exists() or target.is_symlink()
                    or not private_fixture(parent,parent.name)): return False
        except (OSError,TypeError,ValueError): return False
        binaries[identifier] = target
        return True
    target = binaries.get(identifier)
    if (NATIVE_SWIFT[identifier] != 'session_state.swift' or not target
            or executable != str(target) or list(command) not in ([str(target)],[str(target),'--serve'])): return False
    try:
        info = target.lstat()
        return (regular_file(target) and target.resolve(strict=True) == target
                and info.st_uid == os.getuid() and info.st_nlink == 1 and bool(info.st_mode & 0o100)
                and not info.st_mode & 0o022)
    except OSError: return False


def changed_functions(before, after):
    def functions(text):
        return {n.name:ast.dump(n,include_attributes=False) for n in ast.parse(text).body if isinstance(n,ast.FunctionDef)}
    old, new = functions(before), functions(after)
    return sorted(k for k in old.keys() | new.keys() if old.get(k) != new.get(k))


def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite): yield from cases(item)
        else: yield item


def module_files(directory):
    return [p.name for p in sorted(directory.glob('test_*.py')) if p.name not in GENERATED]


def suite_at(directory, filename):
    if filename not in module_files(directory): raise PermissionError('fixed_test_module_unknown')
    loader = unittest.TestLoader()
    return loader.discover(str(directory),pattern=filename)


def run_baseline_module(root, spec, output, filename):
    root = Path(root).resolve(); candidate = Path(spec['candidate']).resolve()
    if root == candidate: raise PermissionError('baseline_is_candidate')
    current = [None]; affected = set(); unscoped = set(); unscoped_details = []; reasons = {}
    changed = {str(root/p):set(names) for p,names in spec['changed'].items()}
    # Load only accepted source modules before coverage starts. Later discovery
    # reads are real dependencies, including module-level cached source text.
    monitored = []
    for relative,names in spec['changed'].items():
        module = importlib.import_module(relative.removeprefix('core/').removesuffix('.py').replace('/','.'))
        monitored.extend(getattr(module,name).__code__ for name in names if hasattr(module,name))
    # Isolated native-client fixtures may only copy the unchanged components.
    native_checks = []; native_process_fixtures = {}; native_swift_binaries = {}
    main_thread = threading.get_ident()
    owners = weakref.WeakKeyDictionary()
    original_start = threading.Thread.start
    def start(thread,*args,**kwargs):
        owners[thread] = current[0] if threading.get_ident() == main_thread else owners.get(threading.current_thread())
        return original_start(thread,*args,**kwargs)
    threading.Thread.start = start
    def mark(reason='changed_function'):
        if current[0]: reasons.setdefault(current[0],set()).add(reason)
        if threading.get_ident() != main_thread:
            owner = owners.get(threading.current_thread())
            if not owner or owner != current[0]:
                unscoped.add('unowned_or_outliving_dependency_thread')
                detail = {'reason':reason,'owner_test':owner,'current_test':current[0]}
                if detail not in unscoped_details and len(unscoped_details)<16:unscoped_details.append(detail)
            else: affected.add(owner)
        elif current[0]: affected.add(current[0])
        else: unscoped.add('changed_code_outside_test')
    def entered(code,offset): mark()
    def audit(event,args):
        if event == 'open' and args and isinstance(args[0],(str,bytes)):
            path = os.path.realpath(os.fsdecode(args[0]))
            if path == str(candidate) or path.startswith(str(candidate)+os.sep):
                raise PermissionError('candidate_read_in_baseline_verifier')
            if path in changed: mark('changed_source_read')
        if event == 'subprocess.Popen':
            # Even a no-op child is conservatively included, except the one
            # fixed native fixture whose complete source copy is unchanged.
            if candidate.as_posix() in repr(args): raise PermissionError('candidate_child_in_baseline_verifier')
            if independent_metadata_child(args,spec['changed']):
                pass  # Exact native metadata operation on this test's private directory.
            elif (independent_native_child(current[0],args,spec['changed'])
                  or independent_connector_child(current[0],args,spec['changed'])
                  or independent_forged_process_child(current[0],args,spec['changed'],native_process_fixtures)
                  or independent_boot_query(current[0],args,spec['changed'])
                  or independent_swift_fixture(current[0],args,spec['changed'],root,native_swift_binaries)):
                native_checks.append({'test':current[0],'editable_python_absent':True})
            else:
                mark('unknown_subprocess')
        if event == 'exec' and args and Path(getattr(args[0],'co_filename','')).name in GENERATED:
            # Accepted old regression files may be copied/scanned as data by
            # the fixed privacy gate, but never executed on the host.
            raise PermissionError('generated_test_in_baseline_verifier')
        if event == 'exec' and args and getattr(args[0],'co_filename',None) in changed:
            # A reload creates new function code objects. Retest the whole
            # fresh-process module rather than relying on stale instrumentation.
            unscoped.add('changed_module_reexecuted')
    sys.addaudithook(audit)
    # Monitor only accepted changed function code objects. Profiling every
    # Python/C call distorts unrelated bounded-time I/O and concurrency tests.
    # Reloaded modules still create audited source reads and are retested.
    monitor = sys.monitoring
    tool = next((i for i in range(5,-1,-1) if monitor.get_tool(i) is None),None)
    if tool is None: raise RuntimeError('fixed_coverage_monitor_unavailable')
    monitor.use_tool_id(tool,'agi-fixed-dependencies')
    monitor.register_callback(tool,monitor.events.PY_START,entered)
    for code in monitored: monitor.set_local_events(tool,code,monitor.events.PY_START)
    suite = suite_at(root/spec['suite'],filename)
    identifiers = [t.id() for t in cases(suite)]
    class Result(unittest.TextTestResult):
        def startTest(self,test): current[0]=test.id();super().startTest(test)
        def stopTest(self,test): super().stopTest(test);current[0]=None
    result = unittest.TextTestRunner(verbosity=2,resultclass=Result).run(suite)
    if any(t is not threading.main_thread() and t.is_alive() for t in threading.enumerate()):
        unscoped.add('background_thread_outlived_module')
    for code in monitored: monitor.set_local_events(tool,code,0)
    monitor.register_callback(tool,monitor.events.PY_START,None);monitor.free_tool_id(tool)
    threading.Thread.start = original_start
    if unscoped: affected.update(identifiers)
    report = {'baseline':str(root),'candidate':str(candidate),'suite':spec['suite'],'module':filename,'tests':identifiers,
        'native_fixture_checks':native_checks,
        'dependency_reasons':{k:sorted(v) for k,v in reasons.items()},
        'affected':sorted(affected),'unscoped':sorted(unscoped),'unscoped_details':unscoped_details,
        'run':result.testsRun,'passed':result.wasSuccessful(),
        'skipped':[t.id() for t,_ in result.skipped],
        'failed':[t.id() for t,_ in result.failures+result.errors]}
    Path(output).write_text(json.dumps(report,sort_keys=True))
    return 0 if result.wasSuccessful() else 1


def run_candidate_module(root, report_path, filename):
    report = json.loads(Path(report_path).read_text())
    if report.get('passed') is not True or report['candidate'] != str(Path(root).resolve()):
        raise PermissionError('baseline_report_unverified')
    module = report['modules'][filename]
    if not module['selected']: raise PermissionError('unselected_fixed_test_module')
    suite = suite_at(Path(root)/report['suite'],filename)
    all_cases = list(cases(suite)); identifiers = [t.id() for t in all_cases]
    if Counter(identifiers) != Counter(module['tests']):
        raise PermissionError('fixed_test_inventory_changed')
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(all_cases))
    if Counter(t.id() for t,_ in result.skipped) - Counter(module['skipped']):
        raise PermissionError('new_candidate_test_skip')
    print(json.dumps({'baseline_tests':len(identifiers),'candidate_tests':result.testsRun,
                      'passed':result.wasSuccessful(),'skipped':len(result.skipped)}))
    return 0 if result.wasSuccessful() else 1


def run_baseline(root, spec_path, output):
    """Fresh processes prevent test-module caches from hiding dependencies."""
    root = Path(root).resolve();spec=json.loads(Path(spec_path).read_text())
    reports=Path(output).with_name(Path(output).stem+'-modules');reports.mkdir(mode=0o700,exist_ok=True)
    combined={'baseline':str(root),'candidate':spec['candidate'],'suite':spec['suite'],'modules':{},
              'tests':[],'affected':[],'unscoped':[],'unscoped_details':[],'native_fixture_checks':[],
              'run':0,'passed':True,'skipped':[],'failed':[]}
    for filename in module_files(root/spec['suite']):
        out=reports/(filename+'.json')
        child=subprocess.run([sys.executable,'-I','-B',str(Path(__file__).resolve()),'baseline-module',str(root),
                              str(spec_path),'--output',str(out),'--module',filename])
        part=json.loads(out.read_text())
        if part['module']!=filename or part['baseline']!=str(root) or part['candidate']!=spec['candidate']:
            raise PermissionError('fixed_module_receipt_changed')
        selected=bool(part['affected'] or part['unscoped'])
        combined['modules'][filename]={'tests':part['tests'],'selected':selected,'skipped':part['skipped']}
        for key in ('tests','skipped','failed','native_fixture_checks','unscoped_details'):combined[key].extend(part[key])
        combined['unscoped'].extend(filename+':'+reason for reason in part['unscoped'])
        if selected:combined['affected'].extend(part['tests'])
        combined['run']+=part['run'];combined['passed'] &= child.returncode==0 and part['passed']
    Path(output).write_text(json.dumps(combined,sort_keys=True))
    print(json.dumps({'full_fixed_tests':combined['run'],'passed':combined['passed'],
                      'candidate_modules':sum(m['selected'] for m in combined['modules'].values())}))
    return 0 if combined['passed'] else 1


def run_candidate(root, report_path):
    report=json.loads(Path(report_path).read_text());root=Path(root).resolve()
    if report.get('passed') is not True or report['candidate']!=str(root):raise PermissionError('baseline_report_unverified')
    if set(module_files(root/report['suite']))!=set(report['modules']):raise PermissionError('fixed_test_inventory_changed')
    passed=True
    for filename,module in report['modules'].items():
        if not module['selected']:continue
        child=subprocess.run([sys.executable,'-I','-B',str(Path(__file__).resolve()),'candidate-module',str(root),
                              str(report_path),'--module',filename])
        passed &= child.returncode==0
    return 0 if passed else 1


def main():
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=('baseline','candidate','baseline-module','candidate-module'))
    parser.add_argument('root');parser.add_argument('input');parser.add_argument('--output')
    parser.add_argument('--module')
    args=parser.parse_args()
    root=Path(args.root).resolve();sys.path[:0]=[str(root/'core'),str(root/'core/evolution')]
    if args.mode == 'candidate':return run_candidate(root,args.input)
    if args.mode == 'baseline':return run_baseline(root,args.input,args.output)
    if args.mode == 'candidate-module':return run_candidate_module(root,args.input,args.module)
    spec=json.loads(Path(args.input).read_text());return run_baseline_module(root,spec,args.output,args.module)

if __name__ == '__main__':raise SystemExit(main())
