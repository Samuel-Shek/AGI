"""Synthetic native sessions; no production transcript or grant is accessed."""
from pathlib import Path
import json
import os
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from activity_mvp import conversation_scope as scope
from activity_mvp.feedback.store import Store as FeedbackStore, packed, digest
from activity_mvp.feedback import sources as feedback_sources, model as feedback_model
from activity_mvp.reflection.store import Store
from activity_mvp.reflection import sources, analysis, presentation
from activity_mvp.presentation_scope import POLICY_FILE
from tests.personal_scope_fixtures import register_personal


class ConversationAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.policy = register_personal(self, self.root)
        self.governance = self.root / "governance"
        self.governance.mkdir(mode=0o700)
        guard = patch.object(scope.personal_scope, "ROOT", self.governance)
        guard.start(); self.addCleanup(guard.stop)
        self.session = "00112233-4455-6677-8899-aabbccddeeff"
        self.native = self.root / "codex"
        self.path = self.native / "2026/10/16" / ("rollout-" + self.session + ".jsonl")
        self.path.parent.mkdir(parents=True)
        self.lines = [{"type": "session_meta", "payload": {
            "id": self.session, "source": "vscode", "cwd": str(self.root / "personal")}}]
        self.write_source()
        self.grant = {"schema_version": 1, "policy": scope.POLICY,
            "owner_id": "local-owner", "enabled": True, "lookback_days": 14, "revision": 1,
            "authorization": {"kind": "owner_explicit", "platform": "codex",
                "session": "synthetic-approval", "turn": "synthetic-turn",
                "observed_at": "2026-10-16T00:00:00+00:00"},
            "sources": [{"id": "codex", "platform": "codex", "path": str(self.native)}]}
        self.write_grant()

    def write_source(self):
        self.path.write_text("".join(packed(item) + "\n" for item in self.lines))
        self.path.chmod(0o600)

    def write_grant(self):
        scope.policy_path().write_text(packed(self.grant))
        scope.policy_path().chmod(0o600)

    def revoke(self):
        self.grant['enabled']=False;self.grant['revision']+=1;self.write_grant()

    def seed(self,feedback,store=None):
        feedback.set('config',{'enabled':True,'since':time.time()-14*86400})
        feedback.conn.execute("INSERT INTO sources(id,platform,path) VALUES('codex','codex',?)",(str(self.native),))
        self.lines.append({'timestamp':time.time(),'type':'response_item','payload':{
            'type':'message','role':'user','content':[{'type':'input_text','text':'你没有核对已有证据，请先核对。'}]}})
        self.write_source()
        if store is not None:
            store.set('enabled',True);sources.scan(store,feedback)

    def test_native_owner_primary_is_automatically_admitted_without_manual_id(self):
        self.assertEqual(scope.admission(self.session, str(self.path))["state"], "allowed")
        self.assertEqual(self.policy["personal_session_ids"], [])
        self.assertFalse(scope.personal_scope.admitted({"session_id": self.session, "path": str(self.path)}))
        self.assertEqual(scope.personal_scope.memory_classify("synthetic-memory", {"session_id": self.session}), "unknown")

    def test_prepare_batch_reuses_paths_but_rechecks_native_each_time(self):
        with patch.object(scope.personal_scope, 'within', wraps=scope.personal_scope.within) as paths:
            for _ in range(4):
                self.assertTrue(scope.allowed(self.session, str(self.path)))
            original_resolutions = paths.call_count
            paths.reset_mock()
            with patch.object(scope, '_native_jsonl', wraps=scope._native_jsonl) as native:
                with scope.source_read_batch():
                    for _ in range(4):
                        self.assertTrue(scope.allowed(self.session, str(self.path)))
                self.assertEqual(native.call_count, 4)
            self.assertGreater(original_resolutions, 0)
            self.assertEqual(paths.call_count, 0)

    def test_prepare_legacy_observation_reuses_dependencies_in_writable_view(self):
        with FeedbackStore(self.root/'feedback') as feedback, Store(self.root/'reflection') as store:
            self.seed(feedback, store)
            view = scope.filtered(store, 'reflection')
            before = store.rows('SELECT processed_seq FROM contexts')
            with patch.object(scope.personal_scope, 'admitted', wraps=scope.personal_scope.admitted) as legacy:
                outside = view.rows('SELECT id,role,text FROM turns ORDER BY seq')
                self.assertGreater(legacy.call_count, 0)
                legacy.reset_mock()
                with scope.source_read_batch(), patch.object(scope, '_native_jsonl', wraps=scope._native_jsonl) as native:
                    inside = view.rows('SELECT id,role,text FROM turns ORDER BY seq')
                    self.assertGreater(native.call_count, 0)
                self.assertEqual(legacy.call_count, 0)
            self.assertEqual(inside, outside)
            self.assertEqual(store.rows('SELECT processed_seq FROM contexts'), before)
            self.assertEqual(store.rows('SELECT id FROM calls'), [])

    def test_recent_sql_rows_skip_duplicate_legacy_checks_but_recheck_native(self):
        with FeedbackStore(self.root/'feedback') as feedback, Store(self.root/'reflection') as store:
            self.seed(feedback, store)
            view = scope.filtered(store, 'reflection')
            with scope.source_read_batch(), patch.object(scope, '_legacy_observation_allowed', wraps=scope._legacy_observation_allowed) as legacy, patch.object(scope, '_native_jsonl', wraps=scope._native_jsonl) as native:
                first = view.rows('SELECT id FROM turns ORDER BY seq')
                self.assertTrue(first)
                first_checks = native.call_count
                self.assertGreater(first_checks, 0)
                self.assertEqual(view.rows('SELECT id FROM turns ORDER BY seq'), first)
                self.assertGreater(native.call_count, first_checks)
                self.assertEqual(legacy.call_count, 0)

    def test_recent_sql_rows_keep_native_company_and_null_path_denials(self):
        with FeedbackStore(self.root/'feedback') as feedback, Store(self.root/'reflection') as store:
            self.seed(feedback, store)
            view = scope.filtered(store, 'reflection')
            with scope.source_read_batch():
                self.assertTrue(view.rows('SELECT id FROM turns'))
                self.lines[0]['payload']['cwd'] = str(self.root/'company'); self.write_source()
                self.assertEqual(view.rows('SELECT id FROM turns'), [])
                self.lines[0]['payload']['cwd'] = str(self.root/'personal'); self.write_source()
                self.assertTrue(view.rows('SELECT id FROM turns'))
                store.conn.execute("UPDATE turns SET reference=json_set(reference,'$.path',NULL)")
                self.assertEqual(view.rows('SELECT id FROM turns'), [])

    def test_recent_sql_rows_policy_change_poison_survives_restore(self):
        with FeedbackStore(self.root/'feedback') as feedback, Store(self.root/'reflection') as store:
            self.seed(feedback, store)
            view = scope.filtered(store, 'reflection')
            original = scope._native_jsonl
            def changed(*args):
                metadata = original(*args)
                self.policy['personal_session_ids'].append('changed-during-native')
                return metadata
            with self.assertRaisesRegex(PermissionError, 'source_policy_changed'):
                with scope.source_read_batch():
                    with patch.object(scope, '_native_jsonl', side_effect=changed):
                        self.assertEqual(view.rows('SELECT id FROM turns'), [])
                    self.policy['personal_session_ids'].clear()
                    self.assertEqual(view.rows('SELECT id FROM turns'), [])

    def test_recent_sql_shortcut_rejects_missing_same_row_native_gate(self):
        with FeedbackStore(self.root/'feedback') as feedback, Store(self.root/'reflection') as store:
            self.seed(feedback, store)
            original = scope.view_predicates
            gate = " AND personal_registered_metadata(session,CASE WHEN json_valid(reference) THEN json_extract(reference,'$.path') END)=1"
            for mutation in ('missing', 'or', 'alias', 'duplicate', 'unknown'):
                with self.subTest(mutation=mutation):
                    def changed(*args):
                        views = dict(original(*args))
                        self.assertIn(gate, views['turns'])
                        if mutation == 'missing': views['turns'] = views['turns'].replace(gate, '', 1)
                        elif mutation == 'or': views['turns'] += ' OR 1=1'
                        elif mutation == 'alias': views['turns'] = views['turns'].replace('personal_registered_metadata(session,', 'personal_registered_metadata(other_session,', 1)
                        elif mutation == 'duplicate': views['turns'] = views['turns'].replace(gate, gate+gate, 1)
                        else: views['turns'] += ' AND 1=1'
                        return views
                    with patch.object(scope, 'view_predicates', side_effect=changed):
                        with self.assertRaisesRegex(ValueError, 'legacy_conversation_view_unverifiable'):
                            scope.filtered(store, 'reflection')

    def test_sql_observation_keeps_old_legacy_window_without_grant_expansion(self):
        with FeedbackStore(self.root/'feedback') as feedback, Store(self.root/'reflection') as store:
            self.seed(feedback, store)
            store.conn.execute('UPDATE turns SET observed=?', (time.time()-16*86400,))
            view = scope.filtered(store, 'reflection')
            with scope.source_read_batch():
                self.assertEqual(view.rows('SELECT id FROM turns'), [])
            self.policy['personal_session_ids'].append(self.session)
            view = scope.filtered(store, 'reflection')
            with scope.source_read_batch():
                self.assertTrue(view.rows('SELECT id FROM turns'))

    def test_quoted_session_literals_cannot_bypass_row_or_context_admission(self):
        with FeedbackStore(self.root/'feedback') as feedback, Store(self.root/'reflection') as store:
            self.seed(feedback, store)
            self.lines[0]['payload']['cwd'] = str(self.root/'company'); self.write_source()
            for session in ('x) OR 1=1 --', "x') OR 1=1 --", "x),('y", 'x, y', "x (') ,)"):
                with self.subTest(session=session):
                    self.policy['personal_session_ids'][:] = [session]
                    view = scope.filtered(store, 'reflection')
                    with scope.source_read_batch():
                        self.assertEqual(view.rows('SELECT id FROM turns'), [])
                        self.assertEqual(view.rows('SELECT id FROM contexts'), [])

    def test_old_nonlegacy_sql_rows_are_denied_without_resolving_their_paths(self):
        with FeedbackStore(self.root/'feedback') as feedback, Store(self.root/'reflection') as store:
            self.seed(feedback, store)
            store.conn.execute('UPDATE turns SET observed=?', (time.time()-16*86400,))
            view = scope.filtered(store, 'reflection')
            with scope.source_read_batch():
                policy = scope._SOURCE_READS.get().current()
                self.assertNotIn(self.session, policy.policy['personal_session_ids'])
                with patch.object(policy, 'classify', wraps=policy.classify) as classify:
                    self.assertEqual(view.rows('SELECT id FROM turns'), [])
                    self.assertEqual(view.rows('SELECT id FROM contexts'), [])
                    self.assertEqual(classify.call_count, 0)

    def test_legacy_classification_change_poison_prevents_recent_fallback(self):
        with self.assertRaisesRegex(PermissionError, 'source_policy_changed'):
            with scope.source_read_batch():
                reader = scope._SOURCE_READS.get()
                policy = reader.current()
                original = policy.classify
                def changed(meta):
                    result = original(meta)
                    self.policy['personal_session_ids'].append('changed-during-classification')
                    return result
                with patch.object(policy, 'classify', side_effect=changed):
                    self.assertFalse(scope.observation_allowed(self.session, str(self.path), time.time()))
                self.policy['personal_session_ids'].remove('changed-during-classification')
                self.assertFalse(scope.observation_allowed(self.session, str(self.path), time.time()))
        self.assertIsNone(scope._SOURCE_READS.get())

    def test_legacy_classification_failure_has_no_other_snapshot_fallback(self):
        from activity_mvp.retirement_snapshot import CURRENT
        other = object(); token = CURRENT.set(other)
        try:
            with self.assertRaisesRegex(PermissionError, 'source_policy_changed'):
                with scope.source_read_batch():
                    policy = scope._SOURCE_READS.get().current()
                    with patch.object(policy, 'classify', side_effect=ValueError('invalid classification')):
                        self.assertFalse(scope.observation_allowed(self.session, str(self.path), time.time()))
                    self.assertFalse(scope.observation_allowed(self.session, str(self.path), time.time()))
                    self.assertIs(CURRENT.get(), other)
            self.assertIs(CURRENT.get(), other)
        finally:
            CURRENT.reset(token)

    def test_legacy_classification_timeout_propagates_and_closes_batch(self):
        from activity_mvp.local_checks import LocalCheckTimeout
        with self.assertRaises(LocalCheckTimeout):
            with scope.source_read_batch():
                policy = scope._SOURCE_READS.get().current()
                with patch.object(policy, 'classify', side_effect=LocalCheckTimeout('reflection_prepare')):
                    scope.observation_allowed(self.session, str(self.path), time.time())
        self.assertIsNone(scope._SOURCE_READS.get())

    def test_prepare_policy_dependency_changes_invalidate_runtime_acceptance(self):
        from activity_mvp.feedback import acceptance
        from activity_mvp import retirement_snapshot
        hashes = acceptance.guard_hashes()
        state = {'repair_capability': {'enabled': True, 'guards': hashes},
                 'isolation': {'passed': True}, 'model_capability': {'enabled': True}}
        self.assertTrue(acceptance.release_ready(state))
        dependency = Path(retirement_snapshot.__file__).resolve()
        read = Path.read_bytes
        def changed(path):
            value = read(path)
            return value+b'\n# synthetic changed dependency\n' if path.resolve() == dependency else value
        with patch.object(Path, 'read_bytes', changed):
            self.assertFalse(acceptance.release_ready(state))
        state['repair_capability']['guards'] = {k:v for k,v in hashes.items() if k != 'retirement_snapshot.py'}
        self.assertFalse(acceptance.release_ready(state))

    def test_prepare_batch_revocation_poison_survives_reenable(self):
        with self.assertRaisesRegex(PermissionError, 'source_policy_changed'):
            with scope.source_read_batch():
                self.assertTrue(scope.allowed(self.session, str(self.path)))
                self.revoke()
                self.assertFalse(scope.allowed(self.session, str(self.path)))
                self.grant['enabled'] = True; self.grant['revision'] += 1; self.write_grant()
                self.assertFalse(scope.allowed(self.session, str(self.path)))
        self.assertIsNone(scope._SOURCE_READS.get())
        with scope.source_read_batch():
            self.assertTrue(scope.allowed(self.session, str(self.path)))

    def test_prepare_batch_initial_policy_failure_cannot_become_idle(self):
        def work():
            with scope.source_read_batch():
                with patch('activity_mvp.retirement_snapshot.Snapshot', side_effect=ValueError('policy_invalid')):
                    self.assertFalse(scope.allowed(self.session, str(self.path)))
                # Recovery does not make this already invalid batch usable.
                self.assertFalse(scope.allowed(self.session, str(self.path)))
                return {'status': 'idle'}
        with self.assertRaisesRegex(PermissionError, 'source_policy_changed'):
            work()
        self.assertIsNone(scope._SOURCE_READS.get())
        with scope.source_read_batch():
            self.assertTrue(scope.allowed(self.session, str(self.path)))

    def test_prepare_batch_checks_policy_after_native_and_existing_personal_returns(self):
        original = scope._native_jsonl
        def changed(*args):
            metadata = original(*args)
            self.policy['personal_session_ids'].append(self.session)
            return metadata
        with self.assertRaisesRegex(PermissionError, 'source_policy_changed'):
            with scope.source_read_batch(), patch.object(scope, '_native_jsonl', side_effect=changed):
                self.assertFalse(scope.allowed(self.session, str(self.path)))
        with self.assertRaisesRegex(PermissionError, 'source_policy_changed'):
            with scope.source_read_batch():
                self.assertEqual(scope.admission(self.session, str(self.path))['reason'], 'existing_personal_registration')
                self.policy['personal_session_ids'].clear()
                self.assertFalse(scope.allowed(self.session, str(self.path)))

    def test_prepare_batch_keeps_native_identity_and_window_live(self):
        with scope.source_read_batch():
            self.assertTrue(scope.allowed(self.session, str(self.path)))
            self.lines[0]['payload']['cwd'] = str(self.root / 'company')
            self.write_source()
            self.assertFalse(scope.allowed(self.session, str(self.path)))
            self.lines[0]['payload']['cwd'] = str(self.root / 'personal')
            self.write_source()
            self.assertTrue(scope.allowed(self.session, str(self.path)))
            self.assertEqual(scope.admission(self.session, str(self.path), now=time.time()+15*86400)['reason'], 'outside_conversation_window')
            linked = self.path.with_name('linked.jsonl'); os.link(self.path, linked)
            self.assertFalse(scope.allowed(self.session, str(self.path)))

    def test_prepare_batch_cwd_alias_change_poison_and_exception_reset(self):
        personal = self.root / 'personal'; personal.mkdir()
        company = self.root / 'company'; company.mkdir()
        alias = self.root / 'project'; alias.symlink_to(personal)
        self.lines[0]['payload']['cwd'] = str(alias); self.write_source()
        with self.assertRaisesRegex(PermissionError, 'source_policy_changed'):
            with scope.source_read_batch():
                self.assertTrue(scope.allowed(self.session, str(self.path)))
                alias.unlink(); alias.symlink_to(company)
                self.assertFalse(scope.allowed(self.session, str(self.path)))
        from activity_mvp.local_checks import LocalCheckTimeout
        with self.assertRaises(LocalCheckTimeout):
            with scope.source_read_batch():
                scope.allowed(self.session, str(self.path))
                raise LocalCheckTimeout('reflection_prepare')
        self.assertIsNone(scope._SOURCE_READS.get())

    def test_prepare_batch_does_not_borrow_or_replace_other_snapshot(self):
        from activity_mvp.retirement_snapshot import CURRENT
        other = object(); token = CURRENT.set(other)
        try:
            with scope.source_read_batch():
                self.assertTrue(scope.allowed(self.session, str(self.path)))
                self.assertIs(CURRENT.get(), other)
        finally:
            CURRENT.reset(token)

    def test_prepare_batch_hermes_wal_changes_are_never_cached(self):
        path = self.root / 'hermes.sqlite'
        native = sqlite3.connect(path); self.addCleanup(native.close)
        native.execute('PRAGMA journal_mode=WAL'); native.execute('PRAGMA wal_autocheckpoint=0')
        native.executescript('CREATE TABLE sessions(id TEXT,source TEXT,user_id TEXT,parent_session_id TEXT,cwd TEXT);CREATE TABLE messages(id INTEGER,session_id TEXT,role TEXT,content TEXT,timestamp REAL);')
        native.execute('INSERT INTO sessions VALUES(?,?,?,?,?)', (self.session, 'cli', '', None, str(self.root)))
        native.execute('INSERT INTO messages VALUES(?,?,?,?,?)', (1,self.session,'user','synthetic',time.time()))
        native.commit(); path.chmod(0o600)
        self.grant['sources'] = [{'id':'hermes','platform':'hermes','path':str(path)}]; self.write_grant()
        before = scope._identity(scope._regular(path))
        with scope.source_read_batch():
            self.assertTrue(scope.allowed(self.session, str(path)))
            native.execute("UPDATE sessions SET parent_session_id='parent'"); native.commit()
            self.assertEqual(scope._identity(scope._regular(path)), before)
            self.assertFalse(scope.allowed(self.session, str(path)))
            native.execute('UPDATE sessions SET parent_session_id=NULL,cwd=?', (str(self.root/'company'),)); native.commit()
            self.assertFalse(scope.allowed(self.session, str(path)))

    def test_prepare_batch_ends_before_model_or_submission_callback(self):
        class ModelBoundary(Exception): pass
        with FeedbackStore(self.root/'feedback') as feedback, Store(self.root/'reflection') as store:
            self.seed(feedback,store)
            def model_boundary(*args, **kwargs):
                self.assertIsNone(scope._SOURCE_READS.get())
                self.assertTrue(kwargs['continue_check']())
                self.assertIsNone(scope._SOURCE_READS.get())
                raise ModelBoundary()
            with patch.object(analysis.model, 'invoke', side_effect=model_boundary):
                with self.assertRaises(ModelBoundary): analysis.analyze(store, feedback)
            self.assertEqual(store.conn.execute('SELECT count(*) FROM calls').fetchone()[0], 0)

    def test_feedback_non_message_events_do_not_repeat_native_admission(self):
        with FeedbackStore(self.root / "feedback") as feedback:
            self.lines.extend({"timestamp": time.time(), "type": "event_msg",
                               "payload": {"type": "task_progress", "text": "synthetic event"}}
                              for _ in range(200))
            self.seed(feedback)
            source = feedback.rows("SELECT * FROM sources WHERE id='codex'")[0]
            with patch.object(feedback_sources, "source_metadata_admitted",
                              wraps=feedback_sources.source_metadata_admitted) as checks:
                _, count, result = feedback_sources.scan_jsonl(feedback, source)
            self.assertEqual(count, 1)
            self.assertEqual(result["status"], "current")
            self.assertEqual(feedback.conn.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
            # File/header and actual message/insert gates remain fresh. Tool
            # progress adds no text and must not multiply those native checks.
            self.assertLessEqual(checks.call_count, 6)

    def test_company_project_is_rejected_even_inside_registered_transcript_root(self):
        self.lines[0]["payload"]["cwd"] = str(self.root / "company/project")
        self.write_source()
        self.assertEqual(scope.admission(self.session, str(self.path))["reason"], "company_source_stopped")

    def test_native_mixed_flags_and_ambiguous_project_paths_fail_closed(self):
        metadata=self.lines[0]['payload']
        for value in (True,1,'true','True'):
            with self.subTest(mixed=value):
                metadata['mixed']=value;self.write_source()
                self.assertEqual(scope.admission(self.session,str(self.path))['reason'],'company_source_stopped')
        metadata.pop('mixed')
        for key,value in (('root','relative-project'),('project_path',7)):
            with self.subTest(key=key):
                metadata[key]=value;self.write_source()
                self.assertEqual(scope.admission(self.session,str(self.path))['state'],'unknown')
                metadata.pop(key)

    def test_unknown_header_and_subagents_are_never_admitted(self):
        for update in ({"id": "another"}, {"source": {"subagent": "parent"}},
                       {"source": "exec"}, {"cwd": None}):
            with self.subTest(update=update):
                original = dict(self.lines[0]["payload"])
                self.lines[0]["payload"].update(update); self.write_source()
                self.assertFalse(scope.allowed(self.session, str(self.path)))
                self.lines[0]["payload"] = original

    def test_revocation_and_policy_change_invalidate_the_read_binding(self):
        before = scope.binding()
        self.grant["enabled"] = False; self.grant["revision"] += 1; self.write_grant()
        self.assertNotEqual(scope.binding(), before)
        self.assertEqual(scope.admission(self.session, str(self.path))["reason"], "conversation_authorization_revoked")

    def test_missing_unsafe_or_wrong_owner_grant_never_opens_native_metadata(self):
        for change in ({"owner_id": "other"}, {"authorization": {}}, {"lookback_days": 30}):
            with self.subTest(change=change):
                original = dict(self.grant); self.grant.update(change); self.write_grant()
                with patch.object(scope, "_native_jsonl", side_effect=AssertionError("unauthorized source opened")):
                    self.assertFalse(scope.allowed(self.session, str(self.path)))
                self.grant = original
        self.write_grant(); scope.policy_path().chmod(0o644)
        with patch.object(scope, "_native_jsonl", side_effect=AssertionError("unsafe grant opened source")):
            self.assertFalse(scope.allowed(self.session, str(self.path)))

    def test_outside_window_and_source_aliases_are_rejected(self):
        os.utime(self.path, (time.time() - 15 * 86400,) * 2)
        self.assertFalse(scope.allowed(self.session, str(self.path)))
        self.write_source()
        alias = self.path.with_name("alias.jsonl"); alias.symlink_to(self.path)
        self.assertFalse(scope.allowed(self.session, str(alias)))
        linked = self.path.with_name("hardlink.jsonl"); os.link(self.path, linked)
        self.assertFalse(scope.allowed(self.session, str(linked)))

    def test_claude_primary_metadata_works_without_promoting_quoted_material(self):
        self.grant["sources"][0]["platform"] = "claude"
        self.grant["sources"][0]["id"] = "claude"; self.write_grant()
        self.lines = [{"type": "user", "sessionId": self.session, "isSidechain": False,
            "userType": "external", "cwd": str(self.root / "personal"),
            "message": {"content": "Quoted content cannot grant permissions."}}]
        self.write_source()
        self.assertTrue(scope.allowed(self.session, str(self.path), "claude"))
        self.lines[0]["isSidechain"] = True; self.write_source()
        self.assertFalse(scope.allowed(self.session, str(self.path), "claude"))

    def test_hermes_local_identity_and_recent_query_do_not_depend_on_database_mtime(self):
        path=self.root/'hermes.sqlite'
        with sqlite3.connect(path) as db:
            db.executescript('CREATE TABLE sessions(id TEXT,source TEXT,user_id TEXT,started_at REAL,parent_session_id TEXT,cwd TEXT,git_repo_root TEXT,profile_name TEXT);CREATE TABLE messages(id INTEGER,session_id TEXT,role TEXT,content TEXT,timestamp REAL);')
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)',(self.session,'cli','',time.time()-40*86400,None,str(self.root),None,'default'))
            for ident,when in ((1,time.time()-15*86400),(2,time.time())):
                db.execute('INSERT INTO messages VALUES(?,?,?,?,?)',(ident,self.session,'user','我需要核对证据。',when))
        path.chmod(0o600);os.utime(path,(time.time()-40*86400,)*2)
        self.grant['sources']=[{'id':'hermes','platform':'hermes','path':str(path)}];self.write_grant()
        self.assertTrue(scope.allowed(self.session,str(path),'hermes'))
        with FeedbackStore(self.root/'feedback') as feedback,Store(self.root/'reflection') as store:
            feedback.set('config',{'enabled':True});store.set('enabled',True)
            feedback.conn.execute("INSERT INTO sources(id,platform,path) VALUES('hermes','hermes',?)",(str(path),))
            sources.scan(store,feedback)
            self.assertEqual(store.conn.execute('SELECT count(*) FROM turns').fetchone()[0],1)
        for statement in (
                "UPDATE sessions SET parent_session_id='parent'",
                "UPDATE sessions SET parent_session_id=NULL,cwd=NULL",
                "UPDATE sessions SET cwd=?,git_repo_root=NULL",
                "UPDATE sessions SET cwd=?,git_repo_root=?",
                "UPDATE sessions SET git_repo_root=NULL,profile_name='other'"):
            with self.subTest(statement=statement):
                parameters=(str(self.root/'company'),) if statement.count('?')==1 else (str(self.root),str(self.root/'company')) if statement.count('?')==2 else ()
                with sqlite3.connect(path) as db:db.execute(statement,parameters)
                self.assertFalse(scope.allowed(self.session,str(path),'hermes'))
        with sqlite3.connect(path) as db:db.execute("UPDATE sessions SET source='telegram',user_id='third-party',profile_name='default'")
        self.assertFalse(scope.allowed(self.session,str(path),'hermes'))

    def test_reflection_scan_and_filtered_views_use_the_same_new_admission(self):
        with FeedbackStore(self.root / "feedback") as feedback, Store(self.root / "reflection") as store:
            feedback.set("config", {"enabled": True, "since": time.time() - 14 * 86400})
            store.set("enabled", True)
            feedback.conn.execute("INSERT INTO sources(id,platform,path) VALUES('codex','codex',?)", (str(self.native),))
            for role, text in [("user", "我希望先核对证据。"), ("assistant", "我们先验证已有材料。")]:
                self.lines.append({"timestamp": time.time(), "type": "response_item", "payload": {
                    "type": "message", "role": role, "content": [{"type": "input_text", "text": text}]}})
            self.write_source()
            sources.scan(store, feedback)
            view = scope.filtered(store, "reflection")
            self.assertEqual(view.conn.execute("SELECT count(*) FROM contexts").fetchone()[0], 1)
            self.assertEqual(view.conn.execute("SELECT count(*) FROM turns").fetchone()[0], 2)
            self.assertTrue(analysis.ready(store))
            before = list(store.conn.iterdump())
            with Store(store.root, readonly=True) as readonly:
                visible = scope.filtered(readonly, "reflection")
                self.assertEqual(visible.conn.execute("SELECT count(*) FROM turns").fetchone()[0], 2)
            self.assertEqual(list(store.conn.iterdump()), before)
            guard = sources.bind_read_check(store, feedback)
            self.grant["enabled"] = False; self.grant["revision"] += 1; self.write_grant()
            self.assertFalse(guard())
            self.assertEqual(scope.filtered(store, "reflection").conn.execute("SELECT count(*) FROM turns").fetchone()[0], 0)
            self.assertFalse(analysis.ready(store))

    def test_live_readonly_hermes_view_rechecks_wal_project_and_parent_changes(self):
        path=self.root/'hermes.sqlite'
        native=sqlite3.connect(path);self.addCleanup(native.close)
        native.execute('PRAGMA journal_mode=WAL');native.execute('PRAGMA wal_autocheckpoint=0')
        native.executescript('CREATE TABLE sessions(id TEXT,source TEXT,user_id TEXT,parent_session_id TEXT,cwd TEXT);CREATE TABLE messages(id INTEGER,session_id TEXT,role TEXT,content TEXT,timestamp REAL);')
        native.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',(self.session,'cli','',None,str(self.root)))
        native.execute('INSERT INTO messages VALUES(?,?,?,?,?)',(1,self.session,'user','请核对已有证据。',time.time()))
        native.commit();path.chmod(0o600)
        native.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        self.grant['sources']=[{'id':'hermes','platform':'hermes','path':str(path)}];self.write_grant()
        with FeedbackStore(self.root/'feedback') as feedback,Store(self.root/'reflection') as store:
            feedback.set('config',{'enabled':True});store.set('enabled',True)
            feedback.conn.execute("INSERT INTO sources(id,platform,path) VALUES('hermes','hermes',?)",(str(path),))
            sources.scan(store,feedback)
            with Store(store.root,readonly=True) as read,sqlite3.connect(path) as writer:
                self.assertEqual(read.conn.execute('SELECT count(*) FROM turns').fetchone()[0],1)
                before=scope._identity(scope._regular(path))
                writer.execute('UPDATE sessions SET cwd=?',(str(self.root/'company'),));writer.commit()
                self.assertTrue(Path(str(path)+'-wal').exists())
                self.assertEqual(scope._identity(scope._regular(path)),before)
                self.assertEqual(read.conn.execute('SELECT count(*) FROM turns').fetchone()[0],0)
                self.assertEqual(read.conn.execute('SELECT count(*) FROM contexts').fetchone()[0],0)
                writer.execute('UPDATE sessions SET cwd=?',(str(self.root),));writer.commit()
                self.assertEqual(read.conn.execute('SELECT count(*) FROM turns').fetchone()[0],1)
                writer.execute("UPDATE sessions SET parent_session_id='parent'");writer.commit()
                self.assertEqual(scope._identity(scope._regular(path)),before)
                self.assertEqual(read.conn.execute('SELECT count(*) FROM turns').fetchone()[0],0)

    def test_company_header_rejected_before_any_conversation_text_processing(self):
        self.lines[0]["payload"]["cwd"] = str(self.root / "company")
        self.write_source()
        with FeedbackStore(self.root / "feedback") as feedback, Store(self.root / "reflection") as store:
            feedback.set("config", {"enabled": True}); store.set("enabled", True)
            feedback.conn.execute("INSERT INTO sources(id,platform,path) VALUES('codex','codex',?)", (str(self.native),))
            with patch.object(sources, "visible_text", side_effect=AssertionError("company text processed")):
                sources.scan(store, feedback)
            self.assertEqual(store.conn.execute("SELECT count(*) FROM turns").fetchone()[0], 0)

    def test_revocation_before_actual_reflection_body_read_stops_the_scan(self):
        with FeedbackStore(self.root/'feedback') as feedback,Store(self.root/'reflection') as store:
            self.seed(feedback);store.set('enabled',True)
            original=sources.read_active;reads=0
            def guarded(store,active,operation):
                nonlocal reads
                reads+=1
                if reads==4:self.revoke()
                return original(store,active,operation)
            with patch.object(sources,'read_active',side_effect=guarded),patch.object(sources,'visible_text',side_effect=AssertionError('body processed after revocation')):
                sources.scan(store,feedback)
            self.assertEqual(store.conn.execute('SELECT count(*) FROM turns').fetchone()[0],0)

    def test_feedback_reader_rechecks_grant_at_each_body_read(self):
        with FeedbackStore(self.root/'feedback') as feedback:
            self.seed(feedback)
            original=feedback_sources.read_active;reads=0
            def guarded(store,active,operation):
                nonlocal reads
                reads+=1
                if reads==4:self.revoke()
                return original(store,active,operation)
            with patch.object(feedback_sources,'read_active',side_effect=guarded),patch.object(feedback_sources,'_text',side_effect=AssertionError('body processed after revocation')):
                feedback_sources.scan(feedback)
            self.assertEqual(feedback.conn.execute('SELECT count(*) FROM messages').fetchone()[0],0)

    def test_reflection_revocation_after_payload_blocks_model_admission(self):
        with FeedbackStore(self.root/'feedback') as feedback,Store(self.root/'reflection') as store:
            self.seed(feedback,store)
            before=feedback.rows('SELECT * FROM budget')
            def invoke(*args,**kwargs):
                self.revoke();self.assertFalse(kwargs['continue_check']())
                kwargs['admission_check']()
                self.fail('revoked payload admitted')
            with patch.object(analysis.model,'invoke',side_effect=invoke):
                with self.assertRaises(PermissionError):analysis.analyze(store,feedback)
            self.assertEqual(feedback.rows('SELECT * FROM budget'),before)
            self.assertFalse(store.rows('SELECT * FROM calls'))

    def test_feedback_revocation_after_payload_blocks_model_admission(self):
        with FeedbackStore(self.root/'feedback') as feedback:
            self.seed(feedback);feedback_sources.scan(feedback)
            self.assertEqual(feedback.conn.execute('SELECT count(*) FROM messages').fetchone()[0],1)
            def invoke(*args,**kwargs):
                self.revoke();self.assertFalse(kwargs['continue_check']())
                kwargs['admission_check']()
                self.fail('revoked feedback admitted')
            with patch.object(feedback_model,'invoke',side_effect=invoke):
                with self.assertRaises(PermissionError):feedback_model.analyze(feedback)
            self.assertFalse(feedback.rows('SELECT * FROM incidents'))

    def test_actual_settlement_preserves_submitted_sources_and_read_boundary(self):
        self.policy['personal_roots']=[str(self.governance)]
        with FeedbackStore(self.root/'feedback') as feedback,Store(self.root/'reflection') as store:
            self.seed(feedback,store)
            submitted={}
            def invoke(current,prompt,schema,**options):
                options['admission_check']()
                request=current.reserve('analysis')
                options['on_reserved'](request)
                submitted.update(json.loads(store.rows('SELECT detail FROM calls WHERE id=?',(request,))[0]['detail']))
                run=current.root/'runs'/request/'123'
                run.mkdir(parents=True,mode=0o700)
                for directory in (run.parent,run.parent.parent):directory.chmod(0o700)
                for name,text in [('input.txt',prompt),('output-schema.json',packed(schema))]:
                    (run/name).write_text(text);(run/name).chmod(0o600)
                data=json.loads(prompt[prompt.index('\n{')+1:])
                result={'contexts':[{'context_id':c['context_id'],'summary':'已核对本次材料。','evidence':[]} for c in data['current_contexts']],
                    'insight':{'related_insight':'','judgment':'not_found',**{k:'' for k in analysis.FIELDS},'evidence':[]},'reviews':[]}
                current.settle(request,'settled',{'fixture':True})
                self.assertTrue(options['continue_check']())
                return result,request
            with patch.object(analysis.model,'invoke',side_effect=invoke):
                self.assertEqual(analysis.analyze(store,feedback)['processed'],1)
            call=store.rows('SELECT * FROM calls')[0]
            self.assertEqual(call['state'],'settled')
            detail=json.loads(call['detail'])
            self.assertIn('input_sources',detail)
            self.assertEqual({key:detail[key] for key in submitted},submitted)
            self.assertTrue(detail['input_sources']['contexts'])
            self.assertTrue(detail['input_sources']['turns'])
            value=store.get('last_analysis')
            self.assertEqual(presentation.last_analysis(store,value),value)
            # Corruption must still be rejected before opening saved input.
            changed=json.loads(call['detail'])
            changed['input_sources']['turns'][0]['fingerprint']='f'*64
            store.conn.execute('UPDATE calls SET detail=? WHERE id=?',(packed(changed),call['id']))
            with patch.object(analysis,'saved_input',side_effect=AssertionError('altered manifest opened input')):
                self.assertEqual(presentation.last_analysis(store,value)['status'],'unavailable')
            store.conn.execute('UPDATE calls SET detail=? WHERE id=?',(call['detail'],call['id']))
            self.revoke()
            with patch.object(analysis,'saved_input',side_effect=AssertionError('revoked source opened input')):
                self.assertEqual(presentation.last_analysis(store,value)['status'],'unavailable')

    def test_settled_run_is_readable_without_granting_its_directory(self):
        self.policy['personal_roots']=[str(self.governance)]
        display=self.root/POLICY_FILE
        display.write_text(packed({'version':1,'enabled':True,'retired_names':[],
            'retired_paths':[str(self.root/'company')],'retired_root_refs':[]}));display.chmod(0o600)
        with FeedbackStore(self.root/'feedback') as feedback,Store(self.root/'reflection') as store:
            self.seed(feedback,store)
            data,known,through=analysis.payload(store)
            self.assertTrue(data['current_contexts'])
            request='d'*24;run=feedback.root/'runs'/request/'123';run.mkdir(parents=True,mode=0o700)
            for directory in (run.parent,run.parent.parent):directory.chmod(0o700)
            raw=analysis.SYSTEM+'\n'+packed(data)
            for name,value in [('input.txt',raw),('output-schema.json',packed(analysis.SCHEMA))]:
                (run/name).write_text(value);(run/name).chmod(0o600)
            store.conn.execute("INSERT INTO calls VALUES(?,'settled',?,?)",(request,time.time(),packed({
                'input_fingerprint':digest([raw,analysis.SCHEMA]),'input_ranges':analysis.input_ranges(data,known,store),
                'input_sources':analysis.input_sources(data,known,store)})))
            value={'at':time.time(),'request':request,'judgment':'not_found'}
            with self.assertRaises(PermissionError):analysis.saved_input(feedback.root,request)
            self.assertEqual(presentation.last_analysis(store,value),value)
            store.conn.execute("UPDATE calls SET state='unknown'")
            with patch.object(analysis,'saved_input',side_effect=AssertionError('unknown run opened')):
                self.assertEqual(presentation.last_analysis(store,value)['status'],'unavailable')
            store.conn.execute("UPDATE calls SET state='settled'");self.revoke()
            with patch.object(analysis,'saved_input',side_effect=AssertionError('revoked run opened')):
                self.assertEqual(presentation.last_analysis(store,value)['status'],'unavailable')

    def test_entire_saved_payload_is_admitted_before_any_run_bytes(self):
        self.policy['personal_roots']=[str(self.governance)]
        with FeedbackStore(self.root/'feedback') as feedback,Store(self.root/'reflection') as store:
            self.seed(feedback,store)
            data,known,_=analysis.payload(store)
            original=next(iter(known.values()))
            store.conn.execute("INSERT INTO contexts(id,source_id,session,updated) VALUES('history','codex',?,?)",(self.session,time.time()))
            store.conn.execute("INSERT INTO turns(id,context_id,source_id,session,role,observed,text,reference) VALUES('historical-turn','history','codex',?,'user',?,?,?)",(self.session,time.time(),original['text'],original['reference']))
            history=store.rows("SELECT * FROM turns WHERE id='historical-turn'")[0];known[history['id']]=history
            data['historical_contexts']=[{'context_id':'history','summary':'Synthetic historical summary.',
                'evidence':[{'id':history['id'],'role':'user','quote':history['text']}]}]
            store.conn.execute("INSERT INTO insights(id,state,judgment,question,pattern,explanation,alternatives,refutation,distinguishing_question,evidence,fingerprint,created,updated) VALUES('old','muted','preliminary','我需要哪些证据？','','','','','',?,'unique',?,?)",
                (packed([{'id':original['id'],'role':'user','quote':original['text']}]),time.time(),time.time()))
            data['previous_insights']=[{'id':'old','state':'muted','question':'我需要哪些证据？','pattern':''}]
            request='a'*24;run=feedback.root/'runs'/request/'123';run.mkdir(parents=True,mode=0o700)
            for directory in (run.parent,run.parent.parent):directory.chmod(0o700)
            raw=analysis.SYSTEM+'\n'+packed(data)
            for name,text in [('input.txt',raw),('output-schema.json',packed(analysis.SCHEMA))]:
                (run/name).write_text(text);(run/name).chmod(0o600)
            detail={'input_fingerprint':digest([raw,analysis.SCHEMA]),'input_ranges':analysis.input_ranges(data,known,store),
                'input_sources':analysis.input_sources(data,known,store)}
            store.conn.execute("INSERT INTO calls VALUES(?,'settled',?,?)",(request,time.time(),packed(detail)))
            value={'request':request,'judgment':'not_found','at':time.time()}
            # The positive display also verifies historical and old-insight text.
            data['previous_insights'][0]['evidence']=[]
            raw=analysis.SYSTEM+'\n'+packed(data);(run/'input.txt').write_text(raw)
            detail['input_fingerprint']=digest([raw,analysis.SCHEMA]);store.conn.execute('UPDATE calls SET detail=?',(packed(detail),))
            self.assertEqual(presentation.last_analysis(store,value),value)
            store.conn.execute("UPDATE turns SET reference=? WHERE id='historical-turn'",(packed({'path':str(self.root/'company/retired.jsonl')}),))
            with patch.object(analysis,'saved_input',side_effect=AssertionError('retired history bytes opened')):
                self.assertEqual(presentation.last_analysis(store,value)['status'],'unavailable')
            store.conn.execute("UPDATE turns SET reference=? WHERE id='historical-turn'",(history['reference'],))
            store.conn.execute("UPDATE insights SET evidence=? WHERE id='old'",(packed([{'id':'not-admitted'}]),))
            with patch.object(analysis,'saved_input',side_effect=AssertionError('retired insight bytes opened')):
                self.assertEqual(presentation.last_analysis(store,value)['status'],'unavailable')
            detail.pop('input_sources');store.conn.execute('UPDATE calls SET detail=?',(packed(detail),))
            with patch.object(analysis,'saved_input',side_effect=AssertionError('unbound legacy bytes opened')):
                self.assertEqual(presentation.last_analysis(store,value)['status'],'unavailable')

    def test_zero_contexts_distinguish_reading_failure_and_no_material(self):
        with Store(self.root/'reflection') as store:
            store.set('enabled',True)
            self.assertEqual(presentation.pipeline_status(store,True,{},0)['state'],'no_material')
            store.conn.execute("INSERT INTO sources(id,platform,path,status) VALUES('codex','codex',?,'pending')",(str(self.native),))
            self.assertEqual(presentation.pipeline_status(store,True,{},0)['state'],'reading')
            store.conn.execute("UPDATE sources SET status='unavailable'")
            self.assertEqual(presentation.pipeline_status(store,True,{},0)['state'],'source_unavailable')
            store.set('last_error','synthetic analysis failure')
            self.assertEqual(presentation.pipeline_status(store,True,{},0)['state'],'analysis_failed')

    def test_recent_file_does_not_keep_expired_cached_messages_admitted(self):
        with FeedbackStore(self.root/'feedback') as feedback,Store(self.root/'reflection') as store:
            self.seed(feedback,store);feedback_sources.scan(feedback)
            self.assertTrue(scope.allowed(self.session,str(self.path)))
            expired=time.time()-15*86400
            store.conn.execute('UPDATE turns SET observed=?',(expired,))
            feedback.conn.execute('UPDATE messages SET observed=?',(expired,))
            self.assertEqual(scope.filtered(store,'reflection').conn.execute('SELECT count(*) FROM turns').fetchone()[0],0)
            self.assertEqual(scope.filtered(store,'reflection').conn.execute('SELECT count(*) FROM contexts').fetchone()[0],0)
            self.assertEqual(scope.filtered(feedback).conn.execute('SELECT count(*) FROM messages').fetchone()[0],0)
            with Store(store.root,readonly=True) as read:
                self.assertEqual(read.conn.execute('SELECT count(*) FROM turns').fetchone()[0],0)

    def test_both_consumers_share_existing_full_credential_interception(self):
        self.assertIs(sources.CREDENTIALS,feedback_sources.CREDENTIALS)
        # Build synthetic key delimiters at runtime so release scanning still
        # rejects literal credential headers anywhere in the source tree.
        key_mark='-'*5
        synthetic_key=key_mark+'BEGIN '+'PRIVATE KEY'+key_mark+'\nsynthetic-key\n'+key_mark+'END '+'PRIVATE KEY'+key_mark
        for text in ('token=synthetic-credential','refresh_token: synthetic-refresh',
                'client_secret=synthetic-secret','Authorization: Basic synthetic-value',
                synthetic_key):
            with self.subTest(kind=text.split()[0]):
                cleaned,redacted=feedback_sources.clean('你没有处理。'+text)
                self.assertTrue(redacted)
                self.assertNotIn('synthetic-',cleaned)
                self.assertNotIn('synthetic-',sources.visible_text(text))
