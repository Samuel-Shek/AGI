from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from activity_mvp import attention_center as center, memory_review as memory
from activity_mvp.activity_confirmations import answer_activity_confirmation_group
from activity_mvp.storage import ensure_ready
from tests import test_memory_review as memories
from tests import test_activity_confirmation_cards_v5 as cards


class AttentionCenterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.conn = ensure_ready(self.root / "activity.sqlite")
        self.base = datetime.now().astimezone().replace(hour=9,minute=0,second=0,microsecond=0)
        self.queue = self.root / "queue.json"
        self.ledger = self.root / "ledger.sqlite"
        self.chroma = self.root / "chroma.sqlite"
        self.queue.write_text(json.dumps(memories._queue_payload([])))
        memories._create_chroma(self.chroma)
        self.paths = {"queue_path":self.queue,"ledger_path":self.ledger}
        self.chroma_patch = patch.object(memory,"DEFAULT_CHROMA_PATH",self.chroma)
        self.chroma_patch.start()
        self.runtime_patch = patch.object(center,"get_quick_status",return_value={"attention_codes":[]})
        self.runtime_patch.start()
        self.hermes_patch = patch.object(center,"hermes_compat_dir",return_value=self.root / "hermes-compat")
        self.hermes_patch.start()

    def tearDown(self):
        self.hermes_patch.stop()
        self.chroma_patch.stop()
        self.runtime_patch.stop()
        self.conn.close()
        self.temp.cleanup()

    def snapshot(self):
        return center.snapshot(self.conn,**self.paths)

    def insert(self,key,**kwargs):
        cards.ActivityConfirmationCardsV5Tests._insert(self,key,**kwargs)

    def evidence(self,identifier,key,error="source_unavailable"):
        now = (self.base-timedelta(days=20)).isoformat()
        self.conn.execute("INSERT OR IGNORE INTO activity_confirmation_evidence_files(evidence_id,source_frame_id,captured_at,state,last_error,created_at,updated_at) VALUES(?,?,?,'unavailable',?,?,?)",(identifier,"fixture",now,error,now,now))
        self.conn.execute("INSERT INTO activity_confirmation_evidence_refs(evidence_id,item_key,created_at) VALUES(?,?,?)",(identifier,key,now))

    def write_memories(self,rows):
        self.queue.write_text(json.dumps(memories._queue_payload(rows)))

    def test_777_old_evidence_gaps_are_not_user_decisions_and_reads_do_not_mutate(self):
        self.insert("old",at=self.base-timedelta(days=20))
        for i in range(777):
            self.evidence(f"ev-{i}","old","privacy_not_clear" if i<427 else "source_unavailable")
        self.conn.commit()
        before = self.conn.total_changes
        result = self.snapshot()
        self.assertEqual(result["status"],"ok")
        self.assertEqual(result["decision_count"],0)
        self.assertEqual(result["system_count"],0)
        gaps = next(r for r in result["resources"] if r["id"]=="evidence-gaps")
        self.assertEqual(gaps["member_count"],777)
        self.assertIn("427",gaps["reason"]); self.assertIn("350",gaps["reason"])
        self.assertEqual(gaps["current_evidence_count"],0)
        history = next(r for r in result["resources"] if r["id"]=="historical-questions")
        self.assertEqual(len(history["children"]),1)
        self.assertEqual(self.conn.total_changes,before)
        self.assertEqual(self.conn.execute("SELECT state,answer FROM activity_confirmation_items").fetchone()[:],("pending",None))

    def test_current_eligible_evidence_is_a_single_system_issue_and_shared_files_deduplicate(self):
        self.insert("current-a",object_label="Atlas 验收公告")
        self.insert("current-b",object_label="Atlas 验收公告",at=self.base+timedelta(minutes=1))
        self.evidence("shared","current-a"); self.evidence("shared","current-b")
        self.conn.commit()
        result=self.snapshot()
        self.assertEqual(result["decision_count"],1)
        self.assertEqual(result["system_count"],1)
        self.assertEqual(result["systems"][0]["id"],"current-evidence-gaps")
        self.assertEqual(center.current_evidence_gaps(self.conn),(1,1))

    def test_insufficient_sources_do_not_become_binary_questions(self):
        self.insert("noise",object_label="8 个条目",evidence="已提交8个条目")
        self.evidence("missing","noise")
        self.conn.commit()
        result=self.snapshot()
        self.assertEqual(result["decision_count"],0)
        self.assertEqual(result["system_count"],0)
        self.assertGreater(result["information_insufficient_count"],0)

    def test_current_evidence_uses_the_complete_current_card_read(self):
        from activity_mvp import activity_confirmations as confirmations
        self.insert("current-a",object_label="Atlas 验收公告")
        self.insert("current-b",object_label="Atlas 验收公告",at=self.base+timedelta(minutes=1))
        self.insert("historical",object_label="Previous 公告",at=self.base-timedelta(days=20))
        self.insert("insufficient",object_label="8 个条目",evidence="已提交8个条目")
        for key in ("current-a", "current-b", "historical", "insufficient"):
            self.evidence("shared" if key.startswith("current-") else key, key)
        self.conn.commit()
        before = self.conn.total_changes
        with (
            patch.object(confirmations, "_live_confirmation_rows", wraps=confirmations._live_confirmation_rows) as reads,
            patch.object(center, "current_evidence_gaps", side_effect=AssertionError("duplicate eligible-group read")),
        ):
            result = self.snapshot()
        self.assertEqual(reads.call_count, 2)  # Current range and retained history.
        gaps = next(row for row in result["resources"] if row["id"] == "evidence-gaps")
        self.assertEqual((gaps["current_evidence_count"], gaps["historical_evidence_count"]), (1, 2))
        current = next(row for row in result["systems"] if row["id"] == "current-evidence-gaps")
        self.assertEqual(current["member_count"], 1)
        self.assertEqual(self.conn.total_changes, before)

    def test_evidence_join_failure_keeps_activity_count_and_marks_evidence_unknown(self):
        self.insert("current",object_label="Atlas 验收公告")
        self.evidence("missing", "current")
        self.conn.commit()
        with patch.object(center, "_evidence_gaps_for_keys", side_effect=sqlite3.OperationalError("busy")):
            result = self.snapshot()
        self.assertEqual(result["activity_question_count"], 1)
        self.assertEqual(result["status"], "partial")
        self.assertTrue(any(row["id"] == "evidence-read-unavailable" for row in result["systems"]))
        self.assertFalse(any(row["id"] == "current-evidence-gaps" for row in result["systems"]))

    def test_evidence_summary_keeps_one_sample_when_a_later_read_repairs_evidence(self):
        self.insert("current",object_label="Atlas 验收公告")
        self.evidence("missing", "current")
        self.conn.commit()
        original = center._cards
        def later_repair(*args, **kwargs):
            result = original(*args, **kwargs)
            if not kwargs.get("historical"):
                self.conn.execute("UPDATE activity_confirmation_evidence_files SET state='available',last_error=''")
                self.conn.commit()
            return result
        with patch.object(center, "_cards", side_effect=later_repair):
            first = self.snapshot()
        gaps = next(row for row in first["resources"] if row["id"] == "evidence-gaps")
        self.assertEqual((gaps["member_count"], gaps["current_evidence_count"], gaps["historical_evidence_count"]), (1, 1, 0))
        second = self.snapshot()
        self.assertFalse(any(row["id"] == "evidence-gaps" for row in second["resources"]))
        self.assertFalse(any(row["id"] == "current-evidence-gaps" for row in second["systems"]))

    def test_evidence_summary_preserves_callers_uncommitted_work_on_success_and_failure(self):
        self.conn.commit()
        for failing in (False, True):
            with self.subTest(failing=failing):
                self.conn.execute("INSERT INTO daemon_state(key,value,updated_at) VALUES('caller-owned','kept','now')")
                self.assertTrue(self.conn.in_transaction)
                if failing:
                    with patch.object(center, "_evidence_gaps_for_keys", side_effect=sqlite3.OperationalError("busy")):
                        with self.assertRaises(sqlite3.OperationalError):
                            center._evidence_summary_for_keys(self.conn, {})
                else:
                    center._evidence_summary_for_keys(self.conn, {})
                self.assertTrue(self.conn.in_transaction)
                self.assertEqual(self.conn.execute("SELECT value FROM daemon_state WHERE key='caller-owned'").fetchone()[0], "kept")
                self.conn.rollback()
                self.assertIsNone(self.conn.execute("SELECT value FROM daemon_state WHERE key='caller-owned'").fetchone())

    def test_light_evidence_total_matches_full_health_with_legacy_and_empty_reasons(self):
        from activity_mvp.confirmation_evidence import evidence_health
        with sqlite3.connect(":memory:") as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("CREATE TABLE activity_confirmation_evidence_files(state TEXT,last_error TEXT,byte_size INTEGER DEFAULT 0)")
            self.assertEqual(center._evidence_summary_for_keys(conn, {})[0], evidence_health(conn)["unavailable_count"])
            conn.executemany("INSERT INTO activity_confirmation_evidence_files(state,last_error) VALUES(?,?)", [
                ("unavailable", None), ("unavailable", "None"), ("unavailable", ""),
                ("available", ""), ("delete_pending", "source_unavailable")])
            total, reasons, affected, missing = center._evidence_summary_for_keys(conn, {})
            self.assertEqual(total, evidence_health(conn)["unavailable_count"])
            self.assertEqual((total, sum(reasons.values()), affected, missing), (3, 3, 0, 0))

    def test_source_read_failure_is_unknown_never_zero(self):
        self.queue.write_text("invalid")
        result=self.snapshot()
        self.assertEqual(result["status"],"partial")
        self.assertIsNone(result["decision_count"])
        self.assertIsNone(result["memory_pending"])
        with patch.object(center,"_cards",side_effect=sqlite3.OperationalError("busy")):
            self.assertIsNone(self.snapshot()["activity_question_count"])

    def test_historical_verification_notice_reaches_home_and_detail_without_erasing_choices(self):
        from activity_mvp import task_closure
        choice = center._issue("closure:choice", "closure", "Personal choice", "Still waiting", requires_decision=True)
        notice = center._issue("closure-evidence-read-unavailable", "system", "Current historical evidence unread", "Verification unknown", current_verification="unknown")
        before = self.conn.total_changes
        with patch.object(task_closure, "projection", return_value=[choice, notice]):
            result = self.snapshot()
            detail = center.detail(self.conn, notice["id"], **self.paths)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["decision_count"], 1)
        self.assertEqual([row["id"] for row in result["decisions"]], [choice["id"]])
        self.assertEqual([row["id"] for row in result["systems"]], [notice["id"]])
        self.assertNotIn(notice, result["closures"])
        self.assertEqual(detail["item"], notice)
        self.assertEqual(self.conn.total_changes, before)

    def test_large_history_keeps_every_unanswered_question_with_one_scan(self):
        from activity_mvp import activity_confirmations as confirmations
        old = self.base - timedelta(days=30)
        for index in range(125):
            self.insert(f"old-{index}", object_label=f"Atlas 公告项目{index}",
                        at=old + timedelta(minutes=index))
        self.conn.execute("UPDATE activity_confirmation_items SET state='deferred' WHERE item_key='old-124'")
        self.conn.commit()
        before = self.conn.total_changes
        with patch.object(confirmations, "_live_confirmation_rows", wraps=confirmations._live_confirmation_rows) as reads:
            result, complete = center._cards(self.conn, historical=True)
        self.assertEqual(reads.call_count, 1)
        self.assertEqual(len(complete), 125)
        self.assertEqual(result["pending_question_count"], 125)
        self.assertFalse(result["has_more"])
        paginated, cursor = [], None
        while True:
            page = confirmations.list_activity_confirmation_cards(
                self.conn, range_name="7d", start="1970-01-01T00:00:00+00:00",
                end=center.resolve_confirmation_range("7d")[0],
                cursor=cursor, section="pending_confirmation")
            paginated.extend(page["cards"])
            cursor = page["next_cursor"]
            if not cursor:
                break
        self.assertEqual(complete, paginated)
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM activity_confirmation_items WHERE answer IS NULL").fetchone()[0], 125)

    def test_subjects_stay_separate_and_metadata_contains_no_body(self):
        rows=[memories._queue_item("mem-a",memories.SHA_A),memories._queue_item("mem-b",memories.SHA_B)]
        for row in rows: row["reason_codes"]=["MEM-AUDIT-SUBJECT-001"]
        self.write_memories(rows)
        result=self.snapshot()
        self.assertEqual(result["decision_count"],2)
        self.assertNotIn(memories.BODY_A,json.dumps(result))
        self.assertNotIn(memories.BODY_B,json.dumps(result))

    def test_batch_checks_members_scope_action_and_source_revision(self):
        self.write_memories([memories._queue_item("mem-a",memories.SHA_A),memories._queue_item("mem-b",memories.SHA_B)])
        issue=self.snapshot()["decisions"][0]
        payload={"issue_id":issue["id"],"revision":issue["revision"],"decisions":[{"memory_id":"mem-a","action":"mark_legacy"},{"memory_id":"mem-b","action":"mark_legacy"}]}
        with self.assertRaisesRegex(memory.MemoryReviewError,"适用范围"):
            center.preview_memory_group(self.conn,payload,**self.paths)
        with patch.object(memory,"read_memory_text",side_effect=[{"applies_to":"one"},{"applies_to":"two"}]):
            with self.assertRaisesRegex(memory.MemoryReviewError,"适用范围"):
                center.preview_memory_group(self.conn,payload,**self.paths)
        single={**payload,"decisions":payload["decisions"][:1]}
        preview=center.preview_memory_group(self.conn,single,**self.paths)
        self.assertIn("preview_digest",preview)
        self.assertFalse(self.ledger.exists())
        changed={**single,"revision":"old"}
        with self.assertRaisesRegex(memory.MemoryReviewError,"事项已变化"):
            center.preview_memory_group(self.conn,changed,**self.paths)
        wrong={**single,"decisions":[{"memory_id":"not-member","action":"mark_legacy"}]}
        with self.assertRaises(memory.MemoryReviewError): center.preview_memory_group(self.conn,wrong,**self.paths)
        self.write_memories([memories._queue_item("mem-a",memories.SHA_A)])
        with self.assertRaisesRegex(memory.MemoryReviewError,"事项已变化"):
            center.preview_memory_group(self.conn,single,**self.paths)

    def test_defer_stays_open_explicit_apply_is_idempotent_and_result_recheck_uses_ledger(self):
        self.write_memories([memories._queue_item("mem-a",memories.SHA_A)])
        issue=self.snapshot()["decisions"][0]
        decisions=[{"memory_id":"mem-a","action":"review_later"}]
        preview=memory.preview_review_batch(decisions,**self.paths)
        memory.apply_review_batch(decisions,confirm_digest=preview["preview_digest"],**self.paths)
        self.assertEqual(self.snapshot()["memory_pending"],1)
        self.assertEqual(center.verify_resolution(self.conn,issue,**self.paths)["state"],"pending")
        decisions=[{"memory_id":"mem-a","action":"mark_legacy"}]
        preview=memory.preview_review_batch(decisions,**self.paths)
        result=memory.apply_review_batch(decisions,confirm_digest=preview["preview_digest"],**self.paths)
        again=memory.apply_review_batch(decisions,confirm_digest=preview["preview_digest"],**self.paths)
        self.assertEqual(again["applied_count"],0)
        self.assertEqual(result["applied_count"],1)
        self.assertEqual(center.verify_resolution(self.conn,issue,**self.paths)["state"],"resolved")
        self.assertEqual(self.snapshot()["decision_count"],0)

    def test_activity_choice_accepts_explicit_work_context_and_defer_is_unresolved(self):
        self.insert("sample",object_label="Atlas 验收公告"); self.conn.commit()
        issue=self.snapshot()["decisions"][0]
        result=answer_activity_confirmation_group(self.conn,group_key=issue["group_key"],member_revision=issue["revision"],answer="defer")
        self.assertNotEqual(result.get("status"),"blocked")
        self.assertEqual(center.verify_resolution(self.conn,issue,**self.paths)["state"],"pending")
        current=self.snapshot()["decisions"][0]
        result=answer_activity_confirmation_group(self.conn,group_key=current["group_key"],member_revision=current["revision"],answer="not_occurred",workstream="Atlas 验收",outcome="unknown")
        self.assertNotEqual(result.get("status"),"blocked")
        self.assertEqual(center.verify_resolution(self.conn,issue,**self.paths)["state"],"resolved")



class HermesCompatAttentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.now = datetime.now().astimezone().replace(microsecond=0)

    def tearDown(self):
        self.temp.cleanup()

    def status(self,state,*,checked=None,findings=(),note="",**extra):
        checked = checked or self.now-timedelta(minutes=10)
        (self.root/"status.json").write_text(json.dumps({"schema":1,"state":state,"checked_at":checked.isoformat(),
            "findings":list(findings),"note":note,"problem_since":extra.get("since")},ensure_ascii=False))

    def history(self,*rows):
        (self.root/"history.jsonl").write_text("".join(json.dumps({"checked_at":at.isoformat(),"state":state,"note":note},ensure_ascii=False)+"\n" for at,state,note in rows))

    def issues(self):
        return center._hermes_compat_issues(self.root,now=self.now)

    def test_missing_watcher_adds_nothing(self):
        self.assertEqual(center._hermes_compat_issues(self.root/"absent",now=self.now),([],[]))

    def test_ok_without_recent_fix_adds_nothing(self):
        self.status("ok")
        self.history((self.now-timedelta(hours=30),"auto_fixed","旧修复"),(self.now-timedelta(minutes=10),"ok",""))
        self.assertEqual(self.issues(),([],[]))

    def test_problem_states_become_system_issues_with_unconfirmed_changes(self):
        for state in ("needs_review","fix_failed","check_error"):
            with self.subTest(state=state):
                self.status(state,findings=[{"area":"a","detail":"切了模型","auto":True},{"area":"b","detail":"新增插件 x","auto":False}],
                    note="没有替你认账。",since="2026-10-05T08:00:00+08:00")
                systems,resources = self.issues()
                self.assertEqual([i["id"] for i in systems],["hermes-compat-"+state])
                self.assertEqual(resources,[])
                issue = systems[0]
                self.assertEqual(issue["kind"],"system")
                self.assertIn("新增插件 x",issue["reason"])
                self.assertNotIn("切了模型",issue["reason"])
                self.assertEqual(issue["revision"],"2026-10-05T08:00:00+08:00")
                self.assertIn("告诉 AI 这些变化是你批准的",issue["recommendation"])

    def test_recent_auto_fix_is_a_reference_for_24_hours(self):
        self.status("ok")
        fixed = self.now-timedelta(hours=2)
        self.history((self.now-timedelta(hours=30),"auto_fixed","太旧"),(fixed,"auto_fixed","已重新验证"),(self.now-timedelta(hours=1),"ok",""))
        systems,resources = self.issues()
        self.assertEqual(systems,[])
        self.assertEqual([i["id"] for i in resources],["hermes-compat-auto-fixed"])
        self.assertEqual(resources[0]["member_count"],1)
        self.assertEqual(resources[0]["reason"],"已重新验证")
        self.assertEqual(resources[0]["revision"],fixed.isoformat())

    def test_current_auto_fix_counts_once_with_history(self):
        checked = self.now-timedelta(minutes=5)
        self.status("auto_fixed",checked=checked,note="回执已更新")
        self.history((checked,"auto_fixed","回执已更新"))
        self.assertEqual(self.issues()[1][0]["member_count"],1)
        (self.root/"history.jsonl").unlink()
        self.assertEqual(self.issues()[1][0]["member_count"],1)

    def test_stale_watcher_and_unreadable_status_are_reported(self):
        self.status("ok",checked=self.now-timedelta(hours=5))
        self.assertEqual([i["id"] for i in self.issues()[0]],["hermes-compat-stale"])
        for text in ("{broken",json.dumps({"schema":2,"state":"ok","checked_at":self.now.isoformat()}),
                     json.dumps({"schema":1,"state":"surprise","checked_at":self.now.isoformat()}),
                     json.dumps({"schema":1,"state":"ok","checked_at":"2026-10-05T08:00:00"})):
            with self.subTest(text=text):
                (self.root/"status.json").write_text(text)
                self.assertEqual([i["id"] for i in self.issues()[0]],["hermes-compat-unreadable"])

    def test_snapshot_lists_hermes_issues_without_changing_agi_completeness(self):
        self.status("needs_review",findings=[{"area":"b","detail":"新增插件 x","auto":False}])
        temp = Path(self.temp.name)/"agi"; temp.mkdir()
        conn = ensure_ready(temp/"activity.sqlite")
        queue = temp/"queue.json"; queue.write_text(json.dumps(memories._queue_payload([])))
        chroma = temp/"chroma.sqlite"; memories._create_chroma(chroma)
        try:
            with patch.object(memory,"DEFAULT_CHROMA_PATH",chroma), \
                 patch.object(center,"get_quick_status",return_value={"attention_codes":[]}), \
                 patch.object(center,"hermes_compat_dir",return_value=self.root):
                state = center.snapshot(conn,queue_path=queue,ledger_path=temp/"ledger.sqlite")
                self.assertEqual(state["status"],"ok")
                self.assertIn("hermes-compat-needs_review",[i["id"] for i in state["systems"]])
                self.assertEqual(center.detail(conn,"hermes-compat-needs_review",queue_path=queue,ledger_path=temp/"ledger.sqlite")["item"]["title"],"Hermes 兼容检查：需要你看一下")
        finally:
            conn.close()


if __name__ == "__main__": unittest.main()
