from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .ha_source import validate_package
from .memory_audit import memory_audit
from .memory_ledger import (
    FROZEN_LEDGER_MESSAGE,
    _detail_path,
    _hash_text,
    _human_memory_text,
    _ledger_path,
    _load_mem0_rows,
    _read_human_memory_lines,
    _read_ledger_entries,
    _run_mem0,
    _safe_text,
    _to_bool,
    _write_backup,
    memory_ledger_status,
)


APPROVAL_DIR_RELATIVE_PATH = Path("Library/Application Support/Personal AI Monitor/memory-approvals")
PLAN_TTL_MINUTES = 10
DEFAULT_LIMIT = 40
CONTROLLED_MEM0_WRITER_ENABLED = False
CONTROLLED_MEM0_WRITER_DISABLED_REASON = (
    "受控 mem0 writer 已关闭：尚未实现基于写入回执 ID 和完整来源证明的 "
    "delete/retention 精确撤销闭环。"
)
ALLOWED_SUBJECT_SCOPES = {"self_or_user_owned"}
HARD_BLOCK_SOURCE_CATEGORIES = {
    "target_role_jd",
    "third_party_resume",
    "ambiguous_document",
    "reference_only_document",
    "reference_only_job_document",
    "personal_medical_document",
}
HARD_BLOCK_FLAG_FRAGMENTS = {
    "third_party_subject",
    "ambiguous_subject",
    "subject_ownership_unclear",
    "target_role_jd",
    "reference_only",
    "third_party_resume",
    "ambiguous_document",
    "personal_medical",
    "blocked_personal_medical",
    "noise_or_reference",
    "file_listing_context",
    "task_list_context",
    "browser_tab_chrome",
    "system_ui_context",
    "read_only_context_app",
    "document_not_modified_near_capture",
    "screen_region_noise_enforced",
    "private_chat",
    "raw_ocr",
    "raw_screenshot",
    "screenshot_path",
    "token",
    "secret",
}
PLAN_ID_RE = re.compile(r"^[a-z0-9_-]{8,80}$", re.I)


def memory_approval_status(
    conn: sqlite3.Connection,
    *,
    package_id: str = "latest",
    limit: int = DEFAULT_LIMIT,
    plan_dir: str | Path | None = None,
) -> dict[str, Any]:
    ledger = memory_ledger_status(conn)
    audit = memory_audit(limit=50)
    candidates = memory_approval_candidates(conn, package_id=package_id, limit=limit)
    records = memory_approval_records(limit=8, plan_dir=plan_dir)
    return {
        "status": "ok",
        "raw_access": False,
        "destructive_actions": False,
        "ledger": _compact_ledger(ledger),
        "audit": _compact_audit(audit),
        "candidate_status": _compact_candidate_status(candidates),
        "current_memories": _current_memory_items(limit=limit),
        "approval_records": records["records"],
        "approval_record_count": records["record_count"],
        "approval_runtime": {
            "plan_ttl_minutes": PLAN_TTL_MINUTES,
            "mem0_available": _mem0_cli_path().exists(),
            "writer_enabled": CONTROLLED_MEM0_WRITER_ENABLED,
            "source_revocation_cascade_ready": False,
            "writer_disabled_reason": CONTROLLED_MEM0_WRITER_DISABLED_REASON,
            "plan_storage": "private_application_support",
        },
    }


