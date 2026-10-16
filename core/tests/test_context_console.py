from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import activity_mvp.activity_confirmations as activity_confirmations_module
import activity_mvp.context_console as context_console_module
import activity_mvp.storage as storage_module

from activity_mvp.activity_confirmations import (
    _member_revision,
    _upsert_confirmation_candidate,
    answer_activity_confirmation_group,
    list_activity_confirmation_cards,
)
from activity_mvp.confirmation_evidence import ensure_evidence_root
from activity_mvp.context_console import (
    CONTEXT_BUNDLE_VERSION,
    CONTEXT_FEATURE_CONFIRMATION,
    CONTEXT_RECEIPT_VERSION,
    CONTEXT_SNAPSHOT_VERSION,
    CONTROLLED_EVIDENCE_VERSION,
    DELIVERY_RECEIPT_VERSION,
    DELIVERY_STATUS_VERSION,
    LOCAL_HANDOFF_VERSION,
    MAX_CONTRACT_BYTES,
    MEMORY_CANDIDATE_LIST_VERSION,
    OWNER_CONTEXT_ACTION_CONFIRMATION,
    OWNER_CONTEXT_ACTION_VERSION,
    PRODUCT_EXPERIMENT_FINALIZE_CONFIRMATION,
    PRODUCT_EXPERIMENT_STATUS_VERSION,
    PRODUCT_FEEDBACK_VERSION,
    build_context_bundle,
    build_local_handoff,
    classify_stability_change,
    cleanup_context_retention,
    configure_context_console,
    configure_context_local_timezone,
    context_console_enabled,
    context_snapshot,
    context_console_status,
    context_console_status_path,
    create_memory_candidate,
    delete_context_observation,
    delivery_status,
    fetch_prepared_delivery,
    finalize_product_experiment,
    ingest_context_receipt,
    ingest_controlled_evidence,
    ingest_delivery_receipt,
    list_memory_candidates,
    owner_context_action,
    phase2_unknown_contract,
    prepare_delivery,
    public_product_experiment_status,
    preview_context_retention,
    product_experiment_status,
    product_win_status,
    record_component_baseline,
    record_product_feedback,
    record_product_experiment_attempt,
    record_product_win,
    record_unverified_clue,
    register_context_label,
    register_context_principal,
    register_document_context_mapping,
    report_context_bundle_adapter,
    revoke_context_label,
    revoke_context_observations_for_range,
    revoke_context_principal,
    runtime_stability_status,
    sign_contract_payload,
    start_product_experiment,
)
from activity_mvp.document_changes import build_document_baseline, configure_document_changes, process_document_event
from activity_mvp.ha_automation import DEFAULT_HA_AUTO_ENABLED, configure_ha_automation
from activity_mvp.ha_source import _safe_timeline_events
from activity_mvp.lifecycle import delete_range
from activity_mvp.memory_approval import CONTROLLED_MEM0_WRITER_ENABLED
from activity_mvp.report_context import _highlight, report_context
from activity_mvp.reclassify import _delete_activity_confirmation_items
from activity_mvp.stability import build_soak_report, record_resource_baseline
from activity_mvp.storage import SCHEMA_VERSION, connect, ensure_ready


UTC = timezone.utc


# Copied from the supplied v0.4 reference source's schema-29 storage contract.
# This is intentionally the real four-table/task-ledger shape plus every
# reference-defined index, not a marker-only migration fixture.
V04_SCHEMA29_SQL = """
CREATE TABLE IF NOT EXISTS task_episodes (
  episode_id TEXT PRIMARY KEY,
  explicit_task_id TEXT NOT NULL DEFAULT '',
  workstream TEXT NOT NULL DEFAULT '',
  subject TEXT NOT NULL DEFAULT '',
  object TEXT NOT NULL DEFAULT '',
  version TEXT NOT NULL DEFAULT '',
  current_state TEXT NOT NULL DEFAULT 'intent_observed',
  execution_status TEXT NOT NULL DEFAULT 'status_unknown',
  execution_status_updated_at TEXT NOT NULL DEFAULT '',
  root_turn_ref TEXT NOT NULL DEFAULT '',
  project_ref TEXT NOT NULL DEFAULT '',
  binding_mode TEXT NOT NULL DEFAULT '',
  highest_source_tier TEXT NOT NULL DEFAULT 'P5',
  confirmation_state TEXT NOT NULL DEFAULT 'pending',
  privacy_class TEXT NOT NULL DEFAULT 'unknown',
  memory_write_eligible INTEGER NOT NULL DEFAULT 0,
  evidence_ids TEXT NOT NULL DEFAULT '[]',
  confirmation_item_key TEXT NOT NULL DEFAULT '',
  started_at TEXT NOT NULL,
  ended_at TEXT NOT NULL,
  event_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_evidence_events (
  event_id TEXT PRIMARY KEY,
  episode_id TEXT,
  task_id TEXT NOT NULL DEFAULT '',
  occurred_at TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  source_tier TEXT NOT NULL,
  workstream TEXT NOT NULL DEFAULT '',
  subject TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL DEFAULT '',
  object TEXT NOT NULL DEFAULT '',
  version TEXT NOT NULL DEFAULT '',
  state_claim TEXT NOT NULL,
  execution_status TEXT NOT NULL DEFAULT 'status_unknown',
  session_ref TEXT NOT NULL DEFAULT '',
  turn_ref TEXT NOT NULL DEFAULT '',
  root_turn_ref TEXT NOT NULL DEFAULT '',
  project_ref TEXT NOT NULL DEFAULT '',
  binding_mode TEXT NOT NULL DEFAULT '',
  evidence_ids TEXT NOT NULL DEFAULT '[]',
  confidence REAL NOT NULL DEFAULT 0.0,
  confirmation_state TEXT NOT NULL DEFAULT 'pending',
  privacy_class TEXT NOT NULL DEFAULT 'unknown',
  memory_write_eligible INTEGER NOT NULL DEFAULT 0,
  contract_version INTEGER NOT NULL DEFAULT 1,
  source_ref TEXT NOT NULL DEFAULT '',
  event_fingerprint TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(episode_id) REFERENCES task_episodes(episode_id)
    ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS hermes_outbox (
  outbox_id TEXT PRIMARY KEY,
  episode_id TEXT,
  event_id TEXT,
  payload_kind TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  attempt_count INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,
  last_error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  delivered_at TEXT,
  FOREIGN KEY(episode_id) REFERENCES task_episodes(episode_id)
    ON DELETE CASCADE,
  FOREIGN KEY(event_id) REFERENCES task_evidence_events(event_id)
    ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS hermes_delivery_receipts (
  receipt_id TEXT PRIMARY KEY,
  outbox_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  delivery_status TEXT NOT NULL,
  provider_receipt_id TEXT NOT NULL DEFAULT '',
  attempt_number INTEGER NOT NULL DEFAULT 1,
  acknowledged_at TEXT NOT NULL,
  safe_metadata TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  FOREIGN KEY(outbox_id) REFERENCES hermes_outbox(outbox_id)
    ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_task_episode_task_id
  ON task_episodes(explicit_task_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_task_episode_identity
  ON task_episodes(workstream, subject, object, version, ended_at);
CREATE INDEX IF NOT EXISTS idx_task_episode_state
  ON task_episodes(confirmation_state, current_state, updated_at);
CREATE INDEX IF NOT EXISTS idx_task_evidence_episode
  ON task_evidence_events(episode_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_task_evidence_time
  ON task_evidence_events(occurred_at, source_tier);
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_evidence_fingerprint
  ON task_evidence_events(event_fingerprint);
CREATE INDEX IF NOT EXISTS idx_hermes_outbox_status
  ON hermes_outbox(status, next_attempt_at, created_at);
CREATE INDEX IF NOT EXISTS idx_hermes_outbox_episode
  ON hermes_outbox(episode_id, created_at);
CREATE INDEX IF NOT EXISTS idx_hermes_receipt_outbox
  ON hermes_delivery_receipts(outbox_id, acknowledged_at);
CREATE INDEX IF NOT EXISTS idx_task_evidence_codex_turn
  ON task_evidence_events(turn_ref, occurred_at);
CREATE INDEX IF NOT EXISTS idx_task_evidence_codex_session
  ON task_evidence_events(session_ref, occurred_at);
CREATE INDEX IF NOT EXISTS idx_task_episode_codex_project
  ON task_episodes(project_ref, execution_status, updated_at);
"""

V04_SCHEMA29_OBJECTS = (
    "task_episodes",
    "task_evidence_events",
    "hermes_outbox",
    "hermes_delivery_receipts",
    "idx_task_episode_task_id",
    "idx_task_episode_identity",
    "idx_task_episode_state",
    "idx_task_evidence_episode",
    "idx_task_evidence_time",
    "idx_task_evidence_fingerprint",
    "idx_hermes_outbox_status",
    "idx_hermes_outbox_episode",
    "idx_hermes_receipt_outbox",
    "idx_task_evidence_codex_turn",
    "idx_task_evidence_codex_session",
    "idx_task_episode_codex_project",
)


class ContextConsoleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "pam.sqlite"
        self.conn = ensure_ready(self.db)
        self.now = datetime(2026, 7, 29, 1, 0, 0, tzinfo=UTC)
        self.default_enabled = context_console_enabled(self.conn)

        self.project = "project_aaaaaaaa"
        self.other_project = "project_bbbbbbbb"
        self.workstream = "workstream_cccccccc"
        self.object = "object_dddddddd"
        self.version = "version_eeeeeeee"
        self.owner = "subject_ffffffff"
        self.third_party = "subject_abababab"
        self.target = "target_cdcdcdcd"
        self.other_target = "target_dededede"
        self.other_project_target = "target_efefefef"

        self._register_label("project", self.project, "Project Alpha")
        self._register_label("project", self.other_project, "Project Beta")
        self._register_label("workstream", self.workstream, "Release Readiness")
        self._register_label("object", self.object, "Context Contract")
        self._register_label("version", self.version, "Candidate V1")
        self._register_label("subject", self.owner, "Owner", subject_scope="owner")
        self._register_label(
            "subject",
            self.third_party,
            "External Person",
            subject_scope="third_party",
        )
        self._register_label("target", self.target, "Local Handoff")
        self._register_label("target", self.other_target, "Secondary Handoff")
        self._register_label(
            "target",
            self.other_project_target,
            "Other Project Handoff",
            project_ref=self.other_project,
        )

        self.secrets = {
            "ai": b"ai-secret-0123456789",
            "user": b"user-secret-01234567",
            "file": b"file-secret-01234567",
            "git": b"git-secret-012345678",
            "external": b"external-secret-0123",
            "consumer": b"consumer-secret-0123",
            "consumer_other": b"consumer-other-secret",
            "provider": b"provider-secret-0123",
        }
        self.principals = {
            "ai": ("principal_a1a1a1a1", "key_a1a1a1a1", "ai_lifecycle"),
            "user": ("principal_b2b2b2b2", "key_b2b2b2b2", "user_confirmation"),
            "file": ("principal_c3c3c3c3", "key_c3c3c3c3", "file_adapter"),
            "git": ("principal_d4d4d4d4", "key_d4d4d4d4", "git_adapter"),
            "external": ("principal_e5e5e5e5", "key_e5e5e5e5", "external_adapter"),
            "consumer": ("principal_f6f6f6f6", "key_f6f6f6f6", "bundle_consumer"),
            "consumer_other": ("principal_18181818", "key_18181818", "bundle_consumer"),
            "provider": ("principal_17171717", "key_17171717", "delivery_provider"),
        }
        for name, (principal_id, key_id, kind) in self.principals.items():
            project_ref = self.project
            target_ref = ""
            target_scope = ""
            if kind in {"bundle_consumer", "delivery_provider"}:
                target_ref = self.other_target if name == "consumer_other" else self.target
                target_scope = "local"
            result = register_context_principal(
                self.conn,
                principal_id=principal_id,
                key_id=key_id,
                principal_kind=kind,
                verification_secret=self.secrets[name],
                project_ref=project_ref,
                target_ref=target_ref,
                target_scope=target_scope,
            )
            self.assertEqual(result["status"], "registered")

        enabled = configure_context_console(
            self.conn,
            enabled=True,
            confirmation=CONTEXT_FEATURE_CONFIRMATION,
        )
        self.assertEqual(enabled["status"], "enabled")

    def tearDown(self) -> None:
        self.conn.close()
        self.temp.cleanup()

    def _register_label(
        self,
        kind: str,
        ref: str,
        label: str,
        *,
        subject_scope: str = "",
        project_ref: str | None = None,
        target_scope: str = "local",
        allowed_privacy_classes: list[str] | None = None,
        export_policy: str = "local_only",
    ) -> None:
        kwargs = {
            "kind": kind,
            "ref": ref,
            "label": label,
            "project_ref": "" if kind == "project" else (project_ref or self.project),
            "subject_scope": subject_scope,
        }
        if kind == "target":
            kwargs.update(
                {
                    "target_scope": target_scope,
                    "allowed_privacy_classes": list(
                        allowed_privacy_classes
                        if allowed_privacy_classes is not None
                        else ["private_local", "sensitive_local", "restricted_local"]
                    ),
                    "export_policy": export_policy,
                }
            )
        result = register_context_label(self.conn, **kwargs)
        self.assertEqual(result["status"], "registered")

    def _receipt_payload(
        self,
        *,
        idempotency_key: str = "idem_00000001",
        lifecycle_event: str = "answer_ready",
        occurred_at: datetime | None = None,
        evidence_refs: list[str] | None = None,
        project_ref: str | None = None,
        workstream_ref: str | None = None,
        object_ref: str | None = None,
        version_ref: str | None = None,
        subject_ref: str | None = None,
        source_ref: str = "source_11111111",
        principal_name: str = "ai",
    ) -> dict:
        principal_id, key_id, _ = self.principals[principal_name]
        payload = {
            "contract_version": CONTEXT_RECEIPT_VERSION,
            "idempotency_key": idempotency_key,
            "producer_id": principal_id,
            "key_id": key_id,
            "source_ref": source_ref,
            "occurred_at": (occurred_at or self.now).isoformat(timespec="seconds"),
            "project_ref": project_ref or self.project,
            "workstream_ref": workstream_ref or self.workstream,
            "object_ref": object_ref or self.object,
            "version_ref": version_ref or self.version,
            "subject_ref": subject_ref or self.owner,
            "privacy_class": "private_local",
            "lifecycle_event": lifecycle_event,
            "evidence_refs": list(evidence_refs or []),
            "signature": "",
        }
        payload["signature"] = sign_contract_payload(payload, self.secrets[principal_name])
        return payload

    def _controlled_payload(
        self,
        *,
        principal_name: str,
        evidence_kind: str,
        state: str,
        idempotency_key: str,
        occurred_at: datetime | None = None,
        provider_receipt_ref: str = "",
        project_ref: str | None = None,
        workstream_ref: str | None = None,
        object_ref: str | None = None,
        version_ref: str | None = None,
        subject_ref: str | None = None,
        source_ref: str = "source_22222222",
        privacy_class: str = "private_local",
        verification_scope: str | None = None,
        memory_eligible: bool | None = None,
    ) -> dict:
        principal_id, key_id, _ = self.principals[principal_name]
        effective_subject = subject_ref or self.owner
        if memory_eligible is None:
            memory_eligible = (
                evidence_kind == "user_confirmation"
                and effective_subject == self.owner
                and privacy_class != "restricted_local"
            )
        if verification_scope is None:
            verification_scope = "memory" if memory_eligible else "operational"
        payload = {
            "contract_version": CONTROLLED_EVIDENCE_VERSION,
            "idempotency_key": idempotency_key,
            "principal_id": principal_id,
            "key_id": key_id,
            "source_ref": source_ref,
            "occurred_at": (occurred_at or self.now).isoformat(timespec="seconds"),
            "project_ref": project_ref or self.project,
            "workstream_ref": workstream_ref or self.workstream,
            "object_ref": object_ref or self.object,
            "version_ref": version_ref or self.version,
            "subject_ref": effective_subject,
            "privacy_class": privacy_class,
            "evidence_kind": evidence_kind,
            "state": state,
            "provider_receipt_ref": provider_receipt_ref,
            "verification_scope": verification_scope,
            "memory_eligible": memory_eligible,
            "signature": "",
        }
        payload["signature"] = sign_contract_payload(payload, self.secrets[principal_name])
        return payload

    def _delivery_payload(
        self,
        *,
        principal_name: str,
        delivery_ref: str,
        bundle_hash: str,
        transition: str,
        idempotency_key: str,
        provider_receipt_ref: str = "",
        failure_reason_code: str = "",
        occurred_at: datetime | None = None,
        bundle_ref: str | None = None,
        attempt_no: int | None = None,
        project_ref: str | None = None,
        target_ref: str | None = None,
        target_scope: str | None = None,
        export_policy: str | None = None,
        privacy_policy_hash: str | None = None,
        principal_role: str | None = None,
    ) -> dict:
        principal_id, key_id, principal_kind = self.principals[principal_name]
        delivery = self.conn.execute(
            "SELECT * FROM context_deliveries WHERE delivery_ref=?", (delivery_ref,)
        ).fetchone()
        self.assertIsNotNone(delivery)
        role = "consumer" if principal_kind == "bundle_consumer" else "provider"
        payload = {
            "contract_version": DELIVERY_RECEIPT_VERSION,
            "idempotency_key": idempotency_key,
            "principal_id": principal_id,
            "key_id": key_id,
            "delivery_ref": delivery_ref,
            "bundle_ref": bundle_ref if bundle_ref is not None else str(delivery["bundle_ref"]),
            "attempt_no": attempt_no if attempt_no is not None else int(delivery["attempt_no"]),
            "project_ref": project_ref if project_ref is not None else str(delivery["project_ref"]),
            "target_ref": target_ref if target_ref is not None else str(delivery["target_ref"]),
            "target_scope": target_scope if target_scope is not None else str(delivery["target_scope"]),
            "export_policy": export_policy if export_policy is not None else str(delivery["export_policy"]),
            "privacy_policy_hash": (
                privacy_policy_hash
                if privacy_policy_hash is not None
                else str(delivery["privacy_policy_hash"])
            ),
            "principal_role": principal_role if principal_role is not None else role,
            "transition": transition,
            "occurred_at": (occurred_at or self.now).isoformat(timespec="seconds"),
            "bundle_hash": bundle_hash,
            "provider_receipt_ref": provider_receipt_ref,
            "failure_reason_code": failure_reason_code,
            "signature": "",
        }
        payload["signature"] = sign_contract_payload(payload, self.secrets[principal_name])
        return payload

    @staticmethod
    def _wire(payload: dict) -> bytes:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def _phase2_manual_action_payload(
        self,
        *,
        idempotency_key: str,
        safe_label: str = "Phase 2 contract implementation",
        context_state: str = "in_progress",
        started_at: datetime | None = None,
    ) -> dict[str, Any]:
        occurred = started_at or self.now
        return {
            "contract_version": OWNER_CONTEXT_ACTION_VERSION,
            "action": "manual_note",
            "idempotency_key": idempotency_key,
            "confirmation_action": OWNER_CONTEXT_ACTION_CONFIRMATION,
            "safe_label": safe_label,
            "workstream": "Release Readiness",
            "outcome": "progressing",
            "started_at": occurred.isoformat(timespec="seconds"),
            "ended_at": occurred.isoformat(timespec="seconds"),
            "project_ref": self.project,
            "workstream_ref": self.workstream,
            "object_ref": self.object,
            "version_ref": self.version,
            "subject_ref": self.owner,
            "privacy_class": "private_local",
            "context_state": context_state,
        }

    def _insert_phase2_confirmation_card(
        self,
        item_key: str,
        *,
        object_label: str,
        at: datetime | None = None,
        answerable: bool = True,
    ) -> None:
        observed = at or datetime.now().astimezone().replace(microsecond=0)
        subject = {
            "event_label": "完成事项",
            "company": "",
            "round": "",
            "object": object_label if answerable else "",
            "label": f"{object_label}完成" if answerable else "完成事项",
            "company_identified": False,
            "round_identified": False,
            "object_identified": answerable,
        }
        _upsert_confirmation_candidate(
            self.conn,
            {
                "item_key": item_key,
                "event_type": "completion",
                "safe_observation": f"{object_label}已完成",
                "decision_question": "legacy question",
                "why_uncertain": "需要本人确认",
                "app_family": "Codex",
                "observed_start": observed.isoformat(timespec="seconds"),
                "observed_end": (observed + timedelta(minutes=1)).isoformat(timespec="seconds"),
                "confidence": 0.6,
                "reason_codes": ["possible_occurrence", "policy_v5_evaluated"],
                "region_kind": "main_content",
                "actionability": "actionable",
                "source_activity_ids": [],
                "deletion_group_ids": [],
                "fingerprint": f"fp_{item_key}",
                "calibration_key": f"cal_{item_key}",
                "group_key": f"grp_{item_key}",
                "group_signature": f"sig_{item_key}",
                "subject": subject,
                "evidence": f"{object_label}已完成",
                "cleaned_content": f"{object_label}已完成",
                "question_kind": "occurrence",
                "candidate_origin": "explicit_claim",
                "priority": "high",
                "episode_id": "",
                "safe_signal_types": ["post_firewall_activity", "independent_file_evidence"],
                "suggested_workstream": "Release Readiness",
                "suggested_outcome": "completed",
                "notification_eligible": True,
                "state": "pending",
            },
        )
        self.conn.commit()

    def _phase2_confirmation_action_payload(
        self,
        question: Mapping[str, Any],
        *,
        idempotency_key: str,
        answer: str,
    ) -> dict[str, Any]:
        return {
            "contract_version": OWNER_CONTEXT_ACTION_VERSION,
            "action": "confirm_card",
            "idempotency_key": idempotency_key,
            "confirmation_action": OWNER_CONTEXT_ACTION_CONFIRMATION,
            "confirmation_ref": str(question["confirmation_ref"]),
            "expected_revision": str(question["revision"]),
            "answer": answer,
            "reason": "other",
        }

    def _ingest_ai(self, **kwargs) -> dict:
        payload = self._receipt_payload(**kwargs)
        return ingest_context_receipt(
            self.conn,
            self._wire(payload),
            verification_secret=self.secrets[kwargs.get("principal_name", "ai")],
            now=self.now,
        )

    def _ingest_controlled(self, *, ingest_now: datetime | None = None, **kwargs) -> dict:
        payload = self._controlled_payload(**kwargs)
        return ingest_controlled_evidence(
            self.conn,
            self._wire(payload),
            verification_secret=self.secrets[kwargs["principal_name"]],
            now=ingest_now or self.now,
        )

    def _ingest_memory_confirmation(
        self,
        *,
        idempotency_key: str,
        source_ref: str,
        subject_ref: str | None = None,
        privacy_class: str = "private_local",
        occurred_at: datetime | None = None,
        ingest_now: datetime | None = None,
        memory_eligible: bool = True,
        verification_scope: str | None = None,
    ) -> dict:
        return self._ingest_controlled(
            principal_name="user",
            evidence_kind="user_confirmation",
            state="completed_confirmed",
            idempotency_key=idempotency_key,
            source_ref=source_ref,
            subject_ref=subject_ref,
            privacy_class=privacy_class,
            occurred_at=occurred_at,
            ingest_now=ingest_now,
            memory_eligible=memory_eligible,
            verification_scope=verification_scope or ("memory" if memory_eligible else "operational"),
        )

    def _ingest_trusted_confirmation(
        self,
        *,
        idempotency_key: str,
        source_ref: str,
        privacy_class: str = "private_local",
        occurred_at: datetime | None = None,
        ingest_now: datetime | None = None,
    ) -> dict:
        return self._ingest_memory_confirmation(
            idempotency_key=idempotency_key,
            source_ref=source_ref,
            privacy_class=privacy_class,
            occurred_at=occurred_at,
            ingest_now=ingest_now,
            memory_eligible=False,
            verification_scope="operational",
        )

    def _ingest_operational_pair(
        self,
        *,
        file_idempotency_key: str,
        git_idempotency_key: str,
        file_source_ref: str,
        git_source_ref: str,
        occurred_at: datetime | None = None,
    ) -> tuple[dict, dict]:
        file_result = self._ingest_controlled(
            principal_name="file",
            evidence_kind="file_saved",
            state="artifact_saved",
            idempotency_key=file_idempotency_key,
            source_ref=file_source_ref,
            occurred_at=occurred_at,
            memory_eligible=False,
        )
        git_result = self._ingest_controlled(
            principal_name="git",
            evidence_kind="git_commit",
            state="artifact_committed",
            idempotency_key=git_idempotency_key,
            source_ref=git_source_ref,
            occurred_at=occurred_at,
            memory_eligible=False,
        )
        return file_result, git_result

    def _table_counts(self) -> dict[str, int]:
        tables = (
            "context_ingest_receipts",
            "context_observations",
            "context_evidence",
            "context_candidates",
            "context_verified",
        )
        return {
            table: int(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }

    def _register_test_principal(
        self,
        name: str,
        *,
        principal_id: str,
        key_id: str,
        principal_kind: str,
        secret: bytes,
        project_ref: str | None = None,
    ) -> None:
        self.secrets[name] = secret
        self.principals[name] = (principal_id, key_id, principal_kind)
        result = register_context_principal(
            self.conn,
            principal_id=principal_id,
            key_id=key_id,
            principal_kind=principal_kind,
            verification_secret=secret,
            project_ref=self.project if project_ref is None else project_ref,
        )
        self.assertEqual(result["status"], "registered")

    def _run_prewrite_revocation_race(
        self,
        *,
        raw_contract: bytes,
        verification_secret: bytes,
        ingest_callable,
        revoke_callable,
    ) -> tuple[dict, dict]:
        ready = threading.Event()
        release = threading.Event()
        calls_lock = threading.Lock()
        call_count = 0
        worker_result: dict[str, dict] = {}
        worker_error: list[BaseException] = []
        original_verify = context_console_module._verify_signature

        def gated_verify(payload, secret) -> None:
            nonlocal call_count
            original_verify(payload, secret)
            with calls_lock:
                call_count += 1
                first = call_count == 1
            if first:
                ready.set()
                if not release.wait(5):
                    raise RuntimeError("prewrite race release timed out")

        def worker() -> None:
            conn = connect(self.db, busy_timeout_seconds=5)
            try:
                worker_result["ingest"] = ingest_callable(
                    conn,
                    raw_contract,
                    verification_secret=verification_secret,
                    now=self.now,
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                worker_error.append(exc)
            finally:
                conn.close()

        with mock.patch.object(context_console_module, "_verify_signature", side_effect=gated_verify):
            thread = threading.Thread(target=worker)
            thread.start()
            self.assertTrue(ready.wait(5), "ingest did not reach pre-write signature boundary")
            revoked = revoke_callable()
            release.set()
            thread.join(10)
            self.assertFalse(thread.is_alive(), "ingest race thread did not finish")
        if worker_error:
            raise worker_error[0]
        return worker_result["ingest"], revoked

    def test_feature_flags_and_existing_writers_remain_fail_closed(self) -> None:
        self.assertFalse(self.default_enabled)
        self.assertEqual(SCHEMA_VERSION, 32)
        self.assertFalse(DEFAULT_HA_AUTO_ENABLED)
        self.assertFalse(CONTROLLED_MEM0_WRITER_ENABLED)
        settings = dict(
            self.conn.execute(
                "SELECT key,value FROM settings WHERE key IN "
                "('context_memory_writer_enabled','context_source_revocation_cascade_ready')"
            ).fetchall()
        )
        self.assertEqual(settings["context_memory_writer_enabled"], "false")
        self.assertEqual(settings["context_source_revocation_cascade_ready"], "false")
        status = context_console_status(self.conn, now=self.now)
        self.assertTrue(status["feature_flag_enabled"])
        self.assertFalse(status["memory_writer_enabled"])
        self.assertFalse(status["source_revocation_cascade_ready"])

        other = ensure_ready(self.root / "disabled.sqlite")
        try:
            self.assertFalse(context_console_enabled(other))
            rejected = configure_context_console(other, enabled=True, confirmation="wrong")
            self.assertEqual((rejected["status"], rejected["reason_code"]), ("rejected", "explicit_enable_confirmation_required"))
            self.assertFalse(context_console_enabled(other))
            ha_status = configure_ha_automation(other, self.root, enabled=True)
            self.assertFalse(ha_status["enabled"])
            self.assertEqual(
                other.execute(
                    "SELECT value FROM settings WHERE key='ha_auto_package_enabled'"
                ).fetchone()[0],
                "false",
            )
        finally:
            other.close()

        self.conn.execute(
            "UPDATE settings SET value='true' WHERE key IN "
            "('context_memory_writer_enabled','context_source_revocation_cascade_ready','ha_auto_package_enabled')"
        )
        self.conn.commit()
        self.conn.close()
        self.conn = ensure_ready(self.db)
        forced = dict(
            self.conn.execute(
                "SELECT key,value FROM settings WHERE key IN "
                "('context_memory_writer_enabled','context_source_revocation_cascade_ready','ha_auto_package_enabled')"
            ).fetchall()
        )
        self.assertEqual(forced["context_memory_writer_enabled"], "false")
        self.assertEqual(forced["context_source_revocation_cascade_ready"], "false")
        self.assertEqual(forced["ha_auto_package_enabled"], "false")
        self.assertTrue(context_console_enabled(self.conn))

    def test_registry_replay_conflict_and_revocation_are_immutable(self) -> None:
        ref = "object_12121212"
        first = register_context_label(
            self.conn,
            kind="object",
            ref=ref,
            label="Immutable Object",
            project_ref=self.project,
        )
        replay = register_context_label(
            self.conn,
            kind="object",
            ref=ref,
            label="Immutable Object",
            project_ref=self.project,
        )
        conflict = register_context_label(
            self.conn,
            kind="object",
            ref=ref,
            label="Relabeled Object",
            project_ref=self.project,
        )
        self.assertEqual(first["status"], "registered")
        self.assertEqual(
            (replay["status"], replay["reason_code"]),
            ("replayed", "idempotent_replay"),
        )
        self.assertEqual((conflict["status"], conflict["reason_code"]), ("conflict", "label_registration_conflict"))
        self.assertEqual(revoke_context_label(self.conn, ref)["status"], "revoked")
        resurrect = register_context_label(
            self.conn,
            kind="object",
            ref=ref,
            label="Immutable Object",
            project_ref=self.project,
        )
        self.assertEqual((resurrect["status"], resurrect["reason_code"]), ("rejected", "label_revoked"))

        principal_id = "principal_12121212"
        key_id = "key_12121212"
        secret = b"immutable-secret-1234"
        registered = register_context_principal(
            self.conn,
            principal_id=principal_id,
            key_id=key_id,
            principal_kind="file_adapter",
            verification_secret=secret,
            project_ref=self.project,
        )
        replayed = register_context_principal(
            self.conn,
            principal_id=principal_id,
            key_id=key_id,
            principal_kind="file_adapter",
            verification_secret=secret,
            project_ref=self.project,
        )
        changed = register_context_principal(
            self.conn,
            principal_id=principal_id,
            key_id=key_id,
            principal_kind="file_adapter",
            verification_secret=b"different-secret-123",
            project_ref=self.project,
        )
        self.assertEqual(registered["status"], "registered")
        self.assertEqual(replayed["status"], "replayed")
        self.assertEqual((changed["status"], changed["reason_code"]), ("conflict", "principal_registration_conflict"))
        self.assertEqual(revoke_context_principal(self.conn, principal_id, key_id)["status"], "revoked")
        resurrected = register_context_principal(
            self.conn,
            principal_id=principal_id,
            key_id=key_id,
            principal_kind="file_adapter",
            verification_secret=secret,
            project_ref=self.project,
        )
        self.assertEqual((resurrected["status"], resurrected["reason_code"]), ("rejected", "key_revoked"))

    def test_context_receipt_idempotent_replay_and_conflict_zero_writes(self) -> None:
        payload = self._receipt_payload()
        accepted = ingest_context_receipt(
            self.conn,
            self._wire(payload),
            verification_secret=self.secrets["ai"],
            now=self.now,
        )
        self.assertEqual((accepted["status"], accepted["result_status"]), ("accepted", "draft_generated"))
        self.assertEqual(accepted["verified_ref"], "")
        before_replay = self._table_counts()
        replayed = ingest_context_receipt(
            self.conn,
            self._wire(payload),
            verification_secret=self.secrets["ai"],
            now=self.now,
        )
        self.assertEqual((replayed["status"], replayed["reason_code"]), ("replayed", "idempotent_replay"))
        self.assertEqual(self._table_counts(), before_replay)

        conflict_payload = copy.deepcopy(payload)
        conflict_payload["lifecycle_event"] = "failed"
        conflict_payload["signature"] = sign_contract_payload(conflict_payload, self.secrets["ai"])
        conflicted = ingest_context_receipt(
            self.conn,
            self._wire(conflict_payload),
            verification_secret=self.secrets["ai"],
            now=self.now,
        )
        self.assertEqual((conflicted["status"], conflicted["reason_code"]), ("conflict", "idempotency_conflict"))
        self.assertEqual(self._table_counts(), before_replay)

    def test_context_receipt_concurrent_replay_materializes_once(self) -> None:
        payload = self._receipt_payload(idempotency_key="idem_00000002")
        wire = self._wire(payload)
        barrier = threading.Barrier(3)
        results: list[dict] = []
        failures: list[BaseException] = []

        def worker() -> None:
            conn = connect(self.db, busy_timeout_seconds=5)
            try:
                barrier.wait(5)
                results.append(
                    ingest_context_receipt(
                        conn,
                        wire,
                        verification_secret=self.secrets["ai"],
                        now=self.now,
                    )
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait(5)
        for thread in threads:
            thread.join(10)
        self.assertEqual(failures, [])
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(sorted(result["status"] for result in results), ["accepted", "replayed"])
        counts = self._table_counts()
        self.assertEqual(counts["context_ingest_receipts"], 1)
        self.assertEqual(counts["context_observations"], 1)
        self.assertEqual(counts["context_evidence"], 1)
        self.assertEqual(counts["context_candidates"], 1)
        self.assertEqual(counts["context_verified"], 0)

    def test_context_receipt_strict_json_and_authentication_fail_closed(self) -> None:
        base = self._receipt_payload(idempotency_key="idem_00000003")

        missing = copy.deepcopy(base)
        del missing["object_ref"]
        result = ingest_context_receipt(self.conn, self._wire(missing), verification_secret=self.secrets["ai"], now=self.now)
        self.assertEqual(result["reason_code"], "missing_required_field")

        unknown = copy.deepcopy(base)
        unknown["assistant_answer"] = "forbidden"
        result = ingest_context_receipt(self.conn, self._wire(unknown), verification_secret=self.secrets["ai"], now=self.now)
        self.assertEqual(result["reason_code"], "unknown_field")

        duplicate = self._wire(base).decode("utf-8")
        duplicate = duplicate[:-1] + ',"project_ref":"project_aaaaaaaa"}'
        result = ingest_context_receipt(self.conn, duplicate.encode(), verification_secret=self.secrets["ai"], now=self.now)
        self.assertEqual(result["reason_code"], "duplicate_json_key")

        confusable = self._wire(base).decode("utf-8").replace('"producer_id"', '"producer_iｄ"', 1)
        result = ingest_context_receipt(self.conn, confusable.encode("utf-8"), verification_secret=self.secrets["ai"], now=self.now)
        self.assertEqual(result["reason_code"], "unicode_confusable_key")

        result = ingest_context_receipt(self.conn, self._wire(base) + b"\x00", verification_secret=self.secrets["ai"], now=self.now)
        self.assertEqual(result["reason_code"], "nul_forbidden")

        result = ingest_context_receipt(self.conn, b"{\xff}", verification_secret=self.secrets["ai"], now=self.now)
        self.assertEqual(result["reason_code"], "invalid_utf8")

        nan_wire = self._wire(base).decode("utf-8").replace('"evidence_refs":[]', '"evidence_refs":NaN')
        result = ingest_context_receipt(self.conn, nan_wire.encode(), verification_secret=self.secrets["ai"], now=self.now)
        self.assertEqual(result["reason_code"], "non_finite_number_forbidden")

        infinity_wire = self._wire(base).decode("utf-8").replace('"evidence_refs":[]', '"evidence_refs":Infinity')
        result = ingest_context_receipt(self.conn, infinity_wire.encode(), verification_secret=self.secrets["ai"], now=self.now)
        self.assertEqual(result["reason_code"], "non_finite_number_forbidden")

        deep = self._wire(base).decode("utf-8").replace('"evidence_refs":[]', '"evidence_refs":[[[[[[[["evidence_11111111"]]]]]]]]')
        result = ingest_context_receipt(self.conn, deep.encode(), verification_secret=self.secrets["ai"], now=self.now)
        self.assertEqual(result["reason_code"], "json_too_deep")

        oversized = self._wire(base) + b" " * (MAX_CONTRACT_BYTES + 1)
        result = ingest_context_receipt(self.conn, oversized, verification_secret=self.secrets["ai"], now=self.now)
        self.assertEqual(result["reason_code"], "contract_too_large")

        bad_signature = copy.deepcopy(base)
        bad_signature["signature"] = "hmac-sha256:" + "0" * 64
        result = ingest_context_receipt(self.conn, self._wire(bad_signature), verification_secret=self.secrets["ai"], now=self.now)
        self.assertEqual(result["reason_code"], "signature_invalid")

        result = ingest_context_receipt(self.conn, self._wire(base), verification_secret=b"wrong-secret-012345", now=self.now)
        self.assertEqual(result["reason_code"], "authentication_key_mismatch")
        self.assertEqual(self._table_counts(), {
            "context_ingest_receipts": 0,
            "context_observations": 0,
            "context_evidence": 0,
            "context_candidates": 0,
            "context_verified": 0,
        })

    def test_context_receipt_registry_time_size_and_prohibited_content_fail_closed(self) -> None:
        unregistered = self._receipt_payload(
            idempotency_key="idem_00000004",
            project_ref="project_abcdabcd",
        )
        result = ingest_context_receipt(self.conn, self._wire(unregistered), verification_secret=self.secrets["ai"], now=self.now)
        self.assertEqual(result["reason_code"], "project_unregistered")

        old = self._receipt_payload(
            idempotency_key="idem_00000005",
            occurred_at=self.now - timedelta(hours=25),
        )
        result = ingest_context_receipt(self.conn, self._wire(old), verification_secret=self.secrets["ai"], now=self.now)
        self.assertEqual(result["reason_code"], "receipt_too_old")

        future = self._receipt_payload(
            idempotency_key="idem_00000006",
            occurred_at=self.now + timedelta(minutes=6),
        )
        result = ingest_context_receipt(self.conn, self._wire(future), verification_secret=self.secrets["ai"], now=self.now)
        self.assertEqual(result["reason_code"], "receipt_from_future")

        too_many = self._receipt_payload(
            idempotency_key="idem_00000007",
            evidence_refs=[f"evidence_{index:08d}" for index in range(17)],
        )
        result = ingest_context_receipt(self.conn, self._wire(too_many), verification_secret=self.secrets["ai"], now=self.now)
        self.assertEqual(result["reason_code"], "too_many_evidence_refs")

        path_body = self._receipt_payload(idempotency_key="idem_00000008")
        path_body["source_ref"] = "https://example.invalid/private"
        path_body["signature"] = sign_contract_payload(path_body, self.secrets["ai"])
        result = ingest_context_receipt(self.conn, self._wire(path_body), verification_secret=self.secrets["ai"], now=self.now)
        self.assertEqual(result["reason_code"], "path_or_url_content_forbidden")

        unix_path_body = self._receipt_payload(idempotency_key="idem_00000013")
        unix_path_body["source_ref"] = "/tmp/private-context"
        unix_path_body["signature"] = sign_contract_payload(unix_path_body, self.secrets["ai"])
        result = ingest_context_receipt(
            self.conn,
            self._wire(unix_path_body),
            verification_secret=self.secrets["ai"],
            now=self.now,
        )
        self.assertEqual(result["reason_code"], "path_or_url_content_forbidden")

        semantic_ref = self._receipt_payload(idempotency_key="idem_00000014")
        semantic_ref["source_ref"] = "source_prompt2026"
        semantic_ref["signature"] = sign_contract_payload(semantic_ref, self.secrets["ai"])
        result = ingest_context_receipt(
            self.conn,
            self._wire(semantic_ref),
            verification_secret=self.secrets["ai"],
            now=self.now,
        )
        self.assertEqual(result["reason_code"], "invalid_source_ref")

        secret_body = self._receipt_payload(idempotency_key="idem_00000009")
        secret_body["source_ref"] = "token=not-allowed-in-contract"
        secret_body["signature"] = sign_contract_payload(secret_body, self.secrets["ai"])
        result = ingest_context_receipt(self.conn, self._wire(secret_body), verification_secret=self.secrets["ai"], now=self.now)
        self.assertEqual(result["reason_code"], "secret_like_content_forbidden")

        for forbidden_field in (
            "prompt",
            "chat_body",
            "assistant_answer",
            "transcript",
            "tool_arguments",
            "tool_output",
            "command",
            "diff",
            "screenshot",
            "ocr_text",
            "absolute_path",
        ):
            body = self._receipt_payload(idempotency_key="idem_12345678")
            body[forbidden_field] = "x"
            result = ingest_context_receipt(self.conn, self._wire(body), verification_secret=self.secrets["ai"], now=self.now)
            self.assertEqual(result["reason_code"], "unknown_field", forbidden_field)

        free_text_subject = self._receipt_payload(idempotency_key="idem_00000010")
        free_text_subject["subject_ref"] = "the user said this in chat"
        free_text_subject["signature"] = sign_contract_payload(free_text_subject, self.secrets["ai"])
        result = ingest_context_receipt(self.conn, self._wire(free_text_subject), verification_secret=self.secrets["ai"], now=self.now)
        self.assertEqual(result["reason_code"], "invalid_subject_ref")

    def test_revoked_key_and_ai_state_cap_fail_closed(self) -> None:
        principal_id, key_id, _ = self.principals["ai"]
        revoked = revoke_context_principal(self.conn, principal_id, key_id)
        self.assertEqual(revoked["status"], "revoked")
        payload = self._receipt_payload(idempotency_key="idem_00000011")
        result = ingest_context_receipt(self.conn, self._wire(payload), verification_secret=self.secrets["ai"], now=self.now)
        self.assertEqual(result["reason_code"], "key_revoked")

        # A fresh fixture still rejects completion/saved/sent lifecycle claims at the enum boundary.
        register_context_principal(
            self.conn,
            principal_id=principal_id,
            key_id=key_id,
            principal_kind="ai_lifecycle",
            verification_secret=self.secrets["ai"],
            project_ref=self.project,
        )
        for forbidden in ("saved", "sent", "completed", "artifact_saved", "completed_confirmed"):
            payload = self._receipt_payload(idempotency_key="idem_00000012")
            payload["lifecycle_event"] = forbidden
            payload["signature"] = sign_contract_payload(payload, self.secrets["ai"])
            result = ingest_context_receipt(self.conn, self._wire(payload), verification_secret=self.secrets["ai"], now=self.now)
            self.assertEqual(result["reason_code"], "invalid_lifecycle_event", forbidden)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM context_verified").fetchone()[0], 0)

    def test_controlled_adapters_enforce_principal_and_state_caps(self) -> None:
        file_ok = self._ingest_controlled(
            principal_name="file",
            evidence_kind="file_saved",
            state="artifact_saved",
            idempotency_key="idem_10000001",
        )
        self.assertEqual((file_ok["status"], file_ok["result_status"]), ("accepted", "artifact_saved"))

        file_overclaim = self._controlled_payload(
            principal_name="file",
            evidence_kind="file_saved",
            state="completed_confirmed",
            idempotency_key="idem_10000002",
        )
        result = ingest_controlled_evidence(self.conn, self._wire(file_overclaim), verification_secret=self.secrets["file"], now=self.now)
        self.assertEqual(result["reason_code"], "evidence_state_exceeds_adapter_cap")

        git_ok = self._ingest_controlled(
            principal_name="git",
            evidence_kind="git_commit",
            state="artifact_committed",
            idempotency_key="idem_10000003",
            source_ref="source_33333333",
        )
        self.assertEqual((git_ok["status"], git_ok["result_status"]), ("accepted", "artifact_committed"))

        external_without_receipt = self._controlled_payload(
            principal_name="external",
            evidence_kind="external_receipt",
            state="external_delivered",
            idempotency_key="idem_10000004",
        )
        result = ingest_controlled_evidence(
            self.conn,
            self._wire(external_without_receipt),
            verification_secret=self.secrets["external"],
            now=self.now,
        )
        self.assertEqual(result["reason_code"], "invalid_provider_receipt_ref")

        external_ok = self._ingest_controlled(
            principal_name="external",
            evidence_kind="external_receipt",
            state="completed_confirmed",
            idempotency_key="idem_10000005",
            provider_receipt_ref="provider_44444444",
            source_ref="source_44444444",
        )
        self.assertEqual((external_ok["status"], external_ok["result_status"]), ("accepted", "completed_confirmed"))

        wrong_principal = self._controlled_payload(
            principal_name="ai",
            evidence_kind="file_saved",
            state="artifact_saved",
            idempotency_key="idem_10000006",
        )
        result = ingest_controlled_evidence(self.conn, self._wire(wrong_principal), verification_secret=self.secrets["ai"], now=self.now)
        self.assertEqual(result["reason_code"], "principal_kind_not_authorized")

        file_with_provider = self._controlled_payload(
            principal_name="file",
            evidence_kind="file_saved",
            state="artifact_saved",
            idempotency_key="idem_10000007",
            provider_receipt_ref="provider_55555555",
        )
        result = ingest_controlled_evidence(self.conn, self._wire(file_with_provider), verification_secret=self.secrets["file"], now=self.now)
        self.assertEqual(result["reason_code"], "provider_receipt_not_allowed")

    def test_screen_and_ocr_are_clues_only_and_never_verified(self) -> None:
        for index, source_kind in enumerate(("screen", "ocr"), start=1):
            result = record_unverified_clue(
                self.conn,
                source_kind=source_kind,
                source_ref=f"source_9000000{index}",
                occurred_at=self.now.isoformat(timespec="seconds"),
                project_ref=self.project,
                workstream_ref=self.workstream,
                object_ref=self.object,
                version_ref=self.version,
                subject_ref=self.owner,
                privacy_class="private_local",
                now=self.now,
            )
            self.assertEqual(result["status"], "accepted")
            self.assertFalse(result["verified_context_created"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM context_verified").fetchone()[0], 0)
        bundle = build_context_bundle(self.conn, now=self.now)
        self.assertEqual(bundle["verified_contexts"], [])
        self.assertEqual(len(bundle["unverified_clues"]), 2)
        self.assertTrue(all(item["verification_state"] == "unverified" for item in bundle["unverified_clues"]))
        self.assertIsNone(
            _highlight(
                {
                    "occurrence_state": "evidence_confirmed",
                    "confirmation_source": "screen_observation",
                    "activity_summary": "synthetic",
                }
            )
        )
        self.assertIsNone(
            _highlight(
                {
                    "occurrence_state": "evidence_confirmed",
                    "confirmation_source": "deterministic_evidence",
                    "activity_summary": "synthetic",
                }
            )
        )

    def test_bundle_partition_filter_search_and_public_projection(self) -> None:
        accepted = self._ingest_trusted_confirmation(
            idempotency_key="idem_20000001", source_ref="source_20000001"
        )
        insufficient = self._ingest_ai(
            idempotency_key="idem_20000002",
            lifecycle_event="status_unknown",
            source_ref="source_55555555",
        )
        self.assertEqual(insufficient["result_status"], "information_insufficient")
        record_unverified_clue(
            self.conn,
            source_kind="screen",
            source_ref="source_66666666",
            occurred_at=self.now.isoformat(timespec="seconds"),
            project_ref=self.project,
            workstream_ref=self.workstream,
            object_ref=self.object,
            version_ref=self.version,
            subject_ref=self.owner,
            privacy_class="private_local",
            now=self.now,
        )
        bundle = build_context_bundle(
            self.conn,
            project_ref=self.project,
            start_at=(self.now - timedelta(minutes=1)).isoformat(timespec="seconds"),
            end_at=(self.now + timedelta(minutes=1)).isoformat(timespec="seconds"),
            query="Context Contract",
            include_clues=True,
            now=self.now,
        )
        self.assertEqual((bundle["contract_version"], bundle["status"]), (CONTEXT_BUNDLE_VERSION, "available"))
        self.assertFalse(bundle["raw_access"])
        self.assertEqual(len(bundle["verified_contexts"]), 1)
        self.assertTrue(bundle["verified_contexts"][0]["context_ref"].startswith("context_"))
        self.assertNotEqual(bundle["verified_contexts"][0]["context_ref"], accepted["verified_ref"])
        self.assertEqual(len(bundle["unverified_clues"]), 2)
        encoded = json.dumps(bundle, ensure_ascii=False)
        for forbidden in (
            "candidate_ref",
            "observation_ref",
            "evidence_ref",
            "receipt_ref",
            "provenance_ref",
            "absolute_path",
            "diagnostic_json",
        ):
            self.assertNotIn(forbidden, encoded)
        self.assertIn("Project Alpha", encoded)
        self.assertIn("Context Contract", encoded)

        no_match = build_context_bundle(self.conn, query="No Such Approved Label", now=self.now)
        self.assertEqual(no_match["verified_contexts"], [])
        other_project = build_context_bundle(self.conn, project_ref=self.other_project, now=self.now)
        self.assertEqual(other_project["verified_contexts"], [])

    def test_ignored_rejected_expired_and_revoked_candidates_do_not_escape(self) -> None:
        dispositions = ("ignored", "rejected")
        refs: list[tuple[str, str]] = []
        for index, disposition in enumerate(dispositions, start=1):
            accepted = self._ingest_trusted_confirmation(
                idempotency_key=f"idem_2100000{index}",
                source_ref=f"source_2100000{index}",
            )
            self.conn.execute(
                "UPDATE context_candidates SET disposition=? WHERE candidate_ref=?",
                (disposition, accepted["candidate_ref"]),
            )
            self.conn.commit()
            refs.append((accepted["verified_ref"], self.owner))
        expired = self._ingest_trusted_confirmation(
            idempotency_key="idem_21000003", source_ref="source_21000003"
        )
        self.conn.execute(
            "UPDATE context_verified SET expires_at=? WHERE verified_ref=?",
            ((self.now - timedelta(seconds=1)).isoformat(timespec="seconds"), expired["verified_ref"]),
        )
        self.conn.commit()
        revoked = self._ingest_trusted_confirmation(
            idempotency_key="idem_21000004", source_ref="source_21000004"
        )
        self.conn.execute(
            "UPDATE context_verified SET status='revoked',revoked_at=? WHERE verified_ref=?",
            (self.now.isoformat(timespec="seconds"), revoked["verified_ref"]),
        )
        self.conn.commit()

        bundle = build_context_bundle(self.conn, now=self.now)
        self.assertEqual(bundle["verified_contexts"], [])
        for verified_ref, subject_ref in refs:
            memory = create_memory_candidate(self.conn, verified_ref=verified_ref, subject_ref=subject_ref, now=self.now)
            self.assertEqual(memory["status"], "rejected")
        self.assertEqual(
            create_memory_candidate(self.conn, verified_ref=expired["verified_ref"], subject_ref=self.owner, now=self.now)["reason_code"],
            "verified_context_expired",
        )
        self.assertEqual(
            create_memory_candidate(self.conn, verified_ref=revoked["verified_ref"], subject_ref=self.owner, now=self.now)["reason_code"],
            "verified_context_revoked",
        )

    def test_memory_candidate_requires_owner_active_provenance_and_never_writes_mem0(self) -> None:
        accepted = self._ingest_memory_confirmation(
            idempotency_key="idem_22000001",
            source_ref="source_22000001",
        )
        candidate = create_memory_candidate(
            self.conn,
            verified_ref=accepted["verified_ref"],
            subject_ref=self.owner,
            now=self.now,
        )
        self.assertEqual(candidate["status"], "candidate_only")
        self.assertFalse(candidate["memory_write_actions"])
        self.assertFalse(candidate["writer_enabled"])
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_memory_candidates WHERE memory_candidate_ref=?",
                (candidate["memory_candidate_ref"],),
            ).fetchone()[0],
            "candidate_only",
        )

        self.assertEqual(
            create_memory_candidate(
                self.conn,
                verified_ref=accepted["verified_ref"],
                subject_ref=self.third_party,
                now=self.now,
            )["reason_code"],
            "third_party_subject_forbidden",
        )
        wrong_subject = self._ingest_controlled(
            principal_name="user",
            evidence_kind="user_confirmation",
            state="completed_confirmed",
            idempotency_key="idem_22000002",
            subject_ref=self.third_party,
            source_ref="source_77777777",
        )
        self.assertEqual(
            create_memory_candidate(
                self.conn,
                verified_ref=wrong_subject["verified_ref"],
                subject_ref=self.owner,
                now=self.now,
            )["reason_code"],
            "subject_mismatch",
        )
        self.conn.execute(
            "UPDATE context_verified SET provenance_ref='' WHERE verified_ref=?",
            (accepted["verified_ref"],),
        )
        self.conn.commit()
        self.assertEqual(
            create_memory_candidate(
                self.conn,
                verified_ref=accepted["verified_ref"],
                subject_ref=self.owner,
                now=self.now,
            )["reason_code"],
            "provenance_missing",
        )

    def test_memory_replay_and_revoked_label_never_escape_trusted_outputs(self) -> None:
        accepted = self._ingest_memory_confirmation(
            idempotency_key="idem_22300001",
            source_ref="source_22300001",
        )
        first = create_memory_candidate(
            self.conn,
            verified_ref=accepted["verified_ref"],
            subject_ref=self.owner,
            now=self.now,
        )
        replay = create_memory_candidate(
            self.conn,
            verified_ref=accepted["verified_ref"],
            subject_ref=self.owner,
            now=self.now,
        )
        self.assertEqual(first["status"], "candidate_only")
        self.assertEqual((replay["status"], replay["candidate_status"]), ("replayed", "candidate_only"))

        self.assertEqual(revoke_context_label(self.conn, self.object)["status"], "revoked")
        bundle = build_context_bundle(self.conn, project_ref=self.project, now=self.now)
        self.assertEqual(bundle["verified_contexts"], [])
        self.assertEqual(context_console_status(self.conn, now=self.now)["counts"]["active_verified_contexts"], 0)
        self.assertEqual(context_console_status(self.conn, now=self.now)["counts"]["memory_candidates"], 0)
        rejected = create_memory_candidate(
            self.conn,
            verified_ref=accepted["verified_ref"],
            subject_ref=self.owner,
            now=self.now,
        )
        self.assertEqual((rejected["status"], rejected["reason_code"]), ("rejected", "verified_context_revoked"))

    def test_revoked_memory_candidate_cannot_be_silently_reactivated(self) -> None:
        accepted = self._ingest_memory_confirmation(
            idempotency_key="idem_22400001",
            source_ref="source_22400001",
        )
        first = create_memory_candidate(
            self.conn,
            verified_ref=accepted["verified_ref"],
            subject_ref=self.owner,
            now=self.now,
        )
        self.conn.execute(
            "UPDATE context_memory_candidates SET status='revoked',revoked_at=?,"
            "revocation_reason_code='synthetic_revocation' WHERE memory_candidate_ref=?",
            (self.now.isoformat(timespec="seconds"), first["memory_candidate_ref"]),
        )
        self.conn.commit()
        replay = create_memory_candidate(
            self.conn,
            verified_ref=accepted["verified_ref"],
            subject_ref=self.owner,
            now=self.now,
        )
        self.assertEqual((replay["status"], replay["reason_code"]), ("rejected", "memory_candidate_revoked"))

    def test_bundle_and_memory_fail_closed_when_any_linked_evidence_is_inactive(self) -> None:
        controlled = self._ingest_memory_confirmation(
            idempotency_key="idem_22500001",
            source_ref="source_22500001",
        )
        self.conn.execute(
            "UPDATE context_evidence SET status='revoked',revoked_at=?,"
            "revocation_reason_code='synthetic_integrity_check' WHERE evidence_ref=?",
            (self.now.isoformat(timespec="seconds"), controlled["evidence_ref"]),
        )
        self.conn.commit()

        bundle = build_context_bundle(self.conn, now=self.now)
        self.assertEqual(bundle["verified_contexts"], [])
        memory = create_memory_candidate(
            self.conn,
            verified_ref=controlled["verified_ref"],
            subject_ref=self.owner,
            now=self.now,
        )
        self.assertEqual((memory["status"], memory["reason_code"]), ("rejected", "provenance_revoked"))

    def _prepare_one_delivery(self, *, target_ref: str | None = None, idempotency_key: str = "idem_30000001") -> dict:
        self._ingest_trusted_confirmation(
            idempotency_key=idempotency_key,
            source_ref="source_" + idempotency_key.removeprefix("idem_"),
        )
        prepared = prepare_delivery(
            self.conn,
            target_ref=target_ref or self.target,
            project_ref=self.project,
            now=self.now,
        )
        self.assertEqual(prepared["status"], "prepared")
        return prepared

    def test_delivery_receipt_state_machine_hash_ack_and_independent_provider(self) -> None:
        prepared = self._prepare_one_delivery()
        delivery_ref = prepared["delivery_ref"]
        bundle_hash = prepared["bundle_hash"]
        prepared_receipt = self.conn.execute(
            "SELECT result_state FROM context_delivery_receipts WHERE delivery_ref=? AND transition='prepared'",
            (delivery_ref,),
        ).fetchone()
        self.assertEqual(prepared_receipt[0], "prepared")

        direct_delivered = self._delivery_payload(
            principal_name="provider",
            delivery_ref=delivery_ref,
            bundle_hash=bundle_hash,
            transition="delivered",
            idempotency_key="idem_30000002",
            provider_receipt_ref="provider_30000002",
        )
        result = ingest_delivery_receipt(self.conn, self._wire(direct_delivered), verification_secret=self.secrets["provider"], now=self.now)
        self.assertEqual(result["reason_code"], "illegal_delivery_transition")

        regressed_time = self._delivery_payload(
            principal_name="consumer",
            delivery_ref=delivery_ref,
            bundle_hash=bundle_hash,
            transition="consumed",
            idempotency_key="idem_30000007",
            provider_receipt_ref="provider_30000007",
            occurred_at=self.now - timedelta(seconds=1),
        )
        result = ingest_delivery_receipt(
            self.conn,
            self._wire(regressed_time),
            verification_secret=self.secrets["consumer"],
            now=self.now,
        )
        self.assertEqual(result["reason_code"], "delivery_timestamp_regression")

        wrong_hash = self._delivery_payload(
            principal_name="consumer",
            delivery_ref=delivery_ref,
            bundle_hash="sha256:" + "0" * 64,
            transition="consumed",
            idempotency_key="idem_30000003",
            provider_receipt_ref="provider_30000003",
        )
        result = ingest_delivery_receipt(self.conn, self._wire(wrong_hash), verification_secret=self.secrets["consumer"], now=self.now)
        self.assertEqual(result["reason_code"], "bundle_hash_mismatch")

        consumed_payload = self._delivery_payload(
            principal_name="consumer",
            delivery_ref=delivery_ref,
            bundle_hash=bundle_hash,
            transition="consumed",
            idempotency_key="idem_30000004",
            provider_receipt_ref="provider_30000004",
        )
        consumed = ingest_delivery_receipt(self.conn, self._wire(consumed_payload), verification_secret=self.secrets["consumer"], now=self.now)
        self.assertEqual((consumed["status"], consumed["state"]), ("accepted", "consumed"))
        replay = ingest_delivery_receipt(self.conn, self._wire(consumed_payload), verification_secret=self.secrets["consumer"], now=self.now)
        self.assertEqual((replay["status"], replay["state"]), ("replayed", "consumed"))

        same_receipt = self._delivery_payload(
            principal_name="provider",
            delivery_ref=delivery_ref,
            bundle_hash=bundle_hash,
            transition="delivered",
            idempotency_key="idem_30000005",
            provider_receipt_ref="provider_30000004",
        )
        result = ingest_delivery_receipt(self.conn, self._wire(same_receipt), verification_secret=self.secrets["provider"], now=self.now)
        self.assertEqual(result["reason_code"], "independent_provider_receipt_required")

        delivered_payload = self._delivery_payload(
            principal_name="provider",
            delivery_ref=delivery_ref,
            bundle_hash=bundle_hash,
            transition="delivered",
            idempotency_key="idem_30000006",
            provider_receipt_ref="provider_30000006",
        )
        delivered = ingest_delivery_receipt(self.conn, self._wire(delivered_payload), verification_secret=self.secrets["provider"], now=self.now)
        self.assertEqual((delivered["status"], delivered["state"]), ("accepted", "delivered"))
        row = self.conn.execute("SELECT state FROM context_deliveries WHERE delivery_ref=?", (delivery_ref,)).fetchone()
        self.assertEqual(row[0], "delivered")

        for internal_alias in ("claimed", "acked"):
            alias = copy.deepcopy(delivered_payload)
            alias["idempotency_key"] = "idem_39999999"
            alias["transition"] = internal_alias
            alias["signature"] = sign_contract_payload(alias, self.secrets["provider"])
            result = ingest_delivery_receipt(self.conn, self._wire(alias), verification_secret=self.secrets["provider"], now=self.now)
            self.assertEqual(result["reason_code"], "invalid_delivery_transition", internal_alias)

    def test_delivery_exact_replay_remains_idempotent_after_freshness_window_when_dependencies_live(self) -> None:
        prepared = self._prepare_one_delivery(idempotency_key="idem_30000101")
        stale_payload = self._delivery_payload(
            principal_name="consumer",
            delivery_ref=prepared["delivery_ref"],
            bundle_hash=prepared["bundle_hash"],
            transition="consumed",
            idempotency_key="idem_30000100",
            provider_receipt_ref="provider_30000100",
            occurred_at=self.now - timedelta(hours=1),
        )
        stale = ingest_delivery_receipt(
            self.conn,
            self._wire(stale_payload),
            verification_secret=self.secrets["consumer"],
            now=self.now,
        )
        self.assertEqual(
            (stale["status"], stale["reason_code"]),
            ("rejected", "receipt_too_old"),
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT state FROM context_deliveries WHERE delivery_ref=?",
                (prepared["delivery_ref"],),
            ).fetchone()[0],
            "prepared",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM context_delivery_receipts "
                "WHERE delivery_ref=? AND principal_id=? AND idempotency_key=?",
                (
                    prepared["delivery_ref"],
                    self.principals["consumer"][0],
                    stale_payload["idempotency_key"],
                ),
            ).fetchone()[0],
            0,
        )

        payload = self._delivery_payload(
            principal_name="consumer",
            delivery_ref=prepared["delivery_ref"],
            bundle_hash=prepared["bundle_hash"],
            transition="consumed",
            idempotency_key="idem_30000102",
            provider_receipt_ref="provider_30000102",
        )
        accepted = ingest_delivery_receipt(
            self.conn,
            self._wire(payload),
            verification_secret=self.secrets["consumer"],
            now=self.now,
        )
        self.assertEqual((accepted["status"], accepted["state"]), ("accepted", "consumed"))

        replay = ingest_delivery_receipt(
            self.conn,
            self._wire(payload),
            verification_secret=self.secrets["consumer"],
            now=self.now + timedelta(hours=1),
        )
        self.assertEqual(
            (replay["status"], replay["reason_code"], replay["state"]),
            ("replayed", "idempotent_replay", "consumed"),
        )

    def test_delivery_failure_transition_and_idempotency_conflict(self) -> None:
        prepared = self._prepare_one_delivery(target_ref=self.other_target, idempotency_key="idem_31000001")
        failure = self._delivery_payload(
            principal_name="consumer_other",
            delivery_ref=prepared["delivery_ref"],
            bundle_hash=prepared["bundle_hash"],
            transition="failed",
            idempotency_key="idem_31000002",
            failure_reason_code="consumer_rejected",
        )
        failed = ingest_delivery_receipt(self.conn, self._wire(failure), verification_secret=self.secrets["consumer_other"], now=self.now)
        self.assertEqual((failed["status"], failed["state"]), ("accepted", "failed"))
        conflict = copy.deepcopy(failure)
        conflict["failure_reason_code"] = "unknown_failure"
        conflict["signature"] = sign_contract_payload(conflict, self.secrets["consumer_other"])
        result = ingest_delivery_receipt(self.conn, self._wire(conflict), verification_secret=self.secrets["consumer_other"], now=self.now)
        self.assertEqual((result["status"], result["reason_code"]), ("conflict", "idempotency_conflict"))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM context_delivery_receipts WHERE delivery_ref=?", (prepared["delivery_ref"],)).fetchone()[0],
            2,
        )

    def test_delivery_manifest_is_reusable_but_project_and_live_bundle_are_enforced(self) -> None:
        accepted = self._ingest_trusted_confirmation(
            idempotency_key="idem_31500001", source_ref="source_31500001"
        )
        first = prepare_delivery(
            self.conn,
            target_ref=self.target,
            project_ref=self.project,
            now=self.now,
        )
        second = prepare_delivery(
            self.conn,
            target_ref=self.other_target,
            project_ref=self.project,
            now=self.now,
        )
        self.assertEqual((first["status"], second["status"]), ("prepared", "prepared"))
        self.assertEqual(first["bundle_ref"], second["bundle_ref"])
        self.assertNotEqual(first["delivery_ref"], second["delivery_ref"])
        self.assertEqual(
            self.conn.execute("SELECT target_ref FROM context_bundle_manifests WHERE bundle_ref=?", (first["bundle_ref"],)).fetchone()[0],
            "",
        )

        other_secret = b"other-project-consumer"
        other_name = "other_consumer"
        self.secrets[other_name] = other_secret
        self.principals[other_name] = ("principal_31313131", "key_31313131", "bundle_consumer")
        register_context_principal(
            self.conn,
            principal_id=self.principals[other_name][0],
            key_id=self.principals[other_name][1],
            principal_kind="bundle_consumer",
            verification_secret=other_secret,
            project_ref=self.other_project,
            target_ref=self.other_project_target,
            target_scope="local",
        )
        wrong_project = self._delivery_payload(
            principal_name=other_name,
            delivery_ref=first["delivery_ref"],
            bundle_hash=first["bundle_hash"],
            transition="consumed",
            idempotency_key="idem_31500002",
            provider_receipt_ref="provider_31500002",
        )
        rejected = ingest_delivery_receipt(
            self.conn,
            self._wire(wrong_project),
            verification_secret=other_secret,
            now=self.now,
        )
        self.assertEqual((rejected["status"], rejected["reason_code"]), ("rejected", "principal_project_mismatch"))

        self.conn.execute(
            "UPDATE context_evidence SET status='revoked',revoked_at=?,"
            "revocation_reason_code='synthetic_revocation' WHERE evidence_ref=?",
            (self.now.isoformat(timespec="seconds"), accepted["evidence_ref"]),
        )
        self.conn.commit()
        consumed = self._delivery_payload(
            principal_name="consumer",
            delivery_ref=first["delivery_ref"],
            bundle_hash=first["bundle_hash"],
            transition="consumed",
            idempotency_key="idem_31500003",
            provider_receipt_ref="provider_31500003",
        )
        invalid = ingest_delivery_receipt(
            self.conn,
            self._wire(consumed),
            verification_secret=self.secrets["consumer"],
            now=self.now,
        )
        self.assertEqual((invalid["status"], invalid["reason_code"]), ("rejected", "bundle_source_invalid"))
        self.assertEqual(
            self.conn.execute("SELECT state FROM context_deliveries WHERE delivery_ref=?", (first["delivery_ref"],)).fetchone()[0],
            "prepared",
        )

    def test_delivery_provider_must_be_independent_principal_not_only_a_new_receipt(self) -> None:
        prepared = self._prepare_one_delivery(idempotency_key="idem_31600001")
        consumed_payload = self._delivery_payload(
            principal_name="consumer",
            delivery_ref=prepared["delivery_ref"],
            bundle_hash=prepared["bundle_hash"],
            transition="consumed",
            idempotency_key="idem_31600002",
            provider_receipt_ref="provider_31600002",
        )
        self.assertEqual(
            ingest_delivery_receipt(
                self.conn,
                self._wire(consumed_payload),
                verification_secret=self.secrets["consumer"],
                now=self.now,
            )["status"],
            "accepted",
        )

        same_principal_name = "same_principal_provider"
        self.secrets[same_principal_name] = b"same-principal-provider"
        self.principals[same_principal_name] = (
            self.principals["consumer"][0],
            "key_32323232",
            "delivery_provider",
        )
        register_context_principal(
            self.conn,
            principal_id=self.principals[same_principal_name][0],
            key_id=self.principals[same_principal_name][1],
            principal_kind="delivery_provider",
            verification_secret=self.secrets[same_principal_name],
            project_ref=self.project,
            target_ref=self.target,
            target_scope="local",
        )
        delivered_payload = self._delivery_payload(
            principal_name=same_principal_name,
            delivery_ref=prepared["delivery_ref"],
            bundle_hash=prepared["bundle_hash"],
            transition="delivered",
            idempotency_key="idem_31600003",
            provider_receipt_ref="provider_31600003",
        )
        rejected = ingest_delivery_receipt(
            self.conn,
            self._wire(delivered_payload),
            verification_secret=self.secrets[same_principal_name],
            now=self.now,
        )
        self.assertEqual(
            (rejected["status"], rejected["reason_code"]),
            ("rejected", "independent_delivery_provider_required"),
        )

    def test_observation_delete_cascades_and_delivered_claim_is_preserved(self) -> None:
        accepted = self._ingest_memory_confirmation(
            idempotency_key="idem_32000001",
            source_ref="source_32000001",
        )
        memory = create_memory_candidate(self.conn, verified_ref=accepted["verified_ref"], subject_ref=self.owner, now=self.now)
        self.assertEqual(memory["status"], "candidate_only")
        prepared = prepare_delivery(self.conn, target_ref=self.target, project_ref=self.project, now=self.now)
        consumed_payload = self._delivery_payload(
            principal_name="consumer",
            delivery_ref=prepared["delivery_ref"],
            bundle_hash=prepared["bundle_hash"],
            transition="consumed",
            idempotency_key="idem_32000002",
            provider_receipt_ref="provider_32000002",
        )
        ingest_delivery_receipt(self.conn, self._wire(consumed_payload), verification_secret=self.secrets["consumer"], now=self.now)
        delivered_payload = self._delivery_payload(
            principal_name="provider",
            delivery_ref=prepared["delivery_ref"],
            bundle_hash=prepared["bundle_hash"],
            transition="delivered",
            idempotency_key="idem_32000003",
            provider_receipt_ref="provider_32000003",
        )
        ingest_delivery_receipt(self.conn, self._wire(delivered_payload), verification_secret=self.secrets["provider"], now=self.now)

        deleted = delete_context_observation(
            self.conn,
            observation_ref=accepted["observation_ref"],
            reason_code="user_deleted",
            now=self.now,
        )
        self.assertEqual(deleted["status"], "completed")
        self.assertGreaterEqual(deleted["cascade"]["evidence_revoked"], 1)
        self.assertEqual(deleted["cascade"]["verified_contexts_revoked"], 1)
        self.assertEqual(deleted["cascade"]["memory_candidates_revoked"], 1)
        self.assertEqual(deleted["cascade"]["bundle_members_revoked"], 1)
        self.assertEqual(deleted["cascade"]["withdrawal_receipts_created"], 1)
        self.assertEqual(
            self.conn.execute("SELECT state FROM context_deliveries WHERE delivery_ref=?", (prepared["delivery_ref"],)).fetchone()[0],
            "delivered",
        )
        withdrawal = self.conn.execute(
            "SELECT status,reason_code FROM context_withdrawal_receipts WHERE delivery_ref=?",
            (prepared["delivery_ref"],),
        ).fetchone()
        self.assertEqual(tuple(withdrawal), ("local_restriction_recorded", "user_deleted"))
        self.assertEqual(build_context_bundle(self.conn, now=self.now)["verified_contexts"], [])
        repeated = delete_context_observation(self.conn, observation_ref=accepted["observation_ref"], now=self.now)
        self.assertEqual(repeated["status"], "already_revoked")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM context_withdrawal_receipts").fetchone()[0],
            1,
        )

    def test_public_range_revoke_commits_across_connections_with_core_gate_off(self) -> None:
        accepted = record_unverified_clue(
            self.conn,
            source_kind="screen",
            source_ref="source_32010001",
            occurred_at=self.now.isoformat(timespec="seconds"),
            project_ref=self.project,
            workstream_ref=self.workstream,
            object_ref=self.object,
            version_ref=self.version,
            subject_ref=self.owner,
            privacy_class="private_local",
            now=self.now,
        )
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(
            configure_context_console(self.conn, enabled=False)["status"],
            "disabled",
        )

        revoked = revoke_context_observations_for_range(
            self.conn,
            start_at=(self.now - timedelta(seconds=1)).isoformat(timespec="seconds"),
            end_at=(self.now + timedelta(seconds=1)).isoformat(timespec="seconds"),
            reason_code="legacy_manual_range_delete",
            now=self.now,
        )

        self.assertEqual(revoked["observations"], 1)
        self.assertFalse(self.conn.in_transaction)
        observer = ensure_ready(self.db)
        try:
            self.assertEqual(
                observer.execute(
                    "SELECT status FROM context_observations WHERE observation_ref=?",
                    (accepted["observation_ref"],),
                ).fetchone()[0],
                "deleted",
            )
        finally:
            observer.close()

    def test_public_range_revoke_rolls_back_every_observation_on_any_cascade_failure(self) -> None:
        observations: list[str] = []
        for index in (1, 2):
            accepted = record_unverified_clue(
                self.conn,
                source_kind="screen",
                source_ref=f"source_3202{index:04x}",
                occurred_at=self.now.isoformat(timespec="seconds"),
                project_ref=self.project,
                workstream_ref=self.workstream,
                object_ref=self.object,
                version_ref=self.version,
                subject_ref=self.owner,
                privacy_class="private_local",
                now=self.now,
            )
            self.assertEqual(accepted["status"], "accepted")
            observations.append(accepted["observation_ref"])

        original_cascade = context_console_module._cascade_observation_locked
        for failure_type in (sqlite3.OperationalError, RuntimeError, KeyboardInterrupt):
            with self.subTest(failure_type=failure_type.__name__):
                calls = 0

                def fail_second_cascade(*args: Any, **kwargs: Any) -> dict[str, int]:
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        raise failure_type("synthetic cascade failure")
                    return original_cascade(*args, **kwargs)

                with mock.patch.object(
                    context_console_module,
                    "_cascade_observation_locked",
                    side_effect=fail_second_cascade,
                ):
                    if issubclass(failure_type, sqlite3.Error):
                        failed = revoke_context_observations_for_range(
                            self.conn,
                            start_at=(self.now - timedelta(seconds=1)).isoformat(timespec="seconds"),
                            end_at=(self.now + timedelta(seconds=1)).isoformat(timespec="seconds"),
                            reason_code="legacy_manual_range_delete",
                            now=self.now,
                        )
                        self.assertEqual(
                            (failed["status"], failed["reason_code"]),
                            ("failed", "deletion_cascade_failed"),
                        )
                    else:
                        with self.assertRaisesRegex(
                            failure_type,
                            "synthetic cascade failure",
                        ):
                            revoke_context_observations_for_range(
                                self.conn,
                                start_at=(self.now - timedelta(seconds=1)).isoformat(timespec="seconds"),
                                end_at=(self.now + timedelta(seconds=1)).isoformat(timespec="seconds"),
                                reason_code="legacy_manual_range_delete",
                                now=self.now,
                            )

                self.assertFalse(self.conn.in_transaction)
                observer = ensure_ready(self.db)
                try:
                    self.assertEqual(
                        [
                            str(row["status"])
                            for row in observer.execute(
                                "SELECT status FROM context_observations "
                                "WHERE observation_ref IN (?,?) ORDER BY observation_ref",
                                tuple(observations),
                            ).fetchall()
                        ],
                        ["active", "active"],
                    )
                finally:
                    observer.close()

        with self.assertRaises(TypeError):
            revoke_context_observations_for_range(
                self.conn,
                start_at=(self.now - timedelta(seconds=1)).isoformat(timespec="seconds"),
                reason_code="legacy_manual_range_delete",
                now=self.now,
            )
        self.assertFalse(self.conn.in_transaction)

    def test_upstream_evidence_deletion_revokes_dependent_context_graph(self) -> None:
        controlled = self._ingest_memory_confirmation(
            idempotency_key="idem_32500001",
            source_ref="source_32500001",
        )
        dependent = self._ingest_ai(
            idempotency_key="idem_32500002",
            source_ref="source_32500002",
            evidence_refs=[controlled["evidence_ref"]],
        )
        memory = create_memory_candidate(
            self.conn,
            verified_ref=controlled["verified_ref"],
            subject_ref=self.owner,
            now=self.now,
        )
        self.assertEqual(memory["status"], "candidate_only")
        prepared = prepare_delivery(
            self.conn,
            target_ref=self.target,
            project_ref=self.project,
            now=self.now,
        )
        self.assertEqual(prepared["status"], "prepared")

        deleted = delete_context_observation(
            self.conn,
            observation_ref=controlled["observation_ref"],
            reason_code="user_deleted",
            now=self.now,
        )
        self.assertEqual(deleted["status"], "completed")
        self.assertEqual(deleted["cascade"]["candidates_revoked"], 2)
        self.assertEqual(deleted["cascade"]["verified_contexts_revoked"], 1)
        self.assertEqual(deleted["cascade"]["memory_candidates_revoked"], 1)
        self.assertEqual(deleted["cascade"]["bundle_members_revoked"], 1)
        self.assertEqual(deleted["cascade"]["delivery_failures_recorded"], 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_candidates WHERE candidate_ref=?",
                (dependent["candidate_ref"],),
            ).fetchone()[0],
            "revoked",
        )
        self.assertEqual(dependent["verified_ref"], "")
        self.assertIsNone(
            self.conn.execute(
                "SELECT status FROM context_verified WHERE candidate_ref=?",
                (dependent["candidate_ref"],),
            ).fetchone()
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_memory_candidates WHERE memory_candidate_ref=?",
                (memory["memory_candidate_ref"],),
            ).fetchone()[0],
            "revoked",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT state FROM context_deliveries WHERE delivery_ref=?",
                (prepared["delivery_ref"],),
            ).fetchone()[0],
            "failed",
        )
        failed_receipt = self.conn.execute(
            "SELECT transition,failure_reason_code,result_state FROM context_delivery_receipts "
            "WHERE delivery_ref=? AND transition='failed'",
            (prepared["delivery_ref"],),
        ).fetchone()
        self.assertEqual(tuple(failed_receipt), ("failed", "source_revoked", "failed"))
        self.assertEqual(build_context_bundle(self.conn, now=self.now)["verified_contexts"], [])

    def test_observation_evidence_delete_closes_every_downstream_dependency(self) -> None:
        # Exact matrix name for the same public deletion contract exercised below.
        self.test_upstream_evidence_deletion_revokes_dependent_context_graph()

    def test_label_revoke_uses_same_dependency_closure(self) -> None:
        controlled = self._ingest_memory_confirmation(
            idempotency_key="idem_32600001",
            source_ref="source_32600001",
        )
        dependent = self._ingest_ai(
            idempotency_key="idem_32600002",
            source_ref="source_32600002",
            evidence_refs=[controlled["evidence_ref"]],
        )
        memory = create_memory_candidate(
            self.conn,
            verified_ref=controlled["verified_ref"],
            subject_ref=self.owner,
            now=self.now,
        )
        prepared = prepare_delivery(
            self.conn,
            target_ref=self.target,
            project_ref=self.project,
            now=self.now,
        )
        self.assertEqual(memory["status"], "candidate_only")
        self.assertEqual(prepared["status"], "prepared")

        revoked = revoke_context_label(self.conn, self.object)
        self.assertEqual(revoked["status"], "revoked")
        self.assertEqual(revoked["dependent_observation_count"], 2)
        self.assertGreaterEqual(revoked["cascade"]["source_event_tombstones_created"], 2)
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_candidates WHERE candidate_ref=?",
                (dependent["candidate_ref"],),
            ).fetchone()[0],
            "revoked",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_verified WHERE verified_ref=?",
                (controlled["verified_ref"],),
            ).fetchone()[0],
            "revoked",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_memory_candidates WHERE memory_candidate_ref=?",
                (memory["memory_candidate_ref"],),
            ).fetchone()[0],
            "revoked",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT state FROM context_deliveries WHERE delivery_ref=?",
                (prepared["delivery_ref"],),
            ).fetchone()[0],
            "failed",
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM context_search_index").fetchone()[0],
            0,
        )

    def test_partial_shared_evidence_revocation_preserves_only_independent_survivors(self) -> None:
        shared = self._ingest_memory_confirmation(
            idempotency_key="idem_32700001",
            source_ref="source_32700001",
        )
        required = self._ingest_trusted_confirmation(
            idempotency_key="idem_32700002",
            source_ref="source_32700002",
        )
        self.conn.execute(
            "INSERT INTO context_candidate_evidence(candidate_ref,evidence_ref,dependency_role) "
            "VALUES(?,?,'required')",
            (required["candidate_ref"], shared["evidence_ref"]),
        )
        self.conn.commit()
        survivor = self._ingest_trusted_confirmation(
            idempotency_key="idem_32700003",
            source_ref="source_32700003",
            occurred_at=self.now + timedelta(seconds=1),
        )
        self.conn.execute(
            "INSERT INTO context_candidate_evidence(candidate_ref,evidence_ref,dependency_role) "
            "VALUES(?,?,'supplemental')",
            (survivor["candidate_ref"], shared["evidence_ref"]),
        )
        self.conn.commit()
        prepared = prepare_delivery(
            self.conn,
            target_ref=self.target,
            project_ref=self.project,
            now=self.now + timedelta(seconds=2),
        )
        self.assertEqual(prepared["status"], "prepared")

        deleted = delete_context_observation(
            self.conn,
            observation_ref=shared["observation_ref"],
            reason_code="user_deleted",
            now=self.now + timedelta(seconds=3),
        )
        self.assertEqual(deleted["status"], "completed")
        self.assertEqual(deleted["cascade"]["supplemental_evidence_links_removed"], 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_candidates WHERE candidate_ref=?",
                (required["candidate_ref"],),
            ).fetchone()[0],
            "revoked",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_verified WHERE verified_ref=?",
                (required["verified_ref"],),
            ).fetchone()[0],
            "revoked",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_candidates WHERE candidate_ref=?",
                (survivor["candidate_ref"],),
            ).fetchone()[0],
            "active",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_verified WHERE verified_ref=?",
                (survivor["verified_ref"],),
            ).fetchone()[0],
            "active",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM context_candidate_evidence WHERE candidate_ref=? AND evidence_ref=?",
                (survivor["candidate_ref"], shared["evidence_ref"]),
            ).fetchone()[0],
            0,
        )
        survivor_members = self.conn.execute(
            "SELECT status FROM context_bundle_members WHERE bundle_ref=? AND member_ref=?",
            (prepared["bundle_ref"], survivor["verified_ref"]),
        ).fetchall()
        self.assertEqual([row["status"] for row in survivor_members], ["active"])
        active_bundle_members = {
            row["member_ref"]
            for row in self.conn.execute(
                "SELECT member_ref FROM context_bundle_members WHERE bundle_ref=? AND status='active'",
                (prepared["bundle_ref"],),
            )
        }
        self.assertEqual(active_bundle_members, {survivor["verified_ref"]})
        bundle = build_context_bundle(self.conn, now=self.now + timedelta(seconds=4))
        surviving_contexts = bundle["verified_contexts"]
        self.assertEqual({item["source_ref"] for item in surviving_contexts}, {"source_32700003"})
        self.assertEqual(len(surviving_contexts), 1)
        self.assertTrue(surviving_contexts[0]["context_ref"].startswith("context_"))
        self.assertNotEqual(surviving_contexts[0]["context_ref"], survivor["verified_ref"])

    def test_expired_revoked_principal_or_p5_evidence_does_not_preserve_supplemental_survivor(
        self,
    ) -> None:
        shared = self._ingest_memory_confirmation(
            idempotency_key="idem_32800001",
            source_ref="source_32800001",
        )
        branches = {
            "expired": self._ingest_trusted_confirmation(
                idempotency_key="idem_32800002",
                source_ref="source_32800002",
                occurred_at=self.now + timedelta(seconds=1),
            ),
            "revoked_principal": self._ingest_trusted_confirmation(
                idempotency_key="idem_32800003",
                source_ref="source_32800003",
                occurred_at=self.now + timedelta(seconds=2),
            ),
            "p5": self._ingest_trusted_confirmation(
                idempotency_key="idem_32800004",
                source_ref="source_32800004",
                occurred_at=self.now + timedelta(seconds=3),
            ),
        }
        own_evidence: dict[str, str] = {}
        for name, branch in branches.items():
            self.conn.execute(
                "INSERT INTO context_candidate_evidence(candidate_ref,evidence_ref,dependency_role) "
                "VALUES(?,?,'supplemental')",
                (branch["candidate_ref"], shared["evidence_ref"]),
            )
            own_evidence[name] = str(
                self.conn.execute(
                    "SELECT evidence_ref FROM context_candidate_evidence "
                    "WHERE candidate_ref=? AND dependency_role='required'",
                    (branch["candidate_ref"],),
                ).fetchone()[0]
            )
        self.conn.commit()
        prepared = prepare_delivery(
            self.conn,
            target_ref=self.target,
            project_ref=self.project,
            now=self.now + timedelta(seconds=4),
        )
        self.assertEqual(prepared["status"], "prepared")

        self.conn.execute(
            "UPDATE context_evidence SET expires_at=? WHERE evidence_ref=?",
            (
                (self.now - timedelta(seconds=1)).isoformat(timespec="seconds"),
                own_evidence["expired"],
            ),
        )
        self.conn.execute(
            "INSERT INTO context_principals(principal_id,key_id,principal_kind,project_ref,"
            "key_fingerprint,status,created_at,revoked_at) "
            "VALUES('principal_32800005','key_32800005','ai_lifecycle',?,?,'revoked',?,?)",
            (
                self.project,
                "0" * 64,
                self.now.isoformat(timespec="seconds"),
                self.now.isoformat(timespec="seconds"),
            ),
        )
        self.conn.execute(
            "UPDATE context_evidence SET principal_id='principal_32800005',"
            "key_id='key_32800005',adapter_kind='ai_lifecycle' WHERE evidence_ref=?",
            (own_evidence["revoked_principal"],),
        )
        self.conn.execute(
            "UPDATE context_evidence SET evidence_tier='P5' WHERE evidence_ref=?",
            (own_evidence["p5"],),
        )
        self.conn.commit()

        deleted = delete_context_observation(
            self.conn,
            observation_ref=shared["observation_ref"],
            reason_code="user_deleted",
            now=self.now + timedelta(seconds=5),
        )
        self.assertEqual(deleted["status"], "completed")
        for name, branch in branches.items():
            with self.subTest(invalid_survivor=name):
                self.assertEqual(
                    self.conn.execute(
                        "SELECT status FROM context_candidates WHERE candidate_ref=?",
                        (branch["candidate_ref"],),
                    ).fetchone()[0],
                    "revoked",
                )
                self.assertEqual(
                    self.conn.execute(
                        "SELECT status FROM context_verified WHERE verified_ref=?",
                        (branch["verified_ref"],),
                    ).fetchone()[0],
                    "revoked",
                )
                self.assertEqual(
                    self.conn.execute(
                        "SELECT COUNT(*) FROM context_search_index WHERE verified_ref=?",
                        (branch["verified_ref"],),
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    self.conn.execute(
                        "SELECT status FROM context_bundle_members "
                        "WHERE bundle_ref=? AND member_ref=?",
                        (prepared["bundle_ref"], branch["verified_ref"]),
                    ).fetchone()[0],
                    "revoked",
                )
        self.assertEqual(
            self.conn.execute(
                "SELECT state FROM context_deliveries WHERE delivery_ref=?",
                (prepared["delivery_ref"],),
            ).fetchone()[0],
            "failed",
        )

    def test_ha_source_keeps_content_free_unverified_clue_without_body(self) -> None:
        forbidden_values = {
            "cleaned text must not cross",
            "window title must not cross",
            "/tmp/private-document.md",
            "subject body must not cross",
        }
        output = _safe_timeline_events(
            [
                {
                    "kind": "unverified_clue",
                    "source_ref": "source_ha000001",
                    "observed_at": self.now.isoformat(),
                    "reason_code": "screen_observation_unverified",
                    "privacy_level": "normal",
                    "dedupe_status": "unique",
                    "app_class": "editor",
                    "cleaned_text": "cleaned text must not cross",
                    "window_title": "window title must not cross",
                    "path": "/tmp/private-document.md",
                    "subject": "subject body must not cross",
                    "body": "cleaned text must not cross",
                }
            ]
        )
        self.assertEqual(len(output), 1)
        clue = output[0]
        self.assertEqual(clue["contract_version"], "HAUnverifiedClueV1")
        self.assertEqual(clue["source_type"], "content_free_unverified_clue")
        self.assertEqual(clue["verification_state"], "unverified")
        self.assertFalse(clue["trusted"])
        self.assertFalse(clue["consumed"])
        self.assertFalse(clue["delivered"])
        self.assertFalse(clue["cleaned_text_included"])
        self.assertFalse(clue["raw_access"])
        serialized = json.dumps(clue, ensure_ascii=False, sort_keys=True)
        for key in ("cleaned_text", "window_title", "path", "subject", "body"):
            self.assertNotIn(f'"{key}"', serialized)
        for value in forbidden_values:
            self.assertNotIn(value, serialized)

    def test_cascade_bundle_and_withdrawal_faults_roll_back_then_retry_once(self) -> None:
        bundled = self._ingest_trusted_confirmation(
            idempotency_key="idem_32800001",
            source_ref="source_32800001",
        )
        prepared = prepare_delivery(
            self.conn,
            target_ref=self.target,
            project_ref=self.project,
            now=self.now,
        )
        self.conn.execute(
            "CREATE TRIGGER context_bundle_fault BEFORE UPDATE ON context_bundle_members "
            "WHEN NEW.status='revoked' BEGIN SELECT RAISE(ABORT,'synthetic bundle fault'); END"
        )
        self.conn.commit()
        failed_bundle = delete_context_observation(
            self.conn,
            observation_ref=bundled["observation_ref"],
            reason_code="user_deleted",
            now=self.now,
        )
        self.assertEqual((failed_bundle["status"], failed_bundle["reason_code"]), (
            "failed", "deletion_cascade_failed"
        ))
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_observations WHERE observation_ref=?",
                (bundled["observation_ref"],),
            ).fetchone()[0],
            "active",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_bundle_members WHERE bundle_ref=? AND member_ref=?",
                (prepared["bundle_ref"], bundled["verified_ref"]),
            ).fetchone()[0],
            "active",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT state FROM context_deliveries WHERE delivery_ref=?",
                (prepared["delivery_ref"],),
            ).fetchone()[0],
            "prepared",
        )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM context_source_event_tombstones").fetchone()[0], 0)
        self.conn.execute("DROP TRIGGER context_bundle_fault")
        self.conn.commit()
        bundle_retry = delete_context_observation(
            self.conn,
            observation_ref=bundled["observation_ref"],
            reason_code="user_deleted",
            now=self.now,
        )
        self.assertEqual(bundle_retry["status"], "completed")
        bundle_replay = delete_context_observation(
            self.conn,
            observation_ref=bundled["observation_ref"],
            reason_code="user_deleted",
            now=self.now,
        )
        self.assertEqual(bundle_replay["status"], "already_revoked")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM context_source_event_tombstones").fetchone()[0], 1)

        delivered = self._ingest_memory_confirmation(
            idempotency_key="idem_32800002",
            source_ref="source_32800002",
            occurred_at=self.now + timedelta(seconds=1),
        )
        prepared_delivered = prepare_delivery(
            self.conn,
            target_ref=self.target,
            project_ref=self.project,
            now=self.now + timedelta(seconds=2),
        )
        consumed_payload = self._delivery_payload(
            principal_name="consumer",
            delivery_ref=prepared_delivered["delivery_ref"],
            bundle_hash=prepared_delivered["bundle_hash"],
            transition="consumed",
            idempotency_key="idem_32800003",
            provider_receipt_ref="provider_32800003",
            occurred_at=self.now + timedelta(seconds=3),
        )
        consumed = ingest_delivery_receipt(
            self.conn,
            self._wire(consumed_payload),
            verification_secret=self.secrets["consumer"],
            now=self.now + timedelta(seconds=3),
        )
        self.assertEqual(consumed["status"], "accepted")
        delivered_payload = self._delivery_payload(
            principal_name="provider",
            delivery_ref=prepared_delivered["delivery_ref"],
            bundle_hash=prepared_delivered["bundle_hash"],
            transition="delivered",
            idempotency_key="idem_32800004",
            provider_receipt_ref="provider_32800004",
            occurred_at=self.now + timedelta(seconds=4),
        )
        delivered_receipt = ingest_delivery_receipt(
            self.conn,
            self._wire(delivered_payload),
            verification_secret=self.secrets["provider"],
            now=self.now + timedelta(seconds=4),
        )
        self.assertEqual(delivered_receipt["status"], "accepted")
        self.conn.execute(
            "CREATE TRIGGER context_withdrawal_fault BEFORE INSERT ON context_withdrawal_receipts "
            "BEGIN SELECT RAISE(ABORT,'synthetic withdrawal fault'); END"
        )
        self.conn.commit()
        failed_withdrawal = delete_context_observation(
            self.conn,
            observation_ref=delivered["observation_ref"],
            reason_code="user_deleted",
            now=self.now + timedelta(seconds=5),
        )
        self.assertEqual((failed_withdrawal["status"], failed_withdrawal["reason_code"]), (
            "failed", "deletion_cascade_failed"
        ))
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_observations WHERE observation_ref=?",
                (delivered["observation_ref"],),
            ).fetchone()[0],
            "active",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT state FROM context_deliveries WHERE delivery_ref=?",
                (prepared_delivered["delivery_ref"],),
            ).fetchone()[0],
            "delivered",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM context_withdrawal_receipts WHERE delivery_ref=?",
                (prepared_delivered["delivery_ref"],),
            ).fetchone()[0],
            0,
        )
        self.conn.execute("DROP TRIGGER context_withdrawal_fault")
        self.conn.commit()
        withdrawal_retry = delete_context_observation(
            self.conn,
            observation_ref=delivered["observation_ref"],
            reason_code="user_deleted",
            now=self.now + timedelta(seconds=5),
        )
        self.assertEqual(withdrawal_retry["status"], "completed")
        withdrawal_replay = delete_context_observation(
            self.conn,
            observation_ref=delivered["observation_ref"],
            reason_code="user_deleted",
            now=self.now + timedelta(seconds=5),
        )
        self.assertEqual(withdrawal_replay["status"], "already_revoked")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM context_withdrawal_receipts WHERE delivery_ref=?",
                (prepared_delivered["delivery_ref"],),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT state FROM context_deliveries WHERE delivery_ref=?",
                (prepared_delivered["delivery_ref"],),
            ).fetchone()[0],
            "delivered",
        )

    def test_delete_failure_rolls_back_and_never_reports_success(self) -> None:
        accepted = self._ingest_trusted_confirmation(
            idempotency_key="idem_33000001", source_ref="source_33000001"
        )
        self.conn.execute(
            "CREATE TRIGGER context_test_revoke_failure BEFORE UPDATE ON context_evidence "
            "WHEN NEW.status='revoked' BEGIN SELECT RAISE(ABORT,'synthetic failure'); END"
        )
        self.conn.commit()
        result = delete_context_observation(
            self.conn,
            observation_ref=accepted["observation_ref"],
            reason_code="user_deleted",
            now=self.now,
        )
        self.assertEqual((result["status"], result["reason_code"]), ("failed", "deletion_cascade_failed"))
        self.assertEqual(
            self.conn.execute("SELECT status FROM context_observations WHERE observation_ref=?", (accepted["observation_ref"],)).fetchone()[0],
            "active",
        )
        self.assertEqual(
            self.conn.execute("SELECT status FROM context_verified WHERE verified_ref=?", (accepted["verified_ref"],)).fetchone()[0],
            "active",
        )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM context_source_event_tombstones").fetchone()[0], 0)
        self.conn.execute("DROP TRIGGER context_test_revoke_failure")
        self.conn.commit()
        retried = delete_context_observation(
            self.conn,
            observation_ref=accepted["observation_ref"],
            reason_code="user_deleted",
            now=self.now,
        )
        self.assertEqual(retried["status"], "completed")
        repeated = delete_context_observation(
            self.conn,
            observation_ref=accepted["observation_ref"],
            reason_code="user_deleted",
            now=self.now,
        )
        self.assertEqual(repeated["status"], "already_revoked")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM context_source_event_tombstones").fetchone()[0], 1)

    def test_partial_bundle_deletion_preserves_only_surviving_relational_members(self) -> None:
        first = self._ingest_trusted_confirmation(
            idempotency_key="idem_34000001", source_ref="source_34000001"
        )
        second = self._ingest_trusted_confirmation(
            idempotency_key="idem_34000002",
            source_ref="source_34000002",
            occurred_at=self.now + timedelta(seconds=1),
        )
        prepared = prepare_delivery(
            self.conn,
            target_ref=self.target,
            project_ref=self.project,
            now=self.now + timedelta(seconds=2),
        )
        before = self.conn.execute(
            "SELECT member_ref,status FROM context_bundle_members WHERE bundle_ref=? ORDER BY member_ref",
            (prepared["bundle_ref"],),
        ).fetchall()
        self.assertEqual({row["member_ref"] for row in before}, {first["verified_ref"], second["verified_ref"]})

        deleted = delete_context_observation(
            self.conn,
            observation_ref=first["observation_ref"],
            reason_code="user_deleted",
            now=self.now + timedelta(seconds=3),
        )
        self.assertEqual(deleted["status"], "completed")
        rows = self.conn.execute(
            "SELECT member_ref,status FROM context_bundle_members WHERE bundle_ref=? ORDER BY member_ref",
            (prepared["bundle_ref"],),
        ).fetchall()
        active = [row["member_ref"] for row in rows if row["status"] == "active"]
        revoked = [row["member_ref"] for row in rows if row["status"] == "revoked"]
        self.assertEqual(active, [second["verified_ref"]])
        self.assertEqual(revoked, [first["verified_ref"]])
        for row in rows:
            self.assertRegex(row["member_ref"], r"^verified_[a-z0-9]{8,64}$")
            self.assertNotIn("[", row["member_ref"])
            self.assertNotIn('"', row["member_ref"])

    def test_retention_policies_are_independent_and_fixed(self) -> None:
        old_clue_time = self.now - timedelta(days=3)
        clue = record_unverified_clue(
            self.conn,
            source_kind="screen",
            source_ref="source_41000001",
            occurred_at=old_clue_time.isoformat(timespec="seconds"),
            project_ref=self.project,
            workstream_ref=self.workstream,
            object_ref=self.object,
            version_ref=self.version,
            subject_ref=self.owner,
            privacy_class="private_local",
            now=old_clue_time,
        )
        verified = self._ingest_trusted_confirmation(
            idempotency_key="idem_41000002", source_ref="source_41000004"
        )
        old_win = record_product_win(
            self.conn,
            win_ref="win_41000001",
            occurred_at=(self.now - timedelta(days=31)).isoformat(timespec="seconds"),
            project_ref=self.project,
            outcome_code="recovered_and_resumed",
            source_ref="source_41000002",
            minutes_saved=20,
        )
        fresh_win = record_product_win(
            self.conn,
            win_ref="win_41000002",
            occurred_at=(self.now - timedelta(days=5)).isoformat(timespec="seconds"),
            project_ref=self.project,
            outcome_code="handoff_reused",
            source_ref="source_41000003",
            minutes_saved=10,
        )
        self.assertEqual((old_win["status"], fresh_win["status"]), ("recorded", "recorded"))

        preview = preview_context_retention(self.conn, now=self.now)
        self.assertEqual(preview["would_expire_or_delete"]["unverified_observations"], 1)
        self.assertEqual(preview["would_expire_or_delete"]["verified_contexts"], 0)
        self.assertEqual(preview["would_expire_or_delete"]["product_wins"], 1)
        cleanup = cleanup_context_retention(self.conn, now=self.now)
        self.assertEqual(cleanup["status"], "completed")
        self.assertEqual(cleanup["unverified_observations_expired"], 1)
        self.assertEqual(cleanup["verified_contexts_expired"], 0)
        self.assertEqual(cleanup["product_wins_deleted"], 1)
        self.assertEqual(
            self.conn.execute("SELECT status FROM context_observations WHERE observation_ref=?", (clue["observation_ref"],)).fetchone()[0],
            "expired",
        )
        self.assertEqual(
            self.conn.execute("SELECT status FROM context_verified WHERE verified_ref=?", (verified["verified_ref"],)).fetchone()[0],
            "active",
        )
        status = product_win_status(self.conn, now=self.now)
        self.assertEqual((status["active_win_count"], status["minutes_saved"]), (1, 10))

    def test_clue_freshness_and_product_win_idempotency_fail_closed(self) -> None:
        old_clue = record_unverified_clue(
            self.conn,
            source_kind="screen",
            source_ref="source_41400001",
            occurred_at=(self.now - timedelta(days=3)).isoformat(timespec="seconds"),
            project_ref=self.project,
            workstream_ref=self.workstream,
            object_ref=self.object,
            version_ref=self.version,
            subject_ref=self.owner,
            privacy_class="private_local",
            now=self.now,
        )
        future_clue = record_unverified_clue(
            self.conn,
            source_kind="ocr",
            source_ref="source_41400002",
            occurred_at=(self.now + timedelta(minutes=6)).isoformat(timespec="seconds"),
            project_ref=self.project,
            workstream_ref=self.workstream,
            object_ref=self.object,
            version_ref=self.version,
            subject_ref=self.owner,
            privacy_class="private_local",
            now=self.now,
        )
        self.assertEqual((old_clue["status"], old_clue["reason_code"]), ("rejected", "unverified_clue_too_old"))
        self.assertEqual((future_clue["status"], future_clue["reason_code"]), ("rejected", "receipt_from_future"))

        kwargs = {
            "win_ref": "win_41400001",
            "occurred_at": self.now.isoformat(timespec="seconds"),
            "project_ref": self.project,
            "outcome_code": "handoff_reused",
            "source_ref": "source_41400003",
            "minutes_saved": 12,
        }
        first = record_product_win(self.conn, **kwargs)
        replay = record_product_win(self.conn, **kwargs)
        conflict = record_product_win(self.conn, **{**kwargs, "minutes_saved": 13})
        self.assertEqual(first["status"], "recorded")
        self.assertEqual(
            (replay["status"], replay["reason_code"]),
            ("replayed", "idempotent_replay"),
        )
        self.assertEqual((conflict["status"], conflict["reason_code"]), ("conflict", "product_win_conflict"))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM context_product_wins WHERE win_ref=?", (kwargs["win_ref"],)).fetchone()[0],
            1,
        )

    def test_product_experiment_natural_days_and_component_soak_are_separate(self) -> None:
        not_started = product_experiment_status(self.conn, local_today=date(2026, 7, 29))
        self.assertEqual(not_started["status"], "not_started")
        rejected = start_product_experiment(
            self.conn,
            experiment_ref="experiment_51000001",
            confirmation="wrong",
        )
        self.assertEqual(rejected["reason_code"], "experiment_start_confirmation_required")
        started = start_product_experiment(
            self.conn,
            experiment_ref="experiment_51000001",
            confirmation="START_LOCAL_PRODUCT_EXPERIMENT",
            now=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
        )
        self.assertTrue(started["experiment_started"])
        self.assertEqual(product_experiment_status(self.conn, experiment_ref="experiment_51000001", local_today=date(2026, 7, 13))["status"], "collecting")
        pending_day14 = product_experiment_status(
            self.conn, experiment_ref="experiment_51000001", local_today=date(2026, 7, 14)
        )
        self.assertEqual((pending_day14["status"], pending_day14["review_readiness"]), ("extended", "pending"))
        self.assertIsNone(pending_day14["useful_rate"])
        self.assertIsNone(pending_day14["median_resume_seconds"])
        for index in range(1, 11):
            recorded = record_product_experiment_attempt(
                self.conn,
                experiment_ref="experiment_51000001",
                local_date=f"2026-07-{index:02d}",
                source_ref=f"source_51{index:06d}",
                outcome="useful" if index <= 7 else "not_useful",
                resume_seconds=float(index * 10),
            )
            self.assertEqual(recorded["status"], "recorded")
        day14 = product_experiment_status(self.conn, experiment_ref="experiment_51000001", local_today=date(2026, 7, 14))
        self.assertEqual((day14["status"], day14["elapsed_natural_days"]), ("not_passed", 14))
        self.assertEqual((day14["attempt_count"], day14["rated_attempt_count"]), (10, 10))
        self.assertAlmostEqual(day14["useful_rate"], 0.7)
        self.assertEqual(day14["median_resume_seconds"], 55.0)
        day21 = product_experiment_status(self.conn, experiment_ref="experiment_51000001", local_today=date(2026, 7, 21))
        self.assertEqual((day21["status"], day21["elapsed_natural_days"]), ("not_passed", 21))
        self.assertFalse(day21["value_gate_passed"])
        self.assertEqual(day21["decision_status"], "not_passed")
        self.assertEqual(
            product_experiment_status(self.conn, experiment_ref="experiment_51000001", local_today=date(2026, 7, 22))["status"],
            "not_passed",
        )

        record_product_win(
            self.conn,
            win_ref="win_51000001",
            occurred_at=self.now.isoformat(timespec="seconds"),
            project_ref=self.project,
            outcome_code="trusted_context_avoided_rework",
            source_ref="source_51000001",
        )
        before_wins = product_win_status(self.conn, now=self.now)["active_win_count"]
        baseline = record_component_baseline(
            self.conn,
            component="context_fusion",
            version_ref=self.version,
            reset_reason_code="contract_changed",
        )
        self.assertEqual(baseline["generation"], 1)
        self.assertFalse(baseline["product_experiment_reset"])
        self.assertFalse(baseline["product_wins_reset"])
        self.assertEqual(product_win_status(self.conn, now=self.now)["active_win_count"], before_wins)
        self.assertTrue(product_experiment_status(self.conn, experiment_ref="experiment_51000001", local_today=date(2026, 7, 14))["experiment_started"])
        self.assertEqual(classify_stability_change("ui")["status"], "no_reset")
        self.assertEqual(classify_stability_change("copy")["status"], "no_reset")
        self.assertEqual(classify_stability_change("storage")["affected_components"], ["storage"])

    def test_context_console_database_busy_status_returns_unknown_with_observed_at(self) -> None:
        observed_at = self.now.isoformat(timespec="seconds")
        self.conn.close()
        locker = sqlite3.connect(self.db, timeout=1, isolation_level=None)
        try:
            locker.execute("PRAGMA journal_mode=DELETE")
            locker.execute("BEGIN EXCLUSIVE")
            status = context_console_status_path(
                self.db,
                busy_timeout_seconds=0.01,
                now=self.now,
            )
            self.assertEqual(status["status"], "unknown")
            self.assertEqual(status["reason_code"], "database_busy")
            self.assertEqual(status["observed_at"], observed_at)
            self.assertIsNone(status["counts"])
            self.assertIsNone(status["last_successful_read_at"])
            self.assertEqual(status["service_status"], "unknown")
            self.assertFalse(status["actions_enabled"])
        finally:
            locker.rollback()
            locker.close()
            self.conn = ensure_ready(self.db)

        missing = context_console_status_path(self.root / "missing.sqlite", now=self.now)
        self.assertEqual((missing["status"], missing["reason_code"]), ("unknown", "database_missing"))
        self.assertEqual(missing["observed_at"], observed_at)

    def test_report_context_adapter_keeps_v2_and_excludes_clues_and_internal_fields(self) -> None:
        accepted = self._ingest_trusted_confirmation(
            idempotency_key="idem_61000001", source_ref="source_61000002"
        )
        record_unverified_clue(
            self.conn,
            source_kind="screen",
            source_ref="source_61000001",
            occurred_at=self.now.isoformat(timespec="seconds"),
            project_ref=self.project,
            workstream_ref=self.workstream,
            object_ref=self.object,
            version_ref=self.version,
            subject_ref=self.owner,
            privacy_class="private_local",
        )
        adapter = report_context_bundle_adapter(
            self.conn,
            start_at=(self.now - timedelta(minutes=1)).isoformat(timespec="seconds"),
            end_at=(self.now + timedelta(minutes=1)).isoformat(timespec="seconds"),
        )
        self.assertEqual(adapter["contract_version"], CONTEXT_BUNDLE_VERSION)
        self.assertEqual(adapter["unverified_clues"], [])
        self.assertEqual([item["source_ref"] for item in adapter["verified_contexts"]], ["source_61000002"])
        self.assertEqual(len(adapter["verified_contexts"]), 1)
        self.assertTrue(adapter["verified_contexts"][0]["context_ref"].startswith("context_"))
        self.assertNotEqual(adapter["verified_contexts"][0]["context_ref"], accepted["verified_ref"])

        payload = report_context(self.conn, self.now.date().isoformat())
        self.assertEqual(payload["report_context_contract_version"], 2)
        self.assertFalse(payload["raw_access"])
        self.assertEqual(payload["context_bundle_v1"]["unverified_clues"], [])
        encoded = json.dumps(payload["context_bundle_v1"], ensure_ascii=False)
        for forbidden in ("candidate_ref", "observation_ref", "evidence_ref", "provenance_ref"):
            self.assertNotIn(forbidden, encoded)


    def test_scoped_idempotency_and_tombstone_block_new_key_revival(self) -> None:
        first = self._receipt_payload(
            idempotency_key="idem_61000001",
            source_ref="source_61000001",
        )
        accepted = ingest_context_receipt(
            self.conn,
            self._wire(first),
            verification_secret=self.secrets["ai"],
            now=self.now,
        )
        self.assertEqual(accepted["status"], "accepted")

        second_secret = b"second-ai-secret-0001"
        second_principal = "principal_61616161"
        second_key = "key_61616161"
        self.assertEqual(
            register_context_principal(
                self.conn,
                principal_id=second_principal,
                key_id=second_key,
                principal_kind="ai_lifecycle",
                verification_secret=second_secret,
                project_ref=self.project,
            )["status"],
            "registered",
        )
        second = copy.deepcopy(first)
        second.update(
            {
                "producer_id": second_principal,
                "key_id": second_key,
                "source_ref": "source_61000002",
                "signature": "",
            }
        )
        second["signature"] = sign_contract_payload(second, second_secret)
        scoped = ingest_context_receipt(
            self.conn,
            self._wire(second),
            verification_secret=second_secret,
            now=self.now,
        )
        self.assertEqual(scoped["status"], "accepted")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM context_ingest_receipts WHERE idempotency_key='idem_61000001'"
            ).fetchone()[0],
            2,
        )

        original = self._controlled_payload(
            principal_name="user",
            evidence_kind="user_confirmation",
            state="completed_confirmed",
            idempotency_key="idem_61000003",
            source_ref="source_61000003",
        )
        materialized = ingest_controlled_evidence(
            self.conn,
            self._wire(original),
            verification_secret=self.secrets["user"],
            now=self.now,
        )
        self.assertEqual(materialized["status"], "accepted")
        self.assertEqual(
            delete_context_observation(
                self.conn,
                observation_ref=materialized["observation_ref"],
                reason_code="user_deleted",
                now=self.now,
            )["status"],
            "completed",
        )
        replay = copy.deepcopy(original)
        replay["idempotency_key"] = "idem_61000004"
        replay["signature"] = sign_contract_payload(replay, self.secrets["user"])
        blocked = ingest_controlled_evidence(
            self.conn,
            self._wire(replay),
            verification_secret=self.secrets["user"],
            now=self.now,
        )
        self.assertEqual((blocked["status"], blocked["reason_code"]), ("rejected", "source_event_tombstoned"))
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM context_observations WHERE source_ref='source_61000003'"
            ).fetchone()[0],
            1,
        )

    def test_bundle_hash_is_time_and_query_order_independent_and_frozen_fetch_matches(self) -> None:
        for index, offset in enumerate((2, 1), start=1):
            result = self._ingest_memory_confirmation(
                idempotency_key=f"idem_6200000{index}",
                source_ref=f"source_6200000{index}",
                occurred_at=self.now - timedelta(minutes=offset),
            )
            self.assertEqual(result["status"], "accepted")
        first = build_context_bundle(self.conn, project_ref=self.project, now=self.now)
        self.conn.execute("PRAGMA reverse_unordered_selects=ON")
        reversed_read = build_context_bundle(
            self.conn,
            project_ref=self.project,
            now=self.now + timedelta(seconds=11),
        )
        self.conn.execute("PRAGMA reverse_unordered_selects=OFF")
        self.assertEqual(first["bundle_hash"], reversed_read["bundle_hash"])
        self.assertEqual(first["frozen_payload"], reversed_read["frozen_payload"])
        self.assertNotIn("generated_at", first["frozen_payload"])

        prepared = prepare_delivery(
            self.conn,
            target_ref=self.target,
            project_ref=self.project,
            now=self.now,
        )
        self.assertEqual(prepared["status"], "prepared")
        fetched = fetch_prepared_delivery(
            self.conn,
            delivery_ref=prepared["delivery_ref"],
            now=self.now + timedelta(seconds=1),
        )
        self.assertEqual(fetched["status"], "available")
        self.assertEqual(fetched["frozen_payload"], prepared["frozen_payload"])
        canonical = json.dumps(
            fetched["frozen_payload"],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        self.assertEqual(
            fetched["bundle_hash"],
            "sha256:" + hashlib.sha256(canonical).hexdigest(),
        )

    def test_failed_delivery_creates_monotonic_new_attempt_and_old_ack_cannot_cross(self) -> None:
        prepared = self._prepare_one_delivery(idempotency_key="idem_63000001")
        failed_payload = self._delivery_payload(
            principal_name="consumer",
            delivery_ref=prepared["delivery_ref"],
            bundle_hash=prepared["bundle_hash"],
            transition="failed",
            idempotency_key="idem_63000002",
            failure_reason_code="provider_unavailable",
        )
        failed = ingest_delivery_receipt(
            self.conn,
            self._wire(failed_payload),
            verification_secret=self.secrets["consumer"],
            now=self.now,
        )
        self.assertEqual((failed["status"], failed["state"]), ("accepted", "failed"))
        retry = prepare_delivery(
            self.conn,
            target_ref=self.target,
            project_ref=self.project,
            now=self.now + timedelta(seconds=1),
        )
        self.assertEqual((retry["status"], retry["attempt_no"]), ("prepared", 2))
        self.assertNotEqual(retry["delivery_ref"], prepared["delivery_ref"])

        old_ack = self._delivery_payload(
            principal_name="consumer",
            delivery_ref=prepared["delivery_ref"],
            bundle_hash=prepared["bundle_hash"],
            transition="consumed",
            idempotency_key="idem_63000003",
            provider_receipt_ref="provider_63000003",
            occurred_at=self.now + timedelta(seconds=1),
        )
        blocked = ingest_delivery_receipt(
            self.conn,
            self._wire(old_ack),
            verification_secret=self.secrets["consumer"],
            now=self.now + timedelta(seconds=1),
        )
        self.assertEqual((blocked["status"], blocked["reason_code"]), ("rejected", "illegal_delivery_transition"))
        self.assertEqual(
            self.conn.execute(
                "SELECT state FROM context_deliveries WHERE delivery_ref=?",
                (retry["delivery_ref"],),
            ).fetchone()[0],
            "prepared",
        )

    def test_unicode_bidi_and_zero_width_controls_are_rejected(self) -> None:
        for index, control in enumerate(("\u202e", "\u2066", "\u200b"), start=1):
            payload = self._receipt_payload(idempotency_key=f"idem_6400000{index}")
            payload["source_ref"] = f"source_6400{control}000{index}"
            payload["signature"] = sign_contract_payload(payload, self.secrets["ai"])
            result = ingest_context_receipt(
                self.conn,
                self._wire(payload),
                verification_secret=self.secrets["ai"],
                now=self.now,
            )
            self.assertEqual(result["reason_code"], "unicode_control_forbidden")

    def test_evidence_binding_rejects_cross_object_subject_time_and_same_adapter_dual_source(self) -> None:
        other_object = "object_64646464"
        other_subject = "subject_65656565"
        self._register_label("object", other_object, "Other Object")
        self._register_label("subject", other_subject, "Other Owner", subject_scope="owner")

        cross_object = self._controlled_payload(
            principal_name="file",
            evidence_kind="file_saved",
            state="artifact_saved",
            idempotency_key="idem_65000001",
            source_ref="source_65000001",
            memory_eligible=False,
        )
        cross_object["object_ref"] = other_object
        cross_object["signature"] = sign_contract_payload(cross_object, self.secrets["file"])
        object_result = ingest_controlled_evidence(
            self.conn,
            self._wire(cross_object),
            verification_secret=self.secrets["file"],
            now=self.now,
        )
        self.assertEqual(object_result["status"], "accepted")
        receipt = self._receipt_payload(
            idempotency_key="idem_65000002",
            evidence_refs=[object_result["evidence_ref"]],
        )
        rejected = ingest_context_receipt(
            self.conn,
            self._wire(receipt),
            verification_secret=self.secrets["ai"],
            now=self.now,
        )
        self.assertEqual(rejected["reason_code"], "evidence_object_mismatch")

        cross_subject = self._controlled_payload(
            principal_name="file",
            evidence_kind="file_saved",
            state="artifact_saved",
            idempotency_key="idem_65000003",
            source_ref="source_65000003",
            subject_ref=other_subject,
            memory_eligible=False,
        )
        subject_result = ingest_controlled_evidence(
            self.conn,
            self._wire(cross_subject),
            verification_secret=self.secrets["file"],
            now=self.now,
        )
        subject_receipt = self._receipt_payload(
            idempotency_key="idem_65000004",
            evidence_refs=[subject_result["evidence_ref"]],
        )
        rejected = ingest_context_receipt(
            self.conn,
            self._wire(subject_receipt),
            verification_secret=self.secrets["ai"],
            now=self.now,
        )
        self.assertEqual(rejected["reason_code"], "evidence_subject_mismatch")

        old_time = self.now - timedelta(hours=25)
        old_payload = self._controlled_payload(
            principal_name="file",
            evidence_kind="file_saved",
            state="artifact_saved",
            idempotency_key="idem_65000005",
            source_ref="source_65000005",
            occurred_at=old_time,
            memory_eligible=False,
        )
        old_result = ingest_controlled_evidence(
            self.conn,
            self._wire(old_payload),
            verification_secret=self.secrets["file"],
            now=old_time,
        )
        time_receipt = self._receipt_payload(
            idempotency_key="idem_65000006",
            evidence_refs=[old_result["evidence_ref"]],
        )
        rejected = ingest_context_receipt(
            self.conn,
            self._wire(time_receipt),
            verification_secret=self.secrets["ai"],
            now=self.now,
        )
        self.assertEqual(rejected["reason_code"], "evidence_time_window_mismatch")

        second_file_secret = b"second-file-secret-01"
        second_file_principal = "principal_65656566"
        second_file_key = "key_65656566"
        self.assertEqual(
            register_context_principal(
                self.conn,
                principal_id=second_file_principal,
                key_id=second_file_key,
                principal_kind="file_adapter",
                verification_secret=second_file_secret,
                project_ref=self.project,
            )["status"],
            "registered",
        )
        one = self._controlled_payload(
            principal_name="file",
            evidence_kind="file_saved",
            state="artifact_saved",
            idempotency_key="idem_65000007",
            source_ref="source_65000007",
            memory_eligible=False,
        )
        one_result = ingest_controlled_evidence(
            self.conn,
            self._wire(one),
            verification_secret=self.secrets["file"],
            now=self.now,
        )
        two = copy.deepcopy(one)
        two.update(
            {
                "idempotency_key": "idem_65000008",
                "principal_id": second_file_principal,
                "key_id": second_file_key,
                "source_ref": "source_65000008",
                "signature": "",
            }
        )
        two["signature"] = sign_contract_payload(two, second_file_secret)
        two_result = ingest_controlled_evidence(
            self.conn,
            self._wire(two),
            verification_secret=second_file_secret,
            now=self.now,
        )
        dual = self._receipt_payload(
            idempotency_key="idem_65000009",
            evidence_refs=[one_result["evidence_ref"], two_result["evidence_ref"]],
        )
        rejected = ingest_context_receipt(
            self.conn,
            self._wire(dual),
            verification_secret=self.secrets["ai"],
            now=self.now,
        )
        self.assertEqual(rejected["reason_code"], "independent_evidence_adapter_required")

    def test_memory_rejects_operational_restricted_and_third_party_contexts(self) -> None:
        operational = self._ingest_controlled(
            principal_name="file",
            evidence_kind="file_saved",
            state="artifact_saved",
            idempotency_key="idem_66000001",
            source_ref="source_66000001",
            memory_eligible=False,
        )
        self.assertEqual(operational["verified_ref"], "")
        _, corroborated = self._ingest_operational_pair(
            file_idempotency_key="idem_66000004",
            git_idempotency_key="idem_66000005",
            file_source_ref="source_66000004",
            git_source_ref="source_66000005",
        )
        self.assertEqual(
            create_memory_candidate(
                self.conn,
                verified_ref=corroborated["verified_ref"],
                subject_ref=self.owner,
                now=self.now,
            )["reason_code"],
            "memory_scope_not_eligible",
        )
        restricted = self._ingest_memory_confirmation(
            idempotency_key="idem_66000002",
            source_ref="source_66000002",
            privacy_class="restricted_local",
            memory_eligible=False,
            verification_scope="operational",
        )
        self.assertEqual(
            create_memory_candidate(
                self.conn,
                verified_ref=restricted["verified_ref"],
                subject_ref=self.owner,
                now=self.now,
            )["reason_code"],
            "restricted_local_memory_forbidden",
        )
        third_party = self._ingest_memory_confirmation(
            idempotency_key="idem_66000003",
            source_ref="source_66000003",
            subject_ref=self.third_party,
            memory_eligible=False,
            verification_scope="operational",
        )
        rejected = create_memory_candidate(
            self.conn,
            verified_ref=third_party["verified_ref"],
            subject_ref=self.third_party,
            now=self.now,
        )
        self.assertEqual(rejected["reason_code"], "third_party_subject_forbidden")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM context_memory_candidates").fetchone()[0], 0)

    def test_target_privacy_matrix_blocks_restricted_external_delivery(self) -> None:
        external_target = "target_67676767"
        registered = register_context_label(
            self.conn,
            kind="target",
            ref=external_target,
            label="External Controlled Target",
            project_ref=self.project,
            target_scope="external",
            allowed_privacy_classes=["private_local", "sensitive_local"],
            export_policy="explicit_export",
        )
        self.assertEqual(registered["status"], "registered")
        private = self._ingest_trusted_confirmation(
            idempotency_key="idem_67000001",
            source_ref="source_67000001",
        )
        self.assertEqual(private["status"], "accepted")
        external_ok = prepare_delivery(
            self.conn,
            target_ref=external_target,
            project_ref=self.project,
            now=self.now,
        )
        self.assertEqual((external_ok["status"], external_ok["target_scope"]), ("prepared", "external"))

        restricted = self._ingest_memory_confirmation(
            idempotency_key="idem_67000002",
            source_ref="source_67000002",
            privacy_class="restricted_local",
            memory_eligible=False,
            verification_scope="operational",
        )
        self.assertEqual(restricted["status"], "accepted")
        blocked = prepare_delivery(
            self.conn,
            target_ref=external_target,
            project_ref=self.project,
            now=self.now + timedelta(seconds=1),
        )
        self.assertEqual((blocked["status"], blocked["reason_code"]), ("rejected", "target_privacy_policy_forbidden"))
        local = prepare_delivery(
            self.conn,
            target_ref=self.other_target,
            project_ref=self.project,
            now=self.now + timedelta(seconds=1),
        )
        self.assertEqual(local["status"], "prepared")

    def test_principal_and_label_revoke_are_linearized_with_ingest(self) -> None:
        original_materialize = context_console_module._materialize_controlled_evidence

        def run_race(*, revoke_label: bool) -> tuple[dict, dict, str]:
            ready = threading.Event()
            release = threading.Event()
            results: dict[str, dict] = {}
            payload = self._controlled_payload(
                principal_name="file",
                evidence_kind="file_saved",
                state="artifact_saved",
                idempotency_key="idem_68000001" if not revoke_label else "idem_68000002",
                source_ref="source_68000001" if not revoke_label else "source_68000002",
                memory_eligible=False,
            )

            def blocked_materialize(*args, **kwargs):
                ready.set()
                self.assertTrue(release.wait(5))
                return original_materialize(*args, **kwargs)

            def ingest_worker() -> None:
                conn = ensure_ready(self.db)
                try:
                    results["ingest"] = ingest_controlled_evidence(
                        conn,
                        self._wire(payload),
                        verification_secret=self.secrets["file"],
                        now=self.now,
                    )
                finally:
                    conn.close()

            def revoke_worker() -> None:
                conn = ensure_ready(self.db)
                try:
                    if revoke_label:
                        results["revoke"] = revoke_context_label(conn, self.object)
                    else:
                        principal_id, key_id, _ = self.principals["file"]
                        results["revoke"] = revoke_context_principal(conn, principal_id, key_id)
                finally:
                    conn.close()

            with mock.patch.object(context_console_module, "_materialize_controlled_evidence", side_effect=blocked_materialize):
                ingest_thread = threading.Thread(target=ingest_worker)
                ingest_thread.start()
                self.assertTrue(ready.wait(5))
                revoke_thread = threading.Thread(target=revoke_worker)
                revoke_thread.start()
                time.sleep(0.05)
                self.assertNotIn("revoke", results)
                release.set()
                ingest_thread.join(10)
                revoke_thread.join(10)
                self.assertFalse(ingest_thread.is_alive())
                self.assertFalse(revoke_thread.is_alive())
            observation_ref = str(results["ingest"].get("observation_ref") or "")
            return results["ingest"], results["revoke"], observation_ref

        ingest, revoked, observation_ref = run_race(revoke_label=False)
        self.assertEqual(ingest["status"], "accepted")
        self.assertEqual(revoked["status"], "revoked")
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_observations WHERE observation_ref=?",
                (observation_ref,),
            ).fetchone()[0],
            "revoked",
        )

        # A fresh database is required because the first half revoked the file
        # adapter principal itself.
        self.conn.close()
        self.temp.cleanup()
        self.setUp()
        ingest, revoked, observation_ref = run_race(revoke_label=True)
        self.assertEqual(ingest["status"], "accepted")
        self.assertEqual(revoked["status"], "revoked")
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_observations WHERE observation_ref=?",
                (observation_ref,),
            ).fetchone()[0],
            "revoked",
        )

    def test_memory_and_bundle_reads_are_linearized_with_delete(self) -> None:
        controlled = self._ingest_memory_confirmation(
            idempotency_key="idem_69000001",
            source_ref="source_69000001",
        )
        original_validate = context_console_module._validate_verified_graph
        ready = threading.Event()
        release = threading.Event()
        results: dict[str, dict] = {}

        def blocked_validate(*args, **kwargs):
            ready.set()
            self.assertTrue(release.wait(5))
            return original_validate(*args, **kwargs)

        def memory_worker() -> None:
            conn = ensure_ready(self.db)
            try:
                results["memory"] = create_memory_candidate(
                    conn,
                    verified_ref=controlled["verified_ref"],
                    subject_ref=self.owner,
                    now=self.now,
                )
            finally:
                conn.close()

        def delete_worker() -> None:
            conn = ensure_ready(self.db)
            try:
                results["delete"] = delete_context_observation(
                    conn,
                    observation_ref=controlled["observation_ref"],
                    reason_code="user_deleted",
                    now=self.now,
                )
            finally:
                conn.close()

        with mock.patch.object(context_console_module, "_validate_verified_graph", side_effect=blocked_validate):
            reader = threading.Thread(target=memory_worker)
            reader.start()
            self.assertTrue(ready.wait(5))
            deleter = threading.Thread(target=delete_worker)
            deleter.start()
            time.sleep(0.05)
            self.assertNotIn("delete", results)
            release.set()
            reader.join(10)
            deleter.join(10)
        self.assertEqual(results["memory"]["status"], "candidate_only")
        self.assertEqual(results["delete"]["status"], "completed")
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_memory_candidates WHERE verified_ref=?",
                (controlled["verified_ref"],),
            ).fetchone()[0],
            "revoked",
        )

        # Repeat with Bundle preparation so the frozen payload cannot escape a
        # concurrent source delete as an active manifest.
        self.conn.close()
        self.temp.cleanup()
        self.setUp()
        controlled = self._ingest_memory_confirmation(
            idempotency_key="idem_69000002",
            source_ref="source_69000002",
        )
        ready = threading.Event()
        release = threading.Event()
        results = {}

        def blocked_validate_bundle(*args, **kwargs):
            ready.set()
            self.assertTrue(release.wait(5))
            return original_validate(*args, **kwargs)

        def bundle_worker() -> None:
            conn = ensure_ready(self.db)
            try:
                results["bundle"] = prepare_delivery(
                    conn,
                    target_ref=self.target,
                    project_ref=self.project,
                    now=self.now,
                )
            finally:
                conn.close()

        def bundle_delete_worker() -> None:
            conn = ensure_ready(self.db)
            try:
                results["delete"] = delete_context_observation(
                    conn,
                    observation_ref=controlled["observation_ref"],
                    reason_code="user_deleted",
                    now=self.now,
                )
            finally:
                conn.close()

        with mock.patch.object(
            context_console_module,
            "_validate_verified_graph",
            side_effect=blocked_validate_bundle,
        ):
            reader = threading.Thread(target=bundle_worker)
            reader.start()
            self.assertTrue(ready.wait(5))
            deleter = threading.Thread(target=bundle_delete_worker)
            deleter.start()
            time.sleep(0.05)
            self.assertNotIn("delete", results)
            release.set()
            reader.join(10)
            deleter.join(10)
        self.assertEqual(results["bundle"]["status"], "prepared")
        self.assertEqual(results["delete"]["status"], "completed")
        fetched = fetch_prepared_delivery(
            self.conn,
            delivery_ref=results["bundle"]["delivery_ref"],
            now=self.now,
        )
        self.assertEqual((fetched["status"], fetched["reason_code"]), ("rejected", "bundle_restricted"))

    def test_expiry_and_label_revoke_apply_complete_dependency_closure_with_withdrawal(self) -> None:
        controlled = self._ingest_memory_confirmation(
            idempotency_key="idem_70000001",
            source_ref="source_70000001",
        )
        memory = create_memory_candidate(
            self.conn,
            verified_ref=controlled["verified_ref"],
            subject_ref=self.owner,
            now=self.now,
        )
        self.assertEqual(memory["status"], "candidate_only")
        prepared = prepare_delivery(
            self.conn,
            target_ref=self.target,
            project_ref=self.project,
            now=self.now,
        )
        consumed_payload = self._delivery_payload(
            principal_name="consumer",
            delivery_ref=prepared["delivery_ref"],
            bundle_hash=prepared["bundle_hash"],
            transition="consumed",
            idempotency_key="idem_70000002",
            provider_receipt_ref="provider_70000002",
        )
        self.assertEqual(
            ingest_delivery_receipt(
                self.conn,
                self._wire(consumed_payload),
                verification_secret=self.secrets["consumer"],
                now=self.now,
            )["state"],
            "consumed",
        )
        delivered_payload = self._delivery_payload(
            principal_name="provider",
            delivery_ref=prepared["delivery_ref"],
            bundle_hash=prepared["bundle_hash"],
            transition="delivered",
            idempotency_key="idem_70000003",
            provider_receipt_ref="provider_70000003",
        )
        self.assertEqual(
            ingest_delivery_receipt(
                self.conn,
                self._wire(delivered_payload),
                verification_secret=self.secrets["provider"],
                now=self.now,
            )["state"],
            "delivered",
        )
        revoked = revoke_context_label(self.conn, self.object)
        self.assertEqual(revoked["status"], "revoked")
        self.assertGreaterEqual(revoked["cascade"]["withdrawal_receipts_created"], 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_withdrawal_receipts WHERE delivery_ref=?",
                (prepared["delivery_ref"],),
            ).fetchone()[0],
            "local_restriction_recorded",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT state FROM context_deliveries WHERE delivery_ref=?",
                (prepared["delivery_ref"],),
            ).fetchone()[0],
            "delivered",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM context_search_index WHERE verified_ref=?",
                (controlled["verified_ref"],),
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_memory_candidates WHERE memory_candidate_ref=?",
                (memory["memory_candidate_ref"],),
            ).fetchone()[0],
            "revoked",
        )

        self.conn.close()
        self.temp.cleanup()
        self.setUp()
        expiring = self._ingest_memory_confirmation(
            idempotency_key="idem_70000004",
            source_ref="source_70000004",
        )
        self.conn.execute(
            "UPDATE context_evidence SET expires_at=? WHERE evidence_ref=?",
            ((self.now - timedelta(seconds=1)).isoformat(timespec="seconds"), expiring["evidence_ref"]),
        )
        self.conn.commit()
        cleaned = cleanup_context_retention(self.conn, now=self.now)
        self.assertEqual(cleaned["status"], "completed")
        self.assertGreaterEqual(cleaned["evidence_expired"], 1)
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_verified WHERE verified_ref=?",
                (expiring["verified_ref"],),
            ).fetchone()[0],
            "revoked",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM context_source_event_tombstones WHERE observation_ref=?",
                (expiring["observation_ref"],),
            ).fetchone()[0],
            1,
        )

    def test_component_baselines_feed_soak_report_and_increment_concurrently(self) -> None:
        capture = record_component_baseline(
            self.conn,
            component="capture",
            version_ref="version_71000001",
            reset_reason_code="capture_changed",
        )
        privacy = record_component_baseline(
            self.conn,
            component="privacy",
            version_ref="version_71000002",
            reset_reason_code="privacy_changed",
        )
        self.assertEqual((capture["generation"], privacy["generation"]), (1, 1))
        self.conn.execute(
            "UPDATE context_component_baselines SET recorded_at=? WHERE component='capture'",
            ((self.now - timedelta(days=3)).isoformat(timespec="seconds"),),
        )
        self.conn.execute(
            "UPDATE context_component_baselines SET recorded_at=? WHERE component='privacy'",
            ((self.now - timedelta(days=1)).isoformat(timespec="seconds"),),
        )
        self.conn.commit()
        report = build_soak_report(
            self.conn,
            self.root,
            days=7,
            end_at=self.now.isoformat(timespec="seconds"),
        )
        windows = report["window"]["component_windows"]
        self.assertEqual(windows["capture"]["generation"], 1)
        self.assertEqual(windows["privacy"]["generation"], 1)
        self.assertEqual(windows["storage"]["generation"], 0)
        self.assertGreater(windows["capture"]["elapsed_hours"], windows["privacy"]["elapsed_hours"])

        barrier = threading.Barrier(3)
        results: list[dict] = []

        def worker(version_ref: str) -> None:
            conn = ensure_ready(self.db)
            try:
                barrier.wait(5)
                results.append(
                    record_component_baseline(
                        conn,
                        component="storage",
                        version_ref=version_ref,
                        reset_reason_code="storage_changed",
                    )
                )
            finally:
                conn.close()

        threads = [
            threading.Thread(target=worker, args=("version_71000003",)),
            threading.Thread(target=worker, args=("version_71000004",)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait(5)
        for thread in threads:
            thread.join(10)
        self.assertEqual(sorted(result["generation"] for result in results), [1, 2])
        self.assertEqual(
            self.conn.execute(
                "SELECT generation FROM context_component_baselines WHERE component='storage'"
            ).fetchone()[0],
            2,
        )
        self.assertEqual(classify_stability_change("copy")["affected_components"], [])
        self.assertEqual(classify_stability_change("ui")["affected_components"], [])

    def test_product_experiment_checkpoints_do_not_invent_missing_ratings_or_time(self) -> None:
        self.assertTrue(
            start_product_experiment(
                self.conn,
                experiment_ref="experiment_72000001",
                confirmation="START_LOCAL_PRODUCT_EXPERIMENT",
                now=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
            )["experiment_started"]
        )
        for index in range(1, 10):
            record_product_experiment_attempt(
                self.conn,
                experiment_ref="experiment_72000001",
                local_date=f"2026-07-{index:02d}",
                source_ref=f"source_72{index:06d}",
                outcome=None if index == 9 else ("useful" if index <= 5 else "not_useful"),
                resume_seconds=None if index == 8 else float(index),
            )
        day14 = product_experiment_status(
            self.conn,
            experiment_ref="experiment_72000001",
            local_today=date(2026, 7, 14),
        )
        self.assertTrue(day14["checkpoints"]["day_14_reached"])
        self.assertTrue(day14["checkpoints"]["attempt_9_reached"])
        self.assertFalse(day14["checkpoints"]["attempt_10_reached"])
        self.assertEqual((day14["attempt_count"], day14["rated_attempt_count"]), (9, 8))
        self.assertEqual(day14["review_readiness"], "pending")
        self.assertEqual(day14["status"], "incomplete")
        self.assertIsNone(day14["useful_rate"])
        self.assertIsNone(day14["median_resume_seconds"])

        record_product_experiment_attempt(
            self.conn,
            experiment_ref="experiment_72000001",
            local_date="2026-07-10",
            source_ref="source_72000010",
            outcome=None,
            resume_seconds=None,
        )
        day21 = product_experiment_status(
            self.conn,
            experiment_ref="experiment_72000001",
            local_today=date(2026, 7, 21),
        )
        self.assertTrue(day21["checkpoints"]["day_21_reached"])
        self.assertTrue(day21["checkpoints"]["attempt_10_reached"])
        self.assertEqual(day21["completion_readiness"], "pending")
        self.assertEqual(day21["status"], "incomplete")
        self.assertEqual(day21["rated_attempt_count"], 8)

        # The next case is an independent fixture. Production permits one
        # running natural-day experiment at a time.
        self.conn.execute(
            "DELETE FROM context_product_experiment_attempts WHERE experiment_ref=?",
            ("experiment_72000001",),
        )
        self.conn.execute(
            "DELETE FROM context_product_experiments WHERE experiment_ref=?",
            ("experiment_72000001",),
        )
        self.conn.commit()

        empty = start_product_experiment(
            self.conn,
            experiment_ref="experiment_72000002",
            confirmation="START_LOCAL_PRODUCT_EXPERIMENT",
            now=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
        )
        self.assertTrue(empty["experiment_started"])
        for index in range(1, 11):
            record_product_experiment_attempt(
                self.conn,
                experiment_ref="experiment_72000002",
                local_date=f"2026-07-{index:02d}",
                source_ref=f"source_73{index:06d}",
                outcome=None,
                resume_seconds=None,
            )
        missing = product_experiment_status(
            self.conn,
            experiment_ref="experiment_72000002",
            local_today=date(2026, 7, 21),
        )
        self.assertEqual(missing["review_readiness"], "pending")
        self.assertEqual(missing["completion_readiness"], "pending")
        self.assertIsNone(missing["useful_rate"])
        self.assertIsNone(missing["median_resume_seconds"])
        self.assertEqual(missing["status"], "incomplete")

    def test_document_change_adapter_projects_operational_context_without_paths_or_memory(self) -> None:
        watch = self.root / "watch"
        watch.mkdir()
        configure_document_changes(
            self.conn,
            add_root=watch,
            root_label="Watch",
            runtime_root=self.root,
        )
        root_id = str(self.conn.execute("SELECT root_id FROM file_watch_roots").fetchone()[0])
        mapping = register_document_context_mapping(
            self.conn,
            mapping_ref="mapping_74000001",
            mapping_version=1,
            root_id=root_id,
            project_ref=self.project,
            workstream_ref=self.workstream,
            object_ref=self.object,
            version_ref=self.version,
            subject_ref=self.owner,
            now=self.now,
        )
        self.assertEqual(mapping["status"], "registered")
        document = watch / "private-name.md"
        document.write_text("initial private document body", encoding="utf-8")
        baseline = build_document_baseline(self.conn, watch, root_label="Watch")
        self.assertEqual(baseline["baseline_status"], "complete")
        document.write_text("private document body must not enter Context Console", encoding="utf-8")
        result = process_document_event(
            self.conn,
            {
                "path": str(document),
                "event_id": "1",
                "observed_at": self.now.isoformat(timespec="seconds"),
                "flags": ["item_modified"],
            },
            root_path=watch,
            root_label="Watch",
        )
        self.assertTrue(result["recorded"])
        row = self.conn.execute(
            "SELECT o.observation_ref,o.source_kind,o.trust_partition,o.status,e.evidence_ref,e.state,"
            "c.candidate_ref,c.disposition,c.status AS candidate_status FROM context_observations o "
            "JOIN context_evidence e ON e.observation_ref=o.observation_ref "
            "JOIN context_candidates c ON c.observation_ref=o.observation_ref "
            "WHERE o.source_kind='file_adapter'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual((row["source_kind"], row["trust_partition"], row["status"]), (
            "file_adapter", "owner_local_unverified", "active"
        ))
        self.assertEqual((row["state"], row["disposition"], row["candidate_status"]), (
            "artifact_saved", "requires_confirmation", "active"
        ))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM context_verified").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM context_memory_candidates").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM context_search_index").fetchone()[0], 0)
        context_text = []
        for table in (
            "context_label_registry",
            "context_principals",
            "context_document_mappings",
            "context_document_source_versions",
            "context_observations",
            "context_evidence",
            "context_candidates",
            "context_verified",
            "context_source_events",
        ):
            rows = self.conn.execute(f"SELECT * FROM {table}").fetchall()
            context_text.append(json.dumps([dict(item) for item in rows], ensure_ascii=False, default=str))
        encoded = "\n".join(context_text)
        self.assertNotIn(str(document), encoded)
        self.assertNotIn(document.name, encoded)
        self.assertNotIn("private document body", encoded)

    def test_schema_28_and_30_unsafe_screen_confirmations_are_downgraded(self) -> None:
        for version, state in ((28, "evidence_confirmed"), (30, "confirmed")):
            path = self.root / f"unsafe-confirmed-{version}.sqlite"
            conn = ensure_ready(path)
            conn.execute(
                "INSERT INTO activity_confirmation_items(item_key,event_type,safe_observation,decision_question,why_uncertain,"
                "observed_start,observed_end,fingerprint,calibration_key,state,answer,candidate_origin,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"confirmation_{version}",
                    "observed_activity",
                    "screen observation only",
                    "Did this happen?",
                    "screen only",
                    self.now.isoformat(timespec="seconds"),
                    self.now.isoformat(timespec="seconds"),
                    f"fingerprint_{version}",
                    f"calibration_{version}",
                    state,
                    None,
                    "explicit_claim",
                    self.now.isoformat(timespec="seconds"),
                    self.now.isoformat(timespec="seconds"),
                ),
            )
            conn.execute(
                "INSERT OR REPLACE INTO activity_confirmation_settlements("
                "report_date,revision,settled_at,completeness_status,pending_total,pending_high_priority,"
                "pending_by_kind,candidate_funnel,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    "2026-07-29",
                    "revision-1",
                    self.now.isoformat(timespec="seconds"),
                    "complete",
                    0,
                    0,
                    "{}",
                    "{}",
                    self.now.isoformat(timespec="seconds"),
                    self.now.isoformat(timespec="seconds"),
                ),
            )
            if version == 28:
                conn.execute("DROP TRIGGER IF EXISTS pam_schema32_guard_update")
                conn.execute("DROP TRIGGER IF EXISTS pam_schema32_guard_insert")
                conn.execute("DROP TRIGGER IF EXISTS pam_schema31_guard_update")
                conn.execute("DROP TRIGGER IF EXISTS pam_schema31_guard_insert")
                conn.execute("DROP TRIGGER IF EXISTS pam_schema30_guard_update")
                conn.execute("DROP TRIGGER IF EXISTS pam_schema30_guard_insert")
                conn.execute("UPDATE meta SET value='28' WHERE key='schema_version'")
            else:
                conn.execute("DROP TRIGGER IF EXISTS pam_schema32_guard_update")
                conn.execute("DROP TRIGGER IF EXISTS pam_schema32_guard_insert")
                conn.execute("DROP TRIGGER IF EXISTS pam_schema31_guard_update")
                conn.execute("DROP TRIGGER IF EXISTS pam_schema31_guard_insert")
                conn.execute("UPDATE meta SET value='30' WHERE key='schema_version'")
            conn.commit()
            conn.close()
            reopened = ensure_ready(path)
            try:
                migrated = reopened.execute(
                    "SELECT state,notification_eligible FROM activity_confirmation_items WHERE item_key=?",
                    (f"confirmation_{version}",),
                ).fetchone()
                expected_state = "pending" if version == 28 else "invalidated"
                self.assertEqual((migrated["state"], migrated["notification_eligible"]), (expected_state, 0))
                self.assertEqual(reopened.execute("SELECT COUNT(*) FROM activity_confirmation_settlements").fetchone()[0], 0)
            finally:
                reopened.close()

    def test_group_e_v2_metrics_upgrade_backfills_event_fields_and_local_win_boundary(self) -> None:
        path = self.root / "group-e-v2-metrics.sqlite"
        legacy = ensure_ready(path)
        legacy.execute("DROP TABLE context_product_experiment_attempts")
        legacy.execute("DROP TABLE context_product_experiments")
        legacy.execute("DROP TABLE context_product_wins")
        legacy.executescript(
            """
            CREATE TABLE context_product_experiments (
              experiment_ref TEXT PRIMARY KEY,
              status TEXT NOT NULL,
              started_local_date TEXT NOT NULL,
              ended_local_date TEXT,
              minimum_days INTEGER NOT NULL DEFAULT 14,
              maximum_days INTEGER NOT NULL DEFAULT 21,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE context_product_experiment_attempts (
              attempt_ref TEXT PRIMARY KEY,
              experiment_ref TEXT NOT NULL,
              local_date TEXT NOT NULL,
              outcome TEXT,
              resume_seconds REAL,
              source_ref TEXT NOT NULL,
              created_at TEXT NOT NULL,
              FOREIGN KEY(experiment_ref) REFERENCES context_product_experiments(experiment_ref),
              UNIQUE(experiment_ref, source_ref)
            );
            CREATE TABLE context_product_wins (
              win_ref TEXT PRIMARY KEY,
              occurred_at TEXT NOT NULL,
              project_ref TEXT NOT NULL,
              outcome_code TEXT NOT NULL,
              minutes_saved INTEGER NOT NULL DEFAULT 0,
              source_ref TEXT NOT NULL,
              created_at TEXT NOT NULL,
              expires_at TEXT NOT NULL
            );
            """
        )
        occurred_local = datetime(2026, 3, 1, 12, 0, tzinfo=ZoneInfo("America/New_York"))
        occurred_utc = occurred_local.astimezone(UTC)
        stamp = occurred_utc.isoformat(timespec="seconds")
        legacy.execute(
            "UPDATE settings SET value='America/New_York' WHERE key='context_local_timezone'"
        )
        legacy.execute(
            "INSERT INTO context_product_experiments VALUES(?,?,?,?,?,?,?,?)",
            ("experiment_ef000001", "running", "2026-03-01", None, 14, 21, stamp, stamp),
        )
        legacy.execute(
            "INSERT INTO context_product_experiment_attempts VALUES(?,?,?,?,?,?,?)",
            (
                "attempt_ef000001",
                "experiment_ef000001",
                "2026-03-01",
                "not_useful",
                31.0,
                "source_ef000001",
                stamp,
            ),
        )
        legacy.execute(
            "INSERT INTO context_product_wins VALUES(?,?,?,?,?,?,?,?)",
            (
                "win_ef000001",
                stamp,
                "project_ef000001",
                "handoff_reused",
                4,
                "source_ef000002",
                stamp,
                (occurred_utc + timedelta(days=30)).isoformat(timespec="seconds"),
            ),
        )
        legacy.commit()
        legacy.close()

        migrated = ensure_ready(path)
        try:
            experiment = migrated.execute(
                "SELECT timezone_name FROM context_product_experiments WHERE experiment_ref='experiment_ef000001'"
            ).fetchone()
            attempt = migrated.execute(
                "SELECT event_id,useful,outcome,resume_seconds FROM context_product_experiment_attempts "
                "WHERE attempt_ref='attempt_ef000001'"
            ).fetchone()
            win = migrated.execute(
                "SELECT local_date,expires_local_date,timezone_name,expires_at FROM context_product_wins "
                "WHERE win_ref='win_ef000001'"
            ).fetchone()
            self.assertEqual(experiment["timezone_name"], "America/New_York")
            self.assertEqual(tuple(attempt), ("source_ef000001", 0, "not_useful", 31.0))
            self.assertEqual(
                (win["local_date"], win["expires_local_date"], win["timezone_name"]),
                ("2026-03-01", "2026-03-31", "America/New_York"),
            )
            self.assertEqual(
                datetime.fromisoformat(win["expires_at"]),
                datetime(2026, 3, 31, 0, 0, tzinfo=ZoneInfo("America/New_York")).astimezone(UTC),
            )
        finally:
            migrated.close()

    def test_schema_28_and_shadow_29_migrate_additively_to_31_with_backups(self) -> None:
        for old_version in (28, 29):
            path = self.root / f"schema{old_version}.sqlite"
            original = ensure_ready(path)
            # Reconstruct the pre-context source shape. The schema-29 case also
            # carries an opaque v0.4 marker that schema 31 must preserve without interpretation.
            original.execute("DROP TRIGGER pam_schema32_guard_insert")
            original.execute("DROP TRIGGER pam_schema32_guard_update")
            original.execute("DROP TRIGGER pam_schema31_guard_insert")
            original.execute("DROP TRIGGER pam_schema31_guard_update")
            original.execute("DROP TRIGGER pam_schema30_guard_insert")
            original.execute("DROP TRIGGER pam_schema30_guard_update")
            ordinary = [
                row[0]
                for row in original.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'context_%' "
                    "AND name NOT LIKE 'context_search_index_%'"
                ).fetchall()
            ]
            if "context_search_index" in ordinary:
                original.execute("DROP TABLE context_search_index")
                ordinary.remove("context_search_index")
            for table in ordinary:
                original.execute(f'DROP TABLE "{table}"')
            if old_version == 29:
                # Use the actual v0.4 shadow schema rather than a marker-only
                # approximation.  Schema 30 must preserve these opaque rows
                # without interpreting, rebuilding, or enqueueing them.
                original.executescript(
                    """
                    CREATE TABLE task_episodes (
                      episode_id TEXT PRIMARY KEY,
                      explicit_task_id TEXT NOT NULL DEFAULT '',
                      workstream TEXT NOT NULL DEFAULT '',
                      subject TEXT NOT NULL DEFAULT '',
                      object TEXT NOT NULL DEFAULT '',
                      version TEXT NOT NULL DEFAULT '',
                      current_state TEXT NOT NULL DEFAULT 'intent_observed',
                      execution_status TEXT NOT NULL DEFAULT 'status_unknown',
                      execution_status_updated_at TEXT NOT NULL DEFAULT '',
                      root_turn_ref TEXT NOT NULL DEFAULT '',
                      project_ref TEXT NOT NULL DEFAULT '',
                      binding_mode TEXT NOT NULL DEFAULT '',
                      highest_source_tier TEXT NOT NULL DEFAULT 'P5',
                      confirmation_state TEXT NOT NULL DEFAULT 'pending',
                      privacy_class TEXT NOT NULL DEFAULT 'unknown',
                      memory_write_eligible INTEGER NOT NULL DEFAULT 0,
                      evidence_ids TEXT NOT NULL DEFAULT '[]',
                      confirmation_item_key TEXT NOT NULL DEFAULT '',
                      started_at TEXT NOT NULL,
                      ended_at TEXT NOT NULL,
                      event_count INTEGER NOT NULL DEFAULT 0,
                      created_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL
                    );
                    CREATE TABLE task_evidence_events (
                      event_id TEXT PRIMARY KEY,
                      episode_id TEXT,
                      task_id TEXT NOT NULL DEFAULT '',
                      occurred_at TEXT NOT NULL,
                      source_kind TEXT NOT NULL,
                      source_tier TEXT NOT NULL,
                      workstream TEXT NOT NULL DEFAULT '',
                      subject TEXT NOT NULL DEFAULT '',
                      action TEXT NOT NULL DEFAULT '',
                      object TEXT NOT NULL DEFAULT '',
                      version TEXT NOT NULL DEFAULT '',
                      state_claim TEXT NOT NULL,
                      execution_status TEXT NOT NULL DEFAULT 'status_unknown',
                      session_ref TEXT NOT NULL DEFAULT '',
                      turn_ref TEXT NOT NULL DEFAULT '',
                      root_turn_ref TEXT NOT NULL DEFAULT '',
                      project_ref TEXT NOT NULL DEFAULT '',
                      binding_mode TEXT NOT NULL DEFAULT '',
                      evidence_ids TEXT NOT NULL DEFAULT '[]',
                      confidence REAL NOT NULL DEFAULT 0.0,
                      confirmation_state TEXT NOT NULL DEFAULT 'pending',
                      privacy_class TEXT NOT NULL DEFAULT 'unknown',
                      memory_write_eligible INTEGER NOT NULL DEFAULT 0,
                      contract_version INTEGER NOT NULL DEFAULT 1,
                      source_ref TEXT NOT NULL DEFAULT '',
                      event_fingerprint TEXT NOT NULL,
                      created_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL,
                      FOREIGN KEY(episode_id) REFERENCES task_episodes(episode_id) ON DELETE CASCADE
                    );
                    CREATE TABLE hermes_outbox (
                      outbox_id TEXT PRIMARY KEY,
                      episode_id TEXT,
                      event_id TEXT,
                      payload_kind TEXT NOT NULL,
                      idempotency_key TEXT NOT NULL UNIQUE,
                      payload_json TEXT NOT NULL,
                      status TEXT NOT NULL DEFAULT 'pending',
                      attempt_count INTEGER NOT NULL DEFAULT 0,
                      next_attempt_at TEXT,
                      last_error TEXT NOT NULL DEFAULT '',
                      created_at TEXT NOT NULL,
                      updated_at TEXT NOT NULL,
                      delivered_at TEXT,
                      FOREIGN KEY(episode_id) REFERENCES task_episodes(episode_id) ON DELETE CASCADE,
                      FOREIGN KEY(event_id) REFERENCES task_evidence_events(event_id) ON DELETE CASCADE
                    );
                    CREATE TABLE hermes_delivery_receipts (
                      receipt_id TEXT PRIMARY KEY,
                      outbox_id TEXT NOT NULL,
                      idempotency_key TEXT NOT NULL UNIQUE,
                      delivery_status TEXT NOT NULL,
                      provider_receipt_id TEXT NOT NULL DEFAULT '',
                      attempt_number INTEGER NOT NULL DEFAULT 1,
                      acknowledged_at TEXT NOT NULL,
                      safe_metadata TEXT NOT NULL DEFAULT '{}',
                      created_at TEXT NOT NULL,
                      FOREIGN KEY(outbox_id) REFERENCES hermes_outbox(outbox_id) ON DELETE CASCADE
                    );
                    """
                )
                stamp = self.now.isoformat(timespec="seconds")
                original.execute(
                    "INSERT INTO task_episodes(episode_id,workstream,subject,object,version,current_state,"
                    "execution_status,highest_source_tier,confirmation_state,privacy_class,memory_write_eligible,"
                    "evidence_ids,started_at,ended_at,event_count,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "episode_shadow_29",
                        "shadow workstream",
                        "self",
                        "shadow object",
                        "shadow version",
                        "artifact_saved",
                        "status_unknown",
                        "P1",
                        "confirmed",
                        "private_local",
                        0,
                        '["evidence_shadow_29"]',
                        stamp,
                        stamp,
                        1,
                        stamp,
                        stamp,
                    ),
                )
                original.execute(
                    "INSERT INTO task_evidence_events(event_id,episode_id,occurred_at,source_kind,source_tier,"
                    "state_claim,evidence_ids,event_fingerprint,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        "event_shadow_29",
                        "episode_shadow_29",
                        stamp,
                        "file",
                        "P1",
                        "artifact_saved",
                        '["evidence_shadow_29"]',
                        "fingerprint_shadow_29",
                        stamp,
                        stamp,
                    ),
                )
                original.execute(
                    "INSERT INTO hermes_outbox(outbox_id,episode_id,event_id,payload_kind,idempotency_key,payload_json,"
                    "status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        "outbox_shadow_29",
                        "episode_shadow_29",
                        "event_shadow_29",
                        "task_episode",
                        "idem_shadow_29",
                        '{"opaque":true}',
                        "pending",
                        stamp,
                        stamp,
                    ),
                )
                original.execute(
                    "INSERT INTO hermes_delivery_receipts(receipt_id,outbox_id,idempotency_key,delivery_status,"
                    "provider_receipt_id,attempt_number,acknowledged_at,safe_metadata,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        "receipt_shadow_29",
                        "outbox_shadow_29",
                        "delivery_idem_shadow_29",
                        "acknowledged",
                        "provider_shadow_29",
                        1,
                        stamp,
                        '{}',
                        stamp,
                    ),
                )
            original.execute(
                "UPDATE settings SET value='true' WHERE key IN "
                "('context_memory_writer_enabled','context_source_revocation_cascade_ready','ha_auto_package_enabled')"
            )
            original.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(old_version),))
            original.commit()
            original.close()

            migrated = ensure_ready(path)
            try:
                self.assertEqual(migrated.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "32")
                self.assertIsNotNone(
                    migrated.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_verified'"
                    ).fetchone()
                )
                if old_version == 29:
                    self.assertEqual(
                        tuple(
                            migrated.execute(
                                "SELECT episode_id,evidence_ids FROM task_episodes"
                            ).fetchone()
                        ),
                        ("episode_shadow_29", '["evidence_shadow_29"]'),
                    )
                    self.assertEqual(
                        tuple(migrated.execute("SELECT status,payload_json FROM hermes_outbox").fetchone()),
                        ("pending", '{"opaque":true}'),
                    )
                migrated_settings = dict(
                    migrated.execute(
                        "SELECT key,value FROM settings WHERE key IN "
                        "('context_memory_writer_enabled','context_source_revocation_cascade_ready','ha_auto_package_enabled')"
                    ).fetchall()
                )
                self.assertEqual(set(migrated_settings.values()), {"false"})
            finally:
                migrated.close()
            backup = path.with_name(f"{path.name}.schema{old_version}-context-backup")
            self.assertTrue(backup.is_file())
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            check = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
            try:
                self.assertEqual(check.execute("PRAGMA quick_check").fetchone()[0], "ok")
                self.assertEqual(check.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], str(old_version))
                if old_version == 29:
                    self.assertEqual(
                        check.execute(
                            "SELECT episode_id,evidence_ids FROM task_episodes"
                        ).fetchone(),
                        ("episode_shadow_29", '["evidence_shadow_29"]'),
                    )
                    self.assertEqual(
                        check.execute("SELECT delivery_status FROM hermes_delivery_receipts").fetchone()[0],
                        "acknowledged",
                    )
                self.assertIsNone(
                    check.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_verified'"
                    ).fetchone()
                )
            finally:
                check.close()

    def test_context_backup_reuse_is_bound_to_database_identity_and_content(self) -> None:
        path = self.root / "stale-backup.sqlite"
        conn = ensure_ready(path)
        conn.execute("DROP TRIGGER pam_schema32_guard_insert")
        conn.execute("DROP TRIGGER pam_schema32_guard_update")
        conn.execute("DROP TRIGGER pam_schema31_guard_insert")
        conn.execute("DROP TRIGGER pam_schema31_guard_update")
        conn.execute("DROP TRIGGER pam_schema30_guard_insert")
        conn.execute("DROP TRIGGER pam_schema30_guard_update")
        conn.execute("UPDATE meta SET value='28' WHERE key='schema_version'")
        conn.commit()
        storage_module._backup_context_schema_before_upgrade(conn, path)
        backup = path.with_name(f"{path.name}.schema28-context-backup")
        identity = backup.with_name(f"{backup.name}.identity.json")
        self.assertTrue(backup.is_file())
        self.assertTrue(identity.is_file())
        conn.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES('backup_identity_probe','changed',?)",
            (self.now.isoformat(timespec="seconds"),),
        )
        conn.commit()
        with self.assertRaisesRegex(RuntimeError, "schema_28_context_backup_stale"):
            storage_module._backup_context_schema_before_upgrade(conn, path)
        conn.close()

    def test_current_schema_guard_refuses_relabel_and_fresh_database_has_context_tables(self) -> None:
        self.assertEqual(self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "32")
        tables = {
            row[0]
            for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        self.assertTrue(
            {
                "context_observations",
                "context_evidence",
                "context_candidates",
                "context_verified",
                "context_bundle_manifests",
                "context_bundle_members",
                "context_product_wins",
                "context_activity_confirmation_links",
                "context_product_feedback",
            }
            <= tables
        )
        with self.assertRaisesRegex(sqlite3.DatabaseError, "schema 32 requires restoring"):
            self.conn.execute("UPDATE meta SET value='30' WHERE key='schema_version'")
        self.assertEqual(self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "32")

        # Startup is a hard governance boundary: the current schema and repeated opens
        # may never preserve a locally toggled HA/Memory writer gate.
        self.conn.execute(
            "UPDATE settings SET value='true' WHERE key IN "
            "('ha_auto_package_enabled','context_memory_writer_enabled','context_source_revocation_cascade_ready')"
        )
        self.conn.commit()
        self.conn.close()
        self.conn = ensure_ready(self.db)
        values = dict(
            self.conn.execute(
                "SELECT key,value FROM settings WHERE key IN "
                "('ha_auto_package_enabled','context_memory_writer_enabled','context_source_revocation_cascade_ready')"
            ).fetchall()
        )
        self.assertEqual(set(values.values()), {"false"})
        self.conn.close()
        self.conn = ensure_ready(self.db)
        values = dict(
            self.conn.execute(
                "SELECT key,value FROM settings WHERE key IN "
                "('ha_auto_package_enabled','context_memory_writer_enabled','context_source_revocation_cascade_ready')"
            ).fetchall()
        )
        self.assertEqual(set(values.values()), {"false"})

    def test_contract_json_rejects_bom_bidi_cf_and_zero_width_controls(self) -> None:
        before = self._table_counts()
        valid = self._wire(self._receipt_payload(idempotency_key="idem_70000001", source_ref="source_70000001"))
        rejected = ingest_context_receipt(
            self.conn,
            b"\xef\xbb\xbf" + valid,
            verification_secret=self.secrets["ai"],
            now=self.now,
        )
        self.assertEqual((rejected["status"], rejected["reason_code"]), ("rejected", "unicode_control_forbidden"))
        self.assertEqual(self._table_counts(), before)

        prohibited = [
            "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
            "\u2066", "\u2067", "\u2068", "\u2069",
            "\u200b", "\u200c", "\u200d", "\ufeff",
        ]
        for index, character in enumerate(prohibited, start=1):
            payload = self._receipt_payload(
                idempotency_key=f"idem_700001{index:02d}",
                source_ref=f"source_700001{index:02d}",
            )
            payload["source_ref"] = f"source_bad{character}value"
            payload["signature"] = sign_contract_payload(payload, self.secrets["ai"])
            result = ingest_context_receipt(
                self.conn,
                self._wire(payload),
                verification_secret=self.secrets["ai"],
                now=self.now,
            )
            self.assertEqual(
                (result["status"], result["reason_code"]),
                ("rejected", "unicode_control_forbidden"),
                repr(character),
            )
            label_result = register_context_label(
                self.conn,
                kind="object",
                ref=f"object_700001{index:02d}",
                label=f"Unsafe{character}Label",
                project_ref=self.project,
            )
            self.assertEqual(
                (label_result["status"], label_result["reason_code"]),
                ("rejected", "approved_label_contains_prohibited_content"),
                repr(character),
            )
            self.assertEqual(self._table_counts(), before)

        invalid_utf8 = ingest_context_receipt(
            self.conn,
            b"\xff",
            verification_secret=self.secrets["ai"],
            now=self.now,
        )
        nul = ingest_context_receipt(
            self.conn,
            valid + b"\x00",
            verification_secret=self.secrets["ai"],
            now=self.now,
        )
        self.assertEqual(invalid_utf8["reason_code"], "invalid_utf8")
        self.assertEqual(nul["reason_code"], "nul_forbidden")
        self.assertEqual(self._table_counts(), before)

    def test_bounded_huge_integer_returns_fail_closed_rejection(self) -> None:
        before = self._table_counts()
        payload = self._receipt_payload(idempotency_key="idem_70100001", source_ref="source_70100001")
        body = self._wire(payload)
        huge = b"9" * 5000
        raw = body.replace(b'"evidence_refs":[]', b'"evidence_refs":[' + huge + b']')
        self.assertLess(len(raw), MAX_CONTRACT_BYTES)
        result = ingest_context_receipt(
            self.conn,
            raw,
            verification_secret=self.secrets["ai"],
            now=self.now,
        )
        self.assertEqual((result["status"], result["reason_code"]), ("rejected", "integer_too_large"))
        self.assertEqual(self._table_counts(), before)

    def test_idempotency_is_scoped_to_contract_principal_and_source(self) -> None:
        first_payload = self._receipt_payload(
            idempotency_key="idem_70200001",
            source_ref="source_70200001",
        )
        first = ingest_context_receipt(
            self.conn,
            self._wire(first_payload),
            verification_secret=self.secrets["ai"],
            now=self.now,
        )
        self.assertEqual(first["status"], "accepted")
        first_counts = self._table_counts()

        replay = ingest_context_receipt(
            self.conn,
            self._wire(first_payload),
            verification_secret=self.secrets["ai"],
            now=self.now,
        )
        self.assertEqual(
            (replay["status"], replay["reason_code"]),
            ("replayed", "idempotent_replay"),
        )
        self.assertEqual(self._table_counts(), first_counts)

        conflicting = copy.deepcopy(first_payload)
        conflicting["lifecycle_event"] = "failed"
        conflicting["signature"] = sign_contract_payload(conflicting, self.secrets["ai"])
        conflict = ingest_context_receipt(
            self.conn,
            self._wire(conflicting),
            verification_secret=self.secrets["ai"],
            now=self.now,
        )
        self.assertEqual(
            (conflict["status"], conflict["reason_code"]),
            ("conflict", "idempotency_conflict"),
        )
        self.assertEqual(self._table_counts(), first_counts)

        other_source = copy.deepcopy(first_payload)
        other_source["source_ref"] = "source_70200002"
        other_source["signature"] = sign_contract_payload(other_source, self.secrets["ai"])
        accepted_other_source = ingest_context_receipt(
            self.conn,
            self._wire(other_source),
            verification_secret=self.secrets["ai"],
            now=self.now,
        )
        self.assertEqual(accepted_other_source["status"], "accepted")

        self._register_test_principal(
            "ai_scope_other",
            principal_id="principal_70200003",
            key_id="key_70200003",
            principal_kind="ai_lifecycle",
            secret=b"scope-other-ai-secret-01",
        )
        other_principal = self._receipt_payload(
            principal_name="ai_scope_other",
            idempotency_key="idem_70200001",
            source_ref="source_70200001",
        )
        accepted_other_principal = ingest_context_receipt(
            self.conn,
            self._wire(other_principal),
            verification_secret=self.secrets["ai_scope_other"],
            now=self.now,
        )
        self.assertEqual(accepted_other_principal["status"], "accepted")

        principal_id, _, _ = self.principals["ai"]
        rotated_key = "key_70200004"
        rotated_secret = b"rotated-ai-secret-7020"
        self.assertEqual(
            register_context_principal(
                self.conn,
                principal_id=principal_id,
                key_id=rotated_key,
                principal_kind="ai_lifecycle",
                verification_secret=rotated_secret,
                project_ref=self.project,
            )["status"],
            "registered",
        )
        rotated = copy.deepcopy(first_payload)
        rotated["key_id"] = rotated_key
        rotated["idempotency_key"] = "idem_70200004"
        rotated["signature"] = sign_contract_payload(rotated, rotated_secret)
        rotation_replay = ingest_context_receipt(
            self.conn,
            self._wire(rotated),
            verification_secret=rotated_secret,
            now=self.now,
        )
        self.assertEqual(
            (rotation_replay["status"], rotation_replay["reason_code"]),
            ("replayed", "canonical_source_event_replay"),
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM context_observations WHERE source_ref='source_70200001' AND principal_id=?",
                (principal_id,),
            ).fetchone()[0],
            1,
        )
        index_rows = self.conn.execute("PRAGMA index_list(context_ingest_receipts)").fetchall()
        unique_columns = []
        for index in index_rows:
            if int(index[2] or 0) == 1:
                unique_columns.append(
                    tuple(row[2] for row in self.conn.execute(f"PRAGMA index_info('{index[1]}')").fetchall())
                )
        self.assertIn(("contract_type", "principal_id", "source_ref", "idempotency_key"), unique_columns)

    def test_idempotency_scope_includes_stable_principal_and_source(self) -> None:
        one = self._receipt_payload(idempotency_key="idem_70210001", source_ref="source_70210001")
        two = copy.deepcopy(one)
        two["source_ref"] = "source_70210002"
        two["signature"] = sign_contract_payload(two, self.secrets["ai"])
        first = ingest_context_receipt(self.conn, self._wire(one), verification_secret=self.secrets["ai"], now=self.now)
        second = ingest_context_receipt(self.conn, self._wire(two), verification_secret=self.secrets["ai"], now=self.now)
        self.assertEqual((first["status"], second["status"]), ("accepted", "accepted"))
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM context_ingest_receipts WHERE idempotency_key='idem_70210001'"
            ).fetchone()[0],
            2,
        )

    def test_principal_revoke_committed_before_ingest_write_is_rejected(self) -> None:
        payload = self._controlled_payload(
            principal_name="file",
            evidence_kind="file_saved",
            state="artifact_saved",
            idempotency_key="idem_70300001",
            source_ref="source_70300001",
            memory_eligible=False,
        )
        before = self._table_counts()
        principal_id, key_id, _ = self.principals["file"]
        ingested, revoked = self._run_prewrite_revocation_race(
            raw_contract=self._wire(payload),
            verification_secret=self.secrets["file"],
            ingest_callable=ingest_controlled_evidence,
            revoke_callable=lambda: revoke_context_principal(self.conn, principal_id, key_id),
        )
        self.assertEqual(revoked["status"], "revoked")
        self.assertEqual((ingested["status"], ingested["reason_code"]), ("rejected", "key_revoked"))
        self.assertEqual(self._table_counts(), before)

    def test_label_revoke_committed_before_ingest_write_is_rejected(self) -> None:
        payload = self._controlled_payload(
            principal_name="file",
            evidence_kind="file_saved",
            state="artifact_saved",
            idempotency_key="idem_70300002",
            source_ref="source_70300002",
            memory_eligible=False,
        )
        before = self._table_counts()
        ingested, revoked = self._run_prewrite_revocation_race(
            raw_contract=self._wire(payload),
            verification_secret=self.secrets["file"],
            ingest_callable=ingest_controlled_evidence,
            revoke_callable=lambda: revoke_context_label(self.conn, self.object),
        )
        self.assertEqual(revoked["status"], "revoked")
        self.assertEqual((ingested["status"], ingested["reason_code"]), ("rejected", "object_unregistered"))
        self.assertEqual(self._table_counts(), before)

    def test_delivery_principal_revoke_committed_before_ack_is_rejected(self) -> None:
        prepared = self._prepare_one_delivery(idempotency_key="idem_70300003")
        payload = self._delivery_payload(
            principal_name="consumer",
            delivery_ref=prepared["delivery_ref"],
            bundle_hash=prepared["bundle_hash"],
            transition="consumed",
            idempotency_key="idem_70300004",
            provider_receipt_ref="provider_70300004",
        )
        before_receipts = self.conn.execute(
            "SELECT COUNT(*) FROM context_delivery_receipts WHERE delivery_ref=?",
            (prepared["delivery_ref"],),
        ).fetchone()[0]
        principal_id, key_id, _ = self.principals["consumer"]
        ingested, revoked = self._run_prewrite_revocation_race(
            raw_contract=self._wire(payload),
            verification_secret=self.secrets["consumer"],
            ingest_callable=ingest_delivery_receipt,
            revoke_callable=lambda: revoke_context_principal(self.conn, principal_id, key_id),
        )
        self.assertEqual(revoked["status"], "revoked")
        self.assertEqual((ingested["status"], ingested["reason_code"]), ("rejected", "key_revoked"))
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM context_delivery_receipts WHERE delivery_ref=?",
                (prepared["delivery_ref"],),
            ).fetchone()[0],
            before_receipts,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT state FROM context_deliveries WHERE delivery_ref=?",
                (prepared["delivery_ref"],),
            ).fetchone()[0],
            "prepared",
        )

    def test_deleted_source_event_cannot_reappear_with_new_idempotency_key(self) -> None:
        payload = self._receipt_payload(idempotency_key="idem_70400001", source_ref="source_70400001")
        accepted = ingest_context_receipt(
            self.conn, self._wire(payload), verification_secret=self.secrets["ai"], now=self.now
        )
        self.assertEqual(accepted["status"], "accepted")
        deleted = delete_context_observation(
            self.conn,
            observation_ref=accepted["observation_ref"],
            reason_code="user_deleted",
            now=self.now,
        )
        self.assertEqual(deleted["status"], "completed")
        counts = self._table_counts()
        replay = copy.deepcopy(payload)
        replay["idempotency_key"] = "idem_70400002"
        replay["occurred_at"] = (self.now + timedelta(seconds=1)).isoformat(timespec="seconds")
        replay["signature"] = sign_contract_payload(replay, self.secrets["ai"])
        blocked = ingest_context_receipt(
            self.conn, self._wire(replay), verification_secret=self.secrets["ai"], now=self.now
        )
        self.assertEqual((blocked["status"], blocked["reason_code"]), ("rejected", "source_event_tombstoned"))
        self.assertEqual(self._table_counts(), counts)

    def test_same_payload_replay_after_tombstone_is_rejected(self) -> None:
        payload = self._receipt_payload(idempotency_key="idem_70410001", source_ref="source_70410001")
        accepted = ingest_context_receipt(
            self.conn, self._wire(payload), verification_secret=self.secrets["ai"], now=self.now
        )
        delete_context_observation(self.conn, observation_ref=accepted["observation_ref"], now=self.now)
        counts = self._table_counts()
        replay = ingest_context_receipt(
            self.conn, self._wire(payload), verification_secret=self.secrets["ai"], now=self.now
        )
        self.assertEqual((replay["status"], replay["reason_code"]), ("rejected", "source_event_tombstoned"))
        self.assertEqual(self._table_counts(), counts)

    def test_tombstone_is_idempotent_and_survives_key_rotation(self) -> None:
        original = self._controlled_payload(
            principal_name="file",
            evidence_kind="file_saved",
            state="artifact_saved",
            idempotency_key="idem_70400003",
            source_ref="source_70400003",
            memory_eligible=False,
        )
        accepted = ingest_controlled_evidence(
            self.conn, self._wire(original), verification_secret=self.secrets["file"], now=self.now
        )
        first_delete = delete_context_observation(
            self.conn, observation_ref=accepted["observation_ref"], reason_code="user_deleted", now=self.now
        )
        second_delete = delete_context_observation(
            self.conn, observation_ref=accepted["observation_ref"], reason_code="user_deleted", now=self.now
        )
        self.assertEqual((first_delete["status"], second_delete["status"]), ("completed", "already_revoked"))
        principal_id, _, _ = self.principals["file"]
        rotated_key = "key_70400004"
        rotated_secret = b"rotated-file-secret-704"
        self.assertEqual(
            register_context_principal(
                self.conn,
                principal_id=principal_id,
                key_id=rotated_key,
                principal_kind="file_adapter",
                verification_secret=rotated_secret,
                project_ref=self.project,
            )["status"],
            "registered",
        )
        rotated = copy.deepcopy(original)
        rotated["key_id"] = rotated_key
        rotated["idempotency_key"] = "idem_70400004"
        rotated["occurred_at"] = (self.now + timedelta(seconds=1)).isoformat(timespec="seconds")
        rotated["signature"] = sign_contract_payload(rotated, rotated_secret)
        blocked = ingest_controlled_evidence(
            self.conn, self._wire(rotated), verification_secret=rotated_secret, now=self.now
        )
        self.assertEqual((blocked["status"], blocked["reason_code"]), ("rejected", "source_event_tombstoned"))
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM context_source_event_tombstones WHERE source_event_ref=?",
                (accepted["source_event_ref"],),
            ).fetchone()[0],
            1,
        )

    def test_tombstone_identity_cannot_change_with_timestamp_or_key_rotation(self) -> None:
        principal_id = "principal_70410002"
        first_key = "key_70410002"
        second_key = "key_70410003"
        first_secret = b"canonical-file-secret-1"
        second_secret = b"canonical-file-secret-2"
        self._register_test_principal(
            "canonical_file",
            principal_id=principal_id,
            key_id=first_key,
            principal_kind="file_adapter",
            secret=first_secret,
            project_ref="",
        )
        original = self._controlled_payload(
            principal_name="canonical_file",
            evidence_kind="file_saved",
            state="artifact_saved",
            idempotency_key="idem_70410002",
            source_ref="source_70410002",
            memory_eligible=False,
        )
        accepted = ingest_controlled_evidence(
            self.conn, self._wire(original), verification_secret=first_secret, now=self.now
        )
        self.assertEqual(accepted["status"], "accepted")
        delete_context_observation(self.conn, observation_ref=accepted["observation_ref"], now=self.now)

        for kind, ref, label, scope in (
            ("workstream", "workstream_70410004", "Other Workstream", ""),
            ("object", "object_70410004", "Other Object", ""),
            ("version", "version_70410004", "Other Version", ""),
            ("subject", "subject_70410004", "Other Owner", "owner"),
        ):
            result = register_context_label(
                self.conn,
                kind=kind,
                ref=ref,
                label=label,
                project_ref=self.other_project,
                subject_scope=scope,
            )
            self.assertEqual(result["status"], "registered")
        self.assertEqual(
            register_context_principal(
                self.conn,
                principal_id=principal_id,
                key_id=second_key,
                principal_kind="file_adapter",
                verification_secret=second_secret,
                project_ref="",
            )["status"],
            "registered",
        )
        changed = copy.deepcopy(original)
        changed.update(
            {
                "key_id": second_key,
                "idempotency_key": "idem_70410003",
                "occurred_at": (self.now + timedelta(seconds=1)).isoformat(timespec="seconds"),
                "project_ref": self.other_project,
                "workstream_ref": "workstream_70410004",
                "object_ref": "object_70410004",
                "version_ref": "version_70410004",
                "subject_ref": "subject_70410004",
                "privacy_class": "sensitive_local",
                "signature": "",
            }
        )
        changed["signature"] = sign_contract_payload(changed, second_secret)
        blocked = ingest_controlled_evidence(
            self.conn, self._wire(changed), verification_secret=second_secret, now=self.now
        )
        self.assertEqual((blocked["status"], blocked["reason_code"]), ("rejected", "source_event_tombstoned"))
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM context_source_events WHERE principal_id=? AND source_ref=?",
                (principal_id, "source_70410002"),
            ).fetchone()[0],
            1,
        )


    def test_single_ai_lifecycle_receipt_is_operational_candidate_not_verified(self) -> None:
        lifecycles = ("turn_started", "answer_ready", "stop", "idle", "failed")
        for index, lifecycle in enumerate(lifecycles, start=1):
            result = self._ingest_ai(
                idempotency_key=f"idem_705000{index:02d}",
                source_ref=f"source_705000{index:02d}",
                lifecycle_event=lifecycle,
            )
            self.assertEqual(result["status"], "accepted", lifecycle)
            self.assertEqual(result["verified_ref"], "", lifecycle)
            self.assertFalse(result["memory_eligible"], lifecycle)
            self.assertEqual(result["verification_scope"], "operational", lifecycle)
            observation = self.conn.execute(
                "SELECT trust_partition,status FROM context_observations WHERE observation_ref=?",
                (result["observation_ref"],),
            ).fetchone()
            candidate = self.conn.execute(
                "SELECT disposition,status FROM context_candidates WHERE candidate_ref=?",
                (result["candidate_ref"],),
            ).fetchone()
            self.assertEqual(tuple(observation), ("unverified_clue", "active"), lifecycle)
            self.assertEqual(tuple(candidate), ("candidate_only", "active"), lifecycle)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM context_verified").fetchone()[0], 0)
        bundle = build_context_bundle(self.conn, project_ref=self.project, now=self.now)
        self.assertEqual(bundle["verified_contexts"], [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM context_memory_candidates").fetchone()[0], 0)

    def test_ai_stop_and_answer_ready_are_capped_at_draft(self) -> None:
        for index, lifecycle in enumerate(("answer_ready", "stop", "idle"), start=1):
            result = self._ingest_ai(
                idempotency_key=f"idem_705100{index:02d}",
                source_ref=f"source_705100{index:02d}",
                lifecycle_event=lifecycle,
            )
            self.assertEqual(result["result_status"], "draft_generated", lifecycle)
            evidence = self.conn.execute(
                "SELECT state,state_cap FROM context_evidence WHERE evidence_ref=?",
                (result["evidence_ref"],),
            ).fetchone()
            self.assertEqual(tuple(evidence), ("draft_generated", "draft_generated"), lifecycle)
            self.assertEqual(result["verified_ref"], "", lifecycle)
        completed = self.conn.execute(
            "SELECT COUNT(*) FROM context_evidence WHERE state LIKE '%completed%' OR state_cap LIKE '%completed%'"
        ).fetchone()[0]
        self.assertEqual(completed, 0)

    def test_single_file_or_git_is_candidate_only(self) -> None:
        alternate_object = "object_70600002"
        self.assertEqual(
            register_context_label(
                self.conn,
                kind="object",
                ref=alternate_object,
                label="Independent Git Object",
                project_ref=self.project,
            )["status"],
            "registered",
        )
        file_result = self._ingest_controlled(
            principal_name="file",
            evidence_kind="file_saved",
            state="artifact_saved",
            idempotency_key="idem_70600001",
            source_ref="source_70600001",
            memory_eligible=False,
        )
        git_result = self._ingest_controlled(
            principal_name="git",
            evidence_kind="git_commit",
            state="artifact_committed",
            idempotency_key="idem_70600002",
            source_ref="source_70600002",
            object_ref=alternate_object,
            memory_eligible=False,
        )
        for result in (file_result, git_result):
            self.assertEqual(result["status"], "accepted")
            self.assertEqual(result["verified_ref"], "")
            self.assertFalse(result["memory_eligible"])
            self.assertEqual(result["verification_scope"], "operational")
            self.assertEqual(
                self.conn.execute(
                    "SELECT disposition FROM context_candidates WHERE candidate_ref=?",
                    (result["candidate_ref"],),
                ).fetchone()[0],
                "candidate_only",
            )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM context_verified").fetchone()[0], 0)
        self.assertEqual(build_context_bundle(self.conn, now=self.now)["verified_contexts"], [])

    def test_two_independent_operational_sources_can_verify_only_same_binding(self) -> None:
        first, second = self._ingest_operational_pair(
            file_idempotency_key="idem_70700001",
            git_idempotency_key="idem_70700002",
            file_source_ref="source_70700001",
            git_source_ref="source_70700002",
        )
        self.assertEqual(first["verified_ref"], "")
        self.assertTrue(second["verified_ref"].startswith("verified_"))
        verified = self.conn.execute(
            "SELECT verification_scope,memory_eligible,source_kind,state,candidate_ref "
            "FROM context_verified WHERE verified_ref=?",
            (second["verified_ref"],),
        ).fetchone()
        self.assertEqual(
            tuple(verified[:4]),
            ("operational", 0, "operational_corroborated", "artifact_committed"),
        )
        evidence = self.conn.execute(
            "SELECT e.adapter_kind,e.principal_id,p.key_fingerprint,e.source_ref,o.source_event_ref,e.provenance_ref "
            "FROM context_candidate_evidence ce JOIN context_evidence e ON e.evidence_ref=ce.evidence_ref "
            "JOIN context_observations o ON o.observation_ref=e.observation_ref "
            "JOIN context_principals p ON p.principal_id=e.principal_id AND p.key_id=e.key_id "
            "WHERE ce.candidate_ref=? ORDER BY e.evidence_ref",
            (verified["candidate_ref"],),
        ).fetchall()
        self.assertEqual(len(evidence), 2)
        for column in range(6):
            self.assertEqual(len({str(row[column]) for row in evidence}), 2)
        bundle = build_context_bundle(self.conn, project_ref=self.project, now=self.now)
        self.assertEqual([item["source_ref"] for item in bundle["verified_contexts"]], [
            self.conn.execute(
                "SELECT source_ref FROM context_verified WHERE verified_ref=?",
                (second["verified_ref"],),
            ).fetchone()[0]
        ])
        self.assertTrue(bundle["verified_contexts"][0]["context_ref"].startswith("context_"))
        self.assertNotEqual(bundle["verified_contexts"][0]["context_ref"], second["verified_ref"])
        memory = create_memory_candidate(
            self.conn,
            verified_ref=second["verified_ref"],
            subject_ref=self.owner,
            now=self.now,
        )
        self.assertEqual((memory["status"], memory["reason_code"]), ("rejected", "memory_scope_not_eligible"))

    def test_two_independent_operational_sources_promote_only_same_binding(self) -> None:
        file_result, git_result = self._ingest_operational_pair(
            file_idempotency_key="idem_70710001",
            git_idempotency_key="idem_70710002",
            file_source_ref="source_70710001",
            git_source_ref="source_70710002",
        )
        self.assertEqual(file_result["verified_ref"], "")
        self.assertNotEqual(git_result["verified_ref"], "")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM context_verified WHERE verification_scope='operational' AND memory_eligible=0"
            ).fetchone()[0],
            1,
        )

    def test_same_adapter_or_same_principal_key_is_not_independent(self) -> None:
        object_same_adapter = "object_70800001"
        object_same_principal = "object_70800002"
        object_same_fingerprint = "object_70800003"
        for ref, label in (
            (object_same_adapter, "Same Adapter Binding"),
            (object_same_principal, "Same Principal Binding"),
            (object_same_fingerprint, "Same Fingerprint Binding"),
        ):
            self.assertEqual(
                register_context_label(
                    self.conn, kind="object", ref=ref, label=label, project_ref=self.project
                )["status"],
                "registered",
            )

        self._register_test_principal(
            "file_second_adapter",
            principal_id="principal_70800011",
            key_id="key_70800011",
            principal_kind="file_adapter",
            secret=b"second-file-adapter-secret",
        )
        same_adapter_one = self._ingest_controlled(
            principal_name="file",
            evidence_kind="file_saved",
            state="artifact_saved",
            idempotency_key="idem_70800011",
            source_ref="source_70800011",
            object_ref=object_same_adapter,
            memory_eligible=False,
        )
        same_adapter_two = self._ingest_controlled(
            principal_name="file_second_adapter",
            evidence_kind="file_saved",
            state="artifact_saved",
            idempotency_key="idem_70800012",
            source_ref="source_70800012",
            object_ref=object_same_adapter,
            memory_eligible=False,
        )
        self.assertEqual((same_adapter_one["verified_ref"], same_adapter_two["verified_ref"]), ("", ""))
        rejected_adapter = self._ingest_ai(
            idempotency_key="idem_70800013",
            source_ref="source_70800013",
            object_ref=object_same_adapter,
            evidence_refs=[same_adapter_one["evidence_ref"], same_adapter_two["evidence_ref"]],
        )
        self.assertEqual(rejected_adapter["reason_code"], "independent_evidence_adapter_required")

        shared_principal = "principal_70800021"
        self._register_test_principal(
            "same_principal_file",
            principal_id=shared_principal,
            key_id="key_70800021",
            principal_kind="file_adapter",
            secret=b"same-principal-file-key",
        )
        self._register_test_principal(
            "same_principal_git",
            principal_id=shared_principal,
            key_id="key_70800022",
            principal_kind="git_adapter",
            secret=b"same-principal-git-key-",
        )
        same_principal_one = self._ingest_controlled(
            principal_name="same_principal_file",
            evidence_kind="file_saved",
            state="artifact_saved",
            idempotency_key="idem_70800021",
            source_ref="source_70800021",
            object_ref=object_same_principal,
            memory_eligible=False,
        )
        same_principal_two = self._ingest_controlled(
            principal_name="same_principal_git",
            evidence_kind="git_commit",
            state="artifact_committed",
            idempotency_key="idem_70800022",
            source_ref="source_70800022",
            object_ref=object_same_principal,
            memory_eligible=False,
        )
        self.assertEqual((same_principal_one["verified_ref"], same_principal_two["verified_ref"]), ("", ""))
        rejected_principal = self._ingest_ai(
            idempotency_key="idem_70800023",
            source_ref="source_70800023",
            object_ref=object_same_principal,
            evidence_refs=[same_principal_one["evidence_ref"], same_principal_two["evidence_ref"]],
        )
        self.assertEqual(rejected_principal["reason_code"], "independent_evidence_principal_required")

        shared_secret = b"shared-fingerprint-secret"
        self._register_test_principal(
            "same_fingerprint_file",
            principal_id="principal_70800031",
            key_id="key_70800031",
            principal_kind="file_adapter",
            secret=shared_secret,
        )
        self._register_test_principal(
            "same_fingerprint_git",
            principal_id="principal_70800032",
            key_id="key_70800032",
            principal_kind="git_adapter",
            secret=shared_secret,
        )
        same_fingerprint_one = self._ingest_controlled(
            principal_name="same_fingerprint_file",
            evidence_kind="file_saved",
            state="artifact_saved",
            idempotency_key="idem_70800031",
            source_ref="source_70800031",
            object_ref=object_same_fingerprint,
            memory_eligible=False,
        )
        same_fingerprint_two = self._ingest_controlled(
            principal_name="same_fingerprint_git",
            evidence_kind="git_commit",
            state="artifact_committed",
            idempotency_key="idem_70800032",
            source_ref="source_70800032",
            object_ref=object_same_fingerprint,
            memory_eligible=False,
        )
        self.assertEqual((same_fingerprint_one["verified_ref"], same_fingerprint_two["verified_ref"]), ("", ""))
        rejected_fingerprint = self._ingest_ai(
            idempotency_key="idem_70800033",
            source_ref="source_70800033",
            object_ref=object_same_fingerprint,
            evidence_refs=[same_fingerprint_one["evidence_ref"], same_fingerprint_two["evidence_ref"]],
        )
        self.assertEqual(
            rejected_fingerprint["reason_code"],
            "independent_evidence_key_fingerprint_required",
        )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM context_verified").fetchone()[0], 0)

    def test_evidence_cross_object_version_subject_and_time_are_rejected(self) -> None:
        alternate_workstream = "workstream_70900001"
        alternate_object = "object_70900001"
        alternate_version = "version_70900001"
        alternate_subject = "subject_70900001"
        for kind, ref, label, scope in (
            ("workstream", alternate_workstream, "Alternate Workstream", ""),
            ("object", alternate_object, "Alternate Object", ""),
            ("version", alternate_version, "Alternate Version", ""),
            ("subject", alternate_subject, "Alternate Owner", "owner"),
        ):
            self.assertEqual(
                register_context_label(
                    self.conn,
                    kind=kind,
                    ref=ref,
                    label=label,
                    project_ref=self.project,
                    subject_scope=scope,
                )["status"],
                "registered",
            )

        cases: list[tuple[dict, str]] = []
        for index, (field, value, reason) in enumerate(
            (
                ("workstream_ref", alternate_workstream, "evidence_workstream_mismatch"),
                ("object_ref", alternate_object, "evidence_object_mismatch"),
                ("version_ref", alternate_version, "evidence_version_mismatch"),
                ("subject_ref", alternate_subject, "evidence_subject_mismatch"),
                ("privacy_class", "sensitive_local", "evidence_privacy_mismatch"),
            ),
            start=1,
        ):
            kwargs = {
                "principal_name": "file",
                "evidence_kind": "file_saved",
                "state": "artifact_saved",
                "idempotency_key": f"idem_709000{index:02d}",
                "source_ref": f"source_709000{index:02d}",
                "memory_eligible": False,
                field: value,
            }
            evidence = self._ingest_controlled(**kwargs)
            self.assertEqual(evidence["verified_ref"], "")
            cases.append((evidence, reason))

        old_time = self.now - timedelta(hours=25)
        old_payload = self._controlled_payload(
            principal_name="file",
            evidence_kind="file_saved",
            state="artifact_saved",
            idempotency_key="idem_70900006",
            source_ref="source_70900006",
            occurred_at=old_time,
            memory_eligible=False,
        )
        old_evidence = ingest_controlled_evidence(
            self.conn,
            self._wire(old_payload),
            verification_secret=self.secrets["file"],
            now=old_time,
        )
        cases.append((old_evidence, "evidence_time_window_mismatch"))

        for index, (evidence, reason) in enumerate(cases, start=1):
            receipt = self._receipt_payload(
                idempotency_key=f"idem_709001{index:02d}",
                source_ref=f"source_709001{index:02d}",
                evidence_refs=[evidence["evidence_ref"]],
            )
            before_verified = self.conn.execute("SELECT COUNT(*) FROM context_verified").fetchone()[0]
            rejected = ingest_context_receipt(
                self.conn,
                self._wire(receipt),
                verification_secret=self.secrets["ai"],
                now=self.now,
            )
            self.assertEqual((rejected["status"], rejected["reason_code"]), ("rejected", reason))
            self.assertEqual(
                self.conn.execute("SELECT COUNT(*) FROM context_verified").fetchone()[0],
                before_verified,
            )

        other_refs = {
            "workstream": "workstream_70900021",
            "object": "object_70900021",
            "version": "version_70900021",
            "subject": "subject_70900021",
        }
        for kind, ref in other_refs.items():
            result = register_context_label(
                self.conn,
                kind=kind,
                ref=ref,
                label=f"Other Project {kind.title()}",
                project_ref=self.other_project,
                subject_scope="owner" if kind == "subject" else "",
            )
            self.assertEqual(result["status"], "registered")
        self._register_test_principal(
            "other_project_file",
            principal_id="principal_70900021",
            key_id="key_70900021",
            principal_kind="file_adapter",
            secret=b"other-project-file-secret",
            project_ref=self.other_project,
        )
        other_project_evidence = self._ingest_controlled(
            principal_name="other_project_file",
            evidence_kind="file_saved",
            state="artifact_saved",
            idempotency_key="idem_70900021",
            source_ref="source_70900021",
            project_ref=self.other_project,
            workstream_ref=other_refs["workstream"],
            object_ref=other_refs["object"],
            version_ref=other_refs["version"],
            subject_ref=other_refs["subject"],
            memory_eligible=False,
        )
        project_receipt = self._receipt_payload(
            idempotency_key="idem_70900022",
            source_ref="source_70900022",
            evidence_refs=[other_project_evidence["evidence_ref"]],
        )
        rejected_project = ingest_context_receipt(
            self.conn,
            self._wire(project_receipt),
            verification_secret=self.secrets["ai"],
            now=self.now,
        )
        self.assertEqual(rejected_project["reason_code"], "evidence_project_mismatch")

        pair_file_object = "object_70900023"
        pair_git_object = "object_70900024"
        for ref, label in (
            (pair_file_object, "Mismatched Pair File Object"),
            (pair_git_object, "Mismatched Pair Git Object"),
        ):
            self.assertEqual(
                register_context_label(
                    self.conn, kind="object", ref=ref, label=label, project_ref=self.project
                )["status"],
                "registered",
            )
        mismatched_file = self._ingest_controlled(
            principal_name="file",
            evidence_kind="file_saved",
            state="artifact_saved",
            idempotency_key="idem_70900023",
            source_ref="source_70900023",
            object_ref=pair_file_object,
            memory_eligible=False,
        )
        mismatched_git = self._ingest_controlled(
            principal_name="git",
            evidence_kind="git_commit",
            state="artifact_committed",
            idempotency_key="idem_70900024",
            source_ref="source_70900024",
            object_ref=pair_git_object,
            memory_eligible=False,
        )
        self.assertEqual((mismatched_file["verified_ref"], mismatched_git["verified_ref"]), ("", ""))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM context_verified").fetchone()[0], 0)

    def test_memory_rejects_ai_file_git_and_other_operational_only_contexts(self) -> None:
        ai = self._ingest_ai(idempotency_key="idem_71000001", source_ref="source_71000001")
        file_result = self._ingest_controlled(
            principal_name="file",
            evidence_kind="file_saved",
            state="artifact_saved",
            idempotency_key="idem_71000002",
            source_ref="source_71000002",
            memory_eligible=False,
        )
        self.assertEqual((ai["verified_ref"], file_result["verified_ref"]), ("", ""))
        _, operational = self._ingest_operational_pair(
            file_idempotency_key="idem_71000003",
            git_idempotency_key="idem_71000004",
            file_source_ref="source_71000003",
            git_source_ref="source_71000004",
        )
        rejected_operational = create_memory_candidate(
            self.conn,
            verified_ref=operational["verified_ref"],
            subject_ref=self.owner,
            now=self.now,
        )
        self.assertEqual(rejected_operational["reason_code"], "memory_scope_not_eligible")

        external = self._ingest_controlled(
            principal_name="external",
            evidence_kind="external_receipt",
            state="external_delivered",
            idempotency_key="idem_71000005",
            source_ref="source_71000005",
            provider_receipt_ref="provider_71000005",
            memory_eligible=False,
            verification_scope="operational",
        )
        rejected_external = create_memory_candidate(
            self.conn,
            verified_ref=external["verified_ref"],
            subject_ref=self.owner,
            now=self.now,
        )
        self.assertEqual(rejected_external["reason_code"], "memory_scope_not_eligible")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM context_memory_candidates").fetchone()[0], 0)

    def test_memory_rejects_restricted_third_party_expired_revoked_and_wrong_subject(self) -> None:
        restricted = self._ingest_memory_confirmation(
            idempotency_key="idem_71100001",
            source_ref="source_71100001",
            privacy_class="restricted_local",
            memory_eligible=False,
            verification_scope="operational",
        )
        self.assertEqual(
            create_memory_candidate(
                self.conn, verified_ref=restricted["verified_ref"], subject_ref=self.owner, now=self.now
            )["reason_code"],
            "restricted_local_memory_forbidden",
        )

        third_party = self._ingest_memory_confirmation(
            idempotency_key="idem_71100002",
            source_ref="source_71100002",
            subject_ref=self.third_party,
            memory_eligible=False,
            verification_scope="operational",
        )
        self.assertEqual(
            create_memory_candidate(
                self.conn,
                verified_ref=third_party["verified_ref"],
                subject_ref=self.third_party,
                now=self.now,
            )["reason_code"],
            "third_party_subject_forbidden",
        )

        other_owner = "subject_71100003"
        self.assertEqual(
            register_context_label(
                self.conn,
                kind="subject",
                ref=other_owner,
                label="Other Owner",
                project_ref=self.project,
                subject_scope="owner",
            )["status"],
            "registered",
        )
        owner_context = self._ingest_memory_confirmation(
            idempotency_key="idem_71100003",
            source_ref="source_71100003",
        )
        self.assertEqual(
            create_memory_candidate(
                self.conn, verified_ref=owner_context["verified_ref"], subject_ref=other_owner, now=self.now
            )["reason_code"],
            "subject_mismatch",
        )

        old_time = self.now - timedelta(days=91)
        expired_payload = self._controlled_payload(
            principal_name="user",
            evidence_kind="user_confirmation",
            state="completed_confirmed",
            idempotency_key="idem_71100004",
            source_ref="source_71100004",
            occurred_at=old_time,
            memory_eligible=True,
            verification_scope="memory",
        )
        expired = ingest_controlled_evidence(
            self.conn,
            self._wire(expired_payload),
            verification_secret=self.secrets["user"],
            now=old_time,
        )
        self.assertEqual(expired["status"], "accepted")
        self.assertEqual(
            create_memory_candidate(
                self.conn, verified_ref=expired["verified_ref"], subject_ref=self.owner, now=self.now
            )["reason_code"],
            "verified_context_expired",
        )

        revoked = self._ingest_memory_confirmation(
            idempotency_key="idem_71100005", source_ref="source_71100005"
        )
        delete_context_observation(self.conn, observation_ref=revoked["observation_ref"], now=self.now)
        self.assertEqual(
            create_memory_candidate(
                self.conn, verified_ref=revoked["verified_ref"], subject_ref=self.owner, now=self.now
            )["reason_code"],
            "verified_context_revoked",
        )

        missing_provenance = self._ingest_memory_confirmation(
            idempotency_key="idem_71100006", source_ref="source_71100006"
        )
        self.conn.execute(
            "UPDATE context_evidence SET provenance_ref='' WHERE evidence_ref=?",
            (missing_provenance["evidence_ref"],),
        )
        self.conn.commit()
        self.assertEqual(
            create_memory_candidate(
                self.conn,
                verified_ref=missing_provenance["verified_ref"],
                subject_ref=self.owner,
                now=self.now,
            )["reason_code"],
            "provenance_missing",
        )
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM context_memory_candidates").fetchone()[0], 0)

    def test_memory_candidate_requires_explicit_memory_scope_and_owner_provenance(self) -> None:
        operational_owner = self._ingest_memory_confirmation(
            idempotency_key="idem_71200001",
            source_ref="source_71200001",
            memory_eligible=False,
            verification_scope="operational",
        )
        rejected = create_memory_candidate(
            self.conn,
            verified_ref=operational_owner["verified_ref"],
            subject_ref=self.owner,
            now=self.now,
        )
        self.assertEqual((rejected["status"], rejected["reason_code"]), ("rejected", "memory_scope_not_eligible"))

        governed = self._ingest_memory_confirmation(
            idempotency_key="idem_71200002",
            source_ref="source_71200002",
            memory_eligible=True,
            verification_scope="memory",
        )
        row = self.conn.execute(
            "SELECT verification_scope,memory_eligible,source_kind,provenance_ref,status "
            "FROM context_verified WHERE verified_ref=?",
            (governed["verified_ref"],),
        ).fetchone()
        self.assertEqual(tuple(row[:3]), ("memory", 1, "user_confirmation"))
        self.assertTrue(row["provenance_ref"])
        self.assertEqual(row["status"], "active")
        candidate = create_memory_candidate(
            self.conn,
            verified_ref=governed["verified_ref"],
            subject_ref=self.owner,
            now=self.now,
        )
        self.assertEqual(candidate["status"], "candidate_only")
        self.assertFalse(candidate["direct_write"])
        self.assertFalse(candidate["writer_enabled"])
        self.assertFalse(candidate["memory_write_actions"])


    def test_external_receipt_reference_is_immutable_semantic_binding(self) -> None:
        alternate_object = "object_71400002"
        self.assertEqual(
            register_context_label(
                self.conn,
                kind="object",
                ref=alternate_object,
                label="Conflicting External Object",
                project_ref=self.project,
            )["status"],
            "registered",
        )
        original = self._controlled_payload(
            principal_name="external",
            evidence_kind="external_receipt",
            state="completed_confirmed",
            idempotency_key="idem_71400001",
            source_ref="source_71400001",
            provider_receipt_ref="provider_71400001",
            memory_eligible=False,
            verification_scope="operational",
        )
        accepted = ingest_controlled_evidence(
            self.conn,
            self._wire(original),
            verification_secret=self.secrets["external"],
            now=self.now,
        )
        self.assertEqual(accepted["status"], "accepted")
        original_receipt_ref = accepted["receipt_ref"]
        principal_id, _old_key, _kind = self.principals["external"]
        rotated_key = "key_71400002"
        rotated_secret = b"rotated-external-secret-714"
        self.assertEqual(
            register_context_principal(
                self.conn,
                principal_id=principal_id,
                key_id=rotated_key,
                principal_kind="external_adapter",
                verification_secret=rotated_secret,
                project_ref=self.project,
            )["status"],
            "registered",
        )
        exact = copy.deepcopy(original)
        exact["key_id"] = rotated_key
        exact["idempotency_key"] = "idem_71400002"
        exact["occurred_at"] = (self.now + timedelta(seconds=1)).isoformat(timespec="seconds")
        exact["signature"] = sign_contract_payload(exact, rotated_secret)
        exact_replay = ingest_controlled_evidence(
            self.conn,
            self._wire(exact),
            verification_secret=rotated_secret,
            now=self.now + timedelta(seconds=1),
        )
        self.assertEqual(
            (exact_replay["status"], exact_replay["reason_code"], exact_replay["receipt_ref"]),
            ("replayed", "canonical_source_event_replay", original_receipt_ref),
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM context_external_receipt_bindings").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM context_ingest_receipts").fetchone()[0],
            1,
        )

        before = {
            table: self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "context_external_receipt_bindings",
                "context_ingest_receipts",
                "context_source_events",
                "context_observations",
                "context_evidence",
                "context_candidates",
                "context_verified",
            )
        }
        self.conn.commit()
        reopened = ensure_ready(self.db)
        try:
            changed = copy.deepcopy(exact)
            changed.update(
                {
                    "idempotency_key": "idem_71400003",
                    "source_ref": "source_71400003",
                    "object_ref": alternate_object,
                    "state": "external_delivered",
                    "occurred_at": (self.now + timedelta(seconds=2)).isoformat(timespec="seconds"),
                    "signature": "",
                }
            )
            changed["signature"] = sign_contract_payload(changed, rotated_secret)
            conflict = ingest_controlled_evidence(
                reopened,
                self._wire(changed),
                verification_secret=rotated_secret,
                now=self.now + timedelta(seconds=2),
            )
            self.assertEqual(
                (conflict["status"], conflict["reason_code"]),
                ("conflict", "external_receipt_binding_conflict"),
            )
            after = {
                table: reopened.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in before
            }
            self.assertEqual(after, before)
            binding = reopened.execute(
                "SELECT source_ref,object_ref,state,privacy_class FROM context_external_receipt_bindings "
                "WHERE principal_id=? AND provider_receipt_ref=?",
                (principal_id, "provider_71400001"),
            ).fetchone()
            self.assertEqual(
                tuple(binding),
                ("source_71400001", self.object, "completed_confirmed", "private_local"),
            )
        finally:
            reopened.close()

    def test_canonical_replay_new_key_rechecks_freshness_and_does_not_persist_stale_alias(self) -> None:
        cases = (
            (
                "ai",
                self._receipt_payload(
                    idempotency_key="idem_71500001",
                    source_ref="source_71500001",
                ),
                ingest_context_receipt,
                self.secrets["ai"],
            ),
            (
                "controlled",
                self._controlled_payload(
                    principal_name="file",
                    evidence_kind="file_saved",
                    state="artifact_saved",
                    idempotency_key="idem_71500002",
                    source_ref="source_71500002",
                    memory_eligible=False,
                ),
                ingest_controlled_evidence,
                self.secrets["file"],
            ),
        )
        for index, (label, original, ingest, secret) in enumerate(cases, start=1):
            accepted = ingest(
                self.conn,
                self._wire(original),
                verification_secret=secret,
                now=self.now,
            )
            self.assertEqual(accepted["status"], "accepted", label)
            original_receipt_ref = accepted["receipt_ref"]
            baseline_counts = {
                table: self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "context_ingest_receipts",
                    "context_source_events",
                    "context_observations",
                    "context_evidence",
                    "context_candidates",
                    "context_verified",
                )
            }
            for suffix, occurred_at, expected_reason in (
                ("old", self.now - timedelta(days=365), "receipt_too_old"),
                ("future", self.now + timedelta(minutes=6), "receipt_from_future"),
            ):
                replay = copy.deepcopy(original)
                replay["idempotency_key"] = f"idem_71500{index}1{1 if suffix == 'old' else 2}"
                replay["occurred_at"] = occurred_at.isoformat(timespec="seconds")
                replay["signature"] = sign_contract_payload(replay, secret)
                result = ingest(
                    self.conn,
                    self._wire(replay),
                    verification_secret=secret,
                    now=self.now,
                )
                self.assertEqual(
                    (result["status"], result["reason_code"]),
                    ("rejected", expected_reason),
                    f"{label}:{suffix}",
                )
                self.assertEqual(
                    {
                        table: self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                        for table in baseline_counts
                    },
                    baseline_counts,
                    f"{label}:{suffix}",
                )

            fresh = copy.deepcopy(original)
            fresh["idempotency_key"] = f"idem_71500{index}13"
            fresh["occurred_at"] = (self.now + timedelta(seconds=1)).isoformat(timespec="seconds")
            fresh["signature"] = sign_contract_payload(fresh, secret)
            fresh_result = ingest(
                self.conn,
                self._wire(fresh),
                verification_secret=secret,
                now=self.now + timedelta(seconds=1),
            )
            self.assertEqual(
                (
                    fresh_result["status"],
                    fresh_result["reason_code"],
                    fresh_result["receipt_ref"],
                ),
                ("replayed", "canonical_source_event_replay", original_receipt_ref),
                label,
            )
            self.assertEqual(
                {
                    table: self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in baseline_counts
                },
                baseline_counts,
                f"{label}:fresh",
            )

    def test_idempotency_scope_migration_removes_legacy_global_unique_when_both_exist(self) -> None:
        legacy_db = self.root / "schema30-both-idempotency-scopes.sqlite"
        conn = ensure_ready(legacy_db)
        try:
            conn.execute("DROP TABLE context_ingest_receipts")
            conn.execute(
                "CREATE TABLE context_ingest_receipts ("
                "receipt_ref TEXT PRIMARY KEY,contract_type TEXT NOT NULL,principal_id TEXT NOT NULL,"
                "key_id TEXT NOT NULL,source_ref TEXT NOT NULL,idempotency_key TEXT NOT NULL,"
                "payload_hash TEXT NOT NULL,source_event_ref TEXT NOT NULL DEFAULT '',"
                "received_at TEXT NOT NULL,occurred_at TEXT NOT NULL,project_ref TEXT NOT NULL,"
                "observation_ref TEXT,result_status TEXT NOT NULL,"
                "UNIQUE(contract_type,principal_id,idempotency_key),"
                "UNIQUE(contract_type,principal_id,source_ref,idempotency_key))"
            )
            conn.execute(
                "INSERT INTO context_ingest_receipts("
                "receipt_ref,contract_type,principal_id,key_id,source_ref,idempotency_key,payload_hash,"
                "source_event_ref,received_at,occurred_at,project_ref,observation_ref,result_status) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "receipt_71600001",
                    CONTEXT_RECEIPT_VERSION,
                    "principal_71600001",
                    "key_71600001",
                    "source_71600001",
                    "idem_71600001",
                    "a" * 64,
                    "",
                    self.now.isoformat(timespec="seconds"),
                    self.now.isoformat(timespec="seconds"),
                    self.project,
                    "",
                    "draft_generated",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        upgraded = ensure_ready(legacy_db)
        try:
            unique_scopes = {
                tuple(item[2] for item in upgraded.execute(f"PRAGMA index_info('{index[1]}')"))
                for index in upgraded.execute("PRAGMA index_list(context_ingest_receipts)")
                if int(index[2] or 0) == 1
            }
            expected = ("contract_type", "principal_id", "source_ref", "idempotency_key")
            legacy = ("contract_type", "principal_id", "idempotency_key")
            self.assertIn(expected, unique_scopes)
            self.assertNotIn(legacy, unique_scopes)
            self.assertEqual(
                tuple(upgraded.execute(
                    "SELECT source_ref,result_status FROM context_ingest_receipts "
                    "WHERE receipt_ref='receipt_71600001'"
                ).fetchone()),
                ("source_71600001", "draft_generated"),
            )
            upgraded.execute(
                "INSERT INTO context_ingest_receipts("
                "receipt_ref,contract_type,principal_id,key_id,source_ref,idempotency_key,payload_hash,"
                "source_event_ref,received_at,occurred_at,project_ref,observation_ref,result_status) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "receipt_71600002",
                    CONTEXT_RECEIPT_VERSION,
                    "principal_71600001",
                    "key_71600001",
                    "source_71600002",
                    "idem_71600001",
                    "b" * 64,
                    "",
                    self.now.isoformat(timespec="seconds"),
                    self.now.isoformat(timespec="seconds"),
                    self.project,
                    "",
                    "draft_generated",
                ),
            )
            upgraded.commit()
        finally:
            upgraded.close()

        repeated = ensure_ready(legacy_db)
        try:
            self.assertEqual(
                repeated.execute(
                    "SELECT COUNT(*) FROM context_ingest_receipts "
                    "WHERE principal_id='principal_71600001' AND idempotency_key='idem_71600001'"
                ).fetchone()[0],
                2,
            )
            self.assertFalse(
                repeated.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='context_ingest_receipts_scope_v2'"
                ).fetchone()
            )
        finally:
            repeated.close()


    def test_external_receipt_binding_migration_backfills_history_and_rejects_rebinding(self) -> None:
        alternate_object = "object_71700002"
        self.assertEqual(
            register_context_label(
                self.conn,
                kind="object",
                ref=alternate_object,
                label="Historical External Object",
                project_ref=self.project,
            )["status"],
            "registered",
        )
        original = self._controlled_payload(
            principal_name="external",
            evidence_kind="external_receipt",
            state="completed_confirmed",
            idempotency_key="idem_71700001",
            source_ref="source_71700001",
            provider_receipt_ref="provider_71700001",
            memory_eligible=False,
            verification_scope="operational",
        )
        accepted = ingest_controlled_evidence(
            self.conn,
            self._wire(original),
            verification_secret=self.secrets["external"],
            now=self.now,
        )
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(
            delete_context_observation(
                self.conn,
                observation_ref=accepted["observation_ref"],
                now=self.now + timedelta(seconds=1),
            )["status"],
            "completed",
        )
        self.conn.execute(
            "UPDATE context_evidence SET expires_at=? WHERE evidence_ref=?",
            ((self.now - timedelta(days=1)).isoformat(timespec="seconds"), accepted["evidence_ref"]),
        )
        self.conn.execute("DROP TABLE context_external_receipt_bindings")
        self.conn.commit()
        self.conn.close()
        self.conn = ensure_ready(self.db)

        principal_id, _key_id, _kind = self.principals["external"]
        binding = self.conn.execute(
            "SELECT source_ref,object_ref,state FROM context_external_receipt_bindings "
            "WHERE principal_id=? AND provider_receipt_ref=?",
            (principal_id, "provider_71700001"),
        ).fetchone()
        self.assertEqual(tuple(binding), ("source_71700001", self.object, "completed_confirmed"))
        before = {
            table: self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "context_external_receipt_bindings",
                "context_ingest_receipts",
                "context_source_events",
                "context_observations",
                "context_evidence",
                "context_candidates",
                "context_verified",
            )
        }
        changed = copy.deepcopy(original)
        changed.update(
            {
                "idempotency_key": "idem_71700002",
                "source_ref": "source_71700002",
                "object_ref": alternate_object,
                "state": "external_delivered",
                "occurred_at": (self.now + timedelta(seconds=2)).isoformat(timespec="seconds"),
                "signature": "",
            }
        )
        changed["signature"] = sign_contract_payload(changed, self.secrets["external"])
        conflict = ingest_controlled_evidence(
            self.conn,
            self._wire(changed),
            verification_secret=self.secrets["external"],
            now=self.now + timedelta(seconds=2),
        )
        self.assertEqual(
            (conflict["status"], conflict["reason_code"]),
            ("conflict", "external_receipt_binding_conflict"),
        )
        self.assertEqual(
            {
                table: self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in before
            },
            before,
        )

        second = self._controlled_payload(
            principal_name="external",
            evidence_kind="external_receipt",
            state="external_delivered",
            idempotency_key="idem_71700003",
            source_ref="source_71700003",
            provider_receipt_ref="provider_71700003",
            object_ref=alternate_object,
            memory_eligible=False,
            verification_scope="operational",
            occurred_at=self.now + timedelta(seconds=3),
        )
        second_result = ingest_controlled_evidence(
            self.conn,
            self._wire(second),
            verification_secret=self.secrets["external"],
            now=self.now + timedelta(seconds=3),
        )
        self.assertEqual(second_result["status"], "accepted")
        self.conn.execute(
            "UPDATE context_evidence SET provenance_ref='provider_71700001' WHERE evidence_ref=?",
            (second_result["evidence_ref"],),
        )
        self.conn.execute("DROP TABLE context_external_receipt_bindings")
        self.conn.commit()
        self.conn.close()
        with self.assertRaisesRegex(
            sqlite3.DatabaseError, "external_receipt_binding_history_conflict"
        ):
            ensure_ready(self.db)
        self.conn = connect(self.db)
        self.assertFalse(
            self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name IN ('context_external_receipt_bindings','context_external_receipt_backfill')"
            ).fetchone()
        )

    def test_external_receipt_exact_replay_repairs_binding(self) -> None:
        payload = self._controlled_payload(
            principal_name="external",
            evidence_kind="external_receipt",
            state="completed_confirmed",
            idempotency_key="idem_71800001",
            source_ref="source_71800001",
            provider_receipt_ref="provider_71800001",
            memory_eligible=False,
            verification_scope="operational",
        )
        accepted = ingest_controlled_evidence(
            self.conn,
            self._wire(payload),
            verification_secret=self.secrets["external"],
            now=self.now,
        )
        self.assertEqual(accepted["status"], "accepted")
        self.conn.execute(
            "DELETE FROM context_external_receipt_bindings "
            "WHERE principal_id=? AND provider_receipt_ref=?",
            (self.principals["external"][0], "provider_71800001"),
        )
        self.conn.commit()
        replay = ingest_controlled_evidence(
            self.conn,
            self._wire(payload),
            verification_secret=self.secrets["external"],
            now=self.now,
        )
        self.assertEqual(
            (replay["status"], replay["reason_code"], replay["receipt_ref"]),
            ("replayed", "idempotent_replay", accepted["receipt_ref"]),
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM context_external_receipt_bindings "
                "WHERE principal_id=? AND provider_receipt_ref=?",
                (self.principals["external"][0], "provider_71800001"),
            ).fetchone()[0],
            1,
        )

        self.conn.execute(
            "UPDATE context_external_receipt_bindings SET semantic_hash=? "
            "WHERE principal_id=? AND provider_receipt_ref=?",
            ("0" * 64, self.principals["external"][0], "provider_71800001"),
        )
        self.conn.commit()
        before = {
            table: self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "context_external_receipt_bindings",
                "context_ingest_receipts",
                "context_source_events",
                "context_observations",
                "context_evidence",
                "context_candidates",
                "context_verified",
            )
        }
        blocked = ingest_controlled_evidence(
            self.conn,
            self._wire(payload),
            verification_secret=self.secrets["external"],
            now=self.now,
        )
        self.assertEqual(
            (blocked["status"], blocked["reason_code"]),
            ("conflict", "external_receipt_binding_conflict"),
        )
        self.assertEqual(
            {
                table: self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in before
            },
            before,
        )

    def test_external_receipt_binding_schema_rebuild_dedupes_and_fails_closed_on_conflict(self) -> None:
        payload = self._controlled_payload(
            principal_name="external",
            evidence_kind="external_receipt",
            state="completed_confirmed",
            idempotency_key="idem_72000001",
            source_ref="source_72000001",
            provider_receipt_ref="provider_72000001",
            memory_eligible=False,
            verification_scope="operational",
        )
        accepted = ingest_controlled_evidence(
            self.conn,
            self._wire(payload),
            verification_secret=self.secrets["external"],
            now=self.now,
        )
        self.assertEqual(accepted["status"], "accepted")
        principal_id = self.principals["external"][0]
        column_names = (
            "principal_id",
            "provider_receipt_ref",
            "semantic_hash",
            "source_ref",
            "project_ref",
            "workstream_ref",
            "object_ref",
            "version_ref",
            "subject_ref",
            "privacy_class",
            "state",
            "created_at",
        )
        original = dict(
            zip(
                column_names,
                tuple(
                    self.conn.execute(
                        "SELECT principal_id,provider_receipt_ref,semantic_hash,source_ref,"
                        "project_ref,workstream_ref,object_ref,version_ref,subject_ref,"
                        "privacy_class,state,created_at "
                        "FROM context_external_receipt_bindings "
                        "WHERE principal_id=? AND provider_receipt_ref=?",
                        (principal_id, "provider_72000001"),
                    ).fetchone()
                ),
            )
        )

        def create_malformed_binding_table(conn: sqlite3.Connection) -> None:
            conn.execute("DROP TABLE context_external_receipt_bindings")
            conn.execute(
                "CREATE TABLE context_external_receipt_bindings ("
                "principal_id TEXT NOT NULL,provider_receipt_ref TEXT NOT NULL,"
                "semantic_hash TEXT NOT NULL,source_ref TEXT NOT NULL,"
                "project_ref TEXT NOT NULL,workstream_ref TEXT NOT NULL,"
                "object_ref TEXT NOT NULL,version_ref TEXT NOT NULL,"
                "subject_ref TEXT NOT NULL,privacy_class TEXT NOT NULL,"
                "state TEXT NOT NULL,created_at TEXT NOT NULL)"
            )

        def insert_binding(conn: sqlite3.Connection, row: dict[str, str]) -> None:
            conn.execute(
                "INSERT INTO context_external_receipt_bindings("
                "principal_id,provider_receipt_ref,semantic_hash,source_ref,project_ref,"
                "workstream_ref,object_ref,version_ref,subject_ref,privacy_class,state,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(row[name] for name in column_names),
            )

        create_malformed_binding_table(self.conn)
        later = dict(original)
        later["created_at"] = (self.now + timedelta(seconds=5)).isoformat(timespec="seconds")
        earlier = dict(original)
        earlier["created_at"] = (self.now - timedelta(seconds=5)).isoformat(timespec="seconds")
        insert_binding(self.conn, later)
        insert_binding(self.conn, earlier)
        self.conn.commit()
        self.conn.close()
        self.conn = ensure_ready(self.db)

        expected_columns = (
            ("principal_id", "TEXT", 1, None, 1),
            ("provider_receipt_ref", "TEXT", 1, None, 2),
            ("semantic_hash", "TEXT", 1, None, 0),
            ("source_ref", "TEXT", 1, None, 0),
            ("project_ref", "TEXT", 1, None, 0),
            ("workstream_ref", "TEXT", 1, None, 0),
            ("object_ref", "TEXT", 1, None, 0),
            ("version_ref", "TEXT", 1, None, 0),
            ("subject_ref", "TEXT", 1, None, 0),
            ("privacy_class", "TEXT", 1, None, 0),
            ("state", "TEXT", 1, None, 0),
            ("created_at", "TEXT", 1, None, 0),
        )
        self.assertEqual(
            tuple(
                (row[1], str(row[2]).upper(), int(row[3]), row[4], int(row[5]))
                for row in self.conn.execute(
                    "PRAGMA table_info(context_external_receipt_bindings)"
                )
            ),
            expected_columns,
        )
        indexes = list(
            self.conn.execute("PRAGMA index_list(context_external_receipt_bindings)")
        )
        self.assertEqual(len(indexes), 1)
        self.assertEqual(
            (int(indexes[0][2]), indexes[0][3], int(indexes[0][4])),
            (1, "pk", 0),
        )
        self.assertEqual(
            tuple(
                row[2]
                for row in self.conn.execute(
                    f"PRAGMA index_info('{indexes[0][1]}')"
                )
            ),
            ("principal_id", "provider_receipt_ref"),
        )
        deduped = self.conn.execute(
            "SELECT semantic_hash,source_ref,project_ref,workstream_ref,object_ref,"
            "version_ref,subject_ref,privacy_class,state,created_at "
            "FROM context_external_receipt_bindings "
            "WHERE principal_id=? AND provider_receipt_ref=?",
            (principal_id, "provider_72000001"),
        ).fetchall()
        self.assertEqual(len(deduped), 1)
        self.assertEqual(
            tuple(deduped[0]),
            tuple(original[name] for name in column_names[2:-1])
            + (earlier["created_at"],),
        )

        create_malformed_binding_table(self.conn)
        insert_binding(self.conn, original)
        conflicting = dict(original)
        conflicting.update(
            {
                "source_ref": "source_72000002",
                "state": "external_delivered",
                "created_at": (self.now + timedelta(seconds=10)).isoformat(
                    timespec="seconds"
                ),
            }
        )
        conflicting_binding = {
            name: conflicting[name]
            for name in (
                "source_ref",
                "project_ref",
                "workstream_ref",
                "object_ref",
                "version_ref",
                "subject_ref",
                "privacy_class",
                "state",
            )
        }
        conflicting["semantic_hash"] = hashlib.sha256(
            json.dumps(
                conflicting_binding,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        insert_binding(self.conn, conflicting)
        self.conn.commit()
        self.conn.close()
        with self.assertRaisesRegex(
            sqlite3.DatabaseError, "external_receipt_binding_history_conflict"
        ):
            ensure_ready(self.db)
        self.conn = connect(self.db)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM context_external_receipt_bindings "
                "WHERE principal_id=? AND provider_receipt_ref=?",
                (principal_id, "provider_72000001"),
            ).fetchone()[0],
            2,
        )
        self.assertFalse(
            self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='context_external_receipt_bindings_scope_v2'"
            ).fetchone()
        )
        self.assertTrue(
            all(
                int(row[5]) == 0
                for row in self.conn.execute(
                    "PRAGMA table_info(context_external_receipt_bindings)"
                )
            )
        )

    def test_external_receipt_binding_schema_rebuilds_virtual_and_stored_generated_columns(self) -> None:
        payload = self._controlled_payload(
            principal_name="external",
            evidence_kind="external_receipt",
            state="completed_confirmed",
            idempotency_key="idem_72100001",
            source_ref="source_72100001",
            provider_receipt_ref="provider_72100001",
            memory_eligible=False,
            verification_scope="operational",
        )
        accepted = ingest_controlled_evidence(
            self.conn,
            self._wire(payload),
            verification_secret=self.secrets["external"],
            now=self.now,
        )
        self.assertEqual(accepted["status"], "accepted")
        principal_id = self.principals["external"][0]
        column_names = (
            "principal_id",
            "provider_receipt_ref",
            "semantic_hash",
            "source_ref",
            "project_ref",
            "workstream_ref",
            "object_ref",
            "version_ref",
            "subject_ref",
            "privacy_class",
            "state",
            "created_at",
        )
        original = dict(
            zip(
                column_names,
                tuple(
                    self.conn.execute(
                        "SELECT principal_id,provider_receipt_ref,semantic_hash,source_ref,"
                        "project_ref,workstream_ref,object_ref,version_ref,subject_ref,"
                        "privacy_class,state,created_at "
                        "FROM context_external_receipt_bindings "
                        "WHERE principal_id=? AND provider_receipt_ref=?",
                        (principal_id, "provider_72100001"),
                    ).fetchone()
                ),
            )
        )

        for generated_kind in ("VIRTUAL", "STORED"):
            with self.subTest(generated_kind=generated_kind):
                self.conn.execute("DROP TABLE context_external_receipt_bindings")
                self.conn.execute(
                    "CREATE TABLE context_external_receipt_bindings ("
                    "principal_id TEXT NOT NULL,provider_receipt_ref TEXT NOT NULL,"
                    "semantic_hash TEXT NOT NULL,source_ref TEXT NOT NULL,"
                    "project_ref TEXT NOT NULL,workstream_ref TEXT NOT NULL,"
                    "object_ref TEXT NOT NULL,version_ref TEXT NOT NULL,"
                    "subject_ref TEXT NOT NULL,privacy_class TEXT NOT NULL,"
                    "state TEXT NOT NULL,created_at TEXT NOT NULL,"
                    "hidden_extra TEXT GENERATED ALWAYS AS (source_ref) "
                    f"{generated_kind},"
                    "PRIMARY KEY(principal_id,provider_receipt_ref))"
                )
                self.conn.execute(
                    "INSERT INTO context_external_receipt_bindings("
                    "principal_id,provider_receipt_ref,semantic_hash,source_ref,project_ref,"
                    "workstream_ref,object_ref,version_ref,subject_ref,privacy_class,state,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    tuple(original[name] for name in column_names),
                )
                self.assertFalse(
                    context_console_module._external_binding_table_contract_is_exact(
                        self.conn
                    )
                )
                self.conn.commit()
                self.conn.close()
                self.conn = ensure_ready(self.db)

                xinfo = [
                    tuple(row)
                    for row in self.conn.execute(
                        "PRAGMA table_xinfo(context_external_receipt_bindings)"
                    )
                ]
                self.assertEqual(len(xinfo), 12)
                self.assertTrue(all(int(row[6]) == 0 for row in xinfo))
                self.assertNotIn("hidden_extra", {str(row[1]) for row in xinfo})
                self.assertTrue(
                    context_console_module._external_binding_table_contract_is_exact(
                        self.conn
                    )
                )
                rebuilt = self.conn.execute(
                    "SELECT principal_id,provider_receipt_ref,semantic_hash,source_ref,"
                    "project_ref,workstream_ref,object_ref,version_ref,subject_ref,"
                    "privacy_class,state,created_at "
                    "FROM context_external_receipt_bindings "
                    "WHERE principal_id=? AND provider_receipt_ref=?",
                    (principal_id, "provider_72100001"),
                ).fetchall()
                self.assertEqual(
                    [tuple(row) for row in rebuilt],
                    [tuple(original[name] for name in column_names)],
                )

        self.conn.commit()
        self.conn.close()
        self.conn = ensure_ready(self.db)
        self.assertTrue(
            context_console_module._external_binding_table_contract_is_exact(
                self.conn
            )
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM context_external_receipt_bindings "
                "WHERE principal_id=? AND provider_receipt_ref=?",
                (principal_id, "provider_72100001"),
            ).fetchone()[0],
            1,
        )

    def test_external_receipt_binding_schema_rebuilds_nocase_desc_pk_and_rejects_invalid_keys(self) -> None:
        payload = self._controlled_payload(
            principal_name="external",
            evidence_kind="external_receipt",
            state="completed_confirmed",
            idempotency_key="idem_72200001",
            source_ref="source_72200001",
            provider_receipt_ref="provider_72200001",
            memory_eligible=False,
            verification_scope="operational",
        )
        accepted = ingest_controlled_evidence(
            self.conn,
            self._wire(payload),
            verification_secret=self.secrets["external"],
            now=self.now,
        )
        self.assertEqual(accepted["status"], "accepted")
        principal_id = self.principals["external"][0]
        column_names = (
            "principal_id",
            "provider_receipt_ref",
            "semantic_hash",
            "source_ref",
            "project_ref",
            "workstream_ref",
            "object_ref",
            "version_ref",
            "subject_ref",
            "privacy_class",
            "state",
            "created_at",
        )
        original = dict(
            zip(
                column_names,
                tuple(
                    self.conn.execute(
                        "SELECT principal_id,provider_receipt_ref,semantic_hash,source_ref,"
                        "project_ref,workstream_ref,object_ref,version_ref,subject_ref,"
                        "privacy_class,state,created_at "
                        "FROM context_external_receipt_bindings "
                        "WHERE principal_id=? AND provider_receipt_ref=?",
                        (principal_id, "provider_72200001"),
                    ).fetchone()
                ),
            )
        )

        def create_nocase_desc_table() -> None:
            self.conn.execute("DROP TABLE context_external_receipt_bindings")
            self.conn.execute(
                "CREATE TABLE context_external_receipt_bindings ("
                "principal_id TEXT NOT NULL COLLATE NOCASE,"
                "provider_receipt_ref TEXT NOT NULL COLLATE NOCASE,"
                "semantic_hash TEXT NOT NULL,source_ref TEXT NOT NULL,"
                "project_ref TEXT NOT NULL,workstream_ref TEXT NOT NULL,"
                "object_ref TEXT NOT NULL,version_ref TEXT NOT NULL,"
                "subject_ref TEXT NOT NULL,privacy_class TEXT NOT NULL,"
                "state TEXT NOT NULL,created_at TEXT NOT NULL,"
                "PRIMARY KEY(principal_id DESC,provider_receipt_ref DESC))"
            )

        def insert_binding(row: dict[str, str]) -> None:
            self.conn.execute(
                "INSERT INTO context_external_receipt_bindings("
                "principal_id,provider_receipt_ref,semantic_hash,source_ref,project_ref,"
                "workstream_ref,object_ref,version_ref,subject_ref,privacy_class,state,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(row[name] for name in column_names),
            )

        create_nocase_desc_table()
        insert_binding(original)
        self.assertFalse(
            context_console_module._external_binding_table_contract_is_exact(
                self.conn
            )
        )
        self.conn.commit()
        self.conn.close()
        self.conn = ensure_ready(self.db)

        self.assertTrue(
            context_console_module._external_binding_table_contract_is_exact(
                self.conn
            )
        )
        table_sql = str(
            self.conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='context_external_receipt_bindings'"
            ).fetchone()[0]
        ).upper()
        self.assertNotIn("COLLATE", table_sql)
        pk_index = next(
            row
            for row in self.conn.execute(
                "PRAGMA index_list(context_external_receipt_bindings)"
            )
            if str(row[3]) == "pk"
        )
        key_terms = [
            (str(row[2]), int(row[3]), str(row[4]).upper(), int(row[5]))
            for row in self.conn.execute(f"PRAGMA index_xinfo('{pk_index[1]}')")
            if int(row[5])
        ]
        self.assertEqual(
            key_terms,
            [
                ("principal_id", 0, "BINARY", 1),
                ("provider_receipt_ref", 0, "BINARY", 1),
            ],
        )
        rebuilt = self.conn.execute(
            "SELECT principal_id,provider_receipt_ref,semantic_hash,source_ref,"
            "project_ref,workstream_ref,object_ref,version_ref,subject_ref,"
            "privacy_class,state,created_at "
            "FROM context_external_receipt_bindings"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rebuilt],
            [tuple(original[name] for name in column_names)],
        )

        create_nocase_desc_table()
        invalid = dict(original)
        invalid["principal_id"] = str(invalid["principal_id"]).upper()
        invalid["provider_receipt_ref"] = str(
            invalid["provider_receipt_ref"]
        ).upper()
        insert_binding(invalid)
        self.conn.commit()
        malformed_sql = str(
            self.conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='context_external_receipt_bindings'"
            ).fetchone()[0]
        )
        malformed_row = tuple(
            self.conn.execute(
                "SELECT principal_id,provider_receipt_ref,semantic_hash,source_ref,"
                "project_ref,workstream_ref,object_ref,version_ref,subject_ref,"
                "privacy_class,state,created_at "
                "FROM context_external_receipt_bindings"
            ).fetchone()
        )
        self.conn.close()
        with self.assertRaisesRegex(
            sqlite3.DatabaseError,
            "external_receipt_binding_history_conflict",
        ):
            ensure_ready(self.db)
        self.conn = connect(self.db)
        self.assertEqual(
            str(
                self.conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' "
                    "AND name='context_external_receipt_bindings'"
                ).fetchone()[0]
            ),
            malformed_sql,
        )
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT principal_id,provider_receipt_ref,semantic_hash,source_ref,"
                    "project_ref,workstream_ref,object_ref,version_ref,subject_ref,"
                    "privacy_class,state,created_at "
                    "FROM context_external_receipt_bindings"
                ).fetchone()
            ),
            malformed_row,
        )
        self.assertFalse(
            self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='context_external_receipt_bindings_scope_v2'"
            ).fetchone()
        )

    def test_receipt_scope_migration_rebuilds_nullable_source_and_fails_closed_on_ambiguity(self) -> None:
        def create_malformed_receipt_table(conn: sqlite3.Connection) -> None:
            conn.execute("DROP TABLE context_ingest_receipts")
            conn.execute(
                "CREATE TABLE context_ingest_receipts ("
                "receipt_ref TEXT NOT NULL UNIQUE,contract_type TEXT NOT NULL,"
                "principal_id TEXT NOT NULL,key_id TEXT NOT NULL,source_ref BLOB DEFAULT '',"
                "idempotency_key TEXT NOT NULL,payload_hash TEXT NOT NULL,"
                "source_event_ref TEXT NOT NULL DEFAULT '',received_at TEXT NOT NULL,"
                "occurred_at TEXT NOT NULL,project_ref TEXT NOT NULL,observation_ref TEXT,"
                "result_status TEXT NOT NULL,"
                "UNIQUE(contract_type,principal_id,source_ref,idempotency_key))"
            )

        recoverable_db = self.root / "schema30-nullable-recoverable.sqlite"
        recoverable = ensure_ready(recoverable_db)
        try:
            create_malformed_receipt_table(recoverable)
            for index in (1, 2):
                recoverable.execute(
                    "INSERT INTO context_source_events("
                    "source_event_ref,contract_type,principal_id,key_id,source_ref,occurred_at,"
                    "project_ref,workstream_ref,object_ref,version_ref,subject_ref,payload_hash,"
                    "observation_ref,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?)",
                    (
                        f"sourceevent_7190000{index}",
                        CONTEXT_RECEIPT_VERSION,
                        "principal_71900001",
                        "key_71900001",
                        f"source_7190000{index}",
                        self.now.isoformat(timespec="seconds"),
                        self.project,
                        self.workstream,
                        self.object,
                        self.version,
                        self.owner,
                        f"{index}" * 64,
                        f"observation_7190000{index}",
                        self.now.isoformat(timespec="seconds"),
                    ),
                )
                recoverable.execute(
                    "INSERT INTO context_ingest_receipts("
                    "receipt_ref,contract_type,principal_id,key_id,source_ref,idempotency_key,"
                    "payload_hash,source_event_ref,received_at,occurred_at,project_ref,"
                    "observation_ref,result_status) VALUES(?,?,?,?,NULL,?,?,?,?,?,?,?,?)",
                    (
                        f"receipt_7190000{index}",
                        CONTEXT_RECEIPT_VERSION,
                        "principal_71900001",
                        "key_71900001",
                        "idem_71900001",
                        f"{index + 2}" * 64,
                        f"sourceevent_7190000{index}",
                        self.now.isoformat(timespec="seconds"),
                        self.now.isoformat(timespec="seconds"),
                        self.project,
                        f"observation_7190000{index}",
                        "draft_generated",
                    ),
                )
            recoverable.execute(
                "UPDATE context_ingest_receipts SET source_ref='' "
                "WHERE receipt_ref='receipt_71900002'"
            )
            recoverable.commit()
        finally:
            recoverable.close()

        upgraded = ensure_ready(recoverable_db)
        try:
            contract = [tuple(row) for row in upgraded.execute(
                "PRAGMA table_info(context_ingest_receipts)"
            )]
            source_column = next(row for row in contract if row[1] == "source_ref")
            receipt_column = next(row for row in contract if row[1] == "receipt_ref")
            self.assertEqual((source_column[2], source_column[3], source_column[4]), ("TEXT", 1, None))
            self.assertEqual((receipt_column[2], receipt_column[3], receipt_column[5]), ("TEXT", 0, 1))
            rows = upgraded.execute(
                "SELECT source_ref,idempotency_key FROM context_ingest_receipts "
                "WHERE principal_id='principal_71900001' ORDER BY source_ref"
            ).fetchall()
            self.assertEqual(
                [tuple(row) for row in rows],
                [
                    ("source_71900001", "idem_71900001"),
                    ("source_71900002", "idem_71900001"),
                ],
            )
        finally:
            upgraded.close()

        ambiguous_db = self.root / "schema30-nullable-ambiguous.sqlite"
        ambiguous = ensure_ready(ambiguous_db)
        try:
            create_malformed_receipt_table(ambiguous)
            for index in (3, 4):
                ambiguous.execute(
                    "INSERT INTO context_source_events("
                    "source_event_ref,contract_type,principal_id,key_id,source_ref,occurred_at,"
                    "project_ref,workstream_ref,object_ref,version_ref,subject_ref,payload_hash,"
                    "observation_ref,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?)",
                    (
                        f"sourceevent_7190000{index}",
                        CONTEXT_RECEIPT_VERSION,
                        "principal_71900002",
                        "key_71900002",
                        f"source_7190000{index}",
                        self.now.isoformat(timespec="seconds"),
                        self.project,
                        self.workstream,
                        self.object,
                        self.version,
                        self.owner,
                        f"{index}" * 64,
                        f"observation_7190000{index}",
                        self.now.isoformat(timespec="seconds"),
                    ),
                )
            ambiguous.execute(
                "INSERT INTO context_ingest_receipts("
                "receipt_ref,contract_type,principal_id,key_id,source_ref,idempotency_key,"
                "payload_hash,source_event_ref,received_at,occurred_at,project_ref,"
                "observation_ref,result_status) VALUES(?,?,?,?,NULL,?,?,?, ?,?,?,?,?)",
                (
                    "receipt_71900003",
                    CONTEXT_RECEIPT_VERSION,
                    "principal_71900002",
                    "key_71900002",
                    "idem_71900002",
                    "7" * 64,
                    "",
                    self.now.isoformat(timespec="seconds"),
                    self.now.isoformat(timespec="seconds"),
                    self.project,
                    "",
                    "draft_generated",
                ),
            )
            ambiguous.commit()
        finally:
            ambiguous.close()

        with self.assertRaisesRegex(
            sqlite3.DatabaseError, "context_ingest_source_ref_ambiguous"
        ):
            ensure_ready(ambiguous_db)
        rolled_back = connect(ambiguous_db)
        try:
            info = [tuple(row) for row in rolled_back.execute(
                "PRAGMA table_info(context_ingest_receipts)"
            )]
            source_column = next(row for row in info if row[1] == "source_ref")
            self.assertEqual((source_column[2], source_column[3], source_column[4]), ("BLOB", 0, "''"))
            self.assertIsNone(
                rolled_back.execute(
                    "SELECT source_ref FROM context_ingest_receipts "
                    "WHERE receipt_ref='receipt_71900003'"
                ).fetchone()[0]
            )
            self.assertFalse(
                rolled_back.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='context_ingest_receipts_scope_v2'"
                ).fetchone()
            )
        finally:
            rolled_back.close()

        collision_db = self.root / "schema30-nullable-collision.sqlite"
        collision = ensure_ready(collision_db)
        try:
            create_malformed_receipt_table(collision)
            for index in (5, 6):
                collision.execute(
                    "INSERT INTO context_source_events("
                    "source_event_ref,contract_type,principal_id,key_id,source_ref,occurred_at,"
                    "project_ref,workstream_ref,object_ref,version_ref,subject_ref,payload_hash,"
                    "observation_ref,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?)",
                    (
                        f"sourceevent_7190000{index}",
                        CONTEXT_RECEIPT_VERSION,
                        "principal_71900003",
                        "key_71900003",
                        "source_71900005",
                        self.now.isoformat(timespec="seconds"),
                        self.project,
                        self.workstream,
                        self.object,
                        self.version,
                        self.owner,
                        f"{index}" * 64,
                        f"observation_7190000{index}",
                        self.now.isoformat(timespec="seconds"),
                    ),
                )
                collision.execute(
                    "INSERT INTO context_ingest_receipts("
                    "receipt_ref,contract_type,principal_id,key_id,source_ref,idempotency_key,"
                    "payload_hash,source_event_ref,received_at,occurred_at,project_ref,"
                    "observation_ref,result_status) VALUES(?,?,?,?,NULL,?,?,?,?,?,?,?,?)",
                    (
                        f"receipt_7190000{index}",
                        CONTEXT_RECEIPT_VERSION,
                        "principal_71900003",
                        "key_71900003",
                        "idem_71900003",
                        f"{index + 1}" * 64,
                        f"sourceevent_7190000{index}",
                        self.now.isoformat(timespec="seconds"),
                        self.now.isoformat(timespec="seconds"),
                        self.project,
                        f"observation_7190000{index}",
                        "draft_generated",
                    ),
                )
            collision.commit()
        finally:
            collision.close()
        with self.assertRaisesRegex(
            sqlite3.DatabaseError, "context_ingest_scope_recovery_conflict"
        ):
            ensure_ready(collision_db)
        collision_rolled_back = connect(collision_db)
        try:
            self.assertEqual(
                collision_rolled_back.execute(
                    "SELECT COUNT(*) FROM context_ingest_receipts "
                    "WHERE principal_id='principal_71900003' AND source_ref IS NULL"
                ).fetchone()[0],
                2,
            )
            self.assertFalse(
                collision_rolled_back.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='context_ingest_receipts_scope_v2'"
                ).fetchone()
            )
        finally:
            collision_rolled_back.close()



    def test_schema30_v2_idempotency_scope_rebuild_preserves_receipts(self) -> None:
        legacy_db = self.root / "schema30-v2-idempotency.sqlite"
        conn = ensure_ready(legacy_db)
        try:
            conn.execute("DROP TABLE context_ingest_receipts")
            conn.execute(
                "CREATE TABLE context_ingest_receipts ("
                "receipt_ref TEXT PRIMARY KEY,contract_type TEXT NOT NULL,principal_id TEXT NOT NULL,"
                "key_id TEXT NOT NULL,idempotency_key TEXT NOT NULL,payload_hash TEXT NOT NULL,"
                "source_event_ref TEXT NOT NULL DEFAULT '',received_at TEXT NOT NULL,occurred_at TEXT NOT NULL,"
                "project_ref TEXT NOT NULL,observation_ref TEXT,result_status TEXT NOT NULL,"
                "UNIQUE(contract_type,principal_id,idempotency_key))"
            )
            conn.execute(
                "INSERT INTO context_source_events(source_event_ref,contract_type,principal_id,key_id,source_ref,"
                "occurred_at,project_ref,workstream_ref,object_ref,version_ref,subject_ref,payload_hash,"
                "observation_ref,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?)",
                (
                    "sourceevent_71300001",
                    CONTEXT_RECEIPT_VERSION,
                    "principal_71300001",
                    "key_71300001",
                    "source_71300001",
                    self.now.isoformat(timespec="seconds"),
                    self.project,
                    self.workstream,
                    self.object,
                    self.version,
                    self.owner,
                    "a" * 64,
                    "observation_71300001",
                    self.now.isoformat(timespec="seconds"),
                ),
            )
            conn.execute(
                "INSERT INTO context_ingest_receipts(receipt_ref,contract_type,principal_id,key_id,idempotency_key,"
                "payload_hash,source_event_ref,received_at,occurred_at,project_ref,observation_ref,result_status) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "receipt_71300001",
                    CONTEXT_RECEIPT_VERSION,
                    "principal_71300001",
                    "key_71300001",
                    "idem_71300001",
                    "b" * 64,
                    "sourceevent_71300001",
                    self.now.isoformat(timespec="seconds"),
                    self.now.isoformat(timespec="seconds"),
                    self.project,
                    "observation_71300001",
                    "draft_generated",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        upgraded = ensure_ready(legacy_db)
        try:
            columns = {row[1] for row in upgraded.execute("PRAGMA table_info(context_ingest_receipts)")}
            self.assertIn("source_ref", columns)
            row = upgraded.execute(
                "SELECT source_ref,receipt_ref,result_status FROM context_ingest_receipts WHERE receipt_ref=?",
                ("receipt_71300001",),
            ).fetchone()
            self.assertEqual(tuple(row), ("source_71300001", "receipt_71300001", "draft_generated"))
            unique_scopes = []
            for index in upgraded.execute("PRAGMA index_list(context_ingest_receipts)"):
                if int(index[2] or 0) == 1:
                    unique_scopes.append(
                        tuple(item[2] for item in upgraded.execute(f"PRAGMA index_info('{index[1]}')"))
                    )
            self.assertIn(
                ("contract_type", "principal_id", "source_ref", "idempotency_key"),
                unique_scopes,
            )
        finally:
            upgraded.close()

        repeated = ensure_ready(legacy_db)
        try:
            self.assertEqual(
                repeated.execute(
                    "SELECT COUNT(*) FROM context_ingest_receipts WHERE receipt_ref='receipt_71300001'"
                ).fetchone()[0],
                1,
            )
            self.assertFalse(
                repeated.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_ingest_receipts_scope_v2'"
                ).fetchone()
            )
        finally:
            repeated.close()

    def test_local_only_target_rejects_external_or_unscoped_ack_principal(self) -> None:
        prepared = self._prepare_one_delivery(idempotency_key="idem_80000001")

        external_name = "legacy_external_consumer"
        external_secret = b"legacy-external-consumer"
        self.secrets[external_name] = external_secret
        self.principals[external_name] = (
            "principal_80808080",
            "key_80808080",
            "bundle_consumer",
        )
        self.conn.execute(
            "INSERT INTO context_principals(principal_id,key_id,principal_kind,project_ref,target_ref,target_scope,"
            "key_fingerprint,status,created_at) VALUES(?,?,?,?,?,?,?,'active',?)",
            (
                self.principals[external_name][0],
                self.principals[external_name][1],
                "bundle_consumer",
                self.project,
                self.target,
                "external",
                hashlib.sha256(external_secret).hexdigest(),
                self.now.isoformat(timespec="seconds"),
            ),
        )
        unscoped_name = "legacy_unscoped_consumer"
        unscoped_secret = b"legacy-unscoped-consumer"
        self.secrets[unscoped_name] = unscoped_secret
        self.principals[unscoped_name] = (
            "principal_81818181",
            "key_81818181",
            "bundle_consumer",
        )
        self.conn.execute(
            "INSERT INTO context_principals(principal_id,key_id,principal_kind,project_ref,target_ref,target_scope,"
            "key_fingerprint,status,created_at) VALUES(?,?,?,?,?,?,?,'active',?)",
            (
                self.principals[unscoped_name][0],
                self.principals[unscoped_name][1],
                "bundle_consumer",
                "",
                "",
                "",
                hashlib.sha256(unscoped_secret).hexdigest(),
                self.now.isoformat(timespec="seconds"),
            ),
        )
        self.conn.commit()

        external_ack = self._delivery_payload(
            principal_name=external_name,
            delivery_ref=prepared["delivery_ref"],
            bundle_hash=prepared["bundle_hash"],
            transition="consumed",
            idempotency_key="idem_80000002",
            provider_receipt_ref="provider_80000002",
        )
        external_result = ingest_delivery_receipt(
            self.conn,
            self._wire(external_ack),
            verification_secret=external_secret,
            now=self.now,
        )
        self.assertEqual(
            (external_result["status"], external_result["reason_code"]),
            ("rejected", "principal_target_scope_mismatch"),
        )

        unscoped_ack = self._delivery_payload(
            principal_name=unscoped_name,
            delivery_ref=prepared["delivery_ref"],
            bundle_hash=prepared["bundle_hash"],
            transition="consumed",
            idempotency_key="idem_80000003",
            provider_receipt_ref="provider_80000003",
        )
        unscoped_result = ingest_delivery_receipt(
            self.conn,
            self._wire(unscoped_ack),
            verification_secret=unscoped_secret,
            now=self.now,
        )
        self.assertEqual(
            (unscoped_result["status"], unscoped_result["reason_code"]),
            ("rejected", "delivery_principal_project_required"),
        )
        row = self.conn.execute(
            "SELECT state,consumer_receipt_ref FROM context_deliveries WHERE delivery_ref=?",
            (prepared["delivery_ref"],),
        ).fetchone()
        self.assertEqual(tuple(row), ("prepared", ""))
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM context_delivery_receipts WHERE delivery_ref=? AND transition='consumed'",
                (prepared["delivery_ref"],),
            ).fetchone()[0],
            0,
        )

    def test_delivery_replay_rechecks_terminal_attempt_and_bundle_liveness(self) -> None:
        accepted = self._ingest_trusted_confirmation(
            idempotency_key="idem_81000001",
            source_ref="source_81000001",
        )
        prepared = prepare_delivery(
            self.conn,
            target_ref=self.target,
            project_ref=self.project,
            now=self.now,
        )
        ack = self._delivery_payload(
            principal_name="consumer",
            delivery_ref=prepared["delivery_ref"],
            bundle_hash=prepared["bundle_hash"],
            transition="consumed",
            idempotency_key="idem_81000002",
            provider_receipt_ref="provider_81000002",
        )
        first = ingest_delivery_receipt(
            self.conn,
            self._wire(ack),
            verification_secret=self.secrets["consumer"],
            now=self.now,
        )
        self.assertEqual((first["status"], first["state"]), ("accepted", "consumed"))

        deleted = delete_context_observation(
            self.conn,
            observation_ref=accepted["observation_ref"],
            reason_code="user_deleted",
            now=self.now,
        )
        self.assertEqual(deleted["status"], "completed")
        replay = ingest_delivery_receipt(
            self.conn,
            self._wire(ack),
            verification_secret=self.secrets["consumer"],
            now=self.now,
        )
        self.assertEqual(
            (replay["status"], replay["reason_code"]),
            ("rejected", "delivery_replay_stale"),
        )
        delivery = self.conn.execute(
            "SELECT state,failure_reason_code FROM context_deliveries WHERE delivery_ref=?",
            (prepared["delivery_ref"],),
        ).fetchone()
        self.assertEqual(tuple(delivery), ("failed", "source_revoked"))
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_bundle_manifests WHERE bundle_ref=?",
                (prepared["bundle_ref"],),
            ).fetchone()[0],
            "restricted",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM context_delivery_receipts WHERE delivery_ref=? AND transition='consumed'",
                (prepared["delivery_ref"],),
            ).fetchone()[0],
            1,
        )

    def test_bundle_and_frozen_payload_have_no_internal_database_ids(self) -> None:
        accepted = self._ingest_trusted_confirmation(
            idempotency_key="idem_82000001",
            source_ref="source_82000001",
        )
        bundle = build_context_bundle(self.conn, project_ref=self.project, now=self.now)
        prepared = prepare_delivery(
            self.conn,
            target_ref=self.target,
            project_ref=self.project,
            now=self.now,
        )
        fetched = fetch_prepared_delivery(
            self.conn,
            bundle_ref=prepared["bundle_ref"],
            now=self.now,
        )
        self.assertEqual(fetched["status"], "available")
        forbidden_keys = {
            "verified_ref",
            "candidate_ref",
            "observation_ref",
            "evidence_ref",
            "receipt_ref",
            "provenance_ref",
            "rowid",
            "raw_json",
            "diagnostic_json",
            "absolute_path",
            "deletion_group",
        }
        internal_prefixes = (
            "verified_",
            "candidate_",
            "observation_",
            "evidence_",
            "receipt_",
            "withdrawal_",
        )

        def assert_no_internal_database_ids(value) -> None:
            if isinstance(value, dict):
                self.assertTrue(forbidden_keys.isdisjoint(value), value)
                for child in value.values():
                    assert_no_internal_database_ids(child)
            elif isinstance(value, list):
                for child in value:
                    assert_no_internal_database_ids(child)
            elif isinstance(value, str):
                self.assertFalse(value.startswith(internal_prefixes), value)

        public_objects = (bundle, prepared["frozen_payload"], fetched["frozen_payload"])
        for public_object in public_objects:
            assert_no_internal_database_ids(public_object)
            encoded = json.dumps(public_object, ensure_ascii=False, sort_keys=True)
            self.assertNotIn(accepted["verified_ref"], encoded)
        public_ref = fetched["frozen_payload"]["verified_contexts"][0]["context_ref"]
        self.assertTrue(public_ref.startswith("context_"))
        member = self.conn.execute(
            "SELECT member_ref FROM context_bundle_members WHERE bundle_ref=?",
            (prepared["bundle_ref"],),
        ).fetchone()[0]
        self.assertEqual(member, accepted["verified_ref"])
        self.assertNotEqual(member, public_ref)

    def test_memory_scope_revocation_removes_candidate_from_counts_and_outputs(self) -> None:
        accepted = self._ingest_memory_confirmation(
            idempotency_key="idem_83000001",
            source_ref="source_83000001",
        )
        created = create_memory_candidate(
            self.conn,
            verified_ref=accepted["verified_ref"],
            subject_ref=self.owner,
            now=self.now,
        )
        self.assertEqual(created["status"], "candidate_only")
        self.assertTrue(created["context_ref"].startswith("context_"))
        self.assertNotEqual(created["context_ref"], accepted["verified_ref"])
        self.assertEqual(
            context_console_status(self.conn, now=self.now)["counts"]["memory_candidates"],
            1,
        )

        self.conn.execute(
            "UPDATE context_verified SET verification_scope='operational',memory_eligible=0 WHERE verified_ref=?",
            (accepted["verified_ref"],),
        )
        self.conn.commit()
        self.assertEqual(
            context_console_status(self.conn, now=self.now)["counts"]["memory_candidates"],
            0,
        )
        replay = create_memory_candidate(
            self.conn,
            verified_ref=accepted["verified_ref"],
            subject_ref=self.owner,
            now=self.now,
        )
        self.assertEqual(
            (replay["status"], replay["reason_code"]),
            ("rejected", "memory_scope_not_eligible"),
        )
        self.assertNotIn("context_ref", replay)
        terminal = self.conn.execute(
            "SELECT status,revocation_reason_code FROM context_memory_candidates WHERE memory_candidate_ref=?",
            (created["memory_candidate_ref"],),
        ).fetchone()
        self.assertEqual(tuple(terminal), ("revoked", "memory_scope_not_eligible"))

    def test_bundle_hash_is_stable_across_time_and_insertion_order(self) -> None:
        self._ingest_trusted_confirmation(
            idempotency_key="idem_84000001",
            source_ref="source_84000001",
            occurred_at=self.now - timedelta(seconds=2),
        )
        self._ingest_trusted_confirmation(
            idempotency_key="idem_84000002",
            source_ref="source_84000002",
            occurred_at=self.now - timedelta(seconds=1),
        )
        first = build_context_bundle(
            self.conn,
            project_ref=self.project,
            include_clues=False,
            now=self.now,
        )
        selected = context_console_module._select_verified_contexts(
            self.conn,
            project_ref=self.project,
            start_at="",
            end_at="",
            query="",
            limit=100,
            now=self.now + timedelta(hours=1),
        )
        self.assertEqual(len(selected), 2)
        with mock.patch.object(
            context_console_module,
            "_select_verified_contexts",
            return_value=list(reversed(selected)),
        ):
            second = build_context_bundle(
                self.conn,
                project_ref=self.project,
                include_clues=False,
                now=self.now + timedelta(hours=1),
            )
        self.assertNotEqual(first["generated_at"], second["generated_at"])
        self.assertEqual(first["bundle_hash"], second["bundle_hash"])
        self.assertEqual(first["frozen_payload"], second["frozen_payload"])
        self.assertEqual(
            [item["context_ref"] for item in first["verified_contexts"]],
            [item["context_ref"] for item in second["verified_contexts"]],
        )

    def test_prepared_delivery_exposes_exact_frozen_payload_by_bundle_ref(self) -> None:
        self._ingest_ai(
            idempotency_key="idem_85000001",
            source_ref="source_85000001",
        )
        prepared = prepare_delivery(
            self.conn,
            target_ref=self.target,
            project_ref=self.project,
            now=self.now,
        )
        fetched = fetch_prepared_delivery(
            self.conn,
            bundle_ref=prepared["bundle_ref"],
            now=self.now,
        )
        self.assertEqual(fetched["status"], "available")
        self.assertEqual(fetched["bundle_ref"], prepared["bundle_ref"])
        self.assertEqual(fetched["bundle_hash"], prepared["bundle_hash"])
        self.assertEqual(fetched["frozen_payload"], prepared["frozen_payload"])
        self.assertNotIn("delivery_ref", fetched)
        exact_attempt = fetch_prepared_delivery(
            self.conn,
            delivery_ref=prepared["delivery_ref"],
            bundle_ref=prepared["bundle_ref"],
            now=self.now,
        )
        self.assertEqual(exact_attempt["frozen_payload"], prepared["frozen_payload"])
        self.assertEqual(exact_attempt["attempt_no"], 1)
        mismatch = fetch_prepared_delivery(
            self.conn,
            delivery_ref=prepared["delivery_ref"],
            bundle_ref="bundle_ffffffffffffffffffffffffffffffff",
            now=self.now,
        )
        self.assertEqual(
            (mismatch["status"], mismatch["reason_code"]),
            ("rejected", "delivery_bundle_mismatch"),
        )

    def test_frozen_payload_hash_round_trip(self) -> None:
        self._ingest_ai(
            idempotency_key="idem_86000001",
            source_ref="source_86000001",
        )
        prepared = prepare_delivery(
            self.conn,
            target_ref=self.target,
            project_ref=self.project,
            now=self.now,
        )
        fetched = fetch_prepared_delivery(
            self.conn,
            delivery_ref=prepared["delivery_ref"],
            now=self.now,
        )
        canonical = json.dumps(
            fetched["frozen_payload"],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        recomputed = "sha256:" + hashlib.sha256(canonical).hexdigest()
        self.assertEqual(recomputed, fetched["bundle_hash"])
        manifest = self.conn.execute(
            "SELECT bundle_hash,stable_projection_json,frozen_payload_json FROM context_bundle_manifests "
            "WHERE bundle_ref=?",
            (prepared["bundle_ref"],),
        ).fetchone()
        self.assertEqual(manifest["bundle_hash"], recomputed)
        self.assertEqual(
            json.loads(manifest["stable_projection_json"]),
            fetched["frozen_payload"],
        )
        self.assertEqual(
            json.loads(manifest["frozen_payload_json"]),
            fetched["frozen_payload"],
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT bundle_hash FROM context_deliveries WHERE delivery_ref=?",
                (prepared["delivery_ref"],),
            ).fetchone()[0],
            recomputed,
        )

    def test_target_privacy_policy_matrix_local_and_external(self) -> None:
        undeclared_target = "target_90909090"
        undeclared = register_context_label(
            self.conn,
            kind="target",
            ref=undeclared_target,
            label="Undeclared Target",
            project_ref=self.project,
        )
        self.assertEqual(
            (undeclared["status"], undeclared["reason_code"]),
            ("rejected", "target_policy_required"),
        )
        private_context = self._ingest_trusted_confirmation(
            idempotency_key="idem_87000001",
            source_ref="source_87000001",
        )
        restricted_context = self._ingest_controlled(
            principal_name="user",
            evidence_kind="user_confirmation",
            state="completed_confirmed",
            idempotency_key="idem_87000002",
            source_ref="source_87000002",
            privacy_class="restricted_local",
            verification_scope="operational",
            memory_eligible=False,
        )
        local = prepare_delivery(
            self.conn,
            target_ref=self.target,
            project_ref=self.project,
            now=self.now,
        )
        self.assertEqual(local["status"], "prepared")
        self.assertEqual((local["target_scope"], local["export_policy"]), ("local", "local_only"))
        self.assertIn(
            "restricted_local",
            {
                item["privacy_class"]
                for item in local["frozen_payload"]["verified_contexts"]
            },
        )

        bad_external_target = "target_91919191"
        bad_external = register_context_label(
            self.conn,
            kind="target",
            ref=bad_external_target,
            label="Unsafe Export",
            project_ref=self.project,
            target_scope="external",
            allowed_privacy_classes=["private_local", "restricted_local"],
            export_policy="explicit_export",
        )
        self.assertEqual(
            (bad_external["status"], bad_external["reason_code"]),
            ("rejected", "restricted_local_export_forbidden"),
        )
        external_target = "target_92929292"
        self._register_label(
            "target",
            external_target,
            "Explicit Export",
            target_scope="external",
            allowed_privacy_classes=["private_local", "sensitive_local"],
            export_policy="explicit_export",
        )
        blocked = prepare_delivery(
            self.conn,
            target_ref=external_target,
            project_ref=self.project,
            now=self.now,
        )
        self.assertEqual(
            (blocked["status"], blocked["reason_code"]),
            ("rejected", "target_privacy_policy_forbidden"),
        )
        deleted = delete_context_observation(
            self.conn,
            observation_ref=restricted_context["observation_ref"],
            reason_code="user_deleted",
            now=self.now,
        )
        self.assertEqual(deleted["status"], "completed")
        external = prepare_delivery(
            self.conn,
            target_ref=external_target,
            project_ref=self.project,
            now=self.now,
        )
        self.assertEqual(external["status"], "prepared")
        self.assertEqual(
            (external["target_scope"], external["export_policy"]),
            ("external", "explicit_export"),
        )
        self.assertTrue(external["privacy_policy_hash"].startswith("sha256:"))
        self.assertEqual(
            {item["source_ref"] for item in external["frozen_payload"]["verified_contexts"]},
            {"source_87000001"},
        )
        self.assertNotIn(
            "restricted_local",
            json.dumps(external["frozen_payload"], ensure_ascii=False),
        )

    def test_failed_delivery_creates_new_monotonic_attempt(self) -> None:
        prepared = self._prepare_one_delivery(idempotency_key="idem_88000001")
        failure = self._delivery_payload(
            principal_name="consumer",
            delivery_ref=prepared["delivery_ref"],
            bundle_hash=prepared["bundle_hash"],
            transition="failed",
            idempotency_key="idem_88000002",
            failure_reason_code="consumer_rejected",
        )
        failed = ingest_delivery_receipt(
            self.conn,
            self._wire(failure),
            verification_secret=self.secrets["consumer"],
            now=self.now,
        )
        self.assertEqual((failed["status"], failed["state"]), ("accepted", "failed"))
        retry = prepare_delivery(
            self.conn,
            target_ref=self.target,
            project_ref=self.project,
            now=self.now,
        )
        self.assertEqual((retry["status"], retry["attempt_no"]), ("prepared", 2))
        self.assertNotEqual(retry["delivery_ref"], prepared["delivery_ref"])
        rows = self.conn.execute(
            "SELECT attempt_no,state,delivery_ref FROM context_deliveries WHERE project_ref=? AND target_ref=? "
            "ORDER BY attempt_no",
            (self.project, self.target),
        ).fetchall()
        self.assertEqual([(row[0], row[1]) for row in rows], [(1, "failed"), (2, "prepared")])
        self.assertEqual(rows[0][2], prepared["delivery_ref"])
        self.assertEqual(rows[1][2], retry["delivery_ref"])

    def test_old_ack_cannot_cross_delivery_attempt(self) -> None:
        prepared = self._prepare_one_delivery(idempotency_key="idem_89000001")
        old_ack = self._delivery_payload(
            principal_name="consumer",
            delivery_ref=prepared["delivery_ref"],
            bundle_hash=prepared["bundle_hash"],
            transition="consumed",
            idempotency_key="idem_89000002",
            provider_receipt_ref="provider_89000002",
        )
        failure = self._delivery_payload(
            principal_name="consumer",
            delivery_ref=prepared["delivery_ref"],
            bundle_hash=prepared["bundle_hash"],
            transition="failed",
            idempotency_key="idem_89000003",
            failure_reason_code="consumer_rejected",
        )
        self.assertEqual(
            ingest_delivery_receipt(
                self.conn,
                self._wire(failure),
                verification_secret=self.secrets["consumer"],
                now=self.now,
            )["state"],
            "failed",
        )
        retry = prepare_delivery(
            self.conn,
            target_ref=self.target,
            project_ref=self.project,
            now=self.now,
        )
        rejected_old = ingest_delivery_receipt(
            self.conn,
            self._wire(old_ack),
            verification_secret=self.secrets["consumer"],
            now=self.now,
        )
        self.assertEqual(
            (rejected_old["status"], rejected_old["reason_code"]),
            ("rejected", "illegal_delivery_transition"),
        )
        crossed = copy.deepcopy(old_ack)
        crossed["delivery_ref"] = retry["delivery_ref"]
        crossed["idempotency_key"] = "idem_89000004"
        crossed["signature"] = sign_contract_payload(crossed, self.secrets["consumer"])
        rejected_cross = ingest_delivery_receipt(
            self.conn,
            self._wire(crossed),
            verification_secret=self.secrets["consumer"],
            now=self.now,
        )
        self.assertEqual(
            (rejected_cross["status"], rejected_cross["reason_code"]),
            ("rejected", "delivery_attempt_no_mismatch"),
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT state FROM context_deliveries WHERE delivery_ref=?",
                (retry["delivery_ref"],),
            ).fetchone()[0],
            "prepared",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM context_delivery_receipts WHERE delivery_ref=? AND transition='consumed'",
                (retry["delivery_ref"],),
            ).fetchone()[0],
            0,
        )

    def test_delivery_manifest_restriction_blocks_consumed_and_delivered(self) -> None:
        prepared = self._prepare_one_delivery(idempotency_key="idem_90000001")
        consumed_payload = self._delivery_payload(
            principal_name="consumer",
            delivery_ref=prepared["delivery_ref"],
            bundle_hash=prepared["bundle_hash"],
            transition="consumed",
            idempotency_key="idem_90000002",
            provider_receipt_ref="provider_90000002",
        )
        self.conn.execute(
            "UPDATE context_bundle_manifests SET status='restricted',restricted_at=?,"
            "restriction_reason_code='synthetic_restriction' WHERE bundle_ref=?",
            (self.now.isoformat(timespec="seconds"), prepared["bundle_ref"]),
        )
        self.conn.commit()
        blocked_consume = ingest_delivery_receipt(
            self.conn,
            self._wire(consumed_payload),
            verification_secret=self.secrets["consumer"],
            now=self.now,
        )
        self.assertEqual(
            (blocked_consume["status"], blocked_consume["reason_code"]),
            ("rejected", "bundle_restricted"),
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT state FROM context_deliveries WHERE delivery_ref=?",
                (prepared["delivery_ref"],),
            ).fetchone()[0],
            "prepared",
        )
        self.conn.execute(
            "UPDATE context_bundle_manifests SET status='active',restricted_at=NULL,restriction_reason_code='' "
            "WHERE bundle_ref=?",
            (prepared["bundle_ref"],),
        )
        self.conn.commit()
        consumed = ingest_delivery_receipt(
            self.conn,
            self._wire(consumed_payload),
            verification_secret=self.secrets["consumer"],
            now=self.now,
        )
        self.assertEqual(consumed["state"], "consumed")
        delivered_payload = self._delivery_payload(
            principal_name="provider",
            delivery_ref=prepared["delivery_ref"],
            bundle_hash=prepared["bundle_hash"],
            transition="delivered",
            idempotency_key="idem_90000003",
            provider_receipt_ref="provider_90000003",
        )
        self.conn.execute(
            "UPDATE context_bundle_manifests SET status='restricted',restricted_at=?,"
            "restriction_reason_code='synthetic_restriction' WHERE bundle_ref=?",
            (self.now.isoformat(timespec="seconds"), prepared["bundle_ref"]),
        )
        self.conn.commit()
        blocked_deliver = ingest_delivery_receipt(
            self.conn,
            self._wire(delivered_payload),
            verification_secret=self.secrets["provider"],
            now=self.now,
        )
        self.assertEqual(
            (blocked_deliver["status"], blocked_deliver["reason_code"]),
            ("rejected", "bundle_restricted"),
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT state FROM context_deliveries WHERE delivery_ref=?",
                (prepared["delivery_ref"],),
            ).fetchone()[0],
            "consumed",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM context_delivery_receipts WHERE delivery_ref=? AND transition='delivered'",
                (prepared["delivery_ref"],),
            ).fetchone()[0],
            0,
        )

    def test_bundle_verified_and_clue_partitions_are_mutually_exclusive(self) -> None:
        accepted = self._ingest_trusted_confirmation(
            idempotency_key="idem_91000001",
            source_ref="source_91000001",
        )
        same_source_clue = record_unverified_clue(
            self.conn,
            source_kind="screen",
            source_ref="source_91000001",
            occurred_at=self.now.isoformat(timespec="seconds"),
            project_ref=self.project,
            workstream_ref=self.workstream,
            object_ref=self.object,
            version_ref=self.version,
            subject_ref=self.owner,
            privacy_class="private_local",
            now=self.now,
        )
        self.assertEqual(same_source_clue["status"], "accepted")
        unique_clue = record_unverified_clue(
            self.conn,
            source_kind="ocr",
            source_ref="source_91000002",
            occurred_at=self.now.isoformat(timespec="seconds"),
            project_ref=self.project,
            workstream_ref=self.workstream,
            object_ref=self.object,
            version_ref=self.version,
            subject_ref=self.owner,
            privacy_class="private_local",
            now=self.now,
        )
        self.assertEqual(unique_clue["status"], "accepted")
        bundle = build_context_bundle(
            self.conn,
            project_ref=self.project,
            include_clues=True,
            now=self.now,
        )
        verified_sources = {item["source_ref"] for item in bundle["verified_contexts"]}
        clue_sources = {item["source_ref"] for item in bundle["unverified_clues"]}
        self.assertIn("source_91000001", verified_sources)
        self.assertNotIn("source_91000001", clue_sources)
        self.assertIn("source_91000002", clue_sources)
        self.assertTrue(verified_sources.isdisjoint(clue_sources))
        prepared = prepare_delivery(
            self.conn,
            target_ref=self.target,
            project_ref=self.project,
            now=self.now,
        )
        self.assertEqual(prepared["frozen_payload"]["unverified_clues"], [])
        self.assertEqual(
            {item["source_ref"] for item in prepared["frozen_payload"]["verified_contexts"]},
            {"source_91000001"},
        )

    def test_bundle_public_projection_has_no_internal_or_raw_fields(self) -> None:
        accepted = self._ingest_trusted_confirmation(
            idempotency_key="idem_92000001",
            source_ref="source_92000001",
        )
        record_unverified_clue(
            self.conn,
            source_kind="screen",
            source_ref="source_92000002",
            occurred_at=self.now.isoformat(timespec="seconds"),
            project_ref=self.project,
            workstream_ref=self.workstream,
            object_ref=self.object,
            version_ref=self.version,
            subject_ref=self.owner,
            privacy_class="private_local",
            now=self.now,
        )
        bundle = build_context_bundle(
            self.conn,
            project_ref=self.project,
            include_clues=True,
            now=self.now,
        )
        frozen = bundle["frozen_payload"]
        self.assertEqual(
            set(frozen),
            {"contract_version", "raw_access", "filters", "verified_contexts", "unverified_clues"},
        )
        self.assertFalse(frozen["raw_access"])
        self.assertEqual(
            set(frozen["verified_contexts"][0]),
            {
                "context_ref",
                "source_ref",
                "state",
                "occurred_at",
                "expires_at",
                "revocation_state",
                "privacy_class",
                "project",
                "workstream",
                "object",
                "version",
                "subject",
                "source",
            },
        )
        forbidden_keys = {
            "verified_ref",
            "candidate_ref",
            "observation_ref",
            "evidence_ref",
            "receipt_ref",
            "provenance_ref",
            "rowid",
            "absolute_path",
            "url",
            "raw_json",
            "body",
            "frame",
            "deletion_group",
        }
        internal_prefixes = (
            "verified_",
            "candidate_",
            "observation_",
            "evidence_",
            "receipt_",
            "withdrawal_",
        )

        def assert_public(value) -> None:
            if isinstance(value, dict):
                self.assertTrue(forbidden_keys.isdisjoint(value))
                for child in value.values():
                    assert_public(child)
            elif isinstance(value, list):
                for child in value:
                    assert_public(child)
            elif isinstance(value, str):
                self.assertFalse(value.startswith(internal_prefixes), value)
                self.assertNotIn("/mnt/", value)
                self.assertNotIn("file://", value)
                self.assertNotIn("https://", value)

        assert_public(bundle)
        encoded = json.dumps(bundle, ensure_ascii=False, sort_keys=True)
        self.assertNotIn(accepted["verified_ref"], encoded)
        self.assertTrue(frozen["verified_contexts"][0]["context_ref"].startswith("context_"))

    def test_delivered_history_is_preserved_and_withdrawal_is_recorded(self) -> None:
        accepted = self._ingest_memory_confirmation(
            idempotency_key="idem_93000001",
            source_ref="source_93000001",
        )
        prepared = prepare_delivery(
            self.conn,
            target_ref=self.target,
            project_ref=self.project,
            now=self.now,
        )
        consumed_payload = self._delivery_payload(
            principal_name="consumer",
            delivery_ref=prepared["delivery_ref"],
            bundle_hash=prepared["bundle_hash"],
            transition="consumed",
            idempotency_key="idem_93000002",
            provider_receipt_ref="provider_93000002",
        )
        self.assertEqual(
            ingest_delivery_receipt(
                self.conn,
                self._wire(consumed_payload),
                verification_secret=self.secrets["consumer"],
                now=self.now,
            )["state"],
            "consumed",
        )
        delivered_payload = self._delivery_payload(
            principal_name="provider",
            delivery_ref=prepared["delivery_ref"],
            bundle_hash=prepared["bundle_hash"],
            transition="delivered",
            idempotency_key="idem_93000003",
            provider_receipt_ref="provider_93000003",
        )
        self.assertEqual(
            ingest_delivery_receipt(
                self.conn,
                self._wire(delivered_payload),
                verification_secret=self.secrets["provider"],
                now=self.now,
            )["state"],
            "delivered",
        )
        before = self.conn.execute(
            "SELECT state,delivered_at,provider_receipt_ref FROM context_deliveries WHERE delivery_ref=?",
            (prepared["delivery_ref"],),
        ).fetchone()
        deleted = delete_context_observation(
            self.conn,
            observation_ref=accepted["observation_ref"],
            reason_code="user_deleted",
            now=self.now,
        )
        self.assertEqual(deleted["status"], "completed")
        after = self.conn.execute(
            "SELECT state,delivered_at,provider_receipt_ref FROM context_deliveries WHERE delivery_ref=?",
            (prepared["delivery_ref"],),
        ).fetchone()
        self.assertEqual(tuple(after), tuple(before))
        self.assertEqual(after["state"], "delivered")
        withdrawal = self.conn.execute(
            "SELECT status,reason_code FROM context_withdrawal_receipts WHERE delivery_ref=?",
            (prepared["delivery_ref"],),
        ).fetchone()
        self.assertEqual(tuple(withdrawal), ("local_restriction_recorded", "user_deleted"))
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_bundle_manifests WHERE bundle_ref=?",
                (prepared["bundle_ref"],),
            ).fetchone()[0],
            "restricted",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_bundle_members WHERE bundle_ref=?",
                (prepared["bundle_ref"],),
            ).fetchone()[0],
            "revoked",
        )
        transitions = [
            row[0]
            for row in self.conn.execute(
                "SELECT transition FROM context_delivery_receipts WHERE delivery_ref=? ORDER BY created_at,transition",
                (prepared["delivery_ref"],),
            ).fetchall()
        ]
        self.assertEqual(sorted(transitions), ["consumed", "delivered", "prepared"])
        self.assertEqual(build_context_bundle(self.conn, now=self.now)["verified_contexts"], [])


    def test_v2_delivery_scope_metadata_requires_explicit_one_way_rebinding(self) -> None:
        self._ingest_ai(
            idempotency_key="idem_94000001",
            source_ref="source_94000001",
        )
        legacy_attempt = prepare_delivery(
            self.conn,
            target_ref=self.target,
            project_ref=self.project,
            now=self.now,
        )
        self.assertEqual(
            (legacy_attempt["status"], legacy_attempt["attempt_no"]),
            ("prepared", 1),
        )

        self.conn.execute(
            "UPDATE context_target_policies SET policy_declared=0 WHERE target_ref=?",
            (self.target,),
        )
        consumer_id, consumer_key, consumer_kind = self.principals["consumer"]
        self.conn.execute(
            "UPDATE context_principals SET project_ref='',target_ref='',target_scope='' "
            "WHERE principal_id=? AND key_id=?",
            (consumer_id, consumer_key),
        )
        self.conn.execute(
            "UPDATE context_deliveries SET project_ref='',allowed_privacy_classes_json='[]',"
            "privacy_policy_hash='' WHERE delivery_ref=?",
            (legacy_attempt["delivery_ref"],),
        )
        self.conn.commit()

        context_console_module.init_context_console_schema(self.conn)
        self.conn.commit()
        migrated_attempt = self.conn.execute(
            "SELECT project_ref,state,failure_reason_code FROM context_deliveries WHERE delivery_ref=?",
            (legacy_attempt["delivery_ref"],),
        ).fetchone()
        self.assertEqual(
            tuple(migrated_attempt),
            (self.project, "failed", "delivery_binding_upgrade_required"),
        )

        blocked = prepare_delivery(
            self.conn,
            target_ref=self.target,
            project_ref=self.project,
            now=self.now,
        )
        self.assertEqual(
            (blocked["status"], blocked["reason_code"]),
            ("rejected", "target_policy_undeclared"),
        )

        declared = register_context_label(
            self.conn,
            kind="target",
            ref=self.target,
            label="Local Handoff",
            project_ref=self.project,
            target_scope="local",
            allowed_privacy_classes=[
                "private_local",
                "sensitive_local",
                "restricted_local",
            ],
            export_policy="local_only",
        )
        self.assertEqual(
            (declared["status"], declared["reason_code"]),
            ("registered", "target_policy_declared"),
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT policy_declared FROM context_target_policies WHERE target_ref=?",
                (self.target,),
            ).fetchone()[0],
            1,
        )

        rebound = register_context_principal(
            self.conn,
            principal_id=consumer_id,
            key_id=consumer_key,
            principal_kind=consumer_kind,
            verification_secret=self.secrets["consumer"],
            project_ref=self.project,
            target_ref=self.target,
            target_scope="local",
        )
        self.assertEqual(
            (rebound["status"], rebound["reason_code"]),
            ("registered", "delivery_scope_bound"),
        )
        principal_row = self.conn.execute(
            "SELECT project_ref,target_ref,target_scope FROM context_principals "
            "WHERE principal_id=? AND key_id=?",
            (consumer_id, consumer_key),
        ).fetchone()
        self.assertEqual(tuple(principal_row), (self.project, self.target, "local"))

        replay = register_context_principal(
            self.conn,
            principal_id=consumer_id,
            key_id=consumer_key,
            principal_kind=consumer_kind,
            verification_secret=self.secrets["consumer"],
            project_ref=self.project,
            target_ref=self.target,
            target_scope="local",
        )
        self.assertEqual(
            (replay["status"], replay["reason_code"]),
            ("replayed", "idempotent_replay"),
        )
        rebound_elsewhere = register_context_principal(
            self.conn,
            principal_id=consumer_id,
            key_id=consumer_key,
            principal_kind=consumer_kind,
            verification_secret=self.secrets["consumer"],
            project_ref=self.project,
            target_ref=self.other_target,
            target_scope="local",
        )
        self.assertEqual(
            (rebound_elsewhere["status"], rebound_elsewhere["reason_code"]),
            ("conflict", "principal_registration_conflict"),
        )

        retried = prepare_delivery(
            self.conn,
            target_ref=self.target,
            project_ref=self.project,
            now=self.now,
        )
        self.assertEqual(
            (retried["status"], retried["attempt_no"]),
            ("prepared", 2),
        )
        self.assertNotEqual(retried["delivery_ref"], legacy_attempt["delivery_ref"])
        attempts = self.conn.execute(
            "SELECT attempt_no,state FROM context_deliveries WHERE project_ref=? AND target_ref=? "
            "ORDER BY attempt_no",
            (self.project, self.target),
        ).fetchall()
        self.assertEqual([tuple(row) for row in attempts], [(1, "failed"), (2, "prepared")])
    def _start_experiment(self, experiment_ref: str, start_local_date: str = "2026-07-01") -> None:
        started = date.fromisoformat(start_local_date)
        result = start_product_experiment(
            self.conn,
            experiment_ref=experiment_ref,
            confirmation="START_LOCAL_PRODUCT_EXPERIMENT",
            now=datetime(started.year, started.month, started.day, 12, 0, tzinfo=UTC),
        )
        self.assertTrue(result["experiment_started"])

    def _record_experiment_attempts(
        self,
        experiment_ref: str,
        attempts: list[tuple[bool | None, float | None]],
        *,
        event_prefix: str,
    ) -> None:
        for index, (useful, resume_seconds) in enumerate(attempts, start=1):
            result = record_product_experiment_attempt(
                self.conn,
                experiment_ref=experiment_ref,
                local_date=f"2026-07-{index:02d}",
                event_id=f"event_{event_prefix}{index:04x}",
                useful=useful,
                resume_seconds=resume_seconds,
            )
            self.assertEqual(result["status"], "recorded")

    def test_product_experiment_start_uses_configured_natural_day_and_requires_core_gate(
        self,
    ) -> None:
        configured = configure_context_local_timezone(
            self.conn,
            timezone_name="Asia/Shanghai",
        )
        self.assertEqual(configured["status"], "configured")
        disabled = configure_context_console(self.conn, enabled=False)
        self.assertEqual(disabled["status"], "disabled")
        boundary = datetime(2026, 7, 1, 16, 0, tzinfo=UTC)
        blocked = start_product_experiment(
            self.conn,
            experiment_ref="experiment_e0000001",
            confirmation="START_LOCAL_PRODUCT_EXPERIMENT",
            now=boundary,
        )
        self.assertEqual((blocked["status"], blocked["reason_code"]), ("disabled", "feature_flag_off"))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM context_product_experiments").fetchone()[0],
            0,
        )

        enabled = configure_context_console(
            self.conn,
            enabled=True,
            confirmation=CONTEXT_FEATURE_CONFIRMATION,
        )
        self.assertEqual(enabled["status"], "enabled")
        started = start_product_experiment(
            self.conn,
            experiment_ref="experiment_e0000001",
            confirmation="START_LOCAL_PRODUCT_EXPERIMENT",
            now=boundary,
        )
        self.assertTrue(started["experiment_started"])
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT started_local_date,timezone_name,status "
                    "FROM context_product_experiments WHERE experiment_ref=?",
                    ("experiment_e0000001",),
                ).fetchone()
            ),
            ("2026-07-02", "Asia/Shanghai", "running"),
        )

    def test_product_experiment_start_refreshes_time_after_write_lock_crosses_midnight(
        self,
    ) -> None:
        configured = configure_context_local_timezone(
            self.conn,
            timezone_name="Asia/Shanghai",
        )
        self.assertEqual(configured["status"], "configured")
        before_midnight = datetime(2026, 7, 29, 15, 59, 59, tzinfo=UTC)
        after_midnight = datetime(2026, 7, 29, 16, 0, 1, tzinfo=UTC)
        clock = {"now": before_midnight}
        reached_begin = threading.Event()
        result: dict[str, Any] = {}
        errors: list[BaseException] = []
        worker_connection = sqlite3.connect(
            self.db,
            timeout=10.0,
            check_same_thread=False,
        )
        worker_connection.row_factory = sqlite3.Row
        worker_connection.execute("PRAGMA busy_timeout = 10000")
        worker_connection.execute("PRAGMA foreign_keys = ON")
        blocker = ensure_ready(self.db)
        blocker.execute("BEGIN IMMEDIATE")
        original_aware_utc = context_console_module._aware_utc

        class SignalingConnection:
            def __init__(inner_self, wrapped: sqlite3.Connection) -> None:
                inner_self.wrapped = wrapped

            def execute(inner_self, sql, *args, **kwargs):
                if str(sql).strip().upper() == "BEGIN IMMEDIATE":
                    reached_begin.set()
                return inner_self.wrapped.execute(sql, *args, **kwargs)

            def __getattr__(inner_self, name):
                return getattr(inner_self.wrapped, name)

        def fake_aware_utc(value):
            return clock["now"] if value is None else original_aware_utc(value)

        def worker() -> None:
            try:
                result.update(
                    start_product_experiment(
                        SignalingConnection(worker_connection),
                        experiment_ref="experiment_e0000002",
                        confirmation="START_LOCAL_PRODUCT_EXPERIMENT",
                    )
                )
            except BaseException as exc:  # pragma: no cover - diagnostic only
                errors.append(exc)
            finally:
                worker_connection.close()

        thread = threading.Thread(target=worker)
        reached = False
        try:
            with mock.patch.object(
                context_console_module,
                "_aware_utc",
                side_effect=fake_aware_utc,
            ):
                thread.start()
                reached = reached_begin.wait(5)
                if reached:
                    clock["now"] = after_midnight
                blocker.commit()
                thread.join(10)
        finally:
            if blocker.in_transaction:
                blocker.rollback()
            blocker.close()
            if thread.is_alive():
                thread.join(10)

        self.assertTrue(reached, "start did not wait for BEGIN IMMEDIATE")
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(result["experiment_started"])
        self.assertEqual(result["started_local_date"], "2026-07-30")
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT started_local_date,timezone_name,status,created_at "
                    "FROM context_product_experiments WHERE experiment_ref=?",
                    ("experiment_e0000002",),
                ).fetchone()
            ),
            (
                "2026-07-30",
                "Asia/Shanghai",
                "running",
                after_midnight.isoformat(timespec="seconds"),
            ),
        )

    def test_product_experiment_legacy_metric_writers_are_not_public_cli(self) -> None:
        commands = (
            [
                "record-experiment-attempt",
                "--db",
                str(self.db),
                "--experiment-ref",
                "experiment_e0000101",
                "--local-date",
                "2099-01-01",
                "--event-id",
                "event_e0000101",
                "--useful",
                "true",
                "--resume-seconds",
                "1",
            ],
            [
                "record-win",
                "--db",
                str(self.db),
                "--win-ref",
                "win_e0000101",
                "--occurred-at",
                "2099-01-01T00:00:00Z",
                "--project-ref",
                self.project,
                "--outcome-code",
                "recovered_and_resumed",
                "--source-ref",
                "source_e0000101",
            ],
            [
                "start-experiment",
                "--db",
                str(self.db),
                "--experiment-ref",
                "experiment_e0000101",
                "--start-local-date",
                "2000-01-01",
                "--confirm",
                "START_LOCAL_PRODUCT_EXPERIMENT",
            ],
        )
        for arguments in commands:
            completed = subprocess.run(
                [sys.executable, "-m", "activity_mvp", "context-console", *arguments],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 2, completed.stderr)
            self.assertEqual(completed.stdout, "")
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT "
                    "(SELECT COUNT(*) FROM context_product_experiments),"
                    "(SELECT COUNT(*) FROM context_product_experiment_attempts),"
                    "(SELECT COUNT(*) FROM context_product_wins)"
                ).fetchone()
            ),
            (0, 0, 0),
        )

    def test_product_experiment_public_status_is_strict_and_read_only(self) -> None:
        self._start_experiment("experiment_e0001001")
        for index in range(1, 11):
            self.conn.execute(
                "INSERT INTO context_product_experiment_attempts("
                "attempt_ref,experiment_ref,event_id,local_date,useful,outcome,resume_seconds,source_ref,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    f"attempt_e000{index:04x}",
                    "experiment_e0001001",
                    f"event_e000{index:04x}",
                    f"2026-07-{index:02d}",
                    1 if index <= 8 else 0,
                    "useful" if index <= 8 else "not_useful",
                    20.0,
                    f"source_e000{index:04x}",
                    self.now.isoformat(timespec="seconds"),
                ),
            )
        self.conn.commit()
        stored_before = tuple(
            self.conn.execute(
                "SELECT status,ended_local_date,updated_at FROM context_product_experiments "
                "WHERE experiment_ref='experiment_e0001001'"
            ).fetchone()
        )

        internal = product_experiment_status(
            self.conn,
            experiment_ref="experiment_e0001001",
            local_today=date(2026, 7, 14),
        )
        public = public_product_experiment_status(
            self.conn,
            now=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
        )
        self.assertEqual(internal["status"], "passed")
        self.assertEqual(
            set(public),
            {
                "contract_version",
                "read_state",
                "observed_at",
                "status",
                "experiment_started",
                "experiment_ref",
                "attempt_count",
                "complete_attempt_count",
                "useful_rate",
                "median_resume_seconds",
                "raw_access",
            },
        )
        self.assertEqual(public["contract_version"], PRODUCT_EXPERIMENT_STATUS_VERSION)
        self.assertEqual((public["read_state"], public["status"]), ("available", "passed"))
        self.assertIsNone(public["experiment_ref"])
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT status,ended_local_date,updated_at FROM context_product_experiments "
                    "WHERE experiment_ref='experiment_e0001001'"
                ).fetchone()
            ),
            stored_before,
        )

        denied = finalize_product_experiment(
            self.conn,
            experiment_ref="experiment_e0001001",
            confirmation="",
            now=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
        )
        self.assertEqual(
            (denied["status"], denied["reason_code"]),
            ("rejected", "experiment_finalize_confirmation_required"),
        )
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT status,ended_local_date,updated_at FROM context_product_experiments "
                    "WHERE experiment_ref='experiment_e0001001'"
                ).fetchone()
            ),
            stored_before,
        )

        finalized = finalize_product_experiment(
            self.conn,
            experiment_ref="experiment_e0001001",
            confirmation=PRODUCT_EXPERIMENT_FINALIZE_CONFIRMATION,
            now=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
        )
        self.assertEqual((finalized["finalization_status"], finalized["status"]), ("finalized", "passed"))
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT status,ended_local_date,updated_at FROM context_product_experiments "
                    "WHERE experiment_ref='experiment_e0001001'"
                ).fetchone()
            ),
            ("passed", "2026-07-14", "2026-07-14T12:00:00+00:00"),
        )

    def test_product_experiment_public_unknown_and_nonavailable_are_null_partitioned(self) -> None:
        self._start_experiment("experiment_e0002001")

        before_start = public_product_experiment_status(
            self.conn,
            now=datetime(2026, 6, 30, 12, 0, tzinfo=UTC),
        )
        self.assertEqual(
            (before_start["read_state"], before_start["status"]),
            ("available", "unknown"),
        )
        self.assertIsNone(before_start["experiment_ref"])

        self.conn.execute(
            "UPDATE context_product_experiments SET experiment_ref=? "
            "WHERE experiment_ref='experiment_e0002001'",
            ("/Users/test/private/resume.txt",),
        )
        self.conn.commit()
        invalid_ref = public_product_experiment_status(self.conn, now=self.now)
        self.assertEqual(
            (invalid_ref["read_state"], invalid_ref["status"]),
            ("available", "unknown"),
        )
        self.assertIsNone(invalid_ref["experiment_ref"])
        self.assertNotIn("/Users/test", json.dumps(invalid_ref, sort_keys=True))
        self.conn.execute(
            "UPDATE context_product_experiments SET experiment_ref='experiment_e0002001' "
            "WHERE experiment_ref=?",
            ("/Users/test/private/resume.txt",),
        )
        self.conn.execute(
            "UPDATE context_product_experiments SET timezone_name='Not/A_Zone' "
            "WHERE experiment_ref='experiment_e0002001'"
        )
        self.conn.commit()
        unknown = public_product_experiment_status(self.conn, now=self.now)
        self.assertEqual((unknown["read_state"], unknown["status"]), ("available", "unknown"))
        for key in (
            "experiment_started",
            "experiment_ref",
            "attempt_count",
            "complete_attempt_count",
            "useful_rate",
            "median_resume_seconds",
        ):
            self.assertIsNone(unknown[key])

        self.conn.execute(
            "UPDATE settings SET value='false' WHERE key='context_console_phase1_enabled'"
        )
        self.conn.commit()
        disabled = public_product_experiment_status(self.conn, now=self.now)
        self.assertEqual(disabled["read_state"], "disabled")
        for key in (
            "status",
            "experiment_started",
            "experiment_ref",
            "attempt_count",
            "complete_attempt_count",
            "useful_rate",
            "median_resume_seconds",
        ):
            self.assertIsNone(disabled[key])

        unavailable_conn = sqlite3.connect(":memory:")
        unavailable_conn.close()
        unavailable = public_product_experiment_status(unavailable_conn, now=self.now)
        self.assertEqual(unavailable["read_state"], "unavailable")
        for key in (
            "status",
            "experiment_started",
            "experiment_ref",
            "attempt_count",
            "complete_attempt_count",
            "useful_rate",
            "median_resume_seconds",
        ):
            self.assertIsNone(unavailable[key])

    def test_product_experiment_public_status_linearizes_before_feature_disable(self) -> None:
        reader = sqlite3.connect(
            self.db,
            timeout=10.0,
            check_same_thread=False,
        )
        reader.row_factory = sqlite3.Row
        reader.execute("PRAGMA busy_timeout = 10000")
        disabler = sqlite3.connect(
            self.db,
            timeout=10.0,
            check_same_thread=False,
        )
        disabler.row_factory = sqlite3.Row
        disabler.execute("PRAGMA busy_timeout = 10000")
        status_entered = threading.Event()
        release_status = threading.Event()
        disable_started = threading.Event()
        disable_finished = threading.Event()
        status_result: dict[str, Any] = {}
        disable_result: dict[str, Any] = {}
        errors: list[BaseException] = []
        original_status = context_console_module.product_experiment_status

        def delayed_status(*args, **kwargs):
            status_entered.set()
            if not release_status.wait(5):
                raise AssertionError("status release was not signaled")
            return original_status(*args, **kwargs)

        def read_worker() -> None:
            try:
                status_result.update(public_product_experiment_status(reader, now=self.now))
            except BaseException as exc:  # pragma: no cover - diagnostic only
                errors.append(exc)

        def disable_worker() -> None:
            disable_started.set()
            try:
                disable_result.update(configure_context_console(disabler, enabled=False))
            except BaseException as exc:  # pragma: no cover - diagnostic only
                errors.append(exc)
            finally:
                disable_finished.set()

        read_thread = threading.Thread(target=read_worker)
        disable_thread = threading.Thread(target=disable_worker)
        try:
            with mock.patch.object(
                context_console_module,
                "product_experiment_status",
                side_effect=delayed_status,
            ):
                read_thread.start()
                self.assertTrue(status_entered.wait(5))
                disable_thread.start()
                self.assertTrue(disable_started.wait(5))
                self.assertFalse(
                    disable_finished.wait(0.2),
                    "feature disable completed while the public status lease was active",
                )
                release_status.set()
                read_thread.join(10)
                disable_thread.join(10)
        finally:
            release_status.set()
            if read_thread.is_alive():
                read_thread.join(10)
            if disable_thread.is_alive():
                disable_thread.join(10)
            reader.close()
            disabler.close()

        self.assertFalse(read_thread.is_alive())
        self.assertFalse(disable_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(status_result["read_state"], "available")
        self.assertEqual(disable_result["status"], "disabled")
        after_disable = public_product_experiment_status(self.conn, now=self.now)
        self.assertEqual(after_disable["read_state"], "disabled")
        for key in (
            "status",
            "experiment_started",
            "experiment_ref",
            "attempt_count",
            "complete_attempt_count",
            "useful_rate",
            "median_resume_seconds",
        ):
            self.assertIsNone(after_disable[key])

    def test_product_experiment_public_status_refreshes_time_and_rechecks_gate(self) -> None:
        before_wait = datetime(2026, 7, 29, 15, 59, 59, tzinfo=UTC)
        after_wait = datetime(2026, 7, 29, 16, 0, 1, tzinfo=UTC)
        clock = {"now": before_wait}
        original_aware_utc = context_console_module._aware_utc

        class DelayedLease:
            def __enter__(inner_self) -> None:
                clock["now"] = after_wait

            def __exit__(inner_self, exc_type, exc, traceback) -> None:
                return None

        def fake_aware_utc(value):
            return clock["now"] if value is None else original_aware_utc(value)

        with mock.patch.object(
            context_console_module,
            "_context_authorization_lock",
            return_value=DelayedLease(),
        ), mock.patch.object(
            context_console_module,
            "_aware_utc",
            side_effect=fake_aware_utc,
        ):
            refreshed = public_product_experiment_status(self.conn)

        self.assertEqual(refreshed["read_state"], "available")
        self.assertEqual(refreshed["observed_at"], after_wait.isoformat(timespec="seconds"))

        original_status = context_console_module.product_experiment_status

        def disable_during_status(*args, **kwargs):
            result = original_status(*args, **kwargs)
            self.conn.execute(
                "UPDATE settings SET value='false' WHERE key=?",
                (context_console_module.CONTEXT_FEATURE_FLAG,),
            )
            self.conn.commit()
            return result

        with mock.patch.object(
            context_console_module,
            "product_experiment_status",
            side_effect=disable_during_status,
        ):
            disabled = public_product_experiment_status(self.conn, now=self.now)

        self.assertEqual(disabled["read_state"], "disabled")
        for key in (
            "status",
            "experiment_started",
            "experiment_ref",
            "attempt_count",
            "complete_attempt_count",
            "useful_rate",
            "median_resume_seconds",
        ):
            self.assertIsNone(disabled[key])

    def test_product_experiment_public_cli_has_no_experiment_ref_argv_surface(self) -> None:
        command = [
            sys.executable,
            "-m",
            "activity_mvp",
            "context-console",
            "experiment-status",
            "--db",
            str(self.db),
        ]
        completed = subprocess.run(
            command,
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["contract_version"], PRODUCT_EXPERIMENT_STATUS_VERSION)
        self.assertEqual(payload["read_state"], "available")

        missing = subprocess.run(
            [*command[:-1], str(self.root / "missing.sqlite")],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(missing.returncode, 0, missing.stderr)
        missing_payload = json.loads(missing.stdout)
        self.assertEqual(missing_payload["read_state"], "unavailable")
        for key in (
            "status",
            "experiment_started",
            "experiment_ref",
            "attempt_count",
            "complete_attempt_count",
            "useful_rate",
            "median_resume_seconds",
        ):
            self.assertIsNone(missing_payload[key])

        rejected = subprocess.run(
            [*command, "--experiment-ref", "experiment_e0006001"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("unrecognized arguments", rejected.stderr)

    def test_product_experiment_allows_only_one_running_window(self) -> None:
        first = start_product_experiment(
            self.conn,
            experiment_ref="experiment_e0002101",
            confirmation="START_LOCAL_PRODUCT_EXPERIMENT",
            now=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
        )
        self.assertTrue(first["experiment_started"])

        replay = start_product_experiment(
            self.conn,
            experiment_ref="experiment_e0002101",
            confirmation="START_LOCAL_PRODUCT_EXPERIMENT",
            now=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
        )
        competing = start_product_experiment(
            self.conn,
            experiment_ref="experiment_e0002102",
            confirmation="START_LOCAL_PRODUCT_EXPERIMENT",
            now=datetime(2026, 7, 1, 12, 0, tzinfo=UTC),
        )
        self.assertEqual(
            (replay["status"], replay["reason_code"]),
            ("conflict", "experiment_already_exists"),
        )
        self.assertEqual(
            (competing["status"], competing["reason_code"]),
            ("conflict", "experiment_already_running"),
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM context_product_experiments WHERE status='running'"
            ).fetchone()[0],
            1,
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO context_product_experiments("
                "experiment_ref,status,started_local_date,ended_local_date,minimum_days,maximum_days,"
                "timezone_name,created_at,updated_at) VALUES(?,'running',?,NULL,?,?,?,?,?)",
                (
                    "experiment_e0002103",
                    "2026-07-01",
                    14,
                    21,
                    "UTC",
                    self.now.isoformat(timespec="seconds"),
                    self.now.isoformat(timespec="seconds"),
                ),
            )
        self.conn.rollback()

    def test_product_experiment_schema_rejects_legacy_multiple_running_windows(self) -> None:
        self.conn.execute("DROP INDEX idx_context_single_running_experiment")
        for suffix in ("01", "02"):
            self.conn.execute(
                "INSERT INTO context_product_experiments("
                "experiment_ref,status,started_local_date,ended_local_date,minimum_days,maximum_days,"
                "timezone_name,created_at,updated_at) VALUES(?,'running',?,NULL,?,?,?,?,?)",
                (
                    f"experiment_e00022{suffix}",
                    "2026-07-01",
                    14,
                    21,
                    "UTC",
                    self.now.isoformat(timespec="seconds"),
                    self.now.isoformat(timespec="seconds"),
                ),
            )
        with self.assertRaisesRegex(
            sqlite3.DatabaseError,
            "multiple_running_product_experiments",
        ):
            context_console_module.init_context_console_schema(self.conn)
        self.conn.rollback()

    def test_product_feedback_running_experiment_requires_explicit_complete_binding(self) -> None:
        self._ingest_trusted_confirmation(
            idempotency_key="idem_e0003001",
            source_ref="source_e0003001",
        )
        handoff = build_local_handoff(
            self.conn,
            self._wire({"project_ref": self.project}),
            now=self.now,
        )
        self.assertEqual(handoff["status"], "available")
        started = start_product_experiment(
            self.conn,
            experiment_ref="experiment_e0003001",
            confirmation="START_LOCAL_PRODUCT_EXPERIMENT",
            now=self.now,
        )
        self.assertTrue(started["experiment_started"])
        base = {
            "contract_version": PRODUCT_FEEDBACK_VERSION,
            "source_kind": "local_handoff",
            "source_ref": handoff["handoff_ref"],
            "project_ref": self.project,
            "useful": True,
        }
        rejected_payloads = (
            {**base, "idempotency_key": "idem_e0003002"},
            {
                **base,
                "idempotency_key": "idem_e0003003",
                "experiment_ref": "experiment_e0003001",
            },
            {
                **base,
                "idempotency_key": "idem_e0003004",
                "resume_seconds": 12.0,
            },
            {
                **base,
                "idempotency_key": "idem_e0003005",
                "resume_seconds": 12.0,
                "experiment_ref": "experiment_e0003999",
            },
            {
                **base,
                "idempotency_key": "idem_e0003007",
                "resume_seconds": -0.1,
                "experiment_ref": "experiment_e0003001",
            },
            {
                **base,
                "idempotency_key": "idem_e0003008",
                "resume_seconds": float("inf"),
                "experiment_ref": "experiment_e0003001",
            },
        )
        for payload in rejected_payloads:
            result = record_product_feedback(self.conn, self._wire(payload), now=self.now)
            self.assertEqual(result["status"], "rejected")
            self.assertEqual(
                tuple(
                    self.conn.execute(
                        "SELECT "
                        "(SELECT COUNT(*) FROM context_product_feedback),"
                        "(SELECT COUNT(*) FROM context_product_experiment_attempts),"
                        "(SELECT COUNT(*) FROM context_product_wins)"
                    ).fetchone()
                ),
                (0, 0, 0),
            )

        recorded = record_product_feedback(
            self.conn,
            self._wire(
                {
                    **base,
                    "idempotency_key": "idem_e0003006",
                    "useful": False,
                    "resume_seconds": 12.0,
                    "experiment_ref": "experiment_e0003001",
                }
            ),
            now=self.now,
        )
        self.assertEqual(recorded["status"], "recorded")
        self.assertTrue(recorded["experiment_recorded"])
        self.assertFalse(recorded["win_recorded"])
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT useful,resume_seconds FROM context_product_experiment_attempts "
                    "WHERE experiment_ref='experiment_e0003001'"
                ).fetchone()
            ),
            (0, 12.0),
        )

    def test_product_feedback_rejects_malformed_running_experiment_window(self) -> None:
        self._ingest_trusted_confirmation(
            idempotency_key="idem_e0003101",
            source_ref="source_e0003101",
        )
        handoff = build_local_handoff(
            self.conn,
            self._wire({"project_ref": self.project}),
            now=self.now,
        )
        self._start_experiment(
            "experiment_e0003101",
            start_local_date="2026-07-16",
        )
        cases = (
            ("started_local_date", "not-a-date"),
            ("minimum_days", "not-an-int"),
            ("maximum_days", 99),
        )
        for index, (field, value) in enumerate(cases, start=1):
            with self.subTest(field=field):
                self.conn.execute(
                    f"UPDATE context_product_experiments SET {field}=? "
                    "WHERE experiment_ref='experiment_e0003101'",
                    (value,),
                )
                self.conn.commit()
                result = record_product_feedback(
                    self.conn,
                    self._wire(
                        {
                            "contract_version": PRODUCT_FEEDBACK_VERSION,
                            "idempotency_key": f"idem_e00031{index + 1:02d}",
                            "source_kind": "local_handoff",
                            "source_ref": handoff["handoff_ref"],
                            "project_ref": self.project,
                            "useful": True,
                            "resume_seconds": 12.0,
                            "experiment_ref": "experiment_e0003101",
                        }
                    ),
                    now=self.now,
                )
                self.assertEqual(result["status"], "rejected")
                self.assertNotIn("reason_code", result)
                self.assertEqual(
                    tuple(
                        self.conn.execute(
                            "SELECT "
                            "(SELECT COUNT(*) FROM context_product_feedback),"
                            "(SELECT COUNT(*) FROM context_product_experiment_attempts),"
                            "(SELECT COUNT(*) FROM context_product_wins)"
                        ).fetchone()
                    ),
                    (0, 0, 0),
                )
                self.conn.execute(
                    "UPDATE context_product_experiments "
                    "SET started_local_date='2026-07-16',minimum_days=14,maximum_days=21 "
                    "WHERE experiment_ref='experiment_e0003101'"
                )
                self.conn.commit()

    def test_product_feedback_refreshes_time_after_authorization_wait(self) -> None:
        """A source expiring during the authorization wait must fail closed."""

        captured_at = datetime(2026, 7, 29, 1, 0, 0, tzinfo=UTC)
        expired_at = captured_at + timedelta(seconds=1)
        after_wait = captured_at + timedelta(seconds=2)
        self.now = captured_at
        self._ingest_trusted_confirmation(
            idempotency_key="idem_e0003201",
            source_ref="source_e0003201",
        )
        expiry = expired_at.isoformat(timespec="microseconds")
        for table in (
            "context_verified",
            "context_observations",
            "context_candidates",
            "context_evidence",
        ):
            self.conn.execute(f"UPDATE {table} SET expires_at=?", (expiry,))
        self.conn.commit()
        handoff = build_local_handoff(
            self.conn,
            self._wire({"project_ref": self.project}),
            now=captured_at,
        )
        self.assertEqual(handoff["status"], "available")
        started = start_product_experiment(
            self.conn,
            experiment_ref="experiment_e0003201",
            confirmation="START_LOCAL_PRODUCT_EXPERIMENT",
            now=captured_at,
        )
        self.assertTrue(started["experiment_started"])

        clock = {"now": captured_at}
        original_aware_utc = context_console_module._aware_utc

        class AuthorizationWait:
            def __enter__(self) -> None:
                clock["now"] = after_wait

            def __exit__(self, exc_type, exc, traceback) -> None:
                return None

        def fake_aware_utc(value):
            return clock["now"] if value is None else original_aware_utc(value)

        payload = {
            "contract_version": PRODUCT_FEEDBACK_VERSION,
            "idempotency_key": "idem_e0003202",
            "source_kind": "local_handoff",
            "source_ref": handoff["handoff_ref"],
            "project_ref": self.project,
            "useful": True,
            "resume_seconds": 1.0,
            "experiment_ref": "experiment_e0003201",
        }
        with mock.patch.object(
            context_console_module,
            "_context_authorization_lock",
            return_value=AuthorizationWait(),
        ), mock.patch.object(
            context_console_module,
            "_aware_utc",
            side_effect=fake_aware_utc,
        ):
            result = record_product_feedback(self.conn, self._wire(payload))

        self.assertEqual(result["status"], "rejected")
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT "
                    "(SELECT COUNT(*) FROM context_product_feedback),"
                    "(SELECT COUNT(*) FROM context_product_experiment_attempts),"
                    "(SELECT COUNT(*) FROM context_product_wins)"
                ).fetchone()
            ),
            (0, 0, 0),
        )

    def test_context_snapshot_refreshes_default_day_after_midnight_authorization_wait(self) -> None:
        before_midnight = datetime(2026, 7, 29, 15, 59, 59, tzinfo=UTC)
        after_midnight = datetime(2026, 7, 29, 16, 0, 1, tzinfo=UTC)
        configured = configure_context_local_timezone(
            self.conn,
            timezone_name="Asia/Shanghai",
        )
        self.assertEqual(configured["status"], "configured")
        self._ingest_trusted_confirmation(
            idempotency_key="idem_e0003211",
            source_ref="source_e0003211",
            occurred_at=before_midnight,
            ingest_now=before_midnight,
        )
        clock = {"now": before_midnight}
        original_aware_utc = context_console_module._aware_utc

        class AuthorizationWait:
            def __enter__(inner_self) -> None:
                clock["now"] = after_midnight

            def __exit__(inner_self, exc_type, exc, traceback) -> None:
                return None

        def fake_aware_utc(value):
            return clock["now"] if value is None else original_aware_utc(value)

        with mock.patch.object(
            context_console_module,
            "_context_authorization_lock",
            return_value=AuthorizationWait(),
        ), mock.patch.object(
            context_console_module,
            "_aware_utc",
            side_effect=fake_aware_utc,
        ):
            snapshot = context_snapshot(self.conn, self._wire({}))

        self.assertEqual(snapshot["status"], "available")
        self.assertEqual(snapshot["observed_at"], after_midnight.isoformat(timespec="seconds"))
        self.assertEqual(snapshot["counts"]["verified_contexts"], 0)
        self.assertEqual(snapshot["projects"], [])
        self.assertEqual(
            snapshot["context_bundle_v1"]["filters"]["start_at"],
            "2026-07-29T16:00:00+00:00",
        )
        self.assertEqual(
            snapshot["context_bundle_v1"]["filters"]["end_at"],
            "2026-07-30T16:00:00+00:00",
        )

    def test_context_flag_disable_is_serialized_and_queued_reads_fail_closed(self) -> None:
        self._ingest_trusted_confirmation(
            idempotency_key="idem_e0003212",
            source_ref="source_e0003212",
        )
        snapshot_result: dict[str, Any] = {}
        handoff_result: dict[str, Any] = {}
        errors: list[BaseException] = []
        started = [threading.Event(), threading.Event()]
        finished = [threading.Event(), threading.Event()]

        def connection() -> sqlite3.Connection:
            opened = sqlite3.connect(self.db, timeout=10.0, check_same_thread=False)
            opened.row_factory = sqlite3.Row
            opened.execute("PRAGMA busy_timeout = 10000")
            opened.execute("PRAGMA foreign_keys = ON")
            return opened

        snapshot_connection = connection()
        handoff_connection = connection()

        def snapshot_worker() -> None:
            started[0].set()
            try:
                snapshot_result.update(
                    context_snapshot(snapshot_connection, self._wire({}), now=self.now)
                )
            except BaseException as exc:  # pragma: no cover - diagnostic only
                errors.append(exc)
            finally:
                snapshot_connection.close()
                finished[0].set()

        def handoff_worker() -> None:
            started[1].set()
            try:
                handoff_result.update(
                    build_local_handoff(
                        handoff_connection,
                        self._wire({"project_ref": self.project}),
                        now=self.now,
                    )
                )
            except BaseException as exc:  # pragma: no cover - diagnostic only
                errors.append(exc)
            finally:
                handoff_connection.close()
                finished[1].set()

        threads = [
            threading.Thread(target=snapshot_worker),
            threading.Thread(target=handoff_worker),
        ]
        with context_console_module._context_authorization_lock(
            self.conn,
            exclusive=True,
        ):
            for thread in threads:
                thread.start()
            self.assertTrue(all(event.wait(5) for event in started))
            self.assertFalse(any(event.wait(0.1) for event in finished))
            self.conn.execute(
                "UPDATE settings SET value='false' WHERE key=?",
                (context_console_module.CONTEXT_FEATURE_FLAG,),
            )
            self.conn.commit()

        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(snapshot_result["status"], "disabled")
        self.assertFalse(snapshot_result["actions_enabled"])
        self.assertIsNone(snapshot_result["context_bundle_v1"])
        self.assertIsNone(snapshot_result["projects"])
        self.assertEqual(handoff_result["status"], "disabled")
        self.assertIsNone(handoff_result["handoff_ref"])
        self.assertIsNone(handoff_result["verified_contexts"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM context_bundle_manifests").fetchone()[0],
            0,
        )

    def test_configure_context_disable_waits_for_active_shared_lease(self) -> None:
        worker_connection = sqlite3.connect(
            self.db,
            timeout=10.0,
            check_same_thread=False,
        )
        worker_connection.row_factory = sqlite3.Row
        worker_connection.execute("PRAGMA busy_timeout = 10000")
        update_reached = threading.Event()
        result: dict[str, Any] = {}
        errors: list[BaseException] = []

        class SignalingConnection:
            def execute(inner_self, sql, *args, **kwargs):
                if "INSERT INTO SETTINGS" in str(sql).upper():
                    update_reached.set()
                return worker_connection.execute(sql, *args, **kwargs)

            def __getattr__(inner_self, name):
                return getattr(worker_connection, name)

        def worker() -> None:
            try:
                result.update(
                    configure_context_console(SignalingConnection(), enabled=False)
                )
            except BaseException as exc:  # pragma: no cover - diagnostic only
                errors.append(exc)
            finally:
                worker_connection.close()

        thread = threading.Thread(target=worker)
        with context_console_module._context_authorization_lock(
            self.conn,
            exclusive=False,
        ):
            thread.start()
            self.assertFalse(
                update_reached.wait(0.2),
                "disable reached its database write before the active shared lease ended",
            )
        self.assertTrue(update_reached.wait(5))
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result["status"], "disabled")
        self.assertFalse(context_console_enabled(self.conn))

    def test_feature_config_and_experiment_start_roll_back_process_control_exceptions(self) -> None:
        class InterruptAfterSQL:
            def __init__(inner_self, wrapped, needle: str, failure: BaseException) -> None:
                inner_self.wrapped = wrapped
                inner_self.needle = needle.upper()
                inner_self.failure = failure
                inner_self.fired = False

            def execute(inner_self, sql, *args, **kwargs):
                result = inner_self.wrapped.execute(sql, *args, **kwargs)
                if not inner_self.fired and inner_self.needle in str(sql).upper():
                    inner_self.fired = True
                    raise inner_self.failure
                return result

            def __getattr__(inner_self, name):
                return getattr(inner_self.wrapped, name)

        configure_failure = KeyboardInterrupt("interrupt_after_feature_update")
        with self.assertRaises(KeyboardInterrupt) as raised_configure:
            configure_context_console(
                InterruptAfterSQL(
                    self.conn,
                    "INSERT INTO SETTINGS",
                    configure_failure,
                ),
                enabled=False,
            )
        self.assertIs(raised_configure.exception, configure_failure)
        self.assertEqual(raised_configure.exception.args, configure_failure.args)
        self.assertFalse(self.conn.in_transaction)
        self.assertTrue(context_console_enabled(self.conn))
        self.conn.execute(
            "INSERT INTO daemon_state(key,value,updated_at) VALUES(?,?,?)",
            ("post_feature_interrupt", "committed", self.now.isoformat(timespec="seconds")),
        )
        self.conn.commit()
        self.assertTrue(context_console_enabled(self.conn))

        start_failure = SystemExit(73)
        with self.assertRaises(SystemExit) as raised_start:
            start_product_experiment(
                InterruptAfterSQL(
                    self.conn,
                    "INSERT INTO CONTEXT_PRODUCT_EXPERIMENTS",
                    start_failure,
                ),
                experiment_ref="experiment_e0006101",
                confirmation="START_LOCAL_PRODUCT_EXPERIMENT",
                now=self.now,
            )
        self.assertIs(raised_start.exception, start_failure)
        self.assertEqual(raised_start.exception.args, start_failure.args)
        self.assertFalse(self.conn.in_transaction)
        self.assertIsNone(
            self.conn.execute(
                "SELECT experiment_ref FROM context_product_experiments "
                "WHERE experiment_ref='experiment_e0006101'"
            ).fetchone()
        )
        self.conn.execute(
            "INSERT INTO daemon_state(key,value,updated_at) VALUES(?,?,?)",
            ("post_start_interrupt", "committed", self.now.isoformat(timespec="seconds")),
        )
        self.conn.commit()
        self.assertIsNone(
            self.conn.execute(
                "SELECT experiment_ref FROM context_product_experiments "
                "WHERE experiment_ref='experiment_e0006101'"
            ).fetchone()
        )

    def test_owner_context_action_rolls_back_process_control_exceptions(self) -> None:
        original_manual_add = activity_confirmations_module.manual_add_confirmed_activity
        failures: tuple[BaseException, ...] = (
            KeyboardInterrupt("owner_action_interrupt"),
            SystemExit(75),
        )
        for index, failure in enumerate(failures, start=1):
            with self.subTest(failure=type(failure).__name__):
                safe_label = f"本人操作异常回滚探针 {index}"
                idempotency_key = f"idem_e00061{index + 1:02x}"
                payload = self._phase2_manual_action_payload(
                    idempotency_key=idempotency_key,
                    safe_label=safe_label,
                )

                def interrupt_after_partial_write(
                    *args,
                    _failure=failure,
                    **kwargs,
                ):
                    original_manual_add(*args, **kwargs)
                    raise _failure

                with mock.patch.object(
                    activity_confirmations_module,
                    "manual_add_confirmed_activity",
                    side_effect=interrupt_after_partial_write,
                ), self.assertRaises(type(failure)) as raised:
                    owner_context_action(
                        self.conn,
                        self._wire(payload),
                        now=self.now,
                    )

                self.assertIs(raised.exception, failure)
                self.assertEqual(raised.exception.args, failure.args)
                self.assertFalse(self.conn.in_transaction)
                self.assertEqual(
                    self.conn.execute(
                        "SELECT COUNT(*) FROM activity_confirmation_items "
                        "WHERE cleaned_content=?",
                        (safe_label,),
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    self.conn.execute(
                        "SELECT COUNT(*) FROM context_activity_confirmation_links "
                        "WHERE idempotency_key=?",
                        (idempotency_key,),
                    ).fetchone()[0],
                    0,
                )

                probe = sqlite3.connect(self.db)
                try:
                    self.assertEqual(
                        probe.execute(
                            "SELECT COUNT(*) FROM activity_confirmation_items "
                            "WHERE cleaned_content=?",
                            (safe_label,),
                        ).fetchone()[0],
                        0,
                    )
                finally:
                    probe.close()

                self.conn.execute(
                    "INSERT INTO daemon_state(key,value,updated_at) VALUES(?,?,?)",
                    (
                        f"post_owner_action_interrupt_{index}",
                        "committed",
                        self.now.isoformat(timespec="seconds"),
                    ),
                )
                self.conn.commit()
                probe = sqlite3.connect(self.db)
                try:
                    self.assertEqual(
                        probe.execute(
                            "SELECT COUNT(*) FROM activity_confirmation_items "
                            "WHERE cleaned_content=?",
                            (safe_label,),
                        ).fetchone()[0],
                        0,
                    )
                finally:
                    probe.close()

    def test_owner_context_action_discards_connection_when_rollback_is_denied(self) -> None:
        original_manual_add = activity_confirmations_module.manual_add_confirmed_activity
        failures: tuple[BaseException, ...] = (
            KeyboardInterrupt("owner_action_denied_rollback_interrupt"),
            SystemExit(76),
        )
        for index, failure in enumerate(failures, start=1):
            with self.subTest(failure=type(failure).__name__):
                safe_label = f"本人回滚拒绝关闭连接探针 {index}"
                idempotency_key = f"idem_e00062{index + 1:02x}"
                payload = self._phase2_manual_action_payload(
                    idempotency_key=idempotency_key,
                    safe_label=safe_label,
                )
                transaction_actions: list[str] = []
                denied_actions: list[str] = []
                fault_conn = ensure_ready(self.db)

                def deny_rollback_only(
                    action_code: int,
                    parameter_one: str | None,
                    _parameter_two: str | None,
                    _database_name: str | None,
                    _trigger_name: str | None,
                ) -> int:
                    if action_code == sqlite3.SQLITE_TRANSACTION:
                        transaction_action = str(parameter_one or "").upper()
                        transaction_actions.append(transaction_action)
                        if transaction_action == "ROLLBACK":
                            denied_actions.append(transaction_action)
                            return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK

                def interrupt_after_partial_write(
                    *args,
                    _failure=failure,
                    **kwargs,
                ):
                    original_manual_add(*args, **kwargs)
                    raise _failure

                fault_conn.set_authorizer(deny_rollback_only)
                try:
                    with mock.patch.object(
                        activity_confirmations_module,
                        "manual_add_confirmed_activity",
                        side_effect=interrupt_after_partial_write,
                    ), self.assertRaises(type(failure)) as raised:
                        owner_context_action(
                            fault_conn,
                            self._wire(payload),
                            now=self.now,
                        )

                    self.assertIs(raised.exception, failure)
                    self.assertEqual(raised.exception.args, failure.args)
                    self.assertEqual(transaction_actions.count("ROLLBACK"), 1)
                    self.assertEqual(denied_actions, ["ROLLBACK"])
                    with self.assertRaises(sqlite3.ProgrammingError):
                        fault_conn.execute("SELECT 1")
                    with self.assertRaises(sqlite3.ProgrammingError):
                        fault_conn.commit()
                finally:
                    fault_conn.close()

                def assert_no_partial_write(probe: sqlite3.Connection) -> None:
                    self.assertEqual(
                        probe.execute(
                            "SELECT COUNT(*) FROM activity_confirmation_items "
                            "WHERE cleaned_content=?",
                            (safe_label,),
                        ).fetchone()[0],
                        0,
                    )
                    self.assertEqual(
                        probe.execute(
                            "SELECT COUNT(*) FROM context_activity_confirmation_links "
                            "WHERE idempotency_key=?",
                            (idempotency_key,),
                        ).fetchone()[0],
                        0,
                    )
                    self.assertEqual(
                        probe.execute(
                            "SELECT COUNT(*) FROM context_label_registry WHERE label=?",
                            (safe_label,),
                        ).fetchone()[0],
                        0,
                    )

                probe = sqlite3.connect(self.db)
                try:
                    assert_no_partial_write(probe)
                finally:
                    probe.close()

                self.conn.execute(
                    "INSERT INTO daemon_state(key,value,updated_at) VALUES(?,?,?)",
                    (
                        f"post_owner_action_denied_rollback_{index}",
                        "committed",
                        self.now.isoformat(timespec="seconds"),
                    ),
                )
                self.conn.commit()
                probe = sqlite3.connect(self.db)
                try:
                    assert_no_partial_write(probe)
                finally:
                    probe.close()

    def test_experiment_finalize_rolls_back_process_control_exception(self) -> None:
        self._start_experiment("experiment_e0006201")
        self._record_experiment_attempts(
            "experiment_e0006201",
            [(True, 10.0)] * 10,
            event_prefix="e620",
        )
        before = tuple(
            self.conn.execute(
                "SELECT status,ended_local_date,updated_at FROM context_product_experiments "
                "WHERE experiment_ref='experiment_e0006201'"
            ).fetchone()
        )
        failure = KeyboardInterrupt("interrupt_after_experiment_finalize")

        class InterruptAfterFinalize:
            def execute(inner_self, sql, *args, **kwargs):
                result = self.conn.execute(sql, *args, **kwargs)
                if "UPDATE CONTEXT_PRODUCT_EXPERIMENTS SET STATUS=" in str(sql).upper():
                    raise failure
                return result

            def __getattr__(inner_self, name):
                return getattr(self.conn, name)

        with self.assertRaises(KeyboardInterrupt) as raised:
            finalize_product_experiment(
                InterruptAfterFinalize(),
                experiment_ref="experiment_e0006201",
                confirmation=PRODUCT_EXPERIMENT_FINALIZE_CONFIRMATION,
                now=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
            )
        self.assertIs(raised.exception, failure)
        self.assertEqual(raised.exception.args, failure.args)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT status,ended_local_date,updated_at FROM context_product_experiments "
                    "WHERE experiment_ref='experiment_e0006201'"
                ).fetchone()
            ),
            before,
        )
        self.conn.execute(
            "INSERT INTO daemon_state(key,value,updated_at) VALUES(?,?,?)",
            ("post_finalize_interrupt", "committed", self.now.isoformat(timespec="seconds")),
        )
        self.conn.commit()
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT status,ended_local_date,updated_at FROM context_product_experiments "
                    "WHERE experiment_ref='experiment_e0006201'"
                ).fetchone()
            ),
            before,
        )

    def test_handoff_and_feedback_roll_back_process_control_exceptions(self) -> None:
        self._ingest_trusted_confirmation(
            idempotency_key="idem_e0006301",
            source_ref="source_e0006301",
        )
        handoff_failure = KeyboardInterrupt("interrupt_after_handoff_manifest")

        class InterruptAfterManifest:
            def execute(inner_self, sql, *args, **kwargs):
                result = self.conn.execute(sql, *args, **kwargs)
                if "INSERT INTO CONTEXT_BUNDLE_MANIFESTS" in str(sql).upper():
                    raise handoff_failure
                return result

            def __getattr__(inner_self, name):
                return getattr(self.conn, name)

        with self.assertRaises(KeyboardInterrupt) as raised_handoff:
            build_local_handoff(
                InterruptAfterManifest(),
                self._wire({"project_ref": self.project}),
                now=self.now,
            )
        self.assertIs(raised_handoff.exception, handoff_failure)
        self.assertEqual(raised_handoff.exception.args, handoff_failure.args)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT "
                    "(SELECT COUNT(*) FROM context_bundle_manifests),"
                    "(SELECT COUNT(*) FROM context_bundle_members)"
                ).fetchone()
            ),
            (0, 0),
        )
        self.conn.execute(
            "INSERT INTO daemon_state(key,value,updated_at) VALUES(?,?,?)",
            ("post_handoff_interrupt", "committed", self.now.isoformat(timespec="seconds")),
        )
        self.conn.commit()
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT "
                    "(SELECT COUNT(*) FROM context_bundle_manifests),"
                    "(SELECT COUNT(*) FROM context_bundle_members)"
                ).fetchone()
            ),
            (0, 0),
        )

        handoff = build_local_handoff(
            self.conn,
            self._wire({"project_ref": self.project}),
            now=self.now,
        )
        self.assertEqual(handoff["status"], "available")
        feedback_failure = SystemExit(74)

        class InterruptAfterFeedback:
            def execute(inner_self, sql, *args, **kwargs):
                result = self.conn.execute(sql, *args, **kwargs)
                if "INSERT INTO CONTEXT_PRODUCT_FEEDBACK" in str(sql).upper():
                    raise feedback_failure
                return result

            def __getattr__(inner_self, name):
                return getattr(self.conn, name)

        payload = {
            "contract_version": PRODUCT_FEEDBACK_VERSION,
            "idempotency_key": "idem_e0006302",
            "source_kind": "local_handoff",
            "source_ref": handoff["handoff_ref"],
            "project_ref": self.project,
            "useful": True,
        }
        with self.assertRaises(SystemExit) as raised_feedback:
            record_product_feedback(
                InterruptAfterFeedback(),
                self._wire(payload),
                now=self.now,
            )
        self.assertIs(raised_feedback.exception, feedback_failure)
        self.assertEqual(raised_feedback.exception.args, feedback_failure.args)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT "
                    "(SELECT COUNT(*) FROM context_product_feedback),"
                    "(SELECT COUNT(*) FROM context_product_wins)"
                ).fetchone()
            ),
            (0, 0),
        )
        self.conn.execute(
            "INSERT INTO daemon_state(key,value,updated_at) VALUES(?,?,?)",
            ("post_feedback_interrupt", "committed", self.now.isoformat(timespec="seconds")),
        )
        self.conn.commit()
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT "
                    "(SELECT COUNT(*) FROM context_product_feedback),"
                    "(SELECT COUNT(*) FROM context_product_wins)"
                ).fetchone()
            ),
            (0, 0),
        )

    def test_local_handoff_revalidates_expiry_after_second_authorization_wait(self) -> None:
        before_midnight = datetime(2026, 7, 29, 15, 59, 59, tzinfo=UTC)
        midnight = datetime(2026, 7, 29, 16, 0, 0, tzinfo=UTC)
        after_midnight = datetime(2026, 7, 29, 16, 0, 1, tzinfo=UTC)
        configured = configure_context_local_timezone(
            self.conn,
            timezone_name="Asia/Shanghai",
        )
        self.assertEqual(configured["status"], "configured")
        self._ingest_trusted_confirmation(
            idempotency_key="idem_e0003221",
            source_ref="source_e0003221",
            occurred_at=before_midnight,
            ingest_now=before_midnight,
        )
        expiry = midnight.isoformat(timespec="microseconds")
        for table in (
            "context_verified",
            "context_observations",
            "context_candidates",
            "context_evidence",
        ):
            self.conn.execute(f"UPDATE {table} SET expires_at=?", (expiry,))
        self.conn.commit()
        clock = {"now": before_midnight}
        original_aware_utc = context_console_module._aware_utc

        class FirstLease:
            def __enter__(inner_self) -> None:
                return None

            def __exit__(inner_self, exc_type, exc, traceback) -> None:
                return None

        class SecondLease:
            def __enter__(inner_self) -> None:
                clock["now"] = after_midnight

            def __exit__(inner_self, exc_type, exc, traceback) -> None:
                return None

        def fake_aware_utc(value):
            return clock["now"] if value is None else original_aware_utc(value)

        with mock.patch.object(
            context_console_module,
            "_context_authorization_lock",
            side_effect=[FirstLease(), SecondLease()],
        ), mock.patch.object(
            context_console_module,
            "_aware_utc",
            side_effect=fake_aware_utc,
        ):
            handoff = build_local_handoff(
                self.conn,
                self._wire({"project_ref": self.project}),
            )

        self.assertEqual(handoff["status"], "rejected")
        self.assertEqual(handoff["observed_at"], after_midnight.isoformat(timespec="seconds"))
        self.assertIsNone(handoff["handoff_ref"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM context_bundle_manifests").fetchone()[0],
            0,
        )

    def test_local_handoff_refreshes_time_after_real_write_lock_crosses_expiry(self) -> None:
        before_expiry = datetime(2026, 7, 29, 1, 0, 0, tzinfo=UTC)
        expiry = before_expiry + timedelta(seconds=1)
        after_expiry = before_expiry + timedelta(seconds=2)
        self._ingest_trusted_confirmation(
            idempotency_key="idem_e0003222",
            source_ref="source_e0003222",
            occurred_at=before_expiry,
            ingest_now=before_expiry,
        )
        for table in (
            "context_verified",
            "context_observations",
            "context_candidates",
            "context_evidence",
        ):
            self.conn.execute(
                f"UPDATE {table} SET expires_at=?",
                (expiry.isoformat(timespec="microseconds"),),
            )
        self.conn.commit()

        clock = {"now": before_expiry}
        reached_begin = threading.Event()
        result: dict[str, Any] = {}
        errors: list[BaseException] = []
        worker_connection = sqlite3.connect(
            self.db,
            timeout=10.0,
            check_same_thread=False,
        )
        worker_connection.row_factory = sqlite3.Row
        worker_connection.execute("PRAGMA busy_timeout = 10000")
        worker_connection.execute("PRAGMA foreign_keys = ON")
        blocker = ensure_ready(self.db)
        blocker.execute("BEGIN IMMEDIATE")
        original_aware_utc = context_console_module._aware_utc

        class SignalingConnection:
            def execute(inner_self, sql, *args, **kwargs):
                if str(sql).strip().upper() == "BEGIN IMMEDIATE":
                    reached_begin.set()
                return worker_connection.execute(sql, *args, **kwargs)

            def __getattr__(inner_self, name):
                return getattr(worker_connection, name)

        def fake_aware_utc(value):
            return clock["now"] if value is None else original_aware_utc(value)

        def worker() -> None:
            try:
                result.update(
                    build_local_handoff(
                        SignalingConnection(),
                        self._wire({"project_ref": self.project}),
                    )
                )
            except BaseException as exc:  # pragma: no cover - diagnostic only
                errors.append(exc)
            finally:
                worker_connection.close()

        thread = threading.Thread(target=worker)
        reached = False
        try:
            with mock.patch.object(
                context_console_module,
                "_aware_utc",
                side_effect=fake_aware_utc,
            ):
                thread.start()
                reached = reached_begin.wait(5)
                if reached:
                    clock["now"] = after_expiry
                blocker.commit()
                thread.join(10)
        finally:
            if blocker.in_transaction:
                blocker.rollback()
            blocker.close()
            if thread.is_alive():
                thread.join(10)

        self.assertTrue(reached, "handoff did not wait for its real BEGIN IMMEDIATE")
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["observed_at"], after_expiry.isoformat(timespec="seconds"))
        self.assertIsNone(result["handoff_ref"])
        self.assertIsNone(result["unresolved_questions"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM context_bundle_manifests").fetchone()[0],
            0,
        )

    def test_local_handoff_reloads_pending_questions_after_real_write_lock(self) -> None:
        self._ingest_trusted_confirmation(
            idempotency_key="idem_e0003223",
            source_ref="source_e0003223",
        )
        self._insert_phase2_confirmation_card(
            "phase2_handoff_wait_question",
            object_label="Writer wait question",
            at=self.now.astimezone(),
        )
        before = context_snapshot(self.conn, self._wire({}), now=self.now)
        self.assertTrue(
            any(
                "Writer wait question" in str(question.get("title") or "")
                for question in before["confirmation_questions"]
            )
        )

        reached_begin = threading.Event()
        result: dict[str, Any] = {}
        errors: list[BaseException] = []
        worker_connection = sqlite3.connect(
            self.db,
            timeout=10.0,
            check_same_thread=False,
        )
        worker_connection.row_factory = sqlite3.Row
        worker_connection.execute("PRAGMA busy_timeout = 10000")
        worker_connection.execute("PRAGMA foreign_keys = ON")
        blocker = ensure_ready(self.db)
        blocker.execute("BEGIN IMMEDIATE")

        class SignalingConnection:
            def execute(inner_self, sql, *args, **kwargs):
                if str(sql).strip().upper() == "BEGIN IMMEDIATE":
                    reached_begin.set()
                return worker_connection.execute(sql, *args, **kwargs)

            def __getattr__(inner_self, name):
                return getattr(worker_connection, name)

        def worker() -> None:
            try:
                result.update(
                    build_local_handoff(
                        SignalingConnection(),
                        self._wire({"project_ref": self.project}),
                        now=self.now,
                    )
                )
            except BaseException as exc:  # pragma: no cover - diagnostic only
                errors.append(exc)
            finally:
                worker_connection.close()

        thread = threading.Thread(target=worker)
        reached = False
        try:
            thread.start()
            reached = reached_begin.wait(5)
            if reached:
                blocker.execute(
                    "UPDATE activity_confirmation_items SET state='ignored',answer='ignored' "
                    "WHERE item_key='phase2_handoff_wait_question'"
                )
            blocker.commit()
            thread.join(10)
        finally:
            if blocker.in_transaction:
                blocker.rollback()
            blocker.close()
            if thread.is_alive():
                thread.join(10)

        self.assertTrue(reached, "handoff did not wait for its real BEGIN IMMEDIATE")
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result["status"], "available")
        self.assertFalse(
            any("Writer wait question" in question for question in result["unresolved_questions"])
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM context_bundle_manifests").fetchone()[0],
            1,
        )

    def test_local_handoff_injected_time_stays_deterministic_across_authorization_waits(self) -> None:
        captured_at = datetime(2026, 7, 29, 1, 0, 0, tzinfo=UTC)
        expired_at = captured_at + timedelta(seconds=1)
        after_wait = captured_at + timedelta(seconds=2)
        self._ingest_trusted_confirmation(
            idempotency_key="idem_e0003231",
            source_ref="source_e0003231",
            occurred_at=captured_at,
            ingest_now=captured_at,
        )
        expiry = expired_at.isoformat(timespec="microseconds")
        for table in (
            "context_verified",
            "context_observations",
            "context_candidates",
            "context_evidence",
        ):
            self.conn.execute(f"UPDATE {table} SET expires_at=?", (expiry,))
        self.conn.commit()

        class AuthorizationWait:
            def __enter__(inner_self) -> None:
                return None

            def __exit__(inner_self, exc_type, exc, traceback) -> None:
                return None

        original_aware_utc = context_console_module._aware_utc

        def fake_aware_utc(value):
            return after_wait if value is None else original_aware_utc(value)

        with mock.patch.object(
            context_console_module,
            "_context_authorization_lock",
            return_value=AuthorizationWait(),
        ), mock.patch.object(
            context_console_module,
            "_aware_utc",
            side_effect=fake_aware_utc,
        ):
            handoff = build_local_handoff(
                self.conn,
                self._wire({"project_ref": self.project}),
                now=captured_at,
            )

        self.assertEqual(handoff["status"], "available")
        self.assertEqual(handoff["observed_at"], captured_at.isoformat(timespec="seconds"))
        self.assertEqual(handoff["expires_at"], expiry)

    def test_product_feedback_exact_replay_survives_source_expiry(self) -> None:
        captured_at = datetime(2026, 7, 29, 1, 0, 0, tzinfo=UTC)
        self._ingest_trusted_confirmation(
            idempotency_key="idem_e0003251",
            source_ref="source_e0003251",
        )
        handoff = build_local_handoff(
            self.conn,
            self._wire({"project_ref": self.project}),
            now=captured_at,
        )
        payload = {
            "contract_version": PRODUCT_FEEDBACK_VERSION,
            "idempotency_key": "idem_e0003252",
            "source_kind": "local_handoff",
            "source_ref": handoff["handoff_ref"],
            "project_ref": self.project,
            "useful": True,
        }
        recorded = record_product_feedback(
            self.conn,
            self._wire(payload),
            now=captured_at,
        )
        self.assertEqual(recorded["status"], "recorded")

        expired_at = (captured_at + timedelta(seconds=1)).isoformat(
            timespec="microseconds"
        )
        for table in (
            "context_verified",
            "context_observations",
            "context_candidates",
            "context_evidence",
        ):
            self.conn.execute(f"UPDATE {table} SET expires_at=?", (expired_at,))
        self.conn.commit()
        replayed = record_product_feedback(
            self.conn,
            self._wire(payload),
            now=captured_at + timedelta(seconds=2),
        )

        self.assertEqual(replayed["status"], "replayed")
        self.assertEqual(replayed["feedback_ref"], recorded["feedback_ref"])
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT "
                    "(SELECT COUNT(*) FROM context_product_feedback),"
                    "(SELECT COUNT(*) FROM context_product_wins)"
                ).fetchone()
            ),
            (1, 1),
        )

    def test_product_feedback_rechecks_feature_flag_after_authorization_wait(self) -> None:
        """Disabling Context while a feedback writer waits prevents every write."""

        self._ingest_trusted_confirmation(
            idempotency_key="idem_e0003301",
            source_ref="source_e0003301",
        )
        handoff = build_local_handoff(
            self.conn,
            self._wire({"project_ref": self.project}),
            now=self.now,
        )
        self.assertEqual(handoff["status"], "available")
        started = start_product_experiment(
            self.conn,
            experiment_ref="experiment_e0003301",
            confirmation="START_LOCAL_PRODUCT_EXPERIMENT",
            now=self.now,
        )
        self.assertTrue(started["experiment_started"])

        class DisableDuringWait:
            def __enter__(inner_self) -> None:
                # This fixture replaces the authorization lock itself, so write
                # the flag directly to model a disable committed while the old
                # caller was waiting. configure_context_console's real lease
                # serialization is covered independently below.
                self.conn.execute(
                    "UPDATE settings SET value='false' WHERE key=?",
                    (context_console_module.CONTEXT_FEATURE_FLAG,),
                )
                self.conn.commit()

            def __exit__(inner_self, exc_type, exc, traceback) -> None:
                return None

        payload = {
            "contract_version": PRODUCT_FEEDBACK_VERSION,
            "idempotency_key": "idem_e0003302",
            "source_kind": "local_handoff",
            "source_ref": handoff["handoff_ref"],
            "project_ref": self.project,
            "useful": True,
            "resume_seconds": 1.0,
            "experiment_ref": "experiment_e0003301",
        }
        with mock.patch.object(
            context_console_module,
            "_context_authorization_lock",
            return_value=DisableDuringWait(),
        ):
            result = record_product_feedback(self.conn, self._wire(payload), now=self.now)

        self.assertEqual(result["status"], "disabled")
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT "
                    "(SELECT COUNT(*) FROM context_product_feedback),"
                    "(SELECT COUNT(*) FROM context_product_experiment_attempts),"
                    "(SELECT COUNT(*) FROM context_product_wins)"
                ).fetchone()
            ),
            (0, 0, 0),
        )

    def test_product_feedback_rejects_existing_logical_day14_terminal(self) -> None:
        self._ingest_trusted_confirmation(
            idempotency_key="idem_e0003501",
            source_ref="source_e0003501",
        )
        handoff = build_local_handoff(
            self.conn,
            self._wire({"project_ref": self.project}),
            now=self.now,
        )
        self.assertEqual(handoff["status"], "available")
        started = start_product_experiment(
            self.conn,
            experiment_ref="experiment_e0003501",
            confirmation="START_LOCAL_PRODUCT_EXPERIMENT",
            now=datetime(2026, 7, 16, 12, 0, tzinfo=UTC),
        )
        self.assertTrue(started["experiment_started"])
        for index in range(10):
            local_day = date(2026, 7, 16) + timedelta(days=index)
            useful = index < 8
            self.conn.execute(
                "INSERT INTO context_product_experiment_attempts("
                "attempt_ref,experiment_ref,event_id,local_date,useful,outcome,resume_seconds,"
                "source_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    f"attempt_e350{index:04x}",
                    "experiment_e0003501",
                    f"event_e350{index:04x}",
                    local_day.isoformat(),
                    int(useful),
                    "useful" if useful else "not_useful",
                    20.0,
                    f"source_e350{index:04x}",
                    self.now.isoformat(timespec="seconds"),
                ),
            )
        self.conn.commit()
        logical_terminal = product_experiment_status(
            self.conn,
            experiment_ref="experiment_e0003501",
            local_today=date(2026, 7, 29),
        )
        self.assertEqual(
            (
                logical_terminal["status"],
                logical_terminal["attempt_count"],
                logical_terminal["useful_rate"],
                logical_terminal["median_resume_seconds"],
            ),
            ("passed", 10, 0.8, 20.0),
        )
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT status,ended_local_date FROM context_product_experiments "
                    "WHERE experiment_ref='experiment_e0003501'"
                ).fetchone()
            ),
            ("running", None),
        )

        result = record_product_feedback(
            self.conn,
            self._wire(
                {
                    "contract_version": PRODUCT_FEEDBACK_VERSION,
                    "idempotency_key": "idem_e0003502",
                    "source_kind": "local_handoff",
                    "source_ref": handoff["handoff_ref"],
                    "project_ref": self.project,
                    "useful": True,
                    "resume_seconds": 1.0,
                    "experiment_ref": "experiment_e0003501",
                }
            ),
            now=self.now,
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT "
                    "(SELECT COUNT(*) FROM context_product_experiment_attempts "
                    " WHERE experiment_ref='experiment_e0003501'),"
                    "(SELECT COUNT(*) FROM context_product_feedback),"
                    "(SELECT COUNT(*) FROM context_product_wins)"
                ).fetchone()
            ),
            (10, 0, 0),
        )
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT status,ended_local_date FROM context_product_experiments "
                    "WHERE experiment_ref='experiment_e0003501'"
                ).fetchone()
            ),
            ("passed", "2026-07-29"),
        )

    def test_product_feedback_accepts_day21_tenth_attempt_and_freezes(self) -> None:
        self._ingest_trusted_confirmation(
            idempotency_key="idem_e0003601",
            source_ref="source_e0003601",
        )
        handoff = build_local_handoff(
            self.conn,
            self._wire({"project_ref": self.project}),
            now=self.now,
        )
        self.assertEqual(handoff["status"], "available")
        self._start_experiment(
            "experiment_e0003601",
            start_local_date="2026-07-09",
        )
        for index in range(9):
            local_day = date(2026, 7, 9) + timedelta(days=index)
            attempt = record_product_experiment_attempt(
                self.conn,
                experiment_ref="experiment_e0003601",
                local_date=local_day.isoformat(),
                event_id=f"event_e360{index:04x}",
                useful=True,
                resume_seconds=15.0,
            )
            self.assertEqual(attempt["status"], "recorded")

        in_progress = public_product_experiment_status(self.conn, now=self.now)
        self.assertEqual((in_progress["status"], in_progress["attempt_count"]), ("extended", 9))
        self.assertEqual(in_progress["experiment_ref"], "experiment_e0003601")

        result = record_product_feedback(
            self.conn,
            self._wire(
                {
                    "contract_version": PRODUCT_FEEDBACK_VERSION,
                    "idempotency_key": "idem_e0003602",
                    "source_kind": "local_handoff",
                    "source_ref": handoff["handoff_ref"],
                    "project_ref": self.project,
                    "useful": True,
                    "resume_seconds": 10.0,
                    "experiment_ref": "experiment_e0003601",
                }
            ),
            now=self.now,
        )

        self.assertEqual(result["status"], "recorded")
        self.assertTrue(result["experiment_recorded"])
        self.assertTrue(result["win_recorded"])
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT "
                    "(SELECT status FROM context_product_experiments "
                    " WHERE experiment_ref='experiment_e0003601'),"
                    "(SELECT COUNT(*) FROM context_product_experiment_attempts "
                    " WHERE experiment_ref='experiment_e0003601'),"
                    "(SELECT COUNT(*) FROM context_product_feedback),"
                    "(SELECT COUNT(*) FROM context_product_wins)"
                ).fetchone()
            ),
            ("passed", 10, 1, 1),
        )
        terminal = public_product_experiment_status(self.conn, now=self.now)
        self.assertEqual(
            (
                terminal["status"],
                terminal["attempt_count"],
                terminal["complete_attempt_count"],
                terminal["useful_rate"],
                terminal["median_resume_seconds"],
            ),
            ("passed", 10, 10, 1.0, 15.0),
        )

    def test_product_feedback_without_running_experiment_remains_outside_experiment(self) -> None:
        self._ingest_trusted_confirmation(
            idempotency_key="idem_e0004001",
            source_ref="source_e0004001",
        )
        handoff = build_local_handoff(
            self.conn,
            self._wire({"project_ref": self.project}),
            now=self.now,
        )
        result = record_product_feedback(
            self.conn,
            self._wire(
                {
                    "contract_version": PRODUCT_FEEDBACK_VERSION,
                    "idempotency_key": "idem_e0004002",
                    "source_kind": "local_handoff",
                    "source_ref": handoff["handoff_ref"],
                    "project_ref": self.project,
                    "useful": True,
                }
            ),
            now=self.now,
        )
        self.assertEqual(result["status"], "recorded")
        self.assertFalse(result["experiment_recorded"])
        self.assertTrue(result["win_recorded"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM context_product_experiment_attempts").fetchone()[0],
            0,
        )

    def test_finalize_experiment_materializes_day21_insufficient_but_rejects_missing_evidence(self) -> None:
        self._start_experiment("experiment_e0005001")
        self._record_experiment_attempts(
            "experiment_e0005001",
            [(True, 10.0)] * 9,
            event_prefix="e500",
        )
        insufficient = finalize_product_experiment(
            self.conn,
            experiment_ref="experiment_e0005001",
            confirmation=PRODUCT_EXPERIMENT_FINALIZE_CONFIRMATION,
            now=datetime(2026, 7, 21, 12, 0, tzinfo=UTC),
        )
        self.assertEqual(
            (insufficient["finalization_status"], insufficient["status"], insufficient["decision_status"]),
            ("finalized", "insufficient", "not_passed"),
        )
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT status,ended_local_date FROM context_product_experiments "
                    "WHERE experiment_ref='experiment_e0005001'"
                ).fetchone()
            ),
            ("not_passed", "2026-07-21"),
        )

        self._start_experiment("experiment_e0005002")
        self._record_experiment_attempts(
            "experiment_e0005002",
            [(True, 10.0)] * 9 + [(None, 10.0)],
            event_prefix="e501",
        )
        incomplete = finalize_product_experiment(
            self.conn,
            experiment_ref="experiment_e0005002",
            confirmation=PRODUCT_EXPERIMENT_FINALIZE_CONFIRMATION,
            now=datetime(2026, 7, 21, 12, 0, tzinfo=UTC),
        )
        self.assertEqual((incomplete["status"], incomplete["reason_code"]), ("rejected", "experiment_evidence_incomplete"))
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT status,ended_local_date FROM context_product_experiments "
                    "WHERE experiment_ref='experiment_e0005002'"
                ).fetchone()
            ),
            ("running", None),
        )

    def test_finalize_experiment_refreshes_time_after_write_lock_crosses_day14_midnight(
        self,
    ) -> None:
        configured = configure_context_local_timezone(
            self.conn,
            timezone_name="Asia/Shanghai",
        )
        self.assertEqual(configured["status"], "configured")
        started = start_product_experiment(
            self.conn,
            experiment_ref="experiment_e0005051",
            confirmation="START_LOCAL_PRODUCT_EXPERIMENT",
            now=datetime(2026, 7, 17, 4, 0, 0, tzinfo=UTC),
        )
        self.assertEqual(started["started_local_date"], "2026-07-17")
        for index in range(10):
            local_day = date(2026, 7, 17) + timedelta(days=index)
            attempt = record_product_experiment_attempt(
                self.conn,
                experiment_ref="experiment_e0005051",
                local_date=local_day.isoformat(),
                event_id=f"event_e00050{index:02d}",
                useful=True,
                resume_seconds=10.0,
            )
            self.assertEqual(attempt["status"], "recorded")

        before_midnight = datetime(2026, 7, 29, 15, 59, 59, tzinfo=UTC)
        after_midnight = datetime(2026, 7, 29, 16, 0, 1, tzinfo=UTC)
        clock = {"now": before_midnight}
        reached_begin = threading.Event()
        result: dict[str, Any] = {}
        errors: list[BaseException] = []
        worker_connection = sqlite3.connect(
            self.db,
            timeout=10.0,
            check_same_thread=False,
        )
        worker_connection.row_factory = sqlite3.Row
        worker_connection.execute("PRAGMA busy_timeout = 10000")
        worker_connection.execute("PRAGMA foreign_keys = ON")
        blocker = ensure_ready(self.db)
        blocker.execute("BEGIN IMMEDIATE")
        original_aware_utc = context_console_module._aware_utc

        class SignalingConnection:
            def __init__(inner_self, wrapped: sqlite3.Connection) -> None:
                inner_self.wrapped = wrapped

            def execute(inner_self, sql, *args, **kwargs):
                if str(sql).strip().upper() == "BEGIN IMMEDIATE":
                    reached_begin.set()
                return inner_self.wrapped.execute(sql, *args, **kwargs)

            def __getattr__(inner_self, name):
                return getattr(inner_self.wrapped, name)

        def fake_aware_utc(value):
            return clock["now"] if value is None else original_aware_utc(value)

        def worker() -> None:
            try:
                result.update(
                    finalize_product_experiment(
                        SignalingConnection(worker_connection),
                        experiment_ref="experiment_e0005051",
                        confirmation=PRODUCT_EXPERIMENT_FINALIZE_CONFIRMATION,
                    )
                )
            except BaseException as exc:  # pragma: no cover - diagnostic only
                errors.append(exc)
            finally:
                worker_connection.close()

        thread = threading.Thread(target=worker)
        reached = False
        try:
            with mock.patch.object(
                context_console_module,
                "_aware_utc",
                side_effect=fake_aware_utc,
            ):
                thread.start()
                reached = reached_begin.wait(5)
                if reached:
                    clock["now"] = after_midnight
                blocker.commit()
                thread.join(10)
        finally:
            if blocker.in_transaction:
                blocker.rollback()
            blocker.close()
            if thread.is_alive():
                thread.join(10)

        self.assertTrue(reached, "finalize did not wait for BEGIN IMMEDIATE")
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(
            (result["status"], result["finalization_status"], result["evaluated_local_date"]),
            ("passed", "finalized", "2026-07-30"),
        )
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT status,ended_local_date,updated_at FROM context_product_experiments "
                    "WHERE experiment_ref=?",
                    ("experiment_e0005051",),
                ).fetchone()
            ),
            (
                "passed",
                "2026-07-30",
                after_midnight.isoformat(timespec="seconds"),
            ),
        )

    def test_finalize_experiment_rechecks_current_timezone_before_any_write(self) -> None:
        self._start_experiment("experiment_e0005101")
        self._record_experiment_attempts(
            "experiment_e0005101",
            [(True, 10.0)] * 10,
            event_prefix="e510",
        )
        before = tuple(
            self.conn.execute(
                "SELECT status,ended_local_date,updated_at FROM context_product_experiments "
                "WHERE experiment_ref='experiment_e0005101'"
            ).fetchone()
        )

        self.conn.execute("DELETE FROM settings WHERE key='context_local_timezone'")
        self.conn.commit()
        missing = finalize_product_experiment(
            self.conn,
            experiment_ref="experiment_e0005101",
            confirmation=PRODUCT_EXPERIMENT_FINALIZE_CONFIRMATION,
            now=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
        )
        self.assertEqual(
            (missing["status"], missing["reason_code"]),
            ("rejected", "context_timezone_missing"),
        )
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT status,ended_local_date,updated_at "
                    "FROM context_product_experiments "
                    "WHERE experiment_ref='experiment_e0005101'"
                ).fetchone()
            ),
            before,
        )

        self.conn.execute(
            "INSERT INTO settings(key,value,updated_at) "
            "VALUES('context_local_timezone','Not/A_Zone',?)",
            (self.now.isoformat(timespec="seconds"),),
        )
        self.conn.commit()
        invalid = finalize_product_experiment(
            self.conn,
            experiment_ref="experiment_e0005101",
            confirmation=PRODUCT_EXPERIMENT_FINALIZE_CONFIRMATION,
            now=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
        )
        self.assertEqual(
            (invalid["status"], invalid["reason_code"]),
            ("rejected", "invalid_context_timezone"),
        )
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT status,ended_local_date,updated_at "
                    "FROM context_product_experiments "
                    "WHERE experiment_ref='experiment_e0005101'"
                ).fetchone()
            ),
            before,
        )

    def test_finalize_experiment_rechecks_core_gate_before_any_write(self) -> None:
        self._start_experiment("experiment_e0005121")
        self._record_experiment_attempts(
            "experiment_e0005121",
            [(True, 10.0)] * 10,
            event_prefix="e512",
        )
        before = tuple(
            self.conn.execute(
                "SELECT status,ended_local_date,updated_at FROM context_product_experiments "
                "WHERE experiment_ref='experiment_e0005121'"
            ).fetchone()
        )
        disabled = configure_context_console(self.conn, enabled=False)
        self.assertEqual(disabled["status"], "disabled")

        blocked = finalize_product_experiment(
            self.conn,
            experiment_ref="experiment_e0005121",
            confirmation=PRODUCT_EXPERIMENT_FINALIZE_CONFIRMATION,
            now=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
        )

        self.assertEqual(
            (blocked["status"], blocked["reason_code"]),
            ("disabled", "feature_flag_off"),
        )
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT status,ended_local_date,updated_at "
                    "FROM context_product_experiments "
                    "WHERE experiment_ref='experiment_e0005121'"
                ).fetchone()
            ),
            before,
        )

    def test_noncanonical_experiment_dates_fail_closed(self) -> None:
        self._start_experiment("experiment_e0005151")
        self._record_experiment_attempts(
            "experiment_e0005151",
            [(True, 10.0)] * 10,
            event_prefix="e515",
        )
        before = tuple(
            self.conn.execute(
                "SELECT status,started_local_date,ended_local_date,updated_at "
                "FROM context_product_experiments "
                "WHERE experiment_ref='experiment_e0005151'"
            ).fetchone()
        )
        self.conn.execute(
            "UPDATE context_product_experiments SET started_local_date='20260701' "
            "WHERE experiment_ref='experiment_e0005151'"
        )
        self.conn.commit()
        public = public_product_experiment_status(
            self.conn,
            now=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
        )
        self.assertEqual(
            (public["read_state"], public["status"]),
            ("available", "unknown"),
        )
        self.assertIsNone(public["experiment_ref"])
        rejected = finalize_product_experiment(
            self.conn,
            experiment_ref="experiment_e0005151",
            confirmation=PRODUCT_EXPERIMENT_FINALIZE_CONFIRMATION,
            now=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
        )
        self.assertEqual(
            (rejected["status"], rejected["reason_code"]),
            ("rejected", "invalid_experiment_date"),
        )
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT status,ended_local_date,updated_at "
                    "FROM context_product_experiments "
                    "WHERE experiment_ref='experiment_e0005151'"
                ).fetchone()
            ),
            (before[0], before[2], before[3]),
        )

        self.conn.execute(
            "UPDATE context_product_experiments "
            "SET started_local_date='2026-07-01',status='passed',ended_local_date='20260714' "
            "WHERE experiment_ref='experiment_e0005151'"
        )
        self.conn.commit()
        bad_terminal = public_product_experiment_status(
            self.conn,
            now=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
        )
        self.assertEqual(
            (bad_terminal["read_state"], bad_terminal["status"]),
            ("available", "unknown"),
        )
        self.assertIsNone(bad_terminal["experiment_ref"])

    def test_corrupt_experiment_attempt_evidence_never_persists_terminal(self) -> None:
        self._start_experiment("experiment_e0005201")
        self._record_experiment_attempts(
            "experiment_e0005201",
            [(True, 10.0)] * 10,
            event_prefix="e520",
        )
        baseline = tuple(
            self.conn.execute(
                "SELECT status,ended_local_date,updated_at FROM context_product_experiments "
                "WHERE experiment_ref='experiment_e0005201'"
            ).fetchone()
        )
        target_attempt = "attempt_" + context_console_module._derived_ref(
            "attempt",
            "experiment_e0005201|event_e5200001",
        ).removeprefix("attempt_")
        cases = (
            ("resume_seconds", -1.0),
            ("resume_seconds", "not-a-number"),
            ("resume_seconds", float("inf")),
            ("useful", 2),
            ("local_date", "2026-07-1"),
            ("local_date", "2026-06-30"),
            ("local_date", "2026-07-22"),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                self.conn.execute(
                    f"UPDATE context_product_experiment_attempts SET {field}=? "
                    "WHERE attempt_ref=?",
                    (value, target_attempt),
                )
                self.conn.commit()
                public = public_product_experiment_status(
                    self.conn,
                    now=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
                )
                self.assertEqual(
                    (public["read_state"], public["status"]),
                    ("available", "unknown"),
                )
                self.assertIsNone(public["experiment_ref"])
                finalized = finalize_product_experiment(
                    self.conn,
                    experiment_ref="experiment_e0005201",
                    confirmation=PRODUCT_EXPERIMENT_FINALIZE_CONFIRMATION,
                    now=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
                )
                self.assertEqual(
                    (finalized["status"], finalized["reason_code"]),
                    ("rejected", "invalid_experiment_attempt_evidence"),
                )
                self.assertEqual(
                    tuple(
                        self.conn.execute(
                            "SELECT status,ended_local_date,updated_at "
                            "FROM context_product_experiments "
                            "WHERE experiment_ref='experiment_e0005201'"
                        ).fetchone()
                    ),
                    baseline,
                )
                self.conn.execute(
                    "UPDATE context_product_experiment_attempts "
                    "SET useful=1,outcome='useful',resume_seconds=10.0,local_date='2026-07-01' "
                    "WHERE attempt_ref=?",
                    (target_attempt,),
                )
                self.conn.commit()

    def test_product_attempt_records_denominator_useful_flag_and_resume_seconds(self) -> None:
        self._start_experiment("experiment_e1000001")
        first = record_product_experiment_attempt(
            self.conn,
            experiment_ref="experiment_e1000001",
            local_date="2026-07-01",
            event_id="event_e1000001",
            source_ref="source_e1000001",
            useful=True,
            resume_seconds=12.5,
        )
        canonical_replay_without_legacy_alias = record_product_experiment_attempt(
            self.conn,
            experiment_ref="experiment_e1000001",
            local_date="2026-07-01",
            event_id="event_e1000001",
            useful=True,
            resume_seconds=12.5,
        )
        second = record_product_experiment_attempt(
            self.conn,
            experiment_ref="experiment_e1000001",
            local_date="2026-07-02",
            event_id="event_e1000002",
            useful=False,
            resume_seconds=42.0,
        )
        replay = record_product_experiment_attempt(
            self.conn,
            experiment_ref="experiment_e1000001",
            local_date="2026-07-02",
            event_id="event_e1000002",
            useful=False,
            resume_seconds=42.0,
        )
        conflict = record_product_experiment_attempt(
            self.conn,
            experiment_ref="experiment_e1000001",
            local_date="2026-07-02",
            event_id="event_e1000002",
            useful=True,
            resume_seconds=42.0,
        )
        non_finite = record_product_experiment_attempt(
            self.conn,
            experiment_ref="experiment_e1000001",
            local_date="2026-07-03",
            event_id="event_e1000003",
            useful=True,
            resume_seconds=float("nan"),
        )
        self.assertEqual((first["status"], second["status"]), ("recorded", "recorded"))
        self.assertEqual(
            (canonical_replay_without_legacy_alias["status"], canonical_replay_without_legacy_alias["reason_code"]),
            ("replayed", "idempotent_replay"),
        )
        self.assertEqual((replay["status"], replay["reason_code"]), ("replayed", "idempotent_replay"))
        self.assertEqual((conflict["status"], conflict["reason_code"]), ("conflict", "experiment_attempt_conflict"))
        self.assertEqual((non_finite["status"], non_finite["reason_code"]), ("rejected", "invalid_resume_seconds"))
        rows = self.conn.execute(
            "SELECT event_id,useful,outcome,resume_seconds FROM context_product_experiment_attempts "
            "WHERE experiment_ref=? ORDER BY local_date",
            ("experiment_e1000001",),
        ).fetchall()
        self.assertEqual(
            [(row["event_id"], row["useful"], row["outcome"], row["resume_seconds"]) for row in rows],
            [
                ("event_e1000001", 1, "useful", 12.5),
                ("event_e1000002", 0, "not_useful", 42.0),
            ],
        )
        status = product_experiment_status(
            self.conn,
            experiment_ref="experiment_e1000001",
            local_today=date(2026, 7, 2),
        )
        self.assertEqual((status["attempt_count"], status["rated_attempt_count"]), (2, 2))
        self.assertEqual((status["useful_count"], status["not_useful_count"]), (1, 1))
        self.assertEqual(status["useful_rate"], 0.5)
        self.assertEqual(status["median_resume_seconds"], 27.25)

    def test_internal_legacy_attempt_rejects_future_local_date(self) -> None:
        self._start_experiment("experiment_e1000101")
        with mock.patch.object(
            context_console_module,
            "_local_today",
            return_value=date(2026, 7, 1),
        ):
            result = record_product_experiment_attempt(
                self.conn,
                experiment_ref="experiment_e1000101",
                local_date="2026-07-02",
                event_id="event_e1000101",
                useful=True,
                resume_seconds=5.0,
            )
        self.assertEqual(
            (result["status"], result["reason_code"]),
            ("rejected", "attempt_after_current_local_date"),
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM context_product_experiment_attempts "
                "WHERE experiment_ref='experiment_e1000101'"
            ).fetchone()[0],
            0,
        )

    def test_experiment_day13_does_not_evaluate_an_otherwise_passing_value_gate(self) -> None:
        self._start_experiment("experiment_f0000001")
        self._record_experiment_attempts(
            "experiment_f0000001",
            [(True, 20.0)] * 8 + [(False, 20.0)] * 2,
            event_prefix="f000",
        )
        status = product_experiment_status(
            self.conn,
            experiment_ref="experiment_f0000001",
            local_today=date(2026, 7, 13),
        )
        self.assertEqual((status["status"], status["elapsed_natural_days"]), ("collecting", 13))
        self.assertEqual(status["useful_rate"], 0.8)
        self.assertEqual(status["median_resume_seconds"], 20.0)
        self.assertIsNone(status["value_gate_passed"])
        self.assertEqual(status["decision_status"], "pending")

    def test_experiment_useful_rate_79_9_percent_does_not_pass(self) -> None:
        self._start_experiment("experiment_f1000001")
        rows = []
        for index in range(1000):
            useful = index < 799
            rows.append(
                (
                    f"attempt_{index:032x}",
                    "experiment_f1000001",
                    f"event_f1{index:06x}",
                    "2026-07-01",
                    int(useful),
                    "useful" if useful else "not_useful",
                    20.0,
                    f"source_f1{index:06x}",
                    self.now.isoformat(timespec="seconds"),
                )
            )
        self.conn.executemany(
            "INSERT INTO context_product_experiment_attempts("
            "attempt_ref,experiment_ref,event_id,local_date,useful,outcome,resume_seconds,source_ref,created_at"
            ") VALUES(?,?,?,?,?,?,?,?,?)",
            rows,
        )
        self.conn.commit()
        status = product_experiment_status(
            self.conn,
            experiment_ref="experiment_f1000001",
            local_today=date(2026, 7, 14),
        )
        self.assertEqual(status["attempt_count"], 1000)
        self.assertEqual(status["useful_rate"], 0.799)
        self.assertEqual(status["median_resume_seconds"], 20.0)
        self.assertFalse(status["value_gate_passed"])
        self.assertEqual((status["status"], status["decision_status"]), ("not_passed", "not_passed"))

    def test_experiment_day14_with_nine_attempts_extends_to_day21(self) -> None:
        nonstandard = start_product_experiment(
            self.conn,
            experiment_ref="experiment_e2000099",
            confirmation="START_LOCAL_PRODUCT_EXPERIMENT",
            minimum_days=15,
            maximum_days=21,
        )
        self.assertEqual(
            (nonstandard["status"], nonstandard["reason_code"]),
            ("rejected", "invalid_experiment_window"),
        )
        self._start_experiment("experiment_e2000001")
        self._record_experiment_attempts(
            "experiment_e2000001",
            [(True, 10.0)] * 9,
            event_prefix="e200",
        )
        status = product_experiment_status(
            self.conn,
            experiment_ref="experiment_e2000001",
            local_today=date(2026, 7, 14),
        )
        self.assertEqual((status["status"], status["extension_status"]), ("extended", "extended_to_day_21"))
        self.assertEqual((status["attempt_count"], status["complete_attempt_count"]), (9, 9))
        self.assertEqual(status["review_readiness"], "pending")
        self.assertIsNone(status["value_gate_passed"])
        self.assertEqual(status["decision_status"], "pending")

    def test_experiment_day14_ten_attempts_computes_useful_rate_and_median(self) -> None:
        self._start_experiment("experiment_e3000001")
        attempts = [
            (True, 10.0),
            (True, 12.0),
            (True, 14.0),
            (True, 16.0),
            (True, 18.0),
            (True, 20.0),
            (True, 22.0),
            (True, 24.0),
            (False, 26.0),
            (False, 28.0),
        ]
        self._record_experiment_attempts("experiment_e3000001", attempts, event_prefix="e300")
        status = product_experiment_status(
            self.conn,
            experiment_ref="experiment_e3000001",
            local_today=date(2026, 7, 14),
        )
        self.assertEqual(status["status"], "passed")
        self.assertEqual((status["attempt_count"], status["complete_attempt_count"]), (10, 10))
        self.assertEqual(status["useful_rate"], 0.8)
        self.assertEqual(status["median_resume_seconds"], 19.0)
        self.assertTrue(status["value_gate_passed"])
        self.assertEqual(status["decision_status"], "passed")

    def test_experiment_day21_insufficient_attempts_does_not_pass(self) -> None:
        self._start_experiment("experiment_e4000001")
        self._record_experiment_attempts(
            "experiment_e4000001",
            [(True, 15.0)] * 9,
            event_prefix="e400",
        )
        status = product_experiment_status(
            self.conn,
            experiment_ref="experiment_e4000001",
            local_today=date(2026, 7, 21),
        )
        self.assertEqual((status["status"], status["reason_code"]), ("extended", "insufficient_complete_attempts_at_day_14"))
        self.assertEqual(status["extension_status"], "day_21_in_progress")
        self.assertEqual(status["decision_status"], "pending")
        self.assertEqual(status["completion_readiness"], "pending")
        self.assertIsNone(status["value_gate_passed"])
        late = record_product_experiment_attempt(
            self.conn,
            experiment_ref="experiment_e4000001",
            local_date="2026-07-22",
            event_id="event_e4000010",
            useful=True,
            resume_seconds=10.0,
        )
        self.assertEqual((late["status"], late["reason_code"]), ("rejected", "attempt_after_experiment_window"))
        day22 = product_experiment_status(
            self.conn,
            experiment_ref="experiment_e4000001",
            local_today=date(2026, 7, 22),
        )
        self.assertEqual(day22["status"], "insufficient")
        self.assertEqual(day22["decision_status"], "not_passed")
        self.assertEqual(day22["attempt_count"], 9)
        self.assertEqual(day22["evidence_cutoff_local_date"], "2026-07-21")

    def test_experiment_missing_useful_or_resume_evidence_is_null_and_pending(self) -> None:
        self._start_experiment("experiment_e5000001")
        missing_useful = [(True, 10.0)] * 9 + [(None, 10.0)]
        self._record_experiment_attempts(
            "experiment_e5000001",
            missing_useful,
            event_prefix="e500",
        )
        useful_status = product_experiment_status(
            self.conn,
            experiment_ref="experiment_e5000001",
            local_today=date(2026, 7, 14),
        )
        self.assertEqual((useful_status["status"], useful_status["evidence_status"]), ("incomplete", "incomplete"))
        self.assertIsNone(useful_status["useful_rate"])
        self.assertEqual(useful_status["median_resume_seconds"], 10.0)
        self.assertEqual(useful_status["review_readiness"], "pending")
        self.assertIsNone(useful_status["value_gate_passed"])

        # The resume-evidence case is independent from the useful-evidence
        # case; clear only this test fixture before opening another window.
        self.conn.execute(
            "DELETE FROM context_product_experiment_attempts WHERE experiment_ref=?",
            ("experiment_e5000001",),
        )
        self.conn.execute(
            "DELETE FROM context_product_experiments WHERE experiment_ref=?",
            ("experiment_e5000001",),
        )
        self.conn.commit()

        self._start_experiment("experiment_e5000002")
        missing_resume = [(True, 10.0)] * 9 + [(True, None)]
        self._record_experiment_attempts(
            "experiment_e5000002",
            missing_resume,
            event_prefix="e501",
        )
        resume_status = product_experiment_status(
            self.conn,
            experiment_ref="experiment_e5000002",
            local_today=date(2026, 7, 14),
        )
        self.assertEqual((resume_status["status"], resume_status["evidence_status"]), ("incomplete", "incomplete"))
        self.assertEqual(resume_status["useful_rate"], 1.0)
        self.assertIsNone(resume_status["median_resume_seconds"])
        self.assertEqual(resume_status["review_readiness"], "pending")
        self.assertIsNone(resume_status["value_gate_passed"])

    def test_experiment_useful_rate_eighty_percent_is_inclusive(self) -> None:
        self._start_experiment("experiment_e6000001")
        self._record_experiment_attempts(
            "experiment_e6000001",
            [(True, 20.0)] * 8 + [(False, 20.0)] * 2,
            event_prefix="e600",
        )
        status = product_experiment_status(
            self.conn,
            experiment_ref="experiment_e6000001",
            local_today=date(2026, 7, 14),
        )
        self.assertEqual(status["useful_rate"], 0.8)
        self.assertTrue(status["value_gate_passed"])
        self.assertEqual(status["status"], "passed")

    def test_experiment_median_resume_exactly_thirty_seconds_does_not_pass(self) -> None:
        self._start_experiment("experiment_e7000001")
        self._record_experiment_attempts(
            "experiment_e7000001",
            [(True, 29.999)] * 8 + [(False, 29.999)] * 2,
            event_prefix="e700",
        )
        below = product_experiment_status(
            self.conn,
            experiment_ref="experiment_e7000001",
            local_today=date(2026, 7, 14),
        )
        self.assertEqual(below["median_resume_seconds"], 29.999)
        self.assertTrue(below["value_gate_passed"])

        finalized_below = finalize_product_experiment(
            self.conn,
            experiment_ref="experiment_e7000001",
            confirmation=PRODUCT_EXPERIMENT_FINALIZE_CONFIRMATION,
            now=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
        )
        self.assertEqual(
            (finalized_below["finalization_status"], finalized_below["status"]),
            ("finalized", "passed"),
        )

        self._start_experiment("experiment_e7000002")
        self._record_experiment_attempts(
            "experiment_e7000002",
            [(True, 30.0)] * 8 + [(False, 30.0)] * 2,
            event_prefix="e701",
        )
        exact = product_experiment_status(
            self.conn,
            experiment_ref="experiment_e7000002",
            local_today=date(2026, 7, 14),
        )
        self.assertEqual(exact["median_resume_seconds"], 30.0)
        self.assertFalse(exact["value_gate_passed"])
        self.assertEqual((exact["status"], exact["decision_status"]), ("not_passed", "not_passed"))

    def test_experiment_timezones_fail_closed_without_utc_guessing(self) -> None:
        self._start_experiment("experiment_fa000001")

        for snapshot, expected_reason in (
            ("Not/A_Real_Zone", "invalid_experiment_timezone"),
            ("", "experiment_timezone_missing"),
        ):
            self.conn.execute(
                "UPDATE context_product_experiments SET timezone_name=? WHERE experiment_ref=?",
                (snapshot, "experiment_fa000001"),
            )
            self.conn.commit()
            product = product_experiment_status(
                self.conn,
                experiment_ref="experiment_fa000001",
                local_today=date(2026, 7, 14),
            )
            self.assertEqual((product["status"], product["reason_code"]), ("unknown", expected_reason))
            self.assertIsNone(product["value_gate_passed"])
            self.assertIsNone(product["timezone_name"])

            console = context_console_status(self.conn, now=self.now)
            self.assertEqual((console["status"], console["reason_code"]), ("unknown", expected_reason))
            self.assertFalse(console["actions_enabled"])
            self.assertIsNone(console["product_experiment"]["value_gate_passed"])

            path_status = context_console_status_path(self.db, now=self.now)
            self.assertEqual((path_status["status"], path_status["reason_code"]), ("unknown", expected_reason))
            self.assertFalse(path_status["actions_enabled"])
            self.assertIsNone(path_status["product_experiment"]["value_gate_passed"])

        self.conn.execute(
            "UPDATE context_product_experiments SET timezone_name='UTC' WHERE experiment_ref=?",
            ("experiment_fa000001",),
        )
        self.conn.execute("DELETE FROM settings WHERE key='context_local_timezone'")
        self.conn.commit()
        for status in (
            product_experiment_status(
                self.conn,
                experiment_ref="experiment_fa000001",
                local_today=date(2026, 7, 14),
            ),
            context_console_status(self.conn, now=self.now),
            context_console_status_path(self.db, now=self.now),
        ):
            self.assertEqual((status["status"], status["reason_code"]), ("unknown", "context_timezone_missing"))
        self.assertIsNone(
            product_experiment_status(
                self.conn,
                experiment_ref="experiment_fa000001",
                local_today=date(2026, 7, 14),
            )["value_gate_passed"]
        )

        self.conn.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES('context_local_timezone',?,?)",
            ("Not/A_Real_Zone", self.now.isoformat(timespec="seconds")),
        )
        self.conn.commit()
        invalid_current = product_experiment_status(
            self.conn,
            experiment_ref="experiment_fa000001",
            local_today=date(2026, 7, 14),
        )
        self.assertEqual(
            (invalid_current["status"], invalid_current["reason_code"]),
            ("unknown", "invalid_context_timezone"),
        )
        self.assertIsNone(invalid_current["value_gate_passed"])
        for status in (
            context_console_status(self.conn, now=self.now),
            context_console_status_path(self.db, now=self.now),
        ):
            self.assertEqual(
                (status["status"], status["reason_code"]),
                ("unknown", "invalid_context_timezone"),
            )
            self.assertFalse(status["actions_enabled"])
            self.assertIsNone(status["product_experiment"]["value_gate_passed"])

    def test_experiment_no_local_today_uses_snapshot_timezone_across_dst(self) -> None:
        configured = configure_context_local_timezone(
            self.conn,
            timezone_name="America/New_York",
        )
        self.assertEqual(configured["status"], "configured")
        self._start_experiment("experiment_fb000001", start_local_date="2026-02-23")
        configured = configure_context_local_timezone(
            self.conn,
            timezone_name="Asia/Singapore",
        )
        self.assertEqual(configured["status"], "configured")

        before_new_york_midnight = datetime(2026, 3, 8, 4, 30, tzinfo=UTC)
        after_dst_transition = datetime(2026, 3, 8, 7, 30, tzinfo=UTC)
        with mock.patch.object(
            context_console_module,
            "_aware_utc",
            return_value=before_new_york_midnight,
        ):
            day13 = product_experiment_status(
                self.conn,
                experiment_ref="experiment_fb000001",
            )
        with mock.patch.object(
            context_console_module,
            "_aware_utc",
            return_value=after_dst_transition,
        ):
            day14 = product_experiment_status(
                self.conn,
                experiment_ref="experiment_fb000001",
            )
        self.assertEqual((day13["evaluated_local_date"], day13["status"]), ("2026-03-07", "collecting"))
        self.assertEqual((day14["evaluated_local_date"], day14["status"]), ("2026-03-08", "extended"))
        self.assertEqual(day14["timezone_name"], "America/New_York")
        self.assertEqual(
            context_console_module.context_local_timezone_status(self.conn)["timezone_name"],
            "Asia/Singapore",
        )

    def test_experiment_timezone_upgrade_failure_is_atomic(self) -> None:
        self._start_experiment("experiment_fc000001")
        win = record_product_win(
            self.conn,
            win_ref="win_fc000001",
            occurred_at=self.now.isoformat(timespec="seconds"),
            project_ref=self.project,
            outcome_code="handoff_reused",
            source_ref="source_fc000001",
            minutes_saved=3,
        )
        self.assertEqual(win["status"], "recorded")

        self.conn.execute(
            "UPDATE context_product_experiments SET timezone_name='Not/A_Real_Zone' "
            "WHERE experiment_ref='experiment_fc000001'"
        )
        self.conn.commit()
        before_experiments = [
            tuple(row)
            for row in self.conn.execute(
                "SELECT * FROM context_product_experiments ORDER BY experiment_ref"
            ).fetchall()
        ]
        before_wins = [
            tuple(row)
            for row in self.conn.execute(
                "SELECT * FROM context_product_wins ORDER BY win_ref"
            ).fetchall()
        ]
        with self.assertRaises(context_console_module.ContractError) as invalid_snapshot:
            context_console_module._upgrade_context_v2_schema(self.conn)
        self.assertEqual(invalid_snapshot.exception.reason_code, "invalid_experiment_timezone")
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(
            [
                tuple(row)
                for row in self.conn.execute(
                    "SELECT * FROM context_product_experiments ORDER BY experiment_ref"
                ).fetchall()
            ],
            before_experiments,
        )
        self.assertEqual(
            [
                tuple(row)
                for row in self.conn.execute(
                    "SELECT * FROM context_product_wins ORDER BY win_ref"
                ).fetchall()
            ],
            before_wins,
        )

        self.conn.execute(
            "UPDATE context_product_experiments SET timezone_name='UTC' "
            "WHERE experiment_ref='experiment_fc000001'"
        )
        self.conn.execute(
            "UPDATE settings SET value='Not/A_Real_Zone' WHERE key='context_local_timezone'"
        )
        self.conn.commit()
        before_experiments = [
            tuple(row)
            for row in self.conn.execute(
                "SELECT * FROM context_product_experiments ORDER BY experiment_ref"
            ).fetchall()
        ]
        before_wins = [
            tuple(row)
            for row in self.conn.execute(
                "SELECT * FROM context_product_wins ORDER BY win_ref"
            ).fetchall()
        ]
        with self.assertRaises(context_console_module.ContractError) as invalid_setting:
            context_console_module._upgrade_context_v2_schema(self.conn)
        self.assertEqual(invalid_setting.exception.reason_code, "invalid_context_timezone")
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(
            [
                tuple(row)
                for row in self.conn.execute(
                    "SELECT * FROM context_product_experiments ORDER BY experiment_ref"
                ).fetchall()
            ],
            before_experiments,
        )
        self.assertEqual(
            [
                tuple(row)
                for row in self.conn.execute(
                    "SELECT * FROM context_product_wins ORDER BY win_ref"
                ).fetchall()
            ],
            before_wins,
        )

    def test_experiment_float_replay_requires_exact_ieee_value(self) -> None:
        self._start_experiment("experiment_fd000001")
        first = record_product_experiment_attempt(
            self.conn,
            experiment_ref="experiment_fd000001",
            local_date="2026-07-01",
            event_id="event_fd000001",
            useful=True,
            resume_seconds=29.9999999995,
        )
        exact = record_product_experiment_attempt(
            self.conn,
            experiment_ref="experiment_fd000001",
            local_date="2026-07-01",
            event_id="event_fd000001",
            useful=True,
            resume_seconds=29.9999999995,
        )
        conflict = record_product_experiment_attempt(
            self.conn,
            experiment_ref="experiment_fd000001",
            local_date="2026-07-01",
            event_id="event_fd000001",
            useful=True,
            resume_seconds=30.0,
        )
        self.assertEqual(first["status"], "recorded")
        self.assertEqual((exact["status"], exact["reason_code"]), ("replayed", "idempotent_replay"))
        self.assertEqual(
            (conflict["status"], conflict["reason_code"]),
            ("conflict", "experiment_attempt_conflict"),
        )
        stored = self.conn.execute(
            "SELECT resume_seconds FROM context_product_experiment_attempts "
            "WHERE experiment_ref=? AND event_id=?",
            ("experiment_fd000001", "event_fd000001"),
        ).fetchone()[0]
        self.assertEqual(float(stored).hex(), float(29.9999999995).hex())

    def test_day14_terminal_is_immutable_and_rejects_later_attempts(self) -> None:
        terminal_cases = (
            ("experiment_fe000001", [(True, 20.0)] * 8 + [(False, 20.0)] * 2, "passed", "a100"),
            ("experiment_fe000002", [(True, 20.0)] * 7 + [(False, 20.0)] * 3, "not_passed", "a200"),
        )
        for experiment_ref, attempts, expected_status, event_prefix in terminal_cases:
            self._start_experiment(experiment_ref)
            self._record_experiment_attempts(
                experiment_ref,
                attempts,
                event_prefix=event_prefix,
            )
            terminal = product_experiment_status(
                self.conn,
                experiment_ref=experiment_ref,
                local_today=date(2026, 7, 14),
            )
            self.assertEqual(terminal["status"], expected_status)
            self.assertEqual(terminal["evidence_cutoff_local_date"], "2026-07-14")
            stored_terminal = self.conn.execute(
                "SELECT status,ended_local_date FROM context_product_experiments "
                "WHERE experiment_ref=?",
                (experiment_ref,),
            ).fetchone()
            self.assertEqual(tuple(stored_terminal), ("running", None))
            frozen_stats = {
                key: terminal[key]
                for key in (
                    "attempt_count",
                    "complete_attempt_count",
                    "useful_rate",
                    "median_resume_seconds",
                    "value_gate_passed",
                )
            }

            replay = record_product_experiment_attempt(
                self.conn,
                experiment_ref=experiment_ref,
                local_date="2026-07-01",
                event_id=f"event_{event_prefix}0001",
                useful=attempts[0][0],
                resume_seconds=attempts[0][1],
            )
            self.assertEqual((replay["status"], replay["reason_code"]), ("replayed", "idempotent_replay"))
            later_results = [
                record_product_experiment_attempt(
                    self.conn,
                    experiment_ref=experiment_ref,
                    local_date=f"2026-07-{local_day:02d}",
                    event_id=f"event_{event_prefix}{local_day:04x}",
                    useful=expected_status == "not_passed",
                    resume_seconds=10.0,
                )
                for local_day in range(15, 20)
            ]
            self.assertEqual(
                [(result["status"], result["reason_code"]) for result in later_results],
                [("rejected", "experiment_not_running")] * 5,
            )
            self.assertEqual(
                tuple(
                    self.conn.execute(
                        "SELECT status,ended_local_date FROM context_product_experiments "
                        "WHERE experiment_ref=?",
                        (experiment_ref,),
                    ).fetchone()
                ),
                (expected_status, "2026-07-14"),
            )
            self.assertEqual(
                self.conn.execute(
                    "SELECT COUNT(*) FROM context_product_experiment_attempts "
                    "WHERE experiment_ref=?",
                    (experiment_ref,),
                ).fetchone()[0],
                10,
            )
            after = product_experiment_status(
                self.conn,
                experiment_ref=experiment_ref,
                local_today=date(2026, 7, 21),
            )
            self.assertEqual(
                {
                    key: after[key]
                    for key in (
                        "attempt_count",
                        "complete_attempt_count",
                        "useful_rate",
                        "median_resume_seconds",
                        "value_gate_passed",
                    )
                },
                frozen_stats,
            )
            self.assertEqual(after["evidence_cutoff_local_date"], "2026-07-14")

    def test_only_insufficient_complete_attempts_extend_and_missing_stays_pending(self) -> None:
        self._start_experiment("experiment_ff000001")
        self._record_experiment_attempts(
            "experiment_ff000001",
            [(True, 10.0)] * 9,
            event_prefix="b100",
        )
        day14 = product_experiment_status(
            self.conn,
            experiment_ref="experiment_ff000001",
            local_today=date(2026, 7, 14),
        )
        self.assertEqual((day14["status"], day14["extension_status"]), ("extended", "extended_to_day_21"))
        tenth = record_product_experiment_attempt(
            self.conn,
            experiment_ref="experiment_ff000001",
            local_date="2026-07-15",
            event_id="event_b100000a",
            useful=False,
            resume_seconds=10.0,
        )
        self.assertEqual(tenth["status"], "recorded")
        day15 = product_experiment_status(
            self.conn,
            experiment_ref="experiment_ff000001",
            local_today=date(2026, 7, 15),
        )
        self.assertEqual(day15["status"], "passed")
        self.assertEqual(day15["evidence_cutoff_local_date"], "2026-07-15")
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT status,ended_local_date FROM context_product_experiments "
                    "WHERE experiment_ref='experiment_ff000001'"
                ).fetchone()
            ),
            ("passed", "2026-07-15"),
        )

        self._start_experiment("experiment_ff000002")
        self._record_experiment_attempts(
            "experiment_ff000002",
            [(True, 10.0)] * 9 + [(None, 10.0)],
            event_prefix="b200",
        )
        missing_day14 = product_experiment_status(
            self.conn,
            experiment_ref="experiment_ff000002",
            local_today=date(2026, 7, 14),
        )
        self.assertEqual((missing_day14["status"], missing_day14["extension_status"]), ("incomplete", "not_applicable"))
        self.assertIsNone(missing_day14["value_gate_passed"])
        extra_complete = record_product_experiment_attempt(
            self.conn,
            experiment_ref="experiment_ff000002",
            local_date="2026-07-15",
            event_id="event_b200000b",
            useful=True,
            resume_seconds=10.0,
        )
        self.assertEqual(extra_complete["status"], "recorded")
        missing_day21 = product_experiment_status(
            self.conn,
            experiment_ref="experiment_ff000002",
            local_today=date(2026, 7, 21),
        )
        self.assertEqual((missing_day21["status"], missing_day21["decision_status"]), ("incomplete", "pending"))
        self.assertIsNone(missing_day21["value_gate_passed"])
        self.assertIsNone(missing_day21["useful_rate"])
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT status,ended_local_date FROM context_product_experiments "
                    "WHERE experiment_ref='experiment_ff000002'"
                ).fetchone()
            ),
            ("running", None),
        )

    def test_retention_boundaries_are_two_ninety_and_thirty_local_days(self) -> None:
        invalid = configure_context_local_timezone(self.conn, timezone_name="Not/A_Real_Zone")
        self.assertEqual((invalid["status"], invalid["reason_code"]), ("rejected", "invalid_context_timezone"))
        configured = configure_context_local_timezone(self.conn, timezone_name="Asia/Singapore")
        self.assertEqual(configured["status"], "configured")
        status_payload = context_console_status(self.conn, now=self.now)
        self.assertEqual(status_payload["local_day_policy"]["timezone_name"], "Asia/Singapore")
        zone = ZoneInfo("Asia/Singapore")
        occurred_local = datetime(2026, 7, 10, 23, 30, 0, tzinfo=zone)
        occurred_utc = occurred_local.astimezone(UTC)

        clue = record_unverified_clue(
            self.conn,
            source_kind="screen",
            source_ref="source_e8000001",
            occurred_at=occurred_utc.isoformat(timespec="seconds"),
            project_ref=self.project,
            workstream_ref=self.workstream,
            object_ref=self.object,
            version_ref=self.version,
            subject_ref=self.owner,
            privacy_class="private_local",
            now=occurred_utc,
        )
        self.assertEqual(clue["status"], "accepted")
        clue_expiry = datetime.fromisoformat(
            self.conn.execute(
                "SELECT expires_at FROM context_observations WHERE observation_ref=?",
                (clue["observation_ref"],),
            ).fetchone()[0]
        ).astimezone(zone)

        verified = self._ingest_trusted_confirmation(
            idempotency_key="idem_e8000001",
            source_ref="source_e8000002",
            occurred_at=occurred_utc,
            ingest_now=occurred_utc,
        )
        self.assertEqual(verified["status"], "accepted")
        verified_expiry = datetime.fromisoformat(
            self.conn.execute(
                "SELECT expires_at FROM context_verified WHERE verified_ref=?",
                (verified["verified_ref"],),
            ).fetchone()[0]
        ).astimezone(zone)

        win = record_product_win(
            self.conn,
            win_ref="win_e8000001",
            occurred_at=occurred_utc.isoformat(timespec="seconds"),
            project_ref=self.project,
            outcome_code="handoff_reused",
            source_ref="source_e8000003",
            minutes_saved=3,
        )
        self.assertEqual(win["status"], "recorded")
        win_expiry = datetime.fromisoformat(win["expires_at"]).astimezone(zone)

        self.assertEqual(clue_expiry, datetime(2026, 7, 12, 0, 0, tzinfo=zone))
        self.assertEqual(verified_expiry, datetime.combine(occurred_local.date() + timedelta(days=90), datetime.min.time(), tzinfo=zone))
        self.assertEqual(win_expiry, datetime.combine(occurred_local.date() + timedelta(days=30), datetime.min.time(), tzinfo=zone))
        self.assertEqual(win["expires_local_date"], (occurred_local.date() + timedelta(days=30)).isoformat())

        clue_before = preview_context_retention(
            self.conn,
            now=clue_expiry.astimezone(UTC) - timedelta(seconds=1),
        )
        clue_at = preview_context_retention(self.conn, now=clue_expiry.astimezone(UTC))
        self.assertEqual(clue_before["would_expire_or_delete"]["unverified_observations"], 0)
        self.assertEqual(clue_at["would_expire_or_delete"]["unverified_observations"], 1)

        win_before = preview_context_retention(
            self.conn,
            now=win_expiry.astimezone(UTC) - timedelta(seconds=1),
        )
        win_at = preview_context_retention(self.conn, now=win_expiry.astimezone(UTC))
        self.assertEqual(win_before["would_expire_or_delete"]["product_wins"], 0)
        self.assertEqual(win_at["would_expire_or_delete"]["product_wins"], 1)

        verified_before = preview_context_retention(
            self.conn,
            now=verified_expiry.astimezone(UTC) - timedelta(seconds=1),
        )
        verified_at = preview_context_retention(self.conn, now=verified_expiry.astimezone(UTC))
        self.assertEqual(verified_before["would_expire_or_delete"]["verified_contexts"], 0)
        self.assertEqual(verified_at["would_expire_or_delete"]["verified_contexts"], 1)

    def test_product_wins_expire_on_30_local_natural_day_boundary(self) -> None:
        configure_context_local_timezone(self.conn, timezone_name="America/New_York")
        zone = ZoneInfo("America/New_York")
        occurred_local = datetime(2026, 3, 1, 12, 0, 0, tzinfo=zone)
        occurred_utc = occurred_local.astimezone(UTC)
        win = record_product_win(
            self.conn,
            win_ref="win_e9000001",
            occurred_at=occurred_utc.isoformat(timespec="seconds"),
            project_ref=self.project,
            outcome_code="trusted_context_avoided_rework",
            source_ref="source_e9000001",
            minutes_saved=8,
        )
        boundary_local = datetime(2026, 3, 31, 0, 0, 0, tzinfo=zone)
        boundary_utc = boundary_local.astimezone(UTC)
        self.assertEqual(win["expires_local_date"], "2026-03-31")
        self.assertEqual(datetime.fromisoformat(win["expires_at"]), boundary_utc)
        self.assertEqual(product_win_status(self.conn, now=boundary_utc - timedelta(seconds=1))["active_win_count"], 1)
        self.assertEqual(product_win_status(self.conn, now=boundary_utc)["active_win_count"], 0)
        cleanup = cleanup_context_retention(self.conn, now=boundary_utc)
        self.assertEqual(cleanup["product_wins_deleted"], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM context_product_wins").fetchone()[0], 0)

    def test_product_wins_survive_short_activity_retention_cleanup(self) -> None:
        from activity_mvp.daemon import cleanup_retention

        now = datetime.now(UTC).replace(microsecond=0)
        win = record_product_win(
            self.conn,
            win_ref="win_ea000001",
            occurred_at=now.isoformat(timespec="seconds"),
            project_ref=self.project,
            outcome_code="handoff_reused",
            source_ref="source_ea000001",
            minutes_saved=5,
        )
        self.assertEqual(win["status"], "recorded")
        result = cleanup_retention(self.conn, self.root, days=1)
        self.assertEqual(result["context_retention"]["product_wins_deleted"], 0)
        self.assertEqual(product_win_status(self.conn, now=now)["active_win_count"], 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM context_product_wins WHERE win_ref=?", ("win_ea000001",)).fetchone()[0],
            1,
        )

    def test_build_soak_report_uses_per_component_effective_windows(self) -> None:
        baselines = {
            "capture": self.now - timedelta(days=5),
            "privacy": self.now - timedelta(days=3),
            "storage": self.now - timedelta(days=4),
            "context_fusion": self.now - timedelta(days=2),
        }
        for index, component in enumerate(baselines, start=1):
            result = record_component_baseline(
                self.conn,
                component=component,
                version_ref=f"version_eb00000{index}",
                reset_reason_code=f"{component}_changed",
            )
            self.assertEqual(result["generation"], 1)
        for component, recorded_at in baselines.items():
            self.conn.execute(
                "UPDATE context_component_baselines SET recorded_at=? WHERE component=?",
                (recorded_at.isoformat(timespec="seconds"), component),
            )
        self.conn.commit()

        report = build_soak_report(
            self.conn,
            self.root,
            days=7,
            end_at=self.now.isoformat(timespec="seconds"),
        )
        windows = report["window"]["component_windows"]
        for component, recorded_at in baselines.items():
            self.assertEqual(windows[component]["effective_start"], recorded_at.isoformat(timespec="seconds"))
            self.assertEqual(windows[component]["generation"], 1)
        gates = {gate["code"]: gate for gate in report["gates"]}
        self.assertEqual(gates["observation_window"]["dependencies"], ["capture"])
        self.assertEqual(gates["observation_window"]["effective_window"]["effective_start"], baselines["capture"].isoformat(timespec="seconds"))
        self.assertEqual(gates["raw_safety"]["effective_window"]["effective_start"], baselines["privacy"].isoformat(timespec="seconds"))
        self.assertEqual(gates["resource_regression"]["effective_window"]["effective_start"], baselines["storage"].isoformat(timespec="seconds"))
        self.assertEqual(gates["context_recovery"]["effective_window"]["effective_start"], baselines["context_fusion"].isoformat(timespec="seconds"))
        self.assertEqual(gates["control_drills"]["dependencies"], ["capture", "privacy"])
        self.assertEqual(gates["control_drills"]["effective_window"]["effective_start"], baselines["privacy"].isoformat(timespec="seconds"))

        runtime = runtime_stability_status(self.conn, now=self.now, days=7)
        runtime_windows = {item["component"]: item for item in runtime["components"]}
        for component in baselines:
            self.assertEqual(runtime_windows[component]["generation"], windows[component]["generation"])
            self.assertEqual(runtime_windows[component]["effective_start"], windows[component]["effective_start"])

    def test_non_utc_soak_end_uses_same_effective_start_representation_as_runtime(self) -> None:
        baseline_at = "2026-07-28T09:00:00+08:00"
        for index, component in enumerate(
            ("capture", "privacy", "storage", "context_fusion"),
            start=1,
        ):
            result = record_component_baseline(
                self.conn,
                component=component,
                version_ref=f"version_f200000{index}",
                reset_reason_code=f"{component}_changed",
            )
            self.assertEqual(result["status"], "baseline_recorded")
        self.conn.execute(
            "UPDATE context_component_baselines SET recorded_at=?",
            (baseline_at,),
        )
        self.conn.commit()
        non_utc_end = datetime(
            2026,
            7,
            29,
            9,
            0,
            tzinfo=timezone(timedelta(hours=8)),
        )
        report = build_soak_report(
            self.conn,
            self.root,
            days=7,
            end_at=non_utc_end.isoformat(timespec="seconds"),
        )
        runtime = runtime_stability_status(
            self.conn,
            now=non_utc_end,
            days=7,
        )
        report_windows = report["window"]["component_windows"]
        runtime_windows = {
            component["component"]: component
            for component in runtime["components"]
        }
        for component in ("capture", "privacy", "storage", "context_fusion"):
            self.assertEqual(
                report_windows[component]["effective_start"],
                runtime_windows[component]["effective_start"],
            )
            self.assertEqual(
                report_windows[component]["effective_start"],
                "2026-07-28T01:00:00+00:00",
            )
            self.assertEqual(
                report_windows[component]["to"],
                runtime_windows[component]["to"],
            )

    def test_missing_component_baseline_remains_unbaselined_without_global_fallback(self) -> None:
        record_resource_baseline(
            self.conn,
            source_version="legacy-global",
            cpu_percent=10.0,
            rss_bytes=1024,
            disk_bytes=2048,
            recorded_at=(self.now - timedelta(days=10)).isoformat(timespec="seconds"),
        )
        report = build_soak_report(
            self.conn,
            self.root,
            days=7,
            end_at=self.now.isoformat(timespec="seconds"),
        )
        storage = report["window"]["component_windows"]["storage"]
        self.assertEqual((storage["baseline_status"], storage["readiness"]), ("unbaselined", "unknown"))
        self.assertIsNone(storage["effective_start"])
        self.assertEqual(storage["generation"], 0)
        self.assertIsNone(report["window"]["campaign_started_at"])
        self.assertEqual(report["resources"]["baseline"]["status"], "recorded")
        resource_gate = next(gate for gate in report["gates"] if gate["code"] == "resource_regression")
        self.assertEqual(resource_gate["status"], "unknown")
        self.assertEqual(resource_gate["effective_window"]["baseline_status"], "unbaselined")
        self.assertEqual(resource_gate["effective_window"]["missing_components"], ["storage"])

    def test_ui_and_copy_changes_reset_no_component(self) -> None:
        record_component_baseline(
            self.conn,
            component="capture",
            version_ref="version_ec000001",
            reset_reason_code="capture_changed",
        )
        before = [tuple(row) for row in self.conn.execute(
            "SELECT component,version_ref,generation,reset_reason_code,recorded_at "
            "FROM context_component_baselines ORDER BY component"
        ).fetchall()]
        for change_kind in ("ui", "copy"):
            result = classify_stability_change(change_kind)
            self.assertEqual(result["status"], "no_reset")
            self.assertEqual(result["affected_components"], [])
            self.assertFalse(result["product_experiment_reset"])
        after = [tuple(row) for row in self.conn.execute(
            "SELECT component,version_ref,generation,reset_reason_code,recorded_at "
            "FROM context_component_baselines ORDER BY component"
        ).fetchall()]
        self.assertEqual(after, before)

    def test_component_reset_does_not_reset_product_experiment_or_wins(self) -> None:
        self._start_experiment("experiment_ed000001")
        attempt = record_product_experiment_attempt(
            self.conn,
            experiment_ref="experiment_ed000001",
            local_date="2026-07-01",
            event_id="event_ed000001",
            useful=True,
            resume_seconds=9.0,
        )
        self.assertEqual(attempt["status"], "recorded")
        win = record_product_win(
            self.conn,
            win_ref="win_ed000001",
            occurred_at=self.now.isoformat(timespec="seconds"),
            project_ref=self.project,
            outcome_code="handoff_reused",
            source_ref="source_ed000001",
            minutes_saved=7,
        )
        self.assertEqual(win["status"], "recorded")
        before_experiment = product_experiment_status(
            self.conn,
            experiment_ref="experiment_ed000001",
            local_today=date(2026, 7, 14),
        )
        before_wins = product_win_status(self.conn, now=self.now)
        reset = record_component_baseline(
            self.conn,
            component="context_fusion",
            version_ref="version_ed000001",
            reset_reason_code="context_fusion_changed",
        )
        self.assertFalse(reset["product_experiment_reset"])
        self.assertFalse(reset["product_wins_reset"])
        after_experiment = product_experiment_status(
            self.conn,
            experiment_ref="experiment_ed000001",
            local_today=date(2026, 7, 14),
        )
        after_wins = product_win_status(self.conn, now=self.now)
        self.assertEqual(after_experiment, before_experiment)
        self.assertEqual(after_wins, before_wins)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM context_product_experiment_attempts").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM context_product_wins").fetchone()[0], 1)

    def test_component_baseline_generation_is_atomic_under_concurrency(self) -> None:
        barrier = threading.Barrier(3)
        results: list[dict] = []
        failures: list[BaseException] = []

        def same_component_worker(version_ref: str) -> None:
            conn = connect(self.db, busy_timeout_seconds=5)
            try:
                barrier.wait(5)
                results.append(
                    record_component_baseline(
                        conn,
                        component="context_fusion",
                        version_ref=version_ref,
                        reset_reason_code="context_fusion_changed",
                    )
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)
            finally:
                conn.close()

        threads = [
            threading.Thread(target=same_component_worker, args=("version_ee000001",)),
            threading.Thread(target=same_component_worker, args=("version_ee000002",)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait(5)
        for thread in threads:
            thread.join(10)
        self.assertFalse(failures)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(sorted(result["generation"] for result in results), [1, 2])
        self.assertEqual(
            self.conn.execute(
                "SELECT generation FROM context_component_baselines WHERE component='context_fusion'"
            ).fetchone()[0],
            2,
        )

        independent_barrier = threading.Barrier(3)
        independent: dict[str, dict] = {}

        def independent_worker(component: str, version_ref: str) -> None:
            conn = connect(self.db, busy_timeout_seconds=5)
            try:
                independent_barrier.wait(5)
                independent[component] = record_component_baseline(
                    conn,
                    component=component,
                    version_ref=version_ref,
                    reset_reason_code=f"{component}_changed",
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)
            finally:
                conn.close()

        independent_threads = [
            threading.Thread(target=independent_worker, args=("privacy", "version_ee000003")),
            threading.Thread(target=independent_worker, args=("storage", "version_ee000004")),
        ]
        for thread in independent_threads:
            thread.start()
        independent_barrier.wait(5)
        for thread in independent_threads:
            thread.join(10)
        self.assertFalse(failures)
        self.assertFalse(any(thread.is_alive() for thread in independent_threads))
        self.assertEqual(independent["privacy"]["generation"], 1)
        self.assertEqual(independent["storage"]["generation"], 1)
        self.assertEqual(
            dict(
                self.conn.execute(
                    "SELECT component,generation FROM context_component_baselines "
                    "WHERE component IN ('privacy','storage')"
                ).fetchall()
            ),
            {"privacy": 1, "storage": 1},
        )
    @staticmethod
    def _primary_key_columns(conn: sqlite3.Connection, table: str) -> list[str]:
        rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        keyed = sorted(
            (row for row in rows if int(row["pk"] or 0) > 0),
            key=lambda row: int(row["pk"]),
        )
        return [str(row["name"]) for row in keyed]

    @staticmethod
    def _clone_sqlite_database(source: Path, destination: Path) -> None:
        source_conn = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)
        destination_conn = sqlite3.connect(destination)
        try:
            source_conn.backup(destination_conn)
        finally:
            destination_conn.close()
            source_conn.close()

    def _drop_context_objects(self, conn: sqlite3.Connection) -> None:
        conn.commit()
        conn.execute("PRAGMA foreign_keys=OFF")
        virtual = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_search_index'"
        ).fetchone()
        if virtual:
            conn.execute("DROP TABLE context_search_index")
        tables = [
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'context_%' ORDER BY name DESC"
            ).fetchall()
        ]
        for table in tables:
            conn.execute(f'DROP TABLE "{table}"')

    def _prepare_pre_context_fixture(
        self,
        path: Path,
        *,
        version: int,
        include_v04_schema29: bool = False,
        identity_marker: str = "fixture",
    ) -> None:
        conn = ensure_ready(path)
        try:
            self._drop_context_objects(conn)
            conn.execute("DROP TRIGGER IF EXISTS pam_schema32_guard_insert")
            conn.execute("DROP TRIGGER IF EXISTS pam_schema32_guard_update")
            conn.execute("DROP TRIGGER IF EXISTS pam_schema31_guard_insert")
            conn.execute("DROP TRIGGER IF EXISTS pam_schema31_guard_update")
            conn.execute("DROP TRIGGER IF EXISTS pam_schema30_guard_insert")
            conn.execute("DROP TRIGGER IF EXISTS pam_schema30_guard_update")
            if include_v04_schema29:
                conn.executescript(V04_SCHEMA29_SQL)
                self._insert_v04_schema29_rows(conn, marker=identity_marker)
            stamp = self.now.isoformat(timespec="seconds")
            for key in (
                "ha_auto_package_enabled",
                "context_memory_writer_enabled",
                "context_source_revocation_cascade_ready",
            ):
                conn.execute(
                    "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                    (key, "true", stamp),
                )
            conn.execute(
                "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                ("database_identity_marker", identity_marker, stamp),
            )
            conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(version),))
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()

    def _insert_v04_schema29_rows(self, conn: sqlite3.Connection, *, marker: str) -> None:
        stamp = self.now.isoformat(timespec="seconds")
        episode_id = f"episode_{marker}"
        event_id = f"event_{marker}"
        outbox_id = f"outbox_{marker}"
        conn.execute(
            "INSERT INTO task_episodes(episode_id,explicit_task_id,workstream,subject,object,version,"
            "current_state,execution_status,execution_status_updated_at,root_turn_ref,project_ref,"
            "binding_mode,highest_source_tier,confirmation_state,privacy_class,memory_write_eligible,"
            "evidence_ids,confirmation_item_key,started_at,ended_at,event_count,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                episode_id,
                f"task_{marker}",
                f"workstream_{marker}",
                f"subject_{marker}",
                f"object_{marker}",
                f"version_{marker}",
                "artifact_saved",
                "completed",
                stamp,
                f"turn_root_{marker}",
                f"project_{marker}",
                "explicit",
                "P1",
                "confirmed",
                "private_local",
                0,
                json.dumps([f"evidence_{marker}"]),
                f"confirmation_{marker}",
                stamp,
                stamp,
                1,
                stamp,
                stamp,
            ),
        )
        conn.execute(
            "INSERT INTO task_evidence_events(event_id,episode_id,task_id,occurred_at,source_kind,"
            "source_tier,workstream,subject,action,object,version,state_claim,execution_status,"
            "session_ref,turn_ref,root_turn_ref,project_ref,binding_mode,evidence_ids,confidence,"
            "confirmation_state,privacy_class,memory_write_eligible,contract_version,source_ref,"
            "event_fingerprint,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                episode_id,
                f"task_{marker}",
                stamp,
                "file",
                "P1",
                f"workstream_{marker}",
                f"subject_{marker}",
                "saved",
                f"object_{marker}",
                f"version_{marker}",
                "artifact_saved",
                "completed",
                f"session_{marker}",
                f"turn_{marker}",
                f"turn_root_{marker}",
                f"project_{marker}",
                "explicit",
                json.dumps([f"evidence_{marker}"]),
                0.99,
                "confirmed",
                "private_local",
                0,
                1,
                f"source_{marker}",
                f"fingerprint_{marker}",
                stamp,
                stamp,
            ),
        )
        conn.execute(
            "INSERT INTO hermes_outbox(outbox_id,episode_id,event_id,payload_kind,idempotency_key,"
            "payload_json,status,attempt_count,next_attempt_at,last_error,created_at,updated_at,delivered_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                outbox_id,
                episode_id,
                event_id,
                "task_episode",
                f"outbox_idem_{marker}",
                json.dumps({"opaque": marker}, separators=(",", ":")),
                "delivered",
                1,
                None,
                "",
                stamp,
                stamp,
                stamp,
            ),
        )
        conn.execute(
            "INSERT INTO hermes_delivery_receipts(receipt_id,outbox_id,idempotency_key,delivery_status,"
            "provider_receipt_id,attempt_number,acknowledged_at,safe_metadata,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                f"receipt_{marker}",
                outbox_id,
                f"receipt_idem_{marker}",
                "acknowledged",
                f"provider_{marker}",
                1,
                stamp,
                json.dumps({"marker": marker}, separators=(",", ":")),
                stamp,
            ),
        )

    def _create_candidate_v1_fixture(self, path: Path) -> None:
        conn = ensure_ready(path)
        try:
            conn.commit()
            conn.execute("PRAGMA foreign_keys=OFF")
            # Reconstruct the historical schema-30 candidate-table shape. The
            # schema-31 guard is intentionally removed only inside this test
            # fixture; the schema-30 guard remains active.
            conn.execute("DROP TRIGGER IF EXISTS pam_schema32_guard_insert")
            conn.execute("DROP TRIGGER IF EXISTS pam_schema32_guard_update")
            conn.execute("DROP TRIGGER IF EXISTS pam_schema31_guard_insert")
            conn.execute("DROP TRIGGER IF EXISTS pam_schema31_guard_update")
            conn.execute("UPDATE meta SET value='30' WHERE key='schema_version'")
            conn.execute("DELETE FROM settings WHERE key='context_candidate_table_rebuild_version'")
            conn.execute("DROP TABLE context_ingest_receipts")
            conn.execute("DROP TABLE context_delivery_receipts")
            conn.execute(
                "CREATE TABLE context_ingest_receipts("
                "receipt_ref TEXT NOT NULL UNIQUE,contract_type TEXT NOT NULL,"
                "principal_id TEXT NOT NULL,key_id TEXT NOT NULL,"
                "source_ref TEXT NOT NULL,idempotency_key TEXT PRIMARY KEY,payload_hash TEXT NOT NULL,"
                "source_event_ref TEXT NOT NULL DEFAULT '',"
                "received_at TEXT NOT NULL,occurred_at TEXT NOT NULL,"
                "project_ref TEXT NOT NULL,observation_ref TEXT,result_status TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE INDEX idx_context_receipts_time "
                "ON context_ingest_receipts(received_at,contract_type)"
            )
            conn.execute(
                "CREATE TABLE context_delivery_receipts("
                "receipt_ref TEXT NOT NULL UNIQUE,delivery_ref TEXT NOT NULL,"
                "principal_id TEXT NOT NULL,key_id TEXT NOT NULL,"
                "idempotency_key TEXT PRIMARY KEY,payload_hash TEXT NOT NULL,"
                "transition TEXT NOT NULL,occurred_at TEXT NOT NULL,bundle_hash TEXT NOT NULL,"
                "provider_receipt_ref TEXT NOT NULL DEFAULT '',"
                "failure_reason_code TEXT NOT NULL DEFAULT '',result_state TEXT NOT NULL,"
                "created_at TEXT NOT NULL,"
                "FOREIGN KEY(delivery_ref) REFERENCES context_deliveries(delivery_ref))"
            )
            stamp = self.now.isoformat(timespec="seconds")
            for index in (1, 2):
                bundle_ref = f"bundle_fault_{index}"
                delivery_ref = f"delivery_fault_{index}"
                bundle_hash = f"sha256:bundle_fault_{index}"
                conn.execute(
                    "INSERT INTO context_bundle_manifests(bundle_ref,bundle_hash,created_at) "
                    "VALUES(?,?,?)",
                    (bundle_ref, bundle_hash, stamp),
                )
                conn.execute(
                    "INSERT INTO context_deliveries(delivery_ref,bundle_ref,bundle_hash,target_ref,state,"
                    "prepared_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        delivery_ref,
                        bundle_ref,
                        bundle_hash,
                        f"target_fault_{index}",
                        "prepared",
                        stamp,
                        stamp,
                        stamp,
                    ),
                )
                conn.execute(
                    "INSERT INTO context_ingest_receipts(receipt_ref,contract_type,principal_id,key_id,"
                    "source_ref,idempotency_key,payload_hash,source_event_ref,received_at,occurred_at,"
                    "project_ref,observation_ref,result_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        f"ingest_receipt_fault_{index}",
                        "ContextReceiptV1",
                        f"principal_fault_{index}",
                        f"key_fault_{index}",
                        f"source_fa{index:06x}",
                        f"ingest_idem_fault_{index}",
                        f"sha256:ingest_fault_{index}",
                        "",
                        stamp,
                        stamp,
                        f"project_fault_{index}",
                        None if index == 1 else f"observation_fault_{index}",
                        "accepted" if index == 1 else "replayed",
                    ),
                )
                conn.execute(
                    "INSERT INTO context_delivery_receipts(receipt_ref,delivery_ref,principal_id,key_id,"
                    "idempotency_key,payload_hash,transition,occurred_at,bundle_hash,provider_receipt_ref,"
                    "failure_reason_code,result_state,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        f"delivery_receipt_fault_{index}",
                        delivery_ref,
                        f"principal_delivery_{index}",
                        f"key_delivery_{index}",
                        f"delivery_idem_fault_{index}",
                        f"sha256:delivery_fault_{index}",
                        "consumed" if index == 1 else "delivered",
                        stamp,
                        bundle_hash,
                        "" if index == 1 else f"provider_fault_{index}",
                        "",
                        "consumed" if index == 1 else "delivered",
                        stamp,
                    ),
                )
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()

    @staticmethod
    def _insert_confirmation_fixture(
        conn: sqlite3.Connection,
        *,
        item_key: str,
        state: str,
        answer: str | None,
        candidate_origin: str = "explicit_claim",
        answer_reason: str | None = None,
        user_note: str = "",
        user_workstream: str = "",
        user_outcome: str = "",
        generalize_scope: str = "exact",
        answered_at: str | None = None,
        stamp: str,
    ) -> None:
        conn.execute(
            "INSERT INTO activity_confirmation_items(item_key,event_type,safe_observation,"
            "decision_question,why_uncertain,observed_start,observed_end,fingerprint,calibration_key,"
            "state,answer,answer_reason,user_note,user_workstream,user_outcome,generalize_scope,"
            "candidate_origin,notification_eligible,created_at,updated_at,answered_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                item_key,
                "observed_activity",
                f"safe observation {item_key}",
                "Did this happen?",
                "screen-derived clue",
                stamp,
                stamp,
                f"fingerprint_{item_key}",
                f"calibration_{item_key}",
                state,
                answer,
                answer_reason,
                user_note,
                user_workstream,
                user_outcome,
                generalize_scope,
                candidate_origin,
                1,
                stamp,
                stamp,
                answered_at,
            ),
        )

    def test_candidate_schema30_rebuild_is_atomic_and_resumable_after_injected_fault(self) -> None:
        fixture = self.root / "candidate-v1-fixture.sqlite"
        self._create_candidate_v1_fixture(fixture)
        check = sqlite3.connect(f"file:{fixture.resolve()}?mode=ro", uri=True)
        check.row_factory = sqlite3.Row
        try:
            expected_ingest = [
                tuple(row)
                for row in check.execute(
                    "SELECT receipt_ref,contract_type,principal_id,key_id,idempotency_key,payload_hash,"
                    "received_at,occurred_at,project_ref,observation_ref,result_status "
                    "FROM context_ingest_receipts ORDER BY idempotency_key"
                ).fetchall()
            ]
            expected_delivery = [
                tuple(row)
                for row in check.execute(
                    "SELECT receipt_ref,delivery_ref,principal_id,key_id,idempotency_key,payload_hash,"
                    "transition,occurred_at,bundle_hash,provider_receipt_ref,failure_reason_code,"
                    "result_state,created_at FROM context_delivery_receipts ORDER BY idempotency_key"
                ).fetchall()
            ]
        finally:
            check.close()

        fault_points = [
            f"{table}.{point}"
            for table in ("context_ingest_receipts", "context_delivery_receipts")
            for point in (
                "before_rename",
                "after_rename",
                "before_create",
                "after_create",
                "before_copy",
                "after_copy",
                "before_drop",
                "after_drop",
            )
        ]

        def assert_legacy_rollback_state(path: Path) -> None:
            conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            try:
                self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
                self.assertEqual(
                    conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0],
                    "30",
                )
                self.assertEqual(
                    self._primary_key_columns(conn, "context_ingest_receipts"),
                    ["idempotency_key"],
                )
                self.assertEqual(
                    self._primary_key_columns(conn, "context_delivery_receipts"),
                    ["idempotency_key"],
                )
                self.assertIn(
                    "source_event_ref",
                    {str(row["name"]) for row in conn.execute("PRAGMA table_info(context_ingest_receipts)")},
                )
                self.assertIsNone(
                    conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name IN ('context_ingest_receipts_v1','context_delivery_receipts_v1')"
                    ).fetchone()
                )
                self.assertIsNone(
                    conn.execute(
                        "SELECT value FROM settings WHERE key='context_candidate_table_rebuild_version'"
                    ).fetchone()
                )
                self.assertEqual(
                    [
                        tuple(row)
                        for row in conn.execute(
                            "SELECT receipt_ref,contract_type,principal_id,key_id,idempotency_key,payload_hash,"
                            "received_at,occurred_at,project_ref,observation_ref,result_status "
                            "FROM context_ingest_receipts ORDER BY idempotency_key"
                        ).fetchall()
                    ],
                    expected_ingest,
                )
                self.assertEqual(
                    [
                        tuple(row)
                        for row in conn.execute(
                            "SELECT receipt_ref,delivery_ref,principal_id,key_id,idempotency_key,payload_hash,"
                            "transition,occurred_at,bundle_hash,provider_receipt_ref,failure_reason_code,"
                            "result_state,created_at FROM context_delivery_receipts ORDER BY idempotency_key"
                        ).fetchall()
                    ],
                    expected_delivery,
                )
            finally:
                conn.close()

        for index, fault_point in enumerate(fault_points):
            with self.subTest(fault_point=fault_point):
                path = self.root / f"candidate-fault-{index:02d}.sqlite"
                self._clone_sqlite_database(fixture, path)

                def inject(point: str, *, expected: str = fault_point) -> None:
                    if point == expected:
                        raise RuntimeError(f"injected_candidate_rebuild_fault:{point}")

                with self.assertRaisesRegex(
                    RuntimeError,
                    "injected_candidate_rebuild_fault",
                ):
                    ensure_ready(path, _candidate_rebuild_fault=inject)
                assert_legacy_rollback_state(path)

                migrated = ensure_ready(path)
                try:
                    self.assertEqual(migrated.execute("PRAGMA quick_check").fetchone()[0], "ok")
                    self.assertEqual(
                        self._primary_key_columns(migrated, "context_ingest_receipts"),
                        ["receipt_ref"],
                    )
                    self.assertEqual(
                        self._primary_key_columns(migrated, "context_delivery_receipts"),
                        ["receipt_ref"],
                    )
                    self.assertIsNone(
                        migrated.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name IN ('context_ingest_receipts_v1','context_delivery_receipts_v1')"
                        ).fetchone()
                    )
                    self.assertEqual(
                        migrated.execute(
                            "SELECT value FROM settings "
                            "WHERE key='context_candidate_table_rebuild_version'"
                        ).fetchone()[0],
                        "3",
                    )
                    transformed_ingest = [
                        tuple(row)
                        for row in migrated.execute(
                            "SELECT receipt_ref,contract_type,principal_id,key_id,idempotency_key,payload_hash,"
                            "source_event_ref,received_at,occurred_at,project_ref,observation_ref,result_status "
                            "FROM context_ingest_receipts ORDER BY idempotency_key"
                        ).fetchall()
                    ]
                    self.assertEqual(
                        transformed_ingest,
                        [row[:6] + ("",) + row[6:] for row in expected_ingest],
                    )
                    self.assertEqual(
                        [
                            tuple(row)
                            for row in migrated.execute(
                                "SELECT receipt_ref,delivery_ref,principal_id,key_id,idempotency_key,payload_hash,"
                                "transition,occurred_at,bundle_hash,provider_receipt_ref,failure_reason_code,"
                                "result_state,created_at FROM context_delivery_receipts ORDER BY idempotency_key"
                            ).fetchall()
                        ],
                        expected_delivery,
                    )
                    candidate_state = {
                        "ingest_sql": migrated.execute(
                            "SELECT sql FROM sqlite_master WHERE type='table' "
                            "AND name='context_ingest_receipts'"
                        ).fetchone()[0],
                        "delivery_sql": migrated.execute(
                            "SELECT sql FROM sqlite_master WHERE type='table' "
                            "AND name='context_delivery_receipts'"
                        ).fetchone()[0],
                        "ingest_rows": transformed_ingest,
                        "delivery_rows": expected_delivery,
                    }
                finally:
                    migrated.close()

                rebuild_callbacks: list[str] = []
                for _ in range(2):
                    repeated = ensure_ready(
                        path,
                        _candidate_rebuild_fault=rebuild_callbacks.append,
                    )
                    try:
                        self.assertEqual(repeated.execute("PRAGMA quick_check").fetchone()[0], "ok")
                        self.assertEqual(
                            repeated.execute(
                                "SELECT sql FROM sqlite_master WHERE type='table' "
                                "AND name='context_ingest_receipts'"
                            ).fetchone()[0],
                            candidate_state["ingest_sql"],
                        )
                        self.assertEqual(
                            repeated.execute(
                                "SELECT sql FROM sqlite_master WHERE type='table' "
                                "AND name='context_delivery_receipts'"
                            ).fetchone()[0],
                            candidate_state["delivery_sql"],
                        )
                    finally:
                        repeated.close()
                self.assertEqual(rebuild_callbacks, [])

        ambiguous = self.root / "candidate-ambiguous-shadow.sqlite"
        self._clone_sqlite_database(fixture, ambiguous)
        conn = sqlite3.connect(ambiguous)
        try:
            conn.execute(
                "CREATE TABLE context_ingest_receipts_scope_v2 AS "
                "SELECT * FROM context_ingest_receipts"
            )
            conn.commit()
        finally:
            conn.close()
        with self.assertRaisesRegex(
            sqlite3.DatabaseError, "ambiguous_context_ingest_receipts_rebuild"
        ):
            ensure_ready(ambiguous)
        conn = sqlite3.connect(f"file:{ambiguous.resolve()}?mode=ro", uri=True)
        try:
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM context_ingest_receipts").fetchone()[0], 2)
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM context_ingest_receipts_scope_v2"
                ).fetchone()[0],
                2,
            )
        finally:
            conn.close()

        unknown_shape = self.root / "candidate-unknown-legacy-column.sqlite"
        self._clone_sqlite_database(fixture, unknown_shape)
        conn = sqlite3.connect(unknown_shape)
        try:
            conn.execute("ALTER TABLE context_ingest_receipts ADD COLUMN operator_note TEXT")
            conn.execute(
                "UPDATE context_ingest_receipts SET operator_note='must-not-be-dropped' "
                "WHERE idempotency_key='ingest_idem_fault_1'"
            )
            conn.commit()
        finally:
            conn.close()
        with self.assertRaisesRegex(
            sqlite3.DatabaseError, "context_ingest_receipt_columns_unsupported"
        ):
            ensure_ready(unknown_shape)
        conn = sqlite3.connect(f"file:{unknown_shape.resolve()}?mode=ro", uri=True)
        try:
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(
                conn.execute(
                    "SELECT operator_note FROM context_ingest_receipts "
                    "WHERE idempotency_key='ingest_idem_fault_1'"
                ).fetchone()[0],
                "must-not-be-dropped",
            )
            self.assertIsNone(
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='context_ingest_receipts_v1'"
                ).fetchone()
            )
        finally:
            conn.close()

    def test_schema29_backup_snapshot_and_upgrade_have_no_writer_gap(self) -> None:
        path = self.root / "writer-barrier.sqlite"
        self._prepare_pre_context_fixture(
            path,
            version=29,
            include_v04_schema29=True,
            identity_marker="barrier",
        )
        stamp = self.now.isoformat(timespec="seconds")
        post_ready = threading.Event()
        post_start = threading.Event()
        post_attempted = threading.Event()
        post_done = threading.Event()
        post_errors: list[BaseException] = []

        def post_writer() -> None:
            writer = connect(path, busy_timeout_seconds=10)
            post_ready.set()
            try:
                if not post_start.wait(10):
                    raise RuntimeError("post_writer_start_timeout")
                post_attempted.set()
                writer.execute("BEGIN IMMEDIATE")
                writer.execute(
                    "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?)",
                    ("writer_after_barrier", "committed", stamp),
                )
                writer.commit()
            except BaseException as exc:  # recorded and asserted by the test thread
                if writer.in_transaction:
                    writer.rollback()
                post_errors.append(exc)
            finally:
                writer.close()
                post_done.set()

        post_thread = threading.Thread(target=post_writer, name="group-f-post-writer")
        post_thread.start()
        self.assertTrue(post_ready.wait(10))

        pre_writer = connect(path, busy_timeout_seconds=10)
        pre_writer.execute("BEGIN IMMEDIATE")
        pre_writer.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?)",
            ("writer_before_barrier", "committed", stamp),
        )

        barrier_acquired = threading.Event()
        backup_complete = threading.Event()
        before_commit = threading.Event()
        release_migration = threading.Event()
        migration_errors: list[BaseException] = []
        observed_stages: list[str] = []

        def observer(stage: str) -> None:
            observed_stages.append(stage)
            if stage == "writer_barrier_acquired":
                barrier_acquired.set()
            elif stage == "backup_snapshot_complete":
                backup_complete.set()
            elif stage == "migration_before_commit":
                before_commit.set()
                if not release_migration.wait(10):
                    raise RuntimeError("migration_release_timeout")

        def migrate() -> None:
            try:
                migrated = ensure_ready(
                    path,
                    busy_timeout_seconds=10,
                    _migration_observer=observer,
                )
                migrated.close()
            except BaseException as exc:
                migration_errors.append(exc)

        migration_thread = threading.Thread(target=migrate, name="group-f-migration")
        migration_thread.start()
        try:
            time.sleep(0.15)
            self.assertFalse(barrier_acquired.is_set())
            self.assertTrue(migration_thread.is_alive())
            pre_writer.commit()
            self.assertTrue(barrier_acquired.wait(10))
            self.assertTrue(backup_complete.wait(10))
            self.assertTrue(before_commit.wait(10))

            post_start.set()
            self.assertTrue(post_attempted.wait(10))
            time.sleep(0.15)
            self.assertFalse(post_done.is_set())

            release_migration.set()
            migration_thread.join(10)
            post_thread.join(10)
        finally:
            if pre_writer.in_transaction:
                pre_writer.rollback()
            pre_writer.close()
            post_start.set()
            release_migration.set()
            migration_thread.join(10)
            post_thread.join(10)

        self.assertFalse(migration_thread.is_alive())
        self.assertFalse(post_thread.is_alive())
        self.assertEqual(migration_errors, [])
        self.assertEqual(post_errors, [])
        self.assertEqual(
            observed_stages,
            [
                "writer_barrier_acquired",
                "backup_snapshot_complete",
                "migration_before_commit",
                "migration_committed",
            ],
        )

        upgraded = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        try:
            self.assertEqual(upgraded.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(upgraded.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "32")
            self.assertEqual(
                upgraded.execute(
                    "SELECT key,value FROM settings WHERE key IN "
                    "('writer_before_barrier','writer_after_barrier') ORDER BY key"
                ).fetchall(),
                [
                    ("writer_after_barrier", "committed"),
                    ("writer_before_barrier", "committed"),
                ],
            )
        finally:
            upgraded.close()

        backup = path.with_name(f"{path.name}.schema29-context-backup")
        identity_path = backup.with_name(f"{backup.name}.identity.json")
        backup_conn = sqlite3.connect(f"file:{backup.resolve()}?mode=ro&immutable=1", uri=True)
        try:
            self.assertEqual(backup_conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(backup_conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "29")
            self.assertEqual(
                backup_conn.execute(
                    "SELECT value FROM settings WHERE key='writer_before_barrier'"
                ).fetchone()[0],
                "committed",
            )
            self.assertIsNone(
                backup_conn.execute(
                    "SELECT value FROM settings WHERE key='writer_after_barrier'"
                ).fetchone()
            )
            self.assertEqual(
                backup_conn.execute("SELECT episode_id FROM task_episodes").fetchone()[0],
                "episode_barrier",
            )
        finally:
            backup_conn.close()
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        self.assertEqual(identity["database_identity"], storage_module._database_file_identity(path))
        self.assertEqual(identity["source_content_sha256"], storage_module._sha256_file(backup))
        self.assertEqual(identity["backup_content_sha256"], storage_module._sha256_file(backup))

        fault_path = self.root / "writer-barrier-fault.sqlite"
        self._prepare_pre_context_fixture(
            fault_path,
            version=29,
            include_v04_schema29=True,
            identity_marker="fault",
        )
        writer = connect(fault_path)
        writer.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?)",
            ("writer_before_fault_barrier", "committed", stamp),
        )
        writer.commit()
        writer.close()

        def fail_before_commit(stage: str) -> None:
            if stage == "migration_before_commit":
                raise RuntimeError("injected_migration_fault")

        with self.assertRaisesRegex(RuntimeError, "injected_migration_fault"):
            ensure_ready(fault_path, _migration_observer=fail_before_commit)
        rolled_back = sqlite3.connect(f"file:{fault_path.resolve()}?mode=ro", uri=True)
        try:
            self.assertEqual(rolled_back.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(rolled_back.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "29")
            self.assertIsNone(
                rolled_back.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_verified'"
                ).fetchone()
            )
            self.assertEqual(
                rolled_back.execute(
                    "SELECT value FROM settings WHERE key='writer_before_fault_barrier'"
                ).fetchone()[0],
                "committed",
            )
        finally:
            rolled_back.close()
        fault_backup = fault_path.with_name(f"{fault_path.name}.schema29-context-backup")
        fault_backup_conn = sqlite3.connect(
            f"file:{fault_backup.resolve()}?mode=ro&immutable=1",
            uri=True,
        )
        try:
            self.assertEqual(fault_backup_conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(
                fault_backup_conn.execute(
                    "SELECT value FROM settings WHERE key='writer_before_fault_barrier'"
                ).fetchone()[0],
                "committed",
            )
        finally:
            fault_backup_conn.close()
        retried = ensure_ready(fault_path)
        try:
            self.assertEqual(retried.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "32")
            self.assertEqual(retried.execute("PRAGMA quick_check").fetchone()[0], "ok")
        finally:
            retried.close()

    def test_schema_28_evidence_confirmed_screen_rows_are_downgraded(self) -> None:
        path = self.root / "schema28-screen-confirmation.sqlite"
        self._prepare_pre_context_fixture(path, version=28, identity_marker="screen28")
        conn = connect(path)
        stamp = self.now.isoformat(timespec="seconds")
        self._insert_confirmation_fixture(
            conn,
            item_key="screen_fake_28",
            state="evidence_confirmed",
            answer=None,
            stamp=stamp,
        )
        conn.execute(
            "INSERT INTO activity_confirmation_settlements(report_date,revision,settled_at,"
            "completeness_status,pending_total,pending_high_priority,pending_by_kind,candidate_funnel,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("2026-07-29", "stale-revision", stamp, "complete", 0, 0, "{}", "{}", stamp, stamp),
        )
        conn.execute(
            "INSERT INTO distribution_packages(id,target,date_range,format,cache_ref,created_at) "
            "VALUES(?,?,?,?,?,?)",
            ("package_screen_28", "local", "2026-07-29", "json", "cache-screen-28", stamp),
        )
        conn.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?)",
            ("activity_confirmation_sampled_out:2026-07-29", "7", stamp),
        )
        conn.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?)",
            ("activity_confirmation_episode_diagnostics:2026-07-29", '{"eligible":7}', stamp),
        )
        conn.commit()
        conn.close()

        migrated = ensure_ready(path)
        try:
            row = migrated.execute(
                "SELECT state,answer,notification_eligible FROM activity_confirmation_items "
                "WHERE item_key='screen_fake_28'"
            ).fetchone()
            self.assertEqual(tuple(row), ("pending", None, 0))
            self.assertEqual(migrated.execute("SELECT COUNT(*) FROM activity_confirmation_settlements").fetchone()[0], 0)
            invalidated_at = migrated.execute(
                "SELECT cache_invalidated_at FROM distribution_packages WHERE id='package_screen_28'"
            ).fetchone()[0]
            self.assertTrue(invalidated_at)
            self.assertEqual(
                migrated.execute(
                    "SELECT COUNT(*) FROM settings WHERE key LIKE 'activity_confirmation_sampled_out:%' "
                    "OR key LIKE 'activity_confirmation_episode_diagnostics:%'"
                ).fetchone()[0],
                0,
            )
        finally:
            migrated.close()
        for _ in range(2):
            repeated = ensure_ready(path)
            try:
                self.assertEqual(
                    tuple(
                        repeated.execute(
                            "SELECT state,answer,notification_eligible FROM activity_confirmation_items "
                            "WHERE item_key='screen_fake_28'"
                        ).fetchone()
                    ),
                    ("pending", None, 0),
                )
                self.assertEqual(
                    repeated.execute(
                        "SELECT cache_invalidated_at FROM distribution_packages "
                        "WHERE id='package_screen_28'"
                    ).fetchone()[0],
                    invalidated_at,
                )
            finally:
                repeated.close()

    def test_schema_30_confirmed_without_answer_is_invalidated(self) -> None:
        path = self.root / "schema30-null-confirmation.sqlite"
        conn = ensure_ready(path)
        conn.execute("DROP TRIGGER IF EXISTS pam_schema32_guard_insert")
        conn.execute("DROP TRIGGER IF EXISTS pam_schema32_guard_update")
        conn.execute("DROP TRIGGER IF EXISTS pam_schema31_guard_insert")
        conn.execute("DROP TRIGGER IF EXISTS pam_schema31_guard_update")
        conn.execute("UPDATE meta SET value='30' WHERE key='schema_version'")
        stamp = self.now.isoformat(timespec="seconds")
        for origin in ("explicit_claim", "manual"):
            self._insert_confirmation_fixture(
                conn,
                item_key=f"null_confirmed_{origin}",
                state="confirmed",
                answer=None,
                candidate_origin=origin,
                answered_at=stamp,
                stamp=stamp,
            )
        conn.execute(
            "INSERT INTO activity_confirmation_settlements(report_date,revision,settled_at,"
            "completeness_status,pending_total,pending_high_priority,pending_by_kind,candidate_funnel,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("2026-07-30", "stale-null-revision", stamp, "complete", 0, 0, "{}", "{}", stamp, stamp),
        )
        conn.commit()
        conn.close()

        migrated = ensure_ready(path)
        try:
            rows = migrated.execute(
                "SELECT item_key,state,answer,notification_eligible,answered_at "
                "FROM activity_confirmation_items WHERE item_key LIKE 'null_confirmed_%' "
                "ORDER BY item_key"
            ).fetchall()
            self.assertEqual(len(rows), 2)
            for row in rows:
                self.assertEqual(
                    (row["state"], row["answer"], row["notification_eligible"], row["answered_at"]),
                    ("invalidated", None, 0, None),
                )
            self.assertEqual(migrated.execute("SELECT COUNT(*) FROM activity_confirmation_settlements").fetchone()[0], 0)
        finally:
            migrated.close()

        repeated = ensure_ready(path)
        try:
            self.assertEqual(
                repeated.execute(
                    "SELECT COUNT(*) FROM activity_confirmation_items "
                    "WHERE state='invalidated' AND answer IS NULL"
                ).fetchone()[0],
                2,
            )
        finally:
            repeated.close()

    def test_explicit_user_answered_confirmation_survives_migration(self) -> None:
        path = self.root / "explicit-answer-survives.sqlite"
        self._prepare_pre_context_fixture(path, version=28, identity_marker="explicit-answer")
        conn = connect(path)
        stamp = self.now.isoformat(timespec="seconds")
        self._insert_confirmation_fixture(
            conn,
            item_key="explicit_occurred_28",
            state="evidence_confirmed",
            answer="occurred",
            answer_reason="correct",
            user_note="owner supplied note",
            user_workstream="owner workstream",
            user_outcome="completed",
            generalize_scope="exact",
            answered_at=stamp,
            stamp=stamp,
        )
        self._insert_confirmation_fixture(
            conn,
            item_key="manual_explicit_28",
            state="confirmed",
            answer="occurred",
            answer_reason="correct",
            candidate_origin="manual",
            user_workstream="manual workstream",
            user_outcome="progressing",
            answered_at=stamp,
            stamp=stamp,
        )
        before = {
            row["item_key"]: dict(row)
            for row in conn.execute(
                "SELECT item_key,answer,answer_reason,user_note,user_workstream,user_outcome,"
                "generalize_scope,answered_at,candidate_origin FROM activity_confirmation_items "
                "WHERE item_key IN ('explicit_occurred_28','manual_explicit_28')"
            ).fetchall()
        }
        conn.commit()
        conn.close()

        migrated = ensure_ready(path)
        try:
            after = {
                row["item_key"]: dict(row)
                for row in migrated.execute(
                    "SELECT item_key,answer,answer_reason,user_note,user_workstream,user_outcome,"
                    "generalize_scope,answered_at,candidate_origin FROM activity_confirmation_items "
                    "WHERE item_key IN ('explicit_occurred_28','manual_explicit_28')"
                ).fetchall()
            }
            self.assertEqual(after, before)
            self.assertEqual(
                dict(
                    migrated.execute(
                        "SELECT item_key,state FROM activity_confirmation_items "
                        "WHERE item_key IN ('explicit_occurred_28','manual_explicit_28')"
                    ).fetchall()
                ),
                {
                    "explicit_occurred_28": "confirmed",
                    "manual_explicit_28": "confirmed",
                },
            )
        finally:
            migrated.close()
        for _ in range(2):
            repeated = ensure_ready(path)
            try:
                self.assertEqual(
                    {
                        row["item_key"]: dict(row)
                        for row in repeated.execute(
                            "SELECT item_key,answer,answer_reason,user_note,user_workstream,user_outcome,"
                            "generalize_scope,answered_at,candidate_origin FROM activity_confirmation_items "
                            "WHERE item_key IN ('explicit_occurred_28','manual_explicit_28')"
                        ).fetchall()
                    },
                    before,
                )
            finally:
                repeated.close()

    def test_schema_28_29_30_31_and_repeated_start_force_ha_auto_package_off(self) -> None:
        keys = (
            "ha_auto_package_enabled",
            "context_memory_writer_enabled",
            "context_source_revocation_cascade_ready",
        )
        stamp = self.now.isoformat(timespec="seconds")
        for version in (28, 29, 30, 31):
            with self.subTest(version=version):
                path = self.root / f"flags-schema-{version}.sqlite"
                if version in {30, 31}:
                    prepared = ensure_ready(path)
                    prepared.execute("DROP TRIGGER IF EXISTS pam_schema32_guard_insert")
                    prepared.execute("DROP TRIGGER IF EXISTS pam_schema32_guard_update")
                    if version == 30:
                        prepared.execute("DROP TRIGGER IF EXISTS pam_schema31_guard_insert")
                        prepared.execute("DROP TRIGGER IF EXISTS pam_schema31_guard_update")
                        prepared.execute("UPDATE meta SET value='30' WHERE key='schema_version'")
                    else:
                        prepared.execute("UPDATE meta SET value='31' WHERE key='schema_version'")
                    prepared.execute(
                        "UPDATE settings SET value='true' WHERE key IN (?,?,?)",
                        keys,
                    )
                    prepared.commit()
                    prepared.close()
                else:
                    self._prepare_pre_context_fixture(
                        path,
                        version=version,
                        include_v04_schema29=version == 29,
                        identity_marker=f"flags-{version}",
                    )
                for startup in range(2):
                    reopened = ensure_ready(path)
                    try:
                        values = dict(
                            reopened.execute(
                                "SELECT key,value FROM settings WHERE key IN (?,?,?)",
                                keys,
                            ).fetchall()
                        )
                        self.assertEqual(values, {key: "false" for key in keys})
                        self.assertEqual(
                            reopened.execute(
                                "SELECT value FROM meta WHERE key='schema_version'"
                            ).fetchone()[0],
                            "32",
                        )
                    finally:
                        reopened.close()
                    if startup == 0:
                        toggle = connect(path)
                        toggle.execute(
                            "UPDATE settings SET value='true',updated_at=? WHERE key IN (?,?,?)",
                            (stamp, *keys),
                        )
                        toggle.commit()
                        toggle.close()

    def test_real_v04_schema29_fixture_migrates_additively_without_reinterpretation(self) -> None:
        path = self.root / "real-v04-schema29.sqlite"
        self._prepare_pre_context_fixture(
            path,
            version=29,
            include_v04_schema29=True,
            identity_marker="real-v04",
        )
        before_conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        before_conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in V04_SCHEMA29_OBJECTS)
        try:
            before_schema = {
                str(row["name"]): str(row["sql"])
                for row in before_conn.execute(
                    f"SELECT name,sql FROM sqlite_master WHERE name IN ({placeholders}) ORDER BY name",
                    V04_SCHEMA29_OBJECTS,
                ).fetchall()
            }
            before_rows = {
                table: [tuple(row) for row in before_conn.execute(f'SELECT * FROM "{table}"').fetchall()]
                for table in (
                    "task_episodes",
                    "task_evidence_events",
                    "hermes_outbox",
                    "hermes_delivery_receipts",
                )
            }
        finally:
            before_conn.close()
        self.assertEqual(set(before_schema), set(V04_SCHEMA29_OBJECTS))

        migrated = ensure_ready(path)
        try:
            after_schema = {
                str(row["name"]): str(row["sql"])
                for row in migrated.execute(
                    f"SELECT name,sql FROM sqlite_master WHERE name IN ({placeholders}) ORDER BY name",
                    V04_SCHEMA29_OBJECTS,
                ).fetchall()
            }
            after_rows = {
                table: [tuple(row) for row in migrated.execute(f'SELECT * FROM "{table}"').fetchall()]
                for table in (
                    "task_episodes",
                    "task_evidence_events",
                    "hermes_outbox",
                    "hermes_delivery_receipts",
                )
            }
            self.assertEqual(after_schema, before_schema)
            self.assertEqual(after_rows, before_rows)
            self.assertEqual(migrated.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "32")
            self.assertIsNotNone(
                migrated.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_verified'"
                ).fetchone()
            )
            for table in (
                "context_observations",
                "context_evidence",
                "context_candidates",
                "context_verified",
                "context_ingest_receipts",
                "context_delivery_receipts",
            ):
                self.assertEqual(migrated.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0], 0)
        finally:
            migrated.close()

        backup = path.with_name(f"{path.name}.schema29-context-backup")
        backup_conn = sqlite3.connect(f"file:{backup.resolve()}?mode=ro&immutable=1", uri=True)
        backup_conn.row_factory = sqlite3.Row
        try:
            backup_schema = {
                str(row["name"]): str(row["sql"])
                for row in backup_conn.execute(
                    f"SELECT name,sql FROM sqlite_master WHERE name IN ({placeholders}) ORDER BY name",
                    V04_SCHEMA29_OBJECTS,
                ).fetchall()
            }
            backup_rows = {
                table: [tuple(row) for row in backup_conn.execute(f'SELECT * FROM "{table}"').fetchall()]
                for table in (
                    "task_episodes",
                    "task_evidence_events",
                    "hermes_outbox",
                    "hermes_delivery_receipts",
                )
            }
            self.assertEqual(backup_conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(backup_schema, before_schema)
            self.assertEqual(backup_rows, before_rows)
        finally:
            backup_conn.close()

        repeated = ensure_ready(path)
        try:
            self.assertEqual(
                {
                    str(row["name"]): str(row["sql"])
                    for row in repeated.execute(
                        f"SELECT name,sql FROM sqlite_master WHERE name IN ({placeholders}) ORDER BY name",
                        V04_SCHEMA29_OBJECTS,
                    ).fetchall()
                },
                before_schema,
            )
        finally:
            repeated.close()

    def test_stale_same_path_backup_from_different_database_identity_is_not_reused(self) -> None:
        path = self.root / "same-path.sqlite"
        self._prepare_pre_context_fixture(path, version=28, identity_marker="database-A")
        source = storage_module.connect_readonly(path)
        try:
            storage_module._backup_context_schema_before_upgrade(source, path)
        finally:
            source.close()
        backup = path.with_name(f"{path.name}.schema28-context-backup")
        identity = backup.with_name(f"{backup.name}.identity.json")
        backup_before = backup.read_bytes()
        identity_before = identity.read_bytes()

        replacement = self.root / "replacement.sqlite"
        self._prepare_pre_context_fixture(
            replacement,
            version=28,
            identity_marker="database-B",
        )
        for base in (path, replacement):
            Path(f"{base}-wal").unlink(missing_ok=True)
            Path(f"{base}-shm").unlink(missing_ok=True)
        replacement.replace(path)

        with self.assertRaisesRegex(RuntimeError, "schema_28_context_backup_stale"):
            ensure_ready(path)
        self.assertEqual(backup.read_bytes(), backup_before)
        self.assertEqual(identity.read_bytes(), identity_before)
        current = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        try:
            self.assertEqual(current.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(current.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "28")
            self.assertEqual(
                current.execute(
                    "SELECT value FROM settings WHERE key='database_identity_marker'"
                ).fetchone()[0],
                "database-B",
            )
        finally:
            current.close()

        same_identity_path = self.root / "same-identity-content-change.sqlite"
        self._prepare_pre_context_fixture(
            same_identity_path,
            version=28,
            identity_marker="same-identity-before",
        )
        same_identity_source = storage_module.connect_readonly(same_identity_path)
        try:
            storage_module._backup_context_schema_before_upgrade(
                same_identity_source,
                same_identity_path,
            )
        finally:
            same_identity_source.close()
        same_identity_backup = same_identity_path.with_name(
            f"{same_identity_path.name}.schema28-context-backup"
        )
        same_identity_metadata = same_identity_backup.with_name(
            f"{same_identity_backup.name}.identity.json"
        )
        same_identity_backup_before = same_identity_backup.read_bytes()
        same_identity_metadata_before = same_identity_metadata.read_bytes()
        same_identity_inode_before = same_identity_path.stat().st_ino
        changed = connect(same_identity_path)
        try:
            changed.execute(
                "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?)",
                ("content_changed_after_backup", "committed", self.now.isoformat(timespec="seconds")),
            )
            changed.commit()
        finally:
            changed.close()
        self.assertEqual(same_identity_path.stat().st_ino, same_identity_inode_before)
        with self.assertRaisesRegex(RuntimeError, "schema_28_context_backup_stale"):
            ensure_ready(same_identity_path)
        self.assertEqual(same_identity_backup.read_bytes(), same_identity_backup_before)
        self.assertEqual(same_identity_metadata.read_bytes(), same_identity_metadata_before)
        same_identity_current = sqlite3.connect(
            f"file:{same_identity_path.resolve()}?mode=ro",
            uri=True,
        )
        try:
            self.assertEqual(same_identity_current.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(
                same_identity_current.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()[0],
                "28",
            )
            self.assertEqual(
                same_identity_current.execute(
                    "SELECT value FROM settings WHERE key='content_changed_after_backup'"
                ).fetchone()[0],
                "committed",
            )
        finally:
            same_identity_current.close()

        orphan_path = self.root / "orphan-sidecar.sqlite"
        self._prepare_pre_context_fixture(
            orphan_path,
            version=28,
            identity_marker="orphan-sidecar-source",
        )
        orphan_backup = orphan_path.with_name(f"{orphan_path.name}.schema28-context-backup")
        orphan_identity = orphan_backup.with_name(f"{orphan_backup.name}.identity.json")
        orphan_payload = b"orphan-sidecar-must-not-be-overwritten\n"
        orphan_identity.write_bytes(orphan_payload)
        orphan_identity.chmod(0o600)
        with self.assertRaisesRegex(RuntimeError, "schema_28_context_backup_identity_orphaned"):
            ensure_ready(orphan_path)
        self.assertFalse(orphan_backup.exists())
        self.assertEqual(orphan_identity.read_bytes(), orphan_payload)
        orphan_current = sqlite3.connect(f"file:{orphan_path.resolve()}?mode=ro", uri=True)
        try:
            self.assertEqual(orphan_current.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(
                orphan_current.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()[0],
                "28",
            )
        finally:
            orphan_current.close()

    def test_context_contract_docs_cover_trust_degradation_migration_and_rollback(self) -> None:
        repo = Path(__file__).resolve().parents[2]
        context_doc = (repo / "docs" / "CONTEXT_CONSOLE_V1.md").read_text(encoding="utf-8")
        release_doc = (repo / "docs" / "RELEASE_CHECKLIST.md").read_text(encoding="utf-8")
        combined = context_doc + "\n" + release_doc
        required_terms = (
            "ContextReceiptV1",
            "ControlledEvidenceV1",
            "ContextBundleV1",
            "DeliveryReceiptV1",
            "closed JSON",
            "trust degradation",
            "privacy target policy",
            "hash projection",
            "delivery state",
            "busy/null",
            "retention/cascade",
            "schema 29 migration",
            "rollback",
            "ha_auto_package_enabled=false",
            "context_memory_writer_enabled=false",
            "context_source_revocation_cascade_ready=false",
            "source evidence",
            "build evidence",
            "installed evidence",
            "live evidence",
            "Hermes/飞书",
            "synthetic SQLite fixture",
            "does not prove a real PAM database migration",
        )
        for term in required_terms:
            with self.subTest(term=term):
                self.assertIn(term, combined)

    def test_concurrent_ensure_ready_with_external_binding_backfill_waits_and_reloads(self) -> None:
        payload = self._controlled_payload(
            principal_name="external",
            evidence_kind="external_receipt",
            state="completed_confirmed",
            idempotency_key="idem_afe00001",
            source_ref="source_afe00001",
            provider_receipt_ref="provider_afe00001",
            memory_eligible=False,
            verification_scope="operational",
        )
        accepted = ingest_controlled_evidence(
            self.conn,
            self._wire(payload),
            verification_secret=self.secrets["external"],
            now=self.now,
        )
        self.assertEqual(accepted["status"], "accepted")
        self.conn.execute("DROP TABLE context_external_receipt_bindings")
        self.conn.execute(
            "CREATE TABLE context_external_receipt_bindings ("
            "principal_id TEXT NOT NULL,provider_receipt_ref TEXT NOT NULL,"
            "semantic_hash TEXT NOT NULL,source_ref TEXT NOT NULL,project_ref TEXT NOT NULL,"
            "workstream_ref TEXT NOT NULL,object_ref TEXT NOT NULL,version_ref TEXT NOT NULL,"
            "subject_ref TEXT NOT NULL,privacy_class TEXT NOT NULL,state TEXT NOT NULL,"
            "created_at TEXT NOT NULL,PRIMARY KEY(provider_receipt_ref,principal_id))"
        )
        self.conn.commit()
        self.conn.close()

        rebuild_paused = threading.Event()
        release_rebuild = threading.Event()
        second_started = threading.Event()
        second_finished = threading.Event()
        results: dict[str, object] = {}

        def hook(point: str) -> None:
            if point == "context_external_receipt_bindings.after_rename":
                rebuild_paused.set()
                if not release_rebuild.wait(5):
                    raise RuntimeError("test_rebuild_release_timeout")

        def first_caller() -> None:
            try:
                conn = ensure_ready(self.db, _candidate_rebuild_fault=hook)
                results["first_quick_check"] = conn.execute("PRAGMA quick_check").fetchone()[0]
                conn.close()
            except BaseException as exc:
                results["first_error"] = repr(exc)

        def second_caller() -> None:
            second_started.set()
            try:
                conn = ensure_ready(self.db)
                results["binding"] = tuple(
                    conn.execute(
                        "SELECT source_ref,state FROM context_external_receipt_bindings "
                        "WHERE provider_receipt_ref='provider_afe00001'"
                    ).fetchone()
                )
                results["binding_exact"] = (
                    context_console_module._external_binding_table_contract_is_exact(conn)
                )
                results["ingest_scopes"] = {
                    tuple(row[2] for row in conn.execute(f"PRAGMA index_xinfo('{index[1]}')")
                          if int(row[5]) == 1)
                    for index in conn.execute("PRAGMA index_list(context_ingest_receipts)")
                    if int(index[2]) == 1
                }
                results["shadow_count"] = conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND "
                    "name IN ('context_ingest_receipts_scope_v2','context_external_receipt_bindings_scope_v2',"
                    "'context_delivery_receipts_v1')"
                ).fetchone()[0]
                conn.close()
            except BaseException as exc:
                results["second_error"] = repr(exc)
            finally:
                second_finished.set()

        first = threading.Thread(target=first_caller, daemon=True)
        first.start()
        self.assertTrue(rebuild_paused.wait(2), "first ensure_ready must reach binding rebuild")
        second = threading.Thread(target=second_caller, daemon=True)
        second.start()
        self.assertTrue(second_started.wait(1))
        time.sleep(0.15)
        self.assertFalse(second_finished.is_set(), "second ensure_ready must wait for the first caller")
        release_rebuild.set()
        first.join(5)
        second.join(5)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertNotIn("first_error", results)
        self.assertNotIn("second_error", results)
        self.assertEqual(results["first_quick_check"], "ok")
        self.assertEqual(results["binding"], ("source_afe00001", "completed_confirmed"))
        self.assertTrue(results["binding_exact"])
        expected = ("contract_type", "principal_id", "source_ref", "idempotency_key")
        legacy = ("contract_type", "principal_id", "idempotency_key")
        self.assertIn(expected, results["ingest_scopes"])
        self.assertNotIn(legacy, results["ingest_scopes"])
        self.assertEqual(results["shadow_count"], 0)

        self.conn = ensure_ready(self.db)
        probe = sqlite3.connect(self.db, timeout=1, isolation_level=None)
        try:
            probe.execute("BEGIN IMMEDIATE")
            probe.rollback()
        finally:
            probe.close()

    def test_delivery_current_pk_base_shape_upgrades_to_bound_shape_and_repeats(self) -> None:
        self.conn.close()
        raw = sqlite3.connect(self.db)
        try:
            raw.execute("PRAGMA foreign_keys=OFF")
            raw.execute("DROP TABLE context_delivery_receipts")
            raw.execute(
                "CREATE TABLE context_delivery_receipts("
                "receipt_ref TEXT PRIMARY KEY,delivery_ref TEXT NOT NULL,"
                "principal_id TEXT NOT NULL,key_id TEXT NOT NULL,"
                "idempotency_key TEXT NOT NULL,payload_hash TEXT NOT NULL,"
                "transition TEXT NOT NULL,occurred_at TEXT NOT NULL,bundle_hash TEXT NOT NULL,"
                "provider_receipt_ref TEXT NOT NULL DEFAULT '',"
                "failure_reason_code TEXT NOT NULL DEFAULT '',result_state TEXT NOT NULL,"
                "created_at TEXT NOT NULL,FOREIGN KEY(delivery_ref) REFERENCES context_deliveries(delivery_ref),"
                "UNIQUE(delivery_ref,principal_id,idempotency_key))"
            )
            raw.commit()
        finally:
            raw.close()

        expected_columns = {
            "receipt_ref", "delivery_ref", "principal_id", "key_id",
            "idempotency_key", "payload_hash", "transition", "occurred_at",
            "bundle_hash", "provider_receipt_ref", "failure_reason_code",
            "result_state", "created_at", "bundle_ref", "attempt_no",
            "project_ref", "target_ref", "target_scope", "export_policy",
            "privacy_policy_hash", "principal_role",
        }
        first = ensure_ready(self.db)
        try:
            self.assertEqual(
                {str(row["name"]) for row in first.execute(
                    "PRAGMA table_xinfo(context_delivery_receipts)"
                )},
                expected_columns,
            )
            self.assertEqual(
                self._primary_key_columns(first, "context_delivery_receipts"),
                ["receipt_ref"],
            )
        finally:
            first.close()
        second = ensure_ready(self.db)
        try:
            self.assertEqual(
                {str(row["name"]) for row in second.execute(
                    "PRAGMA table_xinfo(context_delivery_receipts)"
                )},
                expected_columns,
            )
            self.assertEqual(
                second.execute(
                    "SELECT value FROM settings WHERE key='context_candidate_table_rebuild_version'"
                ).fetchone()[0],
                "3",
            )
        finally:
            second.close()
        self.conn = ensure_ready(self.db)

    def test_phase2_schema31_and_nonavailable_contracts_are_fail_closed(self) -> None:
        self.assertEqual(
            self.conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()[0],
            "32",
        )
        tables = {
            str(row[0])
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        self.assertIn("context_activity_confirmation_links", tables)
        self.assertIn("context_product_feedback", tables)
        feedback_columns = {
            str(row["name"])
            for row in self.conn.execute(
                "PRAGMA table_info(context_product_feedback)"
            ).fetchall()
        }
        self.assertFalse(
            feedback_columns
            & {"body", "text", "summary", "prompt", "raw_json", "chat_content"}
        )

        disabled = configure_context_console(self.conn, enabled=False)
        self.assertEqual(disabled["status"], "disabled")
        snapshot = context_snapshot(self.conn, self._wire({}), now=self.now)
        self.assertEqual(snapshot["status"], "disabled")
        self.assertFalse(snapshot["actions_enabled"])
        for key in (
            "counts",
            "context_bundle_v1",
            "projects",
            "legacy_confirmed",
            "confirmation_questions",
            "capabilities",
        ):
            self.assertIsNone(snapshot[key])
        memory = list_memory_candidates(self.conn, self._wire({}), now=self.now)
        self.assertEqual(memory["status"], "disabled")
        self.assertIsNone(memory["candidates"])
        self.assertIsNone(memory["actions"])
        delivery = delivery_status(self.conn, self._wire({}), now=self.now)
        self.assertEqual(delivery["status"], "disabled")
        self.assertIsNone(delivery["daily_report"])
        self.assertIsNone(delivery["deliveries"])
        handoff = build_local_handoff(
            self.conn,
            self._wire({"project_ref": self.project}),
            now=self.now,
        )
        self.assertEqual(handoff["status"], "disabled")
        for key in (
            "handoff_ref",
            "bundle_hash",
            "project",
            "verified_contexts",
            "unresolved_questions",
            "expires_at",
            "revocation_state",
        ):
            self.assertIsNone(handoff[key])

        unknown = phase2_unknown_contract(
            CONTEXT_SNAPSHOT_VERSION,
            "database_busy",
            now=self.now,
        )
        self.assertEqual(unknown["status"], "busy")
        self.assertNotIn("reason_code", unknown)
        for public_projection in (snapshot, memory, delivery, handoff, unknown):
            self.assertNotIn(
                '"reason_code"',
                json.dumps(public_projection, ensure_ascii=False),
            )
        self.assertFalse(unknown["actions_enabled"])
        self.assertTrue(
            all(
                unknown[key] is None
                for key in (
                    "counts",
                    "context_bundle_v1",
                    "projects",
                    "legacy_confirmed",
                    "confirmation_questions",
                    "capabilities",
                )
            )
        )
        enabled = configure_context_console(
            self.conn,
            enabled=True,
            confirmation=CONTEXT_FEATURE_CONFIRMATION,
        )
        self.assertEqual(enabled["status"], "enabled")
        available = context_snapshot(self.conn, self._wire({}), now=self.now)
        self.assertEqual(available["status"], "available")
        self.assertNotIn(
            '"reason_code"',
            json.dumps(available, ensure_ascii=False),
        )
        self.assertTrue(available["actions_enabled"])
        self.assertTrue(available["capabilities"]["owner_action"])
        self.assertEqual(
            dict(
                self.conn.execute(
                    "SELECT key,value FROM settings WHERE key IN "
                    "('ha_auto_package_enabled','context_memory_writer_enabled',"
                    "'context_source_revocation_cascade_ready')"
                ).fetchall()
            ),
            {
                "context_memory_writer_enabled": "false",
                "context_source_revocation_cascade_ready": "false",
                "ha_auto_package_enabled": "false",
            },
        )

    def test_phase2_owner_actions_dual_write_atomically_and_negative_answers_never_verify(self) -> None:
        positive_payload = self._phase2_manual_action_payload(
            idempotency_key="idem_f2000001",
        )
        before = self._table_counts()
        recorded = owner_context_action(
            self.conn,
            self._wire(positive_payload),
            now=self.now,
        )
        self.assertEqual((recorded["status"], recorded["verification_state"]), ("recorded", "verified"))
        self.assertRegex(str(recorded["action_ref"]), r"^action_[0-9a-f]{32}$")
        self.assertRegex(str(recorded["confirmation_ref"]), r"^confirmation_[0-9a-f]{32}$")
        self.assertRegex(str(recorded["context_ref"]), r"^context_[0-9a-f]{32}$")
        self.assertFalse(recorded["direct_write"])
        self.assertFalse(recorded["writer_enabled"])
        after = self._table_counts()
        for table in (
            "context_ingest_receipts",
            "context_observations",
            "context_evidence",
            "context_candidates",
            "context_verified",
        ):
            self.assertEqual(after[table], before[table] + 1)
        link = self.conn.execute(
            "SELECT * FROM context_activity_confirmation_links WHERE link_ref=?",
            (recorded["action_ref"],),
        ).fetchone()
        self.assertEqual((link["answer"], link["status"]), ("occurred", "verified"))
        self.assertTrue(link["observation_ref"])
        self.assertEqual(
            tuple(self.conn.execute(
                "SELECT state,answer FROM activity_confirmation_items WHERE item_key=?",
                (link["confirmation_key"],),
            ).fetchone()),
            ("confirmed", "occurred"),
        )
        snapshot_after_manual_note = context_snapshot(
            self.conn,
            self._wire({}),
            now=self.now,
        )
        self.assertEqual(snapshot_after_manual_note["legacy_confirmed"], [])
        matching_projects = [
            project
            for project in snapshot_after_manual_note["projects"]
            if any(
                context["context_ref"] == recorded["context_ref"]
                for context in project["verified_contexts"]
            )
        ]
        self.assertEqual(len(matching_projects), 1)
        self.assertEqual(matching_projects[0]["compatibility_contexts"], [])

        replay = owner_context_action(
            self.conn,
            self._wire(positive_payload),
            now=self.now,
        )
        self.assertEqual(replay["status"], "replayed")
        self.assertNotIn("reason_code", replay)
        conflict = owner_context_action(
            self.conn,
            self._wire({**positive_payload, "safe_label": "different text"}),
            now=self.now,
        )
        self.assertEqual(conflict["status"], "conflict")
        self.assertNotIn("reason_code", conflict)
        self.assertEqual(self._table_counts(), after)

        before_fault_items = self.conn.execute(
            "SELECT COUNT(*) FROM activity_confirmation_items"
        ).fetchone()[0]
        self.conn.execute(
            "CREATE TRIGGER phase2_owner_action_fault BEFORE INSERT "
            "ON context_activity_confirmation_links BEGIN "
            "SELECT RAISE(ABORT,'phase2 synthetic fault'); END"
        )
        self.conn.commit()
        faulted = owner_context_action(
            self.conn,
            self._wire(
                self._phase2_manual_action_payload(
                    idempotency_key="idem_f2000002",
                    safe_label="must roll back",
                )
            ),
            now=self.now,
        )
        self.assertNotEqual(faulted["status"], "recorded")
        self.assertEqual(self._table_counts(), after)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM activity_confirmation_items"
            ).fetchone()[0],
            before_fault_items,
        )
        self.conn.execute("DROP TRIGGER phase2_owner_action_fault")
        self.conn.commit()

        action_now = datetime.now(UTC).replace(microsecond=0)
        verified_before_negative = self.conn.execute(
            "SELECT COUNT(*) FROM context_verified"
        ).fetchone()[0]
        for index, answer in enumerate(
            ("ignored", "defer", "not_occurred", "misrecognized"),
            start=1,
        ):
            item_key = f"phase2_negative_{index}"
            self._insert_phase2_confirmation_card(
                item_key,
                object_label=f"Negative answer {index}",
                at=action_now.astimezone(),
            )
            snapshot = context_snapshot(self.conn, self._wire({}), now=action_now)
            question = next(
                question
                for question in snapshot["confirmation_questions"]
                if f"Negative answer {index}" in question["title"]
            )
            result = owner_context_action(
                self.conn,
                self._wire(
                    self._phase2_confirmation_action_payload(
                        question,
                        idempotency_key=f"idem_f2a{index:05d}",
                        answer=answer,
                    )
                ),
                now=action_now,
            )
            self.assertEqual((result["status"], result["verification_state"]), ("recorded", "non_verifying"))
            stored_link = self.conn.execute(
                "SELECT answer,status,verified_ref,public_context_ref "
                "FROM context_activity_confirmation_links WHERE link_ref=?",
                (result["action_ref"],),
            ).fetchone()
            self.assertEqual(
                tuple(stored_link),
                (answer, "non_verifying", "", ""),
            )
            self.assertEqual(
                self.conn.execute(
                    "SELECT COUNT(*) FROM context_verified"
                ).fetchone()[0],
                verified_before_negative,
            )

        for index in range(5):
            self._insert_phase2_confirmation_card(
                f"phase2_visible_{index}",
                object_label=f"Visible question {index}",
                at=(action_now + timedelta(minutes=index + 2)).astimezone(),
            )
        self._insert_phase2_confirmation_card(
            "phase2_insufficient",
            object_label="ambiguous",
            at=(action_now + timedelta(minutes=20)).astimezone(),
            answerable=False,
        )
        snapshot = context_snapshot(self.conn, self._wire({}), now=action_now)
        self.assertLessEqual(len(snapshot["confirmation_questions"]), 50)
        self.assertEqual(
            sum(
                "Visible question" in question["title"]
                for question in snapshot["confirmation_questions"]
            ),
            5,
        )
        self.assertNotIn(
            "information_insufficient",
            json.dumps(snapshot, ensure_ascii=False),
        )

    def test_phase2_confirmation_links_make_reanswer_delete_and_revoke_fail_closed(self) -> None:
        payload = self._phase2_manual_action_payload(
            idempotency_key="idem_f2100001",
            safe_label="Cascade source one",
        )
        recorded = owner_context_action(self.conn, self._wire(payload), now=self.now)
        self.assertEqual(recorded["status"], "recorded")
        link = self.conn.execute(
            "SELECT * FROM context_activity_confirmation_links WHERE link_ref=?",
            (recorded["action_ref"],),
        ).fetchone()
        verified_count = self.conn.execute(
            "SELECT COUNT(*) FROM context_verified"
        ).fetchone()[0]

        attempted_reanswer = owner_context_action(
            self.conn,
            self._wire({**payload, "idempotency_key": "idem_f2100002"}),
            now=self.now,
        )
        self.assertEqual(attempted_reanswer["status"], "conflict")
        self.assertNotIn("reason_code", attempted_reanswer)
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM context_activity_confirmation_links "
                "WHERE confirmation_ref=?",
                (recorded["confirmation_ref"],),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM context_verified").fetchone()[0],
            verified_count,
        )

        handoff = build_local_handoff(
            self.conn,
            self._wire({"project_ref": self.project}),
            now=self.now,
        )
        self.assertEqual(handoff["status"], "available")
        deleted = delete_context_observation(
            self.conn,
            observation_ref=str(link["observation_ref"]),
            reason_code="user_deleted",
            now=self.now,
        )
        self.assertEqual(deleted["status"], "completed")
        self.assertEqual(
            tuple(self.conn.execute(
                "SELECT status,revocation_reason_code "
                "FROM context_activity_confirmation_links WHERE link_ref=?",
                (recorded["action_ref"],),
            ).fetchone()),
            ("revoked", "user_deleted"),
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_bundle_manifests WHERE bundle_ref=?",
                (handoff["handoff_ref"],),
            ).fetchone()[0],
            "restricted",
        )
        snapshot_after_delete = context_snapshot(
            self.conn,
            self._wire({}),
            now=self.now,
        )
        self.assertNotIn(
            "Cascade source one",
            json.dumps(snapshot_after_delete, ensure_ascii=False),
        )
        stale_replay = owner_context_action(
            self.conn,
            self._wire(payload),
            now=self.now,
        )
        self.assertEqual(stale_replay["status"], "rejected")
        self.assertNotIn("reason_code", stale_replay)
        feedback = record_product_feedback(
            self.conn,
            self._wire(
                {
                    "contract_version": PRODUCT_FEEDBACK_VERSION,
                    "idempotency_key": "idem_f2100003",
                    "source_kind": "local_handoff",
                    "source_ref": handoff["handoff_ref"],
                    "project_ref": self.project,
                    "useful": True,
                }
            ),
            now=self.now,
        )
        self.assertEqual(feedback["status"], "rejected")

        second_payload = self._phase2_manual_action_payload(
            idempotency_key="idem_f2100004",
            safe_label="Cascade source two",
        )
        second = owner_context_action(
            self.conn,
            self._wire(second_payload),
            now=self.now,
        )
        self.assertEqual(second["status"], "recorded")
        second_link = self.conn.execute(
            "SELECT verified_ref FROM context_activity_confirmation_links "
            "WHERE link_ref=?",
            (second["action_ref"],),
        ).fetchone()
        second_object = self.conn.execute(
            "SELECT object_ref FROM context_verified WHERE verified_ref=?",
            (second_link["verified_ref"],),
        ).fetchone()[0]
        revoked = revoke_context_label(self.conn, str(second_object))
        self.assertEqual(revoked["status"], "revoked")
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_activity_confirmation_links "
                "WHERE link_ref=?",
                (second["action_ref"],),
            ).fetchone()[0],
            "revoked",
        )
        snapshot_after_label_revoke = context_snapshot(
            self.conn,
            self._wire({}),
            now=self.now,
        )
        self.assertNotIn(
            "Cascade source two",
            json.dumps(snapshot_after_label_revoke, ensure_ascii=False),
        )

    def test_phase2_manual_note_empty_database_derives_owner_graph_and_outcome_states(self) -> None:
        empty_db = self.root / "phase2-empty.sqlite"
        empty = ensure_ready(empty_db)
        try:
            self.assertEqual(
                configure_context_console(
                    empty,
                    enabled=True,
                    confirmation=CONTEXT_FEATURE_CONFIRMATION,
                )["status"],
                "enabled",
            )
            state_by_outcome = {
                "completed": "completed_confirmed",
                "blocked": "blocked",
                "progressing": "in_progress",
                "preparation_only": "in_progress",
                "unknown": "intent_observed",
            }
            recorded: list[dict[str, Any]] = []
            for index, (outcome, expected_state) in enumerate(
                state_by_outcome.items(),
                start=1,
            ):
                payload = {
                    "contract_version": OWNER_CONTEXT_ACTION_VERSION,
                    "action": "manual_note",
                    "idempotency_key": f"idem_5100000{index}",
                    "confirmation_action": OWNER_CONTEXT_ACTION_CONFIRMATION,
                    "safe_label": f"本机补记 {index}",
                    "outcome": outcome,
                    "started_at": self.now.isoformat(timespec="seconds"),
                    # Spoofed legacy fields are intentionally ignored.
                    "subject_ref": "subject_attacker",
                    "privacy_class": "restricted_local",
                    "context_state": "completed_confirmed",
                }
                result = owner_context_action(
                    empty,
                    self._wire(payload),
                    now=self.now,
                )
                self.assertEqual(
                    (result["status"], result["verification_state"]),
                    ("recorded", "verified"),
                )
                link = empty.execute(
                    "SELECT verified_ref FROM context_activity_confirmation_links "
                    "WHERE link_ref=?",
                    (result["action_ref"],),
                ).fetchone()
                graph = empty.execute(
                    "SELECT v.state,v.memory_eligible,v.privacy_class,"
                    "lp.label AS project_label,ls.subject_scope "
                    "FROM context_verified v "
                    "JOIN context_label_registry lp ON lp.ref=v.project_ref "
                    "JOIN context_label_registry ls ON ls.ref=v.subject_ref "
                    "WHERE v.verified_ref=?",
                    (link["verified_ref"],),
                ).fetchone()
                self.assertEqual(
                    tuple(graph),
                    (
                        expected_state,
                        0,
                        "private_local",
                        "未归类",
                        "owner",
                    ),
                )
                recorded.append(result)
            self.assertEqual(
                empty.execute(
                    "SELECT COUNT(*) FROM context_memory_candidates"
                ).fetchone()[0],
                0,
            )

            first_payload = {
                "contract_version": OWNER_CONTEXT_ACTION_VERSION,
                "action": "manual_note",
                "idempotency_key": "idem_51000001",
                "confirmation_action": OWNER_CONTEXT_ACTION_CONFIRMATION,
                "safe_label": "冲突正文",
                "outcome": "completed",
                "started_at": self.now.isoformat(timespec="seconds"),
            }
            conflict = owner_context_action(
                empty,
                self._wire(first_payload),
                now=self.now,
            )
            self.assertEqual(conflict["status"], "conflict")
            self.assertNotIn("reason_code", conflict)
            self.assertFalse(empty.in_transaction)

            accepted_180 = owner_context_action(
                empty,
                self._wire(
                    {
                        "contract_version": OWNER_CONTEXT_ACTION_VERSION,
                        "action": "manual_note",
                        "idempotency_key": "idem_51000007",
                        "confirmation_action": OWNER_CONTEXT_ACTION_CONFIRMATION,
                        "safe_label": "甲" * 180,
                        "outcome": "progressing",
                        "started_at": self.now.isoformat(timespec="seconds"),
                    }
                ),
                now=self.now,
            )
            self.assertEqual(accepted_180["status"], "recorded")
            rejected_181 = owner_context_action(
                empty,
                self._wire(
                    {
                        "contract_version": OWNER_CONTEXT_ACTION_VERSION,
                        "action": "manual_note",
                        "idempotency_key": "idem_51000008",
                        "confirmation_action": OWNER_CONTEXT_ACTION_CONFIRMATION,
                        "safe_label": "甲" * 181,
                        "outcome": "progressing",
                        "started_at": self.now.isoformat(timespec="seconds"),
                    }
                ),
                now=self.now,
            )
            self.assertEqual(rejected_181["status"], "rejected")
            self.assertNotIn("reason_code", rejected_181)

            counts_before_fault = {
                table: empty.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "activity_confirmation_items",
                    "context_label_registry",
                    "context_observations",
                    "context_verified",
                    "context_activity_confirmation_links",
                )
            }
            empty.execute(
                "CREATE TRIGGER phase2_empty_manual_fault BEFORE INSERT "
                "ON context_activity_confirmation_links BEGIN "
                "SELECT RAISE(ABORT,'phase2 empty manual fault'); END"
            )
            empty.commit()
            failed = owner_context_action(
                empty,
                self._wire(
                    {
                        "contract_version": OWNER_CONTEXT_ACTION_VERSION,
                        "action": "manual_note",
                        "idempotency_key": "idem_51000009",
                        "confirmation_action": OWNER_CONTEXT_ACTION_CONFIRMATION,
                        "safe_label": "必须全量回滚",
                        "project_label": "新项目也必须回滚",
                        "outcome": "blocked",
                        "started_at": self.now.isoformat(timespec="seconds"),
                    }
                ),
                now=self.now,
            )
            self.assertNotEqual(failed["status"], "recorded")
            self.assertFalse(empty.in_transaction)
            self.assertEqual(
                {
                    table: empty.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in counts_before_fault
                },
                counts_before_fault,
            )
            empty.execute("DROP TRIGGER phase2_empty_manual_fault")
            empty.commit()
        finally:
            empty.close()

    def test_phase2_manual_note_concurrent_replay_writes_one_atomic_graph(self) -> None:
        payload = {
            "contract_version": OWNER_CONTEXT_ACTION_VERSION,
            "action": "manual_note",
            "idempotency_key": "idem_51100001",
            "confirmation_action": OWNER_CONTEXT_ACTION_CONFIRMATION,
            "safe_label": "并发补记只能形成一条图",
            "outcome": "completed",
            "started_at": self.now.isoformat(timespec="seconds"),
        }
        barrier = threading.Barrier(2)
        results: list[dict[str, Any]] = []
        errors: list[BaseException] = []

        def submit_action() -> None:
            connection = ensure_ready(self.db)
            try:
                barrier.wait(5)
                results.append(
                    owner_context_action(
                        connection,
                        self._wire(payload),
                        now=self.now,
                    )
                )
            except BaseException as exc:  # pragma: no cover - diagnostic only
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=submit_action) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(
            sorted(result["status"] for result in results),
            ["recorded", "replayed"],
        )
        link = self.conn.execute(
            "SELECT observation_ref,evidence_ref,candidate_ref,verified_ref "
            "FROM context_activity_confirmation_links WHERE idempotency_key=?",
            (payload["idempotency_key"],),
        ).fetchone()
        self.assertIsNotNone(link)
        for table, column, value in (
            ("context_observations", "observation_ref", link["observation_ref"]),
            ("context_evidence", "evidence_ref", link["evidence_ref"]),
            ("context_candidates", "candidate_ref", link["candidate_ref"]),
            ("context_verified", "verified_ref", link["verified_ref"]),
        ):
            self.assertEqual(
                self.conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {column}=?",
                    (str(value),),
                ).fetchone()[0],
                1,
            )

    def test_phase2_confirm_card_minimal_wire_purges_evidence_and_last_inbox_card_is_actionable(self) -> None:
        action_now = self.now
        item_key = "phase2_minimal_confirm"
        self._insert_phase2_confirmation_card(
            item_key,
            object_label="Minimal wire completion",
            at=action_now.astimezone(),
        )
        directory = ensure_evidence_root(self.root)
        evidence_path = directory / "phase2-minimal-confirm.png"
        evidence_bytes = b"phase2-local-evidence"
        evidence_path.write_bytes(evidence_bytes)
        evidence_path.chmod(0o600)
        self.conn.execute(
            "INSERT INTO activity_confirmation_evidence_files("
            "evidence_id,source_frame_id,captured_at,display_id,file_name,"
            "content_sha256,byte_size,state,backup_excluded,last_error,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,'available',1,'',?,?)",
            (
                "evidence_phase2minimal",
                "frame_phase2minimal",
                action_now.isoformat(timespec="seconds"),
                "main",
                evidence_path.name,
                hashlib.sha256(evidence_bytes).hexdigest(),
                len(evidence_bytes),
                action_now.isoformat(timespec="seconds"),
                action_now.isoformat(timespec="seconds"),
            ),
        )
        self.conn.execute(
            "INSERT INTO activity_confirmation_evidence_refs("
            "evidence_id,item_key,source_activity_id,deletion_group_id,created_at,"
            "released_at,release_reason) VALUES(?,?, '', '', ?,NULL,'')",
            (
                "evidence_phase2minimal",
                item_key,
                action_now.isoformat(timespec="seconds"),
            ),
        )
        self.conn.commit()
        snapshot = context_snapshot(self.conn, self._wire({}), now=action_now)
        question = next(
            item
            for item in snapshot["confirmation_questions"]
            if "Minimal wire completion" in item["title"]
        )
        payload = self._phase2_confirmation_action_payload(
            question,
            idempotency_key="idem_52000001",
            answer="occurred",
        )
        self.assertEqual(
            set(payload),
            {
                "contract_version",
                "action",
                "idempotency_key",
                "confirmation_action",
                "confirmation_ref",
                "expected_revision",
                "answer",
                "reason",
            },
        )
        recorded = owner_context_action(
            self.conn,
            self._wire(payload),
            now=action_now,
        )
        self.assertEqual(
            (recorded["status"], recorded["verification_state"]),
            ("recorded", "verified"),
        )
        graph = self.conn.execute(
            "SELECT v.state,v.memory_eligible,ls.subject_scope "
            "FROM context_activity_confirmation_links l "
            "JOIN context_verified v ON v.verified_ref=l.verified_ref "
            "JOIN context_label_registry ls ON ls.ref=v.subject_ref "
            "WHERE l.link_ref=?",
            (recorded["action_ref"],),
        ).fetchone()
        self.assertEqual(tuple(graph), ("completed_confirmed", 0, "owner"))
        evidence = self.conn.execute(
            "SELECT state,file_name,last_error FROM activity_confirmation_evidence_files "
            "WHERE evidence_id='evidence_phase2minimal'"
        ).fetchone()
        self.assertEqual(tuple(evidence), ("deleted", "", ""))
        self.assertFalse(evidence_path.exists())
        snapshot_after_confirmation = context_snapshot(
            self.conn,
            self._wire({}),
            now=action_now,
        )
        self.assertEqual(snapshot_after_confirmation["legacy_confirmed"], [])
        self.assertEqual(
            sum(
                context["context_ref"] == recorded["context_ref"]
                for project in snapshot_after_confirmation["projects"]
                for context in project["verified_contexts"]
            ),
            1,
        )

        for index in range(50):
            self._insert_phase2_confirmation_card(
                f"phase2_inbox_{index:02d}",
                object_label=f"Inbox answerable {index:02d}",
                at=(
                    action_now + timedelta(minutes=(index + 1) * 8)
                ).astimezone(),
            )
        inbox = context_snapshot(
            self.conn,
            self._wire({}),
            now=action_now + timedelta(hours=8),
        )["confirmation_questions"]
        self.assertEqual(len(inbox), 50)
        last_result = owner_context_action(
            self.conn,
            self._wire(
                self._phase2_confirmation_action_payload(
                    inbox[-1],
                    idempotency_key="idem_52000002",
                    answer="ignored",
                )
            ),
            now=action_now + timedelta(hours=8),
        )
        self.assertEqual(last_result["status"], "recorded")

    def test_phase2_owner_reanswer_supersedes_graph_atomically_and_versions_concurrency(self) -> None:
        action_now = datetime.now(UTC).replace(microsecond=0)
        self._insert_phase2_confirmation_card(
            "phase2_reanswer",
            object_label="Governed reanswer",
            at=action_now.astimezone(),
        )
        question = next(
            item
            for item in context_snapshot(
                self.conn,
                self._wire({}),
                now=action_now,
            )["confirmation_questions"]
            if "Governed reanswer" in item["title"]
        )
        occurred_payload = self._phase2_confirmation_action_payload(
            question,
            idempotency_key="idem_52500001",
            answer="occurred",
        )
        occurred = owner_context_action(
            self.conn,
            self._wire(occurred_payload),
            now=action_now,
        )
        self.assertEqual(occurred["verification_state"], "verified")
        old_verified = self.conn.execute(
            "SELECT verified_ref FROM context_activity_confirmation_links "
            "WHERE link_ref=?",
            (occurred["action_ref"],),
        ).fetchone()[0]
        handoff = build_local_handoff(
            self.conn,
            self._wire(
                {
                    "project_ref": self.conn.execute(
                        "SELECT project_ref FROM context_verified WHERE verified_ref=?",
                        (old_verified,),
                    ).fetchone()[0]
                }
            ),
            now=action_now,
        )
        self.assertEqual(handoff["status"], "available")

        denied_payload = {
            **occurred_payload,
            "idempotency_key": "idem_52500002",
            "expected_revision": occurred["confirmation_revision"],
            "answer": "not_occurred",
        }
        denied = owner_context_action(
            self.conn,
            self._wire(denied_payload),
            now=action_now,
        )
        self.assertEqual(
            (denied["status"], denied["verification_state"]),
            ("recorded", "non_verifying"),
        )
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT status,revocation_reason_code "
                    "FROM context_activity_confirmation_links WHERE link_ref=?",
                    (occurred["action_ref"],),
                ).fetchone()
            ),
            ("revoked", "owner_reanswered"),
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_verified WHERE verified_ref=?",
                (old_verified,),
            ).fetchone()[0],
            "revoked",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_bundle_manifests WHERE bundle_ref=?",
                (handoff["handoff_ref"],),
            ).fetchone()[0],
            "restricted",
        )
        stale_original = owner_context_action(
            self.conn,
            self._wire(occurred_payload),
            now=action_now,
        )
        self.assertEqual(stale_original["status"], "rejected")
        self.assertNotIn("reason_code", stale_original)

        restored_payload = {
            **occurred_payload,
            "idempotency_key": "idem_52500003",
            "expected_revision": denied["confirmation_revision"],
        }
        restored = owner_context_action(
            self.conn,
            self._wire(restored_payload),
            now=action_now,
        )
        self.assertEqual(
            (restored["status"], restored["verification_state"]),
            ("recorded", "verified"),
            restored,
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_activity_confirmation_links "
                "WHERE link_ref=?",
                (denied["action_ref"],),
            ).fetchone()[0],
            "revoked",
        )
        self.assertEqual(
            owner_context_action(
                self.conn,
                self._wire(restored_payload),
                now=action_now,
            )["status"],
            "replayed",
        )
        restored_snapshot = context_snapshot(
            self.conn,
            self._wire({}),
            now=action_now,
        )
        self.assertEqual(restored_snapshot["legacy_confirmed"], [])

        before_rollback = self.conn.execute(
            "SELECT answer,state FROM activity_confirmation_items "
            "WHERE item_key='phase2_reanswer'"
        ).fetchone()
        self.conn.execute(
            "CREATE TRIGGER phase2_reanswer_fault BEFORE INSERT "
            "ON context_activity_confirmation_links BEGIN "
            "SELECT RAISE(ABORT,'phase2 reanswer fault'); END"
        )
        self.conn.commit()
        rollback_payload = {
            **denied_payload,
            "idempotency_key": "idem_52500004",
            "expected_revision": restored["confirmation_revision"],
        }
        rolled_back = owner_context_action(
            self.conn,
            self._wire(rollback_payload),
            now=action_now,
        )
        self.assertNotEqual(rolled_back["status"], "recorded")
        self.assertEqual(
            tuple(
                self.conn.execute(
                    "SELECT answer,state FROM activity_confirmation_items "
                    "WHERE item_key='phase2_reanswer'"
                ).fetchone()
            ),
            tuple(before_rollback),
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_activity_confirmation_links "
                "WHERE link_ref=?",
                (restored["action_ref"],),
            ).fetchone()[0],
            "verified",
        )
        self.conn.execute("DROP TRIGGER phase2_reanswer_fault")
        self.conn.commit()

        self._insert_phase2_confirmation_card(
            "phase2_defer_reanswer",
            object_label="Deferred then verified",
            at=(action_now + timedelta(minutes=5)).astimezone(),
        )
        defer_question = next(
            item
            for item in context_snapshot(
                self.conn,
                self._wire({}),
                now=action_now,
            )["confirmation_questions"]
            if "Deferred then verified" in item["title"]
        )
        deferred_payload = self._phase2_confirmation_action_payload(
            defer_question,
            idempotency_key="idem_52500005",
            answer="defer",
        )
        deferred = owner_context_action(
            self.conn,
            self._wire(deferred_payload),
            now=action_now,
        )
        verified_after_defer = owner_context_action(
            self.conn,
            self._wire(
                {
                    **deferred_payload,
                    "idempotency_key": "idem_52500006",
                    "expected_revision": deferred["confirmation_revision"],
                    "answer": "occurred",
                }
            ),
            now=action_now,
        )
        self.assertEqual(
            (
                deferred["verification_state"],
                verified_after_defer["verification_state"],
            ),
            ("non_verifying", "verified"),
        )

        concurrent_revision = verified_after_defer["confirmation_revision"]
        concurrent_payloads = [
            {
                **deferred_payload,
                "idempotency_key": f"idem_5250000{index}",
                "expected_revision": concurrent_revision,
                "answer": answer,
            }
            for index, answer in ((7, "not_occurred"), (8, "ignored"))
        ]
        barrier = threading.Barrier(2)
        concurrent_results: list[dict[str, Any]] = []

        def submit_reanswer(payload: Mapping[str, Any]) -> None:
            connection = ensure_ready(self.db)
            try:
                barrier.wait(5)
                concurrent_results.append(
                    owner_context_action(
                        connection,
                        self._wire(payload),
                        now=action_now,
                    )
                )
            finally:
                connection.close()

        threads = [
            threading.Thread(target=submit_reanswer, args=(payload,))
            for payload in concurrent_payloads
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(
            sorted(result["status"] for result in concurrent_results),
            ["recorded", "rejected"],
        )
        self.assertTrue(
            all("reason_code" not in result for result in concurrent_results)
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM context_activity_confirmation_links "
                "WHERE confirmation_ref=? AND status IN ('verified','non_verifying')",
                (deferred["confirmation_ref"],),
            ).fetchone()[0],
            1,
        )

    def test_phase2_local_handoff_feedback_source_binding_and_concurrent_replay(self) -> None:
        controlled = self._ingest_trusted_confirmation(
            idempotency_key="idem_53000001",
            source_ref="source_53000001",
        )
        clue = record_unverified_clue(
            self.conn,
            source_kind="screen",
            source_ref="source_53000002",
            occurred_at=self.now.isoformat(timespec="seconds"),
            project_ref=self.project,
            workstream_ref=self.workstream,
            object_ref=self.object,
            version_ref=self.version,
            subject_ref=self.owner,
            privacy_class="private_local",
            now=self.now,
        )
        self.assertEqual(clue["status"], "accepted")
        handoff = build_local_handoff(
            self.conn,
            self._wire({"project_ref": self.project}),
            now=self.now,
        )
        self.assertEqual(handoff["status"], "available")
        manifest = self.conn.execute(
            "SELECT frozen_payload_json,stable_projection_json,target_ref "
            "FROM context_bundle_manifests WHERE bundle_ref=?",
            (handoff["handoff_ref"],),
        ).fetchone()
        for payload_json in manifest[:2]:
            persisted = json.loads(payload_json)
            self.assertEqual(persisted["unverified_clues"], [])
            self.assertEqual(len(persisted["verified_contexts"]), 1)
        self.assertEqual(manifest["target_ref"], "")
        self.assertEqual(
            {
                str(row["member_kind"])
                for row in self.conn.execute(
                    "SELECT member_kind FROM context_bundle_members WHERE bundle_ref=?",
                    (handoff["handoff_ref"],),
                )
            },
            {"verified_context"},
        )
        self.assertNotIn(
            clue["observation_ref"],
            json.dumps(handoff, ensure_ascii=False),
        )

        experiment = start_product_experiment(
            self.conn,
            experiment_ref="experiment_53000001",
            confirmation="START_LOCAL_PRODUCT_EXPERIMENT",
            now=self.now,
        )
        self.assertTrue(experiment["experiment_started"])
        feedback_payload = {
            "contract_version": PRODUCT_FEEDBACK_VERSION,
            "idempotency_key": "idem_53000003",
            "source_kind": "local_handoff",
            "source_ref": handoff["handoff_ref"],
            "project_ref": self.project,
            "useful": False,
            "resume_seconds": 42.0,
            "experiment_ref": "experiment_53000001",
        }
        barrier = threading.Barrier(2)
        results: list[dict[str, Any]] = []
        errors: list[BaseException] = []

        def submit_feedback() -> None:
            connection = ensure_ready(self.db)
            try:
                barrier.wait(5)
                results.append(
                    record_product_feedback(
                        connection,
                        self._wire(feedback_payload),
                        now=self.now,
                    )
                )
            except BaseException as exc:  # pragma: no cover - diagnostic only
                errors.append(exc)
            finally:
                connection.close()

        threads = [threading.Thread(target=submit_feedback) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(
            sorted(result["status"] for result in results),
            ["recorded", "replayed"],
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM context_product_feedback WHERE source_ref=?",
                (handoff["handoff_ref"],),
            ).fetchone()[0],
            1,
        )
        attempt = self.conn.execute(
            "SELECT useful,outcome,source_ref FROM context_product_experiment_attempts "
            "WHERE experiment_ref='experiment_53000001'"
        ).fetchone()
        self.assertEqual(tuple(attempt[:2]), (0, "not_useful"))
        self.assertRegex(str(attempt["source_ref"]), r"^source_[0-9a-f]{32}$")
        feedback_binding = self.conn.execute(
            "SELECT source_kind,source_ref,bundle_ref FROM context_product_feedback "
            "WHERE idempotency_key=?",
            (feedback_payload["idempotency_key"],),
        ).fetchone()
        self.assertEqual(
            tuple(feedback_binding),
            ("local_handoff", handoff["handoff_ref"], handoff["handoff_ref"]),
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM context_product_wins"
            ).fetchone()[0],
            0,
        )
        cross_kind = record_product_feedback(
            self.conn,
            self._wire(
                {
                    **feedback_payload,
                    "idempotency_key": "idem_53000004",
                    "source_kind": "bundle",
                }
            ),
            now=self.now,
        )
        self.assertEqual(cross_kind["status"], "rejected")
        self.assertNotIn("reason_code", cross_kind)
        forged = record_product_feedback(
            self.conn,
            self._wire(
                {
                    **feedback_payload,
                    "idempotency_key": "idem_53000005",
                    "source_ref": "bundle_53000000000000000000000000000000",
                }
            ),
            now=self.now,
        )
        self.assertEqual(forged["status"], "rejected")
        self.assertNotIn("reason_code", forged)
        same_key_different_source = record_product_feedback(
            self.conn,
            self._wire(
                {
                    **feedback_payload,
                    "source_ref": "bundle_53000000000000000000000000000000",
                }
            ),
            now=self.now,
        )
        self.assertEqual(same_key_different_source["status"], "conflict")
        self.assertNotIn("reason_code", same_key_different_source)

        self._ingest_trusted_confirmation(
            idempotency_key="idem_53000006",
            source_ref="source_53000006",
        )
        second_handoff = build_local_handoff(
            self.conn,
            self._wire({"project_ref": self.project}),
            now=self.now,
        )
        self.assertNotEqual(second_handoff["handoff_ref"], handoff["handoff_ref"])
        useful = record_product_feedback(
            self.conn,
            self._wire(
                {
                    **feedback_payload,
                    "idempotency_key": "idem_53000007",
                    "source_ref": second_handoff["handoff_ref"],
                    "useful": True,
                    "resume_seconds": 12.0,
                }
            ),
            now=self.now,
        )
        self.assertTrue(useful["win_recorded"])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM context_product_wins"
            ).fetchone()[0],
            1,
        )
        self.assertEqual(controlled["result_status"], "completed_confirmed")

    def test_phase2_memory_candidate_lifecycle_projection_keeps_writer_off(self) -> None:
        candidates: dict[str, str] = {}
        for index, name in enumerate(
            ("active", "expired", "wrong_subject", "withdrawn"),
            start=1,
        ):
            ingested = self._ingest_memory_confirmation(
                idempotency_key=f"idem_5400000{index}",
                source_ref=f"source_5400000{index}",
            )
            created = create_memory_candidate(
                self.conn,
                verified_ref=ingested["verified_ref"],
                subject_ref=self.owner,
                now=self.now,
            )
            self.assertEqual(created["status"], "candidate_only")
            candidates[name] = created["memory_candidate_ref"]
        self.conn.execute(
            "UPDATE context_memory_candidates SET status='revoked',expires_at=?,"
            "revoked_at=?,revocation_reason_code='verified_retention_expired' "
            "WHERE memory_candidate_ref=?",
            (
                (self.now - timedelta(seconds=1)).isoformat(timespec="seconds"),
                self.now.isoformat(timespec="seconds"),
                candidates["expired"],
            ),
        )
        self.conn.execute(
            "UPDATE context_memory_candidates SET status='revoked',subject_ref=?,"
            "revoked_at=?,revocation_reason_code='wrong_subject_detected' "
            "WHERE memory_candidate_ref=?",
            (
                self.third_party,
                self.now.isoformat(timespec="seconds"),
                candidates["wrong_subject"],
            ),
        )
        self.conn.execute(
            "UPDATE context_memory_candidates SET status='revoked',revoked_at=?,"
            "revocation_reason_code='user_withdrawn' WHERE memory_candidate_ref=?",
            (
                self.now.isoformat(timespec="seconds"),
                candidates["withdrawn"],
            ),
        )
        self.conn.commit()
        listed = list_memory_candidates(self.conn, self._wire({}), now=self.now)
        self.assertEqual(listed["status"], "available")
        self.assertIsNone(listed["actions"])
        self.assertEqual(
            {item["state"] for item in listed["candidates"]},
            {
                "pending_review",
                "expired",
                "wrong_subject_quarantined",
                "withdrawn",
            },
        )
        for item in listed["candidates"]:
            self.assertFalse(item["direct_write"])
            self.assertFalse(item["writer_enabled"])
            self.assertFalse(item["memory_write_actions"])
            self.assertRegex(item["context_ref"], r"^context_[0-9a-f]{32}$")
            self.assertRegex(
                item["memory_candidate_ref"],
                r"^memory_[0-9a-f]{32}$",
            )

    def test_phase2_memory_and_delivery_reads_hold_consistent_revocation_lease(self) -> None:
        memory_ingest = self._ingest_memory_confirmation(
            idempotency_key="idem_54500001",
            source_ref="source_54500001",
        )
        created = create_memory_candidate(
            self.conn,
            verified_ref=memory_ingest["verified_ref"],
            subject_ref=self.owner,
            now=self.now,
        )
        self.assertEqual(created["status"], "candidate_only")

        read_entered = threading.Event()
        release_read = threading.Event()
        delete_done = threading.Event()
        memory_results: list[dict[str, Any]] = []
        original_validate = context_console_module._validate_memory_candidate_eligible

        def blocking_validate(*args: Any, **kwargs: Any) -> Any:
            read_entered.set()
            self.assertTrue(release_read.wait(5))
            return original_validate(*args, **kwargs)

        def read_memory() -> None:
            connection = ensure_ready(self.db)
            try:
                memory_results.append(
                    list_memory_candidates(
                        connection,
                        self._wire({}),
                        now=self.now,
                    )
                )
            finally:
                connection.close()

        def delete_memory_source() -> None:
            connection = ensure_ready(self.db)
            try:
                delete_context_observation(
                    connection,
                    observation_ref=memory_ingest["observation_ref"],
                    reason_code="user_deleted",
                    now=self.now,
                )
            finally:
                connection.close()
                delete_done.set()

        with mock.patch.object(
            context_console_module,
            "_validate_memory_candidate_eligible",
            side_effect=blocking_validate,
        ):
            reader = threading.Thread(target=read_memory)
            reader.start()
            self.assertTrue(read_entered.wait(5))
            deleter = threading.Thread(target=delete_memory_source)
            deleter.start()
            self.assertFalse(delete_done.wait(0.1))
            release_read.set()
            reader.join(5)
            deleter.join(5)
        self.assertEqual(memory_results[0]["candidates"][0]["state"], "pending_review")
        self.assertTrue(delete_done.is_set())
        terminal_memory = list_memory_candidates(
            self.conn,
            self._wire({}),
            now=self.now,
        )
        self.assertEqual(terminal_memory["candidates"][0]["state"], "withdrawn")

        prepared = self._prepare_one_delivery(idempotency_key="idem_54500002")
        consumed_payload = self._delivery_payload(
            principal_name="consumer",
            delivery_ref=prepared["delivery_ref"],
            bundle_hash=prepared["bundle_hash"],
            transition="consumed",
            idempotency_key="idem_54500003",
            provider_receipt_ref="provider_54500003",
        )
        ingest_delivery_receipt(
            self.conn,
            self._wire(consumed_payload),
            verification_secret=self.secrets["consumer"],
            now=self.now,
        )
        delivered_payload = self._delivery_payload(
            principal_name="provider",
            delivery_ref=prepared["delivery_ref"],
            bundle_hash=prepared["bundle_hash"],
            transition="delivered",
            idempotency_key="idem_54500004",
            provider_receipt_ref="provider_54500004",
        )
        ingest_delivery_receipt(
            self.conn,
            self._wire(delivered_payload),
            verification_secret=self.secrets["provider"],
            now=self.now,
        )
        delivery_observation = self.conn.execute(
            "SELECT observation_ref FROM context_observations "
            "WHERE source_ref='source_54500002'"
        ).fetchone()[0]
        delivery_entered = threading.Event()
        release_delivery = threading.Event()
        delivery_delete_done = threading.Event()
        delivery_results: list[dict[str, Any]] = []
        original_public_label = context_console_module._public_label
        blocked_once = False

        def blocking_public_label(*args: Any, **kwargs: Any) -> Any:
            nonlocal blocked_once
            if not blocked_once:
                blocked_once = True
                delivery_entered.set()
                self.assertTrue(release_delivery.wait(5))
            return original_public_label(*args, **kwargs)

        def read_delivery() -> None:
            connection = ensure_ready(self.db)
            try:
                delivery_results.append(
                    delivery_status(connection, self._wire({}), now=self.now)
                )
            finally:
                connection.close()

        def delete_delivery_source() -> None:
            connection = ensure_ready(self.db)
            try:
                delete_context_observation(
                    connection,
                    observation_ref=str(delivery_observation),
                    reason_code="user_deleted",
                    now=self.now,
                )
            finally:
                connection.close()
                delivery_delete_done.set()

        with mock.patch.object(
            context_console_module,
            "_public_label",
            side_effect=blocking_public_label,
        ):
            reader = threading.Thread(target=read_delivery)
            reader.start()
            self.assertTrue(delivery_entered.wait(5))
            deleter = threading.Thread(target=delete_delivery_source)
            deleter.start()
            self.assertFalse(delivery_delete_done.wait(0.1))
            release_delivery.set()
            reader.join(5)
            deleter.join(5)
        self.assertEqual(
            delivery_results[0]["daily_report"]["current_state"],
            "delivered",
        )
        self.assertTrue(delivery_delete_done.is_set())
        terminal_delivery = delivery_status(
            self.conn,
            self._wire({}),
            now=self.now,
        )
        self.assertEqual(
            terminal_delivery["daily_report"]["current_state"],
            "needs_input",
        )
        self.assertEqual(
            terminal_delivery["deliveries"][0]["withdrawal_state"],
            "restricted",
        )

    def test_phase2_delivery_status_tracks_receipts_retry_and_withdrawal(self) -> None:
        empty = delivery_status(self.conn, self._wire({}), now=self.now)
        self.assertTrue(empty["daily_report"]["needs_input"])
        self.assertEqual(empty["daily_report"]["current_state"], "needs_input")

        prepared = self._prepare_one_delivery(idempotency_key="idem_55000001")
        prepared_status = delivery_status(self.conn, self._wire({}), now=self.now)
        self.assertEqual(
            prepared_status["daily_report"]["current_state"],
            "prepared",
        )
        missing_provider = self._delivery_payload(
            principal_name="consumer",
            delivery_ref=prepared["delivery_ref"],
            bundle_hash=prepared["bundle_hash"],
            transition="consumed",
            idempotency_key="idem_55000002",
        )
        rejected = ingest_delivery_receipt(
            self.conn,
            self._wire(missing_provider),
            verification_secret=self.secrets["consumer"],
            now=self.now,
        )
        self.assertEqual(rejected["status"], "rejected")
        consumed_payload = {
            **missing_provider,
            "provider_receipt_ref": "provider_55000002",
        }
        consumed_payload["signature"] = sign_contract_payload(
            consumed_payload,
            self.secrets["consumer"],
        )
        consumed = ingest_delivery_receipt(
            self.conn,
            self._wire(consumed_payload),
            verification_secret=self.secrets["consumer"],
            now=self.now,
        )
        self.assertEqual(consumed["state"], "consumed")
        self.assertEqual(
            delivery_status(self.conn, self._wire({}), now=self.now)[
                "daily_report"
            ]["current_state"],
            "consumed",
        )
        delivered_payload = self._delivery_payload(
            principal_name="provider",
            delivery_ref=prepared["delivery_ref"],
            bundle_hash=prepared["bundle_hash"],
            transition="delivered",
            idempotency_key="idem_55000003",
            provider_receipt_ref="provider_55000003",
        )
        delivered = ingest_delivery_receipt(
            self.conn,
            self._wire(delivered_payload),
            verification_secret=self.secrets["provider"],
            now=self.now,
        )
        self.assertEqual(delivered["state"], "delivered")
        delivered_status = delivery_status(
            self.conn,
            self._wire({}),
            now=self.now,
        )
        self.assertEqual(
            delivered_status["daily_report"]["current_state"],
            "delivered",
        )

        observation_ref = self.conn.execute(
            "SELECT observation_ref FROM context_observations "
            "WHERE source_ref='source_55000001'"
        ).fetchone()[0]
        self.assertEqual(
            delete_context_observation(
                self.conn,
                observation_ref=str(observation_ref),
                reason_code="user_deleted",
                now=self.now,
            )["status"],
            "completed",
        )
        withdrawn = delivery_status(self.conn, self._wire({}), now=self.now)
        self.assertTrue(withdrawn["daily_report"]["needs_input"])
        self.assertEqual(
            withdrawn["daily_report"]["current_state"],
            "needs_input",
        )
        self.assertEqual(
            withdrawn["deliveries"][0]["withdrawal_state"],
            "restricted",
        )
        self.assertIsNone(withdrawn["daily_report"]["delivered_at"])

        self._ingest_trusted_confirmation(
            idempotency_key="idem_55000004",
            source_ref="source_55000004",
        )
        failed_attempt = prepare_delivery(
            self.conn,
            target_ref=self.other_target,
            project_ref=self.project,
            now=self.now,
        )
        failure_payload = self._delivery_payload(
            principal_name="consumer_other",
            delivery_ref=failed_attempt["delivery_ref"],
            bundle_hash=failed_attempt["bundle_hash"],
            transition="failed",
            idempotency_key="idem_55000005",
            failure_reason_code="consumer_rejected",
        )
        self.assertEqual(
            ingest_delivery_receipt(
                self.conn,
                self._wire(failure_payload),
                verification_secret=self.secrets["consumer_other"],
                now=self.now,
            )["state"],
            "failed",
        )
        retry = prepare_delivery(
            self.conn,
            target_ref=self.other_target,
            project_ref=self.project,
            now=self.now,
        )
        self.assertEqual(retry["attempt_no"], 2)
        old_ack = self._delivery_payload(
            principal_name="consumer_other",
            delivery_ref=failed_attempt["delivery_ref"],
            bundle_hash=failed_attempt["bundle_hash"],
            transition="consumed",
            idempotency_key="idem_55000006",
            provider_receipt_ref="provider_55000006",
        )
        self.assertEqual(
            ingest_delivery_receipt(
                self.conn,
                self._wire(old_ack),
                verification_secret=self.secrets["consumer_other"],
                now=self.now,
            )["status"],
            "rejected",
        )
        retry_status = delivery_status(
            self.conn,
            self._wire({"target_ref": self.other_target}),
            now=self.now,
        )
        self.assertEqual(
            (
                retry_status["deliveries"][0]["attempt_no"],
                retry_status["deliveries"][0]["state"],
            ),
            (2, "prepared"),
        )

    def test_phase2_search_stdin_special_characters_and_public_partitioning(self) -> None:
        self._ingest_trusted_confirmation(
            idempotency_key="idem_56000001",
            source_ref="source_56000001",
        )
        record_unverified_clue(
            self.conn,
            source_kind="screen",
            source_ref="source_56000002",
            occurred_at=self.now.isoformat(timespec="seconds"),
            project_ref=self.project,
            workstream_ref=self.workstream,
            object_ref=self.object,
            version_ref=self.version,
            subject_ref=self.owner,
            privacy_class="private_local",
            now=self.now,
        )
        matching = context_snapshot(
            self.conn,
            self._wire({"query": "Context Contract", "include_clues": True}),
            now=self.now,
        )
        self.assertEqual(matching["status"], "available")
        self.assertEqual(
            len(matching["context_bundle_v1"]["verified_contexts"]),
            1,
        )
        self.assertEqual(
            len(matching["context_bundle_v1"]["unverified_clues"]),
            1,
        )
        self.assertTrue(
            all(
                item["verification_state"] == "unverified"
                for item in matching["context_bundle_v1"]["unverified_clues"]
            )
        )
        json_legacy = '{"raw":"legacy compatibility residue"}'
        self._insert_phase2_confirmation_card(
            "phase2_json_legacy",
            object_label="Legacy JSON guard",
            at=self.now,
        )
        self.conn.execute(
            "UPDATE activity_confirmation_items SET state='confirmed',answer='occurred',"
            "answer_reason='correct',answered_at=?,cleaned_content=?,safe_observation=? "
            "WHERE item_key='phase2_json_legacy'",
            (
                self.now.isoformat(timespec="seconds"),
                json_legacy,
                json_legacy,
            ),
        )
        self.conn.commit()
        legacy_guarded = context_snapshot(
            self.conn,
            self._wire({}),
            now=self.now,
        )
        self.assertNotIn(
            json_legacy,
            json.dumps(legacy_guarded, ensure_ascii=False),
        )
        self._insert_phase2_confirmation_card(
            "phase2_manual_legacy_search",
            object_label="Manual Legacy Search",
            at=self.now,
        )
        self.conn.execute(
            "UPDATE activity_confirmation_items SET state='confirmed',answer='occurred',"
            "answer_reason='correct',answered_at=? "
            "WHERE item_key='phase2_manual_legacy_search'",
            (self.now.isoformat(timespec="seconds"),),
        )
        self.conn.commit()
        legacy_search = context_snapshot(
            self.conn,
            self._wire({"query": "Manual Legacy"}),
            now=self.now,
        )
        self.assertEqual(legacy_search["status"], "available")
        matching_legacy = [
            item
            for item in legacy_search["legacy_confirmed"]
            if "Manual Legacy Search" in item["summary"]
        ]
        self.assertEqual(len(matching_legacy), 1)
        legacy_project_ref = matching_legacy[0]["project"]["ref"]
        legacy_project_filter = context_snapshot(
            self.conn,
            self._wire(
                {
                    "project_ref": legacy_project_ref,
                    "start_at": (self.now - timedelta(days=1)).isoformat(
                        timespec="seconds"
                    ),
                    "end_at": (self.now + timedelta(days=1)).isoformat(
                        timespec="seconds"
                    ),
                }
            ),
            now=self.now,
        )
        self.assertEqual(legacy_project_filter["status"], "available")
        self.assertEqual(
            {
                item["project"]["ref"]
                for item in legacy_project_filter["legacy_confirmed"]
            },
            {legacy_project_ref},
        )
        self.assertEqual(
            legacy_project_filter["context_bundle_v1"]["verified_contexts"],
            [],
        )
        for query in (
            "%",
            "_",
            '"Context"',
            "Context OR Contract",
            "Context NEAR Contract",
            "中文 搜索",
        ):
            result = context_snapshot(
                self.conn,
                self._wire({"query": query}),
                now=self.now,
            )
            self.assertIn(result["status"], {"available", "rejected"})
            self.assertNotIn("reason_code", result)
        for query, _expected_reason in (
            ("\u0001", "unicode_control_forbidden"),
            ("\u200b", "unicode_control_forbidden"),
            ("x" * 81, "invalid_search_query"),
            ("/" + "Users/private/file", "path_or_url_content_forbidden"),
        ):
            rejected = context_snapshot(
                self.conn,
                self._wire({"query": query}),
                now=self.now,
            )
            self.assertEqual(rejected["status"], "rejected")
            self.assertNotIn("reason_code", rejected)
            self.assertFalse(rejected["actions_enabled"])
        encoded = json.dumps(matching, ensure_ascii=False)
        for forbidden in (
            '"verified_ref"',
            '"observation_ref"',
            '"candidate_ref"',
            '"evidence_ref"',
            '"receipt_ref"',
            "information_insufficient",
            "/" + "Users/",
            "prompt",
            "chat_content",
        ):
            self.assertNotIn(forbidden, encoded)

        phase2_command = [
            sys.executable,
            "-m",
            "activity_mvp",
            "--db",
            str(self.db),
            "context-console",
            "snapshot",
            "--payload-stdin",
        ]
        phase2_cli = subprocess.run(
            phase2_command,
            input=json.dumps({"query": "Context Contract"}),
            text=True,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(phase2_cli.returncode, 0, phase2_cli.stderr)
        self.assertEqual(json.loads(phase2_cli.stdout)["status"], "available")
        legacy_command = [
            sys.executable,
            "-m",
            "activity_mvp",
            "--db",
            str(self.db),
            "search",
            "--query-stdin",
        ]
        legacy_cli = subprocess.run(
            legacy_command,
            input=json.dumps({"query": 'Context "OR" % _'}),
            text=True,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(legacy_cli.returncode, 0, legacy_cli.stderr)
        positional = subprocess.run(
            [*legacy_command[:-1], "forbidden-query-body"],
            text=True,
            capture_output=True,
            timeout=10,
        )
        self.assertNotEqual(positional.returncode, 0)
        control = subprocess.run(
            legacy_command,
            input=json.dumps({"query": "\u200b"}),
            text=True,
            capture_output=True,
            timeout=10,
        )
        self.assertNotEqual(control.returncode, 0)

    def test_phase2_link_tombstones_are_filtered_before_legacy_candidate_limit(self) -> None:
        stamp = self.now.isoformat(timespec="seconds")
        for index in range(55):
            item_key = f"phase2_linked_recent_{index:02d}"
            confirmation_kind = "manual_note"
            confirmation_key = item_key
            link_status = "revoked"
            self._insert_phase2_confirmation_card(
                item_key,
                object_label=f"Linked recent {index:02d}",
                at=self.now + timedelta(minutes=index + 1),
            )
            if index == 54:
                confirmation_kind = "confirm_card"
                link_status = "expired"
                self.conn.execute(
                    "UPDATE activity_confirmation_items SET group_key='' "
                    "WHERE item_key=?",
                    (item_key,),
                )
            self.conn.execute(
                "UPDATE activity_confirmation_items SET state='confirmed',"
                "answer='occurred',answer_reason='correct',answered_at=? "
                "WHERE item_key=?",
                (stamp, item_key),
            )
            self.conn.execute(
                "INSERT INTO context_activity_confirmation_links("
                "link_ref,confirmation_ref,confirmation_kind,confirmation_key,"
                "confirmation_revision,idempotency_key,payload_hash,answer,"
                "observation_ref,evidence_ref,candidate_ref,verified_ref,"
                "public_context_ref,status,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"action_linked_recent_{index:02d}",
                    f"confirmation_linked_recent_{index:02d}",
                    confirmation_kind,
                    confirmation_key,
                    f"revision_linked_recent_{index:02d}",
                    f"idem_linked_recent_{index:02d}",
                    f"sha256:{index:064x}",
                    "occurred",
                    "",
                    "",
                    "",
                    "",
                    "",
                    link_status,
                    stamp,
                    stamp,
                ),
            )
        sentinel_key = "phase2_unlinked_legacy_sentinel"
        self._insert_phase2_confirmation_card(
            sentinel_key,
            object_label="Historical unlinked sentinel",
            at=self.now - timedelta(minutes=1),
        )
        self.conn.execute(
            "UPDATE activity_confirmation_items SET state='confirmed',"
            "answer='occurred',answer_reason='correct',answered_at=? "
            "WHERE item_key=?",
            (stamp, sentinel_key),
        )
        self.conn.commit()

        snapshot = context_snapshot(
            self.conn,
            self._wire(
                {
                    "query": "Historical unlinked",
                    "limit": 1,
                }
            ),
            now=self.now,
        )
        self.assertEqual(snapshot["status"], "available")
        self.assertEqual(len(snapshot["legacy_confirmed"]), 1)
        self.assertIn(
            "Historical unlinked sentinel",
            snapshot["legacy_confirmed"][0]["summary"],
        )
        self.assertNotIn(
            "Linked recent",
            json.dumps(snapshot, ensure_ascii=False),
        )

    def test_phase2_find_explicit_range_can_group_more_than_today_five(self) -> None:
        project_refs: list[str] = []
        for index in range(6):
            suffix = f"{index + 1:08x}"
            project_ref = f"project_{suffix}"
            workstream_ref = f"workstream_{index + 17:08x}"
            object_ref = f"object_{index + 33:08x}"
            version_ref = f"version_{index + 49:08x}"
            subject_ref = f"subject_{index + 65:08x}"
            self._register_label(
                "project",
                project_ref,
                f"Find Project {index + 1}",
            )
            self._register_label(
                "workstream",
                workstream_ref,
                f"Find Stream {index + 1}",
                project_ref=project_ref,
            )
            self._register_label(
                "object",
                object_ref,
                f"Find Object {index + 1}",
                project_ref=project_ref,
            )
            self._register_label(
                "version",
                version_ref,
                f"Find Version {index + 1}",
                project_ref=project_ref,
            )
            self._register_label(
                "subject",
                subject_ref,
                "Local Owner",
                subject_scope="owner",
                project_ref=project_ref,
            )
            result = record_unverified_clue(
                self.conn,
                source_kind="screen",
                source_ref=f"source_{index + 81:08x}",
                occurred_at=self.now.isoformat(timespec="seconds"),
                project_ref=project_ref,
                workstream_ref=workstream_ref,
                object_ref=object_ref,
                version_ref=version_ref,
                subject_ref=subject_ref,
                privacy_class="private_local",
                now=self.now,
            )
            self.assertEqual(result["status"], "accepted")
            project_refs.append(project_ref)
        today = context_snapshot(self.conn, self._wire({}), now=self.now)
        self.assertLessEqual(len(today["projects"]), 5)
        ranged = context_snapshot(
            self.conn,
            self._wire(
                {
                    "start_at": (self.now - timedelta(days=89)).isoformat(
                        timespec="seconds"
                    ),
                    "end_at": (self.now + timedelta(days=1)).isoformat(
                        timespec="seconds"
                    ),
                    "limit": 50,
                }
            ),
            now=self.now,
        )
        self.assertGreaterEqual(len(ranged["projects"]), 6)
        filtered = context_snapshot(
            self.conn,
            self._wire(
                {
                    "project_ref": project_refs[-1],
                    "start_at": (self.now - timedelta(days=89)).isoformat(
                        timespec="seconds"
                    ),
                    "end_at": (self.now + timedelta(days=1)).isoformat(
                        timespec="seconds"
                    ),
                }
            ),
            now=self.now,
        )
        self.assertEqual(len(filtered["projects"]), 1)
        self.assertEqual(
            filtered["projects"][0]["project"]["ref"],
            project_refs[-1],
        )

    def test_phase2_context_search_is_bounded_with_10k_graph_and_100k_fts_rows(self) -> None:
        occurred = self.now.isoformat(timespec="seconds")
        expires = (self.now + timedelta(days=30)).isoformat(timespec="seconds")
        recursive = (
            "WITH RECURSIVE n(x) AS (VALUES(1) "
            "UNION ALL SELECT x+1 FROM n WHERE x<10000) "
        )
        self.conn.execute(
            recursive
            + "INSERT INTO context_observations("
            "observation_ref,source_kind,source_ref,source_event_ref,principal_id,key_id,"
            "adapter_kind,receipt_ref,lifecycle_event,occurred_at,project_ref,workstream_ref,"
            "object_ref,version_ref,subject_ref,privacy_class,trust_partition,status,expires_at,created_at"
            ") SELECT printf('observation_scale_%06d',x),'user_confirmation',"
            "printf('source_scale_%06d',x),'','principal_b2b2b2b2','key_b2b2b2b2',"
            "'user_confirmation',printf('receipt_scale_%06d',x),'',?,?,?,?,?,?,"
            "'private_local','verified_eligible','active',?,? FROM n",
            (
                occurred,
                self.project,
                self.workstream,
                self.object,
                self.version,
                self.owner,
                expires,
                occurred,
            ),
        )
        self.conn.execute(
            recursive
            + "INSERT INTO context_evidence("
            "evidence_ref,observation_ref,evidence_kind,evidence_tier,state,state_cap,"
            "provenance_ref,source_ref,occurred_at,project_ref,workstream_ref,object_ref,"
            "version_ref,subject_ref,principal_id,key_id,adapter_kind,privacy_class,status,"
            "expires_at,created_at) SELECT printf('evidence_scale_%06d',x),"
            "printf('observation_scale_%06d',x),'user_confirmation','P0',"
            "'completed_confirmed','completed_confirmed',printf('receipt_scale_%06d',x),"
            "printf('source_scale_%06d',x),?,?,?,?,?,?,"
            "'principal_b2b2b2b2','key_b2b2b2b2','user_confirmation','private_local',"
            "'active',?,? FROM n",
            (
                occurred,
                self.project,
                self.workstream,
                self.object,
                self.version,
                self.owner,
                expires,
                occurred,
            ),
        )
        self.conn.execute(
            recursive
            + "INSERT INTO context_observation_evidence_links(observation_ref,evidence_ref) "
            "SELECT printf('observation_scale_%06d',x),printf('evidence_scale_%06d',x) FROM n"
        )
        self.conn.execute(
            recursive
            + "INSERT INTO context_candidates(candidate_ref,observation_ref,desired_state,"
            "disposition,status,created_at,expires_at) SELECT "
            "printf('candidate_scale_%06d',x),printf('observation_scale_%06d',x),"
            "'completed_confirmed','verified','active',?,? FROM n",
            (occurred, expires),
        )
        self.conn.execute(
            recursive
            + "INSERT INTO context_candidate_evidence(candidate_ref,evidence_ref) "
            "SELECT printf('candidate_scale_%06d',x),printf('evidence_scale_%06d',x) FROM n"
        )
        self.conn.execute(
            recursive
            + "INSERT INTO context_verified("
            "verified_ref,candidate_ref,observation_ref,project_ref,workstream_ref,object_ref,"
            "version_ref,subject_ref,state,evidence_tier,source_kind,source_ref,provenance_ref,"
            "privacy_class,verification_scope,memory_eligible,valid_from,expires_at,status,created_at"
            ") SELECT printf('verified_scale_%06d',x),printf('candidate_scale_%06d',x),"
            "printf('observation_scale_%06d',x),?,?,?,?,?,'completed_confirmed','P0',"
            "'user_confirmation',printf('source_scale_%06d',x),"
            "printf('receipt_scale_%06d',x),'private_local','operational',0,?,?,'active',? FROM n",
            (
                self.project,
                self.workstream,
                self.object,
                self.version,
                self.owner,
                occurred,
                expires,
                occurred,
            ),
        )
        self.conn.execute(
            recursive
            + "INSERT INTO context_search_index(verified_ref,project_ref,occurred_at,"
            "project_label,workstream_label,object_label,version_label,state) "
            "SELECT printf('verified_scale_%06d',x),?,?,'Project Alpha',"
            "'Release Readiness','Scale Item','Candidate V1','completed_confirmed' FROM n",
            (self.project, occurred),
        )
        self.conn.execute(
            "WITH RECURSIVE n(x) AS (VALUES(10001) "
            "UNION ALL SELECT x+1 FROM n WHERE x<100000) "
            "INSERT INTO context_search_index(verified_ref,project_ref,occurred_at,"
            "project_label,workstream_label,object_label,version_label,state) "
            "SELECT printf('orphan_scale_%06d',x),?,?,'Project Alpha',"
            "'Release Readiness','Scale Orphan','Candidate V1','completed_confirmed' FROM n",
            (self.project, occurred),
        )
        self.conn.commit()
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM context_verified WHERE verified_ref LIKE 'verified_scale_%'"
            ).fetchone()[0],
            10_000,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM context_search_index").fetchone()[0],
            100_000,
        )
        statements: list[str] = []
        self.conn.set_trace_callback(
            lambda sql: statements.append(sql)
            if sql.lstrip().upper().startswith("SELECT")
            else None
        )
        try:
            snapshot = context_snapshot(
                self.conn,
                self._wire(
                    {
                        "query": "Scale",
                        "include_clues": False,
                        "limit": 200,
                    }
                ),
                now=self.now,
            )
        finally:
            self.conn.set_trace_callback(None)
        self.assertEqual(snapshot["status"], "available")
        self.assertEqual(
            len(snapshot["context_bundle_v1"]["verified_contexts"]),
            200,
        )
        self.assertEqual(snapshot["context_bundle_v1"]["unverified_clues"], [])
        self.assertLess(len(self._wire(snapshot)), 512 * 1024)
        self.assertLess(len(statements), 5_000)

    def test_phase2_pending_confirmation_scan_is_bounded_below_swift_timeout(self) -> None:
        observed_start = self.now.replace(minute=30).isoformat(timespec="seconds")
        observed_end = (self.now + timedelta(hours=1)).isoformat(timespec="seconds")
        stamp = self.now.isoformat(timespec="seconds")
        subject = json.dumps(
            {
                "event_label": "完成事项",
                "company": "",
                "round": "",
                "object": "批量确认",
                "label": "批量确认完成",
                "company_identified": False,
                "round_identified": False,
                "object_identified": True,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self.conn.execute(
            "WITH RECURSIVE n(x) AS (VALUES(1) "
            "UNION ALL SELECT x+1 FROM n WHERE x<10001) "
            "INSERT INTO activity_confirmation_items("
            "item_key,event_type,safe_observation,decision_question,why_uncertain,"
            "observed_start,observed_end,confidence,reason_codes,region_kind,"
            "actionability,source_activity_ids,deletion_group_ids,fingerprint,"
            "calibration_key,group_key,group_signature,subject,evidence,question_kind,"
            "candidate_origin,priority,safe_signal_types,suggested_workstream,"
            "suggested_outcome,notification_eligible,state,created_at,updated_at"
            ") SELECT printf('phase2_scale_card_%05d',x),'completion','批量确认已完成',"
            "'是否确实完成？','需要本人确认',?,?,0.6,'[\"possible_occurrence\"]',"
            "'main_content','actionable','[]','[]',printf('fp_scale_%05d',x),"
            "printf('cal_scale_%05d',x),printf('grp_scale_%05d',x),"
            "printf('sig_scale_%05d',x),?,'批量确认已完成','occurrence',"
            "'explicit_claim','high','[\"post_firewall_activity\"]',"
            "'Release Readiness','completed',1,'pending',?,? FROM n",
            (observed_start, observed_end, subject, stamp, stamp),
        )
        self.conn.commit()
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM activity_confirmation_items "
                "WHERE item_key LIKE 'phase2_scale_card_%'"
            ).fetchone()[0],
            10_001,
        )
        traced: list[str] = []
        self.conn.set_trace_callback(
            lambda sql: traced.append(sql)
            if "FROM activity_confirmation_items" in sql
            else None
        )
        started = time.monotonic()
        try:
            snapshot = context_snapshot(
                self.conn,
                self._wire({"limit": 50}),
                now=self.now,
            )
        finally:
            elapsed = time.monotonic() - started
            self.conn.set_trace_callback(None)
        self.assertEqual(snapshot["status"], "available")
        self.assertLessEqual(len(snapshot["confirmation_questions"]), 50)
        self.assertTrue(
            any("LIMIT 501" in statement for statement in traced),
            traced[:5],
        )
        self.assertLess(elapsed, 3.0)

        cards = list_activity_confirmation_cards(
            self.conn,
            range_name="today",
            start=self.now.replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            ).isoformat(timespec="seconds"),
            end=(self.now.replace(
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            ) + timedelta(days=1)).isoformat(timespec="seconds"),
            limit=50,
            scan_limit=500,
        )
        self.assertEqual(cards["candidate_scan_limit"], 500)
        self.assertTrue(cards["candidate_scan_truncated"])

        delivery_started = time.monotonic()
        status = delivery_status(self.conn, self._wire({}), now=self.now)
        delivery_elapsed = time.monotonic() - delivery_started
        self.assertEqual(status["status"], "available")
        self.assertTrue(status["daily_report"]["needs_input"])
        self.assertLess(delivery_elapsed, 3.0)

    def test_phase2_linked_legacy_reanswer_and_privacy_reclassification_fail_closed(self) -> None:
        action_now = datetime.now(UTC).replace(microsecond=0)
        activity_id = "activity_phase2cascade"
        deletion_group = "deletion_phase2cascade"
        self.conn.execute(
            "INSERT INTO extracted_activities("
            "id,session_id,started_at,ended_at,activity_summary,apps,domains,"
            "evidence_refs,privacy_level,redaction_confidence,quarantine_status,"
            "dedupe_status,deletion_group_id) VALUES(?,?,?,?,?,'[]','[]','[]',"
            "'normal',1.0,'clear','unique',?)",
            (
                activity_id,
                "session_phase2cascade",
                action_now.isoformat(timespec="seconds"),
                (action_now + timedelta(minutes=1)).isoformat(timespec="seconds"),
                "Source-bound confirmation",
                deletion_group,
            ),
        )
        item_key = "phase2_source_bound"
        self._insert_phase2_confirmation_card(
            item_key,
            object_label="Source-bound completion",
            at=action_now.astimezone(),
        )
        self.conn.execute(
            "UPDATE activity_confirmation_items SET source_activity_ids=?,"
            "deletion_group_ids=? WHERE item_key=?",
            (
                json.dumps([activity_id]),
                json.dumps([deletion_group]),
                item_key,
            ),
        )
        nonverifying_payloads: list[dict[str, Any]] = []
        nonverifying_results: list[dict[str, Any]] = []
        for index, answer in enumerate(("ignored", "defer"), start=1):
            nonverifying_key = f"phase2_source_nonverifying_{index}"
            self._insert_phase2_confirmation_card(
                nonverifying_key,
                object_label=f"Source-bound non-verifying {index}",
                at=(action_now + timedelta(minutes=index)).astimezone(),
            )
            self.conn.execute(
                "UPDATE activity_confirmation_items SET source_activity_ids=?,"
                "deletion_group_ids=? WHERE item_key=?",
                (
                    json.dumps([activity_id]),
                    json.dumps([deletion_group]),
                    nonverifying_key,
                ),
            )
        self.conn.commit()
        questions = context_snapshot(
            self.conn,
            self._wire({}),
            now=action_now,
        )["confirmation_questions"]
        question = next(
            item for item in questions
            if "Source-bound completion" in item["title"]
        )
        recorded = owner_context_action(
            self.conn,
            self._wire(
                self._phase2_confirmation_action_payload(
                    question,
                    idempotency_key="idem_58000001",
                    answer="occurred",
                )
            ),
            now=action_now,
        )
        self.assertEqual(recorded["status"], "recorded")
        for index, answer in enumerate(("ignored", "defer"), start=1):
            refreshed_questions = context_snapshot(
                self.conn,
                self._wire({}),
                now=action_now,
            )["confirmation_questions"]
            question_for_answer = next(
                item
                for item in refreshed_questions
                if f"Source-bound non-verifying {index}" in item["title"]
            )
            payload_for_answer = self._phase2_confirmation_action_payload(
                question_for_answer,
                idempotency_key=f"idem_5800000{index + 1}",
                answer=answer,
            )
            result_for_answer = owner_context_action(
                self.conn,
                self._wire(payload_for_answer),
                now=action_now,
            )
            self.assertEqual(
                (result_for_answer["status"], result_for_answer["verification_state"]),
                ("recorded", "non_verifying"),
                result_for_answer,
            )
            nonverifying_payloads.append(payload_for_answer)
            nonverifying_results.append(result_for_answer)
        link = self.conn.execute(
            "SELECT * FROM context_activity_confirmation_links WHERE link_ref=?",
            (recorded["action_ref"],),
        ).fetchone()

        self._insert_phase2_confirmation_card(
            "phase2_linked_new_member",
            object_label="Linked late member",
            at=(action_now + timedelta(minutes=2)).astimezone(),
        )
        self.conn.execute(
            "UPDATE activity_confirmation_items SET group_key=?,group_signature=? "
            "WHERE item_key='phase2_linked_new_member'",
            (
                str(link["confirmation_key"]),
                "sig_phase2_linked_late",
            ),
        )
        self.conn.commit()
        linked_rows = [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM activity_confirmation_items WHERE group_key=?",
                (str(link["confirmation_key"]),),
            )
        ]
        blocked = answer_activity_confirmation_group(
            self.conn,
            group_key=str(link["confirmation_key"]),
            member_revision=_member_revision(linked_rows),
            answer="not_occurred",
            reason="other",
            workstream="Release Readiness",
            outcome="unknown",
        )
        self.assertEqual(
            (blocked["status"], blocked["error"]),
            ("blocked", "context_confirmation_linked_group_immutable"),
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT state FROM context_verified WHERE verified_ref=?",
                (link["verified_ref"],),
            ).fetchone()[0],
            "completed_confirmed",
        )

        handoff = build_local_handoff(
            self.conn,
            self._wire(
                {
                    "project_ref": self.conn.execute(
                        "SELECT project_ref FROM context_verified WHERE verified_ref=?",
                        (link["verified_ref"],),
                    ).fetchone()[0]
                }
            ),
            now=action_now,
        )
        self.assertEqual(handoff["status"], "available")
        source_row = self.conn.execute(
            "SELECT * FROM extracted_activities WHERE id=?",
            (activity_id,),
        ).fetchone()
        _delete_activity_confirmation_items(self.conn, source_row)
        self.conn.commit()
        self.assertIsNone(
            self.conn.execute(
                "SELECT 1 FROM activity_confirmation_items WHERE item_key=?",
                (item_key,),
            ).fetchone()
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_activity_confirmation_links WHERE link_ref=?",
                (recorded["action_ref"],),
            ).fetchone()[0],
            "revoked",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_verified WHERE verified_ref=?",
                (link["verified_ref"],),
            ).fetchone()[0],
            "revoked",
        )
        for payload_for_answer, result_for_answer in zip(
            nonverifying_payloads,
            nonverifying_results,
        ):
            self.assertEqual(
                tuple(
                    self.conn.execute(
                        "SELECT status,revocation_reason_code "
                        "FROM context_activity_confirmation_links WHERE link_ref=?",
                        (result_for_answer["action_ref"],),
                    ).fetchone()
                ),
                ("revoked", "privacy_reclassification"),
            )
            replay = owner_context_action(
                self.conn,
                self._wire(payload_for_answer),
                now=action_now,
            )
            self.assertEqual(replay["status"], "rejected")
            self.assertNotIn("reason_code", replay)
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_bundle_manifests WHERE bundle_ref=?",
                (handoff["handoff_ref"],),
            ).fetchone()[0],
            "restricted",
        )

    def test_phase2_delete_range_revokes_nonverifying_confirmation_links(self) -> None:
        action_now = datetime.now(UTC).replace(microsecond=0)
        payloads: list[dict[str, Any]] = []
        results: list[dict[str, Any]] = []
        for index, answer in enumerate(("ignored", "defer"), start=1):
            self._insert_phase2_confirmation_card(
                f"phase2_range_nonverifying_{index}",
                object_label=f"Range non-verifying {index}",
                at=(action_now + timedelta(minutes=index)).astimezone(),
            )
        questions = context_snapshot(
            self.conn,
            self._wire({}),
            now=action_now,
        )["confirmation_questions"]
        for index, answer in enumerate(("ignored", "defer"), start=1):
            if index > 1:
                questions = context_snapshot(
                    self.conn,
                    self._wire({}),
                    now=action_now,
                )["confirmation_questions"]
            question = next(
                item
                for item in questions
                if f"Range non-verifying {index}" in item["title"]
            )
            payload = self._phase2_confirmation_action_payload(
                question,
                idempotency_key=f"idem_5810000{index}",
                answer=answer,
            )
            result = owner_context_action(
                self.conn,
                self._wire(payload),
                now=action_now,
            )
            self.assertEqual(
                result["verification_state"],
                "non_verifying",
                result,
            )
            payloads.append(payload)
            results.append(result)
        deleted = delete_range(
            self.conn,
            action_now.isoformat(timespec="seconds"),
            (action_now + timedelta(minutes=5)).isoformat(timespec="seconds"),
            "user_deleted",
        )
        self.assertEqual(deleted["status"], "completed")
        for payload, result in zip(payloads, results):
            link = self.conn.execute(
                "SELECT status,revocation_reason_code "
                "FROM context_activity_confirmation_links WHERE link_ref=?",
                (result["action_ref"],),
            ).fetchone()
            self.assertEqual(
                tuple(link),
                ("revoked", "time_range_user_deleted"),
            )
            replay = owner_context_action(
                self.conn,
                self._wire(payload),
                now=action_now,
            )
            self.assertEqual(replay["status"], "rejected")
            self.assertNotIn("reason_code", replay)

    def test_phase2_activity_retention_preserves_90_day_verified_context(self) -> None:
        action_now = datetime.now(UTC).replace(microsecond=0)
        self._insert_phase2_confirmation_card(
            "phase2_retention_verified",
            object_label="Retention independent verified",
            at=action_now.astimezone(),
        )
        question = next(
            item
            for item in context_snapshot(
                self.conn,
                self._wire({}),
                now=action_now,
            )["confirmation_questions"]
            if "Retention independent verified" in item["title"]
        )
        recorded = owner_context_action(
            self.conn,
            self._wire(
                self._phase2_confirmation_action_payload(
                    question,
                    idempotency_key="idem_58200001",
                    answer="occurred",
                )
            ),
            now=action_now,
        )
        link = self.conn.execute(
            "SELECT * FROM context_activity_confirmation_links WHERE link_ref=?",
            (recorded["action_ref"],),
        ).fetchone()
        project_ref = self.conn.execute(
            "SELECT project_ref FROM context_verified WHERE verified_ref=?",
            (link["verified_ref"],),
        ).fetchone()[0]
        handoff = build_local_handoff(
            self.conn,
            self._wire({"project_ref": project_ref}),
            now=action_now,
        )
        expired = delete_range(
            self.conn,
            action_now.isoformat(timespec="seconds"),
            (action_now + timedelta(minutes=2)).isoformat(timespec="seconds"),
            "retention_expired",
            source_status="expired_deleted",
        )
        self.assertEqual(expired["status"], "completed")
        self.assertIsNone(
            self.conn.execute(
                "SELECT 1 FROM activity_confirmation_items "
                "WHERE item_key='phase2_retention_verified'"
            ).fetchone()
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_activity_confirmation_links "
                "WHERE link_ref=?",
                (recorded["action_ref"],),
            ).fetchone()[0],
            "verified",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_verified WHERE verified_ref=?",
                (link["verified_ref"],),
            ).fetchone()[0],
            "active",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT status FROM context_bundle_manifests WHERE bundle_ref=?",
                (handoff["handoff_ref"],),
            ).fetchone()[0],
            "active",
        )




if __name__ == "__main__":
    unittest.main()
