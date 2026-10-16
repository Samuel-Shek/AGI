import json
import os
import sqlite3
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

from activity_mvp.daemon import get_quick_status, _quick_launch_agent_status, _wait_for_adaptive_capture_deadline
from activity_mvp.adaptive_capture import initial_capture_state
from pathlib import Path
from activity_mvp.runtime_liveness import HeartbeatWriter, heartbeat_snapshot, runtime_health
from activity_mvp.storage import init_schema
from activity_mvp import lifecycle


class RuntimeRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        init_schema(self.conn)
        from activity_mvp.capture_lease import configure_capture_lease
        configure_capture_lease(self.conn, enabled=True)
        self.addCleanup(self.conn.close)
        self.service = {'loaded': True, 'running': True, 'pid': str(os.getpid())}
        self.set_state('daemon_status', 'ready')
        self.set_state('session_state', 'unlocked')
        self.set_state('last_success_at', datetime.now(timezone.utc).isoformat())
        HeartbeatWriter('daemon').touch(self.conn)
        HeartbeatWriter('document_worker').touch(self.conn)
        self.conn.commit()

    def set_state(self, key, value):
        self.conn.execute('INSERT OR REPLACE INTO daemon_state VALUES(?,?,?)',
                          (key, value, datetime.now(timezone.utc).isoformat()))

    def quick(self, service=None):
        with patch('activity_mvp.daemon._quick_launch_agent_status', return_value=service or self.service):
            return get_quick_status(self.conn)

    def test_dead_service_cannot_reuse_ready_or_capture_countdown(self):
        payload = self.quick({'loaded': False, 'running': False, 'pid': ''})
        self.assertEqual(payload['status'], 'stopped')
        self.assertEqual(payload['reason_code'], 'service_stopped')
        self.assertFalse(payload['launch_agent_running'])
        self.assertIsNone(payload['next_capture_in_seconds'])

    def test_missing_stale_wrong_pid_and_stalled_heartbeats_fail_closed(self):
        for worker in ('daemon', 'document_worker'):
            for data in ({}, {'at': (datetime.now(timezone.utc)-timedelta(seconds=181)).isoformat(), 'pid': os.getpid()},
                         {'at': datetime.now(timezone.utc).isoformat(), 'pid': os.getpid()+1},
                         {'at': datetime.now(timezone.utc).isoformat(), 'pid': os.getpid(), 'phase': 'stalled'}):
                with self.subTest(worker=worker, data=data):
                    for name in ('daemon', 'document_worker'):
                        HeartbeatWriter(name).touch(self.conn)
                    self.set_state(worker+'_heartbeat', json.dumps(data))
                    health = runtime_health(self.conn, self.service, document_expected=True)
                    self.assertEqual(health['status'], 'warning')
                    self.assertTrue(health['reason_code'].startswith(worker+'_heartbeat_'))

    def test_query_only_status_cannot_refresh_old_evidence(self):
        self.conn.commit()
        before = self.conn.total_changes
        self.conn.execute('PRAGMA query_only=ON')
        self.assertEqual(self.quick()['status'], 'ready')
        self.assertEqual(self.conn.total_changes, before)
        self.assertFalse(self.quick()['raw_access'])

    def test_closed_screen_does_not_claim_file_worker_is_running(self):
        self.conn.execute("INSERT OR REPLACE INTO settings VALUES('screen_capture_enabled','false','')")
        result = self.quick()
        self.assertEqual(result['status'], 'paused')
        self.assertNotIn('文件追踪运行中', result['pause_label'])
        self.assertFalse(result['capture_allowed_by_session'])

    def test_lock_and_manual_pause_do_not_count_old_capture_as_stalled(self):
        self.set_state('last_success_at', '2020-01-01T00:00:00+00:00')
        self.set_state('session_state', 'locked')
        self.assertEqual(self.quick()['status'], 'locked')
        self.assertFalse(self.quick()['capture_freshness']['is_stale'])
        self.set_state('session_state', 'unlocked')
        self.set_state('pause_mode', 'manual')
        self.assertEqual(self.quick()['status'], 'paused')
        self.assertFalse(self.quick()['capture_freshness']['is_stale'])

    def test_probe_timeout_or_denial_never_claims_service_stopped(self):
        with patch('activity_mvp.daemon.subprocess.run', side_effect=subprocess.TimeoutExpired('launchctl', .5)) as run:
            self.assertIsNone(_quick_launch_agent_status())
            self.assertEqual(run.call_args.kwargs['timeout'], .5)
        with patch('activity_mvp.daemon.subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', 'permission denied')):
            self.assertIsNone(_quick_launch_agent_status())
        with patch('activity_mvp.daemon._quick_launch_agent_status', return_value=None):
            before = self.conn.total_changes
            payload = get_quick_status(self.conn)
            self.assertEqual(payload['status'], 'unavailable')
            self.assertEqual(payload['reason_code'], 'service_status_unavailable')
            self.assertEqual(payload['observed_at'], payload['checked_at'])
            self.assertEqual(datetime.fromisoformat(payload['observed_at']).utcoffset(), timedelta(0))
            self.assertIsNone(payload['last_successful_read_at'])
            self.assertIs(payload['actions_enabled'], False)
            self.assertIs(payload['capture_allowed_by_session'], False)
            self.assertIs(payload['raw_access'], False)
            self.assertEqual(self.conn.total_changes, before)
            self.assertNotIn('launch_agent_running', payload)
            self.assertNotIn('runtime_health', payload)

    def test_heartbeat_writes_are_throttled_and_phase_change_is_immediate(self):
        writer = HeartbeatWriter('daemon')
        self.assertTrue(writer.touch(self.conn))
        self.assertFalse(writer.touch(self.conn))
        self.assertTrue(writer.touch(self.conn, phase='stalled'))
        self.assertEqual(heartbeat_snapshot(self.conn, 'daemon', os.getpid())['status'], 'stalled')

    def test_long_locked_wait_keeps_real_progress_fresh_without_poll_writes(self):
        self._assert_long_wait_progress('locked')

    def test_long_unknown_wait_keeps_worker_progress_without_allowing_capture(self):
        self._assert_long_wait_progress('unknown')

    def _assert_long_wait_progress(self, blocked_kind):
        seconds = [0.0]
        baseline = datetime.now(timezone.utc)
        ages = []
        class ClockDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return baseline + timedelta(seconds=seconds[0])
        def sleep(duration):
            seconds[0] += duration
        def session():
            ages.append(heartbeat_snapshot(self.conn, 'daemon', os.getpid())['age_seconds'])
            return {'state': blocked_kind if seconds[0] <= 300 else 'unlocked',
                    'locked': seconds[0] <= 300}
        proxy = MagicMock(wraps=self.conn)
        proxy.close = MagicMock()
        proxy.in_transaction = False
        with patch('activity_mvp.runtime_liveness.datetime', ClockDateTime), \
             patch('activity_mvp.runtime_liveness.time.monotonic', side_effect=lambda: seconds[0]), \
             patch('activity_mvp.daemon.ensure_ready', return_value=proxy) as connect, \
             patch('activity_mvp.daemon._safe_system_idle_probe', return_value={'available': True, 'idle_seconds': 0}), \
             patch('activity_mvp.daemon._persist_adaptive_capture_state'), \
             patch('activity_mvp.daemon._record_adaptive_wait_session_transition'):
            writer = HeartbeatWriter('daemon')
            writer.touch(self.conn)
            before = self.conn.total_changes
            state, kind, reason = _wait_for_adaptive_capture_deadline(
                Path('unused.sqlite'), Path('unused-root'), initial_capture_state(),
                wait_seconds=0, previous_session_state=blocked_kind, previous_idle_seconds=0,
                base_interval_seconds=15, document_reconcile_enabled=False,
                session_probe=session, monotonic_clock=lambda: seconds[0],
                wall_clock=lambda: seconds[0], sleeper=sleep, heartbeat=writer)
            self.assertEqual((kind, reason), ('unlocked', 'session_resumed'))
            self.assertEqual(heartbeat_snapshot(self.conn, 'daemon', os.getpid())['status'], 'fresh')
            self.assertLessEqual(max(ages), 30)
            self.assertEqual(self.conn.total_changes-before, 10)
            self.assertEqual(connect.call_count, 10)


class LifecycleScaleTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.execute('CREATE TABLE observations(id TEXT PRIMARY KEY, occurred_at TEXT, deletion_group_id TEXT)')
        self.ids = [str(i) for i in range(33000)]
        self.conn.executemany('INSERT INTO observations VALUES(?,?,?)',
            ((i, '2026-01-01', 'g'+i) for i in self.ids))

    def test_count_and_delete_above_native_parameter_limit_with_duplicate_ids(self):
        ids = self.ids + self.ids[:600]
        self.assertEqual(lifecycle._count_for_ids(self.conn, 'observations', 'id', ids), 33000)
        self.conn.execute('PRAGMA query_only=ON')
        rows = lifecycle._time_or_group_ids(self.conn, 'observations', 'occurred_at',
            '2026-01-01', '2026-01-02', ['g'+i for i in self.ids])
        self.assertEqual(set(rows), set(self.ids))
        self.conn.execute('PRAGMA query_only=OFF')
        lifecycle._delete_ids(self.conn, 'observations', 'id', ids)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 0)

    def test_provenance_union_does_not_double_count_overlapping_columns(self):
        self.conn.execute('CREATE TABLE activity_provenance(id TEXT PRIMARY KEY,deletion_group_id TEXT,derived_id TEXT,source_id TEXT)')
        self.conn.executemany('INSERT INTO activity_provenance VALUES(?,?,?,?)',
            ((i, 'g'+i, i, i) for i in self.ids))
        self.conn.execute('PRAGMA query_only=ON')
        rows = lifecycle._provenance_ids(self.conn, deletion_group_ids=['g'+i for i in self.ids],
                                         derived_ids=self.ids, source_ids=self.ids)
        self.assertEqual(len(rows), 33000)
        self.assertEqual(set(rows), set(self.ids))

    def test_quality_feedback_union_and_shared_evidence_stay_exact(self):
        self.conn.execute('CREATE TABLE quality_feedback(id TEXT PRIMARY KEY,deletion_group_id TEXT,target_id TEXT,target_started_at TEXT,target_ended_at TEXT,created_at TEXT)')
        self.conn.executemany('INSERT INTO quality_feedback VALUES(?,?,?,NULL,NULL,?)',
            ((i, 'g'+i, i, '2026-01-01') for i in self.ids))
        groups = ['g'+i for i in self.ids]
        count = lifecycle._count_quality_feedback(self.conn, '2026-01-01', '2026-01-02', groups, self.ids)
        self.assertEqual(count, 33000)
        self.assertEqual(lifecycle._delete_quality_feedback(self.conn, '2026-01-01', '2026-01-02',
            deletion_group_ids=groups, target_ids=self.ids), 33000)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM quality_feedback').fetchone()[0], 0)
        self.conn.execute('CREATE TABLE evidence_refs(evidence_id TEXT,item_key TEXT,released_at TEXT)')
        self.conn.executemany('INSERT INTO evidence_refs VALUES(?,?,NULL)', (('shared', i) for i in self.ids))
        rows = lifecycle._values_for_ids(self.conn, 'evidence_refs', 'evidence_id', 'item_key', self.ids, where='released_at IS NULL')
        self.assertEqual(set(rows), {'shared'})
