from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from typing import Iterable

from .adult_policy import (
    ADULT_POLICY_VERSION,
    ADULT_RISK_TAG,
    SENSITIVE_CONTENT_REASON,
    context_terms as adult_context_terms,
    domain_hit as adult_domain_hit,
    policy_metadata as adult_policy_metadata,
    redact_terms as redact_adult_terms,
    sanitize_context as sanitize_adult_context,
    text_hit as adult_text_hit,
)
from .app_identity import BROWSER_FAMILIES, app_identity_in
from .safe_url import redact_url_for_storage as safe_redact_url_for_storage, safe_hostname_from_url


PRIVACY_FIREWALL_VERSION = "pfw-0.4.1"
REDACTION_POLICY_VERSION = "redaction-0.4.1"
PERSONAL_MEDICAL_CONTENT_REASON = "personal_medical_content"
PERSONAL_MEDICAL_RISK_FLAG = "personal_medical_content_suppressed"
PRIVACY_PROFILE_STRICT = "strict"
PRIVACY_PROFILE_PERSONAL_HIGH_PRECISION = "personal_high_precision"
PRIVACY_PROFILES = {PRIVACY_PROFILE_STRICT, PRIVACY_PROFILE_PERSONAL_HIGH_PRECISION}
DEFAULT_PRIVACY_PROFILE = PRIVACY_PROFILE_PERSONAL_HIGH_PRECISION
ACTIVITY_SUMMARY_MAX_CHARS = 240
CLEANED_OCR_MAX_CHARS = 2400


APP_BLACKLIST = {
    "1password",
    "bitwarden",
    "keepassxc",
    "lastpass",
    "keychain access",
    "loginwindow",
    "securityagent",
    "paypal",
    "stripe dashboard",
    "支付宝",
    "微信支付",
}

COMMUNICATION_APPS = {
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

DISCUSSION_APPS = COMMUNICATION_APPS | {
    "claude",
    "chatgpt",
    "codex",
    "cursor",
    "visual studio code",
    "vscode",
    "obsidian",
    "warp",
    "terminal",
    "iterm",
}

HIGH_RISK_DOMAIN_HINTS = (
    "bank",
    "paypal.",
    "stripe.",
    "creem.",
    "alipay.",
    "weixin.",
    "wechatpay",
    "chase.",
    "hsbc.",
    "citibank.",
    "wise.",
    "revolut.",
    "binance.",
    "okx.",
)

ADULT_DOMAIN_HINTS = ()

SOFT_HIGH_RISK_WORDS = (
    "币安",
    "binance",
    "okx",
    "中国银行",
    "bank of china",
    "token",
    "access token",
    "refresh token",
    "credential",
    "credentials",
    "login",
    "sign in",
    "signin",
    "secret",
    "secret key",
    "client secret",
    "api key",
    "password",
    "银行",
    "登录",
    "登入",
    "密钥",
    "秘钥",
    "令牌",
    "凭证",
)

PERSONAL_CONTEXT_SOFT_ALLOW_TERMS = (
    "token",
    "access token",
    "refresh token",
    "credential",
    "credentials",
    "login",
    "sign in",
    "signin",
    "secret",
    "secret key",
    "client secret",
    "api key",
    "password",
    "登录",
    "登入",
    "密钥",
    "秘钥",
    "令牌",
    "凭证",
)

HIGH_RISK_WORDS = SOFT_HIGH_RISK_WORDS + (
    "verify",
    "verification",
    "otp",
    "2fa",
    "mfa",
    "payment",
    "checkout",
    "card number",
    "cvv",
    "balance",
    "private key",
    "payment method",
    "developer console",
    "验证码",
    "支付",
    "付款",
    "余额",
    "银行卡",
    "身份证",
    "私钥",
    "应用凭证",
    "开放平台",
    "开发者后台",
    "密钥管理",
)

FINANCIAL_LABEL_TERMS = (
    "币安",
    "binance",
    "okx",
    "中国银行",
    "bank of china",
    "银行",
)
ACCOUNT_CONTEXT_TERMS = (
    "account",
    "balance",
    "portfolio",
    "asset",
    "assets",
    "trading",
    "trade",
    "withdraw",
    "deposit",
    "账户",
    "账号",
    "余额",
    "资产",
    "交易",
    "提现",
    "充值",
    "收款",
    "转账",
)
PAYMENT_CONTEXT_TERMS = (
    "payment",
    "checkout",
    "pay now",
    "card number",
    "cvv",
    "billing",
    "支付",
    "付款",
    "收银台",
    "银行卡",
)
LOGIN_CONTEXT_TERMS = (
    "password",
    "passcode",
    "sign in",
    "signin",
    "login",
    "verify",
    "verification",
    "otp",
    "2fa",
    "mfa",
    "密码",
    "登录",
    "登入",
    "验证码",
)
DEV_CREDENTIAL_CONTEXT_TERMS = (
    "api key",
    "secret key",
    "client secret",
    "developer console",
    "credential",
    "credentials",
    "应用凭证",
    "开放平台",
    "开发者后台",
    "密钥管理",
    "凭证与基础信息",
)
OTP_CONTEXT_TERMS = (
    "otp",
    "2fa",
    "mfa",
    "verify",
    "verification",
    "verification code",
    "验证码",
    "校验码",
    "动态码",
    "一次性",
)

ADULT_CONTEXT_TERMS = adult_context_terms()

ADULT_MEDIA_APPS = (
    "finder",
    "quicktime",
    "quicktime player",
    "vlc",
    "iina",
    "photos",
    "photo booth",
    "preview",
    "infuse",
    "elmedia",
)

STEAM_APPS = ("steam",)

PRIVATE_KEY_PATTERN = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----", re.I)
OPENAI_KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")
SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"\b(?:api[_-]?key|secret|token|access[_-]?token|refresh[_-]?token|client[_-]?secret|password)\s*[:=]\s*['\"]?[A-Za-z0-9_\-./+=]{12,}",
    re.I,
)
CARD_CANDIDATE_PATTERN = re.compile(r"\b(?:\d[ -]*?){13,19}\b")
SIX_DIGIT_PATTERN = re.compile(r"\b\d{6}\b")

