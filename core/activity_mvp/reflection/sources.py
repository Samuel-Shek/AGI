"""Authenticated primary conversation reads, bounded and independently cursorized."""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
import pwd
import re
import stat
import sqlite3
import time
from pathlib import Path
from ..feedback.control import gate
from ..feedback.sources import _owned_regular, _text, timestamp, MAX_LINE, source_metadata_admitted, CREDENTIALS
from ..feedback.store import digest, packed, Store as FeedbackStore

import sys
from activity_mvp.source_governance import personal_sources as personal_scope

_reference_reader = ContextVar('reflection_reference_reader', default=None)

def _reader_identity(root):
    value = (Path(root) / 'feedback.sqlite').lstat()
    if (not stat.S_ISREG(value.st_mode) or value.st_uid != os.getuid()
            or value.st_nlink != 1 or value.st_mode & 0o077):
        raise ValueError('reference_reader_identity')
    return (value.st_dev, value.st_ino, value.st_uid, value.st_mode, value.st_nlink)

@contextmanager
def reference_reader(store):
    """Reuse a read-only projection within one batch, never an admission result.

    Every reference still checks current controls, source policy, projection
    versions and source bytes. No transaction pins an old authorization view.
    """
    root = store.get('feedback_root')
    if not root:
        yield
        return
    root = Path(root).absolute()
    current = _reference_reader.get()
    if current is not None and current['root'] == root:
        yield
        return
    # Open lazily inside reference_current's original control gate. Empty or
    # paused batches must not initialize a full projection before that check.
    reader = {'root': root, 'authority': None, 'identity': None, 'invalid': False}
    token = _reference_reader.set(reader)
    try:
        yield
    finally:
        _reference_reader.reset(token)
        if reader['authority'] is not None:
            reader['authority'].close()

def _check_reference_reader(reader):
    if reader['invalid']:
        raise ValueError('reference_reader_invalidated')
    try:
        if _reader_identity(reader['root']) != reader['identity']:
            raise ValueError('reference_reader_changed')
        scope = getattr(reader['authority'].conn, 'scope', None)
        if scope is None or not scope.unchanged() or not scope.memory_unchanged():
            raise ValueError('reference_reader_policy_changed')
    except BaseException:
        # Restoring a file or policy cannot revive this batch's old authority.
        reader['invalid'] = True
        raise

@contextmanager
def _reference_authority(root):
    root = Path(root).absolute()
    reader = _reference_reader.get()
    if reader is None or reader['root'] != root:
        with FeedbackStore(root, readonly=True) as authority:
            yield authority
        return
    try:
        if reader['invalid']:
            raise ValueError('reference_reader_invalidated')
        if reader['authority'] is None:
            reader['identity'] = _reader_identity(root)
            reader['authority'] = FeedbackStore(root, readonly=True)
        _check_reference_reader(reader)
        yield reader['authority']
        _check_reference_reader(reader)
    except BaseException:
        reader['invalid'] = True
        raise

WRAPPERS=re.compile(r'<(?:environment_context|app-context|recommended_plugins|INSTRUCTIONS|external_codex_apps_open_page|workplace-task-assessment)\b[\s\S]*?</(?:environment_context|app-context|recommended_plugins|INSTRUCTIONS|external_codex_apps_open_page|workplace-task-assessment)>',re.I)

def visible_text(value):
    text=_text(value)
    if text.lstrip().startswith(('# AGENTS.md','<proposed_plan>')):return ''
    text=WRAPPERS.sub('',text).strip()
    # Preserve quotations as material. The analysis contract distinguishes them
    # from the owner's direct statements; no historical instruction is executed.
    return CREDENTIALS.sub('[凭据已拦截]',text)

def material_ranges(text):
    ranges=[];offset=0;fence=None;quoted=False
    # Transported control messages retain a user role in some clients. Their
    # nested or unfinished bodies never establish a statement by the owner.
    stack=[];start=None
    for token in re.finditer(r'<(/?)(heartbeat|hook_prompt|codex_delegation|codex_internal_context)\b[^>]*>',text,re.I):
        name=token[2].lower()
        if not token[1]:
            if not stack:start=token.start()
            if token[0].rstrip().endswith('/>'):
                if not stack:ranges.append([start,token.end()]);start=None
            else:stack.append(name)
        elif stack:
            if stack[-1]!=name:
                ranges.append([start,len(text)]);start=None;break
            stack.pop()
            if not stack:ranges.append([start,token.end()]);start=None
        else:ranges.append([token.start(),token.end()])
    if start is not None:ranges.append([start,len(text)])
    for line in text.splitlines(keepends=True):
        stripped=line.lstrip()
        if stripped.startswith(('```','~~~')):
            token=stripped[:3];fence=None if fence==token else token
            ranges.append([offset,offset+len(line)])
        elif fence or stripped.startswith('>') or (quoted and stripped.strip()) or line.startswith(('    ','\t')):
            ranges.append([offset,offset+len(line)])
        if stripped.startswith('>'):quoted=True
        elif not stripped.strip():quoted=False
        offset+=len(line)
    patterns=(r'(?s)“[^”]*(?:”|$)|‘[^’]*(?:’|$)|「[^」]*(?:」|$)|『[^』]*(?:』|$)',r'`+[^`\n]*`+',r'(?is)<blockquote\b[^>]*>.*?(?:</blockquote>|$)',r'(?s)<!--.*?(?:-->|$)',r'(?m)^.*(?:他说|她说|AI回答|助手回答|引用|示例|转述)[：:].*$')
    for pattern in patterns:
        for match in re.finditer(pattern,text):ranges.append([match.start(),match.end()])
    for pattern in (r'(?s)"[^"]*(?:"|$)',r"(?s)(?<![A-Za-z0-9])'[^']*(?:'|$)"):
        for match in re.finditer(pattern,text):ranges.append([match.start(),match.end()])
    # Only an unquoted explicit switch may identify the speaker's own text.
    # A switch inside a quoted draft stays material.
    material=re.compile(r'只(?:优化|改写|润色|翻译|整理).*提示词|不要回答(?:它|这个提示词)|不代表我的(?:自述|经历|观点)|不是我的(?:经历|观点)|(?:AI|助手).*?(?:写的|生成的).*草稿|(?:虚构|合成).*(?:对话|验收|测试)')
    offset=0
    for paragraph in re.split(r'(\n\s*\n)',text):
        if material.search(paragraph):
            boundary=len(paragraph)
            for switch in re.finditer(r'(?:我自己的问题是|我本人的(?:问题|观点|经历)是|我的实际情况是)[：:]',paragraph):
                position=offset+switch.start()
                if not any(a<=position<b for a,b in ranges):boundary=switch.start();break
            ranges.append([offset,offset+boundary])
        offset+=len(paragraph)

    return ranges


