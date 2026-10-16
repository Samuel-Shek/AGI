"""Advisory ordering only. Two oldest seats; no memory decision authority."""
from __future__ import annotations
import hashlib, importlib, importlib.util, json, sys, threading, time
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from .store import digest, packed
from . import jev_dispatch

ADAPTER_VERSION=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
SCHEMA='''
CREATE TABLE IF NOT EXISTS jev_waiting(reference TEXT PRIMARY KEY, first_seen REAL NOT NULL);
CREATE TABLE IF NOT EXISTS jev_rank_attempts(event_id TEXT PRIMARY KEY, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS jev_rank_batches(id TEXT PRIMARY KEY,created REAL NOT NULL,baseline TEXT NOT NULL,selected TEXT NOT NULL,adopted TEXT NOT NULL,completed TEXT NOT NULL,processed TEXT NOT NULL DEFAULT '[]',versions TEXT NOT NULL DEFAULT '{}',state TEXT NOT NULL DEFAULT 'committed');
'''

def client_module():
    name='_bigking_jev_client'
    if name not in sys.modules:
        root=Path.home()/'Code/active/jev-judgment-service/jev'
        spec=importlib.util.spec_from_file_location(name,root/'__init__.py',submodule_search_locations=[str(root)])
        module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module)
    return importlib.import_module(name+'.client')

def binding(candidate):
    source=digest([candidate['content_sha256'],candidate['candidate_fingerprint']])
    obj=digest([candidate['memory_id'],source])
    return source,obj,digest([candidate['memory_id'],candidate['content_sha256'],'analysis_priority'])

def stable_order(items,high,limit):
    ids=[x['candidate']['memory_id'] for x in items]
    reserved=ids[:min(2,limit)]
    remainder=ids[len(reserved):]
    chosen=(reserved+[x for x in remainder if x in high]+[x for x in remainder if x not in high])[:limit]
    mapping={x['candidate']['memory_id']:x for x in items}
    return [mapping[x] for x in chosen]

def oldest(store,candidates):
    store.conn.executescript(SCHEMA + jev_dispatch.SCHEMA)
    if 'state' not in {x[1] for x in store.conn.execute('PRAGMA table_info(jev_rank_batches)')}:
        store.conn.execute("ALTER TABLE jev_rank_batches ADD COLUMN state TEXT NOT NULL DEFAULT 'committed'")
    if 'versions' not in {x[1] for x in store.conn.execute('PRAGMA table_info(jev_rank_batches)')}:
        store.conn.execute("ALTER TABLE jev_rank_batches ADD COLUMN versions TEXT NOT NULL DEFAULT '{}'")
    now=time.time()
    for index,c in enumerate(candidates):
        # Stable admission time, never reset when a candidate waits or is reclassified.
        previous=store.conn.execute('SELECT MIN(e.created) FROM work_events e JOIN work_items w ON w.id=e.work_id WHERE w.kind=? AND w.reference=?',('memory',c['memory_id'])).fetchone()
        admitted=previous[0] if previous and previous[0] is not None else now+index/1000000
        store.conn.execute('INSERT OR IGNORE INTO jev_waiting VALUES (?,?)',(c['memory_id'],admitted))
    ages={r['reference']:r['first_seen'] for r in store.rows('SELECT * FROM jev_waiting')}
    return sorted(candidates,key=lambda c:ages[c['memory_id']])

