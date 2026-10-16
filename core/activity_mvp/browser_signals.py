from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import struct
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .adult_policy import ADULT_RISK_TAG, domain_hit as adult_domain_hit
from .app_identity import same_app_identity
from .safe_url import redact_url_for_storage, safe_hostname_from_url, safe_scheme_from_url


NATIVE_HOST_NAME = "app.personalai.monitor.browser_signal"
SIGNAL_TTL_SECONDS = 45
RISK_TAGS = {
    "private_window",
    "password_field",
    "payment_form",
    "login_form",
    "financial_domain",
    "secret_like",
    "local_file",
    ADULT_RISK_TAG,
}
CHROMIUM_HOST_DIRS = {
    "dia": Path.home() / "Library/Application Support/Dia/User Data/NativeMessagingHosts",
    "chrome": Path.home() / "Library/Application Support/Google/Chrome/NativeMessagingHosts",
    "arc": Path.home() / "Library/Application Support/Arc/User Data/NativeMessagingHosts",
}


def write_browser_signal(root_dir: str | Path, payload: dict) -> dict:
    root = Path(root_dir)
    signal = sanitize_browser_signal(payload)
    signal_dir = _signal_dir(root)
    signal_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(signal_dir, 0o700)
    browser_dir = _browser_signal_dir(root)
    browser_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(browser_dir, 0o700)
    _prune_expired_browser_signals(root)
    signal_path = browser_dir / f"{_browser_signal_key(signal)}.json"
    signal_path.write_text(json.dumps(signal, ensure_ascii=False, indent=2), encoding="utf-8")
    signal_path.chmod(0o600)
    legacy_path = signal_dir / "browser_latest.json"
    legacy_path.write_text(json.dumps(signal, ensure_ascii=False, indent=2), encoding="utf-8")
    legacy_path.chmod(0o600)
    return signal


def read_latest_browser_signal(root_dir: str | Path, app_name: str = "", bundle_id: str = "") -> dict:
    root = Path(root_dir)
    signals = _recent_browser_signals(root)
    for signal in signals:
        if _browser_matches(signal, app_name, bundle_id):
            return signal
    if not app_name and not bundle_id and signals:
        return signals[0]
    return {}


def list_recent_browser_signals(root_dir: str | Path) -> list[dict]:
    return _recent_browser_signals(Path(root_dir))


def sanitize_browser_signal(payload: dict) -> dict:
    now = datetime.now(timezone.utc)
    observed_at = _parse_time(str(payload.get("observed_at") or "")) or now
    url = str(payload.get("url") or "")
    scheme = safe_scheme_from_url(url, str(payload.get("scheme") or ""))
    domain = safe_hostname_from_url(url, str(payload.get("domain") or ""))
    risk_tags = sorted(
        {
            str(tag)
            for tag in payload.get("risk_tags") or []
            if str(tag) in RISK_TAGS
        }
    )
    if scheme == "file":
        risk_tags.append("local_file")
    if _adult_domain_hit(domain):
        risk_tags.append(ADULT_RISK_TAG)
    if payload.get("private_window") is True:
        risk_tags.append("private_window")
    if payload.get("has_password_field") is True:
        risk_tags.append("password_field")
    if payload.get("has_payment_form") is True:
        risk_tags.append("payment_form")
    risk_tags = sorted(set(risk_tags))
    return {
        "observed_at": observed_at.isoformat(timespec="seconds"),
        "expires_at": (observed_at + timedelta(seconds=SIGNAL_TTL_SECONDS)).isoformat(timespec="seconds"),
        "browser": _safe_short(payload.get("browser") or payload.get("app_name") or ""),
        "bundle_id": _safe_short(payload.get("bundle_id") or ""),
        "domain": domain,
        "scheme": scheme,
        "url_redacted": _redact_url(url, scheme=scheme, domain=domain),
        "private_window": bool(payload.get("private_window") is True),
        "has_password_field": bool(payload.get("has_password_field") is True),
        "has_payment_form": bool(payload.get("has_payment_form") is True),
        "has_login_form": bool(payload.get("has_login_form") is True),
        "tab_id": _safe_short(payload.get("tab_id") or ""),
        "window_id": _safe_short(payload.get("window_id") or ""),
        "risk_tags": risk_tags,
        "source": _safe_short(payload.get("source") or "chromium_extension_native_host"),
        "confidence": _bounded_float(payload.get("confidence"), default=0.94),
    }