def cached_material_ranges(store,row):
    """Recheck admitted cache, including old parser-2 chunks, without reingest.

    Return offsets local to this chunk; None means provenance is incomplete.
    This never reads original files or changes references, progress or budgets.
    """
    try:
        ref=json.loads(row['reference']);count=ref.get('segments',1)
        index=ref.get('segment',0);start=ref.get('chunk_start',0)
        if any(type(v) is not int for v in (count,index,start)) or not 1<=count<=256 or not 0<=index<count or start<0:return None
        chunks=[row]
        if count>1:
            message=ref.get('message_id')
            if not isinstance(message,str) or not message:return None
            # The inner query selects metadata IDs only. Body reads still pass
            # through the existing owner/display projection on the outer view.
            chunks=store.rows("SELECT * FROM turns WHERE id IN (SELECT id FROM main.turns WHERE context_id=? AND source_id=? AND session=? AND CASE WHEN json_valid(reference) THEN json_extract(reference,'$.message_id') END=? LIMIT 257)",
                (row['context_id'],row['source_id'],row['session'],message))
            if len(chunks)!=count:return None
            chunks.sort(key=lambda c:json.loads(c['reference'])['segment'])
        identity={k:v for k,v in ref.items() if k not in {'segment','chunk_start','material_ranges'}}
        parts=[];position=size=0;found=False;ranges=[]
        for number,chunk in enumerate(chunks):
            checkpoint()
            current=json.loads(chunk['reference'])
            if any(chunk[k]!=row[k] for k in ('context_id','source_id','session','role','observed')):return None
            if {k:v for k,v in current.items() if k not in {'segment','chunk_start','material_ranges'}}!=identity:return None
            if type(current.get('segment',0)) is not int or type(current.get('chunk_start',0)) is not int:return None
            if current.get('segment',0)!=number or current.get('chunk_start',0)!=position:return None
            text=chunk['text'];size+=len(text.encode())
            if size>MAX_LINE:return None
            if chunk['id']==row['id']:
                if chunk['text']!=row['text'] or chunk['reference']!=row['reference']:return None
                found=True
            parts.append(text);position+=len(text)
            for pair in current.get('material_ranges',[]):
                if not isinstance(pair,list) or len(pair)!=2 or any(type(v) is not int for v in pair) or not 0<=pair[0]<=pair[1]:return None
                ranges.append(pair)
        if not found:return None
        ranges.extend(material_ranges(''.join(parts)))
        local=sorted({(max(a,start)-start,min(b,start+len(row['text']))-start) for a,b in ranges if a<start+len(row['text']) and b>start})
        merged=[]
        for a,b in local:
            if merged and a<=merged[-1][1]:merged[-1][1]=max(merged[-1][1],b)
            else:merged.append([a,b])
        return merged
    except (ValueError,KeyError,TypeError):return None


def owner_quote_current(store,row,text):
    if row['role']!='user':return False
    ranges=cached_material_ranges(store,row)
    if ranges is None:return False
    return any(not any(a<i+len(text) and b>i for a,b in ranges)
        for i in range(len(row['text'])) if row['text'].startswith(text,i))


def admit(store,source,session,role,when,content,reference,now):
    if role not in {'user','assistant'} or not session or when is None or not now-14*86400<=when<=now+300:return False
    if personal_scope.company_stopped() and not source_metadata_admitted(session,reference.get('path')):return False
    text=visible_text(content)
    if not text:return False
    # Split oversized messages by Unicode character boundaries. Each piece keeps
    # the exact raw-message version; pieces cannot count as independent evidence.
    chunks=[];start=0;size=0
    for index,char in enumerate(text):
        width=len(char.encode())
        if size+width>12000:
            chunks.append(text[start:index]);start=index;size=0
        size+=width
    chunks.append(text[start:]);ranges=material_ranges(text);position=0;context=digest([source['id'],session])[:32];count=0
    for index,chunk in enumerate(chunks):
        ref={**reference,'reflection_root':str(store.root),'parser_version':2,'observed':when,'chunk_start':position,'material_ranges':ranges,'segment':index,'segments':len(chunks),'message_id':digest(reference)}
        position+=len(chunk)
        ident=digest([source['id'],session,ref])[:32]
        count+=store.conn.execute('INSERT OR IGNORE INTO turns(id,context_id,source_id,session,role,observed,text,reference) VALUES(?,?,?,?,?,?,?,?)',
            (ident,context,source['id'],session,role,when,chunk,packed(ref))).rowcount
    if count:store.conn.execute('INSERT INTO contexts(id,source_id,session,updated) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET updated=MAX(updated,excluded.updated)',(context,source['id'],session,when))
    return count

