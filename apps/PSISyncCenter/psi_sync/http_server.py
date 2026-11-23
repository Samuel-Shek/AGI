"""Loopback-only transport, fixed asset routes, session-protected APIs."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape
import json
from pathlib import Path
import secrets
import os
import socket
from urllib.parse import parse_qs, urlsplit, unquote

from .core_reader import ReadError, decode
from .security import CSP, HOST, PORT, SecurityError, authorize, body_length
from .storage import StorageError
from .handoff import continue_in_codex, handoff_status

STATIC = Path(__file__).resolve().parents[1] / 'static'
ASSETS = {'/': ('index.html', 'text/html; charset=utf-8'),
          '/app.css': ('app.css', 'text/css; charset=utf-8'),
          '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
          '/workbench.js': ('workbench.js', 'text/javascript; charset=utf-8'),
          '/favicon.svg': ('favicon.svg', 'image/svg+xml')}


class LocalServer(ThreadingHTTPServer):
    allow_reuse_address = True
    allow_reuse_port = False
    daemon_threads = True
    request_queue_size = 16

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(5)
        return connection, address


def handler_for(state, token, port=PORT, instance="", allow_shutdown=False):
    service_instance = instance or os.environ.get('PSI_APP_INSTANCE', '')
    class Handler(BaseHTTPRequestHandler):
        server_version = 'PSISyncCenter'
        sys_version = ''

        def log_message(self, *args):
            pass

        def send_error(self, code, message=None, explain=None):
            self.reply({'message': '请求不可用'}, code)

        def reply(self, payload, status=200, mime='application/json; charset=utf-8'):
            data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            for name, value in [('Content-Type', mime), ('Content-Length', str(len(data))),
                                ('Content-Security-Policy', CSP), ('X-Content-Type-Options', 'nosniff'),
                                ('X-Frame-Options', 'DENY'), ('Referrer-Policy', 'no-referrer'),
                                ('Cache-Control', 'no-store'), ('Connection', 'close')]:
                self.send_header(name, value)
            self.end_headers()
            self.close_connection = True
            self.wfile.write(data)

        def route(self):
            if not self.path.startswith('/') or self.path.startswith('//'):
                raise SecurityError('invalid_target')
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc or parsed.fragment:
                raise SecurityError('invalid_target')
            return parsed

        def do_GET(self):
            try:
                parsed = self.route()
                authorize(self.headers, token, api=parsed.path.startswith('/api/'), port=port)
                if self.headers.get_all('Transfer-Encoding') or self.headers.get_all('Content-Length'):
                    raise SecurityError('get_body_rejected')
                if parsed.path == '/health' and not parsed.query:
                    self.reply({'application': 'PSISyncCenter', 'instance': service_instance})
                elif parsed.path in ASSETS and not parsed.query:
                    name, mime = ASSETS[parsed.path]
                    raw = (STATIC / name).read_bytes()
                    if parsed.path == '/':
                        raw = raw.replace(b'__CSRF_TOKEN__', token.encode())
                        raw = raw.replace(b'__PSI_INSTANCE__', escape(service_instance, quote=True).encode())
                    self.reply(raw, mime=mime)
                elif parsed.path == '/api/state' and not parsed.query:
                    self.reply(state.view())
                elif parsed.path == '/api/workbench':
                    query = parse_qs(parsed.query, strict_parsing=True, keep_blank_values=True, max_num_fields=1)
                    if set(query) - {'scope'} or any(len(v) != 1 for v in query.values()):
                        raise ValueError('invalid_query')
                    self.reply(state.listing(query.get('scope', [None])[0]))
                elif parsed.path.startswith('/api/workbench/') and not parsed.query:
                    parts = parsed.path.split('/')[3:]
                    identifier = unquote(parts[0])
                    if len(parts) == 1:
                        self.reply(state.detail(identifier))
                    elif len(parts) == 2 and parts[1] == 'conversation':
                        self.reply(state.conversation(identifier))
                    else:
                        self.reply({'message': '未找到'}, 404)
                else:
                    self.reply({'message': '未找到'}, 404)
            except SecurityError:
                self.reply({'message': '请求未通过本机校验'}, 403)
            except ReadError as exc:
                self.reply({'message': '详情未能读取，请刷新后重试', 'error': str(exc)}, 404 if str(exc) == 'unregistered_id' else 503)
            except (ValueError, OSError, socket.timeout):
                self.reply({'message': '请求不可用'}, 400)

        def do_POST(self):
            try:
                parsed = self.route()
                authorize(self.headers, token, write=True, api=True, port=port)
                if parsed.query:
                    raise ValueError('invalid_query')
                length = body_length(self.headers)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError('incomplete_body')
                value = decode(raw)
                if not isinstance(value, dict):
                    raise ValueError('invalid_body')
                if parsed.path == '/api/shutdown' and allow_shutdown and value == {}:
                    self.reply({'status': 'stopping'})
                    import threading
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                elif parsed.path == '/api/refresh':
                    self.reply(state.refresh())
                elif parsed.path == '/api/sync':
                    self.reply(state.write('local-sync', 'sync', value))
                elif parsed.path.startswith('/api/workbench/'):
                    parts = parsed.path.split('/')[3:]
                    if len(parts) == 2 and parts[1] == 'codex-continue':
                        self.reply(continue_in_codex(state, unquote(parts[0]), value))
                    elif len(parts) == 2 and parts[1] == 'codex-status':
                        self.reply(handoff_status(state, unquote(parts[0])))
                    elif len(parts) != 2 or parts[1] not in {'message', 'draft', 'action'}:
                        self.reply({'message': '未找到'}, 404)
                    else:
                        self.reply(state.write(unquote(parts[0]), parts[1], value))
                else:
                    self.reply({'message': '未找到'}, 404)
            except SecurityError:
                self.reply({'message': '请求未通过本机校验'}, 403)
            except StorageError as exc:
                message = ('记录已写入本机，但持久化确认失败；请重新读取核对。'
                           if str(exc) == 'storage_durability_unconfirmed'
                           else '本机记录未能保存，请检查私有目录；原文件保留')
                self.reply({'message': message}, 503)
            except (ReadError, ValueError, OSError, socket.timeout):
                self.reply({'message': '未保存：请求无效或问题已变化，请刷新核对'}, 400)
    return Handler


def make_server(state, port=PORT, instance="", allow_shutdown=False):
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("invalid_port")
    return LocalServer((HOST, port), handler_for(state, secrets.token_hex(32), port, instance, allow_shutdown))
