"""Exact native test evidence; a successful build is never a test result."""
from pathlib import Path
import hashlib
import json
import plistlib
import stat
import subprocess

from .store import atomic, packed, digest

CONTRACT = 'agi-exact-swift-tests-v1'
COMMAND = ['/bin/bash', 'tools/run-swift-release-tests.sh']
XCODE = Path('/Applications/Xcode.app')
DEVELOPER = XCODE/'Contents/Developer'


def toolchain_identity(*, verify_signature=False):
    # This is a fixed Apple test runtime, not an arbitrary caller-selected path.
    # /Applications is the standard admin-writable container; the bundle and
    # every developer component itself must be root-owned and non-writable.
    for path in (Path('/Applications'), XCODE, XCODE/'Contents', DEVELOPER):
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
                or (path != Path('/Applications') and info.st_mode & 0o022)):
            raise PermissionError('swift_test_toolchain_not_trusted')
    info_path = XCODE/'Contents/Info.plist'
    if info_path.is_symlink(): raise PermissionError('swift_test_toolchain_alias')
    info = plistlib.loads(info_path.read_bytes())
    if info.get('CFBundleIdentifier') != 'com.apple.dt.Xcode' or info.get('CFBundleShortVersionString') != '27.0':
        raise PermissionError('swift_test_toolchain_version_unapproved')
    if verify_signature:
        subprocess.run(['/usr/bin/codesign','--verify','--strict','-R',
                        '=anchor apple generic and identifier "com.apple.dt.Xcode"',str(XCODE)],
                       check=True,capture_output=True,timeout=180)
    binary = DEVELOPER/'Toolchains/XcodeDefault.xctoolchain/usr/bin/swift-frontend'
    for parent in binary.parents:
        if parent == XCODE: break
        meta = parent.lstat()
        if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != 0 or meta.st_mode & 0o022:
            raise PermissionError('swift_test_compiler_parent_mutable')
    if binary.is_symlink() or not binary.is_file(): raise PermissionError('swift_test_compiler_alias')
    st = binary.stat()
    if st.st_uid != 0 or st.st_mode & 0o022: raise PermissionError('swift_test_compiler_mutable')
    from .project_repair import trusted_swift_sdk
    sdk = trusted_swift_sdk()
    settings = sdk/'SDKSettings.json'
    return {'developer':str(DEVELOPER),'version':info['CFBundleShortVersionString'],
            'bundle_info_sha256':hashlib.sha256(info_path.read_bytes()).hexdigest(),
            'resource_seal_sha256':hashlib.sha256((XCODE/'Contents/_CodeSignature/CodeResources').read_bytes()).hexdigest(),
            'compiler_sha256':hashlib.sha256(binary.read_bytes()).hexdigest(),
            'sdk':str(sdk),'sdk_settings_sha256':hashlib.sha256(settings.read_bytes()).hexdigest()}


def environment(volume, python):
    from .project_repair import trusted_swift_sdk
    return {'DEVELOPER_DIR':str(DEVELOPER),
            'PATH':str(Path(python).resolve().parent)+':/usr/bin:/bin:/usr/sbin:/sbin',
            'PAM_REQUIRE_SWIFT_TESTING':'1','PAM_SWIFT_TEST_SDK':str(trusted_swift_sdk()),
            'PAM_SWIFT_TEST_ISOLATED_ROOT':str(volume),
            'PAM_SWIFT_TEST_EVIDENCE_DIR':str(volume/'evidence')}


def validate_evidence(payload):
    if not isinstance(payload,dict): raise ValueError('swift_exact_evidence_type')
    if payload.get('contract') != CONTRACT: raise ValueError('swift_exact_evidence_contract')
    files = payload['files']
    if not isinstance(files,dict): raise ValueError('swift_exact_evidence_files_type')
    if any(not isinstance(name,str) or '/' in name or not isinstance(value,str) for name,value in files.items()):
        raise ValueError('swift_exact_evidence_files')
    summary = json.loads(files['summary.json']); status = json.loads(files['gate-status.json'])
    expected = json.loads(files['expected-tests.json'])
    if any(not isinstance(item,dict) for item in (summary,status,expected)):
        raise ValueError('swift_exact_evidence_object_type')
    counts = expected['active_counts']
    if (not isinstance(counts,dict) or set(counts) != {'xctest','swift-testing'}
            or any(type(value) is not int for value in counts.values())):
        raise ValueError('swift_exact_evidence_counts_type')
    if (status.get('status') != 'passed' or status.get('exit_code') != 0
            or summary.get('contract_version') != 'PAMSwiftReleaseTestEvidenceV1'
            or summary.get('status') != 'passed' or summary.get('swift_testing_module') != 'available'
            or counts.get('xctest',0) <= 0 or counts.get('swift-testing',0) <= 0
            or summary.get('expected_xctest') != counts['xctest']
            or summary.get('expected_swift_testing') != counts['swift-testing']
            or summary.get('executed_filtered_xctest') != counts['xctest']
            or summary.get('executed_full_xctest') != counts['xctest']
            or summary.get('executed_full_swift_testing') != counts['swift-testing']
            or summary.get('executed_full_total') != sum(counts.values())
            or any(summary.get(k) != 0 for k in ('skipped','failures','errors'))):
        raise ValueError('swift_exact_tests_not_executed')
    for name in ('list.log','xctest.log','xctest.xml','full.log','full.xml','full-swift-testing.xml'):
        if not files.get(name): raise ValueError('swift_exact_raw_evidence_missing')
    return summary


def run(store, root, timeout, extra_read=()):
    from . import project_repair as repair
    if not repair._active(store): raise PermissionError('paused')
    before = toolchain_identity(verify_signature=True)
    with repair.compiler_volume(root) as volume:
        result = repair._sandbox_check(store,root,COMMAND,timeout,
            (*extra_read,XCODE),False,volume,swift_suite=True)
        files = {}
        for path in sorted((volume/'evidence').glob('*')):
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 2*1024*1024:
                raise PermissionError('swift_exact_evidence_type_or_size')
            files[path.name] = path.read_text()
        if sum(len(v.encode()) for v in files.values()) > 8*1024*1024:
            raise PermissionError('swift_exact_evidence_too_large')
        result.swift_test_evidence = {'contract':CONTRACT,'toolchain':before,'files':files}
        if toolchain_identity(verify_signature=True) != before: raise PermissionError('swift_test_toolchain_changed')
        atomic(repair.private_directory(store.root/'checks')/'last-compiler-check.log',result.stdout+'\n'+result.stderr)
    result.compiler_volume = json.loads((volume.parent/'receipt.json').read_text())
    return result


def save(logs, result):
    payload = result.swift_test_evidence
    summary = validate_evidence(payload)
    path = logs/'swift-exact-evidence.json'
    atomic(path,packed(payload))
    return {'swift_contract':CONTRACT,'swift_evidence_sha256':digest(path.read_text()),
            'executed_tests':summary['executed_full_total'],'toolchain':payload['toolchain']}


def checkpoint_valid(logs, gate):
    try:
        path = logs/'swift-exact-evidence.json'
        if (gate.get('swift_contract') != CONTRACT or path.is_symlink()
                or digest(path.read_text()) != gate.get('swift_evidence_sha256')): return False
        payload = json.loads(path.read_text()); summary = validate_evidence(payload)
        return (summary['executed_full_total'] == gate.get('executed_tests')
                and payload['toolchain'] == gate.get('toolchain') == toolchain_identity(verify_signature=True))
    except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError): return False
