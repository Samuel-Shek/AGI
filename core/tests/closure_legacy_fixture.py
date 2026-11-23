"""Construct persisted v1 rows for migration regressions, never new v2 tasks.

Production callers cannot opt out of the source-bound policy. Historical tests
start with the exact old row shape instead of using the new registration API as
a legacy-task factory. All acceptance, permission and native assertions remain.
"""
import time
from pathlib import Path
from activity_mvp import task_closure as c


def legacy_begin(store, payload, *, real_begin=None):
    if payload['platform'] == 'codex' and payload.get('closure_mode') != 'light' and 'requirements' not in payload:
        root = str(Path(payload['scope_root']).resolve())
        identifier = c.digest([payload['platform'], payload['session'], payload['turn']])[:32]
        store.conn.execute('INSERT OR IGNORE INTO closure_tasks VALUES(?,?,?,?,?,?,?,?,?,?)',
            (identifier, payload['platform'], payload['session'], payload['turn'], payload['objective'], root,
             int(payload.get('complex') is True), c.digest([payload['objective'], root, payload.get('constraints', [])]), time.time(), time.time()))
    return real_begin(store, payload) if real_begin else store.begin(payload)
