"""Baseline batching preserves immediate ignore/index/config invalidation."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from activity_mvp import document_changes as changes
from activity_mvp.document_git_ignore import CURRENT, IgnoreProofs
from activity_mvp.storage import ensure_ready


class BaselineGitIgnoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'watch'; self.repo.mkdir()
        self.global_config = self.root / 'global.gitconfig'; self.global_config.write_text('')
        env = patch.dict(os.environ, {'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': str(self.global_config)})
        env.start(); self.addCleanup(env.stop)
        self.git('init', '-q')
        self.paths = [self.repo / ('file-%02d.md' % i) for i in range(12)]
        for path in self.paths: path.write_text('synthetic local document\n')

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.repo, check=True, capture_output=True, text=True)

    def prepared(self):
        proofs = IgnoreProofs()
        before = proofs.prepare(self.repo, self.paths)
        self.assertIsNotNone(before)
        ignored = changes._git_ignored_batch(self.repo, self.paths)
        proofs.remember(before, ignored)
        return proofs

    def test_many_baseline_files_share_one_ignore_command(self):
        conn = ensure_ready(self.root / 'data/activity.sqlite'); self.addCleanup(conn.close)
        changes.configure_document_changes(conn, add_root=self.repo, runtime_root=self.root / 'data')
        run = subprocess.run; commands = []
        def observed(command, **kwargs):
            if 'check-ignore' in command: commands.append(command)
            return run(command, **kwargs)
        with patch.object(changes.subprocess, 'run', side_effect=observed):
            result = changes.build_document_baseline(conn, self.repo, batch_sleep_seconds=0)
        self.assertEqual(result['baseline_status'], 'complete')
        self.assertEqual(result['files_indexed'], len(self.paths))
        self.assertEqual(len(commands), 1, 'one batch must not be repeated for each file')

    def test_new_or_changed_ancestor_ignore_invalidates_before_the_file_read(self):
        proofs = self.prepared()
        self.assertIs(proofs.lookup(self.paths[0]), False)
        (self.repo / '.gitignore').write_text(self.paths[0].name + '\n')
        self.assertIsNone(proofs.lookup(self.paths[0]))
        token = CURRENT.set(proofs)
        try: self.assertFalse(changes._safe_path(self.repo, self.paths[0]))
        finally: CURRENT.reset(token)

    def test_index_change_does_not_reuse_untracked_ignore_result(self):
        (self.repo / '.gitignore').write_text(self.paths[0].name + '\n')
        proofs = self.prepared()
        self.assertIs(proofs.lookup(self.paths[0]), True)
        self.git('add', '-f', self.paths[0].name)
        self.assertIsNone(proofs.lookup(self.paths[0]))
        token = CURRENT.set(proofs)
        try: self.assertFalse(changes._git_ignored(self.repo, self.paths[0]))
        finally: CURRENT.reset(token)

    def test_external_excludes_file_changes_invalidate(self):
        excluded = self.root / 'global.ignore'; excluded.write_text('')
        self.git('config', 'core.excludesfile', str(excluded))
        proofs = self.prepared()
        excluded.write_text(self.paths[0].name + '\n')
        self.assertIsNone(proofs.lookup(self.paths[0]))

    def test_includes_and_linked_worktrees_keep_original_check(self):
        include = self.root / 'included'; include.write_text('')
        self.git('config', 'include.path', str(include))
        self.assertIsNone(IgnoreProofs().prepare(self.repo, self.paths))
        (self.repo / '.git').rename(self.repo / 'actual-git')
        (self.repo / '.git').write_text('gitdir: actual-git\n')
        self.assertIsNone(IgnoreProofs().prepare(self.repo, self.paths))

    def test_configuration_change_during_preparation_is_not_certified(self):
        run = subprocess.run; queries=0
        def changed(command, **kwargs):
            nonlocal queries
            result = run(command, **kwargs)
            if '--name-only' in command:
                queries+=1
                if queries==2:self.global_config.write_text('[core]\nignorecase=true\n')
            return result
        with patch.object(changes.subprocess, 'run', side_effect=changed):
            self.assertIsNone(IgnoreProofs().prepare(self.repo, self.paths))

    def test_actual_system_configuration_origin_is_bound(self):
        with patch.dict(os.environ, {'GIT_CONFIG_NOSYSTEM':'0'}):
            proofs=self.prepared()
            self.assertIs(proofs.lookup(self.paths[0]),False)

    def test_nested_repository_created_during_scan_invalidates_outer_allow(self):
        nested=self.repo/'nested';nested.mkdir()
        path=nested/'new.md';path.write_text('synthetic\n');self.paths.append(path)
        proofs=self.prepared()
        self.assertIs(proofs.lookup(path),False)
        subprocess.run(['git','init','-q'],cwd=nested,check=True,capture_output=True)
        (nested/'.git/info/exclude').write_text('new.md\n')
        self.assertIsNone(proofs.lookup(path))
        token=CURRENT.set(proofs)
        try:self.assertFalse(changes._safe_path(self.repo,path))
        finally:CURRENT.reset(token)

    def test_results_do_not_escape_the_baseline_pass(self):
        conn = ensure_ready(self.root / 'data/activity.sqlite'); self.addCleanup(conn.close)
        changes.configure_document_changes(conn, add_root=self.repo, runtime_root=self.root / 'data')
        changes.build_document_baseline(conn, self.repo, batch_sleep_seconds=0)
        self.assertIsNone(CURRENT.get())
        (self.repo / '.gitignore').write_text(self.paths[0].name + '\n')
        self.assertFalse(changes._safe_path(self.repo, self.paths[0]))
