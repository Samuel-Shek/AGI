"""Content-free worker progress. Status readers must never renew this evidence."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import time

HEARTBEAT_INTERVAL_SECONDS = 30
HEARTBEAT_TTL_SECONDS = 180
WORKERS = frozenset({'daemon', 'document_worker'})


class HeartbeatWriter:
    def __init__(self, worker: str):
        if worker not in WORKERS:
            raise ValueError('unknown worker')
        self.worker = worker
        self.last_write = float('-inf')
        self.last_phase = ''

    def touch(self, conn, *, phase: str = 'running') -> bool:
        if phase not in {'running', 'stalled', 'stopped', 'waiting_for_session', 'backpressure', 'database_busy', 'recovering'}:
            raise ValueError('unknown phase')
        now = time.monotonic()
        if phase == self.last_phase and now - self.last_write < HEARTBEAT_INTERVAL_SECONDS:
            return False
        stamp = datetime.now(timezone.utc).isoformat()
        value = json.dumps({'pid': os.getpid(), 'at': stamp, 'phase': phase}, separators=(',', ':'))
        conn.execute(
            'INSERT INTO daemon_state(key,value,updated_at) VALUES (?,?,?) '
            'ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
            (self.worker + '_heartbeat', value, stamp),
        )
        self.last_write, self.last_phase = now, phase
        return True


def heartbeat_snapshot(conn, worker: str, pid, *, now=None) -> dict:
    if worker not in WORKERS:
        raise ValueError('unknown worker')
    row = conn.execute('SELECT value FROM daemon_state WHERE key=?', (worker + '_heartbeat',)).fetchone()
    result = {'status': 'missing', 'age_seconds': None, 'raw_access': False}
    try:
        data = json.loads(row[0]) if row else {}
        stamp = datetime.fromisoformat(data['at'])
        if stamp.tzinfo is None or int(data['pid']) != int(pid):
            return result
        age = ((now or datetime.now(timezone.utc)) - stamp).total_seconds()
        if age < -5:
            return result
        result['age_seconds'] = max(0, int(age))
        result['phase'] = str(data.get('phase', 'unknown'))
        result['status'] = 'fresh' if age <= HEARTBEAT_TTL_SECONDS else 'expired'
        if data.get('phase') in {'stalled', 'stopped'}:
            result['status'] = data['phase']
    except (KeyError, TypeError, ValueError, OverflowError):
        pass
    return result


def runtime_health(conn, service: dict, *, document_expected: bool) -> dict:
    daemon = heartbeat_snapshot(conn, 'daemon', service.get('pid'))
    document = heartbeat_snapshot(conn, 'document_worker', service.get('pid'))
    reason = ''
    if not service.get('loaded') or not service.get('running'):
        reason = 'service_stopped'
    elif daemon['status'] != 'fresh':
        reason = 'daemon_heartbeat_' + daemon['status']
    elif document_expected and document['status'] != 'fresh':
        reason = 'document_worker_heartbeat_' + document['status']
    return {
        'status': 'warning' if reason else 'ok', 'reason_code': reason,
        'daemon': daemon, 'document_worker': document,
        'document_tracking_expected': document_expected, 'raw_access': False,
    }