def memory_approval_candidates(
    conn: sqlite3.Connection,
    *,
    package_id: str = "latest",
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    package = _load_candidate_package(conn, package_id)
    if package.get("source_status") != "active":
        return {
            "status": package.get("source_status", "missing"),
            "package_id": package.get("package_id") or package_id,
            "raw_access": False,
            "destructive_actions": False,
            "writer_enabled": CONTROLLED_MEM0_WRITER_ENABLED,
            "writer_disabled_reason": CONTROLLED_MEM0_WRITER_DISABLED_REASON,
            "memory_candidates": [],
            "review_only_candidates": [],
            "ephemeral_candidates": [],
            "eligible_candidate_count": 0,
            "review_only_candidate_count": 0,
            "ephemeral_candidate_count": 0,
            "total_candidate_count": 0,
            "message": package.get("reason") or "没有可读取的 HA 候选包。",
        }
    normalized = _normalized_package_candidates(package, limit=limit)
    return {
        "status": "ok",
        "package_id": package["package_id"],
        "source_status": package["source_status"],
        "time_range": package.get("time_range") or {},
        "raw_access": False,
        "destructive_actions": False,
        "writer_enabled": CONTROLLED_MEM0_WRITER_ENABLED,
        "writer_disabled_reason": CONTROLLED_MEM0_WRITER_DISABLED_REASON,
        "quarantine_status": package.get("quarantine_status") or "clear_only",
        "source": package.get("source") or "extracted_only",
        "memory_candidates": normalized["eligible"],
        "review_only_candidates": normalized["review_only"],
        "ephemeral_candidates": normalized["ephemeral"],
        "eligible_candidate_count": normalized["eligible_count"],
        "review_only_candidate_count": normalized["review_only_count"],
        "ephemeral_candidate_count": normalized["ephemeral_count"],
        "total_candidate_count": normalized["total_count"],
        "display_candidate_count": len(normalized["eligible"]) + len(normalized["review_only"]) + len(normalized["ephemeral"]),
    }


def preview_memory_approval(
    conn: sqlite3.Connection,
    *,
    action: str,
    ledger_id: str = "",
    replacement_text: str = "",
    package_id: str = "latest",
    candidate_key: str = "",
    memory_text: str = "",
    plan_dir: str | Path | None = None,
) -> dict[str, Any]:
    normalized_action = _normalize_approval_action(action)
    if normalized_action in {"delete", "rewrite"}:
        return _preview_ledger_action(
            conn,
            action=normalized_action,
            ledger_id=ledger_id,
            replacement_text=replacement_text,
            plan_dir=plan_dir,
        )
    if normalized_action in {"approve_candidate", "promote_review_only"}:
        return _preview_candidate_action(
            conn,
            action=normalized_action,
            package_id=package_id,
            candidate_key=candidate_key,
            memory_text=memory_text,
            plan_dir=plan_dir,
        )
    return _blocked_preview("unsupported_action", "不支持的记忆审批动作。")


def apply_memory_approval(
    conn: sqlite3.Connection,
    *,
    plan_id: str,
    plan_dir: str | Path | None = None,
) -> dict[str, Any]:
    plan_result = _load_plan(plan_id, plan_dir=plan_dir)
    if not plan_result.get("ok"):
        return _apply_failure(plan_id, plan_result.get("status", "plan_missing"), plan_result.get("message", "计划不存在。"))
    plan = plan_result["plan"]
    if _is_plan_expired(plan):
        return _apply_failure(plan_id, "plan_expired", "审批计划已过期，请重新预览。")
    action = plan.get("action") or ""
    if action in {"delete", "rewrite"}:
        # Historical ledger plans are invalidated without writing an approval
        # record, audit row, backup, ledger file, or mem0 mutation.
        return _apply_ledger_action(conn, plan, plan_dir=plan_dir)
    elif action in {"approve_candidate", "promote_review_only"}:
        result = _apply_candidate_action(conn, plan, plan_dir=plan_dir)
    else:
        result = _apply_failure(plan_id, "unsupported_action", "审批计划动作不受支持。")
    _append_approval_record(result, plan_dir=plan_dir)
    return result


def memory_approval_records(*, limit: int = 20, plan_dir: str | Path | None = None) -> dict[str, Any]:
    path = _records_path(plan_dir)
    records: list[dict[str, Any]] = []
    if path.exists():
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        for raw in reversed(lines):
            try:
                item = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                records.append(_public_record(item))
            if len(records) >= max(1, limit):
                break
    return {
        "status": "ok",
        "raw_access": False,
        "destructive_actions": False,
        "record_count": len(records),
        "records": records,
    }


def _preview_ledger_action(
    conn: sqlite3.Connection,
    *,
    action: str,
    ledger_id: str,
    replacement_text: str,
    plan_dir: str | Path | None,
) -> dict[str, Any]:
    return _blocked_preview("frozen_legacy_read_only", FROZEN_LEDGER_MESSAGE)


def _preview_candidate_action(
    conn: sqlite3.Connection,
    *,
    action: str,
    package_id: str,
    candidate_key: str,
    memory_text: str,
    plan_dir: str | Path | None,
) -> dict[str, Any]:
    if not CONTROLLED_MEM0_WRITER_ENABLED:
        return _blocked_preview("controlled_mem0_writer_disabled", CONTROLLED_MEM0_WRITER_DISABLED_REASON)
    package = _load_candidate_package(conn, package_id)
    if package.get("source_status") != "active":
        return _blocked_preview(package.get("source_status", "package_unavailable"), package.get("reason", "候选包不可用。"))
    candidate = _find_candidate(package, candidate_key)
    if not candidate:
        return _blocked_preview("candidate_missing", "没有找到这条候选记忆。")
    text = _safe_text(memory_text if action == "promote_review_only" else (memory_text or candidate["safe_summary"]), max_len=1200)
    gate = _candidate_gate(candidate, action=action, memory_text=text)
    if not gate["allowed"]:
        return _blocked_preview(gate["status"], gate["message"], candidate=candidate)
    duplicate = _duplicate_report(text)
    if duplicate["exact_duplicate"]:
        return _blocked_preview("exact_duplicate", "已有完全相同的 PAM 长期记忆，已阻止重复写入。", candidate=candidate)
    plan = _base_plan(action)
    plan.update(
        {
            "package_id": package["package_id"],
            "candidate_key": candidate["candidate_key"],
            "memory_text": text,
            "candidate": _candidate_plan_snapshot(candidate),
            "duplicate_report": duplicate,
        }
    )
    saved = _write_plan(plan, plan_dir=plan_dir)
    warning = "；发现相似记忆，请确认不是重复。" if duplicate["similar_duplicate_count"] else ""
    return {
        "status": "preview_ready",
        "plan_id": saved["plan_id"],
        "raw_access": False,
        "destructive_actions": False,
        "action": action,
        "summary": f"准备写入 1 条人工确认的安全摘要{warning}",
        "candidate": _public_candidate(candidate),
        "duplicate_report": duplicate,
    }


def _apply_ledger_action(conn: sqlite3.Connection, plan: dict[str, Any], *, plan_dir: str | Path | None) -> dict[str, Any]:
    plan_id = str(plan.get("plan_id") or "")
    return _apply_failure(plan_id, "frozen_legacy_read_only", FROZEN_LEDGER_MESSAGE)


def _apply_candidate_action(conn: sqlite3.Connection, plan: dict[str, Any], *, plan_dir: str | Path | None) -> dict[str, Any]:
    plan_id = str(plan.get("plan_id") or "")
    if not CONTROLLED_MEM0_WRITER_ENABLED:
        return _apply_failure(plan_id, "controlled_mem0_writer_disabled", CONTROLLED_MEM0_WRITER_DISABLED_REASON)
    package = _load_candidate_package(conn, str(plan.get("package_id") or ""))
    if package.get("source_status") != "active":
        return _apply_failure(plan_id, package.get("source_status", "package_unavailable"), package.get("reason", "候选包不可用。"))
    candidate = _find_candidate(package, str(plan.get("candidate_key") or ""))
    if not candidate:
        return _apply_failure(plan_id, "candidate_missing", "执行时没有找到这条候选记忆。")
    action = str(plan.get("action") or "")
    memory_text = _safe_text(str(plan.get("memory_text") or ""), max_len=1200)
    gate = _candidate_gate(candidate, action=action, memory_text=memory_text)
    if not gate["allowed"]:
        return _apply_failure(plan_id, gate["status"], gate["message"])
    duplicate = _duplicate_report(memory_text)
    if duplicate["exact_duplicate"]:
        return _apply_failure(plan_id, "exact_duplicate", "已有完全相同的 PAM 长期记忆，已停止写入。")
    backup_path = _write_backup(
        [
            {
                "backed_up_at_utc": _now_iso(),
                "action": action,
                "candidate": _candidate_plan_snapshot(candidate),
                "memory_text": memory_text,
            }
        ],
        backup_dir=_backup_dir(plan_dir),
    )
    metadata = _candidate_memory_metadata(candidate, plan=plan)
    added = _mem0_add(memory_text, metadata)
    if not added.get("ok"):
        return _apply_failure(plan_id, "mem0_add_failed", added.get("error") or "mem0 add 失败。")
    receipt = added.get("receipt") if isinstance(added.get("receipt"), dict) else {"verified": False, "memory_ids": []}
    ledger = memory_ledger_status(conn)
    detail = {
        "package_id": plan.get("package_id") or "",
        "candidate_key": candidate["candidate_key"],
        "candidate_type": candidate.get("candidate_type") or "",
        "subject_scope": candidate.get("subject_scope") or "",
        "source_category": candidate.get("source_category") or "",
        "backup_path": Path(backup_path).name if backup_path else "",
        "ledger_status": ledger.get("status"),
        "mem0_receipt": receipt,
    }
    _insert_audit_log(conn, action="memory_approval_candidate_write", target_id=candidate["candidate_key"], detail=detail)
    receipt_verified = bool(receipt.get("verified"))
    return {
        "status": "applied" if receipt_verified else "write_unverified",
        "plan_id": plan_id,
        "action": action,
        "raw_access": False,
        "destructive_actions": False,
        "message": "候选记忆已获得 mem0 可核验回执。" if receipt_verified else "mem0 命令已返回，但缺少可核验记忆 ID；不宣称写入完成。",
        "candidate": _public_candidate(candidate),
        "mem0_receipt": receipt,
        "ledger": {
            "status": ledger.get("status"),
            "read_only": True,
            "current_memory_count": ledger.get("current_memory_count", 0),
            "entry_count": ledger.get("entry_count", 0),
        },
    }


def _ledger_state() -> dict[str, Any]:
    ledger_path = _ledger_path(None)
    detail_path = _detail_path(None)
    if not ledger_path.exists():
        return {"status": "ledger_missing", "message": "Obsidian 记忆台账不存在。"}
    entries = _read_ledger_entries(detail_path) or _read_ledger_entries(ledger_path)
    if not entries:
        return {"status": "ledger_detail_missing", "message": "台账详细记录不存在或为空。"}
    human_lines = _read_human_memory_lines(ledger_path)
    from .memory_ledger import _plan_human_ledger_changes

    pending = _plan_human_ledger_changes(entries, human_lines)
    return {
        "status": "ok",
        "ledger_path": ledger_path,
        "detail_path": detail_path,
        "entries": entries,
        "human_lines": human_lines,
        "pending_changes": bool(pending["changes"]),
    }


def _dry_run_ledger_action(
    conn: sqlite3.Connection,
    state: dict[str, Any],
    entry: dict[str, str],
    *,
    action: str,
    replacement_text: str,
) -> dict[str, Any]:
    return _blocked_preview("frozen_legacy_read_only", FROZEN_LEDGER_MESSAGE)


def _write_real_ledger_for_action(state: dict[str, Any], entry: dict[str, str], *, action: str, replacement_text: str) -> None:
    raise RuntimeError(FROZEN_LEDGER_MESSAGE)


def _visible_entries_for_action(
    entries: list[dict[str, str]],
    target: dict[str, str],
    *,
    action: str,
    replacement_text: str,
) -> list[dict[str, str]]:
    visible: list[dict[str, str]] = []
    target_id = target.get("ledger_id") or ""
    for entry in entries:
        if entry.get("entry_type") != "mem0" or entry.get("sync_status") == "deleted":
            continue
        if entry.get("ledger_id") == target_id:
            if action == "delete":
                continue
            updated = dict(entry)
            updated["memory_safe"] = _safe_text(replacement_text, max_len=1200)
            visible.append(updated)
            continue
        visible.append(entry)
    return visible


def _current_memory_items(*, limit: int) -> list[dict[str, Any]]:
    entries = _read_ledger_entries(_detail_path(None)) or _read_ledger_entries(_ledger_path(None))
    items = []
    for entry in entries:
        if entry.get("entry_type") != "mem0" or entry.get("sync_status") == "deleted":
            continue
        items.append(
            {
                "ledger_id": entry.get("ledger_id") or "",
                "safe_summary": _safe_text(entry.get("memory_safe") or "", max_len=360),
                "source_system": _safe_text(entry.get("source_system") or "", max_len=80),
                "source_profile": _safe_text(entry.get("source_profile") or "", max_len=80),
                "subject_scope": _safe_text(entry.get("subject_scope") or "", max_len=80),
                "source_category": _safe_text(entry.get("source_category") or "", max_len=80),
                "sync_status": _safe_text(entry.get("sync_status") or "", max_len=80),
                "can_delete": False,
                "can_rewrite": False,
            }
        )
        if len(items) >= max(1, limit):
            break
    return items


def _load_candidate_package(conn: sqlite3.Connection, package_id: str) -> dict[str, Any]:
    if package_id == "latest":
        row = conn.execute(
            """
            SELECT d.id AS package_id, d.cache_ref, d.date_range, h.source_status, h.retention_until
            FROM distribution_packages d
            JOIN ha_source_records h ON h.package_id = d.id
            WHERE d.target = 'ha'
            ORDER BY d.created_at DESC
            LIMIT 1
            """
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT d.id AS package_id, d.cache_ref, d.date_range, h.source_status, h.retention_until
            FROM distribution_packages d
            JOIN ha_source_records h ON h.package_id = d.id
            WHERE d.id = ?
            ORDER BY h.updated_at DESC
            LIMIT 1
            """,
            (package_id,),
        ).fetchone()
    if not row:
        return {"package_id": package_id, "source_status": "not_found", "raw_access": False}
    if row["source_status"] != "active":
        return {
            "package_id": row["package_id"],
            "source_status": row["source_status"],
            "time_range": _json_loads(row["date_range"]) or {},
            "raw_access": False,
        }
    path = Path(row["cache_ref"] or "")
    if not path.exists():
        return {
            "package_id": row["package_id"],
            "source_status": "cache_missing",
            "reason": "候选包缓存不存在。",
            "time_range": _json_loads(row["date_range"]) or {},
            "raw_access": False,
        }
    try:
        package = json.loads(path.read_text(encoding="utf-8"))
        validate_package(package)
    except (OSError, ValueError, json.JSONDecodeError):
        return {
            "package_id": row["package_id"],
            "source_status": "package_validation_failed",
            "reason": "候选包校验失败。",
            "time_range": _json_loads(row["date_range"]) or {},
            "raw_access": False,
        }
    return package


def _normalized_package_candidates(package: dict[str, Any], *, limit: int) -> dict[str, Any]:
    digest = package.get("learning_digest") if isinstance(package.get("learning_digest"), dict) else {}
    raw_items = digest.get("memory_candidates") if isinstance(digest.get("memory_candidates"), list) else []
    groups = {"eligible": [], "review_only": [], "ephemeral": []}
    total = 0
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        total += 1
        candidate = _normalize_candidate(package, raw)
        eligibility = candidate["write_eligibility"]
        if eligibility == "eligible":
            groups["eligible"].append(candidate)
        elif eligibility == "review_only":
            groups["review_only"].append(candidate)
        else:
            groups["ephemeral"].append(candidate)
    return {
        "eligible": [_public_candidate(item) for item in groups["eligible"][: max(1, limit)]],
        "review_only": [_public_candidate(item) for item in groups["review_only"][: max(1, min(limit, 24))]],
        "ephemeral": [_public_candidate(item) for item in groups["ephemeral"][: max(1, min(limit, 24))]],
        "eligible_count": len(groups["eligible"]),
        "review_only_count": len(groups["review_only"]),
        "ephemeral_count": len(groups["ephemeral"]),
        "total_count": total,
    }


def _find_candidate(package: dict[str, Any], candidate_key: str) -> dict[str, Any] | None:
    digest = package.get("learning_digest") if isinstance(package.get("learning_digest"), dict) else {}
    raw_items = digest.get("memory_candidates") if isinstance(digest.get("memory_candidates"), list) else []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        candidate = _normalize_candidate(package, raw)
        if candidate["candidate_key"] == candidate_key:
            return candidate
    return None


def _normalize_candidate(package: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    package_id = str(package.get("package_id") or "")
    safe_summary = _safe_text(item.get("safe_summary") or "", max_len=500)
    eligibility = _safe_text(item.get("write_eligibility") or "ephemeral", max_len=80)
    if eligibility not in {"eligible", "review_only", "ephemeral"}:
        eligibility = "ephemeral"
    candidate = {
        "candidate_key": _candidate_key(package_id, item),
        "package_id": package_id,
        "time_label": _safe_text(item.get("time_label") or "", max_len=80),
        "workflow": _safe_text(item.get("workflow") or "", max_len=120),
        "topic": _safe_text(item.get("topic") or "", max_len=120),
        "app": _safe_text(item.get("app") or "", max_len=80),
        "safe_summary": safe_summary,
        "candidate_type": _safe_text(item.get("candidate_type") or "activity_fact", max_len=80),
        "confidence": float(item.get("confidence") or 0.0),
        "why_keep": _safe_text(item.get("why_keep") or "", max_len=180),
        "evidence_count": int(item.get("evidence_count") or 1),
        "project_tags": _safe_list(item.get("project_tags"), limit=5),
        "anchor_type": _safe_text(item.get("anchor_type") or "weak_or_no_anchor", max_len=80),
        "anchor_strength": int(item.get("anchor_strength") or 0),
        "source_category": _safe_text(item.get("source_category") or "general_document", max_len=80),
        "source_scope": _safe_text(item.get("source_scope") or "context_only", max_len=80),
        "memory_write_policy": _safe_text(item.get("memory_write_policy") or "workflow_only", max_len=80),
        "subject_scope": _safe_text(item.get("subject_scope") or "user_workflow_only", max_len=80),
        "subject_confidence": float(item.get("subject_confidence") or 0.0),
        "subject_reasons": _safe_list(item.get("subject_reasons"), limit=6),
        "subject_risk_flags": _safe_list(item.get("subject_risk_flags"), limit=8),
        "quality_flags": _safe_list(item.get("quality_flags"), limit=8),
        "write_risk_flags": _safe_list(item.get("write_risk_flags"), limit=8),
        "write_eligibility": eligibility,
        "evidence_layer": _safe_text(item.get("evidence_layer") or "context_clue", max_len=80),
        "confirmation_signals": _safe_list(item.get("confirmation_signals"), limit=8),
        "evidence_explanation": _safe_text(item.get("evidence_explanation") or "", max_len=220),
        "screen_region_signal": _safe_region_signal(item.get("screen_region_signal")),
        "screen_region_signals": _safe_region_signals(item.get("screen_region_signals"), limit=8),
        "evidence_span_minutes": int(item.get("evidence_span_minutes") or 0),
        "merged_duplicate_count": int(item.get("merged_duplicate_count") or 0),
        "source_package_id": package_id,
        "activity_ids": _safe_list(item.get("activity_ids"), limit=12),
        "time_range": _safe_time_range(item.get("time_range") or package.get("time_range")),
        "deletion_group_ids": _safe_list(package.get("deletion_group_ids"), limit=24),
    }
    gate = _candidate_gate(candidate, action="approve_candidate" if eligibility == "eligible" else "promote_review_only", memory_text=safe_summary)
    if eligibility == "review_only" and gate["status"] == "manual_rewrite_required":
        gate = _gate(True, "manual_rewrite_required", "需要人工改写安全摘要后才能写入。", "requires_edit")
    if not CONTROLLED_MEM0_WRITER_ENABLED:
        gate = _gate(
            False,
            "controlled_mem0_writer_disabled",
            CONTROLLED_MEM0_WRITER_DISABLED_REASON,
            "blocked",
        )
    candidate["approval_state"] = gate["approval_state"]
    candidate["disabled_reason"] = gate["message"] if not gate["allowed"] else ""
    candidate["can_approve"] = eligibility == "eligible" and gate["allowed"]
    candidate["can_promote"] = eligibility == "review_only" and gate["allowed"]
    return candidate


def _candidate_gate(candidate: dict[str, Any], *, action: str, memory_text: str) -> dict[str, Any]:
    eligibility = candidate.get("write_eligibility") or "ephemeral"
    text = _safe_text(memory_text, max_len=1200)
    hard_reasons = _candidate_hard_block_reasons(candidate, text)
    if eligibility == "ephemeral":
        return _gate(False, "ephemeral_blocked", "临时候选不写入长期记忆。", "blocked")
    if hard_reasons:
        return _gate(False, "hard_blocked", "硬阻断：" + "；".join(hard_reasons[:3]), "blocked")
    if action == "approve_candidate" and eligibility != "eligible":
        return _gate(False, "not_eligible", "只有 eligible 候选可以直接审批。", "blocked")
    if action == "promote_review_only":
        if eligibility != "review_only":
            return _gate(False, "not_review_only", "只有 review_only 候选需要人工提升。", "blocked")
        if not text or text == candidate.get("safe_summary"):
            return _gate(False, "manual_rewrite_required", "review_only 必须先人工改写安全摘要。", "requires_edit")
    if not text or len(text) < 8:
        return _gate(False, "memory_text_too_short", "记忆摘要过短，不能写入。", "blocked")
    if _unsafe_text_detected(text):
        return _gate(False, "unsafe_text", "文本包含路径、密钥、联系方式或 raw 内容风险。", "blocked")
    state = "writable" if eligibility == "eligible" else "requires_edit"
    return _gate(True, "ok", "", state)


def _candidate_hard_block_reasons(candidate: dict[str, Any], memory_text: str) -> list[str]:
    reasons: list[str] = []
    subject_scope = str(candidate.get("subject_scope") or "")
    if subject_scope not in ALLOWED_SUBJECT_SCOPES:
        reasons.append("主体归属不安全")
    if str(candidate.get("evidence_layer") or "") != "confirmed_action":
        reasons.append("缺少已确认行动证据")
    primary_region = candidate.get("screen_region_signal") if isinstance(candidate.get("screen_region_signal"), dict) else {}
    region_signals = candidate.get("screen_region_signals") if isinstance(candidate.get("screen_region_signals"), list) else []
    if str(primary_region.get("actionability") or "") != "actionable" or any(
        str(signal.get("actionability") or "") != "actionable" for signal in region_signals if isinstance(signal, dict)
    ):
        reasons.append("屏幕区域不是可操作正文")
    if not _candidate_has_complete_provenance(candidate):
        reasons.append("来源回执不完整")
    source_category = str(candidate.get("source_category") or "")
    if source_category in HARD_BLOCK_SOURCE_CATEGORIES:
        reasons.append("来源类别不可写")
    joined_flags = " ".join(
        [
            source_category,
            str(candidate.get("source_scope") or ""),
            str(candidate.get("memory_write_policy") or ""),
            " ".join(candidate.get("subject_risk_flags") or []),
            " ".join(candidate.get("quality_flags") or []),
            " ".join(candidate.get("write_risk_flags") or []),
            str(candidate.get("evidence_layer") or ""),
            memory_text,
        ]
    ).lower()
    for fragment in HARD_BLOCK_FLAG_FRAGMENTS:
        if fragment in joined_flags:
            reasons.append(fragment)
    return sorted(set(reasons))


def _duplicate_report(memory_text: str) -> dict[str, Any]:
    target = _safe_text(memory_text, max_len=1200).strip()
    target_norm = _normalize_text(target)
    exact = False
    similar = 0
    for row in _load_mem0_rows(limit=200):
        existing = _safe_text(_human_memory_text(str(row.get("memory") or "")), max_len=1200)
        existing_norm = _normalize_text(existing)
        if not existing_norm:
            continue
        if existing_norm == target_norm:
            exact = True
        elif _similarity(existing_norm, target_norm) >= 0.92:
            similar += 1
    return {
        "exact_duplicate": exact,
        "similar_duplicate_count": similar,
        "raw_access": False,
    }


def _mem0_add(text: str, metadata: dict[str, Any]) -> dict[str, Any]:
    if not CONTROLLED_MEM0_WRITER_ENABLED:
        return {
            "ok": False,
            "status": "controlled_mem0_writer_disabled",
            "error": CONTROLLED_MEM0_WRITER_DISABLED_REASON,
        }
    mem0 = _mem0_cli_path()
    if not mem0.exists():
        return {"ok": False, "error": "mem0 CLI 不存在。"}
    payload = {
        "messages": [{"role": "user", "content": text}],
        "metadata": metadata,
        "user_id": "local-owner",
        "infer": False,
    }
    result = _run_mem0([str(mem0), "add", json.dumps(payload, ensure_ascii=False, sort_keys=True)], timeout=60)
    if not result.get("ok"):
        return result
    payload_result = result.get("payload")
    if isinstance(payload_result, dict) and payload_result.get("ok") is False:
        return {"ok": False, "error": str(payload_result.get("error") or "mem0 add failed")}
    return {"ok": True, "payload": payload_result, "receipt": _mem0_receipt(payload_result)}


def _candidate_memory_metadata(candidate: dict[str, Any], *, plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_system": "personal_ai_monitor",
        "source_platform": "macos_live_runtime",
        "source_profile": "memory-approval",
        "event_type": "pam_memory_human_approved_candidate",
        "memory_scope": "safe_summary",
        "memory_write_policy": "human_approved_safe_summary",
        "approval_source": "menu_bar_app",
        "approved_by": "local_user",
        "approved_at_utc": _now_iso(),
        "approval_plan_id": plan.get("plan_id") or "",
        "package_id": candidate.get("package_id") or "",
        "source_package_id": candidate.get("source_package_id") or "",
        "activity_ids": candidate.get("activity_ids") or [],
        "time_range": candidate.get("time_range") or {},
        "deletion_group_ids": candidate.get("deletion_group_ids") or [],
        "candidate_key": candidate.get("candidate_key") or "",
        "candidate_type": candidate.get("candidate_type") or "",
        "subject_scope": candidate.get("subject_scope") or "",
        "source_category": candidate.get("source_category") or "",
        "source_scope": candidate.get("source_scope") or "",
        "memory_user_id": "local-owner",
        "user_id": "local-owner",
    }


def _base_plan(action: str) -> dict[str, Any]:
    plan_id = uuid.uuid4().hex[:16]
    created = datetime.now(timezone.utc)
    return {
        "plan_id": plan_id,
        "action": action,
        "created_at_utc": created.isoformat(timespec="seconds"),
        "expires_at_utc": (created + timedelta(minutes=PLAN_TTL_MINUTES)).isoformat(timespec="seconds"),
        "raw_access": False,
        "destructive_actions": False,
    }


def _write_plan(plan: dict[str, Any], *, plan_dir: str | Path | None) -> dict[str, str]:
    root = _plans_path(plan_dir)
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    path = root / f"{plan['plan_id']}.json"
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    path.chmod(0o600)
    return {"plan_id": plan["plan_id"], "plan_file": path.name}


def _load_plan(plan_id: str, *, plan_dir: str | Path | None) -> dict[str, Any]:
    if not PLAN_ID_RE.match(str(plan_id or "")):
        return {"ok": False, "status": "invalid_plan_id", "message": "计划 ID 不合法。"}
    path = _plans_path(plan_dir) / f"{plan_id}.json"
    if not path.exists():
        return {"ok": False, "status": "plan_missing", "message": "审批计划不存在。"}
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"ok": False, "status": "plan_unreadable", "message": "审批计划无法读取。"}
    if not isinstance(plan, dict):
        return {"ok": False, "status": "plan_invalid", "message": "审批计划格式错误。"}
    return {"ok": True, "plan": plan}


def _is_plan_expired(plan: dict[str, Any]) -> bool:
    try:
        expires = datetime.fromisoformat(str(plan.get("expires_at_utc") or ""))
    except ValueError:
        return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires.astimezone(timezone.utc) < datetime.now(timezone.utc)


def _append_approval_record(result: dict[str, Any], *, plan_dir: str | Path | None) -> None:
    record = _public_record({**result, "recorded_at_utc": _now_iso()})
    path = _records_path(plan_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    path.chmod(0o600)


def _insert_audit_log(conn: sqlite3.Connection, *, action: str, target_id: str, detail: dict[str, Any]) -> None:
    try:
        conn.execute(
            """
            INSERT INTO audit_logs(id, at, action, target_type, target_id, detail)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                f"audit_{uuid.uuid4().hex}",
                _now_iso(),
                action,
                "memory_approval",
                target_id,
                json.dumps(detail, ensure_ascii=False, sort_keys=True),
            ),
        )
        conn.commit()
    except sqlite3.Error:
        pass


