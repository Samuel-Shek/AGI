"""Fixed local interface. Read actions neither migrate nor start model work."""
from __future__ import annotations
import argparse
import json
import sys
import sqlite3
import time
from pathlib import Path
from ..feedback.store import Store as FeedbackStore,default_root,packed
from ..feedback.control import gate
from .store import Store,record_feedback

def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['status','list','detail','configure','analyze','feedback'])
    parser.add_argument('--root',type=Path);parser.add_argument('--feedback-root',type=Path);parser.add_argument('--payload-stdin',action='store_true')
    args=parser.parse_args();feedback_root=args.feedback_root or default_root();root=args.root or feedback_root.parent/'reflection'
    raw=sys.stdin.buffer.read(8193) if args.payload_stdin else b'{}'
    if len(raw)>8192:raise ValueError('reflection_payload_limit')
    payload=json.loads(raw or b'{}')
    if not isinstance(payload,dict):raise ValueError('reflection_payload_invalid')
    read=args.action in {'status','list','detail'}
    if read and not (root/'reflection.sqlite').is_file():
        print(packed({'contract':'ReflectionSnapshotV1','status':'not_enabled','enabled':False,'insights':[]}));return
    with FeedbackStore(feedback_root,readonly=True) as feedback,Store(root,readonly=read) as store:
        active=feedback.active() and not feedback.get('autonomy',{}).get('paused',False)
        if read:
            result=store.response(active)
            if args.action=='detail':
                result['insights']=[r for r in result['insights'] if r['id']==payload.get('id')]
        elif args.action=='configure':
            if set(payload)!={'enabled'} or type(payload['enabled']) is not bool:raise ValueError('reflection_configuration_invalid')
            with gate(feedback.root):
                store.set('enabled',payload['enabled'])
                if not payload['enabled']:store.conn.execute('UPDATE notices SET delivered=2 WHERE delivered=0')
                store.set('owner_authorized_at',time.time())
                store.set('authorization_epoch',store.get('authorization_epoch',0)+1)
                store.set('authorization','2026-10-05 owner-approved metacognition plan: primary local user and assistant conversation context through existing Codex channel; credentials protected.')
            result={'status':'ok','enabled':payload['enabled']}
        elif args.action=='feedback':result=record_feedback(store,payload)
        else:
            if payload:raise ValueError('reflection_analysis_payload_invalid')
            if not active or not store.enabled():raise PermissionError('not_active')
            store.set('analysis_requested_at',time.time());result={'status':'queued','note':'现有后台会在下一可用分析时段处理，不新增并发模型调用。'}
        from ..presentation_scope import project_for_runtime
        print(packed(project_for_runtime(result, root.parent)))

if __name__=='__main__':
    try:main()
    except (OSError,ValueError,sqlite3.Error) as error:
        print(packed({'status':'unavailable','reason':str(error)[:100]}));raise SystemExit(1)
