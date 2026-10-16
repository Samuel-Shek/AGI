from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse


ADULT_POLICY_VERSION = "adult-policy-0.4.0"
ADULT_RISK_TAG = "adult_content"
SENSITIVE_CONTENT_REASON = "sensitive_content"
RESOURCE_PATH = Path(__file__).resolve().parent / "resources" / "adult_content_policy.json"

_DOMAIN_RE = re.compile(r"(?i)\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}\b")


@dataclass(frozen=True)
class AdultPolicyHit:
    matched: bool
    kind: str = ""
    reason: str = ""
    source: str = ""
    confidence: float = 0.0


def domain_hit(domain_or_url: str) -> AdultPolicyHit:
    domain = normalize_domain(domain_or_url)
    if not domain:
        return AdultPolicyHit(False)

    policy = _compiled_policy()
    for candidate in _domain_candidates(domain):
        if candidate in policy["domains_exact"] or candidate in policy["domains_suffix"]:
            return AdultPolicyHit(True, "domain", SENSITIVE_CONTENT_REASON, "local_policy", 0.99)
        if _bloom_contains(candidate, policy):
            return AdultPolicyHit(True, "domain_hash", SENSITIVE_CONTENT_REASON, "public_blocklist_snapshot", 0.98)

    labels = set(domain.split("."))
    if labels.intersection(policy["domain_label_terms"]):
        return AdultPolicyHit(True, "domain_label", SENSITIVE_CONTENT_REASON, "local_policy", 0.93)
    for label in labels:
        if any(term in label for term in policy["domain_label_contains"]):
            return AdultPolicyHit(True, "domain_label", SENSITIVE_CONTENT_REASON, "local_policy", 0.91)
    return AdultPolicyHit(False)


def text_hit(text: str, context: dict | None = None) -> AdultPolicyHit:
    del context
    value = str(text or "")
    if not value.strip():
        return AdultPolicyHit(False)

    for match in _DOMAIN_RE.finditer(value):
        hit = domain_hit(match.group(0))
        if hit.matched:
            return AdultPolicyHit(True, "domain_text", SENSITIVE_CONTENT_REASON, hit.source, hit.confidence)

    for pattern in _compiled_policy()["text_patterns"]:
        if pattern.search(value):
            return AdultPolicyHit(True, "text", SENSITIVE_CONTENT_REASON, "local_policy", 0.9)
    return AdultPolicyHit(False)


def redact_terms(text: str) -> str:
    value = str(text or "")
    if not value:
        return value

    def redact_domain(match: re.Match) -> str:
        if domain_hit(match.group(0)).matched:
            return "[sensitive_term_redacted]"
        return match.group(0)

    value = _DOMAIN_RE.sub(redact_domain, value)
    for pattern in _compiled_policy()["text_patterns"]:
        value = pattern.sub("[sensitive_term_redacted]", value)
    return value


def sanitize_context(payload: dict) -> dict:
    output = dict(payload)
    combined = " ".join(
        str(output.get(key) or "")
        for key in (
            "type",
            "reason",
            "app_name",
            "domain",
            "action",
            "title",
            "window_title",
            "url",
            "url_redacted",
            "path",
            "file_name",
            "folder_name",
            "risk_tags",
        )
    )
    risk_tags = output.get("risk_tags")
    tag_hit = (
        ADULT_RISK_TAG in {str(tag).lower() for tag in risk_tags}
        if isinstance(risk_tags, list)
        else ADULT_RISK_TAG in combined.lower()
    )
    if tag_hit or domain_hit(str(output.get("domain") or "")).matched or text_hit(combined).matched:
        output["reason"] = SENSITIVE_CONTENT_REASON
        for key in (
            "app_name",
            "domain",
            "title",
            "window_title",
            "url",
            "url_redacted",
            "path",
            "file_name",
            "folder_name",
        ):
            if key in output:
                output[key] = ""
        if isinstance(risk_tags, list):
            output["risk_tags"] = [tag for tag in risk_tags if str(tag) != ADULT_RISK_TAG]
    return output


