import tempfile
import unittest
from pathlib import Path
from activity_mvp.feedback.store import Store, packed
from activity_mvp.feedback.presentation import progress
from tests.personal_scope_fixtures import register_personal

class FeedbackPresentationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name).resolve()/'feedback'
        register_personal(self, self.path.parent, sessions=('presentation-owner',))
        self.store = Store(self.path)
        self.store.set('config', {'enabled':True})
        self.store.set('last_tick', 1000)
    def tearDown(self):
        self.store.close(); self.tmp.cleanup()
    def test_counts_are_full_ledger_and_do_not_certify_unverified_install(self):
        for n in range(220):
            self.store.conn.execute('INSERT INTO incidents(id,title,category,state,created,updated) VALUES(?,?,?,?,?,?)',
                                    (str(n),'private title','execution','decision',1,1))
            # These are explicitly registered personal incidents. Missing
            # provenance is deliberately excluded by the read-only projection.
            self.store.conn.execute('INSERT INTO messages(id,source_id,session,observed,quote,context,reference,imported) VALUES(?,?,?,?,?,?,?,?)',
                                    (str(n),'synthetic','presentation-owner',1,'private quote','',
                                     packed({'path':str(self.path.parent/'synthetic.jsonl')}),1))
            self.store.conn.execute('INSERT INTO quotes(message_id,incident_id) VALUES(?,?)',(str(n),str(n)))
        self.store.conn.execute("UPDATE incidents SET state='observing',installed_at=2 WHERE id='0'")
        self.store.conn.execute("UPDATE incidents SET state='verified',installed_at=2,verified_at=3 WHERE id='1'")
        self.store.log('classification_completed',processed=3)
        self.store.set('analysis_last_result',{'processed':0,'finished':1100})
        self.store.conn.commit()
        before=self.path.joinpath('feedback.sqlite').read_bytes()
        with Store(self.path,readonly=True) as reader:
            result=progress(reader,now=1100)
        self.assertEqual((result['incident_count'],result['decision_count'],result['installed_count'],result['verified_count']),(220,218,2,1))
        self.assertIsNotNone(result['last_analysis_success_at'])
        self.assertNotIn('private title',str(result))
        self.assertEqual(before,self.path.joinpath('feedback.sqlite').read_bytes())
    def test_unknown_incidents_are_excluded_without_a_display_policy(self):
        self.store.conn.execute("INSERT INTO incidents(id,title,category,state,created,updated,installed_at,verified_at) VALUES('unknown','private title','execution','verified',1,1,2,3)")
        before=self.path.joinpath('feedback.sqlite').read_bytes()
        with Store(self.path,readonly=True) as reader:
            result=progress(reader,now=1100)
        self.assertEqual((result['incident_count'],result['decision_count'],result['installed_count'],result['verified_count']),(0,0,0,0))
        self.assertEqual(before,self.path.joinpath('feedback.sqlite').read_bytes())
    def test_noop_tick_is_not_successful_analysis(self):
        self.store.set('analysis_last_result',{'processed':0,'finished':1100})
        self.assertIsNone(progress(self.store,now=1100)['last_analysis_success_at'])
    def test_running_retry_paused_and_stale_are_distinct(self):
        self.store.set('analysis_last_result',{'status':'running','started':1090})
        self.assertEqual(progress(self.store,now=1100)['state'],'running')
        self.assertEqual(progress(self.store,now=2200)['state'],'stale')
        self.store.set('analysis_last_result',{'status':'deferred'})
        self.store.set('analysis_retry_at',2300)
        self.assertEqual(progress(self.store,now=2200)['state'],'retrying')
        self.store.set('paused',True)
        self.assertEqual(progress(self.store,now=2200)['state'],'paused')
    def test_missing_tick_is_unknown_not_healthy(self):
        self.store.set('last_tick',None)
        self.assertEqual(progress(self.store,now=1100)['state'],'stale')
