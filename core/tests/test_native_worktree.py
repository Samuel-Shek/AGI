"""Real isolated Git registration and authenticated snapshot boundary regressions."""
import copy
import fcntl
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from activity_mvp import attention_native as native
from activity_mvp import native_worktree as identity

SESSION = '00000000-0000-0000-0000-000000000010'
TURN = '00000000-0000-0000-0000-000000000002'


class NativeWorktreeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='.native-worktree-test-',
                                               dir=Path(__file__).resolve().parents[2])
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / 'original'; self.repo.mkdir()
        self.git(self.repo, 'init', '-q')
        self.git(self.repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@invalid',
                 'commit', '-q', '--allow-empty', '-m', 'isolated fixture')
        self.worktree = self.root / 'registered worktree'
        self.git(self.repo, 'worktree', 'add', '-q', '--detach', str(self.worktree))
        self.admin = Path((self.worktree / '.git').read_text().strip()[8:])

    def git(self, root, *args):
        return subprocess.check_output(['/usr/bin/git', '-C', str(root), *args],
                                       stderr=subprocess.DEVNULL, timeout=5)

    def snapshot(self, cwd, *, confirmation=None, owners=None, **kwargs):
        state = {'id': SESSION, 'cwd': str(cwd),
                 'threadRuntimeStatus': {'type': 'active'},
                 'turns': [{'turnId': TURN, 'status': 'inProgress',
                            'items': [{'text': 'private fixture must not escape'}]}]}
        ipc = native.DesktopIPC(); ipc.client_id = 'fixture'; ipc.socket = Mock()
        ipc.owner = Mock(return_value=owners[0] if owners else 'verified-owner')
        message = {'method': 'thread-stream-state-changed', 'sourceClientId': 'verified-owner',
                   'version': 11, 'params': {'conversationId': SESSION,
                     'change': {'type': 'snapshot', 'conversationState': state}}}
        raw = json.dumps(message).encode()
        second = copy.deepcopy(message)
        second['params']['change']['conversationState'].update(confirmation or {})
        second_raw = json.dumps(second).encode()
        ipc.read_exact = Mock(side_effect=[struct.pack('<I', len(raw)), raw])
        confirmed = native.DesktopIPC(); confirmed.client_id = 'confirmation'; confirmed.socket = Mock()
        confirmed.owner = Mock(return_value=owners[1] if owners else 'verified-owner')
        confirmed.read_exact = Mock(side_effect=[struct.pack('<I', len(second_raw)), second_raw])
        with patch.object(native, 'DesktopIPC') as connection:
            connection.return_value.__enter__.return_value = confirmed
            return ipc.snapshot(SESSION, expected_cwd=self.repo, current_turn=True, **kwargs)

    def test_registered_worktree_requires_explicit_current_turn_opt_in(self):
        with self.assertRaisesRegex(native.NativeError, 'target_mismatch'):
            self.snapshot(self.worktree)
        result = self.snapshot(self.worktree, allow_registered_worktree=True)
        self.assertEqual(result['turn_id'], TURN)
        self.assertTrue(result['current_turn_matches'])
        self.assertNotIn('private fixture', json.dumps(result))
        self.assertNotIn('cwd', result)
        self.assertEqual(self.snapshot(self.repo)['turn_id'], TURN)
        ipc = native.DesktopIPC(); ipc.owner = Mock()
        for args in ({'current_turn': True}, {'expected_cwd': self.repo}):
            with self.subTest(args=args), self.assertRaisesRegex(native.NativeError, 'worktree_selector_invalid'):
                ipc.snapshot(SESSION, allow_registered_worktree=True, **args)
        ipc.owner.assert_not_called()

    def test_owner_turn_or_directory_change_during_validation_is_rejected(self):
        for changed in ({'cwd': str(self.repo)},
                        {'turns': [{'turnId': SESSION, 'status': 'inProgress'}]},
                        {'threadRuntimeStatus': {'type': 'idle'}}):
            with self.subTest(changed=changed), self.assertRaises(native.NativeError):
                self.snapshot(self.worktree, confirmation=changed, allow_registered_worktree=True)
        with self.assertRaisesRegex(native.NativeError, 'owner_changed'):
            self.snapshot(self.worktree, owners=['verified-owner', 'replacement-owner'],
                          allow_registered_worktree=True)

    def test_confirmation_time_comes_from_its_actual_received_snapshot(self):
        with patch.object(native.time, 'time', side_effect=[100.0, 101.0, 9999.0]):
            result = self.snapshot(self.worktree, allow_registered_worktree=True)
        self.assertEqual(result['observed_at'], 101.0)

    def test_clone_subdirectory_unregistered_and_prefix_names_rejected(self):
        clone = self.root / 'original-copy'
        self.git(self.root, 'clone', '-q', '--no-local', str(self.repo), str(clone))
        sub = self.worktree / 'child'; sub.mkdir()
        unregistered = self.root / 'unregistered'; unregistered.mkdir()
        (unregistered / '.git').write_bytes((self.worktree / '.git').read_bytes())
        for path in (clone, sub, unregistered, self.root / 'original-suffix', ''):
            with self.subTest(path=path), self.assertRaises(native.NativeError):
                self.snapshot(path, allow_registered_worktree=True)

    def test_symlink_directory_and_binding_file_rejected(self):
        alias = self.root / 'alias'; alias.symlink_to(self.worktree, target_is_directory=True)
        with self.assertRaises(native.NativeError):
            self.snapshot(alias, allow_registered_worktree=True)
        original = (self.worktree / '.git').read_bytes()
        copy = self.root / 'pointer'; copy.write_bytes(original)
        (self.worktree / '.git').unlink(); (self.worktree / '.git').symlink_to(copy)
        with self.assertRaises(native.NativeError):
            self.snapshot(self.worktree, allow_registered_worktree=True)

    def test_back_pointer_common_directory_and_registration_required(self):
        for name, changed in (('gitdir', b'/another/path/.git\n'), ('commondir', b'../../other\n')):
            path = self.admin / name; old = path.read_bytes()
            try:
                path.write_bytes(changed)
                with self.subTest(name=name), self.assertRaises(native.NativeError):
                    self.snapshot(self.worktree, allow_registered_worktree=True)
            finally:
                path.write_bytes(old)
        actual_git = identity._git
        def no_registration(root, args, deadline):
            if args[0] == 'worktree':
                return b'worktree ' + os.fsencode(str(self.repo)) + b'\0HEAD none\0\0'
            return actual_git(root, args, deadline)
        with patch.object(identity, '_git', side_effect=no_registration), self.assertRaises(identity.WorktreeIdentityError):
            identity._verify_inprocess(self.repo, self.worktree)

    def test_binding_swap_during_git_read_is_rejected(self):
        actual_git = identity._git; changed = False
        def swap(root, args, deadline):
            nonlocal changed
            result = actual_git(root, args, deadline)
            if args[0] == 'worktree' and not changed:
                path = self.admin / 'gitdir'; raw = path.read_bytes()
                replacement = path.with_name('gitdir-replacement'); replacement.write_bytes(raw)
                replacement.replace(path); changed = True
            return result
        with patch.object(identity, '_git', side_effect=swap), self.assertRaises(identity.WorktreeIdentityError):
            identity._verify_inprocess(self.repo, self.worktree)
        self.assertTrue(changed)

    def test_foreign_owner_writable_and_hardlinked_metadata_rejected(self):
        path = self.admin / 'gitdir'; mode = path.stat().st_mode
        path.chmod(0o666)
        with self.assertRaises(native.NativeError):
            self.snapshot(self.worktree, allow_registered_worktree=True)
        path.chmod(mode)
        link = self.root / 'second-link'; os.link(path, link)
        with self.assertRaises(native.NativeError):
            self.snapshot(self.worktree, allow_registered_worktree=True)
        link.unlink()
        with patch.object(identity.os, 'getuid', return_value=os.getuid()+1), self.assertRaises(identity.WorktreeIdentityError):
            identity._verify_inprocess(self.repo, self.worktree)

    def test_git_environment_cannot_redirect_repository(self):
        with patch.dict(os.environ, {'GIT_DIR': '/wrong/git', 'GIT_WORK_TREE': '/wrong/tree',
                                    'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'core.worktree',
                                    'GIT_CONFIG_VALUE_0': '/wrong/override',
                                    'DEVELOPER_DIR': '/wrong/toolchain', 'xcrun_nocache': '0'}):
            self.assertEqual(self.snapshot(self.worktree, allow_registered_worktree=True)['turn_id'], TURN)

    def test_search_only_ancestor_preserves_real_registration_and_alias_rejection(self):
        alias = self.root / 'alias'; alias.symlink_to(self.worktree, target_is_directory=True)
        mode = self.root.stat().st_mode
        try:
            self.root.chmod(0o100)
            self.assertEqual(self.snapshot(self.worktree, allow_registered_worktree=True)['turn_id'], TURN)
            with self.assertRaises(native.NativeError):
                self.snapshot(alias, allow_registered_worktree=True)
        finally:
            self.root.chmod(mode)

    def test_git_failure_timeout_and_oversize_output_fail_closed(self):
        for error in (subprocess.TimeoutExpired('fixture', 3), OSError('fixture'),
                      identity.WorktreeIdentityError('worktree_git_output_limit')):
            with self.subTest(error=type(error).__name__), patch.object(identity, '_git', side_effect=error):
                with self.assertRaises((OSError, subprocess.SubprocessError, identity.WorktreeIdentityError)):
                    identity._verify_inprocess(self.repo, self.worktree)
        with self.assertRaises(identity.WorktreeIdentityError):
            identity._git(self.repo, ['status', '--porcelain'], time.monotonic()-1)

    def test_blocked_reader_process_is_bounded_without_touching_real_filesystems(self):
        script = self.root / 'blocked-reader.py'
        script.write_text('import time\ntime.sleep(60)\n')
        before = time.monotonic()
        with patch.object(identity, '__file__', str(script)), self.assertRaisesRegex(identity.WorktreeIdentityError, 'reader_timeout'):
            identity.verify_registered_worktree(self.repo, self.worktree)
        self.assertLess(time.monotonic()-before, 5)

    def test_reader_timeout_terminates_its_child_process_group(self):
        script = self.root / 'reader-with-child.py'; marker = self.root / 'child-identity'
        lease = self.root / 'child-lease'
        lease.touch(mode=0o600)
        child_code = (
            'import fcntl, os, time\n'
            'lease = open(' + repr(str(lease)) + ", 'rb')\n"
            'fcntl.flock(lease, fcntl.LOCK_EX)\n'
            'open(' + repr(str(marker)) + ", 'w').write(str(os.getpid()) + ' ' + str(os.getpgrp()))\n"
            'time.sleep(60)\n')
        script.write_text(
            'import os, subprocess, sys, time\n'
            'if os.getpid() != os.getpgrp(): raise SystemExit(88)\n'
            'child = subprocess.Popen([sys.executable, \'-c\', ' + repr(child_code) + '])\n'
            'time.sleep(60)\n')
        group = None
        try:
            with patch.object(identity, '__file__', str(script)), self.assertRaisesRegex(identity.WorktreeIdentityError, 'reader_timeout'):
                identity.verify_registered_worktree(self.repo, self.worktree)
            child, group = map(int, marker.read_text().split())
            self.assertNotEqual(group, os.getpgrp())
            # The ready marker is written only after the real descendant owns
            # this lease, which it holds throughout its 60-second sleep. A
            # fresh exclusive lock proves its fd was released after killpg;
            # it does not require global process inspection or PID reaping.
            released = False
            with lease.open('rb') as check:
                for _ in range(20):
                    try:
                        fcntl.flock(check, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        released = True
                        break
                    except BlockingIOError:
                        time.sleep(0.05)
            self.assertTrue(released, 'reader descendant retained its live lease')
        finally:
            if group is None and marker.exists():
                _, group = map(int, marker.read_text().split())
            if group is not None and group != os.getpgrp():
                try: os.killpg(group, signal.SIGKILL)
                except ProcessLookupError: pass


if __name__ == '__main__':
    unittest.main()
