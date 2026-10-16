from __future__ import annotations

import json
import sqlite3
import uuid
from collections import Counter

from .attribution_context import attribution_health_for_range
from .document_changes import document_authorization_lock
from .pipeline import utc_now
from .privacy import redact_for_storage
from .timeline_export import cleaned_timeline_export


TARGET_TYPES = {"activity", "privacy_event", "gap_event", "document_signal"}
ISSUE_TYPES = {
    "false_block",
    "false_clear",
    "over_redacted",
    "under_redacted",
    "wrong_workflow",
    "missed_document_signal",
    "duplicate_error",
    "low_signal",
}
EXPECTED_STATUSES = {"clear", "partial_redacted", "blocked", "gap", "duplicate"}


def quality_report(conn: sqlite3.Connection, start: str | None = None, end: str | None = None) -> dict:
    # The report combines multiple public projections.  Keep them under one
    # authorization lease so it cannot mix pre- and post-revocation content.
    with document_authorization_lock(conn, exclusive=False):
        return _quality_report_locked(conn, start, end)


def _quality_report_locked(conn: sqlite3.Connection, start: str | None = None, end: str | None = None) -> dict:
    export = cleaned_timeline_export(conn, start, end)
    quality = export.get("quality_diagnostics") or {}
    digest = export.get("learning_digest") or {}
    learnability = digest.get("learnability_counts") or {}
    memory_candidates = digest.get("memory_candidates") or []
    screen_region_summary = digest.get("screen_region_summary") if isinstance(digest.get("screen_region_summary"), dict) else {}
    memory_write_health = _memory_write_health(digest)
    memory_governance_trend = _memory_governance_trend(conn)
    doc = _document_signal_counts(conn, start, end)
    attribution = _attribution_health(conn, start, end)
    feedback = list_quality_feedback(conn, start, end)
    feedback_counts = Counter(item["issue_type"] for item in feedback)
    candidate_skip_reason = _candidate_skip_reason(memory_candidates, quality, learnability)
    report = {
        "time_range": {"from": start, "to": end},
        "status": _quality_status(quality, doc, attribution, feedback),
        "summary": _quality_summary(quality, doc, memory_candidates, feedback, memory_write_health),
        "cleaning": {
            "activity_count": int(quality.get("activity_count") or 0),
            "normal_clear_count": max(
                0,
                int(quality.get("activity_count") or 0)
                - int(quality.get("partial_redacted_count") or 0)
                - int(quality.get("communication_redaction_count") or 0),
            ),
            "partial_redacted_count": int(quality.get("partial_redacted_count") or 0),
            "communication_redaction_count": int(quality.get("communication_redaction_count") or 0),
            "privacy_event_count": int(quality.get("privacy_event_count") or 0),
            "gap_event_count": int(quality.get("gap_event_count") or 0),
            "duplicate_event_count": int(quality.get("duplicate_event_count") or 0),
            "duplicate_frame_count": int(quality.get("duplicate_frame_count") or 0),
            "short_or_empty_content_count": int(quality.get("short_or_empty_content_count") or 0),
            "near_cleaned_content_cap_count": int(quality.get("near_cleaned_content_cap_count") or 0),
            "short_content_buckets": quality.get("short_content_buckets") or {},
        },
        "document_signals": doc,
        "attribution_health": attribution,
        "learning": {
            "summary": str(digest.get("summary") or ""),
            "learnability_counts": learnability,
            "workflow_summary": digest.get("workflow_summary") or [],
            "screen_region_summary": screen_region_summary,
            "memory_candidate_count": len(memory_candidates),
            "display_candidate_count": len(memory_candidates),
            "exposed_candidate_count": int(memory_write_health.get("exposed_candidate_count") or len(memory_candidates)),
            "total_candidate_count": int(memory_write_health.get("total_candidate_count") or len(memory_candidates)),
            "eligible_candidate_count": int(memory_write_health.get("eligible_candidate_count") or 0),
            "write_input_count": int(memory_write_health.get("write_input_count") or 0),
            "single_evidence_eligible_count": int(memory_write_health.get("single_evidence_eligible_count") or 0),
            "subject_risk_candidate_count": int(memory_write_health.get("subject_risk_candidate_count") or 0),
            "memory_candidate_skip_reason": candidate_skip_reason,
            "memory_write_health": memory_write_health,
            "memory_governance_trend": memory_governance_trend,
            "memory_candidates": memory_candidates[:12],
        },
        "memory_write_health": memory_write_health,
        "memory_governance_trend": memory_governance_trend,
        "feedback": {
            "count": len(feedback),
            "issue_counts": dict(feedback_counts),
            "items": feedback[:50],
        },
        "raw_access": False,
        "cloud_analysis": "off",
    }
    report["recommendations"] = _recommendations(report)
    return report


