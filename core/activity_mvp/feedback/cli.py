"""Fixed local operator commands; no arbitrary model command dispatch."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import time
from .control import gate
from .store import Store,default_root,packed,atomic,private_directory


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['status','detail','configure','tick','pause','resume','stop','decision','notifications','notifications-ack','seed','verify-isolation','verify-model','accept-release','proactive-enable','autonomy-enable','autonomy-pause','autonomy-resume','autonomy-sync','autonomy-investigate'])
    parser.add_argument('--root',type=Path);parser.add_argument('--payload-stdin',action='store_true');parser.add_argument('--local-only',action='store_true')
    args=parser.parse_args();root=args.root or default_root()
    payload=json.loads(sys.stdin.read(32769)) if args.payload_stdin else {}
    if not isinstance(payload,dict):raise ValueError('payload_object_required')
    if args.action=='status':
        try:
            with Store(root,readonly=True) as store:
                from .presentation import progress
                from ..presentation_scope import project_for_runtime
                return project_for_runtime({**store.snapshot(), 'progress': progress(store)}, root.parent)
        except FileNotFoundError:return {'status':'not_configured','enabled':False,'paused':True,'incidents':[],'sources':[],'decisions':[]}
        except __import__('sqlite3').OperationalError:return {'status':'unavailable','enabled':False,'paused':True,'reason':'ledger_read_failed','incidents':[],'sources':[],'decisions':[]}
    if args.action=='detail':
        with Store(root,readonly=True) as store:
            rows=store.rows('SELECT * FROM incidents WHERE id=?',(payload.get('id'),))
            if not rows:raise ValueError('incident_missing')
            from ..presentation_scope import project_for_runtime
            return project_for_runtime({'status':'ok','incident':rows[0],'quotes':store.rows('SELECT m.quote,m.observed,m.source_id,m.redacted FROM messages m JOIN quotes q ON q.message_id=m.id WHERE q.incident_id=? ORDER BY m.observed',(payload['id'],)),
                'decisions':store.rows("SELECT * FROM decisions WHERE incident_id=? AND state='pending'",(payload['id'],))}, root.parent)
    if args.action=='tick':
        from .service import tick
        return tick(root,analyze=not args.local_only,repair=not args.local_only)
    with Store(root) as store:
        if args.action=='proactive-enable':
            from .proactive import enable
            if set(payload)!={'authorization'}:raise PermissionError('proactive_authorization_required')
            return enable(store,payload['authorization'])
        if args.action.startswith('autonomy-'):
            from . import autonomy
            from .work import POLICY
            if args.action=='autonomy-enable':
                if payload!={'authorization':POLICY}: raise PermissionError('explicit_autonomy_plan_required')
                return autonomy.activate(store)
            if not autonomy.enabled(store): raise PermissionError('autonomy_not_configured')
            if args.action in {'autonomy-pause','autonomy-resume'}:
                with gate(store.root):
                    setting=store.get('autonomy');setting['paused']=args.action=='autonomy-pause';store.set('autonomy',setting)
                    store.log('owner_'+args.action.replace('-','_'))
                return {'status':'paused' if setting['paused'] else 'resumed'}
            if not autonomy.active(store):return {'status':'paused'}
            if args.action=='autonomy-sync': return autonomy.synchronize(store)
            return autonomy.investigate(store)
        if args.action=='configure':
            if set(payload)!={'repository','journal','seed','authorization','codex_binary','python'}:raise ValueError('configuration_fields')
            if payload['authorization']!='owner-approved-plan-20260909':raise PermissionError('explicit_plan_authorization_required')
            if store.get('config'):raise ValueError('already_configured_use_controls')
            repo=Path(payload['repository']).resolve();journal=Path(payload['journal']).absolute()
            if not (repo/'.git').exists() or not (repo/'core/activity_mvp').is_dir():raise ValueError('repository_missing')
            if not journal.name=='批评与改进':raise ValueError('journal_scope')
            config={**payload,'repository':str(repo),'journal':str(journal),'enabled':True,'enabled_at':time.time(),
                'since':time.time()-30*86400,'live_core':str(Path.home()/'Library/Application Support/Personal AI Monitor/core'),
                'application':'/Applications/AGI.app','hermes_owner_ids':{},'analysis_limit':None,'repair_limit':1,'revision_limit':2}
            store.set('config',config);store.set('paused',False)
            from .sources import register
            from .journal import import_seed,render
            register(store)
            result=import_seed(store,payload['seed']);render(store)
            store.log('owner_plan_enabled',scope='local-three-platforms',automatic_repair='agi-allowlist',history_days=30)
            return {'status':'configured',**result}
        if args.action=='pause':
            with gate(store.root):store.set('paused',True);store.log('owner_pause')
            return {'status':'paused'}
        if args.action=='stop':
            with gate(store.root):atomic(store.root/'STOP','stopped\n');store.set('paused',True);store.log('owner_stop')
            return {'status':'stopped'}
        if args.action=='resume':
            if not store.get('config',{}).get('enabled'):raise PermissionError('not_configured')
            with gate(store.root):
                if (store.root/'STOP').exists():(store.root/'STOP').unlink()
                store.set('paused',False);store.log('owner_resume')
            return {'status':'resumed'}
        if args.action=='decision':
            if set(payload)!={'id','revision','choice'}:raise ValueError('decision_fields')
            return store.choose(payload['id'],payload['revision'],payload['choice'])
        if args.action=='notifications':
            from .service import notification_summary
            return notification_summary(store)
        if args.action=='notifications-ack':
            ids=payload.get('ids',[])
            if len(ids)>20 or not all(isinstance(x,str) and len(x)==64 for x in ids):raise ValueError('notification_ids')
            for key in ids:store.conn.execute('UPDATE notifications SET delivered=1 WHERE id=?',(key,))
            return {'status':'recorded'}
        if args.action=='seed':
            from .journal import import_seed
            return import_seed(store,store.get('config')['seed'])
        if args.action=='verify-model':
            if not store.get('isolation',{}).get('passed'):raise PermissionError('isolation_not_verified')
            from .model import invoke
            store.set('model_capability',{'enabled':True,'reason':'通道核验中'})
            try:
                schema={'type':'object','properties':{'result':{'type':'string','enum':['ok']}},'required':['result'],'additionalProperties':False}
                result,key=invoke(store,'这是已授权通道的固定无私人材料测试。请只按结构返回 result=ok，不调用工具。',schema)
                if result!={'result':'ok'}:raise ValueError('model_contract_failed')
                store.set('model_capability',{'enabled':True,'reason':'现有 Codex 通道已验证 · 串行分批分析，不设本机每日次数上限','request':key})
                return {'status':'verified','request':key}
            except Exception:
                store.set('model_capability',{'enabled':False,'reason':'模型通道检查未通过'})
                raise
        if args.action=='accept-release':
            from .acceptance import activate
            if set(payload)!={'receipt'}:raise ValueError('receipt_fields')
            return activate(store,payload['receipt'])
        if args.action=='verify-isolation':
            from .isolation import verify
            return verify(store)
    raise ValueError('unknown_action')

if __name__=='__main__':
    try:print(json.dumps(main(),ensure_ascii=False))
    except Exception as e:
        print(json.dumps({'status':'rejected','reason':str(e)[:120] if isinstance(e,(ValueError,PermissionError)) else type(e).__name__},ensure_ascii=False));sys.exit(1)
