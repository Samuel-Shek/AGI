"""Synthetic native transcripts only; no production conversations or network."""
from tests.closure_legacy_fixture import legacy_begin
import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from activity_mvp.task_closure import Store, safe_authority


class CompletedExternalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.projects = self.root / "projects"
        self.session = "00000000-0000-0000-0000-000000000001"
        self.transcript = self.projects / "synthetic-project" / (self.session + ".jsonl")
        self.transcript.parent.mkdir(parents=True)
        self.source = self.root / "published.txt"
        self.source.write_text("synthetic published content")
        self.prompt = "Publish the agreed synthetic artifact to the existing test repository."
        self.output = "Read back synthetic artifact and version: verified."
        self.rows = [
            {"uuid": "user-1", "type": "user", "message": {"role": "user", "content": self.prompt},
             "parentUuid": None},
            {"uuid": "assistant-1", "type": "assistant", "parentUuid": "user-1",
             "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "tool-1",
                 "name": "Bash", "input": {"command": "synthetic-publish-and-readback"}}]}},
            {"uuid": "result-1", "type": "user", "parentUuid": "assistant-1",
             "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool-1",
                 "is_error": False, "content": self.output}]}}
        ]
        for row in self.rows:
            row.update(sessionId=self.session, isSidechain=False, userType="external")
        self.write_transcript()
        self.provider = patch("activity_mvp.task_closure.CLAUDE_PROJECTS", self.projects)
        self.provider.start(); self.addCleanup(self.provider.stop)
        self.store = Store(self.root / "activity.sqlite")
        self.task = legacy_begin(self.store, {"platform": "claude", "session": self.session,
            "turn": hashlib.sha256(self.prompt.encode()).hexdigest(), "objective": "synthetic publish",
            "scope_root": str(self.root), "complex": False, "closure_mode": "full"})["task_id"]
        self.authority = {"source": "original synthetic native user turn", "scope": str(self.root),
            "reversible": True, "cost": False, "new_permission": False, "external": True, "personal_choice": False}
        self.registration = {"task_id": self.task, "category": "main", "key": "synthetic-publish",
            "summary": "已授权且已完成的合成发布", "target_root": str(self.root),
            "evidence": [{"path": str(self.source)}], "authority": self.authority,
            "recommendation": "验收已有结果", "acceptance_rule": "核对合成授权与实际读回版本"}
        self.proof = {"user_message_id": "user-1", "tool_use_id": "tool-1",
            "tool_result_sha256": hashlib.sha256(self.output.encode()).hexdigest(),
            "authorization_review": "Native user authorized this exact synthetic target and action.",
            "readback_review": "Read native output and current artifact; exact synthetic version matches."}

    def tearDown(self):
        self.store.conn.close()
        self.tmp.cleanup()

    def write_transcript(self):
        self.transcript.write_text("".join(json.dumps(r) + "\n" for r in self.rows))
        self.transcript.chmod(0o600)

    def issue(self):
        return self.store.register(copy.deepcopy(self.registration))["issue"]

    def completion(self):
        return {"verified": True, "reviewer": "synthetic executor",
                "review": "Original native authorization and published readback reread, exact scope matches.",
                "completed_external": copy.deepcopy(self.proof)}

    def accept(self, item):
        return self.store.accept({**self.completion(), "issue_id": item["id"],
            "revision": item["revision"], "evidence": [{"path": str(self.source)}]})

    def assert_rejected(self, item, pattern):
        before = self.store.get(item["id"])
        events = self.store.conn.execute("SELECT count(*) FROM closure_events").fetchone()[0]
        with self.assertRaisesRegex(ValueError, pattern): self.accept(item)
        self.assertEqual(self.store.get(item["id"]), before)
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM closure_events").fetchone()[0], events)

    def test_completed_external_can_be_accepted_without_human_choice(self):
        item = self.issue(); self.assertEqual(item["state"], "decision")
        result = self.accept(item)
        self.assertEqual(result["state"], "resolved")
        self.assertEqual(result["authority"], item["authority"])
        self.assertFalse(safe_authority(result))  # No permission for another push.
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM closure_choices").fetchone()[0], 0)
        accepted = json.loads(result["acceptance"])
        self.assertEqual(accepted["completed_external"]["turn"], hashlib.sha256(self.prompt.encode()).hexdigest())
        self.assertNotIn(self.prompt, result["acceptance"])
        self.assertNotIn(self.output, result["acceptance"])
        self.assertIsNone(result["recovery"])

    def test_registration_with_completion_never_leaves_a_pending_decision_or_notification(self):
        result = self.store.register({**self.registration, "completion": self.completion()})["issue"]
        self.assertEqual(result["state"], "resolved")
        kinds = [r[0] for r in self.store.conn.execute("SELECT kind FROM closure_events WHERE seq IN (SELECT event_seq FROM closure_notifications)")]
        self.assertEqual(kinds, ["accepted"])

    def test_invalid_registration_completion_rolls_back_every_issue_and_event(self):
        self.proof["tool_result_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "readback_invalid"):
            self.store.register({**self.registration, "completion": self.completion()})
        self.assertEqual(self.store.conn.execute("SELECT count(*) FROM closure_issues").fetchone()[0], 0)

    def test_completed_duplicate_does_not_reopen_decision(self):
        item = self.accept(self.issue())
        result = self.issue()
        self.assertEqual(result["id"], item["id"])
        self.assertEqual(result["state"], "resolved")
        self.assertEqual(result["revision"], item["revision"])

    def test_completed_registration_reply_lost_can_be_retried_without_reopening(self):
        payload = {**self.registration, "completion": self.completion()}
        item = self.store.register(payload)["issue"]
        second = self.store.register(payload)
        self.assertTrue(second["reused"])
        self.assertEqual(second["issue"], item)

    def test_reviewed_source_change_duplicate_compares_accepted_final_evidence(self):
        item = self.issue(); self.source.write_text("reviewed final synthetic content")
        result = self.store.accept({**self.completion(), "issue_id": item["id"], "revision": item["revision"],
            "evidence": [{"path": str(self.source)}], "source_change_explanation": "Final synthetic content independently read back."})
        duplicate = self.issue()
        self.assertEqual(duplicate["id"], result["id"])
        self.assertEqual(duplicate["state"], "resolved")
        self.assertEqual(duplicate["revision"], result["revision"])

    def test_boolean_claim_or_ordinary_accept_cannot_release_decision(self):
        item = self.issue()
        self.proof = True
        self.assert_rejected(item, "proof_required")
        payload = self.completion(); payload.pop("completed_external")
        with self.assertRaisesRegex(ValueError, "issue_not_accepting"):
            self.store.accept({**payload, "issue_id": item["id"], "revision": 1, "evidence": [{"path": str(self.source)}]})

    def test_cost_permission_personal_choice_and_irreversibility_remain_waiting(self):
        for key in ("cost", "new_permission", "personal_choice", "reversible"):
            with self.subTest(key=key):
                self.registration["key"] = key
                self.authority[key] = False if key == "reversible" else True
                self.assert_rejected(self.issue(), "risk_still_pending")
                self.authority[key] = True if key == "reversible" else False

    def test_actual_unanswered_choice_is_preserved(self):
        item = self.issue(); preview = self.store.choice_preview(item["id"])
        self.assert_rejected(item, "human_wait_preserved")
        self.assertIsNone(self.store.conn.execute("SELECT answered FROM closure_choices WHERE token=?", (preview["token"],)).fetchone()[0])

    def test_explicit_keep_waiting_cannot_be_replaced_by_completed_proof(self):
        item = self.issue(); preview = self.store.choice_preview(item["id"])
        item = self.store.decide({"token": preview["token"], "issue_id": item["id"],
                                 "revision": item["revision"], "choice": "keep_waiting"})
        self.assert_rejected(item, "human_wait_preserved")
        self.assertEqual(item["state"], "decision")

    def test_declared_pause_and_cancellation_not_released(self):
        item = self.issue()
        item = self.store.transition(item["id"], "canceled", "synthetic user cancellation", 1)
        self.assert_rejected(item, "issue_not_accepting")
        self.registration["key"] = "pause"
        self.authority["external"] = False
        item = self.issue()
        item = self.store.transition(item["id"], "blocked", "explicit pause", 1, "wait for original owner")
        self.assert_rejected(item, "human_wait_preserved")

    def test_duplicate_external_registration_preserves_declared_pause(self):
        item = self.issue(); preview = self.store.choice_preview(item["id"])
        item = self.store.decide({"token": preview["token"], "issue_id": item["id"],
                                 "revision": item["revision"], "choice": "accept_recommendation"})
        item = self.store.transition(item["id"], "blocked", "explicit pause",
                                     item["revision"], "wait for original owner")
        repeated = self.issue()
        self.assert_rejected(repeated, "human_wait_preserved")
        self.assertEqual(repeated["state"], "blocked")
        self.assertEqual(repeated["recovery"], item["recovery"])

    def test_non_tool_user_blocks_cannot_cross_a_later_human_turn(self):
        item = self.issue()
        for content in ([{"type": "text", "text": "Wait; do not publish."}],
                        [{"type": "unknown_user_block"}], [], None):
            with self.subTest(content=content):
                later = {**self.rows[0], "uuid": "later-block-user", "parentUuid": "user-1",
                         "message": {"role": "user", "content": content}}
                self.rows.append(later); self.rows[1]["parentUuid"] = later["uuid"]
                self.write_transcript()
                self.assert_rejected(item, "native_turn_changed")
                self.rows.pop(); self.rows[1]["parentUuid"] = "user-1"

    def test_historical_duplicate_state_cannot_erase_last_declared_pause(self):
        item = self.issue(); preview = self.store.choice_preview(item["id"])
        item = self.store.decide({"token": preview["token"], "issue_id": item["id"],
                                 "revision": item["revision"], "choice": "accept_recommendation"})
        item = self.store.transition(item["id"], "blocked", "explicit pause",
                                     item["revision"], "wait for original owner")
        # Model the old duplicate-registration bug in this temporary ledger.
        self.store.conn.execute("UPDATE closure_issues SET state='decision' WHERE id=?", (item["id"],))
        item = self.store.get(item["id"])
        self.assert_rejected(item, "human_wait_preserved")

    def test_explicit_executor_release_allows_existing_completion_review(self):
        self.authority["external"] = False
        item = self.issue()
        item = self.store.transition(item["id"], "blocked", "explicit pause",
                                     item["revision"], "wait for original owner")
        item = self.store.transition(item["id"], "verification", "original owner explicitly released pause",
                                     item["revision"])
        self.authority["external"] = True
        item = self.issue()
        self.assertEqual(self.accept(item)["state"], "resolved")

    def test_new_explicit_owner_choice_can_release_historical_declared_pause(self):
        item = self.issue(); preview = self.store.choice_preview(item["id"])
        item = self.store.decide({"token": preview["token"], "issue_id": item["id"],
                                 "revision": item["revision"], "choice": "accept_recommendation"})
        item = self.store.transition(item["id"], "blocked", "explicit pause",
                                     item["revision"], "wait for original owner")
        # Only this synthetic fixture recreates the former state-loss bug.
        self.store.conn.execute("UPDATE closure_issues SET state='decision' WHERE id=?", (item["id"],))
        item = self.store.get(item["id"]); preview = self.store.choice_preview(item["id"])
        item = self.store.decide({"token": preview["token"], "issue_id": item["id"],
                                 "revision": item["revision"], "choice": "accept_recommendation"})
        self.assertEqual(self.accept(item)["state"], "resolved")

    def test_mixed_tool_result_and_human_blocks_cannot_borrow_prior_consent(self):
        item = self.issue()
        self.rows[-1]["message"]["content"].append({"type": "text", "text": "Wait; do not publish."})
        self.write_transcript()
        self.assert_rejected(item, "native_turn_changed")

    def test_unmatched_tool_result_and_unknown_parent_envelopes_are_rejected(self):
        item = self.issue()
        extra = {**self.rows[-1], "uuid": "extra-result", "parentUuid": "user-1",
                 "message": {"role": "user", "content": [{"type": "tool_result",
                     "tool_use_id": "unmatched-tool", "is_error": False, "content": "synthetic"}]}}
        self.rows.append(extra); self.rows[1]["parentUuid"] = extra["uuid"]
        self.write_transcript(); self.assert_rejected(item, "native_lineage_invalid")
        self.rows[-1].update(type="unknown", message={"role": "unknown", "content": []})
        self.write_transcript(); self.assert_rejected(item, "native_lineage_invalid")

    def test_matching_intermediate_tool_results_preserve_original_human_turn(self):
        item = self.issue()
        call = {**self.rows[1], "uuid": "extra-call", "parentUuid": "user-1",
                "message": {"role": "assistant", "content": [{"type": "tool_use",
                    "id": "extra-tool", "name": "Bash", "input": {"command": "synthetic-preflight"}}]}}
        result = {**self.rows[-1], "uuid": "extra-result", "parentUuid": call["uuid"],
                  "message": {"role": "user", "content": [{"type": "tool_result",
                      "tool_use_id": "extra-tool", "is_error": False, "content": "synthetic preflight"}]}}
        self.rows.extend([call, result]); self.rows[1]["parentUuid"] = result["uuid"]
        self.write_transcript(); self.assertEqual(self.accept(item)["state"], "resolved")

    def test_attachment_metadata_in_lineage_does_not_invalidate_consent(self):
        item = self.issue()
        attach = {"uuid": "attach-1", "type": "attachment", "parentUuid": "user-1",
                  "sessionId": self.session, "isSidechain": False}
        self.rows[1]["parentUuid"] = "attach-1"
        self.rows.insert(1, attach)
        self.write_transcript()
        self.assertEqual(self.accept(item)["state"], "resolved")

    def test_unknown_non_user_non_assistant_type_still_fails_closed(self):
        item = self.issue()
        unknown = {"uuid": "unknown-1", "type": "mystery", "parentUuid": "user-1",
                   "sessionId": self.session, "isSidechain": False,
                   "message": {"role": "mystery", "content": []}}
        self.rows[1]["parentUuid"] = "unknown-1"
        self.rows.insert(1, unknown)
        self.write_transcript()
        self.assert_rejected(item, "native_lineage_invalid")

    def test_wrong_original_turn_or_user_message_cannot_borrow_authorization(self):
        item = self.issue()
        self.rows[0]["message"]["content"] = "An unrelated synthetic user question."
        self.write_transcript()
        self.assert_rejected(item, "native_user_mismatch")

    def test_identical_native_user_text_in_two_turns_is_not_unique_authorization(self):
        item = self.issue()
        later = copy.deepcopy(self.rows)
        later[0].update(uuid="user-2", parentUuid="result-1")
        later[1].update(uuid="assistant-2", parentUuid="user-2")
        later[2].update(uuid="result-2", parentUuid="assistant-2")
        later[1]["message"]["content"][0]["id"] = "tool-2"
        later[2]["message"]["content"][0]["tool_use_id"] = "tool-2"
        self.rows.extend(later); self.write_transcript()
        self.proof.update(user_message_id="user-2", tool_use_id="tool-2")
        self.assert_rejected(item, "native_turn_ambiguous")

    def test_invalid_native_record_and_duplicate_json_keys_fail_closed(self):
        item = self.issue()
        self.transcript.write_text('null\n')
        self.assert_rejected(item, "native_record_invalid")
        self.transcript.write_text('{"uuid":"one","uuid":"two"}\n')
        self.assert_rejected(item, "duplicate_key")

    def test_assistant_quote_or_side_agent_cannot_supply_user_authorization(self):
        item = self.issue()
        for field, value in (("type", "assistant"), ("isSidechain", True), ("isMeta", True), ("userType", "internal")):
            with self.subTest(field=field):
                original = copy.deepcopy(self.rows[0]); self.rows[0][field] = value; self.write_transcript()
                self.assert_rejected(item, "native_user_mismatch")
                self.rows[0] = original

    def test_later_user_turn_or_other_branch_cannot_reuse_prior_consent(self):
        item = self.issue()
        extra = {**self.rows[0], "uuid": "later-user", "parentUuid": "user-1",
                 "message": {"role": "user", "content": "Wait; do not publish."}}
        self.rows.append(extra); self.rows[1]["parentUuid"] = "later-user"; self.write_transcript()
        self.assert_rejected(item, "turn_changed")
        self.rows[1]["parentUuid"] = "missing-branch"; self.write_transcript()
        self.assert_rejected(item, "lineage_invalid")

    def test_missing_duplicate_failed_or_changed_readback_cannot_close(self):
        item = self.issue()
        original = copy.deepcopy(self.rows)
        for mutation in ("missing", "duplicate", "failed", "changed", "interrupted"):
            with self.subTest(mutation=mutation):
                self.rows = copy.deepcopy(original)
                if mutation == "missing": self.rows.pop()
                if mutation == "duplicate": self.rows.append(copy.deepcopy(self.rows[-1]))
                if mutation == "failed": self.rows[-1]["message"]["content"][0]["is_error"] = True
                if mutation == "changed": self.rows[-1]["message"]["content"][0]["content"] += " changed"
                if mutation == "interrupted": self.rows[-1]["toolUseResult"] = {"interrupted": True}
                self.write_transcript(); self.assert_rejected(item, "native_(tool_missing|duplicate|readback_invalid)")

    def test_unknown_platform_and_caller_transcript_fields_fail_closed(self):
        item = self.issue(); self.proof["transcript_path"] = str(self.transcript)
        self.assert_rejected(item, "proof_required")
        self.proof.pop("transcript_path")
        self.store.conn.execute("UPDATE closure_tasks SET platform='codex' WHERE id=?", (self.task,))
        self.assert_rejected(item, "native_identity_unsupported")

    def test_native_source_alias_hardlink_and_nonprivate_file_rejected(self):
        item = self.issue()
        self.transcript.chmod(0o644)
        self.assert_rejected(item, "source_not_private")
        self.transcript.chmod(0o600)
        link = self.root / "link.jsonl"; os.link(self.transcript, link)
        self.assert_rejected(item, "file_invalid")
        link.unlink()
        self.transcript.rename(link); self.transcript.symlink_to(link)
        with self.assertRaises(OSError): self.accept(item)
        self.assertEqual(self.store.get(item["id"])["state"], "decision")

    def test_revision_and_changed_local_evidence_still_need_review(self):
        item = self.issue(); self.source.write_text("a later unpublished change")
        self.assert_rejected(item, "source_change_explanation_required")
        item["revision"] = 999
        self.assert_rejected(item, "revision_changed")


if __name__ == "__main__": unittest.main()
