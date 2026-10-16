#!/usr/bin/env python3
"""Prepare reviewable external integration files; NEVER modify live runtime.

Run with --stage <private staging directory>. Parent reviews and installs the
returned manifest, preserves existing runtime files, then runs shared tests.
"""
import argparse,hashlib,json,os,sys
from pathlib import Path
BASE=Path(__file__).resolve().parents[1]
HOME=Path.home()
CENTER=HOME/'.hermes/memory-review-center'
ENGINE=HOME/'.hermes/extensions/mem0'
SOURCE=BASE/'core/activity_mvp/feedback/memory_operator.py'

def replace_once(text,old,new):
    if new in text:return text
    if text.count(old)!=1:raise RuntimeError('unexpected_source_version: '+old[:70])
    return text.replace(old,new)

def stage(out):
    out.mkdir(parents=True,exist_ok=True,mode=0o700);os.chmod(out,0o700)
    manifest=[]
    def put(target,data):
        dest=out/str(target.relative_to(HOME))
        dest.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        dest.write_text(data);os.chmod(dest,0o600)
        manifest.append({'target':str(target),'staged':str(dest),'before_sha256':hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else None,'after_sha256':hashlib.sha256(data.encode()).hexdigest()})
    put(CENTER/'review_center/autonomy.py',SOURCE.read_text())
    put(ENGINE/'memory_autonomy_operator.py',SOURCE.read_text())
    p=CENTER/'review_center/adapters.py';s=p.read_text()
    s=replace_once(s,'    SAFE_PROVENANCE_KEYS = {','    SAFE_PROVENANCE_KEYS = {\n        "applies_to", "schema_version",')
    put(p,s)
    p=ENGINE/'mem0_service.py';s=p.read_text()
    s=replace_once(s,"        if not row or not row['confirmed_at'] or row['batch_status'] not in {'confirmed','applying','failed','applied'}:","        from memory_autonomy_operator import authorization_valid\n        if not row or not authorization_valid(self.config['review_ledger'], row['batch_id']) or row['batch_status'] not in {'confirmed','applying','failed','applied'}:")
    s=replace_once(s,"WHERE i.action='supersede' AND b.confirmed_at IS NOT NULL'''))","WHERE i.action='supersede' '''))\n            from memory_autonomy_operator import authorization_valid\n            rows = [r for r in rows if authorization_valid(self.config['review_ledger'], r['batch_id'])]")
    put(p,s)
    p=ENGINE/'mem0_engine.py';s=p.read_text()
    s=replace_once(s,'    gate["metadata"]["human_review_status"] = "approved"', '    from memory_autonomy_operator import automatic_actor\n    review_actor = automatic_actor(REVIEW_LEDGER_FILE, operation_key)\n    gate["metadata"]["human_review_status"] = "not_required" if review_actor else "approved"\n    if review_actor:\n        gate["metadata"]["review_authority"] = "automatic_policy"\n        gate["metadata"]["automatic_review_policy"] = "owner-approved-autonomy-20260921"\n        gate["metadata"]["review_actor"] = review_actor')
    # Fixed actor is derived from authenticated ledger authority, never from input.
    s=s.replace('agent_id="memory-review-center",','agent_id=review_actor or "memory-review-center",')
    put(p,s)
    p=ENGINE/'memory_write_policy.py';s=p.read_text()
    s=replace_once(s,'    "sensitivity", "status", "review_status", "human_review_status",','    "sensitivity", "status", "review_status", "human_review_status",\n    "review_authority", "automatic_review_policy", "review_actor",')
    put(p,s)
    p=ENGINE/'memory_review_policy.py';s=p.read_text()
    s=replace_once(s,'                       candidate_fingerprint, policy_version\n                FROM lifecycle_overrides','                       candidate_fingerprint, policy_version, batch_id\n                FROM lifecycle_overrides')
    s=replace_once(s,'        for memory_id, content_sha, lifecycle, decision, fingerprint, policy in rows:', '        from memory_autonomy_operator import automatic_batch, authorization_valid\n        for memory_id, content_sha, lifecycle, decision, fingerprint, policy, batch_id in rows:\n            if lifecycle == "active" and automatic_batch(self.ledger,batch_id) and not authorization_valid(self.ledger,batch_id):\n                lifecycle, decision = "review_required", "review_later"')
    put(p,s)
    p=CENTER/'review_center/retrieval_policy.py';s=p.read_text()
    s=replace_once(s,'                SELECT 1 FROM lifecycle_overrides','                SELECT batch_id FROM lifecycle_overrides')
    s=replace_once(s,'    return row is not None','    if row is None: return False\n    from .autonomy import automatic_batch, authorization_valid\n    return not automatic_batch(ledger_path,row[0]) or authorization_valid(ledger_path,row[0])')
    put(p,s)
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2));os.chmod(out/'manifest.json',0o600)
    return manifest


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--stage',type=Path,required=True);args=parser.parse_args()
    manifest=stage(args.stage)
    print(json.dumps({'staged_files':len(manifest),'manifest':str(args.stage/'manifest.json'),'live_modified':False}))
