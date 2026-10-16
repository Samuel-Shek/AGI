from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from activity_mvp import attention_center as center, attention_investigation as investigation
from activity_mvp import attention_handoff as handoff, memory_review as memory
from tests import test_attention_center as center_tests
from tests import test_memory_review as fixtures


class InvestigationTests(unittest.TestCase):
    snapshot = center_tests.AttentionCenterTests.snapshot
    write_memories = center_tests.AttentionCenterTests.write_memories
    tearDown = center_tests.AttentionCenterTests.tearDown

    def setUp(self):
        center_tests.AttentionCenterTests.setUp(self)
        row = fixtures._queue_item("mem-a", fixtures.SHA_A)
        row["reason_codes"] = ["MEM-AUDIT-SUBJECT-001", "MEM-AUDIT-TEMPORAL-001"]
        self.write_memories([row])
        self.database = self.root / "activity.sqlite"
        self.note_root = investigation.root_for(self.database)
        self.note_root.mkdir(mode=0o700)
        self.vault, self.archive = self.root / "vault", self.root / "mail"
        self.vault.mkdir(); self.archive.mkdir()
        self.maildb = self.root / "mail.sqlite"
        with sqlite3.connect(self.maildb) as db:
            db.execute("CREATE TABLE messages(subject,received_at,direction,archive_path,archive_state)")
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?)", ("市场经理岗位申请回复", "2026-09-07T01:00:00Z", "incoming", str(self.archive), "archived"))
        self.source = self.archive / "body.md"
        self.source.write_text("批准新岗位，先进入试用期。PRIVATE ORIGINAL BODY")
        self.page = self.vault / "20260907-市场经理岗位调整.md"
        self.page.write_text("PRIVATE VAULT BODY")
        self.config = self.note_root / "sources.json"
        self.config.write_text(json.dumps({"schema_version": 1, "owner_id": memory.OWNER_ID,
            "memory_root": str(self.vault), "email_archive_root": str(self.archive), "email_database": str(self.maildb)}))
        self.config.chmod(0o600)

    def issue(self):
        return self.snapshot()["decisions"][0]

    def payload(self, issue):
        return {"revision": issue["revision"], "authorized_memory_id": "mem-a",
                "finding": "已核对岗位批准依据；试用期安排不否定批准。旧岗位仅作历史，审核仍待选择。",
                "sources": [{"path": str(self.source), "sha256": hashlib.sha256(self.source.read_bytes()).hexdigest()}]}

    def record(self):
        issue = self.issue()
        return investigation.record(self.database, issue, self.payload(issue))

    def test_metadata_search_finds_vault_and_mail_without_opening_bodies(self):
        original = Path.open
        def guarded(path, *args, **kwargs):
            if path in (self.page, self.source):
                raise AssertionError("source search read a body")
            return original(path, *args, **kwargs)
        with patch.object(Path, "open", guarded), patch.object(memory, "read_memory_text", side_effect=AssertionError("memory reveal")):
            result = investigation.search_sources(self.database, ["市场经理"])
        self.assertEqual({i["kind"] for i in result["items"]}, {"business_index", "mail_index"})
        self.assertIn("沿用已有有效任务授权", result["boundary"])
        self.assertIn("具体限制", result["boundary"])
        self.assertFalse(result["fact_verified"])
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertEqual(investigation.search_sources(self.database, ["%_' OR 1=1"])['items'], [])

    def test_real_command_records_and_rereads_but_never_changes_lifecycle(self):
        issue = self.issue(); payload = self.payload(issue)
        before = memory.memory_review_candidates(**self.paths)
        output = io.StringIO()
        with patch.object(memory, "DEFAULT_QUEUE_PATH", self.queue), patch.object(memory, "DEFAULT_LEDGER_PATH", self.ledger), patch("sys.stdin", io.StringIO(json.dumps(payload))), contextlib.redirect_stdout(output):
            center.main(["--db", str(self.database), "investigation-record", "--id", issue["id"]])
        self.assertEqual(json.loads(output.getvalue())["status"], "evidence_recorded")
        self.assertEqual(memory.memory_review_candidates(**self.paths)["items"], before["items"])
        with patch.object(memory, "read_memory_text", side_effect=AssertionError("refresh revealed body")), patch.object(Path, "read_bytes", side_effect=AssertionError("refresh read source bytes")):
            latest = self.issue()
        self.assertEqual(latest["investigation_state"], "recorded")
        self.assertIn(payload["finding"], latest["background"])
        self.assertEqual(latest["members"], issue["members"])
        self.assertNotEqual(latest["revision"], issue["revision"])
        self.assertEqual(center.verify_resolution(self.conn, issue, **self.paths)["state"], "pending")
        self.assertNotIn("PRIVATE", json.dumps(latest))

    def test_changed_or_deleted_source_preserves_dated_finding_not_current_authority(self):
        self.record()
        self.source.write_text("changed")
        self.assertEqual(self.issue()["investigation_state"], "needs_recheck")
        self.assertNotIn("试用期安排不否定批准", self.issue()["background"])
        self.assertIn("历史核查", self.issue()["attempts"])
        self.assertIn("来源版本已变化", self.issue()["attempts"])
        self.assertIn("试用期安排不否定批准", self.issue()["attempts"])
        self.source.unlink()
        self.assertEqual(self.issue()["investigation_state"], "needs_recheck")
        self.assertIn("来源暂不可用", self.issue()["attempts"])

    def test_elapsed_review_window_retains_history_without_reads_release_or_forwarding(self):
        import uuid
        self.record(); issue = self.issue()
        path = investigation._note_path(self.database, issue["id"])
        data = json.loads(path.read_text())
        recorded = datetime.now(timezone.utc) - timedelta(days=4)
        data["recorded_at"] = recorded.isoformat()
        data["valid_until"] = (recorded + timedelta(days=3)).isoformat()
        path.write_text(json.dumps(data))
        with patch.object(memory, "read_memory_text", side_effect=AssertionError("refresh revealed body")), patch.object(Path, "read_bytes", side_effect=AssertionError("refresh read source bytes")):
            latest = self.issue()
        self.assertEqual(latest["investigation_state"], "needs_recheck")
        self.assertIn("历史核查", latest["attempts"])
        self.assertIn("已到复查时间", latest["attempts"])
        self.assertIn("不代表当前已重新核验", latest["attempts"])
        self.assertIn(data["finding"], latest["attempts"])
        self.assertNotIn(data["finding"], latest["background"])
        self.assertFalse(memory.memory_review_candidates(**self.paths)["items"][0]["retrievable"])
        self.assertEqual(center.verify_resolution(self.conn, latest, **self.paths)["state"], "pending")
        queue = handoff.Queue(self.database)
        with patch.object(handoff, "detail", return_value={"item": latest}):
            queue.submit(latest["id"], {"client_message_id": str(uuid.uuid4()), "content": ""})
        with queue.db() as db:
            prompt = db.execute("SELECT prompt FROM submissions").fetchone()[0]
        self.assertNotIn(data["finding"], prompt)
        self.assertNotIn(str(self.source), prompt)

    def test_receipt_expiry_and_wrong_candidate_do_not_release_or_show_finding(self):
        self.record(); issue = self.issue()
        path = investigation._note_path(self.database, issue["id"])
        data = json.loads(path.read_text()); data["valid_until"] = "2020-01-01T00:00:00+00:00"
        path.write_text(json.dumps(data))
        self.assertEqual(self.issue()["investigation_state"], "needs_recheck")
        self.record()
        data = json.loads(path.read_text()); data["candidate_fingerprint"] = "0" * 64
        path.write_text(json.dumps(data))
        self.assertEqual(self.issue()["investigation_state"], "needs_recheck")
        self.assertFalse(memory.memory_review_candidates(**self.paths)["items"][0]["retrievable"])

    def test_named_authorization_and_exact_source_version_are_required(self):
        issue = self.issue(); payload = self.payload(issue)
        payload["authorized_memory_id"] = "mem-b"
        with self.assertRaises(ValueError): investigation.record(self.database, issue, payload)
        payload = self.payload(issue); payload["sources"][0]["sha256"] = "0" * 64
        with self.assertRaises(ValueError): investigation.record(self.database, issue, payload)
        payload = self.payload(issue); payload["sources"][0]["path"] = str(self.config)
        with self.assertRaises(ValueError): investigation.record(self.database, issue, payload)

    def test_memory_change_before_next_audit_invalidates_note(self):
        self.record()
        with sqlite3.connect(self.chroma) as db:
            db.execute("CREATE TABLE changed_elsewhere(value)")
        self.assertEqual(self.issue()["investigation_state"], "needs_recheck")

    def test_permissions_symlinks_and_wrong_owner_fail_closed(self):
        self.config.chmod(0o644)
        with self.assertRaises(ValueError): investigation.search_sources(self.database, ["市场经理"])
        self.config.chmod(0o600)
        data = json.loads(self.config.read_text()); data["owner_id"] = "someone-else"
        self.config.write_text(json.dumps(data))
        with self.assertRaises(ValueError): investigation.search_sources(self.database, ["市场经理"])

    def test_local_finding_is_not_forwarded_on_later_handoff(self):
        import uuid
        self.record(); issue = self.issue()
        queue = handoff.Queue(self.database)
        with patch.object(handoff, "detail", return_value={"item": issue}):
            queue.submit(issue["id"], {"client_message_id": str(uuid.uuid4()), "content": ""})
        with queue.db() as db:
            prompt = db.execute("SELECT prompt FROM submissions").fetchone()[0]
        self.assertNotIn("试用期安排不否定批准", prompt)
        self.assertNotIn(str(self.source), prompt)
        self.assertNotIn("mem-a", prompt)
        self.assertIn("source-search", prompt)
        self.assertIn("已有有效授权则继续", prompt)

    def test_audit_risk_is_explained_as_missing_fields_not_wrong_person(self):
        issue = self.issue()
        self.assertIn("字段缺失不等于", issue["reason"])
        self.assertIn("尚未核查", issue["attempts"])
        self.assertNotIn("已核对当前数据与来源状态", issue["attempts"])


if __name__ == "__main__":
    unittest.main()
