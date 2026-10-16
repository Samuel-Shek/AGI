from __future__ import annotations

import json
import os
import plistlib
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import tempfile
import time
import tomllib
import unittest
from contextlib import nullcontext
import uuid
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib import error, request


SYNTHETIC_SECRET_TOKEN = "sk-" + "abcdefghijklmnopqrstuvwxyz123456"
SYNTHETIC_PRIVATE_KEY_HEADER = "-----BEGIN " + "PRIVATE KEY-----"


from activity_mvp import __version__ as CORE_VERSION
from activity_mvp.adult_policy import domain_hit as adult_domain_hit, policy_metadata as adult_policy_metadata, text_hit as adult_text_hit
from activity_mvp.ai_handoff import (
    AI_HANDOFF_VERSION,
    build_ai_handoff_package,
    render_ai_handoff_markdown,
    validate_ai_handoff_payload,
    write_ai_handoff_export,
)
from activity_mvp.ai_connector import AI_CONNECTOR_VERSION, DEFAULT_AI_CONNECTOR_PORT, ai_connector_status, create_ai_connector_server, start_ai_connector, stop_ai_connector
from activity_mvp.activity_sessions import (
    get_current_activity_session,
    list_activity_sessions,
    observe_system_activity_session,
    render_activity_sessions_markdown,
    show_activity_session,
)
from activity_mvp.activity_confirmations import (
    MANUAL_CONFIRMATION_ACTION,
    list_activity_confirmations,
    manual_add_confirmed_activity,
)
from activity_mvp.browser_signals import (
    CHROMIUM_HOST_DIRS,
    _browser_identity_from_process_names,
    extension_id_from_public_key,
    install_native_host_manifests,
    list_recent_browser_signals,
    read_latest_browser_signal,
    sanitize_browser_signal,
    write_browser_signal,
)
from activity_mvp.daemon import BACKGROUND_MAINTENANCE_MIN_IDLE_SECONDS, DOCUMENT_CHANGE_STARTUP_MIN_IDLE_SECONDS, DOCUMENT_CHANGE_STARTUP_RECONCILE_ENV_KEY, SCREEN_CAPTURE_DISABLED_POLL_SECONDS, _background_maintenance_eligibility, _daily_maintenance_worker, _document_change_worker_startup_eligibility, _document_change_startup_reconcile_enabled, _mark_daily_maintenance_pending, _maybe_start_daily_maintenance_worker, _maybe_start_document_change_worker_when_eligible, _run_daemon_startup_maintenance, _safe_daemon_failure_code, _safe_hygiene_cleanup_for_daemon, _safe_cleanup_retention_for_daemon, cleanup_raw_vault, configure_hygiene_cleanup, configure_privacy_profile, cleanup_legacy_server_pid, cleanup_retention, cleanup_retention_preview, configure_screen_capture, count_raw_residue, get_status, install_launch_agent, pause, resume, run_daemon, run_once
from activity_mvp.data_hygiene import (
    HYGIENE_CLEANUP_CONFIRMATION,
    _release_backups_inventory,
    backup_hygiene_report,
    data_hygiene_report,
    hygiene_cleanup_report,
    safe_delete_ha_cache_residue,
)
from activity_mvp.daily_review import daily_review
from activity_mvp.diagnostics import diagnostics_status, render_diagnostics_markdown
from activity_mvp.decision_queue import decision_queue_list, preview_decision_queue_write, record_decision_queue_action
from activity_mvp.document_signals import build_document_signal
from activity_mvp.ha_automation import (
    _memory_write_report_message,
    configure_ha_automation,
    ha_automation_status,
    run_ha_automation_once,
    validate_ha_automation,
)
from activity_mvp.ha_source import (
    generate_package,
    get_package,
    get_package_handoff_summary,
    get_package_memory_candidates,
    list_packages,
    revoke_package,
    validate_package,
)
from activity_mvp.lifecycle import delete_range
from activity_mvp.local_endpoints import validate_local_http_endpoint
from activity_mvp.macos_probe import capture_sck_keyframe_batch
from activity_mvp.ocr_service import OcrOutcome
from activity_mvp.memory_audit import memory_audit, render_memory_audit_markdown
from activity_mvp.memory_approval import (
    apply_memory_approval,
    memory_approval_candidates,
    preview_memory_approval,
)
from activity_mvp.memory_ledger import refresh_memory_ledger, sync_memory_ledger, memory_ledger_status
from activity_mvp.pipeline import ingest_events, parse_time, record_capture_coverage, search, timeline, utc_now
from activity_mvp.privacy import classify_event
from activity_mvp.privacy_policy import privacy_policy_preview, privacy_policy_status, privacy_profile_preview
from activity_mvp.quality import add_quality_feedback, list_quality_feedback, quality_report, render_quality_markdown
from activity_mvp.quality_advice import (
    configure_quality_advice,
    quality_advice_status,
    run_quality_advice_once,
    validate_quality_advice,
)
from activity_mvp.reclassify import reclassify_existing
from activity_mvp.report_context import REPORT_CONTEXT_VERSION
from activity_mvp.retention import (
    HERMES_HANDOFF_MAX_RUNTIME_SECONDS,
    RETENTION_HANDOFF_DRAIN_SECONDS,
    RETENTION_HANDOFF_RELEASE_GRACE_SECONDS,
    RETENTION_MAINTENANCE_HARD_DEADLINE_SECONDS,
    retention_cleanup_cutoff,
    retention_handoff_admission,
    retention_until_iso_for_range_start,
)
from activity_mvp.safe_url import redact_url_for_storage, safe_hostname_from_url
from activity_mvp.server import ActivityHandler, DestructiveConfirmationStore, _host_allowed, _origin_allowed, render_dashboard, validate_loopback_bind_host
from activity_mvp.storage import ensure_ready, harden_sqlite_permissions
from activity_mvp.timeline_export import (
    cleaned_timeline_export,
    render_cleaned_timeline_markdown,
    render_learning_timeline_markdown,
    write_timeline_markdown,
)


