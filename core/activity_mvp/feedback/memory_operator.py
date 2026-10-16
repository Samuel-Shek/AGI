"""Bounded policy operator, also installed as review_center/autonomy.py.

The registry is an operator-owned catalogue of independently checked mappings,
not model-produced evidence. Every use rereads the actual authority. No human
confirmation is fabricated; automatic authorization is a separate ledger table.
"""
from __future__ import annotations
import hashlib
import json
import os
import re
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
from datetime import datetime, timezone

from activity_mvp.source_governance import personal_sources as personal_scope

POLICY = 'owner-approved-autonomy-20260921'
ACTOR = 'agi-memory-autonomy'
ROOT = Path.home()/'.hermes/memory-review-center'
REGISTRY = ROOT/'state/autonomy-policy.json'
MAX_BYTES = 2*1024*1024


def sha(value):
    if not isinstance(value, bytes):
        value = (value if isinstance(value,str) else json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))).encode()
    return hashlib.sha256(value).hexdigest()


_READ_SCRIPT = r"""
import os,sys,stat
from pathlib import Path
try:
 p=Path(sys.argv[1]);limit=int(sys.argv[3])
 if not p.is_absolute() or any(x.is_symlink() for x in (p,*p.parents)):raise ValueError('unsafe_source_path')
 fd=os.open(p,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
 try:
  st=os.fstat(fd)
  if not stat.S_ISREG(st.st_mode) or st.st_size>limit:raise ValueError('unbounded_source')
  if sys.argv[2]=='1' and (st.st_uid!=os.getuid() or st.st_mode&0o077):raise ValueError('private_operator_registry_required')
  data=os.read(fd,limit+1)
  if len(data)>limit:raise ValueError('unbounded_source')
  sys.stdout.buffer.write(data)
 finally:os.close(fd)
except Exception as exc:
 print(str(exc) if isinstance(exc,ValueError) else type(exc).__name__,file=sys.stderr);sys.exit(1)
"""