def owner_allowed(source_id,origin,sender,feedback):
    local=source_id=='hermes' and origin in {'cli','terminal','desktop','tui'} and sender in {'','local','local_user',pwd.getpwuid(os.getuid()).pw_name}
    return local or bool(sender and sender in feedback.get('config',{}).get('hermes_owner_ids',{}).get(source_id,[]))


def read_active(store, active, operation):
    def read():
        value=operation()
        if not active():raise PermissionError('not_active')
        return value
    root=store.get('feedback_root')
    if root:
        with gate(root,active):return read()
    if not active():raise PermissionError('not_active')
    return read()


def _rewritten_turns(store,source_id,path):
    # Select only candidate IDs using stored metadata, then apply the unchanged
    # live source/context projection to those IDs. A predicate on the outer
    # view alone can make SQLite authorize every unrelated turn first. Raw IDs
    # never authorize a read or a deletion; only the projected rows below do.
    return store.rows("SELECT id,context_id,reference FROM turns WHERE id IN "
        "(SELECT id FROM main.turns WHERE source_id=? AND CASE WHEN json_valid(reference) "
        "THEN json_extract(reference,'$.path') END=?)",(source_id,str(path)))


def jsonl(store,source,now,max_files=20,max_lines=1500,active=lambda:True,batch=None):
    root=Path(source['path'])
    if not root.is_dir() or root.is_symlink() or root.stat().st_uid!=os.getuid():raise PermissionError('source_root_unavailable')
    cursor=json.loads(source['cursor']);files=cursor.get('files',{});count=scanned=pending=0
    seen_key='source_scan_seen:'+source['id'];seen=store.get(seen_key,{})
    def file_key(path):return hashlib.sha256(str(path).encode()).hexdigest()
    pattern='*/*/*/*.jsonl' if source['platform']=='codex' else '*/*.jsonl'
    candidates=sorted((p for p in checked_paths(root.glob(pattern)) if p.stat().st_mtime>=now-14*86400),key=lambda p:max(files.get(str(p.relative_to(root)),{}).get('scanned_at',0),seen.get(file_key(p),0)))
    current_keys={file_key(p) for p in candidates};seen={k:v for k,v in seen.items() if k in current_keys}
    def metadata_skipped(path):
        seen[file_key(path)]=now;store.set(seen_key,seen)
        if batch is not None:batch.complete()
    for path in candidates:
        if not active():break
        if batch is not None and batch.yield_now():pending+=1;break
        rel=str(path.relative_to(root))
        session_hint=path.stem[-36:] if source["platform"]=="codex" else path.stem
        if personal_scope.company_stopped() and not source_metadata_admitted(session_hint,str(path)):
            metadata_skipped(path)
            continue
        st=_owned_regular(path);prior=files.get(rel,{})
        unchanged=prior.get('inode')==st.st_ino and prior.get('size')==st.st_size and prior.get('mtime_ns')==st.st_mtime_ns
        if unchanged and not prior.get('force'):
            prior['scanned_at']=now
            if batch is not None:batch.complete()
            continue
        if scanned>=max_files:pending+=1;continue
        with path.open('rb') as stream:
            header=read_active(store,active,lambda:stream.readline(MAX_LINE+1))
            if source['platform']=='codex':
                try:meta=json.loads(header)
                except (ValueError,UnicodeDecodeError):raise PermissionError('identity_metadata_missing')
                payload=meta.get('payload',{})
                if meta.get('type')!='session_meta' or not payload.get('id'):raise PermissionError('identity_metadata_missing')
                origin=payload.get('source','')
                if isinstance(origin,dict) or origin in {'subagent','exec'}:
                    files[rel]={'scanned_at':now,'mtime_ns':st.st_mtime_ns,'inode':st.st_ino,'size':st.st_size,'offset':st.st_size,'excluded':True};scanned+=1
                    if batch is not None:batch.complete()
                    continue
                session=payload['id']
                if personal_scope.company_stopped() and not source_metadata_admitted(session,str(path)):
                    metadata_skipped(path)
                    continue
            else:session=''
            stream.seek(0);prefix=hashlib.sha256(read_active(store,active,lambda:stream.read(min(256,prior.get('prefix_len',256))))).hexdigest()
            rewritten=prior.get('size')==st.st_size and prior.get('mtime_ns')!=st.st_mtime_ns
            offset=prior.get('offset',0) if not prior.get('force') and not rewritten and prior.get('inode')==st.st_ino and st.st_size>=prior.get('offset',0) and prior.get('prefix',prefix)==prefix else 0
            if offset==0 and prior:
                # A rewritten transcript invalidates old evidence; source rows
                # remain intact. Derived input is rebuilt from the current bytes.
                old=_rewritten_turns(store,source['id'],path)
                for row in old:
                    if json.loads(row['reference']).get('path')==str(path):
                        store.conn.execute('DELETE FROM turns WHERE id=?',(row['id'],))
                        store.conn.execute("UPDATE contexts SET processed_seq=0,summary='{}' WHERE id=?",(row['context_id'],))
            stream.seek(offset);lines=0
            position=stream.tell();stream.seek(0)
            head=read_active(store,active,lambda:stream.read(min(256,st.st_size)));stream.seek(position)
            attempted=False
            def save_cursor(position):
                files[rel]={'scanned_at':now if attempted else prior.get('scanned_at',0),'mtime_ns':st.st_mtime_ns,'inode':st.st_ino,'size':st.st_size if position>=st.st_size else -1,'offset':position,'prefix':hashlib.sha256(head).hexdigest(),'prefix_len':len(head)}
                store.conn.execute("UPDATE sources SET cursor=?,status='backfill' WHERE id=?",(packed({'files':files}),source['id']))
            start=position
            try:
                while lines<max_lines and active():
                    if batch is not None and batch.yield_now():pending+=1;break
                    attempted=True
                    start=stream.tell();raw=read_active(store,active,lambda:stream.readline(MAX_LINE+1))
                    if not raw:
                        if batch is not None:batch.complete()
                        break
                    if len(raw)>MAX_LINE:
                        while raw and not raw.endswith(b'\n'):raw=read_active(store,active,lambda:stream.readline(MAX_LINE+1))
                        if not raw.endswith(b'\n'):stream.seek(start);break
                        if batch is not None:batch.complete()
                        lines+=1;pending+=1;continue
                    if not raw.endswith(b'\n'):stream.seek(start);break
                    if batch is not None:batch.complete()
                    lines+=1
                    try:data=json.loads(raw)
                    except (ValueError,UnicodeDecodeError):continue
                    if source['platform']=='codex':
                        q=data.get('payload',{})
                        if data.get('type')!='response_item' or q.get('type')!='message' or q.get('channel')=='analysis':continue
                        role=q.get('role');content=q.get('content');when=timestamp(data.get('timestamp'))
                    else:
                        if data.get('isSidechain') or data.get('isMeta') or data.get('userType') not in (None,'external') or data.get('type') not in {'user','assistant'}:continue
                        session=data.get('sessionId','');q=data.get('message',{});role=q.get('role');content=q.get('content');when=timestamp(data.get('timestamp'))
                    ref={'feedback_root':store.get('feedback_root'),'path':str(path),'offset':start,'sha256':hashlib.sha256(raw).hexdigest(),'source_id':source['id'],'platform':source['platform'],'session':session,'role':role}
                    count+=admit(store,source,session,role,when,content,ref,now)
            except LocalCheckTimeout:
                # Revisit the interrupted record; only completed records may
                # advance the cursor. Existing IDs make that replay idempotent.
                save_cursor(start)
                raise
            position=stream.tell()
            save_cursor(position)
        files[rel]={'scanned_at':now if attempted else prior.get('scanned_at',0),'mtime_ns':st.st_mtime_ns,'inode':st.st_ino,'size':st.st_size if position>=st.st_size else -1,'offset':position,'prefix':hashlib.sha256(head).hexdigest(),'prefix_len':len(head)}
        scanned+=1
        if position<st.st_size:pending+=1
    return {'files':files},count,{'changed_files_scanned':scanned,'pending_files':pending,'status':'backfill' if pending else 'current'}

