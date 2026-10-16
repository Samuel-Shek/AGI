from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone
from io import StringIO
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from activity_mvp.ai_connector import AIConnectorHandler
from activity_mvp.canonical_paths import obsidian_vault_root
from activity_mvp.__main__ import (
    _connect_for_command,
    _connect_busy_status,
    _core_status_current_status,
    _core_quick_status_busy_status,
    _core_quick_status_unavailable_status,
    _core_status_busy_status,
    _diagnostics_busy_status,
    _run_busy_status_command,
    _safe_cli_entrypoint,
    _safe_threading_excepthook,
)
from activity_mvp.daemon import (
    _capture_batch_last_error,
    _ensure_ascii_runtime_link,
    _runtime_core_code_hash,
    _screen_recording_permission_status,
    get_quick_status,
)
from activity_mvp.pipeline import record_capture_coverage
from activity_mvp.storage import init_schema
from activity_mvp.swift_helpers import ensure_helper


class ReleaseHardeningTests(unittest.TestCase):
    def test_cli_entrypoint_sanitizes_uncaught_exception_details(self) -> None:
        stderr = StringIO()

        def fail() -> None:
            raise RuntimeError("/Users/example/private/client-plan.md")

        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            _safe_cli_entrypoint(fail)

        self.assertEqual(raised.exception.code, 1)
        payload = json.loads(stderr.getvalue())
        self.assertEqual(payload, {"status": "error", "error": "command_failed", "raw_access": False})
        self.assertNotIn("/Users/", stderr.getvalue())
        self.assertNotIn("client-plan", stderr.getvalue())

    def test_cli_entrypoint_reports_database_busy_without_traceback(self) -> None:
        stderr = StringIO()

        def fail() -> None:
            raise sqlite3.OperationalError("database is locked at /private/customer/activity.sqlite")

        with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
            _safe_cli_entrypoint(fail)

        self.assertEqual(raised.exception.code, 1)
        payload = json.loads(stderr.getvalue())
        self.assertEqual(payload, {"status": "error", "error": "database_busy", "raw_access": False})
        self.assertNotIn("/private/", stderr.getvalue())
        self.assertNotIn("activity.sqlite", stderr.getvalue())

    def test_cli_entrypoint_does_not_swallow_system_exit_or_keyboard_interrupt(self) -> None:
        for exception in (SystemExit(7), KeyboardInterrupt()):
            stderr = StringIO()
            previous_hook = threading.excepthook
            with redirect_stderr(stderr), self.assertRaises(type(exception)) as raised:
                _safe_cli_entrypoint(lambda exception=exception: (_ for _ in ()).throw(exception))
            self.assertIs(raised.exception, exception)
            self.assertIs(threading.excepthook, previous_hook)
            self.assertEqual(stderr.getvalue(), "")

    def test_cli_module_entrypoint_sanitizes_corrupt_database_failure(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        core_root = repo_root / "core"
        with tempfile.TemporaryDirectory() as tmp:
            database_path = Path(tmp) / "private-client" / "activity.sqlite"
            database_path.parent.mkdir()
            database_path.write_text("not a database: /Users/example/private.md", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-m", "activity_mvp", "--db", str(database_path), "status"],
                cwd=core_root,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(
            json.loads(result.stderr),
            {"status": "error", "error": "command_failed", "raw_access": False},
        )
        for forbidden in (str(database_path), str(core_root), "/Users/", "Traceback", "not a database"):
            self.assertNotIn(forbidden, result.stderr)

    def test_cli_entrypoint_installs_sanitized_thread_exception_hook(self) -> None:
        stderr = StringIO()
        previous_hook = threading.excepthook

        def run_thread() -> None:
            def fail() -> None:
                raise RuntimeError("/Users/example/private/thread-secret.md")

            thread = threading.Thread(target=fail, name="sensitive-thread-name")
            thread.start()
            thread.join()

        with redirect_stderr(stderr):
            _safe_cli_entrypoint(run_thread)

        self.assertIs(threading.excepthook, previous_hook)
        self.assertEqual(
            json.loads(stderr.getvalue()),
            {"status": "error", "error": "thread_failed", "raw_access": False},
        )
        for forbidden in ("/Users/", "thread-secret", "sensitive-thread-name", "Traceback"):
            self.assertNotIn(forbidden, stderr.getvalue())

    def test_safe_thread_exception_hook_ignores_system_exit(self) -> None:
        stderr = StringIO()
        args = threading.ExceptHookArgs((SystemExit, SystemExit(4), None, None))
        with redirect_stderr(stderr):
            _safe_threading_excepthook(args)
        self.assertEqual(stderr.getvalue(), "")

    def test_disabled_memory_surfaces_are_described_fail_closed_in_cli_help(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        core_root = repo_root / "core"
        cases = (
            (("memory-ledger", "--help"), ("frozen read-only", "no files are regenerated", "no changes are applied")),
            (("memory-approval", "--help"), ("controlled memory writer is disabled", "fail-closed refusal")),
            (("decision-queue", "--help"), ("local-only user decisions", "fail-closed refusal")),
        )
        for arguments, expected_phrases in cases:
            result = subprocess.run(
                [sys.executable, "-m", "activity_mvp", *arguments],
                cwd=core_root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            help_text = result.stdout.lower()
            for phrase in expected_phrases:
                self.assertIn(phrase, help_text)

    def test_launch_agent_uses_canonical_ascii_runtime_and_links_only_non_ascii_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            ascii_root = home / "Library" / "Application Support" / "Personal AI Monitor" / "core"
            ascii_root.mkdir(parents=True)

            with patch("activity_mvp.daemon.Path.home", return_value=home):
                self.assertEqual(_ensure_ascii_runtime_link(ascii_root), ascii_root.resolve())
                self.assertFalse((home / ".personal-ai-monitor").exists())

                non_ascii_root = home / "临时监控目录"
                non_ascii_root.mkdir()
                runtime_link = _ensure_ascii_runtime_link(non_ascii_root)

            self.assertEqual(runtime_link, home / ".personal-ai-monitor")
            self.assertTrue(runtime_link.is_symlink())
            self.assertEqual(runtime_link.resolve(), non_ascii_root.resolve())

    def test_build_script_detects_untracked_files_and_requires_explicit_dirty_override(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        wrapper = (repo_root / "scripts" / "build-macos-app.sh").read_text(encoding="utf-8")
        implementation = (
            repo_root / "scripts" / "build-macos-app-impl.sh"
        ).read_text(encoding="utf-8")

        self.assertIn("status --porcelain=v1 --untracked-files=normal", implementation)
        self.assertIn("--allow-dirty-development", wrapper)
        self.assertIn("PAM_ALLOW_DIRTY_BUILD", wrapper)
        self.assertIn("PAMSourceTreeHash", implementation)
        self.assertIn("PAMCoreCodeHash", implementation)
        self.assertIn("PAMGitUntrackedCount", implementation)
        self.assertIn("STAGING_APP_DIR", implementation)
        self.assertIn("codesign --verify --deep --strict", implementation)
        self.assertIn("plutil -lint", implementation)
        # The current release uses commit-bound owner-approved static artwork.
        self.assertIn('AGI-app-icon.sha256', implementation)
        self.assertIn('/usr/bin/shasum -a 256 -c', implementation)
        self.assertIn('/bin/cp "$ROOT_DIR/assets/agi/AGI-app-icon.icns" "$ICNS_FILE"', implementation)
        self.assertNotIn('swift "${SWIFT_SCRIPT_ARGS[@]}"', implementation)
        self.assertIn("hash-object --no-filters", wrapper)
        self.assertIn("/usr/bin/env -i", wrapper)
        self.assertIn('if [ "$PROVENANCE_ONLY" -eq 1 ]', wrapper)
        self.assertIn('current_user" == "runner"', implementation)
        self.assertIn('current_uid" == "501"', implementation)
        self.assertIn(
            'ROOT_DIR" == "/Users/runner/work/AGI/AGI"',
            implementation,
        )
        self.assertIn(
            'https://github.com/Samuel-Shek/AGI',
            implementation,
        )
        self.assertIn('component_mode & 8#022', implementation)
        self.assertNotIn("GITHUB_ACTIONS", implementation)
        self.assertIn("capture_xcode_identity", implementation)
        self.assertIn('com.apple.dt.Xcode" and anchor apple', implementation)
        self.assertIn("Xcode 15.4\\nBuild version 15F31d", implementation)
        self.assertIn('version_mismatches+=("bundle-identifier")', implementation)
        self.assertIn('version_mismatches+=("bundle-version")', implementation)
        self.assertIn('version_mismatches+=("bundle-build")', implementation)
        self.assertIn('version_mismatches+=("xcodebuild-version")', implementation)
        self.assertIn('xcode_build" == "15F31c"', implementation)
        self.assertIn("MacOSX14.5.sdk", implementation)
        self.assertIn("swiftlang-5.10.0.13 clang-1500.3.9.4", implementation)
        self.assertIn('if ! swift_version=', implementation)
        self.assertIn('swift_path" --version 2>&1', implementation)
        self.assertIn(
            "swift-driver version: 1.90.11.1 Apple Swift version 5.10",
            implementation,
        )
        self.assertIn("Observed Swift version identity", implementation)
        self.assertIn("XCODE_IDENTITY_AFTER", implementation)

    # (name, start anchor, end anchor).  Every struct or view body that renders
    # into the confirmation surface belongs here; a region absent from this list
    # is a region with no protection against the 100%-CPU layout loop.
    CONFIRMATION_RENDER_REGIONS = (
        (
            "tab",
            "    private var activityConfirmationsTab: some View {",
            "    private func confirmationGroupHeading",
        ),
        (
            "card",
            "struct ActivityConfirmationQuestionCard: View {",
            "\nstruct ",
        ),
    )

    def test_confirmation_surface_uses_no_lazy_layout_containers(self) -> None:
        """The whole confirmation render path must stay free of lazy layout.

        A lazy container inside this surface can trap SwiftUI in a self-feeding
        placement loop (placeSubviews -> AllItemsPhaseMutation ->
        updateItemPhases -> propagate_dirty -> re-measure) that pins the main
        thread at 100% CPU.  The 2026-07-27 guard only inspected the tab body,
        so the LazyVGrid inside the card struct stayed unchecked and the loop
        returned on 2026-08-07.  Both regions are covered now, and the check
        spans every lazy container kind rather than LazyVStack alone.

        The region list is data rather than inline slicing for a reason: this
        guard only protects code it is told about, so anything newly rendered
        into this surface has to be added here in the same change.  A missing
        anchor used to raise IndexError from a bare split()[1], which said
        nothing about which anchor moved or why it mattered; it now fails with
        that sentence instead.
        """
        repo_root = Path(__file__).resolve().parents[2]
        source = (
            repo_root
            / "apps"
            / "PersonalAIMonitorMenuBar"
            / "Sources"
            / "PersonalAIMonitorMenuBar"
            / "main.swift"
        ).read_text(encoding="utf-8")

        # assertIn would dump all 14k lines of main.swift into the failure, so
        # these check membership explicitly and report only the anchor.
        regions: list[tuple[str, str]] = []
        for region_name, start_anchor, end_anchor in self.CONFIRMATION_RENDER_REGIONS:
            if start_anchor not in source:
                self.fail(
                    f"the lazy-layout guard lost its {region_name!r} start anchor "
                    f"({start_anchor!r}). If that code was renamed or removed, "
                    f"update CONFIRMATION_RENDER_REGIONS in the same change -- "
                    f"otherwise whatever replaced it renders with no "
                    f"lazy-layout guard at all."
                )
            region = source.split(start_anchor, 1)[1]
            if end_anchor not in region:
                self.fail(
                    f"the lazy-layout guard lost its {region_name!r} end anchor "
                    f"({end_anchor!r}), so the region would run to the end of "
                    f"the file and misreport unrelated code. Update "
                    f"CONFIRMATION_RENDER_REGIONS."
                )
            regions.append((region_name, region.split(end_anchor, 1)[0]))

        lazy_containers = (
            "LazyVStack(",
            "LazyHStack(",
            "LazyVGrid(",
            "LazyHGrid(",
        )
        for region_name, region in regions:
            for container in lazy_containers:
                self.assertEqual(
                    region.count(container),
                    0,
                    f"{container} reappeared in the confirmation {region_name}",
                )
        tab = dict(regions)["tab"]
        self.assertIn("return ScrollView {\n            VStack", tab)

    def test_provenance_only_build_contract_handles_clean_tracked_and_untracked_trees(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        source_script = repo_root / "scripts" / "build-macos-app.sh"
        source_implementation = repo_root / "scripts" / "build-macos-app-impl.sh"
        with tempfile.TemporaryDirectory() as tmp:
            test_repo = Path(tmp).resolve() / "repo"
            isolated_build_root = Path(tmp).resolve() / "private-build"
            isolated_build_root.mkdir(mode=0o700)
            script = test_repo / "scripts" / "build-macos-app.sh"
            implementation = test_repo / "scripts" / "build-macos-app-impl.sh"
            module = test_repo / "core" / "activity_mvp" / "sample.py"
            tool = test_repo / "core" / "tools" / "sample.swift"
            script.parent.mkdir(parents=True)
            module.parent.mkdir(parents=True)
            tool.parent.mkdir(parents=True)
            shutil.copy2(source_script, script)
            shutil.copy2(source_implementation, implementation)
            script.chmod(0o755)
            implementation.chmod(0o644)
            module.write_text("VALUE = 1\n", encoding="utf-8")
            tool.write_text("let value = 1\n", encoding="utf-8")
            unicode_path = test_repo / "internal" / "中文 路径.md"
            unicode_path.parent.mkdir(parents=True)
            unicode_path.write_text("unicode tracked content\n", encoding="utf-8")
            spaced_path = test_repo / "docs" / "release identity fixture.txt"
            spaced_path.parent.mkdir(parents=True)
            spaced_path.write_text("space tracked content\n", encoding="utf-8")
            (test_repo / "core" / "pyproject.toml").write_text("[project]\nname = 'sample'\n", encoding="utf-8")
            # Git records ordinary files as 0644; set the fixture modes
            # explicitly rather than depending on the caller's umask.
            for fixture_file in (module, tool, unicode_path, spaced_path, test_repo / "core" / "pyproject.toml"):
                fixture_file.chmod(0o644)
            subprocess.run(["git", "init", "-q"], cwd=test_repo, check=True)
            subprocess.run(["git", "add", "."], cwd=test_repo, check=True)
            subprocess.run(
                ["git", "-c", "user.name=PAM Test", "-c", "user.email=pam@example.invalid", "commit", "-q", "--no-gpg-sign", "-m", "fixture"],
                cwd=test_repo,
                check=True,
            )

            clean = subprocess.run(
                [str(script), "--provenance-only", "--isolated-build-root", str(isolated_build_root)],
                cwd=test_repo,
                env=os.environ,
                capture_output=True,
                text=True,
            )
            self.assertEqual(clean.returncode, 0, clean.stderr)
            self.assertIn("git_dirty=false", clean.stdout)
            values = dict(line.split("=", 1) for line in clean.stdout.splitlines() if "=" in line)
            self.assertEqual(values["core_code_hash"], _runtime_core_code_hash(test_repo / "core"))
            listed = subprocess.run(
                ["git", "ls-tree", "-r", "-z", "--full-tree", "HEAD"],
                cwd=test_repo,
                check=True,
                stdout=subprocess.PIPE,
            ).stdout.split(b"\0")
            expected_source_hash = hashlib.sha256()
            for entry in (item for item in listed if item):
                metadata, raw_relative = entry.split(b"\t", 1)
                _mode, object_type, object_id = metadata.split()
                self.assertEqual(object_type, b"blob")
                blob = subprocess.run(
                    ["git", "cat-file", "blob", object_id.decode("ascii")],
                    cwd=test_repo,
                    check=True,
                    stdout=subprocess.PIPE,
                ).stdout
                expected_source_hash.update(raw_relative)
                expected_source_hash.update(b"\0")
                expected_source_hash.update(hashlib.sha256(blob).hexdigest().encode("ascii"))
                expected_source_hash.update(b"\n")
            self.assertEqual(values["source_tree_hash"], expected_source_hash.hexdigest())

            module.write_text("VALUE = 2\n", encoding="utf-8")
            tracked_dirty = subprocess.run(
                [str(script), "--provenance-only", "--isolated-build-root", str(isolated_build_root)],
                cwd=test_repo,
                env=os.environ,
                capture_output=True,
                text=True,
            )
            self.assertEqual(tracked_dirty.returncode, 2)
            self.assertIn("tracked=1", tracked_dirty.stderr)

            module.write_text("VALUE = 1\n", encoding="utf-8")
            (test_repo / "untracked.txt").write_text("untracked\n", encoding="utf-8")
            untracked_dirty = subprocess.run(
                [str(script), "--provenance-only", "--isolated-build-root", str(isolated_build_root)],
                cwd=test_repo,
                env=os.environ,
                capture_output=True,
                text=True,
            )
            self.assertEqual(untracked_dirty.returncode, 2)
            self.assertIn("untracked=1", untracked_dirty.stderr)

            overridden = subprocess.run(
                [
                    str(script),
                    "--allow-dirty-development",
                    "--provenance-only",
                    "--isolated-build-root", str(isolated_build_root),
                ],
                cwd=test_repo,
                env=os.environ,
                capture_output=True,
                text=True,
            )
            self.assertEqual(overridden.returncode, 0, overridden.stderr)
            self.assertIn("git_dirty=true", overridden.stdout)

    def test_connector_hides_internal_exception_messages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            handler = object.__new__(AIConnectorHandler)
            handler.path = "/status"
            handler.database_path = str(Path(tmp) / "activity.sqlite")
            handler.bind_host = "127.0.0.1"
            handler.bind_port = 8766
            handler._authorized = lambda: True
            responses: list[tuple[dict, HTTPStatus]] = []
            handler._send_json = lambda payload, status=HTTPStatus.OK: responses.append((payload, status))
            handler.log_error = lambda *args, **kwargs: None

            with patch(
                "activity_mvp.ai_connector.ai_connector_status",
                side_effect=RuntimeError("/private/example/activity.sqlite is locked"),
            ):
                handler.do_GET()

        self.assertEqual(len(responses), 1)
        payload, status = responses[0]
        self.assertEqual(status, HTTPStatus.INTERNAL_SERVER_ERROR)
        self.assertEqual(payload["error"], "request_failed")
        self.assertNotIn("/private/example", str(payload))
        self.assertNotIn("sqlite", str(payload).lower())

    def test_connector_hides_database_open_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            handler = object.__new__(AIConnectorHandler)
            handler.path = "/status"
            handler.database_path = str(Path(tmp) / "private" / "activity.sqlite")
            handler.bind_host = "127.0.0.1"
            handler.bind_port = 8766
            handler._authorized = lambda: True
            responses: list[tuple[dict, HTTPStatus]] = []
            handler._send_json = lambda payload, status=HTTPStatus.OK: responses.append((payload, status))
            handler.log_error = lambda *args, **kwargs: None

            with patch(
                "activity_mvp.ai_connector.ensure_ready",
                side_effect=RuntimeError("/private/example/activity.sqlite could not be opened"),
            ):
                handler.do_GET()

        payload, status = responses[0]
        self.assertEqual(status, HTTPStatus.INTERNAL_SERVER_ERROR)
        self.assertEqual(payload["error"], "request_failed")
        self.assertNotIn("/private/example", str(payload))
        self.assertNotIn("sqlite", str(payload).lower())

    def test_quick_status_is_content_free_and_read_only(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_schema(conn)
        before = conn.total_changes

        payload = get_quick_status(conn)

        self.assertEqual(conn.total_changes, before)
        self.assertEqual(payload["interface"], "quick_status")
        self.assertFalse(payload["raw_access"])
        serialized = str(payload).lower()
        for forbidden in ("ocr_text", "window_title", "cleaned_content", "url", "file_path"):
            self.assertNotIn(forbidden, serialized)
        self.assertNotIn("coverage_health", payload)

    def test_partial_status_fallbacks_never_authorize_capture(self) -> None:
        payloads = (
            _core_status_busy_status(),
            _core_quick_status_busy_status(),
            _core_quick_status_unavailable_status("database_unavailable"),
        )
        for payload in payloads:
            self.assertFalse(payload["capture_allowed_by_session"])
            self.assertFalse(payload["raw_access"])

    def test_core_busy_status_uses_unknown_nulls_and_timestamp(self) -> None:
        before = datetime.now(timezone.utc)
        payload = _core_status_busy_status()
        after = datetime.now(timezone.utc)

        self.assertEqual(
            set(payload),
            {
                "status",
                "reason_code",
                "kind",
                "interface",
                "observed_at",
                "last_successful_read_at",
                "capture_allowed_by_session",
                "actions_enabled",
                "raw_access",
            },
        )
        self.assertEqual(payload["status"], "unknown")
        self.assertEqual(payload["reason_code"], "database_busy")
        self.assertEqual(payload["kind"], "warning")
        self.assertEqual(payload["interface"], "full_status")
        self.assertFalse(payload["capture_allowed_by_session"])
        self.assertFalse(payload["actions_enabled"])
        self.assertFalse(payload["raw_access"])
        self.assertIsNone(payload["last_successful_read_at"])
        observed = datetime.fromisoformat(payload["observed_at"])
        self.assertIsNotNone(observed.tzinfo)
        self.assertEqual(observed.utcoffset(), timezone.utc.utcoffset(observed))
        self.assertLessEqual((before - observed).total_seconds(), 2.0)
        self.assertLessEqual((observed - after).total_seconds(), 2.0)

        unreadable_fields = {
            "service",
            "service_status",
            "launch_agent_loaded",
            "launch_agent_running",
            "launch_agent_pid",
            "launch_agent_last_exit_code",
            "today_activity_count",
            "today_privacy_count",
            "today_gap_count",
            "raw_residue_count",
            "dashboard_health",
            "capture_freshness",
            "coverage_health",
            "privacy_health",
            "quality_health",
            "structured_signal_health",
            "vlm_health",
            "document_changes",
            "current_activity_session",
            "today_activity_sessions",
            "capture_interval_seconds",
            "verified_context_refs",
            "trusted_context_refs",
            "bundle_hash",
            "context_refs",
        }
        for field in unreadable_fields:
            self.assertTrue(field not in payload or payload[field] is None, field)

        quick = _core_quick_status_busy_status()
        self.assertEqual(
            set(quick),
            {
                "status",
                "reason_code",
                "kind",
                "interface",
                "observed_at",
                "last_successful_read_at",
                "attention_codes",
                "capture_allowed_by_session",
                "actions_enabled",
                "raw_access",
            },
        )
        self.assertEqual(quick["status"], "unknown")
        self.assertEqual(quick["reason_code"], "database_busy")
        self.assertEqual(quick["kind"], "warning")
        self.assertEqual(quick["interface"], "quick_status")
        self.assertEqual(quick["attention_codes"], ["database_busy"])
        self.assertIsNone(quick["last_successful_read_at"])
        self.assertFalse(quick["capture_allowed_by_session"])
        self.assertFalse(quick["actions_enabled"])
        self.assertFalse(quick["raw_access"])
        datetime.fromisoformat(quick["observed_at"])

    def test_core_current_status_stamps_interface_and_utc_time(self) -> None:
        conn = sqlite3.connect(":memory:")
        try:
            before = datetime.now(timezone.utc)
            with patch(
                "activity_mvp.__main__.get_status",
                return_value={"status": "ready", "raw_access": False},
            ):
                payload = _core_status_current_status(conn, Path("/tmp/pam-fixture"))
            after = datetime.now(timezone.utc)
        finally:
            conn.close()

        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["interface"], "full_status")
        checked = datetime.fromisoformat(payload["checked_at"])
        self.assertIsNotNone(checked.tzinfo)
        self.assertEqual(checked.utcoffset(), timezone.utc.utcoffset(checked))
        self.assertLessEqual((before - checked).total_seconds(), 2.0)
        self.assertLessEqual((checked - after).total_seconds(), 2.0)

    def test_database_busy_status_returns_unknown_with_observed_at(self) -> None:
        def assert_common(payload: dict, before: datetime, after: datetime) -> None:
            self.assertEqual(payload["status"], "unknown")
            self.assertEqual(payload["reason_code"], "database_busy")
            self.assertFalse(payload["capture_allowed_by_session"])
            self.assertFalse(payload["actions_enabled"])
            self.assertFalse(payload["raw_access"])
            self.assertIsNone(payload["last_successful_read_at"])
            observed = datetime.fromisoformat(payload["observed_at"])
            self.assertIsNotNone(observed.tzinfo)
            self.assertEqual(observed.utcoffset(), timezone.utc.utcoffset(observed))
            self.assertLessEqual((before - observed).total_seconds(), 2.0)
            self.assertLessEqual((observed - after).total_seconds(), 2.0)

        conn = sqlite3.connect(":memory:")
        try:
            before_open_connection_fallback = datetime.now(timezone.utc)
            open_connection_payload = _run_busy_status_command(
                conn,
                lambda: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")),
                _core_status_busy_status,
            )
            after_open_connection_fallback = datetime.now(timezone.utc)
        finally:
            conn.close()
        assert_common(
            open_connection_payload,
            before_open_connection_fallback,
            after_open_connection_fallback,
        )

        before_connect_fallback = datetime.now(timezone.utc)
        connect_payload = _connect_busy_status(
            SimpleNamespace(command="status", quick=False)
        )
        after_connect_fallback = datetime.now(timezone.utc)
        assert_common(connect_payload, before_connect_fallback, after_connect_fallback)
        self.assertEqual(set(open_connection_payload), set(connect_payload))

    def test_quick_busy_status_exact_closed_contract(self) -> None:
        before = datetime.now(timezone.utc)
        payload = _core_quick_status_busy_status()
        after = datetime.now(timezone.utc)

        self.assertEqual(
            set(payload),
            {
                "status",
                "reason_code",
                "kind",
                "interface",
                "observed_at",
                "last_successful_read_at",
                "attention_codes",
                "capture_allowed_by_session",
                "actions_enabled",
                "raw_access",
            },
        )
        self.assertEqual(payload["status"], "unknown")
        self.assertEqual(payload["reason_code"], "database_busy")
        self.assertEqual(payload["kind"], "warning")
        self.assertEqual(payload["interface"], "quick_status")
        self.assertEqual(payload["attention_codes"], ["database_busy"])
        self.assertIsNone(payload["last_successful_read_at"])
        self.assertIs(payload["capture_allowed_by_session"], False)
        self.assertIs(payload["actions_enabled"], False)
        self.assertIs(payload["raw_access"], False)
        observed = datetime.fromisoformat(payload["observed_at"])
        self.assertEqual(observed.utcoffset(), timezone.utc.utcoffset(observed))
        self.assertLessEqual((before - observed).total_seconds(), 2.0)
        self.assertLessEqual((observed - after).total_seconds(), 2.0)

    def test_diagnostics_busy_status_never_reports_ok_or_service_true(self) -> None:
        before = datetime.now(timezone.utc)
        payload = _diagnostics_busy_status()
        after = datetime.now(timezone.utc)

        self.assertEqual(payload["status"], "unknown")
        self.assertEqual(payload["reason_code"], "database_busy")
        self.assertEqual(payload["kind"], "warning")
        self.assertEqual(payload["interface"], "diagnostics")
        self.assertIs(payload["capture_allowed_by_session"], False)
        self.assertIs(payload["actions_enabled"], False)
        for field in (
            "raw_access",
            "destructive_actions",
            "memory_write_actions",
            "rule_write_actions",
        ):
            self.assertIs(payload[field], False, field)
        self.assertEqual(payload["cloud_analysis"], "off")
        self.assertIsNone(payload["last_successful_read_at"])
        observed = datetime.fromisoformat(payload["observed_at"])
        self.assertIsNotNone(observed.tzinfo)
        self.assertEqual(observed.utcoffset(), timezone.utc.utcoffset(observed))
        self.assertLessEqual((before - observed).total_seconds(), 2.0)
        self.assertLessEqual((observed - after).total_seconds(), 2.0)

        boundary = payload["safety_boundary"]
        for field in (
            "raw_access",
            "destructive_actions",
            "memory_write_actions",
            "rule_write_actions",
            "reads_raw_sources",
        ):
            self.assertIs(boundary[field], False, f"safety_boundary.{field}")
        self.assertEqual(boundary["cloud_analysis"], "off")
        self.assertEqual(len(payload["probes"]), 1)
        self.assertEqual(payload["probes"][0]["name"], "database_busy")
        self.assertEqual(payload["probes"][0]["status"], "unknown")
        self.assertEqual(payload["probes"][0]["kind"], "warning")
        self.assertNotIn("runtime", payload)
        self.assertNotIn("actions", payload)
        self.assertNotIn("action_counts", payload)
        for field in (
            "service",
            "launch_agent_loaded",
            "launch_agent_running",
            "launch_agent_pid",
            "launch_agent_last_exit_code",
            "checked_probes",
            "skipped_probes",
            "check_scope",
        ):
            self.assertTrue(field not in payload or payload[field] is None, field)

        def walk(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    normalized = key.lower()
                    if normalized in {
                        "service",
                        "service_running",
                        "running",
                        "launch_agent_loaded",
                        "launch_agent_running",
                    }:
                        self.assertIsNot(child, True, key)
                    if normalized in {"status", "kind", "health", "health_status"}:
                        self.assertNotIn(
                            str(child).lower(),
                            {"ok", "healthy", "running", "ready", "capturing", "正常", "运行中", "已停止"},
                        )
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(payload)

    def test_status_connection_busy_timeout_is_bounded_by_interface(self) -> None:
        existing_db = Path(__file__)
        quick_status = SimpleNamespace(command="status", quick=True, db=str(existing_db))
        full_status = SimpleNamespace(command="status", quick=False, db=str(existing_db))
        diagnostics_quick = SimpleNamespace(
            command="diagnostics",
            quick=False,
            mode="quick",
            db=str(existing_db),
        )
        diagnostics_full = SimpleNamespace(
            command="diagnostics",
            quick=False,
            mode="full",
            db=str(existing_db),
        )

        readonly_connection = object()
        with patch(
            "activity_mvp.__main__.connect_readonly",
            return_value=readonly_connection,
        ) as connect_readonly_mock:
            self.assertIs(_connect_for_command(quick_status), readonly_connection)
        connect_readonly_mock.assert_called_once_with(
            str(existing_db),
            busy_timeout_seconds=0.25,
        )

        ready_connection = object()
        for args, expected_timeout in (
            (full_status, 1.0),
            (diagnostics_quick, 0.25),
            (diagnostics_full, 1.0),
        ):
            with self.subTest(command=args.command, mode=getattr(args, "mode", "")):
                with patch(
                    "activity_mvp.__main__.ensure_ready",
                    return_value=ready_connection,
                ) as ensure_ready_mock:
                    self.assertIs(_connect_for_command(args), ready_connection)
                ensure_ready_mock.assert_called_once_with(
                    str(existing_db),
                    busy_timeout_seconds=expected_timeout,
                )

    def test_cli_status_and_diagnostics_under_real_exclusive_lock(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        core_root = repo_root / "core"
        with tempfile.TemporaryDirectory() as tmp:
            database_path = Path(tmp) / "locked" / "activity.sqlite"
            database_path.parent.mkdir(parents=True)
            setup = sqlite3.connect(database_path)
            setup.row_factory = sqlite3.Row
            init_schema(setup)
            setup.commit()
            journal_mode = setup.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            self.assertEqual(str(journal_mode).lower(), "delete")
            setup.close()

            # Keep this a database-lock test, not a cold Swift compiler timing
            # test. The real helper and all eight real CLI subprocesses are
            # still used; only the helper build is moved outside their strict
            # wall-clock assertions.
            if sys.platform == "darwin":
                ensure_helper(database_path.parent, "session_state")

            locker = sqlite3.connect(database_path, timeout=1, isolation_level=None)
            try:
                locker.execute("BEGIN EXCLUSIVE")
                locker.execute(
                    "UPDATE meta SET value = value WHERE key = 'schema_version'"
                )

                probe = sqlite3.connect(database_path, timeout=0, isolation_level=None)
                try:
                    probe.execute("PRAGMA busy_timeout = 0")
                    with self.assertRaises(sqlite3.OperationalError) as raised:
                        probe.execute(
                            "SELECT value FROM meta WHERE key = 'schema_version'"
                        ).fetchone()
                    self.assertTrue(
                        "locked" in str(raised.exception).lower()
                        or "busy" in str(raised.exception).lower()
                    )
                finally:
                    probe.close()

                commands = (
                    ("quick", ["status", "--quick"]),
                    ("full", ["status"]),
                    (
                        "diagnostics_quick",
                        ["diagnostics", "status", "--mode", "quick"],
                    ),
                    (
                        "diagnostics_full",
                        ["diagnostics", "status", "--mode", "full"],
                    ),
                )
                for label, command in commands:
                    started = time.monotonic()
                    result = subprocess.run(
                        [sys.executable, "-m", "activity_mvp", "--db", str(database_path), *command],
                        cwd=core_root,
                        capture_output=True,
                        text=True,
                        timeout=8,
                    )
                    elapsed = time.monotonic() - started
                    self.assertLessEqual(
                        elapsed,
                        2.0 if label == "quick" else 7.0,
                        f"{label}: {elapsed:.3f}s",
                    )
                    self.assertEqual(result.returncode, 0, f"{label}: {result.stderr}")
                    self.assertEqual(result.stderr, "", label)
                    stdout = result.stdout.strip()
                    self.assertTrue(stdout.startswith("{") and stdout.endswith("}"), label)
                    payload = json.loads(stdout)
                    self.assertIsInstance(payload, dict)
                    self.assertEqual(payload["status"], "unknown", label)
                    self.assertEqual(payload["reason_code"], "database_busy", label)
                    self.assertIs(payload["capture_allowed_by_session"], False, label)
                    self.assertIs(payload["actions_enabled"], False, label)
                    self.assertIs(payload["raw_access"], False, label)
                    observed = datetime.fromisoformat(payload["observed_at"])
                    self.assertEqual(
                        observed.utcoffset(),
                        timezone.utc.utcoffset(observed),
                        label,
                    )
                    for forbidden in (
                        "Traceback",
                        str(database_path),
                        str(core_root),
                        "database is locked",
                        "SELECT value FROM meta",
                        "UPDATE meta SET",
                    ):
                        self.assertNotIn(forbidden, result.stdout, label)
                        self.assertNotIn(forbidden, result.stderr, label)

                    if label == "quick":
                        self.assertEqual(
                            set(payload),
                            {
                                "status",
                                "reason_code",
                                "kind",
                                "interface",
                                "observed_at",
                                "last_successful_read_at",
                                "attention_codes",
                                "capture_allowed_by_session",
                                "actions_enabled",
                                "raw_access",
                            },
                        )
                    elif label == "full":
                        for field in (
                            "launch_agent_loaded",
                            "launch_agent_running",
                            "launch_agent_pid",
                            "launch_agent_last_exit_code",
                            "today_activity_count",
                            "today_privacy_count",
                            "today_gap_count",
                            "raw_residue_count",
                            "dashboard_health",
                            "document_changes",
                            "current_activity_session",
                            "verified_context_refs",
                            "trusted_context_refs",
                            "bundle_ref",
                            "bundle_hash",
                        ):
                            self.assertTrue(
                                field not in payload or payload[field] is None,
                                f"{label}: {field}",
                            )
                    else:
                        boundary = payload["safety_boundary"]
                        for field in (
                            "raw_access",
                            "destructive_actions",
                            "memory_write_actions",
                            "rule_write_actions",
                            "reads_raw_sources",
                        ):
                            self.assertIs(boundary[field], False, f"{label}: {field}")
                        self.assertEqual(boundary["cloud_analysis"], "off")
                        self.assertEqual(len(payload["probes"]), 1)
                        self.assertEqual(payload["probes"][0]["name"], "database_busy")
                        self.assertNotIn("actions", payload)
                        self.assertNotIn("action_counts", payload)
            finally:
                locker.rollback()
                locker.close()

            for label, command in commands:
                result = subprocess.run(
                    [sys.executable, "-m", "activity_mvp", "--db", str(database_path), *command],
                    cwd=core_root,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0, f"recovery {label}: {result.stderr}")
                self.assertEqual(result.stderr, "", f"recovery {label}")
                payload = json.loads(result.stdout)
                self.assertIs(payload.get("raw_access"), False, f"recovery {label}")
                self.assertNotIn("database_busy", payload.get("attention_codes") or [], label)
                self.assertNotEqual(payload.get("reason_code"), "database_busy", label)
                if label == "full":
                    self.assertIn(
                        payload.get("status"),
                        {
                            "capturing",
                            "ready",
                            "running",
                            "paused",
                            "locked",
                            "session_state_unknown",
                            "stopped",
                            "error",
                        },
                    )
                    self.assertEqual(payload.get("interface"), "full_status")
                    recovered_at = datetime.fromisoformat(payload["checked_at"])
                    self.assertEqual(
                        recovered_at.utcoffset(),
                        timezone.utc.utcoffset(recovered_at),
                    )
                elif label == "quick":
                    self.assertIn(
                        payload.get("status"),
                        {
                            "capturing",
                            "ready",
                            "running",
                            "paused",
                            "locked",
                            "session_state_unknown",
                            "stopped",
                            "error",
                        },
                    )
                    self.assertEqual(payload.get("interface"), "quick_status")
                else:
                    self.assertIn(payload.get("status"), {"ok", "partial", "attention_required"})
                    self.assertEqual(payload.get("interface"), "diagnostics")
                    self.assertEqual(
                        payload.get("mode"),
                        "quick" if label == "diagnostics_quick" else "full",
                    )

    def test_production_swift_success_unknown_recovery_clears_and_restores_state(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        app_root = repo_root / "apps" / "PersonalAIMonitorMenuBar" / "Sources"
        production = (app_root / "PersonalAIMonitorMenuBar" / "main.swift").read_text(encoding="utf-8")
        model = (app_root / "PAMInteractionModel" / "InteractionModel.swift").read_text(encoding="utf-8")
        checks = (app_root / "PAMInteractionModelChecks" / "main.swift").read_text(encoding="utf-8")
        core_entrypoint = (repo_root / "core" / "activity_mvp" / "__main__.py").read_text(encoding="utf-8")

        self.assertIn("ProductionStatusClientState", production)
        self.assertIn("productionStatusState.apply(dictionary: object, interface: .fullStatus)", production)
        self.assertIn("productionStatusState.apply(dictionary: object, interface: .quickStatus)", production)
        self.assertIn("private var productionDiagnosticsState = ProductionDiagnosticsClientState()", production)
        self.assertIn("productionDiagnosticsState.apply(", production)
        self.assertIn(
            "diagnosticsStatus = DiagnosticsStatus(productionState: productionDiagnosticsState)",
            production,
        )
        diagnostics_method = production[
            production.index("func refreshDiagnosticsStatus("):
            production.index("func refreshMemoryReview(")
        ]
        self.assertIn("@Published var diagnosticsStatus = DiagnosticsStatus()", production)
        self.assertIn("let raw = await runCore([\"diagnostics\", \"status\", \"--mode\", mode])", diagnostics_method)
        self.assertIn("productionDiagnosticsState.apply(", diagnostics_method)
        self.assertIn(
            "diagnosticsStatus = DiagnosticsStatus(productionState: productionDiagnosticsState)",
            diagnostics_method,
        )
        self.assertNotIn("DiagnosticsStatus(jsonText: raw)", diagnostics_method)
        self.assertIn("StatusSnapshot.failClosedUnknown", production)
        self.assertIn("captureActionsAvailable", production)
        self.assertIn("lastFullStatusRefreshAt = .distantPast", production)
        self.assertIn("if transition.mustClearCurrentState", production)
        self.assertIn("statusSnapshot = parsed", production)
        self.assertIn("if parsed.hasCurrentTrustedState && !parsed.requiresFullRefresh", production)
        self.assertIn("Self.statusObservationDate(from: successfulReadAt)", production)
        self.assertNotIn("currentStatusNeedsLocalObservation", production)
        self.assertNotIn('object["observed_at"] = Self.statusObservationFormatter.string(from: Date())', production)
        self.assertIn("let mayRetainFullEvidence", production)
        self.assertIn("snapshot.hasCurrentTrustedState = snapshot.serviceStateKnown && snapshot.countsStateKnown", production)
        self.assertIn("if runner.statusSnapshot.displayStateUnknown", production)
        self.assertIn("private func productionMutationAllowed(", production)
        self.assertIn('guard statusSnapshot.recordingControlAvailable, productionMutationAllowed("暂停记录", requiresCaptureControls: false)', production)
        self.assertNotIn('"修改 VLM 补处理设置"', production)
        self.assertNotIn('"处理 VLM 积压"', production)
        # Product exit now verifies both components; context trust remains a gate.
        exit_method = production[production.index("    func stopRecordingAndQuit() async {"):production.index("    func captureNow() async {")]
        self.assertIn("await EvolutionModel.shared.stopAll(runner: self)", exit_method)
        self.assertIn("!statusSnapshot.displayStateUnknown", exit_method)
        self.assertIn("!statusSnapshot.launchAgentLoaded && !statusSnapshot.launchAgentRunning", exit_method)
        native = (Path(__file__).resolve().parents[2] / "apps/PersonalAIMonitorMenuBar/Sources/PersonalAIMonitorMenuBar/NativeStatusMenu.swift").read_text()
        self.assertIn("quit.isHidden = false", native)
        self.assertIn("await runner.stopRecordingAndQuit()", native)
        self.assertNotIn("var snapshot = previous", production)
        self.assertNotIn("parsed.preservingMenuBarCountdown(from: statusSnapshot)", production)
        self.assertNotIn("private func unavailableSnapshot(preserving previous: StatusSnapshot)", production)
        self.assertIn("public struct ProductionStatusClientState", model)
        self.assertIn("public struct ProductionDiagnosticsClientState", model)
        self.assertIn("ProductionStatusContract.isParseableUTCTimestamp(eventTime)", model)
        self.assertIn("payloadLastSuccessfulRead == nil || payloadLastSuccessfulRead == eventTime", model)
        self.assertIn("trustedSnapshot = nil", model)
        self.assertIn("def _core_status_current_status(", core_entrypoint)
        self.assertIn('payload["interface"] = "full_status"', core_entrypoint)
        self.assertIn('payload["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")', core_entrypoint)
        self.assertIn("lambda: _core_status_current_status(conn, Path(args.db).parent)", core_entrypoint)
        for marker in (
            "status_unknown_payload_accepts_null_service_fields",
            "success_busy_recovery_clears_then_restores_trusted_state",
            "full_success_unknown_success_uses_only_c",
            "quick_success_unknown_success_uses_only_c",
            "diagnostics_success_unknown_success_uses_only_c",
            "unknown_does_not_advance_last_successful_status_time",
            "unknown_requires_next_full_refresh",
            "unknown_exposes_refresh_only",
            "unknown_label_contains_observed_time",
            "paused_locked_session_unknown_do_not_regress",
        ):
            self.assertIn(f'runNamedTest("{marker}")', checks)
        self.assertIn("full_success_A", checks)
        self.assertIn("unknown_busy_B", checks)
        self.assertIn("success_C", checks)

    def test_quick_status_does_not_export_untrusted_display_identifiers(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_schema(conn)
        record_capture_coverage(
            conn,
            "/private/user/Secret Window Title",
            "2026-07-13T10:00:00+00:00",
            "keyframe",
            frames_sampled=1,
            frames_dropped=0,
            gap=0,
        )
        conn.commit()

        serialized = json.dumps(get_quick_status(conn), ensure_ascii=False)
        self.assertNotIn("/private/user", serialized)
        self.assertNotIn("Secret Window", serialized)

    def test_quick_cli_reports_missing_and_corrupt_databases_without_paths(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        core_root = repo_root / "core"
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "private" / "missing.sqlite"
            missing_result = self._run_quick_cli(core_root, missing)
            self.assertEqual(missing_result.returncode, 0, missing_result.stderr)
            missing_payload = json.loads(missing_result.stdout)
            self.assertEqual(missing_payload["status"], "unavailable")
            self.assertEqual(missing_payload["attention_codes"], ["database_missing"])

            corrupt = Path(tmp) / "secret-client.sqlite"
            corrupt.write_bytes(b"not a sqlite database\n/private/customer")
            corrupt_result = self._run_quick_cli(core_root, corrupt)
            self.assertEqual(corrupt_result.returncode, 0, corrupt_result.stderr)
            corrupt_payload = json.loads(corrupt_result.stdout)
            self.assertEqual(corrupt_payload["status"], "unavailable")
            self.assertFalse(corrupt_payload["raw_access"])
            self.assertNotIn(str(corrupt), corrupt_result.stdout)

    @staticmethod
    def _run_quick_cli(core_root: Path, database_path: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "activity_mvp", "--db", str(database_path), "status", "--quick"],
            cwd=core_root,
            capture_output=True,
            text=True,
        )

    def test_canonical_obsidian_root_uses_current_icloud_vault(self) -> None:
        root = obsidian_vault_root(home="/home/example")
        self.assertEqual(
            str(root),
            "/home/example/Library/Mobile Documents/iCloud~md~obsidian/Documents/默认仓库",
        )

    def test_backend_capture_evidence_is_authoritative_for_permission_state(self) -> None:
        denied = _screen_recording_permission_status(
            last_error="screen_recording_permission_denied",
            last_attempt_at="2026-07-13T10:01:00+00:00",
            last_success_at="",
            launch_agent_running=True,
            capture_freshness={"is_stale": True},
        )
        granted = _screen_recording_permission_status(
            last_error="",
            last_attempt_at="2026-07-13T10:00:00+00:00",
            last_success_at="2026-07-13T10:00:00+00:00",
            launch_agent_running=True,
            capture_freshness={"is_stale": False, "last_capture_age_seconds": 10, "stale_threshold_seconds": 90},
        )
        unknown = _screen_recording_permission_status(
            last_error="",
            last_attempt_at="2026-07-13T10:01:00+00:00",
            last_success_at="2026-07-01T10:00:00+00:00",
            launch_agent_running=True,
            capture_freshness={"is_stale": True, "last_capture_age_seconds": 1000, "stale_threshold_seconds": 90},
        )

        self.assertEqual(denied["state"], "denied")
        self.assertEqual(granted["state"], "granted_observed")
        self.assertEqual(unknown["state"], "unknown")
        self.assertTrue(denied["backend_authoritative"])
        generic_permission_error = _screen_recording_permission_status(
            last_error="file permission denied: /private/path",
            last_attempt_at="2026-07-13T10:01:00+00:00",
            last_success_at="",
            launch_agent_running=True,
            capture_freshness={"is_stale": True},
        )
        self.assertEqual(generic_permission_error["state"], "unknown")
        degraded = _screen_recording_permission_status(
            last_error="capture_batch_failed:1_display(s)",
            last_attempt_at="2026-07-13T10:01:00+00:00",
            last_success_at="2026-07-13T10:00:00+00:00",
            launch_agent_running=True,
            capture_freshness={"is_stale": False, "last_capture_age_seconds": 10, "stale_threshold_seconds": 90},
        )
        self.assertEqual(degraded["state"], "unknown")
        stopped_historical = _screen_recording_permission_status(
            last_error="",
            last_attempt_at="2020-01-01T00:00:00+00:00",
            last_success_at="2020-01-01T00:00:00+00:00",
            launch_agent_running=False,
            capture_freshness={"is_stale": False, "last_capture_age_seconds": 10, "stale_threshold_seconds": 90},
        )
        self.assertEqual(stopped_historical["state"], "unknown")
        mixed_timezone_history = _screen_recording_permission_status(
            last_error="",
            last_attempt_at="2026-07-13T10:00:00",
            last_success_at="2026-07-13T10:00:00+00:00",
            launch_agent_running=True,
            capture_freshness={"is_stale": False, "last_capture_age_seconds": 10, "stale_threshold_seconds": 90},
        )
        self.assertEqual(mixed_timezone_history["state"], "unknown")
        self.assertEqual(
            _capture_batch_last_error([{"error": "CGDisplayStream permission denied for /private/path"}]),
            "screen_recording_permission_denied",
        )
        self.assertEqual(
            _capture_batch_last_error([{"error": "output directory permission denied for /private/path"}]),
            "capture_batch_failed:1_display(s)",
        )

    def test_native_app_uses_quick_polling_and_embedded_provenance(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        source = (
            repo_root
            / "apps"
            / "PersonalAIMonitorMenuBar"
            / "Sources"
            / "PersonalAIMonitorMenuBar"
            / "main.swift"
        ).read_text(encoding="utf-8")

        self.assertIn('runCore(["status", "--quick"])', source)
        self.assertIn("lastFullStatusRefreshAt", source)
        self.assertIn('info["PAMSourceTreeHash"]', source)
        self.assertIn('info["PAMCoreCodeHash"]', source)
        self.assertIn("pythonPathFromLaunchAgentArguments", source)
        self.assertIn("arguments.first", source)
        self.assertIn('SettingsSection(title: "历史台账（只读）")', source)
        self.assertIn("integrityUnverified", source)
        self.assertIn("captureInteractionBlocked", source)
        self.assertIn("captureAllowedBySession", source)
        self.assertIn('object["capture_allowed_by_session"] as? Bool ?? false', source)
        self.assertIn('status == "session_state_unknown"', source)
        self.assertIn("backendAuthoritative = false", source)
        self.assertIn('backendAuthoritative && state == "denied"', source)
        self.assertIn("后台采集权限待确认", source)
        self.assertIn("lastFullStatusRefreshAt = .distantPast", source)
        self.assertIn("refreshForInteraction", source)
        self.assertIn("NSWindow.didMiniaturizeNotification", source)
        self.assertNotIn("CGRequestScreenCaptureAccess", source)
        self.assertIn('object["raw_access"] as? Bool == false', source)

    def test_runtime_core_hash_is_deterministic_and_detects_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "activity_mvp").mkdir()
            (root / "tools").mkdir()
            (root / "browser_extension").mkdir()
            module = root / "activity_mvp" / "sample.py"
            module.write_text("VALUE = 1\n", encoding="utf-8")
            (root / "tools" / "sample.swift").write_text("let value = 1\n", encoding="utf-8")
            extension = root / "browser_extension" / "manifest.json"
            extension.write_text('{"version":"0.4.0"}\n', encoding="utf-8")
            (root / "pyproject.toml").write_text("[project]\nname = 'sample'\n", encoding="utf-8")

            first = _runtime_core_code_hash(root)
            second = _runtime_core_code_hash(root)
            extension.write_text('{"version":"0.4.1"}\n', encoding="utf-8")
            changed_resource = _runtime_core_code_hash(root)
            (root / "activity_mvp" / "fixture.json").write_text('{"changed":true}\n', encoding="utf-8")
            changed_json = _runtime_core_code_hash(root)

        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        self.assertNotEqual(first, changed_resource)
        self.assertNotEqual(changed_resource, changed_json)

    def test_runtime_core_hash_fails_closed_when_file_enumeration_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "activity_mvp").mkdir()
            (root / "tools").mkdir()
            with patch.object(Path, "rglob", side_effect=OSError("/private/path is unreadable")):
                result = _runtime_core_code_hash(root)
        self.assertEqual(result, "unknown")


if __name__ == "__main__":
    unittest.main()
