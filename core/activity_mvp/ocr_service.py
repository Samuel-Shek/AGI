from __future__ import annotations

import atexit
import fcntl
import json
import os
import signal
import stat
import subprocess
import threading
import time
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


OCR_HELPER_UNAVAILABLE = "ocr_helper_unavailable"
OCR_SCHEDULER_UNAVAILABLE = "ocr_scheduler_unavailable"
OCR_INPUT_UNAVAILABLE = "ocr_input_unavailable"
OCR_INPUT_CHANGED = "ocr_input_changed"
OCR_SPAWN_FAILED = "ocr_spawn_failed"
OCR_TIMEOUT = "ocr_timeout"
OCR_PROCESS_FAILED = "ocr_process_failed"
OCR_OUTPUT_INVALID = "ocr_output_invalid"
OCR_OUTPUT_TOO_LARGE = "ocr_output_too_large"
OCR_CANCELED = "ocr_canceled"
OCR_ADMISSION_DEFERRED = "ocr_admission_deferred"

OCR_EXECUTION_TIMEOUT_SECONDS = 30.0
OCR_TERMINATION_GRACE_SECONDS = 0.5
OCR_MAX_OUTPUT_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class OcrOutcome:
    payload: dict | None = None
    error_code: str = ""

    @property
    def ok(self) -> bool:
        return self.payload is not None and not self.error_code


class OcrAdmissionDeferredError(RuntimeError):
    """Content-free control signal for a background OCR admission change."""


@dataclass(frozen=True)
class _FileIdentity:
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int