def hermes(store,source,feedback,now,limit=1000,active=lambda:True,batch=None):
    path=Path(source['path']);_owned_regular(path);cursor=json.loads(source['cursor']);positions=cursor.get('sessions',{});seen=cursor.get('seen',{})
    trusted=feedback.get('config',{}).get('hermes_owner_ids',{});count=excluded=0;pending=False
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=.1) as conn:
        conn.row_factory=sqlite3.Row;conn=Connection(conn)
        if not {'id','session_id','role','content','timestamp'}<={r[1] for r in conn.execute('PRAGMA table_info(messages)')}:raise ValueError('source_schema_changed')
        if not {'source','user_id','id'}<={r[1] for r in conn.execute('PRAGMA table_info(sessions)')}:raise ValueError('identity_schema_missing')
        identities=conn.execute('SELECT id,source,user_id FROM sessions WHERE EXISTS(SELECT 1 FROM messages m WHERE m.session_id=sessions.id AND m.timestamp>=?)',(now-14*86400,)).fetchall()
        # Excluded identities cannot consume the bounded personal read budget.
        eligible=[]
        for identity in identities:
            sender=str(identity['user_id'] or '')
            if (personal_scope.company_stopped() and not source_metadata_admitted(identity['id'],str(path))) or not owner_allowed(source['id'],identity['source'],sender,feedback):
                excluded+=1;continue
            eligible.append(identity)
        identities=sorted(eligible,key=lambda r:seen.get(r['id'],0))
        pending=len(identities)>20
        for identity in identities[:20]:
            if batch is not None and batch.yield_now():pending=True;break
            if personal_scope.company_stopped() and not source_metadata_admitted(identity['id'],str(path)):excluded+=1;continue
            if not active():break
            sender=str(identity['user_id'] or '')
            if not owner_allowed(source['id'],identity['source'],sender,feedback):excluded+=1;continue
            session=identity['id'];last=positions.get(session,0)
            rows=read_active(store,active,lambda:conn.execute("SELECT id,role,content,timestamp FROM messages WHERE session_id=? AND id>? AND timestamp>=? AND timestamp<=? AND role IN ('user','assistant') ORDER BY id LIMIT ?",(session,last,now-14*86400,now+300,limit)).fetchall())
            attempted=False
            try:
                for row in rows:
                    if not active():break
                    if batch is not None and batch.yield_now():pending=True;break
                    attempted=True
                    if batch is not None:batch.complete()
                    content=row['content']
                    try:content=_text(json.loads(content))
                    except (ValueError,TypeError):pass
                    ref={'feedback_root':store.get('feedback_root'),'path':str(path),'row':row['id'],'sha256':digest(str(row['content'])),'source_id':source['id'],'platform':'hermes','session':session,'role':row['role'],'origin':identity['source'],'sender':sender}
                    count+=admit(store,source,session,row['role'],timestamp(row['timestamp']),content,ref,now)
                    last=row['id']
            except LocalCheckTimeout:
                positions[session]=last
                if attempted:seen[session]=now
                store.conn.execute("UPDATE sources SET cursor=?,status='backfill' WHERE id=?",(packed({'sessions':positions,'seen':seen}),source['id']))
                raise
            positions[session]=last;pending|=len(rows)==limit
            if attempted or not rows:seen[session]=now
            if not rows and batch is not None:batch.complete()
            store.conn.execute("UPDATE sources SET cursor=?,status='backfill' WHERE id=?",(packed({'sessions':positions,'seen':seen}),source['id']))
    return {'sessions':positions,'seen':seen},count,{'identity_excluded':excluded,'status':'backfill' if pending else 'partial_identity' if excluded else 'current'}

