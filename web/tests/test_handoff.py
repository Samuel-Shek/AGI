import json,sys
from unittest.mock import patch
from test_sync_center import Base
from psi_sync.handoff import core_command,public_result
from psi_sync.core_reader import bounded_run,ReadError,CoreConfig

class HandoffTransportTests(Base):
    def test_unicode_stdin_is_bounded_and_complete(self):
        payload=json.dumps({'content':'验收'*4000},ensure_ascii=False).encode()
        out=bounded_run([sys.executable,'-c','import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())'],5,32768,payload)
        self.assertEqual(out,payload)
    def test_arbitrary_action_is_rejected(self):
        with self.assertRaises(ReadError):core_command(CoreConfig(),'apply','demo-event',{})
    def test_ack_is_not_receipt(self):
        result=public_result({'status':'ok','handoff':{'status':'verifying','native_received':True}})
        self.assertEqual(result['phase'],'pending')
    def test_delivered_needs_persisted_receipt(self):
        self.assertEqual(public_result({'status':'ok','handoff':{'status':'delivered','receipt':None}})['phase'],'uncertain')

    def test_controller_runs_from_its_installed_root_for_worker_import(self):
        from pathlib import Path
        with patch('psi_sync.handoff.bounded_run',return_value=b'{"status":"ok"}') as run:
            core_command(CoreConfig(),'handoff-status','demo-event')
        self.assertEqual(run.call_args.kwargs['cwd'],str(Path(CoreConfig().launcher).expanduser().resolve().parent))
