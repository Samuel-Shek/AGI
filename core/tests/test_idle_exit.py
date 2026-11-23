import json
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from activity_mvp.feedback import idle_exit, project_repair

class ExitReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name).resolve() / "receipt.json"
        self.request = dict(pid=1234, revision="abc123", nonce=str(uuid.uuid4()), created=100.0)
        self.receipt = dict(schema=1, instance=str(uuid.uuid4()), pid=1234, revision="abc123", nonce=self.request["nonce"], request_created=100.0, checked_at=101.0, reasons=["unsaved"], can_exit=False)
    def write(self, value=None):
        self.path.write_text(json.dumps(self.receipt if value is None else value)); self.path.chmod(0o600)
    def test_exact_private_receipt_and_fixed_recovery_messages(self):
        self.write(); self.assertEqual(idle_exit.read_receipt(self.path, self.request, now=102), self.receipt)
        for code in idle_exit.REASONS:
            text=idle_exit.presentation(idle_exit.unavailable(code))
            self.assertTrue(text['summary']); self.assertTrue(text['next_trigger'])
        self.assertEqual(idle_exit.presentation(dict(can_exit=True,reasons=[]))['summary'],'已具备安全退出条件。')
    def test_wrong_identity_age_schema_or_unapproved_content_is_rejected(self):
        changes=[dict(nonce=str(uuid.uuid4())),dict(pid=True),dict(pid=1235),dict(revision="other"),dict(instance="bad"),dict(request_created=99),dict(checked_at=103),dict(checked_at=float('nan')),dict(reasons=['private body']),dict(reasons=['unsaved','unsaved']),dict(can_exit=True),dict(body='private input')]
        for change in changes:
            with self.subTest(change=list(change)):
                self.write({**self.receipt, **change}); self.assertIsNone(idle_exit.read_receipt(self.path,self.request,now=102))
        self.write(); self.assertIsNone(idle_exit.read_receipt(self.path,self.request,now=105))
    def test_duplicate_json_key_permissions_symlink_and_hardlink_are_rejected(self):
        self.write(); text=self.path.read_text(); self.path.write_text(text[:-1]+',"pid":1234}')
        self.assertIsNone(idle_exit.read_receipt(self.path,self.request,now=102))
        self.write(); self.path.chmod(0o644); self.assertIsNone(idle_exit.read_receipt(self.path,self.request,now=102))
        self.write(); alias=self.path.with_name('alias'); os.link(self.path,alias)
        self.assertIsNone(idle_exit.read_receipt(self.path,self.request,now=102)); alias.unlink()
        alias.symlink_to(self.path); self.assertIsNone(idle_exit.read_receipt(alias,self.request,now=102))
    def test_missing_legacy_receipt_does_not_invent_a_draft(self):
        self.assertIsNone(idle_exit.read_receipt(self.path,self.request,now=102))
        text=idle_exit.presentation(idle_exit.unavailable())
        self.assertEqual(text['summary'],'无法确认退出条件。')
        self.assertNotIn('还有未保存',text['summary'])

    def test_optional_interface_sources_preserve_legacy_unknown_and_exact_flags(self):
        self.write()
        legacy=idle_exit.read_receipt(self.path,self.request,now=102)
        self.assertNotIn('interface_sources',legacy)
        for sources in ([], ['menu_open'], list(idle_exit.INTERFACE_SOURCES)):
            reasons=['unsaved']+(['interface_in_use'] if sources else [])
            value={**self.receipt,'reasons':reasons,'interface_sources':sources}
            self.write(value)
            self.assertEqual(idle_exit.read_receipt(self.path,self.request,now=102),value)
        safe={**self.receipt,'reasons':[],'can_exit':True,'interface_sources':[]}
        self.write(safe)
        self.assertEqual(idle_exit.read_receipt(self.path,self.request,now=102),safe)

    def test_interface_sources_reject_unknown_text_shape_duplicates_and_contradictions(self):
        valid={**self.receipt,'reasons':['interface_in_use'],'interface_sources':['menu_open']}
        invalid=[dict(interface_sources=None),dict(interface_sources='menu_open'),
                 dict(interface_sources=['private window title']),dict(interface_sources=[True]),
                 dict(interface_sources=[{}]),dict(interface_sources=['menu_open']*2),
                 dict(interface_sources=[]),dict(reasons=['unsaved']),
                 dict(reasons=[],can_exit=True),dict(window_title='private title')]
        for change in invalid:
            with self.subTest(change=change):
                self.write({**valid,**change})
                self.assertIsNone(idle_exit.read_receipt(self.path,self.request,now=102))

    def test_interface_explanation_uses_fixed_labels_without_inventing_legacy_sources(self):
        legacy=idle_exit.presentation(dict(can_exit=False,reasons=['interface_in_use']))
        self.assertEqual(legacy['summary'],idle_exit.REASONS['interface_in_use'][0])
        detail=idle_exit.presentation(dict(can_exit=False,reasons=['interface_in_use'],
                                         interface_sources=['menu_open','window','private title']))
        self.assertIn('展开的菜单',detail['summary'])
        self.assertIn('可见或最小化窗口',detail['summary'])
        self.assertNotIn('private title',str(detail))
        self.assertEqual(detail['next_trigger'],legacy['next_trigger'])

    def test_legacy_timeout_retains_publisher_check_time_and_request_identity(self):
        import plistlib
        from types import SimpleNamespace
        from subprocess import CompletedProcess
        app=self.path.parent/'PSI.app'; contents=app/'Contents'; contents.mkdir(parents=True)
        (contents/'Info.plist').write_bytes(plistlib.dumps({'PAMGitRevision':'legacy'}))
        store=SimpleNamespace(root=self.path.parent, active=lambda:True,
                              get=lambda key,default=None:{'policy':project_repair.POLICY,'paused':False})
        with patch.dict(project_repair.PROJECTS,{'agi':{'application':str(app)}}), \
             patch.object(project_repair,'app_busy',return_value=True), \
             patch.object(project_repair.subprocess,'run',return_value=CompletedProcess([],0,'1234\n','')), \
             patch.object(project_repair.time,'monotonic',side_effect=[0,0,4]), \
             patch.object(project_repair.time,'sleep'), patch.object(project_repair.time,'time',return_value=100):
            self.assertFalse(project_repair.prepare_idle_exit(store,'agi'))
        result=store._idle_exit_report
        self.assertEqual(result['receipt_source'],'publisher'); self.assertIsNone(result['instance'])
        self.assertEqual((result['pid'],result['revision'],result['request_created'],result['checked_at']),(1234,'legacy',100,100))
        self.assertEqual(str(uuid.UUID(result['nonce'])),result['nonce'])
        self.assertEqual(idle_exit.presentation(result)['checked_at'],100)
        self.assertEqual(result['reasons'],['state_unavailable']); self.assertFalse(result['can_exit'])
        self.write(result)
        self.assertIsNone(idle_exit.read_receipt(self.path,self.request,now=102))
        self.assertFalse((store.root/'ui-exit-request.json').exists())

