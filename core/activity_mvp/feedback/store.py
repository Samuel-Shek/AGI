"""Private, transactional feedback ledger; no writes to activity or memory tables."""
from __future__ import annotations
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ZONE = ZoneInfo('Asia/Shanghai')
ANALYSIS_BATCH_SIZE = 5
ANALYSIS_TIMEOUT = 900
STATES = {'investigating':'待查原因','ready':'待修复','decision':'待你决策','repairing':'修复中',
          'observing':'已安装待观察','verified':'已验证','recurred':'再次出现','insufficient':'证据不足','dismissed':'已排除','waiting_evidence':'等待证据','blocked':'系统核查中','resolved':'已处理','human_required':'需要你选择'}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY,platform TEXT NOT NULL,path TEXT NOT NULL,
 cursor TEXT NOT NULL DEFAULT '{}',status TEXT NOT NULL DEFAULT 'pending',detail TEXT NOT NULL DEFAULT '',updated REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY,source_id TEXT NOT NULL,session TEXT NOT NULL,
 observed REAL NOT NULL,quote TEXT NOT NULL,context TEXT NOT NULL,reference TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending',imported REAL NOT NULL,redacted INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS messages_pending ON messages(state,observed);
CREATE TABLE IF NOT EXISTS incidents(id TEXT PRIMARY KEY,title TEXT NOT NULL,category TEXT NOT NULL,
 state TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,created REAL NOT NULL,updated REAL NOT NULL,
 cause TEXT NOT NULL DEFAULT '',evidence TEXT NOT NULL DEFAULT '',fix TEXT NOT NULL DEFAULT '',gap TEXT NOT NULL DEFAULT '',
 project TEXT NOT NULL DEFAULT '',related TEXT NOT NULL DEFAULT '',auto_disabled INTEGER NOT NULL DEFAULT 0,
 installed_at REAL,verified_at REAL);
