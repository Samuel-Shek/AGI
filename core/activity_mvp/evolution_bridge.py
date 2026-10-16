"""Native AGI client and one idle-bounded, local-only evolution host.

No user profile/plugin import, automatic source observation, model or production
worker. Starting this control host does not activate a recurring observation.
"""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import signal
import stat
import subprocess
import sys
import threading
import time

CORE = Path(__file__).resolve().parent.parent
ENGINE = CORE / "evolution"
sys.path.insert(0, str(ENGINE))
from ai_ceo.controller import Controller, digest
from ai_ceo.policy import GateDenied, iso
from ai_ceo.server import Handler, serve
from ai_ceo.observers import _safe_directory
from . import agi_bridge

ACTIONS = {"status", "replay", "pause", "stop", "resume", "refresh-context"}


class AGIController(Controller):
    def __init__(self, state, database):
        super().__init__(state)
        self.database = Path(database)

    def _active(self):
        super()._active()
        if self.store.state_get("agi_local_paused", False):
            raise GateDenied("paused", "自主进化已暂停。")

    def refresh_context(self):
        with self.lock:
            self._active()
            conn = agi_bridge.open_private(self.database)
            try:
                result = agi_bridge.consume_candidates(conn, self._use_context)
            finally:
                conn.close()
            self.store.append_audit("agi_context_analyzed", {
                "provided_packages": result["provided_packages"],
                "cited_count": len(result["cited_references"]), "model_calls": 0})
            return result

    def _use_context(self, item, share_key):
        # This classifier is conservative, bounded and never interprets commands.
        self._active()
        summary = item["summary"][:80]
        kind = "investigate" if any(word in summary for word in ("重复", "故障", "失败", "耗时", "手动", "卡顿")) else "observe"
        reference = item["reference"]
        revision = int(self.store.state_get("grant_revision:pam-context", 1))
        self.store.state_set("grant_revision:pam-context", revision)
        task = self.store.enqueue(digest(["agi-context-v1", reference, share_key]),
            "核对一条获准工作线索" if kind == "investigate" else "观察一条获准工作线索", "pam-context",
            {"context_reference": reference, "share_key": share_key, "expires_at": item["expires_at"],
             "source_kind": item["source_kind"], "decision": kind,
             "task_kind": "observation_analysis", "evidence_quality": "owner_statement",
             "next_step": "补充问题频率、影响与耗时证据；暂不修改应用。"})
        if task["created"]:
            attempt = self.store.claim(task["id"], revision, self.epoch)
            if not attempt or not self.store.complete(task["id"], attempt["attempt_id"], self.epoch, revision,
                {"task_kind": "observation_analysis", "phase": "observed", "decision": kind,
                 "analysis_completed": True, "model_used": False, "production_changed": False,
                 "improvement_verified": False}):
                raise GateDenied("context_use_not_committed", "引用未完成，未确认使用。")

    def snapshot(self):
        with self.lock:
            value = super().snapshot()
            try:
                conn = agi_bridge.open_private(self.database, readonly=True)
                try:
                    refs = agi_bridge.authorized_references(conn)
                finally:
                    conn.close()
                context_state = "available" if refs else "empty"
            except Exception:
                refs = None
                context_state = "unavailable"
            for task in value["tasks"]:
                if task["asset_id"] != "pam-context":
                    continue
                reference = task["payload"].get("context_reference")
                valid = refs is not None and reference in refs
                task["source_status"] = "authorized" if valid else ("unknown" if refs is None else "invalidated")
                if not valid:
                    task["phase"] = "source_unavailable" if refs is None else "evidence_invalidated"
            value["product"] = "AGI"
            value["local_analysis_paused"] = bool(self.store.state_get("agi_local_paused", False))
            value["context"] = {"state": context_state, "authorized_references": len(refs or {}),
                "consumer": "agi", "raw_access": False, "read_for_analysis": "explicit_action_only",
                "helpfulness": "owner_feedback_only"}
            value["assets"] = [a if a["id"] != "pam" else {"id": "pam", "name": "工作上下文",
                "status": context_state, "reason": "仅使用你单独共享给 AGI 的有效摘要。"} for a in value["assets"]]
            value["boundaries"] = ["原摘要分析仍不调用模型；批评与改进使用独立授权与验收。", "摘要按接收方分别授权；任务只保存无正文引用。",
                "共享摘要是有时间的用户说明，不是执行指令。", "回放通过不代表实际工作已改善。"]
            # Resume restores only local context analysis, never screen/recurring grants.
            value["controls"]["can_resume"] = True
            return value

    def control(self, action, asset_id=None):
        if action == "pause" and asset_id is None:
            with self.lock:
                self.store.state_set("agi_local_paused", True)
            return super().control(action, asset_id)
        if action == "resume" and asset_id is None:
            with self.lock:
                marker = self.state_dir / "STOP"
                if marker.exists():
                    marker.unlink()
                self.store.state_set("agi_local_paused", False)
                self.mode = "paused"
                self.store.state_set("mode", "paused")
                self.store.append_audit("operator_resume_local_analysis", {"recurring": False, "model_calls": 0})
                return {"ok": True, "mode": "paused", "scope": "manual_local_analysis_only"}
        return super().control(action, asset_id)


