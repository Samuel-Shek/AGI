from __future__ import annotations

import sqlite3
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import __version__ as CORE_VERSION
from .ai_handoff import ai_handoff_status
from .daemon import get_status
from .data_hygiene import backup_hygiene_report
from .privacy_policy import privacy_policy_status


LOCAL_USER_PATH_RE = re.compile(r"/Users/(?!example\b|test\b|runner\b)[A-Za-z0-9._-]+/[^\s,'\"`)]*")
SQLITE_RE = re.compile(r"\b[^\s,'\"`)]*activity\.sqlite[^\s,'\"`)]*|\b[^\s,'\"`)]*\.sqlite3?\b", re.IGNORECASE)
IMAGE_PATH_RE = re.compile(r"\b[^\s,'\"`)]*\.(?:png|jpg|jpeg|heic)\b", re.IGNORECASE)
TOKEN_HINT_RE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|xox[baprs]-[A-Za-z0-9-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,})\b")


def diagnostics_status(conn: sqlite3.Connection, root_dir: str | Path, db_path: str | Path, *, mode: str = "full") -> dict:
    mode = _diagnostics_mode(mode)
    root = Path(root_dir)
    db = Path(db_path)
    core_root = Path(__file__).resolve().parents[1]
    probes: list[dict] = []

    started = time.perf_counter()
    if mode == "quick":
        core_probe = _probe("core_status", "记录服务", lambda: _core_status_quick_probe(conn))
    else:
        core_probe = _probe("core_status", "记录服务", lambda: _core_status_probe(get_status(conn, root)))
    probes.append(core_probe)
    core_payload = core_probe.get("payload") if isinstance(core_probe.get("payload"), dict) else {}

    probes.append(_probe("privacy_policy", "隐私规则", lambda: _privacy_policy_probe(privacy_policy_status(conn, root))))
    if mode == "full":
        probes.insert(1, _probe("ai_handoff", "AI 接手包", lambda: _ai_handoff_probe(ai_handoff_status(conn, root))))
        probes.append(_probe("hygiene", "数据卫生", lambda: _hygiene_probe(_hygiene_report_with_retry(core_root, db))))
        ha_payload = core_payload.get("ha_automation") if isinstance(core_payload.get("ha_automation"), dict) else {}
        probes.append(_probe("ha_automation", "HA 自动化", lambda: _ha_probe(ha_payload)))

        quality_payload = core_payload.get("quality_advice") if isinstance(core_payload.get("quality_advice"), dict) else {}
        probes.append(_probe("quality_advice", "质量顾问", lambda: _quality_probe(quality_payload)))

    else:
        probes.append(_quick_probe("ai_handoff", "AI 接手包", "快速诊断未生成完整 AI 接手包；需要时运行 full 诊断。"))
        probes.append(_quick_probe("hygiene", "数据卫生", "快速诊断跳过完整卫生扫描，避免后台写库窗口造成慢响应。"))
        probes.append(_quick_probe("ha_automation", "HA 自动化", "快速诊断跳过 HA 深度状态；完整诊断可查看 pending/failed/dead-letter。"))
        probes.append(_quick_probe("quality_advice", "质量顾问", "快速诊断跳过质量顾问深度状态；完整诊断可查看建议计数。"))

    runtime = _runtime_summary(core_payload)
    boundary = {
        "raw_access": False,
        "destructive_actions": False,
        "memory_write_actions": False,
        "rule_write_actions": False,
        "cloud_analysis": "off",
        "reads_raw_sources": False,
    }
    overall_kind = _overall_kind(probes)
    summary = _overall_summary(overall_kind, probes)
    actions = _diagnostic_actions(probes)
    checked_probes = [str(probe.get("name") or "") for probe in probes if probe.get("status") != "skipped_quick_mode"]
    skipped_probes = [str(probe.get("name") or "") for probe in probes if probe.get("status") == "skipped_quick_mode"]
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return {
        "status": "attention_required" if overall_kind == "danger" else ("partial" if skipped_probes else "ok"),
        "kind": overall_kind,
        "interface": "diagnostics",
        "mode": mode,
        "version": CORE_VERSION,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_ms": elapsed_ms,
        "target_response_ms": 2000 if mode == "quick" else 90000,
        "summary": summary,
        "checked_probes": checked_probes,
        "skipped_probes": skipped_probes,
        "check_scope": {
            "checked_count": len(checked_probes),
            "skipped_count": len(skipped_probes),
            "complete": not skipped_probes,
        },
        "runtime": runtime,
        "safety_boundary": boundary,
        "probes": [_public_probe(probe) for probe in probes],
        "recommendations": _recommendations(probes),
        "actions": actions,
        "action_counts": _action_counts(actions),
        "raw_access": False,
        "destructive_actions": False,
        "memory_write_actions": False,
        "rule_write_actions": False,
        "cloud_analysis": "off",
    }


