"""Independent local derived records; never a shared-memory or repair queue."""
from __future__ import annotations
import json
import sqlite3
import stat
import os
import time
from pathlib import Path
from ..feedback.store import private_directory, packed, digest

SCHEMA = '''
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY,platform TEXT NOT NULL,path TEXT NOT NULL,cursor TEXT NOT NULL DEFAULT '{}',status TEXT NOT NULL DEFAULT 'pending',detail TEXT NOT NULL DEFAULT '{}',updated REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS turns(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE NOT NULL,context_id TEXT NOT NULL,source_id TEXT NOT NULL,session TEXT NOT NULL,role TEXT NOT NULL,observed REAL NOT NULL,text TEXT NOT NULL,reference TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS turns_context ON turns(context_id,seq);
CREATE TABLE IF NOT EXISTS contexts(id TEXT PRIMARY KEY,source_id TEXT NOT NULL,session TEXT NOT NULL,updated REAL NOT NULL,processed_seq INTEGER NOT NULL DEFAULT 0,summary TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS insights(id TEXT PRIMARY KEY,revision INTEGER NOT NULL DEFAULT 1,state TEXT NOT NULL,judgment TEXT NOT NULL,question TEXT NOT NULL,pattern TEXT NOT NULL,explanation TEXT NOT NULL,alternatives TEXT NOT NULL,refutation TEXT NOT NULL,distinguishing_question TEXT NOT NULL,evidence TEXT NOT NULL,fingerprint TEXT NOT NULL UNIQUE,created REAL NOT NULL,updated REAL NOT NULL,reason TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS history(seq INTEGER PRIMARY KEY AUTOINCREMENT,insight_id TEXT NOT NULL,event TEXT NOT NULL,created REAL NOT NULL,detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS feedback(id TEXT PRIMARY KEY,insight_id TEXT NOT NULL,revision INTEGER NOT NULL,choice TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS notices(id TEXT PRIMARY KEY,insight_id TEXT NOT NULL,revision INTEGER NOT NULL,delivered INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS calls(id TEXT PRIMARY KEY,state TEXT NOT NULL,created REAL NOT NULL,detail TEXT NOT NULL);
'''
LABELS = {'supported':'有较充分依据','preliminary':'仅有初步线索','not_found':'尚未发现'}

