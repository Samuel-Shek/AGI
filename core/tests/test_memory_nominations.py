from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from activity_mvp.memory_nominations import detect_nominator, nominate
from activity_mvp.memory_review import (
    MemoryReviewError,
    apply_review_batch,
    memory_review_candidates,
    memory_review_status,
    preview_review_batch,
)
from tests.test_memory_review import SHA_A, _create_chroma, _queue_item, _queue_payload


OLD = "old keyboard memory"
NEW = "new keyboard memory"
SHA_OLD = hashlib.sha256(OLD.encode()).hexdigest()
CLAUDE_CODE = "/opt/vendor/claude-code/2.1/claude.app/Contents/MacOS/claude"
CLAUDE_DESKTOP = "/Applications/Claude.app/Contents/MacOS/Claude"
CODEX = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
IDENTITY = {"platform": "claude", "agent": "claude-code", "method": "test"}


def _process_table(rows: dict[int, tuple[int, str]], labels: dict[int, str] | None = None):
    return {
        "executable_path": lambda pid: rows.get(pid, (0, ""))[1],
        "parent_pid": lambda pid: rows.get(pid, (0, ""))[0],
        "launchd_labels": lambda: labels or {},
    }


class NominatorIdentityTests(unittest.TestCase):
    def test_nearest_trusted_host_names_the_nominator(self) -> None:
        table = _process_table({
            50: (40, "/bin/zsh"), 40: (30, CLAUDE_CODE), 30: (20, CLAUDE_DESKTOP), 20: (1, "/sbin/x"),
        })
        identity = detect_nominator(start_pid=50, **table)
        self.assertEqual((identity["platform"], identity["agent"]), ("claude", "claude-code"))
        self.assertEqual(identity["method"], "process_ancestry")

    def test_unknown_or_nested_hosts_are_refused(self) -> None:
        with self.assertRaisesRegex(MemoryReviewError, "nominator_unrecognized"):
            detect_nominator(start_pid=50, **_process_table({50: (1, "/bin/zsh")}))
        nested = _process_table({50: (40, CODEX), 40: (30, "/bin/zsh"), 30: (1, CLAUDE_CODE)})
        with self.assertRaisesRegex(MemoryReviewError, "nominator_ambiguous"):
            detect_nominator(start_pid=50, **nested)

    def test_hermes_needs_its_registered_gateway_job(self) -> None:
        python = "/Library/Frameworks/Python.framework/Versions/3.12/Resources/Python.app/Contents/MacOS/Python"
        rows = {50: (40, "/bin/sh"), 40: (1, python)}
        identity = detect_nominator(
            start_pid=50, **_process_table(rows, {40: "ai.hermes.gateway-feishubot"})
        )
        self.assertEqual(identity["agent"], "hermes:feishubot")
        with self.assertRaisesRegex(MemoryReviewError, "nominator_unrecognized"):
            detect_nominator(start_pid=50, **_process_table(rows))


class MemoryNominationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.queue_path = self.root / "review-queue-v3.json"
        self.ledger_path = self.root / "review-ledger.sqlite3"
        self.chroma_path = self.root / "chroma.sqlite3"
        self.nominations_path = self.root / "nominations.sqlite3"
        self._write_queue(_queue_payload())
        _create_chroma(self.chroma_path, [
            {"id": "mem-a", "text": "memory body a", "schema_version": "3"},
            {"id": "mem-old", "text": OLD, "schema_version": "3"},
            {"id": "mem-new", "text": NEW, "schema_version": "3"},
        ])

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _write_queue(self, payload: dict[str, object]) -> None:
        self.queue_path.write_text(json.dumps(payload), encoding="utf-8")

    def _paths(self) -> dict[str, Path]:
        return {"queue_path": self.queue_path, "ledger_path": self.ledger_path}

    def _nominate(self, memory_id: str = "mem-old", **overrides):
        options = {
            "action": "mark_legacy",
            "note": "replaced by a newer keyboard record",
            "superseded_by": ["mem-new"],
            "nominator": IDENTITY,
            "nominations_path": self.nominations_path,
            "chroma_path": self.chroma_path,
            **self._paths(),
        }
        options.update(overrides)
        return nominate(memory_id, **options)

    def _review_options(self) -> dict[str, object]:
        return {
            **self._paths(),
            "include_nominations": True,
            "nominations_path": self.nominations_path,
            "chroma_path": self.chroma_path,
        }

    def _set_text(self, memory_id: str, text: str) -> None:
        connection = sqlite3.connect(self.chroma_path)
        connection.execute(
            "UPDATE embedding_metadata SET string_value = ? WHERE key = 'data' AND id = "
            "(SELECT id FROM embeddings WHERE embedding_id = ?)",
            (text, memory_id),
        )
        connection.commit()
        connection.close()

    def test_nomination_changes_neither_queue_nor_ledger(self) -> None:
        queue_bytes = self.queue_path.read_bytes()
        result = self._nominate()
        self.assertTrue(result["nominated"])
        self.assertFalse(result["retrieval_changed"])
        self.assertEqual(result["nominator"]["agent"], "claude-code")
        self.assertEqual(self.queue_path.read_bytes(), queue_bytes)
        self.assertFalse(self.ledger_path.exists())
        self.assertEqual(self.nominations_path.stat().st_mode & 0o777, 0o600)

    def test_default_listing_hides_nominations_from_automation(self) -> None:
        self._nominate()
        default = memory_review_candidates(**self._paths())
        self.assertNotIn("mem-old", {item["memory_id"] for item in default["items"]})
        listed = memory_review_candidates(
            **self._paths(), include_nominations=True, nominations_path=self.nominations_path
        )
        row = next(item for item in listed["items"] if item["memory_id"] == "mem-old")
        self.assertEqual(row["source"], "ai_nomination")
        self.assertEqual(row["review_state"], "pending")
        self.assertEqual(row["nomination"]["evidence"][0]["memory_id"], "mem-new")
        self.assertNotIn(OLD, json.dumps(listed))
        status = memory_review_status(**self._paths(), nominations_path=self.nominations_path)
        self.assertEqual((status["total"], status["nominations_pending"]), (2, 1))

    def test_preview_and_apply_reach_the_ledger_with_the_replacement(self) -> None:
        self._nominate()
        decisions = [{"memory_id": "mem-old", "action": "mark_legacy"}]
        with self.assertRaisesRegex(MemoryReviewError, "not in the current queue"):
            preview_review_batch(decisions, **self._paths())
        preview = preview_review_batch(decisions, **self._review_options())
        self.assertEqual(preview["changes"][0]["nominated_by"], "claude-code")
        self.assertEqual(preview["changes"][0]["superseding_memory_id"], "mem-new")
        result = apply_review_batch(
            decisions, confirm_digest=preview["preview_digest"], **self._review_options()
        )
        self.assertEqual(result["applied_count"], 1)
        connection = sqlite3.connect(self.ledger_path)
        override = connection.execute(
            "SELECT lifecycle_status, content_sha256 FROM lifecycle_overrides WHERE memory_id='mem-old'"
        ).fetchone()
        item = connection.execute(
            "SELECT superseding_memory_id, note FROM batch_items WHERE memory_id='mem-old'"
        ).fetchone()
        connection.close()
        self.assertEqual(override, ("legacy", SHA_OLD))
        self.assertEqual(item[0], "mem-new")
        self.assertIn("claude-code", item[1])
        listed = memory_review_candidates(
            **self._paths(), include_nominations=True, nominations_path=self.nominations_path
        )
        row = next(entry for entry in listed["items"] if entry["memory_id"] == "mem-old")
        self.assertEqual(row["review_state"], "decided")

    def test_changed_target_or_evidence_voids_the_nomination(self) -> None:
        self._nominate()
        decisions = [{"memory_id": "mem-old", "action": "mark_legacy"}]
        preview = preview_review_batch(decisions, **self._review_options())
        self._set_text("mem-new", "edited replacement")
        with self.assertRaisesRegex(MemoryReviewError, "evidence_changed:mem-new"):
            apply_review_batch(decisions, confirm_digest=preview["preview_digest"], **self._review_options())
        self._set_text("mem-new", NEW)
        self._set_text("mem-old", "edited target")
        with self.assertRaisesRegex(MemoryReviewError, "record_changed:mem-old"):
            preview_review_batch(decisions, **self._review_options())
        self.assertFalse(self.ledger_path.exists())

    def test_replacement_must_be_live_and_evidence_is_required(self) -> None:
        self._write_queue(_queue_payload([_queue_item("mem-new", hashlib.sha256(NEW.encode()).hexdigest())]))
        with self.assertRaisesRegex(MemoryReviewError, "still pending review"):
            self._nominate()
        with self.assertRaisesRegex(MemoryReviewError, "at least one piece of evidence"):
            self._nominate(superseded_by=[])
        with self.assertRaisesRegex(MemoryReviewError, "credential"):
            self._nominate(superseded_by=[], evidence_text=["token=abc123"])
        self.assertFalse(self.nominations_path.exists())

    def test_one_nomination_per_version_and_records_are_append_only(self) -> None:
        self._nominate()
        with self.assertRaisesRegex(MemoryReviewError, "already nominated"):
            self._nominate()
        connection = sqlite3.connect(self.nominations_path)
        for statement in ("UPDATE nominations SET action='keep_active'", "DELETE FROM nominations"):
            with self.assertRaisesRegex(sqlite3.DatabaseError, "append_only"):
                connection.execute(statement)
        connection.close()

    def test_daily_queue_regeneration_keeps_the_nomination_reviewable(self) -> None:
        self._nominate()
        self._write_queue(_queue_payload(
            [_queue_item("mem-a", SHA_A, categories=["noise"])],
            generated_at=datetime.now(timezone.utc),
        ))
        decisions = [{"memory_id": "mem-old", "action": "mark_legacy"}]
        self.assertEqual(preview_review_batch(decisions, **self._review_options())["item_count"], 1)

    def test_audit_candidate_keeps_its_own_fingerprint_when_also_nominated(self) -> None:
        self._nominate()
        queued = _queue_item("mem-old", SHA_OLD)
        self._write_queue(_queue_payload([queued]))
        listed = memory_review_candidates(
            **self._paths(), include_nominations=True, nominations_path=self.nominations_path
        )
        self.assertEqual(listed["total"], 1)
        row = listed["items"][0]
        self.assertEqual(row["candidate_fingerprint"], queued["candidate_fingerprint"])
        self.assertEqual(row["nomination"]["suggested_action"], "mark_legacy")


if __name__ == "__main__":
    unittest.main()
