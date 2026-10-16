from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from .memory_ledger import DEFAULT_LEDGER_RELATIVE_PATH, memory_ledger_status
from .pipeline import parse_time
from .privacy import PERSONAL_MEDICAL_CONTENT_REASON, personal_medical_context_hit


LOCAL_PATH_RE = re.compile(r"(/Users/[^\s\"'`，。；;]*|/tmp/[^\s\"'`，。；;]*|~/[^\s\"'`，。；;]*)", re.I)
SECRET_RE = re.compile(r"(-----BEGIN .*PRIVATE KEY-----|\bsk-[A-Za-z0-9_-]{20,}\b)", re.I)
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
SELF_SUBJECT_HINTS = [
    "local owner",
    "local-owner",
    "bigking",
    "alex",
    "exampleuser",
    "我的简历",
    "my resume",
    "my cv",
    "自我介绍",
]
THIRD_PARTY_SUBJECT_HINTS = [
    "applicant",
    "candidate cv",
    "candidate resume",
    "third_party_subject",
    "ambiguous_subject",
    "subject_ownership_unclear",
    "候选人",
    "应聘者",
    "求职者",
    "他人",
    "别人",
    "对方",
    "张三",
    "李四",
    "王五",
]
PROFILE_DETAIL_HINTS = [
    "简历",
    "resume",
    "cv",
    "教育经历",
    "工作经历",
    "项目经历",
    "实习经历",
    "联系电话",
    "联系方式",
    "手机号",
    "邮箱",
    "email",
    "phone",
    "毕业于",
    "曾任",
    "现任",
    "任职",
    "工作年限",
    "years of experience",
]
JD_REQUIREMENT_HINTS = [
    "jd",
    "job description",
    "position requirement",
    "岗位职责",
    "任职要求",
    "职位描述",
    "招聘要求",
    "招聘公司",
    "公司要求",
    "面试公司要求",
    "岗位要求",
    "岗位适配",
    "emea",
    "apac",
    "亚太",
    "欧洲",
    "北欧",
    "东欧",
    "南欧",
    "渠道打法",
    "区域渠道",
    "gtm",
]
SELF_POSITIONING_HINTS = [
    "local owner 的",
    "alex 的",
    "用户的",
    "user:",
    "用户:",
    "核心定位",
    "长期定位",
    "个人定位",
    "求职定位",
    "职业定位",
    "自有强项",
    "核心能力",
    "擅长",
    "能力",
    "经验",
    "履历",
    "负责",
    "具备",
]
MISATTRIBUTION_GUARDRAIL_HINTS = [
    "纠偏",
    "不得",
    "不能",
    "不要",
    "不应",
    "禁止",
    "不是",
    "而不是",
    "只代表",
    "只能作为",
    "只能写成",
    "不写成",
    "非个人定位",
    "reference material",
    "by default",
    "unless explicitly",
    "not local owner",
]

PAM_METADATA_KEYS = {
    "source_system",
    "source_platform",
    "source_profile",
    "package_id",
    "hour_key",
    "user_id",
    "event_type",
}


def memory_audit(start: str | None = None, end: str | None = None, *, limit: int = 200) -> dict:
    raw = _load_mem0_candidates(limit=limit)
    if not raw.get("ok"):
        return {
            "status": "unavailable",
            "summary": raw.get("error") or "mem0 audit source unavailable",
            "time_range": {"from": start, "to": end},
            "raw_access": False,
            "destructive_actions": False,
            "items": [],
            "issue_counts": {},
        }
    rows = raw.get("items") or []
    items = []
    for row in rows:
        if not _is_pam_memory(row):
            continue
        if not _within_range(row, start, end):
            continue
        item = _audit_item(row)
        if item["issue_types"]:
            items.append(item)
    issue_counts: dict[str, int] = {}
    for item in items:
        for issue in item["issue_types"]:
            issue_counts[issue] = issue_counts.get(issue, 0) + 1
    ledger = memory_ledger_status(ledger_path=Path.home() / DEFAULT_LEDGER_RELATIVE_PATH)
    return {
        "status": "ok",
        "summary": f"发现 {len(items)} 条 Personal AI Monitor / Hermes 长期记忆可疑项；包含 mem0 和 Hermes profile markdown 只读扫描；只报告，不删除、不改写。",
        "time_range": {"from": start, "to": end},
        "raw_access": False,
        "destructive_actions": False,
        "source": raw.get("source") or "mem0_search",
        "ledger": ledger,
        "items_checked": len([row for row in rows if _is_pam_memory(row)]),
        "item_count": len(items),
        "issue_counts": issue_counts,
        "items": items[:limit],
    }


