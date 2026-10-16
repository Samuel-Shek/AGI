from __future__ import annotations

import os
import threading
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from activity_mvp.__main__ import (
    _run_get_package_command,
    _run_list_packages_command,
    _run_timeline_command,
)
from activity_mvp.daemon import _complete_status_authorized
from activity_mvp.document_changes import AUTHORIZATION_LOCK_FILENAME, document_authorization_lock
from activity_mvp.pipeline import search
from activity_mvp.quality import quality_report
from activity_mvp.server import ActivityHandler
from activity_mvp.storage import ensure_ready
from activity_mvp.timeline_export import cleaned_timeline_export


class PublicReadAuthorizationLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.db_path = self.root / "activity.sqlite"
        conn = ensure_ready(self.db_path)
        conn.close()
        lock_path = self.root / AUTHORIZATION_LOCK_FILENAME
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(descriptor)
        lock_path.chmod(0o600)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _assert_exclusive_writer_waits(self, reader) -> None:
        reader_inside = threading.Event()
        release_reader = threading.Event()
        writer_ready = threading.Event()
        start_writer = threading.Event()
        writer_attempting = threading.Event()
        writer_entered = threading.Event()
        writer_committed = threading.Event()
        errors: list[BaseException] = []

        def run_reader() -> None:
            conn = ensure_ready(self.db_path)
            try:
                reader(conn, reader_inside, release_reader)
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
                reader_inside.set()
                release_reader.set()
            finally:
                conn.close()

        def run_writer() -> None:
            conn = ensure_ready(self.db_path)
            try:
                writer_ready.set()
                if not start_writer.wait(5):
                    raise AssertionError("reader never started the competing writer")
                writer_attempting.set()
                with document_authorization_lock(conn, exclusive=True):
                    writer_entered.set()
                    conn.execute(
                        "INSERT OR REPLACE INTO daemon_state(key,value,updated_at) VALUES(?,?,?)",
                        ("lease_test_writer", "committed", "2026-07-18T00:00:00+00:00"),
                    )
                    conn.commit()
                    writer_committed.set()
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
                writer_entered.set()
                writer_committed.set()
            finally:
                conn.close()

        writer_thread = threading.Thread(target=run_writer, daemon=True)
        reader_thread = threading.Thread(target=run_reader, daemon=True)
        writer_thread.start()
        self.assertTrue(writer_ready.wait(5), "writer connection was not ready")
        reader_thread.start()
        self.assertTrue(reader_inside.wait(5), "reader did not reach the protected assembly step")
        start_writer.set()
        try:
            self.assertTrue(writer_attempting.wait(5), "exclusive writer did not attempt the lease")
            self.assertFalse(
                writer_entered.wait(0.25),
                "exclusive delete/reclassification lease entered while public read was assembling",
            )
            self.assertFalse(writer_committed.is_set())
        finally:
            release_reader.set()
        reader_thread.join(5)
        writer_thread.join(5)
        self.assertFalse(reader_thread.is_alive(), "reader thread did not finish")
        self.assertFalse(writer_thread.is_alive(), "writer thread did not finish")
        self.assertTrue(writer_committed.is_set(), "exclusive writer did not resume after the read")
        if errors:
            raise errors[0]

    def test_cleaned_timeline_export_holds_lease_through_payload_assembly(self) -> None:
        def reader(conn, entered, release) -> None:
            def blocked_locked(*_args, **_kwargs):
                entered.set()
                self.assertTrue(release.wait(5))
                return {"activities": [], "items": []}

            with patch("activity_mvp.timeline_export._cleaned_timeline_export_locked", side_effect=blocked_locked):
                cleaned_timeline_export(conn)

        self._assert_exclusive_writer_waits(reader)

    def test_confirmed_search_holds_lease_through_result_copy(self) -> None:
        def reader(conn, entered, release) -> None:
            def blocked_search(*_args, **_kwargs):
                entered.set()
                self.assertTrue(release.wait(5))
                return []

            with patch("activity_mvp.pipeline._search_confirmed_activity_view", side_effect=blocked_search):
                search(conn, "needle", confirmed_only=True)

        self._assert_exclusive_writer_waits(reader)

    def test_quality_report_holds_one_lease_through_report_assembly(self) -> None:
        def reader(conn, entered, release) -> None:
            def blocked_report(*_args, **_kwargs):
                entered.set()
                self.assertTrue(release.wait(5))
                return {"raw_access": False}

            with patch("activity_mvp.quality._quality_report_locked", side_effect=blocked_report):
                quality_report(conn)

        self._assert_exclusive_writer_waits(reader)

    def test_status_final_confirmed_view_and_payload_share_one_lease(self) -> None:
        def reader(conn, entered, release) -> None:
            def blocked_view(*_args, **_kwargs):
                entered.set()
                self.assertTrue(release.wait(5))
                return {"activities": [], "pending_confirmation_count": 0}

            payload = {"confirmed_activity": {}, "document_changes": {}}
            with patch("activity_mvp.daemon.confirmed_activity_view", side_effect=blocked_view):
                result = _complete_status_authorized(
                    conn,
                    payload,
                    "2026-07-18T00:00:00+08:00",
                    "2026-07-19T00:00:00+08:00",
                )
            self.assertEqual(result["confirmed_activity"]["confirmed_count"], 0)

        self._assert_exclusive_writer_waits(reader)

    def test_status_document_changes_are_requested_newest_first(self) -> None:
        conn = ensure_ready(self.db_path)
        try:
            payload = {"confirmed_activity": {}, "document_changes": {}}
            with (
                patch(
                    "activity_mvp.daemon.confirmed_activity_view",
                    return_value={"activities": [], "pending_confirmation_count": 0},
                ),
                patch(
                    "activity_mvp.document_changes._document_changes_status_locked",
                    return_value={"roots": []},
                ),
                patch(
                    "activity_mvp.document_changes._list_document_changes_locked",
                    return_value={"items": []},
                ) as listed,
            ):
                _complete_status_authorized(
                    conn,
                    payload,
                    "2026-07-18T00:00:00+08:00",
                    "2026-07-19T00:00:00+08:00",
                )
        finally:
            conn.close()

        self.assertEqual(listed.call_args.kwargs["sort_order"], "recent")

    def test_server_timeline_holds_lease_through_json_send(self) -> None:
        def reader(conn, entered, release) -> None:
            handler = object.__new__(ActivityHandler)

            def blocked_send(_payload, status=None) -> None:
                entered.set()
                self.assertTrue(release.wait(5))

            handler._send_json = blocked_send
            with patch(
                "activity_mvp.activity_confirmations.confirmed_activity_view",
                return_value={"activities": [{"activity_summary": "safe"}]},
            ):
                handler._send_confirmed_timeline(conn, None, None)

        self._assert_exclusive_writer_waits(reader)

    def test_server_search_holds_lease_through_json_send(self) -> None:
        def reader(conn, entered, release) -> None:
            handler = object.__new__(ActivityHandler)

            def blocked_send(_payload, status=None) -> None:
                entered.set()
                self.assertTrue(release.wait(5))

            handler._send_json = blocked_send
            with patch("activity_mvp.server.search", return_value=[{"summary": "safe"}]):
                handler._send_confirmed_search(conn, "safe", None)

        self._assert_exclusive_writer_waits(reader)

    def test_server_package_reads_hold_lease_through_json_send(self) -> None:
        def list_reader(conn, entered, release) -> None:
            handler = object.__new__(ActivityHandler)

            def blocked_send(_payload, status=None) -> None:
                entered.set()
                self.assertTrue(release.wait(5))

            handler._send_json = blocked_send
            with patch("activity_mvp.server.list_packages", return_value=[]):
                handler._send_package_list(conn)

        def get_reader(conn, entered, release) -> None:
            handler = object.__new__(ActivityHandler)

            def blocked_send(_payload, status=None) -> None:
                entered.set()
                self.assertTrue(release.wait(5))

            handler._send_json = blocked_send
            with patch(
                "activity_mvp.server.get_package",
                return_value={"package_id": "pkg_test", "source_status": "active"},
            ):
                handler._send_package(conn, "pkg_test")

        self._assert_exclusive_writer_waits(list_reader)
        self._assert_exclusive_writer_waits(get_reader)

    def test_cli_timeline_holds_lease_through_stdout_emit(self) -> None:
        def reader(conn, entered, release) -> None:
            args = SimpleNamespace(start=None, end=None, format="json", out=None)

            def blocked_print(_payload) -> None:
                entered.set()
                self.assertTrue(release.wait(5))

            with patch(
                "activity_mvp.__main__.confirmed_activity_view",
                return_value={"activities": [{"activity_summary": "safe"}]},
            ), patch("activity_mvp.__main__.print_json", side_effect=blocked_print):
                _run_timeline_command(conn, args)

        self._assert_exclusive_writer_waits(reader)

    def test_cli_package_reads_hold_lease_through_stdout_emit(self) -> None:
        def list_reader(conn, entered, release) -> None:
            def blocked_print(_payload) -> None:
                entered.set()
                self.assertTrue(release.wait(5))

            with patch("activity_mvp.__main__.list_packages", return_value=[]), patch(
                "activity_mvp.__main__.print_json",
                side_effect=blocked_print,
            ):
                _run_list_packages_command(conn)

        self._assert_exclusive_writer_waits(list_reader)

        modes = (
            (False, False, "activity_mvp.__main__.get_package"),
            (False, True, "activity_mvp.__main__.get_package_handoff_summary"),
            (True, False, "activity_mvp.__main__.get_package_memory_candidates"),
        )
        for memory_only, handoff, target in modes:
            with self.subTest(memory_candidates_only=memory_only, handoff_summary=handoff):
                def get_reader(conn, entered, release, *, _target=target) -> None:
                    args = SimpleNamespace(
                        package_id="pkg_test",
                        memory_candidates_only=memory_only,
                        handoff_summary=handoff,
                        max_items=40,
                    )

                    def blocked_print(_payload) -> None:
                        entered.set()
                        self.assertTrue(release.wait(5))

                    with patch(
                        _target,
                        return_value={"package_id": "pkg_test", "source_status": "active"},
                    ), patch("activity_mvp.__main__.print_json", side_effect=blocked_print):
                        _run_get_package_command(conn, args)

                self._assert_exclusive_writer_waits(get_reader)

    def test_nested_shared_public_reads_do_not_deadlock(self) -> None:
        conn = ensure_ready(self.db_path)
        try:
            with document_authorization_lock(conn, exclusive=False):
                export = cleaned_timeline_export(conn)
                matches = search(conn, "nothing", confirmed_only=True)
                report = quality_report(conn)
            self.assertEqual(export["activities"], [])
            self.assertEqual(matches, [])
            self.assertFalse(report["raw_access"])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
