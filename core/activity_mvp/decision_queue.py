from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .ai_handoff import (
    _decision_keywords,
    _follow_up_keywords,
    _blocker_keywords,
    _safe_annotation,
    _signal_items,
    build_ai_handoff_package,
)
from .timeline_export import cleaned_timeline_export
from .memory_approval import (
    CONTROLLED_MEM0_WRITER_DISABLED_REASON,
    CONTROLLED_MEM0_WRITER_ENABLED,
    _duplicate_report,  # compatibility hook for existing read-only queue tests
    apply_memory_approval,
    memory_approval_candidates,
    preview_memory_approval,
)
from .user_decisions import (
    APPROVED_WRITE,
    CONTEXT_ONLY,
    CORRECT_SIGNAL,
    NEVER_WRITE,
    REWRITTEN_WRITE,
    decision_signature_for_item,
    find_user_decision,
    item_key_for_review_signal,
    list_user_decisions,
    public_decision,
    safe_decision_text,
    upsert_user_decision,
)


PLAN_TTL_MINUTES = 10
DEFAULT_LIMIT = 80
PLAN_DIR_RELATIVE_PATH = Path("Library/Application Support/Personal AI Monitor/decision-queue-plans")
FEEDBACK_FOLDER_SIDEBAR = "folder-sidebar"
FEEDBACK_OLD_TASK = "old-task"
FEEDBACK_REFERENCE = "reference-material"
FEEDBACK_CORRECT = "correct"
FEEDBACK_ACTION_MAP = {
    FEEDBACK_FOLDER_SIDEBAR: NEVER_WRITE,
    FEEDBACK_OLD_TASK: CONTEXT_ONLY,
    FEEDBACK_REFERENCE: CONTEXT_ONLY,
    FEEDBACK_CORRECT: CORRECT_SIGNAL,
}
FEEDBACK_NOTE_PREFIX = {
    FEEDBACK_FOLDER_SIDEBAR: "feedback:folder_sidebar",
    FEEDBACK_OLD_TASK: "feedback:old_task",
    FEEDBACK_REFERENCE: "feedback:reference_material",
    FEEDBACK_CORRECT: "feedback:correct_signal",
}


def decision_queue_list(
    conn: sqlite3.Connection,
    *,
    range_name: str = "today",
    start: str | None = None,
    end: str | None = None,
    package_id: str = "latest",
    limit: int = DEFAULT_LIMIT,
    include_decided: bool = False,
) -> dict[str, Any]:
    limit = max(1, int(limit or DEFAULT_LIMIT))
    memory = _memory_candidate_items(conn, package_id=package_id, limit=limit)
    review = _review_signal_items(conn, range_name=range_name, start=start, end=end, limit=limit)
    items: list[dict[str, Any]] = []
    hidden_by_decision = 0
    auto_handled_counts: dict[str, int] = {}
    for item in memory + review:
        decision = find_user_decision(conn, item, source_kind=item["source_kind"], item_type=item["item_type"])
        if decision:
            item["user_decision"] = public_decision(decision)
            item["decision_state"] = _decision_state(decision)
            if not include_decided:
                hidden_by_decision += 1
                continue
        auto_reason = "" if include_decided else _auto_handled_reason(item)
        if auto_reason:
            auto_handled_counts[auto_reason] = auto_handled_counts.get(auto_reason, 0) + 1
            continue
        source_merged = int(item.get("source_merged_duplicate_count") or 0)
        if source_merged:
            auto_handled_counts["merged_duplicate"] = auto_handled_counts.get("merged_duplicate", 0) + source_merged
        items.append(item)
    items = _merge_duplicate_queue_items(items, auto_handled_counts)
    items.sort(key=_queue_sort_key)
    selected = items[:limit]
    return {
        "status": "ok",
        "interface": "decision-queue",
        "raw_access": False,
        "destructive_actions": False,
        "memory_write_actions": False,
        "range": range_name,
        "package_id": package_id,
        "summary": _queue_summary(selected, hidden_by_decision=hidden_by_decision, auto_handled_counts=auto_handled_counts),
        "item_count": len(selected),
        "hidden_by_user_decision_count": hidden_by_decision,
        "auto_handled_count": sum(auto_handled_counts.values()),
        "auto_handled": _public_auto_handled(auto_handled_counts),
        "feedback_actions": _feedback_actions(),
        "memory_candidate_count": sum(1 for item in selected if item["source_kind"] == "memory_candidate"),
        "review_signal_count": sum(1 for item in selected if item["source_kind"].startswith("ai_review_")),
        "items": selected,
        "recent_decisions": list_user_decisions(conn, limit=10),
    }