EVENTS = [
    {
        "captured_at": "2026-05-22T09:00:00+08:00",
        "display_id": "main",
        "app_name": "Obsidian",
        "bundle_id": "md.obsidian",
        "window_title": "internal/mvp-docs",
        "ocr_text": "Personal AI Monitor is implementing SQLite FTS and HA Source Package.",
        "context_confidence": 0.9,
    },
    {
        "captured_at": "2026-05-22T09:05:00+08:00",
        "display_id": "main",
        "app_name": "1Password",
        "bundle_id": "com.1password.1password",
        "window_title": "Secure Note",
        "ocr_text": "credential placeholder REDACTED_TEST_CREDENTIAL_VALUE_DO_NOT_STORE",
        "secure_input": True,
        "context_confidence": 0.9,
    },
    {
        "captured_at": "2026-05-22T09:10:00+08:00",
        "display_id": "external-1",
        "app_name": "Unknown",
        "bundle_id": "",
        "window_title": "",
        "ocr_text": "",
        "context_confidence": 0.2,
    },
]






def private_vlm_test_image(root: Path, name: str) -> Path:
    vault = root / "raw_vault"
    vault.mkdir(mode=0o700, exist_ok=True)
    vault.chmod(0o700)
    path = vault / name
    path.touch(mode=0o600, exist_ok=False)
    path.chmod(0o600)
    return path


class ImmediateOcrTask:
    def __init__(self, outcome: OcrOutcome) -> None:
        self.outcome = outcome

    def result(self) -> OcrOutcome:
        return self.outcome

    def cancel(self) -> None:
        return None

    def done(self) -> bool:
        return True

    def wait_terminal(self) -> None:
        return None


def actionable_layout(region_kind: str) -> dict[str, object]:
    """Explicit spatial evidence used by tests that exercise confirmed actions.

    App-name priors are intentionally context-only in v0.3.9.  A test that
    expects a confirmed action must therefore provide independent layout
    evidence instead of silently relying on the foreground app name.
    """
    return {
        "region_kind": region_kind,
        "actionability": "actionable",
        "source": "ocr_ax",
        "confidence": 0.93,
        "reason_codes": ["ocr_region_main_content"],
    }


# Starting the managed connector spawns a process and binds a TCP port, so its
# readiness depends on how loaded the host is.  A budget tuned to a developer
# machine turns a healthy-but-slow CI runner into a failure that says nothing
# about the code.  `wait_until` returns as soon as the predicate holds, so a
# generous ceiling costs nothing on a passing run and only delays a real
# failure.
CONNECTOR_READINESS_TIMEOUT_SECONDS = 30.0


