"""Owner-authorized, fixed task maintenance; never edits a scheduler or grants.

The scheduler retains its original owner, model and acceptance rules. The one
owner-authorized 20261016 file recheck may amend only its COUNT=1 time binding.
Only two registered tasks can load versioned, evidence-bound progress here.
All state is separate from activity, memory and the feedback databases.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import time
import tomllib
import uuid

from .store import atomic, digest, packed, private_directory

POLICY = 'agi-task-maintenance-20261008-v1'
SCHEDULE_PLAN = 'agi-core-value-20261016'
SCHEDULE_ISSUE_KEY = 'agi-file-merge-natural-acceptance-20261006'
TASKS = {'agi': '文件追踪修复复查', 'automation-9': '元认知上线验收与自然观察'}
SOURCE_NAMES = {'agi': 'agi-file-merge-followup-20261006', 'automation-9': 'metacognition-20261005'}
ZONE = dt.timezone(dt.timedelta(hours=8))
METACOGNITION_EARLIEST = '2026-10-13'
SCHEMA = '''
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY,version INTEGER NOT NULL,spec TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS task_versions(task TEXT NOT NULL,version INTEGER NOT NULL,spec TEXT NOT NULL,PRIMARY KEY(task,version));
CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,created REAL NOT NULL,payload TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending',request_uuid TEXT NOT NULL,message_id TEXT,
 attempted REAL,receipt TEXT NOT NULL DEFAULT '{}',retry_at REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS uses(id TEXT PRIMARY KEY,task TEXT NOT NULL,session TEXT NOT NULL,
 turn TEXT NOT NULL,version INTEGER NOT NULL,spec_sha256 TEXT NOT NULL,created REAL NOT NULL,
 result TEXT,UNIQUE(task,session,turn));
CREATE TABLE IF NOT EXISTS use_sources(id TEXT PRIMARY KEY,sources TEXT NOT NULL,spec TEXT NOT NULL);
'''


def default_root():
    return Path.home() / 'AI/LocalData/AGI-Evolution.nosync'


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value: raise ValueError('duplicate_json_field')
        value[key] = item
    return value


def read_file(path, limit=2 * 1024 * 1024):
    """Open every component without following aliases, then check one object."""
    path = Path(path).absolute()
    if '..' in path.parts: raise PermissionError('noncanonical_path')
    # POSIX search-only descriptors traverse known directory components without
    # requiring permission to list their contents. Darwin exposes O_SEARCH in
    # fcntl.h but some Python builds omit its named constant. Every component
    # still uses O_NOFOLLOW; the final file remains a bounded read-only open.
    directory_flags = getattr(os, 'O_SEARCH', 0x40000000 if sys.platform == 'darwin' else os.O_RDONLY) | os.O_DIRECTORY
    fd = os.open('/', directory_flags)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, directory_flags | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
        child = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            before = os.fstat(child)
            if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_nlink != 1:
                raise PermissionError('source_object_identity')
            if before.st_size > limit: raise ValueError('source_too_large')
            chunks = []; remaining = limit + 1
            while remaining:
                data = os.read(child, min(65536, remaining))
                if not data: break
                chunks.append(data); remaining -= len(data)
            body = b''.join(chunks); after = os.fstat(child)
            if len(body) > limit: raise ValueError('source_too_large')
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise ValueError('source_changed_during_read')
            return body
        finally: os.close(child)
    finally: os.close(fd)


def read_json(path):
    raw = read_file(path)
    value = json.loads(raw, object_pairs_hook=_pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite_json')))
    if not isinstance(value, dict): raise ValueError('source_object_required')
    return value, hashlib.sha256(raw).hexdigest()


class Ledger:
    def __init__(self, root=None, readonly=False):
        self.root = Path(root or default_root()).absolute(); self.readonly = readonly
        if not readonly: private_directory(self.root)
        path = self.root / 'evolution.sqlite'
        if path.exists():
            s = path.lstat()
            if not stat.S_ISREG(s.st_mode) or s.st_uid != os.getuid() or s.st_nlink != 1 or s.st_mode & 0o077:
                raise PermissionError('private_ledger_required')
        elif not readonly:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600); os.close(fd)
        self.db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2) if readonly else sqlite3.connect(path, timeout=2, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        if readonly: self.db.execute('PRAGMA query_only=ON')
        else:
            self.db.execute('PRAGMA synchronous=FULL'); self.db.executescript(SCHEMA)
    def close(self): self.db.close()
    def __enter__(self): return self
    def __exit__(self, *_): self.close()
    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default
    def set(self, key, value): self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, packed(value)))
    @contextlib.contextmanager
    def tx(self):
        self.db.execute('BEGIN IMMEDIATE')
        try: yield; self.db.execute('COMMIT')
        except BaseException: self.db.execute('ROLLBACK'); raise
    def event(self, payload, key=None):
        from .evolution_notices import validate_event
        validate_event(payload)
        identifier = key or digest(payload)
        self.db.execute('INSERT OR IGNORE INTO events(id,created,payload,request_uuid) VALUES(?,?,?,?)',
                        (identifier, time.time(), packed(payload), str(uuid.uuid4())))
        return identifier


def source_root(task):
    if task not in TASKS: raise PermissionError('task_not_authorized')
    return Path.home() / 'AI/ai-change-ledger/reports' / SOURCE_NAMES[task]


def checked_source(task, path):
    path = Path(path).absolute()
    if not path.is_relative_to(source_root(task)) or '..' in path.parts or path.suffix != '.json':
        raise PermissionError('source_outside_registered_task')
    value, sha = read_json(path)
    return value, {'path': str(path), 'sha256': sha}


def scheduler_metadata(task):
    path = Path.home() / '.codex/automations' / task / 'automation.toml'
    value = tomllib.loads(read_file(path).decode())
    # Everything except mutable dispatch counters and the operator's runner text.
    keys = ('kind', 'name', 'status', 'rrule', 'target_thread_id', 'model', 'reasoning_effort',
            'notification_policy', 'notificationPolicy', 'execution_environment', 'cwds', 'project_id')
    return {**{key: value[key] for key in keys if key in value}, 'prompt_sha256': digest(value.get('prompt', ''))}


def _schedule_keys():
    return ('schedule_intent:' + SCHEDULE_PLAN, 'schedule_commit:' + SCHEDULE_PLAN)


def _schedule_rrule(at, now):
    if type(at) is not int or at % 60 or not 60 <= at - now <= 48 * 3600:
        raise ValueError('bounded_future_minute_required')
    local = dt.datetime.fromtimestamp(at, ZONE)
    day = ('MO', 'TU', 'WE', 'TH', 'FR', 'SA', 'SU')[local.weekday()]
    return f'FREQ=WEEKLY;BYDAY={day};BYHOUR={local.hour};BYMINUTE={local.minute};BYSECOND=0;COUNT=1'


def _schedule_case(binding, identifier, *, completed=False):
    from ..task_closure import Store as ClosureStore, safe_authority, current, digest as closure_digest
    with ClosureStore(closure_database(), readonly=True) as closure:
        item = closure.get(identifier)
    target = str(source_root('agi').parent.parent)
    if (item['task_id'] not in binding['closure_tasks'] or item['target_root'] != target
            or item['fingerprint'] != closure_digest([target, SCHEDULE_ISSUE_KEY])
            or item['owner'] != 'codex:' + binding['owner_session']):
        raise PermissionError('original_bound_issue_required')
    allowed = {'pending', 'running', 'blocked', 'verification'} | ({'resolved'} if completed else set())
    if item['state'] not in allowed or item['dispatch_state'] != 'new' or not safe_authority(item):
        raise PermissionError('original_issue_waiting_stopped_or_dispatched')
    if not current(json.loads(item['evidence'])):
        raise ValueError('original_issue_evidence_changed')


def _schedule_read(ledger, body, *, current_version=True):
    row = ledger.db.execute('SELECT * FROM uses WHERE id=?', (body['read_use_id'],)).fetchone()
    loaded = ledger.db.execute('SELECT * FROM use_sources WHERE id=?', (body['read_use_id'],)).fetchone()
    if (not row or not loaded or row['task'] != 'agi' or row['session'] != body['owner']
            or row['id'] != digest(['agi', row['session'], row['turn']])
            or row['version'] != body['used_version'] or row['spec_sha256'] != body['read_spec_sha256']
            or digest(json.loads(loaded['spec'])) != row['spec_sha256']
            or digest(json.loads(loaded['sources'])) != body['read_sources_sha256']):
        raise ValueError('actual_schedule_read_required')
    historical = ledger.db.execute('SELECT spec FROM task_versions WHERE task=? AND version=?', ('agi', row['version'])).fetchone()
    if not historical or digest(json.loads(historical['spec'])) != row['spec_sha256']:
        raise ValueError('schedule_read_history_changed')
    if current_version:
        current = ledger.db.execute('SELECT version,spec FROM tasks WHERE id=?', ('agi',)).fetchone()
        if not current or current['version'] != row['version'] or digest(json.loads(current['spec'])) != row['spec_sha256']:
            raise ValueError('schedule_read_version_changed')
        sources = json.loads(loaded['sources'])
        if ledger.get('validated_sources:agi', {}).get('sources') != sources:
            raise ValueError('schedule_read_current_sources_changed')
        latest = ledger.get('latest_source:agi')
        if latest is not None:
            paths = [latest['source']] + ([latest['archive_source']] if latest['archive_source'] else [])
            if paths != [ref['path'] for ref in sources]:
                raise ValueError('schedule_read_latest_source_changed')
        for ref in sources:
            if checked_source('agi', ref['path'])[1] != ref:
                raise ValueError('schedule_read_source_changed')


def _schedule_intent(ledger, binding, intent):
    if not isinstance(intent, dict) or set(intent) != {'id', 'body'}:
        raise ValueError('schedule_intent_invalid')
    body = intent['body']
    keys = {'plan', 'task', 'owner', 'issue_id', 'at', 'prepared_at', 'used_version', 'read_use_id',
            'read_spec_sha256', 'read_sources_sha256', 'authorization_sha256', 'binding_sha256',
            'old_scheduler', 'new_scheduler'}
    if (not isinstance(body, dict) or set(body) != keys or intent['id'] != digest(body)
            or body['plan'] != SCHEDULE_PLAN or body['task'] != 'agi'
            or body['owner'] != binding['owner_session'] or body['binding_sha256'] != digest(binding)
            or body['old_scheduler'] != binding['scheduler'] or type(body['used_version']) is not int
            or type(body['prepared_at']) not in (int, float) or not 0 < body['prepared_at'] <= time.time() + 30):
        raise ValueError('schedule_intent_binding_invalid')
    authorization, authorization_sha = read_json(ledger.root / 'authorization.json')
    if (authorization_sha != body['authorization_sha256'] or authorization.get('policy') != POLICY
            or authorization.get('tasks', {}).get('agi') != binding):
        raise PermissionError('schedule_original_authorization_changed')
    expected = {**binding['scheduler'], 'rrule': _schedule_rrule(body['at'], body['prepared_at'])}
    if expected != body['new_scheduler'] or expected == body['old_scheduler']:
        raise PermissionError('only_observable_bounded_schedule_change_allowed')
    old = body['old_scheduler']
    counts = [x for x in str(old.get('rrule', '')).split(';') if x.startswith('COUNT=')]
    if (old.get('kind') != 'heartbeat' or old.get('status') != 'ACTIVE'
            or old.get('target_thread_id') != body['owner'] or counts != ['COUNT=1']):
        raise PermissionError('original_single_active_recheck_required')
    _schedule_read(ledger, body, current_version=False)
    return body


def _schedule_commit(ledger, binding, intent, commit):
    body = _schedule_intent(ledger, binding, intent)
    if (not isinstance(commit, dict) or set(commit) != {'intent_id', 'committed_at', 'committed_by', 'scheduler_sha256'}
            or commit['intent_id'] != intent['id'] or commit['scheduler_sha256'] != digest(body['new_scheduler'])
            or type(commit['committed_at']) not in (int, float)
            or not body['prepared_at'] <= commit['committed_at'] < body['at']
            or commit['committed_at'] > time.time() + 30
            or not isinstance(commit['committed_by'], dict) or set(commit['committed_by']) != {'session', 'turn'}
            or commit['committed_by']['session'] != body['owner'] or not commit['committed_by']['turn']):
        raise ValueError('schedule_commit_invalid')
    return body


def effective_scheduler(ledger, task, binding):
    """Derive one exact exception, without rewriting the original authorization."""
    if task != 'agi': return binding['scheduler']
    intent_key, commit_key = _schedule_keys()
    intent, commit = ledger.get(intent_key), ledger.get(commit_key)
    if intent is None and commit is None: return binding['scheduler']
    body = _schedule_intent(ledger, binding, intent)
    _schedule_case(binding, body['issue_id'], completed=True)
    if commit is None: return binding['scheduler']
    _schedule_commit(ledger, binding, intent, commit)
    return body['new_scheduler']


def _append_schedule_record(ledger, key, value):
    if ledger.db.execute('SELECT 1 FROM meta WHERE key=?', (key,)).fetchone():
        raise ValueError('schedule_record_already_exists')
    ledger.db.execute('INSERT INTO meta VALUES(?,?)', (key, packed(value)))


def _schedule_owner(store, task):
    if task != 'agi': raise PermissionError('bounded_schedule_task_not_authorized')
    root, conf, authorization_sha256 = authorized(store, with_fingerprint=True)
    context = native_context(task, conf)
    binding = conf['tasks'][task]
    if context.get('platform') != 'codex' or context.get('session') != binding['owner_session']:
        raise PermissionError('original_native_owner_required')
    return root, conf, context, authorization_sha256


def _schedule_authority_current(store, root, conf_sha256, authorization_sha256):
    current_root, current_conf, current_sha256 = authorized(store, with_fingerprint=True)
    if current_root != root or digest(current_conf) != conf_sha256:
        raise PermissionError('schedule_authority_changed_before_append')
    if current_sha256 != authorization_sha256:
        raise PermissionError('schedule_original_authorization_changed')


def _schedule_control(store):
    from .control import gate
    return gate(store.root)


def prepare_reschedule(store, task, payload):
    root, conf, context, authorization_sha256 = _schedule_owner(store, task)
    binding = conf['tasks'][task]; conf_sha256 = digest(conf)
    if set(payload) != {'used_version', 'at', 'issue_id'} or type(payload['used_version']) is not int:
        raise ValueError('schedule_prepare_fields')
    with Ledger(root) as ledger, task_lock(ledger, task):
        if ledger.get('enabled') is not True: raise PermissionError('maintenance_paused')
        intent_key, commit_key = _schedule_keys()
        existing, committed = ledger.get(intent_key), ledger.get(commit_key)
        if existing is not None:
            body = _schedule_intent(ledger, binding, existing)
            _schedule_case(binding, body['issue_id'], completed=committed is not None)
            if (payload['at'], payload['issue_id'], payload['used_version']) != (body['at'], body['issue_id'], body['used_version']):
                raise PermissionError('bounded_recheck_already_prepared')
            if committed is not None: _schedule_commit(ledger, binding, existing, committed)
            actual = scheduler_metadata(task)
            if actual not in ([body['new_scheduler']] if committed else [body['old_scheduler'], body['new_scheduler']]):
                raise PermissionError('native_schedule_readback_mismatch')
            return {'intent_id': existing['id'], 'idempotent': True,
                    'state': 'committed' if committed else 'prepared', 'reconcile_original_native_update_only': True,
                    'rrule': body['new_scheduler']['rrule'], 'expected_at': body['at'],
                    'scheduler_modified': False, 'native_trigger_verified': False}
        if committed is not None: raise ValueError('schedule_intent_missing')
        _schedule_case(binding, payload['issue_id'])
        now = time.time(); rule = _schedule_rrule(payload['at'], now)
        key = digest([task, context['session'], context['turn']])
        use = ledger.db.execute('SELECT * FROM uses WHERE id=?', (key,)).fetchone()
        loaded = ledger.db.execute('SELECT * FROM use_sources WHERE id=?', (key,)).fetchone()
        if not use or not loaded: raise ValueError('actual_schedule_read_required')
        if use['version'] != payload['used_version']: raise ValueError('actual_loaded_version_required')
        if json.loads(loaded['spec'])['stages']['natural_effect'] is not False:
            raise PermissionError('natural_effect_already_accepted')
        body = {'plan': SCHEDULE_PLAN, 'task': task, 'owner': context['session'], 'issue_id': payload['issue_id'],
                'at': payload['at'], 'prepared_at': now, 'used_version': payload['used_version'], 'read_use_id': key,
                'read_spec_sha256': use['spec_sha256'], 'read_sources_sha256': digest(json.loads(loaded['sources'])),
                'authorization_sha256': authorization_sha256, 'binding_sha256': digest(binding),
                'old_scheduler': binding['scheduler'], 'new_scheduler': {**binding['scheduler'], 'rrule': rule}}
        intent = {'id': digest(body), 'body': body}
        _schedule_intent(ledger, binding, intent); _schedule_read(ledger, body)
        if scheduler_metadata(task) != body['old_scheduler']: raise PermissionError('original_scheduler_definition_changed')
        with _schedule_control(store), ledger.tx():
            if ledger.get('enabled') is not True: raise PermissionError('maintenance_paused')
            _schedule_case(binding, payload['issue_id']); _schedule_intent(ledger, binding, intent); _schedule_read(ledger, body)
            if scheduler_metadata(task) != body['old_scheduler']: raise PermissionError('scheduler_changed_before_prepare')
            _schedule_authority_current(store, root, conf_sha256, body['authorization_sha256'])
            _schedule_rrule(body['at'], time.time())
            _append_schedule_record(ledger, intent_key, intent)
        return {'intent_id': intent['id'], 'state': 'prepared', 'rrule': rule, 'expected_at': body['at'],
                'native_update_required': True, 'scheduler_modified': False, 'native_trigger_verified': False}


def commit_reschedule(store, task, payload):
    root, conf, context, _ = _schedule_owner(store, task)
    binding = conf['tasks'][task]; conf_sha256 = digest(conf)
    if set(payload) != {'intent_id'}: raise ValueError('schedule_commit_fields')
    with Ledger(root) as ledger, task_lock(ledger, task):
        if ledger.get('enabled') is not True: raise PermissionError('maintenance_paused')
        intent_key, commit_key = _schedule_keys()
        intent, saved = ledger.get(intent_key), ledger.get(commit_key)
        body = _schedule_intent(ledger, binding, intent)
        if payload['intent_id'] != intent['id']: raise ValueError('schedule_intent_id_mismatch')
        _schedule_case(binding, body['issue_id'], completed=saved is not None)
        if scheduler_metadata(task) != body['new_scheduler']: raise PermissionError('native_schedule_readback_mismatch')
        if saved is not None:
            _schedule_commit(ledger, binding, intent, saved)
            return {'committed': True, 'idempotent': True, 'receipt': saved,
                    'scheduler_modified': False, 'native_trigger_verified': False}
        _schedule_read(ledger, body)
        if body['at'] <= time.time(): raise ValueError('schedule_target_elapsed')
        with _schedule_control(store), ledger.tx():
            if ledger.get('enabled') is not True: raise PermissionError('maintenance_paused')
            if ledger.get(intent_key) != intent: raise ValueError('schedule_intent_changed_before_commit')
            committed_at = time.time()
            if body['at'] <= committed_at: raise ValueError('schedule_target_elapsed')
            receipt = {'intent_id': intent['id'], 'committed_at': committed_at,
                       'committed_by': {key: context[key] for key in ('session', 'turn')},
                       'scheduler_sha256': digest(body['new_scheduler'])}
            _schedule_case(binding, body['issue_id']); _schedule_commit(ledger, binding, intent, receipt); _schedule_read(ledger, body)
            if scheduler_metadata(task) != body['new_scheduler']: raise PermissionError('scheduler_changed_before_commit')
            _schedule_authority_current(store, root, conf_sha256, body['authorization_sha256'])
            if body['at'] <= time.time(): raise ValueError('schedule_target_elapsed')
            _append_schedule_record(ledger, commit_key, receipt)
        return {'committed': True, 'idempotent': False, 'receipt': receipt,
                'scheduler_modified': False, 'native_trigger_verified': False}


def closure_database():
    # ClosureStore derives its own sibling ledger from the activity database.
    return Path(__file__).resolve().parents[2] / 'data/activity.sqlite'


def configure(store, payload, root=None):
    if set(payload) != {'authorization', 'tasks'} or payload['authorization'] != POLICY:
        raise PermissionError('explicit_maintenance_plan_required')
    if set(payload['tasks']) != set(TASKS): raise PermissionError('exact_two_task_bindings_required')
    bindings = {}
    for task, binding in payload['tasks'].items():
        if set(binding) != {'owner_session', 'source', 'archive_source', 'closure_tasks'}:
            raise ValueError('task_binding_fields')
        metadata = scheduler_metadata(task)
        if metadata.get('kind') != 'heartbeat' or metadata.get('status') != 'ACTIVE' or metadata.get('target_thread_id') != binding['owner_session']:
            raise PermissionError('native_task_owner_or_state_changed')
        if not binding['closure_tasks'] or not all(isinstance(x, str) and len(x) == 32 for x in binding['closure_tasks']):
            raise ValueError('original_closure_binding_required')
        from ..task_closure import Store as ClosureStore
        with ClosureStore(closure_database(), readonly=True) as closure:
            for identifier in binding['closure_tasks']:
                task_row = closure.conn.execute('SELECT platform,session FROM closure_tasks WHERE id=?', (identifier,)).fetchone()
                if not task_row or task_row['platform'] != 'codex' or task_row['session'] != binding['owner_session']:
                    raise PermissionError('original_closure_owner_mismatch')
        checked_source(task, binding['source'])
        if binding['archive_source']: checked_source(task, binding['archive_source'])
        bindings[task] = {**binding, 'scheduler': metadata}
    authorization = {'policy': POLICY, 'tasks': bindings}
    with Ledger(root) as ledger:
        path = ledger.root / 'authorization.json'
        if path.exists():
            old, _ = read_json(path)
            if old != authorization: raise PermissionError('authorization_already_bound')
        else: atomic(path, packed(authorization))
        _, sha = read_json(path)
        ledger.set('enabled', True)
        if ledger.get('repair_observe_since') is None: ledger.set('repair_observe_since', time.time())
        for task, binding in bindings.items():
            observed = checked_source(task, binding['source'])[0].get('at')
            if type(observed) not in (int, float): raise ValueError('source_observation_time_required')
            if ledger.get('latest_source_observed:' + task) is None: ledger.set('latest_source_observed:' + task, observed)
    store.set('evolution_maintenance', {'enabled': True, 'policy': POLICY, 'authorization_sha256': sha,
                                      'root': str(Path(root or default_root()).absolute()), 'authorized_at': time.time()})
    return {'configured': True, 'task_count': 2, 'scheduler_changed': False}


def authorized(store, *, with_fingerprint=False):
    from . import autonomy
    from .acceptance import release_ready
    cap = store.get('evolution_maintenance', {})
    if cap.get('enabled') is not True or cap.get('policy') != POLICY or not autonomy.active(store) or not release_ready(store):
        raise PermissionError('maintenance_not_active_or_release_unverified')
    root = Path(cap['root'])
    conf, sha = read_json(root / 'authorization.json')
    if sha != cap.get('authorization_sha256') or conf.get('policy') != POLICY or set(conf.get('tasks', {})) != set(TASKS):
        raise PermissionError('maintenance_authorization_changed')
    return (root, conf, sha) if with_fingerprint else (root, conf)


def _bool(value, key):
    if type(value.get(key)) is not bool: raise ValueError('required_stage_evidence_missing')
    return value[key]


def normalize(task, receipt, archive=None):
    if task == 'agi':
        return {'engineering': _bool(receipt, 'engineering_and_installation_complete'),
                'natural_effect': _bool(receipt, 'natural_effectiveness_accepted'),
                'archive': _bool(archive, 'all_ten_current_formally_archived') if archive else _bool(receipt, 'external_archive_complete')}
    count = receipt.get('verified_help_cases')
    remaining = receipt.get('remaining')
    lower = receipt.get('original_earliest_time_lower_bound')
    if type(count) is not int or count < 0 or not isinstance(remaining, list) or not isinstance(lower, str):
        raise ValueError('required_natural_evidence_missing')
    dt.date.fromisoformat(lower)
    if lower != METACOGNITION_EARLIEST: raise PermissionError('original_earliest_date_changed')
    archive_ok = False
    if archive is not None:
        packages = archive.get('packages')
        if not isinstance(packages, list) or not packages: raise ValueError('archive_packages_missing')
        archive_ok = all(p.get('content_attributes_structure_readback') is True and
                         p.get('matches_original_package_subtree') is True for p in packages)
    return {'engineering': _bool(receipt, 'source_app_live_pair') and _bool(receipt, 'controller_ready'),
            'whole_complete': _bool(receipt, 'whole_plan_complete'), 'help_cases': count,
            'remaining': len(remaining), 'earliest': lower,
            'archive': archive_ok}


def steps(task, stages):
    if task == 'agi':
        values = []
        if not stages['engineering']: values.append('先核对源码、正式安装与原固定控制器验收。')
        if not stages['natural_effect']: values.append('按原观察窗口和指标核对文件追踪自然效果，缺证保留待验。')
        if not stages['archive']: values.append('按原登记核对恢复材料归档，保留当前数据。')
        return values or ['复核全部原事项的独立验收与取消边界，再判断是否退休调度。']
    values = []
    if not stages['engineering']: values.append('先核对正式安装与固定控制器配套。')
    if not stages['archive']: values.append('核对登记的恢复归档独立读回，退休登记本身不证明归档。')
    if stages['remaining']: values.append('继续原任务未完成的来源覆盖、真实反馈、通知显示与判断质量验收。')
    if not stages['whole_complete'] or stages['help_cases'] < 3:
        values.append('保持原至少七天、三次独立自然结果标准；日期到达或无假设不等于验收。')
    return values or ['复核原主任务全部独立验收，再判断是否退休调度。']


def stage_text(task, value):
    if not value: return '尚未接入自动维护'
    yes = lambda v: '已验' if v else '待验'
    if task == 'agi': return f"工程安装{yes(value['engineering'])}；自然效果{yes(value['natural_effect'])}；恢复归档{yes(value['archive'])}"
    return f"工程配套{yes(value['engineering'])}；自然帮助案例{value['help_cases']}；未完事项{value['remaining']}；整体{'已验' if value['whole_complete'] else '待验'}"


@contextlib.contextmanager
def task_lock(ledger, task):
    if task not in TASKS: raise PermissionError('task_not_authorized')
    fd = os.open(ledger.root / (task + '.lock'), os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally: os.close(fd)


def refresh(ledger, task, binding):
    with task_lock(ledger, task): return _refresh(ledger, task, binding)


def _refresh(ledger, task, binding):
    if scheduler_metadata(task) != effective_scheduler(ledger, task, binding): raise PermissionError('original_scheduler_definition_changed')
    latest = ledger.get('latest_source:' + task, {})
    source = latest.get('source', binding['source']); archive_path = latest.get('archive_source', binding['archive_source'])
    receipt, ref = checked_source(task, source)
    archive, archive_ref = checked_source(task, archive_path) if archive_path else (None, None)
    stages = normalize(task, receipt, archive)
    refs = [ref] + ([archive_ref] if archive_ref else [])
    row = ledger.db.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone()
    old = json.loads(row['spec']) if row else None
    detail = receipt.get('remaining', []) if task == 'automation-9' else []
    semantic = digest([stages, detail])
    changed = old is None or old['semantic'] != semantic
    version = (row['version'] + 1 if changed else row['version']) if row else 1
    spec = {'policy': POLICY, 'task': task, 'name': TASKS[task], 'version': version, 'semantic': semantic,
            'owner_session': binding['owner_session'], 'stages': stages, 'sources': refs, 'next_steps': steps(task, stages),
            'observed_at': receipt.get('at', receipt.get('observed_at')), 'updated_at': time.time(),
            'boundary': '原目标、时间、模型、隐私、预算、验收标准、暂停取消和人工选择保持。'}
    if not changed: spec = old
    # Recheck all objects immediately before committing; no partial new version.
    for item in refs:
        if checked_source(task, item['path'])[1] != item: raise ValueError('source_changed_before_commit')
    with ledger.tx():
        current = ledger.db.execute('SELECT version,spec FROM tasks WHERE id=?', (task,)).fetchone()
        if (current is None) != (row is None) or (current is not None and (current['version'], current['spec']) != (row['version'], row['spec'])):
            raise ValueError('task_progress_changed_before_commit')
        if ledger.get('latest_source:' + task, {}) != latest: raise ValueError('task_source_pointer_changed')
        ledger.db.execute('INSERT OR REPLACE INTO tasks VALUES(?,?,?)', (task, version, packed(spec)))
        saved_version = ledger.db.execute('SELECT spec FROM task_versions WHERE task=? AND version=?', (task, version)).fetchone()
        if saved_version and saved_version['spec'] != packed(spec): raise ValueError('task_version_content_conflict')
        ledger.db.execute('INSERT OR IGNORE INTO task_versions VALUES(?,?,?)', (task, version, packed(spec)))
        previous_error = ledger.get('source_error:' + task)
        ledger.set('source_error:' + task, None)
        observed = receipt.get('at')
        if type(observed) not in (int, float) or not 0 < observed <= time.time() + 30:
            raise ValueError('source_observation_time_required')
        high_water = max(ledger.get('source_observation_high_water:' + task, 0), ledger.get('latest_source_observed:' + task, 0))
        if observed < high_water: raise ValueError('older_progress_source_rejected')
        ledger.set('source_observation_high_water:' + task, observed)
        ledger.set('validated_sources:' + task, {'sources': refs, 'at': time.time(), 'observed_at': observed})
        if previous_error:
            ledger.event({'kind': 'source_recovered', 'task': task}, digest(['source-recovered', task, previous_error, ledger.get('source_error_episode:' + task)]))
        if changed:
            ledger.event({'kind': 'task_updated', 'task': task, 'before': old['stages'] if old else None,
                          'after': stages, 'initial': old is None, 'version': version,
                          'source_versions': [r['sha256'] for r in refs]},
                         digest(['task-version', task, version, semantic]))
    directory = private_directory(ledger.root / 'tasks')
    atomic(directory / (task + '.json'), packed(spec))
    return {'task': task, 'version': version, 'changed': changed}


def collect_repairs(store, ledger):
    """Observe only post-authorization AGI job transitions; never export model prose."""
    since = ledger.get('repair_observe_since', time.time())
    states = {'building', 'prepared', 'blocked', 'deploying', 'observing', 'unknown', 'failed', 'rolled_back', 'verified', 'decision'}
    from .evolution_notices import FILE_TAGS
    for row in store.rows('SELECT j.id,j.state,j.revision,j.updated,j.receipt,i.state AS incident_state FROM jobs j JOIN incidents i ON i.id=j.incident_id WHERE i.project=? AND (j.updated>=? OR i.updated>=?)', ('agi', since, since)):
        state = row['incident_state'] if row['incident_state'] in {'verified', 'decision'} else row['state']
        if state not in states: continue
        old = ledger.get('repair_state:' + row['id'])
        current = [state, row['revision']]
        if old == current: continue
        try: receipt = json.loads(row['receipt'])
        except (ValueError, TypeError): receipt = {}
        rounds = receipt.get('rounds', []) if isinstance(receipt, dict) else []
        if not isinstance(rounds, list): rounds = []
        changed = sorted({FILE_TAGS[p] for r in rounds if isinstance(r, dict) and isinstance(r.get('changed', []), list)
                          for p in r.get('changed', []) if isinstance(p, str) and p in FILE_TAGS})
        reproduced = bool(rounds) and any(r.get('old_failed') is True for r in rounds if isinstance(r, dict))
        candidate_passed = bool(rounds) and isinstance(rounds[-1], dict) and rounds[-1].get('candidate_passed') is True
        ledger.event({'kind': 'repair_state', 'state': state, 'before_state': old[0] if old else None,
                      'changed': changed, 'old_failed': reproduced, 'candidate_passed': candidate_passed},
                     digest(['repair', row['id'], current]))
        ledger.set('repair_state:' + row['id'], current)


def native_context(task, conf):
    from ..task_closure import Store as ClosureStore
    from ..attention_native import DesktopIPC, NativeError, thread_id
    from .projects import PROJECTS
    with ClosureStore(closure_database(), readonly=True) as closure:
        context = closure.recovery_caller()
    if not context or context.get('platform') != 'codex' or context['session'] != conf['tasks'][task]['owner_session']:
        raise PermissionError('original_native_owner_required')
    # Heartbeats do not refresh UserPromptSubmit's root. Keep that receipt as
    # owner provenance, but obtain the executing turn from the live desktop.
    try:
        with DesktopIPC() as ipc:
            snapshot = ipc.snapshot(context['session'], expected_cwd=PROJECTS['agi']['repository'], current_turn=True)
        turn = thread_id(snapshot.get('turn_id'))
    except (NativeError, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise PermissionError('current_native_turn_unavailable') from exc
    observed = snapshot.get('observed_at')
    if (snapshot.get('source') != 'desktop_owner' or snapshot.get('current_turn_matches') is not True
            or snapshot.get('activity') != 'active' or snapshot.get('turn_status') != 'inProgress'
            or type(observed) not in (int, float) or not 0 <= time.time() - observed <= 30):
        raise PermissionError('current_native_turn_unavailable')
    return {'platform': 'codex', 'session': context['session'], 'turn': turn,
            'hook_turn': context.get('turn'), 'hook_task_id': context.get('task_id'),
            'turn_source': 'desktop_owner'}


def load_task(store, task, root=None):
    actual, conf = authorized(store)
    if root is not None and Path(root) != actual: raise PermissionError('maintenance_root_mismatch')
    if task not in conf['tasks']: raise PermissionError('task_not_authorized')
    with Ledger(actual) as ledger:
        if ledger.get('enabled') is not True: raise PermissionError('maintenance_paused')
        with task_lock(ledger, task): return _load_task_locked(ledger, task, conf)


def _load_task_locked(ledger, task, conf):
        _refresh(ledger, task, conf['tasks'][task])
        row = ledger.db.execute('SELECT spec FROM tasks WHERE id=?', (task,)).fetchone()
        spec = json.loads(row[0]); refs = ledger.get('validated_sources:' + task)['sources']
        result = {'spec': spec, 'current_sources': refs, 'native_use_recorded': False,
                  'run_receipt_contract': '本次进度写入登记报告目录内的新文件；成功 record-run 前不要覆盖上一轮已登记来源。采用本次实际读取版本，来源变化或并发拒绝时保留证据，下一轮重新核查；不得降低原条件。'}
        if task == 'agi' and ledger.get(_schedule_keys()[0]) is not None:
            result['current_schedule'] = effective_scheduler(ledger, task, conf['tasks'][task])
            result['schedule_amendment'] = {'plan': SCHEDULE_PLAN, 'intent': ledger.get(_schedule_keys()[0]),
                                             'commit': ledger.get(_schedule_keys()[1]), 'native_trigger_verified': False}
        try: context = native_context(task, conf)
        except (PermissionError, OSError, sqlite3.Error): return result
        key = digest([task, context['session'], context['turn']])
        with ledger.tx():
            ledger.db.execute('INSERT OR IGNORE INTO uses VALUES(?,?,?,?,?,?,?,NULL)',
                              (key, task, context['session'], context['turn'], spec['version'], digest(spec), time.time()))
            saved = ledger.db.execute('SELECT * FROM uses WHERE id=?', (key,)).fetchone()
            if saved['version'] != spec['version'] or saved['spec_sha256'] != digest(spec): raise ValueError('native_turn_already_loaded_other_version')
            saved_sources = ledger.db.execute('SELECT sources,spec FROM use_sources WHERE id=?', (key,)).fetchone()
            if saved_sources and (saved_sources['sources'] != packed(refs) or saved_sources['spec'] != packed(spec)):
                raise ValueError('native_turn_already_loaded_other_sources')
            ledger.db.execute('INSERT OR IGNORE INTO use_sources VALUES(?,?,?)', (key, packed(refs), packed(spec)))
        return {**result, 'native_use_recorded': True, 'use_id': key, 'turn': context['turn']}


def record_run(store, task, payload):
    root, conf = authorized(store)
    if task not in conf['tasks']: raise PermissionError('task_not_authorized')
    context = native_context(task, conf)
    if set(payload) != {'used_version', 'source', 'archive_source', 'outcome'} or payload['outcome'] not in {'partial', 'complete', 'blocked', 'failed', 'awaiting_user'}:
        raise ValueError('run_receipt_fields')
    source, ref = checked_source(task, payload['source'])
    archive, archive_ref = checked_source(task, payload['archive_source']) if payload['archive_source'] else (None, None)
    stages = normalize(task, source, archive)
    with Ledger(root) as ledger:
        if ledger.get('enabled') is not True: raise PermissionError('maintenance_paused')
        if scheduler_metadata(task) != effective_scheduler(ledger, task, conf['tasks'][task]):
            raise PermissionError('original_scheduler_definition_changed')
        with task_lock(ledger, task): return _record_run_locked(ledger, conf, task, context, payload, stages, ref, archive_ref)


def _record_run_locked(ledger, conf, task, context, payload, stages, ref, archive_ref):
        key = digest([task, context['session'], context['turn']])
        use = ledger.db.execute('SELECT * FROM uses WHERE id=?', (key,)).fetchone()
        loaded = ledger.db.execute('SELECT * FROM use_sources WHERE id=?', (key,)).fetchone()
        if not use or not loaded or digest(json.loads(loaded['spec'])) != use['spec_sha256'] or type(payload['used_version']) is not int or use['version'] != payload['used_version']:
            raise ValueError('actual_loaded_version_required')
        value = {**payload, 'context': context, 'stages': stages, 'loaded_spec_sha256': use['spec_sha256'],
                 'loaded_sources': json.loads(loaded['sources']), 'sources': [ref] + ([archive_ref] if archive_ref else []), 'at': time.time()}
        if use['result']:
            previous = json.loads(use['result'])
            if {k:v for k,v in previous.items() if k!='at'} != {k:v for k,v in value.items() if k!='at'}:
                raise ValueError('native_run_receipt_conflict')
            return {'recorded': True, 'idempotent': True, 'retirement_eligible': retirement_eligible(conf['tasks'][task], task, stages)}
        for item in value['sources']:
            if checked_source(task, item['path'])[1] != item: raise ValueError('source_changed_before_run_commit')
        with ledger.tx():
            current = ledger.db.execute('SELECT result FROM uses WHERE id=?', (key,)).fetchone()
            if not current or current['result'] != use['result']: raise ValueError('native_run_result_changed_before_commit')
            spec_row = ledger.db.execute('SELECT version FROM tasks WHERE id=?', (task,)).fetchone()
            if spec_row and spec_row['version'] != use['version']: raise ValueError('current_task_version_changed_recheck_required')
            newer = ledger.db.execute('SELECT 1 FROM uses WHERE task=? AND created>? AND result IS NOT NULL', (task, use['created'])).fetchone()
            if newer: raise ValueError('newer_native_progress_already_recorded')
            validated = ledger.get('validated_sources:' + task, {})
            previous_at = max(ledger.get('source_observation_high_water:' + task, 0), ledger.get('latest_source_observed:' + task, 0), validated.get('observed_at', 0))
            observed = checked_source(task, payload['source'])[0].get('at')
            if type(observed) not in (int, float) or isinstance(observed, bool): raise ValueError('source_observation_time_required')
            if not 0 < observed <= time.time() + 30: raise ValueError('source_observation_time_invalid')
            if previous_at is not None and observed < previous_at: raise ValueError('older_progress_source_rejected')
            for item in value['sources']:
                if checked_source(task, item['path'])[1] != item: raise ValueError('source_changed_in_run_transaction')
            if ledger.db.execute('UPDATE uses SET result=? WHERE id=? AND result IS NULL', (packed(value), key)).rowcount != 1:
                raise ValueError('native_run_receipt_compare_and_set_failed')
            ledger.set('latest_source:' + task, {'source': payload['source'], 'archive_source': payload['archive_source']})
            ledger.set('latest_source_observed:' + task, observed)
            ledger.set('source_observation_high_water:' + task, observed)
            ledger.set('last_adopted_run:' + task, key)
            notice_binding = digest([use['version'], stages, payload['outcome']])
            if ledger.get('last_run_notice:' + task) != notice_binding:
                ledger.event({'kind': 'task_run', 'task': task, 'after': stages, 'outcome': payload['outcome'],
                              'version': use['version'], 'source_versions': [r['sha256'] for r in value['sources']]},
                             digest(['native-run', key]))
                ledger.set('last_run_notice:' + task, notice_binding)
        return {'recorded': True, 'retirement_eligible': retirement_eligible(conf['tasks'][task], task, stages)}


def retirement_eligible(binding, task, stages):
    if task != 'agi' and stages.get('earliest') != METACOGNITION_EARLIEST: return False
    enough = all(stages.values()) if task == 'agi' else (stages['engineering'] and stages['archive'] and stages['whole_complete'] and stages['help_cases'] >= 3 and stages['remaining'] == 0 and dt.datetime.now(ZONE).date() >= dt.date.fromisoformat(stages['earliest']))
    if not enough: return False
    from ..task_closure import Store as ClosureStore
    try:
        with ClosureStore(closure_database(), readonly=True) as closure:
            return all(closure.task_status(key)['all_complete'] is True for key in binding['closure_tasks'])
    except (OSError, ValueError, sqlite3.Error, KeyError): return False


def native_retirement_ready(ledger, task, conf, context):
    if ledger.get('enabled') is not True: return False
    key = digest([task, context['session'], context['turn']])
    use = ledger.db.execute('SELECT result FROM uses WHERE id=?', (key,)).fetchone()
    if not use or not use['result'] or ledger.get('last_adopted_run:' + task) != key: return False
    result = json.loads(use['result'])
    if result['outcome'] != 'complete': return False
    latest = ledger.get('latest_source:' + task)
    if latest != {'source': result['source'], 'archive_source': result['archive_source']}: return False
    for ref in result['sources']:
        if checked_source(task, ref['path'])[1] != ref: return False
    receipt, _ = checked_source(task, result['source'])
    archive, _ = checked_source(task, result['archive_source']) if result['archive_source'] else (None, None)
    return retirement_eligible(conf['tasks'][task], task, normalize(task, receipt, archive))


def record_retirement(store, task):
    root, conf = authorized(store)
    if task not in conf['tasks']: raise PermissionError('task_not_authorized')
    context = native_context(task, conf)
    with Ledger(root) as ledger:
        if ledger.get('enabled') is not True: raise PermissionError('maintenance_paused')
        key = digest([task, context['session'], context['turn']])
        use = ledger.db.execute('SELECT result FROM uses WHERE id=?', (key,)).fetchone()
        if not use or not use['result']: raise ValueError('actual_native_result_required')
        result = json.loads(use['result'])
        if result['outcome'] != 'complete': raise ValueError('completion_result_required')
        receipt, _ = checked_source(task, result['source'])
        archive, _ = checked_source(task, result['archive_source']) if result['archive_source'] else (None, None)
        if not native_retirement_ready(ledger, task, conf, context):
            raise PermissionError('original_acceptance_not_complete')
        scheduler = Path.home() / '.codex/automations' / task / 'automation.toml'
        if scheduler.exists() or scheduler.is_symlink(): raise ValueError('native_scheduler_still_registered')
        with ledger.tx():
            ledger.set('retired:' + task, {'at': time.time(), 'use': key})
            ledger.event({'kind': 'task_retired', 'task': task}, digest(['retired', task]))
        return {'retired': True, 'scheduler_modified_by_adapter': False}


def tick(store):
    cap = store.get('evolution_maintenance', {})
    if cap.get('enabled') is not True: return {'status': 'not_configured'}
    root, conf = authorized(store)
    with Ledger(root) as ledger:
        if ledger.get('enabled') is not True: return {'status': 'paused'}
        results = []
        for task, binding in conf['tasks'].items():
            if ledger.get('retired:' + task):
                results.append({'task': task, 'status': 'retired'}); continue
            try: results.append(refresh(ledger, task, binding))
            except BlockingIOError:
                results.append({'task': task, 'status': 'busy'}); continue
            except (OSError, ValueError, PermissionError, sqlite3.Error) as exc:
                error = type(exc).__name__ + ':' + (str(exc) if isinstance(exc, (ValueError, PermissionError)) else 'source_unavailable')
                if ledger.get('source_error:' + task) != error:
                    ledger.set('source_error:' + task, error)
                    episode = time.time(); ledger.set('source_error_episode:' + task, episode)
                    ledger.event({'kind': 'source_error', 'task': task},
                                 digest(['source-error', task, error, episode]))
                results.append({'task': task, 'status': 'blocked', 'reason': error})
        from .evolution_notices import deliver_pending, collect_functional
        collect_repairs(store, ledger)
        collect_functional(store, ledger)
        try: delivery = deliver_pending(ledger)
        except (OSError, ValueError, PermissionError, RuntimeError) as exc:
            delivery = {'status': 'blocked', 'reason': type(exc).__name__}
        ledger.set('last_tick', {'at': time.time(), 'tasks': results, 'delivery': delivery})
        return {'status': 'ok', 'tasks': results, 'delivery': delivery}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('configure', 'read', 'record-run', 'record-retirement', 'status', 'pause', 'resume', 'retirement-check', 'prepare-reschedule', 'commit-reschedule'))
    parser.add_argument('--task', choices=tuple(TASKS)); parser.add_argument('--input', type=Path)
    args = parser.parse_args()
    from .store import Store
    if args.action == 'status':
        with Ledger(readonly=True) as ledger:
            return {'enabled': ledger.get('enabled', False), 'last_tick': ledger.get('last_tick'),
                    'tasks': [dict(r) for r in ledger.db.execute('SELECT id,version FROM tasks')],
                    'events': [dict(r) for r in ledger.db.execute('SELECT state,count(*) count FROM events GROUP BY state')],
                    'native_runs': ledger.db.execute('SELECT count(*) FROM uses WHERE result IS NOT NULL').fetchone()[0]}
    payload = read_json(args.input)[0] if args.input else {}
    with Store() as store:
        if args.action == 'configure': return configure(store, payload)
        if args.action == 'read': return load_task(store, args.task)
        if args.action == 'record-run': return record_run(store, args.task, payload)
        if args.action == 'prepare-reschedule': return prepare_reschedule(store, args.task, payload)
        if args.action == 'commit-reschedule': return commit_reschedule(store, args.task, payload)
        if args.action == 'record-retirement': return record_retirement(store, args.task)
        root, conf = authorized(store)
        if args.action == 'retirement-check':
            context = native_context(args.task, conf)
            with Ledger(root) as ledger:
                refresh(ledger, args.task, conf['tasks'][args.task])
                return {'eligible': native_retirement_ready(ledger, args.task, conf, context), 'scheduler_modified': False}
        with Ledger(root) as ledger: ledger.set('enabled', args.action == 'resume')
        return {'status': 'resumed' if args.action == 'resume' else 'paused'}


if __name__ == '__main__':
    try: print(packed(main()))
    except Exception as exc:
        print(packed({'status': 'rejected', 'reason': str(exc) if isinstance(exc, (ValueError, PermissionError)) else type(exc).__name__})); raise SystemExit(1)
