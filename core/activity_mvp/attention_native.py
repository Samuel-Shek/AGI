"""Version-checked local Codex transport. Never invokes a model outside its UI.

app-server is used only for durable metadata/history; the desktop owner receives
the user turn through its IPC transport and retains approvals and tool handling.
"""
from __future__ import annotations

import json
import os
import queue
import re
import socket
import stat
import struct
import subprocess
import threading
import time
import uuid
from pathlib import Path

# Transport adapted from Sync Center's receipt-verified desktop handoff.
# Target identity is supplied by the private local installation configuration.
PROJECT_ID = ""
PROJECT_NAME = "AGI"
CODEX_DIRECTORY = Path.home() / ".codex"
PROJECT_ROOT = Path.home() / "Code/active/personal-ai-monitor"
APP_PATH = Path("/Applications/ChatGPT.app")
BINARY = APP_PATH / "Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"


def configure_target(target):
    global PROJECT_ID, PROJECT_ROOT
    if not isinstance(target.get("project_id"), str) or not target["project_id"]:
        raise NativeError("codex_project_unavailable")
    root = Path(target.get("root", ""))
    if not root.is_absolute() or not root.is_dir():
        raise NativeError("codex_project_unavailable")
    PROJECT_ID, PROJECT_ROOT = target["project_id"], root.resolve()


THREAD_RE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")


class NativeError(RuntimeError):
    def __init__(self, code, message=None, *, rpc_error=None):
        super().__init__(message or code)
        self.code = code
        self.rpc_error = rpc_error


def thread_id(value):
    if not isinstance(value, str) or not THREAD_RE.fullmatch(value):
        raise NativeError("invalid_thread_id")
    return value


def missing_thread(error, identifier):
    """Only an exact metadata rejection qualifies, never a connection failure."""
    return (isinstance(error, NativeError) and error.code == "codex_rpc_rejected" and
            isinstance(error.rpc_error, dict) and error.rpc_error.get("code") == -32600 and
            error.rpc_error.get("message") in
            {"thread " + state + ": " + thread_id(identifier) for state in ("not loaded", "not found")})


