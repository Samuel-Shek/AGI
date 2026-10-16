from __future__ import annotations

import sqlite3
from pathlib import Path

from .privacy import (
    APP_BLACKLIST,
    COMMUNICATION_APPS,
    DEFAULT_PRIVACY_PROFILE,
    DISCUSSION_APPS,
    HIGH_RISK_DOMAIN_HINTS,
    PRIVACY_FIREWALL_VERSION,
    REDACTION_POLICY_VERSION,
    classify_event,
)


PROFILE_EFFECTS = {
    "strict": {
        "label": "严格",
        "summary": "软敏感词更保守，适合临时处理未知或高敏感材料；技术、AI 工具和知识工作流里更容易过度清洗。",
        "soft_sensitive_policy": "more_conservative",
        "technical_false_positive_risk": "higher",
    },
    "personal_high_precision": {
        "label": "个人高精度",
        "summary": "硬隐私继续保护，技术讨论、AI 工具、Obsidian 和项目材料里的软敏感词减少误清洗。",
        "soft_sensitive_policy": "precision_weighted",
        "technical_false_positive_risk": "lower",
    },
}


def privacy_policy_status(conn: sqlite3.Connection, root_dir: str | Path) -> dict:
    profile = _setting(conn, "privacy_profile", DEFAULT_PRIVACY_PROFILE)
    return {
        "status": "ok",
        "mode": "read_only_preview",
        "profile": profile,
        "raw_access": False,
        "destructive_actions": False,
        "rule_write_actions": False,
        "memory_write_actions": False,
        "firewall_version": PRIVACY_FIREWALL_VERSION,
        "redaction_policy_version": REDACTION_POLICY_VERSION,
        "runtime_root": "Application Support live runtime" if _is_application_support(root_dir) else "repo_or_custom_runtime",
        "rule_counts": {
            "hard_block_apps": len(APP_BLACKLIST),
            "communication_apps": len(COMMUNICATION_APPS),
            "discussion_apps": len(DISCUSSION_APPS),
            "high_risk_domain_hints": len(HIGH_RISK_DOMAIN_HINTS),
        },
        "policies": [
            {
                "name": "hard_block_apps",
                "label": "硬保护 App",
                "count": len(APP_BLACKLIST),
                "examples": _sample(APP_BLACKLIST),
                "effect": "匹配后不进入可读活动正文，只保留隐私事件计数。",
            },
            {
                "name": "communication_apps",
                "label": "沟通类 App",
                "count": len(COMMUNICATION_APPS),
                "examples": _sample(COMMUNICATION_APPS),
                "effect": "沟通内容默认不进入 AI 接手包可读明细，只进入计数或安全摘要。",
            },
            {
                "name": "high_risk_domain_hints",
                "label": "高风险域名提示",
                "count": len(HIGH_RISK_DOMAIN_HINTS),
                "examples": _sample(HIGH_RISK_DOMAIN_HINTS),
                "effect": "银行、支付、交易、凭据相关页面优先触发保护。",
            },
        ],
        "preview_capability": {
            "available": True,
            "changes_runtime_policy": False,
            "supported_inputs": ["app", "domain", "title", "text", "profile"],
        },
        "safety_boundary": {
            "raw_access": False,
            "destructive_actions": False,
            "rule_write_actions": False,
            "notes": [
                "本命令只读，不新增、不删除、不停用防火墙规则。",
                "预览结果来自假事件分类，不读取 raw 截图、OCR 原文或 SQLite 原始表。",
            ],
        },
    }