def _adult_domain_hit(domain: str) -> bool:
    return adult_domain_hit(domain).matched


def event_fields_from_browser_signal(signal: dict) -> dict:
    if not signal:
        return {}
    return {
        "domain": str(signal.get("domain") or ""),
        "url": str(signal.get("url_redacted") or ""),
        "url_source": str(signal.get("source") or "browser_native_host"),
        "browser_signal": signal,
        "private_window": bool(signal.get("private_window")),
        "browser_has_password_field": bool(signal.get("has_password_field")),
        "browser_has_payment_form": bool(signal.get("has_payment_form")),
        "browser_has_login_form": bool(signal.get("has_login_form")),
        "browser_risk_tags": list(signal.get("risk_tags") or []),
        "context_confidence": _bounded_float(signal.get("confidence"), default=0.94),
    }


def install_native_host_manifests(core_root: str | Path, browsers: list[str] | None = None, extension_id: str = "") -> dict:
    core_path = Path(core_root).resolve()
    extension_id = _safe_extension_id(extension_id) or extension_id_from_manifest(core_path / "browser_extension" / "manifest.json")
    selected = browsers or ["dia", "chrome", "arc"]
    script_path = core_path / "tools" / "browser_native_host.py"
    results: dict[str, dict] = {}
    for browser in selected:
        key = browser.strip().lower()
        host_dir = CHROMIUM_HOST_DIRS.get(key)
        if host_dir is None:
            results[key] = {"installed": False, "reason": "unsupported_browser"}
            continue
        host_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(host_dir, 0o700)
        manifest_path = host_dir / f"{NATIVE_HOST_NAME}.json"
        origin = (
            f"chrome-extension://{extension_id}/"
            if extension_id
            else "chrome-extension://__EXTENSION_ID__/"
        )
        manifest = {
            "name": NATIVE_HOST_NAME,
            "description": "Personal AI Monitor local browser signal bridge",
            "path": str(script_path),
            "type": "stdio",
            "allowed_origins": [origin],
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        manifest_path.chmod(0o600)
        _ensure_executable(script_path)
        results[key] = {
            "installed": True,
            "manifest": str(manifest_path),
            "extension_id": extension_id,
            "origin_configured": bool(extension_id),
        }
    return results


def extension_id_from_manifest(manifest_path: str | Path) -> str:
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return ""
    return extension_id_from_public_key(str(manifest.get("key") or ""))


def extension_id_from_public_key(public_key_b64: str) -> str:
    value = "".join(str(public_key_b64 or "").split())
    if not value:
        return ""
    try:
        public_key = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        return ""
    digest = hashlib.sha256(public_key).digest()[:16]
    return "".join(chr(ord("a") + ((byte >> 4) & 0x0F)) + chr(ord("a") + (byte & 0x0F)) for byte in digest)


def _safe_extension_id(value: str) -> str:
    candidate = str(value or "").strip().lower()
    if re.fullmatch(r"[a-p]{32}", candidate):
        return candidate
    return ""


def native_host_loop(core_root: str | Path) -> None:
    native_identity = browser_identity_from_parent_process()
    while True:
        payload = _read_native_message()
        if payload is None:
            return
        try:
            payload = _merge_native_browser_identity(payload, native_identity)
            signal = write_browser_signal(Path(core_root) / "data", payload)
            _write_native_message({"ok": True, "stored": True, "signal": signal})
        except Exception as exc:
            _write_native_message({"ok": False, "error": type(exc).__name__})


def browser_identity_from_parent_process() -> dict:
    names: list[str] = []
    pid = os.getppid()
    for _ in range(8):
        if pid <= 1:
            break
        info = _process_info(pid)
        if not info:
            break
        names.append(info["command"])
        pid = info["ppid"]
    return _browser_identity_from_process_names(names)


def _browser_identity_from_process_names(names: list[str]) -> dict:
    joined = " ".join(str(name or "") for name in names).lower()
    if re.search(r"(^|[/\s._-])dia($|[/\s._-])|thebrowser\.dia", joined):
        return {"browser": "Dia", "bundle_id": "company.thebrowser.dia", "source": "native_parent_process"}
    if re.search(r"(^|[/\s._-])arc($|[/\s._-])|thebrowser\.browser", joined):
        return {"browser": "Arc", "bundle_id": "company.thebrowser.Browser", "source": "native_parent_process"}
    if "google chrome" in joined or "chrome helper" in joined:
        return {"browser": "Chrome", "bundle_id": "com.google.Chrome", "source": "native_parent_process"}
    return {}


def _process_info(pid: int) -> dict:
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "ppid=", "-o", "comm="],
            check=False,
            timeout=1,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    line = completed.stdout.strip()
    if completed.returncode != 0 or not line:
        return {}
    parts = line.split(None, 1)
    if not parts:
        return {}
    try:
        ppid = int(parts[0])
    except ValueError:
        ppid = 0
    return {"ppid": ppid, "command": parts[1] if len(parts) > 1 else ""}


