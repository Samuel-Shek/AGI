from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_ceo.controller import Controller
from ai_ceo.policy import GateDenied
from ai_ceo.service import FIXED_SOURCE, ServiceRuntime, load_grant


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.now = datetime(2026, 9, 8, 4, 0, tzinfo=timezone.utc)
        self.path = self.root / 'grant.json'
        self.raw = {'schema_version':1, 'id':'synthetic-test', 'status':'approved', 'enabled':True,
                    'approved_by':'local_operator', 'issued_at':self.now.isoformat(),
                    'expires_at':(self.now + timedelta(hours=2)).isoformat(),
                    'source_root':str(FIXED_SOURCE), 'asset_id':'sync-center',
                    'interval_seconds':60, 'max_observations':3, 'enable_recurring':True,
                    'model_export':False, 'production_write':False}
        self.write()
        self.controller = None
        self.observation = {'asset_id':'sync-center', 'status':'unknown', 'evidence_quality':'insufficient',
                            'reason_code':'sources_unavailable', 'observed_at':self.now.isoformat(),
                            'issue_key':'synthetic-test', 'evidence':[], 'metrics':{}, 'decision':'observe'}

    def tearDown(self):
        if self.controller:
            self.controller.close()
        self.temp.cleanup()

    def write(self):
        self.path.write_text(json.dumps(self.raw))
        self.path.chmod(0o600)

    def start(self, activate=True):
        self.controller = Controller(self.root / 'state', None)
        grant = load_grant(self.path, self.now)
        return ServiceRuntime(self.controller, grant, activate=activate, clock=lambda:self.now)

    def test_draft_never_starts(self):
        self.raw.update(enabled=False, enable_recurring=False, status='draft')
        self.write()
        with self.assertRaises(GateDenied):
            load_grant(self.path, self.now)

    def test_permissions_symlink_and_extra_fields_rejected(self):
        self.path.chmod(0o644)
        with self.assertRaises(GateDenied): load_grant(self.path, self.now)
        self.path.chmod(0o600)
        link = self.root / 'link.json'
        link.symlink_to(self.path)
        with self.assertRaises(GateDenied): load_grant(link, self.now)
        self.raw['command'] = 'arbitrary execution'
        self.write()
        with self.assertRaises(GateDenied): load_grant(self.path, self.now)

    def test_strict_bounds_and_boolean_types(self):
        for key, value in [('interval_seconds',59),('interval_seconds',True),('max_observations',0),
                           ('max_observations',False),('model_export',True),('production_write',True),
                           ('source_root','/private'),('approved_by','model'),
                           ('expires_at',(self.now+timedelta(days=15)).isoformat())]:
            with self.subTest(key=key,value=value):
                old = self.raw[key]
                self.raw[key] = value
                self.write()
                with self.assertRaises(GateDenied): load_grant(self.path, self.now)
                self.raw[key] = old

    def test_initial_activation_required(self):
        with self.assertRaises(GateDenied): self.start(activate=False)
        self.assertEqual(self.controller.store.state_get('observation_samples',0),0)

    def test_interval_wake_coalescing_and_budget(self):
        runtime = self.start()
        with patch('ai_ceo.observers.collect_sync', return_value=self.observation) as collect:
            runtime.tick()
            runtime.tick()
            self.assertEqual(collect.call_count,1)
            self.now += timedelta(minutes=20)
            runtime.tick()
            self.assertEqual(collect.call_count,2)  # no missed-interval replay burst
            runtime.tick()
            self.assertEqual(collect.call_count,2)
            self.now += timedelta(seconds=60)
            self.assertEqual(runtime.tick()['state'],'exhausted')
            runtime.tick()
            self.assertEqual(collect.call_count,3)
        self.assertEqual(runtime.row()['attempted'],3)

    def test_manual_observation_cannot_bypass_service_interval(self):
        runtime = self.start()
        with patch('ai_ceo.observers.collect_sync', return_value=self.observation) as collect:
            self.controller.observe()
            with self.assertRaises(GateDenied): self.controller.observe()
            self.assertEqual(collect.call_count,1)
        self.assertEqual(runtime.row()['attempted'],1)

    def test_failure_still_consumes_slot(self):
        runtime = self.start()
        with patch('ai_ceo.observers.collect_sync', side_effect=ValueError('synthetic failure')) as collect:
            self.assertEqual(runtime.tick()['state'],'blocked')
            runtime.tick()
            self.assertEqual(collect.call_count,1)
        self.assertEqual(runtime.row()['attempted'],1)

    def test_pause_and_stop_survive_restart_with_same_grant(self):
        runtime = self.start()
        self.controller.control('pause')
        runtime.close()
        self.controller.close()
        self.controller = None
        runtime = self.start(activate=True)
        self.assertEqual(runtime.tick()['state'],'paused')
        self.controller.control('stop')
        runtime.close()
        self.controller.close()
        self.controller = None
        with self.assertRaises(GateDenied): self.start(activate=True)

    def test_revocation_never_cleared_by_restart(self):
        runtime = self.start()
        self.controller.control('revoke','sync-center')
        runtime.close()
        self.controller.close()
        self.controller = None
        with self.assertRaises(GateDenied): self.start(activate=True)
        self.assertTrue(self.controller.store.state_get('revoked:sync-center'))

    def test_expiry_and_clock_rollback_stop_admission(self):
        runtime = self.start()
        with patch('ai_ceo.observers.collect_sync', return_value=self.observation) as collect:
            runtime.tick()
            self.now += timedelta(hours=3)
            self.assertEqual(runtime.tick()['state'],'expired')
            self.assertEqual(collect.call_count,1)

    def test_grant_cannot_be_edited_and_reused(self):
        runtime = self.start()
        runtime.close()
        self.controller.close()
        self.controller = None
        self.raw['max_observations'] = 4
        self.write()
        with self.assertRaises(GateDenied): self.start(activate=True)

    def test_two_failures_persistently_stop_retries(self):
        runtime = self.start()
        with patch('ai_ceo.observers.collect_sync', side_effect=ValueError('synthetic failure')) as collect:
            runtime.tick()
            self.now += timedelta(minutes=1)
            self.assertEqual(runtime.tick()['blocked_reason'], 'consecutive_observation_failures')
            self.now += timedelta(minutes=10)
            runtime.tick()
            self.assertEqual(collect.call_count, 2)
            runtime.close()
            self.controller.close()
            self.controller = None
            runtime = self.start(activate=True)
            self.assertEqual(runtime.tick()['state'], 'blocked')
            self.assertEqual(collect.call_count, 2)
            with self.assertRaises(GateDenied): self.controller.control('resume')

    def test_success_resets_failure_streak(self):
        runtime = self.start()
        with patch('ai_ceo.observers.collect_sync', side_effect=[ValueError('synthetic'),self.observation,ValueError('synthetic')]):
            runtime.tick()
            self.now += timedelta(minutes=1)
            runtime.tick()
            self.assertEqual(runtime.row()['consecutive_failures'],0)
            self.now += timedelta(minutes=1)
            runtime.tick()
            self.assertEqual(runtime.row()['consecutive_failures'],1)
            self.assertIsNone(runtime.row()['blocked_reason'])

    def test_active_restart_preserves_budget_and_interval(self):
        runtime = self.start()
        with patch('ai_ceo.observers.collect_sync', return_value=self.observation) as collect:
            runtime.tick()
            runtime.close()
            self.controller.close()
            self.controller = None
            runtime = self.start(activate=False)
            self.assertEqual(runtime.row()['attempted'], 1)
            self.assertEqual(runtime.tick()['state'], 'active')
            self.assertEqual(collect.call_count, 1)
            self.now += timedelta(minutes=1)
            runtime.tick()
            self.assertEqual(collect.call_count, 2)

    def test_status_does_not_read_and_reflects_pause_stop(self):
        runtime = self.start()
        with patch('ai_ceo.observers.collect_sync', return_value=self.observation) as collect:
            self.assertEqual(runtime.status()['state'], 'active')
            self.controller.control('pause')
            self.assertEqual(runtime.status()['state'], 'paused')
            self.controller.control('stop')
            self.assertEqual(runtime.status()['state'], 'stopped')
            collect.assert_not_called()

    def test_backward_clock_does_not_admit(self):
        runtime = self.start()
        with patch('ai_ceo.observers.collect_sync', return_value=self.observation) as collect:
            self.now += timedelta(minutes=10)
            runtime.tick()
            self.now -= timedelta(minutes=1)
            self.assertEqual(runtime.tick()['state'], 'clock_rollback')
            collect.assert_called_once()

    def test_stop_marker_seen_before_read(self):
        runtime = self.start()
        (self.root / 'state' / 'STOP').write_text('')
        with patch('ai_ceo.observers.collect_sync', return_value=self.observation) as collect:
            self.assertEqual(runtime.tick()['state'],'stopped')
            collect.assert_not_called()


if __name__ == '__main__': unittest.main()
