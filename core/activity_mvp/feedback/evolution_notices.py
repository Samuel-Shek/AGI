"""Fixed safe summaries and a durable, single-target Miso delivery queue.

No arbitrary text is accepted by the compiler. Unknown writes are reconciled
by reading the original group; they are never retried or redirected.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from .store import atomic, digest, packed
from .task_maintenance import Ledger, TASKS, default_root, read_file, read_json, stage_text

NAME = 'AGI 自我进化'
TARGET_KEY = 'agi_evolution'
API = 'https://open.feishu.cn/open-apis'
OUTCOMES = {'partial': '完成一部分', 'complete': '本次回执标为完成，整体仍按原验收复核',
            'blocked': '受阻，保留原事项', 'failed': '本次失败，保留证据', 'awaiting_user': '等待本人真实答复'}
REPAIR_STATES = {'building': '工程构建中', 'prepared': '候选已准备，安装待验', 'blocked': '准备受阻',
                 'deploying': '配套发布中', 'observing': '流程登记已安装，自然效果待验',
                 'unknown': '执行结果不明，先核原请求', 'failed': '流程失败，保留原证据', 'rolled_back': '流程登记已回退，效果待验',
                 'verified': '原流程登记已验收，依据仍按原验收核对', 'decision': '确需本人决定，保持真实等待'}
FILE_TAGS = {'core/activity_mvp/feedback/sources.py': '来源读取模块（sources.py）',
             'core/activity_mvp/feedback/journal.py': '改进记录模块（journal.py）',
             'apps/PersonalAIMonitorMenuBar/Sources/PersonalAIMonitorMenuBar/FeedbackView.swift': '改进页面（FeedbackView.swift）',
             'apps/PersonalAIMonitorMenuBar/Sources/PersonalAIMonitorMenuBar/EvolutionView.swift': '进化页面（EvolutionView.swift）'}


def validate_stages(task, value, nullable=False):
    if nullable and value is None: return
    if not isinstance(value, dict): raise ValueError('safe_stage_required')
    bools = {'engineering', 'natural_effect', 'archive'} if task == 'agi' else {'engineering', 'whole_complete', 'archive'}
    fields = bools if task == 'agi' else bools | {'help_cases', 'remaining', 'earliest'}
    if set(value) != fields or any(type(value[k]) is not bool for k in bools): raise ValueError('safe_stage_fields')
    if task != 'agi':
        import datetime
        if any(type(value[k]) is not int or not 0 <= value[k] <= 100000 for k in ('help_cases', 'remaining')):
            raise ValueError('safe_stage_counts')
        if not isinstance(value['earliest'], str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value['earliest']):
            raise ValueError('safe_stage_date')
        datetime.date.fromisoformat(value['earliest'])


def validate_event(value):
    if not isinstance(value, dict): raise ValueError('safe_event_required')
    kind = value.get('kind')
    fields = {'task_updated': {'kind', 'task', 'before', 'after', 'initial', 'version', 'source_versions'},
              'task_run': {'kind', 'task', 'after', 'outcome', 'version', 'source_versions'},
              'source_error': {'kind', 'task'}, 'source_recovered': {'kind', 'task'}, 'task_retired': {'kind', 'task'},
              'repair_state': {'kind', 'state', 'before_state', 'changed', 'old_failed', 'candidate_passed'},
              'baseline': {'kind', 'identity_restored', 'paired_installed'},
              'functional_verified': {'kind','work_id','job','contract','revision','proof'}}
    if kind == 'functional_verified':
        from .proactive_contracts import CONTRACTS
        if value.get('contract') not in CONTRACTS: raise ValueError('unknown_functional_contract')
        for key in ('work_id','job'):
            if not isinstance(value.get(key),str) or not re.fullmatch('[0-9a-f]{24}',value[key]): raise ValueError('functional_identity')
        for key in ('revision','proof'):
            if not isinstance(value.get(key),str) or not re.fullmatch('[0-9a-f]{40,64}',value[key]): raise ValueError('functional_proof')
    if kind not in fields or set(value) != fields[kind]: raise ValueError('safe_event_fields')
    if kind.startswith('task_') or kind in {'source_error', 'source_recovered'}:
        if value['task'] not in TASKS: raise ValueError('safe_event_task')
    if kind in {'task_updated', 'task_run'}:
        validate_stages(value['task'], value['after'])
        if type(value['version']) is not int or not 0 < value['version'] < 1000000: raise ValueError('safe_event_version')
        if not isinstance(value['source_versions'], list) or not value['source_versions'] or not all(isinstance(x, str) and re.fullmatch('[0-9a-f]{64}', x) for x in value['source_versions']):
            raise ValueError('safe_event_evidence_required')
    if kind == 'task_updated':
        validate_stages(value['task'], value['before'], nullable=True)
        if type(value['initial']) is not bool: raise ValueError('safe_event_initial')
    if kind == 'task_run' and value['outcome'] not in OUTCOMES: raise ValueError('safe_event_outcome')
    if kind == 'repair_state' and (value['state'] not in REPAIR_STATES or value['before_state'] not in {*REPAIR_STATES, None}):
        raise ValueError('safe_repair_state')
    if kind == 'repair_state' and (not isinstance(value['changed'], list) or not set(value['changed']) <= set(FILE_TAGS.values())
                                   or any(type(value[k]) is not bool for k in ('old_failed', 'candidate_passed'))):
        raise ValueError('safe_repair_evidence')
    if kind == 'baseline' and any(type(value[k]) is not bool for k in ('identity_restored', 'paired_installed')):
        raise ValueError('safe_baseline_required')


def compile_batch(events):
    actions, files, progress = ['• 以下为该次核对记录，消息到达时间不代表实时状态。'], [], []
    for value in events:
        validate_event(value); kind = value['kind']
        if kind == 'task_updated':
            name = TASKS[value['task']]
            origin = '你安排的接线' if value['initial'] else 'AGI 主动发现变化'
            actions.append(f"• {name}｜{origin}；{stage_text(value['task'], value['before'])} → {stage_text(value['task'], value['after'])}。依据：已登记验收回执及来源版本核对；原因：让下一次执行跟随项目进度；执行者：AGI 固定维护流程。")
            files.append(f"• {name}：版本化任务说明，现为第 {value['version']} 版；原目标、时间、模型、权限和验收保持。")
            progress.append(f"• {name}：说明已更新；下一次真实定时执行使用新版本仍待验。未完成部分按原条件继续。")
        elif kind == 'task_run':
            name = TASKS[value['task']]
            actions.append(f"• {name}｜原任务负责人执行；从说明准备转为实际读取第 {value['version']} 版并写回结果。依据：原生归属、读取版本与进度回执绑定；原因：核对维护是否进入执行。")
            files.append(f"• {name}：本次任务运行回执；保留原始执行历史。")
            progress.append(f"• {name}：{stage_text(value['task'], value['after'])}；{OUTCOMES[value['outcome']]}。原生读取记录本身不证明定时触发，也不代替整体验收。")
        elif kind == 'source_error':
            name = TASKS[value['task']]
            actions.append(f"• {name}｜AGI 主动发现核查缺口；原可维护状态 → 证据或原调度绑定暂不能核对，保留原说明。依据：当前来源和身份检查；原因：防止旧状态覆盖新进度；执行者：AGI 固定维护流程。")
            progress.append(f"• {name}：待核查；未降低标准、重开取消任务或代替本人选择。")
        elif kind == 'source_recovered':
            name = TASKS[value['task']]
            actions.append(f'• {name}｜AGI 主动核查；此前来源或绑定缺口 → 当前读取核对恢复。依据：原登记范围、身份和来源指纹重新通过；原因：恢复安全维护；执行者：AGI 固定维护流程。')
            progress.append(f'• {name}：维护可继续；任务效果仍按原验收，不以恢复读取冒称完成。')
        elif kind == 'task_retired':
            name = TASKS[value['task']]
            actions.append(f'• {name}｜全部原验收当前通过，原负责人本轮登记调度退休。依据：本次原生执行结果、原闭环终态及原调度文件不存在的读回；原因：观察任务完成。')
            files.append(f'• {name}：原调度文件已不存在；正式接口回执与历史保留由原运行单独核验。')
            progress.append(f'• {name}：固定维护器停止维护，不重新创建、恢复或扩大任务；不以文件缺失冒称正式接口调用成功。')
        elif kind == 'repair_state':
            before = REPAIR_STATES.get(value['before_state'], '此前阶段未记录，本轮首次核对的流程快照')
            changed = '、'.join(value['changed']) or '具体改动尚未形成可核对回执'
            proof = ('旧版故障已复现' if value['old_failed'] else '旧版复现依据待核') + '；' + ('候选专项通过' if value['candidate_passed'] else '候选专项待核')
            actions.append(f"• AGI 软件修复｜受控流程记录：{before} → {REPAIR_STATES[value['state']]}；改动范围：{changed}。依据：受控修复账本及专项回执，{proof}；原因：处理原已登记缺陷，具体原因摘要未安全核对时保留缺口；执行者：AGI 受控修复器。")
            files.append('• ' + changed + '；完整差异和来源留在本机。')
            progress.append('• 该阶段仅证明账本记录；安装、回退和自然效果分别核验。没有完整证据前仍为未完成。')
        elif kind == 'baseline':
            restored = '已恢复' if value['identity_restored'] else '待验'
            installed = '已核对' if value['paired_installed'] else '待验'
            actions.append(f'• 你安排的改动｜AGI 自身源码登记：旧快捷入口被身份检查阻断 → 当前真实目录识别{restored}。依据：同一身份检查入口的改前失败、改后读回；原因：让受控修复识别自己的项目；执行者：Codex。')
            files.append('• AGI 项目登记、固定任务维护与通报模块、两项原定时任务说明和本机独立台账。')
            progress.append(f'• 本次配套安装{installed}；两项任务原工程阶段依据原负责人回执，历史修复不冒称自主发现；自然效果和下一次真实定时读取仍待验。无变化时保持安静。')
    body = '做了哪些事情\n' + '\n'.join(actions) + '\n\n修改了哪些文件\n' + '\n'.join(files or ['暂无可确认内容']) + '\n\n哪些项目有进展\n' + '\n'.join(progress)
    if len(body.encode()) > 18000: raise ValueError('safe_message_too_large')
    # Additional fail-closed guard; the compiler above is the privacy boundary.
    if re.search(r'(?i)(/Users/|/Volumes/|https?://|(?:ou|oc|cli)_[a-z0-9]+|BEGIN .*PRIVATE KEY|api[_ -]?key|password|[0-9a-f]{32,}|[\w.+-]+@[\w.-]+\.[a-z]{2,})', body):
        raise ValueError('private_content_in_summary')
    return body


def compile_functional(value):
    validate_event(value)
    if value['kind'] != 'functional_verified': raise ValueError('functional_proof_required')
    from .proactive_contracts import CONTRACTS
    _, name, reason = CONTRACTS[value['contract']]
    return ('做了什么：修复了'+name+'，已安装并通过安装后检查；长期使用效果尚未验收。\n'
            '为什么做：'+reason)


def verified_functional(store, payload):
    """Re-read current local proof immediately before dispatch, not a cached label."""
    from . import proactive, project_repair
    from .work import WorkQueue
    from .acceptance import release_ready
    if not proactive.active(store) or not release_ready(store): return False
    row = WorkQueue(store).get(payload['work_id'])
    if not row or row['state'] not in {'observing','verified'} or not row['reference'].startswith(proactive.PREFIX): return False
    result = row['result']; rounds = result.get('rounds', [])
    if result.get('origin') != proactive.POLICY or not rounds: return False
    last = rounds[-1]
    if any(last.get(k) is not True for k in ('old_failed','candidate_passed','fixed_contract_passed')): return False
    if result.get('hypothesis',{}).get('contract') != payload['contract']: return False
    if result.get('job') != payload['job'] or result.get('verification',{}).get('revision') != payload['revision']: return False
    if digest(result) != payload['proof']: return False
    path = project_repair.installation_receipt_path(store,'agi',payload['job'])
    receipt, _ = read_json(path)
    return project_repair.installation_ready(row, receipt)


def current_functional(payload):
    from .store import Store
    with Store(readonly=True) as store:
        return verified_functional(store, payload)


def collect_functional(store, ledger):
    from . import proactive
    for row in proactive.items(store):
        if row['state'] not in {'observing','verified'}: continue
        result = row['result']
        value = {'kind':'functional_verified','work_id':row['id'],'job':result.get('job',''),
                 'contract':result.get('hypothesis',{}).get('contract',''),
                 'revision':result.get('verification',{}).get('revision',''),'proof':digest(result)}
        validate_event(value)
        if verified_functional(store,value):
            ledger.event(value, digest(['functional',row['id'],value['job']]))


class ReadUnavailable(RuntimeError): pass
class UnknownWrite(RuntimeError): pass
class RejectedWrite(RuntimeError):
    def __init__(self, code): self.code = code; super().__init__('remote_rejected')


class Client:
    def __init__(self):
        env = Path.home() / '.hermes/profiles/feishubot/.env'
        if env.stat().st_mode & 0o077: raise PermissionError('private_miso_credentials_required')
        values = {}
        for line in read_file(env, 128 * 1024).decode().splitlines():
            if not line.strip() or line.lstrip().startswith('#') or '=' not in line: continue
            key, value = line.removeprefix('export ').split('=', 1)
            parts = shlex.split(value); values[key.strip()] = parts[0] if parts else ''
        self.app_id = values['FEISHU_APP_ID']; self.secret = values['FEISHU_APP_SECRET']
        owner_text = values.get('FEISHU_ALLOWED_USERS', '').strip()
        owners = owner_text.split(',')
        if len(owners) != 1 or values.get('FEISHU_ALLOW_ALL_USERS', '').lower() != 'false':
            raise PermissionError('single_verified_miso_owner_required')
        if not re.fullmatch(r'(?:ou_[a-zA-Z0-9]+|[a-zA-Z0-9]{8})', owners[0]):
            raise PermissionError('verified_miso_owner_id_format')
        self.owner = owners[0]; self.token = None
        self.owner_type = 'open_id' if self.owner.startswith('ou_') else 'user_id'

    def request(self, method, path, payload=None, authenticated=True):
        if authenticated and self.token is None:
            value = self.request('POST', '/auth/v3/tenant_access_token/internal',
                                 {'app_id': self.app_id, 'app_secret': self.secret}, authenticated=False)
            self.token = value['tenant_access_token']
        if not path.startswith('/') or '://' in path: raise PermissionError('fixed_api_required')
        req = urllib.request.Request(API + path, data=packed(payload).encode() if payload is not None else None,
                                     headers={'Content-Type': 'application/json', **({'Authorization': 'Bearer ' + self.token} if authenticated else {})}, method=method)
        # No redirect can move credentials or a message to another endpoint.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *_): return None
        try:
            with urllib.request.build_opener(NoRedirect).open(req, timeout=15) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024: raise ValueError('remote_response_too_large')
            value = json.loads(raw)
        except urllib.error.HTTPError as exc:
            if method == 'POST' and authenticated and exc.code >= 500: raise UnknownWrite('remote_write_unknown') from None
            if method == 'POST' and authenticated: raise RejectedWrite(exc.code) from None
            raise ReadUnavailable('remote_read_unavailable') from None
        except (OSError, ValueError, KeyError, socket.timeout, urllib.error.URLError):
            if method == 'POST' and authenticated: raise UnknownWrite('remote_write_unknown') from None
            raise ReadUnavailable('remote_read_unavailable') from None
        if value.get('code') != 0:
            if method == 'POST' and authenticated: raise RejectedWrite(value.get('code'))
            raise ReadUnavailable('remote_read_rejected')
        return value

    def bot(self):
        value = self.request('GET', '/bot/v3/info')['bot']
        if value.get('app_name') != 'Miso' or not value.get('open_id'): raise PermissionError('miso_identity_changed')
        return value

    def preflight(self, target):
        bot = self.bot()
        if target['app_id'] != self.app_id or target['owner'] != self.owner or target['bot_open_id'] != bot['open_id'] or target['user_id_type'] != self.owner_type:
            raise PermissionError('miso_target_identity_changed')
        cid = urllib.parse.quote(target['chat_id'], safe='')
        chat = self.request('GET', f'/im/v1/chats/{cid}?user_id_type={self.owner_type}')['data']
        if (chat.get('name') != NAME or chat.get('chat_mode') != 'group' or chat.get('chat_type') != 'private'
                or chat.get('external') is not False or chat.get('owner_id') != self.owner
                or str(chat.get('user_count')) != '1' or str(chat.get('bot_count')) != '1'):
            raise PermissionError('private_group_membership_changed')
        members = self.request('GET', f'/im/v1/chats/{cid}/members?member_id_type={self.owner_type}&page_size=100')['data']
        if members.get('has_more') or {m['member_id'] for m in members['items']} != {self.owner}:
            raise PermissionError('group_human_membership_changed')
        presence = self.request('GET', f'/im/v1/chats/{cid}/members/is_in_chat')['data']
        if presence.get('is_in_chat') is not True: raise PermissionError('miso_not_in_group')
        return {'verified_at': time.time(), 'humans': 1, 'bots': 1, 'private': True, 'miso': True}

    def send(self, target, text, request_uuid):
        value = self.request('POST', '/im/v1/messages?receive_id_type=chat_id',
                             {'receive_id': target['chat_id'], 'msg_type': 'text', 'content': packed({'text': text}), 'uuid': request_uuid})
        mid = value.get('data', {}).get('message_id')
        if not isinstance(mid, str) or not mid: raise UnknownWrite('missing_message_id')
        return mid

    def get_message(self, mid):
        value = self.request('GET', '/im/v1/messages/' + urllib.parse.quote(mid, safe=''))['data']
        items = value.get('items', [])
        if len(items) != 1: raise ReadUnavailable('message_readback_missing')
        return items[0]

    def matches(self, message, target, text):
        try: body = json.loads(message['body']['content'])
        except (KeyError, ValueError, TypeError): return False
        sender = message.get('sender', {})
        return (message.get('chat_id') == target['chat_id'] and message.get('msg_type') == 'text'
                and not message.get('deleted', False) and body == {'text': text}
                and sender.get('sender_type') == 'app' and sender.get('id') in {self.app_id, target['bot_open_id']})

    def reconcile(self, target, text, attempted):
        query = {'container_id_type': 'chat', 'container_id': target['chat_id'], 'page_size': 100,
                 'start_time': int(attempted - 2), 'end_time': int(attempted + 300), 'sort_type': 'ByCreateTimeAsc'}
        found = []
        for _ in range(5):
            data = self.request('GET', '/im/v1/messages?' + urllib.parse.urlencode(query))['data']
            found.extend(m['message_id'] for m in data.get('items', []) if self.matches(m, target, text))
            if not data.get('has_more'):
                # Feishu history does not expose the send UUID. Similar content
                # can locate a candidate but cannot prove the original request.
                return {'candidate_message_id': found[0], 'original_request_verified': False} if len(set(found)) == 1 else None
            query['page_token'] = data['page_token']
        return None

    def groups(self):
        query = {'page_size': 100, 'sort_type': 'ByCreateTimeDesc'}; items = []
        for _ in range(10):
            data = self.request('GET', '/im/v1/chats?' + urllib.parse.urlencode(query))['data']
            items.extend(data.get('items', []))
            if not data.get('has_more'): return items
            query['page_token'] = data['page_token']
        raise ReadUnavailable('group_inventory_incomplete')


def target_for(ledger):
    target, sha = read_json(ledger.root / 'target.json')
    if sha != ledger.get('target_sha256') or set(target) != {'chat_id', 'app_id', 'owner', 'bot_open_id', 'name', 'user_id_type'} or target['name'] != NAME:
        raise PermissionError('dedicated_target_binding_changed')
    mapping, _ = read_json(Path.home() / '.hermes/feishu-channel-targets.json')
    # Existing mappings can be strings or mapping objects; this dedicated entry is explicit.
    mapped = mapping.get(TARGET_KEY)
    if mapped != target: raise PermissionError('formal_channel_target_changed')
    return target


def deliver_pending(ledger, client=None):
    fd = os.open(ledger.root / 'delivery.lock', os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return {'status': 'busy'}
        target = target_for(ledger); client = client or Client(); client.preflight(target)
        # A killed process may have crossed the dispatch boundary. It is unknown.
        ledger.db.execute("UPDATE events SET state='unknown' WHERE state='dispatch_started'")
        unknown = ledger.db.execute("SELECT * FROM events WHERE state IN ('unknown','readback_pending') ORDER BY created LIMIT 1").fetchone()
        if unknown:
            receipt = json.loads(unknown['receipt']); text = receipt['text']
            if receipt.get('target_sha256') != ledger.get('target_sha256'):
                raise PermissionError('unknown_original_target_changed')
            mid = unknown['message_id']
            if not mid:
                match = client.reconcile(target, text, unknown['attempted'])
                if match and match.get('original_request_verified') is True:
                    mid = match['candidate_message_id']
                elif match:
                    ledger.db.execute('UPDATE events SET receipt=? WHERE id=?', (packed({**receipt, 'candidate_message_id': match['candidate_message_id'], 'original_request_verified': False}), unknown['id']))
                    return {'status': 'unknown', 'matching_candidate_only': True, 'replayed': False}
            if not mid: return {'status': 'unknown', 'replayed': False}
            if not client.matches(client.get_message(mid), target, text): return {'status': 'readback_mismatch', 'replayed': False}
            complete_batch(ledger, receipt['events'], mid, receipt)
            return {'status': 'delivered', 'readback': True, 'reconciled': True}
        # Progress remains in the local ledger. Preserve all already-attempted writes.
        pending = ledger.db.execute("SELECT * FROM events WHERE state='pending' ORDER BY created").fetchall()
        rows = []
        for row in pending:
            value = json.loads(row['payload'])
            if value.get('kind') != 'functional_verified':
                ledger.db.execute("UPDATE events SET state='local_only' WHERE id=? AND state='pending'", (row['id'],))
                continue
            if row['retry_at'] > time.time(): continue
            if not current_functional(value): continue
            rows = [row]; break
        if not rows: return {'status': 'quiet'}
        text = compile_functional(json.loads(rows[0]['payload']))
        receipt = {'events': [r['id'] for r in rows], 'text': text, 'text_sha256': digest(text), 'target_sha256': ledger.get('target_sha256'), 'request_uuid': rows[0]['request_uuid']}
        attempted = time.time()
        with ledger.tx():
            for row in rows:
                if ledger.db.execute("UPDATE events SET state='dispatch_started',attempted=?,receipt=? WHERE id=? AND state='pending'", (attempted, packed(receipt), row['id'])).rowcount != 1:
                    raise ValueError('delivery_queue_changed')
        try: mid = client.send(target, text, rows[0]['request_uuid'])
        except RejectedWrite as exc:
            with ledger.tx():
                for row in rows:
                    ledger.db.execute("UPDATE events SET state='pending',retry_at=?,receipt=? WHERE id=?", (time.time() + 300, packed({**receipt, 'not_dispatched': True, 'remote_code': exc.code}), row['id']))
            return {'status': 'not_dispatched', 'remote_code': exc.code}
        except (UnknownWrite, OSError, RuntimeError):
            ledger.db.execute("UPDATE events SET state='unknown' WHERE state='dispatch_started'")
            return {'status': 'unknown', 'replayed': False}
        with ledger.tx():
            for row in rows: ledger.db.execute("UPDATE events SET state='readback_pending',message_id=? WHERE id=?", (mid, row['id']))
        try:
            if not client.matches(client.get_message(mid), target, text): return {'status': 'readback_mismatch', 'replayed': False}
        except ReadUnavailable: return {'status': 'readback_pending', 'replayed': False}
        complete_batch(ledger, receipt['events'], mid, receipt)
        return {'status': 'delivered', 'readback': True, 'count': len(rows)}
    finally: os.close(fd)


def complete_batch(ledger, identifiers, mid, receipt):
    proof = {**receipt, 'message_id': mid, 'readback_verified': True, 'verified_at': time.time()}
    with ledger.tx():
        for key in identifiers:
            ledger.db.execute("UPDATE events SET state='delivered',message_id=?,receipt=? WHERE id=? AND state IN ('unknown','dispatch_started','readback_pending')", (mid, packed(proof), key))


def bootstrap_group(ledger, client=None):
    """Operator-only, one create intent. Unknown creates never issue another POST."""
    client = client or Client(); bot = client.bot()
    existing = ledger.root / 'target.json'
    if existing.exists():
        target = target_for(ledger); client.preflight(target); return {'already_configured': True}
    intent_path = ledger.root / 'group-create.json'
    if intent_path.exists():
        intent, _ = read_json(intent_path)
        if intent['app_id'] != client.app_id or intent['owner'] != client.owner: raise PermissionError('create_intent_identity_changed')
        if not intent.get('chat_id'):
            if not isinstance(intent.get('prior_groups'), list): raise UnknownWrite('original_create_inventory_missing')
            candidates = [g for g in client.groups() if g.get('name') == NAME and g.get('chat_id') not in intent.get('prior_groups', [])]
            if len(candidates) != 1: raise UnknownWrite('group_create_unknown_requires_original_readback')
            candidate = {'chat_id': candidates[0]['chat_id'], 'app_id': client.app_id, 'owner': client.owner,
                         'bot_open_id': bot['open_id'], 'name': NAME, 'user_id_type': client.owner_type}
            client.preflight(candidate)
            intent.update(chat_id=candidate['chat_id'], state='reconciled'); atomic(intent_path, packed(intent))
    else:
        groups = client.groups()
        if any(g.get('name') == NAME for g in groups): raise PermissionError('existing_named_group_requires_identity_review')
        intent = {'app_id': client.app_id, 'owner': client.owner, 'name': NAME, 'uuid': str(uuid.uuid4()), 'state': 'dispatch_started',
                  'prior_groups': [g['chat_id'] for g in groups], 'at': time.time()}
        atomic(intent_path, packed(intent))
        # Creator bot is added automatically; the owner is the only human.
        value = client.request('POST', '/im/v1/chats?' + urllib.parse.urlencode({'user_id_type': client.owner_type, 'set_bot_manager': 'true', 'uuid': intent['uuid']}),
                               {'name': NAME, 'owner_id': client.owner, 'user_id_list': [client.owner],
                                'chat_mode': 'group', 'chat_type': 'private', 'external': False,
                                'description': '由 Miso 通报 AGI 经证据核验的任务维护与受控改进；无变化保持安静。'})
        cid = value.get('data', {}).get('chat_id')
        if not cid: raise UnknownWrite('group_create_missing_id')
        intent.update(chat_id=cid, state='created'); atomic(intent_path, packed(intent))
    target = {'chat_id': intent['chat_id'], 'app_id': client.app_id, 'owner': client.owner, 'bot_open_id': bot['open_id'], 'name': NAME, 'user_id_type': client.owner_type}
    proof = client.preflight(target)
    map_path = Path.home() / '.hermes/feishu-channel-targets.json'
    mapping, sha = read_json(map_path)
    if TARGET_KEY in mapping and mapping[TARGET_KEY] != target: raise PermissionError('target_mapping_conflict')
    if read_json(map_path)[1] != sha: raise ValueError('channel_mapping_changed')
    atomic(map_path, packed({**mapping, TARGET_KEY: target}))
    atomic(existing, packed(target)); ledger.set('target_sha256', read_json(existing)[1]); ledger.set('group_verified', proof)
    return {'created': True, **proof}


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('action', choices=('create-group', 'deliver'))
    args = parser.parse_args()
    with Ledger() as ledger:
        if args.action == 'create-group': return bootstrap_group(ledger)
        from .task_maintenance import authorized
        from .store import Store
        with Store() as store:
            root, _ = authorized(store)
            if root != ledger.root or ledger.get('enabled') is not True: raise PermissionError('maintenance_not_active')
        return deliver_pending(ledger)


if __name__ == '__main__':
    try: print(packed(main()))
    except Exception as exc:
        print(packed({'status': 'blocked', 'reason': str(exc) if isinstance(exc, (PermissionError, ValueError, UnknownWrite)) else type(exc).__name__})); raise SystemExit(1)
