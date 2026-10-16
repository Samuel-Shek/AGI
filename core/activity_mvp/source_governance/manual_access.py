"""Owner-authorized foreground access, separate from retired automation.

The native client is the identity trust boundary. A ledger row is a scope
declaration, not an independent proof that the human authorized its contents.
No background caller, memory writer, scheduler, or sender imports this module.
"""
import argparse
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import importlib.util
import threading
import json
import os
from pathlib import Path
import stat
import sys

ROOT = Path.home()/"Library/Application Support/AGI/source-governance"
OWNER_HOME = Path.home()
_NATIVE_SESSION_ROOT = OWNER_HOME/'.codex/sessions'
POLICY = ROOT / 'manual-use-policy.json'
RUNTIME = OWNER_HOME / '.config/task-closure/runtime.json'
LEGACY = ROOT / 'closure-manual-restorations.json'
VERSION = 'owner-manual-use-v1'


def _load_core():
    # Fixed installed entry; no path or database supplied by document content.
    sys.path.insert(0, str(OWNER_HOME/'Library/Application Support/Personal AI Monitor/core'))
    from activity_mvp import task_closure
    return task_closure


def _policy(core):
    value, raw, identity = core._manual_owned_json(POLICY, private_file=True)
    if (value.get('version') != VERSION or value.get('owner_id') != 'local-owner'
            or value.get('enabled') is not True
            or value.get('manual_operations') != ['read', 'closure', 'backup']
            or value.get('automatic_execution') is not False
            or value.get('memory_write') is not False
            or value.get('external_send') is not False
            or not all(isinstance(value.get(k), str) and value[k].strip()
                       for k in ('authorization_source','authorization_session','authorization_turn'))):
        raise PermissionError('manual_policy_unavailable')
    return value, raw, identity


def _legacy_project(core, task, paths):
    value, _, _ = core._manual_owned_json(core.MANUAL_CLOSURE_PERMITS, private_file=True)
    if value.get('version') != 1 or value.get('owner_id') != 'local-owner':
        raise PermissionError('legacy_manual_policy_invalid')
    roots = [task['scope_root'], *paths]
    return any(isinstance(g, dict) and g.get('platform') == task['platform']
               and isinstance(g.get('root'), str)
               and any(Path(p).is_relative_to(g['root']) for p in roots)
               for g in value.get('project_grants', []))


def _runtime(core):
    runtime, _, _ = core._manual_owned_json(RUNTIME, private_file=True)
    expected_core = OWNER_HOME/'Library/Application Support/Personal AI Monitor/core'
    if runtime.get('core') != str(expected_core) or runtime.get('database') != str(expected_core/'data/activity.sqlite'):
        raise PermissionError('manual_runtime_not_registered')
    return runtime


# This context is issued only by the native Stop adapter after its bounded
# current-event proof. Reads and background callers never consume it.
_STOP_CONTEXT = ContextVar('owner_manual_native_stop', default=None)
_TASK_BINDING = ('id', 'platform', 'session', 'turn', 'scope_root', 'requirements_hash')