def privacy_profile_preview(conn: sqlite3.Connection, root_dir: str | Path, *, profile: str) -> dict:
    current = _setting(conn, "privacy_profile", DEFAULT_PRIVACY_PROFILE)
    target = _normalize_profile(profile)
    current_effect = PROFILE_EFFECTS.get(current, PROFILE_EFFECTS[DEFAULT_PRIVACY_PROFILE])
    target_effect = PROFILE_EFFECTS[target]
    rule_counts = {
        "hard_block_apps": len(APP_BLACKLIST),
        "communication_apps": len(COMMUNICATION_APPS),
        "discussion_apps": len(DISCUSSION_APPS),
        "high_risk_domain_hints": len(HIGH_RISK_DOMAIN_HINTS),
    }
    unchanged = [
        "passwords_and_verification_codes",
        "real_secret_assignments_and_private_keys",
        "payment_bank_and_trading_pages",
        "adult_content_and_private_browsing",
        "password_managers",
        "personal_clinical_medical_content",
    ]
    if target == "strict":
        diff_summary = "将提高软敏感词清洗强度；适合临时高敏感场景，但技术/知识工作流可能更容易误清洗。"
    elif current == "strict":
        diff_summary = "将降低技术、AI 工具和个人知识工作流中的软敏感误清洗；硬隐私保护不变。"
    else:
        diff_summary = "目标 profile 与当前 profile 一致；不会改变运行策略。"
    return {
        "status": "ok",
        "mode": "profile_dry_run",
        "current_profile": current,
        "target_profile": target,
        "profile_change": current != target,
        "requires_confirmation": True,
        "changes_runtime_policy": False,
        "raw_access": False,
        "destructive_actions": False,
        "rule_write_actions": False,
        "memory_write_actions": False,
        "runtime_root": "Application Support live runtime" if _is_application_support(root_dir) else "repo_or_custom_runtime",
        "rule_counts": rule_counts,
        "current_effect": current_effect,
        "target_effect": target_effect,
        "profile_diff": {
            "summary": diff_summary,
            "hard_blocks_unchanged": True,
            "app_domain_rule_write": False,
            "audit_required_for_future_fine_grained_rules": True,
        },
        "unchanged_boundaries": unchanged,
        "safe_counters": {
            "hard_block_apps": len(APP_BLACKLIST),
            "communication_apps": len(COMMUNICATION_APPS),
            "high_risk_domain_hints": len(HIGH_RISK_DOMAIN_HINTS),
            "likely_false_positive_categories": [
                "technical_ai_tool_context",
                "code_or_secret_discussion_without_real_secret",
                "obsidian_or_project_notes",
            ],
        },
        "summary": f"{current_effect['label']} -> {target_effect['label']}：{diff_summary}",
    }


def privacy_policy_preview(
    conn: sqlite3.Connection,
    *,
    app: str = "",
    domain: str = "",
    title: str = "",
    text: str = "",
) -> dict:
    event = {
        "captured_at": "2026-01-01T00:00:00+00:00",
        "display_id": "preview",
        "app_name": app or "Unknown",
        "bundle_id": "",
        "window_title": title,
        "domain": domain,
        "url": f"https://{domain}/" if domain else "",
        "ocr_text": text,
        "context_confidence": 0.8,
    }
    result = classify_event(event)
    visible_policy = _visible_policy(result.status, result.privacy_level)
    return {
        "status": "ok",
        "mode": "read_only_preview",
        "input": {
            "app": app,
            "domain": domain,
            "title_present": bool(title),
            "text_present": bool(text),
        },
        "classification": {
            "firewall_status": result.status,
            "action": result.action,
            "reason": result.reason,
            "privacy_level": result.privacy_level,
            "privacy_flags": result.privacy_flags,
            "redaction_confidence": result.redaction_confidence,
            "context_suppressed": result.context_suppressed,
        },
        "ai_handoff_effect": visible_policy,
        "raw_access": False,
        "destructive_actions": False,
        "rule_write_actions": False,
        "memory_write_actions": False,
    }


def _visible_policy(status: str, privacy_level: str) -> dict:
    if status == "blocked":
        return {
            "readable_detail": False,
            "count_only": True,
            "summary": "会作为隐私保护事件计数，不进入 AI 接手包正文。",
        }
    if privacy_level in {"partial_redacted", "communication"}:
        return {
            "readable_detail": False,
            "count_only": True,
            "summary": "只进入计数或风险提示，不进入 AI 可读明细。",
        }
    return {
        "readable_detail": True,
        "count_only": False,
        "summary": "可进入清洗后活动摘要；仍会经过导出隐私扫描。",
    }


def _setting(conn: sqlite3.Connection, key: str, default: str) -> str:
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    except sqlite3.Error:
        return default
    return str(row["value"]) if row else default


def _normalize_profile(profile: str) -> str:
    value = str(profile or "").strip().lower()
    if value not in PROFILE_EFFECTS:
        raise ValueError(f"unsupported privacy profile: {profile}")
    return value


def _sample(values) -> list[str]:
    return sorted(str(item) for item in values)[:8]


def _is_application_support(root_dir: str | Path) -> bool:
    normalized = str(Path(root_dir).expanduser().resolve())
    return "/Library/Application Support/Personal AI Monitor" in normalized
