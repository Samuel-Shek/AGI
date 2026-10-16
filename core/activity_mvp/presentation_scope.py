"""Owner-local display exclusions. Never grants source or action permission.

The private policy contains names and lexical path aliases, not source bodies.
An archive being offline cannot make an excluded source visible again.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from collections import OrderedDict
from functools import wraps
import json
import os
from pathlib import Path
import stat
import re
import hashlib

POLICY_FILE = "agi-display-scope.json"
_CURRENT = ContextVar("agi_display_scope", default=None)
_ROW_CACHE_ENTRIES = 100000
_ROW_CACHE_BYTES = 128 * 1024 * 1024


class DisplayScope:
    def __init__(self, runtime_root):
        self.path = Path(runtime_root) / POLICY_FILE
        self.enabled = False
        self.invalid = False
        self.names = ()
        self.paths = ()
        self.root_refs = frozenset()
        self.name_pattern = None
        self.reference_pattern = None
        self.memory_sources = None
        self.memory_source_fingerprint = None
        self.memory_policy = None
        self.source_policy = None
        self.memory_cache = {}
        self.document_refs = None
        self.fingerprint = None
        self.revision = ""
        self._text_visibility = OrderedDict()
        self._text_visibility_bytes = 0
        self._row_visibility = OrderedDict()
        self._row_visibility_bytes = 0
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return
        except OSError:
            self.invalid = True
            return
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise ValueError("display_scope_not_private")
            raw = os.read(fd, 65537)
            if len(raw) > 65536:
                raise ValueError("display_scope_too_large")
            value = json.loads(raw)
            if (value.get("version") != 1 or value.get("enabled") is not True
                    or not all(isinstance(value.get(k), list) for k in ("retired_names", "retired_paths", "retired_root_refs"))):
                raise ValueError("display_scope_invalid")
            if not all(isinstance(x, str) and x and "\x00" not in x
                       for k in ("retired_names", "retired_paths", "retired_root_refs") for x in value[k]):
                raise ValueError("display_scope_invalid")
            if any(not Path(x).is_absolute() or os.path.normpath(x) != x for x in value["retired_paths"]):
                raise ValueError("display_scope_path_invalid")
            self.names = tuple(x.casefold() for x in value["retired_names"])
            self.paths = tuple(value["retired_paths"])
            self.root_refs = frozenset(value["retired_root_refs"])
            self.name_pattern = re.compile('|'.join(re.escape(x) for x in self.names)) if self.names else None
            references = (*self.paths, *self.root_refs)
            self.reference_pattern = re.compile('|'.join(re.escape(x) for x in references)) if references else None
            self.fingerprint = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)
            self.revision = hashlib.sha256(raw).hexdigest()
            self.enabled = True
        except (ValueError, TypeError, AttributeError, OSError):
            self.invalid = True
        finally:
            os.close(fd)

    def hidden(self, value):
        if self.invalid:
            return True
        if not self.enabled:
            return False
        if isinstance(value, str):
            cached = self._text_visibility.get(value)
            if cached is not None:
                self._text_visibility.move_to_end(value)
                return cached[0]
            result = self._hidden_text(value)
            # Only immutable strings share an answer, within this one policy
            # object. The existing final policy/source checks still invalidate
            # the entire projection after a withdrawal or policy change.
            size = len(value.encode("utf-8", errors="surrogatepass"))
            if size <= 4096:
                while self._text_visibility and (
                        len(self._text_visibility) >= 50000
                        or self._text_visibility_bytes + size > 8 * 1024 * 1024):
                    _, (_, old_size) = self._text_visibility.popitem(last=False)
                    self._text_visibility_bytes -= old_size
                self._text_visibility[value] = (result, size)
                self._text_visibility_bytes += size
            return result
        if isinstance(value, dict):
            return any(self.hidden(x) for x in value.values())
        if isinstance(value, (tuple, list)):
            return any(self.hidden(x) for x in value)
        return False

    def _hidden_text(self, value):
        if ((self.name_pattern is not None and self.name_pattern.search(value.casefold()))
                or (self.reference_pattern is not None and self.reference_pattern.search(value))):
            return True
        # Decode escaped metadata too, including unicode-escaped names.
        if '\\' in value and value.lstrip().startswith(("{", "[", '"')):
            try:
                decoded = json.loads(value)
                if not isinstance(decoded, str) or decoded != value:
                    return self.hidden(decoded)
            except (ValueError, RecursionError):
                pass
        return False

    def visible_row(self, *values):
        # SQLite scalars are immutable. Only this scope's exact full row can
        # reuse its pure text exclusion; provenance/action checks stay separate.
        cached = self._row_visibility.get(values)
        if cached is not None:
            self._row_visibility.move_to_end(values)
            return cached[0]
        result = int(not self.hidden(values))
        size = sum(len(v.encode("utf-8", errors="surrogatepass")) if isinstance(v, str)
                   else len(v) if isinstance(v, bytes) else 8 for v in values)
        if size <= _ROW_CACHE_BYTES:
            while self._row_visibility and (len(self._row_visibility) >= _ROW_CACHE_ENTRIES
                    or self._row_visibility_bytes + size > _ROW_CACHE_BYTES):
                _, (_, old_size) = self._row_visibility.popitem(last=False)
                self._row_visibility_bytes -= old_size
            self._row_visibility[values] = (result, size)
            self._row_visibility_bytes += size
        return result

    def unchanged(self):
        if self.fingerprint is None:
            return not self.path.exists() and not self.path.is_symlink()
        try:
            info = self.path.lstat()
            return (stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                    and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
                    and self.fingerprint == (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size))
        except OSError:
            return False

    def hidden_closure(self, conn, item):
        """A child inherits its registered task's display source, never grants."""
        if self.hidden(item) or not self.unchanged():
            return True
        if not self.enabled:
            return False
        task_id = item.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            return True
        parent = conn.execute(
            "SELECT objective,scope_root FROM closure_tasks WHERE id=?", (task_id,)
        ).fetchone()
        return parent is None or self.hidden(tuple(parent)) or not self.unchanged()

    def hidden_memory(self, identifier, metadata=None):
        if not self.enabled:
            return self.invalid
        if self.memory_sources is None:
            path = Path.home() / ".hermes/memory-review-center/state/autonomy-policy.json"
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                try:
                    info = os.fstat(fd)
                    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                            or info.st_size > 2 * 1024 * 1024):
                        raise ValueError("display_memory_registry_invalid")
                    registry = json.loads(os.read(fd, 2 * 1024 * 1024 + 1))
                    if (registry.get("owner_id") != "local-owner"
                            or not all(isinstance(registry.get(k), dict) for k in ("bindings", "investigation_sources"))
                            or not all(isinstance(rows, list) and all(isinstance(row, dict) for row in rows)
                                       for k in ("bindings", "investigation_sources") for rows in registry[k].values())):
                        raise ValueError("display_memory_registry_invalid")
                    self.memory_sources = registry
                    self.memory_source_fingerprint = (path, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
                finally:
                    os.close(fd)
                self.memory_policy = self.policy_view()
            except (OSError, ValueError, TypeError, AttributeError):
                raise ValueError("display_memory_sources_unavailable") from None
        refs = [*self.memory_sources.get("bindings", {}).get(identifier, []),
                *self.memory_sources.get("investigation_sources", {}).get(identifier, [])]
        # Unregistered legacy sources are not certified personal merely by
        # lacking a company word. Their records/decisions remain untouched.
        meta = dict(metadata or {})
        meta.pop("source_id", None)
        meta["registered_paths"] = [row["path"] for row in refs if row.get("path")]
        # Registration metadata remains authoritative; names only narrow it.
        for row in refs:
            if self.hidden(row) or self.memory_policy.classify(row) in {"company", "mixed"}:
                return True
        key = json.dumps(meta, sort_keys=True)
        if key not in self.memory_cache:
            self.memory_cache[key] = self.memory_policy.classify(meta) != "personal"
        return not refs or self.hidden(meta) or self.memory_cache[key]

    def memory_unchanged(self):
        if self.memory_source_fingerprint is not None:
            if self.memory_policy is None:
                return False
            path, *expected = self.memory_source_fingerprint
            try:
                info = path.lstat()
                if expected != [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]:
                    return False
            except OSError:
                return False
        policies = [self.source_policy] if self.source_policy is not None else []
        if self.memory_policy is not None and self.memory_policy is not self.source_policy:
            policies.append(self.memory_policy)
        return all(policy.unchanged() for policy in policies)

    def policy_view(self):
        if self.source_policy is None:
            from .task_closure import personal_scope
            from .retirement_snapshot import Snapshot, CURRENT
            current = CURRENT.get()
            self.source_policy = current if current is not None and current.sources is personal_scope else Snapshot(personal_scope)
        return self.source_policy

    def retired_documents(self, conn):
        """Known hashed lineage also excludes duplicates under overlapping roots."""
        if self.document_refs is not None:
            return self.document_refs
        self.document_refs = frozenset()
        if not self.enabled or not self.paths:
            return self.document_refs
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'file_watch_roots', 'file_watch_state', 'file_change_versions'} <= tables:
            return self.document_refs
        known = {r[0] for r in conn.execute('SELECT root_id FROM file_watch_roots')}
        if not known:
            return self.document_refs
        from .document_changes import _existing_hmac_key_readonly, _hmac
        secret = _existing_hmac_key_readonly(conn)
        # Stored lexical binding only; never resolve an old/archive path.
        retired = known & {'root_' + _hmac(secret, path)[:32] for path in self.paths}
        if retired:
            placeholders = ','.join('?' for _ in retired)
            self.document_refs = frozenset(r[0] for r in conn.execute(
                'SELECT document_path_hmac FROM file_watch_state WHERE root_id IN (' + placeholders + ') '
                # Existing databases migrate this lineage onto immutable
                # versions, while their aggregate event table may predate it.
                'UNION SELECT document_path_hmac FROM file_change_versions WHERE root_id IN (' + placeholders + ')',
                (*retired, *retired)) if r[0])
        return self.document_refs

    def project(self, payload):
        unavailable = {"status": "unavailable", "reason": "display_scope_unavailable", "raw_access": False}
        if self.invalid or not self.unchanged() or not self.memory_unchanged():
            return unavailable
        if not self.enabled:
            return payload

        detail_withheld = False

        def clean(value):
            nonlocal detail_withheld
            if isinstance(value, list):
                result = []
                for row in value:
                    group_keys = {"members", "children", "items"} if isinstance(row, dict) else set()
                    grouped = isinstance(row, dict) and any(isinstance(row.get(k), list) for k in group_keys)
                    primary = {k: v for k, v in row.items() if k not in group_keys} if grouped else row
                    if not self.hidden(primary):
                        result.append(clean(row))
                return result
            if not isinstance(value, dict):
                return None if self.hidden(value) else value
            result = {}
            for key, child in value.items():
                if isinstance(child, dict) and self.hidden(child):
                    # Envelopes containing collections are cleaned recursively;
                    # a single retired detail/primary fact is withheld entirely.
                    if any(isinstance(x, list) for x in child.values()):
                        result[key] = clean(child)
                    else:
                        result[key] = None
                        if key in {"item", "incident", "memory", "session"}:
                            detail_withheld = True
                else:
                    result[key] = clean(child)
            if result.get("interface") == "attention-center" and isinstance(result.get("decisions"), list):
                if result.get("decision_count") is not None:
                    result["decision_count"] = len(result["decisions"])
                result["system_count"] = len(result.get("systems", []))
                result["resource_count"] = len(result.get("resources", []))
            if isinstance(result.get("members"), list) and "member_count" in result:
                result["member_count"] = len(result["members"])
            if isinstance(result.get("items"), list):
                if "item_count" in result:
                    result["item_count"] = len(result["items"])
                if "returned" in result:
                    result["returned"] = len(result["items"])
            return result

        result = clean(payload)
        if detail_withheld:
            return {**unavailable, "reason": "source_not_in_display_scope"}
        # Also protects unfamiliar/new response shapes and embedded JSON strings.
        if self.hidden(result) or not self.unchanged() or not self.memory_unchanged():
            return unavailable
        if isinstance(result, dict):
            result["display_scope_revision"] = self.revision
        return result


