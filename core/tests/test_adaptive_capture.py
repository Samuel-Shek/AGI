from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from activity_mvp.adaptive_capture import (
    AdaptiveCaptureState,
    advance_capture_state,
    capture_observation,
    hid_input_detected,
    initial_capture_state,
    observation_payload,
)
from activity_mvp.daemon import (
    _advance_adaptive_capture_schedule,
    _persist_adaptive_capture_state,
    _wait_for_adaptive_capture_deadline,
    ensure_daemon_defaults,
    get_status,
    get_quick_status,
    run_daemon,
)
from activity_mvp.storage import ensure_ready
from activity_mvp.runtime_liveness import HeartbeatWriter


def changed(*display_ids: str):
    return capture_observation(
        [{"display_id": display_id} for display_id in display_ids],
        [],
        [],
    )


def unchanged(*display_ids: str):
    return capture_observation(
        [],
        [
            {"display_id": display_id, "reason": "duplicate_frame"}
            for display_id in display_ids
        ],
        [],
    )


class AdaptiveCapturePolicyTests(unittest.TestCase):
    def test_r0_r1_r2_and_r5_idle_schedule(self) -> None:
        state = initial_capture_state()

        state = advance_capture_state(state, changed("display-1", "display-2"))
        self.assertEqual((state.unchanged_streak, state.effective_interval_seconds), (0, 15))
        self.assertEqual(state.reason, "first_complete_display_set")

        state = advance_capture_state(state, unchanged("display-1", "display-2"))
        self.assertEqual((state.unchanged_streak, state.effective_interval_seconds), (1, 15))
        self.assertEqual(state.reason, "first_unchanged_repeat")

        state = advance_capture_state(state, unchanged("display-1", "display-2"))
        self.assertEqual((state.unchanged_streak, state.effective_interval_seconds), (2, 30))

        state = advance_capture_state(state, unchanged("display-1", "display-2"))
        state = advance_capture_state(state, unchanged("display-1", "display-2"))
        state = advance_capture_state(
            state,
            unchanged("display-1", "display-2"),
            idle_seconds=120,
        )
        self.assertEqual((state.unchanged_streak, state.effective_interval_seconds), (5, 60))
        self.assertEqual(state.reason, "unchanged_idle")

    def test_sixty_seconds_requires_r5_and_resource_qualifier(self) -> None:
        state = advance_capture_state(initial_capture_state(), changed("display-1"))
        for _ in range(4):
            state = advance_capture_state(
                state,
                unchanged("display-1"),
                idle_seconds=999,
                on_battery=True,
                low_power_mode=True,
            )
        self.assertEqual((state.unchanged_streak, state.effective_interval_seconds), (4, 30))

        state = advance_capture_state(
            state,
            unchanged("display-1"),
            idle_seconds=119,
        )
        self.assertEqual((state.unchanged_streak, state.effective_interval_seconds), (5, 30))

        state = advance_capture_state(
            state,
            unchanged("display-1"),
            on_battery=True,
        )
        self.assertEqual((state.unchanged_streak, state.effective_interval_seconds), (6, 60))
        self.assertEqual(state.reason, "unchanged_battery")

    def test_low_power_qualifies_only_after_r5(self) -> None:
        state = advance_capture_state(initial_capture_state(), changed("display-1"))
        for _ in range(5):
            state = advance_capture_state(
                state,
                unchanged("display-1"),
                low_power_mode=True,
            )
        self.assertEqual((state.unchanged_streak, state.effective_interval_seconds), (5, 60))
        self.assertEqual(state.reason, "unchanged_low_power")

    def test_change_display_change_error_and_ambiguous_skip_reset_to_baseline(self) -> None:
        display_ids = changed("display-1").display_ids
        backed_off = AdaptiveCaptureState(
            display_ids=display_ids,
            unchanged_streak=5,
            effective_interval_seconds=60,
            reason="unchanged_idle",
        )

        visual = advance_capture_state(backed_off, changed("display-1"))
        self.assertEqual((visual.unchanged_streak, visual.effective_interval_seconds), (0, 15))
        self.assertEqual(visual.reason, "visual_change")

        topology = advance_capture_state(backed_off, unchanged("display-1", "display-2"))
        self.assertEqual((topology.unchanged_streak, topology.effective_interval_seconds), (0, 15))
        self.assertEqual(topology.reason, "display_set_changed")

        error = capture_observation([], [], [{"display_id": "display-1", "reason": "failed"}])
        failed = advance_capture_state(backed_off, error)
        self.assertEqual((failed.display_ids, failed.unchanged_streak, failed.effective_interval_seconds), (frozenset(), 0, 15))
        self.assertEqual(failed.reason, "capture_error")

        ambiguous = capture_observation(
            [],
            [{"display_id": "display-1", "reason": "vlm_pending_queued"}],
            [],
        )
        skipped = advance_capture_state(backed_off, ambiguous)
        self.assertEqual((skipped.display_ids, skipped.unchanged_streak, skipped.effective_interval_seconds), (frozenset(), 0, 15))
        self.assertEqual(skipped.reason, "ambiguous_skip")

    def test_first_duplicate_batch_after_restart_is_r0(self) -> None:
        state = advance_capture_state(initial_capture_state(), unchanged("display-1"))
        self.assertEqual((state.unchanged_streak, state.effective_interval_seconds), (0, 15))
        self.assertEqual(state.reason, "first_complete_display_set")

    def test_malformed_batch_is_ambiguous_and_cannot_back_off(self) -> None:
        observation = capture_observation(
            [],
            [{"display_id": "display-1", "reason": "duplicate_frame"}, object()],
            [],
        )
        state = advance_capture_state(
            AdaptiveCaptureState(
                display_ids=changed("display-1").display_ids,
                unchanged_streak=5,
                effective_interval_seconds=60,
                reason="unchanged_idle",
            ),
            observation,
        )
        self.assertFalse(observation.complete)
        self.assertEqual((state.display_ids, state.effective_interval_seconds), (frozenset(), 15))
        self.assertEqual(state.reason, "ambiguous_skip")

    def test_observation_payload_never_exports_raw_display_identifier(self) -> None:
        private_identifier = "/private/user/Secret Display Name"
        payload = observation_payload(changed(private_identifier))
        serialized = json.dumps(payload)
        self.assertNotIn(private_identifier, serialized)
        self.assertNotIn("Secret Display", serialized)

    def test_hid_input_is_an_idle_counter_decrease(self) -> None:
        self.assertTrue(hid_input_detected(130, 2))
        self.assertFalse(hid_input_detected(130, 135))
        self.assertFalse(hid_input_detected(None, 0))
        self.assertTrue(hid_input_detected(0, 1, elapsed_seconds=5))
        self.assertFalse(hid_input_detected(0, 4, elapsed_seconds=5))


