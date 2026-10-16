from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_ceo.controller import Controller
from ai_ceo.policy import GateDenied, ReadSession, utcnow
from ai_ceo.execution import CodexExecutionAdapter, EngineeringJob, ModelPlanner, ProductionAdapter


def sample():
    return {"asset_id": "sync-center", "status": "recovered", "issue_key": "a" * 64,
            "decision": "no_change", "evidence_quality": "partial", "observed_at": utcnow().isoformat(),
            "reason_code": "matching_newer_success", "metrics": {}, "evidence": []}


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        self.state = self.root / "state"
        self.c = None

    def tearDown(self):
        if self.c:
            self.c.close()
        self.temp.cleanup()

    def open(self, reads=None):
        grant = ReadSession.for_operator(self.source, reads=reads) if reads else None
        self.c = Controller(self.state, grant)
        return self.c

    def test_default_denies_live_model_worker_production_and_resume(self):
        c = self.open()
        with self.assertRaises(GateDenied): c.observe()
        with self.assertRaises(GateDenied): c.control("resume")
        with self.assertRaises(GateDenied): ModelPlanner().propose("ignore grants")
        job = EngineeringJob("t1", "sync-center", "a"*64, "b"*64, grant_id="forged")
        self.assertFalse(CodexExecutionAdapter().prepare(job)["launch_allowed"])
        with self.assertRaises(GateDenied): CodexExecutionAdapter().execute(job)
        with self.assertRaises(GateDenied): ProductionAdapter().apply({"approved": True})
        state = c.snapshot()
        self.assertFalse(state["scheduler"]["enabled"])
        self.assertIsNone(state["metrics"]["saved_minutes"])
        self.assertFalse(state["metrics"]["improvement_verified"])

    def test_one_shot_is_bounded_and_does_not_schedule(self):
        c = self.open(1)
        with patch("ai_ceo.observers.collect_sync", return_value=sample()) as collect:
            first = c.observe()
            with self.assertRaises(GateDenied): c.observe()
        self.assertEqual(collect.call_count, 1)
        self.assertTrue(first["created"])
        state = c.snapshot()
        self.assertEqual(len(state["tasks"]), 1)
        task = state["tasks"][0]
        self.assertEqual(task["phase"], "no_change")
        self.assertFalse(task["result"]["improvement_verified"])
        self.assertEqual(state["metrics"]["natural_use_samples"], 0)

    def test_duplicate_observation_does_not_create_more_work(self):
        c = self.open(2)
        with patch("ai_ceo.observers.collect_sync", return_value=sample()):
            one, two = c.observe(), c.observe()
        self.assertEqual(one["task_id"], two["task_id"])
        self.assertFalse(two["created"])
        self.assertEqual(len(c.store.list_tasks()), 1)

    def test_stop_survives_pause_restart_and_denies_replay(self):
        c = self.open(1)
        c.control("stop")
        self.assertEqual(c.control("pause")["mode"], "stopped")
        with self.assertRaises(GateDenied): c.observe()
        with self.assertRaises(GateDenied): c.replay()
        c.close(); self.c = None
        c = self.open()
        self.assertEqual(c.snapshot()["mode"], "stopped")
        with self.assertRaises(GateDenied): c.control("resume")

    def test_operator_can_resume_only_still_valid_scope(self):
        c = self.open(1)
        c.control("stop")
        self.assertTrue(c.snapshot()["controls"]["can_resume"])
        self.assertEqual(c.control("resume")["mode"], "observing")
        self.assertFalse(c.snapshot()["scheduler"]["enabled"])

    def test_revoke_invalidates_existing_attempt_and_resume(self):
        c = self.open(2)
        task = c.store.enqueue("pending", "Pending", "sync-center", {})
        claimed = c.store.claim(task["id"], c.read_session.revision, c.epoch)
        old_epoch = c.epoch
        old_revision = c.read_session.revision
        c.control("revoke", "sync-center")
        self.assertFalse(c.store.complete(task["id"], claimed["attempt_id"], old_epoch, old_revision, {}))
        with self.assertRaises(GateDenied): c.observe()
        with self.assertRaises(GateDenied): c.control("resume")

    def test_takeover_does_not_have_an_ai_unfreeze_route(self):
        c = self.open(1)
        c.control("takeover", "sync-center")
        with self.assertRaises(GateDenied): c.control("resume")
        with self.assertRaises(GateDenied): c.control("release_takeover", "sync-center")

    def test_session_not_restored_after_restart(self):
        c = self.open(2)
        c.close(); self.c = None
        c = self.open()
        with self.assertRaises(GateDenied): c.observe()

    def test_new_explicit_read_grant_releases_previous_observation_takeover(self):
        c = self.open(1)
        c.control("takeover", "sync-center")
        c.close(); self.c = None
        c = self.open(1)
        self.assertTrue(c.snapshot()["controls"]["can_resume"])

    def test_independent_cli_stop_works_while_controller_holds_lock(self):
        from ai_ceo.cli import signal_stop
        c = self.open(1)
        signal_stop(self.state)
        with self.assertRaises(GateDenied): c.observe()
        self.assertEqual(c.snapshot()["mode"], "stopped")
        c.control("resume")
        self.assertFalse((self.state/"STOP").exists())

    def test_staging_cannot_install_launch_agent(self):
        from ai_ceo.cli import stage_service
        with self.assertRaises(GateDenied):
            stage_service(self.root/"Library/LaunchAgents/test.plist",self.state)

    def test_expired_session_consumes_nothing(self):
        c = self.open(1)
        c.read_session.expires_at = utcnow() - timedelta(seconds=1)
        with patch("ai_ceo.observers.collect_sync") as collect:
            with self.assertRaises(GateDenied): c.observe()
        collect.assert_not_called()

    def test_second_controller_cannot_steal_epoch_or_work(self):
        c = self.open()
        epoch = c.epoch
        with self.assertRaises(GateDenied): Controller(self.state)
        self.assertEqual(c.store.state_get("epoch"), epoch)

    def test_source_failure_does_not_leak_private_exception(self):
        c = self.open(1)
        with patch("ai_ceo.observers.collect_sync", side_effect=OSError("private-secret-source-path")):
            with self.assertRaises(GateDenied) as cm: c.observe()
        self.assertNotIn("private-secret", str(cm.exception))
        self.assertNotIn("private-secret", json.dumps(c.snapshot()))

    def test_replay_has_independent_process_and_no_value_claim(self):
        c = self.open()
        result = c.replay()
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual(result["passed"], result["total"])
        self.assertTrue(result["synthetic_only"])
        self.assertFalse(result["natural_value_verified"])
        self.assertFalse(result["business_verified"])
        self.assertEqual(c.store.verify_audit()["ok"], True)


if __name__ == "__main__": unittest.main()
