"""Owner-authorized conversation reads, separate from memory and delivery grants.

Only native primary-session metadata can admit a new conversation.  Existing
personal registrations keep their original meaning; this module never changes
the shared personal-v1 classifier or its memory/report consumers.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import json
import os
from pathlib import Path
import pwd
import re
import sqlite3
import stat
import sys
import time

from activity_mvp.source_governance import personal_sources as personal_scope
from activity_mvp.source_governance import personal_store

POLICY = "owner-primary-conversations-20261016-v1"
POLICY_FILE = "conversation-authorization.json"
MAX_METADATA = 1024 * 1024
PRIMARY_CODEX = {"cli", "vscode", "desktop", "app", "tui", "codex_cli", "codex_desktop"}
_SOURCE_READS = ContextVar('agi_conversation_source_reads', default=None)


class _SourceReads:
    """Reuse read dependencies, never native admission or permission results."""
    def __init__(self):
        self.policy = None
        self.invalid = False

    def current(self):
        if self.invalid:
            return None
        try:
            if self.policy is None:
                from .retirement_snapshot import Snapshot
                # Own this batch's snapshot. Do not borrow another consumer's
                # CURRENT scope, which may have different source bindings.
                self.policy = Snapshot(personal_scope)
                self.policy.policy = deepcopy(self.policy.policy)
            if self.policy.sources is personal_scope and self.policy.unchanged():
                return self.policy
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError):
            pass
        self.invalid = True
        return None


@contextmanager
def source_read_batch():
    """Bound one preparation phase; subsequent submission checks start fresh."""
    prior = _SOURCE_READS.get()
    if prior is not None:
        yield
        return
    reader = _SourceReads()
    token = _SOURCE_READS.set(reader)
    try:
        yield
        if reader.invalid or (reader.policy is not None and reader.current() is None):
            raise PermissionError('source_policy_changed_during_read')
    finally:
        _SOURCE_READS.reset(token)
        if reader.policy is not None:
            reader.policy.close()


def policy_path():
    return personal_scope.ROOT / POLICY_FILE


def _regular(path):
    from .local_checks import checkpoint
    checkpoint()
    path = Path(path)
    if not path.is_absolute() or "\x00" in str(path) or ".." in path.parts:
        raise ValueError("source_path_invalid")
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("source_alias_not_registered")
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        raise ValueError("source_identity_unverified")
    return info


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def binding():
    """Changing, removing or revoking the grant invalidates an in-flight read."""
    try:
        info = _regular(policy_path())
        return _identity(info)
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return ("unavailable",)


def bind(check=lambda: True):
    """Bind actual reads/submissions to the grant version at operation start."""
    expected = binding()
    def current():
        from .local_checks import checkpoint
        checkpoint()
        return binding() == expected and check()
    return current


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate_authorization_field')
        value[key] = item
    return value


def authorization(path=None):
    path = policy_path() if path is None else Path(path)
    info = _regular(path)
    if info.st_mode & 0o077 or info.st_size > 65536:
        raise ValueError("conversation_authorization_not_private")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if _identity(os.fstat(fd)) != _identity(info):
            raise ValueError("conversation_authorization_changed")
        raw = os.read(fd, 65537)
        if _identity(os.fstat(fd)) != _identity(info):
            raise ValueError("conversation_authorization_changed")
    finally:
        os.close(fd)
    value = json.loads(raw, object_pairs_hook=_unique_object)
    if (value.get("schema_version") != 1 or value.get("policy") != POLICY
            or value.get("owner_id") != "local-owner" or type(value.get("enabled")) is not bool
            or type(value.get("lookback_days")) is not int or value.get("lookback_days") != 14 or type(value.get("revision")) is not int
            or value["revision"] < 1 or not isinstance(value.get("sources"), list)
            or not 1 <= len(value["sources"]) <= 32):
        raise ValueError("conversation_authorization_invalid")
    actor = value.get("authorization", {})
    if (actor.get("kind") != "owner_explicit" or actor.get("platform") != "codex"
            or not all(isinstance(actor.get(k), str) and actor[k] for k in ("session", "turn", "observed_at"))):
        raise ValueError("conversation_authorization_missing")
    for source in value["sources"]:
        if (not isinstance(source, dict) or source.get("platform") not in {"codex", "claude", "hermes"}
                or not isinstance(source.get("id"), str) or not source["id"]
                or not isinstance(source.get("path"), str) or not Path(source["path"]).is_absolute()):
            raise ValueError("conversation_source_binding_invalid")
    if _identity(_regular(path)) != _identity(info):
        raise ValueError("conversation_authorization_changed")
    return value


def _company(meta, policy=None):
    # Reuse registered corporate roots and explicit mixed-source metadata.
    # Text, titles, quoted instructions and model classifications are not grants.
    if meta.get("mixed") in (True,1,'true','True'):
        return True
    if policy is not None:
        return any(policy.company_path(meta[k])
                   for k in ("path", "cwd", "root", "project_path", "source_path")
                   if isinstance(meta.get(k), str) and meta[k])
    roots = personal_scope.policy()["company_roots"]
    return any(personal_scope.within(meta[k], root)
               for k in ("path", "cwd", "root", "project_path", "source_path")
               if isinstance(meta.get(k), str) and meta[k]
               for root in roots)


def _native_jsonl(source, path, session):
    """Extract metadata only; no user/assistant text is returned or retained."""
    before = _regular(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if _identity(os.fstat(fd)) != _identity(before):
            raise ValueError("source_changed")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            if source["platform"] == "codex":
                raw = stream.readline(MAX_METADATA + 1)
                if len(raw) > MAX_METADATA or not raw.endswith(b"\n"):
                    raise ValueError("native_metadata_unavailable")
                item = json.loads(raw)
                meta = item.get("payload", {})
                if (item.get("type") != "session_meta" or meta.get("id") != session
                        or not isinstance(meta.get("source"), str) or meta["source"] not in PRIMARY_CODEX):
                    raise ValueError("primary_session_unverified")
            else:
                meta = None
                for _ in range(32):
                    raw = stream.readline(MAX_METADATA + 1)
                    if not raw:
                        break
                    if len(raw) > MAX_METADATA or not raw.endswith(b"\n"):
                        raise ValueError("native_metadata_unavailable")
                    item = json.loads(raw)
                    if item.get("type") not in {"user", "assistant"}:
                        continue
                    if (item.get("sessionId") != session or item.get("isSidechain")
                            or item.get("isMeta") or item.get("userType") not in {None, "external"}):
                        raise ValueError("primary_session_unverified")
                    meta = item
                    break
                if meta is None:
                    raise ValueError("native_metadata_unavailable")
            if not isinstance(meta.get("cwd"), str) or not Path(meta["cwd"]).is_absolute():
                raise ValueError("project_identity_unknown")
            for key in ('project_path','root'):
                value=meta.get(key)
                if value is not None and value!='' and (not isinstance(value,str) or not Path(value).is_absolute()):
                    raise ValueError('project_identity_unknown')
            metadata = {k: meta[k] for k in ("cwd", "project_path", "root", "mixed") if k in meta}
        if _identity(os.fstat(fd)) != _identity(before) or _identity(_regular(path)) != _identity(before):
            raise ValueError("source_changed")
        return metadata
    finally:
        os.close(fd)


def _native_hermes(source, path, session):
    # Only the local primary profile is added by this grant. Existing explicitly
    # registered gateway sessions still use their original authorization.
    if source["id"] != "hermes":
        raise ValueError("profile_identity_unknown")
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.1) as conn:
        from .local_checks import Connection
        conn = Connection(conn)
        conn.execute("PRAGMA query_only=ON")
        columns={row[1] for row in conn.execute('PRAGMA table_info(sessions)')}
        fields=['source','user_id','parent_session_id','cwd']
        if not {'id',*fields}<=columns:
            raise ValueError('primary_project_metadata_unavailable')
        fields += [name for name in ('git_repo_root','profile_name') if name in columns]
        row = conn.execute('SELECT '+','.join(fields)+' FROM sessions WHERE id=?', (session,)).fetchone()
        meta=dict(zip(fields,row)) if row else {}
        if (not row or meta['source'] not in {"cli", "terminal", "desktop", "tui"}
                or str(meta['user_id'] or "") not in {"", "local", "local_user", pwd.getpwuid(os.getuid()).pw_name}):
            raise ValueError("owner_identity_unknown")
        if meta['parent_session_id'] or meta.get('profile_name') not in {None,'','default'}:
            raise ValueError('primary_session_unverified')
        if not isinstance(meta['cwd'],str) or not Path(meta['cwd']).is_absolute():
            raise ValueError('project_identity_unknown')
        project=meta.get('git_repo_root')
        if project and (not isinstance(project,str) or not Path(project).is_absolute()):
            raise ValueError('project_identity_unknown')
    return {'cwd':meta['cwd'],'project_path':project}


def admission(session, path, source_id=None, *, now=None):
    """Return allowed/denied/unknown; neither authorizes memory nor writes data."""
    reader = _SOURCE_READS.get()
    if reader is None:
        return _admission(session, path, source_id, now=now)
    policy = reader.current()
    if policy is not None:
        result = _admission(session, path, source_id, now=now, policy=policy)
        if reader.current() is not None:
            return result
    return {"state": "unknown", "reason": "source_policy_changed_during_read"}


def _admission(session, path, source_id=None, *, now=None, policy=None):
    def result(state, reason, **extra):
        return {"state": state, "reason": reason, **extra}
    if not isinstance(session, str) or not session or not isinstance(path, str) or not path:
        return result("unknown", "source_metadata_missing")
    if not Path(path).is_absolute() or "\x00" in path:
        return result("unknown", "source_path_invalid")
    meta = {"session_id": session, "path": path, "source_id": source_id}
    try:
        if (policy.classify(meta) == 'personal' if policy is not None else personal_scope.admitted(meta)):
            return result("allowed", "existing_personal_registration")
        if _company(meta, policy):
            return result("denied", "company_source_stopped")
        grant_before = binding()
        try:
            grant = authorization()
        except FileNotFoundError:
            return result("unknown", "conversation_authorization_missing")
        if not grant["enabled"]:
            return result("denied", "conversation_authorization_revoked")
        candidate = Path(path)
        matches = []
        for source in grant["sources"]:
            root = Path(source["path"])
            if source_id is not None and source["id"] != source_id:
                continue
            if (candidate == root if source["platform"] == "hermes" else candidate.is_relative_to(root)):
                matches.append(source)
        if len(matches) != 1:
            return result("unknown", "registered_source_unverifiable")
        source = matches[0]
        info = _regular(candidate)
        # SQLite's WAL may contain recent messages without changing the main
        # database mtime. Hermes enforces the window in its message query.
        if source['platform']!='hermes' and info.st_mtime < (time.time() if now is None else now) - 14 * 86400:
            return result("denied", "outside_conversation_window")
        metadata = (_native_hermes(source, candidate, session) if source["platform"] == "hermes"
                    else _native_jsonl(source, candidate, session))
        if _company(metadata, policy):
            return result("denied", "company_source_stopped")
        if binding() != grant_before:
            return result("unknown", "conversation_authorization_changed")
        return result("allowed", "authenticated_primary_conversation", policy=POLICY, revision=grant["revision"])
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError, sqlite3.Error):
        return result("unknown", "native_source_unverifiable")


def allowed(session, path, source_id=None):
    return admission(session, path, source_id)["state"] == "allowed"


def earliest_observation(session, path, configured_since):
    if personal_scope.admitted({'session_id': session, 'path': path}):
        return configured_since
    return max(configured_since, time.time() - 14 * 86400)


def _legacy_observation_allowed(session, path):
    reader = _SOURCE_READS.get()
    if reader is not None:
        # Share the phase's dependencies, never its classification result. A
        # failed dependency must not fall back to another consumer's snapshot.
        policy = reader.current()
        if policy is None:
            return False
        try:
            legacy = policy.classify({'session_id': session, 'path': path}) == 'personal'
            if reader.current() is not None:
                return legacy
        except (OSError, ValueError, TypeError, KeyError, AttributeError, RuntimeError):
            reader.invalid = True
        return False
    from .retirement_snapshot import CURRENT
    current = CURRENT.get()
    meta = {'session_id': session, 'path': path}
    legacy = (current.classify(meta) == 'personal'
              if current is not None and getattr(current, 'sources', None) is personal_scope
              else personal_scope.admitted(meta))
    return legacy


def _recent_observation_allowed(observed):
    return type(observed) in (int,float) and time.time()-14*86400<=observed<=time.time()+300


def observation_allowed(session,path,observed):
    # Public callers retain the original fail-closed classification behavior.
    # ReadView can order its timestamp test first because it separately binds
    # and checks every source's current policy and native identity.
    legacy = _legacy_observation_allowed(session,path)
    reader = _SOURCE_READS.get()
    if reader is not None and reader.invalid:
        return False
    return legacy or _recent_observation_allowed(observed)


def _projected_legacy_observation_allowed(session, path):
    """Deny old, unregistered sessions without resolving unrelated paths.

    The installed legacy classifier cannot return personal for a nonempty
    session absent from its explicit session list. Use this only to reject a
    SQL time witness, after checking the phase's current policy dependencies;
    it cannot positively admit a source or replace the same-row native gate.
    """
    reader = _SOURCE_READS.get()
    if reader is not None:
        policy = reader.current()
        if policy is None:
            return False
        try:
            if isinstance(session, str) and session and session not in policy.policy['personal_session_ids']:
                return False
        except (ValueError, TypeError, KeyError, AttributeError):
            reader.invalid = True
            return False
    return _legacy_observation_allowed(session, path)


def _projected_observation_allowed(session, path, observed):
    """Window predicate only; the same SQL row still requires live admission.

    Recent rows do not need a second legacy classification before the native
    registration predicate. This never grants a source: filtered() rejects a
    projection missing that separate mandatory same-row predicate. A positive
    older-row witness retains live legacy classification and batch invalidation.
    """
    return _recent_observation_allowed(observed) or _projected_legacy_observation_allowed(session, path)


def view_predicates(raw, registered):
    inherited=getattr(registered.conn,'views',None)
    if inherited is not None:return dict(inherited)
    inherited={}
    for table in getattr(registered.conn,'tables',()):
        row=raw.execute("SELECT sql FROM sqlite_temp_master WHERE type='view' AND name=?",('personal_'+table,)).fetchone()
        marker='AS SELECT * FROM main.'+table+' WHERE '
        if not row or marker not in row[0]:raise ValueError('legacy_conversation_view_unverifiable')
        inherited[table]=row[0].split(marker,1)[1]
    return inherited


def _bounded_relations(views, tables, kind):
    """Check referenced objects, and each distinct native source per context.

    These are live SQL predicates, not cached permission decisions. All current
    and retired source pairs remain mandatory, including invalid/NULL paths.
    Keep SQL's NULL behavior for a negative membership check as well.
    """
    if kind != 'reflection':
        replacements = {
            'quotes': ('message_id IN (SELECT id FROM temp.personal_messages)',
                       'EXISTS(SELECT 1 FROM temp.personal_messages pm WHERE pm.id=quotes.message_id)'),
            'work_items': ('reference IN (SELECT id FROM temp.personal_incidents)',
                           'EXISTS(SELECT 1 FROM temp.personal_incidents pi WHERE pi.id=work_items.reference)'),
        }
        for table in ('samples', 'decisions', 'notifications', 'jobs'):
            replacements[table] = ('incident_id IN (SELECT id FROM temp.personal_incidents)',
                'EXISTS(SELECT 1 FROM temp.personal_incidents pi WHERE pi.id=' + table + '.incident_id)')
        for table, (before, after) in replacements.items():
            if table in views:
                views[table] = views[table].replace(before, after)
        if 'incidents' in views:
            views['incidents'] = views['incidents'].replace(
                'q.message_id NOT IN (SELECT id FROM temp.personal_messages)',
                "CASE WHEN q.message_id IS NULL THEN NOT EXISTS(SELECT 1 FROM temp.personal_messages) "
                "ELSE NOT EXISTS(SELECT 1 FROM temp.personal_messages pm WHERE pm.id=q.message_id) "
                "AND NOT EXISTS(SELECT 1 FROM temp.personal_messages pm WHERE pm.id IS NULL) END")
        return
    for context in ('contexts', 'retired_contexts'):
        if context not in views:
            continue
        for turns in ('turns', 'retired_turns'):
            if turns not in tables:
                continue
            before = ('NOT EXISTS(SELECT 1 FROM main.' + turns + ' t WHERE t.context_id=' + context
                + ".id AND NOT (personal_registered_metadata(t.session,CASE WHEN json_valid(t.reference) THEN json_extract(t.reference,'$.path') END)=1))")
            after = ("NOT EXISTS(SELECT 1 FROM (SELECT DISTINCT session,CASE WHEN json_valid(reference) "
                "THEN json_extract(reference,'$.path') END AS path FROM main." + turns
                + ' WHERE context_id=' + context + '.id) refs WHERE NOT (personal_registered_metadata(refs.session,refs.path)=1))')
            if before not in views[context]:
                raise ValueError('legacy_conversation_view_unverifiable')
            views[context] = views[context].replace(before, after)
    # Positive membership can use the referenced context's primary key. An IN
    # list eagerly checked every unrelated discussion, even for one turn.
    for turns in ('turns', 'retired_turns'):
        if turns not in views:
            continue
        members = 'SELECT id FROM temp.personal_contexts'
        matches = 'EXISTS(SELECT 1 FROM temp.personal_contexts pc WHERE pc.id=' + turns + '.context_id)'
        if turns == 'retired_turns' and 'retired_contexts' in views:
            members += ' UNION SELECT id FROM temp.personal_retired_contexts'
            matches += ' OR EXISTS(SELECT 1 FROM temp.personal_retired_contexts pc WHERE pc.id=' + turns + '.context_id)'
        before = 'context_id IN (' + members + ')'
        if before not in views[turns]:
            raise ValueError('legacy_conversation_view_unverifiable')
        views[turns] = views[turns].replace(before, '(' + matches + ')')


def _session_list_prefix(predicate):
    # Session IDs are SQL string literals, not necessarily UUIDs. A ')' or SQL
    # punctuation inside an escaped literal cannot terminate this prefix.
    match = re.match(r"^session IN \((?:NULL|'(?:[^']|'')*'(?:,'(?:[^']|'')*')*)\)", predicate)
    if match is None or (predicate[match.end():] and not predicate[match.end():].startswith(' AND ')):
        raise ValueError('legacy_conversation_view_unverifiable')
    return match[0]


def filtered(store, kind="feedback"):
    """Extend only conversation views; inherit every memory/work-item predicate."""
    original = getattr(store, "original", store)
    projected_kind = getattr(original.conn, 'conversation_scope_kind', None)
    if projected_kind is not None:
        if projected_kind != kind:
            raise ValueError('conversation_projection_kind_mismatch')
        # ReadView already combines current identity, policy and display gates.
        # Re-wrapping it expands its underlying relations a second time and
        # replaces its version-aware native-source predicate with an uncached one.
        return original
    legacy = personal_store.filtered(original, kind)
    if not personal_scope.company_stopped():
        return legacy
    try:
        if not authorization()["enabled"]:
            return legacy
    except (OSError, ValueError, TypeError, KeyError):
        return legacy
    raw = original.conn
    views = view_predicates(raw,legacy)
    # The final per-row metadata predicate remains the positive admission gate.
    for table in ("turns", "retired_turns", "messages"):
        if table in views:
            native_gate = " AND personal_registered_metadata(session,CASE WHEN json_valid(reference) THEN json_extract(reference,'$.path') END)=1"
            prefix = _session_list_prefix(views[table])
            membership = ""
            if kind == 'reflection':
                members = 'SELECT id FROM temp.personal_contexts'
                if table == 'retired_turns' and 'retired_contexts' in views:
                    members += ' UNION SELECT id FROM temp.personal_retired_contexts'
                membership = ' AND context_id IN (' + members + ')'
            # Match the complete inherited row predicate, not a substring that
            # might occur in an optional OR branch or use another row's path.
            if views[table] != prefix + native_gate + membership:
                raise ValueError("legacy_conversation_view_unverifiable")
            views[table] = ("personal_conversation_observation(session,CASE WHEN json_valid(reference) THEN json_extract(reference,'$.path') END,observed)=1"
                            + views[table][len(prefix):])
    tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ("contexts", "retired_contexts"):
        if table not in views:
            continue
        candidates = []
        for turns in ("turns", "retired_turns"):
            if turns in tables:
                # Time checks are cheap and stay per row. Legacy source status
                # is checked once per distinct pair in this statement, never
                # retained for another query or for the subsequent action.
                candidates.append("EXISTS(SELECT 1 FROM (SELECT session,CASE WHEN json_valid(reference) "
                    "THEN json_extract(reference,'$.path') END AS path,MAX(personal_recent_observation(observed)) AS recent "
                    'FROM main.' + turns + ' WHERE context_id=' + table + '.id GROUP BY session,path) refs '
                    'WHERE refs.recent=1 OR personal_legacy_observation(refs.session,refs.path)=1)')
        prefix = _session_list_prefix(views[table])
        views[table] = "(" + " OR ".join(candidates or ["0"]) + ")" + views[table][len(prefix):]
    # The all-source checks include the observation witness above, so it cannot
    # authorize a context whose only recent row has a denied native identity.
    _bounded_relations(views, tables, kind)
    raw.create_function("personal_registered_metadata", 2, lambda session, path: int(allowed(session, path)))
    raw.create_function('personal_conversation_observation',3,lambda session,path,observed:int(_projected_observation_allowed(session,path,observed)))
    raw.create_function('personal_recent_observation',1,lambda observed:int(_recent_observation_allowed(observed)))
    raw.create_function('personal_legacy_observation',2,lambda session,path:int(_projected_legacy_observation_allowed(session,path)))
    if raw.execute("PRAGMA query_only").fetchone()[0]:
        return personal_store.FilteredStore(original, list(views), views)
    for table, predicate in views.items():
        raw.execute("DROP VIEW IF EXISTS temp.personal_" + table)
        raw.execute("CREATE TEMP VIEW personal_" + table + " AS SELECT * FROM main." + table + " WHERE " + predicate)
    return personal_store.FilteredStore(original, list(views))
