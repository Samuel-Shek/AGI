import json
import sqlite3
import unittest

from activity_mvp import daemon


def failing(exc):
    try:
        raise exc
    except Exception as caught:
        return caught


class ClosureTickVisibilityTests(unittest.TestCase):
    def test_failure_record_is_redacted_and_throttled(self):
        counters = {"consecutive": 0, "total": 0}
        secret = "/Users/test/私人笔记/客户名单.md"
        records = [daemon._closure_tick_failure_record(failing(OSError(2, "missing", secret)), counters) for _ in range(5)]
        logged = [r for r in records if r is not None]
        self.assertEqual([r["consecutive"] for r in logged], [1, 2, 4])
        self.assertEqual(counters, {"consecutive": 5, "total": 5})
        text = json.dumps(logged, ensure_ascii=False)
        self.assertNotIn("私人", text); self.assertNotIn("/Users/", text); self.assertNotIn("missing", text)
        self.assertEqual(logged[0]["error_type"], "FileNotFoundError")
        self.assertEqual(logged[0]["code"], "task_closure_tick_failed")
        self.assertTrue(logged[0]["location"].startswith("test_daemon_closure_tick_visibility.py:failing:"))

    def test_busy_database_code_and_recovery(self):
        counters = {"consecutive": 0, "total": 0}
        record = daemon._closure_tick_failure_record(failing(sqlite3.OperationalError("database is locked")), counters)
        self.assertEqual(record["code"], "database_busy")
        recovered = daemon._closure_tick_recovered_record(counters)
        self.assertEqual(recovered, {"event": "task_closure_tick_recovered", "after_consecutive": 1, "total": 1})
        self.assertIsNone(daemon._closure_tick_recovered_record(counters))


if __name__ == "__main__":
    unittest.main()