def bounded(path, private=False):
    # File-open permission/cloud stalls must not occupy the shared memory owner.
    # The child executes only fixed stdlib file-read code under isolated Python.
    process=subprocess.Popen([sys.executable,'-I','-S','-c',_READ_SCRIPT,str(path),'1' if private else '0',str(MAX_BYTES)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:output,error=process.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        try:process.communicate(timeout=.2)
        except subprocess.TimeoutExpired:
            process.stdout.close();process.stderr.close()
        raise ValueError('source_read_timeout')
    if process.returncode:raise ValueError(error.decode('utf-8',errors='replace').strip()[:100] or 'source_read_failed')
    return output


def authority_bytes(binding):
    personal_scope.require_source(binding)
    data=bounded(binding['path'])
    lineage=binding.get('governed_source')
    if lineage:
        receipt=json.loads(bounded(lineage['receipt_path']))
        source=receipt.get('source',{})
        version=re.search(r'^core_version:\s*(.+)$',data.decode('utf-8'),re.M)
        if (receipt.get('ok') is not True or source.get('user_path')!=lineage['canonical_path'] or
            source.get('real_source')!=binding['path'] or source.get('raw_sha256')!=sha(data) or
            source.get('content_sha256')!=sha(data.decode('utf-8').strip()) or
            not version or source.get('core_version')!=version.group(1).strip()):
            raise ValueError('governed_source_receipt_mismatch')
    return data


def authority_identity(binding):
    # Access through the governed real-source path must not create a new memory
    # operation. The transport alias is checked independently on every read.
    identity=dict(binding)
    lineage=identity.pop('governed_source',None)
    if lineage:identity['path']=lineage['canonical_path']
    return identity


def load_registry(path=REGISTRY):
    registry = json.loads(bounded(path,private=True))
    if registry.get('policy')!=POLICY or registry.get('owner_id')!='local-owner' or registry.get('enabled') is not True:
        raise ValueError('autonomy_policy_not_enabled')
    if registry.get('actor')!=ACTOR: raise ValueError('automatic_actor_mismatch')
    if not isinstance(registry.get('bindings'),dict): raise ValueError('bindings_required')
    return registry


def scopes(value):
    if isinstance(value,str): value=json.loads(value)
    if not isinstance(value,list) or not value or any(not isinstance(v,str) for v in value):
        raise ValueError('scope_unavailable')
    return sorted(set(value))


def normalized_claim(text):
    text=re.sub(r'^\s*(?:[-*]|\d+[.)、])\s+', '',text.strip())
    return re.sub(r'\s+',' ',text.replace('**','').replace('`','')).strip()


def authority_claims(text):
    # Exact prose matching is not a Markdown renderer. Conservatively exclude
    # examples and quoted material so they cannot release a memory on recall.
    claims = []
    fence = None
    in_comment = False
    quoted = False
    for line in text.splitlines():
        stripped = line.lstrip()
        if fence:
            if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(fence[1]) + r",}\s*", line):
                fence = None
            continue
        if not in_comment:
            # Unprefixed paragraph continuations can still belong to a quote.
            # Use a blank line as the conservative recovery boundary.
            if stripped.startswith('>'):
                quoted = True
                continue
            if quoted:
                if not stripped:
                    quoted = False
                continue
            if re.match(r'^(?: {4}| {0,3}\t)', line):
                continue
            opening = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
            if opening:
                marker, info = opening.groups()
                if marker[0] != '`' or '`' not in info:
                    fence = (marker[0], len(marker))
                    continue
        # Comments may span lines or share a line with prose. Exclude the whole
        # line; malformed/unclosed comments remain unavailable as evidence.
        if in_comment or '<!--' in line:
            remainder = line
            while remainder:
                if in_comment:
                    close = remainder.find('-->')
                    if close < 0:
                        break
                    in_comment = False
                    remainder = remainder[close + 3:]
                else:
                    opening_comment = remainder.find('<!--')
                    if opening_comment < 0:
                        break
                    in_comment = True
                    remainder = remainder[opening_comment + 4:]
            continue
        if stripped.startswith(('#', '|', '---')) or not stripped:
            continue
        claim = normalized_claim(line)
        if 8 <= len(claim) <= 10000:
            claims.append(claim)
    return claims


def replacement_evidence(dependency, *, ledger_path=None, registry_path=REGISTRY, visited=frozenset()):
    """Follow only an explicit, registered replacement relation, never similarity."""
    ledger_path=Path(ledger_path or ROOT/'state/review-ledger.sqlite3')
    with sqlite3.connect(ledger_path.as_uri()+'?mode=ro',uri=True,timeout=1) as db:
        db.row_factory=sqlite3.Row
        row=db.execute("SELECT d.superseding_memory_id,d.batch_id,i.supersede_draft_json FROM review_decisions d JOIN batch_items i ON i.batch_id=d.batch_id AND i.memory_id=d.memory_id WHERE d.memory_id=? AND d.content_sha256=? AND d.action='supersede' AND i.status='applied'",
           (dependency['source_memory_id'],dependency['source_content_sha256'])).fetchone()
    if not row:raise ValueError('replacement_not_yet_verified')
    if not authorization_valid(ledger_path,row['batch_id'],registry_path=registry_path,visited=visited):raise ValueError('replacement_authority_invalid')
    # Reuse the deployed owner-checked read adapter without importing its UI.
    import importlib.util
    name='_agi_owner_scoped_memory_reader'
    if name not in sys.modules:
        spec=importlib.util.spec_from_file_location(name,ROOT/'review_center/adapters.py')
        module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module)
    module=sys.modules[name]
    reader=module.ChromaSqliteReadAdapter(Path('/Volumes/mem0/chroma/chroma.sqlite3'),collection_name='mem0',owner_id='local-owner',admission_registry=load_registry)
    record=reader.get(row['superseding_memory_id'])
    if record.owner_id!='local-owner' or sha(record.content)!=dependency['replacement_sha256']:raise ValueError('replacement_content_or_owner_changed')
    if scopes(record.provenance.get('applies_to'))!=scopes(dependency['applies_to']):raise ValueError('replacement_scope_changed')
    if record.provenance.get('status')!='active' or record.provenance.get('review_status')!='approved':raise ValueError('replacement_not_active')
    draft=json.loads(row['supersede_draft_json'])
    if sha(draft['content'])!=dependency['replacement_sha256']:raise ValueError('replacement_receipt_content_changed')
    return {'memory_id':record.memory_id,'content_sha256':sha(record.content),'batch_id':row['batch_id'],'owner_id':'local-owner','applies_to':scopes(record.provenance['applies_to'])}