def wait_until(predicate, timeout: float = 1.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


class CoreFixture(unittest.TestCase):
    def setUp(self) -> None:
        package_clock = patch(
            "activity_mvp.ha_source._package_access_now",
            return_value=datetime(2026, 5, 22, tzinfo=timezone.utc),
        )
        package_clock.start()
        self.addCleanup(package_clock.stop)
        ha_rollout = patch(
            "activity_mvp.ha_automation._ha_auto_package_rollout_allowed",
            return_value=True,
        )
        ha_rollout.start()
        self.addCleanup(ha_rollout.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.conn = ensure_ready(self.root / "activity.sqlite")
        # Existing capture fixtures exercise an explicitly granted session.
        from activity_mvp.capture_lease import configure_capture_lease
        configure_capture_lease(self.conn, enabled=True)
        self.conn.commit()


    def seed_runtime_progress(self, pid="12345") -> None:
        # These tests model a running daemon; the record must belong to the
        # same mocked launchd PID rather than borrowing a historical ready flag.
        stamp = datetime.now(timezone.utc).isoformat()
        for worker in ("daemon", "document_worker"):
            self.conn.execute("INSERT OR REPLACE INTO daemon_state VALUES(?,?,?)",
                (worker + "_heartbeat", json.dumps({"pid": int(pid), "at": stamp, "phase": "running"}), stamp))
        self.conn.commit()


    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()


    def _seed_historical_confirmed_business_contexts(
        self,
        start: str,
        end: str,
        *,
        workstream: str,
        outcome: str = "completed",
    ) -> int:
        """Seed pre-policy-v7 answers for downstream compatibility tests.

        New information-insufficient rows are intentionally not answerable.
        These tests instead exercise reads of business-context rows that were
        answered by an older installed version.
        """
        list_activity_confirmations(
            self.conn,
            range_name="custom",
            start=start,
            end=end,
            include_resolved=False,
        )
        pending = self.conn.execute(
            """
            SELECT item_key
            FROM activity_confirmation_items
            WHERE candidate_origin='business_episode'
              AND question_kind='business_context'
              AND answer IS NULL
              AND state IN ('pending','deferred')
              AND datetime(observed_start)<datetime(?)
              AND datetime(observed_end)>datetime(?)
            ORDER BY item_key
            """,
            (end, start),
        ).fetchall()
        self.assertGreater(
            len(pending),
            0,
            "fixture must materialize a legacy business-context row",
        )
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        item_keys = [str(row["item_key"]) for row in pending]
        placeholders = ",".join("?" for _ in item_keys)
        cursor = self.conn.execute(
            f"""
            UPDATE activity_confirmation_items
            SET state='confirmed',
                answer='occurred',
                answer_reason='other',
                user_note='',
                user_workstream=?,
                user_outcome=?,
                notification_eligible=0,
                generalize_scope='exact',
                answered_at=?,
                updated_at=?,
                clarification_note=''
            WHERE item_key IN ({placeholders})
              AND answer IS NULL
              AND state IN ('pending','deferred')
            """,
            (workstream, outcome, now, now, *item_keys),
        )
        self.assertEqual(cursor.rowcount, len(item_keys))
        self.conn.commit()
        return len(item_keys)


    def _manual_confirm(
        self,
        label: str,
        started_at: str,
        *,
        workstream: str = "PAM engineering",
        outcome: str = "progressing",
        ended_at: str | None = None,
    ) -> dict:
        """Create the explicit user-confirmation fixture required by v2 trust boundaries."""
        result = manual_add_confirmed_activity(
            self.conn,
            confirmation_action=MANUAL_CONFIRMATION_ACTION,
            safe_label=label,
            workstream=workstream,
            outcome=outcome,
            started_at=started_at,
            ended_at=ended_at or started_at,
        )
        self.assertEqual(result["status"], "recorded")
        return result


    def _insert_ha_automation_job(
        self,
        *,
        hour_key: str,
        status: str,
        updated_at: str,
        completed_at: str = "",
        hermes_result_status: str = "",
        attempt_count: int = 1,
        last_error: str = "",
        dead_letter_reason: str = "",
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO ha_automation_jobs(
              id, hour_key, started_at, ended_at, package_id, activity_count,
              safe_timeline_event_count, trigger_mode, hermes_profile, status,
              attempt_count, last_error, generated_at, triggered_at, completed_at,
              created_at, updated_at, dead_letter_reason, hermes_result_status
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                hour_key,
                hour_key,
                hour_key,
                f"ha_pkg_{uuid.uuid4().hex[:12]}",
                0,
                0,
                "direct_hermes",
                "activity-analysis",
                status,
                attempt_count,
                last_error,
                updated_at,
                updated_at,
                completed_at,
                updated_at,
                updated_at,
                dead_letter_reason,
                hermes_result_status,
            ),
        )
        self.conn.commit()


    def _install_ready_ha_dependencies(self) -> Path:
        """Lay out a self-contained Hermes/mem0 tree so preflight never reads the host.

        `validate_ha_automation` probes the real home directory for the Hermes
        binary, its profile, and the mem0 CLI.  A developer machine has all of
        them, so preflight reports ready; a clean CI runner has none, preflight
        reports not-ready, and `ha_automation_status` degrades its label to
        "依赖异常".  Any assertion about a healthy label then fails for a reason
        the test does not cover.  Patching `Path.home` at this fixture keeps the
        real preflight logic under test while making it depend only on files the
        test itself created.
        """
        fake_home = self.root / "fake-ha-home"
        profile = fake_home / ".hermes" / "profiles" / "activity-analysis"
        (profile / "plugins" / "mem0_local").mkdir(parents=True, exist_ok=True)
        feishu_profile = fake_home / ".hermes" / "profiles" / "feishubot"
        feishu_profile.mkdir(parents=True, exist_ok=True)
        binaries = fake_home / "bin"
        binaries.mkdir(parents=True, exist_ok=True)
        for name in ("hermes", "mem0"):
            executable = binaries / name
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
        (profile / ".env").write_text("MEM0_USER_ID=local-owner\n", encoding="utf-8")
        (profile / "config.yaml").write_text(
            "memory:\n  provider: mem0_local\nplugins:\n  - mem0_local\n",
            encoding="utf-8",
        )
        (feishu_profile / "channel_directory.json").write_text(
            json.dumps(
                {"platforms": {"feishu": [{"id": "oc_memory", "name": "记忆系统", "type": "group"}]}},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return fake_home


    def _start_document_worker_with_pending_daily(
        self,
        maintenance: dict,
        *,
        retry_not_before: float = 0.0,
    ):
        from activity_mvp import daemon as module

        db_path = self.root / "activity.sqlite"
        _mark_daily_maintenance_pending(self.conn, "2026-07-27")
        with patch.object(module, "_DAILY_MAINTENANCE_THREAD", None), patch.object(
            module,
            "_DAILY_MAINTENANCE_RETRY_NOT_BEFORE_MONOTONIC",
            retry_not_before,
        ), patch.object(
            module,
            "_background_maintenance_eligibility",
            return_value=maintenance,
        ) as maintenance_eligibility, patch(
            "activity_mvp.daemon._document_change_worker_startup_eligibility",
            return_value={"allowed": True, "reason": "", "raw_access": False},
        ) as eligibility, patch(
            "activity_mvp.daemon._maybe_start_document_change_worker",
            return_value=True,
        ) as worker:
            result = _maybe_start_document_change_worker_when_eligible(
                db_path,
                self.root,
            )
        return result, maintenance_eligibility, eligibility, worker


    def _connector_failure_detail(self, port: int) -> str:
        """Report why the connector never came up, not just that it didn't.

        `start_ai_connector` redirects the child's stderr into a log file under
        the test root.  When the child dies during startup the assertion here
        can only say "False is not true" while the actual traceback sits in a
        file the runner discards at the end of the job, which makes a CI-only
        failure impossible to diagnose from the logs.
        """
        details: list[str] = [f"port={port}"]
        for name in ("ai_connector.err.log", "ai_connector.out.log"):
            log = self.root / "logs" / name
            if not log.exists():
                details.append(f"{name}: <missing>")
                continue
            body = log.read_text(encoding="utf-8", errors="replace").strip()
            details.append(f"{name}: {body[-1500:] if body else '<empty>'}")
        pidfile = self.root / "ai_connector.pid"
        details.append(
            f"pidfile: {pidfile.read_text(encoding='utf-8', errors='replace').strip()}"
            if pidfile.exists()
            else "pidfile: <missing>"
        )
        return "\n".join(details)




def json_dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def unused_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def port_accepts_connection(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.2):
            return True
    except OSError:
        return False


def start_dashboard_server(db_path: Path, ttl_seconds: int = 120):
    class Handler(ActivityHandler):
        database_path = str(db_path)
        bind_host = "127.0.0.1"
        bind_port = 0
        destructive_confirmation_store = DestructiveConfirmationStore(ttl_seconds=ttl_seconds)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Handler.bind_port = httpd.server_port
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, thread, f"http://127.0.0.1:{httpd.server_port}"


def stop_dashboard_server(httpd: ThreadingHTTPServer, thread: threading.Thread) -> None:
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


def http_post(base_url: str, path: str, payload: dict, include_origin: bool = True) -> tuple[int, dict]:
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if include_origin:
        headers["Origin"] = base_url
    req = request.Request(
        base_url + path,
        data=body,
        method="POST",
        headers=headers,
    )
    try:
        with request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        data = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(data) if data else {}
        except json.JSONDecodeError:
            return exc.code, {"raw": data}



# Share fixtures, including private helper names, without importing test cases.
__all__ = [name for name in globals() if not name.startswith("__")]