class AppServer:
    def __init__(self, target=None, max_duration=None):
        # Legacy callers keep their configured target. New callers hold an
        # immutable per-instance target so concurrent queues cannot cross-route.
        if target is not None and not Path(target.get("root", "")).is_absolute():
            raise NativeError("codex_project_unavailable")
        self.project_id = PROJECT_ID if target is None else target.get("project_id")
        self.explicit_target = target is not None
        self.deadline = time.monotonic() + max_duration if max_duration is not None else None
        self.project_root = PROJECT_ROOT.resolve() if target is None else Path(target.get("root", "")).resolve()
        if not self.project_root.is_dir():
            raise NativeError("codex_project_unavailable")

    def __enter__(self):
        if not BINARY.is_file():
            raise NativeError("codex_missing")
        self.messages = queue.Queue()
        self.sequence = 0
        env = dict(os.environ)
        for key in ("CODEX_THREAD_ID", "CODEX_TURN_ID", "NODE_REPL_REQUEST_META"):
            env.pop(key, None)
        self.process = subprocess.Popen(
            [str(BINARY), "app-server", "--stdio"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", env=env,
        )
        def reader():
            try:
                for line in self.process.stdout:
                    self.messages.put(json.loads(line))
            finally:
                self.messages.put(None)
        threading.Thread(target=reader, daemon=True).start()
        try:
            self.call("initialize", {"clientInfo": {"name": "agi_attention_handoff", "version": "1.0.0"},
                                     "capabilities": {"experimentalApi": True}})
            self.process.stdin.write('{"method":"initialized","params":{}}\n')
            self.process.stdin.flush()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def call(self, method, params, timeout=25):
        if self.deadline is not None:
            timeout = min(timeout, self.deadline - time.monotonic())
            if timeout <= 0: raise NativeError("codex_rpc_timeout")
        self.sequence += 1
        request_id = self.sequence
        self.process.stdin.write(json.dumps({"id": request_id, "method": method, "params": params}) + "\n")
        self.process.stdin.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                result = self.messages.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty as exc:
                raise NativeError("codex_rpc_timeout") from exc
            if result is None:
                raise NativeError("codex_rpc_closed")
            if result.get("id") == request_id:
                if "error" in result:
                    raise NativeError("codex_rpc_rejected", str(result["error"]), rpc_error=result["error"])
                return result["result"]
            if time.monotonic() >= deadline:
                raise NativeError("codex_rpc_timeout")

    def __exit__(self, *_):
        close_timeout = 0.5 if self.deadline is not None else 4
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=close_timeout)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=close_timeout)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=close_timeout)
        self.process.stdout.close()

    def project(self):
        if not self.project_id:
            if not self.explicit_target:
                raise NativeError("codex_project_unavailable")
            result = self.call("project/list", {"limit": 100})
            matches = [p for p in result.get("data", []) if any(r.get("path") == str(self.project_root) for r in p.get("roots", []))]
            if len(matches) > 1 or result.get("nextCursor"):
                raise NativeError("codex_project_ambiguous")
            if not matches:
                return None  # Explicit projectless target; never defaults to AGI.
            self.project_id = matches[0]["id"]
        value = self.call("project/read", {"projectId": self.project_id})["project"]
        roots = [str(Path(row["path"]).resolve()) for row in value.get("roots", [])]
        if str(self.project_root) not in roots:
            raise NativeError("codex_project_mismatch")
        return self.project_id

    def read(self, identifier):
        return self.call("thread/read", {"threadId": thread_id(identifier), "includeTurns": True})["thread"]

    def active_threads(self, exclude=None):
        """Bounded metadata/owner check for this exact target, no history body."""
        page = self.call("thread/list", {"cwd": str(self.project_root), "archived": False,
                                        "limit": 20, "sortKey": "updated_at"})
        if page.get("nextCursor"): raise NativeError("coordination_window_incomplete")
        conflicts = []
        for item in page.get("data", []):
            identifier = item.get("id")
            if identifier == exclude: continue
            if not item.get("cwd") or Path(item["cwd"]).resolve() != self.project_root: continue
            try:
                with DesktopIPC() as ipc: live = ipc.snapshot(identifier, expected_cwd=self.project_root)
            except NativeError as exc:
                # A missing live desktop owner cannot certify an external CLI
                # writer. Keep the receipt's limited coverage explicit.
                if exc.code == "codex_ipc_rejected" and "no-client-found" in str(exc): continue
                raise NativeError("target_coordination_unknown") from exc
            if live.get("activity") not in {"idle", "notLoaded"}: conflicts.append(identifier)
        return conflicts

    def initial_collaboration_mode(self):
        """Resolve first-turn defaults before the desktop can inherit an empty mode."""
        try:
            result = self.call("config/read", {"cwd": str(self.project_root), "includeLayers": False})
        except NativeError as exc:
            raise NativeError("codex_settings_unavailable") from exc
        config = result.get("config") if isinstance(result, dict) else None
        model = config.get("model") if isinstance(config, dict) else None
        effort = config.get("model_reasoning_effort") if isinstance(config, dict) else None
        if (not isinstance(model, str) or not model.strip() or not isinstance(effort, str)
                or effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}):
            raise NativeError("codex_settings_unavailable")
        return {"mode": "default", "settings": {"model": model, "reasoning_effort": effort,
                                                  "developer_instructions": None}}

    def create(self, native_project_id, name, on_created):
        result = self.call("thread/start", {"cwd": str(self.project_root), "projectId": native_project_id,
                                           "ephemeral": False})
        identifier = thread_id(result["thread"]["id"])
        on_created(identifier)
        # No turn is started here. This process exits before the desktop resumes.
        self.call("thread/name/set", {"threadId": identifier, "name": name})
        return identifier