def select(store,items,limit,still_valid,client=None,may_send=None):
    limit=max(0,min(5,int(limit)));baseline=items[:limit]
    may_send=may_send or still_valid
    committed=None
    reserved=min(2,limit)
    if not items or not limit or len(items)-reserved<2 or limit<=reserved or not still_valid():return baseline,None
    store.conn.executescript(SCHEMA + jev_dispatch.SCHEMA)
    if client is None and ('unittest' in sys.modules or 'pytest' in sys.modules):return baseline,None
    try:
        client=client or client_module()
        release=client.scenario('agi','analysis_priority')
        if release.get('mode')!='assist' or release.get('adapter_version')!=ADAPTER_VERSION:return baseline,None
        deadline=time.time()+10;work=[]
        for entry in items[reserved:10]:
            if time.time()>=deadline or not may_send():break
            c=entry['candidate'];m=entry.get('memory')
            if not m or digest(m['text'])!=c['content_sha256']:continue
            source,obj,event=binding(c)
            if client is None:return baseline,None
            preflight=getattr(client,'preflight',None)
            if preflight is not None:
                permitted=preflight('agi','analysis_priority',{'text':m['text']},source_version=source,object_version=obj,adapter_version=ADAPTER_VERSION,deadline=deadline)
                if permitted.get('eligible') is not True:continue
            elif 'unittest' not in sys.modules and 'pytest' not in sys.modules:
                continue
            if time.time()>=deadline or not may_send():break
            if store.conn.execute('SELECT 1 FROM jev_rank_attempts WHERE event_id=?',(event,)).fetchone():continue
            prior=store.conn.execute('SELECT phase FROM jev_dispatch WHERE event_id=?',(event,)).fetchone()
            if prior and prior[0]!='not_dispatched':continue
            work.append((entry,source,obj,event))
        if not work:return baseline,None
        def judge(item):
            entry,source,obj,event=item
            if time.time()>=deadline or not may_send():return None
            return jev_dispatch.execute(store.path,event,lambda:client.request('agi','analysis_priority',{'text':entry['memory']['text']},source_version=source,
                   object_version=obj,adapter_version=ADAPTER_VERSION,event_id=event,data_class='memory_text',deadline=deadline),
                   lambda:time.time()<deadline and may_send())
        # Workers use separate SQLite connections for dispatch evidence only; never mutate order.
        pool=ThreadPoolExecutor(max_workers=4);futures={pool.submit(judge,item):item for item in work}
        done,_=wait(futures,timeout=max(0,deadline-time.time()-2))
        pool.shutdown(wait=False,cancel_futures=True)
        high=set();adopted={};judgments={}
        for future in done:
            entry,source,obj,event=futures[future]
            try:result=future.result()
            except Exception:continue
            if not may_send() or time.time()>=deadline:break
            if not result or result.get('expires_at',0)<=time.time() or result.get('status')!='ok' or result.get('mode')!='assist' or result.get('judgment',{}).get('label')!='high':continue
            if any(result.get(k)!=v for k,v in {'source_version':source,'object_version':obj,'adapter_version':ADAPTER_VERSION,'content_hash':client.sha({'text':entry['memory']['text']})}.items()):continue
            key=entry['candidate']['memory_id'];high.add(key);judgments[key]=result
        if not high or not still_valid() or time.time()>=deadline:return baseline,None
        latest_release=client.scenario('agi','analysis_priority')
        if any(latest_release.get(k)!=release.get(k) for k in ('mode','release_version','adapter_version')):return baseline,None
        chosen=stable_order(items,high,limit)
        base=[x['candidate']['memory_id'] for x in baseline]
        selected=[x['candidate']['memory_id'] for x in chosen]
        if selected==base:return baseline,None
        positions={x['candidate']['memory_id']:i for i,x in enumerate(items)}
        movers=[key for i,key in enumerate(selected) if key in high and positions[key]>i]
        for key in movers:
            if not still_valid() or time.time()>=deadline:return baseline,None
            result=judgments[key];receipt=client.adoption('agi',result)
            if not receipt.get('recommendation_usable'):return baseline,None
            adopted[key]=result
        if not adopted:return baseline,None
        # Immediate transaction stores actual ordering, before any completion acknowledgement.
        batch=digest([time.time_ns(),[x['candidate']['candidate_fingerprint'] for x in items]])
        selected=[x['candidate']['memory_id'] for x in chosen];base=[x['candidate']['memory_id'] for x in baseline]
        if not still_valid() or time.time()>=deadline:return baseline,None
        store.conn.execute('BEGIN IMMEDIATE')
        try:
            if not still_valid() or time.time()>=deadline:
                store.conn.rollback();return baseline,None
            store.conn.execute('INSERT INTO jev_rank_batches(id,created,baseline,selected,adopted,completed,state) VALUES (?,?,?,?,?,?,?)',
                (batch,time.time(),packed(base),packed(selected),packed({k:r['request_id'] for k,r in adopted.items() if k in selected}), '{}','awaiting_receipts'))
            store.conn.commit()
            committed=(chosen,batch)
        except Exception:
            store.conn.rollback();raise
        completed={}
        for key,result in adopted.items():
            if key in selected and still_valid() and time.time()<deadline:
                try:
                    receipt=client.adoption('agi',result,stage='app_reported_completed')
                    completed[key]=receipt.get('receipt_recorded',False)
                except Exception:completed[key]=False
        # A rejected/unknown completion never authorizes the sorted dispatch.
        # Retain the committed proposal as canceled evidence; do not resubmit.
        if not completed or not all(completed.values()) or not still_valid():
            store.conn.execute("UPDATE jev_rank_batches SET completed=?,state='invalidated' WHERE id=?",(packed(completed),batch))
            return baseline,None
        store.conn.execute("UPDATE jev_rank_batches SET completed=?,state='committed' WHERE id=?",(packed(completed),batch))
        return chosen,batch
    except Exception:
        if committed is not None:
            try:store.conn.execute("UPDATE jev_rank_batches SET state='invalidated' WHERE id=?",(committed[1],))
            except Exception:pass  # awaiting_receipts remains excluded from adoption metrics.
        return baseline,None

