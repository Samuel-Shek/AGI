"""Installed project identities. Conversation paths are evidence, never grants."""
from __future__ import annotations
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
from .store import digest

# Dedicated non-Git installed adapter; never routed into the generic app publisher.
EXTERNAL_ADAPTERS = {'workplace-sync-center': {'module':'sync_center', 'policy':'sync-center-owner-approved-20260929-v1', 'publisher':'native_sync_supervisor'}}

ROOT = Path.home()/'Code/active'
PROJECTS = {
    'agi': {'repository': str(ROOT/'artificial-general-intelligence'), 'application': '/Applications/AGI.app',
            'bundle_id': 'app.personalai.monitor', 'markers': ('core/pyproject.toml', 'scripts/build-macos-app.sh'),
            'editable': ('core/activity_mvp/feedback/sources.py', 'core/activity_mvp/feedback/journal.py',
                         'apps/PersonalAIMonitorMenuBar/Sources/PersonalAIMonitorMenuBar/FeedbackView.swift',
                         'apps/PersonalAIMonitorMenuBar/Sources/PersonalAIMonitorMenuBar/EvolutionView.swift'),
            'regression': 'core/tests/test_project_candidate_regression.py'},
    'meeting-assistant': {'repository': str(ROOT/'meeting-assistant'), 'application': '/Applications/Meeting Assistant.app',
            'bundle_id': 'com.bigking.mianshi-assistant', 'markers': ('package.json', 'scripts/install-macos.sh'),
            'editable': ('src/subtitle.css', 'src/utils/attachmentText.js', 'src/utils/recognitionRouteNotice.mjs'),
            'regression': 'scripts/tests/project-candidate-regression.test.js'},
}

