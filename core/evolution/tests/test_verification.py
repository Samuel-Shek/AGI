import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ai_ceo.policy import GateDenied
from ai_ceo.verification import run_replay, checked_manifest


class VerificationTests(unittest.TestCase):
    def test_empty_forged_verifier_output_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            fake = subprocess.CompletedProcess([],0,b'{"cases":[],"total":0,"passed":0}',b'')
            with patch("ai_ceo.verification.subprocess.run", return_value=fake):
                with self.assertRaises(GateDenied) as cm: run_replay(Path(d))
        self.assertEqual(cm.exception.code,"invalid_verifier_output")

    def test_missing_duplicate_and_non_boolean_case_outputs_rejected(self):
        from ai_ceo.verification import PROJECT
        cases = json.loads((PROJECT/"fixtures/sync_cases.json").read_text())["cases"]
        for variant in ["duplicate","typed"]:
            rows = [{"id":c["id"],"passed":True,"mismatch_fields":[]} for c in cases]
            if variant == "duplicate": rows[1]["id"] = rows[0]["id"]
            else: rows[0]["passed"] = 1
            fake = subprocess.CompletedProcess([],0,json.dumps({"cases":rows,"total":len(rows),"passed":len(rows)}).encode(),b'')
            with tempfile.TemporaryDirectory() as d, patch("ai_ceo.verification.subprocess.run",return_value=fake):
                with self.assertRaises(GateDenied): run_replay(Path(d))

    def test_replaced_baseline_or_imported_code_is_rejected(self):
        from ai_ceo.verification import PROJECT, MANIFEST
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            manifest = json.loads(MANIFEST.read_text())
            for relative in manifest["files"]:
                target = root/relative
                target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes((PROJECT/relative).read_bytes())
            marker = root/"acceptance-baseline.json"
            marker.write_text(json.dumps(manifest))
            (root/"ai_ceo/__init__.py").write_text("# altered imported code\n")
            with patch("ai_ceo.verification.PROJECT",root),patch("ai_ceo.verification.MANIFEST",marker):
                with self.assertRaises(GateDenied) as cm: checked_manifest()
            self.assertEqual(cm.exception.code,"baseline_changed")

    def test_timeout_is_failure_not_zero_test_success(self):
        with tempfile.TemporaryDirectory() as d, patch("ai_ceo.verification.subprocess.run",side_effect=subprocess.TimeoutExpired("verifier",30)):
            with self.assertRaises(GateDenied) as cm: run_replay(Path(d))
        self.assertEqual(cm.exception.code,"replay_timeout")


if __name__ == "__main__": unittest.main()