def _native_stop_proof(payload):
    # Fixed local validator; callers cannot supply a callback or pre-approved proof.
    path = ROOT.parent/'shared-skills/task-closure/scripts/closure_hook.py'
    info = path.stat()
    if path.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o022:
        return None
    spec = importlib.util.spec_from_file_location('owner_native_stop_evidence', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    # Identity proof never trusts caller-selected CODEX_HOME or payload roots.
    module.CODEX_SESSIONS = _NATIVE_SESSION_ROOT
    return module.current_final_pair(payload, allow_plain=True)


def _stop_identity(store, task, core):
    context = _STOP_CONTEXT.get()
    if not context or context['core'] is not core or not context['active'] or context['thread'] != threading.get_ident():
        return None
    expected = context['task']
    if any(task.get(k) != expected[k] for k in _TASK_BINDING):
        return None
    row = store.conn.execute('SELECT * FROM closure_tasks WHERE id=?', (task['id'],)).fetchone()
    if not row or any(row[k] != expected[k] for k in _TASK_BINDING) or not store.native_root(row):
        return None
    if context['stop_turn'] != task['turn']:
        guard = store.conn.execute('SELECT attempts FROM closure_guards WHERE guard_key=?', (task['id'],)).fetchone()
        last = store.conn.execute("SELECT data FROM closure_events WHERE task_id=? AND kind='native_correction_turn' ORDER BY seq DESC LIMIT 1", (task['id'],)).fetchone()
        if not guard or not guard[0] or not last or json.loads(last[0]).get('turn') != context['stop_turn']:
            return None
    if _native_stop_proof(context['payload']) != context['proof']:
        return None
    return {'platform': task['platform'], 'session': task['session'], 'turn': task['turn'], 'task_id': task['id']}


def _native_caller(store, task, core, *, closure_only=False):
    actual = store.recovery_caller()
    if not closure_only or _STOP_CONTEXT.get() is None:
        return actual
    native = _stop_identity(store, task, core)
    # A present conflicting process identity is a denial, never overwritten.
    if native is None or (os.environ.get('CODEX_THREAD_ID') and actual != native):
        return None
    return native


@contextmanager
def native_stop_context(task, payload, *, core, native_store):
    """Ephemeral closure-only identity, independently bound to native final bytes."""
    task = dict(task)
    if _STOP_CONTEXT.get() is not None or task.get('platform') != 'codex' or not isinstance(payload, dict):
        raise PermissionError('native_stop_context_invalid')
    payload = {k: payload.get(k) for k in ('session_id', 'sessionId', 'turn_id', 'turnId', 'transcript_path', 'last_assistant_message')}
    proof = _native_stop_proof(payload)
    if proof is None or (payload.get('session_id') or payload.get('sessionId')) != task['session']:
        yield False
        return
    runtime = _runtime(core)
    registered_ledger = core.root_for(runtime['database'])/'closure.sqlite'
    if native_store.path != registered_ledger or type(native_store) is not core.Store:
        yield False
        return
    context = {'task': {k: task[k] for k in _TASK_BINDING},
               'stop_turn': payload.get('turn_id') or payload.get('turnId'), 'payload': payload,
               'proof': proof, 'core': core, 'active': True, 'thread': threading.get_ident()}
    token = _STOP_CONTEXT.set(context)
    try:
        with core.Store(runtime['database'], readonly=True) as store:
            valid = _native_caller(store, task, core, closure_only=True) is not None
        yield valid
    finally:
        # Copies of this ContextVar also share the revoked token object.
        context['active'] = False
        _STOP_CONTEXT.reset(token)


def _native_store(core, task, *, closure_only=False):
    runtime = _runtime(core)
    store = core.Store(runtime['database'], readonly=True)
    try:
        expected = {k: task[k] for k in ('platform','session','turn')}
        expected['task_id'] = task['id']
        actual = _native_caller(store, task, core, closure_only=closure_only)
        canonical = store.conn.execute('SELECT * FROM closure_tasks WHERE id=?',(task['id'],)).fetchone()
        if (actual != expected or not canonical or not store.native_root(canonical)
                or any(dict(canonical).get(k) != task.get(k)
                       for k in ('id','platform','session','turn','scope_root','requirements_hash'))):
            raise PermissionError('manual_native_task_mismatch')
        return store
    except BaseException:
        store.conn.close()
        raise


def _declared(core, row, path):
    authority = json.loads(row['authority'])
    if not authority.get('source') or not core.authority_scope_allows(authority, Path(row['target_root']), Path(path)):
        return False
    return True


def _read_declared(core, row, path):
    # Evidence is an exact read input, not permission to write its directory.
    return _declared(core, row, path) or any(
        r.get('path') == str(path) for r in json.loads(row['evidence']))


def _read_owned(path, root, limit=1024*1024):
    original=str(path);path,root=Path(path),Path(root)
    relative=path.relative_to(root)
    if str(path)!=original or not path.is_absolute() or not relative.parts or any(x in {'.','..'} for x in original.split('/')):
        raise PermissionError('manual_source_path_invalid')
    descriptors=[]
    try:
        fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);descriptors.append(fd)
        for part in path.parts[1:-1]:
            fd=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd);descriptors.append(fd)
        fd=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=fd);descriptors.append(fd)
        before=os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid!=os.getuid() or before.st_nlink!=1 or before.st_size>limit:
            raise PermissionError('manual_source_owner_or_type_invalid')
        chunks=[];length=0
        while length<=limit:
            block=os.read(fd,min(65536,limit+1-length))
            if not block:break
            chunks.append(block);length+=len(block)
        after=os.fstat(fd)
        identity=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_mode,s.st_uid,s.st_nlink)
        if identity(before)!=identity(after) or length!=after.st_size or length>limit:
            raise PermissionError('manual_source_changed_during_read')
        return b''.join(chunks)
    finally:
        for fd in reversed(descriptors):os.close(fd)


