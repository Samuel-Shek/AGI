from __future__ import annotations

import ipaddress
from urllib.parse import urlparse


LOCALHOST_NAMES = {"localhost", "localhost."}
LOCAL_HTTP_SCHEMES = {"http", "https"}


def validate_local_http_endpoint(endpoint: str) -> dict:
    raw = str(endpoint or "").strip()
    if not raw:
        return _result(False, raw, "empty_endpoint", "模型端点为空，已拒绝调用。")
    parsed = urlparse(raw)
    scheme = parsed.scheme.lower()
    if scheme not in LOCAL_HTTP_SCHEMES:
        return _result(False, raw, "unsupported_scheme", "模型端点必须是本机 HTTP(S) 地址。")
    if parsed.username or parsed.password:
        return _result(False, raw, "userinfo_not_allowed", "模型端点不能包含用户名或密码。")
    host = parsed.hostname or ""
    if not is_loopback_host(host):
        return _result(False, raw, "non_loopback_host", "模型端点不是本机回环地址，已拒绝调用。", host=host)
    return _result(True, raw, "", "", host=host)


def require_local_http_endpoint(endpoint: str) -> str:
    validation = validate_local_http_endpoint(endpoint)
    if not validation["allowed"]:
        raise ValueError(validation["message"] or "endpoint is not local-only")
    return validation["endpoint"]


def is_loopback_host(host: str) -> bool:
    candidate = str(host or "").strip().lower().strip("[]")
    if candidate in LOCALHOST_NAMES:
        return True
    if not candidate:
        return False
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def _result(allowed: bool, endpoint: str, reason: str, message: str, *, host: str = "") -> dict:
    return {
        "allowed": allowed,
        "endpoint": endpoint,
        "host": host,
        "reason": reason,
        "message": message,
    }