class DesktopIPC:
    def __enter__(self):
        path = CODEX_DIRECTORY / "ipc/ipc.sock"
        try:
            info, parent = path.lstat(), path.parent.lstat()
        except OSError as exc:
            raise NativeError("codex_not_ready") from exc
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
                or parent.st_uid != os.getuid() or info.st_mode & 0o077 or parent.st_mode & 0o077):
            raise NativeError("codex_ipc_ownership")
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(6)
        self.client_id = "initializing-client"
        try:
            self.socket.connect(str(path))
            result = self.call("initialize", {"clientType": "agi-attention-handoff"}, version=0)
            self.client_id = result["result"]["clientId"]
        except BaseException:
            self.socket.close()
            raise
        return self

    def read_exact(self, length):
        result = b""
        while len(result) < length:
            value = self.socket.recv(length - len(result))
            if not value:
                raise NativeError("codex_ipc_closed")
            result += value
        return result

    def call(self, method, params, *, version, target=None, timeout=6):
        identifier = str(uuid.uuid4())
        message = {"type": "request", "requestId": identifier, "sourceClientId": self.client_id,
                   "version": version, "method": method, "params": params, "timeoutMs": int(timeout * 1000)}
        if target:
            message["targetClientId"] = target
        data = json.dumps(message, ensure_ascii=False).encode()
        self.socket.sendall(struct.pack("<I", len(data)) + data)
        deadline = time.monotonic() + timeout + 1
        while True:
            if time.monotonic() >= deadline:
                raise NativeError("codex_ipc_timeout")
            self.socket.settimeout(max(0.1, deadline - time.monotonic()))
            size = struct.unpack("<I", self.read_exact(4))[0]
            if size > 8 * 1024 * 1024:
                raise NativeError("codex_ipc_incompatible")
            response = json.loads(self.read_exact(size))
            if response.get("requestId") == identifier:
                if response.get("resultType") != "success":
                    raise NativeError("codex_ipc_rejected", str(response.get("error", "unknown")))
                return response

    def owner(self, identifier):
        response = self.call("thread-owner-discovery", {"hostId": "local", "conversationId": thread_id(identifier)}, version=1)
        if not response.get("handledByClientId") or response.get("result", {}).get("supportsUntrustedAppInput") is not True:
            raise NativeError("codex_ipc_incompatible")
        return response["handledByClientId"]

    def snapshot(self, identifier, *, expected_cwd=None, message_id=None, turn_id=None, current_turn=False):
        """Read only the current desktop owner's bounded stream snapshot.

        Persisted app-server status is reconstructed by a different process and
        cannot certify live execution. Discard all conversation text here.
        """
        if current_turn and turn_id is not None:
            raise NativeError("codex_snapshot_turn_selector_conflict")
        identifier = thread_id(identifier)
        owner = self.owner(identifier)
        message = {"type": "broadcast", "sourceClientId": self.client_id,
                   "version": 1, "method": "thread-stream-following-changed",
                   "targetClientIds": [owner], "params": {"hostId": "local",
                   "conversationId": identifier, "following": True}}
        data = json.dumps(message).encode()
        self.socket.sendall(struct.pack("<I", len(data)) + data)
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            self.socket.settimeout(max(0.1, deadline - time.monotonic()))
            size = struct.unpack("<I", self.read_exact(4))[0]
            if size > 8 * 1024 * 1024: raise NativeError("codex_snapshot_too_large")
            response = json.loads(self.read_exact(size))
            params = response.get("params", {})
            if (response.get("method") != "thread-stream-state-changed"
                    or response.get("sourceClientId") != owner
                    or params.get("conversationId") != identifier): continue
            if response.get("version") != 11 or params.get("change", {}).get("type") != "snapshot":
                raise NativeError("codex_snapshot_incompatible")
            state = params["change"].get("conversationState", {})
            if state.get("id") != identifier: raise NativeError("codex_snapshot_target_mismatch")
            if expected_cwd and (not state.get("cwd") or Path(state["cwd"]).resolve() != Path(expected_cwd).resolve()):
                raise NativeError("codex_snapshot_target_mismatch")
            if message_id and state.get("latestTurnStartMessageId") != message_id:
                return {"activity": "unknown", "reason": "newer_turn", "observed_at": time.time()}
            turns = state.get("turns", [])
            latest = turns[-1] if turns else None
            history_state = state.get("turnHistory", {}).get("history", {})
            history = history_state.get("entitiesByKey", {})
            if current_turn:
                # The desktop's paginated stream can keep every turn in the
                # history tail while `turns` is empty. Only an exhausted newer
                # boundary identifies the authoritative end; dict order and
                # UUID order do not certify the current turn.
                if latest is None:
                    tails = [island for island in history_state.get("islands", [])
                             if island.get("newerBoundary", {}).get("status") == "exhausted"]
                    if len(tails) != 1 or not tails[0].get("entries"):
                        raise NativeError("codex_snapshot_current_turn_unavailable")
                    latest = history.get(tails[0]["entries"][-1].get("value"))
                if not isinstance(latest, dict):
                    raise NativeError("codex_snapshot_current_turn_unavailable")
                turn_id = (latest.get("turnId") or latest.get("id")) if latest else None
            current_turn_matches = bool(latest and (latest.get("turnId") or latest.get("id")) == turn_id)
            turns = turns + list(history.values())
            turn = (latest if current_turn else
                    next((t for t in turns if (t.get("turnId") or t.get("id")) == turn_id), None)) if turn_id else None
            runtime = state.get("threadRuntimeStatus", {})
            return {"activity": runtime.get("type", "unknown"), "turn_status": turn.get("status") if turn else None,
                    "turn_id": turn_id, "observed_at": time.time(), "source": "desktop_owner",
                    "current_turn_matches": current_turn_matches,
                    "usage": state.get("latestTokenUsageInfo")}
        raise NativeError("codex_snapshot_timeout")

    def submit(self, identifier, owner, message_id, prompt, *, collaboration_mode=None):
        request = {"threadId": thread_id(identifier),
                   "input": [{"type": "text", "text": prompt, "text_elements": []}],
                   "clientUserMessageId": message_id}
        if collaboration_mode is not None:
            request["collaborationMode"] = collaboration_mode
        return self.call("thread-follower-start-turn", {
            "conversationId": thread_id(identifier),
            "turnStart": {"request": request,
                          "context": {"inheritThreadSettings": True}},
        }, version=2, target=owner, timeout=40)

    def __exit__(self, *_):
        self.socket.close()


