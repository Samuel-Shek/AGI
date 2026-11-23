import copy
from email.message import Message
from html.parser import HTMLParser
import io
from email.parser import Parser
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from psi_sync.core_reader import CoreConfig, CoreReader, ReadError, normalize_snapshot, bounded_run
from psi_sync.storage import LocalStore, StorageError, validate_directory
from psi_sync.state import State, version, personal_question
from psi_sync.http_server import LocalServer, handler_for, make_server
from psi_sync.security import authorize, body_length, SecurityError, AUTHORITY, ORIGIN

FIXTURE = ROOT / 'tests/fixtures/demo.json'


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.reader = CoreReader(fixture=FIXTURE)
        self.directory = Path(self.temp.name) / 'private'
        self.store = LocalStore(self.directory, self.reader.cache_identity)
        self.state = State(self.reader, self.store)
        self.state.refresh()

    def payload(self, action='continue_automatic', identifier='demo-event', **kw):
        return dict(action=action, revision=version(self.state.items[identifier]), request_id='request-1', **kw)


class MappingTests(Base):
    def test_classification_and_badge(self):
        self.assertEqual(self.state.listing()['counts'], {'attention': 5, 'automatic': 2, 'waiting': 2, 'closed': 2, 'all': 11})
        self.assertEqual(len(self.state.listing()['items']), 5)

    def test_ended_hidden_unless_explicit_filter(self):
        self.assertNotIn('demo-closed', [r['id'] for r in self.state.listing()['items']])
        self.assertIn('demo-closed', [r['id'] for r in self.state.listing('closed')['items']])
        self.assertEqual(len(self.state.listing('all')['items']), 11)

    def test_unknown_counts_remain_unknown(self):
        raw = json.loads(FIXTURE.read_text())['snapshot']
        raw['decision_count'] = None
        self.state.snapshot = normalize_snapshot(raw)
        self.assertIsNone(self.state.view()['metrics']['needs'])
        self.state.snapshot['status'] = 'partial'
        self.assertTrue(all(v is None for v in self.state.listing()['counts'].values()))
        self.assertIsNone(self.state.view()['metrics']['resolved'])

    def test_sources_systems_history_children(self):
        view = self.state.view()
        self.assertEqual(len(view['sources']), 3)
        self.assertEqual(len(view['systems']), 2)
        self.assertEqual(view['history'][0]['items'][0]['id'], 'demo-history-child')
        self.assertIn('demo-history-child', self.state.items)

    def test_history_counts_unique_ids(self):
        view = self.state.view()
        ids = [row['id'] for group in view['history'] for row in group['items']]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(view['metrics']['history'], 3)

    def test_shared_history_child_does_not_inflate_metric(self):
        duplicate = copy.deepcopy(self.state.snapshot['resources'][-1])
        duplicate['id'] = 'another-history-parent'
        self.state.snapshot['resources'].append(duplicate)
        self.assertEqual(self.state.view()['metrics']['history'], 3)

    def test_numbered_detail_fields_long_text(self):
        value = self.state.detail('demo-preference')
        self.assertGreater(len(value['evidence_summary']), 900)
        for field in ('title', 'background', 'reason', 'completion_rule', 'revision'):
            self.assertTrue(value[field])

    def test_only_personal_structured_questions(self):
        self.assertTrue(self.state.detail('demo-event')['question'])
        self.assertEqual(personal_question({'requires_decision': True, 'title': '请替我修复进程', 'card': {}}), '')
        self.assertEqual(personal_question({'requires_decision': True, 'card': {'verification_route': 'user_confirmation', 'event_type': 'technical'}}), '')

    def test_local_answer_closes_only_local_view(self):
        before = copy.deepcopy(self.state.items)
        self.state.write('demo-event', 'action', self.payload())
        self.assertEqual(before, self.state.items)
        self.assertEqual(self.state.view()['metrics']['needs'], 4)
        self.assertIn('demo-event', [x['id'] for x in self.state.listing('closed')['items']])
        self.state.refresh()
        self.assertEqual(self.state.view()['metrics']['needs'], 4)

    def test_new_revision_reopens_local_answer(self):
        self.state.write('demo-event', 'action', self.payload())
        self.state.items['demo-event']['revision'] = 'new'
        self.assertEqual(self.state.view()['metrics']['needs'], 5)

    def test_failure_and_scope_withdrawal_clear_ids(self):
        with patch.object(self.reader, 'snapshot', side_effect=ReadError('scope_withdrawn')):
            value = self.state.refresh()
        self.assertEqual(value['status'], 'unavailable')
        self.assertEqual(self.state.items, {})
        self.assertIsNone(value['metrics']['needs'])
        with self.assertRaises(ReadError):
            self.state.write('demo-event', 'message', {'content': 'x', 'request_id': 'x'})

    def test_detail_scope_withdrawal_clears_registered_ids(self):
        with patch.object(self.reader, 'detail', side_effect=ReadError('scope_withdrawn')):
            with self.assertRaises(ReadError):
                self.state.detail('demo-event')
        self.assertFalse(self.state.items)

    def test_synthetic_multiturn_explicitly_labelled(self):
        chat = self.state.conversation('demo-preference')
        self.assertEqual(len(chat['messages']), 4)
        self.assertTrue(all(row['synthetic'] for row in chat['messages']))
        self.assertIn('保存笔记仅在本机', chat['notice'])


