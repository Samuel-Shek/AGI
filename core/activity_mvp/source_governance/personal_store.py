"""Isolated incremental candidate; not installed and not a recovery copy."""
import re

class ReadOnlyConnection:
    def __init__(self, raw, views):
        self.raw = raw
        self.views = dict(views)
    def __getattr__(self, key):
        return getattr(self.raw, key)
    def execute(self, sql, args=()):
        leading = sql.lstrip()
        if leading.upper().startswith(('SELECT', 'WITH')):
            for table in self.views:
                sql = re.sub(r'\b(FROM|JOIN)\s+' + re.escape(table) + r'\b',
                             lambda match: match[1] + ' personal_' + table,
                             sql, flags=re.I)
            definitions = ','.join(
                'personal_' + table + ' AS (SELECT * FROM main.' + table
                + ' WHERE ' + predicate.replace('temp.personal_', 'personal_') + ')'
                for table, predicate in self.views.items())
            leading = sql.lstrip()
            if leading.upper().startswith('WITH RECURSIVE '):
                sql = 'WITH RECURSIVE ' + definitions + ',' + leading[len('WITH RECURSIVE '):]
            elif leading.upper().startswith('WITH '):
                sql = 'WITH ' + definitions + ',' + leading[len('WITH '):]
            else:
                sql = 'WITH ' + definitions + ' ' + sql
        return self.raw.execute(sql, args)

"""Filter automatic ledger reads using registered metadata; never delete history."""
import re,json
from activity_mvp.source_governance import personal_sources as scope
from pathlib import Path
import hashlib,os,time
LOADED_HELPERS={Path(__file__).name:hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),Path(scope.__file__).name:hashlib.sha256(Path(scope.__file__).read_bytes()).hexdigest()}