SECRET_PATTERNS = (
    PRIVATE_KEY_PATTERN,
    OPENAI_KEY_PATTERN,
    SECRET_ASSIGNMENT_PATTERN,
)

EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_PATTERN = re.compile(r"(?<!\d)(?:\+?\d[\d -]{8,}\d)(?!\d)")
URL_PATTERN = re.compile(r"https?://[^\s]+")
LOCAL_PATH_PATTERN = re.compile(r"(/Users/[^\s\"'`，。；;]*|/tmp/[^\s\"'`，。；;]*|~/[^\s\"'`，。；;]*)", re.I)

SENSITIVE_REDACTIONS = (
    (re.compile(r"币安|中国银行|bank of china|binance|okx", re.I), "[sensitive_label_redacted]"),
    (re.compile(r"验证码|校验码|动态码|一次性验证码|银行卡|身份证|私钥"), "[sensitive_term_redacted]"),
)

PERSONAL_MEDICAL_PET_TERMS = (
    "宠物",
    "猫",
    "狗",
    "猫咪",
    "狗狗",
    "犬",
    "兽医",
    "宠物医院",
    "pet",
    "cat",
    "dog",
    "puppy",
    "kitten",
    "vet",
    "veterinary",
)
PERSONAL_MEDICAL_SELF_QUANT_TERMS = (
    "oura",
    "ouraring",
    "oura ring",
    "电子大健康",
    "readiness",
    "resilience",
    "sleep score",
    "activity score",
    "hrv",
    "spo2",
    "daily stress",
    "steps",
    "workout",
    "睡眠评分",
    "恢复",
    "步数",
    "运动",
    "压力",
    "心率变异",
)
PERSONAL_MEDICAL_REPORT_TERMS = (
    "体检",
    "体检报告",
    "病历",
    "病例",
    "病理",
    "检查报告",
    "检验报告",
    "化验",
    "化验单",
    "报告单",
    "血常规",
    "尿常规",
    "肝功能",
    "肾功能",
    "甲状腺",
    "血糖",
    "血脂",
    "胆固醇",
    "medical report",
    "medical record",
    "health check",
    "health checkup",
    "checkup report",
    "lab result",
    "lab report",
    "blood test",
    "urine test",
    "pathology",
    "radiology",
)
PERSONAL_MEDICAL_CONSULT_TERMS = (
    "症状",
    "病情",
    "诊断",
    "确诊",
    "复诊",
    "门诊",
    "医生",
    "医嘱",
    "处方",
    "用药",
    "药物",
    "治疗",
    "医院",
    "感染",
    "炎症",
    "发烧",
    "发热",
    "咳嗽",
    "疼痛",
    "腹泻",
    "头晕",
    "皮疹",
    "肿瘤",
    "癌",
    "symptom",
    "diagnosis",
    "diagnosed",
    "doctor",
    "physician",
    "clinic",
    "hospital",
    "prescription",
    "medication",
    "medicine",
    "treatment",
    "disease",
    "illness",
    "patient",
)
PERSONAL_MEDICAL_SELF_TERMS = (
    "我的",
    "我在",
    "我有",
    "我最近",
    "本人",
    "用户",
    "local owner",
    "alex",
    "exampleuser",
    "my ",
    "i ",
    "i'm",
    "i am",
    "me ",
)
PERSONAL_MEDICAL_CONSULT_PHRASES = (
    "我在咨询",
    "咨询病情",
    "问诊",
    "看医生",
    "去医院",
    "吃什么药",
    "怎么治疗",
    "need medical advice",
    "ask my doctor",
    "see a doctor",
    "my symptoms",
    "my diagnosis",
    "my medication",
)