def render_memory_audit_markdown(report: dict) -> str:
    lines = [
        "# Personal AI Monitor 长期记忆体检",
        "",
        "> 只读取 mem0 里已写入的安全记忆摘要和 metadata；不读取截图、OCR、SQLite 原始活动表或完整文档路径；不删除、不改写。",
        "",
        f"- status: `{report.get('status') or 'unknown'}`",
        f"- raw_access: `{str(report.get('raw_access') is True).lower()}`",
        f"- destructive_actions: `{str(report.get('destructive_actions') is True).lower()}`",
        f"- time_range: `{(report.get('time_range') or {}).get('from') or 'unbounded'}` to `{(report.get('time_range') or {}).get('to') or 'unbounded'}`",
        f"- ledger_path: `{((report.get('ledger') or {}).get('ledger_path') or '')}`",
        f"- ledger_pending_action_count: `{((report.get('ledger') or {}).get('pending_action_count') or 0)}`",
        "",
        "## 摘要",
        "",
        report.get("summary") or "暂无摘要。",
        "",
        "## 问题分布",
        "",
    ]
    issue_counts = report.get("issue_counts") or {}
    if issue_counts:
        for issue, count in sorted(issue_counts.items()):
            lines.append(f"- {issue}: `{count}`")
    else:
        lines.append("- 暂无可疑项。")
    lines.extend(["", "## 可疑项", ""])
    for item in report.get("items") or []:
        issues = "、".join(item.get("issue_types") or [])
        lines.append(f"- `{item.get('memory_id') or 'unknown'}` · {issues} · {item.get('safe_summary') or ''}")
        if item.get("recommendation"):
            lines.append(f"  - 建议：{item.get('recommendation')}")
    if not report.get("items"):
        lines.append("- 暂无。")
    return "\n".join(lines).rstrip() + "\n"


def _load_mem0_candidates(*, limit: int) -> dict:
    mem0 = Path.home() / "bin" / "mem0"
    profile_items = _load_profile_memory_candidates(limit=limit)
    if not mem0.exists():
        if profile_items:
            return {"ok": True, "source": "hermes_profile_memory", "items": profile_items[:limit]}
        return {"ok": False, "error": "mem0 CLI not found"}
    commands = [
        [str(mem0), "search", "personal_ai_monitor macos_live_runtime"],
        [str(mem0), "search", "source_system=personal_ai_monitor"],
        [str(mem0), "list"],
    ]
    merged: list[dict] = []
    seen: set[str] = set()
    errors: list[str] = []
    for command in commands:
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=20)
        except Exception as exc:
            errors.append(str(exc))
            continue
        if completed.returncode != 0:
            errors.append((completed.stderr or completed.stdout or "").strip()[:200])
            continue
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            errors.append("mem0 returned non-json output")
            continue
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                errors.append("mem0 returned nested non-json output")
                continue
        if not isinstance(payload, dict):
            errors.append("mem0 returned unsupported output")
            continue
        for item in _extract_mem0_items(payload):
            memory_id = str(item.get("id") or item.get("hash") or "")
            key = memory_id or str(item.get("memory") or "")[:80]
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(item)
            if len(merged) >= limit:
                break
        if len(merged) >= limit:
            break
    if merged:
        for item in profile_items:
            memory_id = str(item.get("id") or "")
            key = memory_id or str(item.get("memory") or "")[:80]
            if key and key not in seen:
                seen.add(key)
                merged.append(item)
            if len(merged) >= limit:
                break
        source = "mem0_cli+hermes_profile_memory" if profile_items else "mem0_cli"
        return {"ok": True, "source": source, "items": merged}
    if profile_items:
        return {"ok": True, "source": "hermes_profile_memory", "items": profile_items[:limit], "mem0_errors": errors}
    return {"ok": False, "error": "; ".join(error for error in errors if error) or "no mem0 memories returned"}