def inspect_candidate(candidate, memory, registry):
    """Returns only hashes and public evidence; never persists memory/source bodies."""
    if memory.get('memory_id')!=candidate['memory_id'] or sha(memory['text'])!=candidate['content_sha256']:
        raise ValueError('memory_source_changed')
    scope=scopes(memory.get('applies_to'))
    bindings=registry['bindings'].get(candidate['memory_id'],[])
    evidence=[]; matched=[]; gaps=[]
    for binding in bindings:
        identity=authority_identity(binding)
        proof={'source_id':binding['source_id'],'path':identity['path'],'kind':'authoritative_file','binding_sha256':sha(identity)}
        try:
            data=authority_bytes(binding); proof['sha256']=sha(data)
            # All conditions were independently checked when this narrow binding
            # was registered. Matching snippets alone never authorizes a new claim.
            text=data.decode('utf-8')
            lines=[line.strip() for line in text.splitlines() if binding['selector'] in line]
            if len(lines)!=1: raise ValueError('authority_selector_changed')
            proof['excerpt_sha256']=sha(lines[0])
            if proof['excerpt_sha256']!=binding['excerpt_sha256']: raise ValueError('authority_content_changed')
            if binding['memory_sha256']!=candidate['content_sha256']: raise ValueError('binding_memory_changed')
            if scopes(binding['applies_to'])!=scope: raise ValueError('binding_scope_changed')
            if binding.get('owner_id')!='local-owner': raise ValueError('binding_owner_mismatch')
            expires=datetime.fromisoformat(binding['valid_until'].replace('Z','+00:00'))
            if expires.tzinfo is None or expires<=datetime.now(timezone.utc): raise ValueError('binding_expired')
            if binding.get('action') not in {'keep_active','mark_legacy','supersede'}: raise ValueError('action_not_allowed')
            if binding['action']=='keep_active' and (memory.get('status')!='active' or memory.get('review_status')!='approved'):
                raise ValueError('restricted_memory_cannot_be_promoted')
            if binding.get('replacement_dependency'):
                proof['replacement']=replacement_evidence(binding['replacement_dependency'])
            proof.update({'valid':True,'valid_until':binding['valid_until']})
            try:
                from .jev_observer import observe_batch
                observe_batch('agi', [
                    ('claim_support', {'claim': memory['text'], 'evidence': lines[0]}),
                    ('ownership', {'claim': memory['text'], 'evidence': lines[0], 'subject': 'Local Owner / Alex'}),
                    ('relevance', {'query': memory['text'], 'evidence': lines[0]}),
                ], source_version=sha([sha(data), binding]),
                    event_id=sha([candidate['memory_id'], binding]), data_class='memory_text')
            except Exception:
                pass
            matched.append(binding)
        except (OSError,ValueError,KeyError,sqlite3.Error) as exc:
            proof.update({'valid':False,'gap':str(exc) if isinstance(exc,ValueError) else type(exc).__name__})
            gaps.append(proof['gap'])
        evidence.append(proof)
    # Read only directly registered source pointers for unresolved candidates.
    for source in registry.get('investigation_sources',{}).get(candidate['memory_id'],[]):
        aliases={b['path']:authority_identity(b)['path'] for b in [*bindings,*registry.get('authority_sources',[])]}
        proof={'source_id':source['source_id'],'path':aliases.get(source['path'],source['path']),'kind':'source_lookup'}
        try:
            personal_scope.require_source(source)
            proof['sha256']=sha(bounded(source['path']));proof['status']='source_located_requires_claim_verification'
        except (OSError,ValueError): proof['status']='source_unavailable'
        evidence.append(proof)
    if not bindings:
        for authority in registry.get('authority_sources',[]):
            identity=authority_identity(authority)
            proof={'source_id':authority['source_id'],'path':identity['path'],'kind':'authoritative_file',
                   'binding_sha256':sha(identity),'match_kind':'exact_whole_claim'}
            try:
                data=authority_bytes(authority);proof['sha256']=sha(data)
                claim=normalized_claim(memory['text'])
                if authority.get('owner_id')!='local-owner' or scopes(authority.get('applies_to'))!=scope:
                    raise ValueError('authority_owner_or_scope_mismatch')
                expiry=datetime.fromisoformat(authority['valid_until'].replace('Z','+00:00'))
                if expiry.tzinfo is None or expiry<=datetime.now(timezone.utc):raise ValueError('authority_expired')
                if memory.get('status')!='active' or memory.get('review_status')!='approved':raise ValueError('restricted_memory_cannot_be_promoted')
                if claim not in authority_claims(data.decode('utf-8')):raise ValueError('whole_claim_not_present')
                proof.update({'valid':True,'valid_until':authority['valid_until'],'excerpt_sha256':sha(claim)})
                matched.append(dict(authority,action='keep_active'))
            except (OSError,ValueError,KeyError) as exc:
                proof.update({'valid':False,'gap':str(exc) if isinstance(exc,ValueError) else type(exc).__name__})
            evidence.append(proof)
        # A successful exact match qualifies; unrelated authorized documents that
        # do not contain it are lookup gaps, not contradicting facts.
    actions={b['action'] for b in matched}
    proposal=None
    if matched and not gaps and len(actions)==1:
        chosen=matched[0]
        proposal={k:candidate[k] for k in ('memory_id','content_sha256','candidate_fingerprint','policy_version')}
        proposal.update({'action':chosen['action'],'applies_to':scope})
        if chosen['action']=='supersede':
            drafts=[b.get('supersede_draft') for b in matched]
            if not drafts[0] or any(d!=drafts[0] for d in drafts): gaps.append('replacement_conflict')
            else: proposal['supersede_draft']=drafts[0]
    elif len(actions)>1: gaps.append('evidence_conflict')
    if gaps: proposal=None
    revision=sha({'candidate':{k:candidate.get(k) for k in ('memory_id','content_sha256','candidate_fingerprint','policy_version')},
                  'scope':scope,'memory_state':[memory.get('status'),memory.get('review_status')],
                  'policy':POLICY,'bindings':[authority_identity(b) for b in bindings],'authorities':[authority_identity(b) for b in registry.get('authority_sources',[])],'evidence':evidence})
    return {'revision':revision,'proposal':proposal,'evidence':evidence,
            'gap':','.join(sorted(set(gaps))) or ('' if proposal else 'no_independently_verified_current_claim')}


