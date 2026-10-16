import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from activity_mvp.presentation_scope import DisplayScope, POLICY_FILE, read_view
from activity_mvp.retirement_snapshot import Snapshot, source_snapshot, CURRENT


class DisplayScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.policy = self.root / POLICY_FILE
        self.policy.write_text(json.dumps({"version": 1, "enabled": True,
            "retired_names": ["FormerOrg"], "retired_paths": ["/offline/archive/company"],
            "retired_root_refs": ["rootref_retired"]}))
        self.policy.chmod(0o600)

    def test_all_collections_details_and_embedded_metadata_exclude_retired_source(self):
        scope = DisplayScope(self.root)
        for name in ("items", "incidents", "insights", "cards", "memories", "closures", "notifications", "results"):
            with self.subTest(name=name):
                result = scope.project({"status": "ok", name: [{"id": "personal", "summary": "My notes"},
                    {"id": "retired", "summary": "FORMERORG meeting"}]})
                self.assertEqual([x["id"] for x in result[name]], ["personal"])
        self.assertEqual(scope.project({"status": "ok", "item": {"summary": "FormerOrg private"}})["reason"], "source_not_in_display_scope")
        self.assertTrue(scope.hidden(json.dumps({"path": "/offline/archive/company/report.md"})))
        self.assertTrue(scope.hidden(json.dumps({"title": "FormerOrg"}, ensure_ascii=True)))
        self.assertTrue(scope.hidden("rootref_retired"))

    def test_counts_are_visible_counts_and_unknown_does_not_become_zero(self):
        scope = DisplayScope(self.root)
        payload = {"interface": "attention-center", "status": "ok", "decision_count": 2,
            "decisions": [{"title": "FormerOrg"}, {"title": "Personal"}], "systems": [], "resources": []}
        self.assertEqual(scope.project(payload)["decision_count"], 1)
        payload["decision_count"] = None
        self.assertIsNone(scope.project(payload)["decision_count"])

    def test_filters_before_aggregate_and_limit_and_keeps_database_unchanged(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE incidents(id TEXT,summary TEXT)")
        conn.executemany("INSERT INTO incidents VALUES(?,?)", [("0", "FormerOrg"), ("1", "Personal")])
        before = list(conn.iterdump())
        view = read_view(conn, DisplayScope(self.root), ("incidents",))
        self.assertEqual(view.execute("SELECT count(*) FROM incidents").fetchone()[0], 1)
        self.assertEqual(view.execute("SELECT incidents.id FROM incidents LIMIT 1").fetchone()[0], "1")
        self.assertEqual(view.execute("WITH sample AS (SELECT * FROM main.incidents) SELECT count(*) FROM sample").fetchone()[0], 1)
        self.assertEqual(list(conn.iterdump()), before)

    def test_offline_archive_does_not_change_exclusion(self):
        scope = DisplayScope(self.root)
        self.assertFalse(Path("/offline/archive/company").exists())
        self.assertTrue(scope.hidden({"source_path": "/offline/archive/company/notes.md"}))

    def test_invalid_or_replaced_policy_is_unavailable(self):
        scope = DisplayScope(self.root)
        self.policy.write_text("{}")
        self.assertEqual(scope.project({"items": []})["status"], "unavailable")
        self.assertEqual(DisplayScope(self.root).project({"items": []})["status"], "unavailable")
        self.policy.unlink()
        self.policy.symlink_to(self.root / "missing")
        self.assertEqual(DisplayScope(self.root).project({"items": []})["status"], "unavailable")

    def test_missing_policy_preserves_existing_interface(self):
        self.policy.unlink()
        value = {"status": "ok", "items": [{"title": "FormerOrg"}]}
        self.assertEqual(DisplayScope(self.root).project(value), value)

    def test_policy_activation_during_read_withdraws_previous_unfiltered_result(self):
        content = self.policy.read_bytes()
        self.policy.unlink()
        scope = DisplayScope(self.root)
        self.policy.write_bytes(content); self.policy.chmod(0o600)
        self.assertEqual(scope.project({'items': [{'title': 'FormerOrg'}]})['status'], 'unavailable')

    def test_mixed_group_retains_personal_member_and_visible_count(self):
        result = DisplayScope(self.root).project({'items': [{'id': 'g', 'member_count': 2,
            'members': [{'id': 'old', 'title': 'FormerOrg'}, {'id': 'me', 'title': 'Personal note'}]}]})
        self.assertEqual(result['items'][0]['member_count'], 1)
        self.assertEqual(result['items'][0]['members'][0]['id'], 'me')

    def test_generic_incident_and_decision_inherit_retired_message_reference(self):
        conn = sqlite3.connect(':memory:'); self.addCleanup(conn.close)
        conn.executescript('CREATE TABLE incidents(id TEXT,title TEXT);CREATE TABLE messages(id TEXT,quote TEXT,context TEXT,reference TEXT);CREATE TABLE quotes(incident_id TEXT,message_id TEXT);CREATE TABLE decisions(id TEXT,incident_id TEXT);')
        conn.executemany('INSERT INTO incidents VALUES(?,?)', [('old','Generic issue'),('me','Personal issue')])
        conn.execute("INSERT INTO messages VALUES('message','Generic quote','','/offline/archive/company/source')")
        conn.execute("INSERT INTO quotes VALUES('old','message')")
        conn.executemany('INSERT INTO decisions VALUES(?,?)', [('d-old','old'),('d-me','me')])
        before = list(conn.iterdump())
        view = read_view(conn, DisplayScope(self.root), ('incidents','messages','decisions'))
        self.assertEqual(view.execute('SELECT id FROM decisions ORDER BY id LIMIT 1').fetchone()[0], 'd-me')
        self.assertEqual(view.execute('SELECT count(*) FROM incidents').fetchone()[0], 1)
        self.assertEqual(list(conn.iterdump()), before)

    def test_reflection_mixed_context_cannot_support_generic_insight_but_personal_retired_evidence_can(self):
        conn = sqlite3.connect(':memory:'); self.addCleanup(conn.close)
        conn.executescript('CREATE TABLE contexts(id TEXT,summary TEXT);CREATE TABLE turns(id TEXT,context_id TEXT,reference TEXT,text TEXT);CREATE TABLE retired_contexts(id TEXT,summary TEXT);CREATE TABLE retired_turns(id TEXT,context_id TEXT,reference TEXT,text TEXT);CREATE TABLE insights(id TEXT,evidence TEXT);')
        conn.execute("INSERT INTO contexts VALUES('mixed','Generic')")
        conn.executemany('INSERT INTO turns VALUES(?,?,?,?)', [('old','mixed','','FormerOrg'),('generic','mixed','','A generic question')])
        conn.execute("INSERT INTO retired_contexts VALUES('personal','Personal context')")
        conn.execute("INSERT INTO retired_turns VALUES('historic','personal','','Personal question')")
        conn.executemany('INSERT INTO insights VALUES(?,?)', [('leak',json.dumps([{'id':'generic'}])),('safe',json.dumps([{'id':'historic'}]))])
        view = read_view(conn, DisplayScope(self.root), ('contexts','turns','retired_contexts','retired_turns','insights'))
        self.assertEqual(view.execute('SELECT id FROM insights').fetchall(), [('safe',)])

    def memory_registry(self, value):
        path = self.root / '.hermes/memory-review-center/state/autonomy-policy.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value)); path.chmod(0o600)
        return path

    def test_memory_uses_positive_registration_and_rejects_company_metadata_without_body_read(self):
        from activity_mvp import task_closure
        registry = {'owner_id':'local-owner','bindings': {'me':[{'path':'/personal/note.md'}],
            'company':[{'path':'/personal/quote.md','applies_to':'enterprise_email'}]}, 'investigation_sources':{}}
        self.memory_registry(registry)
        sources = SimpleNamespace(classify=task_closure.personal_scope.classify, company_stopped=Mock(return_value=True), policy=Mock(return_value={
            'company_roots':['/offline/archive/company'], 'personal_roots':['/personal'], 'personal_files':[],
            'personal_source_ids':[], 'personal_session_ids':[]}))
        with patch('pathlib.Path.home', return_value=self.root), patch.object(task_closure, 'personal_scope', sources):
            scope = DisplayScope(self.root)
            self.assertFalse(scope.hidden_memory('me'))
            self.assertTrue(scope.hidden_memory('company'))
            self.assertTrue(scope.hidden_memory('unregistered'))
            self.assertTrue(scope.memory_unchanged())
            self.memory_registry({})
            self.assertFalse(scope.memory_unchanged())
            with self.assertRaisesRegex(ValueError, 'display_memory_sources_unavailable'):
                DisplayScope(self.root).hidden_memory('me')

    def test_search_filters_generic_index_entries_by_origin_before_limit(self):
        from activity_mvp.pipeline import search
        from activity_mvp.presentation_scope import display_scope
        from activity_mvp.storage import ensure_ready
        conn = ensure_ready(self.root / 'activity.sqlite'); self.addCleanup(conn.close)
        for n in range(55):
            key = 'old-' + str(n)
            conn.execute('INSERT INTO extracted_activities(id,session_id,started_at,activity_summary,deletion_group_id) VALUES(?,?,?,?,?)', (key,'fixture','2026-10-15T12:00:00+00:00','FormerOrg','fixture'))
            conn.execute('INSERT INTO search_index(entity_type,entity_id,started_at,summary) VALUES(?,?,?,?)', ('extracted_activity',key,'2026-10-15T12:00:00+00:00','Generic matching note'))
        conn.execute("INSERT INTO extracted_activities(id,session_id,started_at,activity_summary,deletion_group_id) VALUES('me','fixture','2026-10-14T12:00:00+00:00','Personal','fixture')")
        conn.execute("INSERT INTO search_index(entity_type,entity_id,started_at,summary) VALUES('extracted_activity','me','2026-10-14T12:00:00+00:00','Generic matching note')")
        conn.commit()
        with display_scope(self.root):
            self.assertEqual([r['entity_id'] for r in search(conn,'matching')], ['me'])

    def test_existing_database_event_schema_keeps_historical_document_exclusion(self):
        from activity_mvp.document_changes import _hmac_key, _hmac
        from activity_mvp.storage import ensure_ready
        from activity_mvp.presentation_scope import ACTIVITY_TABLES
        conn = ensure_ready(self.root / 'activity.sqlite'); self.addCleanup(conn.close)
        # The supported migration adds lineage to state/versions. Older
        # aggregate event tables do not necessarily have this newer column.
        conn.execute('ALTER TABLE file_change_events DROP COLUMN document_path_hmac')
        secret = _hmac_key(conn)
        retired = 'root_' + _hmac(secret, '/offline/archive/company')[:32]
        stamp = '2026-10-15T12:00:00+00:00'
        conn.execute('INSERT INTO file_watch_roots(root_id,root_label,created_at,updated_at) VALUES(?,?,?,?)',
                     (retired, 'FormerOrg', stamp, stamp))
        conn.execute('INSERT INTO file_watch_state(file_id,root_id,document_path_hmac,first_seen_at,last_seen_at,updated_at) VALUES(?,?,?,?,?,?)',
                     ('current', retired, 'current-document', stamp, stamp, stamp))
        conn.execute('INSERT INTO file_change_versions(id,aggregate_event_id,file_id,root_id,document_path_hmac,change_kind,changed_at,deletion_group_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                     ('version', 'event', 'removed', retired, 'historical-document', 'modified', stamp, 'fixture', stamp, stamp))
        conn.commit()
        conn.execute('PRAGMA query_only=ON')
        scope = DisplayScope(self.root)
        self.assertEqual(scope.retired_documents(conn), {'current-document', 'historical-document'})
        view = read_view(conn, scope, ACTIVITY_TABLES)
        self.assertEqual(view.execute('SELECT count(*) FROM document_signals').fetchone()[0], 0)

    def test_feedback_generic_quote_requires_registered_personal_session(self):
        from activity_mvp import task_closure
        from activity_mvp.source_governance import personal_store
        conn = sqlite3.connect(':memory:'); self.addCleanup(conn.close)
        conn.executescript('CREATE TABLE incidents(id TEXT,title TEXT);CREATE TABLE messages(id TEXT,session TEXT,reference TEXT,quote TEXT,context TEXT);CREATE TABLE quotes(incident_id TEXT,message_id TEXT);')
        conn.executemany('INSERT INTO incidents VALUES(?,?)', [('old','Generic title'),('me','Generic title')])
        for key, session in [('old','unregistered-company-session'),('me','personal-session')]:
            conn.execute('INSERT INTO messages VALUES(?,?,?,?,?)', (key,session,json.dumps({'path':'/personal/source.jsonl'}),'Generic quote',''))
            conn.execute('INSERT INTO quotes VALUES(?,?)', (key,key))
        conn.execute('PRAGMA query_only=ON')
        sources = SimpleNamespace(classify=task_closure.personal_scope.classify, company_stopped=Mock(return_value=True), policy=Mock(return_value={
            'company_roots':['/offline/archive/company'], 'personal_roots':['/personal'], 'personal_files':[],
            'personal_source_ids':[], 'personal_session_ids':['personal-session']}))
        with patch.object(task_closure,'personal_scope',sources), patch.object(personal_store,'scope',sources), patch('pathlib.Path.home',return_value=self.root):
            view=read_view(conn,DisplayScope(self.root),('incidents','messages','quotes'),personal_kind='feedback')
            self.assertEqual(view.execute('SELECT id FROM incidents').fetchall(), [('me',)])


class RetirementSnapshotTests(unittest.TestCase):
    def test_alias_retarget_during_projection_withdraws_cached_personal_result(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            personal=root/'personal';personal.mkdir()
            company=root/'company';company.mkdir()
            alias=root/'alias';alias.symlink_to(personal)
            sources=SimpleNamespace(company_stopped=Mock(return_value=True),
                policy=Mock(return_value={'company_roots':[str(company)]}))
            with self.assertRaisesRegex(ValueError,'source_policy_changed'):
                with source_snapshot(sources) as view:
                    self.assertFalse(view.retired(str(alias), ()))
                    alias.unlink();alias.symlink_to(company)

    def test_repeated_source_checks_load_policy_twice_not_per_item(self):
        sources = SimpleNamespace(company_stopped=Mock(return_value=True),
            policy=Mock(return_value={"company_roots": ["/old-company"]}))
        with source_snapshot(sources) as view:
            for _ in range(700):
                self.assertFalse(view.retired("/personal", ("/personal/evidence.md",)))
                self.assertTrue(view.retired("/old-company", ()))
            self.assertEqual(len(view.cache), 2)
        self.assertEqual(sources.policy.call_count, 2)
        self.assertIsNone(CURRENT.get())

    def test_mid_read_revocation_never_emits_cached_success(self):
        sources = SimpleNamespace(company_stopped=Mock(side_effect=[False, True]),
            policy=Mock(return_value={"company_roots": []}))
        with self.assertRaisesRegex(ValueError, "source_policy_changed"):
            with source_snapshot(sources) as view:
                self.assertFalse(view.retired("/personal", ()))
        self.assertIsNone(CURRENT.get())


if __name__ == "__main__":
    unittest.main()
