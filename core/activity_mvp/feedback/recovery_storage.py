"""Owner-installed external recovery storage; no local payload staging."""
from __future__ import annotations
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
import stat

PROVENANCE = 'com.apple.provenance'
# An operator must independently qualify and bind a local recovery policy.
PROVENANCE_DECISION_SHA256 = None

def provenance_approval(b):
    """Read the existing owner decision for minimal recoveries, not cold backups.

    No caller-supplied approval and no fallback to the narrower archive policy.
    Receipts retain only the location and digest of this private decision.
    """
    if PROVENANCE_DECISION_SHA256 is None:
        return None
    path = Path.home() / 'Library/Application Support/PSI/recovery/provenance-approval.json'
    if not os.path.lexists(path):return None
    b.no_alias(path)
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    try:
        before=os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink!=1
            or before.st_uid!=os.getuid() or stat.S_IMODE(before.st_mode)!=0o600):
            raise ValueError('recovery_approval_not_private')
        raw=os.read(fd,32769);after=os.fstat(fd)
        token=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_ctime_ns,s.st_mtime_ns)
        if len(raw)>32768 or token(before)!=token(after) or token(after)!=token(path.lstat()):
            raise ValueError('recovery_approval_changed')
    finally:os.close(fd)
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ValueError('recovery_approval_duplicate_key')
            result[key]=value
        return result
    value=json.loads(raw,object_pairs_hook=pairs)
    if (not isinstance(value,dict) or value.get('schema')!='file-organization-provenance-approval-v1'
        or value.get('status')!='approved'
        or value.get('scope')!='already_authorized_file_organization_archive_and_minimal_recovery'
        or value.get('allowed_attributes')!=[PROVENANCE]
        or any(value.get(k) is not True for k in ('preserve_original_manifest','read_back_actual_target_attributes',
            'all_other_fields_strict','does_not_expand_operation_authority','future_restore_may_not_write_original_attribute'))
        or not isinstance(value.get('approval'),dict)
        or value['approval'].get('kind')!='explicit_human_text'
        or any(not isinstance(value['approval'].get(k),str) or not value['approval'][k]
               for k in ('question_item','answer','session','recorded_at'))):
        raise ValueError('recovery_approval_invalid')
    sha=hashlib.sha256(raw).hexdigest()
    if sha!=PROVENANCE_DECISION_SHA256:raise ValueError('recovery_approval_unrecognized')
    return {'path':str(path),'sha256':sha}

def bound_approval(b,proof):
    policy=proof.get('provenance_approval')
    if policy and provenance_approval(b)!=policy:raise ValueError('recovery_approval_changed')
    return policy

def source_manifest(b,proof,bound):
    relative=bound['payload_relative']
    if (relative in ('','.','..') or Path(relative).is_absolute() or Path(relative).as_posix()!=relative
        or any(p in ('.','..') for p in Path(relative).parts)):
        raise ValueError('recovery_payload_mapping_invalid')
    target={('.' if k==relative else k[len(relative)+1:]):v for k,v in proof['manifest'].items()
            if k==relative or k.startswith(relative+'/')}
    original=bound.get('original_manifest')
    if original is None:
        if proof.get('provenance_approval'):raise ValueError('recovery_original_manifest_missing')
        if b.fingerprint(target)!=bound['manifest_sha256']:raise ValueError('recovery_original_manifest_changed')
        return target  # Existing exact-copy receipts remain supported.
    if (b.fingerprint(original)!=bound['manifest_sha256'] or bound.get('source_observed_manifest')!=original
        or not b.manifests_equivalent(target,original,proof.get('provenance_approval'))):
        raise ValueError('recovery_original_manifest_changed')
    values={key:{'present':PROVENANCE in row['xattrs'],'value_base64':row['xattrs'].get(PROVENANCE)}
            for key,row in original.items()}
    if bound.get('original_provenance')!=values:raise ValueError('recovery_original_attributes_changed')
    return original

class RecoveryDeferred(ValueError):
    """Preparation did not authorize a change to the installed pair."""

def backend():
    path = Path.home() / 'Library/Application Support/PSI/adapters/backup_manager.py'
    if not path.is_file() or path.is_symlink():raise RecoveryDeferred('recovery_tool_unavailable')
    spec=importlib.util.spec_from_file_location('agi_owner_backup_storage',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

def _name(job):
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,96}',job):raise PermissionError('recovery_job_scope')
    return job

