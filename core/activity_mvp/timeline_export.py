from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

from .activity_confirmations import confirmed_activity_view
from .app_identity import BROWSER_FAMILIES, app_identity_in, normalize_app_name
from .document_changes import document_authorization_lock
from .local_endpoints import validate_local_http_endpoint

from .adult_policy import redact_terms as redact_adult_terms
from .pipeline import parse_time
from .privacy import sanitize_privacy_event_context
from .screen_regions import (
    ACTIONABILITY_CONTEXT_ONLY,
    ACTIONABILITY_NOISE,
    ACTIONABILITY_UNKNOWN,
    REGION_UNKNOWN,
    region_distribution,
    region_is_actionable,
    region_is_noise,
    sanitize_screen_region_signal,
)


LOCAL_PATH_RE = re.compile(r"(/Users/[^\s\"'`，。；;]*|/tmp/[^\s\"'`，。；;]*|~/[^\s\"'`，。；;]*)", re.I)
SECRET_VALUE_RE = re.compile(r"(-----BEGIN .*PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{20,}\b)", re.I)
CLEANED_CONTENT_MAX_CHARS = 2400
CLEANED_CONTENT_NEAR_CAP_CHARS = 2350
LEARNING_SNIPPET_CHARS = 220
DEFAULT_MEMORY_CANDIDATE_LIMIT = 40
SCREEN_REGION_ENFORCEMENT_ENV = "PAM_SCREEN_REGION_ENFORCEMENT"
COMMUNICATION_APP_HINTS = {
    "wechat",
    "微信",
    "lark",
    "飞书",
    "feishu",
    "messages",
    "imessage",
    "telegram",
    "signal",
    "discord",
    "slack",
    "zoom",
    "microsoft teams",
}
LEARNING_CATEGORIES = {
    "coding_debugging": "代码 / 调试",
    "writing": "写作 / 文档",
    "research": "研究 / 浏览",
    "file_management": "文件整理",
    "communication": "沟通协作",
    "system_app_control": "系统 / App 控制",
    "media": "媒体处理",
    "unknown": "未分类",
    "protected": "隐私保护",
    "duplicate": "重复内容",
    "gap": "未记录缺口",
}
WRITE_ELIGIBILITY_ELIGIBLE = "eligible"
WRITE_ELIGIBILITY_REVIEW_ONLY = "review_only"
WRITE_ELIGIBILITY_EPHEMERAL = "ephemeral"
EVIDENCE_LAYER_CONFIRMED = "confirmed_action"
EVIDENCE_LAYER_CONTEXT = "context_clue"
EVIDENCE_LAYER_NOISE = "noise_or_reference"
ELIGIBLE_CANDIDATE_TYPES = {"decision", "preference", "blocker", "follow_up", "project_fact", "document_work"}
TRUSTED_APP_COMPATIBLE_MERGE_TYPES = {"decision", "follow_up", "project_fact", "document_work", "work_context"}
WEAK_WORKFLOWS_FOR_MEMORY = {"unknown", "media", "system_app_control"}
MEMORY_CANDIDATE_MERGE_SECONDS = 180
MEMORY_DOCUMENT_CANDIDATE_MERGE_SECONDS = 24 * 60 * 60
TRUSTED_MEMORY_APPS = {
    "codex",
    "claude",
    "chatgpt",
    "obsidian",
    "typora",
    "preview",
    "textedit",
    "pages",
    "microsoft word",
    "word",
    "wps",
    "面试助手",
    "voicememos",
    "voice memos",
    "terminal",
    "warp",
    "cursor",
    "visual studio code",
    "vscode",
}
MEDIA_PLAYER_APPS = {"iina", "vlc", "quicktime player", "quicktime", "mpv", "infuse", "elmedia"}
BROWSER_APPS = {"dia", "chrome", "google chrome", "safari", "arc", "firefox", "edge", "microsoft edge"}
SYSTEM_UI_APPS = {"system settings", "系统设置", "finder", "launchpad", "dock", "menubar", "menu bar"}
TASK_LIST_APPS = {"ticktick", "滴答清单", "things", "todoist", "reminders", "提醒事项", "omnifocus"}
GENERIC_PROJECT_TAGS = {"ai-workflow", "obsidian"}
PRIMARY_ACTION_APPS = {
    "codex",
    "claude",
    "chatgpt",
    "hermes",
    "obsidian",
    "typora",
    "textedit",
    "pages",
    "microsoft word",
    "word",
    "wps",
    "terminal",
    "warp",
    "cursor",
    "visual studio code",
    "vscode",
}
READ_ONLY_CONTEXT_APPS = {*SYSTEM_UI_APPS, *MEDIA_PLAYER_APPS, "preview", "photos", "quicktime player", "quicktime"}
ANCHOR_DOCUMENT_SIGNAL = "document_signal"
ANCHOR_TRUSTED_APP = "trusted_app_context"
ANCHOR_KEYWORD_ONLY = "keyword_only"
ANCHOR_WEAK = "weak_or_no_anchor"
SUBJECT_SELF_OR_USER_OWNED = "self_or_user_owned"
SUBJECT_USER_WORKFLOW_ONLY = "user_workflow_only"
SUBJECT_THIRD_PARTY = "third_party_subject"
SUBJECT_AMBIGUOUS = "ambiguous_subject"
SOURCE_CATEGORY_SELF_RESUME = "self_resume"
SOURCE_CATEGORY_TARGET_ROLE_JD = "target_role_jd"
SOURCE_CATEGORY_THIRD_PARTY_RESUME = "third_party_resume"
SOURCE_CATEGORY_INTERVIEW_NOTES = "interview_notes"
SOURCE_CATEGORY_PERSONAL_MEDICAL_DOCUMENT = "personal_medical_document"
SOURCE_CATEGORY_AMBIGUOUS_DOCUMENT = "ambiguous_document"
SOURCE_CATEGORY_GENERAL_DOCUMENT = "general_document"
SOURCE_SCOPE_SELF_OWNED = "self_owned"
SOURCE_SCOPE_REFERENCE_ONLY = "reference_only"
SOURCE_SCOPE_THIRD_PARTY = "third_party"
SOURCE_SCOPE_CONTEXT_ONLY = "context_only"
SOURCE_SCOPE_AMBIGUOUS = "ambiguous"
SOURCE_SCOPE_PROTECTED = "protected"
DOCUMENT_REFERENCE_ONLY_POLICIES = {"reference_only", "blocked_subject"}
DOCUMENT_REFERENCE_ONLY_CATEGORIES = {
    SOURCE_CATEGORY_TARGET_ROLE_JD,
    SOURCE_CATEGORY_THIRD_PARTY_RESUME,
    SOURCE_CATEGORY_AMBIGUOUS_DOCUMENT,
}
DOCUMENT_PERSONAL_MEDICAL_POLICIES = {"blocked_personal_medical"}
DOCUMENT_PERSONAL_MEDICAL_CATEGORIES = {SOURCE_CATEGORY_PERSONAL_MEDICAL_DOCUMENT}
PERSONAL_MEDICAL_RISK_FLAGS = {"personal_medical_content", "personal_medical_content_suppressed", "personal_medical_document"}
DOCUMENT_THIRD_PARTY_CATEGORIES = {SOURCE_CATEGORY_THIRD_PARTY_RESUME}
DOCUMENT_AMBIGUOUS_CATEGORIES = {SOURCE_CATEGORY_AMBIGUOUS_DOCUMENT}
DOCUMENT_SELF_OWNED_CATEGORIES = {SOURCE_CATEGORY_SELF_RESUME}
SELF_SUBJECT_HINTS = {
    "local owner",
    "local-owner",
    "bigking",
    "alex",
    "exampleuser",
    "我的简历",
    "我的 cv",
    "my resume",
    "my cv",
    "自我介绍",
    "自我定位",
}
EXPLICIT_THIRD_PARTY_SUBJECT_HINTS = {
    "applicant",
    "candidate cv",
    "candidate resume",
    "candidate profile",
    "candidate's cv",
    "candidate's resume",
    "third party",
    "third-party",
    "someone else's",
    "other person's",
    "候选人",
    "应聘者",
    "求职者",
    "他人",
    "别人",
    "对方",
    "张三",
    "李四",
    "王五",
}
RESUME_SUBJECT_HINTS = {
    "简历",
    "resume",
    "cv",
    "curriculum vitae",
    "面试",
    "岗位",
    "求职",
    "应聘",
}
PROFILE_DETAIL_HINTS = {
    "教育经历",
    "工作经历",
    "项目经历",
    "实习经历",
    "联系电话",
    "联系方式",
    "手机号",
    "手机",
    "邮箱",
    "email",
    "phone",
    "毕业于",
    "曾任",
    "现任",
    "任职",
    "工作年限",
    "years of experience",
}


def cleaned_timeline_export(conn: sqlite3.Connection, start: str | None = None, end: str | None = None) -> dict:
    # Keep one shared authorization lease through every query and the final
    # payload assembly.  A completed revocation/delete therefore cannot be
    # followed by an export assembled from a stale pre-delete snapshot.
    with document_authorization_lock(conn, exclusive=False):
        return _cleaned_timeline_export_locked(conn, start, end)


def _cleaned_timeline_export_locked(
    conn: sqlite3.Connection,
    start: str | None = None,
    end: str | None = None,
) -> dict:
    confirmation_view = confirmed_activity_view(conn, start, end)
    activities = list(confirmation_view.get("owner_activities") or [])
    trusted_activities = list(confirmation_view.get("trusted_activities") or [])
    unverified_activity_clues = list(confirmation_view.get("unverified_activity_clues") or [])
    unverified_clues = list(confirmation_view.get("unverified_clues") or [])
    privacy_events = _privacy_events(conn, start, end)
    gap_events = _gap_events(conn, start, end)
    duplicate_events = _duplicate_events(conn, start, end)
    items: list[dict] = []
    for item in activities:
        items.append({"kind": "activity", **item})
    for item in privacy_events:
        items.append({"kind": "privacy_event", **item})
    for item in gap_events:
        items.append({"kind": "gap_event", **item})
    for item in duplicate_events:
        items.append({"kind": "duplicate_event", **item})
    items.sort(key=lambda item: parse_time(str(item.get("started_at") or item.get("ended_at"))))
    export = {
        "time_range": {"from": start, "to": end},
        "confirmation_gate": "confirmed_activity_view",
        "pending_confirmation_count": int(confirmation_view.get("pending_confirmation_count") or 0),
        "clear_activity_count": int(confirmation_view.get("clear_activity_count") or 0),
        "projection_version": "OwnerObservationV1+TrustedActivityV1",
        "activities": activities,
        "owner_observations": activities,
        "trusted_activities": trusted_activities,
        # Local-only privacy-cleaned clue detail. HA/AI package builders do not
        # serialize this field; their inputs remain the verified ``activities``
        # and the content-free ``items`` projection above.
        "unverified_activity_clues": unverified_activity_clues,
        "unverified_clue_count": len(unverified_clues),
        "unverified_clues": unverified_clues,
        "privacy_events": privacy_events,
        "gap_events": gap_events,
        "duplicate_events": duplicate_events,
        "items": items,
    }
    export["quality_diagnostics"] = _quality_diagnostics(
        conn,
        start,
        end,
        activities,
        privacy_events,
        gap_events,
        duplicate_events,
    )
    export["learning_digest"] = build_learning_digest(export)
    trusted_items: list[dict] = [
        {"kind": "activity", **item} for item in trusted_activities
    ]
    trusted_items.extend({"kind": "unverified_clue", **item} for item in unverified_clues)
    trusted_items.extend({"kind": "privacy_event", **item} for item in privacy_events)
    trusted_items.extend({"kind": "gap_event", **item} for item in gap_events)
    trusted_items.extend({"kind": "duplicate_event", **item} for item in duplicate_events)
    trusted_items.sort(key=lambda item: parse_time(str(item.get("started_at") or item.get("ended_at"))))
    export["trusted_items"] = trusted_items
    export["ha_source_events"] = trusted_items
    export["trusted_learning_digest"] = build_learning_digest({"items": trusted_items})
    return export


