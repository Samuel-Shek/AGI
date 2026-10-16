"""Actual-delivery receipts and a policy-bound, personal-only report namespace."""
from datetime import datetime,date,timezone
import hashlib,json,os,re,uuid
from pathlib import Path
from activity_mvp.source_governance import personal_sources as scope
BASE=Path.home()/'Library/Application Support/AGI/reports'
RECEIPTS=Path.home()/'Library/Application Support/AGI/report-receipts'
JOBS={}  # No private schedule registrations are distributed.

def atomic(path,value):
    path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_name('.'+path.name+'.'+uuid.uuid4().hex)
    raw=json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2).encode();fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)

def extract_bound_context(output):
    decoder=json.JSONDecoder()
    for m in re.finditer(r'\{',output):
        try:p,_=decoder.raw_decode(output[m.start():])
        except ValueError:continue
        if isinstance(p,dict):
            try:scope.require_packet(p);return p
            except ValueError:pass
    return None

def record_actual_delivery(job,response,output,outcome,execution_id):
    kind=JOBS.get(job.get('id'))
    if not kind or outcome!='delivered' or not response.strip() or job.get('last_delivery_unverified') or not job.get('_personal_delivery_evidence'):return None
    if not all(evidence_complete(e) for e in job['_personal_delivery_evidence']):return None
    scope.require_packet(job)
    p=job.get('_personal_report_context')
    if not isinstance(p,dict):return None
    scope.require_packet(p)
    day=p.get('report_date') or p.get('trigger_report_end_date') or p.get('window',{}).get('end_date')
    if not day or date.fromisoformat(day)<scope.cutover_day():return None
    if p.get('report_failed') is True:kind='weekly_failure'
    if kind=='weekly' and not p.get('ok_to_write_weekly_report'):return None
    key=hashlib.sha256(str(execution_id).encode()).hexdigest();body=BASE/'飞书投递'/kind/(day+'-'+key[:16]+'.md');body.parent.mkdir(parents=True,exist_ok=True)
    receipt=RECEIPTS/(key+'.json')
    if receipt.exists():return receipt
    raw=response.encode('utf-8');body.write_bytes(raw);body.chmod(0o600)
    if kind=='weekly':
        y,w,_=date.fromisoformat(day).isocalendar();target=BASE/'周报'/f'{y}-W{w:02d}.md';target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
    atomic(receipt,{**scope.binding(),'kind':kind,'report_date':day,'window':p.get('window'),'execution_id':str(execution_id),'job_id':job['id'],'delivery_outcome':'delivered','delivered_at':datetime.now(timezone.utc).isoformat(),'delivery_evidence':job['_personal_delivery_evidence'],'body':str(body),'sha256':hashlib.sha256(raw).hexdigest()})
    return receipt

def load_delivered(kind='daily'):
    out=[]
    if not RECEIPTS.is_dir():return out
    for path in sorted(RECEIPTS.glob('*.json')):
        try:
            p=json.loads(path.read_text());scope.require_packet(p)
            if not p.get('delivery_evidence') or not all(evidence_complete(e) for e in p['delivery_evidence']) or p.get('kind')!=kind or p.get('delivery_outcome')!='delivered' or date.fromisoformat(p['report_date'])<scope.cutover_day():continue
            body=Path(p['body'])
            if not body.is_relative_to(BASE) or body.is_symlink() or any(x.is_symlink() for x in body.parents):continue
            raw=body.read_bytes()
            if hashlib.sha256(raw).hexdigest()!=p['sha256']:continue
            out.append({**p,'content':raw.decode('utf-8'),'source':'actual_delivery_receipt','session_id':p['execution_id'],'message_id':path.name,'message_timestamp':p['delivered_at'],'is_scheduled_daily_job':True})
        except (OSError,ValueError,KeyError,TypeError):continue
    return out

def evidence_complete(e):
    if not isinstance(e,dict) or not e.get('target'):return False
    return chunk_evidence_complete(e)

def chunk_evidence_complete(e):
    if not isinstance(e,dict):return False
    ids=e.get('message_ids');total=e.get('total_chunks');done=e.get('delivered_chunks')
    return isinstance(ids,list) and bool(ids) and all(isinstance(x,str) and x for x in ids) and len(set(ids))==len(ids) and type(total) is int and type(done) is int and total==done==len(ids)


def recovery_root():
    """An operator must configure and qualify their own recovery adapter."""
    raise PermissionError("report_recovery_adapter_not_configured")

def capture_context(job,output):
    if job.get('id') not in JOBS:return
    scope.require_packet(job)
    p=extract_bound_context(output)
    if p is not None:
        scope.require_packet(p);job['_personal_report_context']=p
    else:
        # Script-only fallbacks write only a small local policy-bound sidecar.
        sidecar=Path.home()/'.hermes/profiles/feishubot/cron/personal-v1/context'/str(job['id'])
        if sidecar.is_file():
            p=json.loads(sidecar.read_text());scope.require_packet(p)
            observed=datetime.fromisoformat(p['generated_at'])
            if abs((datetime.now(timezone.utc)-observed).total_seconds())<120:job['_personal_report_context']=p

