"""Parent source exclusions never rewrite child history or personal siblings."""
import json
import io
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
import uuid
from contextlib import contextmanager, redirect_stdout
from unittest.mock import patch

from activity_mvp import task_closure as closure
from activity_mvp.presentation_scope import DisplayScope, read_view
from activity_mvp import attention_handoff as handoff, attention_native as native


class ClosureSourceInheritanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.db = self.root / 'activity.sqlite'
        sqlite3.connect(self.db).close()
        self.source = self.root / 'receipt.txt'
        self.source.write_text('Synthetic personal receipt\n')
        self.store = closure.Store(self.db)
        self.addCleanup(self.store.conn.close)
        self.policy = self.root / 'agi-display-scope.json'
        self.policy.write_text(json.dumps({'version': 1, 'enabled': True,
            'retired_names': ['FormerOrg'], 'retired_paths': [], 'retired_root_refs': []}))
        self.policy.chmod(0o600)

    def item(self, key, objective):
        task = self.store.begin({'platform': 'codex', 'session': 'synthetic-inheritance',
            'turn': key, 'objective': objective, 'scope_root': str(self.root), 'complex': True})['task_id']
        return self.store.register({'task_id': task, 'category': 'related', 'key': key,
            'summary': 'Generic decision', 'target_root': str(self.root),
            'evidence': [{'path': str(self.source)}], 'authority': {
                'source': 'Synthetic owner instruction', 'scope': str(self.root),
                'reversible': True, 'cost': False, 'new_permission': False,
                'external': False, 'personal_choice': True},
            'recommendation': 'Keep waiting for the actual choice',
            'acceptance_rule': 'Owner chooses explicitly'})['issue']

    def test_cli_action_withdrawal_reaches_the_native_display_contract(self):
        from activity_mvp.attention_center import main
        company = self.item('company', 'FormerOrg company page')
        output = io.StringIO()
        with redirect_stdout(output), patch('sys.stdin', io.StringIO('{"content":"Retained old input","draft_revision":1}')):
            main(['--db', str(self.db), 'draft', '--id', 'closure:' + company['id']])
        result = json.loads(output.getvalue())
        self.assertEqual(result, handoff.Queue.withdrawn_status('source_not_in_display_scope'))

    def test_read_only_handoff_status_does_not_repeat_the_whole_projection(self):
        personal = self.item('personal', 'My personal notes')
        identifier = 'closure:' + personal['id']
        queue = handoff.Queue(self.db)
        with patch.object(handoff, 'Queue', return_value=queue), patch.object(queue, 'status', wraps=queue.status) as status, patch.object(queue, 'check_result') as check:
            self.assertEqual(handoff.command(self.db, 'handoff-status', identifier, {})['status'], 'ok')
        self.assertEqual(status.call_count, 1)
        check.assert_not_called()

    def test_count_limit_detail_and_missing_parent_inherit_source_without_writes(self):
        company = self.item('company', 'FormerOrg company page')
        personal = self.item('personal', 'My personal notes')
        before = list(self.store.conn.iterdump())
        view = read_view(self.store.conn, DisplayScope(self.root), ('closure_tasks', 'closure_issues'))
        self.assertEqual(view.execute('SELECT count(*) FROM closure_issues').fetchone()[0], 1)
        self.assertEqual(view.execute('SELECT id FROM main.closure_issues LIMIT 1').fetchone()[0], personal['id'])
        self.assertIsNone(view.execute('SELECT * FROM closure_issues WHERE id=?', (company['id'],)).fetchone())
        self.assertEqual(list(self.store.conn.iterdump()), before)
        self.store.conn.execute('PRAGMA foreign_keys=OFF')
        self.store.conn.execute('DELETE FROM closure_tasks WHERE id=?', (personal['task_id'],))
        self.assertEqual(view.execute('SELECT count(*) FROM closure_issues').fetchone()[0], 0)

    def test_projection_and_direct_detail_retract_generic_company_child(self):
        company = self.item('company', 'FormerOrg company page')
        personal = self.item('personal', 'My personal notes')
        before = list(self.store.conn.iterdump())
        rows = closure.projection(self.db)
        self.assertEqual([r['id'] for r in rows], ['closure:' + personal['id']])
        self.assertEqual(closure.projection(self.db, company['id']), [])
        self.assertEqual(closure.projection(self.db, personal['id'])[0]['id'], 'closure:' + personal['id'])
        self.assertEqual(list(self.store.conn.iterdump()), before)

    def test_notification_keeps_company_request_unsent_and_personal_choice_unchanged(self):
        company = self.item('company', 'FormerOrg company page')
        personal = self.item('personal', 'My personal notes')
        self.store.conn.execute('DELETE FROM closure_notifications')
        self.store.event('state_changed', {'from': 'pending', 'to': 'decision'}, issue=company['id'], task=company['task_id'], notify=True)
        with patch.object(closure.subprocess, 'run') as notice:
            closure._notify_pending(self.store)
        notice.assert_not_called()
        self.assertEqual(tuple(self.store.conn.execute('SELECT requested,error FROM closure_notifications').fetchone()), (0, None))
        self.store.event('state_changed', {'from': 'pending', 'to': 'decision'}, issue=personal['id'], task=personal['task_id'], notify=True)
        with patch.object(closure.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as notice:
            closure._notify_pending(self.store)
        self.assertEqual(notice.call_count, 1)
        self.assertEqual(self.store.get(company['id'])['state'], 'decision')
        self.assertEqual(self.store.get(personal['id'])['state'], 'decision')

    def test_notification_rechecks_parent_before_entering_sender(self):
        personal = self.item('personal', 'My personal notes')
        self.store.conn.execute('DELETE FROM closure_notifications')
        self.store.event('state_changed', {'from': 'pending', 'to': 'decision'}, issue=personal['id'], task=personal['task_id'], notify=True)
        original = DisplayScope.hidden_closure
        calls = []
        def change_parent(display, conn, item):
            calls.append(item['id'])
            if len(calls) == 3:
                conn.execute('UPDATE closure_tasks SET objective=? WHERE id=?', ('FormerOrg company page', item['task_id']))
            return original(display, conn, item)
        with patch.object(DisplayScope, 'hidden_closure', change_parent), patch.object(closure.subprocess, 'run') as notice:
            closure._notify_pending(self.store)
        notice.assert_not_called()
        self.assertGreaterEqual(len(calls), 3)
        self.assertEqual(self.store.get(personal['id'])['state'], 'decision')

    def test_tick_does_not_observe_or_dispatch_retired_parent(self):
        company = self.item('company', 'FormerOrg company page')
        self.store.conn.execute("UPDATE closure_issues SET state='running',owner='codex:synthetic' WHERE id=?", (company['id'],))
        with patch.object(self.store, 'observe_runtime') as observe, patch.object(self.store, 'dispatch') as dispatch, patch.object(closure, '_notify_pending'):
            closure._tick_locked(self.store)
        observe.assert_not_called()
        dispatch.assert_not_called()
        self.assertEqual(self.store.get(company['id'])['state'], 'running')

    def test_parent_read_is_fresh_and_missing_policy_preserves_existing_rules(self):
        personal = self.item('personal', 'My personal notes')
        display = DisplayScope(self.root)
        self.assertFalse(display.hidden_closure(self.store.conn, personal))
        self.store.conn.execute('UPDATE closure_tasks SET objective=? WHERE id=?', ('FormerOrg company page', personal['task_id']))
        self.assertTrue(display.hidden_closure(self.store.conn, personal))
        self.policy.unlink()
        self.assertFalse(DisplayScope(self.root).hidden_closure(self.store.conn, personal))

    def test_missing_parent_source_table_reports_unknown_instead_of_visible_children(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.execute('CREATE TABLE closure_issues(id TEXT,task_id TEXT,summary TEXT)')
        conn.execute("INSERT INTO closure_issues VALUES('generic','missing','Generic decision')")
        with self.assertRaisesRegex(ValueError, 'closure_display_parent_sources_unavailable'):
            read_view(conn, DisplayScope(self.root), ('closure_issues',))

    def test_parent_retirement_during_evidence_read_preserves_old_acceptance(self):
        personal = self.item('personal', 'My personal notes')
        self.store.conn.execute("UPDATE closure_issues SET state='resolved',acceptance=? WHERE id=?",
            (json.dumps({'evidence': json.loads(personal['evidence'])}), personal['id']))
        before = self.store.get(personal['id'])
        def retire_while_reading(_):
            self.store.conn.execute('UPDATE closure_tasks SET objective=? WHERE id=?',
                ('FormerOrg company page', personal['task_id']))
            return False
        with patch.object(closure, 'current', side_effect=retire_while_reading) as current, patch.object(closure, '_notify_pending'):
            closure._tick_locked(self.store)
        current.assert_called_once()
        self.assertEqual(self.store.get(personal['id']), before)

    def cached_handoff(self, item, *, dispatched=0):
        queue = handoff.Queue(self.db)
        identifier = 'closure:' + item['id']
        submission = str(uuid.uuid4())
        thread = str(uuid.uuid4())
        source = {'id': identifier, 'kind': 'closure', 'revision': str(item['revision']),
                  'closure_target': {'root': str(self.root), 'project_id': None}}
        with queue.db() as conn:
            conn.execute('INSERT INTO problems(id,title,thread_id,project_id,creation_state) VALUES(?,?,?,?,?)',
                (identifier, 'Generic retained title', thread if dispatched else None, 'synthetic-project' if dispatched else None, 'ready' if dispatched else 'new'))
            conn.execute('INSERT INTO submissions(id,problem_id,content,prompt,status,dispatched,verification,source,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (submission, identifier, 'Synthetic retained supplement', 'Synthetic retained prompt',
                 'verifying' if dispatched else 'queued', dispatched, json.dumps({'state': 'unavailable'}), json.dumps(source), 1, 1))
            conn.execute('INSERT INTO drafts VALUES(?,?,?,?)', (identifier, 'Synthetic retained draft', 1, 1))
        return queue, identifier, submission, thread

    def test_cached_status_and_command_withdraw_parent_without_reading_result_or_erasing_draft(self):
        personal = self.item('personal', 'My personal notes')
        queue, identifier, submission, _ = self.cached_handoff(personal)
        self.assertEqual(queue.status(identifier)['draft'], 'Synthetic retained draft')
        self.store.conn.execute('UPDATE closure_tasks SET objective=? WHERE id=?', ('FormerOrg company page', personal['task_id']))
        with queue.db() as conn:
            before = list(conn.iterdump())
        with patch.object(handoff.Queue, 'check_result') as check:
            result = handoff.command(self.db, 'handoff-status', identifier, {})
        check.assert_not_called()
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(queue.status(identifier)['draft'], '')
        self.assertIsNone(result['handoff'])
        with queue.db() as conn:
            self.assertEqual(list(conn.iterdump()), before)
        with self.assertRaises(handoff.HandoffError):
            queue.save_draft(identifier, 'A new hidden supplement', 2)
        with queue.db() as conn:
            self.assertEqual(list(conn.iterdump()), before)

    def test_queued_hidden_source_never_creates_or_sends(self):
        company = self.item('company', 'FormerOrg company page')
        queue, _, submission, _ = self.cached_handoff(company)
        with patch.object(native, 'AppServer') as server, patch.object(native, 'DesktopIPC') as ipc, patch.object(native, 'open_thread') as open_thread:
            queue.process(submission)
        server.assert_not_called(); ipc.assert_not_called(); open_thread.assert_not_called()
        with queue.db() as conn:
            row = conn.execute('SELECT status,dispatched,content,prompt FROM submissions WHERE id=?', (submission,)).fetchone()
        self.assertEqual(tuple(row), ('failed', 0, 'Synthetic retained supplement', 'Synthetic retained prompt'))

    def test_dispatched_hidden_source_only_checks_exact_original_receipt(self):
        company = self.item('company', 'FormerOrg company page')
        queue, identifier, submission, thread = self.cached_handoff(company, dispatched=1)
        receipt = {'turn_id': 'synthetic-original-turn', 'message_id': submission}
        with patch.object(native, 'AppServer') as factory, patch.object(native, 'DesktopIPC') as ipc, patch.object(native, 'open_thread') as open_thread, patch.object(native, 'receipt_in_thread', return_value=receipt):
            server = factory.return_value.__enter__.return_value
            server.project.return_value = 'synthetic-project'
            server.read.return_value = {'projectId': 'synthetic-project', 'cwd': str(self.root), 'turns': []}
            queue.process(submission)
        server.create.assert_not_called(); ipc.assert_not_called(); open_thread.assert_not_called()
        server.read.assert_called_once_with(thread)
        with queue.db() as conn:
            row = conn.execute('SELECT status,dispatched,receipt,content FROM submissions WHERE id=?', (submission,)).fetchone()
        self.assertEqual((row['status'], row['dispatched']), ('delivered', 1))
        self.assertEqual(json.loads(row['receipt']), receipt)
        self.assertEqual(row['content'], 'Synthetic retained supplement')
        self.assertEqual(queue.status(identifier)['status'], 'unavailable')

    def test_cached_status_rechecks_parent_after_read(self):
        personal = self.item('personal', 'My personal notes')
        queue, identifier, _, _ = self.cached_handoff(personal)
        original = queue.db
        @contextmanager
        def retire_after_read():
            with original() as conn:
                yield conn
            self.store.conn.execute('UPDATE closure_tasks SET objective=? WHERE id=?', ('FormerOrg company page', personal['task_id']))
        with patch.object(queue, 'db', retire_after_read):
            result = queue.status(identifier)
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['draft'], '')

    def test_personal_cache_with_retired_text_is_withdrawn_and_retained(self):
        personal = self.item('personal', 'My personal notes')
        queue, identifier, _, _ = self.cached_handoff(personal)
        with queue.db() as conn:
            conn.execute('UPDATE drafts SET content=? WHERE problem_id=?', ('FormerOrg retained material', identifier))
        result = queue.status(identifier)
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['draft'], '')
        with queue.db() as conn:
            self.assertEqual(conn.execute('SELECT content FROM drafts WHERE problem_id=?', (identifier,)).fetchone()[0], 'FormerOrg retained material')

    def test_retired_direct_result_check_never_reads_company_acceptance(self):
        company = self.item('company', 'FormerOrg company page')
        queue, identifier, submission, _ = self.cached_handoff(company, dispatched=1)
        with queue.db() as conn:
            conn.execute('UPDATE submissions SET receipt=? WHERE id=?', (json.dumps({'turn_id': 'synthetic-original-turn'}), submission))
            before = list(conn.iterdump())
        with patch.object(handoff, 'verify_resolution') as verify, self.assertRaises(handoff.HandoffError):
            queue.check_result(identifier)
        verify.assert_not_called()
        with queue.db() as conn:
            self.assertEqual(list(conn.iterdump()), before)

    def test_parent_retirement_during_assistance_check_blocks_new_send(self):
        personal = self.item('personal', 'My personal notes')
        queue, identifier, submission, _ = self.cached_handoff(personal)
        with queue.db() as conn:
            row = dict(conn.execute('SELECT * FROM submissions WHERE id=?', (submission,)).fetchone())
        source = json.loads(row['source'])
        source['closure_assistance'] = {'token': 'synthetic', 'thread_id': str(uuid.uuid4())}
        row['source'] = json.dumps(source)
        def retire_during_check(*_, **__):
            self.store.conn.execute('UPDATE closure_tasks SET objective=? WHERE id=?', ('FormerOrg company page', personal['task_id']))
        with patch.object(queue, 'assistance_source', side_effect=retire_during_check), self.assertRaises(handoff.HandoffError):
            queue.target_for(row, sending=True)

    def test_nonclosure_cache_requires_current_visible_source(self):
        queue = handoff.Queue(self.db)
        identifier = 'memory-fixture'
        visible = {'status': 'ok', 'item': {'id': identifier, 'kind': 'memory', 'summary': 'Personal fixture'}}
        with patch.object(handoff, 'detail', return_value=visible):
            self.assertEqual(queue.save_draft(identifier, 'Synthetic retained draft', 1)['draft'], 'Synthetic retained draft')
        with patch.object(handoff, 'detail', return_value={'status': 'changed'}):
            self.assertEqual(queue.status(identifier)['status'], 'unavailable')
        with patch.object(handoff, 'detail', side_effect=RuntimeError('Synthetic source unavailable')):
            self.assertEqual(queue.status(identifier)['reason'], 'display_scope_unavailable')
        with queue.db() as conn:
            self.assertEqual(conn.execute('SELECT content FROM drafts WHERE problem_id=?', (identifier,)).fetchone()[0], 'Synthetic retained draft')