def context_terms() -> tuple[str, ...]:
    policy = _load_policy()
    terms: list[str] = []
    for group in ("platform_terms", "content_terms", "media_terms", "game_terms", "cjk_terms"):
        terms.extend(str(term) for term in policy.get(group, []) if str(term).strip())
    return tuple(dict.fromkeys(terms))


def policy_metadata() -> dict:
    policy = _load_policy()
    domain_blocklist = policy.get("domain_blocklist") or {}
    return {
        "policy_version": str(policy.get("policy_version") or ADULT_POLICY_VERSION),
        "generated_at": str(policy.get("generated_at") or ""),
        "domain_snapshot_count": int(domain_blocklist.get("item_count") or 0),
        "domain_snapshot_sha256": str(domain_blocklist.get("domain_sha256") or ""),
        "sources": [
            {
                "name": str(source.get("name") or ""),
                "url": str(source.get("url") or ""),
                "parsed_domains": int(source.get("parsed_domains") or 0),
            }
            for source in policy.get("sources", [])
            if isinstance(source, dict)
        ],
    }


def normalize_domain(value: str) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    raw = raw.replace("\\", "/")
    raw = re.sub(r"^(?:0\.0\.0\.0|127\.0\.0\.1)\s+", "", raw)
    raw = raw.lstrip("|").lstrip("*").lstrip(".")
    if "://" in raw:
        parsed = urlparse(raw)
        raw = parsed.hostname or parsed.netloc or ""
    else:
        raw = raw.split()[0] if raw.split() else raw
        raw = raw.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        if raw.count(":") == 1:
            raw = raw.rsplit(":", 1)[0]
    raw = raw.strip("[] .")
    if not raw or raw == "localhost":
        return ""
    try:
        raw = raw.encode("idna").decode("ascii")
    except UnicodeError:
        return ""
    if len(raw) > 253 or "." not in raw:
        return ""
    if not re.fullmatch(r"[a-z0-9.-]+", raw):
        return ""
    return raw.strip(".")


def _domain_candidates(domain: str) -> list[str]:
    labels = [part for part in domain.split(".") if part]
    candidates = [".".join(labels[index:]) for index in range(0, max(1, len(labels) - 1))]
    return [candidate for candidate in candidates if "." in candidate]


@lru_cache(maxsize=1)
def _load_policy() -> dict:
    try:
        policy = json.loads(RESOURCE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        policy = _fallback_policy()
    return {**_fallback_policy(), **policy}


@lru_cache(maxsize=1)
def _compiled_policy() -> dict:
    policy = _load_policy()
    domain_blocklist = policy.get("domain_blocklist") or {}
    bitset_b64 = str(domain_blocklist.get("bitset_base64") or "")
    try:
        bloom_bytes = base64.b64decode(bitset_b64.encode("ascii"), validate=True) if bitset_b64 else b""
    except (ValueError, TypeError):
        bloom_bytes = b""

    terms = context_terms()
    regexes = [str(pattern) for pattern in policy.get("context_regexes", []) if str(pattern).strip()]
    text_patterns = [_compile_term(term) for term in terms]
    for pattern in regexes:
        try:
            text_patterns.append(re.compile(pattern, re.I))
        except re.error:
            continue

    return {
        "domains_exact": frozenset(normalize_domain(value) for value in policy.get("domain_exact", []) if normalize_domain(value)),
        "domains_suffix": frozenset(normalize_domain(value) for value in policy.get("domain_suffix", []) if normalize_domain(value)),
        "domain_label_terms": frozenset(str(value).lower() for value in policy.get("domain_label_terms", [])),
        "domain_label_contains": tuple(str(value).lower() for value in policy.get("domain_label_contains", [])),
        "bloom_bytes": bloom_bytes,
        "bloom_bits": int(domain_blocklist.get("bits") or 0),
        "bloom_hashes": int(domain_blocklist.get("hashes") or 0),
        "text_patterns": tuple(text_patterns),
    }


def _compile_term(term: str) -> re.Pattern:
    raw = str(term).strip()
    if re.fullmatch(r"[A-Za-z0-9+.\s_-]+", raw):
        pieces = [piece for piece in re.split(r"[\s_-]+", raw) if piece]
        escaped = r"[\s_-]+".join(re.escape(piece) for piece in pieces)
        return re.compile(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])", re.I)
    return re.compile(re.escape(raw), re.I)


