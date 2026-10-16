import json
import unittest
import tempfile
from pathlib import Path
from activity_mvp.feedback.store import Store
from tests.personal_scope_fixtures import register_personal
from activity_mvp.feedback.routing import route
from activity_mvp.feedback.probes import observe_journal
from activity_mvp.feedback.sources import incident_admitted

class RoutingTests(unittest.TestCase):
    def test_assistant_feedback_does_not_grant_project_write(self):
        value=route([{'quote':'Codex 没有验证就说完成'}],{'status':'identified','project_id':'agi'},'v1')
        self.assertEqual(value['criticized_object'],'codex')
        self.assertEqual(value['editable_object'],'none')
        self.assertEqual(value['handler'],'behavior_observation')
    def test_current_project_and_unknown_stay_distinct(self):
        value=route([{'quote':'设置页面没有显示内容'}],{'status':'identified','project_id':'agi'},'v1')
        self.assertEqual(value['handler'],'project_repair')
        self.assertEqual(route([],{},'v1')['handler'],'investigation')
        self.assertNotEqual(value['binding_sha256'],route([],{},'v2')['binding_sha256'])
    def test_legacy_and_corrupt_receipts_do_not_stop_other_jobs(self):
        jobs=[{'id':str(i),'receipt':body} for i,body in enumerate(['{','[]','{}',json.dumps({'rounds':[{'candidate_passed':True}]}),json.dumps({'rounds':[{'changed':['other.py']} ]})])]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();register_personal(self,root,['fixture'])
            source=root/'fixture.jsonl'
            source.write_text(json.dumps({'sessionId':'fixture','role':'user','content':'synthetic owner evidence'})+'\n')
            with Store(root/'feedback') as store:
                store.set('config',{'journal':str(root/'journal')})
                store.conn.execute('INSERT INTO incidents(id,title,category,state,created,updated,installed_at) VALUES(?,?,?,?,?,?,?)',('i','synthetic','evidence','observing',1,1,1))
                store.conn.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)',('m','fixture','fixture',1,'synthetic owner evidence','', json.dumps({'path':str(source)}),'classified',1,0))
                store.conn.execute('INSERT INTO quotes VALUES(?,?)',('m','i'))
                for job in jobs:store.conn.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?)',(job['id'],'i',1,'observing',1,1,job['receipt']))
                self.assertTrue(incident_admitted(store,'i'))
                self.assertEqual(observe_journal(store),{'new_natural_samples':0,'insufficient_evidence':4})
                self.assertEqual(len(store.rows("SELECT * FROM audit WHERE event='natural_probe_evidence_insufficient'")),4)
                store.conn.execute('UPDATE messages SET reference=?', ('{}',))
                self.assertFalse(incident_admitted(store,'i'))
                self.assertEqual(observe_journal(store),{'new_natural_samples':0,'insufficient_evidence':0})

if __name__=='__main__':unittest.main()