class _FakeMonotonic:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class AdaptiveCaptureWaitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = _FakeMonotonic()
        self.state = AdaptiveCaptureState(
            display_ids=frozenset({"display-1"}),
            unchanged_streak=5,
            effective_interval_seconds=60,
            reason="unchanged_idle",
        )

    def test_long_wait_polls_every_five_seconds_and_hid_wakes_immediately(self) -> None:
        with patch(
            "activity_mvp.daemon._safe_system_idle_probe",
            side_effect=(
                {"available": True, "idle_seconds": 135},
                {"available": True, "idle_seconds": 2},
            ),
        ), patch("activity_mvp.daemon._persist_adaptive_capture_state") as persist:
            state, session_kind, reason = _wait_for_adaptive_capture_deadline(
                Path("unused.sqlite"),
                Path("unused-root"),
                self.state,
                wait_seconds=60,
                previous_session_state="unlocked",
                previous_idle_seconds=130,
                base_interval_seconds=15,
                document_reconcile_enabled=False,
                session_probe=lambda: {"state": "unlocked", "locked": False},
                monotonic_clock=self.clock,
                sleeper=self.clock.sleep,
            )

        self.assertEqual(self.clock.sleeps, [5.0, 5.0])
        self.assertEqual((session_kind, reason), ("unlocked", "input_activity"))
        self.assertEqual((state.unchanged_streak, state.effective_interval_seconds), (0, 15))
        persist.assert_called_once()

    def test_hid_probe_unavailable_fails_back_to_fifteen_seconds(self) -> None:
        with patch(
            "activity_mvp.daemon._safe_system_idle_probe",
            return_value={"available": False},
        ), patch("activity_mvp.daemon._persist_adaptive_capture_state") as persist:
            state, session_kind, reason = _wait_for_adaptive_capture_deadline(
                Path("unused.sqlite"),
                Path("unused-root"),
                self.state,
                wait_seconds=60,
                previous_session_state="unlocked",
                previous_idle_seconds=130,
                base_interval_seconds=15,
                document_reconcile_enabled=False,
                session_probe=lambda: {"state": "unlocked", "locked": False},
                monotonic_clock=self.clock,
                sleeper=self.clock.sleep,
            )

        self.assertEqual(self.clock.sleeps, [5.0])
        self.assertEqual((session_kind, reason), ("unlocked", "hid_unavailable"))
        self.assertEqual((state.unchanged_streak, state.effective_interval_seconds), (0, 15))
        persist.assert_called_once()

    def test_locked_wait_never_reaches_capture_deadline_and_unlock_wakes(self) -> None:
        sessions = iter(
            (
                {"state": "locked", "locked": True},
                {"state": "unlocked", "locked": False},
            )
        )
        with patch(
            "activity_mvp.daemon._safe_system_idle_probe",
            return_value={"available": True, "idle_seconds": 0},
        ), patch("activity_mvp.daemon._persist_adaptive_capture_state"), patch(
            "activity_mvp.daemon._record_adaptive_wait_session_transition"
        ) as transition:
            state, session_kind, reason = _wait_for_adaptive_capture_deadline(
                Path("unused.sqlite"),
                Path("unused-root"),
                self.state,
                wait_seconds=0,
                previous_session_state="locked",
                previous_idle_seconds=0,
                base_interval_seconds=15,
                document_reconcile_enabled=False,
                session_probe=lambda: next(sessions),
                monotonic_clock=self.clock,
                sleeper=self.clock.sleep,
            )

        self.assertEqual(self.clock.sleeps, [5.0, 5.0])
        self.assertEqual((session_kind, reason), ("unlocked", "session_resumed"))
        self.assertEqual((state.display_ids, state.effective_interval_seconds), (frozenset(), 15))
        transition.assert_called_once()

    def test_file_worker_heartbeat_is_independent_during_backoff(self) -> None:
        with patch(
            "activity_mvp.daemon._safe_system_idle_probe",
            side_effect=(
                {"available": True, "idle_seconds": 105},
                {"available": True, "idle_seconds": 110},
                {"available": True, "idle_seconds": 115},
            ),
        ), patch(
            "activity_mvp.daemon._maybe_start_document_change_worker_when_eligible"
        ) as heartbeat:
            state, session_kind, reason = _wait_for_adaptive_capture_deadline(
                Path("unused.sqlite"),
                Path("unused-root"),
                self.state,
                wait_seconds=15,
                previous_session_state="unlocked",
                previous_idle_seconds=100,
                base_interval_seconds=15,
                document_reconcile_enabled=True,
                session_probe=lambda: {"state": "unlocked", "locked": False},
                monotonic_clock=self.clock,
                sleeper=self.clock.sleep,
            )

        self.assertEqual(self.clock.sleeps, [5.0, 5.0, 5.0])
        self.assertEqual((state, session_kind, reason), (self.state, "unlocked", "deadline"))
        self.assertEqual(heartbeat.call_count, 3)

    def test_monotonic_oversleep_is_treated_as_system_resume(self) -> None:
        def oversleep(seconds: float) -> None:
            self.clock.sleeps.append(seconds)
            self.clock.now += seconds + 10

        with patch(
            "activity_mvp.daemon._safe_system_idle_probe",
            return_value={"available": True, "idle_seconds": 140},
        ), patch("activity_mvp.daemon._persist_adaptive_capture_state"):
            state, session_kind, reason = _wait_for_adaptive_capture_deadline(
                Path("unused.sqlite"),
                Path("unused-root"),
                self.state,
                wait_seconds=60,
                previous_session_state="unlocked",
                previous_idle_seconds=130,
                base_interval_seconds=15,
                document_reconcile_enabled=False,
                session_probe=lambda: {"state": "unlocked", "locked": False},
                monotonic_clock=self.clock,
                sleeper=oversleep,
            )

        self.assertEqual((session_kind, reason), ("unlocked", "system_resumed"))
        self.assertEqual((state.display_ids, state.effective_interval_seconds), (frozenset(), 15))

    def test_suspend_gap_wakes_even_when_monotonic_excludes_sleep(self) -> None:
        wall_now = [0.0]

        def suspended_sleep(seconds: float) -> None:
            self.clock.sleeps.append(seconds)
            self.clock.now += seconds
            wall_now[0] += seconds + 300

        with patch(
            "activity_mvp.daemon._safe_system_idle_probe",
            return_value={"available": True, "idle_seconds": 140},
        ), patch("activity_mvp.daemon._persist_adaptive_capture_state"):
            state, session_kind, reason = _wait_for_adaptive_capture_deadline(
                Path("unused.sqlite"),
                Path("unused-root"),
                self.state,
                wait_seconds=60,
                previous_session_state="unlocked",
                previous_idle_seconds=130,
                base_interval_seconds=15,
                document_reconcile_enabled=False,
                session_probe=lambda: {"state": "unlocked", "locked": False},
                monotonic_clock=self.clock,
                wall_clock=lambda: wall_now[0],
                sleeper=suspended_sleep,
            )

        self.assertEqual((session_kind, reason), ("unlocked", "system_resumed"))
        self.assertEqual((state.display_ids, state.effective_interval_seconds), (frozenset(), 15))


