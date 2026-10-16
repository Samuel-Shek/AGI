# Derived from NousResearch/hermes-agent, MIT. See PROVENANCE.json and LICENSE.hermes.
# Reviewed minimal database capsule; never import the live Hermes package.
from __future__ import annotations
import contextlib
import json
import os
import random
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

# Deliberately sealed edges: no profiles, config, hooks, artifacts or workspaces.
KNOWN_TOOLSET_NAMES = frozenset()
def normalize_reasoning_effort(value):
    if value is not None:
        raise ValueError('Model configuration is not available in this database capsule')
    return None

def _canonical_assignee(value):
    if value is not None:
        raise ValueError('Profiles are disabled in the isolated capsule')
    return None

def read_board_metadata(*args):
    return {}

def get_current_board():
    return 'ai-ceo-isolated'

def _assert_not_delegated_child_mutation():
    # Capsule used only behind controller; no live Hermes delegation imports.
    return None

def _fire_kanban_lifecycle_hook(*args, **kwargs):
    return None

def _resolve_claim_ttl_seconds(value):
    if type(value) is not int or not 1 <= value <= 3600:
        raise ValueError('Explicit lease TTL from 1 to 3600 seconds required')
    return value

def _check_file_length_invariant(conn):
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    expected = conn.execute('PRAGMA page_count').fetchone()[0] * conn.execute('PRAGMA page_size').fetchone()[0]
    if path and os.stat(path).st_size < expected:
        raise sqlite3.DatabaseError('Truncated database file')

def _merge_completion_prose_artifacts(conn, task_id, metadata, **kwargs):
    if metadata is not None:
        raise ValueError('Artifact handling is disabled in this capsule')
    return None

def _cleanup_workspace(conn, task_id):
    # This capsule has no access to a runtime workspace and never cleans files.
    return None

VALID_INITIAL_STATUSES = {'running', 'blocked'}

VALID_WORKSPACE_KINDS = {'scratch', 'worktree', 'dir'}

@dataclass
class Task:
    """In-memory view of a row from the ``tasks`` table."""
    id: str
    title: str
    body: Optional[str]
    assignee: Optional[str]
    status: str
    priority: int
    created_by: Optional[str]
    created_at: int
    started_at: Optional[int]
    completed_at: Optional[int]
    workspace_kind: str
    workspace_path: Optional[str]
    claim_lock: Optional[str]
    claim_expires: Optional[int]
    tenant: Optional[str]
    branch_name: Optional[str] = None
    project_id: Optional[str] = None
    result: Optional[str] = None
    idempotency_key: Optional[str] = None
    consecutive_failures: int = 0
    worker_pid: Optional[int] = None
    last_failure_error: Optional[str] = None
    max_runtime_seconds: Optional[int] = None
    last_heartbeat_at: Optional[int] = None
    current_run_id: Optional[int] = None
    workflow_template_id: Optional[str] = None
    current_step_key: Optional[str] = None
    skills: Optional[list] = None
    model_override: Optional[str] = None
    provider_override: Optional[str] = None
    reasoning_effort: Optional[str] = None
    max_retries: Optional[int] = None
    goal_mode: bool = False
    goal_max_turns: Optional[int] = None
    session_id: Optional[str] = None
    block_kind: Optional[str] = None
    block_recurrences: int = 0

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> 'Task':
        keys = set(row.keys())
        skills_value: Optional[list] = None
        if 'skills' in keys and row['skills']:
            try:
                parsed = json.loads(row['skills'])
                if isinstance(parsed, list):
                    skills_value = [str(s) for s in parsed if s]
            except Exception:
                skills_value = None
        return cls(id=row['id'], title=row['title'], body=row['body'], assignee=row['assignee'], status=row['status'], priority=row['priority'], created_by=row['created_by'], created_at=row['created_at'], started_at=row['started_at'], completed_at=row['completed_at'], workspace_kind=row['workspace_kind'], workspace_path=row['workspace_path'], branch_name=row['branch_name'] if 'branch_name' in keys else None, project_id=row['project_id'] if 'project_id' in keys else None, claim_lock=row['claim_lock'], claim_expires=row['claim_expires'], tenant=row['tenant'] if 'tenant' in keys else None, result=row['result'] if 'result' in keys else None, idempotency_key=row['idempotency_key'] if 'idempotency_key' in keys else None, consecutive_failures=row['consecutive_failures'] if 'consecutive_failures' in keys else row['spawn_failures'] if 'spawn_failures' in keys else 0, worker_pid=row['worker_pid'] if 'worker_pid' in keys else None, last_failure_error=row['last_failure_error'] if 'last_failure_error' in keys else row['last_spawn_error'] if 'last_spawn_error' in keys else None, max_runtime_seconds=row['max_runtime_seconds'] if 'max_runtime_seconds' in keys else None, last_heartbeat_at=row['last_heartbeat_at'] if 'last_heartbeat_at' in keys else None, current_run_id=row['current_run_id'] if 'current_run_id' in keys else None, workflow_template_id=row['workflow_template_id'] if 'workflow_template_id' in keys else None, current_step_key=row['current_step_key'] if 'current_step_key' in keys else None, skills=skills_value, model_override=row['model_override'] if 'model_override' in keys and row['model_override'] else None, provider_override=row['provider_override'] if 'provider_override' in keys and row['provider_override'] else None, reasoning_effort=row['reasoning_effort'] if 'reasoning_effort' in keys and row['reasoning_effort'] else None, max_retries=row['max_retries'] if 'max_retries' in keys else None, goal_mode=bool(row['goal_mode']) if 'goal_mode' in keys and row['goal_mode'] else False, goal_max_turns=row['goal_max_turns'] if 'goal_max_turns' in keys and row['goal_max_turns'] else None, session_id=row['session_id'] if 'session_id' in keys else None, block_kind=row['block_kind'] if 'block_kind' in keys and row['block_kind'] else None, block_recurrences=int(row['block_recurrences']) if 'block_recurrences' in keys and row['block_recurrences'] is not None else 0)

