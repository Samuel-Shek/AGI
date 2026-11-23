"""Cooperative deadlines for local reads; never terminate an action or process."""
from contextlib import contextmanager
from contextvars import ContextVar
import math
import re
import fcntl
import sqlite3
import time

LOCAL_SECONDS = 60.0
SCAN_SECONDS = 20.0
SOURCE_SECONDS = 5.0
_current = ContextVar('agi_local_check_budget', default=None)
_handlers = ContextVar('agi_local_read_handlers', default=None)
PHASES = {
    'feedback_scan': '读取改进线索', 'reflection_scan': '读取反思线索',
    'reflection_ready': '核对可分析材料', 'feedback_ready': '核对待处理线索', 'reflection_prepare': '准备反思材料',
    'feedback_prepare': '准备改进材料', 'submission_check': '复核待提交材料',
    'autonomy': '核对自主任务', 'task_maintenance': '核对任务记录',
    'sync_center': '核对同步状态', 'memory': '核对记忆事项',
    'proactive': '检查改进线索', 'analysis': '分析材料', 'model': '等待模型结果',
    'review': '整理检查结果', 'repair': '处理修复事项', 'finish': '完成本轮巡检',
}


class LocalCheckTimeout(Exception):
    def __init__(self, phase):
        self.phase = phase
        super().__init__('local_check_timeout')


class Budget:
    def __init__(self, phase, seconds):
        self.phase = phase
        self.deadline = time.monotonic() + seconds

    def expired(self):
        return time.monotonic() >= self.deadline

    def check(self):
        if self.expired():
            raise LocalCheckTimeout(self.phase)


def checkpoint():
    budget = _current.get()
    if budget is not None:
        budget.check()


class ScanBatch:
    """Normal backfill work quota; never catches or extends a read deadline.

    A slow setup must still get one complete record (or EOF) before yielding.
    Actual reads keep the enclosing phase's independent hard deadline.
    """
    def __init__(self, seconds=SCAN_SECONDS, *, parent=None):
        self.parent = parent
        self.deadline = time.monotonic() + seconds
        if parent is not None:
            self.deadline = min(self.deadline, parent.deadline)
        self.completed = 0

    def complete(self):
        self.completed += 1
        if self.parent is not None:
            self.parent.complete()

    def yield_now(self):
        checkpoint()
        return self.completed > 0 and time.monotonic() >= self.deadline


def scan_order(rows, next_id):
    """Rotate only the caller's current admitted inventory, never the marker."""
    rows = list(rows)
    for index, row in enumerate(rows):
        if row['id'] == next_id:
            return rows[index:] + rows[:index]
    return rows


def checked_paths(paths):
    for path in paths:
        checkpoint()
        yield path


def acquire_control_lock(descriptor):
    if _current.get() is None:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        return
    while True:
        checkpoint()
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            time.sleep(.02)


@contextmanager
def phase(store, name, seconds=None):
    if name not in PHASES:
        raise ValueError('local_phase_invalid')
    started = time.time()
    record = {'phase': name, 'status': 'running', 'started_at': started,
              'checked_at': started, 'limit_seconds': seconds}
    store.set('local_check_progress', record)
    token = _current.set(Budget(name, seconds) if seconds is not None else None)
    outcome = 'completed'
    try:
        yield
        checkpoint()
    except LocalCheckTimeout as error:
        outcome = 'deferred'
        record['phase'] = error.phase
        raise
    except BaseException:
        outcome = 'failed'
        raise
    finally:
        _current.reset(token)
        store.set('local_check_progress', {**record, 'status': outcome, 'checked_at': time.time()})


@contextmanager
def _sql(raw):
    budget = _current.get()
    if budget is None:
        yield
        return
    budget.check()
    # SQLite has one handler per connection. A read inside a SQL callback must
    # restore the outer handler, not disable the remaining outer query's limit.
    handlers = _handlers.get() or {}
    previous = handlers.get(raw, ())
    active = (*previous, budget)
    token = _handlers.set({**handlers, raw: active})
    raw.set_progress_handler(lambda: int(any(item.expired() for item in active)), 1000)
    try:
        yield
        budget.check()
    except sqlite3.Error as error:
        if budget.expired():
            raise LocalCheckTimeout(budget.phase) from error
        raise
    finally:
        _handlers.reset(token)
        if previous:
            raw.set_progress_handler(lambda: int(any(item.expired() for item in previous)), 1000)
        else:
            # Remove interruption before rollback or completed-cursor writes.
            raw.set_progress_handler(None, 0)


_SQL_TOKENS = re.compile(r"--[^\n]*(?:\n|$)|/\*[\s\S]*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^\]]*\]|[A-Za-z_][A-Za-z_0-9]*|[()]", re.ASCII)


def _read_statement(sql):
    """Find the top-level verb, ignoring CTE bodies, quoted names and data."""
    depth = 0
    first = None
    for match in _SQL_TOKENS.finditer(sql):
        word = match.group()
        if word.startswith(('--', '/*', "'", '"', '`', '[')):
            continue
        if word == '(':
            depth += 1
        elif word == ')':
            depth -= 1
        elif depth == 0:
            word = word.upper()
            if first is None:
                first = word
                if first != 'WITH':
                    return first == 'SELECT'
            elif word in {'SELECT', 'INSERT', 'UPDATE', 'DELETE', 'REPLACE'}:
                return word == 'SELECT'
    return False


class _Cursor:
    def __init__(self, cursor, raw):
        self.cursor, self.raw = cursor, raw

    def __getattr__(self, name):
        return getattr(self.cursor, name)

    def __iter__(self):
        return self

    def __next__(self):
        with _sql(self.raw):
            return next(self.cursor)

    def fetchone(self):
        with _sql(self.raw):
            return self.cursor.fetchone()

    def fetchall(self):
        with _sql(self.raw):
            return self.cursor.fetchall()

    def fetchmany(self, *args):
        with _sql(self.raw):
            return self.cursor.fetchmany(*args)


class Connection:
    """Apply an active phase deadline to reads, including lazy cursor iteration."""
    def __init__(self, raw):
        self.raw = raw

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def execute(self, sql, args=()):
        if _current.get() is None or not _read_statement(sql):
            return self.raw.execute(sql, args)
        with _sql(self.raw):
            cursor = self.raw.execute(sql, args)
        return _Cursor(cursor, self.raw)


def snapshot(store):
    """Project a fixed vocabulary; never forward arbitrary stored strings."""
    value = store.get('local_check_progress', {})
    if not isinstance(value, dict) or value.get('phase') not in PHASES:
        return {}
    status = value.get('status')
    if status not in {'running', 'completed', 'deferred', 'failed'}:
        return {}
    result = {'phase': value['phase'], 'label': PHASES[value['phase']], 'status': status}
    for key in ('started_at', 'checked_at', 'limit_seconds'):
        number = value.get(key)
        if type(number) in (int, float) and math.isfinite(number) and number >= 0:
            result[key] = number
    return result