PERIPHERAL_REGIONS = {"top_chrome", "left_sidebar", "right_sidebar", "bottom_chrome"}


@dataclass(frozen=True)
class FirewallResult:
    status: str
    action: str
    reason: str
    privacy_flags: list[str]
    redaction_confidence: float
    app_name: str
    bundle_id: str
    window_title_redacted: str
    url_redacted: str
    domain: str
    ocr_redacted_summary: str
    activity_summary: str
    safe_keywords: str
    privacy_level: str
    context_suppressed: bool = False


def classify_event(event: dict) -> FirewallResult:
    app_name = str(event.get("app_name") or "Unknown")
    bundle_id = str(event.get("bundle_id") or "")
    title = str(event.get("window_title") or "")
    url = str(event.get("url") or "")
    domain = str(event.get("domain") or _domain_from_url(url) or "")
    ocr_blocks = _normalize_ocr_blocks(event.get("ocr_blocks"))
    ocr_text = _combined_event_text(event, ocr_blocks)
    browser_risk_tags = {str(tag) for tag in event.get("browser_risk_tags") or []}
    document_risk_tags = _document_risk_tags(event.get("document_signal"))
    combined = " ".join([app_name, title, url, domain, ocr_text, " ".join(browser_risk_tags), " ".join(document_risk_tags)]).lower()

    hard_flags: list[str] = []
    hard_reasons: list[str] = []
    suppress_context = False

    if _contains_any(app_name.lower(), APP_BLACKLIST):
        hard_flags.append("app_blacklist_detected")
        hard_reasons.append("app_blacklist")

    if domain and any(hint in domain.lower() for hint in HIGH_RISK_DOMAIN_HINTS):
        hard_flags.append("url_blacklist_detected")
        hard_reasons.append("url_blacklist")

    secret_flags, secret_reasons = _hard_secret_signals(combined)
    hard_flags.extend(secret_flags)
    hard_reasons.extend(secret_reasons)

    if event.get("secure_input") is True or event.get("accessibility_secure_input") is True:
        hard_flags.append("secure_input_detected")
        hard_reasons.append("secure_input")

    if event.get("private_window") is True or "private_window" in browser_risk_tags or "incognito" in combined or "private browsing" in combined or "无痕" in combined:
        hard_flags.append("private_window_detected")
        hard_reasons.append("private_window")

    if event.get("browser_has_password_field") is True or "password_field" in browser_risk_tags:
        hard_flags.append("browser_password_field_detected")
        hard_reasons.append("browser_password_field")

    if event.get("browser_has_payment_form") is True or "payment_form" in browser_risk_tags:
        hard_flags.append("browser_payment_form_detected")
        hard_reasons.append("browser_payment_form")

    adult_flags, adult_reasons, adult_suppress = _adult_content_signals(
        app_name,
        title,
        url,
        domain,
        ocr_text,
        ocr_blocks,
        browser_risk_tags,
    )
    hard_flags.extend(adult_flags)
    hard_reasons.extend(adult_reasons)
    suppress_context = suppress_context or adult_suppress

    medical_flags, medical_reasons, medical_suppress = _personal_medical_content_signals(
        app_name,
        title,
        url,
        domain,
        ocr_text,
        ocr_blocks,
        document_risk_tags,
    )
    hard_flags.extend(medical_flags)
    hard_reasons.extend(medical_reasons)
    suppress_context = suppress_context or medical_suppress

    content_flags, content_reasons = _hard_content_signals(app_name, title, ocr_text, ocr_blocks)
    hard_flags.extend(content_flags)
    hard_reasons.extend(content_reasons)

    if PERSONAL_MEDICAL_CONTENT_REASON in hard_reasons:
        return _blocked_result(
            app_name,
            bundle_id,
            domain,
            [PERSONAL_MEDICAL_CONTENT_REASON],
            [PERSONAL_MEDICAL_RISK_FLAG],
            suppress_context=True,
            suppress_reason=PERSONAL_MEDICAL_CONTENT_REASON,
            suppress_flags=[PERSONAL_MEDICAL_RISK_FLAG],
        )

    if hard_reasons:
        return _blocked_result(
            app_name,
            bundle_id,
            domain,
            hard_reasons,
            hard_flags,
            suppress_context=suppress_context,
        )

    if _is_communication_app(app_name):
        summary = "沟通/协作活动，已保留活动类型，未保留对话原文。"
        return FirewallResult(
            status="clear",
            action="store_redacted_activity",
            reason="communication_redacted",
            privacy_flags=sorted(set(["communication_redacted", *_structured_privacy_flags(event)])),
            redaction_confidence=0.9,
            app_name=app_name,
            bundle_id=bundle_id,
            window_title_redacted=_redact_text(title, max_len=80),
            url_redacted=_redact_url(url),
            domain=domain,
            ocr_redacted_summary=summary,
            activity_summary=f"{app_name}：{summary}",
            safe_keywords=_safe_keywords(app_name, domain),
            privacy_level="communication",
        )

    if not ocr_text.strip() and not title.strip() and app_name == "Unknown":
        return _review_required_result(
            app_name,
            bundle_id,
            domain,
            "insufficient_context",
            ["insufficient_context"],
        )

    block_analysis = _analyze_ocr_blocks(ocr_blocks)
    soft_hits = _soft_hits(" ".join([title, ocr_text]))
    partial_reasons: list[str] = []
    partial_flags: list[str] = []


    if block_analysis["peripheral_soft"]:
        partial_reasons.append("peripheral_sensitive_label")
        partial_flags.append("partial_region_redacted")
        partial_flags.extend(f"partial_region_{region}" for region in block_analysis["peripheral_regions"])

    if (
        block_analysis["main_soft"]
        and not _main_soft_allowed_for_personal_context(app_name, block_analysis["main_soft"])
        and not _main_text_requires_block(app_name, " ".join(block_analysis["main_soft"]))
    ):
        partial_reasons.append("main_soft_sensitive_term")
        partial_flags.append("main_soft_sensitive_redacted")

    if block_analysis["unknown_soft"]:
        if _main_soft_allowed_for_personal_context(app_name, block_analysis["unknown_soft"]):
            pass
        elif _needs_fail_closed_for_unknown_region(event, block_analysis):
            return _review_required_result(
                app_name,
                bundle_id,
                domain,
                "uncertain_sensitive_region",
                ["uncertain_sensitive_region"],
            )
        partial_reasons.append("unknown_region_sensitive_term")
        partial_flags.append("unknown_region_redacted")

    if soft_hits and not partial_reasons and not _main_soft_allowed_for_personal_context(app_name, [title, ocr_text]):
        partial_reasons.append("soft_sensitive_term")
        partial_flags.append("soft_sensitive_redacted")
        if _is_discussion_context(app_name):
            partial_flags.append("discussion_context_redacted")

    if partial_reasons:
        ocr_cleaned = _redact_text_preserve_blocks(ocr_text, max_len=CLEANED_OCR_MAX_CHARS)
        ocr_summary = _redact_text(ocr_text, max_len=ACTIVITY_SUMMARY_MAX_CHARS)
        title_redacted = _redact_text(title, max_len=100)
        activity_summary = _build_partial_summary(app_name, domain, title_redacted, ocr_summary)
        return FirewallResult(
            status="partial_redacted",
            action="store_partially_redacted_activity",
            reason=",".join(sorted(set(partial_reasons))),
            privacy_flags=sorted(set(["partial_redaction_applied", *partial_flags, *_structured_privacy_flags(event)])),
            redaction_confidence=0.86,
            app_name=app_name,
            bundle_id=bundle_id,
            window_title_redacted=title_redacted,
            url_redacted=_redact_url(url),
            domain=domain,
            ocr_redacted_summary=ocr_cleaned,
            activity_summary=activity_summary,
            safe_keywords=_safe_keywords(app_name, domain, title_redacted, ocr_summary),
            privacy_level="partial_redacted",
        )

    ocr_cleaned = _redact_text_preserve_blocks(ocr_text, max_len=CLEANED_OCR_MAX_CHARS)
    ocr_summary = _redact_text(ocr_text, max_len=ACTIVITY_SUMMARY_MAX_CHARS)
    title_redacted = _redact_text(title, max_len=100)
    activity_summary = _build_summary(app_name, domain, title_redacted, ocr_summary)
    return FirewallResult(
        status="clear",
        action="store_redacted_activity",
        reason="clear",
        privacy_flags=sorted(set(_structured_privacy_flags(event))),
        redaction_confidence=0.92,
        app_name=app_name,
        bundle_id=bundle_id,
        window_title_redacted=title_redacted,
        url_redacted=_redact_url(url),
        domain=domain,
        ocr_redacted_summary=ocr_cleaned,
        activity_summary=activity_summary,
        safe_keywords=_safe_keywords(app_name, domain, title_redacted, ocr_summary),
        privacy_level="normal",
    )




