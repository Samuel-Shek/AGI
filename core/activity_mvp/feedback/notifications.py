"""Actionable notices through the existing local channel and durable outbox."""
import json
import subprocess

from .control import gate
from .store import digest
from ..conversation_scope import filtered
from ..presentation_scope import DisplayScope

HUMAN_REASONS = {
    'preference': '方案涉及你的个人取舍', 'new_permission': '方案需要新的读取或操作权限',
    'payment': '方案需要付费', 'external_action': '方案涉及对外操作',
    'irreversible': '方案包含难以撤销的操作',
}


def _system_notices(store):
    """Only fixed, content-free health episodes; never reads hidden incidents."""
    current=[]
    from .health import unresolved_analysis, needs_attention
    for family,result in unresolved_analysis(store).items():
        if result.get('failures',0)>=3 and needs_attention(result):
            label={'feedback':'改进分析','reflection':'反思分析','controller':'分析准备'}[family]
            unknown=result.get('reason') in {'prior_input_unverifiable','prior_input_unresolved','reflection_input_unverifiable','reflection_input_duplicate'}
            action=('旧输入或执行结果尚未核清。已保留原记录，未重跑未知操作；请让 Codex 核对原失败事项与回执。' if unknown else
                    '尚未取得完整结果。已保留失败状态并继续独立检查；请让 Codex 核对来源与失败阶段，新增权限仍需你确认。')
            current.append(('analysis-health-'+family+'-'+str(result['episode']),'analysis_blocked',
                label+'持续受阻，'+action))
    if store.get('analysis_failures',0)>=3:
        current.append(('analysis-'+str(store.get('analysis_failure_episode')),'analysis_failure',
            '自动分析连续失败，相关分析暂缓。已按现有退避规则等待；请让 Codex 核查分析服务恢复条件。'))
    failure=store.get('memory_failure_episode',{})
    if failure.get('failures',0)>=3:
        current.append(('memory-'+str(failure['started']),'memory_failure',
            '记忆检查连续失败或结果未核清，本轮修复已停止。已保留可能的部分写入，未重跑未知操作；请让 Codex 核对原失败事项和回执。'))
    for source,episode in store.get('source_failure_episodes',{}).items():
        if episode.get('failures',0)>=3 and store.rows("SELECT id FROM sources WHERE id=? AND status='unavailable'",(source,)):
            current.append(('source-'+source+'-'+str(episode['started']),'source_unavailable',
                '已授权来源连续无法读取，相关检查不完整。已保留原游标并重新检查；请让 Codex 查明路径或权限故障，目录迁移仍需你确认。'))
    return current


def summary(original, delivery_state=0, identifiers=None):
    store=filtered(original);scope=DisplayScope(store.root.parent);pending=[];parts=[]
    if scope.invalid:return {'pending':[],'title':'PSI 需要处理的事项','body':''}
    restriction='' if identifiers is None else ' AND n.id IN ('+','.join('?' for _ in identifiers)+')'
    args=(delivery_state,) if identifiers is None else (delivery_state,*identifiers)
    rows=store.rows("""SELECT n.id,n.kind,n.incident_id,i.revision,i.state,i.auto_disabled
        FROM notifications n JOIN incidents i ON i.id=n.incident_id
        WHERE n.delivered=? AND n.kind IN ('decision','recurred')
        """+restriction+' ORDER BY n.created',args)
    for row in rows:
        if row['id']!=digest([row['incident_id'],row['kind'],row['revision']]):continue
        proposal={}
        if row['kind']=='decision':
            decisions=store.rows("SELECT proposal FROM decisions WHERE incident_id=? AND revision=? AND state='pending'",(row['incident_id'],row['revision']))
            if row['state'] not in {'decision','recurred'} or not decisions:continue
            proposal=json.loads(decisions[0]['proposal'])
        elif row['state']!='recurred' or not row['auto_disabled']:continue
        refs=store.rows('SELECT * FROM incidents WHERE id=?',(row['incident_id'],))
        messages=store.rows('SELECT m.* FROM messages m JOIN quotes q ON q.message_id=m.id WHERE q.incident_id=?',(row['incident_id'],))
        if scope.hidden([refs,messages,proposal]) or not scope.unchanged():continue
        key=row['incident_id'][:6]
        if row['kind']=='decision':
            reason=HUMAN_REASONS.get(proposal.get('reason_code'),'方案超出自动处理范围或执行结果尚待核对')
            body=f'事项 {key}：{reason}。已保留依据和候选方案，相关操作保持等待；请在原事项选择方案、继续调查或暂缓。'
        else:
            body=f'事项 {key}：同一问题修复后再次失败。已停止自动安装并保留复发证据；请让 Codex 核对原事项后提出下一步。'
        pending.append({'id':row['id'],'kind':row['kind']});parts.append(body)
        if len(pending)==20:break
    # Synthetic health notices have no incident row. Read only their exact,
    # currently active IDs from the original outbox, never incident content.
    raw=getattr(original,'original',original)
    for incident,kind,body in _system_notices(store):
        key=digest([incident,kind,0])
        if identifiers is not None and key not in identifiers:continue
        if raw.rows('SELECT id FROM notifications WHERE id=? AND incident_id=? AND kind=? AND delivered=?',(key,incident,kind,delivery_state)):
            pending.append({'id':key,'kind':kind});parts.append(body)
    if not scope.unchanged():pending=[];parts=[]
    if parts:parts.append('详情沿用「批评与改进」原事项及后台检查记录。')
    return {'pending':pending,'title':'PSI 需要处理的事项','body':'\n'.join(parts)}


def deliver(store):
    # 3 means submission started/unknown. Only a known pre-submit cancellation
    # may release this invocation's claims. A crash/timeout never resends them.
    with gate(store.root):
        if not store.active():return {'requested':0,'status':'paused','delivery_observed':False}
        notice=summary(store)
        if not notice['pending']:return {'requested':0,'delivery_observed':False}
        ids=[n['id'] for n in notice['pending']]
        with store.tx():
            for key in ids:
                claimed=store.conn.execute('UPDATE notifications SET delivered=3 WHERE id=? AND delivered=0',(key,))
                if claimed.rowcount!=1:raise RuntimeError('notification_claim_changed')
        submitted=False
        try:
            current=summary(store,delivery_state=3,identifiers=ids)
            if not store.active() or [n['id'] for n in current['pending']]!=ids:
                return {'requested':0,'status':'canceled_before_submission','delivery_observed':False}
            script='display notification '+json.dumps(current['body'],ensure_ascii=False)+' with title '+json.dumps(current['title'],ensure_ascii=False)
            store.log('notification_submission_started',count=len(ids))
            submitted=True
            result=subprocess.run(['/usr/bin/osascript','-e',script],capture_output=True,timeout=8)
        except (OSError,subprocess.TimeoutExpired):
            if not submitted:raise
            return {'requested':0,'delivery_observed':False,'unknown':True}
        finally:
            if not submitted:
                with store.tx():
                    for key in ids:store.conn.execute('UPDATE notifications SET delivered=0 WHERE id=? AND delivered=3',(key,))
        if result.returncode!=0:return {'requested':0,'delivery_observed':False,'unknown':True}
        with store.tx():
            for key in ids:store.conn.execute('UPDATE notifications SET delivered=1 WHERE id=? AND delivered=3',(key,))
            store.log('notification_delivery_requested',count=len(ids))
        return {'requested':len(ids),'delivery_observed':False}
