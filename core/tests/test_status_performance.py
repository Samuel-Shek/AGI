from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from activity_mvp.storage import ensure_ready


class StatusPerformanceIndexTests(unittest.TestCase):
    def test_quick_file_progress_count_uses_covering_index_after_upgrade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database_path = Path(tmp) / "activity.sqlite"
            conn = ensure_ready(database_path)
            conn.execute("DROP INDEX IF EXISTS idx_file_watch_coverage_gaps_affects_attribution")
            conn.executemany(
                "INSERT INTO file_watch_coverage_gaps "
                "(id,root_id,observed_at,reason_code,affects_attribution,created_at) "
                "VALUES(?,?,?,?,?,?)",
                [(str(n), "fixture", "2026-10-16T00:00:00+00:00", "fixture", n % 3 != 0,
                  "2026-10-16T00:00:00+00:00") for n in range(4096)],
            )
            conn.commit()
            before = [tuple(row) for row in conn.execute("SELECT * FROM file_watch_coverage_gaps ORDER BY id")]
            expected = conn.execute("SELECT COUNT(*) FROM file_watch_coverage_gaps WHERE affects_attribution=1").fetchone()[0]
            conn.close()

            conn = ensure_ready(database_path)
            try:
                plan = conn.execute(
                    "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM file_watch_coverage_gaps WHERE affects_attribution=1"
                ).fetchall()
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM file_watch_coverage_gaps WHERE affects_attribution=1"
                ).fetchone()[0], expected)
                self.assertEqual([tuple(row) for row in conn.execute(
                    "SELECT * FROM file_watch_coverage_gaps ORDER BY id"
                )], before)
            finally:
                conn.close()
        details = "\n".join(str(row[3]) for row in plan)
        self.assertIn("COVERING INDEX", details)
        self.assertNotIn("SCAN file_watch_coverage_gaps", details)

    def test_timeline_provenance_lookup_uses_covering_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            database_path = Path(tmp) / "activity.sqlite"
            conn = ensure_ready(database_path)
            conn.execute("DROP INDEX idx_activity_provenance_derived_source")
            conn.commit()
            conn.close()

            # Existing schema-28 databases do not need a schema bump: the
            # idempotent bootstrap recreates the missing performance index.
            conn = ensure_ready(database_path)
            try:
                plan = conn.execute(
                    """
                    EXPLAIN QUERY PLAN
                    SELECT fs.ocr_redacted_summary
                    FROM activity_provenance AS prov
                    JOIN frame_signals AS fs ON fs.id = prov.source_id
                    WHERE prov.derived_type = 'extracted_activity'
                      AND prov.derived_id = ?
                      AND prov.source_type = 'frame_signal'
                    LIMIT 1
                    """,
                    ("activity_probe",),
                ).fetchall()
            finally:
                conn.close()

        details = "\n".join(str(row[3]) for row in plan)
        self.assertIn("idx_activity_provenance_derived_source", details)
        self.assertNotIn("SCAN prov", details)


if __name__ == "__main__":
    unittest.main()