def _entry_by_ledger_id(entries: list[dict[str, str]], ledger_id: str) -> dict[str, str] | None:
    for entry in entries:
        if entry.get("ledger_id") == ledger_id:
            return entry
    return None


def _entry_is_syncable(entry: dict[str, str]) -> bool:
    return entry.get("entry_type") == "mem0" and _to_bool(entry.get("can_sync")) and bool(entry.get("memory_id"))


def _public_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "candidate_key",
        "time_label",
        "workflow",
        "topic",
        "app",
        "safe_summary",
        "candidate_type",
        "confidence",
        "why_keep",
        "evidence_count",
        "project_tags",
        "anchor_type",
        "anchor_strength",
        "source_category",
        "source_scope",
        "memory_write_policy",
        "subject_scope",
        "subject_confidence",
        "subject_reasons",
        "subject_risk_flags",
        "quality_flags",
        "write_risk_flags",
        "write_eligibility",
        "evidence_layer",
        "confirmation_signals",
        "evidence_explanation",
        "screen_region_signal",
        "screen_region_signals",
        "evidence_span_minutes",
        "merged_duplicate_count",
        "source_package_id",
        "activity_ids",
        "time_range",
        "deletion_group_ids",
        "approval_state",
        "disabled_reason",
        "can_approve",
        "can_promote",
    ]
    return {key: candidate.get(key) for key in keys if key in candidate}


