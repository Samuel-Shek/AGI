import json,time,types,unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from tests.test_jev_ranking import RankingTests,Client
from activity_mvp.feedback import jev_ranking as r,jev_dispatch as d

class DispatchTests(RankingTests):
 def test_slow_preflights_never_consume_unsent_versions(self):
  now=[100.0];c=Client(['7'])
  def preflight(*a,**kw):now[0]+=1.8;return {'eligible':now[0]<kw['deadline']}
  c.preflight=preflight
  with patch.object(r,'time',types.SimpleNamespace(time=lambda:now[0])):
   selected,batch=r.select(self.store,self.items,5,lambda:True,c)
  self.assertFalse(c.calls);self.assertIsNone(batch)
  self.assertEqual(self.store.conn.execute('SELECT count(*) FROM jev_dispatch').fetchone()[0],0)
  c.preflight=lambda *a,**k:{'eligible':True}
  self.assertTrue(r.select(self.store,self.items,5,lambda:True,c)[1]);self.assertEqual(len(c.calls),8)
 def test_two_protected_positions_do_not_judge(self):
  c=Client(['0','1']);selected,batch=r.select(self.store,self.items[:2],2,lambda:True,c)
  self.assertFalse(c.calls);self.assertFalse(c.receipts);self.assertIsNone(batch)
 def test_single_adjustable_position_does_not_judge(self):
  c=Client(['2']);self.assertIsNone(r.select(self.store,self.items[:3],3,lambda:True,c)[1]);self.assertFalse(c.calls)
 def test_unchanged_order_does_not_adopt(self):
  c=Client(['2']);selected,batch=r.select(self.store,self.items,5,lambda:True,c)
  self.assertFalse(c.receipts);self.assertIsNone(batch);self.assertEqual(self.ids(selected),['0','1','2','3','4'])
 def test_known_unsent_can_send_later_unknown_cannot(self):
  for phase in ('not_dispatched','unknown'):
   event='event-'+phase;calls=[]
   def invoke():calls.append(1);return {'dispatch_phase':phase}
   d.execute(self.store.path,event,invoke,lambda:True);d.execute(self.store.path,event,invoke,lambda:True)
   self.assertEqual(len(calls),2 if phase=='not_dispatched' else 1)
 def test_concurrent_same_version_claims_once(self):
  calls=[]
  def invoke():calls.append(1);time.sleep(.02);return {'dispatch_phase':'completed'}
  with ThreadPoolExecutor(4) as pool:list(pool.map(lambda _:d.execute(self.store.path,'one-event',invoke,lambda:True),range(4)))
  self.assertEqual(len(calls),1)
 def test_crash_after_claim_is_never_retried(self):
  def fail():raise TimeoutError()
  with self.assertRaises(TimeoutError):d.execute(self.store.path,'crash',fail,lambda:True)
  self.assertIsNone(d.execute(self.store.path,'crash',lambda:self.fail('replayed'),lambda:True))
 def test_legacy_attempt_remains_untouched(self):
  self.store.conn.execute('INSERT INTO jev_rank_attempts VALUES (?,?)',('legacy',1))
  self.assertIsNone(d.execute(self.store.path,'legacy',lambda:self.fail('legacy replay'),lambda:True))
  self.assertEqual(self.store.conn.execute('SELECT created FROM jev_rank_attempts').fetchone()[0],1)
 def test_pause_between_claim_and_send_leaves_unsent(self):
  n=[0]
  def valid():n[0]+=1;return n[0]<=2
  d.execute(self.store.path,'paused',lambda:self.fail('sent while paused'),valid)
  self.assertEqual(self.store.conn.execute("SELECT phase FROM jev_dispatch WHERE event_id='paused'").fetchone()[0],'not_dispatched')

if __name__=='__main__':unittest.main()