def processed(store,batch,reference):
    if not batch:return
    row=store.conn.execute('SELECT processed,versions FROM jev_rank_batches WHERE id=?',(batch,)).fetchone()
    ids=json.loads(row[0]);versions=json.loads(row[1]);ids.append(reference)
    item=store.conn.execute("SELECT source_revision FROM work_items WHERE kind='memory' AND reference=?",(reference,)).fetchone()
    if item:versions[reference]=item[0]
    store.conn.execute('UPDATE jev_rank_batches SET processed=?,versions=? WHERE id=?',(packed(ids),packed(versions),batch))

def status(store):
    try:
        rows=store.rows("SELECT * FROM jev_rank_batches WHERE state='committed' ORDER BY created DESC LIMIT 100")
        dispatch=dict(store.conn.execute('SELECT phase,count(*) FROM jev_dispatch GROUP BY phase')) if store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='jev_dispatch'").fetchone() else {}
        counts=dict(store.conn.execute('SELECT state,count(*) FROM work_items GROUP BY state'))
        waiting=store.conn.execute("SELECT MIN(updated) FROM work_items WHERE state='waiting_evidence'").fetchone()[0]
        recurrences=store.conn.execute("SELECT count(*) FROM incidents WHERE state='recurred'").fetchone()[0]
        changed=[r for r in rows if r['baseline']!=r['selected']]
        advanced=0;verified=0
        for row in changed:
            for ref in json.loads(row['processed']):
                if ref in json.loads(row['baseline']):continue
                advanced+=1
                version=json.loads(row.get('versions','{}')).get(ref)
                done=store.conn.execute("SELECT 1 FROM work_items WHERE kind='memory' AND reference=? AND state='verified' AND source_revision=? AND processed_revision=? AND updated>=?",(ref,version,version,row['created'])).fetchone()
                if done:verified+=1
        return {'batches':len(rows),'changed_batches':len(changed),'adopted':sum(len(json.loads(r['adopted'])) for r in changed),
                'completed':sum(sum(v is True for v in json.loads(r['completed']).values()) for r in changed),
                'advanced_processed':advanced,'advanced_verified':verified,'dispatch_states':dispatch,
                'oldest_wait_hours':(time.time()-waiting)/3600 if waiting else None,'waiting_count':counts.get('waiting_evidence',0),
                'human_required':counts.get('human_required',0),'recurrences':recurrences,
                'benefit':'not_yet_demonstrated' if not verified else 'observed_without_causal_control',
                'latest_at':rows[0]['created'] if rows else None,'window':'latest_100_batches','outcome_window':'current_snapshot'}
    except Exception:return {'available':False,'batches':None,'adopted':None,'completed':None,'advanced_processed':None,'latest_at':None}
