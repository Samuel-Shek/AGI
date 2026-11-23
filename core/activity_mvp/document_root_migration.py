"""Reviewed, owner-operated root replacement; no daemon or public API entry.

The caller supplies a separately approved, hash-bound plan while holding the
existing maintenance/controller lock. Database authorization commits first;
the old registry revision then fails closed until exact sidecar projection.
An interrupted projection is repaired from the same durable receipt, never by
replaying a migration or restoring a database. Source bodies are never read.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path

from . import document_changes as d
from .presentation_scope import DisplayScope, POLICY_FILE
from .storage import document_schema_activation_present


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def plan_sha256(plan):
    return hashlib.sha256(_encoded(plan)).hexdigest()


def _controls(conn):
    return {
        "settings": [list(r) for r in conn.execute(
            "SELECT key,value FROM settings WHERE key IN "
            "('document_change_tracking_enabled','document_change_export_enabled','screen_capture_enabled') ORDER BY key")],
        "pause": [list(r) for r in conn.execute(
            "SELECT key,value FROM daemon_state WHERE key IN ('pause_mode','pause_until') ORDER BY key")],
    }


def _roots(conn):
    return [list(r) for r in conn.execute(
        "SELECT root_id,root_label,enabled,created_at FROM file_watch_roots ORDER BY root_id")]


def _validated_schema(conn):
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    value = str(row[0]) if row else ''
    if value not in {'32', '33'}:
        raise ValueError('migration_schema_unsupported')
    if value == '32' and document_schema_activation_present(conn):
        raise ValueError('migration_schema_activation_inconsistent')
    return value


def _identity(path):
    resolved = d._resolve_root(path)
    if str(resolved) != path:
        raise ValueError("migration_path_not_canonical")
    # Bind every directory in the path, including ancestors that could be
    # swapped between preparation and commit. Resolution rejects aliases.
    return [[str(p), p.stat().st_dev, p.stat().st_ino] for p in (resolved, *resolved.parents)]


def _validate_bindings(conn, registry, replaced_refs):
    secret = d._existing_hmac_key_readonly(conn)
    rows = {r[0]: r[2] for r in _roots(conn)}
    if set(rows) != {r['root_id'] for r in registry}:
        raise ValueError("migration_registry_root_set_changed")
    identities = {}
    for item in registry:
        path = item['path']
        if not Path(path).is_absolute() or os.path.normpath(path) != path:
            raise ValueError("migration_stored_path_invalid")
        if not hmac.compare_digest('root_' + d._hmac(secret, path)[:32], item['root_id']):
            raise ValueError("migration_stored_binding_mismatch")
        if item['root_ref'] in replaced_refs:
            if rows[item['root_id']] != 1:
                raise ValueError("migration_old_root_not_enabled")
        elif rows[item['root_id']]:
            identities[path] = _identity(path)
    return identities


def prepare_migration(conn, runtime_root, replacements, excluded_aliases, authority_sha256):
    """Read-only private plan. Approval must cover its exact SHA before apply.

    replacements contains old_ref, new_path, new_label. Explicit aliases only
    restrict document reads; display and personal-source policies are unchanged.
    """
    if conn.in_transaction:
        raise ValueError("migration_requires_idle_connection")
    if len(authority_sha256) != 64 or any(c not in '0123456789abcdef' for c in authority_sha256):
        raise ValueError("migration_authority_digest_required")
    root = Path(runtime_root).resolve()
    if d._connection_database_path(conn).parent != root:
        raise ValueError("migration_runtime_mismatch")
    with d.document_authorization_lock(conn, exclusive=False):
        schema_version = _validated_schema(conn)
        status, registry, revision = d._load_runtime_registry(root)
        if status != 'valid' or revision != d._root_authorization_revision(conn):
            raise ValueError("migration_registry_revision_mismatch")
        refs = {r['old_ref'] for r in replacements}
        if len(refs) != len(replacements) or not refs or not refs <= {r['root_ref'] for r in registry}:
            raise ValueError("migration_replacement_set_invalid")
        identities = _validate_bindings(conn, registry, refs)
        scope = DisplayScope(root)
        if scope.invalid or not scope.enabled:
            raise ValueError("migration_display_policy_required")
        policy_raw = d._read_private_registry(root / POLICY_FILE).decode()
        if hashlib.sha256(policy_raw.encode()).hexdigest() != scope.revision:
            raise ValueError("migration_policy_changed")
        exclusions = []
        secret = d._existing_hmac_key_readonly(conn)
        additions = []
        occupied = {r['root_id'] for r in registry}
        for replacement in replacements:
            path = replacement['new_path']
            identities[path] = _identity(path)
            rid = 'root_' + d._hmac(secret, path)[:32]
            if rid in occupied or scope.hidden(path):
                raise ValueError("migration_new_root_conflict_or_retired")
            occupied.add(rid)
            additions.append(dict(root_id=rid, root_ref=d._new_root_ref(), path=path,
                                  root_label=d._safe_root_label(replacement['new_label'])))
        for alias in excluded_aliases:
            if not Path(alias).is_absolute() or os.path.normpath(alias) != alias or not any(
                Path(alias).is_relative_to(Path(r['path'])) and alias != r['path'] for r in additions
            ):
                raise ValueError("migration_exclusion_outside_new_roots")
            owner = next(r for r in additions if Path(alias).is_relative_to(Path(r['path'])))
            exclusions.append([owner['root_id'], d._document_subtree_hmac(secret, Path(alias))])
        if not scope.unchanged():
            raise ValueError("migration_policy_changed")
        return dict(version=1, authority_sha256=authority_sha256,
                    database=str(d._connection_database_path(conn)), runtime_root=str(root),
                    revision=revision, epoch=d._document_change_epoch(conn), controls=_controls(conn),
                    schema_version=schema_version,
                    roots=_roots(conn), registry=registry, identities=identities,
                    replacements=[dict(old_ref=r['old_ref'], new_ref=n['root_ref']) for r, n in zip(replacements, additions)],
                    additions=additions, exclusions=exclusions,
                    exclusions_before=[list(r) for r in conn.execute('SELECT root_id,path_hmac FROM file_watch_root_exclusions ORDER BY root_id,path_hmac')],
                    policy_sha256=scope.revision)


def _receipt(conn, digest):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='file_watch_root_migrations'").fetchone():
        return None
    row = conn.execute("SELECT receipt_json FROM file_watch_root_migrations WHERE plan_sha256=?", (digest,)).fetchone()
    return json.loads(row[0]) if row else None


def _project(conn, plan, receipt):
    # Never revive a later withdrawal, pause, or root edit, including when
    # registry projection was interrupted after the database committed.
    if (str(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]) != '33'
            or d._root_authorization_revision(conn) != receipt['revision_after']
            or d._document_change_epoch(conn) != receipt['epoch_after']
            or _roots(conn) != receipt['roots_after'] or _controls(conn) != plan['controls']):
        raise ValueError("migration_committed_state_changed")
    for path, identity in plan['identities'].items():
        if _identity(path) != identity:
            raise ValueError("migration_directory_identity_changed")
    root = Path(plan['runtime_root'])
    status, registry, revision = d._load_runtime_registry(root)
    expected_registry = plan['registry'] + plan['additions']
    if status != 'valid' or (registry, revision) not in (
        (plan['registry'], plan['revision']), (expected_registry, receipt['revision_after'])
    ):
        raise ValueError("migration_registry_projection_changed")
    scope = DisplayScope(root)
    if scope.invalid or not scope.enabled or scope.revision != plan['policy_sha256']:
        raise ValueError('migration_policy_changed')
    if [list(r) for r in conn.execute('SELECT root_id,path_hmac FROM file_watch_root_exclusions ORDER BY root_id,path_hmac')] != sorted(plan['exclusions_before'] + plan['exclusions']):
        raise ValueError('migration_exclusions_changed')
    if (registry, revision) != (expected_registry, receipt['revision_after']):
        d._write_runtime_registry(root, expected_registry, receipt['revision_after'])
    if d._load_runtime_registry(root) != ('valid', expected_registry, receipt['revision_after']):
        raise ValueError("migration_projection_readback_failed")
    scope = DisplayScope(root)
    if scope.invalid or scope.revision != plan['policy_sha256']:
        raise ValueError("migration_policy_readback_failed")
    return dict(status='configured', migration=receipt['plan_sha256'],
                revision=receipt['revision_after'], raw_access=False)


def apply_migration(conn, plan, *, approved_sha256):
    """Apply once; subsequent calls only reconcile the original receipt.

    A commit exception is intentionally not retried or compensated. Close the
    connection, inspect the receipt through a fresh read-only connection, then
    call reconcile_migration explicitly if the receipt exists. No receipt means
    no committed database changes and no sidecar writes by this operation.
    """
    if conn.in_transaction or plan_sha256(plan) != approved_sha256 or plan.get('version') != 1:
        raise ValueError("migration_approval_or_connection_invalid")
    if str(d._connection_database_path(conn)) != plan['database']:
        raise ValueError("migration_database_changed")
    with d.document_authorization_lock(conn, exclusive=True):
        d._acquire_document_change_write_lock(conn)
        try:
            _validated_schema(conn)
            receipt = _receipt(conn, approved_sha256)
            if receipt is not None:
                return _project(conn, plan, receipt)
            if (_roots(conn) != plan['roots'] or _controls(conn) != plan['controls']
                    or str(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]) != plan['schema_version']
                    or d._document_change_epoch(conn) != plan['epoch']
                    or d._root_authorization_revision(conn) != plan['revision']
                    or d._load_runtime_registry(plan['runtime_root']) != ('valid', plan['registry'], plan['revision'])):
                raise ValueError("migration_precondition_changed")
            _validate_bindings(conn, plan['registry'], {r['old_ref'] for r in plan['replacements']})
            for path, identity in plan['identities'].items():
                if _identity(path) != identity:
                    raise ValueError("migration_directory_identity_changed")
            scope = DisplayScope(plan['runtime_root'])
            if scope.invalid or not scope.enabled or scope.revision != plan['policy_sha256']:
                raise ValueError("migration_policy_changed")
            if [list(r) for r in conn.execute('SELECT root_id,path_hmac FROM file_watch_root_exclusions ORDER BY root_id,path_hmac')] != plan['exclusions_before']:
                raise ValueError('migration_exclusions_changed')
            now = d._now()
            old_refs = {r['old_ref'] for r in plan['replacements']}
            for item in plan['registry']:
                if item['root_ref'] in old_refs:
                    conn.execute("UPDATE file_watch_roots SET enabled=0,updated_at=? WHERE root_id=?", (now, item['root_id']))
            # A changed native stream starts a fresh interval. Retain old
            # checkpoint values and gap history on the disabled old roots.
            conn.execute("UPDATE file_watch_checkpoints SET last_event_id=NULL,coverage_status='partial',"
                         "coverage_started_at=NULL,updated_at=? WHERE root_id IN "
                         "(SELECT root_id FROM file_watch_roots WHERE enabled=1)", (now,))
            for item in plan['additions']:
                conn.execute("INSERT INTO file_watch_roots(root_id,root_label,enabled,baseline_status,created_at,updated_at) "
                             "VALUES(?,?,1,'pending',?,?)", (item['root_id'], item['root_label'], now, now))
                conn.execute("INSERT INTO file_watch_checkpoints(root_id,coverage_status,updated_at) VALUES(?,'pending',?)", (item['root_id'], now))
            conn.executemany('INSERT INTO file_watch_root_exclusions(root_id,path_hmac) VALUES(?,?)', plan['exclusions'])
            epoch = d.bump_document_change_epoch(conn)
            _, revision = d._bump_root_authorization_revision(conn)
            receipt = dict(plan_sha256=approved_sha256, authority_sha256=plan['authority_sha256'],
                           revision_before=plan['revision'], revision_after=revision, epoch_after=epoch,
                           replacements=plan['replacements'], roots_after=_roots(conn), committed_at=now)
            conn.execute("CREATE TABLE IF NOT EXISTS file_watch_root_migrations (plan_sha256 TEXT PRIMARY KEY,receipt_json TEXT NOT NULL)")
            conn.execute("INSERT INTO file_watch_root_migrations VALUES(?,?)", (approved_sha256, _encoded(receipt).decode()))
            conn.execute("UPDATE meta SET value='33' WHERE key='schema_version'")
            # No external projection happens before the commit returns. On an
            # unknown outcome the caller must inspect this unique receipt.
            conn.commit()
            d._acquire_document_change_write_lock(conn)
            return _project(conn, plan, receipt)
        finally:
            if conn.in_transaction:
                conn.rollback()


def reconcile_migration(conn, plan, *, approved_sha256):
    """Repair sidecars of a proven commit only, never create authorization."""
    if conn.in_transaction or plan_sha256(plan) != approved_sha256 or str(d._connection_database_path(conn)) != plan['database']:
        raise ValueError("migration_approval_or_connection_invalid")
    with d.document_authorization_lock(conn, exclusive=True):
        d._acquire_document_change_write_lock(conn)
        try:
            receipt = _receipt(conn, approved_sha256)
            if receipt is None:
                raise ValueError("migration_commit_not_proven")
            return _project(conn, plan, receipt)
        finally:
            conn.rollback()