def for_connection(conn):
    current = _CURRENT.get()
    if current is not None:
        return current
    database = next((r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"), "")
    return DisplayScope(Path(database).parent) if database else DisplayScope(Path("/nonexistent-agi-runtime"))


@contextmanager
def display_scope(runtime_root):
    current = _CURRENT.get()
    if current is not None and current.path == Path(runtime_root) / POLICY_FILE:
        yield current
        return
    scope = DisplayScope(runtime_root)
    token = _CURRENT.set(scope)
    try:
        yield scope
    finally:
        _CURRENT.reset(token)


def project_for_runtime(payload, runtime_root):
    return for_runtime(runtime_root).project(payload)


def for_runtime(runtime_root):
    current = _CURRENT.get()
    path = Path(runtime_root) / POLICY_FILE
    return current if current is not None and current.path == path else DisplayScope(runtime_root)


class ReadView:
    """Read projection only: filters before aggregates/LIMIT, never edits rows."""
    def __init__(self, conn, scope, tables, personal_kind=None):
        self.raw = conn
        self.scope = scope
        self.views = {}
        conn.create_function("agi_display_visible", -1, scope.visible_row, deterministic=True)
        available = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        tables = tuple(tables)
        if 'closure_issues' in tables:
            if 'closure_tasks' not in available:
                raise ValueError('closure_display_parent_sources_unavailable')
            if 'closure_tasks' not in tables:
                tables += ('closure_tasks',)
        for table in tables:
            columns = [r[1] for r in conn.execute("PRAGMA main.table_info(" + table + ")")]
            if not columns:
                continue
            if table == 'closure_tasks':
                if not {'objective', 'scope_root'} <= set(columns):
                    raise ValueError('closure_display_parent_sources_unavailable')
                columns = ['objective', 'scope_root']
            args = ",".join('"' + col.replace('"', '""') + '"' for col in columns)
            self.views[table] = "agi_display_visible(" + args + ")=1"
        if {'closure_tasks', 'closure_issues'} <= self.views.keys():
            # Filter inherited sources before LIMIT/counts and before opening
            # any child evidence. Missing parents cannot certify a source.
            self.views['closure_issues'] += (
                ' AND EXISTS(SELECT 1 FROM closure_tasks parent '
                'WHERE parent.id=closure_issues.task_id)'
            )
        if 'document_signals' in self.views:
            retired_documents = scope.retired_documents(conn)
            conn.create_function('agi_document_visible', 1, lambda ref: int(ref not in retired_documents), deterministic=True)
            self.views['document_signals'] += ' AND agi_document_visible(file_path_hmac)=1'
        if personal_kind is not None:
            from types import SimpleNamespace
            policy = scope.policy_view()
            from .conversation_scope import filtered, allowed, binding, authorization, _regular, _identity,view_predicates
            grant_binding=binding()
            try:
                volatile_paths={source['path'] for source in authorization()['sources'] if source['platform']=='hermes'}
            except (OSError,ValueError,TypeError,KeyError):
                volatile_paths=set()
            registered = filtered(SimpleNamespace(conn=conn), personal_kind)
            inherited = view_predicates(conn,registered)
            for table, predicate in inherited.items():
                if table in self.views:
                    self.views[table] += ' AND (' + predicate.replace('temp.personal_', '') + ')'
            registered_cache = {}
            def admitted(session, path):
                if binding()!=grant_binding or not policy.unchanged():return 0
                try:identity=_identity(_regular(path)) if isinstance(path,str) else None
                except (OSError,ValueError):identity=None
                key = (session, path, identity)
                # A Hermes WAL can change project/parent metadata without any
                # change to the main database file. Recheck native admission.
                if path in volatile_paths:
                    try:
                        return int(isinstance(path,str) and Path(path).is_absolute() and '\x00' not in path
                            and (policy.classify({'session_id':session,'path':path})=='personal' or allowed(session,path)))
                    except (OSError,ValueError,TypeError,KeyError):
                        return 0
                if key not in registered_cache:
                    try:
                        registered_cache[key] = int(isinstance(path, str) and Path(path).is_absolute()
                            and '\x00' not in path and (policy.classify({'session_id':session,'path':path})=='personal' or allowed(session,path)))
                    except (OSError, ValueError, TypeError, KeyError):
                        registered_cache[key] = 0
                return registered_cache[key]
            conn.create_function('personal_registered_metadata', 2, admitted)
        if 'incidents' in self.views and {'quotes','messages'} <= available:
            self.views['incidents'] += (" AND NOT EXISTS(SELECT 1 FROM main.quotes q JOIN main.messages m ON m.id=q.message_id "
                "WHERE q.incident_id=incidents.id AND agi_display_visible(m.quote,m.context,m.reference)=0)")
        for table in ('decisions','notifications','samples','jobs'):
            if table in self.views and 'incidents' in self.views:
                self.views[table] += " AND (incident_id IN (SELECT id FROM incidents) OR NOT EXISTS(SELECT 1 FROM main.incidents i WHERE i.id="+table+".incident_id))"
        if 'work_items' in self.views and 'incidents' in self.views:
            self.views['work_items'] += " AND (kind!='feedback' OR reference IN (SELECT id FROM incidents))"
        for turns, contexts in (('turns', 'contexts'), ('retired_turns', 'retired_contexts')):
            if turns in self.views and contexts in self.views:
                # Use raw siblings to avoid a circular contexts <-> turns CTE.
                context_columns = [r[1] for r in conn.execute('PRAGMA main.table_info(' + contexts + ')')]
                args = ','.join('c."' + col + '"' for col in context_columns)
                self.views[contexts] += (' AND NOT EXISTS(SELECT 1 FROM main.' + turns + ' t WHERE t.context_id=' + contexts + '.id AND agi_display_visible(t.reference,t.text)=0)')
                self.views[turns] += (' AND NOT EXISTS(SELECT 1 FROM main.' + contexts + ' c WHERE c.id=' + turns + '.context_id AND (agi_display_visible(' + args + ')=0 OR EXISTS(SELECT 1 FROM main.' + turns + ' t WHERE t.context_id=c.id AND agi_display_visible(t.reference,t.text)=0)))')
        if 'insights' in self.views and 'turns' in self.views:
            turn_sets = "SELECT id FROM turns"
            if 'retired_turns' in self.views:
                turn_sets += " UNION SELECT id FROM retired_turns"
            self.views['insights'] += " AND CASE WHEN json_valid(evidence) THEN NOT EXISTS(SELECT 1 FROM json_each(evidence) j WHERE json_extract(j.value,'$.id') NOT IN ("+turn_sets+")) ELSE 0 END"
        if 'extracted_activities' in self.views and 'activity_provenance' in available:
            for source_type, table in (('document_signal', 'document_signals'), ('frame_signal', 'frame_signals')):
                if table in self.views:
                    self.views['extracted_activities'] += (" AND NOT EXISTS(SELECT 1 FROM main.activity_provenance p WHERE p.derived_type='extracted_activity' AND p.derived_id=extracted_activities.id AND p.source_type='" + source_type + "' AND NOT EXISTS(SELECT 1 FROM " + table + " source WHERE source.id=p.source_id))")
        if 'system_activity_samples' in self.views and 'frame_signals' in self.views:
            self.views['system_activity_samples'] += " AND (source_type!='frame_signal' OR source_id IN (SELECT id FROM frame_signals))"
        if 'extracted_activities' in self.views:
            for table in ('activity_confirmation_items', 'user_decisions'):
                if table in self.views:
                    self.views[table] += (" AND CASE WHEN json_valid(source_activity_ids) THEN NOT EXISTS(SELECT 1 FROM json_each(source_activity_ids) j WHERE NOT EXISTS(SELECT 1 FROM extracted_activities source WHERE source.id=j.value)) ELSE 0 END")

    def __getattr__(self, key):
        return getattr(self.raw, key)

    def execute(self, sql, args=()):
        leading = sql.lstrip()
        if self.views and leading.upper().startswith(("SELECT", "WITH")):
            for table in self.views:
                sql = re.sub(r"\b(FROM|JOIN)\s+main\." + table + r"\b",
                             lambda m: m[1] + " " + table, sql, flags=re.I)
            definitions = ",".join(table + " AS NOT MATERIALIZED (SELECT * FROM main." + table + " WHERE " + condition + ")"
                                   for table, condition in self.views.items())
            leading = sql.lstrip()
            if leading.upper().startswith("WITH RECURSIVE "):
                sql = "WITH RECURSIVE " + definitions + "," + leading[len("WITH RECURSIVE "):]
            elif leading.upper().startswith("WITH "):
                sql = "WITH " + definitions + "," + leading[len("WITH "):]
            else:
                sql = "WITH " + definitions + " " + sql
        return self.raw.execute(sql, args)


def read_view(conn, scope, tables, *, personal_kind=None):
    if isinstance(conn, ReadView):
        return conn
    return ReadView(conn, scope, tables, personal_kind) if personal_kind is not None or scope.enabled or scope.invalid else conn


ACTIVITY_TABLES = ("extracted_activities", "activity_confirmation_items", "user_decisions", "document_signals", "frame_signals")


def display_read(function):
    @wraps(function)
    def call(conn, *args, **kwargs):
        scope = for_connection(conn)
        return scope.project(function(read_view(conn, scope, ACTIVITY_TABLES), *args, **kwargs))
    return call
