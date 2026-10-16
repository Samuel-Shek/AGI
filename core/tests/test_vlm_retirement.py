from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from activity_mvp import daemon
from activity_mvp.legacy_review_cleanup import cleanup_retired_review_files
from activity_mvp.launch_agent_contract import _validated_environment, LAUNCH_AGENT_PATH
from activity_mvp.pipeline import ingest_events
from activity_mvp.privacy import classify_event
from activity_mvp.private_purge import private_purge_status
from activity_mvp.screen_regions import sanitize_screen_region_signal
from activity_mvp.storage import ensure_ready


class VLMRetirementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.conn = ensure_ready(self.root / "activity.sqlite")
        self.addCleanup(self.conn.close)
        self.now = datetime.now(timezone.utc)

    def job(self, *, expired=True, status="pending"):
        job = "vlm_job_" + uuid.uuid4().hex
        directory = self.root / "raw_vault" / "vlm_pending"
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        directory.parent.chmod(0o700)
        directory.chmod(0o700)
        image = directory / (job + ".png")
        image.write_bytes(b"private legacy test payload")
        image.chmod(0o600)
        observed = self.now - timedelta(hours=2) if expired else self.now
        expires = self.now - timedelta(minutes=1) if expired else self.now + timedelta(minutes=30)
        self.conn.execute("""INSERT INTO vlm_pending_jobs
            (id,created_at,updated_at,captured_at,temp_frame_ref,event_json,status,expires_at)
            VALUES(?,?,?,?,?,?,?,?)""", (job,observed.isoformat(),observed.isoformat(),observed.isoformat(),str(image),"{}",status,expires.isoformat()))
        self.conn.commit()
        return job,image

    def test_disabled_capture_still_cleans_expired_private_files_without_inference(self):
        job,image = self.job()
        with patch.dict(os.environ, {"PAM_VLM_REVIEW_ENABLED":"1","PAM_VLM_ENDPOINT":"http://127.0.0.1:11434/api/generate"}), patch.object(daemon,"capture_sck_keyframe_batch", side_effect=AssertionError("capture while disabled")):
            result = daemon.run_once(self.root / "activity.sqlite")
        self.assertEqual(result["status"],"paused")
        self.assertFalse(image.exists())
        self.assertEqual(self.conn.execute("SELECT status FROM vlm_pending_jobs WHERE id=?",(job,)).fetchone()[0],"expired_deleted")
        self.assertNotIn("vlm_backlog",result)

    def test_unexpired_legacy_rows_are_not_replayed_or_marked_completed(self):
        job,image = self.job(expired=False,status="processing")
        before = dict(self.conn.execute("SELECT * FROM vlm_pending_jobs WHERE id=?",(job,)).fetchone())
        cleanup_retired_review_files(self.conn,self.root)
        after = dict(self.conn.execute("SELECT * FROM vlm_pending_jobs WHERE id=?",(job,)).fetchone())
        self.assertEqual(after,before)
        self.assertTrue(image.exists())

    def test_existing_keep_until_processed_policy_is_not_overridden(self):
        _,image = self.job()
        self.conn.execute("INSERT OR REPLACE INTO settings(key,value,updated_at) VALUES('vlm_backlog_retention_policy','keep_until_processed',?)",(self.now.isoformat(),))
        self.conn.commit()
        result = cleanup_retired_review_files(self.conn,self.root)
        self.assertEqual(result["expired"],0)
        self.assertTrue(image.exists())

    def test_failed_purge_keeps_a_durable_obligation_then_settles_real_deletion(self):
        job,image = self.job()
        with patch("activity_mvp.legacy_review_cleanup._delete_file",side_effect=PermissionError("test denied")):
            result = cleanup_retired_review_files(self.conn,self.root)
        self.assertGreater(result["file_purge_failed"],0)
        self.assertGreater(private_purge_status(self.conn)["pending_count"],0)
        self.assertTrue(image.exists())
        cleanup_retired_review_files(self.conn,self.root)
        self.assertFalse(image.exists())
        self.assertEqual(private_purge_status(self.conn)["pending_count"],0)

    def test_symlink_substitution_cannot_delete_external_file(self):
        _,image = self.job()
        external = self.root / "unrelated.txt"
        external.write_text("keep")
        image.unlink()
        image.symlink_to(external)
        result = cleanup_retired_review_files(self.conn,self.root)
        self.assertGreater(result["file_purge_failed"],0)
        self.assertEqual(external.read_text(),"keep")
        self.assertGreater(private_purge_status(self.conn)["pending_count"],0)

    def test_old_environment_is_rejected_by_launch_contract(self):
        with self.assertRaisesRegex(ValueError,"environment_invalid"):
            _validated_environment("core",{"PATH":LAUNCH_AGENT_PATH,"PAM_PRIVACY_PROFILE":"strict","PAM_VLM_REVIEW_ENABLED":"1"})

    def test_removed_cli_switches_and_worker_commands_fail_before_execution(self):
        root = Path(__file__).resolve().parents[1]
        for args in [["install-launch-agent","--enable-vlm"],["install-launch-agent","--preserve-vlm"],["drain-vlm-backlog"],["configure-vlm-backlog"]]:
            result = subprocess.run([sys.executable,"-m","activity_mvp",*args],cwd=root,capture_output=True,text=True)
            self.assertEqual(result.returncode,2,args)

    def test_local_secret_protection_cannot_be_overridden_by_old_clear_verdict(self):
        result = classify_event({"app_name":"TextEdit","window_title":"Credentials","ocr_text":"password: private-example-secret","vlm_review":{"status":"clear"}})
        self.assertIn(result.status,{"blocked","review_required"})

    def test_low_confidence_sensitive_unknown_region_remains_conservative(self):
        result = classify_event({"app_name":"Unknown","context_confidence":0.1,"window_title":"Sensitive account","ocr_blocks":[{"text":"credentials","region":"unknown","confidence":0.1}]})
        self.assertIn(result.status,{"blocked","review_required"})

    def test_retired_layout_evidence_cannot_authorize_an_action(self):
        self.assertIsNone(sanitize_screen_region_signal({"region_kind":"main_content","source":"vlm_layout","actionability":"actionable","confidence":1}))

    def test_ocr_ingest_works_without_populating_model_columns(self):
        event={"captured_at":self.now.isoformat(),"display_id":"main","app_name":"TextEdit","window_title":"Work note","ocr_text":"Draft a general engineering note","frame_hash":"retirement-fixture","context_confidence":0.9}
        result=ingest_events(self.conn,[event])
        self.assertEqual(result["clear"],1)
        row=self.conn.execute("SELECT vlm_review_count,vlm_model FROM capture_coverage_ledger").fetchone()
        self.assertEqual(row[0],0)
        self.assertIsNone(row[1])