class _TaskControl:
    def __init__(self) -> None:
        self.cancel_requested = threading.Event()
        self.terminal = threading.Event()
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None

    def attach_process(self, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            self._process = process
            cancel_now = self.cancel_requested.is_set()
        if cancel_now:
            _signal_process_group(process, signal.SIGTERM)
            _schedule_forced_kill(process)

    def request_cancel(self) -> None:
        self.cancel_requested.set()
        with self._lock:
            process = self._process
        if process is not None and process.poll() is None:
            _signal_process_group(process, signal.SIGTERM)
            _schedule_forced_kill(process)

    def mark_terminal(self) -> None:
        with self._lock:
            self._process = None
        self.terminal.set()


class OcrTask:
    def __init__(self, future: Future[OcrOutcome], control: _TaskControl) -> None:
        self._future = future
        self._control = control

    def result(self) -> OcrOutcome:
        try:
            outcome = self._future.result()
        except CancelledError:
            outcome = OcrOutcome(error_code=OCR_CANCELED)
        except Exception:
            outcome = OcrOutcome(error_code=OCR_PROCESS_FAILED)
        self._control.terminal.wait()
        return outcome

    def cancel(self) -> None:
        self._control.request_cancel()
        if self._future.cancel():
            self._control.mark_terminal()

    def done(self) -> bool:
        return self._future.done() and self._control.terminal.is_set()

    def wait_terminal(self) -> None:
        try:
            self._future.result()
        except (CancelledError, Exception):
            pass
        self._control.terminal.wait()


class OcrService:
    """Process-wide OCR scheduler with a cross-process two-slot guard."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pam-ocr")
        self._live_batch_lock = threading.Lock()
        self._controls_lock = threading.Lock()
        self._controls: set[_TaskControl] = set()
        self._closed = False

    @contextmanager
    def live_batch(self):
        self._live_batch_lock.acquire()
        try:
            yield
        finally:
            self._live_batch_lock.release()

    def submit(
        self,
        root_dir: str | Path,
        command: list[str],
        image_path: str | Path,
        *,
        admission_check: Callable[[], bool | dict] | None = None,
    ) -> OcrTask:
        if self._closed:
            control = _TaskControl()
            control.mark_terminal()
            future: Future[OcrOutcome] = Future()
            future.set_result(OcrOutcome(error_code=OCR_SCHEDULER_UNAVAILABLE))
            return OcrTask(future, control)

        control = _TaskControl()
        with self._controls_lock:
            self._controls.add(control)
        identity = _file_identity(Path(image_path))
        future = self._executor.submit(
            self._run,
            Path(root_dir).resolve(),
            tuple(command),
            Path(image_path),
            identity,
            control,
            admission_check,
        )
        future.add_done_callback(
            lambda completed, current=control: self._finalize_future(
                completed,
                current,
            )
        )
        return OcrTask(future, control)

    def _finalize_future(
        self,
        future: Future[OcrOutcome],
        control: _TaskControl,
    ) -> None:
        # Executor callbacks are the only path that also runs when a queued
        # future is cancelled before _run starts.  The operations are
        # intentionally idempotent with _run's finally block.
        if future.cancelled():
            control.mark_terminal()
        with self._controls_lock:
            self._controls.discard(control)

    def _run(
        self,
        root: Path,
        command: tuple[str, ...],
        image_path: Path,
        identity: _FileIdentity | None,
        control: _TaskControl,
        admission_check: Callable[[], bool | dict] | None,
    ) -> OcrOutcome:
        try:
            if control.cancel_requested.is_set():
                return OcrOutcome(error_code=OCR_CANCELED)
            if identity is None:
                return OcrOutcome(error_code=OCR_INPUT_UNAVAILABLE)
            if not _matches_identity(image_path, identity):
                return OcrOutcome(error_code=OCR_INPUT_CHANGED)
            if len(command) != 1 or not Path(command[0]).is_absolute():
                return OcrOutcome(error_code=OCR_HELPER_UNAVAILABLE)
            try:
                with _cross_process_slot(root, control.cancel_requested) as slot_fd:
                    if control.cancel_requested.is_set():
                        return OcrOutcome(error_code=OCR_CANCELED)
                    if not _matches_identity(image_path, identity):
                        return OcrOutcome(error_code=OCR_INPUT_CHANGED)
                    if admission_check is not None and not _admission_allowed(admission_check):
                        return OcrOutcome(error_code=OCR_ADMISSION_DEFERRED)
                    return _run_process(command[0], image_path, root, control, slot_fd)
            except InterruptedError:
                return OcrOutcome(
                    error_code=OCR_CANCELED
                    if control.cancel_requested.is_set()
                    else OCR_SCHEDULER_UNAVAILABLE
                )
            except OSError:
                return OcrOutcome(error_code=OCR_SCHEDULER_UNAVAILABLE)
        finally:
            control.mark_terminal()
            with self._controls_lock:
                self._controls.discard(control)

    def shutdown(self) -> None:
        with self._controls_lock:
            if self._closed:
                return
            self._closed = True
            controls = tuple(self._controls)
        for control in controls:
            control.request_cancel()
        self._executor.shutdown(wait=True, cancel_futures=True)
        for control in controls:
            if not control.terminal.is_set():
                control.mark_terminal()


def _run_process(
    binary: str,
    image_path: Path,
    root: Path,
    control: _TaskControl,
    slot_fd: int,
) -> OcrOutcome:
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            [binary, str(image_path)],
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            pass_fds=(slot_fd,),
            start_new_session=True,
            cwd=root,
        )
        control.attach_process(process)
        if control.cancel_requested.is_set():
            _signal_process_group(process, signal.SIGTERM)
        try:
            stdout, _ = process.communicate(timeout=OCR_EXECUTION_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            _terminate_and_reap(process)
            return OcrOutcome(error_code=OCR_TIMEOUT)
        if control.cancel_requested.is_set():
            return OcrOutcome(error_code=OCR_CANCELED)
        if process.returncode != 0:
            return OcrOutcome(error_code=OCR_PROCESS_FAILED)
        if len(stdout) > OCR_MAX_OUTPUT_BYTES:
            return OcrOutcome(error_code=OCR_OUTPUT_TOO_LARGE)
        try:
            payload = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return OcrOutcome(error_code=OCR_OUTPUT_INVALID)
        if not isinstance(payload, dict) or not isinstance(payload.get("lines"), list):
            return OcrOutcome(error_code=OCR_OUTPUT_INVALID)
        return OcrOutcome(payload=payload)
    except (OSError, subprocess.SubprocessError):
        if process is not None:
            _terminate_and_reap(process)
        return OcrOutcome(error_code=OCR_SPAWN_FAILED)
    finally:
        if process is not None and process.poll() is None:
            _terminate_and_reap(process)
        if process is not None:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()


def _file_identity(path: Path) -> _FileIdentity | None:
    try:
        info = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        return None
    return _FileIdentity(
        device=info.st_dev,
        inode=info.st_ino,
        size=info.st_size,
        modified_ns=info.st_mtime_ns,
        changed_ns=info.st_ctime_ns,
    )


def _matches_identity(path: Path, expected: _FileIdentity) -> bool:
    current = _file_identity(path)
    return current == expected


def _admission_allowed(check: Callable[[], bool | dict]) -> bool:
    try:
        result = check()
    except Exception:
        return False
    if isinstance(result, dict):
        return bool(result.get("allowed"))
    return bool(result)


_LOCAL_SLOT_LOCK = threading.Lock()
_LOCAL_SLOT_RESERVATIONS: set[tuple[str, int]] = set()


@contextmanager
def _cross_process_slot(root: Path, cancel_requested: threading.Event):
    slot_dir = root / "runtime" / "ocr-scheduler"
    slot_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(slot_dir, 0o700)
    slot_namespace = str(slot_dir.resolve())
    selected = None
    selected_key: tuple[str, int] | None = None
    try:
        while selected is None:
            if cancel_requested.is_set():
                raise InterruptedError("ocr task canceled")
            for index in range(2):
                key = (slot_namespace, index)
                with _LOCAL_SLOT_LOCK:
                    if key in _LOCAL_SLOT_RESERVATIONS:
                        continue
                    _LOCAL_SLOT_RESERVATIONS.add(key)
                slot_path = slot_dir / f"slot-{index}.lock"
                try:
                    descriptor = os.open(
                        slot_path,
                        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                        0o600,
                    )
                    os.fchmod(descriptor, 0o600)
                    handle = os.fdopen(descriptor, "rb+", buffering=0)
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        handle.close()
                        with _LOCAL_SLOT_LOCK:
                            _LOCAL_SLOT_RESERVATIONS.discard(key)
                        continue
                except Exception:
                    with _LOCAL_SLOT_LOCK:
                        _LOCAL_SLOT_RESERVATIONS.discard(key)
                    raise
                selected = handle
                selected_key = key
                break
            if selected is None:
                cancel_requested.wait(0.05)
        # The helper inherits this exact flock file description. If the Python
        # parent is killed, the slot remains held until the helper exits.
        yield selected.fileno()
    finally:
        if selected is not None:
            try:
                fcntl.flock(selected.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                selected.close()
            except OSError:
                pass
        if selected_key is not None:
            with _LOCAL_SLOT_LOCK:
                _LOCAL_SLOT_RESERVATIONS.discard(selected_key)


def _signal_process_group(process: subprocess.Popen[bytes], requested_signal: signal.Signals) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, requested_signal)
    except (OSError, ProcessLookupError):
        pass


def _schedule_forced_kill(process: subprocess.Popen[bytes]) -> None:
    def kill_later() -> None:
        time.sleep(OCR_TERMINATION_GRACE_SECONDS)
        if process.poll() is None:
            _signal_process_group(process, signal.SIGKILL)

    threading.Thread(target=kill_later, name="pam-ocr-kill", daemon=True).start()


def _terminate_and_reap(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        _signal_process_group(process, signal.SIGTERM)
    try:
        process.communicate(timeout=OCR_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        _signal_process_group(process, signal.SIGKILL)
        try:
            process.communicate(timeout=OCR_TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


_SERVICE = OcrService()
atexit.register(_SERVICE.shutdown)


def ocr_service() -> OcrService:
    return _SERVICE


def submit_ocr(root_dir: str | Path, command: list[str], image_path: str | Path) -> OcrTask:
    return _SERVICE.submit(root_dir, command, image_path)


def run_ocr(
    root_dir: str | Path,
    command: list[str],
    image_path: str | Path,
    *,
    admission_check: Callable[[], bool | dict] | None = None,
) -> OcrOutcome:
    return _SERVICE.submit(
        root_dir,
        command,
        image_path,
        admission_check=admission_check,
    ).result()
