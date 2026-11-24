import hashlib
from http.client import HTTPMessage
import json
import os
from pathlib import Path
import tempfile
import unittest

from psi_sync.reviews import Reviews
from psi_sync.security import authorize, SecurityError
from psi_sync.state import State, mapped
from psi_sync.storage import LocalStore


class Reader:
    fixture = None
    cache_identity = 'synthetic-local-web'
    def __init__(self, item): self.item = item
    def snapshot(self):
        return {'status':'ok', 'counts':{'decision_count':1}, 'counts_present':['decision_count'], 'decisions':[self.item], 'closures':[self.item], 'resources':[], 'systems':[]}
    def detail(self, identifier, known): return self.item


class LocalWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name).resolve(); os.chmod(self.directory, 0o700)
        self.proof = self.directory/'proof.txt'; self.proof.write_text('actual inspected evidence')
        self.item = {'id':'closure:synthetic','kind':'closure','title':'技术故障仍待核查','state':'decision','revision':'7','requires_decision':True,'recommendation':'查明原因','children':[]}
        self.record = {'native_revision':'7','responsibility':'ai','progress':'blocked','basis':'已核查，来源不可用','next_step':'AI核对原来源','question':'','user_action':'','reviewed_at':'2026-11-17T16:00:00+08:00','outcome':'','evidence':[{'path':str(self.proof),'sha256':hashlib.sha256(self.proof.read_bytes()).hexdigest()}]}
        self.save()
        self.reviews = Reviews(self.directory)
        self.store = LocalStore(self.directory,'synthetic-local-web')
        self.reader = Reader(self.item); self.state = State(self.reader,self.store,self.reviews)
    def save(self):
        f=self.directory/'reviews.json';f.write_text(json.dumps({'schema':1,'baseline':[self.item],'reviews':{self.item['id']:self.record}}));os.chmod(f,0o600)
    def row(self): self.state.refresh();return self.state.listing('all')['items'][0]
    def test_ai_problem_is_not_a_user_choice_or_running(self):
        row=self.row();self.assertEqual((row['responsibility'],row['progress'],row['status']),('ai','blocked','automatic'));self.assertEqual(row['question'],'');self.assertEqual(row['answer_options'],[])
    def test_embedded_review_evidence_stays_in_its_own_store(self):
        from unittest.mock import patch
        from psi_sync import reviews as module
        embedded = Reviews(self.directory, local_evidence_only=True)
        self.assertTrue(embedded.evidence_current(self.record['evidence']))
        with tempfile.TemporaryDirectory() as outside:
            other = Path(outside).resolve() / 'proof.txt'
            other.write_bytes(self.proof.read_bytes())
            row = {'path': str(other), 'sha256': self.record['evidence'][0]['sha256']}
            with patch.object(module, 'open_local_file', wraps=module.open_local_file) as reader:
                self.assertFalse(embedded.evidence_current([row]))
                reader.assert_not_called()
            self.assertTrue(self.reviews.evidence_current([row]))

    def test_local_choice_does_not_close_issue(self):
        self.state.refresh()
        from psi_sync.state import version
        self.store.update(lambda d:d['actions'].append({'id':self.item['id'],'action':'resolve_plan','created_at':'x','request_id':'note','revision':version(self.item)}))
        row=self.row();self.assertNotEqual(row['status'],'closed');self.assertIsNotNone(row['local_answer'])
    def test_changed_source_version_removes_human_prompt(self):
        self.record.update(responsibility='user_decision',question='具体个人取舍？');self.save();self.item['revision']='8'
        row=self.row();self.assertEqual(row['responsibility'],'unknown');self.assertEqual(row['question'],'')
    def test_changed_proof_removes_review(self):
        self.assertTrue(self.row()['review_valid']);self.proof.write_text('changed')
        self.assertFalse(self.row()['review_valid'])
    def test_pause_and_actual_completion_are_distinct(self):
        self.record['progress']='paused';self.save();self.assertEqual(self.row()['status'],'waiting')
        self.record.update(progress='resolved',outcome='');self.save();self.assertNotEqual(self.row()['status'],'closed')
        self.record.update(outcome='实物结果已核验',completion_verified=True);self.save();self.assertEqual(self.row()['status'],'closed')
    def test_static_running_claim_is_not_live_execution(self):
        self.record['progress']='running';self.save();self.assertEqual(self.row()['progress'],'pending')
    def test_partial_counts_remain_unknown(self):
        self.state.refresh();self.state.snapshot['status']='partial'
        self.assertIsNone(self.state.listing('open')['counts']['open'])
    def test_missing_review_is_unknown_not_personal(self):
        (self.directory/'reviews.json').unlink();row=self.row();self.assertEqual(row['responsibility'],'unknown')
    def test_missing_baseline_source_stays_visible(self):
        self.state.refresh();self.state.items={}
        row=self.state.listing('all')['items'][0];self.assertTrue(row['source_missing']);self.assertFalse(row['codex_handoff_available'])
    def test_port_specific_origin_host_and_token(self):
        h=HTTPMessage()
        for k,v in {'Host':'127.0.0.1:8768','Origin':'http://127.0.0.1:8768','X-PSI-Session':'token'}.items():h[k]=v
        authorize(h,'token',write=True,api=True,port=8768)
        with self.assertRaises(SecurityError):authorize(h,'token',write=True,api=True,port=8767)
        with self.assertRaises(SecurityError):authorize(h,'wrong',write=True,api=True,port=8768)

    def test_withdrawn_source_never_reconstructs_old_text(self):
        self.item.update(title='PRIVATE_OLD_TITLE', background='PRIVATE_OLD_BACKGROUND')
        self.save();self.state.refresh();self.state.items={};self.state.snapshot=None;self.state.error='scope_withdrawn'
        listing=self.state.listing('open');detail=self.state.detail(self.item['id'])
        encoded=json.dumps([listing,detail,self.state.conversation(self.item['id'])])
        self.assertNotIn('PRIVATE_OLD',encoded)
        self.assertTrue(detail['source_missing']);self.assertFalse(detail['codex_handoff_available'])
        self.assertIsNone(listing['counts']['open'])

    def test_ai_responsibility_filter_also_contains_waiting_work(self):
        self.record['progress']='waiting';self.save();self.state.refresh()
        self.assertEqual(self.state.listing('automatic')['counts']['automatic'],1)
        self.assertEqual(len(self.state.listing('automatic')['items']),1)
        self.assertEqual(len(self.state.listing('waiting')['items']),1)

    def test_problem_and_records_are_disjoint_and_cover_all_work(self):
        for responsibility, progress in [('ai','blocked'), ('unknown','pending'),
                                          ('ai','paused'), ('ai','resolved'),
                                          ('user_decision','pending'), ('user_action','pending')]:
            with self.subTest(responsibility=responsibility, progress=progress):
                self.record.update(responsibility=responsibility, progress=progress,
                                   outcome='已核查实际结果', completion_verified=True)
                self.save(); self.state.refresh()
                issues=self.state.listing('attention'); records=self.state.listing('records')
                issue_ids={r['id'] for r in issues['items']}; record_ids={r['id'] for r in records['items']}
                self.assertFalse(issue_ids & record_ids)
                self.assertEqual(issue_ids | record_ids, {self.item['id']})
                self.assertEqual(bool(issue_ids), responsibility.startswith('user_'))
                self.assertEqual(records['counts']['records'],len(record_ids))

    def test_expired_human_review_moves_to_records_without_old_prompt(self):
        self.record.update(responsibility='user_decision',question='旧选择？',expires_at=1)
        self.save(); self.state.refresh()
        self.assertEqual(self.state.listing('attention')['items'],[])
        record=self.state.listing('records')['items'][0]
        self.assertEqual(record['responsibility'],'unknown');self.assertEqual(record['question'],'')

    def test_unreviewed_history_does_not_become_a_new_task(self):
        self.state.refresh()
        old=dict(self.item,id='closure:old-followup',revision='9',requires_decision=False)
        self.state.items[old['id']]=old
        self.assertEqual(self.state.listing('open')['counts']['all'],1)
        self.assertTrue(any(x['id']==old['id'] for g in self.state.view()['history'] for x in g['items']))

    def test_missing_source_handoff_cannot_write_or_call_core(self):
        from psi_sync.handoff import continue_in_codex, handoff_status
        from psi_sync.core_reader import ReadError
        from unittest.mock import patch
        self.state.refresh();self.state.items={}
        placeholder=self.state.detail(self.item['id']);before=self.store.read()
        with patch('psi_sync.handoff.core_command') as command:
            with self.assertRaises(ReadError):
                continue_in_codex(self.state,self.item['id'],{'revision':placeholder['revision'],'draft':'must not save','content':'must not send'})
            self.assertEqual(handoff_status(self.state,self.item['id'])['phase'],'unavailable')
            command.assert_not_called()
        self.assertEqual(self.store.read(),before)

    def test_expired_live_conflict_review_cannot_keep_asking_user(self):
        self.record.update(responsibility='user_decision',question='已过期的选择？',expires_at=1)
        self.save();row=self.row()
        self.assertFalse(row['review_valid']);self.assertEqual(row['responsibility'],'unknown');self.assertEqual(row['question'],'')

    def test_malformed_review_bundle_becomes_unknown_not_zero(self):
        for value in [[],None,'invalid',{'schema':1,'baseline':[None],'reviews':{}}]:
            with self.subTest(value=value):
                path=self.directory/'reviews.json';path.write_text(json.dumps(value));os.chmod(path,0o600)
                row=self.row();self.assertFalse(row['review_valid']);self.assertEqual(row['question'],'')
                self.assertIsNone(self.state.listing()['counts']['attention'])
                self.assertEqual(self.state.listing()['status'],'partial')

    def test_malformed_review_fields_cannot_disconnect_api(self):
        for key,value in [('responsibility',[]),('progress',{}),('basis',{}),('next_step',[])]:
            with self.subTest(key=key):
                old=self.record[key];self.record[key]=value;self.save()
                row=self.row();self.assertFalse(row['review_valid']);self.assertEqual(row['responsibility'],'unknown')
                self.assertIsNone(self.state.listing()['counts']['attention'])
                self.record[key]=old
