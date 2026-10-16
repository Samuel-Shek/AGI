"""Bounded Git ignore input without a parent-to-child pipe write."""
from __future__ import annotations

import errno
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from activity_mvp import document_changes as documents


class GitIgnoreInputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="agi-git-input-")
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name).resolve()
        self.environment = mock.patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        subprocess.run(["git", "init", "--quiet", str(self.repo)],
                       check=True, capture_output=True, timeout=5)

    def assert_closed(self, descriptor):
        with self.assertRaises(OSError) as caught:
            os.fstat(descriptor)
        self.assertEqual(caught.exception.errno, errno.EBADF)

    def test_large_nul_unicode_input_keeps_git_rules_batches_and_private_cleanup(self):
        (self.repo / ".gitignore").write_text("*.tmp\n", encoding="utf-8")
        tracked = self.repo / "tracked 文件.tmp"
        tracked.write_text("tracked", encoding="utf-8")
        subprocess.run(["git", "add", "--force", "--", tracked.name],
                       cwd=self.repo, check=True, capture_output=True, timeout=5)
        paths = [self.repo / (f"{index:04d} 空格\n文件-" + "x" * 160
                             + (".tmp" if index % 3 else ".md"))
                 for index in range(2000)] + [tracked]
        expected = {path for path in paths[:-1] if path.suffix == ".tmp"}
        original_run = subprocess.run
        records = []

        def run(command, **kwargs):
            self.assertEqual(command, ["git", "check-ignore", "-z", "--stdin"])
            self.assertNotIn("input", kwargs)
            self.assertEqual(kwargs["timeout"], 5)
            request = kwargs["stdin"]
            descriptor = request.fileno()
            metadata = os.fstat(descriptor)
            self.assertTrue(stat.S_ISREG(metadata.st_mode))
            self.assertEqual(stat.S_IMODE(metadata.st_mode), 0o600)
            self.assertEqual(metadata.st_nlink, 0)
            self.assertEqual(metadata.st_uid, os.getuid())
            self.assertEqual(request.tell(), 0)
            payload = request.read()
            request.seek(0)
            self.assertTrue(payload.endswith("\0"))
            records.append((descriptor, payload.split("\0")[:-1]))
            return original_run(command, **kwargs)

        with mock.patch.object(documents.subprocess, "run", side_effect=run):
            actual = documents._git_ignored_batch(self.repo, paths)
        self.assertEqual(actual, expected)
        self.assertEqual([len(names) for _, names in records], [2000, 1])
        self.assertGreater(len(records[0][1][0]), 128)
        self.assertEqual([name for _, names in records for name in names],
                         [str(path.relative_to(self.repo)) for path in paths])
        for descriptor, _ in records:
            self.assert_closed(descriptor)

    def test_original_five_second_timeout_reaps_only_the_owned_child(self):
        original_run, original_popen = subprocess.run, subprocess.Popen
        processes, descriptors = [], []

        def popen(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            processes.append(process)
            return process

        def blocked_git(command, **kwargs):
            self.assertEqual(kwargs["timeout"], 5)
            self.assertNotIn("input", kwargs)
            descriptors.append(kwargs["stdin"].fileno())
            # More than a pipeful of output followed by a child that does not
            # exit: output must be drained, then the original timeout applies.
            return original_run([sys.executable, "-I", "-S", "-B", "-c",
                "import os,time;os.write(1,b'x'*131072);time.sleep(30)"], **kwargs)

        with mock.patch.object(documents.subprocess, "run", side_effect=blocked_git), \
                mock.patch.object(subprocess, "Popen", side_effect=popen):
            self.assertIsNone(documents._git_ignored_batch(self.repo, [self.repo / "safe.md"]))
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].poll())
        for descriptor in descriptors:
            self.assert_closed(descriptor)

    def test_temporary_file_creation_failure_does_not_allow_or_spawn_git(self):
        with mock.patch.object(documents.tempfile, "TemporaryFile", side_effect=OSError("injected")), \
                mock.patch.object(documents.subprocess, "run") as run:
            self.assertIsNone(documents._git_ignored_batch(self.repo, [self.repo / "safe.md"]))
        run.assert_not_called()

    def test_temporary_file_write_failure_closes_it_and_does_not_spawn_git(self):
        request = tempfile.TemporaryFile(mode="w+t", newline="")
        descriptor = request.fileno()
        with mock.patch.object(request, "write", side_effect=OSError("injected")), \
                mock.patch.object(documents.tempfile, "TemporaryFile", return_value=request), \
                mock.patch.object(documents.subprocess, "run") as run:
            self.assertIsNone(documents._git_ignored_batch(self.repo, [self.repo / "safe.md"]))
        run.assert_not_called()
        self.assert_closed(descriptor)

    def test_git_failure_preserves_unknown_and_closes_private_input(self):
        descriptors = []

        def failed(command, **kwargs):
            descriptors.append(kwargs["stdin"].fileno())
            return subprocess.CompletedProcess(command, 128, stdout="")

        with mock.patch.object(documents.subprocess, "run", side_effect=failed):
            self.assertIsNone(documents._git_ignored_batch(self.repo, [self.repo / "safe.md"]))
        for descriptor in descriptors:
            self.assert_closed(descriptor)

    def test_stop_after_git_result_still_discards_it_and_closes_input(self):
        original_run = subprocess.run
        stopped = False
        descriptors = []

        def run(command, **kwargs):
            nonlocal stopped
            descriptors.append(kwargs["stdin"].fileno())
            result = original_run(command, **kwargs)
            stopped = True
            return result

        with mock.patch.object(documents.subprocess, "run", side_effect=run):
            self.assertIsNone(documents._git_ignored_batch(
                self.repo, [self.repo / "safe.md"], stop_check=lambda: stopped))
        for descriptor in descriptors:
            self.assert_closed(descriptor)


if __name__ == "__main__":
    unittest.main()
