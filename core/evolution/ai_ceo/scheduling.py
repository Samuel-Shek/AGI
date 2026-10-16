"""Pure admission and wake/retry decisions, without starting background work."""
from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class TrialLimits:
    max_engineering_tasks: int = 1
    idle_seconds: int = 180
    min_free_bytes: int = 10 * 1024**3
    max_artifact_bytes: int = 2 * 1024**3
    max_revisions: int = 2
    max_network_retries: int = 2


def admit_background(snapshot: dict, *, estimated_work_bytes: int, limits=TrialLimits()) -> dict:
    # Unknown information prevents low-priority execution; it is not fabricated
    # as idle, plugged in, cool or sufficiently provisioned.
    required = {"ac_power", "idle_seconds", "free_bytes", "engineering_tasks", "artifact_bytes"}
    if not required <= snapshot.keys() or any(snapshot[k] is None for k in required):
        return {"allowed": False, "reason": "resource_evidence_missing"}
    numeric = [snapshot[k] for k in required if k != "ac_power"] + [estimated_work_bytes,
        limits.max_engineering_tasks, limits.idle_seconds, limits.min_free_bytes, limits.max_artifact_bytes]
    if any(type(v) not in {int, float} or not math.isfinite(v) or v < 0 for v in numeric):
        return {"allowed": False, "reason": "resource_evidence_invalid"}
    if snapshot["ac_power"] is not True:
        return {"allowed": False, "reason": "battery_power"}
    if snapshot["idle_seconds"] < limits.idle_seconds:
        return {"allowed": False, "reason": "user_active"}
    if snapshot["engineering_tasks"] >= limits.max_engineering_tasks:
        return {"allowed": False, "reason": "engineering_slot_busy"}
    if estimated_work_bytes < 0 or snapshot["free_bytes"] < max(limits.min_free_bytes, estimated_work_bytes * 2):
        return {"allowed": False, "reason": "insufficient_disk"}
    if snapshot["artifact_bytes"] + estimated_work_bytes > limits.max_artifact_bytes:
        return {"allowed": False, "reason": "artifact_budget"}
    return {"allowed": True, "reason": "resource_admission_only_not_authorization"}


def wake_decision(previous_boot, current_boot, last_tick, now, interval=60):
    if now < last_tick or previous_boot != current_boot:
        return {"reconcile_first": True, "checks_to_run": 1, "invalidate_old_leases": True}
    return {"reconcile_first": now - last_tick > interval * 2,
            "checks_to_run": 1 if now - last_tick >= interval else 0,
            "invalidate_old_leases": now - last_tick > interval * 2}


def retry_decision(attempts, *, effect_status, has_new_evidence, transient, limits=TrialLimits()):
    if effect_status in {"applying", "unknown"}:
        return {"retry": False, "reason": "reconcile_effect_first"}
    if attempts >= limits.max_network_retries:
        return {"retry": False, "reason": "retry_limit"}
    if not has_new_evidence and not transient:
        return {"retry": False, "reason": "no_new_evidence"}
    return {"retry": True, "delay_seconds": (5, 30)[min(attempts, 1)]}
