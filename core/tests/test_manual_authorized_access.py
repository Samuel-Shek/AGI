"""Owner manual use must not confer scheduler, memory or sending authority."""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from activity_mvp import task_closure as core
from activity_mvp.source_governance import manual_access as access


class AuthorizedManualTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name).resolve()
        self.database=self.root/'activity.sqlite'
        self.store=core.Store(self.database)
        self.file=self.root/'read-input.txt';self.file.write_text('isolated input')
        self.output=self.root/'output.txt';self.output.write_text('isolated output')
        self.task_id=self.store.begin({'platform':'codex','session':'native-test','turn':'current',
            'objective':'isolated owner-authorized check','scope_root':str(self.root),'complex':True})['task_id']
        self.task=dict(self.store.conn.execute('SELECT * FROM closure_tasks WHERE id=?',(self.task_id,)).fetchone())
        self.native={k:self.task[k] for k in ('platform','session','turn')};self.native['task_id']=self.task_id
        self.issue=self.store.register({'task_id':self.task_id,'key':'scope','category':'main',
            'summary':'write A and read B','target_root':str(self.root),'evidence':[{'path':str(self.file)}],
            'authority':{'source':'synthetic human authorization','scope':{'files':[str(self.output)]},
                'reversible':True,'cost':False,'new_permission':False,'external':False,'personal_choice':False},
            'recommendation':'verify','acceptance_rule':'bytes match'})['issue']
        self.policy=self.root/'policy.json'
        self.policy_value={'version':access.VERSION,'owner_id':'local-owner','enabled':True,
            'manual_operations':['read','closure','backup'],'automatic_execution':False,
            'memory_write':False,'external_send':False,'authorization_source':'fixture',
            'authorization_session':'fixture','authorization_turn':'fixture'}
        self.write(self.policy,self.policy_value)
        self.permits=self.root/'permits.json';self.write(self.permits,{'version':1,'owner_id':'local-owner','project_grants':[]})
        self.registry=self.root/'registry.json';self.write(self.registry,{'workplace':{'active':True,'at':'batch'}})
        self.bridge=self.root/'bridge.json';self.write(self.bridge,{'retirement_active':True})
        self.runtime=self.root/'runtime.json'
        fixed=Path.home()/'Library/Application Support/Personal AI Monitor/core'
        self.write(self.runtime,{'core':str(fixed),'database':str(fixed/'data/activity.sqlite')})
        original_store=core.Store
        self.patches=[patch.object(access,'POLICY',self.policy),patch.object(access,'RUNTIME',self.runtime),
            patch.object(access,'_load_core',return_value=core),patch.object(core,'MANUAL_CLOSURE_PERMITS',self.permits),
            patch.object(core,'MANUAL_RETIREMENT_REGISTRY',self.registry),patch.object(core,'MANUAL_RETIREMENT_BRIDGE',self.bridge),
            patch.object(core,'Store',side_effect=lambda *a,**k: original_store(self.database,readonly=True)),
            patch.object(original_store,'recovery_caller',return_value=self.native),
            patch.object(original_store,'native_root',return_value=True),
            patch.object(core.personal_scope,'company_stopped',return_value=True),
            patch.object(core.personal_scope,'classify',return_value='company')]
        for p in self.patches:p.start()

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.store.conn.close();self.temp.cleanup()

    def write(self,path,value):path.write_text(json.dumps(value));path.chmod(0o600)

    def read(self,path=None):return access.read_authorized(self.issue['id'],str(path or self.file))

    def denied_without_body(self):
        with patch.object(access,'_read_owned',side_effect=AssertionError('body read before denial')):
            with self.assertRaises((PermissionError,ValueError,OSError)):self.read()

    def test_authorized_read_evidence_outside_write_targets(self):
        self.assertEqual(self.read()['text'],'isolated input')
        self.assertTrue(core.manual_closure_allowed(self.task,self.issue))
        self.assertTrue(core.automatic_retired(self.issue))
        self.assertFalse(core.personal_scope.admitted({'root':str(self.root)}))

    def test_read_scope_is_exact_file_not_evidence_directory(self):
        other=self.root/'other.txt';other.write_text('unauthorized')
        with self.assertRaisesRegex(PermissionError,'not_authorized'):self.read(other)

    def test_unknown_native_identity_denied_before_body(self):
        with patch.object(self.store.__class__,'recovery_caller',return_value={**self.native,'session':'foreign'}):
            self.denied_without_body()

    def test_stale_native_turn_denied_before_body(self):
        with patch.object(self.store.__class__,'native_root',return_value=False):self.denied_without_body()

    def test_wrong_runtime_never_opens_store(self):
        self.write(self.runtime,{'core':'/untrusted','database':'/untrusted/data.sqlite'})
        with patch.object(core,'Store',side_effect=AssertionError('opened unregistered database')):
            self.denied_without_body()

    def test_revoked_general_policy_denied_before_body(self):
        self.write(self.policy,{**self.policy_value,'enabled':False});self.denied_without_body()

    def test_explicit_revocation_precedes_evidence_read(self):
        self.write(self.permits,{'version':1,'owner_id':'local-owner','project_grants':[
            {'platform':'codex','root':str(self.root),'enabled':False}]})
        self.denied_without_body()

    def test_revocation_during_read_never_returns_content(self):
        original=access._read_owned
        def reading(*args,**kwargs):
            raw=original(*args,**kwargs)
            self.write(self.permits,{'version':1,'owner_id':'local-owner','project_grants':[
                {'platform':'codex','root':str(self.root),'enabled':False}]})
            return raw
        with patch.object(access,'_read_owned',side_effect=reading):
            with self.assertRaisesRegex(PermissionError,'authorization_changed'):self.read()

    def test_symlink_ancestor_denied_before_any_body(self):
        folder=self.root/'folder';folder.mkdir();source=folder/'input.txt';source.write_text('private')
        evidence=json.dumps([{'path':str(source),'sha256':hashlib.sha256(source.read_bytes()).hexdigest()}])
        self.store.conn.execute('UPDATE closure_issues SET evidence=? WHERE id=?',(evidence,self.issue['id']))
        folder.rename(self.root/'moved');folder.symlink_to(self.root/'moved',target_is_directory=True)
        with patch.object(access,'_read_owned',side_effect=AssertionError('body read')):
            with self.assertRaises((ValueError,OSError)):self.read(self.output)

    def test_hardlinked_source_denied(self):
        os.link(self.file,self.root/'alias.txt');self.denied_without_body()

    def test_unknown_file_owner_rejected_on_open_descriptor(self):
        with patch.object(access.os,'getuid',return_value=os.getuid()+1), patch.object(access.os,'read') as read:
            with self.assertRaisesRegex(PermissionError,'owner_or_type'):
                access._read_owned(str(self.file),self.root)
            read.assert_not_called()

    def test_project_revocation_during_closure_metadata_check(self):
        original=core._manual_path_identity
        def check(path):
            value=original(path)
            self.write(self.permits,{'version':1,'owner_id':'local-owner','project_grants':[
                {'platform':'codex','root':str(self.root),'enabled':False}]})
            return value
        with patch.object(core,'_manual_path_identity',side_effect=check):
            self.assertFalse(access.closure_override(self.task,self.issue,core=core))

    def test_evidence_change_denied(self):
        self.file.write_text('changed')
        with self.assertRaisesRegex(PermissionError,'evidence_changed'):self.read()

    def test_waiting_or_canceled_task_never_reactivated(self):
        for state in ('decision','canceled','dismissed'):
            self.store.conn.execute('UPDATE closure_issues SET state=? WHERE id=?',(state,self.issue['id']))
            self.denied_without_body()
            self.assertEqual(self.store.get(self.issue['id'])['state'],state)

    def test_pure_personal_uses_existing_path(self):
        with patch.object(core.personal_scope,'classify',return_value='personal'):
            self.assertIsNone(access.closure_override(self.task,self.issue,core=core))
            self.assertFalse(core.manual_owner_retired(self.task,self.issue))


if __name__=='__main__':unittest.main()