def open_thread(identifier, timeout=10):
    if not APP_PATH.is_dir():
        raise NativeError("codex_missing")
    try:
        subprocess.run(["/usr/bin/open", "-a", str(APP_PATH), "codex://threads/" + thread_id(identifier)],
                       check=True, timeout=timeout, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (subprocess.SubprocessError, OSError) as exc:
        raise NativeError("codex_open_failed") from exc


def wait_owner(identifier, timeout=25):
    deadline = time.monotonic() + timeout
    while True:
        try:
            with DesktopIPC() as ipc:
                return ipc.owner(identifier)
        except (NativeError, OSError) as exc:
            if isinstance(exc, NativeError) and exc.code in {"codex_ipc_ownership", "codex_ipc_incompatible"}:
                raise
            if time.monotonic() >= deadline:
                raise NativeError("codex_not_ready")
            time.sleep(0.5)


def receipt_in_thread(thread, message_id, prompt):
    """Require the exact submitted user text in persisted native history."""
    for turn in thread.get("turns", []):
        for item in turn.get("items", []):
            if item.get("type") != "userMessage":
                continue
            content = item.get("content", [])
            text = "".join(row.get("text", "") for row in content if row.get("type") in {"text", "inputText"})
            if text == prompt and item.get("clientId") == message_id:
                return {"turn_id": turn.get("id"), "user_message_id": item.get("id"),
                        "native_status": turn.get("status")}
    return None