def bind_read_check(store,feedback=None,extra=lambda:True):
    from ..conversation_scope import binding
    grant_binding=binding()
    original_extra=extra
    extra=lambda:binding()==grant_binding and original_extra()
    root=store.get('feedback_root')
    epoch=store.get('authorization_epoch',0)
    if not root:
        store.read_check=lambda:store.enabled() and store.get('authorization_epoch',0)==epoch and extra()
        return store.read_check
    def check():
        with FeedbackStore(root,readonly=True,controls_only=True) as authority:
            stop=authority.conn.execute("SELECT COALESCE(MAX(id),0) FROM audit WHERE event IN ('owner_pause','owner_stop','owner_autonomy_pause')").fetchone()[0]
            return store.enabled() and store.get('authorization_epoch',0)==epoch and authority.active() and not authority.get('autonomy',{}).get('paused',False) and stop==stop_epoch and extra()
    with FeedbackStore(root,readonly=True,controls_only=True) as authority:
        stop_epoch=authority.conn.execute("SELECT COALESCE(MAX(id),0) FROM audit WHERE event IN ('owner_pause','owner_stop','owner_autonomy_pause')").fetchone()[0]
    store.read_check=check
    return check

def reference_current(ref,feedback=None,*,store=None):
    if ref.get('parser_version')!=2:return False
    try:
        if store is None:
            from .store import Store
            with Store(ref.get('reflection_root',Path(ref['feedback_root']).parent/'reflection'),readonly=True) as current:
                bind_read_check(current)
                return reference_current(ref,feedback,store=current)
        check=getattr(store,'read_check',None) or bind_read_check(store)
        with gate(ref['feedback_root'],check):return _reference_current(ref,feedback)
    except (OSError,ValueError,KeyError,sqlite3.Error,TypeError):return False

def _reference_metadata_rows(store,sql,args,ref):
    """Fixed provenance queries only; never expose a mixed source's cursor.

    A shared root can have a hidden, unrelated cursor entry. Its exact admitted
    conversation still needs the registered root and its own progress, while
    root visibility and the bound display/source policies remain mandatory.
    """
    from ..presentation_scope import ReadView
    conn=store.conn
    if not isinstance(conn,ReadView) or not personal_scope.company_stopped():
        return store.rows(sql,args)
    def current():
        return (conn.scope.unchanged() and conn.scope.memory_unchanged()
            and source_metadata_admitted(ref.get('session'),ref.get('path'),ref.get('source_id')))
    if not current():return []
    rows=[dict(row) for row in conn.raw.execute(sql,args)]
    if not current() or any(not conn.scope.visible_row(*row.values()) for row in rows):return []
    return rows


