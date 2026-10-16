#!/usr/bin/env python3
"""Prepare, activate or revoke only the owner's approved conversation grant."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / 'core'))
from activity_mvp import conversation_scope
from activity_mvp.feedback.store import atomic, packed
from activity_mvp.feedback.control import gate


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('prepare', 'activate', 'revoke'))
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--owner-session')
    parser.add_argument('--owner-turn')
    args = parser.parse_args()
    runtime = Path.home() / 'Library/Application Support/Personal AI Monitor/core'
    database = runtime / 'data/feedback/feedback.sqlite'
    target = conversation_scope.policy_path()
    if args.action == 'revoke':
        with gate(database.parent):
            value = conversation_scope.authorization()
            value.update(enabled=False, revision=value['revision'] + 1)
            atomic(target, packed(value))
            if conversation_scope.authorization()!=value:raise ValueError('authorization_readback_mismatch')
        print(packed({'status': 'revoked', 'revision': value['revision']}))
        return
    if args.candidate is None or not args.candidate.is_absolute():
        raise ValueError('private_candidate_path_required')
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=2) as conn:
        conn.execute('PRAGMA query_only=ON')
        sources = [{'id': ident, 'platform': platform, 'path': path}
                   for ident, platform, path in conn.execute('SELECT id,platform,path FROM sources ORDER BY id')
                   if platform in {'codex', 'claude'} or (platform == 'hermes' and ident == 'hermes')]
    if not sources:
        raise ValueError('registered_conversation_sources_missing')
    if args.action == 'prepare':
        if (not args.owner_session or args.owner_session != os.environ.get('CODEX_THREAD_ID')
                or not args.owner_turn):
            raise ValueError('current_owner_task_identity_required')
        value = {'schema_version': 1, 'policy': conversation_scope.POLICY, 'owner_id': 'local-owner',
                 'enabled': True, 'lookback_days': 14, 'revision': 1, 'sources': sources,
                 'authorization': {'kind': 'owner_explicit', 'platform': 'codex',
                     'session': args.owner_session, 'turn': args.owner_turn,
                     'observed_at': datetime.now(timezone.utc).isoformat()}}
        # Preparing is not activation and cannot replace an earlier candidate.
        fd = os.open(args.candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(packed(value)); stream.flush(); os.fsync(stream.fileno())
        print(packed({'status': 'prepared', 'source_count': len(sources), 'active': False}))
        return
    info = conversation_scope._regular(args.candidate)
    if info.st_mode & 0o077 or info.st_size > 65536:
        raise ValueError('candidate_not_private')
    value = conversation_scope.authorization(args.candidate)
    if value.get('sources') != sources or value.get('policy') != conversation_scope.POLICY:
        raise ValueError('source_registration_changed')
    # The ordinary paired release and existing operator acceptance must already
    # have succeeded. This command cannot qualify or install an application.
    module = runtime / 'activity_mvp/conversation_scope.py'
    expected = REPOSITORY / 'core/activity_mvp/conversation_scope.py'
    if not module.is_file() or hashlib.sha256(module.read_bytes()).digest() != hashlib.sha256(expected.read_bytes()).digest():
        raise ValueError('matching_conversation_core_not_installed')
    # A fresh isolated process imports the actual installed modules, never
    # modules already loaded from the candidate checkout in this operator.
    verify = '''import sys,json,plistlib
from pathlib import Path
root=Path.home()/'Library/Application Support/Personal AI Monitor/core'
sys.path.insert(0,str(root))
from activity_mvp.feedback.acceptance import release_ready
from activity_mvp.feedback.release import core_hash
from activity_mvp.feedback.store import Store
with Store(root/'data/feedback',readonly=True) as store:
 print(json.dumps({'paired':core_hash(root)==plistlib.loads(Path('/Applications/AGI.app/Contents/Info.plist').read_bytes()).get('PAMCoreCodeHash'),'accepted':release_ready(store)}))
'''
    checked = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', verify],
        capture_output=True, text=True, timeout=30)
    state = json.loads(checked.stdout) if checked.returncode == 0 else {}
    if state.get('paired') is not True or state.get('accepted') is not True:
        raise ValueError('paired_operator_accepted_release_required')
    with gate(database.parent):
        if target.exists():
            previous = conversation_scope.authorization()
            value['revision'] = previous['revision'] + 1
        atomic(target, packed(value))
        validated = conversation_scope.authorization()
        if validated != value:
            raise ValueError('authorization_readback_mismatch')
    print(packed({'status': 'activated', 'revision': value['revision'], 'source_count': len(sources)}))


if __name__ == '__main__':
    main()
