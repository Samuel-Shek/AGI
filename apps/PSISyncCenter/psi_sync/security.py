"""Exact loopback origin, session and framing checks. No CORS exceptions."""
import secrets

HOST = "127.0.0.1"
PORT = 8767
AUTHORITY = f"{HOST}:{PORT}"
ORIGIN = f"http://{AUTHORITY}"
CSP = "; ".join(("default-src 'none'", "script-src 'self'", "style-src 'self'",
                 "img-src 'self'", "font-src 'self'", "connect-src 'self'",
                 "base-uri 'none'", "form-action 'self'", "frame-ancestors 'none'",
                 "object-src 'none'", "media-src 'none'"))


class SecurityError(ValueError):
    pass


def single(headers, name, required=False):
    values = headers.get_all(name, [])
    if len(values) > 1 or (required and not values):
        raise SecurityError("ambiguous_headers")
    return values[0] if values else None


def authorize(headers, token, *, write=False, api=False, port=PORT):
    authority = f"{HOST}:{port}"
    expected_origin = f"http://{authority}"
    if single(headers, "Host", True) != authority:
        raise SecurityError("host_rejected")
    origin = single(headers, "Origin", write)
    if origin is not None and origin != expected_origin:
        raise SecurityError("origin_rejected")
    site = single(headers, "Sec-Fetch-Site")
    if site not in {None, "none", "same-origin"}:
        raise SecurityError("cross_site_rejected")
    if api or write:
        supplied = single(headers, "X-PSI-Session", True)
        if not supplied.isascii() or not secrets.compare_digest(supplied, token):
            raise SecurityError("session_rejected")


def body_length(headers):
    if headers.get_all("Transfer-Encoding") or headers.get_all("Expect"):
        raise SecurityError("framing_rejected")
    value = single(headers, "Content-Length", True)
    if not value.isascii() or not value.isdecimal() or not 0 < int(value) <= 32768:
        raise SecurityError("body_limit")
    if single(headers, "Content-Type", True) != "application/json":
        raise SecurityError("content_type_rejected")
    return int(value)
