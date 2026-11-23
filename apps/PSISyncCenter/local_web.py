#!/usr/bin/env python3
"""Instance-bound lifecycle for the standalone local workbench."""
import argparse
import socket
import web_agent
import http.client
import contextlib
import fcntl
import stat
import tempfile
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from psi_sync.storage import validate_directory
from psi_sync.reviews import private_json

DIRECTORY = Path('~/Library/Application Support/PSIWebWorkbench').expanduser()
PORT = 8768
URL = f'http://127.0.0.1:{PORT}'
PYTHON = '/Library/Frameworks/Python.framework/Versions/3.12/bin/python3'


def request(path, *, token=None, body=None):
    connection = http.client.HTTPConnection('127.0.0.1', PORT, timeout=2)
    headers = {'Host': f'127.0.0.1:{PORT}'}
    if token:
        headers.update({'X-PSI-Session': token, 'Origin': URL, 'Content-Type': 'application/json'})
    try:
        connection.request('POST' if body is not None else 'GET', path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read(2 * 1024 * 1024 + 1)
        if response.status != 200 or len(raw) > 2 * 1024 * 1024:
            raise ValueError('service_unavailable')
        return raw
    finally:
        connection.close()


def current():
    try:
        receipt = private_json(DIRECTORY / 'runtime.json')
        health = json.loads(request('/health'))
        if not isinstance(receipt, dict) or not isinstance(health, dict):
            return None
        return receipt if health.get('application') == 'PSISyncCenter' and health.get('instance') == receipt.get('instance') and receipt.get('instance') else None
    except (OSError, ValueError, TypeError, http.client.HTTPException):
        return None


def private_fd(path, flags):
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
        os.close(fd)
        raise ValueError('unsafe_private_file')
    return fd


@contextlib.contextmanager
def lifecycle_lock():
    directory = validate_directory(DIRECTORY)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        raise ValueError('private_directory_required')
    fd = private_fd(directory / 'lifecycle.lock', os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def save_receipt(receipt):
    path = DIRECTORY / 'runtime.json'
    if path.exists() or path.is_symlink():
        fd = private_fd(path, os.O_RDONLY)
        os.close(fd)
    fd, name = tempfile.mkstemp(prefix='.runtime-', dir=DIRECTORY)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(receipt, stream)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
        directory_fd = os.open(DIRECTORY, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try: os.fsync(directory_fd)
        finally: os.close(directory_fd)
    finally:
        if os.path.exists(name): os.unlink(name)


def start():
    if current():
        print(URL); return 0
    if not available_port():
        print('8768 端口已被占用；未停止或复用未知服务。', file=sys.stderr); return 1
    directory = validate_directory(DIRECTORY)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        raise ValueError('private_directory_required')
    instance = uuid.uuid4().hex
    log_path = directory / 'service.log'
    fd = private_fd(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    with os.fdopen(fd, 'ab') as log:
        argv = [PYTHON, '-B', str(Path(__file__).with_name('psi_sync_center.py')), '--standalone', '--port', str(PORT), '--instance', instance, '--data-dir', str(directory)]
        env = dict(os.environ)
        for key in ('PSI_APP_INSTANCE', 'PSI_APP_PARENT_PID'):
            env.pop(key, None)
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env, start_new_session=True)
    save_receipt({'pid': child.pid, 'instance': instance, 'started_at': time.time(), 'script': str(Path(__file__).resolve())})
    for _ in range(100):
        if child.poll() is not None:
            print('启动失败；原服务和资料保留。', file=sys.stderr); return 1
        if current():
            print(URL); return 0
        time.sleep(0.5)
    print('服务仍在首次读取；稍后打开本机地址核对，不重复启动。', file=sys.stderr)
    return 2


def stop():
    receipt = current()
    if not receipt:
        print('没有可核对的本次网页实例；未停止其他程序。'); return 1
    raw = request('/').decode()
    match = re.search(r'name="csrf-token" content="([a-f0-9]{64})"', raw)
    if not match or not current() or current()['instance'] != receipt['instance']:
        raise ValueError('instance_changed')
    request('/api/shutdown', token=match[1], body=b'{}')
    for _ in range(40):
        if not current():
            print('本地网页版已停止。'); return 0
        time.sleep(0.1)
    print('停止结果待核对，未强制结束进程。'); return 2


def available_port():
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(('127.0.0.1', PORT))
        except OSError:
            return False
        return True


def serve():
    # launchd owns this foreground PID. Publish its receipt only after bind.
    import psi_sync_center
    directory = validate_directory(DIRECTORY)
    if directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        raise ValueError('private_directory_required')
    instance = uuid.uuid4().hex
    script = Path(__file__).resolve()
    if web_agent.job_pid() != os.getpid():
        raise ValueError('该入口仅由本账号的网页登录项启动')

    def bound():
        save_receipt({'pid': os.getpid(), 'instance': instance, 'started_at': time.time(),
                      'script': str(script), 'manager': web_agent.LABEL})
    fd = private_fd(directory / 'service.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    with os.fdopen(fd, 'a', buffering=1) as log:
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            print(time.strftime('%Y-%m-%d %H:%M:%S %z'), 'launchd 启动网页服务', flush=True)
            return psi_sync_center.main(['--standalone', '--port', str(PORT), '--instance', instance,
                                         '--data-dir', str(directory)], on_bound=bound, allow_shutdown=False)


def managed(action):
    expected = web_agent.configuration(PYTHON, Path(__file__).resolve())
    installed = web_agent.installed(expected)
    if not installed and action != 'install':
        return start() if action == 'start' else stop()
    if action == 'stop':
        web_agent.pause()
        receipt = current()
        if receipt and receipt.get('manager') != web_agent.LABEL:
            if receipt.get('script') != str(Path(__file__).resolve()):
                raise ValueError('其他版本仍在运行，请核对原实例')
            return stop()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if web_agent.job_pid() is None and not current():
                print('本地网页版已停止，下次登录仍保持停止。'); return 0
            time.sleep(0.2)
        raise ValueError('已要求停止，仍待确认')
    active = web_agent.job_pid() is not None
    receipt = current()
    if action == 'install' and installed and web_agent.disabled():
        print('保留网页服务的手动停用状态。请用「打开PSI网页版」恢复。', file=sys.stderr)
        return 2
    if not active and not receipt and not available_port():
        raise ValueError('8768 端口不可用，保留原监听者')
    legacy = receipt and receipt.get('manager') != web_agent.LABEL
    if legacy and receipt.get('script') != str(Path(__file__).resolve()):
        raise ValueError('其他版本正在使用端口，请先核对原实例')
    if not installed:
        web_agent.publish(expected)
    if legacy:
        if stop():
            raise ValueError('未能交接手动启动的实例')
    web_agent.enable()
    deadline = time.monotonic() + 50
    while time.monotonic() < deadline:
        receipt = current()
        if receipt and receipt.get('manager') == web_agent.LABEL and receipt.get('pid') == web_agent.job_pid():
            print(URL); return 0
        time.sleep(0.5)
    raise ValueError('登录项已配置，但网页健康检查尚未通过')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['install', 'serve', 'start', 'stop', 'status'])
    args = parser.parse_args()
    try:
        if args.action == 'serve':
            return serve()
        if args.action in {'install', 'start', 'stop'}:
            with lifecycle_lock():
                return managed(args.action)
        print(URL if current() else '未运行或实例未能核对。')
        return 0
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired) as exc:
        print(f'PSI 网页服务操作未完成：{exc}', file=sys.stderr); return 1


if __name__ == '__main__':
    sys.exit(main())