def _reference_current(ref,feedback=None):
    if personal_scope.company_stopped() and not source_metadata_admitted(ref.get('session'),ref.get('path')):return False
    try:
        path=Path(ref['path']);_owned_regular(path)
        with _reference_authority(ref['feedback_root']) as authority:
            registered=_reference_metadata_rows(authority,'SELECT path,platform FROM main.sources WHERE id=?',(ref['source_id'],),ref)
            if not registered or registered[0]['platform']!=ref['platform']:return False
            base=Path(registered[0]['path']).absolute()
            if ref['platform']=='hermes':
                if path!=base or not owner_allowed(ref['source_id'],ref['origin'],ref['sender'],authority):return False
            elif not path.is_relative_to(base):return False
        if 'offset' in ref:
            with path.open('rb') as stream:
                if ref['platform']=='codex':
                    meta=json.loads(stream.readline(MAX_LINE+1)).get('payload',{})
                    if meta.get('id')!=ref['session'] or isinstance(meta.get('source'),dict) or meta.get('source') in {'subagent','exec'}:return False
                stream.seek(ref['offset']);raw=stream.readline(MAX_LINE+1)
            if hashlib.sha256(raw).hexdigest()!=ref['sha256']:return False
            data=json.loads(raw);q=data.get('payload',{}) if ref['platform']=='codex' else data.get('message',{})
            if ref['platform']=='claude' and (data.get('isSidechain') or data.get('isMeta') or data.get('userType') not in (None,'external') or data.get('sessionId')!=ref['session']):return False
            return q.get('role')==ref['role'] and q.get('channel')!='analysis'
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=.1) as conn:
            conn=Connection(conn)
            conn.execute('BEGIN')
            meta=conn.execute('SELECT m.timestamp,m.role,s.source,s.user_id FROM messages m JOIN sessions s ON s.id=m.session_id WHERE m.id=? AND m.session_id=?',(ref['row'],ref['session'])).fetchone()
            if not meta:return False
            observed=timestamp(meta[0])
            if observed is None or not time.time()-14*86400<=observed<=time.time()+300 or observed!=ref.get('observed') or meta[1]!=ref['role'] or meta[2]!=ref['origin'] or str(meta[3] or '')!=ref['sender']:return False
            # Original time and owner are checked before the body query, in
            # the same read snapshot so metadata/body cannot mix versions.
            row=conn.execute('SELECT content FROM messages WHERE id=? AND session_id=?',(ref['row'],ref['session'])).fetchone()
            return bool(row and digest(str(row[0]))==ref['sha256'])
    except (OSError,ValueError,KeyError,sqlite3.Error,TypeError):return False

def context_complete(store,context_id):
    root=store.get('feedback_root')
    if root:
        try:
            with gate(root,getattr(store,'read_check',None) or bind_read_check(store)):
                # The existing batch reader remains lazy and rechecks policy
                # and source identity. Do not construct an unused projection
                # for every complete-context check.
                with _reference_authority(root):
                    return _context_complete(store,context_id)
        except (PermissionError,ValueError):return False
    return _context_complete(store,context_id)

def _context_complete(store,context_id):
    """A cached conversation is complete only after its source tail is read."""
    rows=store.rows('SELECT source_id,reference FROM turns WHERE context_id=? AND observed>=?',(context_id,time.time()-14*86400))
    if not rows:return False
    checked=set();messages=set()
    for row in rows:
        ref=json.loads(row['reference']);key=(row['source_id'],ref['path'],ref['session'])
        message=(ref['path'],ref.get('offset'),ref.get('row'))
        if message not in messages:
            if not reference_current(ref,store=store):return False
            messages.add(message)
        if key in checked:continue
        checked.add(key)
        sources=_reference_metadata_rows(store,'SELECT path,platform,status FROM main.sources WHERE id=?',(row['source_id'],),ref)
        if len(sources)!=1 or sources[0]['platform']!=ref['platform'] or sources[0]['status']=='unavailable':return False
        source=sources[0]
        try:
            if ref['platform']=='hermes':
                progress_rows=_reference_metadata_rows(store,
                    "SELECT s.path,s.platform,s.status,j.value AS progress FROM main.sources s LEFT JOIN json_each(CASE WHEN json_valid(s.cursor) THEN s.cursor ELSE '{}' END,'$.sessions') j ON j.key=? WHERE s.id=?",
                    (ref['session'],row['source_id']),ref)
                if len(progress_rows)!=1 or any(progress_rows[0][key]!=source[key] for key in source):return False
                progress=progress_rows[0]['progress']
                with sqlite3.connect(Path(ref['path']).as_uri()+'?mode=ro',uri=True,timeout=.1) as conn:
                    conn=Connection(conn)
                    last=conn.execute("SELECT MAX(id) FROM messages WHERE session_id=? AND timestamp>=? AND timestamp<=? AND role IN ('user','assistant')",(ref['session'],time.time()-14*86400,time.time()+300)).fetchone()[0] or 0
                if type(progress) is not int or progress<last:return False
            else:
                path=Path(ref['path']);rel=str(path.relative_to(Path(source['path'])))
                progress_rows=_reference_metadata_rows(store,
                    "SELECT s.path,s.platform,s.status,j.value AS progress FROM main.sources s LEFT JOIN json_each(CASE WHEN json_valid(s.cursor) THEN s.cursor ELSE '{}' END,'$.files') j ON j.key=? WHERE s.id=?",
                    (rel,row['source_id']),ref)
                if len(progress_rows)!=1 or any(progress_rows[0][key]!=source[key] for key in source):return False
                progress=json.loads(progress_rows[0]['progress']) if isinstance(progress_rows[0]['progress'],str) else None
                if not isinstance(progress,dict) or type(progress.get('offset')) is not int:return False
                version=path.stat()
                if progress.get('force') or progress.get('offset',0)<version.st_size or progress.get('inode')!=version.st_ino or progress.get('mtime_ns')!=version.st_mtime_ns:return False
        except (OSError,ValueError,sqlite3.Error):return False
    return True