def _candidate_plan_snapshot(candidate: dict[str, Any]) -> dict[str, Any]:
    public = _public_candidate(candidate)
    public.pop("disabled_reason", None)
    return public


def _safe_sync_preview(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "processed_count": int(result.get("processed_count") or 0),
        "deleted_count": int(result.get("deleted_count") or 0),
        "rewritten_count": int(result.get("rewritten_count") or 0),
        "skipped_count": int(result.get("skipped_count") or 0),
        "results": [
            {
                "ledger_id": item.get("ledger_id") or "",
                "status": item.get("status") or "",
                "message": _safe_text(item.get("message") or "", max_len=180),
            }
            for item in (result.get("results") or [])[:10]
            if isinstance(item, dict)
        ],
    }


def _public_sync_result(result: dict[str, Any]) -> dict[str, Any]:
    output = _safe_sync_preview(result)
    output["backup_file"] = Path(str(result.get("backup_path") or "")).name if result.get("backup_path") else ""
    return output


def _compact_audit(audit: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": audit.get("status") or "unknown",
        "summary": _safe_text(audit.get("summary") or "", max_len=240),
        "raw_access": bool(audit.get("raw_access")),
        "destructive_actions": bool(audit.get("destructive_actions")),
        "items_checked": int(audit.get("items_checked") or 0),
        "item_count": int(audit.get("item_count") or 0),
        "issue_counts": audit.get("issue_counts") if isinstance(audit.get("issue_counts"), dict) else {},
    }


