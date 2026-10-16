from __future__ import annotations

import json
import sqlite3
from collections import Counter
from typing import Any


DISPLAY_MAIN = "main_display"
DISPLAY_SECONDARY = "secondary_display"
DISPLAY_UNKNOWN = "unknown_display"

REASON_LABELS = {
    "main_display_frontmost": "主屏前台窗口",
    "main_display_window_fallback": "主屏窗口安全回退",
    "display_window_match": "窗口匹配成功",
    "display_window_unmatched": "窗口无法可信匹配",
    "display_window_list_empty": "窗口列表为空",
    "browser_signal_display_match": "副屏浏览器信号匹配",
    "browser_signal_missing": "缺少浏览器信号",
    "browser_signal_ambiguous": "浏览器信号不唯一",
    "browser_signal_risky": "浏览器信号含隐私风险",
    "coverage_gap": "采集覆盖缺口",
    "low_confidence": "低置信度",
    "privacy_protected": "隐私保护",
    "unknown_app": "Unknown App",
}


def safe_reason_codes(values: Any) -> list[str]:
    if not isinstance(values, list):
        values = [values]
    output: list[str] = []
    for value in values:
        token = safe_token(value)
        if token and token not in output:
            output.append(token)
    return output[:10]


def safe_token(value: Any) -> str:
    return "".join(ch if ch.isalnum() or ch in {"_", "-", ".", "+"} else "_" for ch in str(value or "").lower())[:80].strip("_")


def parse_json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def display_attribution_from_app_contexts(value: Any) -> dict[str, Any]:
    contexts = parse_json_dict(value)
    attribution = contexts.get("display_attribution")
    return attribution if isinstance(attribution, dict) else {}


def attribution_reason_codes(*, source: str = "", app_contexts: Any = None, attribution: dict[str, Any] | None = None) -> list[str]:
    attr = attribution if isinstance(attribution, dict) else display_attribution_from_app_contexts(app_contexts)
    reasons = safe_reason_codes(attr.get("reason_codes") or [])
    source_token = safe_token(attr.get("source") or source)
    if "frontmost" in source_token:
        reasons.append("main_display_frontmost")
    if "display_window_match" in source_token:
        reasons.append("display_window_match")
    if "display_window_unmatched" in source_token:
        reasons.append("display_window_unmatched")
    if "display_window_list_empty" in source_token:
        reasons.append("display_window_list_empty")
    return safe_reason_codes(reasons)


def display_class_from_attribution(display_id: str, *, source: str = "", app_contexts: Any = None, attribution: dict[str, Any] | None = None) -> str:
    display = str(display_id or "").strip().lower()
    reasons = set(attribution_reason_codes(source=source, app_contexts=app_contexts, attribution=attribution))
    source_token = safe_token((attribution or {}).get("source") or source if isinstance(attribution, dict) else source)
    if "main_display_frontmost" in reasons or "frontmost" in source_token:
        return DISPLAY_MAIN
    if reasons & {"display_window_match", "display_window_unmatched", "display_window_list_empty"} or "display_window" in source_token:
        return DISPLAY_SECONDARY
    if display in {"main", "active-display"}:
        return DISPLAY_MAIN
    if display and not display.startswith("sck-display"):
        return DISPLAY_SECONDARY
    return DISPLAY_UNKNOWN


def display_context_payload(display_id: str, *, source: str = "", attribution: dict[str, Any] | None = None) -> dict[str, Any]:
    attr = attribution if isinstance(attribution, dict) else {}
    reasons = attribution_reason_codes(source=source, attribution=attr)
    return {
        "display_class": display_class_from_attribution(display_id, source=source, attribution=attr),
        "attribution_source": safe_token(attr.get("source") or source) or "unknown",
        "attribution_reason_codes": reasons,
    }


def reason_count_rows(counter: Counter[str], *, limit: int = 8) -> list[dict[str, Any]]:
    return [
        {"reason": reason, "label": REASON_LABELS.get(reason, reason), "count": int(count)}
        for reason, count in counter.most_common(limit)
    ]


