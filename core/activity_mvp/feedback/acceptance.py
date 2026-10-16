"""Operator acceptance, bound to fixed controller bytes and installed release."""
import hashlib
import json
from pathlib import Path
import plistlib
import time
from .store import packed, atomic

GUARDS = ('control.py','cli.py','acceptance.py','store.py','policy.py','model.py','repair.py','release.py','recovery_storage.py','service.py','isolation.py','probes.py','proactive.py','proactive_contracts.py','fixed_verification.py','swift_verification.py','conversation_scope.py',
          'work.py','routing.py','autonomy.py','projects.py','project_repair.py','memory_autonomy.py','memory_operator.py','jev_ranking.py','jev_dispatch.py','memory_coverage.py','sources.py','sync_center.py','task_maintenance.py','evolution_notices.py', 'attention_native.py', 'reflection/store.py','reflection/sources.py','reflection/analysis.py','reflection/service.py','reflection/cli.py')

def guard_hashes():
    return {name:hashlib.sha256(((Path(__file__).parent.parent/name) if name.startswith('reflection/') or name in {'attention_native.py','conversation_scope.py'} else Path(__file__).with_name(name)).read_bytes()).hexdigest() for name in GUARDS}

def release_ready(store):
    cap=store.get('repair_capability',{})
    return (cap.get('enabled') is True and cap.get('guards')==guard_hashes()
            and store.get('isolation',{}).get('passed') is True
            and store.get('model_capability',{}).get('enabled') is True)

def activate(store, receipt_path):
    from .release import core_hash
    path=Path(receipt_path)
    receipt=json.loads(path.read_text())
    if not store.get('isolation',{}).get('passed') or not store.get('model_capability',{}).get('enabled'):
        raise PermissionError('isolation_or_model_not_verified')
    for key in ('sandbox_old_failed','sandbox_candidate_passed','paired_files_installed','rollback_preserved_data'):
        if receipt.get(key) is not True:raise ValueError('release_acceptance_incomplete')
    for key in ('core','evolution','swift','interaction','privacy'):
        gate=receipt['verification']['tests'][key]
        if gate.get('passed') is not True:raise ValueError('fixed_gate_failed')
        if 'log' in gate:
            from .store import digest
            if digest(Path(gate['log']).read_text())!=gate['sha256']:raise ValueError('gate_receipt_changed')
    conf=store.get('config');live=Path(conf['live_core']);app=Path(conf['application'])
    info=plistlib.loads((app/'Contents/Info.plist').read_bytes())
    if core_hash(live)!=info.get('PAMCoreCodeHash') or info['PAMCoreCodeHash']!=receipt['verification']['core_hash']:
        raise ValueError('live_release_not_verified')
    cap={'enabled':True,'reason':'自主处理已验收 · 登记项目的受限文字、显示和布局修复；技术缺口继续由系统核对',
         'guards':guard_hashes(),'verified_at':time.time(),'receipt':str(path)}
    atomic(store.root/'release-acceptance.json',packed(receipt))
    store.set('repair_capability',cap);store.log('repair_capability_verified',guards=cap['guards'])
    return cap


def continue_verified_release(store, item, receipt):
    """Carry acceptance over only a fixed-controller-approved leaf repair.

    Never accept a controller/policy change here. The old accepted guards,
    candidate manifest, full suites and current paired installation must agree.
    Operator changes still require activate(). This cannot enable a disabled cap.
    """
    if item.get('project_id') != 'agi': return False
    from .autonomy import active
    from .project_repair import installation_ready
    cap = store.get('repair_capability', {})
    current = guard_hashes()
    if cap.get('enabled') is not True or not active(store): return False
    if cap.get('guards') == current: return True
    verification = item.get('result', {}).get('verification', {})
    old = verification.get('accepted_guards', {})
    if not old or old != cap.get('guards') or set(old) != set(current): return False
    if {key for key in old if old[key] != current[key]} - {'sources.py'}: return False
    admission = verification.get('leaf_admission',{})
    if admission != {'before':old['sources.py'], 'after':current['sources.py'],
                     'policy':old['policy.py'], 'base':verification.get('base')}: return False
    manifest = verification.get('source_manifest', {})
    for key, sha in current.items():
        path = 'core/activity_mvp/' + (key if key.startswith('reflection/') or key in {'attention_native.py','conversation_scope.py'} else 'feedback/'+key)
        if manifest.get(path) != sha: return False
    if any(verification.get('tests',{}).get(key,{}).get('passed') is not True
           for key in ('core','evolution','swift','interaction','privacy','build')): return False
    if not installation_ready(item, receipt): return False
    store.set('repair_capability', {**cap, 'guards':current, 'verified_at':time.time(),
              'continuation':{'job':item['result']['job'],'revision':verification['revision']}})
    store.log('bounded_release_acceptance_continued', revision=verification['revision'])
    return True
