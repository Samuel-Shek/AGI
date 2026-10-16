"""Explicit synthetic personal registrations; production admission stays intact.

Only this test's owned temporary directory and enumerated sessions are admitted.
Company retirement, unknown/mixed sources, ownership and current-byte checks are
not bypassed. No runtime registry or policy file is written.
"""
from pathlib import Path
import sys
from unittest.mock import patch

from activity_mvp.source_governance import personal_sources as scope


def register_personal(test, root, sessions=(), source_ids=()):
    root = Path(root).resolve()
    policy = {'version': 'personal-v1', 'owner_id': 'local-owner',
              'cutover_at': '2026-01-01T00:00:00+00:00',
              'company_roots': [str(root / 'company')],
              'personal_roots': [str(root)], 'personal_files': [],
              'personal_source_ids': list(source_ids), 'personal_session_ids': list(sessions)}
    for guard in (patch.object(scope, 'company_stopped', return_value=True),
                  patch.object(scope, 'policy', return_value=policy)):
        guard.start()
        test.addCleanup(guard.stop)
    return policy
