from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from activity_mvp.document_changes import AUTHORIZATION_LOCK_FILENAME
from activity_mvp.ha_source import (
    generate_package,
    get_package,
    get_package_handoff_summary,
    get_package_memory_candidates,
    list_packages,
    revoke_package,
)
from activity_mvp.lifecycle import delete_range
from activity_mvp.storage import ensure_ready


_NOW = datetime.now().astimezone().replace(second=0, microsecond=0)
START = (_NOW - timedelta(minutes=30)).isoformat(timespec="seconds")
END = _NOW.isoformat(timespec="seconds")


class HaSourceAuthorizationLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.db_path = self.root / "activity.sqlite"
        conn = ensure_ready(self.db_path)
        conn.close()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _generate(self, suffix: str = "seed") -> dict:
        conn = ensure_ready(self.db_path)
        try:
            return generate_package(
                conn,
                START,
                END,
                self.root / f"packages-{suffix}",
                write_markdown=False,
            )
        finally:
            conn.close()

    def test_overdue_package_is_unreadable_before_physical_cleanup(self) -> None:
        result = self._generate("expired-boundary")
        package_id = result["package"]["package_id"]
        conn = ensure_ready(self.db_path)
        try:
            conn.execute(
                "UPDATE ha_source_records SET retention_until=? WHERE package_id=?",
                (
                    (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(
                        timespec="seconds"
                    ),
                    package_id,
                ),
            )
            conn.commit()

            payload = get_package(conn, package_id)

            self.assertEqual(payload["source_status"], "expired_deleted")
            self.assertEqual(payload["reason"], "retention_expired")
            self.assertFalse(payload["raw_access"])
            self.assertNotIn("activities", payload)
            self.assertTrue(Path(result["json_path"]).exists())
        finally:
            conn.close()

    def test_generate_and_delete_are_serialized_through_package_commit(self) -> None:
        import activity_mvp.ha_source as ha_source_module

        export_complete = threading.Event()
        release_generation = threading.Event()
        delete_started = threading.Event()
        delete_done = threading.Event()
        errors: list[BaseException] = []
        generated: dict = {}
        deleted: dict = {}
        original_export = ha_source_module.cleaned_timeline_export

        def paused_export(*args, **kwargs):
            result = original_export(*args, **kwargs)
            export_complete.set()
            if not release_generation.wait(5):
                raise AssertionError("test did not release package generation")
            return result

        def run_generate() -> None:
            conn = ensure_ready(self.db_path)
            try:
                generated.update(
                    generate_package(
                        conn,
                        START,
                        END,
                        self.root / "packages-race",
                        write_markdown=False,
                    )
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
                export_complete.set()
                release_generation.set()
            finally:
                conn.close()

        def run_delete() -> None:
            delete_started.set()
            conn = ensure_ready(self.db_path)
            try:
                deleted.update(delete_range(conn, START, END))
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                delete_done.set()
                conn.close()

        with patch("activity_mvp.ha_source.cleaned_timeline_export", side_effect=paused_export):
            generation_thread = threading.Thread(target=run_generate, daemon=True)
            generation_thread.start()
            self.assertTrue(export_complete.wait(5), "generation did not finish its source read")

            deletion_thread = threading.Thread(target=run_delete, daemon=True)
            deletion_thread.start()
            self.assertTrue(delete_started.wait(5), "delete thread did not start")
            try:
                self.assertFalse(
                    delete_done.wait(0.25),
                    "delete committed between package source read and package commit",
                )
            finally:
                release_generation.set()

            generation_thread.join(5)
            deletion_thread.join(5)

        self.assertFalse(generation_thread.is_alive(), "generation thread did not finish")
        self.assertFalse(deletion_thread.is_alive(), "delete thread did not finish")
        if errors:
            raise errors[0]

        package_id = generated["package"]["package_id"]
        self.assertTrue((self.root / AUTHORIZATION_LOCK_FILENAME).is_file())
        self.assertEqual(deleted["cascade"]["ha_source_records"], 1)
        conn = ensure_ready(self.db_path)
        try:
            self.assertEqual(get_package(conn, package_id)["source_status"], "user_deleted")
            active_count = conn.execute(
                "SELECT COUNT(*) AS count FROM ha_source_records WHERE package_id=? AND source_status='active'",
                (package_id,),
            ).fetchone()["count"]
            self.assertEqual(active_count, 0)
        finally:
            conn.close()
        self.assertFalse(Path(generated["json_path"]).exists())

    def test_generate_export_does_not_hold_sqlite_writer_slot(self) -> None:
        import activity_mvp.ha_source as ha_source_module

        export_started = threading.Event()
        release_export = threading.Event()
        writer_done = threading.Event()
        errors: list[BaseException] = []
        original_export = ha_source_module.cleaned_timeline_export

        def paused_export(*args, **kwargs):
            result = original_export(*args, **kwargs)
            export_started.set()
            if not release_export.wait(5):
                raise AssertionError("test did not release package export")
            return result

        def run_generate() -> None:
            conn = ensure_ready(self.db_path)
            try:
                generate_package(
                    conn,
                    START,
                    END,
                    self.root / "packages-writer-slot",
                    write_markdown=False,
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
                export_started.set()
                release_export.set()
            finally:
                conn.close()

        def run_writer() -> None:
            writer_conn = None
            try:
                writer_conn = ensure_ready(self.db_path)
                writer_conn.execute(
                    "INSERT INTO daemon_state(key,value,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                    ("concurrent_export_writer", "committed", "2026-07-18T09:10:00+08:00"),
                )
                writer_conn.commit()
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                if writer_conn is not None:
                    writer_conn.close()
                writer_done.set()

        with patch("activity_mvp.ha_source.cleaned_timeline_export", side_effect=paused_export):
            generation_thread = threading.Thread(target=run_generate, daemon=True)
            generation_thread.start()
            self.assertTrue(export_started.wait(5), "generation did not enter export")
            writer_thread = threading.Thread(target=run_writer, daemon=True)
            writer_thread.start()
            try:
                self.assertTrue(
                    writer_done.wait(1),
                    "package export held SQLite's writer slot",
                )
            finally:
                release_export.set()
            generation_thread.join(5)
            writer_thread.join(5)
        self.assertFalse(generation_thread.is_alive())
        self.assertFalse(writer_thread.is_alive())
        if errors:
            raise errors[0]

    def test_public_package_reads_hold_lease_through_final_payload(self) -> None:
        import activity_mvp.ha_source as ha_source_module

        cases = (
            ("get", get_package, "_get_package_locked"),
            ("handoff", get_package_handoff_summary, "_get_package_handoff_summary_locked"),
            ("memory", get_package_memory_candidates, "_get_package_memory_candidates_locked"),
            ("list", lambda conn, _package_id: list_packages(conn), "_list_packages_locked"),
        )

        for index, (label, public_reader, helper_name) in enumerate(cases):
            with self.subTest(reader=label):
                result = self._generate(f"read-{index}")
                package_id = result["package"]["package_id"]
                lock_path = self.root / AUTHORIZATION_LOCK_FILENAME
                if index == 0:
                    # Simulate an active package created by an older Core on a
                    # database that never enabled document tracking.
                    lock_path.unlink()
                reader_inside = threading.Event()
                release_reader = threading.Event()
                revoke_started = threading.Event()
                revoke_done = threading.Event()
                errors: list[BaseException] = []
                reader_result: dict = {}
                original_helper = getattr(ha_source_module, helper_name)

                def paused_helper(*args, **kwargs):
                    value = original_helper(*args, **kwargs)
                    reader_inside.set()
                    if not release_reader.wait(5):
                        raise AssertionError("test did not release package reader")
                    return value

                def run_reader() -> None:
                    conn = ensure_ready(self.db_path)
                    try:
                        reader_result["value"] = public_reader(conn, package_id)
                    except BaseException as exc:  # pragma: no cover - surfaced below
                        errors.append(exc)
                        reader_inside.set()
                        release_reader.set()
                    finally:
                        conn.close()

                def run_revoke() -> None:
                    conn = ensure_ready(self.db_path)
                    try:
                        revoke_started.set()
                        revoke_package(conn, package_id, "authorization_lease_test")
                    except BaseException as exc:  # pragma: no cover - surfaced below
                        errors.append(exc)
                    finally:
                        revoke_done.set()
                        conn.close()

                with patch(f"activity_mvp.ha_source.{helper_name}", side_effect=paused_helper):
                    reader_thread = threading.Thread(target=run_reader, daemon=True)
                    reader_thread.start()
                    self.assertTrue(reader_inside.wait(5), f"{label} reader did not assemble its payload")
                    self.assertTrue(lock_path.is_file(), "HA read did not initialize the revocation barrier")

                    revoke_thread = threading.Thread(target=run_revoke, daemon=True)
                    revoke_thread.start()
                    self.assertTrue(revoke_started.wait(5), "revoke thread did not start")
                    try:
                        self.assertFalse(
                            revoke_done.wait(0.25),
                            f"revoke committed while {label} was returning active package content",
                        )
                    finally:
                        release_reader.set()

                    reader_thread.join(5)
                    revoke_thread.join(5)

                self.assertFalse(reader_thread.is_alive(), f"{label} reader thread did not finish")
                self.assertFalse(revoke_thread.is_alive(), "revoke thread did not finish")
                if errors:
                    raise errors[0]
                value = reader_result["value"]
                if label == "list":
                    self.assertTrue(
                        any(
                            item["package_id"] == package_id and item["source_status"] == "active"
                            for item in value
                        )
                    )
                else:
                    self.assertEqual(value["source_status"], "active")

                conn = ensure_ready(self.db_path)
                try:
                    self.assertEqual(get_package(conn, package_id)["source_status"], "revoked")
                finally:
                    conn.close()
                self.assertFalse(Path(result["json_path"]).exists())


if __name__ == "__main__":
    unittest.main()