def _compact_ledger(ledger: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": ledger.get("status") or "unknown",
        "raw_access": bool(ledger.get("raw_access")),
        "destructive_actions": bool(ledger.get("destructive_actions")),
        "read_only": bool(ledger.get("read_only", True)),
        "write_enabled": bool(ledger.get("write_enabled", False)),
        "entry_count": int(ledger.get("entry_count") or 0),
        "current_memory_count": int(ledger.get("current_memory_count") or 0),
        "pending_action_count": int(ledger.get("pending_action_count") or 0),
        "needs_manual_lookup_count": int(ledger.get("needs_manual_lookup_count") or 0),
        "can_sync_count": int(ledger.get("can_sync_count") or 0),
        "unmatched_new_line_count": int(ledger.get("unmatched_new_line_count") or 0),
        "complex_change_count": int(ledger.get("complex_change_count") or 0),
    }


def _compact_candidate_status(candidates: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": candidates.get("status") or "unknown",
        "package_id": candidates.get("package_id") or "",
        "raw_access": False,
        "writer_enabled": bool(candidates.get("writer_enabled", False)),
        "writer_disabled_reason": _safe_text(candidates.get("writer_disabled_reason") or "", max_len=240),
        "eligible_candidate_count": int(candidates.get("eligible_candidate_count") or 0),
        "review_only_candidate_count": int(candidates.get("review_only_candidate_count") or 0),
        "ephemeral_candidate_count": int(candidates.get("ephemeral_candidate_count") or 0),
        "total_candidate_count": int(candidates.get("total_candidate_count") or 0),
        "message": _safe_text(candidates.get("message") or "", max_len=180),
    }


