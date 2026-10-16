"""Real local sandbox conformance. No model invocation or sensitive test material."""
from __future__ import annotations
import json
import os
from pathlib import Path
import socket
import subprocess
import time
from .store import private_directory,atomic,packed,digest
from .repair import sandbox_args


def verify(store):
    config=store.get('config');root=private_directory(store.root/'isolation');workspace=private_directory(root/'workspace')
    secret=root/'outside-sentinel.txt';atomic(secret,'synthetic-private-sentinel')
    writable=workspace/'inside.txt';atomic(writable,'before')
    script='''import os,socket,json
from pathlib import Path
r={}
try: Path(%r).read_text();r['outside_read_blocked']=False
except PermissionError:r['outside_read_blocked']=True
try: Path(%r).write_text('outside-change');r['outside_write_blocked']=False
except PermissionError:r['outside_write_blocked']=True
try: Path(%r).write_text('inside-change');r['workspace_write_allowed']=True
except OSError:r['workspace_write_allowed']=False
try: socket.create_connection(('127.0.0.1',%d),timeout=1).close();r['network_blocked']=False
except OSError:r['network_blocked']=True
print(json.dumps(r))
'''
    server=socket.socket();server.bind(('127.0.0.1',0));server.listen(1)
    atomic(workspace/'probe.py',script%(str(secret),str(secret),str(writable),server.getsockname()[1]))
    try:
        result=subprocess.run(sandbox_args(config['codex_binary'],workspace,[config['python'],str(workspace/'probe.py')]),capture_output=True,text=True,timeout=30)
    finally:server.close()
    passed=False;checks={}
    if result.returncode==0:
        try:checks=json.loads(result.stdout);passed=all(checks.get(k) is True for k in ['outside_read_blocked','outside_write_blocked','workspace_write_allowed','network_blocked']) and secret.read_text()=='synthetic-private-sentinel'
        except ValueError:pass
    receipt={'passed':passed,'checks':checks,'checked_at':time.time(),'binary':config['codex_binary'],'returncode':result.returncode,
             'output_sha256':digest(result.stdout+result.stderr)}
    atomic(root/'receipt.json',packed(receipt));atomic(root/'probe.log',result.stdout+'\n'+result.stderr)
    store.set('isolation',receipt)
    # Isolation is necessary, but never sufficient to claim model or release verified.
    if not passed:
        store.set('model_capability',{'enabled':False,'reason':'执行隔离检查未通过'})
        store.set('repair_capability',{'enabled':False,'reason':'执行隔离检查未通过'})
    return receipt
