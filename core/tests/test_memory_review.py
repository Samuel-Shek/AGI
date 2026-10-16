from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from activity_mvp.memory_review import (
    ACTIONS,
    REVIEW_POLICY_VERSION,
    MemoryReviewError,
    _candidate_fingerprint,
    apply_review_batch,
    memory_review_candidates,
    memory_review_status,
    preview_review_batch,
    read_memory_text,
    read_queue,
)


BODY_A = "memory body a"
BODY_B = "memory body b"
SHA_A = hashlib.sha256(BODY_A.encode()).hexdigest()
SHA_B = hashlib.sha256(BODY_B.encode()).hexdigest()


def _queue_item(
    memory_id: str,
    content_sha256: str,
    *,
    categories: list[str] | None = None,
    related_memory_ids: list[str] | None = None,
) -> dict[str, object]:
    categories = categories or ["expiry_candidates"]
    related_memory_ids = related_memory_ids or []
    return {
        "memory_id": memory_id,
        "content_sha256": content_sha256,
        "categories": categories,
        "reason_codes": ["MEM-AUDIT-TEST-001"],
        "rule_ids": ["MEM-AUDIT-TEST-001"],
        "evidence_types": ["test_fixture"],
        "related_memory_ids": related_memory_ids,
        "candidate_fingerprint": _candidate_fingerprint(
            policy_version=REVIEW_POLICY_VERSION,
            memory_id=memory_id,
            content_sha256=content_sha256,
            categories=categories,
            related_memory_ids=related_memory_ids,
        ),
        "last_seen_at": datetime.now(timezone.utc).isoformat(),
    }


def _queue_payload(
    items: list[dict[str, object]] | None = None,
    *,
    generated_at: datetime | None = None,
) -> dict[str, object]:
    rows = items if items is not None else [
        _queue_item("mem-a", SHA_A),
        _queue_item("mem-b", SHA_B, categories=["noise"]),
    ]
    return {
        "schema_version": 3,
        "policy_version": REVIEW_POLICY_VERSION,
        "owner_id": "local-owner",
        "generated_at": (generated_at or datetime.now(timezone.utc)).isoformat(),
        "audit_watermark": {"record_count": len(rows)},
        "source_snapshot_sha256": "c" * 64,
        "unique_count": len(rows),
        "items": rows,
    }