def render_quality_markdown(report: dict) -> str:
    cleaning = report.get("cleaning") or {}
    doc = report.get("document_signals") or {}
    attribution = report.get("attribution_health") or {}
    learning = report.get("learning") or {}
    write_health = learning.get("memory_write_health") or {}
    screen_region = learning.get("screen_region_summary") or write_health.get("screen_region_distribution") or {}
    feedback = report.get("feedback") or {}
    lines = [
        "# Personal AI Monitor 质量报告",
        "",
        "> 只基于清洗后活动、统计账本和人工反馈元数据；不包含原始截图、原始 OCR、完整路径或模型原始输出。",
        "",
        f"- time_range: `{report.get('time_range', {}).get('from') or 'unbounded'}` to `{report.get('time_range', {}).get('to') or 'unbounded'}`",
        f"- status: `{report.get('status') or 'unknown'}`",
        f"- raw_access: `{str(report.get('raw_access') is True).lower()}`",
        f"- cloud_analysis: `{report.get('cloud_analysis') or 'off'}`",
        "",
        "## 摘要",
        "",
        report.get("summary") or "暂无质量摘要。",
        "",
        "## 清洗统计",
        "",
        f"- 安全活动：`{cleaning.get('activity_count', 0)}` 条，局部清洗 `{cleaning.get('partial_redacted_count', 0)}` 条，聊天类清洗 `{cleaning.get('communication_redaction_count', 0)}` 条。",
        f"- 隐私保护：`{cleaning.get('privacy_event_count', 0)}` 条；缺口 `{cleaning.get('gap_event_count', 0)}` 条。",
        f"- 重复压缩：重复事件 `{cleaning.get('duplicate_event_count', 0)}` 条，合并帧 `{cleaning.get('duplicate_frame_count', 0)}` 次。",
        f"- 低信号：极短内容 `{cleaning.get('short_or_empty_content_count', 0)}` 条，接近长度上限 `{cleaning.get('near_cleaned_content_cap_count', 0)}` 条。",
        "",
        "## 文档信号",
        "",
        f"- DocumentSignal：候选 `{doc.get('document_candidate_count', 0)}` 条，语义匹配 `{doc.get('document_match_count', 0)}` 条，仅元数据 `{doc.get('metadata_only_count', 0)}` 条。",
        f"- 文档诊断：{doc.get('summary') or '暂无'}",
        f"- Unknown 归因：Unknown app `{attribution.get('unknown_app_frame_count', 0)}` 帧，低可信上下文 `{attribution.get('low_confidence_context_count', 0)}` 条，副屏帧 `{attribution.get('secondary_display_frame_count', 0)}` 条。",
        f"- 结构化归因：浏览器扩展 `{attribution.get('extension_browser_signal_count', 0)}` 条，AppleScript 兜底 `{attribution.get('applescript_browser_signal_count', 0)}` 条，Accessibility `{attribution.get('accessibility_signal_count', 0)}` 条。",
        f"- 归因可信度：`{attribution.get('confidence_label', 'unknown')}`；已知 App 比例 `{attribution.get('known_app_ratio', 0)}`，低可信比例 `{attribution.get('low_confidence_ratio', 0)}`。",
        f"- 归因诊断：{attribution.get('summary') or '暂无'}",
        "",
        "## 可学习性",
        "",
        f"- 展示候选：`{learning.get('display_candidate_count', learning.get('memory_candidate_count', 0))}` 条。",
        f"- 全量候选池：`{learning.get('total_candidate_count', 0)}` 条；可写池 `{learning.get('eligible_candidate_count', 0)}` 条；实际写入输入 `{learning.get('write_input_count', 0)}` 条。",
        f"- 无候选原因：`{learning.get('memory_candidate_skip_reason') or ''}`",
        f"- 长期记忆写入健康：{write_health.get('summary') or '暂无'}",
        f"- 学习摘要：{learning.get('summary') or '暂无'}",
        f"- 区域分布：actionability `{(screen_region.get('actionability_counts') or {})}`；region `{(screen_region.get('region_kind_counts') or {})}`。",
        "",
        "## 长期记忆写入审计",
        "",
        f"- 可写池：`{write_health.get('eligible_candidate_count', 0)}` / 全量候选池 `{write_health.get('total_candidate_count', 0)}`。",
        f"- 展示候选：`{write_health.get('display_candidate_count', learning.get('display_candidate_count', 0))}`；实际写入输入：`{write_health.get('write_input_count', 0)}`。",
        f"- 证据分层：confirmed `{write_health.get('confirmed_action_count', 0)}`；context `{write_health.get('context_clue_count', 0)}`；noise/reference `{write_health.get('noise_or_reference_count', 0)}`；Finder/system/reference `{write_health.get('finder_or_system_reference_count', 0)}`。",
        f"- 区域门控影子指标：区域噪声 `{write_health.get('screen_region_noise_count', 0)}`；区域冲突 `{write_health.get('screen_region_conflict_count', 0)}`。",
        f"- 单证据候选：`{write_health.get('single_evidence_candidate_count', 0)}`；单证据可写：`{write_health.get('single_evidence_eligible_count', 0)}`；单证据降级：`{write_health.get('single_evidence_demoted_count', 0)}`。",
        f"- Unknown 可写：`{write_health.get('unknown_eligible_count', 0)}`；弱锚点仅观察：`{write_health.get('weak_anchor_review_only_count', 0)}`。",
        f"- 媒体/文件列表降级：`{write_health.get('media_or_file_listing_ephemeral_count', 0)}`；仅关键词标签：`{write_health.get('keyword_only_tag_count', 0)}`。",
        f"- 第三方主体：`{write_health.get('third_party_subject_count', 0)}`；主体不明：`{write_health.get('ambiguous_subject_count', 0)}`；主体风险合计：`{write_health.get('subject_risk_candidate_count', 0)}`。",
        f"- 可信锚点可写：`{write_health.get('trusted_anchor_eligible_count', 0)}`；重复合并 `{write_health.get('merged_duplicate_count', 0)}` 次。",
        f"- 写入护栏：{write_health.get('write_guard_summary') or 'third_party_subject / ambiguous_subject 不进入 Hermes 写入输入。'}",
        f"- 最近趋势：{(learning.get('memory_governance_trend') or {}).get('summary') or '暂无趋势。'}",
        "",
        "## 人工反馈",
        "",
        f"- 反馈数量：`{feedback.get('count', 0)}` 条。",
    ]
    buckets = cleaning.get("short_content_buckets") or {}
    if buckets:
        lines.insert(
            19,
            (
                f"- 短内容分桶：隐私清洗 `{buckets.get('privacy_redaction_count', 0)}`，"
                f"重复压缩 `{buckets.get('duplicate_compression_count', 0)}`，"
                f"OCR/AX 弱信号 `{buckets.get('ocr_or_ax_weak_signal_count', 0)}`，"
                f"浏览器/系统框架 `{buckets.get('browser_or_system_chrome_count', 0)}`，"
                f"真实低信号 `{buckets.get('true_low_signal_count', 0)}`。"
            ),
        )
    for issue, count in sorted((feedback.get("issue_counts") or {}).items()):
        lines.append(f"- {issue}: `{count}`")
    if not (feedback.get("issue_counts") or {}):
        lines.append("- 暂无人工反馈。")
    lines.extend(["", "## 建议", ""])
    for item in report.get("recommendations") or []:
        lines.append(f"- {item}")
    if not report.get("recommendations"):
        lines.append("- 暂无需要处理的质量建议。")
    return "\n".join(lines).rstrip() + "\n"


