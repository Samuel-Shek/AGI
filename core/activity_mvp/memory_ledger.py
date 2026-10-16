from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
from difflib import SequenceMatcher
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .canonical_paths import discover_frozen_pam_ledger_paths, frozen_pam_ledger_path

# Retained only for callers importing the historical constant.  Resolution is
# centralized in canonical_paths and the resulting ledger is read-only.
DEFAULT_LEDGER_RELATIVE_PATH = Path("✦ AI 工作台") / "PAM 长期记忆台账（已停更）.md"
DEFAULT_DETAIL_RELATIVE_PATH = Path("Library/Application Support/Personal AI Monitor/memory-ledger/PAM 长期记忆详细记录.md")
DEFAULT_BACKUP_RELATIVE_PATH = Path("Library/Application Support/Personal AI Monitor/memory-ledger-backups")
DELETE_CONFIRM = "DELETE_PAM_MEMORY"
REWRITE_CONFIRM = "REWRITE_PAM_MEMORY"
FROZEN_LEDGER_STATUS = "frozen_legacy"
FROZEN_LEDGER_MESSAGE = "PAM 长期记忆台账已停更，仅供历史查阅；不允许刷新、同步、删除或改写。"

LOCAL_PATH_RE = re.compile(r"(/Users/[^\s\"'`，。；;]*|/tmp/[^\s\"'`，。；;]*|~/[^\s\"'`，。；;]*)", re.I)
SECRET_RE = re.compile(r"(-----BEGIN .*PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{20,}\b)", re.I)
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
PHONE_RE = re.compile(
    r"(?<!\w)(?:\+\d[\d\s().-]{7,}\d|1[3-9]\d{9}|\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4})(?!\w)"
)
LEDGER_BLOCK_RE = re.compile(r"```pam-ledger\n(.*?)\n```", re.S)
LEDGER_COMMENT_RE = re.compile(r"<!--\s*pam-ledger\n(.*?)\n-->", re.S)

FIELD_ORDER = [
    "ledger_id",
    "entry_type",
    "can_sync",
    "memory_id",
    "current_hash",
    "memory_safe",
    "user_id",
    "source_system",
    "source_platform",
    "source_profile",
    "package_id",
    "hour_key",
    "subject_scope",
    "source_category",
    "memory_scope",
    "memory_write_policy",
    "sync_action",
    "sync_confirm",
    "rewrite_memory",
    "review_note",
    "sync_status",
    "last_synced_at",
    "created_at",
]
EDITABLE_FIELDS = {"sync_action", "sync_confirm", "rewrite_memory", "review_note"}
SYNC_STATUS_FIELDS = {"sync_action", "sync_confirm", "rewrite_memory", "sync_status", "last_synced_at"}


def memory_ledger_status(
    conn: sqlite3.Connection | None = None,
    *,
    ledger_path: str | Path | None = None,
    detail_path: str | Path | None = None,
) -> dict:
    path = _ledger_path(ledger_path)
    detail = _detail_path(detail_path)
    discovered = discover_frozen_pam_ledger_paths(home=Path.home()) if ledger_path is None else ([path] if path.is_file() else [])
    read_path = path if path.is_file() else (discovered[0] if discovered else path)
    entries = _read_ledger_entries(detail)
    if not entries:
        entries = _read_ledger_entries(read_path)
    human_format = _human_ledger_format(read_path)
    human_lines = _read_human_memory_lines(read_path) if human_format == "simple" else []
    needs_manual = [entry for entry in entries if entry.get("sync_status") == "needs_manual_lookup"]
    current_entries = [entry for entry in entries if entry.get("entry_type") == "mem0" and entry.get("sync_status") != "deleted"]
    return {
        "status": FROZEN_LEDGER_STATUS,
        "ledger_path": str(path),
        "detail_path": str(detail),
        "read_only_source_path": str(read_path) if read_path.is_file() else "",
        "discovered_read_only_paths": [str(item) for item in discovered],
        "raw_access": False,
        "destructive_actions": False,
        "read_only": True,
        "write_enabled": False,
        "message": FROZEN_LEDGER_MESSAGE,
        "entry_count": len(human_lines) if read_path.exists() else len(current_entries),
        "current_memory_count": len(current_entries),
        "pending_action_count": 0,
        "needs_manual_lookup_count": len(needs_manual),
        "can_sync_count": 0,
        "unmatched_new_line_count": 0,
        "complex_change_count": 0,
    }