@dataclass
class Run:
    """In-memory view of a ``task_runs`` row.

    A run is one attempt to execute a task — created on claim, closed
    on complete/block/crash/timeout/spawn_failure/reclaim. Multiple runs
    per task when retries happen. Carries the claim machinery, PID,
    heartbeat, and the structured handoff summary that downstream workers
    read via ``build_worker_context``.
    """
    id: int
    task_id: str
    profile: Optional[str]
    step_key: Optional[str]
    status: str
    claim_lock: Optional[str]
    claim_expires: Optional[int]
    worker_pid: Optional[int]
    max_runtime_seconds: Optional[int]
    last_heartbeat_at: Optional[int]
    started_at: int
    ended_at: Optional[int]
    outcome: Optional[str]
    summary: Optional[str]
    metadata: Optional[dict]
    error: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> 'Run':
        try:
            meta = json.loads(row['metadata']) if row['metadata'] else None
        except Exception:
            meta = None
        return cls(id=int(row['id']), task_id=row['task_id'], profile=row['profile'], step_key=row['step_key'], status=row['status'], claim_lock=row['claim_lock'], claim_expires=row['claim_expires'], worker_pid=row['worker_pid'], max_runtime_seconds=row['max_runtime_seconds'], last_heartbeat_at=row['last_heartbeat_at'], started_at=int(row['started_at']), ended_at=int(row['ended_at']) if row['ended_at'] is not None else None, outcome=row['outcome'], summary=row['summary'], metadata=meta, error=row['error'])