def definition(job):
    keys=('id','name','prompt','script','cwd','workdir','no_agent','schedule','deliver','failure_deliver','origin','enabled_toolsets','skill','skills','context_from','monitor_script','monitor_url','interpreter','attach_to_session','model','provider','source_policy_version','source_cutover_at')
    return hashlib.sha256(json.dumps({k:job.get(k) for k in keys},sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()

def job_admitted(job):
    if not scope.company_stopped():return True
    try:
        registry=json.loads((scope.ROOT/'personal-jobs.json').read_text())
        return registry.get('owner_id')=='local-owner' and registry.get('jobs',{}).get(str(job.get('id')))==definition(job) and script_binding_matches(job,registry)
    except (OSError,ValueError,TypeError):return False

def before_send(job):
    return job_admitted(job) and report_window_admitted(job)



def reserve_send(job):
    import sqlite3
    kind=JOBS.get(job.get('id'))
    if not kind:return True
    p=job.get('_personal_report_context') or {}
    if p.get('report_failed'):kind+='-failure'
    day=p.get('trigger_report_end_date') or p.get('report_date') or (p.get('window') or {}).get('end_date')
    if not day:return False
    key=hashlib.sha256(json.dumps([scope.binding(),kind,day,job.get('_personal_canonical_targets')],sort_keys=True).encode()).hexdigest()
    dbpath=scope.ROOT/'personal-deliveries.sqlite';db=sqlite3.connect(dbpath,timeout=10);dbpath.chmod(0o600)
    try:
        db.execute('CREATE TABLE IF NOT EXISTS sends(logical_key TEXT PRIMARY KEY,execution_id TEXT NOT NULL,status TEXT NOT NULL,started_at TEXT NOT NULL)')
        db.execute('BEGIN IMMEDIATE')
        previous=db.execute('SELECT status FROM sends WHERE logical_key=?',(key,)).fetchone()
        if previous and previous[0]!='confirmed_not_sent':return False
        if previous:db.execute('DELETE FROM sends WHERE logical_key=?',(key,))
        execution=str(job.get('execution_id') or uuid.uuid4().hex)
        db.execute('INSERT INTO sends VALUES (?,?,?,?)',(key,execution,'in_flight_or_unknown',datetime.now(timezone.utc).isoformat()));db.commit()
        job['_personal_send_key']=key
        return True
    finally:db.close()


def settle_send(job,confirmed_not_sent=False):
    import sqlite3
    key=job.get('_personal_send_key')
    if not key:return
    state='delivered' if job.get('_personal_delivery_evidence') else 'confirmed_not_sent' if confirmed_not_sent else 'in_flight_or_unknown'
    with sqlite3.connect(scope.ROOT/'personal-deliveries.sqlite',timeout=10) as db:
        db.execute('UPDATE sends SET status=? WHERE logical_key=?',(state,key))

def admission_metadata(job):
    keys=('id','source_policy_version','source_cutover_at')
    meta={k:job.get(k) for k in keys};meta['definition_sha256']=definition(job)
    context=job.get('_personal_report_context') or {}
    allowed=('source_policy_version','source_cutover_at','report_date','trigger_report_end_date','window','ok_to_write_weekly_report','report_failed')
    meta['_personal_report_context']={k:context[k] for k in allowed if k in context}
    return meta

def metadata_admitted(meta):
    if not scope.company_stopped():return True
    try:
        registry=json.loads((scope.ROOT/'personal-jobs.json').read_text())
        if registry.get('owner_id')!='local-owner' or registry.get('jobs',{}).get(str(meta.get('id')))!=meta.get('definition_sha256'):return False
        if meta.get('id') in JOBS:return report_window_admitted(meta)
        return True
    except (OSError,ValueError,KeyError,TypeError):return False

def report_window_admitted(job):
    try:
        if job.get('id') not in JOBS:return True
        scope.require_packet(job);p=job.get('_personal_report_context');scope.require_packet(p)
        kind=JOBS[job['id']]
        day=p.get('trigger_report_end_date') or p.get('report_date') or (p.get('window') or {}).get('end_date')
        end=date.fromisoformat(day)
        if end<scope.cutover_day():return False
        if kind!='weekly' or p.get('report_failed') is True:return True
        from datetime import timedelta,time as dtime
        from zoneinfo import ZoneInfo
        if end.weekday()!=4:return False
        if datetime.now(timezone.utc)>=datetime.combine(end+timedelta(days=1),dtime(0,20),tzinfo=ZoneInfo('Asia/Shanghai')):return False
        start=max(end-timedelta(days=6),scope.cutover_day())
        w=p.get('window') or {}
        if w.get('start_date')!=start.isoformat() or w.get('end_date')!=end.isoformat():return False
        if any(p.get(k) and p[k]!=end.isoformat() for k in ('report_date','trigger_report_end_date')):return False
        if w.get('expected_dates') is not None and set(w['expected_dates'])!={(start+timedelta(days=i)).isoformat() for i in range((end-start).days+1)}:return False
        days={r['report_date'] for r in load_delivered('daily')}
        expected={(start+timedelta(days=i)).isoformat() for i in range((end-start).days+1)}
        return bool(p.get('ok_to_write_weekly_report')) and bool(expected) and expected<=days
    except (ValueError,TypeError,KeyError,AttributeError):return False

def before_model(job):
    return job_admitted(job) and report_window_admitted(job)


def script_binding_matches(job,registry):
    if job.get('id') in JOBS:
        for name,proof in registry.get('report_dependencies',{}).items():
            path=Path(name)
            if any(p.is_symlink() for p in (path,*path.parents)) or hashlib.sha256(path.read_bytes()).hexdigest()!=proof['sha256']:return False
    binding=registry.get('scripts',{}).get(str(job.get('id')))
    if not binding:return not job.get('script')
    path=Path(binding['path'])
    if any(p.is_symlink() for p in (path,*path.parents)):return False
    return hashlib.sha256(path.read_bytes()).hexdigest()==binding['sha256']

# Scoped transport context propagates across asyncio tasks and explicitly copied threads.
from contextvars import ContextVar
SEND_CONTEXT=ContextVar('personal_report_send_context',default=None)
LOADED_SHA256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