def refresh_memory_ledger(
    conn: sqlite3.Connection,
    *,
    ledger_path: str | Path | None = None,
    detail_path: str | Path | None = None,
    limit: int = 1000,
    include_mem0: bool = True,
) -> dict:
    status = memory_ledger_status(conn, ledger_path=ledger_path, detail_path=detail_path)
    return {
        **status,
        "action_status": "blocked_read_only",
        "refreshed_at": "",
        "mem0_entry_count": 0,
        "ha_summary_entry_count": 0,
    }


def sync_memory_ledger(
    conn: sqlite3.Connection,
    *,
    ledger_path: str | Path | None = None,
    detail_path: str | Path | None = None,
    backup_dir: str | Path | None = None,
    dry_run: bool = False,
) -> dict:
    status = memory_ledger_status(conn, ledger_path=ledger_path, detail_path=detail_path)
    result = _result("", "blocked_read_only", FROZEN_LEDGER_MESSAGE)
    return {
        **status,
        "action_status": "blocked_read_only",
        "backup_path": "",
        "raw_access": False,
        "destructive_actions": False,
        "processed_count": 0,
        "deleted_count": 0,
        "rewritten_count": 0,
        "unmatched_new_line_count": 0,
        "complex_change_count": 0,
        "skipped_count": 1,
        "results": [result],
    }


def render_memory_ledger_markdown(entries: list[dict[str, str]]) -> str:
    visible_entries = [entry for entry in entries if entry.get("entry_type") == "mem0" and entry.get("sync_status") != "deleted"]
    lines = [
        "# PAM 长期记忆台账（已停更）",
        "",
        "> 历史只读参考；PAM 不再刷新、同步或改写本文件。",
        "",
    ]
    if not visible_entries:
        lines.append("暂无 PAM 来源长期记忆。")
        return "\n".join(lines).rstrip() + "\n"
    for index, entry in enumerate(visible_entries, start=1):
        lines.append(f"{index}. {entry.get('memory_safe') or '（空）'}")
    return "\n".join(lines).rstrip() + "\n"


