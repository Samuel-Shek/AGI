"""Positive source admission before reading bodies, models, writes or delivery."""
from datetime import datetime, date
import json
from pathlib import Path
ROOT=Path.home()/"Library/Application Support/AGI/source-governance"
BRIDGE=Path.home()/"AI/LocalData/WORKPLACE-Memory-Bridge.nosync/config.json"
VERSION="personal-v1"

def policy():
    try: p=json.loads((ROOT/"policy.json").read_text())
    except FileNotFoundError:
        return {"version":VERSION,"owner_id":"local-owner","cutover_at":"1970-01-01T00:00:00+00:00","company_roots":[],"personal_roots":[],"personal_files":[],"personal_source_ids":[],"personal_session_ids":[]}
    if p.get("version")!=VERSION or p.get("owner_id")!="local-owner":raise ValueError("personal_source_policy_invalid")
    datetime.fromisoformat(p["cutover_at"])
    return p

def company_stopped():
    try:
        r=json.loads((ROOT/"retirements.json").read_text());b=json.loads(BRIDGE.read_text())
        if not isinstance(r,dict) or not isinstance(b,dict):return True
        entry=r.get('workplace')
        if not isinstance(entry,dict) or type(entry.get('active')) is not bool or type(b.get('retirement_active')) is not bool:return True
        return entry['active'] or b['retirement_active']
    except (OSError,ValueError,TypeError,KeyError,AttributeError):return True


def within(path,root):
    try:return Path(path).expanduser().resolve().is_relative_to(Path(root).expanduser().resolve())
    except (OSError,RuntimeError,ValueError,TypeError):return False

def classify(meta):
    """Only trusted registration/metadata is admitted; titles and body labels aren't authority."""
    p=policy();paths=[]
    for k in ("path","cwd","root","source_path","project_path"):
        v=meta.get(k)
        if v:paths.append(str(v))
    paths+=list(meta.get("registered_paths") or [])
    scope=str(meta.get("applies_to","")).lower()
    corporate=any(t in scope for t in ("workplace","examplecorp","示例公司","dingtalk","enterprise_email"))
    corporate=corporate or any(any(within(x,r) for r in p["company_roots"]) for x in paths)
    if corporate:return "company"
    if meta.get("mixed") in (True,1,"true","True"):return "mixed"
    if meta.get("session_id"):
        return "personal" if meta["session_id"] in p["personal_session_ids"] else "unknown"
    if meta.get("source_id") in p["personal_source_ids"] and not paths:return "personal"
    if meta.get("session_id") in p["personal_session_ids"]:return "personal"
    if paths and all(str(Path(x).expanduser().resolve()) in p.get("personal_files",[]) or any(within(x,r) for r in p["personal_roots"]) for x in paths):return "personal"
    return "unknown"

def admitted(meta):return classify(meta)=="personal"

def memory_classify(memory_id,meta=None,registry=None):
    registry=registry or {};entries=list(registry.get("bindings",{}).get(memory_id,[]))+list(registry.get("investigation_sources",{}).get(memory_id,[]))
    m=dict(meta or {});m.pop("source_id",None);m["registered_paths"]=[x.get("path") for x in entries if x.get("path")]
    return classify(m)

def cutover():return datetime.fromisoformat(policy()["cutover_at"])

def cutover_day():
    from zoneinfo import ZoneInfo
    return cutover().astimezone(ZoneInfo("Asia/Shanghai")).date()

def binding():return {"source_policy_version":VERSION,"source_cutover_at":policy()["cutover_at"]}

def clipped_start(start):return max(start,cutover_day())

def require_packet(packet):
    if any(packet.get(k)!=v for k,v in binding().items()):raise ValueError("personal_source_binding_mismatch")

def report_namespace(root):return Path(root)/"个人AI报告"


def memory_admitted(memory_id,meta=None,registry=None):
    return memory_classify(memory_id,meta,registry)=='personal'

def require_source(meta):
    if not admitted(meta):raise PermissionError('owner_retired_or_source_unknown')
    for key in ('path','source_path'):
        value=meta.get(key)
        if value:
            path=Path(value)
            if not path.is_absolute() or any(x.is_symlink() for x in (path,*path.parents)):raise PermissionError('source_alias_not_registered')
            import os,stat
            st=path.stat()
            if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.getuid():raise PermissionError('source_owner_or_type_invalid')

def memory_metadata_admitted(memory_id,meta,registry):
    owners=[str(meta.get(k) or '') for k in ('user_id','owner_id','memory_user_id')]
    if not any(owners) or any(x and x!='local-owner' for x in owners):return False
    if str(meta.get('schema_version'))=='3' and any(x!='local-owner' for x in owners):return False
    return memory_admitted(memory_id,meta,registry)