def safe_evidence_id(source: str) -> str:
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
    return f"evidence_{digest}"


def redact_for_storage(value: str, max_len: int = ACTIVITY_SUMMARY_MAX_CHARS) -> str:
    return _redact_text(str(value or ""), max_len=max_len)


def redact_url_for_storage(url: str) -> str:
    return _redact_url(str(url or ""))


def sanitize_privacy_event_context(item: dict) -> dict:
    sanitized = sanitize_adult_context(dict(item))
    reason = str(sanitized.get("reason") or sanitized.get("type") or "").lower()
    if reason == PERSONAL_MEDICAL_CONTENT_REASON:
        sanitized["reason"] = PERSONAL_MEDICAL_CONTENT_REASON
        sanitized["app_name"] = ""
        sanitized["domain"] = ""
    return sanitized


def normalize_privacy_profile(value: str | None) -> str:
    profile = str(value or "").strip().lower()
    return profile if profile in PRIVACY_PROFILES else DEFAULT_PRIVACY_PROFILE


def privacy_profile() -> str:
    return normalize_privacy_profile(os.environ.get("PAM_PRIVACY_PROFILE"))


def personal_high_precision_enabled() -> bool:
    return privacy_profile() == PRIVACY_PROFILE_PERSONAL_HIGH_PRECISION