def render_memory_ledger_detail_markdown(entries: list[dict[str, str]]) -> str:
    mem0_count = sum(1 for entry in entries if entry.get("entry_type") == "mem0")
    ha_count = sum(1 for entry in entries if entry.get("entry_type") == "ha_job_summary")
    lines = [
        "# PAM 长期记忆详细记录",
        "",
        "> 历史系统记录。台账已停更，PAM 不再写入或同步。",
        "",
        "## 统计",
        "",
        f"- refreshed_at: `{_now_iso()}`",
        f"- current_mem0_entries: `{mem0_count}`",
        f"- ha_summary_entries: `{ha_count}`",
        "",
        "## 当前 mem0 记忆",
        "",
    ]
    for index, entry in enumerate([entry for entry in entries if entry.get("entry_type") == "mem0"], start=1):
        lines.extend(
            [
                f"### {index}. {entry.get('memory_safe') or '（空）'}",
                "",
                "```pam-ledger",
                _format_fields(entry),
                "```",
                "",
            ]
        )
    lines.extend(["## 历史 HA 写入线索", ""])
    for index, entry in enumerate([entry for entry in entries if entry.get("entry_type") == "ha_job_summary"], start=1):
        lines.extend(
            [
                f"### {index}. {entry.get('memory_safe') or '（空）'}",
                "",
                "```pam-ledger",
                _format_fields(entry),
                "```",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _sync_one_entry(
    entry: dict[str, str],
    *,
    action: str,
    dry_run: bool,
    rewrite_text: str = "",
) -> tuple[dict[str, Any], dict[str, str], dict[str, Any] | None]:
    ledger_id = entry.get("ledger_id") or ""
    update = dict(entry)
    update["sync_status"] = "blocked_read_only"
    return _result(ledger_id, "blocked_read_only", FROZEN_LEDGER_MESSAGE), update, None


def _load_mem0_rows(*, limit: int) -> list[dict[str, Any]]:
    mem0 = Path.home() / "bin" / "mem0"
    if not mem0.exists():
        return []
    commands = [
        [str(mem0), "search", "personal_ai_monitor macos_live_runtime", str(min(max(limit, 20), 200))],
        [str(mem0), "search", "source_system=personal_ai_monitor", str(min(max(limit, 20), 200))],
        [str(mem0), "list"],
    ]
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for command in commands:
        result = _run_mem0(command, timeout=30)
        if not result.get("ok"):
            continue
        for item in _extract_mem0_items(result.get("payload")):
            key = str(item.get("id") or item.get("hash") or item.get("memory") or "")[:200]
            if not key or key in seen:
                continue
            seen.add(key)
            rows.append(item)
            if len(rows) >= limit:
                return rows
    return rows


def _entries_from_ha_jobs(conn: sqlite3.Connection, *, limit: int) -> list[dict[str, str]]:
    if limit <= 0:
        return []
    try:
        rows = conn.execute(
            """
            SELECT id, hour_key, package_id, completed_at, written_memory_summaries,
                   memories_written_count, write_audit_summary
            FROM ha_automation_jobs
            WHERE COALESCE(written_memory_summaries, '') != ''
            ORDER BY COALESCE(completed_at, updated_at, created_at) DESC
            LIMIT ?
            """,
            (max(limit, 1),),
        ).fetchall()
    except sqlite3.Error:
        return []
    entries: list[dict[str, str]] = []
    for row in rows:
        summaries = _decode_json_list(row["written_memory_summaries"])
        for index, summary in enumerate(summaries, start=1):
            safe = _safe_text(str(summary), max_len=500)
            ledger_id = f"ha-job:{row['id']}:{index}"
            entries.append(
                {
                    "ledger_id": ledger_id,
                    "entry_type": "ha_job_summary",
                    "can_sync": "false",
                    "memory_id": "",
                    "current_hash": _hash_text(safe),
                    "user_id": "local-owner",
                    "source_system": "personal_ai_monitor",
                    "source_platform": "macos_live_runtime",
                    "source_profile": "activity-analysis",
                    "package_id": _safe_text(str(row["package_id"] or ""), max_len=120),
                    "hour_key": _safe_text(str(row["hour_key"] or ""), max_len=120),
                    "subject_scope": "",
                    "source_category": "",
                    "memory_scope": "",
                    "memory_write_policy": "",
                    "sync_action": "keep",
                    "sync_confirm": "",
                    "rewrite_memory": "",
                    "review_note": "",
                    "sync_status": "needs_manual_lookup",
                    "last_synced_at": "",
                    "memory_safe": safe,
                    "created_at": _safe_text(str(row["completed_at"] or ""), max_len=120),
                }
            )
            if len(entries) >= limit:
                return entries
    return entries


def _entry_from_mem0(row: dict[str, Any]) -> dict[str, str]:
    raw_memory = str(row.get("memory") or "")
    metadata = dict(row.get("metadata") if isinstance(row.get("metadata"), dict) else {})
    metadata = {**_inline_metadata(raw_memory), **metadata}
    memory = _human_memory_text(raw_memory)
    memory_id = _safe_text(str(row.get("id") or row.get("memory_id") or ""), max_len=120)
    return {
        "ledger_id": f"mem0:{memory_id or _hash_text(memory)}",
        "entry_type": "mem0",
        "can_sync": "true" if memory_id else "false",
        "memory_id": memory_id,
        "current_hash": _memory_hash(row),
        "user_id": _safe_text(_item_user_id(row), max_len=80),
        "source_system": _meta(metadata, "source_system"),
        "source_platform": _meta(metadata, "source_platform"),
        "source_profile": _meta(metadata, "source_profile"),
        "package_id": _meta(metadata, "package_id"),
        "hour_key": _meta(metadata, "hour_key"),
        "subject_scope": _meta(metadata, "subject_scope"),
        "source_category": _meta(metadata, "source_category"),
        "memory_scope": _meta(metadata, "memory_scope"),
        "memory_write_policy": _meta(metadata, "memory_write_policy"),
        "sync_action": "keep",
        "sync_confirm": "",
        "rewrite_memory": "",
        "review_note": "",
        "sync_status": "ready" if memory_id else "needs_manual_lookup",
        "last_synced_at": "",
        "memory_safe": memory,
        "created_at": _safe_text(str(row.get("created_at") or ""), max_len=120),
    }


def _merge_previous_editable_fields(entry: dict[str, str], previous: dict[str, str] | None) -> None:
    if not previous:
        return
    for key in EDITABLE_FIELDS:
        value = _clean_editable_value(key, previous.get(key) or "")
        if value:
            entry[key] = value
    if previous.get("sync_status") and previous.get("sync_status") != "ready":
        entry["sync_status"] = previous["sync_status"]
    if previous.get("last_synced_at"):
        entry["last_synced_at"] = previous["last_synced_at"]


def _read_ledger_entries(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    entries: list[dict[str, str]] = []
    comment_matches = list(LEDGER_COMMENT_RE.finditer(text))
    if comment_matches:
        previous_end = 0
        for match in comment_matches:
            section = text[previous_end:match.start()]
            fields = _parse_fields(match.group(1))
            _merge_visible_edit_fields(fields, section)
            if fields.get("ledger_id"):
                entries.append(fields)
            previous_end = match.end()
        return entries
    for match in LEDGER_BLOCK_RE.finditer(text):
        fields = _parse_fields(match.group(1))
        if fields.get("ledger_id"):
            entries.append(fields)
    return entries


def _read_human_memory_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines: list[str] = []
    for raw in text.splitlines():
        match = re.match(r"^\s*\d+\.\s+(.+?)\s*$", raw)
        if match:
            value = _safe_text(match.group(1), max_len=1200)
            if value:
                lines.append(value)
    return lines


def _human_ledger_format(path: Path) -> str:
    if not path.exists():
        return "missing"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "missing"
    if "pam-ledger" in text or "### 记忆内容" in text or "### 人工处理" in text:
        return "legacy"
    return "simple"


def _plan_human_ledger_changes(entries: list[dict[str, str]], human_lines: list[str]) -> dict[str, Any]:
    current_entries = [entry for entry in entries if entry.get("entry_type") == "mem0" and entry.get("sync_status") != "deleted"]
    old_lines = [entry.get("memory_safe") or "" for entry in current_entries]
    old_norm = [_normalize_memory_line(line) for line in old_lines]
    new_norm = [_normalize_memory_line(line) for line in human_lines]
    changes: list[dict[str, Any]] = []
    if not current_entries and human_lines:
        for line in human_lines:
            changes.append({"action": "unmatched_new_line", "line": line, "message": "New line has no detailed record; skipped."})
        return _change_plan(changes)
    if old_norm == new_norm:
        return _change_plan(changes)
    if sorted(old_norm) == sorted(new_norm):
        for entry in current_entries:
            changes.append(
                {
                    "action": "complex_change",
                    "entry": entry,
                    "ledger_id": entry.get("ledger_id", ""),
                    "message": "Line order changed; skipped to avoid deleting the wrong memory.",
                }
            )
        return _change_plan(changes)
    matcher = SequenceMatcher(a=old_norm, b=new_norm, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        old_block = current_entries[i1:i2]
        new_block = human_lines[j1:j2]
        if tag == "delete":
            for entry in old_block:
                changes.append({"action": "delete", "entry": entry})
            continue
        if tag == "insert":
            for line in new_block:
                status = "complex_change" if _normalize_memory_line(line) in old_norm else "unmatched_new_line"
                changes.append({"action": status, "line": line, "message": "Inserted line is not tied to an existing memory; skipped."})
            continue
        if tag == "replace" and len(old_block) == 1 and len(new_block) == 1:
            changes.append({"action": "rewrite", "entry": old_block[0], "new_text": new_block[0]})
            continue
        for entry in old_block:
            changes.append(
                {
                    "action": "complex_change",
                    "entry": entry,
                    "ledger_id": entry.get("ledger_id", ""),
                    "message": "Multi-line replacement skipped to avoid misapplying edits.",
                }
            )
        for line in new_block:
            changes.append({"action": "unmatched_new_line", "line": line, "message": "Replacement line was part of a complex change; skipped."})
    return _change_plan(changes)


def _change_plan(changes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "changes": changes,
        "pending_action_count": sum(1 for item in changes if item.get("action") in {"delete", "rewrite"}),
        "unmatched_new_line_count": sum(1 for item in changes if item.get("action") == "unmatched_new_line"),
        "complex_change_count": sum(1 for item in changes if item.get("action") == "complex_change"),
    }


def _normalize_memory_line(value: str) -> str:
    return " ".join(str(value or "").strip().split())


def _apply_entry_updates(entries: list[dict[str, str]], updates: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    merged: list[dict[str, str]] = []
    for entry in entries:
        ledger_id = entry.get("ledger_id")
        merged.append(dict(updates.get(ledger_id, entry)) if ledger_id else dict(entry))
    return merged


def _rewrite_ledger_blocks(path: Path, updates: dict[str, dict[str, str]]) -> None:
    raise RuntimeError(FROZEN_LEDGER_MESSAGE)


def _parse_fields(block: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for raw in block.splitlines():
        if ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        key = key.strip()
        if not key:
            continue
        fields[key] = _strip_field_value(value)
    return fields


def _format_fields(entry: dict[str, str]) -> str:
    lines = []
    for key in FIELD_ORDER:
        value = _single_line(entry.get(key) or "")
        lines.append(f"{key}: {value}")
    return "\n".join(lines)


def _format_hidden_ledger_block(entry: dict[str, str]) -> str:
    return "<!-- pam-ledger\n" + _format_fields(entry) + "\n-->"


def _merge_visible_edit_fields(fields: dict[str, str], section: str) -> None:
    mapping = {
        "sync_action": "处理动作",
        "sync_confirm": "确认码",
        "rewrite_memory": "改写后的记忆",
        "review_note": "人工备注",
    }
    for key, label in mapping.items():
        value = _visible_field_value(section, label)
        if value is not None:
            fields[key] = _clean_editable_value(key, value)


def _visible_field_value(section: str, label: str) -> str | None:
    pattern = rf"^[ \t]*[-*]?[ \t]*{re.escape(label)}[ \t]*[：:][ \t]*(.*?)[ \t]*$"
    match = re.search(pattern, section, re.M)
    if not match:
        return None
    value = match.group(1).strip()
    if value in {"", "（空）", "(空)", "留空", "无"}:
        return ""
    if len(value) >= 2 and value[0] == value[-1] == "`":
        value = value[1:-1].strip()
    return value


def _clean_editable_value(key: str, value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    if key == "sync_action":
        return _normalize_action(value)
    if key == "sync_confirm":
        return value if value in {DELETE_CONFIRM, REWRITE_CONFIRM} else ""
    if value.startswith("- ") or value.startswith("### "):
        return ""
    if key == "rewrite_memory" and value in {"人工备注：", "review_note:"}:
        return ""
    if key == "review_note" and value.startswith("来源"):
        return ""
    return value


def _entry_title(entry: dict[str, str]) -> str:
    prefix = "记忆" if entry.get("entry_type") == "mem0" else "历史线索"
    memory = entry.get("memory_safe") or ""
    title = re.sub(r"^[：:，,。\s]+", "", memory).strip()
    title = title[:42].rstrip("，。；;、 ")
    return f"{prefix}：{title or entry.get('ledger_id') or '未命名'}"


def _entry_type_label(entry: dict[str, str]) -> str:
    if entry.get("entry_type") == "mem0":
        return "mem0 当前长期记忆"
    if entry.get("entry_type") == "ha_job_summary":
        return "旧 HA 写入摘要线索"
    return entry.get("entry_type") or "未知"


def _subject_scope_label(value: str | None) -> str:
    labels = {
        "self_or_user_owned": "明确属于 Local Owner / Alex 自己",
        "user_workflow_only": "只表示 Local Owner 的工作流或处理动作",
        "third_party_subject": "第三方主体",
        "ambiguous_subject": "主体不清",
    }
    value = str(value or "").strip()
    return labels.get(value, value or "未标注")


def _source_category_label(value: str | None) -> str:
    labels = {
        "self_resume": "自用简历/自我材料",
        "self_resume_safe_summary": "自用简历安全摘要",
        "target_role_jd": "目标岗位 JD / 公司要求",
        "third_party_resume": "第三方简历/候选人资料",
        "interview_notes": "面试/复盘笔记",
        "ambiguous_document": "主体不明文档",
        "memory_governance_rule": "记忆治理规则",
    }
    value = str(value or "").strip()
    return labels.get(value, value or "未标注")


def _source_line(entry: dict[str, str]) -> str:
    parts = []
    if entry.get("source_system"):
        parts.append(entry["source_system"])
    if entry.get("source_platform"):
        parts.append(entry["source_platform"])
    if entry.get("source_profile"):
        parts.append(entry["source_profile"])
    if entry.get("package_id"):
        parts.append(entry["package_id"])
    if entry.get("hour_key"):
        parts.append(entry["hour_key"])
    if entry.get("created_at"):
        parts.append(f"created_at={entry['created_at']}")
    return " / ".join(parts) if parts else "未标注"


def _human_memory_text(raw: str) -> str:
    clean = _strip_inline_metadata(str(raw or ""))
    clean = clean.replace("Personal AI Monitor hourly activity analysis memory. Fact:", "").strip()
    clean = _safe_text(clean, max_len=1200)
    if not clean:
        return ""
    translated = _known_english_memory_to_chinese(clean)
    if translated:
        return translated
    if _contains_cjk(clean):
        return clean
    return f"原记忆为英文，中文台账暂保留其含义原文：{clean}"


def _contains_cjk(value: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", value or ""))


def _strip_inline_metadata(value: str) -> str:
    text = str(value or "")
    return re.sub(r"\s*(?:Metadata|metadata)\s*[:=]\s*\{?.*$", "", text, flags=re.I | re.S).strip()


def _inline_metadata(value: str) -> dict[str, str]:
    match = re.search(r"(?:Metadata|metadata)\s*[:=]\s*\{?(.*)$", str(value or ""), re.I | re.S)
    if not match:
        return {}
    blob = match.group(1)
    metadata: dict[str, str] = {}
    for key in [
        "source_system",
        "source_platform",
        "source_profile",
        "package_id",
        "hour_key",
        "user_id",
        "memory_user_id",
        "event_type",
        "written_at",
        "subject_scope",
        "source_category",
        "memory_scope",
        "memory_write_policy",
    ]:
        found = re.search(rf"\b{re.escape(key)}\s*[=:]\s*([^;,}}]+)", blob)
        if found:
            metadata[key] = _safe_text(found.group(1).strip().strip("'\""), max_len=200)
    return metadata


def _known_english_memory_to_chinese(text: str):
    """No personal memory catalogue is distributed in the public source."""
    return ""


def _run_mem0(command: list[str], *, timeout: int) -> dict[str, Any]:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    if completed.returncode != 0:
        return {"ok": False, "error": (completed.stderr or completed.stdout or "").strip()[:500]}
    payload = _json_loads(completed.stdout)
    if payload is None:
        return {"ok": False, "error": "mem0 returned non-json output"}
    return {"ok": True, "payload": payload}


def _mem0_get(memory_id: str) -> dict[str, Any]:
    mem0 = Path.home() / "bin" / "mem0"
    result = _run_mem0([str(mem0), "get", memory_id], timeout=20)
    if not result.get("ok"):
        return result
    payload = result.get("payload")
    if isinstance(payload, dict) and payload.get("ok") is False:
        return {"ok": False, "error": str(payload.get("error") or "mem0 get failed")}
    memory = payload.get("memory") if isinstance(payload, dict) else None
    if isinstance(memory, dict):
        return {"ok": True, "memory": memory}
    return {"ok": False, "error": "mem0 get returned unsupported payload"}


def _mem0_delete(memory_id: str) -> dict[str, Any]:
    return {"ok": False, "error": FROZEN_LEDGER_MESSAGE, "status": "blocked_read_only"}


def _mem0_update(memory_id: str, text: str, metadata: dict[str, Any]) -> dict[str, Any]:
    return {"ok": False, "error": FROZEN_LEDGER_MESSAGE, "status": "blocked_read_only"}


def _extract_mem0_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, str):
        payload = _json_loads(payload)
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("results"), list):
        return [item for item in payload["results"] if isinstance(item, dict)]
    memories = payload.get("memories")
    if isinstance(memories, dict) and isinstance(memories.get("results"), list):
        return [item for item in memories["results"] if isinstance(item, dict)]
    if isinstance(memories, list):
        return [item for item in memories if isinstance(item, dict)]
    return []


def _is_pam_memory(row: dict[str, Any]) -> bool:
    memory = str(row.get("memory") or "").lower()
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    meta_blob = " ".join(f"{key}={value}" for key, value in metadata.items()).lower()
    combined = f"{memory} {meta_blob}"
    return (
        str(metadata.get("source_system") or "").lower() == "personal_ai_monitor"
        or str(metadata.get("source_platform") or "").lower() == "macos_live_runtime"
        or str(metadata.get("source_profile") or "").lower() == "activity-analysis"
        or "source_system=personal_ai_monitor" in combined
        or "source_system: personal_ai_monitor" in combined
        or "package_id=ha_pkg_" in combined
        or ("personal ai monitor" in combined and "activity-analysis" in combined)
    )


def _item_user_id(item: dict[str, Any]) -> str:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    return str(item.get("user_id") or metadata.get("memory_user_id") or metadata.get("user_id") or "").strip()


def _memory_hash(item: dict[str, Any]) -> str:
    value = str(item.get("hash") or "")
    if value:
        return _safe_text(value, max_len=120)
    return _hash_text(str(item.get("memory") or ""))


def _hash_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _correction_metadata(item: dict[str, Any], *, ledger_id: str, previous_hash: str) -> dict[str, Any]:
    metadata = dict(item.get("metadata") if isinstance(item.get("metadata"), dict) else {})
    metadata.update(
        {
            "source_system": "personal_ai_monitor",
            "source_platform": "macos_live_runtime",
            "source_profile": "memory-ledger",
            "event_type": "pam_memory_ledger_correction",
            "memory_scope": metadata.get("memory_scope") or "safe_summary",
            "correction_source": "obsidian_memory_ledger",
            "memory_ledger_id": ledger_id,
            "previous_memory_hash": previous_hash,
            "corrected_at_utc": _now_iso(),
        }
    )
    return metadata


def _write_backup(records: list[dict[str, Any]], *, backup_dir: str | Path | None) -> str:
    root = Path(backup_dir).expanduser() if backup_dir else Path.home() / DEFAULT_BACKUP_RELATIVE_PATH
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"pam-memory-ledger-sync-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    path.chmod(0o600)
    return str(path)


def _safe_json_item(item: dict[str, Any]) -> dict[str, Any]:
    safe = {}
    for key, value in item.items():
        if isinstance(value, dict):
            safe[key] = {str(k): _safe_text(str(v), max_len=1000) for k, v in value.items()}
        else:
            safe[key] = _safe_text(str(value), max_len=1200)
    return safe


def _decode_json_list(value: str) -> list[str]:
    try:
        payload = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    return [str(item) for item in payload if str(item).strip()]


def _json_loads(value: str) -> Any:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return payload
    return payload


def _meta(metadata: dict[str, Any], key: str) -> str:
    return _safe_text(str(metadata.get(key) or ""), max_len=200)


def _safe_text(value: str, max_len: int = 500) -> str:
    text = str(value or "")
    text = LOCAL_PATH_RE.sub("[path_redacted]", text)
    text = SECRET_RE.sub("[secret_redacted]", text)
    text = EMAIL_RE.sub("[email_redacted]", text)
    text = PHONE_RE.sub("[phone_redacted]", text)
    text = " ".join(text.split())
    return text[:max_len]


def _single_line(value: str) -> str:
    return " ".join(str(value or "").replace("```", "` ` `").split())[:1200]


def _strip_field_value(value: str) -> str:
    value = str(value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1].strip()
    return value


def _normalize_action(value: str | None) -> str:
    value = str(value or "").strip().lower()
    if "删除" in value or value.startswith("delete"):
        return "delete"
    if "改写" in value or value.startswith("rewrite"):
        return "rewrite"
    if "保留" in value or value.startswith("keep"):
        return "keep"
    return value if value in {"keep", "delete", "rewrite"} else "keep"


def _to_bool(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _ledger_path(path: str | Path | None) -> Path:
    return Path(path).expanduser() if path else frozen_pam_ledger_path(home=Path.home())


def _detail_path(path: str | Path | None) -> Path:
    return Path(path).expanduser() if path else Path.home() / DEFAULT_DETAIL_RELATIVE_PATH


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_local_label() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")


def _result(ledger_id: str, status: str, message: str) -> dict[str, Any]:
    return {"ledger_id": ledger_id, "status": status, "message": message}
