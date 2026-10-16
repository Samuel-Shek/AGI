from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from .activity_confirmations import confirmed_activity_view
from .daily_review import daily_review
from .document_changes import (
    ABSOLUTE_PATH_SIGNAL_RE,
    DOCUMENT_TRUSTED_PROJECTION_VERSION,
    document_authorization_lock,
)
from .timeline_export import LEARNING_CATEGORIES, cleaned_timeline_export


AI_HANDOFF_VERSION = "0.9.1"
AI_HANDOFF_SOURCE = "local_confirmed_activity_only"
VALID_RANGES = {"today", "yesterday", "7d"}
DETAIL_BLOCKED_PRIVACY_LEVELS = {"partial_redacted", "communication"}
DEFAULT_MAX_ITEMS = 40


def _trusted_document_projection(review: dict) -> dict:
    projection = review.get("document_trusted_projection")
    if isinstance(projection, dict):
        return projection
    return {
        "contract_version": DOCUMENT_TRUSTED_PROJECTION_VERSION,
        "source": "context_trusted_export",
        "projection_type": "trusted_verified_context",
        "verification_state": "verified_only",
        "trusted_export_eligible": True,
        "items": [],
        "status": "unavailable",
        "bundle_hash": "",
        "raw_access": False,
    }

LOCAL_USER_PATH_RE = re.compile(r"/Users/(?!example\b|test\b|runner\b)[A-Za-z0-9._-]+/")
SECRET_RE = re.compile(
    r"(-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{20,}\b|"
    r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}\b|\bxox[baprs]-[A-Za-z0-9-]{20,}\b|"
    r"AKIA[0-9A-Z]{16}|REDACTED_TEST_CREDENTIAL_VALUE|DO_NOT_STORE)",
    re.IGNORECASE,
)
RAW_FIELD_RE = re.compile(
    r"\b(?:ocr_text|ocr_candidate_ref|temp_frame_ref|temp_capture_ref|raw_vault|screenshot_path|"
    r"cleaned_text|memory_id|mem0_id|safe_timeline_events|clarification_note|evidence_id|"
    r"source_frame_id|content_sha256|confirmation_evidence(?:_files|_refs)?|file_name|"
    r"source_activity_ids?|deletion_group_ids?|invalid_evidence_ids?|invalid_file_names?)\b",
    re.IGNORECASE,
)
IMAGE_PATH_RE = re.compile(r"(?:^|[/'\"`\s])[^\\s'\"`]*\.(?:png|jpg|jpeg|heic)\b", re.IGNORECASE)
SQLITE_RE = re.compile(r"\bactivity\.sqlite\b|\.sqlite3?\b|\.db\b", re.IGNORECASE)
PRIVATE_CHAT_RE = re.compile(r"(私聊正文|聊天原文[:：]|private chat transcript)", re.IGNORECASE)


def ai_handoff_status(conn: sqlite3.Connection, data_root: str | Path | None = None) -> dict:
    with document_authorization_lock(conn, exclusive=False):
        return _ai_handoff_status_locked(conn, data_root)


def _ai_handoff_status_locked(conn: sqlite3.Connection, data_root: str | Path | None = None) -> dict:
    start, end, _ = resolve_ai_handoff_range("today")
    review = daily_review(conn, start, end)
    return {
        "status": "ok",
        "interface": "ai-handoff",
        "version": AI_HANDOFF_VERSION,
        "source": AI_HANDOFF_SOURCE,
        "raw_access": False,
        "destructive_actions": False,
        "memory_write_actions": False,
        "cloud_analysis": "off",
        "available_ranges": sorted(VALID_RANGES),
        "default_range": "today",
        "privacy_boundary": privacy_boundary(),
        "today": {
            "time_range": review.get("time_range", {}),
            "activity_count": int(review.get("activity_count") or 0),
            "privacy_event_count": int(review.get("privacy_event_count") or 0),
            "gap_event_count": int(review.get("gap_event_count") or 0),
            "coverage": review.get("coverage", {}),
            "document_changes": _trusted_document_projection(review),
            "document_unverified_clue_count": int(review.get("document_unverified_clue_count") or 0),
        },
        "latest_export": latest_ai_handoff_export(data_root),
    }