class AdaptiveCaptureDaemonPolicyTests(unittest.TestCase):
    def test_busy_cycle_does_not_invent_an_unlocked_session(self) -> None:
        next_state, idle_seconds, session_kind = _advance_adaptive_capture_schedule(
            initial_capture_state(),
            {"status": "busy", "captured": 0, "skipped": 0, "errors": 0},
            base_interval_seconds=15,
            previous_session_state="unknown",
        )
        self.assertEqual(session_kind, "unknown")
        self.assertIsNone(idle_seconds)
        self.assertEqual(next_state.effective_interval_seconds, 15)

    def test_unknown_power_source_does_not_count_as_battery(self) -> None:
        state = AdaptiveCaptureState(
            display_ids=changed("display-1").display_ids,
            unchanged_streak=4,
            effective_interval_seconds=30,
            reason="unchanged_repeat",
        )
        result = {
            "status": "ready",
            "capture_observation": observation_payload(unchanged("display-1")),
        }
        with patch(
            "activity_mvp.daemon.system_idle_seconds",
            return_value={"available": True, "idle_seconds": 119},
        ), patch(
            "activity_mvp.daemon.power_state",
            return_value={"external_power": False, "source": "unknown", "low_power_mode": False},
        ):
            next_state, _, session_kind = _advance_adaptive_capture_schedule(
                state,
                result,
                base_interval_seconds=15,
                previous_session_state="unlocked",
            )

        self.assertEqual(session_kind, "unlocked")
        self.assertEqual((next_state.unchanged_streak, next_state.effective_interval_seconds), (5, 30))

    def test_exact_battery_source_qualifies_at_r5(self) -> None:
        state = AdaptiveCaptureState(
            display_ids=changed("display-1").display_ids,
            unchanged_streak=4,
            effective_interval_seconds=30,
            reason="unchanged_repeat",
        )
        result = {
            "status": "ready",
            "capture_observation": observation_payload(unchanged("display-1")),
        }
        with patch(
            "activity_mvp.daemon.system_idle_seconds",
            return_value={"available": True, "idle_seconds": 1},
        ), patch(
            "activity_mvp.daemon.power_state",
            return_value={"external_power": False, "source": "battery_power", "low_power_mode": False},
        ):
            next_state, _, _ = _advance_adaptive_capture_schedule(
                state,
                result,
                base_interval_seconds=15,
                previous_session_state="unlocked",
            )

        self.assertEqual((next_state.unchanged_streak, next_state.effective_interval_seconds), (5, 60))
        self.assertEqual(next_state.reason, "unchanged_battery")

    def test_daemon_enters_chunked_wait_after_second_unchanged_repeat(self) -> None:
        class StopLoop(RuntimeError):
            pass

        observations = (
            changed("display-1"),
            unchanged("display-1"),
            unchanged("display-1"),
        )
        results = [
            {
                "status": "ready",
                "captured": int(observation.visual_change),
                "skipped": int(observation.all_unchanged),
                "errors": 0,
                "capture_observation": observation_payload(observation),
            }
            for observation in observations
        ]
        baseline_sleeps: list[float] = []

        def sleep_baseline(seconds: float) -> None:
            baseline_sleeps.append(seconds)

        def stop_at_long_wait(*args, **kwargs):
            state = args[2]
            self.assertEqual((state.unchanged_streak, state.effective_interval_seconds), (2, 30))
            self.assertIsInstance(kwargs.get("heartbeat"), HeartbeatWriter)
            self.assertEqual(kwargs["heartbeat"].worker, "daemon")
            raise StopLoop("adaptive wait reached")

        with tempfile.TemporaryDirectory() as temporary, patch(
            "activity_mvp.daemon._document_change_startup_reconcile_enabled",
            return_value=False,
        ), patch(
            "activity_mvp.daemon.session_state",
            return_value={"state": "unlocked", "locked": False, "on_console": True},
        ), patch(
            "activity_mvp.daemon._run_daemon_startup_maintenance",
            return_value={"retention_day": "2099-01-01"},
        ), patch(
            "activity_mvp.daemon.run_once",
            side_effect=results,
        ) as run_once_mock, patch(
            "activity_mvp.daemon.system_idle_seconds",
            return_value={"available": True, "idle_seconds": 0},
        ), patch(
            "activity_mvp.daemon._maybe_start_daily_maintenance_worker"
        ), patch(
            "activity_mvp.daemon._maybe_start_ha_automation_worker"
        ), patch(
            "activity_mvp.daemon._maybe_start_quality_advice_worker"
        ), patch(
            "activity_mvp.daemon._sleep_between_daemon_cycles",
            side_effect=sleep_baseline,
        ), patch(
            "activity_mvp.daemon._wait_for_adaptive_capture_deadline",
            side_effect=stop_at_long_wait,
        ) as adaptive_wait:
            db_path = Path(temporary) / "activity.sqlite"
            from activity_mvp.capture_lease import configure_capture_lease
            grant = ensure_ready(db_path)
            configure_capture_lease(grant, enabled=True)
            grant.commit(); grant.close()
            with self.assertRaisesRegex(StopLoop, "adaptive wait reached"):
                run_daemon(db_path, interval_seconds=15)
            conn = ensure_ready(db_path)
            try:
                status = get_quick_status(conn)
            finally:
                conn.close()

        self.assertEqual(run_once_mock.call_count, 3)
        self.assertEqual(len(baseline_sleeps), 2)
        adaptive_wait.assert_called_once()
        self.assertEqual(status["capture_interval_seconds"], 15)
        self.assertEqual(status["effective_capture_interval_seconds"], 30)
        self.assertEqual(status["capture_unchanged_streak"], 2)


