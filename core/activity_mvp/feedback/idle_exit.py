"""Content-free cooperative UI exit receipts. A receipt is never proof of process exit."""
import json
import math
import os
import stat
import time
import uuid
from pathlib import Path

REASONS = {
    'unsaved': ('还有未保存的内容。', '完成原表单的保存／确认，或明确放弃后可继续更新。'),
    'saving': ('最新内容正在保存。', '收到保存成功回执后解除此项拦截，无需重复点击。'),
    'save_failed': ('保存未成功，内容仍受保护。', '原保存入口重试成功后可继续。'),
    'unknown': ('暂时无法确认最新内容是否保存。', '系统需先核对保存回执，不会据此强行退出。'),
    'interface_in_use': ('当前界面仍在使用。', '完成操作并关闭窗口／菜单后重新检查；最小化仍视为正在使用。'),
    'request_expired': ('本次退出检查已过期。', '等待原更新流程发起新的检查。'),
    'state_unavailable': ('无法确认退出条件。', '等待应用返回本次检查结果；旧版无回执不能据此判断有草稿。'),
    'exit_not_completed': ('应用尚未完成退出。', '等待正常退出并核对进程状态，不会强行关闭。'),
}
INTERFACE_SOURCES = {
    'menu_open': '展开的菜单',
    'application_active': '前台应用',
    'window': '可见或最小化窗口',
    'modal': '对话框',
    'edited': '系统标记的未保存文档',
}

def unavailable(code='state_unavailable', *, request=None):
    # This is a publisher observation, never a native acknowledgement. An old
    # application has no known instance ID and cannot be inferred to have drafts.
    value = {'schema':1, 'can_exit':False, 'reasons':[code],
             'checked_at':time.time(), 'receipt_source':'publisher'}
    if request is not None:
        value.update(instance=None, pid=request['pid'], revision=request['revision'],
                     nonce=request['nonce'], request_created=request['created'])
    return value

def read_receipt(path, request, now=None):
    now=time.time() if now is None else now
    fd=None
    try:
        path=Path(path)
        if any(parent.is_symlink() for parent in path.parents):return None
        fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        st=os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid() or st.st_mode&0o077 or st.st_nlink!=1 or not 0<st.st_size<4096:return None
        data=os.read(fd,4096)
        if len(data)!=st.st_size:return None
        def unique_pairs(pairs):
            result={}
            for key,value in pairs:
                if key in result:raise ValueError('duplicate_field')
                result[key]=value
            return result
        value=json.loads(data,object_pairs_hook=unique_pairs)
        fields={'schema','instance','pid','revision','nonce','request_created','checked_at','reasons','can_exit'}
        if not isinstance(value,dict) or set(value) not in (fields, fields|{'interface_sources'}):return None
        if type(value['schema']) is not int or value['schema']!=1 or type(value['pid']) is not int:return None
        if any(value[k]!=request[k] for k in ('pid','revision','nonce')) or value['request_created']!=request['created']:return None
        if type(value['instance']) is not str or str(uuid.UUID(value['instance']))!=value['instance']:return None
        checked=value['checked_at']
        if type(checked) not in (float,int) or not math.isfinite(checked) or not request['created']<=checked<=now or now-checked>3:return None
        reasons=value['reasons']
        if type(reasons) is not list or len(reasons)>len(REASONS) or any(type(r) is not str or r not in REASONS for r in reasons) or len(set(reasons))!=len(reasons):return None
        if type(value['can_exit']) is not bool or value['can_exit']!=(not reasons):return None
        if 'interface_sources' in value:
            sources=value['interface_sources']
            if type(sources) is not list or len(sources)>len(INTERFACE_SOURCES):return None
            if any(type(s) is not str or s not in INTERFACE_SOURCES for s in sources) or len(set(sources))!=len(sources):return None
            if bool(sources)!=('interface_in_use' in reasons):return None
        return value
    except (OSError,ValueError,TypeError,KeyError):return None
    finally:
        if fd is not None:os.close(fd)


def presentation(receipt):
    codes=receipt.get('reasons',['state_unavailable'])
    codes=[code for code in codes if code in REASONS] or ([] if receipt.get('can_exit') is True else ['state_unavailable'])
    summary='；'.join(REASONS[c][0] for c in codes) if codes else '已具备安全退出条件。'
    sources=receipt.get('interface_sources')
    if 'interface_in_use' in codes and type(sources) is list:
        labels=[label for code,label in INTERFACE_SOURCES.items() if code in sources]
        if labels:summary+='当前占用：'+'、'.join(labels)+'。'
    return {'summary':summary,
            'next_trigger':'；'.join(REASONS[c][1] for c in codes) if codes else '等待原更新流程再次核对。',
            'checked_at':receipt.get('checked_at'), 'reason_codes':codes}
