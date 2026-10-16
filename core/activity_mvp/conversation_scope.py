"""Owner-authorized conversation reads, separate from memory and delivery grants.

Only native primary-session metadata can admit a new conversation.  Existing
personal registrations keep their original meaning; this module never changes
the shared personal-v1 classifier or its memory/report consumers.
"""
from __future__ import annotations

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


def policy_path():
    return personal_scope.ROOT / POLICY_FILE


def _regular(path):
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
    return lambda: binding() == expected and check()


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


def _company(meta):
    # Reuse registered corporate roots and explicit mixed-source metadata.
    # Text, titles, quoted instructions and model classifications are not grants.
    if meta.get("mixed") in (True,1,'true','True'):
        return True
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
    def result(state, reason, **extra):
        return {"state": state, "reason": reason, **extra}
    if not isinstance(session, str) or not session or not isinstance(path, str) or not path:
        return result("unknown", "source_metadata_missing")
    if not Path(path).is_absolute() or "\x00" in path:
        return result("unknown", "source_path_invalid")
    meta = {"session_id": session, "path": path, "source_id": source_id}
    try:
        if personal_scope.admitted(meta):
            return result("allowed", "existing_personal_registration")
        if _company(meta):
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
        if _company(metadata):
            return result("denied", "company_source_stopped")
        if binding() != grant_before:
            return result("unknown", "conversation_authorization_changed")
        return result("allowed", "authenticated_primary_conversation", policy=POLICY, revision=grant["revision"])
    except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error):
        return result("unknown", "native_source_unverifiable")


def allowed(session, path, source_id=None):
    return admission(session, path, source_id)["state"] == "allowed"


def earliest_observation(session, path, configured_since):
    if personal_scope.admitted({'session_id': session, 'path': path}):
        return configured_since
    return max(configured_since, time.time() - 14 * 86400)


def observation_allowed(session,path,observed):
    if personal_scope.admitted({'session_id':session,'path':path}):return True
    return type(observed) in (int,float) and time.time()-14*86400<=observed<=time.time()+300


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


def filtered(store, kind="feedback"):
    """Extend only conversation views; inherit every memory/work-item predicate."""
    original = getattr(store, "original", store)
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
            views[table], count = re.subn(r"^session IN \([^)]*\)",
                "personal_conversation_observation(session,CASE WHEN json_valid(reference) THEN json_extract(reference,'$.path') END,observed)=1", views[table], count=1)
            if count != 1:
                raise ValueError("legacy_conversation_view_unverifiable")
    tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ("contexts", "retired_contexts"):
        if table not in views:
            continue
        candidates = []
        for turns in ("turns", "retired_turns"):
            if turns in tables:
                candidates.append("EXISTS(SELECT 1 FROM main." + turns + " t WHERE t.context_id=" + table
                    + ".id AND personal_registered_metadata(t.session,CASE WHEN json_valid(t.reference) THEN json_extract(t.reference,'$.path') END)=1"
                    + " AND personal_conversation_observation(t.session,CASE WHEN json_valid(t.reference) THEN json_extract(t.reference,'$.path') END,t.observed)=1)")
        views[table] = re.sub(r"^session IN \([^)]*\)", "(" + " OR ".join(candidates or ["0"]) + ")", views[table], count=1)
    raw.create_function("personal_registered_metadata", 2, lambda session, path: int(allowed(session, path)))
    raw.create_function('personal_conversation_observation',3,lambda session,path,observed:int(observation_allowed(session,path,observed)))
    if raw.execute("PRAGMA query_only").fetchone()[0]:
        return personal_store.FilteredStore(original, list(views), views)
    for table, predicate in views.items():
        raw.execute("DROP VIEW IF EXISTS temp.personal_" + table)
        raw.execute("CREATE TEMP VIEW personal_" + table + " AS SELECT * FROM main." + table + " WHERE " + predicate)
    return personal_store.FilteredStore(original, list(views))
