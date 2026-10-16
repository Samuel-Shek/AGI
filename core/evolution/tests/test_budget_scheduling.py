from pathlib import Path
import tempfile
import time
import unittest

from ai_ceo.budget import BudgetLedger
from ai_ceo.storage import Store
from ai_ceo.policy import GateDenied
from ai_ceo.scheduling import admit_background, wake_decision, retry_decision
from ai_ceo.planning import decision_card, rank_candidates


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.b = BudgetLedger(self.store.conn)

    def tearDown(self):
        self.store.close(); self.temp.cleanup()

    def test_no_default_budget(self):
        with self.assertRaises(GateDenied): self.b.reserve("none", "r", 1)

    def test_reserved_and_unknown_cost_block_overallocation(self):
        self.b.configure("day", "operator-test", "synthetic-unit", 100, time.time()+60)
        self.b.reserve("day", "r1", 80)
        self.b.mark_unknown("r1")
        with self.assertRaises(GateDenied): self.b.cancel_unstarted("r1")
        with self.assertRaises(GateDenied): self.b.reserve("day", "r2", 30)
        self.b.settle("r1", 50)
        self.b.reserve("day", "r2", 30)

    def test_idempotent_reservation_cannot_change_scope_or_amount(self):
        self.b.configure("day", "operator-test", "synthetic-unit", 100, time.time()+60)
        one = self.b.reserve("day", "r1", 50)
        two = self.b.reserve("day", "r1", 50)
        self.assertEqual(one, two)
        with self.assertRaises(GateDenied): self.b.reserve("day", "r1", 40)

    def test_true_overspend_is_preserved_and_future_calls_blocked(self):
        self.b.configure("day", "operator-test", "synthetic-unit", 100, time.time()+60)
        self.b.reserve("day", "r1", 50)
        self.b.settle("r1", 120)
        with self.assertRaises(GateDenied): self.b.reserve("day", "r2", 1)
        with self.assertRaises(GateDenied): self.b.settle("r1", 50)
        self.assertEqual(self.store.conn.execute("SELECT actual FROM ceo_budget_reservations").fetchone()[0], 120)

    def test_restart_preserves_reservations(self):
        self.b.configure("day", "operator-test", "synthetic-unit", 100, time.time()+60)
        self.b.reserve("day", "r1", 90)
        self.store.close()
        self.store = Store(Path(self.temp.name))
        self.b = BudgetLedger(self.store.conn)
        with self.assertRaises(GateDenied): self.b.reserve("day", "r2", 20)


class SchedulingTests(unittest.TestCase):
    def test_wake_coalesces_and_invalidates(self):
        self.assertEqual(wake_decision("a", "b", 10, 10000)["checks_to_run"], 1)
        self.assertTrue(wake_decision("a", "a", 100, 50)["invalidate_old_leases"])
        self.assertEqual(wake_decision("a", "a", 10, 20)["checks_to_run"], 0)

    def test_unknown_resource_or_active_user_blocks_background(self):
        self.assertFalse(admit_background({}, estimated_work_bytes=1)["allowed"])
        snapshot = dict(ac_power=True,idle_seconds=0,free_bytes=30*1024**3,engineering_tasks=0,artifact_bytes=0)
        self.assertEqual(admit_background(snapshot,estimated_work_bytes=100)["reason"], "user_active")
        snapshot["idle_seconds"] = 200
        self.assertTrue(admit_background(snapshot,estimated_work_bytes=100)["allowed"])
        snapshot["free_bytes"] = 2*1024**3
        self.assertFalse(admit_background(snapshot,estimated_work_bytes=100)["allowed"])

    def test_nan_infinity_wrong_types_and_negative_resources_refused(self):
        for invalid in [float("nan"),float("inf"),-1,"300",True]:
            snapshot = dict(ac_power=True,idle_seconds=invalid,free_bytes=30*1024**3,engineering_tasks=0,artifact_bytes=0)
            self.assertFalse(admit_background(snapshot,estimated_work_bytes=100)["allowed"])

    def test_unknown_effect_no_evidence_or_budget_exhaustion_blocks_retry(self):
        for state, evidence, transient, attempts in [("unknown",True,True,0),("none",False,False,0),("none",True,True,2)]:
            self.assertFalse(retry_decision(attempts,effect_status=state,has_new_evidence=evidence,transient=transient)["retry"])

    def test_unknown_estimates_do_not_outrank_measured_same_priority(self):
        a = {"priority":2, "expected_saved_minutes":1000000, "implementation_cost_minutes":None}
        b = {"priority":2, "expected_saved_minutes":20, "implementation_cost_minutes":10,"evidence_confidence":0.9}
        self.assertEqual(rank_candidates([a,b])[0], b)

    def test_recovered_problem_does_not_generate_upgrade(self):
        card = decision_card({"status":"recovered","decision":"no_change","new_version":"999"})
        self.assertEqual(card["recommended_option"], "no_change")
        self.assertEqual(len(card["alternatives"]), 6)
        self.assertIsNone(card["expected_saved_minutes"])


if __name__ == "__main__": unittest.main()