class FormReceiptTests(unittest.TestCase):
    def setUp(self):
        from activity_mvp.storage import ensure_ready
        from activity_mvp.context_console import configure_context_console, CONTEXT_FEATURE_CONFIRMATION
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.conn=ensure_ready(Path(self.tmp.name)/"activity.sqlite");self.addCleanup(self.conn.close)
        configure_context_console(self.conn, enabled=True, confirmation=CONTEXT_FEATURE_CONFIRMATION)
    def test_manual_receipt_matches_exact_saved_form_without_writes_or_text(self):
        from activity_mvp.activity_confirmations import manual_add_confirmed_activity, MANUAL_CONFIRMATION_ACTION
        from datetime import datetime, timezone
        now=datetime.now(timezone.utc).isoformat(timespec='seconds')
        payload=dict(confirmation_action=MANUAL_CONFIRMATION_ACTION,safe_label='fixture safe note',workstream='fixture work',outcome='progressing',started_at=now,ended_at=now)
        self.assertEqual(manual_add_confirmed_activity(self.conn,**payload,receipt_only=True)['status'],'not_confirmed')
        saved=manual_add_confirmed_activity(self.conn,**payload)
        self.assertEqual(saved['status'],'recorded')
        changes=self.conn.total_changes
        result=manual_add_confirmed_activity(self.conn,**payload,receipt_only=True)
        self.assertEqual(result['status'],'confirmed')
        self.assertNotIn('fixture safe note',json.dumps(result))
        self.assertEqual(self.conn.total_changes,changes)
        payload['safe_label']='different edit'
        self.assertEqual(manual_add_confirmed_activity(self.conn,**payload,receipt_only=True)['status'],'not_confirmed')
    def test_both_sharing_consumers_read_exact_manual_note_without_replay(self):
        from activity_mvp import codex_bridge, agi_bridge
        from datetime import datetime, timezone
        for bridge in (codex_bridge,agi_bridge):
            payload=dict(summary='fixture summary '+bridge.__name__.split('.')[-1],project='fixture project '+bridge.__name__.split('.')[-1],workstream='fixture work',started_at=datetime.now(timezone.utc).isoformat(timespec='seconds'),request_id=str(uuid.uuid4()),confirm_owner=True)
            saved=bridge.owner_action(self.conn,'manual-note',payload)
            self.assertEqual(saved['status'],'confirmed')
            changes=self.conn.total_changes
            result=bridge.owner_action(self.conn,'manual-note-receipt',payload)
            self.assertEqual(result['status'],'confirmed')
            self.assertEqual(self.conn.total_changes,changes)
            self.assertNotIn('fixture summary',json.dumps(result))
            payload['summary']='different edit'
            self.assertEqual(bridge.owner_action(self.conn,'manual-note-receipt',payload)['status'],'not_confirmed')
    def test_manual_validation_rejects_before_writing_with_private_cli_receipt(self):
        import subprocess, sys
        from activity_mvp.activity_confirmations import MANUAL_CONFIRMATION_ACTION
        database = self.conn.execute("PRAGMA database_list").fetchone()[2]
        command=[sys.executable,"-m","activity_mvp","--db",database,"activity-confirmation","manual-add","--confirm",MANUAL_CONFIRMATION_ACTION,"--payload-stdin"]
        payload=dict(safe_label="fixture",workstream="fixture",outcome="unknown",started_at="invalid date")
        result=subprocess.run(command,input=json.dumps(payload),text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        value=json.loads(result.stdout)
        self.assertEqual(value["status"],"rejected")
        self.assertEqual(value["reason_code"],"invalid_manual_activity_input")
        self.assertNotIn("invalid date",result.stdout)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM activity_confirmation_items").fetchone()[0],0)

    def test_sharing_feedback_and_withdraw_receipts_are_exact_read_only_and_scoped(self):
        from activity_mvp import codex_bridge, agi_bridge
        from datetime import datetime, timezone
        for bridge in (codex_bridge, agi_bridge):
            with self.subTest(consumer=bridge.__name__):
                note=dict(summary='receipt fixture '+bridge.__name__.split('.')[-1],project='receipt project '+bridge.__name__.split('.')[-1],workstream='fixture work',started_at=datetime.now(timezone.utc).isoformat(timespec='seconds'),request_id=str(uuid.uuid4()),confirm_owner=True)
                saved=bridge.owner_action(self.conn,'manual-note',note)
                selection=dict(references=[saved['reference']],confirm_owner=True)
                self.assertEqual(bridge.owner_action(self.conn,'share-receipt',selection)['status'],'not_confirmed')
                shared=bridge.owner_action(self.conn,'share',selection)
                changes=self.conn.total_changes
                self.assertEqual(bridge.owner_action(self.conn,'share-receipt',selection)['status'],'confirmed')
                self.assertEqual(self.conn.total_changes,changes)
                feedback=dict(share_key=shared['share_key'],useful=True,resume_seconds=12,confirm_owner=True)
                self.assertEqual(bridge.owner_action(self.conn,'feedback-receipt',feedback)['status'],'not_confirmed')
                self.assertEqual(bridge.owner_action(self.conn,'feedback',feedback)['status'],'feedback_recorded')
                changes=self.conn.total_changes
                self.assertEqual(bridge.owner_action(self.conn,'feedback-receipt',feedback)['status'],'confirmed')
                self.assertEqual(bridge.owner_action(self.conn,'feedback-receipt',{**feedback,'resume_seconds':13})['status'],'not_confirmed')
                self.assertEqual(self.conn.total_changes,changes)
                withdrawal=dict(share_key=shared['share_key'],confirm_owner=True)
                self.assertEqual(bridge.owner_action(self.conn,'withdraw-receipt',withdrawal)['status'],'not_confirmed')
                self.assertEqual(bridge.owner_action(self.conn,'withdraw',withdrawal)['status'],'withdrawn')
                changes=self.conn.total_changes
                receipt=bridge.owner_action(self.conn,'withdraw-receipt',withdrawal)
                self.assertEqual(receipt['status'],'confirmed')
                other=agi_bridge if bridge is codex_bridge else codex_bridge
                self.assertEqual(other.owner_action(self.conn,'withdraw-receipt',withdrawal)['status'],'not_confirmed')
                self.assertEqual(self.conn.total_changes,changes)
                self.assertEqual(set(receipt),{'interface','status','raw_access'})

    def test_receipt_cli_routes_are_read_only(self):
        from activity_mvp.__main__ import _read_only_command, _phase2_context_read_command, _phase2_context_contract_version
        from argparse import Namespace
        for command,field,value in [('context-console','context_console_command','owner-action-receipt'),('activity-confirmation','activity_confirmation_command','manual-receipt'),('activity-confirmation','activity_confirmation_command','save-receipt'),('codex-sharing','action','feedback-receipt'),('agi-sharing','action','manual-note-receipt')]:
            args=Namespace(command=command,**{field:value})
            self.assertTrue(_read_only_command(args))
            if command=='context-console':
                self.assertTrue(_phase2_context_read_command(args))
                self.assertTrue(_phase2_context_contract_version(args))

    def test_sharing_cli_does_not_present_ambiguous_writes_as_safe_to_retry(self):
        import contextlib, io, sqlite3, sys
        from activity_mvp import __main__ as cli, codex_bridge
        database=self.conn.execute('PRAGMA database_list').fetchone()[2]
        for failure in (OSError('fixture'), sqlite3.OperationalError('fixture'), ValueError('fixture'), codex_bridge.OwnerActionRejected('fixture')):
            with self.subTest(kind=type(failure).__name__):
                output=io.StringIO()
                stdin=io.TextIOWrapper(io.BytesIO(json.dumps(dict(summary='private fixture body',confirm_owner=True)).encode()))
                with patch.object(sys,'argv',['activity_mvp','codex-sharing','--db',database,'--action','manual-note','--payload-stdin']), patch.object(sys,'stdin',stdin), patch.object(codex_bridge,'owner_action',side_effect=failure) as action, contextlib.redirect_stdout(output):
                    cli.main()
                result=json.loads(output.getvalue())
                expected='save_failed' if isinstance(failure,codex_bridge.OwnerActionRejected) else 'unknown'
                self.assertEqual(result['save_state'],expected)
                self.assertFalse(result['raw_access'])
                self.assertEqual(action.call_count,1)
                self.assertNotIn('private fixture body',output.getvalue())

    def test_target_independent_feedback_requires_an_existing_live_delivery(self):
        from activity_mvp import codex_bridge, context_console as context
        from datetime import datetime, timezone
        for condition in ('revoked', 'failed', 'no_delivery', 'target_policy_changed', 'bundle_withdrawn'):
            with self.subTest(condition=condition):
                note=dict(summary='authorization fixture '+condition,project='authorization project '+condition,workstream='fixture work',started_at=datetime.now(timezone.utc).isoformat(timespec='seconds'),request_id=str(uuid.uuid4()),confirm_owner=True)
                saved=codex_bridge.owner_action(self.conn,'manual-note',note)
                shared=codex_bridge.owner_action(self.conn,'share',dict(references=[saved['reference']],confirm_owner=True))
                delivery=codex_bridge._find_share(self.conn,shared['share_key'])
                if condition == 'revoked':
                    codex_bridge.owner_action(self.conn,'withdraw',dict(share_key=shared['share_key'],confirm_owner=True))
                elif condition == 'no_delivery':
                    self.conn.execute('DELETE FROM context_delivery_receipts WHERE delivery_ref=?',(delivery['delivery_ref'],))
                    self.conn.execute('DELETE FROM context_deliveries WHERE delivery_ref=?',(delivery['delivery_ref'],))
                elif condition == 'failed':
                    self.conn.execute("UPDATE context_deliveries SET state='failed' WHERE delivery_ref=?",(delivery['delivery_ref'],))
                elif condition == 'target_policy_changed':
                    self.conn.execute("UPDATE context_deliveries SET privacy_policy_hash='mismatch' WHERE delivery_ref=?",(delivery['delivery_ref'],))
                else:
                    self.conn.execute("UPDATE context_bundle_manifests SET status='revoked' WHERE bundle_ref=?",(delivery['bundle_ref'],))
                self.conn.commit()
                before=self.conn.total_changes
                counts=tuple(self.conn.execute('SELECT (SELECT COUNT(*) FROM context_product_feedback),(SELECT COUNT(*) FROM context_product_wins),(SELECT COUNT(*) FROM context_product_experiment_attempts)').fetchone())
                result=context.record_product_feedback(self.conn,json.dumps(dict(contract_version=context.PRODUCT_FEEDBACK_VERSION,idempotency_key='idem_'+uuid.uuid4().hex,source_kind='bundle',source_ref=delivery['bundle_ref'],project_ref=delivery['project_ref'],useful=True)))
                self.assertEqual(result['status'],'rejected')
                self.assertEqual(self.conn.total_changes,before)
                self.assertEqual(tuple(self.conn.execute('SELECT (SELECT COUNT(*) FROM context_product_feedback),(SELECT COUNT(*) FROM context_product_wins),(SELECT COUNT(*) FROM context_product_experiment_attempts)').fetchone()),counts)

    def test_shared_note_committed_before_lost_response_is_reconciled_without_replay(self):
        import contextlib, io, sys
        from datetime import datetime, timezone
        from activity_mvp import __main__ as cli, codex_bridge
        database=self.conn.execute('PRAGMA database_list').fetchone()[2]
        payload=dict(summary='private receipt-loss fixture',project='receipt-loss project',workstream='fixture work',started_at=datetime.now(timezone.utc).isoformat(timespec='seconds'),request_id=str(uuid.uuid4()),confirm_owner=True)
        self.assertEqual(codex_bridge.owner_action(self.conn,'manual-note-receipt',payload)['status'],'not_confirmed')
        original=codex_bridge.owner_action
        def commit_then_fail(conn, action, body):
            self.assertEqual(original(conn,action,body)['status'],'confirmed')
            raise OSError('response lost after commit')
        output=io.StringIO()
        stdin=io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()))
        with patch.object(sys,'argv',['activity_mvp','codex-sharing','--db',database,'--action','manual-note','--payload-stdin']), patch.object(sys,'stdin',stdin), patch.object(codex_bridge,'owner_action',side_effect=commit_then_fail) as action, contextlib.redirect_stdout(output):
            cli.main()
        self.assertEqual(action.call_count,1)
        self.assertEqual(json.loads(output.getvalue())['save_state'],'unknown')
        before=self.conn.total_changes
        self.assertEqual(original(self.conn,'manual-note-receipt',payload)['status'],'confirmed')
        self.assertEqual(self.conn.total_changes,before)
        self.assertNotIn(payload['summary'],output.getvalue())

