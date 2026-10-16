from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from activity_mvp.macos_probe import build_ocr_event_from_confirmation_image, capture_sck_keyframe_batch
from activity_mvp.ocr_service import (
    OCR_ADMISSION_DEFERRED,
    OCR_CANCELED,
    OCR_INPUT_CHANGED,
    OCR_PROCESS_FAILED,
    OcrOutcome,
    OcrService,
)
from activity_mvp.swift_helpers import (
    compiled_helper_command,
    compiled_helper_lease,
    ensure_helper,
    helper_command,
)


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def write_executable(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\nset -eu\n" + body, encoding="utf-8")
    path.chmod(0o700)
    return path


def private_frame(root: Path, name: str, content: bytes = b"frame") -> Path:
    path = root / name
    path.write_bytes(content)
    path.chmod(0o600)
    return path


def capture_result(images: list[Path]) -> SimpleNamespace:
    payload = []
    for index, image in enumerate(images, start=1):
        payload.append(
            {
                "display_id": str(index),
                "status": "captured",
                "image_path": str(image),
                "frame_hash": f"hash-{index}",
                "is_main": index == 1,
                "width": 1280,
                "height": 720,
                "display_frame": {
                    "x": (index - 1) * 1280,
                    "y": 0,
                    "width": 1280,
                    "height": 720,
                },
                "visible_windows": [],
            }
        )
    return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(payload))


class ImmediateTask:
    def __init__(self, outcome: OcrOutcome) -> None:
        self._outcome = outcome

    def result(self) -> OcrOutcome:
        return self._outcome

    def cancel(self) -> None:
        return None

    def done(self) -> bool:
        return True

    def wait_terminal(self) -> None:
        return None


class OcrPerformanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_vision_helper_is_prebuilt_private_and_runtime_never_falls_back(self) -> None:
        source = self.root / "vision_ocr.swift"
        source.write_text("// deterministic test source", encoding="utf-8")

        def fake_compile(command, **_kwargs):
            output = Path(command[command.index("-o") + 1])
            output.write_bytes(b"compiled-helper")
            return SimpleNamespace(returncode=0, stderr="", stdout="")

        with patch("activity_mvp.swift_helpers._source_path", return_value=source), patch(
            "activity_mvp.swift_helpers.subprocess.run",
            side_effect=fake_compile,
        ) as compiler:
            binary = ensure_helper(self.root, "vision_ocr")
            self.assertEqual(stat.S_IMODE(binary.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(binary.parent.stat().st_mode), 0o700)
            self.assertEqual(compiled_helper_command(self.root, "vision_ocr"), [str(binary.resolve())])
            self.assertEqual(compiler.call_count, 1)

            binary.unlink()
            with self.assertRaises(RuntimeError):
                helper_command(self.root, "vision_ocr")
            self.assertEqual(compiler.call_count, 1)


    def test_helper_lifecycle_lease_blocks_atomic_replacement_until_release(self) -> None:
        source = self.root / "vision_ocr.swift"
        source.write_text("v1", encoding="utf-8")
        state = self.root / "generation-state"
        state.mkdir(mode=0o700)

        def fake_compile(command, **_kwargs):
            output = Path(command[command.index("-o") + 1])
            generation = source.read_text(encoding="utf-8")
            body = (
                f"state='{state}'\n"
                f"/usr/bin/touch \"$state/started-{generation}\"\n"
                + (
                    f"while [ ! -e \"$state/release-{generation}\" ]; do /bin/sleep 0.01; done\n"
                    if generation == "v1"
                    else ""
                )
                + f"/usr/bin/printf '{{\"lines\":[{{\"text\":\"{generation}\"}}]}}\\n'\n"
            )
            write_executable(output, body)
            return SimpleNamespace(returncode=0, stderr="", stdout="")

        with patch("activity_mvp.swift_helpers._source_path", return_value=source), patch(
            "activity_mvp.swift_helpers.subprocess.run",
            side_effect=fake_compile,
        ):
            binary = ensure_helper(self.root, "vision_ocr")
            frame = private_frame(self.root, "lease.png")
            service = OcrService()
            self.addCleanup(service.shutdown)
            build_done = threading.Event()
            build_error: list[BaseException] = []

            def rebuild() -> None:
                try:
                    ensure_helper(self.root, "vision_ocr")
                except BaseException as exc:  # pragma: no cover - asserted below
                    build_error.append(exc)
                finally:
                    build_done.set()

            with compiled_helper_lease(self.root, "vision_ocr") as command:
                task = service.submit(self.root, command, frame)
                self.assertTrue(wait_until(lambda: (state / "started-v1").exists()))
                time.sleep(0.02)
                source.write_text("v2", encoding="utf-8")
                builder = threading.Thread(target=rebuild)
                builder.start()
                self.assertFalse(build_done.wait(0.2))
                (state / "release-v1").touch()
                self.assertEqual(task.result().payload["lines"][0]["text"], "v1")
                self.assertFalse(build_done.is_set())

            builder.join(timeout=5)
            self.assertFalse(builder.is_alive())
            self.assertEqual(build_error, [])
            self.assertTrue(build_done.is_set())
            with compiled_helper_lease(self.root, "vision_ocr") as command:
                self.assertEqual(
                    service.submit(self.root, command, frame).result().payload["lines"][0]["text"],
                    "v2",
                )

    def test_two_local_workers_hold_external_producer_at_global_limit(self) -> None:
        state = self.root / "state"
        state.mkdir(mode=0o700)
        helper = write_executable(
            self.root / "fake-ocr",
            f'''state='{state}'
name=$(/usr/bin/basename "$1")
/usr/bin/touch "$state/started-$name"
while [ ! -e "$state/release-$name" ]; do /bin/sleep 0.01; done
/usr/bin/printf '{{"lines":[{{"text":"%s"}}],"coordinate_space":"vision"}}\\n' "$name"
''',
        )
        frames = [private_frame(self.root, f"display-{index}.png") for index in range(3)]
        service = OcrService()
        self.addCleanup(service.shutdown)
        tasks = [service.submit(self.root, [str(helper)], frame) for frame in frames[:2]]

        self.assertTrue(wait_until(lambda: len(list(state.glob("started-*"))) == 2))
        self.assertEqual(len(list(state.glob("started-*"))), 2)
        core_root = Path(__file__).resolve().parents[1]
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(
            [str(core_root), environment.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        script = (
            "from activity_mvp.ocr_service import run_ocr; import sys; "
            "outcome=run_ocr(sys.argv[1],[sys.argv[2]],sys.argv[3]); "
            "raise SystemExit(0 if outcome.ok else 1)"
        )
        external = subprocess.Popen(
            [sys.executable, "-c", script, str(self.root), str(helper), str(frames[2])],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
        try:
            time.sleep(0.2)
            self.assertFalse((state / "started-display-2.png").exists())
            (state / "release-display-0.png").touch()
            self.assertTrue(wait_until(lambda: (state / "started-display-2.png").exists()))
            (state / "release-display-1.png").touch()
            (state / "release-display-2.png").touch()
            outcomes = [task.result() for task in tasks]
            self.assertTrue(all(outcome.ok for outcome in outcomes))
            _stdout, stderr = external.communicate(timeout=10)
            self.assertEqual(external.returncode, 0, stderr.decode("utf-8", errors="replace"))
        finally:
            for frame in frames:
                (state / f"release-{frame.name}").touch(exist_ok=True)
            if external.poll() is None:
                external.kill()
                external.wait()

    def test_cross_process_slots_limit_three_producers_to_two_helpers(self) -> None:
        state = self.root / "cross-process-state"
        state.mkdir(mode=0o700)
        helper = write_executable(
            self.root / "cross-process-ocr",
            f'''state='{state}'
name=$(/usr/bin/basename "$1")
/usr/bin/touch "$state/started-$name"
while [ ! -e "$state/release-$name" ]; do /bin/sleep 0.01; done
/usr/bin/printf '{{"lines":[{{"text":"%s"}}],"coordinate_space":"vision"}}\\n' "$name"
''',
        )
        frames = [private_frame(self.root, f"producer-{index}.png") for index in range(3)]
        core_root = Path(__file__).resolve().parents[1]
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(
            [str(core_root), environment.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        script = (
            "from activity_mvp.ocr_service import run_ocr; import sys; "
            "outcome=run_ocr(sys.argv[1],[sys.argv[2]],sys.argv[3]); "
            "raise SystemExit(0 if outcome.ok else 1)"
        )
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(self.root), str(helper), str(frame)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
            for frame in frames
        ]
        try:
            self.assertTrue(wait_until(lambda: len(list(state.glob("started-*"))) == 2))
            self.assertEqual(len(list(state.glob("started-*"))), 2)
            for marker in list(state.glob("started-*")):
                name = marker.name.removeprefix("started-")
                (state / f"release-{name}").touch()
            self.assertTrue(wait_until(lambda: len(list(state.glob("started-*"))) == 3))
            for marker in list(state.glob("started-*")):
                name = marker.name.removeprefix("started-")
                (state / f"release-{name}").touch(exist_ok=True)
            for process in processes:
                _stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, stderr.decode("utf-8", errors="replace"))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait()

    def test_orphan_helper_keeps_slot_after_python_parent_is_sigkilled(self) -> None:
        state = self.root / "orphan-state"
        state.mkdir(mode=0o700)
        helper = write_executable(
            self.root / "orphan-ocr",
            f'''state='{state}'
name=$(/usr/bin/basename "$1")
trap '' HUP TERM
/usr/bin/printf '%s' "$$" > "$state/pid-$name"
/usr/bin/touch "$state/started-$name"
while [ ! -e "$state/release-$name" ]; do /bin/sleep 0.01; done
/usr/bin/printf '{{"lines":[{{"text":"%s"}}]}}\n' "$name"
''',
        )
        frames = [private_frame(self.root, f"orphan-{index}.png") for index in range(3)]
        core_root = Path(__file__).resolve().parents[1]
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(
            [str(core_root), environment.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        script = (
            "from activity_mvp.ocr_service import run_ocr; import sys; "
            "outcome=run_ocr(sys.argv[1],[sys.argv[2]],sys.argv[3]); "
            "raise SystemExit(0 if outcome.ok else 1)"
        )

        def producer(frame: Path) -> subprocess.Popen[bytes]:
            return subprocess.Popen(
                [sys.executable, "-c", script, str(self.root), str(helper), str(frame)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )

        processes = [producer(frames[0]), producer(frames[1])]
        helper_pids: set[int] = set()
        try:
            self.assertTrue(wait_until(lambda: len(list(state.glob("started-*"))) == 2))
            orphan_pid_path = state / f"pid-{frames[0].name}"
            self.assertTrue(wait_until(orphan_pid_path.exists))
            orphan_pid = int(orphan_pid_path.read_text(encoding="utf-8"))
            helper_pids.add(orphan_pid)

            os.kill(processes[0].pid, signal.SIGKILL)
            processes[0].wait(timeout=5)
            processes[0].communicate(timeout=1)
            self.assertTrue(_pid_exists(orphan_pid))

            third = producer(frames[2])
            processes.append(third)
            time.sleep(0.3)
            self.assertFalse((state / f"started-{frames[2].name}").exists())
            self.assertTrue(_pid_exists(orphan_pid))

            (state / f"release-{frames[0].name}").touch()
            self.assertTrue(
                wait_until(lambda: (state / f"started-{frames[2].name}").exists())
            )
            for frame in frames[1:]:
                (state / f"release-{frame.name}").touch()
            for process in processes[1:]:
                _stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, stderr.decode("utf-8", errors="replace"))
            self.assertTrue(wait_until(lambda: not _pid_exists(orphan_pid)))
        finally:
            for frame in frames:
                (state / f"release-{frame.name}").touch(exist_ok=True)
                pid_path = state / f"pid-{frame.name}"
                if pid_path.exists():
                    try:
                        helper_pids.add(int(pid_path.read_text(encoding="utf-8")))
                    except ValueError:
                        pass
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait()
            for helper_pid in helper_pids:
                if _pid_exists(helper_pid):
                    try:
                        os.killpg(helper_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_queued_frame_changed_in_place_is_rejected_before_spawn(self) -> None:
        state = self.root / "identity-state"
        state.mkdir(mode=0o700)
        helper = write_executable(
            self.root / "identity-ocr",
            f'''state='{state}'
name=$(/usr/bin/basename "$1")
/usr/bin/touch "$state/started-$name"
while [ ! -e "$state/release-$name" ]; do /bin/sleep 0.01; done
/usr/bin/printf '{{"lines":[{{"text":"%s"}}]}}\n' "$name"
''',
        )
        frames = [private_frame(self.root, f"identity-{index}.png") for index in range(3)]
        service = OcrService()
        self.addCleanup(service.shutdown)
        tasks = [service.submit(self.root, [str(helper)], frame) for frame in frames]
        self.assertTrue(wait_until(lambda: len(list(state.glob("started-*"))) == 2))
        frames[2].write_bytes(b"changed in place after submission")
        frames[2].chmod(0o600)
        for frame in frames[:2]:
            (state / f"release-{frame.name}").touch()
        self.assertTrue(tasks[0].result().ok)
        self.assertTrue(tasks[1].result().ok)
        self.assertEqual(tasks[2].result().error_code, OCR_INPUT_CHANGED)
        self.assertFalse((state / f"started-{frames[2].name}").exists())

    def test_background_admission_is_rechecked_after_waiting_for_global_slot(self) -> None:
        state = self.root / "admission-state"
        state.mkdir(mode=0o700)
        helper = write_executable(
            self.root / "admission-ocr",
            f'''state='{state}'
name=$(/usr/bin/basename "$1")
/usr/bin/touch "$state/started-$name"
while [ ! -e "$state/release-$name" ]; do /bin/sleep 0.01; done
/usr/bin/printf '{{"lines":[{{"text":"%s"}}]}}\n' "$name"
''',
        )
        frames = [private_frame(self.root, f"admission-{index}.png") for index in range(3)]
        service = OcrService()
        self.addCleanup(service.shutdown)
        running = [service.submit(self.root, [str(helper)], frame) for frame in frames[:2]]
        self.assertTrue(wait_until(lambda: len(list(state.glob("started-*"))) == 2))
        allowed = threading.Event()
        allowed.set()
        deferred = service.submit(
            self.root,
            [str(helper)],
            frames[2],
            admission_check=lambda: allowed.is_set(),
        )
        allowed.clear()
        (state / f"release-{frames[0].name}").touch()
        self.assertEqual(deferred.result().error_code, OCR_ADMISSION_DEFERRED)
        self.assertFalse((state / f"started-{frames[2].name}").exists())
        (state / f"release-{frames[1].name}").touch()
        self.assertTrue(all(task.result().ok for task in running))

    def test_cancelled_queued_task_releases_scheduler_control(self) -> None:
        state = self.root / "queued-cancel-state"
        state.mkdir(mode=0o700)
        helper = write_executable(
            self.root / "queued-cancel-ocr",
            f'''state='{state}'
name=$(/usr/bin/basename "$1")
/usr/bin/touch "$state/started-$name"
while [ ! -e "$state/release-$name" ]; do /bin/sleep 0.01; done
/usr/bin/printf '{{"lines":[{{"text":"%s"}}]}}\n' "$name"
''',
        )
        frames = [private_frame(self.root, f"queued-cancel-{index}.png") for index in range(3)]
        service = OcrService()
        self.addCleanup(service.shutdown)
        running = [service.submit(self.root, [str(helper)], frame) for frame in frames[:2]]
        self.assertTrue(wait_until(lambda: len(list(state.glob("started-*"))) == 2))

        queued = service.submit(self.root, [str(helper)], frames[2])
        queued.cancel()
        self.assertEqual(queued.result().error_code, OCR_CANCELED)
        self.assertTrue(wait_until(lambda: len(service._controls) == 2))
        self.assertFalse((state / f"started-{frames[2].name}").exists())

        for frame in frames[:2]:
            (state / f"release-{frame.name}").touch()
        self.assertTrue(all(task.result().ok for task in running))
        self.assertTrue(wait_until(lambda: not service._controls))

    def test_cancel_reaps_parent_and_child_before_task_is_terminal(self) -> None:
        pid_file = self.root / "pids"
        helper = write_executable(
            self.root / "cancel-ocr",
            f'''trap '' TERM
/bin/sleep 30 &
child=$!
/usr/bin/printf '%s %s' "$$" "$child" > '{pid_file}'
wait "$child"
''',
        )
        frame = private_frame(self.root, "cancel.png")
        service = OcrService()
        self.addCleanup(service.shutdown)
        task = service.submit(self.root, [str(helper)], frame)
        self.assertTrue(
            wait_until(
                lambda: pid_file.exists()
                and len(pid_file.read_text(encoding="utf-8").split()) == 2
            )
        )
        parent_pid, child_pid = [int(value) for value in pid_file.read_text(encoding="utf-8").split()]

        task.cancel()
        outcome = task.result()
        self.assertEqual(outcome.error_code, OCR_CANCELED)
        self.assertTrue(wait_until(lambda: not _pid_exists(parent_pid) and not _pid_exists(child_pid)))

    def test_capture_runs_helper_processes_concurrently_but_emits_display_order(self) -> None:
        state = self.root / "state"
        state.mkdir(mode=0o700)
        helper = write_executable(
            self.root / "ordered-ocr",
            f'''state='{state}'
name=$(/usr/bin/basename "$1")
/usr/bin/touch "$state/started-$name"
if [ "$name" = 'main.png' ]; then
  while [ ! -e "$state/release-main.png" ]; do /bin/sleep 0.01; done
fi
/usr/bin/printf '{{"lines":[{{"text":"%s"}}],"coordinate_space":"vision"}}\\n' "$name"
''',
        )
        main = private_frame(self.root, "main.png")
        secondary = private_frame(self.root, "secondary.png")
        result: dict[str, object] = {}

        def run_capture() -> None:
            result["batch"] = capture_sck_keyframe_batch(
                self.root,
                should_process=lambda _metadata: True,
            )

        with patch("activity_mvp.macos_probe.helper_command", return_value=["sck"]), patch(
            "activity_mvp.macos_probe.subprocess.run",
            return_value=capture_result([main, secondary]),
        ), patch(
            "activity_mvp.macos_probe.compiled_helper_lease",
            return_value=nullcontext([str(helper)]),
        ), patch(
            "activity_mvp.macos_probe._frontmost_context",
            return_value={"app_name": "Obsidian", "bundle_id": "md.obsidian", "window_title": "Safe"},
        ), patch("activity_mvp.macos_probe._accessibility_context", return_value={}), patch(
            "activity_mvp.macos_probe._browser_url",
            return_value={"url": "", "domain": "", "source": "test"},
        ):
            thread = threading.Thread(target=run_capture)
            thread.start()
            self.assertTrue(
                wait_until(
                    lambda: (state / "started-main.png").exists()
                    and (state / "started-secondary.png").exists()
                )
            )
            self.assertNotIn("batch", result)
            (state / "release-main.png").touch()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

        batch = result["batch"]
        self.assertEqual(
            [event["ocr_text"] for event in batch["events"]],
            ["main.png", "secondary.png"],
        )
        self.assertFalse(main.exists())
        self.assertFalse(secondary.exists())

    def test_failure_isolated_and_duplicate_or_blocked_never_start_ocr(self) -> None:
        duplicate = private_frame(self.root, "duplicate.png")
        blocked = private_frame(self.root, "blocked.png")
        failed = private_frame(self.root, "failed.png")

        def should_process(metadata: dict) -> bool:
            return metadata["display_id"] != "sck-display-1"

        def privacy_result(event: dict) -> SimpleNamespace:
            return SimpleNamespace(
                status="blocked" if event["display_id"] == "sck-display-2" else "clear"
            )

        with patch("activity_mvp.macos_probe.helper_command", return_value=["sck"]), patch(
            "activity_mvp.macos_probe.subprocess.run",
            return_value=capture_result([duplicate, blocked, failed]),
        ), patch(
            "activity_mvp.macos_probe.compiled_helper_lease",
            return_value=nullcontext(["/private/vision_ocr"]),
        ), patch(
            "activity_mvp.macos_probe.submit_ocr",
            return_value=ImmediateTask(OcrOutcome(error_code=OCR_PROCESS_FAILED)),
        ) as submit, patch(
            "activity_mvp.macos_probe.classify_event",
            side_effect=privacy_result,
        ), patch(
            "activity_mvp.macos_probe._frontmost_context",
            return_value={"app_name": "Obsidian", "bundle_id": "md.obsidian", "window_title": "Safe"},
        ), patch("activity_mvp.macos_probe._accessibility_context", return_value={}), patch(
            "activity_mvp.macos_probe._browser_url",
            return_value={"url": "", "domain": "", "source": "test"},
        ):
            batch = capture_sck_keyframe_batch(self.root, should_process=should_process)

        self.assertEqual(submit.call_count, 1)
        self.assertEqual([item["reason"] for item in batch["skipped"]], ["duplicate_frame"])
        self.assertEqual(len(batch["events"]), 1)
        self.assertTrue(batch["events"][0]["preflight_blocked"])
        self.assertIn(OCR_PROCESS_FAILED, [item["reason"] for item in batch["errors"]])
        self.assertTrue(all(not path.exists() for path in (duplicate, blocked, failed)))

    def test_one_display_failure_does_not_cancel_successful_sibling(self) -> None:
        failed = private_frame(self.root, "display-failed.png")
        succeeded = private_frame(self.root, "display-succeeded.png")

        def submit(_root, _command, path: Path) -> ImmediateTask:
            if Path(path) == failed:
                return ImmediateTask(OcrOutcome(error_code=OCR_PROCESS_FAILED))
            return ImmediateTask(
                OcrOutcome(
                    payload={
                        "lines": [{"text": "sibling success", "confidence": 0.9}],
                        "coordinate_space": "vision",
                    }
                )
            )

        with patch("activity_mvp.macos_probe.helper_command", return_value=["sck"]), patch(
            "activity_mvp.macos_probe.subprocess.run",
            return_value=capture_result([failed, succeeded]),
        ), patch(
            "activity_mvp.macos_probe.compiled_helper_lease",
            return_value=nullcontext(["/private/vision_ocr"]),
        ), patch("activity_mvp.macos_probe.submit_ocr", side_effect=submit) as submitted, patch(
            "activity_mvp.macos_probe._frontmost_context",
            return_value={"app_name": "Obsidian", "bundle_id": "md.obsidian", "window_title": "Safe"},
        ), patch("activity_mvp.macos_probe._accessibility_context", return_value={}), patch(
            "activity_mvp.macos_probe._browser_url",
            return_value={"url": "", "domain": "", "source": "test"},
        ):
            batch = capture_sck_keyframe_batch(self.root, should_process=lambda _metadata: True)

        self.assertEqual(submitted.call_count, 2)
        self.assertEqual([event["ocr_text"] for event in batch["events"]], ["sibling success"])
        self.assertIn(OCR_PROCESS_FAILED, [item["reason"] for item in batch["errors"]])
        self.assertFalse(failed.exists())
        self.assertFalse(succeeded.exists())

    def test_submission_exception_cancels_started_task_and_cleans_every_frame(self) -> None:
        first = private_frame(self.root, "first.png")
        second = private_frame(self.root, "second.png")

        class TrackingTask(ImmediateTask):
            def __init__(self) -> None:
                super().__init__(OcrOutcome(error_code=OCR_CANCELED))
                self.canceled = False
                self.waited = False

            def cancel(self) -> None:
                self.canceled = True

            def done(self) -> bool:
                return False

            def wait_terminal(self) -> None:
                self.waited = True

        tracking = TrackingTask()
        submissions = 0

        def submit(_root, _command, _path):
            nonlocal submissions
            submissions += 1
            if submissions == 1:
                return tracking
            raise RuntimeError("synthetic submit failure")

        with patch("activity_mvp.macos_probe.helper_command", return_value=["sck"]), patch(
            "activity_mvp.macos_probe.subprocess.run",
            return_value=capture_result([first, second]),
        ), patch(
            "activity_mvp.macos_probe.compiled_helper_lease",
            return_value=nullcontext(["/private/vision_ocr"]),
        ), patch("activity_mvp.macos_probe.submit_ocr", side_effect=submit), patch(
            "activity_mvp.macos_probe._frontmost_context",
            return_value={"app_name": "Obsidian", "bundle_id": "md.obsidian", "window_title": "Safe"},
        ), patch("activity_mvp.macos_probe._accessibility_context", return_value={}), patch(
            "activity_mvp.macos_probe._browser_url",
            return_value={"url": "", "domain": "", "source": "test"},
        ):
            with self.assertRaises(RuntimeError):
                capture_sck_keyframe_batch(self.root, should_process=lambda _metadata: True)

        self.assertTrue(tracking.canceled)
        self.assertTrue(tracking.waited)
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())



def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


if __name__ == "__main__":
    unittest.main()