def _merge_native_browser_identity(payload: dict, identity: dict) -> dict:
    if not identity:
        return payload
    return {
        **payload,
        "browser": identity.get("browser") or payload.get("browser") or "",
        "bundle_id": identity.get("bundle_id") or payload.get("bundle_id") or "",
        "native_browser_identity_source": identity.get("source") or "",
    }


def native_host_core_root() -> Path:
    raw = os.environ.get("PERSONAL_AI_MONITOR_CORE_DIR") or os.environ.get("PAM_CORE_DIR")
    if raw:
        return Path(raw).expanduser()
    app_support = Path.home() / "Library/Application Support/Personal AI Monitor/core"
    if app_support.exists():
        return app_support
    return Path(__file__).resolve().parents[1]


def _signal_dir(root: Path) -> Path:
    return root / "structured_signals"


def _browser_signal_dir(root: Path) -> Path:
    return _signal_dir(root) / "browser"


def _browser_signal_key(signal: dict) -> str:
    parts = [
        str(signal.get("bundle_id") or ""),
        str(signal.get("browser") or ""),
        str(signal.get("window_id") or ""),
        str(signal.get("tab_id") or ""),
        str(signal.get("domain") or ""),
    ]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"browser_{digest}"


def _recent_browser_signals(root: Path) -> list[dict]:
    signals: list[dict] = []
    for path in [*_browser_signal_dir(root).glob("browser_*.json"), _signal_dir(root) / "browser_latest.json"]:
        try:
            signal = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
        if isinstance(signal, dict) and not _is_expired(signal):
            signals.append(signal)
    signals.sort(key=lambda item: str(item.get("observed_at") or ""), reverse=True)
    return signals


def _prune_expired_browser_signals(root: Path) -> None:
    browser_dir = _browser_signal_dir(root)
    for path in browser_dir.glob("browser_*.json"):
        try:
            signal = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            signal = {}
        if not signal or _is_expired(signal):
            try:
                path.unlink()
            except OSError:
                pass


def _browser_matches(signal: dict, app_name: str, bundle_id: str) -> bool:
    if not app_name and not bundle_id:
        return True
    return same_app_identity(
        app_name,
        bundle_id,
        signal.get("browser"),
        signal.get("bundle_id"),
    )


def _is_expired(signal: dict) -> bool:
    expires = _parse_time(str(signal.get("expires_at") or ""))
    return expires is None or expires <= datetime.now(timezone.utc)


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _redact_url(url: str, *, scheme: str = "", domain: str = "") -> str:
    return redact_url_for_storage(url, scheme=scheme, domain=domain)


def _bounded_float(value, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, parsed))


def _safe_short(value) -> str:
    return " ".join(str(value or "").split())[:120]


def _ensure_executable(path: Path) -> None:
    if not path.exists():
        return
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR)


def _read_native_message() -> dict | None:
    raw_length = sys.stdin.buffer.read(4)
    if len(raw_length) == 0:
        return None
    if len(raw_length) != 4:
        raise ValueError("invalid native message length")
    message_length = struct.unpack("<I", raw_length)[0]
    if message_length <= 0 or message_length > 1024 * 1024:
        raise ValueError("native message too large")
    message = sys.stdin.buffer.read(message_length)
    if len(message) != message_length:
        raise ValueError("incomplete native message")
    payload = json.loads(message.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("native message must be an object")
    payload.setdefault("observed_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return payload


def _write_native_message(payload: dict) -> None:
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(encoded)))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()