class ClarificationReceiptTests(unittest.TestCase):
    def test_readback_survives_rewrite_split_filter_and_cleaned_notes(self):
        from tests import test_activity_confirmations as fixtures
        from activity_mvp.activity_confirmations import clarify_activity_confirmation_group, saved_form_receipt
        cases = ["test_clarify_rewrites_from_unique_safe_context_without_answer_side_effect",
                 "test_clarify_splits_only_when_each_existing_member_maps_to_one_subject",
                 "test_clarify_game_context_note_filters_without_answer_or_calibration",
                 "test_second_clarify_persists_latest_cleaned_note_without_answer_side_effects"]
        for name in cases:
            with self.subTest(name=name):
                fixture = fixtures.ActivityConfirmationTests(name)
                fixture.setUp()
                def checked(conn, **kwargs):
                    kwargs["save_request"] = str(uuid.uuid4())
                    result = clarify_activity_confirmation_group(conn, **kwargs)
                    if result["status"] == "clarified":
                        changes = conn.total_changes
                        receipt = saved_form_receipt(conn, dict(purpose="clarification", **kwargs))
                        self.assertEqual(receipt["status"], "confirmed")
                        self.assertEqual(conn.total_changes, changes)
                        import subprocess, sys
                        database = conn.execute("PRAGMA database_list").fetchone()[2]
                        cli = subprocess.run([sys.executable,"-m","activity_mvp","--db",database,"activity-confirmation","save-receipt","--payload-stdin"], input=json.dumps(dict(purpose="clarification",**kwargs)), text=True,capture_output=True)
                        self.assertEqual(cli.returncode,0,cli.stderr)
                        self.assertEqual(json.loads(cli.stdout)["status"],"confirmed")
                        self.assertNotIn("save_key", json.dumps(receipt))
                        self.assertEqual(saved_form_receipt(conn, dict(purpose="clarification", **{**kwargs, "save_request":str(uuid.uuid4())}))["status"], "not_confirmed")
                        self.assertEqual(saved_form_receipt(conn, dict(purpose="clarification", **{**kwargs, "note":"different note"}))["status"], "not_confirmed")
                    return result
                try:
                    with patch.object(fixtures, "clarify_activity_confirmation_group", checked):
                        getattr(fixture, name)()
                finally:
                    fixture.tearDown()

    def test_answer_readback_uses_the_write_normalization(self):
        from tests.test_activity_confirmations import ActivityConfirmationTests
        from activity_mvp.activity_confirmations import answer_activity_confirmation_group, saved_form_receipt
        for answer in ("occurred", "defer", "ignored"):
            with self.subTest(answer=answer):
                fixture = ActivityConfirmationTests()
                fixture.setUp()
                try:
                    fixture._new_item("今天完成浩方二面", at=fixture.today.replace(hour=9), app="ChatGPT")
                    group = fixture._materialize()["question_groups"][0]
                    payload = dict(group_key=group["group_key"], member_revision=group["member_revision"], answer=answer, workstream="fixture", outcome="unknown", note="")
                    result = answer_activity_confirmation_group(fixture.conn, **payload)
                    self.assertEqual(result["status"], "recorded")
                    self.assertEqual(saved_form_receipt(fixture.conn, dict(purpose="answer", **payload))["status"], "confirmed")
                finally:
                    fixture.tearDown()

if __name__ == '__main__': unittest.main()
