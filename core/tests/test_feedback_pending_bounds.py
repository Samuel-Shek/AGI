"""Bounded batches retain whole-record admission and live revocation."""
import json
import time
import unittest
from unittest.mock import patch

from activity_mvp import conversation_scope as scope
from activity_mvp.feedback import model, sources
from activity_mvp.feedback.store import Store, packed
from tests import test_conversation_scope as fixtures


class FeedbackPendingBoundsTests(unittest.TestCase):
    setUp=fixtures.ConversationAdmissionTests.setUp
    write_source=fixtures.ConversationAdmissionTests.write_source
    write_grant=fixtures.ConversationAdmissionTests.write_grant

    def message(self,store,ident,rank,*,allowed=True,state='pending'):
        store.conn.execute('INSERT INTO messages(id,source_id,session,observed,quote,context,reference,state,imported) VALUES(?,?,?,?,?,?,?,?,?)',
            (ident,'codex',self.session,time.time()-rank,'synthetic','',packed({'path':str(self.path if allowed else self.root/'missing')}),state,time.time()))

    def incident(self,store,ident,rank,messages):
        store.conn.execute('INSERT INTO incidents(id,title,category,state,created,updated) VALUES(?,?,?,?,?,?)',
            (ident,'synthetic','other','investigating',time.time(),time.time()-rank))
        store.conn.executemany('INSERT INTO quotes VALUES(?,?)',[(message,ident) for message in messages])

    def select(self,store,*,incidents=False,admit=None,active=lambda:True):
        view=scope.filtered(store,'feedback')
        if incidents:
            return model._admitted_ids(view,'incidents','id','1','updated DESC',40,
                admit or (lambda r:sources.incident_admitted(view,r['id'])),active)
        return model._admitted_ids(view,'messages','id,session,source_id,reference',"state='pending'",'observed DESC',5,
            admit or (lambda r:sources.source_metadata_admitted(r['session'],json.loads(r['reference'])['path'],r['source_id'])),active)

    def test_message_limit_stops_admission_and_preserves_rows(self):
        with Store(self.root/'feedback') as store:
            for i in range(100):self.message(store,str(i),i)
            before=list(store.conn.iterdump())
            for readonly in (False,True):
                with Store(store.root,readonly=readonly) as read:
                    with patch.object(sources,'source_metadata_admitted',wraps=sources.source_metadata_admitted) as admission,patch.object(scope,'allowed',wraps=scope.allowed) as native:
                        self.assertEqual(self.select(read),list(map(str,range(5))))
                        self.assertEqual(admission.call_count,5)
                        self.assertLessEqual(native.call_count,20)
            self.assertEqual(list(store.conn.iterdump()),before)

    def test_full_denied_pages_and_explicit_admission_rejections_continue(self):
        with Store(self.root/'feedback') as store:
            for i in range(51):self.message(store,'denied-'+str(i),i,allowed=False)
            for i in range(8):self.message(store,str(i),51+i)
            for readonly in (False,True):
                with Store(store.root,readonly=readonly) as read:
                    seen=[]
                    def admit(row):seen.append(row['id']);return row['id']!='0'
                    self.assertEqual(self.select(read,admit=admit),list(map(str,range(1,6))))
                    self.assertEqual(seen,list(map(str,range(6))))

    def test_incident_limit_rejects_mixed_and_unreferenced_records(self):
        with Store(self.root/'feedback') as store:
            self.message(store,'mixed-allowed',0)
            self.message(store,'mixed-denied',0,allowed=False)
            self.incident(store,'mixed',0,['mixed-allowed','mixed-denied'])
            self.incident(store,'no-quotes',1,[])
            for i in range(50):
                self.message(store,'message-'+str(i),i+2)
                self.incident(store,str(i),i+2,['message-'+str(i)])
            before=list(store.conn.iterdump())
            for readonly in (False,True):
                with Store(store.root,readonly=readonly) as read:
                    with patch.object(sources,'incident_admitted',wraps=sources.incident_admitted) as admission:
                        self.assertEqual(self.select(read,incidents=True),list(map(str,range(40))))
                        self.assertEqual(admission.call_count,40)
                        self.assertNotIn('mixed',[call.args[1] for call in admission.call_args_list])
            self.assertEqual(list(store.conn.iterdump()),before)

    def test_readonly_connection_observes_revocation(self):
        with Store(self.root/'feedback') as store:
            self.message(store,'allowed',0)
            with Store(store.root,readonly=True) as read:
                self.assertEqual(self.select(read),['allowed'])
                self.grant['enabled']=False;self.grant['revision']+=1;self.write_grant()
                self.assertEqual(self.select(read),[])

    def test_inactive_reads_no_candidate_ids(self):
        with Store(self.root/'feedback') as store:
            view=scope.filtered(store,'feedback')
            statements=[];store.conn.set_trace_callback(statements.append)
            with self.assertRaisesRegex(PermissionError,'not_active'):
                model._admitted_ids(view,'messages','id',"state='pending'",'observed DESC',5,lambda r:True,lambda:False)
            self.assertEqual(statements,[])

    def test_source_changed_does_not_refill_batch_or_touch_unknown_budget(self):
        with Store(self.root/'feedback') as store:
            store.set('config',{'enabled':True})
            for i in range(8):self.message(store,str(i),i)
            key=store.reserve('analysis');store.settle(key,'unknown')
            before=store.rows('SELECT * FROM budget')
            with patch.object(sources,'reference_is_current',return_value=False) as current,patch.object(model,'invoke',side_effect=AssertionError('model called')):
                self.assertEqual(model.analyze(store),{'processed':0,'reason':'source_changed'})
                self.assertEqual(current.call_count,5)
            self.assertEqual(store.rows("SELECT id FROM main.messages WHERE state='pending' ORDER BY observed DESC"),[{'id':str(i)} for i in range(5,8)])
            self.assertEqual(store.rows('SELECT * FROM budget'),before)


if __name__=='__main__':unittest.main()