class AdaptiveCaptureStatusTests(unittest.TestCase):
    def test_quick_status_exposes_four_fields_without_changing_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "activity.sqlite"
            conn = ensure_ready(db_path)
            try:
                ensure_daemon_defaults(conn)
                conn.execute(
                    "UPDATE daemon_state SET value = 'unlocked' WHERE key = 'session_state'"
                )
                conn.execute(
                    "UPDATE daemon_state SET value = 'false' WHERE key = 'locked'"
                )
                conn.execute(
                    "UPDATE daemon_state SET value = 'running' WHERE key = 'daemon_status'"
                )
                conn.commit()
            finally:
                conn.close()

            _persist_adaptive_capture_state(
                db_path,
                AdaptiveCaptureState(
                    display_ids=frozenset({"display-1"}),
                    unchanged_streak=3,
                    effective_interval_seconds=30,
                    reason="unchanged_repeat",
                ),
            )
            conn = ensure_ready(db_path)
            try:
                payload = get_quick_status(conn)
                with patch(
                    "activity_mvp.daemon.session_state",
                    return_value={"state": "unlocked", "locked": False, "on_console": True},
                ), patch(
                    "activity_mvp.daemon._launch_agent_status",
                    return_value={
                        "loaded": True,
                        "running": True,
                        "pid": "123",
                        "last_exit_code": "(never exited)",
                    },
                ), patch(
                    "activity_mvp.daemon._dashboard_listener",
                    return_value={"listening": False, "pid": "", "command": ""},
                ):
                    full_payload = get_status(conn, Path(temporary))
                schema_version = int(
                    conn.execute(
                        "SELECT value FROM meta WHERE key = 'schema_version'"
                    ).fetchone()["value"]
                )
            finally:
                conn.close()

        self.assertEqual(payload["capture_interval_seconds"], 15)
        self.assertEqual(payload["capture_schedule_mode"], "adaptive_balanced")
        self.assertEqual(payload["effective_capture_interval_seconds"], 30)
        self.assertEqual(payload["capture_backoff_reason"], "unchanged_repeat")
        self.assertEqual(payload["capture_unchanged_streak"], 3)
        for field in (
            "capture_schedule_mode",
            "effective_capture_interval_seconds",
            "capture_backoff_reason",
            "capture_unchanged_streak",
        ):
            self.assertEqual(full_payload[field], payload[field])
        self.assertEqual(schema_version, 32)

    def test_quick_status_does_not_export_untrusted_backoff_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db_path = Path(temporary) / "activity.sqlite"
            conn = ensure_ready(db_path)
            try:
                ensure_daemon_defaults(conn)
                conn.execute(
                    "UPDATE daemon_state SET value = ? WHERE key = 'capture_backoff_reason'",
                    ("/private/user/Secret Window Title",),
                )
                conn.commit()
                payload = get_quick_status(conn)
            finally:
                conn.close()

        serialized = json.dumps(payload)
        self.assertEqual(payload["capture_backoff_reason"], "baseline")
        self.assertNotIn("/private/user", serialized)
        self.assertNotIn("Secret Window", serialized)


if __name__ == "__main__":
    unittest.main()