def create_center():
    from .adapters import ChromaSqliteReadAdapter,Mem0SupersedeAdapter,DisabledPublishAdapter
    from .config import ReviewCenterConfig
    from .ledger import Ledger
    from .service import ReviewCenter
    config=ReviewCenterConfig(queue_v2_path=Path.home()/'.hermes/obsidian-command-rules/analysis/memory-quality/review-queue-v3.json',
       candidate_dir=Path.home()/'.hermes/ai-memory-candidates',ledger_path=ROOT/'state/review-ledger.sqlite3',capability_token='local-policy-operator-no-http-session')
    return ReviewCenter(config=config,ledger=Ledger(config.ledger_path),
       memory_reader=ChromaSqliteReadAdapter(Path('/Volumes/mem0/chroma/chroma.sqlite3'),collection_name='mem0',owner_id='local-owner',admission_registry=load_registry),
       supersede_adapter=Mem0SupersedeAdapter(Path.home()/'.hermes/venv-mem0/bin/python',Path.home()/'.hermes/extensions/mem0/mem0_bridge.py'),
       publish_adapter=DisabledPublishAdapter())


def _inspect_center(center, memory_id, registry):
    queue,candidate=center._queue_item(memory_id)
    center.memory_reader.admission_registry=lambda:load_registry()
    record,_=center._read_verified(memory_id,candidate['content_sha256'])
    if record.owner_id!='local-owner': raise ValueError('owner_mismatch')
    memory=dict(record.provenance,memory_id=memory_id,text=record.content)
    candidate=dict(candidate,policy_version=queue['policy_version'])
    return queue,candidate,inspect_candidate(candidate,memory,registry)


AUTO_SCHEMA='''CREATE TABLE IF NOT EXISTS automatic_authorizations(
 operation_id TEXT PRIMARY KEY,batch_id TEXT UNIQUE NOT NULL,policy TEXT NOT NULL,actor TEXT NOT NULL,
 evidence_revision TEXT NOT NULL,evidence_json TEXT NOT NULL,memory_id TEXT NOT NULL,authorized_at TEXT NOT NULL);'''


