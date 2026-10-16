"""Fixed verification and paired local publication, outside model execution."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import sqlite3
import subprocess
import time
import ctypes
import stat
import fcntl
if __package__ in (None,''):
    import sys
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
    __package__='activity_mvp.feedback'
from .store import packed,atomic,private_directory,digest
from .control import gate

FOLDERS=('activity_mvp','tools','browser_extension','evolution')
FILES=('pyproject.toml','pam_isolated_launcher.py')

def run(args,cwd=None,timeout=900):return subprocess.run([str(a) for a in args],cwd=cwd,capture_output=True,text=True,check=True,timeout=timeout)
def core_hash(root):
    paths=[root/p for p in FILES]
    for folder in FOLDERS:paths.extend(p for p in (root/folder).rglob('*') if p.is_file() and p.suffix!='.pyc' and p.name!='.DS_Store')
    h=hashlib.sha256()
    for p in sorted(set(paths),key=lambda p:p.relative_to(root).as_posix()):
        h.update(('core/'+p.relative_to(root).as_posix()).encode()+b'\0');h.update(hashlib.sha256(p.read_bytes()).hexdigest().encode()+b'\n')
    return h.hexdigest()

def verify_candidate(store,candidate,base):
    """Reject the retired host-execution route; never run a candidate here.

    The existing governed project controller owns isolated verification and
    authorization. This compatibility entry cannot activate that authority.
    """
    raise PermissionError('legacy_verifier_retired_use_governed_project_repair')

def backup_database(path,destination):
    if not path.exists():return
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as src,sqlite3.connect(destination) as dst:
        src.backup(dst)
        if dst.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('backup_invalid')
    os.chmod(destination,0o600)

def _schema_shape(db):
    """Only structural metadata, never user rows."""
    objects=dict(db.execute("SELECT name,type FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"))
    columns={}
    for name,kind in objects.items():
        if kind=='table':
            quoted=name.replace('"','""')
            columns[name]={r[1]:(r[2].upper(),r[3],r[4],r[5]) for r in db.execute('PRAGMA table_info("'+quoted+'")')}
    return objects,columns

def _unchanged_startup_shape(current,initializer,meta_key,meta_value):
    """Exercise trusted startup on an empty copy of structural metadata only.

    Existing installations can have a valid historical shape different from a
    fresh database. Prove startup leaves that actual shape unchanged instead of
    assuming fresh CREATE statements represent every upgraded installation.
    No user rows are copied or read.
    """
    sql=current.execute("SELECT name,type,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL ORDER BY CASE WHEN sql LIKE 'CREATE VIRTUAL TABLE%' THEN 0 WHEN type='table' THEN 1 WHEN type='index' THEN 2 WHEN type='view' THEN 3 ELSE 4 END").fetchall()
    before=_schema_shape(current)
    with sqlite3.connect(':memory:') as shadow:
        shadow.row_factory=sqlite3.Row
        for name,kind,statement in sql:
            if not shadow.execute('SELECT 1 FROM sqlite_master WHERE name=?',(name,)).fetchone():shadow.execute(statement)
        shadow.execute('INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)',(meta_key,meta_value))
        if _schema_shape(shadow)!=before:raise ValueError('shadow_structure_not_equivalent')
        initializer(shadow)
        after_sql=shadow.execute("SELECT name,type,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL").fetchall()
        if _schema_shape(shadow)!=before or sorted(map(tuple,after_sql))!=sorted(map(tuple,sql)):
            raise ValueError('startup_changes_existing_structure')
    return hashlib.sha256(json.dumps([sorted(map(tuple,sql)),before],sort_keys=True).encode()).hexdigest()

def database_structure_policy(live,feedback_path):
    from ..storage import init_schema,SCHEMA_VERSION
    from .store import SCHEMA
    try:
        with sqlite3.connect((live/'data/activity.sqlite').as_uri()+'?mode=ro',uri=True) as current:
            current.execute('PRAGMA query_only=ON')
            version=current.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if version is None or str(version[0])!=str(SCHEMA_VERSION):raise ValueError('activity_schema_version')
            activity_shape=_unchanged_startup_shape(current,init_schema,'schema_version',version[0])
        with sqlite3.connect(feedback_path.as_uri()+'?mode=ro',uri=True) as current:
            current.execute('PRAGMA query_only=ON')
            version=current.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            if version is None or json.loads(version[0])!=1:raise ValueError('feedback_schema_version')
            feedback_shape=_unchanged_startup_shape(current,lambda db:db.executescript(SCHEMA),'schema',version[0])
    except (sqlite3.Error,ValueError,TypeError) as exc:
        return {'databases_required':True,'reason':'structure_unverified','detail':str(exc)}
    return {'databases_required':False,'reason':'existing_versions_and_startup_structure_verified',
            'schema':SCHEMA_VERSION,'activity_structure_sha256':activity_shape,'feedback_structure_sha256':feedback_shape}

def database_snapshot_policy(live,candidate,feedback_path):
    """Skip databases only for the already-governed pure text repair surface."""
    from .repair import EDITABLE
    allowed={str(Path(p).relative_to('core')) for p in EDITABLE}
    before={str(p.relative_to(live)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (live/'activity_mvp').rglob('*.py')}
    after={str(p.relative_to(candidate/'core')):hashlib.sha256(p.read_bytes()).hexdigest()
           for p in (candidate/'core/activity_mvp').rglob('*.py')}
    changed={p for p in before.keys()|after.keys() if before.get(p)!=after.get(p)}
    if not changed<=allowed:
        return {'databases_required':True,'reason':'initialization_or_other_dependency_changed'}
    from .policy import validate_pure_change
    for rel in changed:validate_pure_change('core/'+rel,(candidate/'core'/rel).read_text())
    result=database_structure_policy(live,feedback_path)
    if not result['databases_required']:result['reason']='pure_text_code_only_schema_and_initializers_unchanged'
    return result

def prepare_recovery(live,candidate,recovery,feedback_path,copy=None,guard=None):
    """Retain changed old files once, directly in the selected external payload."""
    copy=copy or shutil.copy2
    paths=[live/p for p in FILES]
    for folder in FOLDERS:paths.extend(p for p in (live/folder).rglob('*') if p.is_file() and p.suffix!='.pyc' and p.name!='.DS_Store')
    retained={}
    for old in sorted(set(paths)):
        rel=old.relative_to(live);new=candidate/'core'/rel
        if not new.exists() or old.read_bytes()!=new.read_bytes():
            if guard:guard()
            target=recovery/rel;target.parent.mkdir(parents=True,exist_ok=True);copy(old,target)
            retained[str(rel)]=hashlib.sha256(target.read_bytes()).hexdigest()
    policy=database_snapshot_policy(live,candidate,feedback_path)
    if policy['databases_required']:
        if guard:guard()
        needed=sum(p.stat().st_size for p in [live/'data/activity.sqlite',feedback_path] if p.exists())+64*1024*1024
        if shutil.disk_usage(recovery).free<needed:raise ValueError('recovery_insufficient_database_space')
        backup_database(live/'data/activity.sqlite',recovery/'activity.sqlite')
        if guard:guard()
        backup_database(feedback_path,recovery/'feedback.sqlite')
        if guard:guard()
    return {'retained_code':retained,'backup_policy':policy,'database_restore_allowed':False,
            'old_application':'AGI-replaced.app','format':'minimal-recovery-v1'}

def register_recovery_archive(recovery):
    """Only the operator-installed lifecycle tool, never a model-provided command."""
    manager=Path.home()/'AI/backup-governance/backup_manager.py'
    if not manager.is_file() or manager.is_symlink():return {'status':'tool_unavailable'}
    try:
        result=run(['/usr/local/bin/python3','-B',manager,'register',recovery,'--project','agi'],timeout=180)
        return json.loads(result.stdout)
    except (subprocess.SubprocessError,ValueError) as exc:
        # A timeout can occur after registration succeeded. Do not mutate a
        # package whose immutable fingerprint the copier may already have bound.
        errors=private_directory(recovery.parent.parent/'archive-registration-errors')
        atomic(errors/(recovery.name+'.json'),packed({'source':str(recovery),'error':str(exc),'at':time.time()}))
        return {'status':'pending_registration','error':str(exc)}

def _require_publication_active(store):
    from .autonomy import active
    if not active(store):raise PermissionError('authorization_revoked')

def publish_candidate(store,candidate,base,verification,job,*,require_ui_absent=False):
    _require_publication_active(store)
    conf=store.get('config');repo=Path(conf['repository']);live=Path(conf['live_core']);app=Path(conf['application'])
    if live!=Path.home()/'Library/Application Support/Personal AI Monitor/core' or app!=Path('/Applications/AGI.app'):raise PermissionError('release_target_not_registered')
    if run(['git','rev-parse','HEAD'],repo).stdout.strip()!=base or run(['git','status','--porcelain','--untracked-files=no'],repo).stdout.strip():raise ValueError('source_baseline_changed')
    old=plistlib.loads((app/'Contents/Info.plist').read_bytes())
    if not base.startswith(old['PAMGitRevision']) or core_hash(live)!=old['PAMCoreCodeHash']:raise ValueError('installed_baseline_changed')
    if core_hash(candidate/'core')!=verification['core_hash']:raise ValueError('candidate_changed')
    from .recovery_storage import prepare_external,check_external
    before={'revision':old['PAMGitRevision'],'core_hash':old['PAMCoreCodeHash'],'job':job}
    recovery,local_receipt=prepare_external(store.root,job,app,before,
        lambda payload,copy,guard:prepare_recovery(live,candidate,payload,store.path,copy,guard))
    check_external(recovery,local_receipt)
    _require_publication_active(store)
    ui_yielded=False
    # A separate installed publisher process survives UI/core code replacement.
    request={'candidate':str(candidate),'require_ui_absent':require_ui_absent,'base':base,'verification':verification,'recovery':str(recovery),
             'local_receipt':str(local_receipt),'config':{k:conf[k] for k in ['repository','live_core','application']}}
    request_path=private_directory(store.root/'publication-requests')/(job+'.json')
    script=Path(__file__).resolve()
    # Separate process group survives stopping the old launchd controller.
    request_written=False;process=None
    try:
        if require_ui_absent:
            from activity_mvp.feedback.project_repair import app_busy,prepare_idle_exit
            ui_yielded=app_busy('agi')
            ready=prepare_idle_exit(store,'agi')
            _require_publication_active(store)
            if not ready:return {'installed':False,'status':'app_busy'}
        atomic(request_path,packed(request))
        request_written=True
        # Serialize only actual dispatch with the owner's pause/stop commands.
        # External recovery and the cooperative UI wait never hold this lock.
        with gate(store.root):
            _require_publication_active(store)
            process=subprocess.Popen([conf['python'],str(script),'--publish',str(request_path)],
                                     start_new_session=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    except OSError:
        # Archive only a known failed dispatch, never a prior request or a
        # process already started before a later control-lock exit failure.
        if process is None and request_written:retain_unexecuted_request(request_path,local_receipt,'process_not_started')
        if process is None and ui_yielded:
            subprocess.run(['/usr/bin/open','-gj',str(app),'--args','--background-maintenance'],capture_output=True,timeout=15)
        raise
    try:
        output,error=process.communicate(timeout=180)
    except subprocess.TimeoutExpired:
        try:_publication_diagnostic(request_path,'process',process.pid,outcome='timeout',returncode=None)
        except (OSError,ValueError):pass
        raise  # The child may still be running. Never terminate or replay it.
    try:_publication_diagnostic(request_path,'process',process.pid,outcome='returned',returncode=process.returncode)
    except (OSError,ValueError):pass
    if process.returncode:
        if ui_yielded and not app_busy('agi'):
            subprocess.run(['/usr/bin/open','-gj',str(app),'--args','--background-maintenance'],capture_output=True,timeout=15)
        raise RuntimeError('publication_process_failed')
    receipt=json.loads(output)
    if receipt.get('status')=='deferred_t9' and not receipt.get('installed'):
        # A completed child explicitly reports pre-exchange deferral. An
        # exchange journal, even an unfinished one, disallows this retry path.
        retain_unexecuted_request(request_path,local_receipt,'pre_exchange_t9_unavailable')
        if ui_yielded and not app_busy('agi'):
            subprocess.run(['/usr/bin/open','-gj',str(app),'--args','--background-maintenance'],capture_output=True,timeout=15)
        from .recovery_storage import RecoveryDeferred
        raise RecoveryDeferred(receipt.get('reason','external_recovery_pending'))
    receipt['recovery_storage']='external_verified'
    return receipt

def _publication_diagnostic(request,kind,pid,**fields):
    """Small immutable metadata only; never persist output, argv or exceptions."""
    import re
    from .task_maintenance import read_json
    request=Path(request);root=request.parent.parent;job=request.stem
    if kind not in {'process','failure'} or not re.fullmatch('[0-9a-f]{24}',job) or type(pid) is not int or pid < 1:raise ValueError('diagnostic_scope')
    private_directory(root);private_directory(root/'publication-requests')
    receipts=private_directory(root/'publication-receipts')
    value,sha=read_json(request)
    if request.parent!=root/'publication-requests' or value.get('local_receipt')!=str(receipts/(job+'.json')):raise ValueError('diagnostic_binding')
    record={'job':job,'pid':pid,'request_sha256':sha,'at':time.time(),**fields}
    content=packed(record).encode('utf-8')
    if len(content)>4096:raise ValueError('diagnostic_size')
    target=receipts/(job+'.'+kind+'-'+str(pid)+'-'+str(time.time_ns())+'.json')
    fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as handle:handle.write(content);handle.flush();os.fsync(handle.fileno())
    fd=os.open(receipts,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(fd)
    finally:os.close(fd)
    if read_json(target)[0]!=record:raise ValueError('diagnostic_readback')


def _publish_with_diagnostic(request):
    try:return standalone_publish(request)
    except BaseException as exc:
        # Only fixed, exact controller error labels are eligible for a local
        # diagnosis. Unknown errors retain their uncertainty without raw text.
        known={'prepared_original_identity_changed','prepared_original_changed','candidate_application_changed',
               'candidate_application_identity_changed','candidate_code_alias','release_code_stage_conflict',
               'live_code_alias','candidate_changed','source_changed_during_publish','live_drift_before_publish',
               'authorization_revoked','application_reopened_before_exchange','publication_committed_receipt_pending'}
        code=str(exc) if isinstance(exc,(ValueError,PermissionError)) and str(exc) in known else 'unknown'
        category=type(exc).__name__ if type(exc) in {ValueError,PermissionError,OSError,subprocess.TimeoutExpired,subprocess.CalledProcessError} else 'unknown'
        frame=exc.__traceback__;location=None
        while frame:
            if frame.tb_frame.f_code.co_filename==__file__:location={'function':frame.tb_frame.f_code.co_name,'line':frame.tb_lineno}
            frame=frame.tb_next
        try:_publication_diagnostic(request,'failure',os.getpid(),outcome='raised',error=code,category=category,location=location)
        except (OSError,ValueError):pass
        raise

def retain_unexecuted_request(request_path,local_receipt,reason):
    """Archive known no-exchange evidence; never clear an uncertain transaction."""
    if reason not in {'process_not_started','pre_exchange_t9_unavailable'}:raise ValueError('invalid_dispatch_failure')
    if local_receipt.with_suffix('.exchange.json').exists():return False
    target=request_path.with_name(request_path.stem+'.not-executed-'+str(time.time_ns())+'.json')
    # The failed original request remains intact; its separate receipt explains
    # why no installed receipt is expected. This is not an unknown-request retry.
    atomic(target.with_suffix('.receipt.json'),packed({'reason':reason,'request_sha256':hashlib.sha256(request_path.read_bytes()).hexdigest(),'exchange_started':False}))
    os.rename(request_path,target)
    return True

def _swap(left,right):
    """Darwin same-volume atomic exchange of the original objects, not copies."""
    libc=ctypes.CDLL(None,use_errno=True);rename=libc.renamex_np
    rename.argtypes=[ctypes.c_char_p,ctypes.c_char_p,ctypes.c_uint];rename.restype=ctypes.c_int
    if rename(os.fsencode(left),os.fsencode(right),2):
        code=ctypes.get_errno();raise OSError(code,os.strerror(code))
    for parent in {left.parent,right.parent}:
        fd=os.open(parent,os.O_RDONLY|os.O_NOFOLLOW)
        try:os.fsync(fd)
        finally:os.close(fd)

def _token(path):
    s=path.lstat()
    if stat.S_ISLNK(s.st_mode) or (stat.S_ISREG(s.st_mode) and s.st_nlink!=1):raise PermissionError('publication_object_alias')
    return [s.st_dev,s.st_ino]

def _journal_write(path,value):
    private_directory(path.parent);atomic(path,packed(value))
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:fcntl.fcntl(fd,51)
    finally:os.close(fd)
    fd=os.open(path.parent,os.O_RDONLY|os.O_NOFOLLOW)
    try:os.fsync(fd)
    finally:os.close(fd)

def _entry_scope(entry,live,app):
    target=Path(entry['target']);temp=Path(entry['stage'])
    if target!=app:
        if not target.is_relative_to(live) or '..' in target.parts:raise PermissionError('journal_target_scope')
        rel=target.relative_to(live)
        if rel.parts[0] not in FOLDERS and str(rel) not in FILES:raise PermissionError('journal_target_scope')
    allowed=(temp.parent==app.parent and temp.name.startswith('.AGI-feedback-') and temp.name.endswith('.app')) if target==app else (temp.parent.parent==live.parent and temp.parent.name.startswith('.AGI-publication-') and temp.name.isdigit())
    if not allowed:raise PermissionError('journal_stage_scope')
    if any(p.is_symlink() for p in [target,*target.parents,temp,*temp.parents]):raise PermissionError('journal_object_alias')
    return target,temp

def recover_exchange(journal,live,app,commit_info=None):
    """Reconcile a killed pre-commit exchange from its write-ahead identities."""
    journal=Path(journal)
    if not journal.exists():return
    record=json.loads(journal.read_text())
    if record.get('live')!=str(live) or record.get('application')!=str(app):raise PermissionError('journal_pair_binding')
    if record.get('state') not in {'exchanging','committing','accepted','cleaning','completed','rolled_back'}:
        raise ValueError('publication_state_unknown')
    for entry in record['entries']:_entry_scope(entry,live,app)
    if record['state']=='rolled_back':return
    if record['state'] in ('accepted','committing','cleaning','completed'):
        saved=record.get('commit_info')
        if saved:
            if saved!=commit_info:raise ValueError('publication_commit_binding_changed')
            head=run(['git','rev-parse','HEAD'],Path(saved['repository'])).stdout.strip()
            if head==saved['revision']:
                if core_hash(live)!=record['expected_core_hash']:raise ValueError('committed_live_changed')
                entry=next(e for e in record['entries'] if Path(e['target'])==app)
                if _token(app)!=entry['new']:raise ValueError('committed_application_changed')
                if record['state']=='committing':record['state']='accepted';_journal_write(journal,record)
                return record
            if head!=saved['base'] or record['state'] in ('accepted','cleaning','completed'):raise ValueError('publication_commit_conflict')
        elif record['state'] in ('accepted','cleaning','completed'):
            if core_hash(live)!=record['expected_core_hash']:raise ValueError('accepted_live_changed')
            return record
    for entry in reversed(record['entries']):
        target,temp=_entry_scope(entry,live,app)
        actual=_token(target) if target.exists() else None
        staged=_token(temp) if temp.exists() else None
        if entry['old']:
            if actual==entry['new'] and staged==entry['old']:_swap(target,temp)
            elif actual!=entry['old'] or staged!=entry['new']:raise ValueError('journal_identity_conflict')
        elif actual==entry['new'] and staged is None:os.rename(target,temp)
        elif actual is not None or staged!=entry['new']:raise ValueError('journal_identity_conflict')
    if core_hash(live)!=record['previous_core_hash']:raise ValueError('interrupted_code_restore_failed')
    record['state']='rolled_back';record['interruption_recovered']=True
    record['rollback_completed_at']=time.time();_journal_write(journal,record)

def finalize_exchange(journal,guard=None,original_guard=None):
    """Called only after paired installation and runtime receipts are durable."""
    journal=Path(journal);record=json.loads(journal.read_text())
    if record['state']=='completed':return True
    if record['state'] not in ('accepted','cleaning'):raise ValueError('publication_not_accepted')
    record['state']='cleaning';_journal_write(journal,record)
    for entry in record['entries']:
        target,temp=_entry_scope(entry,Path(record['live']),Path(record['application']))
        if _token(target)!=entry['new']:raise ValueError('accepted_target_identity_changed')
        if entry['old'] and temp.exists():
            if guard:guard()
            if _token(temp)!=entry['old']:raise ValueError('accepted_original_identity_changed')
            if original_guard:original_guard.cleanup(entry,guard)
            elif temp.is_dir():shutil.rmtree(temp)
            else:temp.unlink()
    for folder in {Path(e['stage']).parent for e in record['entries'] if Path(e['stage']).parent.name.startswith('.AGI-publication-')}:
        if folder.exists():
            folder.rmdir();fd=os.open(folder.parent,os.O_RDONLY|os.O_NOFOLLOW)
            try:os.fsync(fd)
            finally:os.close(fd)
    record['state']='completed';_journal_write(journal,record);return True

def exchange_pair(candidate, live, app, recovery, changed, expected_hash, validate,guard=None,journal=None,commit=None,commit_info=None,cleanup=True,original_guard=None,candidate_guard=None):
    """Keep original inodes on their volume until acceptance; rollback needs no T9.

    Stages contain the candidate until a swap. After a swap they are the actual
    originals, not a locally generated backup. The journal is metadata only.
    """
    stage=app.with_name('.AGI-feedback-'+str(os.getpid())+'.app')
    journal=Path(journal) if journal else recovery/'exchange.json'
    resumed=recover_exchange(journal,live,app,commit_info)
    if resumed:
        if guard:guard()
        validate()
        # A committed retry validates the pair without executing another commit
        # or overwriting new data. Cleanup is separately identity constrained.
        done=finalize_exchange(journal,guard,original_guard) if cleanup else resumed['state']=='completed'
        return {'paired':True,'core_hash':core_hash(live),'transaction_journal':str(journal),'original_cleanup_complete':done,'resumed':True}
    previous_hash=core_hash(live)
    if guard:guard()
    if candidate_guard:candidate_guard(candidate/'build/AGI.app',True)
    if stage.exists() or stage.is_symlink():raise ValueError('release_exchange_already_started')
    run(['/usr/bin/ditto',candidate/'build/AGI.app',stage])
    if candidate_guard:
        candidate_guard(candidate/'build/AGI.app',True);candidate_guard(stage,False)
    code_stage=live.parent/('.AGI-publication-'+str(os.getpid()))
    if code_stage.exists() or code_stage.is_symlink():raise ValueError('release_code_stage_conflict')
    private_directory(code_stage)
    exchanges=[];stages=[stage];entries=[]
    def save(state):_journal_write(journal,{'state':state,'previous_core_hash':previous_hash,'expected_core_hash':expected_hash,
                                         'commit_info':commit_info,'entries':entries,'live':str(live),'application':str(app),'candidate':str(candidate),'at':time.time()})
    def exchange(target,temp):
        old=_token(target) if target.exists() else None;new=_token(temp)
        if original_guard and original_guard(target)!=old:raise ValueError('prepared_original_identity_changed')
        entry={'target':str(target),'stage':str(temp),'old':old,'new':new}
        entries.append(entry);save('exchanging')
        if old:_swap(target,temp)
        else:os.rename(temp,target)
        exchanges.append(entry)
        if _token(target)!=new or (old and _token(temp)!=old):raise ValueError('publication_identity_changed')
        save('exchanging')
    try:
        seen=set()
        for rel in changed:
            rel=Path(rel)
            if rel.is_absolute() or '..' in rel.parts or str(rel) in seen:raise PermissionError('release_path_scope')
            seen.add(str(rel))
            target=live/rel;target.parent.mkdir(parents=True,exist_ok=True)
            if any(p.is_symlink() for p in [target,*target.parents]):raise PermissionError('live_code_alias')
            source=candidate/'core'/rel
            if not source.is_file() or source.is_symlink() or source.stat().st_nlink!=1:raise PermissionError('candidate_code_alias')
            temp=code_stage/str(len(stages))
            if temp.exists() or temp.is_symlink():raise ValueError('release_stage_conflict')
            shutil.copy2(source,temp);stages.append(temp)
            if guard:guard()
            exchange(target,temp)
        if core_hash(live)!=expected_hash:raise ValueError('live_hash_mismatch')
        if guard:guard()
        exchange(app,stage)
        validate()
        if guard:guard()
        if commit:
            save('committing');commit()
        save('accepted')
    except BaseException:
        if commit_info and run(['git','rev-parse','HEAD'],Path(commit_info['repository'])).stdout.strip()==commit_info['revision']:
            # The ff commit is the commit point. A subsequent receipt failure
            # cannot put installed code back while leaving Git at the new HEAD.
            # Retain original transaction objects and reconcile this same journal.
            raise ValueError('publication_committed_receipt_pending')
        if not entries:
            # No write-ahead intent belongs to this attempt. A pause or guard
            # failure must not erase the previous proven rollback journal.
            _journal_write(journal.with_name(journal.stem+'.unexchanged-'+str(time.time_ns())+'.json'),
                           {'state':'no_new_exchange','candidate':str(candidate),'candidate_app_stage':str(stage),
                            'candidate_code_stage':str(code_stage),'current_data_preserved':True,
                            'previous_journal_sha256':hashlib.sha256(journal.read_bytes()).hexdigest() if journal.exists() else None})
            raise
        # The syscall can have swapped successfully before a directory flush
        # fails. Reconcile all write-ahead intents from actual inode positions.
        recover_exchange(journal,live,app,commit_info)
        restored=core_hash(live)==previous_hash
        # This receipt must survive an unplugged external disk.
        atomic(journal.with_name(journal.stem+'.rolled-back.json'),packed({'code_restored':restored,'current_data_preserved':True,
                'expected_previous_core_hash':previous_hash,'actual_core_hash':core_hash(live)}));save('rolled_back')
        if not restored:raise ValueError('rollback_code_readback_failed')
        raise
    # Remove only the original transaction objects whose identity was retained.
    # A hard interruption leaves them and the intent journal for reconciliation.
    if cleanup:
        try:cleanup=finalize_exchange(journal,guard,original_guard)
        except (OSError,ValueError):cleanup=False
    return {'paired':True,'core_hash':core_hash(live),'transaction_journal':str(journal),'original_cleanup_complete':cleanup}

def _publication_boot_identity():
    import re
    at=run(['/usr/sbin/sysctl','-n','kern.boottime'],timeout=5).stdout
    match=re.search(r'sec = (\d+)',at)
    uuid=run(['/usr/sbin/sysctl','-n','kern.bootsessionuuid'],timeout=5).stdout.strip()
    if not match or not re.fullmatch(r'[A-Fa-f0-9-]{36}',uuid):raise ValueError('publication_boot_identity_unavailable')
    return {'at':int(match[1]),'uuid':uuid}


def _postboot_rollback_context(request,r,prepared,journal):
    """Explicitly revalidate a pre-boot operator rollback on this Data volume.

    Legacy proofs have no source-volume UUID. This does not invent one: every
    old inode, content and attribute is rechecked against the current registered
    internal APFS volume, then its exact current identity is retained separately.
    No caller can supply a device-ignore flag or replace the original proofs.
    """
    from .recovery_storage import backend
    from .task_maintenance import read_file,_pairs
    from .store import Store
    b=backend();request=Path(request);journal=Path(journal)
    candidate=Path(r['candidate']);app_candidate=candidate/'build/AGI.app'
    old_app=r['verification']['application_identity'];current_app=b.tree_identity(app_candidate)
    old_devices={row[0] for row in old_app.values()}
    original_devices={row[0] for bound in prepared['source_bindings'].values() for row in bound['identity'].values()}
    current_devices={row[0] for row in current_app.values()}
    if old_devices==current_devices and original_devices==current_devices:return None
    if len(old_devices)!=1 or old_devices!=original_devices or len(current_devices)!=1:
        raise ValueError('postboot_device_binding_conflict')
    old_device=next(iter(old_devices));new_device=next(iter(current_devices))
    if not journal.exists():raise ValueError('postboot_known_rollback_required')
    def metadata(path):
        data=read_file(Path(path))
        value=json.loads(data,object_pairs_hook=_pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('nonfinite_json')))
        return data,value
    journal_bytes,record=metadata(journal)
    live=Path(r['config']['live_core']);app=Path(r['config']['application']);local=Path(r['local_receipt'])
    binding={'repository':r['config']['repository'],'base':r['base'],'revision':r['verification']['revision']}
    boot=_publication_boot_identity();volume=b.source_volume(app_candidate)
    pointer=local.parent/(local.stem+'.postboot-current-'+boot['uuid']+'.json')
    ref=None;saved=None
    if pointer.exists() or pointer.is_symlink():
        _,ref=metadata(pointer);receipt_path=Path(ref.get('receipt',''))
        if receipt_path.parent!=local.parent/(local.stem+'.postboot-history'):
            raise ValueError('postboot_context_reference_unbound')
        content,saved=metadata(receipt_path)
        expected={name:{member:[new_device,*row[1:]] for member,row in bound['identity'].items()}
                  for name,bound in prepared['source_bindings'].items()}
        if (hashlib.sha256(content).hexdigest()!=ref.get('sha256')
            or saved.get('status')!='explicit_operator_postboot_revalidation'
            or saved.get('boot')!=boot or saved.get('source_volume')!=volume
            or saved.get('commit_info')!=binding or saved.get('request')!=str(request)
            or saved.get('candidate_identity')!=current_app
            or saved.get('original_expected_identities')!=expected):
            raise ValueError('postboot_context_identity_changed')
        for path in (request,local,Path(r['verification']['acceptance_evidence_path']).parent/'release-verification.json'):
            if metadata(path)[0]!=saved['snapshots'][str(path)]['text'].encode():raise ValueError('postboot_evidence_changed')
        original_journal=json.loads(saved['snapshots'][str(journal)]['text'])
        original_completed=original_journal.get('rollback_completed_at',original_journal.get('at',0))
        if (original_journal.get('state')!='rolled_back' or original_journal.get('commit_info')!=binding
            or not prepared['prepared_at']<=original_completed<boot['at']):
            raise ValueError('postboot_context_origin_unbound')
    installed_path=local.with_suffix('.installed.json')
    if record.get('state') in ('committing','accepted','cleaning','completed'):
        if (not ref or record.get('commit_info')!=binding
            or run(['git','rev-parse','HEAD'],Path(binding['repository'])).stdout.strip()!=binding['revision']):
            raise ValueError('postboot_committed_identity_changed')
        if installed_path.exists():
            _,installed=metadata(installed_path)
            if (installed.get('executor',{}).get('postboot_revalidation')!=ref
                or installed.get('installed') is not True or installed.get('revision')!=binding['revision']
                or installed.get('core_hash')!=r['verification']['core_hash']):
                raise ValueError('postboot_committed_receipt_unbound')
        with Store(live/'data/feedback',readonly=True) as store:_require_publication_active(store)
        return {'candidate_identity':current_app,'original_expected_identities':expected,
                'entry_tokens':saved['entry_tokens'],**ref}
    if (record.get('state')!='rolled_back' or record.get('commit_info')!=binding
        or record.get('candidate')!=str(candidate) or record.get('live')!=str(live)
        or record.get('application')!=str(app) or request.parent!=live/'data/feedback/publication-requests'
        or local.parent!=live/'data/feedback/publication-receipts' or request.stem!=local.stem
        or journal!=local.with_suffix('.exchange.json') or prepared['before']['job']!=request.stem):
        raise ValueError('postboot_transaction_binding_changed')
    if local.with_suffix('.installed.json').exists():raise ValueError('postboot_installed_receipt_conflict')
    completed=record.get('rollback_completed_at',record.get('at'))
    if (not isinstance(completed,(int,float)) or not isinstance(prepared.get('prepared_at'),(int,float))
        or not prepared['prepared_at']<=completed<=time.time() or boot['at']>time.time()
        or (not ref and completed>=boot['at'])):
        raise ValueError('postboot_preboot_rollback_required')
    if record.get('interruption_recovered') and 'rollback_completed_at' not in record:
        raise ValueError('postboot_rollback_completion_unbound')
    verification=r['verification']
    if verification.get('operator_release') is not True:raise ValueError('postboot_operator_qualification_required')
    qualification=Path(verification['acceptance_evidence_path']).parent/'release-verification.json'
    qualification_bytes,product=metadata(qualification)
    if product!=verification:raise ValueError('postboot_product_qualification_changed')
    repo=Path(binding['repository'])
    if (run(['git','rev-parse','HEAD'],repo).stdout.strip()!=r['base']
        or run(['git','--no-optional-locks','status','--porcelain','--untracked-files=no'],repo).stdout.strip()
        or core_hash(live)!=prepared['before']['core_hash'] or core_hash(candidate/'core')!=verification['core_hash']
        or record.get('previous_core_hash')!=prepared['before']['core_hash']
        or record.get('expected_core_hash')!=verification['core_hash']):
        raise ValueError('postboot_baseline_changed')
    if volume['device']!=new_device:raise ValueError('postboot_volume_device_changed')
    def on_volume(path):
        b.no_alias(Path(path))
        if b.source_volume(Path(path))!=volume:raise ValueError('postboot_source_volume_changed')
    def remap(tree):return {name:[new_device,*row[1:]] for name,row in tree.items()}
    if current_app!=remap(old_app) or b.fingerprint(b.manifest(app_candidate))!=verification['application_manifest_sha256']:
        raise ValueError('postboot_candidate_changed')
    on_volume(app_candidate)
    run(['/usr/bin/codesign','--verify','--deep','--strict',app_candidate])
    entries=record.get('entries',[]);tokens={};touched=set();stages=set()
    journal_device=old_device if completed<boot['at'] else new_device
    if not entries:raise ValueError('postboot_entries_missing')
    for entry in entries:
        target,stage=_entry_scope(entry,live,app)
        if str(target) in touched or str(stage) in stages:raise ValueError('postboot_duplicate_entry')
        touched.add(str(target));stages.add(str(stage))
        for path,token in ((target,entry.get('old')),(stage,entry.get('new'))):
            if not isinstance(token,list) or len(token)!=2 or token[0]!=journal_device:
                raise ValueError('postboot_entry_identity_unbound')
            on_volume(path)
            expected=[new_device,token[1]]
            if _token(path)!=expected:raise ValueError('postboot_entry_identity_changed')
            tokens[str(path)]=expected
        bound=prepared['source_bindings'].get(str(target))
        original_token=bound['identity'].get(str(target),[])[:2] if bound else None
        if not original_token or [journal_device,original_token[1]]!=entry['old']:
            raise ValueError('postboot_original_binding_changed')
        if target==app:
            if b.fingerprint(b.manifest(stage))!=verification['application_manifest_sha256']:
                raise ValueError('postboot_retained_candidate_changed')
        elif b.digest(stage)!=b.digest(candidate/'core'/target.relative_to(live)):
            raise ValueError('postboot_retained_candidate_changed')
    originals={};observed={}
    for name,bound in prepared['source_bindings'].items():
        path=Path(name);on_volume(path);actual=b.tree_identity(path);expected=remap(bound['identity'])
        if set(actual)!=set(expected):raise ValueError('postboot_original_members_changed')
        for member,old in expected.items():
            now=actual[member];root_ctime=member==name and name in touched
            if len(old)!=6 or len(now)!=6 or (now[:4]+now[5:]!=old[:4]+old[5:] if root_ctime else now!=old):
                raise ValueError('postboot_original_identity_changed')
            if root_ctime and now[4]!=old[4] and not old[4]<=now[4]<=int(completed*1_000_000_000):
                raise ValueError('postboot_original_ctime_changed')
        if b.fingerprint(b.manifest(path))!=bound['manifest_sha256']:raise ValueError('postboot_original_content_changed')
        originals[name]=expected;observed[name]=actual
    snapshots={}
    for path in (request,local,journal,local.with_suffix('.exchange.rolled-back.json'),local.with_suffix('.runtime.json'),qualification):
        if not path.exists():continue
        data,value=metadata(path);snapshots[str(path)]={'sha256':hashlib.sha256(data).hexdigest(),'text':data.decode()}
        if path==request and value!=r or path==local and value!=prepared or path==journal and data!=journal_bytes:
            raise ValueError('postboot_evidence_changed')
    if snapshots[str(qualification)]['text'].encode()!=qualification_bytes:raise ValueError('postboot_evidence_changed')
    def recheck():
        with Store(live/'data/feedback',readonly=True) as store:_require_publication_active(store)
        if _publication_boot_identity()!=boot or b.source_volume(app_candidate)!=volume or b.tree_identity(app_candidate)!=current_app:
            raise ValueError('postboot_current_identity_changed')
        if any(b.tree_identity(Path(name))!=tree for name,tree in observed.items()) or any(_token(Path(name))!=token for name,token in tokens.items()):
            raise ValueError('postboot_current_identity_changed')
        if any(metadata(Path(name))[0]!=row['text'].encode() for name,row in snapshots.items()):raise ValueError('postboot_evidence_changed')
    recheck()
    if ref:return {'candidate_identity':current_app,'original_expected_identities':originals,'entry_tokens':tokens,**ref}
    receipt={'schema':1,'status':'explicit_operator_postboot_revalidation','boot':boot,'source_volume':volume,
             'legacy_source_uuid_recorded':False,'old_device':old_device,'current_device':new_device,
             'request':str(request),'commit_info':binding,'snapshots':snapshots,
             'candidate_identity':current_app,'original_expected_identities':originals,
             'original_observed_identities':observed,'entry_tokens':tokens,'database_restore_allowed':False}
    history=local.parent/(local.stem+'.postboot-history');b.no_alias(history);private_directory(history)
    key=digest([boot['uuid'],snapshots[str(request)]['sha256'],snapshots[str(local)]['sha256'],snapshots[str(journal)]['sha256']])
    destination=history/(key+'.json');content=packed(receipt).encode()
    if len(content)>2*1024*1024:raise ValueError('postboot_history_too_large')
    if destination.exists() or destination.is_symlink():
        if metadata(destination)[0]!=content:raise ValueError('postboot_history_conflict')
    else:
        import tempfile
        fd,name=tempfile.mkstemp(prefix='.'+key+'.',suffix='.pending',dir=history)
        with os.fdopen(fd,'wb') as out:
            out.write(content);out.flush();os.fsync(out.fileno());fcntl.fcntl(out.fileno(),51)
        b.exclusive_move(Path(name),destination);b.sync_directory(history)
    if metadata(destination)[0]!=content:raise ValueError('postboot_history_readback_failed')
    recheck()
    ref={'receipt':str(destination),'sha256':hashlib.sha256(content).hexdigest()}
    import tempfile
    fd,name=tempfile.mkstemp(prefix='.'+pointer.name+'.',suffix='.pending',dir=pointer.parent)
    with os.fdopen(fd,'wb') as out:
        out.write(packed(ref).encode());out.flush();os.fsync(out.fileno());fcntl.fcntl(out.fileno(),51)
    b.exclusive_move(Path(name),pointer);b.sync_directory(pointer.parent)
    if metadata(pointer)[1]!=ref:raise ValueError('postboot_context_readback_failed')
    recheck()
    return {'candidate_identity':current_app,'original_expected_identities':originals,'entry_tokens':tokens,**ref}


def _rollback_original_guard(request, r, prepared, journal, postboot=None):
    """Rebind only a proven native rollback's root ctime, retaining old proof.

    This is a fixed-controller continuation of the same candidate and request.
    It cannot classify an unknown request, replace originals or change a gate.
    Every later swap still checks the complete, exact reconciled identity.
    """
    from .recovery_storage import backend, original_checker
    from .task_maintenance import read_file, _pairs
    def metadata(path):
        data=read_file(path)
        json.loads(data,object_pairs_hook=_pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('nonfinite_json')))
        return data
    b=backend();request=Path(request);journal=Path(journal)
    if not journal.exists():return original_checker(prepared)
    b.no_alias(journal)
    record=json.loads(metadata(journal))
    if record.get('state')!='rolled_back':
        effective={**prepared,'source_bindings':{name:{**bound,'identity':postboot['original_expected_identities'][name]}
                    for name,bound in prepared['source_bindings'].items()}} if postboot else prepared
        return original_checker(effective)
    live=Path(r['config']['live_core']);app=Path(r['config']['application'])
    candidate=Path(r['candidate']);local=Path(r['local_receipt'])
    commit_info={'repository':r['config']['repository'],'base':r['base'],'revision':r['verification']['revision']}
    if (record.get('live')!=str(live) or record.get('application')!=str(app)
        or record.get('candidate')!=str(candidate) or record.get('commit_info')!=commit_info
        or record.get('previous_core_hash')!=prepared['before']['core_hash']
        or record.get('expected_core_hash')!=r['verification']['core_hash']
        or prepared['before']['job']!=request.stem or local.stem!=request.stem
        or local.parent!=live/'data/feedback/publication-receipts'
        or request.parent!=live/'data/feedback/publication-requests'
        or journal!=local.with_suffix('.exchange.json')):
        raise ValueError('rollback_request_binding_changed')
    installed=local.with_suffix('.installed.json')
    if installed.exists() or installed.is_symlink():raise ValueError('rollback_installed_receipt_conflict')
    repo=Path(commit_info['repository'])
    if (run(['git','rev-parse','HEAD'],repo).stdout.strip()!=r['base']
        or run(['git','--no-optional-locks','status','--porcelain','--untracked-files=no'],repo).stdout.strip()
        or core_hash(live)!=prepared['before']['core_hash']):
        raise ValueError('rollback_baseline_changed')
    if (core_hash(candidate/'core')!=r['verification']['core_hash']
        or b.tree_identity(candidate/'build/AGI.app')!=(postboot['candidate_identity'] if postboot else r['verification']['application_identity'])
        or b.fingerprint(b.manifest(candidate/'build/AGI.app'))!=r['verification']['application_manifest_sha256']):
        raise ValueError('rollback_candidate_changed')
    completed_at=record.get('rollback_completed_at')
    if completed_at is None:
        if record.get('interruption_recovered'):raise ValueError('rollback_completion_time_unbound')
        completed_at=record['at']
    if not isinstance(completed_at,(int,float)) or completed_at>time.time():raise ValueError('rollback_completion_time_invalid')
    entries=record.get('entries',[]);touched={};stages=set()
    if not entries:raise ValueError('rollback_entries_missing')
    for entry in entries:
        target,stage=_entry_scope(entry,live,app)
        if str(target) in touched or str(stage) in stages:raise ValueError('rollback_duplicate_entry')
        touched[str(target)]=entry;stages.add(str(stage))
        old=entry.get('old');new=entry.get('new')
        if old:
            bound=prepared['source_bindings'].get(str(target))
            expected_old=postboot['entry_tokens'][str(target)] if postboot else old
            original_token=bound['identity'].get(str(target),[])[:2] if bound else None
            declared=([expected_old[0],original_token[1]] if postboot and old==expected_old and original_token else original_token)
            if not bound or declared!=old or _token(target)!=expected_old:
                raise ValueError('rollback_original_identity_changed')
        elif target.exists() or target.is_symlink() or str(target) in prepared['source_bindings']:
            raise ValueError('rollback_added_original_changed')
        expected_new=postboot['entry_tokens'][str(stage)] if postboot else new
        if _token(stage)!=expected_new:raise ValueError('rollback_retained_candidate_changed')
        if target==app:
            if b.fingerprint(b.manifest(stage))!=r['verification']['application_manifest_sha256']:
                raise ValueError('rollback_retained_candidate_changed')
        else:
            source=candidate/'core'/target.relative_to(live)
            b.no_alias(source)
            if not source.is_file() or b.digest(stage)!=b.digest(source):
                raise ValueError('rollback_retained_candidate_changed')
        if _token(stage)!=expected_new or (old and _token(target)!=expected_old):raise ValueError('rollback_positions_changed')
    # Preserve exact former metadata bytes before exchange.json can be replaced.
    paths=[request,local,journal,local.with_suffix('.exchange.rolled-back.json'),local.with_suffix('.runtime.json')]
    snapshots={};absent=[]
    for path in paths:
        b.no_alias(path)
        if not path.exists():absent.append(str(path));continue
        if not path.is_file() or path.stat().st_nlink!=1 or path.stat().st_size>2*1024*1024:
            raise ValueError('rollback_evidence_invalid')
        data=metadata(path);snapshots[str(path)]={'sha256':hashlib.sha256(data).hexdigest(),'text':data.decode('utf-8')}
    if json.loads(snapshots[str(request)]['text'])!=r or json.loads(snapshots[str(local)]['text'])!=prepared:
        raise ValueError('rollback_evidence_changed')
    if json.loads(snapshots[str(journal)]['text'])!=record:raise ValueError('rollback_evidence_changed')
    identities={}
    for name,bound in prepared['source_bindings'].items():
        path=Path(name);b.no_alias(path);current=b.tree_identity(path)
        expected=postboot['original_expected_identities'][name] if postboot else bound['identity']
        if set(current)!=set(expected):raise ValueError('rollback_original_members_changed')
        for member,old in expected.items():
            now=current[member]
            root_ctime=(member==name and name in touched)
            if len(old)!=6 or len(now)!=6 or (now[:4]+now[5:]!=old[:4]+old[5:] if root_ctime else now!=old):
                raise ValueError('rollback_original_identity_changed')
            if root_ctime and now[4]!=old[4] and not old[4]<=now[4]<=int(completed_at*1_000_000_000):
                raise ValueError('rollback_ctime_outside_recorded_exchange')
        if b.fingerprint(b.manifest(path))!=bound['manifest_sha256']:
            raise ValueError('rollback_original_content_changed')
        identities[name]=current
    for path,row in snapshots.items():
        if metadata(Path(path))!=row['text'].encode('utf-8'):raise ValueError('rollback_evidence_changed')
    if any(Path(p).exists() or Path(p).is_symlink() for p in absent):raise ValueError('rollback_evidence_changed')
    if any(b.tree_identity(Path(name))!=value for name,value in identities.items()):
        raise ValueError('rollback_original_identity_changed')
    receipt={'schema':1,'status':'known_rollback_identity_reconciled','request':str(request),
             'commit_info':commit_info,'snapshots':snapshots,'absent':absent,'identities':identities,
             'database_restore_allowed':False,'allowed_difference':'journal_entry_root_ctime_only'}
    if postboot:receipt['postboot_revalidation']={k:postboot[k] for k in ('receipt','sha256')}
    history=local.parent/(local.stem+'.rollback-history');b.no_alias(history);private_directory(history)
    transaction={'job':request.stem,**{k:record[k] for k in ('live','application','candidate','previous_core_hash','expected_core_hash','commit_info','entries')}}
    if postboot:transaction['postboot_revalidation']={k:postboot[k] for k in ('receipt','sha256')}
    key=hashlib.sha256(packed(transaction).encode()).hexdigest()
    destination=history/(key+'.json');content=packed(receipt).encode()
    if len(content)>2*1024*1024:raise ValueError('rollback_history_too_large')
    def prior_receipt():
        b.no_alias(destination)
        if not destination.is_file() or destination.stat().st_nlink!=1:
            raise ValueError('rollback_reconciliation_conflict')
        previous=metadata(destination);saved=json.loads(previous)
        stable=('schema','status','request','commit_info','identities','database_restore_allowed','allowed_difference')+ (('postboot_revalidation',) if postboot else ())
        if (any(saved.get(k)!=receipt[k] for k in stable)
            or any(saved.get('snapshots',{}).get(str(p))!=snapshots[str(p)] for p in (request,local,journal))):
            raise ValueError('rollback_reconciliation_conflict')
        return previous
    if destination.exists() or destination.is_symlink():content=prior_receipt()
    else:
        import tempfile
        fd,name=tempfile.mkstemp(prefix='.'+key+'.',suffix='.pending',dir=history)
        temporary=Path(name)
        with os.fdopen(fd,'wb') as out:
            out.write(content);out.flush();os.fsync(out.fileno());fcntl.fcntl(out.fileno(),51)
        # An interrupted temporary metadata record is retained. It never
        # occupies the immutable final name or silently replaces old evidence.
        try:b.exclusive_move(temporary,destination)
        except FileExistsError:content=prior_receipt()
        fd=os.open(history,os.O_RDONLY|os.O_NOFOLLOW)
        try:os.fsync(fd)
        finally:os.close(fd)
        fd=os.open(destination,os.O_RDONLY|os.O_NOFOLLOW)
        try:fcntl.fcntl(fd,51)
        finally:os.close(fd)
    if metadata(destination)!=content:raise ValueError('rollback_reconciliation_readback_failed')
    # Only this transient checker uses the separately recorded identity. The
    # original prepared receipt and immutable external payload stay byte exact.
    effective={**prepared,'source_bindings':{name:{**bound,'identity':identities[name]}
               for name,bound in prepared['source_bindings'].items()}}
    return original_checker(effective)


def standalone_publish(request):
    r=json.loads(Path(request).read_text());conf=r['config'];live=Path(conf['live_core']);app=Path(conf['application']);candidate=Path(r['candidate']);recovery=Path(r['recovery'])
    from .recovery_storage import check_external,original_checker,RecoveryDeferred,backend,retained_guard
    from .store import Store
    local_receipt=Path(r['local_receipt'])
    def release_guard():
        with Store(live/'data/feedback',readonly=True) as current:
            _require_publication_active(current)
        check_external(recovery,local_receipt)
        with Store(live/'data/feedback',readonly=True) as current:
            _require_publication_active(current)
    service=f'gui/{os.getuid()}/app.personalai.monitor.core';plist=Path.home()/'Library/LaunchAgents/app.personalai.monitor.core.plist'
    loaded=subprocess.run(['/bin/launchctl','print',service],capture_output=True).returncode==0
    was_loaded=r.get('service_was_loaded',loaded)
    if 'service_was_loaded' not in r:
        r['service_was_loaded']=was_loaded;_journal_write(Path(request),r)
    commit_info={'repository':conf['repository'],'base':r['base'],'revision':r['verification']['revision']}
    journal=local_receipt.with_suffix('.exchange.json')
    if journal.exists():
        state=json.loads(journal.read_text())['state']
        head=run(['git','rev-parse','HEAD'],Path(conf['repository'])).stdout.strip()
        if state in ('exchanging','committing') and head==r['base'] and loaded:
            run(['/bin/launchctl','bootout',service]);loaded=False
    resumed=recover_exchange(journal,live,app,commit_info)
    try:release_guard();prepared=check_external(recovery,local_receipt)
    except (OSError,ValueError) as exc:
        # Pre-commit original-object restoration above also works without T9.
        # A committed pair stays together while waiting for external readback.
        paused=isinstance(exc,PermissionError) and str(exc)=='authorization_revoked'
        _journal_write(local_receipt.with_suffix('.pending.json'),{'status':'paused' if paused else 'deferred_t9','committed':bool(resumed),'reason':str(exc)[:160]})
        if was_loaded and not loaded:run(['/bin/launchctl','bootstrap',f'gui/{os.getuid()}',plist])
        if paused:raise
        return {'installed':bool(resumed),'status':'deferred_t9','runtime_restored':False,'original_cleanup_complete':False,'reason':str(exc)[:160]}
    b=backend()
    postboot=_postboot_rollback_context(request,r,prepared,journal)
    from . import recovery_storage
    executor={'release_controller_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'recovery_storage_sha256':hashlib.sha256(Path(recovery_storage.__file__).read_bytes()).hexdigest()}
    if postboot:executor['postboot_revalidation']={k:postboot[k] for k in ('receipt','sha256')}
    def candidate_guard(path,original):
        if b.fingerprint(b.manifest(path))!=r['verification']['application_manifest_sha256']:raise ValueError('candidate_application_changed')
        if original and b.tree_identity(path)!=(postboot['candidate_identity'] if postboot else r['verification']['application_identity']):raise ValueError('candidate_application_identity_changed')
    # All mutable contents were fixed and backed up by coordinator before dispatch.
    if not resumed and core_hash(live)!=json.loads((recovery/'manifest.json').read_text())['core_hash']:raise ValueError('live_drift_before_publish')
    changed=run(['git','diff','--name-only',r['base'],'HEAD'],candidate).stdout.splitlines()
    release_guard()
    if loaded:run(['/bin/launchctl','bootout',service])
    if not r.get('require_ui_absent',False):
        subprocess.run(['/usr/bin/pkill','-x','PersonalAIMonitor'],capture_output=True)
    time.sleep(1)
    def validate_live():
        run(['/usr/bin/codesign','--verify','--deep','--strict',app])
        info=plistlib.loads((app/'Contents/Info.plist').read_bytes())
        if info.get('PAMGitDirty') or info.get('PAMCoreCodeHash')!=r['verification']['core_hash'] or not r['verification']['revision'].startswith(info.get('PAMGitRevision','missing')):raise ValueError('installed_application_provenance_changed')
        candidate_guard(app,False)
        check=run([__import__('sys').executable,'-B','-m','activity_mvp.feedback.cli','status'],live,timeout=20)
        if json.loads(check.stdout).get('status')!='ok':raise ValueError('live_status_failed')
        repo=Path(conf['repository']);head=run(['git','rev-parse','HEAD'],repo).stdout.strip()
        if head==r['base']:run(['git','fetch',str(candidate),r['verification']['revision']],repo)
        elif not resumed or head!=r['verification']['revision']:raise ValueError('source_changed_during_publish')
    def commit_source():
        release_guard()
        repo=Path(conf['repository'])
        if not resumed and run(['git','rev-parse','HEAD'],repo).stdout.strip()!=r['base']:raise ValueError('source_changed_during_publish')
        run(['git','merge','--ff-only',r['verification']['revision']],repo)
        if run(['git','rev-parse','HEAD'],repo).stdout.strip()!=r['verification']['revision']:raise ValueError('committed_revision_mismatch')
    try:
        if r.get('require_ui_absent',False):
            from .project_repair import app_busy
            if app_busy('agi'): raise PermissionError('application_reopened_before_exchange')
        repo=Path(conf['repository'])
        expected_head=r['verification']['revision'] if resumed else r['base']
        if run(['git','rev-parse','HEAD'],repo).stdout.strip()!=expected_head:raise ValueError('source_changed_during_publish')
        files=[Path(file).relative_to('core') for file in changed if file.startswith('core/activity_mvp/')]
        original_guard=_rollback_original_guard(request,r,prepared,journal,postboot) if postboot else _rollback_original_guard(request,r,prepared,journal)
        pair=exchange_pair(candidate,live,app,recovery,files,r['verification']['core_hash'],validate_live,
                      release_guard,journal,commit_source,commit_info,cleanup=False,original_guard=original_guard,candidate_guard=candidate_guard)
        receipt={'installed':True,'revision':r['verification']['revision'],'core_hash':core_hash(live),'verified_at':time.time(),'recovery':str(recovery),'executor':executor,**pair}
        _journal_write(local_receipt.with_suffix('.installed.json'),{**receipt,'runtime_restored':False,'phase':'paired_committed_runtime_pending'})
    finally:
        errors=[]
        if was_loaded:
            try:run(['/bin/launchctl','bootstrap',f'gui/{os.getuid()}',plist])
            except subprocess.SubprocessError as exc:errors.append('bootstrap_failed:'+type(exc).__name__)
        try:run(['/usr/bin/open','-gj',app,'--args','--background-maintenance'])
        except subprocess.SubprocessError as exc:errors.append('application_open_failed:'+type(exc).__name__)
        _journal_write(local_receipt.with_suffix('.runtime.json'),{'runtime_restored':not errors,'errors':errors,'at':time.time(),'executor':executor})
    receipt.update(runtime_restored=not errors,runtime_errors=errors)
    if not errors:
        b=backend();release_guard()
        receipt['phase']='runtime_verified_cleanup_pending'
        b.atomic(recovery/'installed.json',receipt);b.atomic(local_receipt.with_suffix('.installed.json'),receipt)
        retained=retained_guard(recovery,local_receipt)
        def cleanup_guard():
            retained()
            with Store(live/'data/feedback',readonly=True) as current:
                _require_publication_active(current)
            if (recovery/'installed.json').read_bytes()!=local_receipt.with_suffix('.installed.json').read_bytes():raise ValueError('installed_receipt_mismatch')
        cleanup_guard();receipt['original_cleanup_complete']=finalize_exchange(journal,cleanup_guard,original_guard)
        receipt['phase']='complete';b.atomic(recovery/'installed.json',receipt);b.atomic(local_receipt.with_suffix('.installed.json'),receipt)
    return receipt

if __name__=='__main__':
    import sys
    if len(sys.argv)!=3 or sys.argv[1]!='--publish':raise SystemExit(2)
    print(json.dumps(_publish_with_diagnostic(sys.argv[2])))