class Store:
    def __init__(self, root, readonly=False):
        self.root=Path(root).absolute(); self.readonly=readonly
        if not readonly: private_directory(self.root)
        else:
            for p in [self.root,*self.root.parents]:
                if p.is_symlink(): raise PermissionError('reflection_alias')
        self.path=self.root/'reflection.sqlite'
        if self.path.exists():
            s=self.path.lstat()
            if not stat.S_ISREG(s.st_mode) or s.st_uid!=os.getuid() or s.st_nlink!=1 or s.st_mode&0o077:
                raise PermissionError('reflection_private_database_required')
        elif not readonly:
            fd=os.open(self.path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
        self.conn=sqlite3.connect(self.path.as_uri()+('?mode=ro' if readonly else '?mode=rwc'),uri=True,timeout=.5,isolation_level=None)
        self.conn.row_factory=sqlite3.Row
        if readonly:
            self.conn.execute('PRAGMA query_only=ON')
            from ..presentation_scope import for_runtime,read_view
            self.conn=read_view(self.conn,for_runtime(self.root.parent),('insights','turns','retired_turns','contexts','retired_contexts','sources','notices','calls'),personal_kind='reflection')
        else:
            self.conn.execute('PRAGMA synchronous=FULL');self.conn.executescript(SCHEMA)
    def __enter__(self): return self
    def __exit__(self,*args): self.conn.close()
    def rows(self,sql,args=()):return [dict(r) for r in self.conn.execute(sql,args)]
    def get(self,key,default=None):
        r=self.conn.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
        return json.loads(r[0]) if r else default
    def set(self,key,value):self.conn.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,packed(value)))
    def enabled(self):return self.get('enabled',False) is True
    def event(self,insight,event,detail=''):
        self.conn.execute('INSERT INTO history(insight_id,event,created,detail) VALUES(?,?,?,?)',(insight,event,time.time(),detail))
    def withdraw(self,insight,reason):
        old=self.rows("SELECT * FROM insights WHERE id=? AND state='active'",(insight,))
        if old:self.event(insight,'prior_version',packed(old[0]))
        changed=self.conn.execute("UPDATE insights SET state='withdrawn',revision=revision+1,updated=?,reason=? WHERE id=? AND state='active'",(time.time(),reason,insight)).rowcount
        if changed:self.event(insight,'withdrawn',reason)
    def response(self,feedback_active=True):
        from .sources import evidence_current,bind_read_check
        from .presentation import last_analysis, pipeline_status
        bind_read_check(self)
        items=[]
        for r in self.rows('SELECT * FROM insights ORDER BY updated DESC LIMIT 30'):
            evidence=json.loads(r['evidence'])
            checking=self.enabled() and feedback_active
            expired=any(e.get('observed',0)<time.time()-14*86400 for e in evidence)
            current=not expired and all('context_version' in e for e in evidence) and (all(evidence_current(self,e,require_context=True) for e in evidence) if checking else True)
            state=r['state'] if current else 'withdrawn'
            items.append({k:r[k] for k in ('id','revision','judgment','question','pattern','explanation','alternatives','refutation','distinguishing_question','created','updated','reason')} | {
                'state':state,'source_checked':checking,'judgment_label':LABELS[r['judgment']],
                'reason':r['reason'] if current else '来源已变化或不可核对，判断已撤下。',
                'evidence':[{'id':e['id'],'platform':e['platform'],'session':e['session'],'role':e['role'],'observed':e['observed'],'quote':e['quote'],'attribution':e.get('attribution','material')} for e in evidence] if current else []})
        pending=self.conn.execute('SELECT count(*) FROM contexts c WHERE EXISTS(SELECT 1 FROM turns t WHERE t.context_id=c.id AND t.seq>c.processed_seq AND t.observed>=?)',(time.time()-14*86400,)).fetchone()[0]
        latest=last_analysis(self,self.get('last_analysis',{}))
        return {'contract':'ReflectionSnapshotV1','status':'ok','enabled':self.enabled(),'paused':not feedback_active,
            'pipeline':pipeline_status(self,feedback_active,latest,pending),
            'lookback_days':14,'sources':[{k:r[k] for k in ('id','platform','status','updated')}|{'detail':json.loads(r['detail'])} for r in self.rows('SELECT * FROM sources ORDER BY id')],
            'pending_contexts':pending,'last_scan':self.get('last_scan'), 'last_analysis':latest,
            'last_error':self.get('last_error',''),'coverage_gap':'；'.join(x for x in (self.get('coverage_gap',''),self.get('suppression_gap',''),self.get('replay_gap','')) if x),'insights':items,'calls':self.rows('SELECT state,count(*) AS count FROM calls GROUP BY state'),
            'benefit_status':'至少七天与三次独立自然使用案例后另行评估；觉得被说中不证明正确。'}

def record_feedback(store, payload):
    ident=payload.get('id');revision=payload.get('revision');choice=payload.get('choice')
    if choice not in {'helpful','inaccurate','mute'} or type(revision) is not int:raise ValueError('reflection_feedback_invalid')
    from ..feedback.control import gate
    with gate(store.get('feedback_root',store.root.parent/'feedback')):
        store.conn.execute('BEGIN IMMEDIATE')
        try:
            r=store.conn.execute('SELECT revision,state FROM insights WHERE id=?',(ident,)).fetchone()
            if not r or r[0]!=revision or r[1]!='active':raise ValueError('reflection_revision_changed')
            key=digest([ident,revision,choice])
            store.conn.execute('INSERT OR IGNORE INTO feedback VALUES(?,?,?,?,?)',(key,ident,revision,choice,time.time()))
            if choice in {'inaccurate','mute'}:
                store.event(ident,'prior_version',packed(store.rows('SELECT * FROM insights WHERE id=?',(ident,))[0]))
                state='rejected' if choice=='inaccurate' else 'muted'
                store.conn.execute('UPDATE insights SET state=?,revision=revision+1,updated=?,reason=? WHERE id=?',(state,time.time(),'用户指出判断不准确。' if choice=='inaccurate' else '用户要求暂不提醒这个问题。',ident))
            store.event(ident,'user_'+choice,'反馈不认证深层问题为事实，不写长期记忆。')
            store.conn.execute('COMMIT')
        except BaseException:store.conn.execute('ROLLBACK');raise
    return {'status':'ok','choice':choice}