def _blocked_result(
    app_name: str,
    bundle_id: str,
    domain: str,
    reasons: Iterable[str],
    flags: Iterable[str],
    *,
    suppress_context: bool = False,
    suppress_reason: str = SENSITIVE_CONTENT_REASON,
    suppress_flags: Iterable[str] | None = None,
) -> FirewallResult:
    if suppress_context:
        reasons = [suppress_reason]
        flags = list(suppress_flags or ["sensitive_content_suppressed"])
        app_name = ""
        bundle_id = ""
        domain = ""
    return FirewallResult(
        status="blocked",
        action="drop_content_record_privacy_event",
        reason=",".join(sorted(set(reasons))),
        privacy_flags=sorted(set(flags)),
        redaction_confidence=0.0,
        app_name=app_name,
        bundle_id=bundle_id,
        window_title_redacted="[blocked]",
        url_redacted="",
        domain=domain,
        ocr_redacted_summary="",
        activity_summary="隐私保护已触发；该片段未进入长期活动数据。",
        safe_keywords="",
        privacy_level="high_risk",
        context_suppressed=suppress_context,
    )


def _review_required_result(
    app_name: str,
    bundle_id: str,
    domain: str,
    reason: str,
    flags: list[str],
) -> FirewallResult:
    return FirewallResult(
        status="review_required",
        action="record_gap_event",
        reason=reason,
        privacy_flags=flags,
        redaction_confidence=0.4,
        app_name=app_name,
        bundle_id=bundle_id,
        window_title_redacted="",
        url_redacted="",
        domain=domain,
        ocr_redacted_summary="",
        activity_summary="上下文不足或风险不确定，P0 阶段转为缺口事件。",
        safe_keywords="",
        privacy_level="unknown",
    )


def _contains_any(value: str, candidates: Iterable[str]) -> bool:
    return any(candidate in value for candidate in candidates)


def _domain_from_url(url: str) -> str:
    return safe_hostname_from_url(url)


def _redact_url(url: str) -> str:
    return safe_redact_url_for_storage(url)


def _redact_text(value: str, max_len: int) -> str:
    text = value.strip()
    text = _apply_text_redactions(text)
    text = re.sub(r"\s+", " ", text)
    if len(text) > max_len:
        return text[: max_len - 1].rstrip() + "…"
    return text


def _redact_text_preserve_blocks(value: str, max_len: int) -> str:
    text = value.strip()
    text = _apply_text_redactions(text)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n+ *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > max_len:
        return text[: max_len - 1].rstrip() + "…"
    return text


def _apply_text_redactions(text: str) -> str:
    text = PRIVATE_KEY_PATTERN.sub("[secret_redacted]", text)
    text = OPENAI_KEY_PATTERN.sub("[secret_redacted]", text)
    text = SECRET_ASSIGNMENT_PATTERN.sub("[secret_redacted]", text)
    text = _redact_likely_card_numbers(text)
    text = URL_PATTERN.sub("[url_redacted]", text)
    text = EMAIL_PATTERN.sub("[email_redacted]", text)
    text = LOCAL_PATH_PATTERN.sub("[path_redacted]", text)
    text = _redact_likely_phone_numbers(text)
    for pattern, replacement in SENSITIVE_REDACTIONS:
        text = pattern.sub(replacement, text)
    return redact_adult_terms(text)


