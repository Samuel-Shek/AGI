from __future__ import annotations

import errno
import contextlib
import json
import inspect
import linecache
import os
import queue
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from xml.etree import ElementTree

from activity_mvp.ai_connector import build_ai_connector_package
from activity_mvp import document_changes as document_changes_module
from activity_mvp.ai_handoff import build_ai_handoff_package, validate_ai_handoff_text
from activity_mvp.daily_review import daily_review
from activity_mvp.daemon import pause, resume
from activity_mvp.document_changes import (
    FileEventHelperProcess,
    HMAC_SECRET_FILENAME,
    HMAC_VERIFIER_STATE_KEY,
    _document_change_session_allows_content,
    _document_name_join_key,
    _hmac_key,
    _parse_office_xml,
    build_document_baseline,
    configure_document_changes,
    defer_document_change_startup_reconcile,
    document_changes_status,
    document_context_projections,
    ensure_default_document_roots,
    join_document_change_worker,
    list_document_changes,
    process_document_event,
    resolve_document_change_range,
    start_document_change_worker,
    document_stream_reset_required_reason,
    stop_document_change_worker,
)
from activity_mvp.context_console import (
    CONTEXT_FEATURE_CONFIRMATION,
    CONTROLLED_EVIDENCE_VERSION,
    build_context_bundle,
    create_memory_candidate,
    configure_context_console,
    ingest_controlled_evidence,
    prepare_delivery,
    record_document_change_context_locked,
    register_context_label,
    register_context_principal,
    register_document_context_mapping,
    revoke_document_context_file,
    revoke_document_context_root,
    sign_contract_payload,
)
from activity_mvp.lifecycle import delete_range, preview_delete_range
from activity_mvp.report_context import report_context
from activity_mvp.storage import SCHEMA_VERSION, connect_readonly, ensure_ready, init_schema


def _registry_roots(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return list(payload["roots"]) if isinstance(payload, dict) else list(payload)


def _write_registry_roots(
    path: Path,
    roots: list[dict],
    *,
    legacy: bool = False,
    revision: int | None = None,
) -> None:
    if legacy:
        payload: object = roots
    else:
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        current_revision = current.get("authorization_revision", 0) if isinstance(current, dict) else 0
        payload = {
            "format_version": 2,
            "authorization_revision": current_revision if revision is None else revision,
            "roots": roots,
        }
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)


class _FailingCommitConnection(sqlite3.Connection):
    fail_next_commit = False

    def commit(self) -> None:
        if self.fail_next_commit:
            self.fail_next_commit = False
            raise OSError("simulated commit failure")
        super().commit()


class _Stdout:
    def __iter__(self):
        return iter([
            '{"type":"ready","event_id":"1","root_count":1}\n',
            '{"path":"/tmp/a.md","event_id":"7","flags":["item_modified"]}\n',
        ])


class _Process:
    def __init__(self, command, **kwargs):
        self.command = command
        self.stdout = _Stdout()
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True


class _ManyLines:
    def __iter__(self):
        yield json.dumps({"type": "ready", "event_id": "1", "root_count": 1}) + "\n"
        for index in range(10_050):
            yield json.dumps({
                "path": f"/private/tmp/outside-{index}.md",
                "event_id": str(index + 2),
                "flags": ["item_modified"],
            }) + "\n"


class _ExitedProcess:
    def __init__(self, command, **kwargs):
        self.command = command
        self.stdout = _ManyLines()
        self.terminated = False

    def poll(self):
        return 1

    def terminate(self):
        self.terminated = True


class _ControlledStdout:
    def __init__(self):
        self.lines: queue.Queue[str | None] = queue.Queue()

    def __iter__(self):
        while True:
            line = self.lines.get()
            if line is None:
                return
            yield line

    def emit(self, payload: dict) -> None:
        self.lines.put(json.dumps(payload) + "\n")

    def close(self) -> None:
        self.lines.put(None)


class _ControlledProcess:
    # The worker refuses to start when the helper reports a different number of
    # roots than the database holds, so a multi-root test needs a helper that
    # says so.  Subclass and override rather than editing this default, which
    # every single-root test depends on.
    root_count = 1

    def __init__(self, command, **kwargs):
        self.command = command
        self.stdout = _ControlledStdout()
        self.returncode = None
        self.terminated = False
        self.last_event_id = max(1, int(command[command.index("--since") + 1])) if "--since" in command else 1
        self.stdout.emit({"type": "ready", "event_id": str(self.last_event_id), "root_count": self.root_count})

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0
        self.stdout.close()

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        return self.returncode

    def emit(self, payload: dict) -> None:
        event_id = str(payload.get("event_id") or "")
        if event_id.isdigit():
            self.last_event_id = max(self.last_event_id, int(event_id))
        self.stdout.emit(payload)

    def request_barrier(self, token: str) -> None:
        self.stdout.emit({
            "type": "barrier",
            "token": token,
            "event_id": str(self.last_event_id),
            "observed_at": datetime.now(timezone.utc).isoformat(),
        })


class _NoBarrierProcess(_ControlledProcess):
    def request_barrier(self, token: str) -> None:
        raise RuntimeError("barrier unavailable")


class _NoReadyProcess(_ControlledProcess):
    def __init__(self, command, **kwargs):
        super().__init__(command, **kwargs)
        # Discard the synthetic ready line while retaining a controllable stdout
        # that stop() can close to release the reader thread.
        self.stdout = _ControlledStdout()


class DocumentChangeTests(unittest.TestCase):
    def setUp(self):
        # Overflow fault injection must hit its bounded deadline deterministically.
        backpressure = mock.patch.object(document_changes_module, "DOCUMENT_CHANGE_BACKPRESSURE_SECONDS", 0.1)
        backpressure.start()
        self.addCleanup(backpressure.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.watch = self.root / "watch"
        self.watch.mkdir()
        self.db = self.root / "data/activity.sqlite"
        self.conn = ensure_ready(self.db)
        configure_document_changes(
            self.conn,
            add_root=self.watch,
            root_label="Watch",
            runtime_root=self.root / "data",
        )

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def _event(self, path: Path, event_id: int, *, flags: list[str] | None = None, observed_at: str | None = None) -> dict:
        return {
            "path": str(path),
            "event_id": str(event_id),
            "observed_at": observed_at or datetime.now(timezone.utc).isoformat(),
            "flags": flags or ["item_modified"],
        }

    def _all_changes(self, *, for_export: bool = False, limit: int = 100) -> dict:
        return list_document_changes(
            self.conn,
            "2000-01-01T00:00:00+00:00",
            "2100-01-01T00:00:00+00:00",
            limit=limit,
            for_export=for_export,
        )

    def _configure_explicit_context_mapping(self) -> dict:
        root_id = str(self.conn.execute("SELECT root_id FROM file_watch_roots").fetchone()[0])
        labels = (
            ("project", "project_d0000001", "Document Project", "", ""),
            ("workstream", "workstream_d0000002", "Document Workstream", "project_d0000001", ""),
            ("object", "object_d0000003", "Document Object", "project_d0000001", ""),
            ("version", "version_d0000004", "Mapped Version", "project_d0000001", ""),
            ("subject", "subject_d0000005", "Owner", "project_d0000001", "owner"),
        )
        for kind, ref, label, project_ref, subject_scope in labels:
            result = register_context_label(
                self.conn,
                kind=kind,
                ref=ref,
                label=label,
                project_ref=project_ref,
                subject_scope=subject_scope,
            )
            self.assertIn(result["status"], {"registered", "replayed"})
        enabled = configure_context_console(
            self.conn,
            enabled=True,
            confirmation=CONTEXT_FEATURE_CONFIRMATION,
        )
        self.assertEqual(enabled["status"], "enabled")
        mapping = register_document_context_mapping(
            self.conn,
            mapping_ref="mapping_d0000006",
            mapping_version=1,
            root_id=root_id,
            project_ref="project_d0000001",
            workstream_ref="workstream_d0000002",
            object_ref="object_d0000003",
            version_ref="version_d0000004",
            subject_ref="subject_d0000005",
        )
        self.assertIn(mapping["status"], {"registered", "replayed"})
        return {"root_id": root_id, **mapping}

    def _ingest_trusted_confirmation(
        self,
        *,
        source_ref: str = "source_d0000007",
        idempotency_key: str = "idem_d0000010",
    ) -> dict:
        secret = b"document-test-confirmation"
        principal_id = "principal_d0000008"
        key_id = "key_d0000009"
        registered = register_context_principal(
            self.conn,
            principal_id=principal_id,
            key_id=key_id,
            principal_kind="user_confirmation",
            verification_secret=secret,
            project_ref="project_d0000001",
        )
        self.assertIn(registered["status"], {"registered", "replayed"})
        payload = {
            "contract_version": CONTROLLED_EVIDENCE_VERSION,
            "idempotency_key": idempotency_key,
            "principal_id": principal_id,
            "key_id": key_id,
            "source_ref": source_ref,
            "occurred_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "project_ref": "project_d0000001",
            "workstream_ref": "workstream_d0000002",
            "object_ref": "object_d0000003",
            "version_ref": "version_d0000004",
            "subject_ref": "subject_d0000005",
            "privacy_class": "private_local",
            "evidence_kind": "user_confirmation",
            "state": "completed_confirmed",
            "provider_receipt_ref": "",
            "verification_scope": "memory",
            "memory_eligible": True,
            "signature": "",
        }
        payload["signature"] = sign_contract_payload(payload, secret)
        result = ingest_controlled_evidence(
            self.conn,
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            verification_secret=secret,
        )
        self.assertEqual(result["status"], "accepted")
        return result

    def _attach_trusted_document_dependency(
        self,
        *,
        document_evidence_ref: str,
        source_ref: str,
        idempotency_key: str,
    ) -> dict:
        trusted = self._ingest_trusted_confirmation(
            source_ref=source_ref,
            idempotency_key=idempotency_key,
        )
        memory = create_memory_candidate(
            self.conn,
            verified_ref=trusted["verified_ref"],
            subject_ref="subject_d0000005",
        )
        self.assertEqual(memory["status"], "candidate_only")
        # The explicit confirmation creates the memory candidate.  The governed
        # dependency edge is then attached so source withdrawal closes that
        # already-created downstream branch without treating the P2 file clue as
        # independently memory-eligible.
        self.conn.execute(
            "INSERT INTO context_candidate_evidence(candidate_ref,evidence_ref,dependency_role) "
            "VALUES(?,?,'required')",
            (trusted["candidate_ref"], document_evidence_ref),
        )
        self.conn.commit()
        target = register_context_label(
            self.conn,
            kind="target",
            ref="target_d0000011",
            label="Local Document Delivery",
            project_ref="project_d0000001",
            target_scope="local",
            allowed_privacy_classes=["private_local", "sensitive_local", "restricted_local"],
            export_policy="local_only",
        )
        self.assertIn(target["status"], {"registered", "replayed"})
        delivery = prepare_delivery(
            self.conn,
            target_ref="target_d0000011",
            project_ref="project_d0000001",
        )
        self.assertEqual(delivery["status"], "prepared")
        return {**trusted, "memory": memory, "delivery": delivery}

    @staticmethod
    def _write_zip(path: Path, members: dict[str, str | bytes]) -> None:
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, value in members.items():
                archive.writestr(name, value)

    def _start_controlled_worker(
        self,
        session_provider=None,
        *,
        reconcile_eligibility_provider=None,
        reconcile_eligibility_check_interval_seconds: float = 1.0,
        diagnostic_sink=None,
    ) -> tuple[dict, _ControlledProcess]:
        holder: dict[str, _ControlledProcess] = {}

        def factory(command, **kwargs):
            process = _ControlledProcess(command, **kwargs)
            holder["process"] = process
            return process

        worker = start_document_change_worker(
            self.db,
            ["/safe/helper"],
            popen_factory=factory,
            session_state_provider=session_provider or (lambda: {
                "state": "unlocked",
                "locked": False,
                "on_console": True,
                "capture_allowed": True,
            }),
            reconcile_eligibility_provider=reconcile_eligibility_provider,
            reconcile_eligibility_check_interval_seconds=(
                reconcile_eligibility_check_interval_seconds
            ),
            _diagnostic_sink=diagnostic_sink,
        )
        return worker, holder["process"]

    @staticmethod
    def _wait_until(predicate, *, timeout: float = 8.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return bool(predicate())

    @staticmethod
    def _stop_worker(worker: dict) -> None:
        joined = stop_document_change_worker(worker, timeout_seconds=20)
        expected: list[str] = []
        for name in ("reader", "consumer", "validator", "processor", "monitor"):
            thread = worker.get(name)
            if thread is not None:
                expected.append(name)
                if thread.is_alive():
                    raise AssertionError(f"document worker thread leaked: {name}")
        if set(joined) != set(expected):
            raise AssertionError(
                f"document worker join incomplete: expected={expected!r} joined={joined!r}"
            )

    def _install_state_write_audit(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE test_file_watch_state_writes(kind TEXT NOT NULL);
            CREATE TRIGGER test_file_watch_state_insert
            AFTER INSERT ON file_watch_state
            BEGIN
              INSERT INTO test_file_watch_state_writes(kind) VALUES('insert');
            END;
            CREATE TRIGGER test_file_watch_state_update
            AFTER UPDATE ON file_watch_state
            BEGIN
              INSERT INTO test_file_watch_state_writes(kind) VALUES('update');
            END;
            CREATE TRIGGER test_file_watch_state_delete
            AFTER DELETE ON file_watch_state
            BEGIN
              INSERT INTO test_file_watch_state_writes(kind) VALUES('delete');
            END;
            """
        )
        self.conn.commit()

    def test_pending_events_do_not_mislabel_a_covered_time_range(self):
        build_document_baseline(self.conn, self.watch)
        start = datetime.now(timezone.utc) - timedelta(minutes=2)
        self.conn.execute("UPDATE file_watch_checkpoints SET coverage_started_at=?,coverage_status='complete',event_gap_count=0", ((start-timedelta(minutes=1)).isoformat(),))
        self.conn.execute("INSERT OR REPLACE INTO daemon_state(key,value,updated_at) VALUES('document_change_pending_events','7',?)", (start.isoformat(),))
        self.conn.commit()
        result = list_document_changes(self.conn, start.isoformat(), datetime.now(timezone.utc).isoformat())
        self.assertEqual(result["coverage"]["pending_event_count"], 7)
        self.assertFalse(result["coverage"]["is_complete"])
        self.assertIn("coverage_incomplete", result["limitations"])
        self.assertNotIn("requested_range_not_fully_covered", result["limitations"])

    def test_schema_32_additive_and_newer_schema_refuses_downgrade(self):
        self.assertEqual(SCHEMA_VERSION, 32)
        tables = {row["name"] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"file_watch_roots", "file_watch_checkpoints", "file_watch_state", "file_change_events"} <= tables)
        with self.assertRaisesRegex(
            sqlite3.DatabaseError,
            "requires restoring",
        ):
            self.conn.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version','26')"
            )
        self.assertEqual(
            self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0],
            "32",
        )
        self.conn.execute("UPDATE meta SET value='33' WHERE key='schema_version'")
        self.conn.commit()
        with self.assertRaisesRegex(sqlite3.DatabaseError, "refusing downgrade"):
            init_schema(self.conn)

    def test_hmac_secret_is_private_file_and_not_persisted_in_database(self):
        secret_path = self.db.parent / HMAC_SECRET_FILENAME
        self.assertTrue(secret_path.is_file())
        self.assertEqual(secret_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(secret_path.read_bytes()), 32)
        self.assertIsNone(
            self.conn.execute(
                "SELECT value FROM daemon_state WHERE key='document_change_hmac_key'"
            ).fetchone()
        )
        verifier = self.conn.execute(
            "SELECT value FROM daemon_state WHERE key=?",
            (HMAC_VERIFIER_STATE_KEY,),
        ).fetchone()
        self.assertTrue(str(verifier["value"]).startswith("v1:"))

    def test_status_path_binding_validation_is_readonly_and_legacy_state_fails_partial(self):
        secret_path = self.db.parent / HMAC_SECRET_FILENAME
        registry_path = self.db.parent / "document_watch_roots.json"
        before = {
            "secret_mtime": secret_path.stat().st_mtime_ns,
            "secret_mode": secret_path.stat().st_mode & 0o777,
            "registry_mtime": registry_path.stat().st_mtime_ns,
            "registry_mode": registry_path.stat().st_mode & 0o777,
        }
        readonly = connect_readonly(self.db)
        try:
            status = document_changes_status(readonly)
        finally:
            readonly.close()
        self.assertEqual(len(status["roots"]), 1)
        self.assertEqual(secret_path.stat().st_mtime_ns, before["secret_mtime"])
        self.assertEqual(secret_path.stat().st_mode & 0o777, before["secret_mode"])
        self.assertEqual(registry_path.stat().st_mtime_ns, before["registry_mtime"])
        self.assertEqual(registry_path.stat().st_mode & 0o777, before["registry_mode"])

        self.conn.execute("DELETE FROM daemon_state WHERE key=?", (HMAC_VERIFIER_STATE_KEY,))
        self.conn.execute(
            "INSERT INTO daemon_state(key,value,updated_at) VALUES('document_change_hmac_key',?,?)",
            (secret_path.read_bytes().hex(), datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()
        readonly = connect_readonly(self.db)
        try:
            legacy_status = document_changes_status(readonly)
        finally:
            readonly.close()
        self.assertEqual(legacy_status["roots"], [])
        self.assertFalse(legacy_status["coverage"]["is_complete"])
        self.assertEqual(legacy_status["coverage"]["coverage_status"], "partial")
        self.assertIn("coverage_incomplete", legacy_status["limitations"])
        self.assertIsNotNone(
            self.conn.execute(
                "SELECT 1 FROM daemon_state WHERE key='document_change_hmac_key'"
            ).fetchone()
        )
        self.assertIsNone(
            self.conn.execute(
                "SELECT 1 FROM daemon_state WHERE key=?",
                (HMAC_VERIFIER_STATE_KEY,),
            ).fetchone()
        )

    def test_unsafe_or_symlinked_private_sidecars_fail_closed_without_repair(self):
        secret_path = self.db.parent / HMAC_SECRET_FILENAME
        registry_path = self.db.parent / "document_watch_roots.json"
        registry_bytes = registry_path.read_bytes()

        secret_path.chmod(0o644)
        secret_status = document_changes_status(self.conn)
        self.assertEqual(secret_status["roots"], [])
        self.assertEqual(secret_path.stat().st_mode & 0o777, 0o644)
        secret_path.chmod(0o600)

        registry_path.chmod(0o644)
        registry_status = document_changes_status(self.conn)
        self.assertEqual(registry_status["roots"], [])
        self.assertEqual(registry_path.stat().st_mode & 0o777, 0o644)
        registry_path.unlink()
        target = self.root / "registry-target.json"
        target.write_bytes(registry_bytes)
        target.chmod(0o600)
        registry_path.symlink_to(target)
        symlink_status = document_changes_status(self.conn)
        self.assertEqual(symlink_status["roots"], [])
        self.assertFalse(symlink_status["coverage"]["is_complete"])
        registry_path.unlink()
        registry_path.write_bytes(registry_bytes)
        registry_path.chmod(0o600)

    def test_authorization_lock_status_is_readonly_and_fails_closed_when_unsafe(self):
        lock_path = self.db.parent / ".document_watch_authorization.lock"
        self.assertTrue(lock_path.is_file())
        lock_path.chmod(0o644)
        unsafe = document_changes_status(self.conn)
        self.assertEqual(unsafe["roots"], [])
        self.assertFalse(unsafe["coverage"]["is_complete"])
        self.assertIn("authorization_lock_unavailable", unsafe["limitations"])
        self.assertEqual(lock_path.stat().st_mode & 0o777, 0o644)
        lock_path.chmod(0o600)

        lock_path.unlink()
        missing = document_changes_status(self.conn)
        self.assertEqual(missing["roots"], [])
        self.assertIn("authorization_lock_unavailable", missing["limitations"])
        self.assertFalse(lock_path.exists())
        lock_path.write_bytes(b"")
        lock_path.chmod(0o600)

        target = self.root / "authorization-lock-target"
        target.write_bytes(b"")
        target.chmod(0o600)
        lock_path.unlink()
        lock_path.symlink_to(target)
        symlinked = document_changes_status(self.conn)
        self.assertEqual(symlinked["roots"], [])
        self.assertIn("authorization_lock_unavailable", symlinked["limitations"])
        self.assertTrue(lock_path.is_symlink())
        lock_path.unlink()
        lock_path.write_bytes(b"")
        lock_path.chmod(0o600)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO test requires POSIX mkfifo")
    def test_private_fifo_sidecars_fail_quickly_without_blocking(self):
        registry = self.db.parent / "document_watch_roots.json"
        secret = self.db.parent / HMAC_SECRET_FILENAME
        lock_path = self.db.parent / ".document_watch_authorization.lock"
        originals = {
            registry: registry.read_bytes(),
            secret: secret.read_bytes(),
            lock_path: lock_path.read_bytes(),
        }
        for path in (registry, secret, lock_path):
            with self.subTest(path=path.name):
                path.unlink()
                os.mkfifo(path, 0o600)
                started = time.monotonic()
                status = document_changes_status(self.conn)
                elapsed = time.monotonic() - started
                self.assertLess(elapsed, 1.0)
                self.assertEqual(status["roots"], [])
                path.unlink()
                path.write_bytes(originals[path])
                path.chmod(0o600)

    def test_corrupt_public_timestamps_are_blank_and_force_partial_coverage(self):
        checkpoint_path = "/" + "Volumes/Private/checkpoint"
        baseline_path = "/" + "private/secret/baseline"
        baseline_status_path = "/" + "Volumes/Private/status"
        self.conn.execute(
            "UPDATE file_watch_checkpoints SET last_event_at=?,coverage_started_at=?",
            (checkpoint_path, baseline_path),
        )
        self.conn.execute(
            "UPDATE file_watch_roots SET baseline_status=?,baseline_started_at=?,baseline_completed_at=?",
            (baseline_status_path, baseline_path, checkpoint_path),
        )
        self.conn.commit()
        status = document_changes_status(self.conn)
        serialized = json.dumps(status, ensure_ascii=False)
        self.assertNotIn(checkpoint_path, serialized)
        self.assertNotIn(baseline_path, serialized)
        self.assertNotIn(baseline_status_path, serialized)
        self.assertEqual(status["coverage"]["last_event_at"], "")
        self.assertEqual(status["coverage"]["coverage_started_at"], "")
        self.assertFalse(status["coverage"]["is_complete"])
        self.assertEqual(status["roots"], [])

    def test_unverifiable_authorization_blocks_all_document_items_downstream(self):
        path = self.watch / "authorization-private.md"
        path.write_text("before", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        path.write_text("after", encoding="utf-8")
        process_document_event(self.conn, self._event(path, 199), root_path=self.watch)
        configure_document_changes(self.conn, export_enabled=True, runtime_root=self.root / "data")
        registry = self.root / "data/document_watch_roots.json"
        secret = self.db.parent / HMAC_SECRET_FILENAME
        registry_bytes = registry.read_bytes()
        secret_bytes = secret.read_bytes()

        def assert_blocked_everywhere() -> None:
            local = self._all_changes()
            exported = self._all_changes(for_export=True)
            review = daily_review(
                self.conn,
                "2000-01-01T00:00:00+00:00",
                "2100-01-01T00:00:00+00:00",
            )
            handoff = build_ai_handoff_package(
                self.conn,
                range_name="today",
                start="2000-01-01T00:00:00+00:00",
                end="2100-01-01T00:00:00+00:00",
            )
            connector = build_ai_connector_package(self.conn, range_name="today")
            for document_payload in (local, exported, review["document_changes"]):
                self.assertEqual(document_payload["items"], [])
                self.assertEqual(document_payload["totals"]["distinct_files"], 0)
                self.assertIn("authorization_unverifiable", document_payload["limitations"])
            for payload in (handoff, connector):
                self.assertNotIn("authorization-private.md", json.dumps(payload, ensure_ascii=False))

        registry.write_text("{}", encoding="utf-8")
        registry.chmod(0o600)
        assert_blocked_everywhere()
        registry.write_bytes(registry_bytes)
        registry.chmod(0o600)

        registry.unlink()
        assert_blocked_everywhere()
        registry.write_bytes(registry_bytes)
        registry.chmod(0o600)

        secret.unlink()
        assert_blocked_everywhere()
        secret.write_bytes(secret_bytes)
        secret.chmod(0o600)

    def test_legacy_database_hmac_key_migrates_without_changing_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "data" / "activity.sqlite"
            conn = ensure_ready(database)
            legacy = bytes(range(32))
            conn.execute(
                "INSERT INTO daemon_state(key,value,updated_at) VALUES('document_change_hmac_key',?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (legacy.hex(), datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
            try:
                migrated = _hmac_key(conn)
                conn.commit()
                row = conn.execute(
                    "SELECT value FROM daemon_state WHERE key='document_change_hmac_key'"
                ).fetchone()
            finally:
                conn.close()

            secret_path = database.parent / HMAC_SECRET_FILENAME
            self.assertEqual(migrated, legacy)
            self.assertIsNone(row)
            self.assertEqual(secret_path.read_bytes(), legacy)
            self.assertEqual(secret_path.stat().st_mode & 0o777, 0o600)
            verifier = sqlite3.connect(database)
            try:
                stored_verifier = verifier.execute(
                    "SELECT value FROM daemon_state WHERE key=?",
                    (HMAC_VERIFIER_STATE_KEY,),
                ).fetchone()
            finally:
                verifier.close()
            self.assertTrue(str(stored_verifier[0]).startswith("v1:"))

    def test_hmac_secret_mismatch_fails_closed_without_rekeying(self):
        secret_path = self.db.parent / HMAC_SECRET_FILENAME
        original = secret_path.read_bytes()
        conflicting = bytes(reversed(range(32)))
        self.conn.execute(
            "INSERT INTO daemon_state(key,value,updated_at) VALUES('document_change_hmac_key',?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (conflicting.hex(), datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

        status = document_changes_status(self.conn)
        self.assertEqual(status["roots"], [])
        self.assertFalse(status["coverage"]["is_complete"])
        self.assertIn("coverage_incomplete", status["limitations"])
        with self.assertRaisesRegex(RuntimeError, "secret mismatch"):
            _hmac_key(self.conn)

        self.assertEqual(secret_path.read_bytes(), original)
        row = self.conn.execute(
            "SELECT value FROM daemon_state WHERE key='document_change_hmac_key'"
        ).fetchone()
        self.assertEqual(row["value"], conflicting.hex())

    def test_replaced_hmac_sidecar_fails_closed_without_deleting_identity_rows(self):
        secret_path = self.db.parent / HMAC_SECRET_FILENAME
        root_ids = [row["root_id"] for row in self.conn.execute("SELECT root_id FROM file_watch_roots")]
        secret_path.write_bytes(bytes([0xA5]) * 32)
        secret_path.chmod(0o600)

        self.assertEqual(ensure_default_document_roots(self.conn, self.db.parent), [])

        self.assertFalse(self.conn.in_transaction)
        surviving = [row["root_id"] for row in self.conn.execute("SELECT root_id FROM file_watch_roots")]
        self.assertEqual(surviving, root_ids)

    def test_missing_hmac_sidecar_fails_closed_until_paired_restore(self):
        secret_path = self.db.parent / HMAC_SECRET_FILENAME
        original = secret_path.read_bytes()
        root_ids = [row["root_id"] for row in self.conn.execute("SELECT root_id FROM file_watch_roots")]
        secret_path.unlink()

        self.assertEqual(ensure_default_document_roots(self.conn, self.db.parent), [])

        self.assertFalse(self.conn.in_transaction)
        surviving = [row["root_id"] for row in self.conn.execute("SELECT root_id FROM file_watch_roots")]
        self.assertEqual(surviving, root_ids)
        secret_path.write_bytes(original)
        secret_path.chmod(0o600)
        self.assertEqual(_hmac_key(self.conn), original)

    def test_schema24_backup_requires_paired_hmac_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup_database = Path(tmp) / "activity.sqlite"
            backup_conn = sqlite3.connect(backup_database)
            try:
                self.conn.backup(backup_conn)
            finally:
                backup_conn.close()

            restored = ensure_ready(backup_database)
            try:
                with self.assertRaisesRegex(RuntimeError, "secret missing"):
                    _hmac_key(restored)
                secret = (self.db.parent / HMAC_SECRET_FILENAME).read_bytes()
                backup_secret = backup_database.parent / HMAC_SECRET_FILENAME
                backup_secret.write_bytes(secret)
                backup_secret.chmod(0o600)
                self.assertEqual(_hmac_key(restored), secret)
            finally:
                restored.close()

    def test_schema_23_to_28_is_idempotent_and_preserves_existing_data(self):
        migration_db = self.root / "migration.sqlite"
        original = ensure_ready(migration_db)
        original.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES('pre_v24_marker','kept','2026-07-16T00:00:00+00:00')"
        )
        for table in ("file_change_events", "file_watch_state", "file_watch_checkpoints", "file_watch_roots"):
            original.execute(f"DROP TABLE {table}")
        # Reconstruct a real pre-v24 database, which did not yet contain the
        # database-level downgrade guards.
        original.execute("DROP TRIGGER pam_schema24_guard_insert")
        original.execute("DROP TRIGGER pam_schema24_guard_update")
        original.execute("DROP TRIGGER pam_schema25_guard_insert")
        original.execute("DROP TRIGGER pam_schema25_guard_update")
        original.execute("DROP TRIGGER pam_schema26_guard_insert")
        original.execute("DROP TRIGGER pam_schema26_guard_update")
        original.execute("DROP TRIGGER pam_schema27_guard_insert")
        original.execute("DROP TRIGGER pam_schema27_guard_update")
        original.execute("DROP TRIGGER pam_schema28_guard_insert")
        original.execute("DROP TRIGGER pam_schema28_guard_update")
        original.execute("DROP TRIGGER pam_schema30_guard_insert")
        original.execute("DROP TRIGGER pam_schema30_guard_update")
        original.execute("DROP TRIGGER pam_schema31_guard_insert")
        original.execute("DROP TRIGGER pam_schema31_guard_update")
        original.execute("DROP TRIGGER pam_schema32_guard_insert")
        original.execute("DROP TRIGGER pam_schema32_guard_update")
        original.execute("UPDATE meta SET value='23' WHERE key='schema_version'")
        original.commit()
        original.close()

        migrated = ensure_ready(migration_db)
        init_schema(migrated)
        self.assertEqual(migrated.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "32")
        self.assertEqual(migrated.execute("SELECT value FROM settings WHERE key='pre_v24_marker'").fetchone()[0], "kept")
        tables = {row["name"] for row in migrated.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"file_watch_roots", "file_watch_checkpoints", "file_watch_state", "file_change_events"} <= tables)
        migrated.close()

    def test_baseline_modify_and_exports_never_leak_path_or_raw_text(self):
        path = self.watch / "roadmap.md"
        secret_line = "PRIVATE_RAW_SENTENCE_ALPHA"
        path.write_text("# Roadmap\nfirst\n", encoding="utf-8")
        build_document_baseline(self.conn, self.watch, root_label="Work")
        path.write_text(f"# Roadmap\nfirst\n# Release\n{secret_line}\n", encoding="utf-8")
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        result = process_document_event(self.conn, {"path": str(path), "event_id": "9", "observed_at": at}, root_path=self.watch, root_label="Work")
        self.assertTrue(result["recorded"])
        local = self._all_changes()
        encoded = json.dumps(local, ensure_ascii=False)
        self.assertIn("Release", encoded)
        self.assertNotIn(str(self.watch), encoded)
        self.assertNotIn(secret_line, encoded)
        stored = json.dumps([dict(row) for row in self.conn.execute("SELECT * FROM file_watch_state")], ensure_ascii=False)
        self.assertNotIn(str(self.watch), stored)
        self.assertNotIn(secret_line, stored)

        local_review = daily_review(self.conn, "2000-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00")
        self.assertFalse(local_review["document_changes"]["coverage"]["export_enabled"])
        self.assertEqual(local_review["document_changes"]["totals"]["distinct_files"], 1)
        shadow_export = self._all_changes(for_export=True)
        self.assertEqual(shadow_export["document_changes"]["items"] if "document_changes" in shadow_export else shadow_export["items"], [])
        self.assertEqual(shadow_export["totals"]["distinct_files"], 1)
        self.assertEqual(shadow_export["export_state"], "blocked")
        self.assertTrue(shadow_export["observed_changes"])
        configure_document_changes(self.conn, export_enabled=True)
        review = daily_review(self.conn, "2000-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00")
        handoff = build_ai_handoff_package(self.conn, range_name="today", start="2000-01-01T00:00:00+00:00", end="2100-01-01T00:00:00+00:00")
        connector = build_ai_connector_package(self.conn, range_name="today")
        self.assertEqual(review["document_changes"]["totals"]["distinct_files"], 1)
        self.assertIn("document_changes", handoff)
        self.assertIn("document_changes", connector)
        root_ref = document_changes_status(self.conn)["roots"][0]["root_ref"]
        for payload in (review, handoff, connector):
            safe_payload = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn(root_ref, safe_payload)
            self.assertNotIn('"root_id"', safe_payload)
        exported = json.dumps(self._all_changes(for_export=True), ensure_ascii=False)
        self.assertNotRegex(exported, r"\b[0-9a-f]{64}\b")
        self.assertNotIn("content_hmac", exported)

    def _read_legacy_attribution_rows(self, records):
        log = self.root / "legacy-ai-edits.jsonl"
        observed = datetime.now(timezone.utc).isoformat()
        log.write_text("\n".join(json.dumps({"at": observed, **r}) for r in records) + "\n")
        with (
            mock.patch.object(document_changes_module, "AI_EDIT_LOG_PATH", log),
            mock.patch.object(document_changes_module, "AI_EDIT_ROTATED_LOG_PATH", self.root / "no-rotated-log"),
        ):
            return document_changes_module._load_ai_edit_attribution(
                self.conn, "2000-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00"
            )

    def test_legacy_attribution_reuses_identity_only_within_one_complete_read(self):
        target = self.watch / "repeated.md"
        records = [{"path": str(target), "tool": "first", "operation": "created"},
                   {"path": str(target), "tool": "second", "operation": "write"}]
        with mock.patch.object(document_changes_module, "_file_id", wraps=document_changes_module._file_id) as identify:
            first = self._read_legacy_attribution_rows(records)
            self.assertEqual(identify.call_count, 1)
            self.assertEqual([(r["tool"], r["operation"]) for r in next(iter(first.values()))],
                             [("first", "created"), ("second", "write")])
            self.assertEqual(self._read_legacy_attribution_rows(records).keys(), first.keys())
            self.assertEqual(identify.call_count, 2)

    def test_legacy_attribution_does_not_cache_failed_identity(self):
        target = self.watch / "retry.md"
        original = document_changes_module._file_id
        calls = []
        def interrupted(*args):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("temporary identity read failure")
            return original(*args)
        with mock.patch.object(document_changes_module, "_file_id", side_effect=interrupted):
            result = self._read_legacy_attribution_rows([
                {"path": str(target), "tool": "unavailable"}, {"path": str(target), "tool": "recovered"}])
        self.assertEqual(len(calls), 2)
        self.assertEqual([r["tool"] for r in next(iter(result.values()))], ["recovered"])

    def test_legacy_attribution_discards_cached_identity_after_secret_replacement(self):
        target = self.watch / "replaced-key.md"
        original = document_changes_module._file_id
        def replace_secret(*args):
            value = original(*args)
            secret = self.root / "data" / HMAC_SECRET_FILENAME
            replacement = secret.with_name("replacement-secret")
            replacement.write_bytes(b"x" * 32)
            replacement.chmod(0o600)
            os.replace(replacement, secret)
            return value
        with mock.patch.object(document_changes_module, "_file_id", side_effect=replace_secret):
            self.assertEqual(self._read_legacy_attribution_rows([
                {"path": str(target)}, {"path": str(target)}]), {})

    def test_legacy_attribution_discards_read_after_root_alias_change(self):
        target = self.watch / "changed-root.md"
        other = self.root / "unapproved-target"
        other.mkdir()
        original = document_changes_module._file_id
        def change_root(*args):
            value = original(*args)
            self.watch.rename(self.root / "old-watch")
            self.watch.symlink_to(other, target_is_directory=True)
            return value
        with mock.patch.object(document_changes_module, "_file_id", side_effect=change_root):
            self.assertEqual(self._read_legacy_attribution_rows([
                {"path": str(target)}, {"path": str(target)}]), {})

    def test_legacy_attribution_does_not_resolve_suspended_roots(self):
        self.conn.execute("UPDATE file_watch_roots SET enabled=0")
        self.watch.rename(self.root / "retained-watch")
        self.watch.symlink_to(self.root / "offline-company", target_is_directory=True)
        with mock.patch.object(document_changes_module, "_file_id", side_effect=AssertionError("suspended identity read")) as identify:
            self.assertEqual(self._read_legacy_attribution_rows([{"path": str(self.watch / "private.md")}]), {})
        identify.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM file_watch_roots").fetchone()[0], 1)

    def test_legacy_attribution_rejects_authorization_change_while_loading_roots(self):
        original = document_changes_module._read_runtime_registry
        def change_authorization(*args):
            rows = original(*args)
            document_changes_module.bump_document_change_epoch(self.conn)
            return rows
        with (
            mock.patch.object(document_changes_module, "_read_runtime_registry", side_effect=change_authorization),
            mock.patch.object(document_changes_module, "_file_id", side_effect=AssertionError("stale authorization")) as identify,
        ):
            self.assertEqual(self._read_legacy_attribution_rows([{"path": str(self.watch / "changed.md")}]), {})
        identify.assert_not_called()

    def test_legacy_attribution_rejects_authorization_change_after_identity_read(self):
        original = document_changes_module._file_id
        def change_authorization(*args):
            value = original(*args)
            document_changes_module.bump_document_change_epoch(self.conn)
            return value
        with mock.patch.object(document_changes_module, "_file_id", side_effect=change_authorization):
            self.assertEqual(self._read_legacy_attribution_rows([
                {"path": str(self.watch / "changed.md")}, {"path": str(self.watch / "changed.md")}]), {})

    def test_legacy_attribution_cache_limits_fall_back_to_complete_identity_reads(self):
        records = [{"path": str(self.watch / name), "tool": str(index)}
                   for index, name in enumerate(("a.md", "b.md", "b.md", "a.md"))]
        for entry_limit, byte_limit, expected_calls in ((1, 10000, 3), (10, 1, 4)):
            with (
                self.subTest(entry_limit=entry_limit, byte_limit=byte_limit),
                mock.patch.object(document_changes_module, "_ATTRIBUTION_CACHE_ENTRIES", entry_limit),
                mock.patch.object(document_changes_module, "_ATTRIBUTION_CACHE_BYTES", byte_limit),
                mock.patch.object(document_changes_module, "_file_id", wraps=document_changes_module._file_id) as identify,
            ):
                result = self._read_legacy_attribution_rows(records)
                self.assertEqual(identify.call_count, expected_calls)
                self.assertEqual(sorted(r["tool"] for rows in result.values() for r in rows), ["0", "1", "2", "3"])

    def test_three_file_attribution_classes_are_mutually_exclusive_and_conservative(self):
        build_document_baseline(self.conn, self.watch, root_label="Work")
        attribution_log = self.root / "ai-edits.jsonl"
        rotated_log = self.root / "ai-edits.jsonl.1"
        root_id = str(self.conn.execute("SELECT root_id FROM file_watch_roots").fetchone()[0])
        base = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=1)

        ai_file = self.watch / "ai-created.md"
        ai_file.write_text("created by ai\n", encoding="utf-8")
        ai_file_id = document_changes_module._file_id(self.conn, self.watch, ai_file)
        attribution_log.write_text(
            json.dumps(
                {
                    "at": base.isoformat(),
                    "tool": "claude",
                    "action": "Write",
                    "operation": "write",
                    "root_id": root_id,
                    "file_id": ai_file_id,
                }
            ) + "\n",
            encoding="utf-8",
        )

        unknown_file = self.watch / "unknown.md"
        unknown_file.write_text("unknown source\n", encoding="utf-8")
        user_file = self.watch / "owner-edited.md"
        user_file.write_text("owner source\n", encoding="utf-8")
        user_at = base + timedelta(minutes=2)
        os.utime(user_file, (user_at.timestamp(), user_at.timestamp()))
        user_path_hmac = document_changes_module._document_signal_path_hmac(
            self.conn, user_file
        )
        self.conn.execute(
            "INSERT INTO document_signals("
            "id,session_id,captured_at,app_name,file_path_hmac,file_name_redacted,file_modified_at,"
            "subsequent_mtime_event,source,source_confidence,deletion_group_id"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "doc_user_edit",
                "session_user_edit",
                user_at.isoformat(),
                "Obsidian",
                user_path_hmac,
                user_file.name,
                user_at.isoformat(),
                "modified_near_capture",
                "foreground_app_or_file_url",
                0.86,
                "group_user_edit",
            ),
        )
        self.conn.commit()

        with (
            mock.patch.object(document_changes_module, "AI_EDIT_LOG_PATH", attribution_log),
            mock.patch.object(document_changes_module, "AI_EDIT_ROTATED_LOG_PATH", rotated_log),
        ):
            self.assertTrue(
                process_document_event(
                    self.conn,
                    self._event(ai_file, 10, flags=["item_created"], observed_at=base.isoformat()),
                    root_path=self.watch,
                )["recorded"]
            )
            self.assertTrue(
                process_document_event(
                    self.conn,
                    self._event(
                        unknown_file,
                        11,
                        flags=["item_created"],
                        observed_at=(base + timedelta(minutes=1)).isoformat(),
                    ),
                    root_path=self.watch,
                )["recorded"]
            )
            self.assertTrue(
                process_document_event(
                    self.conn,
                    self._event(user_file, 12, flags=["item_created"], observed_at=user_at.isoformat()),
                    root_path=self.watch,
                )["recorded"]
            )
            listing = self._all_changes()

        self.assertEqual(listing["totals"]["distinct_files"], 3)
        self.assertEqual(
            (
                listing["totals"]["ai_only"],
                listing["totals"]["user_modified"],
                listing["totals"]["unattributed"],
            ),
            (1, 1, 1),
        )
        by_name = {item["file_name_redacted"]: item for item in listing["items"]}
        self.assertEqual(by_name[ai_file.name]["actor_classification"], "ai_only")
        self.assertEqual(by_name[user_file.name]["actor_classification"], "user_modified")
        self.assertEqual(by_name[unknown_file.name]["actor_classification"], "unattributed")
        self.assertNotIn(str(ai_file), attribution_log.read_text(encoding="utf-8"))

    def test_human_evidence_overrides_ai_created_origin(self):
        build_document_baseline(self.conn, self.watch, root_label="Work")
        path = self.watch / "ai-then-owner.md"
        base = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=1)
        root_id = str(self.conn.execute("SELECT root_id FROM file_watch_roots").fetchone()[0])
        path.write_text("ai version\n", encoding="utf-8")
        file_id = document_changes_module._file_id(self.conn, self.watch, path)
        log = self.root / "ai-edits.jsonl"
        log.write_text(
            json.dumps(
                {
                    "at": base.isoformat(),
                    "tool": "codex",
                    "action": "apply_patch",
                    "operation": "created",
                    "root_id": root_id,
                    "file_id": file_id,
                }
            ) + "\n",
            encoding="utf-8",
        )
        with (
            mock.patch.object(document_changes_module, "AI_EDIT_LOG_PATH", log),
            mock.patch.object(document_changes_module, "AI_EDIT_ROTATED_LOG_PATH", self.root / "none"),
        ):
            process_document_event(
                self.conn,
                self._event(path, 20, flags=["item_created"], observed_at=base.isoformat()),
                root_path=self.watch,
            )
            user_at = base + timedelta(minutes=2)
            path.write_text("owner version\n", encoding="utf-8")
            os.utime(path, (user_at.timestamp(), user_at.timestamp()))
            user_path_hmac = document_changes_module._document_signal_path_hmac(
                self.conn, path
            )
            self.conn.execute(
                "INSERT INTO document_signals("
                "id,session_id,captured_at,app_name,file_path_hmac,file_name_redacted,file_modified_at,"
                "subsequent_mtime_event,source,source_confidence,deletion_group_id"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "doc_override",
                    "session_override",
                    user_at.isoformat(),
                    "Obsidian",
                    user_path_hmac,
                    path.name,
                    user_at.isoformat(),
                    "modified_near_capture",
                    "foreground_app_or_file_url",
                    0.86,
                    "group_override",
                ),
            )
            self.conn.commit()
            process_document_event(
                self.conn,
                self._event(path, 21, flags=["item_modified"], observed_at=user_at.isoformat()),
                root_path=self.watch,
            )
            listing = self._all_changes()

        self.assertEqual(listing["totals"]["user_modified"], 1)
        self.assertEqual(listing["totals"]["ai_only"], 0)
        self.assertEqual(listing["items"][0]["actor_classification"], "user_modified")

    def test_attribution_survives_runtime_reopen_and_human_override_query(self):
        """Temporary-root E2E: AI create, reopen, owner edit, reopen, query."""

        build_document_baseline(self.conn, self.watch, root_label="Work")
        path = self.watch / "restarted-attribution.md"
        base = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=1)
        path.write_text("AI created\n", encoding="utf-8")
        os.utime(path, (base.timestamp(), base.timestamp()))
        root_id = str(
            self.conn.execute("SELECT root_id FROM file_watch_roots").fetchone()[0]
        )
        file_id = document_changes_module._file_id(self.conn, self.watch, path)
        log = self.root / "ai-edits.jsonl"
        log.write_text(
            json.dumps(
                {
                    "at": base.isoformat(),
                    "tool": "codex",
                    "action": "apply_patch",
                    "operation": "created",
                    "root_id": root_id,
                    "file_id": file_id,
                }
            )
            + "\n",
            encoding="utf-8",
        )

        with (
            mock.patch.object(document_changes_module, "AI_EDIT_LOG_PATH", log),
            mock.patch.object(
                document_changes_module,
                "AI_EDIT_ROTATED_LOG_PATH",
                self.root / "none",
            ),
        ):
            created = process_document_event(
                self.conn,
                self._event(
                    path,
                    30,
                    flags=["item_created"],
                    observed_at=base.isoformat(),
                ),
                root_path=self.watch,
            )
            self.assertTrue(created["recorded"])
            self.assertEqual(self._all_changes()["totals"]["ai_only"], 1)

            self.conn.close()
            self.conn = ensure_ready(self.db)

            user_at = base + timedelta(minutes=2)
            path.write_text("Owner edited after restart\n", encoding="utf-8")
            os.utime(path, (user_at.timestamp(), user_at.timestamp()))
            path_hmac = document_changes_module._document_signal_path_hmac(
                self.conn, path
            )
            self.conn.execute(
                "INSERT INTO document_signals("
                "id,session_id,captured_at,app_name,file_path_hmac,file_name_redacted,file_modified_at,"
                "subsequent_mtime_event,source,source_confidence,deletion_group_id"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "doc_restarted_owner_edit",
                    "session_restarted_owner_edit",
                    user_at.isoformat(),
                    "Obsidian",
                    path_hmac,
                    path.name,
                    user_at.isoformat(),
                    "modified_near_capture",
                    "foreground_app_or_file_url",
                    0.9,
                    "group_restarted_owner_edit",
                ),
            )
            self.conn.commit()
            modified = process_document_event(
                self.conn,
                self._event(
                    path,
                    31,
                    flags=["item_modified"],
                    observed_at=user_at.isoformat(),
                ),
                root_path=self.watch,
            )
            self.assertTrue(modified["recorded"])

            self.conn.close()
            self.conn = ensure_ready(self.db)
            listing = list_document_changes(
                self.conn,
                (base - timedelta(minutes=1)).isoformat(),
                (user_at + timedelta(minutes=1)).isoformat(),
                for_export=False,
                sort_order="recent",
            )

        self.assertEqual(listing["totals"]["distinct_files"], 1)
        self.assertEqual(listing["totals"]["ai_only"], 0)
        self.assertEqual(listing["totals"]["user_modified"], 1)
        self.assertEqual(listing["items"][0]["actor_classification"], "user_modified")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM file_change_versions WHERE file_id=?",
                (file_id,),
            ).fetchone()[0],
            2,
        )

    def test_human_evidence_requires_exact_private_path_reference(self):
        first_dir = self.watch / "one"
        second_dir = self.watch / "two"
        first_dir.mkdir()
        second_dir.mkdir()
        target = first_dir / "README.md"
        other = second_dir / "README.md"
        target.write_text("before target\n", encoding="utf-8")
        other.write_text("other file\n", encoding="utf-8")
        build_document_baseline(self.conn, self.watch, root_label="Work")

        edited_at = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=1)
        target.write_text("after target\n", encoding="utf-8")
        os.utime(target, (edited_at.timestamp(), edited_at.timestamp()))
        os.utime(other, (edited_at.timestamp(), edited_at.timestamp()))
        other_path_hmac = document_changes_module._document_signal_path_hmac(
            self.conn, other
        )
        self.conn.execute(
            "INSERT INTO document_signals("
            "id,session_id,captured_at,app_name,file_path_hmac,file_name_redacted,file_modified_at,"
            "subsequent_mtime_event,source,source_confidence,deletion_group_id"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "doc_same_name_other_path",
                "session_same_name_other_path",
                edited_at.isoformat(),
                "Obsidian",
                other_path_hmac,
                other.name,
                edited_at.isoformat(),
                "modified_near_capture",
                "foreground_app_or_file_url",
                0.86,
                "group_same_name_other_path",
            ),
        )
        self.conn.commit()

        process_document_event(
            self.conn,
            self._event(target, 22, flags=["item_modified"], observed_at=edited_at.isoformat()),
            root_path=self.watch,
        )
        listing = self._all_changes()

        self.assertEqual(listing["totals"]["user_modified"], 0)
        self.assertEqual(listing["totals"]["unattributed"], 1)
        self.assertEqual(listing["items"][0]["actor_classification"], "unattributed")

    def test_nearest_git_worktree_name_becomes_the_project_label(self):
        repo = self.watch / "customer-portal"
        repo.mkdir()
        subprocess.run(
            ["git", "init", "-q"],
            cwd=repo,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        path = repo / "README.md"
        path.write_text("project file\n", encoding="utf-8")

        build_document_baseline(self.conn, self.watch)

        row = self.conn.execute(
            "SELECT project_label FROM file_watch_state WHERE file_name_redacted='README.md'"
        ).fetchone()
        self.assertEqual(row["project_label"], "customer-portal")

    def test_obsidian_root_uses_top_level_folder_as_project(self):
        root_id = str(self.conn.execute("SELECT root_id FROM file_watch_roots").fetchone()[0])
        self.conn.execute(
            "UPDATE file_watch_roots SET root_label='Obsidian 默认仓库' WHERE root_id=?",
            (root_id,),
        )
        path = self.watch / "✦ AI 工作台" / "日报规则.md"
        path.parent.mkdir()
        path.write_text("rule\n", encoding="utf-8")

        label = document_changes_module._project_label_for_path(
            self.conn, self.watch, path
        )

        self.assertEqual(label, "AI 工作台")

    def test_configured_project_mapping_uses_the_longest_relative_prefix(self):
        self.conn.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (
                "document_change_project_mappings",
                json.dumps([
                    {"root_label": "Watch", "relative_prefix": "clients", "project_label": "客户项目"},
                    {"root_label": "Watch", "relative_prefix": "clients/acme", "project_label": "Acme"},
                ], ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        path = self.watch / "clients" / "acme" / "brief.md"
        path.parent.mkdir(parents=True)
        path.write_text("brief\n", encoding="utf-8")

        label = document_changes_module._project_label_for_path(
            self.conn, self.watch, path
        )

        self.assertEqual(label, "Acme")

    def test_internal_root_hash_fragments_are_removed_from_events_and_exports(self):
        path = self.watch / "safe.md"
        path.write_text("before", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        root_id = str(self.conn.execute("SELECT root_id FROM file_watch_roots").fetchone()[0])
        fragment = root_id.removeprefix("root_")[:24]
        self.conn.execute(
            "UPDATE file_watch_roots SET root_label=? WHERE root_id=?",
            ("Label-" + fragment, root_id),
        )
        self.conn.commit()
        path.write_text("after", encoding="utf-8")
        recorded = process_document_event(self.conn, self._event(path, 19), root_path=self.watch)
        self.assertNotIn(fragment, json.dumps(recorded, ensure_ascii=False))
        stored_project = str(
            self.conn.execute("SELECT project_label FROM file_change_events").fetchone()[0]
        )
        self.assertNotIn(fragment, stored_project)

        injected_file_path = "/" + "private/secret/" + fragment + ".md"
        injected_summary_path = "/" + "Volumes/Private/" + fragment + ".md"
        injected_windows_path = "C:" + "\\Private\\client.md"
        self.conn.execute(
            "UPDATE file_change_events SET project_label=?,file_name_redacted=?,file_type=?,"
            "change_summary_redacted=?",
            (
                "Project-" + fragment,
                injected_file_path,
                fragment,
                "Changed path=\"" + injected_summary_path + "\" backup='" + injected_windows_path + "'",
            ),
        )
        self.conn.commit()
        configure_document_changes(self.conn, export_enabled=True)
        payloads = (
            self._all_changes(),
            self._all_changes(for_export=True),
            daily_review(self.conn, "2000-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00"),
            build_ai_handoff_package(
                self.conn,
                range_name="today",
                start="2000-01-01T00:00:00+00:00",
                end="2100-01-01T00:00:00+00:00",
            ),
            build_ai_connector_package(self.conn, range_name="today"),
        )
        for payload in payloads:
            with self.subTest(keys=tuple(payload.keys())):
                self.assertNotIn(fragment, json.dumps(payload, ensure_ascii=False))
                self.assertNotIn(injected_file_path, json.dumps(payload, ensure_ascii=False))
                self.assertNotIn(injected_summary_path, json.dumps(payload, ensure_ascii=False))
                self.assertNotIn(injected_windows_path, json.dumps(payload, ensure_ascii=False))

        windows_project = "C:" + "\\Users\\sam\\Project"
        windows_file = "C:" + "\\Users\\sam\\secret.md"
        injected_type_path = "/" + "Volumes/Private/type"
        chinese_prefixed_path = "路径/" + "Volumes/Private/client.md"
        unc_path = "\\\\server\\share\\secret.docx"
        single_component_path = "/" + "secret.md"
        self.conn.execute(
            "UPDATE file_change_events SET project_label=?,file_name_redacted=?,file_type=?,"
            "change_summary_redacted=?",
            (
                windows_project,
                windows_file,
                injected_type_path,
                chinese_prefixed_path + " " + unc_path + " " + single_component_path,
            ),
        )
        self.conn.commit()
        hardened_payloads = (
            self._all_changes(),
            self._all_changes(for_export=True),
            daily_review(self.conn, "2000-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00"),
            build_ai_handoff_package(
                self.conn,
                range_name="today",
                start="2000-01-01T00:00:00+00:00",
                end="2100-01-01T00:00:00+00:00",
            ),
            build_ai_connector_package(self.conn, range_name="today"),
        )
        for payload in hardened_payloads:
            encoded = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn(windows_project, encoded)
            self.assertNotIn(windows_file, encoded)
            self.assertNotIn(injected_type_path, encoded)
            self.assertNotIn(chinese_prefixed_path, encoded)
            self.assertNotIn(unc_path, encoded)
            self.assertNotIn(single_component_path, encoded)

    def test_each_absolute_path_shape_is_independently_redacted(self):
        path = self.watch / "path-boundary.md"
        path.write_text("before", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        path.write_text("after", encoding="utf-8")
        process_document_event(self.conn, self._event(path, 198), root_path=self.watch)
        configure_document_changes(self.conn, export_enabled=True, runtime_root=self.root / "data")
        cases = (
            ("change_summary_redacted", "路径/" + "Volumes/Private/client.md"),
            ("change_summary_redacted", "\\\\server\\share\\secret.docx"),
            ("change_summary_redacted", "/" + "secret.md"),
            ("project_label", "C:" + "\\Users\\sam\\Project"),
            ("file_name_redacted", "C:" + "\\Users\\sam\\secret.md"),
            ("file_type", "/" + "Volumes/Private/type"),
        )
        for field, injected in cases:
            with self.subTest(field=field, injected=injected):
                self.conn.execute(
                    "UPDATE file_change_events SET project_label='Project',file_name_redacted='safe.md',"
                    "file_type='md',change_summary_redacted='Safe summary'"
                )
                self.conn.execute(f"UPDATE file_change_events SET {field}=?", (injected,))
                self.conn.commit()
                encoded = json.dumps(self._all_changes(for_export=True), ensure_ascii=False)
                self.assertNotIn(injected, encoded)
                self.assertFalse(validate_ai_handoff_text(injected)["ok"])

    def test_protected_change_is_content_free_in_state_and_event(self):
        path = self.watch / "个人病历.md"
        path.write_text("# 检查\n血压和诊断内容", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        path.write_text("# 检查\n新的诊断内容", encoding="utf-8")
        result = process_document_event(self.conn, {"path": str(path), "event_id": "10"}, root_path=self.watch)
        self.assertEqual(result["item"]["detail_level"], "protected")
        self.assertEqual(result["item"]["file_name_redacted"], "")
        state = self.conn.execute("SELECT file_name_redacted, structure_hmac_json FROM file_watch_state").fetchone()
        self.assertEqual(state["file_name_redacted"], "")
        self.assertEqual(json.loads(state["structure_hmac_json"])["labels"], [])
        listed = self._all_changes()
        self.assertEqual(listed["totals"]["protected"], 1)
        self.assertEqual(listed["items"], [])

    def test_english_medical_financial_private_chat_and_client_names_are_count_only(self):
        bank = self.watch / "bank_statement.pdf"
        medical = self.watch / "medical_record.pdf"
        client = self.watch / "client_list.xlsx"
        private_chat = self.watch / "private_chat.txt"
        bank.write_bytes(b"before")
        medical.write_bytes(b"before")
        private_chat.write_text("neutral before", encoding="utf-8")
        self._write_zip(
            client,
            {
                "xl/workbook.xml": "<workbook xmlns='x'><sheets><sheet name='Sheet1'/></sheets></workbook>",
                "xl/worksheets/sheet1.xml": "<worksheet xmlns='x'><sheetData/></worksheet>",
            },
        )
        build_document_baseline(self.conn, self.watch)
        bank.write_bytes(b"after")
        medical.write_bytes(b"after")
        private_chat.write_text("neutral after", encoding="utf-8")
        self._write_zip(
            client,
            {
                "xl/workbook.xml": "<workbook xmlns='x'><sheets><sheet name='Sheet1'/></sheets></workbook>",
                "xl/worksheets/sheet1.xml": "<worksheet xmlns='x'><sheetData><row r='1'/></sheetData></worksheet>",
            },
        )
        for event_id, path in enumerate((bank, medical, client, private_chat), start=201):
            result = process_document_event(self.conn, self._event(path, event_id), root_path=self.watch)
            self.assertEqual(result["item"]["detail_level"], "protected")
            self.assertEqual(result["item"]["file_name_redacted"], "")
        listed = self._all_changes()
        self.assertEqual(listed["items"], [])
        self.assertEqual(listed["totals"]["distinct_files"], 4)
        self.assertEqual(listed["totals"]["protected"], 4)
        self.assertTrue(
            all(
                not str(row["file_name_redacted"] or "")
                for row in self.conn.execute("SELECT file_name_redacted FROM file_watch_state")
            )
        )

    def test_status_requires_a_completed_baseline_and_checkpoint(self):
        status = document_changes_status(self.conn)
        self.assertEqual(status["coverage"]["baseline_status"], "building")
        self.assertFalse(status["coverage"]["is_complete"])
        self.assertIn("coverage_incomplete", status["limitations"])
        build_document_baseline(self.conn, self.watch)
        ready = document_changes_status(self.conn)
        self.assertTrue(ready["coverage"]["is_complete"])
        self.assertEqual(ready["coverage"]["coverage_status"], "complete")

    def test_startup_deferral_marks_existing_coverage_partial_without_scanning(self):
        path = self.watch / "existing.md"
        path.write_text("existing baseline", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        checkpoint_before = self.conn.execute(
            "SELECT last_event_id FROM file_watch_checkpoints"
        ).fetchone()[0]

        with mock.patch(
            "activity_mvp.document_changes._iter_safe_files",
            side_effect=AssertionError("startup deferral must not scan"),
        ):
            result = defer_document_change_startup_reconcile(
                self.conn,
                observed_at="2026-07-26T14:00:00+00:00",
            )

        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["root_count"], 1)
        root = self.conn.execute(
            "SELECT baseline_status,baseline_started_at,baseline_completed_at "
            "FROM file_watch_roots"
        ).fetchone()
        checkpoint = self.conn.execute(
            "SELECT last_event_id,coverage_status,event_gap_count,coverage_started_at "
            "FROM file_watch_checkpoints"
        ).fetchone()
        self.assertEqual(root["baseline_status"], "partial")
        self.assertIsNone(root["baseline_started_at"])
        self.assertIsNone(root["baseline_completed_at"])
        self.assertEqual(checkpoint["last_event_id"], checkpoint_before)
        self.assertEqual(checkpoint["coverage_status"], "partial")
        self.assertGreaterEqual(checkpoint["event_gap_count"], 1)
        self.assertIsNone(checkpoint["coverage_started_at"])
        coverage = document_changes_status(self.conn)["coverage"]
        self.assertEqual(coverage["coverage_status"], "partial")
        self.assertFalse(coverage["is_complete"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM file_watch_state").fetchone()[0],
            1,
        )

    def test_complete_baseline_reconcile_writes_only_actual_changes(self):
        for index in range(24):
            (self.watch / f"note-{index:02d}.md").write_text(
                f"version {index}\n",
                encoding="utf-8",
            )
        first = build_document_baseline(
            self.conn,
            self.watch,
            batch_sleep_seconds=0,
        )
        self.assertEqual(first["files_written"], 24)
        before_rows = {
            str(row["file_name_redacted"]): str(row["updated_at"])
            for row in self.conn.execute(
                "SELECT file_name_redacted,updated_at FROM file_watch_state"
            ).fetchall()
        }
        changes_before = self.conn.total_changes

        from activity_mvp import document_changes as module

        original_snapshot = module._snapshot
        transaction_states: list[bool] = []

        def observe_transaction(conn, *args, **kwargs):
            transaction_states.append(bool(conn.in_transaction))
            return original_snapshot(conn, *args, **kwargs)

        with mock.patch(
            "activity_mvp.document_changes._snapshot",
            side_effect=observe_transaction,
        ):
            unchanged = build_document_baseline(
                self.conn,
                self.watch,
                batch_sleep_seconds=0,
            )

        self.assertEqual(unchanged["files_written"], 0)
        self.assertEqual(unchanged["files_unchanged"], 24)
        self.assertEqual(unchanged["files_deleted"], 0)
        self.assertTrue(transaction_states)
        self.assertFalse(any(transaction_states))
        self.assertEqual(self.conn.total_changes - changes_before, 3)
        self.assertEqual(
            {
                str(row["file_name_redacted"]): str(row["updated_at"])
                for row in self.conn.execute(
                    "SELECT file_name_redacted,updated_at FROM file_watch_state"
                ).fetchall()
            },
            before_rows,
        )
        completed_at = str(
            self.conn.execute(
                "SELECT baseline_completed_at FROM file_watch_roots"
            ).fetchone()[0]
        )
        overlap = process_document_event(
            self.conn,
            self._event(
                self.watch / "note-02.md",
                899,
                flags=["item_modified"],
                observed_at=completed_at,
            ),
            root_path=self.watch,
        )
        self.assertEqual(overlap["reason"], "same_content_event")
        overlap_coverage = document_changes_status(self.conn)["coverage"]
        self.assertTrue(overlap_coverage["is_complete"])
        self.assertEqual(overlap_coverage["event_gap_count"], 0)

        (self.watch / "note-00.md").write_text("changed content\n", encoding="utf-8")
        (self.watch / "note-01.md").unlink()
        changed = build_document_baseline(
            self.conn,
            self.watch,
            batch_sleep_seconds=0,
        )
        self.assertEqual(changed["files_written"], 1)
        self.assertEqual(changed["files_unchanged"], 22)
        self.assertEqual(changed["files_deleted"], 1)
        after_rows = {
            str(row["file_name_redacted"]): str(row["updated_at"])
            for row in self.conn.execute(
                "SELECT file_name_redacted,updated_at FROM file_watch_state"
            ).fetchall()
        }
        self.assertNotIn("note-01.md", after_rows)
        self.assertNotEqual(after_rows["note-00.md"], before_rows["note-00.md"])
        self.assertEqual(after_rows["note-02.md"], before_rows["note-02.md"])

    def test_unchanged_baseline_uses_only_boundary_write_locks(self):
        from activity_mvp import document_changes as module

        for index in range(24):
            (self.watch / f"stable-{index:02d}.md").write_text(
                f"stable {index}\n",
                encoding="utf-8",
            )
        build_document_baseline(self.conn, self.watch, batch_sleep_seconds=0)
        original_acquire = module._acquire_document_change_write_lock
        write_lock_calls = 0

        def observe_write_lock(conn):
            nonlocal write_lock_calls
            write_lock_calls += 1
            return original_acquire(conn)

        with mock.patch(
            "activity_mvp.document_changes._acquire_document_change_write_lock",
            side_effect=observe_write_lock,
        ):
            result = build_document_baseline(
                self.conn,
                self.watch,
                batch_sleep_seconds=0,
            )

        self.assertEqual(result["files_written"], 0)
        self.assertEqual(result["files_unchanged"], 24)
        # One exact gate marks the root building and one final gate promotes
        # coverage.  Unchanged candidates use only the deferred read gate.
        self.assertEqual(write_lock_calls, 2)
        self.assertFalse(self.conn.in_transaction)

    def test_new_baseline_persists_with_bounded_write_batches(self):
        from activity_mvp import document_changes as module

        file_count = module.DOCUMENT_BASELINE_WRITE_BATCH_SIZE + 5
        for index in range(file_count):
            (self.watch / f"new-{index:03d}.md").write_text(
                f"new {index}\n",
                encoding="utf-8",
            )
        original_acquire = module._acquire_document_change_write_lock
        write_lock_calls = 0

        def observe_write_lock(conn):
            nonlocal write_lock_calls
            write_lock_calls += 1
            return original_acquire(conn)

        with mock.patch(
            "activity_mvp.document_changes._acquire_document_change_write_lock",
            side_effect=observe_write_lock,
        ):
            result = build_document_baseline(
                self.conn,
                self.watch,
                batch_sleep_seconds=0,
            )

        self.assertEqual(result["baseline_status"], "complete")
        self.assertEqual(result["files_written"], file_count)
        self.assertEqual(result["files_unchanged"], 0)
        # Initial + two bounded batch flushes + final coverage promotion.
        self.assertEqual(write_lock_calls, 4)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM file_watch_state").fetchone()[0],
            file_count,
        )
        self.assertFalse(self.conn.in_transaction)

    def test_epoch_change_discards_unflushed_baseline_batch(self):
        first = self.watch / "first.md"
        second = self.watch / "second.md"
        first.write_text("first\n", encoding="utf-8")
        second.write_text("second\n", encoding="utf-8")

        def candidates():
            yield first
            # The first fingerprint is still only in the bounded memory batch.
            # Reset advances the epoch and owns deletion before the old scan can
            # observe the second candidate.
            configure_document_changes(self.conn, reset_baseline=True)
            yield second

        result = build_document_baseline(
            self.conn,
            self.watch,
            paths=candidates(),
            batch_sleep_seconds=0,
        )

        self.assertEqual(result["baseline_status"], "authorization_changed")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM file_watch_state").fetchone()[0],
            0,
        )
        root = self.conn.execute(
            "SELECT baseline_status,baseline_completed_at FROM file_watch_roots"
        ).fetchone()
        self.assertEqual(root["baseline_status"], "pending")
        self.assertIsNone(root["baseline_completed_at"])
        self.assertFalse(self.conn.in_transaction)

    def test_batch_flush_rechecks_row_under_write_lock(self):
        from activity_mvp import document_changes as module

        path = self.watch / "concurrent.md"
        path.write_text("current\n", encoding="utf-8")
        original_snapshot = module._snapshot
        original_begin = module._begin_document_change_write
        captured: list[dict] = []
        begin_calls = 0
        concurrent_revision = "2026-08-10T12:00:00+00:00"

        def capture_snapshot(*args, **kwargs):
            snapshot = original_snapshot(*args, **kwargs)
            captured.append(snapshot)
            return snapshot

        def write_same_row_before_flush(conn, root_id, expected_epoch):
            nonlocal begin_calls
            begin_calls += 1
            if begin_calls == 2:
                concurrent = ensure_ready(self.db)
                try:
                    concurrent.execute("BEGIN IMMEDIATE")
                    module._upsert_state(
                        concurrent,
                        root_id,
                        captured[0],
                        concurrent_revision,
                    )
                    concurrent.commit()
                finally:
                    concurrent.close()
            return original_begin(conn, root_id, expected_epoch)

        with mock.patch(
            "activity_mvp.document_changes._snapshot",
            side_effect=capture_snapshot,
        ), mock.patch(
            "activity_mvp.document_changes._begin_document_change_write",
            side_effect=write_same_row_before_flush,
        ):
            result = build_document_baseline(
                self.conn,
                self.watch,
                batch_sleep_seconds=0,
            )

        self.assertEqual(result["files_written"], 0)
        self.assertEqual(result["files_unchanged"], 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT updated_at FROM file_watch_state WHERE file_name_redacted='concurrent.md'"
            ).fetchone()[0],
            concurrent_revision,
        )
        self.assertNotIn("structure", captured[0])

    def test_batch_flush_rechecks_live_session_after_write_lock(self):
        from activity_mvp import document_changes as module

        path = self.watch / "lock-before-flush.md"
        path.write_text("content captured while unlocked\n", encoding="utf-8")
        probe_transaction_states: list[bool] = []

        def locked_after_wait() -> bool:
            probe_transaction_states.append(self.conn.in_transaction)
            return False

        token = module._DOCUMENT_BASELINE_LIVE_SESSION_PROBE.set(
            locked_after_wait
        )
        metrics = {}
        def record(name, value=1):
            metrics[name] = metrics.get(name, 0) + value
        metric_token = module._DOCUMENT_BASELINE_METRIC_RECORDER.set(record)
        try:
            result = build_document_baseline(
                self.conn,
                self.watch,
                batch_sleep_seconds=0,
                content_allowed_check=lambda: True,
            )
        finally:
            module._DOCUMENT_BASELINE_LIVE_SESSION_PROBE.reset(token)

            module._DOCUMENT_BASELINE_METRIC_RECORDER.reset(metric_token)
        self.assertEqual(metrics.get("failure_session_content_denied"), 1)

        self.assertEqual(probe_transaction_states, [True])
        self.assertEqual(result["baseline_status"], "partial")
        self.assertEqual(result["files_indexed"], 0)
        self.assertEqual(result["files_failed"], 1)
        self.assertEqual(result["files_skipped"], 1)
        self.assertEqual(result["files_written"], 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM file_watch_state").fetchone()[0],
            0,
        )
        self.assertFalse(self.conn.in_transaction)

    def test_lock_during_batch_write_rolls_back_the_whole_batch(self):
        from activity_mvp import document_changes as module

        for index in range(2):
            (self.watch / f"lock-during-write-{index}.md").write_text(
                f"content {index}\n",
                encoding="utf-8",
            )
        original_upsert = module._upsert_state
        session_allowed = True
        session_probe_calls = 0
        upsert_calls = 0

        def session_probe() -> bool:
            nonlocal session_probe_calls
            session_probe_calls += 1
            return session_allowed

        def lock_after_first_upsert(*args, **kwargs):
            nonlocal session_allowed, upsert_calls
            upsert_calls += 1
            result = original_upsert(*args, **kwargs)
            if upsert_calls == 1:
                session_allowed = False
            return result

        token = module._DOCUMENT_BASELINE_LIVE_SESSION_PROBE.set(session_probe)
        metrics = {}
        def record(name, value=1):
            metrics[name] = metrics.get(name, 0) + value
        metric_token = module._DOCUMENT_BASELINE_METRIC_RECORDER.set(record)
        try:
            with mock.patch(
                "activity_mvp.document_changes._upsert_state",
                side_effect=lock_after_first_upsert,
            ):
                result = build_document_baseline(
                    self.conn,
                    self.watch,
                    batch_sleep_seconds=0,
                    content_allowed_check=lambda: True,
                )
        finally:
            module._DOCUMENT_BASELINE_LIVE_SESSION_PROBE.reset(token)

            module._DOCUMENT_BASELINE_METRIC_RECORDER.reset(metric_token)
        self.assertEqual(metrics.get("failure_session_content_denied"), 1)

        self.assertEqual(session_probe_calls, 2)
        self.assertEqual(upsert_calls, 2)
        self.assertEqual(result["baseline_status"], "partial")
        self.assertEqual(result["files_indexed"], 0)
        self.assertEqual(result["files_failed"], 2)
        self.assertEqual(result["files_written"], 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM file_watch_state").fetchone()[0],
            0,
        )
        self.assertFalse(self.conn.in_transaction)

    def test_batch_write_failure_rolls_back_every_row(self):
        from activity_mvp import document_changes as module

        for index in range(3):
            (self.watch / f"atomic-{index}.md").write_text(
                f"atomic {index}\n",
                encoding="utf-8",
            )
        original_upsert = module._upsert_state
        upsert_calls = 0

        def fail_second_upsert(*args, **kwargs):
            nonlocal upsert_calls
            upsert_calls += 1
            if upsert_calls == 2:
                raise sqlite3.OperationalError("simulated batch write failure")
            return original_upsert(*args, **kwargs)

        with mock.patch(
            "activity_mvp.document_changes._upsert_state",
            side_effect=fail_second_upsert,
        ):
            with self.assertRaisesRegex(
                sqlite3.OperationalError,
                "simulated batch write failure",
            ):
                build_document_baseline(
                    self.conn,
                    self.watch,
                    batch_sleep_seconds=0,
                )

        self.assertEqual(upsert_calls, 2)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM file_watch_state").fetchone()[0],
            0,
        )
        self.assertFalse(self.conn.in_transaction)

    def test_baseline_write_batch_is_bounded_by_payload_bytes(self):
        from activity_mvp import document_changes as module

        for index in range(3):
            (self.watch / f"byte-bound-{index}.md").write_text(
                f"bounded {index}\n",
                encoding="utf-8",
            )
        original_acquire = module._acquire_document_change_write_lock
        write_lock_calls = 0

        def observe_write_lock(conn):
            nonlocal write_lock_calls
            write_lock_calls += 1
            return original_acquire(conn)

        with mock.patch.object(
            module,
            "DOCUMENT_BASELINE_WRITE_BATCH_MAX_BYTES",
            1,
        ), mock.patch(
            "activity_mvp.document_changes._acquire_document_change_write_lock",
            side_effect=observe_write_lock,
        ):
            result = build_document_baseline(
                self.conn,
                self.watch,
                batch_sleep_seconds=0,
            )

        self.assertEqual(result["files_written"], 3)
        # Initial + one flush per byte-capped row + final promotion.
        self.assertEqual(write_lock_calls, 5)

    def test_batch_flush_preserves_divergent_concurrent_state(self):
        from activity_mvp import document_changes as module

        path = self.watch / "divergent.md"
        path.write_text("baseline candidate\n", encoding="utf-8")
        original_snapshot = module._snapshot
        original_begin = module._begin_document_change_write
        captured: list[dict] = []
        begin_calls = 0

        def capture_snapshot(*args, **kwargs):
            snapshot = original_snapshot(*args, **kwargs)
            captured.append(snapshot)
            return snapshot

        def write_divergent_row_before_flush(conn, root_id, expected_epoch):
            nonlocal begin_calls
            begin_calls += 1
            if begin_calls == 2:
                concurrent_snapshot = dict(captured[0])
                concurrent_snapshot["content_hmac"] = "f" * 64
                concurrent = ensure_ready(self.db)
                try:
                    concurrent.execute("BEGIN IMMEDIATE")
                    module._upsert_state(
                        concurrent,
                        root_id,
                        concurrent_snapshot,
                        "2026-08-10T12:30:00+00:00",
                    )
                    concurrent.commit()
                finally:
                    concurrent.close()
            return original_begin(conn, root_id, expected_epoch)

        with mock.patch(
            "activity_mvp.document_changes._snapshot",
            side_effect=capture_snapshot,
        ), mock.patch(
            "activity_mvp.document_changes._begin_document_change_write",
            side_effect=write_divergent_row_before_flush,
        ):
            result = build_document_baseline(
                self.conn,
                self.watch,
                batch_sleep_seconds=0,
            )

        self.assertEqual(result["baseline_status"], "partial")
        self.assertEqual(result["files_indexed"], 0)
        self.assertEqual(result["files_failed"], 1)
        self.assertEqual(result["files_written"], 0)
        self.assertEqual(
            self.conn.execute(
                "SELECT content_hmac FROM file_watch_state WHERE file_name_redacted='divergent.md'"
            ).fetchone()[0],
            "f" * 64,
        )

    def test_barriered_large_unchanged_baseline_does_not_write_file_state(self):
        for index in range(160):
            (self.watch / f"unchanged-{index:03d}.md").write_text(
                f"stable {index}\n",
                encoding="utf-8",
            )
        build_document_baseline(
            self.conn,
            self.watch,
            batch_sleep_seconds=0,
        )
        before_rows = {
            str(row["file_id"]): str(row["updated_at"])
            for row in self.conn.execute(
                "SELECT file_id,updated_at FROM file_watch_state"
            ).fetchall()
        }
        self._install_state_write_audit()

        worker, _ = self._start_controlled_worker()
        try:
            writes = self.conn.execute(
                "SELECT kind,COUNT(*) AS count FROM test_file_watch_state_writes GROUP BY kind"
            ).fetchall()
            self.assertEqual(writes, [])
            self.assertEqual(
                {
                    str(row["file_id"]): str(row["updated_at"])
                    for row in self.conn.execute(
                        "SELECT file_id,updated_at FROM file_watch_state"
                    ).fetchall()
                },
                before_rows,
            )
            self.assertTrue(document_changes_status(self.conn)["coverage"]["is_complete"])
        finally:
            self._stop_worker(worker)

    def test_barriered_baseline_writes_only_changed_and_deleted_rows(self):
        changed_path = self.watch / "changed.md"
        deleted_path = self.watch / "deleted.md"
        unchanged_path = self.watch / "unchanged.md"
        changed_path.write_text("before\n", encoding="utf-8")
        deleted_path.write_text("delete me\n", encoding="utf-8")
        unchanged_path.write_text("stable\n", encoding="utf-8")
        build_document_baseline(
            self.conn,
            self.watch,
            batch_sleep_seconds=0,
        )
        unchanged_revision = str(
            self.conn.execute(
                "SELECT updated_at FROM file_watch_state WHERE file_name_redacted='unchanged.md'"
            ).fetchone()[0]
        )
        self._install_state_write_audit()
        changed_path.write_text("after\n", encoding="utf-8")
        deleted_path.unlink()

        worker, _ = self._start_controlled_worker()
        try:
            writes = {
                str(row["kind"]): int(row["count"])
                for row in self.conn.execute(
                    "SELECT kind,COUNT(*) AS count FROM test_file_watch_state_writes GROUP BY kind"
                ).fetchall()
            }
            self.assertEqual(writes, {"delete": 1, "update": 1})
            self.assertEqual(
                self.conn.execute(
                    "SELECT COUNT(*) FROM file_watch_state WHERE file_name_redacted='deleted.md'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                str(
                    self.conn.execute(
                        "SELECT updated_at FROM file_watch_state "
                        "WHERE file_name_redacted='unchanged.md'"
                    ).fetchone()[0]
                ),
                unchanged_revision,
            )
            self.assertTrue(document_changes_status(self.conn)["coverage"]["is_complete"])
        finally:
            self._stop_worker(worker)

    def test_baseline_skips_hidden_temp_dependencies_gitignored_and_outside_symlink(self):
        subprocess.run(["git", "init", "-q"], cwd=self.watch, check=True)
        (self.watch / ".gitignore").write_text("ignored.md\n", encoding="utf-8")
        (self.watch / "safe.md").write_text("safe", encoding="utf-8")
        (self.watch / ".hidden.md").write_text("hidden", encoding="utf-8")
        (self.watch / "ignored.md").write_text("ignored", encoding="utf-8")
        (self.watch / "draft.tmp").write_text("temp", encoding="utf-8")
        dependency = self.watch / "node_modules"
        dependency.mkdir()
        (dependency / "package.md").write_text("dependency", encoding="utf-8")
        outside = self.root / "outside.md"
        outside.write_text("outside", encoding="utf-8")
        (self.watch / "outside-link.md").symlink_to(outside)

        baseline = build_document_baseline(self.conn, self.watch)
        self.assertEqual(baseline["baseline_status"], "complete")
        names = [row["file_name_redacted"] for row in self.conn.execute("SELECT file_name_redacted FROM file_watch_state")]
        self.assertEqual(names, ["safe.md"])
        database_dump = "\n".join(self.conn.iterdump())
        self.assertNotIn(str(self.watch), database_dump)
        self.assertNotIn(str(outside), database_dump)

    def test_sensitive_text_beyond_initial_window_is_protected_and_count_only(self):
        path = self.watch / "archive.md"
        path.write_text(("普通内容\n" * 3000) + "客户姓名：张三 手机号：13800000000\n", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        path.write_text(("普通内容\n" * 3000) + "客户姓名：李四 手机号：13900000000\n", encoding="utf-8")
        result = process_document_event(self.conn, self._event(path, 30), root_path=self.watch)
        self.assertTrue(result["recorded"])
        self.assertEqual(result["item"]["detail_level"], "protected")
        self.assertEqual(result["item"]["project_label"], "")
        listed = self._all_changes()
        self.assertEqual(listed["totals"]["distinct_files"], 1)
        self.assertEqual(listed["totals"]["protected"], 1)
        self.assertEqual(listed["items"], [])
        encoded = json.dumps(listed, ensure_ascii=False)
        self.assertNotIn("archive.md", encoded)
        self.assertNotIn("张三", encoded)
        self.assertNotIn("李四", encoded)

    def test_same_size_text_saves_are_fingerprinted_and_aggregated_by_file(self):
        path = self.watch / "module.py"
        path.write_text("def alpha():\n    return 1\n", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        path.write_text("def bravo():\n    return 2\n", encoding="utf-8")
        first = process_document_event(self.conn, self._event(path, 40), root_path=self.watch)
        path.write_text("def charl():\n    return 3\n", encoding="utf-8")
        second = process_document_event(self.conn, self._event(path, 41), root_path=self.watch)
        self.assertTrue(first["recorded"])
        self.assertTrue(second["recorded"])
        listed = self._all_changes()
        self.assertEqual(listed["totals"]["distinct_files"], 1)
        self.assertEqual(listed["totals"]["modified"], 1)
        self.assertEqual(listed["items"][0]["version_count"], 2)
        self.assertIn("charl", listed["items"][0]["change_summary_redacted"])

    def test_cross_midnight_versions_remain_queryable_in_each_local_day(self):
        path = self.watch / "midnight.md"
        path.write_text("before\n", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        local_day = datetime.now().astimezone().replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        before_midnight = local_day.replace(hour=23, minute=59)
        after_midnight = local_day + timedelta(days=1, minutes=1)

        path.write_text("first\n", encoding="utf-8")
        process_document_event(
            self.conn,
            self._event(path, 45, observed_at=before_midnight.isoformat()),
            root_path=self.watch,
        )
        path.write_text("second\n", encoding="utf-8")
        process_document_event(
            self.conn,
            self._event(path, 46, observed_at=after_midnight.isoformat()),
            root_path=self.watch,
        )

        first_day = list_document_changes(
            self.conn,
            local_day.isoformat(),
            (local_day + timedelta(days=1)).isoformat(),
        )
        second_day = list_document_changes(
            self.conn,
            (local_day + timedelta(days=1)).isoformat(),
            (local_day + timedelta(days=2)).isoformat(),
        )
        self.assertEqual(first_day["totals"]["distinct_files"], 1)
        self.assertEqual(second_day["totals"]["distinct_files"], 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM file_change_versions WHERE file_name_redacted='midnight.md'"
            ).fetchone()[0],
            2,
        )

    def test_owner_file_history_ranges_end_at_now_instead_of_future_midnight(self):
        fixed = datetime(2026, 8, 21, 12, 34, 56, tzinfo=timezone(timedelta(hours=8)))
        today_start, today_end = resolve_document_change_range("today", now=fixed)
        self.assertEqual(datetime.fromisoformat(today_start), fixed.replace(hour=0, minute=0, second=0))
        self.assertEqual(datetime.fromisoformat(today_end), fixed)

        ninety_start, ninety_end = resolve_document_change_range("90d", now=fixed)
        self.assertEqual(datetime.fromisoformat(ninety_start).date(), (fixed - timedelta(days=89)).date())
        self.assertEqual(datetime.fromisoformat(ninety_end), fixed)

        yesterday_start, yesterday_end = resolve_document_change_range("yesterday", now=fixed)
        self.assertEqual(datetime.fromisoformat(yesterday_end), fixed.replace(hour=0, minute=0, second=0))
        self.assertEqual(
            datetime.fromisoformat(yesterday_end) - datetime.fromisoformat(yesterday_start),
            timedelta(days=1),
        )

    def test_create_continuous_save_atomic_replace_rename_and_delete(self):
        build_document_baseline(self.conn, self.watch)
        path = self.watch / "a.md"
        path.write_text("# One\nA\n", encoding="utf-8")
        self.assertEqual(process_document_event(self.conn, self._event(path, 50, flags=["item_created"]), root_path=self.watch)["item"]["change_kind"], "created")
        path.write_text("# Two\nB\n", encoding="utf-8")
        process_document_event(self.conn, self._event(path, 51), root_path=self.watch)
        replacement = self.watch / ".atomic-replacement"
        replacement.write_text("# Three\nC\n", encoding="utf-8")
        os.replace(replacement, path)
        process_document_event(self.conn, self._event(path, 52), root_path=self.watch)

        renamed = self.watch / "b.md"
        path.rename(renamed)
        process_document_event(self.conn, self._event(path, 53, flags=["item_removed", "item_renamed"]), root_path=self.watch)
        renamed_result = process_document_event(self.conn, self._event(renamed, 54, flags=["item_created", "item_renamed"]), root_path=self.watch)
        self.assertEqual(renamed_result["item"]["change_kind"], "renamed")
        renamed.unlink()
        deleted_result = process_document_event(self.conn, self._event(renamed, 55, flags=["item_removed"]), root_path=self.watch)
        self.assertEqual(deleted_result["item"]["change_kind"], "deleted")

        listed = self._all_changes()
        self.assertEqual(listed["totals"]["distinct_files"], 1)
        self.assertEqual({key: listed["totals"][key] for key in ("created", "modified", "renamed", "deleted")}, {"created": 1, "modified": 1, "renamed": 1, "deleted": 1})
        self.assertEqual(listed["items"][0]["version_count"], 3)
        self.assertEqual(listed["items"][0]["file_name_redacted"], "b.md")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM file_change_events WHERE change_kind='deleted'").fetchone()[0], 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(DISTINCT file_id) FROM file_change_versions"
            ).fetchone()[0],
            1,
            "rename/delete history must retain one stable file identity",
        )

    def test_cross_root_move_preserves_one_file_identity_and_new_project(self):
        second_root = self.root / "second-project"
        second_root.mkdir()
        configure_document_changes(
            self.conn,
            add_root=second_root,
            root_label="Second Project",
            runtime_root=self.root / "data",
        )
        original = self.watch / "move-between-projects.md"
        original.write_text("before\n", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        build_document_baseline(self.conn, second_root)

        original.write_text("first tracked version\n", encoding="utf-8")
        process_document_event(self.conn, self._event(original, 56), root_path=self.watch)
        moved = second_root / original.name
        original.rename(moved)
        process_document_event(
            self.conn,
            self._event(original, 57, flags=["item_removed", "item_renamed"]),
            root_path=self.watch,
        )
        result = process_document_event(
            self.conn,
            self._event(moved, 58, flags=["item_created", "item_renamed"]),
            root_path=second_root,
        )

        self.assertEqual(result["item"]["change_kind"], "renamed")
        listing = self._all_changes()
        self.assertEqual(listing["totals"]["distinct_files"], 1)
        self.assertEqual(listing["items"][0]["project_label"], "Second Project")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(DISTINCT file_id) FROM file_change_versions").fetchone()[0],
            1,
        )

    def test_rename_without_inode_evidence_stays_delete_and_create(self):
        original = self.watch / "old.md"
        original.write_text("same content", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        replacement = self.watch / "new.md"
        replacement.write_text(original.read_text(encoding="utf-8"), encoding="utf-8")
        original.unlink()
        process_document_event(self.conn, self._event(original, 60, flags=["item_removed"]), root_path=self.watch)
        created = process_document_event(self.conn, self._event(replacement, 61, flags=["item_created"]), root_path=self.watch)
        self.assertEqual(created["item"]["change_kind"], "created")
        totals = self._all_changes()["totals"]
        self.assertEqual(totals["renamed"], 0)
        self.assertEqual(totals["deleted"], 1)
        self.assertEqual(totals["created"], 1)

    def test_office_structures_report_paragraph_slide_sheet_and_cell_changes(self):
        docx = self.watch / "brief.docx"
        pptx = self.watch / "deck.pptx"
        xlsx = self.watch / "model.xlsx"
        self._write_zip(docx, {"word/document.xml": "<w:document xmlns:w='w'><w:body><w:p><w:r><w:t>Before paragraph</w:t></w:r></w:p></w:body></w:document>"})
        self._write_zip(pptx, {
            "ppt/slides/slide1.xml": "<p:sld xmlns:p='p' xmlns:a='a'><a:t>Opening</a:t><a:t>Before</a:t></p:sld>",
            "ppt/slides/slide2.xml": "<p:sld xmlns:p='p' xmlns:a='a'><a:t>Closing</a:t></p:sld>",
        })
        workbook = "<workbook xmlns='x'><sheets><sheet name='Pipeline'/></sheets></workbook>"
        shared_before = "<sst xmlns='x'><si><t>Before cell</t></si></sst>"
        sheet = "<worksheet xmlns='x'><sheetData><row r='1'><c r='A1' t='s'><v>0</v></c></row></sheetData></worksheet>"
        self._write_zip(xlsx, {"xl/workbook.xml": workbook, "xl/sharedStrings.xml": shared_before, "xl/worksheets/sheet1.xml": sheet})
        build_document_baseline(self.conn, self.watch)

        self._write_zip(docx, {"word/document.xml": "<w:document xmlns:w='w'><w:body><w:p><w:r><w:t>After paragraph</w:t></w:r></w:p></w:body></w:document>"})
        self._write_zip(pptx, {
            "ppt/slides/slide1.xml": "<p:sld xmlns:p='p' xmlns:a='a'><a:t>Opening</a:t><a:t>After</a:t></p:sld>",
            "ppt/slides/slide2.xml": "<p:sld xmlns:p='p' xmlns:a='a'><a:t>Closing</a:t></p:sld>",
        })
        self._write_zip(xlsx, {"xl/workbook.xml": workbook, "xl/sharedStrings.xml": "<sst xmlns='x'><si><t>After cell</t></si></sst>", "xl/worksheets/sheet1.xml": sheet})
        for event_id, path in enumerate((docx, pptx, xlsx), start=70):
            result = process_document_event(self.conn, self._event(path, event_id), root_path=self.watch)
            self.assertTrue(result["recorded"], path.name)
            self.assertEqual(result["item"]["detail_level"], "detailed")
        summaries = {item["file_type"]: item["change_summary_redacted"] for item in self._all_changes()["items"]}
        self.assertIn("段落", summaries["docx"])
        self.assertIn("第 1 页", summaries["pptx"])
        self.assertIn("工作表/单元格", summaries["xlsx"])
        self.assertIn("Pipeline", summaries["xlsx"])

    def test_office_relationship_order_uses_rid_and_handles_absolute_targets(self):
        from activity_mvp import document_changes as module

        presentation = b"""<p:presentation xmlns:p='urn:p' xmlns:r='http://schemas.openxmlformats.org/officeDocument/2006/relationships'>
        <p:sldIdLst><p:sldId id='256' r:id='rId2'/><p:sldId id='257' r:id='rId1'/></p:sldIdLst></p:presentation>"""
        presentation_rels = b"""<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'>
        <Relationship Id='rId1' Target='/ppt/slides/slide1.xml'/>
        <Relationship Id='rId2' Target='slides/slide2.xml'/>
        <Relationship Id='external' Target='https://example.invalid/x' TargetMode='External'/>
        </Relationships>"""
        self.assertEqual(
            module._ordered_package_members(
                presentation,
                presentation_rels,
                item_tag="sldId",
                base_directory="ppt",
            ),
            [("", "ppt/slides/slide2.xml"), ("", "ppt/slides/slide1.xml")],
        )

        workbook = b"""<workbook xmlns='urn:x' xmlns:r='http://schemas.openxmlformats.org/officeDocument/2006/relationships'>
        <sheets><sheet name='Second' sheetId='2' r:id='rId2'/><sheet name='First' sheetId='1' r:id='rId1'/></sheets></workbook>"""
        workbook_rels = b"""<Relationships xmlns='http://schemas.openxmlformats.org/package/2006/relationships'>
        <Relationship Id='rId1' Target='/xl/worksheets/sheet1.xml'/>
        <Relationship Id='rId2' Target='worksheets/sheet2.xml'/>
        </Relationships>"""
        self.assertEqual(
            module._ordered_package_members(
                workbook,
                workbook_rels,
                item_tag="sheet",
                base_directory="xl",
                label_attribute="name",
            ),
            [("Second", "xl/worksheets/sheet2.xml"), ("First", "xl/worksheets/sheet1.xml")],
        )

    def test_pdf_corrupt_office_and_oversize_files_degrade_to_metadata_only(self):
        pdf = self.watch / "notes.pdf"
        corrupt = self.watch / "broken.docx"
        large = self.watch / "large.pdf"
        pdf.write_bytes(b"%PDF-old-0000")
        corrupt.write_bytes(b"not-a-zip")
        with large.open("wb") as handle:
            handle.truncate(50 * 1024 * 1024 + 1)
        build_document_baseline(self.conn, self.watch)
        pdf.write_bytes(b"%PDF-new-0000")
        corrupt.write_bytes(b"still-bad")
        future = time.time() + 2
        os.utime(large, (future, future))
        for event_id, path in enumerate((pdf, corrupt, large), start=80):
            result = process_document_event(self.conn, self._event(path, event_id), root_path=self.watch)
            self.assertTrue(result["recorded"], path.name)
            self.assertEqual(result["item"]["detail_level"], "metadata_only")
            self.assertEqual(result["item"]["change_summary_redacted"], "检测到文件变化，但没有可安全概括的前后内容。")
        self.assertEqual(self._all_changes()["totals"]["metadata_only"], 3)

    def test_gap_is_sticky_until_reconcile_and_delete_range_resets_state(self):
        path = self.watch / "note.md"
        path.write_text("one", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        process_document_event(self.conn, {"path": str(path), "event_id": "11", "flags": ["must_scan_subdirs"]}, root_path=self.watch)
        path.write_text("two", encoding="utf-8")
        process_document_event(self.conn, {"path": str(path), "event_id": "12"}, root_path=self.watch)
        status = document_changes_status(self.conn)
        self.assertEqual(status["coverage"]["coverage_status"], "partial")
        build_document_baseline(self.conn, self.watch)
        self.assertTrue(document_changes_status(self.conn)["coverage"]["is_complete"])
        path.write_text("three", encoding="utf-8")
        process_document_event(self.conn, self._event(path, 13), root_path=self.watch)
        start = "2000-01-01T00:00:00+00:00"
        end = "2100-01-01T00:00:00+00:00"
        deletion = delete_range(self.conn, start, end)
        self.assertEqual(deletion["cascade"]["file_change_events"], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM file_watch_state").fetchone()[0], 0)
        self.assertEqual(document_changes_status(self.conn)["coverage"]["baseline_status"], "building")

    def test_checkpoint_is_monotonic_and_equal_id_paths_remain_idempotent(self):
        first = self.watch / "first.md"
        second = self.watch / "second.md"
        build_document_baseline(self.conn, self.watch)
        first.write_text("one", encoding="utf-8")
        second.write_text("two", encoding="utf-8")
        process_document_event(self.conn, self._event(first, 100, flags=["item_created"]), root_path=self.watch)
        process_document_event(self.conn, self._event(second, 100, flags=["item_created"]), root_path=self.watch)
        replay = process_document_event(self.conn, self._event(first, 99), root_path=self.watch)
        self.assertEqual(replay["reason"], "event_replay")
        checkpoint = self.conn.execute("SELECT last_event_id FROM file_watch_checkpoints").fetchone()[0]
        self.assertEqual(checkpoint, "100")
        self.assertEqual(self._all_changes()["totals"]["distinct_files"], 2)
        self.conn.close()
        self.conn = ensure_ready(self.db)
        restarted_replay = process_document_event(self.conn, self._event(first, 100), root_path=self.watch)
        self.assertFalse(restarted_replay["recorded"])
        self.assertEqual(self._all_changes()["totals"]["distinct_files"], 2)

    def test_helper_queue_overflow_and_unexpected_exit_mark_coverage_partial(self):
        build_document_baseline(self.conn, self.watch)
        from activity_mvp import document_changes as module

        class OverflowProcess(_ExitedProcess):
            def poll(self):
                return 0 if self.terminated else None

        self.conn.execute(
            "UPDATE file_watch_checkpoints SET last_event_id='0'"
        )
        self.conn.commit()
        original_baseline = module.build_document_baseline
        original_registry_match = module._registry_match
        diagnostics: list[dict] = []

        def slow_baseline(*args, **kwargs):
            time.sleep(0.15)
            return original_baseline(*args, **kwargs)

        def slow_registry_match(*args, **kwargs):
            time.sleep(0.2)
            return original_registry_match(*args, **kwargs)

        with mock.patch.object(module, "DOCUMENT_CHANGE_BACKPRESSURE_SECONDS", 0.0), mock.patch.object(
            module,
            "DOCUMENT_CHANGE_EVENT_QUEUE_MAXSIZE",
            2,
        ), mock.patch.object(
            module,
            "build_document_baseline",
            side_effect=slow_baseline,
        ), mock.patch.object(
            module,
            "_registry_match",
            side_effect=slow_registry_match,
        ):
            worker = start_document_change_worker(
                self.db,
                ["/safe/helper"],
                popen_factory=OverflowProcess,
                session_state_provider=lambda: {
                    "state": "unlocked",
                    "locked": False,
                    "on_console": True,
                    "capture_allowed": True,
                },
                _diagnostic_sink=diagnostics.append,
            )
        self._stop_worker(worker)
        self.assertTrue(worker["process"].process.terminated)
        self.conn.close()
        self.conn = ensure_ready(self.db)
        status = document_changes_status(self.conn)
        self.assertEqual(status["coverage"]["coverage_status"], "partial")
        self.assertGreater(status["coverage"]["event_gap_count"], 0)
        self.assertEqual(
            {
                row["last_event_id"]
                for row in self.conn.execute(
                    "SELECT last_event_id FROM file_watch_checkpoints"
                )
            },
            {None},
        )
        self.assertIn(
            "python_event_queue_overflow",
            {str(item.get("reason") or "") for item in diagnostics},
        )
        self.assertEqual(
            document_changes_status(self.conn)["coverage"]["pending_event_count"],
            0,
        )

        replacement, replacement_process = self._start_controlled_worker()
        try:
            self.assertTrue(replacement["started"])
            self.assertNotIn(
                "--since",
                replacement_process.command,
                "a lost-stream replacement retained the stale generation watermark",
            )
            recovered = self.watch / "after-overflow.md"
            recovered.write_text("fresh generation\n", encoding="utf-8")
            replacement_process.emit(
                self._event(
                    recovered,
                    2,
                    flags=["item_created", "item_is_file"],
                )
            )
            self.assertTrue(
                self._wait_until(
                    lambda: self.conn.execute(
                        "SELECT COUNT(*) FROM file_change_events"
                    ).fetchone()[0]
                    == 1,
                    timeout=9,
                ),
                "the fresh no-watermark generation did not record a real event",
            )
        finally:
            self._stop_worker(replacement)

    def test_failed_overflow_watermark_reset_blocks_replacement(self):
        build_document_baseline(self.conn, self.watch)
        from activity_mvp import document_changes as module

        class OverflowProcess(_ExitedProcess):
            def poll(self):
                return 0 if self.terminated else None

        self.conn.execute(
            "UPDATE file_watch_checkpoints SET last_event_id='999'"
        )
        self.conn.commit()
        original_record_gap = module._record_gap
        original_registry_match = module._registry_match

        def fail_terminal_reset(*args, **kwargs):
            if kwargs.get("reset_event_id") is True:
                raise sqlite3.OperationalError("simulated terminal reset failure")
            return original_record_gap(*args, **kwargs)

        with mock.patch.object(
            module,
            "DOCUMENT_CHANGE_EVENT_QUEUE_MAXSIZE",
            2,
        ), mock.patch.object(
            module,
            "_registry_match",
            side_effect=lambda *args, **kwargs: (
                time.sleep(0.02) or original_registry_match(*args, **kwargs)
            ),
        ), mock.patch.object(
            module,
            "_record_gap",
            side_effect=fail_terminal_reset,
        ):
            worker = start_document_change_worker(
                self.db,
                ["/safe/helper"],
                popen_factory=OverflowProcess,
                session_state_provider=lambda: {
                    "state": "unlocked",
                    "locked": False,
                    "on_console": True,
                    "capture_allowed": True,
                },
            )

        try:
            self.assertFalse(worker["started"])
            self.assertEqual(worker["reason"], "stream_reset_failed")
            self.assertTrue(worker["process"].restart_cooldown_required)
            self.assertTrue(worker["process"].restart_blocked)
            self.assertEqual(
                {
                    row["last_event_id"]
                    for row in self.conn.execute(
                        "SELECT last_event_id FROM file_watch_checkpoints"
                    )
                },
                {"999"},
            )
            # Survives a daemon restart: startup reconcile keeps watermarks.
            self.assertIn(
                document_stream_reset_required_reason(self.root / "data"),
                {"helper_barrier_failed", "python_event_queue_overflow"},
            )
        finally:
            self._stop_worker(worker)

    def _retained_watermarks(self) -> set:
        return {
            row["last_event_id"]
            for row in self.conn.execute(
                "SELECT checkpoints.last_event_id FROM file_watch_checkpoints AS checkpoints "
                "JOIN file_watch_roots AS roots ON roots.root_id=checkpoints.root_id "
                "WHERE roots.enabled=1"
            )
        }

    def test_lifecycle_reset_retry_clears_watermarks_and_verifies(self):
        # 2026-10-04 12:10: the in-worker reset lost its write lock
        # (database_busy) and tracking stayed off until the daemon restarted.
        from activity_mvp.document_changes import reset_document_change_stream_watermarks

        build_document_baseline(self.conn, self.watch)
        self.conn.execute("UPDATE file_watch_checkpoints SET last_event_id='999'")
        self.conn.commit()
        gaps_before = self.conn.execute(
            "SELECT COUNT(*) FROM file_watch_coverage_gaps"
        ).fetchone()[0]

        self.assertTrue(
            reset_document_change_stream_watermarks(
                self.db, reason_code="fsevents_control_event"
            )
        )

        self.assertEqual(self._retained_watermarks(), {None})
        self.assertEqual(
            {row[0] for row in self.conn.execute(
                "SELECT baseline_status FROM file_watch_roots WHERE enabled=1"
            )},
            {"partial"},
        )
        self.assertEqual(
            {row[0] for row in self.conn.execute(
                "SELECT coverage_status FROM file_watch_checkpoints"
            )},
            {"partial"},
        )
        gap = self.conn.execute(
            "SELECT reason_code,affects_attribution FROM file_watch_coverage_gaps "
            "ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM file_watch_coverage_gaps").fetchone()[0],
            gaps_before + 1,
        )
        self.assertEqual((gap["reason_code"], gap["affects_attribution"]), ("fsevents_control_event", 1))

    def test_lifecycle_reset_retry_under_contention_changes_nothing(self):
        from activity_mvp.document_changes import reset_document_change_stream_watermarks

        build_document_baseline(self.conn, self.watch)
        self.conn.execute("UPDATE file_watch_checkpoints SET last_event_id='999'")
        self.conn.commit()
        holder = sqlite3.connect(self.db)
        holder.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(sqlite3.OperationalError):
                reset_document_change_stream_watermarks(
                    self.db,
                    reason_code="fsevents_control_event",
                    busy_timeout_seconds=0.1,
                )
        finally:
            holder.rollback()
            holder.close()
        self.assertEqual(self._retained_watermarks(), {"999"})

    def test_fresh_stream_start_refuses_a_retained_watermark(self):
        build_document_baseline(self.conn, self.watch)
        self.conn.execute("UPDATE file_watch_checkpoints SET last_event_id='999'")
        self.conn.commit()
        spawned: list = []

        def factory(command, **kwargs):
            spawned.append(command)
            return _ControlledProcess(command, **kwargs)

        result = start_document_change_worker(
            self.db,
            ["/safe/helper"],
            popen_factory=factory,
            session_state_provider=lambda: {
                "state": "unlocked",
                "locked": False,
                "on_console": True,
                "capture_allowed": True,
            },
            require_fresh_stream=True,
        )

        self.assertEqual(result["reason"], "stream_watermark_retained")
        self.assertFalse(result["started"])
        self.assertEqual(spawned, [])

    def test_failed_reset_recovers_to_fresh_stream_after_lifecycle_retry(self):
        # End to end: the generation that could not clear its watermark joins,
        # the lifecycle retry clears and verifies, and the replacement helper
        # is spawned without the stale --since cursor.
        self.test_failed_overflow_watermark_reset_blocks_replacement()
        from activity_mvp.document_changes import reset_document_change_stream_watermarks

        self.assertTrue(
            reset_document_change_stream_watermarks(
                self.db, reason_code="python_event_queue_overflow"
            )
        )
        holder: dict = {}

        def factory(command, **kwargs):
            holder["process"] = _ControlledProcess(command, **kwargs)
            return holder["process"]

        worker = start_document_change_worker(
            self.db,
            ["/safe/helper"],
            popen_factory=factory,
            session_state_provider=lambda: {
                "state": "unlocked",
                "locked": False,
                "on_console": True,
                "capture_allowed": True,
            },
            require_fresh_stream=True,
        )
        try:
            self.assertTrue(worker["started"])
            self.assertNotIn("--since", holder["process"].command)
            self.assertIsNone(document_stream_reset_required_reason(self.root / "data"))
        finally:
            self._stop_worker(worker)

    def test_stream_reset_marker_is_private_and_fails_closed(self):
        from activity_mvp.document_changes import (
            STREAM_RESET_MARKER_FILENAME,
            clear_document_stream_reset_required,
            mark_document_stream_reset_required,
        )

        runtime = self.root / "data"
        self.assertIsNone(document_stream_reset_required_reason(runtime))
        mark_document_stream_reset_required(runtime, "fsevents_control_event")
        marker = runtime / STREAM_RESET_MARKER_FILENAME
        self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
        self.assertEqual(marker.read_text(), "fsevents_control_event")
        self.assertEqual(
            document_stream_reset_required_reason(runtime), "fsevents_control_event"
        )
        mark_document_stream_reset_required(runtime, str(self.watch / "secret.md"))
        self.assertEqual(marker.read_text(), "unknown_fatal_transition")
        marker.write_text(str(self.watch / "private.md"))
        self.assertEqual(
            document_stream_reset_required_reason(runtime), "unknown_fatal_transition"
        )
        clear_document_stream_reset_required(runtime)
        self.assertIsNone(document_stream_reset_required_reason(runtime))

    def test_successful_in_worker_stream_reset_leaves_no_marker(self):
        build_document_baseline(self.conn, self.watch)
        worker, controlled = self._start_controlled_worker()
        try:
            controlled.emit(
                {
                    "path": str(self.watch),
                    "event_id": "5",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "flags": ["kernel_dropped", "must_scan_subdirs"],
                }
            )
            self.assertTrue(self._wait_until(lambda: controlled.terminated, timeout=4))
            self._stop_worker(worker)
            self.assertFalse(worker["process"].restart_blocked)
            self.assertEqual(self._retained_watermarks(), {None})
            self.assertIsNone(document_stream_reset_required_reason(self.root / "data"))
        finally:
            self._stop_worker(worker)

    def test_epoch_change_cannot_disguise_incomplete_stream_reset(self):
        tracked = self.watch / "epoch-reset-race.md"
        tracked.write_text("baseline\n", encoding="utf-8")
        scan_waiting = threading.Event()
        release_scan = threading.Event()
        holder: dict[str, object] = {}

        def factory(command, **kwargs):
            process = _ControlledProcess(command, **kwargs)
            holder["process"] = process
            return process

        def candidates(_root, *, stop_check=None):
            scan_waiting.set()
            release_scan.wait(timeout=8)
            if stop_check is None or not bool(stop_check()):
                yield tracked

        def launch() -> None:
            try:
                holder["worker"] = start_document_change_worker(
                    self.db,
                    ["/safe/helper"],
                    popen_factory=factory,
                    session_state_provider=lambda: {
                        "state": "unlocked",
                        "locked": False,
                        "on_console": True,
                        "capture_allowed": True,
                    },
                )
            except BaseException as exc:  # pragma: no cover - assertion reports it
                holder["error"] = exc

        launch_thread = threading.Thread(target=launch)
        worker: dict | None = None
        try:
            with mock.patch(
                "activity_mvp.document_changes._iter_safe_files",
                side_effect=candidates,
            ):
                launch_thread.start()
                self.assertTrue(scan_waiting.wait(timeout=4))
                controlled = holder["process"]
                assert isinstance(controlled, _ControlledProcess)
                controlled.emit(
                    {
                        "path": str(self.watch),
                        "event_id": "2",
                        "observed_at": datetime.now(timezone.utc).isoformat(),
                        "flags": ["kernel_dropped", "must_scan_subdirs"],
                    }
                )
                self.assertTrue(
                    self._wait_until(lambda: controlled.terminated, timeout=4)
                )

                added = self.root / "added-during-reset"
                added.mkdir()
                configure_document_changes(
                    self.conn,
                    add_root=added,
                    root_label="Added",
                    runtime_root=self.root / "data",
                )
                release_scan.set()
                launch_thread.join(timeout=12)

            self.assertFalse(launch_thread.is_alive())
            self.assertNotIn("error", holder)
            worker = holder["worker"]
            assert isinstance(worker, dict)
            self.assertFalse(worker["started"])
            self.assertEqual(worker["reason"], "stream_reset_failed")
            self.assertTrue(worker["process"].restart_blocked)
            self.assertGreater(
                self.conn.execute(
                    "SELECT COUNT(*) FROM file_watch_checkpoints AS checkpoints "
                    "JOIN file_watch_roots AS roots ON roots.root_id=checkpoints.root_id "
                    "WHERE roots.enabled=1 AND checkpoints.last_event_id IS NOT NULL"
                ).fetchone()[0],
                0,
            )
        finally:
            release_scan.set()
            launch_thread.join(timeout=12)
            if isinstance(worker, dict):
                self._stop_worker(worker)

    def test_helper_stop_during_startup_baseline_aborts_remaining_candidates(self):
        paths = []
        for index in range(16):
            path = self.watch / f"baseline-{index}.md"
            path.write_text(f"version {index}\n", encoding="utf-8")
            paths.append(path)

        consumed: list[Path] = []
        stop_checks: list[object] = []
        scan_waiting = threading.Event()
        release_scan = threading.Event()
        holder: dict[str, object] = {}

        def factory(command, **kwargs):
            process = _ControlledProcess(command, **kwargs)
            holder["process"] = process
            return process

        def candidates(_root, *, stop_check=None):
            stop_checks.append(stop_check)
            for index, path in enumerate(paths):
                consumed.append(path)
                if index == 1:
                    scan_waiting.set()
                    release_scan.wait(timeout=5)
                yield path

        def launch():
            try:
                holder["worker"] = start_document_change_worker(
                    self.db,
                    ["/safe/helper"],
                    popen_factory=factory,
                    session_state_provider=lambda: {
                        "state": "unlocked",
                        "locked": False,
                        "on_console": True,
                        "capture_allowed": True,
                    },
                )
            except Exception as exc:  # pragma: no cover - assertion reports it
                holder["error"] = exc

        thread = threading.Thread(target=launch)
        try:
            with mock.patch("activity_mvp.document_changes._iter_safe_files", side_effect=candidates):
                thread.start()
                self.assertTrue(scan_waiting.wait(timeout=4))
                controlled = holder["process"]
                assert isinstance(controlled, _ControlledProcess)
                controlled.emit({
                    "path": str(self.watch),
                    "event_id": "2",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "flags": ["kernel_dropped"],
                })
                self.assertTrue(self._wait_until(lambda: controlled.terminated, timeout=3))
                release_scan.set()
                thread.join(timeout=8)
        finally:
            release_scan.set()
            thread.join(timeout=8)

        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", holder)
        worker = holder["worker"]
        assert isinstance(worker, dict)
        try:
            self.assertFalse(worker["started"])
            self.assertEqual(worker["reason"], "authorization_changed_or_reconcile_restart")
            self.assertEqual(len(stop_checks), 1)
            self.assertTrue(callable(stop_checks[0]))
            self.assertLessEqual(len(consumed), 2)
            self.assertLess(len(consumed), len(paths))
            self.assertLess(
                self.conn.execute(
                    "SELECT COUNT(*) FROM file_watch_state WHERE present=1"
                ).fetchone()[0],
                len(paths),
            )
            coverage = document_changes_status(self.conn)["coverage"]
            self.assertEqual(coverage["coverage_status"], "partial")
            self.assertGreater(coverage["event_gap_count"], 0)
        finally:
            self._stop_worker(worker)

    def test_baseline_event_consumer_does_not_wait_for_root_scan_lock(self):
        from activity_mvp import document_changes as module

        tracked = self.watch / "baseline-live-event.md"
        tracked.write_text("before\n", encoding="utf-8")
        scan_waiting = threading.Event()
        release_scan = threading.Event()
        holder: dict[str, object] = {}
        diagnostics: list[dict] = []
        candidate_calls = 0

        def factory(command, **kwargs):
            process = _ControlledProcess(command, **kwargs)
            holder["process"] = process
            return process

        def candidates(_root, *, stop_check=None):
            nonlocal candidate_calls
            candidate_calls += 1
            if candidate_calls == 1:
                scan_waiting.set()
                release_scan.wait(timeout=5)
            if stop_check is None or not bool(stop_check()):
                yield tracked

        def launch() -> None:
            try:
                holder["worker"] = start_document_change_worker(
                    self.db,
                    ["/safe/helper"],
                    popen_factory=factory,
                    session_state_provider=lambda: {
                        "state": "unlocked",
                        "locked": False,
                        "on_console": True,
                        "capture_allowed": True,
                    },
                    _diagnostic_sink=diagnostics.append,
                )
            except BaseException as exc:  # pragma: no cover - assertion reports it
                holder["error"] = exc

        launch_thread = threading.Thread(target=launch)
        worker: dict | None = None
        try:
            with mock.patch.object(
                module,
                "DOCUMENT_CHANGE_EVENT_QUEUE_MAXSIZE",
                4,
            ), mock.patch.object(
                module,
                "_iter_safe_files",
                side_effect=candidates,
            ), mock.patch.object(
                module,
                "_registry_match",
                side_effect=AssertionError(
                    "baseline-time ordinary events must not touch filesystem roots"
                ),
            ):
                launch_thread.start()
                self.assertTrue(scan_waiting.wait(timeout=4))
                controlled = holder["process"]
                assert isinstance(controlled, _ControlledProcess)
                for event_id in range(2, 22):
                    controlled.emit(
                        self._event(
                            tracked,
                            event_id,
                            flags=["item_modified"],
                        )
                    )
                    time.sleep(0.01)
                time.sleep(0.2)
                self.assertFalse(
                    controlled.terminated,
                    "baseline-time events were blocked until the queue overflowed",
                )
                self.assertNotIn(
                    "python_event_queue_overflow",
                    {str(item.get("reason") or "") for item in diagnostics},
                )
                release_scan.set()
                launch_thread.join(timeout=12)
            self.assertFalse(launch_thread.is_alive())
            self.assertNotIn("error", holder)
            worker = holder["worker"]
            assert isinstance(worker, dict)
            self.assertTrue(worker["started"])
            self.assertGreaterEqual(candidate_calls, 2)
        finally:
            release_scan.set()
            launch_thread.join(timeout=12)
            if isinstance(worker, dict):
                self._stop_worker(worker)

    def test_post_barrier_file_event_runs_after_baseline_cutover(self):
        tracked = self.watch / "post-barrier.md"
        tracked.write_text("before\n", encoding="utf-8")
        holder: dict[str, _ControlledProcess] = {}

        class PostBarrierFileProcess(_ControlledProcess):
            def request_barrier(inner_self, token: str) -> None:
                tracked.write_text("after\n", encoding="utf-8")
                super().request_barrier(token)
                inner_self.emit(
                    self._event(
                        tracked,
                        2,
                        flags=["item_modified", "item_is_file"],
                        observed_at=(
                            datetime.now(timezone.utc) + timedelta(seconds=10)
                        ).isoformat(),
                    )
                )

        def factory(command, **kwargs):
            process = PostBarrierFileProcess(command, **kwargs)
            holder["process"] = process
            return process

        worker = start_document_change_worker(
            self.db,
            ["/safe/helper"],
            popen_factory=factory,
            session_state_provider=lambda: {
                "state": "unlocked",
                "locked": False,
                "on_console": True,
                "capture_allowed": True,
            },
        )
        try:
            self.assertTrue(worker["started"])
            self.assertTrue(
                self._wait_until(
                    lambda: self.conn.execute(
                        "SELECT COUNT(*) FROM file_change_events"
                    ).fetchone()[0]
                    == 1,
                    timeout=9,
                ),
                "the first post-barrier file event was consumed as baseline dirtiness",
            )
        finally:
            self._stop_worker(worker)

    def test_post_barrier_directory_event_is_retained_as_benign_churn(self):
        (self.watch / "tracked.md").write_text("before\n", encoding="utf-8")
        directory = self.watch / "post-barrier-folder"

        class PostBarrierDirectoryProcess(_ControlledProcess):
            def request_barrier(inner_self, token: str) -> None:
                directory.mkdir(exist_ok=True)
                super().request_barrier(token)
                inner_self.emit(
                    self._event(
                        directory,
                        2,
                        flags=["item_created", "item_is_dir"],
                    )
                )

        worker = start_document_change_worker(
            self.db,
            ["/safe/helper"],
            popen_factory=PostBarrierDirectoryProcess,
            session_state_provider=lambda: {
                "state": "unlocked",
                "locked": False,
                "on_console": True,
                "capture_allowed": True,
            },
        )
        try:
            self.assertTrue(worker["started"])
            self.assertTrue(
                self._wait_until(
                    lambda: (
                        (coverage := document_changes_status(self.conn)["coverage"])["benign_event_count"] > 0
                        and coverage["is_complete"]
                    ),
                    timeout=5,
                ),
                "finish lost a directory event that arrived after its barrier",
            )
            coverage = document_changes_status(self.conn)["coverage"]
            self.assertTrue(coverage["is_complete"])
            self.assertEqual(coverage["event_gap_count"], 0)
        finally:
            self._stop_worker(worker)

    def test_baseline_overlap_is_absorbed_before_the_coverage_boundary(self):
        path = self.watch / "overlap.md"
        path.write_text("already changed", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        state_time = self.conn.execute("SELECT last_seen_at FROM file_watch_state").fetchone()[0]
        result = process_document_event(
            self.conn,
            self._event(path, 110, flags=["item_modified"], observed_at=state_time),
            root_path=self.watch,
        )
        self.assertEqual(result["reason"], "same_content_event")
        coverage = document_changes_status(self.conn)["coverage"]
        self.assertEqual(coverage["coverage_status"], "complete")
        self.assertEqual(coverage["event_gap_count"], 0)
        self.assertGreaterEqual(coverage["benign_event_count"], 1)
        gap = self.conn.execute(
            "SELECT reason_code,affects_attribution FROM file_watch_coverage_gaps "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(tuple(gap), ("same_content_event", 0))
        self.assertEqual(self._all_changes()["totals"]["distinct_files"], 0)

    def test_duplicate_same_second_content_event_does_not_downgrade_root(self):
        path = self.watch / "duplicate.md"
        path.write_text("stable", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        baseline_completed_at = self.conn.execute(
            "SELECT baseline_completed_at FROM file_watch_roots"
        ).fetchone()[0]
        observed_at = (
            datetime.fromisoformat(str(baseline_completed_at)) + timedelta(seconds=1)
        ).isoformat()

        first = process_document_event(
            self.conn,
            self._event(path, 111, flags=["item_modified"], observed_at=observed_at),
            root_path=self.watch,
        )
        second = process_document_event(
            self.conn,
            self._event(path, 112, flags=["item_modified"], observed_at=observed_at),
            root_path=self.watch,
        )

        self.assertEqual(first["reason"], "metadata_only_no_content_change")
        self.assertEqual(second["reason"], "same_content_event")
        coverage = document_changes_status(self.conn)["coverage"]
        self.assertTrue(coverage["is_complete"])
        self.assertEqual(coverage["event_gap_count"], 0)
        self.assertEqual(self._all_changes()["totals"]["distinct_files"], 0)

    def test_pause_forgets_baseline_ignores_interval_and_does_not_backfill(self):
        path = self.watch / "paused.md"
        path.write_text("before", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        pause(self.conn, mode="manual")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM file_watch_state").fetchone()[0], 0)
        self.assertFalse(document_changes_status(self.conn)["coverage"]["is_complete"])
        path.write_text("during", encoding="utf-8")
        ignored = process_document_event(self.conn, self._event(path, 120), root_path=self.watch)
        self.assertFalse(ignored["recorded"])
        self.assertEqual(self._all_changes()["totals"]["distinct_files"], 0)
        resume(self.conn)
        self.conn.execute("UPDATE daemon_state SET value='false' WHERE key='locked'")
        self.conn.commit()
        build_document_baseline(self.conn, self.watch)
        no_backfill = process_document_event(self.conn, self._event(path, 121), root_path=self.watch)
        self.assertFalse(no_backfill["recorded"])
        path.write_text("after!", encoding="utf-8")
        recorded = process_document_event(self.conn, self._event(path, 122), root_path=self.watch)
        self.assertTrue(recorded["recorded"])
        self.assertEqual(recorded["item"]["change_kind"], "modified")

    def test_locked_state_defers_content_read_until_unlock(self):
        path = self.watch / "locked.md"
        path.write_text("before", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        self.conn.execute(
            "INSERT INTO daemon_state(key,value,updated_at) VALUES('locked','true',?) "
            "ON CONFLICT(key) DO UPDATE SET value='true',updated_at=excluded.updated_at",
            (datetime.now(timezone.utc).isoformat(),),
        )
        self.conn.commit()
        path.write_text("after", encoding="utf-8")
        deferred = process_document_event(self.conn, self._event(path, 125), root_path=self.watch)
        self.assertEqual(deferred["reason"], "locked_deferred")
        self.assertEqual(self._all_changes()["totals"]["distinct_files"], 0)
        self.conn.execute("UPDATE daemon_state SET value='false' WHERE key='locked'")
        self.conn.commit()
        recorded = process_document_event(self.conn, self._event(path, 125), root_path=self.watch)
        self.assertTrue(recorded["recorded"])
        self.assertEqual(recorded["item"]["change_kind"], "modified")

    def test_document_session_policy_accepts_only_verified_local_console_states(self):
        allowed = (
            {
                "state": "unlocked",
                "locked": False,
                "on_console": True,
                "capture_allowed": True,
            },
            {
                "state": "locked",
                "locked": True,
                "on_console": True,
                "capture_allowed": False,
            },
        )
        denied = (
            {},
            {
                "state": "unknown",
                "locked": True,
                "on_console": True,
                "capture_allowed": False,
            },
            {
                "state": "locked",
                "locked": True,
                "on_console": False,
                "capture_allowed": False,
            },
            {
                "state": "locked",
                "locked": False,
                "on_console": True,
                "capture_allowed": False,
            },
            {
                "state": "locked",
                "locked": True,
                "on_console": True,
                "capture_allowed": True,
            },
            {
                "state": "unlocked",
                "locked": False,
                "on_console": True,
                "capture_allowed": False,
            },
            {
                "state": "locked",
                "locked": True,
                "on_console": True,
                "capture_allowed": False,
                "error": "session_probe_failed",
            },
        )
        self.assertTrue(all(_document_change_session_allows_content(item) for item in allowed))
        self.assertFalse(any(_document_change_session_allows_content(item) for item in denied))

    def test_disable_and_remove_prevent_stale_events_and_root_resurrection(self):
        path = self.watch / "disabled.md"
        path.write_text("before", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        configure_document_changes(self.conn, enabled=False, runtime_root=self.root / "data")
        path.write_text("during disabled", encoding="utf-8")
        disabled = process_document_event(self.conn, self._event(path, 130), root_path=self.watch)
        self.assertEqual(disabled["reason"], "tracking_disabled")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM file_watch_state").fetchone()[0], 0)
        configure_document_changes(self.conn, enabled=True, runtime_root=self.root / "data")
        root_ref = document_changes_status(self.conn)["roots"][0]["root_ref"]
        configure_document_changes(self.conn, remove_root=root_ref, runtime_root=self.root / "data")
        stale_baseline = build_document_baseline(self.conn, self.watch)
        self.assertEqual(stale_baseline["baseline_status"], "unconfigured")
        self.assertEqual(document_changes_status(self.conn)["coverage"]["root_count"], 0)
        stale_event = process_document_event(self.conn, self._event(path, 131), root_path=self.watch)
        self.assertEqual(stale_event["reason"], "root_not_configured")

    def test_baseline_read_failure_marks_partial(self):
        path = self.watch / "unreadable.md"
        path.write_text("content", encoding="utf-8")
        with mock.patch("activity_mvp.document_changes._snapshot", side_effect=OSError("read failed")):
            result = build_document_baseline(self.conn, self.watch, paths=[path])
        self.assertEqual(result["baseline_status"], "partial")
        self.assertEqual(result["files_failed"], 1)
        status = document_changes_status(self.conn)
        self.assertFalse(status["coverage"]["is_complete"])
        self.assertGreaterEqual(status["coverage"]["event_gap_count"], 1)

    def test_baseline_snapshot_failures_keep_private_details_out_of_diagnostics(self):
        path = self.watch / "private-client-secret.md"
        path.write_text("private content sentinel", encoding="utf-8")
        cases = [
            (FileNotFoundError(2, "private content sentinel", str(path)), "file_missing"),
            (PermissionError(13, "private content sentinel", str(path)), "permission_denied"),
            (PermissionError("content read deferred while hashing"), "session_content_denied"),
            (OSError("document changed while fingerprinting"), "content_changed"),
            (OSError(5, "private content sentinel", str(path)), "snapshot_io_error"),
            (ValueError("private content sentinel " + str(path)), "invalid_snapshot"),
            (RuntimeError("private content sentinel " + str(path)), "snapshot_runtime_error"),
            (zipfile.BadZipFile("private content sentinel " + str(path)), "invalid_package"),
        ]
        for error, reason in cases:
            with self.subTest(reason=reason):
                metrics = {}
                def record(name, value=1):
                    metrics[name] = metrics.get(name, 0) + value
                token = document_changes_module._DOCUMENT_BASELINE_METRIC_RECORDER.set(record)
                try:
                    with mock.patch.object(document_changes_module, "_snapshot", side_effect=error):
                        result = build_document_baseline(self.conn, self.watch, paths=[path])
                finally:
                    document_changes_module._DOCUMENT_BASELINE_METRIC_RECORDER.reset(token)
                self.assertEqual(result["files_failed"], 1)
                self.assertEqual(result["baseline_status"], "partial")
                self.assertEqual(metrics.get("failure_" + reason), 1)
                serialized = json.dumps(metrics)
                self.assertNotIn(str(path), serialized)
                self.assertNotIn("private content sentinel", serialized)

    def test_baseline_worker_reports_failure_reason_and_preserves_incomplete_coverage(self):
        diagnostics = []
        private_path = self.watch / "private-worker-secret.md"
        private_path.write_text("private worker sentinel", encoding="utf-8")
        original = document_changes_module._snapshot
        def fail_one(conn, root, path, **kwargs):
            if path.resolve() == private_path.resolve():
                raise PermissionError(13, "private worker sentinel", str(path))
            return original(conn, root, path, **kwargs)
        with mock.patch.object(document_changes_module, "_snapshot", side_effect=fail_one):
            worker, controlled = self._start_controlled_worker(diagnostic_sink=diagnostics.append)
        try:
            self.assertTrue(worker["started"])
            self.assertFalse(controlled.terminated)
            self.assertFalse(document_changes_status(self.conn)["coverage"]["is_complete"])
        finally:
            self._stop_worker(worker)
        progress = [x for x in diagnostics if x.get("event") == "document_change_baseline_progress"]
        self.assertTrue(progress)
        self.assertEqual(progress[-1]["scan_issue_counts"].get("permission_denied"), 2)
        for item in progress:
            self.assertEqual(item["pid"], os.getpid())
            self.assertLess(abs((datetime.now(timezone.utc) - datetime.fromisoformat(item["observed_at"])).total_seconds()), 30)
            self.assertLessEqual(set(item["scan_issue_counts"]), set(document_changes_module._DOCUMENT_BASELINE_FAILURE_REASONS))
        serialized = json.dumps(progress)
        self.assertNotIn(str(private_path), serialized)
        self.assertNotIn("private worker sentinel", serialized)

    @contextlib.contextmanager
    def _dataless_fixture(self, path, *, placeholder=True, always_fail=False, hash_only=False, restore_fails=False):
        inode = path.stat().st_ino
        native_read, native_stat = os.read, os.fstat
        state = {"policy": 1, "set": [], "failed_reads": 0, "successful_reads": 0}
        def metadata(descriptor):
            value = native_stat(descriptor)
            if value.st_ino != inode:
                return value
            return SimpleNamespace(**{name: getattr(value, name) for name in dir(value) if name.startswith("st_")},
                                   **({} if hasattr(value, "st_flags") else {"st_flags": 0}))
        def cloud_metadata(descriptor):
            value = metadata(descriptor)
            if value.st_ino == inode:
                value.st_flags = 0x40000000 if placeholder else 0
            return value
        def read(descriptor, size):
            if native_stat(descriptor).st_ino == inode:
                in_hash = any(frame.function == "_file_content_hmac" for frame in inspect.stack())
                if (not hash_only or in_hash) and (always_fail or state["policy"] != 2):
                    state["failed_reads"] += 1
                    raise OSError(errno.EDEADLK, "synthetic cloud placeholder read")
                state["successful_reads"] += 1
            return native_read(descriptor, size)
        def get(kind, scope):
            self.assertEqual((kind, scope), (3, 1))
            return state["policy"]
        def set_policy(kind, scope, value):
            self.assertEqual((kind, scope), (3, 1))
            state["set"].append(value)
            if restore_fails and value == 1:
                return -1
            state["policy"] = value
            return 0
        with mock.patch.object(document_changes_module.os, "fstat", side_effect=cloud_metadata), \
             mock.patch.object(document_changes_module.os, "read", side_effect=read), \
             mock.patch.object(document_changes_module, "_dataless_thread_policy", return_value=(get, set_policy), create=True):
            yield state

    def test_dataless_snapshot_recovers_text_and_hash_then_restores_thread(self):
        path = self.watch / "cloud.md"; path.write_text("same original text")
        with self._dataless_fixture(path) as state:
            value = document_changes_module._snapshot(self.conn, self.watch, path, content_allowed_check=lambda: True)
        self.assertEqual(value["detail_level"], "detailed")
        self.assertEqual(state["set"], [2, 1]); self.assertEqual(state["policy"], 1)
        self.assertEqual(state["failed_reads"], 1); self.assertGreaterEqual(state["successful_reads"], 4)
        self.assertIsNone(document_changes_module._DOCUMENT_DATALESS_READ_RECOVERY.get())

    def test_dataless_hash_failure_alone_recovers_once(self):
        path = self.watch / "cloud.md"; path.write_text("same original text")
        with self._dataless_fixture(path, hash_only=True) as state:
            value = document_changes_module._snapshot(self.conn, self.watch, path, content_allowed_check=lambda: True)
        self.assertTrue(value["content_hmac"]); self.assertEqual(state["set"], [2, 1])
        self.assertEqual(state["failed_reads"], 1)

    def test_dataless_persistent_read_failure_is_not_retried_again(self):
        path = self.watch / "cloud.md"; path.write_text("same original text")
        later = self.watch / "later.md"; later.write_text("normal later text")
        token = document_changes_module._DOCUMENT_BASELINE_LIVE_SESSION_PROBE.set(lambda: True)
        try:
            with self._dataless_fixture(path, always_fail=True) as state:
                result = build_document_baseline(self.conn, self.watch, paths=[path, later], content_allowed_check=lambda: True)
        finally:
            document_changes_module._DOCUMENT_BASELINE_LIVE_SESSION_PROBE.reset(token)
        self.assertEqual(result["files_failed"], 1); self.assertEqual(result["files_indexed"], 1)
        self.assertEqual(state["failed_reads"], 2); self.assertEqual(state["set"], [2, 1])

    def test_dataless_revocation_before_retry_reads_no_more(self):
        path = self.watch / "cloud.md"; path.write_text("same original text")
        with self._dataless_fixture(path) as state:
            with self.assertRaises(PermissionError):
                document_changes_module._snapshot(self.conn, self.watch, path, content_allowed_check=lambda: state["failed_reads"] == 0)
        self.assertEqual(state["set"], []); self.assertEqual(state["failed_reads"], 1)
        self.assertEqual(state["successful_reads"], 0)

    def test_dataless_revocation_after_enable_restores_without_retry_read(self):
        path = self.watch / "cloud.md"; path.write_text("same original text")
        with self._dataless_fixture(path) as state:
            with self.assertRaises(PermissionError):
                document_changes_module._snapshot(self.conn, self.watch, path, content_allowed_check=lambda: state["policy"] != 2)
        self.assertEqual(state["set"], [2, 1]); self.assertEqual(state["policy"], 1)
        self.assertEqual(state["failed_reads"], 1); self.assertEqual(state["successful_reads"], 0)

    def test_dataless_setter_failure_still_restores_exact_original(self):
        path = self.watch / "cloud.md"; path.write_text("same original text")
        with self._dataless_fixture(path) as state:
            getter, setter = document_changes_module._dataless_thread_policy()
            def failed_set(kind, scope, value):
                result = setter(kind, scope, value)
                return -1 if value == 2 else result
            with mock.patch.object(document_changes_module, "_dataless_thread_policy", return_value=(getter, failed_set)):
                with self.assertRaises(OSError):
                    document_changes_module._snapshot(self.conn, self.watch, path, content_allowed_check=lambda: True)
        self.assertEqual(state["set"], [2, 1]); self.assertEqual(state["policy"], 1)
        self.assertEqual(state["failed_reads"], 1); self.assertEqual(state["successful_reads"], 0)

    def test_dataless_enable_readback_exception_still_restores(self):
        path = self.watch / "cloud.md"; path.write_text("same original text")
        with self._dataless_fixture(path) as state:
            getter, setter = document_changes_module._dataless_thread_policy()
            calls = 0
            def failed_get(kind, scope):
                nonlocal calls
                calls += 1
                if calls == 2: raise RuntimeError("synthetic readback unavailable")
                return getter(kind, scope)
            with mock.patch.object(document_changes_module, "_dataless_thread_policy", return_value=(failed_get, setter)):
                with self.assertRaisesRegex(RuntimeError, "readback unavailable"):
                    document_changes_module._snapshot(self.conn, self.watch, path, content_allowed_check=lambda: True)
        self.assertEqual(state["set"], [2, 1]); self.assertEqual(state["policy"], 1)
        self.assertEqual(calls, 3)

    def test_dataless_nonplaceholder_and_missing_gate_do_not_enable(self):
        path = self.watch / "cloud.md"; path.write_text("same original text")
        for placeholder, gate in [(False, lambda: True), (True, None)]:
            with self.subTest(placeholder=placeholder), self._dataless_fixture(path, placeholder=placeholder) as state:
                with self.assertRaises(OSError):
                    document_changes_module._snapshot(self.conn, self.watch, path, content_allowed_check=gate)
            self.assertEqual(state["set"], []); self.assertEqual(state["failed_reads"], 1)

    def test_dataless_oversized_changed_or_nonregular_file_do_not_enable(self):
        path = self.watch / "cloud.md"; path.write_text("same original text")
        descriptor = os.open(path, os.O_RDONLY)
        try:
            value = os.fstat(descriptor)
            for changes in [{"st_size": document_changes_module.MAX_PACKAGE_BYTES + 1}, {"st_ino": value.st_ino + 1}, {"st_mode": 0o040700}]:
                current = SimpleNamespace(**{name: getattr(value, name) for name in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_mode")}, st_flags=0x40000000)
                for name, replacement in changes.items(): setattr(current, name, replacement)
                recovery = document_changes_module._DatalessReadRecovery(descriptor, value, lambda: True)
                with mock.patch.object(document_changes_module.os, "read", side_effect=OSError(errno.EDEADLK, "synthetic")), \
                     mock.patch.object(document_changes_module.os, "fstat", return_value=current), \
                     mock.patch.object(document_changes_module, "_dataless_thread_policy") as policy:
                    with self.assertRaises(OSError): recovery.read(128)
                    policy.assert_not_called()
        finally: os.close(descriptor)

    def test_dataless_other_errno_does_not_enable(self):
        path = self.watch / "normal.md"; path.write_text("normal text")
        with mock.patch.object(document_changes_module.os, "read", side_effect=OSError(errno.EIO, "synthetic")), \
             mock.patch.object(document_changes_module, "_dataless_thread_policy") as policy:
            with self.assertRaises(OSError):
                document_changes_module._snapshot(self.conn, self.watch, path, content_allowed_check=lambda: True)
        policy.assert_not_called()

    def test_dataless_restore_failure_stops_scan_before_next_file(self):
        path = self.watch / "cloud.md"; path.write_text("same original text")
        later = self.watch / "later.md"; later.write_text("normal later text")
        calls = []
        original_snapshot = document_changes_module._snapshot
        def snapshot(*args, **kwargs):
            calls.append(args[2]); return original_snapshot(*args, **kwargs)
        with self._dataless_fixture(path, restore_fails=True) as state, \
             mock.patch.object(document_changes_module, "_snapshot", side_effect=snapshot):
            with self.assertRaises(document_changes_module.DatalessReadPolicyRestoreError):
                build_document_baseline(self.conn, self.watch, paths=[path, later], content_allowed_check=lambda: True)
        self.assertEqual(calls, [path]); self.assertEqual(state["set"], [2, 1])
        self.assertIsNone(document_changes_module._DOCUMENT_DATALESS_READ_RECOVERY.get())

    def test_dataless_restore_and_close_failure_stops_before_next_file(self):
        path = self.watch / "cloud.md"; path.write_text("same original text")
        later = self.watch / "later.md"; later.write_text("normal later text")
        calls, closed = [], []
        original_snapshot, native_close, native_stat = document_changes_module._snapshot, os.close, os.fstat
        inode = path.stat().st_ino
        def snapshot(*args, **kwargs):
            calls.append(args[2]); return original_snapshot(*args, **kwargs)
        def close(descriptor):
            target = native_stat(descriptor).st_ino == inode
            native_close(descriptor)
            if target:
                closed.append(descriptor)
                raise OSError(errno.EIO, "synthetic descriptor close failure")
        with self._dataless_fixture(path, restore_fails=True), \
             mock.patch.object(document_changes_module, "_snapshot", side_effect=snapshot), \
             mock.patch.object(document_changes_module.os, "close", side_effect=close):
            with self.assertRaises(document_changes_module.DatalessReadPolicyRestoreError):
                build_document_baseline(self.conn, self.watch, paths=[path, later], content_allowed_check=lambda: True)
        self.assertEqual(calls, [path]); self.assertEqual(len(closed), 1)
        self.assertIsNone(document_changes_module._DOCUMENT_DATALESS_READ_RECOVERY.get())

    def test_dataless_worker_restore_failure_joins_pipeline_threads(self):
        path = self.watch / "aaa-cloud.md"; path.write_text("same original text")
        before = {thread for thread in threading.enumerate() if thread.name.startswith("pam-document-")}
        with self._dataless_fixture(path, restore_fails=True):
            with self.assertRaises(document_changes_module.DatalessReadPolicyRestoreError):
                self._start_controlled_worker()
        self.assertEqual({thread for thread in threading.enumerate() if thread.name.startswith("pam-document-")}, before)
        self.assertFalse(document_changes_status(self.conn)["coverage"]["is_complete"])

    def test_dataless_auxiliary_io_runs_outside_materialization_window(self):
        path = self.watch / "cloud.md"; path.write_text("same original text")
        names = ("_hmac_key", "_file_id_for_relative", "_document_signal_path_hmac", "_project_label_for_path")
        seen = []
        with self._dataless_fixture(path) as state, contextlib.ExitStack() as stack:
            for name in names:
                original = getattr(document_changes_module, name)
                def checked(*args, _name=name, _original=original, **kwargs):
                    self.assertEqual(state["policy"], 1)
                    seen.append(_name)
                    return _original(*args, **kwargs)
                stack.enter_context(mock.patch.object(document_changes_module, name, side_effect=checked))
            value = document_changes_module._snapshot(self.conn, self.watch, path, content_allowed_check=lambda: True)
        self.assertTrue(value["content_hmac"])
        self.assertEqual(set(seen), set(names)); self.assertEqual(state["set"], [2, 1])

    def test_dataless_final_hash_read_revocation_or_change_discards_snapshot(self):
        path = self.watch / "cloud.md"; path.write_text("same original text")
        for failure in ("revoked", "changed"):
            with self.subTest(failure=failure), self._dataless_fixture(path) as state:
                original = document_changes_module._read_document_chunk
                terminal = False
                def read(descriptor, count):
                    nonlocal terminal
                    value = original(descriptor, count)
                    if not value and any(frame.function == "_file_content_hmac" for frame in inspect.stack()):
                        terminal = True
                        if failure == "changed":
                            value_stat = path.stat()
                            os.utime(path, ns=(value_stat.st_atime_ns, value_stat.st_mtime_ns + 1_000_000_000))
                    return value
                with mock.patch.object(document_changes_module, "_read_document_chunk", side_effect=read):
                    with self.assertRaises(PermissionError if failure == "revoked" else OSError):
                        document_changes_module._snapshot(self.conn, self.watch, path, content_allowed_check=lambda: not (terminal and failure == "revoked"))
            self.assertTrue(terminal); self.assertEqual(state["set"], [2, 1]); self.assertEqual(state["policy"], 1)

    def test_dataless_unknown_original_policy_does_not_enable(self):
        path = self.watch / "cloud.md"; path.write_text("same original text")
        with self._dataless_fixture(path) as state, mock.patch.object(document_changes_module, "_dataless_thread_policy", return_value=(lambda *_: 4, lambda *_: self.fail("unknown policy must not change"))):
            with self.assertRaises(OSError):
                document_changes_module._snapshot(self.conn, self.watch, path, content_allowed_check=lambda: True)
        self.assertEqual(state["set"], [])

    def test_dataless_context_reset_failure_stops_before_next_file(self):
        path = self.watch / "cloud.md"; path.write_text("same original text")
        later = self.watch / "later.md"; later.write_text("normal later text")
        original_context = document_changes_module._DOCUMENT_DATALESS_READ_RECOVERY
        original_snapshot, native_close, native_stat = document_changes_module._snapshot, os.close, os.fstat
        inode = path.stat().st_ino
        for close_fails in (False, True):
            calls, reset_tokens, closed = [], [], []
            class FaultyContext:
                def get(self): return original_context.get()
                def set(self, value): return original_context.set(value)
                def reset(self, token):
                    reset_tokens.append(token)
                    raise ValueError("synthetic context reset failure")
            def snapshot(*args, **kwargs):
                calls.append(args[2]); return original_snapshot(*args, **kwargs)
            def close(descriptor):
                target = native_stat(descriptor).st_ino == inode
                native_close(descriptor)
                if target:
                    closed.append(descriptor)
                    if close_fails: raise OSError(errno.EIO, "synthetic descriptor close failure")
            try:
                with self.subTest(close_fails=close_fails), self._dataless_fixture(path) as state, \
                     mock.patch.object(document_changes_module, "_DOCUMENT_DATALESS_READ_RECOVERY", FaultyContext()), \
                     mock.patch.object(document_changes_module, "_snapshot", side_effect=snapshot), \
                     mock.patch.object(document_changes_module.os, "close", side_effect=close):
                    with self.assertRaisesRegex(document_changes_module.DatalessReadPolicyRestoreError, "context restore"):
                        build_document_baseline(self.conn, self.watch, paths=[path, later], content_allowed_check=lambda: True)
                self.assertEqual(calls, [path]); self.assertEqual(len(closed), 1)
                self.assertEqual(state["set"], [2, 1]); self.assertEqual(state["policy"], 1)
            finally:
                for token in reversed(reset_tokens): original_context.reset(token)
        self.assertIsNone(original_context.get())

    def test_baseline_io_details_identify_real_read_frame_without_private_data(self):
        path = self.watch / "private-io-read.md"
        path.write_text("private io source sentinel", encoding="utf-8")
        inode = path.stat().st_ino
        original_read = os.read
        def fail_read(descriptor, size):
            if os.fstat(descriptor).st_ino == inode:
                raise OSError(errno.EIO, "private io exception sentinel", str(path))
            return original_read(descriptor, size)
        metrics = {}
        def record(name, value=1):
            metrics[name] = metrics.get(name, 0) + value
        token = document_changes_module._DOCUMENT_BASELINE_METRIC_RECORDER.set(record)
        try:
            with mock.patch.object(document_changes_module.os, "read", side_effect=fail_read):
                result = build_document_baseline(self.conn, self.watch, paths=[path])
        finally:
            document_changes_module._DOCUMENT_BASELINE_METRIC_RECORDER.reset(token)
        self.assertEqual(result["baseline_status"], "partial")
        self.assertEqual(result["files_failed"], 1)
        self.assertEqual(metrics["failure_snapshot_io_error"], 1)
        self.assertEqual(metrics["io_step_read"], 1)
        self.assertEqual(metrics["io_errno_EIO"], 1)
        for private in (str(path), path.name, "private io source sentinel", "private io exception sentinel"):
            self.assertNotIn(private, json.dumps(metrics))

    def test_baseline_io_details_distinguish_growth_limit_and_unknown_errno(self):
        path = self.watch / "private-growing.md"
        path.write_text("a", encoding="utf-8")
        calls = 0
        def allowed():
            nonlocal calls
            calls += 1
            if calls == 3:
                path.write_text("private growing source sentinel", encoding="utf-8")
            return True
        metrics = {}
        def record(name, value=1):
            metrics[name] = metrics.get(name, 0) + value
        token = document_changes_module._DOCUMENT_BASELINE_METRIC_RECORDER.set(record)
        try:
            with mock.patch.object(document_changes_module, "MAX_TEXT_BYTES", 2):
                result = build_document_baseline(self.conn, self.watch, paths=[path], content_allowed_check=allowed)
        finally:
            document_changes_module._DOCUMENT_BASELINE_METRIC_RECORDER.reset(token)
        self.assertEqual(result["baseline_status"], "partial")
        self.assertEqual(metrics["failure_snapshot_io_error"], 1)
        self.assertEqual(metrics["io_step_read_limit"], 1)
        self.assertEqual(metrics["io_errno_not_provided"], 1)
        error = OSError(987654, "private arbitrary errno sentinel", str(path))
        step, number = document_changes_module._baseline_snapshot_io_details(error)
        self.assertEqual((step, number), ("other", "other"))
        self.assertNotIn("987654", json.dumps((step, number)))
        self.assertNotIn("private arbitrary errno sentinel", json.dumps((step, number)))

    def test_baseline_io_detail_failure_preserves_scan_and_later_file(self):
        failed_path = self.watch / "private-detail-failure.md"
        good_path = self.watch / "private-detail-success.md"
        failed_path.write_text("private failure source", encoding="utf-8")
        good_path.write_text("private success source", encoding="utf-8")
        original = document_changes_module._snapshot
        def snapshot(conn, root, path, **kwargs):
            if path == failed_path:
                raise OSError(errno.EIO, "private diagnostic failure")
            return original(conn, root, path, **kwargs)
        metrics = {}
        def record(name, value=1):
            metrics[name] = metrics.get(name, 0) + value
        token = document_changes_module._DOCUMENT_BASELINE_METRIC_RECORDER.set(record)
        try:
            with mock.patch.object(document_changes_module, "_snapshot", side_effect=snapshot), mock.patch.object(
                document_changes_module, "_baseline_snapshot_io_details", side_effect=RuntimeError("private helper failure")
            ) as details:
                result = build_document_baseline(self.conn, self.watch, paths=[failed_path, good_path])
                details.assert_called_once()
        finally:
            document_changes_module._DOCUMENT_BASELINE_METRIC_RECORDER.reset(token)
        self.assertEqual(result["baseline_status"], "partial")
        self.assertEqual(result["files_failed"], 1)
        self.assertEqual(result["files_indexed"], 1)
        self.assertEqual(metrics["failure_snapshot_io_error"], 1)
        self.assertNotIn("failure_traversal_failed", metrics)
        self.assertFalse(any(key.startswith("io_") for key in metrics))

    def test_baseline_io_details_are_skipped_without_metrics(self):
        path = self.watch / "private-no-diagnostics.md"
        path.write_text("private source", encoding="utf-8")
        token = document_changes_module._DOCUMENT_BASELINE_METRIC_RECORDER.set(None)
        try:
            with mock.patch.object(document_changes_module, "_snapshot", side_effect=OSError(errno.EIO, "private failure")), mock.patch.object(
                document_changes_module, "_baseline_snapshot_io_details", side_effect=AssertionError("must remain disabled")
            ) as details:
                result = build_document_baseline(self.conn, self.watch, paths=[path])
                details.assert_not_called()
        finally:
            document_changes_module._DOCUMENT_BASELINE_METRIC_RECORDER.reset(token)
        self.assertEqual(result["baseline_status"], "partial")
        self.assertEqual(result["files_failed"], 1)

    def test_baseline_io_details_identify_real_key_hash_and_wrapped_lock_frames(self):
        key_path = self.root / "private-fixture-key"
        key_path.write_bytes(b"k" * 32)
        hash_path = self.watch / "private-fixture-hash.md"
        hash_path.write_text("private hash source", encoding="utf-8")
        original_read = os.read
        for path, expected_step in ((key_path, "key_read"), (hash_path, "hash")):
            with self.subTest(step=expected_step):
                inode = path.stat().st_ino
                def fail_read(descriptor, size):
                    if os.fstat(descriptor).st_ino == inode:
                        raise OSError(errno.EIO, "private chain failure", str(path))
                    return original_read(descriptor, size)
                with mock.patch.object(document_changes_module.os, "read", side_effect=fail_read):
                    try:
                        if expected_step == "key_read":
                            document_changes_module._read_private_hmac_secret(path)
                        else:
                            descriptor = os.open(path, os.O_RDONLY)
                            try:
                                document_changes_module._file_content_hmac(b"k" * 32, descriptor, path.stat().st_size)
                            finally:
                                os.close(descriptor)
                    except OSError as error:
                        details = document_changes_module._baseline_snapshot_io_details(error)
                    else:
                        self.fail("real fixture chain must fail")
                self.assertEqual(details, (expected_step, "EIO"))
                self.assertNotIn(str(path), json.dumps(details))
                self.assertNotIn("private chain failure", json.dumps(details))
        lock_path = self.db.parent / document_changes_module.AUTHORIZATION_LOCK_FILENAME
        with document_changes_module.document_authorization_lock(self.conn, exclusive=True):
            pass
        lock_inode = lock_path.stat().st_ino
        original_stat = os.fstat
        def fail_lock_stat(descriptor):
            metadata = original_stat(descriptor)
            if metadata.st_ino == lock_inode:
                raise OSError(errno.EBADF, "private lock failure", str(lock_path))
            return metadata
        with mock.patch.object(document_changes_module.os, "fstat", side_effect=fail_lock_stat):
            try:
                with document_changes_module.document_authorization_lock(self.conn, exclusive=False):
                    self.fail("wrapped lock fixture must fail before entry")
            except OSError as error:
                details = document_changes_module._baseline_snapshot_io_details(error)
        self.assertEqual(details, ("authorization_lock", "EBADF"))
        self.assertNotIn(str(lock_path), json.dumps(details))
        self.assertNotIn("private lock failure", json.dumps(details))

    def test_baseline_worker_groups_io_details_by_public_root_and_keeps_partial(self):
        diagnostics = []
        path = self.watch / "private-worker-io.md"
        path.write_text("private worker io source sentinel", encoding="utf-8")
        inode = path.stat().st_ino
        original_read = os.read
        def fail_read(descriptor, size):
            if os.fstat(descriptor).st_ino == inode:
                raise OSError(errno.ETIMEDOUT, "private worker io exception sentinel", str(path))
            return original_read(descriptor, size)
        registry = document_changes_module.runtime_document_roots(self.root / "data")
        ref = next(item["root_ref"] for item in registry if Path(item["path"]).resolve() == self.watch.resolve())
        with mock.patch.object(document_changes_module.os, "read", side_effect=fail_read):
            worker, _ = self._start_controlled_worker(diagnostic_sink=diagnostics.append)
        try:
            self.assertTrue(worker["started"])
            self.assertFalse(document_changes_status(self.conn)["coverage"]["is_complete"])
        finally:
            self._stop_worker(worker)
        progress = [x for x in diagnostics if x.get("event") == "document_change_baseline_progress"]
        details = progress[-1]["snapshot_io_failure_details"]
        self.assertEqual(progress[-1]["scan_issue_counts"], {"snapshot_io_error": 2})
        self.assertEqual(details, {
            "steps": {"read": 2}, "errnos": {"ETIMEDOUT": 2},
            "by_root": {ref: {"io_step_read": 2, "io_errno_ETIMEDOUT": 2}},
            "association": "independent_aggregate_counts",
        })
        serialized = json.dumps(progress)
        for private in (str(path), path.name, "private worker io source sentinel", "private worker io exception sentinel", "traceback"):
            self.assertNotIn(private, serialized)

    def test_baseline_diagnostic_failure_counts_stay_with_registered_public_roots(self):
        second_root = self.root / "second-private-root"
        second_root.mkdir()
        configure_document_changes(self.conn, add_root=second_root, root_label="Private root sentinel", runtime_root=self.root / "data")
        first = self.watch / "private-first.md"
        second = second_root / "private-second.md"
        first.write_text("private source sentinel", encoding="utf-8")
        second.write_text("private source sentinel", encoding="utf-8")
        registry = document_changes_module.runtime_document_roots(self.root / "data")
        refs = {Path(item["path"]).resolve(): item["root_ref"] for item in registry}
        diagnostics = []
        original = document_changes_module._snapshot
        def fail_each(conn, root, path, **kwargs):
            if path.resolve() == first.resolve():
                raise PermissionError(13, "private exception sentinel", str(path))
            if path.resolve() == second.resolve():
                raise OSError("document changed while fingerprinting")
            return original(conn, root, path, **kwargs)
        with mock.patch.object(document_changes_module, "_snapshot", side_effect=fail_each), mock.patch.object(_ControlledProcess, "root_count", 2):
            worker, _ = self._start_controlled_worker(diagnostic_sink=diagnostics.append)
        try:
            self.assertTrue(worker["started"])
            self.assertFalse(document_changes_status(self.conn)["coverage"]["is_complete"])
        finally:
            self._stop_worker(worker)
        progress = [item for item in diagnostics if item.get("event") == "document_change_baseline_progress"]
        self.assertEqual(progress[-1]["scan_issue_counts"], {"permission_denied": 2, "content_changed": 2})
        self.assertEqual(progress[-1]["root_scan_issue_counts"], {
            refs[self.watch.resolve()]: {"permission_denied": 2},
            refs[second_root.resolve()]: {"content_changed": 2},
        })
        serialized = json.dumps(progress)
        for item in registry:
            self.assertNotIn(item["root_id"], serialized)
            self.assertNotIn(item["path"], serialized)
            self.assertNotIn(item["root_label"], serialized)
        for private in (first.name, second.name, "private source sentinel", "private exception sentinel"):
            self.assertNotIn(private, serialized)

    def test_git_ignore_failure_fails_closed_and_marks_coverage_partial(self):
        subprocess.run(["git", "init", "-q"], cwd=self.watch, check=True)
        path = self.watch / "source.md"
        path.write_text("before", encoding="utf-8")
        with mock.patch("activity_mvp.document_changes._git_ignored_batch", return_value=None):
            baseline = build_document_baseline(self.conn, self.watch)
        self.assertEqual(baseline["baseline_status"], "partial")
        build_document_baseline(self.conn, self.watch)
        path.write_text("after", encoding="utf-8")
        with mock.patch("activity_mvp.document_changes._git_ignored_batch", return_value=None):
            event = process_document_event(self.conn, self._event(path, 135), root_path=self.watch)
        self.assertEqual(event["reason"], "git_ignore_unavailable")
        self.assertEqual(document_changes_status(self.conn)["coverage"]["coverage_status"], "partial")

    def test_orphaned_linked_worktree_uses_conservative_ignore_rules(self):
        common = self.root / "common-repository"
        orphan = self.root / "orphaned-worktree"
        common.mkdir()
        orphan.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=common, check=True)
        subprocess.run(
            ["git", "config", "user.email", "pam-test@example.invalid"],
            cwd=common,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "PAM Test"],
            cwd=common,
            check=True,
        )
        for directory in (common, orphan):
            (directory / ".gitignore").write_text("ignored.md\n", encoding="utf-8")
            (directory / "ignored.md").write_text("ignored", encoding="utf-8")
            (directory / "visible.md").write_text("visible", encoding="utf-8")
        # The common index says ignored.md is tracked, but the linked
        # worktree's own index disappeared with its admin entry.  The only
        # privacy-safe answer is to honor every ignore match via --no-index;
        # borrowing the common index would risk reading a file that was
        # untracked and ignored on the orphan's original branch.
        subprocess.run(
            ["git", "add", "-f", ".gitignore", "ignored.md", "visible.md"],
            cwd=common,
            check=True,
        )
        subprocess.run(["git", "commit", "-qm", "fixture"], cwd=common, check=True)
        admin_parent = common / ".git" / "worktrees"
        admin_parent.mkdir()
        missing_admin = admin_parent / "orphaned-worktree"
        (orphan / ".git").write_text(
            f"gitdir: {missing_admin}\n",
            encoding="utf-8",
        )
        configure_document_changes(
            self.conn,
            add_root=orphan,
            root_label="Orphaned worktree",
            runtime_root=self.root / "data",
        )

        result = build_document_baseline(self.conn, orphan)

        self.assertEqual(result["baseline_status"], "complete")
        self.assertEqual(result["files_failed"], 0)
        rows = self.conn.execute(
            "SELECT file_name_redacted FROM file_watch_state WHERE root_id=?",
            (result["root_id"],),
        ).fetchall()
        self.assertEqual([str(row["file_name_redacted"]) for row in rows], ["visible.md"])

    def test_retention_keeps_recent_file_history_expires_old_and_manual_delete_resets_baseline(self):
        """File history outlives screen activity; a manual delete still clears it.

        Screen captures and OCR expire after today+yesterday, but a file change
        row is only a filename, a change kind, a count and a structural summary,
        so it keeps FILE_CHANGE_RETENTION_DAYS instead.  Retention must never
        touch the baseline fingerprints either, or every pass would force a full
        rescan of files nobody edited that day.
        """
        path = self.watch / "retention.md"
        path.write_text("before", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        path.write_text("after", encoding="utf-8")
        process_document_event(self.conn, self._event(path, 140), root_path=self.watch)
        root_id = str(self.conn.execute("SELECT root_id FROM file_watch_roots").fetchone()[0])
        recent_gap = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.conn.execute(
            "INSERT INTO file_watch_coverage_gaps("
            "id,root_id,observed_at,reason_code,affects_attribution,helper_event_id,created_at"
            ") VALUES(?,?,?,?,?,?,?)",
            (
                "fcg_retention_probe",
                root_id,
                recent_gap,
                "stream_gap",
                1,
                "140",
                recent_gap,
            ),
        )
        self.conn.commit()
        start = "2000-01-01T00:00:00+00:00"
        end = "2100-01-01T00:00:00+00:00"
        preview = preview_delete_range(self.conn, start, end, source_status="expired_deleted")
        self.assertEqual(preview["cascade"]["file_watch_state_reset"], 0)
        # A retention pass today leaves today's file history in place.
        self.assertEqual(preview["cascade"]["file_change_events"], 0)
        self.assertEqual(preview["cascade"]["file_watch_coverage_gaps"], 0)
        expired = delete_range(self.conn, start, end, source_status="expired_deleted")
        self.assertEqual(expired["cascade"]["file_change_events"], 0)
        self.assertEqual(expired["cascade"]["file_watch_coverage_gaps"], 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM file_change_events").fetchone()[0],
            1,
        )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM file_watch_state").fetchone()[0], 1)
        self.assertTrue(document_changes_status(self.conn)["coverage"]["is_complete"])

        # Age the same row past the file-history window: retention now takes it.
        from activity_mvp.retention import FILE_CHANGE_RETENTION_DAYS

        aged = (
            datetime.now(timezone.utc) - timedelta(days=FILE_CHANGE_RETENTION_DAYS + 1)
        ).isoformat(timespec="seconds")
        self.conn.execute(
            "UPDATE file_change_events SET first_changed_at=?, last_changed_at=?",
            (aged, aged),
        )
        self.conn.execute(
            "UPDATE file_watch_coverage_gaps SET observed_at=?,created_at=? WHERE id='fcg_retention_probe'",
            (aged, aged),
        )
        self.conn.commit()
        aged_expired = delete_range(self.conn, start, end, source_status="expired_deleted")
        self.assertEqual(aged_expired["cascade"]["file_change_events"], 1)
        self.assertEqual(aged_expired["cascade"]["file_watch_coverage_gaps"], 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM file_change_events").fetchone()[0],
            0,
        )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM file_watch_state").fetchone()[0], 1)

        path.write_text("again", encoding="utf-8")
        process_document_event(self.conn, self._event(path, 141), root_path=self.watch)
        manual = delete_range(self.conn, start, end)
        self.assertEqual(manual["cascade"]["file_watch_state_reset"], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM file_watch_state").fetchone()[0], 0)
        self.assertFalse(document_changes_status(self.conn)["coverage"]["is_complete"])

    def test_foreground_link_is_only_confidence_context_and_drives_safe_sorting(self):
        detailed = self.watch / "detailed.md"
        foreground = self.watch / "foreground.md"
        metadata = self.watch / "meta.pdf"
        detailed.write_text("old", encoding="utf-8")
        foreground.write_text("old", encoding="utf-8")
        metadata.write_bytes(b"old")
        build_document_baseline(self.conn, self.watch)
        detailed.write_text("new", encoding="utf-8")
        foreground.write_text("new", encoding="utf-8")
        metadata.write_bytes(b"new")
        at = datetime.now(timezone.utc).isoformat()
        process_document_event(self.conn, self._event(detailed, 150, observed_at=at), root_path=self.watch)
        process_document_event(self.conn, self._event(foreground, 151, observed_at=at), root_path=self.watch)
        process_document_event(self.conn, self._event(metadata, 152, observed_at=at), root_path=self.watch)
        self.conn.execute(
            "INSERT INTO document_signals(id,session_id,captured_at,file_name_redacted,deletion_group_id) VALUES(?,?,?,?,?)",
            ("doc-context", "session", at, "foreground.md", "group"),
        )
        self.conn.commit()
        listed = self._all_changes()
        self.assertEqual([item["file_name_redacted"] for item in listed["items"]], ["foreground.md", "detailed.md", "meta.pdf"])
        self.assertEqual(listed["items"][0]["confidence"], "likely_user_edit")
        self.assertEqual(listed["items"][1]["confidence"], "local_file_changed")

    def test_limit_does_not_change_totals_or_protected_count(self):
        public = self.watch / "public.md"
        private = self.watch / "客户隐私.md"
        public.write_text("one", encoding="utf-8")
        private.write_text("one", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        public.write_text("two", encoding="utf-8")
        private.write_text("two", encoding="utf-8")
        process_document_event(self.conn, self._event(public, 160), root_path=self.watch)
        process_document_event(self.conn, self._event(private, 161), root_path=self.watch)
        limited = self._all_changes(limit=0)
        self.assertEqual(limited["items"], [])
        self.assertEqual(limited["totals"]["distinct_files"], 2)
        self.assertEqual(limited["totals"]["protected"], 1)

    def test_public_status_uses_non_hmac_root_ref_and_remove_accepts_it(self):
        previous_ref = document_changes_status(self.conn)["roots"][0]["root_ref"]
        status = configure_document_changes(self.conn, add_root=self.watch, root_label="Watch", runtime_root=self.root / "data")
        public_status = status["document_changes"]
        root_ref = public_status["roots"][0]["root_ref"]
        root_id = str(self.conn.execute("SELECT root_id FROM file_watch_roots").fetchone()[0])
        registry = self.root / "data/document_watch_roots.json"
        self.assertEqual(oct(registry.stat().st_mode & 0o777), "0o600")
        self.assertIn(str(self.watch), registry.read_text(encoding="utf-8"))
        self.assertEqual(root_ref, previous_ref)
        self.assertRegex(root_ref, r"\Arootref_[A-Za-z0-9_-]{24}\Z")
        serialized = json.dumps(public_status, ensure_ascii=False)
        self.assertNotIn('"root_id"', serialized)
        self.assertNotIn(root_id, serialized)
        removed = configure_document_changes(self.conn, remove_root=root_ref, runtime_root=self.root / "data")
        self.assertEqual(removed["document_changes"]["coverage"]["root_count"], 0)
        self.assertNotIn(root_id, registry.read_text(encoding="utf-8"))

    def test_unknown_root_ref_fails_closed_without_changing_authorization(self):
        before = self.conn.execute("SELECT COUNT(*) FROM file_watch_roots").fetchone()[0]
        with self.assertRaisesRegex(ValueError, "unknown document watch root reference"):
            configure_document_changes(
                self.conn,
                remove_root="rootref_0123456789abcdefghijklmn",
                runtime_root=self.root / "data",
            )
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM file_watch_roots").fetchone()[0], before)

    def test_noop_or_label_only_configure_never_rewrites_authorization_sidecar(self):
        registry = self.root / "data/document_watch_roots.json"
        before = registry.read_text(encoding="utf-8")
        with mock.patch("activity_mvp.document_changes._write_runtime_registry") as writer:
            configure_document_changes(self.conn, runtime_root=self.root / "data")
            configure_document_changes(
                self.conn,
                root_label="ignored-without-add-root",
                runtime_root=self.root / "data",
            )
            configure_document_changes(
                self.conn,
                reset_baseline=True,
                runtime_root=self.root / "data",
            )
        writer.assert_not_called()
        self.assertEqual(registry.read_text(encoding="utf-8"), before)

    def test_invalid_root_or_sidecar_failure_rolls_back_long_lived_connection(self):
        registry = self.root / "data/document_watch_roots.json"
        before_registry = registry.read_text(encoding="utf-8")
        before_roots = self.conn.execute("SELECT COUNT(*) FROM file_watch_roots").fetchone()[0]
        with self.assertRaises((OSError, ValueError)):
            configure_document_changes(
                self.conn,
                add_root=self.root / "missing-root",
                runtime_root=self.root / "data",
            )
        self.assertFalse(self.conn.in_transaction)
        with mock.patch("activity_mvp.document_changes._write_runtime_registry", side_effect=OSError("simulated")):
            with self.assertRaises(OSError):
                configure_document_changes(
                    self.conn,
                    remove_root=document_changes_status(self.conn)["roots"][0]["root_ref"],
                    runtime_root=self.root / "data",
                )
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM file_watch_roots").fetchone()[0], before_roots)
        self.assertEqual(registry.read_text(encoding="utf-8"), before_registry)

    def test_commit_failure_restores_registry_and_never_resurrects_uncommitted_root(self):
        registry = self.root / "data/document_watch_roots.json"
        before_registry = registry.read_text(encoding="utf-8")
        before_ids = {
            str(row["root_id"])
            for row in self.conn.execute("SELECT root_id FROM file_watch_roots").fetchall()
        }
        second = self.root / "commit-failure-root"
        second.mkdir()
        failing = sqlite3.connect(self.db, factory=_FailingCommitConnection, timeout=30)
        failing.row_factory = sqlite3.Row
        failing.execute("PRAGMA foreign_keys = ON")
        failing.fail_next_commit = True
        try:
            with self.assertRaisesRegex(OSError, "simulated commit failure"):
                configure_document_changes(
                    failing,
                    add_root=second,
                    root_label="Uncommitted",
                    runtime_root=self.root / "data",
                )
        finally:
            failing.close()
        self.assertEqual(registry.read_text(encoding="utf-8"), before_registry)
        self.assertEqual(
            {
                str(row["root_id"])
                for row in self.conn.execute("SELECT root_id FROM file_watch_roots").fetchall()
            },
            before_ids,
        )
        ensure_default_document_roots(self.conn, self.root / "data")
        self.assertEqual(
            {
                str(row["root_id"])
                for row in self.conn.execute("SELECT root_id FROM file_watch_roots").fetchall()
            },
            before_ids,
        )

    def test_revision_skew_never_reauthorizes_sidecar_only_root(self):
        second = self.root / "sidecar-only-root"
        second.mkdir()
        configure_document_changes(
            self.conn,
            add_root=second,
            root_label="Sidecar only",
            runtime_root=self.root / "data",
        )
        registry = self.root / "data/document_watch_roots.json"
        envelope = json.loads(registry.read_text(encoding="utf-8"))
        second_item = next(item for item in envelope["roots"] if item["root_label"] == "Sidecar only")
        second_id = str(second_item["root_id"])
        self.conn.execute("DELETE FROM file_watch_checkpoints WHERE root_id=?", (second_id,))
        self.conn.execute("DELETE FROM file_watch_roots WHERE root_id=?", (second_id,))
        self.conn.execute(
            "UPDATE daemon_state SET value=? WHERE key='document_change_root_authorization_revision'",
            (str(int(envelope["authorization_revision"]) - 1),),
        )
        self.conn.commit()
        before_registry = registry.read_text(encoding="utf-8")

        self.assertEqual(ensure_default_document_roots(self.conn, self.root / "data"), [])
        self.assertIsNone(
            self.conn.execute("SELECT 1 FROM file_watch_roots WHERE root_id=?", (second_id,)).fetchone()
        )
        self.assertEqual(registry.read_text(encoding="utf-8"), before_registry)
        status = document_changes_status(self.conn)
        self.assertEqual(status["roots"], [])
        self.assertFalse(status["coverage"]["is_complete"])

    def test_explicit_add_recovers_invalid_authorization_by_revoking_unknown_roots(self):
        old_id = str(self.conn.execute("SELECT root_id FROM file_watch_roots").fetchone()[0])
        registry = self.root / "data/document_watch_roots.json"
        registry.unlink()
        self.assertEqual(document_changes_status(self.conn)["roots"], [])
        replacement = self.root / "replacement-authorization"
        replacement.mkdir()

        with self.assertRaisesRegex(ValueError, "invalid or out of sync"):
            configure_document_changes(
                self.conn,
                add_root=replacement,
                root_label="Replacement",
                runtime_root=self.root / "data",
            )
        self.assertIsNotNone(
            self.conn.execute("SELECT 1 FROM file_watch_roots WHERE root_id=?", (old_id,)).fetchone()
        )

        recovered = configure_document_changes(
            self.conn,
            reauthorize_root=replacement,
            root_label="Replacement",
            runtime_root=self.root / "data",
        )

        root_rows = self.conn.execute("SELECT root_id,root_label FROM file_watch_roots").fetchall()
        self.assertEqual(len(root_rows), 1)
        self.assertNotEqual(str(root_rows[0]["root_id"]), old_id)
        self.assertEqual(str(root_rows[0]["root_label"]), "Replacement")
        self.assertEqual(len(_registry_roots(registry)), 1)
        self.assertEqual(len(recovered["document_changes"]["roots"]), 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM file_watch_state").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM file_change_events").fetchone()[0], 0)

    def test_swapped_registry_bindings_cannot_delete_another_root(self):
        second = self.root / "swapped-root"
        second.mkdir()
        configure_document_changes(
            self.conn,
            add_root=second,
            root_label="Second",
            runtime_root=self.root / "data",
        )
        registry = self.root / "data/document_watch_roots.json"
        roots = _registry_roots(registry)
        target_ref = str(roots[0]["root_ref"])
        roots[0]["root_id"], roots[1]["root_id"] = roots[1]["root_id"], roots[0]["root_id"]
        _write_registry_roots(registry, roots)
        before_registry = registry.read_text(encoding="utf-8")
        before_ids = {
            str(row["root_id"])
            for row in self.conn.execute("SELECT root_id FROM file_watch_roots").fetchall()
        }
        self.assertEqual(document_changes_status(self.conn)["roots"], [])
        with self.assertRaisesRegex(ValueError, "invalid or out of sync"):
            configure_document_changes(
                self.conn,
                remove_root=target_ref,
                runtime_root=self.root / "data",
            )
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(
            {
                str(row["root_id"])
                for row in self.conn.execute("SELECT root_id FROM file_watch_roots").fetchall()
            },
            before_ids,
        )
        self.assertEqual(registry.read_text(encoding="utf-8"), before_registry)

    def test_revocation_waits_for_inflight_public_read_to_finish(self):
        from activity_mvp import document_changes as module

        path = self.watch / "revocation-race.md"
        path.write_text("before", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        path.write_text("after", encoding="utf-8")
        process_document_event(self.conn, self._event(path, 181), root_path=self.watch)
        root_ref = document_changes_status(self.conn)["roots"][0]["root_ref"]
        reached_final_check = threading.Event()
        release_reader = threading.Event()
        reader_done = threading.Event()
        remover_done = threading.Event()
        results: dict[str, dict] = {}
        errors: list[BaseException] = []
        original_fresh = module._fresh_document_authorization

        def gated_fresh(connection):
            value = original_fresh(connection)
            reached_final_check.set()
            release_reader.wait(timeout=8)
            return value

        def read_changes():
            connection = ensure_ready(self.db)
            try:
                results["read"] = list_document_changes(
                    connection,
                    "2000-01-01T00:00:00+00:00",
                    "2100-01-01T00:00:00+00:00",
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                connection.close()
                reader_done.set()

        def revoke_root():
            connection = ensure_ready(self.db)
            try:
                configure_document_changes(
                    connection,
                    remove_root=root_ref,
                    runtime_root=self.root / "data",
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                connection.close()
                remover_done.set()

        with mock.patch("activity_mvp.document_changes._fresh_document_authorization", side_effect=gated_fresh):
            reader = threading.Thread(target=read_changes)
            reader.start()
            self.assertTrue(reached_final_check.wait(timeout=5))
            remover = threading.Thread(target=revoke_root)
            remover.start()
            time.sleep(0.2)
            self.assertFalse(remover_done.is_set(), "revocation bypassed the public read lock")
            release_reader.set()
            self.assertTrue(reader_done.wait(timeout=8))
            self.assertTrue(remover_done.wait(timeout=8))
            reader.join(timeout=2)
            remover.join(timeout=2)
        self.assertEqual(errors, [])
        self.assertEqual(results["read"]["totals"]["distinct_files"], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM file_watch_roots").fetchone()[0], 0)

    def test_revocation_waits_for_inflight_status_root_refs(self):
        from activity_mvp import document_changes as module

        root_ref = document_changes_status(self.conn)["roots"][0]["root_ref"]
        reached_binding_check = threading.Event()
        release_status = threading.Event()
        status_done = threading.Event()
        remove_done = threading.Event()
        errors: list[BaseException] = []
        original_bindings = module._registry_root_bindings_valid

        def gated_bindings(*args, **kwargs):
            value = original_bindings(*args, **kwargs)
            reached_binding_check.set()
            release_status.wait(timeout=8)
            return value

        def read_status():
            connection = ensure_ready(self.db)
            try:
                document_changes_status(connection)
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                connection.close()
                status_done.set()

        def remove_root():
            connection = ensure_ready(self.db)
            try:
                configure_document_changes(
                    connection,
                    remove_root=root_ref,
                    runtime_root=self.root / "data",
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                connection.close()
                remove_done.set()

        with mock.patch("activity_mvp.document_changes._registry_root_bindings_valid", side_effect=gated_bindings):
            status_thread = threading.Thread(target=read_status)
            status_thread.start()
            self.assertTrue(reached_binding_check.wait(timeout=5))
            remove_thread = threading.Thread(target=remove_root)
            remove_thread.start()
            time.sleep(0.2)
            self.assertFalse(remove_done.is_set(), "revocation bypassed status root-ref lease")
            release_status.set()
            self.assertTrue(status_done.wait(timeout=8))
            self.assertTrue(remove_done.wait(timeout=8))
            status_thread.join(timeout=2)
            remove_thread.join(timeout=2)
        self.assertEqual(errors, [])

    def test_revocation_waits_for_inflight_noop_configure_root_refs(self):
        from activity_mvp import document_changes as module

        root_ref = document_changes_status(self.conn)["roots"][0]["root_ref"]
        reached_binding_check = threading.Event()
        release_configure = threading.Event()
        configure_done = threading.Event()
        remove_started = threading.Event()
        remove_done = threading.Event()
        results: dict[str, dict] = {}
        errors: list[BaseException] = []
        original_bindings = module._registry_root_bindings_valid

        def gated_bindings(*args, **kwargs):
            value = original_bindings(*args, **kwargs)
            if threading.current_thread().name == "noop-configure-reader":
                reached_binding_check.set()
                release_configure.wait(timeout=8)
            return value

        def read_noop_configure():
            connection = ensure_ready(self.db)
            try:
                results["configure"] = configure_document_changes(
                    connection,
                    runtime_root=self.root / "data",
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                connection.close()
                configure_done.set()

        def remove_root():
            connection = ensure_ready(self.db)
            try:
                remove_started.set()
                configure_document_changes(
                    connection,
                    remove_root=root_ref,
                    runtime_root=self.root / "data",
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                connection.close()
                remove_done.set()

        with mock.patch("activity_mvp.document_changes._registry_root_bindings_valid", side_effect=gated_bindings):
            configure_thread = threading.Thread(target=read_noop_configure, name="noop-configure-reader")
            configure_thread.start()
            self.assertTrue(reached_binding_check.wait(timeout=5))
            remove_thread = threading.Thread(target=remove_root, name="root-remover")
            remove_thread.start()
            self.assertTrue(remove_started.wait(timeout=5))
            time.sleep(0.2)
            self.assertFalse(remove_done.is_set(), "revocation bypassed no-op configure root-ref lease")
            observer = ensure_ready(self.db)
            try:
                self.assertEqual(observer.execute("SELECT COUNT(*) FROM file_watch_roots").fetchone()[0], 1)
            finally:
                observer.close()
            release_configure.set()
            self.assertTrue(configure_done.wait(timeout=8))
            self.assertTrue(remove_done.wait(timeout=8))
            configure_thread.join(timeout=2)
            remove_thread.join(timeout=2)
        self.assertEqual(errors, [])
        self.assertEqual(
            results["configure"]["document_changes"]["roots"][0]["root_ref"],
            root_ref,
        )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM file_watch_roots").fetchone()[0], 0)

    def test_export_off_waits_for_inflight_export_and_blocks_later_exports(self):
        from activity_mvp import document_changes as module

        path = self.watch / "export-race.md"
        path.write_text("before", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        path.write_text("after", encoding="utf-8")
        process_document_event(self.conn, self._event(path, 182), root_path=self.watch)
        configure_document_changes(self.conn, export_enabled=True, runtime_root=self.root / "data")
        reached_final_check = threading.Event()
        release_reader = threading.Event()
        reader_done = threading.Event()
        disable_done = threading.Event()
        results: dict[str, dict] = {}
        errors: list[BaseException] = []
        original_fresh = module._fresh_document_authorization

        def gated_fresh(connection):
            value = original_fresh(connection)
            reached_final_check.set()
            release_reader.wait(timeout=8)
            return value

        def read_export():
            connection = ensure_ready(self.db)
            try:
                results["read"] = list_document_changes(
                    connection,
                    "2000-01-01T00:00:00+00:00",
                    "2100-01-01T00:00:00+00:00",
                    for_export=True,
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                connection.close()
                reader_done.set()

        def disable_export():
            connection = ensure_ready(self.db)
            try:
                configure_document_changes(
                    connection,
                    export_enabled=False,
                    runtime_root=self.root / "data",
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                connection.close()
                disable_done.set()

        with mock.patch("activity_mvp.document_changes._fresh_document_authorization", side_effect=gated_fresh):
            reader = threading.Thread(target=read_export)
            reader.start()
            self.assertTrue(reached_final_check.wait(timeout=5))
            disabler = threading.Thread(target=disable_export)
            disabler.start()
            time.sleep(0.2)
            self.assertFalse(disable_done.is_set(), "export-off bypassed the public read lock")
            release_reader.set()
            self.assertTrue(reader_done.wait(timeout=8))
            self.assertTrue(disable_done.wait(timeout=8))
            reader.join(timeout=2)
            disabler.join(timeout=2)
        self.assertEqual(errors, [])
        self.assertEqual(results["read"]["totals"]["distinct_files"], 1)
        blocked = self._all_changes(for_export=True)
        self.assertEqual(blocked["items"], [])
        self.assertEqual(blocked["totals"]["distinct_files"], 1)
        self.assertEqual(blocked["export_state"], "blocked")
        self.assertTrue(blocked["observed_changes"])

    def test_export_off_waits_for_complete_handoff_and_connector_packages(self):
        from activity_mvp import ai_connector as connector_module
        from activity_mvp import ai_handoff as handoff_module

        path = self.watch / "package-export-race.md"
        path.write_text("before", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        path.write_text("after", encoding="utf-8")
        process_document_event(self.conn, self._event(path, 183), root_path=self.watch)
        builders = (
            (
                "activity_mvp.ai_handoff.validate_ai_handoff_payload",
                handoff_module.validate_ai_handoff_payload,
                lambda connection: build_ai_handoff_package(connection, range_name="today"),
            ),
            (
                "activity_mvp.ai_connector.validate_ai_handoff_payload",
                connector_module.validate_ai_handoff_payload,
                lambda connection: build_ai_connector_package(connection, range_name="today"),
            ),
        )
        for patch_target, original_validator, builder in builders:
            with self.subTest(target=patch_target):
                configure_document_changes(self.conn, export_enabled=True, runtime_root=self.root / "data")
                reached_validator = threading.Event()
                release_builder = threading.Event()
                builder_done = threading.Event()
                disable_done = threading.Event()
                errors: list[BaseException] = []

                def gated_validator(payload):
                    reached_validator.set()
                    release_builder.wait(timeout=8)
                    return original_validator(payload)

                def build_package():
                    connection = ensure_ready(self.db)
                    try:
                        builder(connection)
                    except BaseException as exc:  # pragma: no cover - surfaced below
                        errors.append(exc)
                    finally:
                        connection.close()
                        builder_done.set()

                def disable_export():
                    connection = ensure_ready(self.db)
                    try:
                        configure_document_changes(
                            connection,
                            export_enabled=False,
                            runtime_root=self.root / "data",
                        )
                    except BaseException as exc:  # pragma: no cover - surfaced below
                        errors.append(exc)
                    finally:
                        connection.close()
                        disable_done.set()

                with mock.patch(patch_target, side_effect=gated_validator):
                    builder_thread = threading.Thread(target=build_package)
                    builder_thread.start()
                    self.assertTrue(reached_validator.wait(timeout=5))
                    disable_thread = threading.Thread(target=disable_export)
                    disable_thread.start()
                    time.sleep(0.2)
                    self.assertFalse(disable_done.is_set(), "export-off bypassed package read lease")
                    release_builder.set()
                    self.assertTrue(builder_done.wait(timeout=8))
                    self.assertTrue(disable_done.wait(timeout=8))
                    builder_thread.join(timeout=2)
                    disable_thread.join(timeout=2)
                self.assertEqual(errors, [])

    def test_legacy_registry_upgrade_preserves_identity_baseline_and_checkpoint(self):
        path = self.watch / "legacy-ref.md"
        path.write_text("baseline", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        self.conn.execute("UPDATE file_watch_checkpoints SET last_event_id='777'")
        self.conn.commit()
        root_id = str(self.conn.execute("SELECT root_id FROM file_watch_roots").fetchone()[0])
        epoch = self.conn.execute(
            "SELECT value FROM daemon_state WHERE key='document_change_authorization_epoch'"
        ).fetchone()[0]
        state_count = self.conn.execute("SELECT COUNT(*) FROM file_watch_state").fetchone()[0]
        checkpoint = tuple(
            self.conn.execute(
                "SELECT last_event_id,coverage_status,event_gap_count FROM file_watch_checkpoints"
            ).fetchone()
        )
        registry = self.root / "data/document_watch_roots.json"
        payload = _registry_roots(registry)
        payload[0].pop("root_ref")
        _write_registry_roots(registry, payload, legacy=True)

        normalized = ensure_default_document_roots(self.conn, self.root / "data")

        self.assertEqual(str(normalized[0]["root_id"]), root_id)
        self.assertRegex(str(normalized[0]["root_ref"]), r"\Arootref_[A-Za-z0-9_-]{24}\Z")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM file_watch_state").fetchone()[0], state_count)
        self.assertEqual(
            tuple(self.conn.execute("SELECT last_event_id,coverage_status,event_gap_count FROM file_watch_checkpoints").fetchone()),
            checkpoint,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT value FROM daemon_state WHERE key='document_change_authorization_epoch'"
            ).fetchone()[0],
            epoch,
        )

    def test_remove_then_readd_rotates_root_ref_and_rejects_old_handles(self):
        old_ref = document_changes_status(self.conn)["roots"][0]["root_ref"]
        internal_id = str(self.conn.execute("SELECT root_id FROM file_watch_roots").fetchone()[0])
        configure_document_changes(self.conn, remove_root=old_ref, runtime_root=self.root / "data")
        readded = configure_document_changes(
            self.conn,
            add_root=self.watch,
            root_label="Watch",
            runtime_root=self.root / "data",
        )
        new_ref = readded["document_changes"]["roots"][0]["root_ref"]
        self.assertNotEqual(new_ref, old_ref)
        with self.assertRaisesRegex(ValueError, "unknown document watch root reference"):
            configure_document_changes(self.conn, remove_root=old_ref, runtime_root=self.root / "data")
        self.assertFalse(self.conn.in_transaction)
        with self.assertRaisesRegex(ValueError, "internal document watch root identifiers are not accepted"):
            configure_document_changes(self.conn, remove_root=internal_id, runtime_root=self.root / "data")
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(document_changes_status(self.conn)["coverage"]["root_count"], 1)

    def test_invalid_or_duplicate_registry_refs_never_fall_back_to_internal_ids(self):
        second_root = self.root / "second-smuggle-root"
        second_root.mkdir()
        configure_document_changes(
            self.conn,
            add_root=second_root,
            root_label="Second",
            runtime_root=self.root / "data",
        )
        registry = self.root / "data/document_watch_roots.json"
        original = _registry_roots(registry)
        internal_id = str(original[0]["root_id"])
        cases = []
        malformed = [dict(original[0])]
        malformed[0]["root_ref"] = "rootref_bad"
        cases.append(malformed)
        non_string = [dict(original[0])]
        non_string[0]["root_ref"] = []
        cases.append(non_string)
        null_ref = [dict(original[0])]
        null_ref[0]["root_ref"] = None
        cases.append(null_ref)
        empty_ref = [dict(original[0])]
        empty_ref[0]["root_ref"] = ""
        cases.append(empty_ref)
        non_string_path = [dict(original[0])]
        non_string_path[0]["path"] = 1
        cases.append(non_string_path)
        relative_path = [dict(original[0])]
        relative_path[0]["path"] = "relative-root"
        cases.append(relative_path)
        non_string_label = [dict(original[0])]
        non_string_label[0]["root_label"] = 123
        cases.append(non_string_label)
        null_label = [dict(original[0])]
        null_label[0]["root_label"] = None
        cases.append(null_label)
        smuggled_internal_hash = [dict(original[0])]
        smuggled_internal_hash[0]["root_ref"] = "rootref_" + internal_id.removeprefix("root_")[:24]
        cases.append(smuggled_internal_hash)
        cross_root_smuggling = [dict(item) for item in original]
        cross_root_smuggling[0]["root_ref"] = (
            "rootref_" + str(original[1]["root_id"]).removeprefix("root_")[:24]
        )
        cases.append(cross_root_smuggling)
        uppercase_cross_root_smuggling = [dict(item) for item in original]
        uppercase_cross_root_smuggling[0]["root_ref"] = (
            "rootref_" + str(original[1]["root_id"]).removeprefix("root_")[:24].upper()
        )
        cases.append(uppercase_cross_root_smuggling)
        cross_root_label_smuggling = [dict(item) for item in original]
        cross_root_label_smuggling[0]["root_label"] = (
            "Label-" + str(original[1]["root_id"]).removeprefix("root_")[:24]
        )
        cases.append(cross_root_label_smuggling)
        uppercase_cross_root_label_smuggling = [dict(item) for item in original]
        uppercase_cross_root_label_smuggling[0]["root_label"] = (
            "Label-" + str(original[1]["root_id"]).removeprefix("root_")[:24].upper()
        )
        cases.append(uppercase_cross_root_label_smuggling)
        duplicate = [dict(original[0]), dict(original[0])]
        cases.append(duplicate)
        for payload in cases:
            with self.subTest(payload_size=len(payload)):
                _write_registry_roots(registry, payload)
                status = document_changes_status(self.conn)
                serialized = json.dumps(status, ensure_ascii=False)
                self.assertEqual(status["roots"], [])
                self.assertIn("root_control_refs_incomplete", status["limitations"])
                self.assertNotIn(internal_id, serialized)
                self.assertNotIn('"root_id"', serialized)
                registry_before = registry.read_text(encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "document watch root registry is invalid"):
                    configure_document_changes(
                        self.conn,
                        remove_root=self.watch,
                        runtime_root=self.root / "data",
                    )
                self.assertFalse(self.conn.in_transaction)
                self.assertEqual(registry.read_text(encoding="utf-8"), registry_before)
                self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM file_watch_roots").fetchone()[0], 2)
        _write_registry_roots(registry, original)
        mismatched_path = [dict(item) for item in original]
        mismatched_path[0]["path"] = str(self.root)
        _write_registry_roots(registry, mismatched_path)
        mismatched_status = document_changes_status(self.conn)
        self.assertEqual(mismatched_status["roots"], [])
        self.assertIn("root_control_refs_incomplete", mismatched_status["limitations"])
        self.assertIn("coverage_incomplete", mismatched_status["limitations"])
        self.assertFalse(mismatched_status["coverage"]["is_complete"])

        _write_registry_roots(registry, original)
        database_only_root = self.root / "database-only-root"
        database_only_root.mkdir()
        configure_document_changes(
            self.conn,
            add_root=database_only_root,
            root_label="Database only",
            runtime_root=self.root / "data",
        )
        registry_with_database_root = _registry_roots(registry)
        database_only_item = next(
            item for item in registry_with_database_root if item["root_label"] == "Database only"
        )
        database_only_root_id = str(database_only_item["root_id"])
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "UPDATE file_watch_roots SET baseline_status='complete',baseline_completed_at=?,updated_at=?",
            (now, now),
        )
        self.conn.execute(
            "UPDATE file_watch_checkpoints SET coverage_status='complete',event_gap_count=0,"
            "coverage_started_at=?,updated_at=?",
            (now, now),
        )
        self.conn.commit()
        self.assertTrue(document_changes_status(self.conn)["coverage"]["is_complete"])
        db_only_smuggling = [dict(item) for item in original]
        db_only_smuggling[0]["root_ref"] = (
            "rootref_" + database_only_root_id.removeprefix("root_")[:24]
        )
        _write_registry_roots(registry, db_only_smuggling)
        skewed_status = document_changes_status(self.conn)
        self.assertEqual(skewed_status["roots"], [])
        self.assertIn("root_control_refs_incomplete", skewed_status["limitations"])
        self.assertIn("coverage_incomplete", skewed_status["limitations"])
        self.assertFalse(skewed_status["coverage"]["is_complete"])
        self.assertEqual(skewed_status["coverage"]["coverage_status"], "partial")
        self.assertNotIn(
            database_only_root_id.removeprefix("root_")[:24],
            json.dumps(skewed_status, ensure_ascii=False),
        )
        configure_document_changes(self.conn, export_enabled=True, runtime_root=self.root / "data")
        skewed_export = list_document_changes(
            self.conn,
            "2000-01-01T00:00:00+00:00",
            "2100-01-01T00:00:00+00:00",
            for_export=True,
        )
        self.assertFalse(skewed_export["coverage"]["is_complete"])
        self.assertIn("coverage_incomplete", skewed_export["limitations"])
        self.conn.execute("DELETE FROM file_watch_checkpoints WHERE root_id=?", (database_only_root_id,))
        self.conn.execute("DELETE FROM file_watch_roots WHERE root_id=?", (database_only_root_id,))
        self.conn.commit()
        _write_registry_roots(registry, original)
        sensitive_path_label = "/" + "Users/private/watch"
        self.conn.execute(
            "UPDATE file_watch_roots SET root_label=? WHERE root_id=?",
            (sensitive_path_label, str(original[0]["root_id"])),
        )
        self.conn.commit()
        path_label_status = document_changes_status(self.conn)
        self.assertNotIn(sensitive_path_label, json.dumps(path_label_status, ensure_ascii=False))
        self.assertIn("path_redacted", [item["label"] for item in path_label_status["roots"]])
        self.conn.execute(
            "UPDATE file_watch_roots SET root_label=? WHERE root_id=?",
            (str(original[0]["root_label"]), str(original[0]["root_id"])),
        )
        self.conn.commit()
        leaked_prefix = str(original[1]["root_id"]).removeprefix("root_")[:24].upper()
        self.conn.execute(
            "UPDATE file_watch_roots SET root_label=? WHERE root_id=?",
            ("Label-" + leaked_prefix, str(original[0]["root_id"])),
        )
        self.conn.commit()
        corrupted_status = document_changes_status(self.conn)
        self.assertIn("root_control_refs_incomplete", corrupted_status["limitations"])
        self.assertNotIn(leaked_prefix, json.dumps(corrupted_status, ensure_ascii=False))

    def test_ensure_acquires_database_write_lock_before_reading_registry(self):
        from activity_mvp import document_changes as module

        connection_ready = threading.Event()
        start_ensure = threading.Event()
        registry_read = threading.Event()
        finished = threading.Event()
        errors: list[BaseException] = []
        original_load = module._load_runtime_registry

        def observed_load(*args, **kwargs):
            registry_read.set()
            return original_load(*args, **kwargs)

        def run_ensure():
            ensure_conn = ensure_ready(self.db)
            connection_ready.set()
            start_ensure.wait(timeout=5)
            try:
                ensure_default_document_roots(ensure_conn, self.root / "data")
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                ensure_conn.close()
                finished.set()

        thread = threading.Thread(target=run_ensure)
        thread.start()
        self.assertTrue(connection_ready.wait(timeout=5))
        blocker = ensure_ready(self.db)
        blocker.execute("BEGIN IMMEDIATE")
        try:
            with mock.patch("activity_mvp.document_changes._load_runtime_registry", side_effect=observed_load):
                start_ensure.set()
                time.sleep(0.2)
                self.assertFalse(registry_read.is_set(), "registry was read before the SQLite write lock")
                blocker.commit()
                self.assertTrue(finished.wait(timeout=5))
        finally:
            if blocker.in_transaction:
                blocker.rollback()
            blocker.close()
            start_ensure.set()
            thread.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertTrue(registry_read.is_set())

    def test_ensure_sidecar_write_stays_locked_and_cannot_resurrect_removed_root(self):
        from activity_mvp import document_changes as module

        root_ref = document_changes_status(self.conn)["roots"][0]["root_ref"]
        ensure_at_write = threading.Event()
        release_ensure_write = threading.Event()
        ensure_done = threading.Event()
        remove_done = threading.Event()
        errors: list[BaseException] = []
        original_write = module._write_runtime_registry

        def gated_write(*args, **kwargs):
            if threading.current_thread().name == "ensure-root-normalizer":
                ensure_at_write.set()
                release_ensure_write.wait(timeout=8)
            return original_write(*args, **kwargs)

        def run_ensure():
            ensure_conn = ensure_ready(self.db)
            try:
                ensure_default_document_roots(ensure_conn, self.root / "data")
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                ensure_conn.close()
                ensure_done.set()

        def run_remove():
            remove_conn = ensure_ready(self.db)
            try:
                configure_document_changes(
                    remove_conn,
                    remove_root=root_ref,
                    runtime_root=self.root / "data",
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                remove_conn.close()
                remove_done.set()

        with mock.patch("activity_mvp.document_changes._write_runtime_registry", side_effect=gated_write):
            normalizer = threading.Thread(target=run_ensure, name="ensure-root-normalizer")
            normalizer.start()
            self.assertTrue(ensure_at_write.wait(timeout=5))
            remover = threading.Thread(target=run_remove, name="remove-root-after-ensure")
            remover.start()
            time.sleep(0.2)
            self.assertFalse(remove_done.is_set(), "remove bypassed ensure's DB+sidecar authorization lock")
            release_ensure_write.set()
            self.assertTrue(ensure_done.wait(timeout=8))
            self.assertTrue(remove_done.wait(timeout=8))
            normalizer.join(timeout=2)
            remover.join(timeout=2)

        self.assertEqual(errors, [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM file_watch_roots").fetchone()[0], 0)
        registry = _registry_roots(self.root / "data/document_watch_roots.json")
        self.assertEqual(registry, [])
        ensure_default_document_roots(self.conn, self.root / "data")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM file_watch_roots").fetchone()[0], 0)
        self.assertEqual(
            _registry_roots(self.root / "data/document_watch_roots.json"),
            [],
        )

    def test_worker_preserves_global_arrival_order_without_timer_threads(self):
        lower = self.watch / "lower.md"
        higher = self.watch / "higher.md"
        lower.write_text("before", encoding="utf-8")
        higher.write_text("before", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        worker, controlled = self._start_controlled_worker()
        lower.write_text("after", encoding="utf-8")
        higher.write_text("after", encoding="utf-8")

        try:
            controlled.emit(self._event(lower, 200))
            controlled.emit(self._event(higher, 201))
            completed = self._wait_until(
                lambda: self.conn.execute(
                    "SELECT COUNT(DISTINCT file_id) FROM file_change_events"
                ).fetchone()[0]
                == 2,
                timeout=9,
            )
            self.assertTrue(completed, "the lower event was lost after the higher checkpoint advanced")
            checkpoint = self.conn.execute("SELECT last_event_id FROM file_watch_checkpoints").fetchone()[0]
            self.assertEqual(checkpoint, "201")
        finally:
            self._stop_worker(worker)

    def test_event_id_wrap_resets_high_watermarks_for_every_stream_root(self):
        second_root = self.root / "second-watch"
        second_root.mkdir()
        configure_document_changes(
            self.conn,
            add_root=second_root,
            root_label="Second",
            runtime_root=self.root / "data",
        )
        first_path = self.watch / "wrapped.md"
        second_path = second_root / "also-wrapped.md"
        first_path.write_text("zero", encoding="utf-8")
        second_path.write_text("zero", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        build_document_baseline(self.conn, second_root)
        roots = {
            str(row["root_label"]): str(row["root_id"])
            for row in self.conn.execute("SELECT root_label,root_id FROM file_watch_roots")
        }
        self.conn.execute(
            "UPDATE file_watch_checkpoints SET last_event_id='999' WHERE root_id=?",
            (roots["Watch"],),
        )
        self.conn.execute(
            "UPDATE file_watch_checkpoints SET last_event_id='1200' WHERE root_id=?",
            (roots["Second"],),
        )
        self.conn.commit()

        first_path.write_text("one", encoding="utf-8")
        gap = process_document_event(
            self.conn,
            self._event(first_path, 2, flags=["event_ids_wrapped"]),
            root_path=self.watch,
        )
        self.assertEqual(gap["reason"], "event_gap")
        checkpoints = {
            row["root_id"]: (row["last_event_id"], row["coverage_status"])
            for row in self.conn.execute(
                "SELECT root_id,last_event_id,coverage_status FROM file_watch_checkpoints"
            )
        }
        self.assertNotEqual(checkpoints[roots["Second"]][0], "1200")
        self.assertEqual(checkpoints[roots["Second"]][1], "partial")
        build_document_baseline(self.conn, self.watch)
        build_document_baseline(self.conn, second_root)
        second_path.write_text("two", encoding="utf-8")
        recorded = process_document_event(
            self.conn,
            self._event(second_path, 3),
            root_path=second_root,
        )
        self.assertTrue(recorded["recorded"])
        self.assertEqual(recorded["item"]["change_kind"], "modified")
        checkpoint = self.conn.execute(
            "SELECT last_event_id FROM file_watch_checkpoints WHERE root_id=?",
            (roots["Second"],),
        ).fetchone()[0]
        self.assertEqual(checkpoint, "3")

    def test_root_changed_event_stops_helper_for_a_fresh_stream(self):
        path = self.watch / "root-change.md"
        path.write_text("baseline", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        worker, controlled = self._start_controlled_worker()
        try:
            controlled.emit(self._event(path, 210, flags=["root_changed"]))
            self.assertTrue(
                self._wait_until(lambda: controlled.terminated, timeout=7),
                "root_changed must terminate the stale FSEvents stream",
            )
        finally:
            self._stop_worker(worker)

    def test_historical_range_before_first_baseline_is_not_complete(self):
        (self.watch / "baseline.md").write_text("current", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        report = list_document_changes(
            self.conn,
            "2000-01-01T00:00:00+00:00",
            "2000-01-02T00:00:00+00:00",
        )
        self.assertFalse(report["coverage"]["is_complete"])
        self.assertIn("coverage_incomplete", report["limitations"])

    def test_gap_remains_visible_for_its_range_after_metadata_reconcile(self):
        path = self.watch / "gap-history.md"
        path.write_text("before", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        gap_at = datetime.now(timezone.utc)
        process_document_event(
            self.conn,
            self._event(path, 220, flags=["kernel_dropped"], observed_at=gap_at.isoformat()),
            root_path=self.watch,
        )
        build_document_baseline(self.conn, self.watch)
        report = list_document_changes(
            self.conn,
            (gap_at - timedelta(minutes=1)).isoformat(),
            (gap_at + timedelta(minutes=1)).isoformat(),
        )
        self.assertFalse(report["coverage"]["is_complete"])
        self.assertIn("coverage_incomplete", report["limitations"])

    def test_resume_race_keeps_range_incomplete_after_rebaseline(self):
        path = self.watch / "resume-race.md"
        path.write_text("before", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        pause(self.conn, mode="manual")
        path.write_text("during pause", encoding="utf-8")
        resume(self.conn)
        self.conn.execute("UPDATE daemon_state SET value='false' WHERE key='locked'")
        self.conn.commit()
        changed_at = datetime.now(timezone.utc)
        path.write_text("first save after resume", encoding="utf-8")
        overlap = process_document_event(
            self.conn,
            self._event(path, 230, observed_at=changed_at.isoformat()),
            root_path=self.watch,
        )
        self.assertEqual(overlap["reason"], "baseline_gap")
        build_document_baseline(self.conn, self.watch)
        report = list_document_changes(
            self.conn,
            (changed_at - timedelta(minutes=1)).isoformat(),
            (changed_at + timedelta(minutes=1)).isoformat(),
        )
        self.assertFalse(report["coverage"]["is_complete"])

    def test_removing_all_roots_is_persistent_across_default_initialization(self):
        root_ref = document_changes_status(self.conn)["roots"][0]["root_ref"]
        configure_document_changes(
            self.conn,
            remove_root=root_ref,
            runtime_root=self.root / "data",
        )
        fake_home = self.root / "fake-home"
        (fake_home / "AI").mkdir(parents=True)
        reloaded = ensure_default_document_roots(
            self.conn,
            self.root / "data",
            home=fake_home,
        )
        self.assertEqual(reloaded, [])
        self.assertEqual(document_changes_status(self.conn)["coverage"]["root_count"], 0)

    def test_manual_full_range_delete_clears_baseline_only_state(self):
        path = self.watch / "baseline-only.md"
        path.write_text("# Private baseline label", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM file_watch_state").fetchone()[0], 1)
        deleted = delete_range(
            self.conn,
            "0001-01-01T00:00:00+00:00",
            "9999-12-31T23:59:59+00:00",
        )
        self.assertGreaterEqual(deleted["cascade"]["file_watch_state_reset"], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM file_watch_state").fetchone()[0], 0)
        self.assertFalse(document_changes_status(self.conn)["coverage"]["is_complete"])

    def test_hardlink_creation_is_not_reported_as_rename(self):
        original = self.watch / "hardlink-source.md"
        linked = self.watch / "hardlink-copy.md"
        original.write_text("same inode and content", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        os.link(original, linked)
        created = process_document_event(
            self.conn,
            self._event(linked, 240, flags=["item_created"]),
            root_path=self.watch,
        )
        self.assertEqual(created["item"]["change_kind"], "created")
        state = self.conn.execute(
            "SELECT present FROM file_watch_state WHERE file_name_redacted=?",
            (original.name,),
        ).fetchone()
        self.assertEqual(state["present"], 1)

    def test_recreated_path_after_delete_is_created_not_modified(self):
        path = self.watch / "recreated.md"
        build_document_baseline(self.conn, self.watch)
        path.write_text("first identity", encoding="utf-8")
        process_document_event(self.conn, self._event(path, 250, flags=["item_created"]), root_path=self.watch)
        path.unlink()
        process_document_event(self.conn, self._event(path, 251, flags=["item_removed"]), root_path=self.watch)
        path.write_text("second identity", encoding="utf-8")
        recreated = process_document_event(
            self.conn,
            self._event(path, 252, flags=["item_created"]),
            root_path=self.watch,
        )
        self.assertEqual(recreated["item"]["change_kind"], "created")

    def test_api_key_content_is_protected_and_count_only(self):
        path = self.watch / "config.json"
        first_key = "sk" + "-proj-" + ("a" * 24)
        second_key = "sk" + "-proj-" + ("b" * 24)
        secret_field = "OPENAI_" + "API_KEY"
        path.write_text(json.dumps({secret_field: first_key}), encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        baseline = self.conn.execute(
            "SELECT detail_level,file_name_redacted FROM file_watch_state"
        ).fetchone()
        self.assertEqual(baseline["detail_level"], "protected")
        self.assertEqual(baseline["file_name_redacted"], "")
        path.write_text(json.dumps({secret_field: second_key}), encoding="utf-8")
        changed = process_document_event(self.conn, self._event(path, 260), root_path=self.watch)
        self.assertEqual(changed["item"]["detail_level"], "protected")
        report = self._all_changes()
        self.assertEqual(report["totals"]["protected"], 1)
        self.assertEqual(report["items"], [])
        encoded = json.dumps(report, ensure_ascii=False)
        self.assertNotIn(path.name, encoded)
        self.assertNotIn(first_key, encoded)
        self.assertNotIn(second_key, encoded)

    def test_unknown_delete_buffered_during_baseline_marks_coverage_gap(self):
        build_document_baseline(self.conn, self.watch)
        missing = self.watch / "deleted-during-baseline.md"
        result = process_document_event(
            self.conn,
            {
                **self._event(missing, 270, flags=["item_removed"]),
                "_arrived_during_baseline": True,
            },
            root_path=self.watch,
        )
        self.assertFalse(result["recorded"])
        self.assertEqual(result["reason"], "baseline_gap")
        self.assertFalse(document_changes_status(self.conn)["coverage"]["is_complete"])

    def test_parent_repository_ignore_rules_apply_to_a_watched_subdirectory(self):
        repo = self.root / "repository"
        watched = repo / "docs"
        watched.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        (repo / ".gitignore").write_text("docs/ignored.md\n", encoding="utf-8")
        ignored = watched / "ignored.md"
        ignored.write_text("must not be fingerprinted", encoding="utf-8")
        configure_document_changes(
            self.conn,
            add_root=watched,
            root_label="Repo docs",
            runtime_root=self.root / "data",
        )
        result = build_document_baseline(self.conn, watched)
        count = self.conn.execute(
            "SELECT COUNT(*) FROM file_watch_state WHERE root_id=?",
            (result["root_id"],),
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_git_repo_walk_honors_helper_stop_before_batching_all_candidates(self):
        subprocess.run(
            ["git", "init", "-q"],
            cwd=self.watch,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for index in range(16):
            (self.watch / f"repo-{index}.md").write_text(
                f"candidate {index}\n",
                encoding="utf-8",
            )

        from activity_mvp import document_changes as module

        original_repo_probe = module._git_repo_for_path
        visited: list[Path] = []

        def observe_repo(root, path):
            visited.append(Path(path))
            return original_repo_probe(root, path)

        def stopped() -> bool:
            return len(visited) >= 2

        with mock.patch(
            "activity_mvp.document_changes._git_repo_for_path",
            side_effect=observe_repo,
        ), mock.patch(
            "activity_mvp.document_changes._git_ignored_batch",
            wraps=module._git_ignored_batch,
        ) as ignored_batch:
            candidates = list(module._iter_safe_files(self.watch, stop_check=stopped))

        self.assertEqual(candidates, [])
        self.assertEqual(len(visited), 2)
        ignored_batch.assert_not_called()

    def test_walk_error_marks_baseline_partial_instead_of_silently_skipping(self):
        with mock.patch(
            "activity_mvp.document_changes.os.walk",
            side_effect=OSError("permission denied"),
        ):
            result = build_document_baseline(self.conn, self.watch)
        self.assertEqual(result["baseline_status"], "partial")
        self.assertGreaterEqual(result["files_failed"], 1)
        self.assertFalse(document_changes_status(self.conn)["coverage"]["is_complete"])

    def test_lock_during_baseline_never_reads_later_file_content(self):
        first = self.watch / "first-lock.md"
        second = self.watch / "second-lock.md"
        first.write_text("first", encoding="utf-8")
        second.write_text("second", encoding="utf-8")
        from activity_mvp import document_changes as module

        original_snapshot = module._snapshot
        calls: list[tuple[str, bool]] = []

        def observe_lock(conn, root, path, *, allow_content=True, content_allowed_check=None):
            calls.append((Path(path).name, allow_content))
            snapshot = original_snapshot(
                conn,
                root,
                path,
                allow_content=allow_content,
                content_allowed_check=content_allowed_check,
            )
            if Path(path).name == first.name:
                conn.execute(
                    "INSERT INTO daemon_state(key,value,updated_at) VALUES('locked','true',?) "
                    "ON CONFLICT(key) DO UPDATE SET value='true',updated_at=excluded.updated_at",
                    (datetime.now(timezone.utc).isoformat(),),
                )
                conn.commit()
            return snapshot

        with mock.patch("activity_mvp.document_changes._snapshot", side_effect=observe_lock):
            result = build_document_baseline(
                self.conn,
                self.watch,
                paths=[first, second],
            )
        second_content_reads = [
            allow_content for name, allow_content in calls if name == second.name
        ]
        self.assertFalse(any(second_content_reads))
        self.assertNotEqual(result["baseline_status"], "complete")

    def test_document_save_enters_context_main_chain_when_explicit_mapping_exists(self):
        self._configure_explicit_context_mapping()
        path = self.watch / "mapped-save.md"
        path.write_text("baseline", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        path.write_text("candidate-only content", encoding="utf-8")

        result = process_document_event(
            self.conn,
            self._event(path, 401),
            root_path=self.watch,
        )
        self.assertTrue(result["recorded"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM context_observations WHERE status='active'").fetchone()[0],
            1,
        )
        observation = self.conn.execute(
            "SELECT source_kind,lifecycle_event,trust_partition,status FROM context_observations"
        ).fetchone()
        self.assertEqual(
            tuple(observation),
            ("file_adapter", "artifact_saved", "owner_local_unverified", "active"),
        )
        evidence = self.conn.execute(
            "SELECT evidence_kind,evidence_tier,state,state_cap,status FROM context_evidence"
        ).fetchone()
        self.assertEqual(
            tuple(evidence),
            ("file_saved", "P2", "artifact_saved", "artifact_saved", "active"),
        )
        candidate = self.conn.execute(
            "SELECT desired_state,disposition,status FROM context_candidates"
        ).fetchone()
        self.assertEqual(tuple(candidate), ("artifact_saved", "requires_confirmation", "active"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM context_verified").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM context_search_index").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM context_memory_candidates").fetchone()[0], 0)
        source_version = self.conn.execute(
            "SELECT root_id,mapping_ref,mapping_version,document_identity_ref,helper_event_ref,"
            "content_version_ref,status FROM context_document_source_versions"
        ).fetchone()
        self.assertEqual(source_version["mapping_ref"], "mapping_d0000006")
        self.assertEqual(source_version["mapping_version"], 1)
        self.assertEqual(source_version["status"], "active")
        for key in ("document_identity_ref", "helper_event_ref", "content_version_ref"):
            self.assertRegex(str(source_version[key]), r"^[a-z]+_[a-z0-9]{8,64}$")

        context_rows = []
        for table in (
            "context_observations",
            "context_evidence",
            "context_candidates",
            "context_source_events",
            "context_document_source_versions",
        ):
            context_rows.extend(dict(row) for row in self.conn.execute(f"SELECT * FROM {table}"))
        serialized = json.dumps(context_rows, ensure_ascii=False, sort_keys=True)
        persisted_hmac = str(
            self.conn.execute(
                "SELECT content_hmac FROM file_watch_state WHERE file_name_redacted='mapped-save.md'"
            ).fetchone()[0]
        )
        self.assertNotIn(str(self.watch), serialized)
        self.assertNotIn(path.name, serialized)
        self.assertNotIn("candidate-only content", serialized)
        self.assertNotIn(persisted_hmac, serialized)

    def test_document_context_adapter_is_idempotent_across_debounce_and_replay(self):
        mapping = self._configure_explicit_context_mapping()
        occurred = datetime.now(timezone.utc).replace(microsecond=0)
        first = record_document_change_context_locked(
            self.conn,
            source_event_id="fce_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            occurred_at=occurred.isoformat(),
            root_id=mapping["root_id"],
            file_identity="file_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            helper_event_id="helper-event-77",
            content_version_identity="content-version-77",
            version_count=1,
            change_kind="modified",
            detail_level="detailed",
            project_label="ignored legacy label",
        )
        self.conn.commit()
        self.assertEqual(first["status"], "accepted")
        before = {
            table: self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "context_source_events",
                "context_observations",
                "context_evidence",
                "context_candidates",
                "context_document_source_versions",
            )
        }
        replay = record_document_change_context_locked(
            self.conn,
            source_event_id="fce_cccccccccccccccccccccccccccccccc",
            occurred_at=(occurred + timedelta(seconds=30)).isoformat(),
            root_id=mapping["root_id"],
            file_identity="file_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            helper_event_id="helper-event-77",
            content_version_identity="content-version-77",
            version_count=99,
            change_kind="modified",
            detail_level="detailed",
            project_label="different ignored label",
        )
        self.conn.commit()
        self.assertEqual(replay["status"], "replayed")
        self.assertEqual(replay["source_event_ref"], first["source_event_ref"])
        self.assertEqual(replay["observation_ref"], first["observation_ref"])
        after = {
            table: self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in before
        }
        self.assertEqual(after, before)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM context_verified").fetchone()[0], 0)

        changed_content_signature = record_document_change_context_locked(
            self.conn,
            source_event_id="fce_ffffffffffffffffffffffffffffffff",
            occurred_at=(occurred + timedelta(seconds=1)).isoformat(),
            root_id=mapping["root_id"],
            file_identity="file_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            helper_event_id="helper-event-77",
            content_version_identity="content-version-77-rekeyed",
            version_count=100,
            change_kind="modified",
            detail_level="detailed",
            project_label="ignored rekey attempt",
        )
        self.conn.rollback()
        self.assertEqual(
            (changed_content_signature["status"], changed_content_signature["reason_code"]),
            ("conflict", "canonical_source_event_conflict"),
        )
        self.assertEqual(
            {
                table: self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in before
            },
            before,
        )

        remapped = register_document_context_mapping(
            self.conn,
            mapping_ref="mapping_d0000006",
            mapping_version=2,
            root_id=mapping["root_id"],
            project_ref="project_d0000001",
            workstream_ref="workstream_d0000002",
            object_ref="object_d0000003",
            version_ref="version_d0000004",
            subject_ref="subject_d0000005",
            effective_at=(occurred + timedelta(seconds=1)).isoformat(),
            now=occurred + timedelta(seconds=1),
        )
        self.assertEqual(remapped["status"], "registered")
        changed_key_attempt = record_document_change_context_locked(
            self.conn,
            source_event_id="fce_dddddddddddddddddddddddddddddddd",
            occurred_at=(occurred + timedelta(seconds=2)).isoformat(),
            root_id=mapping["root_id"],
            file_identity="file_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            helper_event_id="helper-event-77",
            content_version_identity="content-version-77",
            version_count=100,
            change_kind="modified",
            detail_level="detailed",
            project_label="ignored after remap",
        )
        self.conn.rollback()
        self.assertEqual(
            (changed_key_attempt["status"], changed_key_attempt["reason_code"]),
            ("conflict", "canonical_source_event_conflict"),
        )
        self.assertEqual(
            {
                table: self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in before
            },
            before,
        )

        revoked = revoke_document_context_file(
            self.conn,
            root_id=mapping["root_id"],
            file_identity="file_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            now=occurred + timedelta(seconds=3),
        )
        self.assertEqual(revoked["status"], "completed")
        self.assertEqual(revoked["source_versions_revoked"], 1)
        resurrection = record_document_change_context_locked(
            self.conn,
            source_event_id="fce_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
            occurred_at=(occurred + timedelta(seconds=4)).isoformat(),
            root_id=mapping["root_id"],
            file_identity="file_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            helper_event_id="helper-event-77",
            content_version_identity="content-version-77",
            version_count=101,
            change_kind="modified",
            detail_level="detailed",
            project_label="ignored resurrection attempt",
        )
        self.conn.rollback()
        self.assertEqual(
            (resurrection["status"], resurrection["reason_code"]),
            ("rejected", "source_event_tombstoned"),
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM context_document_source_versions WHERE status='active'"
            ).fetchone()[0],
            0,
        )

    def test_document_delete_and_root_revocation_cascade_context_graph(self):
        mapping = self._configure_explicit_context_mapping()
        first_path = self.watch / "branch-one.md"
        second_path = self.watch / "branch-two.md"
        first_path.write_text("baseline one", encoding="utf-8")
        second_path.write_text("baseline two", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)

        first_path.write_text("changed one", encoding="utf-8")
        self.assertTrue(process_document_event(self.conn, self._event(first_path, 420), root_path=self.watch)["recorded"])
        first_source = self.conn.execute(
            "SELECT observation_ref,evidence_ref,candidate_ref FROM context_document_source_versions "
            "ORDER BY created_at,observation_ref LIMIT 1"
        ).fetchone()
        first_observation = str(first_source["observation_ref"])
        first_graph = self._attach_trusted_document_dependency(
            document_evidence_ref=str(first_source["evidence_ref"]),
            source_ref="source_d0000012",
            idempotency_key="idem_d0000013",
        )
        second_path.write_text("changed two", encoding="utf-8")
        self.assertTrue(process_document_event(self.conn, self._event(second_path, 421), root_path=self.watch)["recorded"])
        second_source = self.conn.execute(
            "SELECT observation_ref,evidence_ref,candidate_ref FROM context_document_source_versions "
            "WHERE observation_ref<>?",
            (first_observation,),
        ).fetchone()
        second_observation = str(second_source["observation_ref"])

        first_path.unlink()
        deleted = process_document_event(self.conn, self._event(first_path, 422), root_path=self.watch)
        self.assertTrue(deleted["recorded"])
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_observations WHERE observation_ref=?", (first_observation,)
            ).fetchone()[0],
            "revoked",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_observations WHERE observation_ref=?", (second_observation,)
            ).fetchone()[0],
            "active",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_evidence WHERE evidence_ref=?",
                (first_source["evidence_ref"],),
            ).fetchone()[0],
            "revoked",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_candidates WHERE candidate_ref=?",
                (first_source["candidate_ref"],),
            ).fetchone()[0],
            "revoked",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_verified WHERE verified_ref=?",
                (first_graph["verified_ref"],),
            ).fetchone()[0],
            "revoked",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_memory_candidates WHERE memory_candidate_ref=?",
                (first_graph["memory"]["memory_candidate_ref"],),
            ).fetchone()[0],
            "revoked",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_bundle_members WHERE bundle_ref=? AND member_ref=?",
                (first_graph["delivery"]["bundle_ref"], first_graph["verified_ref"]),
            ).fetchone()[0],
            "revoked",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT state FROM context_deliveries WHERE delivery_ref=?",
                (first_graph["delivery"]["delivery_ref"],),
            ).fetchone()[0],
            "failed",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM context_search_index WHERE verified_ref=?",
                (first_graph["verified_ref"],),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM context_document_tombstones WHERE scope='file'"
            ).fetchone()[0],
            1,
        )

        second_graph = self._attach_trusted_document_dependency(
            document_evidence_ref=str(second_source["evidence_ref"]),
            source_ref="source_d0000014",
            idempotency_key="idem_d0000015",
        )
        removed = configure_document_changes(
            self.conn,
            remove_root=self.watch,
            runtime_root=self.root / "data",
        )
        self.assertNotEqual(removed.get("status"), "failed")
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_observations WHERE observation_ref=?", (second_observation,)
            ).fetchone()[0],
            "revoked",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_verified WHERE verified_ref=?",
                (second_graph["verified_ref"],),
            ).fetchone()[0],
            "revoked",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_memory_candidates WHERE memory_candidate_ref=?",
                (second_graph["memory"]["memory_candidate_ref"],),
            ).fetchone()[0],
            "revoked",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_bundle_members WHERE bundle_ref=? AND member_ref=?",
                (second_graph["delivery"]["bundle_ref"], second_graph["verified_ref"]),
            ).fetchone()[0],
            "revoked",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT state FROM context_deliveries WHERE delivery_ref=?",
                (second_graph["delivery"]["delivery_ref"],),
            ).fetchone()[0],
            "failed",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_document_mappings WHERE mapping_ref=? AND mapping_version=1",
                (mapping["mapping_ref"],),
            ).fetchone()[0],
            "revoked",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM context_document_source_versions WHERE status='active'"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM context_document_tombstones WHERE scope='root' AND root_id=?",
                (mapping["root_id"],),
            ).fetchone()[0],
            1,
        )
        replay = revoke_document_context_root(self.conn, root_id=mapping["root_id"])
        self.assertEqual(replay["status"], "replayed")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM context_document_tombstones WHERE scope='root' AND root_id=?",
                (mapping["root_id"],),
            ).fetchone()[0],
            1,
        )

    def test_document_delete_and_root_revoke_cascade_context_graph(self):
        # Compatibility alias retained because the task matrix used both spellings.
        self.test_document_delete_and_root_revocation_cascade_context_graph()

    def test_root_revocation_failure_rolls_back_and_never_reports_success(self):
        mapping = self._configure_explicit_context_mapping()
        path = self.watch / "root-fault.md"
        path.write_text("baseline", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        path.write_text("changed", encoding="utf-8")
        self.assertTrue(process_document_event(self.conn, self._event(path, 425), root_path=self.watch)["recorded"])
        observation_ref = str(
            self.conn.execute("SELECT observation_ref FROM context_document_source_versions").fetchone()[0]
        )
        registry_path = self.root / "data/document_watch_roots.json"
        registry_before = registry_path.read_bytes()
        self.conn.execute(
            "CREATE TRIGGER document_root_revoke_fault BEFORE UPDATE ON context_evidence "
            "WHEN NEW.status='revoked' BEGIN SELECT RAISE(ABORT,'synthetic root fault'); END"
        )
        self.conn.commit()

        failed = configure_document_changes(
            self.conn,
            remove_root=self.watch,
            runtime_root=self.root / "data",
        )
        self.assertEqual((failed["status"], failed["reason_code"]), (
            "failed", "document_revocation_failed"
        ))
        self.assertEqual(registry_path.read_bytes(), registry_before)
        self.assertEqual(
            self.conn.execute(
                "SELECT enabled FROM file_watch_roots WHERE root_id=?", (mapping["root_id"],)
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_document_mappings WHERE mapping_ref=? AND mapping_version=1",
                (mapping["mapping_ref"],),
            ).fetchone()[0],
            "active",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_observations WHERE observation_ref=?", (observation_ref,)
            ).fetchone()[0],
            "active",
        )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM context_document_tombstones").fetchone()[0], 0)

        self.conn.execute("DROP TRIGGER document_root_revoke_fault")
        self.conn.commit()
        retried = configure_document_changes(
            self.conn,
            remove_root=self.watch,
            runtime_root=self.root / "data",
        )
        self.assertNotEqual(retried.get("status"), "failed")
        self.assertIsNone(
            self.conn.execute(
                "SELECT enabled FROM file_watch_roots WHERE root_id=?", (mapping["root_id"],)
            ).fetchone()
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_observations WHERE observation_ref=?", (observation_ref,)
            ).fetchone()[0],
            "revoked",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM context_document_tombstones WHERE scope='root' AND root_id=?",
                (mapping["root_id"],),
            ).fetchone()[0],
            1,
        )

    def test_document_save_without_explicit_mapping_does_not_invent_context_labels(self):
        enabled = configure_context_console(
            self.conn,
            enabled=True,
            confirmation=CONTEXT_FEATURE_CONFIRMATION,
        )
        self.assertEqual(enabled["status"], "enabled")
        labels_before = self.conn.execute("SELECT COUNT(*) FROM context_label_registry").fetchone()[0]
        principals_before = self.conn.execute("SELECT COUNT(*) FROM context_principals").fetchone()[0]
        path = self.watch / "unmapped.md"
        path.write_text("baseline", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        path.write_text("changed but unmapped", encoding="utf-8")
        result = process_document_event(self.conn, self._event(path, 430), root_path=self.watch)
        self.assertTrue(result["recorded"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM file_change_events").fetchone()[0], 1)
        for table in (
            "context_document_mappings",
            "context_source_events",
            "context_observations",
            "context_evidence",
            "context_candidates",
            "context_verified",
            "context_document_source_versions",
        ):
            self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM context_label_registry").fetchone()[0], labels_before)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM context_principals").fetchone()[0], principals_before)
        registry_text = json.dumps(
            [dict(row) for row in self.conn.execute("SELECT * FROM context_label_registry")],
            ensure_ascii=False,
        ).lower()
        self.assertNotIn("generic", registry_text)

    def test_document_mapping_feature_expiry_and_revocation_fail_closed(self):
        mapping = self._configure_explicit_context_mapping()
        occurred = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=2)
        disabled = configure_context_console(
            self.conn,
            enabled=False,
            confirmation=CONTEXT_FEATURE_CONFIRMATION,
        )
        self.assertEqual(disabled["status"], "disabled")
        feature_off = record_document_change_context_locked(
            self.conn,
            source_event_id="fce_a1000000000000000000000000000000",
            occurred_at=occurred.isoformat(),
            root_id=mapping["root_id"],
            file_identity="file_a2000000000000000000000000000000",
            helper_event_id="helper-feature-off",
            content_version_identity="content-feature-off",
            version_count=1,
            change_kind="modified",
            detail_level="metadata_only",
        )
        self.assertEqual((feature_off["status"], feature_off["reason_code"]), ("disabled", "feature_flag_off"))

        enabled = configure_context_console(
            self.conn,
            enabled=True,
            confirmation=CONTEXT_FEATURE_CONFIRMATION,
        )
        self.assertEqual(enabled["status"], "enabled")
        self.conn.execute(
            "UPDATE context_document_mappings SET expires_at=? WHERE mapping_ref=? AND mapping_version=1",
            ((occurred - timedelta(seconds=1)).isoformat(), mapping["mapping_ref"]),
        )
        self.conn.commit()
        expired = record_document_change_context_locked(
            self.conn,
            source_event_id="fce_a3000000000000000000000000000000",
            occurred_at=occurred.isoformat(),
            root_id=mapping["root_id"],
            file_identity="file_a4000000000000000000000000000000",
            helper_event_id="helper-expired",
            content_version_identity="content-expired",
            version_count=1,
            change_kind="modified",
            detail_level="metadata_only",
        )
        self.assertEqual(
            (expired["status"], expired["reason_code"]),
            ("skipped", "explicit_document_mapping_required"),
        )

        self.conn.execute(
            "UPDATE context_document_mappings SET expires_at='' WHERE mapping_ref=? AND mapping_version=1",
            (mapping["mapping_ref"],),
        )
        self.conn.commit()
        revoked = revoke_document_context_root(
            self.conn,
            root_id=mapping["root_id"],
            now=occurred + timedelta(seconds=1),
        )
        self.assertEqual(revoked["status"], "completed")
        after_revoke = record_document_change_context_locked(
            self.conn,
            source_event_id="fce_a5000000000000000000000000000000",
            occurred_at=(occurred + timedelta(seconds=2)).isoformat(),
            root_id=mapping["root_id"],
            file_identity="file_a6000000000000000000000000000000",
            helper_event_id="helper-revoked",
            content_version_identity="content-revoked",
            version_count=1,
            change_kind="modified",
            detail_level="metadata_only",
        )
        self.assertEqual(
            (after_revoke["status"], after_revoke["reason_code"]),
            ("skipped", "explicit_document_mapping_required"),
        )
        for table in (
            "context_source_events",
            "context_observations",
            "context_evidence",
            "context_candidates",
            "context_document_source_versions",
        ):
            self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)

    def test_document_mapping_principal_is_internal_only_and_revoked_with_root_or_supersede(self):
        mapping_v1 = self._configure_explicit_context_mapping()

        def attempt_controlled_file(mapping: dict, *, idempotency_key: str, source_ref: str) -> dict:
            secret = (
                f"internal-document-mapping|{mapping['mapping_ref']}|"
                f"{mapping['mapping_version']}|project_d0000001"
            ).encode()
            payload = {
                "contract_version": CONTROLLED_EVIDENCE_VERSION,
                "idempotency_key": idempotency_key,
                "principal_id": mapping["principal_id"],
                "key_id": mapping["key_id"],
                "source_ref": source_ref,
                "occurred_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "project_ref": "project_d0000001",
                "workstream_ref": "workstream_d0000002",
                "object_ref": "object_d0000003",
                "version_ref": "version_d0000004",
                "subject_ref": "subject_d0000005",
                "privacy_class": "private_local",
                "evidence_kind": "file_saved",
                "state": "artifact_saved",
                "provider_receipt_ref": "",
                "verification_scope": "operational",
                "memory_eligible": False,
                "signature": "",
            }
            payload["signature"] = sign_contract_payload(payload, secret)
            return ingest_controlled_evidence(
                self.conn,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                verification_secret=secret,
            )

        internal_only = attempt_controlled_file(
            mapping_v1,
            idempotency_key="idem_d9100001",
            source_ref="source_d9100002",
        )
        self.assertEqual(
            (internal_only["status"], internal_only["reason_code"]),
            ("rejected", "principal_kind_not_authorized"),
        )

        # Simulate an existing pre-fix mapping row whose principal was typed as a
        # public file_adapter. Mapping membership still denies the controlled API.
        self.conn.execute(
            "UPDATE context_principals SET principal_kind='file_adapter' "
            "WHERE principal_id=? AND key_id=?",
            (mapping_v1["principal_id"], mapping_v1["key_id"]),
        )
        self.conn.commit()
        legacy_row_denied = attempt_controlled_file(
            mapping_v1,
            idempotency_key="idem_d9100003",
            source_ref="source_d9100004",
        )
        self.assertEqual(
            (legacy_row_denied["status"], legacy_row_denied["reason_code"]),
            ("rejected", "document_mapping_principal_internal_only"),
        )

        path = self.watch / "mapping-principal-source.md"
        path.write_text("baseline", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        path.write_text("changed", encoding="utf-8")
        self.assertTrue(
            process_document_event(
                self.conn,
                self._event(path, 465),
                root_path=self.watch,
            )["recorded"]
        )
        source_graph = self.conn.execute(
            "SELECT observation_ref,candidate_ref FROM context_document_source_versions "
            "WHERE root_id=? AND status='active'",
            (mapping_v1["root_id"],),
        ).fetchone()

        mapping_v2 = register_document_context_mapping(
            self.conn,
            mapping_ref=mapping_v1["mapping_ref"],
            mapping_version=2,
            root_id=mapping_v1["root_id"],
            project_ref="project_d0000001",
            workstream_ref="workstream_d0000002",
            object_ref="object_d0000003",
            version_ref="version_d0000004",
            subject_ref="subject_d0000005",
        )
        self.assertEqual(mapping_v2["status"], "registered")
        self.assertEqual(mapping_v2["superseded_principal_count"], 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_principals WHERE principal_id=? AND key_id=?",
                (mapping_v1["principal_id"], mapping_v1["key_id"]),
            ).fetchone()[0],
            "revoked",
        )
        # Supersede keeps the already-observed local document history until an
        # explicit source/root withdrawal, while the old capability is dead.
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_observations WHERE observation_ref=?",
                (source_graph["observation_ref"],),
            ).fetchone()[0],
            "active",
        )
        self.assertEqual(
            attempt_controlled_file(
                mapping_v1,
                idempotency_key="idem_d9100005",
                source_ref="source_d9100006",
            )["reason_code"],
            "key_revoked",
        )
        self.assertEqual(
            attempt_controlled_file(
                mapping_v2,
                idempotency_key="idem_d9100007",
                source_ref="source_d9100008",
            )["reason_code"],
            "principal_kind_not_authorized",
        )

        # Simulate a database produced by the rejected candidate: a superseded
        # mapping row whose legacy file_adapter principal was never revoked.
        self.conn.execute(
            "UPDATE context_principals SET status='active',revoked_at=NULL "
            "WHERE principal_id=? AND key_id=?",
            (mapping_v1["principal_id"], mapping_v1["key_id"]),
        )
        self.conn.commit()
        self.assertEqual(
            attempt_controlled_file(
                mapping_v1,
                idempotency_key="idem_d9100011",
                source_ref="source_d9100012",
            )["reason_code"],
            "document_mapping_principal_internal_only",
        )

        revoked = revoke_document_context_root(self.conn, root_id=mapping_v1["root_id"])
        self.assertEqual(revoked["status"], "completed")
        self.assertEqual(revoked["document_mapping_principals_revoked"], 2)
        for mapping in (mapping_v1, mapping_v2):
            self.assertEqual(
                self.conn.execute(
                    "SELECT status FROM context_principals WHERE principal_id=? AND key_id=?",
                    (mapping["principal_id"], mapping["key_id"]),
                ).fetchone()[0],
                "revoked",
            )
        self.assertEqual(
            attempt_controlled_file(
                mapping_v2,
                idempotency_key="idem_d9100009",
                source_ref="source_d9100010",
            )["reason_code"],
            "key_revoked",
        )
        for table, ref_column, ref in (
            ("context_observations", "observation_ref", source_graph["observation_ref"]),
            ("context_candidates", "candidate_ref", source_graph["candidate_ref"]),
        ):
            self.assertEqual(
                self.conn.execute(
                    f"SELECT status FROM {table} WHERE {ref_column}=?",
                    (ref,),
                ).fetchone()[0],
                "revoked",
                table,
            )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM context_verified").fetchone()[0], 0)

    def test_document_projection_failure_rolls_back_source_event_and_all_derived_rows(self):
        self._configure_explicit_context_mapping()
        fault_tables = (
            "file_change_events",
            "context_observations",
            "context_evidence",
            "context_candidates",
            "context_source_events",
            "context_document_source_versions",
        )
        paths = []
        for index, table in enumerate(fault_tables):
            path = self.watch / f"fault-{index}.md"
            path.write_text(f"baseline-{table}", encoding="utf-8")
            paths.append(path)
        build_document_baseline(self.conn, self.watch)

        counted_tables = (
            "file_change_events",
            "context_source_events",
            "context_observations",
            "context_evidence",
            "context_candidates",
            "context_document_source_versions",
        )
        for index, (table, path) in enumerate(zip(fault_tables, paths, strict=True)):
            with self.subTest(fault_table=table):
                before = {
                    name: self.conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                    for name in counted_tables
                }
                old_hmac = str(
                    self.conn.execute(
                        "SELECT content_hmac FROM file_watch_state WHERE file_name_redacted=?",
                        (path.name,),
                    ).fetchone()[0]
                )
                trigger = f"document_projection_fault_{index}"
                self.conn.execute(
                    f"CREATE TRIGGER {trigger} BEFORE INSERT ON {table} "
                    "BEGIN SELECT RAISE(ABORT,'synthetic projection fault'); END"
                )
                self.conn.commit()
                path.write_text(f"changed-{table}", encoding="utf-8")
                event = self._event(path, 440 + index)
                failed = process_document_event(self.conn, event, root_path=self.watch)
                self.assertEqual((failed["status"], failed["recorded"]), ("failed", False))
                after_failure = {
                    name: self.conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                    for name in counted_tables
                }
                self.assertEqual(after_failure, before)
                self.assertEqual(
                    self.conn.execute(
                        "SELECT content_hmac FROM file_watch_state WHERE file_name_redacted=?",
                        (path.name,),
                    ).fetchone()[0],
                    old_hmac,
                )

                self.conn.execute(f"DROP TRIGGER {trigger}")
                self.conn.commit()
                retried = process_document_event(self.conn, event, root_path=self.watch)
                self.assertTrue(retried["recorded"])
                after_retry = {
                    name: self.conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                    for name in counted_tables
                }
                for name in counted_tables:
                    self.assertEqual(after_retry[name], before[name] + 1, name)
                replay = process_document_event(self.conn, event, root_path=self.watch)
                self.assertFalse(replay["recorded"])
                self.assertIn(
                    replay["reason"],
                    {"event_replay", "same_content_event", "metadata_only_no_content_change"},
                )
                self.assertEqual(
                    {
                        name: self.conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                        for name in counted_tables
                    },
                    after_retry,
                )

    def test_owner_local_observation_and_trusted_export_are_versioned_separate_projections(self):
        self._configure_explicit_context_mapping()
        path = self.watch / "projection-local-only.md"
        path.write_text("baseline", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        path.write_text("owner local cleaned text only", encoding="utf-8")
        self.assertTrue(process_document_event(self.conn, self._event(path, 460), root_path=self.watch)["recorded"])
        document_evidence_ref = str(
            self.conn.execute(
                "SELECT evidence_ref FROM context_document_source_versions WHERE status='active'"
            ).fetchone()[0]
        )
        trusted = self._attach_trusted_document_dependency(
            document_evidence_ref=document_evidence_ref,
            source_ref="source_d0000007",
            idempotency_key="idem_d0000010",
        )
        self.assertTrue(str(trusted["verified_ref"]).startswith("verified_"))

        projections = document_context_projections(
            self.conn,
            "2000-01-01T00:00:00+00:00",
            "2100-01-01T00:00:00+00:00",
        )
        local = projections["owner_local_projection"]
        exported = projections["trusted_projection"]
        self.assertEqual(local["contract_version"], "DocumentOwnerLocalObservationV2")
        self.assertEqual(exported["contract_version"], "DocumentTrustedExportV1")
        self.assertEqual((local["source"], local["projection_type"]), (
            "document_owner_local_observation", "owner_local_observation"
        ))
        self.assertEqual((exported["source"], exported["projection_type"]), (
            "context_trusted_export", "trusted_verified_context"
        ))
        self.assertEqual(local["verification_state"], "unverified")
        self.assertFalse(local["trusted_export_eligible"])
        self.assertEqual(exported["verification_state"], "verified_only")
        self.assertTrue(exported["trusted_export_eligible"])
        self.assertGreaterEqual(len(local["items"]), 1)
        self.assertEqual(len(exported["items"]), 1)
        self.assertTrue(all("verified_ref" not in item for item in local["items"]))
        self.assertTrue(all(str(item.get("context_ref") or "").startswith("context_") for item in exported["items"]))
        self.assertNotIn(trusted["verified_ref"], json.dumps(exported, ensure_ascii=False, sort_keys=True))
        exported_text = json.dumps(exported, ensure_ascii=False, sort_keys=True)
        self.assertNotIn(str(self.watch), exported_text)
        self.assertNotIn(path.name, exported_text)
        self.assertNotIn("owner local cleaned text only", exported_text)
        self.assertNotIn("change_summary_redacted", exported_text)

        review = daily_review(
            self.conn,
            "2000-01-01T00:00:00+00:00",
            "2100-01-01T00:00:00+00:00",
        )
        self.assertEqual(
            review["document_changes"]["contract_version"],
            "DocumentOwnerLocalObservationV2",
        )
        self.assertEqual(
            review["document_trusted_projection"]["contract_version"],
            "DocumentTrustedExportV1",
        )
        self.assertGreaterEqual(review["document_unverified_clue_count"], 1)
        owner_local_text = json.dumps(review["document_changes"], ensure_ascii=False)
        self.assertIn(path.name, owner_local_text)
        self.assertIn("change_summary_redacted", owner_local_text)

        handoff = build_ai_handoff_package(
            self.conn,
            range_name="today",
            start="2000-01-01T00:00:00+00:00",
            end="2100-01-01T00:00:00+00:00",
        )
        connector = build_ai_connector_package(self.conn, range_name="today")
        report = report_context(self.conn, datetime.now().astimezone().date().isoformat())
        for surface in (handoff, connector, report):
            with self.subTest(surface=surface.get("interface") or "report_context"):
                trusted_documents = surface["document_changes"]
                self.assertEqual(trusted_documents["contract_version"], "DocumentTrustedExportV1")
                self.assertEqual(trusted_documents["verification_state"], "verified_only")
                self.assertTrue(trusted_documents["trusted_export_eligible"])
                if surface.get("package_mode") != "connector_lightweight":
                    self.assertGreaterEqual(len(trusted_documents["items"]), 1)
                serialized = json.dumps(surface, ensure_ascii=False, sort_keys=True)
                self.assertNotIn(path.name, serialized)
                self.assertNotIn("owner local cleaned text only", serialized)
                self.assertNotIn("change_summary_redacted", serialized)
                self.assertNotIn(str(self.watch), serialized)

    def test_document_trusted_projection_excludes_unrelated_verified_contexts(self):
        self._configure_explicit_context_mapping()
        path = self.watch / "document-trusted-subset.md"
        path.write_text("baseline", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        path.write_text("changed", encoding="utf-8")
        self.assertTrue(
            process_document_event(
                self.conn,
                self._event(path, 466),
                root_path=self.watch,
            )["recorded"]
        )
        document_evidence_ref = str(
            self.conn.execute(
                "SELECT evidence_ref FROM context_document_source_versions WHERE status='active'"
            ).fetchone()[0]
        )
        unrelated = self._ingest_trusted_confirmation(
            source_ref="source_d9200001",
            idempotency_key="idem_d9200002",
        )
        related = self._attach_trusted_document_dependency(
            document_evidence_ref=document_evidence_ref,
            source_ref="source_d9200003",
            idempotency_key="idem_d9200004",
        )

        generic = build_context_bundle(
            self.conn,
            start_at="2000-01-01T00:00:00+00:00",
            end_at="2100-01-01T00:00:00+00:00",
            include_clues=False,
        )
        generic_sources = {
            str(item["source_ref"]) for item in generic.get("verified_contexts") or []
        }
        self.assertIn("source_d9200001", generic_sources)
        self.assertIn("source_d9200003", generic_sources)
        self.assertTrue(
            all(
                str(item["context_ref"]).startswith("context_")
                for item in generic.get("verified_contexts") or []
            )
        )

        projection = document_context_projections(
            self.conn,
            "2000-01-01T00:00:00+00:00",
            "2100-01-01T00:00:00+00:00",
        )["trusted_projection"]
        projected_sources = [str(item["source_ref"]) for item in projection["items"]]
        self.assertEqual(projected_sources, ["source_d9200003"])
        self.assertTrue(all(str(item["context_ref"]).startswith("context_") for item in projection["items"]))
        serialized = json.dumps(projection, ensure_ascii=False, sort_keys=True)
        self.assertNotIn(unrelated["verified_ref"], serialized)
        self.assertNotIn(related["verified_ref"], serialized)
        self.assertNotIn(path.name, serialized)
        self.assertNotIn(str(self.watch), serialized)

    def test_debounced_saves_preserve_each_observed_content_version(self):
        self._configure_explicit_context_mapping()
        path = self.watch / "debounced.md"
        path.write_text("version zero", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        from activity_mvp import document_changes as module

        original_snapshot = module._snapshot
        first_snapshot_captured = threading.Event()

        def observe_snapshot(conn, root, candidate_path, *, allow_content=True, content_allowed_check=None):
            snapshot = original_snapshot(
                conn,
                root,
                candidate_path,
                allow_content=allow_content,
                content_allowed_check=content_allowed_check,
            )
            if Path(candidate_path) == path:
                first_snapshot_captured.set()
            return snapshot

        with mock.patch("activity_mvp.document_changes._snapshot", side_effect=observe_snapshot):
            worker, controlled = self._start_controlled_worker()
            try:
                first_snapshot_captured.clear()
                path.write_text("version one", encoding="utf-8")
                controlled.emit(self._event(path, 280))
                self.assertTrue(first_snapshot_captured.wait(timeout=5))
                path.write_text("version two", encoding="utf-8")
                controlled.emit(self._event(path, 281))
                observed = self._wait_until(
                    lambda: self.conn.execute(
                        "SELECT COALESCE(MAX(version_count),0) FROM file_change_events WHERE change_kind='modified'"
                    ).fetchone()[0]
                    >= 2,
                    timeout=12,
                )
                self.assertTrue(observed, "debounce collapsed two confirmed content versions into one")
                source_versions = self.conn.execute(
                    "SELECT helper_event_ref,content_version_ref,document_source_ref FROM "
                    "context_document_source_versions ORDER BY created_at,document_source_ref"
                ).fetchall()
                self.assertEqual(len(source_versions), 2)
                self.assertEqual(len({row["helper_event_ref"] for row in source_versions}), 2)
                self.assertEqual(len({row["content_version_ref"] for row in source_versions}), 2)
                self.assertEqual(len({row["document_source_ref"] for row in source_versions}), 2)
                self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM context_verified").fetchone()[0], 0)
            finally:
                self._stop_worker(worker)

    def test_unstable_arrival_candidate_never_records_partial_content(self):
        path = self.watch / "stable.md"
        path.write_text("# Zero\n", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        worker, controlled = self._start_controlled_worker()
        try:
            path.write_text("# Partial\n", encoding="utf-8")
            controlled.emit(self._event(path, 290))
            time.sleep(0.2)
            path.write_text("# Final\n", encoding="utf-8")
            observed = self._wait_until(
                lambda: self.conn.execute(
                    "SELECT COUNT(*) FROM file_change_events WHERE change_kind='modified'"
                ).fetchone()[0] == 1,
                timeout=9,
            )
            self.assertTrue(observed)
            summary = self.conn.execute(
                "SELECT change_summary_redacted FROM file_change_events WHERE change_kind='modified'"
            ).fetchone()[0]
            self.assertIn("Final", summary)
            self.assertNotIn("Partial", summary)
        finally:
            self._stop_worker(worker)

    def test_persistently_unstable_file_is_unknown_without_root_coverage_gap(self):
        path = self.watch / "office-save.docx"
        path.write_bytes(b"stable-before")
        build_document_baseline(self.conn, self.watch)
        from activity_mvp import document_changes as module

        original_process = module.process_document_event

        def always_unstable(conn, event, **kwargs):
            if Path(str(event.get("path") or "")) == path:
                return {
                    "recorded": False,
                    "reason": "unstable_or_unreadable",
                    "raw_access": False,
                }
            return original_process(conn, event, **kwargs)

        with mock.patch(
            "activity_mvp.document_changes.process_document_event",
            side_effect=always_unstable,
        ):
            worker, controlled = self._start_controlled_worker()
            try:
                path.write_bytes(b"still-being-saved")
                controlled.emit(self._event(path, 291))
                self.assertTrue(
                    self._wait_until(
                        lambda: self.conn.execute(
                            "SELECT COUNT(*) FROM file_change_versions "
                            "WHERE helper_event_id='291' AND coverage_state='file_unresolved'"
                        ).fetchone()[0]
                        == 1,
                        timeout=12,
                    )
                )
                self.assertTrue(
                    self._wait_until(
                        lambda: document_changes_status(self.conn)["coverage"][
                            "pending_event_count"
                        ]
                        == 0,
                        timeout=3,
                    ),
                    "unresolved event was recorded before its pending counter was released",
                )
                coverage = document_changes_status(self.conn)["coverage"]
                gaps = [
                    tuple(row)
                    for row in self.conn.execute(
                        "SELECT reason_code,affects_attribution,helper_event_id "
                        "FROM file_watch_coverage_gaps ORDER BY created_at"
                    ).fetchall()
                ]
                self.assertTrue(coverage["is_complete"], (coverage, gaps))
                self.assertEqual(coverage["event_gap_count"], 0)
                self.assertEqual(gaps, [])
                self.assertFalse(controlled.terminated)
                version = self.conn.execute(
                    "SELECT actor_source,actor_reason_codes FROM file_change_versions "
                    "WHERE helper_event_id='291'"
                ).fetchone()
                self.assertEqual(version["actor_source"], "unattributed")
                self.assertIn(
                    "file_snapshot_unresolved",
                    json.loads(version["actor_reason_codes"]),
                )
                listed = self._all_changes()
                self.assertEqual(listed["totals"]["unattributed"], 1)
                self.assertEqual(listed["totals"]["ai_only"], 0)
                self.assertEqual(listed["totals"]["user_modified"], 0)
            finally:
                self._stop_worker(worker)

    # -- Superseded-entry merging in the live processor (tightened plan B) --

    def _ai_created_file_with_receipts(self, name: str) -> tuple[Path, Path, str, str]:
        """An AI-created file that is ai_only before the worker starts."""

        build_document_baseline(self.conn, self.watch, root_label="Work")
        path = self.watch / name
        base = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=1)
        path.write_text("AI created\n", encoding="utf-8")
        os.utime(path, (base.timestamp(), base.timestamp()))
        root_id = str(self.conn.execute("SELECT root_id FROM file_watch_roots").fetchone()[0])
        file_id = document_changes_module._file_id(self.conn, self.watch, path)
        log = self.root / "ai-edits.jsonl"
        log.write_text(
            json.dumps(
                {
                    "at": base.isoformat(),
                    "tool": "codex",
                    "action": "apply_patch",
                    "operation": "created",
                    "root_id": root_id,
                    "file_id": file_id,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return path, log, root_id, file_id

    @staticmethod
    def _append_ai_receipt(log: Path, at: datetime, root_id: str, file_id: str) -> None:
        with log.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "at": at.isoformat(),
                        "tool": "codex",
                        "action": "apply_patch",
                        "operation": "write",
                        "root_id": root_id,
                        "file_id": file_id,
                    }
                )
                + "\n"
            )

    def _run_ai_file_edit_scenario(self, *, with_superseded_event: bool) -> dict:
        path, log, root_id, file_id = self._ai_created_file_with_receipts(
            "merged-ai.md" if with_superseded_event else "single-ai.md"
        )
        diagnostics: list[dict] = []
        with (
            mock.patch.object(document_changes_module, "AI_EDIT_LOG_PATH", log),
            mock.patch.object(document_changes_module, "AI_EDIT_ROTATED_LOG_PATH", self.root / "none"),
            mock.patch.object(document_changes_module, "DOCUMENT_CHANGE_MERGE_DIAGNOSTIC_INTERVAL_SECONDS", 0.0),
        ):
            created = process_document_event(
                self.conn,
                self._event(path, 30, flags=["item_created"],
                            observed_at=datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()),
                root_path=self.watch,
            )
            self.assertTrue(created["recorded"])
            self.assertEqual(self._all_changes()["totals"]["ai_only"], 1)
            created_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            worker, controlled = self._start_controlled_worker(diagnostic_sink=diagnostics.append)
            try:
                # Later edits must sort after the (deliberately future) create.
                while datetime.now(timezone.utc) <= created_at + timedelta(seconds=1):
                    time.sleep(0.05)
                if with_superseded_event:
                    # An unobserved intermediate write (no AI receipt for it).
                    path.write_text("someone else's intermediate write\n", encoding="utf-8")
                    controlled.emit(self._event(path, 300))
                    time.sleep(0.2)
                ai_at = datetime.now(timezone.utc)
                path.write_text("AI final write\n", encoding="utf-8")
                self._append_ai_receipt(log, ai_at, root_id, file_id)
                controlled.emit(self._event(path, 301, observed_at=ai_at.isoformat()))
                self.assertTrue(
                    self._wait_until(
                        lambda: self.conn.execute(
                            "SELECT COUNT(*) FROM file_change_versions WHERE file_id=?",
                            (file_id,),
                        ).fetchone()[0] >= 2
                        and document_changes_status(self.conn)["coverage"]["pending_event_count"] == 0
                        and worker["merge_state"]()["latest_path_count"] == 0,
                        timeout=15,
                    )
                )
                time.sleep(0.3)
                merge_state = worker["merge_state"]()
                versions = [
                    dict(row)
                    for row in self.conn.execute(
                        "SELECT helper_event_id,coverage_state,observed_version_count,actor_source "
                        "FROM file_change_versions WHERE file_id=? ORDER BY changed_at,id",
                        (file_id,),
                    ).fetchall()
                ]
                listing = self._all_changes()
            finally:
                self._stop_worker(worker)
        return {
            "versions": versions,
            "listing": listing,
            "merge_state": merge_state,
            "diagnostics": diagnostics,
        }

    def test_newer_queued_event_supersedes_older_entry_and_forces_merged_version_unattributed(self):
        result = self._run_ai_file_edit_scenario(with_superseded_event=True)
        # The intermediate write had no AI receipt; the file must not be
        # reported as AI-only just because the final write did.
        self.assertEqual(result["listing"]["totals"]["ai_only"], 0, result)
        by_event = {row["helper_event_id"]: row for row in result["versions"]}
        # The superseded entry did no content work and wrote no version.
        self.assertNotIn("300", by_event)
        merged = by_event["301"]
        self.assertEqual(merged["coverage_state"], "file_unresolved")
        self.assertEqual(merged["observed_version_count"], 2)
        totals = result["listing"]["totals"]
        self.assertEqual(totals["ai_only"], 0, result)
        self.assertEqual(totals["unattributed"], 1, result)
        item = result["listing"]["items"][0]
        self.assertEqual(item["actor_classification"], "unattributed")
        self.assertIn("file_event_evidence_incomplete", item["attribution_reason_codes"])
        # Both maps are pruned once the latest entry for the file is handled.
        self.assertEqual(result["merge_state"]["latest_path_count"], 0)
        self.assertEqual(result["merge_state"]["debt_path_count"], 0)
        self.assertEqual(result["merge_state"]["superseded_skipped_count"], 1)
        self.assertEqual(result["merge_state"]["merged_version_marked_count"], 1)
        summaries = [
            payload for payload in result["diagnostics"]
            if payload.get("event") == "document_change_processor_merge_summary"
        ]
        self.assertTrue(summaries)
        self.assertEqual(summaries[-1]["superseded_skipped_count"], 1)
        self.assertEqual(summaries[-1]["merged_version_marked_count"], 1)
        # Counts only: no path, file name, root, or event identity.
        self.assertEqual(
            set(summaries[-1]),
            {
                "event",
                "superseded_skipped_count",
                "merged_version_marked_count",
                "slow_stability_entry_count",
                "queue_depths",
                "raw_access",
            },
        )
        rendered = json.dumps(result["diagnostics"], ensure_ascii=False)
        self.assertNotIn("merged-ai", rendered)
        self.assertNotIn(str(self.watch), rendered)

    def test_single_queued_event_is_processed_unchanged_without_merge(self):
        result = self._run_ai_file_edit_scenario(with_superseded_event=False)
        by_event = {row["helper_event_id"]: row for row in result["versions"]}
        self.assertEqual(by_event["301"]["coverage_state"], "continuous")
        self.assertEqual(by_event["301"]["observed_version_count"], 1)
        self.assertEqual(by_event["301"]["actor_source"], "ai_tool")
        self.assertEqual(result["listing"]["totals"]["ai_only"], 1, result)
        self.assertEqual(result["merge_state"]["superseded_skipped_count"], 0)
        self.assertEqual(result["merge_state"]["merged_version_marked_count"], 0)
        self.assertEqual(result["merge_state"]["latest_path_count"], 0)
        self.assertFalse(
            [
                payload for payload in result["diagnostics"]
                if payload.get("event") == "document_change_processor_merge_summary"
            ]
        )

    def test_merge_debt_survives_same_content_outcome_until_next_recorded_version(self):
        path = self.watch / "debt.md"
        path.write_text("v0\n", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        original_process = document_changes_module.process_document_event
        seen_counts: dict[str, int] = {}

        def same_content_for_301(conn, event, **kwargs):
            if str(event.get("event_id")) in {"301", "302"}:
                seen_counts[str(event.get("event_id"))] = int(
                    event.get("_superseded_merged_count") or 0
                )
            if str(event.get("event_id")) == "301":
                return {
                    "recorded": False,
                    "reason": "metadata_only_no_content_change",
                    "raw_access": False,
                }
            return original_process(conn, event, **kwargs)

        with mock.patch(
            "activity_mvp.document_changes.process_document_event",
            side_effect=same_content_for_301,
        ):
            worker, controlled = self._start_controlled_worker()
            try:
                path.write_text("v1\n", encoding="utf-8")
                controlled.emit(self._event(path, 300))
                time.sleep(0.2)
                path.write_text("v2\n", encoding="utf-8")
                controlled.emit(self._event(path, 301))
                self.assertTrue(
                    self._wait_until(
                        lambda: worker["merge_state"]()["superseded_skipped_count"] == 1
                        and worker["merge_state"]()["latest_path_count"] == 0
                        and document_changes_status(self.conn)["coverage"]["pending_event_count"] == 0,
                        timeout=10,
                    )
                )
                self.assertEqual(seen_counts.get("301"), 1)
                # Same-content outcome: no version written, debt retained.
                self.assertEqual(worker["merge_state"]()["debt_path_count"], 1)
                self.assertEqual(worker["merge_state"]()["merged_version_marked_count"], 0)
                path.write_text("v3\n", encoding="utf-8")
                controlled.emit(self._event(path, 302))
                self.assertTrue(
                    self._wait_until(
                        lambda: self.conn.execute(
                            "SELECT COUNT(*) FROM file_change_versions WHERE helper_event_id='302'"
                        ).fetchone()[0] == 1,
                        timeout=10,
                    )
                )
                self.assertTrue(
                    self._wait_until(
                        lambda: worker["merge_state"]()["debt_path_count"] == 0, timeout=3
                    )
                )
                self.assertEqual(seen_counts.get("302"), 1)
                version = self.conn.execute(
                    "SELECT coverage_state,observed_version_count FROM file_change_versions "
                    "WHERE helper_event_id='302'"
                ).fetchone()
                self.assertEqual(version["coverage_state"], "file_unresolved")
                self.assertEqual(version["observed_version_count"], 2)
                self.assertEqual(worker["merge_state"]()["merged_version_marked_count"], 1)
            finally:
                self._stop_worker(worker)

    def test_entry_with_validator_candidate_is_not_superseded(self):
        path = self.watch / "stable-pair.md"
        path.write_text("v0\n", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        worker, controlled = self._start_controlled_worker()
        try:
            # Both events see a file that does not change after they arrive,
            # so each one carries a validator candidate and is processed.
            path.write_text("v1\n", encoding="utf-8")
            controlled.emit(self._event(path, 310))
            time.sleep(1.5)
            path.write_text("v2 longer\n", encoding="utf-8")
            controlled.emit(self._event(path, 311))
            self.assertTrue(
                self._wait_until(
                    lambda: self.conn.execute(
                        "SELECT COUNT(*) FROM file_change_versions WHERE helper_event_id IN ('310','311')"
                    ).fetchone()[0] >= 1
                    and document_changes_status(self.conn)["coverage"]["pending_event_count"] == 0
                    and worker["merge_state"]()["latest_path_count"] == 0,
                    timeout=12,
                )
            )
            self.assertEqual(worker["merge_state"]()["superseded_skipped_count"], 0)
            states = {
                row[0]
                for row in self.conn.execute(
                    "SELECT coverage_state FROM file_change_versions WHERE helper_event_id IN ('310','311')"
                ).fetchall()
            }
            self.assertNotIn("file_unresolved", states)
        finally:
            self._stop_worker(worker)

    def _prepare_merge_recovery_origin(self):
        path, log, root_id, file_id = self._ai_created_file_with_receipts("queued-same.md")
        created_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        with (
            mock.patch.object(document_changes_module, "AI_EDIT_LOG_PATH", log),
            mock.patch.object(document_changes_module, "AI_EDIT_ROTATED_LOG_PATH", self.root / "none"),
        ):
            result = process_document_event(
                self.conn, self._event(path, 30, flags=["item_created"], observed_at=created_at.isoformat()),
                root_path=self.watch,
            )
            self.assertTrue(result["recorded"])
            self.assertEqual(self._all_changes()["totals"]["ai_only"], 1)
        while datetime.now(timezone.utc) <= created_at + timedelta(seconds=1):
            time.sleep(0.05)
        return path, log, root_id, file_id

    def test_merged_same_content_evidence_survives_real_worker_restart(self):
        path, log, root_id, file_id = self._prepare_merge_recovery_origin()
        fingerprint = self.conn.execute("SELECT content_hmac FROM file_watch_state WHERE file_id=?", (file_id,)).fetchone()[0]
        with (
            mock.patch.object(document_changes_module, "AI_EDIT_LOG_PATH", log),
            mock.patch.object(document_changes_module, "AI_EDIT_ROTATED_LOG_PATH", self.root / "none"),
        ):
            worker, controlled = self._start_controlled_worker()
            try:
                path.write_text("unobserved intermediate\n", encoding="utf-8")
                controlled.emit(self._event(path, 300))
                time.sleep(0.2)
                path.write_text("AI created\n", encoding="utf-8")
                controlled.emit(self._event(path, 301))
                self.assertTrue(self._wait_until(lambda: worker["merge_state"]()["superseded_skipped_count"] == 1
                    and worker["merge_state"]()["latest_path_count"] == 0
                    and document_changes_status(self.conn)["coverage"]["pending_event_count"] == 0))
                version = self.conn.execute("SELECT coverage_state,observed_version_count,detail_level FROM file_change_versions WHERE helper_event_id='301'").fetchone()
                self.assertIsNotNone(version)
                self.assertEqual(tuple(version), ("file_unresolved", 2, "metadata_only"))
                self.assertEqual(worker["merge_state"]()["debt_path_count"], 0)
                self.assertEqual(self.conn.execute("SELECT content_hmac FROM file_watch_state WHERE file_id=?", (file_id,)).fetchone()[0], fingerprint)
                self.assertIn("当前内容与前次相同", self._all_changes()["items"][0]["change_summary_redacted"])
            finally:
                self._stop_worker(worker)
            self.assertEqual(defer_document_change_startup_reconcile(self.conn, preserve_valid=True)["status"], "replay_pending")
            worker, controlled = self._start_controlled_worker()
            try:
                self.assertIn("--since", controlled.command)
                at = datetime.now(timezone.utc)
                path.write_text("later AI edit\n", encoding="utf-8")
                self._append_ai_receipt(log, at, root_id, file_id)
                controlled.emit(self._event(path, 302, observed_at=at.isoformat()))
                self.assertTrue(self._wait_until(lambda: self.conn.execute("SELECT COUNT(*) FROM file_change_versions WHERE helper_event_id='302'").fetchone()[0] == 1))
                listed = self._all_changes()
                self.assertEqual(listed["totals"]["ai_only"], 0)
                self.assertEqual(listed["totals"]["unattributed"], 1)
            finally:
                self._stop_worker(worker)

    def _run_merged_terminal_event(self, *, rename):
        path, log, root_id, file_id = self._prepare_merge_recovery_origin()
        with (
            mock.patch.object(document_changes_module, "AI_EDIT_LOG_PATH", log),
            mock.patch.object(document_changes_module, "AI_EDIT_ROTATED_LOG_PATH", self.root / "none"),
        ):
            worker, controlled = self._start_controlled_worker()
            try:
                path.write_text("unobserved intermediate\n", encoding="utf-8")
                controlled.emit(self._event(path, 300))
                time.sleep(0.2)
                if rename:
                    path.write_text("AI created\n", encoding="utf-8")
                    destination = path.with_name("queued-renamed.md")
                    path.rename(destination)
                    controlled.emit(self._event(path, 301, flags=["item_renamed"]))
                    controlled.emit(self._event(destination, 302, flags=["item_renamed"]))
                    terminal = "302"
                else:
                    path.unlink()
                    controlled.emit(self._event(path, 301, flags=["item_removed"]))
                    terminal = "301"
                self.assertTrue(self._wait_until(lambda: worker["merge_state"]()["superseded_skipped_count"] == 1
                    and worker["merge_state"]()["latest_path_count"] == 0
                    and self.conn.execute("SELECT COUNT(*) FROM file_change_versions WHERE helper_event_id=?", (terminal,)).fetchone()[0] == 1))
                version = self.conn.execute("SELECT change_kind,coverage_state,observed_version_count FROM file_change_versions WHERE helper_event_id=?", (terminal,)).fetchone()
                self.assertEqual(version["change_kind"], "renamed" if rename else "deleted")
                self.assertEqual(version["coverage_state"], "file_unresolved")
                self.assertGreaterEqual(version["observed_version_count"], 2)
                listed = self._all_changes()
                self.assertEqual(listed["totals"]["ai_only"], 0)
                self.assertEqual(listed["totals"]["unattributed"], 1)
            finally:
                self._stop_worker(worker)

    def test_merged_delete_evidence_blocks_ai_only(self):
        self._run_merged_terminal_event(rename=False)

    def test_merged_delete_rename_pair_preserves_unknown_intermediate_evidence(self):
        self._run_merged_terminal_event(rename=True)

    def _prepare_buffered_startup_state(self, pending=2):
        path, log, root_id, file_id = self._prepare_merge_recovery_origin()
        self.conn.execute("UPDATE file_watch_checkpoints SET last_event_id='301',coverage_status='complete' WHERE root_id=?", (root_id,))
        document_changes_module._set_document_change_pending(self.conn, pending)
        self.conn.commit()
        return path, log, root_id, file_id

    def test_startup_pending_entries_leave_durable_gap_and_keep_replay_cursor(self):
        path, log, root_id, file_id = self._prepare_buffered_startup_state()
        result = defer_document_change_startup_reconcile(self.conn, preserve_valid=True)
        self.assertEqual(result["status"], "deferred")
        saved = self.conn.execute("SELECT last_event_id,coverage_status FROM file_watch_checkpoints WHERE root_id=?", (root_id,)).fetchone()
        self.assertEqual(tuple(saved), ("301", "partial"))
        self.assertEqual(document_changes_status(self.conn)["coverage"]["pending_event_count"], 0)
        gap = self.conn.execute("SELECT affects_attribution FROM file_watch_coverage_gaps WHERE root_id=? AND reason_code='startup_pending_events'", (root_id,)).fetchone()
        self.assertIsNotNone(gap)
        self.assertEqual(gap[0], 1)
        build_document_baseline(self.conn, self.watch)
        document_changes_module.mark_document_root_reconciled(self.conn, root_id)
        with mock.patch.object(document_changes_module, "AI_EDIT_LOG_PATH", log):
            self.assertEqual(self._all_changes()["totals"]["ai_only"], 0)

    def test_pending_startup_commit_failure_keeps_original_count_cursor_and_no_gap(self):
        _, _, root_id, _ = self._prepare_buffered_startup_state()
        failing = sqlite3.connect(self.db, factory=_FailingCommitConnection)
        failing.row_factory = sqlite3.Row
        try:
            failing.fail_next_commit = True
            with self.assertRaises(OSError):
                defer_document_change_startup_reconcile(failing, preserve_valid=True)
            self.assertFalse(failing.in_transaction)
            self.assertEqual(document_changes_status(self.conn)["coverage"]["pending_event_count"], 2)
            saved = self.conn.execute("SELECT last_event_id,coverage_status FROM file_watch_checkpoints WHERE root_id=?", (root_id,)).fetchone()
            self.assertEqual(tuple(saved), ("301", "complete"))
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM file_watch_coverage_gaps WHERE reason_code='startup_pending_events'").fetchone()[0], 0)
        finally:
            failing.close()

    def test_merged_same_content_commit_failure_rolls_back_version_and_cursor(self):
        path, _, root_id, file_id = self._prepare_merge_recovery_origin()
        before = dict(self.conn.execute("SELECT * FROM file_watch_state WHERE file_id=?", (file_id,)).fetchone())
        failing = sqlite3.connect(self.db, factory=_FailingCommitConnection)
        failing.row_factory = sqlite3.Row
        try:
            failing.fail_next_commit = True
            event = {**self._event(path, 301), "_superseded_merged_count": 1}
            with self.assertRaises(OSError):
                process_document_event(failing, event, root_path=self.watch)
            failing.rollback()
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM file_change_versions WHERE helper_event_id='301'").fetchone()[0], 0)
            self.assertEqual(self.conn.execute("SELECT last_event_id FROM file_watch_checkpoints WHERE root_id=?", (root_id,)).fetchone()[0], "30")
            self.assertEqual(dict(self.conn.execute("SELECT * FROM file_watch_state WHERE file_id=?", (file_id,)).fetchone()), before)
        finally:
            failing.close()

    def test_interleaved_merged_file_survives_real_process_crash_after_other_cursor(self):
        cold = self.watch / "cold-entry.md"
        cold.write_text("before\n", encoding="utf-8")
        path, log, root_id, file_id = self._prepare_merge_recovery_origin()
        child_script = r'''
import json, os, sys, threading, time
from pathlib import Path
from unittest import mock
sys.path[:0] = [sys.argv[1], sys.argv[2]]
import test_document_changes as tests
from activity_mvp import document_changes as module
from activity_mvp.storage import ensure_ready
case = tests.DocumentChangeTests()
case.db = Path(sys.argv[3]); case.watch = Path(sys.argv[4]); case.root = case.watch.parent
case.conn = ensure_ready(case.db)
path = case.watch / "queued-same.md"; cold = case.watch / "cold-entry.md"
original = module.process_document_event
blocked = threading.Event()
def hold_final(conn, event, **kwargs):
    if str(event.get("event_id")) == "302":
        blocked.set()
        threading.Event().wait(60)
    return original(conn, event, **kwargs)
with mock.patch.object(module, "process_document_event", side_effect=hold_final):
    worker, controlled = case._start_controlled_worker()
    path.write_text("unobserved intermediate\n")
    controlled.emit(case._event(path, 300))
    time.sleep(0.2)
    cold.write_text("after\n")
    controlled.emit(case._event(cold, 301))
    path.write_text("AI created\n")
    controlled.emit(case._event(path, 302))
    if not blocked.wait(12): raise RuntimeError("final_entry_not_blocked")
    saved = case.conn.execute("SELECT last_event_id FROM file_watch_checkpoints").fetchone()[0]
    pending = module.document_changes_status(case.conn)["coverage"]["pending_event_count"]
    state = worker["merge_state"]()
    print(json.dumps({"cursor":saved,"pending":pending,"debt":state["debt_path_count"]}), flush=True)
    os._exit(0)
'''
        core = Path(__file__).resolve().parents[1]
        child = subprocess.run([sys.executable, "-B", "-c", child_script, str(core), str(core / "tests"),
            str(self.db), str(self.watch)], capture_output=True, text=True, timeout=25)
        self.assertEqual(child.returncode, 0, child.stderr)
        crash = json.loads(child.stdout)
        self.assertEqual(crash["cursor"], "301")
        self.assertGreater(crash["pending"], 0)
        self.assertEqual(crash["debt"], 1)
        self.assertEqual(defer_document_change_startup_reconcile(self.conn, preserve_valid=True)["status"], "deferred")
        self.assertEqual(self.conn.execute("SELECT last_event_id FROM file_watch_checkpoints WHERE root_id=?", (root_id,)).fetchone()[0], "301")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM file_watch_coverage_gaps WHERE reason_code='startup_pending_events' AND affects_attribution=1").fetchone()[0], 1)
        build_document_baseline(self.conn, self.watch)
        document_changes_module.mark_document_root_reconciled(self.conn, root_id)
        with (
            mock.patch.object(document_changes_module, "AI_EDIT_LOG_PATH", log),
            mock.patch.object(document_changes_module, "AI_EDIT_ROTATED_LOG_PATH", self.root / "none"),
        ):
            at = datetime.now(timezone.utc)
            path.write_text("later AI write\n", encoding="utf-8")
            self._append_ai_receipt(log, at, root_id, file_id)
            self.assertTrue(process_document_event(self.conn, self._event(path, 303, observed_at=at.isoformat()), root_path=self.watch)["recorded"])
            self.assertEqual(self._all_changes()["totals"]["ai_only"], 0)

    def test_failed_final_merged_projection_uses_existing_stream_recovery(self):
        path, _, root_id, file_id = self._prepare_merge_recovery_origin()
        from activity_mvp import context_console
        original_projection = context_console.record_document_change_context_locked

        def fail_final_projection(conn, **kwargs):
            final = conn.execute("SELECT 1 FROM file_change_versions WHERE helper_event_id='301' AND aggregate_event_id=?", (kwargs["source_event_id"],)).fetchone()
            if final is not None:
                return {"status": "failed", "reason_code": "document_context_projection_failed", "raw_access": False}
            return original_projection(conn, **kwargs)

        with mock.patch.object(context_console, "record_document_change_context_locked", side_effect=fail_final_projection):
            worker, controlled = self._start_controlled_worker()
            try:
                path.write_text("intermediate\n", encoding="utf-8")
                controlled.emit(self._event(path, 300))
                time.sleep(0.2)
                path.write_text("final content\n", encoding="utf-8")
                controlled.emit(self._event(path, 301))
                self.assertTrue(self._wait_until(lambda: worker["process"].stopping))
            finally:
                self._stop_worker(worker)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM file_change_versions WHERE helper_event_id='301'").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT last_event_id FROM file_watch_checkpoints WHERE root_id=?", (root_id,)).fetchone()[0], None)
        self.assertEqual(self.conn.execute("SELECT coverage_status FROM file_watch_checkpoints WHERE root_id=?", (root_id,)).fetchone()[0], "partial")
        self.assertGreater(self.conn.execute("SELECT COUNT(*) FROM file_watch_coverage_gaps WHERE reason_code='event_handler_exception' AND affects_attribution=1").fetchone()[0], 0)

    def test_failed_final_merged_rename_projection_uses_existing_stream_recovery(self):
        path, _, root_id, file_id = self._prepare_merge_recovery_origin()
        target = path.with_name("queued-renamed.md")
        original = path.read_text(encoding="utf-8")
        from activity_mvp import context_console
        original_projection = context_console.record_document_change_context_locked

        def fail_rename_projection(conn, **kwargs):
            rename = conn.execute("SELECT 1 FROM file_change_versions WHERE helper_event_id='302' AND aggregate_event_id=? AND change_kind='renamed'", (kwargs["source_event_id"],)).fetchone()
            if rename is not None:
                return {"status": "failed", "reason_code": "document_context_projection_failed", "raw_access": False}
            return original_projection(conn, **kwargs)

        with mock.patch.object(context_console, "record_document_change_context_locked", side_effect=fail_rename_projection):
            worker, controlled = self._start_controlled_worker()
            try:
                path.write_text("intermediate\n", encoding="utf-8")
                controlled.emit(self._event(path, 300))
                time.sleep(0.2)
                path.write_text(original, encoding="utf-8")
                path.rename(target)
                controlled.emit(self._event(path, 301, flags=["item_removed", "item_renamed"]))
                self.assertTrue(self._wait_until(lambda: self.conn.execute("SELECT 1 FROM file_change_versions WHERE helper_event_id='301'").fetchone() is not None))
                debt = self.conn.execute("SELECT coverage_state,observed_version_count FROM file_change_versions WHERE helper_event_id='301'").fetchone()
                self.assertEqual(debt["coverage_state"], "file_unresolved")
                self.assertGreaterEqual(debt["observed_version_count"], 2)
                controlled.emit(self._event(target, 302, flags=["item_created", "item_renamed"]))
                self.assertTrue(self._wait_until(lambda: worker["process"].stopping))
            finally:
                self._stop_worker(worker)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM file_change_versions WHERE helper_event_id='302'").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT coverage_state FROM file_change_versions WHERE helper_event_id='301'").fetchone()[0], "file_unresolved")
        self.assertIsNone(self.conn.execute("SELECT last_event_id FROM file_watch_checkpoints WHERE root_id=?", (root_id,)).fetchone()[0])
        self.assertEqual(self.conn.execute("SELECT coverage_status FROM file_watch_checkpoints WHERE root_id=?", (root_id,)).fetchone()[0], "partial")
        self.assertGreater(self.conn.execute("SELECT COUNT(*) FROM file_watch_coverage_gaps WHERE reason_code='event_handler_exception' AND affects_attribution=1").fetchone()[0], 0)

    def test_merged_unsettled_debt_survives_normal_stop_after_other_cursor(self):
        path, log, root_id, file_id = self._prepare_merge_recovery_origin()
        original_content = path.read_text(encoding="utf-8")
        cold = path.with_name("cold-stop.md")
        cold.write_text("before\n", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        original_process = document_changes_module.process_document_event
        processes = {}

        def stop_after_other_commit(conn, event, **kwargs):
            result = original_process(conn, event, **kwargs)
            if str(event.get("event_id")) == "301" and result.get("recorded"):
                processes["helper"].stop()
            return result

        with mock.patch.object(document_changes_module, "process_document_event", side_effect=stop_after_other_commit):
            worker, controlled = self._start_controlled_worker()
            processes["helper"] = worker["process"]
            try:
                path.write_text("unobserved intermediate\n", encoding="utf-8")
                controlled.emit(self._event(path, 300))
                time.sleep(0.2)
                cold.write_text("other final\n", encoding="utf-8")
                controlled.emit(self._event(cold, 301))
                path.write_text(original_content, encoding="utf-8")
                controlled.emit(self._event(path, 302))
                self.assertTrue(self._wait_until(lambda: worker["process"].stopping))
            finally:
                self._stop_worker(worker)
        self.assertEqual(worker["merge_state"]()["superseded_skipped_count"], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM file_change_versions WHERE helper_event_id='301'").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM file_change_versions WHERE helper_event_id='302'").fetchone()[0], 0)
        self.assertEqual(document_changes_status(self.conn)["coverage"]["pending_event_count"], 0)
        self.assertIsNone(self.conn.execute("SELECT last_event_id FROM file_watch_checkpoints WHERE root_id=?", (root_id,)).fetchone()[0])
        self.assertGreater(self.conn.execute("SELECT COUNT(*) FROM file_watch_coverage_gaps WHERE reason_code='merged_events_unsettled_at_stop' AND affects_attribution=1").fetchone()[0], 0)
        self.assertEqual(defer_document_change_startup_reconcile(self.conn, preserve_valid=True)["status"], "deferred")
        build_document_baseline(self.conn, self.watch)
        with (mock.patch.object(document_changes_module, "AI_EDIT_LOG_PATH", log),
              mock.patch.object(document_changes_module, "AI_EDIT_ROTATED_LOG_PATH", self.root / "none")):
            worker, controlled = self._start_controlled_worker()
            try:
                at = datetime.now(timezone.utc)
                path.write_text("later AI edit\n", encoding="utf-8")
                self._append_ai_receipt(log, at, root_id, file_id)
                controlled.emit(self._event(path, 303, observed_at=at.isoformat()))
                self.assertTrue(self._wait_until(lambda: self.conn.execute("SELECT COUNT(*) FROM file_change_versions WHERE helper_event_id='303'").fetchone()[0] == 1))
                self.assertEqual(self._all_changes()["totals"]["ai_only"], 0)
            finally:
                self._stop_worker(worker)

    def test_merged_rename_pairing_binds_same_second_provisional_identity(self):
        path, _, root_id, file_id = self._prepare_merge_recovery_origin()
        target = path.with_name("queued-renamed.md")
        path.rename(target)
        observed = datetime.now(timezone.utc).replace(microsecond=100000)
        removed = {**self._event(path, 301, flags=["item_removed"], observed_at=observed.isoformat()), "_superseded_merged_count": 2}
        self.assertTrue(process_document_event(self.conn, removed, root_path=self.watch)["recorded"])
        first = dict(self.conn.execute("SELECT * FROM file_change_events WHERE file_id=? AND change_kind='deleted'", (file_id,)).fetchone())
        original_version = dict(self.conn.execute("SELECT * FROM file_change_versions WHERE aggregate_event_id=?", (first["id"],)).fetchone())
        # Synthetic tie fixture: exercise identity conservation without claiming
        # the original second-based ordering picks one particular candidate.
        second = {**first, "id": "same-second-second-delete", "last_changed_at": observed.replace(microsecond=900000).isoformat()}
        second_version = {**original_version, "id": "same-second-second-version", "aggregate_event_id": second["id"], "helper_event_id": "302", "coverage_state": "continuous", "observed_version_count": 1}
        for table, row in (("file_change_events", second), ("file_change_versions", second_version)):
            columns = ",".join(row)
            marks = ",".join("?" for _ in row)
            self.conn.execute("INSERT INTO " + table + " (" + columns + ") VALUES (" + marks + ")", tuple(row.values()))
        self.conn.commit()
        before = {table: [tuple(row) for row in self.conn.execute("SELECT * FROM " + table + " ORDER BY 1")] for table in ("file_change_events", "file_change_versions", "file_watch_checkpoints", "file_watch_state")}
        renamed = self._event(target, 303, flags=["item_renamed"], observed_at=(observed + timedelta(seconds=1)).isoformat())
        from activity_mvp import context_console
        original_projection = context_console.record_document_change_context_locked
        def fail_rename(conn, **kwargs):
            if conn.execute("SELECT 1 FROM file_change_versions WHERE helper_event_id='303' AND change_kind='renamed'").fetchone():
                return {"status": "failed", "reason_code": "document_context_projection_failed", "raw_access": False}
            return original_projection(conn, **kwargs)
        with mock.patch.object(context_console, "record_document_change_context_locked", side_effect=fail_rename):
            self.assertEqual(process_document_event(self.conn, renamed, root_path=self.watch)["status"], "failed")
        after_failure = {table: [tuple(row) for row in self.conn.execute("SELECT * FROM " + table + " ORDER BY 1")] for table in before}
        self.assertEqual(before, after_failure)
        trace = []
        self.conn.set_trace_callback(trace.append)
        try:
            self.assertTrue(process_document_event(self.conn, renamed, root_path=self.watch)["recorded"])
        finally:
            self.conn.set_trace_callback(None)
        old_ids = {first["id"], second["id"]}
        present = {row[0] for row in self.conn.execute("SELECT id FROM file_change_events")}
        deleted = old_ids - present
        self.assertEqual(len(deleted), 1)
        selected = deleted.pop()
        selection_queries = [sql for sql in trace if sql.startswith("SELECT id FROM file_change_events WHERE file_id=") and "ORDER BY" in sql]
        self.assertEqual(len(selection_queries), 1)
        for sql in trace:
            if sql.startswith("DELETE FROM file_change_versions WHERE aggregate_event_id=") or sql.startswith("DELETE FROM file_change_events WHERE id="):
                self.assertIn(selected, sql)
                self.assertNotIn("ORDER BY", sql)
        untouched = original_version if selected == second["id"] else second_version
        remaining = self.conn.execute("SELECT coverage_state,observed_version_count FROM file_change_versions WHERE id=?", (untouched["id"],)).fetchone()
        self.assertEqual(tuple(remaining), (untouched["coverage_state"], untouched["observed_version_count"]))
        final = self.conn.execute("SELECT coverage_state,observed_version_count FROM file_change_versions WHERE helper_event_id='303'").fetchone()
        expected = ("file_unresolved", 4) if selected == first["id"] else ("continuous", 1)
        self.assertEqual(tuple(final), expected)
        self.assertEqual(self._all_changes()["totals"]["ai_only"], 0)

    def test_hot_file_does_not_starve_processor_throughput(self):
        hot = self.watch / "hot-append.log.md"
        hot.write_text("start\n", encoding="utf-8")
        cold_paths = []
        for index in range(30):
            cold = self.watch / f"cold-{index:02d}.md"
            cold.write_text("before\n", encoding="utf-8")
            cold_paths.append(cold)
        build_document_baseline(self.conn, self.watch)
        worker, controlled = self._start_controlled_worker()
        try:
            event_id = 400
            started = time.monotonic()
            for index in range(30):
                with hot.open("a", encoding="utf-8") as handle:
                    handle.write(f"line {index}\n")
                controlled.emit(self._event(hot, event_id))
                event_id += 1
                cold_paths[index].write_text(f"after {index}\n", encoding="utf-8")
                controlled.emit(self._event(cold_paths[index], event_id))
                event_id += 1
                time.sleep(0.1)
            # Before the fix every hot entry without a candidate cost at least
            # one 1-second stability sleep in the single FIFO processor (and up
            # to ~11 s while the file kept changing), so 30 hot entries could
            # not drain in under 30 s.
            drained = self._wait_until(
                lambda: document_changes_status(self.conn)["coverage"]["pending_event_count"] == 0
                and self.conn.execute(
                    "SELECT COUNT(DISTINCT file_id) FROM file_change_versions WHERE change_kind='modified'"
                ).fetchone()[0] == 31,
                timeout=15,
            )
            elapsed = time.monotonic() - started
            self.assertTrue(drained, worker["merge_state"]())
            self.assertLess(elapsed, 20.0)
            state = worker["merge_state"]()
            self.assertGreaterEqual(state["superseded_skipped_count"], 20, state)
            self.assertEqual(state["latest_path_count"], 0)
            self.assertEqual(state["debt_path_count"], 0)
            hot_id = document_changes_module._file_id(self.conn, self.watch, hot)
            hot_versions = self.conn.execute(
                "SELECT coverage_state,observed_version_count FROM file_change_versions "
                "WHERE file_id=? AND change_kind='modified'",
                (hot_id,),
            ).fetchall()
            self.assertTrue(hot_versions)
            self.assertTrue(
                all(row["coverage_state"] == "file_unresolved" for row in hot_versions)
            )
            self.assertEqual(
                sum(int(row["observed_version_count"]) for row in hot_versions), 30
            )
        finally:
            self._stop_worker(worker)

    def test_join_document_change_worker_reports_every_live_stage(self):
        names = ("reader", "consumer", "validator", "processor", "monitor")
        release = threading.Event()
        threads = {
            name: threading.Thread(
                target=release.wait,
                name=f"test-document-{name}",
                daemon=True,
            )
            for name in names
        }
        for thread in threads.values():
            thread.start()
        try:
            with self.assertRaises(TimeoutError) as raised:
                join_document_change_worker(threads, timeout_seconds=0.05)
            for name in names:
                self.assertIn(name, str(raised.exception))
        finally:
            release.set()
            joined = join_document_change_worker(threads, timeout_seconds=2)
        self.assertEqual(set(joined), set(names))
        self.assertTrue(all(not thread.is_alive() for thread in threads.values()))

    def test_processor_crash_with_full_queue_releases_pending_and_joins_every_stage(self):
        paths = []
        for index in range(4):
            path = self.watch / f"processor-crash-{index}.md"
            path.write_text("before", encoding="utf-8")
            paths.append(path)
        build_document_baseline(self.conn, self.watch)

        from activity_mvp import document_changes as module

        crash_observation: dict[str, int] = {}
        processor_entered = threading.Event()

        def crash_processor(*_args, **_kwargs):
            probe = ensure_ready(self.db)
            try:
                crash_observation["pending"] = document_changes_status(probe)["coverage"][
                    "pending_event_count"
                ]
            finally:
                probe.close()
            processor_entered.set()
            raise RuntimeError("simulated processor crash")

        worker: dict | None = None
        with (
            mock.patch.object(module, "DOCUMENT_CHANGE_PROCESSOR_QUEUE_MAXSIZE", 3),
            mock.patch(
                "activity_mvp.document_changes.process_document_event",
                side_effect=crash_processor,
            ),
        ):
            worker, controlled = self._start_controlled_worker()
            try:
                for event_id, path in enumerate(paths, start=3200):
                    path.write_text("after", encoding="utf-8")
                    controlled.emit(self._event(path, event_id))

                self.assertTrue(processor_entered.wait(timeout=8))
                # One entry is active in the processor and the other three fill
                # the deliberately tiny downstream queue at the crash boundary.
                self.assertEqual(crash_observation.get("pending"), 4)
                joined = join_document_change_worker(worker, timeout_seconds=10)
                self.assertEqual(
                    set(joined),
                    {"reader", "consumer", "validator", "processor", "monitor"},
                )
                self.assertTrue(
                    all(
                        not worker[name].is_alive()
                        for name in ("reader", "consumer", "validator", "processor", "monitor")
                    )
                )
            finally:
                if worker is not None and any(
                    worker.get(name) is not None and worker[name].is_alive()
                    for name in ("reader", "consumer", "validator", "processor", "monitor")
                ):
                    self._stop_worker(worker)

        self.conn.close()
        self.conn = ensure_ready(self.db)
        coverage = document_changes_status(self.conn)["coverage"]
        self.assertEqual(coverage["pending_event_count"], 0)
        self.assertEqual(coverage["coverage_status"], "partial")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM file_change_events").fetchone()[0], 0)

    def test_stopped_pipeline_cleanup_database_failure_still_joins_every_stage(self):
        path = self.watch / "cleanup-busy.md"
        path.write_text("before", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        module = document_changes_module
        real_queue = queue.Queue
        pipeline_queues = []
        worker = None
        cleanup_failed = threading.Event()

        def capture_queue(*args, **kwargs):
            result = real_queue(*args, **kwargs)
            if kwargs.get("maxsize") == 10000:
                pipeline_queues.append(result)
            return result

        real_adjust = module._adjust_document_change_pending

        def busy_cleanup(conn, delta, **kwargs):
            if delta < 0 and worker is not None and worker["process"].stopping and threading.current_thread().name == "pam-document-validator":
                cleanup_failed.set()
                raise sqlite3.OperationalError("synthetic cleanup database busy")
            return real_adjust(conn, delta, **kwargs)

        with mock.patch.object(module.queue, "Queue", side_effect=capture_queue):
            worker, controlled = self._start_controlled_worker()
        try:
            with mock.patch.object(module, "_adjust_document_change_pending", side_effect=busy_cleanup):
                path.write_text("after", encoding="utf-8")
                for event_id in range(3900, 3904):
                    controlled.emit(self._event(path, event_id))
                self.assertTrue(self._wait_until(lambda: document_changes_status(self.conn)["coverage"]["pending_event_count"] == 4))
                self.assertEqual(len(pipeline_queues), 3)
                self.assertTrue(self._wait_until(lambda: pipeline_queues[2].qsize() == 3))
                worker["process"].stop()
                self.assertTrue(cleanup_failed.wait(timeout=3))
                joined = join_document_change_worker(worker, timeout_seconds=5)
                self.assertEqual(set(joined), {"reader", "consumer", "validator", "processor", "monitor"})
            coverage = document_changes_status(self.conn)["coverage"]
            self.assertGreater(coverage["pending_event_count"], 0)
            self.assertEqual(coverage["coverage_status"], "partial")
            self.assertFalse(coverage["is_complete"])
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM file_change_events").fetchone()[0], 0)
        finally:
            # Old-code failure must not leave synthetic blocked Queue.get()
            # threads behind after reporting the failed regression.
            for target in pipeline_queues:
                try:
                    target.put_nowait(None)
                except queue.Full:
                    pass
            self._stop_worker(worker)


    def test_stopped_pending_handle_reports_real_sqlite_busy_without_erasing_count(self):
        from activity_mvp.storage import connect, connect_readonly
        from activity_mvp.worker_connections import WorkerConnections
        module = document_changes_module
        module._adjust_document_change_pending(self.conn, 1)
        self.conn.commit()
        pool = WorkerConnections(self.db, connect, connect_readonly)
        pending = pool.connect("pending")
        pool.stop_check = lambda: True
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            started = time.monotonic()
            with self.assertRaises(sqlite3.OperationalError) as raised:
                module._adjust_document_change_pending(pending, -1)
            self.assertEqual(raised.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(module._document_change_pending_count(self.conn), 1)
        finally:
            self.conn.rollback()
            pool.close()


    def test_resumed_barrier_and_fatal_reset_do_not_invert_locks(self):
        path = self.watch / "resume-drop.md"
        path.write_text("baseline", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        # A complete scan without a stream cursor takes the fresh-baseline
        # branch. This regression must exercise retained-cursor resume.
        self.conn.execute("UPDATE file_watch_checkpoints SET last_event_id='1'")
        self.conn.commit()
        real_lock = threading.Lock
        transition_entered = threading.Event()
        helper_stopped = threading.Event()
        forced_unlock = threading.Event()
        fatal_holds_baseline = threading.Event()
        holder = {}
        startup_thread = threading.get_ident()

        class BoundedRaceLock:
            def __init__(self, name):
                self.name = name
                self.lock = real_lock()
                self.owner = None
                holder[name] = self

            def __enter__(self):
                # The test-only bound breaks the old deadlock so it cannot leak
                # a pipeline. Production locks remain unbounded and fail closed.
                if not self.lock.acquire(timeout=2):
                    forced_unlock.set()
                    raise RuntimeError("synthetic lock-order deadlock")
                self.owner = threading.get_ident()
                if self.name == "baseline_lock" and self.owner != startup_thread:
                    fatal_holds_baseline.set()
                if self.name == "coverage_transition_lock" and self.owner == startup_thread:
                    transition_entered.set()
                    # If resume acquired transition first (old order), let
                    # fatal own baseline before resume tries it. If resume
                    # already owns baseline (fixed order), fatal must wait.
                    gate = helper_stopped if holder["baseline_lock"].owner == startup_thread else fatal_holds_baseline
                    if not gate.wait(timeout=2):
                        self.owner = None
                        self.lock.release()
                        raise RuntimeError("synthetic drop was not observed")
                return self

            def __exit__(self, *_args):
                self.owner = None
                self.lock.release()

        def instrument_lock(*args, **kwargs):
            frame = inspect.currentframe().f_back
            while frame is not None and frame.f_code.co_name != "start_document_change_worker":
                frame = frame.f_back
            line = linecache.getline(frame.f_code.co_filename, frame.f_lineno).strip() if frame is not None else ""
            if frame is not None:
                for name in ("baseline_lock", "coverage_transition_lock"):
                    if line.startswith(name + " = threading.Lock()"):
                        return BoundedRaceLock(name)
            return real_lock(*args, **kwargs)

        class DropAtResumeProcess(_ControlledProcess):
            def terminate(self):
                super().terminate()
                helper_stopped.set()

            def request_barrier(self, token):
                super().request_barrier(token)

                def emit_drop():
                    if transition_entered.wait(timeout=3):
                        # Reader-side protocol failure reaches fatal even while
                        # the consumer is paused at the resume barrier. A valid
                        # control event behind that barrier cannot do so.
                        self.stdout.emit({"synthetic_malformed_payload": True})

                emitter = threading.Thread(target=emit_drop, daemon=True)
                holder["emitter"] = emitter
                emitter.start()

        def factory(command, **kwargs):
            result = DropAtResumeProcess(command, **kwargs)
            holder["process"] = result
            return result

        with mock.patch.object(document_changes_module.threading, "Lock", side_effect=instrument_lock):
            worker = start_document_change_worker(
                self.db, ["/safe/helper"], popen_factory=factory,
                session_state_provider=lambda: {"state": "unlocked", "locked": False, "on_console": True, "capture_allowed": True},
            )
        try:
            self.assertIn("--since", holder["process"].command)
            self.assertTrue(transition_entered.is_set())
            self.assertTrue(helper_stopped.is_set())
            self.assertFalse(forced_unlock.is_set(), "resume held transition while waiting for fatal reset's baseline lock")
            self.assertFalse(worker["started"])
            self.assertEqual(set(join_document_change_worker(worker, timeout_seconds=4)), {"reader", "consumer", "validator", "processor"})
            self.assertTrue(all(row[0] is None for row in self.conn.execute("SELECT last_event_id FROM file_watch_checkpoints")))
            self.assertEqual(document_changes_status(self.conn)["coverage"]["coverage_status"], "partial")
        finally:
            holder["emitter"].join(timeout=3)
            self._stop_worker(worker)


    def test_raw_event_burst_uses_fixed_worker_threads(self):
        for iteration in range(10):
            path = self.watch / f"burst-{iteration}.md"
            path.write_text("before", encoding="utf-8")
            build_document_baseline(self.conn, self.watch)
            worker, controlled = self._start_controlled_worker()
            change_count_before = self.conn.execute(
                "SELECT COUNT(*) FROM file_change_events"
            ).fetchone()[0]
            try:
                before = len([
                    thread
                    for thread in threading.enumerate()
                    if thread.name.startswith("pam-document-")
                ])
                path.write_text("after", encoding="utf-8")
                first_event_id = 300 + (iteration * 200)
                for event_id in range(first_event_id, first_event_id + 200):
                    controlled.emit(self._event(path, event_id))
                self.assertTrue(
                    self._wait_until(
                        lambda: document_changes_status(self.conn)["coverage"][
                            "pending_event_count"
                        ]
                        > 0,
                        timeout=5,
                    ),
                    f"iteration {iteration} never admitted a pending event",
                )
                after = len([
                    thread
                    for thread in threading.enumerate()
                    if thread.name.startswith("pam-document-")
                ])
                self.assertLessEqual(after - before, 1)
                self.assertFalse(any(
                    thread.name.startswith("Thread-")
                    for thread in threading.enumerate()
                ))
            finally:
                self._stop_worker(worker)
            self.assertEqual(
                document_changes_status(self.conn)["coverage"]["pending_event_count"],
                0,
            )
            self.assertEqual(
                self.conn.execute("SELECT COUNT(*) FROM file_change_events").fetchone()[0],
                change_count_before,
                "a stopped worker committed a late document change",
            )

    def test_barriered_baseline_never_exposes_complete_during_scan(self):
        path = self.watch / "baseline-barrier.md"
        path.write_text("current", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        holder: dict[str, object] = {}
        scan_entered = threading.Event()
        release_scan = threading.Event()
        original_baseline = build_document_baseline

        def factory(command, **kwargs):
            process = _ControlledProcess(command, **kwargs)
            holder["process"] = process
            return process

        def blocked_baseline(*args, **kwargs):
            scan_entered.set()
            release_scan.wait(timeout=5)
            return original_baseline(*args, **kwargs)

        def run_worker():
            holder["worker"] = start_document_change_worker(
                self.db,
                ["/safe/helper"],
                popen_factory=factory,
                session_state_provider=lambda: {
                    "state": "unlocked", "locked": False,
                    "on_console": True, "capture_allowed": True,
                },
            )

        thread = threading.Thread(target=run_worker)
        with mock.patch("activity_mvp.document_changes.build_document_baseline", side_effect=blocked_baseline):
            thread.start()
            self.assertTrue(scan_entered.wait(timeout=3))
            self.assertFalse(document_changes_status(self.conn)["coverage"]["is_complete"])
            release_scan.set()
            thread.join(timeout=8)
        self.assertFalse(thread.is_alive())
        self.assertTrue(document_changes_status(self.conn)["coverage"]["is_complete"])
        self._stop_worker(holder["worker"])

    def test_barriered_delete_revalidates_candidate_revision_before_promotion(self):
        path = self.watch / "missing-at-barrier.md"
        path.write_text("before deletion", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        path.unlink()
        holder: dict[str, object] = {}
        finish_attempts = 0

        def factory(command, **kwargs):
            process = _ControlledProcess(command, **kwargs)
            holder["process"] = process
            return process

        def transition_hook(phase: str) -> None:
            nonlocal finish_attempts
            if phase != "before_finish_lock":
                return
            finish_attempts += 1
            self.assertEqual(
                self.conn.execute(
                    "SELECT COUNT(*) FROM file_watch_state "
                    "WHERE file_name_redacted='missing-at-barrier.md'"
                ).fetchone()[0],
                1,
            )
            if finish_attempts == 1:
                # Simulate a same-epoch event writer changing a missing-row
                # revision after this pass captured its deletion plan.
                self.conn.execute(
                    "UPDATE file_watch_state SET updated_at=? "
                    "WHERE file_name_redacted='missing-at-barrier.md'",
                    ("2099-01-01T00:00:00.000000+00:00",),
                )
                self.conn.commit()

        worker = start_document_change_worker(
            self.db,
            ["/safe/helper"],
            popen_factory=factory,
            session_state_provider=lambda: {
                "state": "unlocked",
                "locked": False,
                "on_console": True,
                "capture_allowed": True,
            },
            _transition_hook=transition_hook,
        )
        try:
            self.assertEqual(finish_attempts, 2)
            self.assertEqual(
                self.conn.execute(
                    "SELECT COUNT(*) FROM file_watch_state "
                    "WHERE file_name_redacted='missing-at-barrier.md'"
                ).fetchone()[0],
                0,
            )
            self.assertTrue(document_changes_status(self.conn)["coverage"]["is_complete"])
        finally:
            self._stop_worker(worker)

    def test_reconcile_window_loss_stops_partial_and_allows_later_retry(self):
        for index in range(4):
            (self.watch / f"resource-gate-{index}.md").write_text(
                f"stable {index}\n",
                encoding="utf-8",
            )
        build_document_baseline(self.conn, self.watch)
        from activity_mvp import document_changes as module

        resource_allowed = True
        eligibility_checks = 0
        snapshots = 0
        original_snapshot = module._snapshot

        def eligibility():
            nonlocal eligibility_checks
            eligibility_checks += 1
            return {"allowed": resource_allowed}

        def wake_after_first_snapshot(*args, **kwargs):
            nonlocal resource_allowed, snapshots
            result = original_snapshot(*args, **kwargs)
            snapshots += 1
            if snapshots == 1:
                resource_allowed = False
            return result

        with mock.patch(
            "activity_mvp.document_changes._snapshot",
            side_effect=wake_after_first_snapshot,
        ):
            worker, controlled = self._start_controlled_worker(
                reconcile_eligibility_provider=eligibility,
                reconcile_eligibility_check_interval_seconds=0,
            )
        try:
            self.assertFalse(worker["started"])
            self.assertEqual(worker["reason"], "reconcile_eligibility_lost")
            self.assertTrue(controlled.terminated)
            self.assertGreaterEqual(eligibility_checks, 2)
            coverage = document_changes_status(self.conn)["coverage"]
            self.assertEqual(coverage["coverage_status"], "partial")
            self.assertFalse(coverage["is_complete"])
            self.assertEqual(
                self.conn.execute("SELECT COUNT(*) FROM file_watch_state").fetchone()[0],
                4,
            )
        finally:
            self._stop_worker(worker)

        resource_allowed = True
        retry, _ = self._start_controlled_worker(
            reconcile_eligibility_provider=eligibility,
            reconcile_eligibility_check_interval_seconds=0,
        )
        try:
            self.assertTrue(retry["started"])
            self.assertTrue(document_changes_status(self.conn)["coverage"]["is_complete"])
        finally:
            self._stop_worker(retry)

    def test_baseline_progress_diagnostic_is_rate_bounded_and_content_free(self):
        from activity_mvp import document_changes as document_changes_module

        diagnostics: list[dict] = []
        private_path = self.watch / "private-progress-client-secret.md"
        private_path.write_text("private progress payload sentinel", encoding="utf-8")
        with mock.patch.object(
            document_changes_module,
            "DOCUMENT_BASELINE_DIAGNOSTIC_INTERVAL_SECONDS",
            3600.0,
        ):
            worker, _controlled = self._start_controlled_worker(
                diagnostic_sink=diagnostics.append
            )
        try:
            self.assertTrue(worker["started"])
            self.assertTrue(document_changes_status(self.conn)["coverage"]["is_complete"])
        finally:
            self._stop_worker(worker)

        progress = [
            item
            for item in diagnostics
            if item.get("event") == "document_change_baseline_progress"
        ]
        self.assertEqual(
            [item["stage"] for item in progress],
            ["started", "pass_finished", "completed"],
        )
        expected_fields = {
            "event",
            "observed_at",
            "pid",
            "scan_issue_counts",
            "root_scan_issue_counts",
            "snapshot_io_failure_details",
            "stage",
            "pass_number",
            "max_passes",
            "root_count",
            "root_scan_attempts_finished_count",
            "candidate_count",
            "files_indexed_count",
            "files_ignored_count",
            "files_failed_count",
            "files_written_count",
            "files_unchanged_count",
            "elapsed_ms",
            "session_probe_count",
            "session_probe_elapsed_ms",
            "authorization_cache_hit_count",
            "authorization_cache_miss_count",
            "authorization_db_probe_count",
            "authorization_db_probe_elapsed_ms",
            "reconcile_probe_count",
            "reconcile_probe_elapsed_ms",
            "enumeration_elapsed_ms",
            "candidate_total_elapsed_ms",
            "safe_path_elapsed_ms",
            "pre_snapshot_gate_inclusive_elapsed_ms",
            "snapshot_inclusive_elapsed_ms",
            "database_write_elapsed_ms",
            "batch_throttle_inclusive_elapsed_ms",
            "scan_throttle_elapsed_ms",
            "queue_depths",
            "helper_stopping",
            "epoch_active",
            "raw_access",
        }
        numeric_fields = expected_fields - {
            "event",
            "observed_at",
            "scan_issue_counts",
            "root_scan_issue_counts",
            "snapshot_io_failure_details",
            "stage",
            "queue_depths",
            "helper_stopping",
            "epoch_active",
            "raw_access",
        }
        for diagnostic in progress:
            self.assertEqual(set(diagnostic), expected_fields)
            self.assertEqual(
                set(diagnostic["queue_depths"]),
                {"event", "validation", "processor"},
            )
            for depth in diagnostic["queue_depths"].values():
                self.assertIsInstance(depth, int)
                self.assertGreaterEqual(depth, 0)
            self.assertIsInstance(diagnostic["helper_stopping"], bool)
            self.assertIsInstance(diagnostic["epoch_active"], bool)
            self.assertIs(diagnostic["raw_access"], False)
            for field in numeric_fields:
                self.assertIsInstance(diagnostic[field], int)
                self.assertGreaterEqual(diagnostic[field], 0)
        self.assertGreaterEqual(progress[-1]["candidate_count"], 1)
        self.assertGreaterEqual(progress[-1]["files_indexed_count"], 1)
        serialized = json.dumps(progress, sort_keys=True)
        self.assertNotIn(str(self.watch), serialized)
        self.assertNotIn(private_path.name, serialized)
        self.assertNotIn("private progress payload sentinel", serialized)
        self.assertNotIn("root_id", serialized)
        self.assertNotIn("event_id", serialized)
        configured_root_id = self.conn.execute(
            "SELECT root_id FROM file_watch_roots LIMIT 1"
        ).fetchone()[0]
        self.assertNotIn(str(configured_root_id), serialized)
        self.assertNotIn(
            "document_change_baseline_progress",
            json.dumps(document_changes_status(self.conn), sort_keys=True),
        )

    def test_baseline_progress_diagnostic_failure_does_not_change_result(self):
        progress_calls = 0

        def failing_sink(payload: dict) -> None:
            nonlocal progress_calls
            if payload.get("event") == "document_change_baseline_progress":
                progress_calls += 1
                raise RuntimeError("diagnostic failure at /private/sensitive/path")

        worker, _controlled = self._start_controlled_worker(
            diagnostic_sink=failing_sink
        )
        try:
            self.assertTrue(worker["started"])
            self.assertGreaterEqual(progress_calls, 1)
            self.assertTrue(document_changes_status(self.conn)["coverage"]["is_complete"])
        finally:
            self._stop_worker(worker)

    def test_baseline_progress_counts_real_session_provider_calls(self):
        from activity_mvp import document_changes as document_changes_module

        provider_calls = 0
        provider_calls_at_completion: list[int] = []
        diagnostics: list[dict] = []
        (self.watch / "probe-count.md").write_text("probe", encoding="utf-8")

        def session_provider() -> dict:
            nonlocal provider_calls
            provider_calls += 1
            return {
                "state": "unlocked",
                "locked": False,
                "on_console": True,
                "capture_allowed": True,
            }

        def diagnostic_sink(payload: dict) -> None:
            diagnostics.append(payload)
            if (
                payload.get("event") == "document_change_baseline_progress"
                and payload.get("stage") == "completed"
            ):
                provider_calls_at_completion.append(provider_calls)

        with mock.patch.object(
            document_changes_module,
            "DOCUMENT_BASELINE_DIAGNOSTIC_INTERVAL_SECONDS",
            0.0,
        ):
            worker, _controlled = self._start_controlled_worker(
                session_provider=session_provider,
                diagnostic_sink=diagnostic_sink,
            )
        try:
            self.assertTrue(worker["started"])
        finally:
            self._stop_worker(worker)

        progress = [
            item
            for item in diagnostics
            if item.get("event") == "document_change_baseline_progress"
        ]
        completed = [item for item in progress if item.get("stage") == "completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(len(provider_calls_at_completion), 1)
        self.assertGreaterEqual(provider_calls, 1)
        self.assertEqual(
            completed[0]["session_probe_count"], provider_calls_at_completion[0]
        )
        self.assertGreaterEqual(completed[0]["authorization_cache_miss_count"], 1)
        self.assertIn("running", [item["stage"] for item in progress])
        self.assertLess(
            [item["stage"] for item in progress].index("running"),
            [item["stage"] for item in progress].index("completed"),
        )

    def test_slow_authorization_probe_cache_starts_at_observed_verdict(self):
        from activity_mvp import document_changes as document_changes_module

        (self.watch / "slow-authorization-probe.md").write_text(
            "probe\n",
            encoding="utf-8",
        )
        original_probe = document_changes_module._document_change_authorized_readonly
        authorization_probe_calls = 0

        def slow_authorization_probe(*args, **kwargs):
            nonlocal authorization_probe_calls
            authorization_probe_calls += 1
            # Longer than the TTL: a timestamp captured before this wait is
            # already stale when the verdict returns.
            time.sleep(0.35)
            return original_probe(*args, **kwargs)

        with mock.patch.object(
            document_changes_module,
            "DOCUMENT_AUTHORIZATION_CACHE_SECONDS",
            0.25,
        ), mock.patch.object(
            document_changes_module,
            "_document_change_authorized_readonly",
            side_effect=slow_authorization_probe,
        ):
            worker, _controlled = self._start_controlled_worker()
        try:
            self.assertTrue(worker["started"])
        finally:
            self._stop_worker(worker)

        self.assertEqual(authorization_probe_calls, 1)

    def test_readonly_authorization_probe_ignores_unrelated_sqlite_writer(self):
        from activity_mvp import document_changes as module

        root_id = str(
            self.conn.execute("SELECT root_id FROM file_watch_roots").fetchone()[0]
        )
        expected_epoch = module._document_change_epoch(self.conn)
        unrelated_writer = ensure_ready(self.db)
        result: list[bool] = []
        errors: list[BaseException] = []

        def probe() -> None:
            try:
                result.append(
                    module._document_change_authorized_readonly(
                        self.db,
                        root_id,
                        expected_epoch,
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        unrelated_writer.execute("BEGIN IMMEDIATE")
        unrelated_writer.execute(
            "INSERT INTO daemon_state(key,value,updated_at) VALUES('unrelated_writer','1',?) "
            "ON CONFLICT(key) DO UPDATE SET value='1',updated_at=excluded.updated_at",
            (datetime.now(timezone.utc).isoformat(),),
        )
        thread = threading.Thread(target=probe)
        try:
            thread.start()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(result, [True])
        finally:
            unrelated_writer.rollback()
            unrelated_writer.close()
            thread.join(timeout=2)

    def test_readonly_authorization_probe_waits_for_exclusive_revocation(self):
        from activity_mvp import document_changes as module

        root_id = str(
            self.conn.execute("SELECT root_id FROM file_watch_roots").fetchone()[0]
        )
        expected_epoch = module._document_change_epoch(self.conn)
        writer = ensure_ready(self.db)
        result: list[bool] = []

        def probe() -> None:
            result.append(
                module._document_change_authorized_readonly(
                    self.db,
                    root_id,
                    expected_epoch,
                )
            )

        thread = threading.Thread(target=probe)
        try:
            with module.document_authorization_lock(writer, exclusive=True):
                module.bump_document_change_epoch(writer)
                thread.start()
                time.sleep(0.1)
                self.assertTrue(thread.is_alive())
                writer.commit()
                # Committing SQLite is not enough: the shared reader remains
                # behind the authorization lease until the destructive
                # operation has fully left its boundary.
                self.assertTrue(thread.is_alive())
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result, [False])
        finally:
            if writer.in_transaction:
                writer.rollback()
            writer.close()
            thread.join(timeout=2)

    def test_pause_owns_the_exclusive_document_authorization_lease(self):
        from activity_mvp import document_changes as module

        original_lock = module.document_authorization_lock
        lock_modes: list[bool] = []

        def observe_lock(conn, *, exclusive, **kwargs):
            lock_modes.append(bool(exclusive))
            return original_lock(conn, exclusive=exclusive, **kwargs)

        with mock.patch(
            "activity_mvp.document_changes.document_authorization_lock",
            side_effect=observe_lock,
        ):
            result = pause(self.conn, mode="manual")

        self.assertTrue(result["paused"])
        self.assertEqual(lock_modes, [True])

    def test_event_pipeline_with_zero_auth_cache_does_not_reenter_flock(self):
        from activity_mvp import document_changes as module

        path = self.watch / "zero-cache-event.md"
        path.write_text("before\n", encoding="utf-8")
        with mock.patch.object(
            module,
            "DOCUMENT_AUTHORIZATION_CACHE_SECONDS",
            0.0,
        ):
            worker, controlled = self._start_controlled_worker()
            try:
                path.write_text("after\n", encoding="utf-8")
                controlled.emit(self._event(path, 3001))
                self.assertTrue(
                    self._wait_until(
                        lambda: self.conn.execute(
                            "SELECT COUNT(*) FROM file_change_events "
                            "WHERE change_kind='modified'"
                        ).fetchone()[0]
                        == 1,
                        timeout=9,
                    ),
                    "event pipeline stalled while the auth cache was disabled",
                )
            finally:
                self._stop_worker(worker)

    def test_revoke_between_event_epoch_bind_and_exclusive_lock_reads_no_content(self):
        from activity_mvp import document_changes as module

        path = self.watch / "event-revoke-race.md"
        path.write_text("before\n", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        path.write_text("after\n", encoding="utf-8")
        original_lock = module.document_authorization_lock
        revocation_inserted = False
        content_checks = 0

        def insert_revocation_before_exclusive(conn, *, exclusive, **kwargs):
            nonlocal revocation_inserted
            if exclusive and not revocation_inserted:
                revocation_inserted = True
                revoker = ensure_ready(self.db)
                try:
                    with original_lock(revoker, exclusive=True):
                        module.bump_document_change_epoch(revoker)
                        revoker.commit()
                finally:
                    revoker.close()
            return original_lock(conn, exclusive=exclusive, **kwargs)

        def content_allowed() -> bool:
            nonlocal content_checks
            content_checks += 1
            return True

        with mock.patch(
            "activity_mvp.document_changes.document_authorization_lock",
            side_effect=insert_revocation_before_exclusive,
        ), mock.patch(
            "activity_mvp.document_changes._snapshot",
            side_effect=AssertionError("revoked event must not snapshot content"),
        ):
            result = process_document_event(
                self.conn,
                self._event(path, 3002),
                root_path=self.watch,
                content_allowed_check=content_allowed,
            )

        self.assertTrue(revocation_inserted)
        self.assertFalse(result["recorded"])
        self.assertEqual(result["reason"], "authorization_epoch_changed")
        self.assertEqual(content_checks, 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM file_change_events").fetchone()[0],
            0,
        )

    def test_event_with_caller_transaction_fails_closed_before_content(self):
        from activity_mvp import document_changes as module

        path = self.watch / "caller-transaction.md"
        path.write_text("before\n", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        path.write_text("after\n", encoding="utf-8")

        self.conn.execute("BEGIN")
        self.conn.execute("SELECT value FROM daemon_state LIMIT 1").fetchone()
        try:
            with mock.patch.object(
                module,
                "_resolve_root",
                side_effect=AssertionError(
                    "an active caller transaction must fail before filesystem access"
                ),
            ), mock.patch.object(
                module,
                "_snapshot",
                side_effect=AssertionError(
                    "an active caller transaction must not snapshot content"
                ),
            ):
                result = process_document_event(
                    self.conn,
                    self._event(path, 3003),
                    root_path=self.watch,
                )
            self.assertEqual(
                result,
                {
                    "recorded": False,
                    "reason": "connection_transaction_active",
                    "raw_access": False,
                },
            )
            self.assertTrue(self.conn.in_transaction)
            self.assertEqual(
                self.conn.execute(
                    "SELECT COUNT(*) FROM file_change_events"
                ).fetchone()[0],
                0,
            )
        finally:
            self.conn.rollback()

    def test_event_early_return_rolls_back_internal_identity_migration(self):
        from activity_mvp import document_changes as module

        path = self.watch / "identity-migration-early-return.md"
        path.write_text("before\n", encoding="utf-8")
        root_id = str(
            self.conn.execute("SELECT root_id FROM file_watch_roots").fetchone()[0]
        )

        def migrate_identity(conn, _root):
            conn.execute(
                "INSERT INTO daemon_state(key,value,updated_at) VALUES(?,?,?)",
                (
                    "test_pending_identity_migration",
                    "pending",
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            return root_id

        with mock.patch.object(module, "_root_id", side_effect=migrate_identity):
            result = process_document_event(
                self.conn,
                self._event(path, 3004),
                root_path=self.watch,
                content_allowed_check=lambda: False,
            )

        self.assertFalse(result["recorded"])
        self.assertEqual(result["reason"], "locked_deferred")
        self.assertFalse(self.conn.in_transaction)
        self.assertIsNone(
            self.conn.execute(
                "SELECT value FROM daemon_state "
                "WHERE key='test_pending_identity_migration'"
            ).fetchone()
        )

    def test_event_exception_rolls_back_internal_identity_migration(self):
        from activity_mvp import document_changes as module

        path = self.watch / "identity-migration-exception.md"
        path.write_text("before\n", encoding="utf-8")

        def migrate_then_fail(conn, _root):
            conn.execute(
                "INSERT INTO daemon_state(key,value,updated_at) VALUES(?,?,?)",
                (
                    "test_failed_identity_migration",
                    "pending",
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            raise RuntimeError("controlled identity migration failure")

        with mock.patch.object(module, "_root_id", side_effect=migrate_then_fail):
            with self.assertRaisesRegex(
                RuntimeError,
                "controlled identity migration failure",
            ):
                process_document_event(
                    self.conn,
                    self._event(path, 3005),
                    root_path=self.watch,
                )

        self.assertFalse(self.conn.in_transaction)
        self.assertIsNone(
            self.conn.execute(
                "SELECT value FROM daemon_state "
                "WHERE key='test_failed_identity_migration'"
            ).fetchone()
        )

    def test_baseline_progress_stops_after_completed_before_live_events(self):
        from activity_mvp import document_changes as document_changes_module

        diagnostics: list[dict] = []
        path = self.watch / "post-baseline-event.md"
        path.write_text("before", encoding="utf-8")
        with mock.patch.object(
            document_changes_module,
            "DOCUMENT_BASELINE_DIAGNOSTIC_INTERVAL_SECONDS",
            0.0,
        ):
            worker, controlled = self._start_controlled_worker(
                diagnostic_sink=diagnostics.append
            )
        try:
            progress_before_event = [
                item
                for item in diagnostics
                if item.get("event") == "document_change_baseline_progress"
            ]
            self.assertTrue(progress_before_event)
            self.assertEqual(progress_before_event[-1]["stage"], "completed")
            path.write_text("after", encoding="utf-8")
            controlled.emit(self._event(path, 301))
            self.assertTrue(
                self._wait_until(
                    lambda: self.conn.execute(
                        "SELECT COUNT(*) FROM file_change_events WHERE change_kind='modified'"
                    ).fetchone()[0]
                    == 1,
                    timeout=9,
                )
            )
            progress_after_event = [
                item
                for item in diagnostics
                if item.get("event") == "document_change_baseline_progress"
            ]
            self.assertEqual(progress_after_event, progress_before_event)
            self.assertEqual(progress_after_event[-1]["stage"], "completed")
        finally:
            self._stop_worker(worker)

    def test_fatal_transition_diagnostic_is_content_free(self):
        diagnostics: list[dict] = []
        private_path = self.watch / "private-client-secret.md"
        private_path.write_text("private", encoding="utf-8")
        worker, controlled = self._start_controlled_worker(
            diagnostic_sink=diagnostics.append
        )
        try:
            controlled.emit(
                {
                    "path": str(private_path),
                    "event_id": "987654321",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "flags": ["kernel_dropped", "item_modified"],
                }
            )
            self.assertTrue(
                self._wait_until(lambda: bool(diagnostics) and controlled.terminated),
                "fatal transition was not diagnosed and stopped",
            )
        finally:
            self._stop_worker(worker)

        fatal_diagnostics = [
            item
            for item in diagnostics
            if item.get("event") == "document_change_coverage_downgraded"
        ]
        self.assertEqual(len(fatal_diagnostics), 1)
        diagnostic = fatal_diagnostics[0]
        self.assertEqual(diagnostic["event"], "document_change_coverage_downgraded")
        self.assertEqual(diagnostic["reason"], "fsevents_control_event")
        self.assertEqual(diagnostic["control_flags"], ["kernel_dropped"])
        self.assertIs(diagnostic["raw_access"], False)
        self.assertEqual(
            set(diagnostic["queue_depths"]), {"event", "validation", "processor"}
        )
        self.assertTrue(diagnostic["caller_chain"])
        self.assertTrue(
            all(
                re.fullmatch(r"[A-Za-z0-9_<>]+:\d+", item)
                for item in diagnostic["caller_chain"]
            )
        )
        serialized = json.dumps(diagnostic, sort_keys=True)
        self.assertNotIn(str(self.watch), serialized)
        self.assertNotIn(private_path.name, serialized)
        self.assertNotIn("987654321", serialized)
        self.assertNotIn("event_id", diagnostic)

    def test_fatal_transition_diagnostic_failure_does_not_weaken_fail_closed(self):
        def failing_sink(_payload: dict) -> None:
            raise RuntimeError("diagnostic sink failed at /private/sensitive/path")

        worker, controlled = self._start_controlled_worker(
            diagnostic_sink=failing_sink
        )
        try:
            controlled.emit(
                {
                    "path": str(self.watch),
                    "event_id": "500",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "flags": ["kernel_dropped"],
                }
            )
            self.assertTrue(
                self._wait_until(lambda: controlled.terminated),
                "diagnostic failure interrupted the fatal transition",
            )
        finally:
            self._stop_worker(worker)

        coverage = document_changes_status(self.conn)["coverage"]
        self.assertEqual(coverage["coverage_status"], "partial")
        self.assertFalse(coverage["is_complete"])

    def test_gap_after_barrier_cannot_be_overwritten_by_complete_commit(self):
        path = self.watch / "barrier-tail.md"
        path.write_text("current", encoding="utf-8")
        holder: dict[str, object] = {}
        finish_entered = threading.Event()
        helper_stopped = threading.Event()
        emitter_failed = threading.Event()

        class TailRaceProcess(_ControlledProcess):
            def terminate(self):
                super().terminate()
                # The control-event path stops the helper only after committing
                # partial coverage, so this is an exact persistence barrier.
                helper_stopped.set()

        def factory(command, **kwargs):
            process = TailRaceProcess(command, **kwargs)
            holder["process"] = process

            def emit_gap():
                if not finish_entered.wait(timeout=4):
                    emitter_failed.set()
                    return
                process.emit({
                    "path": str(self.watch),
                    "event_id": "2",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "flags": ["kernel_dropped"],
                })

            emitter = threading.Thread(target=emit_gap, daemon=True)
            holder["emitter"] = emitter
            emitter.start()
            return process

        def transition_hook(phase: str) -> None:
            if phase != "before_finish_lock":
                return
            finish_entered.set()
            self.assertTrue(
                helper_stopped.wait(timeout=4),
                "gap was not committed before baseline promotion resumed",
            )

        worker = start_document_change_worker(
            self.db,
            ["/safe/helper"],
            popen_factory=factory,
            session_state_provider=lambda: {
                "state": "unlocked",
                "locked": False,
                "on_console": True,
                "capture_allowed": True,
            },
            _transition_hook=transition_hook,
        )
        emitter = holder["emitter"]
        assert isinstance(emitter, threading.Thread)
        emitter.join(timeout=4)
        try:
            self.assertFalse(emitter.is_alive())
            self.assertFalse(emitter_failed.is_set())
            self.assertFalse(worker["started"])
            self.assertEqual(
                worker["reason"],
                "authorization_changed_or_reconcile_restart",
            )
            coverage = document_changes_status(self.conn)["coverage"]
            self.assertEqual(coverage["coverage_status"], "partial")
            self.assertFalse(coverage["is_complete"])
            self.assertGreater(coverage["event_gap_count"], 0)
        finally:
            self._stop_worker(worker)

    def test_disable_during_startup_baseline_cannot_restore_or_complete_it(self):
        path = self.watch / "startup-disable.md"
        path.write_text("current", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        holder: dict[str, object] = {}
        scan_entered = threading.Event()
        release_scan = threading.Event()
        original_baseline = build_document_baseline

        def factory(command, **kwargs):
            process = _ControlledProcess(command, **kwargs)
            holder["process"] = process
            return process

        def blocked_baseline(*args, **kwargs):
            scan_entered.set()
            release_scan.wait(timeout=5)
            return original_baseline(*args, **kwargs)

        def run_worker():
            holder["worker"] = start_document_change_worker(
                self.db,
                ["/safe/helper"],
                popen_factory=factory,
                session_state_provider=lambda: {
                    "state": "unlocked", "locked": False,
                    "on_console": True, "capture_allowed": True,
                },
            )

        thread = threading.Thread(target=run_worker)
        with mock.patch("activity_mvp.document_changes.build_document_baseline", side_effect=blocked_baseline):
            thread.start()
            self.assertTrue(scan_entered.wait(timeout=3))
            configure_document_changes(self.conn, enabled=False, runtime_root=self.root / "data")
            release_scan.set()
            thread.join(timeout=8)
        self.assertFalse(thread.is_alive())
        self.assertFalse(holder["worker"]["started"])
        status = document_changes_status(self.conn)
        self.assertFalse(status["coverage"]["enabled"])
        self.assertEqual(status["coverage"]["baseline_status"], "disabled")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM file_watch_state").fetchone()[0], 0)
        configure_document_changes(self.conn, enabled=True, runtime_root=self.root / "data")
        self.assertFalse(document_changes_status(self.conn)["coverage"]["is_complete"])
        self._stop_worker(holder["worker"])

    def test_missing_helper_barrier_leaves_persisted_partial_coverage(self):
        def factory(command, **kwargs):
            return _NoBarrierProcess(command, **kwargs)

        with self.assertRaises(RuntimeError):
            start_document_change_worker(
                self.db,
                ["/safe/helper"],
                popen_factory=factory,
                session_state_provider=lambda: {
                    "state": "unlocked", "locked": False,
                    "on_console": True, "capture_allowed": True,
                },
            )
        self.conn.close()
        self.conn = ensure_ready(self.db)
        self.assertFalse(document_changes_status(self.conn)["coverage"]["is_complete"])

    def test_handler_exception_marks_gap_and_stops_stream(self):
        path = self.watch / "handler-error.md"
        path.write_text("before", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        worker, controlled = self._start_controlled_worker()
        try:
            path.write_text("after", encoding="utf-8")
            with mock.patch(
                "activity_mvp.document_changes.process_document_event",
                side_effect=RuntimeError("injected handler failure"),
            ):
                controlled.emit(self._event(path, 510))
                self.assertTrue(self._wait_until(lambda: controlled.terminated, timeout=8))
            self.conn.close()
            self.conn = ensure_ready(self.db)
            coverage = document_changes_status(self.conn)["coverage"]
            self.assertFalse(coverage["is_complete"])
            self.assertGreater(coverage["event_gap_count"], 0)
        finally:
            self._stop_worker(worker)

    def test_live_locked_console_records_authorized_event_content(self):
        path = self.watch / "live-lock.md"
        path.write_text("before", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        live_state = {
            "state": "unlocked", "locked": False,
            "on_console": True, "capture_allowed": True,
        }
        worker, controlled = self._start_controlled_worker(lambda: dict(live_state))
        from activity_mvp import document_changes as module
        original_snapshot = module._snapshot
        content_reads: list[bool] = []

        def observe_snapshot(*args, **kwargs):
            content_reads.append(bool(kwargs.get("allow_content", True)))
            return original_snapshot(*args, **kwargs)

        try:
            live_state.update({"state": "locked", "locked": True, "capture_allowed": False})
            self.conn.execute(
                "INSERT INTO daemon_state(key,value,updated_at) VALUES('locked','true',?) "
                "ON CONFLICT(key) DO UPDATE SET value='true',updated_at=excluded.updated_at",
                (datetime.now(timezone.utc).isoformat(),),
            )
            self.conn.commit()
            path.write_text("after", encoding="utf-8")
            with mock.patch("activity_mvp.document_changes._snapshot", side_effect=observe_snapshot):
                controlled.emit(self._event(path, 520))
                self.assertTrue(self._wait_until(
                    lambda: self.conn.execute(
                        "SELECT COUNT(*) FROM file_change_events"
                    ).fetchone()[0] == 1,
                    timeout=8,
                ))
            self.assertTrue(any(content_reads))
            self.assertTrue(self._wait_until(
                lambda: document_changes_status(self.conn)["coverage"]["pending_event_count"] == 0,
                timeout=3,
            ))
            self.assertTrue(document_changes_status(self.conn)["coverage"]["is_complete"])
        finally:
            self._stop_worker(worker)

    def test_live_locked_console_can_build_content_baseline(self):
        path = self.watch / "locked-baseline.md"
        path.write_text("authorized while console locked\n", encoding="utf-8")
        self.conn.execute(
            "INSERT INTO daemon_state(key,value,updated_at) VALUES('locked','true',?) "
            "ON CONFLICT(key) DO UPDATE SET value='true',updated_at=excluded.updated_at",
            (datetime.now(timezone.utc).isoformat(),),
        )
        self.conn.commit()
        locked_state = {
            "state": "locked",
            "locked": True,
            "on_console": True,
            "capture_allowed": False,
        }

        worker, _controlled = self._start_controlled_worker(lambda: dict(locked_state))
        try:
            self.assertTrue(worker["started"])
            self.assertTrue(document_changes_status(self.conn)["coverage"]["is_complete"])
            state = self.conn.execute(
                "SELECT content_hmac,detail_level FROM file_watch_state "
                "WHERE file_name_redacted='locked-baseline.md'"
            ).fetchone()
            self.assertIsNotNone(state)
            self.assertTrue(str(state["content_hmac"] or ""))
            self.assertEqual(state["detail_level"], "detailed")
        finally:
            self._stop_worker(worker)

    def test_disable_after_worker_start_prevents_arrival_content_read(self):
        path = self.watch / "disable-race.md"
        path.write_text("before", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        locked_state = {
            "state": "locked",
            "locked": True,
            "on_console": True,
            "capture_allowed": False,
        }
        self.conn.execute(
            "INSERT INTO daemon_state(key,value,updated_at) VALUES('locked','true',?) "
            "ON CONFLICT(key) DO UPDATE SET value='true',updated_at=excluded.updated_at",
            (datetime.now(timezone.utc).isoformat(),),
        )
        self.conn.commit()
        worker, controlled = self._start_controlled_worker(lambda: dict(locked_state))
        from activity_mvp import document_changes as module
        original_snapshot = module._snapshot
        content_reads: list[bool] = []

        def observe_snapshot(*args, **kwargs):
            content_reads.append(bool(kwargs.get("allow_content", True)))
            return original_snapshot(*args, **kwargs)

        try:
            configure_document_changes(self.conn, enabled=False, runtime_root=self.root / "data")
            path.write_text("after disable", encoding="utf-8")
            with mock.patch("activity_mvp.document_changes._snapshot", side_effect=observe_snapshot):
                controlled.emit(self._event(path, 530))
                time.sleep(1.5)
            self.assertFalse(any(content_reads))
        finally:
            self._stop_worker(worker)

    def test_remove_root_while_validator_waits_never_starts_content_read(self):
        path = self.watch / "remove-race.md"
        path.write_text("before", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        worker, controlled = self._start_controlled_worker()
        from activity_mvp import document_changes as module
        original_snapshot = module._snapshot
        snapshot_entered = threading.Event()
        release_snapshot = threading.Event()
        extraction_calls: list[str] = []

        def blocked_snapshot(*args, **kwargs):
            snapshot_entered.set()
            release_snapshot.wait(timeout=5)
            return original_snapshot(*args, **kwargs)

        try:
            path.write_text("after", encoding="utf-8")
            original_extract = module._extract_structure

            def tracked_extract(target):
                extraction_calls.append(str(target))
                return original_extract(target)

            with mock.patch("activity_mvp.document_changes._snapshot", side_effect=blocked_snapshot), mock.patch(
                "activity_mvp.document_changes._extract_structure", side_effect=tracked_extract
            ):
                controlled.emit(self._event(path, 535))
                self.assertTrue(snapshot_entered.wait(timeout=4))
                configure_document_changes(
                    self.conn,
                    remove_root=self.watch,
                    runtime_root=self.root / "data",
                )
                release_snapshot.set()
                time.sleep(2.5)
            self.assertEqual(extraction_calls, [])
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM file_change_events").fetchone()[0], 0)
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM file_watch_roots").fetchone()[0], 0)
        finally:
            release_snapshot.set()
            self._stop_worker(worker)

    def test_stream_wide_drop_with_slash_path_marks_every_root_partial(self):
        path = self.watch / "drop.md"
        path.write_text("before", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        worker, controlled = self._start_controlled_worker()
        try:
            controlled.emit({
                "path": "/",
                "event_id": "540",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "flags": ["kernel_dropped"],
            })
            self.assertTrue(self._wait_until(lambda: controlled.terminated, timeout=4))
            coverage = document_changes_status(self.conn)["coverage"]
            self.assertFalse(coverage["is_complete"])
            self.assertGreater(coverage["event_gap_count"], 0)
        finally:
            self._stop_worker(worker)

    def test_root_changed_after_watched_path_vanishes_is_not_dropped(self):
        path = self.watch / "root.md"
        path.write_text("before", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        worker, controlled = self._start_controlled_worker()
        moved = self.root / "watch-moved"
        try:
            self.watch.rename(moved)
            controlled.emit({
                "path": str(self.watch),
                "event_id": "541",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "flags": ["root_changed"],
            })
            self.assertTrue(self._wait_until(lambda: controlled.terminated, timeout=4))
            self.assertFalse(document_changes_status(self.conn)["coverage"]["is_complete"])
        finally:
            self._stop_worker(worker)

    def test_remove_root_serializes_with_commit_and_leaves_no_orphans(self):
        path = self.watch / "remove-commit-race.md"
        path.write_text("before", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        worker, controlled = self._start_controlled_worker()
        root_ref = document_changes_status(self.conn)["roots"][0]["root_ref"]
        from activity_mvp import document_changes as module

        original_upsert = module._upsert_state
        write_locked = threading.Event()
        release_write = threading.Event()
        remove_started = threading.Event()
        remove_done = threading.Event()
        errors: list[BaseException] = []

        def blocked_upsert(*args, **kwargs):
            write_locked.set()
            release_write.wait(timeout=8)
            return original_upsert(*args, **kwargs)

        def remove_root():
            remove_started.set()
            remove_conn = ensure_ready(self.db)
            try:
                configure_document_changes(
                    remove_conn,
                    remove_root=root_ref,
                    runtime_root=self.root / "data",
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                remove_conn.close()
                remove_done.set()

        try:
            path.write_text("after", encoding="utf-8")
            with mock.patch("activity_mvp.document_changes._upsert_state", side_effect=blocked_upsert):
                controlled.emit(self._event(path, 542))
                self.assertTrue(write_locked.wait(timeout=6))
                remover = threading.Thread(target=remove_root)
                remover.start()
                self.assertTrue(remove_started.wait(timeout=2))
                time.sleep(0.2)
                self.assertFalse(remove_done.is_set(), "remove bypassed the event writer lock")
                release_write.set()
                remover.join(timeout=8)
            self.assertFalse(remover.is_alive())
            self.assertEqual(errors, [])
            for table in ("file_watch_roots", "file_watch_state", "file_watch_checkpoints", "file_change_events"):
                self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
        finally:
            release_write.set()
            self._stop_worker(worker)

    def test_delete_range_serializes_with_commit_and_cannot_be_repopulated(self):
        path = self.watch / "delete-commit-race.md"
        path.write_text("before", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        worker, controlled = self._start_controlled_worker()
        from activity_mvp import document_changes as module

        original_upsert = module._upsert_state
        write_locked = threading.Event()
        release_write = threading.Event()
        delete_done = threading.Event()
        errors: list[BaseException] = []

        def blocked_upsert(*args, **kwargs):
            write_locked.set()
            release_write.wait(timeout=8)
            return original_upsert(*args, **kwargs)

        def delete_all():
            delete_conn = ensure_ready(self.db)
            try:
                delete_range(
                    delete_conn,
                    "2000-01-01T00:00:00+00:00",
                    "2100-01-01T00:00:00+00:00",
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
            finally:
                delete_conn.close()
                delete_done.set()

        try:
            path.write_text("after", encoding="utf-8")
            with mock.patch("activity_mvp.document_changes._upsert_state", side_effect=blocked_upsert):
                controlled.emit(self._event(path, 543))
                self.assertTrue(write_locked.wait(timeout=6))
                deleter = threading.Thread(target=delete_all)
                deleter.start()
                time.sleep(0.2)
                self.assertFalse(delete_done.is_set(), "delete-range bypassed the event writer lock")
                release_write.set()
                deleter.join(timeout=8)
            self.assertFalse(deleter.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM file_watch_state").fetchone()[0], 0)
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM file_change_events").fetchone()[0], 0)
            self.assertFalse(document_changes_status(self.conn)["coverage"]["is_complete"])
        finally:
            release_write.set()
            self._stop_worker(worker)

    def test_queued_pre_delete_event_cannot_borrow_new_epoch_and_repopulate_range(self):
        path = self.watch / "queued-before-delete.md"
        path.write_text("before", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        worker, controlled = self._start_controlled_worker()
        from activity_mvp import document_changes as module

        original_match = module._registry_match
        consumer_blocked = threading.Event()
        release_consumer = threading.Event()

        def gated_match(registry, candidate):
            if Path(candidate) == path:
                consumer_blocked.set()
                release_consumer.wait(timeout=8)
            return original_match(registry, candidate)

        observed = datetime.now(timezone.utc)
        try:
            path.write_text("after", encoding="utf-8")
            with mock.patch("activity_mvp.document_changes._registry_match", side_effect=gated_match):
                controlled.emit(self._event(path, 5431, observed_at=observed.isoformat()))
                self.assertTrue(consumer_blocked.wait(timeout=4))
                delete_range(
                    self.conn,
                    (observed - timedelta(milliseconds=100)).isoformat(),
                    (observed + timedelta(seconds=1)).isoformat(),
                )
                self.assertEqual(self.conn.execute(
                    "SELECT COUNT(*) FROM file_change_events"
                ).fetchone()[0], 0)
                release_consumer.set()
                self.assertTrue(self._wait_until(lambda: controlled.terminated, timeout=4))
                time.sleep(3.3)
            self.assertEqual(self.conn.execute(
                "SELECT COUNT(*) FROM file_change_events WHERE datetime(last_changed_at)>=datetime(?) "
                "AND datetime(last_changed_at)<datetime(?)",
                (
                    (observed - timedelta(milliseconds=100)).isoformat(),
                    (observed + timedelta(seconds=1)).isoformat(),
                ),
            ).fetchone()[0], 0)
            self.assertEqual(
                document_changes_status(self.conn)["coverage"]["pending_event_count"],
                0,
            )
        finally:
            release_consumer.set()
            self._stop_worker(worker)

    def test_empty_manual_delete_and_root_removal_fail_closed_until_new_barrier(self):
        path = self.watch / "coverage.md"
        path.write_text("baseline", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        worker, controlled = self._start_controlled_worker()
        try:
            self.assertTrue(document_changes_status(self.conn)["coverage"]["is_complete"])
            delete_range(
                self.conn,
                "1990-01-01T00:00:00+00:00",
                "1990-01-02T00:00:00+00:00",
            )
            self.assertTrue(self._wait_until(lambda: controlled.terminated, timeout=4))
            coverage = document_changes_status(self.conn)["coverage"]
            self.assertFalse(coverage["is_complete"])
            self.assertEqual(coverage["baseline_status"], "building")
        finally:
            self._stop_worker(worker)

        second = self.root / "second-root"
        second.mkdir()
        (second / "second.md").write_text("second", encoding="utf-8")
        configure_document_changes(
            self.conn,
            add_root=second,
            root_label="Second",
            runtime_root=self.root / "data",
        )
        build_document_baseline(self.conn, self.watch)
        build_document_baseline(self.conn, second)
        self.assertTrue(document_changes_status(self.conn)["coverage"]["is_complete"])
        configure_document_changes(
            self.conn,
            remove_root=self.watch,
            runtime_root=self.root / "data",
        )
        remaining = document_changes_status(self.conn)
        self.assertEqual(remaining["coverage"]["root_count"], 1)
        self.assertFalse(remaining["coverage"]["is_complete"])
        self.assertEqual(remaining["coverage"]["baseline_status"], "building")

    def test_old_epoch_pending_release_cannot_decrement_new_worker_count(self):
        from activity_mvp import document_changes as module

        old_epoch = module._document_change_epoch(self.conn)
        module._set_document_change_pending(self.conn, 1)
        self.conn.commit()
        module.bump_document_change_epoch(self.conn)
        module._set_document_change_pending(self.conn, 0)
        self.conn.commit()
        new_epoch = module._document_change_epoch(self.conn)
        self.assertGreater(new_epoch, old_epoch)
        self.assertEqual(
            module._adjust_document_change_pending(
                self.conn, 1, expected_epoch=new_epoch
            ),
            1,
        )
        self.conn.commit()
        self.assertIsNone(module._adjust_document_change_pending(
            self.conn, -1, expected_epoch=old_epoch
        ))
        self.conn.rollback()
        self.assertEqual(module._document_change_pending_count(self.conn), 1)

    def test_old_worker_gap_write_serializes_before_manual_delete_reset(self):
        from activity_mvp import document_changes as module

        path = self.watch / "gap-race.md"
        path.write_text("baseline", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        worker, controlled = self._start_controlled_worker()
        original_gap = module._record_gap
        gap_locked = threading.Event()
        release_gap = threading.Event()
        delete_done = threading.Event()
        errors: list[BaseException] = []

        def blocked_gap(*args, **kwargs):
            gap_locked.set()
            release_gap.wait(timeout=8)
            return original_gap(*args, **kwargs)

        def delete_empty_range():
            conn = ensure_ready(self.db)
            try:
                delete_range(
                    conn,
                    "1991-01-01T00:00:00+00:00",
                    "1991-01-02T00:00:00+00:00",
                )
            except BaseException as exc:
                errors.append(exc)
            finally:
                conn.close()
                delete_done.set()

        try:
            with mock.patch("activity_mvp.document_changes._record_gap", side_effect=blocked_gap):
                controlled.emit({
                    "path": "/",
                    "event_id": "5432",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "flags": ["user_dropped", "must_scan_subdirs"],
                })
                self.assertTrue(gap_locked.wait(timeout=4))
                deleter = threading.Thread(target=delete_empty_range)
                deleter.start()
                time.sleep(0.2)
                self.assertFalse(delete_done.is_set(), "manual delete bypassed the old gap writer lock")
                release_gap.set()
                deleter.join(timeout=8)
            self.assertFalse(deleter.is_alive())
            self.assertEqual(errors, [])
            self.assertFalse(document_changes_status(self.conn)["coverage"]["is_complete"])
            self.assertEqual(build_document_baseline(self.conn, self.watch)["baseline_status"], "complete")
            self.assertTrue(document_changes_status(self.conn)["coverage"]["is_complete"])
        finally:
            release_gap.set()
            self._stop_worker(worker)

    def test_old_worker_gap_write_cannot_recreate_removed_root_checkpoint(self):
        from activity_mvp import document_changes as module

        build_document_baseline(self.conn, self.watch)
        worker, controlled = self._start_controlled_worker()
        original_gap = module._record_gap
        gap_locked = threading.Event()
        release_gap = threading.Event()
        remove_done = threading.Event()
        errors: list[BaseException] = []

        def blocked_gap(*args, **kwargs):
            gap_locked.set()
            release_gap.wait(timeout=8)
            return original_gap(*args, **kwargs)

        def remove_root():
            conn = ensure_ready(self.db)
            try:
                configure_document_changes(
                    conn,
                    remove_root=self.watch,
                    runtime_root=self.root / "data",
                )
            except BaseException as exc:
                errors.append(exc)
            finally:
                conn.close()
                remove_done.set()

        try:
            with mock.patch("activity_mvp.document_changes._record_gap", side_effect=blocked_gap):
                controlled.emit({
                    "path": "/",
                    "event_id": "5433",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "flags": ["kernel_dropped", "must_scan_subdirs"],
                })
                self.assertTrue(gap_locked.wait(timeout=4))
                remover = threading.Thread(target=remove_root)
                remover.start()
                time.sleep(0.2)
                self.assertFalse(remove_done.is_set(), "root removal bypassed the old gap writer lock")
                release_gap.set()
                remover.join(timeout=8)
            self.assertFalse(remover.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM file_watch_roots").fetchone()[0], 0)
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM file_watch_checkpoints").fetchone()[0], 0)
        finally:
            release_gap.set()
            self._stop_worker(worker)

    def test_locked_pending_event_completes_without_monitor_restart(self):
        path = self.watch / "pending-lock.md"
        path.write_text("before", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        live_state = {
            "state": "unlocked", "locked": False,
            "on_console": True, "capture_allowed": True,
        }
        worker, controlled = self._start_controlled_worker(lambda: dict(live_state))
        try:
            live_state.update({"state": "locked", "locked": True, "capture_allowed": False})
            self.conn.execute(
                "INSERT INTO daemon_state(key,value,updated_at) VALUES('locked','true',?) "
                "ON CONFLICT(key) DO UPDATE SET value='true',updated_at=excluded.updated_at",
                (datetime.now(timezone.utc).isoformat(),),
            )
            self.conn.commit()
            path.write_text("after", encoding="utf-8")
            controlled.emit(self._event(path, 544))
            self.assertTrue(self._wait_until(
                lambda: self.conn.execute("SELECT COUNT(*) FROM file_change_events").fetchone()[0] == 1,
                timeout=8,
            ))
            self.assertTrue(self._wait_until(
                lambda: document_changes_status(self.conn)["coverage"]["pending_event_count"] == 0,
                timeout=3,
            ))
            self.assertTrue(document_changes_status(self.conn)["coverage"]["is_complete"])
            self.assertFalse(controlled.terminated, "locked-console event stopped the helper")
        finally:
            self._stop_worker(worker)

    def test_off_console_pending_event_waits_then_recovers_to_locked_console(self):
        path = self.watch / "pending-off-console.md"
        path.write_text("before", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        live_state = {
            "state": "unlocked",
            "locked": False,
            "on_console": True,
            "capture_allowed": True,
        }
        worker, controlled = self._start_controlled_worker(lambda: dict(live_state))
        from activity_mvp import document_changes as module

        original_snapshot = module._snapshot
        content_reads: list[bool] = []

        def observe_snapshot(*args, **kwargs):
            content_reads.append(bool(kwargs.get("allow_content", True)))
            return original_snapshot(*args, **kwargs)

        try:
            live_state.update(
                {
                    "state": "locked",
                    "locked": True,
                    "on_console": False,
                    "capture_allowed": False,
                }
            )
            path.write_text("after", encoding="utf-8")
            with mock.patch("activity_mvp.document_changes._snapshot", side_effect=observe_snapshot):
                controlled.emit(self._event(path, 545))
                self.assertTrue(self._wait_until(
                    lambda: document_changes_status(self.conn)["coverage"]["pending_event_count"] == 1,
                    timeout=3,
                ))
                time.sleep(2.3)
                self.assertEqual(
                    self.conn.execute("SELECT COUNT(*) FROM file_change_events").fetchone()[0],
                    0,
                )
                self.assertFalse(any(content_reads))
                self.assertFalse(controlled.terminated)

                live_state.update(
                    {
                        "state": "locked",
                        "locked": True,
                        "on_console": True,
                        "capture_allowed": False,
                    }
                )
                self.assertTrue(self._wait_until(
                    lambda: self.conn.execute("SELECT COUNT(*) FROM file_change_events").fetchone()[0] == 1,
                    timeout=8,
                ))
                self.assertTrue(any(content_reads))
                self.assertTrue(self._wait_until(
                    lambda: document_changes_status(self.conn)["coverage"]["pending_event_count"] == 0,
                    timeout=3,
                ))
                self.assertTrue(document_changes_status(self.conn)["coverage"]["is_complete"])
        finally:
            self._stop_worker(worker)

    def test_helper_ready_timeout_joins_started_pipeline_threads(self):
        before = {
            thread
            for thread in threading.enumerate()
            if thread.name.startswith("pam-document-")
        }
        holder: dict[str, _NoReadyProcess] = {}

        def factory(command, **kwargs):
            process = _NoReadyProcess(command, **kwargs)
            holder["process"] = process
            return process

        with self.assertRaisesRegex(RuntimeError, "did not become ready"):
            start_document_change_worker(
                self.db,
                ["/safe/helper"],
                popen_factory=factory,
                session_state_provider=lambda: {
                    "state": "unlocked", "locked": False,
                    "on_console": True, "capture_allowed": True,
                },
            )
        self.assertTrue(holder["process"].terminated)
        self.assertEqual(
            {
                thread
                for thread in threading.enumerate()
                if thread.name.startswith("pam-document-")
            },
            before,
        )
        self.assertFalse(document_changes_status(self.conn)["coverage"]["is_complete"])

    def test_startup_baseline_exception_joins_started_pipeline_threads(self):
        before = {
            thread
            for thread in threading.enumerate()
            if thread.name.startswith("pam-document-")
        }
        original_baseline = build_document_baseline

        def fail_worker_baseline(*args, **kwargs):
            if kwargs.get("finalize_coverage") is False:
                raise RuntimeError("synthetic startup baseline failure")
            return original_baseline(*args, **kwargs)

        with mock.patch(
            "activity_mvp.document_changes.build_document_baseline",
            side_effect=fail_worker_baseline,
        ):
            with self.assertRaisesRegex(RuntimeError, "synthetic startup baseline failure"):
                self._start_controlled_worker()
        self.assertEqual(
            {
                thread
                for thread in threading.enumerate()
                if thread.name.startswith("pam-document-")
            },
            before,
        )
        self.assertFalse(document_changes_status(self.conn)["coverage"]["is_complete"])

    def test_ready_root_count_mismatch_fails_closed(self):
        before = {
            thread
            for thread in threading.enumerate()
            if thread.name.startswith("pam-document-")
        }
        second = self.root / "second-watch"
        second.mkdir()
        configure_document_changes(
            self.conn,
            add_root=second,
            root_label="Second",
            runtime_root=self.root / "data",
        )
        holder: dict[str, _ControlledProcess] = {}

        def factory(command, **kwargs):
            process = _ControlledProcess(command, **kwargs)  # deliberately reports one root
            holder["process"] = process
            return process

        with self.assertRaisesRegex(RuntimeError, "root count mismatch"):
            start_document_change_worker(
                self.db,
                ["/safe/helper"],
                popen_factory=factory,
                session_state_provider=lambda: {
                    "state": "unlocked", "locked": False,
                    "on_console": True, "capture_allowed": True,
                },
            )
        self.assertTrue(holder["process"].terminated)
        self.assertEqual(
            {
                thread
                for thread in threading.enumerate()
                if thread.name.startswith("pam-document-")
            },
            before,
        )
        coverage = document_changes_status(self.conn)["coverage"]
        self.assertFalse(coverage["is_complete"])
        self.assertGreater(coverage["event_gap_count"], 0)

    def test_directory_event_is_benign_without_stopping_or_losing_file_events(self):
        """A directory rename is benign churn, not a reason to stop recording.

        This used to assert that the helper was terminated and that
        file_change_events stayed empty.  Neither was a safety property -- they
        described the symptom of a worker that killed itself on the first
        directory event any real machine produces, which is why this database
        held 55k dropped events and not one change.  The property actually worth
        defending is that ordinary directory churn must not invalidate an
        otherwise continuous file stream, while later file events still land.
        """

        directory = self.watch / "old-folder"
        directory.mkdir()
        (directory / "inside.md").write_text("before", encoding="utf-8")
        tracked = self.watch / "tracked.md"
        tracked.write_text("before", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        worker, controlled = self._start_controlled_worker()
        try:
            moved = self.watch / "new-folder"
            directory.rename(moved)
            controlled.emit({
                "path": str(directory),
                "event_id": "545",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "flags": ["item_renamed", "item_is_dir"],
            })
            self.assertTrue(self._wait_until(
                lambda: document_changes_status(self.conn)["coverage"]["benign_event_count"] > 0,
                timeout=5,
            ))
            coverage = document_changes_status(self.conn)["coverage"]
            self.assertTrue(coverage["is_complete"])
            self.assertEqual(coverage["event_gap_count"], 0)
            self.assertGreater(coverage["benign_event_count"], 0)
            self.assertFalse(controlled.terminated)
            # Benign churn makes paths uncertain; it does not undo the
            # enumeration, so baseline_status must survive it.
            self.assertEqual(
                {
                    str(row["baseline_status"])
                    for row in self.conn.execute("SELECT baseline_status FROM file_watch_roots")
                },
                {"complete"},
            )
            # The regression this test exists to catch: after one directory
            # event, an ordinary file change must still be recorded.
            tracked.write_text("after", encoding="utf-8")
            controlled.emit(self._event(tracked, 546))
            self.assertTrue(self._wait_until(
                lambda: self.conn.execute(
                    "SELECT COUNT(*) FROM file_change_events"
                ).fetchone()[0] == 1,
                timeout=9,
            ))
            # The watermark has to advance too, or a restart replays the whole
            # backlog and overflows the queue -- the other half of the 55k bug.
            self.assertEqual(
                self.conn.execute(
                    "SELECT last_event_id FROM file_watch_checkpoints"
                ).fetchone()[0],
                "546",
            )
            self._stop_worker(worker)
            for name in ("reader", "consumer", "validator", "processor", "monitor"):
                thread = worker.get(name)
                if thread is not None:
                    self.assertFalse(thread.is_alive(), f"{name} leaked after stop")
            self.assertEqual(document_changes_status(self.conn)["coverage"]["pending_event_count"], 0)
        finally:
            self._stop_worker(worker)

    def test_later_directory_event_cannot_replay_drop_earlier_file_event(self):
        """A fast directory checkpoint must not overtake debounced file work."""

        from activity_mvp import document_changes as module

        build_document_baseline(self.conn, self.watch)
        worker, controlled = self._start_controlled_worker()
        try:
            tracked = self.watch / "created-before-directory.md"
            tracked.write_text("created", encoding="utf-8")
            with mock.patch.object(
                module, "_snapshot", wraps=module._snapshot
            ) as snapshot:
                controlled.emit(
                    self._event(
                        tracked,
                        545,
                        flags=["item_created", "item_is_file"],
                    )
                )
                controlled.emit(
                    self._event(
                        self.watch,
                        546,
                        flags=["item_modified", "item_is_dir"],
                    )
                )

                self.assertTrue(
                    self._wait_until(
                        lambda: (
                            document_changes_status(self.conn)["coverage"][
                                "pending_event_count"
                            ]
                            == 0
                            and document_changes_status(self.conn)["coverage"][
                                "benign_event_count"
                            ]
                            > 0
                        ),
                        timeout=9,
                    )
                )
                self.assertEqual(snapshot.call_count, 1)
            self.assertEqual(
                self.conn.execute(
                    "SELECT COUNT(*) FROM file_change_events WHERE change_kind='created'"
                ).fetchone()[0],
                1,
                "the later directory event advanced the watermark past queued file work",
            )
            self.assertEqual(
                self.conn.execute(
                    "SELECT COUNT(*) FROM file_watch_state WHERE present=1"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                self.conn.execute(
                    "SELECT benign_event_count FROM file_watch_checkpoints"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                self.conn.execute(
                    "SELECT event_gap_count FROM file_watch_checkpoints"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                self.conn.execute(
                    "SELECT last_event_id FROM file_watch_checkpoints"
                ).fetchone()[0],
                "546",
            )
            self.assertFalse(controlled.terminated)
        finally:
            self._stop_worker(worker)

    def test_later_ignore_control_cannot_replay_drop_earlier_file_event(self):
        build_document_baseline(self.conn, self.watch)
        worker, controlled = self._start_controlled_worker()
        try:
            tracked = self.watch / "created-before-ignore-control.md"
            tracked.write_text("created", encoding="utf-8")
            ignore_control = self.watch / ".gitignore"
            ignore_control.write_text("*.tmp\n", encoding="utf-8")
            controlled.emit(
                self._event(
                    tracked,
                    547,
                    flags=["item_created", "item_is_file"],
                )
            )
            controlled.emit(
                self._event(
                    ignore_control,
                    548,
                    flags=["item_modified", "item_is_file"],
                )
            )

            self.assertTrue(
                self._wait_until(
                    lambda: (
                        document_changes_status(self.conn)["coverage"][
                            "pending_event_count"
                        ]
                        == 0
                        and document_changes_status(self.conn)["coverage"][
                            "event_gap_count"
                        ]
                        == 1
                    ),
                    timeout=9,
                )
            )
            self.assertEqual(
                self.conn.execute(
                    "SELECT COUNT(*) FROM file_change_events WHERE change_kind='created'"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                self.conn.execute(
                    "SELECT last_event_id FROM file_watch_checkpoints"
                ).fetchone()[0],
                "548",
            )
            self.assertFalse(controlled.terminated)
        finally:
            self._stop_worker(worker)

    def test_steady_state_event_pipeline_does_not_reinitialize_schema(self):
        """Schema migration belongs to startup, not every event or monitor tick."""

        tracked = self.watch / "steady-state.md"
        tracked.write_text("before", encoding="utf-8")
        monitor_tick = threading.Event()
        from activity_mvp import document_changes as document_changes_module

        original_setting_bool = document_changes_module._setting_bool

        def observed_setting_bool(conn, key, default):
            result = original_setting_bool(conn, key, default)
            if threading.current_thread().name == "pam-document-config-monitor":
                monitor_tick.set()
            return result

        with mock.patch.object(
            document_changes_module,
            "_setting_bool",
            side_effect=observed_setting_bool,
        ):
            worker, controlled = self._start_controlled_worker()
            try:
                root_state = self.conn.execute(
                    "SELECT baseline_status,baseline_completed_at FROM file_watch_roots"
                ).fetchone()
                self.assertEqual(root_state["baseline_status"], "complete")
                self.assertTrue(root_state["baseline_completed_at"])
                baseline_state = self.conn.execute(
                    "SELECT file_id,content_hmac FROM file_watch_state WHERE present=1"
                ).fetchall()
                self.assertEqual(len(baseline_state), 1)
                file_id = str(baseline_state[0]["file_id"])
                before_hmac = str(baseline_state[0]["content_hmac"] or "")
                before_events = self.conn.execute(
                    "SELECT COUNT(*) FROM file_change_events"
                ).fetchone()[0]

                # Establish one real monitor tick before installing the guard,
                # then require another tick while schema initialization is
                # forbidden.  This avoids treating a fixed sleep as evidence
                # that the monitor actually ran.
                self.assertTrue(monitor_tick.wait(5))
                monitor_tick.clear()
                tracked.write_text("after", encoding="utf-8")
                with mock.patch(
                    "activity_mvp.storage.init_schema",
                    side_effect=AssertionError("steady-state schema initialization"),
                ):
                    controlled.emit(
                        self._event(
                            tracked,
                            549,
                            flags=["item_modified", "item_is_file"],
                        )
                    )
                    self.assertTrue(
                        self._wait_until(
                            lambda: (
                                monitor_tick.is_set()
                                and self.conn.execute(
                                    "SELECT COUNT(*) FROM file_change_events"
                                ).fetchone()[0]
                                == before_events + 1
                                and document_changes_status(self.conn)["coverage"][
                                    "pending_event_count"
                                ]
                                == 0
                            ),
                            timeout=9,
                        )
                    )

                    event = self.conn.execute(
                        "SELECT change_kind FROM file_change_events WHERE file_id=?",
                        (file_id,),
                    ).fetchone()
                    self.assertIsNotNone(event)
                    self.assertEqual(event["change_kind"], "modified")
                    after_hmac = str(
                        self.conn.execute(
                            "SELECT content_hmac FROM file_watch_state WHERE file_id=?",
                            (file_id,),
                        ).fetchone()[0]
                        or ""
                    )
                    self.assertNotEqual(before_hmac, after_hmac)

                    # A directory transition is also steady-state traffic.  It
                    # must be counted as benign without re-running schema
                    # initialization or stopping the helper.
                    controlled.emit(
                        self._event(
                            self.watch / "directory-transition",
                            550,
                            flags=["item_created", "item_is_dir"],
                        )
                    )
                    self.assertTrue(
                        self._wait_until(
                            lambda: (
                                document_changes_status(self.conn)["coverage"][
                                    "benign_event_count"
                                ]
                                == 1
                                and document_changes_status(self.conn)["coverage"][
                                    "pending_event_count"
                                ]
                                == 0
                            ),
                            timeout=9,
                        )
                    )

                    self.assertFalse(controlled.terminated)
                    self.assertTrue(worker["monitor"].is_alive())
            finally:
                self._stop_worker(worker)

    def test_worker_startup_still_requires_schema_initialization(self):
        """The steady-state fast path must not weaken the startup schema gate."""

        factory = mock.Mock()
        with mock.patch(
            "activity_mvp.storage.init_schema",
            side_effect=AssertionError("startup schema initialization"),
        ):
            with self.assertRaisesRegex(
                AssertionError, "startup schema initialization"
            ):
                start_document_change_worker(
                    self.db,
                    ["/safe/helper"],
                    popen_factory=factory,
                    session_state_provider=lambda: {
                        "state": "unlocked",
                        "locked": False,
                        "on_console": True,
                        "capture_allowed": True,
                    },
                )
        factory.assert_not_called()

    def test_baseline_completes_despite_continuous_benign_churn(self):
        """The owner's machine is never quiet, and the baseline has to cope.

        Every pass of the barriered baseline saw a directory event here, which
        is what iCloud sync, downloads and build output actually do all day.
        The old code demanded one perfectly quiet pass, never got it, and so
        never completed a baseline in eight weeks.  A busy pass is now accepted
        on the final attempt -- and coverage is still reported incomplete, so
        nothing claims more than it knows.
        """

        (self.watch / "tracked.md").write_text("before", encoding="utf-8")
        holder: dict[str, object] = {}
        stop_churn = threading.Event()

        def factory(command, **kwargs):
            process = _ControlledProcess(command, **kwargs)
            holder["process"] = process

            def churn() -> None:
                event_id = 900
                while not stop_churn.wait(0.15):
                    process.emit({
                        "path": str(self.watch / f"folder-{event_id}"),
                        "event_id": str(event_id),
                        "observed_at": datetime.now(timezone.utc).isoformat(),
                        "flags": ["item_renamed", "item_is_dir"],
                    })
                    event_id += 1

            thread = threading.Thread(target=churn, name="benign-churn", daemon=True)
            holder["churn"] = thread
            thread.start()
            return process

        worker = start_document_change_worker(
            self.db,
            ["/safe/helper"],
            popen_factory=factory,
            session_state_provider=lambda: {
                "state": "unlocked", "locked": False,
                "on_console": True, "capture_allowed": True,
            },
        )
        stop_churn.set()
        churn_thread = holder["churn"]
        assert isinstance(churn_thread, threading.Thread)
        churn_thread.join(timeout=4)
        controlled = holder["process"]
        assert isinstance(controlled, _ControlledProcess)
        try:
            self.assertTrue(worker["started"], "a busy machine must still start tracking")
            statuses = {
                str(row["baseline_status"])
                for row in self.conn.execute("SELECT baseline_status FROM file_watch_roots")
            }
            self.assertEqual(statuses, {"complete"})
            completed_at = self.conn.execute(
                "SELECT baseline_completed_at FROM file_watch_roots"
            ).fetchone()[0]
            self.assertIsNotNone(
                completed_at, "baseline_completed_at is the only unforgeable proof of a closed epoch"
            )
            # A closed epoch is a full re-enumeration, so it legitimately clears
            # the gaps it just re-covered -- coverage is complete at this exact
            # point. Ordinary directory churn is counted separately and does
            # not invalidate attribution coverage.
            self.assertTrue(document_changes_status(self.conn)["coverage"]["is_complete"])
            controlled.emit({
                "path": str(self.watch / "folder-after-baseline"),
                "event_id": "990",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "flags": ["item_renamed", "item_is_dir"],
            })
            self.assertTrue(self._wait_until(
                lambda: document_changes_status(self.conn)["coverage"]["benign_event_count"] > 0,
                timeout=5,
            ), "post-baseline directory churn must still be counted")
            coverage = document_changes_status(self.conn)["coverage"]
            self.assertTrue(coverage["is_complete"])
            self.assertEqual(coverage["event_gap_count"], 0)
            self.assertEqual(
                {
                    str(row["baseline_status"])
                    for row in self.conn.execute("SELECT baseline_status FROM file_watch_roots")
                },
                {"complete"},
                "benign churn must not undo the enumeration it did not invalidate",
            )
            # And the monitor must not kill the helper over that benign churn
            # -- three of its two-second ticks go by here.
            self.assertFalse(
                self._wait_until(lambda: controlled.terminated, timeout=7),
                "the monitor stopped the helper for incomplete coverage again",
            )
        finally:
            stop_churn.set()
            self._stop_worker(worker)

    def test_ignore_control_change_marks_partial_without_stopping_the_worker(self):
        (self.watch / "tracked.md").write_text("before", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        worker, controlled = self._start_controlled_worker()
        try:
            (self.watch / ".gitignore").write_text("build/\n", encoding="utf-8")
            controlled.emit({
                "path": str(self.watch / ".gitignore"),
                "event_id": "700",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "flags": ["item_modified", "item_is_file"],
            })
            self.assertTrue(self._wait_until(
                lambda: document_changes_status(self.conn)["coverage"]["event_gap_count"] > 0,
                timeout=5,
            ))
            self.assertFalse(document_changes_status(self.conn)["coverage"]["is_complete"])
            # An ignore-rule edit moves the tracked set; it is not lost stream
            # data, so it must not take the recording channel down with it.
            self.assertFalse(controlled.terminated)
        finally:
            self._stop_worker(worker)

    def test_final_pass_accepts_a_root_that_holds_an_unreadable_file(self):
        """One unreadable file must not make a closed baseline unreachable.

        build_document_baseline counts a single OSError the same as a total
        failure, and the epoch used to abandon itself before the barrier when
        any root came back partial.  Across 44k real files -- encrypted disk
        images, in-flight downloads, iCloud placeholders -- something is always
        unreadable, so that rule alone made a closed baseline impossible on the
        owner's machine.
        """

        class _TwoRootProcess(_ControlledProcess):
            root_count = 2

        second = self.root / "second-watch"
        second.mkdir()
        (second / "fine.md").write_text("readable", encoding="utf-8")
        configure_document_changes(
            self.conn,
            add_root=second,
            root_label="Second",
            runtime_root=self.root / "data",
        )

        blocked = self.watch / "unreadable.md"
        blocked.write_text("cannot be read", encoding="utf-8")
        os.chmod(blocked, 0o000)

        holder: dict[str, _ControlledProcess] = {}

        def factory(command, **kwargs):
            process = _TwoRootProcess(command, **kwargs)
            holder["process"] = process
            return process

        worker = start_document_change_worker(
            self.db,
            ["/safe/helper"],
            popen_factory=factory,
            session_state_provider=lambda: {
                "state": "unlocked", "locked": False,
                "on_console": True, "capture_allowed": True,
            },
        )
        try:
            self.assertTrue(worker["started"])
            self.assertEqual(
                {
                    str(row["baseline_status"])
                    for row in self.conn.execute("SELECT baseline_status FROM file_watch_roots")
                },
                {"complete"},
            )
            # Promoted, but never claiming the gap does not exist.
            self.assertFalse(document_changes_status(self.conn)["coverage"]["is_complete"])
        finally:
            os.chmod(blocked, 0o600)
            self._stop_worker(worker)

    def test_baseline_still_fails_closed_when_no_root_scans_cleanly(self):
        """The floor under the previous test: nothing readable, nothing promoted."""

        blocked = self.watch / "unreadable.md"
        blocked.write_text("cannot be read", encoding="utf-8")
        os.chmod(blocked, 0o000)

        worker, controlled = self._start_controlled_worker()
        try:
            completed = self.conn.execute(
                "SELECT COUNT(*) FROM file_watch_roots WHERE baseline_completed_at IS NOT NULL"
            ).fetchone()[0]
            self.assertEqual(
                completed,
                0,
                "a volume where nothing scanned cleanly must not report a closed baseline",
            )
        finally:
            os.chmod(blocked, 0o600)
            self._stop_worker(worker)

    def test_ai_edit_receipt_is_kept_without_overclaiming_ai_created_origin(self):
        """An AI edit to an existing file is evidence, but not an AI-only file."""

        from activity_mvp import document_changes as document_changes_module

        target = self.watch / "agent-edited.md"
        target.write_text("before", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        changed_at = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=1)
        root_id = str(self.conn.execute("SELECT root_id FROM file_watch_roots").fetchone()[0])
        file_id = document_changes_module._file_id(self.conn, self.watch, target)
        target.write_text("after", encoding="utf-8")
        log = self.root / "edits.jsonl"
        log.write_text(
            json.dumps({
                "at": changed_at.isoformat(),
                "tool": "claude",
                "action": "Write",
                "operation": "modified",
                "root_id": root_id,
                "file_id": file_id,
            }) + "\n",
            encoding="utf-8",
        )
        with (
            mock.patch.object(document_changes_module, "AI_EDIT_LOG_PATH", log),
            mock.patch.object(
                document_changes_module,
                "AI_EDIT_ROTATED_LOG_PATH",
                self.root / "absent.jsonl.1",
            ),
        ):
            process_document_event(
                self.conn,
                self._event(target, 320, observed_at=changed_at.isoformat()),
                root_path=self.watch,
            )
            item = self._all_changes()["items"][0]

        ledger = self.conn.execute(
            "SELECT actor_source,actor_tool,actor_reason_codes FROM file_change_versions"
        ).fetchone()
        self.assertEqual((ledger["actor_source"], ledger["actor_tool"]), ("ai_tool", "claude"))
        self.assertIn("ai_operation_modified", ledger["actor_reason_codes"])
        self.assertEqual(item["actor_classification"], "unattributed")
        self.assertEqual(item["attribution_source"], "unattributed")
        self.assertIn("ai_only_requirements_not_met", item["attribution_reason_codes"])
        # The hook records stable identity and time, never raw path, session, or content.
        record = json.loads(log.read_text(encoding="utf-8").strip())
        self.assertEqual(
            set(record), {"at", "tool", "action", "operation", "root_id", "file_id"}
        )
        self.assertNotIn(str(target), log.read_text(encoding="utf-8"))
        self.assertNotIn("after", log.read_text(encoding="utf-8"))

    def test_recent_sort_order_puts_the_newest_change_first(self):
        older = self.watch / "older.md"
        newer = self.watch / "newer.md"
        older.write_text("a", encoding="utf-8")
        newer.write_text("b", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        older.write_text("a2", encoding="utf-8")
        newer.write_text("b2", encoding="utf-8")
        now = datetime.now(timezone.utc)
        process_document_event(
            self.conn,
            self._event(older, 401, observed_at=(now - timedelta(hours=3)).isoformat()),
            root_path=self.watch,
        )
        process_document_event(
            self.conn, self._event(newer, 402, observed_at=now.isoformat()), root_path=self.watch
        )
        payload = list_document_changes(
            self.conn,
            "2000-01-01T00:00:00+00:00",
            "2100-01-01T00:00:00+00:00",
            sort_order="recent",
        )
        self.assertEqual(
            [item["file_name_redacted"] for item in payload["items"]],
            ["newer.md", "older.md"],
        )

    def test_fast_path_passes_recent_sort_to_listing(self):
        from activity_mvp import document_changes_cli

        listing_payload = {
            "source": "filesystem_confirmed",
            "items": [],
            "limitations": [],
            "raw_access": False,
        }
        with (
            mock.patch.object(
                sys,
                "argv",
                [
                    "pam-document-changes",
                    "--db",
                    str(self.db),
                    "changes",
                    "--sort",
                    "recent",
                ],
            ),
            mock.patch.object(
                document_changes_cli,
                "list_document_changes",
                return_value=listing_payload,
            ) as listed,
            mock.patch("builtins.print"),
        ):
            document_changes_cli.main()

        self.assertEqual(listed.call_args.kwargs["sort_order"], "recent")

    def test_fast_path_marks_authorization_limitations_as_blocking(self):
        from activity_mvp import document_changes_cli

        for limitation in (
            "authorization_unverifiable",
            "authorization_changed_during_read",
        ):
            with self.subTest(limitation=limitation):
                listing_payload = {
                    "source": "filesystem_confirmed",
                    "items": [],
                    "limitations": [limitation],
                    "raw_access": False,
                }
                with (
                    mock.patch.object(
                        sys,
                        "argv",
                        [
                            "pam-document-changes",
                            "--db",
                            str(self.db),
                            "changes",
                        ],
                    ),
                    mock.patch.object(
                        document_changes_cli,
                        "list_document_changes",
                        return_value=listing_payload,
                    ),
                    mock.patch("builtins.print") as printed,
                ):
                    document_changes_cli.main()

                payload = json.loads(printed.call_args.args[0])
                self.assertEqual(payload["status"], "ok")
                self.assertEqual(payload["data_state"], "blocked")
                self.assertEqual(payload["blocking_limitations"], [limitation])
                self.assertEqual(payload["limitations"], [limitation])
                self.assertFalse(payload["raw_access"])

    def test_fast_path_keeps_nonblocking_limitations_readable(self):
        from activity_mvp import document_changes_cli

        for limitation in (
            "coverage_incomplete",
            "requested_range_not_fully_covered",
        ):
            with self.subTest(limitation=limitation):
                listing_payload = {
                    "source": "filesystem_confirmed",
                    "items": [],
                    "limitations": [limitation],
                    "raw_access": False,
                }
                with (
                    mock.patch.object(
                        sys,
                        "argv",
                        [
                            "pam-document-changes",
                            "--db",
                            str(self.db),
                            "changes",
                        ],
                    ),
                    mock.patch.object(
                        document_changes_cli,
                        "list_document_changes",
                        return_value=listing_payload,
                    ),
                    mock.patch("builtins.print") as printed,
                ):
                    document_changes_cli.main()

                payload = json.loads(printed.call_args.args[0])
                self.assertEqual(payload["status"], "ok")
                self.assertEqual(payload["data_state"], "ok")
                self.assertEqual(payload["blocking_limitations"], [])
                self.assertEqual(payload["limitations"], [limitation])
                self.assertFalse(payload["raw_access"])

    def test_document_name_join_key_bridges_both_redaction_pipelines(self):
        """The two sides redact names differently; the join key must reconcile."""

        def signal_side(name: str) -> str:
            stem = Path(name).stem
            suffix = Path(name).suffix
            redacted = re.sub(r"\b\d{4,}\b", "[number]", stem)
            return f"{redacted or 'document'}{suffix.lower()[:12]}"

        for name in (
            "2024年度总结.MD",
            "汉诺威展会2021.pdf",
            "报告【终版】.docx",
            "quarterly-plan.md",
            "archive.tar.gz",
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    _document_name_join_key(name),
                    _document_name_join_key(signal_side(name)),
                    "a name stored by each side must reach the same join key",
                )

        # Negative: normalization must not fold genuinely different names
        # together, or one foreground sighting would light up unrelated files.
        self.assertNotEqual(
            _document_name_join_key("quarterly-plan.md"),
            _document_name_join_key("annual-plan.md"),
        )
        # A name that is only digits carries no distinguishing text once the
        # signal side has masked it, so it must refuse to produce a key rather
        # than collapse every dated sibling onto the suffix alone.
        self.assertEqual(_document_name_join_key("20260807.md"), "")
        self.assertEqual(_document_name_join_key(""), "")

    def test_ignored_directory_events_checkpoint_without_gap_or_restart(self):
        build_document_baseline(self.conn, self.watch)
        worker, controlled = self._start_controlled_worker()
        try:
            for event_id, relative in enumerate(
                ("node_modules/pkg", ".git/objects", "build/tmp"),
                start=548,
            ):
                controlled.emit({
                    "path": str(self.watch / relative),
                    "event_id": str(event_id),
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "flags": ["item_renamed", "item_is_dir"],
                })
            self.assertTrue(self._wait_until(
                lambda: (
                    str(self.conn.execute(
                        "SELECT last_event_id FROM file_watch_checkpoints"
                    ).fetchone()[0]) == "550"
                    and document_changes_status(self.conn)["coverage"]["pending_event_count"] == 0
                ),
                timeout=7,
            ))
            coverage = document_changes_status(self.conn)["coverage"]
            self.assertTrue(coverage["is_complete"])
            self.assertEqual(coverage["event_gap_count"], 0)
            self.assertFalse(controlled.terminated)
            checkpoint = self.conn.execute(
                "SELECT last_event_id FROM file_watch_checkpoints"
            ).fetchone()[0]
            self.assertEqual(checkpoint, "550")
        finally:
            self._stop_worker(worker)

    def test_git_boundary_creation_forces_ignore_reconcile(self):
        ignored = self.watch / "ignored.md"
        (self.watch / ".gitignore").write_text("ignored.md\n", encoding="utf-8")
        ignored.write_text("indexed before repository exists", encoding="utf-8")
        self.assertEqual(build_document_baseline(self.conn, self.watch)["files_indexed"], 1)
        subprocess.run(
            ["git", "init", "-q"],
            cwd=self.watch,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        result = process_document_event(
            self.conn,
            self._event(self.watch / ".git", 5501, flags=["item_created", "item_is_dir"]),
            root_path=self.watch,
        )
        self.assertEqual(result["reason"], "ignore_rules_reconcile_required")
        self.assertFalse(document_changes_status(self.conn)["coverage"]["is_complete"])
        self.assertEqual(build_document_baseline(self.conn, self.watch)["baseline_status"], "complete")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM file_watch_state WHERE present=1"
        ).fetchone()[0], 0)

    def test_direct_ignored_directory_event_does_not_force_reconcile(self):
        build_document_baseline(self.conn, self.watch)
        result = process_document_event(
            self.conn,
            self._event(
                self.watch / "node_modules/pkg",
                551,
                flags=["item_renamed", "item_is_dir"],
            ),
            root_path=self.watch,
        )
        self.assertEqual(result["reason"], "ignored_path")
        self.assertTrue(document_changes_status(self.conn)["coverage"]["is_complete"])

    def test_startup_baseline_ignores_provably_skipped_event_churn(self):
        from activity_mvp import document_changes as module

        original_baseline = module.build_document_baseline
        scan_entered = threading.Event()
        release_scan = threading.Event()
        holder: dict[str, object] = {}
        calls = 0

        def factory(command, **kwargs):
            process = _ControlledProcess(command, **kwargs)
            holder["process"] = process
            return process

        def blocked_baseline(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                scan_entered.set()
                release_scan.wait(timeout=5)
            return original_baseline(*args, **kwargs)

        def launch():
            try:
                holder["worker"] = start_document_change_worker(
                    self.db,
                    ["/safe/helper"],
                    popen_factory=factory,
                    session_state_provider=lambda: {
                        "state": "unlocked", "locked": False,
                        "on_console": True, "capture_allowed": True,
                    },
                )
            except Exception as exc:  # pragma: no cover - assertion reports it
                holder["error"] = exc

        thread = threading.Thread(target=launch)
        with mock.patch("activity_mvp.document_changes.build_document_baseline", side_effect=blocked_baseline):
            thread.start()
            self.assertTrue(scan_entered.wait(timeout=4))
            controlled = holder["process"]
            assert isinstance(controlled, _ControlledProcess)
            for event_id, relative in enumerate(
                ("node_modules/a.js", ".cache/x.json", "editor.tmp", "unsupported.bin"),
                start=552,
            ):
                controlled.emit(self._event(self.watch / relative, event_id))
            release_scan.set()
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertNotIn("error", holder)
        worker = holder["worker"]
        assert isinstance(worker, dict)
        try:
            self.assertFalse(controlled.terminated)
            self.assertTrue(document_changes_status(self.conn)["coverage"]["is_complete"])
        finally:
            self._stop_worker(worker)

    def test_document_epoch_and_pending_updates_are_atomic(self):
        from activity_mvp import document_changes as module

        base_epoch = module._document_change_epoch(self.conn)
        barrier = threading.Barrier(8)
        values: list[int] = []
        errors: list[Exception] = []

        def bump_once():
            conn = ensure_ready(self.db)
            try:
                barrier.wait(timeout=5)
                values.append(module.bump_document_change_epoch(conn))
                conn.commit()
            except Exception as exc:
                errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=bump_once) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=8)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(values), list(range(base_epoch + 1, base_epoch + 9)))

        # Stay far above zero so clamping cannot make a balanced concurrent
        # update order-dependent; any drift is then a lost read/modify/write.
        module._set_document_change_pending(self.conn, 100)
        self.conn.commit()
        barrier = threading.Barrier(8)
        errors.clear()

        def adjust_once(delta: int):
            conn = ensure_ready(self.db)
            try:
                barrier.wait(timeout=5)
                module._adjust_document_change_pending(conn, delta)
                conn.commit()
            except Exception as exc:
                errors.append(exc)
            finally:
                conn.close()

        deltas = [1, -1, 1, -1, 1, -1, 1, -1]
        threads = [threading.Thread(target=adjust_once, args=(delta,)) for delta in deltas]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=8)
        self.assertEqual(errors, [])
        self.assertEqual(module._document_change_pending_count(self.conn), 100)

    def test_stale_baseline_cannot_delete_replacement_baseline_state(self):
        path = self.watch / "replacement.md"
        path.write_text("replacement baseline", encoding="utf-8")
        parked = threading.Event()
        resume_old = threading.Event()
        result: dict[str, object] = {}

        def candidates():
            parked.set()
            resume_old.wait(timeout=8)
            yield path

        def old_scan():
            old_conn = ensure_ready(self.db)
            try:
                result.update(build_document_baseline(old_conn, self.watch, paths=candidates()))
            finally:
                old_conn.close()

        thread = threading.Thread(target=old_scan)
        try:
            thread.start()
            self.assertTrue(parked.wait(timeout=4))
            configure_document_changes(self.conn, reset_baseline=True)
            self.assertEqual(build_document_baseline(self.conn, self.watch)["baseline_status"], "complete")
            self.assertEqual(self.conn.execute(
                "SELECT COUNT(*) FROM file_watch_state WHERE present=1"
            ).fetchone()[0], 1)
            resume_old.set()
            thread.join(timeout=8)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result.get("baseline_status"), "authorization_changed")
            self.assertEqual(self.conn.execute(
                "SELECT COUNT(*) FROM file_watch_state WHERE present=1"
            ).fetchone()[0], 1)
            self.assertTrue(document_changes_status(self.conn)["coverage"]["is_complete"])
        finally:
            resume_old.set()
            thread.join(timeout=2)

    def test_symlink_swap_cannot_read_outside_watched_root(self):
        from activity_mvp import document_changes as module

        path = self.watch / "race.md"
        outside = self.root / "outside-secret.md"
        path.write_text("before", encoding="utf-8")
        outside.write_text("OUTSIDE-PRIVATE-CONTENT", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        path.write_text("after", encoding="utf-8")
        original_stat = module._safe_file_stat
        calls = 0
        extracted: list[str] = []

        def swap_after_authorized_stat(root, target):
            nonlocal calls
            value = original_stat(root, target)
            calls += 1
            if calls == 1:
                path.unlink()
                path.symlink_to(outside)
            return value

        original_extract = module._extract_structure

        def observe_extract(*args, **kwargs):
            extracted.append("called")
            return original_extract(*args, **kwargs)

        with mock.patch("activity_mvp.document_changes._safe_file_stat", side_effect=swap_after_authorized_stat), mock.patch(
            "activity_mvp.document_changes._extract_structure", side_effect=observe_extract
        ):
            result = process_document_event(self.conn, self._event(path, 560), root_path=self.watch)
        self.assertFalse(result["recorded"])
        self.assertEqual(result["reason"], "unstable_or_unreadable")
        self.assertEqual(extracted, [])
        stored = json.dumps([
            dict(row) for row in self.conn.execute("SELECT * FROM file_watch_state")
        ], ensure_ascii=False)
        self.assertNotIn("OUTSIDE-PRIVATE-CONTENT", stored)

    def test_large_pdf_is_stream_hashed_without_materializing_content(self):
        path = self.watch / "large.pdf"
        with path.open("wb") as handle:
            handle.truncate(49 * 1024 * 1024)
        with mock.patch(
            "activity_mvp.document_changes._read_open_file",
            side_effect=AssertionError("metadata-only PDF must not be materialized"),
        ):
            result = build_document_baseline(self.conn, self.watch)
        self.assertEqual(result["baseline_status"], "complete")
        row = self.conn.execute(
            "SELECT detail_level,size_bytes FROM file_watch_state WHERE present=1"
        ).fetchone()
        self.assertEqual(row["detail_level"], "metadata_only")
        self.assertEqual(row["size_bytes"], 49 * 1024 * 1024)

    def test_protected_to_safe_transition_remains_count_only_and_sticky(self):
        path = self.watch / "neutral.md"
        path.write_text("客户姓名：张三 手机号：13800000000", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        path.write_text("# Public\nAll safe", encoding="utf-8")
        first = process_document_event(self.conn, self._event(path, 546), root_path=self.watch)
        self.assertEqual(first["item"]["detail_level"], "protected")
        self.assertEqual(first["item"]["file_name_redacted"], "")
        path.write_text("# Public\nStill safe", encoding="utf-8")
        second = process_document_event(self.conn, self._event(path, 547), root_path=self.watch)
        self.assertEqual(second["item"]["detail_level"], "protected")
        listed = self._all_changes()
        self.assertEqual(listed["totals"]["protected"], 1)
        self.assertEqual(listed["items"], [])
        encoded = json.dumps([dict(row) for row in self.conn.execute("SELECT * FROM file_change_events")], ensure_ascii=False)
        self.assertNotIn("neutral.md", encoded)
        self.assertNotIn("Public", encoded)

    def test_swift_helper_uses_full_history_and_object_type_flags(self):
        helper_source = (Path(__file__).resolve().parents[1] / "tools/fsevents_watch.swift").read_text(encoding="utf-8")
        self.assertIn("kFSEventStreamCreateFlagFullHistory", helper_source)
        self.assertIn('names.append("item_is_dir")', helper_source)
        self.assertIn('names.append("item_is_file")', helper_source)

    def test_malformed_live_protocol_immediately_marks_partial_and_stops(self):
        path = self.watch / "protocol.md"
        path.write_text("before", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        worker, controlled = self._start_controlled_worker()
        try:
            controlled.stdout.emit({
                "path": str(path),
                "event_id": "1bad",
                "flags": ["item_modified"],
            })
            self.assertTrue(self._wait_until(lambda: controlled.terminated, timeout=4))
            self.conn.close()
            self.conn = ensure_ready(self.db)
            self.assertFalse(document_changes_status(self.conn)["coverage"]["is_complete"])
        finally:
            self._stop_worker(worker)

    def test_symlink_root_and_symlink_component_are_rejected_before_resolve(self):
        real = self.root / "real-root"
        real.mkdir()
        alias = self.root / "root-alias"
        alias.symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            configure_document_changes(
                self.conn,
                add_root=alias,
                root_label="Alias",
                runtime_root=self.root / "data",
            )
        parent_alias = self.root / "parent-alias"
        parent_alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            configure_document_changes(
                self.conn,
                add_root=parent_alias / "real-root",
                root_label="Nested alias",
                runtime_root=self.root / "data",
            )
        self.assertNotIn("root-alias", (self.root / "data/document_watch_roots.json").read_text(encoding="utf-8"))

    def test_future_or_invalid_range_is_never_reported_complete(self):
        (self.watch / "range.md").write_text("current", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        coverage_start = document_changes_status(self.conn)["coverage"]["coverage_started_at"]
        time.sleep(0.02)
        now = datetime.now(timezone.utc)
        covered = list_document_changes(self.conn, coverage_start, now.isoformat())
        self.assertTrue(covered["coverage"]["is_complete"])
        future = list_document_changes(
            self.conn,
            coverage_start,
            (now + timedelta(days=1)).isoformat(),
        )
        self.assertFalse(future["coverage"]["is_complete"])
        invalid = list_document_changes(self.conn, now.isoformat(), coverage_start)
        self.assertFalse(invalid["coverage"]["is_complete"])

    def test_office_metadata_only_save_does_not_claim_content_edit(self):
        docx = self.watch / "metadata-only.docx"
        document_xml = "<w:document xmlns:w='w'><w:body><w:p><w:r><w:t>Same paragraph</w:t></w:r></w:p></w:body></w:document>"
        self._write_zip(docx, {
            "word/document.xml": document_xml,
            "docProps/core.xml": "<core>first</core>",
        })
        build_document_baseline(self.conn, self.watch)
        self._write_zip(docx, {
            "word/document.xml": document_xml,
            "docProps/core.xml": "<core>second</core>",
        })
        result = process_document_event(self.conn, self._event(docx, 540), root_path=self.watch)
        self.assertEqual(result["item"]["detail_level"], "metadata_only")
        self.assertEqual(
            result["item"]["change_summary_redacted"],
            "检测到文件变化，但没有可安全概括的前后内容。",
        )

    def test_pptx_natural_order_reports_tenth_slide_not_second(self):
        pptx = self.watch / "ten-slides.pptx"
        before = {
            f"ppt/slides/slide{index}.xml": (
                f"<p:sld xmlns:p='p' xmlns:a='a'><a:t>Slide {index}</a:t><a:t>Before</a:t></p:sld>"
            )
            for index in range(1, 11)
        }
        self._write_zip(pptx, before)
        build_document_baseline(self.conn, self.watch)
        after = dict(before)
        after["ppt/slides/slide10.xml"] = "<p:sld xmlns:p='p' xmlns:a='a'><a:t>Slide 10</a:t><a:t>After</a:t></p:sld>"
        self._write_zip(pptx, after)
        result = process_document_event(self.conn, self._event(pptx, 550), root_path=self.watch)
        self.assertIn("第 10 页", result["item"]["change_summary_redacted"])
        self.assertNotIn("第 2 页", result["item"]["change_summary_redacted"])

    def test_office_part_declaring_an_entity_is_refused_before_expansion(self):
        # A modest entity stays under libexpat's amplification limit, so the
        # parser would happily expand it.  Refusing the doctype is what keeps
        # the declared text out of the record.
        docx = self.watch / "entity.docx"
        plain = "<w:document xmlns:w='w'><w:body><w:p><w:r><w:t>Plain paragraph</w:t></w:r></w:p></w:body></w:document>"
        self._write_zip(docx, {"word/document.xml": plain})
        build_document_baseline(self.conn, self.watch)
        self._write_zip(docx, {
            "word/document.xml": (
                "<?xml version='1.0'?>"
                "<!DOCTYPE w:document [<!ENTITY smuggled 'ENTITYSENTINEL'>]>"
                "<w:document xmlns:w='w'><w:body><w:p><w:r><w:t>&smuggled;</w:t></w:r></w:p></w:body></w:document>"
            ),
        })
        result = process_document_event(self.conn, self._event(docx, 560), root_path=self.watch)
        # The part is treated exactly like a malformed one: no paragraph is
        # extracted, so nothing the entity declared can reach the record.
        self.assertNotIn("ENTITYSENTINEL", json.dumps(result, ensure_ascii=False))
        self.assertIn("新增约 0", result["item"]["change_summary_redacted"])

    def test_office_entity_expansion_bomb_never_reaches_the_record(self):
        # Belt and braces: a libexpat new enough to enforce its own
        # amplification limit already refuses this payload, so the assertion
        # holds with or without the doctype guard.  It is pinned anyway because
        # the outcome -- no expansion, no wedged worker -- has to survive a
        # swap of the interpreter or the library underneath it.
        docx = self.watch / "bomb.docx"
        plain = "<w:document xmlns:w='w'><w:body><w:p><w:r><w:t>Plain paragraph</w:t></w:r></w:p></w:body></w:document>"
        self._write_zip(docx, {"word/document.xml": plain})
        build_document_baseline(self.conn, self.watch)
        declarations = "".join(
            f"<!ENTITY lol{index} '{'&lol%d;' % (index - 1) * 10}'>"
            for index in range(1, 10)
        )
        self._write_zip(docx, {
            "word/document.xml": (
                "<?xml version='1.0'?>"
                f"<!DOCTYPE w:document [<!ENTITY lol0 'lol'>{declarations}]>"
                "<w:document xmlns:w='w'><w:body><w:p><w:r><w:t>&lol9;</w:t></w:r></w:p></w:body></w:document>"
            ),
        })
        started = time.monotonic()
        result = process_document_event(self.conn, self._event(docx, 561), root_path=self.watch)
        self.assertLess(time.monotonic() - started, 10.0)
        self.assertNotIn("lollol", json.dumps(result, ensure_ascii=False))
        self.assertIn("新增约 0", result["item"]["change_summary_redacted"])

    def test_parse_office_xml_refuses_a_doctype_in_every_package_encoding(self):
        clean = b"<w:document xmlns:w='w'><w:body><w:p><w:t>Kept</w:t></w:p></w:body></w:document>"
        self.assertEqual(_parse_office_xml(clean).tag.rsplit("}", 1)[-1], "document")
        doctype = "<!DOCTYPE w:document [<!ENTITY a 'b'>]><w:document xmlns:w='w'/>"
        for encoding in ("utf-8", "utf-16-le", "utf-16-be"):
            with self.subTest(encoding=encoding):
                with self.assertRaises(ElementTree.ParseError):
                    _parse_office_xml(doctype.encode(encoding))

    def test_cli_status_list_and_configure_return_only_safe_contract(self):
        path = self.watch / "cli.md"
        path.write_text("before", encoding="utf-8")
        build_document_baseline(self.conn, self.watch)
        path.write_text("after", encoding="utf-8")
        process_document_event(self.conn, self._event(path, 170), root_path=self.watch)
        configure_document_changes(self.conn, export_enabled=True, runtime_root=self.root / "data")

        def run_cli(*arguments: str) -> dict:
            completed = subprocess.run(
                [sys.executable, "-m", "activity_mvp", "document-changes", *arguments, "--db", str(self.db)],
                check=True,
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            return json.loads(completed.stdout)

        status = run_cli("status")
        listed = run_cli("list", "--from", "2000-01-01T00:00:00+00:00", "--to", "2100-01-01T00:00:00+00:00", "--limit", "12")
        configured = run_cli("configure", "--export-enabled", "off")
        internal_root_ids = [str(row["root_id"]) for row in self.conn.execute("SELECT root_id FROM file_watch_roots")]
        for payload in (status, listed, configured["document_changes"]):
            self.assertEqual(payload["source"], "filesystem_confirmed")
            self.assertIs(payload["raw_access"], False)
            self.assertTrue({"coverage", "totals", "items", "limitations"} <= payload.keys())
            encoded = json.dumps(payload, ensure_ascii=False)
            self.assertNotIn(str(self.watch), encoded)
            self.assertNotIn("content_hmac", encoded)
            self.assertNotIn('"root_id"', encoded)
            for root_id in internal_root_ids:
                self.assertNotIn(root_id, encoded)
                self.assertNotIn(root_id.removeprefix("root_"), encoded)
        for root in status["roots"]:
            self.assertEqual(set(root).intersection({"root_id", "path", "full_path"}), set())
            self.assertRegex(root["root_ref"], r"\Arootref_[A-Za-z0-9_-]{24}\Z")
        self.assertEqual(listed["totals"]["distinct_files"], 1)
        self.assertFalse(configured["document_changes"]["coverage"]["export_enabled"])

    def test_helper_parser_is_argv_only_and_accepts_strict_jsonl(self):
        helper = FileEventHelperProcess(["/safe/helper", "--since", "1"], [self.watch], popen_factory=_Process).start()
        self.assertEqual(helper.process.command, ["/safe/helper", "--since", "1", str(self.watch)])
        self.assertEqual(
            list(helper.events()),
            [{"path": "/tmp/a.md", "event_id": "7", "flags": ["item_modified"]}],
        )
        helper.stop()
        self.assertTrue(helper.process.terminated)


if __name__ == "__main__":
    unittest.main()
