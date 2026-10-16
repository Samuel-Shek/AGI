from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import uuid
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from typing import Callable

from .app_identity import BROWSER_FAMILIES, app_identity_in, resolve_app_identity, same_app_identity
from .attribution_context import safe_reason_codes
from .browser_signals import event_fields_from_browser_signal, list_recent_browser_signals, read_latest_browser_signal
from .confirmation_evidence import INTERNAL_FRAME_OWNER, INTERNAL_FRAME_REF
from .document_signals import build_document_signal
from .ocr_service import (
    OCR_ADMISSION_DEFERRED,
    OCR_HELPER_UNAVAILABLE,
    OcrAdmissionDeferredError,
    OcrOutcome,
    ocr_service,
    run_ocr,
    submit_ocr,
)
from .privacy import classify_event
from .safe_url import safe_hostname_from_url
from .screen_regions import REGION_UNKNOWN, classify_screen_region
from .swift_helpers import compiled_helper_lease, helper_command


def capture_once(root_dir: str | Path, with_screenshot_ocr: bool = False) -> dict:
    root = Path(root_dir)
    raw_vault = _raw_vault(root)
    frontmost = _frontmost_context()
    accessibility = _accessibility_context(root)
    browser = _browser_url(
        frontmost.get("app_name", ""),
        frontmost.get("bundle_id", ""),
    )
    ocr_text = ""
    ocr_blocks: list[dict] = []
    if with_screenshot_ocr:
        ocr_payload = _capture_and_ocr_payload(raw_vault, root)
        ocr_text = _ocr_text_from_payload(ocr_payload)
        ocr_blocks = _ocr_blocks_from_payload(
            ocr_payload,
            display_id="active-display",
            window_id=str(frontmost.get("frontmost_window_id") or ""),
        )

    event = {
        "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "display_id": "active-display",
        "app_name": frontmost.get("app_name") or "Unknown",
        "bundle_id": frontmost.get("bundle_id") or "",
        "window_title": frontmost.get("window_title") or "",
        "url": browser.get("url") or "",
        "domain": browser.get("domain") or "",
        "ocr_text": ocr_text,
        "ocr_blocks": ocr_blocks,
        "context_confidence": 0.72 if frontmost.get("app_name") else 0.35,
        "url_source": browser.get("source") or "unknown",
        "context_source": "macos_probe",
    }
    _merge_accessibility(event, accessibility, display_id="active-display")
    _merge_browser_signal(root, event, frontmost)
    _merge_document_signal(root, event, frontmost, browser)
    return event


def capture_loop(root_dir: str | Path, count: int, with_screenshot_ocr: bool = False) -> list[dict]:
    return [capture_once(root_dir, with_screenshot_ocr) for _ in range(count)]


FrameDecision = Callable[[dict], bool]
OCRAdmissionCheck = Callable[[], bool | dict]


def capture_sck_keyframes(root_dir: str | Path) -> list[dict]:
    return capture_sck_keyframe_batch(root_dir)["events"]