class StorageTests(Base):
    def test_conversation_persists_across_restart_per_id(self):
        self.state.write('demo-event', 'message', {'content': '仅用于测试的想法', 'request_id': 'm-1'})
        fresh = State(self.reader, LocalStore(self.directory, self.reader.cache_identity))
        fresh.refresh()
        rows = fresh.conversation('demo-event')['messages']
        self.assertEqual(rows[0]['content'], '仅用于测试的想法')
        self.assertEqual(rows[0]['author'], 'user')
        self.assertEqual(fresh.conversation('demo-choice')['messages'], [])
        self.assertEqual(fresh.view()['metrics']['needs'], 5)  # chat never closes issues

    def test_no_ai_generated_reply(self):
        self.state.write('demo-event', 'message', {'content': '你好', 'request_id': 'm-1'})
        self.assertEqual([x['author'] for x in self.state.conversation('demo-event')['messages']], ['user'])

    def test_draft_restart_and_send_clears_draft(self):
        self.state.write('demo-event', 'draft', {'content': '草稿'})
        store = LocalStore(self.directory, self.reader.cache_identity)
        self.assertEqual(store.read()['drafts']['demo-event'], '草稿')
        self.state.write('demo-event', 'message', {'content': '草稿', 'request_id': 'm-1'})
        self.assertEqual(self.state.conversation('demo-event')['draft'], '')

    def test_private_permissions_atomic_replace(self):
        with patch('psi_sync.storage.os.replace', wraps=os.replace) as replace:
            self.state.write('demo-event', 'message', {'content': 'x', 'request_id': 'm-1'})
        self.assertEqual(replace.call_count, 1)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)
        self.assertEqual(list(self.directory.glob('.local-*')), [])

    def test_replace_failure_preserves_original(self):
        self.state.write('demo-event', 'message', {'content': 'original', 'request_id': 'm-1'})
        before = self.store.path.read_bytes()
        with patch('psi_sync.storage.os.replace', side_effect=OSError('synthetic')):
            with self.assertRaises(StorageError):
                self.state.write('demo-event', 'message', {'content': 'new', 'request_id': 'm-2'})
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(len(self.store.read()['messages']['demo-event']), 1)

    def test_directory_fsync_failure_keeps_committed_records(self):
        real_fsync = os.fsync
        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError('synthetic directory fsync failure')
            return real_fsync(fd)
        with patch('psi_sync.storage.os.fsync', side_effect=fail_directory):
            with self.assertRaisesRegex(StorageError, 'durability_unconfirmed'):
                self.state.write('demo-event', 'message', {'content': 'committed', 'request_id': 'm-1'})
        self.assertEqual(len(self.store.read()['messages']['demo-event']), 1)
        self.state.write('demo-event', 'message', {'content': 'next', 'request_id': 'm-2'})
        restarted = LocalStore(self.directory, self.reader.cache_identity)
        self.assertEqual([r['content'] for r in restarted.read()['messages']['demo-event']], ['committed', 'next'])

    def test_damaged_file_is_preserved(self):
        self.store.path.write_text('{broken')
        self.store.path.chmod(0o600)
        with self.assertRaises(StorageError):
            LocalStore(self.directory, self.reader.cache_identity)
        self.assertEqual(self.store.path.read_text(), '{broken')

    def test_symlink_hardlink_and_permissions_rejected(self):
        self.state.write('demo-event', 'draft', {'content': 'x'})
        self.store.path.chmod(0o644)
        with self.assertRaises(StorageError):
            LocalStore(self.directory, self.reader.cache_identity)
        self.store.path.chmod(0o600)
        other = self.directory / 'other'
        os.link(self.store.path, other)
        with self.assertRaises(StorageError):
            LocalStore(self.directory, self.reader.cache_identity)
        other.unlink()
        self.store.path.unlink()
        self.store.path.symlink_to(other)
        with self.assertRaises(StorageError):
            LocalStore(self.directory, self.reader.cache_identity)

    def test_core_or_link_directory_rejected(self):
        with self.assertRaises(StorageError):
            validate_directory(Path(self.temp.name) / 'core/data/local')
        link = Path(self.temp.name) / 'link'
        link.symlink_to(self.directory)
        with self.assertRaises(StorageError):
            LocalStore(link, 'test')

    def test_source_identity_isolation(self):
        self.state.write('demo-event', 'draft', {'content': 'x'})
        self.assertEqual(LocalStore(self.directory, 'another-source').read()['drafts'], {})

    def test_idempotency_and_conflict(self):
        payload = {'content': 'one', 'request_id': 'm-1'}
        self.state.write('demo-event', 'message', payload)
        self.state.write('demo-event', 'message', payload)
        self.assertEqual(len(self.state.conversation('demo-event')['messages']), 1)
        with self.assertRaises(ValueError):
            self.state.write('demo-event', 'message', {**payload, 'content': 'two'})

    def test_concurrent_messages_no_lost_updates(self):
        threads = [threading.Thread(target=self.state.write, args=('demo-event', 'message', {'content': str(i), 'request_id': 'm-' + str(i)})) for i in range(12)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(len(self.state.conversation('demo-event')['messages']), 12)

    def test_all_actions_local_and_never_call_reader(self):
        with patch.object(self.reader, '_raw', side_effect=AssertionError('Core must not be called')):
            for index, action in enumerate(('resolve_plan', 'defer', 'continue_automatic', 'write_memory')):
                payload = self.payload(action, 'demo-choice', content='合成选择', until='2099-01-01T00:00:00+08:00')
                payload['request_id'] = 'action-' + str(index)
                self.state.write('demo-choice', 'action', payload)
            self.state.write('local-sync', 'sync', {'request_id': 'sync-1'})
        self.assertEqual(len(self.store.read()['actions']), 5)

    def test_invalid_actions_and_dates_rejected(self):
        for payload in [self.payload('execute'), self.payload('defer', until='yesterday'), self.payload('defer', until='2000-01-01T00:00:00+08:00'), self.payload('write_memory'), {**self.payload(), 'revision': 'stale'}]:
            with self.assertRaises(ValueError):
                self.state.write('demo-event', 'action', payload)
        self.assertFalse(self.store.read()['actions'])


class ReaderTests(Base):
    def test_allowlist_registered_ids_and_argument_arrays(self):
        config = CoreConfig()
        self.assertEqual(config.argv('snapshot')[-2:], ['attention-center', 'snapshot'])
        self.assertEqual(config.argv('detail', 'demo-event')[-4:], ['attention-center', 'detail', '--id', 'demo-event'])
        for action in ('sync', 'write', 'delete', 'snapshot; pwd'):
            with self.assertRaises(ValueError): config.argv(action)
        for identifier in ('../secret', 'x; echo', 'unregistered'):
            with self.assertRaises(ReadError): self.reader.detail(identifier, {'demo-event'})

    def test_fixture_never_starts_subprocess(self):
        with patch('psi_sync.core_reader.subprocess.Popen', side_effect=AssertionError('must not spawn')):
            self.state.refresh()
            self.state.detail('demo-event')

    def test_production_reader_only_allowlisted_commands_mocked(self):
        reader = CoreReader()
        bundle = json.loads(FIXTURE.read_text())
        with patch('psi_sync.core_reader.bounded_run', side_effect=[json.dumps(bundle['snapshot']).encode(), json.dumps(bundle['details']['demo-event']).encode()]) as run:
            reader.snapshot(); reader.detail('demo-event', {'demo-event'})
        self.assertEqual([call.args[0][7] for call in run.call_args_list], ['snapshot', 'detail'])

    def test_reader_timeout_and_output_limits_with_synthetic_child(self):
        with self.assertRaisesRegex(ReadError, 'output_limit'):
            bounded_run([sys.executable, '-I', '-S', '-c', 'print("x" * 20000)'], 2, 1024)
        with self.assertRaisesRegex(ReadError, 'timeout'):
            bounded_run([sys.executable, '-I', '-S', '-c', 'import time; time.sleep(1)'], .05, 1024)

    def test_contract_type_and_mismatch(self):
        raw = json.loads(FIXTURE.read_text())['snapshot']
        with self.assertRaises(ReadError): normalize_snapshot({**raw, 'contract': True})
        self.reader._raw = lambda *args: {'interface': 'attention-center', 'contract': 1, 'status': 'ok', 'item': {'id': 'different'}}
        with self.assertRaisesRegex(ReadError, 'detail_id_mismatch'):
            self.reader.detail('demo-event', {'demo-event'})


class SecurityTests(unittest.TestCase):
    def headers(self, **changes):
        data = {'Host': AUTHORITY, 'Origin': ORIGIN, 'X-PSI-Session': 'token', 'Content-Type': 'application/json', 'Content-Length': '2'}
        data.update(changes)
        msg = Message()
        for key, value in data.items():
            if value is not None: msg[key] = value
        return msg

    def test_host_origin_and_token(self):
        authorize(self.headers(), 'token', write=True)
        for changes in ({'Host': 'localhost:8767'}, {'Host': 'evil.test'}, {'Origin': 'null'}, {'Origin': 'https://evil.test'}, {'Origin': None}, {'X-PSI-Session': 'wrong'}, {'X-PSI-Session': None}, {'Sec-Fetch-Site': 'cross-site'}):
            with self.subTest(changes=changes), self.assertRaises(SecurityError): authorize(self.headers(**changes), 'token', write=True)

    def test_duplicate_headers_rejected(self):
        for name in ('Host', 'Origin', 'X-PSI-Session'):
            headers = self.headers(); headers[name] = headers[name]
            with self.assertRaises(SecurityError): authorize(headers, 'token', write=True)

    def test_framing_and_size_rejected(self):
        for changes in ({'Content-Length': '32769'}, {'Content-Length': '-1'}, {'Transfer-Encoding': 'chunked'}, {'Expect': '100-continue'}, {'Content-Type': 'text/plain'}):
            with self.assertRaises(SecurityError): body_length(self.headers(**changes))


class HTTPTests(Base):
    def request(self, path, body=None, headers=None, method=None):
        # Exercise BaseHTTPRequestHandler's real wire parser and router without
        # binding a port (the execution sandbox denies even loopback listeners).
        data = {'Host': AUTHORITY, 'X-PSI-Session': 'test-token'}
        raw = json.dumps(body, ensure_ascii=False).encode() if body is not None else b''
        if body is not None:
            data.update({'Origin': ORIGIN, 'Content-Type': 'application/json', 'Content-Length': str(len(raw))})
        data.update(headers or {})
        verb = method or ('POST' if body is not None else 'GET')
        wire = (verb + ' ' + path + ' HTTP/1.1\r\n' + ''.join(k + ': ' + v + '\r\n' for k,v in data.items()) + '\r\n').encode() + raw
        class MemorySocket:
            output = b''
            def makefile(self, *args): return io.BytesIO(wire)
            def sendall(self, block): self.output += bytes(block)
        connection = MemorySocket()
        handler_for(self.state, 'test-token')(connection, ('127.0.0.1', 12345), object())
        head, response = connection.output.split(b'\r\n\r\n', 1)
        status, fields = head.split(b'\r\n', 1)
        return int(status.split()[1]), dict(Parser().parsestr(fields.decode())), response

    def test_index_and_assets_csp_token_injection(self):
        status, headers, body = self.request('/')
        self.assertEqual(status, 200); self.assertIn(b'test-token', body)
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        for path in ('/app.css', '/app.js', '/workbench.js', '/favicon.svg'):
            self.assertEqual(self.request(path)[0], 200)

    def test_post_token_origin_and_host_rejected(self):
        for headers in ({'X-PSI-Session': 'bad'}, {'Origin': 'null'}, {'Host': 'evil.test'}):
            self.assertEqual(self.request('/api/workbench/demo-event/message', {'content': 'x', 'request_id': 'm-1'}, headers)[0], 403)
        self.assertEqual(self.store.read()['messages'], {})

    def test_get_api_token_required_and_no_path_traversal(self):
        self.assertEqual(self.request('/api/state', headers={'X-PSI-Session': ''})[0], 403)
        for path in ('/../psi_sync/state.py', '/api/workbench/..%2Fsecret', '/missing'):
            self.assertNotEqual(self.request(path)[0], 200)

    def test_chat_roundtrip_draft_and_idempotency(self):
        payload = {'content': '合成测试', 'request_id': 'm-1'}
        self.assertEqual(self.request('/api/workbench/demo-event/draft', {'content': '草稿'})[0], 200)
        for _ in range(2): self.assertEqual(self.request('/api/workbench/demo-event/message', payload)[0], 200)
        chat = json.loads(self.request('/api/workbench/demo-event/conversation')[2])
        self.assertEqual(len(chat['messages']), 1); self.assertEqual(chat['draft'], '')

    def test_refresh_and_sync_have_distinct_effects(self):
        with patch.object(self.reader, 'snapshot', wraps=self.reader.snapshot) as read:
            self.assertEqual(self.request('/api/sync', {'request_id': 'sync-1'})[0], 200)
            self.assertEqual(read.call_count, 0)
            self.assertEqual(self.request('/api/refresh', {})[0], 200)
            self.assertEqual(read.call_count, 1)

    def test_busy_port_fails_without_killing_process(self):
        with patch('socketserver.socket.socket') as socket_factory:
            sock = socket_factory.return_value
            sock.bind.side_effect = OSError('address already in use')
            with self.assertRaises(OSError): make_server(self.state)
            sock.bind.assert_called_once_with(('127.0.0.1', 8767))
            sock.close.assert_called_once()
            sock.listen.assert_not_called()
        self.assertFalse(LocalServer.allow_reuse_port)
        with patch('psi_sync.http_server.LocalServer') as server:
            make_server(self.state)
            self.assertEqual(server.call_args.args[0], ('127.0.0.1', 8767))

    def test_invalid_action_type_returns_controlled_error(self):
        self.assertEqual(self.request('/api/workbench/demo-event/action', self.payload(action=[]))[0], 400)

    def test_codex_continue_endpoint_requires_session_and_preserves_issue(self):
        payload={'revision':version(self.state.items['demo-event']),'content':'合成核对'}
        code,headers,raw=self.request('/api/workbench/demo-event/codex-continue',payload)
        body=json.loads(raw)
        self.assertEqual(code,200)
        self.assertEqual(body['phase'],'unavailable') # synthetic UI cannot send
        self.assertEqual(self.store.read()['actions'],[])

    def test_codex_continue_rejects_stale_version(self):
        self.assertEqual(self.request('/api/workbench/demo-event/codex-continue',{'revision':'old'})[0],400)

    def test_methods_and_unknown_operations_rejected(self):
        self.assertEqual(self.request('/api/sync', method='DELETE')[0], 501)
        self.assertEqual(self.request('/api/workbench/demo-event/execute', {})[0], 404)
        self.assertNotEqual(self.request('/api/workbench/unknown/message', {'request_id':'x','content':'x'})[0], 200)


class StaticTests(unittest.TestCase):
    def test_no_external_urls_or_removed_integrations(self):
        for path in (ROOT / 'static').iterdir():
            text = path.read_text()
            if path.suffix == '.svg': text = text.replace('http://www.w3.org/2000/svg', '') # namespace, no request
            # Embedded PNG bytes are not executable text or external dependencies.
            text = re.sub(r'data:image/png;base64,[A-Za-z0-9+/=]+', 'data:image/png;base64,', text)
            self.assertNotRegex(text, r'https?://|(?i:cdn|钉钉|邮件|微信|win11|model-usage|content-relations|/api/alerts)')
            self.assertNotRegex(text, r'(?i:eval\(|new WebSocket|EventSource|sendBeacon)')

    def test_html_balanced_and_unique_ids(self):
        class Parser(HTMLParser):
            def __init__(self): super().__init__(); self.ids=[]; self.stack=[]
            def handle_starttag(self, tag, attrs):
                if tag not in {'meta','link','img','input','br','hr'}: self.stack.append(tag)
                self.ids.extend(value for key, value in attrs if key == 'id')
            def handle_startendtag(self, tag, attrs): pass
            def handle_endtag(self, tag):
                if not self.stack or self.stack.pop() != tag: raise AssertionError('unbalanced ' + tag)
        parser=Parser(); parser.feed((ROOT/'static/index.html').read_text())
        self.assertEqual(parser.stack, []); self.assertEqual(len(parser.ids), len(set(parser.ids)))

    def test_layout_and_dialog_contract(self):
        css=(ROOT/'static/app.css').read_text(); html=(ROOT/'static/index.html').read_text(); js=(ROOT/'static/workbench.js').read_text()
        self.assertIn('@media (min-width: 1180px)', css)
        for cls in ('conversation-messages::before','conversation-message.from-user','memory-proposal-card','composer-wrap','case-workbench'):
            self.assertIn(cls, css)
        for name in ('sync-dialog','choice-dialog','defer-dialog'): self.assertIn('id="'+name+'"', html)
        for number in ('01','02','03','04'): self.assertIn('section("'+number+'"', js)

    def test_javascript_syntax_and_display_helpers(self):
        import shutil
        node=shutil.which('node')
        if not node: self.skipTest('Node unavailable; Python tests do not install dependencies')
        for name in ('app.js','workbench.js'):
            subprocess.run([node, '--check', str(ROOT/'static'/name)], check=True, capture_output=True)
        result=subprocess.run([node, str(ROOT/'tests/frontend_check.js'), str(ROOT/'static')], check=True, capture_output=True, text=True)
        self.assertIn('frontend checks passed', result.stdout)


if __name__ == '__main__': unittest.main()
