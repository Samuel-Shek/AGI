"""Fixed natural-use checks for the registered journal-format repair surface."""
import json
from pathlib import Path
import time
from .store import atomic, packed, digest, STATES

def observe_journal(store):
    store=filtered(store)
    from .sources import reference_is_current
    from .service import record_sample
    from .journal import START, END
    root=Path(store.get('config')['journal'])
    if company_stopped():root=root/'个人记录'
    jobs=store.rows("SELECT j.*,i.installed_at FROM jobs j JOIN incidents i ON i.id=j.incident_id WHERE j.state='observing' AND i.state='observing'")
    counted=0
    insufficient=0
    for job in jobs:
        try:
            receipt=json.loads(job['receipt'])
            rounds=receipt.get('rounds',[]) if isinstance(receipt,dict) else []
            changed=rounds[-1].get('changed') if isinstance(rounds,list) and rounds and isinstance(rounds[-1],dict) else None
            if not isinstance(changed,list) or not all(isinstance(p,str) for p in changed):
                raise ValueError('changed_missing')
        except (TypeError,ValueError):
            insufficient+=1
            store.log('natural_probe_evidence_insufficient', job=job['id'], reason='invalid_or_legacy_receipt')
            continue
        # Parser changes need a fault-specific live oracle; do not label
        # unrelated journal success as proof of a parser repair.
        if changed!=['core/activity_mvp/feedback/journal.py']:continue
        messages=store.rows("SELECT m.*,q.incident_id FROM messages m JOIN quotes q ON m.id=q.message_id WHERE m.observed>? AND m.source_id!='seed' ORDER BY m.observed LIMIT 60",(job['installed_at'],))
        for message in messages:
            reference=message['id']
            if store.rows('SELECT id FROM samples WHERE incident_id=? AND reference=?',(job['incident_id'],reference)):continue
            if not reference_is_current(message):continue
            matches=list((root/'事件').glob('*_'+message['incident_id'][:6]+'_*.md'))
            if len(matches)!=1 or matches[0].is_symlink():continue
            path=matches[0];body=path.read_text()
            exports=store.rows('SELECT body_sha FROM exports WHERE path=?',(str(path),))
            if not exports or body.count(START)!=1 or body.count(END)!=1:continue
            managed=body.split(START,1)[1].split(END,1)[0].strip()
            if digest(managed)!=exports[0]['body_sha']:continue  # human edits are not failures
            required=['## 用户原话','## 背景与依据','## 原因判断','## 实际改进','## 验证与剩余问题']
            complete=all(part in managed for part in required)
            complete &= all('> '+line in managed for line in message['quote'].splitlines())
            result={'origin':'fixed_live_verifier','incident':job['incident_id'],'reference':reference,
                    'outcome':'passed' if complete else 'failed','observed':time.time(),
                    'scope':'journal-format-natural-export','source_hash':digest(message['reference']),
                    'export_hash':digest(managed)}
            target=store.root/'repairs'/job['id']/('natural-'+reference+'.json')
            if not target.parent.is_dir():continue
            atomic(target,packed(result))
            record_sample(store,job['incident_id'],reference,result['outcome'],result['observed'],target)
            counted+=1
    return {'new_natural_samples':counted, 'insufficient_evidence':insufficient}

import sys
from ..conversation_scope import filtered
from activity_mvp.source_governance.personal_sources import company_stopped