def render_timeline_markdown(
    activities: list[dict],
    start: str | None = None,
    end: str | None = None,
) -> str:
    lines = [
        "# 清洗后活动日志",
        "",
        "> 仅包含通过本地 Privacy Firewall 的 clear 活动摘要；不包含原始截图、原始 OCR 或过滤前内容。",
        "",
        f"- source: `local_confirmed_activity_only`",
        f"- raw_access: `false`",
        f"- cloud_analysis: `off`",
        f"- time_range: `{_local_timestamp(start) or 'unbounded'}` to `{_local_timestamp(end) or 'unbounded'}`",
        f"- activity_count: `{len(activities)}`",
        "",
        "## 活动记录",
        "",
    ]
    if not activities:
        lines.append("这个时间段没有可展示的清洗后活动。")
        return "\n".join(lines).rstrip() + "\n"

    for index, item in enumerate(activities, start=1):
        time_label = _time_label(item)
        summary = _sanitize_text(item.get("activity_summary") or "")
        apps = _join_list(item.get("apps"))
        domains = _join_list(item.get("domains"))
        evidence_refs = _join_list(item.get("evidence_refs"))
        privacy_level = str(item.get("privacy_level") or "normal")
        redaction_confidence = item.get("redaction_confidence")
        confidence = "" if redaction_confidence is None else f"{float(redaction_confidence):.2f}"

        lines.extend(
            [
                f"### {index}. {time_label}",
                "",
                summary or "无活动摘要。",
                "",
                f"- App: `{apps or '无'}`",
                f"- Domain: `{domains or '无'}`",
                f"- Evidence: `{evidence_refs or '无'}`",
                f"- Privacy: `{privacy_level}`",
                f"- Redaction confidence: `{confidence or '未知'}`",
                f"- Activity ID: `{item.get('id') or 'unknown'}`",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def render_cleaned_timeline_markdown(export: dict) -> str:
    start = export.get("time_range", {}).get("from")
    end = export.get("time_range", {}).get("to")
    activities = export.get("activities", [])
    privacy_events = export.get("privacy_events", [])
    gap_events = export.get("gap_events", [])
    duplicate_events = export.get("duplicate_events", [])
    unverified_clues = export.get("unverified_clues", [])
    items = export.get("items", [])
    cleaning = _cleaning_summary(activities, privacy_events, gap_events, duplicate_events)
    lines = [
        "# 清洗后完整时间线",
        "",
        "> 包含已通过隐私过滤的清洗后完整内容，以及不含内容的隐私保护事件和未记录缺口；不包含原始截图、原始 OCR 或过滤前内容。",
        "",
        f"- source: `local_cleaned_timeline`",
        f"- raw_access: `false`",
        f"- cloud_analysis: `off`",
        f"- time_range: `{_local_timestamp(start) or 'unbounded'}` to `{_local_timestamp(end) or 'unbounded'}`",
        f"- clear_activity_count: `{int(export.get('clear_activity_count') or len(activities))}`",
        f"- unverified_clue_count: `{len(unverified_clues)}`",
        f"- partial_redacted_count: `{cleaning['partial_redacted_count']}`",
        f"- duplicate_event_count: `{len(duplicate_events)}`",
        f"- privacy_event_count: `{len(privacy_events)}`",
        f"- gap_event_count: `{len(gap_events)}`",
        f"- total_event_count: `{len(items)}`",
        "",
        "## 覆盖与清洗说明",
        "",
        cleaning["summary"],
        "",
        "- 本机观察：通过隐私防火墙的清洗后内容可供所有者本地复盘，并明确标记 verified/unverified。",
        "- 可信导出：普通屏幕/OCR 正文不会进入 HA、外部交付或长期记忆；这些通道只收到不含正文的线索事件。",
        "- 局部清洗：只保留清洗后的安全内容，被清洗的标签、侧边栏词或风险词不进入搜索和 HA 包。",
        "- 重复清洗：完全重复的内容不再重复记录；部分重复只保留新增或变化内容。",
        "- 隐私保护：疑似密钥、验证码、账户、余额、交易等内容只留下不含内容的保护事件。",
        "- 未记录缺口：证据不足或需要复核的片段只记录时间段，不保存具体内容。",
        "",
        "## 时间线",
        "",
    ]
    if not items:
        lines.append("这个时间段没有可展示的清洗后事件。")
        return "\n".join(lines).rstrip() + "\n"

    for index, item in enumerate(items, start=1):
        kind = item.get("kind")
        if kind == "activity":
            _append_activity(lines, index, item)
        elif kind == "unverified_clue":
            _append_unverified_clue(lines, index, item)
        elif kind == "privacy_event":
            _append_privacy_event(lines, index, item)
        elif kind == "gap_event":
            _append_gap_event(lines, index, item)
        elif kind == "duplicate_event":
            _append_duplicate_event(lines, index, item)
    return "\n".join(lines).rstrip() + "\n"


def render_learning_timeline_markdown(export: dict) -> str:
    digest = export.get("learning_digest") or build_learning_digest(export)
    quality = export.get("quality_diagnostics") or {}
    start = export.get("time_range", {}).get("from")
    end = export.get("time_range", {}).get("to")
    learnability = digest.get("learnability_counts", {})
    lines = [
        "# 学习摘要时间线",
        "",
        "> 面向本机复盘和 HA 输入的结构化摘要。只使用已通过 Privacy Firewall 的清洗后内容；不包含原始截图、原始 OCR、聊天原文或被清洗掉的内容。",
        "",
        f"- source: `local_learning_timeline`",
        f"- raw_access: `false`",
        f"- cloud_analysis: `off`",
        f"- memory_write_policy: `ephemeral_only`",
        f"- time_range: `{_local_timestamp(start) or 'unbounded'}` to `{_local_timestamp(end) or 'unbounded'}`",
        f"- useful_count: `{learnability.get('useful', 0)}`",
        f"- low_signal_count: `{learnability.get('low_signal', 0)}`",
        f"- protected_count: `{learnability.get('protected', 0)}`",
        f"- duplicate_count: `{learnability.get('duplicate', 0)}`",
        f"- gap_count: `{learnability.get('gap', 0)}`",
        "",
        "## 质量诊断",
        "",
        f"- 活动：`{quality.get('activity_count', 0)}` 条，局部清洗 `{quality.get('partial_redacted_count', 0)}` 条，聊天类清洗 `{quality.get('communication_redaction_count', 0)}` 条。",
        f"- 重复与缺口：完全重复合并 `{quality.get('duplicate_frame_count', 0)}` 次，隐私保护 `{quality.get('privacy_event_count', 0)}` 条，未记录缺口 `{quality.get('gap_event_count', 0)}` 条。",
        f"- 内容长度：接近 {CLEANED_CONTENT_MAX_CHARS} 字上限 `{quality.get('near_cleaned_content_cap_count', 0)}` 条，空/极短信号 `{quality.get('short_or_empty_content_count', 0)}` 条。",
        "",
        "## 学习摘要",
        "",
        digest.get("summary") or "这个时间段没有足够的可学习内容。",
        "",
    ]
    _append_learning_group_section(lines, "工作流聚合", digest.get("workflow_summary", []), "workflow")
    _append_learning_group_section(lines, "App 聚合", digest.get("app_summary", []), "app")
    _append_learning_group_section(lines, "时间块", digest.get("time_blocks", []), "time_block")
    _append_memory_candidates(lines, digest.get("memory_candidates", []))
    _append_low_signal_reasons(lines, digest.get("low_signal_reasons", []))
    lines.extend(["## 带标注时间线", ""])
    annotations = digest.get("annotations", [])
    if not annotations:
        lines.append("这个时间段没有可展示的学习标注。")
    for index, item in enumerate(annotations, start=1):
        label = LEARNING_CATEGORIES.get(str(item.get("workflow") or "unknown"), "未分类")
        lines.extend(
            [
                f"### {index}. {label} - {item.get('time_label') or 'unknown'}",
                "",
                f"- Learnability: `{item.get('learnability') or 'unknown'}`",
                f"- Workflow: `{item.get('workflow') or 'unknown'}`",
                f"- Topic: `{item.get('topic') or 'general'}`",
                f"- App: `{item.get('app') or '无'}`",
                f"- Reason: `{item.get('reason') or 'none'}`",
                "",
                item.get("safe_summary") or "没有可学习摘要。",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def build_learning_digest(export: dict) -> dict:
    classifier_status = _learning_classifier_status()
    annotations = [
        _learning_annotation(item, classifier_status)
        for item in export.get("items", [])
    ]
    learnability_counts = Counter(str(item.get("learnability") or "unknown") for item in annotations)
    workflow_summary = _group_learning_annotations(annotations, "workflow", include_label=True)
    app_summary = _group_learning_annotations(annotations, "app", include_label=False)
    time_blocks = _time_block_summary(annotations)
    low_signal_reasons = _low_signal_reason_summary(annotations)
    screen_region_summary = _screen_region_summary(annotations)
    candidate_result = _memory_candidate_result(annotations)
    memory_candidates = candidate_result["memory_candidates"]
    memory_candidate_quality = candidate_result["memory_candidate_quality"]
    useful = learnability_counts.get("useful", 0)
    low_signal = learnability_counts.get("low_signal", 0)
    protected = learnability_counts.get("protected", 0)
    duplicate = learnability_counts.get("duplicate", 0)
    gap = learnability_counts.get("gap", 0)
    top_workflows = "、".join(item["label"] for item in workflow_summary[:3]) or "暂无"
    summary = (
        f"本时间段有 {useful} 条可学习活动，主要集中在 {top_workflows}；"
        f"{low_signal} 条信号较低，{protected} 条被隐私保护，{duplicate} 条为重复，{gap} 条为缺口。"
    )
    return {
        "summary": summary,
        "classifier": classifier_status,
        "learnability_counts": {
            "useful": useful,
            "low_signal": low_signal,
            "protected": protected,
            "duplicate": duplicate,
            "gap": gap,
        },
        "workflow_summary": workflow_summary,
        "app_summary": app_summary,
        "time_blocks": time_blocks,
        "low_signal_reasons": low_signal_reasons,
        "screen_region_summary": screen_region_summary,
        "memory_candidates": memory_candidates,
        "memory_candidate_quality": memory_candidate_quality,
        "annotations": annotations,
    }


def _cleaning_summary(activities: list[dict], privacy_events: list[dict], gap_events: list[dict], duplicate_events: list[dict]) -> dict:
    partial = sum(1 for item in activities if str(item.get("privacy_level") or "") == "partial_redacted")
    partial_duplicate = sum(1 for item in activities if str(item.get("dedupe_status") or "") == "partial_duplicate")
    duplicate_total = sum(int(item.get("duplicate_count") or 1) for item in duplicate_events)
    normal = max(0, len(activities) - partial)
    summary = (
        f"本时间段保留 {len(activities)} 条安全活动，其中 {normal} 条直接保留、"
        f"{partial} 条已局部清洗、{partial_duplicate} 条省略了重复内容；"
        f"另有 {duplicate_total} 次完全重复未重复记录、{len(privacy_events)} 条隐私保护事件、"
        f"{len(gap_events)} 条未记录缺口。"
    )
    return {"partial_redacted_count": partial, "normal_clear_count": normal, "summary": summary}


def _quality_diagnostics(
    conn: sqlite3.Connection,
    start: str | None,
    end: str | None,
    activities: list[dict],
    privacy_events: list[dict],
    gap_events: list[dict],
    duplicate_events: list[dict],
) -> dict:
    cleaned_lengths = [len(str(item.get("cleaned_text") or "")) for item in activities]
    short_buckets = _short_content_buckets(activities)
    return {
        "activity_count": len(activities),
        "partial_redacted_count": sum(1 for item in activities if item.get("privacy_level") == "partial_redacted"),
        "communication_redaction_count": sum(1 for item in activities if item.get("privacy_level") == "communication"),
        "privacy_event_count": len(privacy_events),
        "gap_event_count": len(gap_events),
        "duplicate_event_count": len(duplicate_events),
        "duplicate_frame_count": sum(int(item.get("duplicate_count") or 1) for item in duplicate_events),
        "near_cleaned_content_cap_count": sum(1 for length in cleaned_lengths if length >= CLEANED_CONTENT_NEAR_CAP_CHARS),
        "short_or_empty_content_count": sum(1 for length in cleaned_lengths if length <= 80),
        "short_content_buckets": short_buckets,
    }


def _short_content_buckets(activities: list[dict]) -> dict:
    buckets = Counter()
    for item in activities:
        cleaned = str(item.get("cleaned_text") or "")
        length = len(cleaned)
        if length >= CLEANED_CONTENT_NEAR_CAP_CHARS:
            buckets["near_cleaned_content_cap_count"] += 1
        if length > 80:
            continue
        buckets["total_short_or_empty_count"] += 1
        app_names = _json_list(item.get("apps"))
        summary = str(item.get("activity_summary") or "").lower()
        privacy_level = str(item.get("privacy_level") or "")
        dedupe_status = str(item.get("dedupe_status") or "")
        if privacy_level in {"partial_redacted", "communication"}:
            buckets["privacy_redaction_count"] += 1
        elif dedupe_status in {"partial_duplicate", "duplicate_exact"}:
            buckets["duplicate_compression_count"] += 1
        elif any(
            app_identity_in(app_name, "", BROWSER_FAMILIES)
            or normalize_app_name(app_name) in SYSTEM_UI_APPS
            for app_name in app_names
        ):
            buckets["browser_or_system_chrome_count"] += 1
        elif not cleaned.strip() or not summary.strip():
            buckets["ocr_or_ax_weak_signal_count"] += 1
        else:
            buckets["true_low_signal_count"] += 1
    for key in (
        "total_short_or_empty_count",
        "privacy_redaction_count",
        "duplicate_compression_count",
        "ocr_or_ax_weak_signal_count",
        "browser_or_system_chrome_count",
        "true_low_signal_count",
        "near_cleaned_content_cap_count",
    ):
        buckets.setdefault(key, 0)
    return dict(buckets)




def _learning_annotation(item: dict, classifier_status: dict) -> dict:
    kind = str(item.get("kind") or "")
    time_label = _time_label(item)
    if kind == "unverified_clue":
        app = _sanitize_text(item.get("app_name") or "")
        workflow = _classify_workflow(app, "") if app else "low_signal"
        return _non_activity_learning_item(
            item,
            time_label,
            workflow,
            "low_signal",
            "unverified_screen_or_ocr_clue",
            "未验证屏幕/OCR 线索；不保留正文，也不能证明任务状态。",
        )
    if kind == "privacy_event":
        return _non_activity_learning_item(item, time_label, "protected", "protected", "privacy_protected", "隐私保护事件，不保留具体内容。")
    if kind == "gap_event":
        return _non_activity_learning_item(item, time_label, "gap", "gap", "not_retained_gap", "证据不足或需要复核，只记录缺口。")
    if kind == "duplicate_event":
        count = int(item.get("duplicate_count") or 1)
        return _non_activity_learning_item(item, time_label, "duplicate", "duplicate", "content_duplicate", f"与上一帧重复，合并 {count} 次，不重复保留正文。")

    apps = _json_list(item.get("apps"))
    app = apps[0] if apps else ""
    cleaned = _sanitize_text(item.get("cleaned_text") or "")
    summary = _sanitize_text(item.get("activity_summary") or "")
    screen_region_signal = _screen_region_signal(item)
    doc_contexts = _document_contexts(item)
    doc_context_count = len(doc_contexts)
    doc_topic_tags = _document_topic_tags(doc_contexts)
    doc_source_categories = _document_field_values(doc_contexts, "document_source_category")
    doc_source_scopes = _document_field_values(doc_contexts, "document_source_scope")
    doc_subject_scopes = _document_field_values(doc_contexts, "document_subject_scope")
    doc_memory_policies = _document_field_values(doc_contexts, "document_memory_policy")
    doc_risk_flags = _document_list_values(doc_contexts, "document_risk_flags")
    doc_classification_reasons = _document_list_values(doc_contexts, "document_classification_reasons")
    doc_mtime_events = _document_field_values(doc_contexts, "subsequent_mtime_event")
    doc_safe_summary = _document_change_summary(doc_contexts)
    text = " ".join(part for part in [app, summary, cleaned] if part)
    is_communication = item.get("privacy_level") == "communication" or _is_communication_app(app)
    if is_communication:
        return {
            "event_id": item.get("id") or "",
            "kind": "activity",
            "time_label": time_label,
            "app": _sanitize_text(app),
            "workflow": "communication",
            "topic": "沟通活动",
            "learnability": "low_signal",
            "reason": "communication_redacted",
            "safe_summary": "沟通/协作活动，已保留活动类型，不保留对话原文或第三方可识别细节。",
            "document_context_count": doc_context_count,
            "document_topic_tags": doc_topic_tags,
            "document_source_categories": doc_source_categories,
            "document_source_scopes": doc_source_scopes,
            "document_subject_scopes": doc_subject_scopes,
            "document_memory_policies": doc_memory_policies,
            "document_risk_flags": doc_risk_flags,
            "document_classification_reasons": doc_classification_reasons,
            "document_mtime_events": doc_mtime_events,
            "screen_region_signal": screen_region_signal,
            **_confirmation_annotation_fields(item),
        }
    if len(cleaned) <= 20:
        workflow = _classify_workflow(app, text)
        return {
            "event_id": item.get("id") or "",
            "kind": "activity",
            "time_label": time_label,
            "app": _sanitize_text(app),
            "workflow": workflow,
            "topic": _topic_from_text(text, workflow),
            "learnability": "low_signal",
            "reason": "short_or_empty_cleaned_content",
            "safe_summary": _clip_text(summary or "清洗后内容过短，难以形成稳定可学习结论。", LEARNING_SNIPPET_CHARS),
            "document_context_count": doc_context_count,
            "document_topic_tags": doc_topic_tags,
            "document_source_categories": doc_source_categories,
            "document_source_scopes": doc_source_scopes,
            "document_subject_scopes": doc_subject_scopes,
            "document_memory_policies": doc_memory_policies,
            "document_risk_flags": doc_risk_flags,
            "document_classification_reasons": doc_classification_reasons,
            "document_mtime_events": doc_mtime_events,
            "screen_region_signal": screen_region_signal,
            **_confirmation_annotation_fields(item),
        }

    workflow = _classify_workflow(app, text)
    model_result = _classify_with_local_text_model(text, classifier_status)
    if model_result:
        workflow = model_result.get("workflow") or workflow
    return {
        "event_id": item.get("id") or "",
        "kind": "activity",
        "time_label": time_label,
        "app": _sanitize_text(app),
        "workflow": workflow,
        "topic": _topic_from_text(text, workflow),
        "learnability": "useful",
        "reason": _activity_learning_reason(item),
        "safe_summary": _clip_text(doc_safe_summary or cleaned or summary, LEARNING_SNIPPET_CHARS),
        "document_context_count": doc_context_count,
        "document_topic_tags": doc_topic_tags,
        "document_source_categories": doc_source_categories,
        "document_source_scopes": doc_source_scopes,
        "document_subject_scopes": doc_subject_scopes,
        "document_memory_policies": doc_memory_policies,
        "document_risk_flags": doc_risk_flags,
        "document_classification_reasons": doc_classification_reasons,
        "document_mtime_events": doc_mtime_events,
        "screen_region_signal": screen_region_signal,
        **_confirmation_annotation_fields(item),
    }


def _confirmation_annotation_fields(item: dict) -> dict:
    occurrence_state = str(item.get("occurrence_state") or "")
    if occurrence_state not in {
        "evidence_confirmed",
        "user_confirmed",
        "user_confirmed_preparation",
    }:
        occurrence_state = ""
    confirmation_source = str(item.get("confirmation_source") or "")
    if confirmation_source not in {
        "screen_observation",
        "deterministic_evidence",
        "user",
    }:
        confirmation_source = ""
    try:
        source_count = max(1, min(512, int(item.get("confirmation_source_count") or 1)))
    except (TypeError, ValueError):
        source_count = 1
    return {
        "occurrence_state": occurrence_state,
        "confirmation_source": confirmation_source,
        "confirmation_source_count": source_count,
        "confirmation_subject_scope_hint": (
            "self_or_user_owned"
            if str(item.get("confirmation_subject_scope_hint") or "")
            == "self_or_user_owned"
            else ""
        ),
        "confirmation_candidate_type_hint": (
            str(item.get("confirmation_candidate_type_hint") or "")
            if str(item.get("confirmation_candidate_type_hint") or "")
            in ELIGIBLE_CANDIDATE_TYPES
            else ""
        ),
    }


def _non_activity_learning_item(item: dict, time_label: str, workflow: str, learnability: str, reason: str, summary: str) -> dict:
    return {
        "event_id": item.get("id") or "",
        "kind": item.get("kind") or "",
        "time_label": time_label,
        "app": _sanitize_text(item.get("app_name") or item.get("app") or ""),
        "workflow": workflow,
        "topic": LEARNING_CATEGORIES.get(workflow, "未分类"),
        "learnability": learnability,
        "reason": reason,
        "safe_summary": _sanitize_text(summary),
        "document_context_count": 0,
        "document_topic_tags": [],
        "document_source_categories": [],
        "document_source_scopes": [],
        "document_subject_scopes": [],
        "document_memory_policies": [],
        "document_risk_flags": [],
        "document_classification_reasons": [],
        "document_mtime_events": [],
        "screen_region_signal": _screen_region_signal(item),
        "occurrence_state": str(item.get("occurrence_state") or "unverified_clue"),
        "confirmation_source": str(item.get("confirmation_source") or "screen_observation"),
    }


def _activity_learning_reason(item: dict) -> str:
    if item.get("privacy_level") == "partial_redacted":
        return "partial_redacted_safe_content"
    if item.get("dedupe_status") == "partial_duplicate":
        return "partial_duplicate_changed_lines"
    return "safe_cleaned_content"


def _document_contexts(item: dict) -> list[dict]:
    contexts = item.get("document_contexts")
    if not isinstance(contexts, list):
        return []
    return [context for context in contexts if isinstance(context, dict)]


def _screen_region_signal(item: dict) -> dict:
    signal = sanitize_screen_region_signal(item.get("screen_region_signal"))
    return signal or _unknown_screen_region_signal()


def _unknown_screen_region_signal() -> dict:
    return {
        "region_kind": REGION_UNKNOWN,
        "actionability": ACTIONABILITY_UNKNOWN,
        "source": "missing",
        "confidence": 0.0,
        "reason_codes": ["missing_region_signal"],
    }


def _document_topic_tags(contexts: list[dict]) -> list[str]:
    output: list[str] = []
    for context in contexts:
        tags = context.get("topic_tags")
        if not isinstance(tags, list):
            continue
        for tag in tags:
            text = _sanitize_text(str(tag or "")).lower()
            if text and text not in output:
                output.append(text)
    return output[:5]


def _document_field_values(contexts: list[dict], key: str) -> list[str]:
    output: list[str] = []
    for context in contexts:
        value = _sanitize_text(str(context.get(key) or "")).lower()
        if value and value not in output:
            output.append(value)
    return output[:8]


def _document_list_values(contexts: list[dict], key: str) -> list[str]:
    output: list[str] = []
    for context in contexts:
        values = context.get(key)
        if not isinstance(values, list):
            values = [values] if values else []
        for value in values:
            text = _sanitize_text(str(value or "")).lower()
            if text and text not in output:
                output.append(text)
    return output[:12]


def _document_change_summary(contexts: list[dict]) -> str:
    summaries: list[str] = []
    for context in contexts:
        category = _sanitize_text(str(context.get("document_source_category") or "")).lower()
        policy = _sanitize_text(str(context.get("document_memory_policy") or "")).lower()
        summary = _sanitize_text(str(context.get("change_summary") or ""))
        if not summary:
            continue
        if category in {
            SOURCE_CATEGORY_SELF_RESUME,
            SOURCE_CATEGORY_TARGET_ROLE_JD,
            SOURCE_CATEGORY_THIRD_PARTY_RESUME,
            SOURCE_CATEGORY_AMBIGUOUS_DOCUMENT,
            SOURCE_CATEGORY_INTERVIEW_NOTES,
        } or policy in {"self_resume_safe_summary", *DOCUMENT_REFERENCE_ONLY_POLICIES}:
            summaries.append(summary)
    return _clip_text("；".join(summaries[:2]), LEARNING_SNIPPET_CHARS) if summaries else ""


def _classify_workflow(app: str, text: str) -> str:
    app_l = str(app or "").lower()
    text_l = str(text or "").lower()
    combined = f"{app_l} {text_l}"
    if _is_communication_app(app):
        return "communication"
    if _contains_any(combined, ["codex", "cursor", "visual studio code", "vscode", "terminal", "warp", "python", "swift", "unittest", "pytest", "traceback", "git ", "github", "api", "function", "class ", "import ", "代码", "调试", "测试", "报错", "修复", "构建"]):
        return "coding_debugging"
    if _contains_any(combined, ["typora", "obsidian", "markdown", "draft", "article", "copy", "公众号", "文章", "草稿", "写作", "文案", "排版", "报告", "标题"]):
        return "writing"
    if _contains_any(combined, ["dia", "chrome", "safari", "research", "search", "docs", "documentation", "论文", "调研", "研究", "搜索", "资料", "竞品", "官网"]):
        return "research"
    if _contains_any(combined, ["finder", "qspace", "folder", "directory", "file", "download", "desktop", "文件", "目录", "归档", "下载"]):
        return "file_management"
    if _contains_any(combined, ["personalaimonitor", "activity monitor", "system settings", "launchagent", "launchctl", "plist", "设置", "权限", "记录服务", "菜单栏"]):
        return "system_app_control"
    if _contains_any(combined, ["photos", "preview", "image", "video", "media", "photo", "图片", "照片", "视频", "封面"]):
        return "media"
    return "unknown"


def _topic_from_text(text: str, workflow: str) -> str:
    text_l = str(text or "").lower()
    topic_rules = [
        ("Personal AI Monitor", ["personal ai monitor", "activity_mvp", "清洗后", "隐私过滤", "ha source package", "监控系统"]),
        ("Markdown 导出", ["markdown", "导出", "cleaned-timeline"]),
        ("测试与验证", ["unittest", "test", "测试", "验证"]),
        ("代码修复", ["fix", "bug", "traceback", "error", "修复", "报错"]),
        ("写作编辑", ["草稿", "文章", "文案", "标题", "排版"]),
        ("资料调研", ["research", "调研", "资料", "搜索", "竞品"]),
        ("文件整理", ["finder", "文件", "目录", "归档"]),
    ]
    for label, hints in topic_rules:
        if any(hint in text_l for hint in hints):
            return label
    return LEARNING_CATEGORIES.get(workflow, "未分类")


def _learning_classifier_status() -> dict:
    model = os.environ.get("PAM_LEARNING_TEXT_MODEL", "").strip()
    endpoint = os.environ.get("PAM_LEARNING_TEXT_ENDPOINT", "http://127.0.0.1:11434/api/generate")
    timeout = _learning_model_timeout()
    if not model:
        return {
            "primary": "deterministic_rules",
            "local_text_model": {"enabled": False, "status": "not_configured", "model": ""},
        }
    if "vl" in model.lower():
        return {
            "primary": "deterministic_rules",
            "local_text_model": {"enabled": False, "status": "visual_model_not_used_for_text_classification", "model": model},
        }
    endpoint_validation = validate_local_http_endpoint(endpoint)
    if not endpoint_validation["allowed"]:
        return {
            "primary": "deterministic_rules",
            "local_text_model": {
                "enabled": False,
                "status": "endpoint_rejected",
                "model": model,
                "endpoint_allowed": False,
                "endpoint_error": endpoint_validation["message"],
                "endpoint_reason": endpoint_validation["reason"],
            },
        }
    return {
        "primary": "deterministic_rules",
        "local_text_model": {
            "enabled": True,
            "status": "configured",
            "model": model,
            "endpoint": endpoint,
            "endpoint_allowed": True,
            "timeout_seconds": timeout,
        },
    }


def _classify_with_local_text_model(text: str, classifier_status: dict) -> dict | None:
    settings = classifier_status.get("local_text_model") if isinstance(classifier_status, dict) else {}
    if not isinstance(settings, dict) or settings.get("enabled") is not True:
        return None
    model = str(settings.get("model") or "")
    endpoint = str(settings.get("endpoint") or "http://127.0.0.1:11434/api/generate")
    endpoint_validation = validate_local_http_endpoint(endpoint)
    if not endpoint_validation["allowed"]:
        return None
    payload = {
        "model": model,
        "prompt": (
            "Classify this already privacy-cleaned activity into one workflow. "
            "Return strict JSON only: {\"workflow\":\"coding_debugging|writing|research|file_management|communication|system_app_control|media|unknown\"}. "
            f"Activity: {_clip_text(_sanitize_text(text), 1200)}"
        ),
        "stream": False,
        "format": "json",
        "options": {"temperature": 0, "num_predict": 80},
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_learning_model_timeout()) as response:
            raw = json.loads(response.read().decode("utf-8", errors="replace"))
    except (OSError, TimeoutError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError):
        return None
    parsed = _parse_model_response(raw)
    workflow = str(parsed.get("workflow") or "")
    if workflow in LEARNING_CATEGORIES:
        return {"workflow": workflow}
    return None


def _parse_model_response(raw: object) -> dict:
    candidates: list[object] = []
    if isinstance(raw, dict):
        candidates.extend([raw, raw.get("response")])
        message = raw.get("message")
        if isinstance(message, dict):
            candidates.append(message.get("content"))
    else:
        candidates.append(raw)
    for candidate in candidates:
        if isinstance(candidate, dict) and "workflow" in candidate:
            return candidate
        if isinstance(candidate, str):
            text = candidate.strip()
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                try:
                    parsed = json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    return parsed
    return {}


def _learning_model_timeout() -> int:
    raw = os.environ.get("PAM_LEARNING_TEXT_TIMEOUT_SECONDS", "3")
    try:
        return max(1, int(raw))
    except ValueError:
        return 3


def _group_learning_annotations(annotations: list[dict], key: str, *, include_label: bool) -> list[dict]:
    grouped: dict[str, dict] = {}
    for item in annotations:
        value = str(item.get(key) or "unknown")
        bucket = grouped.setdefault(
            value,
            {
                key: value,
                "label": LEARNING_CATEGORIES.get(value, value) if include_label else value,
                "event_count": 0,
                "useful_count": 0,
                "low_signal_count": 0,
                "protected_count": 0,
                "duplicate_count": 0,
                "gap_count": 0,
                "topics": Counter(),
                "apps": Counter(),
            },
        )
        bucket["event_count"] += 1
        learnability = str(item.get("learnability") or "unknown")
        counter_key = f"{learnability}_count"
        if counter_key in bucket:
            bucket[counter_key] += 1
        if item.get("topic"):
            bucket["topics"][str(item["topic"])] += 1
        if item.get("app"):
            bucket["apps"][str(item["app"])] += 1
    output = []
    for bucket in grouped.values():
        output.append(
            {
                key: bucket[key],
                "label": bucket["label"],
                "event_count": bucket["event_count"],
                "useful_count": bucket["useful_count"],
                "low_signal_count": bucket["low_signal_count"],
                "protected_count": bucket["protected_count"],
                "duplicate_count": bucket["duplicate_count"],
                "gap_count": bucket["gap_count"],
                "top_topics": [name for name, _ in bucket["topics"].most_common(5)],
                "top_apps": [name for name, _ in bucket["apps"].most_common(5)],
            }
        )
    return sorted(output, key=lambda item: (-int(item["event_count"]), str(item["label"])))[:12]


def _time_block_summary(annotations: list[dict]) -> list[dict]:
    grouped: dict[str, Counter] = defaultdict(Counter)
    for item in annotations:
        label = _hour_label(item.get("time_label") or "")
        grouped[label]["event_count"] += 1
        grouped[label][str(item.get("learnability") or "unknown")] += 1
        grouped[label][str(item.get("workflow") or "unknown")] += 1
    output = []
    for label, counts in sorted(grouped.items()):
        workflows = [
            name
            for name, _ in counts.most_common()
            if name not in {"event_count", "useful", "low_signal", "protected", "duplicate", "gap", "unknown"}
        ][:3]
        output.append(
            {
                "time_block": label,
                "event_count": counts["event_count"],
                "useful_count": counts["useful"],
                "low_signal_count": counts["low_signal"],
                "protected_count": counts["protected"],
                "duplicate_count": counts["duplicate"],
                "gap_count": counts["gap"],
                "top_workflows": workflows,
            }
        )
    return output[:24]


def _screen_region_summary(annotations: list[dict]) -> dict:
    signals = []
    for item in annotations:
        if item.get("kind") != "activity":
            continue
        signal = sanitize_screen_region_signal(item.get("screen_region_signal"))
        if signal:
            signals.append(signal)
    return region_distribution(signals)


def _low_signal_reason_summary(annotations: list[dict]) -> list[dict]:
    counts = Counter(
        str(item.get("reason") or "unknown")
        for item in annotations
        if item.get("learnability") in {"low_signal", "protected", "duplicate", "gap"}
    )
    return [{"reason": reason, "count": count} for reason, count in counts.most_common(12)]


def _append_learning_group_section(lines: list[str], title: str, rows: list[dict], label_key: str) -> None:
    lines.extend([f"## {title}", ""])
    if not rows:
        lines.extend(["暂无。", ""])
        return
    for row in rows:
        label = row.get("label") or row.get(label_key) or "unknown"
        topics = "、".join(row.get("top_topics") or []) or "无"
        apps = "、".join(row.get("top_apps") or []) or "无"
        lines.append(
            f"- {label}：{row.get('event_count', 0)} 条，useful {row.get('useful_count', 0)}，"
            f"low_signal {row.get('low_signal_count', 0)}，protected {row.get('protected_count', 0)}，"
            f"duplicate {row.get('duplicate_count', 0)}，gap {row.get('gap_count', 0)}；主题：{topics}；App：{apps}"
        )
    lines.append("")


def _append_memory_candidates(lines: list[str], candidates: list[dict]) -> None:
    lines.extend(["## 可记忆候选", ""])
    if not candidates:
        lines.extend(["暂无足够安全、稳定的可记忆候选。", ""])
        return
    for item in candidates:
        tags = "、".join(item.get("project_tags") or []) or "无"
        subject = item.get("subject_scope") or SUBJECT_USER_WORKFLOW_ONLY
        lines.append(
            f"- {item.get('time_label')} · {item.get('topic')} · {item.get('app') or '无'}"
            f" · {item.get('candidate_type') or 'activity_fact'} · confidence {item.get('confidence', 0)}"
            f" · {item.get('write_eligibility') or 'review_only'} · subject {subject} · tags {tags}：{item.get('safe_summary')}"
        )
    lines.append("")


def _memory_candidate_type(item: dict) -> str:
    confirmed_hint = str(item.get("confirmation_candidate_type_hint") or "")
    if (
        str(item.get("occurrence_state") or "")
        in {"user_confirmed", "user_confirmed_preparation"}
        and confirmed_hint in ELIGIBLE_CANDIDATE_TYPES
    ):
        return confirmed_hint
    reason = str(item.get("reason") or "")
    topic_text = f"{item.get('topic') or ''} {item.get('safe_summary') or ''}".lower()
    if reason == "communication_redacted":
        return "workflow_pattern"
    if _contains_any(topic_text, ["决定", "决策", "decision", "decided", "will only", "不再", "默认", "以后", "规则", "要求"]):
        return "decision"
    if _contains_any(topic_text, ["偏好", "喜欢", "不喜欢", "prefer", "preference"]):
        return "preference"
    if _contains_any(topic_text, ["阻塞", "失败", "报错", "卡住", "missing", "error"]):
        return "blocker"
    if _contains_any(topic_text, ["简历", "resume", "cv", "面试", "岗位", "markdown", "文档"]):
        return "document_work"
    if _contains_any(topic_text, ["todo", "待办", "跟进", "下一步"]):
        return "follow_up"
    workflow = str(item.get("workflow") or "")
    if workflow == "coding_debugging":
        return "project_fact"
    if workflow in {"writing", "research"}:
        return "work_context"
    if workflow == "system_app_control":
        return "tooling_state"
    return "activity_fact"


def _memory_candidate_confidence(candidate: dict) -> float:
    summary = str(candidate.get("safe_summary") or "")
    base = 0.58
    if candidate.get("learnability") == "useful":
        base += 0.1
    if candidate.get("learnability") == "low_signal":
        base -= 0.08
    if len(summary) >= 80:
        base += 0.12
    if str(candidate.get("workflow") or "unknown") != "unknown":
        base += 0.08
    if str(candidate.get("topic") or "") not in {"", "未分类"}:
        base += 0.06
    evidence_count = int(candidate.get("evidence_count") or 1)
    if evidence_count > 1:
        base += min(0.14, evidence_count * 0.025)
    candidate_type = str(candidate.get("candidate_type") or "")
    if candidate_type in ELIGIBLE_CANDIDATE_TYPES:
        base += 0.08
    flags = set(candidate.get("quality_flags") or [])
    if "unknown_app" in flags:
        base -= 0.12
    if "single_evidence" in flags:
        base -= 0.1
    if "weak_workflow" in flags or "browser_or_system_chrome" in flags:
        base -= 0.12
    if flags.intersection({"media_player_context", "file_listing_context", "task_list_context", "browser_tab_chrome", "system_ui_context", "read_only_context_app"}):
        base -= 0.16
    if "third_party_subject" in flags:
        base -= 0.18
    elif "ambiguous_subject" in flags:
        base -= 0.12
    if "keyword_only_project_tag" in flags or "weak_document_anchor" in flags:
        base -= 0.08
    if flags.intersection({"reference_only_document", "target_role_jd_document", "blocked_subject_document"}):
        base -= 0.14
    if flags.intersection({"personal_medical_document", "personal_medical_content"}):
        base -= 0.24
    if "trusted_app_context" in flags or "document_anchor" in flags:
        base += 0.06
    eligibility = str(candidate.get("write_eligibility") or WRITE_ELIGIBILITY_REVIEW_ONLY)
    if eligibility == WRITE_ELIGIBILITY_ELIGIBLE:
        base += 0.04
    elif eligibility == WRITE_ELIGIBILITY_REVIEW_ONLY:
        base -= 0.08
    else:
        base -= 0.2
    cap = 0.94
    if "single_evidence" in flags:
        if candidate_type in {"decision", "preference", "blocker", "follow_up", "document_work"} and "unknown_app" not in flags:
            cap = 0.82
        else:
            cap = 0.76 if "unknown_app" not in flags else 0.64
    if eligibility == WRITE_ELIGIBILITY_REVIEW_ONLY:
        cap = min(cap, 0.74)
    if eligibility == WRITE_ELIGIBILITY_EPHEMERAL:
        cap = min(cap, 0.52)
    if flags.intersection({"media_player_context", "file_listing_context", "task_list_context", "browser_tab_chrome", "system_ui_context", "read_only_context_app"}):
        cap = min(cap, 0.68)
    if flags.intersection({"third_party_subject", "ambiguous_subject"}):
        cap = min(cap, 0.66)
    if flags.intersection({"reference_only_document", "target_role_jd_document", "blocked_subject_document"}):
        cap = min(cap, 0.68)
    if flags.intersection({"personal_medical_document", "personal_medical_content"}):
        cap = min(cap, 0.42)
    return round(max(0.2, min(cap, base)), 2)


def _memory_candidate_reason(candidate: dict) -> str:
    topic = str(candidate.get("topic") or "未分类")
    workflow = LEARNING_CATEGORIES.get(str(candidate.get("workflow") or "unknown"), str(candidate.get("workflow") or "unknown"))
    eligibility = str(candidate.get("write_eligibility") or WRITE_ELIGIBILITY_REVIEW_ONLY)
    evidence = int(candidate.get("evidence_count") or 1)
    candidate_type = str(candidate.get("candidate_type") or "activity_fact")
    anchor = str(candidate.get("anchor_type") or ANCHOR_WEAK)
    risks = set(candidate.get("write_risk_flags") or [])
    subject_scope = str(candidate.get("subject_scope") or SUBJECT_USER_WORKFLOW_ONLY)
    source_category = str(candidate.get("source_category") or "")
    memory_policy = str(candidate.get("memory_write_policy") or "")
    anchor_label = {
        ANCHOR_DOCUMENT_SIGNAL: "真实文档语义锚点",
        ANCHOR_TRUSTED_APP: "可信工作 App 上下文",
        ANCHOR_KEYWORD_ONLY: "仅关键词项目标签",
        ANCHOR_WEAK: "缺少可靠锚点",
    }.get(anchor, "缺少可靠锚点")
    if subject_scope == SUBJECT_THIRD_PARTY:
        return f"仅供 Hermes 观察趋势：{workflow} / {topic}，主体像第三方资料，禁止写成 Local Owner 的长期记忆。"
    if subject_scope == SUBJECT_AMBIGUOUS:
        return f"仅供 Hermes 观察趋势：{workflow} / {topic}，主体归属不明确，暂不写长期记忆。"
    if source_category == SOURCE_CATEGORY_PERSONAL_MEDICAL_DOCUMENT or risks.intersection({"personal_medical_document", "personal_medical_content"}):
        return f"仅做质量诊断：{workflow} / {topic}，个人临床医疗内容被保护，禁止交给 Hermes 写长期记忆。"
    if source_category == SOURCE_CATEGORY_TARGET_ROLE_JD or "reference_only_document" in risks:
        return f"仅供 Hermes 观察趋势：{workflow} / {topic}，这是岗位/JD/公司要求类参考材料，只能写成岗位适配准备，不能写成 Local Owner 的长期能力或定位。"
    if memory_policy == "blocked_subject":
        return f"仅供 Hermes 观察趋势：{workflow} / {topic}，文档主体被判定为不适合写入长期记忆。"
    if eligibility == WRITE_ELIGIBILITY_ELIGIBLE:
        return f"可写入长期记忆：{workflow} / {topic}，类型 {candidate_type}，证据 {evidence} 条，依据是{anchor_label}。"
    if eligibility == WRITE_ELIGIBILITY_EPHEMERAL:
        if risks.intersection({"media_player_context", "file_listing_context", "task_list_context", "browser_tab_chrome", "system_ui_context", "read_only_context_app"}):
            return f"仅做质量诊断：{workflow} / {topic}，更像播放器、文件/文件夹列表、任务列表、浏览器标签、只读预览或系统 UI 噪声，不交给 Hermes 写长期记忆。"
        return f"仅做质量诊断：{workflow} / {topic}，{anchor_label}且信号偏临时，不交给 Hermes 写长期记忆。"
    if "single_evidence_demoted" in risks:
        return f"仅供 Hermes 观察趋势：{workflow} / {topic}，只有单条证据，缺少真实文档锚点或可信工作上下文，暂不写长期记忆。"
    if "keyword_only_project_tag" in risks or "weak_document_anchor" in risks:
        return f"仅供 Hermes 观察趋势：{workflow} / {topic}，只靠关键词命中项目标签，缺少真实文档锚点，暂不写长期记忆。"
    if "document_not_modified_near_capture" in risks:
        return f"仅供 Hermes 观察趋势：{workflow} / {topic}，文档只是被打开或展示，未看到当前时间窗内的修改证据，暂不写长期记忆。"
    return f"仅供 Hermes 观察趋势：{workflow} / {topic}，证据 {evidence} 条，未达到直接写入长期记忆门槛。"


def _memory_candidates(annotations: list[dict]) -> list[dict]:
    return _memory_candidate_result(annotations)["memory_candidates"]


def _memory_candidate_result(annotations: list[dict]) -> dict:
    limit = _memory_candidate_limit()
    raw_candidates: list[dict] = []
    for item in annotations:
        if item.get("learnability") == "useful" or _aggressive_candidate_keep(item):
            candidate = _base_memory_candidate(item)
            if candidate:
                raw_candidates.append(candidate)
    merged = _merge_memory_candidates(raw_candidates)
    finalized = [_finalize_memory_candidate(candidate) for candidate in merged]
    exposed = [
        candidate
        for candidate in finalized
        if candidate.get("write_eligibility") != WRITE_ELIGIBILITY_EPHEMERAL
    ]
    exposed.sort(key=_memory_candidate_sort_key)
    limited = [_public_memory_candidate(candidate) for candidate in exposed[:limit]]
    return {
        "memory_candidates": limited,
        "memory_candidate_quality": _memory_candidate_quality(finalized, limited),
    }


def _base_memory_candidate(item: dict) -> dict | None:
    safe_summary = _sanitize_text(item.get("safe_summary") or "")
    if not safe_summary:
        return None
    started_at, ended_at = _candidate_time_bounds(item)
    project_tags = _project_tags(item)
    anchor_type = _candidate_anchor_type(item, project_tags)
    try:
        confirmation_source_count = max(
            1,
            min(512, int(item.get("confirmation_source_count") or 1)),
        )
    except (TypeError, ValueError):
        confirmation_source_count = 1
    occurrence_state = str(item.get("occurrence_state") or "")
    candidate = {
        "time_label": item["time_label"],
        "workflow": item["workflow"],
        "topic": item["topic"],
        "app": item["app"],
        "safe_summary": _clip_text(safe_summary, LEARNING_SNIPPET_CHARS),
        "candidate_type": _memory_candidate_type(item),
        "learnability": item.get("learnability") or "",
        "reason": item.get("reason") or "",
        # A canonical confirmation fact intentionally collapses an episode to
        # one outbound row. Preserve its content-free source count so the
        # learning gate does not mistake a multi-frame, user-confirmed episode
        # for an uncorroborated single observation.
        "evidence_count": confirmation_source_count,
        "project_tags": project_tags,
        "anchor_type": anchor_type,
        "anchor_strength": _anchor_strength(anchor_type),
        "source_category": _primary_document_value(item, "document_source_categories", SOURCE_CATEGORY_GENERAL_DOCUMENT),
        "source_scope": _primary_document_value(item, "document_source_scopes", SOURCE_SCOPE_CONTEXT_ONLY),
        "memory_write_policy": _primary_document_value(item, "document_memory_policies", "workflow_only"),
        "write_risk_flags": [],
        "merged_duplicate_count": 0,
        "evidence_span_minutes": 0,
        "_started_at": started_at,
        "_ended_at": ended_at,
        "_first_at": started_at,
        "_last_at": ended_at or started_at,
        "_fingerprint": _memory_summary_fingerprint(safe_summary),
        "_document_context_count": int(item.get("document_context_count") or 0),
        "_document_source_categories": _string_list(item.get("document_source_categories") or []),
        "_document_source_scopes": _string_list(item.get("document_source_scopes") or []),
        "_document_subject_scopes": _string_list(item.get("document_subject_scopes") or []),
        "_document_memory_policies": _string_list(item.get("document_memory_policies") or []),
        "_document_risk_flags": _string_list(item.get("document_risk_flags") or []),
        "_document_classification_reasons": _string_list(item.get("document_classification_reasons") or []),
        "_document_mtime_events": _string_list(item.get("document_mtime_events") or []),
        "_screen_region_signals": [_screen_region_signal(item)],
        "_activity_ids": _string_list([item.get("event_id") or ""]),
        "_user_confirmed": occurrence_state in {
            "user_confirmed",
            "user_confirmed_preparation",
        },
        "_manual_confirmation": (
            occurrence_state in {"user_confirmed", "user_confirmed_preparation"}
            and str(item.get("confirmation_source") or "") == "user"
            and _normalize_app(str(item.get("app") or "")) == "manual confirmation"
        ),
        "_confirmation_subject_scope_hint": str(
            item.get("confirmation_subject_scope_hint") or ""
        ),
    }
    return candidate


def _merge_memory_candidates(candidates: list[dict]) -> list[dict]:
    merged: list[dict] = []
    for candidate in candidates:
        target = _merge_target(merged, candidate)
        if target is None:
            merged.append(candidate)
            continue
        target["evidence_count"] = int(target.get("evidence_count") or 1) + int(candidate.get("evidence_count") or 1)
        target["_user_confirmed"] = bool(
            target.get("_user_confirmed") or candidate.get("_user_confirmed")
        )
        if not target.get("_confirmation_subject_scope_hint"):
            target["_confirmation_subject_scope_hint"] = candidate.get(
                "_confirmation_subject_scope_hint"
            ) or ""
        target["merged_duplicate_count"] = int(target.get("merged_duplicate_count") or 0) + 1
        target["_last_at"] = _max_candidate_time(target.get("_last_at"), candidate.get("_last_at"))
        target["evidence_span_minutes"] = _candidate_span_minutes(target.get("_first_at"), target.get("_last_at"))
        target["project_tags"] = _merge_tags(target.get("project_tags") or [], candidate.get("project_tags") or [])
        _merge_candidate_document_metadata(target, candidate)
        _merge_candidate_region_metadata(target, candidate)
        target["_activity_ids"] = _merge_unique_values(
            target.get("_activity_ids") or [],
            candidate.get("_activity_ids") or [],
        )
        if int(candidate.get("anchor_strength") or 0) > int(target.get("anchor_strength") or 0):
            target["anchor_type"] = candidate.get("anchor_type") or ANCHOR_WEAK
            target["anchor_strength"] = int(candidate.get("anchor_strength") or 0)
        target["_document_context_count"] = int(target.get("_document_context_count") or 0) + int(candidate.get("_document_context_count") or 0)
        if len(str(candidate.get("safe_summary") or "")) > len(str(target.get("safe_summary") or "")):
            target["safe_summary"] = candidate["safe_summary"]
        target["time_label"] = _merged_time_label(str(target.get("time_label") or ""), str(candidate.get("time_label") or ""))
    return merged


def _merge_target(merged: list[dict], candidate: dict) -> dict | None:
    for current in reversed(merged[-64:]):
        if _document_candidate_cluster_match(current, candidate):
            gap = _candidate_gap_seconds(current.get("_last_at"), candidate.get("_first_at"))
            if gap <= MEMORY_DOCUMENT_CANDIDATE_MERGE_SECONDS:
                return current
        if _trusted_candidate_cluster_match(current, candidate):
            gap = _candidate_gap_seconds(current.get("_last_at"), candidate.get("_first_at"))
            if gap <= MEMORY_CANDIDATE_MERGE_SECONDS:
                return current
        if _candidate_merge_key(current) != _candidate_merge_key(candidate):
            continue
        gap = _candidate_gap_seconds(current.get("_last_at"), candidate.get("_first_at"))
        if gap <= MEMORY_CANDIDATE_MERGE_SECONDS:
            return current
    return None


def _document_candidate_cluster_match(left: dict, right: dict) -> bool:
    if str(left.get("anchor_type") or "") != ANCHOR_DOCUMENT_SIGNAL:
        return False
    if str(right.get("anchor_type") or "") != ANCHOR_DOCUMENT_SIGNAL:
        return False
    for key in ("source_category", "source_scope", "memory_write_policy", "candidate_type"):
        if str(left.get(key) or "") != str(right.get(key) or ""):
            return False
    left_tags = set(left.get("project_tags") or [])
    right_tags = set(right.get("project_tags") or [])
    if left_tags and right_tags and not left_tags.intersection(right_tags):
        return False
    return _summary_token_similarity(str(left.get("safe_summary") or ""), str(right.get("safe_summary") or "")) >= 0.5


def _trusted_candidate_cluster_match(left: dict, right: dict) -> bool:
    if str(left.get("anchor_type") or "") != ANCHOR_TRUSTED_APP:
        return False
    if str(right.get("anchor_type") or "") != ANCHOR_TRUSTED_APP:
        return False
    left_type = str(left.get("candidate_type") or "")
    right_type = str(right.get("candidate_type") or "")
    if left_type != right_type and not {left_type, right_type}.issubset(TRUSTED_APP_COMPATIBLE_MERGE_TYPES):
        return False
    left_app = str(left.get("app") or "")
    right_app = str(right.get("app") or "")
    same_app = _candidate_app_key(left_app) == _candidate_app_key(right_app)
    if not same_app and not (_is_primary_action_app(left_app) and _is_primary_action_app(right_app)):
        return False
    similarity = _summary_token_similarity(str(left.get("safe_summary") or ""), str(right.get("safe_summary") or ""))
    left_tags = set(left.get("project_tags") or [])
    right_tags = set(right.get("project_tags") or [])
    shared_tags = left_tags.intersection(right_tags)
    significant_shared_tags = shared_tags.difference(GENERIC_PROJECT_TAGS)
    if same_app and shared_tags:
        if _is_primary_action_app(left_app) and (
            str(left.get("workflow") or "") == str(right.get("workflow") or "")
            or str(left.get("topic") or "") == str(right.get("topic") or "")
        ):
            return similarity >= 0.3
        return similarity >= 0.6
    if significant_shared_tags:
        return similarity >= 0.65
    return False


def _candidate_merge_key(candidate: dict) -> tuple[str, str, str, str, str]:
    tags = ",".join(candidate.get("project_tags") or [])
    app_key = _candidate_app_key(str(candidate.get("app") or ""))
    if str(candidate.get("anchor_type") or "") == ANCHOR_TRUSTED_APP and str(candidate.get("candidate_type") or "") in ELIGIBLE_CANDIDATE_TYPES:
        app_key = "trusted_memory_app"
    return (
        app_key,
        str(candidate.get("workflow") or ""),
        str(candidate.get("topic") or ""),
        tags,
        str(candidate.get("_fingerprint") or ""),
    )


def _finalize_memory_candidate(candidate: dict) -> dict:
    finalized = dict(candidate)
    finalized.update(_candidate_subject_assessment(finalized))
    flags = _memory_quality_flags(finalized)
    finalized["quality_flags"] = flags
    finalized["write_eligibility"] = _write_eligibility(finalized, flags)
    if "single_evidence" in flags and finalized["write_eligibility"] != WRITE_ELIGIBILITY_ELIGIBLE:
        flags = [*flags, "single_evidence_demoted"]
        finalized["quality_flags"] = flags
    finalized["write_risk_flags"] = _write_risk_flags(finalized, flags)
    finalized.update(_candidate_evidence_layer(finalized, flags))
    finalized["confidence"] = _memory_candidate_confidence(finalized)
    finalized["why_keep"] = _memory_candidate_reason(finalized)
    finalized["evidence_span_minutes"] = _candidate_span_minutes(finalized.get("_first_at"), finalized.get("_last_at"))
    return finalized


def _public_memory_candidate(candidate: dict) -> dict:
    screen_region_signal = _primary_screen_region_signal(candidate)
    return {
        "time_label": candidate["time_label"],
        "workflow": candidate["workflow"],
        "topic": candidate["topic"],
        "app": candidate["app"],
        "safe_summary": candidate["safe_summary"],
        "candidate_type": candidate["candidate_type"],
        "confidence": candidate["confidence"],
        "why_keep": candidate["why_keep"],
        "evidence_count": int(candidate.get("evidence_count") or 1),
        "project_tags": candidate.get("project_tags") or [],
        "anchor_type": candidate.get("anchor_type") or ANCHOR_WEAK,
        "anchor_strength": int(candidate.get("anchor_strength") or 0),
        "source_category": candidate.get("source_category") or SOURCE_CATEGORY_GENERAL_DOCUMENT,
        "source_scope": candidate.get("source_scope") or SOURCE_SCOPE_CONTEXT_ONLY,
        "memory_write_policy": candidate.get("memory_write_policy") or "workflow_only",
        "subject_scope": candidate.get("subject_scope") or SUBJECT_AMBIGUOUS,
        "subject_confidence": float(candidate.get("subject_confidence") or 0.0),
        "subject_reasons": candidate.get("subject_reasons") or [],
        "subject_risk_flags": candidate.get("subject_risk_flags") or [],
        "quality_flags": candidate.get("quality_flags") or [],
        "write_risk_flags": candidate.get("write_risk_flags") or [],
        "write_eligibility": candidate.get("write_eligibility") or WRITE_ELIGIBILITY_REVIEW_ONLY,
        "evidence_layer": candidate.get("evidence_layer") or EVIDENCE_LAYER_CONTEXT,
        "confirmation_signals": candidate.get("confirmation_signals") or [],
        "evidence_explanation": candidate.get("evidence_explanation") or "",
        "screen_region_signal": screen_region_signal,
        "screen_region_signals": _public_region_signals(candidate),
        "activity_ids": _string_list(candidate.get("_activity_ids") or [])[:12],
        "time_range": _candidate_provenance_time_range(candidate),
        "evidence_span_minutes": int(candidate.get("evidence_span_minutes") or 0),
        "merged_duplicate_count": int(candidate.get("merged_duplicate_count") or 0),
    }


def _memory_candidate_sort_key(candidate: dict) -> tuple:
    eligibility_rank = {
        WRITE_ELIGIBILITY_ELIGIBLE: 0,
        WRITE_ELIGIBILITY_REVIEW_ONLY: 1,
        WRITE_ELIGIBILITY_EPHEMERAL: 2,
    }.get(str(candidate.get("write_eligibility") or ""), 1)
    type_rank = 0 if str(candidate.get("candidate_type") or "") in ELIGIBLE_CANDIDATE_TYPES else 1
    unknown_rank = 1 if "unknown_app" in set(candidate.get("quality_flags") or []) else 0
    subject_rank = {
        SUBJECT_SELF_OR_USER_OWNED: 0,
        SUBJECT_USER_WORKFLOW_ONLY: 0,
        SUBJECT_AMBIGUOUS: 1,
        SUBJECT_THIRD_PARTY: 2,
    }.get(str(candidate.get("subject_scope") or ""), 1)
    return (
        eligibility_rank,
        type_rank,
        subject_rank,
        -int(candidate.get("evidence_count") or 1),
        unknown_rank,
        -float(candidate.get("confidence") or 0.0),
        str(candidate.get("time_label") or ""),
    )


def _memory_candidate_quality(all_candidates: list[dict], exposed_candidates: list[dict]) -> dict:
    total = len(all_candidates)
    eligible = [item for item in all_candidates if item.get("write_eligibility") == WRITE_ELIGIBILITY_ELIGIBLE]
    review = [item for item in all_candidates if item.get("write_eligibility") == WRITE_ELIGIBILITY_REVIEW_ONLY]
    ephemeral = [item for item in all_candidates if item.get("write_eligibility") == WRITE_ELIGIBILITY_EPHEMERAL]
    single_eligible = [item for item in eligible if int(item.get("evidence_count") or 1) <= 1]
    unknown_eligible = [item for item in eligible if "unknown_app" in set(item.get("quality_flags") or [])]
    merged_count = sum(int(item.get("merged_duplicate_count") or 0) for item in all_candidates)
    high_single = [
        item
        for item in all_candidates
        if int(item.get("evidence_count") or 1) <= 1 and float(item.get("confidence") or 0) >= 0.8
    ]
    single_demoted = [item for item in all_candidates if "single_evidence_demoted" in set(item.get("quality_flags") or [])]
    media_file_ephemeral = [
        item
        for item in ephemeral
        if set(item.get("quality_flags") or []).intersection({"media_player_context", "file_listing_context"})
    ]
    noise_or_reference = [item for item in all_candidates if str(item.get("evidence_layer") or "") == EVIDENCE_LAYER_NOISE]
    confirmed_actions = [item for item in all_candidates if str(item.get("evidence_layer") or "") == EVIDENCE_LAYER_CONFIRMED]
    context_clues = [item for item in all_candidates if str(item.get("evidence_layer") or "") == EVIDENCE_LAYER_CONTEXT]
    screen_region_signals = [
        signal
        for item in all_candidates
        for signal in _candidate_region_signals(item)
    ]
    screen_region_stats = region_distribution(screen_region_signals)
    screen_region_noise = [item for item in all_candidates if _has_noise_screen_region(item)]
    finder_or_system_reference = [
        item
        for item in all_candidates
        if set(item.get("quality_flags") or []).intersection({"system_ui_context", "read_only_context_app"})
    ]
    task_list_reference = [item for item in all_candidates if "task_list_context" in set(item.get("quality_flags") or [])]
    keyword_only = [item for item in all_candidates if "keyword_only_project_tag" in set(item.get("quality_flags") or [])]
    third_party_subject = [item for item in all_candidates if str(item.get("subject_scope") or "") == SUBJECT_THIRD_PARTY]
    ambiguous_subject = [item for item in all_candidates if str(item.get("subject_scope") or "") == SUBJECT_AMBIGUOUS]
    self_or_owned_subject = [item for item in all_candidates if str(item.get("subject_scope") or "") == SUBJECT_SELF_OR_USER_OWNED]
    workflow_only_subject = [item for item in all_candidates if str(item.get("subject_scope") or "") == SUBJECT_USER_WORKFLOW_ONLY]
    target_role_jd = [item for item in all_candidates if SOURCE_CATEGORY_TARGET_ROLE_JD in _candidate_document_categories(item)]
    personal_medical_document = [item for item in all_candidates if SOURCE_CATEGORY_PERSONAL_MEDICAL_DOCUMENT in _candidate_document_categories(item) or set(item.get("quality_flags") or []).intersection({"personal_medical_document", "personal_medical_content"})]
    third_party_resume = [item for item in all_candidates if SOURCE_CATEGORY_THIRD_PARTY_RESUME in _candidate_document_categories(item)]
    ambiguous_document = [item for item in all_candidates if SOURCE_CATEGORY_AMBIGUOUS_DOCUMENT in _candidate_document_categories(item)]
    self_resume_document = [item for item in all_candidates if SOURCE_CATEGORY_SELF_RESUME in _candidate_document_categories(item)]
    reference_only_document = [
        item
        for item in all_candidates
        if _candidate_is_reference_only_document(item)
    ]
    trusted_anchor_eligible = [
        item
        for item in eligible
        if str(item.get("anchor_type") or "") in {ANCHOR_DOCUMENT_SIGNAL, ANCHOR_TRUSTED_APP}
    ]
    weak_anchor_review = [
        item
        for item in review
        if str(item.get("anchor_type") or ANCHOR_WEAK) in {ANCHOR_KEYWORD_ONLY, ANCHOR_WEAK}
    ]
    eligible_ratio = round(len(eligible) / total, 3) if total else 0.0
    summary = (
        f"长期记忆全量候选池 {total} 条：展示候选 {len(exposed_candidates)} 条、可写池 {len(eligible)} 条、"
        f"实际写入输入 {len(eligible)} 条、仅观察 {len(review)} 条、临时降级 {len(ephemeral)} 条；单证据降级 {len(single_demoted)} 条，"
        f"媒体/文件列表降级 {len(media_file_ephemeral)} 条，第三方/主体不明 {len(third_party_subject) + len(ambiguous_subject)} 条，"
        f"个人临床医疗保护 {len(personal_medical_document)} 条，JD/参考类文档 {len(target_role_jd) + len(reference_only_document)} 条，"
        f"confirmed action {len(confirmed_actions)} 条、context clue {len(context_clues)} 条、noise/reference {len(noise_or_reference)} 条，重复合并 {merged_count} 次。"
    )
    return {
        "total_candidate_count": total,
        "exposed_candidate_count": len(exposed_candidates),
        "eligible_candidate_count": len(eligible),
        "review_only_candidate_count": len(review),
        "ephemeral_candidate_count": len(ephemeral),
        "eligible_ratio": eligible_ratio,
        "single_evidence_candidate_count": sum(1 for item in all_candidates if int(item.get("evidence_count") or 1) <= 1),
        "single_evidence_eligible_count": len(single_eligible),
        "unknown_candidate_count": sum(1 for item in all_candidates if "unknown_app" in set(item.get("quality_flags") or [])),
        "unknown_eligible_count": len(unknown_eligible),
        "merged_duplicate_count": merged_count,
        "high_confidence_single_evidence_count": len(high_single),
        "single_evidence_demoted_count": len(single_demoted),
        "media_or_file_listing_ephemeral_count": len(media_file_ephemeral),
        "confirmed_action_count": len(confirmed_actions),
        "context_clue_count": len(context_clues),
        "noise_or_reference_count": len(noise_or_reference),
        "screen_region_distribution": screen_region_stats,
        "screen_region_noise_count": len(screen_region_noise),
        "screen_region_conflict_count": int(screen_region_stats.get("region_conflict_count") or 0),
        "finder_or_system_reference_count": len(finder_or_system_reference),
        "task_list_reference_count": len(task_list_reference),
        "keyword_only_tag_count": len(keyword_only),
        "third_party_subject_count": len(third_party_subject),
        "ambiguous_subject_count": len(ambiguous_subject),
        "subject_risk_candidate_count": len(third_party_subject) + len(ambiguous_subject),
        "self_or_user_owned_candidate_count": len(self_or_owned_subject),
        "user_workflow_only_candidate_count": len(workflow_only_subject),
        "target_role_jd_document_count": len(target_role_jd),
        "personal_medical_document_count": len(personal_medical_document),
        "third_party_resume_document_count": len(third_party_resume),
        "ambiguous_document_count": len(ambiguous_document),
        "self_resume_document_count": len(self_resume_document),
        "reference_only_document_count": len(reference_only_document),
        "trusted_anchor_eligible_count": len(trusted_anchor_eligible),
        "weak_anchor_review_only_count": len(weak_anchor_review),
        "write_input_count": len(eligible),
        "summary": summary,
    }


def _candidate_anchor_type(item: dict, project_tags: list[str]) -> str:
    if int(item.get("document_context_count") or 0) > 0:
        return ANCHOR_DOCUMENT_SIGNAL
    if (
        str(item.get("confirmation_source") or "") == "user"
        and _normalize_app(str(item.get("app") or "")) == "manual confirmation"
    ):
        return ANCHOR_TRUSTED_APP
    if _is_trusted_memory_app(str(item.get("app") or "")):
        return ANCHOR_TRUSTED_APP
    if project_tags:
        return ANCHOR_KEYWORD_ONLY
    return ANCHOR_WEAK


def _anchor_strength(anchor_type: str) -> int:
    return {
        ANCHOR_DOCUMENT_SIGNAL: 3,
        ANCHOR_TRUSTED_APP: 2,
        ANCHOR_KEYWORD_ONLY: 1,
        ANCHOR_WEAK: 0,
    }.get(str(anchor_type or ""), 0)


def _is_trusted_memory_app(app: str) -> bool:
    return normalize_app_name(app) in TRUSTED_MEMORY_APPS


def _is_media_player_context(app: str, summary: str) -> bool:
    app_text = _normalize_app(app)
    text = f"{app_text} {str(summary or '').lower()}"
    if app_text in MEDIA_PLAYER_APPS:
        return True
    return _contains_any(
        text,
        [
            ".mkv",
            ".mp4",
            ".avi",
            ".mov",
            "webrip",
            "web-dl",
            "bluray",
            "s01e",
            "字幕",
            "回放",
            "音频",
            "插件",
            "片尾",
            "movies",
        ],
    )


def _is_file_listing_context(summary: str) -> bool:
    text = str(summary or "").lower()
    if "[path_redacted]" in text:
        return True
    file_tokens = sum(1 for token in (".md", ".pdf", ".docx", ".mp4", ".mkv", ".png", ".jpg", ".sqlite") if token in text)
    return file_tokens >= 2 or _contains_any(text, ["文件 编辑", "文件 帮助", "downloads", "desktop", "替身", "文件夹", "侧边栏"])


def _is_task_list_context(app: str, summary: str) -> bool:
    app_text = _normalize_app(app)
    text = str(summary or "").lower()
    if app_text in TASK_LIST_APPS:
        return True
    return _contains_any(
        text,
        [
            "滴答清单",
            "ticktick",
            "收集箱",
            "今天",
            "明天",
            "最近7天",
            "已完成",
            "已放弃",
            "添加任务",
            "待办清单",
        ],
    )


def _is_browser_tab_chrome_context(app: str, summary: str) -> bool:
    app_text = _normalize_app(app)
    text = str(summary or "").lower()
    if not app_identity_in(app_text, "", BROWSER_FAMILIES):
        return False
    return _contains_any(text, ["没有新电子邮件", "键盘快捷键", "标签页", "tab", "yahoo 邮箱", "文件夹"]) or text.count(" - ") >= 5


def _is_system_ui_context(workflow: str, app: str, summary: str) -> bool:
    app_text = _normalize_app(app)
    text = str(summary or "").lower()
    if workflow == "system_app_control":
        return True
    if app_text in SYSTEM_UI_APPS:
        return True
    return _contains_any(text, ["菜单栏", "系统设置", "control center", "battery", "wifi"])


def _is_read_only_context_app(app: str) -> bool:
    app_text = _normalize_app(app)
    return app_text in READ_ONLY_CONTEXT_APPS


def _is_primary_action_app(app: str) -> bool:
    app_text = _normalize_app(app)
    return app_text in PRIMARY_ACTION_APPS


def _candidate_region_signals(candidate: dict) -> list[dict]:
    signals = candidate.get("_screen_region_signals") or []
    if not isinstance(signals, list):
        signals = [signals]
    output: list[dict] = []
    for signal in signals:
        normalized = sanitize_screen_region_signal(signal)
        if normalized:
            output.append(normalized)
    return output


def _primary_screen_region_signal(candidate: dict) -> dict:
    signals = _candidate_region_signals(candidate)
    if not signals:
        return _unknown_screen_region_signal()
    signals.sort(
        key=lambda signal: (
            _region_fail_closed_rank(str(signal.get("actionability") or ACTIONABILITY_UNKNOWN)),
            -float(signal.get("confidence") or 0.0),
        )
    )
    return signals[0]


def _public_region_signals(candidate: dict) -> list[dict]:
    return _candidate_region_signals(candidate)[:8]


def _screen_region_enforcement_enabled() -> bool:
    value = str(os.environ.get(SCREEN_REGION_ENFORCEMENT_ENV) or "").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _has_noise_screen_region(candidate: dict) -> bool:
    return any(region_is_noise(signal) for signal in _candidate_region_signals(candidate))


def _has_non_actionable_screen_region(candidate: dict) -> bool:
    signals = _candidate_region_signals(candidate)
    return not signals or any(not region_is_actionable(signal) for signal in signals)


def _screen_region_quality_flags(candidate: dict) -> list[str]:
    # A manual user confirmation is not a screen observation. Requiring a
    # synthetic screen region here would demote the only explicitly governed
    # legacy confirmation path back to an unverified UI clue.
    if candidate.get("_manual_confirmation"):
        return ["manual_user_confirmation"]
    signal = _primary_screen_region_signal(candidate)
    region = str(signal.get("region_kind") or REGION_UNKNOWN)
    actionability = str(signal.get("actionability") or ACTIONABILITY_UNKNOWN)
    source = str(signal.get("source") or "")
    flags = [f"screen_region_{region}", f"screen_region_actionability_{actionability}"]
    if actionability == ACTIONABILITY_NOISE or region_is_noise(signal):
        flags.append("screen_region_noise")
        if _screen_region_enforcement_enabled():
            flags.append("screen_region_noise_enforced")
    if actionability == ACTIONABILITY_CONTEXT_ONLY and _screen_region_enforcement_enabled():
        flags.append("screen_region_context_enforced")
    if actionability == ACTIONABILITY_UNKNOWN and _screen_region_enforcement_enabled():
        flags.append("screen_region_unknown_enforced")
    if _has_non_actionable_screen_region(candidate) and _screen_region_enforcement_enabled():
        flags.append("screen_region_non_actionable_enforced")
    if set(signal.get("reason_codes") or []).intersection(
        {
            "rule_conflict",
            "layout_conflict_fail_closed",
            "same_region_actionability_conflict_fail_closed",
            "non_actionable_conflict_fail_closed",
            "actionable_region_conflict",
        }
    ):
        flags.append("screen_region_conflict")
    return flags[:8]


def _document_mtime_events(candidate: dict) -> set[str]:
    return set(_string_list(candidate.get("_document_mtime_events") or []))


def _has_recent_document_modification(candidate: dict) -> bool:
    return "modified_near_capture" in _document_mtime_events(candidate)


def _has_reference_document_state(candidate: dict) -> bool:
    return bool(
        _document_mtime_events(candidate).intersection(
            {
                "reference_app_visible",
                "metadata_reference",
                "viewed_without_recent_modify",
                "opened_without_recent_modify",
            }
        )
    )


def _candidate_subject_assessment(candidate: dict) -> dict:
    text = _candidate_subject_text(candidate)
    app = _normalize_app(str(candidate.get("app") or ""))
    candidate_type = str(candidate.get("candidate_type") or "")
    anchor_type = str(candidate.get("anchor_type") or ANCHOR_WEAK)
    doc_categories = _candidate_document_categories(candidate)
    doc_subject_scopes = set(_string_list(candidate.get("_document_subject_scopes") or []))
    doc_source_scopes = set(_string_list(candidate.get("_document_source_scopes") or []))
    doc_policies = set(_string_list(candidate.get("_document_memory_policies") or []))
    doc_risk_flags = set(_string_list(candidate.get("_document_risk_flags") or []))
    doc_classification_reasons = _string_list(candidate.get("_document_classification_reasons") or [])
    has_self = _has_self_subject_signal(text)
    has_explicit_third_party = _has_explicit_third_party_subject_signal(text)
    has_resume_subject = candidate_type == "document_work" or _contains_any(text, RESUME_SUBJECT_HINTS)
    has_profile_detail = _contains_any(text, PROFILE_DETAIL_HINTS)
    reasons: list[str] = []
    risks: list[str] = []

    if (
        doc_categories.intersection(DOCUMENT_PERSONAL_MEDICAL_CATEGORIES)
        or doc_policies.intersection(DOCUMENT_PERSONAL_MEDICAL_POLICIES)
        or doc_risk_flags.intersection(PERSONAL_MEDICAL_RISK_FLAGS)
        or SOURCE_SCOPE_PROTECTED in doc_source_scopes
    ):
        scope = SUBJECT_SELF_OR_USER_OWNED
        confidence = 0.92
        reasons.append("document_classified_personal_medical_protected")
        risks.extend(["personal_medical_content", "personal_medical_document"])
    elif doc_categories.intersection(DOCUMENT_THIRD_PARTY_CATEGORIES) or SOURCE_SCOPE_THIRD_PARTY in doc_source_scopes or SUBJECT_THIRD_PARTY in doc_subject_scopes:
        scope = SUBJECT_THIRD_PARTY
        confidence = 0.9
        reasons.append("document_classified_third_party_subject")
        risks.append("third_party_subject")
    elif doc_categories.intersection(DOCUMENT_AMBIGUOUS_CATEGORIES) or SOURCE_SCOPE_AMBIGUOUS in doc_source_scopes or SUBJECT_AMBIGUOUS in doc_subject_scopes:
        scope = SUBJECT_AMBIGUOUS
        confidence = 0.84
        reasons.append("document_classified_ambiguous_subject")
        risks.append("subject_ownership_unclear")
    elif doc_categories.intersection(DOCUMENT_SELF_OWNED_CATEGORIES) or SOURCE_SCOPE_SELF_OWNED in doc_source_scopes or SUBJECT_SELF_OR_USER_OWNED in doc_subject_scopes:
        scope = SUBJECT_SELF_OR_USER_OWNED
        confidence = 0.9
        reasons.append("document_classified_self_or_user_owned")
    elif SOURCE_CATEGORY_TARGET_ROLE_JD in doc_categories or SOURCE_SCOPE_REFERENCE_ONLY in doc_source_scopes or doc_policies.intersection(DOCUMENT_REFERENCE_ONLY_POLICIES):
        scope = SUBJECT_AMBIGUOUS
        confidence = 0.86
        reasons.append("document_classified_reference_only_job_material")
        risks.extend(["reference_only_document", "subject_ownership_unclear"])
    elif has_explicit_third_party and has_self:
        scope = SUBJECT_AMBIGUOUS
        confidence = 0.78
        reasons.append("mixed_self_and_third_party_subject_terms")
        risks.append("subject_ownership_unclear")
    elif has_explicit_third_party:
        scope = SUBJECT_THIRD_PARTY
        confidence = 0.86
        reasons.append("explicit_third_party_subject")
        risks.append("third_party_subject")
    elif has_self:
        scope = SUBJECT_SELF_OR_USER_OWNED
        confidence = 0.88
        reasons.append("explicit_self_or_user_owned_signal")
    elif (
        candidate.get("_user_confirmed")
        and candidate.get("_confirmation_subject_scope_hint")
        == SUBJECT_SELF_OR_USER_OWNED
    ):
        scope = SUBJECT_SELF_OR_USER_OWNED
        confidence = 0.88
        reasons.append("user_confirmed_self_owned_source_signal")
    elif has_resume_subject or (has_profile_detail and anchor_type == ANCHOR_DOCUMENT_SIGNAL):
        scope = SUBJECT_AMBIGUOUS
        confidence = 0.74 if has_profile_detail else 0.68
        reasons.append("document_subject_without_self_signal")
        risks.append("subject_ownership_unclear")
        if _is_external_document_context(app, text):
            risks.append("external_document_subject_unclear")
        if has_profile_detail:
            risks.append("profile_detail_subject_unclear")
    else:
        scope = SUBJECT_AMBIGUOUS
        confidence = 0.72
        reasons.append("missing_explicit_self_ownership_evidence")
        risks.append("subject_ownership_unclear")

    for reason in doc_classification_reasons:
        if reason and reason not in reasons:
            reasons.append(reason)
    for flag in doc_risk_flags:
        if flag and flag not in risks:
            risks.append(flag)
    return {
        "subject_scope": scope,
        "subject_confidence": round(confidence, 2),
        "subject_reasons": reasons[:6],
        "subject_risk_flags": risks[:8],
    }


def _candidate_subject_text(candidate: dict) -> str:
    parts = [
        candidate.get("topic") or "",
        candidate.get("safe_summary") or "",
        candidate.get("app") or "",
        candidate.get("workflow") or "",
        candidate.get("candidate_type") or "",
        " ".join(candidate.get("project_tags") or []),
        " ".join(candidate.get("_document_source_categories") or []),
        " ".join(candidate.get("_document_source_scopes") or []),
        " ".join(candidate.get("_document_subject_scopes") or []),
        " ".join(candidate.get("_document_memory_policies") or []),
        " ".join(candidate.get("_document_risk_flags") or []),
        " ".join(candidate.get("_document_classification_reasons") or []),
    ]
    return " ".join(str(part) for part in parts).lower()


def _has_self_subject_signal(text: str) -> bool:
    return _contains_any(text, SELF_SUBJECT_HINTS)


def _has_explicit_third_party_subject_signal(text: str) -> bool:
    return _contains_any(text, EXPLICIT_THIRD_PARTY_SUBJECT_HINTS)


def _is_external_document_context(app: str, text: str) -> bool:
    app_text = _normalize_app(app)
    if app_identity_in(app_text, "", BROWSER_FAMILIES):
        return True
    if app_text in {"preview", "finder", "acrobat", "pdf", "wps", "word", "microsoft word", "pages"}:
        return True
    return _contains_any(text, [".pdf", ".docx", ".doc", ".pages", "网页", "browser"])


def _normalize_app(value: str) -> str:
    return str(value or "").strip().lower()


def _memory_quality_flags(candidate: dict) -> list[str]:
    flags: list[str] = []
    evidence_count = int(candidate.get("evidence_count") or 1)
    workflow = str(candidate.get("workflow") or "unknown")
    app = str(candidate.get("app") or "")
    summary = str(candidate.get("safe_summary") or "")
    candidate_type = str(candidate.get("candidate_type") or "")
    anchor_type = str(candidate.get("anchor_type") or ANCHOR_WEAK)
    if evidence_count <= 1:
        flags.append("single_evidence")
    else:
        flags.append("multi_evidence")
    if _is_unknown_app(app):
        flags.append("unknown_app")
    if _is_trusted_memory_app(app):
        flags.append("trusted_app_context")
    if workflow in WEAK_WORKFLOWS_FOR_MEMORY:
        flags.append("weak_workflow")
    if workflow in {"media", "system_app_control"}:
        flags.append("browser_or_system_chrome")
    if _is_media_player_context(app, summary):
        flags.append("media_player_context")
    if _is_file_listing_context(summary):
        flags.append("file_listing_context")
    if _is_task_list_context(app, summary):
        flags.append("task_list_context")
    if _is_browser_tab_chrome_context(app, summary):
        flags.append("browser_tab_chrome")
    if _is_system_ui_context(workflow, app, summary):
        flags.append("system_ui_context")
    if _is_read_only_context_app(app):
        flags.append("read_only_context_app")
    flags.extend(_screen_region_quality_flags(candidate))
    if len(summary) < 60:
        flags.append("weak_summary")
    if candidate_type in ELIGIBLE_CANDIDATE_TYPES:
        flags.append("durable_candidate")
    if anchor_type == ANCHOR_DOCUMENT_SIGNAL:
        flags.append("document_anchor")
    elif candidate_type == "document_work":
        flags.append("weak_document_anchor")
    if candidate.get("project_tags") and anchor_type == ANCHOR_KEYWORD_ONLY:
        flags.append("keyword_only_project_tag")
    if not candidate.get("project_tags"):
        flags.append("no_project_tags")
    if candidate.get("learnability") == "low_signal":
        flags.append("low_signal_source")
    if _has_recent_document_modification(candidate):
        flags.append("document_modified_near_capture")
    elif _has_reference_document_state(candidate):
        flags.append("document_not_modified_near_capture")
    elif anchor_type == ANCHOR_DOCUMENT_SIGNAL:
        flags.append("document_mtime_unknown")
    if int(candidate.get("merged_duplicate_count") or 0) > 0:
        flags.append("merged_duplicate")
    subject_scope = str(candidate.get("subject_scope") or SUBJECT_AMBIGUOUS)
    if subject_scope == SUBJECT_THIRD_PARTY:
        flags.append("third_party_subject")
    elif subject_scope == SUBJECT_AMBIGUOUS:
        flags.append("ambiguous_subject")
    doc_categories = _candidate_document_categories(candidate)
    doc_policies = set(_string_list(candidate.get("_document_memory_policies") or []))
    doc_scopes = set(_string_list(candidate.get("_document_source_scopes") or []))
    doc_risk_flags = set(_string_list(candidate.get("_document_risk_flags") or []))
    if SOURCE_CATEGORY_SELF_RESUME in doc_categories:
        flags.append("self_resume_document")
    if doc_categories.intersection(DOCUMENT_PERSONAL_MEDICAL_CATEGORIES) or doc_policies.intersection(DOCUMENT_PERSONAL_MEDICAL_POLICIES):
        flags.append("personal_medical_document")
    if SOURCE_CATEGORY_TARGET_ROLE_JD in doc_categories:
        flags.append("target_role_jd_document")
    if SOURCE_CATEGORY_THIRD_PARTY_RESUME in doc_categories:
        flags.append("third_party_resume_document")
    if SOURCE_CATEGORY_AMBIGUOUS_DOCUMENT in doc_categories:
        flags.append("ambiguous_document")
    if SOURCE_SCOPE_REFERENCE_ONLY in doc_scopes or doc_policies.intersection(DOCUMENT_REFERENCE_ONLY_POLICIES):
        flags.append("reference_only_document")
    if "blocked_subject" in doc_policies:
        flags.append("blocked_subject_document")
    if doc_risk_flags.intersection(PERSONAL_MEDICAL_RISK_FLAGS):
        flags.append("personal_medical_content")
    if "contains_role_requirement_terms" in doc_risk_flags or "contains_role_requirement_terms" in set(candidate.get("subject_risk_flags") or []):
        flags.append("contains_role_requirement_terms")
    return flags


def _write_risk_flags(candidate: dict, quality_flags: list[str]) -> list[str]:
    risks: list[str] = []
    flag_set = set(quality_flags)
    for flag in (
        "single_evidence_demoted",
        "media_player_context",
        "file_listing_context",
        "task_list_context",
        "browser_tab_chrome",
        "system_ui_context",
        "read_only_context_app",
        "screen_region_noise_enforced",
        "screen_region_context_enforced",
        "screen_region_unknown_enforced",
        "screen_region_non_actionable_enforced",
        "document_not_modified_near_capture",
        "document_mtime_unknown",
        "keyword_only_project_tag",
        "weak_document_anchor",
        "unknown_app",
        "weak_summary",
        "low_signal_source",
        "third_party_subject",
        "ambiguous_subject",
        "personal_medical_document",
        "personal_medical_content",
        "target_role_jd_document",
        "third_party_resume_document",
        "ambiguous_document",
        "contains_role_requirement_terms",
        "reference_only_document",
        "blocked_subject_document",
    ):
        if flag in flag_set and flag not in risks:
            risks.append(flag)
    for flag in candidate.get("subject_risk_flags") or []:
        text = str(flag or "")
        if text and text not in risks:
            risks.append(text)
    if str(candidate.get("anchor_type") or ANCHOR_WEAK) == ANCHOR_WEAK and "weak_or_no_anchor" not in risks:
        risks.append("weak_or_no_anchor")
    return risks[:10]


def _write_eligibility(candidate: dict, flags: list[str]) -> str:
    flag_set = set(flags)
    evidence_count = int(candidate.get("evidence_count") or 1)
    candidate_type = str(candidate.get("candidate_type") or "")
    workflow = str(candidate.get("workflow") or "")
    has_tags = bool(candidate.get("project_tags"))
    anchor_type = str(candidate.get("anchor_type") or ANCHOR_WEAK)
    subject_scope = str(candidate.get("subject_scope") or SUBJECT_AMBIGUOUS)
    source_category = str(candidate.get("source_category") or "")
    memory_policy = str(candidate.get("memory_write_policy") or "")
    source_scope = str(candidate.get("source_scope") or "")
    has_document_anchor = anchor_type == ANCHOR_DOCUMENT_SIGNAL
    has_trusted_anchor = anchor_type in {ANCHOR_DOCUMENT_SIGNAL, ANCHOR_TRUSTED_APP}
    hard_reference_context = bool(
        flag_set.intersection(
            {
                "media_player_context",
                "file_listing_context",
                "task_list_context",
                "browser_tab_chrome",
                "system_ui_context",
                "read_only_context_app",
                "screen_region_noise_enforced",
                "screen_region_context_enforced",
                "screen_region_unknown_enforced",
                "screen_region_non_actionable_enforced",
            }
        )
    )
    noisy_context = bool(flag_set.intersection({"browser_or_system_chrome"})) or hard_reference_context
    durable_decision_type = candidate_type in {"decision", "preference", "blocker", "follow_up"}
    confirmed_action = _has_confirmed_action_evidence(candidate, flag_set)
    if flag_set.intersection({"personal_medical_document", "personal_medical_content"}) or memory_policy in DOCUMENT_PERSONAL_MEDICAL_POLICIES or source_category in DOCUMENT_PERSONAL_MEDICAL_CATEGORIES:
        return WRITE_ELIGIBILITY_EPHEMERAL
    if subject_scope != SUBJECT_SELF_OR_USER_OWNED:
        return WRITE_ELIGIBILITY_EPHEMERAL
    if source_category in DOCUMENT_REFERENCE_ONLY_CATEGORIES or memory_policy in DOCUMENT_REFERENCE_ONLY_POLICIES or source_scope == SOURCE_SCOPE_REFERENCE_ONLY:
        return WRITE_ELIGIBILITY_EPHEMERAL if noisy_context else WRITE_ELIGIBILITY_REVIEW_ONLY
    if hard_reference_context:
        if candidate_type in ELIGIBLE_CANDIDATE_TYPES and (has_document_anchor or has_trusted_anchor):
            return WRITE_ELIGIBILITY_REVIEW_ONLY
        return WRITE_ELIGIBILITY_EPHEMERAL
    if evidence_count <= 1:
        if candidate_type in ELIGIBLE_CANDIDATE_TYPES and has_trusted_anchor:
            return WRITE_ELIGIBILITY_REVIEW_ONLY
        return WRITE_ELIGIBILITY_EPHEMERAL
    if (
        candidate_type == "document_work"
        and source_category == SOURCE_CATEGORY_SELF_RESUME
        and subject_scope == SUBJECT_SELF_OR_USER_OWNED
        and has_document_anchor
        and "weak_summary" not in flag_set
        and confirmed_action
    ):
        return WRITE_ELIGIBILITY_ELIGIBLE
    if noisy_context:
        if durable_decision_type and has_trusted_anchor:
            return WRITE_ELIGIBILITY_REVIEW_ONLY
        return WRITE_ELIGIBILITY_EPHEMERAL
    if "unknown_app" in flag_set:
        if evidence_count >= 2 and has_document_anchor and candidate_type in ELIGIBLE_CANDIDATE_TYPES:
            return WRITE_ELIGIBILITY_ELIGIBLE
        return WRITE_ELIGIBILITY_REVIEW_ONLY if candidate_type in ELIGIBLE_CANDIDATE_TYPES else WRITE_ELIGIBILITY_EPHEMERAL
    if "weak_summary" in flag_set and not has_tags and evidence_count <= 1:
        return WRITE_ELIGIBILITY_EPHEMERAL
    if candidate_type == "document_work" and not has_trusted_anchor:
        return WRITE_ELIGIBILITY_REVIEW_ONLY
    if candidate_type == "project_fact" and anchor_type == ANCHOR_KEYWORD_ONLY and not _has_strong_durable_signal(candidate):
        return WRITE_ELIGIBILITY_REVIEW_ONLY
    if candidate_type in ELIGIBLE_CANDIDATE_TYPES:
        if anchor_type == ANCHOR_KEYWORD_ONLY and evidence_count < 2:
            return WRITE_ELIGIBILITY_REVIEW_ONLY
        return WRITE_ELIGIBILITY_ELIGIBLE if confirmed_action else WRITE_ELIGIBILITY_REVIEW_ONLY
    if evidence_count >= 2 and has_tags and workflow in {"coding_debugging", "writing", "research", "file_management"}:
        return WRITE_ELIGIBILITY_ELIGIBLE if has_trusted_anchor and confirmed_action else WRITE_ELIGIBILITY_REVIEW_ONLY
    if workflow in WEAK_WORKFLOWS_FOR_MEMORY and not has_tags:
        return WRITE_ELIGIBILITY_EPHEMERAL
    return WRITE_ELIGIBILITY_REVIEW_ONLY


def _has_strong_durable_signal(candidate: dict) -> bool:
    text = f"{candidate.get('topic') or ''} {candidate.get('safe_summary') or ''}".lower()
    return _contains_any(
        text,
        [
            "决定",
            "决策",
            "decision",
            "decided",
            "default",
            "root cause",
            "fixed",
            "implemented",
            "blocked",
            "todo",
            "next step",
            "偏好",
            "阻塞",
            "修复",
            "实现",
            "默认",
            "待办",
            "下一步",
        ],
    )


def _has_confirmed_action_evidence(candidate: dict, flags: set[str]) -> bool:
    evidence_count = int(candidate.get("evidence_count") or 1)
    user_confirmed = bool(candidate.get("_user_confirmed"))
    if evidence_count < 2 and not user_confirmed:
        return False
    if flags.intersection(
        {
            "media_player_context",
            "file_listing_context",
            "task_list_context",
            "browser_tab_chrome",
            "system_ui_context",
            "read_only_context_app",
            "screen_region_noise_enforced",
            "screen_region_context_enforced",
            "screen_region_unknown_enforced",
            "screen_region_non_actionable_enforced",
        }
    ):
        return False
    app = str(candidate.get("app") or "")
    anchor_type = str(candidate.get("anchor_type") or ANCHOR_WEAK)
    candidate_type = str(candidate.get("candidate_type") or "")
    if str(candidate.get("subject_scope") or SUBJECT_AMBIGUOUS) != SUBJECT_SELF_OR_USER_OWNED:
        return False
    if user_confirmed and candidate.get("_manual_confirmation"):
        return candidate_type in ELIGIBLE_CANDIDATE_TYPES
    if not _has_action_region_evidence(candidate):
        return False
    if _has_recent_document_modification(candidate) and anchor_type == ANCHOR_DOCUMENT_SIGNAL:
        return True
    if anchor_type == ANCHOR_DOCUMENT_SIGNAL and candidate_type == "document_work":
        return False
    if _is_primary_action_app(app) and candidate_type in ELIGIBLE_CANDIDATE_TYPES and anchor_type in {ANCHOR_DOCUMENT_SIGNAL, ANCHOR_TRUSTED_APP}:
        return True
    return False


def _candidate_evidence_layer(candidate: dict, flags: list[str]) -> dict:
    flag_set = set(flags)
    signals: list[str] = []
    if int(candidate.get("evidence_count") or 1) >= 2:
        signals.append("multi_evidence")
    if candidate.get("_user_confirmed"):
        signals.append("user_confirmation")
    if str(candidate.get("anchor_type") or "") == ANCHOR_DOCUMENT_SIGNAL:
        signals.append("document_signal")
    if _is_primary_action_app(str(candidate.get("app") or "")):
        signals.append("primary_action_app")
    if _has_recent_document_modification(candidate):
        signals.append("document_modified_near_capture")
    if _has_non_actionable_screen_region(candidate):
        signals.append("screen_region_non_actionable")
    if flag_set.intersection({"file_listing_context", "system_ui_context", "task_list_context", "browser_tab_chrome", "media_player_context", "read_only_context_app", "screen_region_noise_enforced", "screen_region_context_enforced", "screen_region_unknown_enforced", "screen_region_non_actionable_enforced"}):
        layer = EVIDENCE_LAYER_NOISE
        explanation = "只看到文件/文件夹、侧边栏、任务列表、浏览器标签、媒体或系统 UI 等参考性文字，不能当作当前行动。"
    elif str(candidate.get("write_eligibility") or "") == WRITE_ELIGIBILITY_ELIGIBLE and _has_confirmed_action_evidence(candidate, flag_set):
        layer = EVIDENCE_LAYER_CONFIRMED
        explanation = "多条证据来自可信工作上下文或当前文档修改，足以作为 Hermes confirmed action。"
    else:
        layer = EVIDENCE_LAYER_CONTEXT
        explanation = "有安全上下文线索，但缺少当前修改、深度沟通或明确行动确认，只能作为 Review 线索。"
    return {
        "evidence_layer": layer,
        "confirmation_signals": signals[:8],
        "evidence_explanation": explanation,
    }


def _has_action_region_evidence(candidate: dict) -> bool:
    signals = _candidate_region_signals(candidate)
    if not signals or any(not region_is_actionable(signal) for signal in signals):
        return False
    for signal in signals:
        source = str(signal.get("source") or "")
        reasons = set(signal.get("reason_codes") or [])
        confidence = float(signal.get("confidence") or 0.0)
        if reasons.intersection({"submit_interaction_evidence", "submit_control_geometry", "actionable_layout_preferred"}):
            return True
        if any(reason.startswith("ocr_region_main_content") for reason in reasons) and confidence >= 0.55:
            return True
    return False


def _region_fail_closed_rank(actionability: str) -> int:
    return {
        ACTIONABILITY_NOISE: 0,
        ACTIONABILITY_CONTEXT_ONLY: 1,
        ACTIONABILITY_UNKNOWN: 2,
        "actionable": 3,
    }.get(actionability, 2)


def _candidate_provenance_time_range(candidate: dict) -> dict[str, str]:
    started = candidate.get("_first_at") or candidate.get("_started_at")
    ended = candidate.get("_last_at") or candidate.get("_ended_at") or started
    return {
        "from": started.isoformat() if hasattr(started, "isoformat") else str(started or ""),
        "to": ended.isoformat() if hasattr(ended, "isoformat") else str(ended or ""),
    }


def _has_strong_document_anchor(candidate: dict, flags: set[str]) -> bool:
    return (
        str(candidate.get("anchor_type") or "") == ANCHOR_DOCUMENT_SIGNAL
        and str(candidate.get("source_category") or "") == SOURCE_CATEGORY_SELF_RESUME
        and str(candidate.get("memory_write_policy") or "") == "self_resume_safe_summary"
        and str(candidate.get("subject_scope") or "") == SUBJECT_SELF_OR_USER_OWNED
        and "weak_summary" not in flags
    )


def _has_strong_single_evidence(candidate: dict, flags: list[str]) -> bool:
    if set(flags).intersection({"weak_summary", "low_signal_source", "single_evidence_demoted"}):
        return False
    anchor_type = str(candidate.get("anchor_type") or ANCHOR_WEAK)
    has_strong_anchor = anchor_type == ANCHOR_DOCUMENT_SIGNAL
    return has_strong_anchor and _has_strong_durable_signal(candidate)


def _candidate_time_bounds(item: dict) -> tuple[object | None, object | None]:
    label = str(item.get("time_label") or "")
    parts = [part.strip() for part in label.split("->", 1)]
    started = _parse_candidate_time(parts[0] if parts else "")
    ended = _parse_candidate_time(parts[1] if len(parts) > 1 else "") or started
    return started, ended


def _parse_candidate_time(value: str):
    if not value:
        return None
    try:
        return parse_time(value)
    except (TypeError, ValueError):
        return None


def _candidate_gap_seconds(left, right) -> float:
    if left is None or right is None:
        return 999999.0
    try:
        return abs((right - left).total_seconds())
    except Exception:
        return 999999.0


def _max_candidate_time(left, right):
    if left is None:
        return right
    if right is None:
        return left
    return right if right > left else left


def _candidate_span_minutes(started, ended) -> int:
    if started is None or ended is None:
        return 0
    try:
        return max(0, int(round((ended - started).total_seconds() / 60)))
    except Exception:
        return 0


def _merged_time_label(left: str, right: str) -> str:
    if not left:
        return right
    if not right or right == left:
        return left
    left_start = left.split("->", 1)[0].strip()
    right_end = right.split("->", 1)[-1].strip()
    if left_start and right_end and left_start != right_end:
        return f"{left_start} -> {right_end}"
    return left


def _merge_tags(left: list[str], right: list[str]) -> list[str]:
    output: list[str] = []
    for value in [*left, *right]:
        text = str(value or "").strip()
        if text and text not in output:
            output.append(text)
    return output[:5]


def _string_list(values) -> list[str]:
    if not isinstance(values, list):
        values = [values] if values else []
    output: list[str] = []
    for value in values:
        text = _sanitize_text(str(value or "")).lower()
        if text and text not in output:
            output.append(text)
    return output


def _primary_document_value(item: dict, key: str, default: str) -> str:
    values = _string_list(item.get(key) or [])
    return values[0] if values else default


def _merge_unique_values(left: list[str], right: list[str], limit: int = 12) -> list[str]:
    output: list[str] = []
    for value in [*(left or []), *(right or [])]:
        text = _sanitize_text(str(value or "")).lower()
        if text and text not in output:
            output.append(text)
    return output[:limit]


def _merge_candidate_document_metadata(target: dict, candidate: dict) -> None:
    for key in (
        "_document_source_categories",
        "_document_source_scopes",
        "_document_subject_scopes",
        "_document_memory_policies",
        "_document_risk_flags",
        "_document_classification_reasons",
        "_document_mtime_events",
    ):
        target[key] = _merge_unique_values(target.get(key) or [], candidate.get(key) or [])
    target["source_category"] = _select_source_category(
        _string_list(target.get("_document_source_categories") or []),
        str(target.get("source_category") or SOURCE_CATEGORY_GENERAL_DOCUMENT),
    )
    target["source_scope"] = _select_source_scope(
        _string_list(target.get("_document_source_scopes") or []),
        str(target.get("source_scope") or SOURCE_SCOPE_CONTEXT_ONLY),
    )
    target["memory_write_policy"] = _select_memory_policy(
        _string_list(target.get("_document_memory_policies") or []),
        str(target.get("memory_write_policy") or "workflow_only"),
    )


def _merge_candidate_region_metadata(target: dict, candidate: dict) -> None:
    target["_screen_region_signals"] = _merge_region_signals(
        target.get("_screen_region_signals") or [],
        candidate.get("_screen_region_signals") or [],
    )


def _merge_region_signals(left: list[dict], right: list[dict], limit: int = 12) -> list[dict]:
    output: list[dict] = []
    seen: set[str] = set()
    for signal in [*(left or []), *(right or [])]:
        normalized = sanitize_screen_region_signal(signal)
        if not normalized:
            continue
        key = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        output.append(normalized)
    return output[:limit]


def _select_source_category(values: list[str], fallback: str = SOURCE_CATEGORY_GENERAL_DOCUMENT) -> str:
    priority = [
        SOURCE_CATEGORY_PERSONAL_MEDICAL_DOCUMENT,
        SOURCE_CATEGORY_THIRD_PARTY_RESUME,
        SOURCE_CATEGORY_AMBIGUOUS_DOCUMENT,
        SOURCE_CATEGORY_TARGET_ROLE_JD,
        SOURCE_CATEGORY_SELF_RESUME,
        SOURCE_CATEGORY_INTERVIEW_NOTES,
        SOURCE_CATEGORY_GENERAL_DOCUMENT,
    ]
    for value in priority:
        if value in values:
            return value
    return fallback or SOURCE_CATEGORY_GENERAL_DOCUMENT


def _select_source_scope(values: list[str], fallback: str = SOURCE_SCOPE_CONTEXT_ONLY) -> str:
    priority = [
        SOURCE_SCOPE_PROTECTED,
        SOURCE_SCOPE_THIRD_PARTY,
        SOURCE_SCOPE_AMBIGUOUS,
        SOURCE_SCOPE_REFERENCE_ONLY,
        SOURCE_SCOPE_SELF_OWNED,
        SOURCE_SCOPE_CONTEXT_ONLY,
    ]
    for value in priority:
        if value in values:
            return value
    return fallback or SOURCE_SCOPE_CONTEXT_ONLY


def _select_memory_policy(values: list[str], fallback: str = "workflow_only") -> str:
    priority = ["blocked_personal_medical", "blocked_subject", "reference_only", "self_resume_safe_summary", "workflow_only"]
    for value in priority:
        if value in values:
            return value
    return fallback or "workflow_only"


def _candidate_document_categories(candidate: dict) -> set[str]:
    values = set(_string_list(candidate.get("_document_source_categories") or []))
    source_category = _sanitize_text(str(candidate.get("source_category") or "")).lower()
    if source_category:
        values.add(source_category)
    return values


def _candidate_is_reference_only_document(candidate: dict) -> bool:
    categories = _candidate_document_categories(candidate)
    policies = set(_string_list(candidate.get("_document_memory_policies") or []))
    scopes = set(_string_list(candidate.get("_document_source_scopes") or []))
    source_scope = _sanitize_text(str(candidate.get("source_scope") or "")).lower()
    memory_policy = _sanitize_text(str(candidate.get("memory_write_policy") or "")).lower()
    return bool(
        categories.intersection(DOCUMENT_REFERENCE_ONLY_CATEGORIES)
        or categories.intersection(DOCUMENT_PERSONAL_MEDICAL_CATEGORIES)
        or policies.intersection(DOCUMENT_REFERENCE_ONLY_POLICIES)
        or policies.intersection(DOCUMENT_PERSONAL_MEDICAL_POLICIES)
        or SOURCE_SCOPE_REFERENCE_ONLY in scopes
        or SOURCE_SCOPE_PROTECTED in scopes
        or source_scope == SOURCE_SCOPE_REFERENCE_ONLY
        or source_scope == SOURCE_SCOPE_PROTECTED
        or memory_policy in DOCUMENT_REFERENCE_ONLY_POLICIES
        or memory_policy in DOCUMENT_PERSONAL_MEDICAL_POLICIES
    )


def _candidate_app_key(value: str) -> str:
    text = str(value or "").strip().lower()
    return "unknown" if _is_unknown_app(text) else text


def _is_unknown_app(value: str) -> bool:
    text = str(value or "").strip().lower()
    return text in {"", "unknown", "未知", "none", "null"}


def _memory_summary_fingerprint(value: str) -> str:
    text = _sanitize_text(value).lower()
    text = re.sub(r"\d+", " ", text)
    tokens = re.findall(r"[a-zA-Z][a-zA-Z_-]{2,}|[\u4e00-\u9fff]{2,}", text)
    stop = {
        "the",
        "and",
        "for",
        "with",
        "this",
        "that",
        "into",
        "from",
        "about",
        "当前",
        "这个",
        "内容",
        "页面",
    }
    filtered = [token for token in tokens if token not in stop]
    if filtered:
        return " ".join(filtered[:18])
    return re.sub(r"\W+", " ", text).strip()[:80]


def _summary_token_similarity(left: str, right: str) -> float:
    left_tokens = _summary_token_set(left)
    right_tokens = _summary_token_set(right)
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens.intersection(right_tokens)) / max(1, len(left_tokens.union(right_tokens)))


def _summary_token_set(value: str) -> set[str]:
    text = _sanitize_text(value).lower()
    tokens = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}|[\u4e00-\u9fff]{2,}", text)
    stop = {
        "safe",
        "summary",
        "document",
        "自用",
        "简历",
        "文档",
        "安全",
        "摘要",
        "角色",
        "能力",
        "线索",
        "主题",
        "标签",
        "禁止",
        "写入",
        "原文",
        "联系方式",
    }
    return {token for token in tokens if token not in stop}


def _aggressive_candidate_keep(item: dict) -> bool:
    if item.get("learnability") != "low_signal":
        return False
    reason = str(item.get("reason") or "")
    if reason in {"communication_redacted", "short_or_empty_cleaned_content"}:
        text = f"{item.get('topic') or ''} {item.get('safe_summary') or ''} {item.get('app') or ''}".lower()
        return _contains_any(
            text,
            [
                "简历",
                "resume",
                "cv",
                "面试",
                "岗位",
                "personal ai monitor",
                "监控系统",
                "codex",
                "hermes",
                "obsidian",
                "待办",
                "跟进",
            ],
        )
    return True


def _memory_candidate_limit() -> int:
    raw = os.environ.get("PAM_MEMORY_CANDIDATE_LIMIT", str(DEFAULT_MEMORY_CANDIDATE_LIMIT))
    try:
        return max(1, min(80, int(raw)))
    except ValueError:
        return DEFAULT_MEMORY_CANDIDATE_LIMIT


def _project_tags(item: dict) -> list[str]:
    text = " ".join(
        str(item.get(key) or "")
        for key in ("topic", "safe_summary", "app", "workflow")
    ).lower()
    rules = [
        ("personal-ai-monitor", ["personal ai monitor", "activity_mvp", "监控系统", "隐私过滤"]),
        ("interview", ["interview", "面试", "自我介绍", "star"]),
        ("obsidian", ["obsidian", "markdown", "知识库"]),
        ("wechat-writing", ["公众号", "文章", "标题", "草稿"]),
        ("ai-workflow", ["claude", "chatgpt", "codex", "hermes", "mem0"]),
    ]
    tags = [label for label, hints in rules if any(hint in text for hint in hints)]
    return tags[:5]


def _append_low_signal_reasons(lines: list[str], reasons: list[dict]) -> None:
    lines.extend(["## 低信号原因", ""])
    if not reasons:
        lines.extend(["暂无明显低信号原因。", ""])
        return
    for item in reasons:
        lines.append(f"- {item.get('reason')}: `{item.get('count', 0)}`")
    lines.append("")


def _hour_label(value: str) -> str:
    text = str(value or "")
    first = text.split(" -> ", 1)[0]
    try:
        return parse_time(first).astimezone().replace(minute=0, second=0, microsecond=0).isoformat(timespec="minutes")
    except (TypeError, ValueError):
        return first[:13] or "unknown"


def _is_communication_app(app_name: str) -> bool:
    return _contains_any(str(app_name or "").lower(), COMMUNICATION_APP_HINTS)


def _contains_any(value: str, hints) -> bool:
    text = str(value or "").lower()
    return any(str(hint).lower() in text for hint in hints)


def _clip_text(value: str, limit: int) -> str:
    text = " ".join(_sanitize_text(value).split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def write_timeline_markdown(path: str | Path, markdown: str) -> Path:
    output_path = Path(path).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")
    output_path.chmod(0o600)
    return output_path.resolve()


def _privacy_events(conn: sqlite3.Connection, start: str | None, end: str | None) -> list[dict]:
    where = ["1 = 1"]
    params: list[str] = []
    if start:
        where.append("datetime(COALESCE(ended_at, started_at)) > datetime(?)")
        params.append(start)
    if end:
        where.append("datetime(started_at) < datetime(?)")
        params.append(end)
    rows = conn.execute(
        f"""
        SELECT id, started_at, ended_at, type, reason, app_name, domain, action
        FROM privacy_events
        WHERE {' AND '.join(where)}
        ORDER BY datetime(started_at) ASC
        """,
        params,
    ).fetchall()
    return [sanitize_privacy_event_context(dict(row)) for row in rows]


def _gap_events(conn: sqlite3.Connection, start: str | None, end: str | None) -> list[dict]:
    where = ["1 = 1"]
    params: list[str] = []
    if start:
        where.append("datetime(COALESCE(ended_at, started_at)) > datetime(?)")
        params.append(start)
    if end:
        where.append("datetime(started_at) < datetime(?)")
        params.append(end)
    rows = conn.execute(
        f"""
        SELECT id, started_at, ended_at, reason, display_ids, detail
        FROM gap_events
        WHERE {' AND '.join(where)}
        ORDER BY datetime(started_at) ASC
        """,
        params,
    ).fetchall()
    output = []
    for row in rows:
        item = dict(row)
        item["display_ids"] = _json_list(item.get("display_ids"))
        output.append(item)
    return output


def _duplicate_events(conn: sqlite3.Connection, start: str | None, end: str | None) -> list[dict]:
    where = ["1 = 1"]
    params: list[str] = []
    if start:
        where.append("datetime(COALESCE(ended_at, started_at)) > datetime(?)")
        params.append(start)
    if end:
        where.append("datetime(started_at) < datetime(?)")
        params.append(end)
    rows = conn.execute(
        f"""
        SELECT id, started_at, ended_at, display_id, reason, duplicate_count,
               source_activity_id, note
        FROM duplicate_events
        WHERE {' AND '.join(where)}
        ORDER BY datetime(started_at) ASC
        """,
        params,
    ).fetchall()
    return _merge_duplicate_events([dict(row) for row in rows])


def _merge_duplicate_events(events: list[dict]) -> list[dict]:
    merged: list[dict] = []
    for item in events:
        if not merged:
            merged.append(dict(item))
            continue
        current = merged[-1]
        same_bucket = (
            current.get("display_id") == item.get("display_id")
            and current.get("reason") == item.get("reason")
            and current.get("source_activity_id") == item.get("source_activity_id")
        )
        try:
            gap_seconds = (
                parse_time(str(item.get("started_at")))
                - parse_time(str(current.get("ended_at") or current.get("started_at")))
            ).total_seconds()
        except (TypeError, ValueError):
            gap_seconds = 999999
        if same_bucket and gap_seconds <= 120:
            current["ended_at"] = item.get("ended_at") or item.get("started_at")
            current["duplicate_count"] = int(current.get("duplicate_count") or 1) + int(item.get("duplicate_count") or 1)
            if item.get("note") and item.get("note") != current.get("note"):
                current["note"] = item["note"]
        else:
            merged.append(dict(item))
    return merged


def _append_activity(lines: list[str], index: int, item: dict) -> None:
    cleaned_text = _sanitize_text(item.get("cleaned_text") or "")
    if not cleaned_text:
        cleaned_text = "此条旧记录没有保存清洗后完整内容；原始 OCR 已按隐私规则删除，无法恢复。"
    dedupe_status = str(item.get("dedupe_status") or "unique")
    dedupe_note = _sanitize_text(item.get("dedupe_note") or "")
    lines.extend(
        [
            f"### {index}. 活动 - {_time_label(item)}",
            "",
            f"- App: `{_join_list(item.get('apps')) or '无'}`",
            f"- Domain: `{_join_list(item.get('domains')) or '无'}`",
            f"- Evidence: `{_join_list(item.get('evidence_refs')) or '无'}`",
            f"- Privacy: `{item.get('privacy_level') or 'normal'}`",
            f"- Redaction confidence: `{_confidence(item.get('redaction_confidence'))}`",
            f"- Dedupe: `{dedupe_status}`",
            f"- Activity ID: `{item.get('id') or 'unknown'}`",
            "",
        ]
    )
    if dedupe_status == "partial_duplicate" and dedupe_note:
        lines.extend([f"重复清洗：{dedupe_note}", ""])
    lines.extend(["**清洗后完整内容**", "", cleaned_text, ""])


def _append_unverified_clue(lines: list[str], index: int, item: dict) -> None:
    app_name = _sanitize_text(item.get("app_name") or "")
    dedupe_note = _sanitize_text(item.get("dedupe_note") or "")
    lines.extend(
        [
            f"### {index}. 未验证屏幕/OCR 线索 - {_time_label(item)}",
            "",
            "该观察仅用于定位或后续确认，不能证明任务已保存、发送、发布或完成。",
            "",
            f"- Source: `{_sanitize_text(item.get('source_ref') or '') or 'unknown'}`",
            f"- Verification: `{_sanitize_text(item.get('verification_state') or '') or 'unverified'}`",
            f"- Reason: `{_sanitize_text(item.get('reason_code') or '') or 'screen_observation_only'}`",
            f"- App class: `{_sanitize_text(item.get('app_class') or '') or 'unknown'}`",
            "- Raw access: `false`",
            "",
        ]
    )
    if app_name:
        lines.insert(len(lines) - 1, f"- App: `{app_name}`")
    if dedupe_note:
        lines.insert(len(lines) - 1, f"- Dedupe: `{dedupe_note}`")


def _append_duplicate_event(lines: list[str], index: int, item: dict) -> None:
    count = int(item.get("duplicate_count") or 1)
    note = _sanitize_text(item.get("note") or "")
    if not note:
        note = "与上一条同显示器截屏内容完全重复，不再重复记录内容。"
    lines.extend(
        [
            f"### {index}. 重复清洗 - {_time_label(item)}",
            "",
            f"{note}本段合并 {count} 次重复截屏。",
            "",
            f"- Display: `{_sanitize_text(item.get('display_id') or '') or '无'}`",
            f"- Reason: `{_sanitize_text(item.get('reason') or '') or 'duplicate'}`",
            f"- Duplicate count: `{count}`",
            f"- Event ID: `{item.get('id') or 'unknown'}`",
            "",
        ]
    )


def _append_privacy_event(lines: list[str], index: int, item: dict) -> None:
    reason = _human_privacy_reason(item.get("reason") or item.get("type"))
    lines.extend(
        [
            f"### {index}. 隐私保护 - {_time_label(item)}",
            "",
            f"敏感画面已隐藏内容：{reason}",
            "",
            f"- App: `{_sanitize_text(item.get('app_name') or '') or '无'}`",
            f"- Domain: `{_sanitize_text(item.get('domain') or '') or '无'}`",
            f"- Action: `{_sanitize_text(item.get('action') or '') or 'blocked'}`",
            f"- Event ID: `{item.get('id') or 'unknown'}`",
            "",
        ]
    )


def _append_gap_event(lines: list[str], index: int, item: dict) -> None:
    reason = _human_gap_reason(item.get("reason") or item.get("detail"))
    lines.extend(
        [
            f"### {index}. 未记录缺口 - {_time_label(item)}",
            "",
            reason,
            "",
            f"- Display: `{_join_list(item.get('display_ids')) or '无'}`",
            f"- Event ID: `{item.get('id') or 'unknown'}`",
            "",
        ]
    )


def _time_label(item: dict) -> str:
    started_at = _local_timestamp(item.get("started_at"))
    ended_at = _local_timestamp(item.get("ended_at"))
    return started_at if not ended_at else f"{started_at} -> {ended_at}"


def _local_timestamp(value) -> str:
    if not value:
        return ""
    text = str(value)
    try:
        return parse_time(text).astimezone().isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return _sanitize_text(text)


def _confidence(value) -> str:
    if value is None:
        return "未知"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return _sanitize_text(value)


def _json_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if not value:
        return []
    try:
        import json

        parsed = json.loads(str(value))
    except Exception:
        return [str(value)]
    if isinstance(parsed, list):
        return [str(item) for item in parsed if str(item).strip()]
    return [str(parsed)]


def _human_privacy_reason(value) -> str:
    text = str(value or "")
    mapping = {
        "high_risk_app": "高风险应用",
        "high_risk_domain": "高风险网站",
        "secret_like_content": "疑似密钥或凭证",
        "secure_input": "安全输入场景",
        "sensitive_content": "高敏感内容",
        "personal_medical_content": "个人临床医疗内容已保护",
        "privacy_firewall_blocked": "Privacy Firewall 已阻断",
    }
    return mapping.get(text, _sanitize_text(text.replace("_", " ")) or "Privacy Firewall 已阻断")


def _human_gap_reason(value) -> str:
    text = str(value or "")
    if "review_required" in text or text in {"low_confidence", "uncertain"}:
        return "片段需要复核，P0 阶段不保留内容，只记录缺口。"
    if "privacy_or_review_gate" in text:
        return "隐私保护或复核门槛导致未记录内容。"
    return _sanitize_text(text.replace("_", " ")) or "这个时段没有保留可展示内容。"


def _join_list(value) -> str:
    if not value:
        return ""
    if isinstance(value, list):
        return ", ".join(_sanitize_text(item) for item in value if str(item).strip())
    return _sanitize_text(value)


def _sanitize_text(value) -> str:
    text = str(value)
    text = SECRET_VALUE_RE.sub("[secret_redacted]", text)
    text = LOCAL_PATH_RE.sub("[path_redacted]", text)
    return redact_adult_terms(text).strip()
