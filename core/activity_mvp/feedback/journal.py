"""Conflict-aware managed Markdown sections; user annotations remain untouched."""
from __future__ import annotations
import re
import time
from datetime import datetime
from pathlib import Path
from .store import ZONE,STATES,atomic,digest,packed

START='<!-- AGI-FEEDBACK:BEGIN -->';END='<!-- AGI-FEEDBACK:END -->'
def managed(body):return START+'\n'+body.strip()+'\n'+END

def write_section(store,path,body):
    path=Path(path)
    if path.is_symlink() or any(p.is_symlink() for p in path.parents):raise PermissionError('journal_symlink')
    path.parent.mkdir(parents=True,exist_ok=True)
    current=path.read_text() if path.exists() else ''
    previous=store.conn.execute('SELECT body_sha FROM exports WHERE path=?',(str(path),)).fetchone()
    if current:
        if current.count(START)!=1 or current.count(END)!=1:
            conflict=path.with_name(path.stem+'（待合并-'+digest(body)[:8]+'）.md')
            if not conflict.exists():atomic(conflict,managed(body)+'\n')
            return False
        existing=current.split(START,1)[1].split(END,1)[0].strip()
        if not previous or previous[0]!=digest(existing):
            conflict=path.with_name(path.stem+'（待合并-'+digest(body)[:8]+'）.md')
            if not conflict.exists():atomic(conflict,managed(body)+'\n')
            return False
        result=current.split(START,1)[0]+managed(body)+current.split(END,1)[1]
    else:
        result='---\ntags: [领域/AI工作台, 类型/复盘, 隐私/内部]\n---\n\n'+managed(body)+'\n\n## 我的批注\n\n'
    if current!=result:atomic(path,result)
    store.conn.execute('INSERT INTO exports VALUES(?,?,?) ON CONFLICT(path) DO UPDATE SET body_sha=excluded.body_sha,updated=excluded.updated',(str(path),digest(body.strip()),time.time()))
    return True

def incident_body(incident,quotes):
    text='**'+incident['title']+'**\n\n状态：'+STATES[incident['state']]+'\n\n'
    text+='## 用户原话\n\n'
    for q in quotes:
        text+='\n'.join('> '+line for line in q['quote'].splitlines())+'\n\n'
        text+=datetime.fromtimestamp(q['observed'],ZONE).strftime('%Y-%m-%d %H:%M')+' · '+q['source_id']+(' · 含敏感信息遮盖' if q['redacted'] else '')+'\n\n'
    text+='## 背景与依据\n\n'+(incident['evidence'] or '尚待核对。')+'\n\n'
    text+='## 原因判断\n\n'+(incident['cause'] or '尚未查明，不能仅据抱怨认定技术根因。')+'\n\n'
    text+='## 实际改进\n\n'+(incident['fix'] or '尚无可核实的修复记录。')+'\n\n'
    text+='## 验证与剩余问题\n\n'+(incident['gap'] or '等待与本问题对应的实际验证。')+'\n\n'
    if incident['related']:text+='关联问题可在 AGI 中查看；关联不等于已证实复发。\n\n'
    text+='归档不是共享长期记忆，也不代表已获准修改规则或记忆审核决定。\n'
    return text

