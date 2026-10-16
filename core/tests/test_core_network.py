from core_fixture import *


class CoreNetworkTests(CoreFixture):
    def setUp(self):
        super().setUp()
        if self._testMethodName == 'test_ai_connector_managed_start_stop_and_pid_safety':
            # Real connector process and original assertions; the child gets
            # only unchanged code and cannot import either editable leaf.
            import activity_mvp.ai_connector as connector
            fixture = self.root.resolve() / 'connector-core'
            shutil.copytree(Path(connector.__file__).resolve().parent, fixture / 'activity_mvp',
                            ignore=shutil.ignore_patterns('feedback', '__pycache__', '*.pyc'))
            fixture.chmod(0o700)
            self.assertFalse((fixture / 'activity_mvp/feedback').exists())
            self.assertFalse((fixture / 'activity_mvp/feedback/sources.py').exists())
            self.assertFalse((fixture / 'activity_mvp/feedback/journal.py').exists())
            location = patch.object(connector, '__file__', str(fixture / 'activity_mvp/ai_connector.py'))
            location.start(); self.addCleanup(location.stop)
            environment = patch.dict(os.environ, {'PYTHONPATH':'', 'PYTHONNOUSERSITE':'1', 'PYTHONSAFEPATH':'1'})
            environment.start(); self.addCleanup(environment.stop)

    def test_capture_now_api_respects_pause(self) -> None:
        pause(self.conn, minutes=15)
        httpd, thread, base_url = start_dashboard_server(self.root / "activity.sqlite")
        try:
            with patch("activity_mvp.daemon.session_state", return_value={"locked": False}), patch(
                "activity_mvp.daemon.capture_sck_keyframe_batch"
            ) as capture:
                status, payload = http_post(base_url, "/api/capture-now", {})
        finally:
            stop_dashboard_server(httpd, thread)

        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "paused")
        capture.assert_not_called()


    def test_ai_connector_is_loopback_read_only(self) -> None:
        status = ai_connector_status(self.conn, self.root)
        self.assertEqual(status["interface"], "ai-connector")
        self.assertEqual(status["mode"], "managed_loopback_read_only")
        self.assertEqual(status["base_url"], f"http://127.0.0.1:{DEFAULT_AI_CONNECTOR_PORT}")
        self.assertFalse(status["server_running"])
        self.assertTrue(status["managed_by_app"])
        self.assertFalse(status["raw_access"])
        self.assertFalse(status["destructive_actions"])
        self.assertFalse(status["memory_write_actions"])
        with self.assertRaises(ValueError):
            ai_connector_status(self.conn, self.root, host="0.0.0.0")

        server = create_ai_connector_server(self.root / "activity.sqlite", port=0)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{port}"
            with request.urlopen(f"{base_url}/status", timeout=5) as response:
                connector = json.loads(response.read().decode("utf-8"))
            self.assertEqual(connector["interface"], "ai-connector")
            self.assertTrue(connector["server_running"])
            self.assertFalse(connector["raw_access"])

            with request.urlopen(f"{base_url}/package?range=today&format=json&max_items=5", timeout=5) as response:
                package = json.loads(response.read().decode("utf-8"))
            self.assertEqual(package["interface"], "ai-handoff")
            self.assertEqual(package["package_mode"], "connector_lightweight")
            self.assertFalse(package["raw_access"])
            self.assertFalse(package["memory_write_actions"])

            with request.urlopen(f"{base_url}/diagnostics?mode=quick", timeout=5) as response:
                diagnostics = json.loads(response.read().decode("utf-8"))
            self.assertEqual(diagnostics["interface"], "diagnostics")
            self.assertEqual(diagnostics["mode"], "quick")
            self.assertFalse(diagnostics["raw_access"])
            self.assertFalse(diagnostics["memory_write_actions"])

            post = request.Request(f"{base_url}/package", data=b"{}", method="POST")
            with self.assertRaises(error.HTTPError) as ctx:
                request.urlopen(post, timeout=5)
            self.assertEqual(ctx.exception.code, 405)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


    def test_ai_connector_managed_start_stop_and_pid_safety(self) -> None:
        port = unused_tcp_port()
        started = start_ai_connector(self.root / "activity.sqlite", self.root, port=port)
        try:
            self.assertEqual(started["status"], "ok")
            self.assertIn(started["action_status"], {"started", "starting"})
            self.assertTrue(
                wait_until(
                    lambda: port_accepts_connection("127.0.0.1", port),
                    timeout=CONNECTOR_READINESS_TIMEOUT_SECONDS,
                ),
                f"connector never accepted a connection\n{self._connector_failure_detail(port)}",
            )
            with request.urlopen(f"http://127.0.0.1:{port}/status", timeout=5) as response:
                connector = json.loads(response.read().decode("utf-8"))
            self.assertEqual(connector["mode"], "managed_loopback_read_only")
            self.assertTrue(connector["server_running"])
            self.assertFalse(connector["raw_access"])
        finally:
            stopped = stop_ai_connector(self.root, port=port)
        self.assertEqual(stopped["status"], "ok")
        self.assertIn(stopped["action_status"], {"stopped", "force_stopped", "stale_pid_removed"})
        self.assertTrue(
            wait_until(
                lambda: not port_accepts_connection("127.0.0.1", port),
                timeout=CONNECTOR_READINESS_TIMEOUT_SECONDS,
            )
        )

        pidfile = self.root / "ai_connector.pid"
        pidfile.write_text(json.dumps({"pid": os.getpid(), "host": "127.0.0.1", "port": port}), encoding="utf-8")
        refused = stop_ai_connector(self.root, port=port)
        self.assertEqual(refused["status"], "blocked")
        self.assertEqual(refused["action_status"], "refused_unmanaged_pid")
        self.assertTrue(pidfile.exists())
        pidfile.unlink()


    def test_ai_connector_start_reports_port_conflict(self) -> None:
        port = unused_tcp_port()
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen(1)
        try:
            result = start_ai_connector(self.root / "activity.sqlite", self.root, port=port)
        finally:
            listener.close()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["action_status"], "port_conflict")
        self.assertTrue(result["port_conflict"])


    def test_dashboard_delete_requires_confirmation_code(self) -> None:
        ingest_events(self.conn, EVENTS)
        package = generate_package(
            self.conn,
            "2026-05-22T09:00:00+08:00",
            "2026-05-22T09:12:00+08:00",
            self.root / "packages",
        )["package"]

        httpd, thread, base_url = start_dashboard_server(self.root / "activity.sqlite")
        try:
            status, payload = http_post(
                base_url,
                "/api/delete",
                {
                    "from": "2026-05-22T08:59:00+08:00",
                    "to": "2026-05-22T09:02:00+08:00",
                    "reason": "user_deleted_from_dashboard",
                },
            )
        finally:
            stop_dashboard_server(httpd, thread)

        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "destructive_confirmation_required")
        self.assertEqual(len(search(self.conn, "Personal AI Monitor")), 1)
        self.assertEqual(get_package(self.conn, package["package_id"])["source_status"], "active")


    def test_dashboard_delete_confirmation_requires_origin(self) -> None:
        subject = {
            "from": "2026-05-22T08:59:00+08:00",
            "to": "2026-05-22T09:02:00+08:00",
            "reason": "user_deleted_from_dashboard",
        }
        httpd, thread, base_url = start_dashboard_server(self.root / "activity.sqlite")
        try:
            status, _ = http_post(base_url, "/api/delete-confirmation", subject, include_origin=False)
        finally:
            stop_dashboard_server(httpd, thread)

        self.assertEqual(status, 403)


    def test_dashboard_control_operations_require_origin_or_referer(self) -> None:
        httpd, thread, base_url = start_dashboard_server(self.root / "activity.sqlite")
        try:
            status, _ = http_post(base_url, "/api/package-today", {}, include_origin=False)
        finally:
            stop_dashboard_server(httpd, thread)

        self.assertEqual(status, 403)


    def test_dashboard_delete_confirmation_is_bound_single_use_and_revokes_package(self) -> None:
        ingest_events(self.conn, EVENTS)
        package = generate_package(
            self.conn,
            "2026-05-22T09:00:00+08:00",
            "2026-05-22T09:12:00+08:00",
            self.root / "packages",
        )["package"]
        subject = {
            "from": "2026-05-22T08:59:00+08:00",
            "to": "2026-05-22T09:02:00+08:00",
            "reason": "user_deleted_from_dashboard",
        }

        httpd, thread, base_url = start_dashboard_server(self.root / "activity.sqlite")
        try:
            status, confirmation = http_post(base_url, "/api/delete-confirmation", subject)
            self.assertEqual(status, 200)
            wrong_status, _ = http_post(
                base_url,
                "/api/delete",
                {
                    **subject,
                    "to": "2026-05-22T09:03:00+08:00",
                    "confirmation_code": confirmation["confirmation_code"],
                },
            )
            self.assertEqual(wrong_status, 403)

            ok_status, payload = http_post(
                base_url,
                "/api/delete",
                {**subject, "confirmation_code": confirmation["confirmation_code"]},
            )
            self.assertEqual(ok_status, 200)
            self.assertEqual(payload["activities_deleted"], 1)

            reuse_status, _ = http_post(
                base_url,
                "/api/delete",
                {**subject, "confirmation_code": confirmation["confirmation_code"]},
            )
        finally:
            stop_dashboard_server(httpd, thread)

        self.assertEqual(reuse_status, 403)
        self.assertEqual(len(search(self.conn, "Personal AI Monitor")), 0)
        self.assertEqual(get_package(self.conn, package["package_id"])["source_status"], "user_deleted")


    def test_dashboard_delete_confirmation_expires(self) -> None:
        ingest_events(self.conn, EVENTS)
        subject = {
            "from": "2026-05-22T08:59:00+08:00",
            "to": "2026-05-22T09:02:00+08:00",
            "reason": "user_deleted_from_dashboard",
        }

        httpd, thread, base_url = start_dashboard_server(self.root / "activity.sqlite", ttl_seconds=0)
        try:
            status, confirmation = http_post(base_url, "/api/delete-confirmation", subject)
            self.assertEqual(status, 200)
            delete_status, _ = http_post(
                base_url,
                "/api/delete",
                {**subject, "confirmation_code": confirmation["confirmation_code"]},
            )
        finally:
            stop_dashboard_server(httpd, thread)

        self.assertEqual(delete_status, 403)
        self.assertEqual(len(search(self.conn, "Personal AI Monitor")), 1)



if __name__ == "__main__":
    unittest.main()