def render_diagnostics_markdown(payload: dict) -> str:
    lines = [
        "# PAM 本机诊断包",
        "",
        "> 只读诊断摘要；不包含 raw 截图、OCR 原文、SQLite 原始表、完整本机路径、token、私聊原文或 HA package 正文。",
        "",
        f"- status: `{payload.get('status', 'unknown')}`",
        f"- kind: `{payload.get('kind', 'neutral')}`",
        f"- version: `{payload.get('version', '')}`",
        f"- mode: `{payload.get('mode', 'full')}`",
        f"- checked_at: `{payload.get('checked_at', '')}`",
        f"- elapsed_ms: `{payload.get('elapsed_ms', 0)}`",
        f"- raw_access: `{str(payload.get('raw_access')).lower()}`",
        f"- destructive_actions: `{str(payload.get('destructive_actions')).lower()}`",
        "",
        "## 总览",
        "",
        _safe_text(str(payload.get("summary") or "无诊断摘要。")),
        "",
        "## 检查项",
        "",
    ]
    for probe in payload.get("probes") or []:
        lines.append(
            f"- **{probe.get('label', probe.get('name', 'unknown'))}** "
            f"`{probe.get('kind', 'neutral')}`：{_safe_text(str(probe.get('summary', '')))}"
            f"（{int(probe.get('elapsed_ms') or 0)}ms）"
        )
    recommendations = payload.get("recommendations") or []
    if recommendations:
        lines.extend(["", "## 建议", ""])
        for item in recommendations:
            lines.append(f"- {_safe_text(str(item))}")
    actions = payload.get("actions") or []
    if actions:
        lines.extend(["", "## 动作分级", ""])
        for item in actions:
            lines.append(
                f"- `{item.get('kind', 'read_only')}` {item.get('label', '')}："
                f"{_safe_text(str(item.get('summary', '')))}"
            )
    return "\n".join(lines).rstrip() + "\n"


def _probe(name: str, label: str, build: Callable[[], dict]) -> dict:
    started = time.perf_counter()
    try:
        payload = build()
    except Exception as exc:  # pragma: no cover - defensive diagnostic boundary
        return {
            "name": name,
            "label": label,
            "kind": "danger",
            "status": "error",
            "summary": _safe_text(f"{label}暂时无法读取：{type(exc).__name__}"),
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "payload": {},
        }
    payload["name"] = name
    payload["label"] = label
    payload["elapsed_ms"] = int((time.perf_counter() - started) * 1000)
    if payload.get("raw_access") or payload.get("destructive_actions") or payload.get("rule_write_actions"):
        payload["kind"] = "danger"
        payload["summary"] = _safe_text(f"{label}返回了异常安全边界。")
    return payload


def _quick_probe(name: str, label: str, summary: str) -> dict:
    return {
        "name": name,
        "label": label,
        "kind": "neutral",
        "status": "skipped_quick_mode",
        "summary": _safe_text(summary),
        "raw_access": False,
        "destructive_actions": False,
        "memory_write_actions": False,
        "rule_write_actions": False,
        "elapsed_ms": 0,
        "counts": {},
    }


