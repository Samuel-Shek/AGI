from __future__ import annotations

from collections import Counter
from typing import Any

from .app_identity import BROWSER_FAMILIES, DOCUMENT_EDITOR_FAMILIES, resolve_app_identity


REGION_EDITOR_BODY = "editor_body"
REGION_CHAT_THREAD = "chat_thread"
REGION_MAIN_CONTENT = "main_content"
REGION_SUBMIT_AREA = "submit_area"
REGION_SIDEBAR = "sidebar"
REGION_FILE_LIST = "file_list"
REGION_DESKTOP_ICON = "desktop_icon"
REGION_BROWSER_TAB = "browser_tab"
REGION_TASK_LIST = "task_list"
REGION_MENU_BAR = "menu_bar"
REGION_MEDIA_CHROME = "media_chrome"
REGION_UNKNOWN = "unknown"

ACTIONABILITY_ACTIONABLE = "actionable"
ACTIONABILITY_CONTEXT_ONLY = "context_only"
ACTIONABILITY_NOISE = "noise"
ACTIONABILITY_UNKNOWN = "unknown"

SOURCE_RULE = "rule"
SOURCE_PROVIDED = "provided"

VALID_REGION_KINDS = {
    REGION_EDITOR_BODY,
    REGION_CHAT_THREAD,
    REGION_MAIN_CONTENT,
    REGION_SUBMIT_AREA,
    REGION_SIDEBAR,
    REGION_FILE_LIST,
    REGION_DESKTOP_ICON,
    REGION_BROWSER_TAB,
    REGION_TASK_LIST,
    REGION_MENU_BAR,
    REGION_MEDIA_CHROME,
    REGION_UNKNOWN,
}
VALID_ACTIONABILITY = {
    ACTIONABILITY_ACTIONABLE,
    ACTIONABILITY_CONTEXT_ONLY,
    ACTIONABILITY_NOISE,
    ACTIONABILITY_UNKNOWN,
}
CONTEXT_ONLY_REGIONS = {
    REGION_SIDEBAR,
    REGION_BROWSER_TAB,
    REGION_TASK_LIST,
}
NOISE_REGIONS = {
    REGION_FILE_LIST,
    REGION_DESKTOP_ICON,
    REGION_MENU_BAR,
    REGION_MEDIA_CHROME,
}
ACTIONABLE_REGIONS = {
    REGION_EDITOR_BODY,
    REGION_CHAT_THREAD,
    REGION_MAIN_CONTENT,
    REGION_SUBMIT_AREA,
}

TASK_LIST_APPS = {"ticktick", "滴答清单", "things", "todoist", "reminders", "提醒事项", "omnifocus"}
MEDIA_PLAYER_APPS = {"iina", "vlc", "quicktime player", "quicktime", "mpv", "infuse", "elmedia"}
BROWSER_APPS = {"dia", "chrome", "google chrome", "safari", "arc", "firefox", "edge", "microsoft edge"}
SYSTEM_UI_APPS = {"system settings", "系统设置", "finder", "launchpad", "dock", "menubar", "menu bar"}
EDITOR_APPS = {
    "obsidian",
    "typora",
    "textedit",
    "pages",
    "microsoft word",
    "word",
    "wps",
    "cursor",
    "visual studio code",
    "vscode",
}
CHAT_APPS = {"chatgpt", "claude", "hermes", "codex", "poe"}


def classify_screen_region(event: dict[str, Any]) -> dict[str, Any]:
    """Return a safe, bounded screen region signal. Never includes raw OCR or paths."""
    rule = _rule_signal(event)
    provided = _provided_signal(event)
    if not provided:
        return rule
    return _resolve_signals(provided, rule)


