"""AX-first OCR skip: gates still run first; skip only privacy-clear frames."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from activity_mvp import macos_probe
from activity_mvp.macos_probe import (
    AX_SKIP_OCR_ENV,
    DEFAULT_AX_SKIP_OCR_MIN_CHARS,
    OCR_SKIP_REASON_AX,
    ax_skip_ocr_min_chars,
    ax_text_sufficient,
    capture_sck_keyframe_batch,
)
from activity_mvp.ocr_service import OcrOutcome


SAFE_PARAGRAPH = (
    "Quarterly planning notes for the structured signals project describe milestones, "
    "owners and review dates for the next iteration of the local timeline feature."
)


def ax_payload(*, nodes: int = 5, trusted: bool = True, secure: bool = False, text: str = SAFE_PARAGRAPH) -> dict:
    items = [{"role": "AXStaticText", "text": f"{text} #{index}"} for index in range(nodes)]
    return {
        "trusted": trusted,
        "text": "\n".join(item["text"] for item in items),
        "nodes": items,
        "secure_input_detected": secure,
    }


def private_frame(root: Path, name: str) -> Path:
    path = root / name
    path.write_bytes(b"frame-bytes")
    path.chmod(0o600)
    return path


def sck_result(images: list[Path]) -> SimpleNamespace:
    payload = []
    for index, image in enumerate(images, start=1):
        payload.append({
            "display_id": str(index), "status": "captured", "image_path": str(image),
            "frame_hash": f"hash-{index}-{image.name}", "is_main": index == 1, "width": 1280, "height": 720,
            "display_frame": {"x": (index - 1) * 1280, "y": 0, "width": 1280, "height": 720},
            "visible_windows": [],
        })
    return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(payload))


class ImmediateTask:
    def __init__(self, outcome: OcrOutcome) -> None:
        self._outcome = outcome

    def result(self) -> OcrOutcome:
        return self._outcome

    def cancel(self) -> None:
        return None

    def done(self) -> bool:
        return True

    def wait_terminal(self) -> None:
        return None


def ocr_ok(*_args, **_kwargs) -> ImmediateTask:
    return ImmediateTask(OcrOutcome(payload={"lines": [{"text": "ocr line", "confidence": 0.9}]}))


FRONTMOST = {"app_name": "Notes", "bundle_id": "com.apple.Notes", "window_title": "Planning"}


class AxThresholdTests(unittest.TestCase):
    def test_threshold_default_override_bounds_and_off_switch(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(AX_SKIP_OCR_ENV, None)
            self.assertEqual(ax_skip_ocr_min_chars(), DEFAULT_AX_SKIP_OCR_MIN_CHARS)
        for raw, expected in (("600", 600), ("5", 80), ("999999", 8000), ("junk", DEFAULT_AX_SKIP_OCR_MIN_CHARS),
                              ("0", 0), ("off", 0), ("false", 0)):
            with self.subTest(raw=raw), patch.dict(os.environ, {AX_SKIP_OCR_ENV: raw}):
                self.assertEqual(ax_skip_ocr_min_chars(), expected)

    def test_sufficiency_requires_trust_text_volume_nodes_and_no_secure_input(self) -> None:
        def merged(payload: dict) -> dict:
            event = {"context_source": "test"}
            macos_probe._merge_accessibility(event, payload, display_id="d")
            return event

        good = ax_payload()
        self.assertTrue(ax_text_sufficient(merged(good), good, min_chars=400))
        untrusted = ax_payload(trusted=False)
        self.assertFalse(ax_text_sufficient(merged(untrusted), untrusted, min_chars=400))
        secure = ax_payload(secure=True)
        self.assertFalse(ax_text_sufficient(merged(secure), secure, min_chars=400))
        few_nodes = ax_payload(nodes=2, text=SAFE_PARAGRAPH * 4)
        self.assertFalse(ax_text_sufficient(merged(few_nodes), few_nodes, min_chars=400))
        short = ax_payload(text="Tab")
        self.assertFalse(ax_text_sufficient(merged(short), short, min_chars=400))
        self.assertFalse(ax_text_sufficient(merged(good), good, min_chars=0))
        whitespace = ax_payload(text="a" + " " * 500)
        self.assertFalse(ax_text_sufficient(merged(whitespace), whitespace, min_chars=400))


class AxFirstCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {AX_SKIP_OCR_ENV: "400"})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    def capture(self, images, accessibility, *, frontmost=FRONTMOST, classify=None, retain=True):
        patches = [
            patch("activity_mvp.macos_probe.helper_command", return_value=["sck"]),
            patch("activity_mvp.macos_probe.subprocess.run", return_value=sck_result(images)),
            patch("activity_mvp.macos_probe.compiled_helper_lease", return_value=nullcontext(["/private/vision_ocr"])),
            patch("activity_mvp.macos_probe.submit_ocr", side_effect=ocr_ok),
            patch("activity_mvp.macos_probe._frontmost_context", return_value=frontmost),
            patch("activity_mvp.macos_probe._accessibility_context", return_value=accessibility),
            patch("activity_mvp.macos_probe._browser_url", return_value={"url": "", "domain": "", "source": "test"}),
            patch("activity_mvp.macos_probe._merge_document_signal"),
        ]
        if classify is not None:
            patches.append(patch("activity_mvp.macos_probe.classify_event", side_effect=classify))
        started = [p.start() for p in patches]
        try:
            batch = capture_sck_keyframe_batch(self.root, should_process=lambda _m: True, retain_for_ingest=retain)
        finally:
            for p in reversed(patches):
                p.stop()
        return batch, started[3], started[2]

    def test_main_display_skips_ocr_secondary_still_ocrs_and_pixels_are_not_retained(self) -> None:
        main = private_frame(self.root, "main.png")
        side = private_frame(self.root, "side.png")
        batch, submit, _lease = self.capture([main, side], ax_payload())
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(Path(submit.call_args.args[2]), side)
        by_display = {event["display_id"]: event for event in batch["events"]}
        ax_event = by_display["sck-display-1"]
        self.assertTrue(ax_event["ocr_skipped"])
        self.assertEqual(ax_event["ocr_skip_reason"], OCR_SKIP_REASON_AX)
        self.assertEqual(ax_event["ocr_text"], "")
        self.assertIn("structured signals", ax_event["accessibility_text"])
        self.assertNotIn("_internal_frame_ref", json.dumps(ax_event))
        from activity_mvp.confirmation_evidence import INTERNAL_FRAME_REF
        self.assertNotIn(INTERNAL_FRAME_REF, ax_event)
        self.assertFalse(main.exists(), "un-OCR'd pixels must not be retained for evidence")
        self.assertEqual(by_display["sck-display-2"]["ocr_text"], "ocr line")
        self.assertNotIn("ocr_skipped", by_display["sck-display-2"])
        self.assertEqual(batch["errors"], [])

    def test_ax_only_batch_never_starts_the_ocr_helper(self) -> None:
        main = private_frame(self.root, "main.png")
        batch, submit, lease = self.capture([main], ax_payload())
        submit.assert_not_called()
        lease.assert_not_called()
        self.assertEqual(len(batch["events"]), 1)

    def test_secure_input_blocks_before_ax_shortcut_and_without_ocr(self) -> None:
        main = private_frame(self.root, "main.png")
        batch, submit, _ = self.capture([main], ax_payload(secure=True))
        submit.assert_not_called()
        self.assertTrue(batch["events"][0]["preflight_blocked"])
        self.assertNotIn("ocr_skip_reason", batch["events"][0])
        self.assertFalse(main.exists())

    def test_privacy_firewall_blacklist_blocks_before_ax_shortcut(self) -> None:
        main = private_frame(self.root, "main.png")
        frontmost = {"app_name": "1Password", "bundle_id": "com.1password.1password", "window_title": "Vault"}
        batch, submit, _ = self.capture([main], ax_payload(), frontmost=frontmost)
        submit.assert_not_called()
        self.assertTrue(batch["events"][0]["preflight_blocked"])
        self.assertNotIn("ocr_skip_reason", batch["events"][0])

    def test_firewall_secret_in_ax_text_blocks_and_is_never_skipped_as_clear(self) -> None:
        main = private_frame(self.root, "main.png")
        leaked = ax_payload(text=SAFE_PARAGRAPH + " api_key=" + "sk" + "-live-0123456789abcdefghijklmnop")
        batch, submit, _ = self.capture([main], leaked)
        submit.assert_not_called()
        self.assertTrue(batch["events"][0]["preflight_blocked"])

    def test_review_required_preflight_keeps_the_ocr_path(self) -> None:
        main = private_frame(self.root, "main.png")
        batch, submit, _ = self.capture([main], ax_payload(), classify=lambda _e: SimpleNamespace(status="review_required"))
        self.assertEqual(submit.call_count, 1)
        self.assertNotIn("ocr_skipped", batch["events"][0])

    def test_untrusted_short_or_disabled_ax_falls_back_to_ocr(self) -> None:
        cases = {
            "untrusted": (ax_payload(trusted=False), "400"),
            "short": (ax_payload(text="Toolbar"), "400"),
            "disabled": (ax_payload(), "0"),
        }
        for name, (payload, env) in cases.items():
            with self.subTest(case=name), patch.dict(os.environ, {AX_SKIP_OCR_ENV: env}):
                main = private_frame(self.root, f"{name}.png")
                batch, submit, _ = self.capture([main], payload)
                self.assertEqual(submit.call_count, 1)
                self.assertNotIn("ocr_skipped", batch["events"][0])


class AxFirstDaemonGateTests(unittest.TestCase):
    """The daemon's lease, pause and lock gates run before any AX/OCR probe."""

    def setUp(self) -> None:
        from activity_mvp.daemon import ensure_daemon_defaults
        from activity_mvp.storage import ensure_ready
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "activity.sqlite"
        self.conn = ensure_ready(self.db_path)
        ensure_daemon_defaults(self.conn)
        self.conn.commit()
        self.env = patch.dict(os.environ, {AX_SKIP_OCR_ENV: "400"})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.conn.close()
        self.temp.cleanup()

    def grant(self) -> None:
        from activity_mvp.capture_lease import configure_capture_lease
        configure_capture_lease(self.conn, enabled=True)
        self.conn.commit()

    def forbidden_probes(self):
        return (
            patch("activity_mvp.macos_probe._accessibility_context", side_effect=AssertionError("ax probed")),
            patch("activity_mvp.macos_probe.helper_command", side_effect=AssertionError("screen captured")),
            patch("activity_mvp.macos_probe.submit_ocr", side_effect=AssertionError("ocr ran")),
        )

    def run_blocked(self, **session):
        from activity_mvp.daemon import run_once
        probes = self.forbidden_probes()
        with patch("activity_mvp.daemon.session_state", return_value=session or {"state": "unlocked", "locked": False}), \
                probes[0] as ax, probes[1] as sck, probes[2] as ocr:
            result = run_once(self.db_path)
        ax.assert_not_called(); sck.assert_not_called(); ocr.assert_not_called()
        return result

    def test_without_30_minute_authorization_nothing_is_probed(self) -> None:
        self.assertTrue(self.run_blocked()["status"].startswith("paused"))

    def test_pause_blocks_before_ax_probe(self) -> None:
        from activity_mvp.daemon import pause
        self.grant()
        pause(self.conn, minutes=30)
        self.assertTrue(self.run_blocked()["status"].startswith("paused"))

    def test_lock_screen_blocks_before_ax_probe(self) -> None:
        self.grant()
        result = self.run_blocked(state="locked", locked=True)
        self.assertEqual(result["status"], "locked")

    def test_ax_skipped_frame_is_ingested_counted_and_lock_after_capture_discards(self) -> None:
        from activity_mvp.daemon import _ax_ocr_coverage, run_once
        self.grant()
        main = private_frame(self.root, "main.png")
        common = (
            patch("activity_mvp.macos_probe.helper_command", return_value=["sck"]),
            patch("activity_mvp.macos_probe.subprocess.run", return_value=sck_result([main])),
            patch("activity_mvp.macos_probe.submit_ocr", side_effect=AssertionError("ocr ran")),
            patch("activity_mvp.macos_probe._frontmost_context", return_value=FRONTMOST),
            patch("activity_mvp.macos_probe._accessibility_context", return_value=ax_payload()),
            patch("activity_mvp.macos_probe._browser_url", return_value={"url": "", "domain": "", "source": "test"}),
            patch("activity_mvp.macos_probe._merge_document_signal"),
        )
        unlocked = {"state": "unlocked", "locked": False, "on_console": True}
        with patch("activity_mvp.daemon.session_state", return_value=unlocked), \
                common[0], common[1], common[2], common[3], common[4], common[5], common[6]:
            result = run_once(self.db_path)
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual(result["captured"], 1)
        coverage = _ax_ocr_coverage(self.conn, "2000-01-01T00:00:00+00:00", "2999-01-01T00:00:00+00:00")
        self.assertEqual(coverage["ax_ocr_skipped_count"], 1)
        self.assertEqual(coverage["accessibility_signal_count"], 1)
        self.assertEqual(coverage["ocr_frame_count"], coverage["frame_signal_count"] - 1)
        self.assertEqual(coverage["ax_skip_min_chars"], 400)
        self.assertNotIn("structured signals", json.dumps(coverage))

        second = private_frame(self.root, "second.png")
        states = iter([unlocked, {"state": "locked", "locked": True}])
        last = {"value": unlocked}

        def session(_root):
            last["value"] = next(states, last["value"])
            return last["value"]

        common = (
            patch("activity_mvp.macos_probe.helper_command", return_value=["sck"]),
            patch("activity_mvp.macos_probe.subprocess.run", return_value=sck_result([second])),
            patch("activity_mvp.macos_probe.submit_ocr", side_effect=AssertionError("ocr ran")),
            patch("activity_mvp.macos_probe._frontmost_context", return_value=FRONTMOST),
            patch("activity_mvp.macos_probe._accessibility_context", return_value=ax_payload(text=SAFE_PARAGRAPH + " v2")),
            patch("activity_mvp.macos_probe._browser_url", return_value={"url": "", "domain": "", "source": "test"}),
            patch("activity_mvp.macos_probe._merge_document_signal"),
        )
        with patch("activity_mvp.daemon.session_state", side_effect=session), \
                common[0], common[1], common[2], common[3], common[4], common[5], common[6]:
            result = run_once(self.db_path)
        self.assertEqual(result["status"], "locked_after_capture")
        after = _ax_ocr_coverage(self.conn, "2000-01-01T00:00:00+00:00", "2999-01-01T00:00:00+00:00")
        self.assertEqual(after["ax_ocr_skipped_count"], 1)


if __name__ == "__main__":
    unittest.main()