def check_external(recovery,local_receipt):
    """Recheck both durable receipts and the complete immutable payload."""
    b=backend();bound=b.volume();recovery=Path(recovery);local_receipt=Path(local_receipt)
    b.no_alias(recovery);b.no_alias(local_receipt)
    if recovery.parent!=b.MOUNT/b.ARCHIVE/'agi':raise PermissionError('external_recovery_scope')
    external=recovery/'prepared.json'
    if external.read_bytes()!=local_receipt.read_bytes():raise ValueError('recovery_receipt_mismatch')
    proof=b.load(external)
    if (proof.get('status')!='direct_recovery_prepared' or proof.get('target_volume_uuid')!=bound['uuid']
        or proof.get('recovery')!=str(recovery) or proof.get('database_restore_allowed') is not False):
        raise ValueError('recovery_proof_invalid')
    bound_approval(b,proof)
    if proof.get('provenance_approval') and proof.get('local_receipt')!=str(local_receipt):
        raise ValueError('recovery_local_receipt_changed')
    if b.fingerprint(proof['manifest'])!=proof.get('manifest_sha256'):raise ValueError('recovery_manifest_changed')
    mapped=set()
    for source in proof['source_bindings'].values():
        source_manifest(b,proof,source)
        relative=source['payload_relative']
        if any(relative==p or relative.startswith(p+'/') or p.startswith(relative+'/') for p in mapped):
            raise ValueError('recovery_payload_mapping_overlap')
        mapped.add(relative)
    def guard():
        if not os.path.ismount(b.MOUNT) or os.stat(b.MOUNT).st_dev!=bound['device']:raise ValueError('recovery_volume_changed')
    if b.manifest(recovery/'payload',guard)!=proof['manifest']:raise ValueError('recovery_readback_changed')
    guard()
    if b.volume()!=bound:raise ValueError('recovery_volume_changed')
    return proof

def original_checker(proof):
    b=backend();bindings=proof['source_bindings']
    def check(path):
        bound_approval(b,proof)
        path=Path(path);bound=bindings.get(str(path))
        if not bound:
            if path.exists() or path.is_symlink():raise ValueError('unbound_original_appeared')
            return None
        b.no_alias(path)
        if b.tree_identity(path)!=bound['identity'] or b.fingerprint(b.manifest(path))!=bound['manifest_sha256']:
            raise ValueError('prepared_original_changed')
        return bound['identity'][str(path)][:2]
    def cleanup(entry,guard):
        source=Path(entry['target']);temp=Path(entry['stage']);bound=bindings[str(source)]
        attestations={}
        policy=bound_approval(b,proof)
        expected=source_manifest(b,proof,bound)
        objects={('.' if Path(k)==source else Path(k).relative_to(source).as_posix()):v[:2] for k,v in bound['identity'].items()}
        b.no_alias(temp);b.verify_remainder(temp,expected,objects,policy)
        if policy:
            # A rename can itself change provenance. Preserve that exact new
            # observation on both volumes before deleting any original member.
            if guard:guard()
            identity=b.tree_identity(temp);observed=b.manifest(temp)
            for key,row in observed.items():
                wanted=expected.get(key)
                normalized={**row,'mtime_ns':wanted['mtime_ns']} if row['type']=='dir' and wanted else row
                if not wanted or not b.manifests_equivalent({key:normalized},{key:wanted},policy):
                    raise ValueError('cleanup_original_content_changed')
            recovery=Path(proof['recovery']);local=Path(proof['local_receipt'])
            check_external(recovery,local)
            attestation={'schema':1,'status':'original_cleanup_observed','source':str(source),'stage':str(temp),
                'prepared_sha256':b.digest(recovery/'prepared.json'),'approval':policy,
                'original_manifest_sha256':bound['manifest_sha256'],'observed_manifest':observed,'identity':identity}
            name=b.fingerprint(attestation)+'.json'
            external=recovery/'cleanup-attestations'/name
            internal=local.parent/(local.stem+'.cleanup-attestations')/name
            b.publish_attestation(external,attestation);b.publish_attestation(internal,attestation)
            raw=external.read_bytes()
            if raw!=internal.read_bytes() or json.loads(raw)!=attestation:raise ValueError('cleanup_attestation_mismatch')
            attestations={path:(b.tree_identity(path),hashlib.sha256(raw).hexdigest()) for path in (external,internal)}
            check_external(recovery,local)
            if b.tree_identity(temp)!=identity or b.manifest(temp)!=observed:raise ValueError('cleanup_original_changed')
            expected=observed
        def member_guard(rel):
            if guard:guard()
            bound_approval(b,proof)
            if any(b.tree_identity(path)!=identity or b.digest(path)!=sha for path,(identity,sha) in attestations.items()):
                raise ValueError('cleanup_attestation_changed')
            current=temp if rel=='.' else temp/rel
            if [current.lstat().st_dev,current.lstat().st_ino]!=objects.get(rel):raise ValueError('cleanup_original_member_changed')
            row=single_manifest(b,current);wanted=expected.get(rel)
            if row['type']=='dir' and wanted:row={**row,'mtime_ns':wanted['mtime_ns']}
            if row!=wanted:raise ValueError('cleanup_original_content_changed')
        b.remove_verified(temp,member_guard,objects)
    check.cleanup=cleanup
    return check