def _public_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "recorded_at_utc": record.get("recorded_at_utc") or record.get("created_at_utc") or _now_iso(),
        "status": _safe_text(record.get("status") or "", max_len=80),
        "action": _safe_text(record.get("action") or "", max_len=80),
        "plan_id": _safe_text(record.get("plan_id") or "", max_len=80),
        "message": _safe_text(record.get("message") or "", max_len=180),
    }


def _blocked_preview(status: str, message: str, *, candidate: dict[str, Any] | None = None) -> dict[str, Any]:
    output = {
        "status": status,
        "raw_access": False,
        "destructive_actions": False,
        "message": _safe_text(message, max_len=240),
    }
    if candidate:
        output["candidate"] = _public_candidate(candidate)
    return output


def _apply_failure(plan_id: str, status: str, message: str) -> dict[str, Any]:
    return {
        "status": status,
        "plan_id": plan_id,
        "raw_access": False,
        "destructive_actions": False,
        "message": _safe_text(message, max_len=240),
    }


def _gate(allowed: bool, status: str, message: str, approval_state: str) -> dict[str, Any]:
    return {
        "allowed": allowed,
        "status": status,
        "message": message,
        "approval_state": approval_state,
    }


def _ledger_preview_summary(action: str, entry: dict[str, str], replacement_text: str) -> str:
    if action == "delete":
        return "准备删除 1 条 PAM 来源长期记忆。"
    preview = replacement_text[:80].rstrip()
    return f"准备改写 1 条 PAM 来源长期记忆：{preview}"


