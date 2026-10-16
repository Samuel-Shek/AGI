"""Single controller for observation, local replay and durable control state."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading
import fcntl
import os
from datetime import datetime, timezone

from .policy import ReadSession, GateDenied, UNAVAILABLE, iso, require_capability
from .planning import decision_card
from .storage import Store

VERSION = "0.1.0"


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class Controller:
    def __init__(self, state_dir: Path, read_session: ReadSession | None = None):
        self.lock = threading.RLock()
        self.state_dir = Path(state_dir).absolute()
        self.store = Store(self.state_dir)
        self._singleton = os.open(self.state_dir / ".controller.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self._singleton, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self._singleton)
            self.store.close()
            raise GateDenied("controller_already_running", "该状态目录已有控制服务；请使用现有面板。") from None
        self.read_session = read_session
        self.epoch = int(self.store.state_get("epoch", 0)) + 1
        self.store.recover(self.epoch)
        # Persisted stop always survives startup. No grants survive process exit.
        self.mode = self.store.state_get("mode", "paused")
        self.startup_mode = self.mode
        if self.mode != "stopped":
            self.mode = "paused"
        self.store.state_set("mode", self.mode)
        if read_session:
            read_session.revision = int(self.store.state_get("grant_revision:sync-center", 0)) + 1
            self.store.state_set("grant_revision:sync-center", read_session.revision)
            self.store.state_set("revoked:sync-center", False)
            # A new explicit local read session can release an old observation
            # takeover; it never grants business writes or recurrent work.
            self.store.state_set("takeover:sync-center", False)
            self.store.append_audit("operator_read_session", {
                "asset_id": "sync-center", "revision": read_session.revision,
                "expires_at": iso(read_session.expires_at), "remaining": read_session.remaining,
                "scope": "fixed_source_sanitized_fields_only", "recurring": False,
            })
        self.store.append_audit("controller_started", {"epoch": self.epoch, "mode": self.mode,
                                                      "scheduler_installed": False})

    def close(self):
        with self.lock:
            self.store.close()
            fcntl.flock(self._singleton, fcntl.LOCK_UN)
            os.close(self._singleton)

    def _active(self):
        self._sync_stop_request()
        if self.mode == "stopped":
            raise GateDenied("stopped", "系统已停止；没有新的观察、回放或执行。")

    def _sync_stop_request(self):
        # Independently issued owner stop signal is consulted at every action
        # admission and snapshot, even when the CLI cannot acquire our lock.
        if (self.state_dir / "STOP").exists() and self.mode != "stopped":
            self.mode = "stopped"
            self.store.state_set("mode", "stopped")
            self.epoch += 1
            self.store.recover(self.epoch)
            self.store.append_audit("external_stop_applied", {"epoch": self.epoch})

    def _read_allowed(self):
        self._active()
        if self.store.state_get("revoked:sync-center", False):
            raise GateDenied("asset_revoked", "该资产的观察授权已撤销。")
        if self.store.state_get("takeover:sync-center", False):
            raise GateDenied("asset_taken_over", "该资产由你接管，AI 观察任务已暂停。")
        if self.read_session is None or not self.read_session.valid():
            raise GateDenied("read_grant_missing", "尚无当前进程的限次只读授权；没有读取业务来源。")

    def observe(self) -> dict:
        # Holding the lock makes admission/revocation linearizable. A bounded
        # file-only read already in progress may finish before stop returns.
        with self.lock:
            require_capability("observe_sanitized_health")
            self._read_allowed()
            self.read_session.consume()
            from .observers import collect_sync
            try:
                observation = collect_sync(self.read_session.root)
            except Exception as exc:
                if getattr(self, "service_runtime", None):
                    self.service_runtime.record_outcome(False, "source_read_failed")
                self.store.append_audit("observation_rejected", {"error_type": type(exc).__name__})
                raise GateDenied("source_read_failed", "无法安全读取约定的状态字段，已停止本次观察。") from None
            # No tainted input may change a permission, action or target.
            if observation.get("asset_id") != "sync-center":
                if getattr(self, "service_runtime", None):
                    self.service_runtime.record_outcome(False, "invalid_observation")
                raise GateDenied("invalid_observation", "来源适配器返回了错误的资产。")
            card = decision_card(observation)
            event_key = digest(["sync-center", observation.get("issue_key"),
                                observation.get("status"), observation.get("decision")])
            titles = {"fault": "核对同步中心当前异常", "unknown": "补充同步中心状态证据",
                      "waiting": "观察同步中心等待条件", "recovered": "保留修复，观察防复发",
                      "healthy": "保留同步中心现状"}
            item = self.store.enqueue(event_key, titles.get(observation.get("status"), "核对同步中心状态"),
                                      "sync-center", {"observation": observation, "decision_card": card,
                                                      "task_kind": "observation_analysis"})
            if item.get("created"):
                claimed = self.store.claim(item["id"], self.read_session.revision, self.epoch)
                if claimed:
                    accepted = self.store.complete(item["id"], claimed["attempt_id"], self.epoch,
                                                   self.read_session.revision, {
                        "analysis_completed": True, "production_changed": False,
                        "model_used": False, "improvement_verified": False,
                        "task_kind": "observation_analysis",
                        "phase": "no_change" if card["decision"] == "no_change" else "observed",
                        "decision": card["decision"], "status": observation["status"],
                    })
                    if not accepted:
                        if getattr(self, "service_runtime", None):
                            self.service_runtime.record_outcome(False, "completion_rejected")
                        raise GateDenied("completion_rejected", "任务回执已失效，未标记完成。")
            self.store.state_set("latest_observation", observation)
            self.store.state_set("observation_samples", int(self.store.state_get("observation_samples", 0)) + 1)
            if not self.store.state_get("baseline"):
                self.store.state_set("baseline", {"started_at": iso(), "natural_use_samples": 0,
                    "human_interventions": None, "saved_minutes": None,
                    "status": "insufficient", "window_days_proposed": 14,
                    "window_authorized": False})
            self.store.append_audit("observation_recorded", {"status": observation["status"],
                "task_id": item["id"], "new_task": bool(item.get("created")), "business_verified": False})
            if getattr(self, "service_runtime", None):
                self.service_runtime.record_outcome(True)
            return {"observation": observation, "task_id": item["id"], "created": item.get("created", False),
                    "decision_card": card, "remaining_reads": self.read_session.remaining}

    def replay(self) -> dict:
        with self.lock:
            self._active()
            require_capability("run_replay")
            from .verification import run_replay
            result = run_replay(self.state_dir)
            self.store.state_set("last_replay", result)
            self.store.append_audit("replay_completed", {"verdict": result["verdict"],
                "passed": result["passed"], "total": result["total"],
                "baseline_sha256": result["baseline_sha256"], "synthetic_only": True})
            return result

    def control(self, action: str, asset_id: str | None = None) -> dict:
        with self.lock:
            if action not in {"pause", "stop", "resume", "revoke", "takeover"}:
                raise GateDenied("unknown_control", "未登记的控制动作。")
            if action in {"revoke", "takeover"}:
                if asset_id != "sync-center":
                    raise GateDenied("unknown_asset", "该资产尚未接入，不能变更权限。")
                self.store.state_set(f"{'revoked' if action == 'revoke' else 'takeover'}:{asset_id}", True)
                self.store.state_set(f"grant_revision:{asset_id}",
                                     int(self.store.state_get(f"grant_revision:{asset_id}", 0)) + 1)
                if self.read_session:
                    self.read_session.remaining = 0
                self.epoch += 1
                self.store.recover(self.epoch)
            elif action == "resume":
                # Explicit local operator action may lift a persisted stop only
                # with a fresh still-valid read session. Never enables scheduling.
                if self.read_session is None or not self.read_session.valid():
                    raise GateDenied("resume_requires_grant", "恢复需要有效的当前只读授权；不会自行扩大权限。")
                if self.store.state_get("revoked:sync-center", False) or self.store.state_get("takeover:sync-center", False):
                    raise GateDenied("resume_asset_frozen", "资产已撤权或由你接管，需要新的明确授权。")
                stop_path = self.state_dir / "STOP"
                if stop_path.exists():
                    stop_path.unlink()  # explicitly requested resumption clears only our own stop marker
                self.mode = "observing"
                self.store.state_set("mode", self.mode)
            else:
                self.mode = "paused" if action == "pause" else "stopped"
                # pause must not clear a durable stop; resume is the sole path.
                if self.store.state_get("mode") == "stopped":
                    self.mode = "stopped"
                self.store.state_set("mode", self.mode)
                if action == "stop":
                    self.epoch += 1
                    self.store.recover(self.epoch)
            self.store.append_audit("operator_control", {"action": action, "asset_id": asset_id,
                                                        "epoch": self.epoch, "mode": self.mode})
            runtime = getattr(self, "service_runtime", None)
            scheduling = runtime.status()["enabled"] if runtime else False
            return {"ok": True, "mode": self.mode, "epoch": self.epoch, "scheduler_enabled": scheduling}

    def snapshot(self) -> dict:
        with self.lock:
            self._sync_stop_request()
            live = False
            try:
                self._read_allowed()
                live = True
            except GateDenied:
                pass
            runtime = getattr(self, "service_runtime", None)
            scheduler = runtime.status() if runtime else {
                "installed": False, "enabled": False, "runtime_started": False,
                "state": "not_running", "implemented": True}
            if runtime:
                live = live and scheduler["enabled"]
            latest = self.store.state_get("latest_observation")
            assets = [{"id": "sync-center", "name": "业务记忆同步中心",
                       "status": (latest or {}).get("status", "unknown"),
                       "reason": (latest or {}).get("reason_code", "尚无当前观察证据"),
                       "evidence_quality": (latest or {}).get("evidence_quality", "insufficient"),
                       "observed_at": (latest or {}).get("observed_at"),
                       "evidence": (latest or {}).get("evidence", []),
                       "revoked": bool(self.store.state_get("revoked:sync-center", False)),
                       "takeover": bool(self.store.state_get("takeover:sync-center", False))},
                      {"id": "pam", "name": "PAM", "status": "not_connected", "reason": UNAVAILABLE["pam"]},
                      {"id": "pma", "name": "PMA", "status": "unconfirmed", "reason": UNAVAILABLE["pma"]}]
            capabilities = [
                {"id": "live_observation", "name": "一次真实观察", "status": "enabled" if live else "disabled",
                 "reason": ("受已批准观察次数与间隔限制。" if runtime else "当前进程的限次授权有效；不启动循环观察。") if live else "需要有效且未暂停的当前观察授权。"},
                {"id": "replay", "name": "本地回放验证", "status": "enabled" if self.mode != "stopped" else "disabled",
                 "reason": "使用固定合成回放集，无业务写入和模型调用。"},
            ] + [{"id": key, "name": name, "status": "disabled", "reason": UNAVAILABLE[key]}
                 for key, name in [("model", "模型决策"), ("worker", "工程执行"),
                                   ("production", "生产发布")]]
            capabilities.append({"id": "scheduler", "name": "受限持续观察",
                "status": "enabled" if scheduler["enabled"] else "disabled",
                "reason": "只按已批准文件观察白名单字段。" if scheduler["enabled"] else "代码已准备；当前未启用，系统启动项未验证安装。"})
            return {"version": VERSION, "mode": self.mode, "stage": 1 if runtime else 0, "capabilities": capabilities,
                    "assets": assets, "tasks": self.store.list_tasks(), "updated_at": iso(),
                    "metrics": {"observation_samples": self.store.state_get("observation_samples", 0),
                                "model_calls": 0, "production_changes": 0, "saved_minutes": None,
                                "human_interventions": None, "natural_use_samples": 0,
                                "runtime_cost": None, "improvement_verified": False},
                    "baseline": self.store.state_get("baseline", {"status": "not_started", "natural_use_samples": 0}),
                    "last_replay": self.store.state_get("last_replay"),
                    "audit": self.store.audit_list()[-30:],
                    "boundaries": ["观察来源只读，业务数据库未接入写操作。",
                                   "判断来自可检查的规则；模型决策尚未接通。",
                                   "生产发布、模型外传和共享记忆写入均未接通。",
                                   "本地回放通过不代表自然使用价值已经验证。"],
                    "scheduler": scheduler, "epoch": self.epoch,
                    "controls": {"can_resume": bool(self.read_session and self.read_session.valid()
                        and not self.store.state_get("revoked:sync-center", False)
                        and not self.store.state_get("takeover:sync-center", False))}}
