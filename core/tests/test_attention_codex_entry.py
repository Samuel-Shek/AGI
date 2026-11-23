from tests.closure_legacy_fixture import legacy_begin
"""Isolated semantic checks for closure navigation and explicit user handoff."""
import json,sqlite3,tempfile,unittest,uuid
from pathlib import Path
from unittest.mock import MagicMock,call,patch
from activity_mvp import attention_center as center,attention_handoff as h,attention_native as native
from activity_mvp.task_closure import Store,projection
class CodexEntryTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve();self.db=self.root/'activity.sqlite';sqlite3.connect(self.db).close()
  self.source=self.root/'evidence.txt';self.source.write_text('isolated evidence')
  self.store=Store(self.db);self.queue=h.Queue(self.db);self.thread=str(uuid.uuid4())
 def tearDown(self):self.store.conn.close();self.tmp.cleanup()
 def issue(self,platform='codex',state='decision',category='main'):
  task=legacy_begin(self.store, {'platform':platform,'session':self.thread,'turn':str(uuid.uuid4()),'objective':'isolated task','scope_root':str(self.root),'complex':True})['task_id']
  item=self.store.register({'task_id':task,'category':category,'key':str(uuid.uuid4()),'summary':'isolated follow-up','target_root':str(self.root),'evidence':[{'path':str(self.source)}],'authority':{'source':'isolated UI test','scope':str(self.root),'reversible':True,'cost':False,'new_permission':False,'external':False,'personal_choice':True},'recommendation':'wait for user','acceptance_rule':'verified evidence'})['issue']
  self.store.conn.execute('UPDATE closure_issues SET state=? WHERE id=?',(state,item['id']));return self.store.get(item['id'])
 def projected(self,item):return next(v for v in projection(self.db) if v['id']=='closure:'+item['id'])
 def submit(self,item,message=None,**reply):
  projected=self.projected(item)
  payload={'content':'explain current state','client_message_id':message or str(uuid.uuid4()),'issue_revision':str(item['revision']),**reply}
  with patch.object(h,'detail',return_value={'item':projected}):return self.queue.submit(projected['id'],payload)
 def row(self):
  with self.queue.db() as db:return dict(db.execute('SELECT s.*,p.thread_id,p.project_id,p.title,p.creation_state FROM submissions s JOIN problems p ON p.id=s.problem_id').fetchone())
 def test_main_codex_owner_is_a_navigation_target_without_rewriting_original(self):
  item=self.issue();before=self.store.get(item['id']);v=self.projected(item)
  self.assertEqual(v.get('codex_thread_id'),self.thread);self.assertIsNone(v['thread_id']);self.assertEqual(before,self.store.get(item['id']))
 def test_dispatched_chat_wins_and_foreign_owner_uuid_is_not_codex(self):
  item=self.issue('workbuddy');self.assertIsNone(self.projected(item).get('codex_thread_id'));self.store.conn.execute('UPDATE closure_issues SET thread_id=? WHERE id=?',(self.thread,item['id']));self.assertEqual(self.projected(item).get('codex_thread_id'),self.thread)
 def test_malformed_explicit_thread_never_falls_through_to_owner(self):
  item=self.issue();self.store.conn.execute('UPDATE closure_issues SET thread_id=? WHERE id=?',('bad?prompt=run',item['id']));self.assertIsNone(self.projected(item).get('codex_thread_id'))
 def test_existing_codex_owner_cannot_create_second_handoff(self):
  item=self.issue()
  with self.assertRaises(h.HandoffError):self.submit(item)
  with self.queue.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM submissions').fetchone()[0],0)
 def test_foreign_owner_explicit_handoff_binds_project_and_preserves_choice(self):
  item=self.issue('workbuddy');before=self.store.get(item['id']);message=str(uuid.uuid4());self.submit(item,message);self.submit(item,message);row=self.row()
  self.assertEqual(json.loads(row['source'])['closure_target'],{'root':str(self.root),'project_id':None});self.assertIn('只读核查',row['prompt']);self.assertIn('不得替用户同意建议',row['prompt']);self.assertEqual(before,self.store.get(item['id']))
  with self.queue.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM submissions').fetchone()[0],1)
 def test_workbench_reply_discusses_independent_decision_without_dispatching(self):
  item=self.issue('claude',category='independent')
  self.assertFalse(self.projected(item)['codex_handoff_available'])
  self.assertTrue(self.projected(item)['codex_reply_available'])
  with self.assertRaises(h.HandoffError):self.submit(item)
  self.submit(item,workbench_reply=True,content='先整理备份清单。\n迁移留待讨论。')
  row=self.row()
  self.assertIn('请直接回应用户这次说的话',row['prompt'])
  self.assertIn('先整理备份清单。\n迁移留待讨论。',row['prompt'])
  self.assertIn(item['summary'],row['prompt'])
  self.assertEqual(self.queue.target_for(row,sending=True)['root'],str(self.root))
  self.assertEqual(self.store.get(item['id']),item)
  self.store.conn.execute("UPDATE closure_issues SET dispatch_state='send_uncertain' WHERE id=?", (item['id'],))
  with self.assertRaises(h.HandoffError):self.queue.target_for(row,sending=True)
 def test_workbench_reply_does_not_name_new_chat_with_private_title_fragment(self):
  item=self.issue('claude',category='independent')
  self.store.conn.execute("UPDATE closure_issues SET summary=? WHERE id=?", ('Follow up synthetic@example.test',item['id']))
  self.submit(self.store.get(item['id']),workbench_reply=True)
  row=self.row(); server=MagicMock(); server.project.return_value=None
  server.create.side_effect=lambda project,name,created:created(self.thread)
  server.read.return_value={'projectId':None,'cwd':str(self.root),'turns':[{}]}
  with patch.object(native,'AppServer') as app,patch.object(native,'receipt_in_thread',return_value={'turn_id':'reply'}):
   app.return_value.__enter__.return_value=server;self.queue.process(row['id'])
  self.assertIn('[email_redacted]',server.create.call_args.args[1])
  self.assertNotIn('synthetic@example.test',row['prompt'])
 def test_workbench_reply_uses_bound_codex_thread_and_preserves_issue(self):
  item=self.issue()
  self.submit(item,workbench_reply=True,content='请回应这一条')
  row=self.row(); server=MagicMock(); server.project.return_value=None
  server.read.return_value={'id':self.thread,'projectId':None,'cwd':str(self.root),'turns':[{}]}
  ipc=MagicMock(); ipc.owner.return_value='desktop'; ipc.submit.return_value={'received':True}
  with patch.object(native,'AppServer') as app,patch.object(native,'open_thread'),patch.object(native,'wait_owner'),patch.object(native,'DesktopIPC') as channel,patch.object(native,'receipt_in_thread',side_effect=[None,{'turn_id':'reply'}]):
   app.return_value.__enter__.return_value=server;channel.return_value.__enter__.return_value=ipc;self.queue.process(row['id'])
  server.create.assert_not_called()
  self.assertEqual(ipc.submit.call_args.args[:3],(self.thread,'desktop',row['id']))
  self.assertIn('请回应这一条',ipc.submit.call_args.args[3])
  self.assertEqual(self.store.get(item['id']),item)
 def test_cancel_or_revision_change_blocks_creation_and_send_but_receipt_is_readable(self):
  item=self.issue('workbuddy');self.submit(item);row=self.row();self.store.conn.execute('UPDATE closure_issues SET state=?,revision=revision+1 WHERE id=?',('canceled',item['id']))
  with self.assertRaises(h.HandoffError):self.queue.target_for(row,sending=True)
  self.assertEqual(self.queue.target_for(row),{'root':str(self.root),'project_id':None})
  with self.assertRaises(h.HandoffError):self.submit(self.store.get(item['id']))
 def test_changed_project_blocks_original_transport(self):
  item=self.issue('workbuddy');self.submit(item);row=self.row();self.store.conn.execute('UPDATE closure_issues SET project_id=? WHERE id=?',('changed-project',item['id']))
  with self.assertRaises(h.HandoffError):self.queue.target_for(row,sending=True)
 def test_real_queue_transport_uses_original_projectless_target(self):
  item=self.issue('workbuddy');self.submit(item);row=self.row();thread=str(uuid.uuid4());server=MagicMock();server.project.return_value=None
  server.read.return_value={'projectId':None,'cwd':str(self.root),'turns':[]}
  server.create.side_effect=lambda project,name,created:created(thread)
  ipc=MagicMock();ipc.owner.return_value='desktop';ipc.submit.return_value={'received':True}
  with patch.object(native,'AppServer') as factory,patch.object(native,'open_thread'),patch.object(native,'wait_owner'),patch.object(native,'DesktopIPC') as channel,patch.object(native,'receipt_in_thread',side_effect=[None,{'turn_id':'t'}]):
   factory.return_value.__enter__.return_value=server;channel.return_value.__enter__.return_value=ipc;self.queue.process(row['id'])
  self.assertEqual(factory.call_args.kwargs['target'],{'root':str(self.root),'project_id':None});self.assertEqual(server.create.call_count,1);self.assertEqual(ipc.submit.call_count,1);self.assertEqual(self.queue.status('closure:'+item['id'])['handoff']['status'],'delivered');self.assertEqual(self.store.get(item['id'])['state'],'decision')
 def test_unknown_original_creation_is_not_a_second_handoff(self):
  item=self.issue('workbuddy');self.store.conn.execute('UPDATE closure_issues SET dispatch_state=? WHERE id=?',('creation_uncertain',item['id']))
  self.assertFalse(self.projected(item)['codex_handoff_available'])
  with self.assertRaises(h.HandoffError):self.submit(item)
 def test_saved_helper_thread_becomes_direct_navigation_only_for_its_project(self):
  item=self.issue('workbuddy');self.submit(item);thread=str(uuid.uuid4())
  with self.queue.db() as db:db.execute('UPDATE problems SET thread_id=?,creation_state=?',(thread,'ready'))
  self.assertEqual(self.projected(item)['codex_thread_id'],thread);self.assertFalse(self.projected(item)['codex_handoff_available']);self.assertTrue(self.projected(item)['codex_open_via_handoff'])
  self.store.conn.execute('UPDATE closure_issues SET project_id=? WHERE id=?',('different-project',item['id']))
  self.assertIsNone(self.projected(item)['codex_thread_id'])
 def test_resolution_needs_current_independent_acceptance(self):
  from activity_mvp.attention_center import verify_resolution
  item=self.issue('workbuddy',state='verification');source={'kind':'closure','id':'closure:'+item['id']}
  with sqlite3.connect(self.db) as conn:self.assertEqual(verify_resolution(conn,source)['state'],'pending')
  self.store.accept({'issue_id':item['id'],'revision':item['revision'],'verified':True,'reviewer':'independent isolated reviewer','review':'read actual evidence','evidence':[{'path':str(self.source)}]})
  with sqlite3.connect(self.db) as conn:self.assertEqual(verify_resolution(conn,source)['state'],'resolved')
  self.source.write_text('changed after acceptance')
  with sqlite3.connect(self.db) as conn:self.assertEqual(verify_resolution(conn,source)['state'],'pending')
 def test_fixed_independent_dispatcher_cannot_race_helper_creation(self):
  item=self.issue('workbuddy');self.store.conn.execute('UPDATE closure_issues SET category=? WHERE id=?',('independent',item['id']))
  self.assertFalse(self.projected(item)['codex_handoff_available'])
  with self.assertRaises(h.HandoffError):self.submit(item)
 def open_current(self,item):
  from activity_mvp.attention_center import open_closure_codex
  with sqlite3.connect(self.db) as conn:
   return open_closure_codex(conn,'closure:'+item['id'],{'revision':str(item['revision']),'thread_id':self.thread})
 def test_focused_detail_skips_unrelated_workspace_and_evidence(self):
  from activity_mvp.attention_center import detail
  item=self.issue();before=self.store.get(item['id'])
  with patch('activity_mvp.attention_center.snapshot',side_effect=AssertionError('full workspace read')):
   with sqlite3.connect(self.db) as conn:self.assertEqual(detail(conn,'closure:'+item['id'])['item']['codex_thread_id'],self.thread)
  self.assertEqual(before,self.store.get(item['id']))
 def test_original_navigation_reads_metadata_and_never_starts_a_turn(self):
  item=self.issue();before=self.store.get(item['id']);server=MagicMock();server.call.return_value={'thread':{'id':self.thread}}
  settings={'model':'gpt-6.2-sol','reasoning_effort':'xhigh'}
  server.initial_collaboration_mode.return_value={'settings':settings}
  with patch.object(native,'AppServer') as factory,patch.object(center,'open_existing_codex_thread') as opened:
   factory.return_value.__enter__.return_value=server;result=self.open_current(item)
  self.assertTrue(result['navigation_requested']);opened.assert_called_once_with(self.thread,settings)
  factory.assert_called_once_with(max_duration=10)
  server.call.assert_called_once_with('thread/read',{'threadId':self.thread,'includeTurns':False},timeout=5)
  self.assertEqual(before,self.store.get(item['id']))
 def test_missing_original_is_visible_failure_without_open_or_replacement(self):
  item=self.issue();server=MagicMock();server.call.side_effect=native.NativeError('codex_rpc_rejected','thread not loaded')
  with patch.object(native,'AppServer') as factory,patch.object(native,'open_thread') as opened:
   factory.return_value.__enter__.return_value=server;result=self.open_current(item)
  self.assertEqual(result['status'],'unavailable');self.assertIn('事项仍保留',result['error']);opened.assert_not_called()
  with self.queue.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM submissions').fetchone()[0],0)
 def test_changed_owner_during_read_invalidates_navigation(self):
  item=self.issue();server=MagicMock()
  def change(*args,**kwargs):
   self.store.conn.execute('UPDATE closure_issues SET owner=?,revision=revision+1 WHERE id=?',('codex:'+str(uuid.uuid4()),item['id']))
   return {'thread':{'id':self.thread}}
  server.call.side_effect=change
  with patch.object(native,'AppServer') as factory,patch.object(native,'open_thread') as opened:
   factory.return_value.__enter__.return_value=server
   with self.assertRaises(ValueError):self.open_current(item)
  opened.assert_not_called()
 def test_forged_click_target_is_rejected_before_native_calls(self):
  from activity_mvp.attention_center import open_closure_codex
  item=self.issue()
  with patch.object(native,'AppServer') as factory:
   with sqlite3.connect(self.db) as conn:
    with self.assertRaises(ValueError):open_closure_codex(conn,'closure:'+item['id'],{'revision':str(item['revision']),'thread_id':str(uuid.uuid4())})
  factory.assert_not_called()
 def test_existing_navigation_updates_only_preferences_and_checks_live_readback(self):
  settings={'model':'gpt-6.2-sol','reasoning_effort':'xhigh'}
  ipc=MagicMock()
  ipc.call.side_effect=[
   {'result':{'settings':{'model':'gpt-6.1-sol','reasoningEffort':'max'}}},
   {'result':{'applied':True}},
   {'result':{'settings':{'model':'gpt-6.2-sol','reasoningEffort':'xhigh'}}},
  ]
  with patch.object(native,'DesktopIPC') as channel, patch.object(native,'wait_owner',return_value='desktop'), patch.object(native,'open_thread') as opened:
   channel.return_value.__enter__.return_value=ipc
   center.open_existing_codex_thread(self.thread,settings)
  opened.assert_called_once_with(self.thread,timeout=3)
  self.assertEqual(ipc.call.call_args_list[1], call('thread-follower-update-thread-settings',{
   'conversationId':self.thread,
   'threadSettings':{'model':'gpt-6.2-sol','effort':'xhigh'},
   'condition':{'ifModelEquals':'gpt-6.1-sol','ifEffortEquals':'max'},
  },version=2,target='desktop',timeout=2))
  ipc.submit.assert_not_called()
  self.assertEqual([record.args[0] for record in ipc.call.call_args_list], [
   'thread-follower-read-model-settings', 'thread-follower-update-thread-settings', 'thread-follower-read-model-settings',
  ])

 def test_existing_navigation_keeps_navigation_and_reports_unverified_settings(self):
  item=self.issue()
  server=MagicMock()
  server.call.return_value={'thread':{'id':self.thread}}
  error=native.NativeError('codex_model_settings_unverified','unverified')
  with patch.object(native,'AppServer') as factory,patch.object(center,'open_existing_codex_thread',side_effect=error):
   factory.return_value.__enter__.return_value=server
   result=self.open_current(item)
  self.assertEqual(result,{'status':'unavailable','navigation_requested':True,
                           'error':'unverified','error_code':'codex_model_settings_unverified'})

 def test_navigation_uses_exact_app_and_surfaces_launch_failure(self):
  import subprocess
  with patch.object(native.APP_PATH.__class__,'is_dir',return_value=True),patch.object(native.subprocess,'run') as run:
   native.open_thread(self.thread);self.assertEqual(run.call_args.args[0],['/usr/bin/open','-a',str(native.APP_PATH),'codex://threads/'+self.thread])
   run.side_effect=subprocess.TimeoutExpired('open',10)
   with self.assertRaises(native.NativeError):native.open_thread(self.thread)
 def test_helper_cannot_certify_another_thread_in_the_same_project(self):
  item=self.issue('workbuddy');self.submit(item)
  with self.queue.db() as db:db.execute('UPDATE problems SET thread_id=?,creation_state=?',(self.thread,'ready'))
  server=MagicMock();server.call.return_value={'thread':{'id':str(uuid.uuid4()),'projectId':None,'cwd':str(self.root)}};server.project.return_value=None
  with patch.object(native,'AppServer') as factory,patch.object(native,'open_thread') as opened:
   factory.return_value.__enter__.return_value=server;result=self.open_current(item)
  self.assertEqual(result['status'],'unavailable');self.assertEqual(result['error_code'],'codex_thread_mismatch');opened.assert_not_called()
 def metadata_missing(self):
  server=MagicMock();server.call.side_effect=native.NativeError('codex_rpc_rejected','thread not loaded: '+self.thread,rpc_error={'code':-32600,'message':'thread not loaded: '+self.thread})
  return server
 def assistance_submit(self,item,message=None):
  receipt=self.queue.record_navigation_failure('closure:'+item['id'],str(item['revision']),self.thread)
  v=self.projected(item)
  with patch.object(h,'detail',return_value={'item':v}),patch.object(native,'AppServer') as factory:
   factory.return_value.__enter__.return_value=self.metadata_missing()
   return self.queue.submit(v['id'],{'content':'explain only','client_message_id':message or str(uuid.uuid4()),'issue_revision':str(item['revision']),'assistance_token':receipt['token'],'origin_thread_id':self.thread})
 def test_exact_missing_metadata_offers_explicit_assistance_without_changing_original(self):
  item=self.issue();before=self.store.get(item['id'])
  with patch.object(native,'AppServer') as factory,patch.object(native,'open_thread') as opened:
   factory.return_value.__enter__.return_value=self.metadata_missing();r=self.open_current(item)
  self.assertTrue(r['assistance_available']);self.assertTrue(r['item']['codex_assistance_available']);opened.assert_not_called();self.assertEqual(before,self.store.get(item['id']))
  with self.queue.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM submissions').fetchone()[0],0)
 def test_communication_failure_never_enables_assistance(self):
  item=self.issue();server=MagicMock();server.call.side_effect=native.NativeError('codex_rpc_timeout')
  with patch.object(native,'AppServer') as factory:
   factory.return_value.__enter__.return_value=server;r=self.open_current(item)
  self.assertFalse(r['assistance_available']);self.assertFalse(self.projected(item)['codex_assistance_available'])
 def test_legacy_closure_open_without_current_binding_is_rejected(self):
  item=self.issue()
  with patch.object(native,'AppServer') as factory,patch.object(native,'open_thread') as opened:
   for payload in ({},{'revision':str(item['revision'])},{'thread_id':self.thread}):
    with self.assertRaises(h.HandoffError):h.command(self.db,'handoff-open','closure:'+item['id'],payload)
  factory.assert_not_called();opened.assert_not_called()
 def test_initialization_failure_is_not_a_failed_thread_read(self):
  item=self.issue();error=native.NativeError('codex_rpc_rejected','thread not loaded: '+self.thread,rpc_error={'code':-32600,'message':'thread not loaded: '+self.thread})
  with patch.object(native,'AppServer') as factory:
   factory.return_value.__enter__.side_effect=error;r=self.open_current(item)
  self.assertFalse(r['assistance_available'])
  receipt=self.queue.record_navigation_failure('closure:'+item['id'],str(item['revision']),self.thread)
  with patch.object(native,'AppServer') as factory:
   factory.return_value.__enter__.side_effect=error
   with self.assertRaises(h.HandoffError):self.queue.assistance_source(item,{'assistance_token':receipt['token'],'origin_thread_id':self.thread},check_current=True)
 def test_assistance_rechecks_original_and_rejects_restored_chat(self):
  item=self.issue();receipt=self.queue.record_navigation_failure('closure:'+item['id'],str(item['revision']),self.thread)
  with patch.object(native,'AppServer') as factory:
   factory.return_value.__enter__.return_value=MagicMock()
   with self.assertRaises(h.HandoffError):self.queue.assistance_source(item,{'assistance_token':receipt['token'],'origin_thread_id':self.thread},check_current=True)
  with self.queue.db() as db:self.assertEqual(db.execute('SELECT count(*) FROM submissions').fetchone()[0],0)
 def test_assistance_old_receipt_does_not_override_current_rpc_timeout(self):
  item=self.issue();receipt=self.queue.record_navigation_failure('closure:'+item['id'],str(item['revision']),self.thread);server=MagicMock();server.call.side_effect=native.NativeError('codex_rpc_timeout')
  with patch.object(native,'AppServer') as factory:
   factory.return_value.__enter__.return_value=server
   with self.assertRaises(h.HandoffError):self.queue.assistance_source(item,{'assistance_token':receipt['token'],'origin_thread_id':self.thread},check_current=True)
 def test_assistance_wrong_token_expiry_and_revision_fail_closed(self):
  import time
  item=self.issue();receipt=self.queue.record_navigation_failure('closure:'+item['id'],str(item['revision']),self.thread)
  with self.assertRaises(h.HandoffError):self.queue.assistance_source(item,{'assistance_token':str(uuid.uuid4()),'origin_thread_id':self.thread})
  path=self.queue.root/('navigation-'+item['id']+'.json');receipt['checked_at']=time.time()-86401;path.write_text(json.dumps(receipt))
  self.assertFalse(self.projected(item)['codex_assistance_available'])
  with self.assertRaises(h.HandoffError):self.queue.assistance_source(item,{'assistance_token':receipt['token'],'origin_thread_id':self.thread})
  self.queue.record_navigation_failure('closure:'+item['id'],str(item['revision']),self.thread);self.store.conn.execute('UPDATE closure_issues SET revision=revision+1 WHERE id=?',(item['id'],));self.assertFalse(self.projected(self.store.get(item['id']))['codex_assistance_available'])
 def test_assistance_unsafe_receipt_permissions_and_symlink_are_rejected(self):
  item=self.issue();self.queue.record_navigation_failure('closure:'+item['id'],str(item['revision']),self.thread);path=self.queue.root/('navigation-'+item['id']+'.json')
  path.chmod(0o644);self.assertFalse(self.projected(item)['codex_assistance_available']);path.chmod(0o600)
  original=self.root/'outside.json';path.rename(original);path.symlink_to(original);self.assertFalse(self.projected(item)['codex_assistance_available'])
 def test_assistance_owner_race_during_metadata_recheck_blocks_submission(self):
  item=self.issue();receipt=self.queue.record_navigation_failure('closure:'+item['id'],str(item['revision']),self.thread);server=MagicMock()
  def race(*args,**kw):
   self.store.conn.execute('UPDATE closure_issues SET owner=?,revision=revision+1 WHERE id=?',('codex:'+str(uuid.uuid4()),item['id']));raise native.NativeError('codex_rpc_rejected','thread not loaded: '+self.thread,rpc_error={'code':-32600,'message':'thread not loaded: '+self.thread})
  server.call.side_effect=race
  with patch.object(native,'AppServer') as factory:
   factory.return_value.__enter__.return_value=server
   with self.assertRaises(h.HandoffError):self.queue.assistance_source(item,{'assistance_token':receipt['token'],'origin_thread_id':self.thread},check_current=True)
 def test_assistance_helper_is_separate_and_never_replaces_original_route(self):
  item=self.issue();before=self.store.get(item['id']);message=str(uuid.uuid4());self.assistance_submit(item,message);self.assistance_submit(item,message);row=self.row();helper=str(uuid.uuid4())
  self.assertIn('不写长期记忆',row['prompt']);self.assertIn('不读取原记忆或会话正文',row['prompt']);self.assertEqual(before,self.store.get(item['id']))
  with self.queue.db() as db:db.execute('UPDATE problems SET thread_id=?,creation_state=?',(helper,'ready'));self.assertEqual(db.execute('SELECT count(*) FROM submissions').fetchone()[0],1)
  projected=self.projected(item);self.assertEqual(projected['codex_thread_id'],self.thread);self.assertEqual(projected['codex_assistance_thread_id'],helper);self.assertFalse(projected['codex_open_via_handoff']);self.assertFalse(projected['codex_assistance_available'])
 def test_existing_assistance_chat_stays_readable_after_cancellation_and_expiry(self):
  import time
  from activity_mvp.attention_center import open_closure_codex
  item=self.issue();self.assistance_submit(item);helper=str(uuid.uuid4())
  with self.queue.db() as db:db.execute('UPDATE problems SET thread_id=?,creation_state=?',(helper,'ready'))
  path=self.queue.root/('navigation-'+item['id']+'.json');value=json.loads(path.read_text());value['checked_at']=time.time()-86401;path.write_text(json.dumps(value))
  self.store.conn.execute("UPDATE closure_issues SET state='canceled',revision=revision+1 WHERE id=?",(item['id'],));current=self.store.get(item['id']);p=self.projected(current)
  self.assertEqual(p['codex_assistance_thread_id'],helper);self.assertFalse(p['codex_assistance_available'])
  server=MagicMock();server.call.return_value={'thread':{'id':helper,'projectId':None,'cwd':str(self.root)}};server.project.return_value=None
  settings={'model':'gpt-6.2-sol','reasoning_effort':'xhigh'}
  server.initial_collaboration_mode.return_value={'settings':settings}
  with patch.object(native,'AppServer') as factory,patch.object(h,'open_existing_codex_thread') as opened:
   factory.return_value.__enter__.return_value=server
   with sqlite3.connect(self.db) as db:r=open_closure_codex(db,p['id'],{'revision':str(current['revision']),'thread_id':helper,'assistance':True})
  self.assertEqual(r['status'],'ok');opened.assert_called_once_with(helper,settings);server.call.assert_called_once_with('thread/read',{'threadId':helper,'includeTurns':False},timeout=5);self.assertEqual(self.store.get(item['id'])['state'],'canceled')
 def test_assistance_unknown_creation_and_send_never_create_second_chat(self):
  item=self.issue();self.assistance_submit(item);row=self.row()
  with self.queue.db() as db:db.execute("UPDATE problems SET creation_state='creating'")
  server=self.metadata_missing();server.project.return_value=None
  with patch.object(native,'AppServer') as factory,patch.object(native,'open_thread'),patch.object(native,'DesktopIPC') as ipc:
   factory.return_value.__enter__.return_value=server;self.queue.process(row['id'])
  server.create.assert_not_called();ipc.assert_not_called();self.assertEqual(self.queue.status('closure:'+item['id'])['handoff']['status'],'uncertain')
 def test_assistance_fake_transport_delivers_once_and_preserves_original_decision(self):
  item=self.issue();before=self.store.get(item['id']);self.assistance_submit(item);row=self.row();helper=str(uuid.uuid4());server=self.metadata_missing();server.project.return_value=None
  server.read.return_value={'id':helper,'projectId':None,'cwd':str(self.root),'turns':[]};server.create.side_effect=lambda project,name,created:created(helper);ipc=MagicMock();ipc.owner.return_value='desktop';ipc.submit.return_value={'received':True}
  with patch.object(native,'AppServer') as factory,patch.object(native,'open_thread'),patch.object(native,'wait_owner'),patch.object(native,'DesktopIPC') as channel,patch.object(native,'receipt_in_thread',side_effect=[None,{'turn_id':'isolated-turn'}]):
   factory.return_value.__enter__.return_value=server;channel.return_value.__enter__.return_value=ipc;self.queue.process(row['id'])
  self.assertEqual(server.create.call_count,1);self.assertEqual(ipc.submit.call_count,1);self.assertEqual(before,self.store.get(item['id']));self.assertEqual(self.queue.status('closure:'+item['id'])['handoff']['status'],'delivered')

if __name__=='__main__':unittest.main()
