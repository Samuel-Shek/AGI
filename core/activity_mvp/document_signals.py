from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from .app_identity import (
    DOCUMENT_EDITOR_FAMILIES,
    DOCUMENT_REFERENCE_FAMILIES,
    resolve_app_identity,
)
from .canonical_paths import obsidian_vault_root
from .privacy import PERSONAL_MEDICAL_RISK_FLAG, personal_high_precision_enabled, personal_medical_context_hit, redact_for_storage


ALLOWED_SUFFIXES = {
    ".md",
    ".markdown",
    ".txt",
    ".rtf",
    ".html",
    ".htm",
    ".doc",
    ".docx",
    ".odt",
    ".pages",
    ".pdf",
}
TEXTUTIL_SUFFIXES = {".rtf", ".html", ".htm", ".doc", ".docx", ".odt"}
PLAIN_SUFFIXES = {".md", ".markdown", ".txt"}
MAX_TEXT_BYTES = 512_000
MAX_CONTEXT_CHARS = 1200
MAX_PERSONAL_CANDIDATE_FILES = 20000
MIN_PERSONAL_MATCH_SCORE = 6.0
MIN_PERSONAL_DIAGNOSTIC_SCORE = 3.0
PERSONAL_DOCUMENT_ROOT_ENV = "PAM_PERSONAL_DOCUMENT_ROOTS"
BLACKLIST_PATH_PARTS = {
    "Library/Keychains",
    "Library/Application Support/Google/Chrome",
    "Library/Application Support/Dia",
    "Library/Application Support/Arc",
    ".ssh",
    ".gnupg",
    ".trash",
    ".Trash",
    "1Password",
    "Bitwarden",
}
SENSITIVE_SUFFIXES = {
    ".key",
    ".pem",
    ".p12",
    ".pfx",
    ".sqlite",
    ".db",
    ".csv",
}
DOCUMENT_HIGH_VALUE_TERMS = {
    "简历",
    "resume",
    "cv",
    "面试",
    "岗位",
    "求职",
    "应聘",
    "自我介绍",
    "star",
}
DOCUMENT_CATEGORY_SELF_RESUME = "self_resume"
DOCUMENT_CATEGORY_TARGET_ROLE_JD = "target_role_jd"
DOCUMENT_CATEGORY_THIRD_PARTY_RESUME = "third_party_resume"
DOCUMENT_CATEGORY_INTERVIEW_NOTES = "interview_notes"
DOCUMENT_CATEGORY_PERSONAL_MEDICAL = "personal_medical_document"
DOCUMENT_CATEGORY_AMBIGUOUS = "ambiguous_document"
DOCUMENT_CATEGORY_GENERAL = "general_document"
DOCUMENT_SCOPE_SELF_OWNED = "self_owned"
DOCUMENT_SCOPE_REFERENCE_ONLY = "reference_only"
DOCUMENT_SCOPE_THIRD_PARTY = "third_party"
DOCUMENT_SCOPE_CONTEXT_ONLY = "context_only"
DOCUMENT_SCOPE_AMBIGUOUS = "ambiguous"
DOCUMENT_SCOPE_PROTECTED = "protected"
DOCUMENT_MEMORY_POLICY_SELF_SUMMARY = "self_resume_safe_summary"
DOCUMENT_MEMORY_POLICY_WORKFLOW_ONLY = "workflow_only"
DOCUMENT_MEMORY_POLICY_REFERENCE_ONLY = "reference_only"
DOCUMENT_MEMORY_POLICY_BLOCKED = "blocked_subject"
DOCUMENT_MEMORY_POLICY_PERSONAL_MEDICAL_BLOCKED = "blocked_personal_medical"
DOCUMENT_SELF_HINTS = {
    "alex",
    "exampleuser",
    "local owner",
    "local-owner",
    "bigking",
    "示例用户",
    "我的简历",
    "我的 cv",
    "my resume",
    "my cv",
}
DOCUMENT_THIRD_PARTY_HINTS = {
    "candidate cv",
    "candidate resume",
    "candidate profile",
    "applicant",
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
DOCUMENT_JD_HINTS = {
    "jd",
    "job description",
    "岗位职责",
    "任职要求",
    "职位描述",
    "职位要求",
    "招聘职位",
    "招聘公司",
    "岗位要求",
    "招聘要求",
    "公司要求",
    "面试公司要求",
}
DOCUMENT_RESUME_HINTS = {"简历", "resume", "cv", "curriculum vitae"}
DOCUMENT_INTERVIEW_HINTS = {"面试", "自我介绍", "star", "公司介绍", "面试问题", "interview"}
DOCUMENT_EDITOR_APPS = {
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
DOCUMENT_REFERENCE_APPS = {
    "finder",
    "launchpad",
    "dock",
    "preview",
    "photos",
    "quicktime",
    "iina",
}
DOCUMENT_RECENT_MODIFIED_SECONDS = 30 * 60
DOCUMENT_CLOCK_SKEW_SECONDS = 60
DOCUMENT_PROFILE_DETAIL_HINTS = {
    "教育经历",
    "工作经历",
    "项目经历",
    "实习经历",
    "联系方式",
    "联系电话",
    "手机号",
    "手机",
    "邮箱",
    "email",
    "phone",
    "毕业于",
    "曾任",
    "现任",
    "任职",
}
SELF_RESUME_SAFE_TERMS = [
    ("ceo office", "CEO Office"),
    ("ceo 助理", "CEO 助理"),
    ("executive assistant", "CEO 助理"),
    ("overseas business", "海外商务"),
    ("海外商务", "海外商务"),
    ("project manager", "项目经理"),
    ("项目经理", "项目经理"),
    ("project management", "项目管理"),
    ("gtm", "GTM"),
    ("go-to-market", "GTM"),
    ("b2b", "B2B"),
    ("b2g", "B2G"),
    ("market research", "市场/竞品研究"),
    ("竞品研究", "市场/竞品研究"),
    ("客户需求", "客户需求拆解"),
    ("proposal", "方案/报价"),
    ("quotation", "方案/报价"),
    ("contract", "合同谈判"),
    ("合同", "合同谈判"),
    ("cross-cultural", "跨文化推进"),
    ("跨文化", "跨文化推进"),
    ("channel", "渠道运营"),
    ("渠道", "渠道运营"),
    ("kol", "KOL/跨境电商"),
    ("sop", "SOP/知识库"),
    ("knowledge base", "SOP/知识库"),
    ("ai tools", "AI 工具工作流"),
    ("ai 工具", "AI 工具工作流"),
]
TOKEN_STOPWORDS = {
    "file",
    "edit",
    "view",
    "window",
    "help",
    "menu",
    "search",
    "open",
    "sidebar",
    "unknown",
    "obsidian",
    "codex",
    "chrome",
    "safari",
    "dia",
    "文件",
    "编辑",
    "显示",
    "窗口",
    "帮助",
    "搜索",
}


def build_document_signal(root_dir: str | Path, event: dict) -> dict | None:
    path = document_path_from_event(event)
    source = str(event.get("document_source") or "foreground_document")
    candidate_score = 1.0
    candidate_reason = ""
    # v0.3.11: screenshot/title-driven recursive discovery is not evidence of an
    # edit and is disabled. Only an explicit foreground document path remains a
    # DocumentSignal; confirmed edits come from document_changes.
    if path is None:
        return None
    allowed, reason = _path_allowed(path)
    if not allowed:
        return {
            "status": "rejected",
            "reason": reason,
            "privacy_flags": ["document_signal_rejected"],
        }
    text_result = _read_front_document_text(path)
    visible_text = _visible_text(event)
    if text_result["text"]:
        match = _match_visible_text(text_result["text"], visible_text)
        if not match["matched"] and candidate_score >= MIN_PERSONAL_MATCH_SCORE:
            match = {
                "matched": True,
                "location": f"candidate_score:{candidate_score:.1f}:{candidate_reason or 'personal_document_candidate'}",
                "context": _candidate_context_from_text(text_result["text"], visible_text),
            }
        if not match["matched"]:
            context_text = text_result.get("summary") or f"{path.suffix.lower() or 'document'} foreground document candidate"
            semantic_match = False
            match_status = "low_score_candidate" if candidate_score >= MIN_PERSONAL_DIAGNOSTIC_SCORE else "no_semantic_match"
        else:
            context_text = match["context"]
            semantic_match = True
            match_status = "matched_text"
    else:
        match = {"matched": True, "location": text_result.get("location_hint") or "metadata_only"}
        context_text = text_result.get("summary") or ""
        semantic_match = False
        match_status = "metadata_only"

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stat_result = path.stat()
    mtime_state = _document_mtime_state(event, stat_result, source)
    assessment = _document_source_assessment(path, text_result.get("text") or "", visible_text, context_text, event)
    is_personal_medical_document = assessment["document_source_category"] == DOCUMENT_CATEGORY_PERSONAL_MEDICAL
    file_name_redacted = _protected_document_file_name(path) if is_personal_medical_document else _redact_file_name(path.name)
    change_summary = redact_for_storage(
        assessment.get("safe_summary") or context_text or f"{file_name_redacted} opened in foreground",
        max_len=420,
    )
    signal = {
        "captured_at": str(event.get("captured_at") or now),
        "app_name": str(event.get("app_name") or "Unknown"),
        "file_ref": _path_hmac(root_dir, path),
        "file_name_redacted": file_name_redacted,
        "file_type": _file_type(path),
        "file_modified_at": datetime.fromtimestamp(stat_result.st_mtime, timezone.utc).isoformat(timespec="seconds"),
        "content_hash_at_capture": _content_hash(path, text_result.get("text") or ""),
        "subsequent_mtime_event": mtime_state,
        "visible_location_hint": "protected_medical_document"
        if is_personal_medical_document
        else redact_for_storage(str(match.get("location") or candidate_reason or "foreground_document"), max_len=120),
        "topic_tags": _protected_document_tags(path) if is_personal_medical_document else _topic_tags(context_text, path),
        "change_summary_redacted": change_summary,
        "document_source_category": assessment["document_source_category"],
        "document_source_scope": assessment["document_source_scope"],
        "document_subject_scope": assessment["document_subject_scope"],
        "document_memory_policy": assessment["document_memory_policy"],
        "document_classification_confidence": assessment["document_classification_confidence"],
        "document_classification_reasons": assessment["document_classification_reasons"],
        "document_risk_flags": assessment["document_risk_flags"],
        "document_parse_status": text_result.get("parse_status") or ("text_extracted" if text_result.get("text") else "metadata_only"),
        "document_parse_method": text_result.get("parse_method") or "",
        "privacy_flags": sorted(set(["document_signal", *text_result.get("privacy_flags", []), *(["personal_medical_document"] if is_personal_medical_document else [])])),
        "redaction_confidence": 0.99
        if is_personal_medical_document
        else (min(0.96, max(0.62, 0.62 + (candidate_score / 20.0))) if source == "personal_high_precision_candidate" else (0.88 if text_result["text"] else 0.72)),
        "redaction_status": "redacted",
        "semantic_match": semantic_match,
        "match_status": match_status,
        "source": source,
        "source_confidence": min(0.96, max(0.5, candidate_score / 12.0)) if source == "personal_high_precision_candidate" else (0.86 if text_result["text"] else 0.62),
        "context_text_for_privacy": "" if is_personal_medical_document else (context_text[:MAX_CONTEXT_CHARS] if semantic_match else ""),
    }
    return signal


def document_path_from_event(event: dict) -> Path | None:
    direct = str(event.get("document_path") or "").strip()
    if direct:
        return Path(direct).expanduser()
    url = str(event.get("url") or "")
    if not url.startswith("file://"):
        return None
    parsed = urlparse(url)
    path = unquote(parsed.path)
    return Path(path).expanduser() if path else None


def public_document_signal(signal: dict | None) -> dict | None:
    if not signal or signal.get("status") == "rejected":
        return None
    if signal.get("semantic_match") is False or signal.get("match_status") == "metadata_only":
        return None
    return {
        "captured_at": signal.get("captured_at"),
        "app_name": signal.get("app_name"),
        "file_ref": signal.get("file_ref"),
        "file_name_redacted": signal.get("file_name_redacted"),
        "file_type": signal.get("file_type"),
        "file_modified_at": signal.get("file_modified_at"),
        "content_hash_at_capture": signal.get("content_hash_at_capture"),
        "subsequent_mtime_event": signal.get("subsequent_mtime_event") or "",
        "visible_location_hint": signal.get("visible_location_hint"),
        "topic_tags": signal.get("topic_tags") or [],
        "change_summary_redacted": signal.get("change_summary_redacted"),
        "document_source_category": signal.get("document_source_category") or DOCUMENT_CATEGORY_GENERAL,
        "document_source_scope": signal.get("document_source_scope") or DOCUMENT_SCOPE_CONTEXT_ONLY,
        "document_subject_scope": signal.get("document_subject_scope") or DOCUMENT_SCOPE_CONTEXT_ONLY,
        "document_memory_policy": signal.get("document_memory_policy") or DOCUMENT_MEMORY_POLICY_WORKFLOW_ONLY,
        "document_classification_confidence": signal.get("document_classification_confidence") or 0.0,
        "document_classification_reasons": signal.get("document_classification_reasons") or [],
        "document_risk_flags": signal.get("document_risk_flags") or [],
        "document_parse_status": signal.get("document_parse_status") or "",
        "document_parse_method": signal.get("document_parse_method") or "",
        "privacy_flags": signal.get("privacy_flags") or [],
        "redaction_confidence": signal.get("redaction_confidence") or 0.0,
        "redaction_status": signal.get("redaction_status") or "redacted",
        "semantic_match": bool(signal.get("semantic_match") is not False),
        "match_status": signal.get("match_status") or "matched_text",
        "source": signal.get("source") or "foreground_document",
        "source_confidence": signal.get("source_confidence") or 0.0,
    }


def _document_mtime_state(event: dict, stat_result: os.stat_result, source: str) -> str:
    identity = resolve_app_identity(event.get("app_name"), event.get("bundle_id"))
    if source == "personal_high_precision_candidate":
        return "metadata_reference"
    if identity.matched and identity.family in DOCUMENT_REFERENCE_FAMILIES:
        return "reference_app_visible"
    captured = _parse_event_time(str(event.get("captured_at") or ""))
    modified = datetime.fromtimestamp(stat_result.st_mtime, timezone.utc)
    recently_modified = False
    if captured is not None:
        delta = (captured - modified).total_seconds()
        recently_modified = -DOCUMENT_CLOCK_SKEW_SECONDS <= delta <= DOCUMENT_RECENT_MODIFIED_SECONDS
    if identity.matched and identity.family in DOCUMENT_EDITOR_FAMILIES:
        return "modified_near_capture" if recently_modified else "opened_without_recent_modify"
    return "viewed_without_recent_modify"


def _parse_event_time(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_front_document_text(path: Path) -> dict:
    suffix = path.suffix.lower()
    if suffix in PLAIN_SUFFIXES:
        data = path.read_bytes()[:MAX_TEXT_BYTES]
        return {"text": data.decode("utf-8", errors="replace"), "privacy_flags": [], "parse_status": "text_extracted", "parse_method": "plain_text"}
    if suffix in TEXTUTIL_SUFFIXES:
        completed = subprocess.run(
            ["textutil", "-convert", "txt", "-stdout", str(path)],
            check=False,
            timeout=8,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        text = completed.stdout[:MAX_TEXT_BYTES].decode("utf-8", errors="replace") if completed.returncode == 0 else ""
        return {
            "text": text,
            "privacy_flags": ["document_textutil_extracted"] if text else ["document_metadata_only"],
            "parse_status": "text_extracted" if text else "metadata_only",
            "parse_method": "textutil" if text else "",
        }
    if suffix == ".pdf":
        return _read_pdf_document_text(path)
    return {
        "text": "",
        "summary": f"{path.suffix.lower() or 'document'} foreground document metadata",
        "location_hint": "metadata_only",
        "privacy_flags": ["document_metadata_only"],
        "parse_status": "metadata_only",
        "parse_method": "",
    }


def _read_pdf_document_text(path: Path) -> dict:
    mdls_text = _read_pdf_mdls_text(path)
    if mdls_text:
        return {
            "text": mdls_text,
            "privacy_flags": ["document_pdf_text_extracted"],
            "parse_status": "text_extracted",
            "parse_method": "mdls_kMDItemTextContent",
        }
    strings_text = _read_pdf_strings_text(path)
    if strings_text:
        return {
            "text": strings_text,
            "privacy_flags": ["document_pdf_strings_extracted"],
            "parse_status": "text_extracted",
            "parse_method": "pdf_printable_strings",
        }
    return {
        "text": "",
        "summary": ".pdf foreground document metadata",
        "location_hint": "metadata_only",
        "privacy_flags": ["document_metadata_only"],
        "parse_status": "metadata_only",
        "parse_method": "",
    }


def _read_pdf_mdls_text(path: Path) -> str:
    try:
        completed = subprocess.run(
            ["mdls", "-raw", "-name", "kMDItemTextContent", str(path)],
            check=False,
            timeout=8,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    text = completed.stdout[:MAX_TEXT_BYTES].decode("utf-8", errors="replace")
    text = text.strip()
    if not text or text == "(null)":
        return ""
    return text


def _read_pdf_strings_text(path: Path) -> str:
    try:
        raw = path.read_bytes()[:MAX_TEXT_BYTES]
    except OSError:
        return ""
    decoded = raw.decode("latin-1", errors="ignore")
    chunks = re.findall(r"[A-Za-z0-9\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff ,.;:()/_+\-@#%&\[\]{}]{3,}", decoded)
    text = " ".join(chunk.strip() for chunk in chunks if chunk.strip())
    if len(text.strip()) < 40:
        return ""
    return text[:MAX_TEXT_BYTES]


def _path_allowed(path: Path) -> tuple[bool, str]:
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return False, "document_path_missing"
    suffix = resolved.suffix.lower()
    if suffix in SENSITIVE_SUFFIXES:
        return False, "sensitive_document_type"
    if suffix not in ALLOWED_SUFFIXES:
        return False, "unsupported_document_type"
    normalized = str(resolved)
    if any(part.startswith(".") for part in resolved.parts if part not in {resolved.anchor, "."}):
        return False, "hidden_document_location"
    for part in BLACKLIST_PATH_PARTS:
        if part in normalized:
            return False, "blacklisted_document_location"
    if not resolved.is_file():
        return False, "document_path_not_file"
    return True, "ok"


def _document_source_assessment(path: Path, document_text: str, visible_text: str, context_text: str, event: dict) -> dict:
    name_blob = " ".join(str(part or "") for part in [path.name, path.stem, " ".join(path.parts[-5:])]).lower()
    text_blob = " ".join(
        str(part or "")
        for part in [
            name_blob,
            document_text[:12000],
            visible_text,
            context_text,
            event.get("app_name") or "",
            event.get("window_title") or "",
        ]
    ).lower()
    has_self = _contains_any(text_blob, DOCUMENT_SELF_HINTS)
    has_resume = _contains_any(text_blob, DOCUMENT_RESUME_HINTS)
    has_jd = _contains_any(text_blob, DOCUMENT_JD_HINTS)
    has_third_party = _contains_any(text_blob, DOCUMENT_THIRD_PARTY_HINTS)
    has_profile_detail = _contains_any(text_blob, DOCUMENT_PROFILE_DETAIL_HINTS)
    has_interview = _contains_any(text_blob, DOCUMENT_INTERVIEW_HINTS)
    has_personal_medical = personal_medical_context_hit(text_blob)
    reasons: list[str] = []
    risks: list[str] = []

    if has_personal_medical:
        category = DOCUMENT_CATEGORY_PERSONAL_MEDICAL
        scope = DOCUMENT_SCOPE_PROTECTED
        subject_scope = "self_or_user_owned"
        policy = DOCUMENT_MEMORY_POLICY_PERSONAL_MEDICAL_BLOCKED
        confidence = 0.92
        reasons.append("personal_medical_document_signal")
        risks.extend(["personal_medical_document", PERSONAL_MEDICAL_RISK_FLAG])
    elif has_third_party and has_resume:
        category = DOCUMENT_CATEGORY_THIRD_PARTY_RESUME
        scope = DOCUMENT_SCOPE_THIRD_PARTY
        subject_scope = "third_party_subject"
        policy = DOCUMENT_MEMORY_POLICY_BLOCKED
        confidence = 0.9
        reasons.append("third_party_resume_signal")
        risks.append("third_party_subject")
    elif has_self and has_resume:
        category = DOCUMENT_CATEGORY_SELF_RESUME
        scope = DOCUMENT_SCOPE_SELF_OWNED
        subject_scope = "self_or_user_owned"
        policy = DOCUMENT_MEMORY_POLICY_SELF_SUMMARY
        confidence = 0.88
        reasons.append("explicit_self_resume_signal")
        if has_jd:
            risks.append("contains_role_requirement_terms")
    elif has_jd:
        category = DOCUMENT_CATEGORY_TARGET_ROLE_JD
        scope = DOCUMENT_SCOPE_REFERENCE_ONLY
        subject_scope = "user_workflow_only"
        policy = DOCUMENT_MEMORY_POLICY_REFERENCE_ONLY
        confidence = 0.86
        reasons.append("job_description_or_role_requirement_signal")
        risks.append("reference_only_job_description")
    elif has_resume or has_profile_detail:
        category = DOCUMENT_CATEGORY_AMBIGUOUS
        scope = DOCUMENT_SCOPE_AMBIGUOUS
        subject_scope = "ambiguous_subject"
        policy = DOCUMENT_MEMORY_POLICY_REFERENCE_ONLY
        confidence = 0.74
        reasons.append("resume_or_profile_without_self_signal")
        risks.append("subject_ownership_unclear")
    elif has_interview:
        category = DOCUMENT_CATEGORY_INTERVIEW_NOTES
        scope = DOCUMENT_SCOPE_CONTEXT_ONLY
        subject_scope = "user_workflow_only"
        policy = DOCUMENT_MEMORY_POLICY_WORKFLOW_ONLY
        confidence = 0.76
        reasons.append("interview_workflow_document")
    else:
        category = DOCUMENT_CATEGORY_GENERAL
        scope = DOCUMENT_SCOPE_CONTEXT_ONLY
        subject_scope = "user_workflow_only"
        policy = DOCUMENT_MEMORY_POLICY_WORKFLOW_ONLY
        confidence = 0.66
        reasons.append("general_foreground_document")

    return {
        "document_source_category": category,
        "document_source_scope": scope,
        "document_subject_scope": subject_scope,
        "document_memory_policy": policy,
        "document_classification_confidence": round(confidence, 2),
        "document_classification_reasons": reasons[:6],
        "document_risk_flags": risks[:8],
        "safe_summary": _document_safe_summary(category, policy, context_text or document_text, path),
    }


def _document_safe_summary(category: str, policy: str, text: str, path: Path) -> str:
    tags = _topic_tags(text, path)
    tag_text = "、".join(tags[:5]) if tags else _file_type(path)
    if category == DOCUMENT_CATEGORY_PERSONAL_MEDICAL:
        return "个人临床医疗文档已整段保护：不保存文件名细节、指标、症状、诊断、用药或正文线索；只保留 content-free 隐私保护事件。"
    if category == DOCUMENT_CATEGORY_SELF_RESUME:
        capability_text = _safe_resume_capability_summary(text)
        if capability_text:
            return f"自用简历文档安全摘要：角色/能力线索 {capability_text}；主题标签 {tag_text}；禁止写入联系方式或原文。"
        return f"自用简历文档安全摘要：主题标签 {tag_text}；可提炼角色定位和能力结构，禁止写入联系方式或原文。"
    if category == DOCUMENT_CATEGORY_TARGET_ROLE_JD:
        return f"岗位/JD 参考文档：主题标签 {tag_text}；只能作为岗位适配准备，不得写成 Local Owner 的长期能力或定位。"
    if category == DOCUMENT_CATEGORY_THIRD_PARTY_RESUME:
        return f"第三方简历/候选人资料：主题标签 {tag_text}；禁止写成 Local Owner 的个人事实。"
    if category == DOCUMENT_CATEGORY_AMBIGUOUS:
        return f"主体不明的人物/简历类文档：主题标签 {tag_text}；仅可用于当前工作流判断，暂不写长期记忆。"
    if category == DOCUMENT_CATEGORY_INTERVIEW_NOTES:
        return f"面试/求职工作流文档：主题标签 {tag_text}；最多写成准备流程，不写成未经确认的个人能力。"
    if policy == DOCUMENT_MEMORY_POLICY_REFERENCE_ONLY:
        return f"参考文档：主题标签 {tag_text}；不作为 Local Owner 本人事实写入。"
    return redact_for_storage(text or f"{path.suffix.lower() or 'document'} foreground document", max_len=360)


def _safe_resume_capability_summary(text: str) -> str:
    lowered = str(text or "").lower()
    found: list[str] = []
    for needle, label in SELF_RESUME_SAFE_TERMS:
        if needle.lower() in lowered and label not in found:
            found.append(label)
    return "、".join(found[:10])


def _personal_document_candidate(event: dict) -> dict | None:
    if not personal_high_precision_enabled():
        return None
    query = _document_query(event)
    if not query["tokens"] and not query["terms"] and not query["names"]:
        return None
    if not _document_candidate_scan_allowed(event, query):
        return None
    checked = 0
    best: dict | None = None
    for root in _personal_document_roots():
        if not root.exists() or not root.is_dir():
            continue
        try:
            walker = root.rglob("*")
            for path in walker:
                checked += 1
                if checked > MAX_PERSONAL_CANDIDATE_FILES:
                    break
                if not path.is_file():
                    continue
                suffix = path.suffix.lower()
                if suffix not in ALLOWED_SUFFIXES or suffix in SENSITIVE_SUFFIXES:
                    continue
                allowed, _ = _path_allowed(path)
                if not allowed:
                    continue
                scored = _score_document_candidate(path, query)
                if not scored:
                    continue
                if best is None or scored["score"] > best["score"] or (
                    scored["score"] == best["score"] and _safe_mtime(path) > _safe_mtime(best["path"])
                ):
                    best = scored
        except OSError:
            continue
    if best and best["score"] >= MIN_PERSONAL_DIAGNOSTIC_SCORE:
        return best
    return None


def _document_candidate_scan_allowed(event: dict, query: dict) -> bool:
    app_name = str(event.get("app_name") or "").lower()
    identity = resolve_app_identity(event.get("app_name"), event.get("bundle_id"))
    title = str(query.get("title") or "").lower()
    visible = str(query.get("visible_text") or "").lower()
    combined = f"{app_name} {title} {visible}"
    if identity.matched and identity.family in {
        *DOCUMENT_EDITOR_FAMILIES,
        *DOCUMENT_REFERENCE_FAMILIES,
    }:
        return True
    if query.get("terms"):
        return True
    if any(suffix in combined for suffix in (".md", ".markdown", ".docx", ".pages", ".pdf", ".txt")):
        return True
    if any(term in combined for term in DOCUMENT_HIGH_VALUE_TERMS):
        return True
    return False


def _candidate_document_names(event: dict) -> list[str]:
    title = str(event.get("window_title") or "").strip()
    if not title:
        return []
    for separator in (" — ", " – ", " - ", " | "):
        if separator in title:
            title = title.split(separator, 1)[0].strip()
    title = title.strip("\"' ")
    if not title or len(title) > 160:
        return []
    path_name = Path(title).name
    names = [path_name]
    if not Path(path_name).suffix:
        for suffix in (".md", ".markdown", ".txt", ".rtf", ".docx", ".doc", ".pages", ".pdf"):
            names.append(f"{path_name}{suffix}")
    return [name for name in dict.fromkeys(names) if name and not name.startswith(".")]


def _document_query(event: dict) -> dict:
    title = str(event.get("window_title") or "")
    visible = _visible_text(event)
    names = _candidate_document_names(event)
    text = f"{title} {visible}"
    tokens = _semantic_tokens(text)
    terms = [term for term in DOCUMENT_HIGH_VALUE_TERMS if term.lower() in text.lower()]
    return {
        "title": title,
        "visible_text": visible,
        "names": names,
        "name_stems": [Path(name).stem for name in names],
        "tokens": tokens,
        "terms": terms,
        "app_name": str(event.get("app_name") or ""),
        "bundle_id": str(event.get("bundle_id") or ""),
    }


def _score_document_candidate(path: Path, query: dict) -> dict | None:
    name_text = f"{path.name} {path.stem} {' '.join(path.parts[-5:])}"
    name_norm = name_text.lower()
    title_norm = str(query.get("title") or "").lower()
    visible_norm = str(query.get("visible_text") or "").lower()
    score = 0.0
    reasons: list[str] = []
    for name in query.get("names") or []:
        if name and name.lower() == path.name.lower():
            score += 5.0
            reasons.append("filename")
            break
    for stem in query.get("name_stems") or []:
        stem_l = str(stem or "").lower()
        if stem_l and (stem_l == path.stem.lower() or stem_l in name_norm):
            score += 3.5
            reasons.append("stem")
            break
    for term in query.get("terms") or []:
        if term.lower() in name_norm:
            score += 3.0
            reasons.append(f"term:{term}")
    identity = resolve_app_identity(query.get("app_name"), query.get("bundle_id"))
    if identity.matched and identity.family == "obsidian" and path.suffix.lower() in {".md", ".markdown"}:
        score += 2.5
        reasons.append("obsidian_markdown")
    path_text = str(path).lower()
    if "obsidian vault" in path_text or "icloud~md~obsidian" in path_text or "默认仓库" in path_text:
        score += 1.5
        reasons.append("obsidian_vault")
    if title_norm and path.stem.lower() in title_norm:
        score += 3.0
        reasons.append("title_contains_stem")
    document_text = _read_scoring_text(path)
    if document_text:
        overlap = _token_overlap_score(document_text, f"{visible_norm} {title_norm}")
        if overlap:
            score += min(8.0, overlap)
            reasons.append(f"text_overlap:{overlap:.1f}")
    age_days = max(0.0, (datetime.now(timezone.utc).timestamp() - _safe_mtime(path)) / 86400.0)
    if age_days <= 2:
        score += 2.0
        reasons.append("recent")
    elif age_days <= 14:
        score += 1.0
        reasons.append("recent_14d")
    if score <= 0:
        return None
    return {"path": path, "score": round(score, 2), "reason": "+".join(reasons[:6])}


def _personal_document_roots() -> list[Path]:
    roots: list[Path] = []
    extra = os.environ.get(PERSONAL_DOCUMENT_ROOT_ENV, "")
    for raw in extra.split(":"):
        value = raw.strip()
        if value:
            roots.append(Path(value).expanduser())
    documents = Path.home() / "Documents"
    roots.extend([
        obsidian_vault_root(),
        documents,
        Path.home() / "Desktop",
        Path.home() / "Downloads",
        Path.home() / "AI",
        Path.home() / "Code",
        Path.home() / "LocalOnly",
    ])
    output: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key not in seen:
            seen.add(key)
            output.append(root)
    return output


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _visible_text(event: dict) -> str:
    parts = [
        str(event.get("accessibility_text") or ""),
        str(event.get("ocr_text") or ""),
        str(event.get("window_title") or ""),
    ]
    return " ".join(part for part in parts if part).strip()


def _match_visible_text(document_text: str, visible_text: str) -> dict:
    if not document_text.strip() or not visible_text.strip():
        return {"matched": False, "location": "", "context": ""}
    doc_norm = _normalize(document_text)
    snippets = _candidate_snippets(visible_text)
    for snippet in snippets:
        normalized = _normalize(snippet)
        if len(normalized) < 12:
            continue
        index = doc_norm.find(normalized)
        if index >= 0:
            raw_index = _raw_index_for_normalized(document_text, index)
            start = max(0, raw_index - 320)
            end = min(len(document_text), raw_index + len(snippet) + 680)
            return {
                "matched": True,
                "location": f"visible_text_match:{index}",
                "context": document_text[start:end],
            }
    overlap = _token_overlap_score(document_text, visible_text)
    if overlap >= 5:
        return {
            "matched": True,
            "location": f"visible_token_overlap:{overlap:.1f}",
            "context": _candidate_context_from_text(document_text, visible_text),
        }
    return {"matched": False, "location": "", "context": ""}


def _candidate_snippets(text: str) -> list[str]:
    compact = " ".join(str(text or "").split())
    if not compact:
        return []
    candidates = [compact[:160]]
    words = compact.split()
    for size in (12, 8, 5):
        if len(words) >= size:
            candidates.append(" ".join(words[:size]))
            candidates.append(" ".join(words[-size:]))
    return candidates


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _read_scoring_text(path: Path) -> str:
    if path.suffix.lower() not in PLAIN_SUFFIXES:
        return ""
    try:
        return path.read_bytes()[:MAX_TEXT_BYTES].decode("utf-8", errors="replace")
    except OSError:
        return ""


def _semantic_tokens(text: str) -> set[str]:
    lowered = str(text or "").lower()
    tokens: set[str] = set()
    for value in re.findall(r"[a-z0-9][a-z0-9_-]{2,}|[\u4e00-\u9fff]{2,}", lowered):
        if value in TOKEN_STOPWORDS:
            continue
        if re.fullmatch(r"\d+", value):
            continue
        if re.fullmatch(r"[\u4e00-\u9fff]{5,}", value):
            for size in (2, 3, 4):
                for index in range(0, max(0, len(value) - size + 1)):
                    chunk = value[index : index + size]
                    if chunk not in TOKEN_STOPWORDS:
                        tokens.add(chunk)
        else:
            tokens.add(value)
    for term in DOCUMENT_HIGH_VALUE_TERMS:
        if term.lower() in lowered:
            tokens.add(term.lower())
    return tokens


def _token_overlap_score(document_text: str, visible_text: str) -> float:
    doc_tokens = _semantic_tokens(document_text)
    visible_tokens = _semantic_tokens(visible_text)
    if not doc_tokens or not visible_tokens:
        return 0.0
    overlap = doc_tokens & visible_tokens
    if not overlap:
        return 0.0
    weighted = 0.0
    for token in overlap:
        weighted += 2.0 if token in DOCUMENT_HIGH_VALUE_TERMS or len(token) >= 5 else 1.0
    return weighted


def _candidate_context_from_text(document_text: str, visible_text: str) -> str:
    tokens = sorted(_semantic_tokens(visible_text), key=len, reverse=True)
    lowered = document_text.lower()
    for token in tokens:
        index = lowered.find(token.lower())
        if index >= 0:
            start = max(0, index - 320)
            end = min(len(document_text), index + 680)
            return document_text[start:end]
    return document_text[:1000]


def _raw_index_for_normalized(raw: str, normalized_index: int) -> int:
    if normalized_index <= 0:
        return 0
    count = 0
    in_space = False
    for idx, char in enumerate(raw):
        if char.isspace():
            if in_space:
                continue
            in_space = True
            count += 1
        else:
            in_space = False
            count += 1
        if count >= normalized_index:
            return idx
    return 0


def _topic_tags(context_text: str, path: Path) -> list[str]:
    tags: list[str] = []
    suffix = path.suffix.lower().lstrip(".")
    if suffix:
        tags.append(suffix)
    lowered = context_text.lower()
    for keyword, tag in [
        ("python", "code"),
        ("swift", "code"),
        ("todo", "task"),
        ("计划", "planning"),
        ("报告", "report"),
        ("总结", "summary"),
        ("review", "review"),
        ("resume", "resume"),
        ("简历", "resume"),
        ("cv", "resume"),
        ("jd", "job_description"),
        ("岗位职责", "job_description"),
        ("任职要求", "job_description"),
        ("interview", "interview"),
        ("面试", "interview"),
        ("gtm", "go_to_market"),
        ("channel", "channel"),
        ("渠道", "channel"),
    ]:
        matched = (
            _contains_any(lowered, {keyword})
            if keyword in {"cv", "jd"}
            else keyword in lowered
        )
        if matched and tag not in tags:
            tags.append(tag)
    return tags[:6]


def _file_type(path: Path) -> str:
    return path.suffix.lower().lstrip(".") or "document"


def _protected_document_file_name(path: Path) -> str:
    suffix = path.suffix.lower()[:12]
    return f"protected_medical_document{suffix}"


def _protected_document_tags(path: Path) -> list[str]:
    tags = ["protected_medical_document"]
    file_type = _file_type(path)
    if file_type and file_type not in tags:
        tags.append(file_type)
    return tags[:3]


def _content_hash(path: Path, text: str) -> str:
    if text:
        payload = text[:MAX_TEXT_BYTES].encode("utf-8", errors="ignore")
    else:
        stat_result = path.stat()
        payload = f"{path.suffix}:{stat_result.st_mtime_ns}:{stat_result.st_size}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _path_hmac(root_dir: str | Path, path: Path) -> str:
    key_path = Path(root_dir) / "structured_signals" / "document_hmac.key"
    key_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(key_path.parent, 0o700)
    if key_path.exists():
        key = key_path.read_bytes()
    else:
        key = os.urandom(32)
        key_path.write_bytes(key)
        key_path.chmod(0o600)
    digest = hmac.new(key, str(path.resolve()).encode("utf-8"), hashlib.sha256).hexdigest()
    return f"doc_{digest[:24]}"


def _redact_file_name(name: str) -> str:
    stem = Path(name).stem
    suffix = Path(name).suffix
    redacted = redact_for_storage(stem, max_len=80)
    redacted = re.sub(r"\b\d{4,}\b", "[number]", redacted)
    return f"{redacted or 'document'}{suffix.lower()[:12]}"


def _contains_any(text: str, needles: set[str]) -> bool:
    lowered = str(text or "").lower()
    for needle in needles:
        normalized = str(needle or "").strip().lower()
        if not normalized:
            continue
        # Every ASCII edge is an identity boundary, not only short tokens such
        # as "cv" and "jd". Plain substring matching would also treat a third
        # party surname such as "Alexson" as the owner's "alex" marker.
        prefix = (
            r"(?<![a-z0-9])"
            if normalized[0].isascii() and normalized[0].isalnum()
            else ""
        )
        suffix = (
            r"(?![a-z0-9])"
            if normalized[-1].isascii() and normalized[-1].isalnum()
            else ""
        )
        if re.search(rf"{prefix}{re.escape(normalized)}{suffix}", lowered):
            return True
    return False