def git(root, *args, timeout=30, finite_read=False):
    env = {'PATH': '/usr/bin:/bin', 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_NO_REPLACE_OBJECTS': '1', 'LANG': 'C'}
    if Path('/Library/Developer/CommandLineTools/usr/bin/git').is_file():
        env.update(DEVELOPER_DIR='/Library/Developer/CommandLineTools',xcrun_nocache='1')
    command = ['/usr/bin/git', '-c', 'core.hooksPath=/dev/null', '-C', str(root), *args]
    if finite_read:
        if args not in {('rev-parse', '--show-toplevel'), ('rev-parse', 'HEAD'),
                       ('status', '--porcelain', '--untracked-files=normal'),
                       ('status', '--porcelain', '--untracked-files=no'), ('diff', 'HEAD', '--')}:
            raise PermissionError('finite_git_read_outside_scope')
        # Status otherwise writes an optional index refresh, which can leave a
        # lock behind when a bounded child times out. Identity reads never need it.
        env['GIT_OPTIONAL_LOCKS'] = '0'
        # Only this bounded identity read leaves the inherited Background type.
        # Persistent service, deadline, required write locks and model policy stay.
        if sys.platform == 'darwin': command = ['/usr/sbin/taskpolicy', '-a', *command]
    try:
        return subprocess.run(command,
                              check=True, capture_output=True, text=True, timeout=timeout, env=env).stdout.strip()
    except subprocess.TimeoutExpired as exc:
        # Safe controller-owned diagnostics; never persist arbitrary argv/output.
        if finite_read: exc.identity_read = {'operation':list(args), 'timeout_seconds':timeout}
        raise

def safe_path(path):
    path = Path(path).absolute()
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise PermissionError('project_symlink')
    if not path.is_dir() or path.stat().st_uid != os.getuid():
        raise PermissionError('project_identity_unavailable')
    return path

def identity(project_id, cache=None, *, finite_read=False):
    if cache is not None and project_id in cache: return cache[project_id]
    spec = PROJECTS.get(project_id)
    if not spec: raise PermissionError('project_not_registered')
    root = safe_path(spec['repository'])
    def read(*args): return git(root, *args, finite_read=finite_read)
    if Path(read('rev-parse', '--show-toplevel')) != root:
        raise PermissionError('project_root_mismatch')
    for name in spec['markers']:
        path = root/name
        if not path.is_file() or path.is_symlink(): raise PermissionError('project_marker_missing')
    if project_id == 'meeting-assistant':
        meta = json.loads((root/'package.json').read_text())
        if meta.get('name') != 'meeting-assistant' or meta.get('main') != 'src/index.js':
            raise PermissionError('project_marker_mismatch')
    result = {'project_id': project_id, 'repository': str(root), 'head': read('rev-parse', 'HEAD'),
            'working_tree': digest([read('status', '--porcelain', '--untracked-files=normal'), read('diff', 'HEAD', '--')]),
            'markers': {p: hashlib.sha256((root/p).read_bytes()).hexdigest() for p in spec['markers']}}
    if cache is not None: cache[project_id] = result
    return result

def _task_directories(store, incident):
    """Read only the referenced source header; never search task bodies for paths."""
    from .sources import reference_is_current
    rows = store.rows('SELECT m.*,s.platform,s.path source_root FROM messages m JOIN quotes q ON q.message_id=m.id '
                      'JOIN sources s ON s.id=m.source_id WHERE q.incident_id=?', (incident['id'],))
    found = []
    for row in rows:
        if row['platform'] not in ('codex', 'claude') or not reference_is_current(row): continue
        reference = json.loads(row['reference']); path = Path(reference.get('path', ''))
        source_root = safe_path(row['source_root'])
        if not path.is_absolute() or not path.is_relative_to(source_root) or path.is_symlink(): continue
        if path.stat().st_uid != os.getuid(): continue
        with path.open('rb') as handle:
            for _ in range(20):
                raw = handle.readline(65537)
                if not raw or len(raw) > 65536: break
                try: record = json.loads(raw)
                except ValueError: continue
                if row['platform'] == 'codex' and record.get('type') == 'session_meta':
                    payload = record.get('payload', {})
                    if payload.get('id') == row['session'] and isinstance(payload.get('cwd'), str):
                        found.append(payload['cwd'])
                    break
                if row['platform'] == 'claude' and record.get('sessionId') == row['session']:
                    if isinstance(record.get('cwd'), str): found.append(record['cwd'])
                    break
    return found

def resolve_project(store, incident):
    """Result status identified/waiting_evidence; no model inference is a binding."""
    from .sources import incident_admitted
    if not incident_admitted(store,incident["id"]):return {"status":"waiting_evidence","project_id":"","evidence":[],"reason":"owner_retired_or_source_unknown"}
    candidates = set(); evidence = []; preparation = {}
    cache = getattr(store, '_autonomy_project_identities', None)
    try:
        try: directories = _task_directories(store, incident)
        except (OSError, ValueError, PermissionError, subprocess.SubprocessError): directories = []
        for directory in directories:
            failed_key='unavailable-task-directory:'+str(directory)
            # One failed locator per directory and synchronization pass. A
            # failure never supplies identity and is rechecked on the next pass.
            if cache is not None and cache.get(failed_key):continue
            try:
                path = safe_path(directory)
                common = Path(git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir',timeout=3))
            except (OSError, ValueError, PermissionError, subprocess.SubprocessError):
                if cache is not None:cache[failed_key]=True
                continue
            matched = False
            for key, spec in PROJECTS.items():
                registered = safe_path(spec['repository'])
                cache_key = 'common:'+str(registered)
                known_common = cache.get(cache_key) if cache is not None else None
                if not known_common:
                    known_common = git(registered, 'rev-parse', '--path-format=absolute', '--git-common-dir')
                    if cache is not None: cache[cache_key] = known_common
                if common == Path(known_common):
                    matched = True
                    candidates.add(key); evidence.append({'kind': 'source_task_project', 'project_id': key, 'summary':'来源任务的当前项目与已核对的源码项目一致。'})
            canonical = common.parent
            if not matched and canonical.is_relative_to(ROOT) and canonical != ROOT and (canonical/'.git').is_dir():
                # Other current owner source tasks may prepare an isolated source
                # review, but gain no installation or production-write adapter.
                safe_path(canonical)
                markers = [p for p in ('package.json','pyproject.toml','Cargo.toml','Package.swift','AGENTS.md') if (canonical/p).is_file() and not (canonical/p).is_symlink()]
                if markers:
                    key = 'local-'+digest(str(canonical))[:12]
                    candidates.add(key)
                    preparation[key] = {'project_id':key, 'repository':str(canonical), 'head':git(canonical,'rev-parse','HEAD'),
                                        'working_tree':digest(git(canonical,'diff','HEAD','--')),
                                        'markers':{p:hashlib.sha256((canonical/p).read_bytes()).hexdigest() for p in markers}}
                    evidence.append({'kind':'source_task_project','project_id':key,'summary':'来源任务直接关联当前本地源码项目。'})
        # The owner already authorized these fixed products in POLICY. A verified
        # complaint can select one of those identities, never register a new path.
        from .sources import reference_is_current
        rows = store.rows('SELECT m.* FROM messages m JOIN quotes q ON q.message_id=m.id WHERE q.incident_id=?', (incident['id'],))
        aliases = {'agi': r'(?<![\w])AGI(?![\w])|Personal AI Monitor',
                   'meeting-assistant': r'Meeting Assistant|会议助手|面试助手'}
        for row in rows:
            if not reference_is_current(row): continue
            for key, pattern in aliases.items():
                if re.search(pattern, row['quote'], re.IGNORECASE):
                    candidates.add(key); evidence.append({'kind': 'verified_product_reference', 'project_id': key, 'summary':'已核对的原始反馈明确提到该软件，当前项目入口与预设授权一致。'})
        # Controller-written bindings are useful for an explicit owner task which
        # addresses a different repository. They cannot originate in model output.
        binding = store.get('project_bindings', {}).get(incident['id'], {})
        if binding.get('authority') == 'owner-approved-autonomy-20260921':
            key = binding.get('project_id')
            if key in PROJECTS and binding.get('repository') == PROJECTS[key]['repository']:
                candidates.add(key); evidence.append({'kind': 'owner_task_binding', 'project_id': key, 'summary':'本次任务已明确授权该项目，控制器再次核对了当前源码身份。'})
        if len(candidates) != 1:
            return {'status': 'waiting_evidence', 'project_id': '', 'evidence': evidence,
                    'reason': 'project_ambiguous' if candidates else 'project_evidence_missing'}
        key = candidates.pop(); current = preparation[key] if key in preparation else identity(key, cache)
        return {'status': 'identified', 'project_id': key, 'source_revision': digest(current),
                'identity': current, 'evidence': evidence, 'reason': '', 'prepare_only':key in preparation}
    except (OSError, ValueError, PermissionError, subprocess.SubprocessError) as exc:
        return {'status': 'waiting_evidence', 'project_id': '', 'evidence': evidence,
                'reason': str(exc)[:100] if isinstance(exc, PermissionError) else 'project_evidence_unavailable'}
