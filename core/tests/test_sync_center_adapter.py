import json,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch
from activity_mvp.feedback import sync_center as s
from activity_mvp.feedback.store import Store
from activity_mvp.feedback.work import POLICY,WorkQueue
from tests.personal_scope_fixtures import register_personal
class Adapter(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.root=Path(self.t.name).resolve();self.root.chmod(0o700)
  register_personal(self,self.root)
  self.store=Store(self.root/'feedback');self.store.set('config',{'enabled':True});self.store.set('autonomy',{'policy':POLICY});self.store.set('sync_center',{'policy':s.POLICY})
  WorkQueue(self.store)
  self.bridge=self.root/'bridge';self.area=self.bridge/'state/sync-autonomy';self.area.mkdir(parents=True);(self.area/'candidates').mkdir()
  self.data={'schema_version':1,'policy':s.POLICY,'generated_at':time.time(),'binding':{'fixed':'v1'},'probe_revision':'a'*64,'probe':{'passed':True},'issues':[{'id':'issue-'+'b'*24,'revision':'c'*64,'kind':'media','reason':'video_waiting_instruction','state':'waiting'}]}
  self.guard=patch.object(s,'FIXED_GUARDS',{'fixed':'v1'});self.guard.start();self.save()
 def save(self):(self.area/'export.json').write_text(json.dumps(self.data))
 def tearDown(self):self.guard.stop();self.store.close();self.t.cleanup()
 def test_idempotent_and_no_business_writes(self):
  before=(self.area/'export.json').read_bytes()
  with patch.object(s,'read_export',side_effect=AssertionError('retired source must not be read')):
   self.assertEqual(s.tick(self.store,root=self.bridge)['status'],'owner_retired')
   self.assertEqual(s.tick(self.store,root=self.bridge)['status'],'owner_retired')
  self.assertEqual(self.store.rows('SELECT * FROM work_events'),[]);self.assertEqual(before,(self.area/'export.json').read_bytes())
 def test_absence_not_close(self):
  queue=WorkQueue(self.store);item=queue.enqueue('sync_center',self.data['issues'][0]['id'],'old',s.PROJECT_ID)
  queue.update(item['id'],'waiting_evidence');self.data['issues']=[];self.save();s.tick(self.store,root=self.bridge)
  self.assertEqual(queue.get(item['id'])['state'],'waiting_evidence')
 def test_stale_version_rejected(self):
  self.data['binding']['fixed']='changed';self.save()
  with self.assertRaisesRegex(ValueError,'version'):s.read_export(self.bridge)
  self.assertEqual(s.tick(self.store,root=self.bridge)['status'],'owner_retired')
 def test_stale_export_rejected(self):
  self.data['generated_at']-=901;self.save()
  with self.assertRaisesRegex(ValueError,'stale'):s.read_export(self.bridge)
  self.assertEqual(s.tick(self.store,root=self.bridge)['status'],'owner_retired')
 def test_explicit_close_is_not_sync_success(self):
  queue=WorkQueue(self.store);item=queue.enqueue('sync_center',self.data['issues'][0]['id'],'old',s.PROJECT_ID);queue.update(item['id'],'waiting_evidence')
  self.data['issues'][0]['state']='closed';self.save();s.tick(self.store,root=self.bridge)
  self.assertEqual(queue.get(item['id'])['state'],'waiting_evidence');self.assertEqual(self.store.rows('SELECT * FROM jobs'),[])
if __name__=='__main__':unittest.main()
