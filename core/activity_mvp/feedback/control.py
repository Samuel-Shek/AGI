"""Short shared boundaries for owner controls and actual local action starts."""
from contextlib import contextmanager
from pathlib import Path
import fcntl
import os
import stat
import threading

_held=threading.local()

@contextmanager
def gate(root, check=None):
    root=Path(root).absolute()
    held=getattr(_held, 'roots', set())
    if root in held:
        if check is not None and not check():raise PermissionError('not_active')
        yield
        return
    fd=os.open(root/'control.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        s=os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_uid!=os.getuid() or s.st_nlink!=1 or s.st_mode&0o077:
            raise PermissionError('control_lock_invalid')
        fcntl.flock(fd,fcntl.LOCK_EX)
        _held.roots=held|{root}
        if check is not None and not check():raise PermissionError('not_active')
        yield
    finally:
        _held.roots=held
        os.close(fd)