def reject_current_human_decision(center,candidate):
    with center.ledger.connect() as db:
        current=db.execute('SELECT l.batch_id,l.candidate_fingerprint,l.policy_version,b.confirmed_at FROM lifecycle_overrides l LEFT JOIN batches b ON b.batch_id=l.batch_id WHERE l.memory_id=? AND l.content_sha256=?',
            (candidate['memory_id'],candidate['content_sha256'])).fetchone()
    if current and current['confirmed_at'] and current['candidate_fingerprint']==candidate['candidate_fingerprint'] and current['policy_version']==candidate['policy_version']:
        raise ValueError('current_human_decision_takes_precedence')


def apply_automatic(center, memory_id, expected_revision, *, registry_path=REGISTRY):
    registry=load_registry(registry_path)
    queue,candidate,proof=_inspect_center(center,memory_id,registry)
    if proof['revision']!=expected_revision: raise ValueError('evidence_changed')
    if not proof['proposal']: raise ValueError('insufficient_evidence')
    reject_current_human_decision(center,candidate)
    proposal=proof['proposal'];operation_id=sha([POLICY,memory_id,expected_revision,proposal])
    with center.ledger.connect() as db:
        db.executescript(AUTO_SCHEMA)
        prior=db.execute('SELECT batch_id FROM automatic_authorizations WHERE operation_id=?',(operation_id,)).fetchone()
    if prior:
        batch=center.ledger.get_batch(prior['batch_id'])
        if batch['status']=='applied': return receipt(center,batch,proof,operation_id)
        # A crash while applying can be resumed with the original operation key.
        # A known failure stays failed; evidence changes are the only new attempt.
        if batch['status']=='failed': raise ValueError('previous_attempt_failed')
    else:
        action=dict(proposal,reviewer=ACTOR+':'+expected_revision,decided_at=datetime.now(timezone.utc).isoformat(),
                    note='Automatic policy adjudication; evidence '+expected_revision,
                    queue_sha256=queue['source_sha256'],source_snapshot_sha256=queue['source_snapshot_sha256'])
        # Recover a crash between batch creation and recording authority. The
        # evidence-qualified actor key separates reconfirmation after updates.
        with center.ledger.connect() as db:
            orphan=db.execute('SELECT batch_id FROM batch_items WHERE reviewer=? AND memory_id=? AND content_sha256=? AND queue_sha256=? AND source_snapshot_sha256=?',
                (action['reviewer'],memory_id,candidate['content_sha256'],queue['source_sha256'],queue['source_snapshot_sha256'])).fetchone()
        batch=center.ledger.get_batch(orphan['batch_id']) if orphan else center.ledger.create_batch([action])
        if orphan and batch['status']!='previewed': raise ValueError('orphan_batch_state_conflict')
        with center.ledger.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT INTO automatic_authorizations VALUES(?,?,?,?,?,?,?,?)',
                (operation_id,batch['batch_id'],POLICY,ACTOR,expected_revision,json.dumps(proof['evidence'],sort_keys=True),memory_id,action['decided_at']))
            # Separate authorization: do not set confirmed_at or call confirm_batch.
            db.execute("UPDATE batches SET status='applying' WHERE batch_id=? AND status='previewed'",(batch['batch_id'],))
            db.execute('COMMIT')
    # Bind again immediately before the existing writer. Receipt validation below
    # also detects source mutation during the write.
    fresh_queue,_,fresh=_inspect_center(center,memory_id,load_registry(registry_path))
    if fresh['revision']!=expected_revision or fresh_queue['source_sha256']!=queue['source_sha256']:
        raise ValueError('source_changed_before_apply')
    reject_current_human_decision(center,candidate)
    batch=center.apply_batch(batch['batch_id'])
    if batch['status']!='applied': raise ValueError('automatic_apply_failed')
    _,_,fresh=_inspect_center(center,memory_id,load_registry(registry_path))
    if fresh['revision']!=expected_revision: raise ValueError('source_changed_during_apply')
    return receipt(center,batch,proof,operation_id)


def receipt(center,batch,proof,operation_id):
    p=proof['proposal']
    with center.ledger.connect() as db:
        row=db.execute('SELECT * FROM lifecycle_overrides WHERE memory_id=? AND content_sha256=?',(p['memory_id'],p['content_sha256'])).fetchone()
    if not row or row['decision']!=p['action'] or row['candidate_fingerprint']!=p['candidate_fingerprint'] or row['batch_id']!=batch['batch_id']:
        raise ValueError('automatic_readback_mismatch')
    return {'verified':True,'action':p['action'],'batch_id':batch['batch_id'],'operation_id':operation_id,
            'actor':ACTOR,'policy':POLICY,'human_confirmed':False,'evidence_revision':proof['revision']}


