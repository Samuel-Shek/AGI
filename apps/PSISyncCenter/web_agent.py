"""Per-user launchd control for the standalone PSI web server."""
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import tempfile

LABEL = 'app.psi.web-workbench'
PLIST = Path.home() / 'Library' / 'LaunchAgents' / (LABEL + '.plist')
DOMAIN = f'gui/{os.getuid()}'
TARGET = f'{DOMAIN}/{LABEL}'


def configuration(python, script):
    return {
        'Label': LABEL,
        'ProgramArguments': [python, '-B', str(script), 'serve'],
        'RunAtLoad': True,
        'KeepAlive': True,
        'ThrottleInterval': 10,
        'LimitLoadToSessionType': 'Aqua',
    }


def installed(expected):
    try:
        info = PLIST.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError('登录项文件归属或权限异常')
    with PLIST.open('rb') as stream:
        if plistlib.load(stream) != expected:
            raise ValueError('同名登录项指向其他配置')
    return True


def publish(expected):
    # Exclusive publication. A concurrent writer cannot be overwritten.
    with tempfile.NamedTemporaryFile(dir=PLIST.parent, prefix='.psi-web-', delete=False) as stream:
        tmp = Path(stream.name)
    try:
        with tmp.open('wb') as stream:
            plistlib.dump(expected, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(tmp, PLIST)
    finally:
        tmp.unlink()


def ctl(*args, check=True):
    result = subprocess.run(['/bin/launchctl', *args], capture_output=True, text=True, timeout=30)
    if check and result.returncode:
        raise ValueError(f'launchctl {args[0]} 失败 ({result.returncode}): {result.stderr.strip()}')
    return result


def job_pid():
    """None for unregistered, zero while waiting, PID for a running job."""
    result = ctl('print', TARGET, check=False)
    if result.returncode:
        if 'Could not find service' in result.stderr:
            return None
        raise ValueError('无法核对当前登录会话的服务')
    paths = [line.strip() for line in result.stdout.splitlines() if line.startswith('\tpath = ')]
    if paths != [f'path = {PLIST}']:
        raise ValueError('同名服务的实际来源不匹配')
    for line in result.stdout.splitlines():
        if line.startswith('\tpid = '):
            return int(line.split(' = ', 1)[1])
    return 0


def disabled():
    lines = ctl('print-disabled', DOMAIN).stdout.splitlines()
    return any(line.strip() == f'"{LABEL}" => disabled' for line in lines)


def enable():
    active = job_pid() is not None
    ctl('enable', TARGET)
    if active:
        ctl('kickstart', TARGET)
    else:
        ctl('bootstrap', DOMAIN, str(PLIST))


def pause():
    active = job_pid() is not None
    ctl('disable', TARGET)  # Persists across logouts and reboots.
    if active:
        result = ctl('bootout', TARGET, check=False)
        if result.returncode and job_pid() is not None:
            raise ValueError('登录项已禁用，本次运行尚未停止')
