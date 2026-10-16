"""Control-owned gates. External text cannot create grants or attest isolation.

This release has no generated-code or production execution capability. A grant
document alone cannot change that compile-time boundary. Same-user file modes
are access hygiene, not claimed to be an OS security boundary against a worker.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
import secrets


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    return (value or utcnow()).astimezone(timezone.utc).isoformat()


class GateDenied(PermissionError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class ReadSession:
    """Explicit process-local, expiring, bounded observation authorization.

    Issued only by the local operator CLI, never from an observation, a model,
    a fixture, a persisted task, a web payload or a restored controller state.
    It grants no recurring work, model transmission, or production mutation.
    """
    root: Path
    expires_at: datetime
    remaining: int
    id: str = field(default_factory=lambda: secrets.token_hex(16))
    revision: int = 1

    @classmethod
    def for_operator(cls, root: Path, *, reads: int = 1, seconds: int = 300):
        if not 1 <= reads <= 10 or not 1 <= seconds <= 3600:
            raise ValueError("read session must be bounded to 1-10 reads and <=3600 seconds")
        root = Path(root).expanduser().absolute()
        if root.is_symlink() or not root.is_dir():
            raise ValueError("source must be an existing directory, not a symlink")
        return cls(root=root, expires_at=utcnow() + timedelta(seconds=seconds), remaining=reads)

    def valid(self) -> bool:
        return self.remaining > 0 and utcnow() < self.expires_at

    def consume(self) -> None:
        if not self.valid():
            raise GateDenied("read_grant_expired", "一次观察授权已用完或过期，需要新的明确授权。")
        self.remaining -= 1


UNAVAILABLE = {
    "model": "尚未批准模型通道、传输范围与费用预算。没有发起模型调用。",
    "worker": "尚未安装并验收独立执行身份和操作系统隔离；仅可准备任务包。",
    "production": "尚未登记并验收生产发布适配器，也没有对应类别授权。",
    "scheduler": "常驻观察尚未授权和安装；不会在后台自动接管业务任务。",
    "pam": "只能接入逐份确认的有效摘要；本版本未调用 PAM 上下文接口。",
    "pma": "应用身份及项目路径尚未确认。",
}


def require_capability(action: str) -> None:
    if action in UNAVAILABLE:
        raise GateDenied(f"{action}_disabled", UNAVAILABLE[action])
    if action not in {"observe_sanitized_health", "run_replay", "propose_change"}:
        raise GateDenied("unknown_action", "未登记的动作不能执行。")
