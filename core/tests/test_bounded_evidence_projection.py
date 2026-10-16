"""Synthetic evidence only; no production files, grants or acceptance writes."""
import hashlib
import io
import json
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from activity_mvp import retirement_snapshot as reader
from activity_mvp import task_closure


class BoundedEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / "evidence.json"
        self.file.write_bytes(b'{"synthetic":true}')
        self.rows = [{"path": str(self.file), "sha256": hashlib.sha256(self.file.read_bytes()).hexdigest()}]
        self.sources = SimpleNamespace(company_stopped=lambda: False,
            policy=lambda: {"company_roots": []})

    def test_default_and_actions_keep_fresh_complete_check(self):
        with patch.object(reader.subprocess, "Popen") as spawn:
            with reader.source_snapshot(self.sources) as view:
                self.assertTrue(task_closure.current(self.rows))
                self.file.write_bytes(b"changed")
                self.assertFalse(task_closure.current(self.rows))
                self.assertFalse(view.bounded_evidence)
            spawn.assert_not_called()
        self.assertFalse(task_closure.current(self.rows))

    def test_real_same_request_worker_hash_and_identity_cache_then_reaped(self):
        with reader.source_snapshot(self.sources, bounded_evidence=True) as view:
            self.assertTrue(task_closure.current(self.rows))
            worker = view.evidence_reader
            with patch.object(worker, "read", wraps=worker.read) as read:
                self.assertTrue(task_closure.current(self.rows))
                read.assert_not_called()
            self.file.write_bytes(b"changed")
            self.assertFalse(task_closure.current(self.rows))
        self.assertIsNotNone(worker.process.poll())
        self.assertIsNone(reader.CURRENT.get())

    def test_timeout_is_unknown_and_exact_child_is_reaped(self):
        original = subprocess.Popen
        children = []
        def stalled(*args, **kwargs):
            child = original([sys.executable, "-I", "-c",
                "import sys,time;sys.stdin.buffer.readline();time.sleep(30)"], **kwargs)
            children.append(child)
            return child
        with patch.object(reader, "EVIDENCE_READ_SECONDS", .15), patch.object(reader.subprocess, "Popen", side_effect=stalled):
            with self.assertRaisesRegex(ValueError, "closure_evidence_read_unavailable"):
                with reader.source_snapshot(self.sources, bounded_evidence=True):
                    task_closure.current(self.rows)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
        self.assertIsNone(reader.CURRENT.get())

    def test_child_ignoring_terminate_is_killed_and_reaped(self):
        original = subprocess.Popen
        children = []
        def stalled(*args, **kwargs):
            child = original([sys.executable, "-I", "-c",
                "import sys,time,signal;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                "sys.stdin.buffer.readline();time.sleep(30)"], **kwargs)
            children.append(child)
            return child
        with patch.object(reader, "EVIDENCE_READ_SECONDS", .3), patch.object(reader.subprocess, "Popen", side_effect=stalled):
            with self.assertRaisesRegex(ValueError, "closure_evidence_read_unavailable"):
                with reader.source_snapshot(self.sources, bounded_evidence=True):
                    task_closure.current(self.rows)
        self.assertEqual(children[0].poll(), -signal.SIGKILL)

    def test_definitely_missing_file_is_changed_instead_of_unknown(self):
        self.file.unlink()
        with reader.source_snapshot(self.sources, bounded_evidence=True):
            self.assertFalse(task_closure.current(self.rows))
        source = SimpleNamespace(buffer=io.BytesIO((json.dumps({"path": str(self.file)}) + "\n").encode()))
        output = io.StringIO()
        with patch.object(reader.sys, "stdin", source), patch.object(reader.sys, "stdout", output):
            reader._evidence_worker()
        self.assertEqual(json.loads(output.getvalue()), {"status": "changed"})

    def test_parent_replaced_by_file_is_changed_instead_of_unknown(self):
        folder = self.file.parent / "folder"
        folder.mkdir()
        path = folder / "evidence"
        path.write_bytes(b"example")
        rows = [{"path": str(path), "sha256": hashlib.sha256(b"example").hexdigest()}]
        path.unlink(); folder.rmdir(); folder.write_bytes(b"replacement")
        with reader.source_snapshot(self.sources, bounded_evidence=True):
            self.assertFalse(task_closure.current(rows))

    def test_link_is_changed_and_never_followed(self):
        link = self.file.with_name("link")
        link.symlink_to(self.file)
        rows = [{**self.rows[0], "path": str(link)}]
        with reader.source_snapshot(self.sources, bounded_evidence=True) as view:
            self.assertFalse(task_closure.current(rows))
            self.assertIsNone(view.evidence_reader)

    def test_refusal_is_unknown_instead_of_a_changed_acceptance(self):
        source = SimpleNamespace(buffer=io.BytesIO((json.dumps({"path": str(self.file)}) + "\n").encode()))
        output = io.StringIO()
        with patch.object(reader.sys, "stdin", source), patch.object(reader.sys, "stdout", output), \
             patch.object(reader.os, "open", side_effect=PermissionError("synthetic refusal")):
            reader._evidence_worker()
        self.assertEqual(json.loads(output.getvalue()), {"status": "unavailable"})
        original = subprocess.Popen
        def denied(*args, **kwargs):
            return original([sys.executable, "-I", "-c",
                "import sys;sys.stdin.buffer.readline();print('{\"status\":\"unavailable\"}',flush=True);sys.stdin.buffer.read()"], **kwargs)
        with patch.object(reader.subprocess, "Popen", side_effect=denied):
            with self.assertRaisesRegex(ValueError, "closure_evidence_read_unavailable"):
                with reader.source_snapshot(self.sources, bounded_evidence=True) as view:
                    task_closure.current(self.rows)
            self.assertIsNotNone(view.evidence_reader.process.poll())

    def test_lstat_refusal_is_unknown_only_in_read_projection(self):
        with patch.object(Path, "lstat", side_effect=PermissionError("synthetic refusal")):
            with reader.source_snapshot(self.sources, bounded_evidence=True):
                with self.assertRaisesRegex(ValueError, "closure_evidence_read_unavailable"):
                    task_closure.current(self.rows)
            with reader.source_snapshot(self.sources):
                self.assertFalse(task_closure.current(self.rows))

    def test_one_total_budget_is_not_restarted_for_next_file(self):
        second = self.file.with_name("second.json")
        second.write_bytes(b"second")
        second_rows = [{"path": str(second), "sha256": hashlib.sha256(b"second").hexdigest()}]
        with reader.source_snapshot(self.sources, bounded_evidence=True) as view:
            self.assertTrue(task_closure.current(self.rows))
            deadline = view.evidence_reader.deadline
            with patch.object(reader.time, "monotonic", return_value=deadline + 1):
                with self.assertRaisesRegex(ValueError, "closure_evidence_read_unavailable"):
                    task_closure.current(second_rows)
            self.assertEqual(view.evidence_reader.deadline, deadline)
        self.assertIsNotNone(view.evidence_reader.process.poll())

    def test_nested_read_cannot_upgrade_original_fresh_context(self):
        with reader.source_snapshot(self.sources) as original:
            with reader.source_snapshot(self.sources, bounded_evidence=True) as nested:
                self.assertIs(original, nested)
                self.assertFalse(nested.bounded_evidence)
                self.assertTrue(task_closure.current(self.rows))
        self.assertIsNone(reader.CURRENT.get())


if __name__ == "__main__":
    unittest.main()
