"""Readiness visits pending candidates; raw identifiers never grant access."""
import time
import unittest
from unittest.mock import patch

from activity_mvp import conversation_scope as scope
from activity_mvp.reflection import analysis
from activity_mvp.reflection.store import Store
from activity_mvp.feedback.store import packed
from activity_mvp.retirement_snapshot import Snapshot
from tests import test_conversation_scope as fixtures


class ReadinessBoundsTests(unittest.TestCase):
    setUp=fixtures.ConversationAdmissionTests.setUp
    write_source=fixtures.ConversationAdmissionTests.write_source
    write_grant=fixtures.ConversationAdmissionTests.write_grant

    def add(self,store,ident,*,pending=True,observed=None,path=None):
        now=time.time()
        store.conn.execute('INSERT INTO contexts(id,source_id,session,updated) VALUES(?,?,?,?)',(ident,'codex',self.session,now))
        store.conn.execute('INSERT INTO turns(id,context_id,source_id,session,role,observed,text,reference) VALUES(?,?,?,?,?,?,?,?)',
            (ident,ident,'codex',self.session,'user',now if observed is None else observed,'synthetic',packed({'path':str(path or self.path),'parser_version':2})))
        if not pending:store.conn.execute('UPDATE contexts SET processed_seq=(SELECT seq FROM turns WHERE id=?) WHERE id=?',(ident,ident))

    def test_processed_and_expired_history_do_not_multiply_policy_checks(self):
        with Store(self.root/'reflection') as store:
            store.set('enabled',True)
            for i in range(250):
                self.add(store,'completed-'+str(i),pending=False)
                self.add(store,'expired-'+str(i),observed=time.time()-15*86400)
            self.add(store,'pending')
            before=list(store.conn.iterdump())
            with Store(store.root,readonly=True) as read:
                calls=[];original=Snapshot.unchanged
                def counted(view):calls.append(1);return original(view)
                with patch.object(Snapshot,'unchanged',counted):self.assertTrue(analysis.ready(read))
                self.assertLess(len(calls),40)
            self.assertEqual(list(store.conn.iterdump()),before)

    def test_denied_full_page_does_not_hide_next_allowed_context(self):
        with Store(self.root/'reflection') as store:
            store.set('enabled',True)
            for i in range(51):self.add(store,'denied-'+str(i),path=self.root/'missing')
            self.add(store,'eligible')
            for read_only in (False,True):
                with Store(store.root,readonly=read_only) as read:
                    projected=scope.filtered(read,'reflection')
                    self.assertEqual([c['id'] for c,_ in analysis.pending_contexts(projected,time.time(),read.enabled)],['eligible'])
                    self.assertTrue(analysis.ready(read))

    def test_retired_denied_source_and_live_revocation_remain_closed(self):
        with Store(self.root/'reflection') as store:
            store.set('enabled',True);self.add(store,'mixed')
            store.conn.execute('CREATE TABLE retired_turns AS SELECT * FROM turns')
            store.conn.execute('UPDATE retired_turns SET reference=?',(packed({'path':str(self.root/'missing')}),))
            self.add(store,'eligible')
            with Store(store.root,readonly=True) as read:
                self.assertEqual([c['id'] for c,_ in analysis.pending_contexts(read,time.time(),read.enabled)],['eligible'])
                self.assertTrue(analysis.ready(read))
                self.grant['enabled']=False;self.grant['revision']+=1;self.write_grant()
                self.assertFalse(analysis.ready(read))

    def test_raw_future_turn_does_not_make_completed_context_pending(self):
        with Store(self.root/'reflection') as store:
            store.set('enabled',True);self.add(store,'completed',pending=False)
            store.conn.execute('UPDATE contexts SET summary=?',(packed({'complete':True}),))
            store.conn.execute("INSERT INTO turns(id,context_id,source_id,session,role,observed,text,reference) SELECT 'future',context_id,source_id,session,role,?,text,reference FROM turns",(time.time()+3600,))
            for read_only in (False,True):
                with Store(store.root,readonly=read_only) as read:
                    projected=scope.filtered(read,'reflection')
                    with patch.object(analysis,'context_complete',return_value=True) as completion:
                        self.assertEqual(list(analysis.pending_contexts(projected,time.time(),read.enabled)),[])
                        completion.assert_not_called()

    def test_inactive_does_not_read_candidate_ids(self):
        with Store(self.root/'reflection') as store:
            self.add(store,'pending')
            with Store(store.root,readonly=True) as read:
                statements=[];read.conn.set_trace_callback(statements.append)
                with self.assertRaisesRegex(PermissionError,'not_active'):
                    next(analysis.pending_contexts(read,time.time(),lambda:False))
                self.assertEqual(statements,[])


if __name__=='__main__':unittest.main()