def _core_status_probe(status: dict) -> dict:
    freshness = status.get("capture_freshness") if isinstance(status.get("capture_freshness"), dict) else {}
    attention = status.get("attention_items") if isinstance(status.get("attention_items"), list) else []
    actionable_attention = [item for item in attention if str(item.get("kind") or "") in {"warning", "danger"}]
    raw_residue = int(status.get("raw_residue_count") or 0)
    last_error = str(status.get("last_error") or "")
    launch_running = bool(status.get("launch_agent_running"))
    kind = "ok"
    if last_error or raw_residue > 0 or freshness.get("is_stale"):
        kind = "danger"
    elif not launch_running or actionable_attention:
        kind = "warning"
    return {
        "status": status.get("status", "unknown"),
        "kind": kind,
        "summary": _safe_text(status.get("status_explanation") or freshness.get("summary") or "记录状态已读取。"),
        "raw_access": bool(status.get("raw_access")),
        "destructive_actions": False,
        "counts": {
            "activities": int(status.get("today_activity_count") or 0),
            "privacy_events": int(status.get("today_privacy_count") or 0),
            "gaps": int(status.get("today_gap_count") or 0),
            "raw_residue": raw_residue,
            "attention_items": len(actionable_attention),
        },
        "payload": status,
    }


def _core_status_quick_probe(conn: sqlite3.Connection) -> dict:
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    end = now.isoformat()
    activity_count = _count_between(
        conn,
        "extracted_activities",
        "started_at",
        "COALESCE(ended_at, started_at)",
        start,
        end,
        extra="deleted_at IS NULL",
    )
    privacy_count = _count_between(conn, "privacy_events", "started_at", "COALESCE(ended_at, started_at)", start, end)
    gap_count = _count_between(conn, "gap_events", "started_at", "COALESCE(ended_at, started_at)", start, end)
    last_error = _daemon_state(conn, "last_error")
    last_capture_at = _daemon_state(conn, "last_capture_at")
    kind = "warning" if last_error else "ok"
    summary = (
        "快速诊断已读取记录服务轻量摘要；raw residue、coverage 和 LaunchAgent 深度状态请看 full 诊断。"
        if not last_error
        else "记录服务最近错误非空；快速诊断未读取 raw 或深度覆盖信息。"
    )
    return {
        "status": "quick_ok" if not last_error else "last_error_present",
        "kind": kind,
        "summary": _safe_text(summary),
        "raw_access": False,
        "destructive_actions": False,
        "counts": {
            "activities": activity_count,
            "privacy_events": privacy_count,
            "gaps": gap_count,
            "raw_residue": None,
            "attention_items": None,
            "known_last_error": 1 if last_error else 0,
        },
        "payload": {
            "runtime_identity": {},
            "launch_agent_loaded": None,
            "launch_agent_running": None,
            "last_capture_at": last_capture_at,
            "last_error": _safe_text(last_error),
        },
    }


def _ai_handoff_probe(status: dict) -> dict:
    today = status.get("today") if isinstance(status.get("today"), dict) else {}
    kind = "ok" if status.get("status") == "ok" and not status.get("raw_access") else "danger"
    activity_count = int(today.get("activity_count") or 0)
    summary = f"只读接口可用；今天可读活动 {activity_count} 条。"
    return {
        "status": status.get("status", "unknown"),
        "kind": kind,
        "summary": _safe_text(summary),
        "raw_access": bool(status.get("raw_access")),
        "destructive_actions": bool(status.get("destructive_actions")),
        "memory_write_actions": bool(status.get("memory_write_actions")),
        "counts": {
            "activity_count": activity_count,
            "privacy_event_count": int(today.get("privacy_event_count") or 0),
            "gap_event_count": int(today.get("gap_event_count") or 0),
        },
    }


