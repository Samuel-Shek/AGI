from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from activity_mvp import daemon
from activity_mvp.daemon import _PersistentSessionStateProvider


UNLOCKED_HELPER_PAYLOAD = {
    "state": "unlocked",
    "locked": False,
    "on_console": True,
    "evidence_source": "session_state_helper_cgs",
}


class PersistentSessionStateProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _write_helper(self, body: str) -> list[str]:
        script = self.root / "session-helper.py"
        script.write_text(textwrap.dedent(body), encoding="utf-8")
        return [sys.executable, "-u", str(script)]

    def test_one_process_serves_multiple_fresh_probes(self) -> None:
        starts = self.root / "starts.txt"
        command = self._write_helper(
            f"""
            import json
            import sys
            from pathlib import Path

            starts = Path({str(starts)!r})
            with starts.open("a", encoding="utf-8") as handle:
                handle.write("started\\n")
            for index, request_line in enumerate(sys.stdin, start=1):
                request = json.loads(request_line)
                unlocked = index % 2 == 1
                print(json.dumps({{
                    "nonce": request["nonce"],
                    "session": {{
                        "state": "unlocked" if unlocked else "locked",
                        "locked": not unlocked,
                        "on_console": True,
                        "evidence_source": "session_state_helper_cgs",
                    }},
                }}, separators=(",", ":")), flush=True)
            """
        )
        with patch("activity_mvp.daemon.helper_command", return_value=command):
            provider = _PersistentSessionStateProvider(
                self.root,
                timeout_seconds=1.0,
            )
            try:
                first = provider()
                second = provider()
                third = provider()
                close_threads = [
                    threading.Thread(target=provider.close, daemon=True)
                    for _ in range(2)
                ]
                for thread in close_threads:
                    thread.start()
                for thread in close_threads:
                    thread.join(timeout=2)
            finally:
                provider.close()

        self.assertEqual(starts.read_text(encoding="utf-8").splitlines(), ["started"])
        self.assertTrue(all(not thread.is_alive() for thread in close_threads))
        self.assertEqual(first["state"], "unlocked")
        self.assertTrue(first["capture_allowed"])
        self.assertEqual(second["state"], "locked")
        self.assertFalse(second["capture_allowed"])
        self.assertEqual(third["state"], "unlocked")
        self.assertTrue(third["capture_allowed"])
        self.assertEqual(
            set(first),
            {
                "state",
                "locked",
                "on_console",
                "capture_allowed",
                "source",
                "reason",
                "observed_at",
            },
        )

    def test_protocol_failures_fail_closed_then_respawn(self) -> None:
        for mode in (
            "timeout",
            "partial",
            "oversized",
            "eof",
            "invalid_json",
            "wrong_nonce",
            "conflicting_state",
            "exit",
        ):
            with self.subTest(mode=mode):
                starts = self.root / f"starts-{mode}.txt"
                command = self._write_helper(
                    f"""
                    import json
                    import sys
                    import time
                    from pathlib import Path

                    starts = Path({str(starts)!r})
                    try:
                        generation = int(starts.read_text(encoding="utf-8")) + 1
                    except (FileNotFoundError, ValueError):
                        generation = 1
                    starts.write_text(str(generation), encoding="utf-8")
                    mode = {mode!r}
                    if generation == 1 and mode == "exit":
                        raise SystemExit(7)
                    for request_line in sys.stdin:
                        if generation == 1:
                            if mode == "timeout":
                                time.sleep(2)
                            elif mode == "partial":
                                sys.stdout.write('{{"nonce":')
                                sys.stdout.flush()
                                time.sleep(2)
                            elif mode == "oversized":
                                print("x" * 20000, flush=True)
                                continue
                            elif mode == "eof":
                                raise SystemExit(0)
                            elif mode == "invalid_json":
                                print("not-json", flush=True)
                                continue
                        request = json.loads(request_line)
                        response_nonce = (
                            "0" * 32 if generation == 1 and mode == "wrong_nonce"
                            else request["nonce"]
                        )
                        session = {UNLOCKED_HELPER_PAYLOAD!r}
                        if generation == 1 and mode == "conflicting_state":
                            session = dict(session, locked=True)
                        print(json.dumps({{
                            "nonce": response_nonce,
                            "session": session,
                        }}, separators=(",", ":")), flush=True)
                    """
                )
                with patch("activity_mvp.daemon.helper_command", return_value=command):
                    provider = _PersistentSessionStateProvider(
                        self.root,
                        # Keep the failure well below the helper's two-second
                        # stall while leaving enough headroom for process
                        # startup during the full, CPU-heavy OCR suite.
                        timeout_seconds=1.0,
                    )
                    try:
                        failed = provider()
                        recovered = provider()
                    finally:
                        provider.close()

                self.assertEqual(failed["state"], "unknown")
                self.assertTrue(failed["locked"])
                self.assertFalse(failed["capture_allowed"])
                self.assertEqual(recovered["state"], "unlocked")
                self.assertTrue(recovered["capture_allowed"])
                self.assertEqual(starts.read_text(encoding="utf-8"), "2")

    def test_conflicting_declared_and_boolean_state_is_canonical_unknown(self) -> None:
        for state, locked in (("unlocked", True), ("locked", False)):
            with self.subTest(state=state, locked=locked):
                result = daemon._session_state_from_payload(
                    {
                        "state": state,
                        "locked": locked,
                        "on_console": True,
                        "evidence_source": "session_state_helper_cgs",
                    }
                )

                self.assertEqual(result["state"], "unknown")
                self.assertTrue(result["locked"])
                self.assertFalse(result["capture_allowed"])
                self.assertEqual(
                    result["error"],
                    "session_state_conflicting_fields",
                )

    def test_worker_injects_provider_and_closes_it_after_pipeline_join(self) -> None:
        provider = Mock()
        provider.return_value = dict(UNLOCKED_HELPER_PAYLOAD, capture_allowed=True)
        events: list[str] = []
        provider.close.side_effect = lambda: events.append("close")
        captured: dict[str, object] = {}

        def start_worker(_database, _command, **kwargs):
            events.append("start")
            captured.update(kwargs)
            self.assertIs(kwargs["session_state_provider"], provider)
            eligibility = kwargs["reconcile_eligibility_provider"]()
            self.assertTrue(eligibility["allowed"])
            return {"started": False, "reason": "test_complete"}

        def join_worker(_worker):
            events.append("join")

        worker_thread: threading.Thread | None = None
        with (
            patch.object(daemon, "_DOCUMENT_CHANGE_WORKER_THREAD", None),
            patch.object(
                daemon,
                "_DOCUMENT_CHANGE_WORKER_RETRY_NOT_BEFORE_MONOTONIC",
                0.0,
            ),
            patch.object(
                daemon,
                "_PersistentSessionStateProvider",
                return_value=provider,
            ) as provider_factory,
            patch(
                "activity_mvp.document_changes.start_document_change_worker",
                side_effect=start_worker,
            ),
            patch(
                "activity_mvp.document_changes.join_document_change_worker",
                side_effect=join_worker,
            ),
            patch(
                "activity_mvp.daemon._document_change_worker_startup_eligibility",
                return_value={"allowed": True, "raw_access": False},
            ) as eligibility,
            patch("activity_mvp.daemon.helper_command", return_value=["/safe/helper"]),
        ):
            self.assertTrue(
                daemon._maybe_start_document_change_worker(
                    self.root / "activity.sqlite"
                )
            )
            worker_thread = daemon._DOCUMENT_CHANGE_WORKER_THREAD
            self.assertIsNotNone(worker_thread)
            worker_thread.join(timeout=3)

        self.assertIsNotNone(worker_thread)
        self.assertFalse(worker_thread.is_alive())
        resolved_root = (self.root / "activity.sqlite").resolve().parent
        provider_factory.assert_called_once_with(resolved_root)
        eligibility.assert_called_once_with(
            resolved_root,
            session=provider.return_value,
        )
        self.assertEqual(events, ["start", "join", "close"])

    def test_long_worker_honors_process_restart_policy(self) -> None:
        started_at = 100.0
        finished_at = (
            started_at
            + daemon.DOCUMENT_CHANGE_WORKER_HEALTHY_RUN_SECONDS
            + 1.0
        )
        for restart_required, restart_blocked, close_fails, expected_deadline in (
            (False, False, False, finished_at),
            (
                True,
                False,
                False,
                finished_at
                + daemon.DOCUMENT_CHANGE_WORKER_RETRY_COOLDOWN_SECONDS,
            ),
            # 2026-10-05: a joined generation whose reset failed is no longer
            # stranded until the daemon restarts; the next start retries the
            # reset after the same fresh-stream cooldown.
            (
                True,
                True,
                False,
                finished_at
                + daemon.DOCUMENT_CHANGE_WORKER_RETRY_COOLDOWN_SECONDS,
            ),
            (
                True,
                True,
                True,
                finished_at
                + daemon.DOCUMENT_CHANGE_WORKER_RETRY_COOLDOWN_SECONDS,
            ),
        ):
            with self.subTest(
                restart_required=restart_required,
                restart_blocked=restart_blocked,
                close_fails=close_fails,
            ):
                provider = Mock()
                events: list[str] = []

                def close_provider() -> None:
                    events.append("close")
                    if close_fails:
                        raise RuntimeError("/private/watched/file-name")

                provider.close.side_effect = close_provider
                process = Mock(
                    restart_cooldown_required=restart_required,
                    restart_blocked=restart_blocked,
                )
                stderr = io.StringIO()

                def start_worker(_database, _command, **_kwargs):
                    events.append("start")
                    return {"started": True, "process": process}

                def join_worker(_worker):
                    events.append("join")

                worker_thread: threading.Thread | None = None
                with (
                    patch.object(daemon, "_DOCUMENT_CHANGE_WORKER_THREAD", None),
                    patch.object(
                        daemon,
                        "_DOCUMENT_CHANGE_WORKER_RETRY_NOT_BEFORE_MONOTONIC",
                        0.0,
                    ),
                    patch.object(
                        daemon,
                        "_DOCUMENT_STREAM_RESET_PENDING_REASON",
                        None,
                    ),
                    patch.object(
                        daemon,
                        "_PersistentSessionStateProvider",
                        return_value=provider,
                    ),
                    patch(
                        "activity_mvp.document_changes.start_document_change_worker",
                        side_effect=start_worker,
                    ),
                    patch(
                        "activity_mvp.document_changes.join_document_change_worker",
                        side_effect=join_worker,
                    ),
                    patch(
                        "activity_mvp.daemon.helper_command",
                        return_value=["/safe/helper"],
                    ),
                    patch(
                        "activity_mvp.daemon.time.monotonic",
                        side_effect=(started_at, finished_at, finished_at),
                    ),
                    patch("activity_mvp.daemon.sys.stderr", stderr),
                ):
                    self.assertTrue(
                        daemon._maybe_start_document_change_worker(
                            self.root / "activity.sqlite"
                        )
                    )
                    worker_thread = daemon._DOCUMENT_CHANGE_WORKER_THREAD
                    self.assertIsNotNone(worker_thread)
                    worker_thread.join(timeout=3)

                    self.assertFalse(worker_thread.is_alive())
                    self.assertEqual(
                        daemon._DOCUMENT_CHANGE_WORKER_RETRY_NOT_BEFORE_MONOTONIC,
                        expected_deadline,
                    )
                    self.assertEqual(
                        daemon._DOCUMENT_STREAM_RESET_PENDING_REASON,
                        "unknown_fatal_transition" if restart_blocked else None,
                    )

                self.assertEqual(events, ["start", "join", "close"])
                if close_fails:
                    self.assertIn(
                        "document_change_session_provider_close_failed",
                        stderr.getvalue(),
                    )
                    self.assertNotIn("/private/watched/file-name", stderr.getvalue())

    def _run_worker_lifecycle_once(
        self,
        *,
        start_result,
        join_error: BaseException | None = None,
        retry_verified: bool | None = None,
        pending_reason: str | None = None,
        failed_retries: int = 0,
    ) -> dict:
        """Run one daemon worker lifecycle with a fake worker and reset retry."""
        events: list[str] = []
        start_kwargs: dict = {}
        provider = Mock()

        def start_worker(_database, _command, **kwargs):
            events.append("start")
            start_kwargs.update(kwargs)
            return start_result

        def join_worker(_worker):
            events.append("join")
            if join_error is not None:
                raise join_error

        def retry(_db_path, reason):
            events.append(f"retry:{reason}")
            return bool(retry_verified)

        state: dict = {}
        with (
            patch.object(daemon, "_DOCUMENT_CHANGE_WORKER_THREAD", None),
            patch.object(daemon, "_DOCUMENT_CHANGE_WORKER_RETRY_NOT_BEFORE_MONOTONIC", 0.0),
            patch.object(daemon, "_DOCUMENT_STREAM_RESET_PENDING_REASON", pending_reason),
            patch.object(daemon, "_DOCUMENT_STREAM_RESET_FAILED_RETRIES", failed_retries),
            patch.object(daemon, "_PersistentSessionStateProvider", return_value=provider),
            patch("activity_mvp.document_changes.start_document_change_worker", side_effect=start_worker),
            patch("activity_mvp.document_changes.join_document_change_worker", side_effect=join_worker),
            patch("activity_mvp.daemon._retry_document_stream_reset", side_effect=retry),
            patch("activity_mvp.daemon.helper_command", return_value=["/safe/helper"]),
            patch("activity_mvp.daemon.sys.stderr", io.StringIO()),
        ):
            before = daemon.time.monotonic()
            self.assertTrue(daemon._maybe_start_document_change_worker(self.root / "activity.sqlite"))
            worker_thread = daemon._DOCUMENT_CHANGE_WORKER_THREAD
            assert worker_thread is not None
            worker_thread.join(timeout=3)
            self.assertFalse(worker_thread.is_alive())
            state.update(
                events=events,
                start_kwargs=start_kwargs,
                before=before,
                after=daemon.time.monotonic(),
                deadline=daemon._DOCUMENT_CHANGE_WORKER_RETRY_NOT_BEFORE_MONOTONIC,
                pending=daemon._DOCUMENT_STREAM_RESET_PENDING_REASON,
                failed=daemon._DOCUMENT_STREAM_RESET_FAILED_RETRIES,
            )
        return state

    def test_joined_generation_with_failed_reset_schedules_reset_retry(self) -> None:
        process = Mock(
            restart_cooldown_required=True,
            restart_blocked=True,
            stream_reset_reason="fsevents_control_event",
        )
        state = self._run_worker_lifecycle_once(
            start_result={"started": True, "process": process},
        )

        self.assertEqual(state["events"], ["start", "join"])
        self.assertEqual(state["pending"], "fsevents_control_event")
        self.assertEqual(state["failed"], 0)
        cooldown = daemon.DOCUMENT_CHANGE_WORKER_RETRY_COOLDOWN_SECONDS
        self.assertGreaterEqual(state["deadline"], state["before"] + cooldown)
        self.assertLessEqual(state["deadline"], state["after"] + cooldown)

    def test_unjoined_generation_with_failed_reset_still_never_restarts(self) -> None:
        process = Mock(
            restart_cooldown_required=True,
            restart_blocked=True,
            stream_reset_reason="fsevents_control_event",
        )
        state = self._run_worker_lifecycle_once(
            start_result={"started": True, "process": process},
            join_error=TimeoutError("document_change_worker_threads_alive:validator"),
        )

        self.assertEqual(state["deadline"], float("inf"))
        self.assertIsNone(state["pending"])

    def test_verified_reset_retry_starts_fresh_stream_and_clears_pending(self) -> None:
        process = Mock(restart_cooldown_required=False, restart_blocked=False)
        state = self._run_worker_lifecycle_once(
            start_result={"started": True, "process": process},
            retry_verified=True,
            pending_reason="python_event_queue_overflow",
            failed_retries=2,
        )

        self.assertEqual(
            state["events"],
            ["retry:python_event_queue_overflow", "start", "join"],
        )
        self.assertIs(state["start_kwargs"].get("require_fresh_stream"), True)
        self.assertIsNone(state["pending"])
        self.assertEqual(state["failed"], 0)

    def test_failed_reset_retry_backs_off_without_starting_helper(self) -> None:
        state = self._run_worker_lifecycle_once(
            start_result={"started": True, "process": Mock()},
            retry_verified=False,
            pending_reason="fsevents_control_event",
        )

        self.assertEqual(state["events"], ["retry:fsevents_control_event"])
        self.assertEqual(state["pending"], "fsevents_control_event")
        self.assertEqual(state["failed"], 1)
        backoff = daemon._document_stream_reset_retry_cooldown(1)
        self.assertEqual(backoff, 2 * daemon.DOCUMENT_CHANGE_WORKER_RETRY_COOLDOWN_SECONDS)
        self.assertGreaterEqual(state["deadline"], state["before"] + backoff)
        self.assertLessEqual(state["deadline"], state["after"] + backoff)
        self.assertEqual(
            daemon._document_stream_reset_retry_cooldown(50),
            daemon.DOCUMENT_STREAM_RESET_RETRY_MAX_COOLDOWN_SECONDS,
        )

    def test_reappeared_watermark_at_start_keeps_reset_pending(self) -> None:
        state = self._run_worker_lifecycle_once(
            start_result={
                "started": False,
                "reason": "stream_watermark_retained",
                "raw_access": False,
            },
            retry_verified=True,
            pending_reason="fsevents_control_event",
        )

        self.assertEqual(state["events"], ["retry:fsevents_control_event", "start"])
        self.assertEqual(state["pending"], "fsevents_control_event")
        self.assertEqual(state["failed"], 1)

    def test_reset_marker_from_earlier_daemon_is_retried_before_start(self) -> None:
        from activity_mvp.document_changes import mark_document_stream_reset_required

        mark_document_stream_reset_required(self.root, "fsevents_control_event")
        state = self._run_worker_lifecycle_once(
            start_result={"started": True, "process": Mock(restart_blocked=False, restart_cooldown_required=False)},
            retry_verified=True,
        )

        self.assertEqual(state["events"], ["retry:fsevents_control_event", "start", "join"])
        self.assertIs(state["start_kwargs"].get("require_fresh_stream"), True)
        self.assertIsNone(state["pending"])

    def test_zero_idle_threshold_skips_redundant_system_probe(self) -> None:
        with patch("activity_mvp.daemon.system_idle_seconds") as idle_probe:
            result = daemon._document_change_worker_startup_eligibility(
                self.root,
                session=dict(UNLOCKED_HELPER_PAYLOAD, capture_allowed=True),
                power={"external_power": True, "low_power_mode": False},
            )

        idle_probe.assert_not_called()
        self.assertTrue(result["allowed"])
        self.assertEqual(result["idle_seconds"], 0)
        self.assertEqual(
            set(result),
            {"allowed", "reason", "idle_seconds", "min_idle_seconds", "raw_access"},
        )

    def test_document_worker_accepts_only_canonical_locked_local_console(self) -> None:
        locked = daemon._document_change_worker_startup_eligibility(
            self.root,
            session={
                "state": "locked",
                "locked": True,
                "on_console": True,
                "capture_allowed": False,
            },
            power={"external_power": True, "low_power_mode": False},
        )
        off_console = daemon._document_change_worker_startup_eligibility(
            self.root,
            session={
                "state": "locked",
                "locked": True,
                "on_console": False,
                "capture_allowed": False,
            },
            power={"external_power": True, "low_power_mode": False},
        )
        unknown = daemon._document_change_worker_startup_eligibility(
            self.root,
            session={
                "state": "unknown",
                "locked": True,
                "on_console": True,
                "capture_allowed": False,
            },
            power={"external_power": True, "low_power_mode": False},
        )

        self.assertTrue(locked["allowed"])
        self.assertFalse(off_console["allowed"])
        self.assertEqual(off_console["reason"], "session_not_eligible")
        self.assertFalse(unknown["allowed"])
        self.assertEqual(unknown["reason"], "session_not_eligible")




if __name__ == "__main__":
    unittest.main()
