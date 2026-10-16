"""Synthetic authority tests; no model, native transcript, or production writes."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
sys.dont_write_bytecode=True
from activity_mvp.feedback.store import Store as OriginalFeedbackStore
from activity_mvp.reflection.store import Store as ReflectionStore
from activity_mvp import conversation_scope, presentation_scope
from activity_mvp.feedback import store as candidate
from activity_mvp.reflection import sources

class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name).resolve()
        self.feedback=OriginalFeedbackStore(self.root/'feedback')
        self.feedback.set('config',{'enabled':True});self.feedback.set('autonomy',{'paused':False})
        self.reflection=ReflectionStore(self.root/'reflection');self.reflection.set('enabled',True)
        self.reflection.set('feedback_root',str(self.feedback.root))
        self.bind=patch.object(conversation_scope,'binding',return_value=('synthetic-grant',1));self.binding=self.bind.start()
    def tearDown(self):
        self.bind.stop();self.reflection.__exit__(None,None,None);self.feedback.close();self.temp.cleanup()
    def check(self):return sources.bind_read_check(self.reflection)
    def test_control_mode_never_builds_display_or_source_projection(self):
        with patch.object(presentation_scope,'read_view',side_effect=AssertionError('projection built')):
            with candidate.Store(self.feedback.root,readonly=True,controls_only=True) as authority:
                self.assertTrue(authority.active());self.assertFalse(authority.get('autonomy')['paused'])
    def test_control_mode_denies_content_other_metadata_and_writes(self):
        with candidate.Store(self.feedback.root,readonly=True,controls_only=True) as authority:
            for sql,args in [('SELECT quote FROM messages',()),('SELECT value FROM meta WHERE key=?',('model_capability',)),('DELETE FROM meta',())]:
                with self.assertRaisesRegex(PermissionError,'control_metadata_only'):authority.conn.execute(sql,args)
    def test_readonly_is_required_before_creating_root(self):
        missing=self.root/'must-not-exist'
        with self.assertRaisesRegex(ValueError,'readonly'):candidate.Store(missing,controls_only=True)
        self.assertFalse(missing.exists())
    def test_pause_visible_immediately_and_stop_history_prevents_resume(self):
        check=self.check();self.assertTrue(check());self.feedback.set('paused',True);self.assertFalse(check())
        self.feedback.set('paused',False);self.assertTrue(check());self.feedback.log('owner_pause');self.assertFalse(check())
    def test_disable_and_reenable_epoch_invalidates_existing_check(self):
        check=self.check();self.assertTrue(check());self.reflection.set('enabled',False);self.assertFalse(check())
        self.reflection.set('authorization_epoch',1);self.reflection.set('enabled',True);self.assertFalse(check())
    def test_grant_change_fails_closed_without_cached_admission(self):
        check=self.check();self.assertTrue(check());self.binding.return_value=('synthetic-grant',2);self.assertFalse(check())
    def test_autonomy_pause_is_visible(self):
        check=self.check();self.assertTrue(check());self.feedback.set('autonomy',{'paused':True});self.assertFalse(check())
    def test_global_stop_file_prevents_new_read(self):
        check=self.check();called=[];self.assertTrue(check());(self.feedback.root/'STOP').touch()
        with self.assertRaises(PermissionError):sources.read_active(self.reflection,check,lambda:called.append(True))
        self.assertFalse(called)
    def test_pause_during_read_rejects_returned_value(self):
        check=self.check()
        def operation():self.feedback.set('paused',True);return 'synthetic-not-delivered'
        with self.assertRaises(PermissionError):sources.read_active(self.reflection,check,operation)
    def test_close_during_read_rejects_returned_value(self):
        check=self.check()
        def operation():self.reflection.set('enabled',False);return 'synthetic-not-delivered'
        with self.assertRaises(PermissionError):sources.read_active(self.reflection,check,operation)
    def test_grant_revocation_during_read_rejects_returned_value(self):
        check=self.check()
        def operation():self.binding.return_value=None;return 'synthetic-not-delivered'
        with self.assertRaises(PermissionError):sources.read_active(self.reflection,check,operation)
    def test_database_permission_failure_does_not_reuse_true(self):
        check=self.check();self.assertTrue(check());self.feedback.path.chmod(0o644)
        with self.assertRaises(PermissionError):check()
        self.feedback.path.chmod(0o600)
    def test_extra_guard_is_rechecked(self):
        state={'valid':True};check=sources.bind_read_check(self.reflection,extra=lambda:state['valid'])
        self.assertTrue(check());state['valid']=False;self.assertFalse(check())
    def test_repeated_check_opens_fresh_control_reader_without_projection(self):
        check=self.check();count=0;original=candidate.Store.__init__
        def track(obj,*a,**kw):
            nonlocal count
            count+=1;return original(obj,*a,**kw)
        with patch.object(candidate.Store,'__init__',track),patch.object(presentation_scope,'read_view',side_effect=AssertionError('projection built')):
            for _ in range(10):self.assertTrue(check())
        self.assertEqual(count,10)

if __name__=='__main__':unittest.main(verbosity=2)