def capture_sck_keyframe_batch(
    root_dir: str | Path,
    should_process: FrameDecision | None = None,
    retain_for_ingest: bool = False,
) -> dict:
    root = Path(root_dir)
    raw_vault = _raw_vault(root)
    events: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []
    records: list[dict] = []
    capture_batch_id = f"sck_batch_{uuid.uuid4().hex}"
    batch_captured_at = datetime.now().astimezone().isoformat(timespec="seconds")
    batch_ready = False
    service = ocr_service()
    with service.live_batch(), ExitStack() as helper_leases:
        try:
            frontmost = _frontmost_context()
            accessibility = _accessibility_context(root)
            browser = _browser_url(
                frontmost.get("app_name", ""),
                frontmost.get("bundle_id", ""),
            )
            completed = subprocess.run(
                [*helper_command(root, "sck_keyframe"), str(raw_vault)],
                check=False,
                timeout=30,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if completed.returncode != 0 and not completed.stdout.strip():
                raise RuntimeError("sck_capture_failed")

            payload = json.loads(completed.stdout)
            _chmod_payload_images_private(payload)
            for ordinal, item in enumerate(payload):
                display_id = f"sck-display-{item.get('display_id')}"
                metadata = {
                    "capture_batch_id": capture_batch_id,
                    "captured_at": batch_captured_at,
                    "display_id": display_id,
                    "frame_hash": str(item.get("frame_hash") or ""),
                    "is_main": bool(item.get("is_main")),
                    "capture_width": item.get("width"),
                    "capture_height": item.get("height"),
                    "display_bounds": _safe_rect(item.get("display_frame")),
                }
                image_path_value = item.get("image_path")
                if item.get("status") != "captured" or not image_path_value:
                    errors.append(
                        {
                            **metadata,
                            "reason": "sck_capture_failed",
                            "status": item.get("status") or "error",
                        }
                    )
                    continue
                image_path = Path(image_path_value)
                _chmod_private_file(image_path)
                record = {
                    "ordinal": ordinal,
                    "metadata": metadata,
                    "image_path": image_path,
                    "file_identity": _frame_file_identity(image_path),
                    "state": "preflight_error",
                    "base_event": None,
                    "use_frontmost_context": False,
                    "task": None,
                    "retained_for_ingest": False,
                    "cleaned": False,
                    "cleanup_reported": False,
                }
                records.append(record)
                try:
                    if should_process is not None and not should_process(metadata):
                        record["state"] = "duplicate"
                        continue
                    display_context = _display_attribution(item, frontmost=frontmost)
                    use_frontmost_context = bool(
                        metadata.get("is_main")
                        and "main_display_frontmost" in (display_context.get("reason_codes") or [])
                    )
                    base_event = {
                        **metadata,
                        "app_name": display_context.get("app_name") or "Unknown",
                        "bundle_id": display_context.get("bundle_id") or "",
                        "window_title": display_context.get("window_title") or "",
                        "frontmost_window_id": display_context.get("window_id") or "",
                        "window_bounds": display_context.get("window_bounds") or {},
                        "visible_windows": _safe_visible_windows(item.get("visible_windows")),
                        "display_attribution": display_context,
                        "url": (browser.get("url") or "") if use_frontmost_context else "",
                        "domain": (browser.get("domain") or "") if use_frontmost_context else "",
                        "ocr_text": "",
                        "context_confidence": float(
                            display_context.get("confidence") or (0.78 if use_frontmost_context else 0.35)
                        ),
                        "url_source": (browser.get("source") or "unknown")
                        if use_frontmost_context
                        else "display_ocr_only",
                        "context_source": display_context.get("source") or "screencapturekit_keyframe",
                    }
                    if use_frontmost_context:
                        _merge_accessibility(base_event, accessibility, display_id=display_id)
                        _merge_browser_signal(root, base_event, frontmost)
                    else:
                        _merge_display_browser_signal(root, base_event)
                    record["base_event"] = base_event
                    record["use_frontmost_context"] = use_frontmost_context
                    preflight = classify_event(base_event)
                    record["state"] = "privacy_blocked" if preflight.status == "blocked" else "eligible"
                except Exception:
                    record["state"] = "preflight_error"

            eligible = [record for record in records if record["state"] == "eligible"]
            command: list[str] | None = None
            if eligible:
                try:
                    command = helper_leases.enter_context(
                        compiled_helper_lease(root, "vision_ocr")
                    )
                except (RuntimeError, ValueError):
                    command = None
            for record in eligible:
                if command is None:
                    record["outcome"] = OcrOutcome(error_code=OCR_HELPER_UNAVAILABLE)
                else:
                    record["task"] = submit_ocr(root, command, record["image_path"])

            for record in records:
                metadata = record["metadata"]
                state = record["state"]
                if state == "duplicate":
                    skipped.append({**metadata, "reason": "duplicate_frame"})
                    _cleanup_captured_frame(record, errors)
                    continue
                if state == "privacy_blocked":
                    events.append({**record["base_event"], "preflight_blocked": True})
                    _cleanup_captured_frame(record, errors)
                    continue
                if state == "preflight_error":
                    errors.append({**metadata, "reason": "privacy_preflight_failed", "status": "error"})
                    _cleanup_captured_frame(record, errors)
                    continue

                task = record.get("task")
                outcome = task.result() if task is not None else record.get("outcome")
                if not isinstance(outcome, OcrOutcome) or not outcome.ok:
                    reason = outcome.error_code if isinstance(outcome, OcrOutcome) else "ocr_process_failed"
                    errors.append({**metadata, "reason": reason, "status": "error"})
                    _cleanup_captured_frame(record, errors)
                    continue

                image_path = record["image_path"]
                base_event = record["base_event"]
                try:
                    ocr_payload = outcome.payload or {}
                    ocr_blocks = _ocr_blocks_from_payload(
                        ocr_payload,
                        display_id=str(metadata["display_id"]),
                        window_id=str(base_event.get("frontmost_window_id") or ""),
                    )
                    event = {
                        **base_event,
                        "ocr_text": _ocr_text_from_payload(ocr_payload),
                        "ocr_blocks": [*base_event.get("ocr_blocks", []), *ocr_blocks],
                    }
                    if record["use_frontmost_context"]:
                        _merge_document_signal(root, event, frontmost, browser)
                    if retain_for_ingest:
                        event[INTERNAL_FRAME_REF] = str(image_path)
                        event[INTERNAL_FRAME_OWNER] = "capture"
                        record["retained_for_ingest"] = True
                    events.append(event)
                    if not retain_for_ingest:
                        _cleanup_captured_frame(record, errors)
                except Exception:
                    errors.append({**metadata, "reason": "ocr_postprocess_failed", "status": "error"})
                    _cleanup_captured_frame(record, errors)

            batch_ready = True
            return {
                "capture_batch_id": capture_batch_id,
                "captured_at": batch_captured_at,
                "events": events,
                "skipped": skipped,
                "errors": errors,
            }
        finally:
            tasks = [record["task"] for record in records if record.get("task") is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            for task in tasks:
                task.wait_terminal()
            for record in records:
                if not (batch_ready and record.get("retained_for_ingest")):
                    _cleanup_captured_frame(record, errors)




def build_ocr_event_from_confirmation_image(
    image_path: str | Path,
    base_event: dict,
    *,
    root_dir: str | Path,
) -> dict:
    """Re-run local Apple Vision OCR without invoking VLM or any network service."""
    image = Path(image_path)
    display_id = str(base_event.get("display_id") or "confirmation-evidence")
    payload = _ocr_payload(image, root_dir)
    return {
        **base_event,
        "ocr_text": _ocr_text_from_payload(payload),
        "ocr_blocks": _ocr_blocks_from_payload(
            payload,
            display_id=display_id,
            window_id=str(base_event.get("frontmost_window_id") or ""),
        ),
        "context_source": "confirmation_evidence_apple_vision",
    }




def _frontmost_attribution(frontmost: dict) -> dict:
    return {
        "app_name": str(frontmost.get("app_name") or "Unknown"),
        "bundle_id": str(frontmost.get("bundle_id") or ""),
        "window_title": str(frontmost.get("window_title") or ""),
        "window_id": str(frontmost.get("frontmost_window_id") or ""),
        "window_bounds": _safe_rect(frontmost.get("window_bounds")),
        "confidence": 0.78 if frontmost.get("app_name") else 0.35,
        "source": "screencapturekit_keyframe+frontmost",
        "reason_codes": ["main_display_frontmost"],
    }


def _display_attribution(item: dict, *, frontmost: dict) -> dict:
    windows = _safe_visible_windows(item.get("visible_windows"), include_title=True)
    selected = _best_display_window(windows)
    if bool(item.get("is_main")):
        frontmost_attribution = _frontmost_attribution(frontmost)
        if _known_app(frontmost_attribution.get("app_name")):
            return frontmost_attribution
        if selected:
            return _window_attribution(
                selected,
                confidence=max(0.72, _window_confidence(selected)),
                source="screencapturekit_keyframe+main_display_window_fallback",
                reason_codes=["main_display_window_fallback"],
            )
    if not windows:
        return {
            "app_name": "Unknown",
            "bundle_id": "",
            "window_title": "",
            "window_id": "",
            "window_bounds": {},
            "confidence": 0.32,
            "source": "screencapturekit_keyframe+display_window_list_empty",
            "reason_codes": ["display_window_list_empty"],
        }
    if not selected:
        return {
            "app_name": "Unknown",
            "bundle_id": "",
            "window_title": "",
            "window_id": "",
            "window_bounds": {},
            "confidence": 0.35,
            "source": "screencapturekit_keyframe+display_window_unmatched",
            "reason_codes": ["display_window_unmatched"],
        }
    return _window_attribution(
        selected,
        confidence=_window_confidence(selected),
        source="screencapturekit_keyframe+display_window_match",
        reason_codes=["display_window_match"],
    )


def _best_display_window(windows: list[dict]) -> dict:
    candidates: list[tuple[float, int, dict]] = []
    for index, window in enumerate(windows):
        app = str(window.get("app_name") or "").strip().lower()
        if not app or app in {"window server", "dock", "control center", "notification center", "loginwindow"}:
            continue
        overlap = _safe_float(window.get("overlap_ratio"), default=0.0)
        score = overlap * 100.0
        if bool(window.get("has_title")):
            score += 6.0
        if app in {"finder", "wps office", "wps", "google chrome", "safari", "arc", "dia", "microsoft edge", "firefox"}:
            score += 2.0
        if app in {"personal ai monitor", "personal ai monitor menu bar"}:
            score -= 20.0
        candidates.append((score, -index, window))
    return max(candidates, key=lambda item: (item[0], item[1]))[2] if candidates else {}


def _known_app(value: object) -> bool:
    return str(value or "").strip().lower() not in {"", "unknown", "未知"}


def _window_confidence(window: dict) -> float:
    overlap = _safe_float(window.get("overlap_ratio"), default=0.0)
    if overlap >= 0.75:
        return 0.76
    if overlap >= 0.40:
        return 0.70
    if overlap >= 0.15:
        return 0.60
    return 0.52


def _window_attribution(
    selected: dict,
    *,
    confidence: float,
    source: str,
    reason_codes: list[str],
) -> dict:
    return {
        "app_name": selected.get("app_name") or "Unknown",
        "bundle_id": selected.get("bundle_id") or "",
        "window_title": selected.get("title") or "",
        "window_id": selected.get("window_id") or "",
        "window_bounds": selected.get("frame") or {},
        "confidence": min(0.82, max(0.0, float(confidence))),
        "source": source,
        "reason_codes": reason_codes,
        "overlap_ratio": selected.get("overlap_ratio") or 0.0,
    }


def _safe_visible_windows(value: object, *, include_title: bool = False) -> list[dict]:
    if not isinstance(value, list):
        return []
    safe: list[dict] = []
    for item in value[:8]:
        if not isinstance(item, dict):
            continue
        app_name = _safe_short(item.get("app_name") or "")
        bundle_id = _safe_short(item.get("bundle_id") or "")
        if not app_name and not bundle_id:
            continue
        row = {
            "window_id": _safe_short(item.get("window_id") or ""),
            "app_name": app_name,
            "bundle_id": bundle_id,
            "has_title": bool(str(item.get("title") or "").strip()),
            "frame": _safe_rect(item.get("frame")),
            "overlap_ratio": _safe_float(item.get("overlap_ratio"), default=0.0),
        }
        if include_title:
            row["title"] = _safe_short(item.get("title") or "", max_len=160)
        safe.append(row)
    return safe


def _safe_rect(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    rect = {}
    for key in ("x", "y", "width", "height"):
        number = _safe_float(value.get(key), default=0.0)
        if number:
            rect[key] = number
    return rect


def _safe_float(value: object, *, default: float) -> float:
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return default


def _safe_short(value: object, *, max_len: int = 120) -> str:
    return " ".join(str(value or "").split())[:max_len]


def _frontmost_context() -> dict:
    script = r'''
    tell application "System Events"
      set frontApp to first application process whose frontmost is true
      set appName to name of frontApp
      set bundleId to ""
      try
        set bundleId to bundle identifier of frontApp
      end try
      set windowTitle to ""
      try
        set windowTitle to name of front window of frontApp
      end try
      return appName & "\n" & bundleId & "\n" & windowTitle
    end tell
    '''
    result = _osascript(script)
    if not result["ok"]:
        return {"app_name": "Unknown", "bundle_id": "", "window_title": ""}
    lines = result["stdout"].splitlines()
    return {
        "app_name": lines[0] if len(lines) > 0 else "Unknown",
        "bundle_id": lines[1] if len(lines) > 1 else "",
        "window_title": lines[2] if len(lines) > 2 else "",
    }


def _browser_url(app_name: str, bundle_id: str = "") -> dict:
    identity = resolve_app_identity(app_name, bundle_id)
    if not identity.matched or identity.family not in BROWSER_FAMILIES:
        return {"url": "", "domain": "", "source": "not_browser"}
    if identity.family == "dia":
        script = 'tell application "Dia" to return URL of active tab of front window'
        source = "applescript_dia"
    elif identity.family == "safari":
        script = 'tell application "Safari" to return URL of front document'
        source = "applescript_safari"
    elif identity.family == "chrome":
        script = 'tell application "Google Chrome" to return URL of active tab of front window'
        source = "applescript_chrome"
    elif identity.family == "arc":
        script = 'tell application "Arc" to return URL of active tab of front window'
        source = "applescript_arc"
    else:
        return {"url": "", "domain": "", "source": "not_browser"}
    result = _osascript(script)
    if not result["ok"]:
        return {"url": "", "domain": "", "source": f"{source}_failed"}
    url = result["stdout"].strip()
    payload = {"url": url, "domain": _domain(url), "source": source}
    if identity.family == "chrome":
        mode = _osascript('tell application "Google Chrome" to return mode of front window')
        if mode["ok"] and mode["stdout"].strip().lower() != "normal":
            payload["private_window"] = True
    return payload


def _accessibility_context(root: Path) -> dict:
    try:
        completed = subprocess.run(
            [*helper_command(root, "accessibility_probe")],
            check=False,
            timeout=5,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (RuntimeError, subprocess.TimeoutExpired):
        return {"trusted": False, "text": "", "nodes": [], "secure_input_detected": False}
    if completed.returncode != 0 or not completed.stdout.strip():
        return {"trusted": False, "text": "", "nodes": [], "secure_input_detected": False}
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"trusted": False, "text": "", "nodes": [], "secure_input_detected": False}
    return payload if isinstance(payload, dict) else {}


def _merge_accessibility(event: dict, accessibility: dict, *, display_id: str) -> None:
    if not accessibility:
        return
    event["accessibility_trusted"] = bool(accessibility.get("trusted"))
    if accessibility.get("secure_input_detected") is True:
        event["accessibility_secure_input"] = True
        event["secure_input"] = True
    text = str(accessibility.get("text") or "").strip()
    if text:
        event["accessibility_text"] = text
        blocks = [
            {
                "text": str(node.get("text") or ""),
                "confidence": 0.99,
                "region": "unknown",
                "display_id": display_id,
                "window_id": str(event.get("frontmost_window_id") or ""),
                "source": "accessibility",
                "role": str(node.get("role") or ""),
            }
            for node in accessibility.get("nodes") or []
            if isinstance(node, dict) and str(node.get("text") or "").strip()
        ]
        if blocks:
            event["ocr_blocks"] = [*event.get("ocr_blocks", []), *blocks]
    event["context_source"] = f"{event.get('context_source') or 'macos_probe'}+accessibility"


def _merge_browser_signal(root: Path, event: dict, frontmost: dict) -> None:
    signal = read_latest_browser_signal(
        root,
        app_name=str(frontmost.get("app_name") or event.get("app_name") or ""),
        bundle_id=str(frontmost.get("bundle_id") or event.get("bundle_id") or ""),
    )
    if not signal:
        if event.get("private_window") is None:
            browser_private = event.get("private_window") or False
            if browser_private:
                event["private_window"] = True
        return
    fields = event_fields_from_browser_signal(signal)
    for key, value in fields.items():
        if key == "url" and event.get("url"):
            continue
        if key == "domain" and event.get("domain"):
            continue
        event[key] = value
    event["context_source"] = f"{event.get('context_source') or 'macos_probe'}+browser_signal"


def _merge_display_browser_signal(root: Path, event: dict) -> None:
    if not _is_browser_context(str(event.get("app_name") or ""), str(event.get("bundle_id") or "")):
        return
    candidates = [
        signal
        for signal in list_recent_browser_signals(root)
        if _browser_signal_matches_event(signal, event)
    ]
    unique_candidates: dict[tuple[str, str, str, str, str], dict] = {}
    for signal in candidates:
        key = (
            str(signal.get("browser") or "").lower(),
            str(signal.get("bundle_id") or "").lower(),
            str(signal.get("window_id") or ""),
            str(signal.get("tab_id") or ""),
            str(signal.get("domain") or "").lower(),
        )
        unique_candidates[key] = signal
    candidates = list(unique_candidates.values())
    if not candidates:
        _append_display_reason(event, "browser_signal_missing")
        return
    if len(candidates) != 1:
        _append_display_reason(event, "browser_signal_ambiguous")
        return
    signal = candidates[0]
    if _browser_signal_has_privacy_risk(signal):
        _append_display_reason(event, "browser_signal_risky")
        return
    fields = event_fields_from_browser_signal(signal)
    for key, value in fields.items():
        if key == "url":
            event[key] = value
        elif key == "domain":
            event[key] = value
        elif key == "context_confidence":
            event[key] = max(float(event.get("context_confidence") or 0.0), min(float(value or 0.0), 0.82))
        else:
            event[key] = value
    _append_display_reason(event, "browser_signal_display_match")
    event["context_source"] = f"{event.get('context_source') or 'macos_probe'}+browser_signal"


def _append_display_reason(event: dict, reason: str) -> None:
    attribution = event.get("display_attribution")
    if not isinstance(attribution, dict):
        attribution = {}
        event["display_attribution"] = attribution
    attribution["reason_codes"] = safe_reason_codes([*(attribution.get("reason_codes") or []), reason])


def _is_browser_context(app_name: str, bundle_id: str) -> bool:
    return app_identity_in(app_name, bundle_id, BROWSER_FAMILIES)


def _browser_signal_matches_event(signal: dict, event: dict) -> bool:
    return same_app_identity(
        event.get("app_name"),
        event.get("bundle_id"),
        signal.get("browser"),
        signal.get("bundle_id"),
    )


def _browser_signal_has_privacy_risk(signal: dict) -> bool:
    return bool(
        signal.get("private_window")
        or signal.get("has_password_field")
        or signal.get("has_payment_form")
        or signal.get("has_login_form")
        or signal.get("risk_tags")
    )


def _merge_document_signal(root: Path, event: dict, frontmost: dict, browser: dict) -> None:
    document_path = _current_document_path(frontmost, browser)
    if document_path:
        event["document_path"] = document_path
        event["document_source"] = "foreground_app_or_file_url"
    signal = build_document_signal(root, event)
    if not signal or signal.get("status") == "rejected":
        if signal and signal.get("status") == "rejected":
            event.setdefault("document_signal_status", signal.get("reason") or "rejected")
        return
    event["document_signal"] = signal
    context_text = str(signal.get("context_text_for_privacy") or "")
    if context_text:
        event["document_text_for_privacy"] = context_text
    event["context_source"] = f"{event.get('context_source') or 'macos_probe'}+document_signal"


def _current_document_path(frontmost: dict, browser: dict) -> str:
    url = str(browser.get("url") or "")
    if url.startswith("file://"):
        return unquote_url_path(url)
    identity = resolve_app_identity(
        frontmost.get("app_name"),
        frontmost.get("bundle_id"),
    )
    scripts = {
        "textedit": 'tell application "TextEdit" to return POSIX path of (path of front document)',
        "preview": 'tell application "Preview" to return POSIX path of (path of front document)',
        "pages": 'tell application "Pages" to return POSIX path of (file of front document as alias)',
        "word": 'tell application "Microsoft Word" to return POSIX path of (path of active document)',
        "wps": 'tell application "wpsoffice" to return POSIX path of (path of active document)',
    }
    script = scripts.get(identity.family) if identity.matched else None
    if script:
        result = _osascript(script)
        if result["ok"] and result["stdout"].strip():
            return result["stdout"].strip()
    return ""


def unquote_url_path(url: str) -> str:
    from urllib.parse import unquote, urlparse

    parsed = urlparse(url)
    return unquote(parsed.path)


def _capture_and_ocr(raw_vault: Path, root_dir: str | Path) -> str:
    return _ocr_text_from_payload(_capture_and_ocr_payload(raw_vault, root_dir))


def _capture_and_ocr_payload(raw_vault: Path, root_dir: str | Path) -> dict:
    with tempfile.TemporaryDirectory(dir=raw_vault) as tmp:
        image_path = Path(tmp) / "capture.png"
        try:
            subprocess.run(
                ["screencapture", "-x", "-t", "png", str(image_path)],
                check=True,
                timeout=8,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            _chmod_private_file(image_path)
            return _ocr_payload(image_path, root_dir)
        finally:
            try:
                image_path.unlink(missing_ok=True)
            except OSError:
                pass


def _ocr_image(image_path: Path, root_dir: str | Path) -> str:
    return _ocr_text_from_payload(_ocr_payload(image_path, root_dir))


def _ocr_payload(
    image_path: Path,
    root_dir: str | Path,
    *,
    admission_check: OCRAdmissionCheck | None = None,
) -> dict:
    try:
        lease = compiled_helper_lease(root_dir, "vision_ocr")
    except (RuntimeError, ValueError) as exc:
        raise RuntimeError(OCR_HELPER_UNAVAILABLE) from exc
    try:
        with lease as resolved:
            outcome = run_ocr(
                root_dir,
                resolved,
                image_path,
                admission_check=admission_check,
            )
    except (RuntimeError, ValueError) as exc:
        raise RuntimeError(OCR_HELPER_UNAVAILABLE) from exc
    if not outcome.ok:
        if outcome.error_code == OCR_ADMISSION_DEFERRED:
            raise OcrAdmissionDeferredError(OCR_ADMISSION_DEFERRED)
        raise RuntimeError(outcome.error_code or "ocr_process_failed")
    return outcome.payload or {"lines": []}


def _frame_file_identity(path: Path) -> tuple[int, int, int, int, int] | None:
    try:
        info = path.lstat()
    except OSError:
        return None
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        return None
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _cleanup_captured_frame(record: dict, errors: list[dict]) -> None:
    if record.get("cleaned"):
        return
    path = Path(record["image_path"])
    expected = record.get("file_identity")
    try:
        current = _frame_file_identity(path)
        if current is None:
            if not path.exists() and not path.is_symlink():
                record["cleaned"] = True
                return
            raise OSError("raw frame identity unavailable")
        if expected is None or current != expected:
            raise OSError("raw frame identity changed")
        path.unlink()
        record["cleaned"] = True
    except OSError:
        if not record.get("cleanup_reported"):
            errors.append(
                {
                    **record["metadata"],
                    "reason": "raw_cleanup_deferred",
                    "status": "error",
                }
            )
            record["cleanup_reported"] = True


def _ocr_text_from_payload(payload: dict) -> str:
    return " ".join(item["text"] for item in payload.get("lines", []) if item.get("text"))


def _ocr_blocks_from_payload(payload: dict, *, display_id: str, window_id: str = "") -> list[dict]:
    blocks: list[dict] = []
    for item in payload.get("lines", []):
        if not isinstance(item, dict) or not item.get("text"):
            continue
        blocks.append(
            {
                "text": str(item.get("text") or ""),
                "confidence": item.get("confidence"),
                "bounding_box": item.get("bounding_box") or item.get("boundingBox") or {},
                "display_id": display_id,
                "window_id": window_id,
                "coordinate_space": payload.get("coordinate_space") or "vision_normalized_bottom_left",
            }
        )
    return blocks


def _raw_vault(root: Path) -> Path:
    vault = root / "raw_vault"
    vault.mkdir(parents=True, exist_ok=True)
    os.chmod(vault, 0o700)
    marker = vault / ".metadata_never_index"
    marker.touch(exist_ok=True)
    return vault


def _chmod_private_file(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _chmod_payload_images_private(payload: list[dict]) -> None:
    for item in payload:
        if item.get("status") != "captured":
            continue
        image_path_value = item.get("image_path")
        if image_path_value:
            _chmod_private_file(Path(image_path_value))


def _osascript(script: str) -> dict:
    try:
        completed = subprocess.run(
            ["osascript", "-e", script],
            check=False,
            timeout=3,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "stderr": "osascript_timeout"}
    return {
        "ok": completed.returncode == 0,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def _domain(url: str) -> str:
    return safe_hostname_from_url(url)