def record_decision_queue_action(
    conn: sqlite3.Connection,
    *,
    item_key: str,
    action: str,
    note: str = "",
    generalize_scope: str = "similar",
    range_name: str = "today",
    start: str | None = None,
    end: str | None = None,
    package_id: str = "latest",
) -> dict[str, Any]:
    requested_action = str(action or "").strip()
    normalized_action = FEEDBACK_ACTION_MAP.get(requested_action, requested_action)
    if normalized_action not in {CONTEXT_ONLY, NEVER_WRITE, CORRECT_SIGNAL}:
        return _blocked("unsupported_action", "只支持 context-only、never-write 或本地反馈动作；写入类动作必须先生成预览。")
    item = _find_queue_item(
        conn,
        item_key=item_key,
        range_name=range_name,
        start=start,
        end=end,
        package_id=package_id,
        include_decided=True,
    )
    if not item:
        return _blocked("item_missing", "没有找到这条待决策项目。")
    if normalized_action == CORRECT_SIGNAL:
        generalize_scope = "exact"
    note_prefix = FEEDBACK_NOTE_PREFIX.get(requested_action, "")
    safe_note = f"{note_prefix} {note}".strip() if note_prefix else note
    decision = upsert_user_decision(
        conn,
        item=item,
        source_kind=item["source_kind"],
        item_type=item["item_type"],
        action=normalized_action,
        note=safe_note,
        generalize_scope=generalize_scope,
    )
    message = (
        "已标注这次判断正确；这只增加本地质量标注，不会绕过写入审批。"
        if normalized_action == CORRECT_SIGNAL
        else "已记录你的处理方式；同类内容会按该规则降级或不再反复询问。"
    )
    return {
        "status": "recorded",
        "raw_access": False,
        "destructive_actions": False,
        "memory_write_actions": False,
        "requested_action": requested_action,
        "recorded_action": normalized_action,
        "decision": decision,
        "message": message,
    }


def preview_decision_queue_write(
    conn: sqlite3.Connection,
    *,
    item_key: str,
    memory_text: str = "",
    range_name: str = "today",
    start: str | None = None,
    end: str | None = None,
    package_id: str = "latest",
    plan_dir: str | Path | None = None,
) -> dict[str, Any]:
    item = _find_queue_item(
        conn,
        item_key=item_key,
        range_name=range_name,
        start=start,
        end=end,
        package_id=package_id,
        include_decided=True,
    )
    if not item:
        return _blocked("item_missing", "没有找到这条待决策项目。")
    text = safe_decision_text(memory_text or item.get("safe_summary") or "", max_len=1200)
    if item["source_kind"] == "memory_candidate":
        return _preview_memory_candidate(conn, item=item, memory_text=text, plan_dir=plan_dir)
    return _preview_review_signal(conn, item=item, memory_text=text, plan_dir=plan_dir)


def apply_decision_queue_plan(
    conn: sqlite3.Connection,
    *,
    plan_id: str,
    plan_dir: str | Path | None = None,
) -> dict[str, Any]:
    local = _load_plan(plan_id, plan_dir=plan_dir)
    if local.get("ok"):
        return _apply_local_plan(conn, local["plan"], plan_dir=plan_dir)
    delegated = apply_memory_approval(conn, plan_id=plan_id)
    if delegated.get("status") == "applied":
        candidate = delegated.get("candidate") if isinstance(delegated.get("candidate"), dict) else {}
        if candidate:
            action = APPROVED_WRITE if delegated.get("action") == "approve_candidate" else REWRITTEN_WRITE
            upsert_user_decision(
                conn,
                item=_queue_item_from_candidate(candidate, package_id=candidate.get("package_id") or ""),
                source_kind="memory_candidate",
                item_type="memory_candidate",
                action=action,
                generalize_scope="exact",
                applied_plan_id=plan_id,
            )
    return delegated


