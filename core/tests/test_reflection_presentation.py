"""Full saved-input ancestry controls a generic derived reflection cache."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from activity_mvp.feedback.store import packed, digest
from activity_mvp.presentation_scope import POLICY_FILE, DisplayScope
from activity_mvp.reflection import analysis, presentation
from activity_mvp.reflection.store import Store
from tests.personal_scope_fixtures import register_personal


class ReflectionAnalysisDisplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.policy = register_personal(self, self.root, ["personal-session"])
        self.display = self.root / POLICY_FILE
        self.display.write_text(packed({"version": 1, "enabled": True,
            "retired_names": ["FormerOrg"], "retired_paths": [str(self.root / "company")],
            "retired_root_refs": []}))
        self.display.chmod(0o600)
        self.store = Store(self.root / "reflection")
        self.addCleanup(self.store.conn.close)
        self.request = "d" * 24
        self.run = self.root / "feedback" / "runs" / self.request / "1234567890"
        self.run.mkdir(parents=True, mode=0o700)
        for path in (self.root / "feedback", self.run.parent.parent, self.run.parent):
            path.chmod(0o700)
        self.input = self.run / "input.txt"
        self.schema = self.run / "output-schema.json"
        self.schema.write_text(packed(analysis.SCHEMA)); self.schema.chmod(0o600)
        self.store.set("last_analysis", {"at": 123.0, "request": self.request,
            "judgment": "not_found", "explanation": "A generic derived summary.",
            "coverage": {"total_discussions": 2}})
        self.store.conn.execute("INSERT INTO contexts(id,source_id,session,updated) VALUES('current','codex','personal-session',123)")
        self.store.conn.execute("INSERT INTO contexts(id,source_id,session,updated) VALUES('history','codex','personal-session',123)")
        for ident, context, text in (("current-turn", "current", "My current question."),
                                     ("history-turn", "history", "My earlier personal question.")):
            self.store.conn.execute("INSERT INTO turns(id,context_id,source_id,session,role,observed,text,reference) VALUES(?,?,'codex','personal-session','user',123,?,?)",
                (ident, context, text, packed({"path": str(self.root / (ident + ".jsonl"))})))
        self.data = {"current_contexts": [{"context_id": "current", "previous_summary": {},
            "turns": [{"turn_id": "current-turn", "role": "user", "text": "My current question."}]}],
            "historical_contexts": [{"context_id": "history", "summary": "A previous discussion.",
                "evidence": [{"turn_id": "history-turn", "role": "user", "text": "earlier personal"}]}],
            "previous_insights": []}
        self.save_input()

    def save_input(self):
        raw = "Synthetic analysis instructions.\n" + packed(self.data)
        self.input.write_text(raw); self.input.chmod(0o600)
        self.store.conn.execute("INSERT OR REPLACE INTO calls VALUES(?, 'settled', 123, ?)",
            (self.request, packed({"input_fingerprint": digest([raw, analysis.SCHEMA]),
                                  "input_ranges": {"current": ["only-current-is-not-complete-provenance"]}})))

    def project(self):
        return presentation.last_analysis(self.store, self.store.get("last_analysis"))

    def assert_hidden(self):
        before = list(self.store.conn.iterdump())
        result = self.project()
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("judgment", result)
        self.assertNotIn("coverage", result)
        self.assertNotIn("generic derived", result["explanation"])
        self.assertEqual(result["at"], 123.0)
        self.assertEqual(list(self.store.conn.iterdump()), before)

    def test_personal_history_substring_quotes_preserve_original_analysis(self):
        self.assertEqual(self.project(), self.store.get("last_analysis"))

    def test_preserved_personal_retired_turn_still_supports_analysis(self):
        self.store.conn.execute("CREATE TABLE retired_contexts AS SELECT * FROM contexts WHERE id='history'")
        self.store.conn.execute("CREATE TABLE retired_turns AS SELECT * FROM turns WHERE id='history-turn'")
        self.store.conn.execute("DELETE FROM turns WHERE id='history-turn'")
        self.assertEqual(self.project(), self.store.get("last_analysis"))

    def test_empty_or_orphan_current_input_is_unknown(self):
        self.data["current_contexts"][0]["turns"] = []
        self.save_input()
        self.assert_hidden()
        self.data["current_contexts"] = []
        self.save_input()
        self.assert_hidden()

    def test_retired_historical_input_hides_generic_summary_without_record_changes(self):
        self.data["historical_contexts"][0]["evidence"][0]["text"] = "FormerOrg"
        self.save_input()
        self.assert_hidden()

    def test_company_sibling_with_generic_quote_cannot_certify_personal_context(self):
        self.store.conn.execute("UPDATE turns SET text=text || ' FormerOrg' WHERE id='history-turn'")
        self.assert_hidden()

    def test_unknown_run_is_rejected_before_input_open(self):
        self.policy["personal_roots"] = [str(self.root / "somewhere-else")]
        with patch.object(analysis, "saved_input", side_effect=AssertionError("body opened before admission")):
            self.assert_hidden()

    def test_unregistered_historical_session_is_unknown(self):
        self.store.conn.execute("UPDATE turns SET session='unknown-session' WHERE id='history-turn'")
        self.assert_hidden()

    def test_changed_input_and_unsettled_call_are_unknown(self):
        self.input.write_text(self.input.read_text() + " ")
        self.assert_hidden()
        self.save_input()
        self.store.conn.execute("UPDATE calls SET state='unknown'")
        self.assert_hidden()

    def test_previous_summary_inherits_its_evidence_source(self):
        self.data["current_contexts"][0]["previous_summary"] = {"summary": "Generic previous batch.",
            "evidence": [{"id": "missing-turn", "role": "user", "quote": "A quote"}]}
        self.save_input()
        self.assert_hidden()

    def test_previous_insight_must_still_be_in_personal_source_view(self):
        self.data["previous_insights"] = [{"id": "missing-insight", "state": "muted",
            "question": "A generic previous issue.", "evidence": []}]
        self.save_input()
        self.assert_hidden()

    def test_current_text_or_historical_role_mismatch_is_unknown(self):
        self.data["current_contexts"][0]["turns"][0]["text"] = "A changed question."
        self.save_input()
        self.assert_hidden()
        self.data["current_contexts"][0]["turns"][0]["text"] = "My current question."
        self.data["historical_contexts"][0]["evidence"][0]["role"] = "assistant"
        self.save_input()
        self.assert_hidden()

    def test_missing_quote_role_is_unknown(self):
        self.data["historical_contexts"][0]["evidence"][0].pop("role")
        self.save_input()
        self.assert_hidden()

    def test_late_call_change_is_unknown_without_any_replay(self):
        original = analysis.saved_input
        def change(root, request):
            result = original(root, request)
            self.store.conn.execute("UPDATE calls SET state='unknown' WHERE id=?", (request,))
            return result
        with patch.object(analysis, "saved_input", side_effect=change):
            result = self.project()
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("judgment", result)
        self.assertEqual(self.store.rows("SELECT state FROM calls"), [{"state": "unknown"}])

    def test_previous_personal_insight_preserves_analysis(self):
        evidence = [{"id": "history-turn", "role": "user", "quote": "earlier personal"}]
        self.store.conn.execute("INSERT INTO insights(id,state,judgment,question,pattern,explanation,alternatives,refutation,distinguishing_question,evidence,fingerprint,created,updated) VALUES('prior','muted','preliminary','A prior question.','Pattern','','','A possible refutation.','',?,'unique',123,123)", (packed(evidence),))
        self.data["previous_insights"] = [{"id": "prior", "state": "muted",
            "question": "A prior question.", "pattern": "Pattern", "evidence": []}]
        self.save_input()
        self.assertEqual(self.project(), self.store.get("last_analysis"))

    def test_readonly_response_applies_the_same_complete_input_guard(self):
        self.data["historical_contexts"][0]["evidence"][0]["text"] = "FormerOrg"
        self.save_input()
        before = list(self.store.conn.iterdump())
        with Store(self.store.root, readonly=True) as readable:
            result = readable.response()["last_analysis"]
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("judgment", result)
        self.assertEqual(list(self.store.conn.iterdump()), before)

    def test_saved_input_alias_or_extra_attempt_is_unknown(self):
        original = self.run / "original.txt"
        self.input.rename(original)
        self.input.symlink_to(original)
        self.assert_hidden()
        self.input.unlink(); original.rename(self.input)
        (self.run.parent / "second-attempt").mkdir(mode=0o700)
        self.assert_hidden()

    def test_policy_change_or_late_input_replacement_is_unknown(self):
        original = analysis.saved_input
        count = 0
        def replace(root, request):
            nonlocal count
            count += 1
            result = original(root, request)
            if count == 1:
                self.input.write_text(self.input.read_text() + " ")
            return result
        with patch.object(analysis, "saved_input", side_effect=replace):
            self.assert_hidden()
        self.save_input()
        with patch.object(DisplayScope, "unchanged", side_effect=[True, False]):
            self.assert_hidden()

    def test_missing_display_policy_does_not_disable_source_checks(self):
        self.display.unlink()
        self.assertEqual(self.project(),self.store.get('last_analysis'))
        self.store.conn.execute("UPDATE turns SET session='unregistered'")
        self.assert_hidden()