def single_manifest(b,path):
    s=path.lstat();row={'mode':stat.S_IMODE(s.st_mode),'uid':s.st_uid,'gid':s.st_gid,'mtime_ns':s.st_mtime_ns,
                       'flags':getattr(s,'st_flags',0),'xattrs':b.attrs(path),'acl':b.acl(path)}
    if stat.S_ISLNK(s.st_mode):row.update(type='link',target=os.readlink(path))
    elif stat.S_ISDIR(s.st_mode):row.update(type='dir')
    elif stat.S_ISREG(s.st_mode):
        if s.st_nlink!=1:raise ValueError('cleanup_member_hardlink')
        row.update(type='file',size=s.st_size,sha256=b.digest(path))
    else:raise ValueError('cleanup_member_type')
    return row

def retained_guard(recovery,local_receipt):
    """Full readback once, then identity checks while removing original objects."""
    b=backend();proof=check_external(recovery,local_receipt);bound=b.volume();payload=Path(recovery)/'payload'
    tree=b.tree_identity(payload);last=[time.monotonic()]
    receipts={p:[p.lstat().st_dev,p.lstat().st_ino,p.lstat().st_mtime_ns,p.lstat().st_ctime_ns,p.lstat().st_size]
              for p in [Path(recovery)/'prepared.json',Path(local_receipt)]}
    def guard():
        bound_approval(b,proof)
        if not os.path.ismount(b.MOUNT) or os.stat(b.MOUNT).st_dev!=bound['device']:raise ValueError('recovery_volume_changed')
        for p,token in receipts.items():
            s=p.lstat()
            if [s.st_dev,s.st_ino,s.st_mtime_ns,s.st_ctime_ns,s.st_size]!=token:raise ValueError('recovery_receipt_changed')
        if b.tree_identity(payload)!=tree:raise ValueError('recovery_target_identity_changed')
        if time.monotonic()-last[0]>=1:
            if b.volume()!=bound:raise ValueError('recovery_volume_changed')
            last[0]=time.monotonic()
    return guard

