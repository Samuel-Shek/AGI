"""Explicit one-click handoff through the installed fixed Core controller."""
from .core_reader import ReadError
from .storage import store_draft
# This adapter invokes the installed, fixed controller only on an explicit click.
# It does not implement its own Codex IPC, model configuration or permission gate.
import json
import uuid
from pathlib import Path
from .core_reader import bounded_run, decode, valid_id


def core_command(config, action, identifier, payload=None):
    if action not in {'handoff-status', 'handoff-submit', 'handoff-open', 'handoff-retry', 'closure-open'} or not valid_id(identifier):
        raise ReadError('command_not_allowed')
    argv = config.argv('detail', identifier)
    argv[argv.index('attention-center') + 1] = action
    raw = bounded_run(argv, config.timeout, config.output_limit,
                      json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None,
                      cwd=str(Path(config.launcher).expanduser().resolve().parent))
    value = decode(raw)
    if not isinstance(value, dict):
        raise ReadError('invalid_contract')
    return value


def public_result(result):
    if result.get('status') != 'ok':
        return {'phase':'unavailable', 'message':('补充中含有不适合直接转交的私密信息，请修改后再点击。原输入保留在本机。' if '补充中含有不适合直接转交的私密信息' in str(result.get('error', '')) else '交接暂未完成，输入已保留。请刷新核对原事项的接手状态。')}
    current = result.get('handoff')
    if not current:
        return {'phase':'empty', 'message':'尚未交接。'}
    if current.get('status') == 'delivered' and current.get('receipt'):
        return {'phase':'delivered', 'message':'Codex 已收到这条回复。'}
    if current.get('status') in {'queued','preparing','opening','sending','verifying'}:
        return {'phase':'pending', 'message':'正在打开 Codex 并核对接收回执…'}
    if current.get('status') == 'failed':
        return {'phase':'failed', 'message':'交接尚未完成，输入已保留。再次点击会用原提交核对并重试，不新建重复会话。'}
    return {'phase':'uncertain', 'message':'接收结果待核对；再次点击会核对原提交，不重复发送。'}


def handoff_status(state, identifier):
    with state.lock:
        item = state.detail(identifier)
        if item.get('source_missing'):
            return {'phase':'unavailable', 'message':'原事项来源暂不可读取，不能交接或写入。'}
        if state.reader.fixture:
            return {'phase':'unavailable', 'message':'合成界面展示不会发起真实交接。'}
        try:
            value = public_result(core_command(state.reader.config, 'handoff-status', identifier))
        except ReadError:
            value = {'phase':'uncertain', 'message':'接收结果待核对；输入已保留，请稍后核对。'}
        if value['phase'] == 'empty' and identifier in state.store.read().get('handoffs', {}):
            value = {'phase':'uncertain', 'message':'上次交接结果尚未确认，输入已保留；没有重复提交。'}
        return value


def continue_in_codex(state, identifier, payload):
    content = payload.get('content', '')
    if not isinstance(content, str) or len(content) > 4000:
        raise ValueError('invalid_content')
    draft = payload.get('draft')
    if draft is not None and (not isinstance(draft, str) or len(draft) > 4000):
        raise ValueError('invalid_draft')
    with state.lock:
        item = state.detail(identifier)
        if item.get('source_missing'):
            raise ReadError('scope_withdrawn')
        if item['revision'] != payload.get('revision'):
            raise ReadError('stale_revision')
        if state.reader.fixture:
            return {'phase':'unavailable', 'message':'合成界面展示不会发起真实交接。'}
        # Preserve typed content before any request that could outlive this App.
        if draft is not None:
            state.store.update(lambda data: store_draft(data, identifier, draft, payload.get('draft_edit')))
        try:
            result = core_command(state.reader.config, 'handoff-status', identifier)
            if result.get('status') != 'ok':
                return public_result(result)
            current = result.get('handoff')
            intent = state.store.read().get('handoffs', {}).get(identifier)
            if current and not (current.get('status') == 'delivered' and current.get('receipt')):
                previous = result
                if current.get('status') == 'queued' and intent and intent.get('client_message_id') == current.get('submission_id'):
                    # Re-wake an interrupted queued handoff through Core's
                    # idempotent SAME-ID submit, never invent a new submission.
                    previous = core_command(state.reader.config, 'handoff-submit', identifier, intent)
                elif current.get('status') in {'failed', 'uncertain'}:
                    # Only a fresh user click retries the SAME durable submission.
                    # Core reconciles creation/dispatch uncertainty before any send.
                    previous = core_command(state.reader.config, 'handoff-retry', identifier,
                                            {'submission_id':current['submission_id']})
                if content.strip() and content != current.get('content'):
                    return {'phase':'prior_reply', 'message':'正在核对上一条回复。新回复已保留，确认上一条后再点击发送。'}
                return public_result(previous)
            if intent and (not current or current.get('submission_id') != intent['client_message_id']):
                return {'phase':'uncertain', 'message':'上次交接结果尚未确认，输入已保留；没有重复提交。'}
            closure = item.get('kind') == 'closure'
            thread = item.get('codex_thread_id')
            can_reply = item.get('codex_reply_available', False)
            already_delivered = current and content == current.get('content')
            navigation_only = thread and not can_reply
            if (thread or current) and (not content.strip() or already_delivered or navigation_only):
                action = 'closure-open' if closure else 'handoff-open'
                target = {'revision':item['native_revision'], 'thread_id':thread} if closure else None
                opened = core_command(state.reader.config, action, identifier, target)
                if opened.get('status') != 'ok':
                    return public_result(opened)
                message = '已打开会话。'
                if already_delivered:
                    message = '已打开会话；这条回复已送达。'
                elif navigation_only and content.strip():
                    message = '已打开会话；当前无法从这里发送回复，输入仍保存在本机。'
                return {'phase':'opened', 'message':message}
            if not can_reply and not item.get('codex_handoff_available', not closure):
                return {'phase':'unavailable', 'message':'这项回复暂不能交给 Codex，草稿已保留在本机。'}
            intent = {'client_message_id':str(uuid.uuid4()), 'content':content,
                      'issue_revision':item['native_revision']}
            if closure and can_reply:
                intent['workbench_reply'] = True
            # Commit intent BEFORE submit. A timeout/crash must never invent a new ID.
            state.store.update(lambda data: data.setdefault('handoffs', {}).__setitem__(identifier, intent))
            submitted = core_command(state.reader.config, 'handoff-submit', identifier, intent)
            if submitted.get('status') != 'ok':
                # A definitive rejection plus trustworthy empty history is safe
                # to release; transport errors never reach this branch.
                checked = core_command(state.reader.config, 'handoff-status', identifier)
                if (checked.get('status') == 'ok' and checked.get('handoff') == current
                        and not checked.get('history_unknown')):
                    state.store.update(lambda data: data.get('handoffs', {}).pop(identifier, None))
            return public_result(submitted)
        except ReadError:
            return {'phase':'uncertain', 'message':'接收结果待核对；输入已保留，没有重复提交。'}