def _privacy_policy_probe(status: dict) -> dict:
    counts = status.get("rule_counts") if isinstance(status.get("rule_counts"), dict) else {}
    kind = "ok" if status.get("status") == "ok" and not status.get("rule_write_actions") else "danger"
    return {
        "status": status.get("status", "unknown"),
        "kind": kind,
        "summary": _safe_text(f"{status.get('profile', 'unknown')}；只读规则预览可用，不写防火墙规则。"),
        "raw_access": bool(status.get("raw_access")),
        "destructive_actions": bool(status.get("destructive_actions")),
        "rule_write_actions": bool(status.get("rule_write_actions")),
        "counts": {
            "hard_block_apps": int(counts.get("hard_block_apps") or 0),
            "communication_apps": int(counts.get("communication_apps") or 0),
            "high_risk_domain_hints": int(counts.get("high_risk_domain_hints") or 0),
        },
    }


def _hygiene_probe(report: dict) -> dict:
    if report.get("_diagnostic_status") == "database_busy":
        return {
            "status": "unavailable",
            "kind": "warning",
            "summary": "数据卫生状态暂时不可读：后台记录正在写入数据库，稍后刷新会自动重试。",
            "raw_access": False,
            "destructive_actions": False,
            "counts": {
                "runtime_permission_candidates": 0,
                "backup_candidates": 0,
                "ha_cache_residue": 0,
                "activity_export_cache": 0,
            },
        }
    runtime_permissions = _candidate_count(report, "runtime_permission_summary")
    backups = _candidate_count(report, "risk_summary")
    ha_cache = _candidate_count(report, "ha_cache_residue_summary")
    exports = _candidate_count(report, "activity_export_cache_summary")
    strict_ok = _strict_health_ok(report)
    kind = "ok" if strict_ok and runtime_permissions == 0 and ha_cache == 0 else "warning"
    strict_text = "strict health 正常" if strict_ok else "strict health 需要复查"
    return {
        "status": "ok" if strict_ok else "warning",
        "kind": kind,
        "summary": _safe_text(f"{strict_text}；runtime 权限候选 {runtime_permissions}，HA cache 残留 {ha_cache}，备份候选 {backups}。权限候选属于可修复卫生项，不代表采集服务故障。"),
        "raw_access": False,
        "destructive_actions": False,
        "counts": {
            "runtime_permission_candidates": runtime_permissions,
            "backup_candidates": backups,
            "ha_cache_residue": ha_cache,
            "activity_export_cache": exports,
        },
    }


def _hygiene_report_with_retry(core_root: Path, db: Path, *, attempts: int = 3, delay_seconds: float = 0.35) -> dict:
    for attempt in range(max(1, attempts)):
        try:
            return backup_hygiene_report(core_root, db)
        except sqlite3.OperationalError as exc:
            if not _sqlite_busy(exc) or attempt >= attempts - 1:
                if _sqlite_busy(exc):
                    return {"_diagnostic_status": "database_busy"}
                raise
            time.sleep(delay_seconds * (attempt + 1))
    return {"_diagnostic_status": "database_busy"}