def native_closure_rows(store, task):
    """Canonical same-chat continuations; this metadata query grants no access."""
    task = dict(task)
    rows = [dict(r) for r in store.conn.execute(
        'SELECT * FROM closure_issues WHERE task_id=?', (task['id'],))]
    selected = {r['id']: r for r in rows}
    links = store.conn.execute(
        "SELECT issue_id,data FROM closure_events WHERE task_id=? AND kind='duplicate_linked'",
        (task['id'],)).fetchall()
    for link in links:
        try:
            link_data = json.loads(link['data'])
        except (ValueError, TypeError):
            return None
        if link_data != {'source_task': task['id']}:
            return None
        if link['issue_id'] in selected:
            continue
        row = store.conn.execute(
            "SELECT i.* FROM closure_issues i JOIN closure_tasks original ON original.id=i.task_id "
            "WHERE i.id=? AND i.owner=? AND original.platform=? AND original.session=? "
            "AND i.state NOT IN ('decision','canceled','dismissed')",
            (link['issue_id'], task['platform'] + ':' + task['session'], task['platform'], task['session'])).fetchone()
        if row is None:
            return None
        row = dict(row)
        if row['task_id'] != task['id']:
            original = store.conn.execute('SELECT * FROM closure_tasks WHERE id=?', (row['task_id'],)).fetchone()
            if (not original or task['platform'] != 'codex'
                    or original['platform'] != task['platform'] or original['session'] != task['session']
                    or row['owner'] != task['platform'] + ':' + task['session']
                    or row['state'] in {'decision', 'canceled', 'dismissed'}):
                return None
        selected[row['id']] = row
    return [selected[key] for key in sorted(selected)]


def _current_closure_artifact(core, store, task, path):
    """Only the current task's canonical, hash-bound closure leaf files."""
    member, root = Path(path), Path(task['scope_root'])
    if not member.is_relative_to(root) or member == root:
        return False
    audit = store.conn.execute('SELECT * FROM closure_audits WHERE task_id=?', (task['id'],)).fetchone()
    if not audit or audit['requirements_hash'] != task['requirements_hash']:
        return False
    review = json.loads(audit['review'])
    entries = json.loads(audit['evidence'])
    entries += [review.get(key) for key in ('independent_artifact', 'wording_artifact', 'final_response')]
    matches = [entry for entry in entries if isinstance(entry, dict) and entry.get('path') == path]
    digests = {entry.get('sha256') for entry in matches}
    if len(digests) != 1:
        return False
    expected = next(iter(digests))
    if (not isinstance(expected, str) or len(expected) != 64
            or any(c not in '0123456789abcdef' for c in expected)):
        return False
    core._manual_path_identity(path)
    return hashlib.sha256(_read_owned(member, root, limit=2 * 1024 * 1024)).hexdigest() == expected


def closure_override(task, item=None, paths=(), *, core):
    """None retains the existing explicit project permit; False denies override."""
    if not POLICY.exists(): return None
    try:
        task = dict(task)
        item = dict(item) if item is not None else task
        bound = _policy(core)
        inputs=[core.MANUAL_CLOSURE_PERMITS,core.MANUAL_RETIREMENT_REGISTRY,
                core.MANUAL_RETIREMENT_BRIDGE,RUNTIME]
        bindings=[core._manual_owned_json(p,private_file=True)[1:] for p in inputs]
        explicit = list(dict.fromkeys(core.metadata_paths(item) + list(paths)))
        if len(explicit)>2000:
            return False
        if _legacy_project(core,task,explicit): return None
        with _native_store(core,task,closure_only=True) as store:
            rows = native_closure_rows(store, task)
            if rows is None:
                return False
            canonical_rows = rows
            if not core.owner_retired(item, paths) and not any(core.owner_retired(r) for r in rows):
                return None
            if item.get('id') != task['id']:
                canonical=next((r for r in rows if r['id']==item.get('id')),None)
                if not canonical or any(item.get(k)!=canonical[k] for k in ('task_id','target_root','authority','owner','revision','state','evidence','acceptance')):
                    return False
                rows=[canonical]
            elif any(item.get(k) != task.get(k) for k in _TASK_BINDING):
                return False
            for path in explicit:
                if not isinstance(path,str) or not Path(path).is_absolute():return False
                # Root metadata is not a body read. Every evidence leaf still
                # needs a declared issue scope (or the original task root).
                if path not in {task['scope_root'],*(r['target_root'] for r in rows)}:
                    if not any(_read_declared(core,r,path) for r in rows):
                        if item.get('id') != task['id'] or not _current_closure_artifact(core,store,task,path):
                            return False
                core._manual_path_identity(path)
            if (native_closure_rows(store,task) != canonical_rows
                    or _policy(core)[1:]!=bound[1:]
                    or [core._manual_owned_json(p,private_file=True)[1:] for p in inputs]!=bindings):return False
            if not store.native_root(task) or _native_caller(store,task,core,closure_only=True) != {'platform':task['platform'],'session':task['session'],'turn':task['turn'],'task_id':task['id']}:
                return False
        return True
    except (OSError,ValueError,TypeError,KeyError,AttributeError,RuntimeError):
        return False