def build_ai_handoff_package(
    conn: sqlite3.Connection,
    *,
    range_name: str = "today",
    start: str | None = None,
    end: str | None = None,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> dict:
    with document_authorization_lock(conn, exclusive=False):
        return _build_ai_handoff_package_locked(
            conn,
            range_name=range_name,
            start=start,
            end=end,
            max_items=max_items,
        )


def _build_ai_handoff_package_locked(
    conn: sqlite3.Connection,
    *,
    range_name: str = "today",
    start: str | None = None,
    end: str | None = None,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> dict:
    resolved_start, resolved_end, range_label = resolve_ai_handoff_range(range_name, start=start, end=end)
    export = cleaned_timeline_export(conn, resolved_start, resolved_end)
    review = daily_review(conn, resolved_start, resolved_end)
    confirmation_view = confirmed_activity_view(conn, resolved_start, resolved_end)
    allowed_activity_ids = {
        str(item.get("id") or "")
        for item in confirmation_view.get("trusted_activities") or []
        if str(item.get("id") or "")
    }
    activities = [
        item
        for item in export.get("activities", [])
        if str(item.get("id") or "") in allowed_activity_ids
    ]
    activities_by_id = {str(item.get("id") or ""): item for item in activities}
    annotations = (export.get("trusted_learning_digest") or {}).get("annotations") or []
    safe_annotations = [
        _safe_annotation(item)
        for item in annotations
        if _annotation_has_readable_activity_detail(item, activities_by_id)
    ]
    safe_annotations = [item for item in safe_annotations if item]
    safe_annotations.sort(key=lambda item: item.get("time_label") or "")
    visible_items = safe_annotations[: max(0, max_items)]

    summary = _handoff_summary(export, review, activities, safe_annotations)
    data_trust = _data_trust(summary, review.get("coverage", {}), activities, safe_annotations)
    payload = {
        "status": "ok",
        "interface": "ai-handoff",
        "version": AI_HANDOFF_VERSION,
        "source": AI_HANDOFF_SOURCE,
        "cloud_analysis": "off",
        "raw_access": False,
        "destructive_actions": False,
        "memory_write_actions": False,
        "confirmation_gate": "confirmed_activity_view",
        "pending_confirmation_count": int(confirmation_view.get("pending_confirmation_count") or 0),
        "user_confirmed_count": int(confirmation_view.get("user_confirmed_count") or 0),
        "user_rejected_count": int(confirmation_view.get("user_rejected_count") or 0),
        "auto_suppressed_count": int(confirmation_view.get("auto_suppressed_count") or 0),
        # Count-only compatibility signal. Screen/OCR clue bodies are never
        # released through this legacy trusted handoff surface.
        "unverified_clue_count": int(confirmation_view.get("unverified_clue_count") or 0),
        "time_range": {
            "label": range_label,
            "from": resolved_start,
            "to": resolved_end,
        },
        "summary": summary,
        "data_trust": data_trust,
        "privacy_boundary": privacy_boundary(),
        "top_apps": _top_counter_rows(_activity_counter(activities, "apps")),
        "top_domains": _top_counter_rows(_activity_counter(activities, "domains")),
        "project_summaries": _project_summaries(safe_annotations),
        "decision_signals": _signal_items(safe_annotations, _decision_keywords(), "suspected_decision"),
        "follow_ups": _signal_items(safe_annotations, _follow_up_keywords(), "suspected_follow_up"),
        "blockers": _signal_items(safe_annotations, _blocker_keywords(), "suspected_blocker"),
        "safe_activity_samples": visible_items,
        # Trusted handoff receives only the verified ContextBundle projection.
        # Owner-local document summaries remain count-only clues here.
        "document_changes": _trusted_document_projection(review),
        "document_unverified_clue_count": int(review.get("document_unverified_clue_count") or 0),
        "downstream_ai_rules": downstream_ai_rules(),
    }
    scan = validate_ai_handoff_payload(payload)
    if not scan["ok"]:
        return _blocked_package(resolved_start, resolved_end, range_label, scan)
    payload["privacy_scan"] = scan
    return payload


def render_ai_handoff_markdown(package: dict) -> str:
    if package.get("status") != "ok":
        scan = package.get("privacy_scan") if isinstance(package.get("privacy_scan"), dict) else {}
        findings = ", ".join(scan.get("findings") or []) or "unknown"
        return (
            "# PAM AI 接手包\n\n"
            "> 已失败关闭；未输出活动正文。\n\n"
            f"- status: `{package.get('status') or 'blocked'}`\n"
            f"- reason: `{findings}`\n"
            "- raw_access: `false`\n"
            "- destructive_actions: `false`\n"
            "- cloud_analysis: `off`\n"
        )

    time_range = package.get("time_range", {})
    summary = package.get("summary", {})
    data_trust = package.get("data_trust", {})
    lines = [
        "# PAM AI 接手包",
        "",
        "> 给外部 AI 的只读安全活动包。只包含 clear 安全摘要和计数级风险提示；不包含原始截图、原始 OCR、SQLite 原始表、完整路径、token 或私聊原文。",
        "",
        f"- source: `{package.get('source')}`",
        f"- raw_access: `{str(package.get('raw_access')).lower()}`",
        f"- destructive_actions: `{str(package.get('destructive_actions')).lower()}`",
        f"- memory_write_actions: `{str(package.get('memory_write_actions')).lower()}`",
        f"- cloud_analysis: `{package.get('cloud_analysis')}`",
        f"- time_range: `{time_range.get('label')}` / `{time_range.get('from')}` to `{time_range.get('to')}`",
        f"- privacy_scan: `{(package.get('privacy_scan') or {}).get('status', 'ok')}`",
        "",
        "## 总览",
        "",
        f"- 活动计数：{summary.get('activity_count', 0)} 条；可读明细 {summary.get('readable_activity_count', 0)} 条。",
        f"- 局部清洗：{summary.get('partial_redacted_count', 0)} 条；沟通类清洗 {summary.get('communication_redacted_count', 0)} 条，只进入计数。",
        f"- 隐私保护：{summary.get('privacy_event_count', 0)} 条；未记录缺口 {summary.get('gap_event_count', 0)} 条；重复合并 {summary.get('duplicate_event_count', 0)} 条。",
        f"- 数据可信度：{data_trust.get('level', 'unknown')}。",
        "",
    ]
    warnings = data_trust.get("warnings") or []
    if warnings:
        lines.extend(["## 低置信 / 风险提示", ""])
        for warning in warnings:
            lines.append(f"- {warning.get('message') or warning.get('type') or 'unknown'}")
        lines.append("")

    _append_project_markdown(lines, package.get("project_summaries") or [])
    _append_signal_markdown(lines, "疑似决策", package.get("decision_signals") or [])
    _append_signal_markdown(lines, "待办 / 跟进", package.get("follow_ups") or [])
    _append_signal_markdown(lines, "阻塞 / 异常", package.get("blockers") or [])
    _append_samples_markdown(lines, package.get("safe_activity_samples") or [])
    lines.extend(["## AI 使用规则", ""])
    for rule in package.get("downstream_ai_rules") or []:
        lines.append(f"- {rule}")
    return "\n".join(lines).rstrip() + "\n"


def write_ai_handoff_export(path: str | Path, content: str, *, package: dict, output_format: str) -> dict:
    scan = validate_ai_handoff_text(content)
    if package.get("status") != "ok":
        raise ValueError("ai-handoff package is blocked; no export was written")
    if not scan["ok"]:
        raise ValueError("ai-handoff privacy scan failed: " + ", ".join(scan["findings"]))
    output = Path(path).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8")
    output.chmod(0o600)
    summary = package.get("summary") if isinstance(package.get("summary"), dict) else {}
    result = {
        "status": "ok",
        "format": output_format,
        "path": str(output.resolve()),
        "activity_count": int(summary.get("activity_count") or 0),
        "readable_activity_count": int(summary.get("readable_activity_count") or 0),
        "project_count": len(package.get("project_summaries") or []),
        "privacy_scan": scan,
        "raw_access": False,
        "destructive_actions": False,
        "memory_write_actions": False,
        "cloud_analysis": "off",
    }
    if output_format == "json":
        result["json_path"] = result["path"]
    else:
        result["markdown_path"] = result["path"]
    return result


def validate_ai_handoff_payload(payload: object) -> dict:
    return validate_ai_handoff_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def validate_ai_handoff_text(text: str) -> dict:
    findings = []
    checks = [
        ("local_user_path", LOCAL_USER_PATH_RE),
        ("secret_or_token", SECRET_RE),
        ("raw_or_internal_field", RAW_FIELD_RE),
        ("image_path", IMAGE_PATH_RE),
        ("sqlite_path", SQLITE_RE),
        ("private_chat_transcript", PRIVATE_CHAT_RE),
    ]
    for name, pattern in checks:
        if pattern.search(text):
            findings.append(name)
    if ABSOLUTE_PATH_SIGNAL_RE.search(text):
        findings.append("absolute_local_path")
    return {"ok": not findings, "status": "ok" if not findings else "blocked", "findings": findings}


def resolve_ai_handoff_range(
    range_name: str = "today",
    *,
    start: str | None = None,
    end: str | None = None,
    now: datetime | None = None,
) -> tuple[str, str, str]:
    if range_name not in VALID_RANGES:
        raise ValueError(f"unsupported ai-handoff range: {range_name}")
    current = (now or datetime.now().astimezone()).astimezone()
    today_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    if range_name == "today":
        resolved_start, resolved_end = today_start, current
    elif range_name == "yesterday":
        resolved_start, resolved_end = today_start - timedelta(days=1), today_start
    else:
        resolved_start, resolved_end = current - timedelta(days=7), current
    label = "custom" if start or end else range_name
    return (
        start or resolved_start.isoformat(timespec="seconds"),
        end or resolved_end.isoformat(timespec="seconds"),
        label,
    )


def privacy_boundary() -> dict:
    return {
        "raw_access": False,
        "destructive_actions": False,
        "memory_write_actions": False,
        "cloud_analysis": "off",
        "detail_policy": "normal clear activity summaries only",
        "partial_redacted_policy": "count_only",
        "communication_policy": "count_only",
        "excluded": [
            "raw_screenshots",
            "raw_ocr",
            "sqlite_raw_tables",
            "full_local_paths",
            "tokens",
            "private_chat_transcripts",
            "ha_package_body",
            "persistent_identifiers",
        ],
    }


def downstream_ai_rules() -> list[str]:
    return [
        "只能把本包内容当作安全摘要和线索，不能扩写成未经验证的事实。",
        "视频播放、聊天窗口、Finder 停留或 PAM 后台运行只能当背景上下文，不能写成用户完成了工程动作。",
        "低置信、Unknown app、低覆盖、隐私保护和未记录缺口必须保留不确定性。",
        "不要要求读取 raw 截图、原始 OCR、SQLite 原始表、完整本机路径、token 或私聊原文。",
        "不能通过本包写入、改写或删除长期记忆；长期记忆仍走 memory-approval 预览和执行链路。",
    ]


def latest_ai_handoff_export(data_root: str | Path | None) -> dict:
    if not data_root:
        return {"tracked": False, "status": "not_tracked"}
    exports_root = Path(data_root).expanduser() / "exports"
    if not exports_root.exists():
        return {"tracked": True, "status": "none", "path": ""}
    candidates = [
        path
        for path in exports_root.glob("pam-ai-handoff-*")
        if path.is_file() and path.suffix.lower() in {".json", ".md"}
    ]
    if not candidates:
        return {"tracked": True, "status": "none", "path": ""}
    latest = max(candidates, key=lambda item: item.stat().st_mtime)
    return {
        "tracked": True,
        "status": "found",
        "path": str(latest),
        "updated_at": datetime.fromtimestamp(latest.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
    }


def _annotation_has_readable_activity_detail(item: dict, activities_by_id: dict[str, dict]) -> bool:
    if item.get("kind") != "activity":
        return False
    activity = activities_by_id.get(str(item.get("event_id") or ""))
    if not activity:
        return False
    if str(activity.get("quarantine_status") or "") != "clear":
        return False
    privacy_level = str(activity.get("privacy_level") or "normal")
    if privacy_level in DETAIL_BLOCKED_PRIVACY_LEVELS:
        return False
    return True


def _safe_annotation(item: dict) -> dict:
    summary = _clip_text(item.get("safe_summary") or "", 220)
    return {
        "time_label": str(item.get("time_label") or ""),
        "app": _safe_text(item.get("app") or ""),
        "workflow": str(item.get("workflow") or "unknown"),
        "workflow_label": LEARNING_CATEGORIES.get(str(item.get("workflow") or "unknown"), "未分类"),
        "topic": _safe_text(item.get("topic") or "未分类"),
        "learnability": str(item.get("learnability") or "unknown"),
        "reason": str(item.get("reason") or ""),
        "confidence": _confidence_for_annotation(item, summary),
        "safe_summary": summary,
    }


def _handoff_summary(export: dict, review: dict, activities: list[dict], safe_annotations: list[dict]) -> dict:
    # Redaction counts on this trusted surface use the trusted activity
    # denominator. Owner-facing unverified observations are reported only via
    # ``unverified_clue_count`` and must not inflate trusted handoff quality.
    partial_redacted_count = sum(
        1 for item in activities if str(item.get("privacy_level") or "") == "partial_redacted"
    )
    communication_redacted_count = sum(
        1 for item in activities if str(item.get("privacy_level") or "") == "communication"
    )
    learning = (export.get("learning_digest") or {}).get("learnability_counts") or {}
    return {
        "activity_count": len(activities),
        "readable_activity_count": len(safe_annotations),
        "partial_redacted_count": partial_redacted_count,
        "communication_redacted_count": communication_redacted_count,
        "privacy_event_count": len(export.get("privacy_events") or []),
        "gap_event_count": len(export.get("gap_events") or []),
        "duplicate_event_count": len(export.get("duplicate_events") or []),
        "low_signal_count": int(learning.get("low_signal") or 0),
        "protected_count": int(learning.get("protected") or 0),
        "recorded_hours": int((review.get("coverage") or {}).get("recorded_hours") or 0),
        "frames_dropped": int((review.get("coverage") or {}).get("frames_dropped") or 0),
        "unverified_clue_count": int(export.get("unverified_clue_count") or 0),
    }


def _data_trust(summary: dict, coverage: dict, activities: list[dict], safe_annotations: list[dict]) -> dict:
    warnings: list[dict] = []
    total = max(1, len(activities))
    unknown_count = _activity_counter(activities, "apps").get("Unknown", 0)
    if not activities:
        warnings.append({"type": "no_activity", "message": "这个时间段没有可展示的 clear 活动；不要推断用户没有工作。"})
    if not safe_annotations and activities:
        warnings.append({"type": "no_readable_details", "message": "这个时间段有活动，但可给 AI 阅读的 normal clear 明细为 0。"})
    if unknown_count / total >= 0.2:
        warnings.append({"type": "unknown_app_high", "message": f"Unknown app 占比偏高：{unknown_count}/{len(activities)}，项目归因需要保留不确定性。"})
    if int(summary.get("gap_event_count") or 0) > 0:
        warnings.append({"type": "gap_events_present", "message": f"存在 {summary.get('gap_event_count')} 条未记录缺口，不能把本时间段当作完整记录。"})
    if int(summary.get("partial_redacted_count") or 0) > 0 or int(summary.get("communication_redacted_count") or 0) > 0:
        warnings.append({"type": "redacted_details_count_only", "message": "局部清洗或沟通类内容只进入计数，不进入 AI 可读明细。"})
    if int(summary.get("unverified_clue_count") or 0) > 0:
        warnings.append(
            {
                "type": "unverified_screen_clues_present",
                "message": (
                    f"存在 {summary.get('unverified_clue_count')} 条未验证屏幕/OCR 线索；"
                    "其正文未进入可信接手包。"
                ),
            }
        )
    if int(coverage.get("recorded_hours") or 0) == 0 and activities:
        warnings.append({"type": "coverage_ledger_missing", "message": "活动存在，但覆盖 ledger 没有记录小时数；覆盖率判断为低置信。"})
    level = "high"
    if warnings:
        level = "medium"
    if not safe_annotations or any(item["type"] in {"unknown_app_high", "coverage_ledger_missing"} for item in warnings):
        level = "low"
    return {"level": level, "warnings": warnings, "coverage": coverage}


def _project_summaries(annotations: list[dict]) -> list[dict]:
    buckets: dict[str, dict] = {}
    for item in annotations:
        workflow = str(item.get("workflow") or "unknown")
        bucket = buckets.setdefault(
            workflow,
            {
                "workflow": workflow,
                "label": item.get("workflow_label") or LEARNING_CATEGORIES.get(workflow, workflow),
                "event_count": 0,
                "topics": Counter(),
                "apps": Counter(),
                "representative_items": [],
                "confidence_values": [],
            },
        )
        bucket["event_count"] += 1
        if item.get("topic"):
            bucket["topics"][str(item["topic"])] += 1
        if item.get("app"):
            bucket["apps"][str(item["app"])] += 1
        bucket["confidence_values"].append(float(item.get("confidence") or 0.4))
        if len(bucket["representative_items"]) < 3:
            bucket["representative_items"].append(
                {
                    "time_label": item.get("time_label") or "",
                    "app": item.get("app") or "",
                    "topic": item.get("topic") or "",
                    "safe_summary": item.get("safe_summary") or "",
                    "confidence": item.get("confidence") or 0.4,
                }
            )
    rows = []
    for bucket in buckets.values():
        values = bucket.pop("confidence_values")
        topics = bucket.pop("topics")
        apps = bucket.pop("apps")
        avg = sum(values) / max(1, len(values))
        rows.append(
            {
                **bucket,
                "confidence": round(avg, 2),
                "top_topics": [name for name, _ in topics.most_common(5)],
                "top_apps": [name for name, _ in apps.most_common(5)],
            }
        )
    return sorted(rows, key=lambda item: (-int(item["event_count"]), str(item["label"])))[:12]


def _signal_items(annotations: list[dict], keywords: tuple[str, ...], signal_type: str) -> list[dict]:
    output = []
    for item in annotations:
        text = f"{item.get('topic') or ''} {item.get('safe_summary') or ''}".casefold()
        matched = [keyword for keyword in keywords if keyword in text]
        if not matched:
            continue
        confidence = float(item.get("confidence") or 0.4)
        output.append(
            {
                "type": signal_type,
                "time_label": item.get("time_label") or "",
                "workflow": item.get("workflow") or "unknown",
                "topic": item.get("topic") or "",
                "app": item.get("app") or "",
                "safe_summary": item.get("safe_summary") or "",
                "confidence": round(min(confidence, 0.74), 2),
                "recommendation": "作为线索复核；不要写成已确认事实，除非用户或源文档另行确认。",
            }
        )
    return output[:12]


def _activity_counter(activities: list[dict], key: str) -> Counter[str]:
    counter: Counter[str] = Counter()
    for activity in activities:
        for value in _json_list(activity.get(key)):
            if value:
                counter[value] += 1
    return counter


def _top_counter_rows(counter: Counter[str], limit: int = 8) -> list[dict]:
    return [{"name": name, "count": count} for name, count in counter.most_common(limit)]


def _json_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item]
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return [str(item) for item in parsed if item]


def _safe_text(value: object) -> str:
    text = " ".join(str(value or "").split())
    text = LOCAL_USER_PATH_RE.sub("[path_redacted]/", text)
    text = ABSOLUTE_PATH_SIGNAL_RE.sub("[path_redacted]", text)
    text = SECRET_RE.sub("[secret_redacted]", text)
    return text


def _clip_text(value: object, limit: int) -> str:
    text = _safe_text(value)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _confidence_for_annotation(item: dict, summary: str) -> float:
    value = 0.52
    if item.get("learnability") == "useful":
        value += 0.12
    if len(summary) >= 80:
        value += 0.12
    if str(item.get("workflow") or "unknown") != "unknown":
        value += 0.08
    if str(item.get("app") or "") in {"", "Unknown"}:
        value -= 0.18
    if str(item.get("reason") or "") == "safe_cleaned_content":
        value += 0.05
    return round(max(0.2, min(0.86, value)), 2)


def _blocked_package(start: str, end: str, label: str, scan: dict) -> dict:
    return {
        "status": "blocked",
        "interface": "ai-handoff",
        "version": AI_HANDOFF_VERSION,
        "source": AI_HANDOFF_SOURCE,
        "raw_access": False,
        "destructive_actions": False,
        "memory_write_actions": False,
        "cloud_analysis": "off",
        "time_range": {"label": label, "from": start, "to": end},
        "privacy_boundary": privacy_boundary(),
        "privacy_scan": scan,
        "message": "AI handoff package failed closed before exposing readable activity details.",
    }


def _decision_keywords() -> tuple[str, ...]:
    return ("decision", "decided", "choose", "chosen", "决定", "决策", "选定", "采用", "结论", "默认", "不再")


def _follow_up_keywords() -> tuple[str, ...]:
    return ("todo", "follow up", "next", "rerun", "retry", "待办", "跟进", "下一步", "需要", "补上", "复核", "验证")


def _blocker_keywords() -> tuple[str, ...]:
    return ("blocked", "failed", "failure", "error", "traceback", "missing", "阻塞", "失败", "报错", "异常", "缺失", "权限")


def _append_project_markdown(lines: list[str], rows: list[dict]) -> None:
    lines.extend(["## 项目推进", ""])
    if not rows:
        lines.extend(["暂无可给 AI 阅读的项目明细。", ""])
        return
    for row in rows:
        topics = "、".join(row.get("top_topics") or []) or "无"
        apps = "、".join(row.get("top_apps") or []) or "无"
        lines.append(
            f"- {row.get('label') or row.get('workflow')}: {row.get('event_count', 0)} 条，"
            f"confidence {row.get('confidence', 0)}；主题：{topics}；App：{apps}"
        )
    lines.append("")


def _append_signal_markdown(lines: list[str], title: str, rows: list[dict]) -> None:
    lines.extend([f"## {title}", ""])
    if not rows:
        lines.extend(["暂无。", ""])
        return
    for row in rows:
        lines.append(
            f"- {row.get('time_label') or 'unknown'} · {row.get('topic') or '未分类'} · "
            f"{row.get('app') or '无'} · confidence {row.get('confidence', 0)}：{row.get('safe_summary') or ''}"
        )
    lines.append("")


def _append_samples_markdown(lines: list[str], rows: list[dict]) -> None:
    lines.extend(["## 安全片段样例", ""])
    if not rows:
        lines.extend(["暂无可读样例。", ""])
        return
    for row in rows[:20]:
        lines.append(
            f"- {row.get('time_label') or 'unknown'} · {row.get('workflow_label') or row.get('workflow') or '未分类'} · "
            f"{row.get('app') or '无'}：{row.get('safe_summary') or ''}"
        )
    lines.append("")