def sanitize_screen_region_signal(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    if str(value.get("source") or "").startswith("vlm_layout"):
        return None
    region = str(value.get("region_kind") or value.get("region") or REGION_UNKNOWN).strip()
    if region not in VALID_REGION_KINDS:
        region = REGION_UNKNOWN
    derived_actionability = _actionability_for_region(region)
    requested_actionability = str(value.get("actionability") or derived_actionability).strip()
    if requested_actionability not in VALID_ACTIONABILITY:
        requested_actionability = derived_actionability
    # A supplied label may downgrade a region, but it may never upgrade a
    # sidebar/file-list/unknown region into an actionable one.
    actionability = (
        requested_actionability
        if _actionability_rank(requested_actionability) < _actionability_rank(derived_actionability)
        else derived_actionability
    )
    return {
        "region_kind": region,
        "actionability": actionability,
        "source": _safe_token(value.get("source") or SOURCE_PROVIDED, fallback=SOURCE_PROVIDED),
        "confidence": _safe_confidence(value.get("confidence"), default=0.5),
        "reason_codes": _safe_reason_codes(value.get("reason_codes") or value.get("reasons") or []),
    }


def region_distribution(signals: list[dict[str, Any]]) -> dict[str, Any]:
    kinds = Counter(str(signal.get("region_kind") or REGION_UNKNOWN) for signal in signals)
    actions = Counter(str(signal.get("actionability") or ACTIONABILITY_UNKNOWN) for signal in signals)
    sources = Counter(str(signal.get("source") or SOURCE_RULE) for signal in signals)
    reason_counts = Counter(
        reason
        for signal in signals
        for reason in signal.get("reason_codes") or []
    )
    return {
        "total_signal_count": len(signals),
        "region_kind_counts": dict(sorted(kinds.items())),
        "actionability_counts": dict(sorted(actions.items())),
        "source_counts": dict(sorted(sources.items())),
        "region_conflict_count": sum(
            reason_counts.get(reason, 0)
            for reason in (
                "rule_conflict",
                "layout_conflict_fail_closed",
                "same_region_actionability_conflict_fail_closed",
                "non_actionable_conflict_fail_closed",
                "actionable_region_conflict",
            )
        ),
    }


def region_is_noise(signal: dict[str, Any] | None) -> bool:
    if not isinstance(signal, dict):
        return False
    return str(signal.get("actionability") or "") == ACTIONABILITY_NOISE or str(signal.get("region_kind") or "") in NOISE_REGIONS


def region_is_actionable(signal: dict[str, Any] | None) -> bool:
    if not isinstance(signal, dict):
        return False
    return str(signal.get("actionability") or "") == ACTIONABILITY_ACTIONABLE and str(signal.get("region_kind") or "") in ACTIONABLE_REGIONS


def _rule_signal(event: dict[str, Any]) -> dict[str, Any]:
    app = _normalize(event.get("app_name") or event.get("app") or "")
    app_identity = resolve_app_identity(
        event.get("app_name") or event.get("app") or "",
        event.get("bundle_id") or "",
    )
    title = _normalize(event.get("window_title") or event.get("window_title_redacted") or "")
    text = _normalize(" ".join([str(event.get("ocr_text") or ""), str(event.get("accessibility_text") or "")]))
    ocr_regions = _ocr_region_counts(event.get("ocr_blocks"))
    combined = " ".join([app, title, text])
    if _has_submit_interaction_evidence(event):
        return _signal(REGION_SUBMIT_AREA, 0.92, ["submit_interaction_evidence"])
    if _has_submit_control_geometry(event):
        return _signal(REGION_SUBMIT_AREA, 0.86, ["submit_control_geometry"])
    if any(hint in app for hint in TASK_LIST_APPS) or _contains_any(combined, ["ticktick", "滴答清单", "收集箱", "已完成", "添加任务", "待办"]):
        return _signal(REGION_TASK_LIST, 0.9, ["task_list_app"])
    if any(hint in app for hint in MEDIA_PLAYER_APPS):
        return _signal(REGION_MEDIA_CHROME, 0.9, ["media_app"])
    if "finder" in app:
        if "desktop" in title or _contains_any(combined, ["desktop", "桌面"]):
            return _signal(REGION_DESKTOP_ICON, 0.88, ["finder_desktop"])
        if ocr_regions.get("left_sidebar", 0) > 0 or _contains_any(combined, ["sidebar", "侧边栏"]):
            return _signal(REGION_SIDEBAR, 0.86, ["finder_sidebar"])
        return _signal(REGION_FILE_LIST, 0.82, ["finder_file_list"])
    if any(hint in app for hint in SYSTEM_UI_APPS):
        return _signal(REGION_MENU_BAR, 0.84, ["system_ui_app"])
    if app_identity.matched and app_identity.family in BROWSER_FAMILIES and (
        _contains_any(combined, ["标签页", "tab", "new tab", "没有新电子邮件"]) or text.count(" - ") >= 5
    ):
        return _signal(REGION_BROWSER_TAB, 0.82, ["browser_tab_chrome"])
    if ocr_regions:
        region, count = ocr_regions.most_common(1)[0]
        mapped = _map_ocr_region(region)
        confidence = min(0.72, 0.48 + (count / max(1, sum(ocr_regions.values())) * 0.3))
        return _signal(mapped, confidence, [f"ocr_region_{region}"])
    if any(hint in app for hint in CHAT_APPS):
        return _signal(
            REGION_CHAT_THREAD,
            0.56,
            ["chat_app_prior", "app_prior_context_only"],
            actionability=ACTIONABILITY_CONTEXT_ONLY,
        )
    if app_identity.matched and app_identity.family in DOCUMENT_EDITOR_FAMILIES:
        return _signal(
            REGION_EDITOR_BODY,
            0.56,
            ["editor_app_prior", "app_prior_context_only"],
            actionability=ACTIONABILITY_CONTEXT_ONLY,
        )
    return _signal(REGION_UNKNOWN, 0.35, ["rule_unknown"])


def _provided_signal(event: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("screen_region_signal",):
        signal = sanitize_screen_region_signal(event.get(key))
        if signal:
            return signal
    return None


def _merge_signal(preferred: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    reasons = [*(preferred.get("reason_codes") or []), *(fallback.get("reason_codes") or [])]
    if preferred["region_kind"] != fallback["region_kind"] and fallback["region_kind"] != REGION_UNKNOWN:
        reasons.append("rule_conflict")
    return {
        **preferred,
        "reason_codes": _safe_reason_codes(reasons),
    }


def _resolve_signals(provided: dict[str, Any], rule: dict[str, Any]) -> dict[str, Any]:
    # Spatial evidence has precedence over a context-only app-name prior even
    # when both outcomes are non-actionable.  This prevents a chat/editor app
    # label from diluting a OCR file-list, sidebar, or chrome finding.
    if _is_spatial_signal(provided) and _is_app_prior(rule):
        return _with_reason(provided, "layout_override_app_prior")
    if _is_spatial_signal(rule) and _is_app_prior(provided):
        return _with_reason(rule, "layout_override_app_prior")
    same_region = provided["region_kind"] == rule["region_kind"]
    if same_region:
        if provided.get("actionability") != rule.get("actionability"):
            if _is_spatial_signal(provided) and _is_app_prior(rule):
                return _with_reason(provided, "layout_override_app_prior")
            if _is_spatial_signal(rule) and _is_app_prior(provided):
                return _with_reason(rule, "layout_override_app_prior")
            return _with_reason(_safer_signal(provided, rule), "same_region_actionability_conflict_fail_closed")
        preferred = provided if float(provided.get("confidence") or 0.0) >= float(rule.get("confidence") or 0.0) else rule
        reason = "layout_and_rule_agree" if _is_spatial_signal(provided) else "provided_and_rule_agree"
        return _with_reason(preferred, reason)

    provided_actionable = provided.get("actionability") == ACTIONABILITY_ACTIONABLE
    rule_actionable = rule.get("actionability") == ACTIONABILITY_ACTIONABLE
    if provided_actionable != rule_actionable:
        # Explicit layout/geometry evidence always wins over an app-name prior.
        if _is_spatial_signal(provided) and _is_app_prior(rule):
            return _with_reason(provided, "layout_override_app_prior")
        if _is_spatial_signal(rule) and _is_app_prior(provided):
            return _with_reason(rule, "layout_override_app_prior")
        safer = _safer_signal(provided, rule)
        return _with_reason(safer, "layout_conflict_fail_closed")

    if not provided_actionable and not rule_actionable:
        safer = _safer_signal(provided, rule)
        return _with_reason(safer, "non_actionable_conflict_fail_closed")

    # Both signals are actionable. Prefer explicit layout evidence, otherwise
    # keep the higher-confidence result while retaining a conflict marker.
    if _is_spatial_signal(provided):
        return _with_reason(provided, "actionable_layout_preferred")
    preferred = provided if float(provided.get("confidence") or 0.0) >= float(rule.get("confidence") or 0.0) else rule
    return _with_reason(preferred, "actionable_region_conflict")


def _safer_signal(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    left_rank = _actionability_rank(str(left.get("actionability") or ACTIONABILITY_UNKNOWN))
    right_rank = _actionability_rank(str(right.get("actionability") or ACTIONABILITY_UNKNOWN))
    if left_rank != right_rank:
        return left if left_rank < right_rank else right
    return left if float(left.get("confidence") or 0.0) >= float(right.get("confidence") or 0.0) else right


def _is_spatial_signal(signal: dict[str, Any]) -> bool:
    source = str(signal.get("source") or "")
    reasons = set(signal.get("reason_codes") or [])
    return any(
        reason.startswith("ocr_region_")
        or reason in {"submit_control_geometry", "submit_interaction_evidence", "accessibility_control_geometry"}
        for reason in reasons
    )


def _is_app_prior(signal: dict[str, Any]) -> bool:
    return "app_prior_context_only" in set(signal.get("reason_codes") or [])


def _with_reason(signal: dict[str, Any], reason: str) -> dict[str, Any]:
    return {**signal, "reason_codes": _safe_reason_codes([*(signal.get("reason_codes") or []), reason])}


def _signal(
    region: str,
    confidence: float,
    reasons: list[str],
    *,
    actionability: str | None = None,
) -> dict[str, Any]:
    return {
        "region_kind": region,
        "actionability": actionability or _actionability_for_region(region),
        "source": SOURCE_RULE,
        "confidence": round(max(0.0, min(1.0, confidence)), 2),
        "reason_codes": _safe_reason_codes(reasons),
    }


def _actionability_for_region(region: str) -> str:
    if region in {REGION_EDITOR_BODY, REGION_CHAT_THREAD, REGION_MAIN_CONTENT, REGION_SUBMIT_AREA}:
        return ACTIONABILITY_ACTIONABLE
    if region in CONTEXT_ONLY_REGIONS:
        return ACTIONABILITY_CONTEXT_ONLY
    if region in NOISE_REGIONS:
        return ACTIONABILITY_NOISE
    return ACTIONABILITY_UNKNOWN


def _actionability_rank(value: str) -> int:
    return {
        ACTIONABILITY_NOISE: 0,
        ACTIONABILITY_UNKNOWN: 1,
        ACTIONABILITY_CONTEXT_ONLY: 2,
        ACTIONABILITY_ACTIONABLE: 3,
    }.get(value, 1)


def _has_submit_interaction_evidence(event: dict[str, Any]) -> bool:
    fields = " ".join(
        str(event.get(key) or "")
        for key in (
            "interaction_type",
            "action_type",
            "accessibility_action",
            "event_type",
            "user_action",
        )
    ).lower()
    explicit = any(event.get(key) is True for key in ("submitted", "send_invoked", "control_activated", "click_confirmed"))
    return explicit or _contains_any(
        fields,
        ["submit", "send", "press_button", "button_press", "control_activate", "提交", "发送", "点击按钮"],
    )


def _has_submit_control_geometry(event: dict[str, Any]) -> bool:
    submit_terms = ["submit", "submitted", "send", "sent", "发送", "提交", "投递", "已发送"]
    blocks = event.get("ocr_blocks")
    if not isinstance(blocks, list):
        return False
    for block in blocks:
        if not isinstance(block, dict) or not _contains_any(_normalize(block.get("text")), submit_terms):
            continue
        role = _normalize(block.get("role") or block.get("accessibility_role") or block.get("control_role"))
        if _contains_any(role, ["button", "control", "menuitem", "axbutton", "按钮", "控件"]):
            return True
        box = block.get("bounding_box") or block.get("boundingBox") or {}
        if not isinstance(box, dict):
            continue
        try:
            x = float(box.get("x"))
            y = float(box.get("y"))
            width = float(box.get("width"))
            height = float(box.get("height"))
        except (TypeError, ValueError):
            continue
        if y + height >= 0.72 and x + width >= 0.5:
            return True
    return False


def _ocr_region_counts(raw_blocks: Any) -> Counter:
    counts: Counter = Counter()
    if not isinstance(raw_blocks, list):
        return counts
    for block in raw_blocks:
        if not isinstance(block, dict) or not str(block.get("text") or "").strip():
            continue
        region = str(block.get("region") or "").strip()
        if not region:
            box = block.get("bounding_box") or block.get("boundingBox") or {}
            region = _region_from_box(box if isinstance(box, dict) else {})
        counts[region or "unknown"] += 1
    return counts


def _region_from_box(box: dict[str, Any]) -> str:
    try:
        x = float(box.get("x"))
        y = float(box.get("y"))
        width = float(box.get("width"))
        height = float(box.get("height"))
    except (TypeError, ValueError):
        return "unknown"
    if y <= 0.08:
        return "top_chrome"
    if y + height >= 0.84:
        return "bottom_chrome"
    if x <= 0.24:
        return "left_sidebar"
    if x >= 0.72:
        return "right_sidebar"
    return "main_content"


def _map_ocr_region(region: str) -> str:
    if region == "main_content":
        return REGION_MAIN_CONTENT
    if region in {"left_sidebar", "right_sidebar"}:
        return REGION_SIDEBAR
    if region in {"top_chrome", "bottom_chrome"}:
        return REGION_MENU_BAR
    return REGION_UNKNOWN


def _safe_reason_codes(values: Any) -> list[str]:
    if not isinstance(values, list):
        values = [values]
    output: list[str] = []
    for value in values:
        token = _safe_token(value, fallback="")
        if token and token not in output:
            output.append(token)
    return output[:8]


def _safe_token(value: Any, *, fallback: str) -> str:
    token = "".join(ch if ch.isalnum() or ch in {"_", "-", "."} else "_" for ch in str(value or "").lower())[:48].strip("_")
    return token or fallback


def _safe_confidence(value: Any, *, default: float) -> float:
    try:
        return round(max(0.0, min(1.0, float(value))), 2)
    except (TypeError, ValueError):
        return default


def _normalize(value: Any) -> str:
    return str(value or "").lower()


def _contains_any(text: str, needles: list[str]) -> bool:
    return any(needle.lower() in text for needle in needles)
