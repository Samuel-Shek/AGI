"""Synthetic post-settlement reads preserve live admission and atomic writes."""
import time
import json
import unittest
from unittest.mock import patch

from activity_mvp import conversation_scope as scope
from activity_mvp.feedback import model, sources
from activity_mvp.feedback.store import Store
from tests import test_feedback_pending_bounds as fixtures


class FeedbackResultBoundsTests(unittest.TestCase):
    setUp=fixtures.FeedbackPendingBoundsTests.setUp
    write_source=fixtures.FeedbackPendingBoundsTests.write_source
    write_grant=fixtures.FeedbackPendingBoundsTests.write_grant
    message=fixtures.FeedbackPendingBoundsTests.message
    incident=fixtures.FeedbackPendingBoundsTests.incident

    def seed(self,store,ident,observed,*,allowed=True,quote='matching'):
        self.message(store,ident,0,allowed=allowed,state='classified')
        store.conn.execute("UPDATE messages SET source_id='seed',quote=?,observed=? WHERE id=?",(quote,observed,ident))
        self.incident(store,'event-'+ident,0,[ident])

    def item(self,ident,related=''):
        return {'message_id':ident,'is_criticism':True,'title':'synthetic',
                'category':'other','project':'agi','related_id':related,
                'cause_hypothesis':'synthetic','evidence_notes':'synthetic','action_plan':'synthetic'}

    def test_seed_admission_does_not_expand_unrelated_history(self):
        with Store(self.root/'feedback') as s:
            for i in range(100):
                self.message(s,'u'+str(i),i,state='classified')
                self.incident(s,'event-u'+str(i),i,['u'+str(i)])
            now=time.time();self.seed(s,'target',now)
            view=scope.filtered(s,'feedback')
            with patch.object(scope,'allowed',wraps=scope.allowed) as admitted:
                original=view.rows("SELECT q.incident_id,m.observed FROM quotes q JOIN messages m ON m.id=q.message_id WHERE m.source_id='seed' AND m.quote=?",('matching',))
                old_calls=admitted.call_count
            with patch.object(scope,'allowed',wraps=scope.allowed) as admitted:
                actual=model._seed_match(view,{'quote':'matching','observed':now},lambda:True)
                new_calls=admitted.call_count
            self.assertEqual(actual,original[0]['incident_id'])
            self.assertGreaterEqual(old_calls,100)
            self.assertLessEqual(new_calls,4)

    def test_seed_rejections_do_not_hide_later_valid_match(self):
        with Store(self.root/'feedback') as s:
            now=time.time()
            self.seed(s,'denied',now,allowed=False)
            self.seed(s,'mixed',now)
            self.message(s,'denied-child',0,allowed=False,state='classified')
            s.conn.execute('INSERT INTO quotes VALUES(?,?)',('denied-child','event-mixed'))
            self.seed(s,'good',now)
            view=scope.filtered(s,'feedback')
            self.assertEqual(model._seed_match(view,{'quote':'matching','observed':now},lambda:True),'event-good')

    def test_seed_day_boundaries_and_live_revocation(self):
        with Store(self.root/'feedback') as s:
            now=time.time();self.seed(s,'target',now)
            view=scope.filtered(s,'feedback')
            for delta,expected in [(-1,None),(0,'event-target'),(86399,'event-target'),(86400,None)]:
                with self.subTest(delta=delta):
                    self.assertEqual(model._seed_match(view,{'quote':'matching','observed':now+delta},lambda:True),expected)
            self.grant['enabled']=False;self.grant['revision']+=1;self.write_grant()
            self.assertIsNone(model._seed_match(view,{'quote':'matching','observed':now},lambda:True))

    def test_multiple_seed_matches_retain_original_quote_order(self):
        with Store(self.root/'feedback') as s:
            now=time.time()
            for ident,offset in [('z',3),('a',0),('m',2)]:self.seed(s,ident,now-offset)
            s.conn.execute('DELETE FROM quotes')
            s.conn.executemany('INSERT INTO quotes VALUES(?,?)',[(x,'event-'+x) for x in ('m','z','a')])
            view=scope.filtered(s,'feedback')
            old=view.rows("SELECT q.incident_id,m.observed FROM quotes q JOIN messages m ON m.id=q.message_id WHERE m.source_id='seed' AND m.quote=?",('matching',))
            self.assertEqual(model._seed_match(view,{'quote':'matching','observed':now},lambda:True),old[0]['incident_id'])

    def test_related_session_day_and_state_semantics(self):
        for state in ('investigating','observing','verified'):
            for delta in (0,86399,86400):
                with self.subTest(state=state,delta=delta),Store(self.root/(state+str(delta))) as s:
                    now=time.time();self.message(s,'old',0,state='classified');self.message(s,'new',0)
                    s.conn.execute('UPDATE messages SET observed=? WHERE id=?',(now-delta,'old'))
                    s.conn.execute('UPDATE messages SET observed=? WHERE id=?',(now,'new'))
                    self.incident(s,'related',0,['old'])
                    s.conn.execute('UPDATE incidents SET state=? WHERE id=?',(state,'related'))
                    view=scope.filtered(s,'feedback');rows=view.rows("SELECT * FROM messages WHERE id='new'")
                    with patch.object(sources,'reference_is_current',return_value=True):
                        model._apply_analysis_result(view,rows,[{'id':'related'}],{'items':[self.item('new','related')]},'settled-key',lambda:True)
                    actual=s.rows("SELECT incident_id FROM quotes WHERE message_id='new'")[0]['incident_id']
                    self.assertEqual(actual=='related',state=='investigating' and delta<86400)

    def test_related_different_native_session_is_not_merged(self):
        other='12345678-0000-4000-8000-000000000001'
        path=self.path.with_name('rollout-'+other+'.jsonl')
        lines=json.loads(json.dumps(self.lines));lines[0]['payload']['id']=other
        path.write_text('\n'.join(json.dumps(x) for x in lines)+'\n');path.chmod(0o600)
        with Store(self.root/'feedback') as s:
            self.message(s,'old',0,state='classified');self.message(s,'new',0)
            s.conn.execute('UPDATE messages SET session=?,reference=? WHERE id=?',(other,json.dumps({'path':str(path)}),'old'))
            self.incident(s,'related',0,['old']);view=scope.filtered(s,'feedback')
            rows=view.rows("SELECT * FROM messages WHERE id='new'")
            with patch.object(sources,'reference_is_current',return_value=True):
                model._apply_analysis_result(view,rows,[{'id':'related'}],{'items':[self.item('new','related')]},'settled-key',lambda:True)
            self.assertNotEqual(s.rows("SELECT incident_id FROM quotes WHERE message_id='new'")[0]['incident_id'],'related')

    def test_revocation_during_candidate_read_rolls_back_earlier_item(self):
        with Store(self.root/'feedback') as s:
            s.set('config',{'enabled':True})
            now=time.time();self.seed(s,'target',now)
            self.message(s,'first',0);self.message(s,'second',0)
            s.conn.execute("UPDATE messages SET quote='matching' WHERE id='second'")
            view=scope.filtered(s,'feedback');rows=[s.rows('SELECT * FROM messages WHERE id=?',(x,))[0] for x in ('first','second')]
            active=scope.bind(lambda:True)
            key=s.reserve('analysis');s.settle(key,'settled',{'input_tokens':2})
            before={t:s.rows('SELECT * FROM '+t) for t in ('budget','messages','quotes','incidents')}
            read=type(view).rows
            def revoke_after_candidate(current,sql,args=()):
                result=read(current,sql,args)
                if sql=='SELECT id FROM incidents WHERE id=?' and args==('event-target',):
                    self.grant['enabled']=False;self.grant['revision']+=1;self.write_grant()
                return result
            with patch.object(sources,'reference_is_current',return_value=True),patch.object(type(view),'rows',autospec=True,side_effect=revoke_after_candidate):
                with self.assertRaisesRegex(PermissionError,'authorization_changed'):
                    model._apply_analysis_result(view,rows,[],{'items':[self.item(r['id']) for r in rows]},key,active)
            self.assertEqual({t:s.rows('SELECT * FROM '+t) for t in before},before)

    def test_apply_failure_rolls_back_without_changing_settled_usage(self):
        with Store(self.root/'feedback') as s:
            s.set('config',{'enabled':True})
            for i in ('first','second'):self.message(s,i,0)
            key=s.reserve('analysis');s.settle(key,'settled',{'input_tokens':2})
            before={t:s.rows('SELECT * FROM '+t) for t in ('budget','messages','quotes','incidents')}
            view=scope.filtered(s,'feedback');rows=view.rows('SELECT * FROM messages')
            with patch.object(sources,'reference_is_current',return_value=True),patch.object(model,'_seed_match',side_effect=[None,RuntimeError('synthetic-read-failure')]),patch.object(model,'invoke',side_effect=AssertionError('unexpected model')):
                with self.assertRaisesRegex(RuntimeError,'synthetic-read-failure'):
                    model._apply_analysis_result(view,rows,[],{'items':[self.item(r['id']) for r in rows]},key,lambda:True)
            self.assertEqual({t:s.rows('SELECT * FROM '+t) for t in before},before)

    def test_after_model_phase_is_review_with_no_new_deadline(self):
        with Store(self.root/'feedback') as s:
            s.set('config',{'enabled':True});self.message(s,'new',0)
            def invocation(*args,**kwargs):
                s.set('local_check_progress',{'phase':'model','status':'running'})
                key=s.reserve('analysis');s.settle(key,'settled',{})
                return {'items':[self.item('new')]},key
            def apply(*args):
                phase=s.get('local_check_progress')
                self.assertEqual((phase['phase'],phase['status'],phase['limit_seconds']),('review','running',None))
                return {'processed':1}
            with patch.object(sources,'reference_is_current',return_value=True),patch.object(model,'invoke',side_effect=invocation) as invoke,patch.object(model,'_apply_analysis_result',side_effect=apply):
                self.assertEqual(model.analyze(s),{'processed':1})
            self.assertEqual(invoke.call_count,1)
            self.assertEqual(s.get('local_check_progress')['status'],'completed')
            self.assertEqual([r['state'] for r in s.rows('SELECT state FROM budget')],['settled'])


if __name__=='__main__':unittest.main()
