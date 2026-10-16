"""Final delivery binds actual text, scope and current state; no raw text stored."""
import hashlib
import json
from pathlib import Path
from tests.test_task_closure import ClosureTests
from activity_mvp.task_closure import final_wording

class WordingTests(ClosureTests):
    def final_audit(self, text='本次修复全部完成。', completion='complete', remaining=None, **extra):
        response=self.root/'final.md'; response.write_text(text)
        sha=hashlib.sha256(response.read_bytes()).hexdigest()
        artifact=self.root/'independent.json'; artifact.write_text(json.dumps({'final_response_sha256':sha,'wording_approved':True}))
        payload={'task_id':self.task,'evidence':[{'path':str(self.source)}], 'independent':True,'reviewer':'independent fixture',
            'independent_artifact':{'path':str(artifact)},'wording_approved':True,
            'review':{k:'independently read current versions' for k in ('main','related','discovered','failed_steps','verification_gaps','downstream')},
            'final_response':{'path':str(response),'sha256':sha,'completion':completion,'scope':'本次修复','remaining':remaining or [],
                'user_action':{'required':False,'scope':'本次修复','reason':'当前修复由AI完成'}}}
        payload.update(extra)
        self.store.audit(payload); return response,text

    def followup(self, scope='out_of_scope', **extra):
        return {'scope':scope,'summary':'真实实验','blocker':'执行资格未核验且未获本轮启动授权','owner':'来源会话','next_step':'如另行发起，先核验七个前提','continuation':'本轮未安排执行',**extra}

    def test_accepted_stage_does_not_qualify_unbounded_complete_or_no_action(self):
        for text in ('全部完成。无需操作。','本次修复完成。\n无需操作。','**全部完成。**','本次修复完成。\n**无需操作。**','真实实验仍未就绪；本轮未进行真实的答题、排盘或查询。你现在无需操作。'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.final_audit(text,'partial',[self.followup()])

    def test_explicit_stage_completion_with_outside_work_never_dispatches(self):
        _,text=self.final_audit('本次修复已完成。真实实验未启动；来源会话仅跟踪，未安排执行。本次修复暂不需用户操作。','stage_complete',[self.followup()])
        s=self.store.task_status(self.task)
        self.assertTrue(s['all_complete']); self.assertTrue(s['wording_valid']); self.assertTrue(s['delivery_ready'])
        self.assertEqual(s['stop_decision']['decision'],'complete')
        self.assertEqual(s['remaining'],[])
        self.assertTrue(self.store.check_final_response(self.task,text)['valid'])

    def test_actual_all_complete_binds_exact_text_and_no_database_body(self):
        _,text=self.final_audit()
        self.assertTrue(self.store.check_final_response(self.task,text)['valid'])
        raw=' '.join(r[0] for r in self.store.conn.execute('SELECT review FROM closure_audits'))
        self.assertNotIn(text,raw)
        self.assertEqual(self.store.check_final_response(self.task,text+' changed')['reasons'],['native_final_text_mismatch'])

    def test_missing_native_text_and_file_drift_fail_closed(self):
        p,text=self.final_audit()
        self.assertFalse(self.store.check_final_response(self.task,None)['valid'])
        p.write_text(text+' drift')
        self.assertFalse(self.store.task_status(self.task)['wording_valid'])

    def test_old_audit_stays_structurally_complete_without_wording_qualification(self):
        self.audit(); s=self.store.task_status(self.task)
        self.assertTrue(s['all_complete']); self.assertFalse(s['wording_valid']); self.assertFalse(s['delivery_ready'])
        self.assertFalse(self.store.check_final_response(self.task,'全部完成')['valid'])

    def test_in_scope_pending_cannot_claim_complete_and_cannot_be_omitted(self):
        item=self.issue(category='main')
        for comp,followups in [('complete',[self.followup('in_scope',issue_id=item['id'])]),('partial',[])]:
            with self.subTest(comp=comp), self.assertRaises(ValueError):self.final_audit('完成一部分。',comp,followups)
        _,text=self.final_audit('完成一部分。主任务仍由当前AI推进。','partial',[self.followup('in_scope',issue_id=item['id'])])
        self.assertTrue(self.store.check_final_response(self.task,text)['valid']); self.assertFalse(self.store.task_status(self.task)['delivery_ready'])

    def test_choice_and_cancel_state_changes_expire_review(self):
        item=self.issue(category='main')
        self.final_audit('完成一部分。等待真实答复。','partial',[self.followup('in_scope',issue_id=item['id'])])
        self.store.transition(item['id'],'canceled','real user canceled',1)
        self.assertFalse(self.store.task_status(self.task)['wording_valid'])
        self.assertEqual(self.store.get(item['id'])['state'],'canceled')

    def test_required_user_action_and_reviewer_artifact_must_be_bound(self):
        with self.assertRaises(ValueError):self.final_audit(wording_approved=False)
        _,text=self.final_audit('本次修复全部完成。')
        artifact=self.root/'independent.json'; artifact.write_text('{}')
        self.assertFalse(self.store.task_status(self.task)['wording_valid'])

    def test_examples_quotes_and_negation_are_not_completion_claims(self):
        for text in ('> 全部完成。\n本次修复完成。','```\n全部完成。\n```\n本次修复完成。','不得说全部完成。\n本次修复完成。'):
            with self.subTest(text=text):self.final_audit(text,'stage_complete',[self.followup()])

    def test_accepted_evidence_drift_invalidates_completed_wording(self):
        item=self.issue(category='main')
        accepted=self.root/'accepted.txt'; accepted.write_text('independently verified outcome')
        self.store.accept({'issue_id':item['id'],'revision':1,'verified':True,'reviewer':'independent',
            'review':'verified outcome','evidence':[{'path':str(accepted)}],'source_change_explanation':'separate outcome evidence'})
        _,text=self.final_audit()
        accepted.write_text('changed after acceptance')
        s=self.store.task_status(self.task)
        self.assertTrue(s['audit_valid']); self.assertFalse(s['wording_valid']); self.assertFalse(s['delivery_ready'])
        self.assertFalse(self.store.check_final_response(self.task,text)['valid'])

if __name__=='__main__':__import__('unittest').main()