def prepare_external(root,job,app,before,writer):
    """Writer receives only an external payload directory and verified copier.

    Interrupted bytes stay on T9. The local pending record contains paths and
    fingerprints only. A retry never replaces a conflicting package.
    """
    b=backend();job=_name(job);root=Path(root);app=Path(app)
    pending=root/'publication-pending'/(job+'.json')
    local=root/'publication-receipts'/(job+'.json')
    intent={'job':job,'source_core_hash':before['core_hash'],'revision':before['revision'],
            'application':str(app),'status':'deferred_t9','at':time.time()}
    try:
        bound=b.volume()
        target=b.MOUNT/b.ARCHIVE/'agi'/('release-'+job)
        stage=target.with_name('.'+target.name+'.partial')
        b.no_alias(target);b.no_alias(stage)
        def guard():
            if not os.path.ismount(b.MOUNT) or os.stat(b.MOUNT).st_dev!=bound['device']:raise ValueError('recovery_volume_changed')
        with b.lock('agi-release'):
            guard()
            if target.exists():
                saved=b.load(target/'prepared.json')
                bound_approval(b,saved)
                app_bound=saved.get('source_bindings',{}).get(str(app))
                if (saved.get('before')!=before or not app_bound
                    or b.manifest(app,guard)!=source_manifest(b,saved,app_bound)):
                    raise ValueError('recovery_source_conflict')
                if not local.exists():
                    if saved.get('status')!='direct_recovery_prepared' or saved.get('target_volume_uuid')!=bound['uuid'] or saved.get('recovery')!=str(target):
                        raise ValueError('recovery_proof_invalid')
                    if b.manifest(target/'payload',guard)!=saved['manifest']:raise ValueError('recovery_readback_changed')
                    b.durable_tree(target);guard();b.atomic(local,saved)
                proof=check_external(target,local)
                if proof['before']!=before:raise ValueError('recovery_source_conflict')
                return target,local
            app_manifest=b.manifest(app,guard)
            app_fp=b.fingerprint(app_manifest)
            prior=b.load(pending) if pending.exists() else None
            if prior and (prior.get('source_core_hash')!=before['core_hash'] or prior.get('revision')!=before['revision']
                          or prior.get('application_sha256',app_fp)!=app_fp):raise ValueError('recovery_pending_source_changed')
            needed=sum(r.get('size',0) for r in app_manifest.values())+128*1024*1024
            if shutil.disk_usage(b.MOUNT).free<needed:raise ValueError('recovery_insufficient_space')
            if stage.exists():
                if not prior or prior.get('stage_identity')!=[stage.stat().st_dev,stage.stat().st_ino]:raise ValueError('recovery_stage_conflict')
            else:
                guard();b.private(stage)
            intent.update(application_sha256=app_fp,stage_identity=[stage.stat().st_dev,stage.stat().st_ino],
                          recovery=str(target),target_volume_uuid=bound['uuid'])
            b.atomic(pending,intent)
            payload=b.private(stage/'payload')
            sources={};bindings={};policy=provenance_approval(b)
            def copy(source,destination):
                source=Path(source);destination=Path(destination)
                b.no_alias(source);b.no_alias(destination)
                if not destination.is_relative_to(payload):raise PermissionError('recovery_copy_scope')
                m=b.manifest(source,guard)
                identity=b.tree_identity(source)
                b.copy_tree(source,destination,m,guard)
                observed=b.manifest(source,guard);copied=b.manifest(destination,guard)
                if (b.tree_identity(source)!=identity or observed!=m
                    or not b.manifests_equivalent(copied,m,policy)):raise ValueError('recovery_source_or_copy_changed')
                sources[str(source)]=m
                bindings[str(source)]={'identity':identity,'manifest_sha256':b.fingerprint(m),'payload_relative':destination.relative_to(payload).as_posix(),
                    'original_manifest':m,'source_observed_manifest':observed,
                    'original_provenance':{key:{'present':PROVENANCE in row['xattrs'],'value_base64':row['xattrs'].get(PROVENANCE)} for key,row in m.items()}}
            details=writer(payload,copy,guard)
            copy(app,payload/'AGI-replaced.app')
            b.verify_apps(payload/'AGI-replaced.app')
            for source,m in sources.items():
                if b.tree_identity(Path(source))!=bindings[source]['identity'] or b.manifest(Path(source),guard)!=m:raise ValueError('recovery_source_changed')
            if b.fingerprint(b.manifest(app,guard))!=app_fp:raise ValueError('recovery_application_changed')
            manifest=b.manifest(payload,guard);b.durable_tree(payload);guard()
            if b.manifest(payload,guard)!=manifest or b.volume()!=bound:raise ValueError('recovery_durability_readback_failed')
            proof={'status':'direct_recovery_prepared','target_volume_uuid':bound['uuid'],'recovery':str(target),
                   'before':before,'details':details,'manifest':manifest,'manifest_sha256':b.fingerprint(manifest),'source_bindings':bindings,
                   'database_restore_allowed':False,'source_originals_deleted':False,'prepared_at':time.time()}
            if policy:
                if provenance_approval(b)!=policy:raise ValueError('recovery_approval_changed')
                proof.update(provenance_approval=policy,local_receipt=str(local))
            b.atomic(stage/'manifest.json',{**before,**details})
            b.atomic(stage/'prepared.json',proof);guard();b.exclusive_move(stage,target);b.sync_directory(target.parent)
            # One identical, durable receipt on each volume, before publication.
            b.atomic(local,proof);check_external(target,local)
            if pending.exists():pending.unlink();b.sync_directory(pending.parent)
            return target,local
    except (OSError,ValueError) as exc:
        # Never include old code, database rows or application bytes in a pending
        # record. Keep an existing partial-package binding intact across retries.
        saved=b.load(pending) if pending.exists() else intent
        saved.update(status='deferred_t9',reason=str(exc)[:160],at=time.time());b.atomic(pending,saved)
        raise RecoveryDeferred(str(exc)) from exc
