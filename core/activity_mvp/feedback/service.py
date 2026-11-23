"""One bounded controller tick. Persistent scheduling survives UI closure."""
from __future__ import annotations
import fcntl
import json
import sqlite3
import subprocess
import os
from pathlib import Path
import time
from datetime import datetime
from .store import Store,ZONE,day,packed,atomic
from . import sources,journal,model
from . import autonomy, health


def notification_summary(store):
    from .notifications import summary
    return summary(store)


def autonomous_installation_current(store, incident):
    """None means a genuine legacy incident; False never grants new verification."""
    if not autonomy.enabled(store): return None
    from .work import WorkQueue, POLICY
    work = next((row for row in WorkQueue(store).items('feedback') if row['reference'] == incident['id']), None)
    jobs = store.rows('SELECT * FROM jobs WHERE incident_id=? ORDER BY created DESC', (incident['id'],))
    try:
        decoded = [(job, json.loads(job['receipt'])) for job in jobs]
    except (ValueError, TypeError): return False
    if any(not isinstance(receipt, dict) for _, receipt in decoded): return False
    result = work.get('result', {}) if work else {}
    if not isinstance(result, dict): return False
    job_id = result.get('job')
    if not job_id and not any('work_revision' in receipt for _, receipt in decoded):
        return None
    if not work or work.get('policy') != POLICY or not isinstance(job_id, str): return False
    import re
    if not re.fullmatch(r'[a-f0-9]{24}', job_id): return False
    match = next(((job, receipt) for job, receipt in decoded if job['id'] == job_id), None)
    if not match: return False
    job, stored = match
    if job['state'] != 'observing' or job['revision'] != incident['revision']: return False
    if result.get('incident_revision') != incident['revision'] or not result.get('work_revision'): return False
    if not result.get('verification') or not result.get('installation'): return False
    for key in ('job', 'work_revision', 'incident_revision', 'verification', 'installation'):
        if stored.get(key) != result.get(key): return False
    from .project_repair import installation_receipt_path, installation_ready
    path = installation_receipt_path(store, work['project_id'], job_id)
    try:
        if path.is_symlink() or path.parent.is_symlink() or not path.is_file(): return False
        installation = json.loads(path.read_text())
        # The caller may add local routing metadata; compare the durable proof.
        saved = {k:v for k,v in result['installation'].items() if k != 'recovery_storage'}
        if installation != saved: return False
        return installation_ready(work, installation)
    except (OSError, ValueError, TypeError): return False


def review(store,now=None):
    store=filtered(store)
    now=time.time() if now is None else now
    rows=store.rows("SELECT * FROM incidents WHERE state IN ('observing','verified')")
    changes=[]
    for incident in rows:
        samples=store.rows('SELECT * FROM samples WHERE incident_id=? AND verified=1 AND observed>=?',(incident['id'],incident['installed_at'] or now))
        failed=[s for s in samples if s['outcome']=='failed']
        if failed:
            store.conn.execute('UPDATE incidents SET state=?,auto_disabled=1,revision=revision+1,updated=?,gap=? WHERE id=?',
                ('recurred',now,'已核实修复后的新失败，停止同问题自动安装。',incident['id']))
            (store.log('recurrence_investigation_required',incident=incident['id']) if autonomy.enabled(store) else store.decision(incident['id'],{'reason':'同一故障在修复后再次出现。','recommendation':'重新定位原因和扩大验证范围，需确认具体后续方案。',
                'consequence':'继续保留旧记录；不会自动反复覆盖安装。','choices':['investigate','defer','dismiss']}))
            # Keep recurrence visible alongside a pending decision.
            store.conn.execute("UPDATE incidents SET state='recurred' WHERE id=?",(incident['id'],))
            store.notify(incident['id'],'recurred');changes.append(incident['id'])
        elif incident['state']=='observing' and incident['installed_at'] and now-incident['installed_at']>=7*86400 and len({s['reference'] for s in samples if s['outcome']=='passed'})>=3:
            if autonomous_installation_current(store, incident) is False:
                store.conn.execute('UPDATE incidents SET gap=? WHERE id=?', ('自然样本已保留；安装版本或执行凭据已变化，不能沿用原验证。', incident['id']))
                store.log('natural_verification_installation_mismatch', incident=incident['id'])
                continue
            store.conn.execute("UPDATE incidents SET state='verified',verified_at=?,updated=?,gap=? WHERE id=?",(now,now,'已取得至少七天与三次相关自然使用验证；不代替用户评价。',incident['id']))
            if autonomy.enabled(store):
                try:
                    evidence_revision=autonomy.version(store,incident,autonomy.binding(store,incident))
                    verifications=store.get('work_verifications',{});verifications[incident['id']]=evidence_revision;store.set('work_verifications',verifications)
                except (OSError,ValueError,PermissionError):
                    store.log('work_verification_binding_unavailable',incident=incident['id'])
            store.notify(incident['id'],'verified');changes.append(incident['id'])
    return {'checked':len(rows),'changed':changes,'note':'无新批评或无样本不证明已解决。'}