def runtime_receipt(root,kind):
 from activity_mvp.source_governance import personal_reports
 helpers={**LOADED_HELPERS,Path(personal_reports.__file__).name:personal_reports.LOADED_SHA256}
 p=Path(root)/'retirement-runtime.json'
 if p.exists():
  try:
   old=json.loads(p.read_text())
   if old.get('pid')==os.getpid() and old.get('loaded_helpers')==helpers:return old
  except (OSError,ValueError):pass
 value={'pid':os.getpid(),'kind':kind,'company_stopped':scope.company_stopped(),'loaded_helpers':helpers,'observed_at':time.time(),**scope.binding()}
 p.parent.mkdir(parents=True,exist_ok=True);temp=p.with_suffix('.'+str(os.getpid())+'.tmp')
 fd=os.open(temp,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
 with os.fdopen(fd,'w') as f:json.dump(value,f);f.flush();os.fsync(f.fileno())
 os.replace(temp,p);return value

def _quote(s):return "'"+str(s).replace("'","''")+"'"
class Connection:
 def __init__(self,raw,tables):self.raw=raw;self.tables=tables
 def __getattr__(self,k):return getattr(self.raw,k)
 def execute(self,sql,args=()):
  if sql.lstrip().upper().startswith(('SELECT','WITH')):
   for table in self.tables:sql=re.sub(r'\b(FROM|JOIN)\s+'+table+r'\b',lambda m:m[1]+' temp.personal_'+table,sql,flags=re.I)
  return self.raw.execute(sql,args)
class FilteredStore:
 def __init__(self,store,tables,readonly_views=None):self.original=store;self.conn=ReadOnlyConnection(store.conn,readonly_views) if readonly_views is not None else Connection(store.conn,tables)
 def __getattr__(self,k):return getattr(self.original,k)
 def rows(self,sql,args=()):return [dict(r) for r in self.conn.execute(sql,args)]
 def __delattr__(self,k):delattr(self.original,k)
 def __setattr__(self,k,v):
  if k in ('original','conn'):object.__setattr__(self,k,v)
  else:setattr(self.original,k,v)

def filtered(store,kind='feedback'):
 if not scope.company_stopped() or isinstance(store,FilteredStore):return store
 raw=store.conn;readonly=bool(raw.execute('PRAGMA query_only').fetchone()[0])
 if not readonly and getattr(store,'root',None):runtime_receipt(store.root,'agi-'+kind)
 raw=store.conn;tables={r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")};views={}
 def registered(session,path):
  if not isinstance(path,str) or not path or '\x00' in path or not Path(path).is_absolute():return 0
  try:return int(scope.admitted({'session_id':session,'path':path}))
  except (OSError,ValueError,TypeError,KeyError,AttributeError):return 0
 raw.create_function('personal_registered_metadata',2,registered)
 def registered_rows(table,alias=''):
  columns={r[1] for r in raw.execute('PRAGMA main.table_info('+table+')')}
  prefix=alias+'.' if alias else ''
  path="CASE WHEN json_valid("+prefix+"reference) THEN json_extract("+prefix+"reference,'$.path') END" if 'reference' in columns else 'NULL'
  return 'personal_registered_metadata('+prefix+'session,'+path+')=1'

 ids=','.join(_quote(x) for x in scope.policy()['personal_session_ids']) or "NULL"
 if kind=='reflection':
  views={'turns':f'session IN ({ids})','contexts':f'session IN ({ids})'}
  for table in ('retired_turns','retired_contexts'):
   if table in tables:views[table]=f'session IN ({ids})'
  views['insights']="json_valid(evidence) AND json_array_length(evidence)>0 AND NOT EXISTS(SELECT 1 FROM json_each(evidence) j WHERE json_extract(j.value,'$.id') IS NULL OR json_extract(j.value,'$.id') NOT IN (SELECT id FROM temp.personal_turns))"
  if 'retired_turns' in tables:views['insights']=views['insights'].replace('SELECT id FROM temp.personal_turns','SELECT id FROM temp.personal_turns UNION SELECT id FROM temp.personal_retired_turns')
  views['notices']='insight_id IN (SELECT id FROM temp.personal_insights)'
  views['calls']="json_valid(detail) AND json_type(detail,'$.input_ranges')='object' AND EXISTS(SELECT 1 FROM json_each(detail,'$.input_ranges')) AND NOT EXISTS(SELECT 1 FROM json_each(detail,'$.input_ranges') j WHERE j.key NOT IN (SELECT id FROM temp.personal_contexts))"
 else:
  views['messages']=f'session IN ({ids})'
  views['quotes']='message_id IN (SELECT id FROM temp.personal_messages)'
  views['incidents']="EXISTS(SELECT 1 FROM main.quotes q WHERE q.incident_id=incidents.id) AND NOT EXISTS(SELECT 1 FROM main.quotes q WHERE q.incident_id=incidents.id AND q.message_id NOT IN (SELECT id FROM temp.personal_messages))"
  for table in ('samples','decisions','notifications','jobs'):views[table]='incident_id IN (SELECT id FROM temp.personal_incidents)'
  if True:
   try:
    registry=json.loads((Path.home()/'.hermes/memory-review-center/state/autonomy-policy.json').read_text())
    memory_ids=[x for x in registry.get('bindings',{}) if scope.memory_admitted(x,{},registry)]
   except (OSError,ValueError,TypeError,KeyError,AttributeError):memory_ids=[]
   mids=','.join(_quote(x) for x in memory_ids) or 'NULL'
   views['work_items']=f"(kind='feedback' AND reference IN (SELECT id FROM temp.personal_incidents)) OR (kind='memory' AND reference IN ({mids})) OR kind IN ('memory_system','personal_system')"
 for table in ('turns','retired_turns','messages'):
  if table in views:views[table]+=' AND '+registered_rows(table)
 if kind=='reflection':
  for context_table in ('contexts','retired_contexts'):
   if context_table not in views:continue
   for turn_table in ('turns','retired_turns'):
    if turn_table in tables:
     views[context_table]+=' AND NOT EXISTS(SELECT 1 FROM main.'+turn_table+' t WHERE t.context_id='+context_table+'.id AND NOT ('+registered_rows(turn_table,'t')+'))'
  for table in ('turns','retired_turns'):
   if table in views:
    contexts='SELECT id FROM temp.personal_contexts'
    if table=='retired_turns' and 'retired_contexts' in views:contexts+=' UNION SELECT id FROM temp.personal_retired_contexts'
    views[table]+=' AND context_id IN ('+contexts+')'
 if readonly:return FilteredStore(store,list(views),views)
 for table,predicate in views.items():
  if True:
   raw.execute('DROP VIEW IF EXISTS temp.personal_'+table)
   raw.execute('CREATE TEMP VIEW personal_'+table+' AS SELECT * FROM main.'+table+' WHERE '+predicate)
 return FilteredStore(store,list(views))
