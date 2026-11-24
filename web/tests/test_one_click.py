import json
from unittest.mock import patch
from test_sync_center import Base
from psi_sync import handoff
from psi_sync.core_reader import ReadError

class OneClickTests(Base):
    def setUp(self):
        super().setUp()
        self.item = self.state.detail('demo-event')
        self.item.update(native_revision='7',codex_thread_id=None)
        self.state.detail = lambda _: self.item
        self.store_source=self.reader.cache_identity
        self.reader.fixture = None
        self.calls=[]
    def call(self, config, action, identifier, payload=None):
        self.calls.append((action,payload))
        return {'status':'ok','handoff':None} if action=='handoff-status' else {'status':'ok','handoff':{'status':'queued','receipt':None}}
    def run_click(self, content='请核对'):
        return handoff.continue_in_codex(self.state,'demo-event',{'revision':self.item['revision'],'content':content})
    def test_single_click_submits_and_repeat_never_replays(self):
        with patch.object(handoff,'core_command',side_effect=self.call):
            self.run_click(); self.run_click()
        self.assertEqual([a for a,p in self.calls].count('handoff-submit'),1)
        payload=next(p for a,p in self.calls if a=='handoff-submit')
        self.assertEqual(payload['issue_revision'],'7')
    def test_unknown_outcome_survives_restart_without_resubmit(self):
        def uncertain(config,action,identifier,payload=None):
            if action=='handoff-submit': raise ReadError('timeout')
            return self.call(config,action,identifier,payload)
        with patch.object(handoff,'core_command',side_effect=uncertain):
            result=self.run_click()
        self.assertEqual(result['phase'],'uncertain')
        from psi_sync.storage import LocalStore
        # Read the exact persisted original namespace after restart.
        self.state.store=LocalStore(self.directory, self.store_source)
        with patch.object(handoff,'core_command',side_effect=self.call):
            result=self.run_click()
        self.assertEqual(result['phase'],'uncertain')
        self.assertNotIn('handoff-submit',[a for a,p in self.calls])
    def test_existing_closure_only_navigates(self):
        self.item.update(kind='closure',codex_thread_id='12345678-0000-4000-8000-000000000003')
        with patch.object(handoff,'core_command',return_value={'status':'ok','navigation_requested':True}) as invoke:
            result=handoff.continue_in_codex(self.state,'closure:sample',{'revision':self.item['revision']})
        self.assertEqual(invoke.call_args.args[1],'closure-open')
        self.assertEqual(invoke.call_args.args[3]['revision'],'7')
        self.assertEqual(result['phase'],'opened')
        with patch.object(handoff,'core_command',return_value={'status':'ok','navigation_requested':True}) as invoke:
            result = self.run_click('旧后台下应保留的草稿')
        self.assertEqual(invoke.call_args.args[1], 'closure-open')
        self.assertIn('无法从这里发送回复', result['message'])
    def test_stale_revision_never_calls_core(self):
        with patch.object(handoff,'core_command') as invoke:
            with self.assertRaises(ReadError):handoff.continue_in_codex(self.state,'demo-event',{'revision':'stale'})
        invoke.assert_not_called()
    def test_fixture_never_reaches_live_core(self):
        self.reader.fixture=True
        with patch.object(handoff,'core_command') as invoke:
            result=self.run_click()
        self.assertEqual(result['phase'],'unavailable');invoke.assert_not_called()

    def test_explicit_rejection_with_empty_known_history_allows_corrected_click(self):
        def rejected(config,action,identifier,payload=None):
            self.calls.append((action,payload))
            if action=='handoff-submit':return {'status':'unavailable','error':'补充中含有不适合直接转交的私密信息'}
            return {'status':'ok','handoff':None,'history_unknown':False}
        with patch.object(handoff,'core_command',side_effect=rejected):
            self.run_click();self.run_click()
        self.assertEqual([a for a,p in self.calls].count('handoff-submit'),2)
    def test_delivered_repeat_opens_without_sending(self):
        def delivered(config,action,identifier,payload=None):
            self.calls.append((action,payload))
            return {'status':'ok','handoff':{'status':'delivered','thread_id':'test','content':'请核对','receipt':{'id':'verified'}}}
        with patch.object(handoff,'core_command',side_effect=delivered):
            self.assertEqual(self.run_click()['phase'],'opened')
        self.assertEqual([a for a,p in self.calls],['handoff-status','handoff-open'])

    def test_independent_closure_reply_uses_core_conversation_permission(self):
        self.item.update(kind='closure', codex_handoff_available=False, codex_reply_available=True)
        with patch.object(handoff,'core_command',side_effect=self.call):
            self.assertEqual(self.run_click('请讨论备份范围')['phase'], 'pending')
        action, payload = self.calls[-1]
        self.assertEqual(action, 'handoff-submit')
        self.assertTrue(payload['workbench_reply'])
        self.assertEqual(payload['content'], '请讨论备份范围')

    def test_later_reply_is_sent_even_when_conversation_already_exists(self):
        self.item.update(kind='closure', codex_handoff_available=False, codex_reply_available=True,
                         codex_thread_id='12345678-0000-4000-8000-000000000003')
        original = {'status':'ok','handoff':{'status':'delivered','submission_id':'first',
                                            'content':'第一条回复','receipt':{'id':'verified'}}}
        with patch.object(handoff,'core_command',side_effect=[original, {'status':'ok','handoff':{'status':'queued'}}]) as invoke:
            self.assertEqual(self.run_click('第二条回复')['phase'], 'pending')
        self.assertEqual(invoke.call_args.args[1], 'handoff-submit')
        self.assertEqual(invoke.call_args.args[3]['content'], '第二条回复')

    def test_created_but_unsent_closure_retries_original_instead_of_empty_open(self):
        self.item.update(kind='closure',codex_thread_id='12345678-0000-4000-8000-000000000003')
        def failed(config,action,identifier,payload=None):
            self.calls.append((action,payload))
            return {'status':'ok','handoff':{'status':'failed','thread_id':self.item['codex_thread_id'],'submission_id':'original','receipt':None}}
        with patch.object(handoff,'core_command',side_effect=failed):self.run_click()
        self.assertEqual([a for a,p in self.calls],['handoff-status','handoff-retry'])
        self.assertEqual(self.calls[-1][1],{'submission_id':'original'})

    def test_prior_receipt_does_not_claim_delivery_of_different_reply(self):
        current = {'submission_id':'first','status':'uncertain','content':'第一条'}
        delivered = {**current, 'status':'delivered', 'receipt':{'id':'verified'}}
        with patch.object(handoff, 'core_command', side_effect=[
            {'status':'ok','handoff':current}, {'status':'ok','handoff':delivered},
        ]):
            response = self.run_click('第二条')
        self.assertEqual(response['phase'], 'prior_reply')
        self.assertIn('新回复已保留', response['message'])

    def test_action_content_never_overwrites_editor_draft(self):
        self.store.update(lambda data:data['drafts'].__setitem__('demo-event','未发送的补充'))
        with patch.object(handoff,'core_command',side_effect=self.call):self.run_click()
        self.assertEqual(self.store.read()['drafts']['demo-event'],'未发送的补充')

    def test_queued_worker_recovery_reuses_exact_saved_intent(self):
        intent={'client_message_id':'same-id','content':'原补充','issue_revision':'7'}
        self.store.update(lambda data:data.setdefault('handoffs',{}).__setitem__('demo-event',intent))
        def queued(config,action,identifier,payload=None):
            self.calls.append((action,payload))
            return {'status':'ok','handoff':{'status':'queued','submission_id':'same-id'}}
        with patch.object(handoff,'core_command',side_effect=queued):self.run_click()
        self.assertEqual(self.calls[-1],('handoff-submit',intent))