def _redact_likely_card_numbers(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn_valid(digits):
            return "[number_redacted]"
        return match.group(0)

    return CARD_CANDIDATE_PATTERN.sub(replace, text)


def _redact_likely_phone_numbers(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        digits = re.sub(r"\D", "", raw)
        has_phone_shape = raw.strip().startswith("+") or "-" in raw or " " in raw
        if has_phone_shape and 10 <= len(digits) <= 15:
            return "[number_redacted]"
        return raw

    return PHONE_PATTERN.sub(replace, text)


def _safe_keywords(*parts: str) -> str:
    text = " ".join(part for part in parts if part)
    text = _redact_text(text, max_len=240)
    return text


def _build_summary(app_name: str, domain: str, title: str, ocr_summary: str) -> str:
    context = app_name
    if domain:
        context += f" / {domain}"
    detail = ocr_summary or title or "记录到一次普通屏幕活动。"
    return f"{context}：{detail}"


def _build_partial_summary(app_name: str, domain: str, title: str, ocr_summary: str) -> str:
    context = app_name
    if domain:
        context += f" / {domain}"
    detail = ocr_summary or title or "保留安全活动摘要。"
    return f"{context}：局部敏感内容已清洗；{detail}"


def _hard_secret_signals(text: str) -> tuple[list[str], list[str]]:
    flags: list[str] = []
    reasons: list[str] = []
    if PRIVATE_KEY_PATTERN.search(text):
        flags.append("private_key_detected")
        reasons.append("secret_pattern")
    if OPENAI_KEY_PATTERN.search(text):
        flags.append("api_key_detected")
        reasons.append("secret_pattern")
    if SECRET_ASSIGNMENT_PATTERN.search(text):
        flags.append("secret_assignment_detected")
        reasons.append("secret_pattern")
    if _otp_code_hit(text):
        flags.append("otp_code_detected")
        reasons.append("otp_code")
    if _card_number_hit(text):
        flags.append("card_number_detected")
        reasons.append("card_number")
    return flags, reasons


def _otp_code_hit(text: str) -> bool:
    return bool(SIX_DIGIT_PATTERN.search(text) and _contains_any(text, OTP_CONTEXT_TERMS))


def _card_number_hit(text: str) -> bool:
    for match in CARD_CANDIDATE_PATTERN.finditer(text):
        digits = re.sub(r"\D", "", match.group(0))
        if len(digits) < 13 or len(digits) > 19:
            continue
        if _luhn_valid(digits) or _contains_any(text, PAYMENT_CONTEXT_TERMS):
            return True
    return False


def _luhn_valid(digits: str) -> bool:
    total = 0
    reverse_digits = digits[::-1]
    for index, char in enumerate(reverse_digits):
        value = int(char)
        if index % 2 == 1:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _hard_content_signals(
    app_name: str,
    title: str,
    ocr_text: str,
    ocr_blocks: list[dict],
) -> tuple[list[str], list[str]]:
    flags: list[str] = []
    reasons: list[str] = []
    main_text = _main_content_text(title, ocr_text, ocr_blocks).lower()
    if not main_text:
        return flags, reasons

    if _main_text_requires_block(app_name, main_text):
        if _contains_any(main_text, DEV_CREDENTIAL_CONTEXT_TERMS):
            flags.append("developer_credential_page_detected")
            reasons.append("developer_credential_page")
        if _contains_any(main_text, PAYMENT_CONTEXT_TERMS):
            flags.append("payment_page_detected")
            reasons.append("payment_page")
        if _contains_any(main_text, FINANCIAL_LABEL_TERMS) and _contains_any(main_text, ACCOUNT_CONTEXT_TERMS):
            flags.append("financial_account_page_detected")
            reasons.append("financial_account_page")
        if _contains_any(main_text, LOGIN_CONTEXT_TERMS) and not _is_discussion_context(app_name):
            flags.append("login_or_verification_page_detected")
            reasons.append("login_or_verification_page")
    return flags, reasons


def _adult_content_signals(
    app_name: str,
    title: str,
    url: str,
    domain: str,
    ocr_text: str,
    ocr_blocks: list[dict],
    browser_risk_tags: set[str],
) -> tuple[list[str], list[str], bool]:
    text = _main_content_text(title, ocr_text, ocr_blocks)
    combined = " ".join([app_name, title, url, domain, text]).lower()
    tag_hit = ADULT_RISK_TAG in browser_risk_tags
    domain_policy_hit = _adult_domain_hit(domain) or _adult_domain_hit(_domain_from_url(url))
    text_policy_hit = _adult_context_hit(combined)
    hard_hit = tag_hit or domain_policy_hit or text_policy_hit
    if not hard_hit:
        return [], [], False

    if _is_discussion_context(app_name) and not (tag_hit or domain_policy_hit):
        return [], [], False

    if _is_steam_context(app_name) or _is_media_context(app_name) or _is_browser_context(app_name) or hard_hit:
        return ["sensitive_content_suppressed"], [SENSITIVE_CONTENT_REASON], True
    return [], [], False


def _personal_medical_content_signals(
    app_name: str,
    title: str,
    url: str,
    domain: str,
    ocr_text: str,
    ocr_blocks: list[dict],
    document_risk_tags: set[str],
) -> tuple[list[str], list[str], bool]:
    if PERSONAL_MEDICAL_RISK_FLAG in document_risk_tags or "personal_medical_document" in document_risk_tags:
        return [PERSONAL_MEDICAL_RISK_FLAG], [PERSONAL_MEDICAL_CONTENT_REASON], True
    text = _main_content_text(title, ocr_text, ocr_blocks)
    combined = " ".join([app_name, title, url, domain, text]).lower()
    if personal_medical_context_hit(combined):
        return [PERSONAL_MEDICAL_RISK_FLAG], [PERSONAL_MEDICAL_CONTENT_REASON], True
    return [], [], False


def personal_medical_context_hit(text: str) -> bool:
    lowered = str(text or "").lower()
    if not lowered:
        return False
    if _contains_any(lowered, PERSONAL_MEDICAL_PET_TERMS):
        return False
    has_report = _contains_any(lowered, PERSONAL_MEDICAL_REPORT_TERMS)
    has_consult = _contains_any(lowered, PERSONAL_MEDICAL_CONSULT_TERMS)
    if not has_report and not has_consult:
        return False
    if _contains_any(lowered, PERSONAL_MEDICAL_SELF_QUANT_TERMS) and not has_report and not _contains_any(lowered, PERSONAL_MEDICAL_CONSULT_PHRASES):
        return False
    if has_report:
        return True
    consult_count = sum(1 for term in PERSONAL_MEDICAL_CONSULT_TERMS if term in lowered)
    return consult_count >= 2 or _contains_any(lowered, PERSONAL_MEDICAL_SELF_TERMS) or _contains_any(lowered, PERSONAL_MEDICAL_CONSULT_PHRASES)


def document_protected_context_hit(text: str) -> bool:
    """Return True when a local document must remain count-only.

    This intentionally reuses the hard-secret detectors used by the screen
    privacy firewall and adds the document-specific private-chat, customer-PII,
    medical, and financial boundaries.  Callers must not retain a filename or
    structure labels when this returns True.
    """
    value = str(text or "")
    lowered = value.lower()
    if not lowered:
        return False
    if _hard_secret_signals(value)[0] or personal_medical_context_hit(value):
        return True
    protected_terms = (
        "private chat", "direct message", "chat transcript", "wechat", "whatsapp",
        "私聊", "聊天记录", "聊天截图", "微信聊天", "私信",
        "customer pii", "customer list", "client list", "customer data",
        "客户隐私", "客户资料", "客户名单", "客户姓名", "联系方式", "身份证",
        "financial", "bank account", "invoice", "payment", "credit card",
        "财务", "银行", "银行卡", "支付", "账单", "发票",
    )
    if _contains_any(lowered, protected_terms):
        return True
    return bool(EMAIL_PATTERN.search(value) or PHONE_PATTERN.search(value))


def _document_risk_tags(raw_signal) -> set[str]:
    if not isinstance(raw_signal, dict):
        return set()
    values: set[str] = set()
    for key in ("document_risk_flags", "privacy_flags", "topic_tags"):
        raw_values = raw_signal.get(key)
        if not isinstance(raw_values, list):
            continue
        values.update(str(value) for value in raw_values if value)
    for key in ("document_source_category", "document_memory_policy"):
        value = str(raw_signal.get(key) or "")
        if value:
            values.add(value)
    return values


def _adult_domain_hit(domain: str) -> bool:
    return adult_domain_hit(domain).matched


def _adult_context_hit(text: str) -> bool:
    return adult_text_hit(text).matched




def _main_text_requires_block(app_name: str, main_text: str) -> bool:
    if _is_discussion_context(app_name):
        return False
    if _contains_any(main_text, DEV_CREDENTIAL_CONTEXT_TERMS):
        return True
    if _contains_any(main_text, PAYMENT_CONTEXT_TERMS):
        return True
    if _contains_any(main_text, FINANCIAL_LABEL_TERMS) and _contains_any(main_text, ACCOUNT_CONTEXT_TERMS):
        return True
    login_hits = sum(1 for term in LOGIN_CONTEXT_TERMS if term in main_text)
    return login_hits >= 2 or ("password" in main_text and ("login" in main_text or "sign in" in main_text))


def _main_content_text(title: str, ocr_text: str, ocr_blocks: list[dict]) -> str:
    main_parts = [title]
    if ocr_blocks:
        main_parts.extend(block["text"] for block in ocr_blocks if block.get("region") == "main_content")
    else:
        main_parts.append(ocr_text)
    return " ".join(part for part in main_parts if part)


def _normalize_ocr_blocks(raw_blocks) -> list[dict]:
    if not isinstance(raw_blocks, list):
        return []
    blocks: list[dict] = []
    for raw in raw_blocks:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        box = raw.get("bounding_box") or raw.get("boundingBox") or {}
        if not isinstance(box, dict):
            box = {}
        confidence = _float_or_none(raw.get("confidence"))
        region = str(raw.get("region") or _region_from_box(box))
        blocks.append(
            {
                "text": text,
                "confidence": confidence,
                "bounding_box": box,
                "region": region,
                "display_id": str(raw.get("display_id") or ""),
                "window_id": str(raw.get("window_id") or raw.get("source_window_id") or ""),
            }
        )
    return blocks


def _float_or_none(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _region_from_box(box: dict) -> str:
    try:
        x = float(box.get("x"))
        y = float(box.get("y"))
        width = float(box.get("width"))
        height = float(box.get("height"))
    except (TypeError, ValueError):
        return "unknown"
    if y + height >= 0.84:
        return "top_chrome"
    if y <= 0.08:
        return "bottom_chrome"
    if x <= 0.24:
        return "left_sidebar"
    if x >= 0.72:
        return "right_sidebar"
    return "main_content"


def _join_block_text(blocks: list[dict]) -> str:
    return " ".join(block["text"] for block in blocks if block.get("text"))


def _combined_event_text(event: dict, ocr_blocks: list[dict]) -> str:
    parts = [
        str(event.get("accessibility_text") or ""),
        str(event.get("document_text_for_privacy") or ""),
        str(event.get("ocr_text") or _join_block_text(ocr_blocks)),
    ]
    return "\n".join(part for part in parts if part.strip())


def _structured_privacy_flags(event: dict) -> list[str]:
    flags: list[str] = []
    if str(event.get("accessibility_text") or "").strip():
        flags.append("accessibility_text_used")
    if event.get("accessibility_secure_input") is True:
        flags.append("accessibility_secure_input_detected")
    if event.get("browser_signal"):
        flags.append("browser_signal_used")
    if event.get("document_signal"):
        flags.append("document_signal_used")
    return flags


def _analyze_ocr_blocks(ocr_blocks: list[dict]) -> dict:
    result = {
        "peripheral_soft": [],
        "peripheral_regions": [],
        "main_soft": [],
        "unknown_soft": [],
        "low_confidence": False,
    }
    for block in ocr_blocks:
        text = str(block.get("text") or "")
        if not text:
            continue
        confidence = block.get("confidence")
        if confidence is not None and confidence < 0.45:
            result["low_confidence"] = True
        if not _soft_hits(text):
            continue
        region = str(block.get("region") or "unknown")
        if region in PERIPHERAL_REGIONS:
            result["peripheral_soft"].append(text)
            result["peripheral_regions"].append(region)
        elif region == "main_content":
            result["main_soft"].append(text)
        else:
            result["unknown_soft"].append(text)
    result["peripheral_regions"] = sorted(set(result["peripheral_regions"]))
    return result


def _soft_hits(text: str) -> list[str]:
    lower = text.lower()
    hits = [word for word in SOFT_HIGH_RISK_WORDS if _soft_term_hit(lower, word)]
    hits.extend(term for term in ADULT_CONTEXT_TERMS if str(term).lower() in lower)
    return hits


def _main_soft_allowed_for_personal_context(app_name: str, texts: Iterable[str]) -> bool:
    if not personal_high_precision_enabled():
        return False
    if not _is_discussion_context(app_name):
        return False
    hits = {str(hit).lower() for text in texts for hit in _soft_hits(str(text or ""))}
    if not hits:
        return False
    allowed = {term.lower() for term in PERSONAL_CONTEXT_SOFT_ALLOW_TERMS}
    return hits.issubset(allowed)


def _soft_term_hit(lower_text: str, term: str) -> bool:
    lowered_term = str(term).lower().strip()
    if not lowered_term:
        return False
    if _ascii_soft_term(lowered_term):
        pattern = r"(?<![a-z0-9_])" + re.escape(lowered_term).replace(r"\ ", r"\s+") + r"(?![a-z0-9_])"
        return re.search(pattern, lower_text, re.I) is not None
    return lowered_term in lower_text


def _ascii_soft_term(term: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9 _-]*[a-z0-9]", term))


def _is_communication_app(app_name: str) -> bool:
    return _contains_any(app_name.lower(), COMMUNICATION_APPS)


def _is_discussion_context(app_name: str) -> bool:
    return _contains_any(app_name.lower(), DISCUSSION_APPS)


def _is_browser_context(app_name: str, bundle_id: str = "") -> bool:
    return app_identity_in(app_name, bundle_id, BROWSER_FAMILIES)


def _is_media_context(app_name: str) -> bool:
    return _contains_any(app_name.lower(), ADULT_MEDIA_APPS)


def _is_steam_context(app_name: str) -> bool:
    return _contains_any(app_name.lower(), STEAM_APPS)






def _needs_fail_closed_for_unknown_region(event: dict, block_analysis: dict) -> bool:
    confidence = event.get("context_confidence")
    try:
        if confidence is not None and float(confidence) < 0.55:
            return True
    except (TypeError, ValueError):
        return True
    return bool(block_analysis["low_confidence"])