def attribution_health_for_range(conn: sqlite3.Connection, start: str | None = None, end: str | None = None) -> dict[str, Any]:
    frame_where = []
    frame_params: list[str] = []
    context_where = []
    context_params: list[str] = []
    if start:
        frame_where.append("datetime(fs.captured_at) >= datetime(?)")
        frame_params.append(start)
        context_where.append("datetime(captured_at) >= datetime(?)")
        context_params.append(start)
    if end:
        frame_where.append("datetime(fs.captured_at) < datetime(?)")
        frame_params.append(end)
        context_where.append("datetime(captured_at) < datetime(?)")
        context_params.append(end)
    frame_clause = f"WHERE {' AND '.join(frame_where)}" if frame_where else ""
    context_clause = f"WHERE {' AND '.join(context_where)}" if context_where else ""
    frame_rows = conn.execute(
        f"""
        SELECT fs.display_id, fs.app_name, fs.domain, fs.privacy_flags,
               dfc.app_contexts, dfc.source AS context_source, dfc.context_confidence,
               dfc.url_contexts
        FROM frame_signals fs
        LEFT JOIN display_frame_contexts dfc
          ON dfc.session_id = fs.session_id
         AND dfc.segment_id = fs.segment_id
         AND dfc.display_id = fs.display_id
         AND dfc.captured_at = fs.captured_at
        {frame_clause}
        ORDER BY fs.captured_at ASC, fs.display_id ASC
        """,
        frame_params,
    ).fetchall()
    context_rows = conn.execute(
        f"""
        SELECT display_id, app_contexts, source, context_confidence, url_contexts, privacy_flags
        FROM display_frame_contexts
        {context_clause}
        """,
        context_params,
    ).fetchall()
    frame_count = len(frame_rows)
    distinct_displays = {str(row["display_id"] or "").strip() for row in frame_rows if str(row["display_id"] or "").strip()}
    unknown_count = 0
    domain_count = 0
    accessibility_count = 0
    frame_display_counts: Counter[str] = Counter()
    issue_reasons: Counter[str] = Counter()
    for row in frame_rows:
        app = str(row["app_name"] or "").strip().lower()
        is_unknown = app in {"", "unknown", "未知"}
        if is_unknown:
            unknown_count += 1
        if str(row["domain"] or "").strip():
            domain_count += 1
        flags = str(row["privacy_flags"] or "")
        if "accessibility_text_used" in flags:
            accessibility_count += 1
        display_class = display_class_from_attribution(
            str(row["display_id"] or ""),
            source=str(row["context_source"] or ""),
            app_contexts=row["app_contexts"],
        )
        frame_display_counts[display_class] += 1
        reasons = attribution_reason_codes(source=str(row["context_source"] or ""), app_contexts=row["app_contexts"])
        if is_unknown:
            issue_reasons["unknown_app"] += 1
        if row["context_confidence"] is not None and float(row["context_confidence"] or 0.0) < 0.45:
            issue_reasons["low_confidence"] += 1
        for reason in reasons:
            if reason not in {"main_display_frontmost", "display_window_match"}:
                issue_reasons[reason] += 1
    context_count = len(context_rows)
    low_confidence = 0
    browser_extension = 0
    applescript_count = 0
    context_display_counts: Counter[str] = Counter()
    for row in context_rows:
        confidence = float(row["context_confidence"] or 0.0)
        if confidence < 0.45:
            low_confidence += 1
        source = str(row["source"] or "")
        url_contexts = str(row["url_contexts"] or "")
        if source == "browser_extension" or "chromium_extension_native_host" in url_contexts:
            browser_extension += 1
        if "applescript_dia" in url_contexts or "applescript_chrome" in url_contexts or "applescript_arc" in url_contexts or "applescript_safari" in url_contexts or "applescript" in source:
            applescript_count += 1
        context_display_counts[
            display_class_from_attribution(str(row["display_id"] or ""), source=source, app_contexts=row["app_contexts"])
        ] += 1
    known_app_count = max(0, frame_count - unknown_count)
    known_app_ratio = round(known_app_count / frame_count, 3) if frame_count else 0.0
    low_confidence_ratio = round(low_confidence / context_count, 3) if context_count else 0.0
    if frame_count == 0:
        status = "no_frames"
        confidence_label = "none"
        summary = "当前范围没有 frame signal；不要据此推断没有活动。"
    elif unknown_count == 0 and low_confidence == 0:
        status = "ok"
        confidence_label = "high"
        summary = "归因健康正常；未发现 Unknown app 或低可信上下文聚集。"
    elif unknown_count > max(10, frame_count // 3) or low_confidence > max(10, context_count // 3):
        status = "watch"
        confidence_label = "low"
        summary = "Unknown app 或低可信上下文比例偏高；副屏不会继承主屏上下文。"
    else:
        status = "observe"
        confidence_label = "medium"
        summary = "存在少量 Unknown app 或低可信上下文，可继续观察；副屏不会继承主屏上下文。"
    source_buckets = [
        {"source_class": "known_app_frame", "count": known_app_count},
        {"source_class": "unknown_app_frame", "count": unknown_count},
        {"source_class": "browser_extension_context", "count": browser_extension},
        {"source_class": "applescript_fallback_context", "count": applescript_count},
        {"source_class": "other_fallback_context", "count": max(0, context_count - browser_extension - applescript_count)},
        {"source_class": "accessibility_text_used", "count": accessibility_count},
    ]
    display_buckets = [
        {"display_class": key, "count": int(count)}
        for key, count in sorted(frame_display_counts.items())
    ]
    return {
        "status": status,
        "summary": summary,
        "confidence_label": confidence_label,
        "known_app_ratio": known_app_ratio,
        "low_confidence_ratio": low_confidence_ratio,
        "frame_signal_count": frame_count,
        "unknown_app_frame_count": unknown_count,
        "known_app_frame_count": known_app_count,
        "domain_attributed_frame_count": domain_count,
        "display_count": len(distinct_displays),
        "secondary_display_frame_count": int(frame_display_counts.get(DISPLAY_SECONDARY, 0)),
        "main_display_frame_count": int(frame_display_counts.get(DISPLAY_MAIN, 0)),
        "unknown_display_frame_count": int(frame_display_counts.get(DISPLAY_UNKNOWN, 0)),
        "context_count": context_count,
        "low_confidence_context_count": low_confidence,
        "browser_extension_context_count": browser_extension,
        "extension_browser_signal_count": browser_extension,
        "applescript_browser_signal_count": applescript_count,
        "accessibility_signal_count": accessibility_count,
        "fallback_context_count": max(0, context_count - browser_extension),
        "source_buckets": [item for item in source_buckets if item["count"] > 0],
        "display_buckets": display_buckets,
        "context_display_buckets": [
            {"display_class": key, "count": int(count)}
            for key, count in sorted(context_display_counts.items())
        ],
        "attribution_reason_counts": reason_count_rows(issue_reasons),
        "cross_display_inheritance": "disabled",
    }