def _load_profile_memory_candidates(*, limit: int) -> list[dict]:
    roots = [
        Path.home() / ".hermes" / "memories",
        Path.home() / ".hermes" / "profiles",
    ]
    files: list[Path] = []
    base = roots[0]
    if base.exists():
        files.extend(path for path in base.glob("*.md") if path.is_file() and not path.name.endswith(".lock"))
    profiles_root = roots[1]
    if profiles_root.exists():
        for profile in profiles_root.iterdir():
            memories = profile / "memories"
            if not memories.is_dir():
                continue
            files.extend(path for path in memories.glob("*.md") if path.is_file() and not path.name.endswith(".lock"))
    rows: list[dict] = []
    for path in sorted(files):
        profile = _profile_from_memory_path(path)
        for block in _profile_memory_blocks(path):
            memory = block["memory"]
            rows.append(
                {
                    "id": f"hermes-profile:{profile}:{path.name}:{block['line_start']}",
                    "memory": memory,
                    "metadata": {
                        "source_system": "hermes_profile_memory",
                        "source_platform": "hermes",
                        "source_profile": profile,
                        "file_name": path.name,
                        "line_start": block["line_start"],
                    },
                }
            )
            if len(rows) >= limit:
                return rows
    return rows


def _profile_memory_blocks(path: Path) -> list[dict]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    blocks: list[dict] = []
    current: list[str] = []
    start_line = 1
    for index, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped == "§" or not stripped:
            if current:
                blocks.append({"line_start": start_line, "memory": " ".join(current).strip()})
                current = []
            start_line = index + 1
            continue
        if not current:
            start_line = index
        current.append(stripped)
    if current:
        blocks.append({"line_start": start_line, "memory": " ".join(current).strip()})
    return [block for block in blocks if block["memory"]]


def _profile_from_memory_path(path: Path) -> str:
    parts = path.parts
    if "profiles" in parts:
        index = parts.index("profiles")
        if index + 1 < len(parts):
            return parts[index + 1]
    return "default"


def _extract_mem0_items(payload: dict) -> list[dict]:
    if isinstance(payload.get("results"), list):
        return [item for item in payload["results"] if isinstance(item, dict)]
    memories = payload.get("memories")
    if isinstance(memories, dict) and isinstance(memories.get("results"), list):
        return [item for item in memories["results"] if isinstance(item, dict)]
    if isinstance(memories, list):
        return [item for item in memories if isinstance(item, dict)]
    return []


def _is_pam_memory(row: dict) -> bool:
    memory = str(row.get("memory") or "").lower()
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    return (
        str(metadata.get("source_system") or "").lower() == "personal_ai_monitor"
        or str(metadata.get("source_system") or "").lower() == "hermes_profile_memory"
        or str(metadata.get("source_platform") or "").lower() == "macos_live_runtime"
        or "source_system=personal_ai_monitor" in memory
        or "source_system: personal_ai_monitor" in memory
        or "package_id=ha_pkg_" in memory
        or "personal ai monitor" in memory and "activity-analysis" in memory
    )


def _within_range(row: dict, start: str | None, end: str | None) -> bool:
    value = str(row.get("created_at") or row.get("updated_at") or "")
    if not value:
        return True
    try:
        created = parse_time(value)
    except (TypeError, ValueError):
        return True
    if start:
        try:
            if created < parse_time(start):
                return False
        except (TypeError, ValueError):
            pass
    if end:
        try:
            if created > parse_time(end):
                return False
        except (TypeError, ValueError):
            pass
    return True


def _audit_item(row: dict) -> dict:
    memory = str(row.get("memory") or "")
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    issue_types = _issue_types(memory, metadata)
    safe_summary = (
        "个人临床医疗长期记忆可疑项：内容已隐藏；请通过 PAM 长期记忆台账复核并删除。"
        if "personal_medical_content_risk" in issue_types
        else _safe_text(memory, 260)
    )
    return {
        "memory_id": _safe_text(str(row.get("id") or ""))[:80],
        "created_at": _safe_text(str(row.get("created_at") or ""))[:80],
        "updated_at": _safe_text(str(row.get("updated_at") or ""))[:80],
        "issue_types": issue_types,
        "safe_summary": safe_summary,
        "metadata_keys": sorted(str(key) for key in metadata.keys())[:20],
        "recommendation": _recommendation(issue_types),
    }