class AGIHandler(Handler):
    def do_GET(self):
        self.server.last_activity = time.monotonic()
        super().do_GET()

    def do_POST(self):
        self.server.last_activity = time.monotonic()
        if self.path != "/api/context/refresh":
            return super().do_POST()
        if not self._boundary(mutation=True) or not self._authenticated():
            return
        body = self._body()
        if body is None:
            return
        if body:
            return self._error(400, "unexpected_fields", "不接受外部摘要或命令。")
        try:
            self._json(200, self.server.controller.refresh_context())
        except PermissionError:
            self._error(403, "action_not_authorized", "已停止或没有有效授权。")
        except Exception:
            self._error(503, "context_unavailable", "摘要来源暂不可用；没有扩大读取范围。")


def paths():
    # The native app selects its core directory through the existing launch path.
    return CORE / "data" / "agi-evolution", CORE / "data" / "activity.sqlite"


def _private_root(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = _safe_directory(path)
    info = os.fstat(fd)
    os.close(fd)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("private_state_required")


def host(state, database):
    controller = AGIController(state, database)
    server = serve(controller)
    server.RequestHandlerClass = AGIHandler
    server.last_activity = time.monotonic()
    entry = state / "native-endpoint.json"
    temp = state / (".endpoint-" + secrets.token_hex(8))
    metadata = {"schema_version": 1, "pid": os.getpid(), "port": server.server_address[1],
                "token": server.access_token, "core": str(CORE), "engine": "agi-local-v1"}
    fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as out:
        json.dump(metadata, out)
    os.replace(temp, entry)
    def shutdown(*_args):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    def idle_watch():
        while not stopped.wait(5):
            if time.monotonic() - server.last_activity > 120:
                shutdown()
                break
    stopped = threading.Event()
    threading.Thread(target=idle_watch, daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        stopped.set()
        server.server_close()
        controller.close()
        # Remove only this process's endpoint; never another host's entry.
        try:
            if json.loads(entry.read_text()).get("pid") == os.getpid():
                entry.unlink()
        except (OSError, ValueError):
            pass


def _entry(state):
    path = state / "native-endpoint.json"
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid() or info.st_size > 2048:
            raise ValueError("invalid_endpoint")
        value = json.load(source)
    if (value.get("schema_version") != 1 or value.get("core") != str(CORE)
            or value.get("engine") != "agi-local-v1" or type(value.get("port")) is not int
            or not 1 <= value["port"] <= 65535 or not isinstance(value.get("token"), str)
            or len(value["token"]) != 43):
        raise ValueError("invalid_endpoint")
    return value


def _call(entry, action):
    port = entry["port"]
    origin = f"http://127.0.0.1:{port}"
    route, body = ("/api/state", None) if action == "status" else (
        ("/api/context/refresh", {}) if action == "refresh-context" else
        (("/api/replay", {}) if action == "replay" else ("/api/control", {"action": action})))
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=35 if action == "replay" else 5)
    try:
        conn.request("GET" if body is None else "POST", route,
            body=None if body is None else json.dumps(body),
            headers={"X-AI-CEO-Token": entry["token"], "Origin": origin, "Content-Type": "application/json"})
        response = conn.getresponse()
        data = response.read(2_000_001)
        if len(data) > 2_000_000:
            raise ValueError("response_too_large")
        value = json.loads(data)
        if response.status != 200:
            return {"status": "rejected", "reason": "control_unavailable_or_not_authorized"}
        return value
    finally:
        conn.close()


def request(action):
    if action not in ACTIONS:
        raise ValueError("unsupported_action")
    state, database = paths()
    # Initialize the known Store before adding host metadata or a startup lock.
    if not state.exists():
        from ai_ceo.storage import Store
        Store(state).close()
    _private_root(state)
    if action == "stop":
        from ai_ceo.cli import signal_stop
        signal_stop(state)
    fd = os.open(state / ".native-start.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        # Bounded lock: two UI clients never spawn parallel controllers.
        deadline = time.monotonic() + 3
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("startup_busy")
                time.sleep(0.05)
        try:
            entry = _entry(state)
            _call(entry, "status")
        except (OSError, ValueError, http.client.HTTPException):
            subprocess.Popen([sys.executable, "-I", "-B", "-c",
                "import sys;sys.path.insert(0,sys.argv[1]);from activity_mvp.evolution_bridge import main;main()",
                str(CORE), "host"], cwd=CORE, env={"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"},
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
            deadline = time.monotonic() + 3
            while True:
                try:
                    entry = _entry(state)
                    _call(entry, "status")
                    break
                except (OSError, ValueError, http.client.HTTPException):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("host_unavailable")
                    time.sleep(0.1)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    # Never automatically retry an action whose effect may be unknown.
    return _call(entry, action)


def main():
    action = sys.argv[-1]
    try:
        if action == "host":
            state, database = paths()
            host(state, database)
            return
        from .presentation_scope import project_for_runtime
        _, database = paths()
        print(json.dumps(project_for_runtime(request(action), Path(database).parent), ensure_ascii=False, allow_nan=False))
    except Exception:
        print(json.dumps({"status": "unavailable", "reason": "local_evolution_unavailable", "raw_access": False}))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
