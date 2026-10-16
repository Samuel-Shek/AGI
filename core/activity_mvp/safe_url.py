from __future__ import annotations

from urllib.parse import urlparse


def safe_hostname_from_url(url: str, fallback_domain: str = "") -> str:
    parsed = urlparse(str(url or ""))
    host = parsed.hostname or _hostname_from_raw_domain(fallback_domain)
    return _normalize_hostname(host)


def safe_scheme_from_url(url: str, fallback_scheme: str = "") -> str:
    parsed = urlparse(str(url or ""))
    return (parsed.scheme or str(fallback_scheme or "")).lower()


def redact_url_for_storage(url: str, *, scheme: str = "", domain: str = "") -> str:
    safe_scheme = (scheme or safe_scheme_from_url(url)).lower()
    safe_domain = safe_hostname_from_url(url, domain)
    if safe_scheme == "file":
        return "file://[local-document]"
    if not safe_domain:
        return ""
    return f"{safe_scheme or 'https'}://{safe_domain}/[redacted]"


def _hostname_from_raw_domain(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "://" in raw:
        parsed = urlparse(raw)
        if parsed.hostname:
            return parsed.hostname
        raw = parsed.netloc or parsed.path
    raw = raw.rsplit("@", 1)[-1]
    if raw.startswith("[") and "]" in raw:
        return raw[1 : raw.index("]")]
    return raw.split(":", 1)[0]


def _normalize_hostname(value: str) -> str:
    host = str(value or "").strip().strip(".").lower()
    if not host:
        return ""
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host
