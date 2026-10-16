from __future__ import annotations

import fcntl
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from activity_mvp import confirmation_cards_cli
from activity_mvp.activity_confirmations import _upsert_confirmation_candidate
from activity_mvp.document_changes import AUTHORIZATION_LOCK_FILENAME
from activity_mvp.storage import ensure_ready


class _ExclusiveLeaseHolder:
    """Hold the authorization flock from another thread for a bounded window."""

    def __init__(self, lock_path: Path, hold_seconds: float) -> None:
        self._lock_path = lock_path
        self._hold_seconds = hold_seconds
        self._acquired = threading.Event()
        self._release = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        descriptor = os.open(self._lock_path, os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            self._acquired.set()
            self._release.wait(self._hold_seconds)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def __enter__(self) -> "_ExclusiveLeaseHolder":
        self._thread.start()
        if not self._acquired.wait(5):
            raise AssertionError("exclusive lease was never acquired")
        return self

    def __exit__(self, *_exc: object) -> None:
        self._release.set()
        self._thread.join(timeout=10)


class ConfirmationCardsCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "activity.sqlite"
        conn = ensure_ready(self.db_path)
        conn.close()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _run(self, *args: str) -> dict:
        stdout = io.StringIO()
        argv = ["pam-confirmation-cards", "--db", str(self.db_path), *args]
        with patch.object(sys, "argv", argv), redirect_stdout(stdout):
            confirmation_cards_cli.main()
        return json.loads(stdout.getvalue())

    def _seed_card(
        self,
        item_key: str,
        *,
        question_kind: str = "occurrence",
        object_label: str = "客户公告",
        minute: int = 0,
    ) -> None:
        conn = ensure_ready(self.db_path)
        observed = datetime.now().astimezone().replace(
            hour=9, minute=0, second=0, microsecond=0
        ) + timedelta(minutes=minute)
        _upsert_confirmation_candidate(
            conn,
            {
                "item_key": item_key,
                "event_type": "publish",
                "safe_observation": f"{object_label}已发布",
                "decision_question": "legacy question",
                "why_uncertain": "需要独立证据",
                "app_family": "ChatGPT",
                "observed_start": observed.isoformat(timespec="seconds"),
                "observed_end": (observed + timedelta(minutes=1)).isoformat(
                    timespec="seconds"
                ),
                "confidence": 0.55,
                "reason_codes": ["possible_occurrence", "policy_v5_evaluated"],
                "region_kind": "main_content",
                "actionability": "actionable",
                "source_activity_ids": [],
                "deletion_group_ids": [],
                "fingerprint": f"fp_{item_key}",
                "calibration_key": f"cal_{item_key}",
                "group_key": f"grp_{item_key}",
                "group_signature": f"sig_{item_key}",
                "subject": {
                    "event_label": "发布",
                    "company": "",
                    "round": "",
                    "object": object_label,
                    "label": f"{object_label}发布",
                    "company_identified": False,
                    "round_identified": False,
                    "object_identified": True,
                },
                "evidence": f"{object_label}已发布",
                "cleaned_content": f"{object_label}已发布",
                "question_kind": question_kind,
                "candidate_origin": "explicit_claim",
                "priority": "high",
                "episode_id": "",
                "safe_signal_types": ["post_firewall_activity", "independent_platform_receipt"],
                "suggested_workstream": object_label,
                "suggested_outcome": "completed",
                "notification_eligible": True,
                "state": "pending",
            },
        )
        conn.commit()
        conn.close()

    def test_cards_and_revision_emit_content_free_contracts(self) -> None:
        cards = self._run("cards", "--range", "7d", "--limit", "12")
        revision = self._run("revision", "--range", "7d")
        self.assertEqual(cards["contract_version"], 5)
        self.assertEqual(cards["raw_access"], False)
        self.assertEqual(revision["contract_version"], 5)
        encoded = json.dumps({"cards": cards, "revision": revision}, ensure_ascii=False)
        for forbidden in (
            "cleaned_content",
            "cleaned_contents",
            "source_activity_ids",
            "deletion_group_ids",
            "trigger_sentence",
            "content_sha256",
            "transcript_path",
        ):
            self.assertNotIn(forbidden, encoded)

    def test_cards_section_filter_via_real_argv(self) -> None:
        for index in range(2):
            self._seed_card(
                f"answerable_{index}",
                object_label=f"客户公告{index}",
                minute=index,
            )
        for index in range(3):
            self._seed_card(
                f"insufficient_{index}",
                question_kind="business_context",
                object_label=f"业务片段{index}",
                minute=10 + index,
            )

        unfiltered = self._run("cards", "--range", "today", "--limit", "12")
        answerable = self._run(
            "cards", "--range", "today", "--limit", "12",
            "--section", "pending_confirmation",
        )
        insufficient = self._run(
            "cards", "--range", "today", "--limit", "12",
            "--section", "information_insufficient",
        )

        self.assertEqual(unfiltered["total_card_count"], 5)
        self.assertEqual(len(answerable["cards"]), 2)
        self.assertTrue(
            all(card["section"] == "pending_confirmation" for card in answerable["cards"])
        )
        self.assertEqual(len(insufficient["cards"]), 3)
        self.assertTrue(
            all(
                card["section"] == "information_insufficient"
                for card in insufficient["cards"]
            )
        )
        # Count fields keep their full-range meaning on filtered pages, and the
        # two sections partition the unfiltered queue exactly.
        for response in (answerable, insufficient):
            self.assertEqual(response["total_card_count"], 5)
            self.assertEqual(response["pending_confirmation_count"], 2)
            self.assertEqual(response["information_insufficient_count"], 3)
            self.assertEqual(response["revision"], unfiltered["revision"])
        filtered_keys = {
            card["group_key"] for card in answerable["cards"]
        } | {card["group_key"] for card in insufficient["cards"]}
        self.assertEqual(
            filtered_keys,
            {card["group_key"] for card in unfiltered["cards"]},
        )

    def test_section_cursor_pagination_and_has_more(self) -> None:
        for index in range(15):
            self._seed_card(
                f"page_{index:02d}",
                question_kind="business_context",
                object_label=f"片段{index:02d}",
                minute=index,
            )

        first = self._run(
            "cards", "--range", "today", "--limit", "12",
            "--section", "information_insufficient",
        )
        self.assertEqual(len(first["cards"]), 12)
        self.assertTrue(first["has_more"])
        self.assertEqual(first["has_more"], bool(first["next_cursor"]))
        self.assertTrue(1 <= first["page_size"] <= 12)

        second = self._run(
            "cards", "--range", "today", "--limit", "12",
            "--section", "information_insufficient",
            "--cursor", first["next_cursor"],
        )
        self.assertEqual(len(second["cards"]), 3)
        self.assertFalse(second["has_more"])
        self.assertEqual(second["has_more"], bool(second["next_cursor"]))
        first_keys = {card["group_key"] for card in first["cards"]}
        second_keys = {card["group_key"] for card in second["cards"]}
        self.assertFalse(first_keys & second_keys)

    def test_busy_database_returns_database_busy_within_budget(self) -> None:
        # Guards the 8-second App kill line: contention must surface as a
        # database_busy JSON error well before the fast path would be killed.
        lock_path = self.db_path.parent / AUTHORIZATION_LOCK_FILENAME
        lock_path.touch(mode=0o600)
        os.chmod(lock_path, 0o600)
        stdout = io.StringIO()
        argv = [
            "pam-confirmation-cards",
            "--db",
            str(self.db_path),
            "cards",
            "--range",
            "7d",
        ]
        with _ExclusiveLeaseHolder(lock_path, 30):
            started = time.monotonic()
            with patch.object(sys, "argv", argv), redirect_stdout(stdout):
                with self.assertRaises(SystemExit) as raised:
                    confirmation_cards_cli.safe_main()
            waited = time.monotonic() - started
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {"status": "error", "error": "database_busy", "raw_access": False},
        )
        self.assertLess(waited, 8.0, "busy must return before the App kill line")

    def test_failure_output_does_not_echo_exception_or_path(self) -> None:
        stdout = io.StringIO()
        with patch.object(
            confirmation_cards_cli,
            "main",
            side_effect=RuntimeError("secret /Users/example/private.sqlite"),
        ), redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as raised:
                confirmation_cards_cli.safe_main()
        self.assertEqual(raised.exception.code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(
            payload,
            {"status": "error", "error": "command_failed", "raw_access": False},
        )
        self.assertNotIn("secret", stdout.getvalue())
        self.assertNotIn("/Users", stdout.getvalue())

    def test_missing_database_is_an_error_not_an_empty_queue(self) -> None:
        self.db_path.unlink()
        stdout = io.StringIO()
        argv = [
            "pam-confirmation-cards",
            "--db",
            str(self.db_path),
            "cards",
            "--range",
            "7d",
        ]
        with patch.object(sys, "argv", argv), redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as raised:
                confirmation_cards_cli.safe_main()
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {"status": "error", "error": "database_missing", "raw_access": False},
        )


if __name__ == "__main__":
    unittest.main()
