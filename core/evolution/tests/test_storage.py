from __future__ import annotations
import ast
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ai_ceo.storage import Store
from ai_ceo.vendor import hermes_kanban as kernel

PROJECT = Path(__file__).resolve().parents[1]


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "state")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def task(self, key="event", asset="sync-center"):
        return self.store.enqueue(key, "Synthetic evidence analysis", asset, {"synthetic": True})

    def claim(self, key="event", asset="sync-center"):
        t = self.task(key, asset)
        return self.store.claim(t["id"], 1, 1)

    def run_code(self, code, *args, expected=0):
        result = subprocess.run([sys.executable, "-c", code, *map(str, args)], cwd=PROJECT,
                                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, expected, result.stderr)
        return result

    def test_native_kernel_runs_and_done_means_execution_only(self):
        claimed = self.claim()
        row = self.store.conn.execute("SELECT * FROM task_runs").fetchone()
        self.assertEqual(row["id"], claimed["native_run_id"])
        self.assertEqual(row["claim_lock"], claimed["attempt_id"])
        self.assertEqual(row["status"], "running")
        self.assertTrue(self.store.complete(claimed["id"], claimed["attempt_id"], 1, 1,
                                            {"result": "candidate only"}))
        task = self.store.get_task(claimed["id"])
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["phase"], "verifying")
        self.assertEqual(self.store.conn.execute("SELECT outcome FROM task_runs").fetchone()[0], "completed")

    def test_observation_analysis_can_choose_no_change(self):
        claimed = self.claim()
        self.assertTrue(self.store.complete(claimed["id"], claimed["attempt_id"], 1, 1,
                          {"task_kind": "observation_analysis", "phase": "no_change"}))
        self.assertEqual(self.store.get_task(claimed["id"])["phase"], "no_change")
        self.assertFalse(self.task()["created"])

    def test_analysis_cannot_fabricate_deployment_success(self):
        claimed = self.claim()
        with self.assertRaises(ValueError):
            self.store.complete(claimed["id"], claimed["attempt_id"], 1, 1,
                                {"task_kind": "observation_analysis", "phase": "retained"})
        self.assertEqual(self.store.get_task(claimed["id"])["status"], "running")

    def test_atomic_dedupe_rollback_after_native_create(self):
        original = self.store._audit
        with patch.object(self.store, "_audit", side_effect=RuntimeError("crash window")):
            with self.assertRaises(RuntimeError):
                self.task()
        self.assertEqual(self.store.list_tasks(), [])
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0], 0)
        self.assertTrue(self.task()["created"])
        self.assertFalse(self.task()["created"])

    def test_concurrent_process_enqueue_exactly_once(self):
        code = """
import sys
from pathlib import Path
from ai_ceo.storage import Store
with Store(Path(sys.argv[1])) as s:
 result=s.enqueue('concurrent','same event','sync-center',{'synthetic':True})
 print(result['id'],int(result['created']))
"""
        processes = [subprocess.Popen([sys.executable, "-c", code, str(self.root / "state")],
                     cwd=PROJECT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(8)]
        outputs = []
        for proc in processes:
            out, err = proc.communicate(timeout=30)
            self.assertEqual(proc.returncode, 0, err)
            outputs.append(out.strip().split())
        self.assertEqual(len({out[0] for out in outputs}), 1)
        self.assertEqual(sum(int(out[1]) for out in outputs), 1)
        self.assertEqual(len(self.store.list_tasks()), 1)
        self.assertTrue(self.store.verify_audit()["ok"])

    def test_concurrent_process_claim_has_one_native_run(self):
        task = self.task()
        code = """
import sys
from pathlib import Path
from ai_ceo.storage import Store
with Store(Path(sys.argv[1])) as s:
 print(int(s.claim(sys.argv[2],1,1) is not None))
"""
        processes = [subprocess.Popen([sys.executable, "-c", code, str(self.root / "state"), task["id"]],
                     cwd=PROJECT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(6)]
        winners = 0
        for proc in processes:
            out, err = proc.communicate(timeout=30)
            self.assertEqual(proc.returncode, 0, err)
            winners += int(out)
        self.assertEqual(winners, 1)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0], 1)

    def test_hard_process_crash_before_enqueue_commit_rolls_back_both_records(self):
        code = """
import os,sys
from pathlib import Path
from ai_ceo.storage import Store
s=Store(Path(sys.argv[1]))
s._audit=lambda *args: os._exit(73)
s.enqueue('killed','crash event','sync-center',{})
"""
        self.run_code(code, self.root / "state", expected=73)
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
        self.assertTrue(self.store.enqueue("killed", "crash event", "sync-center", {})["created"])

    def test_hard_process_crash_after_effect_start_retains_unknown(self):
        code = """
import os,sys
from pathlib import Path
from ai_ceo.storage import Store
s=Store(Path(sys.argv[1]))
t=s.enqueue('effect-crash','synthetic operation','sync-center',{})
a=s.claim(t['id'],1,1)
s.register_effect('external-operation',t['id'],a['attempt_id'])
s.set_effect_state('external-operation','applying')
os._exit(75)
"""
        self.run_code(code, self.root / "state", expected=75)
        self.store.recover(2)
        self.assertEqual(self.store.effects_list()[0]["state"], "unknown")
        self.assertEqual(self.store.list_tasks()[0]["phase"], "effect_unknown")
        self.assertIsNone(self.store.claim(self.store.list_tasks()[0]["id"], 1, 2))

    def test_hard_process_crash_between_native_claim_and_fence_is_atomic(self):
        task = self.task()
        code = """
import os,sys
from pathlib import Path
from ai_ceo.storage import Store,kernel
s=Store(Path(sys.argv[1]))
original=kernel.claim_task
def crash(*a,**k):
 original(*a,**k)
 os._exit(74)
kernel.claim_task=crash
s.claim(sys.argv[2],1,1)
"""
        self.run_code(code, self.root / "state", task["id"], expected=74)
        self.assertEqual(self.store.get_task(task["id"])["status"], "ready")
        self.assertEqual(self.store.conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0], 0)
        self.assertIsNotNone(self.store.claim(task["id"], 1, 1))

    def test_forgeries_expiry_and_revocation_rejected(self):
        claimed = self.claim()
        args = (claimed["id"], claimed["attempt_id"], 1, 1, {})
        self.assertFalse(self.store.complete(claimed["id"], "forged", 1, 1, {}))
        self.assertFalse(self.store.complete(claimed["id"], claimed["attempt_id"], 2, 1, {}))
        self.assertFalse(self.store.complete(claimed["id"], claimed["attempt_id"], 1, 2, {}))
        self.store.state_set("grant_revision:sync-center", 2)
        self.assertFalse(self.store.complete(*args))
        self.store.state_set("grant_revision:sync-center", 1)
        self.store.conn.execute("UPDATE ceo_tasks SET lease_until=0")
        self.assertFalse(self.store.complete(*args))
        self.assertEqual(self.store.get_task(claimed["id"])["status"], "running")

    def test_native_run_mismatch_rejected(self):
        claimed = self.claim()
        self.store.conn.execute("UPDATE tasks SET current_run_id=NULL")
        self.assertFalse(self.store.complete(claimed["id"], claimed["attempt_id"], 1, 1, {}))

    def test_one_asset_one_attempt(self):
        first = self.claim()
        second = self.task("event-2")
        self.assertIsNone(self.store.claim(second["id"], 1, 1))
        self.assertIsNotNone(self.claim("event-3", "different-asset"))

    def test_running_tasks_cannot_be_deferred(self):
        claimed = self.claim()
        with self.assertRaises(ValueError):
            self.store.defer(claimed["id"], "no_change", "would fake success")
        self.assertTrue(self.store.retire(claimed["id"], "manual stop"))
        self.assertEqual(self.store.get_task(claimed["id"])["phase"], "blocked")

    def test_effect_unknown_after_restart_cannot_be_reexecuted(self):
        claimed = self.claim()
        first = self.store.register_effect("deploy-A", claimed["id"], claimed["attempt_id"])
        self.assertTrue(first["created"])
        self.assertFalse(self.store.register_effect("deploy-A", claimed["id"], claimed["attempt_id"])["created"])
        self.store.set_effect_state("deploy-A", "applying")
        self.assertFalse(self.store.complete(claimed["id"], claimed["attempt_id"], 1, 1, {}))
        self.store.recover(2)
        self.assertEqual(self.store.effects_list()[0]["state"], "unknown")
        self.assertEqual(self.store.get_task(claimed["id"])["phase"], "effect_unknown")
        self.assertIsNone(self.store.claim(claimed["id"], 1, 2))
        self.assertFalse(self.store.complete(claimed["id"], claimed["attempt_id"], 1, 1, {}))
        with self.assertRaises(ValueError):
            self.store.set_effect_state("deploy-A", "applying")
        with self.assertRaises(PermissionError):
            self.store.register_effect("deploy-A", claimed["id"], "new-attempt")
        with self.assertRaises(ValueError):
            self.store.set_effect_state("deploy-A", "reconciled")
        self.store.set_effect_state("deploy-A", "reconciled", {"synthetic_receipt": "observed_success"})
        with self.assertRaises(ValueError):
            self.store.set_effect_state("deploy-A", "applying")

    def test_stale_effect_cannot_apply_or_confirm(self):
        claimed = self.claim()
        self.store.register_effect("operation", claimed["id"], claimed["attempt_id"])
        self.store.state_set("grant_revision:sync-center", 2)
        with self.assertRaises(PermissionError):
            self.store.set_effect_state("operation", "applying")
        self.store.recover(2)
        self.assertEqual(self.store.effects_list()[0]["state"], "cancelled")

    def test_recovery_keeps_stopped_and_never_reruns(self):
        claimed = self.claim()
        self.store.state_set("mode", "stopped")
        self.store.recover(2)
        self.assertEqual(self.store.state_get("mode"), "stopped")
        self.assertIsNone(self.store.claim(claimed["id"], 1, 2))
        with self.assertRaises(ValueError):
            self.store.recover(1)
        self.store.close()
        self.store = Store(self.root / "state")
        self.assertEqual(self.store.state_get("mode"), "stopped")

    def test_successful_effect_needs_receipt(self):
        claimed = self.claim()
        self.store.register_effect("operation", claimed["id"], claimed["attempt_id"])
        self.store.set_effect_state("operation", "applying")
        with self.assertRaises(ValueError):
            self.store.set_effect_state("operation", "confirmed")
        self.store.set_effect_state("operation", "confirmed", {"synthetic_receipt": "ok"})
        self.assertTrue(self.store.complete(claimed["id"], claimed["attempt_id"], 1, 1, {}))

    def test_audit_tamper_detected_and_boundary_disclosed(self):
        self.store.append_audit("first", {"case": 1})
        self.store.append_audit("second", {"case": 2})
        check = self.store.verify_audit()
        self.assertTrue(check["ok"])
        self.assertIn("no external immutable anchor", check["boundary"])
        self.store.conn.execute("UPDATE ceo_audit SET details='{}' WHERE id=1")
        self.assertFalse(self.store.verify_audit()["ok"])

    def test_backup_is_consistent_and_preserves_unknown_effect(self):
        claimed = self.claim()
        self.store.register_effect("operation", claimed["id"], claimed["attempt_id"])
        self.store.set_effect_state("operation", "applying")
        self.store.recover(2)
        dest = self.root / "consistent-backup.db"
        self.assertEqual(self.store.backup(dest)["integrity"], "ok")
        with sqlite3.connect(dest) as copied:
            self.assertEqual(copied.execute("SELECT state FROM ceo_effects").fetchone()[0], "unknown")
            self.assertEqual(copied.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        with self.assertRaises(FileExistsError):
            self.store.backup(dest)

    def test_never_adopts_existing_database_or_symlinks(self):
        other = self.root / "foreign"
        other.mkdir(mode=0o700)
        db = other / "kanban.db"
        db.write_bytes(b"not our database")
        before = db.read_bytes()
        with self.assertRaises(PermissionError):
            Store(other)
        self.assertEqual(db.read_bytes(), before)
        self.assertEqual({p.name for p in other.iterdir()}, {"kanban.db"})
        alias = self.root / "alias"
        alias.symlink_to(self.root / "state", target_is_directory=True)
        with self.assertRaises(PermissionError):
            Store(alias)

    def test_json_nan_and_bad_lease_rejected(self):
        with self.assertRaises(ValueError):
            self.store.enqueue("nan", "bad", "asset", {"score": float("nan")})
        task = self.task()
        for value in [0, -1, True, 3601]:
            with self.assertRaises(ValueError):
                self.store.claim(task["id"], 1, 1, ttl_seconds=value)

    def test_no_profile_hooks_worker_imports_and_pin_matches(self):
        module = Path(kernel.__file__)
        provenance = json.loads((module.parent / "PROVENANCE.json").read_text())
        self.assertEqual(hashlib.sha256(module.read_bytes()).hexdigest(), provenance["capsule_sha256"])
        tree = ast.parse(module.read_text())
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module)
        allowed = {"__future__", "contextlib", "json", "os", "random", "re", "secrets", "sqlite3", "time", "dataclasses", "pathlib", "typing", "socket"}
        self.assertTrue(set(imports) <= allowed, imports)
        self.assertNotIn("hermes_cli", sys.modules)
        self.assertEqual(self.store.conn.execute("PRAGMA journal_mode").fetchone()[0], "delete")
        self.assertFalse(hasattr(kernel, "connect"))
        self.assertFalse(hasattr(kernel, "dispatch_once"))


if __name__ == "__main__":
    unittest.main()