def invalidate_input(store,ref):
    rows=store.rows('SELECT * FROM sources WHERE id=?',(ref['source_id'],))
    if not rows:return
    source=rows[0];cursor=json.loads(source['cursor'])
    if source['platform']=='hermes':
        cursor.setdefault('sessions',{})[ref['session']]=0
        store.conn.execute('DELETE FROM turns WHERE source_id=? AND session=?',(source['id'],ref['session']))
    else:
        try:rel=str(Path(ref['path']).relative_to(Path(source['path'])))
        except ValueError:
            store.conn.execute('DELETE FROM turns WHERE source_id=? AND session=?',(source['id'],ref['session']));return
        cursor.setdefault('files',{}).setdefault(rel,{})['force']=True
    store.conn.execute("UPDATE contexts SET processed_seq=0,summary='{}' WHERE source_id=? AND session=?",(source['id'],ref['session']))
    store.conn.execute("UPDATE sources SET cursor=?,status='pending' WHERE id=?",(packed(cursor),source['id']))


def context_version(store, context_id):
    if not context_complete(store,context_id):return None
    return digest(store.rows('SELECT id,role,observed,reference FROM turns WHERE context_id=? AND observed>=? ORDER BY seq',(context_id,time.time()-14*86400,)))

def evidence_current(store,evidence,*,require_context=False):
    # Reject expired evidence before reopening any original transcript.
    if not time.time()-14*86400<=evidence.get('observed',0)<=time.time()+300:return False
    rows=store.rows('SELECT * FROM turns WHERE id=?',(evidence['id'],))
    if not rows or rows[0]['role']!=evidence['role'] or evidence['quote'] not in rows[0]['text']:return False
    if evidence.get('attribution')=='user_statement' and not owner_quote_current(store,rows[0],evidence['quote']):return False
    if not reference_current(json.loads(rows[0]['reference']),store=store):return False
    if require_context and 'context_version' not in evidence:return False
    if 'context_version' in evidence:
        return evidence['context_version']==context_version(store,rows[0]['context_id'])
    return True


def _archive_input(store, rows):
    """Only the caller's already admitted metadata selects preserved rows."""
    contexts={r['id'] for r in store.rows('SELECT id FROM contexts')}
    rows=[r for r in rows if r['context_id'] in contexts]
    store.conn.execute('CREATE TABLE IF NOT EXISTS retired_turns AS SELECT * FROM main.turns WHERE 0')
    store.conn.execute('CREATE TABLE IF NOT EXISTS retired_contexts AS SELECT * FROM main.contexts WHERE 0')
    store.conn.execute('CREATE INDEX IF NOT EXISTS retired_turns_context ON retired_turns(context_id,seq)')
    store.conn.execute('CREATE INDEX IF NOT EXISTS retired_contexts_id ON retired_contexts(id)')
    for context in sorted({r['context_id'] for r in rows}):
        store.conn.execute('INSERT INTO retired_contexts SELECT * FROM main.contexts WHERE id=? AND NOT EXISTS(SELECT 1 FROM retired_contexts WHERE id=?)',(context,context))
    for row in rows:
        store.conn.execute('INSERT INTO retired_turns SELECT * FROM main.turns WHERE id=? AND NOT EXISTS(SELECT 1 FROM retired_turns WHERE id=?)',(row['id'],row['id']))
        store.conn.execute('DELETE FROM turns WHERE id=?',(row['id'],))
    for context in sorted({r['context_id'] for r in rows}):
        store.conn.execute("UPDATE contexts SET processed_seq=0,summary='{}' WHERE id=?",(context,))


def _reset_cursor(source):
    if not personal_scope.company_stopped():return {}
    cursor=json.loads(source['cursor'])
    # Shared collectors retain excluded sessions' cursor history.
    if source['platform']=='hermes':
        for key in ('sessions','seen'):
            values=cursor.get(key,{})
            for session in list(values):
                if source_metadata_admitted(session,source['path'],source['id']):values.pop(session)
    else:
        files=cursor.get('files',{})
        for name in list(files):
            path=Path(source['path'])/name
            session=path.stem[-36:] if source['platform']=='codex' else path.stem
            if source_metadata_admitted(session,str(path)):files.pop(name)
    return cursor


