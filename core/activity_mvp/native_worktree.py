"""Read-only Git identity for the two existing native maintenance tasks.

This validates an execution directory, never an additional data-source grant.
Only the configured primary repository's registered linked worktrees qualify.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import sys
import time


class WorktreeIdentityError(ValueError):
    pass


def _path(value):
    if not isinstance(value, (str, Path)):
        raise WorktreeIdentityError('worktree_path_invalid')
    raw = str(value)
    path = Path(raw)
    if (not path.is_absolute() or str(path) != raw or '..' in path.parts
            or len(os.fsencode(raw)) > 4096 or any(ord(c) < 32 for c in raw)):
        raise WorktreeIdentityError('worktree_path_invalid')
    return path


def _identity(path, *, file=False):
    """Anchor each component with no-follow descriptors; return stable identity."""
    # Traversal needs metadata handles, not permission to enumerate parents.
    # Match the existing fixed readers; the actual binding file stays read-only.
    access = getattr(os, 'O_SEARCH', 0x40000000 if sys.platform == 'darwin'
                     else getattr(os, 'O_PATH', os.O_RDONLY))
    flags = access | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open('/', flags)
    chain = []
    try:
        for index, part in enumerate(path.parts[1:]):
            leaf = index == len(path.parts) - 2
            mode = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK if file and leaf else flags
            child = os.open(part, mode, dir_fd=fd)
            os.close(fd); fd = child
            info = os.fstat(fd)
            expected = stat.S_ISREG if file and leaf else stat.S_ISDIR
            if (not expected(info.st_mode) or info.st_uid not in (0, os.getuid())
                    or (leaf and info.st_uid != os.getuid()) or info.st_mode & 0o022):
                raise WorktreeIdentityError('worktree_object_invalid')
            chain.append((info.st_dev, info.st_ino, info.st_mode, info.st_uid))
        if not file:
            return tuple(chain)
        before = os.fstat(fd)
        if before.st_nlink != 1 or before.st_size > 8192:
            raise WorktreeIdentityError('worktree_binding_invalid')
        raw = os.read(fd, 8193)
        after = os.fstat(fd)
        fields = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if len(raw) > 8192 or fields(before) != fields(after):
            raise WorktreeIdentityError('worktree_binding_changed')
        return raw, (tuple(chain), fields(after), hashlib.sha256(raw).hexdigest())
    finally:
        os.close(fd)


def _git(root, arguments, deadline):
    """Fixed binary, clean Git environment, bounded output and one total deadline."""
    env = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_OPTIONAL_LOCKS': '0'}
    if sys.platform == 'darwin':
        # /usr/bin/git is a toolchain launcher. Use the same fixed command-line
        # toolchain as existing isolated checks, never caller-selected Xcode.
        env.update(DEVELOPER_DIR='/Library/Developer/CommandLineTools', xcrun_nocache='1')
    command = ['/usr/bin/git', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null',
               '-C', str(root), *arguments]
    process = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    chunks = []; count = 0
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise WorktreeIdentityError('worktree_git_timeout')
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                count += len(chunk)
                if count > 512 * 1024:
                    raise WorktreeIdentityError('worktree_git_output_limit')
                chunks.append(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0 or process.wait(timeout=remaining) != 0:
            raise WorktreeIdentityError('worktree_git_failed')
        return b''.join(chunks)
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=1)
        process.stdout.close()


def _metadata(repository, worktree):
    common = repository / '.git'
    identities = [_identity(repository), _identity(common), _identity(worktree)]
    pointer, fingerprint = _identity(worktree / '.git', file=True)
    identities.append(fingerprint)
    if not pointer.startswith(b'gitdir: ') or not pointer.endswith(b'\n'):
        raise WorktreeIdentityError('worktree_binding_invalid')
    admin = _path(os.fsdecode(pointer[8:-1]))
    if admin.parent != common / 'worktrees':
        raise WorktreeIdentityError('worktree_common_directory_mismatch')
    identities.append(_identity(admin))
    back, fingerprint = _identity(admin / 'gitdir', file=True)
    identities.append(fingerprint)
    relative, fingerprint = _identity(admin / 'commondir', file=True)
    identities.append(fingerprint)
    if back != os.fsencode(str(worktree / '.git')) + b'\n' or relative != b'../..\n':
        raise WorktreeIdentityError('worktree_back_reference_mismatch')
    return tuple(identities)


def _binding(repository, worktree, deadline):
    identities = _metadata(repository, worktree)
    common = repository / '.git'
    expected = os.fsencode(str(common))
    for root in (repository, worktree):
        info = _git(root, ['rev-parse', '--path-format=absolute', '--show-toplevel', '--git-common-dir'], deadline)
        if info != os.fsencode(str(root)) + b'\n' + expected + b'\n':
            raise WorktreeIdentityError('worktree_repository_mismatch')
    listed = _git(repository, ['worktree', 'list', '--porcelain', '-z'], deadline)
    records = [record.split(b'\0') for record in listed.split(b'\0\0') if record]
    matches = [record for record in records if record[0] == b'worktree ' + os.fsencode(str(worktree))]
    if len(matches) != 1 or any(field.startswith(b'prunable') for field in matches[0]):
        raise WorktreeIdentityError('worktree_not_registered')
    if identities != _metadata(repository, worktree):
        raise WorktreeIdentityError('worktree_binding_changed')
    return identities


def _verify_inprocess(repository, worktree):
    """Check two complete bindings around Git reads; reject concurrent rebinding."""
    repository, worktree = _path(repository), _path(worktree)
    if repository == worktree:
        raise WorktreeIdentityError('linked_worktree_required')
    deadline = time.monotonic() + 3.0
    first = _binding(repository, worktree, deadline)
    if first != _binding(repository, worktree, deadline) or time.monotonic() > deadline:
        raise WorktreeIdentityError('worktree_binding_changed')


def verify_registered_worktree(repository, worktree):
    """Bound all filesystem reads in one disposable, isolated reader process."""
    repository, worktree = _path(repository), _path(worktree)
    process = subprocess.Popen(
        [sys.executable, '-I', '-S', str(Path(__file__).absolute()), str(repository), str(worktree)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
    try:
        try:
            code = process.wait(timeout=3)
        except subprocess.TimeoutExpired as exc:
            raise WorktreeIdentityError('worktree_reader_timeout') from exc
        if code != 0:
            raise WorktreeIdentityError('worktree_identity_unverified')
    finally:
        if process.poll() is None:
            # The child has its own process group; include its fixed Git reads.
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=1)


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit(2)
    _verify_inprocess(sys.argv[1], sys.argv[2])