SCHEMA_SQL = "\nCREATE TABLE IF NOT EXISTS tasks (\n    id                   TEXT PRIMARY KEY,\n    title                TEXT NOT NULL,\n    body                 TEXT,\n    assignee             TEXT,\n    status               TEXT NOT NULL,\n    priority             INTEGER DEFAULT 0,\n    created_by           TEXT,\n    created_at           INTEGER NOT NULL,\n    started_at           INTEGER,\n    completed_at         INTEGER,\n    workspace_kind       TEXT NOT NULL DEFAULT 'scratch',\n    workspace_path       TEXT,\n    branch_name          TEXT,\n    -- Optional link to a first-class Project (hermes_cli/projects_db). When set,\n    -- the task's worktree is anchored under the project's primary repo with a\n    -- deterministic branch name instead of a random wt/<task-id> fallback.\n    project_id           TEXT,\n    claim_lock           TEXT,\n    claim_expires        INTEGER,\n    tenant               TEXT,\n    result               TEXT,\n    idempotency_key      TEXT,\n    -- Unified consecutive-failure counter. Incremented on spawn\n    -- failure, timeout, or crash; reset only on successful completion.\n    -- The circuit breaker in _record_task_failure trips when this\n    -- exceeds DEFAULT_FAILURE_LIMIT consecutive non-successes.\n    consecutive_failures INTEGER NOT NULL DEFAULT 0,\n    worker_pid           INTEGER,\n    -- Short excerpt of the most recent failure's error text.\n    last_failure_error   TEXT,\n    max_runtime_seconds  INTEGER,\n    last_heartbeat_at    INTEGER,\n    -- Pointer into task_runs for the currently-active run (NULL if no\n    -- run is in-flight). Denormalised for cheap reads.\n    current_run_id       INTEGER,\n    -- Forward-compat for v2 workflow routing. In v1 the kernel writes\n    -- these when the task is opted into a template but otherwise ignores\n    -- them; the dispatcher doesn't consult them for routing yet.\n    workflow_template_id TEXT,\n    current_step_key     TEXT,\n    -- Force-loaded skills for the worker on this task, stored as JSON.\n    -- Passed to the worker via `--skills`. NULL or empty array = no extras.\n    skills               TEXT,\n    -- Per-task model override. When set, the dispatcher passes -m <model>\n    -- to the worker, overriding the profile's default model. NULL = use\n    -- the profile default.\n    model_override       TEXT,\n    -- Provider the model override belongs to. When set (alongside\n    -- model_override), the dispatcher passes --provider <name> so the\n    -- worker resolves the model against the right backend instead of the\n    -- profile's configured provider. NULL = profile provider.\n    provider_override    TEXT,\n    -- Per-task reasoning effort for the worker (minimal|low|medium|high|\n    -- xhigh|max|ultra, or 'none' for thinking off). When set, the dispatcher\n    -- passes --reasoning <level> so the worker runs at that depth regardless\n    -- of the profile's agent.reasoning_effort. NULL = profile setting.\n    reasoning_effort     TEXT,\n    -- Per-task override for the consecutive-failure circuit breaker.\n    -- The value is the failure count at which the breaker trips — e.g.\n    -- ``max_retries=1`` blocks on the first failure. NULL (the common\n    -- case) falls through to the dispatcher-level ``kanban.failure_limit``\n    -- config and then ``DEFAULT_FAILURE_LIMIT``.\n    max_retries          INTEGER,\n    -- When 1, the dispatched worker runs in a Ralph-style goal loop: an\n    -- auxiliary judge re-evaluates the worker's response against the\n    -- card title/body after each turn and feeds a continuation prompt\n    -- back into the SAME session until the judge agrees the work is done\n    -- or ``goal_max_turns`` is exhausted. NULL/0 = classic single-shot\n    -- worker (the default).\n    goal_mode            INTEGER NOT NULL DEFAULT 0,\n    -- Goal-loop turn budget for ``goal_mode`` workers. NULL = use the\n    -- goals-engine default.\n    goal_max_turns       INTEGER,\n    -- Originating chat/agent session id when the task was created from\n    -- inside an agent loop that propagated ``HERMES_SESSION_ID``. NULL\n    -- for tasks created from the CLI, dashboard, or any path that doesn't\n    -- set the env var. Indexed so per-session list queries stay cheap on\n    -- larger boards.\n    session_id           TEXT,\n    -- Typed block reason set by ``block_task`` (one of VALID_BLOCK_KINDS, or\n    -- NULL for legacy/un-typed blocks). Drives routing: ``dependency`` never\n    -- sits in ``blocked`` (goes to ``todo`` for parent-gating); the others go\n    -- to ``blocked`` for a human. Preserved across unblock so a re-block for\n    -- the SAME kind can be recognised as a loop.\n    block_kind           TEXT,\n    -- Unblock-loop counter. Incremented each time a task is re-blocked for the\n    -- same truly-blocked reason after having been unblocked. When it reaches\n    -- BLOCK_RECURRENCE_LIMIT the task is routed to ``triage`` instead of\n    -- ``blocked`` so a cron can't spin it forever. Reset to 0 only on a\n    -- successful completion — NOT on unblock (resetting on unblock is exactly\n    -- the amnesia that let the loop run unbounded).\n    block_recurrences    INTEGER NOT NULL DEFAULT 0\n);\n\nCREATE TABLE IF NOT EXISTS task_links (\n    parent_id  TEXT NOT NULL,\n    child_id   TEXT NOT NULL,\n    PRIMARY KEY (parent_id, child_id)\n);\n\nCREATE TABLE IF NOT EXISTS task_comments (\n    id         INTEGER PRIMARY KEY AUTOINCREMENT,\n    task_id    TEXT NOT NULL,\n    author     TEXT NOT NULL,\n    body       TEXT NOT NULL,\n    created_at INTEGER NOT NULL\n);\n\nCREATE TABLE IF NOT EXISTS task_events (\n    id         INTEGER PRIMARY KEY AUTOINCREMENT,\n    task_id    TEXT NOT NULL,\n    run_id     INTEGER,\n    kind       TEXT NOT NULL,\n    payload    TEXT,\n    created_at INTEGER NOT NULL\n);\n\n-- Historical attempt record. Each time the dispatcher claims a task, a\n-- new row is created here; claim state, PID, heartbeat, runtime cap,\n-- and structured summary all live on the run, not the task. Multiple\n-- rows per task id when the task was retried after crash/timeout/block.\n-- v2 of the kanban schema will use ``step_key`` to drive per-stage\n-- workflow routing; in v1 the column is nullable and unused (kernel\n-- ignores it).\nCREATE TABLE IF NOT EXISTS task_runs (\n    id                  INTEGER PRIMARY KEY AUTOINCREMENT,\n    task_id             TEXT NOT NULL,\n    profile             TEXT,\n    step_key            TEXT,\n    status              TEXT NOT NULL,\n    -- status: running | done | blocked | crashed | timed_out | failed | released\n    claim_lock          TEXT,\n    claim_expires       INTEGER,\n    worker_pid          INTEGER,\n    max_runtime_seconds INTEGER,\n    last_heartbeat_at   INTEGER,\n    started_at          INTEGER NOT NULL,\n    ended_at            INTEGER,\n    outcome             TEXT,\n    -- outcome: completed | blocked | crashed | timed_out | spawn_failed |\n    --          gave_up | reclaimed | (null while still running)\n    summary             TEXT,\n    metadata            TEXT,\n    error               TEXT\n);\n\n-- Files attached to a task (PDFs, images, source documents). The blob\n-- lives on disk under ``attachments_root(board)/<task_id>/<stored_name>``;\n-- this row carries metadata + the absolute ``stored_path`` so the\n-- dashboard can list/download and ``build_worker_context`` can surface\n-- the absolute path to the worker (which has full file-tool access). See\n-- #35338.\nCREATE TABLE IF NOT EXISTS task_attachments (\n    id           INTEGER PRIMARY KEY AUTOINCREMENT,\n    task_id      TEXT NOT NULL,\n    filename     TEXT NOT NULL,\n    stored_path  TEXT NOT NULL,\n    content_type TEXT,\n    size         INTEGER NOT NULL DEFAULT 0,\n    uploaded_by  TEXT,\n    created_at   INTEGER NOT NULL\n);\n\n-- Subscription from a gateway source (platform + chat + thread) to a\n-- task. The gateway's kanban-notifier watcher tails task_events and\n-- pushes ``completed`` / ``blocked`` / ``spawn_auto_blocked`` events to\n-- the original requester so human-in-the-loop workflows close the loop.\nCREATE TABLE IF NOT EXISTS kanban_notify_subs (\n    task_id       TEXT NOT NULL,\n    platform      TEXT NOT NULL,\n    chat_id       TEXT NOT NULL,\n    thread_id     TEXT NOT NULL DEFAULT '',\n    user_id       TEXT,\n    user_id_alt   TEXT,\n    chat_type     TEXT,\n    notifier_profile TEXT,\n    delivery_mode TEXT NOT NULL DEFAULT 'notify',\n    delivery_metadata TEXT,\n    created_at    INTEGER NOT NULL,\n    last_event_id INTEGER NOT NULL DEFAULT 0,\n    PRIMARY KEY (task_id, platform, chat_id, thread_id)\n);\n\nCREATE INDEX IF NOT EXISTS idx_tasks_assignee_status ON tasks(assignee, status);\nCREATE INDEX IF NOT EXISTS idx_tasks_status          ON tasks(status);\nCREATE INDEX IF NOT EXISTS idx_links_child           ON task_links(child_id);\nCREATE INDEX IF NOT EXISTS idx_links_parent          ON task_links(parent_id);\nCREATE INDEX IF NOT EXISTS idx_comments_task         ON task_comments(task_id, created_at);\nCREATE INDEX IF NOT EXISTS idx_events_task           ON task_events(task_id, created_at);\nCREATE INDEX IF NOT EXISTS idx_runs_task             ON task_runs(task_id, started_at);\nCREATE INDEX IF NOT EXISTS idx_runs_status           ON task_runs(status);\nCREATE INDEX IF NOT EXISTS idx_attachments_task      ON task_attachments(task_id, created_at);\nCREATE INDEX IF NOT EXISTS idx_notify_task           ON kanban_notify_subs(task_id);\n"