def _issue_types(memory: str, metadata: dict) -> list[str]:
    text = memory.lower()
    issues: list[str] = []
    metadata_blob = " ".join(f"{key}={value}" for key, value in metadata.items()).lower()
    combined = f"{text} {metadata_blob}"
    is_profile_memory = str(metadata.get("source_system") or "").lower() == "hermes_profile_memory"
    missing = [key for key in PAM_METADATA_KEYS if key not in metadata and f"{key}=" not in text and f"{key}:" not in text]
    if missing and not is_profile_memory:
        issues.append("missing_metadata")
    if _contains_any(combined, ["single_evidence", "evidence_count=1", "证据 1 条", "single evidence"]):
        issues.append("weak_single_evidence")
    if _contains_any(combined, ["iina", ".mkv", ".mp4", "webrip", "web-dl", "字幕", "file_listing_context", "media_player_context"]):
        issues.append("media_or_file_listing_noise")
    if _contains_any(combined, ["unknown", "weak_or_no_anchor", "keyword_only", "no_project_tags", "unanchored"]):
        issues.append("unknown_or_unanchored")
    if _contains_any(combined, ["content_duplicate", "near_duplicate", "merged_duplicate", "duplicate_or_near_duplicate", "重复内容", "重复候选"]):
        issues.append("duplicate_or_near_duplicate")
    if PERSONAL_MEDICAL_CONTENT_REASON in combined or "personal_medical_document" in combined or personal_medical_context_hit(combined):
        issues.append("personal_medical_content_risk")
    if _third_party_subject_risk(combined):
        issues.append("third_party_subject_risk")
    if _jd_requirement_as_self_positioning_risk(combined):
        issues.append("jd_requirement_as_self_positioning_risk")
    return issues


def _third_party_subject_risk(text: str) -> bool:
    has_self = _contains_any(text, SELF_SUBJECT_HINTS)
    has_third_party = _contains_any(text, THIRD_PARTY_SUBJECT_HINTS)
    has_profile_detail = _contains_any(text, PROFILE_DETAIL_HINTS)
    if has_third_party:
        return True
    return has_profile_detail and not has_self


def _jd_requirement_as_self_positioning_risk(text: str) -> bool:
    if _contains_any(text, MISATTRIBUTION_GUARDRAIL_HINTS):
        return False
    has_jd_requirement = _contains_any(text, JD_REQUIREMENT_HINTS)
    has_self_positioning = _contains_any(text, SELF_POSITIONING_HINTS)
    if not has_jd_requirement or not has_self_positioning:
        return False
    metadata_allows_reference = _contains_any(
        text,
        [
            "source_category=target_role_jd",
            "source_scope=reference_only",
            "memory_write_policy=reference_only",
            "subject_scope=user_workflow_only",
        ],
    )
    metadata_allows_self_resume = _contains_any(
        text,
        [
            "source_category=self_resume",
            "source_category=self_resume_safe_summary",
            "memory_write_policy=self_resume_safe_summary",
            "subject_scope=self_or_user_owned",
        ],
    )
    if metadata_allows_self_resume:
        return False
    return not metadata_allows_reference


def _recommendation(issue_types: list[str]) -> str:
    issues = set(issue_types)
    if "jd_requirement_as_self_positioning_risk" in issues:
        return "复核是否应改写为“某公司/某岗位要求或准备内容”，不要作为 Local Owner 长期能力/定位保留。"
    if "personal_medical_content_risk" in issues:
        return "复核是否包含 Local Owner 的体检、病历、症状、诊断、用药或病情咨询；如是，应通过 PAM 台账删除，不保留长期记忆。"
    if "third_party_subject_risk" in issues:
        return "复核主体是否为第三方；如是候选人/他人资料，应删除或改写为安全工作流摘要。"
    if "missing_metadata" in issues:
        return "补充来源 metadata 或重新写入带 subject_scope/source_category 的安全摘要。"
    return "人工复核后再决定是否保留、删除或改写。"


def _safe_text(value: str, max_len: int = 500) -> str:
    text = str(value or "")
    text = LOCAL_PATH_RE.sub("[path_redacted]", text)
    text = SECRET_RE.sub("[secret_redacted]", text)
    text = EMAIL_RE.sub("[email_redacted]", text)
    text = " ".join(text.split())
    return text[:max_len]


def _contains_any(text: str, needles: list[str]) -> bool:
    return any(needle.lower() in text for needle in needles)
