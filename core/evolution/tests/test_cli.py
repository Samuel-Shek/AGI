"""CLI admission and lifecycle tests. No approved grant or live daemon is used.

Filesystem fixtures are private temporary directories. Runtime lifecycle tests
mock the service and HTTP server, so they neither inspect production sources nor
start background observation. One stop test uses a real isolated controller.
"""

from argparse import Namespace
import contextlib
import io
import json
import os
from pathlib import Path
import plistlib
import signal
import stat
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from ai_ceo import cli
from ai_ceo.controller import Controller
from ai_ceo.policy import GateDenied


PROJECT = Path(__file__).resolve().parent.parent
ENTRY_MARKER = "# ai-ceo-local-panel-v1\n"
TEST_URL = "http://127.0.0.1:43123/#token=synthetic-test-token-not-a-live-grant"


class FakeServer:
    """No socket is created. Shutdown still crosses the worker boundary."""
    server_address = ("127.0.0.1", 43123)
    access_token = "synthetic-test-token-not-a-live-grant"

    def __init__(self):
        self.stopped = threading.Event()
        self.closed = False
        self.entered = False
        self.on_serve = None

    def shutdown(self):
        self.stopped.set()

    def serve_forever(self, **_kwargs):
        self.entered = True
        if self.on_serve:
            self.on_serve()
        if not self.stopped.wait(2):
            raise AssertionError("mock lifecycle did not shut down")

    def server_close(self):
        self.closed = True


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ai-ceo-cli-test-")
        # macOS exposes its default temporary root through /var -> /private/var.
        # Use the canonical test root; explicit symlink tests stay separate.
        self.root = Path(self.temp.name).resolve()
        self.state = self.root / "state"

    def tearDown(self):
        self.temp.cleanup()

    def private_file(self, relative, content):
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        target.chmod(0o600)
        return target

    def invoke(self, arguments):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--state", str(self.state), *arguments])
        return code, out.getvalue(), err.getvalue()

    def test_daemon_draft_is_rejected_before_controller_or_state_creation(self):
        draft = self.private_file("draft.json", (PROJECT / "examples/observer-grant.draft.json").read_text())
        with patch("ai_ceo.controller.Controller") as constructor, patch("ai_ceo.cli.serve_observer") as start:
            code, out, err = self.invoke(["daemon", "--grant", str(draft), "--url-file", str(self.root / "entry"), "--activate"])
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(err)["ok"])
        self.assertEqual(out, "")
        constructor.assert_not_called()
        start.assert_not_called()
        self.assertFalse(self.state.exists())
        self.assertFalse((self.root / "entry").exists())

    def test_daemon_missing_grant_does_not_initialize_state(self):
        with patch("ai_ceo.controller.Controller") as constructor:
            code, _, err = self.invoke(["daemon", "--grant", str(self.root / "absent"), "--url-file", str(self.root / "entry")])
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(err)["ok"])
        constructor.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_daemon_invalid_port_does_not_load_grant_or_create_controller(self):
        with patch("ai_ceo.controller.Controller") as constructor, patch("ai_ceo.service.load_grant") as load:
            code, _, err = self.invoke(["daemon", "--grant", "unused", "--url-file", "unused", "--port", "65536"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["code"], "invalid_port")
        load.assert_not_called()
        constructor.assert_not_called()

    def test_initial_status_has_no_unattended_or_external_capability(self):
        code, out, err = self.invoke(["status"])
        self.assertEqual((code, err), (0, ""))
        state = json.loads(out)
        self.assertEqual(state["mode"], "paused")
        self.assertFalse(state["scheduler"]["installed"])
        self.assertFalse(state["scheduler"]["enabled"])
        by_id = {entry["id"]: entry for entry in state["capabilities"]}
        for key in ["model", "production", "worker", "live_observation", "scheduler"]:
            self.assertEqual(by_id[key]["status"], "disabled", key)

    def test_stop_bypasses_live_controller_lock_and_survives_restart(self):
        controller = Controller(self.state)
        try:
            with patch("ai_ceo.controller.Controller", side_effect=AssertionError("must not acquire another controller")):
                code, out, err = self.invoke(["control", "stop"])
            self.assertEqual((code, err), (0, ""))
            self.assertTrue(json.loads(out)["stop_requested"])
            self.assertEqual((self.state / "STOP").read_text(), "operator_stop\n")
            self.assertEqual(stat.S_IMODE((self.state / "STOP").stat().st_mode), 0o600)
            with self.assertRaises(GateDenied):
                controller.replay()
            with self.assertRaises(GateDenied):
                controller.observe()
            self.assertEqual(controller.snapshot()["mode"], "stopped")
        finally:
            controller.close()
        second = Controller(self.state)
        try:
            self.assertEqual(second.snapshot()["mode"], "stopped")
        finally:
            second.close()

    def test_missing_stop_target_has_redacted_rejection(self):
        code, out, err = self.invoke(["control", "stop"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertFalse(json.loads(err)["ok"])
        self.assertNotIn(str(self.state), err)
        self.assertFalse(self.state.exists())

    def test_stop_refuses_unregistered_or_shared_directory(self):
        self.state.mkdir(mode=0o700)
        with self.assertRaises(GateDenied):
            cli.signal_stop(self.state)
        (self.state / ".ai-ceo-state.json").write_text("{}")
        self.state.chmod(0o755)
        with self.assertRaises(GateDenied):
            cli.signal_stop(self.state)
        self.assertFalse((self.state / "STOP").exists())

    def test_stop_does_not_follow_existing_stop_symlink(self):
        controller = Controller(self.state)
        controller.close()
        unrelated = self.private_file("unrelated", "retain-me")
        (self.state / "STOP").symlink_to(unrelated)
        with self.assertRaises(GateDenied):
            cli.signal_stop(self.state)
        self.assertEqual(unrelated.read_text(), "retain-me")

    def test_stage_service_is_disabled_draft_without_activation(self):
        candidate = self.root / "candidate.plist"
        result = cli.stage_service(candidate, self.state)
        document = plistlib.loads(candidate.read_bytes())
        self.assertTrue(document["Disabled"])
        self.assertFalse(document["RunAtLoad"])
        self.assertFalse(document["KeepAlive"])
        args = document["ProgramArguments"]
        self.assertIn("daemon", args)
        self.assertNotIn("--activate", args)
        self.assertIn(str(PROJECT / "examples/observer-grant.draft.json"), args)
        self.assertFalse(result["installed"])
        self.assertFalse(result["enabled"])
        self.assertFalse(self.state.exists())
        self.assertEqual(stat.S_IMODE(candidate.stat().st_mode), 0o600)

    def test_stage_refuses_launch_locations_and_never_overwrites(self):
        for directory in ["LaunchAgents", "LaunchDaemons"]:
            destination = self.root / directory / "candidate.plist"
            with self.subTest(directory=directory), self.assertRaises(GateDenied):
                cli.stage_service(destination, self.state)
            self.assertFalse(destination.exists())
        existing = self.private_file("candidate.plist", "unrelated-plist-content")
        with self.assertRaises((OSError, GateDenied)):
            cli.stage_service(existing, self.state)
        self.assertEqual(existing.read_text(), "unrelated-plist-content")

    def test_entry_is_private_marked_and_can_update_only_owned_entry(self):
        path = self.root / "entry.txt"
        cli.write_observer_entry(path, TEST_URL)
        self.assertEqual(path.read_text(), ENTRY_MARKER + TEST_URL + "\n")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        next_url = TEST_URL.replace("43123", "43124")
        cli.write_observer_entry(path, next_url)
        self.assertEqual(path.read_text(), ENTRY_MARKER + next_url + "\n")

    def test_entry_never_overwrites_unrelated_private_regular_file(self):
        path = self.private_file("unrelated-private-note", "my private original")
        with self.assertRaises((OSError, GateDenied)):
            cli.write_observer_entry(path, TEST_URL)
        self.assertEqual(path.read_text(), "my private original")

    def test_entry_rejects_nonprivate_files_symlinks_and_hardlinks(self):
        path = self.private_file("not-private", ENTRY_MARKER + TEST_URL + "\n")
        path.chmod(0o644)
        with self.assertRaises((OSError, GateDenied)):
            cli.write_observer_entry(path, TEST_URL)
        original = self.private_file("original", ENTRY_MARKER + TEST_URL + "\n")
        linked = self.root / "linked"
        linked.symlink_to(original)
        with self.assertRaises((OSError, GateDenied)):
            cli.write_observer_entry(linked, TEST_URL)
        hardlinked = self.root / "hardlinked"
        os.link(original, hardlinked)
        with self.assertRaises((OSError, GateDenied)):
            cli.write_observer_entry(hardlinked, TEST_URL)
        self.assertEqual(original.read_text(), ENTRY_MARKER + TEST_URL + "\n")

    def test_entry_rejects_symlinked_parent_without_creating_target(self):
        target = self.root / "target"
        target.mkdir(mode=0o700)
        linked = self.root / "linked-parent"
        linked.symlink_to(target, target_is_directory=True)
        with self.assertRaises((OSError, GateDenied)):
            cli.write_observer_entry(linked / "new-entry", TEST_URL)
        self.assertFalse((target / "new-entry").exists())

    def lifecycle(self, *, outcome=None, failure=None, terminate=False, entry_conflict=False):
        runtime = MagicMock()
        if failure:
            runtime.tick.side_effect = failure
        else:
            runtime.tick.return_value = outcome or {"state": "exhausted"}
        server = FakeServer()
        controller = MagicMock()
        args = Namespace(activate=False, port=0, url_file=self.root / "observer-entry")
        if entry_conflict:
            self.private_file("observer-entry", "unrelated data")
        handlers = {}
        restored = []
        def signal_mock(signum, handler):
            previous = handlers.get(signum, f"original-{int(signum)}")
            handlers[signum] = handler
            restored.append((signum, handler))
            return previous
        if terminate:
            server.on_serve = lambda: handlers[signal.SIGTERM](signal.SIGTERM, None)
        out = io.StringIO()
        caught = None
        result = None
        with patch("ai_ceo.service.ServiceRuntime", return_value=runtime), patch("ai_ceo.server.serve", return_value=server), patch("ai_ceo.cli.signal.signal", side_effect=signal_mock), contextlib.redirect_stdout(out):
            try:
                result = cli.serve_observer(controller, Namespace(interval_seconds=60), args)
            except Exception as exc:
                caught = exc
        return result, caught, runtime, server, controller, handlers, out.getvalue()

    def test_observer_terminal_state_closes_runtime_server_and_restores_signals(self):
        result, error, runtime, server, _, handlers, output = self.lifecycle()
        self.assertIsNone(error)
        self.assertEqual(result["status"], "observer_stopped")
        self.assertTrue(server.closed)
        self.assertTrue(server.stopped.is_set())
        runtime.close.assert_called_once()
        self.assertEqual(handlers[signal.SIGINT], f"original-{int(signal.SIGINT)}")
        self.assertEqual(handlers[signal.SIGTERM], f"original-{int(signal.SIGTERM)}")
        self.assertNotIn(server.access_token, output)

    def test_observer_signal_stops_waiting_worker_and_cleans_resources(self):
        result, error, runtime, server, _, _, _ = self.lifecycle(outcome={"state": "active"}, terminate=True)
        self.assertIsNone(error)
        self.assertEqual(result["status"], "observer_stopped")
        self.assertTrue(server.closed)
        runtime.close.assert_called_once()
        self.assertFalse(any(thread.name == "ceo-observer" and thread.is_alive() for thread in threading.enumerate()))

    def test_observer_runtime_failure_retains_type_only_and_exits(self):
        _, error, runtime, server, controller, _, output = self.lifecycle(failure=RuntimeError("private-source-secret"))
        self.assertIsInstance(error, GateDenied)
        self.assertEqual(error.code, "observer_runtime_failed")
        self.assertTrue(server.closed)
        runtime.close.assert_called_once()
        controller.store.append_audit.assert_called_once_with("observer_runtime_error", {"error_type": "RuntimeError"})
        self.assertNotIn("private-source-secret", output)
        self.assertNotIn("private-source-secret", str(error))

    def test_observer_entry_failure_still_closes_server_and_runtime(self):
        _, error, runtime, server, _, _, _ = self.lifecycle(entry_conflict=True)
        self.assertIsNotNone(error)
        self.assertFalse(server.entered)
        self.assertTrue(server.closed)
        runtime.close.assert_called_once()
        self.assertEqual((self.root / "observer-entry").read_text(), "unrelated data")

    def test_preview_entry_collision_closes_server_and_preserves_file(self):
        entry = self.private_file("entry", "existing unrelated file")
        controller, server = MagicMock(), FakeServer()
        with patch("ai_ceo.controller.Controller", return_value=controller), patch("ai_ceo.server.serve", return_value=server):
            code, _, err = self.invoke(["serve", "--url-file", str(entry), "--duration-seconds", "1"])
        self.assertEqual(code, 2)
        self.assertFalse(json.loads(err)["ok"])
        self.assertTrue(server.closed)
        self.assertFalse(server.entered)
        controller.close.assert_called_once()
        self.assertEqual(entry.read_text(), "existing unrelated file")


if __name__ == "__main__":
    unittest.main()
