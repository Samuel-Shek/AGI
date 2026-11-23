"""Owner-authorized, fixed task maintenance; never edits a scheduler or grants.

The scheduler retains its original owner, model and acceptance rules. The one
owner-authorized 20261016 file recheck may amend only its COUNT=1 time binding.
The separately source-bound 20261027 continuation is append-only and still
requires the original owner to update the same native scheduler externally.
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
import re
import shlex
import sqlite3
import stat
import sys
import time
import tomllib
import uuid

from .store import atomic, digest, packed, private_directory

POLICY = 'agi-task-maintenance-20261008-v1'
SCHEDULE_PLAN = 'agi-core-value-20261016'
CONTINUATION_PLAN = 'agi-natural-continuation-20261027'
CONTINUATION_RRULE = 'FREQ=DAILY;BYHOUR=20;BYMINUTE=30;BYSECOND=0'
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


def _original_effective_scheduler(ledger, task, binding):
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


def effective_scheduler(ledger, task, binding):
    previous = _original_effective_scheduler(ledger, task, binding)
    if task != 'agi': return previous
    intent_key, commit_key = _continuation_keys()
    intent, commit = ledger.get(intent_key), ledger.get(commit_key)
    if intent is None and commit is None: return previous
    body = _continuation_intent(ledger, binding, intent)
    _schedule_case(binding, body['issue_id'], completed=True)
    if commit is None: return previous
    _continuation_commit(ledger, body, intent, commit)
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


def _continuation_keys():
    return ('schedule_intent:' + CONTINUATION_PLAN, 'schedule_commit:' + CONTINUATION_PLAN)


def _continuation_source(identifier):
    """Native hook metadata is a locator only, never proof of a human grant."""
    from ..task_closure import Store as ClosureStore, digest as closure_digest
    if not isinstance(identifier, str) or not re.fullmatch(r'[0-9a-f]{32}', identifier):
        raise PermissionError('continuation_native_source_identity')
    with ClosureStore(closure_database(), readonly=True) as closure:
        if not closure.has_table('closure_request_sources'):
            raise PermissionError('continuation_native_source_missing')
        row = closure.conn.execute('SELECT * FROM closure_request_sources WHERE task_id=?',
                                   (identifier,)).fetchone()
        task = closure.conn.execute('SELECT platform,session,turn FROM closure_tasks WHERE id=?',
                                    (identifier,)).fetchone()
    native_id = r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
    if (not row or not task or row['platform'] != 'codex'
            or not isinstance(row['session'], str) or not isinstance(row['turn'], str)
            or not re.fullmatch(native_id, row['session']) or not re.fullmatch(native_id, row['turn'])
            or identifier != closure_digest([row['platform'], row['session'], row['turn']])[:32]
            or any(task[key] != row[key] for key in ('platform', 'session', 'turn'))
            or not isinstance(row['prompt_hash'], str) or not re.fullmatch(r'[0-9a-f]{64}', row['prompt_hash'])
            or row['policy'] != 2 or type(row['at']) not in (int, float)
            or not 0 < row['at'] <= time.time() + 30):
        raise PermissionError('continuation_native_source_mismatch')
    return {**{key: row[key] for key in ('task_id', 'platform', 'session', 'turn', 'prompt_hash', 'policy')},
            'registered_at': row['at']}


@contextlib.contextmanager
def _continuation_native_file(session):
    """Derive the single native path; callers cannot submit a transcript path."""
    root = Path.home() / '.codex/sessions'
    matches = list(root.glob('*/*/*/rollout-*-' + session + '.jsonl'))
    if len(matches) != 1: raise PermissionError('continuation_native_file_ambiguous')
    path = matches[0]
    relative = path.relative_to(root)
    if (len(relative.parts) != 4 or not re.fullmatch(r'\d{4}/\d{2}/\d{2}', '/'.join(relative.parts[:3]))
            or not path.name.endswith('-' + session + '.jsonl')):
        raise PermissionError('continuation_native_path_identity')
    flags = getattr(os, 'O_SEARCH', 0x40000000 if sys.platform == 'darwin' else os.O_RDONLY) | os.O_DIRECTORY
    directory = os.open('/', flags)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, flags | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory); directory = child
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally: os.close(directory)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o022):
            raise PermissionError('continuation_native_file_not_owned')
        yield fd, info
    finally: os.close(fd)


def _continuation_record(raw):
    if not raw.endswith(b'\n') or len(raw) > 512 * 1024:
        raise PermissionError('continuation_native_record_invalid')
    value = json.loads(raw, object_pairs_hook=_pairs)
    if not isinstance(value, dict) or not isinstance(value.get('payload'), dict):
        raise PermissionError('continuation_native_record_invalid')
    return value


def _continuation_request(text):
    # Reuse automation_guard.instructions' data/quotation distinction. Only
    # generic action semantics live here, never a production user's sentence.
    if not isinstance(text, str) or not 0 < len(text) <= 4096 or text.lstrip().startswith(
            ('<hook_prompt', '<codex_delegation', 'Stop hook feedback:', '# AGENTS.md', '<environment_context>')):
        raise PermissionError('continuation_human_request_unverified')
    clean = re.sub(r'```[\s\S]*?(?:```|$)|~~~[\s\S]*?(?:~~~|$)', ' ', text)
    clean = re.sub(r'(?m)^\s*>.*$|`[^`\n]*`|“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|"[^"\n]*"', ' ', clean)
    direct = re.search(r'^\s*(?:请|麻烦|现在|剩下的|接着)*\s*继续(?:跟进|处理|完成)', clean)
    decision = r'(?:必须|需要).{0,8}(?:我|本人).{0,8}(?:决定|选择)'
    quiet = (re.search(r'(?:只在|只有|仅在|仅有).{0,8}' + decision + r'.{0,12}(?:再找我|通知(?:我|本人))', clean)
             or re.search(r'有.{0,4}' + decision + r'.{0,12}再找我', clean))
    if (not direct or not all(word in clean for word in ('原任务', '文件追踪', '自然验收'))
            or not re.search(r'做完|完成', clean)
            or not quiet or re.search(r'测试|模拟|示例|假设|翻译|转述|是否|要不要|方案|分析|讨论|研究|解释|评估|考虑|探讨|[？?]', clean)
            or re.search(r'(?:不要|不必|不用|无需|别|禁止|暂不|勿|不允许|停止|取消|暂停).{0,4}(?:执行|实施|修改|动手|操作|安装|发布|做)', clean)
            or re.search(r'(?:不要|禁止|取消|暂停|停止|撤回).{0,12}(?:文件追踪|自然验收|原任务|跟进|自动)', clean)):
        raise PermissionError('continuation_human_scope_unverified')


def _continuation_user_pair(prior, record, source):
    event, message = record['payload'], prior['payload']
    item = event.get('item', {}); metadata = message.get('internal_chat_message_metadata_passthrough', {})
    content, visible = item.get('content'), message.get('content')
    if (record['type'] != 'event_msg' or event.get('type') != 'item_completed'
            or event.get('thread_id') != source['session'] or event.get('turn_id') != source['turn']
            or item.get('type') != 'UserMessage' or not isinstance(item.get('client_id'), str) or not item['client_id']
            or not isinstance(item.get('id'), str) or not item['id']
            or prior['type'] != 'response_item' or message.get('type') != 'message' or message.get('role') != 'user'
            or metadata.get('turn_id') != source['turn'] or metadata.get('content_item_kinds') != ['user.text']
            or metadata.get('content_item_metadata') != [{'provenance': {'type': 'user'}}]
            or type(metadata.get('create_time')) not in (int, float)
            or not 0 < metadata['create_time'] <= time.time() + 30
            or not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict)
            or content[0].get('type') not in {'text', 'Text'}
            or not isinstance(visible, list) or len(visible) != 1 or not isinstance(visible[0], dict)
            or visible[0].get('type') != 'input_text' or not isinstance(content[0].get('text'), str)
            or visible[0].get('text') != content[0]['text']
            or hashlib.sha256(content[0]['text'].encode()).hexdigest() != source['prompt_hash']):
        raise PermissionError('continuation_real_user_pair_required')
    _continuation_request(content[0]['text'])
    return metadata['create_time']


def _continuation_root_record(record, source):
    event = record['payload']; attribution = event.get('turn_attribution', {})
    if (record['type'] != 'event_msg' or event.get('type') != 'task_started'
            or event.get('turn_id') != source['turn'] or event.get('root_turn_id') != source['turn']
            or attribution.get('turn_id') != source['turn'] or attribution.get('root_turn_id') != source['turn']
            or attribution.get('parent_turn_id', True) is not None
            or attribution.get('initiating_agent_path', True) is not None
            or attribution.get('turn_trigger') != 'composer'):
        raise PermissionError('continuation_primary_human_turn_required')


def _continuation_route_record(record, source, owner):
    event = record['payload']; item = event.get('item', {}); result = item.get('result', {})
    if (record['type'] != 'event_msg' or event.get('type') != 'item_completed'
            or event.get('thread_id') != source['session'] or event.get('turn_id') != source['turn']
            or item.get('type') != 'McpToolCall'
            or item.get('server') != 'codex_app' or item.get('tool') != 'send_message_to_thread'
            or item.get('status') != 'completed' or not isinstance(item.get('arguments'), dict)
            or item['arguments'].get('threadId') != owner or result.get('isError') is not False):
        raise PermissionError('continuation_original_task_route_required')
    content = result.get('content')
    if not isinstance(content, list) or len(content) != 1 or content[0].get('type') != 'text':
        raise PermissionError('continuation_original_task_route_required')
    receipt = json.loads(content[0].get('text', ''), object_pairs_hook=_pairs)
    if not isinstance(receipt, dict) or receipt != {'threadId': owner}:
        raise PermissionError('continuation_original_task_route_required')


def _native_volume_uuid(fd):
    """Read a persistent volume identity from the already safe native fd only."""
    import ctypes
    import struct
    if sys.platform != 'darwin': raise PermissionError('native_volume_uuid_unsupported')
    class Attributes(ctypes.Structure):
        _fields_ = [('bitmapcount', ctypes.c_ushort), ('reserved', ctypes.c_ushort),
                    ('common', ctypes.c_uint32), ('volume', ctypes.c_uint32),
                    ('directory', ctypes.c_uint32), ('file', ctypes.c_uint32), ('fork', ctypes.c_uint32)]
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_nlink != 1:
        raise PermissionError('native_volume_file_not_owned')
    function = ctypes.CDLL(None, use_errno=True).fgetattrlist
    function.argtypes = [ctypes.c_int, ctypes.POINTER(Attributes), ctypes.c_void_p, ctypes.c_size_t, ctypes.c_ulong]
    function.restype = ctypes.c_int
    attributes = Attributes(5, 0, 0, 0x80000000 | 0x00040000, 0, 0, 0)
    buffer = ctypes.create_string_buffer(20)
    if function(fd, ctypes.byref(attributes), buffer, len(buffer), 0):
        raise PermissionError('native_volume_uuid_unavailable')
    raw = buffer.raw
    if struct.unpack_from('=I', raw)[0] != 20 or raw[4:20] == bytes(16):
        raise PermissionError('native_volume_uuid_invalid')
    after = os.fstat(fd)
    if any(getattr(before, key) != getattr(after, key) for key in ('st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_nlink')):
        raise PermissionError('native_volume_file_changed')
    return str(uuid.UUID(bytes=raw[4:20]))


def _continuation_native_proof(source, owner, issue, *, proof=None, fresh=False, identity='device'):
    """Bounded native-pair proof; later reads verify only frozen exact records.

    This uses the existing automation_guard.source_message/native closure format
    principles inside this qualified controller; no external hook is executed.
    No transcript text or whole-file hash is saved. Source IDs alone grant none.
    """
    with _continuation_native_file(source['session']) as (fd, info):
        signature = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]
        if identity not in {'device', 'volume', 'records_only', 'device_drift'}:
            raise ValueError('continuation_native_identity_scheme')
        if fresh and identity not in {'device', 'volume'}: raise ValueError('record_check_cannot_mint_proof')
        volume = _native_volume_uuid(fd) if identity == 'volume' else None
        if fresh:
            head = os.pread(fd, min(info.st_size, 128 * 1024 + 1), 0).partition(b'\n')[0] + b'\n'
            if len(head) > 128 * 1024: raise PermissionError('continuation_native_header_invalid')
            start = max(0, info.st_size - 8 * 1024 * 1024)
            data = os.pread(fd, info.st_size - start, start)
            if not data.endswith(b'\n'): raise PermissionError('continuation_native_source_incomplete')
            if start:
                skipped, _, data = data.partition(b'\n'); start += len(skipped) + 1
            lines = data.splitlines(keepends=True); positions = []; position = start
            for line in lines: positions.append(position); position += len(line)
            users, roots, routes, boundaries, later = [], [], [], [], False
            for index, line in enumerate(lines):
                if b'"UserMessage"' in line:
                    record = _continuation_record(line); event = record['payload']; item = event.get('item', {})
                    if record['type'] != 'event_msg' or item.get('type') != 'UserMessage': continue
                    if event.get('turn_id') == source['turn']:
                        if not index: raise PermissionError('continuation_real_user_pair_required')
                        prior = _continuation_record(lines[index - 1]); _continuation_user_pair(prior, record, source)
                        users.append(index)
                    elif users:
                        content = item.get('content') or []
                        text = content[0].get('text', '') if len(content) == 1 else ''
                        if not text.lstrip().startswith(('# AGENTS.md', '<environment_context>', '<hook_prompt', 'Stop hook feedback:')):
                            later = True
                elif any(kind in line for kind in (b'"task_started"', b'"task_complete"', b'"turn_aborted"')):
                    record = _continuation_record(line)
                    event = record['payload']
                    if record['type'] != 'event_msg': continue
                    if event.get('type') == 'task_started' and event.get('turn_id') == source['turn']:
                        _continuation_root_record(record, source); roots.append(index)
                    elif (event.get('type') == 'task_started'
                          or event.get('type') in {'task_complete', 'turn_aborted'} and event.get('turn_id') == source['turn']):
                        boundaries.append(index)
                elif b'"McpToolCall"' in line and b'"send_message_to_thread"' in line:
                    record = _continuation_record(line)
                    try: _continuation_route_record(record, source, owner)
                    except (PermissionError, ValueError, TypeError, KeyError): continue
                    routes.append(index)
            if len(users) != 1 or len(roots) != 1 or later or roots[0] >= users[0]:
                raise PermissionError('continuation_native_source_missing_or_superseded')
            selected = {'header': (0, head), 'root': (positions[roots[0]], lines[roots[0]]),
                        'prior': (positions[users[0] - 1], lines[users[0] - 1]),
                        'user': (positions[users[0]], lines[users[0]])}
            end = next((index for index in boundaries if index > users[0]), len(lines))
            if end < len(lines): selected['boundary'] = (positions[end], lines[end])
            if source['session'] != owner:
                routes = [index for index in routes if users[0] < index < end]
                if not routes: raise PermissionError('continuation_original_task_route_required')
                selected['route'] = (positions[routes[-1]], lines[routes[-1]])
            created = _continuation_user_pair(_continuation_record(lines[users[0] - 1]),
                                              _continuation_record(lines[users[0]]), source)
            proof = {'session': source['session'], 'turn': source['turn'], 'owner': owner, 'issue_id': issue,
                     'message_created_at': created,
                     'scan_signature': signature, 'records': {name: {'offset': offset, 'length': len(raw),
                     'sha256': hashlib.sha256(raw).hexdigest()} for name, (offset, raw) in selected.items()}}
            if identity == 'volume': proof['volume_uuid'] = volume
        keys = {'session', 'turn', 'owner', 'issue_id', 'message_created_at', 'scan_signature', 'records'}
        if identity == 'volume': keys.add('volume_uuid')
        if (not isinstance(proof, dict) or set(proof) != keys
                or (proof['session'], proof['turn'], proof['owner'], proof['issue_id']) !=
                (source['session'], source['turn'], owner, issue)
                or not isinstance(proof['scan_signature'], list) or len(proof['scan_signature']) != 4
                or (identity == 'device' and proof['scan_signature'][:2] != signature[:2])
                or (identity == 'device_drift' and (proof['scan_signature'][0] == info.st_dev
                    or proof['scan_signature'][1] != info.st_ino))
                or (identity == 'volume' and (proof['scan_signature'][1] != info.st_ino
                    or not isinstance(proof['volume_uuid'], str)
                    or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', proof['volume_uuid'])
                    or proof['volume_uuid'] != volume))
                or set(proof['records']) - {'boundary'} !=
                ({'header', 'root', 'prior', 'user'} | ({'route'} if source['session'] != owner else set()))):
            raise PermissionError('continuation_frozen_native_proof_invalid')
        records = {}
        for name, slot in proof['records'].items():
            if (not isinstance(slot, dict) or set(slot) != {'offset', 'length', 'sha256'}
                    or type(slot['offset']) is not int or type(slot['length']) is not int
                    or slot['offset'] < 0 or not 0 < slot['length'] <= 512 * 1024
                    or slot['offset'] + slot['length'] > info.st_size):
                raise PermissionError('continuation_frozen_native_proof_invalid')
            raw = os.pread(fd, slot['length'], slot['offset'])
            if hashlib.sha256(raw).hexdigest() != slot['sha256']:
                raise PermissionError('continuation_frozen_native_record_changed')
            records[name] = _continuation_record(raw)
        header = records['header']
        if (proof['records']['header']['offset'] != 0 or header['type'] != 'session_meta'
                or header['payload'].get('id') != source['session']
                or header['payload'].get('source') not in {'vscode', 'cli'}):
            raise PermissionError('continuation_native_header_invalid')
        slots = proof['records']
        if (slots['prior']['offset'] + slots['prior']['length'] != slots['user']['offset']
                or slots['root']['offset'] >= slots['prior']['offset']):
            raise PermissionError('continuation_frozen_native_proof_invalid')
        _continuation_root_record(records['root'], source)
        if _continuation_user_pair(records['prior'], records['user'], source) != proof['message_created_at']:
            raise PermissionError('continuation_frozen_native_proof_invalid')
        if 'route' in records:
            if slots['route']['offset'] <= slots['user']['offset']: raise PermissionError('continuation_frozen_native_proof_invalid')
            _continuation_route_record(records['route'], source, owner)
        if 'boundary' in records:
            boundary = records['boundary']; event = boundary['payload']
            if (boundary['type'] != 'event_msg' or slots['boundary']['offset'] <= slots['user']['offset']
                    or 'route' in slots and slots['route']['offset'] >= slots['boundary']['offset']
                    or not (event.get('type') == 'task_started' and event.get('turn_id') != source['turn']
                            or event.get('type') in {'task_complete', 'turn_aborted'} and event.get('turn_id') == source['turn'])):
                raise PermissionError('continuation_native_turn_boundary_invalid')
        after = os.fstat(fd)
        if fresh and signature != [after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns]:
            raise PermissionError('continuation_native_source_changed')
        if identity == 'volume' and _native_volume_uuid(fd) != volume:
            raise PermissionError('continuation_native_volume_changed')
    return proof


def _continuation_source_is_current(source):
    from ..task_closure import Store as ClosureStore
    with ClosureStore(closure_database(), readonly=True) as closure:
        if not closure.has_table('closure_hook_roots'):
            raise PermissionError('continuation_current_native_source_missing')
        row = closure.conn.execute("SELECT * FROM closure_hook_roots WHERE platform='codex' AND session=?",
                                   (source['session'],)).fetchone()
    if not row or any(row[key] != source[key] for key in ('task_id', 'platform', 'session', 'turn', 'prompt_hash')):
        raise PermissionError('continuation_native_source_superseded')


def _continuation_snapshot_unchanged(source, proof):
    _continuation_source_is_current(source)
    with _continuation_native_file(source['session']) as (_, info):
        if proof['scan_signature'] != [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]:
            raise PermissionError('continuation_native_source_changed')


def _continuation_prompt():
    core = Path.home() / 'Library/Application Support/PSI/core'
    bootstrap = ('import os,sys,runpy;os.umask(0o077);sys.dont_write_bytecode=True;sys.path.insert(0,'
                 + repr(str(core)) + ');sys.argv=["task-maintenance",*sys.argv[1:]];'
                 'runpy.run_module("activity_mvp.feedback.task_maintenance",run_name="__main__",alter_sys=True)')
    entry = '/Library/Frameworks/Python.framework/Versions/3.12/bin/python3 -I -S -c ' + shlex.quote(bootstrap)
    return f"""在本聊天继续原PSI文件追踪方案B的自然验收，不创建重复任务。先执行正式安装版固定读取入口：
{entry} read --task agi
核native_use_recorded=true，保存实际version、完整说明及current_sources指纹；spec.sources只作历史。按当前说明和主聊天受控修复回执只读核最新配套、主/文件心跳、八启用根完整基线及实际版本推进；保留停用历史及三项文档排除，不重复迁移。分别统计北京时间日系统控制重置与溢出，区分消费异常、重启、重建、锁屏/等待解锁、进程代际与有来源的有效观察；缺失保持未知。每日重置≤1、无新溢出且充分有效观察和实际处理只称初步见效；任一天≥3或新溢出称未见效；每日2或观察不足待验。系统明确丢事件仍重建。pending=0、旧累计、安装及迁移不代自然验收。
复用原932自然事项及agi，每日一次必要有界核查。本轮统一发布归主聊天；未来发现可修故障先核当前并行负责人和实际安装版本，不与同一正在执行的修复竞争。没有正在执行的同一修复时，由原agi任务在既有权限、固定控制器、串行维护、独立审查及配套发布条件内继续必要修复与验收，不只写“沿主聊天”后无限等待，不要求本人盯进度或另开聊天。superseded_file_events_merged合并待确认记录按原收紧B保留，不误当新故障或自然帮助。保留原权限、范围、固定控制器、预算、模型、暂停取消和人工选择，不清锁/历史、回灌旧库、重放未知、外发或长期记忆。自然观察不新增备份；必要修复发布的恢复件仅按当时原发布条件在已核验T9直接最小生成，不本机暂存或重复。原子写本轮真实UTC进度JSON，三阶段真实布尔与全部未完/负责人/恢复条件；沿已有独立archive_source，不重复归档。使用实际加载版本，经相同固定入口record-run严格四参数并读回recorded=true；未知提交不重试。结束前retirement-check，仅eligible=true且全部原验收/闭环当前满足，由原owner正式删除同一agi、核接口/原生读回并record-retirement，保留历史。日期或低计数不冒称完成。普通等待、恢复、无变化保持安静；只有必须本人决定时请求行动，不要求本人盯进度。"""


def _continuation_previous(ledger, binding):
    previous = _original_effective_scheduler(ledger, 'agi', binding)
    old_commit = ledger.get(_schedule_keys()[1])
    if old_commit is None:
        raise PermissionError('original_committed_recheck_required')
    return previous, digest(old_commit)


def _continuation_scheduler(previous):
    if ('notification_policy' in previous and 'notificationPolicy' in previous
            and previous['notification_policy'] != previous['notificationPolicy']):
        raise PermissionError('continuation_notification_alias_conflict')
    value = {key: item for key, item in previous.items()
             if key not in {'notification_policy', 'notificationPolicy'}}
    return {**value, 'rrule': CONTINUATION_RRULE, 'prompt_sha256': digest(_continuation_prompt()),
            'notification_policy': 'failed_runs_only'}


def _continuation_intent_metadata(ledger, binding, intent):
    if not isinstance(intent, dict) or set(intent) != {'id', 'body'}:
        raise ValueError('continuation_intent_invalid')
    body = intent['body']
    keys = {'plan', 'task', 'owner', 'issue_id', 'prepared_at', 'used_version', 'read_use_id',
            'read_spec_sha256', 'read_sources_sha256', 'authorization_sha256', 'binding_sha256',
            'previous_commit_sha256', 'native_source', 'native_proof', 'old_scheduler', 'new_scheduler'}
    if (not isinstance(body, dict) or set(body) != keys or intent['id'] != digest(body)
            or body['plan'] != CONTINUATION_PLAN or body['task'] != 'agi'
            or body['owner'] != binding['owner_session'] or body['binding_sha256'] != digest(binding)
            or type(body['used_version']) is not int or type(body['prepared_at']) not in (int, float)
            or not 0 < body['prepared_at'] <= time.time() + 30):
        raise ValueError('continuation_intent_binding_invalid')
    authorization, sha = read_json(ledger.root / 'authorization.json')
    if (sha != body['authorization_sha256'] or authorization.get('policy') != POLICY
            or authorization.get('tasks', {}).get('agi') != binding):
        raise PermissionError('schedule_original_authorization_changed')
    previous, previous_commit = _continuation_previous(ledger, binding)
    if (body['old_scheduler'] != previous or body['previous_commit_sha256'] != previous_commit
            or previous.get('kind') != 'heartbeat' or previous.get('status') != 'ACTIVE'
            or previous.get('target_thread_id') != body['owner']
            or body['new_scheduler'] != _continuation_scheduler(previous)):
        raise PermissionError('continuation_exact_scheduler_required')
    if (not isinstance(body['native_source'], dict)
            or body['native_source'] != _continuation_source(body['native_source'].get('task_id'))):
        raise PermissionError('continuation_native_source_changed')
    _schedule_read(ledger, body, current_version=False)
    return body


def _continuation_intent(ledger, binding, intent):
    body = _continuation_intent_metadata(ledger, binding, intent)
    renewal = ledger.get(_source_renewal_key())
    if renewal is not None:
        _source_renewal_record(ledger, binding, intent, renewal)
        # Original records remain historical facts, not a renewed physical proof.
        proof = _continuation_native_proof(body['native_source'], body['owner'], body['issue_id'],
                                           proof=body['native_proof'], identity='records_only')
    else:
        proof = _continuation_native_proof(body['native_source'], body['owner'], body['issue_id'], proof=body['native_proof'])
    if (proof['message_created_at'] < dt.datetime(2026, 10, 27, tzinfo=ZONE).timestamp()
            or proof['message_created_at'] <= ledger.get(_schedule_keys()[1])['committed_at']):
        raise PermissionError('continuation_current_human_request_required')
    return body


def _source_renewal_key():
    return 'native_source_renewal:' + CONTINUATION_PLAN


def _source_renewal_record(ledger, binding, intent, record):
    original = _continuation_intent_metadata(ledger, binding, intent)
    commit = ledger.get(_continuation_keys()[1])
    _continuation_commit(ledger, original, intent, commit)
    keys = {'intent_id', 'commit_sha256', 'authorization_sha256', 'binding_sha256', 'scheduler_sha256',
            'created_at', 'native_source', 'native_proof'}
    if not isinstance(record, dict) or set(record) != {'id', 'body'}:
        raise ValueError('native_source_renewal_record_invalid')
    body = record['body']
    if (not isinstance(body, dict) or set(body) != keys or record['id'] != digest(body)
            or body['intent_id'] != intent['id'] or body['commit_sha256'] != digest(commit)
            or body['authorization_sha256'] != original['authorization_sha256']
            or body['binding_sha256'] != digest(binding)
            or body['scheduler_sha256'] != digest(original['new_scheduler'])
            or type(body['created_at']) not in (int, float)
            or not commit['committed_at'] <= body['created_at'] <= time.time() + 30
            or not isinstance(body['native_source'], dict)
            or body['native_source'] != _continuation_source(body['native_source'].get('task_id'))
            or body['native_source'] == original['native_source']):
        raise PermissionError('native_source_renewal_binding_invalid')
    proof = _continuation_native_proof(body['native_source'], original['owner'], original['issue_id'],
                                       proof=body['native_proof'], identity='volume')
    if not commit['committed_at'] < proof['message_created_at'] <= body['created_at'] + 30:
        raise PermissionError('native_source_renewal_new_person_required')
    return body


def _renew_native_source_locked(store, ledger, task, conf, payload):
    """One explicit read --input renewal; never changes the original grant.

    Device drift is diagnosed, not grandfathered. A new current primary human
    request is independently proven before one append. Another chat must have
    the same successful native route the original protocol requires. Normal
    reads cannot create this record, and no old UUID is inferred.
    """
    if task != 'agi' or set(payload) != {'renew_native_source', 'intent_id', 'scheduler_sha256', 'source_task_id'} or payload['renew_native_source'] is not True:
        raise ValueError('native_source_renewal_fields')
    root, current_conf, context, authorization_sha = _schedule_owner(store, task)
    if root != ledger.root or current_conf != conf: raise PermissionError('schedule_authority_changed_before_append')
    binding = conf['tasks'][task]
    intent = ledger.get(_continuation_keys()[0])
    original = _continuation_intent_metadata(ledger, binding, intent)
    commit = ledger.get(_continuation_keys()[1]); _continuation_commit(ledger, original, intent, commit)
    if payload['intent_id'] != intent['id'] or payload['scheduler_sha256'] != digest(original['new_scheduler']):
        raise PermissionError('native_source_renewal_exact_intent_required')
    if scheduler_metadata(task) != original['new_scheduler']: raise PermissionError('native_schedule_readback_mismatch')
    _schedule_case(binding, original['issue_id'], completed=True)
    saved = ledger.get(_source_renewal_key())
    if saved is not None:
        saved_body = _source_renewal_record(ledger, binding, intent, saved)
        if payload['source_task_id'] != saved_body['native_source']['task_id']:
            raise PermissionError('native_source_renewal_already_recorded')
        return {'recorded': True, 'idempotent': True, 'receipt': saved, 'original_proof_renewed': False, 'scheduler_modified': False}
    # No alternate recovery for changed inode, changed bytes or other failures.
    _continuation_native_proof(original['native_source'], original['owner'], original['issue_id'],
                               proof=original['native_proof'], identity='device_drift')
    source = _continuation_source(payload['source_task_id'])
    if source['session'] == context['session'] and (source['turn'] != context['turn']
            or source['task_id'] != context.get('hook_task_id')):
        raise PermissionError('native_source_renewal_current_person_required')
    _continuation_source_is_current(source)
    proof = _continuation_native_proof(source, original['owner'], original['issue_id'], fresh=True, identity='volume')
    body = {'intent_id': intent['id'], 'commit_sha256': digest(commit), 'authorization_sha256': authorization_sha,
            'binding_sha256': digest(binding), 'scheduler_sha256': payload['scheduler_sha256'],
            'created_at': time.time(), 'native_source': source, 'native_proof': proof}
    record = {'id': digest(body), 'body': body}
    _source_renewal_record(ledger, binding, intent, record)
    confirmed = native_context(task, conf)  # IPC stays outside the writer tx.
    if any(confirmed.get(key) != context.get(key) for key in ('session', 'turn', 'hook_task_id')):
        raise PermissionError('current_native_turn_changed')
    with _schedule_control(store), ledger.tx():
        if ledger.get(_continuation_keys()[0]) != intent or ledger.get(_continuation_keys()[1]) != commit:
            raise PermissionError('native_source_renewal_original_chain_changed')
        _continuation_native_proof(original['native_source'], original['owner'], original['issue_id'],
                                   proof=original['native_proof'], identity='device_drift')
        _source_renewal_record(ledger, binding, intent, record)
        # Finish record/UUID I/O before the final live-authority and source
        # checks. No second proof read may open another window after this.
        _schedule_authority_current(store, root, digest(conf), authorization_sha)
        if ledger.get('enabled') is not True: raise PermissionError('maintenance_paused')
        if scheduler_metadata(task) != original['new_scheduler']: raise PermissionError('scheduler_changed_before_append')
        _schedule_case(binding, original['issue_id'], completed=True)
        _continuation_snapshot_unchanged(source, proof)
        _append_schedule_record(ledger, _source_renewal_key(), record)
    return {'recorded': True, 'idempotent': False, 'receipt': record, 'original_proof_renewed': False, 'scheduler_modified': False}


def _continuation_loaded_read(ledger, context, *, unrecorded=False):
    key = digest(['agi', context['session'], context['turn']])
    row = ledger.db.execute('SELECT * FROM uses WHERE id=?', (key,)).fetchone()
    loaded = ledger.db.execute('SELECT * FROM use_sources WHERE id=?', (key,)).fetchone()
    if not row or not loaded:
        raise ValueError('actual_schedule_read_required')
    if unrecorded and row['result'] is not None:
        raise ValueError('continuation_run_already_recorded')
    proof = {'used_version': row['version'], 'read_use_id': key, 'read_spec_sha256': row['spec_sha256'],
             'read_sources_sha256': digest(json.loads(loaded['sources']))}
    _schedule_read(ledger, {**proof, 'owner': context['session']})
    return proof, json.loads(loaded['spec'])


def _continuation_commit(ledger, body, intent, commit):
    keys = {'intent_id', 'committed_at', 'committed_by', 'scheduler_sha256', 'read'}
    if (not isinstance(commit, dict) or set(commit) != keys or commit['intent_id'] != intent['id']
            or commit['scheduler_sha256'] != digest(body['new_scheduler'])
            or type(commit['committed_at']) not in (int, float)
            or not body['prepared_at'] <= commit['committed_at'] <= time.time() + 30
            or not isinstance(commit['committed_by'], dict) or set(commit['committed_by']) != {'session', 'turn'}
            or commit['committed_by']['session'] != body['owner'] or not commit['committed_by']['turn']
            or not isinstance(commit['read'], dict) or set(commit['read']) !=
            {'used_version', 'read_use_id', 'read_spec_sha256', 'read_sources_sha256'}
            or commit['read']['read_use_id'] != digest(['agi', body['owner'], commit['committed_by']['turn']])):
        raise ValueError('continuation_commit_invalid')
    _schedule_read(ledger, {**body, **commit['read']}, current_version=False)


def _continuation_pending(ledger, task, binding):
    if task != 'agi': return None
    intent_key, commit_key = _continuation_keys()
    intent, commit = ledger.get(intent_key), ledger.get(commit_key)
    if intent is None or commit is not None: return None
    body = _continuation_intent(ledger, binding, intent)
    _schedule_case(binding, body['issue_id'], completed=True)
    return body


def _continuation_result(intent, *, state, idempotent, receipt=None):
    body = intent['body']
    value = {'intent_id': intent['id'], 'plan': CONTINUATION_PLAN, 'state': state,
             'idempotent': idempotent, 'rrule': body['new_scheduler']['rrule'],
             'notificationPolicy': body['new_scheduler']['notification_policy'],
             'prompt': _continuation_prompt(), 'expected_scheduler': body['new_scheduler'],
             'scheduler_modified': False, 'native_trigger_verified': False}
    if receipt is not None:
        value.update(committed=True, receipt=receipt, native_readback_verified=True,
                     next_version_read_requires_new_native_turn=True)
    elif idempotent:
        value['reconcile_original_native_update_only'] = True
    else:
        value['native_update_required'] = True
    return value


def prepare_continuation(store, task, payload):
    root, conf, context, authorization_sha = _schedule_owner(store, task)
    binding, conf_sha = conf['tasks'][task], digest(conf)
    if set(payload) != {'used_version', 'issue_id', 'source_task_id'} or type(payload['used_version']) is not int:
        raise ValueError('continuation_prepare_fields')
    with Ledger(root) as ledger, task_lock(ledger, task):
        if ledger.get('enabled') is not True: raise PermissionError('maintenance_paused')
        intent_key, commit_key = _continuation_keys()
        existing, committed = ledger.get(intent_key), ledger.get(commit_key)
        read, spec = _continuation_loaded_read(ledger, context, unrecorded=existing is None)
        if read['used_version'] != payload['used_version']: raise ValueError('actual_loaded_version_required')
        if existing is not None:
            body = _continuation_intent(ledger, binding, existing)
            _schedule_case(binding, body['issue_id'], completed=True)
            if (payload['issue_id'] != body['issue_id']
                    or payload['source_task_id'] != body['native_source']['task_id']):
                raise PermissionError('continuation_already_prepared')
            if committed is not None: _continuation_commit(ledger, body, existing, committed)
            allowed = [body['new_scheduler']] if committed else [body['old_scheduler'], body['new_scheduler']]
            if scheduler_metadata(task) not in allowed: raise PermissionError('native_schedule_readback_mismatch')
            return _continuation_result(existing, state='committed' if committed else 'prepared',
                                        idempotent=True, receipt=committed)
        if committed is not None: raise ValueError('continuation_intent_missing')
        _schedule_case(binding, payload['issue_id'])
        if spec['stages']['natural_effect'] is not False: raise PermissionError('natural_effect_already_accepted')
        previous, previous_commit = _continuation_previous(ledger, binding)
        native_source = _continuation_source(payload['source_task_id'])
        _continuation_source_is_current(native_source)
        native_proof = _continuation_native_proof(native_source, context['session'], payload['issue_id'], fresh=True)
        body = {'plan': CONTINUATION_PLAN, 'task': task, 'owner': context['session'],
                'issue_id': payload['issue_id'], 'prepared_at': time.time(), **read,
                'authorization_sha256': authorization_sha, 'binding_sha256': digest(binding),
                'previous_commit_sha256': previous_commit, 'native_source': native_source, 'native_proof': native_proof,
                'old_scheduler': previous, 'new_scheduler': _continuation_scheduler(previous)}
        intent = {'id': digest(body), 'body': body}
        _continuation_intent(ledger, binding, intent)
        if scheduler_metadata(task) != previous: raise PermissionError('original_scheduler_definition_changed')
        with _schedule_control(store), ledger.tx():
            if ledger.get('enabled') is not True: raise PermissionError('maintenance_paused')
            _schedule_authority_current(store, root, conf_sha, authorization_sha)
            _schedule_case(binding, payload['issue_id']); _continuation_intent(ledger, binding, intent)
            if _continuation_loaded_read(ledger, context, unrecorded=True)[0] != read: raise ValueError('continuation_read_changed')
            if scheduler_metadata(task) != previous: raise PermissionError('scheduler_changed_before_prepare')
            _schedule_authority_current(store, root, conf_sha, authorization_sha)
            _continuation_snapshot_unchanged(native_source, native_proof)
            _append_schedule_record(ledger, intent_key, intent)
        return _continuation_result(intent, state='prepared', idempotent=False)


def commit_continuation(store, task, payload):
    root, conf, context, authorization_sha = _schedule_owner(store, task)
    binding, conf_sha = conf['tasks'][task], digest(conf)
    if set(payload) != {'intent_id'}: raise ValueError('continuation_commit_fields')
    with Ledger(root) as ledger, task_lock(ledger, task):
        if ledger.get('enabled') is not True: raise PermissionError('maintenance_paused')
        intent_key, commit_key = _continuation_keys()
        intent, saved = ledger.get(intent_key), ledger.get(commit_key)
        body = _continuation_intent(ledger, binding, intent)
        if payload['intent_id'] != intent['id']: raise ValueError('continuation_intent_id_mismatch')
        _schedule_case(binding, body['issue_id'], completed=saved is not None)
        if scheduler_metadata(task) != body['new_scheduler']: raise PermissionError('native_schedule_readback_mismatch')
        if saved is not None:
            _continuation_commit(ledger, body, intent, saved)
            return _continuation_result(intent, state='committed', idempotent=True, receipt=saved)
        read, _ = _continuation_loaded_read(ledger, context, unrecorded=True)
        with _schedule_control(store), ledger.tx():
            if ledger.get('enabled') is not True: raise PermissionError('maintenance_paused')
            _schedule_authority_current(store, root, conf_sha, authorization_sha)
            if ledger.get(intent_key) != intent: raise ValueError('continuation_intent_changed_before_commit')
            _schedule_case(binding, body['issue_id']); _continuation_intent(ledger, binding, intent)
            if _continuation_loaded_read(ledger, context, unrecorded=True)[0] != read: raise ValueError('continuation_read_changed')
            if scheduler_metadata(task) != body['new_scheduler']: raise PermissionError('scheduler_changed_before_commit')
            _schedule_authority_current(store, root, conf_sha, authorization_sha)
            receipt = {'intent_id': intent['id'], 'committed_at': time.time(),
                       'committed_by': {key: context[key] for key in ('session', 'turn')},
                       'scheduler_sha256': digest(body['new_scheduler']), 'read': read}
            _continuation_commit(ledger, body, intent, receipt)
            _append_schedule_record(ledger, commit_key, receipt)
        return _continuation_result(intent, state='committed', idempotent=False, receipt=receipt)


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


def _refresh(ledger, task, binding, *, allow_pending_read=False):
    expected, actual = effective_scheduler(ledger, task, binding), scheduler_metadata(task)
    if actual != expected:
        pending = _continuation_pending(ledger, task, binding) if allow_pending_read else None
        if not pending or actual != pending['new_scheduler']:
            raise PermissionError('original_scheduler_definition_changed')
    latest = ledger.get('latest_source:' + task, {})
    source = latest.get('source', binding['source']); archive_path = latest.get('archive_source', binding['archive_source'])
    receipt, ref = checked_source(task, source)
    archive, archive_ref = checked_source(task, archive_path) if archive_path else (None, None)
    stages = normalize(task, receipt, archive)
    refs = [ref] + ([archive_ref] if archive_ref else [])
    row = ledger.db.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone()
    old = json.loads(row['spec']) if row else None
    detail = receipt.get('remaining', []) if task == 'automation-9' else []
    continuation = None
    if task == 'agi' and ledger.get(_continuation_keys()[1]) is not None:
        continuation = {'plan': CONTINUATION_PLAN, 'intent_id': ledger.get(_continuation_keys()[0])['id'],
                        'scheduler_sha256': digest(expected)}
        renewal = ledger.get(_source_renewal_key())
        if renewal is not None: continuation['native_source_renewal_id'] = renewal['id']
    semantic = digest([stages, detail, continuation]) if continuation else digest([stages, detail])
    changed = old is None or old['semantic'] != semantic
    version = (row['version'] + 1 if changed else row['version']) if row else 1
    spec = {'policy': POLICY, 'task': task, 'name': TASKS[task], 'version': version, 'semantic': semantic,
            'owner_session': binding['owner_session'], 'stages': stages, 'sources': refs, 'next_steps': steps(task, stages),
            'observed_at': receipt.get('at', receipt.get('observed_at')), 'updated_at': time.time(),
            'boundary': '原目标、时间、模型、隐私、预算、验收标准、暂停取消和人工选择保持。'}
    if continuation:
        spec['schedule_continuation'] = continuation
        spec['boundary'] = '仅PSI原任务观察时间沿本次原生授权的精确续接；原目标、模型、隐私、预算、验收标准、暂停取消和人工选择保持。'
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
    """Observe only post-authorization PSI job transitions; never export model prose."""
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
    if (task not in TASKS or not context or context.get('platform') != 'codex'
            or context['session'] != conf['tasks'][task]['owner_session']):
        raise PermissionError('original_native_owner_required')
    # Heartbeats do not refresh UserPromptSubmit's root. Keep that receipt as
    # owner provenance, but obtain the executing turn from the live desktop.
    try:
        from ..native_metadata import MetadataBudget
        with DesktopIPC(metadata_budget=MetadataBudget()) as ipc:
            snapshot = ipc.snapshot(context['session'], expected_cwd=PROJECTS['agi']['repository'],
                                    current_turn=True, allow_registered_worktree=True)
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


def load_task(store, task, root=None, *, source_renewal=None):
    actual, conf = authorized(store)
    if root is not None and Path(root) != actual: raise PermissionError('maintenance_root_mismatch')
    if task not in conf['tasks']: raise PermissionError('task_not_authorized')
    with Ledger(actual) as ledger:
        if ledger.get('enabled') is not True: raise PermissionError('maintenance_paused')
        with task_lock(ledger, task):
            if source_renewal is None: return _load_task_locked(ledger, task, conf)
            renewal = _renew_native_source_locked(store, ledger, task, conf, source_renewal)
            try:
                result = _load_task_locked(ledger, task, conf)
            except (PermissionError, ValueError, OSError, sqlite3.Error) as exc:
                # The append may have committed. Preserve its exact receipt;
                # do not conceal it in a generic rejection or append again.
                return {'status': 'blocked', 'source_renewal': renewal, 'native_use_recorded': False,
                        'read_error': str(exc) if isinstance(exc, (PermissionError, ValueError)) else type(exc).__name__}
            return {**result, 'source_renewal': renewal}


def _load_task_locked(ledger, task, conf):
        # Only the original operator read may reconcile one authenticated native
        # update before commit. Ordinary refresh/record/retirement stay closed.
        context = None
        pending = _continuation_pending(ledger, task, conf['tasks'][task])
        if pending:
            context = native_context(task, conf)
            if context.get('platform') != 'codex' or context.get('session') != conf['tasks'][task]['owner_session']:
                raise PermissionError('original_native_owner_required')
        _refresh(ledger, task, conf['tasks'][task], allow_pending_read=pending is not None)
        row = ledger.db.execute('SELECT spec FROM tasks WHERE id=?', (task,)).fetchone()
        spec = json.loads(row[0]); refs = ledger.get('validated_sources:' + task)['sources']
        result = {'spec': spec, 'current_sources': refs, 'native_use_recorded': False,
                  'run_receipt_contract': '本次进度写入登记报告目录内的新文件；成功 record-run 前不要覆盖上一轮已登记来源。采用本次实际读取版本，来源变化或并发拒绝时保留证据，下一轮重新核查；不得降低原条件。'}
        if task == 'agi' and ledger.get(_schedule_keys()[0]) is not None:
            result['current_schedule'] = effective_scheduler(ledger, task, conf['tasks'][task])
            result['schedule_amendment'] = {'plan': SCHEDULE_PLAN, 'intent': ledger.get(_schedule_keys()[0]),
                                             'commit': ledger.get(_schedule_keys()[1]), 'native_trigger_verified': False}
        if task == 'agi' and ledger.get(_continuation_keys()[0]) is not None:
            result['schedule_continuation'] = {
                'plan': CONTINUATION_PLAN, 'intent': ledger.get(_continuation_keys()[0]),
                'commit': ledger.get(_continuation_keys()[1]),
                'source_renewal': ledger.get(_source_renewal_key()),
                'pending_commit': ledger.get(_continuation_keys()[1]) is None,
                'current_native_schedule': scheduler_metadata(task), 'native_trigger_verified': False}
        try: context = context or native_context(task, conf)
        except (PermissionError, OSError, sqlite3.Error): return result
        key = digest([task, context['session'], context['turn']])
        # The per-task lock is held, but IPC/filesystem checks must not hold
        # evolution.sqlite's database-wide writer transaction.
        confirmed = native_context(task, conf)
        if (confirmed['session'], confirmed['turn']) != (context['session'], context['turn']):
            raise PermissionError('current_native_turn_changed')
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
    with Ledger(root) as ledger, task_lock(ledger, task):
        if ledger.get('enabled') is not True: raise PermissionError('maintenance_paused')
        if _continuation_pending(ledger, task, conf['tasks'][task]) is not None:
            raise PermissionError('continuation_commit_pending')
        if scheduler_metadata(task) != effective_scheduler(ledger, task, conf['tasks'][task]):
            raise PermissionError('original_scheduler_definition_changed')
        return _record_run_locked(ledger, conf, task, context, payload, stages, ref, archive_ref)


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
        confirmed = native_context(task, conf)
        if (confirmed['session'], confirmed['turn']) != (context['session'], context['turn']):
            raise PermissionError('current_native_turn_changed')
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
    if task == 'agi' and _continuation_pending(ledger, task, conf['tasks'][task]) is not None:
        return False
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
    parser.add_argument('action', choices=('configure', 'read', 'record-run', 'record-retirement', 'status', 'pause', 'resume', 'retirement-check', 'prepare-reschedule', 'commit-reschedule', 'prepare-continuation', 'commit-continuation'))
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
        if args.action == 'read': return load_task(store, args.task, source_renewal=payload if args.input else None)
        if args.action == 'record-run': return record_run(store, args.task, payload)
        if args.action == 'prepare-reschedule': return prepare_reschedule(store, args.task, payload)
        if args.action == 'commit-reschedule': return commit_reschedule(store, args.task, payload)
        if args.action == 'prepare-continuation': return prepare_continuation(store, args.task, payload)
        if args.action == 'commit-continuation': return commit_continuation(store, args.task, payload)
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