def _bloom_contains(domain: str, policy: dict) -> bool:
    bloom_bytes = policy["bloom_bytes"]
    bit_count = int(policy["bloom_bits"] or 0)
    hash_count = int(policy["bloom_hashes"] or 0)
    if not bloom_bytes or bit_count <= 0 or hash_count <= 0:
        return False
    h1, h2 = _hash_pair(domain)
    for index in range(hash_count):
        bit_index = (h1 + index * h2) % bit_count
        if not (bloom_bytes[bit_index // 8] & (1 << (bit_index % 8))):
            return False
    return True


def _hash_pair(value: str) -> tuple[int, int]:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    first = int.from_bytes(digest[:8], "big")
    second = int.from_bytes(digest[8:16], "big") or 0x9E3779B97F4A7C15
    return first, second


def _fallback_policy() -> dict:
    return {
        "policy_version": ADULT_POLICY_VERSION,
        "generated_at": "",
        "sources": [],
        "domain_blocklist": {
            "type": "bloom_filter_sha256",
            "bits": 0,
            "hashes": 0,
            "item_count": 0,
            "domain_sha256": "",
            "bitset_base64": "",
        },
        "domain_exact": [
            "pornhub.com",
            "xvideos.com",
            "xhamster.com",
            "xnxx.com",
            "redtube.com",
            "youporn.com",
            "spankbang.com",
            "onlyfans.com",
            "fansly.com",
            "chaturbate.com",
            "stripchat.com",
            "nhentai.net",
            "hanime.tv",
            "f95zone.to",
            "dlsite.com",
            "missav.com",
            "javbus.com",
            "javdb.com",
            "avgle.com",
        ],
        "domain_suffix": [],
        "domain_label_terms": ["porn", "porno", "xxx", "nsfw", "hentai", "eroge", "jav", "r18"],
        "domain_label_contains": ["porn", "hentai", "rule34"],
        "platform_terms": [
            "pornhub",
            "xvideos",
            "xhamster",
            "xnxx",
            "redtube",
            "youporn",
            "spankbang",
            "onlyfans",
            "fansly",
            "chaturbate",
            "stripchat",
            "bongacams",
            "nhentai",
            "hanime",
            "rule34",
            "f95zone",
            "dlsite",
            "missav",
            "javbus",
            "javdb",
        ],
        "content_terms": [
            "porn",
            "pornography",
            "pornographic",
            "xxx",
            "nsfw",
            "hentai",
            "eroge",
            "adult content",
            "adult video",
            "adult videos",
            "adult movie",
            "adult movies",
            "adult site",
            "adult sites",
            "adult entertainment",
            "explicit sexual content",
            "sexual content",
            "adult-only sexual content",
            "adult only sexual content",
            "nude video",
            "nude videos",
            "nude photos",
            "nudity",
            "webcam sex",
            "adult dating",
            "18+",
            "r18",
            "r-18",
        ],
        "media_terms": [
            "adult media",
            "adult image",
            "adult images",
            "adult photo",
            "adult photos",
            "adult clip",
            "adult clips",
            "nsfw video",
            "nsfw image",
        ],
        "game_terms": [
            "adult game",
            "adult games",
            "sex game",
            "sex games",
            "eroge",
            "visual novel adult",
            "adult only game",
        ],
        "cjk_terms": [
            "色情",
            "成人内容",
            "成人视频",
            "成人影片",
            "成人电影",
            "成人图片",
            "成人照片",
            "成人游戏",
            "黄色游戏",
            "黄油",
            "裸露",
            "性内容",
            "18禁",
            "アダルト",
            "エロゲ",
        ],
        "context_regexes": [
            r"\badult[\s_-]*(?:only|18\+)?[\s_-]*(?:sexual|explicit)[\s_-]*content\b",
            r"\b(?:nsfw|xxx|r-?18)\b",
        ],
    }