def record_sample(store,incident,reference,outcome,observed,receipt):
    # Only the fixed verifier may produce a matching receipt in the private job tree.
    # A user/model assertion cannot use this function as an attestation shortcut.
    if outcome not in {'passed','failed'}:raise ValueError('sample_outcome')
    receipt=Path(receipt).resolve();base=(store.root/'repairs').resolve()
    if not receipt.is_relative_to(base) or receipt.is_symlink() or not receipt.is_file():raise PermissionError('sample_receipt_scope')
    data=json.loads(receipt.read_text())
    if data.get('incident')!=incident or data.get('reference')!=reference or data.get('outcome')!=outcome or data.get('origin')!='fixed_live_verifier':raise ValueError('unverified_sample')
    from .store import digest
    store.conn.execute('INSERT OR IGNORE INTO samples VALUES(?,?,?,?,?,1)',(digest([incident,reference]),incident,observed,outcome,reference))

def tick(root=None,*,analyze=True,repair=True,now=None):
    from ..local_checks import phase, LOCAL_SECONDS, LocalCheckTimeout
    now=time.time() if now is None else now
    with Store(root) as original_store:
        store=filtered(original_store)
        analysis_family=None
        descriptor=os.open(store.root/'controller.lock',os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)
        try:
            try:fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:return {'status':'busy'}
            if not store.active():return {'status':'paused'}
            # Migrate predecessor evidence before rotating analysis_last_family.
            store.set('analysis_health',health.unresolved_analysis(store))
            # Interrupted executions remain unknown; never automatically replay installation.
            for job in store.rows("SELECT * FROM jobs WHERE state IN ('building','deploying')"):
                store.conn.execute("UPDATE jobs SET state='unknown',updated=? WHERE id=?",(now,job['id']))
                store.conn.execute('UPDATE incidents SET auto_disabled=1 WHERE id=?',(job['incident_id'],))
                if autonomy.enabled(store):
                    store.log('interrupted_job_needs_reconciliation',job=job['id'])
                else:
                    store.decision(job['incident_id'],{'reason':'上次执行中断，结果待核对。','recommendation':'先核对安装与恢复记录，再决定后续。','choices':['investigate','defer']})
            with phase(store,'feedback_scan',LOCAL_SECONDS):
                results={'status':'ok','sources':sources.scan(store)}
            if autonomy.active(store):
                results['autonomy']=autonomy.synchronize(store)
                from .task_maintenance import tick as task_maintenance_tick
                try:
                    with phase(store,'task_maintenance'):
                        results['task_maintenance']=task_maintenance_tick(store)
                except (OSError,ValueError,PermissionError,RuntimeError,sqlite3.Error) as e:
                    results['task_maintenance']={'status':'blocked','reason':type(e).__name__}
                    store.set('task_maintenance_error',type(e).__name__)
                from .sync_center import tick as sync_center_tick
                try:
                    with phase(store,'sync_center'):
                        results['sync_center']=sync_center_tick(store,allow_repair=repair)
                except (OSError,ValueError,PermissionError,RuntimeError) as e:
                    results['sync_center']={'status':'blocked','reason':type(e).__name__}
                    store.set('sync_center_error',type(e).__name__)

                from .memory_autonomy import tick as memory_tick
                try:
                    with phase(store,'memory'):
                        results['memory']=memory_tick(store,limit=5)
                except (OSError,ValueError,PermissionError,RuntimeError,sqlite3.Error) as e:
                    # The worker uses durable statements and an external writer.
                    # A failure cannot certify that earlier actions rolled back.
                    # Preserve those records; this tick may continue independent
                    # read/notification work, but cannot start analysis or repairs.
                    results['memory']={'status':'blocked','reason':type(e).__name__,
                                       'partial_writes_possible':True,
                                       'diagnostic':health.exception_evidence(e)}
                    results['status']='partial'
                    analyze=False;repair=False
                    store.set('memory_autonomy_error',type(e).__name__)
                if results['memory'].get('error') or results['memory'].get('status') in {'blocked','unavailable','partial'}:
                    results['status']='partial'
                    analyze=False;repair=False
                    episode=store.get('memory_failure_episode',{}) or {'started':now,'failures':0}
                    episode['failures']+=1
                    store.set('memory_failure_episode',episode)
                    if episode['failures']==3:store.notify('memory-'+str(episode['started']),'memory_failure')
                else:
                    store.set('memory_failure_episode',{})
                    store.set('memory_autonomy_error','')
                # Fixed probes are private synthetic inputs, not new user sources.
                from . import proactive
                if results['status'] != 'partial':
                    try:
                        with phase(store,'proactive'):
                            results['proactive_discovery'] = proactive.discover(store)
                    except (OSError,ValueError,PermissionError,RuntimeError) as e:
                        results['proactive_discovery'] = {'status':'deferred','reason':type(e).__name__}
            if autonomy.enabled(store) and not autonomy.active(store): analyze=False;repair=False
            from ..reflection import service as reflection_service
            try:
                with phase(store,'reflection_scan',LOCAL_SECONDS):
                    results['reflection_sources']=reflection_service.scan(store,now)
            except (OSError,ValueError,sqlite3.Error) as e:
                results['reflection_sources']={'status':'unavailable','reason':type(e).__name__}
            if analyze:
                try:
                    if time.time()<store.get('analysis_retry_at',0):raise PermissionError('analysis_backoff')
                    store.set('analysis_last_result',{'status':'running','started':time.time()})
                    with phase(store,'reflection_ready',LOCAL_SECONDS):
                        reflection_due=reflection_service.ready(store)
                    with phase(store,'feedback_ready',LOCAL_SECONDS):
                        feedback_pending=bool(store.conn.execute("SELECT 1 FROM messages WHERE state='pending' LIMIT 1").fetchone())
                        feedback_due=bool(store.get('investigate_requests',[]) or feedback_pending or autonomy.active(store))
                    reflection_turn=reflection_due and (not feedback_due or store.get('analysis_last_family','feedback')!='reflection')
                    analysis_family='reflection' if reflection_turn else 'feedback'
                    # Rotate even when a selected family fails, while keeping
                    # the shared account backoff and unknown-call accounting.
                    store.set('analysis_last_family','reflection' if reflection_turn else 'feedback')
                    if reflection_turn:
                        results['analysis']=reflection_service.analyze(store)
                        store.set('analysis_last_family','reflection')
                    elif store.get('investigate_requests',[]): results['analysis']=model.investigate_requested(store)
                    elif feedback_pending: results['analysis']=model.analyze(store)
                    elif autonomy.active(store): results['analysis']=autonomy.investigate(store)
                    else: results['analysis']=model.analyze(store)
                    if not reflection_turn:store.set('analysis_last_family','feedback')
                    # An idle family or blocked unchanged input does not prove
                    # the shared model/account recovered. Keep its failure
                    # episode and existing delay until real analysis succeeds.
                    if results['analysis'].get('processed',0)>0:
                        store.set('last_error','');store.set('analysis_failures',0);store.set('analysis_retry_at',0)
                except (PermissionError,RuntimeError,TimeoutError,ValueError,OSError) as e:
                    results['analysis']={'status':'deferred','reason':str(e)[:100] if isinstance(e,PermissionError) else type(e).__name__}
                    if results['analysis']['reason'] not in {'daily_budget_exhausted','not_active','model_busy','analysis_backoff'}:
                        failures=store.get('analysis_failures',0)+1
                        store.set('analysis_failures',failures);store.set('last_error',results['analysis']['reason'])
                        delay=3600 if results['analysis']['reason']=='account_rate_limited' else (300,900,1800,3600)[min(failures-1,3)]
                        store.set('analysis_retry_at',time.time()+delay)
                        if failures==1:store.set('analysis_failure_episode',now)
                        if failures==3:store.notify('analysis-'+str(store.get('analysis_failure_episode')),'analysis_failure')
                health.record_analysis(store,analysis_family,results['analysis'])
                store.set('analysis_last_result',{**results['analysis'],'finished':time.time()})
            today=day(now);local=datetime.fromtimestamp(now,ZONE)
            if (local.hour,local.minute)>=(9,30) and store.get('last_daily')!=today:
                results['review']=review(store,now)
                store.conn.execute('INSERT OR IGNORE INTO reviews VALUES(?,?,?,?)',('daily-'+today,'daily',now,packed(results['review'])))
                store.set('last_daily',today)
            # Once due, catch up a missed weekly slot even after a reboot on Monday.
            from datetime import timedelta
            sunday=local.date()-timedelta(days=(local.weekday()+1)%7)
            if local.weekday()==6 and local.hour<18:sunday-=timedelta(days=7)
            week=sunday.strftime('%y%m%d')
            if store.get('last_weekly')!=week:
                journal.weekly(store,week);store.set('last_weekly',week)
                store.conn.execute('INSERT OR IGNORE INTO reviews VALUES(?,?,?,?)',('weekly-'+week,'weekly',now,packed({'source':'incident-ledger','unverified_not_closed':True})))
            if repair and (not autonomy.enabled(store) or autonomy.active(store)):
                from .project_repair import process_one
                try:results['repair']=process_one(store)
                except (PermissionError,RuntimeError,TimeoutError,ValueError,OSError) as e:results['repair']={'status':'deferred','reason':type(e).__name__}
                if autonomy.active(store) and results['repair'].get('status') in {'idle','not_verified','personal_source_unavailable','source_missing'}:
                    from . import proactive
                    try: results['proactive_repair'] = proactive.process_one(store)
                    except (PermissionError,RuntimeError,TimeoutError,ValueError,OSError) as e:
                        results['proactive_repair'] = {'status':'deferred','reason':str(e)[:100] if isinstance(e,PermissionError) else type(e).__name__}
            results['journal']=journal.render(store)
            from .probes import observe_journal
            results['natural_checks']=observe_journal(store)
            # Ordinary insights remain in their original records without
            # creating an obligation to open the control console.
            results['reflection_notifications']={'requested':0,'status':'quiet'}
            from .notifications import deliver
            try:
                results['notifications']=deliver(store)
                if results['notifications'].get('unknown'):results['status']='partial'
            except (OSError,ValueError,PermissionError,RuntimeError,sqlite3.Error) as error:
                results['notifications']={'status':'partial','reason':type(error).__name__}
                results['status']='partial'
            with phase(store,'finish'):
                results['source_health']=health.source_health(store,results['sources'])
                analysis_health=health.unresolved_analysis(store)
                stages={k:health.summary(v) for k,v in results.items() if isinstance(v,dict)}
                if analysis_health or any(health.incomplete(v) for v in results.values()):results['status']='partial'
                store.set('last_tick',now)
                store.set('last_tick_result',{'status':results['status'],'finished_at':time.time(),
                                             'memory':results.get('memory',{}),'stages':stages,
                                             'analysis_health':analysis_health})
            store.log('tick_completed',new_messages=sum(s.get('added',0) for s in results['sources']),status=results['status'])
            return results
        except LocalCheckTimeout as error:
            result={'status':'deferred','reason':'local_check_timeout','phase':error.phase}
            health.record_analysis(store,analysis_family,result)
            store.set('last_tick_result',{'status':'partial','finished_at':time.time(),
                                         'stages':{error.phase:health.summary(result)},
                                         'analysis_health':health.unresolved_analysis(store)})
            store.set('analysis_last_result',{**result,'finished':time.time()})
            store.log('local_check_deferred',phase=error.phase)
            # The interrupted scan is not a prerequisite for the existing
            # outbox. A persistently failing phase must not silence its notice.
            from .notifications import deliver
            try:notice=deliver(store)
            except (OSError,ValueError,PermissionError,RuntimeError,sqlite3.Error) as failure:
                notice={'status':'partial','reason':type(failure).__name__}
            recorded=store.get('last_tick_result',{})
            store.set('last_tick_result',{**recorded,'notifications':notice})
            return result
        finally:os.close(descriptor)

import sys
from pathlib import Path
from ..conversation_scope import filtered