def _sqlite_busy(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "database is locked" in message or "database table is locked" in message


def _ha_probe(status: dict) -> dict:
    if not status:
        return {"status": "unavailable", "kind": "warning", "summary": "HA 自动化状态暂时不可读。"}
    return {
        "status": status.get("status", "unknown"),
        "kind": status.get("kind", "neutral"),
        "summary": _safe_text(status.get("summary") or status.get("label") or "HA 自动化状态已读取。"),
        "counts": {
            "pending": int(status.get("pending_count") or 0),
            "failed": int(status.get("failed_count") or 0),
            "unresolved_dead_letters": int(status.get("unresolved_dead_letter_count") or 0),
            "historical_dead_letters": int(status.get("historical_dead_letter_count") or 0),
        },
        "raw_access": False,
        "destructive_actions": False,
    }


def _quality_probe(status: dict) -> dict:
    if not status:
        return {"status": "unavailable", "kind": "warning", "summary": "质量顾问状态暂时不可读。"}
    return {
        "status": status.get("status", "unknown"),
        "kind": status.get("kind", "neutral"),
        "summary": _safe_text(status.get("summary") or status.get("label") or "质量顾问状态已读取。"),
        "counts": {
            "recommendations": int(status.get("latest_recommendation_count") or 0),
            "issues": int(status.get("latest_issue_count") or 0),
        },
        "raw_access": False,
        "destructive_actions": False,
    }




def _runtime_summary(core_payload: dict) -> dict:
    identity = core_payload.get("runtime_identity") if isinstance(core_payload.get("runtime_identity"), dict) else {}
    return {
        "path_kind": identity.get("path_kind", "unknown"),
        "path_label": identity.get("path_label", "unknown"),
        "is_application_support_runtime": _optional_bool(identity, "is_application_support_runtime"),
        "is_repo_local": _optional_bool(identity, "is_repo_local"),
        "launch_agent_loaded": _optional_bool(core_payload, "launch_agent_loaded"),
        "launch_agent_running": _optional_bool(core_payload, "launch_agent_running"),
        "dev_dashboard_enabled": _optional_bool(core_payload, "dev_dashboard_enabled"),
        "dashboard_bind": core_payload.get("dashboard_bind", "127.0.0.1"),
    }


def _optional_bool(payload: dict, key: str):
    return bool(payload.get(key)) if key in payload and payload.get(key) is not None else None


def _public_probe(probe: dict) -> dict:
    public = {
        "name": probe.get("name", ""),
        "label": probe.get("label", ""),
        "status": probe.get("status", "unknown"),
        "kind": probe.get("kind", "neutral"),
        "summary": _safe_text(str(probe.get("summary", ""))),
        "elapsed_ms": int(probe.get("elapsed_ms") or 0),
        "raw_access": bool(probe.get("raw_access")),
        "destructive_actions": bool(probe.get("destructive_actions")),
    }
    if "rule_write_actions" in probe:
        public["rule_write_actions"] = bool(probe.get("rule_write_actions"))
    if "memory_write_actions" in probe:
        public["memory_write_actions"] = bool(probe.get("memory_write_actions"))
    if isinstance(probe.get("counts"), dict):
        public["counts"] = probe["counts"]
    return public


def _overall_kind(probes: list[dict]) -> str:
    kinds = {str(probe.get("kind") or "neutral") for probe in probes}
    if "danger" in kinds:
        return "danger"
    if "warning" in kinds:
        return "warning"
    if any(probe.get("status") == "skipped_quick_mode" for probe in probes):
        return "partial"
    return "ok"


def _overall_summary(kind: str, probes: list[dict]) -> str:
    warnings = [probe for probe in probes if probe.get("kind") == "warning"]
    dangers = [probe for probe in probes if probe.get("kind") == "danger"]
    if dangers:
        labels = "、".join(str(item.get("label") or item.get("name")) for item in dangers[:3])
        return _safe_text(f"诊断发现 {len(dangers)} 个异常项：{labels}。")
    if warnings:
        labels = "、".join(str(item.get("label") or item.get("name")) for item in warnings[:3])
        return _safe_text(f"诊断发现 {len(warnings)} 个需要观察的项目：{labels}。")
    skipped = [probe for probe in probes if probe.get("status") == "skipped_quick_mode"]
    if skipped:
        return _safe_text(
            f"快速诊断已完成轻量检查；另有 {len(skipped)} 项未检查，不能据此判断全部系统正常。需要完整结论时运行 full 诊断。"
        )
    return _safe_text("本机诊断正常；记录、隐私、AI 接手包、自动化和卫生检查都在安全边界内。")


def _recommendations(probes: list[dict]) -> list[str]:
    items: list[str] = []
    for probe in probes:
        if probe.get("kind") not in {"warning", "danger"}:
            continue
        label = str(probe.get("label") or probe.get("name") or "检查项")
        summary = str(probe.get("summary") or "需要复查。")
        items.append(_safe_text(f"{label}：{summary}"))
    return items[:8]


def _diagnostic_actions(probes: list[dict]) -> list[dict]:
    actions: list[dict] = []
    for probe in probes:
        if probe.get("kind") not in {"warning", "danger"}:
            continue
        name = str(probe.get("name") or "")
        label = str(probe.get("label") or name or "检查项")
        if name == "hygiene":
            action = {
                "id": "backup_hygiene_dry_run",
                "kind": "previewable",
                "label": "只读卫生复查",
                "summary": "可运行 backup-hygiene 重新查看候选；不会删除、移动或修改数据。",
            }
        elif name == "privacy_policy":
            action = {
                "id": "privacy_policy_preview",
                "kind": "read_only",
                "label": "隐私规则预览",
                "summary": "可用 app/domain 样例预览 Privacy Firewall 效果；不写规则。",
            }
        elif name == "core_status":
            action = {
                "id": "refresh_core_status",
                "kind": "read_only",
                "label": "刷新记录状态",
                "summary": "先刷新 status 与权限状态；涉及权限或清理时仍需人工确认。",
            }
        elif name == "ha_automation":
            action = {
                "id": "ha_automation_status",
                "kind": "read_only",
                "label": "查看 HA 自动化",
                "summary": "只读查看待处理、失败和历史 dead-letter，不触发写入。",
            }
        else:
            action = {
                "id": f"inspect_{name or 'diagnostic'}",
                "kind": "read_only",
                "label": f"查看{label}",
                "summary": str(probe.get("summary") or "只读查看诊断摘要。"),
            }
        actions.append({"probe": name, **action})
    return actions[:8]


def _action_counts(actions: list[dict]) -> dict:
    counts = {"read_only": 0, "previewable": 0, "confirm_required": 0}
    for action in actions:
        kind = str(action.get("kind") or "read_only")
        if kind not in counts:
            counts[kind] = 0
        counts[kind] += 1
    return counts


def _candidate_count(report: dict, key: str) -> int:
    summary = report.get(key) if isinstance(report.get(key), dict) else {}
    return int(summary.get("candidate_count") or 0)


def _count_between(
    conn: sqlite3.Connection,
    table: str,
    start_column: str,
    end_expression: str,
    start: str,
    end: str,
    *,
    extra: str = "",
) -> int:
    where = [
        f"datetime({end_expression}) >= datetime(?)",
        f"datetime({start_column}) < datetime(?)",
    ]
    if extra:
        where.append(extra)
    row = conn.execute(
        f"SELECT COUNT(*) AS count FROM {table} WHERE {' AND '.join(where)}",
        (start, end),
    ).fetchone()
    return int(row["count"] or 0)


def _daemon_state(conn: sqlite3.Connection, key: str) -> str:
    try:
        row = conn.execute("SELECT value FROM daemon_state WHERE key = ?", (key,)).fetchone()
    except sqlite3.Error:
        return ""
    return str(row["value"] or "") if row else ""


def _strict_health_ok(report: dict) -> bool:
    automation = report.get("automation_plan") if isinstance(report.get("automation_plan"), dict) else {}
    backup_rotation = automation.get("backup_rotation") if isinstance(automation.get("backup_rotation"), dict) else {}
    actions = backup_rotation.get("actions") if isinstance(backup_rotation.get("actions"), list) else []
    values = [bool(action.get("strict_cleanup_health_ok")) for action in actions if isinstance(action, dict) and "strict_cleanup_health_ok" in action]
    if values:
        return all(values)
    return True


def _safe_text(value: str) -> str:
    text = LOCAL_USER_PATH_RE.sub("[local_path]", value)
    text = SQLITE_RE.sub("[sqlite_path]", text)
    text = IMAGE_PATH_RE.sub("[image_path]", text)
    text = TOKEN_HINT_RE.sub("[token]", text)
    return text


def _diagnostics_mode(mode: str) -> str:
    value = str(mode or "full").strip().lower()
    if value in {"quick", "fast"}:
        return "quick"
    if value in {"full", "complete"}:
        return "full"
    raise ValueError(f"unsupported diagnostics mode: {mode}")