def render(store):
    store=filtered(store)
    conf=store.get('config',{});root=Path(conf['journal'])
    if company_stopped():root=root/'个人记录'
    # Export only into the single directory explicitly configured at enable time.
    if root.is_symlink() or any(p.is_symlink() for p in root.parents if p.exists()):raise PermissionError('journal_scope_changed')
    entries=store.rows('SELECT * FROM incidents ORDER BY created DESC');links=[];conflicts=0
    if company_stopped() and not entries:
        return {'incidents':None,'conflicts':0,'source_status':'personal_source_unavailable','history_preserved':True}
    for incident in entries:
        stamp=datetime.fromtimestamp(incident['created'],ZONE).strftime('%y%m%d')
        name='批评_'+re.sub(r'[^\w\u4e00-\u9fff -]','',incident['title'])[:35]+'_'+incident['id'][:6]+'_'+stamp+'.md'
        path=root/'事件'/name
        quotes=store.rows('SELECT m.* FROM quotes q JOIN messages m ON m.id=q.message_id WHERE q.incident_id=? ORDER BY m.observed',(incident['id'],))
        text=incident_body(incident,quotes)
        conflicts+=not write_section(store,path,text)
        links.append(f'- [[事件/{path.stem}|{incident["title"]}]]：{STATES[incident["state"]]}')
    index='自动收录与改进复查日志。原话、原因假设、实际修复和验证状态分别保存。\n\n'
    index+='\n'.join(links) if links else '暂无已归档事件。'
    conflicts+=not write_section(store,root/'00 批评与改进索引.md',index)
    store.set('journal_conflicts',conflicts);return {'incidents':len(entries),'conflicts':conflicts}

def weekly(store,key):
    store=filtered(store)
    rows=store.rows('SELECT title,state,fix,gap FROM incidents WHERE updated>? ORDER BY updated DESC',(time.time()-7*86400,))
    if company_stopped() and not rows:return {'source_status':'personal_source_unavailable','history_preserved':True}
    text='## ① 做了哪些事情\n\n'
    text+=f'- 本周复查 {len(rows)} 件有变化的事项；没有新批评不能单独证明已改善。\n'
    text+='\n## ② 修改了哪些文件\n\n- 更新本目录内的事件记录与索引；具体程序修复以事件验收记录为准。\n'
    text+='\n## ③ 哪些项目有进展\n\n'
    text+='\n'.join('- '+r['title']+'：'+STATES[r['state']]+'；'+(r['gap'] or '详见事件记录。') for r in rows) or '暂无可确认内容。'
    root=Path(store.get('config')['journal'])
    if company_stopped():root=root/'个人记录'
    return write_section(store,root/'复盘'/('改进复盘_'+key+'.md'),text)

def import_seed(store,path):
    path=Path(path);text=path.read_text();seed_hash=digest(text)
    if store.get('seed_hash')==seed_hash:return {'imported':0}
    sections=re.split(r'(?m)^## (?=\d+\.)',text)[1:];count=0
    with store.tx():
        for section in sections:
            title=section.splitlines()[0];match=re.search(r'(20\d\d-\d\d-\d\d)',title)
            if not match:continue
            when=datetime.fromisoformat(match[1]).replace(tzinfo=ZONE).timestamp()
            body=section.split('\n## ',1)[0];quotes=re.findall(r'(?m)^> (.+)$',body)
            if not quotes:continue
            key=digest(['seed',str(path),title])[:24]
            store.conn.execute('INSERT OR IGNORE INTO incidents(id,title,category,state,created,updated,cause,evidence,fix,gap,project) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (key,title.split('｜')[-1][:72],'other','insufficient',when,time.time(),
                 '历史日志已有复盘，当前根因及修复状态需按原证据继续核对。','已获准历史合集：'+path.name,
                 '原日志中的完成记录保留在原文件；未把历史说明当成当前运行验收。','历史导入不自动结案；详见原合集。','unknown'))
            for quote in quotes:
                mid=digest(['seed-quote',key,quote])[:32]
                store.conn.execute('INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)',(mid,'seed','seed-'+key,when,quote,'',packed({'path':str(path),'sha256':seed_hash}),'classified',time.time(),0))
                store.conn.execute('INSERT OR IGNORE INTO quotes VALUES(?,?)',(mid,key))
            count+=1
        store.set('seed_hash',seed_hash);store.log('seed_import',count=count,source_hash=seed_hash)
    return {'imported':count}

import sys
from pathlib import Path
from ..conversation_scope import filtered
from activity_mvp.source_governance.personal_sources import company_stopped