def _memory_candidate_items(conn: sqlite3.Connection, *, package_id: str, limit: int) -> list[dict[str, Any]]:
    result = memory_approval_candidates(conn, package_id=package_id, limit=limit)
    if result.get("status") != "ok":
        return []
    resolved_package_id = str(result.get("package_id") or package_id)
    output: list[dict[str, Any]] = []
    for item in result.get("memory_candidates") or []:
        output.append(_queue_item_from_candidate(item, package_id=resolved_package_id))
    for item in result.get("review_only_candidates") or []:
        output.append(_queue_item_from_candidate(item, package_id=resolved_package_id))
    for item in result.get("ephemeral_candidates") or []:
        output.append(_queue_item_from_candidate(item, package_id=resolved_package_id))
    return output


def _queue_item_from_candidate(item: dict[str, Any], *, package_id: str) -> dict[str, Any]:
    source_kind = "memory_candidate"
    item_type = "memory_candidate"
    safe_summary = safe_decision_text(item.get("safe_summary") or "")
    eligibility = safe_decision_text(item.get("write_eligibility") or "ephemeral", max_len=80)
    hard_blocked = _candidate_is_hard_blocked(item)
    can_direct = (
        CONTROLLED_MEM0_WRITER_ENABLED
        and bool(item.get("can_approve"))
        and eligibility == "eligible"
        and not hard_blocked
    )
    can_rewrite = (
        CONTROLLED_MEM0_WRITER_ENABLED
        and bool(item.get("can_promote"))
        and eligibility == "review_only"
        and not hard_blocked
    )
    source_merged_duplicates = int(item.get("merged_duplicate_count") or 0)
    output = {
        "item_key": str(item.get("candidate_key") or ""),
        "item_type": item_type,
        "source_kind": source_kind,
        "source_label": "长期记忆候选",
        "package_id": str(item.get("package_id") or package_id),
        "candidate_key": str(item.get("candidate_key") or ""),
        "safe_summary": safe_summary,
        "topic": safe_decision_text(item.get("topic") or "", max_len=160),
        "app": safe_decision_text(item.get("app") or "", max_len=80),
        "time_label": safe_decision_text(item.get("time_label") or "", max_len=80),
        "candidate_type": safe_decision_text(item.get("candidate_type") or "activity_fact", max_len=80),
        "confidence": float(item.get("confidence") or 0.0),
        "why_uncertain": safe_decision_text(item.get("disabled_reason") or item.get("why_keep") or "", max_len=220),
        "evidence_count": int(item.get("evidence_count") or 0),
        "source_category": safe_decision_text(item.get("source_category") or "", max_len=80),
        "subject_scope": safe_decision_text(item.get("subject_scope") or "", max_len=80),
        "write_eligibility": eligibility,
        "evidence_layer": safe_decision_text(item.get("evidence_layer") or "context_clue", max_len=80),
        "confirmation_signals": [safe_decision_text(value, max_len=80) for value in (item.get("confirmation_signals") or [])[:8]],
        "evidence_explanation": safe_decision_text(item.get("evidence_explanation") or "", max_len=220),
        "screen_region_signal": _safe_region_signal(item.get("screen_region_signal")),
        "screen_region_signals": _safe_region_signals(item.get("screen_region_signals"), limit=8),
        "source_package_id": safe_decision_text(item.get("source_package_id") or item.get("package_id") or package_id, max_len=120),
        "activity_ids": [safe_decision_text(value, max_len=120) for value in (item.get("activity_ids") or [])[:12]],
        "time_range": item.get("time_range") if isinstance(item.get("time_range"), dict) else {},
        "deletion_group_ids": [safe_decision_text(value, max_len=120) for value in (item.get("deletion_group_ids") or [])[:24]],
        "quality_flags": [safe_decision_text(value, max_len=80) for value in (item.get("quality_flags") or [])[:8]],
        "can_direct_write": can_direct,
        "can_rewrite_write": can_rewrite,
        "can_context_only": True,
        "can_never_write": True,
        "direct_write_disabled_reason": "" if can_direct else _direct_write_disabled_reason(item, eligibility=eligibility, hard_blocked=hard_blocked),
        "rewrite_disabled_reason": "" if can_rewrite else _rewrite_disabled_reason(item, eligibility=eligibility, hard_blocked=hard_blocked),
        "decision_state": "needs_decision",
        "user_decision": {},
        "source_merged_duplicate_count": source_merged_duplicates,
        "merged_item_count": max(1, source_merged_duplicates + 1),
    }
    output["decision_signature"] = decision_signature_for_item(output, source_kind=source_kind, item_type=item_type)
    _decorate_human_decision_fields(output)
    return output


