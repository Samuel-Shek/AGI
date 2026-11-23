import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('psi_local_lifecycle', ROOT/'local_web.py')
lifecycle = importlib.util.module_from_spec(spec);spec.loader.exec_module(lifecycle)


class LifecycleTests(unittest.TestCase):
    def test_receipt_and_log_reject_hardlinks_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp).resolve();os.chmod(directory,0o700)
            original = directory/'original';original.write_text('keep');os.chmod(original,0o600)
            with patch.object(lifecycle,'DIRECTORY',directory):
                os.link(original,directory/'runtime.json')
                with self.assertRaises(ValueError): lifecycle.save_receipt({'instance':'x'})
                self.assertEqual(original.read_text(),'keep')
                (directory/'runtime.json').unlink()
                (directory/'runtime.json').symlink_to(original)
                with self.assertRaises(OSError):lifecycle.save_receipt({'instance':'x'})
                os.link(original,directory/'service.log')
                with self.assertRaises(ValueError):lifecycle.private_fd(directory/'service.log',os.O_WRONLY | os.O_APPEND)

    def test_cross_process_lifecycle_lock_serializes_same_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp).resolve();os.chmod(directory,0o700)
            code = '''import sys,time,json
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import local_web as w
w.DIRECTORY=Path(sys.argv[2])
with w.lifecycle_lock():
 p=w.DIRECTORY/'counter.json'
 n=json.loads(p.read_text()) if p.exists() else 0
 time.sleep(0.2)
 p.write_text(json.dumps(n+1))
'''
            children=[subprocess.Popen([sys.executable,'-B','-c',code,str(ROOT),str(directory)]) for _ in range(2)]
            for child in children:self.assertEqual(child.wait(timeout=10),0)
            self.assertEqual(json.loads((directory/'counter.json').read_text()),2)
