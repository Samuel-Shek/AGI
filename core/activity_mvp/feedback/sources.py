"""Bounded local adapters. Text is evidence, never a source of authority."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import pwd
import time
from datetime import datetime
from .store import digest,packed

import sys
from activity_mvp.source_governance import personal_sources as personal_scope

def source_metadata_admitted(session,path,source_id=None):
    """Fail closed on missing identity/path before touching source content."""
    if not isinstance(path,str) or not path or '\x00' in path or not Path(path).is_absolute():return False
    if source_id!='seed' and (not isinstance(session,str) or not session):return False
    try:
        if source_id=='seed':return personal_scope.admitted({'path':path})
        from ..conversation_scope import allowed
        return allowed(session, path, source_id)
    except (OSError,ValueError,TypeError,KeyError,AttributeError):return False

MAX_LINE=1024*1024
CREDENTIALS=re.compile(r"(?i)\b(?:sk-[a-z0-9_-]{12,}|(?:gh[pousr]|github_pat)_[a-z0-9_]{15,}|xox[baprs]-[a-z0-9-]{10,}|glpat-[a-z0-9_-]{10,}|AIza[a-z0-9_-]{25,}|(?:AKIA|ASIA)[A-Z0-9]{16}|eyJ[a-z0-9_-]+\.[a-z0-9_-]+\.[a-z0-9_-]+|(?:Bearer|Basic)\s+\S+)|(?:password|passwd|api[_ -]?key|client[_ -]?secret|secret|(?:access|refresh|id|oauth)[_ -]?token|token|authorization|验证码|密码)[\"']?\s*[:=：]\s*[\"']?(?:(?:Bearer|Basic)\s+)?[^\s,，;；\"']+|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
# Only failed reads are cached, for one scheduling interval. No cached content or
# successful validation can authorize use of a source. File changes invalidate it.
_SEED_READ_FAILURES={}
SIGNAL=re.compile(r'不对|不应该|怎么又|又.{0,18}(错|问|漏|失败)|还是.{0,18}(没|错|不行)|明明|根本|疏漏|你.{0,16}(没|不|错|漏|假|骂)|我说的是|我不是说|我已经说|不是让你|别再|不要再|请先看|自己.{0,12}(查|验收|核对|搞定)|字数太多|太[长慢啰嗦]|发到哪里|不满意|不符合|没体现|没有.{0,16}(完成|体现|依据|查|看|修复)|错了|重复.{0,10}(问|犯)|欠骂|胡说|胡编|瞎编|撒谎|主观能动|浪费|失望|搞错|why.*again|you.*(wrong|missed|forgot)|not what I (asked|meant)',re.I)
WRAPPER=re.compile(r'<(?:environment_context|hook_prompt|app-context|recommended_plugins|INSTRUCTIONS|external_codex_apps_writing_block_edits)\b',re.I)
SENSITIVE=[(re.compile(r'(?i)\b(?:sk-[a-z0-9_-]{12,}|(?:ghp|github_pat)_[a-z0-9_]{15,}|Bearer\s+\S+)'),'[凭据已遮盖]'),
 (re.compile(r'(?i)(?:password|api[_ -]?key|secret|access[_ -]?token|验证码|密码)\s*[:=：]\s*[^\s,，;；]+'),'[敏感值已遮盖]'),
 (re.compile(r'(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b'),'[邮箱已遮盖]'),
 (re.compile(r'(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)'),'[联系方式已遮盖]')]

def clean(text):
    old=text
    text=CREDENTIALS.sub('[凭据已遮盖]',text)
    for rule,replacement in SENSITIVE:text=rule.sub(replacement,text)
    return text,text!=old

def direct_text(text):
    # Only the request region is considered; source attachments and quoted history
    # must not be elevated to a present complaint.
    if not isinstance(text,str):return ''
    if '## My request:' in text:text=text.split('## My request:',1)[1].split('<image ',1)[0]
    if WRAPPER.search(text) or text.lstrip().startswith(('# AGENTS.md','<proposed_plan>')):return ''
    if '以下是参考资料' in text:text=text.split('以下是参考资料',1)[0]
    text=re.sub(r'```[\s\S]*?```','',text)
    text=re.sub(r'(?m)^\s*>.*$','',text)
    text=re.sub(r'<(?:image|in-app-browser-context)[\s\S]*?</(?:image|in-app-browser-context)>','',text)
    return text.strip()[:8000]

def timestamp(value):
    if isinstance(value,(int,float)):return float(value)
    try:return datetime.fromisoformat(str(value).replace('Z','+00:00')).timestamp()
    except (TypeError,ValueError):return None

def _owned_regular(path):
    for parent in path.parents:
        if parent.is_symlink():raise PermissionError('source_parent_alias')
    s=path.lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_uid!=os.getuid() or s.st_nlink!=1:raise PermissionError('source_identity_unverified')
    return s

def _text(content):
    if isinstance(content,str):return content
    if isinstance(content,list):return '\n'.join(x.get('text','') for x in content if isinstance(x,dict) and x.get('type') in {'text','input_text','output_text'})
    return ''

def register(store,home=None):
    home=Path(home or Path.home())
    paths=[('codex','codex',home/'.codex/sessions'),('claude','claude',home/'.claude/projects'),('hermes','hermes',home/'.hermes/state.db')]
    profile=home/'.hermes/profiles'
    if profile.is_dir():
        for p in sorted(profile.iterdir()):
            if p.is_dir() and not p.is_symlink() and (p/'state.db').is_file():paths.append(('hermes-'+p.name,'hermes',p/'state.db'))
    for key,platform,path in paths:
        store.conn.execute('INSERT OR IGNORE INTO sources(id,platform,path) VALUES(?,?,?)',(key,platform,str(path)))

def context_features(text):
    # Only structural cues leave the original conversation. Do not automatically
    # forward assistant prose that may quote memory or business source bodies.
    labels=[]
    for pattern,label in [(r'完成|已修复|通过|成功','包含完成或通过的表述'),
                          (r'未验证|待核|尚未|缺口','包含未验证或待核对的表述'),
                          (r'授权|确认|允许|是否','包含提问或授权相关表述'),
                          (r'\.md|文件|正文|交付','包含交付物相关表述')]:
        if re.search(pattern,text or ''):labels.append(label)
    return '前文结构线索（不认证内容）：'+'；'.join(labels) if labels else ''


def insert(store,source,session,when,text,context,reference):
    if when is None or when<store.get('config',{}).get('since',time.time()):return False
    if personal_scope.company_stopped() and not source_metadata_admitted(session,reference.get('path'),source.get('id')):return False
    quote=direct_text(text)
    if not quote or not SIGNAL.search(quote):return False
    # Ambiguous candidates are classified by a model before becoming incidents.
    quote,redacted=clean(quote);context=context_features(context)
    key=digest([source['platform'],round(when,3),quote])[:32]
    count=store.conn.execute('INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)',
        (key,source['id'],session,when,quote,context[:6000],packed(reference),'pending',time.time(),int(redacted))).rowcount
    return bool(count)

def read_active(store,active,operation):
    from .control import gate
    with gate(store.root,active):
        value=operation()
        if not active():raise PermissionError('not_active')
        return value


def scan_jsonl(store,source,max_files=20,max_lines=1500,*,active=None):
    from ..conversation_scope import bind
    active=bind(active or store.active)
    root=Path(source['path'])
    if not root.is_dir() or root.is_symlink() or root.stat().st_uid!=os.getuid():raise PermissionError('source_root_unavailable')
    cursor=json.loads(source['cursor']);since=store.get('config',{})['since']
    pattern='*/*/*/*.jsonl' if source['platform']=='codex' else '*/*.jsonl'
    files=sorted((p for p in root.glob(pattern) if p.stat().st_mtime>=since),key=lambda p:p.stat().st_mtime,reverse=True)
    scanned=added=0;pending=0
    for p in files:
        if not active():raise PermissionError('not_active')
        session_hint=p.stem[-36:] if source["platform"]=="codex" else p.stem
        if personal_scope.company_stopped() and not source_metadata_admitted(session_hint,str(p)):continue
        from ..conversation_scope import earliest_observation
        conversation_since=earliest_observation(session_hint,str(p),since)
        st=_owned_regular(p);rel=str(p.relative_to(root));prior=cursor.get(rel,{})
        if prior.get('inode')==st.st_ino and prior.get('size')==st.st_size:continue
        if scanned>=max_files:pending+=1;continue
        offset=prior.get('offset',0) if prior.get('inode')==st.st_ino and st.st_size>=prior.get('offset',0) else 0
        # Content prefix detects in-place rotation/rewrite; stable appends retain cursor.
        with p.open('rb') as f:
            if personal_scope.company_stopped() and source['platform']=='codex':
                header=json.loads(read_active(store,active,lambda:f.readline(MAX_LINE+1)))
                if header.get('type')!='session_meta' or not source_metadata_admitted(header.get('payload',{}).get('id'),str(p)):continue
                f.seek(0)
            prefix=hashlib.sha256(read_active(store,active,lambda:f.read(min(256,prior.get('prefix_len',256))))).hexdigest()
            if prior.get('prefix') and prefix!=prior['prefix']:offset=0
            f.seek(offset);previous=prior.get('context','');session=prior.get('session','');lines=0
            while lines<max_lines:
                start=f.tell();raw=read_active(store,active,lambda:f.readline(MAX_LINE+1))
                if not raw:break
                if len(raw)>MAX_LINE:
                    while raw and not raw.endswith(b'\n'):raw=read_active(store,active,lambda:f.readline(MAX_LINE+1))
                    lines+=1;continue
                if not raw.endswith(b'\n'):f.seek(start);break
                lines+=1
                try:d=json.loads(raw)
                except (ValueError,UnicodeDecodeError):continue
                when=timestamp(d.get('timestamp'));role='';content=''
                if d.get('type')!='session_meta' and (when is None or when<conversation_since):continue
                if source['platform']=='codex':
                    if d.get('type')=='session_meta':
                        meta=d.get('payload',{});session=meta.get('id','')
                        origin=meta.get('source','')
                        # Agent/background transcripts are not owner input.
                        if isinstance(origin,dict) or origin in {'subagent','exec'}:session='excluded'
                    if session=='excluded':continue
                    q=d.get('payload',{})
                    # Non-message events never reach text extraction. Keep the
                    # fresh native admission at the actual message boundary,
                    # rather than repeating it for every progress/tool event.
                    if d.get('type')!='response_item' or q.get('type')!='message':continue
                    if personal_scope.company_stopped() and not source_metadata_admitted(session,str(p)):continue
                    role=q.get('role');content=_text(q.get('content'))
                else:
                    if d.get('isSidechain') or d.get('userType') not in (None,'external') or d.get('type') not in {'user','assistant'}:continue
                    if d.get('isMeta'):continue
                    session=d.get('sessionId','')
                    if personal_scope.company_stopped() and not source_metadata_admitted(session,str(p)):continue
                    role=d.get('message',{}).get('role');content=_text(d.get('message',{}).get('content'))
                if role=='user':
                    added+=insert(store,source,session,when,content,previous,{'path':str(p),'offset':start,'sha256':hashlib.sha256(raw).hexdigest()})
                if role in {'assistant','user'}:
                    safe=direct_text(content)
                    if safe:previous=context_features(safe)
            offset=f.tell();f.seek(0);head=read_active(store,active,lambda:f.read(min(256,st.st_size)))
        cursor[rel]={'inode':st.st_ino,'size':st.st_size if offset>=st.st_size else -1,'offset':offset,
                     'prefix':hashlib.sha256(head).hexdigest(),'prefix_len':len(head),'session':session,'context':previous}
        scanned+=1
        if offset<st.st_size:pending+=1
    return cursor,added,{'changed_files_scanned':scanned,'pending_files':pending,'status':'backfill' if pending else 'current'}

def scan_hermes(store,source,limit=1000,*,active=None):
    from ..conversation_scope import bind
    active=bind(active or store.active)
    path=Path(source['path']);_owned_regular(path)
    cursor=json.loads(source['cursor']);positions=cursor.get('sessions',{})
    conf=store.get('config',{});trusted=conf.get('hermes_owner_ids',{})
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=.1);db.row_factory=sqlite3.Row
    try:
        cols={r[1] for r in db.execute('PRAGMA table_info(messages)')}
        if not {'id','session_id','role','content','timestamp'}<=cols:raise ValueError('source_schema_changed')
        s_cols={r[1] for r in db.execute('PRAGMA table_info(sessions)')}
        if not {'source','user_id','id'}<=s_cols:raise ValueError('identity_schema_missing')
        # Source ownership is selected before content is read. Gateway sessions need
        # an explicit, trusted per-profile sender mapping, never a role=user guess.
        identities=db.execute('SELECT s.id,s.source,s.user_id FROM sessions s WHERE s.started_at>=? OR EXISTS(SELECT 1 FROM messages m WHERE m.session_id=s.id AND m.timestamp>=?)',(conf['since'],conf['since'])).fetchall()
        allowed=[];excluded=0
        for row in identities:
            if personal_scope.company_stopped() and not source_metadata_admitted(row['id'],str(path)):excluded+=1;continue
            origin=row['source'];sender=str(row['user_id'] or '')
            local=source['id']=='hermes' and origin in {'cli','terminal','desktop','tui'} and sender in {'','local','local_user',pwd.getpwuid(os.getuid()).pw_name}
            mapped=sender and sender in trusted.get(source['id'],[])
            if local or mapped:allowed.append(row['id'])
            else:excluded+=1
        added=0;pending=False
        # Query only already authenticated sessions; unrelated bodies never enter the process.
        for session in allowed:
            last=positions.get(session,0)
            maximum=last
            from ..conversation_scope import earliest_observation
            conversation_since=earliest_observation(session,str(path),conf['since'])
            rows=read_active(store,active,lambda:db.execute('SELECT id,role,content,timestamp FROM messages WHERE session_id=? AND id>? AND timestamp>=? ORDER BY id LIMIT ?',
                (session,last,conversation_since,limit)).fetchall())
            for row in rows:
                if not active():raise PermissionError('not_active')
                maximum=max(maximum,row['id'])
                if row['role']!='user':continue
                content=row['content']
                try:content=_text(json.loads(content))
                except (ValueError,TypeError):pass
                added+=insert(store,source,session,timestamp(row['timestamp']),content,'',{'path':str(path),'row':row['id'],'sha256':digest(str(row['content']))})
            positions[session]=maximum
            pending|=len(rows)==limit
        # A global id cursor must not skip a capped older session.
        if pending:return {'sessions':positions},added,{'pending_sessions':True,'identity_excluded':excluded,'status':'backfill'}
        return {'sessions':positions},added,{'identity_excluded':excluded,'owner_sessions':len(allowed),'status':'partial_identity' if excluded else ('current' if allowed else 'no_owner_events')}
    finally:db.close()

def scan(store):
    from ..conversation_scope import bind
    active=bind(store.active)
    results=[]
    for source in store.rows('SELECT * FROM sources ORDER BY platform,id'):
        if not active():break
        try:
            cursor,count,info=(scan_hermes(store,source,active=active) if source['platform']=='hermes' else scan_jsonl(store,source,active=active))
            store.conn.execute('UPDATE sources SET cursor=?,status=?,detail=?,updated=? WHERE id=?',
                (packed(cursor),info.pop('status'),packed(info),time.time(),source['id']))
            results.append({'source':source['id'],'added':count})
        except (OSError,ValueError,sqlite3.Error) as e:
            code=type(e).__name__
            if source['status']!='unavailable':store.notify('source-'+source['id']+'-'+str(source['updated']),'source_unavailable')
            store.conn.execute('UPDATE sources SET status=?,detail=?,updated=? WHERE id=?',('unavailable',code,time.time(),source['id']))
            results.append({'source':source['id'],'error':code})
    return results


def reference_is_current(message):
    """Recheck exact bytes before transmission; changed/unavailable evidence waits."""
    try:
        ref=json.loads(message['reference'])
        if personal_scope.company_stopped() and not source_metadata_admitted(message.get('session'),ref.get('path'),message.get('source_id')):return False
        path=Path(ref['path'])
        identity=_owned_regular(path)
        if 'offset' in ref:
            with path.open('rb') as f:f.seek(ref['offset']);raw=f.readline(MAX_LINE+1)
            return hashlib.sha256(raw).hexdigest()==ref['sha256']
        if 'row' in ref:
            conn=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=.1)
            try:
                row=conn.execute('SELECT content FROM messages WHERE id=?',(ref['row'],)).fetchone()
                return bool(row) and digest(str(row[0]))==ref['sha256']
            finally:conn.close()
        if message.get('source_id')=='seed':
            signature=(identity.st_dev,identity.st_ino,identity.st_size,identity.st_mtime_ns,identity.st_ctime_ns)
            now=time.monotonic();key=str(path)
            prior=_SEED_READ_FAILURES.get(key)
            if prior and prior[0]==signature and prior[1]>now:return False
            from .memory_operator import bounded
            try:raw=bounded(path)
            except (OSError,ValueError):
                for old in list(_SEED_READ_FAILURES):
                    if _SEED_READ_FAILURES[old][1]<=now:del _SEED_READ_FAILURES[old]
                if len(_SEED_READ_FAILURES)>=32:_SEED_READ_FAILURES.pop(next(iter(_SEED_READ_FAILURES)))
                _SEED_READ_FAILURES[key]=(signature,time.monotonic()+60)
                return False
            _SEED_READ_FAILURES.pop(key,None)
            after=_owned_regular(path)
            if signature!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):return False
            # Preserve Path.read_text universal-newline behavior used by seed hashes.
            return digest(raw.decode('utf-8').replace('\r\n','\n').replace('\r','\n'))==ref['sha256']
        return False
    except (OSError,ValueError,KeyError,TypeError,AttributeError,sqlite3.Error):return False


def incident_admitted(store,incident_id):
    if not personal_scope.company_stopped():return True
    rows=store.rows("SELECT m.session,m.source_id,CASE WHEN json_valid(m.reference) THEN json_extract(m.reference,'$.path') END AS path FROM messages m JOIN quotes q ON q.message_id=m.id WHERE q.incident_id=?",(incident_id,))
    return bool(rows) and all(source_metadata_admitted(r['session'],r['path'],r['source_id']) for r in rows)