def _create_chroma(
    path: Path,
    records: list[dict[str, object]] | None = None,
) -> None:
    records = records or [
        {"id": "mem-a", "text": BODY_A, "schema_version": "3"},
        {"id": "mem-b", "text": BODY_B, "schema_version": "3"},
    ]
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE collections(id TEXT PRIMARY KEY,name TEXT);
        CREATE TABLE segments(id TEXT PRIMARY KEY,collection TEXT);
        CREATE TABLE embeddings(
            id INTEGER PRIMARY KEY,segment_id TEXT,embedding_id TEXT,created_at TEXT
        );
        CREATE TABLE embedding_metadata(
            id INTEGER,key TEXT,string_value TEXT,int_value INTEGER,
            float_value REAL,bool_value INTEGER
        );
        INSERT INTO collections VALUES('collection','mem0');
        INSERT INTO segments VALUES('segment','collection');
    """)
    for ordinal, record in enumerate(records, start=1):
        connection.execute(
            "INSERT INTO embeddings VALUES(?,?,?,?)",
            (ordinal, "segment", record["id"], "2026-08-26"),
        )
        metadata = {"data": record.get("text", "")}
        owners = record.get("owners")
        if owners is None:
            owners = {
                "user_id": "local-owner",
                "owner_id": "local-owner",
                "memory_user_id": "local-owner",
            }
        metadata.update(dict(owners))
        if record.get("schema_version") is not None:
            metadata["schema_version"] = record["schema_version"]
        for key, value in metadata.items():
            connection.execute(
                "INSERT INTO embedding_metadata VALUES(?,?,?,?,?,?)",
                (ordinal, key, str(value), None, None, None),
            )
    connection.commit()
    connection.close()


class MemoryReviewQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.queue_path = self.root / "review-queue-v3.json"
        self.ledger_path = self.root / "review-ledger.sqlite3"
        self.chroma_path = self.root / "chroma.sqlite3"
        self._write_queue(_queue_payload())
        _create_chroma(self.chroma_path)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _write_queue(self, payload: dict[str, object]) -> None:
        self.queue_path.write_text(json.dumps(payload), encoding="utf-8")

    def test_queue_requires_fresh_v3_contract_and_owner(self) -> None:
        self._write_queue({**_queue_payload(), "schema_version": 2})
        with self.assertRaises(MemoryReviewError):
            read_queue(self.queue_path)
        self._write_queue({**_queue_payload(), "owner_id": "someone-else"})
        with self.assertRaises(MemoryReviewError):
            read_queue(self.queue_path)
        self._write_queue(_queue_payload(
            generated_at=datetime.now(timezone.utc) - timedelta(hours=73)
        ))
        with self.assertRaisesRegex(MemoryReviewError, "stale"):
            read_queue(self.queue_path)

    def test_queue_rejects_non_hex_sha_and_duplicate_id(self) -> None:
        invalid = _queue_item("mem-a", SHA_A)
        invalid["content_sha256"] = "z" * 64
        self._write_queue(_queue_payload([invalid]))
        with self.assertRaises(MemoryReviewError):
            read_queue(self.queue_path)
        duplicate = _queue_item("mem-a", SHA_A)
        self._write_queue(_queue_payload([duplicate, dict(duplicate)]))
        with self.assertRaises(MemoryReviewError):
            read_queue(self.queue_path)

    def test_missing_or_corrupt_sources_are_explicitly_degraded(self) -> None:
        status = memory_review_status(
            queue_path=self.root / "absent.json", ledger_path=self.ledger_path
        )
        self.assertFalse(status["ok"])
        self.assertTrue(status["retrieval_blocked"])
        self.ledger_path.write_bytes(b"not sqlite")
        status = memory_review_status(
            queue_path=self.queue_path, ledger_path=self.ledger_path
        )
        self.assertFalse(status["ok"])
        self.assertTrue(status["retrieval_blocked"])

    def test_candidates_never_carry_memory_text(self) -> None:
        listing = memory_review_candidates(
            queue_path=self.queue_path, ledger_path=self.ledger_path
        )
        self.assertEqual(listing["contract"], 2)
        self.assertFalse(listing["raw_access"])
        self.assertEqual(listing["total"], 2)
        for item in listing["items"]:
            self.assertNotIn("text", item)
            self.assertEqual(item["review_state"], "pending")
            self.assertFalse(item["retrievable"])

    def test_status_counts_more_than_one_batch_limit(self) -> None:
        items = [
            _queue_item(f"mem-{index:04d}", hashlib.sha256(str(index).encode()).hexdigest())
            for index in range(650)
        ]
        self._write_queue(_queue_payload(items))
        status = memory_review_status(
            queue_path=self.queue_path, ledger_path=self.ledger_path
        )
        self.assertTrue(status["ok"])
        self.assertEqual(status["total"], 650)
        self.assertEqual(status["pending"], 650)


class MemoryRevealOwnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _db(self, name: str, record: dict[str, object]) -> Path:
        path = self.root / name
        _create_chroma(path, [record])
        return path

    def test_schema_v3_requires_all_owner_fields(self) -> None:
        path = self._db("missing.sqlite3", {
            "id": "memory", "text": "body", "schema_version": "3",
            "owners": {"user_id": "local-owner", "owner_id": "local-owner"},
        })
        with self.assertRaisesRegex(MemoryReviewError, "complete owner"):
            read_memory_text("memory", chroma_path=path)

    def test_legacy_can_omit_owner_but_any_conflict_is_rejected(self) -> None:
        ownerless = self._db("ownerless.sqlite3", {
            "id": "memory", "text": "body", "schema_version": None, "owners": {},
        })
        self.assertEqual(read_memory_text("memory", chroma_path=ownerless)["text"], "body")
        conflict = self._db("conflict.sqlite3", {
            "id": "memory", "text": "body", "schema_version": None,
            "owners": {"user_id": "local-owner", "owner_id": "someone-else"},
        })
        with self.assertRaisesRegex(MemoryReviewError, "conflict"):
            read_memory_text("memory", chroma_path=conflict)


class MemoryReviewDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.queue_path = self.root / "review-queue-v3.json"
        self.ledger_path = self.root / "review-ledger.sqlite3"
        self.chroma_path = self.root / "chroma.sqlite3"
        self.queue_path.write_text(json.dumps(_queue_payload()), encoding="utf-8")
        _create_chroma(self.chroma_path)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _apply(self, decisions: list[dict[str, object]]) -> dict[str, object]:
        preview = preview_review_batch(
            decisions, queue_path=self.queue_path, ledger_path=self.ledger_path
        )
        return apply_review_batch(
            decisions,
            confirm_digest=preview["preview_digest"],
            queue_path=self.queue_path,
            ledger_path=self.ledger_path,
            chroma_path=self.chroma_path,
        )

    def test_supersede_is_not_offered_and_outside_queue_is_rejected(self) -> None:
        self.assertNotIn("supersede", ACTIONS)
        with self.assertRaises(MemoryReviewError):
            preview_review_batch(
                [{"memory_id": "mem-a", "action": "supersede"}],
                queue_path=self.queue_path,
                ledger_path=self.ledger_path,
            )
        with self.assertRaises(MemoryReviewError):
            preview_review_batch(
                [{"memory_id": "unknown", "action": "keep_active"}],
                queue_path=self.queue_path,
                ledger_path=self.ledger_path,
            )

    def test_apply_refuses_unseen_digest_without_creating_ledger(self) -> None:
        with self.assertRaises(MemoryReviewError):
            apply_review_batch(
                [{"memory_id": "mem-a", "action": "keep_active"}],
                confirm_digest="0" * 64,
                queue_path=self.queue_path,
                ledger_path=self.ledger_path,
                chroma_path=self.chroma_path,
            )
        self.assertFalse(self.ledger_path.exists())

    def test_live_record_change_returns_record_changed_and_writes_nothing(self) -> None:
        preview = preview_review_batch(
            [{"memory_id": "mem-a", "action": "keep_active"}],
            queue_path=self.queue_path,
            ledger_path=self.ledger_path,
        )
        changed = self.root / "changed.sqlite3"
        _create_chroma(changed, [{
            "id": "mem-a", "text": "changed after preview", "schema_version": "3"
        }])
        with self.assertRaisesRegex(MemoryReviewError, "record_changed:mem-a"):
            apply_review_batch(
                [{"memory_id": "mem-a", "action": "keep_active"}],
                confirm_digest=preview["preview_digest"],
                queue_path=self.queue_path,
                ledger_path=self.ledger_path,
                chroma_path=changed,
            )
        self.assertFalse(self.ledger_path.exists())

    def test_applied_decisions_bind_current_candidate_fingerprint(self) -> None:
        result = self._apply([
            {"memory_id": "mem-a", "action": "keep_active", "note": "still true"},
            {"memory_id": "mem-b", "action": "mark_expired", "note": "old"},
        ])
        self.assertEqual(result["applied_count"], 2)
        connection = sqlite3.connect(self.ledger_path)
        connection.row_factory = sqlite3.Row
        rows = {
            row["memory_id"]: row
            for row in connection.execute("SELECT * FROM lifecycle_overrides")
        }
        batch_rows = connection.execute(
            "SELECT candidate_fingerprint,policy_version FROM batch_items"
        ).fetchall()
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        connection.close()
        expected = _queue_payload()["items"][0]["candidate_fingerprint"]
        self.assertEqual(rows["mem-a"]["candidate_fingerprint"], expected)
        self.assertEqual(rows["mem-a"]["policy_version"], REVIEW_POLICY_VERSION)
        self.assertTrue(all(row[1] == REVIEW_POLICY_VERSION for row in batch_rows))
        self.assertEqual(version, 3)

    def test_second_judgement_is_audited_and_listing_reflects_it(self) -> None:
        self._apply([{"memory_id": "mem-a", "action": "mark_expired"}])
        self._apply([{"memory_id": "mem-a", "action": "keep_active"}])
        connection = sqlite3.connect(self.ledger_path)
        connection.row_factory = sqlite3.Row
        events = connection.execute(
            "SELECT previous_action,action FROM review_events ORDER BY event_id"
        ).fetchall()
        connection.close()
        self.assertEqual(events[1]["previous_action"], "mark_expired")
        listing = memory_review_candidates(
            queue_path=self.queue_path, ledger_path=self.ledger_path
        )
        by_id = {item["memory_id"]: item for item in listing["items"]}
        self.assertTrue(by_id["mem-a"]["retrievable"])
        self.assertEqual(by_id["mem-b"]["review_state"], "pending")

    def test_changed_candidate_relationship_makes_old_keep_active_stale(self) -> None:
        self._apply([{"memory_id": "mem-a", "action": "keep_active"}])
        changed_item = _queue_item("mem-a", SHA_A, related_memory_ids=["mem-b"])
        self.queue_path.write_text(
            json.dumps(_queue_payload([changed_item, _queue_item("mem-b", SHA_B)])),
            encoding="utf-8",
        )
        listing = memory_review_candidates(
            queue_path=self.queue_path, ledger_path=self.ledger_path
        )
        by_id = {item["memory_id"]: item for item in listing["items"]}
        self.assertEqual(by_id["mem-a"]["review_state"], "pending")
        self.assertTrue(by_id["mem-a"]["stale_decision"])
        self.assertFalse(by_id["mem-a"]["retrievable"])


if __name__ == "__main__":
    unittest.main()
