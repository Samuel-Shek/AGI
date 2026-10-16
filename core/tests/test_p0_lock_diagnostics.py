from __future__ import annotations

import json
import os
import plistlib
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from activity_mvp.daemon import (
    _dashboard_health,
    _start_dashboard_thread,
    ensure_daemon_defaults,
    get_quick_status,
    get_status,
    install_launch_agent,
    run_once,
    session_state,
)
from activity_mvp.diagnostics import diagnostics_status
from activity_mvp.pipeline import record_capture_coverage, utc_now
from activity_mvp.storage import ensure_ready


class P0LockAndDiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.db_path = self.root / "activity.sqlite"
        self.conn = ensure_ready(self.db_path)
        ensure_daemon_defaults(self.conn)
        from activity_mvp.capture_lease import configure_capture_lease
        configure_capture_lease(self.conn, enabled=True)
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self.tempdir.cleanup()

    def _helper_result(self, payload: object, *, returncode: int = 0) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            ["session_state"],
            returncode,
            stdout=json.dumps(payload),
            stderr="helper failed" if returncode else "",
        )

    def test_partial_capture_receipt_uses_public_error_without_overriding_safety_state(self) -> None:
        from activity_mvp.capture_lease import configure_capture_lease
        from activity_mvp.runtime_liveness import HeartbeatWriter
        running = {"loaded": True, "running": True, "pid": str(os.getpid()), "last_exit_code": ""}
        stopped = {"loaded": False, "running": False, "pid": "", "last_exit_code": ""}
        cases = [
            ("unlocked", "", True, running, "error", ""),
            ("unlocked", "manual", True, running, "paused", ""),
            ("locked", "", True, running, "locked", ""),
            ("unknown", "", True, running, "session_state_unknown", ""),
            ("unlocked", "", False, running, "paused", ""),
            ("unlocked", "", True, stopped, "stopped", "service_stopped"),
        ]
        for session, pause_mode, enabled, service, expected, reason in cases:
            with self.subTest(session=session, pause=pause_mode, enabled=enabled, service=service):
                configure_capture_lease(self.conn, enabled=enabled)
                for key, value in (("daemon_status", "partial"), ("last_error", "fixture_capture_batch_error"),
                                   ("pause_mode", pause_mode), ("pause_until", ""),
                                   ("session_state", session)):
                    self.conn.execute("INSERT OR REPLACE INTO daemon_state VALUES(?,?,?)", (key, value, utc_now()))
                for worker in ("daemon", "document_worker"):
                    HeartbeatWriter(worker).touch(self.conn)
                self.conn.commit()
                with patch("activity_mvp.daemon.session_state", return_value={"state": session, "locked": session != "unlocked"}), \
                     patch("activity_mvp.daemon._launch_agent_status", return_value=service), \
                     patch("activity_mvp.daemon._quick_launch_agent_status", return_value=service):
                    full = get_status(self.conn, self.root)
                    quick = get_quick_status(self.conn)
                for payload in (full, quick):
                    self.assertEqual(payload["status"], expected)
                    self.assertEqual(payload["reason_code"], reason)
                    self.assertIs(payload["capture_allowed_by_session"], enabled and session == "unlocked")
                self.assertEqual(full["last_error"], "fixture_capture_batch_error")
                self.assertIs(quick["last_error_present"], True)
                if enabled:
                    self.assertIn("last_error", quick["attention_codes"])
                self.assertEqual(self.conn.execute("SELECT value FROM daemon_state WHERE key='daemon_status'").fetchone()[0], "partial")
                self.assertEqual(self.conn.execute("SELECT value FROM daemon_state WHERE key='last_error'").fetchone()[0], "fixture_capture_batch_error")

    def test_session_state_only_allows_explicit_unlocked_console_session(self) -> None:
        cases = [
            ({"state": "unlocked", "locked": False, "on_console": True}, "unlocked", True),
            (
                {
                    "state": "unlocked",
                    "locked": False,
                    "on_console": True,
                    "evidence_source": "session_state_helper_io_registry",
                },
                "unlocked",
                True,
            ),
            ({"state": "locked", "locked": True, "on_console": True}, "locked", False),
            ({"state": "unknown", "locked": True, "on_console": False}, "unknown", False),
            ({"locked": False}, "unknown", False),
            ({"locked": False, "on_console": False}, "unknown", False),
        ]
        for payload, expected_state, allowed in cases:
            with self.subTest(payload=payload), patch(
                "activity_mvp.daemon.helper_command", return_value=["session_state"]
            ), patch("activity_mvp.daemon.subprocess.run", return_value=self._helper_result(payload)):
                result = session_state(self.root)
            self.assertEqual(result["state"], expected_state)
            self.assertEqual(result["capture_allowed"], allowed)
            self.assertEqual(result["locked"], not allowed)
        self.assertEqual(result["source"], "session_state_helper")

        with patch("activity_mvp.daemon.helper_command", return_value=["session_state"]), patch(
            "activity_mvp.daemon.subprocess.run",
            return_value=self._helper_result(
                {
                    "state": "unlocked",
                    "locked": False,
                    "on_console": True,
                    "evidence_source": "session_state_helper_io_registry",
                }
            ),
        ):
            fallback = session_state(self.root)
        self.assertEqual(fallback["source"], "session_state_helper_io_registry")
        self.assertEqual(fallback["reason"], "explicit_unlocked_console_io_registry")
        self.assertNotIn("user_id", fallback)

    def test_session_state_failures_and_invalid_json_fail_closed(self) -> None:
        outcomes = [
            subprocess.TimeoutExpired(["session_state"], 5),
            OSError("helper unavailable"),
            subprocess.CompletedProcess(["session_state"], 1, stdout="", stderr="private detail"),
            subprocess.CompletedProcess(["session_state"], 0, stdout="not-json", stderr=""),
        ]
        for outcome in outcomes:
            with self.subTest(outcome=type(outcome).__name__), patch(
                "activity_mvp.daemon.helper_command", return_value=["session_state"]
            ), patch("activity_mvp.daemon.subprocess.run", side_effect=outcome if isinstance(outcome, Exception) else None,
                     return_value=None if isinstance(outcome, Exception) else outcome):
                result = session_state(self.root)
            self.assertEqual(result["state"], "unknown")
            self.assertTrue(result["locked"])
            self.assertFalse(result["capture_allowed"])
            self.assertNotIn("private detail", json.dumps(result))

    def test_unknown_session_blocks_capture_before_helper_is_called(self) -> None:
        with patch(
            "activity_mvp.daemon.session_state",
            return_value={"state": "unknown", "locked": True, "error": "session_state_timeout"},
        ), patch("activity_mvp.daemon.capture_sck_keyframe_batch") as capture:
            result = run_once(self.db_path)

        capture.assert_not_called()
        self.assertEqual(result["status"], "session_state_unknown")
        self.assertEqual(result["captured"], 0)

    def test_partial_display_failure_keeps_last_success_and_surfaces_attention(self) -> None:
        previous_success = "2026-07-13T00:00:00+00:00"
        now = utc_now()
        for key, value in (("last_capture_at", previous_success), ("last_success_at", previous_success)):
            self.conn.execute(
                "UPDATE daemon_state SET value = ?, updated_at = ? WHERE key = ?",
                (value, now, key),
            )
        self.conn.commit()
        batch = {
            "events": [],
            "skipped": [
                {
                    "captured_at": now,
                    "display_id": "display-ok",
                    "frame_hash": "same-frame",
                    "reason": "duplicate_frame",
                }
            ],
            "errors": [
                {
                    "captured_at": now,
                    "display_id": "display-failed",
                    "reason": "sck_capture_failed",
                }
            ],
        }
        unlocked = {"state": "unlocked", "locked": False, "on_console": True}
        with patch("activity_mvp.daemon.session_state", return_value=unlocked), patch(
            "activity_mvp.daemon.capture_sck_keyframe_batch", return_value=batch
        ):
            result = run_once(self.db_path)

        self.assertEqual(result["status"], "partial")
        state = {
            row["key"]: row["value"]
            for row in self.conn.execute(
                "SELECT key, value FROM daemon_state WHERE key IN "
                "('last_attempt_at', 'last_success_at', 'last_capture_at', 'last_error', 'daemon_status')"
            )
        }
        self.assertTrue(state["last_attempt_at"])
        self.assertEqual(state["last_success_at"], previous_success)
        self.assertEqual(state["last_capture_at"], previous_success)
        self.assertEqual(state["last_error"], "capture_batch_failed:1_display(s)")
        self.assertEqual(state["daemon_status"], "partial")

        with patch("activity_mvp.daemon.session_state", return_value=unlocked), patch(
            "activity_mvp.daemon._launch_agent_status",
            return_value={"loaded": True, "running": True, "pid": "123", "last_exit_code": ""},
        ), patch(
            "activity_mvp.daemon._dashboard_listener",
            return_value={"listening": False, "pid": "", "command": ""},
        ):
            status_payload = get_status(self.conn, self.root)
        codes = {item["code"] for item in status_payload["attention_items"]}
        self.assertIn("display_coverage_gap", codes)
        failed = next(
            item for item in status_payload["coverage_health"]["displays"] if item["display_id"] == "display-failed"
        )
        self.assertEqual(failed["status"], "attention")
        self.assertTrue(failed["last_attempt_at"])
        self.assertEqual(failed["last_success_at"], "")

    def test_quick_diagnostics_reports_partial_and_unknown_counts(self) -> None:
        report = diagnostics_status(self.conn, self.root, self.db_path, mode="quick")
        probes = {probe["name"]: probe for probe in report["probes"]}

        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["kind"], "partial")
        self.assertFalse(report["check_scope"]["complete"])
        self.assertIn("privacy_policy", report["checked_probes"])
        self.assertIn("hygiene", report["skipped_probes"])
        self.assertIsNone(probes["core_status"]["counts"]["raw_residue"])
        self.assertIsNone(probes["core_status"]["counts"]["attention_items"])
        self.assertNotIn("都在安全边界内", report["summary"])
        self.assertIsNone(report["runtime"]["launch_agent_running"])

    def test_get_quick_status_is_read_only_and_content_free(self) -> None:
        now = utc_now()
        for key, value in (
            ("daemon_status", "running"),
            ("session_state", "unlocked"),
            ("locked", "false"),
            ("last_attempt_at", now),
            ("last_success_at", now),
            ("last_capture_at", now),
        ):
            self.conn.execute("UPDATE daemon_state SET value = ?, updated_at = ? WHERE key = ?", (value, now, key))
        record_capture_coverage(
            self.conn,
            "display-main",
            now,
            "keyframe",
            frames_sampled=1,
            frames_dropped=0,
            gap=0,
        )
        self.conn.commit()
        before = list(self.conn.execute("SELECT key, value, updated_at FROM daemon_state ORDER BY key"))
        self.conn.execute("PRAGMA query_only = ON")
        payload = get_quick_status(self.conn)
        self.conn.execute("PRAGMA query_only = OFF")
        after = list(self.conn.execute("SELECT key, value, updated_at FROM daemon_state ORDER BY key"))

        self.assertEqual([tuple(row) for row in before], [tuple(row) for row in after])
        self.assertEqual(payload["interface"], "quick_status")
        self.assertEqual(payload["session_state"], "unlocked")
        self.assertTrue(payload["capture_allowed_by_session"])
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(str(self.root), encoded)
        self.assertNotIn("last_error\"", encoded)
        self.assertNotIn("display-main", encoded)
        self.assertNotIn("coverage_health", payload)

    def test_launch_agent_uses_fixed_python_arguments_and_private_logs(self) -> None:
        home = self.root / "home"
        home.mkdir()
        launchctl_result = {"returncode": 0, "stdout": "", "stderr": ""}

        def install() -> dict:
            with patch("activity_mvp.daemon.Path.home", return_value=home), patch(
                "activity_mvp.daemon.ensure_all_helpers", return_value=[]
            ), patch("activity_mvp.daemon._ensure_ascii_runtime_link", return_value=self.root), patch(
                "activity_mvp.daemon._launchctl", return_value=launchctl_result
            ):
                return install_launch_agent(
                    self.db_path,
                    preserve_privacy_profile=True,
                )

        first = install()
        plist_path = Path(first["plist"])
        plist = plistlib.loads(plist_path.read_bytes())
        self.assertEqual(plist["ProgramArguments"][0], os.sys.executable)
        self.assertEqual(plist["ProgramArguments"][1:3], ["-I", "-S"])
        self.assertEqual(
            plist["ProgramArguments"][3],
            str(self.root / "pam_isolated_launcher.py"),
        )
        self.assertNotIn("/bin/zsh", plist["ProgramArguments"])
        self.assertNotIn("-lc", plist["ProgramArguments"])
        self.assertNotIn("PYTHONPATH", plist["EnvironmentVariables"])
        self.assertFalse(any(key.startswith("PAM_VLM") for key in plist["EnvironmentVariables"]))
        self.assertEqual(stat.S_IMODE(plist_path.stat().st_mode), 0o600)
        logs_dir = Path(first["logs"])
        self.assertEqual(stat.S_IMODE(logs_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((logs_dir / "daemon.out.log").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((logs_dir / "daemon.err.log").stat().st_mode), 0o600)


    def test_dashboard_foreign_listener_is_not_claimed_as_pam_health(self) -> None:
        launch = {"pid": "111"}
        foreign = {"listening": True, "pid": "222", "command": "OtherService"}
        with patch("activity_mvp.daemon._dashboard_listener", return_value=foreign):
            disabled = _dashboard_health(self.conn, launch)
        self.assertEqual(disabled["port_status"], "foreign_listener")
        self.assertTrue(disabled["foreign_listener"])
        self.assertFalse(disabled["health_in_scope"])
        self.assertIn("不计入 PAM 健康", disabled["summary"])

        now = utc_now()
        self.conn.execute(
            "UPDATE settings SET value = 'true', updated_at = ? WHERE key = 'dev_dashboard_enabled'",
            (now,),
        )
        self.conn.commit()
        with patch("activity_mvp.daemon._dashboard_listener", return_value=foreign):
            enabled = _dashboard_health(self.conn, launch)
            start = _start_dashboard_thread(self.db_path)
        self.assertEqual(enabled["status"], "port_conflict")
        self.assertTrue(enabled["port_conflict"])
        self.assertEqual(start["status"], "port_conflict")


if __name__ == "__main__":
    unittest.main()
