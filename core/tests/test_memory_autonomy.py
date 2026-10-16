import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from datetime import datetime,timedelta,timezone
from activity_mvp.feedback import memory_operator as op
from activity_mvp.feedback import memory_autonomy as worker
from activity_mvp.feedback.store import Store
from activity_mvp.feedback.work import WorkQueue
from tests.personal_scope_fixtures import register_personal


class MemoryAutonomyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name).resolve()
        register_personal(self, self.root)
        self.source=self.root/'authority.md';self.source.write_text('Report rule: three sections only.\n')
        self.memory={'memory_id':'record-1','text':'Use three report sections.','applies_to':['universal'],'status':'active','review_status':'approved'}
        self.memory['owner_id']='local-owner'
        self.candidate={'memory_id':'record-1','content_sha256':op.sha(self.memory['text']),'candidate_fingerprint':'a'*64,'policy_version':'2026-08-26.1','review_state':'pending'}
        self.binding={'source_id':'shared-rule','path':str(self.source),'selector':'Report rule:',
            'excerpt_sha256':op.sha(self.source.read_text().strip()),'memory_sha256':self.candidate['content_sha256'],
            'applies_to':['universal'],'owner_id':'local-owner','action':'keep_active',
            'valid_until':(datetime.now(timezone.utc)+timedelta(days=10)).isoformat()}
        self.registry={'policy':op.POLICY,'owner_id':'local-owner','actor':op.ACTOR,'enabled':True,'bindings':{'record-1':[self.binding]}}
        self.registry_path=self.root/'policy.json';self.write_registry()
    def tearDown(self):self.tmp.cleanup()
    def write_registry(self):
        self.registry_path.write_text(json.dumps(self.registry));self.registry_path.chmod(0o600)
    def inspect(self):return op.inspect_candidate(self.candidate,self.memory,self.registry)
    def test_blocked_source_read_has_real_process_deadline(self):
        import time
        start=time.monotonic()
        with patch.object(op,'_READ_SCRIPT','import time; time.sleep(30)'):
            with self.assertRaisesRegex(ValueError,'source_read_timeout'):op.bounded(self.source)
        self.assertLess(time.monotonic()-start,5)
    def test_governed_alias_keeps_operation_identity_and_checks_lineage(self):
        self.source.write_text('---\ncore_version: 2026-09-21.1\n---\nReport rule: three sections only.\n')
        self.binding['action']='supersede'
        self.binding['supersede_draft']={'content':'Corrected rule preserving period comparisons.','rationale':'Current authority.','applies_to':['universal']}
        self.write_registry();center=self.center();proof=self.inspect()
        first=op.apply_automatic(center,'record-1',proof['revision'],registry_path=self.registry_path)
        real=self.root/'runtime.md';os.link(self.source,real)
        receipt=self.root/'sync.json'
        receipt_data={'ok':True,'source':{'user_path':str(self.source),'real_source':str(real),'raw_sha256':op.sha(real.read_bytes()),'content_sha256':op.sha(real.read_text().strip()),'core_version':'2026-09-21.1'}}
        receipt.write_text(json.dumps(receipt_data))
        self.binding.update(path=str(real),governed_source={'canonical_path':str(self.source),'receipt_path':str(receipt)})
        self.write_registry()
        self.assertEqual(op.authority_bytes(self.binding),self.source.read_bytes())
        self.assertEqual(proof['revision'],self.inspect()['revision'])
        self.assertTrue(op.authorization_valid(center.config.ledger_path,first['batch_id'],registry_path=self.registry_path))
        with patch.object(center,'apply_batch',side_effect=AssertionError('duplicate write')):
            second=op.apply_automatic(center,'record-1',self.inspect()['revision'],registry_path=self.registry_path)
        self.assertEqual(first,second)
        with center.ledger.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM batches').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM review_events').fetchone()[0],1)
        receipt_data['source']['user_path']='unrelated';receipt.write_text(json.dumps(receipt_data))
        self.assertFalse(op.authorization_valid(center.config.ledger_path,first['batch_id'],registry_path=self.registry_path))
        self.assertIsNone(self.inspect()['proposal'])
        receipt_data['source']['user_path']=str(self.source);receipt.write_text(json.dumps(receipt_data))
        real.write_text(real.read_text()+'Another source version.\n')
        self.assertFalse(op.authorization_valid(center.config.ledger_path,first['batch_id'],registry_path=self.registry_path))
    def test_current_rule_qualifies_but_age_and_similarity_do_not(self):
        self.assertEqual(self.inspect()['proposal']['action'],'keep_active')
        self.registry['bindings']={}
        proof=self.inspect();self.assertIsNone(proof['proposal']);self.assertEqual(proof['gap'],'no_independently_verified_current_claim')
    def test_edited_authority_invalidates_bound_claim(self):
        old=self.inspect()['revision'];self.source.write_text('Report rule: five sections.\n')
        proof=self.inspect();self.assertNotEqual(old,proof['revision']);self.assertIsNone(proof['proposal'])
    def test_forged_claim_file_cannot_certify_itself(self):
        # Merely supplying a new proof document cannot satisfy the registered
        # authority excerpt, even when it contains the same model conclusion.
        self.source.write_text(json.dumps({'approved':True,'claim':'Use three report sections.'}))
        self.assertIsNone(self.inspect()['proposal'])
    def test_owner_scope_and_restricted_memory_never_promoted(self):
        self.binding['owner_id']='someone-else';self.assertIsNone(self.inspect()['proposal'])
        self.binding['owner_id']='local-owner';self.binding['applies_to']=['project:other'];self.assertIsNone(self.inspect()['proposal'])
        self.binding['applies_to']=['universal'];self.memory['status']='quarantined';self.assertIsNone(self.inspect()['proposal'])
    def test_conflicting_evidence_waits(self):
        self.registry['bindings']['record-1'].append(dict(self.binding,action='mark_legacy'))
        self.assertIsNone(self.inspect()['proposal']);self.assertIn('evidence_conflict',self.inspect()['gap'])
    def test_expired_binding_and_symlink_source_rejected(self):
        self.binding['valid_until']='2000-01-01T00:00:00+00:00';self.assertIsNone(self.inspect()['proposal'])
        self.source.unlink();self.source.symlink_to(self.registry_path)
        with self.assertRaises(ValueError):op.bounded(self.source)
    def test_unchanged_worker_is_idempotent_and_changed_source_reopens(self):
        with Store(self.root/'store') as store:
            store.set('autonomy',{'policy':op.POLICY});store.set('config',{'enabled':True})
            with patch.object(worker,'load_registry',return_value=self.registry),patch.object(worker,'memory_review_candidates',return_value={'items':[self.candidate]}),patch.object(worker,'read_memory_text',return_value=self.memory),patch.object(worker,'apply',return_value={'verified':True}) as apply:
                self.assertEqual(worker.tick(store)['applied'],1)
                self.assertEqual(worker.tick(store)['processed'],0);apply.assert_called_once()
                self.source.write_text('Report rule: changed.\n')
                self.assertEqual(worker.tick(store)['processed'],1)
                self.assertEqual(WorkQueue(store).items('memory')[0]['state'],'waiting_evidence')
                self.assertEqual(store.rows('SELECT * FROM decisions'),[])
    def test_failed_operator_does_not_retry_without_new_evidence(self):
        with Store(self.root/'store') as store:
            store.set('autonomy',{'policy':op.POLICY});store.set('config',{'enabled':True})
            with patch.object(worker,'load_registry',return_value=self.registry),patch.object(worker,'memory_review_candidates',return_value={'items':[self.candidate]}),patch.object(worker,'read_memory_text',return_value=self.memory),patch.object(worker,'apply',side_effect=RuntimeError('writer failure')) as apply:
                worker.tick(store);worker.tick(store);apply.assert_called_once()
                self.assertEqual(WorkQueue(store).items('memory')[0]['state'],'waiting_evidence')
    def center(self):
        # Use the installed review-center implementation against temporary
        # ledger/fake owner-scoped records; never touch its runtime ledger.
        root=Path.home()/'.hermes/memory-review-center/review_center'
        if not root.is_dir():self.skipTest('review-center integration dependency unavailable')
        package=types.ModuleType('test_review_center');package.__path__=[str(root)];sys.modules['test_review_center']=package
        service=importlib.import_module('test_review_center.service');ledger=importlib.import_module('test_review_center.ledger')
        adapters=importlib.import_module('test_review_center.adapters');config=importlib.import_module('test_review_center.config')
        outer=self
        class Reader:
            def get(self,memory_id):return adapters.MemoryRecord(memory_id,'local-owner',outer.memory['text'],{k:v for k,v in outer.memory.items() if k not in {'text','memory_id'}})
        class Writer:
            def create_and_supersede(self,**args):return {'superseding_memory_id':'new-record','supersedes':args['memory_id']}
        center=service.ReviewCenter(config=config.ReviewCenterConfig(queue_v2_path=self.root/'queue.json',candidate_dir=self.root,ledger_path=self.root/'ledger.sqlite',capability_token='x'*32),ledger=ledger.Ledger(self.root/'ledger.sqlite'),memory_reader=Reader(),supersede_adapter=Writer(),publish_adapter=adapters.DisabledPublishAdapter())
        center.queue=lambda:{'items':[self.candidate],'policy_version':self.candidate['policy_version'],'source_sha256':'b'*64,'source_snapshot_sha256':'c'*64}
        return center
    def test_real_ledger_apply_actor_no_human_confirmation_and_idempotency(self):
        center=self.center();proof=self.inspect()
        r=op.apply_automatic(center,'record-1',proof['revision'],registry_path=self.registry_path)
        self.assertTrue(r['verified']);self.assertFalse(r['human_confirmed'])
        self.assertTrue(op.authorization_valid(center.config.ledger_path,r['batch_id'],registry_path=self.registry_path))
        r2=op.apply_automatic(center,'record-1',proof['revision'],registry_path=self.registry_path);self.assertEqual(r,r2)
        with center.ledger.connect() as db:
            self.assertIsNone(db.execute('SELECT confirmed_at FROM batches').fetchone()[0])
            self.assertTrue(db.execute('SELECT reviewer FROM batch_items').fetchone()[0].startswith(op.ACTOR+':'))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM review_events').fetchone()[0],1)
        self.source.write_text('Report rule: different.\n')
        self.assertFalse(op.authorization_valid(center.config.ledger_path,r['batch_id'],registry_path=self.registry_path))
    def test_real_ledger_refuses_forged_revision_and_owner(self):
        center=self.center()
        with self.assertRaisesRegex(ValueError,'evidence_changed'):op.apply_automatic(center,'record-1','f'*64,registry_path=self.registry_path)
        self.registry['owner_id']='third-party';self.write_registry()
        with self.assertRaisesRegex(ValueError,'not_enabled'):op.apply_automatic(center,'record-1',self.inspect()['revision'],registry_path=self.registry_path)
        with center.ledger.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM batches').fetchone()[0],0)
    def test_real_ledger_new_source_revision_reconfirms_without_human_click(self):
        center=self.center();p=self.inspect();r=op.apply_automatic(center,'record-1',p['revision'],registry_path=self.registry_path)
        self.source.write_text(self.source.read_text()+'Unrelated new section.\n')
        p2=self.inspect();self.assertNotEqual(p['revision'],p2['revision'])
        r2=op.apply_automatic(center,'record-1',p2['revision'],registry_path=self.registry_path)
        self.assertNotEqual(r['batch_id'],r2['batch_id'])
        self.assertTrue(op.authorization_valid(center.config.ledger_path,r2['batch_id'],registry_path=self.registry_path))
    def test_automatic_authorization_rejects_forged_evidence_and_registry_changes(self):
        center=self.center();r=op.apply_automatic(center,'record-1',self.inspect()['revision'],registry_path=self.registry_path)
        with center.ledger.connect() as db:
            db.execute('UPDATE automatic_authorizations SET evidence_json=?',(json.dumps([{'kind':'model_claim','valid':True}]),))
        self.assertFalse(op.authorization_valid(center.config.ledger_path,r['batch_id'],registry_path=self.registry_path))
    def test_interrupted_batch_creation_recovers_original_batch(self):
        center=self.center();create=center.ledger.create_batch
        def crash(actions):create(actions);raise RuntimeError('simulated_process_exit')
        with patch.object(center.ledger,'create_batch',side_effect=crash):
            with self.assertRaises(RuntimeError):op.apply_automatic(center,'record-1',self.inspect()['revision'],registry_path=self.registry_path)
        r=op.apply_automatic(center,'record-1',self.inspect()['revision'],registry_path=self.registry_path)
        self.assertTrue(r['verified'])
        with center.ledger.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM batches').fetchone()[0],1)
    def test_worker_pause_prevents_operator_and_external_human_decision_wins(self):
        with Store(self.root/'store') as store:
            store.set('config',{'enabled':True});store.set('autonomy',{'policy':op.POLICY,'paused':True})
            with patch.object(worker,'apply') as apply:
                self.assertFalse(worker.tick(store)['enabled']);apply.assert_not_called()
            store.set('autonomy',{'policy':op.POLICY})
            WorkQueue(store).enqueue('memory','record-1','old')
            self.candidate['review_state']='decided'
            with patch.object(worker,'load_registry',return_value=self.registry),patch.object(worker,'memory_review_candidates',return_value={'items':[self.candidate]}),patch.object(worker,'current_decision_is_automatic',return_value=False),patch.object(worker,'apply') as apply:
                worker.tick(store);apply.assert_not_called()
                self.assertEqual(WorkQueue(store).items('memory')[0]['state'],'resolved')
    def test_unseen_memory_exact_authority_claim_qualifies_similarity_does_not(self):
        self.registry['bindings']={}
        self.registry['authority_sources']=[{k:self.binding[k] for k in ('source_id','path','owner_id','applies_to','valid_until')}]
        self.memory.update(memory_id='brand-new-record',text='Report rule: three sections only.')
        self.candidate.update(memory_id='brand-new-record',content_sha256=op.sha(self.memory['text']))
        proof=self.inspect();self.assertEqual(proof['proposal']['action'],'keep_active')
        self.memory['text']='The report should probably have three sections.'
        self.candidate['content_sha256']=op.sha(self.memory['text'])
        self.assertIsNone(self.inspect()['proposal'])
    def test_generic_automatic_release_is_version_bound(self):
        self.registry['bindings']={}
        self.registry['authority_sources']=[{k:self.binding[k] for k in ('source_id','path','owner_id','applies_to','valid_until')}]
        self.memory['text']='Report rule: three sections only.';self.candidate['content_sha256']=op.sha(self.memory['text']);self.write_registry()
        center=self.center();r=op.apply_automatic(center,'record-1',self.inspect()['revision'],registry_path=self.registry_path)
        self.assertTrue(op.authorization_valid(center.config.ledger_path,r['batch_id'],registry_path=self.registry_path))
        self.source.write_text('Report rule: use five sections instead.')
        self.assertFalse(op.authorization_valid(center.config.ledger_path,r['batch_id'],registry_path=self.registry_path))
    def test_code_examples_cannot_authorize_whole_claim(self):
        self.registry['bindings'] = {}
        self.registry['authority_sources'] = [{k:self.binding[k] for k in
            ('source_id', 'path', 'owner_id', 'applies_to', 'valid_until')}]
        self.memory['text'] = 'Report rule: three sections only.'
        self.candidate['content_sha256'] = op.sha(self.memory['text'])
        for example in (
            '~~~text\n{claim}\n~~~',
            '````text\n```\n{claim}\n````',
            '~~~text\n```\n{claim}\n~~~',
            '```text\n{claim}',
            '~~~text\n{claim}',
            '    {claim}',
            '\t{claim}',
            '  \t{claim}',
            '> {claim}',
            '> Example paragraph\n{claim}',
            '~~~text <!-- comment -->\n{claim}\n~~~',
            '```text <!-- comment -->\n{claim}\n```',
            '<!--\n{claim}\n-->',
            '<!-- comment -->{claim}',
        ):
            with self.subTest(example=example):
                self.source.write_text(example.format(claim=self.memory['text']))
                self.assertIsNone(self.inspect()['proposal'])
                self.assertFalse(self.inspect()['evidence'][0]['valid'])
        self.source.write_text('~~~example\nNot a current rule.\n~~~~\n' + self.memory['text'])
        self.assertEqual(self.inspect()['proposal']['action'], 'keep_active')
        for prefix in (
            '~~~text <!-- comment -->\n<!-- literal unclosed comment\n~~~\n',
            '<!--\n```text\n-->\n',
            '    <!-- literal code\n',
            '> <!-- literal quote\n\n',
            '> Example paragraph\nQuoted continuation.\n\n',
            '<!-- first --> <!-- second -->\n',
        ):
            with self.subTest(recovery=prefix):
                self.source.write_text(prefix + self.memory['text'])
                self.assertEqual(self.inspect()['proposal']['action'], 'keep_active')

    def test_old_whole_claim_receipt_rechecks_prose_extraction(self):
        self.registry['bindings'] = {}
        self.registry['authority_sources'] = [{k:self.binding[k] for k in
            ('source_id', 'path', 'owner_id', 'applies_to', 'valid_until')}]
        self.memory['text'] = 'Report rule: three sections only.'
        self.candidate['content_sha256'] = op.sha(self.memory['text'])
        self.source.write_text('~~~text\n' + self.memory['text'] + '\n~~~')
        self.write_registry()
        # Reproduce a receipt accepted by the old parser, then check the same
        # unchanged source bytes through the current read-side fence.
        center = self.center()
        with patch.object(op, 'authority_claims', return_value=[self.memory['text']]):
            result = op.apply_automatic(center, 'record-1', self.inspect()['revision'],
                registry_path=self.registry_path)
        self.assertFalse(op.authorization_valid(center.config.ledger_path,
            result['batch_id'], registry_path=self.registry_path))
        self.assertIsNotNone(center.ledger.get_batch(result['batch_id']))

    def test_staged_writer_records_automatic_metadata_and_rejects_invalid_actor(self):
        import ast,hashlib,re,importlib.util
        stage_path=Path(__file__).resolve().parents[2]/'tools/autonomy-memory-center-patch.py'
        spec=importlib.util.spec_from_file_location('stage_memory_patch',stage_path);stage=importlib.util.module_from_spec(spec);spec.loader.exec_module(stage)
        if not (stage.ENGINE/'mem0_engine.py').exists():self.skipTest('shared writer not installed')
        manifest=stage.stage(self.root/'staged')
        engine=next(Path(e['staged']) for e in manifest if e['target'].endswith('/mem0_engine.py'))
        node=next(n for n in ast.parse(engine.read_text()).body if isinstance(n,ast.FunctionDef) and n.name=='_supersede_owned_memory')
        captured=[]
        class Memory:
            def add(self,records,**kwargs):captured.append(kwargs);return {'results':[{'event':'ADD','memory':records[0]['content'],'id':'new-record'}]}
        env={'re':re,'hashlib':hashlib,'os':os,'REVIEW_LEDGER_FILE':self.root/'ledger.sqlite',
            '_require_requested_owner':lambda v:'local-owner','_validate_durable_text':lambda v:None,
            '_require_accessible_memory':lambda *args:{'memory':self.memory['text'],'metadata':{}},'_memory_content':lambda x:x['memory'],
            '_govern_write':lambda *args,**kw:{'metadata':{}},
            '_find_review_operation':lambda *args,**kw:'new-record' if captured else None,
            '_base_metadata':lambda *args,**kw:dict(kw['governance'],agent_id=kw['agent_id']),
            '_created_memory_id':lambda x:'new-record'}
        exec(compile(ast.Module(body=[node],type_ignores=[]),str(engine),'exec'),env)
        helper=types.ModuleType('memory_autonomy_operator');helper.automatic_actor=lambda *args:op.ACTOR
        data={'memory_id':'record-1','content_sha256':op.sha(self.memory['text']),'idempotency_key':'d'*64,'supersede_draft':{'content':'Corrected stable statement.'},'applies_to':['universal']}
        with patch.dict(sys.modules,{'memory_autonomy_operator':helper}):
            env['_supersede_owned_memory'](Memory(),data)
            self.assertEqual(captured[0]['metadata']['human_review_status'],'not_required')
            self.assertEqual(captured[0]['metadata']['review_actor'],op.ACTOR)
            self.assertEqual(captured[0]['agent_id'],op.ACTOR);self.assertFalse(captured[0]['infer'])
            helper.automatic_actor=lambda *args:(_ for _ in ()).throw(ValueError('forged_authority'))
            with self.assertRaisesRegex(ValueError,'forged_authority'):env['_supersede_owned_memory'](Memory(),data)
            self.assertEqual(len(captured),1)
    def test_operator_preserves_current_human_decision(self):
        center=self.center();r=op.apply_automatic(center,'record-1',self.inspect()['revision'],registry_path=self.registry_path)
        with center.ledger.connect() as db:
            db.execute("UPDATE batches SET confirmed_at='2026-09-21T03:00:00Z' WHERE batch_id=?",(r['batch_id'],))
        with self.assertRaisesRegex(ValueError,'human_decision_takes_precedence'):
            op.apply_automatic(center,'record-1',self.inspect()['revision'],registry_path=self.registry_path)
    def test_dependent_retirement_waits_for_verified_replacement_and_rechecks_body(self):
        center=self.center();old_hash=self.candidate['content_sha256']
        corrected='Corrected stable rule preserving comparison periods.'
        self.binding['action']='supersede';self.binding['supersede_draft']={'content':corrected,'applies_to':['universal'],'rationale':'Current authoritative rule.'};self.write_registry()
        # No replacement exists yet: the explicit dependency cannot self-certify.
        dependency={'source_memory_id':'record-1','source_content_sha256':old_hash,'replacement_sha256':op.sha(corrected),'applies_to':['universal']}
        with self.assertRaisesRegex(ValueError,'replacement_not_yet_verified'):
            op.replacement_evidence(dependency,ledger_path=center.config.ledger_path,registry_path=self.registry_path)
        op.apply_automatic(center,'record-1',self.inspect()['revision'],registry_path=self.registry_path)
        replacement={'text':corrected,'owner':'local-owner'}
        class Reader:
            def __init__(self,*args,**kwargs):pass
            def get(self,key):return types.SimpleNamespace(memory_id=key,owner_id=replacement['owner'],content=replacement['text'],provenance={'applies_to':['universal'],'status':'active','review_status':'approved'})
        module=types.ModuleType('_agi_owner_scoped_memory_reader');module.ChromaSqliteReadAdapter=Reader
        original=op.replacement_evidence
        def local_evidence(dep,**kwargs):
            return original(dep,ledger_path=kwargs.get('ledger_path',center.config.ledger_path),registry_path=kwargs.get('registry_path',self.registry_path),visited=kwargs.get('visited',frozenset()))
        with patch.dict(sys.modules,{'_agi_owner_scoped_memory_reader':module}),patch.object(op,'replacement_evidence',side_effect=local_evidence):
            self.memory.update(memory_id='record-2',text='Earlier report order and comparison periods.')
            self.candidate.update(memory_id='record-2',content_sha256=op.sha(self.memory['text']))
            self.registry['bindings']['record-2']=[dict(self.binding,action='mark_legacy',memory_sha256=self.candidate['content_sha256'],replacement_dependency=dependency)]
            self.registry['bindings']['record-2'][0].pop('supersede_draft');self.write_registry()
            proof=self.inspect();self.assertEqual(proof['proposal']['action'],'mark_legacy')
            r=op.apply_automatic(center,'record-2',proof['revision'],registry_path=self.registry_path)
            self.assertEqual(r['action'],'mark_legacy')
            replacement['text']='Unrelated replacement.'
            self.assertIsNone(self.inspect()['proposal'])
            self.assertFalse(op.authorization_valid(center.config.ledger_path,r['batch_id'],registry_path=self.registry_path))
    def test_real_ledger_historical_and_supersede_use_existing_writer(self):
        center=self.center();self.binding['action']='mark_legacy';self.write_registry()
        r=op.apply_automatic(center,'record-1',self.inspect()['revision'],registry_path=self.registry_path)
        self.assertEqual(r['action'],'mark_legacy')
        self.binding['action']='supersede';self.binding['supersede_draft']={'content':'Corrected stable rule.','rationale':'Current authoritative rule changed.','applies_to':['universal']};self.write_registry()
        r=op.apply_automatic(center,'record-1',self.inspect()['revision'],registry_path=self.registry_path)
        with center.ledger.connect() as db:self.assertEqual(db.execute('SELECT superseding_memory_id FROM review_decisions').fetchone()[0],'new-record')

if __name__=='__main__':unittest.main()
