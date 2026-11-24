"""Exercise the public entry point with a fresh home and fictional data."""
from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

WEB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WEB))
import psi_sync_center


class PublicDemoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / 'home'
        self.home.mkdir()
        self.scratch = self.root / 'tmp'
        self.scratch.mkdir()

    @contextmanager
    def session(self, *arguments):
        with socket.socket() as available:
            available.bind(('127.0.0.1', 0))
            port = available.getsockname()[1]
        env = {**os.environ, 'HOME': str(self.home), 'TMPDIR': str(self.scratch),
               'PYTHONNOUSERSITE': '1'}
        process = subprocess.Popen(
            [sys.executable, '-B', str(WEB / 'psi_sync_center.py'), '--port', str(port), *arguments],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        self.origin = f'http://127.0.0.1:{port}'
        self.port = port
        try:
            deadline = time.monotonic() + 10
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    http = self.request('/')
                    break
                except ConnectionRefusedError:
                    time.sleep(0.05)
            else:
                self.fail('Demo did not become available')
            self.assertEqual(http[0], 200)
            html = http[1].decode()
            self.assertIn('PSI · 问题与进展', html)
            self.session_token = re.search(r'name="csrf-token" content="([0-9a-f]+)"', html)[1]
            yield
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
            try:
                _, errors = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                raise
            self.assertEqual(process.returncode, 0, errors)

    def request(self, path, data=None, api=False):
        headers = {'X-PSI-Session': self.session_token} if api else {}
        body = None
        if data is not None:
            headers.update({'Content-Type': 'application/json', 'Origin': self.origin})
            body = json.dumps(data).encode()
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        try:
            connection.request('POST' if body is not None else 'GET', path, body, headers)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def api(self, path, data=None):
        status, raw = self.request(path, data, api=True)
        self.assertEqual(status, 200)
        return json.loads(raw)

    def test_default_session_is_synthetic_and_drafts_are_temporary(self):
        chat = '/api/workbench/demo-event/conversation'
        with self.session():
            self.assertIs(self.api('/api/state')['synthetic'], True)
            self.assertIs(self.api('/api/workbench/demo-event')['synthetic'], True)
            self.assertIn('demo-event', [item['id'] for item in self.api('/api/workbench')['items']])
            self.assertTrue(self.api('/api/workbench/demo-event/draft', {'content': 'sample draft'})['saved'])
            self.assertEqual(self.api(chat)['draft'], 'sample draft')
        self.assertEqual(list(self.scratch.glob('psi-demo-*')), [])
        self.assertEqual(list(self.home.iterdir()), [])
        with self.session():
            self.assertEqual(self.api(chat)['draft'], '')

    def test_persistent_drafts_and_fixture_do_not_import_operator_reviews(self):
        drafts = self.root / 'drafts'
        drafts.mkdir(mode=0o700)
        reviews = drafts / 'reviews.json'
        reviews.write_text(json.dumps({'schema': 1, 'reviews': {}, 'baseline': [
            {'id': 'unrelated-review', 'title': 'Separate operator evidence'}]}))
        reviews.chmod(0o600)
        for index in range(2):
            with self.session('--data-dir', str(drafts), '--fixture', str(psi_sync_center.DEMO)):
                self.assertNotIn('unrelated-review', [r['id'] for r in self.api('/api/workbench?scope=all')['items']])
                if index == 0:
                    self.api('/api/workbench/demo-event/draft', {'content': 'remember this sample'})
                self.assertEqual(self.api('/api/workbench/demo-event/conversation')['draft'], 'remember this sample')

    def test_demo_refresh_never_uses_live_core_or_review_files(self):
        with patch('psi_sync.core_reader.CoreConfig.argv', side_effect=AssertionError('live Core used')), \
                patch('psi_sync_center.Reviews', side_effect=AssertionError('review store used')), \
                patch('psi_sync_center.make_server') as server:
            server.return_value.__enter__.return_value.serve_forever.side_effect = KeyboardInterrupt
            self.assertEqual(psi_sync_center.main(['--demo', '--data-dir', str(self.root / 'demo')]), 0)
