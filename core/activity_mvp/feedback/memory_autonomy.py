"""Autonomous investigation of versioned memory candidates, without raw archives."""
from __future__ import annotations
import json
from pathlib import Path
import subprocess
import sqlite3
import time
from ..memory_review import memory_review_candidates, read_memory_text, MemoryReviewError, DEFAULT_LEDGER_PATH
from .work import WorkQueue,POLICY
from .memory_operator import load_registry,inspect_candidate,sha


import sys
from activity_mvp.source_governance import personal_sources as personal_scope

OPERATOR_PYTHON=Path.home()/'.hermes/hermes-agent/venv/bin/python'
OPERATOR_ROOT=Path.home()/'.hermes/memory-review-center'


def apply(proposal, revision, should_continue=lambda: True):
    if not should_continue(): raise RuntimeError("autonomy_paused")
    process=subprocess.Popen([str(OPERATOR_PYTHON),'-m','review_center.autonomy'],cwd=OPERATOR_ROOT,
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    process.stdin.write(json.dumps({'memory_id':proposal['memory_id'],'expected_revision':revision,'policy':POLICY}))
    process.stdin.close(); process.stdin=None
    deadline=time.monotonic()+150
    while process.poll() is None:
        if not should_continue() or time.monotonic()>deadline:
            process.terminate()
            try: process.communicate(timeout=3)
            except subprocess.TimeoutExpired: process.kill();process.communicate()
            # The fixed writer may have committed already. Preserve the operation
            # key and reconcile the receipt on resume, never mint a new attempt.
            raise RuntimeError('operator_interrupted_receipt_requires_recheck')
        time.sleep(.2)
    output,_=process.communicate()
    try: receipt=json.loads(output)
    except ValueError as exc: raise RuntimeError('operator_receipt_missing') from exc
    if process.returncode or not receipt.get('verified') or receipt.get('actor')!='agi-memory-autonomy' or receipt.get('human_confirmed') is not False:
        raise RuntimeError(receipt.get('error','operator_receipt_invalid'))
    if receipt.get('evidence_revision')!=revision or receipt.get('policy')!=POLICY:
        raise RuntimeError('operator_receipt_version_mismatch')
    return receipt


def active(store):
    return store.active() and store.get('autonomy',{}).get('policy')==POLICY and not store.get('autonomy',{}).get('paused',False)


def current_decision_is_automatic(candidate):
    try:
        with sqlite3.connect(DEFAULT_LEDGER_PATH.as_uri()+'?mode=ro',uri=True,timeout=1) as db:
            return db.execute('SELECT 1 FROM lifecycle_overrides l JOIN automatic_authorizations a ON a.batch_id=l.batch_id WHERE l.memory_id=? AND l.content_sha256=? AND a.policy=? AND a.actor=?',
                (candidate['memory_id'],candidate['content_sha256'],POLICY,'agi-memory-autonomy')).fetchone() is not None
    except sqlite3.Error: return False


def _tick_original(store, limit=5):
    """At most five investigations; unchanged facts make no model/write calls.

    Source revision includes actual source bytes, registry bindings, scope, and
    candidate identity. Technical failures wait in the system queue, never
    synthesize a human decision. Empty evidence never implies approval.
    """
    if not active(store):
        return {'enabled':False,'processed':0}
    queue=WorkQueue(store)
    try:
        registry=load_registry()
        candidates=memory_review_candidates(limit=None,state='all')['items']
    except (OSError,ValueError,MemoryReviewError) as exc:
        row=queue.enqueue('memory_system','source_access',sha(type(exc).__name__))
        if row['state']!='waiting_evidence':
            queue.update(row['id'],'waiting_evidence',summary='记忆审核来源暂不可用，系统等待恢复；不需要你猜测。',result={'error':type(exc).__name__})
        return {'enabled':True,'processed':0,'error':type(exc).__name__}
    processed=0; applied=0
    known_items={r['reference']:r for r in queue.items('memory')}
    operator_path=OPERATOR_ROOT/'review_center/autonomy.py'
    try: operator_version=sha(operator_path.read_bytes())
    except OSError: operator_version='unavailable'
    runtime_revision=sha([operator_version,registry])
    for candidate in candidates:
        if not active(store) or processed>=max(0,min(int(limit),5)): break
        # Existing human decisions remain intact. Already processed automatic
        # candidates are still watched so source changes invalidate old proof.
        if not personal_scope.memory_admitted(candidate['memory_id'],candidate,registry):continue
        known=known_items.get(candidate['memory_id'])
        if candidate.get('review_state')=='decided':
            if not known: continue
            if not current_decision_is_automatic(candidate):
                if known['state']!='resolved':queue.update(known['id'],'resolved',summary='已有外部审核决定，系统保留该决定。',result={'verification_summary':'外部审核结果优先，未自动覆盖。'})
                continue
        memory = None
        try:
            memory=read_memory_text(candidate['memory_id'],admission_registry=load_registry())
            proof=inspect_candidate(candidate,memory,registry)
        except (OSError,ValueError,MemoryReviewError,KeyError) as exc:
            proof={'revision':sha([candidate.get('candidate_fingerprint'),type(exc).__name__]),
                'proposal':None,'evidence':[],'gap':str(exc) if isinstance(exc,(ValueError,MemoryReviewError)) else type(exc).__name__}
        try:
            from .jev_observer import observe
            if memory is None or sha(memory['text']) != candidate['content_sha256']:
                raise ValueError('no_current_memory')
            observe('agi', 'analysis_priority', {'text': memory['text']},
                    source_version=sha([proof['revision'],runtime_revision]),
                    event_id=sha([candidate['memory_id'],proof['revision']]),
                    data_class='memory_text', still_valid=lambda: active(store) and personal_scope.memory_admitted(candidate["memory_id"],memory,load_registry()))
        except Exception:
            pass
        revision=sha([proof['revision'],runtime_revision])
        for entry in proof['evidence']:
            entry['summary']='已定位来源并核对版本。' if entry.get('valid') else '已查找直接来源，仍需核对具体事实。'
        row=queue.enqueue('memory',candidate['memory_id'],revision)
        if row['processed_revision']==revision and row['state'] not in {'queued','investigating','running','verifying'}: continue
        processed+=1
        queue.update(row['id'],'investigating',summary='系统正在核对这条记忆及其当前依据。',evidence=proof['evidence'])
        if not proof['proposal']:
            queue.update(row['id'],'waiting_evidence',summary='已核对现有记忆和可定位来源；证据还不足，等待相关来源更新。',
                processed_revision=revision,result={'gap':proof['gap'],'verification_summary':'当前依据尚不能确认继续有效或替代关系。','next_trigger':'memory_or_authoritative_source_changed'},attempts=row['attempts']+1)
            continue
        queue.update(row['id'],'running',summary='依据已核对，正在通过记忆审核中心处理。')
        try:
            if not active(store):
                queue.update(row['id'],'queued',summary='自主处理已暂停，保留当前进度。');break
            result=apply(proof['proposal'],proof['revision'],should_continue=lambda:active(store) and personal_scope.memory_admitted(candidate['memory_id'],candidate,load_registry()))
            result['verification_summary']='已重新读取审核结果，来源与记忆版本匹配。'
            result['changes']='确认继续有效' if proof['proposal']['action']=='keep_active' else ('保留原文并标为历史' if proof['proposal']['action']=='mark_legacy' else '新建正确记忆并关联旧记录')
            queue.update(row['id'],'verified',summary='已通过审核中心处理，并重新读取结果核对。',
                processed_revision=revision,result=result,attempts=row['attempts']+1)
            applied+=1
        except (RuntimeError,OSError,subprocess.SubprocessError) as exc:
            queue.update(row['id'],'verifying' if str(exc)=='operator_interrupted_receipt_requires_recheck' else 'waiting_evidence',summary='处理没有完成，系统保留原始记录并等待可核对的新结果。',
                processed_revision=revision,result={'gap':str(exc),'verification_summary':'尚未取得可核对的完成凭据。','next_trigger':'evidence_or_operator_recovery_changed'},attempts=row['attempts']+1)
    return {'enabled':True,'processed':processed,'applied':applied,'total':len(candidates)}


def _tick_ranked(store, limit=5, ranking_client=None):
    """At most five investigations; unchanged facts make no model/write calls.

    Source revision includes actual source bytes, registry bindings, scope, and
    candidate identity. Technical failures wait in the system queue, never
    synthesize a human decision. Empty evidence never implies approval.
    """
    if not active(store):
        return {'enabled':False,'processed':0}
    queue=WorkQueue(store)
    try:
        registry=load_registry()
        candidates=memory_review_candidates(limit=None,state='all')['items']
    except (OSError,ValueError,MemoryReviewError) as exc:
        row=queue.enqueue('memory_system','source_access',sha(type(exc).__name__))
        if row['state']!='waiting_evidence':
            queue.update(row['id'],'waiting_evidence',summary='记忆审核来源暂不可用，系统等待恢复；不需要你猜测。',result={'error':type(exc).__name__})
        return {'enabled':True,'processed':0,'error':type(exc).__name__}
    processed=0; applied=0
    known_items={r['reference']:r for r in queue.items('memory')}
    operator_path=OPERATOR_ROOT/'review_center/autonomy.py'
    try: operator_version=sha(operator_path.read_bytes())
    except OSError: operator_version='unavailable'
    runtime_revision=sha([operator_version,registry])
    from . import jev_ranking
    candidates=jev_ranking.oldest(store,candidates)
    prepared=[]
    for candidate in candidates:
        if not active(store) or len(prepared)>=10: break
        # Existing human decisions remain intact. Already processed automatic
        # candidates are still watched so source changes invalidate old proof.
        if not personal_scope.memory_admitted(candidate['memory_id'],candidate,registry):continue
        known=known_items.get(candidate['memory_id'])
        if candidate.get('review_state')=='decided':
            if not known: continue
            if not current_decision_is_automatic(candidate):
                if known['state']!='resolved':queue.update(known['id'],'resolved',summary='已有外部审核决定，系统保留该决定。',result={'verification_summary':'外部审核结果优先，未自动覆盖。'})
                continue
        memory = None
        try:
            memory=read_memory_text(candidate['memory_id'],admission_registry=load_registry())
            proof=inspect_candidate(candidate,memory,registry)
        except (OSError,ValueError,MemoryReviewError,KeyError) as exc:
            proof={'revision':sha([candidate.get('candidate_fingerprint'),type(exc).__name__]),
                'proposal':None,'evidence':[],'gap':str(exc) if isinstance(exc,(ValueError,MemoryReviewError)) else type(exc).__name__}
        revision=sha([proof['revision'],runtime_revision])
        if known and known['processed_revision']==revision and known['state'] not in {'queued','investigating','running','verifying'}:continue
        prepared.append({'candidate':candidate,'memory':memory,'proof':proof,'revision':revision})
    def ranking_current():
        # Each worker opens its own read-only connection; pause and source updates revoke the batch.
        from .store import Store
        try:
            with Store(store.root,readonly=True) as current:
                if not active(current):return False
            if sha(load_registry())!=sha(registry):return False
            latest={c['memory_id']:c for c in memory_review_candidates(limit=None,state='all')['items']}
            return all(personal_scope.memory_admitted(e['candidate']['memory_id'],e['candidate'],load_registry()) and latest.get(e['candidate']['memory_id'],{}).get('candidate_fingerprint')==e['candidate']['candidate_fingerprint'] and sha(read_memory_text(e['candidate']['memory_id'],admission_registry=load_registry())['text'])==e['candidate']['content_sha256'] for e in prepared)
        except Exception:return False
    def may_send():
        from .store import Store
        try:
            with Store(store.root,readonly=True) as current:return active(current)
        except Exception:return False
    selected,batch=jev_ranking.select(store,prepared,limit,ranking_current,client=ranking_client,may_send=may_send)
    for entry in selected:
        if not active(store):break
        candidate=entry['candidate'];memory=entry['memory'];proof=entry['proof'];revision=entry['revision']
        if not personal_scope.memory_admitted(candidate['memory_id'],candidate,load_registry()):continue
        # Recheck the original authority immediately before existing processing.
        try:
            fresh=read_memory_text(candidate['memory_id'],admission_registry=load_registry())
            if sha(fresh['text'])!=candidate['content_sha256']:continue
            current_proof=inspect_candidate(candidate,fresh,registry)
            if sha([current_proof['revision'],runtime_revision])!=revision:continue
        except (OSError,ValueError,MemoryReviewError,KeyError):
            if memory is not None:continue
        for entry in proof['evidence']:
            entry['summary']='已定位来源并核对版本。' if entry.get('valid') else '已查找直接来源，仍需核对具体事实。'
        row=queue.enqueue('memory',candidate['memory_id'],revision)
        if row['processed_revision']==revision and row['state'] not in {'queued','investigating','running','verifying'}: continue
        processed+=1
        jev_ranking.processed(store,batch,candidate['memory_id'])
        queue.update(row['id'],'investigating',summary='系统正在核对这条记忆及其当前依据。',evidence=proof['evidence'])
        if not proof['proposal']:
            queue.update(row['id'],'waiting_evidence',summary='已核对现有记忆和可定位来源；证据还不足，等待相关来源更新。',
                processed_revision=revision,result={'gap':proof['gap'],'verification_summary':'当前依据尚不能确认继续有效或替代关系。','next_trigger':'memory_or_authoritative_source_changed'},attempts=row['attempts']+1)
            continue
        queue.update(row['id'],'running',summary='依据已核对，正在通过记忆审核中心处理。')
        try:
            if not active(store):
                queue.update(row['id'],'queued',summary='自主处理已暂停，保留当前进度。');break
            result=apply(proof['proposal'],proof['revision'],should_continue=lambda:active(store) and personal_scope.memory_admitted(candidate['memory_id'],candidate,load_registry()))
            result['verification_summary']='已重新读取审核结果，来源与记忆版本匹配。'
            result['changes']='确认继续有效' if proof['proposal']['action']=='keep_active' else ('保留原文并标为历史' if proof['proposal']['action']=='mark_legacy' else '新建正确记忆并关联旧记录')
            queue.update(row['id'],'verified',summary='已通过审核中心处理，并重新读取结果核对。',
                processed_revision=revision,result=result,attempts=row['attempts']+1)
            applied+=1
        except (RuntimeError,OSError,subprocess.SubprocessError) as exc:
            queue.update(row['id'],'verifying' if str(exc)=='operator_interrupted_receipt_requires_recheck' else 'waiting_evidence',summary='处理没有完成，系统保留原始记录并等待可核对的新结果。',
                processed_revision=revision,result={'gap':str(exc),'verification_summary':'尚未取得可核对的完成凭据。','next_trigger':'evidence_or_operator_recovery_changed'},attempts=row['attempts']+1)
    return {'enabled':True,'processed':processed,'applied':applied,'total':len(candidates)}


def tick(store, limit=5, *, ranking_client=None):
    """Keep the existing path verbatim until a matching qualified release is active."""
    import os, sys
    if not active(store) or os.environ.get('JEV_DISABLE')=='1':
        return _tick_original(store,limit)
    if ranking_client is None and ('unittest' in sys.modules or 'pytest' in sys.modules):
        return _tick_original(store,limit)
    try:
        from . import jev_ranking
        client=ranking_client or jev_ranking.client_module()
        release=client.scenario('agi','analysis_priority')
    except Exception:
        # Only the pre-dispatch capability probe can fall back. Once processing
        # begins, an error must not replay an already partially processed batch.
        return _tick_original(store,limit)
    if release.get('mode')=='assist' and release.get('adapter_version')==jev_ranking.ADAPTER_VERSION:
        return _tick_ranked(store,limit,client)
    return _tick_original(store,limit)