def add_quality_feedback(
    conn: sqlite3.Connection,
    *,
    target_type: str,
    target_id: str,
    issue_type: str,
    expected_status: str,
    note: str = "",
) -> dict:
    target_type = _require_choice(target_type, TARGET_TYPES, "target_type")
    issue_type = _require_choice(issue_type, ISSUE_TYPES, "issue_type")
    expected_status = _require_choice(expected_status, EXPECTED_STATUSES, "expected_status")
    target = _target_metadata(conn, target_type, target_id)
    feedback_id = f"qfb_{uuid.uuid4().hex}"
    created_at = utc_now()
    note_redacted = redact_for_storage(note, max_len=360)
    conn.execute(
        """
        INSERT INTO quality_feedback(
          id, created_at, target_type, target_id, target_started_at, target_ended_at,
          issue_type, expected_status, note_redacted, deletion_group_id
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            feedback_id,
            created_at,
            target_type,
            str(target_id),
            target["started_at"],
            target["ended_at"],
            issue_type,
            expected_status,
            note_redacted,
            target["deletion_group_id"],
        ),
    )
    conn.commit()
    return {
        "id": feedback_id,
        "created_at": created_at,
        "target_type": target_type,
        "target_id": str(target_id),
        "issue_type": issue_type,
        "expected_status": expected_status,
        "note_redacted": note_redacted,
        "raw_access": False,
    }


def list_quality_feedback(conn: sqlite3.Connection, start: str | None = None, end: str | None = None) -> list[dict]:
    where = ["1 = 1"]
    params: list[str] = []
    if start:
        where.append("datetime(COALESCE(target_ended_at, target_started_at, created_at)) > datetime(?)")
        params.append(start)
    if end:
        where.append("datetime(COALESCE(target_started_at, target_ended_at, created_at)) < datetime(?)")
        params.append(end)
    rows = conn.execute(
        f"""
        SELECT id, created_at, target_type, target_id, target_started_at, target_ended_at,
               issue_type, expected_status, note_redacted
        FROM quality_feedback
        WHERE {' AND '.join(where)}
        ORDER BY datetime(created_at) DESC
        LIMIT 200
        """,
        params,
    ).fetchall()
    return [
        {
            "id": row["id"],
            "created_at": row["created_at"],
            "target_type": row["target_type"],
            "target_id": row["target_id"],
            "target_started_at": row["target_started_at"] or "",
            "target_ended_at": row["target_ended_at"] or "",
            "issue_type": row["issue_type"],
            "expected_status": row["expected_status"],
            "note_redacted": row["note_redacted"] or "",
        }
        for row in rows
    ]


def delete_quality_feedback_for_range(
    conn: sqlite3.Connection,
    start: str,
    end: str,
    *,
    deletion_group_ids: list[str] | None = None,
    target_ids: list[str] | None = None,
) -> int:
    clauses = [
        """
        (
          datetime(COALESCE(target_ended_at, target_started_at, created_at)) > datetime(?)
          AND datetime(COALESCE(target_started_at, target_ended_at, created_at)) < datetime(?)
        )
        """
    ]
    params: list[str] = [start, end]
    if deletion_group_ids:
        placeholders = ",".join("?" for _ in deletion_group_ids)
        clauses.append(f"deletion_group_id IN ({placeholders})")
        params.extend(deletion_group_ids)
    if target_ids:
        placeholders = ",".join("?" for _ in target_ids)
        clauses.append(f"target_id IN ({placeholders})")
        params.extend(target_ids)
    row = conn.execute(
        f"SELECT COUNT(*) AS count FROM quality_feedback WHERE {' OR '.join(clauses)}",
        params,
    ).fetchone()
    conn.execute(
        f"DELETE FROM quality_feedback WHERE {' OR '.join(clauses)}",
        params,
    )
    return int(row["count"] or 0)


def _target_metadata(conn: sqlite3.Connection, target_type: str, target_id: str) -> dict:
    target_id = str(target_id)
    if target_type == "activity":
        row = conn.execute(
            "SELECT started_at, ended_at, deletion_group_id FROM extracted_activities WHERE id = ?",
            (target_id,),
        ).fetchone()
    elif target_type == "privacy_event":
        row = conn.execute(
            "SELECT started_at, ended_at, deletion_group_id FROM privacy_events WHERE id = ?",
            (target_id,),
        ).fetchone()
    elif target_type == "gap_event":
        row = conn.execute(
            "SELECT started_at, ended_at, deletion_group_id FROM gap_events WHERE id = ?",
            (target_id,),
        ).fetchone()
    elif target_type == "document_signal":
        row = conn.execute(
            "SELECT captured_at AS started_at, captured_at AS ended_at, deletion_group_id FROM document_signals WHERE id = ?",
            (target_id,),
        ).fetchone()
    elif target_type == "vlm_job":
        row = conn.execute(
            "SELECT captured_at AS started_at, captured_at AS ended_at, '' AS deletion_group_id FROM vlm_pending_jobs WHERE id = ?",
            (target_id,),
        ).fetchone()
    else:
        row = None
    if not row:
        raise ValueError(f"quality feedback target not found: {target_type}:{target_id}")
    return {
        "started_at": row["started_at"] or "",
        "ended_at": row["ended_at"] or row["started_at"] or "",
        "deletion_group_id": row["deletion_group_id"] or "",
    }


def _document_signal_counts(conn: sqlite3.Connection, start: str | None, end: str | None) -> dict:
    where = ["1 = 1"]
    params: list[str] = []
    if start:
        where.append("datetime(captured_at) >= datetime(?)")
        params.append(start)
    if end:
        where.append("datetime(captured_at) < datetime(?)")
        params.append(end)
    row = conn.execute(
        f"""
        SELECT
          COUNT(*) AS candidate_count,
          SUM(CASE WHEN COALESCE(semantic_match, 1) = 1 THEN 1 ELSE 0 END) AS match_count,
          SUM(CASE WHEN COALESCE(semantic_match, 1) = 0 OR match_status = 'metadata_only' THEN 1 ELSE 0 END) AS metadata_count
        FROM document_signals
        WHERE {' AND '.join(where)}
        """,
        params,
    ).fetchone()
    candidate_count = int(row["candidate_count"] or 0)
    match_count = int(row["match_count"] or 0)
    metadata_count = int(row["metadata_count"] or 0)
    if match_count:
        status = "matched"
        summary = f"DocumentSignal 有 {match_count} 条语义匹配。"
    elif metadata_count:
        status = "metadata_only"
        summary = f"DocumentSignal 只有 {metadata_count} 条元数据。"
    elif candidate_count:
        status = "no_semantic_match"
        summary = f"DocumentSignal 有 {candidate_count} 条候选，但没有语义匹配。"
    else:
        status = "none"
        summary = "DocumentSignal 当前时间段没有候选。"
    return {
        "document_candidate_count": candidate_count,
        "document_match_count": match_count,
        "metadata_only_count": metadata_count,
        "status": status,
        "summary": summary,
    }


def _attribution_health(conn: sqlite3.Connection, start: str | None, end: str | None) -> dict:
    return attribution_health_for_range(conn, start, end)

    signal_where = ["1 = 1"]
    context_where = ["1 = 1"]
    signal_params: list[str] = []
    context_params: list[str] = []
    if start:
        signal_where.append("datetime(captured_at) >= datetime(?)")
        signal_params.append(start)
        context_where.append("datetime(captured_at) >= datetime(?)")
        context_params.append(start)
    if end:
        signal_where.append("datetime(captured_at) < datetime(?)")
        signal_params.append(end)
        context_where.append("datetime(captured_at) < datetime(?)")
        context_params.append(end)
    signal_row = conn.execute(
        f"""
        SELECT
          COUNT(*) AS frame_count,
          SUM(CASE WHEN COALESCE(app_name, '') = '' OR LOWER(app_name) = 'unknown' THEN 1 ELSE 0 END) AS unknown_app_count,
          SUM(CASE WHEN COALESCE(domain, '') != '' THEN 1 ELSE 0 END) AS domain_attributed_count,
          SUM(CASE WHEN COALESCE(display_id, '') NOT IN ('', 'main') THEN 1 ELSE 0 END) AS secondary_display_count,
          COUNT(DISTINCT COALESCE(display_id, '')) AS display_count,
          SUM(CASE WHEN privacy_flags LIKE '%accessibility_text_used%' THEN 1 ELSE 0 END) AS accessibility_count
        FROM frame_signals
        WHERE {' AND '.join(signal_where)}
        """,
        signal_params,
    ).fetchone()
    context_row = conn.execute(
        f"""
        SELECT
          COUNT(*) AS context_count,
          SUM(CASE WHEN COALESCE(context_confidence, 0.0) < 0.45 THEN 1 ELSE 0 END) AS low_confidence_count,
          SUM(CASE
            WHEN COALESCE(source, '') = 'browser_extension'
              OR url_contexts LIKE '%chromium_extension_native_host%'
            THEN 1 ELSE 0 END) AS browser_extension_count,
          SUM(CASE
            WHEN url_contexts LIKE '%applescript_dia%'
              OR url_contexts LIKE '%applescript_chrome%'
              OR url_contexts LIKE '%applescript_arc%'
              OR url_contexts LIKE '%applescript_safari%'
              OR COALESCE(source, '') LIKE '%applescript%'
            THEN 1 ELSE 0 END) AS applescript_count
        FROM display_frame_contexts
        WHERE {' AND '.join(context_where)}
        """,
        context_params,
    ).fetchone()
    display_rows = conn.execute(
        f"""
        SELECT
          CASE
            WHEN COALESCE(display_id, '') = '' THEN 'unknown_display'
            WHEN display_id = 'main' THEN 'main_display'
            ELSE 'secondary_display'
          END AS display_class,
          COUNT(*) AS count
        FROM frame_signals
        WHERE {' AND '.join(signal_where)}
        GROUP BY display_class
        ORDER BY count DESC, display_class ASC
        """,
        signal_params,
    ).fetchall()
    frame_count = int(signal_row["frame_count"] or 0)
    unknown_count = int(signal_row["unknown_app_count"] or 0)
    known_app_count = max(0, frame_count - unknown_count)
    low_confidence = int(context_row["low_confidence_count"] or 0)
    context_count = int(context_row["context_count"] or 0)
    secondary_count = int(signal_row["secondary_display_count"] or 0)
    browser_extension = int(context_row["browser_extension_count"] or 0)
    applescript_count = int(context_row["applescript_count"] or 0)
    fallback_context = max(0, context_count - browser_extension)
    accessibility_count = int(signal_row["accessibility_count"] or 0)
    known_app_ratio = round(known_app_count / frame_count, 3) if frame_count else 0.0
    low_confidence_ratio = round(low_confidence / context_count, 3) if context_count else 0.0
    source_buckets = [
        {"source_class": "known_app_frame", "count": known_app_count},
        {"source_class": "unknown_app_frame", "count": unknown_count},
        {"source_class": "browser_extension_context", "count": browser_extension},
        {"source_class": "applescript_fallback_context", "count": applescript_count},
        {"source_class": "other_fallback_context", "count": max(0, fallback_context - applescript_count)},
        {"source_class": "accessibility_text_used", "count": accessibility_count},
    ]
    display_buckets = [
        {"display_class": row["display_class"], "count": int(row["count"] or 0)}
        for row in display_rows
    ]
    if frame_count == 0:
        status = "no_frames"
        summary = "当前时间段没有 frame signal；不要据此推断没有活动。"
        confidence_label = "none"
    elif unknown_count == 0 and low_confidence == 0:
        status = "ok"
        confidence_label = "high"
        summary = "归因健康正常；未发现 Unknown app 或低可信上下文聚集。"
    elif unknown_count > max(10, frame_count // 3) or low_confidence > max(10, int(context_row["context_count"] or 0) // 3):
        status = "watch"
        confidence_label = "low"
        summary = (
            "Unknown app 或低可信上下文比例偏高，建议检查浏览器扩展、辅助功能权限和副屏归因；"
            "副屏不会继承主屏上下文。"
        )
    else:
        status = "observe"
        confidence_label = "medium"
        summary = "存在少量 Unknown app 或低可信上下文，可继续观察；副屏不会继承主屏上下文。"
    return {
        "status": status,
        "summary": summary,
        "confidence_label": confidence_label,
        "known_app_ratio": known_app_ratio,
        "low_confidence_ratio": low_confidence_ratio,
        "frame_signal_count": frame_count,
        "unknown_app_frame_count": unknown_count,
        "known_app_frame_count": known_app_count,
        "domain_attributed_frame_count": int(signal_row["domain_attributed_count"] or 0),
        "display_count": int(signal_row["display_count"] or 0),
        "secondary_display_frame_count": secondary_count,
        "context_count": context_count,
        "low_confidence_context_count": low_confidence,
        "browser_extension_context_count": browser_extension,
        "extension_browser_signal_count": browser_extension,
        "applescript_browser_signal_count": applescript_count,
        "accessibility_signal_count": accessibility_count,
        "fallback_context_count": fallback_context,
        "source_buckets": [item for item in source_buckets if item["count"] > 0],
        "display_buckets": display_buckets,
        "cross_display_inheritance": "disabled",
    }


def _candidate_skip_reason(memory_candidates: list[dict], quality: dict, learnability: dict) -> str:
    if memory_candidates:
        return ""
    useful = int(learnability.get("useful") or 0)
    if useful <= 0:
        return "no_useful_learning_items"
    if int(quality.get("short_or_empty_content_count") or 0) >= useful:
        return "low_signal_or_short_content"
    return "no_long_term_memory_candidates"


def _memory_write_health(digest: dict) -> dict:
    quality = digest.get("memory_candidate_quality") if isinstance(digest.get("memory_candidate_quality"), dict) else {}
    candidates = digest.get("memory_candidates") if isinstance(digest.get("memory_candidates"), list) else []
    if not quality:
        eligible = [item for item in candidates if str(item.get("write_eligibility") or "eligible") == "eligible"]
        review = [item for item in candidates if str(item.get("write_eligibility") or "") == "review_only"]
        ephemeral = [item for item in candidates if str(item.get("write_eligibility") or "") == "ephemeral"]
        quality = {
            "total_candidate_count": len(candidates),
            "exposed_candidate_count": len(candidates),
            "eligible_candidate_count": len(eligible),
            "review_only_candidate_count": len(review),
            "ephemeral_candidate_count": len(ephemeral),
            "confirmed_action_count": sum(1 for item in candidates if str(item.get("evidence_layer") or "") == "confirmed_action"),
            "context_clue_count": sum(1 for item in candidates if str(item.get("evidence_layer") or "") == "context_clue"),
            "noise_or_reference_count": sum(1 for item in candidates if str(item.get("evidence_layer") or "") == "noise_or_reference"),
            "finder_or_system_reference_count": sum(
                1
                for item in candidates
                if {"system_ui_context", "read_only_context_app"}.intersection(set(item.get("quality_flags") or []))
            ),
            "write_input_count": len(eligible),
            "single_evidence_candidate_count": sum(1 for item in candidates if int(item.get("evidence_count") or 1) <= 1),
            "single_evidence_eligible_count": sum(1 for item in eligible if int(item.get("evidence_count") or 1) <= 1),
            "unknown_eligible_count": sum(1 for item in eligible if "unknown_app" in (item.get("quality_flags") or [])),
            "merged_duplicate_count": sum(int(item.get("merged_duplicate_count") or 0) for item in candidates),
            "single_evidence_demoted_count": sum(1 for item in candidates if "single_evidence_demoted" in (item.get("quality_flags") or [])),
            "media_or_file_listing_ephemeral_count": sum(
                1
                for item in ephemeral
                if {"media_player_context", "file_listing_context"}.intersection(set(item.get("quality_flags") or []))
            ),
            "keyword_only_tag_count": sum(1 for item in candidates if "keyword_only_project_tag" in (item.get("quality_flags") or [])),
            "third_party_subject_count": sum(1 for item in candidates if str(item.get("subject_scope") or "") == "third_party_subject"),
            "ambiguous_subject_count": sum(1 for item in candidates if str(item.get("subject_scope") or "") == "ambiguous_subject"),
            "subject_risk_candidate_count": sum(
                1 for item in candidates if str(item.get("subject_scope") or "") in {"third_party_subject", "ambiguous_subject"}
            ),
            "trusted_anchor_eligible_count": sum(
                1 for item in eligible if str(item.get("anchor_type") or "") in {"document_signal", "trusted_app_context"}
            ),
            "weak_anchor_review_only_count": sum(
                1 for item in review if str(item.get("anchor_type") or "weak_or_no_anchor") in {"keyword_only", "weak_or_no_anchor"}
            ),
            "screen_region_distribution": {},
            "screen_region_noise_count": sum(1 for item in candidates if "screen_region_noise" in (item.get("quality_flags") or [])),
            "screen_region_conflict_count": sum(1 for item in candidates if "screen_region_conflict" in (item.get("quality_flags") or [])),
        }
    total = int(quality.get("total_candidate_count") or 0)
    eligible_count = int(quality.get("eligible_candidate_count") or 0)
    review_count = int(quality.get("review_only_candidate_count") or 0)
    ephemeral_count = int(quality.get("ephemeral_candidate_count") or 0)
    ratio = float(quality.get("eligible_ratio") or (round(eligible_count / total, 3) if total else 0.0))
    exposed_count = int(quality.get("exposed_candidate_count") or len(candidates))
    write_input_count = int(quality.get("write_input_count") or eligible_count)
    single_evidence_eligible_count = int(quality.get("single_evidence_eligible_count") or 0)
    third_party_subject_count = int(quality.get("third_party_subject_count") or 0)
    ambiguous_subject_count = int(quality.get("ambiguous_subject_count") or 0)
    subject_risk_candidate_count = int(quality.get("subject_risk_candidate_count") or 0)
    write_guard_summary = (
        f"元数据/清洗摘要级诊断：单证据可写 {single_evidence_eligible_count} 条；"
        f"主体风险候选 {subject_risk_candidate_count} 条，其中 third_party_subject {third_party_subject_count} 条、"
        f"ambiguous_subject {ambiguous_subject_count} 条；third_party_subject / ambiguous_subject 不进入 Hermes 写入输入。"
    )
    summary = quality.get("summary") or (
        f"长期记忆全量候选池 {total} 条：展示候选 {exposed_count} 条、可写池 {eligible_count} 条、"
        f"实际写入输入 {write_input_count} 条、仅观察 {review_count} 条、临时降级 {ephemeral_count} 条。"
    )
    return {
        "total_candidate_count": total,
        "exposed_candidate_count": exposed_count,
        "display_candidate_count": len(candidates),
        "eligible_candidate_count": eligible_count,
        "write_input_count": write_input_count,
        "review_only_candidate_count": review_count,
        "ephemeral_candidate_count": ephemeral_count,
        "confirmed_action_count": int(quality.get("confirmed_action_count") or 0),
        "context_clue_count": int(quality.get("context_clue_count") or 0),
        "noise_or_reference_count": int(quality.get("noise_or_reference_count") or 0),
        "finder_or_system_reference_count": int(quality.get("finder_or_system_reference_count") or 0),
        "eligible_ratio": ratio,
        "single_evidence_candidate_count": int(quality.get("single_evidence_candidate_count") or 0),
        "single_evidence_eligible_count": single_evidence_eligible_count,
        "unknown_eligible_count": int(quality.get("unknown_eligible_count") or 0),
        "merged_duplicate_count": int(quality.get("merged_duplicate_count") or 0),
        "high_confidence_single_evidence_count": int(quality.get("high_confidence_single_evidence_count") or 0),
        "single_evidence_demoted_count": int(quality.get("single_evidence_demoted_count") or 0),
        "media_or_file_listing_ephemeral_count": int(quality.get("media_or_file_listing_ephemeral_count") or 0),
        "screen_region_distribution": quality.get("screen_region_distribution") if isinstance(quality.get("screen_region_distribution"), dict) else {},
        "screen_region_noise_count": int(quality.get("screen_region_noise_count") or 0),
        "screen_region_conflict_count": int(quality.get("screen_region_conflict_count") or 0),
        "keyword_only_tag_count": int(quality.get("keyword_only_tag_count") or 0),
        "third_party_subject_count": third_party_subject_count,
        "ambiguous_subject_count": ambiguous_subject_count,
        "subject_risk_candidate_count": subject_risk_candidate_count,
        "trusted_anchor_eligible_count": int(quality.get("trusted_anchor_eligible_count") or 0),
        "weak_anchor_review_only_count": int(quality.get("weak_anchor_review_only_count") or 0),
        "write_guard_summary": write_guard_summary,
        "summary": str(summary),
    }


def _memory_governance_trend(conn: sqlite3.Connection, *, limit: int = 6) -> dict:
    rows = conn.execute(
        """
        SELECT hour_key, status, candidate_count, eligible_candidate_count,
               review_only_candidate_count, ephemeral_candidate_count,
               confirmed_action_count, context_clue_count, noise_or_reference_count,
               single_evidence_eligible_count, subject_risk_candidate_count,
               single_evidence_demoted_count, media_or_file_listing_ephemeral_count,
               memories_written_count,
               COALESCE(NULLIF(completed_at, ''), NULLIF(triggered_at, ''), NULLIF(generated_at, ''), NULLIF(updated_at, '')) AS event_at
        FROM ha_automation_jobs
        ORDER BY datetime(event_at) DESC, datetime(updated_at) DESC, rowid DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    totals = {
        "candidate_count": 0,
        "eligible_candidate_count": 0,
        "review_only_candidate_count": 0,
        "ephemeral_candidate_count": 0,
        "confirmed_action_count": 0,
        "context_clue_count": 0,
        "noise_or_reference_count": 0,
        "single_evidence_eligible_count": 0,
        "subject_risk_candidate_count": 0,
        "single_evidence_demoted_count": 0,
        "media_or_file_listing_ephemeral_count": 0,
        "memories_written_count": 0,
    }
    items: list[dict] = []
    for row in rows:
        item = {
            "hour_key": row["hour_key"] or "",
            "event_at": row["event_at"] or "",
            "status": row["status"] or "unknown",
            "candidate_count": int(row["candidate_count"] or 0),
            "eligible_candidate_count": int(row["eligible_candidate_count"] or 0),
            "review_only_candidate_count": int(row["review_only_candidate_count"] or 0),
            "ephemeral_candidate_count": int(row["ephemeral_candidate_count"] or 0),
            "confirmed_action_count": int(row["confirmed_action_count"] or 0),
            "context_clue_count": int(row["context_clue_count"] or 0),
            "noise_or_reference_count": int(row["noise_or_reference_count"] or 0),
            "single_evidence_eligible_count": int(row["single_evidence_eligible_count"] or 0),
            "subject_risk_candidate_count": int(row["subject_risk_candidate_count"] or 0),
            "single_evidence_demoted_count": int(row["single_evidence_demoted_count"] or 0),
            "media_or_file_listing_ephemeral_count": int(row["media_or_file_listing_ephemeral_count"] or 0),
            "memories_written_count": int(row["memories_written_count"] or 0),
        }
        for key in totals:
            totals[key] += int(item[key])
        items.append(item)
    if items:
        summary = (
            f"最近 {len(items)} 次 HA：候选 {totals['candidate_count']}，可写 {totals['eligible_candidate_count']}，"
            f"review-only {totals['review_only_candidate_count']}，ephemeral {totals['ephemeral_candidate_count']}，"
            f"confirmed/context/noise {totals['confirmed_action_count']}/{totals['context_clue_count']}/{totals['noise_or_reference_count']}，"
            f"主体风险 {totals['subject_risk_candidate_count']}，单证据降级 {totals['single_evidence_demoted_count']}。"
        )
    else:
        summary = "还没有可用于趋势统计的 HA 记忆治理记录。"
    return {
        "window_job_count": len(items),
        "items": items,
        "totals": totals,
        "latest": items[0] if items else {},
        "summary": summary,
        "raw_access": False,
    }


def _quality_status(quality: dict, doc: dict, attribution: dict, feedback: list[dict]) -> str:
    if feedback:
        return "needs_review"
    if int(quality.get("gap_event_count") or 0) > 0:
        return "watch"
    if doc.get("status") in {"none", "no_semantic_match"}:
        return "watch"
    if attribution.get("status") == "watch":
        return "watch"
    return "ok"


def _quality_summary(quality: dict, doc: dict, memory_candidates: list[dict], feedback: list[dict], memory_write_health: dict | None = None) -> str:
    write_health = memory_write_health or {}
    display_count = int(write_health.get("display_candidate_count") or len(memory_candidates))
    total_count = int(write_health.get("total_candidate_count") or display_count)
    eligible_count = int(write_health.get("eligible_candidate_count") or 0)
    return (
        f"活动 {int(quality.get('activity_count') or 0)} 条，局部清洗 {int(quality.get('partial_redacted_count') or 0)} 条，"
        f"隐私保护 {int(quality.get('privacy_event_count') or 0)} 条，缺口 {int(quality.get('gap_event_count') or 0)} 条；"
        f"DocumentSignal 匹配 {int(doc.get('document_match_count') or 0)} 条；"
        f"展示候选 {display_count} 条，全量候选池 {total_count} 条，可写池 {eligible_count} 条，"
        f"人工反馈 {len(feedback)} 条。"
    )


def _recommendations(report: dict) -> list[str]:
    cleaning = report.get("cleaning") or {}
    doc = report.get("document_signals") or {}
    learning = report.get("learning") or {}
    attribution = report.get("attribution_health") or {}
    feedback = report.get("feedback") or {}
    output: list[str] = []
    if int(feedback.get("count") or 0):
        output.append("优先查看人工反馈，确认是否需要调整隐私规则、文档匹配或工作流分类。")
    if doc.get("status") in {"none", "no_semantic_match"}:
        output.append("DocumentSignal 没有稳定语义匹配，建议检查前台文档权限、窗口标题和允许根目录。")
    if int(learning.get("memory_candidate_count") or 0) == 0 and int(cleaning.get("activity_count") or 0) > 0:
        output.append("当前有活动但没有长期记忆候选，建议检查学习摘要是否过短或主题标签不足。")
    if attribution.get("status") == "watch":
        output.append("Unknown 归因或低可信上下文偏高，优先检查浏览器扩展、辅助功能权限和副屏归因，不要把主屏上下文继承到副屏。")
    write_health = learning.get("memory_write_health") or {}
    total_candidates = int(write_health.get("total_candidate_count") or 0)
    eligible_candidates = int(write_health.get("eligible_candidate_count") or 0)
    if total_candidates > 0 and eligible_candidates == 0:
        output.append("当前有候选但没有可写入长期记忆的 eligible 项，说明质量门槛正在拦截弱候选。")
    elif int(write_health.get("single_evidence_eligible_count") or 0) > max(3, eligible_candidates // 2):
        output.append("可写候选里单证据比例偏高，建议继续观察是否需要更强的多证据门槛。")
    if int(write_health.get("single_evidence_demoted_count") or 0) > 0:
        output.append("已有单证据候选被降级为观察或诊断，建议用下一轮 Hermes 写入结果确认是否还需继续收紧。")
    if int(write_health.get("media_or_file_listing_ephemeral_count") or 0) > 0:
        output.append("媒体播放器或文件列表噪声已被降级，不应进入 Hermes 长期记忆写入输入。")
    if int(write_health.get("screen_region_conflict_count") or 0) > 0:
        output.append("存在区域规则与 输入信号冲突，先在 Review 中观察，不要立即提升为硬门槛。")
    if int(cleaning.get("short_or_empty_content_count") or 0) > 0:
        output.append("存在极短清洗内容，可能来自 OCR 弱信号、过度清洗或重复压缩。")
    return output


def _require_choice(value: str, allowed: set[str], name: str) -> str:
    text = str(value or "").strip()
    if text not in allowed:
        raise ValueError(f"unsupported {name}: {value}")
    return text
