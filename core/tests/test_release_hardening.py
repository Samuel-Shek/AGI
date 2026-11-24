from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone
from io import StringIO
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from activity_mvp.ai_connector import AIConnectorHandler
from activity_mvp.canonical_paths import obsidian_vault_root
from activity_mvp.__main__ import (
    _connect_for_command,
    _connect_busy_status,
    _core_status_current_status,
    _core_quick_status_busy_status,
    _core_quick_status_unavailable_status,
    _core_status_busy_status,
    _diagnostics_busy_status,
    _run_busy_status_command,
    _safe_cli_entrypoint,
    _safe_threading_excepthook,
)
from activity_mvp.daemon import (
    _capture_batch_last_error,
    _ensure_ascii_runtime_link,
    _runtime_core_code_hash,
    _screen_recording_permission_status,
    get_quick_status,
)
from activity_mvp.pipeline import record_capture_coverage
from activity_mvp.storage import init_schema
from activity_mvp.swift_helpers import ensure_helper


class ReleaseHardeningTests(unittest.TestCase):
    def test_cli_entrypoint_sanitizes_uncaught_exception_details(self) -> None:
        stderr = StringIO()

        def fail() -> None:
            raise RuntimeError("/Users/example/private/client-plan.md")

        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            _safe_cli_entrypoint(fail)

        self.assertEqual(raised.exception.code, 1)
        payload = json.loads(stderr.getvalue())
        self.assertEqual(payload, {"status": "error", "error": "command_failed", "raw_access": False})
        self.assertNotIn("/Users/", stderr.getvalue())
        self.assertNotIn("client-plan", stderr.getvalue())

    def test_cli_entrypoint_reports_database_busy_without_traceback(self) -> None:
        stderr = StringIO()

        def fail() -> None:
            raise sqlite3.OperationalError("database is locked at /private/customer/activity.sqlite")

        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            _safe_cli_entrypoint(fail)

        self.assertEqual(raised.exception.code, 1)
        payload = json.loads(stderr.getvalue())
        self.assertEqual(payload, {"status": "error", "error": "database_busy", "raw_access": False})
        self.assertNotIn("/private/", stderr.getvalue())
        self.assertNotIn("activity.sqlite", stderr.getvalue())

    def test_cli_entrypoint_does_not_swallow_system_exit_or_keyboard_interrupt(self) -> None:
        for exception in (SystemExit(7), KeyboardInterrupt()):
            stderr = StringIO()
            previous_hook = threading.excepthook
            with redirect_stderr(stderr), self.assertRaises(type(exception)) as raised:
                _safe_cli_entrypoint(lambda exception=exception: (_ for _ in ()).throw(exception))
            self.assertIs(raised.exception, exception)
            self.assertIs(threading.excepthook, previous_hook)
            self.assertEqual(stderr.getvalue(), "")

    def test_cli_module_entrypoint_sanitizes_corrupt_database_failure(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        core_root = repo_root / "core"
        with tempfile.TemporaryDirectory() as tmp:
            database_path = Path(tmp) / "private-client" / "activity.sqlite"
            database_path.parent.mkdir()
            database_path.write_text("not a database: /Users/example/private.md", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-m", "activity_mvp", "--db", str(database_path), "status"],
                cwd=core_root,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(
            json.loads(result.stderr),
            {"status": "error", "error": "command_failed", "raw_access": False},
        )
        for forbidden in (str(database_path), str(core_root), "/Users/", "Traceback", "not a database"):
            self.assertNotIn(forbidden, result.stderr)

    def test_cli_entrypoint_installs_sanitized_thread_exception_hook(self) -> None:
        stderr = StringIO()
        previous_hook = threading.excepthook

        def run_thread() -> None:
            def fail() -> None:
                raise RuntimeError("/Users/example/private/thread-secret.md")

            thread = threading.Thread(target=fail, name="sensitive-thread-name")
            thread.start()
            thread.join()

        with redirect_stderr(stderr):
            _safe_cli_entrypoint(run_thread)

        self.assertIs(threading.excepthook, previous_hook)
        self.assertEqual(
            json.loads(stderr.getvalue()),
            {"status": "error", "error": "thread_failed", "raw_access": False},
        )
        for forbidden in ("/Users/", "thread-secret", "sensitive-thread-name", "Traceback"):
            self.assertNotIn(forbidden, stderr.getvalue())

    def test_safe_thread_exception_hook_ignores_system_exit(self) -> None:
        stderr = StringIO()
        args = threading.ExceptHookArgs((SystemExit, SystemExit(4), None, None))
        with redirect_stderr(stderr):
            _safe_threading_excepthook(args)
        self.assertEqual(stderr.getvalue(), "")

    def test_disabled_memory_surfaces_are_described_fail_closed_in_cli_help(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        core_root = repo_root / "core"
        cases = (
            (("memory-ledger", "--help"), ("frozen read-only", "no files are regenerated", "no changes are applied")),
            (("memory-approval", "--help"), ("controlled memory writer is disabled", "fail-closed refusal")),
            (("decision-queue", "--help"), ("local-only user decisions", "fail-closed refusal")),
        )
        for arguments, expected_phrases in cases:
            result = subprocess.run(
                [sys.executable, "-m", "activity_mvp", *arguments],
                cwd=core_root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            help_text = result.stdout.lower()
            for phrase in expected_phrases:
                self.assertIn(phrase, help_text)

    def test_launch_agent_uses_canonical_ascii_runtime_and_links_only_non_ascii_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            ascii_root = home / "Library" / "Application Support" / "PSI" / "core"
            ascii_root.mkdir(parents=True)

            with patch("activity_mvp.daemon.Path.home", return_value=home):
                self.assertEqual(_ensure_ascii_runtime_link(ascii_root), ascii_root.resolve())
                self.assertFalse((home / ".personal-ai-monitor").exists())

                non_ascii_root = home / "临时监控目录"
                non_ascii_root.mkdir()
                runtime_link = _ensure_ascii_runtime_link(non_ascii_root)

            self.assertEqual(runtime_link, home / ".personal-ai-monitor")
            self.assertTrue(runtime_link.is_symlink())
            self.assertEqual(runtime_link.resolve(), non_ascii_root.resolve())

    def test_connector_hides_internal_exception_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            handler = object.__new__(AIConnectorHandler)
            handler.path = "/status"
            handler.database_path = str(Path(tmp) / "activity.sqlite")
            handler.bind_host = "127.0.0.1"
            handler.bind_port = 8766
            handler._authorized = lambda: True
            responses: list[tuple[dict, HTTPStatus]] = []
            handler._send_json = lambda payload, status=HTTPStatus.OK: responses.append((payload, status))
            handler.log_error = lambda *args, **kwargs: None

            with patch(
                "activity_mvp.ai_connector.ai_connector_status",
                side_effect=RuntimeError("/private/example/activity.sqlite is locked"),
            ):
                handler.do_GET()

        self.assertEqual(len(responses), 1)
        payload, status = responses[0]
        self.assertEqual(status, HTTPStatus.INTERNAL_SERVER_ERROR)
        self.assertEqual(payload["error"], "request_failed")
        self.assertNotIn("/private/example", str(payload))
        self.assertNotIn("sqlite", str(payload).lower())

    def test_connector_hides_database_open_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            handler = object.__new__(AIConnectorHandler)
            handler.path = "/status"
            handler.database_path = str(Path(tmp) / "private" / "activity.sqlite")
            handler.bind_host = "127.0.0.1"
            handler.bind_port = 8766
            handler._authorized = lambda: True
            responses: list[tuple[dict, HTTPStatus]] = []
            handler._send_json = lambda payload, status=HTTPStatus.OK: responses.append((payload, status))
            handler.log_error = lambda *args, **kwargs: None

            with patch(
                "activity_mvp.ai_connector.ensure_ready",
                side_effect=RuntimeError("/private/example/activity.sqlite could not be opened"),
            ):
                handler.do_GET()

        payload, status = responses[0]
        self.assertEqual(status, HTTPStatus.INTERNAL_SERVER_ERROR)
        self.assertEqual(payload["error"], "request_failed")
        self.assertNotIn("/private/example", str(payload))
        self.assertNotIn("sqlite", str(payload).lower())

    def test_quick_status_is_content_free_and_read_only(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_schema(conn)
        before = conn.total_changes

        payload = get_quick_status(conn)

        self.assertEqual(conn.total_changes, before)
        self.assertEqual(payload["interface"], "quick_status")
        self.assertFalse(payload["raw_access"])
        serialized = str(payload).lower()
        for forbidden in ("ocr_text", "window_title", "cleaned_content", "url", "file_path"):
            self.assertNotIn(forbidden, serialized)
        self.assertNotIn("coverage_health", payload)

    def test_partial_status_fallbacks_never_authorize_capture(self) -> None:
        payloads = (
            _core_status_busy_status(),
            _core_quick_status_busy_status(),
            _core_quick_status_unavailable_status("database_unavailable"),
        )
        for payload in payloads:
            self.assertFalse(payload["capture_allowed_by_session"])
            self.assertFalse(payload["raw_access"])

    def test_core_busy_status_uses_unknown_nulls_and_timestamp(self) -> None:
        before = datetime.now(timezone.utc)
        payload = _core_status_busy_status()
        after = datetime.now(timezone.utc)

        self.assertEqual(
            set(payload),
            {
                "status",
                "reason_code",
                "kind",
                "interface",
                "observed_at",
                "last_successful_read_at",
                "capture_allowed_by_session",
                "actions_enabled",
                "raw_access",
            },
        )
        self.assertEqual(payload["status"], "unknown")
        self.assertEqual(payload["reason_code"], "database_busy")
        self.assertEqual(payload["kind"], "warning")
        self.assertEqual(payload["interface"], "full_status")
        self.assertFalse(payload["capture_allowed_by_session"])
        self.assertFalse(payload["actions_enabled"])
        self.assertFalse(payload["raw_access"])
        self.assertIsNone(payload["last_successful_read_at"])
        observed = datetime.fromisoformat(payload["observed_at"])
        self.assertIsNotNone(observed.tzinfo)
        self.assertEqual(observed.utcoffset(), timezone.utc.utcoffset(observed))
        self.assertLessEqual((before - observed).total_seconds(), 2.0)
        self.assertLessEqual((observed - after).total_seconds(), 2.0)

        unreadable_fields = {
            "service",
            "service_status",
            "launch_agent_loaded",
            "launch_agent_running",
            "launch_agent_pid",
            "launch_agent_last_exit_code",
            "today_activity_count",
            "today_privacy_count",
            "today_gap_count",
            "raw_residue_count",
            "dashboard_health",
            "capture_freshness",
            "coverage_health",
            "privacy_health",
            "quality_health",
            "structured_signal_health",
            "vlm_health",
            "document_changes",
            "current_activity_session",
            "today_activity_sessions",
            "capture_interval_seconds",
            "verified_context_refs",
            "trusted_context_refs",
            "bundle_hash",
            "context_refs",
        }
        for field in unreadable_fields:
            self.assertTrue(field not in payload or payload[field] is None, field)

        quick = _core_quick_status_busy_status()
        self.assertEqual(
            set(quick),
            {
                "status",
                "reason_code",
                "kind",
                "interface",
                "observed_at",
                "last_successful_read_at",
                "attention_codes",
                "capture_allowed_by_session",
                "actions_enabled",
                "raw_access",
            },
        )
        self.assertEqual(quick["status"], "unknown")
        self.assertEqual(quick["reason_code"], "database_busy")
        self.assertEqual(quick["kind"], "warning")
        self.assertEqual(quick["interface"], "quick_status")
        self.assertEqual(quick["attention_codes"], ["database_busy"])
        self.assertIsNone(quick["last_successful_read_at"])
        self.assertFalse(quick["capture_allowed_by_session"])
        self.assertFalse(quick["actions_enabled"])
        self.assertFalse(quick["raw_access"])
        datetime.fromisoformat(quick["observed_at"])

    def test_core_current_status_stamps_interface_and_utc_time(self) -> None:
        conn = sqlite3.connect(":memory:")
        try:
            before = datetime.now(timezone.utc)
            with patch(
                "activity_mvp.__main__.get_status",
                return_value={"status": "ready", "raw_access": False},
            ):
                payload = _core_status_current_status(conn, Path("/tmp/pam-fixture"))
            after = datetime.now(timezone.utc)
        finally:
            conn.close()

        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["interface"], "full_status")
        checked = datetime.fromisoformat(payload["checked_at"])
        self.assertIsNotNone(checked.tzinfo)
        self.assertEqual(checked.utcoffset(), timezone.utc.utcoffset(checked))
        self.assertLessEqual((before - checked).total_seconds(), 2.0)
        self.assertLessEqual((checked - after).total_seconds(), 2.0)

    def test_database_busy_status_returns_unknown_with_observed_at(self) -> None:
        def assert_common(payload: dict, before: datetime, after: datetime) -> None:
            self.assertEqual(payload["status"], "unknown")
            self.assertEqual(payload["reason_code"], "database_busy")
            self.assertFalse(payload["capture_allowed_by_session"])
            self.assertFalse(payload["actions_enabled"])
            self.assertFalse(payload["raw_access"])
            self.assertIsNone(payload["last_successful_read_at"])
            observed = datetime.fromisoformat(payload["observed_at"])
            self.assertIsNotNone(observed.tzinfo)
            self.assertEqual(observed.utcoffset(), timezone.utc.utcoffset(observed))
            self.assertLessEqual((before - observed).total_seconds(), 2.0)
            self.assertLessEqual((observed - after).total_seconds(), 2.0)

        conn = sqlite3.connect(":memory:")
        try:
            before_open_connection_fallback = datetime.now(timezone.utc)
            open_connection_payload = _run_busy_status_command(
                conn,
                lambda: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")),
                _core_status_busy_status,
            )
            after_open_connection_fallback = datetime.now(timezone.utc)
        finally:
            conn.close()
        assert_common(
            open_connection_payload,
            before_open_connection_fallback,
            after_open_connection_fallback,
        )

        before_connect_fallback = datetime.now(timezone.utc)
        connect_payload = _connect_busy_status(
            SimpleNamespace(command="status", quick=False)
        )
        after_connect_fallback = datetime.now(timezone.utc)
        assert_common(connect_payload, before_connect_fallback, after_connect_fallback)
        self.assertEqual(set(open_connection_payload), set(connect_payload))

    def test_quick_busy_status_exact_closed_contract(self) -> None:
        before = datetime.now(timezone.utc)
        payload = _core_quick_status_busy_status()
        after = datetime.now(timezone.utc)

        self.assertEqual(
            set(payload),
            {
                "status",
                "reason_code",
                "kind",
                "interface",
                "observed_at",
                "last_successful_read_at",
                "attention_codes",
                "capture_allowed_by_session",
                "actions_enabled",
                "raw_access",
            },
        )
        self.assertEqual(payload["status"], "unknown")
        self.assertEqual(payload["reason_code"], "database_busy")
        self.assertEqual(payload["kind"], "warning")
        self.assertEqual(payload["interface"], "quick_status")
        self.assertEqual(payload["attention_codes"], ["database_busy"])
        self.assertIsNone(payload["last_successful_read_at"])
        self.assertIs(payload["capture_allowed_by_session"], False)
        self.assertIs(payload["actions_enabled"], False)
        self.assertIs(payload["raw_access"], False)
        observed = datetime.fromisoformat(payload["observed_at"])
        self.assertEqual(observed.utcoffset(), timezone.utc.utcoffset(observed))
        self.assertLessEqual((before - observed).total_seconds(), 2.0)
        self.assertLessEqual((observed - after).total_seconds(), 2.0)

    def test_diagnostics_busy_status_never_reports_ok_or_service_true(self) -> None:
        before = datetime.now(timezone.utc)
        payload = _diagnostics_busy_status()
        after = datetime.now(timezone.utc)

        self.assertEqual(payload["status"], "unknown")
        self.assertEqual(payload["reason_code"], "database_busy")
        self.assertEqual(payload["kind"], "warning")
        self.assertEqual(payload["interface"], "diagnostics")
        self.assertIs(payload["capture_allowed_by_session"], False)
        self.assertIs(payload["actions_enabled"], False)
        for field in (
            "raw_access",
            "destructive_actions",
            "memory_write_actions",
            "rule_write_actions",
        ):
            self.assertIs(payload[field], False, field)
        self.assertEqual(payload["cloud_analysis"], "off")
        self.assertIsNone(payload["last_successful_read_at"])
        observed = datetime.fromisoformat(payload["observed_at"])
        self.assertIsNotNone(observed.tzinfo)
        self.assertEqual(observed.utcoffset(), timezone.utc.utcoffset(observed))
        self.assertLessEqual((before - observed).total_seconds(), 2.0)
        self.assertLessEqual((observed - after).total_seconds(), 2.0)

        boundary = payload["safety_boundary"]
        for field in (
            "raw_access",
            "destructive_actions",
            "memory_write_actions",
            "rule_write_actions",
            "reads_raw_sources",
        ):
            self.assertIs(boundary[field], False, f"safety_boundary.{field}")
        self.assertEqual(boundary["cloud_analysis"], "off")
        self.assertEqual(len(payload["probes"]), 1)
        self.assertEqual(payload["probes"][0]["name"], "database_busy")
        self.assertEqual(payload["probes"][0]["status"], "unknown")
        self.assertEqual(payload["probes"][0]["kind"], "warning")
        self.assertNotIn("runtime", payload)
        self.assertNotIn("actions", payload)
        self.assertNotIn("action_counts", payload)
        for field in (
            "service",
            "launch_agent_loaded",
            "launch_agent_running",
            "launch_agent_pid",
            "launch_agent_last_exit_code",
            "checked_probes",
            "skipped_probes",
            "check_scope",
        ):
            self.assertTrue(field not in payload or payload[field] is None, field)

        def walk(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    normalized = key.lower()
                    if normalized in {
                        "service",
                        "service_running",
                        "running",
                        "launch_agent_loaded",
                        "launch_agent_running",
                    }:
                        self.assertIsNot(child, True, key)
                    if normalized in {"status", "kind", "health", "health_status"}:
                        self.assertNotIn(
                            str(child).lower(),
                            {"ok", "healthy", "running", "ready", "capturing", "正常", "运行中", "已停止"},
                        )
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(payload)

    def test_status_connection_busy_timeout_is_bounded_by_interface(self) -> None:
        existing_db = Path(__file__)
        quick_status = SimpleNamespace(command="status", quick=True, db=str(existing_db))
        full_status = SimpleNamespace(command="status", quick=False, db=str(existing_db))
        diagnostics_quick = SimpleNamespace(
            command="diagnostics",
            quick=False,
            mode="quick",
            db=str(existing_db),
        )
        diagnostics_full = SimpleNamespace(
            command="diagnostics",
            quick=False,
            mode="full",
            db=str(existing_db),
        )

        readonly_connection = object()
        with patch(
            "activity_mvp.__main__.connect_readonly",
            return_value=readonly_connection,
        ) as connect_readonly_mock:
            self.assertIs(_connect_for_command(quick_status), readonly_connection)
        connect_readonly_mock.assert_called_once_with(
            str(existing_db),
            busy_timeout_seconds=0.25,
        )

        ready_connection = object()
        for args, expected_timeout in (
            (full_status, 1.0),
            (diagnostics_quick, 0.25),
            (diagnostics_full, 1.0),
        ):
            with self.subTest(command=args.command, mode=getattr(args, "mode", "")):
                with patch(
                    "activity_mvp.__main__.ensure_ready",
                    return_value=ready_connection,
                ) as ensure_ready_mock:
                    self.assertIs(_connect_for_command(args), ready_connection)
                ensure_ready_mock.assert_called_once_with(
                    str(existing_db),
                    busy_timeout_seconds=expected_timeout,
                )

    def test_cli_status_and_diagnostics_under_real_exclusive_lock(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        core_root = repo_root / "core"
        with tempfile.TemporaryDirectory() as tmp:
            database_path = Path(tmp) / "locked" / "activity.sqlite"
            database_path.parent.mkdir(parents=True)
            setup = sqlite3.connect(database_path)
            setup.row_factory = sqlite3.Row
            init_schema(setup)
            setup.commit()
            journal_mode = setup.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            self.assertEqual(str(journal_mode).lower(), "delete")
            setup.close()

            # Keep this a database-lock test, not a cold Swift compiler timing
            # test. The real helper and all eight real CLI subprocesses are
            # still used; only the helper build is moved outside their strict
            # wall-clock assertions.
            if sys.platform == "darwin":
                ensure_helper(database_path.parent, "session_state")

            locker = sqlite3.connect(database_path, timeout=1, isolation_level=None)
            try:
                locker.execute("BEGIN EXCLUSIVE")
                locker.execute(
                    "UPDATE meta SET value = value WHERE key = 'schema_version'"
                )

                probe = sqlite3.connect(database_path, timeout=0, isolation_level=None)
                try:
                    probe.execute("PRAGMA busy_timeout = 0")
                    with self.assertRaises(sqlite3.OperationalError) as raised:
                        probe.execute(
                            "SELECT value FROM meta WHERE key = 'schema_version'"
                        ).fetchone()
                    self.assertTrue(
                        "locked" in str(raised.exception).lower()
                        or "busy" in str(raised.exception).lower()
                    )
                finally:
                    probe.close()

                commands = (
                    ("quick", ["status", "--quick"]),
                    ("full", ["status"]),
                    (
                        "diagnostics_quick",
                        ["diagnostics", "status", "--mode", "quick"],
                    ),
                    (
                        "diagnostics_full",
                        ["diagnostics", "status", "--mode", "full"],
                    ),
                )
                for label, command in commands:
                    started = time.monotonic()
                    result = subprocess.run(
                        [sys.executable, "-m", "activity_mvp", "--db", str(database_path), *command],
                        cwd=core_root,
                        capture_output=True,
                        text=True,
                        timeout=8,
                    )
                    elapsed = time.monotonic() - started
                    # Keep the original wall-clock assertion, but retain the
                    # already-returned result if it fails before payload checks.
                    diagnostic = {
                        "command": label,
                        "elapsed_seconds": elapsed,
                        "returncode": result.returncode,
                        "stderr_present": bool(result.stderr),
                    }
                    try:
                        observed_payload = json.loads(result.stdout)
                        diagnostic["payload_is_object"] = isinstance(observed_payload, dict)
                        if isinstance(observed_payload, dict):
                            diagnostic["status_is_unknown"] = observed_payload.get("status") == "unknown"
                            diagnostic["reason_is_database_busy"] = observed_payload.get("reason_code") == "database_busy"
                    except (ValueError, TypeError):
                        diagnostic["payload_is_json"] = False
                    self.assertLessEqual(
                        elapsed,
                        2.0 if label == "quick" else 7.0,
                        json.dumps(diagnostic, sort_keys=True),
                    )
                    self.assertEqual(result.returncode, 0, f"{label}: {result.stderr}")
                    self.assertEqual(result.stderr, "", label)
                    stdout = result.stdout.strip()
                    self.assertTrue(stdout.startswith("{") and stdout.endswith("}"), label)
                    payload = json.loads(stdout)
                    self.assertIsInstance(payload, dict)
                    self.assertEqual(payload["status"], "unknown", label)
                    self.assertEqual(payload["reason_code"], "database_busy", label)
                    self.assertIs(payload["capture_allowed_by_session"], False, label)
                    self.assertIs(payload["actions_enabled"], False, label)
                    self.assertIs(payload["raw_access"], False, label)
                    observed = datetime.fromisoformat(payload["observed_at"])
                    self.assertEqual(
                        observed.utcoffset(),
                        timezone.utc.utcoffset(observed),
                        label,
                    )
                    for forbidden in (
                        "Traceback",
                        str(database_path),
                        str(core_root),
                        "database is locked",
                        "SELECT value FROM meta",
                        "UPDATE meta SET",
                    ):
                        self.assertNotIn(forbidden, result.stdout, label)
                        self.assertNotIn(forbidden, result.stderr, label)

                    if label == "quick":
                        self.assertEqual(
                            set(payload),
                            {
                                "status",
                                "reason_code",
                                "kind",
                                "interface",
                                "observed_at",
                                "last_successful_read_at",
                                "attention_codes",
                                "capture_allowed_by_session",
                                "actions_enabled",
                                "raw_access",
                            },
                        )
                    elif label == "full":
                        for field in (
                            "launch_agent_loaded",
                            "launch_agent_running",
                            "launch_agent_pid",
                            "launch_agent_last_exit_code",
                            "today_activity_count",
                            "today_privacy_count",
                            "today_gap_count",
                            "raw_residue_count",
                            "dashboard_health",
                            "document_changes",
                            "current_activity_session",
                            "verified_context_refs",
                            "trusted_context_refs",
                            "bundle_ref",
                            "bundle_hash",
                        ):
                            self.assertTrue(
                                field not in payload or payload[field] is None,
                                f"{label}: {field}",
                            )
                    else:
                        boundary = payload["safety_boundary"]
                        for field in (
                            "raw_access",
                            "destructive_actions",
                            "memory_write_actions",
                            "rule_write_actions",
                            "reads_raw_sources",
                        ):
                            self.assertIs(boundary[field], False, f"{label}: {field}")
                        self.assertEqual(boundary["cloud_analysis"], "off")
                        self.assertEqual(len(payload["probes"]), 1)
                        self.assertEqual(payload["probes"][0]["name"], "database_busy")
                        self.assertNotIn("actions", payload)
                        self.assertNotIn("action_counts", payload)
            finally:
                locker.rollback()
                locker.close()

            for label, command in commands:
                result = subprocess.run(
                    [sys.executable, "-m", "activity_mvp", "--db", str(database_path), *command],
                    cwd=core_root,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0, f"recovery {label}: {result.stderr}")
                self.assertEqual(result.stderr, "", f"recovery {label}")
                payload = json.loads(result.stdout)
                self.assertIs(payload.get("raw_access"), False, f"recovery {label}")
                self.assertNotIn("database_busy", payload.get("attention_codes") or [], label)
                self.assertNotEqual(payload.get("reason_code"), "database_busy", label)
                if label == "full":
                    self.assertIn(
                        payload.get("status"),
                        {
                            "capturing",
                            "ready",
                            "running",
                            "paused",
                            "locked",
                            "session_state_unknown",
                            "stopped",
                            "error",
                        },
                    )
                    self.assertEqual(payload.get("interface"), "full_status")
                    recovered_at = datetime.fromisoformat(payload["checked_at"])
                    self.assertEqual(
                        recovered_at.utcoffset(),
                        timezone.utc.utcoffset(recovered_at),
                    )
                elif label == "quick":
                    self.assertIn(
                        payload.get("status"),
                        {
                            "capturing",
                            "ready",
                            "running",
                            "paused",
                            "locked",
                            "session_state_unknown",
                            "stopped",
                            "error",
                        },
                    )
                    self.assertEqual(payload.get("interface"), "quick_status")
                else:
                    self.assertIn(payload.get("status"), {"ok", "partial", "attention_required"})
                    self.assertEqual(payload.get("interface"), "diagnostics")
                    self.assertEqual(
                        payload.get("mode"),
                        "quick" if label == "diagnostics_quick" else "full",
                    )

    def test_quick_status_does_not_export_untrusted_display_identifiers(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_schema(conn)
        record_capture_coverage(
            conn,
            "/private/user/Secret Window Title",
            "2026-07-13T10:00:00+00:00",
            "keyframe",
            frames_sampled=1,
            frames_dropped=0,
            gap=0,
        )
        conn.commit()

        serialized = json.dumps(get_quick_status(conn), ensure_ascii=False)
        self.assertNotIn("/private/user", serialized)
        self.assertNotIn("Secret Window", serialized)

    def test_quick_cli_reports_missing_and_corrupt_databases_without_paths(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        core_root = repo_root / "core"
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "private" / "missing.sqlite"
            missing_result = self._run_quick_cli(core_root, missing)
            self.assertEqual(missing_result.returncode, 0, missing_result.stderr)
            missing_payload = json.loads(missing_result.stdout)
            self.assertEqual(missing_payload["status"], "unavailable")
            self.assertEqual(missing_payload["attention_codes"], ["database_missing"])

            corrupt = Path(tmp) / "secret-client.sqlite"
            corrupt.write_bytes(b"not a sqlite database\n/private/customer")
            corrupt_result = self._run_quick_cli(core_root, corrupt)
            self.assertEqual(corrupt_result.returncode, 0, corrupt_result.stderr)
            corrupt_payload = json.loads(corrupt_result.stdout)
            self.assertEqual(corrupt_payload["status"], "unavailable")
            self.assertFalse(corrupt_payload["raw_access"])
            self.assertNotIn(str(corrupt), corrupt_result.stdout)

    @staticmethod
    def _run_quick_cli(core_root: Path, database_path: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "activity_mvp", "--db", str(database_path), "status", "--quick"],
            cwd=core_root,
            capture_output=True,
            text=True,
        )

    def test_canonical_obsidian_root_uses_current_icloud_vault(self) -> None:
        root = obsidian_vault_root(home="/home/example")
        self.assertEqual(
            str(root),
            "/home/example/Library/Mobile Documents/iCloud~md~obsidian/Documents/默认仓库",
        )

    def test_backend_capture_evidence_is_authoritative_for_permission_state(self) -> None:
        denied = _screen_recording_permission_status(
            last_error="screen_recording_permission_denied",
            last_attempt_at="2026-07-13T10:01:00+00:00",
            last_success_at="",
            launch_agent_running=True,
            capture_freshness={"is_stale": True},
        )
        granted = _screen_recording_permission_status(
            last_error="",
            last_attempt_at="2026-07-13T10:00:00+00:00",
            last_success_at="2026-07-13T10:00:00+00:00",
            launch_agent_running=True,
            capture_freshness={"is_stale": False, "last_capture_age_seconds": 10, "stale_threshold_seconds": 90},
        )
        unknown = _screen_recording_permission_status(
            last_error="",
            last_attempt_at="2026-07-13T10:01:00+00:00",
            last_success_at="2026-07-01T10:00:00+00:00",
            launch_agent_running=True,
            capture_freshness={"is_stale": True, "last_capture_age_seconds": 1000, "stale_threshold_seconds": 90},
        )

        self.assertEqual(denied["state"], "denied")
        self.assertEqual(granted["state"], "granted_observed")
        self.assertEqual(unknown["state"], "unknown")
        self.assertTrue(denied["backend_authoritative"])
        generic_permission_error = _screen_recording_permission_status(
            last_error="file permission denied: /private/path",
            last_attempt_at="2026-07-13T10:01:00+00:00",
            last_success_at="",
            launch_agent_running=True,
            capture_freshness={"is_stale": True},
        )
        self.assertEqual(generic_permission_error["state"], "unknown")
        degraded = _screen_recording_permission_status(
            last_error="capture_batch_failed:1_display(s)",
            last_attempt_at="2026-07-13T10:01:00+00:00",
            last_success_at="2026-07-13T10:00:00+00:00",
            launch_agent_running=True,
            capture_freshness={"is_stale": False, "last_capture_age_seconds": 10, "stale_threshold_seconds": 90},
        )
        self.assertEqual(degraded["state"], "unknown")
        stopped_historical = _screen_recording_permission_status(
            last_error="",
            last_attempt_at="2020-01-01T00:00:00+00:00",
            last_success_at="2020-01-01T00:00:00+00:00",
            launch_agent_running=False,
            capture_freshness={"is_stale": False, "last_capture_age_seconds": 10, "stale_threshold_seconds": 90},
        )
        self.assertEqual(stopped_historical["state"], "unknown")
        mixed_timezone_history = _screen_recording_permission_status(
            last_error="",
            last_attempt_at="2026-07-13T10:00:00",
            last_success_at="2026-07-13T10:00:00+00:00",
            launch_agent_running=True,
            capture_freshness={"is_stale": False, "last_capture_age_seconds": 10, "stale_threshold_seconds": 90},
        )
        self.assertEqual(mixed_timezone_history["state"], "unknown")
        self.assertEqual(
            _capture_batch_last_error([{"error": "CGDisplayStream permission denied for /private/path"}]),
            "screen_recording_permission_denied",
        )
        self.assertEqual(
            _capture_batch_last_error([{"error": "output directory permission denied for /private/path"}]),
            "capture_batch_failed:1_display(s)",
        )

    def test_runtime_core_hash_is_deterministic_and_detects_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "activity_mvp").mkdir()
            (root / "tools").mkdir()
            (root / "browser_extension").mkdir()
            module = root / "activity_mvp" / "sample.py"
            module.write_text("VALUE = 1\n", encoding="utf-8")
            (root / "tools" / "sample.swift").write_text("let value = 1\n", encoding="utf-8")
            extension = root / "browser_extension" / "manifest.json"
            extension.write_text('{"version":"0.4.0"}\n', encoding="utf-8")
            (root / "pyproject.toml").write_text("[project]\nname = 'sample'\n", encoding="utf-8")

            first = _runtime_core_code_hash(root)
            second = _runtime_core_code_hash(root)
            extension.write_text('{"version":"0.4.1"}\n', encoding="utf-8")
            changed_resource = _runtime_core_code_hash(root)
            (root / "activity_mvp" / "fixture.json").write_text('{"changed":true}\n', encoding="utf-8")
            changed_json = _runtime_core_code_hash(root)

        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        self.assertNotEqual(first, changed_resource)
        self.assertNotEqual(changed_resource, changed_json)

    def test_runtime_core_hash_fails_closed_when_file_enumeration_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "activity_mvp").mkdir()
            (root / "tools").mkdir()
            with patch.object(Path, "rglob", side_effect=OSError("/private/path is unreadable")):
                result = _runtime_core_code_hash(root)
        self.assertEqual(result, "unknown")


if __name__ == "__main__":
    unittest.main()
