import tempfile,time,unittest,json
from pathlib import Path
from activity_mvp.feedback import jev_ranking as r
from activity_mvp.feedback.store import Store,digest
from activity_mvp.feedback.work import WorkQueue
class Client:
 def __init__(self,high=()):self.high=set(high);self.calls=[];self.receipts=[]
 @staticmethod
 def sha(v):return digest(v)
 def scenario(self,*a):return {'mode':'assist','adapter_version':r.ADAPTER_VERSION,'release_version':'r'}
 def request(self,caller,task,state,**k):
  self.calls.append(state['text']);return dict(k,status='ok',mode='assist',content_hash=self.sha(state),request_id=state['text'],judgment={'label':'high' if state['text'] in self.high else 'unknown'},expires_at=time.time()+10,dispatch_phase='completed')
 def adoption(self,caller,result,stage='adopted'):
  self.receipts.append((stage,result['request_id']));return {'recommendation_usable':True,'receipt_recorded':True}
class RankingTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.store=Store(Path(self.tmp.name).resolve()/'store');WorkQueue(self.store)
  self.items=[{'candidate':{'memory_id':str(i),'content_sha256':digest(str(i)),'candidate_fingerprint':digest(['fp',i])},'memory':{'text':str(i)}} for i in range(10)]
  r.oldest(self.store,[x['candidate'] for x in self.items])
 def tearDown(self):self.store.close();self.tmp.cleanup()
 def ids(self,x):return [i['candidate']['memory_id'] for i in x]
 def test_two_oldest_and_only_high_promoted_stably(self):
  c=Client(['8','5','7']);selected,batch=r.select(self.store,self.items,5,lambda:True,c)
  self.assertEqual(self.ids(selected),['0','1','5','7','8']);self.assertTrue(batch)
  self.assertEqual(len(c.calls),8);self.assertTrue(all(s=='app_reported_completed' for s,_ in c.receipts[-3:]))
 def test_unknown_is_local_order_and_no_false_completion(self):
  c=Client();selected,batch=r.select(self.store,self.items,5,lambda:True,c)
  self.assertEqual(self.ids(selected),['0','1','2','3','4']);self.assertIsNone(batch);self.assertEqual(c.receipts,[])
 def test_repeated_same_content_has_no_second_inference(self):
  c=Client(['7']);r.select(self.store,self.items,5,lambda:True,c);r.select(self.store,self.items,5,lambda:True,c)
  self.assertEqual(len(c.calls),8)
 def test_paused_before_submission_has_no_calls(self):
  c=Client(['7']);chosen,batch=r.select(self.store,self.items,5,lambda:False,c)
  self.assertEqual(c.calls,[]);self.assertIsNone(batch)
 def test_revoked_after_adoption_cannot_commit(self):
  c=Client(['7']);valid=[True]
  def adoption(*a,**k):valid[0]=False;return {'recommendation_usable':True}
  c.adoption=adoption;chosen,batch=r.select(self.store,self.items,5,lambda:valid[0],c)
  self.assertIsNone(batch);self.assertEqual(self.ids(chosen),['0','1','2','3','4'])
 def test_completion_only_after_durable_order(self):
  c=Client(['7']);original=c.adoption
  def adoption(caller,result,stage='adopted'):
   if stage=='app_reported_completed':self.assertEqual(self.store.conn.execute('select count(*) from jev_rank_batches').fetchone()[0],1)
   return original(caller,result,stage)
  c.adoption=adoption;r.select(self.store,self.items,5,lambda:True,c)
 def test_dropped_completion_does_not_repeat_or_claim_success(self):
  c=Client(['7']);original=c.adoption
  def adoption(caller,result,stage='adopted'):
   if stage=='app_reported_completed':raise TimeoutError()
   return original(caller,result,stage)
  c.adoption=adoption;chosen,batch=r.select(self.store,self.items,5,lambda:True,c)
  self.assertIsNone(batch);self.assertEqual(r.status(self.store)['completed'],0)
  self.assertEqual(self.ids(chosen),['0','1','2','3','4'])
  self.assertEqual(self.store.conn.execute("SELECT state FROM jev_rank_batches").fetchone()[0],'invalidated')
 def test_missing_memory_stays_eligible_without_call(self):
  self.items[0]['memory']=None;c=Client(['7']);chosen,_=r.select(self.store,self.items,5,lambda:True,c)
  self.assertEqual(self.ids(chosen)[:2],['0','1']);self.assertNotIn('0',c.calls)
 def test_order_age_is_persistent(self):
  first=[x['memory_id'] for x in r.oldest(self.store,[x['candidate'] for x in reversed(self.items)])];self.assertEqual(first,[str(i) for i in range(10)])
 def test_version_mismatch_falls_back(self):
  c=Client(['7']);c.scenario=lambda *a:{'mode':'assist','adapter_version':'old'}
  chosen,batch=r.select(self.store,self.items,5,lambda:True,c);self.assertFalse(c.calls);self.assertIsNone(batch)

 def test_expired_result_cannot_change_order(self):
  c=Client(['7']);original=c.request
  def request(*a,**k):
   answer=original(*a,**k);answer['expires_at']=0;return answer
  c.request=request
  chosen,batch=r.select(self.store,self.items,5,lambda:True,c)
  self.assertIsNone(batch)
 def test_commit_error_falls_back_without_completion(self):
  c=Client(['7']);self.store.conn.execute('CREATE TRIGGER deny_rank BEFORE INSERT ON jev_rank_batches BEGIN SELECT RAISE(ABORT, "fixture"); END;')
  chosen,batch=r.select(self.store,self.items,5,lambda:True,c)
  self.assertIsNone(batch);self.assertFalse(any(stage=='app_reported_completed' for stage,_ in c.receipts))
 def test_completion_storage_failure_invalidates_committed_order(self):
  c=Client(['7'])
  self.store.conn.execute('CREATE TRIGGER deny_completion BEFORE UPDATE OF completed ON jev_rank_batches BEGIN SELECT RAISE(ABORT, "fixture"); END;')
  chosen,batch=r.select(self.store,self.items,5,lambda:True,c)
  self.assertIsNone(batch);self.assertEqual(self.ids(chosen),['0','1','2','3','4'])
  self.assertEqual(self.store.conn.execute('SELECT state FROM jev_rank_batches').fetchone()[0],'invalidated')
  self.assertEqual(r.status(self.store)['completed'],0)
  r.select(self.store,self.items,5,lambda:True,c);self.assertEqual(len(c.calls),8)
 def test_all_receipt_writes_fail_without_false_adoption_metrics(self):
  c=Client(['7'])
  self.store.conn.execute('CREATE TRIGGER deny_updates BEFORE UPDATE ON jev_rank_batches BEGIN SELECT RAISE(ABORT, "fixture"); END;')
  chosen,batch=r.select(self.store,self.items,5,lambda:True,c)
  self.assertIsNone(batch);self.assertEqual(self.ids(chosen),['0','1','2','3','4'])
  self.assertEqual(self.store.conn.execute('SELECT state FROM jev_rank_batches').fetchone()[0],'awaiting_receipts')
  self.assertEqual(r.status(self.store)['batches'],0)
 def test_delayed_workers_cannot_apply_after_timeout(self):
  c=Client(['7']);original=c.request
  def request(*a,**k):time.sleep(.15);return original(*a,**k)
  c.request=request
  from unittest.mock import patch
  with patch.object(r,'wait',return_value=(set(),set())):
   chosen,batch=r.select(self.store,self.items,5,lambda:True,c)
  time.sleep(.2);self.assertIsNone(batch);self.assertEqual(c.receipts,[])
 def test_old_queue_age_preserved(self):
  q=WorkQueue(self.store);old=q.enqueue('memory','old','r')
  self.store.conn.execute('UPDATE work_events SET created=1 WHERE work_id=?',(old['id'],))
  all_candidates=[x['candidate'] for x in self.items]+[{'memory_id':'old'}]
  self.assertEqual(r.oldest(self.store,all_candidates)[0]['memory_id'],'old')

class AdmissionRankingTests(RankingTests):
 def test_denied_preflight_does_not_burn_an_attempt(self):
  c=Client(['7']);c.preflight=lambda *a,**k:{'eligible':False}
  selected,batch=r.select(self.store,self.items,5,lambda:True,c)
  self.assertFalse(c.calls);self.assertIsNone(batch)
  self.assertEqual(self.store.conn.execute('SELECT count(*) FROM jev_rank_attempts').fetchone()[0],0)
  c.preflight=lambda *a,**k:{'eligible':True}
  selected,batch=r.select(self.store,self.items,5,lambda:True,c)
  self.assertTrue(batch);self.assertEqual(self.ids(selected),['0','1','7','2','3'])