def _review_signal_items(
    conn: sqlite3.Connection,
    *,
    range_name: str,
    start: str | None,
    end: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Build owner-facing review signals without widening the HA trust gate.

    The AI handoff package is a trusted/export surface and therefore only sees
    explicit user confirmations.  The local decision queue may still surface
    Privacy-Firewall-cleaned, explicitly unverified screen observations for a
    human decision.  These rows remain review-only and can never be written
    directly.
    """
    if start is None or end is None:
        package = build_ai_handoff_package(
            conn,
            range_name=range_name,
            start=start,
            end=end,
            max_items=limit,
        )
        time_range = package.get("time_range") or {}
        resolved_start = str(time_range.get("from") or "")
        resolved_end = str(time_range.get("to") or "")
    else:
        resolved_start, resolved_end = start, end
        time_range = {"from": start, "to": end}
    export = cleaned_timeline_export(conn, resolved_start or None, resolved_end or None)
    annotations = [
        _safe_annotation(item)
        for item in ((export.get("learning_digest") or {}).get("annotations") or [])
        if str(item.get("kind") or "") == "activity"
    ]
    annotations = [item for item in annotations if item]
    output: list[dict[str, Any]] = []
    for source_kind, source_label, items in (
        ("ai_review_decision", "Review 疑似决策", _signal_items(annotations, _decision_keywords(), "suspected_decision")),
        ("ai_review_follow_up", "Review 待办/跟进", _signal_items(annotations, _follow_up_keywords(), "suspected_follow_up")),
        ("ai_review_blocker", "Review 阻塞/异常", _signal_items(annotations, _blocker_keywords(), "suspected_blocker")),
    ):
        for item in items[:limit]:
            output.append(
                _queue_item_from_review_signal(
                    item,
                    source_kind=source_kind,
                    source_label=source_label,
                    time_range=time_range,
                )
            )
    return output

def _queue_item_from_review_signal(
    item: dict[str, Any],
    *,
    source_kind: str,
    source_label: str,
    time_range: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item_type = "review_signal"
    safe_summary = safe_decision_text(item.get("safe_summary") or "")
    output = {
        "item_key": item_key_for_review_signal(item, source_kind),
        "item_type": item_type,
        "source_kind": source_kind,
        "source_label": source_label,
        "package_id": "",
        "candidate_key": "",
        "safe_summary": safe_summary,
        "topic": safe_decision_text(item.get("topic") or "", max_len=160),
        "app": safe_decision_text(item.get("app") or "", max_len=80),
        "time_label": safe_decision_text(item.get("time_label") or "", max_len=80),
        "candidate_type": safe_decision_text(item.get("type") or "review_signal", max_len=80),
        "confidence": float(item.get("confidence") or 0.0),
        "why_uncertain": safe_decision_text(item.get("recommendation") or "Review 线索只能当待复核内容，不能直接写成长期事实。", max_len=220),
        "evidence_count": 1,
        "source_category": "ai_review_safe_summary",
        "subject_scope": "user_workflow_only",
        "write_eligibility": "review_only",
        "screen_region_signal": {},
        "screen_region_signals": [],
        "source_package_id": "",
        "activity_ids": [],
        "time_range": {
            "from": safe_decision_text((time_range or {}).get("from") or "", max_len=80),
            "to": safe_decision_text((time_range or {}).get("to") or "", max_len=80),
        },
        "deletion_group_ids": [],
        "quality_flags": ["review_signal_requires_human_rewrite"],
        "can_direct_write": False,
        "can_rewrite_write": False,
        "can_context_only": True,
        "can_never_write": True,
        "direct_write_disabled_reason": "Review 线索不能直接写长期记忆；请先改写成安全长期事实。",
        "rewrite_disabled_reason": "Review 线索缺少本人归属和完整可撤销来源，只能作为当次建议。",
        "decision_state": "needs_decision",
        "user_decision": {},
        "merged_item_count": 1,
    }
    output["decision_signature"] = decision_signature_for_item(output, source_kind=source_kind, item_type=item_type)
    _decorate_human_decision_fields(output)
    return output


def _decorate_human_decision_fields(item: dict[str, Any]) -> None:
    candidate = safe_decision_text(item.get("safe_summary") or "", max_len=520)
    item["decision_question"] = _decision_question(item)
    item["candidate_sentence"] = candidate
    item["uncertainty_explanation"] = _uncertainty_explanation(item)
    item["available_actions"] = _available_actions(item)
    item["recommended_action"] = _recommended_action(item)
    item["queue_bucket"] = _queue_bucket(item)


def _decision_question(item: dict[str, Any]) -> str:
    if item.get("source_kind") == "memory_candidate":
        if not CONTROLLED_MEM0_WRITER_ENABLED:
            return "长期记忆写入已关闭；这条只作当前参考，要标为参考还是永不写入？"
        return "要不要把这句话记进长期记忆？"
    return "这条 Review 线索只作当次建议，要标成参考还是永不写入？"


def _uncertainty_explanation(item: dict[str, Any]) -> str:
    if item.get("source_kind") == "memory_candidate" and not CONTROLLED_MEM0_WRITER_ENABLED:
        return CONTROLLED_MEM0_WRITER_DISABLED_REASON
    reason = safe_decision_text(item.get("why_uncertain") or "", max_len=260)
    if reason:
        return reason
    eligibility = item.get("write_eligibility") or "ephemeral"
    if eligibility == "eligible":
        return "系统认为它可能是稳定事实，但写入前仍需要你确认。"
    if eligibility == "review_only":
        return "系统觉得它可能有用，但现在不像一句能直接长期保存的事实，需要你改写或否决。"
    return "系统认为这类内容只适合临时参考，通常不该写进长期记忆。"


def _available_actions(item: dict[str, Any]) -> list[str]:
    actions: list[str] = []
    if item.get("can_direct_write"):
        actions.append("direct-write")
    if item.get("can_rewrite_write"):
        actions.append("rewrite-write")
    if item.get("can_context_only"):
        actions.append("context-only")
    if item.get("can_never_write"):
        actions.append("never-write")
    actions.extend([FEEDBACK_FOLDER_SIDEBAR, FEEDBACK_OLD_TASK, FEEDBACK_REFERENCE, FEEDBACK_CORRECT])
    return actions


def _recommended_action(item: dict[str, Any]) -> str:
    actions = set(item.get("available_actions") or [])
    eligibility = item.get("write_eligibility") or "ephemeral"
    if eligibility == "eligible" and "direct-write" in actions:
        return "direct-write"
    if "rewrite-write" in actions:
        return "rewrite-write"
    if "context-only" in actions:
        return "context-only"
    return "never-write"


def _queue_bucket(item: dict[str, Any]) -> str:
    if item.get("source_kind", "").startswith("ai_review_"):
        return "context_only"
    if not CONTROLLED_MEM0_WRITER_ENABLED:
        return "context_only"
    eligibility = item.get("write_eligibility") or "ephemeral"
    if eligibility == "eligible":
        return "can_preview"
    if eligibility == "review_only":
        return "needs_rewrite"
    return "context_only"


def _candidate_is_hard_blocked(item: dict[str, Any]) -> bool:
    reason = str(item.get("disabled_reason") or "").lower()
    source = str(item.get("source_category") or "").lower()
    flags = " ".join(str(value).lower() for value in [*(item.get("quality_flags") or []), *(item.get("write_risk_flags") or []), item.get("evidence_layer") or ""])
    if "硬阻断" in reason or "hard_block" in reason:
        return True
    if source in {"target_role_jd", "third_party_resume", "ambiguous_document", "reference_only_document", "reference_only_job_document", "personal_medical_document"}:
        return True
    return any(
        fragment in flags
        for fragment in (
            "third_party_subject",
            "ambiguous_subject",
            "personal_medical",
            "reference_only_document",
            "noise_or_reference",
            "file_listing_context",
            "task_list_context",
            "browser_tab_chrome",
            "system_ui_context",
            "read_only_context_app",
            "document_not_modified_near_capture",
            "screen_region_noise_enforced",
        )
    )


def _direct_write_disabled_reason(item: dict[str, Any], *, eligibility: str, hard_blocked: bool) -> str:
    if not CONTROLLED_MEM0_WRITER_ENABLED:
        return CONTROLLED_MEM0_WRITER_DISABLED_REASON
    if hard_blocked:
        return safe_decision_text(item.get("disabled_reason") or "这类内容不适合写进长期记忆。", max_len=180)
    if eligibility == "review_only":
        return "这条需要先改写成一句安全长期事实，不能直接写入。"
    if eligibility == "ephemeral":
        return "这类内容只适合临时参考，不写进长期记忆。"
    return safe_decision_text(item.get("disabled_reason") or "暂不能直接写入。", max_len=180)


def _rewrite_disabled_reason(item: dict[str, Any], *, eligibility: str, hard_blocked: bool) -> str:
    if not CONTROLLED_MEM0_WRITER_ENABLED:
        return CONTROLLED_MEM0_WRITER_DISABLED_REASON
    if hard_blocked:
        return safe_decision_text(item.get("disabled_reason") or "这类内容不允许通过改写提升。", max_len=180)
    if eligibility == "ephemeral":
        return "这类内容只适合临时参考，不需要改写后写入。"
    return safe_decision_text(item.get("disabled_reason") or "这条不能改写写入。", max_len=180)


def _auto_handled_reason(item: dict[str, Any]) -> str:
    if _looks_like_personal_ai_monitor_ui_noise(item):
        return "self_ui_noise"
    if _is_hard_blocked_queue_item(item):
        return "hard_blocked"
    return ""


def _looks_like_personal_ai_monitor_ui_noise(item: dict[str, Any]) -> bool:
    text = f"{item.get('safe_summary') or ''} {item.get('topic') or ''} {item.get('app') or ''}".lower()
    has_pam_name = "personal ai monitor" in text or "personal al monitor" in text or "ai 个人监控" in text
    if not has_pam_name:
        return False
    ui_markers = ("待决策", "raw_access=false", "raw_access=talse", "重新读取", "本机入口", "概览", "先预览", "不自动写")
    return any(marker.lower() in text for marker in ui_markers)


def _is_hard_blocked_queue_item(item: dict[str, Any]) -> bool:
    if item.get("source_kind") == "memory_candidate" and not CONTROLLED_MEM0_WRITER_ENABLED:
        return _candidate_is_hard_blocked(item)
    reason = f"{item.get('direct_write_disabled_reason') or ''} {item.get('rewrite_disabled_reason') or ''}".lower()
    if "硬阻断" in reason or "hard_block" in reason:
        return True
    return bool(item.get("source_kind") == "memory_candidate" and not item.get("can_direct_write") and not item.get("can_rewrite_write") and item.get("write_eligibility") != "ephemeral")


def _merge_duplicate_queue_items(items: list[dict[str, Any]], auto_handled_counts: dict[str, int]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in items:
        signature = str(item.get("decision_signature") or item.get("item_key") or uuid.uuid4().hex)
        existing = merged.get(signature)
        if not existing:
            merged[signature] = item
            order.append(signature)
            continue
        existing["merged_item_count"] = int(existing.get("merged_item_count") or 1) + 1
        existing["evidence_count"] = max(int(existing.get("evidence_count") or 0), int(item.get("evidence_count") or 0))
        if not existing.get("time_label") and item.get("time_label"):
            existing["time_label"] = item.get("time_label")
        _decorate_human_decision_fields(existing)
        auto_handled_counts["merged_duplicate"] = auto_handled_counts.get("merged_duplicate", 0) + 1
    return [merged[key] for key in order]


def _public_auto_handled(counts: dict[str, int]) -> list[dict[str, Any]]:
    labels = {
        "self_ui_noise": "系统页面噪声",
        "hard_blocked": "不适合写入",
        "already_written": "已存在长期记忆",
        "near_duplicate": "疑似重复",
        "merged_duplicate": "同类合并",
    }
    return [
        {"reason": key, "label": labels.get(key, key), "count": int(value)}
        for key, value in sorted(counts.items())
        if int(value) > 0
    ]


def _find_queue_item(
    conn: sqlite3.Connection,
    *,
    item_key: str,
    range_name: str,
    start: str | None,
    end: str | None,
    package_id: str,
    include_decided: bool,
) -> dict[str, Any] | None:
    queue = decision_queue_list(
        conn,
        range_name=range_name,
        start=start,
        end=end,
        package_id=package_id,
        limit=max(DEFAULT_LIMIT, 120),
        include_decided=include_decided,
    )
    for item in queue.get("items") or []:
        if item.get("item_key") == item_key:
            return item
    return None


def _preview_memory_candidate(
    conn: sqlite3.Connection,
    *,
    item: dict[str, Any],
    memory_text: str,
    plan_dir: str | Path | None,
) -> dict[str, Any]:
    eligibility = item.get("write_eligibility") or "ephemeral"
    if eligibility == "ephemeral":
        return _blocked("ephemeral_blocked", "临时降级候选不写入长期记忆。")
    action = "approve-candidate" if eligibility == "eligible" else "promote-review-only"
    return preview_memory_approval(
        conn,
        action=action,
        package_id=item.get("package_id") or "latest",
        candidate_key=item.get("candidate_key") or "",
        memory_text=memory_text,
        plan_dir=plan_dir,
    )


def _preview_review_signal(
    conn: sqlite3.Connection,
    *,
    item: dict[str, Any],
    memory_text: str,
    plan_dir: str | Path | None,
) -> dict[str, Any]:
    return _blocked(
        "review_signal_not_self_owned",
        "Review 线索没有明确的本人归属与可撤销来源，只允许作为当次建议，不写入长期记忆。",
    )


def _apply_local_plan(conn: sqlite3.Connection, plan: dict[str, Any], *, plan_dir: str | Path | None) -> dict[str, Any]:
    plan_id = str(plan.get("plan_id") or "")
    return _apply_failure(
        plan_id,
        "review_signal_not_self_owned",
        "旧的 Review 线索写入计划已失效；缺少本人归属与可撤销来源，未执行 mem0 写入。",
    )


def _plan_item(item: dict[str, Any]) -> dict[str, Any]:
    allowed = [
        "item_key",
        "item_type",
        "source_kind",
        "source_label",
        "safe_summary",
        "topic",
        "app",
        "time_label",
        "candidate_type",
        "confidence",
        "why_uncertain",
        "decision_question",
        "candidate_sentence",
        "uncertainty_explanation",
        "recommended_action",
        "available_actions",
        "queue_bucket",
        "source_category",
        "subject_scope",
        "screen_region_signal",
        "screen_region_signals",
        "source_package_id",
        "activity_ids",
        "time_range",
        "deletion_group_ids",
        "decision_signature",
    ]
    return {key: item.get(key) for key in allowed if key in item}


def _write_plan(plan: dict[str, Any], *, plan_dir: str | Path | None) -> None:
    path = _plan_path(str(plan["plan_id"]), plan_dir=plan_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    path.chmod(0o600)


def _load_plan(plan_id: str, *, plan_dir: str | Path | None) -> dict[str, Any]:
    if not plan_id.startswith("dq_"):
        return {"ok": False, "status": "not_local_plan"}
    path = _plan_path(plan_id, plan_dir=plan_dir)
    if not path.exists():
        return {"ok": False, "status": "plan_missing", "message": "决策队列计划不存在。"}
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"ok": False, "status": "plan_invalid", "message": "决策队列计划无法解析。"}
    return {"ok": True, "plan": plan}


def _plan_path(plan_id: str, *, plan_dir: str | Path | None) -> Path:
    root = Path(plan_dir) if plan_dir else Path.home() / PLAN_DIR_RELATIVE_PATH
    return root / f"{plan_id}.json"


def _is_plan_expired(plan: dict[str, Any]) -> bool:
    expires = str(plan.get("expires_at_utc") or "")
    if not expires:
        return True
    try:
        return datetime.fromisoformat(expires) < datetime.now(timezone.utc)
    except ValueError:
        return True


def _blocked(status: str, message: str) -> dict[str, Any]:
    return {
        "status": status,
        "raw_access": False,
        "destructive_actions": False,
        "message": message,
    }


def _apply_failure(plan_id: str, status: str, message: str) -> dict[str, Any]:
    return {
        "status": status,
        "plan_id": plan_id,
        "raw_access": False,
        "destructive_actions": False,
        "message": message,
    }


def _decision_state(decision: dict[str, Any]) -> str:
    action = decision.get("action") or ""
    if action == NEVER_WRITE:
        return "user_never_write"
    if action == CONTEXT_ONLY:
        return "user_context_only"
    if action == CORRECT_SIGNAL:
        return "user_marked_correct"
    if action in {APPROVED_WRITE, REWRITTEN_WRITE}:
        return "user_approved_write"
    return "decided"


def _feedback_actions() -> list[dict[str, str]]:
    return [
        {"action": FEEDBACK_FOLDER_SIDEBAR, "label": "这只是文件夹/侧边栏", "records_as": NEVER_WRITE},
        {"action": FEEDBACK_OLD_TASK, "label": "这是旧任务", "records_as": CONTEXT_ONLY},
        {"action": FEEDBACK_REFERENCE, "label": "这是参考资料", "records_as": CONTEXT_ONLY},
        {"action": FEEDBACK_CORRECT, "label": "这个判断正确", "records_as": CORRECT_SIGNAL},
    ]


def _safe_region_signal(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    region_kind = safe_decision_text(value.get("region_kind") or "", max_len=80)
    actionability = safe_decision_text(value.get("actionability") or "", max_len=80)
    if not region_kind and not actionability:
        return {}
    try:
        confidence = float(value.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "region_kind": region_kind,
        "actionability": actionability,
        "source": safe_decision_text(value.get("source") or "", max_len=80),
        "confidence": round(max(0.0, min(1.0, confidence)), 2),
        "reason_codes": [safe_decision_text(reason, max_len=80) for reason in (value.get("reason_codes") or [])[:8]],
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


def _queue_summary(items: list[dict[str, Any]], *, hidden_by_decision: int, auto_handled_counts: dict[str, int]) -> str:
    memory = sum(1 for item in items if item["source_kind"] == "memory_candidate")
    review = sum(1 for item in items if item["source_kind"].startswith("ai_review_"))
    auto = sum(auto_handled_counts.values())
    return f"待你判断 {len(items)} 条：长期记忆候选 {memory} 条，Review 线索 {review} 条；系统已自动处理 {auto} 条，按你的规则隐藏 {hidden_by_decision} 条。"


def _queue_sort_key(item: dict[str, Any]) -> tuple[int, str, str]:
    source = str(item.get("source_kind") or "")
    eligibility = str(item.get("write_eligibility") or "")
    priority = 4
    if source == "memory_candidate" and eligibility == "eligible":
        priority = 0
    elif source == "memory_candidate" and eligibility == "review_only":
        priority = 1
    elif source.startswith("ai_review_"):
        priority = 2
    elif source == "memory_candidate":
        priority = 3
    return (priority, str(item.get("time_label") or ""), str(item.get("item_key") or ""))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