def authorization_valid(ledger_path, batch_id, *, registry_path=REGISTRY, visited=frozenset()):
    """Read-side fence: human batches pass; automatic batches recheck sources.

    No source text is returned. Missing/corrupt automatic evidence fails closed.
    """
    if batch_id in visited or len(visited)>=8:return False
    visited=visited|{batch_id}
    try:
        db=sqlite3.connect(Path(ledger_path).as_uri()+'?mode=ro',uri=True)
        db.row_factory=sqlite3.Row
        try:
            row=db.execute('SELECT confirmed_at FROM batches WHERE batch_id=?',(batch_id,)).fetchone()
            if not row: return False
            if row['confirmed_at']: return True
            auth=db.execute('SELECT * FROM automatic_authorizations WHERE batch_id=?',(batch_id,)).fetchone()
        finally: db.close()
        if not auth or auth['policy']!=POLICY or auth['actor']!=ACTOR: return False
        registry=load_registry(registry_path)
        evidence=json.loads(auth['evidence_json'])
        bindings=registry['bindings'].get(auth['memory_id'],[])
        authorities=registry.get('authority_sources',[])
        valid=[e for e in evidence if e.get('kind')=='authoritative_file' and e.get('valid')]
        if not valid:return False
        if bindings:
            registered={(b['source_id'],authority_identity(b)['path'],sha(authority_identity(b))) for b in bindings}
            proven={(e.get('source_id'),e.get('path'),e.get('binding_sha256')) for e in valid}
            if proven!=registered:return False
        for proof in valid:
            pool=authorities if proof.get('match_kind')=='exact_whole_claim' else bindings
            b=next((b for b in pool if b['source_id']==proof['source_id'] and authority_identity(b)['path']==proof['path']),None)
            if not b or proof.get('binding_sha256')!=sha(authority_identity(b)):return False
            data=authority_bytes(b)
            if sha(data)!=proof['sha256']:return False
            if b.get('replacement_dependency') and replacement_evidence(b['replacement_dependency'],ledger_path=ledger_path,registry_path=registry_path,visited=visited)!=proof.get('replacement'):return False
            if datetime.fromisoformat(b['valid_until'].replace('Z','+00:00'))<=datetime.now(timezone.utc):return False
            if proof.get('match_kind')=='exact_whole_claim' and proof.get('excerpt_sha256') not in {sha(c) for c in authority_claims(data.decode('utf-8'))}:return False
        return True
    except (OSError,ValueError,KeyError,sqlite3.Error): return False


def automatic_batch(ledger_path, batch_id):
    try:
        with sqlite3.connect(Path(ledger_path).as_uri()+'?mode=ro',uri=True) as db:
            return db.execute('SELECT 1 FROM automatic_authorizations WHERE batch_id=?',(batch_id,)).fetchone() is not None
    except sqlite3.OperationalError as exc:
        if 'no such table' in str(exc): return False
        raise


def automatic_actor(ledger_path, operation_key):
    with sqlite3.connect(Path(ledger_path).as_uri()+'?mode=ro',uri=True) as db:
        row=db.execute('SELECT batch_id FROM batch_items WHERE idempotency_key=?',(operation_key,)).fetchone()
    if row and automatic_batch(ledger_path,row[0]):
        if not authorization_valid(ledger_path,row[0]): raise ValueError('automatic_authorization_invalid')
        return ACTOR
    return ''


def main():
    try:
        request=json.loads(sys.stdin.read(MAX_BYTES))
        if set(request)!={'memory_id','expected_revision','policy'} or request['policy']!=POLICY:
            raise ValueError('invalid_operator_request')
        result=apply_automatic(create_center(),request['memory_id'],request['expected_revision'])
        print(json.dumps(result,ensure_ascii=False));return 0
    except Exception as exc:
        print(json.dumps({'error':str(exc) if isinstance(exc,ValueError) else type(exc).__name__}));return 1

if __name__=='__main__':raise SystemExit(main())
