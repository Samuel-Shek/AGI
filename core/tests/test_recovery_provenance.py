"""Offline native-copy/cleanup contracts for the existing minimal-recovery approval."""
from pathlib import Path
import copy
import json
import os
import unittest
from unittest.mock import patch
from activity_mvp.feedback import recovery_storage as storage
from tests import test_external_recovery as external_tests

ATTRIBUTE = 'com.apple.provenance'

class PublicRecoveryPolicyTests(unittest.TestCase):
    def test_unconfigured_policy_does_not_read_local_approval(self):
        with patch.object(Path, 'home', side_effect=AssertionError("unexpected read")):
            self.assertIsNone(storage.provenance_approval(None))


class RecoveryProvenanceTests(unittest.TestCase):
    def setUp(self):
        external_tests.ExternalRecoveryTests.setUp(self)
        self.policy={'path':'owner-decision-fixture','sha256':'a'*64}
        self.enterContext(patch.object(storage,'provenance_approval',return_value=self.policy,create=True))
        self.source_value='b3JpZ2luYWw='
        self.target_value='Y29weQ=='
        self.extra={}
        self.moved=None
        self.original_attrs=self.b.attrs
        def attrs(path):
            row=self.original_attrs(path)
            if 'payload' in Path(path).parts:
                row[ATTRIBUTE]=self.target_value;row.update(self.extra)
            elif Path(path)==self.code:
                row.pop(ATTRIBUTE,None)
                if self.source_value is not None:row[ATTRIBUTE]=self.source_value
            elif Path(path)==self.moved:
                row[ATTRIBUTE]='cmVuYW1lZA=='
            return row
        self.enterContext(patch.object(self.b,'attrs',side_effect=attrs))
    writer=external_tests.ExternalRecoveryTests.writer
    prepare=external_tests.ExternalRecoveryTests.prepare

    def test_approved_copy_keeps_original_and_exact_target_and_reuses_same_package(self):
        target,local=self.prepare()
        proof=storage.check_external(target,local)
        original=proof['source_bindings'][str(self.code)]
        self.assertEqual(original['original_manifest']['.']['xattrs'][ATTRIBUTE],self.source_value)
        self.assertEqual(original['source_observed_manifest'],original['original_manifest'])
        self.assertEqual(proof['manifest']['old.py']['xattrs'][ATTRIBUTE],self.target_value)
        self.assertEqual(original['original_provenance']['.'],{'present':True,'value_base64':self.source_value})
        self.assertEqual(local.read_bytes(),(target/'prepared.json').read_bytes())
        identity=self.b.tree_identity(target/'payload')
        self.assertEqual(self.prepare(),(target,local))
        self.assertEqual(self.b.tree_identity(target/'payload'),identity)

    def test_missing_original_attribute_is_preserved_as_absent(self):
        self.source_value=None
        target,local=self.prepare();proof=storage.check_external(target,local)
        row=proof['source_bindings'][str(self.code)]
        self.assertEqual(row['original_provenance']['.'],{'present':False,'value_base64':None})
        self.assertNotIn(ATTRIBUTE,row['original_manifest']['.']['xattrs'])

    def test_unapproved_or_other_attribute_difference_blocks_before_publication(self):
        with patch.object(storage,'provenance_approval',return_value=None,create=True):
            with self.assertRaisesRegex(storage.RecoveryDeferred,'source_or_copy_changed'):self.prepare()
        partial=next(self.external.rglob('.release-test.partial'));identity=[partial.stat().st_dev,partial.stat().st_ino]
        self.extra={'user.unrelated':'dGFtcGVy'}
        with self.assertRaisesRegex(storage.RecoveryDeferred,'source_or_copy_changed'):self.prepare()
        self.assertEqual([partial.stat().st_dev,partial.stat().st_ino],identity)
        self.extra={}
        target,local=self.prepare();self.assertEqual([target.stat().st_dev,target.stat().st_ino],identity)
        storage.check_external(target,local)

    def test_source_provenance_change_during_copy_is_not_authorized(self):
        original=self.b.copy_tree
        def changed(*args):
            original(*args);self.source_value='Y2hhbmdlZA=='
        with patch.object(self.b,'copy_tree',side_effect=changed):
            with self.assertRaisesRegex(storage.RecoveryDeferred,'source_or_copy_changed'):self.prepare()
        self.assertTrue(self.code.exists())

    def test_target_provenance_readback_and_policy_revocation_remain_strict(self):
        target,local=self.prepare();guard=storage.retained_guard(target,local)
        self.target_value='bGF0ZXI='
        with self.assertRaisesRegex(ValueError,'readback_changed'):storage.check_external(target,local)
        self.target_value='Y29weQ=='
        with patch.object(storage,'provenance_approval',return_value=None):
            with self.assertRaisesRegex(ValueError,'approval_changed'):storage.check_external(target,local)
            with self.assertRaisesRegex(ValueError,'approval_changed'):guard()

    def test_source_mapping_and_original_manifest_cannot_be_rebound(self):
        target,local=self.prepare();saved=json.loads(local.read_text())
        for kind in ('original','missing','mapping','other_attribute','local'):
            bad=copy.deepcopy(saved);bound=bad['source_bindings'][str(self.code)]
            if kind=='original':bound['original_manifest']['.']['xattrs'][ATTRIBUTE]='dGFtcGVy'
            if kind=='missing':del bound['original_manifest']
            if kind=='mapping':bound['payload_relative']='../old.py'
            if kind=='other_attribute':bad['manifest']['old.py']['mode']=0
            if kind=='local':bad['local_receipt']=str(self.root/'wrong')
            self.b.atomic(target/'prepared.json',bad);self.b.atomic(local,bad)
            with self.subTest(kind=kind),self.assertRaises(ValueError):storage.check_external(target,local)
        self.b.atomic(target/'prepared.json',saved);self.b.atomic(local,saved)

    def cleanup(self,target,local):
        proof=storage.check_external(target,local);checker=storage.original_checker(proof)
        checker(self.code)
        self.moved=self.code.with_suffix('.old');self.code.rename(self.moved)
        return checker,{'target':str(self.code),'stage':str(self.moved)},storage.retained_guard(target,local)

    def test_rename_provenance_cleanup_preserves_dual_exact_observation(self):
        target,local=self.prepare();checker,entry,guard=self.cleanup(target,local)
        checker.cleanup(entry,guard)
        self.assertFalse(self.moved.exists())
        external=list((target/'cleanup-attestations').glob('*.json'));self.assertEqual(len(external),1)
        internal=local.parent/(local.stem+'.cleanup-attestations')/external[0].name
        self.assertEqual(external[0].read_bytes(),internal.read_bytes())
        self.assertEqual(json.loads(internal.read_text())['observed_manifest']['.']['xattrs'][ATTRIBUTE],'cmVuYW1lZA==')
        storage.check_external(target,local)

    def test_cleanup_requires_both_durable_receipts_before_removing_original(self):
        target,local=self.prepare();checker,entry,guard=self.cleanup(target,local)
        original=self.b.publish_attestation
        def failed(path,value):
            if Path(path).is_relative_to(self.local):raise OSError('local attestation unavailable')
            return original(path,value)
        with patch.object(self.b,'publish_attestation',side_effect=failed):
            with self.assertRaisesRegex(OSError,'attestation unavailable'):checker.cleanup(entry,guard)
        self.assertTrue(self.moved.exists())
        checker.cleanup(entry,guard);self.assertFalse(self.moved.exists())
        self.assertEqual(len(list((target/'cleanup-attestations').glob('*.json'))),1)

    def test_cleanup_rejects_content_change_and_same_bytes_different_object(self):
        target,local=self.prepare();checker,entry,guard=self.cleanup(target,local)
        mode=self.moved.stat().st_mode;self.moved.chmod(0o600 if mode&0o777!=0o600 else 0o644)
        with self.assertRaisesRegex(ValueError,'remainder_changed'):checker.cleanup(entry,guard)
        self.moved.chmod(mode)
        other=self.moved.with_suffix('.replacement');other.write_bytes(self.moved.read_bytes());os.replace(other,self.moved)
        with self.assertRaisesRegex(ValueError,'identity_changed'):checker.cleanup(entry,guard)
        self.assertTrue(self.moved.exists())

    def test_cleanup_stops_if_new_dual_attestation_changes_before_deletion(self):
        target,local=self.prepare();checker,entry,guard=self.cleanup(target,local)
        original=self.b.remove_verified
        def tamper(*args):
            receipt=next((local.parent/(local.stem+'.cleanup-attestations')).glob('*.json'))
            receipt.write_text('{}')
            return original(*args)
        with patch.object(self.b,'remove_verified',side_effect=tamper):
            with self.assertRaisesRegex(ValueError,'attestation_changed'):checker.cleanup(entry,guard)
        self.assertTrue(self.moved.exists())

class RecoveryApprovalTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.b=external_tests.require_backup_manager(self)
        self.enterContext(patch.object(storage.Path,'home',return_value=self.root))
        self.path=self.root/'Library/Application Support/PSI/recovery/provenance-approval.json'
        self.path.parent.mkdir(parents=True)
        self.value={'schema':'file-organization-provenance-approval-v1','status':'approved',
            'scope':'already_authorized_file_organization_archive_and_minimal_recovery','allowed_attributes':[ATTRIBUTE],
            **{k:True for k in ('preserve_original_manifest','read_back_actual_target_attributes','all_other_fields_strict',
                'does_not_expand_operation_authority','future_restore_may_not_write_original_attribute')},
            'approval':{'kind':'explicit_human_text','question_item':'fixture','answer':'fixture owner approval',
                'session':'fixture','recorded_at':'fixture'}}
        import hashlib
        self.enterContext(patch.object(storage,'PROVENANCE_DECISION_SHA256',
            hashlib.sha256(json.dumps(self.value).encode()).hexdigest()))
    def write(self,value):
        self.path.write_text(json.dumps(value));self.path.chmod(0o600)
    def test_requires_existing_exact_scope_private_owner_decision(self):
        self.assertIsNone(storage.provenance_approval(self.b))
        self.write(self.value);self.assertEqual(storage.provenance_approval(self.b)['sha256'],self.b.digest(self.path))
        for field,value in [('status','withdrawn'),('scope','registered_sealed_backups'),('allowed_attributes',['other']),
            ('all_other_fields_strict',False),('approval',{})]:
            self.write({**self.value,field:value})
            with self.subTest(field=field),self.assertRaises(ValueError):storage.provenance_approval(self.b)
        self.write({**self.value,'approval':{**self.value['approval'],'answer':'deny'}})
        with self.assertRaisesRegex(ValueError,'unrecognized'):storage.provenance_approval(self.b)
        self.write(self.value);self.path.chmod(0o644)
        with self.assertRaisesRegex(ValueError,'not_private'):storage.provenance_approval(self.b)
    def test_rejects_alias_hardlink_duplicate_keys_and_oversized_decisions(self):
        self.write(self.value);alias=self.path.with_suffix('.other');os.link(self.path,alias)
        with self.assertRaisesRegex(ValueError,'not_private'):storage.provenance_approval(self.b)
        alias.unlink();self.path.rename(alias);self.path.symlink_to(alias)
        with self.assertRaises(ValueError):storage.provenance_approval(self.b)
        self.path.unlink();alias.rename(self.path)
        self.path.write_text('{"status":"approved","status":"withdrawn"}')
        with self.assertRaisesRegex(ValueError,'duplicate_key'):storage.provenance_approval(self.b)
        self.path.write_text(' '*32769)
        with self.assertRaisesRegex(ValueError,'approval_changed'):storage.provenance_approval(self.b)