def scan(store,feedback,now=None):
    from ..local_checks import ScanBatch, SOURCE_SECONDS, scan_order
    store=filtered(store,'reflection')
    now=time.time() if now is None else now
    if not store.enabled() or not feedback.active() or feedback.get('autonomy',{}).get('paused',False):return {'status':'paused'}
    store.set('feedback_root',str(feedback.root))
    epoch=store.get('authorization_epoch',0)
    stop_epoch=feedback.conn.execute("SELECT COALESCE(MAX(id),0) FROM audit WHERE event IN ('owner_pause','owner_stop','owner_autonomy_pause')").fetchone()[0]
    active=lambda:store.enabled() and store.get('authorization_epoch',0)==epoch and feedback.active() and not feedback.get('autonomy',{}).get('paused',False) and feedback.conn.execute("SELECT COALESCE(MAX(id),0) FROM audit WHERE event IN ('owner_pause','owner_stop','owner_autonomy_pause')").fetchone()[0]==stop_epoch
    active=bind_read_check(store,feedback,active)
    # A metadata-only absence check cannot grant access to any source. When an
    # old row exists, the unchanged filtered read still decides what may move.
    original=getattr(store,'original',store)
    has_legacy=original.conn.execute("SELECT 1 FROM main.turns WHERE coalesce(CASE WHEN json_valid(reference) THEN json_extract(reference,'$.parser_version') END,0)!=2 LIMIT 1").fetchone()
    legacy=store.rows("SELECT id,context_id,source_id FROM turns WHERE coalesce(json_extract(reference,'$.parser_version'),0)!=2") if has_legacy else []
    if store.get('source_parser_version')!=2 or legacy:
        with gate(feedback.root,active):
            store.conn.execute('BEGIN IMMEDIATE')
            try:
                # Preserve prior derived rows, too. Only recent-source cursors
                # are rebuilt by the existing bounded scanner; no call replay.
                _archive_input(store,legacy)
                for source in store.rows('SELECT id,platform,path,cursor FROM sources'):
                    if not personal_scope.company_stopped() or source['id'] in {r['source_id'] for r in legacy}:
                        store.conn.execute("UPDATE sources SET cursor=?,status='pending' WHERE id=?",(packed(_reset_cursor(source)),source['id']))
                store.set('source_parser_version',2)
                store.conn.execute('COMMIT')
            except BaseException:
                if store.conn.in_transaction:store.conn.execute('ROLLBACK')
                raise
        store=filtered(getattr(store,'original',store),'reflection')
    registered=feedback.rows("SELECT * FROM sources WHERE platform IN ('codex','claude','hermes') ORDER BY id")
    for source in registered:
        old=store.rows('SELECT path FROM sources WHERE id=?',(source['id'],))
        if old and old[0]['path']!=source['path']:
            with gate(feedback.root,active):
                store.conn.execute('BEGIN IMMEDIATE')
                try:
                    rows=store.rows('SELECT id,context_id FROM turns WHERE source_id=?',(source['id'],))
                    _archive_input(store,rows)
                    store.conn.execute('CREATE TABLE IF NOT EXISTS retired_sources AS SELECT * FROM main.sources WHERE 0')
                    store.conn.execute('INSERT INTO retired_sources SELECT * FROM main.sources WHERE id=?',(source['id'],))
                    store.conn.execute("UPDATE sources SET path=?,cursor='{}',status='pending' WHERE id=?",(source['path'],source['id']))
                    store.conn.execute('COMMIT')
                except BaseException:
                    if store.conn.in_transaction:store.conn.execute('ROLLBACK')
                    raise
            store=filtered(getattr(store,'original',store),'reflection')
    for source in feedback.rows("SELECT * FROM sources WHERE platform IN ('codex','claude','hermes') ORDER BY id"):
        store.conn.execute('INSERT OR IGNORE INTO sources(id,platform,path) VALUES(?,?,?)',(source['id'],source['platform'],source['path']))
    results=[];registered_ids={s['id'] for s in registered};inventory=[]
    for source in store.rows('SELECT * FROM sources ORDER BY id'):
        if source['id'] not in registered_ids:
            store.conn.execute("UPDATE sources SET status='unavailable' WHERE id=?",(source['id'],))
        else:inventory.append(source)
    inventory=scan_order(inventory,store.get('source_scan_next'));batch=ScanBatch()
    for index,source in enumerate(inventory):
        if not store.enabled() or not feedback.active() or feedback.get('autonomy',{}).get('paused',False):break
        if batch.yield_now():break
        store.set('source_scan_next',inventory[(index+1)%len(inventory)]['id'])
        try:
            source_batch=ScanBatch(SOURCE_SECONDS,parent=batch)
            cursor,count,detail=hermes(store,source,feedback,now,active=active,batch=source_batch) if source['platform']=='hermes' else jsonl(store,source,now,active=active,batch=source_batch)
            status=detail.pop('status');store.conn.execute('UPDATE sources SET cursor=?,status=?,detail=?,updated=? WHERE id=?',(packed(cursor),status,packed(detail),now,source['id']))
            results.append({'id':source['id'],'added':count})
        except (OSError,ValueError,sqlite3.Error) as error:
            store.conn.execute("UPDATE sources SET status='unavailable',detail=?,updated=? WHERE id=?",(packed({'reason':type(error).__name__}),now,source['id']))
            results.append({'id':source['id'],'error':type(error).__name__})
        batch.complete()
    for item in store.rows("SELECT id,evidence FROM insights WHERE state='active'"):
        if not active():return {'status':'paused','sources':results}
        if not all(evidence_current(store,e,require_context=True) for e in json.loads(item['evidence'])):store.withdraw(item['id'],'来源已变化或不可核对，判断已撤下。')
    if not active():return {'status':'paused','sources':results}
    store.set('last_scan',now)
    unavailable=any(r['id'] in registered_ids for r in store.rows("SELECT id FROM sources WHERE status='unavailable'"))
    return {'status':'partial' if unavailable else 'ok','sources':results}

from ..conversation_scope import filtered
from ..local_checks import LocalCheckTimeout, checked_paths, Connection, checkpoint


def automatic_contexts_admitted(store,*,context_id=None):
    if not personal_scope.company_stopped():return True
    store=filtered(store,'reflection')
    # Readiness needs one eligible discussion, not an inventory of every source.
    # The turn view still checks its complete context (including retired rows).
    # Payload construction and submission revalidate every selected source.
    condition=' AND t.context_id=?' if context_id is not None else ''
    rows=store.rows("SELECT t.session,json_extract(t.reference,'$.path') path FROM turns t WHERE json_valid(t.reference)"+condition+" LIMIT 1",(context_id,) if context_id is not None else ())
    return bool(rows) and source_metadata_admitted(rows[0]['session'],rows[0]['path'])