_BUSY_MAX_RETRIES = 5

_BUSY_RETRY_MIN_S = 0.02

_BUSY_RETRY_MAX_S = 0.15

def _is_busy_error(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and ('database is locked' in str(exc).lower() or 'database is busy' in str(exc).lower())

def _execute_boundary_with_retry(conn: sqlite3.Connection, sql: str) -> None:
    for attempt in range(_BUSY_MAX_RETRIES + 1):
        try:
            conn.execute(sql)
            return
        except sqlite3.OperationalError as exc:
            if not _is_busy_error(exc) or attempt == _BUSY_MAX_RETRIES:
                raise
            time.sleep(random.uniform(_BUSY_RETRY_MIN_S, _BUSY_RETRY_MAX_S))

@contextlib.contextmanager
def write_txn(conn: sqlite3.Connection, *, allow_nested: bool=False):
    _assert_not_delegated_child_mutation()
    if getattr(conn, 'in_transaction', False):
        if not allow_nested:
            raise RuntimeError('write_txn: already inside a transaction. Nested composition must opt in explicitly with write_txn(conn, allow_nested=True) (savepoint semantics; the inner RELEASE is not durable until the outer transaction commits).')
        savepoint = f'hermes_nested_{secrets.token_hex(8)}'
        conn.execute(f'SAVEPOINT {savepoint}')
        try:
            yield conn
        except Exception:
            try:
                conn.execute(f'ROLLBACK TO {savepoint}')
                conn.execute(f'RELEASE {savepoint}')
            except sqlite3.OperationalError:
                pass
            raise
        else:
            conn.execute(f'RELEASE {savepoint}')
        return
    _execute_boundary_with_retry(conn, 'BEGIN IMMEDIATE')
    try:
        yield conn
    except Exception:
        try:
            conn.execute('ROLLBACK')
        except sqlite3.OperationalError:
            pass
        raise
    else:
        try:
            _execute_boundary_with_retry(conn, 'COMMIT')
        except Exception:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.OperationalError:
                pass
            raise
        _check_file_length_invariant(conn)

def _new_task_id() -> str:
    return 't_' + secrets.token_hex(4)

def _claimer_id() -> str:
    import socket
    try:
        host = socket.gethostname() or 'unknown'
    except Exception:
        host = 'unknown'
    return f'{host}:{os.getpid()}'

def create_task(conn: sqlite3.Connection, *, title: str, body: Optional[str]=None, assignee: Optional[str]=None, created_by: Optional[str]=None, workspace_kind: str='scratch', workspace_path: Optional[str]=None, branch_name: Optional[str]=None, tenant: Optional[str]=None, priority: int=0, parents: Iterable[str]=(), triage: bool=False, idempotency_key: Optional[str]=None, max_runtime_seconds: Optional[int]=None, skills: Optional[Iterable[str]]=None, max_retries: Optional[int]=None, model_override: Optional[str]=None, provider_override: Optional[str]=None, reasoning_effort: Optional[str]=None, goal_mode: bool=False, goal_max_turns: Optional[int]=None, initial_status: str='running', session_id: Optional[str]=None, board: Optional[str]=None, project_id: Optional[str]=None, project_source_task_id: Optional[str]=None) -> str:
    model_override = (model_override or '').strip() or None
    provider_override = (provider_override or '').strip() or None
    reasoning_effort = normalize_reasoning_effort(reasoning_effort)
    if provider_override and (not model_override):
        raise ValueError('provider_override requires a model_override')
    assignee = _canonical_assignee(assignee)
    if not title or not title.strip():
        raise ValueError('title is required')
    if initial_status not in VALID_INITIAL_STATUSES:
        raise ValueError(f'initial_status must be one of {sorted(VALID_INITIAL_STATUSES)}')
    if workspace_kind not in VALID_WORKSPACE_KINDS:
        raise ValueError(f'workspace_kind must be one of {sorted(VALID_WORKSPACE_KINDS)}, got {workspace_kind!r}')
    if branch_name is not None:
        branch_name = str(branch_name).strip() or None
    if branch_name and workspace_kind != 'worktree':
        raise ValueError('branch_name is only valid for worktree workspaces')
    if project_id is None:
        try:
            _bmeta = read_board_metadata(board if board else get_current_board())
            _board_project = (_bmeta.get('project_id') or '').strip()
            if _board_project:
                project_id = _board_project
        except Exception:
            pass
    project_obj = None
    project_repo: Optional[str] = None
    if project_id is not None:
        project_id = str(project_id).strip() or None
    if project_id:
        raise RuntimeError('External Hermes imports are disabled in this capsule')
        try:
            with _pdb.connect_closing() as _pconn:
                project_obj = _pdb.get_project(_pconn, project_id)
        except Exception:
            project_obj = None
        if project_obj is None and project_source_task_id:
            source_task = get_task(conn, str(project_source_task_id))
            if source_task is not None and source_task.project_id == project_id and (source_task.workspace_kind == 'worktree') and source_task.workspace_path:
                source_path = Path(source_task.workspace_path)
                if source_path.is_absolute() and source_path.name == source_task.id and (source_path.parent.name == '.worktrees'):
                    project_slug = None
                    if source_task.branch_name:
                        prefix, separator, leaf = source_task.branch_name.partition('/')
                        if separator and (leaf == source_task.id or leaf.startswith(f'{source_task.id}-')):
                            try:
                                project_slug = _pdb.normalize_slug(prefix)
                            except ValueError:
                                project_slug = None
                    if project_slug is None:
                        try:
                            project_slug = _pdb.normalize_slug(project_id)
                        except ValueError:
                            project_slug = None
                    if project_slug:
                        project_repo = str(source_path.parent.parent)
                        project_obj = _pdb.Project(id=project_id, slug=project_slug, name=project_slug, created_at=0, primary_path=project_repo)
                        if workspace_kind == 'scratch':
                            workspace_kind = 'worktree'
        if project_obj is None:
            project_id = None
        else:
            project_id = project_obj.id
            if workspace_kind == 'scratch' and project_obj.primary_path:
                workspace_kind = 'worktree'
            if workspace_kind == 'worktree' and workspace_path is None and project_obj.primary_path:
                project_repo = str(project_obj.primary_path)
    parents = tuple((p for p in parents if p))
    skills_list: Optional[list[str]] = None
    if skills is not None:
        cleaned: list[str] = []
        seen: set[str] = set()
        toolset_typos: list[str] = []
        for s in skills:
            if not s:
                continue
            name = str(s).strip()
            if not name:
                continue
            if ',' in name:
                raise ValueError(f'skill name cannot contain comma: {name!r} (pass a list of separate names instead of a comma-joined string)')
            if name.casefold() in KNOWN_TOOLSET_NAMES:
                toolset_typos.append(name)
                continue
            if name in seen:
                continue
            seen.add(name)
            cleaned.append(name)
        if toolset_typos:
            quoted = ', '.join((repr(n) for n in toolset_typos))
            noun = 'is a toolset name' if len(toolset_typos) == 1 else 'are toolset names'
            raise ValueError(f"{quoted} {noun}, not skill name(s). Put toolsets in the assignee profile's `toolsets:` config instead of per-task skills. Skills are named skill bundles (e.g. `blogwatcher`, `github-code-review`); toolsets are runtime capabilities (e.g. `web`, `browser`, `terminal`).")
        skills_list = cleaned
    if idempotency_key:
        row = conn.execute("SELECT id FROM tasks WHERE idempotency_key = ? AND status != 'archived' ORDER BY created_at DESC LIMIT 1", (idempotency_key,)).fetchone()
        if row:
            return row['id']
    now = int(time.time())
    if workspace_path is None and project_repo is None and (workspace_kind in {'dir', 'worktree'}):
        board_slug = board if board else get_current_board()
        board_meta = read_board_metadata(board_slug)
        board_default = board_meta.get('default_workdir')
        if board_default:
            workspace_path = str(board_default)
    for attempt in range(2):
        task_id = _new_task_id()
        try:
            with write_txn(conn, allow_nested=True):
                if initial_status == 'blocked':
                    task_status = 'blocked'
                    if parents:
                        missing = _find_missing_parents(conn, parents)
                        if missing:
                            raise ValueError(f'unknown parent task(s): {', '.join(missing)}')
                elif triage:
                    task_status = 'triage'
                else:
                    task_status = 'ready'
                    if parents:
                        missing = _find_missing_parents(conn, parents)
                        if missing:
                            raise ValueError(f'unknown parent task(s): {', '.join(missing)}')
                        rows = conn.execute('SELECT status FROM tasks WHERE id IN (' + ','.join('?' * len(parents)) + ')', parents).fetchall()
                        if any((r['status'] != 'done' for r in rows)):
                            task_status = 'todo'
                if triage and parents:
                    missing = _find_missing_parents(conn, parents)
                    if missing:
                        raise ValueError(f'unknown parent task(s): {', '.join(missing)}')
                if project_obj is not None and workspace_kind == 'worktree':
                    if project_repo and (not workspace_path):
                        workspace_path = os.path.join(project_repo, '.worktrees', task_id)
                    if not branch_name:
                        try:
                            branch_name = _pdb.branch_name_for(project_obj, task_id, title=title or '')
                        except Exception:
                            branch_name = None
                conn.execute('\n                    INSERT INTO tasks (\n                        id, title, body, assignee, status, priority,\n                        created_by, created_at, workspace_kind, workspace_path,\n                        branch_name, project_id, tenant, idempotency_key,\n                        max_runtime_seconds,\n                        skills, max_retries, model_override, provider_override,\n                        reasoning_effort,\n                        goal_mode, goal_max_turns, session_id\n                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                    ', (task_id, title.strip(), body, assignee, task_status, priority, created_by, now, workspace_kind, workspace_path, branch_name, project_id, tenant, idempotency_key, int(max_runtime_seconds) if max_runtime_seconds is not None else None, json.dumps(skills_list) if skills_list is not None else None, int(max_retries) if max_retries is not None else None, model_override, provider_override, reasoning_effort, 1 if goal_mode else 0, int(goal_max_turns) if goal_max_turns is not None else None, session_id))
                for pid in parents:
                    conn.execute('INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)', (pid, task_id))
                _append_event(conn, task_id, 'created', {'assignee': assignee, 'status': task_status, 'parents': list(parents), 'tenant': tenant, 'workspace_kind': workspace_kind, 'workspace_path': workspace_path, 'branch_name': branch_name, 'project_id': project_id, 'skills': list(skills_list) if skills_list else None, 'goal_mode': bool(goal_mode) or None, 'model_override': model_override, 'provider_override': provider_override})
                _inherit_notify_subs(conn, task_id, parents, created_at=now)
            return task_id
        except sqlite3.IntegrityError:
            if attempt == 1:
                raise
            continue
    raise RuntimeError('unreachable')

def _find_missing_parents(conn: sqlite3.Connection, parents: Iterable[str]) -> list[str]:
    parents = list(parents)
    if not parents:
        return []
    placeholders = ','.join('?' * len(parents))
    rows = conn.execute(f'SELECT id FROM tasks WHERE id IN ({placeholders})', parents).fetchall()
    present = {r['id'] for r in rows}
    return [p for p in parents if p not in present]

def _inherit_notify_subs(conn: sqlite3.Connection, child_id: str, parents: Iterable[str], *, created_at: Optional[int]=None) -> None:
    parent_ids = tuple(dict.fromkeys((p for p in parents if p)))
    if not parent_ids:
        return
    row = conn.execute('SELECT COALESCE(MAX(id), 0) AS cursor FROM task_events WHERE task_id = ?', (child_id,)).fetchone()
    cursor = int(row['cursor'] if row is not None else 0)
    placeholders = ','.join('?' * len(parent_ids))
    conn.execute(f"\n        INSERT OR IGNORE INTO kanban_notify_subs\n            (task_id, platform, chat_id, thread_id, user_id, user_id_alt,\n             chat_type, notifier_profile, delivery_mode, delivery_metadata,\n             created_at, last_event_id)\n        SELECT ?, platform, chat_id, thread_id, user_id, user_id_alt,\n               COALESCE(chat_type, 'dm'), notifier_profile,\n               COALESCE(delivery_mode, 'notify'), delivery_metadata, ?, ?\n          FROM kanban_notify_subs\n         WHERE task_id IN ({placeholders})\n        ", (child_id, int(created_at if created_at is not None else time.time()), cursor, *parent_ids))

def get_task(conn: sqlite3.Connection, task_id: str) -> Optional[Task]:
    row = conn.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
    return Task.from_row(row) if row else None

def _append_event(conn: sqlite3.Connection, task_id: str, kind: str, payload: Optional[dict]=None, *, run_id: Optional[int]=None) -> None:
    now = int(time.time())
    pl = json.dumps(payload, ensure_ascii=False) if payload else None
    conn.execute('INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)', (task_id, run_id, kind, pl, now))

def _end_run(conn: sqlite3.Connection, task_id: str, *, outcome: str, summary: Optional[str]=None, error: Optional[str]=None, metadata: Optional[dict]=None, status: Optional[str]=None) -> Optional[int]:
    now = int(time.time())
    row = conn.execute('SELECT current_run_id FROM tasks WHERE id = ?', (task_id,)).fetchone()
    if not row or not row['current_run_id']:
        return None
    run_id = int(row['current_run_id'])
    conn.execute('\n        UPDATE task_runs\n           SET status        = ?,\n               outcome       = ?,\n               summary       = ?,\n               error         = ?,\n               metadata      = ?,\n               ended_at      = ?,\n               claim_lock    = NULL,\n               claim_expires = NULL,\n               worker_pid    = NULL\n         WHERE id = ?\n           AND ended_at IS NULL\n        ', (status or outcome, outcome, summary, error, json.dumps(metadata, ensure_ascii=False) if metadata else None, now, run_id))
    conn.execute('UPDATE tasks SET current_run_id = NULL WHERE id = ?', (task_id,))
    return run_id

def _current_run_id(conn: sqlite3.Connection, task_id: str) -> Optional[int]:
    row = conn.execute('SELECT current_run_id FROM tasks WHERE id = ?', (task_id,)).fetchone()
    return int(row['current_run_id']) if row and row['current_run_id'] else None

def _synthesize_ended_run(conn: sqlite3.Connection, task_id: str, *, outcome: str, summary: Optional[str]=None, error: Optional[str]=None, metadata: Optional[dict]=None) -> int:
    now = int(time.time())
    trow = conn.execute('SELECT assignee, current_step_key FROM tasks WHERE id = ?', (task_id,)).fetchone()
    profile = trow['assignee'] if trow else None
    step_key = trow['current_step_key'] if trow else None
    cur = conn.execute('\n        INSERT INTO task_runs (\n            task_id, profile, step_key,\n            status, outcome,\n            summary, error, metadata,\n            started_at, ended_at\n        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n        ', (task_id, profile, step_key, outcome, outcome, summary, error, json.dumps(metadata, ensure_ascii=False) if metadata else None, now, now))
    return int(cur.lastrowid or 0)

def _has_sticky_block(conn: sqlite3.Connection, task_id: str) -> bool:
    row = conn.execute("SELECT kind FROM task_events WHERE task_id = ? AND kind IN ('blocked', 'unblocked') ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    return bool(row) and row['kind'] == 'blocked'

def _resume_status_from_events(conn: sqlite3.Connection, task_id: str) -> str:
    row = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind IN ('blocked', 'block_loop_detected', 'dependency_wait', 'gave_up', 'unblocked', 'changes_requested', 'review_reopened', 'status', 'reclaimed', 'stale', 'timed_out', 'crashed', 'spawn_failed', 'rate_limited') ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    try:
        payload = json.loads(row['payload']) if row and row['payload'] else {}
    except (json.JSONDecodeError, TypeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    for key in ('resume_status', 'retry_status', 'source_status'):
        if payload.get(key) == 'review':
            return 'review'
    return 'ready'

def recompute_ready(conn: sqlite3.Connection, failure_limit: int=None) -> int:
    if failure_limit is None:
        failure_limit = DEFAULT_FAILURE_LIMIT
    promoted = 0
    with write_txn(conn, allow_nested=True):
        todo_rows = conn.execute("SELECT id, status, consecutive_failures, max_retries FROM tasks WHERE status IN ('todo', 'blocked')").fetchall()
        for row in todo_rows:
            task_id = row['id']
            cur_status = row['status']
            if cur_status == 'blocked' and _has_sticky_block(conn, task_id):
                continue
            parents = conn.execute('SELECT t.status FROM tasks t JOIN task_links l ON l.parent_id = t.id WHERE l.child_id = ?', (task_id,)).fetchall()
            if all((p['status'] in ('done', 'archived') for p in parents)):
                resume_status = _resume_status_from_events(conn, task_id)
                if cur_status == 'blocked':
                    failures = int(row['consecutive_failures'] or 0)
                    task_limit = row['max_retries']
                    effective_limit = int(task_limit) if task_limit is not None else int(failure_limit)
                    if failures >= effective_limit:
                        continue
                    conn.execute("UPDATE tasks SET status = ? WHERE id = ? AND status = 'blocked'", (resume_status, task_id))
                else:
                    conn.execute("UPDATE tasks SET status = ? WHERE id = ? AND status = 'todo'", (resume_status, task_id))
                _append_event(conn, task_id, 'promoted', {'status': resume_status} if resume_status != 'ready' else None)
                promoted += 1
    return promoted

def _parents_satisfied(conn: sqlite3.Connection, task_id: str) -> bool:
    return conn.execute("SELECT 1 FROM task_links l JOIN tasks p ON p.id = l.parent_id WHERE l.child_id = ? AND p.status NOT IN ('done', 'archived') LIMIT 1", (task_id,)).fetchone() is None

def claim_task(conn: sqlite3.Connection, task_id: str, *, ttl_seconds: Optional[int]=None, claimer: Optional[str]=None) -> Optional[Task]:
    now = int(time.time())
    lock = claimer or _claimer_id()
    expires = now + _resolve_claim_ttl_seconds(ttl_seconds)
    with write_txn(conn, allow_nested=True):
        undone = conn.execute("SELECT 1 FROM task_links l JOIN tasks p ON p.id = l.parent_id WHERE l.child_id = ? AND p.status NOT IN ('done', 'archived') LIMIT 1", (task_id,)).fetchone()
        if undone:
            conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ? AND status = 'ready'", (task_id,))
            _append_event(conn, task_id, 'claim_rejected', {'reason': 'parents_not_done'})
            return None
        stale = conn.execute("SELECT current_run_id FROM tasks WHERE id = ? AND status = 'ready'", (task_id,)).fetchone()
        if stale and stale['current_run_id']:
            conn.execute("\n                UPDATE task_runs\n                   SET status = 'reclaimed', outcome = 'reclaimed',\n                       summary = COALESCE(summary, 'invariant recovery on re-claim'),\n                       ended_at = ?,\n                       claim_lock = NULL, claim_expires = NULL, worker_pid = NULL\n                 WHERE id = ? AND ended_at IS NULL\n                ", (now, int(stale['current_run_id'])))
        cur = conn.execute("\n            UPDATE tasks\n               SET status        = 'running',\n                   claim_lock    = ?,\n                   claim_expires = ?,\n                   started_at    = COALESCE(started_at, ?)\n             WHERE id = ?\n               AND status = 'ready'\n               AND claim_lock IS NULL\n            ", (lock, expires, now, task_id))
        if cur.rowcount != 1:
            return None
        trow = conn.execute('SELECT assignee, max_runtime_seconds, current_step_key FROM tasks WHERE id = ?', (task_id,)).fetchone()
        run_cur = conn.execute("\n            INSERT INTO task_runs (\n                task_id, profile, step_key, status,\n                claim_lock, claim_expires, max_runtime_seconds,\n                started_at\n            ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?)\n            ", (task_id, trow['assignee'] if trow else None, trow['current_step_key'] if trow else None, lock, expires, trow['max_runtime_seconds'] if trow else None, now))
        run_id = run_cur.lastrowid
        conn.execute('UPDATE tasks SET current_run_id = ? WHERE id = ?', (run_id, task_id))
        _append_event(conn, task_id, 'claimed', {'lock': lock, 'expires': expires, 'run_id': run_id}, run_id=run_id)
        claimed = get_task(conn, task_id)
    _fire_kanban_lifecycle_hook('kanban_task_claimed', task_id, board=get_current_board(), assignee=claimed.assignee if claimed else None, run_id=run_id)
    return claimed

def _verify_created_cards(conn: sqlite3.Connection, completing_task_id: str, claimed_ids: Iterable[str]) -> tuple[list[str], list[str]]:
    claimed = [str(x).strip() for x in claimed_ids or [] if str(x).strip()]
    if not claimed:
        return ([], [])
    seen: set[str] = set()
    ordered: list[str] = []
    for cid in claimed:
        if cid not in seen:
            seen.add(cid)
            ordered.append(cid)
    row = conn.execute('SELECT assignee FROM tasks WHERE id = ?', (completing_task_id,)).fetchone()
    if row is None:
        return ([], ordered)
    completing_assignee = row['assignee']
    placeholders = ','.join(['?'] * len(ordered))
    rows = conn.execute(f'SELECT id, created_by FROM tasks WHERE id IN ({placeholders})', tuple(ordered)).fetchall()
    found = {r['id']: r['created_by'] for r in rows}
    linked_children: set[str] = set(child_ids(conn, completing_task_id))
    verified: list[str] = []
    phantom: list[str] = []
    for cid in ordered:
        created_by = found.get(cid)
        if created_by is None:
            phantom.append(cid)
            continue
        if completing_assignee and created_by == completing_assignee:
            verified.append(cid)
        elif created_by == completing_task_id:
            verified.append(cid)
        elif cid in linked_children:
            verified.append(cid)
        else:
            phantom.append(cid)
    return (verified, phantom)

_TASK_ID_PROSE_RE = re.compile('\\bt_[a-f0-9]{8,}\\b')

def _scan_prose_for_phantom_ids(conn: sqlite3.Connection, text: str) -> list[str]:
    if not text:
        return []
    matches = _TASK_ID_PROSE_RE.findall(text)
    if not matches:
        return []
    seen: set[str] = set()
    unique: list[str] = []
    for m in matches:
        if m not in seen:
            seen.add(m)
            unique.append(m)
    placeholders = ','.join(['?'] * len(unique))
    rows = conn.execute(f'SELECT id FROM tasks WHERE id IN ({placeholders})', tuple(unique)).fetchall()
    existing = {r['id'] for r in rows}
    return [m for m in unique if m not in existing]

class HallucinatedCardsError(ValueError):
    """Raised by ``complete_task`` when ``created_cards`` contains ids
    that don't exist or weren't created by the completing worker.

    The phantom list is attached as ``.phantom`` for callers that want
    structured access. Kept as ``ValueError`` subclass so existing
    tool-error handlers treat it as a recoverable user error.
    """

    def __init__(self, phantom: list[str], completing_task_id: str):
        self.phantom = list(phantom)
        self.completing_task_id = completing_task_id
        super().__init__(f'completion blocked: claimed created_cards that do not exist or were not created by this worker: {', '.join(phantom)}')

def complete_task(conn: sqlite3.Connection, task_id: str, *, result: Optional[str]=None, summary: Optional[str]=None, metadata: Optional[dict]=None, created_cards: Optional[Iterable[str]]=None, expected_run_id: Optional[int]=None, fire_lifecycle_hook: bool=True) -> bool:
    now = int(time.time())
    if not _parents_satisfied(conn, task_id):
        return False
    if created_cards:
        verified_cards, phantom_cards = _verify_created_cards(conn, task_id, created_cards)
        if phantom_cards:
            with write_txn(conn, allow_nested=True):
                _append_event(conn, task_id, 'completion_blocked_hallucination', {'phantom_cards': phantom_cards, 'verified_cards': verified_cards, 'summary_preview': (summary or result or '').strip().splitlines()[0][:200] if summary or result else None})
            raise HallucinatedCardsError(phantom_cards, task_id)
    else:
        verified_cards = []
    metadata = _merge_completion_prose_artifacts(conn, task_id, metadata, summary=summary, result=result)
    with write_txn(conn, allow_nested=True):
        if not _parents_satisfied(conn, task_id):
            return False
        prior = conn.execute('SELECT status FROM tasks WHERE id = ?', (task_id,)).fetchone()
        prior_status = prior['status'] if prior else None
        if expected_run_id is None:
            cur = conn.execute("\n                UPDATE tasks\n                   SET status       = 'done',\n                       result       = ?,\n                       completed_at = ?,\n                       claim_lock   = NULL,\n                       claim_expires= NULL,\n                       worker_pid   = NULL,\n                       block_kind   = NULL,\n                       block_recurrences = 0\n                 WHERE id = ?\n                   AND status IN ('running', 'ready', 'blocked', 'review')\n                ", (result, now, task_id))
        else:
            cur = conn.execute("\n                UPDATE tasks\n                   SET status       = 'done',\n                       result       = ?,\n                       completed_at = ?,\n                       claim_lock   = NULL,\n                       claim_expires= NULL,\n                       worker_pid   = NULL,\n                       block_kind   = NULL,\n                       block_recurrences = 0\n                 WHERE id = ?\n                   AND status IN ('running', 'ready', 'blocked', 'review')\n                   AND current_run_id = ?\n                ", (result, now, task_id, int(expected_run_id)))
        if cur.rowcount != 1:
            return False
        if isinstance(metadata, dict):
            _persist_scratch_completion_artifacts(conn, task_id, metadata)
            for stored_path in metadata.pop('_staged_artifacts', []):
                path = Path(stored_path)
                _insert_completion_attachment(conn, task_id, filename=path.name, stored_path=str(path), size=path.stat().st_size, created_at=now)
        run_id = _end_run(conn, task_id, outcome='completed', status='done', summary=summary if summary is not None else result, metadata=metadata)
        if run_id is None and (summary or metadata or result or (prior_status == 'review')):
            synth_summary = summary if summary is not None else result
            synth_metadata = metadata
            if prior_status == 'review' and (not synth_summary) and (not synth_metadata):
                synth_summary = 'Review approved without additional evidence.'
                synth_metadata = {'source_status': 'review', 'approval': 'manual'}
            run_id = _synthesize_ended_run(conn, task_id, outcome='completed', summary=synth_summary, metadata=synth_metadata)
        event_summary = summary if summary is not None else result
        if prior_status == 'review' and (not event_summary):
            event_summary = 'Review approved without additional evidence.'
        _ev_lines = (event_summary or '').strip().splitlines()
        ev_summary = _ev_lines[0][:400] if _ev_lines else ''
        completed_payload: dict = {'result_len': len(result) if result else 0, 'summary': ev_summary or None}
        if verified_cards:
            completed_payload['verified_cards'] = verified_cards
        if isinstance(metadata, dict):
            md_artifacts = metadata.get('artifacts')
            if isinstance(md_artifacts, (list, tuple)):
                cleaned_artifacts = [str(p).strip() for p in md_artifacts if isinstance(p, str) and str(p).strip()]
                if cleaned_artifacts:
                    completed_payload['artifacts'] = cleaned_artifacts
        _append_event(conn, task_id, 'completed', completed_payload, run_id=run_id)
    scan_text = ' '.join(filter(None, [summary, result]))
    if scan_text:
        phantom_refs = _scan_prose_for_phantom_ids(conn, scan_text)
        phantom_refs = [p for p in phantom_refs if p not in set(verified_cards)]
        if phantom_refs:
            with write_txn(conn, allow_nested=True):
                _append_event(conn, task_id, 'suspected_hallucinated_references', {'phantom_refs': phantom_refs, 'source': 'completion_summary'}, run_id=run_id)
    _clear_failure_counter(conn, task_id)
    recompute_ready(conn)
    _cleanup_workspace(conn, task_id)
    _done_task = get_task(conn, task_id)
    if fire_lifecycle_hook:
        _fire_kanban_lifecycle_hook('kanban_task_completed', task_id, board=get_current_board(), assignee=_done_task.assignee if _done_task else None, run_id=run_id, summary=summary if summary is not None else result)
    return True

DEFAULT_FAILURE_LIMIT = 2

def _clear_failure_counter(conn: sqlite3.Connection, task_id: str) -> None:
    with write_txn(conn, allow_nested=True):
        conn.execute('UPDATE tasks SET consecutive_failures = 0, last_failure_error = NULL WHERE id = ?', (task_id,))