def read_authorized(issue_id, path, *, expected_sha256=None, content=True):
    """Read one declared ordinary file in the current native owner task.

    This does not enroll a source, grant memory recall, export anything, or
    authorize a future operation. The result remains in the calling task.
    """
    core=_load_core();bound=_policy(core)
    runtime=_runtime(core)
    with core.Store(runtime['database'],readonly=True) as initial:
        row=initial.get(issue_id)
        task=dict(initial.conn.execute('SELECT * FROM closure_tasks WHERE id=?',(row['task_id'],)).fetchone())
        native=initial.recovery_caller()
    with _native_store(core,task) as store:
        current=store.get(issue_id)
        if current!=row or row['state'] in {'decision','canceled','dismissed'} or not _read_declared(core,row,path):
            raise PermissionError('manual_source_not_authorized')
        if not core.safe_authority(row):
            raise PermissionError('manual_authority_stale_or_requires_decision')
        evidence=json.loads(row['evidence'])
        paths=[path,*[r['path'] for r in evidence]]
        inputs=[core.MANUAL_CLOSURE_PERMITS, core.MANUAL_RETIREMENT_REGISTRY,
                core.MANUAL_RETIREMENT_BRIDGE, RUNTIME]
        bindings=[core._manual_owned_json(p,private_file=True)[1:] for p in inputs]

        def permission_current():
            if (_policy(core)[1:]!=bound[1:] or store.get(issue_id)!=row
                    or not store.native_root(task) or store.recovery_caller()!=native
                    or [core._manual_owned_json(p,private_file=True)[1:] for p in inputs]!=bindings):
                raise PermissionError('manual_authorization_changed')
            if _legacy_project(core,task,paths) and not core.manual_closure_allowed(task,row,paths):
                raise PermissionError('explicit_project_permission_unavailable')

        permission_current()
        # Check every input before any content read. Exact evidence can be a
        # read-only input outside the declared write targets, within this root.
        for member in paths:
            Path(member).relative_to(row['target_root'])
            core._manual_path_identity(member)
        for member in evidence:
            permission_current()
            evidence_raw=_read_owned(member['path'],row['target_root'],2*1024*1024)
            if hashlib.sha256(evidence_raw).hexdigest()!=member['sha256']:
                raise PermissionError('manual_evidence_changed')
        permission_current()
        raw=_read_owned(path, Path(row['target_root']))
        digest=hashlib.sha256(raw).hexdigest()
        if expected_sha256 is not None and digest!=expected_sha256:
            raise PermissionError('manual_source_changed')
        permission_current()
        result={'status':'authorized_manual_read','path':path,'sha256':digest,'bytes':len(raw),'automatic_execution':False}
        if content: result['text']=raw.decode('utf-8')
        return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['read','check'])
    parser.add_argument('--issue-id',required=True)
    parser.add_argument('--path',required=True)
    parser.add_argument('--sha256')
    args=parser.parse_args()
    try:
        result=read_authorized(args.issue_id,args.path,expected_sha256=args.sha256,content=args.action=='read')
        print(json.dumps(result,ensure_ascii=False))
    except (OSError,ValueError,TypeError,KeyError,RuntimeError) as error:
        print(json.dumps({'status':'denied','error':str(error)}));raise SystemExit(2)


if __name__=='__main__':main()
