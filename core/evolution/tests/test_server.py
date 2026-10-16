"""Real loopback requests with a fake controller; never touches live assets."""

import contextlib
import http.client
import io
import json
import threading
import unittest

from ai_ceo.server import MAX_BODY, serve


class ControllerStub:
    def __init__(self):
        self.calls = []
        self.fail = None

    def snapshot(self):
        if self.fail:
            raise RuntimeError(self.fail)
        return {"mode": "paused", "tasks": [], "assets": [], "stage": 0}

    def control(self, action, asset_id=None):
        self.calls.append((action, asset_id))
        if action == "resume":
            raise PermissionError("sensitive internal grant data")
        return {"ok": True}

    def observe(self):
        self.calls.append(("observe", None))
        return {"status": "unknown"}

    def replay(self):
        self.calls.append(("replay", None))
        return {"passed": True, "source": "synthetic"}


class LocalServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.controller = ControllerStub()
        cls.server = serve(cls.controller)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.host = f"127.0.0.1:{cls.server.server_port}"
        cls.origin = f"http://{cls.host}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.controller.calls.clear()
        self.controller.fail = None

    def request(self, method="GET", path="/api/state", body=None, headers=None, authenticated=True, origin=True):
        request_headers = {"Host": self.host}
        if authenticated:
            request_headers["X-AI-CEO-Token"] = self.server.access_token
        if origin:
            request_headers["Origin"] = self.origin
        if body is not None:
            if isinstance(body, dict):
                body = json.dumps(body).encode()
            if isinstance(body, str):
                body = body.encode()
            request_headers["Content-Type"] = "application/json"
            request_headers["Content-Length"] = str(len(body))
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=4)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            data = response.read()
            return response.status, dict(response.getheaders()), data
        finally:
            connection.close()

    def test_only_literal_ipv4_loopback_bind_allowed(self):
        for host in ["0.0.0.0", "localhost", "::", "::1", "192.168.1.2"]:
            with self.subTest(host=host), self.assertRaises(ValueError):
                serve(self.controller, host)

    def test_auth_required_for_state_and_authstate(self):
        for path in ["/api/state", "/api/authstate"]:
            with self.subTest(path=path):
                status, _, body = self.request(path=path, authenticated=False)
                self.assertEqual(status, 401)
                self.assertNotIn(b'"tasks"', body)
        status, _, body = self.request(path="/api/authstate")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"authenticated": True})

    def test_wrong_token_has_no_access(self):
        status, _, _ = self.request(headers={"X-AI-CEO-Token": "wrong"})
        self.assertEqual(status, 401)

    def test_dns_rebinding_hosts_are_rejected(self):
        for host in ["evil.example", f"evil.example:{self.server.server_port}", "127.0.0.1.evil.example", f"localhost:{self.server.server_port}"]:
            with self.subTest(host=host):
                status, _, _ = self.request(path="/", headers={"Host": host}, authenticated=False, origin=False)
                self.assertEqual(status, 403)

    def test_origin_and_fetch_site_cannot_bypass_auth(self):
        for headers in [{"Origin": "https://evil.example"}, {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}]:
            with self.subTest(headers=headers):
                status, _, _ = self.request("POST", "/api/control", {"action": "stop"}, headers=headers)
                self.assertEqual(status, 403)
        self.assertEqual(self.controller.calls, [])

    def test_missing_origin_rejects_mutations_with_valid_token(self):
        status, _, _ = self.request("POST", "/api/replay", {}, origin=False)
        self.assertEqual(status, 403)
        self.assertEqual(self.controller.calls, [])

    def test_no_unauthenticated_mutation(self):
        status, _, _ = self.request("POST", "/api/control", {"action": "pause"}, authenticated=False)
        self.assertEqual(status, 401)
        self.assertEqual(self.controller.calls, [])

    def test_security_headers_and_static_no_token_disclosure(self):
        for path in ["/", "/app.js", "/style.css"]:
            with self.subTest(path=path):
                status, headers, body = self.request(path=path, authenticated=False, origin=False)
                self.assertEqual(status, 200)
                self.assertIn("no-store", headers["Cache-Control"])
                self.assertEqual(headers["Referrer-Policy"], "no-referrer")
                self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertNotIn("Access-Control-Allow-Origin", headers)
                self.assertNotIn(self.server.access_token.encode(), body)

    def test_exact_paths_reject_traversal_queries_and_tokens_in_query(self):
        for path in ["/../server.py", "/%2e%2e/server.py", "//api/state", "/api/state/", "/api/state?token=secret", "/?token=secret", "/static/../../server.py"]:
            with self.subTest(path=path):
                status, _, _ = self.request(path=path)
                self.assertEqual(status, 404)

    def test_only_narrow_controls_dispatch(self):
        for action in ["pause", "stop"]:
            status, _, _ = self.request("POST", "/api/control", {"action": action})
            self.assertEqual(status, 200)
        for action in ["revoke", "takeover"]:
            status, _, _ = self.request("POST", "/api/control", {"action": action, "asset_id": "sync-center"})
            self.assertEqual(status, 200)
        self.assertEqual(self.controller.calls, [("pause", None), ("stop", None), ("revoke", "sync-center"), ("takeover", "sync-center")])

    def test_widening_commands_and_untyped_arguments_never_dispatch(self):
        payloads = [
            {"action": "grant"}, {"action": "publish"}, {"action": "model"},
            {"action": "pause", "command": "touch anything"}, {"action": ["stop"]},
            {"action": "revoke"}, {"action": "revoke", "asset_id": "../prod"},
            {"action": "takeover", "asset_id": "$(whoami)"},
            {"action": "pause", "asset_id": "sync-center"},
            {"action": "revoke", "asset_id": 123},
        ]
        for body in payloads:
            with self.subTest(body=body):
                status, _, _ = self.request("POST", "/api/control", body)
                self.assertEqual(status, 400)
        self.assertEqual(self.controller.calls, [])

    def test_no_production_or_grant_routes(self):
        for path in ["/api/grant", "/api/authorize", "/api/execute", "/api/publish", "/api/model"]:
            with self.subTest(path=path):
                self.assertEqual(self.request("POST", path, {})[0], 404)

    def test_duplicate_keys_non_objects_nonfinite_values_rejected(self):
        for body in ['{"action":"stop","action":"pause"}', '[]', 'null', '{"x": NaN}', '{"x": Infinity}', '{', b'\xff']:
            with self.subTest(body=body):
                self.assertEqual(self.request("POST", "/api/control", body)[0], 400)
        self.assertEqual(self.controller.calls, [])

    def test_bounded_body_and_content_type(self):
        self.assertEqual(self.request("POST", "/api/control", b"x" * (MAX_BODY + 1))[0], 413)
        self.assertEqual(self.request("POST", "/api/control", {}, headers={"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/api/control", {}, headers={"Transfer-Encoding": "chunked"})[0], 400)
        self.assertEqual(self.controller.calls, [])

    def test_replay_observe_no_arbitrary_inputs(self):
        for path in ["/api/replay", "/api/observe"]:
            with self.subTest(path=path):
                self.assertEqual(self.request("POST", path, {"root": "/private"})[0], 400)
                self.assertEqual(self.request("POST", path, {})[0], 200)
        self.assertEqual(self.controller.calls, [("replay", None), ("observe", None)])

    def test_controller_gate_is_not_overridden(self):
        status, _, body = self.request("POST", "/api/control", {"action": "resume"})
        self.assertEqual(status, 403)
        self.assertNotIn(b"sensitive internal grant", body)

    def test_error_redacts_exception_and_never_prints_token(self):
        self.controller.fail = "private/path credential-secret"
        capture = io.StringIO()
        with contextlib.redirect_stderr(capture), contextlib.redirect_stdout(capture):
            status, _, body = self.request()
            self.request(path=f"/?token={self.server.access_token}")
        self.assertEqual(status, 503)
        self.assertNotIn(b"credential-secret", body)
        self.assertNotIn(self.server.access_token, capture.getvalue())

    def test_preflight_does_not_enable_cors(self):
        status, headers, _ = self.request("OPTIONS", "/api/control")
        self.assertEqual(status, 405)
        self.assertNotIn("Access-Control-Allow-Origin", headers)


if __name__ == "__main__":
    unittest.main()
