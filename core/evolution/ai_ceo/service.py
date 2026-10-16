"""Staged recurring read-only service; no daemon installation or grant issuance.

Only an explicit, owner-only operator file can enable this path. The operator
label describes the approval source; it is not cryptographic identity proof.
No HTTP/model payload is accepted here and no production/model capability exists.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from threading import Event
from typing import Callable

from .observers import _safe_directory, _json_object, _signature
from .policy import GateDenied, ReadSession, utcnow

FIXED_SOURCE = Path.home() / 'AI/LocalData/WORKPLACE-Memory-Bridge.nosync'
MAX_GRANT_BYTES = 8192
KEYS = frozenset({'schema_version', 'id', 'status', 'enabled', 'approved_by', 'issued_at',
                  'expires_at', 'source_root', 'asset_id', 'interval_seconds', 'max_observations',
                  'enable_recurring', 'model_export', 'production_write'})
STATE_KEY = 'service:grant'


def timestamp(value) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError('invalid timestamp')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('timezone required')
    return result.astimezone(timezone.utc)


def stamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError('timezone required')
    return value.astimezone(timezone.utc).isoformat()


def denied(code):
    raise GateDenied(code, '常驻观察条件未满足；没有启动新的来源读取。')


@dataclass(frozen=True)
class ServiceGrant:
    id: str
    issued_at: datetime
    expires_at: datetime
    interval_seconds: int
    max_observations: int
    digest: str
    root: Path = FIXED_SOURCE


def load_grant(path: Path, now: datetime | None = None) -> ServiceGrant:
    """Load a strict operator-owned 0600 regular file, never follow symlinks."""
    now = now or utcnow()
    descriptor = directory = None
    try:
        path = Path(path).absolute()
        directory = _safe_directory(path.parent)
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1:
            denied('grant_file_permissions')
        if before.st_size > MAX_GRANT_BYTES:
            denied('grant_file_size')
        with os.fdopen(descriptor, 'rb') as stream:
            descriptor = None
            content = stream.read(MAX_GRANT_BYTES + 1)
            after = os.fstat(stream.fileno())
        current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        if len(content) > MAX_GRANT_BYTES or _signature(before) != _signature(after) or _signature(after) != _signature(current):
            denied('grant_file_changed')
        raw = json.loads(content, object_pairs_hook=_json_object)
        if not isinstance(raw, dict) or set(raw) != KEYS:
            denied('grant_schema')
        if type(raw['schema_version']) is not int or raw['schema_version'] != 1:
            denied('grant_schema')
        if (raw['status'] != 'approved' or raw['enabled'] is not True
                or raw['approved_by'] != 'local_operator' or raw['enable_recurring'] is not True
                or raw['model_export'] is not False or raw['production_write'] is not False):
            denied('grant_not_approved')
        if raw['asset_id'] != 'sync-center' or raw['source_root'] != str(FIXED_SOURCE):
            denied('grant_source_scope')
        if not isinstance(raw['id'], str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,64}', raw['id']):
            denied('grant_id')
        issued, expires = timestamp(raw['issued_at']), timestamp(raw['expires_at'])
        if not issued <= now < expires or not timedelta(0) < expires - issued <= timedelta(days=14):
            denied('grant_time_window')
        interval, count = raw['interval_seconds'], raw['max_observations']
        if type(interval) is not int or not 60 <= interval <= 86400:
            denied('grant_interval')
        if type(count) is not int or not 1 <= count <= 20160:
            denied('grant_count')
        canonical = json.dumps(raw, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
        return ServiceGrant(raw['id'], issued, expires, interval, count, hashlib.sha256(canonical).hexdigest())
    except GateDenied:
        raise
    except (OSError, ValueError, TypeError, RecursionError):
        denied('grant_unreadable')
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)


class RecurringReadSession(ReadSession):
    """Consumes the persistent budget even for a manual UI observation request."""
    def __init__(self, runtime):
        self.runtime = runtime
        row = runtime.row()
        super().__init__(root=runtime.grant.root, expires_at=runtime.grant.expires_at,
                         remaining=max(0, runtime.grant.max_observations - row['attempted']),
                         id=runtime.grant.id, revision=row['revision'])

    def valid(self):
        runtime = self.runtime
        row = runtime.row()
        return (runtime.grant.issued_at <= runtime.clock() < self.expires_at
                and row['attempted'] < runtime.grant.max_observations
                and row['digest'] == runtime.grant.digest and not row.get('blocked_reason'))

    def consume(self):
        runtime, controller = self.runtime, self.runtime.controller
        now, row = runtime.clock(), runtime.row()
        if controller.mode != 'observing' or not self.valid():
            denied('service_not_active')
        if row.get('last_admitted_at') and now < timestamp(row['last_admitted_at']):
            denied('service_clock_rollback')
        if row.get('next_due_at') and now < timestamp(row['next_due_at']):
            denied('service_interval')
        # Reserve BEFORE the actual source read; crash/exception consumes its
        # slot rather than repeating an uncertain admission after restart.
        row.update(attempted=row['attempted'] + 1, last_admitted_at=stamp(now),
                   next_due_at=stamp(now + timedelta(seconds=runtime.grant.interval_seconds)))
        controller.store.state_set(STATE_KEY, row)
        self.remaining = runtime.grant.max_observations - row['attempted']


class ServiceRuntime:
    def __init__(self, controller, grant: ServiceGrant, *, activate=False,
                 clock: Callable[[], datetime] = utcnow):
        self.controller, self.grant, self.clock = controller, grant, clock
        self.closed = False
        if (not isinstance(grant, ServiceGrant) or grant.root != FIXED_SOURCE
                or type(grant.interval_seconds) is not int or not 60 <= grant.interval_seconds <= 86400
                or type(grant.max_observations) is not int or not 1 <= grant.max_observations <= 20160
                or not timedelta(0) < grant.expires_at - grant.issued_at <= timedelta(days=14)):
            denied('grant_source_scope')
        with controller.lock:
            controller._sync_stop_request()
            if controller.read_session is not None:
                denied('service_mixed_grant')
            if (controller.mode == 'stopped' or controller.store.state_get('revoked:sync-center', False)
                    or controller.store.state_get('takeover:sync-center', False)):
                denied('service_asset_frozen')
            if not grant.issued_at <= clock() < grant.expires_at:
                denied('grant_time_window')
            previous = controller.store.state_get(STATE_KEY)
            if previous and (previous['id'] != grant.id or previous['digest'] != grant.digest):
                denied('service_grant_changed')
            if not previous:
                if not activate:
                    denied('service_activation_required')
                # Explicit initial CLI activation is insufficient to clear any
                # previously stopped or revoked state (checked above).
                revision = int(controller.store.state_get('grant_revision:sync-center', 0))
                if revision:
                    denied('service_requires_fresh_state')
                revision = 1
                controller.store.state_set('grant_revision:sync-center', revision)
                previous = {'id': grant.id, 'digest': grant.digest, 'attempted': 0,
                            'revision': revision, 'next_due_at': stamp(clock()),
                            'consecutive_failures': 0, 'last_outcome_attempt': 0, 'blocked_reason': None,
                            'issued_at': stamp(grant.issued_at), 'expires_at': stamp(grant.expires_at)}
                controller.store.state_set(STATE_KEY, previous)
                controller.mode = 'observing'
                controller.store.state_set('mode', 'observing')
                controller.store.append_audit('operator_service_activation', {
                    'asset_id': 'sync-center', 'grant_id': grant.id, 'expires_at': stamp(grant.expires_at),
                    'interval_seconds': grant.interval_seconds, 'max_observations': grant.max_observations,
                    'recurring': True, 'model_export': False, 'production_write': False})
            else:
                # Controller exposes the persisted disposition before its safe
                # default pause. Crash recovery can restore only observing.
                if getattr(controller, 'startup_mode', controller.mode) == 'observing':
                    controller.mode = 'observing'
                    controller.store.state_set('mode', 'observing')
                if previous['revision'] != controller.store.state_get('grant_revision:sync-center'):
                    denied('service_grant_revision_changed')
            controller.read_session = RecurringReadSession(self)
            controller.service_runtime = self
            self.status()

    def row(self):
        return self.controller.store.state_get(STATE_KEY)

    def _publish(self, state: str):
        row = self.row()
        status = {'installed': False, 'enabled': state == 'active', 'runtime_started': not self.closed,
                  'state': state, 'grant_id': self.grant.id, 'expires_at': stamp(self.grant.expires_at),
                  'interval_seconds': self.grant.interval_seconds,
                  'remaining_observations': max(0, self.grant.max_observations - row['attempted']),
                  'next_due_at': row.get('next_due_at'), 'blocked_reason': row.get('blocked_reason'),
                  'consecutive_failures': row.get('consecutive_failures', 0),
                  'model_export': False, 'production_write': False}
        if self.controller.store.state_get('service:status') != status:
            self.controller.store.state_set('service:status', status)
        return status

    def record_outcome(self, success: bool, code: str | None = None):
        """Controller calls this for UI reads too; duplicate callbacks are safe."""
        with self.controller.lock:
            row = self.row()
            if row.get('last_outcome_attempt', 0) >= row['attempted']:
                return
            row['last_outcome_attempt'] = row['attempted']
            row['consecutive_failures'] = 0 if success else row.get('consecutive_failures', 0) + 1
            if row['consecutive_failures'] >= 2:
                row['blocked_reason'] = 'consecutive_observation_failures'
                self.controller.store.append_audit('service_failure_limit', {
                    'consecutive_failures': row['consecutive_failures'], 'automatic_retry': False})
            self.controller.store.state_set(STATE_KEY, row)

    def status(self):
        """Refresh capability status only; never admit or perform a source read."""
        with self.controller.lock:
            self.controller._sync_stop_request()
            row, now = self.row(), self.clock()
            if self.closed:
                return self._publish('not_running')
            if self.controller.mode == 'stopped':
                return self._publish('stopped')
            if (self.controller.store.state_get('revoked:sync-center', False)
                    or self.controller.store.state_get('takeover:sync-center', False)):
                return self._publish('frozen')
            if now >= self.grant.expires_at or now < self.grant.issued_at:
                return self._publish('expired')
            if row.get('blocked_reason'):
                return self._publish('blocked')
            if row['attempted'] >= self.grant.max_observations:
                return self._publish('exhausted')
            if self.controller.mode != 'observing':
                return self._publish('paused')
            if row.get('last_admitted_at') and now < timestamp(row['last_admitted_at']):
                return self._publish('clock_rollback')
            previous = self.controller.store.state_get('service:status', {})
            if previous.get('state') == 'blocked' and now < timestamp(row['next_due_at']):
                return self._publish('blocked')
            return self._publish('active')

    def tick(self):
        with self.controller.lock:
            self.controller._sync_stop_request()
            row, now = self.row(), self.clock()
            frozen = (self.controller.store.state_get('revoked:sync-center', False)
                      or self.controller.store.state_get('takeover:sync-center', False))
            if self.closed or frozen or self.controller.mode == 'stopped':
                return self._publish('stopped' if self.controller.mode == 'stopped' else 'frozen')
            if now >= self.grant.expires_at or now < self.grant.issued_at:
                return self._publish('expired')
            if row.get('blocked_reason'):
                return self._publish('blocked')
            if row['attempted'] >= self.grant.max_observations:
                return self._publish('exhausted')
            if self.controller.mode != 'observing':
                return self._publish('paused')
            if row.get('last_admitted_at') and now < timestamp(row['last_admitted_at']):
                return self._publish('clock_rollback')
            if now < timestamp(row['next_due_at']):
                return self._publish('active')
            try:
                self.controller.observe()
            except GateDenied as error:
                self.record_outcome(False, error.code)
                self.controller.store.append_audit('service_observation_denied', {'code': error.code})
                return self._publish('blocked')
            self.record_outcome(True)
            return self._publish('exhausted' if self.row()['attempted'] >= self.grant.max_observations else 'active')

    def close(self):
        with self.controller.lock:
            self.closed = True
            self._publish('not_running')
            self.controller.read_session = None


def run_service(controller, grant, stop_event: Event, *, activate=False, clock=utcnow):
    runtime = ServiceRuntime(controller, grant, activate=activate, clock=clock)
    try:
        while not stop_event.is_set():
            state = runtime.tick()
            if state['state'] in {'stopped', 'frozen', 'expired', 'exhausted', 'clock_rollback'} or (state['state'] == 'blocked' and state.get('blocked_reason')):
                break
            stop_event.wait(min(60, grant.interval_seconds))
    finally:
        runtime.close()