CREATE TABLE IF NOT EXISTS quotes(message_id TEXT PRIMARY KEY REFERENCES messages(id),incident_id TEXT NOT NULL REFERENCES incidents(id));
CREATE TABLE IF NOT EXISTS reviews(id TEXT PRIMARY KEY,kind TEXT NOT NULL,created REAL NOT NULL,result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions(id TEXT PRIMARY KEY,incident_id TEXT NOT NULL,revision INTEGER NOT NULL,
 proposal TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',choice TEXT,created REAL NOT NULL,decided REAL);
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,incident_id TEXT NOT NULL,revision INTEGER NOT NULL,
 state TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,receipt TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS budget(id TEXT PRIMARY KEY,day TEXT NOT NULL,kind TEXT NOT NULL,state TEXT NOT NULL,
 created REAL NOT NULL,usage TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS notifications(id TEXT PRIMARY KEY,incident_id TEXT NOT NULL,kind TEXT NOT NULL,
 created REAL NOT NULL,delivered INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS samples(id TEXT PRIMARY KEY,incident_id TEXT NOT NULL,observed REAL NOT NULL,
 outcome TEXT NOT NULL,reference TEXT NOT NULL,verified INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS exports(path TEXT PRIMARY KEY,body_sha TEXT NOT NULL,updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,event TEXT NOT NULL,created REAL NOT NULL,details TEXT NOT NULL);
'''

def packed(value): return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
def digest(value): return hashlib.sha256((value if isinstance(value,str) else packed(value)).encode()).hexdigest()
def utc(value=None): return datetime.fromtimestamp(time.time() if value is None else value,timezone.utc).isoformat()
def day(value=None): return datetime.fromtimestamp(time.time() if value is None else value,ZONE).strftime('%Y-%m-%d')
def default_root(): return Path(__file__).resolve().parents[2]/'data'/'feedback'

def private_directory(path):
    path=Path(path).absolute()
    for parent in [path,*path.parents]:
        if parent.exists() and parent.is_symlink(): raise PermissionError('symlink_directory')
    path.mkdir(parents=True,exist_ok=True,mode=0o700)
    st=path.stat()
    if st.st_uid!=os.getuid() or stat.S_IMODE(st.st_mode)!=0o700: raise PermissionError('private_directory_required')
    return path

def atomic(path,content):
    path=Path(path)
    if path.is_symlink() or (path.exists() and not path.is_file()): raise PermissionError('invalid_destination')
    fd,tmp=tempfile.mkstemp(prefix='.'+path.name+'.',dir=path.parent)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'w',encoding='utf-8') as f: f.write(content);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

class _ControlConnection:
    """A fresh read-only authority connection limited to pause/control metadata."""
    def __init__(self,raw):self.raw=raw
    def execute(self,sql,args=()):
        meta=sql=='SELECT value FROM meta WHERE key=?' and len(args)==1 and args[0] in {'config','paused','autonomy'}
        stop=sql=="SELECT COALESCE(MAX(id),0) FROM audit WHERE event IN ('owner_pause','owner_stop','owner_autonomy_pause')" and not args
        if not (meta or stop):raise PermissionError('control_metadata_only')
        return self.raw.execute(sql,args)
    def close(self):self.raw.close()

class Store:
    def __init__(self,root=None,readonly=False,*,controls_only=False):
        if controls_only and not readonly:raise ValueError('control_reader_must_be_readonly')
        self.root=Path(root or default_root()).absolute();self.readonly=readonly
        if not readonly: private_directory(self.root)
        self.path=self.root/'feedback.sqlite'
        if self.path.exists():
            s=self.path.lstat()
            if not stat.S_ISREG(s.st_mode) or s.st_uid!=os.getuid() or s.st_nlink!=1 or s.st_mode&0o077:raise PermissionError('private_database_required')
        elif not readonly:
            fd=os.open(self.path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(fd)
        if readonly:
            self.conn=sqlite3.connect(self.path.as_uri()+'?mode=ro',uri=True,timeout=.25)
            self.conn.execute('PRAGMA query_only=ON')
        else:
            self.conn=sqlite3.connect(self.path,timeout=2,isolation_level=None)
            self.conn.execute('PRAGMA foreign_keys=ON');self.conn.execute('PRAGMA synchronous=FULL')
            self.conn.executescript(SCHEMA)
            self.set_default('schema',1)
        self.conn.row_factory=sqlite3.Row
        if readonly:
            if controls_only:
                self.conn=_ControlConnection(self.conn)
            else:
                from ..presentation_scope import for_runtime,read_view
                self.conn=read_view(self.conn,for_runtime(self.root.parent),('incidents','messages','quotes','sources','decisions','notifications','samples','jobs','work_items'),personal_kind='feedback')
    def close(self):self.conn.close()
    def __enter__(self):return self
    def __exit__(self,*args):self.close()
    def rows(self,sql,args=()):return [dict(r) for r in self.conn.execute(sql,args)]
    def get(self,key,default=None):
        row=self.conn.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default
    def set(self,key,value):self.conn.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,packed(value)))
    def set_default(self,key,value):self.conn.execute('INSERT OR IGNORE INTO meta VALUES(?,?)',(key,packed(value)))
    @contextlib.contextmanager
    def tx(self):
        self.conn.execute('BEGIN IMMEDIATE')
        try:yield;self.conn.execute('COMMIT')
        except BaseException:self.conn.execute('ROLLBACK');raise
    def log(self,event,**details):self.conn.execute('INSERT INTO audit(event,created,details) VALUES(?,?,?)',(event,time.time(),packed(details)))
    def active(self):
        config=self.get('config',{})
        return config.get('enabled') is True and not self.get('paused',False) and not (self.root/'STOP').exists()
    def reserve(self,kind,now=None):
        if kind not in ('analysis','repair'):raise ValueError('budget_kind')
        now=time.time() if now is None else now
        with self.tx():
            if not self.active():raise PermissionError('paused_or_unconfigured')
            used=self.conn.execute('SELECT count(*) FROM budget WHERE day=? AND kind=? AND state!=?',(day(now),kind,'cancelled')).fetchone()[0]
            if kind=='repair' and used>=1:raise PermissionError('daily_budget_exhausted')
            key=digest([kind,now,os.urandom(12).hex()])[:24]
            self.conn.execute('INSERT INTO budget VALUES(?,?,?,?,?,?)',(key,day(now),kind,'reserved',now,'{}'))
            return key
    def settle(self,key,state,usage=None):
        if state not in {'settled','unknown','cancelled'}:raise ValueError('budget_state')
        row=self.conn.execute('SELECT usage FROM budget WHERE id=?',(key,)).fetchone()
        history=json.loads(row[0]).get('calls',[]) if row else []
        if usage:history.append(usage)
        self.conn.execute('UPDATE budget SET state=?,usage=? WHERE id=?',(state,packed({'calls':history}),key))
    def notify(self,incident,kind):
        row=self.conn.execute('SELECT revision FROM incidents WHERE id=?',(incident,)).fetchone()
        key=digest([incident,kind,row[0] if row else 0])
        self.conn.execute('INSERT OR IGNORE INTO notifications VALUES(?,?,?,?,0)',(key,incident,kind,time.time()))
    def decision(self,incident,proposal):
        row=self.conn.execute('SELECT revision FROM incidents WHERE id=?',(incident,)).fetchone()
        if not row:raise ValueError('incident_missing')
        key=digest([incident,row[0],proposal])[:24]
        self.conn.execute("UPDATE decisions SET state='superseded' WHERE incident_id=? AND state='pending' AND id!=?",(incident,key))
        self.conn.execute('INSERT OR IGNORE INTO decisions(id,incident_id,revision,proposal,created) VALUES(?,?,?,?,?)',(key,incident,row[0],packed(proposal),time.time()))
        self.conn.execute('UPDATE incidents SET state=? WHERE id=?',('decision',incident));self.notify(incident,'decision');return key
    def choose(self,key,revision,choice):
        if type(revision) is not int or revision<1:raise ValueError('decision_revision')
        if choice not in {'defer','dismiss','investigate','option_1','option_2','option_3'}:raise ValueError('unsupported_choice')
        with self.tx():
            r=self.conn.execute('SELECT d.*,i.revision current FROM decisions d JOIN incidents i ON i.id=d.incident_id WHERE d.id=?',(key,)).fetchone()
            if not r or r['state']!='pending' or r['current']!=revision or r['revision']!=revision:raise ValueError('decision_changed_refresh_required')
            if choice not in json.loads(r['proposal']).get('choices',['investigate','defer','dismiss']):raise ValueError('choice_not_offered')
            if choice.startswith('option_'):
                alternatives=json.loads(r['proposal']).get('alternatives',[])
                number=int(choice.split('_')[1])-1
                if number>=len(alternatives) or not isinstance(alternatives[number],str): raise ValueError('choice_not_offered')
            if choice=='defer':return {'status':'pending','id':key}
            self.conn.execute('UPDATE decisions SET state=?,choice=?,decided=? WHERE id=?',('answered',choice,time.time(),key))
            self.conn.execute('UPDATE incidents SET state=?,revision=revision+1,updated=? WHERE id=?',('dismissed' if choice=='dismiss' else 'investigating',time.time(),r['incident_id']))
            if choice=='investigate' or choice.startswith('option_'):
                requests=self.get('investigate_requests',[])
                if r['incident_id'] not in requests:requests.append(r['incident_id'])
                self.set('investigate_requests',requests)
            self.log('owner_decision',id=key,choice=choice)
        return {'status':'recorded','id':key}
    def snapshot(self):
        config=self.get('config',{})
        from .autonomy import snapshot as autonomy_snapshot
        autonomy=autonomy_snapshot(self)
        from .jev_ranking import status as ranking_status
        autonomy['jev_ranking']=ranking_status(self)
        from .memory_coverage import status as coverage_status
        autonomy['memory_coverage']=coverage_status()
        incidents=self.rows('SELECT * FROM incidents ORDER BY updated DESC LIMIT 200')
        for r in incidents:r['status_label']=STATES.get(r['state'],'待核对')
        return {'status':'ok','enabled':config.get('enabled',False),'paused':not self.active(),'autonomy':autonomy,
                'incidents':incidents,'sources':self.rows('SELECT id,platform,status,detail,updated FROM sources ORDER BY id'),
                'source_changed_messages':self.conn.execute("SELECT count(*) FROM messages WHERE state='source_changed'").fetchone()[0],
                'pending_messages':self.conn.execute("SELECT count(*) FROM messages WHERE state='pending'").fetchone()[0],
                'decisions':self.rows("SELECT * FROM decisions WHERE state='pending' ORDER BY created"),
                'budget':self.rows('SELECT kind,count(*) used FROM budget WHERE day=? AND state!=? GROUP BY kind',(day(),'cancelled')),
                'journal':config.get('journal',''),'last_tick':self.get('last_tick'), 'last_review':self.get('last_daily'),
                'last_weekly':self.get('last_weekly'),'last_error':self.get('last_error',''),
                'analysis_control':{'daily_limit':None,'batch_size':ANALYSIS_BATCH_SIZE,'timeout_seconds':ANALYSIS_TIMEOUT,
                                    'next_retry_at':self.get('analysis_retry_at',0),'last_result':self.get('analysis_last_result',{})},
                'repair_capability':self.get('repair_capability',{'enabled':False,'reason':'隔离与发布尚未验收'}),
                'model_capability':self.get('model_capability',{'enabled':False,'reason':'模型通道尚未验收'})}