def _candidate_key(package_id: str, item: dict[str, Any]) -> str:
    blob = json.dumps(
        {
            "package_id": package_id,
            "safe_summary": item.get("safe_summary") or "",
            "candidate_type": item.get("candidate_type") or "",
            "time_label": item.get("time_label") or "",
            "subject_scope": item.get("subject_scope") or "",
            "source_category": item.get("source_category") or "",
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return _hash_text(blob).replace("sha256:", "cand_")[:29]


def _safe_list(value: Any, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    output = []
    for item in value[:limit]:
        text = _safe_text(str(item or ""), max_len=80)
        if text:
            output.append(text)
    return output


def _safe_time_range(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {"from": "", "to": ""}
    return {
        "from": _safe_text(value.get("from") or "", max_len=80),
        "to": _safe_text(value.get("to") or "", max_len=80),
    }


def _candidate_has_complete_provenance(candidate: dict[str, Any]) -> bool:
    time_range = candidate.get("time_range") if isinstance(candidate.get("time_range"), dict) else {}
    return bool(
        str(candidate.get("source_package_id") or "")
        and candidate.get("activity_ids")
        and _valid_provenance_time_range(time_range)
        and candidate.get("deletion_group_ids")
    )


def _valid_provenance_time_range(time_range: dict[str, Any]) -> bool:
    source_start = str(time_range.get("from") or "")
    source_end = str(time_range.get("to") or "")
    if not source_start or not source_end:
        return False
    try:
        return datetime.fromisoformat(source_start) <= datetime.fromisoformat(source_end)
    except (TypeError, ValueError):
        return False


def _mem0_receipt(payload: Any) -> dict[str, Any]:
    ids: list[str] = []

    def collect(value: Any, *, depth: int = 0) -> None:
        if depth > 4:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"id", "memory_id"} and isinstance(child, (str, int)):
                    safe = _safe_text(str(child), max_len=120)
                    if safe and safe not in ids:
                        ids.append(safe)
                elif key in {"result", "results", "memory", "memories", "data"}:
                    collect(child, depth=depth + 1)
        elif isinstance(value, list):
            for child in value[:20]:
                collect(child, depth=depth + 1)

    collect(payload)
    return {
        "verified": bool(ids),
        "memory_ids": ids[:8],
        "receipt_type": "mem0_add_response",
    }


def _safe_region_signal(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    region_kind = _safe_text(value.get("region_kind") or "", max_len=80)
    actionability = _safe_text(value.get("actionability") or "", max_len=80)
    if not region_kind and not actionability:
        return {}
    try:
        confidence = float(value.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "region_kind": region_kind,
        "actionability": actionability,
        "source": _safe_text(value.get("source") or "", max_len=80),
        "confidence": round(max(0.0, min(1.0, confidence)), 2),
        "reason_codes": _safe_list(value.get("reason_codes"), limit=8),
    }


def _safe_region_signals(value: Any, *, limit: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    output: list[dict[str, Any]] = []
    for item in value[:limit]:
        signal = _safe_region_signal(item)
        if signal:
            output.append(signal)
    return output


def _unsafe_text_detected(value: str) -> bool:
    lowered = value.lower()
    if any(fragment in lowered for fragment in ["raw ocr", "raw screenshot", "private key", "oauth token", "api token"]):
        return True
    sanitized = _safe_text(value, max_len=max(len(value), 1200))
    return any(marker in sanitized for marker in ["[path_redacted]", "[secret_redacted]", "[email_redacted]", "[phone_redacted]"])


def _normalize_approval_action(action: str) -> str:
    value = str(action or "").strip().lower().replace("-", "_")
    aliases = {
        "approve": "approve_candidate",
        "approve_candidate": "approve_candidate",
        "promote": "promote_review_only",
        "promote_review_only": "promote_review_only",
        "delete": "delete",
        "rewrite": "rewrite",
    }
    return aliases.get(value, value)


def _normalize_text(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    from difflib import SequenceMatcher

    return SequenceMatcher(a=left, b=right, autojunk=False).ratio()


def _json_loads(value: str) -> Any:
    try:
        return json.loads(value or "")
    except json.JSONDecodeError:
        return None


def _plans_path(plan_dir: str | Path | None) -> Path:
    root = Path(plan_dir).expanduser() if plan_dir else Path.home() / APPROVAL_DIR_RELATIVE_PATH
    return root / "pending"


def _records_path(plan_dir: str | Path | None) -> Path:
    root = Path(plan_dir).expanduser() if plan_dir else Path.home() / APPROVAL_DIR_RELATIVE_PATH
    return root / "approval-records.jsonl"


def _backup_dir(plan_dir: str | Path | None) -> Path:
    root = Path(plan_dir).expanduser() if plan_dir else Path.home() / APPROVAL_DIR_RELATIVE_PATH
    return root / "backups"


def _mem0_cli_path() -> Path:
    return Path.home() / "bin" / "mem0"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
